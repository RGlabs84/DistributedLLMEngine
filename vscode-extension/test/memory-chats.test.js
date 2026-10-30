'use strict';
/**
 * Memory 2, in the chats (the contract's section 7). On 2026-10-30 a dozen `remember` calls failed in a row in the
 * owner's project, and the sidebar showed each one with a green ✅: it drew every result that way and never looked at
 * `is_error`. Now:
 *
 * 1. A result the tool marked as an error shows ⚠️, for every tool, in both chats: the sidebar as it runs, the sidebar
 *    when a saved chat is shown again, and `@dlne` in VS Code's Chat view (which already did).
 * 2. `DLNE: Open Agent Memory` (dlne.memory.open) opens the project's .dlne/memory.md, offers ~/.dlne/memory.md when there
 *    is no project memory, and says plainly when there is no memory at all.
 *
 * The ledger's side of Memory 2 is in agent-ledger.test.js.
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
const { openAgentMemory } = require('../dist/extension');

const { stub } = harness;
const ROOT = path.join(__dirname, '..');
const STATUS = { state: 'idle', gpu: { name: 'GPU', api: 'vulkan', vramTotalGb: 8 }, vramContributedGb: 0, cpuPool: false };

/** A remember call that cannot work: nothing was ever saved, so there is nothing for `replaces` to pick out. */
const badRemember = { fact: 'Tests run with npm test.', scope: 'project', replaces: 'project: Tests run with make.' };

// ---- the sidebar: the page on its own ----------------------------------------------------------------------------

/** The sidebar's real page script, with nobody on the other side (what it posts is kept, not answered). */
function lonePage() {
  stub.reset();
  const provider = new SidebarProvider({ fsPath: ROOT }, sidebar.scriptedClient([sidebar.say('x')]),
    { onStatusChange() {}, getStatus: () => STATUS }, undefined);
  const view = sidebar.openView(provider);
  view.mount();
  return sidebar.createPage(view.webview.html, { toHost: () => {} });
}

/** The text of every result line (tool-pill) and terminal card heading in the chat. */
const marks = (page) => [...page.find('tool-pill'), ...page.find('terminal-cmd')].map((e) => e.ownText);

test('sidebar: an error result shows ⚠️ and a good one ✅, for every tool, the Undo still on a memory change', () => {
  const page = lonePage();
  page.receive({ type: 'appendUserMessage', text: 'tidy the memory' });
  page.receive({ type: 'toolEnd', result: { name: 'remember', output: 'No fact you saved matches "project: x".', is_error: true } });
  page.receive({ type: 'toolEnd', result: { name: 'read_file', output: 'File not found: a.txt', is_error: true } });
  page.receive({ type: 'toolEnd', result: { name: 'list_directory', output: 'notes.txt' } });
  page.receive({ type: 'toolEnd', result: { name: 'remember', output: '📝 Remembered for this project [a7f3]: x', memoryChange: { n: 1 } } });
  page.receive({ type: 'toolEnd', result: { name: 'run_terminal_command', output: 'Stopped: the user stopped the task.', terminalCommand: 'sleep 60', is_error: true } });
  page.receive({ type: 'toolEnd', result: { name: 'run_terminal_command', output: 'ok', terminalCommand: 'echo ok' } });
  assert.deepEqual(marks(page), [
    '⚠️ remember: No fact you saved matches "project: x".',
    '⚠️ read_file: File not found: a.txt',
    '✅ list_directory: notes.txt',
    '✅ remember: 📝 Remembered for this project [a7f3]: x',
    '⚠️ Terminal: sleep 60',
    '⚡ Terminal: echo ok',
  ]);
  // Only the change that was made has a way back.
  assert.equal(page.find('undo-btn').length, 1);
  assert.equal(page.find('tool-pill')[3].querySelector('button').textContent, '↩ Undo');
  assert.deepEqual(page.errors, []);
});

test('sidebar: a saved chat shown again draws its results with the same marks, and no Undo', () => {
  const page = lonePage();
  page.receive({
    type: 'restoreHistory',
    session: { active: 'chat-0001', title: 'tidy', list: [{ id: 'chat-0001', title: 'tidy', updated: 1, active: true }] },
    history: [
      { id: 'u1', role: 'user', text: 'tidy the memory' },
      {
        id: 'a1', role: 'assistant', text: 'I could not.', toolCalls: [
          { name: 'remember', args: {}, result: { name: 'remember', output: 'No fact you saved matches "project: x".', is_error: true } },
          { name: 'list_directory', args: {}, result: { name: 'list_directory', output: 'notes.txt' } },
          { name: 'remember', args: {}, result: { name: 'remember', output: '📝 Remembered for you [b812]: y', memoryChange: { n: 1 } } },
          { name: 'run_terminal_command', args: {}, result: { name: 'run_terminal_command', output: 'Stopped.', terminalCommand: 'sleep 60', is_error: true } },
          { name: 'read_file', args: {} },        // a call that never got its result (the window closed): nothing to draw
        ],
      },
    ],
    run: { events: [], busy: false },
  });
  assert.deepEqual(marks(page), [
    '⚠️ remember: No fact you saved matches "project: x".',
    '✅ list_directory: notes.txt',
    '✅ remember: 📝 Remembered for you [b812]: y',
    '⚠️ Terminal: sleep 60',
  ]);
  assert.equal(page.find('undo-btn').length, 0, 'a button belongs to a live task');
  assert.deepEqual(page.errors, []);
});

