'use strict';
/**
 * The request pipeline of docs/coding-members.md section 10, against a mock coordinator: sessions, the
 * privacy header, redaction, the route lookup, sealing (end-to-end), the member's sealed answer, and the
 * fallback to today's plain path for a coordinator that has no coding members (or is too old to know of them).
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const stub = require('./helpers/vscode-stub');
const swarm = require('./helpers/mock-swarm');
const { DlneClient } = require('../dist/client/dlneClient');
const { AgentLoop } = require('../dist/agent/agentLoop');
const { forgetPrivacyModes } = require('../dist/privacy');

process.env.HOME = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-pipeline-home-'));
// Whether the extension's own ResponseOpener already reads section 6's version 1 additions (rs, sealed end).
// If not yet, the client below runs on the contract's reader from the mock, so its own logic is still tested.
const REAL_OPENER_IS_CURRENT = swarm.realOpenerIsCurrent();
const restoreOpener = swarm.useContractOpenerIfNeeded();
test.after(restoreOpener);

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const AWS_KEY = 'AKIAIOSFODNN7EXAMPLE';
const PEM = '-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA0123456789abcdef\n-----END RSA PRIVATE KEY-----';

/** A coordinator that has coding members (or not) and a member behind its route. */
async function world({ members = true, route = 'member', chat, completions, status } = {}) {
  stub.reset();
  forgetPrivacyModes();
  const member = swarm.createMember();
  let routeCount = 0;
  const c = await swarm.startCoordinator({
    status: status === undefined ? swarm.statusBody(members ? swarm.MEMBERS_ON : {}) : status,
    route: route === 'member'
      ? () => swarm.offer(member, `route-${++routeCount}`)
      : route === 'none'
      ? () => ({ status: 200, body: { route: null } })
      : route === 'missing'
      ? undefined
      : route,
    chat,
    completions,
  });
  stub.state.settings['dlne.serverUrl'] = c.url;
  return { c, member, client: new DlneClient(), close: () => c.close() };
}

async function ask(client, text = 'hello', options = {}, tools = []) {
  let out = '';
  const progress = [];
  const queue = [];
  const result = await client.streamChat(
    [{ role: 'user', content: text }],
    tools,
    (delta) => { out += delta; },
    undefined,
    undefined,
    (line) => progress.push(line),
    { onQueue: (q) => queue.push(q), ...options }
  );
  return { out, progress, queue, result };
}

const sealed = (c) => c.of('/v1/chat/completions').filter((r) => r.body && r.body.dlne_e2e);
const epks = (c) => c.of('/v1/chat/completions').filter((r) => r.body && r.body.dlne_e2e).map((r) => r.body.dlne_e2e.epk);

// ---- today's plain path: no members, or a coordinator that cannot know of them ----------------------------

test('a coordinator without coding members gets the plain request it always did, and is never asked for a route', async () => {
  const w = await world({ members: false, chat: (ctx) => swarm.plainStream(ctx.res, ['hel', 'lo']) });
  try {
    const { out } = await ask(w.client, 'write a function');
    assert.equal(out, 'hello');
    assert.equal(w.c.of('/v1/coding/route').length, 0);
    const [chat] = w.c.of('/v1/chat/completions');
    assert.equal(chat.body.messages[0].content, 'write a function');
    assert.equal(chat.body.dlne_e2e, undefined);
    assert.equal(chat.headers['x-dlne-route'], undefined);
    // nothing about the workspace's repository is worked out or sent while there are no members
    assert.equal(chat.headers['x-dlne-privacy'], undefined);
    assert.match(chat.headers['x-dlne-session'], UUID);
  } finally {
    w.close();
  }
});

test('an old coordinator (no coding_members in its status, no route endpoint) works exactly as before', async () => {
  const w = await world({ members: false, route: 'missing', chat: (ctx) => swarm.plainStream(ctx.res, ['ok']) });
  try {
    assert.equal((await ask(w.client)).out, 'ok');
    assert.equal((await ask(w.client)).out, 'ok');
    assert.equal(w.c.of('/v1/coding/route').length, 0, 'an old server is not asked what it cannot answer');
  } finally {
    w.close();
  }
});

test('a coordinator whose status cannot be read is not asked for a route either', async () => {
  const w = await world({ status: null, route: 'missing', chat: (ctx) => swarm.plainStream(ctx.res, ['ok']) });
  try {
    assert.equal((await ask(w.client)).out, 'ok');
    assert.equal(w.c.of('/v1/coding/route').length, 0);
  } finally {
    w.close();
  }
});

