/**
 * End-to-end encryption between this extension and a coding member (docs/coding-members.md, section 6).
 * The Python counterpart is engine/e2e.py; both follow the same algorithm and are checked against the same
 * test vectors (tests/fixtures/e2e_vectors.json). Node `crypto` only, no dependencies.
 *
 * What it protects: the coordinator, the relay tunnel and the network carry only ciphertext of the user's
 * code and of the model's answer; any change, drop, reorder or repeat is detected, and so is a stream cut short
 * (a stream only counts as complete after a sealed end marker). Requests and response chunks are padded to
 * 256-byte blocks so message lengths do not show token lengths.
 * What it does not: the member's own memory (a GPU must see plaintext; consumer cards have no confidential
 * computing), and the member's identity (its public key comes through the coordinator, which is trusted not to
 * swap it). Timing and the number of chunks are still visible. See engine/e2e.py for the full note.
 *
 * Algorithm x25519-hkdf-sha256-chacha20poly1305, version 1:
 *   shared = X25519(eph_priv, member_pub)  (all-zero result refused)
 *   okm = HKDF-SHA256(ikm=shared, salt=eph_pub || member_pub, info="dlne-e2e-v1", length=64)
 *   req_key = okm[0:32], resp_key = okm[32:64]
 *   request  {"dlne_e2e": {"v":1, "epk", "nonce" (12 random bytes), "ct" = ChaCha20-Poly1305(req_key, nonce, json, aad "dlne-req")}}
 *   rs = 16 random bytes the member picks for each response, carried as "rs" by the first sealed object
 *   stream_key = HKDF-SHA256(ikm=resp_key, salt=rs, info="dlne-e2e-v1-resp", length=32)
 *   response {"n": counter, "ct" = ChaCha20-Poly1305(stream_key, nonce = counter as 12-byte big-endian, payload, aad "dlne-resp")}
 *   a stream ends with one more sealed object whose plaintext is "[DONE]" (then the plain `data: [DONE]` line);
 *   a non-streamed answer is a single object with n = 0 and no end marker.
 *   request json and response payloads get trailing spaces up to a multiple of 256 bytes before sealing.
 * "ct" is ciphertext followed by the 16-byte tag. All base64 is the canonical standard alphabet with padding.
 *
 * Use the helpers, never count nonces by hand: sealRequest, ResponseOpener (client); openRequest and
 * ResponseSealer (fake members in tests). sealPayload / openPayload are the low-level pair: they use exactly the
 * key you hand them (the stream key, see deriveStreamKey) and know nothing about rs or the end marker.
 *
 * Points the contract text left open (same choices as engine/e2e.py): sealRequest returns the whole wire body
 * `{dlne_e2e: {...}}`; a sealed object is the inner `{n, ct}` (the first also has `rs`) that the caller wraps as
 * `{dlne_e2e: sealed}`; counters are capped at 2^32 - 1 and must arrive exactly in order; bodies and payloads are
 * capped at 4 MiB.
 *
 * Errors are E2EError with a short `code` and a fixed message. They never contain plaintext, ciphertext or keys.
 */
import * as crypto from 'crypto';

export const ALGORITHM = 'x25519-hkdf-sha256-chacha20poly1305';
export const VERSION = 1;
export const WRAPPER_KEY = 'dlne_e2e';
export const MAX_BODY_BYTES = 4 * 1024 * 1024;
export const MAX_COUNTER = 0xffffffff;
/** Default padding of requests and response chunks. */
export const PAD_BLOCK = 256;

const HKDF_INFO = Buffer.from('dlne-e2e-v1', 'utf8');
const HKDF_INFO_STREAM = Buffer.from('dlne-e2e-v1-resp', 'utf8');
const AAD_REQUEST = Buffer.from('dlne-req', 'utf8');
const AAD_RESPONSE = Buffer.from('dlne-resp', 'utf8');
const END_MARKER = Buffer.from('[DONE]', 'utf8');
const KEY_LEN = 32;
const NONCE_LEN = 12;
const TAG_LEN = 16;
const RS_LEN = 16;
const SPKI_PREFIX = Buffer.from('302a300506032b656e032100', 'hex');
const PKCS8_PREFIX = Buffer.from('302e020100300506032b656e04220420', 'hex');
const ZERO_SHARED = Buffer.alloc(KEY_LEN);
const B64_RE = /^[A-Za-z0-9+/]*={0,2}$/;

