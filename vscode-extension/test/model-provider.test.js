'use strict';
/**
 * DLNE Swarm in VS Code's Chat model picker (extension 1.5.0): src/client/modelProvider.ts, with the request side in
 * lmMessages.ts and the reply side in lmReply.ts.
 *
 * What these tests keep true (the design is in the chat-migration notes, model-provider.md):
 *   - it is registered only where the editor has the stable API, under a vendor that package.json declares, and an
 *     editor without the API sees nothing (no error, no log line);
 *   - VS Code's messages reach the swarm as strings with a real system role, and secrets never leave;
 *   - nothing but the model's answer is ever reply text: not thinking, not tool calls written as tags, not the
 *     coordinator's notices, not a "waiting" line;
 *   - tool calls are passed on only for tools VS Code offered, and never half-written;
 *   - a dropped engine is retried until something has been shown, never after.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const stub = require('./helpers/vscode-stub');
const lm = require('./helpers/lm-stub');
const { generator } = require('./helpers/fake-secrets');

// The compiled code copies the `vscode` module's members when it is first loaded, so what the editor has (the chat
// and language-model namespaces, here recording everything the old and the new registration would do) is in place
// before anything from dist/ is required.
const apiCalls = [];
stub.vscode.chat = { createChatParticipant: () => ({ dispose() {} }) };
stub.vscode.lm = {
  registerChatModelProvider: (...args) => { apiCalls.push(['registerChatModelProvider', ...args]); return { dispose() {} }; },
  registerLanguageModelChatProvider: (...args) => { apiCalls.push(['registerLanguageModelChatProvider', ...args]); return { dispose() {} }; },
};

// The DLNE log: the module keeps the first channel it makes, so this one lives for the whole file.
const logLines = [];
stub.vscode.window.createOutputChannel = (name) => ({ name, appendLine: (line) => logLines.push(line), show() {}, dispose() {} });

const { DlneClient, EngineDropped, ServerRefusal, AGENT_MAX_TOKENS } = require('../dist/client/dlneClient');
const prov = require('../dist/client/modelProvider');
const msg = require('../dist/client/lmMessages');
const rep = require('../dist/client/lmReply');
const { AgentLoop, ENGINE_DROP_RETRIES, contextTokensFrom, engineDropWaitMs, promptBudgetFor } = require('../dist/agent/agentLoop');
const { registerNativeChatParticipant } = require('../dist/views/nativeChatParticipant');

process.env.HOME = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-lm-home-'));

const { Role, message, makeToken, makeLm } = lm;
const { LanguageModelTextPart, LanguageModelToolCallPart, LanguageModelToolResultPart, LanguageModelDataPart } = lm;

const ROOT = path.join(__dirname, '..');
const pkg = JSON.parse(fs.readFileSync(path.join(ROOT, 'package.json'), 'utf8'));
const declared = (pkg.contributes.languageModelChatProviders || []).map((p) => p.vendor);

const gen = generator(1504);
const ghp = 'gh' + 'p_' + gen('alnum', 36);
const dbPassword = 'Tr0ub4dor' + gen('alnum', 6);

// ---- helpers ------------------------------------------------------------------------------------------------------

/** A client whose answers a test writes: `script(n, io)` is called for the n-th request and returns or throws. */
function fakeClient(script = () => ({ content: '', toolCalls: [] })) {
  const calls = [];
  return {
    calls,
    async streamChat(messages, tools, onDelta, onToolCallStart, signal, onLoadingProgress, options) {
      calls.push({ messages, tools, signal, onLoadingProgress, options });
      return script(calls.length, { onDelta, signal, options });
    },
  };
}

/** An answer that streams `pieces` and is complete. */
const answering = (...pieces) => (n, { onDelta }) => {
  pieces.forEach((piece) => onDelta(piece));
  return { content: pieces.join(''), toolCalls: [], finishReason: 'stop' };
};

const MODEL = { id: 'swarm-auto', name: 'DLNE Swarm' };
const tool = (name, props = {}) => ({ name, description: `the ${name} tool`, inputSchema: { type: 'object', properties: props } });
const READ_TOOLS = [tool('read_file', { filePath: { type: 'string' } }), tool('list_dir', { path: { type: 'string' } }), tool('run_in_terminal', { command: { type: 'string' } })];

/** One request through the provider; collects what VS Code would have been given, and the error if it would have been one. */
async function run(client, messages, options = {}, token = makeToken()) {
  const provider = options.provider || new prov.DlneChatModelProvider(client);
  const parts = [];
  let error;
  try {
    await provider.provideLanguageModelChatResponse(
      MODEL, messages, { tools: [], toolMode: 1, ...options.request }, { report: (part) => parts.push(part) }, token
    );
  } catch (err) {
    error = err;
  }
  return {
    provider,
    parts,
    error,
    text: parts.filter((p) => p instanceof LanguageModelTextPart).map((p) => p.value).join(''),
    calls: parts.filter((p) => p instanceof LanguageModelToolCallPart).map((p) => ({ id: p.callId, name: p.name, input: p.input })),
  };
}

const ask = (text = 'hello') => [message(Role.User, text)];

/** Timers that fire at once, and the waits they were asked for. */
function fastTimers(waits = []) {
  const real = global.setTimeout;
  global.setTimeout = (fn, ms, ...args) => { waits.push(ms); return real(fn, 0, ...args); };
  return () => { global.setTimeout = real; };
}

