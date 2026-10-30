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
const crypto = require('node:crypto');
const {
  loadMemory, renderContextBlock, rememberFact, checkRemember, factId, factIds,
  CONTEXT_OPEN, CONTEXT_INTRO, CONTEXT_CLOSE, CONTEXT_HEADINGS,
} = require('../dist/agent/memory');

/** A fact's id worked out here, apart from memory.ts: SHA-256 of the lower-case fact, first 4 hex characters. */
const idOf = (fact) => crypto.createHash('sha256').update(fact.toLowerCase(), 'utf8').digest('hex').slice(0, 4);

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
    const id = idOf('Tests run with `npm test`.');
    assert.equal(result.message, `📝 Remembered for this project [${id}]: Tests run with \`npm test\`.`);
    assert.deepEqual(result.ids, [id]);
    // The line carries the agent's mark (a comment, so the model never sees it), and Undo gets what changed.
    assert.equal(fs.readFileSync(w.projectFile, 'utf8'), PROJECT_HEADER + '- Tests run with `npm test`. <!-- dlne -->\n');
    assert.deepEqual(result.change, { file: w.projectFile, scope: 'project', removed: null, added: '- Tests run with `npm test`. <!-- dlne -->', index: 4 });
    rememberFact('project', 'Source is in src/.', w.root, w.options);
    assert.equal(fs.readFileSync(w.projectFile, 'utf8'),
      PROJECT_HEADER + '- Tests run with `npm test`. <!-- dlne -->\n- Source is in src/. <!-- dlne -->\n');
    // What was written loads back as just the facts, each with its id in front.
    assert.equal(loadMemory(w.root, w.options).project.text,
      `- [${id}] Tests run with \`npm test\`.\n- [${idOf('Source is in src/.')}] Source is in src/.`);
  }));