// ---- the sidebar: the real provider, a task, and a restart -----------------------------------------------------------

test('sidebar: is_error reaches the page from a real run, and is still there after VS Code restarts', async () => {
  stub.reset();
  ToolRegistry.resetApprovals();
  const realApprover = ToolRegistry.approver;
  ToolRegistry.approver = async () => 'once';
  const workspace = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-memory-chats-ws-'));
  fs.writeFileSync(path.join(workspace, 'notes.txt'), 'hello\n');
  stub.vscode.workspace.workspaceFolders = [{ uri: { fsPath: workspace } }];
  const storage = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-memory-chats-store-'));
  const memento = new Map();
  const context = {
    extensionUri: { fsPath: ROOT },
    storageUri: { fsPath: storage },
    globalStorageUri: { fsPath: path.join(storage, 'global') },
    subscriptions: [],
    workspaceState: { get: (k) => memento.get(k), update: async (k, v) => { memento.set(k, v); } },
  };
  const steps = [
    sidebar.say('', [sidebar.toolCall('remember', badRemember)]),
    sidebar.say('', [sidebar.toolCall('list_directory', { path: '.' })]),
    sidebar.say('Nothing to tidy.'),
  ];
  const open = () => {
    const provider = new SidebarProvider(context.extensionUri, sidebar.scriptedClient(steps), { onStatusChange() {}, getStatus: () => STATUS }, context);
    const view = sidebar.openView(provider);
    view.mount();
    return view;
  };
  try {
    const view = open();
    view.page.type('tidy the memory');
    view.page.pressEnter();
    await view.idle();
    const ended = view.posted('toolEnd').map((m) => [m.result.name, m.result.is_error === true]);
    assert.deepEqual(ended, [['remember', true], ['list_directory', false]], 'the flag crossed to the page');
    const live = marks(view.page);
    assert.equal(live.length, 2);
    assert.match(live[0], /^⚠️ remember: No fact you saved matches/);
    assert.match(live[1], /^✅ list_directory:/);

    // A restart: a new provider over the same storage draws the chat from what was saved.
    const again = open();
    await again.idle();
    assert.deepEqual(marks(again.page), live);
  } finally {
    ToolRegistry.approver = realApprover;
    stub.vscode.workspace.workspaceFolders = undefined;
    fs.rmSync(workspace, { recursive: true, force: true });
    fs.rmSync(storage, { recursive: true, force: true });
  }
});

// ---- @dlne in VS Code's Chat view ------------------------------------------------------------------------------------

test('@dlne: an error result shows ⚠️ and a good one ✅, as in the sidebar', async () => {
  stub.reset();
  ToolRegistry.resetApprovals();
  const realApprover = ToolRegistry.approver;
  ToolRegistry.approver = async () => 'once';
  const ws = harness.workspace({ 'notes.txt': 'hello\n' });
  try {
    const client = harness.scriptedClient([
      { toolCalls: [harness.toolCall('remember', badRemember)] },
      { toolCalls: [harness.toolCall('list_directory', { path: '.' })] },
      { content: 'Nothing to tidy.' },
    ]);
    const asked = await harness.openChat({ client }).thread().ask('tidy the memory');
    const text = asked.stream.text();
    assert.match(text, /⚠️ \*\*`remember`\*\*: No fact you saved matches/);
    assert.ok(!text.includes('✅ **`remember`**'), text);
    assert.match(text, /✅ \*\*`list_directory`\*\*: /);
  } finally {
    ToolRegistry.approver = realApprover;
    ws.restore();
  }
});

// ---- DLNE: Open Agent Memory ------------------------------------------------------------------------------------------

