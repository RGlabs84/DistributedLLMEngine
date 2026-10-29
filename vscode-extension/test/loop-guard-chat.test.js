'use strict';
/**
 * The loop guard (src/agent/loopGuard.ts) in the two chats that run the extension's own agent loop: `@dlne` in VS Code's
 * Chat view (the real participant, through the Chat-view stand-in) and the sidebar (the real provider and the real page
 * script). In both, the first sign of an agent that keeps repeating itself is a progress line for the user and a note for
 * the model; if it carries on, the run ends as a pause, with a notice and the Continue that a turn limit offers; and
 * Continue starts a new run with a fresh guard. (The guard itself and the agent loop are in loop-guard.test.js, DLNE Swarm
 * in the model picker in loop-guard-picker.test.js.)
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const harness = require('./helpers/chat-harness');
const sidebar = require('./helpers/sidebar-page');
const { ToolRegistry } = require('../dist/agent/tools');
const { SidebarProvider } = require('../dist/views/sidebarProvider');

const { stub } = harness;
const ROOT = path.join(__dirname, '..');

const NOTE_LINE = '🔁 The agent was repeating itself (reading `a.txt` again and again); DLNE told it to change approach.';
const STOP_NOTE = 'DLNE stopped the last task because you kept reading `a.txt` again and again; take a different approach.';
const NOTICE = '\n\n⏹️ DLNE stopped this task: the agent kept reading `a.txt` again and again without getting anywhere. Everything so far is kept: say continue with a hint about what to try, or ask for something else.';

// ---- @dlne in VS Code's Chat view ------------------------------------------------------------------------------------

test('@dlne: the nudge is a progress line, the stop is a notice and a paused answer that offers Continue, and the log has both', async () => {
  stub.reset();
  const ws = harness.workspace({ 'a.txt': 'alpha\n' });
  try {
    const read = () => ({ toolCalls: [harness.toolCall('read_file', { path: 'a.txt' })] });
    const client = harness.scriptedClient([read(), read(), read(), read(), { content: 'never reached' }]);
    const chat = harness.openChat({ client });
    const asked = await chat.thread().ask('read a.txt');

    assert.equal(client.requests.length, 4, 'no fifth step');
    assert.equal(asked.result.metadata.paused, true, 'a pause: the same metadata the turn limit leaves');
    assert.equal(asked.result.metadata.failed, false);
    assert.equal(asked.result.errorDetails, undefined, 'not an error');
    assert.deepEqual(asked.stream.progressLines().filter((line) => line.includes('🔁')), [NOTE_LINE]);
    assert.ok(asked.stream.text().endsWith(NOTICE), asked.stream.text());
    assert.deepEqual(asked.followups().filter((f) => f.prompt === 'continue'), [{ prompt: 'continue', label: '▶ Continue' }]);

    const log = chat.log().join('\n');
    assert.match(log, /agent thread [0-9a-f]{8}: run 1 loop check: told the model to change approach after step 3 \(read_file, the same call with the same result, 3 times in the last 12 steps\)/);
    assert.match(log, /agent thread [0-9a-f]{8}: run 1 loop check: stopped the run after step 4 \(read_file, the same call with the same result, 4 times in the last 12 steps\)/);
    assert.match(log, /run 1 ended: paused, 4 steps/);
    assert.ok(!log.includes('a.txt'), 'names only');
  } finally {
    ws.restore();
  }
});

test('@dlne: Continue carries on from where the loop stopped, with a fresh guard, and a clean answer offers no Continue', async () => {
  stub.reset();
  const ws = harness.workspace({ 'a.txt': 'alpha\n' });
  try {
    const read = () => ({ toolCalls: [harness.toolCall('read_file', { path: 'a.txt' })] });
    const client = harness.scriptedClient([read(), read(), read(), read(), read(), read(), { content: 'It says alpha.' }]);
    const chat = harness.openChat({ client });
    const thread = chat.thread();
    const stopped = await thread.ask('read a.txt');
    assert.equal(stopped.result.metadata.paused, true);

    const carried = await thread.ask('continue');
    assert.equal(carried.result.metadata.paused, false, 'two more reads of the same file are not a loop for a new run');
    assert.equal(carried.result.metadata.failed, false);
    assert.ok(carried.stream.text().endsWith('It says alpha.'), carried.stream.text());
    assert.ok(!carried.stream.text().includes('⏹️'), 'no notice');
    assert.deepEqual(carried.stream.progressLines().filter((line) => line.includes('🔁')), []);
    assert.equal(carried.followups().some((f) => f.prompt === 'continue'), false);
    assert.equal(client.requests.length, 7);
    // The model got the same conversation back, with its note in it, then the note of why the run was stopped, and "continue" after.
    const messages = client.requests[4].messages;
    assert.deepEqual(messages.slice(-2), [{ role: 'user', content: STOP_NOTE }, { role: 'user', content: 'continue' }]);
    assert.ok(messages.some((m) => m.role === 'user' && String(m.content).startsWith('Loop check: you have been reading `a.txt` again and again.')));
  } finally {
    ws.restore();
  }
});

test('@dlne: a hint in the Continue message reaches the model after the note, and the run goes on', async () => {
  stub.reset();
  const ws = harness.workspace({ 'a.txt': 'alpha\n', 'b.txt': 'beta\n' });
  try {
    const read = () => ({ toolCalls: [harness.toolCall('read_file', { path: 'a.txt' })] });
    const client = harness.scriptedClient([read(), read(), read(), read(),
      { toolCalls: [harness.toolCall('read_file', { path: 'b.txt' })] },
      { content: 'b.txt says beta.' }]);
    const chat = harness.openChat({ client });
    const thread = chat.thread();
    await thread.ask('read a.txt');
    const hinted = await thread.ask('continue, but read b.txt instead');
    assert.equal(hinted.result.metadata.paused, false);
    assert.match(hinted.stream.text(), /b\.txt says beta\./);
    assert.deepEqual(harness.userWordsIn(client.requests.at(-1)).slice(-1), ['continue, but read b.txt instead']);
  } finally {
    ws.restore();
  }
});

// ---- the sidebar --------------------------------------------------------------------------------------------------------

const STATUS = { state: 'idle', gpu: { name: 'GPU', api: 'vulkan', vramTotalGb: 8 }, vramContributedGb: 0, cpuPool: false };

/** The real provider over a scripted client, its workspace and storage in temp folders, shown in a view. */
async function withSidebar(steps, body) {
  stub.reset();
  ToolRegistry.resetApprovals();
  const workspace = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-loop-sidebar-ws-'));
  fs.writeFileSync(path.join(workspace, 'notes.txt'), 'hello\n');
  stub.vscode.workspace.workspaceFolders = [{ uri: { fsPath: workspace } }];
  const storage = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-loop-sidebar-store-'));
  const memento = new Map();
  const context = {
    extensionUri: { fsPath: ROOT },
    storageUri: { fsPath: storage },
    globalStorageUri: { fsPath: path.join(storage, 'global') },
    subscriptions: [],
    workspaceState: { get: (k) => memento.get(k), update: async (k, v) => { memento.set(k, v); } },
  };
  const client = sidebar.scriptedClient(steps);
  const provider = new SidebarProvider(context.extensionUri, client, { onStatusChange() {}, getStatus: () => STATUS }, context);
  const view = sidebar.openView(provider);
  view.mount();
  try {
    return await body({ client, view });
  } finally {
    stub.vscode.workspace.workspaceFolders = undefined;
    fs.rmSync(workspace, { recursive: true, force: true });
    fs.rmSync(storage, { recursive: true, force: true });
  }
}