export type E2ECode =
  | 'format' | 'version' | 'base64' | 'length' | 'size' | 'key' | 'auth' | 'json'
  | 'counter' | 'sequence' | 'closed' | 'truncated';

export class E2EError extends Error {
  readonly code: E2ECode;
  constructor(code: E2ECode, message: string) {
    super(message);
    this.name = 'E2EError';
    this.code = code;
  }
}

export interface SealedRequest {
  /** The whole request body to send: `{ dlne_e2e: { v, epk, nonce, ct } }`. */
  envelope: { dlne_e2e: { v: number; epk: string; nonce: string; ct: string } };
  /** Key for the response; keep it until the answer has been read, then drop it. */
  respKey: Buffer;
}

/** One sealed response object: the inner `{n, ct}`; the first one also carries `rs`. Send it as `{dlne_e2e: sealed}`. */
export interface SealedPayload {
  n: number;
  ct: string;
  rs?: string;
}

export interface PadOptions {
  /** Pad with trailing spaces up to a multiple of this many bytes (0 = off). Hides token lengths. */
  padBlock?: number;
}

// ------------------------------------------------------------------------------------------------
// Encoding helpers
// ------------------------------------------------------------------------------------------------

function b64d(
  value: unknown,
  field: string,
  opts: { exact?: number; min?: number; max?: number; tooBig?: E2ECode } = {},
): Buffer {
  if (typeof value !== 'string') throw new E2EError('base64', `${field} must be a base64 string`);
  const tooBig = opts.tooBig ?? 'length';
  const limit = opts.exact ?? opts.max;
  if (limit !== undefined && value.length > 4 * Math.ceil(limit / 3)) {
    throw new E2EError(tooBig, `${field} is too large`);
  }
  if (value.length % 4 !== 0 || !B64_RE.test(value)) throw new E2EError('base64', `${field} is not valid base64`);
  const raw = Buffer.from(value, 'base64');
  const again = Buffer.from(raw.toString('base64'), 'utf8');
  const given = Buffer.from(value, 'utf8');
  if (again.length !== given.length || !crypto.timingSafeEqual(again, given)) {
    throw new E2EError('base64', `${field} is not canonical base64`);
  }
  if (opts.exact !== undefined && raw.length !== opts.exact) throw new E2EError('length', `${field} has the wrong length`);
  if (opts.max !== undefined && raw.length > opts.max) throw new E2EError(tooBig, `${field} is too large`);
  if (opts.min !== undefined && raw.length < opts.min) throw new E2EError('length', `${field} has the wrong length`);
  return raw;
}

function checkKey(key: unknown, name: string): Buffer {
  if (!Buffer.isBuffer(key) && !(key instanceof Uint8Array)) throw new E2EError('key', `${name} must be ${KEY_LEN} bytes`);
  if (key.length !== KEY_LEN) throw new E2EError('key', `${name} must be ${KEY_LEN} bytes`);
  return Buffer.from(key);
}

function checkCounter(n: unknown): number {
  if (typeof n !== 'number' || !Number.isInteger(n) || n < 0 || n > MAX_COUNTER) {
    throw new E2EError('counter', 'bad response counter');
  }
  return n;
}

function pad(data: Buffer, padBlock: number | undefined): Buffer {
  const block = padBlock ?? 0;
  if (!Number.isInteger(block) || block < 0 || block > MAX_BODY_BYTES) throw new E2EError('length', 'padBlock must be 0 or a positive size');
  if (block <= 1) return data;
  const missing = Math.min((block - (data.length % block)) % block, Math.max(0, MAX_BODY_BYTES - data.length));
  return missing === 0 ? data : Buffer.concat([data, Buffer.alloc(missing, 0x20)]);
}

