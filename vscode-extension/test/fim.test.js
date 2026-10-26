'use strict';
/**
 * Autocomplete latency: what the extension sends, how often, and over which connection.
 * A completion pays for every prompt token before its first output token, and every keystroke
 * burst used to open a new TCP connection and could be blocked by a stale request in flight.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const stub = require('./helpers/vscode-stub');
const { DlneClient } = require('../dist/client/dlneClient');
const { DlneFimProvider, buildFimContext } = require('../dist/completion/fimProvider');

process.env.HOME = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-fim-home-'));

/** A minimal TextDocument over `text`. */
function doc(text) {
  const lines = text.split('\n');
  const offsetOf = (p) => lines.slice(0, p.line).reduce((n, l) => n + l.length + 1, 0) + p.character;
  return {
    uri: { scheme: 'file' },
    lineCount: lines.length,
    lineAt: (i) => ({ text: lines[i] }),
    getText: (range) => text.slice(offsetOf(range.start), offsetOf(range.end)),
  };
}

const pos = (line, character) => new stub.vscode.Position(line, character);
const token = () => {
  const listeners = [];
  return {
    isCancellationRequested: false,
    onCancellationRequested(fn) { listeners.push(fn); return { dispose() {} }; },
    cancel() { this.isCancellationRequested = true; listeners.forEach((fn) => fn()); },
  };
};

// --- the prompt -----------------------------------------------------------------------------

test('the prefix is 50 to 57 lines (its first line moves in blocks of 8) and the suffix the next 10', () => {
  const text = Array.from({ length: 200 }, (_, i) => `line ${i}`).join('\n');
  const { prefix, suffix } = buildFimContext(doc(text), pos(100, 0));
  assert.equal(prefix.split('\n')[0], 'line 48');
  assert.equal(prefix.split('\n').length, 53);
  assert.equal(suffix.split('\n').pop(), 'line 110');
});

test('the front of the prompt holds still while lines are typed, so the model can reuse what it read', () => {
  const text = Array.from({ length: 300 }, (_, i) => `line ${i}`).join('\n');
  const firstLine = (line) => buildFimContext(doc(text), pos(line, 0)).prefix.split('\n')[0];
  const firsts = new Set();
  for (let line = 202; line < 210; line++) firsts.add(firstLine(line));
  assert.equal(firsts.size, 1);                                    // eight lines typed, one front (50..57 lines above the cursor)
  assert.notEqual(firstLine(202), firstLine(218));                 // it does move, in steps of 8
  for (let line = 120; line < 220; line++) {
    const n = buildFimContext(doc(text), pos(line, 0)).prefix.split('\n').length - 1;
    assert.ok(n >= 50 && n < 58, `line ${line} sends ${n} lines above the cursor`);
  }
});

test('over the character budget whole blocks are dropped from the top, so the front still moves in steps', () => {
  const fat = Array.from({ length: 300 }, (_, i) => `${'x'.repeat(150)} ${String(i).padStart(3, '0')}`).join('\n');
  const first = (line) => buildFimContext(doc(fat), pos(line, 154)).prefix.split('\n')[0].slice(-3);
  const fronts = new Set();
  for (let line = 200; line < 208; line++) fronts.add(first(line));
  assert.ok(fronts.size <= 2, `${[...fronts]}`);
  assert.ok(buildFimContext(doc(fat), pos(207, 154)).prefix.length <= 4000);
});

test('very long lines are capped by characters too', () => {
  const long = 'x'.repeat(10000);
  const { prefix, suffix } = buildFimContext(doc(`${long}\n${long}`), pos(1, 5000));
  assert.equal(prefix.length, 4000);
  assert.equal(suffix.length, 600);
  assert.ok(prefix.endsWith('x'));
});

test('mid-line is detected from the rest of the cursor line', () => {
  assert.equal(buildFimContext(doc('foo(bar)'), pos(0, 4)).midLine, true);
  assert.equal(buildFimContext(doc('foo(   \nnext'), pos(0, 4)).midLine, false);
});

// --- the connection -------------------------------------------------------------------------

async function coordinator(handler) {
  const sockets = new Set();
  const requests = [];
  const server = http.createServer((req, res) => {
    let body = '';
    req.on('data', (c) => (body += c));
    req.on('end', () => {
      requests.push({ body: JSON.parse(body || '{}'), socket: req.socket });
      handler(req, res, requests.length);
    });
  });
  server.on('connection', (s) => sockets.add(s));
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  stub.state.settings['dlne.serverUrl'] = `http://127.0.0.1:${server.address().port}`;
  return {
    sockets, requests,
    close() { server.closeAllConnections(); server.close(); },
  };
}

