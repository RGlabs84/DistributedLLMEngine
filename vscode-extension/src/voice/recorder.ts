/**
 * Voice (2.8.0, docs/voice-contract.md): the extension HOST records, because a VS Code webview cannot use the microphone.
 * This file finds the platform's own recorder, drives it, and turns what it wrote into the WAV the server wants
 * (PCM 16-bit, mono, 16 kHz). The recorder's temp file lives in os.tmpdir() and is deleted on every path: after it is
 * read, on cancel, and on any error.
 *
 * Everything here that does not need a process is pure (`wavFormat`, `normaliseWav`, `detectRecorder`), and the process
 * is started through an injected `spawn`, so the tests drive it with a stub. Only the child this file spawned is ever
 * signalled, through its own handle.
 */
import * as childProcess from 'child_process';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';

export const TARGET_RATE = 16000;
/** The most a recording may be when the status does not say (the contract's VOICE_MAX_SECONDS default). */
export const DEFAULT_MAX_SECONDS = 60;

// ---- WAV --------------------------------------------------------------------------------------------------------

export interface WavFormat {
  channels: number;
  rate: number;
  bits: number;
  /** 1 = PCM, 3 = IEEE float. */
  tag: number;
  dataOffset: number;
  dataLength: number;
}

/** Reads a RIFF/WAVE header. A recorder that was stopped before it could fix its sizes leaves 0 or 0xFFFFFFFF: the data then runs to the end. */
export function wavFormat(buf: Buffer): WavFormat {
  if (buf.length < 12 || buf.toString('latin1', 0, 4) !== 'RIFF' || buf.toString('latin1', 8, 12) !== 'WAVE') {
    throw new Error('The recording is not a WAV file.');
  }
  let fmt: Omit<WavFormat, 'dataOffset' | 'dataLength'> | null = null;
  let pos = 12;
  while (pos + 8 <= buf.length) {
    const id = buf.toString('latin1', pos, pos + 4);
    let size = buf.readUInt32LE(pos + 4);
    const body = pos + 8;
    if (id === 'fmt ' && body + 16 <= buf.length) {
      let tag = buf.readUInt16LE(body);
      // WAVE_FORMAT_EXTENSIBLE: the real tag is the first two bytes of the sub-format.
      if (tag === 0xfffe && size >= 26 && body + 26 <= buf.length) tag = buf.readUInt16LE(body + 24);
      fmt = { tag, channels: buf.readUInt16LE(body + 2), rate: buf.readUInt32LE(body + 4), bits: buf.readUInt16LE(body + 14) };
    } else if (id === 'data') {
      if (!fmt) throw new Error('The recording has no format header.');
      const left = buf.length - body;
      if (size === 0 || size > left) size = left;
      return { ...fmt, dataOffset: body, dataLength: size };
    }
    pos = body + size + (size % 2);
  }
  throw new Error('The recording has no audio in it.');
}

/** A 44-byte header plus 16-bit mono 16 kHz samples. */
export function encodeWav(samples: Int16Array, rate: number = TARGET_RATE): Buffer {
  const dataLength = samples.length * 2;
  const out = Buffer.alloc(44 + dataLength);
  out.write('RIFF', 0, 'latin1');
  out.writeUInt32LE(36 + dataLength, 4);
  out.write('WAVE', 8, 'latin1');
  out.write('fmt ', 12, 'latin1');
  out.writeUInt32LE(16, 16);
  out.writeUInt16LE(1, 20);
  out.writeUInt16LE(1, 22);
  out.writeUInt32LE(rate, 24);
  out.writeUInt32LE(rate * 2, 28);
  out.writeUInt16LE(2, 32);
  out.writeUInt16LE(16, 34);
  out.write('data', 36, 'latin1');
  out.writeUInt32LE(dataLength, 40);
  for (let i = 0; i < samples.length; i++) out.writeInt16LE(samples[i], 44 + i * 2);
  return out;
}

/**
 * Any 16-bit PCM (or 32-bit float) WAV to PCM 16-bit mono 16 kHz: stereo is averaged to mono, and a higher rate is
 * averaged down (a box filter over the span each output sample covers, so 48 kHz does not alias). A WAV that already is
 * right is returned as it is. A WAV with no samples, or in a form this does not read, throws a plain sentence.
 */
