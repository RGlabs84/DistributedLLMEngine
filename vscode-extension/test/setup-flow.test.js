'use strict';
/**
 * The first-run installer (WorkerManager.ensureWorkerRuntime) against a sandboxed machine.
 * Every failure a new user can hit must (a) stop the install, (b) say what went wrong, and
 * (c) never announce success. The one success case must announce it exactly once and leave a
 * usable venv behind.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const stub = require('./helpers/vscode-stub');
const { createSandbox } = require('./helpers/sandbox');
const { WorkerManager } = require('../dist/worker/workerManager');

const SUCCESS = /installed successfully/i;

/** Runs the installer in a sandbox and tears everything down, whatever happens. */
async function install(sandboxOpts, env, check) {
  stub.reset();
  const sb = await createSandbox(sandboxOpts);
  stub.state.settings['dlne.serverUrl'] = sb.serverUrl;
  sb.set(env || {});
  const wm = new WorkerManager({ subscriptions: [] }, { releasePublicKeyHex: sb.releasePublicKeyHex });
  try {
    const result = await wm.ensureWorkerRuntime(false);
    await new Promise((r) => setImmediate(r)); // let the error toast's .then() run
    await check({ result, sb, wm, state: stub.state, log: stub.state.channels.map((c) => c.text()).join('\n') });
  } finally {
    wm.dispose();
    sb.restore();
  }
}

const errors = (state) => state.errorMessages.map((e) => e.message).join('\n');
const nothingClaimedSuccess = (state) => assert.ok(!state.infoMessages.some((m) => SUCCESS.test(m)), `unexpected success: ${state.infoMessages}`);

test('no Python at all: refuses, and tells the user to install 3.10+', async () => {
  await install({ python: 'none' }, {}, ({ result, state }) => {
    assert.equal(result, null);
    assert.match(errors(state), /Python 3\.10\+/);
    nothingClaimedSuccess(state);
  });
});

test('Python 3.9 is rejected instead of being treated as good enough', async () => {
  await install({ python: 'fake' }, { FAKE_PYVER: '3.9' }, ({ result, state, log }) => {
    assert.equal(result, null);
    assert.match(log, /older than 3\.10/);
    assert.match(errors(state), /Python 3\.10\+/);
    nothingClaimedSuccess(state);
  });
});

test('Python 3.10 exactly is accepted (boundary)', async () => {
  await install({ python: 'fake' }, { FAKE_PYVER: '3.10' }, ({ result, state }) => {
    assert.ok(result, 'install should succeed on 3.10');
    assert.match(state.infoMessages.join('\n'), SUCCESS);
  });
});

test('Python 3.4 is not mistaken for 3.10+ (numeric, not string, comparison)', async () => {
  await install({ python: 'fake' }, { FAKE_PYVER: '3.4' }, ({ result }) => {
    assert.equal(result, null);
  });
});

test('missing venv module: stops, names python3-venv, offers Show Log', async () => {
  stub.reset();
  await install({ python: 'fake' }, { FAKE_VENV: 'fail' }, ({ result, state, log }) => {
    assert.equal(result, null);
    assert.match(errors(state), /virtual environment/i);
    assert.match(errors(state), /sudo apt install python3-venv/);
    assert.deepEqual(state.errorMessages[0].items, ['Show Log']);
    assert.match(log, /ensurepip is not available/);
    nothingClaimedSuccess(state);
  });
});

test('clicking Show Log opens the DLNE output channel', async () => {
  stub.reset();
  stub.state.errorChoice = 'Show Log';
  // install() calls stub.reset(), which clears errorChoice, so drive it by hand.
  const sb = await createSandbox({ python: 'fake' });
  stub.state.settings['dlne.serverUrl'] = sb.serverUrl;
  sb.set({ FAKE_VENV: 'fail' });
  stub.state.errorChoice = 'Show Log';
  const wm = new WorkerManager({ subscriptions: [] }, { releasePublicKeyHex: sb.releasePublicKeyHex });
  try {
    await wm.ensureWorkerRuntime(false);
    await new Promise((r) => setImmediate(r));
    assert.ok(stub.state.channels.some((c) => c.shown > 0), 'output channel was never shown');
  } finally {
    wm.dispose();
    sb.restore();
  }
});

