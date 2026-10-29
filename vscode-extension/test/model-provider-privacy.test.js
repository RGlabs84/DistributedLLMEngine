'use strict';
/**
 * DLNE Swarm in VS Code's model picker and private files (client/lmPrivacy.ts, the privacy cap in client/pipeline.ts, and the
 * first-use notice in client/modelProvider.ts).
 *
 * In `@dlne` DLNE's own tools read the files and can refuse the excluded ones. In the picker VS Code's tools read, and DLNE gets
 * text. What is still done there, and tested here:
 *   - a tool result whose call named an excluded file becomes the notice `read_file` gives, and so does the body of an
 *     excluded attachment; nothing of the file reaches the coordinator;
 *   - the request never goes to a volunteer's machine just because a repository is public (it is capped at "trusted"; only the
 *     user's own anyMember setting widens it);
 *   - the tool list is redacted like the messages;
 *   - the first use says what this path can and cannot keep private.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const stub = require('./helpers/vscode-stub');
const lm = require('./helpers/lm-stub');
const swarm = require('./helpers/mock-swarm');
const { generator } = require('./helpers/fake-secrets');

const { DlneClient } = require('../dist/client/dlneClient');
const prov = require('../dist/client/modelProvider');
const priv = require('../dist/client/lmPrivacy');
const { capPrivacy } = require('../dist/client/pipeline');
const { ToolRegistry } = require('../dist/agent/tools');
const { excludedNotice, excludedReason, forgetPrivacyModes } = require('../dist/privacy');
const { clearPrivacyCaches } = require('../dist/privacy/redact');

process.env.HOME = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-lm-privacy-home-'));

const { Role, message, makeToken, makeLm, LanguageModelTextPart, LanguageModelToolCallPart, LanguageModelToolResultPart } = lm;

const gen = generator(77);
const ghp = 'gh' + 'p_' + gen('alnum', 36);
const dbPassword = 'Tr0ub4dor' + gen('alnum', 6);

// Words no secret pattern knows: only the exclusion rules can keep them back.
const ENV_WORDS = 'FAVOURITE_FRUIT=dragonfruit42';
const PLAN_WORDS = 'the private plan: launch on friday';
const DRAFT_WORDS = 'an unfinished chapter about herons';
const NOTES_WORDS = 'ordinary notes about the build';

/** A workspace with an .env, a gitignored folder, a .dlneignore'd folder and an ordinary file; `body` gets its paths. */
async function withWorkspace(body) {
  stub.reset();
  clearPrivacyCaches();
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-lm-ws-')));
  const write = (rel, text) => {
    fs.mkdirSync(path.dirname(path.join(root, rel)), { recursive: true });
    fs.writeFileSync(path.join(root, rel), text);
  };
  write('.env', `${ENV_WORDS}\n`);
  write('.gitignore', 'private/\n*.secret\n');
  write('.dlneignore', 'drafts/\n');
  write('private/plan.txt', `${PLAN_WORDS}\n`);
  write('drafts/chapter.md', `${DRAFT_WORDS}\n`);
  write('notes.txt', `${NOTES_WORDS}\n`);
  stub.vscode.workspace.workspaceFolders = [{ uri: { fsPath: root, scheme: 'file' } }];
  try {
    await body({ root, at: (rel) => path.join(root, rel) });
  } finally {
    stub.vscode.workspace.workspaceFolders = undefined;
    clearPrivacyCaches();
    fs.rmSync(root, { recursive: true, force: true });
  }
}

const call = (id, name, args) => ({ id, type: 'function', function: { name, arguments: JSON.stringify(args) } });
const tool = (id, content) => ({ role: 'tool', tool_call_id: id, content });

// ---- which files a call names --------------------------------------------------------------------------------------------

test('the files a call names: the arguments VS Code\'s tools and MCP servers use for them', () => {
  assert.deepEqual(priv.filesNamedBy('{"filePath":"/a/b.ts","startLine":1}'), ['/a/b.ts']);
  assert.deepEqual(priv.filesNamedBy('{"filePaths":["/a","/b",7,""]}'), ['/a', '/b']);
  assert.deepEqual(priv.filesNamedBy('{"path":"x/y","uri":"file:///a/b","file":"c","file_path":"d"}').sort(), ['c', 'd', 'file:///a/b', 'x/y']);
  assert.deepEqual(priv.filesNamedBy('{"command":"cat .env","query":"password"}'), [], 'a command or a query names no file');
  for (const nothing of [undefined, '', 'not json', '[]', '"x"', 'null', '{"filePath":42}', '{"filePath":["", "  "]}']) {
    assert.deepEqual(priv.filesNamedBy(nothing), [], String(nothing));
  }
});

