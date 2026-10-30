'use strict';
/**
 * VS Code first (2.7.0, docs/release-2.7-contract.md section 4b), the worker manager's half: a DLNE Donate tray from 1.1.0
 * pauses its worker for an open editor, so Start here waits for the tray's worker to go and then starts the editor's own;
 * a tray from before 1.1.0 knows nothing of the editor and is stepped back from, as it always was. While the tray is
 * paused and nothing runs here, the panel and the status line say so. Runs on any machine: the worker is a fake and the
 * tray is whatever the test says.
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
const t = require('../dist/worker/trayApp');

process.env.HOME = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-handover-home-'));
process.env.USERPROFILE = process.env.HOME;
WorkerManager.prototype.init = async function init() {};

// ---- what the tray's state.json says --------------------------------------------------------------------------------

test('versions are compared as dotted numbers', () => {
  assert.equal(t.versionAtLeast('1.1.0', '1.1.0'), true);
  assert.equal(t.versionAtLeast('1.1', '1.1.0'), true);
  assert.equal(t.versionAtLeast('1.10.0', '1.1.0'), true);
  assert.equal(t.versionAtLeast('2.0.0', '1.1.0'), true);
  assert.equal(t.versionAtLeast('1.0.2', '1.1.0'), false);
  assert.equal(t.versionAtLeast('0.9.9', '1.1.0'), false);
  assert.equal(t.versionAtLeast('1.1.0-beta', '1.1.0'), false);
  assert.equal(t.versionAtLeast(undefined, '1.1.0'), false);
  assert.equal(t.versionAtLeast(1.1, '1.1.0'), false);
});

test('a tray can pause for the editor when its state names 1.1.0 or later, or it is paused or leaving anyway', () => {
  assert.deepEqual(t.parseTrayHandover('{"state": "donating", "pid": 5, "version": "1.1.0"}'), { canPause: true, paused: false });
  assert.deepEqual(t.parseTrayHandover('{"state": "donating", "pid": 5, "version": "1.0.2"}'), { canPause: false, paused: false });
  assert.deepEqual(t.parseTrayHandover('{"state": "donating", "pid": 5}'), { canPause: false, paused: false }, 'a 1.0.x state file has no version');
  assert.deepEqual(t.parseTrayHandover('{"state": "paused", "pid": 5}'), { canPause: true, paused: true });
  assert.deepEqual(t.parseTrayHandover('{"state": "idle", "paused": true}'), { canPause: true, paused: true });
  assert.deepEqual(t.parseTrayHandover('{"state": "stopping", "pid": 5}'), { canPause: true, paused: false }, 'a tray that is leaving is waited for');
  assert.deepEqual(t.parseTrayHandover(null), { canPause: false, paused: false });
  assert.deepEqual(t.parseTrayHandover('not json'), { canPause: false, paused: false });
  assert.deepEqual(t.parseTrayHandover('[1]'), { canPause: false, paused: false });
});

test('trayHandover reads the tray\'s state.json where trayWorkerRunning does', () => {
  const files = { '/home/ann/.local/share/dlne-donate/state.json': '{"state": "donating", "version": "1.1.0"}' };
  const probe = { readText: (file) => files[file] ?? null, run: () => null };
  assert.equal(t.trayHandover('linux', probe, {}, '/home/ann').canPause, true);
  assert.equal(t.trayHandover('linux', probe, {}, '/home/bob').canPause, false);
  assert.equal(t.trayHandover('darwin', probe, {}, '/home/ann').canPause, false, 'no tray app on macOS');
});

// ---- the worker manager ---------------------------------------------------------------------------------------------

function installEngine() {
  const root = path.join(process.env.HOME, '.dlne', 'runtime');
  fs.mkdirSync(path.join(root, 'config'), { recursive: true });
  fs.writeFileSync(path.join(root, 'dlne_server.py'), 'p_worker.add_argument("--coding-max-ram", type=float)\n');
}

/**
 * A manager on a computer where a tray is (or is not) donating. `world` is what the test changes as the time goes:
 * tray (its worker runs), handover (what its state.json says).
 */
