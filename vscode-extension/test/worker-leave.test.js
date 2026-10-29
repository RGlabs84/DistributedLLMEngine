'use strict';
/**
 * Stop Contributing and a worker that carries a shard of the model (2.6.1): SIGTERM starts the worker's graceful
 * leave (engine/worker.py, LEAVE_GRACE_SECONDS = 90), so the extension waits for the worker to exit instead of
 * killing it after 8 s, and removes an orphaned ggml-rpc-server only once the worker is gone. A worker with no
 * shard keeps the quick stop. Runs on any machine: the worker is a fake and timers and child_process are replaced.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const childProcess = require('node:child_process');
const { EventEmitter } = require('node:events');

const stub = require('./helpers/vscode-stub');
const { WorkerManager } = require('../dist/worker/workerManager');
const c = require('../dist/worker/contribution');

process.env.HOME = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-worker-leave-home-'));
WorkerManager.prototype.init = async function init() {};

/** Run `fn` while process.platform says `platform`. */
async function onPlatform(platform, fn) {
  const real = Object.getOwnPropertyDescriptor(process, 'platform');
  Object.defineProperty(process, 'platform', { value: platform });
  try {
    return await fn();
  } finally {
    Object.defineProperty(process, 'platform', real);
  }
}

/**
 * A manager whose worker is a fake that logs its signals, with timers, systemd and the orphan clean-up
 * replaced. `events` is the order of everything that happened: ['SIGTERM'], ['exit'], ['orphan stop'], ...
 */
function rig(role) {
  stub.reset();
  const wm = new WorkerManager({ subscriptions: [], globalState: undefined });
  wm.currentState = 'contributing';
  wm.externalWorker = false;
  wm.isSystemdActive = () => false;
  wm.findEngineScript = () => path.join(os.tmpdir(), 'dlne engine', 'dlne_server.py');
  const events = [];
  const worker = Object.assign(new EventEmitter(), { exitCode: null, signalCode: null, kill: (signal) => events.push(signal) });
  wm.workerProcess = worker;
  wm.assignedRole = role ? { role } : null;
  const timers = [];
  const real = { setTimeout: global.setTimeout, clearTimeout: global.clearTimeout, execFileSync: childProcess.execFileSync, execSync: childProcess.execSync };
  global.setTimeout = (fn, ms) => { const timer = { fn, ms, cleared: false, unref() {} }; timers.push(timer); return timer; };
  global.clearTimeout = (timer) => { if (timer) timer.cleared = true; };
  childProcess.execSync = () => '';
  childProcess.execFileSync = () => { events.push('orphan stop'); return ''; };
  const exit = () => { events.push('exit'); worker.emit('exit', 0, null); };
  const done = () => {
    global.setTimeout = real.setTimeout;
    global.clearTimeout = real.clearTimeout;
    childProcess.execFileSync = real.execFileSync;
    childProcess.execSync = real.execSync;
    wm.dispose();
  };
  const settle = () => new Promise((resolve) => setImmediate(resolve));
  return { wm, worker, events, timers, exit, done, settle };
}

const LEAVE_MS = c.WORKER_LEAVE_GRACE_MS + c.WORKER_LEAVE_MARGIN_MS;

test('the stop wait is the worker\'s own leave time plus a margin for a shard, a few seconds for the rest, 800 ms on Windows', () => {
  assert.ok(c.WORKER_LEAVE_GRACE_MS >= 90_000, 'engine/worker.py LEAVE_GRACE_SECONDS is 90');
  const workerPy = path.join(__dirname, '..', '..', 'engine', 'worker.py');   // absent from a packaged copy of the tests
  if (fs.existsSync(workerPy)) {
    assert.match(fs.readFileSync(workerPy, 'utf8'), /^LEAVE_GRACE_SECONDS = 90\.0$/m, 'the extension\'s wait follows the worker\'s');
  }
  assert.ok(LEAVE_MS > c.WORKER_LEAVE_GRACE_MS);
  assert.equal(c.workerStopGraceMs(true, 'linux'), LEAVE_MS);
  assert.equal(c.workerStopGraceMs(true, 'darwin'), LEAVE_MS);
  assert.equal(c.workerStopGraceMs(false, 'linux'), 8000);
  assert.equal(c.workerStopGraceMs(true, 'win32'), 800, 'Windows has no graceful SIGTERM: its stop stays quick');
  assert.equal(c.workerStopGraceMs(false, 'win32'), 800);
});

