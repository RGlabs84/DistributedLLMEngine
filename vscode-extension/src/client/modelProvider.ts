/**
 * DLNE Swarm in VS Code's Chat model picker (extension 1.5.0).
 *
 * A language model provider (the stable `vscode.lm.registerLanguageModelChatProvider`, VS Code 1.104 and later) that
 * puts the swarm's coding model next to the others. That gives three things the participant (@dlne) alone cannot:
 * VS Code's own Ask, Edit and Agent modes can run on the swarm, `@dlne` runs for someone who is not signed in to
 * GitHub (a participant needs *some* model before VS Code calls its handler), and the swarm is the model Microsoft's
 * supported extension route points at, for when the participant API is retired.
 *
 * The request goes through DlneClient.streamChat, so it keeps everything a DLNE request has: the API key, the worker
 * token or the free trial, the queue and the status bar's place in line, secret redaction, end-to-end sealing for
 * a coding member, and the retry while the coordinator is busy or loading. What is new is the translation at both ends
 * (client/lmMessages.ts and client/lmReply.ts) and three rules for a model that VS Code's agent drives:
 *
 *  - Nothing that is not the model's answer is ever reply text. VS Code stores the text as the assistant's message and
 *    sends it back on the next step, so a "waiting in line" line, the coordinator's notices and a tool call written as
 *    tags would all become part of the conversation. The wait is the status bar's; a notice is thrown as an error.
 *  - A dropped engine is retried here (VS Code will not), but only while nothing has been shown: text that has already
 *    reached the chat cannot be taken back, and a second try would be appended to it.
 *  - Only tools VS Code offered for this request can be called.
 *
 * What this path cannot promise is the "private files are never read" rule: here it is VS Code's tools that read the
 * files, and the provider sees only the text they hand over. What it does instead (client/lmPrivacy.ts): secrets in a
 * known shape are removed, as everywhere; a tool result or an attachment is replaced by the usual notice when the call or the
 * tag names a file the privacy rules exclude; the tool list is redacted too; and the request never goes to a volunteer's
 * machine because a repository is public (it is capped at "trusted" unless the user chose anyMember). A file read without
 * being named (a search's matching lines, `cat .env` in a terminal) cannot be recognised, and the first use says so.
 *
 * Registered only where the API exists (`engines.vscode` stays at 1.85, so older editors still install), and quietly
 * everywhere else. The vendor must also be declared in package.json (`contributes.languageModelChatProviders`): VS
 * Code refuses an undeclared one, with an error in its log and none in the extension.
 */
import * as vscode from 'vscode';
import { AGENT_MAX_TOKENS, ChatMessage, DlneClient, EngineDropped, ServerRefusal, StreamChatResult } from './dlneClient';
import {
  DEFAULT_CHARS_PER_TOKEN, ENGINE_DROP_RETRIES, contextTokensFrom, engineDropWaitMs, isOverflowAlert, isOverflowError,
  promptBudgetFor,
} from '../agent/agentLoop';
import { visionOffered } from './pictures';
import { checkHistory, loopNote, loopStopError, loopStopNote, loopSummary } from '../agent/loopGuard';
import { logOnce } from '../log';
import { redactText } from '../privacy';
import { RequestMessage, TokenEstimator, chatChars, toOpenAiMessages, toOpenAiTools } from './lmMessages';
import { redactedTools, withoutExcludedFiles } from './lmPrivacy';
import { activeModelName } from './modelChoice';
import { ReplyFilter, SkippedCall, cleanNotice, collectToolCalls } from './lmReply';
import { newSessionId } from './session';

/** The vendor id: the same text as `contributes.languageModelChatProviders[].vendor` in package.json. */
export const MODEL_VENDOR = 'dlne';
/** Kept for good: VS Code remembers a user's pick by this id. "auto" because the swarm chooses the model itself. */
export const MODEL_ID = 'swarm-auto';
export const MODEL_NAME = 'DLNE Swarm';
export const MODEL_FAMILY = 'dlne-swarm';

export const OVERFLOW_ERROR =
  'This conversation no longer fits the DLNE swarm\'s context window. Start a new chat, or summarize what matters and carry on there.';
export const DROP_GIVE_UP =
  'The swarm\'s engine kept dropping the connection (it may be restarting), even after waiting about a minute and a half. ' +
  'Send your message again in a moment.';
export const DROP_MID_ANSWER =
  'The swarm\'s engine dropped the connection part-way through its answer, so the text above is incomplete. Send your message again.';