export function normaliseWav(buf: Buffer): Buffer {
  const f = wavFormat(buf);
  const pcm16 = f.tag === 1 && f.bits === 16;
  const float32 = f.tag === 3 && f.bits === 32;
  if (!pcm16 && !float32) throw new Error(`The recording is in a form DLNE cannot read (${f.bits}-bit, format ${f.tag}).`);
  if (f.channels < 1 || f.rate < 1) throw new Error('The recording has a damaged header.');
  const bytes = pcm16 ? 2 : 4;
  const frames = Math.floor(f.dataLength / (bytes * f.channels));
  if (frames === 0) throw new Error('The recording is empty. Nothing was heard.');
  if (pcm16 && f.channels === 1 && f.rate === TARGET_RATE && f.dataOffset === 44 && buf.length === 44 + f.dataLength) return buf;

  const mono = new Float64Array(frames);
  for (let i = 0; i < frames; i++) {
    let sum = 0;
    for (let c = 0; c < f.channels; c++) {
      const at = f.dataOffset + (i * f.channels + c) * bytes;
      sum += pcm16 ? buf.readInt16LE(at) : buf.readFloatLE(at) * 32768;
    }
    mono[i] = sum / f.channels;
  }
  let samples: Float64Array;
  if (f.rate === TARGET_RATE) {
    samples = mono;
  } else {
    const outLength = Math.max(1, Math.floor((frames * TARGET_RATE) / f.rate));
    const ratio = f.rate / TARGET_RATE;
    samples = new Float64Array(outLength);
    for (let i = 0; i < outLength; i++) {
      const start = i * ratio;
      const end = Math.min(frames, start + Math.max(ratio, 1));
      if (ratio < 1) {
        // Upsampling: linear between the two neighbours.
        const a = Math.min(frames - 1, Math.floor(start));
        const b = Math.min(frames - 1, a + 1);
        const t = start - a;
        samples[i] = mono[a] * (1 - t) + mono[b] * t;
      } else {
        const from = Math.floor(start);
        const to = Math.max(from + 1, Math.ceil(end));
        let sum = 0;
        for (let j = from; j < to && j < frames; j++) sum += mono[j];
        samples[i] = sum / Math.max(1, Math.min(to, frames) - from);
      }
    }
  }
  const out = new Int16Array(samples.length);
  for (let i = 0; i < samples.length; i++) out[i] = Math.max(-32768, Math.min(32767, Math.round(samples[i])));
  return encodeWav(out);
}

/** How long a 16 kHz mono 16-bit WAV is, in seconds. */
export function wavSeconds(buf: Buffer): number {
  const f = wavFormat(buf);
  return f.dataLength / Math.max(1, f.rate * f.channels * (f.bits / 8));
}

// ---- the platform's recorder ------------------------------------------------------------------------------------

export interface RecorderSpec {
  /** Short name for messages and tests: 'pw-record', 'parecord', 'arecord', 'powershell', 'rec', 'ffmpeg'. */
  name: string;
  command: string;
  /** The arguments that record to `file`. */
  args(file: string): string[];
  /** The signal that makes the tool finish its file. `stdin`: Windows' PowerShell is told on its input instead. */
  stopWith: 'SIGINT' | 'stdin';
  /** Extra environment (Windows: where PowerShell saves). */
  env?(file: string): Record<string, string>;
  /** Windows: the line PowerShell prints once it is recording. */
  readyLine?: string;
}

