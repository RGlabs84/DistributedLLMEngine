'use strict';
/**
 * @dlne in VS Code's Chat view, one conversation per Chat thread (1.5.0, plan item 2), through the REAL participant and
 * a stand-in for the Chat view (test/helpers/chat-harness.js, which says what it reproduces of VS Code 1.143.1):
 * - every thread has its own agent conversation and saved task, found again by the id its answers carry, after a reload too;
 * - one run per thread: Enter during a run ("steer") makes it stop after its step and the message carries on; a message
 *   that arrives while the old run is still going waits for it;
 * - Stop ends a running command (its whole process tree) and a run waiting on a dialog, inside VS Code's one-second grace;
 * - an error offers Continue; the task is saved however a run ends; the run is written to the DLNE log (no folder names in it);
 * - a chat that was gone back in (Restore Checkpoint, editing an earlier request) or forked does not bring back the memory
 *   of the answers that were taken out: the thread carries on in a copy cut back to where the chat now stands;
 * - housekeeping (deleting old saved tasks) never fails a request;
 * - a stream that goes silent is ended and the step sent again.
 * The pieces under it are tested where they live: agent-state.test.js, agent-run-control.test.js, engine-drop.test.js.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const childProcess = require('node:child_process');

const { openChat, scriptedClient, toolCall, gate, until, userWordsIn, workspace, tmpDir, stub } = require('./helpers/chat-harness');
const { generator } = require('./helpers/fake-secrets');
const { ownProcesses } = require('./helpers/own-processes');
const { ToolRegistry } = require('../dist/agent/tools');
const { STATE_MAX_AGE_MS, agentThreadFile, agentThreadsDir, saveAgentState } = require('../dist/agent/agentState');
const { NEW_CONVERSATION_NOTE, readTranscript, threadIdFromHistory } = require('../dist/agent/chatThreads');
const { DlneClient, EngineDropped } = require('../dist/client/dlneClient');

const posix = { skip: process.platform === 'win32' && 'POSIX shell commands' };
const realExecute = ToolRegistry.executeTool;
const realApprover = ToolRegistry.approver;
const realSetTimeout = global.setTimeout;
test.afterEach(() => {
  ToolRegistry.executeTool = realExecute;
  ToolRegistry.approver = realApprover;
  global.setTimeout = realSetTimeout;
  stub.vscode.workspace.workspaceFolders = undefined;
});

/** The engine-drop pause is 2 s and up; a test does not sit it out (timers of a second or less keep their length). */
function quickPauses() {
  global.setTimeout = (fn, ms, ...args) => realSetTimeout(fn, ms >= 1000 ? 0 : ms, ...args);
}

const sleep = (ms) => new Promise((resolve) => realSetTimeout(resolve, ms));

/** The processes that run `sleep <marker>` (a sleep time no other process has): only ever the test's own. */
const running = ownProcesses;

// ---- threads ------------------------------------------------------------------------------------------------

test('two threads each have a conversation and a saved task of their own, and the second never stops the first', async () => {
  stub.reset();
  const hold = gate();
  const client = scriptedClient([async (req) => {
    const last = userWordsIn(req).at(-1);
    if (last === 'task A') await hold.opened;
    return { content: `answer to ${last}` };
  }]);
  const chat = openChat({ client });
  const a = chat.thread();
  const b = chat.thread();

  const runA = a.send('task A');
  await until(() => client.requests.length === 1, 4000, "thread A's first step");
  await b.ask('task B'); // thread B starts and finishes while A is still in its step
  assert.equal(client.requests[0].signal.aborted, false, 'the run in thread A was not stopped by thread B');
  assert.equal(runA.token.isCancellationRequested, false);
  hold.open();
  await runA.done;
  assert.match(runA.stream.text(), /answer to task A/);

  assert.ok(a.id && b.id && a.id !== b.id, 'every answer carries its thread\'s id');
  assert.deepEqual(chat.savedTask(a).messages.filter((m) => m.role === 'user').map((m) => m.content), ['task A']);
  assert.deepEqual(chat.savedTask(b).messages.filter((m) => m.role === 'user').map((m) => m.content), ['task B']);
  assert.notEqual(chat.savedTask(a).sessionId, chat.savedTask(b).sessionId, 'two conversations, two session ids');

  await a.ask('more A');
  assert.deepEqual(userWordsIn(client.requests.at(-1)), ['task A', 'more A'], 'A goes on with its own memory only');
  await b.ask('more B');
  assert.deepEqual(userWordsIn(client.requests.at(-1)), ['task B', 'more B']);
});

test('two threads can run at the same time', async () => {
  stub.reset();
  const holdA = gate();
  const holdB = gate();
  let inStep = 0;
  let mostAtOnce = 0;
  const client = scriptedClient([async (req) => {
    const last = userWordsIn(req).at(-1);
    mostAtOnce = Math.max(mostAtOnce, ++inStep);
    await (last === 'in A' ? holdA : holdB).opened;
    inStep--;
    return { content: `done ${last}` };
  }]);
  const chat = openChat({ client });
  const a = chat.thread().send('in A');
  const b = chat.thread().send('in B');
  await until(() => inStep === 2, 4000, 'both runs to be inside their step');
  holdA.open();
  holdB.open();
  await Promise.all([a.done, b.done]);
  assert.equal(mostAtOnce, 2);
  assert.match(a.stream.text(), /done in A/);
  assert.match(b.stream.text(), /done in B/);
});

test('a slash command in a new thread starts no conversation and wipes none (/model used to clear the agent), and the thread carries on', async () => {
  stub.reset();
  const client = scriptedClient([{ content: 'noted' }]);
  const chat = openChat({ client });
  const a = chat.thread();
  await a.ask('remember the plan');
  const savedBefore = chat.savedTask(a);

  const b = chat.thread();
  const slash = await b.ask('', { command: 'model' });
  assert.ok(slash.result.metadata.threadId, 'even a slash command names its thread');
  assert.equal(chat.threadFiles().length, 1, 'no saved task for a thread that has not talked to the agent');
  assert.equal(chat.savedTask(a).sessionId, savedBefore.sessionId, "thread A's memory is untouched");

  await b.ask('hello from b');
  assert.equal(b.id, slash.result.metadata.threadId, 'the thread the slash command began is the one that carries on');
  assert.deepEqual(userWordsIn(client.requests.at(-1)), ['hello from b']);
  await a.ask('what was the plan');
  assert.deepEqual(userWordsIn(client.requests.at(-1)), ['remember the plan', 'what was the plan']);
});

test('a window reload resumes a thread by the id its answers carry; a new thread after it starts empty', async () => {
  stub.reset();
  const client = scriptedClient([{ content: 'First answer.' }]);
  const chat = openChat({ client });
  const thread = chat.thread();
  await thread.ask('fix the bug');
  const id = thread.id;
  const before = chat.savedTask(thread);
  assert.ok(id);

  await chat.reload();
  await thread.ask('and now continue');
  assert.deepEqual(userWordsIn(client.requests.at(-1)), ['fix the bug', 'and now continue'], 'the new window knew the old conversation');
  assert.equal(chat.savedTask(thread).sessionId, before.sessionId);
  assert.equal(thread.id, id);
  assert.ok(chat.log().some((line) => /thread [0-9a-f]{8}: resumed from its saved task/.test(line)));

  await chat.thread().ask('hello');
  assert.deepEqual(userWordsIn(client.requests.at(-1)), ['hello']);
});

