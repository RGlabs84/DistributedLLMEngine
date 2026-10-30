'use strict';
/**
 * VS Code first (2.7.0, docs/release-2.7-contract.md section 4b), the editor's half: ~/.dlne/editor.json is written when
 * the extension activates, refreshed every 30 s, and removed when it deactivates, but only by the window whose pid is in
 * it. Runs on any machine: the home folder is a temporary one, and the clock and timers are the test's.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { EditorPresence, PRESENCE_REFRESH_MS, parsePresence, presenceFile } = require('../dist/worker/editorPresence');

function rig({ pid = 4242, version = '1.6.0' } = {}) {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-presence-home-'));
  const clock = { now: 1_800_000_000_000 };
  const timers = [];
  const env = {
    home: () => home,
    pid,
    now: () => clock.now,
    setInterval: (fn, ms) => { const timer = { fn, ms, cleared: false }; timers.push(timer); return timer; },
    clearInterval: (timer) => { timer.cleared = true; },
  };
  const presence = new EditorPresence(version, env);
  const file = presenceFile(home);
  const read = () => JSON.parse(fs.readFileSync(file, 'utf8'));
  return { home, clock, timers, env, presence, file, read };
}

test('the file is ~/.dlne/editor.json', () => {
  assert.equal(presenceFile('/home/ann'), path.join('/home/ann', '.dlne', 'editor.json'));
});

test('activation writes pid, updated (epoch seconds) and version, readable only by its owner', () => {
  const t = rig();
  t.presence.start();
  assert.deepEqual(t.read(), { pid: 4242, updated: 1_800_000_000, version: '1.6.0' });
  if (process.platform !== 'win32') assert.equal(fs.statSync(t.file).mode & 0o777, 0o600);
  assert.deepEqual(fs.readdirSync(path.dirname(t.file)), ['editor.json'], 'no scratch file is left behind');
  t.presence.stop();
});

test('it is refreshed every 30 seconds, with the new time', () => {
  const t = rig();
  t.presence.start();
  assert.equal(t.timers.length, 1);
  assert.equal(t.timers[0].ms, 30_000);
  assert.equal(PRESENCE_REFRESH_MS, 30_000);
  t.clock.now += 30_000;
  t.timers[0].fn();
  assert.equal(t.read().updated, 1_800_000_030);
  t.clock.now += 30_000;
  t.timers[0].fn();
  assert.equal(t.read().updated, 1_800_000_060);
  // Starting again does not stack a second timer.
  t.presence.start();
  assert.equal(t.timers.length, 1);
  t.presence.stop();
});

test('a refresh puts the file back when something removed it', () => {
  const t = rig();
  t.presence.start();
  fs.unlinkSync(t.file);
  t.timers[0].fn();
  assert.equal(t.read().pid, 4242);
  t.presence.stop();
});

test('deactivation stops the refresh and removes the file', () => {
  const t = rig();
  t.presence.start();
  t.presence.stop();
  assert.ok(t.timers[0].cleared);
  assert.equal(fs.existsSync(t.file), false);
  t.presence.stop();   // twice is harmless
});

test('several windows: each refreshes, and a window only removes the file when its own pid is in it', () => {
  const a = rig({ pid: 111 });
  const home = a.home;
  const b = new EditorPresence('1.6.0', { ...a.env, pid: 222 });
  a.presence.start();
  b.start();
  assert.equal(a.read().pid, 222, 'the last writer is in the file');
  a.presence.stop();
  assert.equal(fs.existsSync(a.file), true, 'window A closing leaves window B\'s file alone');
  b.stop();
  assert.equal(fs.existsSync(path.join(home, '.dlne', 'editor.json')), false);
});

test('a home folder that cannot be written does not throw', () => {
  const t = rig();
  fs.writeFileSync(path.join(t.home, '.dlne'), 'a file where the folder should be');
  assert.doesNotThrow(() => t.presence.start());
  assert.doesNotThrow(() => t.presence.stop());
});

test('parsePresence reads the file and refuses anything else', () => {
  assert.deepEqual(parsePresence('{"pid": 7, "updated": 5, "version": "1.6.0"}'), { pid: 7, updated: 5, version: '1.6.0' });
  assert.equal(parsePresence(null), null);
  assert.equal(parsePresence('nope'), null);
  assert.equal(parsePresence('{"pid": "7", "updated": 5}'), null);
  assert.equal(parsePresence('{"pid": 0, "updated": 5}'), null);
  assert.equal(parsePresence('{"pid": 7}'), null);
});
