/**
 * The loop guard (extension 1.5.0): notices an agent that keeps repeating itself, so that it can be told once to change
 * approach and, if it carries on, be stopped. Until now the only brake was the turn limit (100 steps by default).
 *
 * A step is one model turn with its tool calls and their results. Within the last 12 steps three things count:
 *   - the same call (the tool and its arguments, compared as JSON with sorted keys) that got the same result (the error
 *     flag and the first 2,000 characters of the output), 3 times;
 *   - the same terminal command failing, 3 times, even when its output differs a little (a time, a duration);
 *   - the same file read again, 4 times, with no change to that file in between: the same lines, judged by the lines
 *     that came back, not the lines asked for (a long answer is cut short, so asking for more is not reading more).
 * Work is not a loop. A call that really changed a file starts the count again: for a repeated command, search or read,
 * an edit of any file between the two ("run the tests, edit, run the tests"); for a file read again, only an edit of that
 * file (an edit to another file leaves it as it was). A command whose output keeps changing (a log being tailed) is not
 * repeating either, unless it keeps failing.
 *
 * Two ways in. A run keeps a LoopGuard: its first hit is a nudge and a hit after that is a stop (until 12 steps in a row
 * have had no hit; then the next one is a nudge again). VS Code's own agent drives DLNE Swarm in the model picker and
 * sends the whole history with every request, so there checkHistory reads the history's trailing steps and answers on
 * its own, with nothing kept between requests.
 *
 * No `vscode` import, so a test loads it straight from dist/. Whoever calls it passes the redaction the run log uses,
 * which every word naming what was repeated goes through (a command can hold a secret).
 */

/** The last this many steps are looked at. */
export const LOOP_WINDOW_STEPS = 12;

const SAME_CALL_TIMES = 3;
const FAILING_COMMAND_TIMES = 3;
const REREAD_TIMES = 4;
/** In a history, a loop that has gone this many repeats past its count is stopped (it was told, in the system message, at its count). */
const STOP_AFTER_EXTRA = 2;
/** Results are compared on their first characters: a long output that differs only far down is the same result. */
const RESULT_COMPARE_CHARS = 2000;
/** A command, path or query is named in at most this many characters. */
const NAME_CHARS = 60;
/** The text handed to the redaction, before it is shortened: a secret that starts early must still be seen whole. */
const REDACT_CHARS = 2000;

/** A tool call of one step, with what came back. */
export interface GuardCall {
  name: string;
  /** The call's arguments, parsed. */
  args: unknown;
  /** The tool said it failed. The agent's own tools do; VS Code's give no such flag, so there it is always false. */
  isError: boolean;
  output: string;
}

export type LoopRule = 'same-call' | 'failing-command' | 'reread';

/** What was found, without the words for it: safe for the DLNE log, which holds names and numbers only. */
export interface LoopHit {
  rule: LoopRule;
  /** The tool the repeated call went to, or "?" when its name does not look like a tool's. */
  tool: string;
  /** How many times within the window, this one included. */
  count: number;
  /** How many it takes to count: 3, or 4 for a file read again. */
  threshold: number;
}

export type LoopVerdict =
  | { kind: 'none' }
  | { kind: 'nudge'; reason: string; hit: LoopHit }
  | { kind: 'stop'; reason: string; hit: LoopHit };

/** Hides secrets in a piece of text; what the run log uses (agentLoop.ts), or the picker's own. */
export type Redact = (text: string) => string;

// --- What the user and the model are told ---------------------------------------------------------------------------
// `reason` is a phrase that fits after "kept" ("running `npm test` and failing each time"), so the note says "you have
// been" in front of it.

/** The note for the model, before its next step. */
export function loopNote(reason: string): string {
  return `Loop check: you have been ${reason}. That is not working. Try a different approach, or stop and tell the user what is blocking you.`;
}

/** The progress line for the user when the model was told. */
export function loopProgressLine(reason: string): string {
  return `🔁 The agent was repeating itself (${reason}); DLNE told it to change approach.`;
}

