'use strict';
/**
 * The agent's persistent memory store (docs/agent-memory.md section 7): what is loaded from AGENTS.md,
 * .dlne/memory.md and the user's ~/.dlne/memory.md, the <dlne_context> block made from it, and the writer
 * behind the `remember` tool. A temporary folder stands in for both the project and the home folder.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const stub = require('./helpers/vscode-stub');
const { generator, pem } = require('./helpers/fake-secrets');
const {
  loadMemory, renderContextBlock, rememberFact, checkRemember,
  CONTEXT_OPEN, CONTEXT_INTRO, CONTEXT_CLOSE, CONTEXT_HEADINGS,
} = require('../dist/agent/memory');

/** A project folder and a home folder, both temporary; the project is the open workspace folder. */
function world(body) {
  stub.reset();
  const base = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-memory-'));
  const root = path.join(base, 'project');
  const home = path.join(base, 'home');
  fs.mkdirSync(root);
  fs.mkdirSync(home);
  stub.vscode.workspace.workspaceFolders = [{ uri: { fsPath: root } }];
  const w = {
    root,
    home,
    options: { homeDir: home },
    write(rel, text, base = root) {
      const file = path.join(base, rel);
      fs.mkdirSync(path.dirname(file), { recursive: true });
      fs.writeFileSync(file, text);
      return file;
    },
    projectFile: path.join(root, '.dlne', 'memory.md'),
    userFile: path.join(home, '.dlne', 'memory.md'),
  };
  try {
    return body(w);
  } finally {
    stub.vscode.workspace.workspaceFolders = undefined;
    fs.rmSync(base, { recursive: true, force: true });
  }
}

// The header a new project file starts with, copied from the contract.
const PROJECT_HEADER =
  '# DLNE memory\n\n' +
  '<!-- What the DLNE agent remembers about this project. It reads this file at the start of every request and sends it, ' +
  "with the rest of the prompt, to the DLNE swarm (the coordinator or a volunteer's coding member). " +
  'Add, edit or delete lines freely; keep secrets out. -->\n\n';

// ---- loading ------------------------------------------------------------------------------------------

test('nothing exists: nothing is loaded, and no block is made', () =>
  world((w) => {
    assert.deepEqual(loadMemory(w.root, w.options), {});
    assert.deepEqual(loadMemory(undefined, w.options), {});
    assert.equal(renderContextBlock({}), null);
  }));

test('all three files load, each with its label and path', () =>
  world((w) => {
    w.write('AGENTS.md', 'Use tabs.\n');
    w.write('.dlne/memory.md', '- Tests: npm test\n');
    w.write('.dlne/memory.md', '- Likes short answers\n', w.home);
    const sources = loadMemory(w.root, w.options);
    assert.deepEqual(sources.instructions, { label: 'instructions', path: path.join(w.root, 'AGENTS.md'), text: 'Use tabs.' });
    assert.deepEqual(sources.project, { label: 'project', path: w.projectFile, text: '- Tests: npm test' });
    assert.deepEqual(sources.user, { label: 'user', path: w.userFile, text: '- Likes short answers' });
  }));

test('with no folder open only the user\'s memory applies', () =>
  world((w) => {
    w.write('AGENTS.md', 'ignored');
    w.write('.dlne/memory.md', '- ignored too');
    w.write('.dlne/memory.md', '- mine', w.home);
    const sources = loadMemory(undefined, w.options);
    assert.deepEqual(Object.keys(sources), ['user']);
  }));

test('comments and the title line are stripped, then the text is trimmed', () =>
  world((w) => {
    w.write('.dlne/memory.md', PROJECT_HEADER + '- one\n<!-- a note for\nthe person editing -->\n- two\n\n');
    assert.equal(loadMemory(w.root, w.options).project.text, '- one\n\n- two');
    // Only an exact first line counts as the title, and CRLF files work too.
    w.write('.dlne/memory.md', '# DLNE memory notes\n- kept');
    assert.equal(loadMemory(w.root, w.options).project.text, '# DLNE memory notes\n- kept');
    w.write('.dlne/memory.md', '# DLNE memory\r\n- crlf\r\n');
    assert.equal(loadMemory(w.root, w.options).project.text, '- crlf');
  }));

