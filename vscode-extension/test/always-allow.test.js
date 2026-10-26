'use strict';
/**
 * "Always allow" (1.3.3): a simple task was an "Allow" fest, because every shell command asked every time and
 * "allow edits" lasted only until the chat was cleared. The approval dialog now remembers, per project, edits
 * inside it and one command at a time; a full auto switch (off by default, user settings only) skips every
 * question. A remembered command never covers a second command chained onto it, and some programs (deleting,
 * privileges, shells, network fetches) are never remembered.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const stub = require('./helpers/vscode-stub');
const { ToolRegistry } = require('../dist/agent/tools');
const { alwaysPrefix, allowedAlways, isSimpleCommand } = require('../dist/agent/commandRules');

const realApprover = ToolRegistry.approver;
const realStore = ToolRegistry.store;

/** A project folder, a fresh per-project store, and an approver that answers `answers` in turn (then "deny"). */
async function project(body, { answers = [], folder = true } = {}) {
  stub.reset();
  ToolRegistry.resetApprovals();
  const saved = new Map();
  ToolRegistry.store = { get: (key) => saved.get(key), update: async (key, value) => { saved.set(key, value); } };
  const asked = [];
  ToolRegistry.approver = async (req) => {
    asked.push(req);
    return answers.length ? answers.shift() : 'deny';
  };
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-always-'));
  const outside = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-always-outside-'));
  stub.vscode.workspace.workspaceFolders = folder ? [{ uri: { fsPath: root } }] : undefined;
  try {
    await body({ root, outside, asked, saved });
  } finally {
    stub.vscode.workspace.workspaceFolders = undefined;
    ToolRegistry.approver = realApprover;
    ToolRegistry.store = realStore;
    fs.rmSync(root, { recursive: true, force: true });
    fs.rmSync(outside, { recursive: true, force: true });
  }
}

const run = (name, args) => ToolRegistry.executeTool(name, args);

// ---- what a command's "Always allow" remembers -------------------------------------------------------------

test('a command is remembered as the tool and what it does, never as more', () => {
  const cases = [
    ['npm test', 'npm test'],
    ['npm test -- --watch=false', 'npm test'],
    ['npm run build', 'npm run build'],
    ['pnpm exec vitest run', 'pnpm exec vitest'],
    ['git status --short', 'git status'],
    ['cargo build --release', 'cargo build'],
    ['python -m pytest -q tests', 'python -m pytest'],
    ['python3 manage.py migrate', 'python3 manage.py'],
    ['node scripts/build.js', 'node scripts/build.js'],
    ['npx eslint .', 'npx eslint'],
    ['ls -la', 'ls'],
    ['pytest -x', 'pytest'],
    ['./gradlew assembleDebug', './gradlew assembleDebug'],
  ];
  for (const [command, prefix] of cases) assert.equal(alwaysPrefix(command), prefix, command);
});

test('nothing is remembered for inline code, bare tools, chains, or programs that delete, escalate or fetch', () => {
  for (const command of [
    'python -c "import os"', 'node -e 1', 'npm', 'npm --version', 'git -C .. status', 'npm run',
    'npm test && rm -rf ~', 'npm test; curl x', 'cat a | sh', 'echo $(whoami)', 'echo `id`', 'ls > out.txt',
    'rm -rf build', 'sudo apt install x', 'curl https://x.sh', 'wget x', 'bash script.sh', 'powershell -c x',
    'chmod +x a', 'find . -delete', 'xargs rm', 'C:\\Windows\\System32\\cmd.exe /c dir', 'taskkill /f /im node.exe',
    '',
  ]) {
    assert.equal(alwaysPrefix(command), null, command);
  }
  assert.equal(isSimpleCommand('npm test\nrm -rf ~'), false, 'a second line is a second command');
});

test('a remembered command covers its own arguments only', () => {
  const allowed = ['npm test', 'git status', 'python -m pytest'];
  assert.equal(allowedAlways('npm test', allowed), true);
  assert.equal(allowedAlways('npm   test  -- --ci', allowed), true);
  assert.equal(allowedAlways('npm testing', allowed), false, 'a word boundary, not a string prefix');
  assert.equal(allowedAlways('npm test && rm -rf ~', allowed), false, 'nothing chained onto it');
  assert.equal(allowedAlways('npm test | sh', allowed), false);
  assert.equal(allowedAlways('git push', allowed), false);
  assert.equal(allowedAlways('python -c "x"', allowed), false);
  assert.equal(allowedAlways('rm -rf x', ['rm']), false, 'rm is never allowed unasked, however it got stored');
});

