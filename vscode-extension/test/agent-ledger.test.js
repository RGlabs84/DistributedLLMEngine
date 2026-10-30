'use strict';
/**
 * The ledger (docs/agent-memory.md, section 5.2): an exact record of what the agent read, changed,
 * ran, searched, listed, checked and remembered, and of what was declined, refused or failed.
 * Every tool result below is written in the tools' real wording (tools.ts, privacy/exclude.ts),
 * so a change to that wording fails here instead of silently blinding the ledger.
 */
const test = require('node:test');
const assert = require('node:assert/strict');

require('./helpers/vscode-stub');
const { Ledger, LEDGER_MAX_CHARS } = require('../dist/agent/ledger');
const { ToolRegistry } = require('../dist/agent/tools');

// --- Helpers: tool results as the tools return them ---

const numbered = (from, to) => Array.from({ length: to - from + 1 }, (_, i) => `${from + i}: line ${from + i}`).join('\n');
const ok = (name, output, extra = {}) => ({ name, output, ...extra });
const err = (name, output) => ({ name, output, is_error: true });
const diff = (additions, deletions) => ({ additions, deletions });

function read(ledger, args, output) {
  ledger.record('read_file', args, ok('read_file', output));
}

/** What `capOutput` does to an output over 8000 characters (it is private, so run the real one). */
function capped(output) {
  return ToolRegistry.capOutput({ name: 'x', output }).output;
}

test('LEDGER_MAX_CHARS is 5000', () => {
  assert.equal(LEDGER_MAX_CHARS, 5000);
});

test('an empty ledger is empty and renders as nothing', () => {
  const l = new Ledger();
  assert.equal(l.isEmpty(), true);
  assert.equal(l.render(), '');
});

test('one record makes the ledger non-empty', () => {
  const l = new Ledger();
  l.record('list_directory', { path: 'src' }, ok('list_directory', '[DIR] agent'));
  assert.equal(l.isEmpty(), false);
});

// --- read_file ---

test('read_file with a range records that range', () => {
  const l = new Ledger();
  read(l, { path: 'src/a.ts', start_line: 10, end_line: 20 }, numbered(10, 20));
  assert.equal(l.render(), 'Read:\n- src/a.ts lines 10-20');
});

test('read_file without a range is the whole file, with its line count from the last N: line', () => {
  const l = new Ledger();
  read(l, { path: 'src/b.ts' }, numbered(1, 212));
  assert.equal(l.render(), 'Read:\n- src/b.ts (whole file, 212 lines)');
});

test('a whole-file read ignores the redaction note after the numbered lines', () => {
  const l = new Ledger();
  read(l, { path: '.env.example' }, numbered(1, 3) + '\n\n[DLNE hid 1 secret value above as [REDACTED] for privacy. They still exist in the file; never write [REDACTED] into a file.]');
  assert.equal(l.render(), 'Read:\n- .env.example (whole file, 3 lines)');
});

test('a one-line file and a one-line range read naturally', () => {
  const l = new Ledger();
  read(l, { path: 'one.txt' }, '1: only');
  read(l, { path: 'src/c.ts', start_line: 7, end_line: 7 }, '7: x');
  assert.equal(l.render(), 'Read:\n- one.txt (whole file, 1 line)\n- src/c.ts line 7');
});

test('a range that runs past the end of the file records the lines that came back', () => {
  const l = new Ledger();
  read(l, { path: 'src/a.ts', start_line: 90, end_line: 500 }, numbered(90, 100));
  assert.equal(l.render(), 'Read:\n- src/a.ts lines 90-100');
});

test('only start_line: the range runs to the last line that came back', () => {
  const l = new Ledger();
  read(l, { path: 'src/a.ts', start_line: 95 }, numbered(95, 100));
  assert.equal(l.render(), 'Read:\n- src/a.ts lines 95-100');
});

test('a range past the end of the file (nothing came back) records nothing', () => {
  const l = new Ledger();
  read(l, { path: 'src/a.ts', start_line: 900, end_line: 950 }, '');
  assert.equal(l.isEmpty(), true);
});

test('read ranges for one path merge when they overlap or touch, and stay apart otherwise', () => {
  const l = new Ledger();
  read(l, { path: 'f.ts', start_line: 1, end_line: 50 }, numbered(1, 50));
  read(l, { path: 'f.ts', start_line: 300, end_line: 380 }, numbered(300, 380));
  read(l, { path: 'f.ts', start_line: 40, end_line: 120 }, numbered(40, 120)); // overlaps the first
  assert.equal(l.render(), 'Read:\n- f.ts lines 1-120, 300-380');
  read(l, { path: 'f.ts', start_line: 121, end_line: 130 }, numbered(121, 130)); // touches 1-120
  read(l, { path: 'f.ts', start_line: 381, end_line: 400 }, numbered(381, 400)); // touches 300-380
  assert.equal(l.render(), 'Read:\n- f.ts lines 1-130, 300-400');
  read(l, { path: 'f.ts', start_line: 131, end_line: 299 }, numbered(131, 299)); // bridges the gap
  assert.equal(l.render(), 'Read:\n- f.ts lines 1-400');
});

