'use strict';
/**
 * What differs on Windows (and in a pipe) for the extension that runs the worker: no console windows,
 * quoting of paths with spaces and apostrophes, the executable name of every Windows build, worker
 * output that arrives in pieces or with CRLF line endings, and a stop that does not kill a newer worker.
 * Runs on any machine: the platform and child_process are replaced where it matters.
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
const { RPC_PLATFORMS } = require('../dist/worker/engineRelease');

process.env.HOME = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-windows-worker-home-'));
WorkerManager.prototype.init = async function init() {};

function manager(settings = {}) {
  stub.reset();
  Object.assign(stub.state.settings, settings);
  const wm = new WorkerManager({ subscriptions: [], globalState: undefined });
  wm.currentState = 'idle';
  wm.externalWorker = false; // a fake worker another test file runs is not this machine's
  return wm;
}

/** Run `fn` while process.platform says `platform`. */
function onPlatform(platform, fn) {
  const real = Object.getOwnPropertyDescriptor(process, 'platform');
  Object.defineProperty(process, 'platform', { value: platform });
  try {
    return fn();
  } finally {
    Object.defineProperty(process, 'platform', real);
  }
}

// ---- quoting ------------------------------------------------------------------------------

test('a PowerShell path with an apostrophe is doubled, so a user called O\'Brien can install', () => {
  assert.equal(c.powershellQuote("C:\\Users\\O'Brien\\x.zip"), "'C:\\Users\\O''Brien\\x.zip'");
  const command = c.powershellExpandCommand("C:\\Users\\O'Brien\\AppData\\Local\\DLNE\\runtime\\a [1].zip", "C:\\Users\\O'Brien\\AppData\\Local\\DLNE\\runtime");
  assert.equal(
    command,
    `powershell -NoProfile -Command "Expand-Archive -LiteralPath 'C:\\Users\\O''Brien\\AppData\\Local\\DLNE\\runtime\\a [1].zip' -DestinationPath 'C:\\Users\\O''Brien\\AppData\\Local\\DLNE\\runtime' -Force"`
  );
});

test('a path with a space is quoted for a command line, and a quoted one is left alone', () => {
  assert.equal(c.quoteIfSpaced('C:\\Users\\Jane Doe\\python.exe'), '"C:\\Users\\Jane Doe\\python.exe"');
  assert.equal(c.quoteIfSpaced('C:\\Python311\\python.exe'), 'C:\\Python311\\python.exe');
  assert.equal(c.quoteIfSpaced('"C:\\a b\\python.exe"'), '"C:\\a b\\python.exe"');
});

// ---- the engine's executable name ---------------------------------------------------------

test('every Windows build holds ggml-rpc-server.exe, including the ARM one', () => {
  for (const platform of RPC_PLATFORMS) {
    assert.equal(c.isRpcExecutablePlatform(platform), platform.startsWith('win'), platform);
  }
  assert.equal(c.isRpcExecutablePlatform('win-arm64'), true);
  assert.equal(c.isRpcExecutablePlatform('macos-arm64'), false);
  assert.equal(c.isRpcExecutablePlatform('linux-x64'), false);
});

// ---- worker output in pieces, with CRLF ---------------------------------------------------

test('lines are taken whole, whatever the chunks and line endings', () => {
  assert.deepEqual(c.takeCompleteLines('', 'abc'), { complete: '', rest: 'abc' });
  assert.deepEqual(c.takeCompleteLines('ab', 'c\r\nde'), { complete: 'abc\r\n', rest: 'de' });
  assert.deepEqual(c.takeCompleteLines('x', '\n\ny'), { complete: 'x\n\n', rest: 'y' });
  assert.equal(c.takeCompleteLines('', 'a'.repeat(200000)).rest.length, 65536, 'a worker that never ends a line cannot grow it');
});

test('a DLNE_ROLE line cut in two chunks, with CRLF endings, is read when it is whole', () => {
  const wm = manager();
  const trust = '0123456789abcdef0123456789abcdef';
  wm.noteWorkerOutput('Heartbeat OK\r\nDLNE_ROLE: coding {"model":"m.gg', 'stdout');
  assert.equal(wm.assignedRole, null, 'half a line says nothing yet');
  wm.noteWorkerOutput(`uf","slots":2,"trust_id":"${trust}"}\r\nHeartbeat OK\r\n`, 'stdout');
  assert.deepEqual(wm.assignedRole, { role: 'coding', model: 'm.gguf', slots: 2, trustId: trust });
  wm.dispose();
});

test('the two streams keep their unfinished lines apart', () => {
  const wm = manager();
  wm.noteWorkerOutput('DLNE_ROLE: cod', 'stdout');
  wm.noteWorkerOutput('some error text\r\n', 'stderr');
  assert.equal(wm.assignedRole, null);
  wm.noteWorkerOutput('ing\r\n', 'stdout');
  assert.equal(wm.assignedRole.role, 'coding');
  wm.dispose();
});