test('members on but the route endpoint missing (404): plain, and asked once, not before every request', async () => {
  const w = await world({ route: 'missing', chat: (ctx) => swarm.plainStream(ctx.res, ['ok']) });
  try {
    assert.equal((await ask(w.client)).out, 'ok');
    assert.equal((await ask(w.client)).out, 'ok');
    assert.equal(w.c.of('/v1/coding/route').length, 1);
    assert.equal(w.c.of('/v1/chat/completions').length, 2);
    assert.equal(w.c.of('/v1/chat/completions')[0].body.dlne_e2e, undefined);
  } finally {
    w.close();
  }
});

test('{route: null} sends the plain request, with the privacy mode and session headers', async () => {
  const w = await world({ route: 'none', chat: (ctx) => swarm.plainStream(ctx.res, ['local']) });
  try {
    assert.equal((await ask(w.client, 'q', { sessionId: 'session-from-test' })).out, 'local');
    const [chat] = w.c.of('/v1/chat/completions');
    assert.equal(chat.body.messages[0].content, 'q');
    assert.equal(chat.headers['x-dlne-route'], undefined);
    assert.equal(chat.headers['x-dlne-privacy'], 'trusted');
    assert.equal(chat.headers['x-dlne-session'], 'session-from-test');
    const [route] = w.c.of('/v1/coding/route');
    assert.deepEqual(route.query, { session: 'session-from-test', privacy: 'trusted' });
  } finally {
    w.close();
  }
});

test('a route lookup that fails (500) leaves the plain path, not an error', async () => {
  const w = await world({ route: () => ({ status: 500, body: { detail: 'boom' } }), chat: (ctx) => swarm.plainStream(ctx.res, ['ok']) });
  try {
    assert.equal((await ask(w.client)).out, 'ok');
  } finally {
    w.close();
  }
});

test('an offer the client cannot use (bad key, other algorithm) is the plain path', async () => {
  const bad = [
    { route: 'r', pubkey: 'not-a-key', alg: 'x25519-hkdf-sha256-chacha20poly1305' },
    { route: 'r', pubkey: Buffer.alloc(32, 1).toString('base64'), alg: 'something-else' },
    { route: 'bad\r\nheader', pubkey: Buffer.alloc(32, 1).toString('base64') },
  ];
  for (const body of bad) {
    const w = await world({ route: () => ({ status: 200, body }), chat: (ctx) => swarm.plainStream(ctx.res, ['plain']) });
    try {
      assert.equal((await ask(w.client)).out, 'plain');
      assert.equal(w.c.of('/v1/chat/completions')[0].headers['x-dlne-route'], undefined);
    } finally {
      w.close();
    }
  }
});

// ---- the member path ----------------------------------------------------------------------------------------

test('a member is sealed to: the wire carries ciphertext only, and the sealed answer is read', async () => {
  let opened;
  const w = await world({
    chat: (ctx) => { opened = swarm.memberAnswers(ctx, w.member, ['Hel', 'lo ', 'there']); },
  });
  try {
    const marker = 'PROMPT-MARKER-48151623';
    const { out } = await ask(w.client, `please refactor ${marker}`);
    assert.equal(out, 'Hello there');
    const [req] = sealed(w.c);
    assert.deepEqual(Object.keys(req.body), ['dlne_e2e']);
    assert.equal(req.body.dlne_e2e.v, 1);
    assert.ok(!req.raw.includes(marker), 'the prompt is not on the wire');
    assert.equal(req.headers['x-dlne-route'], 'route-1');
    assert.equal(req.headers['x-dlne-privacy'], 'trusted');
    assert.match(req.headers['x-dlne-session'], UUID);
    // the member, which holds the key, sees the request
    assert.ok(JSON.stringify(opened).includes(marker));
    assert.equal(opened.stream, true);
  } finally {
    w.close();
  }
});

test('tool calls in a sealed answer are assembled like plain ones', async () => {
  const w = await world({
    chat: (ctx) => swarm.memberAnswers(ctx, w.member, [
      { tool_calls: [{ index: 0, id: 'call_1', function: { name: 'read_file', arguments: '{"path":' } }] },
      { tool_calls: [{ index: 0, function: { arguments: '"a.ts"}' } }] },
    ]),
  });
  try {
    const started = [];
    const result = await w.client.streamChat([{ role: 'user', content: 'x' }], [], () => {}, (name) => started.push(name));
    assert.deepEqual(started, ['read_file']);
    assert.deepEqual(result.toolCalls, [{ id: 'call_1', name: 'read_file', arguments: '{"path":"a.ts"}' }]);
  } finally {
    w.close();
  }
});

test('the privacy mode goes in the header and in the route lookup', async () => {
  const cases = [['anyMember', 'any'], ['trustedOnly', 'trusted'], ['auto', 'trusted']];
  for (const [setting, mode] of cases) {
    const w = await world({ chat: (ctx) => swarm.memberAnswers(ctx, w.member, ['ok']) });
    try {
      stub.state.settings['dlne.privacy'] = setting;
      await ask(w.client);
      assert.equal(sealed(w.c)[0].headers['x-dlne-privacy'], mode, setting);
      assert.equal(w.c.of('/v1/coding/route')[0].query.privacy, mode, setting);
    } finally {
      w.close();
    }
  }
});