async function coordinator(answer) {
  const hits = [];
  const server = http.createServer((req, res) => {
    if (req.url === '/v1/cluster/status') {
      res.setHeader('Content-Type', 'application/json');
      res.end(JSON.stringify({ status: 'Active', global_active_model: 'served.gguf', model_loading: { is_loading: false }, policy: { coding_context_tokens: 54613 } }));
      return;
    }
    let data = '';
    req.on('data', (chunk) => (data += chunk));
    req.on('end', () => {
      const hit = { headers: req.headers, body: JSON.parse(data), res };
      hits.push(hit);
      answer(hit, res, hits.length);
    });
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const url = `http://127.0.0.1:${server.address().port}`;
  return { hits, url, close: () => { server.closeAllConnections(); server.close(); } };
}
const sseFrame = (obj) => `data: ${JSON.stringify(obj)}\n\n`;
const sseText = (content) => sseFrame({ choices: [{ delta: { content } }] });
function sseAnswer(res, ...frames) {
  res.writeHead(200, { 'Content-Type': 'text/event-stream' });
  frames.forEach((f) => res.write(f));
  res.end('data: [DONE]\n\n');
}

/** Runs `body` against a mock coordinator with the client pointed at it. */
async function withCoordinator(answer, body) {
  stub.reset();
  const c = await coordinator(answer);
  stub.state.settings['dlne.serverUrl'] = c.url;
  try {
    await body(c, new DlneClient());
  } finally {
    c.close();
  }
}

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// ---- the manifest and the registration ----------------------------------------------------------------------------

test('package.json declares the vendor the code registers; engines still reach back to 1.85', () => {
  assert.equal(prov.MODEL_VENDOR, 'dlne');
  assert.deepEqual(declared, [prov.MODEL_VENDOR]);
  assert.ok(pkg.contributes.languageModelChatProviders[0].displayName, 'VS Code lists the vendor by its display name');
  assert.equal(pkg.engines.vscode, '^1.85.0', 'older editors must still install it');
});

test('on an editor with the 1.143 API: registered once under the declared vendor, and VS Code\'s log has no error', () => {
  const api = makeLm(declared);
  const context = { subscriptions: [] };
  const registration = prov.registerDlneChatModel(context, fakeClient(), api);
  assert.ok(registration && typeof registration.dispose === 'function');
  assert.equal(api.registrations.length, 1);
  assert.equal(api.registrations[0].vendor, 'dlne');
  assert.deepEqual(api.errors, []);
  assert.equal(context.subscriptions.length, 2, 'the registration and the provider are both released with the extension');
});

test('the stub knows the failure the old entry had: a vendor package.json does not declare is "UNKNOWN" in VS Code\'s log', () => {
  const api = makeLm([]);
  prov.registerDlneChatModel({ subscriptions: [] }, fakeClient(), api);
  assert.deepEqual(api.errors, ['Chat model provider uses UNKNOWN vendor dlne.']);
});

test('an editor without the API sees nothing: no registration, no log line, no error', () => {
  logLines.length = 0;
  const legacy = { selectChatModels() {}, registerChatModelProvider() { throw new Error('the retired API must never be called'); } };
  for (const missing of [null, {}, { selectChatModels() {} }, legacy]) {
    const context = { subscriptions: [] };
    assert.equal(prov.registerDlneChatModel(context, fakeClient(), missing), undefined);
    assert.equal(context.subscriptions.length, 0);
  }
  assert.deepEqual(logLines, []);
});

test('a registration that throws is swallowed and written to the DLNE log once; activation goes on', () => {
  logLines.length = 0;
  const api = { registerLanguageModelChatProvider() { throw new Error('boom'); } };
  const context = { subscriptions: [] };
  assert.equal(prov.registerDlneChatModel(context, fakeClient(), api), undefined);
  assert.equal(context.subscriptions.length, 0);
  assert.equal(logLines.length, 1);
  assert.match(logLines[0], /could not be added to the model picker: boom/);
});

test('by default it registers through the editor\'s own vscode.lm', () => {
  apiCalls.length = 0;
  prov.registerDlneChatModel({ subscriptions: [] }, fakeClient());
  assert.equal(apiCalls.length, 1);
  assert.equal(apiCalls[0][0], 'registerLanguageModelChatProvider');
  assert.equal(apiCalls[0][1], 'dlne');
  assert.equal(apiCalls[0].length, 3, 'the stable call takes a vendor and a provider and nothing else');
});

test('the participant no longer registers a model: the old "dlne-auto" entry and its retired-API call are gone', () => {
  apiCalls.length = 0;
  registerNativeChatParticipant({ extensionUri: { fsPath: ROOT }, subscriptions: [] }, fakeClient(), { onStatusChange() {}, getStatus: () => ({}) });
  assert.deepEqual(apiCalls, []);
  const source = fs.readFileSync(path.join(ROOT, 'src', 'views', 'nativeChatParticipant.ts'), 'utf8');
  assert.doesNotMatch(source, /registerChatModelProvider|registerLanguageModelChatProvider|dlne-auto|SWARM_MODEL/);
});

test('the model is registered by activate(), on its own and not behind the participant', () => {
  const extension = fs.readFileSync(path.join(ROOT, 'src', 'extension.ts'), 'utf8');
  assert.match(extension, /registerDlneChatModel\(context, client\);/);
  const participant = fs.readFileSync(path.join(ROOT, 'src', 'views', 'nativeChatParticipant.ts'), 'utf8');
  assert.doesNotMatch(participant, /registerDlneChatModel/);
});

test('VS Code 1.143 calls the provider the way its probe saw: silent information requests and core\'s request options', async () => {
  const client = fakeClient(answering('ok'));
  const provider = new prov.DlneChatModelProvider(client);
  const [info] = await provider.provideLanguageModelChatInformation({ silent: true, configuration: undefined }, makeToken());
  assert.equal(info.id, 'swarm-auto');
  const options = { tools: READ_TOOLS, modelOptions: { _capturingTokenCorrelationId: 'x', _conversationId: 'chat-1' }, modelConfiguration: undefined, requestInitiator: 'x', toolMode: 1, includeEncryptedThinking: false };
  const { text, error } = await run(client, ask(), { provider, request: options });
  assert.equal(error, undefined);
  assert.equal(text, 'ok');
});

// ---- the entry ------------------------------------------------------------------------------------------------------

test('the entry is DLNE Swarm: tool calling on, images off, a context sized from the live status', () => {
  const live = (tokens) => ({ policy: { coding_context_tokens: tokens }, coding_model: { name: 'Qwen3.6-35B-A3B' } });
  for (const [status, expected] of [[live(54613), 39321], [live(40960), 29491], [null, 21504], [{}, 21504], [live(1000), 21504]]) {
    const provider = new prov.DlneChatModelProvider({ ...fakeClient(), lastKnownClusterStatus: () => status });
    const entries = provider.provideLanguageModelChatInformation();
    assert.equal(entries.length, 1);
    const [info] = entries;
    assert.equal(info.id, 'swarm-auto');
    assert.equal(info.name, 'DLNE Swarm');
    assert.equal(info.family, 'dlne-swarm');
    assert.deepEqual(info.capabilities, { toolCalling: true, imageInput: false });
    assert.equal(info.maxInputTokens, expected, JSON.stringify(status));
    assert.equal(info.maxOutputTokens, AGENT_MAX_TOKENS);
    assert.equal(info.detail, status && status.coding_model ? 'Qwen3.6-35B-A3B' : undefined);
  }
});

test('the prompt budget is the agent loop\'s, at any context (one formula, shared)', () => {
  for (const context of [4096, 8192, 16384, 32768, 40960, 54613, 109226]) {
    const status = { policy: { coding_context_tokens: context } };
    const loop = new AgentLoop({ lastKnownClusterStatus: () => status });
    assert.equal(promptBudgetFor(contextTokensFrom(status)), loop.promptBudget(), `at ${context}`);
  }
  assert.equal(contextTokensFrom(undefined), 32768);
  assert.equal(contextTokensFrom({ policy: { coding_context_tokens: 'many' } }), 32768);
  // From 2.6.x the Chat view's entry offers the largest machine's context too, as the agent does.
  assert.equal(contextTokensFrom({ policy: { coding_context_tokens: 40960, coding_context_tokens_max: 131072 } }), 131072);
  assert.equal(contextTokensFrom({ policy: { coding_context_tokens: 40960, coding_context_tokens_max: 'big' } }), 40960);
});

test('VS Code is told to ask again when the context or the model changes, not on every status poll', () => {
  let listener;
  let status = null;
  const client = {
    ...fakeClient(),
    lastKnownClusterStatus: () => status,
    onClusterStatus: (fn) => { listener = fn; return () => { listener = undefined; }; },
  };
  const provider = new prov.DlneChatModelProvider(client);
  let fired = 0;
  provider.onDidChangeLanguageModelChatInformation(() => fired++);

  const live = { policy: { coding_context_tokens: 54613 }, coding_model: { name: 'Qwen3.6-35B-A3B' } };
  listener(live);
  assert.equal(fired, 0, 'VS Code has not looked yet, so there is nothing to correct');

  provider.provideLanguageModelChatInformation(); // its first look: nothing known yet
  status = live;
  listener(live);
  assert.equal(fired, 1);
  provider.provideLanguageModelChatInformation();
  listener(live);
  listener({ ...live });
  assert.equal(fired, 1, 'a poll that says the same thing changes nothing');
  listener({ ...live, policy: { coding_context_tokens: 40960 } });
  assert.equal(fired, 2, 'a volunteer paired: the context is smaller');
  provider.provideLanguageModelChatInformation();
  listener({ ...live, policy: { coding_context_tokens: 40960 }, coding_model: { name: 'Another-Model' } });
  assert.equal(fired, 3, 'the swarm switched models');

  provider.dispose();
  assert.equal(listener, undefined, 'the status listener is released');
});

test('the entry and the token count never touch the network', async () => {
  const client = fakeClient(() => { throw new Error('no request may be made'); });
  const provider = new prov.DlneChatModelProvider(client);
  provider.provideLanguageModelChatInformation();
  await provider.provideTokenCount(MODEL, 'some text', makeToken());
  assert.equal(client.calls.length, 0);
});

// ---- VS Code's messages, as the swarm gets them ---------------------------------------------------------------------

test('the system prompt is a real system message, first; the old entry sent it as the assistant\'s', () => {
  const out = msg.toOpenAiMessages([
    message(Role.System, 'You are DLNE.'),
    message(Role.User, 'hi'),
    message(Role.Assistant, 'hello'),
    message(Role.System, 'Another rule.'),
    message(Role.User, 'again'),
  ]);
  assert.deepEqual(out, [
    { role: 'system', content: 'You are DLNE.\n\nAnother rule.' },
    { role: 'user', content: 'hi' },
    { role: 'assistant', content: 'hello' },
    { role: 'user', content: 'again' },
  ]);
});

test('a tool step: calls keep their ids and arguments; each result is a tool message, in order, before the user\'s own words', () => {
  const out = msg.toOpenAiMessages([
    message(Role.User, 'fix the bug'),
    message(
      Role.Assistant,
      'Let me look.',
      new LanguageModelToolCallPart('call_1', 'read_file', { filePath: 'a.ts' }),
      new LanguageModelToolCallPart('call_2', 'list_dir', { path: '.' })
    ),
    message(
      Role.User,
      new LanguageModelToolResultPart('call_1', [new LanguageModelTextPart('line 1'), new LanguageModelTextPart('\nline 2')]),
      new LanguageModelToolResultPart('call_2', [new LanguageModelTextPart('a.ts')]),
      'And keep it small.'
    ),
    message(Role.Assistant, new LanguageModelToolCallPart('call_3', 'run_in_terminal', {})),
  ]);
  assert.deepEqual(out, [
    { role: 'user', content: 'fix the bug' },
    {
      role: 'assistant',
      content: 'Let me look.',
      tool_calls: [
        { id: 'call_1', type: 'function', function: { name: 'read_file', arguments: '{"filePath":"a.ts"}' } },
        { id: 'call_2', type: 'function', function: { name: 'list_dir', arguments: '{"path":"."}' } },
      ],
    },
    { role: 'tool', tool_call_id: 'call_1', name: 'read_file', content: 'line 1\nline 2' },
    { role: 'tool', tool_call_id: 'call_2', name: 'list_dir', content: 'a.ts' },
    { role: 'user', content: 'And keep it small.' },
    { role: 'assistant', content: '', tool_calls: [{ id: 'call_3', type: 'function', function: { name: 'run_in_terminal', arguments: '{}' } }] },
  ]);
});

test('what the model cannot use is left out: cache markers and thinking; data that is text is read as text', () => {
  const out = msg.toOpenAiMessages([
    message(
      Role.User,
      'a',
      LanguageModelDataPart.text('b'),
      LanguageModelDataPart.json({ c: 1 }),
      new LanguageModelDataPart(new Uint8Array([1, 2]), 'cache_control'),
      { value: ['an earlier thought'], id: 't1' },
      null,
      42
    ),
    message(Role.User, new LanguageModelDataPart(new Uint8Array([1, 2]), 'cache_control')),
  ]);
  assert.deepEqual(out, [{ role: 'user', content: 'ab{"c":1}' }]);
  // A structured (prompt-tsx) value in a tool result is read as its JSON, and a result that failed is still a result.
  const result = msg.toOpenAiMessages([
    message(Role.Assistant, new LanguageModelToolCallPart('c9', 'read_file', {})),
    message(Role.User, Object.assign(new LanguageModelToolResultPart('c9', [{ value: { node: 'text', text: 'hi' } }]), { isError: true })),
  ]);
  assert.equal(result[1].content, '{"node":"text","text":"hi"}');
  assert.equal(result[1].tool_call_id, 'c9');
});

test('an image is refused with the reason, in a message or inside a tool result, and no request is made', async () => {
  const png = new Uint8Array([137, 80, 78, 71]);
  const refused = (e) => e instanceof msg.UnsupportedRequest && /image/.test(e.message);
  assert.throws(() => msg.toOpenAiMessages([message(Role.User, 'look', LanguageModelDataPart.image(png, 'image/png'))]), refused);
  assert.throws(() => msg.toOpenAiMessages([message(Role.User, new LanguageModelToolResultPart('c', [LanguageModelDataPart.image(png, 'image/jpeg')]))]), refused);

  const client = fakeClient(() => { throw new Error('nothing may be sent'); });
  const { error, parts } = await run(client, [message(Role.User, 'look', LanguageModelDataPart.image(png, 'image/png'))]);
  assert.ok(refused(error), String(error));
  assert.equal(client.calls.length, 0);
  assert.deepEqual(parts, []);
});

test('tools: names, descriptions and schemas go through; a tool with no schema takes no arguments', () => {
  const out = msg.toOpenAiTools([tool('read_file', { filePath: { type: 'string' } }), { name: 'status', description: 'status' }, { name: '' }, null]);
  assert.deepEqual(out, [
    { type: 'function', function: { name: 'read_file', description: 'the read_file tool', parameters: { type: 'object', properties: { filePath: { type: 'string' } } } } },
    { type: 'function', function: { name: 'status', description: 'status', parameters: { type: 'object', properties: {} } } },
  ]);
  assert.deepEqual(msg.toOpenAiTools(undefined), []);
});

test('every content that leaves is a string, and the tools and the session go with the request', async () => {
  const client = fakeClient(answering('ok'));
  await run(client, [
    message(Role.System, 'rules'),
    message(Role.User, 'go', LanguageModelDataPart.text(' on')),
    message(Role.Assistant, new LanguageModelToolCallPart('c1', 'read_file', { filePath: 'a' })),
    message(Role.User, new LanguageModelToolResultPart('c1', [new LanguageModelTextPart('x'), LanguageModelDataPart.json({ y: 1 })])),
  ], { request: { tools: READ_TOOLS } });
  const [call] = client.calls;
  for (const m of call.messages) assert.equal(typeof m.content, 'string', JSON.stringify(m));
  assert.deepEqual(call.tools.map((t) => t.function.name), ['read_file', 'list_dir', 'run_in_terminal']);
  assert.match(call.options.sessionId, /^[0-9a-f-]{36}$/);
  // No progress line is asked for (a wait is never reply text), and a place-in-line listener is, so the client does not word it either.
  assert.equal(call.onLoadingProgress, undefined);
  assert.equal(typeof call.options.onQueue, 'function');
  assert.ok(call.signal instanceof AbortSignal, 'Stop reaches the request');
});

// ---- secrets ----------------------------------------------------------------------------------------------------------

function dirtyConversation() {
  return [
    message(Role.System, `Rules. Deploy key: ${ghp}`),
    message(Role.User, `password: ${dbPassword}\nplease deploy`),
    message(Role.Assistant, 'Reading.', new LanguageModelToolCallPart('c1', 'run_in_terminal', { command: `curl -H "Authorization: Bearer ${ghp}" x` })),
    message(Role.User, new LanguageModelToolResultPart('c1', [new LanguageModelTextPart(`TOKEN="${ghp}"`)])),
  ];
}

test('the provider scrubs on its own: secrets in the system prompt, the user\'s words, a call\'s arguments and a result never reach the client', async () => {
  const client = fakeClient(answering('Done.'));
  await run(client, dirtyConversation());
  const everything = JSON.stringify(client.calls[0].messages);
  assert.doesNotMatch(everything, new RegExp(ghp));
  assert.doesNotMatch(everything, new RegExp(dbPassword));
  assert.match(everything, /Rules\./);
  assert.match(everything, /please deploy/);
});

test('and on the wire: the coordinator receives the system role first and none of the secrets', () =>
  withCoordinator((hit, res) => sseAnswer(res, sseText('Done.')), async (c, client) => {
    const { error, text } = await run(client, dirtyConversation(), { request: { tools: READ_TOOLS } });
    assert.equal(error, undefined);
    assert.equal(text, 'Done.');
    const { body } = c.hits[0];
    const wire = JSON.stringify(body);
    assert.doesNotMatch(wire, new RegExp(ghp));
    assert.doesNotMatch(wire, new RegExp(dbPassword));
    assert.deepEqual(body.messages.map((m) => m.role), ['system', 'user', 'assistant', 'tool']);
    assert.equal(body.stream, true);
    assert.equal(body.max_tokens, AGENT_MAX_TOKENS);
    assert.ok(!('model' in body), 'no model is named: the swarm serves its own');
    assert.equal(body.tools.length, 3);
  }));

// ---- the answer ------------------------------------------------------------------------------------------------------

test('the answer is shown as it arrives, in order, and choosing the entry writes no setting', async () => {
  stub.reset();
  const { parts, text, error } = await run(fakeClient(answering('Hel', 'lo ', 'world')), ask());
  assert.equal(error, undefined);
  assert.equal(text, 'Hello world');
  assert.ok(parts.length >= 1 && parts.every((p) => p instanceof LanguageModelTextPart));
  assert.deepEqual(stub.state.updates, [], 'picking the entry must not write dlne.chatModel');
});

test('thinking and tool-call tags are never shown, whatever the pieces; the call is passed on', async () => {
  const pieces = ['<think>plan', ' it</think>Hel', 'lo.\n<tool_c', 'all>{"name":"read_file","arguments":{"filePath":"a.ts"}}</tool', '_call>\nDone'];
  const { text, calls, error } = await run(fakeClient(answering(...pieces)), ask(), { request: { tools: READ_TOOLS } });
  assert.equal(error, undefined);
  assert.equal(text, 'Hello.\n\nDone');
  assert.equal(calls.length, 1);
  assert.equal(calls[0].name, 'read_file');
  assert.deepEqual(calls[0].input, { filePath: 'a.ts' });
  assert.match(calls[0].id, /^call_[0-9a-f]{16}$/);
});

test('a native tool call comes after the text, with the engine\'s id and parsed arguments', async () => {
  const client = fakeClient((n, { onDelta }) => {
    onDelta('Looking.');
    return {
      content: 'Looking.',
      toolCalls: [
        { id: 'call_9', name: 'read_file', arguments: '{"filePath":"a.ts"}' },
        { id: 'call_10', name: 'run_in_terminal', arguments: '' },
      ],
      finishReason: 'tool_calls',
    };
  });
  const { parts, error } = await run(client, ask(), { request: { tools: READ_TOOLS } });
  assert.equal(error, undefined);
  assert.deepEqual(parts.map((p) => (p instanceof LanguageModelTextPart ? ['text', p.value] : [p.callId, p.name, p.input])), [
    ['text', 'Looking.'],
    ['call_9', 'read_file', { filePath: 'a.ts' }],
    ['call_10', 'run_in_terminal', {}],
  ]);
});

test('a native call for a tool VS Code did not offer is dropped; if nothing else is left, the user is told', async () => {
  const wrong = { id: 'c1', name: 'write_file', arguments: '{"path":"x"}' };
  const only = await run(fakeClient(() => ({ content: '', toolCalls: [wrong], finishReason: 'tool_calls' })), ask(), { request: { tools: READ_TOOLS } });
  assert.deepEqual(only.calls, []);
  assert.match(only.error.message, /asked for a tool that VS Code did not offer \(write_file\)/);

  // With text before it, the text stays and the error follows: ending the turn as if the answer were complete would
  // leave the user watching for a step that was never taken.
  const withText = await run(fakeClient((n, { onDelta }) => { onDelta('I will write it now.'); return { content: 'I will write it now.', toolCalls: [wrong] }; }), ask(), { request: { tools: READ_TOOLS } });
  assert.equal(withText.text, 'I will write it now.');
  assert.deepEqual(withText.calls, []);
  assert.match(withText.error.message, /did not offer \(write_file\)/);

  // One good call among the wrong ones is passed on, and the wrong one is only left out.
  const mixed = await run(fakeClient(() => ({ content: '', toolCalls: [wrong, { id: 'c2', name: 'read_file', arguments: '{"filePath":"a"}' }] })), ask(), { request: { tools: READ_TOOLS } });
  assert.equal(mixed.error, undefined);
  assert.deepEqual(mixed.calls, [{ id: 'c2', name: 'read_file', input: { filePath: 'a' } }]);

  const noTools = await run(fakeClient(() => ({ content: '', toolCalls: [{ id: 'c1', name: 'read_file', arguments: '{}' }] })), ask());
  assert.match(noTools.error.message, /did not offer \(read_file\)/, 'with no tools offered, every call is for a tool VS Code did not offer');
});

test('a call cut off mid-arguments is never run or repaired: it is dropped, and the user is told why', async () => {
  const cut = { id: 'c1', name: 'run_in_terminal', arguments: '{"command":"rm -rf /tmp/some' };
  const only = await run(fakeClient(() => ({ content: '', toolCalls: [cut], finishReason: 'length' })), ask(), { request: { tools: READ_TOOLS } });
  assert.deepEqual(only.calls, []);
  assert.equal(only.error.message, prov.CUT_OFF_CALL);

  // An answer that ran to the length limit with text is a valid answer, as VS Code's own providers treat it.
  const withText = await run(fakeClient((n, { onDelta }) => { onDelta('The plan is long and'); return { content: 'The plan is long and', toolCalls: [], finishReason: 'length' }; }), ask());
  assert.equal(withText.error, undefined);
  assert.equal(withText.text, 'The plan is long and');
});

test('a call written as text is passed on only for an offered tool, in every shape the agent loop reads', async () => {
  const shapes = {
    'a tool_call tag': '<tool_call>{"name":"read_file","arguments":{"filePath":"a.ts"}}</tool_call>',
    'a tools tag': '<tools>{"name":"read_file","arguments":{"filePath":"a.ts"}}</tools>',
    'a function-call tag': '<function-call>{"name":"read_file","arguments":{"filePath":"a.ts"}}</function-call>',
    'arguments as a JSON string': '<tool_call>{"name":"read_file","arguments":"{\\"filePath\\":\\"a.ts\\"}"}</tool_call>',
    'tool and parameters': '<tool_call>{"tool":"read_file","parameters":{"filePath":"a.ts"}}</tool_call>',
    'a function wrapper': '<tool_call>{"type":"function","function":{"name":"read_file","arguments":"{\\"filePath\\":\\"a.ts\\"}"}}</tool_call>',
    'flattened arguments': '<tool_call>{"name":"read_file","filePath":"a.ts"}</tool_call>',
    'a fenced block': 'Reading it.\n```json\n{"name": "read_file", "arguments": {"filePath": "a.ts"}}\n```',
    'a tag with parameters': '<tool_call name="read_file"><param name="filePath">a.ts</param></tool_call>',
    'a bare object': 'Sure:\n{"name": "read_file", "arguments": {"filePath": "a.ts"}}\ndone',
  };
  for (const [how, answer] of Object.entries(shapes)) {
    const { calls, error } = await run(fakeClient(answering(answer)), ask(), { request: { tools: READ_TOOLS } });
    assert.equal(error, undefined, how);
    assert.equal(calls.length, 1, how);
    assert.equal(calls[0].name, 'read_file', how);
    assert.deepEqual(calls[0].input, { filePath: 'a.ts' }, how);
    // ...and for an editor that offered no such tool, it is not a call at all.
    const other = await run(fakeClient(answering(answer)), ask(), { request: { tools: [tool('list_dir')] } });
    assert.deepEqual(other.calls, [], `${how}, tool not offered`);
  }
});

test('several calls in one answer come in order; tag text is hidden, and braces inside strings do not confuse it', async () => {
  const answer = 'First.\n<tool_call>{"name":"run_in_terminal","arguments":{"command":"echo \\"}\\" && ls"}}</tool_call>\n' +
    '<tool_call>{"name":"read_file","arguments":{"filePath":"b.ts"}}</tool_call>';
  const { text, calls } = await run(fakeClient(answering(answer)), ask(), { request: { tools: READ_TOOLS } });
  assert.equal(text, 'First.\n\n');
  assert.deepEqual(calls.map((c) => [c.name, c.input]), [['run_in_terminal', { command: 'echo "}" && ls' }], ['read_file', { filePath: 'b.ts' }]]);
  assert.notEqual(calls[0].id, calls[1].id);
  // Calls written one right after the other on a line are still calls: a block that ends starts the next one afresh.
  const together = '<tool_call>{"name":"read_file","arguments":{"filePath":"a"}}</tool_call><tool_call>{"name":"list_dir","arguments":{"path":"."}}</tool_call>';
  const both = await run(fakeClient(answering(together)), ask(), { request: { tools: READ_TOOLS } });
  assert.deepEqual(both.calls.map((c) => c.name), ['read_file', 'list_dir']);
  assert.equal(both.text, '');
  const two = '<tool_call>{"name":"read_file","arguments":{"filePath":"a"}}{"name":"list_dir","arguments":{"path":"."}}</tool_call>';
  assert.deepEqual((await run(fakeClient(answering(two)), ask(), { request: { tools: READ_TOOLS } })).calls.map((c) => c.name), ['read_file', 'list_dir']);
});

test('a text call that is cut off, or cannot be read, runs nothing and says so; it is not repaired', async () => {
  for (const answer of [
    'Starting.\n<tool_call>{"name": "read_file", "arguments": {"filePath": "a.',
    '<tool_call>{"name": "read_file", "arguments": {"filePath": "a.</tool_call>',
    '<tool_call>{"name": "read_file", "arguments": "not json at all"}</tool_call>',
  ]) {
    const { calls, error } = await run(fakeClient(answering(answer)), ask(), { request: { tools: READ_TOOLS } });
    assert.deepEqual(calls, [], answer);
    assert.equal(error && error.message, prov.CUT_OFF_CALL, answer);
  }
});

test('an answer that talks about a <tools> element, or quotes a call or <think>, arrives whole: no hidden text, no call, no "cut off" error', async () => {
  const call = '<tool_call>{"name":"read_file","arguments":{"filePath":"a.ts"}}</tool_call>';
  const answers = [
    ['In your pom.xml add a ', '<tools>', '<tool>x</tool>', ' section, then run it.'],
    ['Models write <tools ', 'around JSON; the closing tag is optional here.'],
    ['Qwen uses `<think>` to open its scratchpad.', ' It closes with `</think>`.'],
    [`To read a file the model writes ${call} and VS Code runs it.`],
    [`Example:\n\`\`\`xml\n${call}\n\`\`\`\nThat is all.`],
  ];
  for (const tools of [[], READ_TOOLS]) {
    for (const pieces of answers) {
      const { text, calls, error } = await run(fakeClient(answering(...pieces)), ask(), { request: { tools } });
      assert.equal(error, undefined, `${pieces.join('')} (tools offered: ${tools.length})`);
      assert.equal(text, pieces.join(''), `all of it is shown (tools offered: ${tools.length})`);
      assert.deepEqual(calls, [], 'a quoted call runs nothing');
    }
  }
});

test('the coordinator\'s notice is an error for VS Code to show, never reply text', async () => {
  const sentence = 'The cluster is reorganising itself right now (a node joined or left, or an engine is being restarted). ' +
    'Nothing has gone wrong — please send your message again in a moment.';
  const { error, parts } = await run(fakeClient(answering(`\n\n[System] ${sentence}`)), ask());
  assert.equal(error.message, sentence);
  assert.deepEqual(parts, [], 'nothing was reported as text');

  for (const [notice, expected] of [
    ['\n\n[System] This model has not finished downloading to the cluster yet. It will start automatically once the weights are in place — please try again shortly.', 'This model has not finished downloading to the cluster yet. It will start automatically once the weights are in place — please try again shortly.'],
    ['\n\n[System Alert] The engine stream failed (ReadError). This has been logged. Please send your message again.', 'The engine stream failed (ReadError). This has been logged. Please send your message again.'],
    ['\n\n[System] Feature Disabled by Coordinator Administrator (403).', 'Feature Disabled by Coordinator Administrator (403).'],
  ]) {
    assert.equal((await run(fakeClient(answering(notice)), ask())).error.message, expected);
  }
});

test('a notice that follows real text: the text stays, the error follows; a tool call in the same answer is not passed on', async () => {
  const client = fakeClient((n, { onDelta }) => {
    onDelta('Half of an answer');
    onDelta('\n\n[System Alert] The engine stream failed (ReadError). This has been logged. Please send your message again.');
    return { content: 'Half of an answer', toolCalls: [{ id: 'c1', name: 'read_file', arguments: '{}' }] };
  });
  const { text, calls, error } = await run(client, ask(), { request: { tools: READ_TOOLS } });
  assert.equal(text, 'Half of an answer');
  assert.deepEqual(calls, []);
  assert.match(error.message, /^The engine stream failed/);
});

test('the engine\'s overflow alert is a clear error, and from then on the Chat view is told to count higher', async () => {
  const provider = new prov.DlneChatModelProvider(fakeClient(answering(
    '\n\n[System Alert] Error from internal engine: request (60000 tokens) exceeds the available context size (54613 tokens), try increasing it..'
  )));
  const sample = 'x'.repeat(3000);
  const before = await provider.provideTokenCount(MODEL, sample, makeToken());
  const { error, parts } = await run(null, ask(), { provider });
  assert.equal(error.message, prov.OVERFLOW_ERROR);
  assert.deepEqual(parts, []);
  assert.ok(await provider.provideTokenCount(MODEL, sample, makeToken()) > before, 'the next count is higher, so VS Code trims sooner');

  // The same sentence as an error from the client (a refusal's detail) reads the same.
  const refused = new prov.DlneChatModelProvider(fakeClient(() => { throw new ServerRefusal(400, 'request exceeds the available context size'); }));
  assert.equal((await run(null, ask(), { provider: refused })).error.message, prov.OVERFLOW_ERROR);
});

test('model text that merely mentions [System] is text: a coordinator notice is a whole chunk of its own', async () => {
  const { text, error } = await run(fakeClient(answering('See the log: ', '[System', '] started', ' and the [System Alert] rows')), ask());
  assert.equal(error, undefined);
  assert.equal(text, 'See the log: [System] started and the [System Alert] rows');
});

test('the swarm\'s refusals are shown in its own words; any other failure says what it was', async () => {
  const trial = 'DLNE Compute Required: Free starter quota (100/100 inferences) used. Run \'@dlne /contribute\' to donate idle GPU compute or provide an authorized API key.';
  assert.equal((await run(fakeClient(() => { throw new ServerRefusal(403, trial); }), ask())).error.message, trial);
  const window = 'You\'ve used all 300 coding chats of this window. It frees up in 42 minutes.';
  assert.equal((await run(fakeClient(() => { throw new ServerRefusal(429, window); }), ask())).error.message, window);
  const down = await run(fakeClient(() => { throw Object.assign(new Error('connect ECONNREFUSED 127.0.0.1:1'), { code: 'ECONNREFUSED' }); }), ask());
  assert.equal(down.error.message, 'Could not get an answer from the DLNE swarm: connect ECONNREFUSED 127.0.0.1:1');
});

test('an empty answer is an error with a reason, not a silent nothing', async () => {
  const { error, parts } = await run(fakeClient(() => ({ content: '', toolCalls: [] })), ask());
  assert.equal(error.message, prov.EMPTY_REPLY);
  assert.deepEqual(parts, []);
  assert.equal((await run(fakeClient(answering('  \n ')), ask())).error.message, prov.EMPTY_REPLY, 'blanks are not an answer');
  assert.equal((await run(fakeClient(answering('<think>only thinking</think>')), ask())).error.message, prov.EMPTY_REPLY);
});

test('an error for the user has no stack: the Chat view prints a stack under the message, paths from this machine and all', async () => {
  // (Seen live on VS Code 1.143.1 with Copilot Chat 0.71: "Reason: <message>: Error: <message> at DlneChatModelProvider...")
  const restore = fastTimers();
  try {
    const cases = [
      fakeClient(answering('\n\n[System] Please send it again.')),
      fakeClient(answering('\n\n[System Alert] Error from internal engine: request exceeds the available context size.')),
      fakeClient(() => ({ content: '', toolCalls: [] })),
      fakeClient(answering('<tool_call>{"name": "read_file", "arguments": {')),
      fakeClient(() => { throw new EngineDropped('engine_dropped', false); }),
      fakeClient(() => { throw new ServerRefusal(403, 'The free trial is used up.'); }),
      fakeClient(() => { throw new Error('connect ECONNREFUSED'); }),
    ];
    for (const client of cases) {
      const { error } = await run(client, ask(), { request: { tools: READ_TOOLS } });
      assert.ok(error instanceof Error && error.message, 'an error with a sentence');
      assert.equal(error.stack, undefined, error.message);
    }
    const image = await run(fakeClient(), [message(Role.User, LanguageModelDataPart.image(new Uint8Array(4), 'image/png'))]);
    assert.equal(image.error.stack, undefined);
  } finally {
    restore();
  }
});

test('a request that failed is written to the DLNE log, once, with the sentence the user saw', async () => {
  logLines.length = 0;
  const sentence = 'Log test: the engine fell over (it is only a test).';
  await run(fakeClient(() => { throw new ServerRefusal(500, sentence); }), ask());
  await run(fakeClient(() => { throw new ServerRefusal(500, sentence); }), ask());
  assert.deepEqual(logLines.filter((l) => l.includes(sentence)).length, 1, logLines.join('\n'));
  assert.match(logLines.find((l) => l.includes(sentence)), /DLNE Swarm: a request failed: Log test/);
});

// ---- a dropped engine ---------------------------------------------------------------------------------------------------

test('a dropped step is sent again after the agent loop\'s waits, and the answer is shown once', async () => {
  const waits = [];
  const restore = fastTimers(waits);
  try {
    logLines.length = 0;
    const client = fakeClient((n, { onDelta }) => {
      if (n <= 2) throw new EngineDropped(n === 1 ? 'engine_unavailable' : 'engine_dropped', false);
      onDelta('All done.');
      return { content: 'All done.', toolCalls: [] };
    });
    const { text, error } = await run(client, ask());
    assert.equal(error, undefined);
    assert.equal(text, 'All done.');
    assert.equal(client.calls.length, 3);
    assert.deepEqual(waits, [engineDropWaitMs(1), engineDropWaitMs(2)]);
    assert.ok(logLines.some((l) => /engine dropped the request \(engine_unavailable\); sending it again \(1\/8\)/.test(l)));
  } finally {
    restore();
  }
});

test('nothing is retried once text has been shown: it would be appended to the first try. The user is told the text is incomplete', async () => {
  const restore = fastTimers();
  try {
    const client = fakeClient((n, { onDelta }) => {
      onDelta('Half an ans');
      throw new EngineDropped('engine_dropped', true);
    });
    const { text, error } = await run(client, ask());
    assert.equal(client.calls.length, 1);
    assert.equal(text, 'Half an ans');
    assert.equal(error.message, prov.DROP_MID_ANSWER);
  } finally {
    restore();
  }
});

test('thinking that was never shown does not stop a retry, even though the client saw part of the answer', async () => {
  const restore = fastTimers();
  try {
    const client = fakeClient((n, { onDelta }) => {
      if (n === 1) {
        onDelta('<think>plan');
        throw new EngineDropped('engine_dropped', true);
      }
      onDelta('Fine.');
      return { content: 'Fine.', toolCalls: [] };
    });
    const { text, error } = await run(client, ask());
    assert.equal(error, undefined);
    assert.equal(client.calls.length, 2);
    assert.equal(text, 'Fine.');
  } finally {
    restore();
  }
});

test('an engine that keeps dropping ends it after the same number of tries as the agent loop, with a plain sentence', async () => {
  const waits = [];
  const restore = fastTimers(waits);
  try {
    const client = fakeClient(() => { throw new EngineDropped('engine_unavailable', false); });
    const { error, parts } = await run(client, ask());
    assert.equal(client.calls.length, ENGINE_DROP_RETRIES + 1);
    assert.deepEqual(waits, Array.from({ length: ENGINE_DROP_RETRIES }, (_, i) => engineDropWaitMs(i + 1)));
    assert.equal(error.message, prov.DROP_GIVE_UP);
    assert.deepEqual(parts, []);
  } finally {
    restore();
  }
});

test('a free-trial user\'s silent step is sent again by the provider like everyone else\'s', async () => {
  const waits = [];
  const restore = fastTimers(waits);
  try {
    const client = Object.assign(fakeClient((n, { onDelta }) => {
      if (n === 1) throw new EngineDropped('stream_silent', false);
      onDelta('Back again.');
      return { content: 'Back again.', toolCalls: [] };
    }), { getQuotaRemaining: () => 3 }); // a trial user: the coordinator's "chats left" header was seen
    const { text, error } = await run(client, ask());
    assert.equal(error, undefined);
    assert.equal(text, 'Back again.');
    assert.equal(client.calls.length, 2);
    assert.deepEqual(waits, [engineDropWaitMs(1)]);
  } finally {
    restore();
  }
});

test('Stop during the wait before a retry ends it at once: no new request, no error', async () => {
  const client = fakeClient(() => { throw new EngineDropped('engine_unavailable', false); });
  const token = makeToken();
  const pending = run(client, ask(), {}, token);
  await wait(30);
  assert.equal(client.calls.length, 1);
  token.cancel();
  const { error, parts } = await pending;
  assert.equal(error, undefined);
  assert.deepEqual(parts, []);
  assert.equal(client.calls.length, 1);
  assert.equal(token.listenerCount, 0, 'the cancellation listener is released');
});

// ---- the wait, Stop, and the conversation's session ----------------------------------------------------------------------

test('a request that waits for a slot waits silently: the place in line goes to the status bar\'s listeners, never into the reply', () =>
  withCoordinator((hit, res) => {
    res.writeHead(200, { 'Content-Type': 'text/event-stream' });
    res.write(sseFrame({ dlne_queue: { position: 2, waiting: 2, slots: 2, in_use: 2 } }));
    setTimeout(() => { res.write(sseText('Your turn: ')); res.write(sseText('answer.')); res.end('data: [DONE]\n\n'); }, 150);
  }, async (c, client) => {
    const heard = [];
    const off = client.onQueue((q) => heard.push(q));
    const { parts, text, error } = await run(client, ask());
    off();
    assert.equal(error, undefined);
    assert.equal(text, 'Your turn: answer.');
    assert.ok(parts.every((p) => p instanceof LanguageModelTextPart));
    assert.doesNotMatch(text, /line|wait|slot|queue/i);
    assert.ok(heard.some((q) => q && q.position === 2), 'the status bar is told where the request stands');
    assert.equal(heard[heard.length - 1], null, 'and told when it is served');
    assert.equal(client.currentQueue(), null);
  }));

test('the coordinator\'s own notice inside a real stream is an error, not reply text', () =>
  withCoordinator((hit, res) => sseAnswer(res, sseText('\n\n[System] Sorry, the distributed network is currently maxed out. The currently active models are: [x.gguf]. Please switch your chat to one of those models for immediate responses.')), async (c, client) => {
    const { error, parts } = await run(client, ask());
    assert.match(error.message, /^Sorry, the distributed network is currently maxed out\./);
    assert.deepEqual(parts, []);
  }));

test('a restart of the engine, as 2.6.2 tags it, is waited out and retried, with the text shown once', () =>
  withCoordinator((hit, res, n) => {
    if (n === 1) {
      sseAnswer(res, sseFrame({ choices: [{ delta: { content: '\n\n[System] The cluster is reorganising itself right now.' } }], dlne_error: 'engine_unavailable' }));
    } else {
      sseAnswer(res, sseText('Back up.'));
    }
  }, async (c, client) => {
    const restore = fastTimers();
    try {
      const { text, error } = await run(client, ask());
      assert.equal(error, undefined);
      assert.equal(text, 'Back up.');
      assert.equal(c.hits.length, 2);
    } finally {
      restore();
    }
  }));

test('Stop drops the request at once, which frees the slot, and nothing more is shown', () => {
  let closed = false;
  return withCoordinator((hit, res) => {
    res.on('close', () => { closed = true; });
    res.writeHead(200, { 'Content-Type': 'text/event-stream' });
    res.write(sseText('Thinking about '));
    // ...and the answer never ends.
  }, async (c, client) => {
    const token = makeToken();
    const pending = run(client, ask(), {}, token);
    for (let i = 0; i < 100 && c.hits.length === 0; i++) await wait(10);
    await wait(100); // the first words have arrived and are shown
    token.cancel();
    const { error, text } = await pending;
    assert.equal(error, undefined, 'Stop is not a failure');
    assert.equal(text, 'Thinking about ');
    for (let i = 0; i < 100 && !closed; i++) await wait(10);
    assert.ok(closed, 'the coordinator saw the connection close');
    assert.equal(token.listenerCount, 0);
  });
});

test('a request that is already cancelled makes no request', async () => {
  const client = fakeClient(answering('x'));
  const token = makeToken();
  token.cancel();
  const { parts, error } = await run(client, ask(), {}, token);
  assert.equal(client.calls.length, 0);
  assert.equal(error, undefined);
  assert.deepEqual(parts, []);
});

test('each conversation has its own X-DLNE-Session, a random one; without a conversation id they share one', () =>
  withCoordinator((hit, res) => sseAnswer(res, sseText('ok')), async (c, client) => {
    const provider = new prov.DlneChatModelProvider(client);
    const as = (conversation) => run(client, ask(), { provider, request: { modelOptions: conversation === undefined ? {} : { _conversationId: conversation } } });
    await as('chat-A');
    await as('chat-B');
    await as('chat-A');
    await as(undefined);
    await as(undefined);
    const [a1, b, a2, n1, n2] = c.hits.map((h) => h.headers['x-dlne-session']);
    const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
    for (const id of [a1, b, n1]) assert.match(id, uuid);
    assert.equal(a1, a2, 'the same conversation keeps its session');
    assert.notEqual(a1, b);
    assert.equal(n1, n2, 'requests that name no conversation share one');
    assert.ok(![a1, b].includes(n1));
    assert.ok(!c.hits.some((h) => JSON.stringify(h.headers).includes('chat-A')), 'the conversation\'s own name never leaves');
  }));

test('the table of conversations stays small: a conversation that fell out of it gets a new random session when it returns', async () => {
  const client = fakeClient(answering('ok'));
  const provider = new prov.DlneChatModelProvider(client);
  const sessionOf = async (conversation) => {
    await run(client, ask(), { provider, request: { modelOptions: { _conversationId: conversation } } });
    return client.calls[client.calls.length - 1].options.sessionId;
  };
  const first = await sessionOf('chat-0');
  assert.equal(await sessionOf('chat-0'), first);
  for (let i = 1; i <= 70; i++) await sessionOf(`chat-${i}`);
  assert.notEqual(await sessionOf('chat-0'), first, 'the oldest was forgotten');
  assert.equal(await sessionOf('chat-70'), await sessionOf('chat-70'), 'a recent one is kept');
});

test('a spent free trial reaches the user in the coordinator\'s own words, through the real client', () =>
  withCoordinator((hit, res) => {
    res.writeHead(403, { 'Content-Type': 'application/json', 'X-DLNE-Quota-Remaining': '0' });
    res.end(JSON.stringify({ detail: 'DLNE Compute Required: Free starter quota (100/100 inferences) used. Run \'@dlne /contribute\' to donate idle GPU compute or provide an authorized API key.' }));
  }, async (c, client) => {
    const { error, parts } = await run(client, ask());
    assert.equal(error.message, 'DLNE Compute Required: Free starter quota (100/100 inferences) used. Run \'@dlne /contribute\' to donate idle GPU compute or provide an authorized API key.');
    assert.deepEqual(parts, []);
    assert.equal(c.hits.length, 1, 'a refusal is not asked for again');
    assert.equal(client.getQuotaRemaining(), 0);
  }));

// ---- counting tokens --------------------------------------------------------------------------------------------------------

test('tokens are counted from characters, for a text and for a message, without the network', async () => {
  const provider = new prov.DlneChatModelProvider(fakeClient());
  const count = (x) => provider.provideTokenCount(MODEL, x, makeToken());
  assert.equal(await count(''), 0);
  assert.equal(await count('abcdefghi'), 3);
  assert.equal(await count('abcdefghij'), 4);
  // a message: 16 characters of framing, its text, a call's name and arguments, a result's text
  const message1 = message(Role.User, 'abc', new LanguageModelToolResultPart('c', [new LanguageModelTextPart('defghi')]));
  assert.equal(await count(message1), Math.ceil((16 + 3 + 6) / 3));
  const call = message(Role.Assistant, new LanguageModelToolCallPart('c', 'read_file', { filePath: 'a' }));
  assert.equal(await count(call), Math.ceil((16 + 'read_file'.length + '{"filePath":"a"}'.length) / 3));
  assert.equal(await count(message(Role.User, LanguageModelDataPart.image(new Uint8Array(4), 'image/png'))), Math.ceil(16 / 3) + msg.IMAGE_TOKENS);
  assert.equal(await count({ role: 1, content: [] }), Math.ceil(16 / 3));
});

test('counting is fast: a megabyte of text is a division, not a scan', async () => {
  const provider = new prov.DlneChatModelProvider(fakeClient());
  const big = 'x'.repeat(1_000_000);
  const started = process.hrtime.bigint();
  for (let i = 0; i < 1000; i++) await provider.provideTokenCount(MODEL, big, makeToken());
  const ms = Number(process.hrtime.bigint() - started) / 1e6;
  assert.ok(ms < 250, `1000 counts of 1 MB took ${ms.toFixed(0)} ms`);
});

test('the count learns from what the swarm says its prompts really were, and never leaves 2 to 5 characters a token', async () => {
  const sample = 'x'.repeat(3000);
  const learner = async (charsPerToken) => {
    const client = fakeClient((n, { onDelta }) => {
      onDelta('ok');
      const sent = msg.chatChars(client.calls[n - 1].messages, client.calls[n - 1].tools);
      return { content: 'ok', toolCalls: [], promptTokens: Math.round(sent / charsPerToken) };
    });
    const provider = new prov.DlneChatModelProvider(client);
    const before = await provider.provideTokenCount(MODEL, sample, makeToken());
    for (let i = 0; i < 12; i++) await run(client, ask('y'.repeat(2000)), { provider });
    return { before, after: await provider.provideTokenCount(MODEL, sample, makeToken()) };
  };
  const dense = await learner(4.5); // prose-like text: fewer tokens than the first guess
  assert.equal(dense.before, 1000);
  assert.ok(dense.after < 800 && dense.after >= Math.ceil(3000 / 5), `${dense.after}`);
  const sparse = await learner(0.5); // six times fewer characters a token than expected: not believed, ignored
  assert.equal(sparse.after, 1000);
});

test('a reply too small to measure from, or a figure nobody could believe, does not move the count', () => {
  const e = new msg.TokenEstimator(3);
  e.calibrate(1000, 100);   // under 200 tokens: too small to trust
  e.calibrate(1000, undefined);
  e.calibrate(100000, 10000); // 10 chars a token: the coordinator rewrote the prompt, so not believed
  assert.equal(e.charsPerToken, 3);
  e.calibrate(40000, 10000); // 4 chars a token: believed, partway
  assert.ok(e.charsPerToken > 3 && e.charsPerToken < 4);
});

// ---- the reply filter, directly -----------------------------------------------------------------------------------------------

function shownBy(pieces, toolsOffered = true) {
  const filter = new rep.ReplyFilter(toolsOffered);
  let out = '';
  for (const piece of pieces) out += filter.push(piece);
  const end = filter.finish();
  return out + end.text;
}
const chunked = (text, size) => text.match(new RegExp(`[\\s\\S]{1,${size}}`, 'g')) || [];

/** `check(pieces)` over the answer whole, in pieces of 1 to 9 characters, and in seeded random pieces: every cut must read the same. */
function inEveryCut(answer, check) {
  let seed = 7;
  const random = () => (seed = (seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff;
  check([answer], 'whole');
  for (let size = 1; size <= 9; size++) check(chunked(answer, size), `pieces of ${size}`);
  for (let round = 0; round < 25; round++) {
    const pieces = [];
    for (let at = 0; at < answer.length;) {
      const take = 1 + Math.floor(random() * 8);
      pieces.push(answer.slice(at, at + take));
      at += take;
    }
    check(pieces, 'random pieces');
  }
}

test('what is shown does not depend on how the answer was cut into pieces (tools offered)', () => {
  const call = '<tool_call>{"name":"read_file","arguments":{"filePath":"a.ts"}}</tool_call>';
  const cases = [
    ['Plain text, no tags.', 'Plain text, no tags.'],
    [`<think>Plan: read it.</think>\n\nI will read the file.\n${call}`, 'I will read the file.\n'],
    ['Compare a < b and c <tools_list> then <thinking> ... </thinking> done', 'Compare a < b and c <tools_list> then <thinking> ... </thinking> done'],
    // a tag that does not open a line is ordinary text, however it is written
    ['\n\n  Leading blanks are dropped, <tool_call name="x"><param name="a">1</param></tool_call> trailing',
      'Leading blanks are dropped, <tool_call name="x"><param name="a">1</param></tool_call> trailing'],
    ['Unfinished <tool_call>{"name": "read_fi', 'Unfinished <tool_call>{"name": "read_fi'],
    ['<function-call>{"name":"x"}</function-call>after<tools>[]</tools>', 'after<tools>[]</tools>'],
    ['Ends with a lone <', 'Ends with a lone <'],
    ['if (a <tool_call) { }', 'if (a <tool_call) { }'],
    // a tag that opens a line is a call, with blanks before it, and after a block the next may follow at once
    [`Intro\n${call}\nOutro`, 'Intro\n\nOutro'],
    [`  ${call}\nok`, 'ok'],
    [`${call}${call}\nthe end`, 'the end'],
    // inside a code fence it is a sample, and it is a call again once the fence is closed
    [`Example:\n\`\`\`xml\n${call}\n\`\`\`\n${call}\nend`, `Example:\n\`\`\`xml\n${call}\n\`\`\`\n\nend`],
    ['~~~\n<tools>x</tools>\n~~~~\n<tools>y</tools>\nend', '~~~\n<tools>x</tools>\n~~~~\n\nend'],
    [`Code:\n\`\`\`\n${call}`, `Code:\n\`\`\`\n${call}`],
    // <think> is the start of the answer, and nowhere else
    ["I think <think> is Qwen's tag.\n<think>real?</think> end", "I think <think> is Qwen's tag.\n<think>real?</think> end"],
    ['<think>a</think><think>b</think>Answer', 'Answer'],
    ['\n<think>x</think>\n<think>y</think>\nAnswer', 'Answer'],
  ];
  for (const [answer, expected] of cases) {
    inEveryCut(answer, (pieces, how) => assert.equal(shownBy(pieces, true), expected, `${JSON.stringify(answer)} in ${how}`));
  }
});

test('with no tools offered a tool-call tag is only text, and only the answer\'s own thinking is hidden', () => {
  const call = '<tool_call>{"name":"read_file","arguments":{"filePath":"a.ts"}}</tool_call>';
  const cases = [
    [`In your pom.xml add a <tools> section:\n<tools>\n  <tool>x</tool>\n</tools>\nthen run it.`, `In your pom.xml add a <tools> section:\n<tools>\n  <tool>x</tool>\n</tools>\nthen run it.`],
    [`${call}\nafter`, `${call}\nafter`],
    ['<function-call>x</function-call>', '<function-call>x</function-call>'],
    ['<think>plan</think>\nAnswer <think>kept</think>', 'Answer <think>kept</think>'],
    ['Models write <tools around JSON; the closing tag is optional.', 'Models write <tools around JSON; the closing tag is optional.'],
  ];
  for (const [answer, expected] of cases) {
    inEveryCut(answer, (pieces, how) => assert.equal(shownBy(pieces, false), expected, `${JSON.stringify(answer)} in ${how}`));
  }
  const filter = new rep.ReplyFilter(false);
  filter.push(`text\n${call}\n`);
  assert.deepEqual(filter.finish(), { text: '', notice: null, unfinishedCall: false, sawCallBlock: false }, 'nothing was taken for a call');
});

test('seeded random answers: ordinary text is never lost, and no cut of the pieces changes the result', () => {
  let seed = 987;
  const random = () => (seed = (seed * 1664525 + 1013904223) >>> 0) / 2 ** 32;
  const pick = (list) => list[Math.floor(random() * list.length)];
  const fragments = ['<', '>', '<think>', '</think>', '<tool_call>', '</tool_call>', '<tools>', '</tools>', '<thin', 'k>', '<b>', 'text ', '\n', ' ', 'a<b', '`', '```', '~~~', '```json\n', '\n```\n'];
  const anyTag = /<(think|tools|function-call|tool_call)[\s>]/;
  for (let n = 0; n < 4000; n++) {
    const text = Array.from({ length: 1 + Math.floor(random() * 10) }, () => pick(fragments)).join('');
    for (const offered of [true, false]) {
      const whole = shownBy([text], offered);
      const pieces = [];
      for (let at = 0; at < text.length;) {
        const take = 1 + Math.floor(random() * 4);
        pieces.push(text.slice(at, at + take));
        at += take;
      }
      assert.equal(shownBy(pieces, offered), whole, `${JSON.stringify(text)} (tools offered: ${offered}) cut as ${JSON.stringify(pieces)}`);
      if (!anyTag.test(text)) assert.equal(whole, text.replace(/^\s+/, ''), `text changed without a tag: ${JSON.stringify(text)}`);
    }
  }
});

test('the filter says when a call was left open and when a notice came', () => {
  const open = new rep.ReplyFilter(true);
  open.push('Text\n<tool_call>{"name":');
  assert.deepEqual(open.finish(), { text: '', notice: null, unfinishedCall: true, sawCallBlock: true });
  const midline = new rep.ReplyFilter(true);
  midline.push('Text <tool_call>{"name":');
  assert.deepEqual(midline.finish(), { text: '', notice: null, unfinishedCall: false, sawCallBlock: false }, 'a tag that opens no line is not a call');
  const thinking = new rep.ReplyFilter(true);
  thinking.push('<think>unfinished thought');
  assert.deepEqual(thinking.finish(), { text: '', notice: null, unfinishedCall: false, sawCallBlock: false });
  const notice = new rep.ReplyFilter(true);
  assert.equal(notice.push('Some text.'), 'Some text.');
  assert.equal(notice.push('\n\n[System] Please send it again.'), '');
  assert.equal(notice.push(' More.'), '');
  assert.deepEqual(notice.finish(), { text: '', notice: '[System] Please send it again. More.', unfinishedCall: false, sawCallBlock: false });
});

test('a text call counts where the filter would hide it: at the start of a line, outside a code fence', () => {
  const offered = new Set(['read_file']);
  const call = '<tool_call>{"name":"read_file","arguments":{"filePath":"a.ts"}}</tool_call>';
  const names = (text) => rep.parseInlineToolCalls(text, offered).map((c) => c.name);
  assert.deepEqual(names(call), ['read_file']);
  assert.deepEqual(names(`Reading it.\n${call}`), ['read_file']);
  assert.deepEqual(names(`  ${call}`), ['read_file']);
  assert.deepEqual(names(`${call}${call}`), ['read_file', 'read_file'], 'one right after the other');
  assert.deepEqual(names(`Reading it. ${call}`), [], 'in the middle of a sentence');
  assert.deepEqual(names(`Write it as ${call} and I will run it.`), [], 'quoted');
  assert.deepEqual(names(`Example:\n\`\`\`xml\n${call}\n\`\`\``), [], 'in a code fence');
  assert.deepEqual(names(`~~~\n${call}\n~~~\n${call}`), ['read_file'], 'after the fence is closed');
  assert.deepEqual(names(`${call.replace('</tool_call>', '')}`), [], 'never closed: cut off');
  // a bare JSON object is held to the same rule
  const bare = '{"name": "read_file", "arguments": {"filePath": "a.ts"}}';
  assert.deepEqual(names(`Sure:\n${bare}\ndone`), ['read_file']);
  assert.deepEqual(names(`Sure: ${bare} done`), [], 'in the middle of a sentence');
  assert.deepEqual(names(`\`\`\`python\n${bare}\n\`\`\``), [], 'in a fence that is not a JSON block');
  assert.deepEqual(names(`\`\`\`json\n${bare}\n\`\`\``), ['read_file'], 'a fenced JSON block is a call as it always was');
  assert.deepEqual(names(`<tool_call name="read_file"><param name="filePath">a.ts</param></tool_call>`), ['read_file']);
  assert.deepEqual(names(`x <tool_call name="read_file"><param name="filePath">a.ts</param></tool_call>`), []);
  // the answer's own thinking is skipped, and only that
  assert.deepEqual(names(`<think>\n${call}\n</think>\nOK`), [], 'a call inside the answer\'s opening thinking is not a call');
  assert.deepEqual(names(`Hm. <think>\n${call}\n</think>`), ['read_file'], 'a <think> that does not open the answer is only text');
});

test('cleanNotice takes the marker off and joins the lines', () => {
  assert.equal(rep.cleanNotice('\n\n[System] The engine is still\nloading.'), 'The engine is still loading.');
  assert.equal(rep.cleanNotice('[System Alert] Error from internal engine: Status code 500.'), 'Error from internal engine: Status code 500.');
  assert.equal(rep.cleanNotice('[System Alert: Node disconnected mid-response.]\n\nThe cluster will reorganise.'), 'Node disconnected mid-response. The cluster will reorganise.');
});

test('native calls come first and text calls only when there are none; each is checked against the offered tools', () => {
  const offered = new Set(['read_file']);
  const text = '<tool_call>{"name":"read_file","arguments":{"filePath":"text.ts"}}</tool_call>';
  const native = [{ id: 'n1', name: 'read_file', arguments: '{"filePath":"native.ts"}' }];
  const ids = () => 'call_fixed';
  assert.deepEqual(rep.collectToolCalls(native, text, offered, ids).calls, [{ id: 'n1', name: 'read_file', input: { filePath: 'native.ts' } }]);
  assert.deepEqual(rep.collectToolCalls([], text, offered, ids).calls, [{ id: 'call_fixed', name: 'read_file', input: { filePath: 'text.ts' } }]);
  assert.deepEqual(rep.collectToolCalls([{ id: '', name: 'read_file', arguments: '[]' }], '', offered, ids), { calls: [], skipped: [{ name: 'read_file', why: 'bad-arguments' }] });
  assert.deepEqual(rep.collectToolCalls([{ id: '', name: 'read_file', arguments: '{}' }], '', offered, ids).calls, [{ id: 'call_fixed', name: 'read_file', input: {} }], 'an id the engine did not give is made');
  assert.deepEqual(rep.parseInlineToolCalls('<think><tool_call>{"name":"read_file"}</tool_call></think>', offered), [], 'a call inside the thinking is not a call');
});
