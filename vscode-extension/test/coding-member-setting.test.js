'use strict';
/**
 * The owner's off switch for the coding-member job: `dlne.worker.allowCodingMember` in the editor,
 * `CODING_MEMBER_ALLOWED=true|false` in the engine's config.txt (engine/config.py: only the literal
 * `true`, any casing, is true; the worker reads it again at every heartbeat).
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const stub = require('./helpers/vscode-stub');
const { WorkerManager } = require('../dist/worker/workerManager');
const { setConfigValue, CODING_MEMBER_ALLOWED_KEY } = require('../dist/worker/contribution');

const HOME = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-member-setting-home-'));
process.env.HOME = HOME;
process.env.USERPROFILE = HOME;

// The constructor's background start-up (probe the GPU, poll the worker, resume) would race these tests.
WorkerManager.prototype.init = async function init() {};

const KEY = 'CODING_MEMBER_ALLOWED';
const SETTING = 'dlne.worker.allowCodingMember';

/** A fresh engine folder with a dlne_server.py in the place the extension looks (<home>/.dlne/runtime). */
function engine(configText) {
  const root = path.join(HOME, '.dlne', 'runtime');
  fs.rmSync(root, { recursive: true, force: true });
  fs.mkdirSync(path.join(root, 'config'), { recursive: true });
  fs.writeFileSync(path.join(root, 'dlne_server.py'), '');
  if (configText !== undefined) fs.writeFileSync(path.join(root, 'config', 'config.txt'), configText);
  return { root, config: path.join(root, 'config', 'config.txt'), read: () => fs.readFileSync(path.join(root, 'config', 'config.txt'), 'utf8') };
}

function manager(settings = {}) {
  stub.reset();
  Object.assign(stub.state.settings, settings);
  const wm = new WorkerManager({ subscriptions: [], globalState: undefined });
  wm.currentState = 'idle';
  return wm;
}

// ---- package.json -------------------------------------------------------------------------

test('the setting is a boolean, on by default, application-scoped so a repository cannot change it', () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'package.json'), 'utf8'));
  const setting = manifest.contributes.configuration.properties[SETTING];
  assert.equal(setting.type, 'boolean');
  assert.equal(setting.default, true);
  assert.equal(setting.scope, 'application');
  assert.equal(
    setting.description,
    "Let the swarm give this GPU the coding-member job (it answers other people's coding requests with a whole model). Off: the GPU only joins the shared pool."
  );
});

// ---- the config.txt edit ------------------------------------------------------------------

test('an existing key is changed in place, whatever its casing, and everything else is left alone', () => {
  const before = '# my node\nCOORDINATOR_URL=https://x\n\n  coding_member_allowed = TRUE  \nMAX_RAM_GB=8\n';
  assert.equal(
    setConfigValue(before, KEY, 'false'),
    '# my node\nCOORDINATOR_URL=https://x\n\ncoding_member_allowed=false\nMAX_RAM_GB=8\n'
  );
});

test('a missing key is appended; a commented-out one is not touched', () => {
  assert.equal(setConfigValue('A=1\n# CODING_MEMBER_ALLOWED=false\n', KEY, 'true'), 'A=1\n# CODING_MEMBER_ALLOWED=false\nCODING_MEMBER_ALLOWED=true\n');
  assert.equal(setConfigValue('', KEY, 'false'), 'CODING_MEMBER_ALLOWED=false\n');
  assert.equal(setConfigValue('A=1', KEY, 'false'), 'A=1\nCODING_MEMBER_ALLOWED=false\n', 'a file with no final newline');
});

test('a duplicate further down is dropped, because the engine reads the last one', () => {
  assert.equal(setConfigValue('CODING_MEMBER_ALLOWED=true\nA=1\nCoding_Member_Allowed=true\n', KEY, 'false'), 'CODING_MEMBER_ALLOWED=false\nA=1\n');
});

test('a config saved on Windows keeps its CRLF line endings, and a byte-order mark is removed', () => {
  assert.equal(setConfigValue('A=1\r\nCODING_MEMBER_ALLOWED=true\r\nB=2\r\n', KEY, 'false'), 'A=1\r\nCODING_MEMBER_ALLOWED=false\r\nB=2\r\n');
  assert.equal(setConfigValue('﻿NETWORK_KEY=abc\r\n', KEY, 'true'), 'NETWORK_KEY=abc\r\nCODING_MEMBER_ALLOWED=true\r\n');
  assert.equal(CODING_MEMBER_ALLOWED_KEY, KEY);
});

test('a value containing an equals sign in another key is not mistaken for this key', () => {
  const before = 'NOTE=CODING_MEMBER_ALLOWED=false\n';
  assert.equal(setConfigValue(before, KEY, 'true'), before + 'CODING_MEMBER_ALLOWED=true\n');
});

// ---- the extension writes it ---------------------------------------------------------------

