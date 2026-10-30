'use strict';
/**
 * Engine flavours in the extension (2.7.0, docs/release-2.7-contract.md sections 1 and 7): the worker's DLNE_CUDA and
 * DLNE_ENGINE lines, the one question about CUDA (Use CUDA / Not now, asked again after 3 days / Keep Vulkan, never
 * again), which flavour the worker is started with, and the restart when that changes. Runs on any machine: the worker
 * is a fake, the answers are the test's.
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
const f = require('../dist/worker/engineFlavour');

process.env.HOME = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-flavour-home-'));
process.env.USERPROFILE = process.env.HOME;
WorkerManager.prototype.init = async function init() {};

const DAY = 24 * 60 * 60 * 1000;
const CAPABLE = 'DLNE_CUDA: {"capable": true, "reason": "", "gpu": "NVIDIA GeForce RTX 4070 Laptop GPU", "compute_cap": "8.9", "driver": "615.78"}';
const OLD_GPU = 'DLNE_CUDA: {"capable": false, "reason": "this GPU is older than Turing (compute capability 6.1)", "gpu": "NVIDIA GeForce GTX 1060", "compute_cap": "6.1", "driver": "576.02"}';

// ---- the marker lines ---------------------------------------------------------------------------------------------

test('DLNE_CUDA: capable and not, the last line wins, malformed lines are ignored', () => {
  assert.deepEqual(c.parseCudaProbe(`noise\n${CAPABLE}\n`), {
    capable: true, reason: '', gpu: 'NVIDIA GeForce RTX 4070 Laptop GPU', computeCap: '8.9', driver: '615.78',
  });
  const old = c.parseCudaProbe(OLD_GPU);
  assert.equal(old.capable, false);
  assert.equal(old.reason, 'this GPU is older than Turing (compute capability 6.1)');
  assert.equal(c.parseCudaProbe(`${OLD_GPU}\r\n${CAPABLE}\r\n`).capable, true, 'a later line replaces an earlier one, CRLF or not');
  assert.equal(c.parseCudaProbe('DLNE_CUDA: {"capable": "yes"}'), null, 'capable must be a boolean');
  assert.equal(c.parseCudaProbe('DLNE_CUDA: not json'), null);
  assert.equal(c.parseCudaProbe('DLNE_CUDA: [1]'), null);
  assert.equal(c.parseCudaProbe('hello'), null);
  assert.equal(c.parseCudaProbe(`DLNE_CUDA: ${JSON.stringify({ capable: true, reason: 'x', gpu: 7 })}`).reason, '', 'a capable GPU has no reason');
  assert.equal(c.parseCudaProbe('DLNE_CUDA: {"capable": false, "reason": "no NVIDIA GPU"}').gpu, '');
});

test('DLNE_ENGINE: the flavour that runs, and why CUDA fell back', () => {
  assert.deepEqual(c.parseEngineReport('DLNE_ENGINE: {"flavour": "cuda", "fallback": ""}'), { flavour: 'cuda', fallback: '' });
  assert.deepEqual(
    c.parseEngineReport('x\nDLNE_ENGINE: {"flavour": "vulkan", "fallback": "the driver is out of date"}'),
    { flavour: 'vulkan', fallback: 'the driver is out of date' });
  assert.deepEqual(c.parseEngineReport('DLNE_ENGINE: {"flavour": "vulkan"}'), { flavour: 'vulkan', fallback: '' });
  assert.equal(c.parseEngineReport('DLNE_ENGINE: {"flavour": "cuda", "fallback": "odd"}').fallback, '', 'CUDA running has nothing to excuse');
  assert.equal(c.parseEngineReport('DLNE_ENGINE: {"flavour": "rocm", "fallback": ""}'), null);
  assert.equal(c.parseEngineReport('DLNE_ENGINE: nope'), null);
});

// ---- which flavour ------------------------------------------------------------------------------------------------

test('resolving the flavour: CUDA needs a capable GPU and a "yes", and never after a failed start', () => {
  const r = f.resolveFlavour;
  assert.equal(r('auto', 'yes', true, false), 'cuda');
  assert.equal(r('cuda', 'yes', true, false), 'cuda');
  for (const consent of ['unasked', 'later', 'never']) {
    assert.equal(r('auto', consent, true, false), 'vulkan', `auto without a yes (${consent}) is Vulkan: nothing CUDA yet`);
    assert.equal(r('cuda', consent, true, false), 'vulkan', `cuda without a yes (${consent}) waits for the question`);
  }
  assert.equal(r('auto', 'yes', false, false), 'vulkan', 'a GPU that cannot');
  assert.equal(r('cuda', 'yes', false, false), 'vulkan', 'cuda on a GPU that cannot falls to Vulkan');
  assert.equal(r('auto', 'yes', null, false), 'vulkan', 'until the worker has said what the GPU is');
  assert.equal(r('vulkan', 'yes', true, false), 'vulkan');
  assert.equal(r('auto', 'yes', true, true), 'vulkan', 'CUDA failed its start-up check');
});

test('the stored answer: yes, never, and a "later" that is over counts as not asked', () => {
  const now = 1_000_000;
  assert.equal(f.consentOf('yes', now), 'yes');
  assert.equal(f.consentOf('never', now), 'never');
  assert.equal(f.consentOf({ later: now + 1 }, now), 'later');
  assert.equal(f.consentOf({ later: now }, now), 'unasked');
  assert.equal(f.consentOf(undefined, now), 'unasked');
  assert.equal(f.consentOf('maybe', now), 'unasked');
  assert.equal(f.consentOf({ later: 'soon' }, now), 'unasked');
});

test('the version label puts the flavour after a plus, and only for display', () => {
  assert.equal(f.engineLabel('2.7.0', 'cuda'), '2.7.0+cuda');
  assert.equal(f.engineLabel('2.7.0', 'vulkan'), '2.7.0+vulkan');
  assert.equal(f.engineLabel('2.7.0', null), '2.7.0');
  assert.equal(f.fallbackText('the driver is out of date'), 'CUDA could not start: the driver is out of date; running on Vulkan');
});

// ---- the question -------------------------------------------------------------------------------------------------

/** A global state in memory. */
function memento(initial = {}) {
  const data = new Map(Object.entries(initial));
  return { data, get: (key, fallback) => (data.has(key) ? data.get(key) : fallback), update: async (key, value) => { if (value === undefined) data.delete(key); else data.set(key, value); } };
}

