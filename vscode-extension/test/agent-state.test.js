'use strict';
/**
 * The saved task (docs/agent-memory.md section 8): the agent's conversation survives a VS Code restart, every Chat
 * thread has a conversation and a saved task of its own (a new thread starts with fresh working memory and never
 * touches another's), and Clear removes the saved copy along with the visible one. (Threads, steer and Stop through
 * the real participant: chat-threads.test.js.)
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');

const { openChat } = require('./helpers/chat-harness');
const stub = require('./helpers/vscode-stub');
const {
  STATE_MAX_AGE_MS, SAVE_EVERY_MS, MAX_SAVED_THREADS, agentStateFile, agentThreadFile, agentThreadsDir, saveAgentState, loadAgentState,
  deleteAgentState, isThreadId, listAgentThreads, pruneAgentThreads, AgentStateStore, sidebarSessionsDir,
} = require('../dist/agent/agentState');
const { SESSIONS_KEY, MAX_SESSIONS, historyKey } = require('../dist/views/sidebarSessions');
const { SidebarProvider } = require('../dist/views/sidebarProvider');

const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-state-'));
const stateOf = (extra = {}) => ({
  version: 1,
  savedAt: new Date().toISOString(),
  sessionId: 'session-1',
  messages: [{ role: 'user', content: 'fix the bug' }],
  userWords: ['fix the bug'],
  notes: '',
  ledger: {},
  promptBudget: 15360,
  charsPerToken: 3,
  ...extra,
});

test('the file lives in the workspace storage, or in the global one named for having no folder', () => {
  const folders = { storageUri: { fsPath: '/ws' }, globalStorageUri: { fsPath: '/global' } };
  assert.equal(agentStateFile(folders, 'chat'), path.join('/ws', 'agent-state-chat.json'));
  assert.equal(agentStateFile(folders, 'sidebar'), path.join('/ws', 'agent-state-sidebar.json'));
  const noFolder = { storageUri: undefined, globalStorageUri: { fsPath: '/global' } };
  assert.equal(agentStateFile(noFolder, 'chat'), path.join('/global', 'agent-state-chat-nofolder.json'));
  assert.equal(agentStateFile(noFolder, 'sidebar'), path.join('/global', 'agent-state-sidebar-nofolder.json'));
});

test('a save is atomic: the folder is created, no temp file stays, and only the owner can read it', () => {
  const dir = tmp();
  const file = path.join(dir, 'deeper', 'agent-state-chat.json');
  saveAgentState(file, stateOf());
  saveAgentState(file, stateOf({ notes: 'second save' }));
  assert.deepEqual(fs.readdirSync(path.dirname(file)), ['agent-state-chat.json']);
  assert.equal(JSON.parse(fs.readFileSync(file, 'utf8')).notes, 'second save');
  if (process.platform !== 'win32') {
    assert.equal(fs.statSync(file).mode & 0o777, 0o600);
  }
});

test('a saved state loads back as it was', () => {
  const file = path.join(tmp(), 's.json');
  const state = stateOf({ notes: 'found it in src/a.ts' });
  saveAgentState(file, state);
  assert.deepEqual(loadAgentState(file), state);
});

test('a missing or malformed file gives null', () => {
  const dir = tmp();
  assert.equal(loadAgentState(path.join(dir, 'nothing.json')), null);
  const bad = path.join(dir, 'bad.json');
  for (const text of ['', '{not json', 'null', '[]', '"text"', '{"version":1}', '{"savedAt":"yesterday-ish"}']) {
    fs.writeFileSync(bad, text);
    assert.equal(loadAgentState(bad), null, text);
  }
});

test('a state saved more than 7 days ago is deleted, one just inside the week is kept', () => {
  const file = path.join(tmp(), 's.json');
  const savedAt = Date.parse('2026-10-01T00:00:00Z');
  saveAgentState(file, stateOf({ savedAt: new Date(savedAt).toISOString() }));
  assert.notEqual(loadAgentState(file, savedAt + STATE_MAX_AGE_MS - 1000), null);
  assert.ok(fs.existsSync(file));
  assert.equal(loadAgentState(file, savedAt + STATE_MAX_AGE_MS + 1000), null);
  assert.equal(fs.existsSync(file), false);
});

test('delete removes the file, and a missing one is fine', () => {
  const file = path.join(tmp(), 's.json');
  saveAgentState(file, stateOf());
  deleteAgentState(file);
  assert.equal(fs.existsSync(file), false);
  assert.doesNotThrow(() => deleteAgentState(file));
});

// A stand-in for the AgentLoop that holds a conversation of the given messages.
const loopHolding = (messages) => ({
  messages,
  exportState: () => stateOf({ messages: [...messages] }),
  importState(json) {
    this.messages = json.messages;
    return true;
  },
});

test('a thread id is a plain token, because it names a file: no path, no dots, nothing from a tampered chat file', () => {
  assert.equal(isThreadId('3f9a21c4-5d7e-4b1a-9c3d-0a1b2c3d4e5f'), true);
  assert.equal(isThreadId('abcdefgh'), true);
  for (const bad of ['', 'short', '../../etc/passwd', 'a/b/c/d/e/f/g/h', 'ok-but with space', 'dots.in.it.json', 'x'.repeat(65), 42, null, undefined, {}]) {
    assert.equal(isThreadId(bad), false, String(bad));
  }
  const folders = { storageUri: { fsPath: '/ws' }, globalStorageUri: { fsPath: '/global' } };
  assert.equal(agentThreadFile(folders, 'abcdefgh-1'), path.join('/ws', 'agent-threads', 'abcdefgh-1.json'));
  assert.equal(agentThreadFile({ globalStorageUri: { fsPath: '/global' } }, 'abcdefgh-1'), path.join('/global', 'agent-threads-nofolder', 'abcdefgh-1.json'));
  assert.throws(() => agentThreadFile(folders, '../../x'), /not a thread id/);
});

test('the newest 20 saved threads are kept: older ones, week-old ones and a crash\'s temp files go, threads in use stay', () => {
  const dir = path.join(tmp(), 'agent-threads');
  fs.mkdirSync(dir, { recursive: true });
  const now = Date.parse('2026-10-28T12:00:00Z');
  const make = (id, ageMs) => {
    const file = path.join(dir, `${id}.json`);
    fs.writeFileSync(file, '{}');
    fs.utimesSync(file, new Date(now - ageMs), new Date(now - ageMs));
    return file;
  };
  for (let n = 1; n <= 24; n++) make(`thread-${String(n).padStart(2, '0')}`, n * 60_000); // thread-01 is the newest
  make('thread-old', STATE_MAX_AGE_MS + 5000);
  const stale = path.join(dir, 'thread-01.json.4242.tmp');
  fs.writeFileSync(stale, '{');
  fs.utimesSync(stale, new Date(now - 2 * 3600_000), new Date(now - 2 * 3600_000));
  const fresh = path.join(dir, 'thread-02.json.4243.tmp');
  fs.writeFileSync(fresh, '{');
  fs.utimesSync(fresh, new Date(now - 60_000), new Date(now - 60_000));
  fs.writeFileSync(path.join(dir, 'notes.txt'), 'not ours');

  pruneAgentThreads(dir, MAX_SAVED_THREADS, new Set(['thread-24']), now);
  const left = listAgentThreads(dir).map((t) => t.id);
  assert.equal(MAX_SAVED_THREADS, 20);
  assert.deepEqual(left.slice(0, 20), Array.from({ length: 20 }, (_, i) => `thread-${String(i + 1).padStart(2, '0')}`), 'the newest 20, newest first');
  assert.ok(left.includes('thread-24'), 'a thread open in this window is never deleted');
  assert.ok(!left.includes('thread-21') && !left.includes('thread-22') && !left.includes('thread-23'), 'the older ones are gone');
  assert.ok(!left.includes('thread-old'), 'a task over a week old would not be loaded anyway');
  assert.equal(fs.existsSync(stale), false, 'a temp file a crash left, hours old');
  assert.equal(fs.existsSync(fresh), true, 'a temp file a save may still be writing');
  assert.equal(fs.existsSync(path.join(dir, 'notes.txt')), true, 'a file that is not a thread\'s is left alone');
  assert.doesNotThrow(() => pruneAgentThreads(path.join(dir, 'missing'), 20, new Set(), now));
});

/**
 * Makes every delete of a saved task or a temp file fail the way it does on Windows while a virus scanner or an indexer holds
 * the file open (EBUSY, EPERM), until `restore()`. Done on fs itself, so it works the same as root and on every platform.
 */
