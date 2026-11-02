'use strict';
/**
 * Voice in the DLNE Agent panel (docs/voice-contract.md, extension 1.7.0), through the real SidebarProvider and the real
 * page script, with a stand-in recorder: the mic button's visibility (capability, setting, recorder), the click that
 * starts and stops, the timer, the one-time note, the text put in the box at the cursor and never sent, the words for
 * every refusal, the request the client sends, and the manifest's command, keybinding and setting.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');

const stub = require('./helpers/vscode-stub');
const { openView, scriptedClient, say, until } = require('./helpers/sidebar-page');
const { SidebarProvider } = require('../dist/views/sidebarProvider');
const { DlneClient } = require('../dist/client/dlneClient');
const voice = require('../dist/voice/voice');

process.env.HOME = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-voice-panel-home-'));
const ROOT = path.join(__dirname, '..');
const WAV = Buffer.concat([Buffer.from('RIFF'), Buffer.alloc(40, 1)]);
const IDLE = { state: 'idle', gpu: { hasGpu: true, name: 'GPU', api: 'vulkan', vramTotalGb: 12 }, vramContributedGb: 0, cpuPool: false };

/** A recorder that records nothing: `found`, what start was asked, and what stop hands back. */
function fakeRecorder({ found = true, startError, stopError } = {}) {
  const r = {
    found, started: [], stops: 0, cancels: 0, autoStop: null,
    available: () => r.found,
    missingHint: () => 'Install arecord, please.',
    async start(max, onAuto) { r.started.push(max); r.autoStop = onAuto; if (startError) throw new Error(startError); },
    async stop() { r.stops++; if (stopError) throw new Error(stopError); return WAV; },
    async cancel() { r.cancels++; },
  };
  return r;
}