// ---- tool results ----------------------------------------------------------------------------------------------------------

test('a tool result whose call named an excluded file becomes the notice read_file gives; the others are left alone', () =>
  withWorkspace(async ({ root, at }) => {
    const history = [
      { role: 'user', content: 'read them' },
      { role: 'assistant', content: '', tool_calls: [
        call('c1', 'read_file', { filePath: at('.env') }),
        call('c2', 'read_file', { filePath: at('notes.txt') }),
        call('c3', 'read_file', { filePath: 'private/plan.txt' }),
        call('c4', 'read_file', { filePath: `file://${at('drafts/chapter.md')}` }),
        call('c5', 'get_errors', { filePaths: [at('notes.txt'), at('private/plan.txt')] }),
        call('c6', 'run_in_terminal', { command: 'cat .env' }),
        call('c7', 'list_dir', { path: root }),
      ] },
      tool('c1', ENV_WORDS),
      tool('c2', NOTES_WORDS),
      tool('c3', PLAN_WORDS),
      tool('c4', DRAFT_WORDS),
      tool('c5', 'two files, one of them private'),
      tool('c6', ENV_WORDS),
      tool('c7', 'the folder\'s listing'),
      tool('c-without-a-call', 'result of nothing we know'),
    ];
    const before = JSON.stringify(history);
    const out = priv.withoutExcludedFiles(history, root);
    assert.equal(JSON.stringify(history), before, 'the messages given are not changed');

    // the text is the one read_file's own refusal gives, naming the file as the call wrote it
    assert.equal(out[2].content, excludedNotice(at('.env'), excludedReason(at('.env'))));
    assert.match(out[2].content, /excluded for privacy/);
    assert.equal(out[4].content, excludedNotice('private/plan.txt', excludedReason(at('private/plan.txt'))), 'a relative path is read against the workspace');
    assert.match(out[4].content, /ignored by/);
    assert.equal(out[5].content, excludedNotice(`file://${at('drafts/chapter.md')}`, excludedReason(at('drafts/chapter.md'))), 'a file: URI');
    assert.match(out[6].content, /excluded for privacy/, 'one excluded file among those a call names is enough');
    // everything else as it was
    assert.equal(out[3], history[3]);
    assert.equal(out[7], history[7], 'a command is not a file argument');
    assert.equal(out[8], history[8], 'a listing of the folder');
    assert.equal(out[9], history[9], 'a result with no call before it');
    // nothing of the files that were named is left in the results
    for (const [index, words] of [[2, 'dragonfruit42'], [4, 'launch on friday'], [5, 'herons'], [6, 'private']]) {
      assert.ok(!out[index].content.includes(words) || index === 6, `${words} is still in result ${index}`);
    }
    // ...and the one thing this cannot see is a file read without being named: the terminal's output is as it was
    assert.ok(out[7].content.includes('dragonfruit42'));
  }));

test('a check that cannot be made counts as excluded, as read_file does it', () =>
  withWorkspace(async ({ at }) => {
    const exclude = require('../dist/privacy/exclude');
    const real = exclude.excludedReason;
    exclude.excludedReason = () => { throw new Error('the disk is gone'); };
    try {
      const out = priv.withoutExcludedFiles([
        { role: 'assistant', content: '', tool_calls: [call('c1', 'read_file', { filePath: at('notes.txt') })] },
        tool('c1', NOTES_WORDS),
      ], undefined);
      assert.match(out[1].content, /excluded for privacy \(the privacy check could not be completed\)/);
      assert.doesNotMatch(out[1].content, /ordinary notes/);
    } finally {
      exclude.excludedReason = real;
    }
  }));

// ---- attachments ----------------------------------------------------------------------------------------------------------