function checkPlaintext(data: Buffer, what: string): Buffer {
  if (data.length === 0) throw new E2EError('length', `${what} must not be empty`);
  if (data.length > MAX_BODY_BYTES) throw new E2EError('size', `${what} is too large`);
  return data;
}

function counterNonce(n: number): Buffer {
  const nonce = Buffer.alloc(NONCE_LEN);
  nonce.writeUInt32BE(n, NONCE_LEN - 4);
  return nonce;
}

// ------------------------------------------------------------------------------------------------
// Primitives
// ------------------------------------------------------------------------------------------------

function aeadSeal(key: Buffer, nonce: Buffer, aad: Buffer, plaintext: Buffer): Buffer {
  const cipher = crypto.createCipheriv('chacha20-poly1305', key, nonce, { authTagLength: TAG_LEN });
  cipher.setAAD(aad, { plaintextLength: plaintext.length });
  const body = Buffer.concat([cipher.update(plaintext), cipher.final()]);
  return Buffer.concat([body, cipher.getAuthTag()]);
}

function aeadOpen(key: Buffer, nonce: Buffer, aad: Buffer, data: Buffer, failure: string): Buffer {
  const body = data.subarray(0, data.length - TAG_LEN);
  const tag = data.subarray(data.length - TAG_LEN);
  try {
    const decipher = crypto.createDecipheriv('chacha20-poly1305', key, nonce, { authTagLength: TAG_LEN });
    decipher.setAAD(aad, { plaintextLength: body.length });
    decipher.setAuthTag(tag);
    return Buffer.concat([decipher.update(body), decipher.final()]);
  } catch {
    throw new E2EError('auth', failure);
  }
}

function rawPublicOf(priv: crypto.KeyObject): Buffer {
  const der = crypto.createPublicKey(priv).export({ type: 'spki', format: 'der' });
  return Buffer.from(der.subarray(der.length - KEY_LEN));
}

function privateFromRaw(raw: Buffer): crypto.KeyObject {
  try {
    return crypto.createPrivateKey({ key: Buffer.concat([PKCS8_PREFIX, raw]), format: 'der', type: 'pkcs8' });
  } catch {
    throw new E2EError('key', 'bad private key');
  }
}

function publicFromRaw(raw: Buffer): crypto.KeyObject {
  try {
    return crypto.createPublicKey({ key: Buffer.concat([SPKI_PREFIX, raw]), format: 'der', type: 'spki' });
  } catch {
    throw new E2EError('key', 'bad public key');
  }
}

/** X25519 + HKDF. `ephPub` and `memberPub` are the raw keys that go into the salt, in that order. */
function derive(priv: crypto.KeyObject, peerPubRaw: Buffer, ephPub: Buffer, memberPub: Buffer): { reqKey: Buffer; respKey: Buffer } {
  let shared: Buffer;
  try {
    shared = crypto.diffieHellman({ privateKey: priv, publicKey: publicFromRaw(peerPubRaw) });
  } catch {
    throw new E2EError('key', 'key agreement failed');
  }
  // A low-order point gives an all-zero secret that an attacker can compute. Refuse it.
  if (shared.length !== KEY_LEN || crypto.timingSafeEqual(shared, ZERO_SHARED)) throw new E2EError('key', 'key agreement failed');
  const okm = Buffer.from(crypto.hkdfSync('sha256', shared, Buffer.concat([ephPub, memberPub]), HKDF_INFO, 2 * KEY_LEN));
  return { reqKey: okm.subarray(0, KEY_LEN), respKey: okm.subarray(KEY_LEN) };
}

/** A fresh key pair: raw 32-byte private key and base64 raw 32-byte public key (same shape as the Python one). */
export function generateKeyPair(): { priv: Buffer; pubB64: string } {
  const { privateKey } = crypto.generateKeyPairSync('x25519');
  const der = privateKey.export({ type: 'pkcs8', format: 'der' });
  return { priv: Buffer.from(der.subarray(der.length - KEY_LEN)), pubB64: rawPublicOf(privateKey).toString('base64') };
}

// ------------------------------------------------------------------------------------------------
// Requests
// ------------------------------------------------------------------------------------------------

