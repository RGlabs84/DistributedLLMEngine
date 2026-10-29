'use strict';
/**
 * When the coordinator loses its own engine during a request (2026-10-27: "Server disconnected without sending a
 * response" ended an agent's task with "[System Alert: Node disconnected mid-response.]" as its last word), the
 * agent sends the step again instead. The coordinator marks such a frame "dlne_error" (2.5.4); before that, the
 * alert's text is recognised.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const stub = require('./helpers/vscode-stub');
const { DlneClient, EngineDropped } = require('../dist/client/dlneClient');
const { AgentLoop, ENGINE_DROP_RETRIES, ENGINE_DROP_NOTICE } = require('../dist/agent/agentLoop');

process.env.HOME = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-drop-home-'));

/** A coordinator that streams `frames` (objects become SSE data lines) for every chat request. */
async function coordinator(frames) {
  const server = http.createServer((req, res) => {
    if (req.url === '/v1/cluster/status') {
      res.setHeader('Content-Type', 'application/json');
      res.end(JSON.stringify({ status: 'Active', global_active_model: 'served.gguf', model_loading: { is_loading: false } }));
      return;
    }
    req.resume();
    req.on('end', () => {
      res.writeHead(200, { 'Content-Type': 'text/event-stream' });
      for (const f of frames) res.write('data: ' + JSON.stringify(f) + '\n\n');
      res.end('data: [DONE]\n\n');
    });
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  return { url: `http://127.0.0.1:${server.address().port}`, close: () => { server.closeAllConnections(); server.close(); } };
}

const text = (content) => ({ choices: [{ delta: { content } }] });

async function streamFrom(frames) {
  stub.reset();
  const c = await coordinator(frames);
  stub.state.settings['dlne.serverUrl'] = c.url;
  const shown = [];
  try {
    const result = await new DlneClient().streamChat([{ role: 'user', content: 'hi' }], [], (d) => shown.push(d));
    return { result, shown };
  } catch (err) {
    return { err, shown };
  } finally {
    c.close();
  }
}

test('an engine that keeps timing out while loading is an error after a few tries, not 180 (2.6.2 review)', async () => {
  stub.reset();
  let chats = 0;
  const server = http.createServer((req, res) => {
    if (req.url === '/v1/cluster/status') {
      res.setHeader('Content-Type', 'application/json');
      res.end(JSON.stringify({ status: 'Active', model_loading: { is_loading: false } }));
      return;
    }
    req.resume();
    req.on('end', () => {
      chats++;
      res.writeHead(504, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ detail: 'Inference engine timed out while loading the model.' }));
    });
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  stub.state.settings['dlne.serverUrl'] = `http://127.0.0.1:${server.address().port}`;
  const restore = fastTimers();
  try {
    await assert.rejects(new DlneClient().streamChat([{ role: 'user', content: 'hi' }], [], () => {}),
      /timed out while loading/);
    assert.equal(chats, DlneClient.ENGINE_START_TRIES + 1, 'each try is a charged request: a few, then the refusal');
  } finally {
    restore();
    server.closeAllConnections();
    server.close();
  }
});

test('a frame marked engine_dropped ends the request as EngineDropped, and its alert is never shown', async () => {
  const { err, shown } = await streamFrom([
    text('Half an ans'),
    { ...text('\n\n[System Alert: Node disconnected mid-response.]\n\nPlease send your message again.'), dlne_error: 'engine_dropped' },
  ]);
  assert.ok(err instanceof EngineDropped, String(err));
  assert.equal(err.kind, 'engine_dropped');
  assert.equal(err.partial, true);
  assert.ok(!shown.join('').includes('System Alert'));
});

test("an older coordinator's alert text is recognised the same way", async () => {
  const { err, shown } = await streamFrom([text('\n\n[System Alert: Node disconnected mid-response.]\n\nThe cluster will reorganise itself automatically.')]);
  assert.ok(err instanceof EngineDropped);
  assert.equal(err.partial, false);
  assert.deepEqual(shown, []);
});

test('an engine that timed out is not sent again (it is still loading a model): the answer says so', async () => {
  const { result, err } = await streamFrom([{ ...text('\n\n[System] The engine has not answered yet.'), dlne_error: 'engine_timeout' }]);
  assert.equal(err, undefined);
  assert.match(result.content, /has not answered yet/);
});

/** A client for the agent loop whose first `drops` requests are dropped by the engine. */
function droppingClient(drops, answer = 'All done: the file is fixed.', kind = 'engine_dropped') {
  const requests = [];
  return {
    requests,
    lastKnownClusterStatus: () => null,
    async streamChat(messages, tools, onDelta) {
      requests.push(messages.length);
      if (requests.length <= drops) throw new EngineDropped(kind, false);
      onDelta(answer);
      return { content: answer, toolCalls: [] };
    },
  };
}

function recorder() {
  const shown = { content: '', progress: [] };
  return {
    shown,
    callbacks: {
      onThinkingDelta() {}, onToolStart() {}, onToolEnd() {},
      onContentDelta: (d) => { shown.content += d; },
      onLoadingProgress: (line) => shown.progress.push(line),
    },
  };
}

function fastTimers() {
  const real = global.setTimeout;
  global.setTimeout = (fn, _ms, ...args) => real(fn, 0, ...args);
  return () => { global.setTimeout = real; };
}

test('the agent sends a dropped step again and the task ends as if nothing happened', async () => {
  const restore = fastTimers();
  try {
    const client = droppingClient(2);
    const { shown, callbacks } = recorder();
    assert.equal(await new AgentLoop(client).run('fix it', callbacks), 'done');
    assert.equal(client.requests.length, 3);
    assert.equal(client.requests[0], client.requests[2], 'the same step, with nothing of the dropped replies kept');
    assert.equal(shown.content, 'All done: the file is fixed.');
    assert.ok(shown.progress.some((line) => line.includes(`Sending that step again (1/${ENGINE_DROP_RETRIES})`)));
  } finally {
    restore();
  }
});

test('only an engine that keeps dropping stops the task, which is kept for continue', async () => {
  const restore = fastTimers();
  try {
    const client = droppingClient(ENGINE_DROP_RETRIES + 1);
    const { shown, callbacks } = recorder();
    assert.equal(await new AgentLoop(client).run('fix it', callbacks), 'error');
    assert.equal(client.requests.length, ENGINE_DROP_RETRIES + 1);
    assert.equal(shown.content, ENGINE_DROP_NOTICE);
    assert.ok(!shown.content.includes('System Alert'));
  } finally {
    restore();
  }
});

test('the retries outlast an engine restart: the waits double to 15 s, about 90 s in all', () => {
  const { engineDropWaitMs } = require('../dist/agent/agentLoop');
  const waits = Array.from({ length: ENGINE_DROP_RETRIES }, (_, i) => engineDropWaitMs(i + 1));
  assert.deepEqual(waits.slice(0, 4), [2000, 4000, 8000, 15000]);
  assert.ok(waits.every((ms) => ms <= 15000));
  const total = waits.reduce((a, b) => a + b, 0);
  assert.ok(total >= 60000, `a restart with a shard to load takes about 50 s; the retries wait ${total} ms`);
});

test('a coordinator rebuilding its engine (engine_unavailable) is waited for, and the task carries on', async () => {
  const restore = fastTimers();
  try {
    const client = droppingClient(5, 'All done: the file is fixed.', 'engine_unavailable');
    const { shown, callbacks } = recorder();
    assert.equal(await new AgentLoop(client).run('fix it', callbacks), 'done');
    assert.equal(shown.content, 'All done: the file is fixed.');
    assert.ok(shown.progress.some((line) => line.includes("engine is restarting")));
  } finally {
    restore();
  }
});

// ---- a stream that goes silent ---------------------------------------------------------------------------------
// Node's `timeout` option only reports that a socket was idle; nothing ended the request, so a dead network path (or an
// engine that stopped sending) kept the chat waiting for ever. Silence for DlneClient.streamSilenceMs now ends the
// attempt as a drop, and the agent sends the step again like any other drop.

/** A coordinator whose answer to a chat request is whatever `onChat(res, closed)` does; `closed()` says whether the client hung up. */
async function chatServer(onChat) {
  const hungUp = [];
  const server = http.createServer((req, res) => {
    if (req.url === '/v1/cluster/status') {
      res.setHeader('Content-Type', 'application/json');
      res.end(JSON.stringify({ status: 'Active', global_active_model: 'served.gguf', model_loading: { is_loading: false } }));
      return;
    }
    req.resume();
    req.on('end', () => {
      let gone = false;
      res.on('close', () => { gone = true; hungUp.push(Date.now()); });
      onChat(res, () => gone);
    });
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  stub.reset();
  stub.state.settings['dlne.serverUrl'] = `http://127.0.0.1:${server.address().port}`;
  return { hungUp, close: () => { server.closeAllConnections(); server.close(); } };
}

/**
 * Runs `body` with the silence limits shortened: `afterAnswer` once the answer has started (streamSilenceMs), and
 * `beforeAnswer` until it does (firstByteSilenceMs), which is the same unless a test says otherwise.
 */
async function withSilenceLimit(afterAnswer, body, beforeAnswer = afterAnswer) {
  const before = [DlneClient.streamSilenceMs, DlneClient.firstByteSilenceMs];
  DlneClient.streamSilenceMs = afterAnswer;
  DlneClient.firstByteSilenceMs = beforeAnswer;
  try {
    return await body();
  } finally {
    [DlneClient.streamSilenceMs, DlneClient.firstByteSilenceMs] = before;
  }
}

test('a request the coordinator never answers ends as EngineDropped after the silence limit, and the connection is closed', async () => {
  const server = await chatServer(() => { /* accepts it and says nothing, ever */ });
  try {
    await withSilenceLimit(250, async () => {
      const started = Date.now();
      await assert.rejects(
        new DlneClient().streamChat([{ role: 'user', content: 'hi' }], [], () => {}),
        (err) => err instanceof EngineDropped && err.kind === 'stream_silent' && err.partial === false
      );
      const waited = Date.now() - started;
      assert.ok(waited >= 240 && waited < 2000, `ended after ${waited} ms`);
      await until(() => server.hungUp.length === 1, 2000);
    });
  } finally {
    server.close();
  }
});

test('an answer that stops half way ends the same way, with what had arrived marked as partial', async () => {
  const server = await chatServer((res) => {
    res.writeHead(200, { 'Content-Type': 'text/event-stream' });
    res.write('data: ' + JSON.stringify(text('Half an ans')) + '\n\n'); // and then nothing
  });
  try {
    await withSilenceLimit(250, async () => {
      const shown = [];
      await assert.rejects(
        new DlneClient().streamChat([{ role: 'user', content: 'hi' }], [], (d) => shown.push(d)),
        (err) => err instanceof EngineDropped && err.kind === 'stream_silent' && err.partial === true
      );
      assert.deepEqual(shown, ['Half an ans']);
    });
  } finally {
    server.close();
  }
});

test('any bytes reset the clock: a coordinator that keeps the stream alive with comment lines is not cut off', async () => {
  const server = await chatServer((res, closed) => {
    res.writeHead(200, { 'Content-Type': 'text/event-stream' });
    const keepAlive = setInterval(() => { if (closed()) clearInterval(keepAlive); else res.write(': still working\n\n'); }, 100);
    setTimeout(() => {
      clearInterval(keepAlive);
      if (closed()) return;
      res.write('data: ' + JSON.stringify(text('Worth the wait.')) + '\n\n');
      res.end('data: [DONE]\n\n');
    }, 800); // well over the 300 ms limit, in all
  });
  try {
    await withSilenceLimit(300, async () => {
      const result = await new DlneClient().streamChat([{ role: 'user', content: 'hi' }], [], () => {});
      assert.equal(result.content, 'Worth the wait.');
    });
  } finally {
    server.close();
  }
});

test('the agent sends a silent step again after the usual pause, and says why', async () => {
  const restore = fastTimers();
  let asked = 0;
  const server = await chatServer((res) => {
    if (++asked === 1) return; // silent
    res.writeHead(200, { 'Content-Type': 'text/event-stream' });
    res.write('data: ' + JSON.stringify(text('Back again.')) + '\n\n');
    res.end('data: [DONE]\n\n');
  });
  try {
    await withSilenceLimit(250, async () => {
      const { shown, callbacks } = recorder();
      assert.equal(await new AgentLoop(new DlneClient()).run('hello', callbacks), 'done');
      assert.equal(asked, 2);
      assert.equal(shown.content, 'Back again.');
      assert.ok(shown.progress.some((line) => line.includes(`The swarm stopped answering. Sending that step again (1/${ENGINE_DROP_RETRIES})`)));
    });
  } finally {
    restore();
    server.close();
  }
});

// ---- the silence limit, split: long before the answer starts, short once it has --------------------------------------
// A long prompt is read before the first word, and a volunteer's machine reads 80 to 120 tokens a second without saying
// anything (30,000 tokens is four minutes), so the wait before the answer is 300 s; once the answer has started, 120 s
// of silence is a dead connection. Bytes reset whichever clock is running; only the answer itself changes it.

test('the limits are 300 s before the answer starts and 120 s once it has', () => {
  assert.equal(DlneClient.firstByteSilenceMs, 300_000);
  assert.equal(DlneClient.streamSilenceMs, 120_000);
});

test('before the answer starts the longer limit applies: a request that says nothing is not ended at the shorter one', async () => {
  const server = await chatServer(() => { /* accepts it and says nothing, ever */ });
  try {
    await withSilenceLimit(150, async () => {
      const started = Date.now();
      await assert.rejects(
        new DlneClient().streamChat([{ role: 'user', content: 'hi' }], [], () => {}),
        (err) => err instanceof EngineDropped && err.kind === 'stream_silent' && err.partial === false
      );
      const waited = Date.now() - started;
      assert.ok(waited >= 550 && waited < 4000, `ended after ${waited} ms: the limit before the answer was 600 ms, the one after it 150 ms`);
    }, 600);
  } finally {
    server.close();
  }
});

test('once the answer has started the shorter limit applies: a stream that stops after its first words is ended at it', async () => {
  const server = await chatServer((res) => {
    res.writeHead(200, { 'Content-Type': 'text/event-stream' });
    res.write('data: ' + JSON.stringify(text('First words. ')) + '\n\n'); // and then nothing
  });
  try {
    await withSilenceLimit(150, async () => {
      const started = Date.now();
      const shown = [];
      await assert.rejects(
        new DlneClient().streamChat([{ role: 'user', content: 'hi' }], [], (d) => shown.push(d)),
        (err) => err instanceof EngineDropped && err.kind === 'stream_silent' && err.partial === true
      );
      const waited = Date.now() - started;
      assert.ok(waited >= 140 && waited < 2500, `ended after ${waited} ms: the limit once the answer had started was 150 ms, the one before it 6000 ms`);
      assert.deepEqual(shown, ['First words. ']);
    }, 6000);
  } finally {
    server.close();
  }
});

test('before the answer starts, comment lines keep the stream alive without switching it to the shorter limit', async () => {
  // Comments for 600 ms, then 500 ms of nothing (longer than the 250 ms that applies once the answer has started), then
  // the answer. The whole wait is longer than the 1,000 ms limit before the answer: only the comments, each resetting
  // the clock, got it through, and the pause after them was judged by the long limit.
  const server = await chatServer((res, closed) => {
    res.writeHead(200, { 'Content-Type': 'text/event-stream' });
    const keepAlive = setInterval(() => { if (closed()) clearInterval(keepAlive); else res.write(': still working\n\n'); }, 100);
    setTimeout(() => clearInterval(keepAlive), 600);
    setTimeout(() => {
      if (closed()) return;
      res.write('data: ' + JSON.stringify(text('Worth the wait.')) + '\n\n');
      res.end('data: [DONE]\n\n');
    }, 1100);
  });
  try {
    await withSilenceLimit(250, async () => {
      const result = await new DlneClient().streamChat([{ role: 'user', content: 'hi' }], [], () => {});
      assert.equal(result.content, 'Worth the wait.');
    }, 1000);
  } finally {
    server.close();
  }
});

test('once the answer has started, bytes still reset the shorter clock: comment lines between the words keep it alive', async () => {
  const server = await chatServer((res, closed) => {
    res.writeHead(200, { 'Content-Type': 'text/event-stream' });
    res.write('data: ' + JSON.stringify(text('First words. ')) + '\n\n');
    const keepAlive = setInterval(() => { if (closed()) clearInterval(keepAlive); else res.write(': still working\n\n'); }, 100);
    setTimeout(() => {
      clearInterval(keepAlive);
      if (closed()) return;
      res.write('data: ' + JSON.stringify(text('Then the rest.')) + '\n\n');
      res.end('data: [DONE]\n\n');
    }, 900); // well over the 300 ms limit, in all
  });
  try {
    await withSilenceLimit(300, async () => {
      const result = await new DlneClient().streamChat([{ role: 'user', content: 'hi' }], [], () => {});
      assert.equal(result.content, 'First words. Then the rest.');
    }, 6000);
  } finally {
    server.close();
  }
});

// ---- a coding member reads a long prompt before it says a word ---------------------------------------------------------
// The coordinator's own engine says "still working" every 15 s while it reads, so 300 s of silence is a dead connection.
// A coding member's stream is sealed and carries no such lines, and a member reads 80 to 120 tokens a second: a 29,000 token
// prompt is silent for four to six minutes, and the fixed limit cut it off and sent it again, up to 8 times (review of 1.5.0).

test('the wait before the answer is 300 s for the coordinator\'s own engine, and for a coding member grows with the prompt (tokens / 60 s and a minute, at most 15 minutes)', () => {
  const limit = (tokens, member) => DlneClient.firstByteLimitMs(tokens, member);
  assert.equal(DlneClient.memberReadTokensPerSecond, 60);
  assert.equal(DlneClient.memberStartAllowanceMs, 60_000);
  for (const tokens of [0, 1000, 29_000, 500_000]) assert.equal(limit(tokens, false), 300_000, 'the coordinator\'s own engine keeps the stream alive: the same 300 s whatever the prompt');
  assert.equal(limit(0, true), 300_000, 'never less than 300 s');
  assert.equal(limit(10_000, true), 300_000, '10,000 tokens: 167 s and a minute is still under 300 s');
  assert.ok(Math.abs(limit(29_000, true) - (29_000 / 60 * 1000 + 60_000)) < 1000, `29,000 tokens: about 543 s, got ${limit(29_000, true)} ms`);
  assert.ok(Math.abs(limit(39_000, true) - (39_000 / 60 * 1000 + 60_000)) < 1000, 'the biggest prompt the agent builds: about 710 s');
  assert.equal(limit(500_000, true), 900_000, 'however big, a request is not awaited for more than 15 minutes');
});

test('a request a coding member answers is given time to read its prompt: it is not cut off at the limit a short prompt gets', async () => {
  // Nothing is sealed here (no respKey): this is about the limit the attempt is given, which is set by the route.
  const server = await chatServer((res, closed) => {
    setTimeout(() => {
      if (closed()) return;
      res.writeHead(200, { 'Content-Type': 'text/event-stream' });
      res.write('data: ' + JSON.stringify(text('Read it all.')) + '\n\n');
      res.end('data: [DONE]\n\n');
    }, 700); // silent for 700 ms, as a member reading a long prompt is
  });
  const tunables = [DlneClient.memberReadTokensPerSecond, DlneClient.memberStartAllowanceMs];
  DlneClient.memberReadTokensPerSecond = 1000;
  DlneClient.memberStartAllowanceMs = 0;
  const base = `http://127.0.0.1:${Number(new URL(stub.state.settings['dlne.serverUrl']).port)}`;
  const attempt = (content, route) => {
    const payload = { messages: [{ role: 'user', content }], stream: true };
    const prepared = { body: JSON.stringify(payload), headers: {}, route, privacy: 'open', redactions: 0 };
    return new DlneClient().executeSingleStreamAttempt(base, payload, () => {}, undefined, undefined, undefined, false, prepared);
  };
  try {
    await withSilenceLimit(250, async () => {
      // about 3,000 tokens by the coordinator's count (3 characters each): a 3 s wait at 1,000 tokens a second
      const big = 'x'.repeat(9000);
      assert.equal((await attempt(big, 'member-1')).content, 'Read it all.', 'a member answering a big prompt is waited for');
      // the same silence from the coordinator's own engine, or a member given a small prompt, is judged by the base limit
      await assert.rejects(attempt(big, undefined), (err) => err instanceof EngineDropped && err.kind === 'stream_silent');
      await assert.rejects(attempt('hi', 'member-1'), (err) => err instanceof EngineDropped && err.kind === 'stream_silent');
    }, 250);
  } finally {
    [DlneClient.memberReadTokensPerSecond, DlneClient.memberStartAllowanceMs] = tunables;
    server.close();
  }
});

// ---- a free-trial user's silent step is sent again, like everyone else's ----------------------------------------------------
// (Stopping instead would save nothing, because the user's "continue" spends the same trial chat, and it would bring back
// "it stops running, doesn't continue".)

test('a step the swarm went silent on is sent again for a free-trial user too, and the task carries on', async () => {
  const restore = fastTimers();
  let asked = 0;
  const server = await chatServer((res) => {
    asked++;
    if (asked === 1) {
      res.writeHead(200, { 'Content-Type': 'text/event-stream', 'X-DLNE-Quota-Remaining': '7' });
      res.flushHeaders(); // the trial header arrives, and then the engine says nothing
      return;
    }
    res.writeHead(200, { 'Content-Type': 'text/event-stream', 'X-DLNE-Quota-Remaining': '6' });
    res.write('data: ' + JSON.stringify(text('Back again.')) + '\n\n');
    res.end('data: [DONE]\n\n');
  });
  try {
    await withSilenceLimit(250, async () => {
      const client = new DlneClient();
      const { shown, callbacks } = recorder();
      assert.equal(await new AgentLoop(client).run('hello', callbacks), 'done');
      assert.equal(client.getQuotaRemaining(), 6, 'the client knows it is on the trial');
      assert.equal(asked, 2, 'the silent step was sent again by itself');
      assert.equal(shown.content, 'Back again.');
      assert.ok(shown.progress.some((line) => line.includes(`The swarm stopped answering. Sending that step again (1/${ENGINE_DROP_RETRIES})`)));
    });
  } finally {
    restore();
    server.close();
  }
});

/** Polls until `condition()` holds (or throws after `ms`). */
async function until(condition, ms) {
  const started = Date.now();
  while (!condition()) {
    if (Date.now() - started > ms) throw new Error(`timed out after ${ms} ms`);
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}
