'use strict';
/**
 * How a run of the agent is started, stopped and kept apart from others (1.5.0, plan item 2), at the level of AgentLoop and
 * the tools: a run that yields between steps, runs on one conversation that never overlap, callbacks and tools that throw,
 * a retry pause that Stop cuts short, and what Stop does to a command and to a question waiting for the user.
 * (Through the whole participant: chat-threads.test.js.)
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const childProcess = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const { scriptedClient, toolCall, gate, until, workspace, stub } = require('./helpers/chat-harness');
const { ownProcesses, killOwn } = require('./helpers/own-processes');
const { AgentLoop } = require('../dist/agent/agentLoop');
const { ToolRegistry, STOPPED_NOTICE, endCommand, runShell } = require('../dist/agent/tools');
const { EngineDropped } = require('../dist/client/dlneClient');

const posix = { skip: process.platform === 'win32' && 'POSIX shell commands' };
const realExecute = ToolRegistry.executeTool;
const realApprover = ToolRegistry.approver;
const realHandover = AgentLoop.handoverMs;
test.afterEach(() => {
  ToolRegistry.executeTool = realExecute;
  ToolRegistry.approver = realApprover;
  AgentLoop.handoverMs = realHandover;
  stub.vscode.workspace.workspaceFolders = undefined;
});

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function recorder() {
  const seen = { content: '', progress: [], tools: [], toolResults: [], errors: [] };
  return {
    seen,
    callbacks: {
      onThinkingDelta() {},
      onContentDelta: (d) => { seen.content += d; },
      onToolStart: (name) => seen.tools.push(name),
      onToolEnd: (result) => seen.toolResults.push(result),
      onLoadingProgress: (line) => seen.progress.push(line),
      onError: (err) => seen.errors.push(err),
    },
  };
}

/** The model's view of the conversation after a run: role and a short form of the content, system prompt left out. */
const shape = (loop) => loop.messages.filter((m) => m.role !== 'system')
  .map((m) => (m.role === 'assistant' && m.tool_calls ? `assistant[${m.tool_calls.map((c) => c.function.name).join(',')}]` : `${m.role}:${String(m.content).slice(0, 24)}`));

/** Every tool call of the conversation has its result, and every result a call (the engine refuses anything else). */
function callsPaired(loop) {
  const open = new Set();
  for (const m of loop.messages) {
    if (m.role === 'assistant') for (const c of m.tool_calls ?? []) open.add(c.id);
    if (m.role === 'tool') {
      if (!open.has(m.tool_call_id)) return false;
      open.delete(m.tool_call_id);
    }
  }
  return open.size === 0;
}

// ---- yielding -----------------------------------------------------------------------------------------------

test('a run told to yield ends between two steps, with every tool call answered, and the next run carries on from there', async () => {
  stub.reset();
  const executed = [];
  ToolRegistry.executeTool = async (name, args) => { executed.push(args.path); return { name, output: `1: ${args.path}` }; };
  const client = scriptedClient([
    { toolCalls: [toolCall('read_file', { path: 'a.ts' }), toolCall('read_file', { path: 'b.ts' })] },
    { toolCalls: [toolCall('read_file', { path: 'c.ts' })] },
    { content: 'Understood, stopping the reading.' },
  ]);
  const loop = new AgentLoop(client, { log() {} });
  const { callbacks } = recorder();
  let yieldNow = false;
  const outcome = await loop.run('read the files', {
    ...callbacks,
    onToolStart: (name, args) => { if (args.path === 'a.ts') yieldNow = true; }, // the user's message arrives during the first step
  }, { shouldYield: () => yieldNow });
  assert.equal(outcome, 'yielded');
  assert.deepEqual(executed, ['a.ts', 'b.ts'], 'the step in progress ran to its end, both calls, and no further step began');
  assert.equal(client.requests.length, 1);
  assert.equal(callsPaired(loop), true);
  assert.deepEqual(shape(loop), ['user:read the files', 'assistant[read_file,read_file]', 'tool:1: a.ts', 'tool:1: b.ts']);

  yieldNow = false;
  assert.equal(await loop.run('only a.ts matters', callbacks), 'done');
  assert.equal(client.requests[1].messages.at(-1).content, 'only a.ts matters', 'the newer message follows the finished step');
  assert.equal(client.requests[1].messages.at(-2).role, 'tool');
});

test('a run asked to yield before its first step records the user\'s words and does nothing else', async () => {
  stub.reset();
  const client = scriptedClient([{ content: 'never asked' }]);
  const loop = new AgentLoop(client, { log() {} });
  assert.equal(await loop.run('first', recorder().callbacks, { shouldYield: () => true }), 'yielded');
  assert.equal(client.requests.length, 0);
  assert.deepEqual(shape(loop), ['user:first']);
});

