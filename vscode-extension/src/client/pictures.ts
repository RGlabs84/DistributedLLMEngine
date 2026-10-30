/**
 * Pictures in the agent's conversation (docs/agent-memory.md sections 1 and 3, docs/release-2.7-contract.md 4d).
 *
 * The coordinator has read pictures in coding chats since 2.6.1: OpenAI `image_url` parts with `data:` URIs, checked by
 * engine/media.py (PNG or JPEG only, at most 4 a request and 4 MiB each; the coordinator's own engine reads them, a
 * coding member refuses them). Here a picture travels in the conversation as a field of a ChatMessage (`images`), so
 * every place that counts or shortens the message's text keeps working on a plain string, and it becomes `image_url`
 * parts only when the request is written (`wireMessages`).
 *
 * No vscode import: tests load it straight from dist/.
 */
import type { ChatMessage } from './dlneClient';

/** A picture of the conversation: where it came from (a file name), and its bytes as the data URI that is sent. */
export interface PictureAttachment {
  name: string;
  dataUri: string;
  width?: number;
  height?: number;
}

/** At most this many pictures in a message (the server's limit is 4 a request). */
export const MAX_PICTURES = 4;
/** The most bytes of one picture, decoded (the server's limit too). */
export const MAX_PICTURE_BYTES = 4 * 1024 * 1024;
/** What the engine counts for a picture, at most (about; `engine_image_max_tokens`). */
export const PICTURE_TOKENS = 1024;
/** A picture's cost in the agent's character estimate (3 characters a token). */
export const PICTURE_CHARS = PICTURE_TOKENS * 3;
/** Pictures of the last this many user turns stay in a request; older ones become a note. */
export const KEEP_PICTURE_TURNS = 3;
/** What the server takes (engine/media.py IMAGE_FORMATS). */
export const SENT_MIMES = ['image/png', 'image/jpeg'];
/** The refusal codes of engine/media.py and the coordinator that mean "the picture, not the request". */
export const PICTURE_REFUSAL_CODES = new Set([
  'too_many_images', 'image_too_large', 'unsupported_image_type', 'remote_image_url', 'image_input_unavailable', 'image_quota',
]);

export interface PictureInfo { mime: 'image/png' | 'image/jpeg' | 'image/gif' | 'image/webp'; width: number; height: number }

/** The kind and size of a picture file, read from its own first bytes (never from its name). Null when it is none of the four. */
export function sniffPicture(bytes: Uint8Array): PictureInfo | null {
  const b = bytes;
  const u16be = (i: number) => (b[i] << 8) | b[i + 1];
  const u32be = (i: number) => ((b[i] * 0x1000000) + ((b[i + 1] << 16) | (b[i + 2] << 8) | b[i + 3]));
  if (b.length >= 24 && b[0] === 0x89 && b[1] === 0x50 && b[2] === 0x4e && b[3] === 0x47 && b[4] === 0x0d && b[5] === 0x0a && b[6] === 0x1a && b[7] === 0x0a) {
    return { mime: 'image/png', width: u32be(16), height: u32be(20) };
  }
  if (b.length >= 4 && b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff) {
    // Walk the segments to the first start-of-frame marker, which holds the size.
    let i = 2;
    while (i + 9 < b.length) {
      if (b[i] !== 0xff) { i++; continue; }
      const marker = b[i + 1];
      if (marker === 0xff) { i++; continue; }
      if (marker === 0xd8 || (marker >= 0xd0 && marker <= 0xd7) || marker === 0x01) { i += 2; continue; }
      if (marker >= 0xc0 && marker <= 0xcf && marker !== 0xc4 && marker !== 0xc8 && marker !== 0xcc) {
        return { mime: 'image/jpeg', height: u16be(i + 5), width: u16be(i + 7) };
      }
      i += 2 + u16be(i + 2);
    }
    return { mime: 'image/jpeg', width: 0, height: 0 };
  }
  const ascii = (from: number, to: number) => String.fromCharCode(...Array.from(b.subarray(from, to)));
  if (b.length >= 10 && (ascii(0, 6) === 'GIF87a' || ascii(0, 6) === 'GIF89a')) {
    return { mime: 'image/gif', width: b[6] | (b[7] << 8), height: b[8] | (b[9] << 8) };
  }
  if (b.length >= 30 && ascii(0, 4) === 'RIFF' && ascii(8, 12) === 'WEBP') {
    const kind = ascii(12, 16);
    if (kind === 'VP8 ') return { mime: 'image/webp', width: (b[26] | (b[27] << 8)) & 0x3fff, height: (b[28] | (b[29] << 8)) & 0x3fff };
    if (kind === 'VP8L') {
      const bits = (b[21] | (b[22] << 8) | (b[23] << 16) | (b[24] * 0x1000000)) >>> 0;
      return { mime: 'image/webp', width: (bits & 0x3fff) + 1, height: ((bits >>> 14) & 0x3fff) + 1 };
    }
    if (kind === 'VP8X') {
      return { mime: 'image/webp', width: (b[24] | (b[25] << 8) | (b[26] << 16)) + 1, height: (b[27] | (b[28] << 8) | (b[29] << 16)) + 1 };
    }
    return { mime: 'image/webp', width: 0, height: 0 };
  }
  return null;
}

