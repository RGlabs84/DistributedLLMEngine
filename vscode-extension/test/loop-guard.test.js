'use strict';
/**
 * The loop guard (extension 1.5.0, src/agent/loopGuard.ts): an agent that keeps repeating itself is told once, in plain
 * words, to change approach, and stopped if it carries on. The owner's decision: "the same tool call, the same failing
 * command, or rereading the same files. First the model is told what it keeps repeating and to change approach. If it
 * carries on, the run stops with a plain message and a Continue button."
 *
 * Three parts:
 *   - the guard itself, on made-up steps: every trigger, and everything that looks like one and is not (work between,
 *     output that changes, other files, other arguments, the window);
 *   - checkHistory, which DLNE Swarm in the model picker uses on the history VS Code sends with every request;
 *   - the agent loop: the note for the model, the lines for the user, nudge then stop, Continue, the run log.
 * The two chats (@dlne and the sidebar) are in loop-guard-chat.test.js, the model picker in loop-guard-picker.test.js.
 */
const test = require('node:test');
const assert = require('node:assert/strict');

const { scriptedClient, toolCall, workspace, stub } = require('./helpers/chat-harness');
const { generator } = require('./helpers/fake-secrets');
const {
  LOOP_WINDOW_STEPS, LoopGuard, checkHistory, trailingSteps, stepsBeforeUser, loopNote, loopProgressLine, loopStopNotice, loopStopNote, loopStopError, loopSummary,
} = require('../dist/agent/loopGuard');
const { redactSecrets } = require('../dist/privacy/redact');
const { AgentLoop } = require('../dist/agent/agentLoop');
const { ToolRegistry } = require('../dist/agent/tools');

const realExecute = ToolRegistry.executeTool;
test.afterEach(() => {
  ToolRegistry.executeTool = realExecute;
  stub.vscode.workspace.workspaceFolders = undefined;
});

// ---- made-up steps -------------------------------------------------------------------------------------------------

const call = (name, args, output = 'ok', isError = false) => ({ name, args, isError, output });
const read = (path, output = `1: ${path}`, range = {}) => call('read_file', { path, ...range }, output);
const ran = (command, output = 'done') => call('run_terminal_command', { command }, output);
/** A command that exited non-zero, as the tool words it (it sets no error flag). */
const failed = (command, detail = '') => call('run_terminal_command', { command }, `Command failed (exit code 1):\nFAIL ${detail}`.trim());
/** An edit that went through. Each one is a different edit (the same one three times is a loop too), unless `n` says which. */
let editNumber = 0;
const edited = (path, n = ++editNumber) => call('edit_file_diff', { path, search_block: `old ${n}`, replace_block: `new ${n}` }, `Successfully patched ${path} (applied)`);
const editFailed = (path, n = ++editNumber) => call('edit_file_diff', { path, search_block: `old ${n}`, replace_block: `new ${n}` }, 'Diff Error: the block was not found', true);

const newGuard = () => new LoopGuard((text) => text);

/** Feeds each step (a call, or a list of calls) to a guard; the kinds of its verdicts, in order. */
function kinds(guard, steps) {
  return steps.map((step) => guard.record(Array.isArray(step) ? step : [step]).kind);
}
const quiet = (count) => Array(count).fill('none');

// ---- the three triggers ---------------------------------------------------------------------------------------------

test('the same call with the same result, 3 times: the third is a nudge that names it, and a hit after that is a stop', () => {
  const guard = newGuard();
  assert.deepEqual(kinds(guard, [read('a.ts'), read('a.ts')]), ['none', 'none']);
  const third = guard.record([read('a.ts')]);
  assert.equal(third.kind, 'nudge');
  assert.equal(third.reason, 'reading `a.ts` again and again');
  assert.deepEqual(third.hit, { rule: 'same-call', tool: 'read_file', count: 3, threshold: 3 });
  const fourth = guard.record([read('a.ts')]);
  assert.equal(fourth.kind, 'stop');
  assert.equal(fourth.reason, 'reading `a.ts` again and again');
  assert.deepEqual(fourth.hit, { rule: 'same-call', tool: 'read_file', count: 4, threshold: 3 });
});

test('the same command with the same output says so in its reason; a search names what it looked for', () => {
  const command = newGuard();
  const verdict = kinds(command, [ran('git status'), ran('git status')]).concat(command.record([ran('git status')]));
  assert.deepEqual(verdict.slice(0, 2), ['none', 'none']);
  assert.equal(verdict[2].reason, 'running `git status` and getting the same output each time');

  const search = newGuard();
  const none = call('search_codebase', { query: 'TODO' }, 'No matches found for "TODO"');
  search.record([none]);
  search.record([none]);
  assert.equal(search.record([none]).reason, 'searching for `TODO` and getting the same result each time');

  const other = newGuard();
  const status = call('get_workspace_diagnostics', {}, 'No errors or warnings found.');
  other.record([status]);
  other.record([status]);
  assert.equal(other.record([status]).reason, 'calling get_workspace_diagnostics with the same arguments and getting the same result each time');
});

test('arguments are compared as JSON: key order does not matter, and a call to another tool or with other arguments is another call', () => {
  const guard = newGuard();
  const a = call('search_codebase', { query: 'x', path: 'src' }, 'No matches');
  const b = call('search_codebase', { path: 'src', query: 'x' }, 'No matches');
  assert.deepEqual(kinds(guard, [a, b]), ['none', 'none']);
  assert.equal(guard.record([a]).kind, 'nudge');

  const other = newGuard();
  const calls = [call('search_codebase', { query: 'x' }, 'No matches'), call('search_codebase', { query: 'y' }, 'No matches'),
    call('list_directory', { query: 'x' }, 'No matches'), call('search_codebase', { query: 'x', path: 'src' }, 'No matches')];
  assert.deepEqual(kinds(other, calls), quiet(4));
});

test('results are compared on their first 2,000 characters, and an error is not the same result as an answer', () => {
  const tail = (n) => 'x'.repeat(2000) + `end ${n}`;
  const longer = newGuard();
  assert.deepEqual(kinds(longer, [read('a.ts', tail(1)), read('a.ts', tail(2)), read('a.ts', tail(3))]), ['none', 'none', 'nudge'],
    'a difference after the 2,000th character is not a different result');

  const earlier = newGuard();
  assert.deepEqual(kinds(earlier, [read('a.ts', 'x'.repeat(1999) + 'A'), read('a.ts', 'x'.repeat(1999) + 'B'), read('a.ts', 'x'.repeat(1999) + 'C')]),
    quiet(3), 'a difference inside them is');

  const flagged = newGuard();
  assert.deepEqual(kinds(flagged, [call('read_file', { path: 'a.ts' }, 'oops'), call('read_file', { path: 'a.ts' }, 'oops', true),
    call('read_file', { path: 'a.ts' }, 'oops')]), quiet(3), 'the error flag is part of the result');
});

