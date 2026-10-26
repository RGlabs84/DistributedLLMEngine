'use strict';
/**
 * What a new user met on a first run, seen while recording the DLNE demo with extension 1.3.0 (26 Oct 2026),
 * fixed in 1.3.2:
 * 1. the chat vanished when the agent wrote a file (an explorer refresh took over the side bar);
 * 2. nothing moved while a big file was being written (82 s for a snake game), and a "Waiting for the swarm"
 *    line stayed on screen after the answer;
 * 3. the agent's answer showed raw Markdown (**Gameplay**, backticks);
 * 4. autocomplete stopped mid-expression ("text.split('')."), offered nothing just inside `{}`, and failed silently;
 * 5. a machine without a user systemd logged "Failed to connect to user scope bus" every 5 seconds.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const childProcess = require('node:child_process');

const stub = require('./helpers/vscode-stub');
const swarm = require('./helpers/mock-swarm');
const { ToolRegistry } = require('../dist/agent/tools');
const { AgentLoop, toolProgressLine } = require('../dist/agent/agentLoop');
const { DlneClient } = require('../dist/client/dlneClient');
const { renderMarkdown } = require('../dist/views/markdown');
const { DlneFimProvider, buildFimContext, closesBlock, trimCutOff } = require('../dist/completion/fimProvider');
const { WorkerManager } = require('../dist/worker/workerManager');
const { forgetPrivacyModes } = require('../dist/privacy');

process.env.HOME = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-first-run-home-'));
WorkerManager.prototype.init = async function init() {};
const SRC = path.join(__dirname, '..', 'src');

// ---- 1. the chat stays in view while the agent writes ----------------------------------------------------------

test('writing a file never switches the side bar, so the DLNE chat stays in view', async () => {
  stub.reset();
  ToolRegistry.resetApprovals();
  const realApprover = ToolRegistry.approver;
  ToolRegistry.approver = async () => 'once';
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-first-run-ws-'));
  stub.vscode.workspace.workspaceFolders = [{ uri: { fsPath: root } }];
  try {
    const result = await ToolRegistry.executeTool('write_file', { path: 'snake.html', content: '<html></html>\n' });
    assert.equal(result.is_error, undefined, result.output);
    assert.equal(fs.readFileSync(path.join(root, 'snake.html'), 'utf8'), '<html></html>\n');
    assert.deepEqual(stub.state.commands.filter(([id]) => /explorer|files\.action|workbench\.view/i.test(id)), []);
  } finally {
    stub.vscode.workspace.workspaceFolders = undefined;
    ToolRegistry.approver = realApprover;
    fs.rmSync(root, { recursive: true, force: true });
  }
  const tools = fs.readFileSync(path.join(SRC, 'agent', 'tools.ts'), 'utf8');
  assert.doesNotMatch(tools, /executeCommand\(\s*'workbench\.(files\.action\.refreshFilesExplorer|view\.explorer)/);
});

// ---- 2. a file being written shows that it is being written --------------------------------------------------

test('the status line says which file is being written and how much has come', () => {
  const partial = '{"path": "snake.html", "content": "' + 'x'.repeat(9400);
  assert.equal(toolProgressLine('write_file', partial), '✍️ Writing snake.html (9.2 KB so far)…');
  assert.equal(toolProgressLine('write_file', '{"pa'), '✍️ Writing a file (4 bytes so far)…');
  assert.equal(toolProgressLine('edit_file_diff', '{"path":"src\\\\app.ts","search_block":"a'), '✍️ Preparing an edit to src\\app.ts (39 bytes so far)…');
  assert.equal(toolProgressLine('run_terminal_command', '{"command":"npm te'), '⌨️ Preparing a command…');
  assert.equal(toolProgressLine('', ''), '🔧 Preparing the next step…');
});

test('the client hands on a native tool call\'s arguments as they arrive', async () => {
  stub.reset();
  forgetPrivacyModes();
  const c = await swarm.startCoordinator({
    status: swarm.statusBody(),
    chat: (ctx) => swarm.plainStream(ctx.res, [
      { tool_calls: [{ index: 0, id: 'call_1', function: { name: 'write_file', arguments: '{"path":"snake.html",' } }] },
      { tool_calls: [{ index: 0, function: { arguments: '"content":"<html>' } }] },
      { tool_calls: [{ index: 0, function: { arguments: '</html>"}' } }] },
    ]),
  });
  stub.state.settings['dlne.serverUrl'] = c.url;
  try {
    const seen = [];
    const result = await new DlneClient().streamChat([{ role: 'user', content: 'x' }], [], () => {}, undefined, undefined, undefined, {
      onToolCallProgress: (name, soFar) => seen.push([name, soFar]),
    });
    assert.deepEqual(seen.map(([name]) => name), ['write_file', 'write_file', 'write_file']);
    assert.equal(seen[2][1], '{"path":"snake.html","content":"<html></html>"}');
    assert.equal(result.toolCalls[0].arguments, seen[2][1]);
  } finally {
    c.close();
  }
});

test('the agent turns those pieces into the status line, for native calls and for calls written inline', async () => {
  const lines = [];
  const callbacks = {
    onThinkingDelta() {}, onContentDelta() {}, onToolStart() {}, onToolEnd() {},
    onLoadingProgress: (text) => lines.push(text),
  };
  const native = {
    streamChat: async (messages, tools, onDelta, onToolCallStart, signal, onLoadingProgress, options) => {
      options.onToolCallProgress('write_file', '{"path": "snake.html", "content": "' + 'x'.repeat(2000));
      options.onToolCallProgress('write_file', '{"path": "snake.html", "content": "' + 'x'.repeat(2100)); // within 0.5 s: not shown
      return { content: 'done', toolCalls: [] };
    },
  };
  await new AgentLoop(native).run('make a game', callbacks);
  assert.deepEqual(lines, ['✍️ Writing snake.html (2.0 KB so far)…']);

  lines.length = 0;
  const inline = {
    streamChat: async (messages, tools, onDelta) => {
      onDelta('Writing it now.\n<tool_call>{"name": "write_file", "arguments": {"path": "b.txt", "content": "abc');
      return { content: '', toolCalls: [] };
    },
  };
  await new AgentLoop(inline).run('write b', callbacks);
  assert.equal(lines.length, 1);
  assert.match(lines[0], /^✍️ Writing b\.txt \(\d+ bytes so far\)…$/);
});

// ---- the sidebar page ----------------------------------------------------------------------------------------

/** Runs the sidebar's own script against a small stand-in page. Returns its message handler and what it made. */
function sidebarPage() {
  const { SidebarProvider } = require('../dist/views/sidebarProvider');
  const status = { state: 'idle', gpu: { name: 'GPU', api: 'vulkan', vramTotalGb: 8 }, vramContributedGb: 0, cpuPool: false };
  const provider = new SidebarProvider({ fsPath: path.join(__dirname, '..') }, {}, { onStatusChange() {}, getStatus: () => status });
  const html = provider._getHtmlForWebview();
  const scripts = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map((m) => m[1]);

  const made = [];
  const htmlWrites = [];
  const element = (tag = 'div') => {
    const el = {
      tag, children: [], style: {}, dataset: {}, className: '', textContent: '', removed: false, _html: '',
      classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
      appendChild(child) { this.children.push(child); return child; },
      append(...children) { this.children.push(...children); },
      prepend() {}, insertBefore(child) { this.children.push(child); return child; },
      addEventListener() {}, removeEventListener() {}, scrollIntoView() {}, focus() {}, click() {},
      setAttribute() {}, removeAttribute() {}, getAttribute: () => null,
      remove() { this.removed = true; },
      querySelector: () => null, closest: () => null, querySelectorAll: () => [], getElementsByClassName: () => [],
      scrollHeight: 0, scrollTop: 0, clientHeight: 0,
      get innerHTML() { return this._html; },
      set innerHTML(value) { this._html = String(value); htmlWrites.push(this._html); },
    };
    made.push(el);
    return el;
  };
  const byId = {};
  const listeners = {};
  const sandbox = {
    document: {
      getElementById: (id) => (byId[id] ??= element()),
      createElement: element, querySelector: () => element(), querySelectorAll: () => [], body: element('body'), addEventListener() {},
    },
    acquireVsCodeApi: () => ({ postMessage() {}, getState: () => undefined, setState() {} }),
    window: { addEventListener: (type, fn) => { listeners[type] = fn; } },
    addEventListener: (type, fn) => { listeners[type] = fn; },
    setTimeout, clearTimeout, setInterval: () => 0, clearInterval() {}, console, navigator: {},
  };
  vm.createContext(sandbox);
  for (const script of scripts) vm.runInContext(script, sandbox);
  return {
    html,
    send: (data) => listeners.message({ data }),
    pills: () => made.filter((el) => el.className === 'tool-pill'),
    answers: () => made.filter((el) => el.className === 'assistant-body'),
    htmlWrites,
  };
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 80));

