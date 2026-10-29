/**
 * The ledger: an exact record of what the agent read, changed, ran, searched, listed, checked and
 * remembered, and of what was declined, refused or failed (docs/agent-memory.md, section 5.2).
 *
 * The agent's message history is shortened when it grows too long, and the model's own notes are
 * only as good as its memory. The ledger is kept outside that history: the AgentLoop calls
 * `record()` right after every tool call, and puts `render()` into the working-memory block, so the
 * facts survive however many old messages are dropped.
 *
 * Nothing here is guessed. Every entry is read from the arguments the model gave and from the exact
 * text the tools return, so this file knows the tools' wording (`executeToolInternal`,
 * `checkApproval`, `privacyRefusal` and `capOutput` in tools.ts, `excludedNotice` in privacy).
 * If one of those strings changes, the matching constant below changes with it (and the tests fail
 * until it does).
 *
 * This module must stay free of `vscode`, so it is light and testable: ToolCallResult is imported as
 * a type only.
 */
import type { ToolCallResult } from './tools';

/** The default size limit of `render()`, in characters. */
export const LEDGER_MAX_CHARS = 5000;

// --- Retention: how many entries of each kind are kept; past that, the oldest go. ---
const MAX_READ_PATHS = 200;
const MAX_COMMANDS = 40;
const MAX_OTHER = 30; // searches, listings, diagnostics, memories, declines, and each of refused / failed

/** When `render()` must shorten, each group keeps at least this many of its newest entries first. */
const KEEP_NEWEST = 10;

// --- Limits on the text of one entry, so one huge command or message cannot crowd out the rest. ---
const COMMAND_CHARS = 200;
const COMMAND_LINE_CHARS = 160;
const QUERY_CHARS = 120;
const FACT_CHARS = 300;
const MESSAGE_CHARS = 120;

// --- The tools' exact wording. ---
const DECLINED_PREFIX = 'The user declined: ';
const DECLINED_SUFFIX = '. Do not retry it';
const EXCLUDED_NOTICE = /^This file is excluded for privacy \((.*?)\): /;
const REDACTION_REFUSAL = 'Refused: the text contains';
const COMMAND_FAILED = /^Command failed \(exit code ([^)]*)\):/;
const WRITE_OK = /^Successfully (created|updated) file: /;
const EDIT_OK = 'Successfully patched';
const DIFF_STATS = /\(\+(\d+) lines?, -(\d+) lines?\)/;
const SEARCH_HIT = /^Matches found in (\d+) files?:/;
const SEARCH_MISS = 'No matches found for';
// What `remember` says when it saved a fact (memory.ts, rememberFact).
const REMEMBERED_PROJECT = '📝 Remembered for this project: ';
const REMEMBERED_USER = '📝 Remembered for you: ';
const ALREADY_REMEMBERED = 'Already remembered:';
const FAILED_PREFIX = 'Tool execution failed: ';
/** What `capOutput` puts between the head and the tail of an output it shortened. */
const TRUNCATION_MARK = /\n\n\.\.\. \[Output truncated: \d+ characters omitted[^\]]*\] \.\.\.\n\n/;
const NUMBERED_LINE = /^(\d+):(?: |$)/;

type Range = [number, number];

/** One file the model has read. `ranges` are sorted and never overlap or touch. */
interface ReadEntry {
  path: string;
  ranges: Range[];
  /** The whole file came back in one read, and nothing was cut from it. */
  whole: boolean;
  /** The file's line count when `whole` and known, else 0. */
  lines: number;
}

/** Everything done to one file by write_file and edit_file_diff. */
interface ChangeEntry {
  path: string;
  created: boolean;
  rewrites: number;
  edits: number;
  additions: number;
  deletions: number;
}

interface RanEntry {
  command: string;
  /** The exit code as the tool reported it ("0" when the command did not fail). */
  exit: string;
  /** The first output line of a failed command, else ''. */
  line: string;
}

interface SearchEntry { query: string; result: string }

interface DiagnosticEntry {
  target: string;
  errors: number;
  warnings: number;
  /** The output was cut by capOutput, so the counts are a lower bound. */
  partial: boolean;
}

interface RememberedEntry { scope: 'project' | 'user'; fact: string }

interface TroubleEntry { kind: 'refused' | 'failed'; text: string }