/** What the chat shows when the run is stopped (it starts with a blank line, like the turn limit's notice). */
export function loopStopNotice(reason: string): string {
  return `\n\n⏹️ DLNE stopped this task: the agent kept ${reason} without getting anywhere. ` +
    'Everything so far is kept: say continue with a hint about what to try, or ask for something else.';
}

/**
 * What the model is left with when the run is stopped. The user has the notice above, which the model never sees, so
 * without this a Continue that says nothing more would start again with no word of why the last run ended.
 */
export function loopStopNote(reason: string): string {
  return `DLNE stopped the last task because you kept ${reason}; take a different approach.`;
}

/** The error DLNE Swarm gives VS Code's Chat view when a request is refused for the same reason. */
export function loopStopError(reason: string): string {
  return `DLNE stopped: the agent kept ${reason}. Change the request or try again.`;
}

const RULE_WORDS: Record<LoopRule, string> = {
  'same-call': 'the same call with the same result',
  'failing-command': 'the same command failing',
  'reread': 'the same file read again',
};

/** One line for the DLNE log: the tool's name and the numbers, never a command, a path or a result. */
export function loopSummary(hit: LoopHit): string {
  return `${hit.tool}, ${RULE_WORDS[hit.rule]}, ${hit.count} times in the last ${LOOP_WINDOW_STEPS} steps`;
}

// --- Reading a call ------------------------------------------------------------------------------------------------

type Kind = 'terminal' | 'read' | 'edit' | 'other';

const TERMINAL_TOOLS = new Set(['run_terminal_command', 'run_in_terminal']);
const READ_TOOLS = new Set(['read_file']);
/**
 * The tools that change files: the agent's own, and the ones VS Code's agent offers (read from the Copilot extension
 * built into VS Code 1.143). `edit_file` is not either's name today; it costs nothing to know it.
 */
const EDIT_TOOLS = new Set([
  'write_file', 'edit_file_diff', 'edit_file', 'generate_image',
  'create_file', 'replace_string_in_file', 'multi_replace_string_in_file', 'insert_edit_into_file', 'apply_patch',
  'create_directory', 'edit_notebook_file',
]);
/** A tool that is not in the list but is named like one that changes files. A wrong guess only makes the guard slower to speak. */
const EDIT_NAME = /(?:^|[_\-.])(?:write|edit|replace|insert|patch|create|delete|remove|rename|move)(?:[_\-.]|$)/i;
/** What a failed command's output starts with (tools.ts: the tool returns no error flag for a command that exited non-zero). */
const COMMAND_FAILED = /^Command failed \(exit code [^)]*\):/;
/**
 * Arguments that only say why: VS Code's tools carry an "explanation" the model words afresh each time, which would
 * make a call it repeats look new.
 */
const WHY_ARGUMENTS = new Set(['explanation', 'description', 'goal', 'reason']);

function kindOf(name: string): Kind {
  if (TERMINAL_TOOLS.has(name)) return 'terminal';
  if (READ_TOOLS.has(name)) return 'read';
  if (EDIT_TOOLS.has(name) || EDIT_NAME.test(name)) return 'edit';
  return 'other';
}

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

/** `text` on one line, trimmed. */
function flat(text: string): string {
  return text.replace(/\s+/g, ' ').trim();
}

