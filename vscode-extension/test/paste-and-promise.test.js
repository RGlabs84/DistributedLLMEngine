'use strict';
/**
 * @dlne "completing instantly" on a big pasted log (the owner, 2026-10-30; the run log at 01:28–01:29 and the saved
 * thread showed both causes), mended in 1.5.3:
 * (a) a paste too long for one message was cut to ATTACHED_MAX_CHARS, with no way for the agent to see the rest. It is
 *     now kept whole in the extension's own storage, the message carries its start and its path, and read_file pages
 *     through it without asking (nothing is in that folder but what the user pasted, secrets already hidden);
 * (b) the model announced a step ("Let me read that file to see what we've been through.") without calling a tool, and
 *     the loop took that as the final answer: a finished run in one second. Once per run it is now told to take it.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const stub = require('./helpers/vscode-stub');
const { ToolRegistry } = require('../dist/agent/tools');
const { AgentLoop, PROMISE_NUDGE, promisesAStep } = require('../dist/agent/agentLoop');
const { attachedContext, ATTACHED_MAX_CHARS } = require('../dist/views/chatReferences');
const { setPastedDir, savePasted, isPastedFile, prunePasted, MAX_PASTES } = require('../dist/agent/pasted');

// The agent reads ~/.dlne/memory.md: a home of its own keeps these runs out of the real one.
process.env.HOME = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-paste-home-'));
const tmp = (prefix) => fs.mkdtempSync(path.join(os.tmpdir(), prefix));

// ---- (b) a step announced and not taken ---------------------------------------------------------------------------

test('a reply that ends by announcing an action is told apart from a closing courtesy', () => {
  for (const reply of [
    'Let me read that file to see what we\'ve been through.',
    'Let me check the current state of the workspace and logs.',
    'The build failed. Now I\'ll look at the linker error:',
    'OK, I will run the tests again.',
    'Let me also check the config.',
    'I\'m going to go through the log from the start.',
    'Got it.\nNext, let\'s open main.c',
  ]) assert.equal(promisesAStep(reply), true, reply);
  for (const reply of [
    'Done. The module builds cleanly. Let me know if you want anything else.',
    'I\'ll wait for your answer before rebooting.',
    'All three tests pass.',
    'Let me be clear: the change is safe.',
    'I will be here if you need me.',
    '',
    'x'.repeat(5000) + ' Let me check it.',
  ]) assert.equal(promisesAStep(reply), false, reply.slice(0, 60));
});

/** A client that answers the agent's requests with these replies, in order, and keeps what it was sent. */
function scripted(replies) {
  const sent = [];
  return {
    sent,
    streamChat: async (messages, tools, onDelta) => {
      sent.push({ messages: JSON.parse(JSON.stringify(messages)), tools: tools.length });
      const reply = replies[sent.length - 1] ?? 'Out of script.';
      onDelta(reply);
      return { content: reply, toolCalls: [], finishReason: 'stop' };
    },
  };
}
const callbacks = () => ({ onThinkingDelta() {}, onContentDelta() {}, onToolStart() {}, onToolEnd() {} });

test('a step announced and not taken is asked for once: the model takes it, or answers', async () => {
  stub.reset();
  const client = scripted(['Let me read that file to see what we\'ve been through.', 'Here is what the log shows: it booted.']);
  const outcome = await new AgentLoop(client).run('please review the log', callbacks());
  assert.equal(outcome, 'done');
  assert.equal(client.sent.length, 2, 'one more turn, not an end');
  const second = client.sent[1];
  assert.equal(second.messages.at(-1).content, PROMISE_NUDGE);
  assert.ok(second.tools > 0, 'with its tools, to take the step');
});

test('only once per run: a model that keeps announcing ends the run, and the next run may be nudged again', async () => {
  stub.reset();
  const client = scripted(['Let me check the logs.', 'Let me check the logs.', 'Let me look at it.', 'Fine.']);
  const loop = new AgentLoop(client);
  assert.equal(await loop.run('check it', callbacks()), 'done');
  assert.equal(client.sent.length, 2);
  await loop.run('again', callbacks());
  assert.equal(client.sent.length, 4, 'a new run has its own nudge');
});

test('an ordinary answer is not nudged', async () => {
  stub.reset();
  const client = scripted(['All done: the module builds and loads. Let me know if you want the reboot steps.']);
  await new AgentLoop(client).run('build it', callbacks());
  assert.equal(client.sent.length, 1);
});

// ---- (a) a paste too long for one message --------------------------------------------------------------------------

const PASTE = Array.from({ length: 18000 }, (_, i) => `[${i + 1}] boot line ${i + 1}: gpu_unlock probe state ok`).join('\n') + '\n';