test('no look back after a long task while a newer message waits: it is housekeeping, and the message is next', async () => {
  stub.reset();
  ToolRegistry.executeTool = async (name) => ({ name, output: 'ok' });
  // Another line each time: the same read five times running is what the loop check stops (loopGuard.ts).
  const steps = Array.from({ length: 5 }, (_, i) => ({ toolCalls: [toolCall('read_file', { path: 'x.ts', start_line: i + 1, end_line: i + 1 })] }));
  let waiting = false;
  const client = scriptedClient([...steps, () => { waiting = true; return { content: 'All read.' }; }, { content: 'nothing' }]);
  const loop = new AgentLoop(client, { log() {} });
  assert.equal(await loop.run('read x five times', recorder().callbacks, { reflect: true, shouldYield: () => waiting }), 'done');
  assert.equal(client.requests.length, 6, 'five steps and the answer; the look back is a seventh request that was not made');
});

// ---- runs on one conversation never overlap -----------------------------------------------------------------

test('a run that starts while another is going stops it and begins when it has ended: its late step lands before the new words', async () => {
  stub.reset();
  const slow = gate();
  let started = false;
  ToolRegistry.executeTool = async (name) => { started = true; await slow.opened; return { name, output: 'slow result' }; };
  const client = scriptedClient([{ toolCalls: [toolCall('read_file', { path: 'a.ts' })] }, { content: 'Done.' }]);
  const loop = new AgentLoop(client, { log() {} });
  const first = loop.run('one', recorder().callbacks);
  await until(() => started);
  const second = loop.run('two', recorder().callbacks);
  await sleep(50);
  assert.equal(client.requests.length, 1, 'the second run has not begun');
  assert.deepEqual(shape(loop), ['user:one'], 'and has not touched the conversation');
  slow.open();
  assert.equal(await first, 'aborted');
  assert.equal(await second, 'done');
  assert.deepEqual(shape(loop), ['user:one', 'assistant[read_file]', 'tool:slow result', 'user:two', 'assistant:Done.']);
  assert.equal(callsPaired(loop), true);
});

test('a run that never ends is cut loose after the hand-over time: it adds nothing to the conversation, and the new run goes ahead', async () => {
  stub.reset();
  AgentLoop.handoverMs = 80;
  const stuck = gate();
  let started = false;
  ToolRegistry.executeTool = async (name) => { started = true; await stuck.opened; return { name, output: 'too late' }; };
  const client = scriptedClient([{ toolCalls: [toolCall('read_file', { path: 'a.ts' })] }, { content: 'Fresh start.' }]);
  const notes = [];
  const loop = new AgentLoop(client, { log: (line) => notes.push(line) });
  const first = loop.run('one', recorder().callbacks);
  await until(() => started);
  assert.equal(await loop.run('two', recorder().callbacks), 'done', 'the new run did not wait for ever');
  assert.ok(notes.some((line) => /run 2 went ahead: the run before it had not ended/.test(line)));
  stuck.open();
  assert.equal(await first, 'aborted');
  assert.ok(!shape(loop).some((entry) => /too late/.test(entry)), 'nothing from the cut-loose run reached the conversation');
  assert.equal(shape(loop).at(-1), 'assistant:Fresh start.');
});

test('a run stopped while it waits for its turn ends without touching the conversation', async () => {
  stub.reset();
  const slow = gate();
  let started = false;
  ToolRegistry.executeTool = async (name) => { started = true; await slow.opened; return { name, output: 'r' }; };
  const client = scriptedClient([{ toolCalls: [toolCall('read_file', { path: 'a.ts' })] }, { content: 'Done.' }]);
  const loop = new AgentLoop(client, { log() {} });
  const first = loop.run('one', recorder().callbacks);
  await until(() => started);
  const second = loop.run('two', recorder().callbacks);
  loop.abort(); // Stop, while the second still waits
  slow.open();
  assert.equal(await first, 'aborted');
  assert.equal(await second, 'aborted');
  assert.ok(!shape(loop).includes('user:two'));
});

// ---- callbacks and tools that throw -------------------------------------------------------------------------