test('a range read after a whole-file read leaves it a whole file', () => {
  const l = new Ledger();
  read(l, { path: 'f.ts' }, numbered(1, 30));
  read(l, { path: 'f.ts', start_line: 5, end_line: 9 }, numbered(5, 9));
  assert.equal(l.render(), 'Read:\n- f.ts (whole file, 30 lines)');
});

test('reading a file again moves it to the newest place', () => {
  const l = new Ledger();
  read(l, { path: 'a', start_line: 1, end_line: 2 }, numbered(1, 2));
  read(l, { path: 'b', start_line: 1, end_line: 2 }, numbered(1, 2));
  read(l, { path: 'a', start_line: 5, end_line: 6 }, numbered(5, 6));
  assert.equal(l.render(), 'Read:\n- b lines 1-2\n- a lines 1-2, 5-6');
});

test('read_file: File not found is a failure, not a read', () => {
  const l = new Ledger();
  l.record('read_file', { path: 'src/nope.ts' }, err('read_file', 'File not found: src/nope.ts'));
  assert.equal(l.render(), 'Refused or failed:\n- read of src/nope.ts failed: file not found');
});

test('read_file: an excluded file is a refusal that names the reason', () => {
  const l = new Ledger();
  const notice = 'This file is excluded for privacy (a private file): .env. DLNE does not read it and nothing from it is ' +
    'sent to the model. Do not retry; if you need something from it, ask the user to paste the part that is not secret.';
  l.record('read_file', { path: '.env' }, err('read_file', notice));
  assert.equal(l.render(), 'Refused or failed:\n- read of .env refused: excluded for privacy (a private file)');
});

// What capOutput does to a long read, checked on the real function.

test('capOutput keeps the head and the tail, and the last numbered line of the tail is the true last line', () => {
  const full = Array.from({ length: 1000 }, (_, i) => `${i + 1}: some code on line ${i + 1}`).join('\n');
  const out = capped(full);
  assert.ok(out.includes('[Output truncated:'));
  const lines = out.split('\n').filter((s) => /^\d+: /.test(s));
  assert.equal(lines[lines.length - 1], '1000: some code on line 1000');
});

test('a truncated whole-file read records only the lines the model saw, not "whole file"', () => {
  const full = Array.from({ length: 1000 }, (_, i) => `${i + 1}: some code on line ${i + 1}`).join('\n');
  const l = new Ledger();
  read(l, { path: 'big.ts' }, capped(full));
  const text = l.render();
  assert.match(text, /^Read:\n- big\.ts lines 1-\d+, \d+-1000$/);
  assert.doesNotMatch(text, /whole file/);
  // The two ranges must leave a gap: the middle was cut.
  const [, headEnd, tailStart] = /lines 1-(\d+), (\d+)-1000/.exec(text).map(Number);
  assert.ok(headEnd > 50 && tailStart > headEnd + 1);
});

test('a truncated read whose last line is huge adds no tail range rather than a wrong one', () => {
  const body = Array.from({ length: 300 }, (_, i) => `${i + 1}: code ${i + 1}`).join('\n') + '\n301: ' + 'x'.repeat(9000);
  const l = new Ledger();
  read(l, { path: 'min.js' }, capped(body));
  assert.match(l.render(), /^Read:\n- min\.js lines 1-\d+$/);
});

// --- write_file and edit_file_diff ---

test('write_file: created and updated, with +additions -deletions from fileDiff', () => {
  const l = new Ledger();
  l.record('write_file', { path: 'src/new.ts' }, ok('write_file', 'Successfully created file: src/new.ts (+80 lines, -0 lines)', { fileDiff: diff(80, 0) }));
  l.record('write_file', { path: 'src/old.ts' }, ok('write_file', 'Successfully updated file: src/old.ts (+5 lines, -9 lines)', { fileDiff: diff(5, 9) }));
  assert.equal(l.render(), 'Changed:\n- src/new.ts: created (+80)\n- src/old.ts: rewritten (+5 -9)');
});

test('write_file without a fileDiff takes the numbers from the output', () => {
  const l = new Ledger();
  l.record('write_file', { path: 'a.txt' }, ok('write_file', 'Successfully created file: a.txt (+3 lines, -0 lines)'));
  assert.equal(l.render(), 'Changed:\n- a.txt: created (+3)');
});

test('edit_file_diff: edits for one path add up', () => {
  const l = new Ledger();
  l.record('edit_file_diff', { path: 'src/a.ts' }, ok('edit_file_diff', 'Successfully patched src/a.ts (Applied)', { fileDiff: diff(10, 2) }));
  assert.equal(l.render(), 'Changed:\n- src/a.ts: 1 edit (+10 -2)');
  l.record('edit_file_diff', { path: 'src/a.ts' }, ok('edit_file_diff', 'Successfully patched src/a.ts (Applied)', { fileDiff: diff(4, 1) }));
  assert.equal(l.render(), 'Changed:\n- src/a.ts: 2 edits (+14 -3)');
});