/** JSON with sorted keys and no spaces, so two calls with the same arguments are the same text whatever their layout. */
function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (isObject(value)) {
    return `{${Object.keys(value).sort().filter((key) => value[key] !== undefined)
      .map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value) ?? 'null';
}

/** A path as one spelling: slashes forward, no "./" in front, no doubled or trailing slash. */
function normalPath(text: string): string {
  const path = text.trim().replace(/\\/g, '/').replace(/\/{2,}/g, '/').replace(/^(?:\.\/)+/, '');
  return path.length > 1 ? path.replace(/\/$/, '') : path;
}

function stringAt(args: Record<string, unknown>, keys: string[]): string | null {
  for (const key of keys) {
    const value = args[key];
    if (typeof value === 'string' && value.trim()) return value;
  }
  return null;
}

/** The file a call is about: the agent's `path`, VS Code's `filePath`. */
function pathOf(args: Record<string, unknown>): string | null {
  const path = stringAt(args, ['path', 'filePath', 'file_path', 'file', 'filename']);
  return path === null ? null : normalPath(path);
}

/** The files an edit changed, or null when the call does not say (then it counts as changing any file). */
function pathsChanged(args: Record<string, unknown>): string[] | null {
  const found: string[] = [];
  const one = pathOf(args);
  if (one !== null) found.push(one);
  for (const key of ['filePaths', 'paths']) {
    const list = args[key];
    if (Array.isArray(list)) found.push(...list.filter((p): p is string => typeof p === 'string' && p.trim() !== '').map(normalPath));
  }
  if (Array.isArray(args.replacements)) {
    for (const part of args.replacements) {
      const path = isObject(part) ? pathOf(part) : null;
      if (path !== null) found.push(path);
    }
  }
  // apply_patch carries its files in the patch text: "*** Update File: src/a.ts".
  if (typeof args.input === 'string') {
    for (const m of args.input.matchAll(/^\*\*\* (?:Add|Update|Delete) File: (.+)$/gm)) found.push(normalPath(m[1]));
  }
  return found.length > 0 ? found : null;
}

/** One call, read for the three rules. */
interface Event {
  step: number;
  kind: Kind;
  name: string;
  /** What makes two calls the same call: the tool and its arguments (a command: its words alone). */
  key: string;
  /** What makes two results the same result. */
  result: string;
  /** A command that failed; for any other call, the tool said it failed. */
  failed: boolean;
  command: string;
  /** The file a read or an edit is about. */
  path: string | null;
  /** The files an edit changed; null when unknown. */
  paths: string[] | null;
  /** The lines a read got back (see linesReturned), or, when its output does not say, the lines it asked for. */
  from: number;
  to: number;
  /** What names the call in words: the command, the path or the query. */
  subject: string;
}

/**
 * Where capOutput (tools.ts) cut a long output short. After it come the file's last lines, which capOutput adds to every
 * long output. The blank line in front matters: every line of a numbered output starts with its number, so none is
 * blank, and a file that holds these words (tools.ts does) is not taken for a cut. The note about hidden secrets
 * (redactionNote) does start with a blank line, but it comes last and starts with "[DLNE hid", so it is never a line.
 */
const OUTPUT_CUT = '\n\n... [Output truncated: ';
/** A line as the agent's read_file numbers it: "12: text". */
const NUMBERED_LINE = /^(\d+): /gm;

/**
 * The lines a read really got back, from the numbers the agent's read_file puts in front of each line. A read that
 * asks for a start and no end gets the rest of the file, and a long answer is cut to its first few thousand characters,
 * so the lines asked for say little about the lines seen: a model paging forward (1, 81, 161, ... with no end) asks
 * for ranges that all overlap and sees ones that do not. Only the lines before the cut count; the file's tail after it
 * is the tool's doing, added to every long answer, not the model's. Null when the output has no numbered
 * lines (VS Code's own read_file, in the model picker, does not number them), and then the lines asked for are used.
 */
function linesReturned(output: string): { from: number; to: number } | null {
  const cut = output.indexOf(OUTPUT_CUT);
  const seen = cut >= 0 ? output.slice(0, cut) : output;
  let from = Number.POSITIVE_INFINITY;
  let to = 0;
  for (const m of seen.matchAll(NUMBERED_LINE)) {
    const line = Number(m[1]);
    if (line < 1) continue;
    from = Math.min(from, line);
    to = Math.max(to, line);
  }
  return to >= 1 ? { from, to } : null;
}

function describe(call: GuardCall, step: number): Event {
  const args = isObject(call.args) ? call.args : {};
  const output = typeof call.output === 'string' ? call.output : '';
  const kind = kindOf(call.name);
  const command = kind === 'terminal' ? flat(typeof args.command === 'string' ? args.command : '') : '';
  const own: Record<string, unknown> = {};
  for (const key of Object.keys(args)) if (!WHY_ARGUMENTS.has(key)) own[key] = args[key];
  const start = Number(args.start_line ?? args.startLine);
  const end = Number(args.end_line ?? args.endLine);
  const askedFrom = Number.isFinite(start) && start >= 1 ? Math.floor(start) : 1;
  const askedTo = Number.isFinite(end) && end >= 1 ? Math.max(askedFrom, Math.floor(end)) : Number.POSITIVE_INFINITY;
  const { from, to } = (kind === 'read' ? linesReturned(output) : null) ?? { from: askedFrom, to: askedTo };
  const path = kind === 'read' || kind === 'edit' || /^list_dir/.test(call.name) ? pathOf(args) : null;
  const query = stringAt(args, ['query', 'pattern']);
  return {
    step,
    kind,
    name: call.name,
    key: kind === 'terminal' ? `terminal\n${command}` : `${call.name}\n${canonical(own)}`,
    result: `${call.isError ? 'error' : 'ok'}\n${output.slice(0, RESULT_COMPARE_CHARS)}`,
    failed: call.isError === true || (kind === 'terminal' && COMMAND_FAILED.test(output)),
    command,
    path,
    paths: kind === 'edit' ? pathsChanged(args) : null,
    from,
    to,
    subject: kind === 'terminal' ? command : path ?? query ?? '',
  };
}

/** Which edits start a count again: any file's (what a command, a search or a repeated call returns can depend on any file), or only the file the call is about. */
type Scope = 'any' | 'file';

/**
 * Whether `earlier` changed what `e` returns, so that `e` is not a repeat of anything before `earlier`. Only an edit that
 * went through counts (a failed one changed nothing), and not another go at the very call being counted.
 */
function breaks(earlier: Event, e: Event, scope: Scope): boolean {
  if (earlier.kind !== 'edit' || earlier.failed || earlier.key === e.key) return false;
  if (scope === 'any') return true;
  if (earlier.paths === null || e.path === null) return true; // it does not say which file it changed: this one, for all we know
  return earlier.paths.includes(e.path);
}

/** How many calls, `events[at]` included, match going back from it, up to a change that starts the count again. */
function chain(events: Event[], at: number, scope: Scope, matches: (earlier: Event) => boolean): number {
  const e = events[at];
  let count = 1;
  for (let i = at - 1; i >= 0; i--) {
    if (breaks(events[i], e, scope)) break;
    if (matches(events[i])) count++;
  }
  return count;
}

const overlap = (a: Event, b: Event): boolean => a.from <= b.to && b.from <= a.to;

interface Found {
  rule: LoopRule;
  count: number;
  threshold: number;
  event: Event;
}

/** Earlier in this list wins when two rules are equally far past their counts: it says more. */
const RULE_ORDER: LoopRule[] = ['failing-command', 'reread', 'same-call'];

function deeper(a: Found, b: Found | null): boolean {
  if (b === null) return true;
  const gap = (f: Found) => f.count - f.threshold;
  return gap(a) !== gap(b) ? gap(a) > gap(b) : RULE_ORDER.indexOf(a.rule) < RULE_ORDER.indexOf(b.rule);
}

/** The loop the newest step is part of, if any: the deepest of what its calls repeat. */
function findLoop(steps: ReadonlyArray<ReadonlyArray<GuardCall>>): Found | null {
  const window = steps.slice(-LOOP_WINDOW_STEPS);
  const events: Event[] = [];
  window.forEach((step, n) => step.forEach((call) => events.push(describe(call, n))));
  const newest = window.length - 1;
  let best: Found | null = null;
  events.forEach((e, at) => {
    if (e.step !== newest) return;
    const found: Found[] = [];
    // The same call: an edit anywhere starts the count again, except for a call that is itself an edit, which only an
    // edit of the same file does (a failed edit tried again and again, with other files being edited in between).
    const same = chain(events, at, e.kind === 'edit' ? 'file' : 'any', (o) => o.key === e.key && o.result === e.result);
    if (same >= SAME_CALL_TIMES) found.push({ rule: 'same-call', count: same, threshold: SAME_CALL_TIMES, event: e });
    if (e.kind === 'terminal' && e.failed) {
      const failing = chain(events, at, 'any', (o) => o.kind === 'terminal' && o.failed && o.command === e.command);
      if (failing >= FAILING_COMMAND_TIMES) found.push({ rule: 'failing-command', count: failing, threshold: FAILING_COMMAND_TIMES, event: e });
    }
    if (e.kind === 'read' && !e.failed && e.path !== null) {
      // "No change to that path in between": only an edit of this file starts the count again. And reading other parts
      // of a long file is how the tool's own advice says to read it, so only lines read before count: the lines that
      // came back, which for a long file are fewer than the lines asked for.
      const again = chain(events, at, 'file', (o) => o.kind === 'read' && !o.failed && o.path === e.path && overlap(o, e));
      if (again >= REREAD_TIMES) found.push({ rule: 'reread', count: again, threshold: REREAD_TIMES, event: e });
    }
    for (const f of found) if (deeper(f, best)) best = f;
  });
  return best;
}

// --- Saying it -----------------------------------------------------------------------------------------------------

/** A tool's name as the log may hold it: the model chose it, so only a name of a tool's shape is written. */
function safeName(name: string): string {
  return /^[A-Za-z_][\w-]{0,39}$/.test(name) ? name : '?';
}

function hitOf(found: Found): LoopHit {
  return { rule: found.rule, tool: safeName(found.event.name), count: found.count, threshold: found.threshold };
}

/** The subject in backticks: redacted first, then on one line, and shortened. */
function quoted(subject: string, redact: Redact, fallback: string): string {
  if (!subject) return fallback;
  const clean = flat(redact(subject.slice(0, REDACT_CHARS))).replace(/`/g, "'");
  if (!clean) return fallback;
  return `\`${clean.length > NAME_CHARS ? `${clean.slice(0, NAME_CHARS - 1)}…` : clean}\``;
}