const reply = (res, text = 'done') => {
  res.writeHead(200, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify({ choices: [{ text }] }));
};

test('consecutive completions reuse one connection', async () => {
  stub.reset();
  const c = await coordinator((req, res) => reply(res));
  try {
    const client = new DlneClient();
    for (let i = 0; i < 3; i++) assert.equal(await client.getCompletion('p'), 'done');
    assert.equal(c.sockets.size, 1);
  } finally {
    c.close();
  }
});

test('a pooled socket the server dropped is retried once on a fresh one', async () => {
  stub.reset();
  const c = await coordinator((req, res, n) => (n === 2 ? req.socket.destroy() : reply(res, `answer ${n}`)));
  try {
    const client = new DlneClient();
    assert.equal(await client.getCompletion('p'), 'answer 1');
    assert.equal(await client.getCompletion('p'), 'answer 3');   // request 2 died on the reused socket
    assert.equal(c.requests.length, 3);
  } finally {
    c.close();
  }
});

test('a failure on a fresh socket is not retried', async () => {
  stub.reset();
  const c = await coordinator((req) => req.socket.destroy());
  try {
    assert.equal(await new DlneClient().getCompletion('p'), '');
    assert.equal(c.requests.length, 1);
  } finally {
    c.close();
  }
});

test('an aborted completion returns at once and is not retried', async () => {
  stub.reset();
  const c = await coordinator(() => { /* never answers */ });
  try {
    const controller = new AbortController();
    const started = Date.now();
    const pending = new DlneClient().getCompletion('p', 8, [], controller.signal);
    setTimeout(() => controller.abort(), 50);
    assert.equal(await pending, '');
    assert.ok(Date.now() - started < 2000);
    assert.equal(c.requests.length, 1);
  } finally {
    c.close();
  }
});

// --- the provider ---------------------------------------------------------------------------

test('the configured delay is honoured (it was clamped to at least 450 ms)', async () => {
  stub.reset();
  stub.state.settings['dlne.inlineCompletionDelay'] = 100;
  const c = await coordinator((req, res) => reply(res, 'x'));
  try {
    const started = Date.now();
    const items = await new DlneFimProvider(new DlneClient()).provideInlineCompletionItems(doc('def f():\n    '), pos(1, 4), {}, token());
    assert.equal(items[0].insertText, 'x');
    assert.ok(Date.now() - started < 400, `took ${Date.now() - started} ms`);
  } finally {
    c.close();
  }
});

test('the newest request wins: a stale one is aborted instead of blocking', async () => {
  stub.reset();
  stub.state.settings['dlne.inlineCompletionDelay'] = 75;
  const c = await coordinator((req, res, n) => (n === 1 ? undefined : reply(res, 'fresh')));  // the first hangs
  try {
    const provider = new DlneFimProvider(new DlneClient());
    const first = provider.provideInlineCompletionItems(doc('a = 1\nb'), pos(1, 1), {}, token());
    await new Promise((r) => setTimeout(r, 150));                  // first is now in flight
    const second = await provider.provideInlineCompletionItems(doc('a = 1\nbc'), pos(1, 2), {}, token());
    assert.equal(second[0].insertText, 'fresh');
    assert.equal(await first, null);
  } finally {
    c.close();
  }
});

test('the request carries the trimmed prompt and a newline stop when mid-line', async () => {
  stub.reset();
  stub.state.settings['dlne.inlineCompletionDelay'] = 75;
  const c = await coordinator((req, res) => reply(res));
  try {
    const provider = new DlneFimProvider(new DlneClient());
    await provider.provideInlineCompletionItems(doc('print(x)'), pos(0, 6), {}, token());
    await provider.provideInlineCompletionItems(doc('x = 1\n'), pos(1, 0), {}, token());
    const [mid, end] = c.requests.map((r) => r.body);
    assert.ok(mid.stop.includes('\n'));
    assert.ok(!end.stop.includes('\n'));
    assert.equal(mid.prompt, '<|fim_prefix|>print(<|fim_suffix|>x)<|fim_middle|>');
    assert.equal(mid.max_tokens, 48);
  } finally {
    c.close();
  }
});

test('cancelling while waiting sends nothing', async () => {
  stub.reset();
  stub.state.settings['dlne.inlineCompletionDelay'] = 300;
  const c = await coordinator((req, res) => reply(res));
  try {
    const t = token();
    const pending = new DlneFimProvider(new DlneClient()).provideInlineCompletionItems(doc('abc'), pos(0, 3), {}, t);
    setTimeout(() => t.cancel(), 50);
    assert.equal(await pending, null);
    assert.equal(c.requests.length, 0);
  } finally {
    c.close();
  }
});