test('a file created and then edited shows both', () => {
  const l = new Ledger();
  l.record('write_file', { path: 'n.ts' }, ok('write_file', 'Successfully created file: n.ts (+10 lines, -0 lines)', { fileDiff: diff(10, 0) }));
  l.record('edit_file_diff', { path: 'n.ts' }, ok('edit_file_diff', 'Successfully patched n.ts (Applied)', { fileDiff: diff(1, 1) }));
  l.record('write_file', { path: 'n.ts' }, ok('write_file', 'Successfully updated file: n.ts (+2 lines, -2 lines)', { fileDiff: diff(2, 2) }));
  assert.equal(l.render(), 'Changed:\n- n.ts: created, rewritten, 1 edit (+13 -3)');
});

test('edit_file_diff: a Diff Error is "edit to PATH did not apply"', () => {
  const l = new Ledger();
  l.record('edit_file_diff', { path: 'src/c.ts' }, err('edit_file_diff', 'Diff Error: search block not found'));
  assert.equal(l.render(), 'Refused or failed:\n- edit to src/c.ts did not apply');
});

test('edit_file_diff: File not found and an excluded file are not edits', () => {
  const l = new Ledger();
  l.record('edit_file_diff', { path: 'gone.ts' }, err('edit_file_diff', 'File not found: gone.ts'));
  l.record('edit_file_diff', { path: 'key.pem' }, err('edit_file_diff', 'This file is excluded for privacy (a key file): key.pem. DLNE does not read it.'));
  assert.equal(l.render(), 'Refused or failed:\n- edit to gone.ts failed: file not found\n- edit to key.pem refused: excluded for privacy (a key file)');
});

test('write_file: a rewrite that would put [REDACTED] in a file is a refusal', () => {
  const l = new Ledger();
  l.record('write_file', { path: 'config.ts' }, err('write_file', 'Refused: the text contains [REDACTED], which stands for a secret DLNE hid from you.'));
  assert.equal(l.render(), 'Refused or failed:\n- write to config.ts refused: it would have written [REDACTED] over real secrets');
});

test('write_file: a thrown error is a failure with its first line', () => {
  const l = new Ledger();
  l.record('write_file', { path: 'ro/x.ts' }, err('write_file', 'Tool execution failed: EACCES: permission denied, open \'ro/x.ts\''));
  assert.equal(l.render(), "Refused or failed:\n- write to ro/x.ts failed: EACCES: permission denied, open 'ro/x.ts'");
});

// --- run_terminal_command ---

test('run_terminal_command: success is exit 0 with no output line', () => {
  const l = new Ledger();
  l.record('run_terminal_command', { command: 'git status' }, ok('run_terminal_command', 'On branch main\nnothing to commit', { terminalCommand: 'git status' }));
  l.record('run_terminal_command', { command: 'true' }, ok('run_terminal_command', '(Command completed successfully with no output)'));
  assert.equal(l.render(), 'Ran:\n- `git status` (exit 0)\n- `true` (exit 0)');
});

test('run_terminal_command: a failure gives its exit code and the first non-empty output line', () => {
  const l = new Ledger();
  l.record('run_terminal_command', { command: 'npm test' }, ok('run_terminal_command', 'Command failed (exit code 1):\n\n   FAIL test/x.test.js\n  more\nCommand failed: npm test'));
  assert.equal(l.render(), 'Ran:\n- `npm test` (exit 1): FAIL test/x.test.js');
});

test('run_terminal_command: a long command is cut at 200 characters and its output line at 160', () => {
  const l = new Ledger();
  l.record('run_terminal_command', { command: 'echo ' + 'a'.repeat(300) }, ok('run_terminal_command', 'Command failed (exit code 2):\n' + 'b'.repeat(300)));
  assert.equal(l.render(), `Ran:\n- \`echo ${'a'.repeat(195)}…\` (exit 2): ${'b'.repeat(160)}…`);
});

test('run_terminal_command: a multi-line command is shown on one line', () => {
  const l = new Ledger();
  l.record('run_terminal_command', { command: 'cd x\nls   -la' }, ok('run_terminal_command', 'ok'));
  assert.equal(l.render(), 'Ran:\n- `cd x ls -la` (exit 0)');
});

test('run_terminal_command: a missing command is a failure', () => {
  const l = new Ledger();
  l.record('run_terminal_command', {}, err('run_terminal_command', 'Error: No command provided'));
  assert.equal(l.render(), 'Refused or failed:\n- command `` failed: Error: No command provided');
});

// --- search_codebase, list_directory, get_workspace_diagnostics ---