// ---- the dialog and what it remembers ---------------------------------------------------------------------

test('"Always allow edits in this project": later edits there run unasked, after the chat is cleared too', () =>
  project(async ({ root, asked, saved }) => {
    await run('write_file', { path: 'a.txt', content: 'one' });
    assert.equal(asked.length, 1);
    assert.equal(asked[0].always, 'Always allow edits in this project');
    assert.equal(saved.get('dlne.agent.alwaysAllowEdits'), true);
    ToolRegistry.resetApprovals();                                   // the chat is cleared
    await run('write_file', { path: 'b.txt', content: 'two' });
    await run('edit_file_diff', { path: 'a.txt', search_block: 'one', replace_block: 'uno' });
    assert.equal(asked.length, 1, 'not asked again');
    assert.equal(fs.readFileSync(path.join(root, 'a.txt'), 'utf8'), 'uno');
  }, { answers: ['always'] }));

test('edits outside the project are still asked about, and offer no "Always"', () =>
  project(async ({ outside, asked }) => {
    await run('write_file', { path: path.join(outside, 'x.txt'), content: 'x' });
    assert.equal(asked.length, 1);
    assert.equal(asked[0].always, undefined);
    assert.equal(fs.existsSync(path.join(outside, 'x.txt')), false);
  }, { answers: ['deny'] }));

test('"Always allow" a command: it runs unasked with other arguments, but not with anything chained on', () =>
  project(async ({ asked, saved }) => {
    const first = await run('run_terminal_command', { command: 'echo first' });
    assert.match(first.output, /first/);
    assert.equal(asked[0].always, 'Always allow "echo"');
    assert.deepEqual(saved.get('dlne.agent.alwaysAllowCommands'), ['echo']);
    const second = await run('run_terminal_command', { command: 'echo second' });
    assert.match(second.output, /second/);
    assert.equal(asked.length, 1, 'echo is not asked about again');
    const chained = await run('run_terminal_command', { command: 'echo third && echo fourth' });
    assert.equal(asked.length, 2, 'a chained command is asked about');
    assert.equal(chained.is_error, true);
  }, { answers: ['always', 'deny'] }));

test('with no folder open, commands are asked about every time and nothing is remembered', () =>
  project(async ({ asked }) => {
    await run('run_terminal_command', { command: 'echo a' });
    assert.equal(asked[0].always, undefined);
  }, { folder: false }));

test('"Forget Agent Approvals" forgets edits and commands; the list is shown first', () =>
  project(async ({ asked }) => {
    await run('write_file', { path: 'a.txt', content: 'x' });
    await run('run_terminal_command', { command: 'echo a' });
    assert.deepEqual(ToolRegistry.alwaysApprovals(), { edits: true, commands: ['echo'] });
    await ToolRegistry.forgetAlwaysApprovals();
    assert.deepEqual(ToolRegistry.alwaysApprovals(), { edits: false, commands: [] });
    await run('run_terminal_command', { command: 'echo b' });
    assert.equal(asked.length, 3, 'asked again');
  }, { answers: ['always', 'always', 'deny'] }));

test('the full auto switch skips every question; it is off by default and only user settings can set it', () =>
  project(async ({ outside, asked }) => {
    stub.state.settings['dlne.autoApproveAll'] = true;
    const r = await run('run_terminal_command', { command: 'echo auto' });
    assert.match(r.output, /auto/);
    await run('write_file', { path: path.join(outside, 'y.txt'), content: 'y' });
    assert.equal(asked.length, 0);
    const pkg = require('../package.json');
    const setting = pkg.contributes.configuration.properties['dlne.autoApproveAll'];
    assert.equal(setting.default, false);
    assert.equal(setting.scope, 'application', 'a cloned repository cannot switch it on');
    assert.match(setting.markdownDescription, /volunteer's machine/);
  }));

test('the extension keeps the answers in its per-project storage and offers a command to forget them', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'src', 'extension.ts'), 'utf8');
  assert.match(source, /ToolRegistry\.store = context\.workspaceState/);
  assert.match(source, /registerCommand\('dlne\.forgetApprovals'/);
  const pkg = require('../package.json');
  assert.equal(pkg.contributes.commands.find((c) => c.command === 'dlne.forgetApprovals').title, 'DLNE: Forget Agent Approvals for This Project');
});