/** The saved form (section 8's `AgentStateJSON.ledger`). */
export interface LedgerJSON {
  version: 1;
  reads: ReadEntry[];
  changes: ChangeEntry[];
  ran: RanEntry[];
  searches: SearchEntry[];
  listed: string[];
  diagnostics: DiagnosticEntry[];
  remembered: RememberedEntry[];
  declined: string[];
  trouble: TroubleEntry[];
}

/** One group of lines in the rendered ledger. `items` run from the oldest to the newest. */
interface Group {
  title: string;
  /** The items go on the title's own line, separated by commas (the "Listed:" group). */
  inline: boolean;
  items: string[];
}

// --- Small helpers ---

/** One line, whitespace collapsed, cut to `max` characters. */
function clip(text: string, max: number): string {
  const flat = text.replace(/\s+/g, ' ').trim();
  return flat.length > max ? flat.slice(0, max) + '…' : flat;
}

/** The path as the model gave it. */
function pathOf(args: Record<string, any>, fallback = ''): string {
  return typeof args.path === 'string' && args.path ? args.path : fallback;
}

/** Adds `item` as the newest of `list`, then drops the oldest ones beyond `cap`. */
function pushCapped<T>(list: T[], item: T, cap: number): void {
  list.push(item);
  while (list.length > cap) list.shift();
}

/** Puts `value` last in `map` (the most recently touched), dropping the oldest keys beyond `cap`. */
function touch<T>(map: Map<string, T>, key: string, value: T, cap: number): void {
  map.delete(key);
  map.set(key, value);
  while (map.size > cap) map.delete(map.keys().next().value as string);
}

function plural(count: number, noun: string, exact = true): string {
  return `${count}${exact ? '' : '+'} ${count === 1 && exact ? noun : noun + 's'}`;
}

/** Adds [start, end] to a sorted list of ranges, merging every range it overlaps or touches. */
function addRange(ranges: Range[], start: number, end: number): Range[] {
  const merged: Range[] = [];
  let [from, to] = start <= end ? [start, end] : [end, start];
  for (const [s, e] of [...ranges, [from, to] as Range].sort((a, b) => a[0] - b[0])) {
    const last = merged[merged.length - 1];
    if (last && s <= last[1] + 1) last[1] = Math.max(last[1], e);
    else merged.push([s, e]);
  }
  return merged;
}

/**
 * Which line numbers of a read_file output the model actually saw.
 *
 * Output is "N: text" per line. `capOutput` keeps only the head (3500 characters) and the tail
 * (1500 characters) of a long output, so the middle can be missing: then two ranges come back, and
 * `cut` is true. The tail starts at an arbitrary character, so its first line may be a fragment
 * and is skipped; every line after it is whole and numbered, which also means the last numbered
 * line of a tail is the true last line of the read (a huge final line leaves no numbered line in
 * the tail at all, and the tail then adds no range rather than a wrong one).
 */
function seenLines(output: string): { ranges: Range[]; cut: boolean; last: number } {
  const span = (text: string): Range | null => {
    let first = 0;
    let last = 0;
    for (const line of text.split('\n')) {
      const m = NUMBERED_LINE.exec(line);
      if (!m) continue;
      if (!first) first = Number(m[1]);
      last = Number(m[1]);
    }
    return first ? [first, last] : null;
  };
  const mark = TRUNCATION_MARK.exec(output);
  const pieces = mark
    ? [output.slice(0, mark.index), output.slice(mark.index + mark[0].length).replace(/^[^\n]*\n/, '')]
    : [output];
  const ranges = pieces.map(span).filter((r): r is Range => r !== null);
  const last = ranges.length ? ranges[ranges.length - 1][1] : 0;
  return { ranges, cut: mark !== null, last };
}

function isCut(output: string): boolean {
  return TRUNCATION_MARK.test(output);
}

/** The first non-empty line of `text`, shortened, for the "why it failed" part of an entry. */
function firstLine(text: string, max = MESSAGE_CHARS): string {
  const line = text.split('\n').map((l) => l.trim()).find((l) => l.length > 0) ?? '';
  return clip(line.startsWith(FAILED_PREFIX) ? line.slice(FAILED_PREFIX.length) : line, max);
}

// --- Validation, for fromJSON ---