/**
 * Client side: encrypt a JSON request body for a member. Throws E2EError if the key or body is unusable.
 * The JSON is padded with trailing spaces to a multiple of `padBlock` (256 by default; 0 turns it off).
 */
export function sealRequest(body: object, memberPubB64: string, opts: PadOptions = {}): SealedRequest {
  const memberPub = b64d(memberPubB64, 'member key', { exact: KEY_LEN });
  if (body === null || typeof body !== 'object' || Array.isArray(body)) throw new E2EError('json', 'request body must be a JSON object');
  let json: string | undefined;
  try {
    json = JSON.stringify(body);
  } catch {
    throw new E2EError('json', 'request body is not serializable');
  }
  if (typeof json !== 'string') throw new E2EError('json', 'request body is not serializable');
  const plaintext = checkPlaintext(pad(Buffer.from(json, 'utf8'), opts.padBlock ?? PAD_BLOCK), 'request body');

  const eph = crypto.generateKeyPairSync('x25519');
  const ephPub = rawPublicOf(eph.privateKey);
  const { reqKey, respKey } = derive(eph.privateKey, memberPub, ephPub, memberPub);
  const nonce = crypto.randomBytes(NONCE_LEN);
  const ct = aeadSeal(reqKey, nonce, AAD_REQUEST, plaintext);
  return {
    envelope: { dlne_e2e: { v: VERSION, epk: ephPub.toString('base64'), nonce: nonce.toString('base64'), ct: ct.toString('base64') } },
    respKey: Buffer.from(respKey),
  };
}

/**
 * Member side (fake members in tests, tooling): decrypt a request. Accepts the wire body or its inner object.
 * No replay protection here; engine/e2e.py has it for the real member.
 */
export function openRequest(envelope: unknown, memberPriv: Buffer): { body: Record<string, unknown>; respKey: Buffer } {
  if (envelope === null || typeof envelope !== 'object' || Array.isArray(envelope)) throw new E2EError('format', 'not an end-to-end envelope');
  let inner = envelope as Record<string, unknown>;
  if (Object.prototype.hasOwnProperty.call(inner, WRAPPER_KEY)) {
    if (Object.keys(inner).length !== 1) throw new E2EError('format', 'unexpected field in envelope');
    const wrapped = inner[WRAPPER_KEY];
    if (wrapped === null || typeof wrapped !== 'object' || Array.isArray(wrapped)) throw new E2EError('format', 'not an end-to-end envelope');
    inner = wrapped as Record<string, unknown>;
  }
  if (inner.v !== VERSION) throw new E2EError('version', 'unsupported envelope version');
  const names = Object.keys(inner).sort().join(',');
  if (names !== 'ct,epk,nonce,v') throw new E2EError('format', 'envelope fields are wrong');

  const epk = b64d(inner.epk, 'epk', { exact: KEY_LEN });
  const nonce = b64d(inner.nonce, 'nonce', { exact: NONCE_LEN });
  const ct = b64d(inner.ct, 'ct', { min: TAG_LEN + 1, max: MAX_BODY_BYTES + TAG_LEN, tooBig: 'size' });

  const privRaw = checkKey(memberPriv, 'member private key');
  const priv = privateFromRaw(privRaw);
  const memberPub = rawPublicOf(priv);
  const { reqKey, respKey } = derive(priv, epk, epk, memberPub);
  const plaintext = aeadOpen(reqKey, nonce, AAD_REQUEST, ct, 'request authentication failed');
  let body: unknown;
  try {
    body = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(plaintext));
  } catch {
    throw new E2EError('json', 'request body is not valid JSON');
  }
  if (body === null || typeof body !== 'object' || Array.isArray(body)) throw new E2EError('json', 'request body must be a JSON object');
  return { body: body as Record<string, unknown>, respKey: Buffer.from(respKey) };
}

// ------------------------------------------------------------------------------------------------
// Responses
// ------------------------------------------------------------------------------------------------

/**
 * stream_key = HKDF-SHA256(ikm=resp_key, salt=rs, info="dlne-e2e-v1-resp", length=32): the key a response is actually
 * sealed with. `rs` is 16 random bytes the member picks for each response.
 */