test('a reload while a thread\'s first request is running (its answer is never stored) still resumes it, by its first message', async () => {
  stub.reset();
  const ws = workspace({ 'a.txt': 'alpha' });
  try {
    const client = scriptedClient([
      { toolCalls: [toolCall('list_directory', { path: '.' })] },
      () => new Promise(() => {}), // the second step never answers: the window is reloaded while it waits
      { content: 'Carried on.' },
    ]);
    const chat = openChat({ client });
    const thread = chat.thread();
    const first = thread.send('build the thing');
    await until(() => client.requests.length === 2, 4000, 'the second step');
    await chat.reload();
    assert.equal(first.lost, true);
    assert.equal(thread.id, undefined, 'the interrupted answer left no id behind');
    const oldId = fs.readdirSync(path.join(chat.storage, 'agent-threads'))[0].replace('.json', '');

    const next = await thread.ask('continue');
    assert.equal(thread.id, oldId, 'the saved task was found again');
    const sent = client.requests.at(-1).messages;
    assert.deepEqual(userWordsIn(client.requests.at(-1)), ['build the thing', 'continue']);
    assert.ok(sent.some((m) => m.role === 'tool' && /a\.txt/.test(m.content)), 'with what the first step had found');
    assert.equal(next.result.metadata.threadId, oldId);
    assert.equal(chat.threadFiles().length, 1, 'one conversation, not two');
  } finally {
    ws.restore();
  }
});

test('a thread from before 1.5.0 (answers with no id) takes the saved task of that time when its first message matches, once', async () => {
  stub.reset();
  const client = scriptedClient([{ content: 'Carried on.' }]);
  const chat = openChat({ client });
  saveAgentState(chat.legacyFile(), {
    version: 1, savedAt: new Date().toISOString(), sessionId: 'session-before-1-5-0',
    messages: [{ role: 'user', content: 'lets continue the build' }, { role: 'assistant', content: 'The build is half done.' }],
    userWords: ['lets continue the build'], notes: '', ledger: {}, promptBudget: 0, charsPerToken: 3,
  });
  const oldTurns = (first) => [
    { prompt: first, command: undefined, participant: 'dlne.agent', references: [], toolReferences: [] },
    { response: [], result: { metadata: { command: 'chat', paused: false } }, participant: 'dlne.agent' },
  ];

  const stranger = chat.thread();
  stranger.turns = oldTurns('something else entirely');
  await stranger.ask('hello');
  assert.deepEqual(userWordsIn(client.requests.at(-1)), ['hello'], 'a thread that began with other words takes nothing');
  assert.equal(fs.existsSync(chat.legacyFile()), true, 'and the old file stays for its own thread');

  const owner = chat.thread();
  owner.turns = oldTurns('lets continue the build');
  await owner.ask('continue');
  assert.deepEqual(userWordsIn(client.requests.at(-1)), ['lets continue the build', 'continue']);
  assert.equal(chat.savedTask(owner).sessionId, 'session-before-1-5-0');
  assert.equal(fs.existsSync(chat.legacyFile()), false, 'the old file went with it: no second thread can take it');
  assert.ok(chat.log().some((line) => /took over the saved task from before/.test(line)));
});

test('a thread id that is not a plain token (a tampered chat file) is ignored: nothing is read or written outside the threads\' folder', async () => {
  stub.reset();
  const chat = openChat({ client: scriptedClient([{ content: 'ok' }]) });
  const thread = chat.thread();
  thread.turns = [
    { prompt: 'hi', participant: 'dlne.agent', references: [], toolReferences: [] },
    { response: [], result: { metadata: { threadId: '../../../../tmp/evil' } }, participant: 'dlne.agent' },
  ];
  assert.equal(threadIdFromHistory(thread.turns), undefined);
  const r = await thread.ask('hello');
  assert.match(r.result.metadata.threadId, /^[A-Za-z0-9-]{8,64}$/);
  assert.deepEqual(chat.threadFiles(), [`${r.result.metadata.threadId}.json`]);
});

test('saved tasks age out when the window starts, as before: the old single file and the threads\' own, a week after they were written', () => {
  stub.reset();
  const storage = tmpDir('dlne-harness-age-');
  const state = (savedAt) => ({
    version: 1, savedAt: new Date(savedAt).toISOString(), sessionId: 'session-old-one', messages: [{ role: 'user', content: 'old' }],
    userWords: ['old'], notes: '', ledger: {}, promptBudget: 0, charsPerToken: 3,
  });
  const eightDays = Date.now() - 8 * 24 * 3600_000;
  const chat = openChat({ client: scriptedClient([{ content: 'ok' }]), storage });
  saveAgentState(chat.legacyFile(), state(eightDays));
  const dir = path.join(storage, 'agent-threads');
  saveAgentState(path.join(dir, 'oldthread-aaaa.json'), state(eightDays));
  fs.utimesSync(path.join(dir, 'oldthread-aaaa.json'), new Date(eightDays), new Date(eightDays));
  saveAgentState(path.join(dir, 'newthread-bbbb.json'), state(Date.now()));

  openChat({ client: scriptedClient([{ content: 'ok' }]), storage }); // the window is reopened
  assert.equal(fs.existsSync(chat.legacyFile()), false);
  assert.deepEqual(fs.readdirSync(dir), ['newthread-bbbb.json']);
});

test('threadIdFromHistory reads the newest answer that has an id, and skips turns that are not answers', () => {
  const answer = (id) => ({ response: [], result: { metadata: id ? { threadId: id } : {} } });
  const request = { prompt: 'x' };
  assert.equal(threadIdFromHistory(undefined), undefined);
  assert.equal(threadIdFromHistory([]), undefined);
  assert.equal(threadIdFromHistory([request, answer('11111111-aaaa')]), '11111111-aaaa');
  assert.equal(threadIdFromHistory([request, answer('11111111-aaaa'), request, answer('22222222-bbbb')]), '22222222-bbbb');
  assert.equal(threadIdFromHistory([request, answer('11111111-aaaa'), request, answer(undefined)]), '11111111-aaaa', 'a lost result is looked past');
  assert.equal(threadIdFromHistory([request, { response: [], result: {} }]), undefined);
  assert.equal(threadIdFromHistory([null, undefined, 5, 'x']), undefined);
});

