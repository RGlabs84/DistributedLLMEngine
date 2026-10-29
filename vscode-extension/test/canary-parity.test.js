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
 * assignments, the `_tool` helper and `_context_message`), then prints JSON. It runs here, on the laptop,
 * because the CI copy on TheWall has no docs/.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
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
// engine/canary.py, read without importing it
// ---------------------------------------------------------------------------------------------

const READ_CANARY = String.raw`
import ast, json, random, sys

tree = ast.parse(open(sys.argv[1], encoding="utf-8").read())
values = {"_AGENT_SYSTEM_PROMPT", "_AGENT_TOOLS", "_PROJECTS", "CONTEXT_OPEN", "CONTEXT_INTRO", "CONTEXT_HEADINGS",
          "CONTEXT_CLOSE", "CONTEXT_SHARE", "_CONTEXT_AGENTS", "_CONTEXT_PROJECT", "_CONTEXT_USER", "_CONTEXT_SECTIONS"}
code = {"_tool", "_context_message", "_Sample"}
keep = []
for node in tree.body:
    if isinstance(node, (ast.Import, ast.ImportFrom)) and (getattr(node, "module", None) or node.names[0].name) in ("typing", "random", "dataclasses"):
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
    "open": ns["CONTEXT_OPEN"], "intro": ns["CONTEXT_INTRO"], "headings": ns["CONTEXT_HEADINGS"], "close": ns["CONTEXT_CLOSE"],
    "share": ns["CONTEXT_SHARE"], "agents": ns["_CONTEXT_AGENTS"], "project_lines": ns["_CONTEXT_PROJECT"],
    "user_lines": ns["_CONTEXT_USER"], "blocks": blocks,
}))
`;

const canary = JSON.parse(execFileSync('python3', ['-I', '-c', READ_CANARY, path.join(REPO, 'engine', 'canary.py'), '600'],
  { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 }));

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

test('the contract\'s remember tool is the last entry of ToolRegistry.getToolDefinitions()', () => {
  const doc = JSON.parse(fromDoc('REMEMBER_TOOL'));
  const tools = ToolRegistry.getToolDefinitions();
  assert.equal(tools.length, 8, 'the seven tools of today plus remember');
  assert.deepEqual(tools[tools.length - 1], doc);
});

test('the contract\'s remember tool is the last entry of the canary\'s _AGENT_TOOLS', () => {
  const doc = JSON.parse(fromDoc('REMEMBER_TOOL'));
  assert.equal(canary.tools.length, 8);
  assert.deepEqual(canary.tools[canary.tools.length - 1], doc);
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