test('search_codebase: files found, one file, and no matches', () => {
  const l = new Ledger();
  l.record('search_codebase', { query: 'parseQueueInfo' }, ok('search_codebase', 'Matches found in 3 files:\na.ts\nb.ts\nc.ts'));
  l.record('search_codebase', { query: 'onlyHere' }, ok('search_codebase', 'Matches found in 1 files:\na.ts'));
  l.record('search_codebase', { query: 'nothing' }, ok('search_codebase', 'No matches found for "nothing"'));
  assert.equal(l.render(), 'Searched:\n- "parseQueueInfo": 3 files\n- "onlyHere": 1 file\n- "nothing": no matches');
});

test('search_codebase: searching the same query again replaces the older answer', () => {
  const l = new Ledger();
  l.record('search_codebase', { query: 'q' }, ok('search_codebase', 'No matches found for "q"'));
  l.record('search_codebase', { query: 'q' }, ok('search_codebase', 'Matches found in 2 files:\na\nb'));
  assert.equal(l.render(), 'Searched:\n- "q": 2 files');
});

test('list_directory: the path, listed on one line; a missing directory is a failure', () => {
  const l = new Ledger();
  l.record('list_directory', { path: 'src' }, ok('list_directory', '[DIR] agent\n      a.ts'));
  l.record('list_directory', { path: 'src/agent' }, ok('list_directory', '      ledger.ts'));
  l.record('list_directory', {}, ok('list_directory', '      README.md'));
  l.record('list_directory', { path: 'nope' }, err('list_directory', 'Directory not found: nope'));
  assert.equal(l.render(), 'Listed: src, src/agent, .\nRefused or failed:\n- list of nope failed: directory not found');
});

test('get_workspace_diagnostics: counts the ERROR and WARN lines, for a file or the workspace', () => {
  const l = new Ledger();
  l.record('get_workspace_diagnostics', {}, ok('get_workspace_diagnostics', '[ERROR] a.ts:1: x\n[ERROR] a.ts:5: y\n[WARN] b.ts:2: z'));
  l.record('get_workspace_diagnostics', { file_path: 'c.ts' }, ok('get_workspace_diagnostics', 'No errors or warnings found.'));
  l.record('get_workspace_diagnostics', { file_path: 'd.ts' }, ok('get_workspace_diagnostics', '[ERROR] d.ts:9: boom'));
  assert.equal(l.render(), 'Diagnostics:\n- workspace: 2 errors, 1 warning\n- c.ts: 0 errors, 0 warnings\n- d.ts: 1 error, 0 warnings');
});

test('get_workspace_diagnostics: checking again replaces the older answer; a cut output gives lower bounds', () => {
  const l = new Ledger();
  l.record('get_workspace_diagnostics', {}, ok('get_workspace_diagnostics', '[ERROR] a:1: x'));
  const many = Array.from({ length: 400 }, (_, i) => `[ERROR] src/file${i}.ts:${i}: something is wrong with this line number ${i}`).join('\n');
  l.record('get_workspace_diagnostics', {}, ok('get_workspace_diagnostics', capped(many)));
  const text = l.render();
  assert.match(text, /^Diagnostics:\n- workspace: \d+\+ errors, 0\+ warnings$/);
});

// --- remember ---

test('remember: both scopes are recorded when the output says it was saved', () => {
  const l = new Ledger();
  l.record('remember', { fact: 'a', scope: 'project' }, ok('remember', '📝 Remembered for this project: Tests run with `npm test` from vscode-extension/.'));
  l.record('remember', { fact: 'b', scope: 'user' }, ok('remember', '📝 Remembered for you: Prefers tabs.'));
  assert.equal(l.render(), 'Remembered:\n- project: Tests run with `npm test` from vscode-extension/.\n- user: Prefers tabs.');
});

test('remember: any other output is a failure, and "Already remembered" is neither', () => {
  const l = new Ledger();
  l.record('remember', { fact: 'x', scope: 'project' }, err('remember', 'Could not save: the project has no folder open.'));
  l.record('remember', { fact: 'x', scope: 'project' }, ok('remember', 'Already remembered: x'));
  l.record('remember', { fact: 'y', scope: 'user' }, ok('remember', 'something unexpected'));
  assert.equal(l.render(), 'Refused or failed:\n- remember did not save: Could not save: the project has no folder open.\n- remember did not save: something unexpected');
});

test('remember: the same fact twice is one entry', () => {
  const l = new Ledger();
  const out = '📝 Remembered for this project: Same.';
  l.record('remember', {}, ok('remember', out));
  l.record('remember', {}, ok('remember', out));
  assert.equal(l.render(), 'Remembered:\n- project: Same.');
});

