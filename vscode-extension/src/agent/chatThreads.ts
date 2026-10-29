import { createHash, randomBytes } from 'crypto';
import { DlneClient } from '../client/dlneClient';
import { newSessionId } from '../client/session';
import {
  AgentCallbacks, AgentLoop, AgentRunOutcome, AgentStateJSON, RunOptions, SAVED_USER_WORDS, WORKING_MEMORY_OPEN, logError, workspaceContextBlock,
} from './agentLoop';
import {
  AgentStateStore, HousekeepingReport, MAX_SAVED_THREADS, MaybeStateFolders, StatefulLoop, agentStateFile, agentThreadFile,
  agentThreadsDir, deleteAgentState, isThreadId, listAgentThreads, loadAgentState, newThreadId, openAgentThreadStore, pruneAgentThreads,
  stateFolders,
} from './agentState';
import { redactSecrets } from '../privacy';
import { logLine } from '../log';

/**
 * One conversation per Chat thread in VS Code's Chat view (@dlne). Before 1.5.0 the participant held one AgentLoop for
 * the whole window: a second thread cleared the first one's memory and stopped its run, and Enter during a run could start
 * a second run on the same conversation. Here every thread has its own AgentLoop and its own saved task, found again
 * from the thread id that every answer leaves in its result metadata (VS Code hands the metadata of a participant's
 * own answers back in `chatContext.history`), and each thread runs one request at a time.
 *
 * The Chat transcript can change under a thread: the user goes back (Restore Checkpoint, or editing an earlier request,
 * which drops everything after it), or forks the chat (the copy has the same thread id in its answers). Each finished run
 * therefore leaves a mark (a random id, saved with the conversation) and the answer carries it, with its turn: the
 * request's place among the answers of the Chat thread. A request whose transcript shows fewer answers than the last run of
 * the thread had is cut back to the mark its newest answer names, in a copy under a new thread id (the original may be the
 * other branch of a fork), or, when that state cannot be found, starts a new conversation: the model is never sent what
 * the user took out of the chat.
 */

/** What of VS Code's CancellationToken a run needs (the tests' fakes need no more than this). */
export interface CancelSignal {
  isCancellationRequested: boolean;
  onCancellationRequested(listener: () => void): { dispose(): void };
}

export interface ThreadRunOptions extends RunOptions {
  /** This request's own Stop: it ends the run, or the wait for the run in front of it. */
  token?: CancelSignal;
  /** True once VS Code has asked this request to yield (`chatContext.yieldRequested`, read live: the user sent a message during the run). */
  yieldRequested?: () => boolean;
  /** Told to the user while this request waits for the run in front of it. */
  onWait?: (text: string) => void;
  /**
   * What the Chat transcript shows (readTranscript). With it the request is checked against the thread's conversation on its
   * turn, and the run leaves a mark that its answer can name. Without it (callers that have no transcript) neither happens.
   */
  transcript?: Transcript;
}

/** What a request handler passes to ChatThreads.run: the run's options, and two ways to hear what the registry decided. */
export interface ChatRunOptions extends Omit<ThreadRunOptions, 'transcript'> {
  /** Told to the user before the run when it starts a new conversation because the transcript could not be matched. */
  onNote?: (text: string) => void;
  /**
   * Told which thread the request runs on, before it runs: the thread the transcript named, or the copy made for it. A
   * handler that gets an exception back uses this to still name the right thread (and mark) in its answer.
   */
  onThread?: (thread: Thread) => void;
}

export interface ChatThreadsOptions {
  /** The DLNE log unless a test gives its own. */
  log?: (line: string) => void;
  /**
   * How long a message waits for the run in front of it to finish its step, before that run is stopped outright, and then
   * how long it waits for that stop, before it goes ahead (the loop keeps a run that never ends out of the conversation).
   * Tests shorten both.
   */
  yieldWaitMs?: number;
  stopWaitMs?: number;
}

/** `listener` called when `token` is cancelled; a token without the event (a partial fake) is never cancelled. */
function whenCancelled(token: CancelSignal | undefined, listener: () => void): { dispose(): void } | undefined {
  return typeof token?.onCancellationRequested === 'function' ? token.onCancellationRequested(listener) : undefined;
}

