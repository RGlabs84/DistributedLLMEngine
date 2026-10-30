import * as vscode from 'vscode';
import * as path from 'path';
import * as crypto from 'crypto';
import * as os from 'os';
import { AgentLoop, AgentRunOutcome, workspaceContextBlock } from '../agent/agentLoop';
import { AgentStateStore, legacySidebarStateFile, openSidebarSessionStore } from '../agent/agentState';
import {
  LEGACY_HISTORY_KEY, NEW_SESSION_TITLE, SESSIONS_KEY, SessionIndex, byRecent, historyKey, newSession, readIndex,
  sessionRows, sessionTitle, sessionsToDrop,
} from './sidebarSessions';
import { DlneClient } from '../client/dlneClient';
import { swarmModelLine } from '../client/modelChoice';
import { QueueUpdate, queueDetail, queueHeadline } from '../client/queue';
import { WorkerManager, WorkerStatus } from '../worker/workerManager';
import { contributionCopy, roleCopy } from '../worker/contribution';
import {
  DonateChoice, DonateMachine, DonatePanelModel, buildDonatePanel, choiceFromSettings, donateMachine, readDonateSettings,
  reconcileDraft, validateChoice,
} from '../worker/donatePanel';
import { TrayAddon } from '../worker/trayAddon';
import { TRAY_PAGE_URL } from '../worker/trayApp';
import { DONATE_CSS, createDonatePanel } from './donateView';
import { phoneChatUrl } from '../serverUrl';
import { ToolCallResult, ToolRegistry } from '../agent/tools';
import { renderMarkdown } from './markdown';
import { memoryMode, undoMemoryChange } from '../agent/memory';

export interface ChatMessageItem {
  id: string;
  role: 'user' | 'assistant';
  text: string;
  thinking?: string;
  toolCalls?: Array<{
    name: string;
    args: Record<string, any>;
    result?: ToolCallResult;
  }>;
}

/** A message to the page about a task. The same shape is sent live and, later, again to a page that was away. */
type PageEvent = { type: string; [key: string]: any };

/**
 * What an error says, in words. An Error does not survive the trip to the page (JSON drops its message), and
 * "[object Object]" tells nobody anything, so the host sends text. A refusal from the swarm carries its HTTP status.
 */
export function describeError(err: unknown): string {
  let message = '';
  if (err instanceof Error) {
    message = err.message || err.name;
  } else if (typeof err === 'string') {
    message = err;
  } else if (err && typeof err === 'object') {
    const said = (err as { message?: unknown }).message;
    if (typeof said === 'string') {
      message = said;
    } else {
      try {
        message = JSON.stringify(err) ?? '';
      } catch {
        message = '';
      }
      if (message === '{}') message = '';
    }
  }
  message = message.trim();
  if (!message) return 'something went wrong, and nothing more is known';
  const status = (err as { status?: unknown } | null | undefined)?.status;
  return typeof status === 'number' && !message.includes(String(status)) ? `${message} (HTTP ${status})` : message;
}

/**
 * A tool result as the page needs it. The page draws a diff's path, counts and changed lines, never the two whole
 * files, so those stay on this side: they are big, and the chat can be sent again every time the view is shown.
 */
function slimResult(result: ToolCallResult | undefined): ToolCallResult | undefined {
  if (!result || !result.fileDiff) return result;
  const { originalContent: _before, modifiedContent: _after, ...diff } = result.fileDiff;
  return { ...result, fileDiff: diff };
}

/** The saved chat as the page draws it: results without the whole files, and without the arguments it never reads. */
function historyForPage(items: ChatMessageItem[]): ChatMessageItem[] {
  return items.map((item) => (item.toolCalls
    ? { ...item, toolCalls: item.toolCalls.map((call) => ({ name: call.name, args: {}, result: slimResult(call.result) })) }
    : item));
}

/** The events after which the page has taken its status line down (it does so for each of them). */
const TAKES_DOWN_STATUS_LINE = new Set(['thinkingDelta', 'contentDelta', 'toolStart', 'toolEnd', 'turnComplete', 'taskAborted', 'error']);

/**
 * Writes down what the page was told, in the fewest messages that draw the same thing: a run of text is one message,
 * a status line only matters while it is the last thing said, and a tool's arguments and whole files, which the page
 * never draws, are left out. A page that was away is shown the task by playing these back.
 */
function recordEvent(events: PageEvent[], event: PageEvent): void {
  let last = events[events.length - 1];
  if (last?.type === 'loadingProgress' && TAKES_DOWN_STATUS_LINE.has(event.type)) {
    events.pop();
    last = events[events.length - 1];
  }
  if (event.type === 'loadingProgress' && last?.type === 'loadingProgress') {
    events[events.length - 1] = { ...event };
  } else if ((event.type === 'thinkingDelta' || event.type === 'contentDelta') && last?.type === event.type) {
    last.delta += event.delta;
  } else if (event.type === 'toolStart') {
    events.push({ type: 'toolStart', toolName: event.toolName });
  } else if (event.type === 'toolEnd') {
    events.push({ type: 'toolEnd', result: slimResult(event.result) });
  } else {
    events.push({ ...event });
  }
}

/**
 * One sidebar session (1.5.3): a conversation of its own, like a thread of VS Code's Chat view. It is made when it is
 * first shown or run, and stays in memory after that, so a task keeps running when another session is shown.
 */
interface Session {
  id: string;
  loop: AgentLoop;
  /** The agent's conversation as saved on disk (docs/agent-memory.md section 8). */
  store: AgentStateStore;
  /** The chat as the page draws it. */
  history: ChatMessageItem[];
  /**
   * Whether a task is running. Starting a task stops the one before it (AgentLoop.run), so a message never starts
   * one while another runs in the same session: this is the host's own word on it, whatever a page believes.
   */
  running: boolean;
  /** Stop was pressed (or the session deleted) and the task has not ended yet: it ends when its step does. */
  stopping: boolean;
  /**
   * The latest task as the page was told it, with the place of its first line in `history`. It is kept after the task
   * ends (a Continue button, an error line) until the next task, so a page that was away, made again, or showing
   * another session at the time, is shown exactly what it would have seen.
   */
  turn: { index: number; events: PageEvent[] } | null;
}

export class SidebarProvider implements vscode.WebviewViewProvider {
  private _view?: vscode.WebviewView;
  /** Which sessions there are and the one shown (sidebarSessions.ts), as saved in the workspace state. */
  private index: SessionIndex;
  /** The sessions made so far in this window, by id. */
  private readonly open = new Map<string, Session>();
  /** The page may have missed some of the chat (it was hidden, or gone, while the task went on): it is redrawn on show. */
  private chatStale = false;

  constructor(
    private readonly _extensionUri: vscode.Uri,
    private readonly client: DlneClient,
    private readonly workerManager: WorkerManager,
    private readonly context?: vscode.ExtensionContext
  ) {
    const saved = readIndex(this.context?.workspaceState.get(SESSIONS_KEY));
    if (saved) {
      this.index = saved;
    } else {
      // The first start of 1.5.3: the one chat of before becomes the first session, its saved task with it.
      const first = newSession(Date.now());
      const legacy = this.context?.workspaceState.get<ChatMessageItem[]>(LEGACY_HISTORY_KEY);
      const history = Array.isArray(legacy) ? legacy : [];
      first.title = sessionTitle(history.find((item) => item.role === 'user')?.text);
      this.index = { active: first.id, sessions: [first] };
      if (history.length > 0) void this.context?.workspaceState.update(historyKey(first.id), history);
      const session = this.session(first.id, legacySidebarStateFile(this.context));
      if (session.history.length === 0) this.forgetTask(session);
      void this.context?.workspaceState.update(LEGACY_HISTORY_KEY, undefined);
      this.saveIndex();
    }

    this.workerManager.onStatusChange((status) => {
      this._view?.webview.postMessage({
        type: 'workerStatus', status, copy: contributionCopy(status), role: roleCopy(status), donate: this._donateModel(status),
      });
    });
  }

  /**
   * What the Donate panel's sliders said last, kept while the panel is open: the amounts of a switched-off job
   * (the settings do not hold them) and a choice with nothing switched on (no setting can say that). It is dropped
   * as soon as the settings disagree with it (reconcileDraft).
   */
  private draft: DonateChoice | undefined;
  /** Writes of the panel's settings, one after another, so two quick changes cannot interleave. */
  private donateWrites: Promise<unknown> = Promise.resolve();
  /** The panel's "Optional add-on" card (DLNE Donate, the tray app): nothing is fetched until its button is clicked. */
  private readonly trayAddon = new TrayAddon(() => this._postDonate());

  private _donateMachine(status: WorkerStatus): DonateMachine {
    return donateMachine(status.gpu, os.totalmem(), os.cpus().length);
  }

  /** The panel for this status. `choice` is what the sliders say right now, when that is not what the settings say. */
  private _donateModel(status: WorkerStatus, choice?: DonateChoice): DonatePanelModel {
    const machine = this._donateMachine(status);
    const settings = readDonateSettings(vscode.workspace.getConfiguration('dlne'));
    return buildDonatePanel({
      machine,
      settings,
      state: status.state,
      external: status.external === true,
      trayApp: status.trayApp === true,
      started: status.startedWith,
      choice: choice ?? reconcileDraft(this.draft, settings, machine),
      addon: this.trayAddon.view(),
    });
  }

  private _postDonate(choice?: DonateChoice): void {
    this._view?.webview.postMessage({ type: 'donatePanel', model: this._donateModel(this.workerManager.getStatus(), choice) });
  }

  /** What the panel shows now, as a valid choice: the webview's posts are checked against it. */
  private _shownChoice(machine: DonateMachine): DonateChoice {
    const settings = readDonateSettings(vscode.workspace.getConfiguration('dlne'));
    return reconcileDraft(this.draft, settings, machine) ?? choiceFromSettings(settings, machine);
  }

  private _writeChoice(choice: DonateChoice, machine: DonateMachine): Promise<unknown> {
    const write = () => this.workerManager.applyDonateChoice(choice, machine);
    this.donateWrites = this.donateWrites.then(write, write);
    return this.donateWrites;
  }

