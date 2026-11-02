'use strict';
/**
 * Voice, the extension host's recorder (docs/voice-contract.md, "VS Code extension"; src/voice/recorder.ts): which
 * recorder each platform tries and in what order, the exact commands, start / stop / auto-stop, the temp file gone on
 * every path, and the WAV normalised to PCM 16-bit mono 16 kHz. The recorders are stand-ins started through an injected
 * spawn; nothing here touches a microphone.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { PassThrough } = require('node:stream');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const rec = require('../dist/voice/recorder');

// ---- WAV helpers -----------------------------------------------------------------------------------------------

/** A PCM 16-bit WAV of `frames` frames; `sample(frame, channel)` gives each sample. */
function wav({ rate = 16000, channels = 1, frames = 160, sample = (i) => (i % 100) * 100, bits = 16, sizes = true }) {
  const bytes = bits / 8;
  const data = Buffer.alloc(frames * channels * bytes);
  for (let i = 0; i < frames; i++) for (let c = 0; c < channels; c++) data.writeInt16LE(sample(i, c), (i * channels + c) * 2);
  const h = Buffer.alloc(44);
  h.write('RIFF', 0, 'latin1'); h.writeUInt32LE(sizes ? 36 + data.length : 0xffffffff, 4); h.write('WAVE', 8, 'latin1');
  h.write('fmt ', 12, 'latin1'); h.writeUInt32LE(16, 16); h.writeUInt16LE(1, 20); h.writeUInt16LE(channels, 22);
  h.writeUInt32LE(rate, 24); h.writeUInt32LE(rate * channels * bytes, 28); h.writeUInt16LE(channels * bytes, 32); h.writeUInt16LE(bits, 34);
  h.write('data', 36, 'latin1'); h.writeUInt32LE(sizes ? data.length : 0xffffffff, 40);
  return Buffer.concat([h, data]);
}
const samplesOf = (buf) => { const f = rec.wavFormat(buf); const out = []; for (let i = 0; i < f.dataLength / 2; i++) out.push(buf.readInt16LE(f.dataOffset + i * 2)); return out; };

// ---- a recorder stand-in ---------------------------------------------------------------------------------------

/**
 * A spawn that behaves like the real tools: it creates the output file as it starts, finishes it when it is told to stop
 * (SIGINT, or "stop" on stdin for PowerShell) and then exits. Options: `output` (the WAV it leaves), `exitAtStart`
 * (code, with `stderr`), `hang` (ignores the stop), `noFile` (leaves nothing).
 */
function fakeSpawn(options = {}) {
  const calls = [];
  const spawn = (command, args, spawnOptions) => {
    const child = new EventEmitter();
    child.stdin = new PassThrough(); child.stdout = new PassThrough(); child.stderr = new PassThrough();
    child.exitCode = null; child.signals = [];
    const file = (spawnOptions.env && spawnOptions.env.DLNE_VOICE_WAV) || args[args.length - 1];
    const exit = (code) => { if (child.exitCode === null) { child.exitCode = code; child.emit('exit', code, null); } };
    const finish = () => {
      if (options.hang) return;
      if (!options.noFile) fs.writeFileSync(file, options.output || wav({}));
      setImmediate(() => exit(0));
    };
    child.kill = (signal) => {
      child.signals.push(signal);
      if (signal === 'SIGKILL') setImmediate(() => exit(null));
      else if (signal === 'SIGINT' && !(options.windows)) finish();
      return true;
    };
    child.stdin.on('data', (chunk) => { if (String(chunk).includes('stop')) finish(); });
    calls.push({ command, args, env: spawnOptions.env, file, child });
    fs.writeFileSync(file, 'RIFF....');                 // the tool creates its file as it starts
    if (options.exitAtStart !== undefined) {
      if (options.stderr) child.stderr.write(options.stderr);
      setImmediate(() => exit(options.exitAtStart));
    } else if (options.windows) {
      setImmediate(() => child.stdout.write('RECORDING\r\n'));
    }
    return child;
  };
  spawn.calls = calls;
  return spawn;
}

function makeRecorder(platform, spawnOptions, extra = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-voice-test-'));
  const spawn = fakeSpawn({ windows: platform === 'win32', ...spawnOptions });
  const recorder = new rec.VoiceRecorder({
    platform, spawn, has: () => true, tmpdir: () => dir, readyMs: 5, stopGraceMs: 80, ...extra,
  });
  return { recorder, spawn, dir, left: () => fs.readdirSync(dir), done: () => fs.rmSync(dir, { recursive: true, force: true }) };
}

