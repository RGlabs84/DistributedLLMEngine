'use strict';
/**
 * A Windows PC without the Visual C++ Runtime: the engine exits 0xC0000135, the worker prints
 * "DLNE_NEEDS: vcredist" (engine/downloader.py), and the extension says what to install, once,
 * with the official link. It never blames the CPU.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const stub = require('./helpers/vscode-stub');
const { WorkerManager } = require('../dist/worker/workerManager');
const c = require('../dist/worker/contribution');

process.env.HOME = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-vcredist-home-'));
WorkerManager.prototype.init = async function init() {};

function manager() {
  stub.reset();
  const wm = new WorkerManager({ subscriptions: [], globalState: undefined });
  wm.currentState = 'idle';
  wm.externalWorker = false;
  return wm;
}

test('the marker and the link match the engine and Microsoft', () => {
  assert.equal(c.VCREDIST_MARKER, 'DLNE_NEEDS: vcredist');
  assert.equal(c.VCREDIST_URL, 'https://aka.ms/vs/17/release/vc_redist.x64.exe');
  assert.match(c.VCREDIST_MESSAGE, /Visual C\+\+ Runtime/);
  assert.doesNotMatch(c.VCREDIST_MESSAGE, /AVX/);
  assert.equal(c.parseVcredistNeeded('x\r\nDLNE_NEEDS: vcredist\r\n'), true);
  assert.equal(c.parseVcredistNeeded('Please ensure your CPU supports AVX2'), false);
});

test('the notice is shown once and Download opens the installer link', async () => {
  const wm = manager();
  stub.state.errorChoice = 'Download';
  wm.noteWorkerOutput('DLNE_NEEDS: vcredist\n', 'stdout');
  wm.noteWorkerOutput('DLNE_NEEDS: vcredist\n', 'stdout');
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(stub.state.errorMessages.length, 1);
  assert.equal(stub.state.errorMessages[0].message, c.VCREDIST_MESSAGE);
  assert.deepEqual(stub.state.errorMessages[0].items, ['Download']);
  assert.deepEqual(stub.state.opened, ['https://aka.ms/vs/17/release/vc_redist.x64.exe']);
  assert.equal(wm.currentState, 'error');
  wm.dispose();
});

test('declining the notice opens nothing, and ordinary output shows none', async () => {
  const wm = manager();
  wm.noteWorkerOutput('Heartbeat OK\n', 'stdout');
  assert.equal(stub.state.errorMessages.length, 0);
  wm.noteWorkerOutput('DLNE_NEEDS: vcredist\n', 'stdout');
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(stub.state.opened, []);
  wm.dispose();
});

test('the worker stopping afterwards keeps the install-the-runtime status', () => {
  const wm = manager();
  wm.noteWorkerOutput('DLNE_NEEDS: vcredist\n', 'stdout');
  wm.onWorkerExit(1);
  assert.equal(wm.currentState, 'error');
  assert.match(wm.statusMessage, /Visual C\+\+/);
  wm.dispose();
});