// Memory 2 puts each fact's id in the message (the contract's section 4). The ledger keeps the fact, not the id: an
// update gives the fact a new id, so an id in the ledger would soon point at nothing.
test('remember (Memory 2): saved, updated and forgotten facts with ids are recorded as before, without the ids', () => {
  const l = new Ledger();
  l.record('remember', {}, ok('remember', '📝 Remembered for this project [a7f3]: Tests run with `npm test` from vscode-extension/.'));
  l.record('remember', {}, ok('remember', '📝 Remembered for you [0c9e1]: Prefers tabs.'));
  l.record('remember', {}, ok('remember', '📝 Updated for this project [a7f3] → [b812]: Tests run with `npm test`. → Tests run with `npm test` from vscode-extension/, after `npm run compile`.'));
  l.record('remember', {}, ok('remember', '📝 Forgot for you [d00d]: Likes long answers.'));
  assert.equal(l.render(), [
    'Remembered:',
    '- project: Tests run with `npm test` from vscode-extension/.',
    '- user: Prefers tabs.',
    '- project: Tests run with `npm test` from vscode-extension/, after `npm run compile`. (replacing: Tests run with `npm test`.)',
    '- user: forgot: Likes long answers.',
  ].join('\n'));
});

test('remember: an update or removal without ids (a task saved by 1.5.3) is recorded the same way', () => {
  const withIds = new Ledger();
  withIds.record('remember', {}, ok('remember', '📝 Updated for you [1234] → [5678]: Old. → New.'));
  withIds.record('remember', {}, ok('remember', '📝 Forgot for this project [9abc]: Gone.'));
  const without = new Ledger();
  without.record('remember', {}, ok('remember', '📝 Updated for you: Old. → New.'));
  without.record('remember', {}, ok('remember', '📝 Forgot for this project: Gone.'));
  assert.equal(without.render(), withIds.render());
  assert.equal(without.render(), 'Remembered:\n- user: New. (replacing: Old.)\n- project: forgot: Gone.');
});

test('remember: a fact saved in parts is one entry per part, and the sentence after them is not a fact', () => {
  const note = 'It is over 500 characters, so it was saved as these 2 facts, one after the other. That is expected: nothing more to do.';
  const l = new Ledger();
  l.record('remember', {}, ok('remember', `📝 Remembered for this project [aa11]: Part one.\n📝 Remembered for this project [bb22]: Part two.\n${note}`));
  // An update in parts: the first part takes the old fact's place, the rest follow it.
  l.record('remember', {}, ok('remember', `📝 Updated for you [cc33] → [dd44]: Old. → New one.\n📝 Remembered for you [ee55]: New two.\n${note}`));
  assert.equal(l.render(), [
    'Remembered:',
    '- project: Part one.',
    '- project: Part two.',
    '- user: New one. (replacing: Old.)',
    '- user: New two.',
  ].join('\n'));
});

test('remember (Memory 2): a full memory and an unmatched replaces are failures; a line that only looks like a save is too', () => {
  const l = new Ledger();
  l.record('remember', {}, err('remember', 'The project memory is full (7541 of 8000 characters), so nothing was saved. Make room first: merge or shorten facts with replaces, or remove ones that no longer matter. Yours, largest first: [a7f3] (212 chars) "Tests run with…"'));
  l.record('remember', {}, err('remember', 'No fact you saved matches "project: Tests run". Yours: [a7f3] "Tests run with `npm test`…". Give the id in replaces.'));
  l.record('remember', {}, ok('remember', 'Saved: 📝 Remembered for this project [a7f3]: not at the start'));
  const failed = l.render().split('\n');
  assert.equal(failed[0], 'Refused or failed:');
  assert.equal(failed.length, 4);
  assert.match(failed[1], /^- remember did not save: The project memory is full \(7541 of 8000 characters\)/);
  assert.match(failed[2], /^- remember did not save: No fact you saved matches "project: Tests run"/);
  assert.match(failed[3], /^- remember did not save: Saved: 📝/);
});

// The wording above is written by hand. This runs the real store, so the ledger and memory.ts cannot drift apart: it
// passes with the messages with ids (Memory 2) and without them (1.5.3).
test('remember: what the real store says is what the ledger records', () => {
  const fs = require('node:fs');
  const os = require('node:os');
  const path = require('node:path');
  const { rememberFact } = require('../dist/agent/memory');
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-ledger-ws-'));
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-ledger-home-'));
  try {
    const options = { homeDir: home };
    const l = new Ledger();
    const save = (scope, fact, replaces) => {
      const result = rememberFact(scope, fact, root, options, replaces);
      assert.equal(result.ok, true, result.message);
      l.record('remember', { scope, fact, replaces }, { name: 'remember', output: result.message });
    };
    save('project', 'Tests run with npm test from vscode-extension.');
    save('user', 'Prefers short answers.');
    save('project', 'Tests run with npm test from vscode-extension, after npm run compile.', 'Tests run with npm test from vscode-extension.');
    save('user', '', 'Prefers short answers.');
    assert.equal(l.render(), [
      'Remembered:',
      '- project: Tests run with npm test from vscode-extension.',
      '- user: Prefers short answers.',
      '- project: Tests run with npm test from vscode-extension, after npm run compile. (replacing: Tests run with npm test from vscode-extension.)',
      '- user: forgot: Prefers short answers.',
    ].join('\n'));
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
    fs.rmSync(home, { recursive: true, force: true });
  }
});

// --- declined, refused, failed from any tool ---