const isObject = (v: unknown): v is Record<string, any> => typeof v === 'object' && v !== null && !Array.isArray(v);
const isString = (v: unknown): v is string => typeof v === 'string';
const isCount = (v: unknown): v is number => typeof v === 'number' && Number.isInteger(v) && v >= 0;
const isList = <T>(v: unknown, ok: (item: unknown) => item is T): v is T[] => Array.isArray(v) && v.every(ok);

const isRange = (v: unknown): v is Range =>
  Array.isArray(v) && v.length === 2 && isCount(v[0]) && isCount(v[1]) && v[0] >= 1 && v[0] <= v[1];

/** Ranges must be sorted and apart, as addRange leaves them. */
const isTidy = (ranges: Range[]) => ranges.every((r, i) => i === 0 || r[0] > ranges[i - 1][1] + 1);

const isReadEntry = (v: unknown): v is ReadEntry =>
  isObject(v) && isString(v.path) && isList(v.ranges, isRange) && isTidy(v.ranges) &&
  typeof v.whole === 'boolean' && isCount(v.lines);

const isChangeEntry = (v: unknown): v is ChangeEntry =>
  isObject(v) && isString(v.path) && typeof v.created === 'boolean' && isCount(v.rewrites) && isCount(v.edits) &&
  isCount(v.additions) && isCount(v.deletions);

const isRanEntry = (v: unknown): v is RanEntry =>
  isObject(v) && isString(v.command) && isString(v.exit) && isString(v.line);

const isSearchEntry = (v: unknown): v is SearchEntry => isObject(v) && isString(v.query) && isString(v.result);

const isDiagnosticEntry = (v: unknown): v is DiagnosticEntry =>
  isObject(v) && isString(v.target) && isCount(v.errors) && isCount(v.warnings) && typeof v.partial === 'boolean';

const isRememberedEntry = (v: unknown): v is RememberedEntry =>
  isObject(v) && (v.scope === 'project' || v.scope === 'user') && isString(v.fact);

const isTroubleEntry = (v: unknown): v is TroubleEntry =>
  isObject(v) && (v.kind === 'refused' || v.kind === 'failed') && isString(v.text);

export class Ledger {
  // Maps are keyed by path (or target), ordered from the least to the most recently touched.
  private reads = new Map<string, ReadEntry>();
  private changes = new Map<string, ChangeEntry>();
  private ran: RanEntry[] = [];
  private searches = new Map<string, SearchEntry>();
  private listed: string[] = [];
  private diagnostics = new Map<string, DiagnosticEntry>();
  private remembered: RememberedEntry[] = [];
  private declined: string[] = [];
  private trouble: TroubleEntry[] = [];

  /** Notes the effect of one tool call. Call it right after `ToolRegistry.executeTool`. */
  record(name: string, args: Record<string, any>, result: ToolCallResult): void {
    const a = isObject(args) ? args : {};
    const output = typeof result?.output === 'string' ? result.output : '';

    // These two can come from any tool: the approval check and the privacy check run before it.
    if (output.startsWith(DECLINED_PREFIX)) return this.noteDeclined(name, a, output);
    if (result?.is_error && this.noteRefusal(name, a, output)) return;

    switch (name) {
      case 'read_file': return this.noteRead(a, result, output);
      case 'write_file': return this.noteWrite(a, result, output);
      case 'edit_file_diff': return this.noteEdit(a, result, output);
      case 'run_terminal_command': return this.noteCommand(a, result, output);
      case 'search_codebase': return this.noteSearch(a, result, output);
      case 'list_directory': return this.noteListing(a, result, output);
      case 'get_workspace_diagnostics': return this.noteDiagnostics(a, result, output);
      case 'remember': return this.noteRemember(output);
      default:
        // A tool this ledger has no section for: only its failure is worth keeping.
        if (result?.is_error) this.fail(`${name} failed: ${firstLine(output)}`);
    }
  }

  /** True when nothing has been recorded. */
  isEmpty(): boolean {
    return this.reads.size === 0 && this.changes.size === 0 && this.ran.length === 0 &&
      this.searches.size === 0 && this.listed.length === 0 && this.diagnostics.size === 0 &&
      this.remembered.length === 0 && this.declined.length === 0 && this.trouble.length === 0;
  }

