import { randomUUID } from 'crypto';
import * as fs from 'fs';
import * as path from 'path';
import type { AgentStateJSON } from './agentLoop';

/**
 * The saved task (docs/agent-memory.md section 8): the agent's conversation, kept in a file so a VS Code restart
 * does not end a task and Continue still works after reopening. Nothing here imports `vscode` at runtime, so the
 * tests can run it under plain Node; the chats hand it the pieces of their ExtensionContext.
 */

/** A state older than this is dropped, not loaded: a week-old task is not what the user comes back to. */
export const STATE_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;

/** The most often a running conversation is written to disk. The end of a run always writes. */
export const SAVE_EVERY_MS = 5000;

/**
 * The chats that keep their own saved task. `chat` is the one file per workspace that VS Code's Chat view used before
 * 1.5.0; from then on each Chat thread has a file of its own (agentThreadFile), and `chat` is only read to pick a task
 * up from before.
 */
export type AgentSurface = 'chat' | 'sidebar';

/** The two folders of an ExtensionContext the file can live in (only these are read, so tests can fake them). */
export interface StateFolders {
  storageUri?: { fsPath: string };
  globalStorageUri: { fsPath: string };
}

/**
 * Where a chat keeps its saved task: VS Code's private folder for this workspace, never the repo (the state holds
 * code excerpts). With no folder open there is no workspace storage, so it goes in the global one under its own name.
 */
export function agentStateFile(context: StateFolders, surface: AgentSurface): string {
  if (context.storageUri) {
    return path.join(context.storageUri.fsPath, `agent-state-${surface}.json`);
  }
  return path.join(context.globalStorageUri.fsPath, `agent-state-${surface}-nofolder.json`);
}

/** The newest this many Chat threads keep a saved task; the rest are deleted when a run ends. */
export const MAX_SAVED_THREADS = 20;

/**
 * A Chat thread's id is a random UUID, the same kind as a session id (and, like it, means nothing). It names a file and
 * comes back from VS Code's stored chat, so nothing but letters, digits and dashes is accepted: never a path.
 */
export function isThreadId(value: unknown): value is string {
  return typeof value === 'string' && /^[A-Za-z0-9-]{8,64}$/.test(value);
}

export function newThreadId(): string {
  return randomUUID();
}

/** The folder of the Chat threads' saved tasks: one file each, in this workspace's storage (or the global one with no folder open). */
export function agentThreadsDir(context: StateFolders): string {
  return context.storageUri
    ? path.join(context.storageUri.fsPath, 'agent-threads')
    : path.join(context.globalStorageUri.fsPath, 'agent-threads-nofolder');
}

/** Where one Chat thread keeps its saved task. */
export function agentThreadFile(context: StateFolders, threadId: string): string {
  if (!isThreadId(threadId)) throw new Error('not a thread id');
  return path.join(agentThreadsDir(context), `${threadId}.json`);
}

/** The thread ids that have a saved task, newest first (by when each was last written). */
export function listAgentThreads(dir: string): Array<{ id: string; file: string; modified: number }> {
  let names: string[];
  try {
    names = fs.readdirSync(dir);
  } catch {
    return [];
  }
  const found: Array<{ id: string; file: string; modified: number }> = [];
  for (const name of names) {
    const match = /^([A-Za-z0-9-]{8,64})\.json$/.exec(name);
    if (!match) continue;
    const file = path.join(dir, name);
    try {
      found.push({ id: match[1], file, modified: fs.statSync(file).mtimeMs });
    } catch {
      // gone since the listing
    }
  }
  return found.sort((a, b) => b.modified - a.modified);
}

/**
 * Told when housekeeping (deleting an old saved task, an expired one, a leftover temp file) could not do its job: a file an
 * antivirus scanner or an indexer holds open (EBUSY, EPERM on Windows), a folder that is read-only. Housekeeping never
 * fails the request it runs beside; whoever listens decides whether to write it down.
 */
export type HousekeepingReport = (what: string, err: unknown) => void;

/**
 * Keeps the newest `keep` saved threads and deletes the rest, with any saved task over a week old (it would not be
 * loaded) and any temp file a crash left behind. `inUse`: threads open in this window, never deleted. A file that cannot
 * be deleted (a scanner or an indexer holds it open, the folder is read-only) is left: the next pass tries again, and
 * nothing else is stopped by it. It is reported once per pass and kind of file, with how many, not once per file: a
 * problem that stays would otherwise fill the log at every request.
 */