/**
 * The thread id an earlier answer of this participant left in `history`. The newest one counts; an answer that VS Code
 * stored without its result (Stop pressed late, an error) has none, so older answers are looked at too.
 */
export function threadIdFromHistory(history: readonly unknown[] | undefined): string | undefined {
  for (let i = (history?.length ?? 0) - 1; i >= 0; i--) {
    const id = (history![i] as { result?: { metadata?: { threadId?: unknown } } } | undefined)?.result?.metadata?.threadId;
    if (isThreadId(id)) return id;
  }
  return undefined;
}

/** What the Chat transcript of a thread shows of it, read from `chatContext.history`. */
export interface Transcript {
  /**
   * How many answers of the chat could have been a turn of the conversation (an answer of another participant counts, one VS Code
   * stored without its result counts, a /status does not): the request being handled is the next one.
   */
  answered: number;
  /** The mark that the newest answer naming one carries (an answer VS Code stored without its result names none). */
  markId?: string;
}

const MARK_ID = /^[0-9a-f]{16}$/;

/**
 * The slash commands the participant answers itself, without the agent (nativeChatParticipant.ts). Their answers are not turns of
 * the conversation: counting them would let a /status typed after going back make up for the answers that were taken out.
 */
const COMMANDS_WITHOUT_AGENT = new Set(['status', 'contribute', 'model', 'clear', 'reset']);

/** A response turn of `history` (ChatResponseTurn) has `response` and `result`; a request turn has a `prompt` instead. */
function isAnswer(turn: unknown): boolean {
  return !!turn && typeof turn === 'object' && ('result' in turn || Array.isArray((turn as { response?: unknown }).response));
}

export function readTranscript(history: readonly unknown[] | undefined): Transcript {
  let answered = 0;
  let markId: string | undefined;
  for (const turn of history ?? []) {
    if (!isAnswer(turn)) continue;
    const command = (turn as { command?: unknown }).command;
    if (typeof command === 'string' && COMMANDS_WITHOUT_AGENT.has(command)) continue;
    answered++;
    const mark = (turn as { result?: { metadata?: { mark?: unknown } } }).result?.metadata?.mark;
    if (typeof mark === 'string' && MARK_ID.test(mark)) markId = mark;
  }
  return { answered, markId };
}

/** How much of a first message is compared when a thread is matched to a saved task by it. */
const MATCH_CHARS = 2000;

/**
 * The first message of the thread that reached the agent (not a slash command), as the agent stores the user's words.
 * A request turn of the history has `prompt` (and `command`); a response turn has `response` and `result` instead.
 */
function firstAgentPrompt(history: readonly unknown[]): string | undefined {
  for (const turn of history as Array<{ prompt?: unknown; command?: unknown } | undefined>) {
    if (!turn || typeof turn.prompt !== 'string') continue;
    if (turn.command && turn.command !== 'code') continue; // /status, /clear ... never reached the agent; /fix is rewritten before it
    const words = turn.prompt.trim();
    if (words) return redactSecrets(words).text.slice(0, MATCH_CHARS);
  }
  return undefined;
}

/** The first message of a saved task: the first of the user's words, which compaction never drops. */
function firstWordsOf(state: { userWords?: unknown } | null): string | undefined {
  const words = Array.isArray(state?.userWords) ? state!.userWords[0] : undefined;
  return typeof words === 'string' ? words.slice(0, MATCH_CHARS) : undefined;
}

// ---- marks: where a conversation stood after each run --------------------------------------------------------

/** How many marks a thread keeps (going back further than this starts a new conversation). */
const MAX_MARKS = 30;