test('the same command failing, 3 times, even when its output differs a little (a time, a duration)', () => {
  const guard = newGuard();
  assert.deepEqual(kinds(guard, [failed('npm test', 'at 10:00:01'), failed('npm test', 'at 10:00:09')]), ['none', 'none']);
  const third = guard.record([failed('npm test', 'at 10:00:15')]);
  assert.equal(third.kind, 'nudge');
  assert.equal(third.reason, 'running `npm test` and failing each time');
  assert.deepEqual(third.hit, { rule: 'failing-command', tool: 'run_terminal_command', count: 3, threshold: 3 });
});

test('a command that fails with the very same output says it is failing, not only that the output is the same', () => {
  const guard = newGuard();
  guard.record([failed('npm test')]);
  guard.record([failed('npm test')]);
  assert.equal(guard.record([failed('npm test')]).hit.rule, 'failing-command');
});

test('an error result counts as failing too: a command the user declined, asked for again and again', () => {
  const declined = call('run_terminal_command', { command: 'rm -rf build' }, 'The user declined: run a command in your shell. Do not retry it.', true);
  const guard = newGuard();
  assert.deepEqual(kinds(guard, [declined, declined]), ['none', 'none']);
  const third = guard.record([declined]);
  assert.equal(third.kind, 'nudge');
  assert.equal(third.hit.rule, 'failing-command');
});

test('the same edit made three times is a loop too, and so is an edit that fails the same way three times', () => {
  const same = call('edit_file_diff', { path: 'a.ts', search_block: 'x', replace_block: 'x\nx' }, 'Successfully patched a.ts (applied)');
  const guard = newGuard();
  assert.deepEqual(kinds(guard, [same, same]), ['none', 'none']);
  const third = guard.record([same]);
  assert.equal(third.kind, 'nudge');
  assert.equal(third.reason, 'editing `a.ts` the same way and getting the same result each time');

  const stuck = call('edit_file_diff', { path: 'a.ts', search_block: 'x', replace_block: 'y' }, 'Diff Error: the block was not found', true);
  assert.deepEqual(kinds(newGuard(), [stuck, stuck, stuck]), ['none', 'none', 'nudge'], 'a failed edit changes nothing, so nothing starts the count again');

  const write = call('write_file', { path: 'a.ts', content: 'x' }, 'Refused: a.ts is part of the agent\'s persistent memory.', true);
  assert.equal(kinds(newGuard(), [write, write, write]).at(-1), 'nudge');
  assert.equal(newGuard().record([write, write, write]).reason, 'writing `a.ts` the same way and getting the same result each time');

  const picture = call('generate_image', { prompt: 'a cat', path: 'cat.png', size: '768x768' }, 'Image generation is not available on this coordinator.', true);
  assert.equal(newGuard().record([picture, picture, picture]).reason, 'generating `cat.png` the same way and getting the same result each time');
});

test('polling is not a loop while its output changes, but a poll that keeps failing is the same failing command', () => {
  const polling = newGuard();
  const outputs = Array.from({ length: 8 }, (_, i) => ran('tail -n 1 build.log', `building ${i * 10}%`));
  assert.deepEqual(kinds(polling, outputs), quiet(8));

  const waiting = newGuard();
  const refused = Array.from({ length: 3 }, (_, i) => failed('curl -s localhost:8080/health', `connection refused (try ${i + 1})`));
  assert.deepEqual(kinds(waiting, refused), ['none', 'none', 'nudge']);
});

test('a file read 4 times is a nudge on the 4th, with its lines read before or not, even when each answer differs', () => {
  const guard = newGuard();
  // Each answer differs (the file is changed by something the guard cannot see), so only this rule can speak.
  const reads = [1, 2, 3, 4].map((n) => read('a.ts', `1: version ${n}`, { start_line: 1, end_line: 10 + n }));
  assert.deepEqual(kinds(guard, reads.slice(0, 3)), quiet(3));
  const fourth = guard.record([reads[3]]);
  assert.equal(fourth.kind, 'nudge');
  assert.equal(fourth.reason, 'reading `a.ts` again and again');
  assert.deepEqual(fourth.hit, { rule: 'reread', tool: 'read_file', count: 4, threshold: 4 });

  const whole = newGuard();
  const wholes = [1, 2, 3, 4].map((n) => read('b.ts', `1: version ${n}`));
  assert.deepEqual(kinds(whole, wholes), ['none', 'none', 'none', 'nudge']);
});

test('reading different parts of a long file is how the tool says to read it, so it is not rereading', () => {
  const guard = newGuard();
  const chunks = [1, 101, 201, 301, 401, 501, 601].map((from) => read('big.ts', `${from}: text`, { start_line: from, end_line: from + 99 }));
  assert.deepEqual(kinds(guard, chunks), quiet(7));
});

test('reading four different files is not a loop, and nor is the same tool with different arguments', () => {
  const files = newGuard();
  assert.deepEqual(kinds(files, ['a.ts', 'b.ts', 'c.ts', 'd.ts', 'e.ts'].map((path) => read(path))), quiet(5));
  const searches = newGuard();
  assert.deepEqual(kinds(searches, Array.from({ length: 8 }, (_, i) => call('search_codebase', { query: `q${i}` }, 'No matches found'))), quiet(8));
  const changing = newGuard();
  assert.deepEqual(kinds(changing, Array.from({ length: 6 }, (_, i) => ran('date', `Wed Oct 28 10:00:0${i}`))), quiet(6));
});

// ---- work is not a loop ----------------------------------------------------------------------------------------------

test('run the tests, edit, run the tests is work: an edit between starts the count again', () => {
  const guard = newGuard();
  const cycle = [];
  for (let i = 0; i < 6; i++) cycle.push(failed('npm test'), edited(i % 2 ? 'a.ts' : 'b.ts'));
  cycle.push(failed('npm test'));
  assert.deepEqual(kinds(guard, cycle), quiet(cycle.length));
});

test('an edit that failed changed nothing, so it does not start the count again', () => {
  const guard = newGuard();
  assert.deepEqual(kinds(guard, [failed('npm test'), editFailed('a.ts'), failed('npm test'), editFailed('b.ts'), failed('npm test')]),
    ['none', 'none', 'none', 'none', 'nudge']);
});

test('every tool that changes a file starts a command\'s count again, the agent\'s own and VS Code\'s', () => {
  const editsBy = {
    write_file: { path: 'a.ts', content: 'x' },
    edit_file_diff: { path: 'a.ts', search_block: 'a', replace_block: 'b' },
    edit_file: { path: 'a.ts' },
    generate_image: { prompt: 'a cat', path: 'cat.png', size: '768x768' },
    create_file: { filePath: 'a.ts', content: 'x' },
    replace_string_in_file: { filePath: 'a.ts', oldString: 'a', newString: 'b' },
    multi_replace_string_in_file: { explanation: 'two edits', replacements: [{ filePath: 'a.ts', oldString: 'a', newString: 'b' }] },
    insert_edit_into_file: { filePath: 'a.ts', code: 'x', explanation: 'add it' },
    apply_patch: { input: '*** Begin Patch\n*** Update File: src/a.ts\n@@\n-a\n+b\n*** End Patch', explanation: 'patch' },
    edit_notebook_file: { filePath: 'a.ipynb', editType: 'insert' },
    mcp_files_write_text: { target: 'a.ts' }, // not a tool we know, but named like one
  };
  for (const [name, args] of Object.entries(editsBy)) {
    const guard = newGuard();
    const edit = call(name, args, 'done');
    assert.deepEqual(kinds(guard, [failed('make'), edit, failed('make'), edit, failed('make')]), quiet(5), `${name} is an edit`);
  }
  // Not an edit: nothing starts the count again, so the third failure speaks.
  for (const name of ['get_changed_files', 'grep_search', 'read_file', 'list_dir', 'run_in_terminal', 'fetch_webpage']) {
    const guard = newGuard();
    const other = call(name, { query: 'x' }, 'fine');
    assert.deepEqual(kinds(guard, [failed('make'), other, failed('make'), other, failed('make')]).at(-1), 'nudge', `${name} is not an edit`);
  }
});