/** A folder for the project and one for home, `opened` filled by a stand-in for showTextDocument, and the answer to the offer. */
async function withMemoryFiles({ project, user, folder = true, choice, before = () => {} }, body) {
  stub.reset();
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-memory-open-ws-'));
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-memory-open-home-'));
  const write = (dir, text) => {
    fs.mkdirSync(path.join(dir, '.dlne'), { recursive: true });
    fs.writeFileSync(path.join(dir, '.dlne', 'memory.md'), text);
  };
  if (project !== undefined) write(root, project);
  if (user !== undefined) write(home, user);
  before({ root, home });
  stub.vscode.workspace.workspaceFolders = folder ? [{ uri: { fsPath: root } }] : undefined;
  const opened = [];
  const realShow = stub.vscode.window.showTextDocument;
  stub.vscode.window.showTextDocument = async (uri) => { opened.push(uri.fsPath); };
  const offers = [];
  const realInfo = stub.vscode.window.showInformationMessage;
  stub.vscode.window.showInformationMessage = async (message, ...items) => {
    offers.push({ message, items });
    return choice;
  };
  try {
    await openAgentMemory(home);
    return await body({ root, home, opened, offers });
  } finally {
    stub.vscode.window.showTextDocument = realShow;
    stub.vscode.window.showInformationMessage = realInfo;
    stub.vscode.workspace.workspaceFolders = undefined;
    fs.rmSync(root, { recursive: true, force: true });
    fs.rmSync(home, { recursive: true, force: true });
  }
}

const memoryFile = (dir) => path.join(dir, '.dlne', 'memory.md');

test('Open Agent Memory: the project\'s .dlne/memory.md opens at once, even with a memory of your own', async () => {
  await withMemoryFiles({ project: '# DLNE memory\n', user: '# DLNE memory\n' }, ({ root, opened, offers }) => {
    assert.deepEqual(opened, [memoryFile(root)]);
    assert.deepEqual(offers, []);
  });
});

test('Open Agent Memory: with no project memory, your own is offered, and opened only when you take the offer', async () => {
  await withMemoryFiles({ user: '# DLNE memory\n', choice: 'Open Your Memory' }, ({ home, opened, offers }) => {
    assert.equal(offers.length, 1);
    assert.match(offers[0].message, /this project has no \.dlne\/memory\.md/);
    assert.match(offers[0].message, /~\/\.dlne\/memory\.md/);
    assert.deepEqual(offers[0].items, ['Open Your Memory']);
    assert.deepEqual(opened, [memoryFile(home)]);
  });
  await withMemoryFiles({ user: '# DLNE memory\n', choice: undefined }, ({ opened, offers }) => {
    assert.equal(offers.length, 1);
    assert.deepEqual(opened, [], 'the toast was closed: nothing opens');
  });
});

test('Open Agent Memory: with no folder open, your own is offered', async () => {
  await withMemoryFiles({ user: '# DLNE memory\n', folder: false, choice: 'Open Your Memory' }, ({ home, opened, offers }) => {
    assert.match(offers[0].message, /no folder is open/);
    assert.deepEqual(opened, [memoryFile(home)]);
  });
});

test('Open Agent Memory: with no memory anywhere it says so plainly, offers nothing and makes no file', async () => {
  await withMemoryFiles({}, ({ root, home, opened, offers }) => {
    assert.deepEqual(opened, []);
    assert.equal(offers.length, 1);
    assert.deepEqual(offers[0].items, []);
    assert.equal(offers[0].message,
      'DLNE: There is no agent memory yet: this project has no .dlne/memory.md, and you have no ~/.dlne/memory.md. The agent starts one when it first remembers something.');
    assert.equal(fs.existsSync(path.join(root, '.dlne')), false);
    assert.equal(fs.existsSync(path.join(home, '.dlne')), false);
  });
  await withMemoryFiles({ folder: false }, ({ offers }) => {
    assert.match(offers[0].message, /^DLNE: There is no agent memory yet: no folder is open, and you have no ~\/\.dlne\/memory\.md\./);
  });
  // A folder called memory.md is not a memory.
  const folders = ({ root, home }) => {
    fs.mkdirSync(memoryFile(root), { recursive: true });
    fs.mkdirSync(memoryFile(home), { recursive: true });
  };
  await withMemoryFiles({ before: folders }, ({ opened, offers }) => {
    assert.deepEqual(opened, []);
    assert.match(offers[0].message, /^DLNE: There is no agent memory yet/);
  });
});

test('Open Agent Memory: the manifest contributes the command and the extension registers it', () => {
  const pkg = JSON.parse(fs.readFileSync(path.join(ROOT, 'package.json'), 'utf8'));
  const command = pkg.contributes.commands.find((c) => c.command === 'dlne.memory.open');
  assert.deepEqual(command, { command: 'dlne.memory.open', title: 'DLNE: Open Agent Memory', category: 'DLNE' });
  const source = fs.readFileSync(path.join(ROOT, 'src', 'extension.ts'), 'utf8');
  assert.match(source, /registerCommand\('dlne\.memory\.open', \(\) => openAgentMemory\(\)\)/);
});