test('what a chat shows is not allowed to end a task: a display callback that throws is noted once and the run goes on', async () => {
  stub.reset();
  ToolRegistry.executeTool = async (name) => ({ name, output: 'result of the tool' });
  const client = scriptedClient([{ toolCalls: [toolCall('read_file', { path: 'a.ts' })] }, { content: 'Finished.' }]);
  const notes = [];
  const loop = new AgentLoop(client, { log: (line) => notes.push(line) });
  const outcome = await loop.run('go', {
    onThinkingDelta() {},
    onContentDelta: () => { throw new Error('Response stream has been closed'); },
    onToolStart: () => { throw new Error('Response stream has been closed'); },
    onToolEnd: () => { throw new Error('Response stream has been closed'); },
    onLoadingProgress() {},
  });
  assert.equal(outcome, 'done');
  assert.deepEqual(shape(loop), ['user:go', 'assistant[read_file]', 'tool:result of the tool', 'assistant:Finished.'], 'the tool\'s result was kept');
  for (const name of ['onToolStart', 'onToolEnd']) {
    assert.equal(notes.filter((line) => line.includes(`callback ${name} threw`)).length, 1, `${name} is noted once`);
  }
});

test('a tool that throws is an error result the model can read, not the end of the task (a path that is not text threw out of the approval)', async () => {
  stub.reset();
  const ws = workspace();
  try {
    ToolRegistry.approver = async () => 'deny';
    const client = scriptedClient([{ toolCalls: [toolCall('read_file', { path: 5 })] }, { content: 'I will try something else.' }]);
    const loop = new AgentLoop(client, { log() {} });
    const { callbacks, seen } = recorder();
    assert.equal(await loop.run('read it', callbacks), 'done');
    assert.equal(seen.toolResults.length, 1);
    assert.equal(seen.toolResults[0].is_error, true);
    assert.match(seen.toolResults[0].output, /^Tool execution failed: /);
    const toolMessage = loop.messages.find((m) => m.role === 'tool');
    assert.match(toolMessage.content, /Tool execution failed/);
    assert.equal(callsPaired(loop), true);
  } finally {
    ws.restore();
  }
});

// ---- the retry pause ----------------------------------------------------------------------------------------

test('Stop cuts the pause before a retry short: the run is over at once, not after 2 to 15 seconds', async () => {
  stub.reset();
  const client = scriptedClient([{ throw: new EngineDropped('engine_dropped', false) }]);
  const loop = new AgentLoop(client, { log() {} });
  const { callbacks, seen } = recorder();
  const run = loop.run('go', callbacks);
  await until(() => seen.progress.some((line) => /Sending that step again \(1\/8\)/.test(line)));
  const t0 = Date.now();
  loop.abort();
  assert.equal(await run, 'aborted');
  assert.ok(Date.now() - t0 < 500, `ended in ${Date.now() - t0} ms`);
});

// ---- Stop in the tools --------------------------------------------------------------------------------------

test('a tool call whose run was stopped before it began does nothing and asks nothing', async () => {
  stub.reset();
  const ws = workspace();
  try {
    let asked = 0;
    ToolRegistry.approver = async () => { asked++; return 'once'; };
    const stopped = new AbortController();
    stopped.abort();
    const r = await ToolRegistry.executeTool('write_file', { path: 'x.txt', content: 'x' }, false, { signal: stopped.signal });
    assert.equal(r.output, STOPPED_NOTICE);
    assert.equal(r.is_error, true);
    assert.equal(asked, 0);
    assert.equal(fs.existsSync(path.join(ws.root, 'x.txt')), false);
    // and Stop just before the dialog: no dialog is opened for it
    const late = new AbortController();
    ToolRegistry.approver = async () => { asked++; late.abort(); return 'once'; };
    await ToolRegistry.executeTool('write_file', { path: 'y.txt', content: 'y' }, false, { signal: late.signal });
    assert.equal(fs.existsSync(path.join(ws.root, 'y.txt')), false, 'an answer that came after Stop is ignored');
  } finally {
    ws.restore();
  }
});

test('Stop ends a running command and everything it started; the result says so, and the run does not wait for the process', posix, async () => {
  stub.reset();
  const ws = workspace();
  const marker = `53.${process.pid % 1000}13`;
  const live = () => ownProcesses(marker);
  try {
    const stop = new AbortController();
    const started = Date.now();
    const pending = ToolRegistry.executeTool('run_terminal_command',
      { command: `(sleep ${marker}; echo late) & sleep ${marker}; echo done` }, true, { signal: stop.signal });
    await until(() => live().length >= 3, 4000, 'the shell and its children to run');
    stop.abort();
    const r = await pending;
    assert.ok(Date.now() - started < 3000);
    assert.equal(r.is_error, true);
    assert.match(r.output, /^Stopped: the user stopped the task while this command was running/);
    assert.equal(r.terminalCommand.startsWith('(sleep'), true);
    await until(() => live().length === 0, 5000, 'every process of the command to be gone');
  } finally {
    ws.restore();
    for (const pid of live()) { try { process.kill(Number(pid), 'SIGKILL'); } catch { /* gone */ } }
  }
});