export function pruneAgentThreads(
  dir: string,
  keep: number = MAX_SAVED_THREADS,
  inUse: ReadonlySet<string> = new Set(),
  now: number = Date.now(),
  report?: HousekeepingReport
): void {
  const stuck = { tasks: 0, temps: 0 };
  let firstTask: unknown;
  let firstTemp: unknown;
  listAgentThreads(dir).forEach((thread, index) => {
    if (inUse.has(thread.id)) return;
    if (index >= keep || now - thread.modified > STATE_MAX_AGE_MS) {
      try {
        deleteAgentState(thread.file);
      } catch (err) {
        if (stuck.tasks++ === 0) firstTask = err;
      }
    }
  });
  let names: string[] = [];
  try {
    names = fs.readdirSync(dir);
  } catch {
    // nothing to tidy
  }
  for (const name of names) {
    if (!name.endsWith('.tmp')) continue;
    const file = path.join(dir, name);
    try {
      if (now - fs.statSync(file).mtimeMs > 60 * 60 * 1000) fs.rmSync(file, { force: true });
    } catch (err) {
      if (stuck.temps++ === 0) firstTemp = err;
    }
  }
  const plural = (n: number) => (n === 1 ? '' : 's');
  if (stuck.tasks > 0) report?.(`delete ${stuck.tasks} old saved task${plural(stuck.tasks)}`, firstTask);
  if (stuck.temps > 0) report?.(`remove ${stuck.temps} leftover temp file${plural(stuck.temps)}`, firstTemp);
}

/**
 * Writes the state so a crash half way never leaves a broken file: into a temp file beside it, then renamed over
 * the real one. Only the user can read it (0600), because it holds their code.
 */
export function saveAgentState(file: string, state: AgentStateJSON): void {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const temp = `${file}.${process.pid}.tmp`;
  try {
    fs.writeFileSync(temp, JSON.stringify(state), { mode: 0o600 });
    fs.renameSync(temp, file);
  } catch (err) {
    fs.rmSync(temp, { force: true });
    throw err;
  }
}

/**
 * The saved state, or null when there is none worth loading: no file, a file that is not a state, or one saved
 * more than 7 days ago (that one is deleted: if it cannot be, that is reported and it is still not loaded, so an expired
 * task is never what stops a chat). Whether the content fits the AgentLoop is for importState to say.
 */