test('an edit between two identical reads starts the count again, whichever file it changed', () => {
  const sameFile = newGuard();
  assert.deepEqual(kinds(sameFile, [read('a.ts'), read('a.ts'), edited('a.ts'), read('a.ts'), read('a.ts')]), quiet(5),
    'three reads of it would be a nudge; the edit in the middle makes it two and two');
  const otherFile = newGuard();
  assert.deepEqual(kinds(otherFile, [read('a.ts'), read('a.ts'), edited('b.ts'), read('a.ts')]), quiet(4),
    'the edit may have been of what the reads were for');
});

test('for a file read again (4 times, however its answers differ), only an edit of that file starts the count again', () => {
  // Each answer differs, so only this rule can speak; the lines asked for overlap, so it is the same lines read again.
  const reads = (path, n) => read(path, `1: version ${n}`, { start_line: 1, end_line: 10 + n });
  const sameFile = newGuard();
  assert.deepEqual(kinds(sameFile, [reads('a.ts', 1), reads('a.ts', 2), edited('a.ts'), reads('a.ts', 3), reads('a.ts', 4)]), quiet(5));
  const otherFile = newGuard();
  assert.deepEqual(kinds(otherFile, [reads('a.ts', 1), reads('a.ts', 2), edited('b.ts'), reads('a.ts', 3), reads('a.ts', 4)]),
    ['none', 'none', 'none', 'none', 'nudge'], 'an edit to another file leaves a.ts as it was');
  // A patch names its files in its text; one that names none could have changed any file.
  const patch = call('apply_patch', { input: '*** Begin Patch\n*** Update File: src/other.ts\n*** End Patch' }, 'done');
  const patched = newGuard();
  assert.equal(kinds(patched, [reads('src/a.ts', 1), reads('src/a.ts', 2), patch, reads('src/a.ts', 3), reads('src/a.ts', 4)]).at(-1), 'nudge');
  const blind = newGuard();
  assert.equal(kinds(blind, [reads('src/a.ts', 1), reads('src/a.ts', 2), call('apply_patch', {}, 'done'), reads('src/a.ts', 3), reads('src/a.ts', 4)]).at(-1), 'none');
});

test('a path is the same path however it is written: ./a.ts, a.ts and a\\b\\c.ts', () => {
  const guard = newGuard();
  assert.deepEqual(kinds(guard, [read('./src/a.ts', '1: x', { start_line: 1 }), read('src//a.ts', '2: x', { start_line: 1 }),
    read('src\\a.ts', '3: x', { start_line: 1 }), read('src/a.ts', '4: x', { start_line: 1 })]), ['none', 'none', 'none', 'nudge']);
});

// ---- the window ---------------------------------------------------------------------------------------------------

test('only the last 12 steps count', () => {
  assert.equal(LOOP_WINDOW_STEPS, 12);
  const target = read('a.ts');
  /** Steps 1..last: the target at `at`, a different file at every other step. */
  const steps = (at, last) => Array.from({ length: last }, (_, i) => (at.includes(i + 1) ? target : read(`f${i + 1}.ts`)));
  assert.equal(kinds(newGuard(), steps([1, 7, 12], 12)).at(-1), 'nudge', 'steps 1, 7 and 12 are all within 12 steps');
  assert.equal(kinds(newGuard(), steps([1, 7, 13], 13)).at(-1), 'none', 'at step 13 the first has gone out of the window');
  assert.equal(kinds(newGuard(), steps([2, 7, 13], 13)).at(-1), 'nudge', 'step 2 has not');
});

test('a step with no calls is not a step', () => {
  const guard = newGuard();
  guard.record([read('a.ts')]);
  guard.record([read('a.ts')]);
  for (let i = 0; i < 30; i++) assert.equal(guard.record([]).kind, 'none');
  assert.equal(guard.record([read('a.ts')]).kind, 'nudge', 'they pushed nothing out of the window');
});

test('one reply that makes the same call three times is a loop too', () => {
  const guard = newGuard();
  assert.equal(guard.record([read('a.ts'), read('a.ts'), read('a.ts')]).kind, 'nudge');
});

// ---- nudge, then stop; a new run starts clean -------------------------------------------------------------------------

test('after a nudge, a step that does something else is not a hit, and a repeat after it is a stop', () => {
  const guard = newGuard();
  assert.deepEqual(kinds(guard, [read('a.ts'), read('a.ts'), read('a.ts')]), ['none', 'none', 'nudge']);
  assert.equal(guard.record([read('other.ts')]).kind, 'none', 'the three reads are still in the window, but this step is not one of them');
  assert.equal(guard.record([ran('ls')]).kind, 'none');
  assert.equal(guard.record([read('a.ts')]).kind, 'stop', 'it went back to it');
});

/** `count` steps that are repeats of nothing: a different file each time. */
const filler = (count, from = 0) => Array.from({ length: count }, (_, i) => read(`f${from + i}.ts`));

test('after a nudge, the next hit of any kind is a stop, until a whole window has gone by with no hit', () => {
  const guard = newGuard();
  assert.deepEqual(kinds(guard, [read('a.ts'), read('a.ts'), read('a.ts')]), ['none', 'none', 'nudge']);
  // 9 steps with no hit, and the first two of a different loop: 11 in all, one short of a window.
  assert.deepEqual(kinds(guard, filler(9)), quiet(9));
  assert.deepEqual(kinds(guard, [failed('make'), failed('make')]), ['none', 'none']);
  assert.equal(guard.record([failed('make')]).kind, 'stop', 'a different loop, in the same run, soon after the nudge');
});

test('12 steps in a row with no hit, and the next loop gets its own nudge first, and a stop only after that', () => {
  const guard = newGuard();
  assert.deepEqual(kinds(guard, [read('a.ts'), read('a.ts'), read('a.ts')]), ['none', 'none', 'nudge']);
  // 10 steps with no hit, and the first two of a different loop: 12 in all, a whole window.
  assert.deepEqual(kinds(guard, filler(10)), quiet(10));
  assert.deepEqual(kinds(guard, [failed('make'), failed('make')]), ['none', 'none']);
  const second = guard.record([failed('make')]);
  assert.equal(second.kind, 'nudge', 'the old loop is out of the window, so this one is told first');
  assert.equal(second.reason, 'running `make` and failing each time');
  assert.equal(guard.record([failed('make')]).kind, 'stop', 'and if it carries on, it is stopped, as the first would have been');
});

