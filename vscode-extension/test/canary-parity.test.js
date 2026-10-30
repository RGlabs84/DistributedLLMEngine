'use strict';
/**
 * Canary parity (docs/agent-memory.md section 9.1).
 *
 * The coordinator's canaries are decoy requests that must be indistinguishable from the extension's real
 * ones, so the three things a request is built from - the system prompt, the tool list and the optional
 * <dlne_context> message - must be the same on both sides. This file makes the contract the single source of
 * truth: it reads the text out of docs/agent-memory.md (between the AGENT_SYSTEM_PROMPT, REMEMBER_TOOL and
 * CONTEXT_BLOCK markers) and compares the extension (dist/) and engine/canary.py against it.
 *
 * engine/canary.py is read without importing it (it imports the engine): a small Python program run with
 * `python3 -I` parses the file with `ast` and runs only the pieces that build these values (literal
 * assignments, the `_tool` helper, `_context_message` and the fact-id functions it uses), then prints JSON.
 * It runs here, on the laptop, because the CI copy on TheWall has no docs/.
 *
 * Memory 2 (docs/agent-memory.md section 7.8) shows the agent its own facts with an id in front, `- [a7f3] <fact>`,
 * computed from the fact (SHA-256 of its normalised text). So decoy memory sections carry ids too, and they must
 * be the ids the extension would compute: this file works them out a third way, with Node's crypto, and checks
 * canary.py and the extension's memory.ts against that and against each other.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

const stub = require('./helpers/vscode-stub');
stub.vscode.languages = { getDiagnostics: () => [] };
stub.vscode.DiagnosticSeverity = { Error: 0, Warning: 1 };

const { AGENT_SYSTEM_PROMPT } = require('../dist/agent/agentLoop');
const { ToolRegistry } = require('../dist/agent/tools');
const { redactSecrets } = require('../dist/privacy/redact');

const REPO = path.resolve(__dirname, '..', '..');
const DOC = fs.readFileSync(path.join(REPO, 'docs', 'agent-memory.md'), 'utf8');

/** The text of the fenced block between `<!-- NAME begin -->` and `<!-- NAME end -->`, fence lines excluded. */
function fromDoc(name) {
  const marked = DOC.match(new RegExp(`<!-- ${name} begin -->\\n(\`{3,})[a-z]*\\n([\\s\\S]*?)\\n\\1\\n<!-- ${name} end -->`));
  assert.ok(marked, `docs/agent-memory.md has no ${name} block`);
  return marked[2];
}

// ---------------------------------------------------------------------------------------------
// Fact ids, worked out here from the contract (section 7.8)
// ---------------------------------------------------------------------------------------------

/** The normalisation an id is computed from: comments removed, one list marker stripped, whitespace collapsed, lowercased. */
function factKey(text) {
  return text.replace(/<!--[\s\S]*?-->/g, '').replace(/^\s*[-*]\s+/, '').replace(/\s+/g, ' ').trim().toLowerCase();
}

/** The ids of one file's agent lines, in order: 4 hex of SHA-256, longer (up to 8) for all of them when two share theirs. */
function contractIds(lines) {
  const digests = lines.map((line) => crypto.createHash('sha256').update(factKey(line), 'utf8').digest('hex'));
  let length = 4;
  while (length < 8 && new Set(digests.map((d) => d.slice(0, length))).size !== digests.length) length++;
  return digests.map((d) => d.slice(0, length));
}

// Single facts, and whole files' agent lines. The hex values in the tests below were worked out with sha256sum.
const ID_FACTS = [
  'Tests run with `make test` from the repository root.',
  '- Prefers short answers and small diffs. <!-- dlne -->',
  '  *   Two\t spaces   and\nA BREAK  ',
  '<!-- a --> - x <!-- b\nc -->',
  '- - nested list marker',
  'Ünïcode is hashed as UTF-8: café, naïve, Straße.',
];
const ID_FILES = [
  ['Tests run with `make test` from the repository root.', 'Prefers short answers and small diffs.'],
  // the first two share f056, so the whole file takes 5 characters
  ['Fact number 222 for the collision test.', 'Fact number 739 for the collision test.', 'Prefers short answers and small diffs.'],
  // the same fact twice never separates: 8 characters
  ['- Same fact. <!-- dlne -->', '- same   FACT.'],
  [],
];