test('the privacy policy is only consulted while the coordinator has coding members', async () => {
  const policy = require('../dist/privacy/policy');
  const real = policy.getPrivacyMode;
  let asked = 0;
  policy.getPrivacyMode = async () => { asked++; return 'open'; };
  try {
    const off = await world({ members: false, chat: (ctx) => swarm.plainStream(ctx.res, ['ok']) });
    try {
      await ask(off.client);
      assert.equal(asked, 0, 'no public-repository lookup with members off');
    } finally {
      off.close();
    }
    const on = await world({ route: 'none', chat: (ctx) => swarm.plainStream(ctx.res, ['ok']) });
    try {
      await ask(on.client);
      assert.equal(asked, 1);
      assert.equal(on.c.of('/v1/chat/completions')[0].headers['x-dlne-privacy'], 'open');
    } finally {
      on.close();
    }
  } finally {
    policy.getPrivacyMode = real;
    forgetPrivacyModes();
  }
});

test('a privacy lookup that throws or hangs reads as trusted and does not hold the request up', async () => {
  const policy = require('../dist/privacy/policy');
  const real = policy.getPrivacyMode;
  try {
    for (const lookup of [async () => { throw new Error('boom'); }, () => new Promise(() => {}), async () => 'everyone']) {
      policy.getPrivacyMode = lookup;
      const w = await world({ route: 'none', chat: (ctx) => swarm.plainStream(ctx.res, ['ok']) });
      try {
        const started = Date.now();
        await ask(w.client);
        assert.ok(Date.now() - started < 4000);
        assert.equal(w.c.of('/v1/chat/completions')[0].headers['x-dlne-privacy'], 'trusted');
      } finally {
        w.close();
      }
    }
  } finally {
    policy.getPrivacyMode = real;
    forgetPrivacyModes();
  }
});

// ---- when a member fails -------------------------------------------------------------------------------------

test('a member that fails (502): the route is asked for again, the request is sealed again, and an envelope is never resent', async () => {
  let n = 0;
  const w = await world({
    chat: (ctx) => {
      n++;
      if (n === 1) return swarm.json(ctx.res, 502, { detail: 'the member went away' });
      return swarm.memberAnswers(ctx, w.member, ['served']);
    },
  });
  try {
    const { out, progress } = await ask(w.client);
    assert.equal(out, 'served');
    assert.equal(w.c.of('/v1/coding/route').length, 2, 'asked again');
    assert.deepEqual(w.c.of('/v1/chat/completions').map((r) => r.headers['x-dlne-route']), ['route-1', 'route-2']);
    const keys = epks(w.c);
    assert.equal(keys.length, 2);
    assert.equal(new Set(keys).size, 2, 'a new envelope, not the old one again');
    assert.ok(progress.some((line) => /another one/.test(line)));
  } finally {
    w.close();
  }
});

test('the router\'s 503 dlne_reroute is a member that cannot take it (ask for another route), not "the model is loading"', async () => {
  let n = 0;
  const w = await world({
    chat: (ctx) => {
      n++;
      if (n === 1) return swarm.json(ctx.res, 503, { error: { code: 'dlne_reroute', reason: 'busy', message: 'that member is full' } });
      return swarm.memberAnswers(ctx, w.member, ['served elsewhere']);
    },
  });
  try {
    const { out } = await ask(w.client);
    assert.equal(out, 'served elsewhere');
    assert.equal(w.c.of('/v1/coding/route').length, 2, 'a new route was asked for');
    const keys = epks(w.c);
    assert.equal(new Set(keys).size, 2, 'sealed again, never the old envelope');
  } finally {
    w.close();
  }
});

test('autocomplete: the router\'s 503 dlne_reroute is replaced with a new seal too', async () => {
  let n = 0;
  const w = await world({
    completions: (ctx) => (++n === 1
      ? swarm.json(ctx.res, 503, { error: { code: 'dlne_reroute', reason: 'busy' } })
      : swarm.memberAnswersWhole(ctx, w.member, { choices: [{ text: 'again' }] })),
  });
  try {
    await w.client.getClusterStatusDetailed();
    assert.equal(await w.client.getCompletion('p'), 'again');
    const reqs = w.c.of('/v1/completions');
    assert.equal(reqs.length, 2);
    assert.notEqual(reqs[0].body.dlne_e2e.epk, reqs[1].body.dlne_e2e.epk);
  } finally {
    w.close();
  }
});