test('pip failure: stops with the pip error and never claims success', async () => {
  await install({ python: 'fake' }, { FAKE_PIP: 'fail' }, ({ result, state, log }) => {
    assert.equal(result, null);
    assert.match(errors(state), /dependencies failed/i);
    assert.match(errors(state), /Could not find a version/);
    assert.match(log, /Install Python dependencies: failed \(exit 1\)/);
    nothingClaimedSuccess(state);
  });
});

test('dependencies that install but cannot be imported are caught before reporting success', async () => {
  await install({ python: 'fake' }, { FAKE_IMPORT: 'fail' }, ({ result, state }) => {
    assert.equal(result, null);
    assert.match(errors(state), /could not be imported/i);
    assert.match(errors(state), /No module named 'fastapi'/);
    nothingClaimedSuccess(state);
  });
});

test('happy path: announces success once, returns the venv python, writes config', async () => {
  await install({ python: 'fake' }, {}, ({ result, sb, state }) => {
    assert.ok(result);
    assert.equal(result.pythonPath, path.join(sb.runtimeDir, '.venv', 'bin', 'python'));
    assert.equal(result.scriptPath, path.join(sb.runtimeDir, 'dlne_server.py'));
    assert.equal(state.errorMessages.length, 0);
    assert.equal(state.infoMessages.filter((m) => SUCCESS.test(m)).length, 1);
    assert.ok(state.updates.some(([k, v]) => k === 'dlne.worker.pythonPath' && v === result.pythonPath));
    const cfg = fs.readFileSync(path.join(sb.runtimeDir, 'config', 'config.txt'), 'utf8');
    assert.match(cfg, /^NETWORK_KEY=[0-9a-f]{64}$/m);
    assert.match(cfg, /^COORDINATOR_URL=/m);
  });
});

test('a machine without unzip still extracts the engine (Python zipfile fallback)', async () => {
  await install({ python: 'fake', unzip: false }, {}, ({ result, sb, state }) => {
    assert.ok(result, `install failed without unzip: ${errors(state)}`);
    assert.ok(fs.existsSync(path.join(sb.runtimeDir, 'dlne_server.py')), 'engine was not extracted');
    assert.equal(state.errorMessages.length, 0);
  });
});

test('no working extractor: fails cleanly with a message instead of crashing', async (t) => {
  // GNU tar cannot read zips; bsdtar can, which would make this scenario unreachable.
  const { spawnSync } = require('node:child_process');
  if (/bsdtar|libarchive/i.test(spawnSync('tar', ['--version'], { encoding: 'utf8' }).stdout)) {
    return t.skip('tar here is bsdtar and can read zips');
  }
  await install({ python: 'none', unzip: false }, {}, ({ result, state }) => {
    assert.equal(result, null);
    assert.match(errors(state), /unpack the downloaded engine archive/i);
    assert.deepEqual(state.errorMessages[0].items, ['Show Log']);
    nothingClaimedSuccess(state);
  });
});

test('runSetupStep: success, failure with a tail, and timeout', async () => {
  stub.reset();
  const wm = new WorkerManager({ subscriptions: [] });
  try {
    const ok = await wm.runSetupStep('echo hello', 'say hello', 5000);
    assert.deepEqual(ok, { ok: true, tail: 'hello' });

    const bad = await wm.runSetupStep('echo first; echo boom >&2; exit 3', 'fail', 5000);
    assert.equal(bad.ok, false);
    assert.match(bad.tail, /boom/);

    const slow = await wm.runSetupStep('sleep 5', 'slow', 300);
    assert.equal(slow.ok, false);
    assert.match(slow.tail, /Timed out after 0s|Timed out after 1s/);

    const missing = await wm.runSetupStep('/definitely/not/a/binary', 'missing', 5000);
    assert.equal(missing.ok, false);
  } finally {
    wm.dispose();
  }
});

test('runSetupStep keeps only the last 15 output lines', async () => {
  stub.reset();
  const wm = new WorkerManager({ subscriptions: [] });
  try {
    const r = await wm.runSetupStep('i=1; while [ $i -le 40 ]; do echo line$i; i=$((i+1)); done; exit 1', 'noisy', 5000);
    const lines = r.tail.split('\n');
    assert.equal(lines.length, 15);
    assert.equal(lines[0], 'line26');
    assert.equal(lines[14], 'line40');
  } finally {
    wm.dispose();
  }
});
