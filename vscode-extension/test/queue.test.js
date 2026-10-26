'use strict';
/**
 * "The swarm is busy": a request that has to wait for a coding slot says where it stands, whichever way the
 * coordinator tells it (in the stream, in a 503, or not at all), and never waits in silence.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const stub = require('./helpers/vscode-stub');
const swarm = require('./helpers/mock-swarm');
const { DlneClient, ServerRefusal } = require('../dist/client/dlneClient');
const queue = require('../dist/client/queue');
const { WaitWatcher } = require('../dist/client/waitWatcher');
const { ClusterStatusBar } = require('../dist/statusbar/clusterStatusBar');
const { SidebarProvider } = require('../dist/views/sidebarProvider');

process.env.HOME = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-queue-home-'));

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const frame = (res, obj) => res.write(`data: ${JSON.stringify(obj)}\n\n`);

async function world(handlers = {}) {
  stub.reset();
  const c = await swarm.startCoordinator({ status: swarm.statusBody(), ...handlers });
  stub.state.settings['dlne.serverUrl'] = c.url;
  return { c, client: new DlneClient(), close: () => c.close() };
}

async function ask(client, options = {}, onProgress = true) {
  let out = '';
  const progress = [];
  const updates = [];
  const result = await client.streamChat(
    [{ role: 'user', content: 'hi' }], [], (d) => { out += d; }, undefined, options.signal,
    onProgress ? (line) => progress.push(line) : undefined,
    options.noQueueCallback ? {} : { onQueue: (q) => updates.push(q) }
  );
  return { out, progress, updates, result };
}

// ---- the wire object --------------------------------------------------------------------------------------------

test('a dlne_queue object is read field by field, and bad fields are dropped, not trusted', () => {
  assert.deepEqual(
    queue.parseQueueInfo({ position: 3, waiting: 5, slots: 2, in_use: 2, eta_seconds: 40, ticket: 'abc.DEF-123_~' }),
    { position: 3, waiting: 5, slots: 2, inUse: 2, etaSeconds: 40, ticket: 'abc.DEF-123_~' }
  );
  assert.deepEqual(queue.parseQueueInfo({}), {}, 'queued, no details');
  assert.deepEqual(
    queue.parseQueueInfo({ position: 0, waiting: -1, slots: 'two', in_use: NaN, eta_seconds: 1e9, ticket: 'has space\r\nheader' }),
    {}
  );
  assert.equal(queue.parseQueueInfo(null), null);
  assert.equal(queue.parseQueueInfo([1, 2]), null);
  assert.equal(queue.parseQueueInfo('3'), null);
});

test('a refusal body is "busy" only if it carries a dlne_queue', () => {
  assert.deepEqual(queue.queueFromBody('{"detail":"busy","dlne_queue":{"position":2}}'), { position: 2 });
  assert.equal(queue.queueFromBody('{"detail":"You used all 300 chats"}'), null);
  assert.equal(queue.queueFromBody('not json'), null);
});

test('coding_queue from the status is read defensively', () => {
  const stats = queue.parseCodingQueue({ coding_queue: { slots: 2, in_use: 2, waiting: 3, waiting_by_priority: { 0: 1, 2: 2, x: 5, 1: -4 } } });
  assert.deepEqual(stats, { slots: 2, in_use: 2, waiting: 3, waiting_by_priority: { 0: 1, 2: 2 } });
  assert.equal(queue.parseCodingQueue({}), null);
  assert.equal(queue.parseCodingQueue({ coding_queue: { slots: 0, in_use: 0, waiting: 0 } }), null);
  assert.equal(queue.isBusy({ slots: 2, in_use: 2, waiting: 1 }), true);
  assert.equal(queue.isBusy({ slots: 2, in_use: 1, waiting: 1 }), false, 'a free slot: not queued');
  assert.equal(queue.isBusy({ slots: 2, in_use: 2, waiting: 0 }), false, 'every slot taken but nobody waiting: this request holds one');
  assert.equal(queue.isBusy(null), false);
});

test('the place in line: better-placed requests first, then those already waiting at the same priority', () => {
  const est = new queue.PlaceEstimator();
  const stats = (by) => ({ slots: 2, in_use: 2, waiting: Object.values(by).reduce((a, b) => a + b, 0), waiting_by_priority: by });
  // free trial (2) with 1 GPU contributor and 3 trial users waiting: behind the 1 and the 2 earlier trial users
  assert.equal(est.estimate(stats({ 0: 1, 2: 3 }), 2), 4);
  // later arrivals at the same priority are behind this request: the figure does not rise with them ...
  assert.equal(est.estimate(stats({ 0: 1, 2: 9 }), 2), 4);
  // ... it falls as the people ahead are served ...
  assert.equal(est.estimate(stats({ 0: 1, 2: 1 }), 2), 2);
  // ... and a GPU contributor never waits behind trial users
  assert.equal(new queue.PlaceEstimator().estimate(stats({ 0: 1, 2: 8 }), 0), 1);
  // with no breakdown, all that is known is how many wait
  assert.equal(new queue.PlaceEstimator().estimate({ slots: 2, in_use: 2, waiting: 4 }, 2), 4);
});

test('the wording says where you stand, how many wait, and what skips the line', () => {
  const base = { source: 'stream', waitedMs: 0, priority: 2, position: 3, waiting: 5, slots: 2 };
  assert.equal(queue.queueHeadline(base), 'All 2 coding slots are busy. You are #3 in line.');
  assert.equal(queue.queueHeadline({ ...base, source: 'status' }), 'All 2 coding slots are busy. You are about #3 in line.');
  assert.equal(queue.queueHeadline({ source: 'response', waitedMs: 0, priority: 2 }), 'The swarm is busy. Your request is waiting its turn.');
  assert.match(queue.queueDetail({ ...base, etaSeconds: 40 }), /^5 waiting, about 40 s to go\. GPU contributors go first: lending your GPU skips the line\.$/);
  assert.match(queue.queueDetail({ ...base, priority: 0 }), /You are in the first class, ahead of free-trial and CPU-pool users/);
  assert.match(queue.queueDetail({ ...base, waitedMs: 65000 }), /you have waited 65 s/);
  assert.match(queue.queueProgressLine(base), /^⏳ All 2 coding slots are busy\. You are #3 in line \(5 waiting\)\. GPU contributors go first\.$/);
  assert.equal(queue.queueStatusText(base), '#3 in line');
  assert.equal(queue.queueStatusText({ ...base, source: 'status' }), '~#3 in line');
  assert.equal(queue.queueStatusText({ source: 'response', waitedMs: 0, priority: 2 }), 'waiting for a slot');
  assert.equal(queue.formatWait(30), '30 s');
  assert.equal(queue.formatWait(600), '10 min');
});

// ---- channel 1: the coordinator speaks in the stream --------------------------------------------------------------

test('queue frames in the stream are shown as they come, then taken down when the answer starts', async () => {
  const w = await world({
    chat: async ({ res }) => {
      res.writeHead(200, { 'Content-Type': 'text/event-stream' });
      frame(res, { dlne_queue: { position: 3, waiting: 5, slots: 2, in_use: 2, eta_seconds: 40 } });
      await sleep(30);
      frame(res, { dlne_queue: { position: 2, waiting: 4, slots: 2, in_use: 2 } });
      await sleep(30);
      frame(res, { dlne_queue: { position: 1, waiting: 3, slots: 2, in_use: 2 } });
      await sleep(30);
      frame(res, swarm.chunk({ content: 'served' }));
      frame(res, swarm.chunk({}, 'stop'));
      res.end('data: [DONE]\n\n');
    },
  });
  try {
    const { out, updates, progress } = await ask(w.client);
    assert.equal(out, 'served');
    assert.deepEqual(updates.map((u) => (u ? u.position : null)), [3, 2, 1, null]);
    assert.ok(updates.slice(0, 3).every((u) => u.source === 'stream'));
    assert.equal(updates[0].etaSeconds, 40);
    assert.ok(!progress.some((line) => /coding slots/.test(line)), 'a caller that takes queue updates is not also told in a progress line');
    assert.equal(w.client.currentQueue(), null);
  } finally {
    w.close();
  }
});

test('a caller with no queue callback is told the same thing as a progress line', async () => {
  const w = await world({
    chat: async ({ res }) => {
      res.writeHead(200, { 'Content-Type': 'text/event-stream' });
      frame(res, { dlne_queue: { position: 2, waiting: 3, slots: 2, in_use: 2 } });
      await sleep(30);
      frame(res, swarm.chunk({ content: 'ok' }));
      res.end('data: [DONE]\n\n');
    },
  });
  try {
    const { progress } = await ask(w.client, { noQueueCallback: true });
    assert.ok(progress.some((line) => /All 2 coding slots are busy\. You are #2 in line \(3 waiting\)/.test(line)), progress.join('|'));
  } finally {
    w.close();
  }
});

test('queue frames never reach the answer or the tool calls', async () => {
  const w = await world({
    chat: ({ res }) => {
      res.writeHead(200, { 'Content-Type': 'text/event-stream' });
      frame(res, { dlne_queue: { position: 1 } });
      frame(res, swarm.chunk({ content: 'text' }));
      res.end('data: [DONE]\n\n');
    },
  });
  try {
    const { out, result } = await ask(w.client);
    assert.equal(out, 'text');
    assert.equal(result.content, 'text');
    assert.deepEqual(result.toolCalls, []);
  } finally {
    w.close();
  }
});

// ---- channel 2: a 503 with the place in line -----------------------------------------------------------------------

test('a 503 with dlne_queue is "busy": the place is shown, the request is asked again holding its ticket', async () => {
  let n = 0;
  const w = await world({
    chat: ({ res }) => {
      n++;
      if (n === 1) return swarm.json(res, 503, { detail: 'busy', dlne_queue: { position: 2, waiting: 2, slots: 1, in_use: 1, ticket: 'tkt-1' } }, { 'Retry-After': '1' });
      if (n === 2) return swarm.json(res, 503, { detail: 'busy', dlne_queue: { position: 1, waiting: 1, slots: 1, in_use: 1, ticket: 'tkt-2' } }, { 'Retry-After': '1' });
      return swarm.plainStream(res, ['finally']);
    },
  });
  try {
    const started = Date.now();
    const { out, updates } = await ask(w.client);
    assert.equal(out, 'finally');
    assert.ok(Date.now() - started >= 1900, 'it waited out the two Retry-After pauses');
    assert.deepEqual(updates.map((u) => (u ? u.position : null)), [2, 1, null]);
    assert.equal(updates[0].source, 'response');
    const chats = w.c.of('/v1/chat/completions');
    assert.equal(chats.length, 3);
    assert.equal(chats[0].headers['x-dlne-ticket'], undefined);
    assert.equal(chats[1].headers['x-dlne-ticket'], 'tkt-1');
    assert.equal(chats[2].headers['x-dlne-ticket'], 'tkt-2');
    assert.equal(w.client.currentQueue(), null);
  } finally {
    w.close();
  }
});

test('Stop takes effect during a busy pause', async () => {
  const w = await world({
    chat: ({ res }) => swarm.json(res, 503, { detail: 'busy', dlne_queue: { position: 5 } }, { 'Retry-After': '10' }),
  });
  try {
    const controller = new AbortController();
    setTimeout(() => controller.abort(), 150);
    const started = Date.now();
    const { out, result } = await ask(w.client, { signal: controller.signal });
    assert.equal(out, '');
    assert.deepEqual(result.toolCalls, []);
    assert.ok(Date.now() - started < 2000);
    assert.equal(w.client.currentQueue(), null);
  } finally {
    w.close();
  }
});

test('a request that stays busy for too long gives up with a message, not a silent wait', async () => {
  const w = await world({
    chat: ({ res }) => swarm.json(res, 503, { detail: 'busy', dlne_queue: { position: 9, waiting: 9, slots: 2, in_use: 2 } }, { 'Retry-After': '1' }),
  });
  const real = DlneClient.busyGiveUpMs;
  DlneClient.busyGiveUpMs = 1500;
  try {
    await assert.rejects(ask(w.client), (err) => err instanceof ServerRefusal && err.status === 503 && /stayed busy/.test(err.message) && /lend your GPU/.test(err.message));
    assert.equal(w.client.currentQueue(), null);
  } finally {
    DlneClient.busyGiveUpMs = real;
    w.close();
  }
});

test('a 429 is "busy" only with a dlne_queue; a CPU contributor\'s usage-window 429 is still a refusal', async () => {
  const w = await world({
    chat: ({ res }) => swarm.json(res, 429, { detail: 'You\'ve used all 300 coding chats. More frees up in 42 minutes.' }),
  });
  try {
    await assert.rejects(ask(w.client), (err) => err instanceof ServerRefusal && err.status === 429 && /42 minutes/.test(err.message));
    assert.equal(w.c.of('/v1/chat/completions').length, 1, 'not retried');
  } finally {
    w.close();
  }
  let n = 0;
  const busy = await world({
    chat: ({ res }) => (++n === 1 ? swarm.json(res, 429, { detail: 'busy', dlne_queue: { position: 1 } }, { 'Retry-After': '1' }) : swarm.plainStream(res, ['now'])),
  });
  try {
    assert.equal((await ask(busy.client)).out, 'now');
  } finally {
    busy.close();
  }
});

test('a 503 without dlne_queue is still "the model is loading": waited out and tried again', async () => {
  let n = 0;
  const w = await world({
    chat: ({ res }) => (++n === 1 ? swarm.json(res, 503, { detail: 'Engine not ready' }) : swarm.plainStream(res, ['loaded'])),
  });
  try {
    const { out, updates } = await ask(w.client);
    assert.equal(out, 'loaded');
    assert.deepEqual(updates, []);
  } finally {
    w.close();
  }
});

// ---- channel 3: today's coordinator says nothing --------------------------------------------------------------------

function holdThenAnswer(ms, pieces = ['at last']) {
  return async ({ res }) => {
    await sleep(ms);
    swarm.plainStream(res, pieces);
  };
}

async function withFastWatcher(fn) {
  const real = DlneClient.waitTiming;
  DlneClient.waitTiming = { firstMs: 80, everyMs: 120 };
  try {
    return await fn();
  } finally {
    DlneClient.waitTiming = real;
  }
}

test('a coordinator that says nothing: the place in line is worked out from its status, and updated', async () => {
  let in_queue = { slots: 2, in_use: 2, waiting: 4, waiting_by_priority: { 0: 1, 2: 3 } };
  const w = await world({ status: () => swarm.statusBody({ coding_queue: in_queue }), chat: holdThenAnswer(700) });
  try {
    await withFastWatcher(async () => {
      setTimeout(() => { in_queue = { slots: 2, in_use: 2, waiting: 3, waiting_by_priority: { 0: 1, 2: 2 } }; }, 350);
      const { out, updates } = await ask(w.client);
      assert.equal(out, 'at last');
      const shown = updates.filter(Boolean);
      assert.ok(shown.length >= 2, `updates: ${JSON.stringify(updates)}`);
      assert.ok(shown.every((u) => u.source === 'status' && u.slots === 2));
      assert.equal(shown[0].position, 4, 'a free-trial request behind 1 GPU contributor and 2 earlier trial users');
      assert.equal(shown[shown.length - 1].position, 3, 'one person ahead was served');
      assert.equal(updates[updates.length - 1], null);
    });
  } finally {
    w.close();
  }
});

test('a GPU contributor\'s place is worked out as first class', async () => {
  const w = await world({
    status: swarm.statusBody({ coding_queue: { slots: 2, in_use: 2, waiting: 4, waiting_by_priority: { 0: 1, 2: 3 } } }),
    chat: holdThenAnswer(400),
  });
  try {
    w.client.setWorkerManager({ isContributing: () => true, getStatus: () => ({ cpuPool: false }) });
    await withFastWatcher(async () => {
      const { updates } = await ask(w.client);
      const first = updates.find(Boolean);
      assert.equal(first.position, 1);
      assert.equal(first.priority, 0);
    });
  } finally {
    w.close();
  }
});

test('when the swarm is not busy the wait is still explained, not silent', async () => {
  const w = await world({
    status: swarm.statusBody({ coding_queue: { slots: 2, in_use: 1, waiting: 0, waiting_by_priority: {} } }),
    chat: holdThenAnswer(450),
  });
  try {
    await withFastWatcher(async () => {
      const { progress, updates } = await ask(w.client);
      assert.ok(progress.some((line) => /Waiting for the swarm to start your answer \(\d+ s\)/.test(line)), progress.join('|'));
      assert.deepEqual(updates.filter(Boolean), []);
    });
  } finally {
    w.close();
  }
});

test('a status that cannot be read does not break the wait', async () => {
  const w = await world({ status: null, chat: holdThenAnswer(450) });
  try {
    await withFastWatcher(async () => {
      const { out, progress } = await ask(w.client);
      assert.equal(out, 'at last');
      assert.ok(progress.some((line) => /Waiting for the swarm/.test(line)));
    });
  } finally {
    w.close();
  }
});

test('the watcher stops asking once the coordinator speaks for itself', async () => {
  let asked = 0;
  const updates = [];
  const watcher = new WaitWatcher({
    fetchQueue: async () => { asked++; return null; },
    priority: 2,
    queue: (u) => updates.push(u),
    timing: { firstMs: 30, everyMs: 30 },
  });
  watcher.start();
  watcher.report({ position: 2 }, 'stream');
  await sleep(150);
  watcher.stop();
  assert.equal(asked, 0);
  assert.equal(updates.length, 1);
  assert.equal(updates[0].source, 'stream');
});

test('a stopped watcher says nothing more', async () => {
  const lines = [];
  const watcher = new WaitWatcher({ fetchQueue: async () => null, priority: 2, progress: (l) => lines.push(l), queue() {}, timing: { firstMs: 20, everyMs: 20 } });
  watcher.start();
  watcher.stop();
  await sleep(100);
  assert.deepEqual(lines, []);
});

// ---- what the user sees ------------------------------------------------------------------------------------------------

test('the status bar shows the place in line while a request waits, and goes back when it is served', async () => {
  const w = await world({
    status: swarm.statusBody({ coding_queue: { slots: 2, in_use: 2, waiting: 3, waiting_by_priority: { 2: 3 } } }),
    chat: holdThenAnswer(500),
  });
  try {
    const bar = new ClusterStatusBar(w.client, { onStatusChange() {}, getStatus: () => ({ state: 'idle', gpu: { name: 'GPU', api: 'vulkan' }, vramContributedGb: 0, cpuPool: false }) });
    const item = stub.state.statusBarItems[0];
    await bar.updateStatus();
    assert.match(item.text, /served\.gguf/);
    await withFastWatcher(async () => {
      const pending = ask(w.client);
      await sleep(300);
      assert.match(item.text, /\$\(clock\) DLNE: ~#3 in line/);
      assert.match(item.tooltip, /All 2 coding slots are busy/);
      await bar.updateStatus(); // the 10 s poll must not wipe it
      assert.match(item.text, /in line/);
      await pending;
      await sleep(100);
      assert.match(item.text, /served\.gguf/);
      assert.doesNotMatch(item.text, /in line/);
    });
    bar.dispose();
  } finally {
    w.close();
  }
});

test('the sidebar gets the place in line for its banner, and a null to take it down', async () => {
  stub.reset();
  const client = {
    streamChat: async (_m, _t, onDelta, _s, _sig, _p, options) => {
      options.onQueue({ source: 'response', position: 3, waiting: 5, slots: 2, waitedMs: 0, priority: 2 });
      options.onQueue(null);
      onDelta('answer');
      return { content: 'answer', toolCalls: [] };
    },
  };
  const posted = [];
  const provider = new SidebarProvider({ fsPath: path.join(__dirname, '..') }, client, { onStatusChange() {}, getStatus: () => ({ state: 'idle', gpu: { name: 'GPU', api: 'vulkan', vramTotalGb: 8 }, vramContributedGb: 0, cpuPool: false }) });
  provider.resolveWebviewView(
    { webview: { options: {}, html: '', asWebviewUri: (u) => u, postMessage: (m) => posted.push(m), onDidReceiveMessage() {} } },
    {},
    {}
  );
  await provider.handleUserMessage('hello');
  const queues = posted.filter((m) => m.type === 'queue');
  assert.equal(queues.length, 2);
  assert.equal(queues[0].queue.headline, 'All 2 coding slots are busy. You are #3 in line.');
  assert.match(queues[0].queue.detail, /5 waiting/);
  assert.equal(queues[1].queue, null);
  assert.match(provider._getHtmlForWebview(), /id="queue-banner"/);
});
