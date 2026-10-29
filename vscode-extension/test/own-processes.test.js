'use strict';
/**
 * A test's clean-up kills only what the test started (2026-10-28 14:21: a `pgrep -f` marker matched 16 of VS Code's own
 * helper processes, and the clean-up SIGKILLed every window of the editor running the tests).
 * - test/helpers/own-processes.js matches `sleep <marker>` literally, never as a pattern that other command lines fit;
 * - scripts/run-tests.js runs the tests in a PID namespace of their own on Linux, where nothing else can be seen.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const childProcess = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { ownProcesses, killOwn } = require('./helpers/own-processes');
const { ISOLATED } = require('../scripts/run-tests');

const posix = { skip: process.platform === 'win32' && 'POSIX processes' };
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

test('the marker is the sleep time itself: a command line that only looks like it (VS Code\'s long digit strings) is never matched', posix, async () => {
  const marker = `59.${process.pid % 1000}77`;
  const lookalike = marker.replace('.', '7');                       // what "59.N77" as a regular expression also matched
  const own = childProcess.spawn('sleep', [marker], { stdio: 'ignore' });
  const decoys = [
    childProcess.spawn(process.execPath, ['-e', 'setTimeout(() => {}, 30000)', '--', `--field-trial-handle=3,i,8${lookalike}4,262144`],
      { stdio: 'ignore' }),
    childProcess.spawn(process.execPath, ['-e', 'setTimeout(() => {}, 30000)', '--', `--launch-time-ticks=${marker}`],
      { stdio: 'ignore' }),
    childProcess.spawn('sleep', [`${marker}5`], { stdio: 'ignore' }),
  ];
  try {
    await sleep(300);
    assert.deepEqual(ownProcesses(marker), [String(own.pid)]);
    killOwn(marker);
    await sleep(200);
    assert.notEqual(own.exitCode ?? own.signalCode, null, 'the test\'s own sleep is ended');
    for (const decoy of decoys) assert.equal(decoy.exitCode ?? decoy.signalCode, null, 'and nothing that only looked like it');
  } finally {
    for (const child of [own, ...decoys]) child.kill('SIGKILL');
  }
});

test('the shell that runs the sleep counts as the test\'s own as well, and a marker that is not a sleep time is refused', posix, async () => {
  const marker = `57.${process.pid % 1000}31`;
  const shell = childProcess.spawn('/bin/sh', ['-c', `(sleep ${marker}; echo late) & sleep ${marker}; wait`], { stdio: 'ignore' });
  try {
    for (let i = 0; i < 40 && ownProcesses(marker).length < 3; i++) await sleep(50);
    assert.ok(ownProcesses(marker).length >= 3, 'the shell, its subshell and the sleeps');
    assert.throws(() => ownProcesses('code'), /a marker is a sleep time/);
    assert.throws(() => ownProcesses(''), /a marker is a sleep time/);
  } finally {
    killOwn(marker);
    shell.kill('SIGKILL');
  }
});

test('npm test runs the tests in a PID namespace of their own on Linux, where only the run\'s processes can be seen', {
  skip: process.platform !== 'linux' && 'Linux namespaces',
}, () => {
  const usable = childProcess.spawnSync('unshare', ['--user', '--map-current-user', '--pid', '--fork', '--mount-proc', 'true']).status === 0;
  if (!usable) return; // the runner says so and runs the tests as before
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-isolation-'));
  try {
    fs.writeFileSync(path.join(dir, 'look.test.js'), [
      "const test = require('node:test');",
      "test('look', () => {",
      "  const seen = require('fs').readdirSync('/proc').filter((name) => /^\\d+$/.test(name)).length;",
      `  console.log(JSON.stringify({ isolated: process.env.${ISOLATED}, seen }));`,
      '});',
    ].join('\n'));
    const env = { ...process.env };
    delete env[ISOLATED];
    delete env.NODE_TEST_CONTEXT;
    const run = childProcess.spawnSync(process.execPath, [path.join(__dirname, '..', 'scripts', 'run-tests.js'), '--dir', dir],
      { env, encoding: 'utf8' });
    assert.equal(run.status, 0, run.stderr);
    const report = JSON.parse(/\{"isolated".*\}/.exec(run.stdout)[0]);
    assert.equal(report.isolated, '1');
    assert.ok(report.seen < 20, `the test saw ${report.seen} processes: only the run's own`);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