export const EMPTY_REPLY = 'DLNE Swarm sent an empty reply. Send your message again.';
export const CUT_OFF_CALL =
  'DLNE Swarm wrote a tool call that was cut off or could not be used, so nothing was run. Ask for a smaller step, or send your message again.';

/**
 * Said once, the first time the swarm is used from the Chat view. The picker path cannot keep private files out of a request
 * the way @dlne can, and the user should hear that before relying on it, and hear what is still done. It has a button (the
 * setting it ends with): VS Code shows a toast without one on a single line, cut after about ninety characters, and the part
 * that matters comes after that.
 */
export const FIRST_USE_NOTICE =
  'DLNE Swarm in the Chat view: VS Code\'s own tools read your files and hand DLNE the text, so DLNE cannot keep private files out ' +
  'of a request the way @dlne does. It still hides secrets it recognises, refuses an excluded file (.env, key files, .gitignore, ' +
  '.dlneignore) that a tool or an attachment names, and sends these requests only to DLNE\'s own machines and trusted members ' +
  'unless dlne.privacy is anyMember.';
/** The notice's button, and what it opens. */
export const OPEN_PRIVACY_SETTING = 'Privacy setting';
/** Where the extension remembers that the notice was shown (its global state). */
const FIRST_USE_KEY = 'dlne.pickerPrivacyNoticeShown';

/** What the provider needs of the extension's global state. */
export type NoticeMemory = Pick<vscode.Memento, 'get' | 'update'>;

/** How many conversations keep their own X-DLNE-Session (older ones fall back to a fresh id when they come back). */
const MAX_SESSIONS = 64;

/** What the provider needs of the client; a test can give less. */
export type ModelClient = Pick<DlneClient, 'streamChat'> & Partial<Pick<DlneClient, 'lastKnownClusterStatus' | 'onClusterStatus'>>;

type Status = ReturnType<DlneClient['lastKnownClusterStatus']>;

/** What the entry shows of the status: if this changes, VS Code is told to ask again. */
function signatureOf(status: Status | undefined): string {
  return `${contextTokensFrom(status)}|${activeModelName(status) ?? ''}|${visionOffered(status) ? 'vision' : ''}`;
}

/** A pause that a Stop cuts short. */
function pause(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    signal.addEventListener('abort', () => {
      clearTimeout(timer);
      resolve();
    }, { once: true });
  });
}

/**
 * An error whose text is all there is to say. The Chat view prints an error's stack under its message ("Reason: text:
 * Error: text at ..."), which for a sentence written for the user is the sentence twice, then paths from this machine.
 * So it has no stack (the DLNE log keeps what went wrong, see provideLanguageModelChatResponse).
 */
function sentence(message: string): Error {
  const error = new Error(message);
  error.stack = undefined;
  return error;
}

/**
 * The loop check's stop (agent/loopGuard.ts). The chat shows its sentence, which names what the agent kept doing, so the
 * log gets `logText` instead: the tool and the counts, never a command or a path.
 */
class LoopStopped extends Error {
  constructor(message: string, readonly logText: string) {
    super(message);
    this.name = 'LoopStopped';
    this.stack = undefined;
  }
}

/**
 * The messages with secrets hidden, once more. The request pipeline hides them again before anything leaves; doing it
 * here keeps "nothing VS Code built for the model goes out unchecked" true of this file by itself, whatever the
 * pipeline later becomes. The same text always gives the same result, so the engine's cached prompt still matches.
 */
function scrub(messages: ChatMessage[]): ChatMessage[] {
  return messages.map((message) => {
    const clean: ChatMessage = {
      ...message,
      content: typeof message.content === 'string' ? redactText(message.content).text : message.content,
    };
    if (message.tool_calls) {
      clean.tool_calls = message.tool_calls.map((call) => ({
        ...call,
        function: { ...call.function, arguments: redactText(call.function.arguments).text },
      }));
    }
    return clean;
  });
}

export class DlneChatModelProvider implements vscode.LanguageModelChatProvider {
  private readonly changed = new vscode.EventEmitter<void>();
  readonly onDidChangeLanguageModelChatInformation = this.changed.event;

  private readonly estimator = new TokenEstimator(DEFAULT_CHARS_PER_TOKEN);
  /** What the entry last said (signatureOf); null until VS Code has asked once. */
  private reported: string | null = null;
  private readonly stopListening?: () => void;
  /** One X-DLNE-Session per conversation (sticky routing to a coding member, and the engine's cache), see sessionFor. */
  private readonly sessions = new Map<string, string>();
  private readonly fallbackSession = newSessionId();
  /** Whether this provider has already dealt with the first-use notice (shown it, or found it was shown before). */
  private noticeHandled = false;

