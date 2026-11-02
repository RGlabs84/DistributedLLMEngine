/**
 * Conversation mode (2.8.2, docs/voice-contract.md): the extension HOST speaks, with the platform's own speech, because
 * a VS Code webview cannot be relied on to. Windows: PowerShell with System.Speech.Synthesis.SpeechSynthesizer reading
 * the text from STDIN. macOS: `say -f -`. Linux: `espeak-ng --stdin`, else `espeak --stdin`, else `spd-say` (the text is
 * one argv entry after `--`). The child is always spawned WITHOUT a shell and the text is never put in a command line
 * except as that one argv entry; it goes in through stdin.
 *
 * Sentences queue up: while one child speaks, the sentences that arrive wait, and the next child speaks them all.
 * `stop()` empties the queue and ends only the child this file spawned, through its own handle.
 */
import * as childProcess from 'child_process';
import { onPath } from './recorder';

export interface SpeakerSpec {
  name: string;
  command: string;
  /** `stdin`: the text is written to the child's input. `argv`: the text is one argument. */
  input: 'stdin' | 'argv';
  args(text: string, lang?: string): string[];
  env?(lang?: string): Record<string, string>;
  /** A command to run after the child is ended, for engines that go on speaking on their own (spd-say). */
  cancel?: string[];
}

/** The PowerShell program: it reads all of stdin as UTF-8 and speaks it. A voice for the language (DLNE_VOICE_LANG) is chosen if one is installed. */
export const WINDOWS_SPEAK_SCRIPT = [
  "$ErrorActionPreference = 'Stop'",
  'try {',
  "Add-Type -AssemblyName System.Speech",
  '[Console]::InputEncoding = [System.Text.Encoding]::UTF8',
  '$text = [Console]::In.ReadToEnd()',
  '$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer',
  'if ($env:DLNE_VOICE_LANG) {',
  '  foreach ($v in $synth.GetInstalledVoices()) {',
  '    if ($v.Enabled -and $v.VoiceInfo.Culture.TwoLetterISOLanguageName -eq $env:DLNE_VOICE_LANG) { $synth.SelectVoice($v.VoiceInfo.Name); break }',
  '  }',
  '}',
  '$synth.Speak($text)',
  '$synth.Dispose()',
  '} catch {',
  '  [Console]::Error.WriteLine($_.Exception.Message)',
  '  exit 1',
  '}',
].join('\r\n');

const SPECS: Record<string, SpeakerSpec> = {
  'espeak-ng': { name: 'espeak-ng', command: 'espeak-ng', input: 'stdin', args: () => ['--stdin'] },
  espeak: { name: 'espeak', command: 'espeak', input: 'stdin', args: () => ['--stdin'] },
  // `-w` waits until the speech is over, so the child lives as long as the speech; `--` ends the options, so text that starts with "-" is text.
  'spd-say': { name: 'spd-say', command: 'spd-say', input: 'argv', args: (text) => ['-w', '--', text], cancel: ['-C'] },
  say: { name: 'say', command: 'say', input: 'stdin', args: () => ['-f', '-'] },
};

function windowsSpec(command: string): SpeakerSpec {
  return {
    name: 'powershell', command, input: 'stdin',
    args: () => ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', Buffer.from(WINDOWS_SPEAK_SCRIPT, 'utf16le').toString('base64')],
    env: (lang): Record<string, string> => (lang ? { DLNE_VOICE_LANG: lang } : {}),
  };
}

/** The order tried per platform. */
export const SPEAKER_ORDER: Record<string, string[]> = {
  linux: ['espeak-ng', 'espeak', 'spd-say'],
  darwin: ['say'],
  win32: ['powershell.exe'],
};

/** What to install where nothing was found, in plain words. */
export function speakerHint(platform: string): string {
  if (platform === 'linux') {
    return 'DLNE cannot read answers aloud because no speech tool was found. Install espeak-ng (for example "sudo apt install espeak-ng" or "sudo pacman -S espeak-ng"), or speech-dispatcher (spd-say). The answer stays in the chat.';
  }
  if (platform === 'darwin') {
    return 'DLNE cannot read answers aloud because the macOS "say" command was not found. The answer stays in the chat.';
  }
  if (platform === 'win32') {
    return 'DLNE cannot read answers aloud because Windows PowerShell was not found. The answer stays in the chat.';
  }
  return 'DLNE cannot read answers aloud on this computer. The answer stays in the chat.';
}

export function detectSpeaker(platform: string, has: (command: string) => boolean = onPath): SpeakerSpec | null {
  for (const command of SPEAKER_ORDER[platform] ?? []) {
    if (!has(command)) continue;
    return platform === 'win32' ? windowsSpec(command) : SPECS[command];
  }
  return null;
}

type Spawn = (command: string, args: string[], options: childProcess.SpawnOptions) => childProcess.ChildProcess;

export interface SpeakerOptions {
  platform?: string;
  spawn?: Spawn;
  has?: (command: string) => boolean;
}

