'use strict';
/**
 * The swarm serves one coding model, the one its coordinator has active, and the coordinator reads a
 * request's `model` as `body.model or <active model>`. So the extension names no model (naming one
 * can make the coordinator swap), offers a single "DLNE Swarm" entry in VS Code's model picker
 * (model-provider.test.js), and shows the live model as a read-only line in the sidebar.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const stub = require('./helpers/vscode-stub');

const { DlneClient } = require('../dist/client/dlneClient');
const { SidebarProvider } = require('../dist/views/sidebarProvider');
const choice = require('../dist/client/modelChoice');

process.env.HOME = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-model-home-'));

const ROOT = path.join(__dirname, '..');
const pkg = JSON.parse(fs.readFileSync(path.join(ROOT, 'package.json'), 'utf8'));

const OLD_PICKER = [
  'qwen3-coder-30b-instruct-q4_k_m.gguf',
  'qwen2.5-14b-1m.gguf',
  'Qwen2.5-Coder-32B-Instruct-Q4_K_M.gguf',
  'qwen2.5-coder-14b-instruct-q4_k_m.gguf',
  'DeepSeek-R1-Distill-Qwen-32B-Q4_K_M.gguf',
  'Llama-3.3-70B-Instruct-IQ3_M.gguf',
  'DeepSeek-R1-Distill-Llama-70B-Q4_K_M.gguf',
  'Qwen2.5-72B-Instruct-Q4_K_M.gguf',
];

// ---- what a request names ----------------------------------------------------------------

test('a request names no model unless one was set', () => {
  for (const blank of [undefined, null, '', '   ', 'undefined', 42, {}]) {
    assert.equal(choice.requestedModel(blank), undefined, JSON.stringify(blank));
  }
});

test('a model set for a private coordinator is passed through as written', () => {
  assert.equal(choice.requestedModel('my-model.gguf'), 'my-model.gguf');
  assert.equal(choice.requestedModel('  Qwen3.6-35B-A3B-UD-IQ3_S.gguf '), 'Qwen3.6-35B-A3B-UD-IQ3_S.gguf');
});

test('the eight names the old picker saved are read as empty, however they are written', () => {
  for (const file of OLD_PICKER) {
    assert.equal(choice.requestedModel(file), undefined, file);
    assert.equal(choice.requestedModel(file.toLowerCase()), undefined);
    assert.equal(choice.requestedModel(`/models/${file}`), undefined);
    assert.equal(choice.requestedModel(file.replace(/\.gguf$/, '')), undefined);
  }
  assert.equal(choice.RETIRED_MODEL_FILES.length, 8);
});

async function coordinator(statusAnswer) {
  const hits = { chat: [], completions: [] };
  const server = http.createServer((req, res) => {
    if (req.url === '/v1/cluster/status') {
      if (!statusAnswer) {
        res.writeHead(503);
        res.end('down');
        return;
      }
      res.setHeader('Content-Type', 'application/json');
      res.end(JSON.stringify(statusAnswer));
      return;
    }
    let body = '';
    req.on('data', (c) => (body += c));
    req.on('end', () => {
      const parsed = JSON.parse(body);
      if (req.url === '/v1/completions') {
        hits.completions.push(parsed);
        res.setHeader('Content-Type', 'application/json');
        res.end(JSON.stringify({ choices: [{ text: 'x' }] }));
        return;
      }
      hits.chat.push(parsed);
      res.writeHead(200, { 'Content-Type': 'text/event-stream' });
      res.write('data: ' + JSON.stringify({ choices: [{ delta: { content: 'ok' } }] }) + '\n\n');
      res.end('data: [DONE]\n\n');
    });
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  return { hits, url: `http://127.0.0.1:${server.address().port}`, close: () => { server.closeAllConnections(); server.close(); } };
}

const chat = (client) => client.streamChat([{ role: 'user', content: 'hi' }], [], () => {});

test('chat and autocomplete leave the model out, so the coordinator serves its active one', async () => {
  stub.reset();
  const c = await coordinator({ status: 'Active', global_active_model: 'Qwen3.6-35B-A3B-UD-IQ3_S.gguf', model_loading: { is_loading: false } });
  stub.state.settings['dlne.serverUrl'] = c.url;
  try {
    const client = new DlneClient();
    await chat(client);
    await client.getCompletion('def f(');
    assert.equal(c.hits.chat.length, 1);
    assert.ok(!('model' in c.hits.chat[0]), 'chat names no model');
    assert.ok(!('model' in c.hits.completions[0]), 'autocomplete names no model');
  } finally {
    c.close();
  }
});

test('when the status cannot be read, the request still names no model (it used to name the 14B)', async () => {
  stub.reset();
  const c = await coordinator(null);
  stub.state.settings['dlne.serverUrl'] = c.url;
  try {
    await chat(new DlneClient());
    assert.ok(!('model' in c.hits.chat[0]), JSON.stringify(c.hits.chat[0]));
    assert.doesNotMatch(JSON.stringify(c.hits.chat[0]), /qwen2\.5|14b/i);
  } finally {
    c.close();
  }
});

test('a model the user set is asked for; a leftover from the old picker is not', async () => {
  stub.reset();
  const c = await coordinator({ status: 'Active', global_active_model: 'served.gguf', model_loading: { is_loading: false } });
  stub.state.settings['dlne.serverUrl'] = c.url;
  try {
    const client = new DlneClient();
    stub.state.settings['dlne.chatModel'] = 'my-model.gguf';
    stub.state.settings['dlne.fimModel'] = 'my-fim.gguf';
    await chat(client);
    await client.getCompletion('x');
    assert.equal(c.hits.chat[0].model, 'my-model.gguf');
    assert.equal(c.hits.completions[0].model, 'my-fim.gguf');

    stub.state.settings['dlne.chatModel'] = 'Llama-3.3-70B-Instruct-IQ3_M.gguf';
    stub.state.settings['dlne.fimModel'] = 'qwen2.5-coder-14b-instruct-q4_k_m.gguf';
    await chat(client);
    await client.getCompletion('x');
    assert.ok(!('model' in c.hits.chat[1]));
    assert.ok(!('model' in c.hits.completions[1]));
  } finally {
    c.close();
  }
});

test('the client no longer asks the coordinator to swap its model', () => {
  assert.equal(typeof DlneClient.prototype.swapModel, 'undefined');
  const source = fs.readFileSync(path.join(ROOT, 'src', 'client', 'dlneClient.ts'), 'utf8');
  assert.doesNotMatch(source, /cluster\/swap/);
});

// ---- what the sidebar shows --------------------------------------------------------------

test('the live model is named by coding_model.name when the server sends it (2.3.5), else by its file', () => {
  assert.equal(choice.activeModelName({ global_active_model: 'q.gguf', coding_model: { name: ' Qwen3.6-35B-A3B ' } }), 'Qwen3.6-35B-A3B');
  assert.equal(choice.activeModelName({ global_active_model: 'Qwen3.6-35B-A3B-UD-IQ3_S.gguf' }), 'Qwen3.6-35B-A3B-UD-IQ3_S');
  assert.equal(choice.activeModelName({ global_active_model: 'x.gguf', coding_model: null }), 'x');
  assert.equal(choice.activeModelName({ global_active_model: 'x.gguf', coding_model: { name: 7 } }), 'x');
  assert.equal(choice.activeModelName({ global_active_model: 'x.gguf', coding_model: { name: '  ' } }), 'x');
  for (const none of [null, undefined, {}, { global_active_model: null }, { global_active_model: ' ' }]) {
    assert.equal(choice.activeModelName(none), null);
  }
});

test('the sidebar line says "DLNE Swarm (auto)", with the model once it is known', () => {
  assert.equal(choice.swarmModelLine(null), 'DLNE Swarm (auto)');
  assert.equal(choice.swarmModelLine({ global_active_model: 'a.gguf' }), 'DLNE Swarm (auto): a');
  assert.equal(choice.swarmModelLine({ coding_model: { name: 'Qwen3.6-35B-A3B' } }), 'DLNE Swarm (auto): Qwen3.6-35B-A3B');
});

const workerStatus = { state: 'idle', gpu: { hasGpu: true, name: 'RTX 4060', vramTotalGb: 8, api: 'vulkan' }, cpuPool: false, vramContributedGb: 0 };
const workerManager = { onStatusChange() {}, getStatus: () => workerStatus };

function sidebarWith(client) {
  stub.reset();
  return new SidebarProvider({ fsPath: ROOT }, client, workerManager);
}

test('the sidebar has no model dropdown, and shows a read-only line instead', () => {
  const html = sidebarWith({})._getHtmlForWebview();
  assert.doesNotMatch(html, /<select|<option|model-select|selectModel/);
  assert.match(html, /<span class="model-line" id="model-line"[^>]*>DLNE Swarm \(auto\)<\/span>/);
  for (const old of OLD_PICKER) assert.ok(!html.includes(old), `the sidebar still offers ${old}`);
  assert.doesNotMatch(html, /Llama|DeepSeek|Qwen 2\.5|Frontier/);
});

test('the line starts from the last status answer, and the name is escaped', () => {
  const html = sidebarWith({ lastKnownClusterStatus: () => ({ coding_model: { name: 'Qwen3.6-35B-A3B' } }) })._getHtmlForWebview();
  assert.match(html, />DLNE Swarm \(auto\): Qwen3\.6-35B-A3B<\/span>/);
  const hostile = sidebarWith({ lastKnownClusterStatus: () => ({ global_active_model: '"><script>x</script>.gguf' }) })._getHtmlForWebview();
  assert.ok(!hostile.includes('"><script>x'));
  assert.ok(hostile.includes('&quot;>&lt;script>x'));
});

test('the sidebar follows the status the status bar polls, and stops when it is closed', () => {
  let listener;
  let unsubscribed = 0;
  let onDispose;
  const client = {
    lastKnownClusterStatus: () => null,
    onClusterStatus: (fn) => { listener = fn; return () => { unsubscribed++; }; },
  };
  const provider = sidebarWith(client);
  const posted = [];
  provider.resolveWebviewView(
    {
      webview: { options: {}, html: '', asWebviewUri: (uri) => uri, postMessage: (m) => posted.push(m), onDidReceiveMessage() {} },
      onDidDispose: (fn) => { onDispose = fn; },
    },
    {},
    {}
  );
  assert.equal(posted[0].type, 'restoreHistory');
  assert.equal(posted[0].modelLine, 'DLNE Swarm (auto)');
  listener({ global_active_model: 'Qwen3.6-35B-A3B-UD-IQ3_S.gguf', coding_model: { name: 'Qwen3.6-35B-A3B' } });
  assert.deepEqual(posted[1], { type: 'modelLine', text: 'DLNE Swarm (auto): Qwen3.6-35B-A3B' });
  onDispose();
  assert.equal(unsubscribed, 1);
});

test('a selectModel message from an old webview changes nothing', async () => {
  const provider = sidebarWith({});
  let onMessage;
  provider.resolveWebviewView(
    { webview: { options: {}, html: '', asWebviewUri: (uri) => uri, postMessage() {}, onDidReceiveMessage: (fn) => { onMessage = fn; } } },
    {},
    {}
  );
  await onMessage({ type: 'selectModel', model: 'Llama-3.3-70B-Instruct-IQ3_M.gguf' });
  assert.deepEqual(stub.state.updates, []);
  assert.deepEqual(stub.state.infoMessages, []);
});

test('the status listener hears every good answer, and can be removed', async () => {
  stub.reset();
  const c = await coordinator({ status: 'Active', global_active_model: 'm.gguf', model_loading: { is_loading: false } });
  stub.state.settings['dlne.serverUrl'] = c.url;
  try {
    const client = new DlneClient();
    assert.equal(client.lastKnownClusterStatus(), null);
    const heard = [];
    const off = client.onClusterStatus((s) => heard.push(s.global_active_model));
    client.onClusterStatus(() => { throw new Error('a broken listener must not break the others'); });
    await client.getClusterStatusDetailed();
    assert.deepEqual(heard, ['m.gguf']);
    assert.equal(client.lastKnownClusterStatus().global_active_model, 'm.gguf');
    off();
    await client.getClusterStatusDetailed();
    assert.deepEqual(heard, ['m.gguf']);
  } finally {
    c.close();
  }
});

// (VS Code's Chat model picker, and that choosing its entry writes no setting: model-provider.test.js)

// ---- the words ---------------------------------------------------------------------------

function walk(dir) {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
    const full = path.join(dir, e.name);
    return e.isDirectory() ? walk(full) : [full];
  });
}

test('no old model, and no "Frontier" wording, is left in what the extension shows', () => {
  const retired = /qwen2\.5-coder-14b|qwen2\.5-coder-32b|qwen2\.5-14b-1m|qwen2\.5-72b|llama-3\.3-70b|deepseek-r1|qwen3-coder-30b/i;
  for (const file of walk(path.join(ROOT, 'src'))) {
    const text = fs.readFileSync(file, 'utf8');
    assert.doesNotMatch(text, /Frontier/, `${path.relative(ROOT, file)} says Frontier`);
    if (path.basename(file) !== 'modelChoice.ts') {
      assert.doesNotMatch(text, retired, `${path.relative(ROOT, file)} names a model the swarm does not run`);
    }
  }
  const manifest = JSON.stringify(pkg.contributes);
  assert.doesNotMatch(manifest, /Frontier/);
  assert.doesNotMatch(manifest, retired);
  assert.equal(pkg.contributes.views['dlne-sidebar'][0].name, 'DLNE Code Assistant');
  assert.equal(pkg.contributes.chatParticipants[0].fullName, 'DLNE Code Assistant');
});