// ---------------------------------------------------------------------------------------------
// engine/canary.py, read without importing it
// ---------------------------------------------------------------------------------------------

const READ_CANARY = String.raw`
import ast, json, random, sys

tree = ast.parse(open(sys.argv[1], encoding="utf-8").read())
values = {"_AGENT_SYSTEM_PROMPT", "_AGENT_TOOLS", "_PROJECTS", "CONTEXT_OPEN", "CONTEXT_INTRO", "CONTEXT_HEADINGS",
          "CONTEXT_CLOSE", "CONTEXT_SHARE", "_CONTEXT_AGENTS", "_CONTEXT_PROJECT", "_CONTEXT_USER", "_CONTEXT_SECTIONS",
          "_MEMORY_STYLES", "_IMAGE_TOOL", "_IMAGE_PROMPT_LINE", "_IMAGE_PROMPT_MARKER", "_VIEW_IMAGE_TOOL", "_VIEW_IMAGE_PROMPT_LINE"}
code = {"_tool", "_context_message", "_Sample", "fact_key", "fact_id", "fact_ids", "_memory_section", "agent_system_prompt", "agent_tools"}
keep = []
for node in tree.body:
    if isinstance(node, (ast.Import, ast.ImportFrom)) and (getattr(node, "module", None) or node.names[0].name) in ("typing", "random", "dataclasses", "hashlib", "re"):
        keep.append(node)
    elif isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in code:
        keep.append(node)
    elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in values for t in node.targets):
        keep.append(node)
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id in values:
        keep.append(node)
ns = {"__name__": "canary_parity"}
exec(compile(ast.Module(body=keep, type_ignores=[]), "canary.py", "exec"), ns)

langs = {"python": "svc/settings.py", "typescript": "src/apiClient.ts", "bash": "scripts/deploy_x.sh",
         "yaml": "docker-compose.yml", "json": "config/app.json", "go": "internal/config/config.go"}
blocks = []
for seed in range(int(sys.argv[2])):
    r = random.Random(seed)
    lang = list(langs)[seed % len(langs)]
    project = r.choice(ns["_PROJECTS"])
    sample = ns["_Sample"](langs[lang], lang, "", [])
    blocks.append({"lang": lang, "project": project, "path": langs[lang], "content": ns["_context_message"](sample, project, r)})
print(json.dumps({
    "prompt": ns["_AGENT_SYSTEM_PROMPT"], "tools": ns["_AGENT_TOOLS"], "projects": ns["_PROJECTS"],
    "prompt_both": ns["agent_system_prompt"](True, True), "tools_both": ns["agent_tools"](True, True),
    "view_tool": ns["_VIEW_IMAGE_TOOL"], "view_line": ns["_VIEW_IMAGE_PROMPT_LINE"],
    "open": ns["CONTEXT_OPEN"], "intro": ns["CONTEXT_INTRO"], "headings": ns["CONTEXT_HEADINGS"], "close": ns["CONTEXT_CLOSE"],
    "share": ns["CONTEXT_SHARE"], "agents": ns["_CONTEXT_AGENTS"], "project_lines": ns["_CONTEXT_PROJECT"],
    "user_lines": ns["_CONTEXT_USER"], "blocks": blocks,
    "fact_ids": [ns["fact_id"](fact) for fact in json.loads(sys.argv[3])],
    "file_ids": [[ns["fact_ids"](lines)[line] for line in lines] for lines in json.loads(sys.argv[4])],
}))
`;

