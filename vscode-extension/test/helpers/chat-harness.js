'use strict';
/**
 * A stand-in for VS Code's Chat view that drives the REAL `@dlne` participant (dist/views/nativeChatParticipant.js):
 * fake ChatRequest, ChatContext, ChatResponseStream and CancellationToken objects, Chat threads that keep their own
 * transcript the way VS Code does, and window reloads. Written for item 2 of the 1.5.0 plan (one conversation per thread,
 * steer, Stop); built to be added to: other builders write their tests against it after merging.
 *
 *   const { openChat, scriptedClient, toolCall, gate, until } = require('./helpers/chat-harness');
 *   const chat = openChat({ client: scriptedClient([{ content: 'Hello.' }]) });
 *   const thread = chat.thread();                       // a new Chat thread (an empty transcript)
 *   const r = await thread.ask('say hi');               // one request, awaited: r.result, r.stream.text(), r.token ...
 *   const running = thread.send('long task');           // a request left running: running.stop(), await running.done
 *   thread.steer('also this');                          // the user presses Enter during a run (VS Code's default)
 *   thread.rewindTo(2);                                 // Restore Checkpoint / edit an earlier request: only the first 2 requests stay
 *   thread.removeRequest(2);                            // Delete Request: one request and its answer out of the middle
 *   const copy = thread.fork();                         // Fork: a new thread with a copy of the transcript (same ids and marks)
 *   chat.reload();                                      // the window is reloaded: a new participant over the same storage
 *
 * What it reproduces of VS Code 1.143.1, read from its source (extensionHostProcess.js, workbench.desktop.main.js):
 *  - `chatContext.history` holds, for each finished request of this participant, a request turn { prompt, command,
 *    participant, references, toolReferences } and a response turn { response, result, participant, command }. `result`
 *    is what the handler returned, metadata included (prepareHistoryTurns keeps metadata only for the participant's own
 *    turns). A request that was not finished when a window was reloaded has a response with an empty result.
 *  - Enter during a request ("steer", the default of chat.requestQueuing.defaultAction): the new message is QUEUED, not
 *    run beside the old request; `chatContext.yieldRequested` becomes true on the request in flight (a live getter); the
 *    queued message is sent when that request has ended, unless it was cancelled or ended with errorDetails
 *    (ChatService processNextPendingRequest, the `Y` condition). Several steering messages are joined into one.
 *    "Add to Queue" (`thread.queued`) is the same without the yield. (Both seen live in VS Code 1.143.1.)
 *  - Stop fires the token. The handler then has `grace` ms (1 s in VS Code: Dne(1e3, ...)): if it returns in time its
 *    result is kept, otherwise the result is lost (an answer with no metadata) and the stream is closed under it
 *    (every stream method then throws "Response stream has been closed").
 *  - "Stop and Send" (`thread.stopAndSend`) cancels, waits at most that grace, and sends the new message even when the
 *    old handler is still running: the one way two requests of a thread overlap.
 *  - A handler that throws gives VS Code's error result: { errorDetails }, with no metadata.
 *  - The stream is closed when the request is over.
 * What it does not: webviews, the model picker, real rendering (the stream records parts in order, nothing more).
 *
 * Each test file runs in its own process; the harness isolates HOME the way the other tests do.
 */
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const stub = require('./vscode-stub');

/** A temp folder that is removed when the test process ends (/tmp is memory: the harness leaves nothing behind). */
const made = [];
function tmpDir(prefix) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), prefix));
  made.push(dir);
  return dir;
}
process.once('exit', () => {
  for (const dir of made) fs.rmSync(dir, { recursive: true, force: true });
});

process.env.HOME = tmpDir('dlne-harness-home-'); // (chat-latency.test.js already uses "dlne-chat-home-" and leaves it behind)

