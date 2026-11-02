'use strict';
/**
 * Conversation mode (docs/voice-contract.md, 2.8.2, extension 1.8.0): the contract's shared test vectors for `speakable`
 * and the sentence split, the streaming reader and its 1,500-character limit, the host-side speaker per platform (what
 * is probed, that nothing runs through a shell, that stop ends only its own child), and the DLNE Agent panel (a spoken
 * message sends itself, only its reply is read, every stop).
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const stub = require('./helpers/vscode-stub');
const { openView, scriptedClient, say, until } = require('./helpers/sidebar-page');
const { SidebarProvider } = require('../dist/views/sidebarProvider');
const speech = require('../dist/voice/speakable');
const { Speaker, detectSpeaker, speakerHint, WINDOWS_SPEAK_SCRIPT } = require('../dist/voice/speaker');
const voice = require('../dist/voice/voice');

process.env.HOME = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-voice-conv-home-'));
const ROOT = path.join(__dirname, '..');
const sentences = (md) => speech.splitSentences(speech.speakable(md));

// ---- the contract's shared vectors ----------------------------------------------------------------------------------

test('shared vectors: speakable and the sentence split give the contract\'s sentences', () => {
  const vectors = [
    ['**Paris** is the capital [1].', ['Paris is the capital.']],
    ['Run this:\n\n```bash\nls -la\n```\n\nDone.', ['Run this:', "There's code in the chat.", 'Done.']],
    ['See [the docs](https://x.test/a) or https://y.test/b.', ['See the docs or.']],
    ['- one\n- two', ['one', 'two']],
    ['It costs 3.5 dollars, e.g. today. Really!', ['It costs 3.5 dollars, e.g. today.', 'Really!']],
    ['| a | b |\n|---|---|\n| 1 | 2 |', ["There's a table in the chat."]],
    ['Use `git status` first.', ['Use git status first.']],
  ];
  for (const [markdown, want] of vectors) assert.deepEqual(sentences(markdown), want, markdown);
});

test('speakable: more of the contract\'s rules', () => {
  assert.deepEqual(sentences('# Title\n> quoted *text* and _more_\n1. first\n2) second'), ['Title', 'quoted text and more', 'first', 'second']);
  assert.deepEqual(sentences('Cited [2][3] and [4, 5].'), ['Cited and.']);
  assert.deepEqual(sentences('```\na\n```\n```\nb\n```'), ["There's code in the chat.", "There's code in the chat."], 'once per block');
  assert.deepEqual(sentences('~~~py\nx = 1\n\n\ny = 2'), ["There's code in the chat."], 'an unfinished block is one block');
  assert.deepEqual(sentences('Ask Dr. Smith, i.e. him. Fine? Yes.'), ['Ask Dr. Smith, i.e. him.', 'Fine?', 'Yes.']);
  assert.deepEqual(sentences('Version 1.8.0 is out. Go!'), ['Version 1.8.0 is out.', 'Go!']);
  assert.deepEqual(sentences(''), []);
  assert.equal(speech.speakable('a   b\t\tc'), 'a b c');
});

// ---- the streaming reader --------------------------------------------------------------------------------------------

test('newSentences: only complete sentences while it streams, the rest at the end', () => {
  assert.deepEqual(speech.newSentences('Hello there', 0, false), []);
  assert.deepEqual(speech.newSentences('Hello there.', 0, false), [], 'a final "." may still become "3.5"');
  assert.deepEqual(speech.newSentences('Hello there. Next', 0, false), ['Hello there.']);
  assert.deepEqual(speech.newSentences('Hello there. Next one.', 1, false), []);
  assert.deepEqual(speech.newSentences('Hello there. Next one.', 1, true), ['Next one.']);
  assert.deepEqual(speech.newSentences('A heading\n\nBody', 0, false), ['A heading'], 'a newline completes a line');
  assert.deepEqual(speech.newSentences('Value 3.', 0, false), []);
});

test('the reader gives every sentence once, however the text is cut into deltas', () => {
  const text = 'First one. Second one!\n\n```js\ncode();\n```\n\n- item a\n- item b\n\nLast words';
  for (const step of [1, 3, 7, 50]) {
    const reader = new speech.TurnReader();
    const said = [];
    for (let i = step; i < text.length + step; i += step) said.push(...reader.feed(text.slice(0, i)));
    said.push(...reader.feed(text, true));
    assert.deepEqual(said, ['First one.', 'Second one!', "There's code in the chat.", 'item a', 'item b', 'Last words'], `step ${step}`);
  }
});

test('tool calls are never read: fenced JSON, tags, finished or still arriving', () => {
  const reader = new speech.TurnReader();
  const said = [];
  said.push(...reader.feed('I will look. ```json\n{"name": "read_file", "arguments": {"path": "a.ts"}}\n```\n'));
  said.push(...reader.feed('I will look. ```json\n{"name": "read_file", "arguments": {"path": "a.ts"}}\n```\nNow the answer. <tool_call>{"name":"x"}', false));
  said.push(...reader.feed('I will look. ```json\n{"name": "read_file", "arguments": {"path": "a.ts"}}\n```\nNow the answer. <tool_call>{"name":"x"}</tool_call>\nDone.', true));
  assert.deepEqual(said, ['I will look.', 'Now the answer.', 'Done.']);
});

test('at most 1,500 characters a turn, then "The rest is in the chat." once', () => {
  const sentence = 'x'.repeat(98) + '.';         // 99 characters
  const reader = new speech.TurnReader();
  const said = reader.feed(Array(30).fill(sentence).join(' '), true);
  assert.equal(said.length, 16, '15 sentences fit (1,485) and then the note');
  assert.equal(said[15], 'The rest is in the chat.');
  assert.ok(said.slice(0, 15).join('').length <= 1500);
  assert.deepEqual(reader.feed(Array(40).fill(sentence).join(' '), true), [], 'nothing more, no second note');
  assert.equal(speech.MAX_SPOKEN_CHARS, 1500);
  assert.deepEqual(new speech.TurnReader().picture(), ["I made a picture. It's in the chat."]);
});

// ---- the speaker -----------------------------------------------------------------------------------------------------

function fakeSpawn() {
  const calls = [];
  const spawn = (command, args, options) => {
    const child = new EventEmitter();
    child.stdin = Object.assign(new EventEmitter(), { written: '', end(t) { this.written += t || ''; } });
    child.stderr = new EventEmitter();
    child.killed = false;
    child.kill = () => { child.killed = true; };
    child.finish = (code = 0) => child.emit('close', code);
    calls.push({ command, args, options, child });
    return child;
  };
  return { spawn, calls };
}

test('which tool is probed on each OS, in order', () => {
  const only = (...have) => (c) => have.includes(c);
  assert.equal(detectSpeaker('linux', only('espeak-ng', 'espeak', 'spd-say')).command, 'espeak-ng');
  assert.equal(detectSpeaker('linux', only('espeak', 'spd-say')).command, 'espeak');
  assert.equal(detectSpeaker('linux', only('spd-say')).command, 'spd-say');
  assert.equal(detectSpeaker('linux', only()), null);
  assert.equal(detectSpeaker('darwin', only('say')).command, 'say');
  assert.equal(detectSpeaker('win32', only('powershell.exe')).command, 'powershell.exe');
  assert.equal(detectSpeaker('freebsd', only('say')), null);
  assert.match(speakerHint('linux'), /espeak-ng/);
  assert.match(speakerHint('darwin'), /say/);
  assert.match(speakerHint('win32'), /PowerShell/);
});

test('the text goes in through stdin (or one argv entry for spd-say), never through a shell', () => {
  const secret = '$(touch /tmp/pwned); `rm -rf` "quoted" & more';
  const cases = [
    ['linux', ['espeak-ng'], 'espeak-ng', ['--stdin'], true],
    ['linux', ['espeak'], 'espeak', ['--stdin'], true],
    ['darwin', ['say'], 'say', ['-f', '-'], true],
  ];
  for (const [platform, have, command, args] of cases) {
    const { spawn, calls } = fakeSpawn();
    const speaker = new Speaker({ platform, has: (c) => have.includes(c), spawn });
    speaker.speak([secret]);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].command, command);
    assert.deepEqual(calls[0].args, args);
    assert.equal(calls[0].options.shell, false);
    assert.equal(calls[0].child.stdin.written, secret);
    assert.ok(!calls[0].args.join(' ').includes('pwned'), 'the text is not in the command line');
  }
  const { spawn, calls } = fakeSpawn();
  new Speaker({ platform: 'linux', has: (c) => c === 'spd-say', spawn }).speak(['--version is a sentence']);
  assert.deepEqual(calls[0].args, ['-w', '--', '--version is a sentence'], 'one argv entry after --');
  assert.equal(calls[0].options.shell, false);
  const win = fakeSpawn();
  new Speaker({ platform: 'win32', has: () => true, spawn: win.spawn }).speak(['Hello & goodbye'], 'de');
  assert.equal(win.calls[0].command, 'powershell.exe');
  assert.equal(win.calls[0].options.shell, false);
  assert.equal(win.calls[0].child.stdin.written, 'Hello & goodbye');
  assert.equal(win.calls[0].options.env.DLNE_VOICE_LANG, 'de');
  const encoded = win.calls[0].args[win.calls[0].args.length - 1];
  assert.equal(Buffer.from(encoded, 'base64').toString('utf16le'), WINDOWS_SPEAK_SCRIPT);
  assert.match(WINDOWS_SPEAK_SCRIPT, /System\.Speech\.Synthesis\.SpeechSynthesizer/);
  assert.match(WINDOWS_SPEAK_SCRIPT, /\[Console\]::In\.ReadToEnd\(\)/);
  assert.ok(!win.calls[0].args.join(' ').includes('goodbye'));
});

test('sentences queue: the next child speaks everything that arrived meanwhile', () => {
  const { spawn, calls } = fakeSpawn();
  const speaker = new Speaker({ platform: 'linux', has: (c) => c === 'espeak-ng', spawn });
  const states = [];
  speaker.onSpeaking((on) => states.push(on));
  speaker.speak(['One.']);
  speaker.speak(['Two.']);
  speaker.speak(['Three.']);
  assert.equal(calls.length, 1, 'one child at a time');
  calls[0].child.finish();
  assert.equal(calls.length, 2);
  assert.equal(calls[1].child.stdin.written, 'Two.\nThree.');
  calls[1].child.finish();
  assert.deepEqual(states, [true, false]);
  assert.equal(speaker.isSpeaking(), false);
});

test('stop ends only the child it spawned, drops the queue, and a late exit changes nothing', () => {
  const { spawn, calls } = fakeSpawn();
  const speaker = new Speaker({ platform: 'linux', has: (c) => c === 'espeak-ng', spawn });
  speaker.speak(['One.']);
  speaker.speak(['Two.']);
  speaker.stop();
  assert.equal(calls[0].child.killed, true);
  assert.equal(speaker.isSpeaking(), false);
  calls[0].child.finish(null);
  assert.equal(calls.length, 1, 'the queued sentence was dropped');
  speaker.speak(['Fresh.']);
  assert.equal(calls.length, 2);
  assert.equal(calls[1].child.killed, false);
});

test('spd-say: stop also asks speech-dispatcher to cancel', () => {
  const { spawn, calls } = fakeSpawn();
  const speaker = new Speaker({ platform: 'linux', has: (c) => c === 'spd-say', spawn });
  speaker.speak(['Hi.']);
  speaker.stop();
  assert.equal(calls[0].child.killed, true);
  assert.deepEqual(calls[1].args, ['-C']);
});

test('a tool that fails or cannot start is reported in words, once, and nothing keeps running', () => {
  const { spawn, calls } = fakeSpawn();
  const speaker = new Speaker({ platform: 'linux', has: (c) => c === 'espeak-ng', spawn });
  const errors = [];
  speaker.onError((m) => errors.push(m));
  speaker.speak(['Hi.']);
  calls[0].child.stderr.emit('data', Buffer.from('no voices'));
  calls[0].child.finish(2);
  assert.equal(errors.length, 1);
  assert.match(errors[0], /espeak-ng.*no voices/);
  assert.equal(speaker.isSpeaking(), false);
  const none = new Speaker({ platform: 'linux', has: () => false, spawn });
  assert.equal(none.available(), false);
  none.speak(['Hi.']);
  assert.equal(calls.length, 1, 'nothing is spawned without a tool');
});

// ---- the panel -------------------------------------------------------------------------------------------------------

const WAV = Buffer.concat([Buffer.from('RIFF'), Buffer.alloc(40, 1)]);
const IDLE = { state: 'idle', gpu: { hasGpu: true, name: 'GPU', api: 'vulkan', vramTotalGb: 12 }, vramContributedGb: 0, cpuPool: false };

function fakeRecorder() {
  const r = { started: 0, available: () => true, missingHint: () => 'x', async start() { r.started++; }, async stop() { return WAV; }, async cancel() {} };
  return r;
}

async function withPanel(options, body) {
  stub.reset();
  Object.assign(stub.state.settings, { 'dlne.voice.mode': 'conversation' }, options.settings || {});
  const steps = options.steps || [say('Sure thing. All done now.')];
  const client = Object.assign(scriptedClient(steps), {
    lastKnownClusterStatus: () => ({ capabilities: { voice: true } }),
    onClusterStatus: () => () => {},
    transcribe: async () => ({ status: 200, body: { text: options.heard ?? 'make it blue', language: 'en' } }),
  });
  const memento = new Map();
  const context = {
    extensionUri: { fsPath: ROOT },
    storageUri: { fsPath: fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-voice-conv-store-')) },
    subscriptions: [],
    workspaceState: { get: (k) => memento.get(k), update: async (k, v) => { memento.set(k, v); } },
    globalState: { get: () => true, update: async () => {} },
  };
  const spawned = fakeSpawn();
  const speaker = new Speaker({ platform: 'linux', has: (c) => (options.tool === undefined ? c === 'espeak-ng' : c === options.tool), spawn: spawned.spawn });
  const provider = new SidebarProvider(context.extensionUri, client, { onStatusChange() {}, getStatus: () => IDLE }, context, fakeRecorder(), speaker);
  const view = openView(provider);
  view.mount();
  try {
    await view.idle();
    await new Promise((resolve) => setImmediate(resolve));
    return await body({ view, page: view.page, provider, spawned, speaker, client });
  } finally {
    provider.dispose();
    fs.rmSync(context.storageUri.fsPath, { recursive: true, force: true });
  }
}

/** Lets every speech child finish, so what was queued behind it is spoken too. */
const drain = (spawned) => { for (let i = 0; i < spawned.calls.length; i++) spawned.calls[i].child.finish(0); };
const spoke = (spawned) => spawned.calls.filter((c) => c.child.stdin.written).map((c) => c.child.stdin.written);
const talk = async (page, view) => { page.el('mic-btn').click(); await view.idle(); page.el('mic-btn').click(); await view.idle(); };