test('one status line at a time: the "waiting" line goes when the tool starts, and nothing is left at the end', () => {
  stub.reset();
  const page = sidebarPage();
  page.send({ type: 'appendUserMessage', text: 'Build a snake game' });
  page.send({ type: 'loadingProgress', statusText: '⏳ Waiting for the swarm to start your answer (2 s).' });
  page.send({ type: 'loadingProgress', statusText: '✍️ Writing snake.html (9.2 KB so far)…' });
  assert.equal(page.pills().length, 1, 'the writing line replaces the waiting line');
  assert.equal(page.pills()[0].textContent, '✍️ Writing snake.html (9.2 KB so far)…');
  page.send({ type: 'toolStart', toolName: 'write_file', args: {} });
  const [first, second] = page.pills();
  assert.equal(first.removed, true, 'the line before the tool is taken down');
  assert.match(second.textContent, /Executing write_file/);
  page.send({ type: 'toolEnd', result: { name: 'write_file', output: 'ok' } });
  page.send({ type: 'contentDelta', delta: 'Done.' });
  page.send({ type: 'turnComplete' });
  // What stays is the record of what the tool did ("✅ write_file: ok"); no "waiting", "writing" or "executing" line.
  const left = page.pills().filter((pill) => !pill.removed).map((pill) => pill.textContent);
  assert.deepEqual(left, ['✅ write_file: ok']);
});