// The compiled participant copies the vscode module's members when it loads, so the Chat API exists before it is required.
let registering = null;
stub.vscode.chat = {
  createChatParticipant(id, handler) {
    registering = { id, handler, participant: { dispose() {} } };
    return registering.participant;
  },
};
const { registerNativeChatParticipant } = require('../../dist/views/nativeChatParticipant');
const { agentThreadsDir, agentThreadFile, agentStateFile } = require('../../dist/agent/agentState');
const { threadIdFromHistory } = require('../../dist/agent/chatThreads');

const EXT_ROOT = path.join(__dirname, '..', '..');

// ---- small helpers for scripted conversations ---------------------------------------------------------------

/** Polls until `condition()` is truthy (or throws after `ms`): for events that happen on their own schedule. */
async function until(condition, ms = 4000, what = 'the condition') {
  const started = Date.now();
  for (;;) {
    const value = await condition();
    if (value) return value;
    if (Date.now() - started > ms) throw new Error(`timed out after ${ms} ms waiting for ${what}`);
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

/** A promise that a test opens by hand: `await g.opened`, and `g.open(value)` lets whoever waits go (with `value`). */
function gate() {
  let open;
  const opened = new Promise((resolve) => { open = resolve; });
  return { opened, open };
}

/** A native tool call as streamChat returns it. */
function toolCall(name, args = {}, id = `call_${name}_${Math.random().toString(36).slice(2, 8)}`) {
  return { id, name, arguments: JSON.stringify(args) };
}

/**
 * A DlneClient stand-in whose model plays `replies`: one per agent request, the last repeating. A reply is
 * { content, toolCalls, finishReason, throw (a message or an Error), status } or a function of the request that returns one
 * (it may wait: that is how a test holds a step open). `client.requests` records what the model was sent:
 * { messages, tools (names), signal }. Like the real client, a request whose signal aborts returns at once with nothing.
 */
function scriptedClient(replies, extra = {}) {
  const requests = [];
  let next = 0;
  const nothing = { content: '', toolCalls: [] };
  return {
    requests,
    lastKnownClusterStatus: () => null,
    getServerUrl: () => 'http://127.0.0.1:9',
    async getClusterStatusDetailed() { return { status: 'unreachable' }; },
    async streamChat(messages, tools, onDelta, _onToolStart, signal) {
      const request = { messages: JSON.parse(JSON.stringify(messages)), tools: tools.map((t) => t.function.name), signal };
      requests.push(request);
      if (signal?.aborted) return nothing;
      let reply = replies[Math.min(next++, replies.length - 1)];
      if (typeof reply === 'function') {
        const aborted = new Promise((resolve) => signal?.addEventListener('abort', () => resolve(nothing), { once: true }));
        reply = await Promise.race([reply(request, signal), aborted]);
        if (signal?.aborted) return nothing;
      }
      if (reply.throw) throw reply.throw instanceof Error ? reply.throw : Object.assign(new Error(reply.throw), { status: reply.status });
      if (reply.content) onDelta(reply.content);
      return { content: reply.content || '', toolCalls: reply.toolCalls || [], finishReason: reply.finishReason };
    },
    ...extra,
  };
}

/** The user's messages the model was sent in `request` (not the system prompt, context blocks, tool results or the agent's own turns). */
const userWordsIn = (request) => request.messages
  .filter((m) => m.role === 'user' && !String(m.content).startsWith('<dlne_'))
  .map((m) => m.content);

// ---- the fakes -----------------------------------------------------------------------------------------------

/** A ChatResponseStream that records its parts in order, and fails like VS Code's once the request is over. */
class FakeStream {
  constructor() {
    this.parts = [];
    this.closed = false;
    for (const kind of ['markdown', 'progress', 'reference', 'button', 'anchor', 'filetree', 'push']) {
      this[kind] = (value, ...rest) => {
        if (this.closed) throw new Error('Response stream has been closed');
        this.parts.push({ kind, value, rest });
        return this;
      };
    }
  }
  close() { this.closed = true; }
  /** Everything written with markdown(), as one string. */
  text() { return this.parts.filter((p) => p.kind === 'markdown').map((p) => String(p.value)).join(''); }
  progressLines() { return this.parts.filter((p) => p.kind === 'progress').map((p) => String(p.value)); }
  buttons() { return this.parts.filter((p) => p.kind === 'button').map((p) => p.value); }
}

/** A CancellationTokenSource: `token` is what a handler gets, `cancel()` is the user's Stop. */
class CancellationSource {
  constructor() {
    const listeners = [];
    this.token = {
      isCancellationRequested: false,
      onCancellationRequested: (listener) => {
        if (this.token.isCancellationRequested) setImmediate(listener);
        else listeners.push(listener);
        return { dispose() { const i = listeners.indexOf(listener); if (i >= 0) listeners.splice(i, 1); } };
      },
    };
    this.cancel = () => {
      if (this.token.isCancellationRequested) return;
      this.token.isCancellationRequested = true;
      for (const listener of [...listeners]) listener();
    };
  }
}

/** What VS Code stores of a handler's result (and what it stores when the handler threw). */
function vscodeResult(handlerResult, handlerError) {
  if (handlerError) return { errorDetails: { message: String(handlerError.message ?? handlerError), responseIsIncomplete: true } };
  const result = handlerResult ?? {};
  if (result.metadata) JSON.stringify(result.metadata); // "result.metadata MUST be JSON.stringify-able"
  return {
    errorDetails: result.errorDetails ? { ...result.errorDetails, responseIsIncomplete: true } : undefined,
    metadata: result.metadata,
    nextQuestion: result.nextQuestion,
    details: result.details,
  };
}

/** One request of a Chat thread, from the user's Enter to VS Code's idea of it being over. */
class ChatRequest {
  constructor(thread, prompt, options) {
    this.thread = thread;
    this.prompt = prompt;
    this.command = options.command;
    this.stream = new FakeStream();
    this.cancellation = new CancellationSource();
    this.token = this.cancellation.token;
    this.yieldRequested = false;
    this.request = {
      prompt,
      command: options.command,
      references: [],
      toolReferences: [],
      permissionLevel: options.permissionLevel,
      ...options.request,
    };
    const request = this;
    const context = { history: thread.historyForNextRequest(), get yieldRequested() { return request.yieldRequested; } };
    /** The handler's own promise: it may still be running after VS Code gave up on it. */
    this.handler = new Promise((resolve) => {
      Promise.resolve()
        .then(() => thread.chat.handler(this.request, context, this.stream, this.token))
        .then((value) => resolve({ value }), (error) => resolve({ error }));
    });
    this.handled = false;
    this.handler.then(() => { this.handled = true; });
    // Dne(1e3, handler, token): after a Stop the handler has `grace` ms, then VS Code takes the request as over.
    const graceOver = new Promise((resolve) => {
      this.token.onCancellationRequested(() => setTimeout(() => resolve('grace over'), thread.chat.grace));
    });
    /** Resolves with the result VS Code stores, when the request is over for VS Code. */
    this.done = Promise.race([this.handler, graceOver]).then((outcome) => {
      const lost = outcome === 'grace over' || this.crashed === true;
      this.lost = lost;
      this.result = lost ? {} : vscodeResult(outcome.value, outcome.error);
      this.error = lost ? undefined : outcome.error;
      this.stream.close();
      thread.finish(this);
      return this.result;
    });
  }

  /** The user's Stop. */
  stop() {
    this.cancellation.cancel();
    return this.done;
  }

  /** The extension host died under this request (a window reload): whatever the handler did last, its answer is never stored. */
  crash() {
    this.crashed = true;
    this.cancellation.cancel();
    return this.done;
  }

  /** The "Continue"-style follow-ups the participant offers under this answer. */
  followups() {
    const provider = this.thread.chat.participant.followupProvider;
    return provider ? provider.provideFollowups(this.result, { history: this.thread.turns }, this.token) : [];
  }
}

/** One Chat thread: a transcript that survives reloads, and at most one request in flight (the rest are queued). */
class ChatThread {
  constructor(chat) {
    this.chat = chat;
    /** The turns VS Code keeps for this thread: request and response turns in order. */
    this.turns = [];
    this.inFlight = null;
    this.queue = [];
  }

  /** The thread id the participant left in the newest answer that has one. */
  get id() {
    return threadIdFromHistory(this.turns);
  }

  historyForNextRequest() {
    return this.turns.map((turn) => ({ ...turn }));
  }

  /**
   * Going back in the chat ("Restore Checkpoint", or editing an earlier request and sending it again): the transcript
   * keeps the first `requests` requests with their answers and loses everything after them. Nothing of the extension's
   * own files changes: it only learns of it from the history of the next request.
   */
  rewindTo(requests) {
    this.turns.length = Math.min(this.turns.length, requests * 2);
  }

  /** "Delete Request": request number `number` (1 is the first) and its answer are taken out of the middle of the transcript. */
  removeRequest(number) {
    this.turns.splice((number - 1) * 2, 2);
  }

  /**
   * Forking the chat: a new Chat thread whose transcript is a copy of this one (its answers carry the same thread id and
   * marks, because they are the same answers), up to the first `requests` requests (all of them by default).
   */
  fork(requests = this.turns.length / 2) {
    const copy = this.chat.thread();
    copy.turns = JSON.parse(JSON.stringify(this.turns.slice(0, requests * 2)));
    return copy;
  }

  /** Sends a message now, whatever else is running (no queueing): VS Code does this when nothing is in flight. */
  send(prompt, options = {}) {
    const request = new ChatRequest(this, prompt, options);
    this.inFlight = request;
    this.chat.requests.push(request);
    return request;
  }

  /** `send`, awaited until VS Code takes the request as over. */
  async ask(prompt, options = {}) {
    const request = this.send(prompt, options);
    await request.done;
    return request;
  }

  /**
   * Enter during a request ("Steer with Message", the default): queued behind it, and the request in flight is asked to
   * yield. With nothing in flight it is sent at once. Resolves with the ChatRequest when the message is finally sent.
   */
  steer(prompt, options = {}) {
    return this.enqueue(prompt, options, true);
  }

  /**
   * "Add to Queue" (Alt+Enter, Enter when chat.requestQueuing.defaultAction is "queue", and what a message sent through
   * the `workbench.action.chat.open` command does, seen live in VS Code 1.143.1): queued behind the request in flight,
   * which is NOT asked to yield and so runs to its end first.
   */
  queued(prompt, options = {}) {
    return this.enqueue(prompt, options, false);
  }

  enqueue(prompt, options, yields) {
    if (!this.inFlight) return Promise.resolve(this.send(prompt, options));
    return new Promise((resolve) => {
      this.queue.push({ prompt, options, resolve });
      if (yields) this.inFlight.yieldRequested = true;
    });
  }

  /** "Stop and Send": the request in flight is cancelled, VS Code waits its grace, and the message goes even if the handler runs on. */
  async stopAndSend(prompt, options = {}) {
    const running = this.inFlight;
    if (running) {
      running.cancellation.cancel();
      await running.done;
    }
    return this.send(prompt, options);
  }

  /** VS Code's side of a request ending: it joins the transcript, and queued messages are sent if the request ended well. */
  finish(request) {
    this.turns.push(
      { prompt: request.prompt, command: request.command, participant: 'dlne.agent', references: [], toolReferences: [] },
      {
        response: request.stream.parts.filter((p) => p.kind === 'markdown').map((p) => ({ value: { value: String(p.value) } })),
        result: request.result,
        participant: 'dlne.agent',
        command: request.command,
      }
    );
    if (this.inFlight === request) this.inFlight = null;
    const well = !request.result.errorDetails && !request.token.isCancellationRequested;
    if (well && this.queue.length > 0 && !this.inFlight) {
      const queued = this.queue.splice(0);
      const next = this.send(queued.map((q) => q.prompt).join('\n\n'), queued[0].options);
      queued.forEach((q) => q.resolve(next));
    }
  }
}

/**
 * A VS Code window with the real participant registered over the storage folder `storage`. `client` is what the
 * participant talks to (scriptedClient, or a real DlneClient against a test server).
 */
function openChat({ client, storage, workerManager, grace = 1000 } = {}) {
  const chat = {
    storage: storage ?? tmpDir('dlne-harness-storage-'),
    client,
    grace,
    requests: [],
    threads: [],
    workerManager: workerManager ?? {
      onStatusChange() {},
      getStatus: () => ({ state: 'idle', gpu: { name: 'GPU', api: 'vulkan', vramTotalGb: 8 }, vramContributedGb: 0, cpuPool: false }),
      toggle: async () => true,
    },

    /** A new Chat thread. */
    thread() {
      const thread = new ChatThread(chat);
      chat.threads.push(thread);
      return thread;
    },

    /**
     * The window is reloaded: every request still running is cancelled (the extension host that ran it is gone, so its answer
     * is never stored: the transcript keeps the request with an empty result), and a new participant is registered over the
     * same storage. The transcripts of the threads stay, as VS Code keeps them.
     */
    async reload() {
      await Promise.all(chat.requests.filter((r) => r.result === undefined).map((r) => r.crash()));
      chat.register();
    },

    /** Registers the participant (a new one per call): `chat.handler`, `chat.participant`, `chat.context`. */
    register() {
      chat.context = fakeContext(chat.storage);
      registerNativeChatParticipant(chat.context, chat.client, chat.workerManager);
      chat.handler = registering.handler;
      chat.participant = registering.participant;
    },

    /** The saved task of a thread, as parsed JSON, or undefined. */
    savedTask(thread) {
      const id = typeof thread === 'string' ? thread : thread.id;
      try {
        return JSON.parse(fs.readFileSync(agentThreadFile(chat.context, id), 'utf8'));
      } catch {
        return undefined;
      }
    },

    /** The thread files on disk (names only), oldest naming irrelevant. */
    threadFiles() {
      try {
        return fs.readdirSync(agentThreadsDir(chat.context)).filter((name) => name.endsWith('.json'));
      } catch {
        return [];
      }
    },

    /** The single file the participant used before each thread had its own. */
    legacyFile() {
      return agentStateFile(chat.context, 'chat');
    },

    /** The lines written to the DLNE output channel so far. */
    log() {
      return stub.state.channels.filter((c) => c.name === 'DLNE').flatMap((c) => c.lines);
    },
  };
  chat.register();
  return chat;
}

/** The ExtensionContext fields the participant uses, over a storage folder. */
function fakeContext(storage) {
  const memento = new Map();
  return {
    extensionUri: { fsPath: EXT_ROOT },
    storageUri: { fsPath: storage },
    globalStorageUri: { fsPath: path.join(storage, 'global') },
    subscriptions: [],
    workspaceState: { get: (k) => memento.get(k), update: async (k, v) => { memento.set(k, v); } },
  };
}

/**
 * A workspace folder for the tools: `files` ({ 'a.txt': 'text' }) are written into a temp folder that becomes the open folder.
 * Call `restore()` when done. Remember `stub.reset()` first, and the approver (ToolRegistry.approver) for anything that asks.
 */
function workspace(files = {}) {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-harness-ws-')));
  for (const [name, text] of Object.entries(files)) {
    fs.mkdirSync(path.dirname(path.join(root, name)), { recursive: true });
    fs.writeFileSync(path.join(root, name), text);
  }
  stub.vscode.workspace.workspaceFolders = [{ uri: { fsPath: root } }];
  return {
    root,
    restore() {
      stub.vscode.workspace.workspaceFolders = undefined;
      fs.rmSync(root, { recursive: true, force: true });
    },
  };
}

module.exports = { openChat, fakeContext, workspace, tmpDir, scriptedClient, toolCall, gate, until, userWordsIn, FakeStream, CancellationSource, stub };