test('declined: the summary, with the command added for a command', () => {
  const l = new Ledger();
  l.record('run_terminal_command', { command: 'rm -rf build' }, err('run_terminal_command', 'The user declined: run a command in your shell. Do not retry it; ask the user or choose another approach.'));
  l.record('write_file', { path: 'src/x.ts', content: 'z' }, err('write_file', 'The user declined: create src/x.ts. Do not retry it; ask the user or choose another approach.'));
  l.record('read_file', { path: '../x' }, err('read_file', 'The user declined: read /etc/hosts (outside your workspace). Do not retry it; ask the user or choose another approach.'));
  assert.equal(l.render(), 'Declined by the user:\n- run a command in your shell: rm -rf build\n- create src/x.ts\n- read /etc/hosts (outside your workspace)');
});

test('failures: other errors go under "Refused or failed", whatever the tool', () => {
  const l = new Ledger();
  l.record('search_codebase', { query: 'q' }, err('search_codebase', 'Tool execution failed: boom\nsecond line'));
  l.record('mystery_tool', {}, err('mystery_tool', 'Unknown tool: mystery_tool'));
  assert.equal(l.render(), 'Refused or failed:\n- search for "q" failed: boom\n- mystery_tool failed: Unknown tool: mystery_tool');
});

test('a successful call of an unknown tool is not recorded', () => {
  const l = new Ledger();
  l.record('mystery_tool', {}, ok('mystery_tool', 'fine'));
  assert.equal(l.isEmpty(), true);
});

test('record never throws on odd arguments or results', () => {
  const l = new Ledger();
  assert.doesNotThrow(() => {
    l.record('read_file', undefined, { name: 'read_file', output: undefined });
    l.record('write_file', null, { name: 'write_file', output: 'Successfully created file: x (+1 lines, -0 lines)' });
    l.record('run_terminal_command', { command: 42 }, { name: 'x', output: 'ok' });
    l.record('search_codebase', { query: {} }, { name: 'x', output: 'No matches found for "[object Object]"' });
  });
});

// --- The realistic session: the whole rendered string ---

function realisticSession() {
  const l = new Ledger();
  read(l, { path: 'src/a.ts', start_line: 1, end_line: 120 }, numbered(1, 120));
  read(l, { path: 'src/a.ts', start_line: 300, end_line: 380 }, numbered(300, 380));
  read(l, { path: 'src/b.ts' }, numbered(1, 212));
  l.record('search_codebase', { query: 'parseQueueInfo' }, ok('search_codebase', 'Matches found in 3 files:\nsrc/a.ts\nsrc/b.ts\nsrc/c.ts'));
  l.record('list_directory', { path: 'src' }, ok('list_directory', '[DIR] agent\n      a.ts'));
  l.record('list_directory', { path: 'src/agent' }, ok('list_directory', '      tools.ts'));
  l.record('edit_file_diff', { path: 'src/a.ts' }, ok('edit_file_diff', 'Successfully patched src/a.ts (Applied)', { fileDiff: diff(10, 2) }));
  l.record('edit_file_diff', { path: 'src/a.ts' }, ok('edit_file_diff', 'Successfully patched src/a.ts (Applied)', { fileDiff: diff(4, 1) }));
  l.record('write_file', { path: 'src/new.ts', content: '...' }, ok('write_file', 'Successfully created file: src/new.ts (+80 lines, -0 lines)', { fileDiff: diff(80, 0) }));
  l.record('run_terminal_command', { command: 'npm test' }, ok('run_terminal_command', 'Command failed (exit code 1):\nFAIL test/x.test.js\n  1 failing'));
  l.record('run_terminal_command', { command: 'git status' }, ok('run_terminal_command', 'On branch main'));
  l.record('get_workspace_diagnostics', {}, ok('get_workspace_diagnostics', '[ERROR] src/a.ts:3: x\n[ERROR] src/a.ts:9: y\n[WARN] src/b.ts:1: z'));
  l.record('remember', { fact: 'f', scope: 'project' }, ok('remember', '📝 Remembered for this project: Tests run with `npm test` from vscode-extension/.'));
  l.record('run_terminal_command', { command: 'rm -rf build' }, err('run_terminal_command', 'The user declined: run a command in your shell. Do not retry it; ask the user or choose another approach.'));
  l.record('edit_file_diff', { path: 'src/c.ts' }, err('edit_file_diff', 'Diff Error: search block not found'));
  return l;
}

const REALISTIC = [
  'Read:',
  '- src/a.ts lines 1-120, 300-380',
  '- src/b.ts (whole file, 212 lines)',
  'Changed:',
  '- src/a.ts: 2 edits (+14 -3)',
  '- src/new.ts: created (+80)',
  'Ran:',
  '- `npm test` (exit 1): FAIL test/x.test.js',
  '- `git status` (exit 0)',
  'Searched:',
  '- "parseQueueInfo": 3 files',
  'Listed: src, src/agent',
  'Diagnostics:',
  '- workspace: 2 errors, 1 warning',
  'Remembered:',
  '- project: Tests run with `npm test` from vscode-extension/.',
  'Declined by the user:',
  '- run a command in your shell: rm -rf build',
  'Refused or failed:',
  '- edit to src/c.ts did not apply',
].join('\n');