async function withPanel(options, body) {
  stub.reset();
  Object.assign(stub.state.settings, options.settings || {});
  const world = { status: options.status === undefined ? { capabilities: { voice: true }, voice: { max_seconds: 30 } } : options.status, listeners: [], sent: [], reply: options.reply || { status: 200, body: { text: 'make the button blue' } } };
  const recorder = options.recorder || fakeRecorder();
  const client = Object.assign(scriptedClient([say('x')]), {
    lastKnownClusterStatus: () => world.status,
    onClusterStatus: (fn) => { world.listeners.push(fn); return () => {}; },
    transcribe: async (wav) => { world.sent.push(wav); return world.reply; },
  });
  world.fire = () => world.listeners.forEach((fn) => fn(world.status));
  const memento = new Map();
  const global = new Map(options.noted ? [['dlne.voice.noted', true]] : []);
  const context = {
    extensionUri: { fsPath: ROOT },
    storageUri: { fsPath: fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-voice-panel-store-')) },
    subscriptions: [],
    workspaceState: { get: (k) => memento.get(k), update: async (k, v) => { memento.set(k, v); } },
    globalState: { get: (k) => global.get(k), update: async (k, v) => { global.set(k, v); } },
  };
  const workerManager = { onStatusChange() {}, getStatus: () => IDLE };
  const provider = new SidebarProvider(context.extensionUri, client, workerManager, context, recorder);
  const view = openView(provider);
  view.mount();
  try {
    await view.idle();
    await new Promise((resolve) => setImmediate(resolve));
    return await body({ view, page: view.page, world, recorder, provider, global });
  } finally {
    fs.rmSync(context.storageUri.fsPath, { recursive: true, force: true });
  }
}

const shown = (page) => page.el('mic-btn').style.display !== 'none';
const hint = (page) => page.el('voice-hint').textContent;

// ---- the button ------------------------------------------------------------------------------------------------

test('the mic is shown only while the server offers voice, the setting is on, and a recorder exists', async () => {
  const cases = [
    { status: { capabilities: { voice: true } }, setting: true, found: true, want: true },
    { status: { capabilities: { voice: false } }, setting: true, found: true, want: false },
    { status: { capabilities: {} }, setting: true, found: true, want: false },
    { status: null, setting: true, found: true, want: false },
    { status: { capabilities: { voice: true } }, setting: false, found: true, want: false },
    { status: { capabilities: { voice: true } }, setting: true, found: false, want: false },
  ];
  for (const c of cases) {
    await withPanel({ status: c.status, settings: { 'dlne.voice.enabled': c.setting }, recorder: fakeRecorder({ found: c.found }) }, async ({ page }) => {
      assert.equal(shown(page), c.want, JSON.stringify(c));
    });
  }
  assert.equal(voice.micShown({ capabilities: { voice: true } }, true, true), true);
  assert.equal(voice.micShown({ capabilities: { voice: 'yes' } }, true, true), false, 'only a true counts');
});

test('the mic follows the status and the setting while the panel is open', async () => {
  await withPanel({ status: { capabilities: { voice: false } } }, async ({ page, world }) => {
    assert.equal(shown(page), false);
    world.status = { capabilities: { voice: true } };
    world.fire();
    assert.equal(shown(page), true, 'the server turned voice on');
    stub.state.settings['dlne.voice.enabled'] = false;
    stub.state.configListeners.forEach((fn) => fn({ affectsConfiguration: (k) => k === 'dlne.voice.enabled' }));
    assert.equal(shown(page), false, 'the setting hid it');
    world.status = { capabilities: { voice: false } };
    world.fire();
    assert.equal(shown(page), false);
  });
});

// ---- recording ---------------------------------------------------------------------------------------------------

test('a click starts recording for the status\'s max_seconds with a visible timer; the next click stops, sends, and puts the text in the box', async () => {
  await withPanel({}, async ({ page, view, world, recorder }) => {
    page.el('mic-btn').click();
    await view.idle();
    assert.deepEqual(recorder.started, [30], 'the longest clip is the status\'s voice.max_seconds');
    assert.match(page.el('mic-btn').textContent, /0:00 \/ 0:30/, 'a timer with the limit');
    assert.equal(page.el('mic-btn').getAttribute('aria-pressed'), 'true');

    page.el('mic-btn').click();
    await view.idle();
    assert.equal(recorder.stops, 1);
    assert.deepEqual(world.sent, [WAV], 'the clip went to the client');
    assert.equal(page.el('prompt-input').value, 'make the button blue');
    assert.equal(page.el('mic-btn').textContent, '🎤');
    assert.equal(page.el('mic-btn').disabled, false);
    assert.equal(hint(page), '', 'the hint is gone');
  });
});

test('the text is never sent: no message goes to the agent, and the box keeps it for the person to read and fix', async () => {
  await withPanel({}, async ({ page, view }) => {
    page.el('mic-btn').click(); await view.idle();
    page.el('mic-btn').click(); await view.idle();
    await new Promise((r) => setTimeout(r, 20));
    assert.equal(view.fromPage.some((m) => m.type === 'sendMessage'), false);
    assert.equal(view.posted('appendUserMessage').length, 0);
    assert.equal(page.el('prompt-input').value, 'make the button blue');
  });
});

test('the text lands at the cursor, keeping a space on each side; a selection is replaced', async () => {
  await withPanel({}, async ({ page, view }) => {
    const box = page.el('prompt-input');
    const speak = async () => { page.el('mic-btn').click(); await view.idle(); page.el('mic-btn').click(); await view.idle(); };
    box.value = 'please now'; box.selectionStart = 6; box.selectionEnd = 6;
    await speak();
    assert.equal(box.value, 'please make the button blue now', 'cursor after "please ": one space each side');
    box.value = 'fix this'; box.selectionStart = 8; box.selectionEnd = 8;
    await speak();
    assert.equal(box.value, 'fix this make the button blue');
    box.value = 'AAA bbb CCC'; box.selectionStart = 4; box.selectionEnd = 7;
    await speak();
    assert.equal(box.value, 'AAA make the button blue CCC', 'a selected word is replaced');
    box.value = ''; box.selectionStart = 0; box.selectionEnd = 0;
    await speak();
    assert.equal(box.value, 'make the button blue');
  });
});

test('the one-time note says the clip goes to the DLNE server and is not kept, once only', async () => {
  await withPanel({}, async ({ page, view, global }) => {
    page.el('mic-btn').click(); await view.idle();
    assert.match(hint(page), /goes to the DLNE server to be turned into text/);
    assert.match(hint(page), /not kept/);
    assert.equal(global.get('dlne.voice.noted'), true);
    page.el('mic-btn').click(); await view.idle();
    page.el('mic-btn').click(); await view.idle();
    assert.doesNotMatch(hint(page), /not kept/, 'the second recording does not repeat it');
    assert.match(hint(page), /Recording/);
  });
  await withPanel({ noted: true }, async ({ page, view }) => {
    page.el('mic-btn').click(); await view.idle();
    assert.doesNotMatch(hint(page), /not kept/, 'already said in an earlier session');
  });
});

test('the clip ends at max_seconds by itself and is sent without another click', async () => {
  await withPanel({}, async ({ page, view, recorder, world }) => {
    page.el('mic-btn').click(); await view.idle();
    recorder.autoStop(Promise.resolve(WAV));
    await until(() => page.el('prompt-input').value === 'make the button blue', 'the text from the auto-stopped clip');
    assert.deepEqual(world.sent, [WAV]);
    assert.equal(recorder.stops, 0, 'stop() was not asked for: the recorder ended it');
    assert.equal(page.el('mic-btn').textContent, '🎤');
  });
});

test('while a clip is being turned into text a second click changes nothing', async () => {
  const states = [];
  const c = new voice.VoiceController({
    recorder: fakeRecorder(), status: () => ({ capabilities: { voice: true } }), settingOn: () => true,
    transcribe: () => new Promise(() => {}), state: (s) => states.push(s), say() {}, insert() {}, noted: () => true, markNoted() {},
  });
  await c.toggle();
  void c.toggle();
  await new Promise((r) => setImmediate(r));
  assert.equal(c.state, 'sending');
  await c.toggle();
  assert.deepEqual(states, ['recording', 'sending'], 'a click while sending changes nothing');
});

// ---- refusals and errors -----------------------------------------------------------------------------------------

test('every refusal reads in plain words, with the server\'s own sentence where the contract says so', () => {
  const t = (status, body) => voice.voiceErrorText({ status, body });
  assert.equal(t(429, { detail: { code: 'voice_quota', message: 'x' } }), 'Voice is used up for this hour. Try again later.');
  assert.equal(t(429, { code: 'voice_quota' }), 'Voice is used up for this hour. Try again later.');
  assert.equal(t(429, {}), 'Voice is used up for this hour. Try again later.');
  assert.match(t(503, { code: 'voice_busy' }), /busy right now/);
  assert.equal(t(503, { code: 'voice_paused', detail: 'Voice is paused while a lease uses the card.' }), 'Voice is paused while a lease uses the card.');
  assert.equal(t(503, { detail: { code: 'voice_unavailable', message: 'The voice model is not installed.' } }), 'The voice model is not installed.');
  assert.equal(t(503, { error: { code: 'voice_unavailable', message: 'Voice is off on this server.' } }), 'Voice is off on this server.');
  assert.match(t(503, { code: 'voice_paused' }), /paused/);
  assert.match(t(400, { code: 'voice_bad_audio', detail: 'The clip is longer than 60 s.' }), /could not use that recording: The clip is longer than 60 s\./);
  assert.match(t(404, {}), /does not offer voice/);
  assert.match(t(401, {}), /key/);
  assert.match(t(0, { detail: 'connect ECONNREFUSED' }), /Could not reach.*ECONNREFUSED/);
  assert.match(t(500, {}), /answered 500/);
});

test('a refusal from the server shows in the panel as an error and puts nothing in the box', async () => {
  await withPanel({ reply: { status: 429, body: { detail: { code: 'voice_quota', message: 'quota' } } } }, async ({ page, view }) => {
    page.el('mic-btn').click(); await view.idle();
    page.el('mic-btn').click(); await view.idle();
    assert.equal(hint(page), 'Voice is used up for this hour. Try again later.');
    assert.equal(page.el('voice-hint').style.display, 'block');
    assert.equal(page.el('prompt-input').value, '');
    assert.equal(page.el('mic-btn').textContent, '🎤');
  });
});

test('an empty transcript says nothing was heard; a recorder that fails says why', async () => {
  await withPanel({ reply: { status: 200, body: { text: '  ' } } }, async ({ page, view }) => {
    page.el('mic-btn').click(); await view.idle();
    page.el('mic-btn').click(); await view.idle();
    assert.match(hint(page), /Nothing was heard/);
  });
  await withPanel({ recorder: fakeRecorder({ stopError: 'Nothing was recorded.' }) }, async ({ page, view, world }) => {
    page.el('mic-btn').click(); await view.idle();
    page.el('mic-btn').click(); await view.idle();
    assert.equal(hint(page), 'Nothing was recorded.');
    assert.equal(world.sent.length, 0, 'nothing was sent');
  });
  await withPanel({ recorder: fakeRecorder({ startError: 'The microphone could not be opened (arecord).' }) }, async ({ page, view }) => {
    page.el('mic-btn').click(); await view.idle();
    assert.match(hint(page), /microphone could not be opened/);
    assert.equal(page.el('mic-btn').textContent, '🎤');
  });
});

test('the keybinding with no recorder, voice off or the setting off says what is wrong instead of failing silently', async () => {
  await withPanel({ recorder: fakeRecorder({ found: false }) }, async ({ page, view, recorder }) => {
    view.toHost({ type: 'voiceToggle' }); await view.idle();
    assert.equal(hint(page), 'Install arecord, please.');
    assert.equal(recorder.started.length, 0);
  });
  await withPanel({ status: { capabilities: {} } }, async ({ page, view, recorder }) => {
    view.toHost({ type: 'voiceToggle' }); await view.idle();
    assert.match(hint(page), /does not offer voice/);
    assert.equal(recorder.started.length, 0);
  });
  await withPanel({ settings: { 'dlne.voice.enabled': false } }, async ({ page, view, recorder }) => {
    view.toHost({ type: 'voiceToggle' }); await view.idle();
    assert.match(hint(page), /turned off/);
    assert.equal(recorder.started.length, 0);
  });
});

test('closing the panel while recording throws the recording away', async () => {
  await withPanel({}, async ({ page, view, recorder }) => {
    page.el('mic-btn').click(); await view.idle();
    view.dispose();
    await new Promise((r) => setImmediate(r));
    assert.equal(recorder.cancels, 1);
  });
});

test('the page puts the words in with textContent and a value, never as markup', async () => {
  await withPanel({ reply: { status: 200, body: { text: '<img src=x onerror=alert(1)>' } } }, async ({ page, view }) => {
    page.el('mic-btn').click(); await view.idle();
    page.el('mic-btn').click(); await view.idle();
    assert.equal(page.el('prompt-input').value, '<img src=x onerror=alert(1)>');
    assert.equal(page.el('prompt-input').ownHtml, '');
  });
  const html = await new Promise((resolve) => {
    stub.reset();
    const provider = new SidebarProvider({ fsPath: ROOT }, Object.assign(scriptedClient([say('x')]), { lastKnownClusterStatus: () => ({ capabilities: { voice: true } }) }), { onStatusChange() {}, getStatus: () => IDLE }, undefined, fakeRecorder());
    const v = openView(provider);
    v.mount();
    resolve(v.webview.html);
  });
  assert.match(html, /Content-Security-Policy" content="default-src 'none'/);
  assert.match(html, /id="mic-btn"/);
  assert.doesNotMatch(html.match(/<script[\s\S]*<\/script>/)[0], /voiceHint\.innerHTML|promptInput\.innerHTML/);
});

// ---- the request -------------------------------------------------------------------------------------------------

test('the client POSTs the WAV as multipart to /v1/audio/transcriptions with the chat headers', async () => {
  const seen = [];
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      seen.push({ method: req.method, url: req.url, headers: req.headers, body: Buffer.concat(chunks) });
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ text: 'hello there', language: 'en', duration_seconds: 1.2 }));
    });
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  try {
    stub.reset();
    stub.state.settings['dlne.serverUrl'] = `http://127.0.0.1:${server.address().port}`;
    stub.state.settings['dlne.apiKey'] = 'key-123';
    const reply = await new DlneClient().transcribe(WAV);
    assert.equal(reply.status, 200);
    assert.equal(reply.body.text, 'hello there');
    assert.deepEqual(voice.transcriptFromReply(reply), { text: 'hello there' });
    const r = seen[0];
    assert.equal(r.method, 'POST');
    assert.equal(r.url, '/v1/audio/transcriptions');
    assert.equal(r.headers.authorization, 'Bearer key-123', 'the same auth as chat');
    const boundary = /^multipart\/form-data; boundary=(.+)$/.exec(r.headers['content-type'])[1];
    assert.equal(Number(r.headers['content-length']), r.body.length);
    const text = r.body.toString('latin1');
    assert.ok(text.startsWith(`--${boundary}\r\n`));
    assert.ok(text.endsWith(`\r\n--${boundary}--\r\n`));
    assert.match(text, /name="model"\r\n\r\nwhisper-1\r\n/);
    assert.match(text, /name="response_format"\r\n\r\njson\r\n/);
    assert.match(text, /name="file"; filename="voice\.wav"\r\nContent-Type: audio\/wav\r\n\r\n/);
    assert.ok(r.body.includes(WAV), 'the WAV bytes are in the body, unchanged');
  } finally {
    server.closeAllConnections(); server.close();
  }
});