export function deriveStreamKey(respKey: Buffer, rs: Buffer): Buffer {
  const key = checkKey(respKey, 'response key');
  if (!Buffer.isBuffer(rs) || rs.length !== RS_LEN) throw new E2EError('length', 'rs has the wrong length');
  return Buffer.from(crypto.hkdfSync('sha256', key, rs, HKDF_INFO_STREAM, KEY_LEN));
}

/**
 * Low level: decrypt one payload under exactly the key given (a stream key). Does not check order, rs or the end
 * marker; read a response with ResponseOpener instead. The result is the payload bytes exactly (trailing padding
 * spaces included; JSON.parse ignores them).
 */
export function openPayload(respKey: Buffer, n: number, ctB64: string): Buffer {
  const key = checkKey(respKey, 'response key');
  const counter = checkCounter(n);
  const ct = b64d(ctB64, 'ct', { min: TAG_LEN + 1, max: MAX_BODY_BYTES + TAG_LEN, tooBig: 'size' });
  return aeadOpen(key, counterNonce(counter), AAD_RESPONSE, ct, 'response authentication failed');
}

/**
 * Low level: seal one payload under exactly the key given (a stream key, see deriveStreamKey) with nonce = n.
 * Returns `{n, ct}` with no `rs` and no end marker; padding only if `padBlock` is set. The caller owns the counter and
 * must never reuse one for a key (use ResponseSealer).
 */
export function sealPayload(respKey: Buffer, n: number, payload: Buffer | string, opts: PadOptions = {}): SealedPayload {
  const key = checkKey(respKey, 'response key');
  const counter = checkCounter(n);
  const data = checkPlaintext(pad(typeof payload === 'string' ? Buffer.from(payload, 'utf8') : Buffer.from(payload), opts.padBlock), 'payload');
  return { n: counter, ct: aeadSeal(key, counterNonce(counter), AAD_RESPONSE, data).toString('base64') };
}

function isEndMarker(data: Buffer): boolean {
  let end = data.length;
  while (end > 0 && data[end - 1] === 0x20) end -= 1;
  return end === END_MARKER.length && data.subarray(0, end).equals(END_MARKER);
}

export interface ResponseSealerOptions extends PadOptions {
  /** Known-answer tests only: the 16-byte salt. Leave unset; a fresh random one is used. */
  rs?: Buffer;
}

/**
 * Member side of one response (fake members in tests). Picks a fresh random salt `rs`, seals with the stream key
 * derived from it, counts 0, 1, 2, ... so a nonce never repeats, pads every payload to `padBlock` (256) and ends a
 * stream with `finish()`. The first object returned also carries `rs`. A non-streamed answer is one `seal()` and
 * no `finish()`.
 */
export class ResponseSealer {
  private next = 0;
  private finished = false;
  private readonly key: Buffer;
  private readonly rs: Buffer;
  private readonly padBlock: number;
  constructor(respKey: Buffer, opts: ResponseSealerOptions = {}) {
    const key = checkKey(respKey, 'response key');
    this.rs = opts.rs === undefined ? crypto.randomBytes(RS_LEN) : opts.rs;
    this.key = deriveStreamKey(key, this.rs);
    this.padBlock = opts.padBlock ?? PAD_BLOCK;
    pad(Buffer.from('{}'), this.padBlock); // refuse a bad padBlock now, not on the first chunk
  }
  private sealNext(payload: Buffer | string, final: boolean): SealedPayload {
    if (this.finished) throw new E2EError('sequence', 'the response was already finished');
    if (this.next > MAX_COUNTER) throw new E2EError('counter', 'response counter exhausted');
    const sealed = sealPayload(this.key, this.next, payload, { padBlock: this.padBlock });
    const out: SealedPayload = this.next === 0 ? { n: 0, rs: this.rs.toString('base64'), ct: sealed.ct } : sealed;
    this.next += 1;
    this.finished = final;
    return out;
  }
  seal(payload: Buffer | string): SealedPayload {
    if (isEndMarker(typeof payload === 'string' ? Buffer.from(payload, 'utf8') : Buffer.from(payload))) {
      throw new E2EError('format', 'a payload may not look like the end marker; use finish()');
    }
    return this.sealNext(payload, false);
  }
  /** The sealed end of a stream (plaintext `[DONE]`). Nothing can be sealed after it. */
  finish(): SealedPayload {
    return this.sealNext(END_MARKER, true);
  }
}