test('a realistic session renders exactly the contract\'s example', () => {
  assert.equal(realisticSession().render(), REALISTIC);
});

// --- Trimming ---

/** n command entries, "cmd 1" (oldest) to "cmd n" (newest). */
function withCommands(n) {
  const l = new Ledger();
  for (let i = 1; i <= n; i++) l.record('run_terminal_command', { command: `cmd ${i}` }, ok('run_terminal_command', 'ok'));
  return l;
}

test('under maxChars nothing is trimmed', () => {
  assert.equal(realisticSession().render(100000), REALISTIC);
});

test('over maxChars the oldest entries go first, leaving "- … N more", and the result fits', () => {
  const l = withCommands(30);
  const full = l.render();
  const text = l.render(full.length - 10);
  assert.ok(text.length <= full.length - 10);
  const lines = text.split('\n');
  assert.equal(lines[0], 'Ran:');
  assert.match(lines[1], /^- … \d+ more$/);
  assert.equal(lines[lines.length - 1], '- `cmd 30` (exit 0)'); // the newest stays
  assert.ok(!text.includes('`cmd 1`'));
  const dropped = Number(/… (\d+) more/.exec(text)[1]);
  assert.equal(lines.length - 2, 30 - dropped);
});

test('trimming keeps at least the 10 newest of a group before shrinking below that', () => {
  const l = withCommands(30);
  // Small enough to force a cut, big enough that ten commands still fit.
  const ten = ['Ran:', '- … 20 more', ...Array.from({ length: 10 }, (_, i) => `- \`cmd ${21 + i}\` (exit 0)`)].join('\n');
  assert.equal(l.render(ten.length), ten);
});

test('below the 10-newest floor, groups shrink further, then the text is cut so it always fits', () => {
  const l = withCommands(30);
  const tiny = l.render(60);
  assert.ok(tiny.length <= 60);
  assert.ok(tiny.startsWith('Ran:'));
});

test('trimming takes from the group with the most spare entries, and leaves the short groups whole', () => {
  const l = withCommands(30);
  l.record('search_codebase', { query: 'q1' }, ok('search_codebase', 'No matches found for "q1"'));
  l.record('list_directory', { path: 'src' }, ok('list_directory', 'x'));
  const full = l.render();
  const text = l.render(full.length - 100);
  assert.ok(text.includes('Searched:\n- "q1": no matches\nListed: src'));
  assert.match(text, /- … \d+ more/);
});

test('a trimmed Listed group says how many were left out on its own line', () => {
  const l = new Ledger();
  for (let i = 1; i <= 25; i++) l.record('list_directory', { path: `dir${i}` }, ok('list_directory', 'x'));
  const full = l.render();
  const text = l.render(full.length - 20);
  assert.match(text, /^Listed: … \d+ more, dir\d+/);
  assert.ok(text.endsWith(', dir25'));
  assert.ok(text.length <= full.length - 20);
});

// --- Retention caps ---

test('retention: 200 paths read, 40 commands, 30 of each other kind; the oldest go', () => {
  const l = new Ledger();
  for (let i = 1; i <= 210; i++) read(l, { path: `f${i}.ts`, start_line: 1, end_line: 2 }, numbered(1, 2));
  for (let i = 1; i <= 50; i++) l.record('run_terminal_command', { command: `c${i}` }, ok('run_terminal_command', 'ok'));
  for (let i = 1; i <= 40; i++) {
    l.record('search_codebase', { query: `q${i}` }, ok('search_codebase', 'No matches found for "x"'));
    l.record('list_directory', { path: `d${i}` }, ok('list_directory', 'x'));
    l.record('get_workspace_diagnostics', { file_path: `g${i}.ts` }, ok('get_workspace_diagnostics', 'No errors or warnings found.'));
    l.record('write_file', { path: `w${i}.ts` }, ok('write_file', `Successfully created file: w${i}.ts (+1 lines, -0 lines)`, { fileDiff: diff(1, 0) }));
    l.record('remember', {}, ok('remember', `📝 Remembered for you: fact ${i}`));
    l.record('run_terminal_command', { command: `no${i}` }, err('run_terminal_command', `The user declined: run a command in your shell. Do not retry it`));
    l.record('read_file', { path: `missing${i}` }, err('read_file', 'File not found: x'));
    l.record('read_file', { path: `.env${i}` }, err('read_file', 'This file is excluded for privacy (a private file): x. DLNE does not read it.'));
  }
  const j = l.toJSON();
  assert.equal(j.reads.length, 200);
  assert.equal(j.reads[0].path, 'f11.ts');
  assert.equal(j.ran.length, 40);
  assert.equal(j.ran[0].command, 'c11');
  assert.equal(j.searches.length, 30);
  assert.equal(j.listed.length, 30);
  assert.equal(j.diagnostics.length, 30);
  assert.equal(j.changes.length, 30);
  assert.equal(j.remembered.length, 30);
  assert.equal(j.declined.length, 30);
  assert.equal(j.trouble.filter((t) => t.kind === 'failed').length, 30);
  assert.equal(j.trouble.filter((t) => t.kind === 'refused').length, 30);
  assert.equal(j.searches[0].query, 'q11');
});