/** The conversation as it stood when one run ended: enough to cut it back to that point. */
export interface ThreadMark {
  /** Random, 16 hex digits: the answer of that run names it, so a transcript can be matched to exactly this state. */
  id: string;
  /** The request's place among the answers of its Chat thread (1 for the first), counted from the transcript. */
  turn: number;
  /** How many messages the conversation held (exportState().messages) and how many of the user's words. */
  messages: number;
  userWords: number;
  /** The model's notes then. */
  notes: string;
  /** A digest of the working-memory block then, or '': once it changes (a compaction) older messages are gone, and this mark cannot be cut back to. */
  frozen: string;
  /** A digest of the newest message then, or '': a cut is only made where the conversation still has that same message. */
  last: string;
}

/** What is saved with a thread's conversation (the `thread` field of its file). */
export interface ThreadStateJSON {
  /** The turn of the last run: a transcript with fewer answers than this has been gone back in. */
  turn: number;
  marks: ThreadMark[];
}

function isMark(value: unknown): value is ThreadMark {
  const m = value as Partial<ThreadMark> | null;
  return !!m && typeof m === 'object' && typeof m.id === 'string' && MARK_ID.test(m.id)
    && Number.isInteger(m.turn) && m.turn! >= 0 && Number.isInteger(m.messages) && m.messages! >= 0
    && Number.isInteger(m.userWords) && m.userWords! >= 0 && typeof m.notes === 'string'
    && typeof m.frozen === 'string' && m.frozen.length <= 40 && typeof m.last === 'string' && m.last.length <= 40;
}

/** A short digest, to tell two states of a conversation apart (not for security). */
function digestOf(text: string): string {
  return createHash('sha1').update(text).digest('hex').slice(0, 12);
}

/** A digest of the working-memory block that opens `state.messages` after a compaction, or '' when there is none. */
function frozenDigest(state: AgentStateJSON): string {
  const first = state.messages[0];
  return first && first.role === 'user' && typeof first.content === 'string' && first.content.startsWith(WORKING_MEMORY_OPEN)
    ? digestOf(first.content)
    : '';
}

/** A digest of one message (its role, text and tool calls), or '' for no message. */
function messageDigest(message: unknown): string {
  return message === undefined ? '' : digestOf(JSON.stringify(message));
}

/**
 * An AgentLoop's saved task together with the thread's marks: what the thread's store reads and writes. The marks go in the
 * file's `thread` field, which a loop ignores, so the file stays a saved task as the loop knows it.
 */
class ThreadTrack implements StatefulLoop {
  /** The turn of the last run; undefined for a conversation with no marks yet (one saved before they existed, or new). */
  turn: number | undefined;
  marks: ThreadMark[] = [];

  constructor(readonly loop: AgentLoop) {}

  exportState(): AgentStateJSON & { thread?: ThreadStateJSON } {
    const state = this.loop.exportState();
    return this.turn === undefined ? state : { ...state, thread: { turn: this.turn, marks: this.marks } };
  }

  importState(json: unknown): boolean {
    if (!this.loop.importState(json)) return false;
    const saved = (json as { thread?: { turn?: unknown; marks?: unknown } } | null)?.thread;
    const turn = saved && Number.isInteger(saved.turn) && (saved.turn as number) >= 0 ? (saved.turn as number) : undefined;
    this.turn = turn;
    this.marks = turn !== undefined && Array.isArray(saved?.marks) ? saved!.marks.filter(isMark).slice(-MAX_MARKS) : [];
    return true;
  }

  /** The conversation was cleared: nothing to cut back to, and no turn to compare a transcript with. */
  reset(): void {
    this.turn = undefined;
    this.marks = [];
  }

  /** A mark of the conversation as it stands, the run of `turn` having just ended. */
  record(turn: number): ThreadMark {
    const state = this.loop.exportState();
    const frozen = frozenDigest(state);
    const mark: ThreadMark = {
      id: randomBytes(8).toString('hex'),
      turn,
      messages: state.messages.length,
      userWords: state.userWords.length,
      notes: state.notes,
      frozen,
      last: messageDigest(state.messages[state.messages.length - 1]),
    };
    // A compaction replaced the older steps: marks from before it cannot be cut back to, so they are not kept.
    this.marks = [...this.marks.filter((m) => m.frozen === frozen), mark].slice(-MAX_MARKS);
    this.turn = turn;
    return mark;
  }