function undeletable() {
  const real = fs.rmSync;
  fs.rmSync = (target, options) => {
    if (/\.(json|tmp)$/.test(String(target))) throw Object.assign(new Error(`EBUSY: resource busy or locked, unlink '${target}'`), { code: 'EBUSY' });
    return real(target, options);
  };
  return { restore() { fs.rmSync = real; } };
}

test('housekeeping that cannot delete a file reports it once per pass and kind, stops nothing, and tries again next time', () => {
  const dir = path.join(tmp(), 'agent-threads');
  fs.mkdirSync(dir, { recursive: true });
  const now = Date.parse('2026-10-28T12:00:00Z');
  for (let n = 1; n <= 24; n++) {
    const file = path.join(dir, `thread-${String(n).padStart(2, '0')}.json`);
    fs.writeFileSync(file, '{}');
    fs.utimesSync(file, new Date(now - n * 60_000), new Date(now - n * 60_000));
  }
  const stale = path.join(dir, 'thread-01.json.4242.tmp');
  fs.writeFileSync(stale, '{');
  fs.utimesSync(stale, new Date(now - 2 * 3600_000), new Date(now - 2 * 3600_000));

  const reports = [];
  const stuck = undeletable();
  try {
    assert.doesNotThrow(() => pruneAgentThreads(dir, MAX_SAVED_THREADS, new Set(), now, (what, err) => reports.push([what, err.code])));
  } finally {
    stuck.restore();
  }
  assert.deepEqual(reports, [['delete 4 old saved tasks', 'EBUSY'], ['remove 1 leftover temp file', 'EBUSY']], 'one line each, not one per file');
  assert.equal(listAgentThreads(dir).length, 24, 'nothing could be deleted, and nothing else went wrong');

  pruneAgentThreads(dir, MAX_SAVED_THREADS, new Set(), now, () => assert.fail('nothing fails once the files are free'));
  assert.equal(listAgentThreads(dir).length, 20, 'the next pass deletes them');
  assert.equal(fs.existsSync(stale), false);
  assert.doesNotThrow(() => pruneAgentThreads(dir, 20, new Set(), now), 'and with nobody to report to');
});