/** The PowerShell program: winmm's MCI, which is part of Windows. It records until it is told "stop" on its input. */
export const WINDOWS_SCRIPT = [
  "$ErrorActionPreference = 'Stop'",
  'try {',
  "Add-Type -TypeDefinition @'",
  'using System;',
  'using System.Text;',
  'using System.Runtime.InteropServices;',
  'public static class DlneMci {',
  '  [DllImport("winmm.dll", CharSet = CharSet.Auto)] public static extern int mciSendString(string command, StringBuilder result, int length, IntPtr callback);',
  '  [DllImport("winmm.dll", CharSet = CharSet.Auto)] public static extern bool mciGetErrorString(int error, StringBuilder text, int length);',
  '}',
  "'@",
  'function Mci([string]$command) {',
  '  $code = [DlneMci]::mciSendString($command, $null, 0, [IntPtr]::Zero)',
  '  if ($code -ne 0) {',
  '    $text = New-Object System.Text.StringBuilder 256',
  '    [void][DlneMci]::mciGetErrorString($code, $text, 256)',
  "    throw ('the microphone could not be used: ' + $text.ToString())",
  '  }',
  '}',
  "Mci 'open new Type waveaudio Alias rec'",
  "Mci 'set rec bitspersample 16 samplespersec 16000 channels 1'",
  "Mci 'record rec'",
  "[Console]::Out.WriteLine('RECORDING'); [Console]::Out.Flush()",
  '$line = [Console]::In.ReadLine()',
  "if ($line -eq 'stop') {",
  "  Mci 'stop rec'",
  "  Mci ('save rec \"' + $env:DLNE_VOICE_WAV + '\"')",
  '}',
  "Mci 'close rec'",
  "[Console]::Out.WriteLine('DONE')",
  '} catch {',
  "  [Console]::Error.WriteLine($_.Exception.Message)",
  '  exit 1',
  '}',
].join('\r\n');

export const WINDOWS_READY = 'RECORDING';

const SPECS: Record<string, RecorderSpec> = {
  'pw-record': {
    name: 'pw-record', command: 'pw-record', stopWith: 'SIGINT',
    args: (file) => ['--rate', '16000', '--channels', '1', '--format', 's16', file],
  },
  parecord: {
    name: 'parecord', command: 'parecord', stopWith: 'SIGINT',
    args: (file) => ['--rate=16000', '--channels=1', '--format=s16le', '--file-format=wav', file],
  },
  arecord: {
    name: 'arecord', command: 'arecord', stopWith: 'SIGINT',
    args: (file) => ['-q', '-f', 'S16_LE', '-r', '16000', '-c', '1', '-t', 'wav', file],
  },
  rec: {
    name: 'rec', command: 'rec', stopWith: 'SIGINT',
    args: (file) => ['-q', '-r', '16000', '-c', '1', '-b', '16', file],
  },
  ffmpeg: {
    name: 'ffmpeg', command: 'ffmpeg', stopWith: 'SIGINT',
    args: (file) => ['-loglevel', 'error', '-y', '-f', 'avfoundation', '-i', ':default', '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le', file],
  },
};

/** Windows: `powershell.exe` (it ships with every Windows). */
function windowsSpec(command: string): RecorderSpec {
  return {
    name: 'powershell', command, stopWith: 'stdin', readyLine: WINDOWS_READY,
    args: () => ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', Buffer.from(WINDOWS_SCRIPT, 'utf16le').toString('base64')],
    env: (file) => ({ DLNE_VOICE_WAV: file }),
  };
}

/** The order tried per platform. */
export const RECORDER_ORDER: Record<string, string[]> = {
  linux: ['pw-record', 'parecord', 'arecord'],
  darwin: ['rec', 'ffmpeg'],
  win32: ['powershell.exe'],
};

/** What to install where nothing was found, in plain words. */
export function installHint(platform: string): string {
  if (platform === 'linux') {
    return 'DLNE voice needs a sound recorder, and none was found. Install PipeWire (pw-record), PulseAudio (parecord) or ALSA utils (arecord), for example "sudo apt install alsa-utils" or "sudo pacman -S alsa-utils".';
  }
  if (platform === 'darwin') {
    return 'DLNE voice needs a sound recorder, and none was found. Install SoX (brew install sox, which gives "rec") or ffmpeg (brew install ffmpeg).';
  }
  if (platform === 'win32') {
    return 'DLNE voice needs Windows PowerShell to record, and it was not found.';
  }
  return 'DLNE voice does not have a recorder for this computer.';
}

/** Whether an executable of this name is on the PATH. */
export function onPath(command: string, env: NodeJS.ProcessEnv = process.env, platform: string = process.platform): boolean {
  const dirs = (env.PATH ?? env.Path ?? '').split(platform === 'win32' ? ';' : ':').filter(Boolean);
  const exts = platform === 'win32' ? ['', ...(env.PATHEXT ?? '.EXE;.CMD').split(';')] : [''];
  for (const dir of dirs) {
    for (const ext of exts) {
      try {
        const candidate = path.join(dir, command + ext);
        if (!fs.statSync(candidate).isFile()) continue;
        if (platform !== 'win32') fs.accessSync(candidate, fs.constants.X_OK);
        return true;
      } catch { /* not here */ }
    }
  }
  return false;
}