  /**
   * Whether the conversation still begins with what `mark` was taken of: no compaction since (the working-memory block is the
   * same), the same message where the mark ends, and the user's words never shortened (a loaded task keeps only the first and
   * the newest 199 of more than SAVED_USER_WORDS, which moves every count).
   */
  canCutBackTo(mark: ThreadMark): boolean {
    const state = this.loop.exportState();
    return frozenDigest(state) === mark.frozen
      && mark.messages <= state.messages.length
      && messageDigest(state.messages[mark.messages - 1]) === mark.last
      && mark.userWords <= state.userWords.length
      && state.userWords.length < SAVED_USER_WORDS;
  }

  /**
   * The saved task as it stood at `mark`, for a copy of this thread: the messages and the user's words cut back, the notes of
   * then, the marks up to it, a conversation id of its own. The ledger stays: it records what was read, changed and run, and
   * that happened (VS Code's Restore Checkpoint does not undo the agent's writes).
   */
  cutBackTo(mark: ThreadMark): (AgentStateJSON & { thread: ThreadStateJSON }) | null {
    if (!this.canCutBackTo(mark)) return null;
    const state = this.loop.exportState();
    const upTo = this.marks.indexOf(mark) + 1;
    return {
      ...state,
      savedAt: new Date().toISOString(),
      sessionId: newSessionId(),
      messages: state.messages.slice(0, mark.messages),
      userWords: state.userWords.slice(0, mark.userWords),
      notes: mark.notes,
      thread: { turn: mark.turn, marks: upTo > 0 ? this.marks.slice(0, upTo) : [mark] },
    };
  }
}

/** Why a transcript does not fit a thread, and what to do about it. */
type Divergence = { kind: 'cut back'; mark: ThreadMark } | { kind: 'unmatched'; why: string };

/** Thrown by Thread.run, on the request's turn, when its transcript does not fit the thread (ChatThreads.run handles it). */
class ThreadDiverged extends Error {
  constructor(readonly divergence: Divergence) {
    super('the Chat transcript does not fit this thread');
  }
}

/** What Thread.run gives back. */
export interface ThreadRun {
  outcome: AgentRunOutcome;
  /** The conversation as this run left it, when it ran and was given a transcript; the answer names it. */
  mark?: ThreadMark;
}

/** What a thread needs from the registry that holds it. */
interface ThreadHost {
  log(line: string): void;
  /** Housekeeping could not do its job (a file that cannot be deleted): written down, nothing else. */
  report: HousekeepingReport;
  /** A run on a thread ended and its task was saved. */
  ended(): void;
  yieldWaitMs: number;
  stopWaitMs: number;
}

/**
 * One Chat thread: its conversation (an AgentLoop), its saved task, and a lane that lets one request at a time use them.
 */
export class Thread {
  lastUsed = Date.now();
  /** The mark of the last run that ended here with a transcript (see ThreadRunOptions.transcript). */
  lastMark: ThreadMark | undefined;
  /** The requests that use this thread right now: the one running and those waiting behind it. */
  private pending = 0;
  /** Settles when the last request to arrive has had its turn. */
  private tail: Promise<void> = Promise.resolve();
  private readonly short: string;

  constructor(
    readonly id: string,
    readonly loop: AgentLoop,
    readonly track: ThreadTrack,
    readonly store: AgentStateStore,
    private readonly host: ThreadHost
  ) {
    this.short = id.slice(0, 8);
  }

  /** Whether a request is running on this thread or waiting for its turn. */
  get busy(): boolean {
    return this.pending > 0;
  }

  /** /clear: the conversation is forgotten, and so is its saved copy. */
  clear(): void {
    this.loop.clearHistory();
    this.track.reset();
    this.lastMark = undefined;
    this.store.clear();
  }