test('a worker that carries a shard is given its leave: no SIGKILL for 105 s, no orphan stop while it lives', async () => {
  const t = rig('rpc');
  try {
    const stopping = t.wm.stopWorker();
    await t.settle();
    assert.deepEqual(t.events, ['SIGTERM']);
    const kill = t.timers.find((x) => x.ms === LEAVE_MS);
    assert.ok(kill, 'the kill waits for the leave');
    assert.ok(!t.timers.some((x) => x.ms === 8000), 'not the quick stop');
    // It is shown as leaving, not stuck, and its heartbeats do not turn that back into "contributing".
    assert.equal(t.wm.getStatus().message, c.LEAVING_TEXT);
    t.wm.markContributing();
    assert.equal(t.wm.getStatus().message, c.LEAVING_TEXT);
    assert.ok(t.timers.some((x) => x.ms === 2000), 'a notice follows if it takes more than a moment');
    // A poll during the leave does not take the leaving worker for one started outside VS Code.
    assert.equal((await t.wm.refreshWorkerState()).external, undefined);

    t.exit();
    await stopping;
    assert.deepEqual(t.events, ['SIGTERM', 'exit', 'orphan stop'], 'the orphan stop comes after the exit, once');
    assert.equal(t.wm.getStatus().state, 'idle');
    assert.ok(kill.cleared, 'the kill timer is gone with the worker');
  } finally {
    t.done();
  }
});

test('a worker with no shard keeps the quick stop and is killed after 8 s if it has not exited', async () => {
  for (const role of ['coding', 'app', null]) {
    const t = rig(role);
    try {
      const stopping = t.wm.stopWorker();
      await t.settle();
      assert.deepEqual(t.timers.map((x) => x.ms), [8000], `${role}: one timer, the quick one, and no leaving notice`);
      assert.notEqual(t.wm.getStatus().message, c.LEAVING_TEXT);
      t.timers[0].fn();
      assert.deepEqual(t.events, ['SIGTERM', 'SIGKILL']);
      assert.ok(!t.events.includes('orphan stop'));
      t.exit();
      await stopping;
      assert.deepEqual(t.events, ['SIGTERM', 'SIGKILL', 'exit', 'orphan stop']);
    } finally {
      t.done();
    }
  }
});

test('a shard-carrying worker that outlasts its leave is killed, and its engine is cleaned up after it exits', async () => {
  const t = rig('rpc');
  try {
    const stopping = t.wm.stopWorker();
    await t.settle();
    t.timers.find((x) => x.ms === LEAVE_MS).fn();
    assert.deepEqual(t.events, ['SIGTERM', 'SIGKILL']);
    t.exit();
    await stopping;
    assert.deepEqual(t.events, ['SIGTERM', 'SIGKILL', 'exit', 'orphan stop']);
  } finally {
    t.done();
  }
});

test('a worker that does not exit even after the kill is given up on, and its engine is left alone', async () => {
  const t = rig('rpc');
  try {
    const stopping = t.wm.stopWorker();
    await t.settle();
    t.timers.find((x) => x.ms === LEAVE_MS).fn();
    await t.settle();
    t.timers.find((x) => x.ms === 3000).fn();
    await stopping;
    assert.deepEqual(t.events, ['SIGTERM', 'SIGKILL'], 'no orphan stop for a process that may still be alive');
    assert.equal(t.wm.workerProcess, null);
  } finally {
    t.done();
  }
});