test('the same declined text or trouble twice is one entry', () => {
  const l = new Ledger();
  for (let i = 0; i < 3; i++) {
    l.record('edit_file_diff', { path: 'c.ts' }, err('edit_file_diff', 'Diff Error: x'));
    l.record('run_terminal_command', { command: 'rm x' }, err('run_terminal_command', 'The user declined: run a command in your shell. Do not retry it'));
  }
  assert.equal(l.render(), 'Declined by the user:\n- run a command in your shell: rm x\nRefused or failed:\n- edit to c.ts did not apply');
});

// --- toJSON / fromJSON ---

test('toJSON is plain data that survives JSON.stringify unchanged', () => {
  const j = realisticSession().toJSON();
  assert.equal(j.version, 1);
  assert.deepEqual(JSON.parse(JSON.stringify(j)), j);
});

test('fromJSON(toJSON()) round-trips exactly, even through a JSON string', () => {
  const l = realisticSession();
  const back = Ledger.fromJSON(JSON.parse(JSON.stringify(l.toJSON())));
  assert.deepEqual(back.toJSON(), l.toJSON());
  assert.equal(back.render(), REALISTIC);
  assert.equal(back.isEmpty(), false);
});

test('a restored ledger goes on recording where the old one stopped', () => {
  const back = Ledger.fromJSON(realisticSession().toJSON());
  read(back, { path: 'src/a.ts', start_line: 121, end_line: 130 }, numbered(121, 130));
  assert.match(back.render(), /- src\/a\.ts lines 1-130, 300-380/);
});

test('an empty ledger round-trips as an empty ledger', () => {
  const back = Ledger.fromJSON(new Ledger().toJSON());
  assert.equal(back.isEmpty(), true);
  assert.deepEqual(back.toJSON(), new Ledger().toJSON());
});

test('fromJSON gives an empty ledger for anything malformed, and never throws', () => {
  const good = realisticSession().toJSON();
  const bad = [
    undefined, null, 0, 'text', [], {}, true,
    { ...good, version: 2 },
    { ...good, version: undefined },
    { ...good, reads: 'no' },
    { ...good, reads: [{ path: 'a', ranges: [[5, 1]], whole: false, lines: 0 }] },
    { ...good, reads: [{ path: 'a', ranges: [[1, 5], [6, 9]], whole: false, lines: 0 }] }, // would have merged
    { ...good, reads: [{ path: 'a', ranges: [[1, 'x']], whole: false, lines: 0 }] },
    { ...good, reads: [{ path: 3, ranges: [], whole: false, lines: 0 }] },
    { ...good, changes: [{ path: 'a', created: 'yes', rewrites: 0, edits: 0, additions: 0, deletions: 0 }] },
    { ...good, changes: [{ path: 'a', created: true, rewrites: -1, edits: 0, additions: 0, deletions: 0 }] },
    { ...good, ran: [{ command: 'x', exit: 1, line: '' }] },
    { ...good, searches: [null] },
    { ...good, listed: [1] },
    { ...good, diagnostics: [{ target: 'w', errors: 1.5, warnings: 0, partial: false }] },
    { ...good, remembered: [{ scope: 'team', fact: 'x' }] },
    { ...good, declined: [{}] },
    { ...good, trouble: [{ kind: 'other', text: 'x' }] },
    { ...good, trouble: undefined },
  ];
  for (const input of bad) {
    let ledger;
    assert.doesNotThrow(() => { ledger = Ledger.fromJSON(input); });
    assert.equal(ledger.isEmpty(), true, `should be empty for ${JSON.stringify(input)?.slice(0, 80)}`);
    assert.equal(ledger.render(), '');
  }
});

test('fromJSON survives hostile objects (getters that throw, circular data)', () => {
  const hostile = { get version() { throw new Error('boom'); } };
  assert.equal(Ledger.fromJSON(hostile).isEmpty(), true);
  const loop = {}; loop.self = loop;
  assert.equal(Ledger.fromJSON(loop).isEmpty(), true);
});

test('fromJSON of a save with more entries than today\'s caps keeps the newest', () => {
  const j = new Ledger().toJSON();
  j.listed = Array.from({ length: 50 }, (_, i) => `d${i + 1}`);
  const back = Ledger.fromJSON(j).toJSON();
  assert.equal(back.listed.length, 30);
  assert.equal(back.listed[0], 'd21');
});

test('fromJSON copies its input: changing the saved object later does not change the ledger', () => {
  const j = realisticSession().toJSON();
  const back = Ledger.fromJSON(j);
  j.reads[0].ranges[0][1] = 9999;
  j.ran.length = 0;
  assert.equal(back.render(), REALISTIC);
});
