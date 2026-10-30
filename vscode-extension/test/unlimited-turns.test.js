'use strict';
/**
 * The agent's turn limit and API-key holders (2.7.0, docs/release-2.7-contract.md section 3): with dlne.agent.unlimitedTurns on
 * (the default) and the coordinator confirming a key holder at the start of a run, the run has no turn limit; Stop and the
 * loop check still end it. Otherwise the limit is today's (100 by default, 10 to 500 as the setting says). The coordinator
 * is a small server on this machine answering /v1/me; the model is scripted.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');

const { scriptedClient, toolCall, stub } = require('./helpers/chat-harness');
const { AgentLoop, pauseNotice } = require('../dist/agent/agentLoop');
const { ToolRegistry } = require('../dist/agent/tools');
const { DlneClient } = require('../dist/client/dlneClient');
const keyTier = require('../dist/agent/keyTier');

const realExecute = ToolRegistry.executeTool;
test.afterEach(() => {
  ToolRegistry.executeTool = realExecute;
  keyTier.resetKeyTierForTests();
});

/** A coordinator that says what tier the caller is (or that it knows no /v1/me, as before 2.7.0), until closed. */
async function coordinatorSays(answer) {
  const server = http.createServer((req, res) => {
    const known = answer !== 'old';
    res.writeHead(known ? 200 : 404, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(known ? { tier: answer } : { detail: 'Not Found' }));
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  stub.reset();
  stub.state.settings['dlne.serverUrl'] = `http://127.0.0.1:${server.address().port}`;
  stub.state.settings['dlne.apiKey'] = 'dlne_sk_test_key';
  keyTier.resetKeyTierForTests();
  const subscription = keyTier.bindKeyTierClient(new DlneClient());
  return () => new Promise((resolve) => { subscription.dispose(); server.closeAllConnections?.(); server.close(resolve); });
}

function recorder() {
  const seen = { content: '', progress: [], errors: [] };
  return {
    seen,
    callbacks: {
      onThinkingDelta() {},
      onContentDelta: (d) => { seen.content += d; },
      onToolStart() {},
      onToolEnd() {},
      onLoadingProgress: (line) => seen.progress.push(line),
      onError: (err) => seen.errors.push(err),
    },
  };
}

/** A model that reads a different file at each of its first `steps` replies (so the loop check sees progress), then answers. */
function readsThenAnswers(steps, onRequest) {
  let n = 0;
  return scriptedClient([(request) => {
    n++;
    onRequest?.(n, request);
    return n <= steps ? { toolCalls: [toolCall('read_file', { path: `file-${n}.ts` })] } : { content: 'All read.' };
  }]);
}

function fakeTools() {
  ToolRegistry.executeTool = async (name, args) => ({ name, output: `1: ${args.path}` });
}

const STEPS = 520;     // past today's hard maximum of 500

test('a key holder with the setting on (the default) runs past 500 steps with no pause', async () => {
  const close = await coordinatorSays('key');
  try {
    fakeTools();
    const client = readsThenAnswers(STEPS);
    const { seen, callbacks } = recorder();
    const log = [];
    const outcome = await new AgentLoop(client, { log: (line) => log.push(line) })
      .run('read everything', callbacks, { approveAll: () => true, maxTurns: 100 });
    assert.equal(outcome, 'done');
    assert.equal(client.requests.length, STEPS + 1);
    assert.ok(!seen.content.includes('Paused'), 'the pause notice never comes');
    assert.ok(!client.requests.some((r) => r.messages.some((m) => String(m.content).includes('has used its limit'))), 'the pause prompt is never sent');
    assert.ok(log.some((line) => line.includes('no turn limit')));
    assert.ok(log.some((line) => line.includes(`${STEPS + 1} steps`)), 'the step counter still counts');
  } finally {
    await close();
  }
});

test('the host is lifted too', async () => {
  const close = await coordinatorSays('host');
  try {
    fakeTools();
    const client = readsThenAnswers(120);
    const outcome = await new AgentLoop(client, { log() {} }).run('go', recorder().callbacks, { approveAll: () => true, maxTurns: 10 });
    assert.equal(outcome, 'done');
    assert.equal(client.requests.length, 121);
  } finally {
    await close();
  }
});

test('with the setting off a key holder has today\'s limit: 100 by default, and 500 at the most', async () => {
  const close = await coordinatorSays('key');
  try {
    fakeTools();
    stub.state.settings['dlne.agent.unlimitedTurns'] = false;
    let client = readsThenAnswers(STEPS);
    let { seen, callbacks } = recorder();
    let outcome = await new AgentLoop(client, { log() {} }).run('read everything', callbacks, { approveAll: () => true });
    assert.equal(outcome, 'paused');
    assert.equal(client.requests.length, 101, '100 steps and one more to report');
    assert.ok(seen.content.includes(pauseNotice(100).trim()));

    client = readsThenAnswers(STEPS);
    ({ seen, callbacks } = recorder());
    outcome = await new AgentLoop(client, { log() {} }).run('read everything', callbacks, { approveAll: () => true, maxTurns: 9999 });
    assert.equal(outcome, 'paused');
    assert.equal(client.requests.length, 501, 'the setting\'s own maximum is still 500');
    assert.ok(seen.content.includes(pauseNotice(500).trim()));
  } finally {
    await close();
  }
});

test('anyone the coordinator does not call a key holder keeps the limit', async () => {
  for (const answer of ['trial', 'gpu', 'cpu', 'open', 'old']) {
    const close = await coordinatorSays(answer);
    try {
      fakeTools();
      const client = readsThenAnswers(STEPS);
      const outcome = await new AgentLoop(client, { log() {} }).run('go', recorder().callbacks, { approveAll: () => true, maxTurns: 30 });
      assert.equal(outcome, 'paused', answer);
      assert.equal(client.requests.length, 31, answer);
    } finally {
      await close();
    }
  }
});

test('before the extension has a client for /v1/me (or while the coordinator is not asked) the limit holds', async () => {
  stub.reset();
  keyTier.resetKeyTierForTests();
  fakeTools();
  const client = readsThenAnswers(STEPS);
  const outcome = await new AgentLoop(client, { log() {} }).run('go', recorder().callbacks, { approveAll: () => true, maxTurns: 20 });
  assert.equal(outcome, 'paused');
  assert.equal(client.requests.length, 21);
});

test('a caller can say so itself, and the answer is taken once at the start of the run', async () => {
  const close = await coordinatorSays('trial');
  try {
    fakeTools();
    const client = readsThenAnswers(60);
    const outcome = await new AgentLoop(client, { log() {} }).run('go', recorder().callbacks, { approveAll: () => true, maxTurns: 10, unlimitedTurns: true });
    assert.equal(outcome, 'done');
    assert.equal(client.requests.length, 61);
  } finally {
    await close();
  }
});

test('Stop still ends an unlimited run', async () => {
  const close = await coordinatorSays('key');
  try {
    fakeTools();
    const loop = new AgentLoop(readsThenAnswers(STEPS, (n) => { if (n === 150) loop.abort(); }), { log() {} });
    const outcome = await loop.run('go', recorder().callbacks, { approveAll: () => true, maxTurns: 10 });
    assert.equal(outcome, 'aborted');
  } finally {
    await close();
  }
});

test('the loop check still stops an unlimited run that keeps repeating itself', async () => {
  const close = await coordinatorSays('key');
  try {
    fakeTools();
    const client = scriptedClient([{ toolCalls: [toolCall('read_file', { path: 'same.ts' })] }]);
    const { seen, callbacks } = recorder();
    const outcome = await new AgentLoop(client, { log() {} }).run('go', callbacks, { approveAll: () => true, maxTurns: 10 });
    assert.equal(outcome, 'paused', 'the loop check ends it as a pause, which offers Continue');
    assert.ok(client.requests.length < 40, `stopped after ${client.requests.length} requests`);
    assert.ok(seen.progress.some((line) => line.includes('🔁')) || seen.content.length > 0);
  } finally {
    await close();
  }
});

test('nothing in the extension\'s text shows "step N of M" or a fixed ceiling for a run', () => {
  const fs = require('node:fs');
  const path = require('node:path');
  const walk = (dir) => fs.readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
    e.isDirectory() ? walk(path.join(dir, e.name)) : e.name.endsWith('.ts') ? [path.join(dir, e.name)] : []);
  const offenders = walk(path.join(__dirname, '..', 'src'))
    .filter((file) => /\bsteps? \$\{[^}]*\} of \$\{|\bstep \d+ of \d+/i.test(fs.readFileSync(file, 'utf8')));
  assert.deepEqual(offenders, []);
});