const send = (view, text) => { view.page.type(text); view.page.pressEnter(); };
const readNotes = () => sidebar.say('', [sidebar.toolCall('read_file', { path: 'notes.txt' })]);

test('the sidebar: the nudge is a status line, the stop is a notice with the Continue button, and Continue starts a fresh run', async () => {
  const steps = [readNotes(), readNotes(), readNotes(), readNotes(), readNotes(), readNotes(), sidebar.say('It says hello.')];
  await withSidebar(steps, async ({ client, view }) => {
    send(view, 'read notes.txt');
    await view.idle();

    assert.equal(client.calls.length, 4, 'the fourth step was the last');
    const line = '🔁 The agent was repeating itself (reading `notes.txt` again and again); DLNE told it to change approach.';
    assert.deepEqual(view.posted('loadingProgress').map((m) => m.statusText).filter((text) => text.includes('🔁')), [line]);
    assert.match(view.page.chat(), /DLNE stopped this task: the agent kept reading .*notes\.txt.* again and again without getting anywhere\. Everything so far is kept: say continue with a hint about what to try, or ask for something else\./);
    assert.equal(view.page.find('continue-btn').length, 1, 'the Continue button, as at the turn limit');
    assert.equal(view.posted('turnComplete').at(-1).paused, true);
    assert.equal(view.page.controls().send, 'shown', 'the page is free again');
    assert.deepEqual(view.page.errors, []);

    // The click sends "continue" without touching what is typed in the box.
    view.page.type('a draft for later');
    view.page.find('continue-btn')[0].click();
    assert.equal(view.fromPage.filter((m) => m.type === 'sendMessage').at(-1).text, 'continue');
    await view.idle();
    assert.equal(client.calls.length, 7, 'two more reads of the same file and the answer: a new run, a fresh guard');
    assert.deepEqual(client.calls[4].messages.slice(-2).map((m) => [m.role, m.content]), [
      ['user', 'DLNE stopped the last task because you kept reading `notes.txt` again and again; take a different approach.'],
      ['user', 'continue'],
    ], 'a click that says nothing more still tells the model why the run was stopped');
    assert.match(view.page.chat(), /It says hello\./);
    assert.equal(view.page.find('continue-btn').length, 0, 'the old Continue button is gone');
    assert.equal(view.posted('turnComplete').at(-1).paused, false);
    assert.equal(view.page.controls().input, 'a draft for later');
    assert.deepEqual(view.page.errors, []);
  });
});

test('the sidebar: a stop that came while the view was hidden is there, Continue button and all, when it is shown', async () => {
  const steps = [readNotes(), readNotes(), readNotes(), readNotes(), sidebar.say('never reached')];
  await withSidebar(steps, async ({ view }) => {
    send(view, 'read notes.txt');
    view.hide();
    await view.idle();
    view.show();
    assert.match(view.page.chat(), /DLNE stopped this task/);
    assert.equal(view.page.find('continue-btn').length, 1);
    assert.equal(view.page.controls().send, 'shown');
  });
});
