'use strict';
/**
 * A DLNE worker this editor did not start (a systemd service, a terminal, another editor) is described as such,
 * never from the settings here, and Start Contributing never starts the dlne-worker systemd service: that service
 * runs with its own fixed arguments, and starting it turned the owner's "lend my CPU, 16 GB" into the 8 GB GPU worker
 * the service was set up as (26 Oct 2026). Stop removes an orphaned engine by this install's path only.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const childProcess = require('node:child_process');

const stub = require('./helpers/vscode-stub');
const { WorkerManager } = require('../dist/worker/workerManager');
const c = require('../dist/worker/contribution');

process.env.HOME = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-external-home-'));
process.env.USERPROFILE = process.env.HOME;
WorkerManager.prototype.init = async function init() {};

const CPU_16 = {
  'dlne.worker.device': 'cpu',
  'dlne.worker.maxRamGb': 16,
  'dlne.worker.pythonPath': process.execPath,
  'dlne.autoUpdate': false,
  'dlne.serverUrl': 'http://127.0.0.1:9',
};

/** A manager whose machine-wide probes say what the test wants; constructed with them already in place. */
function manager({ running = false } = {}) {
  stub.reset();
  Object.assign(stub.state.settings, CPU_16);
  const proto = WorkerManager.prototype;
  const real = { sysd: proto.isSystemdActive, proc: proto.isProcessActive };
  proto.isSystemdActive = () => running;
  proto.isProcessActive = () => false;
  try {
    const wm = new WorkerManager({ subscriptions: [], globalState: undefined });
    wm.isSystemdActive = () => running;
    wm.isProcessActive = () => false;
    wm.isPortListening = async () => running;
    wm.ensureRpcBinaries = async () => {};
    wm.localGpu = { hasGpu: true, name: 'RTX 4070 Laptop GPU', vramTotalGb: 8, api: 'vulkan' };
    return wm;
  } finally {
    proto.isSystemdActive = real.sysd;
    proto.isProcessActive = real.proc;
  }
}

function installEngine() {
  const root = path.join(process.env.HOME, '.dlne', 'runtime');
  fs.mkdirSync(path.join(root, 'config'), { recursive: true });
  fs.writeFileSync(path.join(root, 'dlne_server.py'), 'p_worker.add_argument("--coding-max-ram", type=float)\n');
  // The hand-made service that caused it: present, but this editor must never start it.
  const units = path.join(process.env.HOME, '.config', 'systemd', 'user');
  fs.mkdirSync(units, { recursive: true });
  fs.writeFileSync(path.join(units, 'dlne-worker.service'), '[Service]\nExecStart=/usr/bin/false\n');
}

test('Start Contributing never starts the dlne-worker service; it starts its own worker with the settings here', async () => {
  installEngine();
  const wm = manager();
  const realExec = childProcess.execSync;
  const realSpawn = childProcess.spawn;
  const execs = [];
  const spawned = [];
  childProcess.execSync = (cmd, ...rest) => { execs.push(String(cmd)); return realExec(cmd, ...rest); };
  childProcess.spawn = (...args) => { spawned.push(args); return realSpawn(...args); };
  try {
    assert.equal(await wm.startWorker(), true);
  } finally {
    childProcess.execSync = realExec;
    childProcess.spawn = realSpawn;
    if (wm.workerProcess) wm.workerProcess.kill();
    wm.dispose();
  }
  assert.ok(!execs.some((cmd) => /systemctl\s+--user\s+start/.test(cmd)), `no systemctl start: ${execs.join(' | ')}`);
  assert.equal(spawned.length, 1, 'one worker, this editor\'s own');
  const args = spawned[0][1];
  assert.ok(args.includes('worker'));
  assert.equal(args[args.indexOf('--allowed-gpus') + 1], 'none', 'CPU only: the GPU is hidden from the worker');
  assert.equal(args[args.indexOf('--max-ram') + 1], '16', 'the 16 GB the owner chose');
});

test('a worker started outside VS Code is said to be so, and nothing about it is read from the settings here', async () => {
  const wm = manager({ running: true });
  const realSpawn = childProcess.spawn;
  const spawned = [];
  childProcess.spawn = (...args) => { spawned.push(args); return realSpawn(...args); };
  try {
    const st0 = wm.getStatus();
    assert.equal(st0.external, true, 'found at start-up');
    assert.equal(await wm.startWorker(), true);
    assert.equal(spawned.length, 0, 'no second worker on top of it');
  } finally {
    childProcess.spawn = realSpawn;
    wm.dispose();
  }
  const st = wm.getStatus();
  assert.equal(st.state, 'contributing');
  assert.equal(st.external, true);
  assert.equal(st.cpuPool, false, 'not described as this editor\'s CPU pool contribution');
  assert.equal(st.role, undefined, 'no job read from the settings');
  assert.equal(st.message, c.EXTERNAL_WORKER_LINE);
  assert.ok(stub.state.infoMessages.some((m) => m.includes(c.EXTERNAL_WORKER_TEXT)), 'the owner is told, once, when they try to start');
});

test('the sidebar card, the tooltip and the role line say "outside VS Code", never the settings\' CPU and RAM', () => {
  const view = { state: 'contributing', cpuPool: false, vramContributedGb: 0, external: true,
    gpu: { name: 'RTX 4070 Laptop GPU', api: 'vulkan', vramTotalGb: 8 } };
  const card = c.contributionCopy(view);
  assert.match(card.deviceHtml, /Outside VS Code/);
  assert.ok(!/16 GB|CPU pool|Contributing \d/.test(card.deviceHtml + card.ruleHtml));
  assert.equal(card.buttonText, '⏹ Stop Contributing');
  const tip = c.contributionTooltip(view).join('\n');
  assert.match(tip, /OUTSIDE VS CODE/);
  assert.ok(!/RTX 4070|GB via/.test(tip));
  assert.match(c.roleCopy(view).roleHtml, /not known here/);
});

test('Stop removes an orphaned engine by this install\'s full path, never every ggml-rpc-server on the machine', () => {
  const unix = c.orphanRpcStopCommand('/home/a b/.dlne/runtime/bin/ggml-rpc-server', 'linux');
  assert.deepEqual(unix, ['pkill', ['-9', '-f', '^/home/a b/\\.dlne/runtime/bin/ggml-rpc-server( |$)']]);
  const win = c.orphanRpcStopCommand("C:\\Users\\O'Brien\\AppData\\Local\\DLNE\\runtime\\bin\\ggml-rpc-server.exe", 'win32');
  assert.equal(win[0], 'powershell');
  assert.match(win[1].join(' '), /Where-Object \{ \$_\.Path -eq 'C:\\Users\\O''Brien\\AppData\\Local\\DLNE\\runtime\\bin\\ggml-rpc-server\.exe' \}/);
  assert.ok(!/taskkill|killall|\/IM /.test(win[1].join(' ')), 'never by image name');
  assert.equal(c.orphanRpcStopCommand('', 'linux'), null);
  const src = fs.readFileSync(path.join(__dirname, '..', 'src', 'worker', 'workerManager.ts'), 'utf8');
  assert.ok(!/killall|taskkill \/F \/IM/.test(src), 'no machine-wide kill left in the worker manager');
  assert.ok(!/systemctl --user start/.test(src), 'and no systemd start');
});