test('Conversation: a spoken message sends itself and its reply is read aloud, sentence by sentence', async () => {
  await withPanel({}, async ({ page, view, spawned }) => {
    await talk(page, view);
    await until(() => view.posted('turnComplete').length > 0);
    assert.equal(view.fromPage.filter((m) => m.type === 'sendMessage').length, 1);
    assert.equal(view.fromPage.find((m) => m.type === 'sendMessage').text, 'make it blue');
    assert.equal(page.el('prompt-input').value, '', 'the box was sent');
    drain(spawned);
    assert.equal(spoke(spawned).join('|'), 'Sure thing.|All done now.', 'the reply was spoken, sentence by sentence');
  });
});

test('Dictate (the default) still only fills the box, and nothing is spoken', async () => {
  await withPanel({ settings: { 'dlne.voice.mode': 'dictate' } }, async ({ page, view, spawned }) => {
    await talk(page, view);
    await new Promise((r) => setTimeout(r, 20));
    assert.equal(view.fromPage.some((m) => m.type === 'sendMessage'), false);
    assert.equal(page.el('prompt-input').value, 'make it blue');
    assert.equal(spawned.calls.length, 0);
  });
});

test('a typed message is never read aloud', async () => {
  await withPanel({}, async ({ page, view, spawned }) => {
    page.el('prompt-input').value = 'typed question';
    page.el('send-btn').click();
    await until(() => view.posted('turnComplete').length > 0);
    assert.equal(spawned.calls.length, 0);
  });
});