  /**
   * Whether a transcript with `view.answered` answers fits this thread's conversation, on this request's turn. It does when
   * it shows at least as many answers as the last run of the thread had. With fewer, the user went back (or this is the other
   * branch of a fork): the conversation is cut back to the mark of the newest answer the transcript still names, if that mark
   * is one this thread has, lies within what the transcript shows, and can still be cut back to; anything else cannot be
   * matched (a request taken out of the middle, a state long gone, a compaction since).
   */
  private divergence(view: Transcript): Divergence | null {
    const last = this.track.turn;
    if (last === undefined || view.answered >= last) return null;
    const named = view.markId === undefined ? undefined : this.track.marks.find((m) => m.id === view.markId);
    if (!named) {
      return { kind: 'unmatched', why: view.markId === undefined ? 'no answer left in the chat names a state of this thread' : 'the newest answer left in the chat names a state this thread no longer has' };
    }
    if (named.turn > view.answered) return { kind: 'unmatched', why: 'a request was taken out of the middle of the chat' };
    let target: ThreadMark | undefined; // the newest mark within what the transcript shows (answers VS Code kept without their result may follow `named`)
    for (const mark of this.track.marks) if (mark.turn <= view.answered) target = mark;
    if (!target || !this.track.canCutBackTo(target)) return { kind: 'unmatched', why: 'the conversation has been compacted since' };
    return { kind: 'cut back', mark: target };
  }

  /** The conversation cut back to a mark, as the saved task of a copy; null when that cannot be done. */
  cutBackTo(mark: ThreadMark): ReturnType<ThreadTrack['cutBackTo']> {
    return this.track.cutBackTo(mark);
  }

  /**
   * Runs a request on this thread's conversation. A request that arrives while another is running or waiting takes its
   * turn after it: the one running is told to stop after its current step (a message waiting is how VS Code's "steer"
   * looks from here), and if that step takes too long it is stopped outright. This request's own Stop ends the run, or
   * its wait. The task is saved when the request ends, however it ended. Housekeeping at the end never fails the request.
   */
  async run(prompt: string, callbacks: AgentCallbacks, options: ThreadRunOptions = {}): Promise<ThreadRun> {
    const { token, yieldRequested, onWait, transcript, ...loopOptions } = options;
    this.lastUsed = Date.now();
    const ahead = this.tail;
    let release!: () => void;
    this.tail = new Promise<void>((resolve) => { release = resolve; });
    const mustWait = this.pending > 0;
    this.pending++;
    let tookTurn = false;
    let ran = false;
    let stopListener: { dispose(): void } | undefined;
    // Given back as it is when the request ends; the end-of-run bookkeeping below adds the mark.
    const result: ThreadRun = { outcome: 'aborted' };
    try {
      if (mustWait) {
        this.host.log(`thread ${this.short}: a message arrived while the run before it is still going; it waits for that run's current step`);
        try {
          onWait?.('⏳ Finishing the previous step before taking your message…');
        } catch {
          // a progress line on a stream VS Code has closed: nothing to tell
        }
      }
      // (Always asked, so the order of turns holds even if the count of requests and the lane ever disagree.)
      if (!(await this.waitForTurn(ahead, token))) {
        this.host.log(`thread ${this.short}: that message was stopped while it waited`);
        return result;
      }
      tookTurn = true;
      if (token?.isCancellationRequested) return result;
      // Only now, on its own turn: the run in front has ended, so the thread's last turn is final.
      if (transcript) {
        const diverged = this.divergence(transcript);
        if (diverged) throw new ThreadDiverged(diverged);
      }
      // Stop ends the run whatever it is doing: the step's stream, a command, a question to the user.
      stopListener = whenCancelled(token, () => this.loop.abort());
      ran = true;
      result.outcome = await this.loop.run(
        prompt,
        { ...callbacks, onStateChanged: (urgent?: boolean) => { this.store.changed(urgent); callbacks.onStateChanged?.(urgent); } },
        { ...loopOptions, shouldYield: () => this.pending > 1 || yieldRequested?.() === true || loopOptions.shouldYield?.() === true }
      );
      return result;
    } finally {
      // Whatever happens below, the lane is released and the request ends as it ended: this is bookkeeping, not the answer.
      try {
        stopListener?.dispose();
      } catch {
        // nothing left to listen to
      }
      // The end of a run always saves (a tool that finished after Stop included), before the next request takes its turn.
      // A request that never ran leaves the file alone: the saved tasks are kept in the order they were last written.
      if (ran) {
        try {
          if (transcript) result.mark = this.lastMark = this.track.record(transcript.answered + 1);
        } catch (err) {
          this.host.report('note where the conversation stood', err);
        }
        try {
          this.store.flush();
        } catch (err) {
          this.host.report('save the task', err);
        }
      }
      this.pending--;
      if (tookTurn) release();
      else void ahead.then(release); // never had its turn: the ones behind still wait for the run in front
      try {
        this.host.ended();
      } catch (err) {
        this.host.report('tidy the saved tasks', err);
      }
    }
  }

