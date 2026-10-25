import * as vscode from 'vscode';
import { DlneClient } from '../client/dlneClient';

// Prompt budget. Prefill is the cost a completion pays before its first token (measured ~250
// tokens/s on the public swarm), and the old 100 + 40 lines made a cold completion take 3-4 s.
// The few lines around the cursor carry nearly all of the signal.
const PREFIX_LINES = 50;
const PREFIX_CHARS = 4000;
const SUFFIX_LINES = 10;
const SUFFIX_CHARS = 600;
const MAX_TOKENS = 48;

const MIN_DELAY_MS = 75;
const MAX_DELAY_MS = 2000;

/** The prompt pieces for a completion at `position`, trimmed to the budget above. */
export function buildFimContext(document: vscode.TextDocument, position: vscode.Position): { prefix: string; suffix: string; midLine: boolean } {
  const startLine = Math.max(0, position.line - PREFIX_LINES);
  let prefix = document.getText(new vscode.Range(new vscode.Position(startLine, 0), position));
  if (prefix.length > PREFIX_CHARS) {
    prefix = prefix.slice(prefix.length - PREFIX_CHARS);
  }

  const endLine = Math.min(document.lineCount - 1, position.line + SUFFIX_LINES);
  let suffix = document.getText(new vscode.Range(position, new vscode.Position(endLine, document.lineAt(endLine).text.length)));
  if (suffix.length > SUFFIX_CHARS) {
    suffix = suffix.slice(0, SUFFIX_CHARS);
  }

  const restOfLine = document.lineAt(position.line).text.slice(position.character);
  return { prefix, suffix, midLine: restOfLine.trim().length > 0 };
}

export class DlneFimProvider implements vscode.InlineCompletionItemProvider {
  private debounceTimer: NodeJS.Timeout | null = null;
  // Latest wins: a newer request aborts the one in flight instead of being dropped, so typing
  // during a slow completion no longer leaves a dead zone with no suggestions.
  private inFlight: AbortController | null = null;

  constructor(private client: DlneClient) {}

  async provideInlineCompletionItems(
    document: vscode.TextDocument,
    position: vscode.Position,
    context: vscode.InlineCompletionContext,
    token: vscode.CancellationToken
  ): Promise<vscode.InlineCompletionItem[] | null> {
    // Only run FIM on real workspace source files — NEVER on VS Code Chat inputs, Output panels, or virtual buffers
    if (document.uri.scheme !== 'file') {
      return null;
    }

    const config = vscode.workspace.getConfiguration('dlne');
    if (!config.get<boolean>('enableInlineCompletion', true)) {
      return null;
    }

    const configured = Number(config.get<number>('inlineCompletionDelay', 150));
    const delay = Math.min(MAX_DELAY_MS, Math.max(MIN_DELAY_MS, Number.isFinite(configured) ? configured : 150));

    // Real debounce: wait until user stops typing for `delay` ms
    const cancelledDuringDebounce = await new Promise<boolean>((resolve) => {
      if (this.debounceTimer) {
        clearTimeout(this.debounceTimer);
      }
      const disp = token.onCancellationRequested(() => {
        if (this.debounceTimer) {
          clearTimeout(this.debounceTimer);
          this.debounceTimer = null;
        }
        disp.dispose();
        resolve(true);
      });
      this.debounceTimer = setTimeout(() => {
        this.debounceTimer = null;
        disp.dispose();
        resolve(token.isCancellationRequested);
      }, delay);
    });

    if (cancelledDuringDebounce || token.isCancellationRequested) {
      return null;
    }

    const { prefix, suffix, midLine } = buildFimContext(document, position);
    if (!prefix.trim()) {
      return null;
    }

    // Standard Qwen2.5-Coder / DeepSeek FIM formatting
    const fimPrompt = `<|fim_prefix|>${prefix}<|fim_suffix|>${suffix}<|fim_middle|>`;
    const stopTokens = ['<|fim_pad|>', '<|file_separator|>', '<|im_end|>', '\n\n\n'];
    if (midLine) {
      stopTokens.push('\n'); // completing inside a line: the rest of that line is all that fits
    }

    this.inFlight?.abort();
    const controller = new AbortController();
    this.inFlight = controller;
    const cancel = token.onCancellationRequested(() => controller.abort());
    try {
      const completionText = await this.client.getCompletion(fimPrompt, MAX_TOKENS, stopTokens, controller.signal);
      if (!completionText || token.isCancellationRequested || controller.signal.aborted) {
        return null;
      }

      const item = new vscode.InlineCompletionItem(
        completionText,
        new vscode.Range(position, position)
      );
      return [item];
    } finally {
      cancel.dispose();
      if (this.inFlight === controller) {
        this.inFlight = null;
      }
    }
  }
}
