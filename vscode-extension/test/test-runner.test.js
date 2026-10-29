'use strict';
/**
 * `npm test` runs the tests through scripts/run-tests.js so that they leave nothing in the system's temp folder: /tmp is
 * memory on the maintainer's laptop, and over a thousand `dlne-*` folders piled up there from runs that never cleaned up.
 * The script gives the run one folder of its own as its temp folder (TMPDIR, TEMP and TMP), removes it when the run ends,
 * whatever its result, and keeps the tests' exit code. These tests run it over small test folders of their own.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const { ROOT_PREFIX, MARKER } = require('../scripts/run-tests');

const EXT_ROOT = path.join(__dirname, '..');
const RUNNER = path.join(EXT_ROOT, 'scripts', 'run-tests.js');

/** A temp folder of this file's own, removed when the file is done. */
const made = [];
function folder(prefix) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), prefix));
  made.push(dir);
  return dir;
}
test.after(() => {
  for (const dir of made) fs.rmSync(dir, { recursive: true, force: true });
});

/**
 * A test file for the script to run: one test, called `name`, that makes a temp folder with a file in it and records what
 * the system said the temp folder was. `then` is more code for the test to run.
 */
const fixture = (name, then = '') => `
'use strict';
const test = require('node:test');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
test(${JSON.stringify(name)}, () => {
  const made = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-fixture-'));
  fs.writeFileSync(path.join(made, 'file.txt'), 'x');
  fs.appendFileSync(process.env.RUNNER_TEST_RECORD, JSON.stringify({
    name: ${JSON.stringify(name)}, tmpdir: os.tmpdir(), TMPDIR: process.env.TMPDIR, TEMP: process.env.TEMP, TMP: process.env.TMP, made,
  }) + '\\n');
  ${then}
});
`;

/** Runs the script over a folder holding `files`, with a temp folder of the test's own as the system's. */
function runOver(files, args = []) {
  const parent = folder('dlne-runner-parent-');
  const dir = folder('dlne-runner-fixture-');
  for (const [name, body] of Object.entries(files)) fs.writeFileSync(path.join(dir, name), body);
  const recordFile = path.join(folder('dlne-runner-records-'), 'record.jsonl');
  const result = spawnSync(process.execPath, [RUNNER, '--dir', dir, ...args], {
    env: { ...process.env, TMPDIR: parent, TEMP: parent, TMP: parent, RUNNER_TEST_RECORD: recordFile },
    encoding: 'utf8',
    timeout: 60000,
  });
  const records = fs.existsSync(recordFile) ? fs.readFileSync(recordFile, 'utf8').trim().split('\n').map((line) => JSON.parse(line)) : [];
  return { result, parent, records };
}

test('the tests\' temp folder is a folder of the run\'s own, and nothing is left in the system\'s when the run ends', () => {
  const { result, parent, records } = runOver({ 'one.test.js': fixture('one') });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.equal(records.length, 1, 'the test ran');
  const [record] = records;
  assert.equal(path.dirname(record.tmpdir), parent, 'inside the system temp folder');
  assert.ok(path.basename(record.tmpdir).startsWith(ROOT_PREFIX), record.tmpdir);
  for (const name of ['TMPDIR', 'TEMP', 'TMP']) assert.equal(record[name], record.tmpdir, `${name} is the run's folder`);
  assert.ok(record.made.startsWith(record.tmpdir + path.sep), 'what the test made is inside it');
  assert.deepEqual(fs.readdirSync(parent), [], 'the folder the test made, and the run\'s own, are gone');
});

test('a failing run leaves nothing behind either, and its exit code is the tests\' own', () => {
  const { result, parent, records } = runOver({ 'one.test.js': fixture('fails', "require('node:assert').fail('this test fails');") });
  assert.equal(result.status, 1, result.stdout + result.stderr);
  assert.equal(records.length, 1);
  assert.deepEqual(fs.readdirSync(parent), []);
});

test('a test file that ends its process in the middle is a failure too, with the folder removed', () => {
  const { result, parent, records } = runOver({ 'crash.test.js': fixture('crashes', 'process.exit(3);') });
  assert.notEqual(result.status, 0);
  assert.equal(records.length, 1);
  assert.deepEqual(fs.readdirSync(parent), []);
});

test('every test file of the folder runs, in one run, and the arguments after the script go on to node --test', () => {
  const files = { 'a.test.js': fixture('first'), 'b.test.js': fixture('second'), 'notes.txt': 'not a test' };
  const all = runOver(files);
  assert.equal(all.result.status, 0, all.result.stdout + all.result.stderr);
  assert.deepEqual(all.records.map((r) => r.name).sort(), ['first', 'second']);
  assert.equal(new Set(all.records.map((r) => r.tmpdir)).size, 1, 'one temp folder for the whole run');
  assert.deepEqual(fs.readdirSync(all.parent), []);

  const some = runOver(files, ['--test-name-pattern=second']);
  assert.equal(some.result.status, 0, some.result.stdout + some.result.stderr);
  assert.deepEqual(some.records.map((r) => r.name), ['second']);
});

test('with no test files there is nothing to run: a failure that says so, and still nothing left behind', () => {
  const { result, parent } = runOver({ 'notes.txt': 'not a test' });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /no \*\.test\.js files/);
  assert.deepEqual(fs.readdirSync(parent), []);
});

test('the folder of a run that was killed outright is swept by the next run, and nothing else in the temp folder is touched', () => {
  const parent = folder('dlne-runner-parent-');
  const dir = folder('dlne-runner-fixture-');
  fs.writeFileSync(path.join(dir, 'one.test.js'), fixture('one'));

  const gone = spawnSync(process.execPath, ['-e', '']).pid; // the id of a process that has ended
  const leftover = path.join(parent, `${ROOT_PREFIX}dead01`);
  fs.mkdirSync(leftover);
  fs.writeFileSync(path.join(leftover, MARKER), String(gone));
  fs.writeFileSync(path.join(leftover, 'junk.txt'), 'x');

  const running = path.join(parent, `${ROOT_PREFIX}alive1`);
  fs.mkdirSync(running);
  fs.writeFileSync(path.join(running, MARKER), String(process.pid)); // this test is the process that "made" it, and it is running

  const unmarked = path.join(parent, `${ROOT_PREFIX}nomark`);
  fs.mkdirSync(unmarked);
  const stranger = path.join(parent, 'dlne-something-else');
  fs.mkdirSync(stranger);
  fs.writeFileSync(path.join(stranger, MARKER), String(gone));

  const result = spawnSync(process.execPath, [RUNNER, '--dir', dir], {
    env: { ...process.env, TMPDIR: parent, TEMP: parent, TMP: parent, RUNNER_TEST_RECORD: path.join(folder('dlne-runner-records-'), 'r.jsonl') },
    encoding: 'utf8',
    timeout: 60000,
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.equal(fs.existsSync(leftover), false, 'the folder of a run that is gone is removed');
  assert.equal(fs.existsSync(running), true, 'one whose run is still going is not');
  assert.equal(fs.existsSync(unmarked), true, 'a folder with no marker is not ours');
  assert.equal(fs.existsSync(stranger), true, 'and neither is a folder with another name');
});

test('npm test goes through the script, after the compile', () => {
  const pkg = JSON.parse(fs.readFileSync(path.join(EXT_ROOT, 'package.json'), 'utf8'));
  assert.equal(pkg.scripts.test, 'tsc -p ./ && node scripts/run-tests.js');
});