// ---- detection -------------------------------------------------------------------------------------------------

test('Linux tries pw-record, then parecord, then arecord', () => {
  const names = (have) => { const r = rec.detectRecorder('linux', (c) => have.includes(c)); return r && r.name; };
  assert.equal(names(['arecord', 'parecord', 'pw-record']), 'pw-record');
  assert.equal(names(['arecord', 'parecord']), 'parecord');
  assert.equal(names(['arecord']), 'arecord');
  assert.equal(names([]), null);
});

test('macOS tries rec (sox), then ffmpeg; Windows uses PowerShell; an unknown platform has none', () => {
  assert.equal(rec.detectRecorder('darwin', (c) => c === 'rec' || c === 'ffmpeg').name, 'rec');
  assert.equal(rec.detectRecorder('darwin', (c) => c === 'ffmpeg').name, 'ffmpeg');
  assert.equal(rec.detectRecorder('darwin', () => false), null);
  assert.equal(rec.detectRecorder('win32', (c) => c === 'powershell.exe').name, 'powershell');
  assert.equal(rec.detectRecorder('win32', () => false), null);
  assert.equal(rec.detectRecorder('freebsd', () => true), null);
});

test('each recorder is asked for 16 kHz mono 16-bit WAV, with the flags of its own tool', () => {
  const f = '/tmp/x.wav';
  const args = (platform, name) => rec.detectRecorder(platform, (c) => c === name).args(f);
  assert.deepEqual(args('linux', 'pw-record'), ['--rate', '16000', '--channels', '1', '--format', 's16', f]);
  assert.deepEqual(args('linux', 'parecord'), ['--rate=16000', '--channels=1', '--format=s16le', '--file-format=wav', f]);
  assert.deepEqual(args('linux', 'arecord'), ['-q', '-f', 'S16_LE', '-r', '16000', '-c', '1', '-t', 'wav', f]);
  assert.deepEqual(args('darwin', 'rec'), ['-q', '-r', '16000', '-c', '1', '-b', '16', f]);
  assert.deepEqual(args('darwin', 'ffmpeg'), ['-loglevel', 'error', '-y', '-f', 'avfoundation', '-i', ':default', '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le', f]);
});

test('Windows: PowerShell with the winmm MCI steps, saving to the file named in its environment', () => {
  const spec = rec.detectRecorder('win32', () => true);
  const args = spec.args('C:\\Temp\\a.wav');
  assert.equal(args[args.indexOf('-EncodedCommand') - 1], 'Bypass');
  const script = Buffer.from(args[args.length - 1], 'base64').toString('utf16le');
  for (const step of ['mciSendString', "'open new Type waveaudio Alias rec'", "'set rec bitspersample 16 samplespersec 16000 channels 1'",
    "'record rec'", "'stop rec'", 'save rec', "'close rec'"]) assert.ok(script.includes(step), `the script has ${step}`);
  assert.deepEqual(spec.env('C:\\Temp\\a.wav'), { DLNE_VOICE_WAV: 'C:\\Temp\\a.wav' });
  assert.ok(!args.join(' ').includes('a.wav'), 'the path travels in the environment, so it is never quoted into the script');
});

test('with no recorder, the message names what to install', () => {
  assert.match(rec.installHint('linux'), /pw-record.*parecord.*arecord/);
  assert.match(rec.installHint('darwin'), /sox.*ffmpeg/);
  assert.match(rec.installHint('win32'), /PowerShell/);
  const r = new rec.VoiceRecorder({ platform: 'linux', has: () => false });
  assert.equal(r.available(), false);
  assert.equal(r.missingHint(), rec.installHint('linux'));
});