function rig({ tray = true, canPause = true, paused = false } = {}) {
  stub.reset();
  Object.assign(stub.state.settings, {
    'dlne.worker.device': 'gpu', 'dlne.worker.pythonPath': process.execPath, 'dlne.autoUpdate': false, 'dlne.serverUrl': 'http://127.0.0.1:9',
  });
  installEngine();
  const world = { tray, handover: { canPause, paused } };
  const proto = WorkerManager.prototype;
  const real = { sysd: proto.isSystemdActive, proc: proto.isProcessActive, tray: proto.isTrayWorkerActive, handover: proto.readTrayHandover };
  proto.isSystemdActive = () => false;
  proto.isProcessActive = () => false;
  proto.isTrayWorkerActive = () => world.tray;
  proto.readTrayHandover = () => world.handover;
  let wm;
  try {
    wm = new WorkerManager({ subscriptions: [], globalState: undefined });
  } finally {
    Object.assign(proto, { isSystemdActive: real.sysd, isProcessActive: real.proc, isTrayWorkerActive: real.tray, readTrayHandover: real.handover });
  }
  Object.assign(wm, {
    isSystemdActive: () => false, isProcessActive: () => false, isTrayWorkerActive: () => world.tray,
    readTrayHandover: () => world.handover, isPortListening: async () => false, ensureRpcBinaries: async () => {},
    localGpu: { hasGpu: true, name: 'RTX 4070 Laptop GPU', vramTotalGb: 8, api: 'vulkan' },
    trayWaitMs: 400, trayPollMs: 10,
  });
  const workers = [];
  const realSpawn = childProcess.spawn;
  childProcess.spawn = () => {
    const worker = Object.assign(new EventEmitter(), {
      stdout: new EventEmitter(), stderr: new EventEmitter(), pid: 50000 + workers.length, exitCode: null, signalCode: null, killed: false,
      kill() { this.killed = true; setImmediate(() => { this.exitCode = 0; this.emit('exit', 0, 'SIGTERM'); }); return true; },
    });
    workers.push(worker);
    return worker;
  };
  const done = () => { childProcess.spawn = realSpawn; wm.dispose(); };
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  return { wm, world, workers, done, sleep };
}

test('with a tray worker that pauses, Start waits for it to go, then starts the editor\'s own worker', async () => {
  const r = rig();
  try {
    // Found at start-up: shown as the tray's, until it pauses.
    assert.equal(r.wm.getStatus().trayApp, true);
    const starting = r.wm.startWorker();
    await r.sleep(60);
    assert.equal(r.workers.length, 0, 'nothing is started while the tray\'s worker still runs');
    let st = r.wm.getStatus();
    assert.deepEqual([st.state, st.external, st.message], ['starting', undefined, 'Waiting for DLNE Donate to pause...']);
    // The wait is not mistaken for an outside worker by the poll.
    st = await r.wm.refreshWorkerState();
    assert.equal(st.external, undefined);
    assert.equal(st.state, 'starting');

    r.world.tray = false;   // the tray's worker has left
    assert.equal(await starting, true);
    assert.equal(r.workers.length, 1, 'the editor\'s own worker runs');
    assert.equal(stub.state.infoMessages.includes(`DLNE: ${c.TRAY_WORKER_TEXT}`), false, 'no "stop it in the tray" notice');
    assert.equal(r.wm.getStatus().external, undefined);
  } finally {
    r.done();
  }
});

test('the Start button with a pausing tray running waits for it and starts here; with an older tray it only says where to stop it', async () => {
  const r = rig();
  r.wm.confirmLendingGpu = async () => true;   // the owner's one-time "lend my GPU?" answer is not what is tested here
  try {
    const clicked = r.wm.toggle();
    await r.sleep(60);
    assert.equal(r.workers.length, 0);
    r.world.tray = false;
    assert.equal(await clicked, true);
    assert.equal(r.workers.length, 1);
  } finally {
    r.done();
  }
  const old = rig({ canPause: false });
  try {
    assert.equal(await old.wm.toggle(), true);
    assert.equal(old.workers.length, 0);
    assert.ok(stub.state.infoMessages.includes(`DLNE: ${c.TRAY_WORKER_TEXT}`));
  } finally {
    old.done();
  }
});

test('a tray worker that never goes is stepped back from after the wait, as before', async () => {
  const r = rig();
  r.wm.trayWaitMs = 80;
  try {
    assert.equal(await r.wm.startWorker(), true);
    assert.equal(r.workers.length, 0);
    const st = r.wm.getStatus();
    assert.deepEqual([st.state, st.external, st.trayApp], ['contributing', true, true]);
    assert.ok(stub.state.infoMessages.includes(`DLNE: ${c.TRAY_WORKER_TEXT}`));
  } finally {
    r.done();
  }
});