test('threads: the newest 20 saved tasks are kept as new ones come, and an old thread just starts a new conversation under its id', async () => {
  stub.reset();
  const client = scriptedClient([{ content: 'ok' }]);
  const chat = openChat({ client });
  const base = Date.now() - 200_000;
  const made = [];
  for (let n = 0; n < 24; n++) {
    const thread = chat.thread();
    await thread.ask(`thread number ${n}`);
    made.push(thread);
    // distinct, increasing file times: which are "the newest" must not depend on the clock's resolution
    fs.utimesSync(path.join(chat.storage, 'agent-threads', `${thread.id}.json`), new Date(base + n * 1000), new Date(base + n * 1000));
  }
  const files = chat.threadFiles();
  assert.equal(files.length, 20);
  for (const thread of made.slice(-20)) assert.ok(files.includes(`${thread.id}.json`), `thread ${made.indexOf(thread)} is among the newest`);
  for (const thread of made.slice(0, 3)) assert.ok(!files.includes(`${thread.id}.json`), `thread ${made.indexOf(thread)} is gone`);
  // a thread whose file was pruned (it was long idle) just starts a new conversation under the same id
  const old = made[0];
  const r = await old.ask('are you there');
  assert.equal(r.result.metadata.threadId, old.id);
  assert.deepEqual(userWordsIn(client.requests.at(-1)), ['are you there'], 'it has lost its memory, not its place');
});

// ---- a chat that was gone back in, or forked ----------------------------------------------------------------

/** The user's words in a thread's saved task. */
const wordsSaved = (chat, thread) => chat.savedTask(thread).userWords;
/** Everything the model was sent in `request`, as one string: to look for what must not be in it. */
const everythingIn = (request) => JSON.stringify(request.messages);
/** A model that answers ANSWER-<word> to each request in turn (the last repeating). */
const answering = (...words) => scriptedClient(words.map((word) => ({ content: `ANSWER-${word}` })));

test('going back in the chat (Restore Checkpoint, or editing an earlier request) does not bring back the answers that were taken out: the thread carries on in a copy cut back to where the chat now stands', async () => {
  stub.reset();
  const client = answering('ONE', 'TWO', 'THREE', 'FOUR', 'FIVE', 'SIX');
  const chat = openChat({ client });
  const thread = chat.thread();
  await thread.ask('first request');
  await thread.ask('second request');
  const third = await thread.ask('third request');
  const original = thread.id;
  assert.equal(third.result.metadata.turn, 3, 'each answer says which turn of the chat it is');
  assert.match(third.result.metadata.mark, /^[0-9a-f]{16}$/, 'and names the state of the conversation it left');
  assert.equal(chat.savedTask(original).thread.turn, 3, 'the saved task knows its turn too');
  assert.equal(chat.savedTask(original).thread.marks.length, 3);

  thread.rewindTo(1); // the chat goes back to before the second request
  const edited = await thread.ask('a different second request');
  const sent = client.requests.at(-1);
  assert.deepEqual(userWordsIn(sent), ['first request', 'a different second request']);
  assert.ok(everythingIn(sent).includes('ANSWER-ONE'), 'what is still in the chat is remembered');
  assert.ok(!everythingIn(sent).includes('ANSWER-TWO') && !everythingIn(sent).includes('ANSWER-THREE'), 'the model is not sent the answers that were taken out');
  const copy = edited.result.metadata.threadId;
  assert.notEqual(copy, original, 'the cut-back conversation is a copy: the original may be the other branch of a fork');
  assert.equal(edited.result.metadata.turn, 2);
  assert.equal(thread.id, copy, 'and the chat carries on with the copy');
  assert.deepEqual(wordsSaved(chat, original), ['first request', 'second request', 'third request'], 'the original is as it was');
  assert.deepEqual(wordsSaved(chat, copy), ['first request', 'a different second request']);
  assert.notEqual(chat.savedTask(copy).sessionId, chat.savedTask(original).sessionId, 'with a conversation id of its own');
  assert.equal(chat.savedTask(copy).thread.marks.length, 2, 'it keeps the marks up to where it was cut, and its own');
  assert.ok(chat.log().some((line) => new RegExp(`thread ${copy.slice(0, 8)}: a copy of thread ${original.slice(0, 8)} as the chat shows it now, cut back to its turn 1`).test(line)));
  assert.ok(!edited.stream.text().includes(NEW_CONVERSATION_NOTE), 'nothing was lost, so nothing is said');

  await thread.ask('and a third');
  assert.deepEqual(userWordsIn(client.requests.at(-1)), ['first request', 'a different second request', 'and a third'], 'it goes on in the copy, without cutting again');
  assert.equal(thread.id, copy);
  assert.equal(chat.threadFiles().length, 2);
});

test('editing the last request and sending it again cuts the conversation back by that one answer, tool steps and all', async () => {
  stub.reset();
  const ws = workspace({ 'a.txt': 'alpha' });
  try {
    const client = scriptedClient([
      { content: 'ANSWER-ONE' },
      { toolCalls: [toolCall('read_file', { path: 'a.txt' })] },
      { content: 'ANSWER-TWO, after reading the file' },
      { content: 'ANSWER-THREE' },
    ]);
    const chat = openChat({ client });
    const thread = chat.thread();
    await thread.ask('first request');
    await thread.ask('please read a.txt');
    thread.rewindTo(1); // the second request is edited: VS Code drops it and its answer
    await thread.ask('please read b.txt instead');
    const sent = client.requests.at(-1);
    assert.deepEqual(userWordsIn(sent), ['first request', 'please read b.txt instead']);
    assert.equal(sent.messages.some((m) => m.role === 'tool'), false, 'what the removed step read is not sent either');
    assert.ok(!everythingIn(sent).includes('a.txt') && !everythingIn(sent).includes('ANSWER-TWO'));
  } finally {
    ws.restore();
  }
});

test('a forked chat has the same thread id as the original, so the branch that runs second gets a copy of its own, cut back to where the chat was forked', async () => {
  stub.reset();
  const client = answering('ONE', 'TWO', 'THREE', 'FOUR', 'FIVE', 'SIX', 'SEVEN');
  const chat = openChat({ client });
  const a = chat.thread();
  await a.ask('shared one');
  await a.ask('shared two');
  const original = a.id;
  const b = a.fork();
  assert.equal(b.id, original, 'the same answers carry the same id');

  await a.ask('A goes on');
  assert.equal(a.id, original, 'the branch that runs first keeps the thread');
  await b.ask('B goes on');
  assert.notEqual(b.id, original, 'the other gets a copy');
  assert.deepEqual(userWordsIn(client.requests.at(-1)), ['shared one', 'shared two', 'B goes on']);
  assert.ok(!everythingIn(client.requests.at(-1)).includes('A goes on'), 'nothing of the other branch');

  await a.ask('A again');
  assert.deepEqual(userWordsIn(client.requests.at(-1)), ['shared one', 'shared two', 'A goes on', 'A again']);
  await b.ask('B again');
  assert.deepEqual(userWordsIn(client.requests.at(-1)), ['shared one', 'shared two', 'B goes on', 'B again']);
  assert.equal(chat.threadFiles().length, 2, 'two conversations, one each');
  assert.deepEqual(wordsSaved(chat, original), ['shared one', 'shared two', 'A goes on', 'A again']);
});

test('a fork that runs first keeps the thread, and the original (now the one that lags) gets the copy', async () => {
  stub.reset();
  const client = answering('ONE', 'TWO', 'THREE', 'FOUR', 'FIVE');
  const chat = openChat({ client });
  const a = chat.thread();
  await a.ask('shared one');
  const original = a.id;
  const b = a.fork();
  await b.ask('B goes on');
  assert.equal(b.id, original);
  await a.ask('A goes on');
  assert.notEqual(a.id, original);
  assert.deepEqual(userWordsIn(client.requests.at(-1)), ['shared one', 'A goes on']);
  await b.ask('B again');
  assert.deepEqual(userWordsIn(client.requests.at(-1)), ['shared one', 'B goes on', 'B again']);
});