export function loadAgentState(file: string, now: number = Date.now(), report?: HousekeepingReport): AgentStateJSON | null {
  let state: any;
  try {
    state = JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch {
    return null;
  }
  const savedAt = state && typeof state === 'object' ? Date.parse(state.savedAt) : NaN;
  if (Number.isNaN(savedAt)) {
    return null;
  }
  // Too old, or dated in the future (a clock that jumped, or a hand-made file): neither would ever expire properly.
  if (now - savedAt > STATE_MAX_AGE_MS || savedAt - now > 24 * 60 * 60 * 1000) {
    try {
      deleteAgentState(file);
    } catch (err) {
      report?.('delete an expired saved task', err);
    }
    return null;
  }
  return state as AgentStateJSON;
}

/** Removes the saved state; nothing to remove is fine. */
export function deleteAgentState(file: string): void {
  fs.rmSync(file, { force: true });
}

/** The part of an AgentLoop the saved task needs. */
export interface StatefulLoop {
  exportState(): AgentStateJSON;
  importState(json: unknown): boolean;
}

/**
 * One chat's saved task: loading it back when the chat is created, saving it as the conversation goes, and
 * deleting it when the conversation is cleared. A store with no file (no storage folder to use) does nothing.
 * A disk problem never reaches the chat; the task simply is not saved.
 */
export class AgentStateStore {
  private lastSaved = 0;

  constructor(
    private readonly file: string | undefined,
    private readonly loop: StatefulLoop,
    private readonly now: () => number = Date.now,
    private readonly report?: HousekeepingReport
  ) {}

  /** Takes the saved task back into the loop. True when there was one and it fit. */
  load(): boolean {
    if (!this.file) return false;
    const state = loadAgentState(this.file, this.now(), this.report);
    return state !== null && this.loop.importState(state);
  }

  /**
   * Takes another file's saved task into the loop as this chat's own, once: it is written to this chat's file and the
   * other one goes (the single file of before the Chat threads each had theirs). True when there was one and it fit.
   */
  adopt(other: string | undefined): boolean {
    if (!this.file || !other) return false;
    const state = loadAgentState(other, this.now(), this.report);
    if (state === null || !this.loop.importState(state)) return false;
    this.flush();
    try {
      deleteAgentState(other);
    } catch (err) {
      this.report?.('delete the saved task that was taken over', err); // left where it is: it expires by itself
    }
    return true;
  }

  /**
   * Called whenever the conversation changed: writes it, but not more than once every SAVE_EVERY_MS, except when
   * `urgent` (a turn changed the machine: a file written, a command run), which is written at once so a crash right
   * after cannot leave a saved task that would do it again.
   */
  changed(urgent = false): void {
    if (urgent || this.now() - this.lastSaved >= SAVE_EVERY_MS) {
      this.flush();
    }
  }

  /** Writes it now (the end of a run). An empty conversation leaves no file. */
  flush(): void {
    if (!this.file) return;
    this.lastSaved = this.now();
    try {
      const state = this.loop.exportState();
      if (state.messages.length === 0) {
        deleteAgentState(this.file);
      } else {
        saveAgentState(this.file, state);
      }
    } catch {
      // Not saved this time; the next change or the end of the run tries again.
    }
  }

  /** The conversation was cleared: its saved copy goes too. */
  clear(): void {
    if (!this.file) return;
    this.lastSaved = 0;
    try {
      deleteAgentState(this.file);
    } catch {
      // A file that cannot be removed is overwritten by the next save.
    }
  }
}

/** An ExtensionContext, or as much of one as the tests' fakes have. */
export type MaybeStateFolders = { storageUri?: { fsPath: string }; globalStorageUri?: { fsPath: string } } | undefined;

/** The folders a saved task can live in, or undefined when the context has neither (nothing is then saved). */
export function stateFolders(context: MaybeStateFolders): StateFolders | undefined {
  return context && (context.storageUri || context.globalStorageUri) ? (context as StateFolders) : undefined;
}

/** The store for a chat, tolerating an ExtensionContext without storage folders (as the tests' fakes are). */
export function openAgentStateStore(context: MaybeStateFolders, surface: AgentSurface, loop: StatefulLoop): AgentStateStore {
  const folders = stateFolders(context);
  return new AgentStateStore(folders && agentStateFile(folders, surface), loop);
}

/**
 * The folder of the sidebar's sessions' saved tasks (1.5.3): one file each, apart from the Chat threads' so neither's
 * housekeeping ever deletes the other's. A session id is a thread id (isThreadId).
 */
export function sidebarSessionsDir(context: StateFolders): string {
  return context.storageUri
    ? path.join(context.storageUri.fsPath, 'sidebar-sessions')
    : path.join(context.globalStorageUri.fsPath, 'sidebar-sessions-nofolder');
}

/** The store for one sidebar session: its own file in sidebarSessionsDir. */
export function openSidebarSessionStore(context: MaybeStateFolders, sessionId: string, loop: StatefulLoop): AgentStateStore {
  if (!isThreadId(sessionId)) throw new Error('not a session id');
  const folders = stateFolders(context);
  return new AgentStateStore(folders && path.join(sidebarSessionsDir(folders), `${sessionId}.json`), loop);
}

/** Where the sidebar's single chat kept its saved task before 1.5.3, or undefined with no storage folder. */
export function legacySidebarStateFile(context: MaybeStateFolders): string | undefined {
  const folders = stateFolders(context);
  return folders && agentStateFile(folders, 'sidebar');
}

/** The store for one Chat thread: its own file, among the threads' (see agentThreadFile). */
export function openAgentThreadStore(
  context: MaybeStateFolders,
  threadId: string,
  loop: StatefulLoop,
  report?: HousekeepingReport
): AgentStateStore {
  const folders = stateFolders(context);
  return new AgentStateStore(folders && agentThreadFile(folders, threadId), loop, Date.now, report);
}