test('a tray from before 1.1.0 is stepped back from at once: no wait', async () => {
  const r = rig({ canPause: false });
  r.wm.trayWaitMs = 60_000;
  try {
    const began = Date.now();
    assert.equal(await r.wm.startWorker(), true);
    assert.ok(Date.now() - began < 1000, 'it did not wait');
    assert.equal(r.workers.length, 0);
    assert.equal(r.wm.getStatus().trayApp, true);
  } finally {
    r.done();
  }
});

test('Stop while the editor waits for the tray cancels the start', async () => {
  const r = rig();
  r.wm.trayWaitMs = 60_000;
  try {
    const starting = r.wm.startWorker();
    await r.sleep(40);
    assert.equal(r.wm.getStatus().state, 'starting');
    await r.wm.stopWorker(true);
    assert.equal(await starting, false);
    assert.equal(r.workers.length, 0);
    assert.equal(r.wm.getStatus().state, 'idle');
  } finally {
    r.done();
  }
});

test('with no tray worker running, nothing waits', async () => {
  const r = rig({ tray: false });
  try {
    assert.equal(await r.wm.startWorker(), true);
    assert.equal(r.workers.length, 1);
  } finally {
    r.done();
  }
});

test('once the tray has paused and nothing runs here, the status says so; starting here clears it', async () => {
  const r = rig();
  try {
    // The tray was donating when a poll saw it (a tray that pauses) ...
    await r.wm.refreshWorkerState();
    // ... and then it left, because it paused for this editor.
    r.world.tray = false;
    for (let i = 0; i < 3; i++) await r.wm.refreshWorkerState();
    let st = r.wm.getStatus();
    assert.equal(st.state, 'idle');
    assert.equal(st.trayPaused, true);
    const copy = c.contributionCopy(st);
    assert.ok(copy.ruleHtml.includes('DLNE Donate is paused while VS Code is open; Start here to donate'), copy.ruleHtml);
    assert.ok(c.contributionTooltip(st).includes(c.TRAY_PAUSED_TEXT));
    assert.equal(c.TRAY_PAUSED_TEXT, 'DLNE Donate is paused while VS Code is open; Start here to donate');

    assert.equal(await r.wm.startWorker(), true);
    st = r.wm.getStatus();
    assert.equal(st.trayPaused, undefined, 'a worker runs here now');
    assert.ok(!c.contributionCopy(st).ruleHtml.includes('paused while VS Code'));
  } finally {
    r.done();
  }
});

test('a tray that says it is paused is shown as paused even if this editor never saw it donate', async () => {
  const r = rig({ tray: false, paused: true });
  try {
    for (let i = 0; i < 3; i++) await r.wm.refreshWorkerState();
    assert.equal(r.wm.getStatus().trayPaused, true);
    r.world.handover = { canPause: true, paused: false };
    await r.wm.refreshWorkerState();
    assert.equal(r.wm.getStatus().trayPaused, undefined, 'and not once it says otherwise');
  } finally {
    r.done();
  }
});

test('a tray worker that cannot pause (before 1.1.0) never makes the status "paused"', async () => {
  const r = rig({ canPause: false });
  try {
    await r.wm.refreshWorkerState();
    r.world.tray = false;
    for (let i = 0; i < 3; i++) await r.wm.refreshWorkerState();
    assert.equal(r.wm.getStatus().trayPaused, undefined);
  } finally {
    r.done();
  }
});

test('the idle card of a computer with the tray paused keeps its usual lines under the notice', () => {
  const idle = (cpuPool) => ({
    state: 'idle', cpuPool, vramContributedGb: 0, trayPaused: true, gpu: { name: 'RTX 4070', api: 'vulkan', vramTotalGb: 8 },
  });
  assert.ok(c.contributionCopy(idle(false)).ruleHtml.includes('Core Rule'));
  assert.ok(c.contributionCopy(idle(true)).ruleHtml.includes('You get'));
  assert.ok(c.contributionCopy(idle(true)).ruleHtml.includes(c.TRAY_PAUSED_TEXT));
  assert.ok(!c.contributionCopy({ ...idle(false), trayPaused: undefined }).ruleHtml.includes('paused'));
  assert.ok(!c.contributionCopy({ ...idle(false), state: 'contributing' }).ruleHtml.includes('paused'), 'only an idle card says it');
});