const canary = JSON.parse(execFileSync('python3', ['-I', '-c', READ_CANARY, path.join(REPO, 'engine', 'canary.py'), '600',
  JSON.stringify(ID_FACTS), JSON.stringify(ID_FILES)], { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 }));

// ---------------------------------------------------------------------------------------------
// The context block as the contract writes it (section 7.3)
// ---------------------------------------------------------------------------------------------

/** Open and close tags, intro line and the three heading lines of the contract's template, in order. */
function contextFormat() {
  const lines = fromDoc('CONTEXT_BLOCK').split('\n');
  const headings = lines.filter((l) => l.startsWith('## '));
  assert.equal(headings.length, 3, 'the contract names three section headings');
  return { open: lines[0], intro: lines[1], headings, close: lines[lines.length - 1], lines };
}

/** The block the contract describes for these section texts (heading names in the contract's order), or null. */
function renderPerContract(texts) {
  const { open, intro, headings, close } = contextFormat();
  const sections = ['instructions', 'project', 'user'].map((k, i) => (texts[k] ? `${headings[i]}\n${texts[k]}` : null)).filter(Boolean);
  return sections.length ? `${open}\n${intro}\n\n${sections.join('\n\n')}\n${close}` : null;
}

/** Split a rendered block back into its section texts, insisting on the contract's exact layout. */
function parseBlock(content) {
  const { open, intro, headings, close } = contextFormat();
  assert.ok(content.startsWith(`${open}\n${intro}\n\n`), `bad opening: ${content.slice(0, 160)}`);
  assert.ok(content.endsWith(`\n${close}`), 'bad closing');
  const inner = content.slice(`${open}\n${intro}\n\n`.length, content.length - `\n${close}`.length);
  const names = ['instructions', 'project', 'user'];
  const texts = {};
  let rest = inner;
  let from = 0;
  for (let i = 0; i < 3; i++) {
    if (!rest.startsWith(headings[i] + '\n')) continue;
    const body = rest.slice(headings[i].length + 1);
    // the text runs to the next heading that follows an empty line, or to the end
    let end = body.length;
    for (let j = i + 1; j < 3; j++) {
      const at = body.indexOf(`\n\n${headings[j]}\n`);
      if (at !== -1) end = Math.min(end, at);
    }
    texts[names[i]] = body.slice(0, end);
    rest = body.slice(end).replace(/^\n\n/, '');
    from = i + 1;
  }
  assert.equal(rest, '', `text after the sections (or sections out of order) from ${from}: ${rest.slice(0, 100)}`);
  assert.ok(Object.keys(texts).length >= 1);
  return texts;
}

// ---------------------------------------------------------------------------------------------
// 1. The system prompt
// ---------------------------------------------------------------------------------------------

test('the contract\'s system prompt is word for word the extension\'s AGENT_SYSTEM_PROMPT', () => {
  const doc = fromDoc('AGENT_SYSTEM_PROMPT');
  assert.ok(doc.startsWith('You are the DLNE coding agent') && doc.endsWith('Begin with a short <think> plan.'));
  assert.equal(AGENT_SYSTEM_PROMPT, doc);
});

test('the contract\'s system prompt is word for word the canary\'s _AGENT_SYSTEM_PROMPT', () => {
  assert.equal(canary.prompt, fromDoc('AGENT_SYSTEM_PROMPT'));
});

// ---------------------------------------------------------------------------------------------
// 2. The tools
// ---------------------------------------------------------------------------------------------

test('the contract\'s remember tool is the eighth entry of ToolRegistry.getToolDefinitions()', () => {
  const doc = JSON.parse(fromDoc('REMEMBER_TOOL'));
  const tools = ToolRegistry.getToolDefinitions();
  assert.equal(tools.length, 10, 'the seven tools of today plus remember, use_skill and save_skill');
  assert.deepEqual(tools[7], doc);
});