test('a request taken out of the middle of the chat cannot be followed: the answer starts a new conversation, says so, and leaves the old one alone', async () => {
  stub.reset();
  const client = answering('ONE', 'TWO', 'THREE', 'FOUR', 'FIVE');
  const chat = openChat({ client });
  const thread = chat.thread();
  await thread.ask('one');
  await thread.ask('two');
  await thread.ask('three');
  const original = thread.id;
  thread.removeRequest(2);

  const next = await thread.ask('four');
  assert.deepEqual(userWordsIn(client.requests.at(-1)), ['four'], 'the model is sent none of the old conversation');
  assert.ok(next.stream.text().startsWith(NEW_CONVERSATION_NOTE), 'and the user is told before the answer');
  assert.notEqual(thread.id, original);
  assert.deepEqual(wordsSaved(chat, original), ['one', 'two', 'three']);
  assert.ok(chat.log().some((line) => /a new conversation: a request was taken out of the middle of the chat \(thread [0-9a-f]{8} is left as it was\)/.test(line)));

  const after = await thread.ask('five');
  assert.deepEqual(userWordsIn(client.requests.at(-1)), ['four', 'five'], 'the new conversation goes on');
  assert.ok(!after.stream.text().includes(NEW_CONVERSATION_NOTE), 'the note is said once');
});

test('a chat that was gone back in to before everything has no thread to be confused with: it is a new thread', async () => {
  stub.reset();
  const client = answering('ONE', 'TWO', 'THREE');
  const chat = openChat({ client });
  const thread = chat.thread();
  await thread.ask('private paste');
  const original = thread.id;
  thread.rewindTo(0);
  await thread.ask('fresh start');
  assert.deepEqual(userWordsIn(client.requests.at(-1)), ['fresh start']);
  assert.notEqual(thread.id, original);
});

test('a slash command typed after going back does not hide that the chat was gone back in', async () => {
  stub.reset();
  const client = answering('ONE', 'TWO', 'THREE', 'FOUR', 'FIVE');
  const chat = openChat({ client });
  const thread = chat.thread();
  await thread.ask('one');
  await thread.ask('two');
  await thread.ask('three');
  thread.rewindTo(2);
  await thread.ask('', { command: 'model' }); // answered by the participant itself: it is not a turn of the conversation
  await thread.ask('three again');
  assert.deepEqual(userWordsIn(client.requests.at(-1)), ['one', 'two', 'three again']);
  assert.ok(!everythingIn(client.requests.at(-1)).includes('ANSWER-THREE'));
});

test('an answer VS Code stored without its result (Stop pressed while a tool ran on) is not mistaken for a chat that was gone back in', async () => {
  stub.reset();
  const ws = workspace();
  const late = gate();
  let hold = false;
  let started = false;
  ToolRegistry.executeTool = async (name) => {
    if (hold) {
      started = true;
      await late.opened;
    }
    return { name, output: 'tool result', terminalCommand: 'echo late' };
  };
  try {
    const client = scriptedClient([
      { content: 'ANSWER-ONE' },
      { toolCalls: [toolCall('run_terminal_command', { command: 'echo late' })] },
      { content: 'ANSWER-THREE' },
    ]);
    const chat = openChat({ client, grace: 40 });
    const thread = chat.thread();
    await thread.ask('first request');
    const original = thread.id;
    hold = true;
    const second = thread.send('second request');
    await until(() => started, 4000, 'the tool to start');
    await second.stop();
    assert.equal(second.lost, true, 'the answer was stored without its result');
    late.open();
    await second.handler;
    hold = false;

    const third = await thread.ask('third request');
    assert.equal(third.result.metadata.threadId, original, 'the chat was not gone back in: no copy');
    assert.deepEqual(userWordsIn(client.requests.at(-1)), ['first request', 'second request', 'third request'], 'the whole conversation, the stopped step included');
  } finally {
    ws.restore();
  }
});

test('what a thread needs to be cut back is saved with it: after a window reload, going back still gives a copy cut to where the chat stands', async () => {
  stub.reset();
  const client = answering('ONE', 'TWO', 'THREE', 'FOUR', 'FIVE');
  const chat = openChat({ client });
  const thread = chat.thread();
  await thread.ask('one');
  await thread.ask('two');
  await thread.ask('three');
  const original = thread.id;
  await chat.reload();

  thread.rewindTo(1);
  await thread.ask('other two');
  assert.deepEqual(userWordsIn(client.requests.at(-1)), ['one', 'other two']);
  assert.notEqual(thread.id, original);
  assert.deepEqual(wordsSaved(chat, original), ['one', 'two', 'three']);

  // and a chat that was only continued after the reload is not cut
  await chat.reload();
  await thread.ask('three, for real');
  assert.deepEqual(userWordsIn(client.requests.at(-1)), ['one', 'other two', 'three, for real']);
});

test('going back past a compaction cannot be followed (the steps it replaced are gone); going back to after it can', async () => {
  stub.reset();
  const ws = workspace();
  ToolRegistry.executeTool = async (name) => ({ name, output: 'ok' });
  try {
    // A 30,000 character write is more than the prompt budget of a 20,000 token context, so the second request compacts:
    // the model is first asked for its notes (the third reply), and then takes its step again.
    const client = scriptedClient([
      { content: 'ANSWER-ONE' },
      { toolCalls: [toolCall('write_file', { path: 'big.txt', content: 'z'.repeat(30000) })] },
      { content: '### Goal\nThe notes the model wrote when the room ran out.' },
      { content: 'ANSWER-TWO' },
      { content: 'ANSWER-THREE' },
      { content: 'ANSWER-FOUR' },
      { content: 'ANSWER-FIVE' },
    ], { lastKnownClusterStatus: () => ({ policy: { coding_context_tokens: 20000 } }) });
    const chat = openChat({ client });
    const thread = chat.thread();
    await thread.ask('first request');
    await thread.ask('second request, with the big write');
    await thread.ask('THIRD-ORIGINAL');
    const original = thread.id;
    assert.ok(chat.savedTask(original).messages[0].content.startsWith('<dlne_working_memory>'), 'the second request compacted the conversation');
    const beforeIt = thread.fork(1); // the chat as it was before the compaction

    thread.rewindTo(2); // after it
    await thread.ask('a new third');
    const sent = client.requests.at(-1);
    assert.deepEqual(userWordsIn(sent), ['a new third']);
    assert.match(sent.messages.find((m) => String(m.content).startsWith('<dlne_working_memory>')).content, /second request, with the big write/, 'what the compaction kept is still there');
    assert.ok(!everythingIn(sent).includes('THIRD-ORIGINAL') && !everythingIn(sent).includes('ANSWER-THREE'));
    assert.notEqual(thread.id, original);

    const other = await beforeIt.ask('a new second');
    assert.deepEqual(userWordsIn(client.requests.at(-1)), ['a new second'], 'before the compaction there is nothing left to cut back to');
    assert.ok(other.stream.text().startsWith(NEW_CONVERSATION_NOTE));
    assert.ok(chat.log().some((line) => /a new conversation: the newest answer left in the chat names a state this thread no longer has/.test(line)));
  } finally {
    ws.restore();
  }
});