/** What was repeated, as a phrase after "kept": "running `npm test` and failing each time". */
function phrase(found: Found, redact: Redact): string {
  const e = found.event;
  if (found.rule === 'failing-command') return `running ${quoted(e.subject, redact, 'a command')} and failing each time`;
  if (found.rule === 'reread') return `reading ${quoted(e.subject, redact, 'a file')} again and again`;
  switch (e.kind) {
    case 'terminal':
      return `running ${quoted(e.subject, redact, 'a command')} and getting the same output each time`;
    case 'read':
      return `reading ${quoted(e.subject, redact, 'a file')} again and again`;
    case 'edit': {
      const verb = /image/i.test(e.name) ? 'generating' : /create|write/i.test(e.name) ? 'writing' : 'editing';
      return `${verb} ${quoted(e.subject, redact, 'a file')} the same way and getting the same result each time`;
    }
    default:
      if (/^list_dir/.test(e.name)) return `listing ${quoted(e.subject, redact, 'a folder')} again and again`;
      if (/search/.test(e.name) && e.subject) return `searching for ${quoted(e.subject, redact, 'the same text')} and getting the same result each time`;
      return `calling ${safeName(e.name)} with the same arguments and getting the same result each time`;
  }
}

// --- The two ways in -----------------------------------------------------------------------------------------------