test('onPath finds an executable on the PATH and nothing that is not there', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-voice-path-'));
  try {
    fs.writeFileSync(path.join(dir, 'arecord'), '#!/bin/sh\n', { mode: 0o755 });
    fs.writeFileSync(path.join(dir, 'parecord'), 'not executable', { mode: 0o644 });
    assert.equal(rec.onPath('arecord', { PATH: dir }, 'linux'), true);
    assert.equal(rec.onPath('pw-record', { PATH: dir }, 'linux'), false);
    if (process.platform !== 'win32' && process.getuid && process.getuid() !== 0) assert.equal(rec.onPath('parecord', { PATH: dir }, 'linux'), false);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

// ---- start, stop, auto-stop, the temp file ----------------------------------------------------------------------

for (const [platform, first] of [['linux', 'pw-record'], ['darwin', 'rec'], ['win32', 'powershell.exe']]) {
  test(`${platform}: start runs the first recorder, stop returns a 16 kHz mono WAV, and the temp file is gone`, async () => {
    const t = makeRecorder(platform, {});
    try {
      await t.recorder.start(0);
      assert.equal(t.spawn.calls.length, 1);
      assert.equal(t.spawn.calls[0].command, first);
      assert.equal(path.dirname(t.spawn.calls[0].file), t.dir, 'the clip is written to the temp folder');
      assert.ok(fs.existsSync(t.spawn.calls[0].file), 'while recording, the file is there');
      assert.equal(t.recorder.recording, true);
      const clip = await t.recorder.stop();
      assert.equal(rec.wavFormat(clip).rate, 16000);
      assert.equal(rec.wavFormat(clip).channels, 1);
      assert.deepEqual(t.left(), [], 'nothing is left behind');
      assert.equal(t.recorder.recording, false);
    } finally { t.done(); }
  });
}

test('Linux stops its recorder with SIGINT on its own child only; PowerShell is told "stop" on its input', async () => {
  const l = makeRecorder('linux', {});
  const w = makeRecorder('win32', {});
  try {
    await l.recorder.start(0);
    await l.recorder.stop();
    assert.deepEqual(l.spawn.calls[0].child.signals, ['SIGINT']);
    await w.recorder.start(0);
    let told = '';
    w.spawn.calls[0].child.stdin.on('data', (c) => { told += c; });
    await w.recorder.stop();
    assert.match(told, /stop/);
    assert.deepEqual(w.spawn.calls[0].child.signals, [], 'PowerShell finishes by itself');
  } finally { l.done(); w.done(); }
});

test('the clip ends by itself at max_seconds and is handed on', async () => {
  const t = makeRecorder('linux', {});
  try {
    let clip;
    await t.recorder.start(0.03, (c) => { clip = c; });
    await new Promise((r) => setTimeout(r, 120));
    assert.ok(clip, 'the auto-stop was announced');
    assert.equal(rec.wavFormat(await clip).rate, 16000);
    assert.equal(t.recorder.recording, false);
    assert.deepEqual(t.left(), []);
  } finally { t.done(); }
});

test('a manual stop cancels the auto-stop timer', async () => {
  const t = makeRecorder('linux', {});
  try {
    let fired = false;
    await t.recorder.start(0.05, () => { fired = true; });
    await t.recorder.stop();
    await new Promise((r) => setTimeout(r, 120));
    assert.equal(fired, false);
  } finally { t.done(); }
});

test('cancel ends the recorder and deletes the file', async () => {
  const t = makeRecorder('linux', {});
  try {
    await t.recorder.start(0);
    assert.deepEqual(t.left().length, 1);
    await t.recorder.cancel();
    assert.deepEqual(t.left(), []);
    assert.equal(t.recorder.recording, false);
    assert.ok(t.spawn.calls[0].child.signals.includes('SIGKILL'), 'its own child was ended');
  } finally { t.done(); }
});

test('a recorder that dies at once (no microphone) rejects with its own words and leaves no file', async () => {
  const t = makeRecorder('linux', { exitAtStart: 1, stderr: 'arecord: audio open error: No such file or directory' });
  try {
    await assert.rejects(t.recorder.start(0), /microphone could not be opened.*audio open error/);
    assert.deepEqual(t.left(), []);
    assert.equal(t.recorder.recording, false);
  } finally { t.done(); }
});

test('a spawn that throws (the program vanished) leaves no file and says so', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-voice-test-'));
  try {
    const r = new rec.VoiceRecorder({ platform: 'linux', has: () => true, tmpdir: () => dir, spawn: () => { throw new Error('spawn pw-record ENOENT'); } });
    await assert.rejects(r.start(0), /could not start.*ENOENT/);
    assert.deepEqual(fs.readdirSync(dir), []);
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});

test('stop with nothing recorded, with garbage in the file, or with a recorder that never finishes: an error, and the file is gone', async () => {
  for (const [options, message] of [
    [{ noFile: true, stderr: '' }, /./],
    [{ output: Buffer.from('this is not a wav file at all') }, /not a WAV/],
    [{ output: wav({ frames: 0 }) }, /empty/],
    [{ hang: true }, /did not finish/],
  ]) {
    const t = makeRecorder('linux', options);
    try {
      await t.recorder.start(0);
      if (options.noFile) fs.rmSync(t.spawn.calls[0].file);
      await assert.rejects(t.recorder.stop(), message);
      assert.deepEqual(t.left(), [], JSON.stringify(Object.keys(options)));
      assert.equal(t.recorder.recording, false);
      if (options.hang) assert.ok(t.spawn.calls[0].child.signals.includes('SIGKILL'), 'a hung recorder is ended through its own handle');
    } finally { t.done(); }
  }
});

test('start twice is refused; stop without start is refused; no recorder: start says what to install', async () => {
  const t = makeRecorder('linux', {});
  try {
    await t.recorder.start(0);
    await assert.rejects(t.recorder.start(0), /already recording/);
    await t.recorder.cancel();
    await assert.rejects(t.recorder.stop(), /not recording/);
  } finally { t.done(); }
  const none = new rec.VoiceRecorder({ platform: 'darwin', has: () => false });
  await assert.rejects(none.start(0), /sox/);
});

// ---- WAV normalisation -----------------------------------------------------------------------------------------

test('a WAV that is already 16 kHz mono 16-bit is returned untouched', () => {
  const w = wav({});
  assert.equal(rec.normaliseWav(w), w);
});

test('48 kHz stereo becomes 16 kHz mono: the channels are averaged and every third sample is kept as the mean of three', () => {
  const w = wav({ rate: 48000, channels: 2, frames: 480, sample: (i, c) => (c === 0 ? 1000 : 3000) });
  const out = rec.normaliseWav(w);
  const f = rec.wavFormat(out);
  assert.deepEqual([f.rate, f.channels, f.bits, f.tag], [16000, 1, 16, 1]);
  const s = samplesOf(out);
  assert.equal(s.length, 160);
  assert.ok(s.every((v) => v === 2000), 'left 1000 and right 3000 average to 2000');
  assert.equal(out.length, 44 + 160 * 2);
});

test('44.1 kHz mono is resampled to the right length; 8 kHz is stretched', () => {
  assert.equal(samplesOf(rec.normaliseWav(wav({ rate: 44100, frames: 4410 }))).length, 1600);
  assert.equal(samplesOf(rec.normaliseWav(wav({ rate: 8000, frames: 800 }))).length, 1600);
});

test('downsampling averages, so a tone above the new Nyquist does not alias into loud noise', () => {
  const w = wav({ rate: 48000, frames: 4800, sample: (i) => (i % 2 ? 10000 : -10000) });   // 24 kHz square wave
  const s = samplesOf(rec.normaliseWav(w));
  assert.ok(Math.max(...s.map(Math.abs)) < 4000);
});

test('a header whose sizes were never fixed (a recorder cut off) is read to the end of the file', () => {
  const w = wav({ frames: 320, sizes: false, rate: 32000 });
  assert.equal(samplesOf(rec.normaliseWav(w)).length, 160);
});

test('32-bit float is read; 8-bit and non-WAV are refused in plain words', () => {
  const f = Buffer.alloc(44 + 8);
  f.write('RIFF', 0, 'latin1'); f.writeUInt32LE(44, 4); f.write('WAVE', 8, 'latin1'); f.write('fmt ', 12, 'latin1');
  f.writeUInt32LE(16, 16); f.writeUInt16LE(3, 20); f.writeUInt16LE(1, 22); f.writeUInt32LE(16000, 24); f.writeUInt32LE(64000, 28);
  f.writeUInt16LE(4, 32); f.writeUInt16LE(32, 34); f.write('data', 36, 'latin1'); f.writeUInt32LE(8, 40);
  f.writeFloatLE(0.5, 44); f.writeFloatLE(-0.5, 48);
  assert.deepEqual(samplesOf(rec.normaliseWav(f)), [16384, -16384]);
  const eight = wav({}); eight.writeUInt16LE(8, 34);
  assert.throws(() => rec.normaliseWav(eight), /cannot read/);
  assert.throws(() => rec.normaliseWav(Buffer.from('nope')), /not a WAV/);
});

test('wavSeconds says how long a clip is', () => {
  assert.equal(rec.wavSeconds(wav({ frames: 32000 })), 2);
});
