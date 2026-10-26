'use strict';
/**
 * The RAM the coding job may use. dlne.worker.maxRamGb defaults to 4, which is right for the GPU's other job (an
 * RPC node for the shared model) and wrong for a whole coding model, so the extension passes it as --max-ram exactly as
 * before and adds --coding-max-ram: -1 (no cap, the job counts only the RAM that is free when it starts) unless the user
 * set dlne.worker.maxRamGb, and then that value.
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

process.env.HOME = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-ram-cap-home-'));
process.env.USERPROFILE = process.env.HOME;
WorkerManager.prototype.init = async function init() {};

const SETTING = 'dlne.worker.maxRamGb';

// ---- the pure function --------------------------------------------------------------------

test('no setting from the user means no cap, and a setting means that value', () => {
  assert.equal(c.codingMaxRamGb(undefined), -1);
  assert.equal(c.codingMaxRamGb(null), -1);
  assert.equal(c.codingMaxRamGb(16), 16);
  assert.equal(c.codingMaxRamGb(4), 4, 'an explicit 4 is the user\'s choice, whatever the default is');
  assert.equal(c.codingMaxRamGb(12.5), 12.5);
});

test('zero, negative and nonsense values count as not set, as they do for --max-ram', () => {
  for (const junk of [0, -1, -8, Number.NaN, Infinity, '8', true, {}, []]) {
    assert.equal(c.codingMaxRamGb(junk), -1, String(junk));
  }
});

// ---- what the worker is started with -----------------------------------------------------

/** Start the worker with these settings and return the arguments it was spawned with. */
async function spawnedArgs(settings, engineSource = 'p_worker.add_argument("--coding-max-ram", type=float)\n') {
  stub.reset();
  Object.assign(stub.state.settings, {
    'dlne.worker.pythonPath': process.execPath,
    'dlne.worker.device': 'gpu',
    'dlne.autoUpdate': false,
    'dlne.serverUrl': 'http://127.0.0.1:9',
    ...settings,
  });
  const root = path.join(process.env.HOME, '.dlne', 'runtime');
  fs.rmSync(root, { recursive: true, force: true });
  fs.mkdirSync(path.join(root, 'config'), { recursive: true });
  fs.writeFileSync(path.join(root, 'dlne_server.py'), engineSource);
  const wm = new WorkerManager({ subscriptions: [], globalState: undefined });
  wm.currentState = 'idle';
  wm.localGpu = { hasGpu: true, name: 'RTX 4060', vramTotalGb: 8, api: 'vulkan' };
  wm.isSystemdActive = () => false;
  wm.isProcessActive = () => false;
  wm.isPortListening = async () => false;
  wm.ensureRpcBinaries = async () => {};
  const realSpawn = childProcess.spawn;
  const spawned = [];
  childProcess.spawn = (...args) => { spawned.push(args); return realSpawn(...args); };
  try {
    assert.equal(await wm.startWorker(), true);
    assert.equal(spawned.length, 1);
    return spawned[0][1];
  } finally {
    childProcess.spawn = realSpawn;
    if (wm.workerProcess) wm.workerProcess.kill();
    wm.dispose();
  }
}

const valueOf = (args, flag) => (args.includes(flag) ? args[args.indexOf(flag) + 1] : undefined);

test('left alone, --max-ram is the default 4 and the coding job is uncapped', async () => {
  const args = await spawnedArgs({});
  assert.equal(valueOf(args, '--max-ram'), '4');
  assert.equal(valueOf(args, '--coding-max-ram'), '-1');
});

test('a value the user set goes to both jobs', async () => {
  const args = await spawnedArgs({ [SETTING]: 12 });
  assert.equal(valueOf(args, '--max-ram'), '12');
  assert.equal(valueOf(args, '--coding-max-ram'), '12');
});

test('an explicit 4 is the user\'s and caps the coding job at 4', async () => {
  const args = await spawnedArgs({ [SETTING]: 4 });
  assert.equal(valueOf(args, '--max-ram'), '4');
  assert.equal(valueOf(args, '--coding-max-ram'), '4');
});

test('--max-ram is built exactly as before: unset or zero is the default 4', async () => {
  for (const value of [undefined, 0]) {
    const args = await spawnedArgs(value === undefined ? {} : { [SETTING]: value });
    assert.equal(valueOf(args, '--max-ram'), '4');
    assert.equal(valueOf(args, '--coding-max-ram'), '-1');
    assert.equal(args.filter((a) => a === '--max-ram').length, 1);
  }
});

test('an engine older than 2.4.0 is not given --coding-max-ram, which it would refuse and never start', async () => {
  const args = await spawnedArgs({}, 'p_worker.add_argument("--max-ram", type=float)\n');
  assert.equal(valueOf(args, '--max-ram'), '4');
  assert.ok(!args.includes('--coding-max-ram'));
});

// ---- what the user is told ---------------------------------------------------------------

test('the lend dialog says, in one sentence, what RAM the coding job uses and how to limit it', () => {
  const { detail } = c.lendGpuPrompt('RTX 4060');
  assert.equal(
    c.CODING_RAM_SENTENCE,
    'The coding job uses the memory that is free when it starts (it steps aside when you game); set dlne.worker.maxRamGb to limit it.'
  );
  assert.ok(detail.split('\n').includes(c.CODING_RAM_SENTENCE));
});

test('the setting\'s description names both jobs', () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'package.json'), 'utf8'));
  const description = manifest.contributes.configuration.properties[SETTING].description;
  assert.match(description, /shared-model job/);
  assert.match(description, /coding-member job is not held to the default 4/);
  assert.match(description, /unless you set this yourself/);
});

test('the README says it too', () => {
  const readme = fs.readFileSync(path.join(__dirname, '..', 'README.md'), 'utf8');
  assert.ok(readme.includes('The coding job uses the memory that is free when it starts'));
  assert.ok(readme.includes('--coding-max-ram'));
});