test('two members in a row fail: the coordinator\'s own engine answers, in plain', async () => {
  const w = await world({
    chat: (ctx) => (ctx.headers['x-dlne-route'] ? swarm.json(ctx.res, 502, { detail: 'gone' }) : swarm.plainStream(ctx.res, ['local engine'])),
  });
  try {
    const { out } = await ask(w.client, 'plain me');
    assert.equal(out, 'local engine');
    const chats = w.c.of('/v1/chat/completions');
    assert.equal(chats.length, 3);
    assert.equal(chats[2].headers['x-dlne-route'], undefined);
    assert.equal(chats[2].body.messages[0].content, 'plain me');
    assert.equal(new Set(epks(w.c)).size, 2);
  } finally {
    w.close();
  }
});

// The coordinator answers a sealed request it cannot hand to the member with 503 and a body naming `dlne_reroute`
// (engine/coding_router.py): the member is gone, full, failed, not allowed for this privacy mode, or the route expired.
// A 503 is also what "the model is loading" looks like, so the code in the body is what tells them apart.
const reroute = (reason) => ({ error: { code: 'dlne_reroute', message: 'Ask for a new route and send the request again.', reason } });

test('a 503 dlne_reroute is a member failure: a new route, a new seal, and the user is told another member is tried', async () => {
  let n = 0;
  const w = await world({
    chat: (ctx) => {
      n++;
      if (n === 1) return swarm.json(ctx.res, 503, reroute('gone'));
      return swarm.memberAnswers(ctx, w.member, ['served']);
    },
  });
  try {
    const { out, progress } = await ask(w.client);
    assert.equal(out, 'served');
    assert.equal(w.c.of('/v1/coding/route').length, 2, 'asked again');
    assert.equal(new Set(epks(w.c)).size, 2, 'a new envelope, not the old one again');
    assert.ok(progress.some((line) => /another one/.test(line)), progress.join(' | '));
    assert.ok(!progress.some((line) => /Loading|loading/.test(line)), 'a failed member is not "the model is loading"');
  } finally {
    w.close();
  }
});

test('two members in a row answer dlne_reroute: the coordinator\'s own engine answers, in plain', async () => {
  const w = await world({
    chat: (ctx) => (ctx.headers['x-dlne-route'] ? swarm.json(ctx.res, 503, reroute('busy')) : swarm.plainStream(ctx.res, ['local engine'])),
  });
  try {
    const { out } = await ask(w.client, 'plain me');
    assert.equal(out, 'local engine');
    const chats = w.c.of('/v1/chat/completions');
    assert.equal(chats.length, 3);
    assert.equal(chats[2].headers['x-dlne-route'], undefined);
    assert.equal(new Set(epks(w.c)).size, 2);
  } finally {
    w.close();
  }
});

test('a 503 without dlne_reroute is still "the model is loading", sealed request or not', async () => {
  let n = 0;
  const w = await world({
    chat: (ctx) => (++n === 1 ? swarm.json(ctx.res, 503, { detail: 'Engine not ready yet' }) : swarm.memberAnswers(ctx, w.member, ['served'])),
  });
  try {
    const { out, progress } = await ask(w.client);
    assert.equal(out, 'served');
    assert.ok(!progress.some((line) => /another one/.test(line)), 'not a member failure');
  } finally {
    w.close();
  }
});

test('autocomplete: a 503 dlne_reroute replaces the member, with a new seal; any other 503 gives up quietly', async () => {
  let n = 0;
  const w = await world({
    completions: (ctx) => (++n === 1 ? swarm.json(ctx.res, 503, reroute('expired')) : swarm.memberAnswersWhole(ctx, w.member, { choices: [{ text: 'again' }] })),
  });
  try {
    await w.client.getClusterStatusDetailed();
    assert.equal(await w.client.getCompletion('p'), 'again');
    const reqs = w.c.of('/v1/completions');
    assert.equal(reqs.length, 2);
    assert.notEqual(reqs[0].body.dlne_e2e.epk, reqs[1].body.dlne_e2e.epk);
  } finally {
    w.close();
  }
  const v = await world({ completions: (ctx) => swarm.json(ctx.res, 503, { detail: 'loading' }) });
  try {
    await v.client.getClusterStatusDetailed();
    assert.equal(await v.client.getCompletion('p'), '');
    assert.equal(v.c.of('/v1/completions').length, 1, 'no retry for a 503 that is not about the member');
  } finally {
    v.close();
  }
});

test('a stream that stops before the sealed end is a cut answer: an error, not a finished answer', async () => {
  const w = await world({ chat: (ctx) => swarm.memberAnswers(ctx, w.member, ['half an ans'], { end: false }) });
  try {
    await assert.rejects(ask(w.client), /stopped before it was complete/);
    assert.equal(w.c.of('/v1/chat/completions').length, 1, 'part of it was shown, so it is not quietly redone');
  } finally {
    w.close();
  }
});