test('a paste too long for the message is kept whole, and the message carries its start and where the rest is', async () => {
  stub.reset();
  const dir = tmp('dlne-pasted-');
  setPastedDir(dir);
  try {
    const attached = await attachedContext([{ id: 'vscode.paste', name: 'Pasted text #1', value: PASTE }]);
    const saved = /all of it is saved at (\S+)\. /.exec(attached.text)?.[1];
    assert.ok(saved, attached.text.slice(-400));
    assert.equal(fs.readFileSync(saved, 'utf8'), PASTE, 'all of it, as pasted');
    assert.equal(path.dirname(saved), dir);
    assert.ok(attached.text.length < ATTACHED_MAX_CHARS + 600, 'the message stays within its room');
    assert.match(attached.text, /^1: \[1\] boot line 1:/m, 'its first lines, numbered as read_file prints them');
    const shown = Number(/\(the first (\d+) of 18000 lines\)/.exec(attached.text)?.[1]);
    assert.ok(shown > 50, `${shown} lines shown`);
    assert.match(attached.text, new RegExp(`from start_line ${shown + 1}`));
    assert.deepEqual(attached.skipped, ['Pasted text #1 is too long to send whole (18000 lines): the agent was given its start and reads the rest from a copy kept by DLNE.']);
    if (process.platform !== 'win32') assert.equal(fs.statSync(saved).mode & 0o777, 0o600);
  } finally {
    setPastedDir(undefined);
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

// (That secrets in a kept paste are hidden is tested with the other fake secrets, in redact.test.js.)
test('a short paste is still sent as it is, and not kept', async () => {
  stub.reset();
  const dir = tmp('dlne-pasted-');
  setPastedDir(dir);
  try {
    const short = await attachedContext([{ id: 'vscode.paste', name: 'Pasted text #2', value: 'just a few words' }]);
    assert.match(short.text, /--- Pasted text #2 ---\njust a few words\n--- end of Pasted text #2 ---/);
    assert.deepEqual(fs.readdirSync(dir), [], 'a short paste is not kept');
  } finally {
    setPastedDir(undefined);
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('with nowhere to keep it, a long paste is cut as before, and the user is told', async () => {
  stub.reset();
  setPastedDir(undefined);
  const attached = await attachedContext([{ id: 'vscode.paste', name: 'Pasted text #1', value: PASTE }]);
  assert.match(attached.text, /\[\.\.\. cut: \d+ more characters\.\]/);
  assert.match(attached.skipped[0], /^Pasted text #1 was cut to its first \d+ characters/);
});

test('read_file pages through a kept paste without asking; anything else in that folder, or outside, still asks', async () => {
  stub.reset();
  ToolRegistry.resetApprovals();
  const realApprover = ToolRegistry.approver;
  const asked = [];
  ToolRegistry.approver = async (req) => { asked.push(req); return 'deny'; };
  const root = tmp('dlne-paste-ws-');
  const dir = tmp('dlne-pasted-');
  stub.vscode.workspace.workspaceFolders = [{ uri: { fsPath: root } }];
  setPastedDir(dir);
  try {
    const saved = savePasted(PASTE);
    assert.ok(isPastedFile(saved));
    const page = await ToolRegistry.executeTool('read_file', { path: saved, start_line: 9000, end_line: 9002 });
    assert.equal(page.is_error, undefined, page.output);
    assert.match(page.output, /boot line 9000/);
    assert.equal(asked.length, 0);

    const other = path.join(dir, 'notes.txt');
    fs.writeFileSync(other, 'not a paste\n');
    assert.equal(isPastedFile(other), false);
    assert.equal((await ToolRegistry.executeTool('read_file', { path: other })).is_error, true);
    const link = path.join(dir, 'pasted-link.txt');
    fs.symlinkSync(path.join(os.homedir(), 'nothing-here'), link);
    assert.equal(isPastedFile(link), false, 'a link out of the folder is not a paste');
    assert.equal(asked.length, 1, 'the other file was asked about');
  } finally {
    ToolRegistry.approver = realApprover;
    stub.vscode.workspace.workspaceFolders = undefined;
    setPastedDir(undefined);
    fs.rmSync(root, { recursive: true, force: true });
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('the newest pastes are kept, none older than a week', () => {
  const dir = tmp('dlne-pasted-');
  setPastedDir(dir);
  try {
    const now = Date.now();
    const old = savePasted('old one', now);
    fs.utimesSync(old, new Date(now - 8 * 24 * 3600 * 1000), new Date(now - 8 * 24 * 3600 * 1000));
    for (let i = 0; i < MAX_PASTES + 3; i++) savePasted(`paste ${i}`, now);
    prunePasted(now);
    const left = fs.readdirSync(dir);
    assert.ok(left.length <= MAX_PASTES, `${left.length} kept`);
    assert.equal(fs.existsSync(old), false, 'the week-old one went');
  } finally {
    setPastedDir(undefined);
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
