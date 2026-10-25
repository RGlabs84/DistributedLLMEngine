'use strict';
/**
 * The free-trial plumbing on the extension side: the client must read the coordinator's
 * X-DLNE-Quota-Remaining header from every response type it uses, and the status bar must turn
 * it into the text a user sees.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const stub = require('./helpers/vscode-stub');
const { DlneClient } = require('../dist/client/dlneClient');
const { ClusterStatusBar } = require('../dist/statusbar/clusterStatusBar');

// The client reads ~/.dlne tokens; keep the test away from the developer's real ones.
process.env.HOME = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-quota-home-'));

/** A fake coordinator whose /v1/completions and /v1/chat/completions replies are scripted per test. */
function fakeCoordinator(handler) {
  const server = http.createServer((req, res) => {
    if (req.url === '/v1/cluster/status') {
      res.setHeader('Content-Type', 'application/json');
      res.end(JSON.stringify({ status: 'Active', global_active_model: 'm.gguf', model_loading: { is_loading: false } }));
      return;
    }
    let body = '';
    req.on('data', (c) => (body += c));
    req.on('end', () => handler(req, res, body));
  });
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve(server)));
}

async function withServer(handler, fn) {
  stub.reset();
  const server = await fakeCoordinator(handler);
  stub.state.settings['dlne.serverUrl'] = `http://127.0.0.1:${server.address().port}`;
  try {
    await fn(new DlneClient());
  } finally {
    server.close();
  }
}

const completion = (res, headers) => {
  res.writeHead(200, { 'Content-Type': 'application/json', ...headers });
  res.end(JSON.stringify({ choices: [{ text: 'return a + b' }] }));
};

test('a fresh client has no quota information', async () => {
  stub.reset();
  assert.equal(new DlneClient().getQuotaRemaining(), null);
});

test('autocomplete responses update the remaining count', async () => {
  await withServer((req, res) => completion(res, { 'X-DLNE-Quota-Remaining': '137' }), async (client) => {
    assert.equal(await client.getCompletion('def add(a, b):'), 'return a + b');
    assert.equal(client.getQuotaRemaining(), 137);
  });
});

test('the count goes back to null once the coordinator stops sending it (contributor / API key)', async () => {
  let send = true;
  await withServer((req, res) => completion(res, send ? { 'X-DLNE-Quota-Remaining': '5' } : {}), async (client) => {
    await client.getCompletion('x');
    assert.equal(client.getQuotaRemaining(), 5);
    send = false;
    await client.getCompletion('x');
    assert.equal(client.getQuotaRemaining(), null);
  });
});

test('a garbage header value is ignored rather than shown', async () => {
  await withServer((req, res) => completion(res, { 'X-DLNE-Quota-Remaining': 'lots' }), async (client) => {
    await client.getCompletion('x');
    assert.equal(client.getQuotaRemaining(), null);
  });
});

test('a 403 carrying 0 records an exhausted trial', async () => {
  await withServer((req, res) => {
    res.writeHead(403, { 'Content-Type': 'application/json', 'X-DLNE-Quota-Remaining': '0' });
    res.end(JSON.stringify({ detail: 'DLNE Compute Required: Free starter quota used.' }));
  }, async (client) => {
    assert.equal(await client.getCompletion('x'), '');
    assert.equal(client.getQuotaRemaining(), 0);
  });
});

test('streamed chat responses update the remaining count', async () => {
  await withServer((req, res) => {
    res.writeHead(200, { 'Content-Type': 'text/event-stream', 'X-DLNE-Quota-Remaining': '41' });
    res.write('data: ' + JSON.stringify({ choices: [{ delta: { content: 'hi' } }] }) + '\n\n');
    res.write('data: ' + JSON.stringify({ choices: [{ delta: {}, finish_reason: 'stop' }] }) + '\n\n');
    res.end('data: [DONE]\n\n');
  }, async (client) => {
    let text = '';
    await client.streamChat([{ role: 'user', content: 'hello' }], [], (d) => (text += d));
    assert.equal(text, 'hi');
    assert.equal(client.getQuotaRemaining(), 41);
  });
});

test('a chat 403 records 0 and surfaces an error naming the status', async () => {
  await withServer((req, res) => {
    res.writeHead(403, { 'Content-Type': 'application/json', 'X-DLNE-Quota-Remaining': '0' });
    res.end(JSON.stringify({ detail: 'trial used' }));
  }, async (client) => {
    await assert.rejects(
      client.executeSingleStreamAttempt(client.getServerUrl(), { model: 'm', messages: [] }, () => {}),
      /403/
    );
    assert.equal(client.getQuotaRemaining(), 0);
  });
});

// ---- status bar -------------------------------------------------------------------------

function statusBar({ quota, worker, cluster }) {
  stub.reset();
  const client = {
    getQuotaRemaining: () => quota,
    getServerUrl: () => 'http://coordinator',
    getClusterStatusDetailed: async () => cluster,
  };
  const workerManager = {
    onStatusChange: () => ({ dispose() {} }),
    getStatus: () => ({
      state: worker,
      vramContributedGb: 8,
      gpu: { name: 'GPU', api: 'vulkan', hasGpu: true, vramTotalGb: 8 },
    }),
  };
  const bar = new ClusterStatusBar(client, workerManager);
  return { bar, item: stub.state.statusBarItems[0] };
}

const okCluster = {
  status: 'ok',
  httpStatus: 200,
  data: {
    status: 'Active', global_active_model: 'qwen', total_vram_gb: 24, total_memory_gb: 44, total_sysram_gb: 20,
    active_workers_count: 2, active_leases_count: 0, is_locked_for_edits: false, server_version: '1.1.12',
    model_loading: { is_loading: false },
  },
};

test('status bar shows the free-trial counter for a non-contributor', async () => {
  const { bar, item } = statusBar({ quota: 137, worker: 'idle', cluster: okCluster });
  await bar.updateStatus();
  assert.match(item.text, /\[Free trial: 137 chats left\]/);
  assert.doesNotMatch(item.text, /Contributing/);
});

test('status bar shows contribution, not the trial, for a contributor', async () => {
  const { bar, item } = statusBar({ quota: 137, worker: 'contributing', cluster: okCluster });
  await bar.updateStatus();
  assert.match(item.text, /\[Contributing 8GB\]/);
  assert.doesNotMatch(item.text, /Free trial/);
});

test('status bar shows no trial tag when the coordinator reports none', async () => {
  const { bar, item } = statusBar({ quota: null, worker: 'idle', cluster: okCluster });
  await bar.updateStatus();
  assert.doesNotMatch(item.text, /Free trial/);
});

test('status bar shows an exhausted trial as 0 chats left', async () => {
  const { bar, item } = statusBar({ quota: 0, worker: 'idle', cluster: okCluster });
  await bar.updateStatus();
  assert.match(item.text, /\[Free trial: 0 chats left\]/);
});