test('the client turns a refusal into a reply and an unreachable server into status 0, never a throw', async () => {
  const server = http.createServer((req, res) => {
    req.resume();
    res.writeHead(429, { 'Content-Type': 'application/json', 'Retry-After': '30' });
    res.end(JSON.stringify({ detail: { code: 'voice_quota', message: 'quota' } }));
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const port = server.address().port;
  try {
    stub.reset();
    stub.state.settings['dlne.serverUrl'] = `http://127.0.0.1:${port}`;
    const reply = await new DlneClient().transcribe(WAV);
    assert.equal(reply.status, 429);
    assert.equal(reply.retryAfter, 30);
    assert.equal(voice.voiceErrorText(reply), 'Voice is used up for this hour. Try again later.');
  } finally {
    server.closeAllConnections(); server.close();
  }
  await new Promise((r) => server.once('close', r));
  const down = await new DlneClient().transcribe(WAV);
  assert.equal(down.status, 0);
  assert.match(voice.voiceErrorText(down), /Could not reach/);
});

test('the request builder: boundary, fields and file, byte for byte', () => {
  const { body, contentType } = voice.transcriptionRequest(WAV, 'BND');
  assert.equal(contentType, 'multipart/form-data; boundary=BND');
  const expected = Buffer.concat([
    Buffer.from('--BND\r\nContent-Disposition: form-data; name="model"\r\n\r\nwhisper-1\r\n'),
    Buffer.from('--BND\r\nContent-Disposition: form-data; name="response_format"\r\n\r\njson\r\n'),
    Buffer.from('--BND\r\nContent-Disposition: form-data; name="file"; filename="voice.wav"\r\nContent-Type: audio/wav\r\n\r\n'),
    WAV,
    Buffer.from('\r\n--BND--\r\n'),
  ]);
  assert.deepEqual(body, expected);
});

test('max_seconds comes from the status, with the contract\'s 60 as the default', () => {
  assert.equal(voice.voiceMaxSeconds({ voice: { max_seconds: 45 } }), 45);
  assert.equal(voice.voiceMaxSeconds({}), 60);
  assert.equal(voice.voiceMaxSeconds(null), 60);
  assert.equal(voice.voiceMaxSeconds({ voice: { max_seconds: 'many' } }), 60);
  assert.equal(voice.voiceMaxSeconds({ voice: { max_seconds: 0 } }), 60);
});

// ---- the manifest ------------------------------------------------------------------------------------------------

test('package.json: 1.8.0, the setting (application scope, default true), the command and its keybinding', () => {
  const pkg = JSON.parse(fs.readFileSync(path.join(ROOT, 'package.json'), 'utf8'));
  assert.equal(pkg.version, '1.8.0');
  const setting = pkg.contributes.configuration.properties['dlne.voice.enabled'];
  assert.equal(setting.type, 'boolean');
  assert.equal(setting.default, true);
  assert.equal(setting.scope, 'application');
  assert.ok(setting.description && !/[*`]/.test(setting.description), 'a plain description');
  assert.ok(pkg.contributes.commands.some((c) => c.command === 'dlne.voice.toggle'));
  const key = pkg.contributes.keybindings.find((k) => k.command === 'dlne.voice.toggle');
  assert.equal(key.key, 'ctrl+alt+v');
  assert.equal(key.mac, 'cmd+alt+v');
  assert.match(key.when, /focusedView == dlne\.chatView/);
  assert.match(key.when, /editorTextFocus/);
  const lock = JSON.parse(fs.readFileSync(path.join(ROOT, 'package-lock.json'), 'utf8'));
  assert.equal(lock.version, '1.8.0');
});