  /** The sliders moved (`commit`: and were let go): check the values, and answer with the panel for them. */
  private async _onDonateChoice(raw: unknown, commit: boolean): Promise<void> {
    const status = this.workerManager.getStatus();
    const machine = this._donateMachine(status);
    const choice = validateChoice(raw, machine, this._shownChoice(machine));
    this.draft = choice;
    if (commit) {
      await this._writeChoice(choice, machine);
    }
    this._postDonate(choice);
  }

  /**
   * The big button. Starting sets the settings from the sliders first (checked, like every post), so the old modal
   * RAM offers do not appear for an amount the panel already holds; stopping needs nothing from the sliders.
   */
  private async _onToggleWorker(raw: unknown): Promise<void> {
    const status = this.workerManager.getStatus();
    const running = status.state === 'contributing' || status.state === 'starting';
    if (raw === undefined || running) {
      await this.workerManager.toggle();
    } else {
      const machine = this._donateMachine(status);
      const choice = validateChoice(raw, machine, this._shownChoice(machine));
      this.draft = choice;
      if (!choice.gpuOn && !choice.cpuOn) {
        this._postDonate(choice);
        return;
      }
      await this._writeChoice(choice, machine);
      await this.workerManager.toggle({ fromPanel: true });
    }
    this._postDonate();
  }

  /** "Apply (restarts the worker)": the settings follow the sliders, then the worker starts again with them. */
  private async _onDonateApply(raw: unknown): Promise<void> {
    const status = this.workerManager.getStatus();
    const machine = this._donateMachine(status);
    const choice = validateChoice(raw, machine, this._shownChoice(machine));
    this.draft = choice;
    if (!choice.gpuOn && !choice.cpuOn) {
      this._postDonate(choice);
      return;
    }
    await this._writeChoice(choice, machine);
    await this.workerManager.restartWorker();
    this._postDonate();
  }

  /** What the queue banner shows for a place in line (null hides it). */
  private _queueView(queue: QueueUpdate | null | undefined) {
    return queue ? { headline: queueHeadline(queue), detail: queueDetail(queue) } : null;
  }

  // ---- sessions ------------------------------------------------------------------------------------------------------

  /**
   * The session with this id, made the first time it is asked for: its chat from the workspace state, its saved task
   * from its own file (or, once, from `adopt`: the single chat's file of before 1.5.3). The agent's memory of the task
   * comes back with the visible chat, so a reload carries on where it was; a saved task with no chat to show for it is
   * dropped, because the page and the agent must agree on what happened.
   */
  private session(id: string, adopt?: string): Session {
    const known = this.open.get(id);
    if (known) return known;
    // Persistent memory (AGENTS.md and the memory files) goes with every request: docs/agent-memory.md section 7.
    const loop = new AgentLoop(this.client, { contextProvider: workspaceContextBlock });
    const saved = this.context?.workspaceState.get<ChatMessageItem[]>(historyKey(id));
    const session: Session = {
      id, loop, store: openSidebarSessionStore(this.context, id, loop), history: Array.isArray(saved) ? saved : [],
      running: false, stopping: false, turn: null,
    };
    this.open.set(id, session);
    const loaded = adopt ? session.store.adopt(adopt) : session.store.load();
    if (loaded && session.history.length === 0) this.forgetTask(session);
    return session;
  }

  /** The session the sidebar shows. */
  private get active(): Session {
    return this.session(this.index.active);
  }

  /** The shown session's chat and agent, as the single chat of before 1.5.3 had them (the tests read these). */
  get conversationHistory(): ChatMessageItem[] {
    return this.active.history;
  }
  get agentLoop(): AgentLoop {
    return this.active.loop;
  }

  private forgetTask(session: Session): void {
    session.loop.clearHistory();
    session.store.clear();
  }

  private saveIndex(): void {
    void this.context?.workspaceState.update(SESSIONS_KEY, this.index);
  }

  /** Keeps the session's chat (its last 40 entries) and moves it to the top of the list. */
  private saveHistory(session: Session) {
    if (this.context) {
      // Keep at most last 40 turns to avoid workspaceState bloat
      void this.context.workspaceState.update(historyKey(session.id), session.history.slice(-40));
    }
    const meta = this.index.sessions.find((s) => s.id === session.id);
    if (meta) {
      meta.updated = Date.now();
      this.saveIndex();
    }
  }

  private runningIds(): Set<string> {
    return new Set([...this.open.values()].filter((s) => s.running).map((s) => s.id));
  }

  /** The session list and the shown session's title, for the page's header and its list of sessions. */
  private sessionsMessage(): PageEvent {
    const rows = sessionRows(this.index, this.runningIds());
    return { type: 'sessions', active: this.index.active, title: rows.find((r) => r.active)?.title ?? NEW_SESSION_TITLE, list: rows };
  }

  private postSessions(): void {
    this.post(this.sessionsMessage());
  }

  /** Shows another session: the page draws its chat, and its task as far as it has gone, from the start. */
  private showSession(id: string): void {
    if (!this.index.sessions.some((s) => s.id === id) || id === this.index.active) {
      this.postSessions();
      return;
    }
    this.index.active = id;
    this.saveIndex();
    this.syncPage();
  }

  /**
   * New chat: the one shown is kept in the list and an empty one is shown. One that is still empty is reused rather
   * than piling up empty sessions. Housekeeping then lets the oldest go past MAX_SESSIONS (never a running one).
   */
  private newChat(): void {
    const current = this.active;
    if (current.history.length === 0 && !current.running) {
      this.syncPage();
      return;
    }
    const meta = newSession(Date.now());
    this.index.sessions.push(meta);
    this.index.active = meta.id;
    for (const id of sessionsToDrop(this.index, this.runningIds())) this.dropSession(id);
    this.saveIndex();
    this.syncPage();
  }

  /** The trash button of a row: a running session is not deleted (the page offers no button for one). */
  private deleteSession(id: string): void {
    const meta = this.index.sessions.find((s) => s.id === id);
    if (!meta || this.open.get(id)?.running) {
      this.postSessions();
      return;
    }
    this.dropSession(id);
    if (this.index.active === id) {
      const next = byRecent(this.index.sessions)[0];
      if (next) {
        this.index.active = next.id;
      } else {
        const fresh = newSession(Date.now());
        this.index.sessions.push(fresh);
        this.index.active = fresh.id;
      }
      this.saveIndex();
      this.syncPage();
    } else {
      this.saveIndex();
      this.postSessions();
    }
  }

  /** Forgets a session: its saved task, its chat and its place in the list. */
  private dropSession(id: string): void {
    const session = this.session(id);
    this.forgetTask(session);
    this.open.delete(id);
    void this.context?.workspaceState.update(historyKey(id), undefined);
    this.index.sessions = this.index.sessions.filter((s) => s.id !== id);
  }