// ---- the shell runner: exec's behaviour, and a Stop that signals only what is the command's -----------------

/**
 * The processes a test started (`sleep <marker>` and its shells), among this user's own, matched literally: never a
 * `pgrep -f` pattern, which once matched VS Code's own helpers (helpers/own-processes.js).
 */
const processesWith = ownProcesses;
const killAll = killOwn;
/** Every process.kill call made while `body` runs, as [pid, signal] (the real signal is still sent). */
async function spyOnKill(body) {
  const real = process.kill;
  const calls = [];
  process.kill = (pid, signal) => { calls.push([pid, signal]); return real.call(process, pid, signal); };
  try {
    await body();
  } finally {
    process.kill = real;
  }
  return calls;
}
const viaExec = (command, options) => new Promise((resolve) =>
  childProcess.exec(command, { maxBuffer: 1024 * 1024, ...options }, (error, stdout, stderr) => resolve({ error, stdout, stderr })));
const viaRunShell = (command, options) => new Promise((resolve) =>
  runShell(command, { maxBuffer: 1024 * 1024, ...options }, (error, stdout, stderr) => resolve({ error, stdout, stderr })));
const seen = (r) => ({
  stdout: r.stdout, stderr: r.stderr, message: r.error?.message, code: r.error?.code, killed: r.error?.killed, signal: r.error?.signal,
  cmd: r.error?.cmd, name: r.error?.name,
});

test('runShell gives what child_process.exec gave: the output, the errors and their fields, the buffer limit, the timeout', posix, async () => {
  const cases = [
    ['echo hi; echo err >&2', {}],
    ['true', {}],
    ['echo out; echo oops >&2; exit 3', {}],
    ['definitely-not-a-command-xyz', {}],
    ['kill -9 $$', {}],
    ['sleep 5', { timeout: 200 }],
    ['yes | head -c 5000', { maxBuffer: 1000 }],
    ['pwd', { cwd: '/' }],
    ['echo $DLNE_RUNSHELL', { env: { ...process.env, DLNE_RUNSHELL: 'seen' } }],
    ['printf "caf\\303\\251 \\342\\202\\254"', {}],
    ['echo x', { cwd: '/definitely/not/a/folder' }],
  ];
  for (const [command, options] of cases) {
    assert.deepEqual(seen(await viaRunShell(command, options)), seen(await viaExec(command, options)), command);
  }
});

test('Stop ends the jobs a command left in the background after its shell has exited (npm run dev &), by their process group', posix, async () => {
  stub.reset();
  const ws = workspace();
  const marker = `58.${process.pid % 1000}41`;
  try {
    const stop = new AbortController();
    const started = Date.now();
    const calls = await spyOnKill(async () => {
      const pending = ToolRegistry.executeTool('run_terminal_command', { command: `sleep ${marker} & echo started` }, true, { signal: stop.signal });
      await sleep(500); // the shell has printed and gone; `sleep` still holds the output open, so the tool is still waiting
      assert.equal(processesWith(marker).length, 1, 'the job is running and its shell is not');
      stop.abort();
      const r = await pending;
      assert.match(r.output, /^Stopped: the user stopped the task/);
      assert.ok(Date.now() - started < 3000, 'the tool did not wait for the job');
      await until(() => processesWith(marker).length === 0, 4000, 'the background job to be gone');
      await sleep(1700); // past the grace in which what ignored SIGTERM would get SIGKILL
    });
    const signals = calls.filter(([, signal]) => signal !== 0);
    assert.ok(signals.length >= 1 && signals.every(([pid]) => pid < 0), `only a process group was signalled: ${JSON.stringify(signals)}`);
    assert.deepEqual(signals.map(([, signal]) => signal), ['SIGTERM'], 'nothing was left to need SIGKILL');
  } finally {
    ws.restore();
    killAll(marker);
  }
});