test('an expired saved task that cannot be deleted is reported and still not loaded, and a task taken over keeps working when its old file stays', () => {
  const dir = tmp();
  const expired = path.join(dir, 'expired.json');
  saveAgentState(expired, stateOf({ savedAt: new Date(Date.now() - STATE_MAX_AGE_MS - 5000).toISOString() }));
  const reports = [];
  let stuck = undeletable();
  try {
    assert.equal(loadAgentState(expired, Date.now(), (what, err) => reports.push([what, err.code])), null, 'a week-old task is not what the user comes back to');
    assert.doesNotThrow(() => loadAgentState(expired, Date.now()), 'nobody listening is fine too');
  } finally {
    stuck.restore();
  }
  assert.deepEqual(reports, [['delete an expired saved task', 'EBUSY']]);
  assert.equal(fs.existsSync(expired), true, 'it is still there: the next load tries again');

  const other = path.join(dir, 'old', 'agent-state-chat.json');
  saveAgentState(other, stateOf({ messages: [{ role: 'user', content: 'from before' }] }));
  const loop = { messages: [], exportState() { return stateOf({ messages: [...this.messages] }); }, importState(json) { this.messages = json.messages; return true; } };
  const mine = path.join(dir, 'threads', 'abcdefgh-1.json');
  reports.length = 0;
  stuck = undeletable();
  try {
    assert.equal(new AgentStateStore(mine, loop, Date.now, (what, err) => reports.push([what, err.code])).adopt(other), true);
  } finally {
    stuck.restore();
  }
  assert.deepEqual(reports, [['delete the saved task that was taken over', 'EBUSY']]);
  assert.equal(loop.messages[0].content, 'from before', 'the conversation was taken over all the same');
  assert.equal(fs.existsSync(mine), true);
});

test('a store takes another file\'s saved task as its own, once: written to its file, the other one deleted', () => {
  const dir = tmp();
  const other = path.join(dir, 'old', 'agent-state-chat.json');
  saveAgentState(other, stateOf({ sessionId: 'session-from-before', messages: [{ role: 'user', content: 'from before' }] }));
  const mine = path.join(dir, 'threads', 'abcdefgh-1.json');
  const loop = { messages: [], exportState() { return stateOf({ messages: [...this.messages] }); }, importState(json) { this.messages = json.messages; return true; } };
  const store = new AgentStateStore(mine, loop);
  assert.equal(store.adopt(other), true);
  assert.equal(loop.messages[0].content, 'from before');
  assert.equal(fs.existsSync(mine), true);
  assert.equal(fs.existsSync(other), false);
  assert.equal(store.adopt(other), false, 'nothing left to take');
  assert.equal(new AgentStateStore(undefined, loop).adopt(other), false, 'a store with no file takes nothing');
  assert.equal(new AgentStateStore(mine, loopHolding([])).adopt(undefined), false);
});