test('the user\'s file has its own header wording and mode 0600', () =>
  world((w) => {
    const result = rememberFact('user', 'Prefers short answers.', w.root, w.options);
    assert.equal(result.ok, true);
    assert.equal(result.message, `📝 Remembered for you [${idOf('Prefers short answers.')}]: Prefers short answers.`);
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
    const id = idOf('Build with make, then run ./test and look.');
    assert.equal(result.message, `📝 Remembered for this project [${id}]: Build with make, then run ./test and look.`);
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
    // All of it is the user's: the agent has nothing of its own to shorten, so only the user can make room.
    assert.equal(full.message, 'The project memory is full (7900 of 8000 characters), so nothing was saved. ' +
      'None of it was saved by DLNE, so ask the user to tidy it.');
    assert.equal(fs.readFileSync(w.projectFile, 'utf8'), 'x'.repeat(7_900), 'untouched');
    // 7,900 + a line break + "- [xxxx] " + 50 = 7,960: it fits, ids counted.
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

// ---- Memory 2: fact ids -------------------------------------------------------------------------------

/** A line as the agent writes it. */
const mine = (fact) => `- ${fact} <!-- dlne -->`;

/** A project file with the header, then `lines` (the agent's made with `mine`, the user's as they are). */
function seed(w, lines) {
  w.write('.dlne/memory.md', PROJECT_HEADER + lines.join('\n') + '\n');
}

/** The facts in the project file, the agent's mark and bullet taken off. */
const factsIn = (w) => fs.readFileSync(w.projectFile, 'utf8').split('\n')
  .filter((l) => l.startsWith('- ')).map((l) => l.replace(/^- /, '').replace(/ <!-- dlne -->$/, ''));

test('a fact\'s id is the first 4 hex characters of the SHA-256 of its key, however the line is written', () => {
  // Hard-coded as well as worked out, so a change to the key or the hash shows here (engine/canary.py must agree).
  assert.equal(factId('Tests run with npm test.'), 'b468');
  assert.equal(factId('Tests run with npm test.'), idOf('Tests run with npm test.'));
  for (const same of ['- Tests run with npm test. <!-- dlne -->', '  *  tests   RUN with npm\ttest.  ', 'TESTS RUN WITH NPM TEST.']) {
    assert.equal(factId(same), 'b468', same);
  }
  assert.notEqual(factId('Tests run with npm test'), 'b468', 'the full stop is part of the fact');
  // Stable: the same fact keeps its id when other facts come and go around it.
  const before = factIds([mine('Tests run with npm test.')]).get(mine('Tests run with npm test.'));
  const after = factIds(['- a note of the user\'s', mine('Source is in src/.'), mine('Tests run with npm test.')]);
  assert.equal(before, 'b468');
  assert.equal(after.get(mine('Tests run with npm test.')), 'b468');
  assert.equal(after.size, 2, 'only the agent\'s lines have ids');
});

test('two ids that clash make every id in the file longer, to the first length that tells them apart', () => {
  // "Fact 59." and "Fact 148." share e556; at 5 characters they differ. "Fact 116." and "Fact 1327." share 6cd70.
  assert.equal(idOf('Fact 59.'), idOf('Fact 148.'));
  const five = factIds([mine('Fact 59.'), '- the user\'s line', mine('Fact 148.'), mine('Tests run with npm test.')]);
  assert.deepEqual([...five.values()], ['e5569', 'e5564', 'b4683'], 'the bystander grows too');
  const six = factIds([mine('Fact 116.'), mine('Fact 1327.')]);
  assert.deepEqual([...six.values()], ['6cd70f', '6cd708']);
  // Without the clash the same facts are back to 4.
  assert.deepEqual([...factIds([mine('Fact 59.'), mine('Tests run with npm test.')]).values()], ['e556', 'b468']);
  // The same fact twice (copied by hand) never separates: 8 is the limit, used anyway.
  const twice = factIds([mine('Same fact.'), mine('same   FACT.')]);
  assert.deepEqual([...twice.values()].map((id) => id.length), [8, 8]);
});

test('loading shows the id on the agent\'s lines only, and the cap counts it', () =>
  world((w) => {
    seed(w, ['- My note: use tabs.', mine('Tests run with npm test.'), '* Starred fact <!-- dlne -->', 'Bare fact <!-- dlne -->']);
    w.write('AGENTS.md', '- An instruction that happens to end with the mark <!-- dlne -->\n');
    const sources = loadMemory(w.root, w.options);
    assert.equal(sources.project.text, [
      '- My note: use tabs.',
      '- [b468] Tests run with npm test.',
      `* [${idOf('Starred fact')}] Starred fact`,
      `- [${idOf('Bare fact')}] Bare fact`,
    ].join('\n'));
    assert.equal(sources.instructions.text, '- An instruction that happens to end with the mark', 'AGENTS.md has no ids');

    // The user's file has them too.
    w.write('.dlne/memory.md', mine('Prefers short answers.') + '\n', w.home);
    assert.equal(loadMemory(w.root, w.options).user.text, `- [${idOf('Prefers short answers.')}] Prefers short answers.`);

    // 380 facts of 17 characters: 7,599 without ids, past 8,000 with them, so the loaded text is cut.
    const many = Array.from({ length: 380 }, (_, n) => mine(`Fact ${String(n).padStart(3, '0')} is here.`));
    seed(w, many);
    const text = loadMemory(w.root, w.options).project.text;
    assert.ok(many.map((l) => l.replace(' <!-- dlne -->', '')).join('\n').length <= 8_000);
    assert.match(text, /\n\[… \d+ characters cut: the file is longer than DLNE loads\]$/);
  }));

// ---- Memory 2: replaces -------------------------------------------------------------------------------

test('replaces takes an id: a7f3, [a7f3], id: a7f3, in either case', () =>
  world((w) => {
    seed(w, [mine('Tests run with npm test.'), mine('Source is in src/.')]);
    const forms = ['b468', '[b468]', 'id: b468', 'ID:b468', '  [B468]  ', 'id b468'];
    for (const form of forms) {
      const check = checkRemember('project', 'Tests run with npm run test:all.', w.root, w.options, form);
      assert.equal(check.ready, true, form);
      assert.equal(check.replacedText, 'Tests run with npm test.', form);
      assert.equal(check.replacedId, 'b468', form);
    }
    const result = rememberFact('project', 'Tests run with npm run test:all.', w.root, w.options, '[b468]');
    assert.equal(result.message,
      `📝 Updated for this project [b468] → [${idOf('Tests run with npm run test:all.')}]: Tests run with npm test. → Tests run with npm run test:all.`);
    assert.deepEqual(factsIn(w), ['Tests run with npm run test:all.', 'Source is in src/.'], 'in place');
  }));

test('2026-10-30: replaces "project: <the fact>" now finds the fact (a dozen calls failed on the label)', () =>
  world((w) => {
    const fact = 'GA102 firmware: the SEC2 image is at 0x1c000 and is signed with the production key.';
    seed(w, ['- The user\'s note.', mine(fact), mine('Build with make -j8.')]);
    // As the model wrote it that day, and the other ways a model dresses up a quote.
    const quotes = [
      `project: ${fact}`,
      `Project memory: "${fact}"`,
      `project:${fact}`,
      `- [${idOf(fact)}] ${fact}`,
      `project: - ${fact}`,
      `\`${fact}\``,
      fact.replace(/\.$/, ''),
    ];
    for (const quote of quotes) {
      const check = checkRemember('project', '', w.root, w.options, quote);
      assert.equal(check.ready, true, `${quote}: ${check.ready ? '' : check.result.message}`);
      assert.equal(check.replacedText, fact, quote);
    }
    // And in the user's file, with "user:".
    w.write('.dlne/memory.md', mine('Prefers short answers.') + '\n', w.home);
    assert.equal(rememberFact('user', '', w.root, w.options, 'user: Prefers short answers.').message,
      `📝 Forgot for you [${idOf('Prefers short answers.')}]: Prefers short answers.`);
  }));

test('a quote cut short with … or ... picks out the fact, also as the no-match list shows it', () =>
  world((w) => {
    const fact = 'AD102 BTL1 header: os_data_off=0x7e00, patch_loc=0x7d00, num_sigs=2, sig_size=384.';
    seed(w, [mine(fact), mine('AD102 DMEM frame: 72-byte header, then zeros.')]);
    for (const quote of [`${fact.slice(0, 40)}…`, `${fact.slice(0, 40)}...`, `[${idOf(fact)}] "${fact.slice(0, 50)}…"`]) {
      const check = checkRemember('project', '', w.root, w.options, quote);
      assert.equal(check.ready && check.replacedText, fact, quote);
    }
  }));

test('a quote close in words picks out the fact; one close to two facts asks which', () =>
  world((w) => {
    const strip = 'Release builds use cargo build --release, then strip the binary in target/release.';
    const sign = 'Release builds use cargo build --release, then sign the binary in target/release.';
    const debug = 'Debug builds use cargo build, then run the tests.';
    seed(w, [mine(strip), mine(debug)]);
    // Not a part of the fact ("and then"), but 9 of its 10 words are there: 0.9, well ahead of the debug fact.
    const close = checkRemember('project', '', w.root, w.options, 'Release builds use cargo build --release and then strip the binary');
    assert.equal(close.ready && close.replacedText, strip);
    // Fewer than 4 words say too little to judge by.
    assert.match(checkRemember('project', '', w.root, w.options, 'release and strip').result.message, /^No fact you saved matches/);

    seed(w, [mine(strip), mine(sign), mine(debug)]);
    const both = rememberFact('project', '', w.root, w.options, 'release builds use cargo build --release and then the binary');
    assert.equal(both.ok, false);
    assert.equal(both.message, '"release builds use cargo build --release and then the binary" matches 2 facts you saved: ' +
      `[${idOf(strip)}] "${strip.slice(0, 50)}…"; [${idOf(sign)}] "${sign.slice(0, 50)}…". Give the id of the one you mean in replaces.`);
    assert.equal(factsIn(w).length, 3, 'nothing changed');
  }));

test('a line the user wrote is refused as theirs, by text or by words, and left as it is', () =>
  world((w) => {
    const theirs = 'Use tabs, never spaces, in every Python file of this repository.';
    seed(w, [`- ${theirs}`, mine('Tests run with npm test.')]);
    const before = fs.readFileSync(w.projectFile, 'utf8');
    for (const quote of [theirs, 'project: Use tabs, never spaces', 'use tabs and never spaces in every python file']) {
      const result = rememberFact('project', 'Use spaces.', w.root, w.options, quote);
      assert.deepEqual(result, {
        ok: false,
        message: 'That fact was written by the user, not by DLNE, so DLNE cannot change it. Ask the user, or save a new fact.',
      }, quote);
    }
    assert.equal(fs.readFileSync(w.projectFile, 'utf8'), before);
  }));

test('no match lists the saved facts with their ids, at most 40; with none saved it says so', () =>
  world((w) => {
    const long = 'The coordinator listens on port 8443 behind Caddy, which renews its certificate.';
    seed(w, ['- The user\'s note.', mine(long), mine('Source is in src/.')]);
    assert.equal(rememberFact('project', '', w.root, w.options, 'GA102 DMEM pattern').message,
      'No fact you saved matches "GA102 DMEM pattern". Yours: ' +
      `[${idOf(long)}] "${long.slice(0, 50)}…"; [${idOf('Source is in src/.')}] "Source is in src/.". Give the id in replaces.`);
    // An id no fact has is no match either; the quote is cut at 80 characters.
    assert.match(rememberFact('project', '', w.root, w.options, 'ffff').message, /^No fact you saved matches "ffff"\. Yours: /);
    const wordy = 'q'.repeat(100);
    assert.match(rememberFact('project', '', w.root, w.options, wordy).message, new RegExp(`^No fact you saved matches "${'q'.repeat(80)}"\\. `));

    seed(w, Array.from({ length: 45 }, (_, n) => mine(`Fact number ${n}.`)));
    assert.match(rememberFact('project', '', w.root, w.options, 'nothing like it').message,
      /"Fact number 39\."(?!;) and 5 more\. Give the id in replaces\.$/);

    seed(w, ['- Only the user\'s note.']);
    assert.equal(rememberFact('project', '', w.root, w.options, 'Tests run').message,
      'No fact you saved matches "Tests run"; you have not saved any in this file.');
  }));

test('several facts holding the quote: each listed with its id', () =>
  world((w) => {
    seed(w, [mine('AD102 BTL1 header.'), mine('AD102 DMEM frame.')]);
    assert.equal(rememberFact('project', '', w.root, w.options, 'ad102').message,
      `"ad102" matches 2 facts you saved: [${idOf('AD102 BTL1 header.')}] "AD102 BTL1 header."; ` +
      `[${idOf('AD102 DMEM frame.')}] "AD102 DMEM frame.". Give the id of the one you mean in replaces.`);
  }));

test('a full file refuses growth, listing the agent\'s facts largest first, but an update that shrinks it is allowed', () =>
  world((w) => {
    const [a, b, c] = ['a'.repeat(300), 'b'.repeat(500), 'c'.repeat(100)];
    seed(w, ['x'.repeat(7_000), mine(a), mine(b), mine(c)]);
    const used = loadMemory(w.root, w.options).project.text.length;
    assert.ok(used > 7_900 && used <= 8_000, String(used));

    const grow = rememberFact('project', 'A new fact of a hundred characters, give or take, which there is no room left for at all.', w.root, w.options);
    assert.equal(grow.ok, false);
    assert.equal(grow.message, `The project memory is full (${used} of 8000 characters), so nothing was saved. ` +
      'Make room first: merge or shorten facts with replaces, or remove ones that no longer matter. Yours, largest first: ' +
      `[${idOf(b)}] (500 chars) "${'b'.repeat(50)}…"; [${idOf(a)}] (300 chars) "${'a'.repeat(50)}…"; [${idOf(c)}] (100 chars) "${'c'.repeat(50)}…"`);

    // Shorter: allowed. Longer than there is room for: refused, the same way.
    assert.equal(rememberFact('project', 'b'.repeat(200), w.root, w.options, idOf(b)).ok, true);
    assert.match(rememberFact('project', 'c'.repeat(600), w.root, w.options, idOf(c)).message, /^The project memory is full/);
    assert.deepEqual(factsIn(w), [a, 'b'.repeat(200), c]);
  }));

test('a file the user filled past the cap by hand: shrinking and removing still work, growing does not', () =>
  world((w) => {
    const fact = 'd'.repeat(200);
    seed(w, ['x'.repeat(8_100), mine(fact)]);
    assert.match(loadMemory(w.root, w.options).project.text, /characters cut/);
    assert.match(rememberFact('project', 'A new fact.', w.root, w.options).message, /^The project memory is full \(\d+ of 8000 characters\)/);
    assert.match(rememberFact('project', 'd'.repeat(201), w.root, w.options, idOf(fact)).message, /^The project memory is full/, 'one character more');
    assert.equal(rememberFact('project', 'e'.repeat(200), w.root, w.options, idOf(fact)).ok, true, 'the same size is allowed');
    assert.equal(rememberFact('project', 'e'.repeat(120), w.root, w.options, idOf('e'.repeat(200))).ok, true, 'smaller is allowed');
    assert.equal(rememberFact('project', '', w.root, w.options, idOf('e'.repeat(120))).ok, true, 'a removal is allowed');
    assert.deepEqual(factsIn(w), []);
  }));

test('the messages carry ids: saved, updated, forgot, and a fact saved in parts', () =>
  world((w) => {
    const saved = rememberFact('project', 'Source is in src/.', w.root, w.options);
    const id = idOf('Source is in src/.');
    assert.equal(saved.message, `📝 Remembered for this project [${id}]: Source is in src/.`);
    assert.deepEqual(saved.ids, [id]);

    const updated = rememberFact('project', 'Source is in lib/.', w.root, w.options, id);
    assert.equal(updated.message, `📝 Updated for this project [${id}] → [${idOf('Source is in lib/.')}]: Source is in src/. → Source is in lib/.`);
    assert.deepEqual(updated.ids, [idOf('Source is in lib/.')]);

    const forgot = rememberFact('project', '', w.root, w.options, idOf('Source is in lib/.'));
    assert.equal(forgot.message, `📝 Forgot for this project [${idOf('Source is in lib/.')}]: Source is in lib/.`);
    assert.deepEqual(forgot.ids, []);

    // Two sentences of about 300 and 240 characters: together over 500, so each is a part of its own.
    const one = `First half of a long note: ${'alpha beta gamma delta '.repeat(12).trim()}.`;
    const two = `Second half: ${'epsilon zeta eta theta '.repeat(10).trim()}.`;
    const parts = rememberFact('project', `${one} ${two}`, w.root, w.options);
    assert.equal(parts.message, [
      `📝 Remembered for this project [${idOf(one)}]: ${one}`,
      `📝 Remembered for this project [${idOf(two)}]: ${two}`,
      'It is over 500 characters, so it was saved as these 2 facts, one after the other. That is expected: nothing more to do.',
    ].join('\n'));
    assert.deepEqual(parts.ids, [idOf(one), idOf(two)]);

    assert.equal(rememberFact('user', 'Likes Rust.', w.root, w.options).message, `📝 Remembered for you [${idOf('Likes Rust.')}]: Likes Rust.`);
  }));

test('the ids in a message are the ones the next load shows, a clash included', () =>
  world((w) => {
    rememberFact('project', 'Fact 59.', w.root, w.options);
    const second = rememberFact('project', 'Fact 148.', w.root, w.options);
    assert.deepEqual(second.ids, ['e5564'], 'the clash makes it 5 characters');
    assert.equal(loadMemory(w.root, w.options).project.text, '- [e5569] Fact 59.\n- [e5564] Fact 148.');
    assert.equal(checkRemember('project', '', w.root, w.options, 'e5569').replacedText, 'Fact 59.');
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