test('an early nudge, 60 clean steps, and an unrelated loop is nudged, not stopped (review of 2026-10-28, finding 8)', () => {
  const guard = newGuard();
  assert.deepEqual(kinds(guard, [read('a.ts'), read('a.ts'), read('a.ts')]), ['none', 'none', 'nudge']);
  assert.deepEqual(kinds(guard, filler(60)), quiet(60));
  assert.deepEqual(kinds(guard, [ran('git status'), ran('git status')]), ['none', 'none']);
  assert.equal(guard.record([ran('git status')]).kind, 'nudge');
});

test('a hit starts the count of clean steps again, and steps with no calls are not steps', () => {
  const guard = newGuard();
  kinds(guard, [read('a.ts'), read('a.ts'), read('a.ts')]); // the nudge
  assert.deepEqual(kinds(guard, filler(8)), quiet(8));
  assert.equal(guard.record([read('a.ts')]).kind, 'stop', 'it went back to the loop: a hit, and the 8 clean steps before it count for nothing now');
  // From that hit, 9 clean steps and the first two of a new loop are 11 in all, one short of a window. Steps that call
  // nothing are not steps, so the 20 in the middle change nothing.
  assert.deepEqual(kinds(guard, filler(9, 100)), quiet(9));
  for (let i = 0; i < 20; i++) assert.equal(guard.record([]).kind, 'none');
  assert.deepEqual(kinds(guard, [ran('make'), ran('make')]), ['none', 'none']);
  assert.equal(guard.record([ran('make')]).kind, 'stop');
});

test('a new guard starts with a clean slate: it needs its own three repeats, and its own nudge comes first', () => {
  const first = newGuard();
  kinds(first, [read('a.ts'), read('a.ts'), read('a.ts'), read('a.ts')]);
  const second = newGuard();
  assert.deepEqual(kinds(second, [read('a.ts'), read('a.ts')]), ['none', 'none']);
  assert.equal(second.record([read('a.ts')]).kind, 'nudge');
});

// ---- what is said ------------------------------------------------------------------------------------------------

test('the words: the note for the model, the line for the user, the notice, the model\'s note of the stop, and the error for the picker', () => {
  const reason = 'running `npm test` and failing each time';
  assert.equal(loopNote(reason), 'Loop check: you have been running `npm test` and failing each time. That is not working. Try a different approach, or stop and tell the user what is blocking you.');
  assert.equal(loopProgressLine(reason), '🔁 The agent was repeating itself (running `npm test` and failing each time); DLNE told it to change approach.');
  assert.equal(loopStopNotice(reason), '\n\n⏹️ DLNE stopped this task: the agent kept running `npm test` and failing each time without getting anywhere. Everything so far is kept: say continue with a hint about what to try, or ask for something else.');
  assert.equal(loopStopNote(reason), 'DLNE stopped the last task because you kept running `npm test` and failing each time; take a different approach.');
  assert.equal(loopStopError(reason), 'DLNE stopped: the agent kept running `npm test` and failing each time. Change the request or try again.');
});

test('what is named is shortened, on one line, and has no secrets (the redaction the run log uses)', () => {
  const gen = generator(1517);
  const token = 'gh' + 'p_' + gen('alnum', 36);
  const command = `curl -s -H "Authorization: Bearer ${token}" https://example.test/api/health`;
  const real = new LoopGuard((text) => redactSecrets(text).text);
  const verdicts = [failed(command), failed(command), failed(command)].map((step) => real.record([step]));
  const reason = verdicts[2].reason;
  assert.equal(verdicts[2].kind, 'nudge');
  assert.ok(reason.includes('[REDACTED]'), reason);
  assert.ok(!reason.includes(token.slice(0, 12)), 'no part of the token: ' + reason);

  // Not vacuous: without the redaction the same text would show a piece of the token.
  const plain = newGuard();
  const bare = [failed(command), failed(command), failed(command)].map((step) => plain.record([step]))[2].reason;
  assert.ok(bare.includes(token.slice(0, 12)), 'the token starts inside the 60 characters that are kept');

  // Long, on several lines, with backticks.
  const long = newGuard();
  const messy = `echo "first line"\n  &&   echo \`date\` ${'x'.repeat(200)}`;
  const text = [failed(messy), failed(messy), failed(messy)].map((step) => long.record([step]))[2].reason;
  assert.ok(!/[\n\r]/.test(text));
  const quoted = /running `([^`]*)` and failing each time/.exec(text);
  assert.ok(quoted, text);
  assert.ok(quoted[1].length <= 60, `${quoted[1].length} characters`);
  assert.ok(quoted[1].endsWith('…'));
  assert.ok(quoted[1].startsWith("echo \"first line\" && echo 'date' "), quoted[1]);
});

test('what goes to the log is the tool and the numbers, never a command, a path or a result', () => {
  const guard = newGuard();
  const verdict = [failed('npm test SECRET-ARG'), failed('npm test SECRET-ARG'), failed('npm test SECRET-ARG')].map((s) => guard.record([s]))[2];
  assert.equal(loopSummary(verdict.hit), 'run_terminal_command, the same command failing, 3 times in the last 12 steps');
  assert.ok(!loopSummary(verdict.hit).includes('SECRET'));
  const odd = newGuard();
  const strange = [call('run\nSECRET', {}, 'x'), call('run\nSECRET', {}, 'x'), call('run\nSECRET', {}, 'x')].map((s) => odd.record([s]))[2];
  assert.equal(loopSummary(strange.hit), '?, the same call with the same result, 3 times in the last 12 steps', 'a tool name that does not look like one is not written down');
});

// ---- a history, for DLNE Swarm in the model picker ----------------------------------------------------------------------

const assistantCalls = (id, name, args) => ({
  role: 'assistant', content: '', tool_calls: [{ id, type: 'function', function: { name, arguments: typeof args === 'string' ? args : JSON.stringify(args) } }],
});
const toolResult = (id, content) => ({ role: 'tool', tool_call_id: id, content });
/** `count` steps, each a tool call and its result, built by `make(n)` = [name, args, output]. */
function stepsOf(count, make) {
  return Array.from({ length: count }, (_, n) => {
    const [name, args, output] = make(n);
    return [assistantCalls(`c${n}`, name, args), toolResult(`c${n}`, output)];
  }).flat();
}
const head = [{ role: 'system', content: 'You are an agent.' }, { role: 'user', content: 'fix the tests' }];
const levels = (make, counts) => counts.map((count) => checkHistory([...head, ...stepsOf(count, make)], (t) => t).level);

test('a history: the same call is a nudge at 3 and a stop at 5', () => {
  assert.deepEqual(levels(() => ['read_file', { filePath: 'src/a.ts' }, '1: x'], [1, 2, 3, 4, 5, 6]),
    ['none', 'none', 'nudge', 'nudge', 'stop', 'stop']);
});

test('a history: a file read again is a nudge at 4 and a stop at 6', () => {
  assert.deepEqual(levels((n) => ['read_file', { path: 'a.ts', start_line: 1, end_line: 10 + n }, `1: version ${n}`], [1, 2, 3, 4, 5, 6, 7]),
    ['none', 'none', 'none', 'nudge', 'nudge', 'stop', 'stop']);
});

test('a history: a command that says it failed is a nudge at 3 and a stop at 5', () => {
  assert.deepEqual(levels((n) => ['run_terminal_command', { command: 'npm test' }, `Command failed (exit code 1):\nFAIL at ${n}`], [1, 2, 3, 4, 5, 6]),
    ['none', 'none', 'nudge', 'nudge', 'stop', 'stop']);
});

test('a history says what is repeated, and the verdict carries the numbers', () => {
  const check = checkHistory([...head, ...stepsOf(5, () => ['run_terminal_command', { command: 'npm test' }, 'Command failed (exit code 1):\nFAIL'])], (t) => t);
  assert.equal(check.level, 'stop');
  assert.equal(check.reason, 'running `npm test` and failing each time');
  assert.deepEqual(check.hit, { rule: 'failing-command', tool: 'run_terminal_command', count: 5, threshold: 3 });
});

test('a history: arguments written with other spacing or key order are the same call', () => {
  const texts = ['{"path": "a.ts", "start_line": 1}', '{"start_line":1,"path":"a.ts"}', '{ "path" : "a.ts" ,\n  "start_line" : 1 }'];
  const check = checkHistory([...head, ...stepsOf(3, (n) => ['read_file', texts[n], '1: x'])], (t) => t);
  assert.equal(check.level, 'nudge');
  // Text that is not JSON at all is compared as it is, without its spaces.
  const broken = checkHistory([...head, ...stepsOf(3, (n) => ['read_file', ['{"path": "a', '{"path":"a', '{ "path" : "a'][n], '1: x'])], (t) => t);
  assert.equal(broken.level, 'nudge');
});

test('a history from VS Code: its tool names, its argument names, and an explanation worded afresh each time', () => {
  const terminal = checkHistory([...head, ...stepsOf(3, (n) => ['run_in_terminal', { command: 'npm test', explanation: `Run the tests, attempt ${n}`, isBackground: false }, 'tests ran'])], (t) => t);
  assert.equal(terminal.level, 'nudge');
  assert.equal(terminal.reason, 'running `npm test` and getting the same output each time');
  const file = checkHistory([...head, ...stepsOf(3, () => ['read_file', { filePath: 'src/a.ts', startLine: 1, endLine: 50 }, 'text'])], (t) => t);
  assert.equal(file.reason, 'reading `src/a.ts` again and again');
  const listing = checkHistory([...head, ...stepsOf(3, () => ['list_dir', { path: 'src' }, 'a.ts\nb.ts'])], (t) => t);
  assert.equal(listing.reason, 'listing `src` again and again');
  const grep = checkHistory([...head, ...stepsOf(3, () => ['grep_search', { query: 'TODO', isRegexp: false }, 'no results'])], (t) => t);
  assert.equal(grep.reason, 'searching for `TODO` and getting the same result each time');
});

test('a history: what is named has no secret in it, by the redaction it is given', () => {
  const token = 'gh' + 'p_' + generator(1520)('alnum', 36);
  const command = `curl -s -H "Authorization: Bearer ${token}" https://example.test/api/health`;
  const messages = [...head, ...stepsOf(3, () => ['run_in_terminal', { command }, 'curl: (22) 401'])];
  const redacted = checkHistory(messages, (text) => redactSecrets(text).text);
  assert.equal(redacted.level, 'nudge');
  assert.ok(redacted.reason.includes('[REDACTED]') && !redacted.reason.includes(token.slice(0, 12)), redacted.reason);
  assert.ok(checkHistory(messages, (text) => text).reason.includes(token.slice(0, 12)), 'not vacuous: without it the token is there');
  // The same for the reason of a loop the user has written after.
  const after = [...head, ...stepsOf(5, () => ['run_in_terminal', { command }, 'curl: (22) 401']), { role: 'user', content: 'go on' }];
  const stopped = checkHistory(after, (text) => redactSecrets(text).text);
  assert.equal(stopped.level, 'stopped');
  assert.ok(stopped.reason.includes('[REDACTED]') && !stopped.reason.includes(token.slice(0, 12)), stopped.reason);
});