test('a refusal split in two chunks is read too', () => {
  const wm = manager();
  wm.noteWorkerOutput('DLNE_REFUSAL: this machine has 4 CPU co', 'stdout');
  wm.noteWorkerOutput('res, the swarm needs 4\r\n', 'stdout');
  assert.equal(wm.lastRefusal, 'this machine has 4 CPU cores, the swarm needs 4');
  wm.dispose();
});

// ---- no console windows -------------------------------------------------------------------

test('on Windows every probe the extension runs is started with windowsHide', async () => {
  const wm = manager();
  const calls = [];
  const real = childProcess.execSync;
  childProcess.execSync = (command, options) => { calls.push({ command, options }); throw new Error('not installed'); };
  try {
    // detectLocalGpu is async but has nothing to wait for: all its probes run before it returns.
    await onPlatform('win32', () => {
      wm.isProcessActive();
      wm.detectDiscreteVulkanIndices();
      return wm.detectLocalGpu();
    });
  } finally {
    childProcess.execSync = real;
    wm.dispose();
  }
  assert.ok(calls.length >= 4, `expected tasklist, nvidia-smi and two vulkaninfo runs, saw ${calls.map((x) => x.command)}`);
  for (const { command, options } of calls) {
    assert.equal(options && options.windowsHide, true, `${command} would flash a console window`);
  }
});

test('the worker itself is spawned hidden, with UTF-8 output, from a folder that is its own', async () => {
  const root = path.join(process.env.HOME, '.dlne', 'runtime');
  fs.mkdirSync(path.join(root, 'config'), { recursive: true });
  const marker = path.join(process.env.HOME, 'spawned.txt');
  fs.writeFileSync(path.join(root, 'dlne_server.py'), `require('fs').writeFileSync(${JSON.stringify(marker)}, process.env.PYTHONIOENCODING || '');`);
  const wm = manager({
    'dlne.worker.pythonPath': process.execPath,
    'dlne.worker.device': 'gpu',
    'dlne.autoUpdate': false,
    'dlne.serverUrl': 'http://127.0.0.1:9',
  });
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
    assert.equal(spawned[0][2].windowsHide, true, 'python.exe must not open a console window');
    assert.equal(spawned[0][2].env.PYTHONIOENCODING, 'utf-8');
    assert.equal(spawned[0][2].cwd, root);
    const deadline = Date.now() + 15000;
    while (!fs.existsSync(marker) && Date.now() < deadline) await new Promise((r) => setTimeout(r, 50));
    assert.equal(fs.readFileSync(marker, 'utf8'), 'utf-8');
  } finally {
    childProcess.spawn = realSpawn;
    if (wm.workerProcess) wm.workerProcess.kill();
    wm.dispose();
  }
});

// ---- stopping ----------------------------------------------------------------------------

test('a stop does not later kill a worker that was started after it', async () => {
  const wm = manager();
  const kills = [];
  const oldWorker = { kill: (signal) => kills.push(['old', signal]) };
  const newWorker = { kill: (signal) => kills.push(['new', signal]) };
  wm.workerProcess = oldWorker;
  wm.isSystemdActive = () => false;
  const realExec = childProcess.execSync;
  const realExecFile = childProcess.execFileSync;
  const realSetTimeout = global.setTimeout;
  const timers = [];
  const execFiles = [];
  childProcess.execSync = () => ''; // nothing real runs on the machine running the tests
  childProcess.execFileSync = (file, args) => { execFiles.push([file, args]); return ''; };
  wm.findEngineScript = () => path.join(os.tmpdir(), 'dlne engine', 'dlne_server.py');
  global.setTimeout = (fn, ms) => { timers.push({ fn, ms }); return { unref() {} }; };
  try {
    await wm.stopWorker();
  } finally {
    global.setTimeout = realSetTimeout;
    childProcess.execSync = realExec;
    childProcess.execFileSync = realExecFile;
  }
  assert.deepEqual(kills, [['old', 'SIGTERM']]);
  // An orphaned engine is stopped by this install's own path, never every ggml-rpc-server on the machine.
  const exe = path.join(os.tmpdir(), 'dlne engine', 'bin', process.platform === 'win32' ? 'ggml-rpc-server.exe' : 'ggml-rpc-server');
  assert.equal(execFiles.length, 1);
  assert.ok(execFiles[0][1].join(' ').includes(process.platform === 'win32' ? exe : exe.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')),
    'the orphan stop names this install\'s engine by its full path');
  assert.equal(timers.length, 1);
  assert.equal(timers[0].ms, process.platform === 'win32' ? 800 : 8000, 'a few seconds for the worker to stop its model server');

  wm.workerProcess = newWorker; // the owner started contributing again before the timer fired
  timers[0].fn();
  assert.deepEqual(kills, [['old', 'SIGTERM']], 'the newer worker is untouched');
  assert.equal(wm.workerProcess, newWorker);

  wm.workerProcess = oldWorker; // and a worker that really is still there is killed
  timers[0].fn();
  assert.deepEqual(kills, [['old', 'SIGTERM'], ['old', 'SIGKILL']]);
  wm.dispose();
});