  /**
   * Waits for the run in front to end. It is given `yieldWaitMs` to finish its step (it stops after it, because this
   * request is waiting), then it is stopped outright and given `stopWaitMs`. False when `token`, this request's own Stop,
   * comes first.
   */
  private async waitForTurn(ahead: Promise<void>, token?: CancelSignal): Promise<boolean> {
    let cancelNow!: () => void;
    const cancelled = new Promise<'cancelled'>((resolve) => { cancelNow = () => resolve('cancelled'); });
    const subscription = whenCancelled(token, cancelNow);
    if (token?.isCancellationRequested) cancelNow();
    const ended = ahead.then(() => 'ended' as const);
    try {
      const stages: Array<{ waitMs: number; then: () => void }> = [
        { waitMs: this.host.yieldWaitMs, then: () => { this.host.log(`thread ${this.short}: the run before it is still in its step; stopping it`); this.loop.abort(); } },
        { waitMs: this.host.stopWaitMs, then: () => this.host.log(`thread ${this.short}: the run before it has not ended; going ahead without it`) },
      ];
      for (const stage of stages) {
        let timer: ReturnType<typeof setTimeout> | undefined;
        const timeout = new Promise<'timeout'>((resolve) => { timer = setTimeout(() => resolve('timeout'), stage.waitMs); });
        const winner = await Promise.race([ended, cancelled, timeout]);
        if (timer) clearTimeout(timer);
        if (winner === 'ended') return true;
        if (winner === 'cancelled') return false;
        stage.then();
      }
      return true;
    } finally {
      subscription?.dispose();
    }
  }
}

/** What ChatThreads.run gives back. */
export interface ChatRun {
  outcome: AgentRunOutcome;
  /** The thread the request ran on: the one its transcript named, or a new one when the transcript was cut back. */
  thread: Thread;
  /** The request's turn: its place among the answers of the Chat thread. */
  turn: number;
  /** The mark of the conversation as this run left it; the answer carries it. */
  mark?: string;
}

/** Said at the start of an answer that begins a new conversation, because the chat no longer matches what the thread remembered. */
export const NEW_CONVERSATION_NOTE =
  '_(This chat was edited in a way the agent can\'t follow, so it starts fresh here and doesn\'t remember the earlier messages. ' +
  'Files it already changed stay changed.)_\n\n';

/**
 * The Chat threads of this window. A request is matched to its thread by the id its history carries; a thread that is
 * not open is read back from its file, and a new one starts with nothing.
 */
export class ChatThreads {
  private readonly open = new Map<string, Thread>();
  /** Threads that take the saved task of before the threads each had a file (agent-state-chat.json) when they are opened. */
  private readonly adoptions = new Map<string, string>();
  private readonly host: ThreadHost;

  constructor(private readonly context: MaybeStateFolders, private readonly client: DlneClient, private readonly options: ChatThreadsOptions = {}) {
    this.host = {
      log: (line) => this.log(line),
      report: (what, err) => {
        try {
          this.log(`housekeeping: could not ${what}: ${logError(err instanceof Error ? err.message : err)}`);
        } catch {
          // writing it down is the last thing that may fail a request
        }
      },
      ended: () => this.prune(),
      yieldWaitMs: options.yieldWaitMs ?? 120_000,
      stopWaitMs: options.stopWaitMs ?? 20_000,
    };
    // Saved tasks age out as before, a week after they were written: the single file of before the threads (reading it
    // is what deletes it once it is old) and the threads' own, and only the newest 20 of those are kept.
    try {
      const folders = stateFolders(context);
      if (folders) loadAgentState(agentStateFile(folders, 'chat'), Date.now(), this.host.report);
      this.prune();
    } catch {
      // folders that cannot be read: nothing to age out
    }
  }

