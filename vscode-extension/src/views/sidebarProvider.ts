import * as vscode from 'vscode';
import * as path from 'path';
import * as crypto from 'crypto';
import * as os from 'os';
import { AgentLoop } from '../agent/agentLoop';
import { DlneClient } from '../client/dlneClient';
import { swarmModelLine } from '../client/modelChoice';
import { QueueUpdate, queueDetail, queueHeadline } from '../client/queue';
import { WorkerManager, WorkerStatus } from '../worker/workerManager';
import { contributionCopy, roleCopy } from '../worker/contribution';
import {
  DonateChoice, DonateMachine, DonatePanelModel, buildDonatePanel, choiceFromSettings, donateMachine, readDonateSettings,
  reconcileDraft, validateChoice,
} from '../worker/donatePanel';
import { DONATE_CSS, createDonatePanel } from './donateView';
import { phoneChatUrl } from '../serverUrl';
import { ToolCallResult } from '../agent/tools';
import { renderMarkdown } from './markdown';

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

export class SidebarProvider implements vscode.WebviewViewProvider {
  private _view?: vscode.WebviewView;
  private agentLoop: AgentLoop;
  private conversationHistory: ChatMessageItem[] = [];

  constructor(
    private readonly _extensionUri: vscode.Uri,
    private readonly client: DlneClient,
    private readonly workerManager: WorkerManager,
    private readonly context?: vscode.ExtensionContext
  ) {
    this.agentLoop = new AgentLoop(client);

    // Load persisted history
    if (this.context) {
      const saved = this.context.workspaceState.get<ChatMessageItem[]>('dlne.chatHistory');
      if (saved && Array.isArray(saved)) {
        this.conversationHistory = saved;
      }
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
      started: status.startedWith,
      choice: choice ?? reconcileDraft(this.draft, settings, machine),
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

  private saveHistory() {
    if (this.context) {
      // Keep at most last 40 turns to avoid workspaceState bloat
      const trimmed = this.conversationHistory.slice(-40);
      this.context.workspaceState.update('dlne.chatHistory', trimmed);
    }
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

    // Push state immediately on mount
    const workerStatus = this.workerManager.getStatus();
    webviewView.webview.postMessage({
      type: 'restoreHistory',
      history: this.conversationHistory,
      workerStatus,
      workerCopy: contributionCopy(workerStatus),
      workerRole: roleCopy(workerStatus),
      donate: this._donateModel(workerStatus),
      modelLine: this._modelLine(),
      queue: this._queueView(this.client.currentQueue?.())
    });

    // The model line follows the status the status bar already polls; nothing here asks for it.
    const stopFollowing = this.client.onClusterStatus?.((status) => {
      this._view?.webview.postMessage({ type: 'modelLine', text: swarmModelLine(status) });
    });
    webviewView.onDidDispose?.(() => stopFollowing?.());

    webviewView.webview.onDidReceiveMessage(async (data) => {
      switch (data.type) {
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
        case 'abortTask': {
          this.agentLoop.abort();
          this._view?.webview.postMessage({ type: 'taskAborted' });
          break;
        }
        case 'clearChat': {
          this.conversationHistory = [];
          this.saveHistory();
          this.agentLoop.clearHistory();
          this._view?.webview.postMessage({ type: 'cleared' });
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
        case 'installSuite': {
          await vscode.commands.executeCommand('dlne.installSuite');
          break;
        }
        case 'openPhoneApp': {
          await vscode.commands.executeCommand('dlne.openPhoneApp');
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

  private async handleUserMessage(text: string) {
    if (!text.trim()) return;

    const userMsg: ChatMessageItem = {
      id: 'msg-' + Date.now() + '-u',
      role: 'user',
      text
    };
    this.conversationHistory.push(userMsg);
    this.saveHistory();

    this._view?.webview.postMessage({ type: 'appendUserMessage', text });

    const assistantMsg: ChatMessageItem = {
      id: 'msg-' + Date.now() + '-a',
      role: 'assistant',
      text: '',
      thinking: '',
      toolCalls: []
    };
    this.conversationHistory.push(assistantMsg);

    await this.agentLoop.run(text, {
      onLoadingProgress: (statusText) => {
        this._view?.webview.postMessage({ type: 'loadingProgress', statusText });
      },
      // While every coding slot is taken: where this request stands, in a banner that stays in view.
      onQueue: (queue) => {
        this._view?.webview.postMessage({ type: 'queue', queue: this._queueView(queue) });
      },
      onThinkingDelta: (delta) => {
        assistantMsg.thinking = (assistantMsg.thinking || '') + delta;
        this._view?.webview.postMessage({ type: 'thinkingDelta', delta });
      },
      onContentDelta: (delta) => {
        assistantMsg.text += delta;
        this._view?.webview.postMessage({ type: 'contentDelta', delta });
      },
      onToolStart: (toolName, args) => {
        if (!assistantMsg.toolCalls) assistantMsg.toolCalls = [];
        assistantMsg.toolCalls.push({ name: toolName, args });
        this._view?.webview.postMessage({ type: 'toolStart', toolName, args });
      },
      onToolEnd: (result) => {
        if (assistantMsg.toolCalls && assistantMsg.toolCalls.length > 0) {
          assistantMsg.toolCalls[assistantMsg.toolCalls.length - 1].result = result;
        }
        this.saveHistory();
        this._view?.webview.postMessage({ type: 'toolEnd', result });
      },
      onError: (error) => {
        this._view?.webview.postMessage({ type: 'error', error });
      }
    });

    this.saveHistory();
    this._view?.webview.postMessage({ type: 'turnComplete' });
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
      background-color: var(--vscode-button-background);
      color: var(--vscode-button-foreground);
      border: none;
      border-radius: 4px;
      cursor: pointer;
      font-size: 0.88em;
    }
    button:hover {
      background-color: var(--vscode-button-hoverBackground);
    }
    button.secondary {
      background-color: var(--vscode-button-secondaryBackground);
      color: var(--vscode-button-secondaryForeground);
    }
    button.contrib-btn {
      flex: 1;
      font-weight: 600;
    }
    button.stop-btn {
      background-color: #f43f5e;
      color: #fff;
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

  <div id="chat-container">
    <div class="message assistant" id="welcome-msg">
      <strong><img class="logo" src="${logoSrc}" alt="" width="24" height="24">DLNE Code Assistant</strong>
      <div class="assistant-body">Connected to cluster. Ready for autonomous coding, refactoring, and test verification.</div>
    </div>
  </div>

  <div class="queue-banner" id="queue-banner" role="status" aria-live="polite" style="display:none">
    <div class="queue-headline" id="queue-headline"></div>
    <div class="queue-detail" id="queue-detail"></div>
  </div>

  <div id="input-container">
    <textarea id="prompt-input" rows="3" placeholder="Ask DLNE to write code, edit files, or fix bugs..."></textarea>
    <div class="btn-row">
      <span class="model-line" id="model-line" title="The swarm picks the model from the GPUs available; there is nothing to choose.">${modelLine}</span>
      <div class="btn-group-right">
        <button class="secondary" id="clear-btn">Clear</button>
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

    // The agent's answer is shown as Markdown. renderMarkdown (src/views/markdown.ts, this same function) escapes
    // everything the model wrote before it reads any Markdown, so the text can never become markup of its own.
    ${renderMarkdown.toString()}

    function renderCurrentAnswer() {
      if (renderTimer) {
        clearTimeout(renderTimer);
        renderTimer = null;
      }
      if (currentContentElem) {
        currentContentElem.innerHTML = renderMarkdown(currentContentRaw);
        chatContainer.scrollTop = chatContainer.scrollHeight;
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

    function saveWebviewState() {
      vscode.setState({ chatHtml: chatContainer.innerHTML, donateOpen: contribCard.open });
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

    sendBtn.addEventListener('click', () => {
      const text = promptInput.value.trim();
      if (!text) return;
      promptInput.value = '';
      sendBtn.style.display = 'none';
      stopBtn.style.display = 'inline-block';
      vscode.postMessage({ type: 'sendMessage', text });
    });

    stopBtn.addEventListener('click', () => {
      vscode.postMessage({ type: 'abortTask' });
    });

    clearBtn.addEventListener('click', () => {
      vscode.postMessage({ type: 'clearChat' });
      chatContainer.innerHTML = '';
      saveWebviewState();
    });

    promptInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendBtn.click();
      }
    });

    // Handle delegated click for Open File buttons in diff cards
    chatContainer.addEventListener('click', (e) => {
      const target = e.target;
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

    window.addEventListener('message', (event) => {
      const msg = event.data;
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

        case 'restoreHistory': {
          if (msg.modelLine) {
            modelLine.textContent = msg.modelLine;
          }
          if (msg.workerStatus) {
            applyWorkerStatus(msg.workerStatus, msg.workerCopy, msg.workerRole, msg.donate);
          }
          showQueue(msg.queue);
          if (msg.history && msg.history.length > 0) {
            chatContainer.innerHTML = '';
            msg.history.forEach((item) => {
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
                  asstDiv.appendChild(body);
                }
                chatContainer.appendChild(asstDiv);
              }
            });
            chatContainer.scrollTop = chatContainer.scrollHeight;
            saveWebviewState();
          }
          break;
        }

        case 'appendUserMessage': {
          const div = document.createElement('div');
          div.className = 'message user';
          div.textContent = msg.text;
          chatContainer.appendChild(div);
          chatContainer.scrollTop = chatContainer.scrollHeight;

          currentAssistantMsg = document.createElement('div');
          currentAssistantMsg.className = 'message assistant';
          chatContainer.appendChild(currentAssistantMsg);

          currentThinkingElem = null;
          currentThinkingBadge = null;
          currentContentElem = null;
          currentContentRaw = '';
          activePillElem = null;
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
            chatContainer.scrollTop = chatContainer.scrollHeight;
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
            chatContainer.scrollTop = chatContainer.scrollHeight;
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
            chatContainer.scrollTop = chatContainer.scrollHeight;
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
          }
          chatContainer.scrollTop = chatContainer.scrollHeight;
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
          sendBtn.style.display = 'inline-block';
          stopBtn.style.display = 'none';
          saveWebviewState();
          break;
        }

        case 'error': {
          showQueue(null);
          const div = document.createElement('div');
          div.className = 'message assistant';
          div.style.color = 'var(--vscode-errorForeground)';
          div.textContent = 'Error: ' + msg.error;
          chatContainer.appendChild(div);
          sendBtn.style.display = 'inline-block';
          stopBtn.style.display = 'none';
          saveWebviewState();
          break;
        }
      }
    });
  </script>
</body>
</html>`;
  }
}