test('readTranscript counts the answers that could be turns of the conversation, and finds the newest mark', () => {
  const mark = (n) => n.toString(16).padStart(16, '0');
  const answer = (command, metadata) => ({ response: [], result: { metadata }, participant: 'dlne.agent', command });
  const request = (command) => ({ prompt: 'x', command, participant: 'dlne.agent', references: [], toolReferences: [] });
  assert.deepEqual(readTranscript(undefined), { answered: 0, markId: undefined });
  assert.deepEqual(readTranscript([]), { answered: 0, markId: undefined });
  assert.deepEqual(readTranscript([request(), answer(undefined, { mark: mark(1) }), request(), answer(undefined, { mark: mark(2) })]), { answered: 2, markId: mark(2) });
  // the participant's own slash commands are not turns of the conversation; /fix and /code are
  assert.equal(readTranscript([request('status'), answer('status', {}), request('clear'), answer('clear', {}), request('contribute'), answer('contribute', {}), request('model'), answer('model', {})]).answered, 0);
  assert.equal(readTranscript([request('fix'), answer('fix', { mark: mark(3) }), request('code'), answer('code', { mark: mark(4) })]).answered, 2);
  // an answer stored without its result still counts, and names nothing: the newest mark is the one before it
  assert.deepEqual(readTranscript([request(), answer(undefined, { mark: mark(5) }), request(), { response: [], result: {}, command: undefined }]), { answered: 2, markId: mark(5) });
  // another participant's answer (its metadata is removed by VS Code) counts too; a mark that is not 16 hex digits is not one
  assert.deepEqual(readTranscript([request(), answer(undefined, { mark: mark(6) }), request(), { response: [], result: { metadata: undefined }, participant: 'github.copilot' }, request(), answer(undefined, { mark: 'not-a-mark' })]), { answered: 3, markId: mark(6) });
  assert.deepEqual(readTranscript([null, 5, 'x', {}]), { answered: 0, markId: undefined });
});

// ---- one run per thread: steer ------------------------------------------------------------------------------

test('steer: Enter during a run ends it after its current step, and the message carries on in the same conversation', async () => {
  stub.reset();
  const ws = workspace({ 'a.txt': 'alpha', 'b.txt': 'beta' });
  try {
    const step = gate();
    const client = scriptedClient([
      async () => { await step.opened; return { toolCalls: [toolCall('read_file', { path: 'a.txt' })] }; },
      { content: 'Understood: only b.txt.' },
    ]);
    const chat = openChat({ client });
    const thread = chat.thread();
    const first = thread.send('read the files');
    await until(() => client.requests.length === 1, 4000, 'the first step');

    const steered = thread.steer('actually only b.txt matters'); // the user presses Enter during the run
    assert.equal(first.yieldRequested, true, 'VS Code asks the request in flight to yield');
    step.open();
    await first.done;
    assert.equal(first.result.metadata.paused, true, 'it stopped after its step, and says it can carry on');
    assert.equal(first.result.errorDetails, undefined, 'an error would keep VS Code from sending the queued message');
    assert.match(first.stream.text(), /Stopped after this step to take your newer message/);

    const second = await steered; // sent by VS Code now that the first request has ended
    await second.done;
    assert.equal(client.requests.length, 2, 'the first run took no step after the one in progress');
    const sent = client.requests[1].messages.filter((m) => m.role !== 'system');
    assert.deepEqual(sent.map((m) => m.role), ['user', 'assistant', 'tool', 'user']);
    assert.match(sent[2].content, /alpha/, 'the step in progress was finished, its result kept');
    assert.equal(sent[3].content, 'actually only b.txt matters');
    assert.equal(second.result.metadata.threadId, first.result.metadata.threadId, 'one conversation');
    assert.equal(first.followups().some((f) => f.prompt === 'continue'), true, 'and Continue is there should the message never come');
  } finally {
    ws.restore();
  }
});

test('a queued message ("Add to Queue", or one sent by a command) does not stop the run: it waits for the run to end, then carries on', async () => {
  stub.reset();
  const ws = workspace({ 'a.txt': 'alpha' });
  try {
    const step = gate();
    const client = scriptedClient([
      async () => { await step.opened; return { toolCalls: [toolCall('read_file', { path: 'a.txt' })] }; },
      { content: 'The whole task is finished.' },
      { content: 'And the queued message is answered.' },
    ]);
    const chat = openChat({ client });
    const thread = chat.thread();
    const first = thread.send('read it');
    await until(() => client.requests.length === 1);
    const queued = thread.queued('a message for afterwards');
    assert.equal(first.yieldRequested, false, 'a queued message does not ask the run to yield');
    step.open();
    await first.done;
    assert.equal(first.result.metadata.paused, false, 'the run went on to its end');
    assert.match(first.stream.text(), /The whole task is finished/);
    assert.doesNotMatch(first.stream.text(), /Stopped after this step/);
    const second = await queued;
    await second.done;
    assert.deepEqual(client.requests.map((r) => r.messages.at(-1).role), ['user', 'tool', 'user']);
    assert.deepEqual(userWordsIn(client.requests.at(-1)), ['read it', 'a message for afterwards']);
    assert.equal(second.result.metadata.threadId, first.result.metadata.threadId);
  } finally {
    ws.restore();
  }
});

test('steer: several messages sent during a run reach the agent together, after the run has stopped', async () => {
  stub.reset();
  const step = gate();
  const client = scriptedClient([
    async () => { await step.opened; return { content: 'First part done.' }; },
    { content: 'Both noted.' },
  ]);
  const chat = openChat({ client });
  const thread = chat.thread();
  const first = thread.send('do the first part');
  await until(() => client.requests.length === 1);
  const one = thread.steer('and also this');
  thread.steer('and that');
  step.open();
  await first.done;
  const second = await one;
  await second.done;
  assert.deepEqual(userWordsIn(client.requests.at(-1)), ['do the first part', 'and also this\n\nand that']);
});