/**
 * One run's guard. record() each step after its tool results are in; the first hit is a nudge, and any hit after it is
 * a stop. A hit is a step that is itself part of a repeat, so after a nudge a step that does something else is not one,
 * even though the repeats before it are still within the window. Once a whole window of steps has gone by with no hit,
 * the loop that was nudged is behind the run: a task of 100 steps can loop twice, and the second loop is told first too.
 */
export class LoopGuard {
  private readonly steps: GuardCall[][] = [];
  private nudged = false;
  /** Steps in a row since the nudge that were not a hit. */
  private clean = 0;

  constructor(private readonly redact: Redact) {}

  record(step: ReadonlyArray<GuardCall>): LoopVerdict {
    if (step.length === 0) return { kind: 'none' };
    this.steps.push([...step]);
    if (this.steps.length > LOOP_WINDOW_STEPS) this.steps.shift();
    const found = findLoop(this.steps);
    if (found === null) {
      if (this.nudged && ++this.clean >= LOOP_WINDOW_STEPS) {
        this.nudged = false;
        this.clean = 0;
      }
      return { kind: 'none' };
    }
    this.clean = 0;
    const reason = phrase(found, this.redact);
    if (this.nudged) return { kind: 'stop', reason, hit: hitOf(found) };
    this.nudged = true;
    return { kind: 'nudge', reason, hit: hitOf(found) };
  }
}