test('a file with only a header, only comments, or only spaces is absent', () =>
  world((w) => {
    w.write('.dlne/memory.md', PROJECT_HEADER);
    w.write('AGENTS.md', '   \n<!-- nothing -->\n');
    assert.deepEqual(loadMemory(w.root, w.options), {});
  }));

test('secrets are redacted on the way in', () =>
  world((w) => {
    const gen = generator(7);
    w.write('AGENTS.md', `Deploy notes\n${pem(gen, 'RSA PRIVATE KEY')}\nend`);
    const text = loadMemory(w.root, w.options).instructions.text;
    assert.match(text, /Deploy notes/);
    assert.match(text, /\[REDACTED\]/);
    assert.doesNotMatch(text, /PRIVATE KEY/);
  }));

test('a file the privacy rules exclude is not loaded, and not written', () =>
  world((w) => {
    w.write('.dlneignore', '.dlne/\nAGENTS.md\n');
    w.write('AGENTS.md', 'secret plans');
    w.write('.dlne/memory.md', '- hidden');
    w.write('.dlne/memory.md', '- user fact', w.home);
    const sources = loadMemory(w.root, w.options);
    assert.deepEqual(Object.keys(sources), ['user']);
    const before = fs.readFileSync(w.projectFile, 'utf8');
    const result = rememberFact('project', 'Run npm test', w.root, w.options);
    assert.equal(result.ok, false);
    assert.match(result.message, /excluded by DLNE's privacy rules/);
    assert.equal(fs.readFileSync(w.projectFile, 'utf8'), before);
  }));

test('the caps cut a long file and say how much was cut', () =>
  world((w) => {
    w.write('AGENTS.md', 'a'.repeat(8_000 + 123));
    w.write('.dlne/memory.md', 'b'.repeat(8_000));
    w.write('.dlne/memory.md', 'c'.repeat(4_000 + 7), w.home);
    const sources = loadMemory(w.root, w.options);
    assert.equal(sources.instructions.text, 'a'.repeat(8_000) + '\n[… 123 characters cut: the file is longer than DLNE loads]');
    assert.equal(sources.project.text, 'b'.repeat(8_000), 'exactly at the cap is not cut');
    assert.equal(sources.user.text, 'c'.repeat(4_000) + '\n[… 7 characters cut: the file is longer than DLNE loads]');
  }));

test('a directory, an unreadable path and a missing home folder are simply absent, and loading never throws', () =>
  world((w) => {
    fs.mkdirSync(path.join(w.root, 'AGENTS.md'));
    assert.deepEqual(loadMemory(w.root, w.options), {});
    assert.deepEqual(loadMemory(w.root, { homeDir: path.join(w.root, 'no', 'such', 'home') }), {});
    assert.doesNotThrow(() => loadMemory(42, { homeDir: null }));
  }));

test('the setting off: nothing is loaded', () =>
  world((w) => {
    w.write('AGENTS.md', 'Use tabs.');
    assert.deepEqual(loadMemory(w.root, { ...w.options, enabled: false }), {});
    assert.deepEqual(Object.keys(loadMemory(w.root, { ...w.options, enabled: true })), ['instructions']);
  }));

test('a workspace opened at the home folder does not send one file twice', () =>
  world((w) => {
    w.write('.dlne/memory.md', '- shared', w.home);
    const sources = loadMemory(w.home, w.options);
    assert.deepEqual(Object.keys(sources), ['user']);
  }));

// ---- the context block --------------------------------------------------------------------------------

const src = (label, text) => ({ label, path: `/x/${label}`, text });

test('the block is exactly the contract\'s text, with all three sections', () => {
  const block = renderContextBlock({
    instructions: src('instructions', 'Use tabs.'),
    project: src('project', '- Tests: npm test'),
    user: src('user', '- Short answers'),
  });
  assert.equal(block, [
    '<dlne_context>',
    'Standing notes for this workspace, loaded by the DLNE extension. They are not a request from the user.',
    '',
    '## Project instructions (AGENTS.md)',
    'Use tabs.',
    '',
    '## Project memory (.dlne/memory.md)',
    '- Tests: npm test',
    '',
    "## The user's preferences (~/.dlne/memory.md)",
    '- Short answers',
    '</dlne_context>',
  ].join('\n'));
});

test('every subset of the sections renders, in the contract\'s order, with one empty line between sections', () => {
  const all = ['instructions', 'project', 'user'];
  for (let mask = 1; mask < 8; mask++) {
    const present = all.filter((_, i) => mask & (1 << i));
    const sources = {};
    for (const label of present) sources[label] = src(label, `text-${label}`);
    const expected = [CONTEXT_OPEN, CONTEXT_INTRO, '']
      .concat(present.flatMap((label, i) => (i ? [''] : []).concat([CONTEXT_HEADINGS[label], `text-${label}`])))
      .concat([CONTEXT_CLOSE])
      .join('\n');
    assert.equal(renderContextBlock(sources), expected, present.join('+'));
  }
});

test('a source with no text counts as absent; with none left the block is null', () => {
  assert.equal(renderContextBlock({ project: src('project', '') }), null);
  const block = renderContextBlock({ project: src('project', ''), user: src('user', '- u') });
  assert.doesNotMatch(block, /Project memory/);
  assert.match(block, /preferences/);
});

test('the exported constants are the contract\'s lines', () => {
  assert.equal(CONTEXT_OPEN, '<dlne_context>');
  assert.equal(CONTEXT_CLOSE, '</dlne_context>');
  assert.deepEqual(CONTEXT_HEADINGS, {
    instructions: '## Project instructions (AGENTS.md)',
    project: '## Project memory (.dlne/memory.md)',
    user: "## The user's preferences (~/.dlne/memory.md)",
  });
});

// ---- rememberFact -------------------------------------------------------------------------------------

test('a fact for the project starts the file with the header, then one line', () =>
  world((w) => {
    const result = rememberFact('project', 'Tests run with `npm test`.', w.root, w.options);
    assert.equal(result.ok, true);
    assert.equal(result.message, '📝 Remembered for this project: Tests run with `npm test`.');
    // The line carries the agent's mark (a comment, so the model never sees it), and Undo gets what changed.
    assert.equal(fs.readFileSync(w.projectFile, 'utf8'), PROJECT_HEADER + '- Tests run with `npm test`. <!-- dlne -->\n');
    assert.deepEqual(result.change, { file: w.projectFile, scope: 'project', removed: null, added: '- Tests run with `npm test`. <!-- dlne -->', index: 4 });
    rememberFact('project', 'Source is in src/.', w.root, w.options);
    assert.equal(fs.readFileSync(w.projectFile, 'utf8'),
      PROJECT_HEADER + '- Tests run with `npm test`. <!-- dlne -->\n- Source is in src/. <!-- dlne -->\n');
    // What was written loads back as just the facts.
    assert.equal(loadMemory(w.root, w.options).project.text, '- Tests run with `npm test`.\n- Source is in src/.');
  }));

test('the user\'s file has its own header wording and mode 0600', () =>
  world((w) => {
    const result = rememberFact('user', 'Prefers short answers.', w.root, w.options);
    assert.equal(result.ok, true);
    assert.equal(result.message, '📝 Remembered for you: Prefers short answers.');
    const text = fs.readFileSync(w.userFile, 'utf8');
    assert.ok(text.startsWith('# DLNE memory\n\n<!-- What the DLNE agent remembers about your preferences, for every project. It reads this file'));
    assert.ok(text.endsWith('-->\n\n- Prefers short answers. <!-- dlne -->\n'));
    if (process.platform !== 'win32') {
      assert.equal(fs.statSync(w.userFile).mode & 0o777, 0o600);
      // An existing, looser file is tightened when it is rewritten.
      fs.chmodSync(w.userFile, 0o644);
      rememberFact('user', 'Another.', w.root, w.options);
      assert.equal(fs.statSync(w.userFile).mode & 0o777, 0o600);
    }
  }));

test('the user scope works with no folder open', () =>
  world((w) => {
    assert.equal(rememberFact('user', 'Likes Rust.', undefined, w.options).ok, true);
    assert.ok(fs.existsSync(w.userFile));
  }));

test('a project fact with no folder open is refused', () =>
  world((w) => {
    assert.deepEqual(rememberFact('project', 'x', undefined, w.options),
      { ok: false, message: 'There is no project folder open, so nothing was saved for a project.' });
    assert.equal(fs.existsSync(w.projectFile), false);
  }));

test('the scope must be project or user', () =>
  world((w) => {
    for (const scope of ['team', '', undefined, null, 3, 'Project']) {
      const result = rememberFact(scope, 'A fact.', w.root, w.options);
      assert.equal(result.ok, false, String(scope));
      assert.match(result.message, /scope must be "project" or "user"/);
    }
    assert.equal(fs.existsSync(w.projectFile), false);
  }));

test('the fact is cleaned: newlines become spaces and the ends are trimmed', () =>
  world((w) => {
    const result = rememberFact('project', '  Build with make,\nthen run ./test\r\nand look.  ', w.root, w.options);
    assert.equal(result.message, '📝 Remembered for this project: Build with make, then run ./test and look.');
    assert.match(fs.readFileSync(w.projectFile, 'utf8'), /\n- Build with make, then run \.\/test and look\. <!-- dlne -->\n$/);
  }));

test('a fact is not empty, and one over 500 characters is written as several lines', () =>
  world((w) => {
    for (const empty of ['', '   ', '\n\n']) {
      assert.equal(rememberFact('project', empty, w.root, w.options).ok, false);
    }
    assert.equal(rememberFact('project', undefined, w.root, w.options).ok, false);
    assert.equal(rememberFact('project', 42, w.root, w.options).ok, false);
    assert.equal(fs.existsSync(w.projectFile), false, 'nothing written for a refused fact');
    const single = rememberFact('project', 'y'.repeat(500), w.root, w.options);
    assert.equal(single.ok, true);
    assert.ok(!Array.isArray(single.change), 'one line, one change');
    const long = rememberFact('project', 'x'.repeat(501), w.root, w.options);
    assert.equal(long.ok, true);
    assert.equal(long.change.length, 2);
    assert.match(long.message, /saved as these 2 facts, one after the other\. That is expected: nothing more to do\.$/);
  }));

test('a comment marker in a fact is refused, since it would hide the lines after it', () =>
  world((w) => {
    assert.equal(rememberFact('project', 'Use <!-- carefully', w.root, w.options).ok, false);
    assert.equal(rememberFact('project', 'Use --> carefully', w.root, w.options).ok, false);
  }));

test('a fact already in the file is not added twice, however its case, spacing or bullet differs', () =>
  world((w) => {
    rememberFact('project', 'Tests run with npm test.', w.root, w.options);
    const before = fs.readFileSync(w.projectFile, 'utf8');
    const again = rememberFact('project', '  tests   RUN with npm\ttest. ', w.root, w.options);
    assert.deepEqual(again, { ok: true, message: 'Already remembered: tests   RUN with npm\ttest.' });
    assert.equal(fs.readFileSync(w.projectFile, 'utf8'), before);
    // A line the user typed without the bullet counts too.
    fs.appendFileSync(w.projectFile, 'Plain line written by hand\n');
    assert.match(rememberFact('project', 'plain line written by hand', w.root, w.options).message, /^Already remembered/);
  }));

test('a fact holding a secret is refused and nothing is written', () =>
  world((w) => {
    const gen = generator(11);
    const secret = `The API token is ghp_${gen('alnum', 36)} for deploys.`;
    const result = rememberFact('project', secret, w.root, w.options);
    assert.deepEqual(result, { ok: false, message: 'That fact holds a secret, so it was not saved.' });
    assert.equal(fs.existsSync(w.projectFile), false);
    const password = rememberFact('user', `password = ${gen('alnum', 20)}`, w.root, w.options);
    assert.equal(password.ok, false);
    assert.equal(fs.existsSync(w.userFile), false);
  }));

test('a file that would pass its load cap is full: the project\'s at 8,000, the user\'s at 4,000', () =>
  world((w) => {
    w.write('.dlne/memory.md', 'x'.repeat(7_900));
    const full = rememberFact('project', 'y'.repeat(150), w.root, w.options);
    assert.equal(full.ok, false);
    assert.equal(full.message, `The memory file is full (${w.projectFile}); ask the user to tidy it.`);
    assert.equal(fs.readFileSync(w.projectFile, 'utf8'), 'x'.repeat(7_900), 'untouched');
    assert.equal(rememberFact('project', 'y'.repeat(50), w.root, w.options).ok, true, 'a smaller one still fits');

    w.write('.dlne/memory.md', 'x'.repeat(3_900), w.home);
    assert.equal(rememberFact('user', 'y'.repeat(150), w.root, w.options).ok, false);
  }));

test('comments in a file do not count toward its cap', () =>
  world((w) => {
    w.write('.dlne/memory.md', '<!-- ' + 'c'.repeat(9_000) + ' -->\n- a fact\n');
    assert.equal(rememberFact('project', 'Another fact.', w.root, w.options).ok, true);
  }));

test('the setting off: nothing is saved and the model is told why', () =>
  world((w) => {
    const result = rememberFact('project', 'A fact.', w.root, { ...w.options, enabled: false });
    assert.deepEqual(result, { ok: false, message: 'Memory is off (the dlne.agent.memory setting), so nothing was saved.' });
    assert.equal(fs.existsSync(w.projectFile), false);
  }));

test('a write leaves no temporary file behind, and an unwritable folder is an answer, not an exception', () =>
  world((w) => {
    rememberFact('project', 'One.', w.root, w.options);
    assert.deepEqual(fs.readdirSync(path.dirname(w.projectFile)), ['memory.md']);
    // .dlne exists as a plain file, so the folder cannot be made.
    fs.rmSync(path.dirname(w.projectFile), { recursive: true });
    fs.writeFileSync(path.dirname(w.projectFile), 'in the way');
    const result = rememberFact('project', 'Two.', w.root, w.options);
    assert.equal(result.ok, false);
    assert.match(result.message, /could not be (read|written)/);
  }));

test('checkRemember judges without writing', () =>
  world((w) => {
    const ready = checkRemember('project', ' A fact. ', w.root, w.options);
    assert.equal(ready.ready, true);
    assert.equal(ready.fact, 'A fact.');
    assert.equal(ready.file, w.projectFile);
    assert.equal(fs.existsSync(w.projectFile), false);
    assert.equal(checkRemember('team', 'A fact.', w.root, w.options).ready, false);
  }));

// ---- git-ignore and the memory files ------------------------------------------------------------------

test('.dlne/ git-ignored: the memory still loads, and remember still writes', () =>
  world((w) => {
    w.write('.gitignore', '.dlne/\n');
    w.write('.dlne/memory.md', '- kept out of commits only');
    assert.equal(loadMemory(w.root, w.options).project.text, '- kept out of commits only');
    assert.equal(rememberFact('project', 'Still written.', w.root, w.options).ok, true);
    assert.match(fs.readFileSync(w.projectFile, 'utf8'), /- Still written\. <!-- dlne -->\n$/);
  }));

test('.git/info/exclude is no more a bar than .gitignore', () =>
  world((w) => {
    fs.mkdirSync(path.join(w.root, '.git', 'info'), { recursive: true });
    w.write('.git/info/exclude', '.dlne/\n');
    w.write('.dlne/memory.md', '- fine');
    assert.equal(loadMemory(w.root, w.options).project.text, '- fine');
    assert.equal(rememberFact('project', 'Also fine.', w.root, w.options).ok, true);
  }));

test('.dlne/memory.md in .dlneignore: not loaded, and remember refuses', () =>
  world((w) => {
    w.write('.dlneignore', '.dlne/memory.md\n');
    w.write('.dlne/memory.md', '- hidden');
    assert.equal(loadMemory(w.root, w.options).project, undefined);
    const result = rememberFact('project', 'Refused.', w.root, w.options);
    assert.equal(result.ok, false);
    assert.match(result.message, /\.dlneignore/);
  }));

test('AGENTS.md git-ignored: not loaded, since it keeps every rule', () =>
  world((w) => {
    w.write('.gitignore', 'AGENTS.md\n');
    w.write('AGENTS.md', 'private instructions');
    assert.equal(loadMemory(w.root, w.options).instructions, undefined);
  }));