test('the body of an excluded attachment is replaced by the notice; the others, and the request itself, are not', () =>
  withWorkspace(async ({ root, at }) => {
    const text = [
      '<attachments>',
      `<attachment id="env" filePath="${at('.env')}">`,
      ENV_WORDS,
      '</attachment>',
      `<attachment id="notes" filePath="${at('notes.txt')}">`,
      NOTES_WORDS,
      '</attachment>',
      `<attachment id="ref" filePath="${at('private/plan.txt')}" />`,
      `<attachment id="plan" filePath='${at('private/plan.txt')}'>`,
      PLAN_WORDS,
      '</attachment>',
      `<attachment id="rel" filePath="drafts/chapter.md">`,
      DRAFT_WORDS,
      '</attachment>',
      '</attachments>',
      '<userRequest>',
      'what do these say?',
      '</userRequest>',
    ].join('\n');
    const out = priv.withoutExcludedAttachments(text, root);
    for (const hidden of ['dragonfruit42', 'launch on friday', 'herons']) assert.ok(!out.includes(hidden), `${hidden} is still there`);
    assert.ok(out.includes(NOTES_WORDS), 'an ordinary attachment is kept');
    assert.ok(out.includes('what do these say?'));
    assert.ok(out.includes('</attachments>'));
    assert.equal(out.split(excludedNotice(at('.env'), excludedReason(at('.env')))).length, 2, 'the notice of the .env');
    assert.equal(out.split('This file is excluded for privacy').length, 4, 'one notice for each of the three excluded files');
    assert.ok(out.includes(`<attachment id="ref" filePath="${at('private/plan.txt')}" />`), 'a tag that holds nothing is as it was');

    assert.equal(priv.withoutExcludedAttachments('no attachments here', root), 'no attachments here');
    assert.equal(priv.withoutExcludedAttachments(`<attachment id="n" filePath="${at('notes.txt')}">x</attachment>`, root).includes('x</attachment>'), true);
  }));

test('an attachment whose closing tag is missing is cut where the next part of the prompt begins', () =>
  withWorkspace(async ({ root, at }) => {
    const unclosed = `<attachments>\n<attachment id="env" filePath="${at('.env')}">\n${ENV_WORDS}\n</attachments>\n<userRequest>\nread it\n</userRequest>`;
    const out = priv.withoutExcludedAttachments(unclosed, root);
    assert.ok(!out.includes('dragonfruit42'));
    assert.ok(out.includes('</attachments>') && out.includes('read it'));
    const toTheEnd = priv.withoutExcludedAttachments(`before\n<attachment filePath="${at('.env')}">\n${ENV_WORDS}`, root);
    assert.ok(!toTheEnd.includes('dragonfruit42') && toTheEnd.startsWith('before\n'));
  }));

test('in the conversation, attachments in the user\'s turns are emptied the same way, whichever turn holds them', () =>
  withWorkspace(async ({ root, at }) => {
    const attach = (file, words) => `<attachments>\n<attachment id="x" filePath="${at(file)}">\n${words}\n</attachment>\n</attachments>`;
    const out = priv.withoutExcludedFiles([
      { role: 'system', content: attach('.env', ENV_WORDS) },
      { role: 'user', content: attach('.env', ENV_WORDS) },
      { role: 'assistant', content: 'ok' },
      { role: 'user', content: attach('notes.txt', NOTES_WORDS) },
    ], root);
    assert.ok(out[1].content.includes('excluded for privacy') && !out[1].content.includes('dragonfruit42'));
    assert.ok(out[3].content.includes(NOTES_WORDS));
    assert.equal(out[0].content.includes('dragonfruit42'), true, 'only the user\'s own turns carry attachments');
  }));

// ---- through the provider --------------------------------------------------------------------------------------------------

function fakeClient(script = (n, { onDelta }) => { onDelta('ok'); return { content: 'ok', toolCalls: [] }; }) {
  const calls = [];
  return {
    calls,
    async streamChat(messages, tools, onDelta, onToolCallStart, signal, onLoadingProgress, options) {
      calls.push({ messages, tools, options });
      return script(calls.length, { onDelta });
    },
  };
}

async function run(client, messages, request = {}, provider = new prov.DlneChatModelProvider(client)) {
  const parts = [];
  let error;
  try {
    await provider.provideLanguageModelChatResponse({ id: 'swarm-auto' }, messages, { tools: [], toolMode: 1, ...request }, { report: (p) => parts.push(p) }, makeToken());
  } catch (err) {
    error = err;
  }
  return { parts, error, provider, text: parts.filter((p) => p instanceof LanguageModelTextPart).map((p) => p.value).join('') };
}

const TOOLS = [{ name: 'read_file', description: 'reads a file', inputSchema: { type: 'object', properties: { filePath: { type: 'string' } } } }];