test('a message that reaches the agent while the old run is still going waits for it: nothing the old run does lands after the new message', async () => {
  stub.reset();
  const ws = workspace({ 'a.txt': 'alpha' });
  try {
    // A tool that ignores Stop (as every tool did before 1.5.0), and VS Code's grace cut short so that the old handler is still going.
    const slow = gate();
    let started = false;
    ToolRegistry.executeTool = async (name) => {
      started = true;
      await slow.opened;
      return { name, output: 'slow result' };
    };
    const client = scriptedClient([
      { content: 'warmed up' },
      { toolCalls: [toolCall('read_file', { path: 'a.txt' })] },
      { content: 'Both done.' },
    ]);
    const chat = openChat({ client, grace: 40 });
    const thread = chat.thread();
    await thread.ask('warm up'); // so that the thread has an answer with its id
    const old = thread.send('task one');
    await until(() => started, 4000, 'the slow tool to start');

    const next = await thread.stopAndSend('task two'); // VS Code gives up on the old handler after its grace and sends the new message
    assert.equal(old.handled, false, 'the old handler is still running when the new request starts');
    await sleep(100);
    assert.equal(client.requests.length, 2, 'the new message has not been taken yet: it waits for the old run');
    slow.open();
    await next.done;

    const sent = client.requests.at(-1).messages.filter((m) => m.role !== 'system');
    assert.deepEqual(sent.map((m) => `${m.role}:${m.role === 'user' ? m.content : ''}`),
      ['user:warm up', 'assistant:', 'user:task one', 'assistant:', 'tool:', 'user:task two'],
      "the old run's last step comes before the new message, as if the user had waited");
    assert.ok(next.stream.progressLines().some((line) => /Finishing the previous step/.test(line)));
    assert.ok(chat.log().some((line) => /a message arrived while the run before it is still going/.test(line)));
  } finally {
    ws.restore();
  }
});

test('a message that is stopped while it waits for the run in front leaves the thread working', async () => {
  stub.reset();
  const slow = gate();
  let started = false;
  ToolRegistry.executeTool = async (name) => { started = true; await slow.opened; return { name, output: 'late' }; };
  const ws = workspace();
  try {
    const client = scriptedClient([{ content: 'warm' }, { toolCalls: [toolCall('read_file', { path: 'a.txt' })] }, { content: 'fine' }]);
    const chat = openChat({ client, grace: 30 });
    const thread = chat.thread();
    await thread.ask('warm up');
    const old = thread.send('slow one');
    await until(() => started);
    const waiting = await thread.stopAndSend('second');
    await sleep(60);
    const t0 = Date.now();
    await waiting.stop();
    assert.ok(Date.now() - t0 < 500, 'Stop ends a request that is only waiting, at once');
    slow.open();
    await old.handler;
    const third = await thread.ask('third');
    assert.deepEqual(userWordsIn(client.requests.at(-1)).at(-1), 'third');
    assert.equal(third.result.metadata.failed, false, 'the lane was not left stuck behind the stopped message');
  } finally {
    ws.restore();
  }
});

// ---- Stop ---------------------------------------------------------------------------------------------------

test('Stop ends a running command and everything it started, and the answer still reaches VS Code with its thread id', posix, async () => {
  stub.reset();
  const ws = workspace();
  ToolRegistry.resetApprovals();
  const marker = `54.${process.pid % 1000}71`; // a sleep time no other process has
  try {
    const client = scriptedClient([
      { toolCalls: [toolCall('run_terminal_command', { command: `sleep ${marker} & sleep ${marker}; echo not reached` })] },
      { content: 'Carried on.' },
    ]);
    const chat = openChat({ client });
    const thread = chat.thread();
    const request = thread.send('start the slow thing', { permissionLevel: 'autoApprove' });
    await until(() => running(marker).length >= 2, 4000, 'the command and its children to run');

    const t0 = Date.now();
    await request.stop();
    assert.ok(Date.now() - t0 < chat.grace, 'the handler returned inside VS Code\'s one-second grace');
    assert.equal(request.lost, false, 'so VS Code kept its result');
    assert.ok(request.result.metadata.threadId);
    assert.equal(request.result.metadata.paused, false);
    assert.equal(request.result.metadata.failed, false, 'a Stop is not an error');
    assert.equal(request.followups().some((f) => f.prompt === 'continue'), false, 'and offers no Continue: the user just said stop');
    await until(() => running(marker).length === 0, 5000, 'the whole process tree to be gone');

    const saved = chat.savedTask(request.result.metadata.threadId);
    assert.match(saved.messages.find((m) => m.role === 'tool').content, /Stopped: the user stopped the task/);
    await thread.ask('continue');
    assert.ok(client.requests.at(-1).messages.some((m) => m.role === 'tool' && /Stopped/.test(m.content)), 'the model is told what happened');
  } finally {
    ws.restore();
    for (const pid of running(marker)) { try { process.kill(Number(pid), 'SIGKILL'); } catch { /* gone */ } }
  }
});

test('Stop ends a job the command left running in the background (`server &`), though the shell itself has already exited, and signals only that job\'s own group', posix, async () => {
  stub.reset();
  const ws = workspace();
  ToolRegistry.resetApprovals();
  const marker = `55.${process.pid % 1000}73`;
  const realKill = process.kill;
  const signals = [];
  process.kill = (pid, signal) => {
    if (signal !== 0 && signal !== undefined) signals.push([pid, signal]);
    return realKill.call(process, pid, signal);
  };
  try {
    const client = scriptedClient([{ toolCalls: [toolCall('run_terminal_command', { command: `sleep ${marker} & echo started` })] }, { content: 'Carried on.' }]);
    const chat = openChat({ client });
    const thread = chat.thread();
    const request = thread.send('start the server', { permissionLevel: 'autoApprove' });
    await until(() => running(marker).length >= 1, 4000, 'the background job to run');
    await sleep(300); // the shell has printed and exited; the job still holds the output pipe, so the command goes on "running"
    assert.equal(client.requests.length, 1, 'the run is still waiting for that command');

    const t0 = Date.now();
    await request.stop();
    assert.ok(Date.now() - t0 < chat.grace, 'the handler returned inside VS Code\'s one-second grace');
    assert.equal(request.lost, false);
    await until(() => running(marker).length === 0, 5000, 'the background job to be gone');
    assert.ok(signals.length >= 1 && signals.every(([pid, signal]) => pid < 0 && signal === 'SIGTERM'), `only the job's own process group was signalled, got ${JSON.stringify(signals)}`);
    await sleep(1700); // past the grace after which a job that ignored SIGTERM would be killed: nothing is sent to a group that is empty
    assert.equal(signals.length, 1);
  } finally {
    process.kill = realKill;
    ws.restore();
    for (const pid of running(marker)) { try { process.kill(Number(pid), 'SIGKILL'); } catch { /* gone */ } }
  }
});

test('Stop while a dialog waits for the user: the run ends at once, and an Allow clicked later runs nothing and remembers nothing', async () => {
  stub.reset();
  const ws = workspace();
  ToolRegistry.resetApprovals();
  const dialog = gate();
  let asked = 0;
  ToolRegistry.approver = async () => { asked++; return dialog.opened; }; // a modal that stays open until the user answers
  try {
    const client = scriptedClient([{ toolCalls: [toolCall('write_file', { path: 'new.txt', content: 'hello' })] }, { content: 'Carried on.' }]);
    const chat = openChat({ client });
    const thread = chat.thread();
    const request = thread.send('create new.txt');
    await until(() => asked === 1, 4000, 'the dialog to open');

    const t0 = Date.now();
    await request.stop();
    assert.ok(Date.now() - t0 < chat.grace, 'the run did not wait for the dialog');
    assert.equal(request.lost, false);
    assert.ok(request.result.metadata.threadId);

    dialog.open('always'); // the user clicks "Always allow" on the dialog that was still on screen
    await sleep(60);
    assert.equal(fs.existsSync(path.join(ws.root, 'new.txt')), false, 'a late Allow runs nothing');
    assert.equal(ToolRegistry.alwaysApprovals().edits, false, 'and remembers nothing');
    const saved = chat.savedTask(request.result.metadata.threadId);
    assert.equal(saved.messages.find((m) => m.role === 'tool').content, 'Not run: the user stopped the task.');
  } finally {
    ws.restore();
  }
});

