'use strict';
/**
 * Who the coordinator says the caller is (src/agent/keyTier.ts, GET /v1/me; docs/release-2.7-contract.md sections 3 and 7):
 * asked through the extension's own client with a chat request's headers, kept for a few minutes, asked again when the key
 * or the server address changes, null for a coordinator from before 2.7.0, and the switch for unlimited agent turns.
 * The coordinator is a small server on this machine.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');

const { stub } = require('./helpers/chat-harness');   // (also gives this process a HOME of its own)
const { DlneClient } = require('../dist/client/dlneClient');
const keyTier = require('../dist/agent/keyTier');

/** A coordinator that answers /v1/me as `behaviour` says: { status, body } or a function of the request. */
async function coordinator(behaviour) {
  const seen = [];
  const server = http.createServer((req, res) => {
    seen.push({ url: req.url, auth: req.headers.authorization });
    const reply = typeof behaviour.current === 'function' ? behaviour.current(req) : behaviour.current;
    res.writeHead(reply.status, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(reply.body ?? {}));
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  return { seen, url: `http://127.0.0.1:${server.address().port}`, close: () => new Promise((resolve) => { server.closeAllConnections?.(); server.close(resolve); }) };
}

/** The setting-change events a bound key tier listens to; a test fires them by hand. */
let configListeners = [];
stub.vscode.workspace.onDidChangeConfiguration = (fn) => {
  configListeners.push(fn);
  return { dispose() { configListeners = configListeners.filter((f) => f !== fn); } };
};
const settingChanged = (...keys) => configListeners.forEach((fn) => fn({ affectsConfiguration: (k) => keys.includes(k) }));

async function setup(behaviour, settings = {}) {
  stub.reset();
  keyTier.resetKeyTierForTests();
  const server = await coordinator(behaviour);
  stub.state.settings['dlne.serverUrl'] = server.url;
  stub.state.settings['dlne.apiKey'] = 'dlne_sk_test_key';
  Object.assign(stub.state.settings, settings);
  const subscription = keyTier.bindKeyTierClient(new DlneClient());
  return {
    server,
    async done() {
      subscription.dispose();
      keyTier.resetKeyTierForTests();
      await server.close();
    },
  };
}

const realNow = Date.now;
test.afterEach(() => { Date.now = realNow; });

test('before the client is handed over the tier is unknown and nothing is asked', async () => {
  stub.reset();
  keyTier.resetKeyTierForTests();
  assert.equal(await keyTier.getKeyTier(), null);
  assert.equal(await keyTier.unlimitedTurnsActive(), false);
});

test('it asks GET /v1/me with the same headers as a chat request, and answers the tier', async () => {
  const env = await setup({ current: { status: 200, body: { tier: 'key' } } });
  try {
    assert.equal(await keyTier.getKeyTier(), 'key');
    assert.deepEqual(env.server.seen, [{ url: '/v1/me', auth: 'Bearer dlne_sk_test_key' }]);
  } finally {
    await env.done();
  }
});

test('every tier the coordinator can name comes through, and a name it cannot is unknown', async () => {
  const behaviour = { current: null };
  const env = await setup(behaviour);
  try {
    for (const tier of ['key', 'host', 'gpu', 'cpu', 'trial', 'open']) {
      behaviour.current = { status: 200, body: { tier } };
      assert.equal(await keyTier.refreshKeyTier(), tier);
    }
    behaviour.current = { status: 200, body: { tier: 'emperor' } };
    assert.equal(await keyTier.refreshKeyTier(), null);
    behaviour.current = { status: 200, body: {} };
    assert.equal(await keyTier.refreshKeyTier(), null);
  } finally {
    await env.done();
  }
});

test('the answer is kept for a few minutes, then asked again', async () => {
  let clock = 1_000_000;
  Date.now = () => clock;
  const env = await setup({ current: { status: 200, body: { tier: 'key' } } });
  try {
    assert.equal(await keyTier.getKeyTier(), 'key');
    clock += keyTier.KEY_TIER_REFRESH_MS - 1000;
    assert.equal(await keyTier.getKeyTier(), 'key');
    assert.equal(env.server.seen.length, 1, 'still kept');
    clock += 2000;
    assert.equal(await keyTier.getKeyTier(), 'key');
    assert.equal(env.server.seen.length, 2, 'asked again');
  } finally {
    await env.done();
  }
});

test('calls made together share one question', async () => {
  const env = await setup({ current: { status: 200, body: { tier: 'key' } } });
  try {
    const answers = await Promise.all([keyTier.getKeyTier(), keyTier.getKeyTier(), keyTier.getKeyTier()]);
    assert.deepEqual(answers, ['key', 'key', 'key']);
    assert.equal(env.server.seen.length, 1);
  } finally {
    await env.done();
  }
});

test('a coordinator from before 2.7.0 (404) is null, asked once and not again until the time is up', async () => {
  const env = await setup({ current: { status: 404, body: { detail: 'Not Found' } } });
  try {
    assert.equal(await keyTier.getKeyTier(), null);
    assert.equal(await keyTier.getKeyTier(), null);
    assert.equal(env.server.seen.length, 1);
    assert.equal(await keyTier.unlimitedTurnsActive(), false);
  } finally {
    await env.done();
  }
});

test('when the coordinator cannot be reached the last answer for the same caller stands, and it is asked again soon', async () => {
  let clock = 5_000_000;
  Date.now = () => clock;
  const behaviour = { current: { status: 200, body: { tier: 'key' } } };
  const env = await setup(behaviour);
  try {
    assert.equal(await keyTier.getKeyTier(), 'key');
    clock += keyTier.KEY_TIER_REFRESH_MS + 1;
    behaviour.current = { status: 503, body: {} };
    assert.equal(await keyTier.getKeyTier(), 'key', 'a server error does not take the tier away');
    assert.equal(env.server.seen.length, 2);
    clock += 31_000;
    behaviour.current = { status: 200, body: { tier: 'trial' } };
    assert.equal(await keyTier.getKeyTier(), 'trial', 'asked again after the short wait');
  } finally {
    await env.done();
  }
});

test('a server nobody answers at is null, never a throw', async () => {
  stub.reset();
  keyTier.resetKeyTierForTests();
  stub.state.settings['dlne.serverUrl'] = 'http://127.0.0.1:9';
  const subscription = keyTier.bindKeyTierClient(new DlneClient());
  try {
    assert.equal(await keyTier.getKeyTier(), null);
  } finally {
    subscription.dispose();
    keyTier.resetKeyTierForTests();
  }
});

test('a changed API key or server address is asked again at once, and listeners hear the new tier', async () => {
  const behaviour = { current: (req) => ({ status: 200, body: { tier: req.headers.authorization === 'Bearer dlne_sk_test_key' ? 'key' : 'trial' } }) };
  const env = await setup(behaviour);
  const heard = [];
  const listener = keyTier.onKeyTierChanged((tier) => heard.push(tier));
  try {
    assert.equal(await keyTier.getKeyTier(), 'key');
    stub.state.settings['dlne.apiKey'] = 'dlne_sk_someone_elses';
    settingChanged('dlne.apiKey');
    await new Promise((resolve) => setTimeout(resolve, 50));
    assert.equal(env.server.seen.length, 2);
    assert.equal(await keyTier.getKeyTier(), 'trial', 'the old key\'s answer is not used for the new key');
    assert.equal(env.server.seen.length, 2, 'and the new answer is kept');
    assert.deepEqual(heard, ['key', 'trial']);
    // an unrelated setting changes nothing
    settingChanged('dlne.agent.maxTurns');
    await new Promise((resolve) => setTimeout(resolve, 30));
    assert.equal(env.server.seen.length, 2);
  } finally {
    listener.dispose();
    await env.done();
  }
});

test('listeners hear a change only, and stop hearing once disposed', async () => {
  const behaviour = { current: { status: 200, body: { tier: 'key' } } };
  const env = await setup(behaviour);
  const heard = [];
  const listener = keyTier.onKeyTierChanged((tier) => heard.push(tier));
  try {
    await keyTier.refreshKeyTier();
    await keyTier.refreshKeyTier();
    assert.deepEqual(heard, ['key']);
    behaviour.current = { status: 404, body: {} };
    await keyTier.refreshKeyTier();
    assert.deepEqual(heard, ['key', null]);
    listener.dispose();
    behaviour.current = { status: 200, body: { tier: 'host' } };
    await keyTier.refreshKeyTier();
    assert.deepEqual(heard, ['key', null]);
  } finally {
    await env.done();
  }
});

test('unlimited turns: on by default, and only for a key holder or the host', async () => {
  const behaviour = { current: { status: 200, body: { tier: 'key' } } };
  const env = await setup(behaviour);
  try {
    assert.equal(await keyTier.unlimitedTurnsActive(), true, 'the setting is on when nobody set it');
    for (const [tier, expected] of [['host', true], ['gpu', false], ['cpu', false], ['trial', false], ['open', false]]) {
      behaviour.current = { status: 200, body: { tier } };
      await keyTier.refreshKeyTier();
      assert.equal(await keyTier.unlimitedTurnsActive(), expected, tier);
    }
  } finally {
    await env.done();
  }
});

test('unlimited turns switched off means no limit lifted and no question asked', async () => {
  const env = await setup({ current: { status: 200, body: { tier: 'key' } } }, { 'dlne.agent.unlimitedTurns': false });
  try {
    assert.equal(await keyTier.unlimitedTurnsActive(), false);
    assert.equal(env.server.seen.length, 0);
  } finally {
    await env.done();
  }
});

test('package.json declares dlne.agent.unlimitedTurns: boolean, on, application scope, in plain words', () => {
  const pkg = require('../package.json');
  const setting = pkg.contributes.configuration.properties['dlne.agent.unlimitedTurns'];
  assert.equal(setting.type, 'boolean');
  assert.equal(setting.default, true);
  assert.equal(setting.scope, 'application');
  assert.match(setting.markdownDescription, /API.key/i);
});
