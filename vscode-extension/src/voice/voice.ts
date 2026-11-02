/**
 * Voice (2.8.0, docs/voice-contract.md), the part between the recorder and the panel: whether the mic is offered, the
 * toggle (record, stop, send), the plain words for every refusal, and the multipart request.
 */
import { DEFAULT_MAX_SECONDS } from './recorder';

/** What the coordinator says (`capabilities.voice`, and `voice.max_seconds`). */
export interface VoiceStatus {
  capabilities?: { voice?: unknown };
  voice?: { max_seconds?: unknown; queue?: unknown; busy?: unknown };
}

export function voiceOffered(status: VoiceStatus | null | undefined): boolean {
  return status?.capabilities?.voice === true;
}

/** The longest a clip may be: the status's `voice.max_seconds`, else the contract's default. */
export function voiceMaxSeconds(status: VoiceStatus | null | undefined): number {
  const n = Number(status?.voice?.max_seconds);
  return Number.isFinite(n) && n >= 1 ? Math.min(Math.floor(n), 600) : DEFAULT_MAX_SECONDS;
}

/** The mic button is shown only while the server offers voice, the setting is on, and a recorder exists. */
export function micShown(status: VoiceStatus | null | undefined, settingOn: boolean, recorderFound: boolean): boolean {
  return voiceOffered(status) && settingOn && recorderFound;
}

export const VOICE_NOTE =
  'Your voice clip goes to the DLNE server to be turned into text, and it is not kept. The text lands in the message box; nothing is sent until you press Send.';

export const VOICE_NOTE_CONVERSATION =
  'Your voice clip goes to the DLNE server to be turned into text, and it is not kept. In Conversation mode the text is sent as your message and the answer is read aloud by this computer\'s own voice.';

// ---- the request ------------------------------------------------------------------------------------------------