/** What VS Code's agent sends after reading the .env and a note, with an attached private file too. */
function agentConversation(at) {
  return [
    message(Role.System, 'You are an agent.'),
    message(Role.User, `<attachments>\n<attachment id="plan" filePath="${at('private/plan.txt')}">\n${PLAN_WORDS}\n</attachment>\n</attachments>\n<userRequest>\nsummarize my files\n</userRequest>`),
    message(Role.Assistant, new LanguageModelToolCallPart('c1', 'read_file', { filePath: at('.env') }), new LanguageModelToolCallPart('c2', 'read_file', { filePath: at('notes.txt') })),
    message(Role.User, new LanguageModelToolResultPart('c1', [new LanguageModelTextPart(ENV_WORDS)]), new LanguageModelToolResultPart('c2', [new LanguageModelTextPart(NOTES_WORDS)])),
  ];
}

test('through the provider: the contents of an excluded file never reach the client, and an ordinary one does', () =>
  withWorkspace(async ({ at }) => {
    const client = fakeClient();
    const { error } = await run(client, agentConversation(at), { tools: TOOLS });
    assert.equal(error, undefined);
    const sent = JSON.stringify(client.calls[0].messages);
    for (const hidden of ['dragonfruit42', 'launch on friday']) assert.ok(!sent.includes(hidden), `${hidden} was sent`);
    assert.ok(sent.includes(NOTES_WORDS), 'the ordinary file is read as usual');
    assert.ok(sent.includes('excluded for privacy'));
  }));

test('and on the wire: the coordinator receives the notice, not the file', () =>
  withWorkspace(async ({ at }) => {
    const coordinator = await swarm.startCoordinator({
      status: swarm.statusBody(),
      chat: (ctx) => swarm.plainStream(ctx.res, ['ok']),
    });
    stub.state.settings['dlne.serverUrl'] = coordinator.url;
    try {
      const { error, text } = await run(new DlneClient(), agentConversation(at), { tools: TOOLS });
      assert.equal(error, undefined);
      assert.equal(text, 'ok');
      const wire = JSON.stringify(coordinator.of('/v1/chat/completions')[0].body);
      assert.ok(!wire.includes('dragonfruit42') && !wire.includes('launch on friday'));
      assert.ok(wire.includes(NOTES_WORDS) && wire.includes('excluded for privacy'));
    } finally {
      coordinator.close();
    }
  }));

// ---- the privacy cap ----------------------------------------------------------------------------------------------------------

test('capPrivacy takes "open" down to "trusted" and leaves the user\'s own choice alone', () => {
  assert.equal(capPrivacy('open', 'trusted'), 'trusted');
  assert.equal(capPrivacy('trusted', 'trusted'), 'trusted');
  assert.equal(capPrivacy('any', 'trusted'), 'any', 'anyMember is the user\'s own choice');
  for (const mode of ['open', 'trusted', 'any']) assert.equal(capPrivacy(mode, undefined), mode, 'without a cap nothing changes');
});

/** A coordinator with coding members and the privacy mode the workspace would have; `body` gets what it was sent. */
async function withMembers(mode, body) {
  stub.reset();
  forgetPrivacyModes();
  const policy = require('../dist/privacy/policy');
  const real = policy.getPrivacyMode;
  policy.getPrivacyMode = async () => mode;
  const coordinator = await swarm.startCoordinator({
    status: swarm.statusBody(swarm.MEMBERS_ON),
    route: () => ({ status: 200, body: { route: null } }),
    chat: (ctx) => swarm.plainStream(ctx.res, ['ok']),
  });
  stub.state.settings['dlne.serverUrl'] = coordinator.url;
  try {
    await body(coordinator);
  } finally {
    coordinator.close();
    policy.getPrivacyMode = real;
    forgetPrivacyModes();
  }
}

test('a public repository (auto mode) does not send what VS Code\'s agent read to a volunteer: the picker\'s requests say "trusted"', () =>
  withMembers('open', async (coordinator) => {
    const client = new DlneClient();
    const { error } = await run(client, [message(Role.User, 'hello')]);
    assert.equal(error, undefined);
    const [picker] = coordinator.of('/v1/chat/completions');
    assert.equal(picker.headers['x-dlne-privacy'], 'trusted');
    assert.equal(coordinator.of('/v1/coding/route')[0].query.privacy, 'trusted', 'and the member is asked for as a trusted one');

    // @dlne's own requests (DLNE's tools chose those files, with the exclusion rules) keep the mode the repository has
    await client.streamChat([{ role: 'user', content: 'hello' }], [], () => {}, undefined, undefined, undefined, { sessionId: 'a-session-id-123' });
    assert.equal(coordinator.of('/v1/chat/completions')[1].headers['x-dlne-privacy'], 'open');
  }));