  /**
   * The ledger as text for the working-memory block, in the contract's format. Empty groups are left
   * out, and an empty ledger renders as ''.
   *
   * Past `maxChars`, the oldest entries of each group go first, but every group keeps its newest
   * KEEP_NEWEST; a group that lost entries starts with "- … N more". Only if that is still too long
   * do groups shrink further (down to their newest entry), and the very last resort is a plain cut,
   * so the result never exceeds `maxChars`.
   */
  render(maxChars = LEDGER_MAX_CHARS): string {
    const groups = this.groups().filter((g) => g.items.length > 0);
    if (groups.length === 0) return '';
    const dropped = groups.map(() => 0);
    const text = () => groups.map((g, i) => formatGroup(g, dropped[i])).join('\n');

    for (const keep of [KEEP_NEWEST, 1]) {
      while (text().length > maxChars) {
        // Take the oldest entry from whichever group has the most to spare above `keep`.
        let pick = -1;
        let spareOfPick = 0;
        groups.forEach((g, i) => {
          const spare = g.items.length - dropped[i] - keep;
          if (spare > spareOfPick) { pick = i; spareOfPick = spare; }
        });
        if (pick < 0) break;
        dropped[pick]++;
      }
    }
    const out = text();
    return out.length > maxChars ? out.slice(0, Math.max(0, maxChars - 1)) + '…' : out;
  }

  /** The saved form. Plain data: `fromJSON(toJSON())` gives back an identical ledger. */
  toJSON(): LedgerJSON {
    return {
      version: 1,
      reads: [...this.reads.values()].map((e) => ({ path: e.path, ranges: e.ranges.map(([s, t]): Range => [s, t]), whole: e.whole, lines: e.lines })),
      changes: [...this.changes.values()].map((e) => ({ ...e })),
      ran: this.ran.map((e) => ({ ...e })),
      searches: [...this.searches.values()].map((e) => ({ ...e })),
      listed: [...this.listed],
      diagnostics: [...this.diagnostics.values()].map((e) => ({ ...e })),
      remembered: this.remembered.map((e) => ({ ...e })),
      declined: [...this.declined],
      trouble: this.trouble.map((e) => ({ ...e })),
    };
  }

  /**
   * Rebuilds a ledger from `toJSON()`'s output. Never throws: anything that is not exactly that shape
   * (a wrong version, a missing or mistyped field, a bad range) gives an empty ledger, because a half
   * trusted ledger would state things that did not happen. A saved file from a build with larger
   * retention caps is trimmed to the current caps, newest entries kept.
   */
  static fromJSON(json: unknown): Ledger {
    const ledger = new Ledger();
    try {
      if (!isObject(json) || json.version !== 1 ||
        !isList(json.reads, isReadEntry) || !isList(json.changes, isChangeEntry) ||
        !isList(json.ran, isRanEntry) || !isList(json.searches, isSearchEntry) ||
        !isList(json.listed, isString) || !isList(json.diagnostics, isDiagnosticEntry) ||
        !isList(json.remembered, isRememberedEntry) || !isList(json.declined, isString) ||
        !isList(json.trouble, isTroubleEntry)) {
        return ledger;
      }
      for (const e of json.reads) touch(ledger.reads, e.path, { path: e.path, ranges: e.ranges.map(([s, t]): Range => [s, t]), whole: e.whole, lines: e.lines }, MAX_READ_PATHS);
      for (const e of json.changes) touch(ledger.changes, e.path, { ...e }, MAX_OTHER);
      for (const e of json.ran) pushCapped(ledger.ran, { ...e }, MAX_COMMANDS);
      for (const e of json.searches) touch(ledger.searches, e.query, { ...e }, MAX_OTHER);
      for (const p of json.listed) ledger.noteListed(p);
      for (const e of json.diagnostics) touch(ledger.diagnostics, e.target, { ...e }, MAX_OTHER);
      for (const e of json.remembered) ledger.noteRemembered({ ...e });
      for (const s of json.declined) ledger.noteDeclinedText(s);
      for (const e of json.trouble) ledger.addTrouble(e.kind, e.text);
    } catch {
      return new Ledger();
    }
    return ledger;
  }

  // --- Recording, one method per kind of effect ---