  resolveWebviewView(
    webviewView: vscode.WebviewView,
    context: vscode.WebviewViewResolveContext,
    _token: vscode.CancellationToken
  ) {
    this._view = webviewView;
    webviewView.webview.options = {
      enableScripts: true,
      localResourceRoots: [this._extensionUri]
    };

    // Pre-populate HTML with current worker status to eliminate visual flicker
    webviewView.webview.html = this._getHtmlForWebview();

    // Push state immediately on mount: the chat, and the task too when the view is made again in the middle of one
    // (moved to another side bar, closed and opened again).
    this.syncPage();

    // The view is kept while it is hidden (registerWebviewViewProvider, extension.ts), but VS Code does not deliver
    // messages to a hidden view: whatever the task did meanwhile is sent again when it is shown.
    webviewView.onDidChangeVisibility?.(() => {
      if (this._view === webviewView && webviewView.visible !== false) this.onShown();
    });

    // The Auto button shows the switch wherever it is flipped (here, the status bar, the command, the settings).
    const autoListener = vscode.workspace.onDidChangeConfiguration?.((e) => {
      if (e.affectsConfiguration('dlne.autoApproveAll')) {
        this._view?.webview.postMessage({ type: 'autoApprove', on: ToolRegistry.autoApproveAllOn() });
      }
    });
    webviewView.onDidDispose?.(() => autoListener?.dispose());

    // The model line follows the status the status bar already polls; nothing here asks for it.
    const stopFollowing = this.client.onClusterStatus?.((status) => {
      this._view?.webview.postMessage({ type: 'modelLine', text: swarmModelLine(status) });
    });
    webviewView.onDidDispose?.(() => {
      stopFollowing?.();
      // A closed view throws when it is used; the task goes on unseen and the next view is shown it (syncPage).
      if (this._view === webviewView) this._view = undefined;
    });

    webviewView.webview.onDidReceiveMessage(async (data) => {
      switch (data.type) {
        case 'ready': {
          // The page has just started: the first time, or again after VS Code made it anew (the view was moved, the
          // webviews were reloaded). It asks for the chat, so it never waits for a message that was sent too early.
          this.syncPage();
          break;
        }
        case 'copyTrustId': {
          // Only a trust id (32 hex characters, from the worker's own output) is ever copied.
          if (typeof data.id === 'string' && /^[0-9a-f]{32}$/.test(data.id)) {
            await vscode.env?.clipboard?.writeText(data.id);
            vscode.window.showInformationMessage('DLNE: trust id copied.');
          }
          break;
        }
        case 'sendMessage': {
          await this.handleUserMessage(data.text);
          break;
        }
        case 'setAutoApprove': {
          await ToolRegistry.setAutoApproveAll(data.on === true);
          this._view?.webview.postMessage({ type: 'autoApprove', on: ToolRegistry.autoApproveAllOn() });
          break;
        }
        case 'copyCode': {
          if (typeof data.text === 'string' && data.text.length <= 1_000_000) {
            await vscode.env?.clipboard?.writeText(data.text);
          }
          break;
        }
        case 'abortTask': {
          const session = this.active;
          if (session.running) {
            // The task is over when its step is: a message sent before that would start a second one over it, so the
            // page keeps showing it (as stopping) and gets its end-of-turn message from the task itself.
            session.stopping = true;
            session.loop.abort();
            this.emit(session, { type: 'stopping' });
          } else {
            // Nothing is running, so this page was behind: it goes back to idle.
            this.post({ type: 'taskAborted' }, true);
          }
          break;
        }
        // New chat (1.5.3); 'clearChat' is what a page from before 1.5.3 still sends for its Clear button.
        case 'newSession':
        case 'clearChat': {
          this.newChat();
          break;
        }
        case 'openSession': {
          if (typeof data.id === 'string') this.showSession(data.id);
          break;
        }
        case 'deleteSession': {
          if (typeof data.id === 'string') this.deleteSession(data.id);
          break;
        }
        case 'toggleWorker': {
          await this._onToggleWorker(data.choice);
          break;
        }
        case 'donateChoice': {
          await this._onDonateChoice(data.choice, data.commit === true);
          break;
        }
        case 'donateApply': {
          await this._onDonateApply(data.choice);
          break;
        }
        case 'trayAddon': {
          // Not awaited: the flow asks questions and downloads, and the panel follows it through the card.
          void this.trayAddon.download();
          break;
        }
        case 'trayPage': {
          // A fixed address on the DLNE site; nothing from the message is used.
          await vscode.env.openExternal(vscode.Uri.parse(TRAY_PAGE_URL));
          break;
        }
        case 'installSuite': {
          await vscode.commands.executeCommand('dlne.installSuite');
          break;
        }
        case 'openPhoneApp': {
          await vscode.commands.executeCommand('dlne.openPhoneApp');
          break;
        }
        case 'undoMemory': {
          // The Undo next to a memory change (docs/agent-memory.md 7.6); undoMemoryChange checks what it is given.
          const undone = undoMemoryChange(data.change);
          if (undone.ok) vscode.window.showInformationMessage(`DLNE: ${undone.message}`);
          else vscode.window.showWarningMessage(`DLNE: ${undone.message}`);
          this._view?.webview.postMessage({ type: 'memoryUndone', ok: undone.ok, message: undone.message });
          break;
        }
        case 'openFile': {
          const relPath = data.path;
          const folders = vscode.workspace.workspaceFolders;
          if (folders && folders.length > 0) {
            const fullPath = path.isAbsolute(relPath) ? relPath : path.join(folders[0].uri.fsPath, relPath);
            try {
              const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(fullPath));
              await vscode.window.showTextDocument(doc, { preview: false });
            } catch (err: any) {
              vscode.window.showErrorMessage(`Could not open ${relPath}: ${err.message}`);
            }
          }
          break;
        }
      }
    });
  }

  /**
   * Runs one task, start to end, and always ends it: whatever the run does (finishes, stops, fails, throws), the
   * page is sent one `turnComplete` and the host is free for the next message. A page left "running" has only Stop
   * to press, which is what "it stops running and doesn't continue" looked like.
   */
  private async handleUserMessage(text: string) {
    if (typeof text !== 'string' || !text.trim()) return;

    // The session shown when the message was sent: the task stays its own if another session is shown meanwhile.
    const session = this.active;
    if (session.running) {
      // Starting a task stops the one before it, so a message never starts one over a live task. The page keeps Send
      // out of reach while a task runs; this is for a page that lost track of it. It is shown the task again, and gets
      // its words back to send once the task is over.
      this.syncPage();
      this.post({ type: 'refused', text });
      return;
    }
    session.running = true;
    session.stopping = false;
    let outcome: AgentRunOutcome | undefined;
    try {
      outcome = await this.runTask(session, text);
    } catch (err) {
      // A throw out of the run: a tool call that failed outside the tool's own handling, a broken callback. The task
      // is over, and the page is told why in words.
      this.emit(session, {
        type: 'error',
        error: `The task stopped because of an unexpected error: ${describeError(err).replace(/[\s.]+$/, '')}. ` +
          'Everything so far is kept: say continue to try again.',
      });
    } finally {
      session.running = false;
      session.stopping = false;
      try {
        this.saveHistory(session);
        session.store.flush();
      } catch {
        // The chat is still shown; the next save tries again.
      }
      // `paused`: the run stopped at its turn limit; the page offers a Continue button.
      this.emit(session, { type: 'turnComplete', paused: outcome === 'paused' });
      this.postSessions();
    }
  }

  /** The agent's run for one message, drawing it on the page as it goes (while its session is the one shown). */
  private runTask(session: Session, text: string): Promise<AgentRunOutcome> {
    // The new turn comes first, so whatever follows (even an error) belongs to it and not to the one before.
    const turn = { index: session.history.length, events: [] as PageEvent[] };
    session.turn = turn;

    const userMsg: ChatMessageItem = {
      id: 'msg-' + Date.now() + '-u',
      role: 'user',
      text
    };
    session.history.push(userMsg);
    // The first thing asked names the session.
    const meta = this.index.sessions.find((s) => s.id === session.id);
    if (meta && meta.title === NEW_SESSION_TITLE) meta.title = sessionTitle(text);
    this.saveHistory(session);

    this.emit(session, { type: 'appendUserMessage', text });
    this.postSessions();

    const assistantMsg: ChatMessageItem = {
      id: 'msg-' + Date.now() + '-a',
      role: 'assistant',
      text: '',
      thinking: '',
      toolCalls: []
    };
    session.history.push(assistantMsg);

    // Only this task's turn is drawn: nothing a finished turn's late callback says reaches the page.
    const show = (event: PageEvent) => {
      if (session.turn === turn) this.emit(session, event);
    };

    const maxTurns = vscode.workspace.getConfiguration('dlne').get<number>('agent.maxTurns');
    return session.loop.run(text, {
      onStateChanged: (urgent) => session.store.changed(urgent),
      onLoadingProgress: (statusText) => show({ type: 'loadingProgress', statusText }),
      // The sidebar shows the look back after the answer; VS Code's Chat view does not (see onReviewProgress).
      onReviewProgress: (statusText) => show({ type: 'loadingProgress', statusText }),
      // While every coding slot is taken: where this request stands, in a banner that stays in view. It is the state of
      // the moment (a page shown later is told it with the chat), so it is not part of what is played back.
      onQueue: (queue) => {
        if (session.turn === turn && session.id === this.index.active) this.post({ type: 'queue', queue: this._queueView(queue) });
      },
      onThinkingDelta: (delta) => {
        assistantMsg.thinking = (assistantMsg.thinking || '') + delta;
        show({ type: 'thinkingDelta', delta });
      },
      onContentDelta: (delta) => {
        assistantMsg.text += delta;
        show({ type: 'contentDelta', delta });
      },
      onToolStart: (toolName, args) => {
        if (!assistantMsg.toolCalls) assistantMsg.toolCalls = [];
        assistantMsg.toolCalls.push({ name: toolName, args });
        show({ type: 'toolStart', toolName, args });
      },
      onToolEnd: (result) => {
        if (assistantMsg.toolCalls && assistantMsg.toolCalls.length > 0) {
          assistantMsg.toolCalls[assistantMsg.toolCalls.length - 1].result = result;
        }
        this.saveHistory(session);
        show({ type: 'toolEnd', result });
      },
      // Text, not the error itself: an Error is JSON'd to nothing on its way to the page.
      onError: (error) => show({ type: 'error', error: `Error: ${describeError(error)}` })
    }, { maxTurns, reflect: memoryMode(vscode.workspace.getConfiguration('dlne').get('agent.memory', 'auto')) !== 'off' });
  }

  /**
   * Says something to the page, and notes when the page cannot have got it: the view is hidden (VS Code delivers
   * nothing to a hidden view), gone, or says it did not arrive. Such a page is shown the chat again when it is back
   * (onShown). It never throws: a view that was closed in the middle of a task must not end the task.
   */
  private post(message: PageEvent, chat = false): void {
    const view = this._view;
    const missed = () => {
      if (chat) this.chatStale = true;
    };
    if (!view) return missed();
    try {
      if (view.visible === false) missed();
      const sent: unknown = view.webview.postMessage(message);
      if (sent && typeof (sent as PromiseLike<unknown>).then === 'function') {
        (sent as PromiseLike<unknown>).then((delivered) => { if (delivered === false) missed(); }, missed);
      }
    } catch {
      missed();
    }
  }

  /**
   * Tells the page something about a session's task, and writes it down so a page that was away, or that shows the
   * session later, can be told it again. Only the session shown is drawn as it happens.
   */
  private emit(session: Session, event: PageEvent): void {
    if (session.turn) recordEvent(session.turn.events, event);
    if (session.id === this.index.active) this.post(event, true);
  }

  /**
   * What the page needs to show everything as it is now: the shown session's older chat from its saved history, its
   * latest task played back step by step (so a task in progress, a Continue button or an error line is there as it
   * was), whether that task is running, the list of sessions, and the card's state. The page draws all of it again
   * from this, so it is also how a page that VS Code made anew, or one that switched sessions, finds its way back.
   */
  private restoreMessage(): PageEvent {
    const workerStatus = this.workerManager.getStatus();
    const session = this.active;
    const turn = session.turn;
    return {
      type: 'restoreHistory',
      session: this.sessionsMessage(),
      history: historyForPage(turn ? session.history.slice(0, turn.index) : session.history),
      workerStatus,
      workerCopy: contributionCopy(workerStatus),
      workerRole: roleCopy(workerStatus),
      donate: this._donateModel(workerStatus),
      modelLine: this._modelLine(),
      // The queue is the swarm's answer to this window's latest request: it is shown with a session that is waiting.
      queue: session.running ? this._queueView(this.client.currentQueue?.()) : null,
      autoApprove: ToolRegistry.autoApproveAllOn(),
      run: { busy: session.running, stopping: session.stopping, events: turn ? turn.events : [] }
    };
  }

  /** Sends the page everything (restoreMessage). */
  private syncPage(): void {
    this.chatStale = false;
    this.post(this.restoreMessage(), true);
  }

  /**
   * The view is visible again. If the chat changed while it was away, all of it is sent again. If not, the page is
   * exactly as it was left (its scroll, its open thought drawers), and only the small state is sent: the status, the
   * model line, the Auto switch and the queue may have changed unseen.
   */
  private onShown(): void {
    if (this.chatStale) {
      this.syncPage();
      return;
    }
    const status = this.workerManager.getStatus();
    this.post({ type: 'workerStatus', status, copy: contributionCopy(status), role: roleCopy(status), donate: this._donateModel(status) });
    this.post({ type: 'modelLine', text: this._modelLine() });
    this.post({ type: 'autoApprove', on: ToolRegistry.autoApproveAllOn() });
    this.post({ type: 'queue', queue: this.active.running ? this._queueView(this.client.currentQueue?.()) : null });
    this.postSessions();
  }

  /**
   * The official DLNE logo (media/dlne_logo.png) as this webview can load it. It is the only DLNE
   * mark in the sidebar: no emoji stands in for it, and resolveWebviewView sets `_view` before
   * the page is built (the plain URI is only for building the page without a view, as tests do).
   */
  private _logoSrc(): string {
    const logo = vscode.Uri.joinPath(this._extensionUri, 'media', 'dlne_logo.png');
    return (this._view ? this._view.webview.asWebviewUri(logo) : logo).toString();
  }

  /** "DLNE Swarm (auto)", followed by the live model's name once a status answer has said it. */
  private _modelLine(): string {
    return swarmModelLine(this.client.lastKnownClusterStatus?.());
  }

  /**
   * Sends `text` as the user's message, for a command that starts a chat (DLNE: Fix Current File Problems). The caller
   * has just shown the sidebar; the page that shows the answer is there only once VS Code has loaded the view, so this
   * waits for it (at most `waitMs`) and says so if it never comes, rather than running an agent nobody can see.
   */
  async sendFromCommand(text: string, waitMs = 3000): Promise<void> {
    const until = Date.now() + waitMs;
    while (!this._view && Date.now() < until) await new Promise((resolve) => setTimeout(resolve, 50));
    if (!this._view) {
      vscode.window.showWarningMessage('DLNE: the chat panel did not open, so the message was not sent.');
      return;
    }
    await this.handleUserMessage(text);
  }

  private _getHtmlForWebview(): string {
    const logoSrc = this._logoSrc();
    const modelLine = this._modelLine()
      .replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
    const st = this.workerManager.getStatus();
    const isContrib = st.state === 'contributing';
    const isStarting = st.state === 'starting';

    const badgeClass = isContrib ? 'contributing' : isStarting ? 'starting' : 'idle';
    const badgeText = st.state.toUpperCase();
    // The status line under the header says what the running worker is doing; the Donate panel is the rest of the card.
    const live = isContrib || isStarting;
    const { ruleHtml: ruleText } = contributionCopy(st);
    const role = roleCopy(st);
    // The panel's first model sits in the script as data. Nothing in it may end the script block (a GPU name comes
    // from a driver), so the characters that could are written as escapes.
    const donateModel = JSON.stringify(this._donateModel(st))
      .replace(/</g, '\\u003c').replace(/>/g, '\\u003e').replace(/&/g, '\\u0026')
      .replace(/\u2028/g, '\\u2028').replace(/\u2029/g, '\\u2029');
    // Shown as the link's tooltip; it comes from a setting, so it is escaped for the attribute.
    const phoneUrl = phoneChatUrl(vscode.workspace.getConfiguration('dlne'))
      .replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');

    // Script runs only with this nonce and the page may load nothing but its own files: if text a coding
    // member wrote ever did become markup, it could not become a handler that reads the chat, reaches the
    // network or posts a message to the extension. (The page has no inline handlers, one script block, and
    // loads only the logo from the extension; its styles are inline.)
    const nonce = crypto.randomBytes(16).toString('base64');
    const cspSource = this._view ? this._view.webview.cspSource : '';

    return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src ${cspSource} data:; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>DLNE Code Assistant</title>
  <style>
    :root {
      --card-bg: var(--vscode-editor-background);
      --card-border: var(--vscode-widget-border, #333);
      --diff-add-bg: rgba(34, 197, 94, 0.16);
      --diff-add-text: #4ade80;
      --diff-del-bg: rgba(244, 63, 94, 0.16);
      --diff-del-text: #f87171;
    }

    body {
      font-family: var(--vscode-font-family);
      font-size: var(--vscode-font-size);
      color: var(--vscode-foreground);
      background-color: var(--vscode-sideBar-background);
      margin: 0;
      padding: 10px;
      display: flex;
      flex-direction: column;
      height: 100vh;
      box-sizing: border-box;
      gap: 10px;
    }

    /* Compute Contribution Card */
    .contrib-card {
      background-color: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 6px;
      padding: 10px 12px;
      display: flex;
      flex-direction: column;
      gap: 6px;
      flex-shrink: 0;
    }
    .contrib-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-weight: 600;
      font-size: 0.9em;
    }
    /* The DLNE logo image (the cyan-and-pink brain with DLNE under it). */
    .logo {
      width: 24px;
      height: 24px;
      border-radius: 22%;
      vertical-align: middle;
      margin-right: 6px;
    }
    .badge {
      font-size: 0.72em;
      padding: 2px 7px;
      border-radius: 4px;
      font-weight: bold;
      letter-spacing: 0.5px;
      text-transform: uppercase;
    }
    .badge.idle {
      background-color: var(--vscode-badge-background);
      color: var(--vscode-badge-foreground);
    }
    .badge.starting {
      background-color: #d7ba7d;
      color: #1e1e1e;
    }
    .badge.contributing {
      background-color: #10b981;
      color: #0f172a;
    }
    .badge.error {
      background-color: var(--vscode-errorForeground);
      color: #fff;
    }
    .contrib-info {
      font-size: 0.82em;
      color: var(--vscode-descriptionForeground);
      line-height: 1.35;
    }
    .contrib-actions {
      display: flex;
      gap: 6px;
      margin-top: 4px;
    }
    /* The card folds away to its header, so a short window can keep the chat in reach. */
    details.contrib-card > summary {
      list-style: none;
      cursor: pointer;
    }
    details.contrib-card > summary::-webkit-details-marker { display: none; }
    details.contrib-card > summary::after {
      content: '';
      width: 6px;
      height: 6px;
      margin: 0 2px 2px 8px;
      border-right: 1.5px solid var(--vscode-descriptionForeground);
      border-bottom: 1.5px solid var(--vscode-descriptionForeground);
      transform: rotate(45deg);
    }
    details.contrib-card[open] > summary::after { transform: rotate(-135deg); margin-bottom: -2px; }
    details.contrib-card > summary .contrib-title { flex: 1; }
    #contrib-rule, #role-display { margin-top: 4px; }
${DONATE_CSS}
    .contrib-card a {
      color: var(--vscode-textLink-foreground);
      cursor: pointer;
    }
    .contrib-card code {
      font-family: var(--vscode-editor-font-family, monospace);
      word-break: break-all;
    }
    .role-details summary {
      cursor: pointer;
      font-size: 0.82em;
      color: var(--vscode-textLink-foreground);
    }
    .role-details ul {
      margin: 4px 0 0;
      padding-left: 18px;
      font-size: 0.82em;
      color: var(--vscode-descriptionForeground);
      line-height: 1.35;
    }

    /* Busy: every coding slot is taken and this request is waiting its turn */
    .queue-banner {
      flex-shrink: 0;
      border: 1px solid var(--vscode-inputValidation-warningBorder, #b89500);
      background-color: var(--vscode-inputValidation-warningBackground, rgba(184, 149, 0, 0.12));
      border-radius: 6px;
      padding: 8px 12px;
      font-size: 0.86em;
      line-height: 1.4;
    }
    .queue-headline {
      font-weight: 600;
    }
    .queue-detail {
      color: var(--vscode-descriptionForeground);
      margin-top: 2px;
    }

    /* Chat Container */
    #chat-container {
      flex: 1;
      min-height: 140px;
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: 12px;
      padding-bottom: 8px;
    }
    /* The sessions (1.5.3): a header like VS Code's Chat view, and the list behind its back arrow. */
    .session-bar {
      flex-shrink: 0;
      display: flex;
      align-items: center;
      gap: 6px;
      min-height: 26px;
    }
    .session-title {
      flex: 1;
      min-width: 0;
      font-weight: 600;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    button.icon-btn {
      flex-shrink: 0;
      padding: 2px 8px;
      min-width: 26px;
      background: transparent;
      color: var(--vscode-foreground);
    }
    button.icon-btn:hover {
      background-color: var(--vscode-toolbar-hoverBackground, rgba(90, 93, 94, 0.31));
    }
    #session-list {
      flex: 1;
      min-height: 140px;
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: 2px;
    }
    .session-row {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 6px 8px;
      border-radius: 4px;
      cursor: pointer;
    }
    .session-row:hover {
      background-color: var(--vscode-list-hoverBackground);
    }
    .session-row.active {
      background-color: var(--vscode-list-inactiveSelectionBackground);
    }
    .session-name {
      flex: 1;
      min-width: 0;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .session-when {
      flex-shrink: 0;
      font-size: 0.85em;
      opacity: 0.7;
    }
    .session-running {
      flex-shrink: 0;
      font-size: 0.85em;
      color: var(--vscode-charts-green, #3fb950);
    }
    /* Shown while the chat is scrolled up: the chat no longer follows the answer until it is clicked. */
    #jump-btn {
      align-self: center;
      flex-shrink: 0;
      margin-top: -34px;
      position: relative;
      z-index: 1;
      font-size: 0.85em;
      padding: 3px 10px;
      border-radius: 12px;
      box-shadow: 0 1px 4px rgba(0, 0, 0, 0.3);
    }
    .message {
      padding: 10px 12px;
      border-radius: 6px;
      line-height: 1.45;
      word-wrap: break-word;
    }
    .user {
      background-color: var(--vscode-button-background);
      color: var(--vscode-button-foreground);
      align-self: flex-end;
      max-width: 88%;
      white-space: pre-wrap;
    }
    .assistant {
      background-color: var(--card-bg);
      border: 1px solid var(--card-border);
      align-self: flex-start;
      width: 96%;
      box-sizing: border-box;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    /* The answer as Markdown (src/views/markdown.ts makes these tags, and only these) */
    .assistant-body {
      white-space: normal;
      overflow-wrap: anywhere;
    }
    .assistant-body p { margin: 0 0 8px; }
    .assistant-body > :last-child { margin-bottom: 0; }
    .assistant-body h3, .assistant-body h4, .assistant-body h5, .assistant-body h6 {
      margin: 10px 0 6px;
      font-size: 1em;
      font-weight: 700;
    }
    .assistant-body h3 { font-size: 1.15em; }
    .assistant-body h4 { font-size: 1.05em; }
    .assistant-body ul, .assistant-body ol { margin: 0 0 8px; padding-left: 20px; }
    .assistant-body li { margin: 2px 0; }
    .assistant-body code {
      font-family: var(--vscode-editor-font-family);
      font-size: 0.92em;
      background: rgba(127, 127, 127, 0.15);
      padding: 1px 4px;
      border-radius: 3px;
    }
    .assistant-body pre {
      white-space: pre;
      overflow-x: auto;
      background: var(--vscode-textCodeBlock-background, rgba(127, 127, 127, 0.12));
      padding: 8px;
      border-radius: 4px;
      margin: 0 0 8px;
    }
    .assistant-body pre code { background: none; padding: 0; }
    .code-wrap { position: relative; }
    .copy-btn {
      position: absolute;
      top: 4px;
      right: 4px;
      padding: 1px 6px;
      font-size: 0.75em;
      opacity: 0.7;
    }
    .copy-btn:hover { opacity: 1; }
    /* Auto on: DLNE blue, the theme colour dlne.autoForeground that also colours the status bar's DLNE: Auto
       (package.json "colors"). Bright cyan with dark ink on dark themes, deep teal with white ink on light ones. */
    button.toggle[aria-pressed="true"] {
      background-color: var(--vscode-dlne-autoForeground, #00f0ff);
      color: #00363b;
    }
    body.vscode-light button.toggle[aria-pressed="true"],
    body.vscode-high-contrast-light button.toggle[aria-pressed="true"] {
      color: #ffffff;
    }
    /* Under the mouse it stays blue: the secondary button's hover background is as specific and comes later, and with
       the fixed ink it was unreadable (1.5:1) right after the click (review). */
    button.toggle[aria-pressed="true"]:hover {
      background-color: var(--vscode-dlne-autoForeground, #00f0ff);
      filter: brightness(1.1);
    }
    .assistant-body blockquote {
      margin: 0 0 8px;
      padding-left: 8px;
      border-left: 3px solid var(--card-border);
      color: var(--vscode-descriptionForeground);
    }
    .assistant-body hr { border: none; border-top: 1px solid var(--card-border); margin: 8px 0; }

    /* Claude Code / Antigravity Style Thinking Drawer */
    .thinking-drawer {
      margin-bottom: 4px;
      padding: 7px 10px;
      background-color: rgba(255, 255, 255, 0.03);
      border-left: 3px solid #8b5cf6;
      border-radius: 4px;
      font-size: 0.88em;
      color: var(--vscode-descriptionForeground);
    }
    .thinking-drawer summary {
      cursor: pointer;
      font-weight: 600;
      display: flex;
      justify-content: space-between;
      align-items: center;
      user-select: none;
    }
    .thinking-badge {
      font-size: 0.78em;
      color: #a78bfa;
      font-weight: normal;
    }
    .thinking-content {
      margin-top: 6px;
      white-space: pre-wrap;
      font-family: var(--vscode-editor-font-family, monospace);
      font-size: 0.9em;
      line-height: 1.4;
      color: var(--vscode-foreground);
      opacity: 0.9;
      max-height: 240px;
      overflow-y: auto;
    }

    /* Claude Code / Antigravity Style File Modification Card */
    .file-card {
      background-color: var(--vscode-editor-background);
      border: 1px solid var(--card-border);
      border-radius: 6px;
      overflow: hidden;
      margin: 4px 0;
    }
    .file-card-header {
      background-color: rgba(255, 255, 255, 0.04);
      padding: 6px 10px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 0.86em;
      border-bottom: 1px solid var(--card-border);
    }
    .file-path {
      font-weight: 600;
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .diff-counts {
      display: flex;
      gap: 4px;
      font-size: 0.8em;
      font-family: monospace;
      font-weight: bold;
    }
    .diff-badge-add {
      color: #4ade80;
      background-color: var(--diff-add-bg);
      padding: 1px 5px;
      border-radius: 3px;
    }
    .diff-badge-del {
      color: #f87171;
      background-color: var(--diff-del-bg);
      padding: 1px 5px;
      border-radius: 3px;
    }
    .file-card-actions {
      display: flex;
      gap: 6px;
    }
    .btn-file-open {
      padding: 2px 8px;
      font-size: 0.8em;
      background-color: var(--vscode-button-secondaryBackground);
      color: var(--vscode-button-secondaryForeground);
      border-radius: 3px;
      border: none;
      cursor: pointer;
    }
    .btn-file-open:hover {
      background-color: var(--vscode-button-secondaryHoverBackground);
    }

    /* Unified Diff View */
    .diff-box {
      font-family: var(--vscode-editor-font-family, monospace);
      font-size: 0.82em;
      line-height: 1.45;
      overflow-x: auto;
      max-height: 240px;
      padding: 4px 0;
      background-color: #121216;
    }
    .diff-line {
      display: flex;
      padding: 1px 8px;
      white-space: pre-wrap;
      word-break: break-all;
    }
    .diff-line.hdr {
      color: #38bdf8;
      background-color: rgba(56, 189, 248, 0.08);
      font-style: italic;
    }
    .diff-line.add {
      background-color: var(--diff-add-bg);
      color: var(--diff-add-text);
    }
    .diff-line.del {
      background-color: var(--diff-del-bg);
      color: var(--diff-del-text);
    }
    .diff-line.ctx {
      color: var(--vscode-foreground);
      opacity: 0.75;
    }

    /* Terminal Execution Card */
    .terminal-card {
      background-color: #0e1117;
      border: 1px solid var(--card-border);
      border-radius: 5px;
      padding: 6px 10px;
      font-size: 0.84em;
      font-family: var(--vscode-editor-font-family, monospace);
      margin: 4px 0;
    }
    .terminal-cmd {
      color: #38bdf8;
      font-weight: bold;
    }
    .terminal-out {
      margin-top: 4px;
      color: #94a3b8;
      white-space: pre-wrap;
      max-height: 140px;
      overflow-y: auto;
    }

    /* Tool Call Active Pill */
    .tool-pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      margin: 4px 0;
      padding: 3px 8px;
      font-size: 0.82em;
      border-radius: 4px;
      background-color: var(--vscode-badge-background);
      color: var(--vscode-badge-foreground);
    }

    #input-container {
      display: flex;
      flex-direction: column;
      gap: 8px;
      flex-shrink: 0;
    }
    textarea {
      width: 100%;
      box-sizing: border-box;
      resize: vertical;
      background-color: var(--vscode-input-background);
      color: var(--vscode-input-foreground);
      border: 1px solid var(--vscode-input-border);
      padding: 8px;
      border-radius: 4px;
      font-family: inherit;
      font-size: inherit;
    }
    /* Shown when Enter or Continue is pressed while a task runs: nothing is sent, and the words stay in the box. */
    .busy-hint {
      font-size: 0.82em;
      line-height: 1.35;
      color: var(--vscode-descriptionForeground);
    }
    /* Stop was pressed: "Stopping..." cannot be pressed again until the task has ended. */
    #stop-btn:disabled {
      opacity: 0.6;
      cursor: default;
    }
    .btn-row {
      display: flex;
      gap: 8px;
      align-items: center;
      justify-content: space-between;
    }
    .btn-group-right {
      display: flex;
      gap: 6px;
      align-items: center;
    }
    .model-line {
      flex: 1;
      min-width: 0;
      max-width: 240px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      color: var(--vscode-descriptionForeground);
      font-size: 0.84em;
    }
    button {
      padding: 6px 12px;
      background-color: var(--vscode-button-background, #0e639c);
      color: var(--vscode-button-foreground, #fff);
      border: 1px solid var(--vscode-button-border, transparent);
      border-radius: 4px;
      cursor: pointer;
      font-size: 0.88em;
    }
    button:hover {
      background-color: var(--vscode-button-hoverBackground, #1177bb);
    }
    /* Fallbacks: a theme that leaves the secondary colours out would draw these as bare text. */
    button.secondary {
      background-color: var(--vscode-button-secondaryBackground, #3a3d41);
      color: var(--vscode-button-secondaryForeground, #fff);
      border-color: var(--vscode-button-border, var(--vscode-widget-border, #5a5d62));
    }
    button.secondary:hover {
      background-color: var(--vscode-button-secondaryHoverBackground, #45494e);
    }
    button.contrib-btn {
      flex: 1;
      font-weight: 600;
    }
    button.stop-btn {
      background-color: #f43f5e;
      color: #fff;
    }
    button.continue-btn {
      display: block;
      margin-top: 8px;
    }
    button.undo-btn {
      margin-left: 8px;
      padding: 1px 8px;
      font-size: 0.8em;
    }

    /* About */
    .about {
      flex-shrink: 0;
      font-size: 0.82em;
      color: var(--vscode-descriptionForeground);
    }
    .about a {
      color: var(--vscode-textLink-foreground);
      cursor: pointer;
    }
  </style>
</head>
<body>
  <!-- Donate compute: the panel is drawn by the script from the model in worker/donatePanel.ts -->
  <details class="contrib-card" id="contrib-card" open>
    <summary class="contrib-header">
      <span class="contrib-title"><img class="logo" src="${logoSrc}" alt="" width="24" height="24">Donate compute</span>
      <span class="badge ${badgeClass}" id="worker-badge">${badgeText}</span>
    </summary>
    <div class="dn" id="donate-panel"></div>
    <div class="contrib-info" id="contrib-rule"${live ? '' : ' style="display:none"'}>${live ? ruleText : ''}</div>
    <div class="contrib-info" id="role-display">${role.roleHtml}</div>
    <div class="contrib-info" id="trust-display"${role.trustHtml ? '' : ' style="display:none"'}>${role.trustHtml}</div>
    <div id="role-facts">${role.factsHtml}</div>
    <div class="contrib-actions">
      <button class="secondary" id="suite-btn" title="Install the signed DLNE compute engine">Set Up Engine</button>
    </div>
  </details>

  <div class="session-bar" id="session-bar">
    <button class="secondary icon-btn" id="sessions-btn" title="All chats" aria-label="All chats">&#8592;</button>
    <span class="session-title" id="session-title">New chat</span>
    <button class="secondary icon-btn" id="new-session-btn" title="New chat" aria-label="New chat">+</button>
  </div>

  <div id="session-list" role="list" style="display:none"></div>

  <div id="chat-container">
    <div class="message assistant" id="welcome-msg">
      <strong><img class="logo" src="${logoSrc}" alt="" width="24" height="24">DLNE Code Assistant</strong>
      <div class="assistant-body">Connected to cluster. Ready for autonomous coding, refactoring, and test verification.</div>
    </div>
  </div>
  <button class="secondary" id="jump-btn" style="display:none" title="Scroll to the latest">&#8595; Latest</button>

  <div class="queue-banner" id="queue-banner" role="status" aria-live="polite" style="display:none">
    <div class="queue-headline" id="queue-headline"></div>
    <div class="queue-detail" id="queue-detail"></div>
  </div>

  <div id="input-container">
    <textarea id="prompt-input" rows="3" placeholder="Ask DLNE to write code, edit files, or fix bugs..."></textarea>
    <div class="busy-hint" id="busy-hint" role="status" aria-live="polite" style="display:none">DLNE is still working. Press Stop to interrupt it, or wait for it to finish. Your message is kept in the box.</div>
    <div class="btn-row">
      <span class="model-line" id="model-line" title="The swarm picks the model from the GPUs available; there is nothing to choose.">${modelLine}</span>
      <div class="btn-group-right">
        <button class="secondary toggle" id="auto-btn" aria-pressed="false" title="Off: the agent asks before it edits files or runs a command. On: every action runs without asking (edits anywhere, every command), from its next step, until you turn it off. The same switch as the status bar's DLNE: Auto/Ask and the setting dlne.autoApproveAll. The agent's next step can come from a volunteer's machine.">Auto: off</button>
        <button class="secondary" id="clear-btn" title="Start a new chat; this one stays in the list behind the back arrow">New chat</button>
        <button class="secondary" id="stop-btn" style="display:none;">Stop</button>
        <button id="send-btn">Send</button>
      </div>
    </div>
  </div>

  <div class="about">
    <a href="#" id="phone-link" title="${phoneUrl}">📱 Use DLNE on your phone</a>
  </div>

  <script nonce="${nonce}">
    const vscode = acquireVsCodeApi();
    const chatContainer = document.getElementById('chat-container');
    const promptInput = document.getElementById('prompt-input');
    const sendBtn = document.getElementById('send-btn');
    const stopBtn = document.getElementById('stop-btn');
    const clearBtn = document.getElementById('clear-btn');
    const autoBtn = document.getElementById('auto-btn');
    const busyHint = document.getElementById('busy-hint');
    const jumpBtn = document.getElementById('jump-btn');
    const inputContainer = document.getElementById('input-container');
    const sessionsBtn = document.getElementById('sessions-btn');
    const newSessionBtn = document.getElementById('new-session-btn');
    const sessionTitleEl = document.getElementById('session-title');
    const sessionList = document.getElementById('session-list');

    // The chat follows the answer only while it is scrolled to the bottom (1.5.3). Scrolled up to read something, it
    // stays where it is while the task goes on, and "Latest" brings it back down; scrolling to the bottom does too.
    let followLatest = true;
    function atBottom() {
      return chatContainer.scrollHeight - chatContainer.scrollTop - chatContainer.clientHeight < 40;
    }
    chatContainer.addEventListener('scroll', () => {
      followLatest = atBottom();
      if (followLatest) jumpBtn.style.display = 'none';
    });
    // Called whenever the chat grew. \`force\`: the user's own message, or a chat drawn anew, always shows its end.
    function keepUp(force) {
      if (force) followLatest = true;
      if (followLatest) {
        chatContainer.scrollTop = chatContainer.scrollHeight;
        jumpBtn.style.display = 'none';
      } else if (!listShown) {
        jumpBtn.style.display = 'inline-block';
      }
    }
    jumpBtn.addEventListener('click', () => keepUp(true));

    // ---- sessions (1.5.3): the header names the chat shown; its back arrow lists them all, newest first -----------
    let currentSessionId = null;
    let listShown = false;
    function showList(on) {
      listShown = on;
      sessionList.style.display = on ? 'flex' : 'none';
      chatContainer.style.display = on ? 'none' : 'flex';
      inputContainer.style.display = on ? 'none' : '';
      jumpBtn.style.display = 'none';
      sessionsBtn.title = on ? 'Back to the chat' : 'All chats';
      sessionsBtn.setAttribute('aria-label', sessionsBtn.title);
      if (!on) keepUp(false);
    }
    function whenText(ms) {
      const minutes = Math.round((Date.now() - ms) / 60000);
      if (!(minutes >= 1)) return 'now';
      if (minutes < 60) return minutes + ' min';
      const hours = Math.round(minutes / 60);
      if (hours < 24) return hours + ' h';
      return Math.round(hours / 24) + ' d';
    }
    // Every title is drawn as text: it is the user's own first words, never markup.
    function applySessions(info) {
      if (!info) return;
      sessionTitleEl.textContent = typeof info.title === 'string' ? info.title : 'New chat';
      sessionList.innerHTML = '';
      (Array.isArray(info.list) ? info.list : []).forEach((row) => {
        const item = document.createElement('div');
        item.className = 'session-row' + (row.active ? ' active' : '');
        item.setAttribute('role', 'listitem');
        const name = document.createElement('span');
        name.className = 'session-name';
        name.textContent = String(row.title);
        item.appendChild(name);
        if (row.running) {
          const live = document.createElement('span');
          live.className = 'session-running';
          live.textContent = '● working';
          item.appendChild(live);
        }
        const when = document.createElement('span');
        when.className = 'session-when';
        when.textContent = whenText(Number(row.updated));
        item.appendChild(when);
        item.addEventListener('click', () => {
          showList(false);
          vscode.postMessage({ type: 'openSession', id: row.id });
        });
        // A running chat cannot be deleted: Stop it first.
        if (!row.running) {
          const del = document.createElement('button');
          del.className = 'secondary icon-btn session-delete';
          del.title = 'Delete this chat';
          del.textContent = '🗑';
          del.addEventListener('click', (e) => {
            if (e && e.stopPropagation) e.stopPropagation();
            vscode.postMessage({ type: 'deleteSession', id: row.id });
          });
          item.appendChild(del);
        }
        sessionList.appendChild(item);
      });
    }
    sessionsBtn.addEventListener('click', () => showList(!listShown));
    function startNewChat() {
      showList(false);
      vscode.postMessage({ type: 'newSession' });
    }
    newSessionBtn.addEventListener('click', startNewChat);

    function showAutoApprove(on) {
      autoBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
      autoBtn.textContent = on ? 'Auto: on' : 'Auto: off';
    }
    autoBtn.addEventListener('click', () => {
      vscode.postMessage({ type: 'setAutoApprove', on: autoBtn.getAttribute('aria-pressed') !== 'true' });
    });
    const modelLine = document.getElementById('model-line');
    const suiteBtn = document.getElementById('suite-btn');
    const workerBadge = document.getElementById('worker-badge');
    const contribCard = document.getElementById('contrib-card');
    const contribRule = document.getElementById('contrib-rule');
    const roleDisplay = document.getElementById('role-display');
    const trustDisplay = document.getElementById('trust-display');
    const roleFacts = document.getElementById('role-facts');
    const queueBanner = document.getElementById('queue-banner');
    const queueHeadline = document.getElementById('queue-headline');
    const queueDetail = document.getElementById('queue-detail');

    // Where this request stands while every coding slot is taken; null takes the banner down.
    function showQueue(queue) {
      if (!queue) {
        queueBanner.style.display = 'none';
        return;
      }
      queueHeadline.textContent = queue.headline;
      queueDetail.textContent = queue.detail;
      queueBanner.style.display = 'block';
    }

    let currentAssistantMsg = null;
    let currentThinkingElem = null;
    let currentThinkingBadge = null;
    let currentContentElem = null;
    let currentContentRaw = '';     // the answer as the model wrote it; currentContentElem shows it as Markdown
    let renderTimer = null;
    let activePillElem = null;
    // A task is running, as the extension says (the page only guesses between a click and the extension's answer).
    // While it is, Send is out of reach and Enter or Continue send nothing: a message sent over a running task would
    // stop it, so the one task finishes (or Stop ends it) before the next begins.
    let busy = false;
    // A re-sync is drawing the task again: nothing is saved until it has finished.
    let replaying = false;
    // The Continue button that can still be pressed (only the latest task's), and the Undo buttons awaiting an answer.
    let continueBtn = null;
    let pendingUndo = [];

    function setBusy(on, stopping) {
      busy = on;
      sendBtn.style.display = on ? 'none' : 'inline-block';
      stopBtn.style.display = on ? 'inline-block' : 'none';
      // Stop was pressed: the task ends when its step does, and that is what the button says until it has.
      stopBtn.disabled = on && stopping === true;
      stopBtn.textContent = on && stopping === true ? 'Stopping...' : 'Stop';
      if (!on) busyHint.style.display = 'none';
    }

    // Forget the bubble that is being filled: a re-sync or a Clear starts from nothing.
    function resetRun() {
      if (renderTimer) {
        clearTimeout(renderTimer);
        renderTimer = null;
      }
      currentAssistantMsg = null;
      currentThinkingElem = null;
      currentThinkingBadge = null;
      currentContentElem = null;
      currentContentRaw = '';
      activePillElem = null;
      continueBtn = null;
      pendingUndo = [];
    }

    // Sends a message to the agent, unless a task is running: then nothing is sent, and the words stay where they are.
    function sendText(text) {
      if (busy) {
        busyHint.style.display = 'block';
        return false;
      }
      setBusy(true);
      vscode.postMessage({ type: 'sendMessage', text });
      return true;
    }

    // The agent's answer is shown as Markdown. renderMarkdown (src/views/markdown.ts, this same function) escapes
    // everything the model wrote before it reads any Markdown, so the text can never become markup of its own.
    ${renderMarkdown.toString()}

    // Every code block in an answer gets a small Copy button; the text goes through VS Code's clipboard.
    function addCopyButtons(root) {
      root.querySelectorAll('pre').forEach((pre) => {
        if (pre.parentElement.classList.contains('code-wrap')) return;
        const wrap = document.createElement('div');
        wrap.className = 'code-wrap';
        pre.parentNode.insertBefore(wrap, pre);
        wrap.appendChild(pre);
        const btn = document.createElement('button');
        btn.className = 'secondary copy-btn';
        btn.title = 'Copy to clipboard';
        btn.textContent = 'Copy';
        wrap.appendChild(btn);
      });
    }

    function renderCurrentAnswer() {
      if (renderTimer) {
        clearTimeout(renderTimer);
        renderTimer = null;
      }
      if (currentContentElem) {
        currentContentElem.innerHTML = renderMarkdown(currentContentRaw);
        addCopyButtons(currentContentElem);
        keepUp();
      }
    }

    // The answer block is done (a tool ran, or the turn ended): show its last words and start a new one next time.
    function finishAnswerBlock() {
      renderCurrentAnswer();
      currentContentElem = null;
      currentContentRaw = '';
    }

    function removePill() {
      if (activePillElem) {
        activePillElem.remove();
        activePillElem = null;
      }
    }

    // Restore state from webview cache on instant reload
    const previousState = vscode.getState();
    if (previousState && previousState.chatHtml) {
      chatContainer.innerHTML = previousState.chatHtml;
    }
    if (previousState && previousState.donateOpen === false) {
      contribCard.open = false;
    }
    // The session that chat was (the extension's account, restoreHistory, follows at once and says which is shown).
    if (previousState && typeof previousState.sessionId === 'string') {
      currentSessionId = previousState.sessionId;
    }
    if (previousState && typeof previousState.sessionTitle === 'string') {
      sessionTitleEl.textContent = previousState.sessionTitle;
    }

    function saveWebviewState() {
      if (replaying) return;
      vscode.setState({
        chatHtml: chatContainer.innerHTML, donateOpen: contribCard.open,
        sessionId: currentSessionId, sessionTitle: sessionTitleEl.textContent,
      });
    }
    contribCard.addEventListener('toggle', saveWebviewState);

    // The Donate panel (src/views/donateView.ts, this same function) draws the model the extension builds from the
    // machine and the settings. It shows every string as text, and the extension checks every value it posts.
    ${createDonatePanel.toString()}
    const donatePanel = createDonatePanel(document.getElementById('donate-panel'), (message) => vscode.postMessage(message));
    donatePanel.update(${donateModel});

    suiteBtn.addEventListener('click', () => {
      vscode.postMessage({ type: 'installSuite' });
    });

    document.getElementById('phone-link').addEventListener('click', (e) => {
      e.preventDefault();
      vscode.postMessage({ type: 'openPhoneApp' });
    });

    trustDisplay.addEventListener('click', (e) => {
      const target = e.target;
      if (target && target.classList && target.classList.contains('copy-trust')) {
        e.preventDefault();
        vscode.postMessage({ type: 'copyTrustId', id: target.getAttribute('data-id') });
      }
    });

    // Enter calls this too, even while Send is hidden: the busy check is here, not in the button's looks.
    sendBtn.addEventListener('click', () => {
      const text = promptInput.value.trim();
      if (!text) return;
      if (sendText(text)) promptInput.value = '';
    });

    stopBtn.addEventListener('click', () => {
      // "Stopping" until the extension says the task has ended, so a message cannot start a second task over it.
      setBusy(true, true);
      vscode.postMessage({ type: 'abortTask' });
    });

    // New chat (it was Clear before 1.5.3): the chat shown stays in the list, a task in it keeps running, and the
    // extension answers with the new, empty chat (restoreHistory).
    clearBtn.addEventListener('click', startNewChat);

    promptInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendBtn.click();
      }
    });

    // Handle delegated click for Open File buttons in diff cards
    chatContainer.addEventListener('click', (e) => {
      const target = e.target;
      if (target && target.classList.contains('copy-btn')) {
        const code = target.parentElement.querySelector('code');
        vscode.postMessage({ type: 'copyCode', text: code ? code.textContent : '' });
        target.textContent = 'Copied';
        setTimeout(() => { target.textContent = 'Copy'; }, 1500);
        return;
      }
      if (target && target.classList.contains('btn-file-open')) {
        const filePath = target.getAttribute('data-path');
        if (filePath) {
          vscode.postMessage({ type: 'openFile', path: filePath });
        }
      }
    });

    function renderDiffCard(parentElem, diff) {
      const card = document.createElement('div');
      card.className = 'file-card';

      const header = document.createElement('div');
      header.className = 'file-card-header';

      const pathElem = document.createElement('div');
      pathElem.className = 'file-path';
      pathElem.textContent = '📄 ' + diff.path;

      const counts = document.createElement('div');
      counts.className = 'diff-counts';
      if (diff.additions > 0) {
        counts.innerHTML += '<span class="diff-badge-add">+' + diff.additions + '</span>';
      }
      if (diff.deletions > 0) {
        counts.innerHTML += '<span class="diff-badge-del">-' + diff.deletions + '</span>';
      }
      pathElem.appendChild(counts);

      const actions = document.createElement('div');
      actions.className = 'file-card-actions';
      const openBtn = document.createElement('button');
      openBtn.className = 'btn-file-open';
      openBtn.textContent = '📂 Open File';
      openBtn.setAttribute('data-path', diff.path);
      actions.appendChild(openBtn);

      header.appendChild(pathElem);
      header.appendChild(actions);
      card.appendChild(header);

      if (diff.lines && diff.lines.length > 0) {
        const diffBox = document.createElement('div');
        diffBox.className = 'diff-box';
        diff.lines.forEach((l) => {
          const row = document.createElement('div');
          row.className = 'diff-line ' + l.type;
          row.textContent = l.text;
          diffBox.appendChild(row);
        });
        card.appendChild(diffBox);
      }

      parentElem.appendChild(card);
    }

    function renderTerminalCard(parentElem, cmd, output) {
      const card = document.createElement('div');
      card.className = 'terminal-card';
      const cmdLine = document.createElement('div');
      cmdLine.className = 'terminal-cmd';
      cmdLine.textContent = '⚡ Terminal: ' + cmd;          // the command is the model's, the output may be a repository's
      const out = document.createElement('div');
      out.className = 'terminal-out';
      out.textContent = output || '';
      card.appendChild(cmdLine);
      card.appendChild(out);
      parentElem.appendChild(card);
    }

    // The wording comes from the extension (contributionCopy), so the first render and these
    // updates say the same thing, for a GPU contributor and for the CPU pool alike.
    function applyWorkerStatus(st, copy, role, donate) {
      if (!st || !copy) return;
      workerBadge.className = 'badge ' + st.state;
      workerBadge.textContent = st.state.toUpperCase();
      // Only a worker that is starting or running has anything to report here; the panel says the rest.
      const live = st.state === 'contributing' || st.state === 'starting';
      contribRule.innerHTML = live ? copy.ruleHtml : '';
      contribRule.style.display = live ? '' : 'none';
      if (donate) donatePanel.update(donate);
      if (copy.engineButton) {
        suiteBtn.textContent = copy.engineButton.text;
        suiteBtn.title = copy.engineButton.title;
      }
      if (role) {
        roleDisplay.innerHTML = role.roleHtml;
        trustDisplay.innerHTML = role.trustHtml;
        trustDisplay.style.display = role.trustHtml ? '' : 'none';
        // The details stay open or shut as the user left them while the card updates around them.
        const wasOpen = roleFacts.querySelector('details') && roleFacts.querySelector('details').open;
        roleFacts.innerHTML = role.factsHtml;
        if (wasOpen && roleFacts.querySelector('details')) roleFacts.querySelector('details').open = true;
      }
    }

    // One saved turn of the chat, drawn as it was kept (no status lines, no buttons: those belong to a live task).
    function renderHistoryItem(item) {
      if (item.role === 'user') {
        const div = document.createElement('div');
        div.className = 'message user';
        div.textContent = item.text;
        chatContainer.appendChild(div);
      } else {
        const asstDiv = document.createElement('div');
        asstDiv.className = 'message assistant';

        if (item.thinking) {
          const details = document.createElement('details');
          details.className = 'thinking-drawer';
          const summary = document.createElement('summary');
          summary.innerHTML = '<span>Thought Process</span> <span class="thinking-badge">Complete</span>';
          const thinking = document.createElement('div');
          thinking.className = 'thinking-content';
          thinking.textContent = item.thinking;      // what the model wrote is text, never markup
          details.appendChild(summary);
          details.appendChild(thinking);
          asstDiv.appendChild(details);
        }

        if (item.toolCalls && item.toolCalls.length > 0) {
          item.toolCalls.forEach((tc) => {
            if (tc.result && tc.result.fileDiff) {
              renderDiffCard(asstDiv, tc.result.fileDiff);
            } else if (tc.result && tc.result.terminalCommand) {
              renderTerminalCard(asstDiv, tc.result.terminalCommand, tc.result.output);
            }
          });
        }

        if (item.text) {
          const body = document.createElement('div');
          body.className = 'assistant-body';
          body.innerHTML = renderMarkdown(item.text);   // escaped first, then Markdown (markdown.ts)
          addCopyButtons(body);
          asstDiv.appendChild(body);
        }
        chatContainer.appendChild(asstDiv);
      }
    }

    function dispatchMessage(msg) {
      switch (msg.type) {
        case 'workerStatus': {
          applyWorkerStatus(msg.status, msg.copy, msg.role, msg.donate);
          break;
        }

        case 'donatePanel': {
          donatePanel.update(msg.model);
          break;
        }

        case 'queue': {
          showQueue(msg.queue);
          break;
        }

        case 'modelLine': {
          modelLine.textContent = msg.text;
          break;
        }

        case 'autoApprove': {
          showAutoApprove(msg.on === true);
          break;
        }

        case 'restoreHistory': {
          if (msg.modelLine) {
            modelLine.textContent = msg.modelLine;
          }
          showAutoApprove(msg.autoApprove === true);
          if (msg.workerStatus) {
            applyWorkerStatus(msg.workerStatus, msg.workerCopy, msg.workerRole, msg.donate);
          }
          showQueue(msg.queue);
          // The extension's whole account of the chat: older turns from the saved history, the latest one played back
          // step by step (run.events), so a task in progress, a Continue button or an error line is there as it was.
          // It replaces whatever this page was showing: the page may have missed part of it while it was hidden. Another
          // session than the one drawn (1.5.3: a New chat, a chat picked from the list) is drawn even when it is empty.
          const run = msg.run || {};
          const events = Array.isArray(run.events) ? run.events : [];
          const session = msg.session || null;
          const switched = !!session && session.active !== currentSessionId;
          if (session) {
            applySessions(session);
            currentSessionId = session.active;
          }
          if (switched || (msg.history && msg.history.length > 0) || events.length > 0) {
            resetRun();
            chatContainer.innerHTML = '';
            (msg.history || []).forEach(renderHistoryItem);
            replaying = true;
            try {
              events.forEach((event) => handleMessage(event));
            } finally {
              replaying = false;
            }
            keepUp(true);
          }
          if (switched && listShown) showList(false);
          saveWebviewState();
          setBusy(run.busy === true, run.stopping === true);
          break;
        }

        case 'sessions': {
          // The list and the shown chat's title changed (a title set, a task started or ended, a chat deleted).
          applySessions(msg);
          saveWebviewState();
          break;
        }

        case 'appendUserMessage': {
          // A new task: the Continue button of the one before is no longer the way on.
          if (continueBtn) {
            continueBtn.remove();
            continueBtn = null;
          }
          const div = document.createElement('div');
          div.className = 'message user';
          div.textContent = msg.text;
          chatContainer.appendChild(div);
          keepUp(true);     // your own message: the chat shows it, and follows the answer from there

          currentAssistantMsg = document.createElement('div');
          currentAssistantMsg.className = 'message assistant';
          chatContainer.appendChild(currentAssistantMsg);

          currentThinkingElem = null;
          currentThinkingBadge = null;
          currentContentElem = null;
          currentContentRaw = '';
          activePillElem = null;
          // A task has begun, whether this page sent the message or had to be told (a re-sync, a message sent
          // from a page that was behind): Stop shows, Send is out of reach.
          setBusy(true);
          saveWebviewState();
          break;
        }

        case 'loadingProgress': {
          if (currentAssistantMsg) {
            if (!activePillElem) {
              activePillElem = document.createElement('div');
              activePillElem.className = 'tool-pill';
              currentAssistantMsg.appendChild(activePillElem);
            }
            activePillElem.textContent = msg.statusText;
            keepUp();
          }
          break;
        }

        case 'thinkingDelta': {
          if (activePillElem) {
            activePillElem.remove();
            activePillElem = null;
          }
          if (!currentThinkingElem && currentAssistantMsg) {
            const details = document.createElement('details');
            details.className = 'thinking-drawer';
            details.open = true;

            const summary = document.createElement('summary');
            summary.innerHTML = '<span>Thought Process</span>';

            currentThinkingBadge = document.createElement('span');
            currentThinkingBadge.className = 'thinking-badge';
            currentThinkingBadge.textContent = '⚡ Reasoning...';
            summary.appendChild(currentThinkingBadge);

            const content = document.createElement('div');
            content.className = 'thinking-content';

            details.appendChild(summary);
            details.appendChild(content);
            currentAssistantMsg.appendChild(details);
            currentThinkingElem = content;
          }
          if (currentThinkingElem) {
            currentThinkingElem.textContent += msg.delta;
            keepUp();
          }
          break;
        }

        case 'contentDelta': {
          removePill();
          if (!currentContentElem && currentAssistantMsg) {
            currentContentElem = document.createElement('div');
            currentContentElem.className = 'assistant-body';
            currentAssistantMsg.appendChild(currentContentElem);
            currentContentRaw = '';
          }
          if (currentContentElem) {
            currentContentRaw += msg.delta;
            // Re-rendered at most every 50 ms while words arrive, not once per word.
            if (!renderTimer) renderTimer = setTimeout(renderCurrentAnswer, 50);
          }
          break;
        }

        case 'toolStart': {
          if (currentContentElem) {
            const fencedToolPattern = new RegExp('\\x60\\x60\\x60(?:json|tool_call)?\\\\s*\\\\{\\\\s*"name"\\\\s*:[\\\\s\\\\S]*?\\x60\\x60\\x60\\\\s*$', 'g');
            const xmlToolPattern = new RegExp('<tool_call[\\\\s\\\\S]*?<\\\\/tool_call>\\\\s*$', 'g');
            currentContentRaw = currentContentRaw
              .replace(fencedToolPattern, '')
              .replace(xmlToolPattern, '')
              .trim();
            if (!currentContentRaw) {
              if (renderTimer) {
                clearTimeout(renderTimer);
                renderTimer = null;
              }
              currentContentElem.remove();
              currentContentElem = null;
            } else {
              renderCurrentAnswer();
            }
          }
          // One status line at a time: the "waiting" or "writing" line before this tool gives way to it.
          removePill();
          if (currentAssistantMsg) {
            activePillElem = document.createElement('div');
            activePillElem.className = 'tool-pill';
            activePillElem.textContent = '🔧 Executing ' + msg.toolName + '...';
            currentAssistantMsg.appendChild(activePillElem);
            keepUp();
          }
          break;
        }

        case 'toolEnd': {
          removePill();
          finishAnswerBlock();
          const res = msg.result;
          if (res && res.fileDiff && currentAssistantMsg) {
            renderDiffCard(currentAssistantMsg, res.fileDiff);
          } else if (res && res.terminalCommand && currentAssistantMsg) {
            renderTerminalCard(currentAssistantMsg, res.terminalCommand, res.output);
          } else if (res && currentAssistantMsg) {
            const pill = document.createElement('div');
            pill.className = 'tool-pill';
            pill.textContent = '✅ ' + res.name + ': ' + (res.output.slice(0, 100));
            currentAssistantMsg.appendChild(pill);
            // A memory change is made at once, so it comes with a way back.
            if (res.memoryChange) {
              const change = res.memoryChange;
              const undoBtn = document.createElement('button');
              undoBtn.className = 'secondary undo-btn';
              undoBtn.textContent = '↩ Undo';
              undoBtn.addEventListener('click', () => {
                undoBtn.disabled = true;
                // The extension answers each Undo in turn (memoryUndone); the button it was pressed on says how it went.
                pendingUndo.push(undoBtn);
                vscode.postMessage({ type: 'undoMemory', change });
              });
              pill.appendChild(undoBtn);
            }
          }
          keepUp();
          saveWebviewState();
          break;
        }

        case 'turnComplete':
        case 'taskAborted': {
          showQueue(null);
          if (currentThinkingBadge) {
            currentThinkingBadge.textContent = 'Complete';
          }
          removePill();
          finishAnswerBlock();
          // Stopped at the turn limit: one click sends "continue", and the agent carries on with its history.
          if (msg.type === 'turnComplete' && msg.paused && currentAssistantMsg) {
            const button = document.createElement('button');
            button.className = 'continue-btn';
            button.textContent = '▶ Continue';
            button.addEventListener('click', () => {
              // Sent as it is, without going through the box: whatever the user is typing there stays. While a task
              // is running (this button is from an earlier one) nothing is sent.
              if (!sendText('continue')) return;
              button.remove();
              if (continueBtn === button) continueBtn = null;
            });
            continueBtn = button;
            currentAssistantMsg.appendChild(button);
            keepUp();
          }
          // The end of the task, and the only thing that frees Send again.
          setBusy(false);
          saveWebviewState();
          break;
        }

        case 'error': {
          showQueue(null);
          removePill();
          const div = document.createElement('div');
          div.className = 'message assistant';
          div.style.color = 'var(--vscode-errorForeground)';
          // The extension sends the words. (An Error object does not survive the trip to the page, and
          // "Error: [object Object]" is what this line used to say.) The task's end follows as a turnComplete.
          div.textContent = typeof msg.error === 'string' && msg.error ? msg.error : 'Error: something went wrong.';
          chatContainer.appendChild(div);
          saveWebviewState();
          break;
        }

        case 'stopping': {
          setBusy(true, true);
          break;
        }

        case 'refused': {
          // The extension is still running a task this page did not know about, and did not start the message: the
          // words go back in the box (unless something else is typed there now), and the hint says why.
          if (!promptInput.value && typeof msg.text === 'string') promptInput.value = msg.text;
          busyHint.style.display = 'block';
          break;
        }

        case 'cleared': {
          // The extension has cleared its side too (the page cleared itself when Clear was pressed): nothing is left
          // of the chat or the task, and a task that was running ends when its step does.
          resetRun();
          chatContainer.innerHTML = '';
          showQueue(null);
          if (busy) setBusy(true, true);
          saveWebviewState();
          break;
        }

        case 'memoryUndone': {
          const button = pendingUndo.shift();
          if (button) {
            button.textContent = msg.ok ? '✓ Undone' : 'Not undone';
            if (typeof msg.message === 'string') button.title = msg.message;
          }
          break;
        }
      }
    }

    // A message the page cannot draw must not leave it stuck: the end of a task still frees Send.
    function handleMessage(msg) {
      try {
        dispatchMessage(msg);
      } catch (err) {
        if (msg && (msg.type === 'turnComplete' || msg.type === 'taskAborted')) setBusy(false);
        console.error('DLNE chat: could not show a message', err);
      }
    }

    window.addEventListener('message', (event) => handleMessage(event.data));

    // The page is ready for the extension's account of the chat. It asks, so that a page VS Code made anew (a view
    // moved, the webviews reloaded) is never left with what was saved at its last event.
    vscode.postMessage({ type: 'ready' });
  </script>
</body>
</html>`;
  }
}