  /** `memory`: where "the first-use notice was shown" is kept between sessions; without it the notice is once per session. */
  constructor(private readonly client: ModelClient, private readonly memory?: NoticeMemory) {
    // The status bar polls the status; when it shows a new context size or model, the entry is read again.
    this.stopListening = client.onClusterStatus?.((status) => this.statusChanged(status));
  }

  /** The first request ever: one toast that says what this path can and cannot keep private. Never holds the request up. */
  private noteFirstUse(): void {
    if (this.noticeHandled) return;
    this.noticeHandled = true;
    try {
      if (this.memory?.get<boolean>(FIRST_USE_KEY)) return;
      Promise.resolve(this.memory?.update(FIRST_USE_KEY, true)).catch(() => {});
      Promise.resolve(vscode.window.showInformationMessage(FIRST_USE_NOTICE, OPEN_PRIVACY_SETTING))
        .then((choice) => (choice === OPEN_PRIVACY_SETTING ? vscode.commands.executeCommand('workbench.action.openSettings', 'dlne.privacy') : undefined))
        .then(undefined, () => {});
    } catch {
      // a notice that cannot be shown changes nothing about the request
    }
  }

  dispose(): void {
    this.stopListening?.();
    this.changed.dispose();
  }

  private statusChanged(status: Status): void {
    if (this.reported === null) return;
    const now = signatureOf(status);
    if (now !== this.reported) {
      this.reported = now;
      this.changed.fire();
    }
  }

  /** One entry, always (even while the swarm is out of reach: a request then says why). It never touches the network. */
  provideLanguageModelChatInformation(): vscode.LanguageModelChatInformation[] {
    const status = this.client.lastKnownClusterStatus?.() ?? null;
    this.reported = signatureOf(status);
    return [{
      id: MODEL_ID,
      name: MODEL_NAME,
      family: MODEL_FAMILY,
      version: '1',
      detail: activeModelName(status) ?? undefined,
      tooltip: 'The coding model the DLNE swarm is running, on GPUs that people share with it. Every step of an agent task is one request.',
      // The prompt gets what the agent loop gives its own: the reply's room and a margin are kept back from the context.
      maxInputTokens: promptBudgetFor(contextTokensFrom(status)),
      maxOutputTokens: AGENT_MAX_TOKENS,
      // Pictures are read while the coordinator says its engine can (capabilities.vision); a coding member cannot.
      capabilities: { toolCalling: true, imageInput: visionOffered(status) },
    }];
  }

  provideTokenCount(_model: vscode.LanguageModelChatInformation, text: string | RequestMessage, _token?: vscode.CancellationToken): Thenable<number> {
    return Promise.resolve(this.estimator.tokens(text));
  }

  async provideLanguageModelChatResponse(
    _model: vscode.LanguageModelChatInformation,
    messages: readonly vscode.LanguageModelChatRequestMessage[],
    options: vscode.ProvideLanguageModelChatResponseOptions,
    progress: vscode.Progress<vscode.LanguageModelResponsePart>,
    token: vscode.CancellationToken
  ): Promise<void> {
    if (token.isCancellationRequested) return;
    this.noteFirstUse();
    // Stop: the request is dropped at once, which frees the swarm's slot for someone else.
    const abort = new AbortController();
    const listener = token.onCancellationRequested(() => abort.abort());
    try {
      await this.respond(messages, options, progress, abort.signal);
    } catch (err) {
      // The chat shows the sentence; the DLNE log keeps it too, for a bug report (it never holds a prompt or a reply).
      const why = err instanceof LoopStopped ? err.logText : err instanceof Error ? err.message : String(err);
      logOnce(`lm-failed ${why.slice(0, 80)}`, `DLNE Swarm: a request failed: ${why}`);
      throw err;
    } finally {
      listener.dispose();
    }
  }

