/**
 * The reply side of DLNE Swarm in VS Code's model picker: what of the swarm's answer is shown, which tool calls it
 * holds, and which of its "words" are really the coordinator speaking. No vscode import, so tests load it straight
 * from dist/.
 *
 * The Chat view stores a reply's text as the assistant's message and sends it back to the model on the next step, so
 * nothing that is not the model's answer may be shown as text: not its thinking, not a tool call written out as
 * tags, and not the coordinator's own notices (which arrive inside the answer's text). Those are never text; a
 * notice becomes an error VS Code shows as an error.
 *
 * Tags are only believed where a model really writes them. A tool call is a tag that opens a line (blanks before it
 * are fine), outside a code fence, and only when the request offered tools; the model's `<think>` is the start of the
 * answer. Anywhere else the same characters are ordinary text: an answer that explains a pom.xml's `<tools>` section,
 * or quotes `<think>`, must arrive whole.
 */
import { randomUUID } from 'crypto';
import type { StreamedToolCall } from './dlneClient';

// ---- where a tag may open -------------------------------------------------------------------------------------

/** The tags whose contents are never shown: the model's thinking, and a tool call written out as text. */
const TAG_NAMES = ['think', 'tools', 'function-call', 'tool_call'];
/** A tag at the start of a line, blanks before it allowed; its name must be followed by `>` or a blank (`<tools_list>` is not one). */
const TAG_LINE = /^([ \t]*)<(think|tools|function-call|tool_call)(?=[\s>])/;
/** A line that so far holds blanks and the beginning of a tag's name, and nothing more: it may still turn into a tag. */
const TAG_BEGUN = /^[ \t]*(<[a-z_-]*)$/;
const BLANK = /^[ \t]*$/;
/** A code fence line (CommonMark): up to three blanks, then three or more backticks or tildes. */
const FENCE_LINE = /^[ \t]{0,3}(`{3,}|~{3,})(.*)$/;
/** How much of a line is kept to tell a fence from text: its first characters are all that matter. */
const LINE_KEEP = 256;

interface Fence {
  char: string;
  length: number;
}

/** Where handled text has got to: the part of the current line so far, and the code fence it is inside (if any). */
interface Place {
  line: string;
  fence: Fence | null;
}

/** The fence state after one whole line (without its newline). */
function afterLine(fence: Fence | null, line: string): Fence | null {
  const found = FENCE_LINE.exec(line.replace(/\r$/, ''));
  if (!found) return fence;
  const [, mark, rest] = found;
  if (fence === null) {
    // An opening fence. A backtick fence's info string cannot hold a backtick (then it is code in a sentence).
    return mark[0] === '`' && rest.includes('`') ? null : { char: mark[0], length: mark.length };
  }
  // A closing fence: the same character, at least as long, and nothing after it but blanks.
  return mark[0] === fence.char && mark.length >= fence.length && BLANK.test(rest) ? null : fence;
}

/** `place` after `text` has been handled. */
function advance(place: Place, text: string): Place {
  let { line, fence } = place;
  let pos = 0;
  while (pos < text.length) {
    const newline = text.indexOf('\n', pos);
    if (newline < 0) {
      line = (line + text.slice(pos)).slice(0, LINE_KEEP);
      break;
    }
    fence = afterLine(fence, (line + text.slice(pos, newline)).slice(0, LINE_KEEP));
    line = '';
    pos = newline + 1;
  }
  return { line, fence };
}

/**
 * The coordinator speaks inside an answer as a chunk of its own that begins `[System]` or `[System Alert`, after a
 * blank line (engine/coordinator.py: the "nudges", and the alert for an engine that failed). A model's text reaches
 * the client a few characters at a time, so a whole marker inside one chunk is not the model writing it.
 */
const NOTICE_START = /^\s*\[System(?:\]| Alert[\]:])/;

/** The longest end of `text` that is the beginning (but not all) of `tag`. */
function overlap(text: string, tag: string): number {
  for (let k = Math.min(tag.length - 1, text.length); k > 0; k--) {
    if (text.endsWith(tag.slice(0, k))) return k;
  }
  return 0;
}

/**
 * Takes the swarm's answer as it streams in, in pieces of any size, and hands back what may be shown now. Thinking and
 * tool-call tags are dropped where they count (see the top of this file; the calls are read from the finished answer, see
 * collectToolCalls), a tag split between two pieces is held until it can be told from text, and everything from a
 * coordinator notice on is kept for `finish`. Blank lines before the first words are dropped.
 */
export class ReplyFilter {
  private pending = '';
  private mode: 'text' | 'block' | 'notice' = 'text';
  private closing = '';
  /** Whether the block being skipped is a tool call (and not thinking). */
  private blockIsCall = false;
  private sawCall = false;
  private held = '';
  private started = false;
  private place: Place = { line: '', fence: null };

  /** `toolsOffered`: whether the request offered any tools. Without them a tool-call tag is only text. */
  constructor(private readonly toolsOffered: boolean) {}

  /** One piece of the answer's text; returns the part that is safe to show. */
  push(piece: string): string {
    if (!piece) return '';
    if (this.mode !== 'notice' && NOTICE_START.test(piece)) {
      const text = this.drain(true);
      this.mode = 'notice';
      this.held = piece;
      return text;
    }
    this.pending += piece;
    return this.drain(false);
  }

  /**
   * The answer is complete: the rest of the text, the coordinator's notice if there was one (trimmed), whether a
   * tool call was still open when the answer stopped (it was cut off), and whether the answer held any tool-call tag.
   */
  finish(): { text: string; notice: string | null; unfinishedCall: boolean; sawCallBlock: boolean } {
    const unfinishedCall = this.mode === 'block' && this.blockIsCall;
    const text = this.drain(true);
    return { text, notice: this.mode === 'notice' ? this.held.trim() : null, unfinishedCall, sawCallBlock: this.sawCall };
  }

  /** Whether a tag of this name opens a hidden block at a place that counts. `nothingShown`: only blanks come before it. */
  private opens(name: string, nothingShown: boolean): boolean {
    return name === 'think' ? nothingShown : this.toolsOffered;
  }

  /**
   * The first tag in the pending text that opens a hidden block, read line by line (a tag counts only at the start of a
   * line, outside a code fence). Without one, how much of the end may still become a tag and so is held back.
   */
  private findTag(final: boolean): { tag: string | null; at: number; keep: number } {
    const text = this.pending;
    let place = this.place;
    let nothingShown = !this.started;
    let keep = 0;
    let pos = 0;
    while (pos < text.length) {
      const newline = text.indexOf('\n', pos);
      const end = newline < 0 ? text.length : newline + 1;
      const piece = text.slice(pos, end);
      if (place.fence === null && BLANK.test(place.line)) {
        const found = TAG_LINE.exec(piece);
        if (found && this.opens(found[2], nothingShown)) return { tag: found[2], at: pos + found[1].length, keep: 0 };
        if (newline < 0 && !final) {
          const begun = TAG_BEGUN.exec(piece);
          if (begun && TAG_NAMES.some((name) => this.opens(name, nothingShown) && `<${name}`.startsWith(begun[1]))) {
            keep = begun[1].length;
          }
        }
      }
      if (nothingShown && /\S/.test(piece)) nothingShown = false;
      place = advance(place, piece);
      pos = end;
    }
    return { tag: null, at: -1, keep };
  }

  private drain(final: boolean): string {
    let out = '';
    for (;;) {
      if (this.mode === 'notice') {
        this.held += this.pending;
        this.pending = '';
        return out;
      }
      if (this.mode === 'block') {
        const end = this.pending.indexOf(this.closing);
        if (end >= 0) {
          this.pending = this.pending.slice(end + this.closing.length);
          this.mode = 'text';
          // Text starts afresh after a block: a second call may follow the first on the same line.
          this.place = { line: '', fence: this.place.fence };
          continue;
        }
        // Not closed yet: all of it is dropped, except an end that may be the start of the closing tag.
        this.pending = final ? '' : this.pending.slice(this.pending.length - overlap(this.pending, this.closing));
        return out;
      }
      const found = this.findTag(final);
      if (found.tag !== null) {
        out += this.emit(this.pending.slice(0, found.at));
        this.pending = this.pending.slice(found.at + 1 + found.tag.length);
        this.mode = 'block';
        this.closing = `</${found.tag}>`;
        this.blockIsCall = found.tag !== 'think';
        if (this.blockIsCall) this.sawCall = true;
        continue;
      }
      const keep = final ? 0 : found.keep;
      out += this.emit(this.pending.slice(0, this.pending.length - keep));
      this.pending = this.pending.slice(this.pending.length - keep);
      return out;
    }
  }

  /** Text that is let through: noted for where the next line stands, then shown. */
  private emit(text: string): string {
    if (!text) return '';
    this.place = advance(this.place, text);
    return this.show(text);
  }

  private show(text: string): string {
    if (!this.started) {
      text = text.replace(/^\s+/, '');
      if (text) this.started = true;
    }
    return text;
  }
}

/**
 * A coordinator notice as a sentence: the marker is taken off and the lines are joined. ("[System] The cluster is
 * reorganising itself... please send your message again in a moment." becomes the sentence after the marker.)
 */
export function cleanNotice(notice: string): string {
  return notice
    .replace(/^\s*\[System(?: Alert)?(?::\s*([^\]]*)\]|\])\s*/, '$1 ')
    .replace(/\s+/g, ' ')
    .trim();
}

// ---- tool calls -----------------------------------------------------------------------------------------------

export interface ParsedCall {
  name: string;
  input: Record<string, unknown>;
}

export interface OutgoingCall extends ParsedCall {
  id: string;
}

/** A call the swarm made that is not passed on: for a tool VS Code did not offer, or with arguments that are not JSON. */
export interface SkippedCall {
  name: string;
  why: 'not-offered' | 'bad-arguments';
}

const isPlainObject = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value);

/** `text` without the model's `<think>…</think>` scratchpad at its start (several in a row, or one never closed, included). */
export function withoutLeadingThinking(text: string): string {
  let rest = text;
  for (;;) {
    const opened = /^\s*<think>/.exec(rest);
    if (!opened) return rest;
    const end = rest.indexOf('</think>', opened[0].length);
    if (end < 0) return '';
    rest = rest.slice(end + '</think>'.length);
  }
}

/**
 * A call's arguments as an object. Complete JSON only: an answer cut off in the middle of a call must not run, and
 * a half-written command or file is exactly what repairing it would produce (the agent loop repairs for its own tools;
 * a tool VS Code offers is not ours to guess at). Empty arguments mean none.
 */
function parseArguments(text: string): Record<string, unknown> | null {
  if (typeof text !== 'string' || text.trim() === '') return {};
  try {
    const parsed: unknown = JSON.parse(text);
    return isPlainObject(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

/** One call from JSON text, in the shapes models write it: {"name", "arguments"}, {"tool", "parameters"}, {"function": {…}}, or flattened. */
function callFromJson(text: string, offered: ReadonlySet<string>): ParsedCall | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return null;
  }
  if (!isPlainObject(parsed)) return null;
  const fn = isPlainObject(parsed.function) ? parsed.function : undefined;
  const name = parsed.name ?? parsed.tool ?? fn?.name;
  if (typeof name !== 'string' || !offered.has(name)) return null;
  let args: unknown = parsed.arguments ?? parsed.parameters ?? parsed.args ?? fn?.arguments;
  if (args === undefined) {
    // Flattened: {"name": "run_in_terminal", "command": "ls"}
    const { name: _name, tool: _tool, function: _function, ...rest } = parsed;
    args = rest;
  }
  if (typeof args === 'string') {
    const decoded = parseArguments(args);
    if (decoded === null) return null;
    args = decoded;
  }
  return isPlainObject(args) ? { name, input: args } : null;
}

/** The JSON object that begins at `start` (a `{`), as text, found by matching braces (strings and escapes respected); null if it never closes. */
function objectAt(text: string, start: number): string | null {
  let depth = 0;
  let inString = false;
  for (let j = start; j < text.length; j++) {
    const c = text[j];
    if (inString) {
      if (c === '\\') j++;
      else if (c === '"') inString = false;
    } else if (c === '"') {
      inString = true;
    } else if (c === '{') {
      depth++;
    } else if (c === '}' && --depth === 0) {
      return text.slice(start, j + 1);
    }
  }
  return null;
}

/** The JSON objects in `text`, each as its own text. */
function jsonObjects(text: string): string[] {
  const found: string[] = [];
  for (let i = text.indexOf('{'); i >= 0; ) {
    const object = objectAt(text, i);
    if (object === null) break; // never closed: cut off
    found.push(object);
    i = text.indexOf('{', i + object.length);
  }
  return found;
}

/** The JSON objects of `text` that open a line (blanks before them are fine), outside a code fence: the rule a tag is held to. */
function lineStartObjects(text: string): string[] {
  const found: string[] = [];
  let place: Place = { line: '', fence: null };
  let pos = 0;
  while (pos < text.length) {
    const newline = text.indexOf('\n', pos);
    const end = newline < 0 ? text.length : newline + 1;
    const piece = text.slice(pos, end);
    if (place.fence === null && BLANK.test(place.line)) {
      const opens = /^[ \t]*\{/.exec(piece);
      if (opens) {
        const start = pos + opens[0].length - 1;
        const object = objectAt(text, start);
        if (object === null) break;
        found.push(object);
        pos = start + object.length;
        place = { line: '', fence: null };
        continue;
      }
    }
    place = advance(place, piece);
    pos = end;
  }
  return found;
}

/** One tool-call tag of an answer: its name, the attributes written in the opening tag, and what is between the tags. */
interface CallBlock {
  name: string;
  attributes: string;
  body: string;
}

/**
 * The tool-call tags of `text` that count, by the rule ReplyFilter hides them by: opening a line (blanks before it are fine),
 * outside a code fence, closed by its own closing tag. A block that never closes was cut off and is not here.
 */
function callBlocks(text: string): CallBlock[] {
  const blocks: CallBlock[] = [];
  let place: Place = { line: '', fence: null };
  let pos = 0;
  while (pos < text.length) {
    const newline = text.indexOf('\n', pos);
    const end = newline < 0 ? text.length : newline + 1;
    const piece = text.slice(pos, end);
    if (place.fence === null && BLANK.test(place.line)) {
      const opening = /^([ \t]*)<(tools|function-call|tool_call)(\s[^>]*)?>/.exec(piece);
      if (opening) {
        const bodyStart = pos + opening[0].length;
        const closing = `</${opening[2]}>`;
        const close = text.indexOf(closing, bodyStart);
        if (close < 0) break;
        blocks.push({ name: opening[2], attributes: opening[3] ?? '', body: text.slice(bodyStart, close) });
        pos = close + closing.length;
        place = { line: '', fence: null };
        continue;
      }
    }
    place = advance(place, piece);
    pos = end;
  }
  return blocks;
}

/**
 * The tool calls a model wrote into its answer as text, for a model whose template did not turn them into native
 * calls. The shapes are the ones the agent loop reads (agentLoop.ts parseInlineToolCalls), and the first shape that
 * holds a call wins:
 *   1. <tool_call>, <tools> or <function-call> around JSON;
 *   2. a fenced JSON block;
 *   3. <tool_call name="…"><param name="…">…</param></tool_call>;
 *   4. a bare {"name": "…", …} object.
 * Tags (1 and 3) and bare objects (4) count only where they open a line, outside a code fence, which is where ReplyFilter
 * hides the tags: a call quoted in a sentence or shown in a code sample is not a request. (A fenced block, 2, is the fence's own
 * business.) Only tools in `offered` count: a call for any other name is the model talking, not a request VS Code can run.
 */
export function parseInlineToolCalls(raw: string, offered: ReadonlySet<string>): ParsedCall[] {
  const text = withoutLeadingThinking(raw);
  if (offered.size === 0) return [];
  const calls: ParsedCall[] = [];
  const blocks = callBlocks(text);

  for (const block of blocks) {
    if (/\sname\s*=/i.test(block.attributes)) continue; // the form with parameters, below
    const inner = block.body.trim();
    const one = callFromJson(inner, offered);
    if (one) {
      calls.push(one);
    } else {
      for (const object of jsonObjects(inner)) {
        const call = callFromJson(object, offered);
        if (call) calls.push(call);
      }
    }
  }
  if (calls.length > 0) return calls;

  const fenced = /```(?:json|tool_call)?\s*(\{[\s\S]*?\})\s*```/g;
  for (let m = fenced.exec(text); m; m = fenced.exec(text)) {
    const call = callFromJson(m[1], offered);
    if (call) calls.push(call);
  }
  if (calls.length > 0) return calls;

  for (const block of blocks) {
    const named = /\sname\s*=\s*["']([^"']+)["']/i.exec(block.attributes);
    if (block.name !== 'tool_call' || !named) continue;
    const name = named[1].trim();
    if (!offered.has(name)) continue;
    const input: Record<string, unknown> = {};
    const param = /<param\s+name=["']([^"']+)["']\s*>([\s\S]*?)<\/param>/g;
    for (let p = param.exec(block.body); p; p = param.exec(block.body)) input[p[1].trim()] = p[2].trim();
    calls.push({ name, input });
  }
  if (calls.length > 0) return calls;

  for (const object of lineStartObjects(text)) {
    if (!/^\{\s*"name"\s*:/.test(object)) continue;
    const call = callFromJson(object, offered);
    if (call) calls.push(call);
  }
  return calls;
}

/** A fresh id for a call the model wrote as text (a native call brings the engine's own). */
export function newCallId(): string {
  return `call_${randomUUID().replace(/-/g, '').slice(0, 16)}`;
}

/**
 * The calls to hand VS Code, and the ones left out. Native calls come first; text calls only when there are none,
 * as the agent loop does. A call for a tool that was not offered, or whose arguments are not complete JSON, is not
 * passed on (and is never repaired): VS Code would run it, or fail on it, as if the model had meant it.
 */
export function collectToolCalls(
  native: ReadonlyArray<StreamedToolCall>,
  answer: string,
  offered: ReadonlySet<string>,
  makeId: () => string = newCallId
): { calls: OutgoingCall[]; skipped: SkippedCall[] } {
  const calls: OutgoingCall[] = [];
  const skipped: SkippedCall[] = [];
  if (native.length > 0) {
    for (const call of native) {
      if (!offered.has(call.name)) {
        skipped.push({ name: call.name, why: 'not-offered' });
        continue;
      }
      const input = parseArguments(call.arguments);
      if (!input) {
        skipped.push({ name: call.name, why: 'bad-arguments' });
        continue;
      }
      calls.push({ id: call.id || makeId(), name: call.name, input });
    }
  } else {
    for (const call of parseInlineToolCalls(answer, offered)) calls.push({ id: makeId(), ...call });
  }
  return { calls, skipped };
}