/** The first recorder this platform has, or null. `has` says whether a command exists. */
export function detectRecorder(platform: string, has: (command: string) => boolean = onPath): RecorderSpec | null {
  for (const command of RECORDER_ORDER[platform] ?? []) {
    if (!has(command)) continue;
    return platform === 'win32' ? windowsSpec(command) : SPECS[command];
  }
  return null;
}

// ---- the recording ----------------------------------------------------------------------------------------------

type Spawn = (command: string, args: string[], options: childProcess.SpawnOptions) => childProcess.ChildProcess;

export interface RecorderOptions {
  platform?: string;
  spawn?: Spawn;
  has?: (command: string) => boolean;
  tmpdir?: () => string;
  setTimeout?: (fn: () => void, ms: number) => unknown;
  clearTimeout?: (handle: unknown) => void;
  /** How long a recorder gets to finish its file after it is told to stop, before its own process is killed. */
  stopGraceMs?: number;
  /** How long a recorder that prints nothing must stay up to count as started. */
  readyMs?: number;
}

export class VoiceRecorder {
  private readonly platform: string;
  private readonly spawnFn: Spawn;
  private readonly has: (command: string) => boolean;
  private readonly tmp: () => string;
  private readonly setT: (fn: () => void, ms: number) => unknown;
  private readonly clearT: (handle: unknown) => void;
  private readonly graceMs: number;
  private readonly readyMs: number;
  private spec: RecorderSpec | null | undefined;

  private child: childProcess.ChildProcess | null = null;
  private file = '';
  private exited: Promise<number | null> | null = null;
  private stderr = '';
  private timer: unknown;
  private autoStopped: Promise<Buffer> | null = null;

  constructor(options: RecorderOptions = {}) {
    this.platform = options.platform ?? process.platform;
    this.spawnFn = options.spawn ?? ((c, a, o) => childProcess.spawn(c, a, o));
    this.has = options.has ?? ((c) => onPath(c, process.env, this.platform));
    this.tmp = options.tmpdir ?? os.tmpdir;
    this.setT = options.setTimeout ?? ((fn, ms) => setTimeout(fn, ms));
    this.clearT = options.clearTimeout ?? ((h) => clearTimeout(h as NodeJS.Timeout));
    this.graceMs = options.stopGraceMs ?? 3000;
    this.readyMs = options.readyMs ?? 250;
  }

  /** The recorder this computer has, found once (null: none). */
  recorder(): RecorderSpec | null {
    if (this.spec === undefined) this.spec = detectRecorder(this.platform, this.has);
    return this.spec;
  }

  available(): boolean {
    return this.recorder() !== null;
  }

  missingHint(): string {
    return installHint(this.platform);
  }

  get recording(): boolean {
    return this.child !== null;
  }

  /** The temp file of the recording in progress (for tests); '' when none. */
  get tempFile(): string {
    return this.file;
  }

  /**
   * Starts recording. `maxSeconds`: the clip ends by itself then, and `onAutoStop` is given what `stop()` would have
   * returned. Resolves once the recorder is running; rejects, with the temp file gone, if it could not start.
   */
  async start(maxSeconds: number, onAutoStop?: (clip: Promise<Buffer>) => void): Promise<void> {
    if (this.child) throw new Error('DLNE is already recording.');
    const spec = this.recorder();
    if (!spec) throw new Error(this.missingHint());
    const file = path.join(this.tmp(), `dlne-voice-${process.pid}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}.wav`);
    const env = { ...process.env, ...(spec.env ? spec.env(file) : {}) };
    let child: childProcess.ChildProcess;
    try {
      child = this.spawnFn(spec.command, spec.args(file), { stdio: ['pipe', 'pipe', 'pipe'], env, windowsHide: true });
    } catch (err: any) {
      this.removeFile(file);
      throw new Error(`The recorder (${spec.name}) could not start: ${err?.message ?? err}`);
    }
    this.child = child;
    this.file = file;
    this.stderr = '';
    child.stderr?.on('data', (chunk) => { this.stderr = (this.stderr + String(chunk)).slice(-500); });
    this.exited = new Promise((resolve) => {
      let done = false;
      const finish = (code: number | null) => { if (!done) { done = true; resolve(code); } };
      child.once('exit', (code) => finish(code));
      child.once('error', (err) => { this.stderr += ` ${err.message}`; finish(-1); });
    });
    try {
      await this.waitReady(child, spec);
    } catch (err) {
      await this.discard();
      throw err;
    }
    if (maxSeconds > 0) {
      this.timer = this.setT(() => {
        this.timer = undefined;
        const clip = this.stop();
        clip.catch(() => { /* the caller of onAutoStop reads the failure */ });
        this.autoStopped = clip;
        onAutoStop?.(clip);
      }, maxSeconds * 1000);
    }
  }