test('Stop sends no signal when the command has nobody left in its group, even though a process outside it still holds the output', posix, async (t) => {
  if (childProcess.spawnSync('sh', ['-c', 'command -v setsid']).status !== 0) return t.skip('no setsid');
  stub.reset();
  const ws = workspace();
  const marker = `59.${process.pid % 1000}77`;
  try {
    const stop = new AbortController();
    let escapee = [];
    const calls = await spyOnKill(async () => {
      // `setsid` moves the job to a session of its own: the command's group is empty once the shell has gone, and its
      // (reusable) number must not be signalled. The job still holds the output pipe, so the tool is still waiting.
      const pending = ToolRegistry.executeTool('run_terminal_command', { command: `setsid sleep ${marker} & echo started` }, true, { signal: stop.signal });
      await sleep(600);
      escapee = processesWith(marker);
      assert.equal(escapee.length, 1);
      stop.abort();
      const r = await pending;
      assert.match(r.output, /^Stopped: the user stopped the task/);
      await sleep(1800);
    });
    assert.deepEqual(calls.filter(([, signal]) => signal !== 0), [], 'no signal went anywhere');
    assert.equal(processesWith(marker).length, 1, 'what left the group on purpose is not the group\'s to end');
  } finally {
    ws.restore();
    killAll(marker);
  }
});

test('what ignores SIGTERM gets SIGKILL after the grace, to the same group and only while it has a member', posix, async () => {
  const marker = `52.${process.pid % 1000}29`;
  const stubborn = runShell(`trap '' TERM; (trap '' TERM; sleep ${marker}) & sleep ${marker}; wait`, { timeout: 0 }, () => {});
  try {
    await until(() => processesWith(marker).length >= 2, 4000, 'the stubborn processes to start');
    const calls = await spyOnKill(async () => {
      endCommand(stubborn.child, 300);
      await until(() => processesWith(marker).length === 0, 5000, 'SIGKILL to end what ignored SIGTERM');
    });
    assert.deepEqual(calls.filter(([, signal]) => signal !== 0).map(([, signal]) => signal), ['SIGTERM', 'SIGKILL']);
    assert.ok(calls.every(([pid]) => pid === -stubborn.child.pid), 'always the one group');
    // a group that is gone is not signalled at all, however often it is asked
    const after = await spyOnKill(async () => { endCommand(stubborn.child, 10); endCommand({ pid: undefined, exitCode: null, signalCode: null }); });
    assert.deepEqual(after.filter(([, signal]) => signal !== 0), []);
  } finally {
    killAll(marker);
  }
});

test('Windows: taskkill /T is asked only for a shell that is still running (its number cannot have been reused); an exited one has no tree to follow', async () => {
  const realPlatform = Object.getOwnPropertyDescriptor(process, 'platform');
  const realExecFile = childProcess.execFile;
  const asked = [];
  // These pids are made up: nothing is ever really signalled here, whatever endCommand decides (the signal is recorded, not sent).
  const realKill = process.kill;
  const signalled = [];
  process.kill = (pid, signal) => { signalled.push([pid, signal]); return true; };
  childProcess.execFile = (file, args, options, callback) => { asked.push([file, ...args]); callback?.(null, '', ''); return {}; };
  Object.defineProperty(process, 'platform', { value: 'win32' });
  try {
    endCommand({ pid: 4242, exitCode: null, signalCode: null });
    assert.deepEqual(asked, [['taskkill', '/pid', '4242', '/T', '/F']]);
    asked.length = 0;
    endCommand({ pid: 4242, exitCode: 0, signalCode: null });          // the shell has exited: `start /b server` left it behind
    endCommand({ pid: 4242, exitCode: null, signalCode: 'SIGTERM' });
    endCommand({ pid: undefined, exitCode: null, signalCode: null });
    endCommand({ pid: 0, exitCode: null, signalCode: null });
    assert.deepEqual(asked, []);
    assert.deepEqual(signalled, [], 'the Windows branch sends no POSIX signal');
  } finally {
    process.kill = realKill;
    Object.defineProperty(process, 'platform', realPlatform);
    childProcess.execFile = realExecFile;
  }
});

test('a command is never typed into a VS Code terminal: a heredoc runs whole, in the tool\'s own shell', posix, async () => {
  // 2026-10-28: an echo into "DLNE Agent Terminal" commented out only a command's first line, so the user's bash ran
  // the rest of a Python heredoc (`import struct` started ImageMagick's screen grab).
  const opened = [];
  stub.vscode.window.createTerminal = (options) => {
    opened.push(options);
    return { exitStatus: undefined, show() {}, sendText(text) { opened.push(text); }, dispose() {} };
  };
  try {
    const r = await ToolRegistry.executeTool('run_terminal_command', { command: "cat <<'EOF'\nimport struct\nfor x in y:\nEOF" }, true);
    assert.equal(r.output, 'import struct\nfor x in y:');
    assert.deepEqual(opened, []);
  } finally {
    delete stub.vscode.window.createTerminal;
  }
});