test('a history: an edit between two runs of a command starts the count again, whichever tool made it', () => {
  const make = (edit) => (n) => (n % 2 ? edit : ['run_in_terminal', { command: 'npm test' }, 'tests ran']);
  for (const edit of [
    ['replace_string_in_file', { filePath: 'a.ts', oldString: 'a', newString: 'b' }, 'ok'],
    ['create_file', { filePath: 'b.ts', content: 'x' }, 'ok'],
    ['apply_patch', { input: '*** Begin Patch\n*** Update File: a.ts\n*** End Patch' }, 'ok'],
  ]) {
    assert.equal(checkHistory([...head, ...stepsOf(5, make(edit))], (t) => t).level, 'none', edit[0]);
  }
  // Not an edit between them: 3 runs with 2 searches in between, and the loop is seen.
  const search = ['grep_search', { query: 'x' }, 'none'];
  assert.equal(checkHistory([...head, ...stepsOf(5, make(search))], (t) => t).level, 'nudge');
});

test('a history that ends with the user speaking is never nudged or refused, so the user can always go on', () => {
  const steps = stepsOf(8, () => ['read_file', { path: 'a.ts' }, '1: x']);
  assert.equal(checkHistory([...head, ...steps], (t) => t).level, 'stop');
  // The user speaks after it: not a refusal, not a nudge; what the loop was is said once (below).
  const spoke = checkHistory([...head, ...steps, { role: 'user', content: 'try something else' }], (t) => t);
  assert.equal(spoke.level, 'stopped');
  assert.equal(spoke.reason, 'reading `a.ts` again and again');
  assert.equal(checkHistory([...head, ...steps, { role: 'assistant', content: 'I could not.' }], (t) => t).level, 'none');
  assert.equal(checkHistory([], (t) => t).level, 'none');
});

/** The level for `count` steps of `make(n)`, with the user's words after them. */
const spoken = (make, counts) => counts.map((count) =>
  checkHistory([...head, ...stepsOf(count, make), { role: 'user', content: 'go on' }], (t) => t).level);

test('a history: the user writing after a loop that went as far as a stop is "stopped"; after one that was only nudged, nothing', () => {
  // The counts are those of a refusal: 5 of the same call, 6 reads of the same lines, 5 failures of the same command.
  assert.deepEqual(spoken(() => ['read_file', { filePath: 'src/a.ts' }, '1: x'], [1, 2, 3, 4, 5, 6]),
    ['none', 'none', 'none', 'none', 'stopped', 'stopped']);
  assert.deepEqual(spoken((n) => ['read_file', { path: 'a.ts', start_line: 1, end_line: 10 + n }, `1: version ${n}`], [1, 2, 3, 4, 5, 6, 7]),
    ['none', 'none', 'none', 'none', 'none', 'stopped', 'stopped']);
  assert.deepEqual(spoken((n) => ['run_terminal_command', { command: 'npm test' }, `Command failed (exit code 1):\nFAIL at ${n}`], [1, 2, 3, 4, 5, 6]),
    ['none', 'none', 'none', 'none', 'stopped', 'stopped']);
  // A clean history has none to speak of.
  assert.deepEqual(spoken((n) => ['read_file', { path: `f${n}.ts` }, `1: file ${n}`], [1, 5, 12, 20]), ['none', 'none', 'none', 'none']);
});