  private waitReady(child: childProcess.ChildProcess, spec: RecorderSpec): Promise<void> {
    return new Promise((resolve, reject) => {
      let buffered = '';
      const failed = () => reject(new Error(this.startFailure(spec)));
      const early = () => failed();
      child.once('exit', early);
      child.once('error', early);
      const ok = () => { child.off('exit', early); child.off('error', early); resolve(); };
      if (spec.readyLine) {
        child.stdout?.on('data', (chunk) => {
          buffered += String(chunk);
          if (buffered.includes(spec.readyLine as string)) ok();
        });
      } else {
        // These tools write nothing to say they are recording: a moment without an exit is the start.
        this.setT(() => { if (this.child === child && child.exitCode === null) ok(); }, this.readyMs);
      }
    });
  }

  private startFailure(spec: RecorderSpec): string {
    const said = this.stderr.replace(/\s+/g, ' ').trim();
    return `The microphone could not be opened (${spec.name})${said ? `: ${said}` : '.'} Check that a microphone is connected and not muted.`;
  }

  /** Stops, reads the clip, deletes the temp file (always) and returns the normalised WAV. */
  async stop(): Promise<Buffer> {
    const child = this.child;
    const spec = this.recorder();
    if (this.autoStopped && !child) return this.autoStopped;
    if (!child || !spec) throw new Error('DLNE is not recording.');
    const file = this.file;
    const exited = this.exited as Promise<number | null>;
    this.clearTimer();
    try {
      this.tellToStop(child, spec);
      const code = await this.within(exited, this.graceMs);
      if (code === undefined) {
        this.killOwn(child);
        await exited;
        throw new Error('The recorder did not finish in time.');
      }
      let raw: Buffer;
      try {
        raw = fs.readFileSync(file);
      } catch {
        throw new Error(this.stderr.trim() ? `Nothing was recorded: ${this.stderr.replace(/\s+/g, ' ').trim()}` : 'Nothing was recorded.');
      }
      return normaliseWav(raw);
    } finally {
      this.child = null;
      this.file = '';
      this.exited = null;
      this.removeFile(file);
    }
  }

  /** Throws the recording away: the recorder is ended and the temp file deleted. */
  async cancel(): Promise<void> {
    this.clearTimer();
    this.autoStopped = null;
    await this.discard();
  }

  private async discard(): Promise<void> {
    const child = this.child;
    const file = this.file;
    const exited = this.exited;
    this.child = null;
    this.file = '';
    this.exited = null;
    if (child && exited) {
      this.killOwn(child);
      await this.within(exited, this.graceMs);
    }
    if (file) this.removeFile(file);
  }

  private tellToStop(child: childProcess.ChildProcess, spec: RecorderSpec): void {
    if (spec.stopWith === 'stdin') {
      try { child.stdin?.write('stop\n'); child.stdin?.end(); } catch { /* it has gone already */ }
    } else {
      try { child.kill('SIGINT'); } catch { /* it has gone already */ }
    }
  }

  /** Ends the child this recorder started, by its own handle. */
  private killOwn(child: childProcess.ChildProcess): void {
    try { child.stdin?.end(); } catch { /* closed */ }
    try { child.kill('SIGKILL'); } catch { /* gone */ }
  }

  private within<T>(promise: Promise<T>, ms: number): Promise<T | undefined> {
    return new Promise((resolve) => {
      const timer = this.setT(() => resolve(undefined), ms);
      promise.then((v) => { this.clearT(timer); resolve(v); });
    });
  }

  private clearTimer(): void {
    if (this.timer !== undefined) this.clearT(this.timer);
    this.timer = undefined;
  }

  private removeFile(file: string): void {
    try { fs.rmSync(file, { force: true }); } catch { /* nothing to remove */ }
  }
}