/** A file name fit to show and to put in a note: its last part, no control characters or brackets, at most 60 characters. */
export function pictureName(name: unknown): string {
  const base = String(name ?? '').split(/[\\/]/).pop() ?? '';
  const clean = base.replace(/[\u0000-\u001f\u007f[\]<>]/g, '').replace(/\s+/g, ' ').trim().slice(0, 60);
  return clean || 'picture';
}

/** How a picture stands in saved chats and in the working memory: its name, never its bytes. */
export const pictureNote = (name: string): string => `[picture: ${name}]`;
/** What replaces a picture that is no longer in the request. */
export const shownEarlier = (name: string): string => `[picture: ${name}, shown earlier]`;

const DATA_URI = /^data:(image\/(?:png|jpeg));base64,([A-Za-z0-9+/]+={0,2})$/;

/** A data URI for these bytes. */
export function dataUriOf(mime: string, bytes: Uint8Array): string {
  return `data:${mime};base64,${Buffer.from(bytes.buffer, bytes.byteOffset, bytes.byteLength).toString('base64')}`;
}

/**
 * A picture file as an attachment. Only PNG and JPEG go to the server, at most 4 MiB: the answer is the reason in plain
 * words when it is not one (a GIF or WebP file is converted by the panel before it gets here, a file on disk is not).
 */
export function pictureFromBytes(name: string, bytes: Uint8Array): { ok: true; picture: PictureAttachment } | { ok: false; error: string } {
  const info = sniffPicture(bytes);
  if (!info) return { ok: false, error: 'it is not a PNG, JPEG, WebP or GIF picture' };
  if (info.mime !== 'image/png' && info.mime !== 'image/jpeg') {
    return { ok: false, error: `the swarm reads PNG and JPEG pictures, and this is a ${info.mime.slice(6).toUpperCase()}: convert it to PNG first` };
  }
  if (bytes.byteLength > MAX_PICTURE_BYTES) {
    return { ok: false, error: `it is ${(bytes.byteLength / 1048576).toFixed(1)} MiB, and a picture may be 4 MiB at most` };
  }
  if (info.width > 4096 || info.height > 4096) return { ok: false, error: `it is ${info.width}x${info.height}, and a side may be 4096 pixels at most` };
  return { ok: true, picture: { name: pictureName(name), dataUri: dataUriOf(info.mime, bytes), width: info.width || undefined, height: info.height || undefined } };
}

/**
 * The pictures a page (the panel's webview) hands over, checked again here: the page is only a page. What is not a
 * well-formed PNG or JPEG data URI within the limits is left out, each with the reason; the rest is returned cleaned.
 */