test('the store saves at most every 5 seconds, always at the end of a run, and an empty conversation leaves no file', () => {
  const file = path.join(tmp(), 's.json');
  let clock = 1_000_000;
  const loop = loopHolding([{ role: 'user', content: 'one' }]);
  const store = new AgentStateStore(file, loop, () => clock);

  store.changed();
  assert.equal(JSON.parse(fs.readFileSync(file, 'utf8')).messages.length, 1);
  loop.messages.push({ role: 'assistant', content: 'two' });
  clock += SAVE_EVERY_MS - 1;
  store.changed();
  assert.equal(JSON.parse(fs.readFileSync(file, 'utf8')).messages.length, 1, 'too soon to save again');
  clock += 1;
  store.changed();
  assert.equal(JSON.parse(fs.readFileSync(file, 'utf8')).messages.length, 2);

  loop.messages.push({ role: 'user', content: 'three' });
  store.flush();
  assert.equal(JSON.parse(fs.readFileSync(file, 'utf8')).messages.length, 3, 'the end of a run saves at once');

  store.clear();
  assert.equal(fs.existsSync(file), false);
  loop.messages.length = 0;
  store.flush();
  assert.equal(fs.existsSync(file), false);
});

test('a store that cannot write does not break the chat, and one without a file does nothing', () => {
  const dir = tmp();
  const blocker = path.join(dir, 'a-file');
  fs.writeFileSync(blocker, '');
  const loop = loopHolding([{ role: 'user', content: 'x' }]);
  assert.doesNotThrow(() => new AgentStateStore(path.join(blocker, 'inside', 's.json'), loop).flush());
  const none = new AgentStateStore(undefined, loop);
  assert.equal(none.load(), false);
  assert.doesNotThrow(() => { none.changed(); none.flush(); none.clear(); });
});

// ---- the chats ----

const answeringClient = () => ({
  streamChat: async (_messages, _tools, onDelta) => {
    onDelta('done');
    return { content: 'done', toolCalls: [] };
  },
});
const workerManager = { onStatusChange() {}, getStatus: () => ({ state: 'idle', gpu: { name: 'GPU', api: 'vulkan', vramTotalGb: 8 }, vramContributedGb: 0, cpuPool: false }) };

function fakeContext(storage) {
  const memento = new Map();
  return {
    extensionUri: { fsPath: path.join(__dirname, '..') },
    storageUri: { fsPath: storage },
    globalStorageUri: { fsPath: path.join(storage, 'global') },
    subscriptions: [],
    workspaceState: { get: (k) => memento.get(k), update: async (k, v) => { memento.set(k, v); } },
  };
}

function openSidebar(context, client = answeringClient()) {
  const provider = new SidebarProvider(context.extensionUri, client, workerManager, context);
  let onMessage;
  provider.resolveWebviewView(
    { webview: { options: {}, html: '', asWebviewUri: (u) => u, postMessage() {}, onDidReceiveMessage: (fn) => { onMessage = fn; } } },
    {},
    {}
  );
  return { provider, send: (message) => onMessage(message) };
}

/** The single sidebar chat's file of before 1.5.3, read once to become the first session. */
const legacySidebarFile = (context) => agentStateFile(context, 'sidebar');
/** The sessions as the workspace state keeps them (sidebarSessions.ts). */
const sessionIndex = (context) => context.workspaceState.get(SESSIONS_KEY);
/** A sidebar session's saved task (1.5.3): the shown one's unless an id is given. */
const sidebarFile = (context, id = sessionIndex(context).active) => path.join(sidebarSessionsDir(context), `${id}.json`);