test('the user\'s own anyMember setting is still honoured in the picker, and "trusted" stays "trusted"', async () => {
  await withMembers('any', async (coordinator) => {
    await run(new DlneClient(), [message(Role.User, 'hello')]);
    assert.equal(coordinator.of('/v1/chat/completions')[0].headers['x-dlne-privacy'], 'any');
  });
  await withMembers('trusted', async (coordinator) => {
    await run(new DlneClient(), [message(Role.User, 'hello')]);
    assert.equal(coordinator.of('/v1/chat/completions')[0].headers['x-dlne-privacy'], 'trusted');
  });
});

// ---- the tool list -----------------------------------------------------------------------------------------------------------

test('secrets in the tool list (descriptions, defaults, examples, enums) never leave; names and structure do', async () => {
  const dirty = [
    {
      name: 'deploy_site',
      description: `Deploys with the key ${ghp} (do not share)`,
      inputSchema: {
        type: 'object',
        properties: {
          target: { type: 'string', description: 'where to', enum: ['staging', 'production'], default: 'staging' },
          config: { type: 'object', properties: { db: { type: 'string', default: `password: ${dbPassword}`, examples: [`token ${ghp}`, 'plain'] } } },
          retries: { type: 'integer', default: 3 },
        },
        required: ['target'],
      },
    },
    { name: 'read_file', description: 'reads a file', inputSchema: { type: 'object', properties: { filePath: { type: 'string' } } } },
  ];
  const client = fakeClient((n, { onDelta }) => { onDelta('ok'); return { content: 'ok', toolCalls: [] }; });
  const { error } = await run(client, [message(Role.User, 'go')], { tools: dirty });
  assert.equal(error, undefined);
  const sent = client.calls[0].tools;
  const wire = JSON.stringify(sent);
  assert.doesNotMatch(wire, new RegExp(ghp));
  assert.doesNotMatch(wire, new RegExp(dbPassword));
  assert.deepEqual(sent.map((t) => t.function.name), ['deploy_site', 'read_file']);
  const schema = sent[0].function.parameters;
  assert.deepEqual(schema.required, ['target']);
  assert.deepEqual(schema.properties.target.enum, ['staging', 'production']);
  assert.equal(schema.properties.retries.default, 3, 'numbers are left as they are');
  assert.equal(schema.properties.config.properties.db.examples[1], 'plain');
  assert.match(sent[0].function.description, /^Deploys with the key .* \(do not share\)$/);
  assert.deepEqual(sent[1], { type: 'function', function: { name: 'read_file', description: 'reads a file', parameters: { type: 'object', properties: { filePath: { type: 'string' } } } } }, 'an ordinary tool is exactly as it was');

  // the same tool list always gives the same text (the engine's cached prompt still matches)
  assert.equal(JSON.stringify(priv.redactedTools(client.calls[0].tools)), wire);
});

test('the agent\'s own tool list passes through the redaction untouched, so an ordinary list is never altered', () => {
  const own = ToolRegistry.getToolDefinitions();
  assert.deepEqual(priv.redactedTools(own), own);
});

test('a call to a tool whose description held a secret is still a call: names are not redacted', async () => {
  const dirty = [{ name: 'deploy_site', description: `key ${ghp}`, inputSchema: { type: 'object', properties: {} } }];
  const client = fakeClient((n, { onDelta }) => ({ content: '', toolCalls: [{ id: 'c1', name: 'deploy_site', arguments: '{}' }], finishReason: 'tool_calls' }));
  const { parts, error } = await run(client, [message(Role.User, 'go')], { tools: dirty });
  assert.equal(error, undefined);
  assert.deepEqual(parts.map((p) => [p.callId, p.name]), [['c1', 'deploy_site']]);
});

// ---- the first-use notice ------------------------------------------------------------------------------------------------------

function memory(initial = {}) {
  const kept = { ...initial };
  return { kept, get: (key) => kept[key], update: async (key, value) => { kept[key] = value; } };
}