export interface ResponseOpenerOptions {
  /** false for a non-streamed answer: a single sealed object (n = 0) and no end marker. Default true. */
  streamed?: boolean;
}

/**
 * Client side of one response. Insists on the first object carrying `rs`, on the counter 0, 1, 2, ... exactly in
 * order, and on a sealed end. After any failure it refuses everything: a bad chunk ends the stream.
 *
 *   const opener = new ResponseOpener(respKey);        // { streamed: false } for a non-streamed answer
 *   const data = opener.open(sealed);                  // Buffer (JSON, maybe with trailing spaces), or null
 *   ...                                                // null is the sealed end; opener.finished is then true
 *   opener.close();                                    // at the plain [DONE] or at EOF: throws E2EError
 *                                                      // 'truncated' if the sealed end never came
 *
 * `sealed` is the inner object of a `data:` line (or the whole `{dlne_e2e: ...}`).
 */
export class ResponseOpener {
  private next = 0;
  private done = false;
  private dead = false;
  private streamKey: Buffer | null = null;
  private readonly respKey: Buffer;
  private readonly streamed: boolean;
  constructor(respKey: Buffer, opts: ResponseOpenerOptions = {}) {
    this.respKey = checkKey(respKey, 'response key');
    this.streamed = opts.streamed !== false;
  }
  /** How many objects were opened so far. */
  get count(): number {
    return this.next;
  }
  /** True once the sealed end arrived (or the single object of a non-streamed answer was opened). */
  get finished(): boolean {
    return this.done;
  }
  open(sealed: unknown): Buffer | null {
    if (this.dead) throw new E2EError('closed', 'response stream already failed');
    try {
      return this.openNext(sealed);
    } catch (err) {
      this.dead = true;
      throw err;
    }
  }
  private openNext(sealed: unknown): Buffer | null {
    if (this.done) throw new E2EError('sequence', 'data after the end of the response');
    if (sealed === null || typeof sealed !== 'object' || Array.isArray(sealed)) throw new E2EError('format', 'not a sealed response object');
    let obj = sealed as Record<string, unknown>;
    if (Object.prototype.hasOwnProperty.call(obj, WRAPPER_KEY)) {
      const inner = obj[WRAPPER_KEY];
      if (Object.keys(obj).length !== 1 || inner === null || typeof inner !== 'object' || Array.isArray(inner)) {
        throw new E2EError('format', 'not a sealed response object');
      }
      obj = inner as Record<string, unknown>;
    }
    const first = this.next === 0;
    if (Object.keys(obj).sort().join(',') !== (first ? 'ct,n,rs' : 'ct,n')) throw new E2EError('format', 'sealed object fields are wrong');
    if (typeof obj.n !== 'number' || obj.n !== this.next) throw new E2EError('sequence', 'response chunk is out of order, repeated or missing');
    if (first) this.streamKey = deriveStreamKey(this.respKey, b64d(obj.rs, 'rs', { exact: RS_LEN }));
    const data = openPayload(this.streamKey as Buffer, obj.n, obj.ct as string);
    this.next += 1;
    if (!this.streamed) {
      this.done = true;
      return data;
    }
    if (isEndMarker(data)) {
      this.done = true;
      return null;
    }
    return data;
  }
  /**
   * Call when the plain `data: [DONE]` arrives or the connection ends. Throws E2EError 'truncated' unless the sealed
   * end was seen (the answer may have been cut short), or 'closed' if the stream had already failed.
   */
  close(): void {
    if (this.dead) throw new E2EError('closed', 'response stream already failed');
    if (!this.done) throw new E2EError('truncated', 'the response ended before its sealed end');
  }
}
