/**
 * Plain-text helpers for what the @dlne participant writes into VS Code's Chat view. There is no VS Code API in
 * here, so the tests can feed these any text in any chunking.
 */
import type { FileDiffData } from '../agent/diffManager';

/** `text` in a code fence its own content cannot close: longer than any run of backticks inside it. */
export function fenced(language: string, text: string): string {
  let longest = 0;
  for (const run of text.match(/`+/g) ?? []) longest = Math.max(longest, run.length);
  const fence = '`'.repeat(Math.max(3, longest + 1));
  return `${fence}${language}\n${text}\n${fence}`;
}

/** How many lines of a file's diff the chat shows. */
export const DIFF_MAX_LINES = 60;

/**
 * The block shown after the agent changed a file. DiffManager's lines already carry their own markers ("+ ", "- ",
 * two spaces for context), so they are printed as they are: adding the markers again printed "+ +" and "- -".
 */
export function diffBlock(diff: FileDiffData, maxLines: number = DIFF_MAX_LINES): string {
  const shown = diff.lines.slice(0, maxLines).map((line) => line.text).join('\n');
  const hidden = diff.lines.length - maxLines;
  return (
    `\n\n📄 **Modified \`${diff.path}\`** (+${diff.additions} / -${diff.deletions})\n${fenced('diff', shown)}\n` +
    (hidden > 0 ? `\n_${hidden} more line${hidden === 1 ? '' : 's'} not shown. Open the file to see them._\n` : '')
  );
}

/**
 * Slash commands that answer without the agent. A thread whose only turns are these has not begun a conversation
 * with the agent, so it is not what makes a later message "the next one of this thread".
 */
const COMMANDS_WITHOUT_THE_AGENT = new Set(['status', 'model', 'contribute', 'clear', 'reset']);

/**
 * The turns of a chat thread (`chatContext.history`) that were a conversation with the agent: the thread's `/status`,
 * `/model`, `/contribute` and `/clear` turns left out. A turn that does not say what it was counts.
 */
export function agentTurns(history: readonly unknown[] | undefined): readonly unknown[] {
  return (history ?? []).filter((turn: any) => !COMMANDS_WITHOUT_THE_AGENT.has(turn?.command ?? turn?.result?.metadata?.command));
}

// ---- tool calls the model writes as text ----------------------------------------------------------------

/** What `rest` (the text after an opening fence) says about the fence: not yet known, a possible tool call, or code. */
function classifyFence(rest: string): 'wait' | 'candidate' | 'plain' {
  const word = /^[A-Za-z_]*/.exec(rest)![0];
  const after = rest.slice(word.length);
  // The info word may still be arriving.
  if (after === '') return 'json'.startsWith(word) || 'tool_call'.startsWith(word) ? 'wait' : 'plain';
  if (word !== '' && word !== 'json' && word !== 'tool_call') return 'plain';
  const body = after.replace(/^\s+/, '');
  if (body === '') return 'wait';
  return body[0] === '{' ? 'candidate' : 'plain';
}

/**
 * The tool a fenced block calls, or null. Read the way the agent loop reads it (agentLoop.ts tryExtractJsonTool): the
 * JSON's `name`, `tool` or `function.name`, or, when the JSON is broken, the first `"name"` in it.
 */
function toolCalledBy(block: string, isToolName: (name: string) => boolean): string | null {
  const json = /```(?:json|tool_call)?\s*(\{[\s\S]*?\})\s*```/.exec(block)?.[1];
  if (!json) return null;
  try {
    const parsed = JSON.parse(json);
    const name = parsed?.name || parsed?.tool || parsed?.function?.name;
    return typeof name === 'string' && isToolName(name) ? name : null;
  } catch {
    const name = /"name"\s*:\s*"([^"]+)"/.exec(json)?.[1];
    return name && isToolName(name) ? name : null;
  }
}

/** Trailing backticks of `text` (at most two): the start of a fence that the next chunk may complete. */
function trailingBackticks(text: string): number {
  return text.endsWith('``') ? 2 : text.endsWith('`') ? 1 : 0;
}

/**
 * Passes the model's answer on to the chat but leaves out the tool calls it writes as fenced JSON
 * (```json {"name": "read_file", "arguments": {...}} ```). A model that cannot make native tool calls writes them
 * this way, the agent loop runs them, and the chat shows the step as a progress line: the JSON itself, a whole
 * file's text in a write_file call, is not an answer. The Chat view cannot take text back once it is sent, so a
 * fence that may be a tool call is held until it closes and is judged then; every other text goes through at once.
 * What counts as a call is what the agent loop would run (it finds them with a pattern, wherever they are, not as
 * fences that open and close in pairs), so a JSON example in an answer is shown.
 */
export class ToolJsonFilter {
  private pending = '';
  /** `pending` starts with a fence that may open a tool call, and holds the text from there on. */
  private held = false;
  /** A dropped block's closing fence is followed by its line break, which goes with it. */
  private dropNextLineBreak = false;

  constructor(private readonly show: (text: string) => void, private readonly isToolName: (name: string) => boolean) {}

  push(delta: string): void {
    this.pending += delta;
    this.process(false);
  }

  /** The turn is over, so whatever is still held was never a finished tool call: it is shown. */
  flush(): void {
    this.process(true);
    this.held = false;
    this.dropNextLineBreak = false;
  }

  private out(text: string): void {
    if (text) this.show(text);
  }

  /** The end (just after the closing fence) of the held block, or -1: a fence closes it once a `}` ends what came before. */
  private heldBlockEnd(): number {
    const start = this.pending.indexOf('{');
    for (let fence = this.pending.indexOf('```', start); fence !== -1; fence = this.pending.indexOf('```', fence + 3)) {
      if (this.pending.slice(start, fence).trimEnd().endsWith('}')) return fence + 3;
    }
    return -1;
  }

  private process(final: boolean): void {
    for (;;) {
      if (this.dropNextLineBreak) {
        if (this.pending === '' || (this.pending === '\r' && !final)) return;
        this.pending = this.pending.replace(/^\r?\n/, '');
        this.dropNextLineBreak = false;
      }
      if (this.held) {
        const end = this.heldBlockEnd();
        if (end === -1) {
          if (final) {
            this.out(this.pending);
            this.pending = '';
          }
          return;
        }
        const block = this.pending.slice(0, end);
        this.pending = this.pending.slice(end);
        this.held = false;
        if (toolCalledBy(block, this.isToolName)) this.dropNextLineBreak = true;
        else this.out(block);
        continue;
      }
      const fence = this.pending.indexOf('```');
      if (fence === -1) {
        const keep = final ? 0 : trailingBackticks(this.pending);
        this.out(this.pending.slice(0, this.pending.length - keep));
        this.pending = this.pending.slice(this.pending.length - keep);
        return;
      }
      this.out(this.pending.slice(0, fence));
      this.pending = this.pending.slice(fence);
      const rest = this.pending.slice(3);
      // In a longer run of backticks only the last three can open a call: the agent loop's pattern skips the others.
      if (rest.startsWith('`')) {
        this.out('`');
        this.pending = this.pending.slice(1);
        continue;
      }
      const verdict = classifyFence(rest);
      if (verdict === 'wait' && !final) return;
      if (verdict === 'candidate') {
        this.held = true;
        continue;
      }
      // Code, or text that only has three backticks in it: shown, a backtick at a time, so the next fence is looked at.
      this.out('`');
      this.pending = this.pending.slice(1);
    }
  }
}