test('the contract\'s remember tool is the eighth entry of the canary\'s _AGENT_TOOLS', () => {
  const doc = JSON.parse(fromDoc('REMEMBER_TOOL'));
  assert.equal(canary.tools.length, 10);
  assert.deepEqual(canary.tools[7], doc);
});

test('the contract\'s skill tools are the ninth and tenth entries of the extension\'s tool list and of the canary\'s, key order included', () => {
  const docs = [JSON.parse(fromDoc('USE_SKILL_TOOL')), JSON.parse(fromDoc('SAVE_SKILL_TOOL'))];
  const tools = ToolRegistry.getToolDefinitions();
  assert.deepEqual(tools.slice(8), docs);
  assert.deepEqual(canary.tools.slice(8), docs);
  // the engine's cache sees bytes: the keys come in the same order too
  assert.equal(JSON.stringify(tools.slice(8)), JSON.stringify(docs));
  assert.equal(JSON.stringify(canary.tools.slice(8)), JSON.stringify(docs));
  assert.deepEqual(docs.map((t) => t.function.name), ['use_skill', 'save_skill']);
  assert.deepEqual(docs[1].function.parameters.required, ['name', 'description', 'instructions', 'scope']);
});

test('the contract\'s view_image tool is the canary\'s and the extension\'s, key order included, and the lists with it agree', () => {
  const { viewImageToolDefinition, VIEW_IMAGE_PROMPT_LINE, withViewImageLine } = require('../dist/agent/viewImage');
  const { imageToolDefinition, withImageLine } = require('../dist/agent/imageTool');
  const doc = JSON.parse(fromDoc('VIEW_IMAGE_TOOL'));
  assert.deepEqual(viewImageToolDefinition(), doc);
  assert.equal(JSON.stringify(viewImageToolDefinition()), JSON.stringify(doc));
  assert.equal(JSON.stringify(canary.view_tool), JSON.stringify(doc));
  assert.equal(canary.view_line, VIEW_IMAGE_PROMPT_LINE);
  // generate_image, then view_image, last; and the prompt gains the two lines in that order
  const own = JSON.parse(JSON.stringify([...ToolRegistry.getToolDefinitions(), imageToolDefinition(), viewImageToolDefinition()]));
  assert.equal(JSON.stringify(canary.tools_both), JSON.stringify(own));
  assert.equal(canary.prompt_both, withViewImageLine(withImageLine(AGENT_SYSTEM_PROMPT)));
});

test('the canary\'s whole tool list is the extension\'s, in the same order', () => {
  assert.deepEqual(canary.tools, JSON.parse(JSON.stringify(ToolRegistry.getToolDefinitions())));
});

// ---------------------------------------------------------------------------------------------
// 3. The <dlne_context> message
// ---------------------------------------------------------------------------------------------

test('the canary\'s context constants and every decoy block follow the contract\'s format', () => {
  const format = contextFormat();
  assert.equal(canary.open, format.open);
  assert.equal(canary.intro, format.intro);
  assert.equal(canary.close, format.close);
  assert.deepEqual(['instructions', 'project', 'user'].map((k) => canary.headings[k]), format.headings);

  let carrying = 0;
  const combos = new Set();
  for (const { content } of canary.blocks) {
    assert.equal(content.role, 'user');
    const texts = parseBlock(content.content);
    for (const text of Object.values(texts)) assert.ok(text.length > 0 && text === text.trim());
    assert.equal(renderPerContract(texts), content.content, 'a decoy block differs from the contract\'s rendering');
    combos.add(Object.keys(texts).join('+'));
    carrying++;
    assert.ok(!content.content.includes('<dlne_working_memory>'), 'a canary never carries working memory');
  }
  assert.equal(carrying, canary.blocks.length);
  assert.equal(combos.size, 7, `every combination of sections comes up: ${[...combos]}`);
  assert.ok(canary.share > 0.3 && canary.share < 0.5, 'about two in five canaries carry a block');
});

