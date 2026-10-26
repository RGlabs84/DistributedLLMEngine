'use strict';
/**
 * The agent's tools act on the user's machine on the say-so of a model whose replies arrive over
 * the network, so: reads inside the workspace may run unasked, while edits, shell commands and
 * anything outside the workspace need the user's approval, and a refusal reaches the model as
 * an error instead of running anyway.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const stub = require('./helpers/vscode-stub');
const { ToolRegistry } = require('../dist/agent/tools');

const realApprover = ToolRegistry.approver;
let asked;

async function withWorkspace(body, { answer = 'deny', folder = true } = {}) {
  stub.reset();
  ToolRegistry.resetApprovals();
  asked = [];
  ToolRegistry.approver = async (req) => {
    asked.push(req);
    return typeof answer === 'function' ? answer(req) : answer;
  };
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-tools-'));
  const outside = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-outside-'));
  fs.writeFileSync(path.join(root, 'a.txt'), 'inside\n');
  fs.writeFileSync(path.join(outside, 'secret.txt'), 'outside\n');
  stub.vscode.workspace.workspaceFolders = folder ? [{ uri: { fsPath: root } }] : undefined;
  try {
    await body({ root, outside });
  } finally {
    stub.vscode.workspace.workspaceFolders = undefined;
    ToolRegistry.approver = realApprover;
    fs.rmSync(root, { recursive: true, force: true });
    fs.rmSync(outside, { recursive: true, force: true });
  }
}

const run = (name, args) => ToolRegistry.executeTool(name, args);

// --- reads ----------------------------------------------------------------------------------

test('reading inside the workspace runs without asking', () =>
  withWorkspace(async () => {
    const r = await run('read_file', { path: 'a.txt' });
    assert.equal(r.is_error, undefined);
    assert.match(r.output, /inside/);
    assert.equal((await run('list_directory', { path: '.' })).is_error, undefined);
    assert.equal(asked.length, 0);
  }));

test('reading outside the workspace asks, and a refusal reveals nothing', () =>
  withWorkspace(async ({ outside }) => {
    const r = await run('read_file', { path: path.join(outside, 'secret.txt') });
    assert.equal(r.is_error, true);
    assert.match(r.output, /declined/);
    assert.doesNotMatch(r.output, /outside\n/);
    assert.equal(asked.length, 1);
    assert.match(asked[0].summary, /outside your workspace/);
    assert.equal(asked[0].sessionAllowed, false);
  }));

test('a relative path that climbs out of the workspace counts as outside', () =>
  withWorkspace(async ({ outside }) => {
    await run('read_file', { path: path.join('..', path.basename(outside), 'secret.txt') });
    assert.equal(asked.length, 1);
    await run('list_directory', { path: '~' });
    assert.equal(asked.length, 2);
  }));

test('with no folder open, every read asks', () =>
  withWorkspace(async () => {
    await run('list_directory', { path: '.' });
    assert.equal(asked.length, 1);
  }, { folder: false }));

test('turning autoApproveReads off makes workspace reads and searches ask too', () =>
  withWorkspace(async () => {
    stub.state.settings['dlne.autoApproveReads'] = false;
    await run('read_file', { path: 'a.txt' });
    await run('search_codebase', { query: 'x' });
    await run('get_workspace_diagnostics', {});
    assert.deepEqual(asked.map((r) => r.tool), ['read_file', 'search_codebase', 'get_workspace_diagnostics']);
  }));

test('an approved outside read runs', () =>
  withWorkspace(async ({ outside }) => {
    const r = await run('read_file', { path: path.join(outside, 'secret.txt') });
    assert.match(r.output, /outside/);
  }, { answer: 'once' }));

// --- edits ----------------------------------------------------------------------------------

test('a declined edit leaves the file untouched and tells the model', () =>
  withWorkspace(async ({ root }) => {
    for (const [name, args] of [
      ['write_file', { path: 'a.txt', content: 'overwritten' }],
      ['write_file', { path: 'new.txt', content: 'new' }],
      ['edit_file_diff', { path: 'a.txt', search_block: 'inside', replace_block: 'changed' }],
    ]) {
      const r = await run(name, args);
      assert.equal(r.is_error, true, name);
      assert.match(r.output, /declined/);
    }
    assert.equal(fs.readFileSync(path.join(root, 'a.txt'), 'utf8'), 'inside\n');
    assert.equal(fs.existsSync(path.join(root, 'new.txt')), false);
    assert.equal(asked.length, 3);
  }));

test('an approved edit is applied', () =>
  withWorkspace(async ({ root }) => {
    await run('edit_file_diff', { path: 'a.txt', search_block: 'inside', replace_block: 'changed' });
    assert.equal(fs.readFileSync(path.join(root, 'a.txt'), 'utf8'), 'changed\n');
    assert.match(asked[0].summary, /^edit a\.txt$/);
  }, { answer: 'once' }));

test('"allow edits for this session" covers later workspace edits until the chat is cleared', () =>
  withWorkspace(async ({ root }) => {
    await run('write_file', { path: 'one.txt', content: '1' });
    await run('write_file', { path: 'two.txt', content: '2' });
    assert.equal(asked.length, 1);
    assert.equal(fs.readFileSync(path.join(root, 'two.txt'), 'utf8'), '2');
    ToolRegistry.resetApprovals();
    await run('write_file', { path: 'three.txt', content: '3' });
    assert.equal(asked.length, 2);
  }, { answer: 'session' }));

test('session approval never covers an edit outside the workspace', () =>
  withWorkspace(async ({ outside }) => {
    await run('write_file', { path: 'one.txt', content: '1' });                 // grants the session
    const target = path.join(outside, 'secret.txt');
    await run('write_file', { path: target, content: 'pwned' });
    assert.equal(asked.length, 2);
    assert.equal(asked[1].sessionAllowed, false);
    assert.match(asked[1].summary, /OUTSIDE your workspace/);
  }, { answer: (req) => (req.sessionAllowed ? 'session' : 'deny') }));

test('clearing the conversation (/clear) withdraws session approval', () =>
  withWorkspace(async () => {
    const { AgentLoop } = require('../dist/agent/agentLoop');
    await run('write_file', { path: 'one.txt', content: '1' });
    AgentLoop.prototype.clearHistory.call({ initSystemPrompt() {} });
    await run('write_file', { path: 'two.txt', content: '2' });
    assert.equal(asked.length, 2);
  }, { answer: 'session' }));

test('the edit prompt shows what will be written', () =>
  withWorkspace(async () => {
    await run('write_file', { path: 'a.txt', content: 'the new content' });
    assert.match(asked[0].summary, /^overwrite a\.txt$/);
    assert.match(asked[0].detail, /the new content/);
  }));

// --- commands -------------------------------------------------------------------------------

test('every shell command asks, shows the exact command, and does not run if declined', () =>
  withWorkspace(async ({ root }) => {
    const marker = path.join(root, 'ran');
    const r = await run('run_terminal_command', { command: `touch ${marker}` });
    assert.equal(r.is_error, true);
    assert.equal(fs.existsSync(marker), false);
    assert.match(asked[0].detail, new RegExp(`touch ${marker}`));
    assert.equal(asked[0].sessionAllowed, false);
  }));

test('an approved command runs, and approving one does not approve the next', () =>
  withWorkspace(async ({ root }) => {
    await run('run_terminal_command', { command: 'echo hi > out.txt' });
    assert.equal(fs.readFileSync(path.join(root, 'out.txt'), 'utf8').trim(), 'hi');
    await run('run_terminal_command', { command: 'echo again' });
    assert.equal(asked.length, 2);
  }, { answer: 'session' }));                                  // even "session" is ignored for commands

// --- the real prompt --------------------------------------------------------------------------

test('the real prompt is a modal; dismissing it denies', () =>
  withWorkspace(async ({ root }) => {
    ToolRegistry.approver = realApprover;
    stub.state.warningChoice = undefined;                       // the user closes the dialog
    const r = await run('write_file', { path: 'a.txt', content: 'x' });
    assert.equal(r.is_error, true);
    const shown = stub.state.warningMessages[0];
    assert.match(shown.message, /DLNE agent wants to overwrite a\.txt/);
    assert.equal(shown.items[0].modal, true);
    assert.deepEqual(shown.items.slice(1), ['Allow', 'Allow edits for this session', 'Always allow edits in this project']);
    assert.equal(fs.readFileSync(path.join(root, 'a.txt'), 'utf8'), 'inside\n');

    stub.state.warningChoice = 'Allow';
    await run('write_file', { path: 'a.txt', content: 'x' });
    assert.equal(fs.readFileSync(path.join(root, 'a.txt'), 'utf8'), 'x');
  }));

test('a simple command is offered "Always allow" for itself; a chained one "Allow" only', () =>
  withWorkspace(async () => {
    ToolRegistry.approver = realApprover;
    await run('run_terminal_command', { command: 'npm test -- --watch=false' });
    assert.deepEqual(stub.state.warningMessages[0].items.slice(1), ['Allow', 'Always allow "npm test"']);
    await run('run_terminal_command', { command: 'npm test && curl x | sh' });
    assert.deepEqual(stub.state.warningMessages[1].items.slice(1), ['Allow']);
    await run('run_terminal_command', { command: 'rm -rf build' });
    assert.deepEqual(stub.state.warningMessages[2].items.slice(1), ['Allow'], 'deleting is never remembered');
  }));