  private noteRead(a: Record<string, any>, result: ToolCallResult, output: string): void {
    const path = pathOf(a, '(no path)');
    if (result.is_error) return this.fail(`read of ${path} failed: ${failureReason(output)}`);

    const startArg = Number(a.start_line);
    const endArg = Number(a.end_line);
    const hasStart = Number.isFinite(startArg) && startArg > 1;
    const hasEnd = Number.isFinite(endArg) && endArg >= 1;
    const seen = seenLines(output);

    const entry = this.reads.get(path) ?? { path, ranges: [], whole: false, lines: 0 };
    if (seen.ranges.length > 0) {
      for (const [s, e] of seen.ranges) entry.ranges = addRange(entry.ranges, s, e);
    } else if (hasEnd && output.trim() !== '') {
      // Output that is not the tools' numbered lines (a tool answering otherwise): trust the range the
      // model asked for rather than lose the read.
      entry.ranges = addRange(entry.ranges, hasStart ? startArg : 1, endArg);
    } else {
      return; // no numbered line came back (a range past the end of the file, or an output cut to nothing)
    }

    // A read without a range that came back whole is "the whole file". If the output was cut, the
    // middle was never shown to the model, so only the ranges it saw are recorded.
    if (!hasStart && !hasEnd && !seen.cut) {
      entry.whole = true;
      entry.lines = seen.last;
    }
    touch(this.reads, path, entry, MAX_READ_PATHS);
  }

  private noteWrite(a: Record<string, any>, result: ToolCallResult, output: string): void {
    const path = pathOf(a, '(no path)');
    if (result.is_error) return this.fail(`write to ${path} failed: ${failureReason(output)}`);
    const ok = WRITE_OK.exec(output);
    if (!ok) return;
    const entry = this.changeFor(path);
    if (ok[1] === 'created') entry.created = true;
    else entry.rewrites++;
    this.addStats(entry, result, output);
    touch(this.changes, path, entry, MAX_OTHER);
  }

  private noteEdit(a: Record<string, any>, result: ToolCallResult, output: string): void {
    const path = pathOf(a, '(no path)');
    if (result.is_error) {
      this.fail(output.startsWith('Diff Error') ? `edit to ${path} did not apply` : `edit to ${path} failed: ${failureReason(output)}`);
      return;
    }
    if (!output.startsWith(EDIT_OK)) return;
    const entry = this.changeFor(path);
    entry.edits++;
    this.addStats(entry, result, output);
    touch(this.changes, path, entry, MAX_OTHER);
  }

  private changeFor(path: string): ChangeEntry {
    return this.changes.get(path) ?? { path, created: false, rewrites: 0, edits: 0, additions: 0, deletions: 0 };
  }

  /** Adds the change's size, from the tool's own diff, or from the "(+N lines, -M lines)" in its output. */
  private addStats(entry: ChangeEntry, result: ToolCallResult, output: string): void {
    const diff = result.fileDiff;
    if (diff && isCount(diff.additions) && isCount(diff.deletions)) {
      entry.additions += diff.additions;
      entry.deletions += diff.deletions;
      return;
    }
    const m = DIFF_STATS.exec(output);
    if (m) {
      entry.additions += Number(m[1]);
      entry.deletions += Number(m[2]);
    }
  }

  private noteCommand(a: Record<string, any>, result: ToolCallResult, output: string): void {
    const command = clip(typeof a.command === 'string' ? a.command : '', COMMAND_CHARS);
    if (result.is_error) return this.fail(`command \`${command}\` failed: ${firstLine(output)}`);
    const failed = COMMAND_FAILED.exec(output);
    // After the "Command failed (exit code N):" header comes the command's own output.
    const line = failed ? firstLine(output.slice(output.indexOf('\n') + 1), COMMAND_LINE_CHARS) : '';
    pushCapped(this.ran, { command, exit: failed ? failed[1].trim() : '0', line }, MAX_COMMANDS);
  }

  private noteSearch(a: Record<string, any>, result: ToolCallResult, output: string): void {
    const query = clip(typeof a.query === 'string' ? a.query : '', QUERY_CHARS);
    if (result.is_error) return this.fail(`search for "${query}" failed: ${failureReason(output)}`);
    const hit = SEARCH_HIT.exec(output);
    if (hit) touch(this.searches, query, { query, result: plural(Number(hit[1]), 'file') }, MAX_OTHER);
    else if (output.startsWith(SEARCH_MISS)) touch(this.searches, query, { query, result: 'no matches' }, MAX_OTHER);
  }