test('a tool that finishes after Stop, once VS Code has closed the stream, ends no run and loses no save', async () => {
  stub.reset();
  const ws = workspace();
  const late = gate();
  let started = false;
  ToolRegistry.executeTool = async (name) => {
    started = true;
    await late.opened;
    return { name, output: 'late result', terminalCommand: 'echo late' };
  };
  try {
    const client = scriptedClient([{ toolCalls: [toolCall('run_terminal_command', { command: 'echo late' })] }, { content: 'unused' }]);
    const chat = openChat({ client, grace: 40 });
    const thread = chat.thread();
    const request = thread.send('do it');
    await until(() => started, 4000, 'the tool to start');
    await request.stop();
    assert.equal(request.lost, true, 'the tool ignored Stop, so VS Code gave up after its grace');
    assert.equal(request.stream.closed, true);

    late.open(); // the tool ends now: its result is written to a stream that is closed
    const outcome = await request.handler;
    assert.equal(outcome.error, undefined, 'the handler did not throw');
    const id = outcome.value.metadata.threadId;
    assert.ok(id);
    const saved = chat.savedTask(id);
    assert.ok(saved.messages.some((m) => m.role === 'tool' && m.content === 'late result'), 'the task was saved with the late result');
    assert.ok(chat.log().some((line) => /callback onToolEnd threw \(Response stream has been closed\)/.test(line)));
  } finally {
    ws.restore();
  }
});

// ---- errors and Continue ------------------------------------------------------------------------------------

test('an error ends the answer with a Continue follow-up, and Continue carries on from the saved conversation', async () => {
  stub.reset();
  const client = scriptedClient([{ throw: 'The engine exploded', status: 500 }, { content: 'Carried on.' }]);
  const chat = openChat({ client });
  const thread = chat.thread();
  const failed = await thread.ask('build it');
  assert.equal(failed.result.metadata.failed, true);
  assert.equal(failed.result.metadata.paused, false);
  assert.match(failed.stream.text(), /Error connecting to DLNE cluster: The engine exploded/);
  const carryOn = failed.followups().filter((f) => f.prompt === 'continue');
  assert.deepEqual(carryOn.map((f) => f.label), ['▶ Continue']);

  const next = await thread.ask('continue');
  assert.deepEqual(userWordsIn(client.requests.at(-1)), ['build it', 'continue']);
  assert.equal(next.result.metadata.failed, false);
  assert.equal(next.followups().some((f) => f.prompt === 'continue'), false, 'a clean answer offers no Continue');
});

test('the turn limit still offers Continue', async () => {
  stub.reset();
  const ws = workspace({ 'a.txt': 'alpha' });
  stub.state.settings['dlne.agent.maxTurns'] = 10;
  try {
    // ten steps, then the report the agent is asked for at the limit (no tool call). Another line each time: the same
    // read ten times running is what the loop check stops (loopGuard.ts), and this is about the turn limit.
    const steps = Array.from({ length: 10 }, (_, i) => ({ toolCalls: [toolCall('read_file', { path: 'a.txt', start_line: i + 1, end_line: i + 1 })] }));
    const chat = openChat({ client: scriptedClient([...steps, { content: 'Report: read a.txt ten times.' }]) });
    const paused = await chat.thread().ask('read it a lot');
    assert.equal(paused.result.metadata.paused, true);
    assert.equal(paused.result.metadata.failed, false);
    assert.equal(paused.followups().some((f) => f.prompt === 'continue'), true);
  } finally {
    ws.restore();
  }
});

test('an exception inside the handler still gives an answer that names its thread, and offers Continue', async () => {
  stub.reset();
  const chat = openChat({
    client: scriptedClient([{ content: 'ok' }]),
    workerManager: { onStatusChange() {}, getStatus: () => ({}), toggle: async () => { throw new Error('systemd is not running'); } },
  });
  const thread = chat.thread();
  const r = await thread.ask('', { command: 'contribute' });
  assert.equal(r.lost, false);
  assert.match(r.result.errorDetails.message, /systemd is not running/);
  assert.equal(r.result.metadata.failed, true);
  assert.ok(r.result.metadata.threadId, 'VS Code keeps metadata next to errorDetails, so the thread is found again');
  assert.equal(thread.id, r.result.metadata.threadId);
  assert.equal(r.followups().some((f) => f.prompt === 'continue'), true);
  assert.ok(chat.log().some((line) => /the request failed with an exception: systemd is not running/.test(line)));
});

// ---- housekeeping never fails a request ---------------------------------------------------------------------

/**
 * Makes every delete of a saved task fail the way it does on Windows while a virus scanner or an indexer holds the file
 * open (EBUSY), until `restore()`. Done on fs itself, so it holds as root and on every platform.
 */
function undeletable() {
  const real = fs.rmSync;
  fs.rmSync = (target, options) => {
    if (/\.(json|tmp)$/.test(String(target))) throw Object.assign(new Error(`EBUSY: resource busy or locked, unlink '${target}'`), { code: 'EBUSY' });
    return real(target, options);
  };
  return { restore() { fs.rmSync = real; } };
}

/** A saved task as the agent writes one, `ageMs` old. */
function oldTask(file, ageMs, words = 'x') {
  const savedAt = new Date(Date.now() - ageMs);
  saveAgentState(file, {
    version: 1, savedAt: savedAt.toISOString(), sessionId: `session-${path.basename(file, '.json')}`,
    messages: [{ role: 'user', content: words }], userWords: [words], notes: '', ledger: {}, promptBudget: 0, charsPerToken: 3.5,
  });
  fs.utimesSync(file, savedAt, savedAt);
}

test('an old saved task that cannot be deleted never fails the request that triggered the clean-up (review probe A), and the next one clears it', async () => {
  stub.reset();
  const client = scriptedClient([{ content: 'all done' }]);
  const chat = openChat({ client });
  const dir = agentThreadsDir(chat.context);
  for (let n = 0; n < 22; n++) oldTask(path.join(dir, `old-thread-${String(n).padStart(2, '0')}.json`), (n + 1) * 60_000);
  const thread = chat.thread();
  const stuck = undeletable();
  try {
    const r = await thread.ask('hello');
    assert.equal(r.result.errorDetails, undefined, 'the request ends as it ended');
    assert.equal(r.result.metadata.failed, false);
    assert.match(r.stream.text(), /all done/);
    assert.ok(r.result.metadata.threadId);
    assert.deepEqual(wordsSaved(chat, thread), ['hello'], 'its own task was saved');
    assert.equal(chat.threadFiles().length, 23, 'nothing could be deleted');
    assert.ok(chat.log().some((line) => /housekeeping: could not delete 3 old saved tasks: EBUSY: resource busy or locked/.test(line)), 'it is written down');
    const again = await thread.ask('and again');
    assert.equal(again.result.errorDetails, undefined, 'every request after it works too (VS Code sends no queued message after an error)');
  } finally {
    stuck.restore();
  }
  await thread.ask('once more');
  assert.equal(chat.threadFiles().length, 20, 'once the files are free the next request deletes them');
});