test('the extension\'s CONTEXT_* constants and a rendered block are the contract\'s, and render the decoys identically', () => {
  const memory = require('../dist/agent/memory');
  const format = contextFormat();
  assert.equal(memory.CONTEXT_OPEN, format.open);
  assert.equal(memory.CONTEXT_INTRO, format.intro);
  assert.equal(memory.CONTEXT_CLOSE, format.close);
  assert.deepEqual(Object.values(memory.CONTEXT_HEADINGS), format.headings);

  const source = (label, text) => ({ label, path: `/x/${label}`, text });
  const rendered = memory.renderContextBlock({
    instructions: source('instructions', 'A text'), project: source('project', '- B fact'), user: source('user', '- C pref'),
  });
  assert.equal(rendered, renderPerContract({ instructions: 'A text', project: '- B fact', user: '- C pref' }));
  assert.equal(memory.renderContextBlock({}), null);

  // The extension's renderer given the sections of each decoy gives the decoy's bytes back.
  for (const { content } of canary.blocks.slice(0, 200)) {
    const texts = parseBlock(content.content);
    const sources = {};
    for (const [label, text] of Object.entries(texts)) sources[label] = source(label, text);
    assert.equal(memory.renderContextBlock(sources), content.content);
  }
});

test('nothing in a decoy context block is changed by the extension\'s redactor', () => {
  // The blocks as built, and every text the canary can pick, filled in for several projects and files.
  const texts = canary.blocks.map((b) => b.content.content);
  const fills = [['orders', 'svc/settings.py'], ['search-api', 'src/apiClient.ts'], ['sync-worker', 'internal/config/config.go']];
  for (const [p, file] of fills) {
    const fill = (t) => t.replaceAll('{p}', p).replaceAll('{path}', file);
    for (const variants of Object.values(canary.agents)) texts.push(...variants.map(fill));
    texts.push(...canary.project_lines.map(fill), ...canary.user_lines.map(fill));
  }
  assert.ok(texts.length > 700);
  for (const text of texts) {
    for (const name of ['AGENTS.md', 'memory.md', 'notes.py', 'notes.ts']) {
      const result = redactSecrets(text, name);
      assert.equal(result.count, 0, `the redactor would change: ${text}`);
      assert.equal(result.text, text);
    }
  }
  // and the bullet form a memory file holds, line by line
  for (const line of canary.project_lines.concat(canary.user_lines)) assert.equal(redactSecrets('- ' + line, 'memory.md').count, 0);
});

// ---------------------------------------------------------------------------------------------
// 4. Fact ids (Memory 2, section 7.8)
// ---------------------------------------------------------------------------------------------

const ID_LINE = /^- \[([0-9a-f]{4,8})\] (.+)$/;

/** A decoy's memory sections (project and user), each as its lines split into the agent's facts and the user's. */
function decoyMemoryFiles() {
  const files = [];
  for (const { content } of canary.blocks) {
    const texts = parseBlock(content.content);
    for (const label of ['project', 'user']) {
      if (!texts[label]) continue;
      const lines = texts[label].split('\n').map((line) => {
        const mine = line.match(ID_LINE);
        return mine ? { agent: true, id: mine[1], fact: mine[2] } : { agent: false, fact: line.replace(/^- /, '') };
      });
      files.push({ label, text: texts[label], lines });
    }
  }
  return files;
}

test('the contract\'s id rule, worked out with Node\'s crypto, gives the known values', () => {
  assert.deepEqual(ID_FACTS.map((fact) => contractIds([fact])[0]), ['f6d5', '6675', 'cde9', '2d71', '3067', '4802']);
  assert.deepEqual(ID_FILES.map(contractIds), [['f6d5', '6675'], ['f0566', 'f0560', '66759'], ['daa7a650', 'daa7a650'], []]);
});