export function checkPictures(list: unknown): { pictures: PictureAttachment[]; problems: string[] } {
  const pictures: PictureAttachment[] = [];
  const problems: string[] = [];
  if (!Array.isArray(list)) return { pictures, problems };
  for (const raw of list) {
    const item = raw !== null && typeof raw === 'object' ? (raw as Record<string, unknown>) : {};
    const name = pictureName(item.name);
    if (pictures.length >= MAX_PICTURES) {
      problems.push(`${name}: a message may carry ${MAX_PICTURES} pictures, so this one was left out`);
      continue;
    }
    const match = typeof item.dataUri === 'string' ? DATA_URI.exec(item.dataUri) : null;
    if (!match) {
      problems.push(`${name}: it is not a PNG or JPEG picture`);
      continue;
    }
    const bytes = Buffer.from(match[2], 'base64');
    const checked = pictureFromBytes(name, bytes);
    if (!checked.ok) problems.push(`${name}: ${checked.error}`);
    else if (checked.picture.dataUri.slice(5, checked.picture.dataUri.indexOf(';')) !== match[1]) problems.push(`${name}: its bytes are not the ${match[1].slice(6).toUpperCase()} it says`);
    else pictures.push(checked.picture);
  }
  return { pictures, problems };
}

/** The request body's messages: a user message with pictures becomes text and `image_url` parts; nothing else changes. */
export function wireMessages(messages: ChatMessage[]): any[] {
  return messages.map((m) => {
    const { images, pictureTurn: _turn, ...rest } = m;
    if (!images || images.length === 0 || m.role !== 'user') return rest;
    const parts: any[] = [];
    if (m.content) parts.push({ type: 'text', text: m.content });
    for (const picture of images) parts.push({ type: 'image_url', image_url: { url: picture.dataUri } });
    return { ...rest, content: parts };
  });
}

/** Whether a request body carries a picture (a message whose content is a list with an `image_url` part). */
export function carriesPicture(payload: { messages?: unknown } | null | undefined): boolean {
  const messages = payload?.messages;
  if (!Array.isArray(messages)) return false;
  return messages.some((m: any) => Array.isArray(m?.content) && m.content.some((p: any) => p && p.type === 'image_url'));
}

/**
 * The messages with the pictures of old turns replaced by a note. `turn` is the newest user turn; a picture stays while
 * its message's `pictureTurn` is within the last KEEP_PICTURE_TURNS turns. A message that loses all its pictures gains
 * the note in its text. The messages given are not changed.
 */
export function withoutOldPictures(messages: ChatMessage[], turn: number, keep: number = KEEP_PICTURE_TURNS): ChatMessage[] {
  // The server takes MAX_PICTURES a request, all messages together: the newest are kept, whatever their turn.
  let room = MAX_PICTURES;
  const out: ChatMessage[] = new Array(messages.length);
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (!m.images || m.images.length === 0) {
      out[i] = m;
      continue;
    }
    const fresh = typeof m.pictureTurn === 'number' && m.pictureTurn > turn - keep;
    const kept = fresh ? Math.min(m.images.length, room) : 0;
    room -= kept;
    if (kept === m.images.length) {
      out[i] = m;
      continue;
    }
    const { images, pictureTurn: _turn, ...rest } = m;
    const notes = images.slice(kept).map((p) => shownEarlier(p.name)).join(' ');
    const content = m.content ? `${m.content}\n${notes}` : notes;
    out[i] = kept > 0 ? { ...rest, content, images: images.slice(0, kept), pictureTurn: m.pictureTurn } : { ...rest, content };
  }
  return out;
}

/** The messages with every picture replaced by its note (a saved chat, a compaction). The messages given are not changed. */
export function withoutPictures(messages: ChatMessage[], note: (name: string) => string = shownEarlier): ChatMessage[] {
  return messages.map((m) => {
    if (!m.images || m.images.length === 0) return m;
    const { images, pictureTurn: _turn, ...rest } = m;
    const notes = images.map((p) => note(p.name)).join(' ');
    return { ...rest, content: m.content ? `${m.content}\n${notes}` : notes };
  });
}

/** Whether any message carries a picture of its own (the `images` field). */
export function holdsPictures(messages: ReadonlyArray<ChatMessage>): boolean {
  return messages.some((m) => m.images && m.images.length > 0);
}

/** Whether the coordinator says its engine can read pictures (`capabilities.vision`): the controls and the tool are offered. */
export function visionOffered(status: { capabilities?: { vision?: unknown } } | null | undefined): boolean {
  return status?.capabilities?.vision === true;
}