test('an answer cut off part-way is reported to the user and is not recorded as a turn or redone', async () => {
  for (const sealedPath of [false, true]) {
    const w = await world({
      members: sealedPath,
      chat: (ctx) => {
        if (sealedPath) {
          const { respKey } = w.member.open(ctx.body);
          const sealer = new swarm.MemberSealer(respKey);
          ctx.res.writeHead(200, { 'Content-Type': 'text/event-stream' });
          ctx.res.write(`data: ${JSON.stringify({ dlne_e2e: sealer.seal(swarm.chunk({ content: 'half an ans' })) })}\n\n`);
        } else {
          ctx.res.writeHead(200, { 'Content-Type': 'text/event-stream' });
          ctx.res.write(`data: ${JSON.stringify(swarm.chunk({ content: 'half an ans' }))}\n\n`);
        }
        setTimeout(() => ctx.res.socket.destroy(), 40); // the connection drops
      },
    });
    try {
      const loop = new AgentLoop(w.client);
      const shown = [];
      await loop.run('do it', { onThinkingDelta() {}, onContentDelta: (d) => shown.push(d), onToolStart() {}, onToolEnd() {} });
      assert.equal(w.c.of('/v1/chat/completions').length, 1, 'not retried: part of it was already shown');
      assert.match(shown.join(''), /half an ans/);
      assert.match(shown.join(''), /not treating it as finished/);
      assert.equal(loop.messages.length, 2, 'system prompt and the user\'s message; the half answer is not a turn');
    } finally {
      w.close();
    }
  }
});

test('a member that vanishes before saying anything is replaced, once', async () => {
  let n = 0;
  const w = await world({
    chat: (ctx) => (++n === 1 ? swarm.memberCuts(ctx, w.member) : swarm.memberAnswers(ctx, w.member, ['second try'])),
  });
  try {
    assert.equal((await ask(w.client)).out, 'second try');
    assert.equal(new Set(epks(w.c)).size, 2);
  } finally {
    w.close();
  }
});

test('a chunk that fails to open ends the stream and nothing after it is shown', async () => {
  const w = await world({ chat: (ctx) => swarm.memberAnswers(ctx, w.member, ['one', 'two', 'three'], { tamper: 1 }) });
  try {
    const seen = [];
    await assert.rejects(
      w.client.streamChat([{ role: 'user', content: 'x' }], [], (d) => seen.push(d)),
      /failed its integrity check/
    );
    assert.deepEqual(seen, ['one']);
  } finally {
    w.close();
  }
});

test('a member that answers in the clear is refused and replaced', async () => {
  let n = 0;
  const w = await world({
    chat: (ctx) => (++n === 1 ? swarm.memberAnswers(ctx, w.member, ['SHOULD NOT BE SHOWN'], { plain: true }) : swarm.memberAnswers(ctx, w.member, ['sealed'])),
  });
  try {
    const { out } = await ask(w.client);
    assert.equal(out, 'sealed');
  } finally {
    w.close();
  }
});

// ---- sessions --------------------------------------------------------------------------------------------------

test('one session per agent conversation, a new one when it is cleared; autocomplete has its own', async () => {
  const w = await world({
    members: false,
    chat: (ctx) => swarm.plainStream(ctx.res, ['ok']),
    completions: (ctx) => swarm.json(ctx.res, 200, { choices: [{ text: 'x' }] }),
  });
  try {
    const loop = new AgentLoop(w.client);
    const callbacks = { onThinkingDelta() {}, onContentDelta() {}, onToolStart() {}, onToolEnd() {} };
    await loop.run('first', callbacks);
    await loop.run('second', callbacks);
    loop.clearHistory();
    await loop.run('third', callbacks);
    const sessions = w.c.of('/v1/chat/completions').map((r) => r.headers['x-dlne-session']);
    assert.equal(sessions.length, 3);
    assert.match(sessions[0], UUID);
    assert.equal(sessions[0], sessions[1]);
    assert.notEqual(sessions[1], sessions[2]);

    await w.client.getCompletion('a');
    await w.client.getCompletion('b');
    const editor = w.c.of('/v1/completions').map((r) => r.headers['x-dlne-session']);
    assert.match(editor[0], UUID);
    assert.equal(editor[0], editor[1]);
    assert.ok(!sessions.includes(editor[0]), 'the editor session is not a conversation');
  } finally {
    w.close();
  }
});

test('the session id is random and carries nothing about the user', () => {
  const { newSessionId, editorSessionId } = require('../dist/client/session');
  const a = newSessionId();
  assert.match(a, UUID);
  assert.notEqual(a, newSessionId());
  assert.equal(editorSessionId(), editorSessionId());
});