test('when Send is blocked (a task is running) the words stay in the box and nothing is read', async () => {
  const gate = { release: null };
  const hold = async ({ onDelta }) => { await new Promise((r) => { gate.release = r; }); onDelta('Late reply.'); return { content: 'Late reply.', toolCalls: [], finishReason: 'stop' }; };
  await withPanel({ steps: [hold] }, async ({ page, view, spawned }) => {
    page.el('prompt-input').value = 'first';
    page.el('send-btn').click();
    await until(() => view.fromPage.some((m) => m.type === 'sendMessage'));
    // view.idle() would wait for the held task, so the clicks are paced by hand.
    page.el('mic-btn').click();
    await new Promise((r) => setTimeout(r, 30));
    page.el('mic-btn').click();
    await new Promise((r) => setTimeout(r, 60));
    assert.equal(view.fromPage.filter((m) => m.type === 'sendMessage').length, 1, 'no second message');
    assert.equal(page.el('prompt-input').value, 'make it blue', 'the words stay');
    gate.release();
    await until(() => view.posted('turnComplete').length > 0);
    assert.equal(spawned.calls.length, 0);
  });
});

test('"nothing heard" sends nothing', async () => {
  await withPanel({ heard: '   ' }, async ({ page, view }) => {
    await talk(page, view);
    await new Promise((r) => setTimeout(r, 20));
    assert.equal(view.fromPage.some((m) => m.type === 'sendMessage'), false);
  });
});