test('a history: only the steps right before the user\'s words count, so an old loop with anything after it says nothing', () => {
  const loop = stepsOf(5, () => ['read_file', { path: 'a.ts' }, '1: x']);
  const user = (content) => ({ role: 'user', content });
  const clean = (count) => stepsOf(count, (n) => ['read_file', { path: `clean${n}.ts` }, `1: ${n}`]);
  const level = (...rest) => checkHistory([...head, ...rest], (t) => t).level;
  assert.equal(level(...loop, user('go on')), 'stopped');
  assert.equal(level(...loop, user('one'), user('two')), 'stopped', 'two messages of the user in a row are one speaking');
  assert.equal(level(...loop, ...clean(1), user('go on')), 'none', 'one clean step after the loop, and it is behind them');
  assert.equal(level(...loop, ...clean(LOOP_WINDOW_STEPS), user('go on')), 'none', 'a whole clean window after it');
  assert.equal(level(...loop, { role: 'assistant', content: 'I could not.' }, user('go on')), 'none', 'the agent answered in between');
  assert.equal(level(...loop, user('go on'), ...clean(1), user('and then?')), 'none', 'their earlier words came after the loop too');
  // The request after the "stopped" one ends with the model's own new step: judged as any step is, and the loop before the user's words is not in it.
  assert.equal(level(...loop, user('go on'), ...clean(1)), 'none');
});

test('stepsBeforeUser: the steps right before the user\'s last words, none when they did not speak last', () => {
  const steps = stepsOf(3, (n) => ['read_file', { path: `f${n}.ts` }, 'x']);
  const user = (content) => ({ role: 'user', content });
  assert.deepEqual(stepsBeforeUser([...head, ...steps, user('a')]).map((s) => s[0].args.path), ['f0.ts', 'f1.ts', 'f2.ts']);
  assert.equal(stepsBeforeUser([...head, ...steps, user('a'), user('b')]).length, 3, 'all their messages in a row are skipped');
  assert.deepEqual(stepsBeforeUser([...head, ...steps]), [], 'they did not speak last');
  assert.deepEqual(stepsBeforeUser([...head, ...steps, { role: 'assistant', content: 'ok' }, user('a')]), [], 'an answer between');
  assert.deepEqual(stepsBeforeUser([user('only words')]), []);
  assert.deepEqual(stepsBeforeUser([]), []);
  const many = stepsBeforeUser([...head, ...stepsOf(30, (n) => ['read_file', { path: `f${n}.ts` }, 'x']), user('a')]);
  assert.equal(many.length, LOOP_WINDOW_STEPS, 'at most a window of them');
});

test('a history: steps before the user last spoke or the agent last answered are another run\'s', () => {
  const before = stepsOf(3, () => ['read_file', { path: 'a.ts' }, '1: x']);
  const after = stepsOf(2, () => ['read_file', { path: 'a.ts' }, '1: x']);
  const history = [...head, ...before, { role: 'assistant', content: 'Done.' }, { role: 'user', content: 'once more' }, ...after];
  assert.equal(checkHistory(history, (t) => t).level, 'none', 'two reads in this run, three in the one before');
});

test('trailingSteps: results are matched to their calls by id, a call nothing answered is left out, at most a window of steps', () => {
  const messages = [
    ...head,
    { role: 'assistant', content: '', tool_calls: [
      { id: 'x1', type: 'function', function: { name: 'read_file', arguments: '{"path":"a.ts"}' } },
      { id: 'x2', type: 'function', function: { name: 'read_file', arguments: '{"path":"b.ts"}' } },
    ] },
    toolResult('x2', 'b text'),
  ];
  const steps = trailingSteps(messages);
  assert.equal(steps.length, 1);
  assert.deepEqual(steps[0].map((c) => [c.name, c.args, c.output]), [['read_file', { path: 'b.ts' }, 'b text']]);
  const many = trailingSteps([...head, ...stepsOf(30, (n) => ['read_file', { path: `f${n}.ts` }, 'x'])]);
  assert.equal(many.length, LOOP_WINDOW_STEPS);
  assert.deepEqual(many.at(-1)[0].args, { path: 'f29.ts' });
});

// ---- the agent loop ----------------------------------------------------------------------------------------------------

function recorder() {
  const seen = { content: '', progress: [], tools: [], results: [] };
  return {
    seen,
    callbacks: {
      onThinkingDelta() {},
      onContentDelta: (d) => { seen.content += d; },
      onToolStart: (name) => seen.tools.push(name),
      onToolEnd: (result) => seen.results.push(result),
      onLoadingProgress: (line) => seen.progress.push(line),
    },
  };
}

/** Commands answer as `answer(command, n)` says (n counts from 1); every other tool is the real one, on the open workspace. */
function stubCommands(answer) {
  let n = 0;
  ToolRegistry.executeTool = function (name, args, approveAll, context) {
    if (name === 'run_terminal_command') {
      return Promise.resolve({ name, output: answer(args.command, ++n), terminalCommand: args.command });
    }
    return realExecute.call(ToolRegistry, name, args, approveAll, context);
  };
}

/** Every tool call of the conversation has its result, and every result a call (the engine refuses anything else). */
function callsPaired(loop) {
  const open = new Set();
  for (const m of loop.messages) {
    if (m.role === 'assistant') for (const c of m.tool_calls ?? []) open.add(c.id);
    if (m.role === 'tool') {
      if (!open.has(m.tool_call_id)) return false;
      open.delete(m.tool_call_id);
    }
  }
  return open.size === 0;
}

const readA = () => ({ toolCalls: [toolCall('read_file', { path: 'a.txt' })] });
const NOTE = 'Loop check: you have been reading `a.txt` again and again. That is not working. Try a different approach, or stop and tell the user what is blocking you.';
const LINE = '🔁 The agent was repeating itself (reading `a.txt` again and again); DLNE told it to change approach.';
const NOTICE = '\n\n⏹️ DLNE stopped this task: the agent kept reading `a.txt` again and again without getting anywhere. Everything so far is kept: say continue with a hint about what to try, or ask for something else.';
const STOP_NOTE = 'DLNE stopped the last task because you kept reading `a.txt` again and again; take a different approach.';