  private log(line: string): void {
    try {
      (this.options.log ?? logLine)(`agent ${line}`); // the same "agent thread xxxxxxxx:" start as the loop's own lines
    } catch {
      // the log never stops a task
    }
  }

  /**
   * Which thread a request belongs to. The id of an earlier answer in `history` (every answer carries one). Failing that,
   * a thread that has history but no id (a request in flight when the window was reloaded never got its answer stored;
   * threads from before 1.5.0 never had one) is matched to a saved task that began with the same first message, if exactly
   * one did. Otherwise it is a new thread.
   */
  idFor(history: readonly unknown[] | undefined): string {
    const known = threadIdFromHistory(history);
    if (known) return known;
    if ((history?.length ?? 0) > 0) {
      const saved = this.savedTaskFor(history!);
      if (saved) {
        if (saved.legacyFile) this.adoptions.set(saved.id, saved.legacyFile);
        return saved.id;
      }
    }
    return newThreadId();
  }

  private savedTaskFor(history: readonly unknown[]): { id: string; legacyFile?: string } | null {
    try {
      const first = firstAgentPrompt(history);
      const folders = stateFolders(this.context);
      if (!first || !folders) return null;
      const hits: Array<{ id: string; legacyFile?: string }> = [];
      for (const saved of listAgentThreads(agentThreadsDir(folders))) {
        if (this.open.has(saved.id)) continue; // a thread of this window answers by its own id
        if (firstWordsOf(loadAgentState(saved.file, Date.now(), this.host.report)) === first) hits.push({ id: saved.id });
      }
      const legacyFile = agentStateFile(folders, 'chat');
      if (firstWordsOf(loadAgentState(legacyFile, Date.now(), this.host.report)) === first) hits.push({ id: newThreadId(), legacyFile });
      return hits.length === 1 ? hits[0] : null;
    } catch {
      return null; // a folder that cannot be read: the thread starts fresh rather than the request failing
    }
  }

  /** A thread with a conversation of its own: nothing in it yet, apart from what `loadWith` puts there. */
  private newThread(id: string): { thread: Thread; loop: AgentLoop; track: ThreadTrack; store: AgentStateStore } {
    const loop = new AgentLoop(this.client, { contextProvider: workspaceContextBlock, label: `thread ${id.slice(0, 8)}`, log: this.options.log });
    const track = new ThreadTrack(loop);
    const store = openAgentThreadStore(this.context, id, track, this.host.report);
    return { thread: new Thread(id, loop, track, store, this.host), loop, track, store };
  }

  /** The thread's conversation: the one open in this window, else read back from its saved task, else a new one. */
  get(id: string): Thread {
    const open = this.open.get(id);
    if (open) {
      open.lastUsed = Date.now();
      return open;
    }
    const short = id.slice(0, 8);
    const { thread, loop, store } = this.newThread(id);
    const legacyFile = this.adoptions.get(id);
    this.adoptions.delete(id);
    if (legacyFile && store.adopt(legacyFile)) {
      this.log(`thread ${short}: took over the saved task from before the Chat threads each had their own`);
    } else if (store.load()) {
      this.log(`thread ${short}: resumed from its saved task (${loop.exportState().messages.length} messages)`);
    } else {
      this.log(`thread ${short}: a new conversation`);
    }
    this.open.set(id, thread);
    this.dropIdle();
    return thread;
  }

