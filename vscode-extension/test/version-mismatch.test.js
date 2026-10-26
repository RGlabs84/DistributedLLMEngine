'use strict';
/**
 * When the coordinator is upgraded, every running worker is refused and exits with code 75.
 * The extension must notice, fetch the matching signed engine and start the worker again, once
 * or twice, and report honestly if that does not fix it, instead of looping or going silent.
 */
const test = require('node:test');
const assert = require('node:assert/strict');

const stub = require('./helpers/vscode-stub');
const { WorkerManager } = require('../dist/worker/workerManager');

function manager() {
  stub.reset();
  stub.state.warnings = [];
  const wm = new WorkerManager({ subscriptions: [] });
  wm.currentState = 'idle';
  wm.externalWorker = false; // a fake worker another test file runs is not this machine's
  wm.restarts = 0;
  wm.startWorker = async () => {
    wm.restarts++;
    return true;
  };
  return wm;
}

const settle = () => new Promise((r) => setImmediate(r));
const log = () => stub.state.channels.map((c) => c.text()).join('\n');

test('exit code 75 triggers a restart that re-syncs the engine', async () => {
  const wm = manager();
  try {
    wm.onWorkerExit(75);
    await settle();
    assert.equal(wm.restarts, 1);
    assert.match(log(), /different engine version\. Updating the engine and restarting/);
  } finally {
    wm.dispose();
  }
});

test('an ordinary exit does not restart anything', async () => {
  const wm = manager();
  try {
    for (const code of [0, 1, 2, 74, 76, 137, null]) wm.onWorkerExit(code);
    await settle();
    assert.equal(wm.restarts, 0);
  } finally {
    wm.dispose();
  }
});

test('the worker is shown as stopped immediately, whatever happens next', async () => {
  const wm = manager();
  try {
    wm.currentState = 'contributing';
    wm.onWorkerExit(1);
    assert.equal(wm.getStatus().state, 'idle');
    assert.match(wm.getStatus().message, /code 1/);
    assert.equal(wm.getStatus().vramContributedGb, 0);
  } finally {
    wm.dispose();
  }
});

test('a persistent mismatch is reported after two automatic attempts, not looped on', async () => {
  const wm = manager();
  const warnings = [];
  stub.vscode.window.showWarningMessage = async (m) => warnings.push(m);
  try {
    for (let i = 0; i < 5; i++) {
      wm.onWorkerExit(75);
      await settle();
    }
    assert.equal(wm.restarts, 2, 'exactly two automatic restarts, then it stops');
    assert.equal(wm.getStatus().state, 'error');
    assert.match(wm.getStatus().message, /Could not match/);
    assert.ok(warnings.some((w) => /still doesn't match the coordinator/.test(w)), 'the user is told');
  } finally {
    wm.dispose();
    delete stub.vscode.window.showWarningMessage;
    stub.vscode.window.showWarningMessage = async () => undefined;
  }
});

test('restarts spread over more than ten minutes are not counted together', async () => {
  const wm = manager();
  try {
    wm.versionRestarts = [Date.now() - 11 * 60 * 1000, Date.now() - 12 * 60 * 1000];
    wm.onWorkerExit(75);
    await settle();
    assert.equal(wm.restarts, 1, 'old attempts have aged out of the window');
  } finally {
    wm.dispose();
  }
});

test('with auto-update off the engine is not touched and the user is told why', async () => {
  const wm = manager();
  const warnings = [];
  stub.vscode.window.showWarningMessage = async (m) => warnings.push(m);
  try {
    stub.state.settings['dlne.autoUpdate'] = false;
    wm.onWorkerExit(75);
    await settle();
    assert.equal(wm.restarts, 0);
    assert.ok(warnings.some((w) => /dlne\.autoUpdate/.test(w)));
    assert.match(log(), /dlne\.autoUpdate is off/);
  } finally {
    wm.dispose();
    stub.vscode.window.showWarningMessage = async () => undefined;
  }
});

test('the exit code the extension reacts to is the one the engine uses', () => {
  const fs = require('node:fs');
  const path = require('node:path');
  const engine = fs.readFileSync(path.join(__dirname, '..', '..', 'engine', 'worker.py'), 'utf8');
  const extension = fs.readFileSync(path.join(__dirname, '..', 'src', 'worker', 'workerManager.ts'), 'utf8');
  assert.match(engine, /VERSION_MISMATCH_EXIT_CODE = 75/);
  assert.match(extension, /VERSION_MISMATCH_EXIT_CODE = 75/);
});