test('code and tool calls in the reply are not read; code becomes a sentence', async () => {
  const reply = 'Here you go.\n\n```ts\nconst a = 1;\n```\n\nThat is it.';
  await withPanel({ steps: [say(reply)] }, async ({ page, view, spawned }) => {
    await talk(page, view);
    await until(() => view.posted('turnComplete').length > 0);
    drain(spawned);
    const text = spoke(spawned).join('\n');
    assert.match(text, /Here you go\./);
    assert.match(text, /There's code in the chat\./);
    assert.match(text, /That is it\./);
    assert.ok(!text.includes('const a'));
  });
});

test('speaking shows Stop speaking on the mic button, and a click stops speech (only the child it spawned)', async () => {
  await withPanel({}, async ({ page, view, spawned }) => {
    await talk(page, view);
    await until(() => view.posted('turnComplete').length > 0);
    await until(() => view.posted('speaking').some((m) => m.on === true));
    assert.equal(page.el('mic-btn').title, 'Stop speaking');
    assert.equal(page.el('mic-btn').getAttribute('aria-label'), 'Stop speaking');
    page.el('mic-btn').click();
    await view.idle();
    assert.ok(view.fromPage.some((m) => m.type === 'stopSpeaking'));
    assert.equal(spawned.calls[0].child.killed, true);
    assert.equal(view.posted('speaking').at(-1).on, false);
    assert.equal(page.el('mic-btn').textContent, '🎤');
    assert.equal(view.fromPage.filter((m) => m.type === 'voiceToggle').length, 2, 'the stop click was not a recording toggle');
  });
});

test('sending, starting a recording, a new chat, turning Conversation off and dispose all stop speech', async () => {
  const stops = [
    ['sending', async ({ page }) => { page.el('prompt-input').value = 'typed'; page.el('send-btn').click(); }],
    ['recording', async ({ provider }) => { await provider.toggleVoice(); }],
    ['a new chat', async ({ page }) => { page.el('new-session-btn').click(); }],
    ['Conversation off', async () => { stub.state.settings['dlne.voice.mode'] = 'dictate'; stub.state.configListeners.forEach((fn) => fn({ affectsConfiguration: (k) => k === 'dlne.voice.mode' })); }],
    ['deactivate', async ({ provider }) => { provider.dispose(); }],
  ];
  for (const [label, act] of stops) {
    await withPanel({}, async (ctx) => {
      await talk(ctx.page, ctx.view);
      await until(() => ctx.view.posted('turnComplete').length > 0);
      assert.equal(ctx.speaker.isSpeaking(), true, label);
      await act(ctx);
      await ctx.view.idle();
      assert.equal(ctx.speaker.isSpeaking() && !ctx.spawned.calls.every((c) => c.child.killed || false) , false, label);
      assert.equal(ctx.spawned.calls[0].child.killed, true, label);
    });
  }
});

test('no speech tool: one plain message naming what to install, the answer stays text', async () => {
  await withPanel({ tool: 'nothing' }, async ({ page, view, spawned }) => {
    await talk(page, view);
    await until(() => view.posted('turnComplete').length > 0);
    assert.equal(spawned.calls.length, 0);
    const notice = view.posted('voiceNotice').find((m) => /espeak-ng/.test(m.text));
    assert.ok(notice && notice.error === true);
    await talk(page, view);
    await until(() => view.posted('turnComplete').length > 1);
    assert.equal(view.posted('voiceNotice').filter((m) => /espeak-ng/.test(m.text)).length, 1, 'told once');
  });
});

test('package.json: setting dlne.voice.mode and the toggle command', () => {
  const pkg = JSON.parse(fs.readFileSync(path.join(ROOT, 'package.json'), 'utf8'));
  const mode = pkg.contributes.configuration.properties['dlne.voice.mode'];
  assert.deepEqual(mode.enum, ['dictate', 'conversation']);
  assert.equal(mode.default, 'dictate');
  assert.ok(mode.description.length > 80 && mode.enumDescriptions.length === 2);
  const cmd = pkg.contributes.commands.find((c) => c.command === 'dlne.voice.toggleMode');
  assert.equal(cmd.title, 'DLNE: Toggle Voice Conversation Mode');
  assert.equal(voice.voiceModeFrom('conversation'), 'conversation');
  assert.equal(voice.voiceModeFrom('anything'), 'dictate');
  assert.equal(voice.voiceModeFrom(undefined), 'dictate');
});

test('the toggle command flips the setting', async () => {
  await withPanel({ settings: { 'dlne.voice.mode': 'dictate' } }, async ({ provider }) => {
    await provider.toggleVoiceMode();
    assert.equal(stub.state.settings['dlne.voice.mode'], 'conversation');
    await provider.toggleVoiceMode();
    assert.equal(stub.state.settings['dlne.voice.mode'], 'dictate');
  });
});
