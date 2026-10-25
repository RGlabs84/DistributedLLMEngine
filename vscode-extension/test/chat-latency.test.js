'use strict';
/**
 * A chat turn used to ask /v1/cluster/status before every request, one extra round trip per turn
 * (and the agent makes several requests per user message). The status bar already polls it every
 * 10 s through the same client, so a recent answer is reused.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const stub = require('./helpers/vscode-stub');
const { DlneClient } = require('../dist/client/dlneClient');

process.env.HOME = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-chat-home-'));

async function coordinator() {
  const hits = { status: 0, chat: [] };
  const server = http.createServer((req, res) => {
    if (req.url === '/v1/cluster/status') {
      hits.status++;
      res.setHeader('Content-Type', 'application/json');
      res.end(JSON.stringify({ status: 'Active', global_active_model: 'served.gguf', model_loading: { is_loading: false } }));
      return;
    }
    let body = '';
    req.on('data', (c) => (body += c));
    req.on('end', () => {
      hits.chat.push(JSON.parse(body));
      res.writeHead(200, { 'Content-Type': 'text/event-stream' });
      res.write('data: ' + JSON.stringify({ choices: [{ delta: { content: 'ok' } }] }) + '\n\n');
      res.end('data: [DONE]\n\n');
    });
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  return { hits, url: `http://127.0.0.1:${server.address().port}`, close: () => { server.closeAllConnections(); server.close(); } };
}

const chat = (client) => client.streamChat([{ role: 'user', content: 'hi' }], [], () => {});

test('a chat turn reuses a recent status answer instead of asking again', async () => {
  stub.reset();
  const c = await coordinator();
  stub.state.settings['dlne.serverUrl'] = c.url;
  try {
    const client = new DlneClient();
    await client.getClusterStatusDetailed();          // what the status bar poll does
    await chat(client);
    await chat(client);
    assert.equal(c.hits.status, 1);
    assert.equal(c.hits.chat[1].model, 'served.gguf', 'the model still comes from the status answer');
  } finally {
    c.close();
  }
});

test('without a recent answer, the chat asks once itself', async () => {
  stub.reset();
  const c = await coordinator();
  stub.state.settings['dlne.serverUrl'] = c.url;
  try {
    await chat(new DlneClient());
    assert.equal(c.hits.status, 1);
    assert.equal(c.hits.chat[0].model, 'served.gguf');
  } finally {
    c.close();
  }
});

test('a status answer from another coordinator is never reused', async () => {
  stub.reset();
  const a = await coordinator();
  const b = await coordinator();
  try {
    const client = new DlneClient();
    stub.state.settings['dlne.serverUrl'] = a.url;
    await client.getClusterStatusDetailed();
    stub.state.settings['dlne.serverUrl'] = b.url;
    await chat(client);
    assert.equal(b.hits.status, 1);
  } finally {
    a.close();
    b.close();
  }
});

test('an old status answer is refreshed', async () => {
  stub.reset();
  const c = await coordinator();
  stub.state.settings['dlne.serverUrl'] = c.url;
  const realNow = Date.now;
  try {
    const client = new DlneClient();
    await client.getClusterStatusDetailed();
    Date.now = () => realNow() + 16000;
    await chat(client);
    assert.equal(c.hits.status, 2);
  } finally {
    Date.now = realNow;
    c.close();
  }
});