// ---- 3. the answer as Markdown, safely --------------------------------------------------------------------------

test('the answer is shown as Markdown while it streams, and the last words are shown when the turn ends', async () => {
  stub.reset();
  const page = sidebarPage();
  page.send({ type: 'appendUserMessage', text: 'hi' });
  page.send({ type: 'contentDelta', delta: 'Done! I created `snake.html`.\n\n**Game' });
  page.send({ type: 'contentDelta', delta: 'play**\n- Classic snake\n- High score saved' });
  await settle();
  const [answer] = page.answers();
  assert.equal(answer._html, '<p>Done! I created <code>snake.html</code>.</p><p><strong>Gameplay</strong></p><ul><li>Classic snake</li><li>High score saved</li></ul>');
  page.send({ type: 'contentDelta', delta: '\n\nEnjoy!' });
  page.send({ type: 'turnComplete' });                     // no wait: the end shows what is pending at once
  assert.match(answer._html, /<p>Enjoy!<\/p>$/);
});

test('an answer that holds HTML stays text, streamed or restored', async () => {
  stub.reset();
  const payload = '<img src=x onerror="vscode.postMessage({type:\'toggleWorker\'})"><script>alert(1)</script>';
  const page = sidebarPage();
  page.send({ type: 'appendUserMessage', text: 'hi' });
  page.send({ type: 'contentDelta', delta: `**bold ${payload}** \`${payload}\`\n\n${payload}` });
  await settle();
  page.send({ type: 'turnComplete' });
  page.send({ type: 'restoreHistory', history: [{ role: 'user', text: 'hi' }, { role: 'assistant', text: `# ${payload}\n- ${payload}` }] });
  const answerHtml = page.htmlWrites.filter((html) => html.includes('toggleWorker'));
  assert.ok(answerHtml.length >= 2, 'both answers were rendered');
  for (const html of answerHtml) {
    assert.doesNotMatch(html, /<img|<script|onerror="/i, html);
    assert.match(html, /&lt;img src=x onerror=&quot;/);
  }
});

test('the page runs the same renderMarkdown the tests check', () => {
  stub.reset();
  assert.ok(sidebarPage().html.includes(renderMarkdown.toString()));
});

test('Markdown: headings, lists, code, emphasis, quotes, rules; links shown as text with their address', () => {
  assert.equal(renderMarkdown('# Title\n## Part\n#### Small'), '<h3>Title</h3><h4>Part</h4><h6>Small</h6>');
  assert.equal(renderMarkdown('1. one\n2. two\n   more\n- a'), '<ol><li>one</li><li>two<br>more</li></ol><ul><li>a</li></ul>');
  assert.equal(renderMarkdown('```js\nif (a < b) { x = "y"; }\n```'), '<pre><code>if (a &lt; b) { x = &quot;y&quot;; }</code></pre>');
  assert.equal(renderMarkdown('```\nstill arriving'), '<pre><code>still arriving</code></pre>');
  assert.equal(renderMarkdown('**b** *i* _i_ ~~s~~ `**not bold**`'), '<p><strong>b</strong> <em>i</em> <em>i</em> <del>s</del> <code>**not bold**</code></p>');
  assert.equal(renderMarkdown('snake_case_name, 2*3*4 and __init__'), '<p>snake_case_name, 2*3*4 and <strong>init</strong></p>');
  assert.equal(renderMarkdown('> note\n\n---'), '<blockquote>note</blockquote><hr>');
  assert.equal(renderMarkdown('see [docs](https://x.dev/a) now'), '<p>see docs (https://x.dev/a) now</p>');
  assert.equal(renderMarkdown('line one\r\nline two'), '<p>line one<br>line two</p>');
  assert.equal(renderMarkdown(''), '');
});

test('Markdown: whatever the text, the only markup is the renderer\'s own tags, with no attributes', () => {
  const allowed = /^<\/?(p|br|h[3-6]|ul|ol|li|pre|code|strong|em|del|blockquote|hr)>$/;
  const alphabet = ['<', '>', '"', "'", '&', '`', '*', '_', '~', '#', '-', '>', '[', ']', '(', ')', '\n', ' ', 'a', 'on', '=', '/', 'img', 'script', '\u0000', '1.'];
  let seed = 42;
  const random = () => ((seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648);
  for (let n = 0; n < 2000; n++) {
    let text = '';
    const length = Math.floor(random() * 60);
    for (let i = 0; i < length; i++) text += alphabet[Math.floor(random() * alphabet.length)];
    const html = renderMarkdown(text);
    for (const tag of html.match(/<[^>]*>/g) || []) {
      assert.match(tag, allowed, `from ${JSON.stringify(text)}`);
    }
    assert.doesNotMatch(html.replace(/<[^>]*>/g, ''), /[<>"']/, `an unescaped character from ${JSON.stringify(text)}`);
  }
});

// ---- 4. autocomplete -----------------------------------------------------------------------------------------

function doc(text) {
  const lines = text.split('\n');
  const offsetOf = (p) => lines.slice(0, p.line).reduce((n, l) => n + l.length + 1, 0) + p.character;
  return {
    uri: { scheme: 'file' },
    lineCount: lines.length,
    lineAt: (i) => ({ text: lines[i] }),
    getText: (range) => text.slice(offsetOf(range.start), offsetOf(range.end)),
  };
}
const pos = (line, character) => new stub.vscode.Position(line, character);
const token = () => ({ isCancellationRequested: false, onCancellationRequested() { return { dispose() {} }; } });

test('just inside {} (the editor typed the closing brace) is the start of a body, not the middle of a line', () => {
  assert.equal(buildFimContext(doc('function isPalindrome(text) {}'), pos(0, 29)).midLine, false);
  assert.equal(buildFimContext(doc('items.forEach((x) => {});'), pos(0, 22)).midLine, false);
  assert.equal(buildFimContext(doc('foo(bar)'), pos(0, 4)).midLine, true, 'inside a call is still mid-line');
  assert.equal(closesBlock('}'), true);
  assert.equal(closesBlock('  },'), true);
  assert.equal(closesBlock(')'), false);
  assert.equal(closesBlock('} else {'), false);
});

test('a block completion that ran into its cap keeps only its complete lines', () => {
  const cut = "\n  text = text.toLowerCase();\n  return text.split('').";
  assert.equal(trimCutOff(cut, 'length', false), '\n  text = text.toLowerCase();');
  assert.equal(trimCutOff(cut, 'stop', false), cut, 'a completion that ended by itself is kept whole');
  assert.equal(trimCutOff('let', 'length', false), '', 'no complete line: nothing to offer');
  assert.equal(trimCutOff('a, b', 'length', true), 'a, b', 'inside a line the rest of the line is the point');
});

async function completions(answer) {
  stub.reset();
  forgetPrivacyModes();
  stub.state.settings['dlne.inlineCompletionDelay'] = 75;
  const c = await swarm.startCoordinator({ status: swarm.statusBody(), completions: (ctx) => answer(ctx) });
  stub.state.settings['dlne.serverUrl'] = c.url;
  return c;
}

test('inside {} the provider asks for a block and offers its complete lines', async () => {
  const c = await completions((ctx) => swarm.json(ctx.res, 200, {
    choices: [{ text: '\n  const clean = text.toLowerCase();\n  return clean.split(\'\').', finish_reason: 'length' }],
  }));
  try {
    const items = await new DlneFimProvider(new DlneClient()).provideInlineCompletionItems(
      doc('function isPalindrome(text) {}'), pos(0, 29), {}, token());
    assert.equal(items[0].insertText, '\n  const clean = text.toLowerCase();');
    const sent = c.of('/v1/completions')[0].body;
    assert.equal(sent.max_tokens, 64);
    assert.ok(!sent.stop.includes('\n'), 'a block may run over several lines');
  } finally {
    c.close();
  }
});

test('a failed completion is written to the DLNE log, once a minute for the same reason', async () => {
  const c = await completions((ctx) => swarm.json(ctx.res, 500, { detail: 'boom' }));
  try {
    const provider = new DlneFimProvider(new DlneClient());
    assert.equal(await provider.provideInlineCompletionItems(doc('x = 1\n'), pos(1, 0), {}, token()), null);
    assert.equal(await provider.provideInlineCompletionItems(doc('x = 2\n'), pos(1, 0), {}, token()), null);
    const log = stub.state.channels.find((channel) => channel.name === 'DLNE');
    assert.ok(log, 'the DLNE log was opened');
    assert.equal(log.lines.length, 1);
    assert.match(log.lines[0], /Autocomplete: no suggestion, HTTP 500\./);
  } finally {
    c.close();
  }
});

// ---- 5. no user systemd ------------------------------------------------------------------------------------

test('without a user systemd the 5-second check asks once, quietly, and then stops asking', () => {
  stub.reset();
  const proto = WorkerManager.prototype;
  const real = { sysd: proto.isSystemdActive, proc: proto.isProcessActive };
  proto.isSystemdActive = () => false;
  proto.isProcessActive = () => false;
  let wm;
  try {
    wm = new WorkerManager({ subscriptions: [], globalState: undefined });
  } finally {
    proto.isSystemdActive = real.sysd;
    proto.isProcessActive = real.proc;
  }
  const platform = Object.getOwnPropertyDescriptor(process, 'platform');
  Object.defineProperty(process, 'platform', { value: 'linux' });
  const realExec = childProcess.execSync;
  const calls = [];
  const fail = (stdout, signal) => (cmd, options) => {
    calls.push(options);
    const err = new Error('Command failed');
    err.stdout = stdout;
    err.signal = signal;
    throw err;
  };
  try {
    childProcess.execSync = fail('inactive\n', null);              // systemd answers: the service is not running
    assert.equal(wm.isSystemdActive(), false);
    assert.equal(wm.isSystemdActive(), false);
    assert.equal(calls.length, 2, 'a working systemd keeps being asked');
    childProcess.execSync = fail('', 'SIGTERM');                    // a timeout on a busy machine
    wm.isSystemdActive();
    assert.equal(calls.length, 3, 'a timeout is not "no systemd"');
    childProcess.execSync = fail('', null);                         // Failed to connect to user scope bus
    wm.isSystemdActive();
    wm.isSystemdActive();
    wm.isSystemdActive();
    assert.equal(calls.length, 4, 'asked once, then not again');
    assert.deepEqual(calls[0].stdio, ['ignore', 'pipe', 'ignore'], 'its complaints never reach the log');
  } finally {
    childProcess.execSync = realExec;
    Object.defineProperty(process, 'platform', platform);
    wm.dispose();
  }
});