/** The part of a chat message the history check reads (a ChatMessage fits; this file does not import the client). */
export interface HistoryMessage {
  role: string;
  content?: string | null;
  tool_call_id?: string;
  tool_calls?: ReadonlyArray<{ id?: string; function?: { name?: string; arguments?: string } }>;
}

function parseArguments(text: string | undefined): unknown {
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    return { arguments: text.replace(/\s+/g, '') }; // text that is not JSON: compared as it is, without its spaces
  }
}

/**
 * The steps at the end of a history: assistant turns that called tools, each with the results that came back, in
 * order, at most a window's worth. Empty when the history does not end with tool results: a message from the user
 * after them is the user speaking, and what came before is no longer what the agent is doing now.
 */
export function trailingSteps(messages: ReadonlyArray<HistoryMessage>): GuardCall[][] {
  const steps: GuardCall[][] = [];
  let i = messages.length - 1;
  while (i >= 0 && messages[i].role === 'tool' && steps.length < LOOP_WINDOW_STEPS) {
    const results = new Map<string, string>();
    while (i >= 0 && messages[i].role === 'tool') {
      const id = messages[i].tool_call_id;
      if (typeof id === 'string') results.set(id, typeof messages[i].content === 'string' ? (messages[i].content as string) : '');
      i--;
    }
    const turn = messages[i];
    if (!turn || turn.role !== 'assistant' || !turn.tool_calls || turn.tool_calls.length === 0) break;
    i--;
    const step: GuardCall[] = [];
    for (const call of turn.tool_calls) {
      const output = typeof call.id === 'string' ? results.get(call.id) : undefined;
      if (output === undefined) continue; // a call nothing answered was not run
      step.push({ name: call.function?.name ?? '', args: parseArguments(call.function?.arguments), isError: false, output });
    }
    if (step.length > 0) steps.unshift(step);
  }
  return steps;
}

/**
 * The steps right before the user's last words, for a history that ends with the user speaking (trailingSteps is for
 * one that ends with tool results). Several messages in a row can be the user's, and all are skipped. Empty when the
 * user did not speak last.
 */
export function stepsBeforeUser(messages: ReadonlyArray<HistoryMessage>): GuardCall[][] {
  let end = messages.length;
  while (end > 0 && messages[end - 1].role === 'user') end--;
  return end === messages.length ? [] : trailingSteps(messages.slice(0, end));
}

export type HistoryVerdict =
  | { level: 'none' }
  | { level: 'nudge' | 'stop'; reason: string; hit: LoopHit }
  /** The user has written since a loop that had gone as far as a stop: the request with their words is told why, this once. */
  | { level: 'stopped'; reason: string; hit: LoopHit };

/**
 * For a request that carries its whole history (DLNE Swarm in the picker): the newest step's repeat, if it is one.
 * A nudge at the rule's count, a stop two repeats past it. Nothing is remembered between requests; the history is
 * all there is, and a step that does something else ends the verdict.
 *
 * When the user spoke last there is no step to judge, but what came right before their words may have been a loop that
 * the stop above refused: VS Code kept nothing of the refusal, so without a word the model would not know why. That is
 * `stopped`. It is only for a loop that had gone that far (a nudged one was never stopped), only for the steps right
 * before the user's words (a clean step after the loop, or an earlier message of theirs, ends it), and so only for the
 * one request that carries those words: the next one ends with the model's own new step.
 */
export function checkHistory(messages: ReadonlyArray<HistoryMessage>, redact: Redact): HistoryVerdict {
  const steps = trailingSteps(messages);
  if (steps.length > 0) {
    const running = findLoop(steps);
    if (running === null) return { level: 'none' };
    const level = running.count - running.threshold >= STOP_AFTER_EXTRA ? 'stop' : 'nudge';
    return { level, reason: phrase(running, redact), hit: hitOf(running) };
  }
  const before = stepsBeforeUser(messages);
  const stopped = before.length > 0 ? findLoop(before) : null;
  if (stopped === null || stopped.count - stopped.threshold < STOP_AFTER_EXTRA) return { level: 'none' };
  return { level: 'stopped', reason: phrase(stopped, redact), hit: hitOf(stopped) };
}
