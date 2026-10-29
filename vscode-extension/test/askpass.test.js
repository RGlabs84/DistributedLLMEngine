/**
 * sudo for the agent's commands (src/agent/askpass.ts). They run without a terminal, so sudo used to stop with
 * "a terminal is required to read the password". Now SUDO_ASKPASS points at a helper that asks through VS Code.
 * These run the real helper script and its real main against the real socket; only the password box is replaced.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const child_process = require('node:child_process');
const fs = require('node:fs');

const stub = require('./helpers/vscode-stub');
const { withAskpass, sudoPrompt, forgetSudoPassword } = require('../dist/agent/askpass');
const { ToolRegistry } = require('../dist/agent/tools');

const posix = { skip: process.platform === 'win32' ? 'sudo and askpass are POSIX' : false };
const realAsk = sudoPrompt.ask;      // the centred dialog and the password box; the tests below replace it

/** What sudo does: run the helper with its prompt, read the password from its stdout. */
function sudoAsks(env, prompt = '[sudo] password for you: ') {
  return new Promise((resolve) => {
    child_process.execFile(env.SUDO_ASKPASS, [prompt], { env: { ...process.env, ...env } },
      (err, stdout) => resolve({ code: err ? err.code : 0, stdout }));
  });
}

function answering(answers) {
  const asked = [];
  sudoPrompt.ask = async (prompt, command) => {
    asked.push({ prompt, command });
    return answers.length ? answers.shift() : undefined;
  };
  return asked;
}

test('sudo gets the password typed in the box, and nothing when the box is closed', posix, async () => {
  forgetSudoPassword();
  const asked = answering([{ password: 's3cret', remember: false }, undefined]);
  const typed = await withAskpass('sudo pacman -S tk', 10000, (env) => sudoAsks(env));
  assert.deepEqual(typed, { code: 0, stdout: 's3cret\n' });
  assert.equal(asked[0].command, 'sudo pacman -S tk', 'the box names the command asking');
  assert.equal(asked[0].prompt, '[sudo] password for you:');
  const closed = await withAskpass('sudo true', 10000, (env) => sudoAsks(env));
  assert.equal(closed.code, 1);
  assert.equal(closed.stdout, '');
});

test('the helper answers only while an agent command runs', posix, async () => {
  forgetSudoPassword();
  const asked = answering([{ password: 'never', remember: false }]);
  let kept;
  await withAskpass('echo done', 10000, async (env) => { kept = env; });
  const late = await sudoAsks(kept);
  assert.equal(late.code, 1);
  assert.equal(asked.length, 0, 'nobody is asked for a password on behalf of a command that has finished');
  assert.ok((fs.statSync(kept.SUDO_ASKPASS).mode & 0o077) === 0, 'only this user can run the helper');
});

test('"Remember" answers every later sudo of the session, Auto runs included, until forgotten or refused', posix, async () => {
  forgetSudoPassword();
  const asked = answering([{ password: 'pw', remember: true }, { password: 'new', remember: true },
                           { password: 'again', remember: false }]);
  assert.equal((await withAskpass('sudo a', 10000, sudoAsks)).stdout, 'pw\n');
  assert.equal((await withAskpass('sudo b', 10000, sudoAsks)).stdout, 'pw\n');
  assert.equal(asked.length, 1, 'remembered: not asked again');

  // sudo asks again when the answer was wrong: the remembered password is dropped and the user asked.
  const retried = await withAskpass('sudo c', 10000, async (env) => { await sudoAsks(env); return sudoAsks(env); });
  assert.equal(retried.stdout, 'new\n');
  assert.equal(asked.length, 2);

  await ToolRegistry.forgetAlwaysApprovals();          // "DLNE: Forget Agent Approvals" forgets it too
  assert.equal((await withAskpass('sudo d', 10000, sudoAsks)).stdout, 'again\n');
  assert.equal(asked.length, 3);
});

test('a command that ends withdraws its open prompt, so no stale box is left and sudo cannot hold the agent', posix, async () => {
  // 2026-10-27: unseen boxes of timed-out commands stayed open and stacked; one was answered (Remember ticked) after
  // its command had gone, and the live command asked again. sudo also ignored the timeout's signal while it waited.
  forgetSudoPassword();
  let withdrawn = false;
  sudoPrompt.ask = (prompt, command, token) => new Promise((resolve) => {
    token.onCancellationRequested(() => { withdrawn = true; resolve(undefined); });
  });
  const started = Date.now();
  const r = await withAskpass('sudo slow', 300, sudoAsks);
  assert.equal(r.code, 1);
  assert.ok(withdrawn && Date.now() - started < 5000);
});

test('a sudo that gave up for want of a password tells the model plain sudo works', posix, async () => {
  stub.reset();
  const r = await ToolRegistry.executeTool('run_terminal_command',
    { command: 'echo "sudo: a password is required" >&2; exit 1' }, true);
  assert.match(r.output, /plain `sudo <command>` works here/);
  assert.match(r.output, /Do not use `sudo -S`, `sudo -n`/);
  const ok = await ToolRegistry.executeTool('run_terminal_command', { command: 'echo fine' }, true);
  assert.doesNotMatch(ok.output, /sudo/);
  // pkexec opens the desktop's own dialog every time and never sees "Remember"
  const polkit = await ToolRegistry.executeTool('run_terminal_command', { command: 'true || pkexec true' }, true);
  assert.match(polkit.output, /use plain `sudo <command>` rather than pkexec/);
});

test("the agent's commands run with the helper in place", posix, async () => {
  stub.reset();
  forgetSudoPassword();
  answering([]);
  const r = await ToolRegistry.executeTool('run_terminal_command',
    { command: 'test -x "$SUDO_ASKPASS" && test -n "$DLNE_ASKPASS_ID" && echo has-askpass' }, true);
  assert.match(r.output, /has-askpass/);
});

test('the user is asked in a centred dialog first, then the password box, and chooses Remember there', async () => {
  stub.reset();
  const boxes = [];
  stub.vscode.window.showInputBox = async (options, token) => { boxes.push({ options, token }); return 's3cret'; };
  const token = { isCancellationRequested: false, onCancellationRequested: () => ({ dispose() {} }) };
  try {
    stub.state.warningChoice = 'Enter and Remember for This Session';
    assert.deepEqual(await realAsk('[sudo] password for you: ', 'sudo pacman -S tk', token), { password: 's3cret', remember: true });
    const dialog = stub.state.warningMessages[0];
    assert.equal(dialog.message, 'The DLNE agent needs your sudo password');
    assert.equal(dialog.items[0].modal, true, 'modal: centred, where it cannot be missed');
    assert.match(dialog.items[0].detail, /sudo pacman -S tk/);
    assert.match(dialog.items[0].detail, /Auto runs included/);
    assert.equal(boxes[0].options.password, true);
    assert.equal(boxes[0].token, token, 'withdrawn with its command');

    stub.state.warningChoice = 'Enter Password';
    assert.deepEqual(await realAsk('', 'sudo true', token), { password: 's3cret', remember: false });
    stub.state.warningChoice = undefined;                                   // Cancel
    assert.equal(await realAsk('', 'sudo true', token), undefined);
    stub.state.warningChoice = 'Enter Password';
    token.isCancellationRequested = true;                                   // the command ended while the dialog was up
    assert.equal(await realAsk('', 'sudo true', token), undefined);
    assert.equal(boxes.length, 2, 'no password box for a command that has gone');
  } finally {
    delete stub.vscode.window.showInputBox;
  }
});