  private async respond(
    messages: readonly vscode.LanguageModelChatRequestMessage[],
    options: vscode.ProvideLanguageModelChatResponseOptions,
    progress: vscode.Progress<vscode.LanguageModelResponsePart>,
    signal: AbortSignal
  ): Promise<void> {
    // An image is refused here, before anything is sent. What VS Code's tools read is held back where the privacy rules say so
    // (the contents of an excluded file become the usual notice), then secrets are hidden, in the tool list as well.
    const workspace = vscode.workspace.workspaceFolders?.[0]?.uri?.fsPath;
    const vision = visionOffered(this.client.lastKnownClusterStatus?.() ?? null);
    const body = this.checkedForLoop(scrub(withoutExcludedFiles(toOpenAiMessages(messages, { vision }), workspace)));
    const tools = redactedTools(toOpenAiTools(options.tools));
    const offered = new Set(tools.map((tool) => tool.function.name));
    const sessionId = this.sessionFor(options.modelOptions);
    const sentChars = chatChars(body, tools);

    let shown = false; // some of the answer has reached the chat
    const say = (text: string) => {
      if (!text || signal.aborted) return;
      progress.report(new vscode.LanguageModelTextPart(text));
      if (text.trim()) shown = true;
    };

    let result: StreamChatResult;
    let filter: ReplyFilter;
    for (let attempt = 1; ; attempt++) {
      filter = new ReplyFilter(offered.size > 0);
      const current = filter;
      try {
        result = await this.client.streamChat(
          body,
          tools,
          (delta) => say(current.push(delta)),
          undefined,
          signal,
          // No progress line: a wait is not reply text. The place in line goes to the status bar through the client's
          // own listeners, and passing a listener here is what keeps it from being worded into the reply instead.
          undefined,
          // maxPrivacy: VS Code's tools chose the files, so a public repository (auto mode) is no reason to send them to a
          // volunteer's machine; only the user's own anyMember setting widens it.
          { sessionId, onQueue: () => {}, maxPrivacy: 'trusted' }
        );
        break;
      } catch (err) {
        if (signal.aborted) return;
        // The coordinator lost its engine (a GPU joined or left): the same request goes again, after the wait the agent
        // loop uses. Only while nothing has been shown, so no answer is ever shown twice.
        if (err instanceof EngineDropped && !shown && attempt <= ENGINE_DROP_RETRIES) {
          logOnce('lm-engine-drop', `DLNE Swarm: the engine dropped the request (${err.kind}); sending it again (${attempt}/${ENGINE_DROP_RETRIES}).`, 0);
          await pause(engineDropWaitMs(attempt), signal);
          if (signal.aborted) return;
          continue;
        }
        throw this.readable(err, shown);
      }
    }
    if (signal.aborted) return;

    this.estimator.calibrate(sentChars, result.promptTokens);
    const end = filter.finish();
    say(end.text);
    // The coordinator spoke inside the answer. It is an error for VS Code to show, never part of the conversation.
    if (end.notice !== null) throw this.noticeError(end.notice);

    const { calls, skipped } = collectToolCalls(result.toolCalls, result.content, offered);
    if (skipped.length > 0) {
      logOnce('lm-tool-skipped', `DLNE Swarm: left out ${skipped.map((s) => `${s.name} (${s.why})`).join(', ')}.`);
    }
    if (signal.aborted) return;
    for (const call of calls) {
      progress.report(new vscode.LanguageModelToolCallPart(call.id, call.name, call.input));
    }
    if (calls.length === 0) {
      // The model reached for a tool and nothing usable came out of it (cut off, unreadable, or not offered): with
      // nothing run, the turn would end as if the answer were complete, so the user is told instead.
      const lost = this.lostCallReason(end, skipped);
      if (lost) throw sentence(lost);
      // VS Code reads a reply with neither text nor a call as "no response", which tells the user nothing.
      if (!shown) throw sentence(EMPTY_REPLY);
    }
  }

  /**
   * VS Code's own agent drives this model, and every request carries the whole history, so an agent that keeps repeating
   * itself is seen here, from the history alone (agent/loopGuard.ts): at the rule's count the system message gets a note to
   * change approach, and two repeats later the request is refused before anything is sent. Nothing is kept between requests:
   * when the agent does something else, or the user writes, the next request is checked afresh and is not refused.
   *
   * The refusal leaves nothing in VS Code's history, so when the user writes after one, the request that carries their words
   * would show the model its own loop and no word of why it ended. That request, and only that one, gets the stop's note in
   * its system message, as the agent loop leaves it in the history of a run it stops. The history is otherwise as VS Code sent it.
   */
  private checkedForLoop(body: ChatMessage[]): ChatMessage[] {
    const check = checkHistory(body, (text) => redactText(text).text);
    if (check.level === 'none') return body;
    if (check.level === 'stop') {
      throw new LoopStopped(loopStopError(check.reason), `loop check stopped the agent (${loopSummary(check.hit)})`);
    }
    let note: string;
    if (check.level === 'nudge') {
      logOnce('lm-loop-nudge', `DLNE Swarm: loop check told the agent to change approach (${loopSummary(check.hit)}).`);
      note = loopNote(check.reason);
    } else {
      logOnce('lm-loop-stopped', `DLNE Swarm: loop check told the agent why its last task was stopped (${loopSummary(check.hit)}).`);
      note = loopStopNote(check.reason);
    }
    const [first, ...rest] = body;
    // Chat templates want one system message, first (lmMessages.ts): the note joins it, or is one when there is none.
    return first?.role === 'system'
      ? [{ ...first, content: `${first.content ?? ''}\n\n${note}` }, ...rest]
      : [{ role: 'system', content: note }, ...body];
  }