// ---- secrets -----------------------------------------------------------------------------------------------------

test('secrets are hidden from everything sent, the agent keeps its own history, and a sealed body is redacted too', async () => {
  let opened;
  const w = await world({ chat: (ctx) => { opened = swarm.memberAnswers(ctx, w.member, ['ok']); } });
  try {
    const messages = [
      { role: 'user', content: `deploy with ${AWS_KEY}` },
      { role: 'assistant', content: '', tool_calls: [{ id: 'c', type: 'function', function: { name: 'write_file', arguments: JSON.stringify({ path: 'k.pem', content: PEM }) } }] },
      { role: 'tool', tool_call_id: 'c', name: 'write_file', content: `wrote it; also ${AWS_KEY}` },
    ];
    await w.client.streamChat(messages, [], () => {});
    const wire = JSON.stringify(opened);
    assert.ok(!wire.includes(AWS_KEY), 'the key');
    assert.ok(!wire.includes('MIIEowIBAAKCAQEA0123456789abcdef'), 'the private key');
    assert.ok(wire.includes('[REDACTED]'));
    assert.ok(messages[0].content.includes(AWS_KEY), 'the caller\'s own messages are not rewritten');
  } finally {
    w.close();
  }
});

test('the plain path is redacted as well', async () => {
  const w = await world({ members: false, chat: (ctx) => swarm.plainStream(ctx.res, ['ok']) });
  try {
    await w.client.streamChat([{ role: 'user', content: `key ${AWS_KEY}` }], [], () => {});
    const [chat] = w.c.of('/v1/chat/completions');
    assert.ok(!chat.raw.includes(AWS_KEY));
    assert.match(chat.body.messages[0].content, /\[REDACTED\]/);
  } finally {
    w.close();
  }
});

test('an autocomplete prompt is redacted before it is sent', async () => {
  const w = await world({ members: false, completions: (ctx) => swarm.json(ctx.res, 200, { choices: [{ text: 'z' }] }) });
  try {
    await w.client.getCompletion(`<|fim_prefix|>const k = "${AWS_KEY}";\n<|fim_suffix|><|fim_middle|>`);
    const [req] = w.c.of('/v1/completions');
    assert.ok(!req.raw.includes(AWS_KEY));
    assert.match(req.body.prompt, /<\|fim_prefix\|>/);
  } finally {
    w.close();
  }
});

test('redaction that cannot run withholds the text instead of sending it', () => {
  const redact = require('../dist/privacy/redact');
  const { redactPayload, WITHHELD } = require('../dist/privacy');
  const real = redact.redactSecrets;
  redact.redactSecrets = () => { throw new Error('boom'); };
  try {
    const { payload, count } = redactPayload({ messages: [{ role: 'user', content: 'private' }], prompt: 'also private' });
    assert.equal(payload.messages[0].content, WITHHELD);
    assert.equal(payload.prompt, WITHHELD);
    assert.equal(count, 2);
  } finally {
    redact.redactSecrets = real;
  }
});

test('the same text redacts to the same text, so the engine\'s cache of the conversation prefix still matches', () => {
  const { redactPayload } = require('../dist/privacy');
  const body = { messages: [{ role: 'system', content: 'rules' }, { role: 'user', content: `a ${AWS_KEY} b` }] };
  assert.deepEqual(redactPayload(body).payload, redactPayload(body).payload);
});

// ---- excluded files -----------------------------------------------------------------------------------------------