test('the canary\'s fact ids are the contract\'s: SHA-256 of the normalised fact, 4 hex, the collision rule', () => {
  assert.deepEqual(canary.fact_ids, ID_FACTS.map((fact) => contractIds([fact])[0]));
  assert.deepEqual(canary.file_ids, ID_FILES.map(contractIds));
});

test('the extension\'s factId and factIds are the contract\'s too', () => {
  // memory.ts gains these with Memory 2 (agent A's part); until that is merged this test fails, and should.
  const memory = require('../dist/agent/memory');
  assert.equal(typeof memory.factId, 'function', 'dist/agent/memory exports factId');
  assert.equal(typeof memory.factIds, 'function', 'dist/agent/memory exports factIds');
  assert.deepEqual(ID_FACTS.map((fact) => memory.factId(fact)), canary.fact_ids);
  // factIds reads file lines and gives ids to the agent's own (the ones with its mark), so the facts go in as such lines.
  for (const [i, facts] of ID_FILES.entries()) {
    // An entry already written as a file line ("- fact <!-- dlne -->") keeps one bullet and one mark.
    const lines = facts.map((fact) =>
      `- ${fact.replace(/<!--[\s\S]*?-->/g, '').trim().replace(/^[-*]\s+/, '')} ${memory.AGENT_MARK}`);
    const ids = memory.factIds(lines);
    assert.deepEqual(lines.map((line) => ids.get(line)), canary.file_ids[i], `file ${i}`);
  }
});

test('every agent fact in a decoy carries the id of its fact, and the files mix the agent\'s lines and the user\'s', () => {
  const kinds = { agent: 0, user: 0, mixed: 0 };
  for (const file of decoyMemoryFiles()) {
    const mine = file.lines.filter((line) => line.agent);
    // the collision rule runs over this file's agent lines only, so the user's lines never lengthen an id
    assert.deepEqual(mine.map((line) => line.id), contractIds(mine.map((line) => line.fact)), file.text);
    kinds[mine.length === file.lines.length ? 'agent' : mine.length === 0 ? 'user' : 'mixed']++;
  }
  const total = kinds.agent + kinds.user + kinds.mixed;
  for (const [kind, n] of Object.entries(kinds)) assert.ok(n > total * 0.1, `${kind} files come up often: ${JSON.stringify(kinds)}`);
});

test('the extension loads a decoy\'s memory files into exactly the text the decoy shows', () => {
  // The decoy's facts written back as files (the agent's with the marker), loaded by the extension, must give the
  // decoy's section back byte for byte: ids, the user's lines and the layout. Fails until Memory 2's loader is merged.
  const memory = require('../dist/agent/memory');
  const base = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-canary-ids-'));
  const folders = stub.vscode.workspace.workspaceFolders;
  try {
    const root = path.join(base, 'project');
    const home = path.join(base, 'home');
    stub.vscode.workspace.workspaceFolders = [{ uri: { fsPath: root } }];
    const header = '# DLNE memory\n\n<!-- What the DLNE agent remembers. Add, edit or delete lines freely; keep secrets out. -->\n\n';
    for (const file of decoyMemoryFiles().slice(0, 150)) {
      const target = file.label === 'project' ? path.join(root, '.dlne', 'memory.md') : path.join(home, '.dlne', 'memory.md');
      fs.rmSync(base, { recursive: true, force: true });
      fs.mkdirSync(path.dirname(target), { recursive: true });
      fs.mkdirSync(root, { recursive: true });
      fs.mkdirSync(home, { recursive: true });
      const lines = file.lines.map((line) => (line.agent ? `- ${line.fact} <!-- dlne -->` : `- ${line.fact}`));
      fs.writeFileSync(target, header + lines.join('\n') + '\n');
      const loaded = memory.loadMemory(root, { homeDir: home });
      assert.equal(loaded[file.label] && loaded[file.label].text, file.text);
    }
  } finally {
    stub.vscode.workspace.workspaceFolders = folders;
    fs.rmSync(base, { recursive: true, force: true });
  }
});