  /** Why a tool call the model reached for was not passed on, or null when it did not reach for one. */
  private lostCallReason(end: { unfinishedCall: boolean; sawCallBlock: boolean }, skipped: SkippedCall[]): string | null {
    if (end.unfinishedCall || skipped.some((call) => call.why === 'bad-arguments')) return CUT_OFF_CALL;
    if (skipped.length > 0) {
      const names = [...new Set(skipped.map((call) => call.name))].join(', ');
      return `DLNE Swarm asked for a tool that VS Code did not offer (${names}). Send your message again.`;
    }
    return end.sawCallBlock ? CUT_OFF_CALL : null;
  }

  private noticeError(notice: string): Error {
    if (isOverflowAlert(notice)) {
      this.estimator.noteOverflow();
      return sentence(OVERFLOW_ERROR);
    }
    return sentence(cleanNotice(notice));
  }

  /** An error for the user, in plain words. The coordinator's own sentence is kept as it wrote it (a spent trial, a CPU window). */
  private readable(err: unknown, shown: boolean): Error {
    if (err instanceof EngineDropped) return sentence(shown ? DROP_MID_ANSWER : DROP_GIVE_UP);
    const message = err instanceof Error ? err.message : String(err);
    if (isOverflowError(message)) {
      this.estimator.noteOverflow();
      return sentence(OVERFLOW_ERROR);
    }
    if (err instanceof ServerRefusal) return sentence(message);
    return sentence(`Could not get an answer from the DLNE swarm: ${message}`);
  }

  /**
   * The X-DLNE-Session for a request: one per conversation, so each is sent back to the coding member that has already
   * read its prompt. Copilot Chat names the conversation in `modelOptions._conversationId`, an internal field that
   * may change or go: without it every request shares one id, as before. The id sent is always a fresh random one
   * and never made from the conversation's name or text (session.ts).
   */
  private sessionFor(modelOptions: unknown): string {
    const key = (modelOptions as { _conversationId?: unknown } | undefined)?._conversationId;
    if (typeof key !== 'string' || !key) return this.fallbackSession;
    let id = this.sessions.get(key);
    if (id === undefined) {
      id = newSessionId();
    } else {
      this.sessions.delete(key); // to the newest end
    }
    this.sessions.set(key, id);
    if (this.sessions.size > MAX_SESSIONS) {
      this.sessions.delete(this.sessions.keys().next().value as string);
    }
    return id;
  }
}

/**
 * Adds DLNE Swarm to the model picker, if this editor has the API. Never throws and never logs on an editor without
 * it (VS Code before 1.104, and the editors built on it that leave the language model API out): there the
 * participant and the sidebar are DLNE's chat, as before. `lm` is the editor's `vscode.lm`, passed only by tests. The
 * extension's global state, when given, is where "the first-use notice was shown" is kept.
 */
export function registerDlneChatModel(
  context: { subscriptions: { dispose(): unknown }[]; globalState?: NoticeMemory },
  client: ModelClient,
  lm: any = (vscode as any).lm
): { dispose(): unknown } | undefined {
  if (!lm || typeof lm.registerLanguageModelChatProvider !== 'function') return undefined;
  let provider: DlneChatModelProvider | undefined;
  try {
    provider = new DlneChatModelProvider(client, context.globalState);
    const registration = lm.registerLanguageModelChatProvider(MODEL_VENDOR, provider);
    context.subscriptions.push(registration, provider);
    return registration;
  } catch (err) {
    provider?.dispose();
    logOnce('lm-register', `DLNE Swarm could not be added to the model picker: ${err instanceof Error ? err.message : String(err)}`);
    return undefined;
  }
}