test('an excluded file is never read for the model, and gets no autocomplete', async () => {
  const { ToolRegistry } = require('../dist/agent/tools');
  const { DlneFimProvider } = require('../dist/completion/fimProvider');
  stub.reset();
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-excluded-'));
  fs.writeFileSync(path.join(root, '.env'), `TOKEN=${AWS_KEY}\n`);
  fs.writeFileSync(path.join(root, 'app.ts'), 'export {}\n');
  stub.vscode.workspace.workspaceFolders = [{ uri: { fsPath: root } }];
  const realApprover = ToolRegistry.approver;
  let asked = 0;
  ToolRegistry.approver = async () => { asked++; return 'once'; };
  try {
    const refused = await ToolRegistry.executeTool('read_file', { path: '.env' });
    assert.equal(refused.is_error, true);
    assert.match(refused.output, /excluded for privacy/);
    assert.ok(!refused.output.includes(AWS_KEY));
    assert.equal(asked, 0, 'refused before the user is even asked');
    assert.equal((await ToolRegistry.executeTool('read_file', { path: 'app.ts' })).is_error, undefined);

    let completions = 0;
    const provider = new DlneFimProvider({ getCompletion: async () => { completions++; return 'x'; } });
    const doc = (fsPath) => ({
      uri: { scheme: 'file', fsPath },
      lineCount: 1,
      lineAt: () => ({ text: 'const a = 1' }),
      getText: () => 'const a = 1',
    });
    const token = { isCancellationRequested: false, onCancellationRequested: () => ({ dispose() {} }) };
    stub.state.settings['dlne.inlineCompletionDelay'] = 75;
    const pos = new stub.vscode.Position(0, 5);
    assert.equal(await provider.provideInlineCompletionItems(doc(path.join(root, '.env')), pos, {}, token), null);
    assert.equal(completions, 0);
    assert.ok(await provider.provideInlineCompletionItems(doc(path.join(root, 'app.ts')), pos, {}, token));
    assert.equal(completions, 1);
  } finally {
    ToolRegistry.approver = realApprover;
    stub.vscode.workspace.workspaceFolders = undefined;
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('an exclusion check that fails counts as excluded', () => {
  // redact.ts re-exports isExcluded from exclude.ts (a read-only getter), so the failure is planted in exclude.
  const exclude = require('../dist/privacy/exclude');
  const { isExcludedFile } = require('../dist/privacy');
  const real = exclude.isExcluded;
  exclude.isExcluded = () => { throw new Error('boom'); };
  try {
    assert.equal(isExcludedFile({ fsPath: '/x/a.ts' }), true);
    assert.equal(isExcludedFile({ scheme: 'untitled' }), false, 'a document with no path on disk has nothing to exclude');
  } finally {
    exclude.isExcluded = real;
  }
});

// ---- autocomplete ---------------------------------------------------------------------------------------------------

test('autocomplete goes to a member sealed, reads the sealed answer, and reuses the route for a short while', async () => {
  const w = await world({
    completions: (ctx) => swarm.memberAnswersWhole(ctx, w.member, { choices: [{ text: 'return 1;' }] }),
  });
  try {
    await w.client.getClusterStatusDetailed(); // what the status bar's poll does
    const marker = 'FIM-MARKER-777';
    assert.equal(await w.client.getCompletion(`<|fim_prefix|>// ${marker}\n<|fim_suffix|><|fim_middle|>`), 'return 1;');
    assert.equal(await w.client.getCompletion('<|fim_prefix|>b<|fim_suffix|><|fim_middle|>'), 'return 1;');
    const reqs = w.c.of('/v1/completions');
    assert.equal(reqs.length, 2);
    assert.ok(reqs.every((r) => r.body.dlne_e2e && r.headers['x-dlne-route'] === 'route-1'));
    assert.ok(!reqs[0].raw.includes(marker));
    assert.notEqual(reqs[0].body.dlne_e2e.epk, reqs[1].body.dlne_e2e.epk);
    assert.equal(w.c.of('/v1/coding/route').length, 1, 'the route is reused, so a keystroke does not cost a lookup');
    assert.equal(reqs[0].headers['x-dlne-privacy'], 'trusted');
  } finally {
    w.close();
  }
});

test('autocomplete does not ask a route question before the status has said there are members', async () => {
  const w = await world({ completions: (ctx) => swarm.json(ctx.res, 200, { choices: [{ text: 'plain' }] }) });
  try {
    assert.equal(await w.client.getCompletion('p'), 'plain'); // no status poll yet
    assert.equal(w.c.of('/v1/coding/route').length, 0);
    assert.equal(w.c.of('/v1/completions')[0].body.prompt, 'p');
  } finally {
    w.close();
  }
});

test('autocomplete: a member that fails is replaced, with a new seal', async () => {
  let n = 0;
  const w = await world({
    completions: (ctx) => (++n === 1 ? swarm.json(ctx.res, 502, { detail: 'gone' }) : swarm.memberAnswersWhole(ctx, w.member, { choices: [{ text: 'again' }] })),
  });
  try {
    await w.client.getClusterStatusDetailed();
    assert.equal(await w.client.getCompletion('p'), 'again');
    const reqs = w.c.of('/v1/completions');
    assert.equal(reqs.length, 2);
    assert.notEqual(reqs[0].body.dlne_e2e.epk, reqs[1].body.dlne_e2e.epk);
  } finally {
    w.close();
  }
});

test('autocomplete: a retry on a fresh socket is sealed afresh, never the old envelope again', async () => {
  let n = 0;
  const w = await world({
    completions: (ctx) => {
      n++;
      if (n === 2) return ctx.req.socket.destroy(); // the pooled socket the server dropped
      return swarm.memberAnswersWhole(ctx, w.member, { choices: [{ text: `answer ${n}` }] });
    },
  });
  try {
    await w.client.getClusterStatusDetailed();
    assert.equal(await w.client.getCompletion('one'), 'answer 1');
    assert.equal(await w.client.getCompletion('two'), 'answer 3');
    const keys = w.c.of('/v1/completions').map((r) => r.body.dlne_e2e.epk);
    assert.equal(keys.length, 3);
    assert.equal(new Set(keys).size, 3);
  } finally {
    w.close();
  }
});

test('autocomplete answered in the clear by a member is dropped, and the coordinator\'s own engine is used instead', async () => {
  const w = await world({
    completions: (ctx) => {
      if (!ctx.headers['x-dlne-route']) return swarm.json(ctx.res, 200, { choices: [{ text: 'local' }] });
      w.member.open(ctx.body);
      swarm.json(ctx.res, 200, { choices: [{ text: 'MEMBER ANSWERED IN THE CLEAR' }] });
    },
  });
  try {
    await w.client.getClusterStatusDetailed();
    assert.equal(await w.client.getCompletion('p'), 'local');
    assert.equal(w.c.of('/v1/completions').length, 3, 'two members tried, then the local engine');
  } finally {
    w.close();
  }
});

// ---- the extension's own reader, against a member written from the contract -------------------------------------------

test('the extension\'s ResponseOpener reads what a member written from section 6 sends',
  { skip: REAL_OPENER_IS_CURRENT ? false : 'waiting for C1: ResponseOpener.open(sealed), .finished and the per-response salt' },
  () => {
    const e2e = require('../dist/privacy/e2e');
    const member = swarm.createMember();
    const { envelope, respKey } = e2e.sealRequest({ prompt: 'x' }, member.pubB64);
    const opened = member.open(envelope);
    assert.deepEqual(opened.respKey, respKey);

    const send = (parts) => {
      const sealer = new swarm.MemberSealer(opened.respKey);
      return [...parts.map((p) => sealer.seal(p)), sealer.finish()];
    };
    const frames = send([swarm.chunk({ content: 'a' }), swarm.chunk({ content: 'b' })]);
    const reader = new e2e.ResponseOpener(respKey);
    assert.deepEqual(JSON.parse(reader.open(frames[0]).toString()), swarm.chunk({ content: 'a' }));
    assert.equal(reader.finished, false);
    assert.deepEqual(JSON.parse(reader.open(frames[1]).toString()), swarm.chunk({ content: 'b' }));
    assert.equal(reader.open(frames[2]), null, 'the sealed end');
    assert.equal(reader.finished, true);

    // out of order, repeated, or altered: the stream is over
    const again = send([swarm.chunk({ content: 'a' })]);
    const strict = new e2e.ResponseOpener(respKey);
    assert.throws(() => strict.open(again[1]));
    assert.throws(() => strict.open(again[0]), 'a reader that failed refuses everything after');
    const flipped = send([swarm.chunk({ content: 'a' })]);
    const ct = Buffer.from(flipped[0].ct, 'base64');
    ct[0] ^= 1;
    assert.throws(() => new e2e.ResponseOpener(respKey).open({ ...flipped[0], ct: ct.toString('base64') }));
  });

// ---- the adapter ------------------------------------------------------------------------------------------------------

test('sealing that cannot be done falls back to the plain path instead of failing the request', async () => {
  const w = await world({
    route: () => ({ status: 200, body: { route: 'r', pubkey: Buffer.alloc(32, 0).toString('base64') } }), // a key that fails key agreement
    chat: (ctx) => swarm.plainStream(ctx.res, ['plain']),
  });
  try {
    assert.equal((await ask(w.client)).out, 'plain');
    assert.equal(w.c.of('/v1/chat/completions')[0].headers['x-dlne-route'], undefined);
  } finally {
    w.close();
  }
});

test('the privacy status item exists only while the coordinator has coding members', () => {
  const { PrivacyStatusGate } = require('../dist/privacy');
  const made = [];
  const gate = new PrivacyStatusGate(() => { const item = { disposed: false, dispose() { this.disposed = true; } }; made.push(item); return item; });
  gate.update(false);
  assert.equal(made.length, 0);
  gate.update(true);
  gate.update(true);
  assert.equal(made.length, 1);
  assert.equal(gate.shown, true);
  gate.update(false);
  assert.equal(made[0].disposed, true);
  assert.equal(gate.shown, false);
  gate.update(true);
  assert.equal(made.length, 2);
  gate.dispose();
  assert.equal(made[1].disposed, true);
});

test('the client imports the privacy modules only through the adapter', () => {
  const srcDir = path.join(__dirname, '..', 'src');
  const offenders = [];
  const walk = (dir) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) { walk(full); continue; }
      if (!full.endsWith('.ts') || full.includes(`${path.sep}privacy${path.sep}`)) continue;
      const text = fs.readFileSync(full, 'utf8');
      if (/from '(\.\.?\/)+privacy\/(e2e|redact|policy)'/.test(text)) offenders.push(path.relative(srcDir, full));
    }
  };
  walk(srcDir);
  assert.deepEqual(offenders, []);
});
