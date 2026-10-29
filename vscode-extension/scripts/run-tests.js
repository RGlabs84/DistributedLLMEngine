#!/usr/bin/env node
// `npm test` runs the compiled tests through this script (package.json "test"). Every temp folder the tests make (a HOME
// of their own, a workspace, a saved task) goes into one folder of the run's own, which is removed when the run ends,
// whatever its result. /tmp is memory on the maintainer's laptop, and over a thousand `dlne-*` folders piled up there
// from runs that never cleaned up after themselves. The exit code is the tests' own.
//
//   node scripts/run-tests.js [node --test arguments]      e.g. npm test -- --test-name-pattern=loop
//   node scripts/run-tests.js --dir <folder> ...           the *.test.js files of another folder (the runner's own test)
'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn, spawnSync } = require('child_process');

/** What the run's folder is called inside the system temp folder. */
const ROOT_PREFIX = 'dlne-ext-tests-';
/** Written into it, with the id of the process that made it: a later run knows the folder is this script's, and whose. */
const MARKER = '.dlne-test-run';

/** Whether a process with this id is running (EPERM means it is, and belongs to someone else). */
function alive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    return err.code === 'EPERM';
  }
}

function removeFolder(dir) {
  fs.rmSync(dir, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
}

/**
 * Removes what an earlier run left when it could not clean up after itself (killed outright, a power cut): only a folder
 * with this script's prefix and its marker, made by a process that is gone. Anything else in `parent` is not ours.
 */
function sweepOldRuns(parent) {
  let names;
  try {
    names = fs.readdirSync(parent);
  } catch {
    return;
  }
  for (const name of names) {
    if (!name.startsWith(ROOT_PREFIX)) continue;
    const dir = path.join(parent, name);
    try {
      const pid = Number.parseInt(fs.readFileSync(path.join(dir, MARKER), 'utf8'), 10);
      if (Number.isInteger(pid) && pid > 0 && !alive(pid)) removeFolder(dir);
    } catch {
      // no marker, or a folder that cannot be read: not ours to remove
    }
  }
}

/**
 * The environment of the tests: the temp folder is `root` for every way an operating system names it. A run started from
 * inside a test (the runner's own tests do) must not inherit node:test's mark that it is one, or it would skip every file.
 */
function testEnvironment(root) {
  const env = {};
  for (const [key, value] of Object.entries(process.env)) {
    if (!['TMPDIR', 'TEMP', 'TMP', 'NODE_TEST_CONTEXT'].includes(key.toUpperCase())) env[key] = value;
  }
  return { ...env, TMPDIR: root, TEMP: root, TMP: root };
}

/** unshare's flags for a user and PID namespace of the tests' own, as this user, with a /proc that shows only them. */
const UNSHARE = ['--user', '--map-current-user', '--pid', '--fork', '--mount-proc', '--kill-child'];
/** Set inside: a run started from a test (the runner's own tests) is in the namespace already. */
const ISOLATED = 'DLNE_TESTS_ISOLATED';

/**
 * On Linux the tests run in a PID namespace of their own, where the only processes they can see, find by name or signal
 * are their own. On 2026-10-28 at 14:21 a test's clean-up matched 16 of VS Code's helper processes with `pgrep -f` and
 * SIGKILLed them, and every window of the editor that ran the tests went down; in here they cannot even be seen. Whatever
 * a test leaves running also ends with the run: everything in the namespace does when its first process exits. Where
 * unshare or user namespaces are not available the tests run as before, with a line that says so
 * (test/helpers/own-processes.js still finds only the tests' own processes).
 */
function isolation() {
  if (process.platform !== 'linux' || process.env[ISOLATED] === '1') return null;
  if (spawnSync('unshare', [...UNSHARE, 'true'], { stdio: 'ignore' }).status === 0) return UNSHARE;
  console.error('run-tests: no PID namespace for the tests here (unshare); they run beside every other process.');
  return null;
}

/** Runs `node --test` over the test files of `dir`; resolves with its exit code. */
function runTests(dir, forwarded, root) {
  const files = fs.readdirSync(dir).filter((name) => name.endsWith('.test.js')).sort().map((name) => path.join(dir, name));
  if (files.length === 0) {
    console.error(`run-tests: no *.test.js files in ${dir}`);
    return Promise.resolve(1);
  }
  const nodeArgs = ['--test', ...forwarded, ...files];
  const flags = isolation();
  return new Promise((resolve) => {
    // Inside the namespace the first process is a shell that runs node and waits (never `exec`): it is the "init" of the
    // namespace, which every orphan is handed to, and a shell collects them. With node itself as first process a killed
    // background job stayed a zombie for the whole run, and a zombie still counts as a member of its process group, so
    // "nobody is left in the group" could never be seen (agent-run-control.test.js).
    const inside = [process.execPath, ...nodeArgs];
    const child = spawn(flags ? 'unshare' : process.execPath, flags ? [...flags, 'sh', '-c', '"$0" "$@"; exit $?', ...inside] : nodeArgs, {
      cwd: path.join(__dirname, '..'),
      env: { ...testEnvironment(root), ...(flags ? { [ISOLATED]: '1' } : {}) },
      stdio: 'inherit',
    });
    // A Stop (Ctrl+C reaches the tests too, as one process group) or a kill of this script reaches the tests as well, and
    // the folder is still removed once they have ended.
    const forward = (signal) => () => child.kill(signal);
    for (const signal of ['SIGINT', 'SIGTERM', 'SIGHUP']) process.on(signal, forward(signal));
    child.on('error', (err) => {
      console.error(`run-tests: could not start the tests: ${err.message}`);
      resolve(1);
    });
    child.on('close', (code, signal) => resolve(code ?? 128 + (os.constants.signals[signal] ?? 1)));
  });
}

async function main(argv) {
  let dir = path.join(__dirname, '..', 'test');
  const forwarded = [];
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--dir') dir = path.resolve(argv[++i] ?? '');
    else forwarded.push(argv[i]);
  }
  const parent = os.tmpdir();
  sweepOldRuns(parent);
  const root = fs.mkdtempSync(path.join(parent, ROOT_PREFIX));
  fs.writeFileSync(path.join(root, MARKER), String(process.pid));
  try {
    return await runTests(dir, forwarded, root);
  } finally {
    try {
      removeFolder(root);
    } catch (err) {
      console.error(`run-tests: could not remove ${root}: ${err.message}`);
    }
  }
}

if (require.main === module) {
  main(process.argv.slice(2)).then(
    (code) => process.exit(code),
    (err) => {
      console.error(`run-tests: ${err.stack || err}`);
      process.exit(1);
    }
  );
}

module.exports = { ROOT_PREFIX, MARKER, ISOLATED, sweepOldRuns };