test('the agent loop: a nudge before the next step, then a stop as a pause, with everything kept; both are logged by name only', async () => {
  stub.reset();
  const ws = workspace({ 'a.txt': 'alpha\n' });
  try {
    const client = scriptedClient([readA(), readA(), readA(), readA(), { content: 'never reached' }]);
    const log = [];
    const loop = new AgentLoop(client, { log: (line) => log.push(line) });
    const { seen, callbacks } = recorder();
    assert.equal(await loop.run('read a.txt', callbacks), 'paused');

    assert.equal(client.requests.length, 4, 'the fourth step was the last one: no fifth request');
    const third = client.requests[2].messages.at(-1);
    assert.equal(third.role, 'tool', 'two reads are not a loop');
    const fourth = client.requests[3].messages;
    assert.deepEqual(fourth.at(-1), { role: 'user', content: NOTE }, 'the note comes before the step after the third read');
    assert.equal(fourth.at(-2).role, 'tool');
    assert.deepEqual(seen.progress.filter((line) => line.includes('🔁')), [LINE]);
    assert.ok(seen.content.endsWith(NOTICE), seen.content);
    assert.equal(seen.content.split('⏹️').length, 2, 'the notice once');
    assert.equal(seen.tools.length, 4, 'all four reads ran');
    assert.equal(callsPaired(loop), true, 'everything so far is kept, every call with its result');
    assert.equal(loop.exportState().messages.filter((m) => m.role === 'user' && m.content === NOTE).length, 1, 'and saved with the task');
    assert.deepEqual(loop.exportState().userWords, ['read a.txt'], 'the note is not one of the user\'s words');
    // The notice is the user's; the model is left a note of why the run ended, after the results of the last step.
    assert.deepEqual(loop.messages.at(-1), { role: 'user', content: STOP_NOTE });
    assert.equal(loop.messages.at(-2).role, 'tool');
    assert.deepEqual(loop.exportState().messages.at(-1), { role: 'user', content: STOP_NOTE }, 'and it is saved with the task too');
    assert.ok(!seen.content.includes('take a different approach'), 'the model\'s note is not shown to the user');

    const text = log.join('\n');
    assert.match(text, /agent: run 1 loop check: told the model to change approach after step 3 \(read_file, the same call with the same result, 3 times in the last 12 steps\)/);
    assert.match(text, /agent: run 1 loop check: stopped the run after step 4 \(read_file, the same call with the same result, 4 times in the last 12 steps\)/);
    assert.match(text, /run 1 ended: paused, 4 steps/);
    assert.ok(!text.includes('a.txt'), 'names only: no path in the log');
  } finally {
    ws.restore();
  }
});

test('Continue after a stop starts a fresh guard: the same call twice more is not a loop, and the run goes on to its answer', async () => {
  stub.reset();
  const ws = workspace({ 'a.txt': 'alpha\n' });
  try {
    const client = scriptedClient([readA(), readA(), readA(), readA(), readA(), readA(), { content: 'It says alpha.' }]);
    const loop = new AgentLoop(client, { log() {} });
    assert.equal(await loop.run('read a.txt', recorder().callbacks), 'paused');
    const { seen, callbacks } = recorder();
    assert.equal(await loop.run('continue', callbacks), 'done');
    assert.equal(client.requests.length, 7);
    assert.equal(seen.content, 'It says alpha.', 'no notice');
    assert.deepEqual(seen.progress.filter((line) => line.includes('🔁')), []);
    // The guard that stopped the first run is gone: it would have stopped this one at its first read.
    assert.deepEqual(client.requests[4].messages.at(-1), { role: 'user', content: 'continue' });
    assert.equal(client.requests[5].messages.at(-1).role, 'tool', 'one read of this run: no note');
    assert.equal(client.requests[6].messages.at(-1).role, 'tool', 'two reads of this run: no note');
  } finally {
    ws.restore();
  }
});

test('a Continue that says nothing more still tells the model why the last run was stopped', async () => {
  stub.reset();
  const ws = workspace({ 'a.txt': 'alpha\n' });
  try {
    const client = scriptedClient([readA(), readA(), readA(), readA(), { content: 'I will not read it again.' }]);
    const loop = new AgentLoop(client, { log() {} });
    assert.equal(await loop.run('read a.txt', recorder().callbacks), 'paused');
    assert.equal(await loop.run('continue', recorder().callbacks), 'done');
    const resumed = client.requests[4].messages;
    assert.deepEqual(resumed.slice(-2), [{ role: 'user', content: STOP_NOTE }, { role: 'user', content: 'continue' }],
      'the note, then the user\'s words: two user messages, which Qwen\'s template accepts (docs/agent-memory.md 5.4)');
    assert.equal(resumed.at(-3).role, 'tool', 'after the results of the last step');
    assert.deepEqual(loop.exportState().userWords, ['read a.txt', 'continue'], 'the working memory\'s words are the user\'s own');
  } finally {
    ws.restore();
  }
});

test('a model that changes its approach after the nudge is not stopped, and is told once', async () => {
  stub.reset();
  const ws = workspace({ 'a.txt': 'alpha\n', 'b.txt': 'beta\n' });
  try {
    const client = scriptedClient([readA(), readA(), readA(),
      { toolCalls: [toolCall('read_file', { path: 'b.txt' })] },
      { toolCalls: [toolCall('list_directory', { path: '.' })] },
      { content: 'There are two files.' }]);
    const loop = new AgentLoop(client, { log() {} });
    const { seen, callbacks } = recorder();
    assert.equal(await loop.run('what is here', callbacks), 'done');
    assert.equal(seen.content, 'There are two files.');
    assert.deepEqual(seen.progress.filter((line) => line.includes('🔁')), [LINE]);
    assert.equal(loop.messages.filter((m) => m.role === 'user' && String(m.content).startsWith('Loop check:')).length, 1);
  } finally {
    ws.restore();
  }
});

test('a second loop later in the same run is told first: a whole window of steps with no hit and the guard starts over', async () => {
  stub.reset();
  const ws = workspace({ 'a.txt': 'alpha\n', 'b.txt': 'beta\n' });
  try {
    stubCommands((command) => `ran ${command}`);
    const echo = (n) => ({ toolCalls: [toolCall('run_terminal_command', { command: `echo ${n}` })] });
    const readB = () => ({ toolCalls: [toolCall('read_file', { path: 'b.txt' })] });
    const replies = [readA(), readA(), readA(), ...Array.from({ length: 12 }, (_, n) => echo(n)), readB(), readB(), readB(), readB(), { content: 'never reached' }];
    const client = scriptedClient(replies);
    const log = [];
    const { seen, callbacks } = recorder();
    const loop = new AgentLoop(client, { log: (line) => log.push(line) });
    assert.equal(await loop.run('look around', callbacks, { approveAll: () => true }), 'paused');

    assert.equal(client.requests.length, 19, 'three reads of a.txt, twelve steps with no hit, four reads of b.txt');
    // The review's probe: an early nudge, a long clean stretch, a later unrelated loop. It used to go straight to a stop at the third read of b.txt.
    assert.deepEqual(seen.progress.filter((line) => line.includes('🔁')), [
      LINE, '🔁 The agent was repeating itself (reading `b.txt` again and again); DLNE told it to change approach.',
    ], 'told twice, once for each loop');
    const notes = loop.messages.filter((m) => m.role === 'user' && String(m.content).startsWith('Loop check:')).map((m) => m.content);
    assert.deepEqual(notes, [NOTE, NOTE.replace('a.txt', 'b.txt')]);
    assert.equal(client.requests[18].messages.at(-1).content, NOTE.replace('a.txt', 'b.txt'), 'the second note comes before the step after the third read of b.txt');
    assert.ok(seen.content.endsWith(NOTICE.replace('a.txt', 'b.txt')), 'and the stop, which names the second loop, only when it carried on');
    assert.deepEqual(loop.messages.at(-1), { role: 'user', content: STOP_NOTE.replace('a.txt', 'b.txt') });
    const text = log.join('\n');
    assert.match(text, /told the model to change approach after step 3 /);
    assert.match(text, /told the model to change approach after step 18 /);
    assert.match(text, /stopped the run after step 19 /);
  } finally {
    ws.restore();
  }
});