test('the first time the swarm is used from the Chat view, one notice says what this path can and cannot keep private', async () => {
  stub.reset();
  const kept = memory();
  const provider = new prov.DlneChatModelProvider(fakeClient(), kept);
  assert.deepEqual(stub.state.infoMessages, [], 'nothing is said just because the entry exists');
  await run(null, [message(Role.User, 'hello')], {}, provider);
  assert.equal(stub.state.infoMessages.length, 1);
  const notice = stub.state.infoMessages[0];
  assert.equal(notice, prov.FIRST_USE_NOTICE);
  for (const phrase of [/VS Code's own tools read your files/, /hides secrets/, /excluded file/, /\.gitignore/, /trusted members/, /anyMember/]) assert.match(notice, phrase);
  assert.equal(kept.kept['dlne.pickerPrivacyNoticeShown'], true, 'and it is remembered');
  // the toast has the one button, the setting the sentence ends with; nothing is opened unless it is pressed
  assert.deepEqual(stub.state.commands, []);

  await run(null, [message(Role.User, 'again')], {}, provider);
  assert.equal(stub.state.infoMessages.length, 1, 'once');
  // a later session (a new provider, the same memory) does not say it again
  await run(null, [message(Role.User, 'again')], {}, new prov.DlneChatModelProvider(fakeClient(), kept));
  assert.equal(stub.state.infoMessages.length, 1);
});

test('the notice\'s button opens the privacy setting', async () => {
  stub.reset();
  stub.state.infoChoice = prov.OPEN_PRIVACY_SETTING;
  await run(null, [message(Role.User, 'hello')], {}, new prov.DlneChatModelProvider(fakeClient(), memory()));
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(stub.state.commands, [['workbench.action.openSettings', 'dlne.privacy']]);
  stub.reset();
});

test('without a memory the notice is once per session; one that cannot be shown or remembered never stops a request', async () => {
  stub.reset();
  const provider = new prov.DlneChatModelProvider(fakeClient());
  await run(null, [message(Role.User, 'a')], {}, provider);
  await run(null, [message(Role.User, 'b')], {}, provider);
  assert.equal(stub.state.infoMessages.length, 1);

  const real = stub.vscode.window.showInformationMessage;
  stub.vscode.window.showInformationMessage = () => { throw new Error('no window'); };
  try {
    const broken = { get: () => { throw new Error('no storage'); }, update: async () => { throw new Error('no storage'); } };
    const { error, text } = await run(null, [message(Role.User, 'c')], {}, new prov.DlneChatModelProvider(fakeClient(), broken));
    assert.equal(error, undefined);
    assert.equal(text, 'ok');
    stub.vscode.window.showInformationMessage = () => Promise.reject(new Error('rejected'));
    const rejected = await run(null, [message(Role.User, 'd')], {}, new prov.DlneChatModelProvider(fakeClient(), memory()));
    assert.equal(rejected.error, undefined);
    await new Promise((resolve) => setImmediate(resolve)); // an unhandled rejection would fail the run here
  } finally {
    stub.vscode.window.showInformationMessage = real;
  }
});

test('the notice comes before the request, and a request cancelled before it starts says nothing', async () => {
  stub.reset();
  const seen = [];
  const client = fakeClient((n, { onDelta }) => { seen.push(stub.state.infoMessages.length); onDelta('ok'); return { content: 'ok', toolCalls: [] }; });
  await run(client, [message(Role.User, 'hello')], {}, new prov.DlneChatModelProvider(client, memory()));
  assert.deepEqual(seen, [1], 'the toast is up before the swarm is asked');

  stub.reset();
  const token = makeToken();
  token.cancel();
  const provider = new prov.DlneChatModelProvider(fakeClient(), memory());
  await provider.provideLanguageModelChatResponse({ id: 'x' }, [message(Role.User, 'hello')], { tools: [], toolMode: 1 }, { report() {} }, token);
  assert.deepEqual(stub.state.infoMessages, []);
});

test('registered with the extension\'s global state, the provider remembers the notice there', async () => {
  stub.reset();
  const api = makeLm(['dlne']);
  const kept = memory();
  prov.registerDlneChatModel({ subscriptions: [], globalState: kept }, fakeClient(), api);
  await run(null, [message(Role.User, 'hello')], {}, api.registrations[0].provider);
  assert.equal(stub.state.infoMessages.length, 1);
  assert.equal(kept.kept['dlne.pickerPrivacyNoticeShown'], true);
});