  private noteListing(a: Record<string, any>, result: ToolCallResult, output: string): void {
    const path = pathOf(a, '.');
    if (result.is_error) return this.fail(`list of ${path} failed: ${failureReason(output)}`);
    this.noteListed(path);
  }

  private noteListed(path: string): void {
    const at = this.listed.indexOf(path);
    if (at >= 0) this.listed.splice(at, 1); // listed again: it moves to the newest place
    pushCapped(this.listed, path, MAX_OTHER);
  }

  private noteDiagnostics(a: Record<string, any>, result: ToolCallResult, output: string): void {
    const target = typeof a.file_path === 'string' && a.file_path ? a.file_path : 'workspace';
    if (result.is_error) return this.fail(`diagnostics for ${target} failed: ${failureReason(output)}`);
    const count = (tag: string) => output.split('\n').filter((l) => l.startsWith(`[${tag}] `)).length;
    touch(this.diagnostics, target, { target, errors: count('ERROR'), warnings: count('WARN'), partial: isCut(output) }, MAX_OTHER);
  }

  private noteRemember(output: string): void {
    // Exactly these two texts mean it was saved. Anything else (declined is handled before this) is a failure.
    const scoped: Array<[string, 'project' | 'user']> = [[REMEMBERED_PROJECT, 'project'], [REMEMBERED_USER, 'user']];
    for (const [prefix, scope] of scoped) {
      if (output.startsWith(prefix)) {
        this.noteRemembered({ scope, fact: clip(output.slice(prefix.length), FACT_CHARS) });
        return;
      }
    }
    // An update ("📝 Updated for …: old → new") or a removal ("📝 Forgot for …: old") of a fact the agent saved.
    const changed = /^📝 (Updated|Forgot) for (this project|you): ([\s\S]*)$/.exec(output.split('\n')[0]);
    if (changed) {
      const scope = changed[2] === 'you' ? 'user' : 'project';
      const arrow = changed[3].indexOf(' → ');
      const fact = changed[1] === 'Forgot'
        ? `forgot: ${changed[3]}`
        : arrow === -1 ? changed[3] : `${changed[3].slice(arrow + 3)} (replacing: ${changed[3].slice(0, arrow)})`;
      this.noteRemembered({ scope, fact: clip(fact, FACT_CHARS) });
      return;
    }
    // A fact that was already in the memory is neither saved now nor a failure: nothing to record.
    if (output.startsWith(ALREADY_REMEMBERED)) return;
    this.fail(`remember did not save: ${firstLine(output)}`);
  }

  private noteRemembered(entry: RememberedEntry): void {
    // The same fact twice is one entry, at its newest place.
    this.remembered = this.remembered.filter((r) => r.scope !== entry.scope || r.fact !== entry.fact);
    pushCapped(this.remembered, entry, MAX_OTHER);
  }

  /** "The user declined: <summary>. Do not retry it; ...". A command's own text is added, as the summary only says "run a command". */
  private noteDeclined(name: string, a: Record<string, any>, output: string): void {
    const end = output.indexOf(DECLINED_SUFFIX);
    const summary = end >= 0 ? output.slice(DECLINED_PREFIX.length, end) : output.slice(DECLINED_PREFIX.length);
    const detail = name === 'run_terminal_command' && typeof a.command === 'string' && a.command
      ? `: ${clip(a.command, COMMAND_CHARS)}`
      : '';
    this.noteDeclinedText(clip(summary, COMMAND_CHARS) + detail);
  }

  private noteDeclinedText(text: string): void {
    this.declined = this.declined.filter((d) => d !== text);
    pushCapped(this.declined, text, MAX_OTHER);
  }

  /** The privacy checks run before the approval; true when `output` is one of their refusals. */
  private noteRefusal(name: string, a: Record<string, any>, output: string): boolean {
    if (!['read_file', 'write_file', 'edit_file_diff'].includes(name)) return false;
    const path = pathOf(a, '(no path)');
    const verb = name === 'read_file' ? 'read of' : name === 'edit_file_diff' ? 'edit to' : 'write to';
    const excluded = EXCLUDED_NOTICE.exec(output);
    if (excluded) {
      this.addTrouble('refused', `${verb} ${path} refused: excluded for privacy (${clip(excluded[1], MESSAGE_CHARS)})`);
      return true;
    }
    if (output.startsWith(REDACTION_REFUSAL)) {
      this.addTrouble('refused', `${verb} ${path} refused: it would have written [REDACTED] over real secrets`);
      return true;
    }
    return false;
  }