test('an expired saved task that cannot be deleted does not stop its thread: the message still reaches the model (review probe B)', async () => {
  stub.reset();
  const client = scriptedClient([{ content: 'hello again' }]);
  const chat = openChat({ client });
  const id = '0a1b2c3d-1111-4222-8333-444455556666';
  oldTask(agentThreadFile(chat.context, id), STATE_MAX_AGE_MS + 3600_000, 'old task');
  const thread = chat.thread();
  // a thread VS Code remembers: one earlier answer that carries this thread's id
  thread.turns.push(
    { prompt: 'old task', command: undefined, participant: 'dlne.agent', references: [], toolReferences: [] },
    { response: [], result: { metadata: { threadId: id } }, participant: 'dlne.agent' }
  );
  const stuck = undeletable();
  try {
    for (const n of [1, 2]) {
      const r = await thread.ask(`carry on ${n}`);
      assert.equal(r.result.errorDetails, undefined, `message ${n} was answered`);
      assert.match(r.stream.text(), /hello again/);
    }
  } finally {
    stuck.restore();
  }
  assert.equal(client.requests.length, 2, 'both messages reached the model');
  assert.deepEqual(userWordsIn(client.requests[0]), ['carry on 1'], 'the week-old task was not loaded');
  assert.ok(chat.log().some((line) => /housekeeping: could not delete an expired saved task: EBUSY/.test(line)));
});

// ---- the DLNE log -------------------------------------------------------------------------------------------

test('the run is written to the DLNE log: start, each step\'s tools, retries, errors and the end, never the words or the file contents', async () => {
  stub.reset();
  quickPauses();
  const ws = workspace({ 'notes.txt': 'TOPSECRET-FILE-BODY' });
  const ghp = 'gh' + 'p_' + generator(7)('alnum', 36);
  try {
    const client = scriptedClient([
      { toolCalls: [toolCall('read_file', { path: 'notes.txt' })] },
      { throw: new EngineDropped('engine_dropped', false) },
      { content: 'Finished with zebra-stripes.' },
      { throw: `Server returned 500: the key ${ghp} was rejected`, status: 500 },
    ]);
    const chat = openChat({ client });
    const thread = chat.thread();
    await thread.ask('please read notes.txt, the project is called ZEBRA-PROJECT');
    await thread.ask('and again');

    const log = chat.log().join('\n');
    assert.match(log, /agent thread [0-9a-f]{8}: run 1 started \(turn limit 100, 0 messages already in the conversation\)/);
    assert.match(log, /run 1 step 1: read_file/);
    assert.match(log, /run 1 sends the step again: engine_dropped, retry 1 of 8, after 2 s/);
    assert.match(log, /run 1 step 2: answer/);
    assert.match(log, /run 1 ended: done, 2 steps, \d+ s/);
    assert.match(log, /run 2 error: Server returned 500: the key \[REDACTED\] was rejected \(HTTP 500\)/);
    assert.match(log, /run 2 ended: error, 0 steps/);
    for (const secret of ['ZEBRA-PROJECT', 'TOPSECRET-FILE-BODY', 'zebra-stripes', ghp, 'notes.txt']) {
      assert.ok(!log.includes(secret), `the log must not hold ${secret}`);
    }
  } finally {
    ws.restore();
  }
});

test('the DLNE log holds none of this machine\'s folders: a tool exception and a failed request name them <workspace> and ~ (the log is pasted into bug reports)', async () => {
  stub.reset();
  const ws = workspace({ 'a.txt': 'alpha' });
  const home = os.homedir();
  ToolRegistry.executeTool = async () => {
    throw new Error(`EACCES: permission denied, open '${path.join(ws.root, 'src', 'a.txt')}' (config in ${path.join(home, '.dlne', 'x.json')})`);
  };
  try {
    const client = scriptedClient([{ toolCalls: [toolCall('read_file', { path: 'a.txt' })] }, { content: 'Done.' }]);
    const chat = openChat({
      client,
      workerManager: {
        onStatusChange() {},
        getStatus: () => ({}),
        toggle: async () => { throw new Error(`ENOENT: no such file or directory, open '${path.join(home, '.config', 'systemd', 'user', 'dlne.service')}'`); },
      },
    });
    const thread = chat.thread();
    await thread.ask('read it');
    await thread.ask('', { command: 'contribute' });
    const log = chat.log().join('\n');
    assert.match(log, /the read_file tool threw: EACCES: permission denied, open '<workspace>.src.a\.txt' \(config in ~.\.dlne.x\.json\)/);
    assert.match(log, /the request failed with an exception: ENOENT: no such file or directory, open '~.\.config.systemd.user.dlne\.service'/);
    assert.ok(!log.includes(ws.root), 'the workspace folder is not in the log');
    assert.ok(!log.includes(home), 'nor is the home folder, with the user name in it');
  } finally {
    ws.restore();
  }
});

// ---- the silent stream --------------------------------------------------------------------------------------

test('a stream that goes silent is ended after the silence limit, and the step is sent again after the usual pause', async () => {
  stub.reset();
  quickPauses();
  const attempts = [];
  const server = http.createServer((req, res) => {
    if (req.url === '/v1/cluster/status') {
      res.setHeader('Content-Type', 'application/json');
      res.end(JSON.stringify({ status: 'Active', global_active_model: 'served.gguf', model_loading: { is_loading: false } }));
      return;
    }
    req.resume();
    req.on('end', () => {
      attempts.push(Date.now());
      if (attempts.length === 1) return; // accepts the request and says nothing, ever
      res.writeHead(200, { 'Content-Type': 'text/event-stream' });
      res.write(`data: ${JSON.stringify({ choices: [{ delta: { content: 'Back again.' } }] })}\n\n`);
      res.end('data: [DONE]\n\n');
    });
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  stub.state.settings['dlne.serverUrl'] = `http://127.0.0.1:${server.address().port}`;
  // The silent request never says a word, so it is the limit before the answer starts that ends it.
  const silence = [DlneClient.streamSilenceMs, DlneClient.firstByteSilenceMs];
  DlneClient.streamSilenceMs = 250;
  DlneClient.firstByteSilenceMs = 250;
  try {
    const chat = openChat({ client: new DlneClient() });
    const request = await chat.thread().ask('hello');
    assert.equal(attempts.length, 2, 'the silent request was ended and sent again');
    assert.match(request.stream.text(), /Back again\./);
    assert.ok(request.stream.progressLines().some((line) => /The swarm stopped answering\. Sending that step again \(1\/8\)/.test(line)));
    assert.ok(chat.log().some((line) => /run 1 sends the step again: stream_silent, retry 1 of 8/.test(line)));
    assert.equal(request.result.metadata.failed, false);
  } finally {
    [DlneClient.streamSilenceMs, DlneClient.firstByteSilenceMs] = silence;
    server.closeAllConnections();
    server.close();
  }
});