test('VS Code chat: a task survives a restart, Continue works, a new thread forgets, /clear deletes the file', async () => {
  stub.reset();
  const chat = openChat({ client: answeringClient() });

  const thread = chat.thread();
  await thread.ask('fix the bug');
  const saved = chat.savedTask(thread);
  assert.ok(saved, 'saved when the run ended');
  assert.ok(saved.messages.some((m) => m.content === 'fix the bug'));

  // Restart: a new participant over the same storage. VS Code reopens the thread with its history, ids included.
  await chat.reload();
  await thread.ask('continue');
  const afterContinue = chat.savedTask(thread);
  assert.equal(afterContinue.sessionId, saved.sessionId, 'the same conversation carried on');
  assert.ok(afterContinue.messages.some((m) => m.content === 'fix the bug'), 'the old request is still known');

  // A brand-new thread (no history): fresh working memory, a new conversation, and the first thread keeps its own.
  const other = chat.thread();
  await other.ask('something else');
  const fresh = chat.savedTask(other);
  assert.notEqual(fresh.sessionId, saved.sessionId);
  assert.equal(fresh.messages.some((m) => m.content === 'fix the bug'), false);
  assert.ok(fresh.messages.some((m) => m.content === 'something else'));
  assert.equal(chat.savedTask(thread).sessionId, saved.sessionId, 'the other thread did not touch it');

  await thread.ask('', { command: 'clear' });
  assert.equal(chat.savedTask(thread), undefined, '/clear deletes the thread\'s saved task');
  assert.ok(chat.savedTask(other), 'and only that thread\'s');
});

test('VS Code chat: /clear with no saved task, and a first-ever request, both leave things working', async () => {
  stub.reset();
  const chat = openChat({ client: answeringClient() });
  const thread = chat.thread();
  await thread.ask('', { command: 'clear' });
  assert.deepEqual(chat.threadFiles(), []);
  await thread.ask('hello');
  assert.equal(chat.threadFiles().length, 1);
  assert.equal(fs.existsSync(chat.legacyFile()), false, 'the single per-workspace file is not written any more');
});

test('sidebar: a reload restores the agent with the visible chat, and New chat keeps it in the list', async () => {
  stub.reset();
  const context = fakeContext(tmp());

  const first = openSidebar(context);
  await first.provider.handleUserMessage('write a parser');
  const parser = sessionIndex(context).active;
  const saved = JSON.parse(fs.readFileSync(sidebarFile(context), 'utf8'));
  assert.ok(saved.messages.some((m) => m.content === 'write a parser'));
  assert.equal(sessionIndex(context).sessions[0].title, 'write a parser', 'the first words name the session');

  const reloaded = openSidebar(context);
  assert.equal(reloaded.provider.conversationHistory.length, 2, 'the visible chat came back');
  assert.equal(reloaded.provider.agentLoop.getSessionId(), saved.sessionId, 'and so did the agent');

  await reloaded.send({ type: 'newSession' });
  assert.notEqual(sessionIndex(context).active, parser, 'a new session is shown');
  assert.equal(reloaded.provider.conversationHistory.length, 0);
  assert.notEqual(reloaded.provider.agentLoop.getSessionId(), saved.sessionId);
  assert.ok(fs.existsSync(sidebarFile(context, parser)), 'the old one keeps its saved task');
  assert.equal(context.workspaceState.get(historyKey(parser)).length, 2, 'and its chat');
  await reloaded.send({ type: 'newSession' });
  assert.equal(sessionIndex(context).sessions.length, 2, 'an empty session is reused, not piled up');

  const again = openSidebar(context);
  assert.equal(again.provider.conversationHistory.length, 0, 'the new session is the one shown after a reload');
  await again.send({ type: 'openSession', id: parser });
  assert.equal(again.provider.conversationHistory.length, 2, 'picked from the list, the old chat is back');
  assert.equal(again.provider.agentLoop.getSessionId(), saved.sessionId, 'with its agent');
});

test('sidebar: a deleted session takes its saved task and chat with it; deleting the shown one shows the newest', async () => {
  stub.reset();
  const context = fakeContext(tmp());
  const sidebar = openSidebar(context);
  await sidebar.provider.handleUserMessage('first task');
  const first = sessionIndex(context).active;
  await sidebar.send({ type: 'newSession' });
  await sidebar.provider.handleUserMessage('second task');
  const second = sessionIndex(context).active;

  await sidebar.send({ type: 'deleteSession', id: second });
  assert.equal(fs.existsSync(sidebarFile(context, second)), false);
  assert.equal(context.workspaceState.get(historyKey(second)), undefined);
  assert.deepEqual(sessionIndex(context).sessions.map((s) => s.id), [first]);
  assert.equal(sessionIndex(context).active, first);
  assert.equal(sidebar.provider.conversationHistory[0].text, 'first task');

  await sidebar.send({ type: 'deleteSession', id: first });
  assert.equal(sessionIndex(context).sessions.length, 1, 'the last one deleted: an empty one is shown');
  assert.equal(sidebar.provider.conversationHistory.length, 0);
  await sidebar.send({ type: 'deleteSession', id: '../../etc/passwd' });
  assert.equal(sessionIndex(context).sessions.length, 1, 'an id that is not a session is ignored');
});