export class Speaker {
  private readonly platform: string;
  private readonly spawnFn: Spawn;
  private readonly has: (command: string) => boolean;
  private spec: SpeakerSpec | null | undefined;
  private child: childProcess.ChildProcess | null = null;
  private queue: string[] = [];
  private lang: string | undefined;
  private generation = 0;
  private listeners: Array<(speaking: boolean) => void> = [];
  private errorListeners: Array<(message: string) => void> = [];
  private speaking = false;

  constructor(options: SpeakerOptions = {}) {
    this.platform = options.platform ?? process.platform;
    this.spawnFn = options.spawn ?? ((c, a, o) => childProcess.spawn(c, a, o));
    this.has = options.has ?? ((c) => onPath(c, process.env, this.platform));
  }

  tool(): SpeakerSpec | null {
    if (this.spec === undefined) this.spec = detectSpeaker(this.platform, this.has);
    return this.spec;
  }

  available(): boolean {
    return this.tool() !== null;
  }

  missingHint(): string {
    return speakerHint(this.platform);
  }

  /** Called with true when speech starts and false when everything queued has been said or stopped. */
  onSpeaking(fn: (speaking: boolean) => void): void {
    this.listeners.push(fn);
  }

  /** Called with a plain sentence when the speech tool fails. */
  onError(fn: (message: string) => void): void {
    this.errorListeners.push(fn);
  }

  isSpeaking(): boolean {
    return this.speaking;
  }

  private setSpeaking(on: boolean): void {
    if (this.speaking === on) return;
    this.speaking = on;
    for (const fn of this.listeners) fn(on);
  }

  /** Adds sentences to what is said. `lang` is the language the server heard, if it said (two or three letters). */
  speak(sentences: string[], lang?: string): void {
    const text = sentences.map((s) => s.trim()).filter(Boolean);
    if (text.length === 0 || !this.tool()) return;
    if (lang && /^[a-z]{2,3}$/i.test(lang)) this.lang = lang.toLowerCase();
    this.queue.push(...text);
    if (!this.child) this.next();
  }

  private next(): void {
    const spec = this.tool();
    if (!spec || this.queue.length === 0) {
      this.child = null;
      this.setSpeaking(false);
      return;
    }
    const text = this.queue.join('\n');
    this.queue = [];
    const generation = this.generation;
    let child: childProcess.ChildProcess;
    try {
      child = this.spawnFn(spec.command, spec.args(text, this.lang), {
        shell: false,
        windowsHide: true,
        stdio: [spec.input === 'stdin' ? 'pipe' : 'ignore', 'ignore', 'pipe'],
        env: { ...process.env, ...(spec.env?.(this.lang) ?? {}) },
      });
    } catch (err: any) {
      this.failed(generation, `DLNE could not start the speech tool (${spec.name}): ${String(err?.message ?? err)}`);
      return;
    }
    this.child = child;
    this.setSpeaking(true);
    let stderr = '';
    child.stderr?.on('data', (d: Buffer) => { if (stderr.length < 400) stderr += d.toString('utf8'); });
    let done = false;
    const finish = (message?: string) => {
      if (done) return;
      done = true;
      if (generation !== this.generation) return;      // stopped meanwhile: stop() has already tidied up
      this.child = null;
      if (message) {
        this.failed(generation, message);
        return;
      }
      this.next();
    };
    child.on('error', (err: Error) => finish(`DLNE could not use the speech tool (${spec.name}): ${err.message}`));
    child.on('close', (code: number | null) => {
      finish(code !== 0 && code !== null ? `The speech tool (${spec.name}) stopped with an error${stderr.trim() ? `: ${stderr.trim().slice(0, 200)}` : '.'}` : undefined);
    });
    if (spec.input === 'stdin' && child.stdin) {
      child.stdin.on('error', () => { /* the child ended before it read everything: close tells why */ });
      child.stdin.end(text, 'utf8');
    }
  }

  private failed(generation: number, message: string): void {
    if (generation !== this.generation) return;
    this.queue = [];
    this.child = null;
    this.setSpeaking(false);
    for (const fn of this.errorListeners) fn(message);
  }

  /** Stops at once: what is queued is dropped and only the speech child this file spawned is ended. */
  stop(): void {
    this.generation++;
    this.queue = [];
    const child = this.child;
    this.child = null;
    if (child) {
      try { child.kill(); } catch { /* already gone */ }
      const spec = this.tool();
      if (spec?.cancel) {
        // spd-say hands the words to speech-dispatcher, which goes on speaking when spd-say is ended: ask it to cancel.
        try {
          const c = this.spawnFn(spec.command, spec.cancel, { shell: false, windowsHide: true, stdio: 'ignore' });
          c.on('error', () => { /* nothing to cancel */ });
        } catch { /* nothing to cancel */ }
      }
    }
    this.setSpeaking(false);
  }
}