  private fail(text: string): void {
    this.addTrouble('failed', text);
  }

  private addTrouble(kind: TroubleEntry['kind'], text: string): void {
    // The same trouble twice is one entry, at its newest place; each kind keeps its own newest MAX_OTHER.
    this.trouble = this.trouble.filter((t) => t.kind !== kind || t.text !== text);
    this.trouble.push({ kind, text });
    const ofKind = this.trouble.filter((t) => t.kind === kind);
    if (ofKind.length > MAX_OTHER) this.trouble.splice(this.trouble.indexOf(ofKind[0]), 1);
  }

  // --- Rendering ---

  /** Every group, in the contract's order, with its items from the oldest to the newest. */
  private groups(): Group[] {
    return [
      { title: 'Read:', inline: false, items: [...this.reads.values()].map(readLine) },
      { title: 'Changed:', inline: false, items: [...this.changes.values()].map(changeLine) },
      { title: 'Ran:', inline: false, items: this.ran.map(ranLine) },
      { title: 'Searched:', inline: false, items: [...this.searches.values()].map((s) => `"${s.query}": ${s.result}`) },
      { title: 'Listed:', inline: true, items: this.listed },
      { title: 'Diagnostics:', inline: false, items: [...this.diagnostics.values()].map(diagnosticLine) },
      { title: 'Remembered:', inline: false, items: this.remembered.map((r) => `${r.scope}: ${r.fact}`) },
      { title: 'Declined by the user:', inline: false, items: this.declined },
      { title: 'Refused or failed:', inline: false, items: this.trouble.map((t) => t.text) },
    ];
  }
}

/** Why a tool failed, for the part of an entry after the colon. */
function failureReason(output: string): string {
  if (output.startsWith('File not found')) return 'file not found';
  if (output.startsWith('Directory not found')) return 'directory not found';
  return firstLine(output);
}

/** "src/a.ts lines 1-120, 300-380", or "src/b.ts (whole file, 212 lines)". */
function readLine(e: ReadEntry): string {
  if (e.whole) return `${e.path} (whole file${e.lines > 0 ? ', ' + plural(e.lines, 'line') : ''})`;
  const parts = e.ranges.map(([s, t]) => (s === t ? `${s}` : `${s}-${t}`));
  const single = e.ranges.length === 1 && e.ranges[0][0] === e.ranges[0][1];
  return `${e.path} ${single ? 'line' : 'lines'} ${parts.join(', ')}`;
}

/** "src/a.ts: 2 edits (+14 -3)", "src/new.ts: created (+80)". */
function changeLine(e: ChangeEntry): string {
  const what: string[] = [];
  if (e.created) what.push('created');
  if (e.rewrites > 0) what.push(e.rewrites === 1 ? 'rewritten' : `rewritten ${e.rewrites} times`);
  if (e.edits > 0) what.push(plural(e.edits, 'edit'));
  const size: string[] = [];
  if (e.additions > 0 || e.deletions === 0) size.push(`+${e.additions}`);
  if (e.deletions > 0) size.push(`-${e.deletions}`);
  return `${e.path}: ${what.join(', ')} (${size.join(' ')})`;
}

/** "`npm test` (exit 1): FAIL test/x.test.js", "`git status` (exit 0)". */
function ranLine(e: RanEntry): string {
  return `\`${e.command}\` (exit ${e.exit})${e.line ? ': ' + e.line : ''}`;
}

/** "workspace: 2 errors, 1 warning"; a "+" after a count means the output was cut, so there may be more. */
function diagnosticLine(e: DiagnosticEntry): string {
  return `${e.target}: ${plural(e.errors, 'error', !e.partial)}, ${plural(e.warnings, 'warning', !e.partial)}`;
}

/** A group as text, without its `dropped` oldest items (they become one "… N more"). */
function formatGroup(group: Group, dropped: number): string {
  const items = group.items.slice(dropped);
  const more = dropped > 0 ? `… ${dropped} more` : null;
  if (group.inline) return `${group.title} ${[more, ...items].filter((s) => s !== null).join(', ')}`;
  return [group.title, ...(more ? [`- ${more}`] : []), ...items.map((item) => `- ${item}`)].join('\n');
}