test('the default writes true; turning the setting off writes false; the rest of the file survives', () => {
  const e = engine('COORDINATOR_URL=https://x\nNETWORK_KEY=abc\n');
  const wm = manager();
  wm.syncCodingMemberSetting(e.root);
  assert.equal(e.read(), 'COORDINATOR_URL=https://x\nNETWORK_KEY=abc\nCODING_MEMBER_ALLOWED=true\n');

  stub.state.settings[SETTING] = false;
  wm.syncCodingMemberSetting(e.root);
  assert.equal(e.read(), 'COORDINATOR_URL=https://x\nNETWORK_KEY=abc\nCODING_MEMBER_ALLOWED=false\n');
  assert.deepEqual(fs.readdirSync(path.dirname(e.config)).sort(), ['config.txt'], 'no scratch file is left behind');
  wm.dispose();
});

test('anything but a real true counts as off (a hand-edited settings.json cannot switch it on by accident)', () => {
  const e = engine('');
  for (const odd of ['true', 'yes', 1, null]) {
    const wm = manager({ [SETTING]: odd });
    wm.syncCodingMemberSetting(e.root);
    assert.match(e.read(), /^CODING_MEMBER_ALLOWED=false\n$/, `${JSON.stringify(odd)} is off`);
    wm.dispose();
  }
});

test('a file that is already right is not rewritten', () => {
  const e = engine('CODING_MEMBER_ALLOWED=true\n');
  const past = new Date(Date.now() - 3600_000);
  fs.utimesSync(e.config, past, past);
  const wm = manager();
  wm.syncCodingMemberSetting(e.root);
  assert.equal(Math.round(fs.statSync(e.config).mtimeMs / 1000), Math.round(past.getTime() / 1000));
  wm.dispose();
});

test('no engine installed yet: nothing is written and nothing throws', () => {
  fs.rmSync(path.join(HOME, '.dlne'), { recursive: true, force: true });
  const wm = manager();
  assert.doesNotThrow(() => wm.syncCodingMemberSetting());
  assert.equal(fs.existsSync(path.join(HOME, '.dlne')), false);
  wm.dispose();
});

test('an unwritable config is logged, not thrown', () => {
  const e = engine('');
  fs.rmSync(e.config);
  fs.mkdirSync(e.config); // a directory where the file should be
  const wm = manager();
  assert.doesNotThrow(() => wm.syncCodingMemberSetting(e.root));
  assert.match(stub.state.channels[0].text(), /Could not write CODING_MEMBER_ALLOWED/);
  wm.dispose();
});

test('changing the setting while the worker runs writes it at once (the worker re-reads config.txt)', () => {
  const e = engine('A=1\n');
  let listener;
  stub.vscode.workspace.onDidChangeConfiguration = (fn) => { listener = fn; return { dispose() { listener = undefined; } }; };
  try {
    const wm = manager();
    assert.equal(typeof listener, 'function');

    stub.state.settings[SETTING] = false;
    listener({ affectsConfiguration: (key) => key === 'dlne.serverUrl' });
    assert.equal(e.read(), 'A=1\n', 'an unrelated setting changes nothing');

    listener({ affectsConfiguration: (key) => key === SETTING });
    assert.equal(e.read(), 'A=1\nCODING_MEMBER_ALLOWED=false\n');

    wm.dispose();
    assert.equal(listener, undefined, 'the listener is released with the manager');
  } finally {
    delete stub.vscode.workspace.onDidChangeConfiguration;
  }
});

test('startWorker writes the switch before it launches the worker', async () => {
  const e = engine('NETWORK_KEY=abc\n');
  // A "python" that is node: it copies the config it finds at launch to a file this test reads.
  const seen = path.join(HOME, 'seen-config.txt');
  fs.rmSync(seen, { force: true });
  fs.writeFileSync(
    path.join(e.root, 'dlne_server.py'),
    `require('fs').writeFileSync(${JSON.stringify(seen)}, require('fs').readFileSync(${JSON.stringify(e.config)}, 'utf8'));`
  );
  const wm = manager({
    [SETTING]: false,
    'dlne.worker.pythonPath': process.execPath,
    'dlne.worker.device': 'gpu',
    'dlne.autoUpdate': false,
    'dlne.serverUrl': 'http://127.0.0.1:9',
  });
  wm.localGpu = { hasGpu: true, name: 'RTX 4060', vramTotalGb: 8, api: 'vulkan' };
  // This machine may well run a real worker (pgrep, systemd, the port): these tests describe their own.
  wm.isSystemdActive = () => false;
  wm.isProcessActive = () => false;
  wm.isPortListening = async () => false;
  wm.ensureRpcBinaries = async () => {};
  try {
    assert.equal(await wm.startWorker(), true);
    const deadline = Date.now() + 15000;
    while (!fs.existsSync(seen) && Date.now() < deadline) await new Promise((r) => setTimeout(r, 50));
    assert.equal(fs.readFileSync(seen, 'utf8'), 'NETWORK_KEY=abc\nCODING_MEMBER_ALLOWED=false\n');
  } finally {
    if (wm.workerProcess) wm.workerProcess.kill();
    wm.dispose();
  }
});