/** A multipart/form-data body for POST /v1/audio/transcriptions: `file`, `model` (ignored by the server), `response_format`. */
export function transcriptionRequest(wav: Buffer, boundary: string = `----dlne-voice-${Math.random().toString(36).slice(2, 12)}`): { body: Buffer; contentType: string } {
  const field = (name: string, value: string) =>
    Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="${name}"\r\n\r\n${value}\r\n`, 'utf8');
  const fileHead = Buffer.from(
    `--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="voice.wav"\r\nContent-Type: audio/wav\r\n\r\n`, 'utf8');
  const body = Buffer.concat([field('model', 'whisper-1'), field('response_format', 'json'), fileHead, wav, Buffer.from(`\r\n--${boundary}--\r\n`, 'utf8')]);
  return { body, contentType: `multipart/form-data; boundary=${boundary}` };
}

// ---- the answer -------------------------------------------------------------------------------------------------

export interface VoiceReply {
  status: number;
  body: any;
  retryAfter?: number;
}

/** The error code and sentence the server sent, whichever of the usual shapes carries them. */
export function errorParts(body: any): { code: string; detail: string } {
  const pick = (v: unknown) => (typeof v === 'string' ? v : '');
  const d = body?.detail;
  const e = body?.error;
  const code = pick(body?.code) || pick(d?.code) || pick(e?.code) || pick(body?.error_code) || (typeof e === 'string' && /^voice_/.test(e) ? e : '');
  const detail = pick(d?.message) || pick(d?.detail) || pick(e?.message) || pick(body?.message) || (typeof d === 'string' ? d : '') || (typeof e === 'string' && !/^voice_/.test(e) ? e : '');
  return { code, detail: detail.trim() };
}

/** Plain words for a refusal (or a failure to reach the server). */
export function voiceErrorText(reply: VoiceReply): string {
  const { code, detail } = errorParts(reply.body);
  const sentence = detail && detail.length < 300 ? detail : '';
  switch (code) {
    case 'voice_quota':
      return 'Voice is used up for this hour. Try again later.';
    case 'voice_busy':
      return 'Voice is busy right now, with too many clips waiting. Try again in a moment.';
    case 'voice_paused':
      return sentence || 'Voice is paused for now. Try again later.';
    case 'voice_unavailable':
      return sentence || 'Voice is not available on this server right now.';
    case 'voice_bad_audio':
      return `The server could not use that recording${sentence ? `: ${sentence}` : '.'} Try recording again.`;
    default:
  }
  switch (reply.status) {
    case 0:
      return `Could not reach the DLNE server${sentence ? `: ${sentence}` : '.'} Check your connection.`;
    case 401:
    case 403:
      return 'The DLNE server did not accept your key for voice. Check dlne.apiKey in Settings.';
    case 404:
      return 'This DLNE server does not offer voice yet.';
    case 413:
      return 'That recording is too long for the server. Try a shorter one.';
    case 429:
      return 'Voice is used up for this hour. Try again later.';
    case 503:
      return sentence || 'Voice is not available on this server right now.';
    default:
      return `Voice did not work (the server answered ${reply.status})${sentence ? `: ${sentence}` : '.'}`;
  }
}

/** The text of a good answer, or the words for why there is none. */
export function transcriptFromReply(reply: VoiceReply): { text: string } | { error: string } {
  if (reply.status >= 200 && reply.status < 300) {
    const text = typeof reply.body?.text === 'string' ? reply.body.text.trim() : '';
    return text ? { text } : { error: 'Nothing was heard. Try again, closer to the microphone.' };
  }
  return { error: voiceErrorText(reply) };
}

// ---- the toggle -------------------------------------------------------------------------------------------------

export type VoiceMode = 'dictate' | 'conversation';

/** The setting `dlne.voice.mode`: anything but "conversation" is Dictate. */
export function voiceModeFrom(value: unknown): VoiceMode {
  return value === 'conversation' ? 'conversation' : 'dictate';
}

export type VoiceState = 'idle' | 'recording' | 'sending';

export interface VoiceRecorderLike {
  available(): boolean;
  missingHint(): string;
  start(maxSeconds: number, onAutoStop?: (clip: Promise<Buffer>) => void): Promise<void>;
  stop(): Promise<Buffer>;
  cancel(): Promise<void>;
}

export interface VoiceHost {
  recorder: VoiceRecorderLike;
  status(): VoiceStatus | null | undefined;
  settingOn(): boolean;
  transcribe(wav: Buffer): Promise<VoiceReply>;
  /** Tells the panel (and the user) what is happening. */
  state(state: VoiceState, extra?: { maxSeconds?: number; note?: string }): void;
  /** Words for the user, shown as a notice. */
  say(text: string, isError: boolean): void;
  /**
   * The text to put into the message box at the cursor. In Conversation mode `send` is true: the whole box is then sent as
   * if Send were pressed (every rule that blocks Send blocks it, and the text stays). `language` is what the server heard.
   */
  insert(text: string, options?: { send: boolean; language?: string }): void;
  /** Dictate (the default) or Conversation (2.8.2). */
  mode?(): VoiceMode;
  /** Starting a recording ends any speech at once. */
  beforeRecord?(): void;
  /** True once the one-time privacy note has been shown; `markNoted` records it. */
  noted(): boolean;
  markNoted(): void;
}

/** Record, stop, send: one clip at a time. The text is handed to `insert`; nothing here sends a chat message. */
export class VoiceController {
  private current: VoiceState = 'idle';

  constructor(private readonly host: VoiceHost) {}

  get state(): VoiceState {
    return this.current;
  }

  /** Whether the mic is offered right now. */
  shown(): boolean {
    return micShown(this.host.status(), this.host.settingOn(), this.host.recorder.available());
  }

  private set(state: VoiceState, extra?: { maxSeconds?: number; note?: string }): void {
    this.current = state;
    this.host.state(state, extra);
  }

  /** The mic button, or the keybinding: starts a recording, or stops the one in progress and sends it. */
  async toggle(): Promise<void> {
    if (this.current === 'sending') return;
    if (this.current === 'recording') {
      await this.finish();
      return;
    }
    if (!this.host.settingOn()) {
      this.host.say('DLNE voice is turned off. Turn on "dlne.voice.enabled" in Settings to use it.', true);
      return;
    }
    if (!voiceOffered(this.host.status())) {
      this.host.say('This DLNE server does not offer voice right now.', true);
      return;
    }
    if (!this.host.recorder.available()) {
      this.host.say(this.host.recorder.missingHint(), true);
      return;
    }
    const maxSeconds = voiceMaxSeconds(this.host.status());
    this.host.beforeRecord?.();
    try {
      await this.host.recorder.start(maxSeconds, (clip) => { void this.send(clip); });
    } catch (err: any) {
      this.host.say(String(err?.message ?? err), true);
      this.set('idle');
      return;
    }
    const note = this.host.noted() ? undefined : (this.host.mode?.() === 'conversation' ? VOICE_NOTE_CONVERSATION : VOICE_NOTE);
    if (note) this.host.markNoted();
    this.set('recording', { maxSeconds, note });
  }

  private async finish(): Promise<void> {
    await this.send(this.host.recorder.stop());
  }

  private async send(clip: Promise<Buffer>): Promise<void> {
    if (this.current === 'sending') return;
    this.set('sending');
    try {
      const wav = await clip;
      const reply = await this.host.transcribe(wav);
      const outcome = transcriptFromReply(reply);
      if ('text' in outcome) {
        const language = typeof reply.body?.language === 'string' ? reply.body.language : undefined;
        const send = (this.host.mode?.() ?? 'dictate') === 'conversation';
        if (send) this.host.insert(outcome.text, { send, language });
        else this.host.insert(outcome.text);
      }
      else this.host.say(outcome.error, true);
    } catch (err: any) {
      this.host.say(String(err?.message ?? err), true);
    } finally {
      this.set('idle');
    }
  }

  /** The panel goes away or a new chat starts: a recording in progress is thrown away. */
  async cancel(): Promise<void> {
    if (this.current !== 'recording') return;
    await this.host.recorder.cancel();
    this.set('idle');
  }
}