test('sidebar: past MAX_SESSIONS the oldest goes, never the one shown', async () => {
  stub.reset();
  const context = fakeContext(tmp());
  const sidebar = openSidebar(context);
  const ids = [];
  for (let i = 0; i <= MAX_SESSIONS; i++) {
    if (i > 0) await sidebar.send({ type: 'newSession' });
    await sidebar.provider.handleUserMessage(`task ${i}`);
    ids.push(sessionIndex(context).active);
    sessionIndex(context).sessions.find((s) => s.id === ids[i]).updated = i;     // a clear order, whatever the clock did
  }
  await sidebar.send({ type: 'newSession' });
  const kept = sessionIndex(context).sessions.map((s) => s.id);
  assert.equal(kept.length, MAX_SESSIONS);
  assert.equal(kept.includes(ids[0]) || kept.includes(ids[1]), false, 'the two oldest went');
  assert.equal(fs.existsSync(sidebarFile(context, ids[0])), false);
  assert.ok(kept.includes(ids[MAX_SESSIONS]));
});

test('sidebar: the single chat of before 1.5.3 becomes the first session, its saved task with it', async () => {
  stub.reset();
  const context = fakeContext(tmp());
  const before = openSidebar(context);
  await before.provider.handleUserMessage('old task');
  // What 1.5.2 left: the chat under dlne.chatHistory and the saved task in agent-state-sidebar.json.
  const active = sessionIndex(context).active;
  fs.renameSync(sidebarFile(context), legacySidebarFile(context));
  await context.workspaceState.update('dlne.chatHistory', context.workspaceState.get(historyKey(active)));
  await context.workspaceState.update(SESSIONS_KEY, undefined);
  await context.workspaceState.update(historyKey(active), undefined);
  const savedId = JSON.parse(fs.readFileSync(legacySidebarFile(context), 'utf8')).sessionId;

  const after = openSidebar(context);
  assert.equal(after.provider.conversationHistory.length, 2, 'the chat came over');
  assert.equal(after.provider.agentLoop.getSessionId(), savedId, 'and the agent');
  assert.equal(sessionIndex(context).sessions[0].title, 'old task');
  assert.equal(fs.existsSync(legacySidebarFile(context)), false, 'the old file went');
  assert.ok(fs.existsSync(sidebarFile(context)));
  assert.equal(context.workspaceState.get('dlne.chatHistory'), undefined);
});

test('sidebar: a saved task with no visible chat is dropped, so the page and the agent agree', async () => {
  stub.reset();
  const context = fakeContext(tmp());
  const first = openSidebar(context);
  await first.provider.handleUserMessage('write a parser');
  await context.workspaceState.update(historyKey(sessionIndex(context).active), []);

  const reloaded = openSidebar(context);
  assert.equal(fs.existsSync(sidebarFile(context)), false);
  assert.notEqual(reloaded.provider.agentLoop.getSessionId(), JSON.parse(JSON.stringify(first.provider.agentLoop.getSessionId())));
});

test('the two chats keep separate saved tasks', async () => {
  stub.reset();
  const storage = tmp();
  const chat = openChat({ client: answeringClient(), storage });
  const thread = chat.thread();
  await thread.ask('chat task');
  const context = fakeContext(storage);
  await openSidebar(context).provider.handleUserMessage('sidebar task');
  assert.ok(chat.savedTask(thread));
  assert.ok(fs.existsSync(sidebarFile(context)));
  assert.notEqual(agentThreadFile(context, thread.id), sidebarFile(context));
  assert.ok(chat.savedTask(thread).messages.every((m) => m.content !== 'sidebar task'));
  assert.ok(JSON.parse(fs.readFileSync(sidebarFile(context), 'utf8')).messages.every((m) => m.content !== 'chat task'));
});