test('the same failing command is a nudge on the third and a stop on the fourth, whatever its output says', async () => {
  stub.reset();
  const ws = workspace({ 'a.txt': 'alpha\n' });
  try {
    stubCommands((command, n) => `Command failed (exit code 1):\nFAIL at 10:00:0${n}`);
    const run = () => ({ toolCalls: [toolCall('run_terminal_command', { command: 'npm test' })] });
    const client = scriptedClient([run(), run(), run(), run(), { content: 'never reached' }]);
    const { seen, callbacks } = recorder();
    assert.equal(await new AgentLoop(client, { log() {} }).run('make it pass', callbacks, { approveAll: () => true }), 'paused');
    assert.equal(client.requests.length, 4);
    assert.equal(client.requests[3].messages.at(-1).content,
      'Loop check: you have been running `npm test` and failing each time. That is not working. Try a different approach, or stop and tell the user what is blocking you.');
    assert.ok(seen.content.endsWith('the agent kept running `npm test` and failing each time without getting anywhere. Everything so far is kept: say continue with a hint about what to try, or ask for something else.'));
  } finally {
    ws.restore();
  }
});

test('run the tests, edit, run the tests is work for the agent loop: eight failing runs with an edit between each are never a loop', async () => {
  stub.reset();
  const ws = workspace({ 'a.txt': 'v0\n' });
  try {
    stubCommands(() => 'Command failed (exit code 1):\nFAIL: the same assertion');
    const steps = [];
    for (let i = 1; i <= 8; i++) {
      steps.push({ toolCalls: [toolCall('run_terminal_command', { command: 'npm test' })] });
      steps.push({ toolCalls: [toolCall('write_file', { path: 'a.txt', content: `v${i}\n` })] });
    }
    steps.push({ content: 'Still failing, but I tried eight things.' });
    const client = scriptedClient(steps);
    const log = [];
    const { seen, callbacks } = recorder();
    assert.equal(await new AgentLoop(client, { log: (l) => log.push(l) }).run('make it pass', callbacks, { approveAll: () => true, maxTurns: 40 }), 'done');
    assert.equal(client.requests.length, 17);
    assert.deepEqual(seen.progress.filter((line) => line.includes('🔁')), []);
    assert.ok(!log.some((line) => line.includes('loop check')));
  } finally {
    ws.restore();
  }
});

test('tool calls written as text in the reply are checked too, and the note joins their observations (two user messages never stand together)', async () => {
  stub.reset();
  const ws = workspace({ 'a.txt': 'alpha\n' });
  try {
    const inline = { content: '```json\n{"name": "read_file", "arguments": {"path": "a.txt"}}\n```' };
    const client = scriptedClient([inline, inline, inline, { content: 'Done.' }]);
    const loop = new AgentLoop(client, { log() {} });
    const { seen, callbacks } = recorder();
    assert.equal(await loop.run('read a.txt', callbacks), 'done');
    const last = client.requests[3].messages;
    assert.match(last.at(-1).content, /^Observation from tool \[read_file\]:/);
    assert.ok(last.at(-1).content.includes('Continue with the next step.'));
    assert.ok(last.at(-1).content.endsWith(NOTE), 'the note follows the observations in the same message');
    const roles = last.filter((m) => m.role !== 'system').map((m) => m.role);
    for (let i = 1; i < roles.length; i++) assert.ok(!(roles[i] === 'user' && roles[i - 1] === 'user'), `two user messages together at ${i}: ${roles}`);
    assert.deepEqual(seen.progress.filter((line) => line.includes('🔁')), [LINE]);
  } finally {
    ws.restore();
  }
});

test('a stop after tool calls written as text leaves the model its note at the end of the observations', async () => {
  stub.reset();
  const ws = workspace({ 'a.txt': 'alpha\n' });
  try {
    const inline = { content: '```json\n{"name": "read_file", "arguments": {"path": "a.txt"}}\n```' };
    const client = scriptedClient([inline, inline, inline, inline, { content: 'never reached' }]);
    const loop = new AgentLoop(client, { log() {} });
    const { seen, callbacks } = recorder();
    assert.equal(await loop.run('read a.txt', callbacks), 'paused');
    assert.equal(client.requests.length, 4);
    const last = loop.messages.at(-1);
    assert.equal(last.role, 'user');
    assert.match(last.content, /^Observation from tool \[read_file\]:/);
    assert.ok(last.content.endsWith(STOP_NOTE), 'the note follows the observations in the same message');
    assert.ok(seen.content.endsWith(NOTICE));
    const roles = loop.messages.filter((m) => m.role !== 'system').map((m) => m.role);
    for (let i = 1; i < roles.length; i++) assert.ok(!(roles[i] === 'user' && roles[i - 1] === 'user'), `two user messages together at ${i}: ${roles}`);
  } finally {
    ws.restore();
  }
});

test('nothing the user sees, the model is told or the log holds carries a secret from the command', async () => {
  stub.reset();
  const ws = workspace({ 'a.txt': 'alpha\n' });
  try {
    const gen = generator(1518);
    const token = 'gh' + 'p_' + gen('alnum', 36);
    const command = `curl -s -H "Authorization: Bearer ${token}" https://example.test/api/health`;
    stubCommands(() => 'Command failed (exit code 22):\ncurl: (22) The requested URL returned error: 401');
    const run = () => ({ toolCalls: [toolCall('run_terminal_command', { command })] });
    const client = scriptedClient([run(), run(), run(), run(), { content: 'never reached' }]);
    const log = [];
    const { seen, callbacks } = recorder();
    const loop = new AgentLoop(client, { log: (l) => log.push(l) });
    assert.equal(await loop.run('check the api', callbacks, { approveAll: () => true }), 'paused');
    const note = client.requests[3].messages.at(-1).content;
    const stopNote = loop.messages.at(-1).content;
    const surfaces = [note, stopNote, seen.progress.join('\n'), seen.content, log.join('\n')];
    for (const text of surfaces) assert.ok(!text.includes(token.slice(0, 12)), 'a piece of the token was written down');
    assert.ok(note.includes('[REDACTED]'), note);
    assert.match(stopNote, /^DLNE stopped the last task because you kept running `curl .*\[REDACTED\]/);
    assert.ok(seen.progress.some((line) => line.includes('🔁')));
  } finally {
    ws.restore();
  }
});

test('Stop during a step is not a loop check: nothing is said, and the run ends as aborted', async () => {
  stub.reset();
  const ws = workspace({ 'a.txt': 'alpha\n' });
  try {
    const client = scriptedClient([readA(), readA(), readA(), readA()]);
    const loop = new AgentLoop(client, { log() {} });
    const { seen, callbacks } = recorder();
    let reads = 0;
    callbacks.onToolEnd = () => { if (++reads === 3) loop.abort(); };
    assert.equal(await loop.run('read a.txt', callbacks), 'aborted');
    assert.equal(seen.content, '');
    assert.deepEqual(seen.progress.filter((line) => line.includes('🔁')), []);
  } finally {
    ws.restore();
  }
});