test('a second Stop while the worker is leaving ends it at once', async () => {
  const t = rig('rpc');
  try {
    const first = t.wm.stopWorker();
    await t.settle();
    const second = t.wm.stopWorker();
    assert.deepEqual(t.events, ['SIGTERM', 'SIGKILL'], 'the second Stop kills it, with no second SIGTERM');
    t.exit();
    await Promise.all([first, second]);
    assert.deepEqual(t.events, ['SIGTERM', 'SIGKILL', 'exit', 'orphan stop']);
    // And a Stop after that starts a new stop, not a force of the finished one.
    t.wm.workerProcess = Object.assign(new EventEmitter(), { exitCode: null, signalCode: null, kill: (signal) => t.events.push(`again ${signal}`) });
    const third = t.wm.stopWorker();
    await t.settle();
    assert.equal(t.events.at(-1), 'again SIGTERM');
    t.wm.workerProcess.emit('exit', 0, null);
    await third;
  } finally {
    t.done();
  }
});

test('on Windows the stop stays quick even for a shard: 800 ms, then the orphan clean-up after the exit', async () => {
  await onPlatform('win32', async () => {
    const t = rig('rpc');
    try {
      const stopping = t.wm.stopWorker();
      await t.settle();
      assert.deepEqual(t.timers.map((x) => x.ms), [800]);
      assert.deepEqual(t.events, ['SIGTERM']);
      t.exit();
      await stopping;
      assert.deepEqual(t.events, ['SIGTERM', 'exit', 'orphan stop']);
    } finally {
      t.done();
    }
  });
});

test('a worker that is already gone does not wait, and the orphan clean-up still runs', async () => {
  const t = rig('rpc');
  try {
    t.worker.exitCode = 0;
    await t.wm.stopWorker();
    assert.deepEqual(t.events, ['SIGTERM', 'orphan stop']);
  } finally {
    t.done();
  }
});

test('the worker from before a window reload (its job unknown) is waited for too, and a second Stop forces it', { skip: process.platform === 'win32' }, async () => {
  // A worker that ignores SIGTERM, as a leaving one does for up to 90 s.
  const fake = childProcess.spawn(process.execPath, ['-e', "process.on('SIGTERM', () => {}); setTimeout(() => {}, 60000)", 'dlne_server.py', 'worker']);
  const exited = new Promise((resolve) => fake.on('exit', (code, signal) => resolve(signal)));
  await new Promise((resolve) => setTimeout(resolve, 300));   // its handler is installed
  stub.reset();
  const wm = new WorkerManager({ subscriptions: [], globalState: undefined });
  wm.isSystemdActive = () => false;
  wm.findEngineScript = () => path.join(os.tmpdir(), 'dlne engine', 'dlne_server.py');
  wm.adoptedPid = fake.pid;
  const realExecFile = childProcess.execFileSync;
  const realExec = childProcess.execSync;
  const orphanStops = [];
  childProcess.execSync = () => '';
  childProcess.execFileSync = () => { orphanStops.push(Date.now()); return ''; };
  try {
    const first = wm.stopWorker(true);
    await new Promise((resolve) => setTimeout(resolve, 800));
    assert.equal(fake.exitCode, null, 'still leaving: not killed after the quick stop\'s 8 s either');
    assert.deepEqual(orphanStops, [], 'its engine is not an orphan while it lives');
    assert.equal(wm.getStatus().message, c.LEAVING_TEXT);
    const second = wm.stopWorker(true);
    assert.equal(await exited, 'SIGKILL');
    await Promise.all([first, second]);
    assert.equal(orphanStops.length, 1, 'cleaned up once it is gone');
  } finally {
    try { fake.kill('SIGKILL'); } catch {}
    childProcess.execFileSync = realExecFile;
    childProcess.execSync = realExec;
    wm.dispose();
  }
});

test('the editor keeps its worker across a window reload: dispose does not stop it', () => {
  const t = rig('rpc');
  try {
    t.wm.dispose();
    assert.deepEqual(t.events, []);
    assert.equal(t.wm.workerProcess, t.worker);
  } finally {
    t.done();
  }
});