/** Every question put to the owner, and the click the test gives it. */
function asking(choice) {
  const real = stub.vscode.window.showInformationMessage;
  const asked = [];
  stub.vscode.window.showInformationMessage = async (message, ...items) => {
    asked.push({ message, items });
    return typeof choice === 'function' ? choice(asked.length) : choice;
  };
  return { asked, restore: () => { stub.vscode.window.showInformationMessage = real; } };
}

/** A controller at a clock the test moves. */
function controller(store, clock = { now: 5_000_000_000 }) {
  stub.reset();
  const ctl = new f.EngineFlavourController(store, () => clock.now);
  return { ctl, clock };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

test('a capable GPU that has not chosen is asked once, with the contract\'s words and three answers', async () => {
  const q = asking(undefined);
  try {
    const { ctl } = controller(memento());
    ctl.noteOutput(CAPABLE);
    await settle();
    assert.equal(q.asked.length, 1);
    assert.equal(q.asked[0].message,
      'Your NVIDIA GPU can run DLNE about 30-50% faster with the CUDA engine. It is a one-time download of about 600 MB. Use CUDA?');
    assert.deepEqual(q.asked[0].items, ['Use CUDA', 'Not now', 'Keep Vulkan']);
    // The worker prints the line again (a restart, a change): one question per session, and a closed one stores nothing.
    ctl.noteOutput(CAPABLE);
    ctl.noteOutput(OLD_GPU);
    ctl.noteOutput(CAPABLE);
    await settle();
    assert.equal(q.asked.length, 1);
    assert.equal(ctl.getState().consent, 'unasked');
    assert.equal(ctl.resolve(), 'vulkan', 'nothing CUDA without a yes');
    ctl.dispose();
  } finally {
    q.restore();
  }
});

test('Use CUDA is stored as "yes", turns the flavour to CUDA, and restarts a worker that started on Vulkan', async () => {
  const q = asking('Use CUDA');
  try {
    const store = memento();
    const { ctl } = controller(store);
    const restarts = [];
    ctl.onRestartNeeded = () => restarts.push(1);
    assert.equal(ctl.beginStart(false), 'vulkan', 'the first start is Vulkan: no yes yet');
    ctl.noteOutput(CAPABLE);
    await settle();
    assert.equal(store.get('dlne.cudaConsent'), 'yes');
    assert.equal(ctl.getState().consent, 'yes');
    assert.equal(ctl.resolve(), 'cuda');
    assert.equal(restarts.length, 1, 'the worker restarts on CUDA');
    assert.equal(ctl.beginStart(false), 'cuda');
    ctl.noteOutput('DLNE_ENGINE: {"flavour": "cuda", "fallback": ""}');
    assert.equal(ctl.getState().running, 'cuda');
    ctl.noteOutput(CAPABLE);
    await settle();
    assert.equal(restarts.length, 1, 'no second restart once the worker runs what is resolved');
    ctl.dispose();
  } finally {
    q.restore();
  }
});

test('Not now is asked again after 3 days and not before', async () => {
  const q = asking('Not now');
  try {
    const store = memento();
    const { ctl, clock } = controller(store);
    const start = clock.now;
    ctl.noteOutput(CAPABLE);
    await settle();
    assert.deepEqual(store.get('dlne.cudaConsent'), { later: start + 3 * DAY });
    assert.equal(ctl.getState().consent, 'later');
    assert.equal(ctl.resolve(), 'vulkan');
    ctl.dispose();

    // A new session two days later: not yet.
    clock.now = start + 2 * DAY;
    const second = controller(store, clock).ctl;
    second.noteOutput(CAPABLE);
    await settle();
    assert.equal(q.asked.length, 1, 'not asked on day 2');
    assert.equal(second.getState().consent, 'later');
    second.dispose();

    // Past the 3 days: asked again.
    clock.now = start + 3 * DAY + 1000;
    const third = controller(store, clock).ctl;
    assert.equal(third.getState().consent, 'unasked');
    third.noteOutput(CAPABLE);
    await settle();
    assert.equal(q.asked.length, 2, 'asked again after 3 days');
    third.dispose();
  } finally {
    q.restore();
  }
});

test('Keep Vulkan is never asked again, in this session or any later one', async () => {
  const q = asking('Keep Vulkan');
  try {
    const store = memento();
    const { ctl, clock } = controller(store);
    ctl.noteOutput(CAPABLE);
    await settle();
    assert.equal(store.get('dlne.cudaConsent'), 'never');
    assert.equal(ctl.resolve(), 'vulkan');
    ctl.dispose();
    clock.now += 400 * DAY;
    const later = controller(store, clock).ctl;
    later.noteOutput(CAPABLE);
    await settle();
    assert.equal(q.asked.length, 1);
    assert.equal(later.getState().consent, 'never');
    later.dispose();
  } finally {
    q.restore();
  }
});

test('no question when the GPU cannot, when the setting is vulkan or cuda, or when the owner already said yes', async () => {
  const q = asking('Use CUDA');
  try {
    let { ctl } = controller(memento());
    ctl.noteOutput(OLD_GPU);
    await settle();
    assert.equal(q.asked.length, 0, 'a GPU that cannot is never asked');
    assert.deepEqual([ctl.getState().capable, ctl.getState().reason, ctl.getState().gpu],
      [false, 'this GPU is older than Turing (compute capability 6.1)', 'NVIDIA GeForce GTX 1060']);
    ctl.dispose();

    ({ ctl } = controller(memento()));
    stub.state.settings['dlne.worker.engine'] = 'vulkan';
    ctl.noteOutput(CAPABLE);
    await settle();
    assert.equal(q.asked.length, 0, 'the vulkan setting asks nothing');
    ctl.dispose();

    ({ ctl } = controller(memento({ 'dlne.cudaConsent': 'yes' })));
    ctl.noteOutput(CAPABLE);
    await settle();
    assert.equal(q.asked.length, 0);
    ctl.dispose();
  } finally {
    q.restore();
  }
});

test('the GPU facts are remembered, so the panel can show them before a worker has run', () => {
  const store = memento();
  const { ctl } = controller(store);
  assert.deepEqual([ctl.getState().capable, ctl.getState().gpu], [null, '']);
  stub.state.settings['dlne.worker.engine'] = 'vulkan';
  ctl.noteOutput(OLD_GPU);
  ctl.dispose();
  const next = controller(store).ctl;
  assert.equal(next.getState().capable, false);
  assert.equal(next.getState().gpu, 'NVIDIA GeForce GTX 1060');
  next.dispose();
});

test('choosing CUDA in the panel asks first unless the owner already said yes', async () => {
  // Declined: nothing is written.
  let q = asking('Not now');
  try {
    const { ctl } = controller(memento());
    stub.state.settings['dlne.worker.engine'] = 'vulkan';
    ctl.noteOutput(CAPABLE);
    await ctl.setSetting('cuda');
    assert.equal(q.asked.length, 1);
    assert.equal(stub.state.settings['dlne.worker.engine'], 'vulkan', 'the setting stays');
    ctl.dispose();
  } finally {
    q.restore();
  }
  // Accepted: stored, then written.
  q = asking('Use CUDA');
  try {
    const store = memento();
    const { ctl } = controller(store);
    stub.state.settings['dlne.worker.engine'] = 'vulkan';
    ctl.noteOutput(CAPABLE);
    await ctl.setSetting('cuda');
    assert.equal(store.get('dlne.cudaConsent'), 'yes');
    assert.equal(stub.state.settings['dlne.worker.engine'], 'cuda');
    assert.equal(ctl.getState().setting, 'cuda');
    assert.equal(ctl.resolve(), 'cuda');
    // Already yes: no second question.
    await ctl.setSetting('auto');
    await ctl.setSetting('cuda');
    assert.equal(q.asked.length, 1);
    ctl.dispose();
  } finally {
    q.restore();
  }
  // A GPU that cannot is not asked; the choice is kept and the reason shown.
  q = asking('Use CUDA');
  try {
    const { ctl } = controller(memento());
    ctl.noteOutput(OLD_GPU);
    await ctl.setSetting('cuda');
    assert.equal(q.asked.length, 0);
    assert.equal(stub.state.settings['dlne.worker.engine'], 'cuda');
    assert.equal(ctl.resolve(), 'vulkan');
    assert.match(ctl.getState().reason, /older than Turing/);
    ctl.dispose();
  } finally {
    q.restore();
  }
});

test('Vulkan and Auto are set without a question', async () => {
  const q = asking('Use CUDA');
  try {
    const { ctl } = controller(memento());
    ctl.noteOutput(CAPABLE);
    await settle();
    q.asked.length = 0;
    await ctl.setSetting('vulkan');
    await ctl.setSetting('auto');
    assert.equal(q.asked.length, 0);
    assert.deepEqual(stub.state.updates.filter(([k]) => k === 'dlne.worker.engine').map(([, v]) => v), ['vulkan', 'auto']);
    ctl.dispose();
  } finally {
    q.restore();
  }
});

test('a CUDA engine that could not start leaves Vulkan running and is not retried until the setting changes', async () => {
  const q = asking(undefined);
  try {
    const { ctl } = controller(memento({ 'dlne.cudaConsent': 'yes' }));
    const restarts = [];
    ctl.onRestartNeeded = () => restarts.push(1);
    ctl.noteOutput(CAPABLE);
    assert.equal(ctl.beginStart(false), 'cuda');
    ctl.noteOutput('DLNE_ENGINE: {"flavour": "vulkan", "fallback": "the driver is out of date"}');
    const state = ctl.getState();
    assert.deepEqual([state.running, state.fallback], ['vulkan', 'the driver is out of date']);
    assert.equal(ctl.resolve(), 'vulkan', 'no retry');
    ctl.noteOutput(CAPABLE);
    await settle();
    assert.deepEqual(restarts, [], 'and no restart loop');
    assert.equal(ctl.beginStart(false), 'vulkan');
    // The owner changes the engine setting: CUDA gets another try.
    await ctl.setSetting('cuda');
    assert.equal(ctl.resolve(), 'cuda');
    ctl.dispose();
  } finally {
    q.restore();
  }
});

test('a computer that lends its CPU only is always started on Vulkan', () => {
  const { ctl } = controller(memento({ 'dlne.cudaConsent': 'yes' }));
  stub.state.settings['dlne.worker.engine'] = 'cuda';
  ctl.noteOutput(CAPABLE);
  assert.equal(ctl.beginStart(true), 'vulkan');
  assert.equal(ctl.beginStart(false), 'cuda');
  ctl.dispose();
});

test('the panel\'s three functions work through the module and follow the controller that replaces another', async () => {
  const q = asking('Keep Vulkan');
  try {
    stub.reset();
    const seen = [];
    const sub = f.onEngineStateChanged((state) => seen.push(state.consent));
    const ctl = f.attachEngineFlavour(memento());
    assert.deepEqual(f.getEngineState(), {
      setting: 'auto', running: null, capable: null, reason: '', gpu: '', fallback: '', consent: 'unasked',
    });
    ctl.noteOutput(CAPABLE);
    await settle();
    assert.equal(f.getEngineState().consent, 'never');
    assert.ok(seen.includes('never'), 'a listener set before the controller existed hears it');
    await f.setEngineSetting('vulkan');
    assert.equal(f.getEngineState().setting, 'vulkan');
    sub.dispose();
    ctl.dispose();
  } finally {
    q.restore();
  }
});

// ---- the worker manager -------------------------------------------------------------------------------------------

function installEngine() {
  const root = path.join(process.env.HOME, '.dlne', 'runtime');
  fs.mkdirSync(path.join(root, 'config'), { recursive: true });
  fs.writeFileSync(path.join(root, 'dlne_server.py'), 'p_worker.add_argument("--coding-max-ram", type=float)\n');
}

/** A manager (GPU, no outside worker) whose spawned workers are fakes that record their environment. */
function rig(consent, probe) {
  stub.reset();
  Object.assign(stub.state.settings, {
    'dlne.worker.device': 'gpu', 'dlne.worker.pythonPath': process.execPath, 'dlne.autoUpdate': false, 'dlne.serverUrl': 'http://127.0.0.1:9',
  });
  installEngine();
  const store = memento({ ...(consent ? { 'dlne.cudaConsent': consent } : {}), ...(probe ? { 'dlne.cudaProbe': probe } : {}) });
  const wm = new WorkerManager({ subscriptions: [], globalState: store });
  wm.isSystemdActive = () => false;
  wm.isProcessActive = () => false;
  wm.isTrayWorkerActive = () => false;
  wm.readTrayHandover = () => ({ canPause: false, paused: false });
  wm.isPortListening = async () => false;
  wm.ensureRpcBinaries = async () => {};
  wm.localGpu = { hasGpu: true, name: 'RTX 4070 Laptop GPU', vramTotalGb: 8, api: 'vulkan' };
  const workers = [];
  const real = childProcess.spawn;
  childProcess.spawn = (cmd, args, options) => {
    const worker = Object.assign(new EventEmitter(), {
      stdout: new EventEmitter(), stderr: new EventEmitter(), pid: 40000 + workers.length, exitCode: null, signalCode: null, killed: false, env: options.env, args,
      kill(signal) { this.killed = true; setImmediate(() => { this.exitCode = 0; this.emit('exit', 0, signal); }); return true; },
    });
    workers.push(worker);
    return worker;
  };
  const say = (worker, text) => worker.stdout.emit('data', Buffer.from(`${text}\n`));
  const done = () => { childProcess.spawn = real; wm.dispose(); };
  return { wm, store, workers, say, done };
}

test('the worker is started with DLNE_ENGINE_FLAVOR: vulkan until a yes, cuda after it', async () => {
  const q = asking('Use CUDA');
  const t = rig();
  try {
    assert.equal(await t.wm.startWorker(), true);
    assert.equal(t.workers.length, 1);
    assert.equal(t.workers[0].env.DLNE_ENGINE_FLAVOR, 'vulkan', 'nothing CUDA before the owner says yes');
    // The worker says what the GPU is; the owner says yes; the worker is restarted on CUDA.
    t.say(t.workers[0], CAPABLE);
    for (let i = 0; i < 20 && t.workers.length < 2; i++) await new Promise((resolve) => setTimeout(resolve, 25));
    assert.equal(q.asked.length, 1);
    assert.equal(t.workers.length, 2, 'restarted once');
    assert.equal(t.workers[1].env.DLNE_ENGINE_FLAVOR, 'cuda');
    t.say(t.workers[1], 'DLNE_ENGINE: {"flavour": "cuda", "fallback": ""}');
    assert.equal(t.wm.getStatus().engineFlavour, 'cuda');
    assert.equal(f.getEngineState().running, 'cuda');
  } finally {
    t.done();
    q.restore();
  }
});

test('a computer that said yes before starts on CUDA at once; the status shows what the worker says it runs', async () => {
  const q = asking(undefined);
  const known = { capable: true, reason: '', gpu: 'NVIDIA GeForce RTX 4070 Laptop GPU', computeCap: '8.9', driver: '615.78' };
  const t = rig('yes', known);
  try {
    assert.equal(await t.wm.startWorker(), true);
    assert.equal(t.workers.at(-1).env.DLNE_ENGINE_FLAVOR, 'cuda');
    assert.equal(t.wm.getStatus().engineFlavour, undefined, 'nothing is claimed until the worker says so');
    t.say(t.workers.at(-1), 'DLNE_ENGINE: {"flavour": "vulkan", "fallback": "the driver is out of date"}');
    assert.equal(t.wm.getStatus().engineFlavour, 'vulkan');
    assert.equal(f.getEngineState().fallback, 'the driver is out of date');
    assert.equal(t.workers.length, 1, 'a fall-back is not a reason to restart');
  } finally {
    t.done();
    q.restore();
  }
});

test('a Stop forgets the running flavour', async () => {
  const q = asking(undefined);
  const t = rig('yes');
  try {
    assert.equal(await t.wm.startWorker(), true);
    t.say(t.workers[0], 'DLNE_ENGINE: {"flavour": "vulkan", "fallback": ""}');
    assert.equal(f.getEngineState().running, 'vulkan');
    await t.wm.stopWorker(true);
    assert.equal(f.getEngineState().running, null);
    assert.equal(t.wm.getStatus().engineFlavour, undefined);
  } finally {
    t.done();
    q.restore();
  }
});

test('the card and the tooltip name the running flavour', () => {
  const view = (engineFlavour) => ({
    state: 'contributing', cpuPool: false, vramContributedGb: 8, engineFlavour,
    gpu: { name: 'RTX 4070', api: 'vulkan', vramTotalGb: 8 },
  });
  assert.match(c.contributionCopy(view('cuda')).deviceHtml, /^<strong>CUDA GPU:<\/strong>/);
  assert.match(c.contributionCopy(view('vulkan')).deviceHtml, /^<strong>VULKAN GPU:<\/strong>/);
  assert.match(c.contributionCopy(view(undefined)).deviceHtml, /^<strong>VULKAN GPU:<\/strong>/);
});

test('the setting is in package.json: auto, vulkan, cuda, default auto, application scope, described in plain words', () => {
  const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'package.json'), 'utf8'));
  const setting = pkg.contributes.configuration.properties['dlne.worker.engine'];
  assert.deepEqual(setting.enum, ['auto', 'vulkan', 'cuda']);
  assert.equal(setting.default, 'auto');
  assert.equal(setting.scope, 'application');
  assert.equal(setting.enumDescriptions.length, 3);
});
