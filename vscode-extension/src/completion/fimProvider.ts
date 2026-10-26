import * as vscode from 'vscode';
import { CompletionOutcome, DlneClient } from '../client/dlneClient';
import { logOnce } from '../log';
import { REDACTION_MARK, isExcludedFile, redactSecrets } from '../privacy';

// Prompt budget. Prefill is the cost a completion pays before its first token (measured ~250
// tokens/s on the public swarm), and the old 100 + 40 lines made a cold completion take 3-4 s.
// The few lines around the cursor carry nearly all of the signal.
const PREFIX_LINES = 50;
const PREFIX_CHARS = 4000;
const SUFFIX_LINES = 10;
const SUFFIX_CHARS = 600;
// Token caps: the rest of one line, or a few lines of a block. Every token is waited for (about 38 a second on
// the public swarm), so these stay short; a completion that reaches its cap keeps only its complete lines.
const MAX_TOKENS_LINE = 48;
const MAX_TOKENS_BLOCK = 64;
// The prompt's first line moves once per this many lines typed, not once per line. The model's cache can reuse a
// prompt only up to the first token that changed, so a front that moves on every new line re-reads the whole
// prompt each time (measured: 654 tokens on TheWall instead of about 5).
const PREFIX_BLOCK = 8;

// Stands for the cursor while the text around it is redacted (U+2063 INVISIBLE SEPARATOR: code has no reason to hold it).
const CURSOR_MARK = '⁣';

const MIN_DELAY_MS = 75;
const MAX_DELAY_MS = 2000;

/** The prompt pieces for a completion at `position`, trimmed to the budget above. */
export function buildFimContext(document: vscode.TextDocument, position: vscode.Position): { prefix: string; suffix: string; midLine: boolean } {
  let startLine = Math.floor(Math.max(0, position.line - PREFIX_LINES) / PREFIX_BLOCK) * PREFIX_BLOCK;
  let prefix = document.getText(new vscode.Range(new vscode.Position(startLine, 0), position));
  // Over the character budget: drop whole blocks from the top (not single characters), so the front still holds still.
  while (prefix.length > PREFIX_CHARS && startLine < position.line) {
    startLine = Math.min(position.line, startLine + PREFIX_BLOCK);
    prefix = document.getText(new vscode.Range(new vscode.Position(startLine, 0), position));
  }
  if (prefix.length > PREFIX_CHARS) {
    prefix = prefix.slice(prefix.length - PREFIX_CHARS);
  }

  const endLine = Math.min(document.lineCount - 1, position.line + SUFFIX_LINES);
  let suffix = document.getText(new vscode.Range(position, new vscode.Position(endLine, document.lineAt(endLine).text.length)));
  if (suffix.length > SUFFIX_CHARS) {
    suffix = suffix.slice(0, SUFFIX_CHARS);
  }

  const restOfLine = document.lineAt(position.line).text.slice(position.character);
  return { prefix, suffix, midLine: restOfLine.trim().length > 0 && !closesBlock(restOfLine) };
}

/**
 * The rest of the line only closes a block: the `}` the editor typed for you after `{` (and `});`, `},`). The
 * cursor is then at the start of a body, not inside a line, and a body starts with a new line: completing only
 * "the rest of this line" stopped at that first new line and never suggested anything.
 */
export function closesBlock(restOfLine: string): boolean {
  return /^\s*}[\s;),\]]*$/.test(restOfLine);
}

/**
 * A completion that reached its token cap stops wherever the cap fell, often mid-expression (`text.split('').`).
 * A block completion then keeps only its complete lines, and one with no complete line is not offered. A
 * completion inside a line is kept: it ends with that line anyway.
 */
export function trimCutOff(text: string, finishReason: string | undefined, midLine: boolean): string {
  if (finishReason !== 'length' || midLine) {
    return text;
  }
  const lastBreak = text.lastIndexOf('\n');
  return lastBreak > 0 ? text.slice(0, lastBreak) : '';
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
    // A file whose contents never leave the editor (.gitignore'd, .dlneignore, .env, keys) gets no
    // suggestions: completing it would send its text, and its neighbours, to the swarm.
    if (isExcludedFile(document.uri)) {
      return null;
    }

    // The folder this file belongs to decides its privacy mode (a public repository in a multi-root
    // workspace does not make its private neighbour public). A file in none of the open folders has no
    // mode of its own: with no folder named, the policy falls back to "the workspace's only folder", and a
    // public repository there would make this file's text public too. So: no suggestion.
    const folder = vscode.workspace.getWorkspaceFolder?.(document.uri);
    if (!folder && typeof vscode.workspace.getWorkspaceFolder === 'function' && (vscode.workspace.workspaceFolders?.length ?? 0) > 0) {
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
    const fileName = document.uri.fsPath;
    // Redacted as ONE text with a marker where the cursor is, then cut again at the marker. Redacting the two
    // halves apart missed a quoted secret the cursor sits in: while a password is being typed between quotes
    // (the editor closes the quote for you) the opening quote is in one half and the closing one in the
    // other, so neither half looked like an assignment and the half-typed password went out. If the marker
    // does not survive, the cursor is inside a secret: no suggestion, nothing sent.
    if (prefix.includes(CURSOR_MARK) || suffix.includes(CURSOR_MARK)) {
      return null;     // the file already holds the marker character: the cut below could not be trusted
    }
    const redacted = redactSecrets(prefix + CURSOR_MARK + suffix, fileName).text;
    const cut = redacted.indexOf(CURSOR_MARK);
    if (cut < 0) {
      return null;
    }
    const fimPrompt = `<|fim_prefix|>${redacted.slice(0, cut)}<|fim_suffix|>${redacted.slice(cut + CURSOR_MARK.length)}<|fim_middle|>`;
    const stopTokens = ['<|fim_pad|>', '<|file_separator|>', '<|im_end|>', '\n\n\n'];
    if (midLine) {
      stopTokens.push('\n'); // completing inside a line: the rest of that line is all that fits
    }

    this.inFlight?.abort();
    const controller = new AbortController();
    this.inFlight = controller;
    const cancel = token.onCancellationRequested(() => controller.abort());
    try {
      // `folder` (above) decides the privacy mode. Session and redaction are the client's.
      let outcome: CompletionOutcome = {};
      const answer = await this.client.getCompletion(
        fimPrompt,
        midLine ? MAX_TOKENS_LINE : MAX_TOKENS_BLOCK,
        stopTokens,
        controller.signal,
        { workspaceFolder: folder, onOutcome: (o) => { outcome = o; } }
      );
      if (outcome.failure) {
        // No suggestion is shown either way; the log says why (once a minute for the same reason).
        logOnce(`fim:${outcome.failure}`, `Autocomplete: no suggestion, ${outcome.failure}.`);
      }
      const completionText = trimCutOff(answer, outcome.finishReason, midLine);
      if (!completionText || token.isCancellationRequested || controller.signal.aborted) {
        return null;
      }
      // The model saw [REDACTED] where a secret is; echoing it would put the marker into the user's code.
      if (completionText.includes(REDACTION_MARK)) {
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