  /**
   * Runs a request on the thread `id` names, after checking the Chat transcript (`history`) against what the thread
   * remembers. A transcript that shows fewer answers than the thread has run (the user went back, or this is the other
   * branch of a fork) is cut back: the request runs on a copy of the thread's conversation as the transcript shows it,
   * under a new thread id (the original stays as it is), or on a new conversation when that cannot be found. The answer
   * names the thread it ran on (ChatRun.thread), and the mark of the conversation it left (ChatRun.mark).
   */
  async run(id: string, history: readonly unknown[] | undefined, prompt: string, callbacks: AgentCallbacks, options: ChatRunOptions = {}): Promise<ChatRun> {
    const { onNote, onThread, ...threadOptions } = options;
    const transcript = readTranscript(history);
    let thread = this.get(id);
    const told = (about: Thread) => {
      try {
        onThread?.(about);
      } catch {
        // a listener that fails does not stop the request
      }
    };
    told(thread);
    let run: ThreadRun;
    try {
      run = await thread.run(prompt, callbacks, { ...threadOptions, transcript });
    } catch (err) {
      if (!(err instanceof ThreadDiverged)) throw err;
      thread = this.copyForTranscript(thread, err.divergence);
      told(thread);
      if (err.divergence.kind === 'unmatched') {
        try {
          onNote?.(NEW_CONVERSATION_NOTE);
        } catch {
          // the stream was closed: nobody to tell
        }
      }
      run = await thread.run(prompt, callbacks, { ...threadOptions, transcript });
    }
    return { outcome: run.outcome, thread, turn: transcript.answered + 1, mark: run.mark?.id };
  }

  /** A new thread for a request whose transcript does not fit `from`: its conversation cut back to a mark, or empty. */
  private copyForTranscript(from: Thread, divergence: Divergence): Thread {
    const id = newThreadId();
    const { thread, track, store } = this.newThread(id);
    let how: string;
    const state = divergence.kind === 'cut back' ? from.cutBackTo(divergence.mark) : null;
    if (divergence.kind === 'cut back' && state && track.importState(state)) {
      store.flush(); // the copy is on disk before anything else can ask for it
      how = `a copy of thread ${from.id.slice(0, 8)} as the chat shows it now, cut back to its turn ${divergence.mark.turn}`;
    } else {
      how = `a new conversation: ${divergence.kind === 'unmatched' ? divergence.why : 'the saved task could not be cut back to where the chat now stands'}`;
    }
    this.log(`thread ${id.slice(0, 8)}: ${how} (thread ${from.id.slice(0, 8)} is left as it was)`);
    this.open.set(id, thread);
    this.dropIdle();
    return thread;
  }

  /** /clear: the thread's conversation and its saved task are forgotten; the thread (its id) stays. */
  forget(id: string): void {
    const open = this.open.get(id);
    if (open) {
      open.clear();
    } else {
      const folders = stateFolders(this.context);
      try {
        if (folders) deleteAgentState(agentThreadFile(folders, id));
      } catch {
        // a file that cannot be removed is overwritten by the next save
      }
    }
    const legacyFile = this.adoptions.get(id);
    if (legacyFile) { // cleared before it was ever opened: the old file must not come back to life
      this.adoptions.delete(id);
      try {
        deleteAgentState(legacyFile);
      } catch {
        // as above
      }
    }
    this.log(`thread ${id.slice(0, 8)}: cleared`);
  }

  /** The extension is closing: the task of every thread that is running is saved (an idle one was saved when its last run ended). */
  dispose(): void {
    for (const thread of this.open.values()) {
      if (thread.busy) thread.store.flush();
    }
  }

  /** Threads open in this window stay at most as many as are kept on disk; an idle one is dropped from memory (its file keeps it). */
  private dropIdle(): void {
    while (this.open.size > MAX_SAVED_THREADS) {
      const idle = [...this.open.values()].filter((thread) => !thread.busy).sort((a, b) => a.lastUsed - b.lastUsed)[0];
      if (!idle) return;
      this.open.delete(idle.id);
    }
  }

  /** Only the newest MAX_SAVED_THREADS saved tasks are kept; the threads open in this window are never deleted. */
  private prune(): void {
    const folders = stateFolders(this.context);
    if (folders) pruneAgentThreads(agentThreadsDir(folders), MAX_SAVED_THREADS, new Set(this.open.keys()), Date.now(), this.host.report);
  }
}
