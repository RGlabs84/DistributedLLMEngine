'use strict';
/**
 * A mock coordinator with coding members, for the tests of the client's request pipeline
 * (docs/coding-members.md sections 5, 6 and 10).
 *
 * The member side of end-to-end encryption is written out here from the contract text (section 6 and its
 * "Version 1 additions": per-response salt, sealed end, padding), not taken from the extension's own module,
 * so a test also checks the client against the contract. Requests are opened with the extension's
 * `openRequest` (a member needs that much of the shared module).
 */
const http = require('node:http');
const crypto = require('node:crypto');

const e2e = require('../../dist/privacy/e2e');

// ---- the member's side of the answer ------------------------------------------------------------------

const PAD = 256;

function pad(buffer) {
  const missing = (PAD - (buffer.length % PAD)) % PAD;
  return missing ? Buffer.concat([buffer, Buffer.alloc(missing, 0x20)]) : buffer;
}

function counterNonce(n) {
  const nonce = Buffer.alloc(12);
  nonce.writeUInt32BE(n, 8);
  return nonce;
}

function aeadSeal(key, nonce, aad, plaintext) {
  const cipher = crypto.createCipheriv('chacha20-poly1305', key, nonce, { authTagLength: 16 });
  cipher.setAAD(aad, { plaintextLength: plaintext.length });
  return Buffer.concat([cipher.update(plaintext), cipher.final(), cipher.getAuthTag()]);
}

function aeadOpen(key, nonce, aad, data) {
  const body = data.subarray(0, data.length - 16);
  const decipher = crypto.createDecipheriv('chacha20-poly1305', key, nonce, { authTagLength: 16 });
  decipher.setAAD(aad, { plaintextLength: body.length });
  decipher.setAuthTag(data.subarray(data.length - 16));
  return Buffer.concat([decipher.update(body), decipher.final()]);
}

/** stream_key = HKDF-SHA256(ikm=resp_key, salt=rs, info="dlne-e2e-v1-resp", length=32) */
function streamKey(respKey, rs) {
  return Buffer.from(crypto.hkdfSync('sha256', respKey, rs, 'dlne-e2e-v1-resp', 32));
}

/** Seals one answer's payloads: counter 0, 1, 2 ..., `rs` on the first, a sealed end from finish(). */
class MemberSealer {
  constructor(respKey) {
    this.rs = crypto.randomBytes(16);
    this.key = streamKey(respKey, this.rs);
    this.n = 0;
  }
  seal(payload) {
    const bytes = pad(Buffer.from(typeof payload === 'string' ? payload : JSON.stringify(payload), 'utf8'));
    const sealed = { n: this.n, ct: aeadSeal(this.key, counterNonce(this.n), Buffer.from('dlne-resp'), bytes).toString('base64') };
    if (this.n === 0) sealed.rs = this.rs.toString('base64');
    this.n += 1;
    return sealed;
  }
  finish() {
    return this.seal('[DONE]');
  }
}

/**
 * The reader of section 6, for the day the extension's ResponseOpener does not have the version 1 additions
 * yet (and as a second opinion afterwards). `open(sealed)` returns the bytes, or null for the sealed end.
 */
class ContractOpener {
  constructor(respKey) {
    this.respKey = respKey;
    this.next = 0;
    this.key = null;
    this.finished = false;
    this.dead = false;
  }
  open(sealed) {
    if (this.dead) throw new Error('closed');
    try {
      if (this.finished) throw new Error('data after the end');
      if (sealed.n !== this.next) throw new Error('sequence');
      if (this.next === 0) {
        if (typeof sealed.rs !== 'string') throw new Error('missing rs');
        this.key = streamKey(this.respKey, Buffer.from(sealed.rs, 'base64'));
      }
      const bytes = aeadOpen(this.key, counterNonce(sealed.n), Buffer.from('dlne-resp'), Buffer.from(sealed.ct, 'base64'));
      this.next += 1;
      if (bytes.toString('utf8').trim() === '[DONE]') {
        this.finished = true;
        return null;
      }
      return bytes;
    } catch (err) {
      this.dead = true;
      throw err;
    }
  }
}

/** True when the extension's own ResponseOpener already speaks section 6's version 1 additions. */
function realOpenerIsCurrent() {
  const { ResponseOpener } = e2e;
  return typeof ResponseOpener === 'function' && ResponseOpener.prototype.open.length === 1;
}

/**
 * Until the extension's module has the additions, the client's reader is the contract's, so the client's own
 * logic (retries, cut streams, busy answers) is still tested. Returns what to call to put the original back.
 */
function useContractOpenerIfNeeded() {
  if (realOpenerIsCurrent()) return () => {};
  const original = e2e.ResponseOpener;
  e2e.ResponseOpener = ContractOpener;
  return () => {
    e2e.ResponseOpener = original;
  };
}

function createMember() {
  const { priv, pubB64 } = e2e.generateKeyPair();
  return {
    priv,
    pubB64,
    /** The request body as the member sees it: `{body, respKey}` (throws if it does not open). */
    open(envelope) {
      return e2e.openRequest(envelope, priv);
    },
  };
}

// ---- the coordinator -----------------------------------------------------------------------------------

function readBody(req) {
  return new Promise((resolve) => {
    let data = '';
    req.on('data', (chunk) => (data += chunk));
    req.on('end', () => resolve(data));
  });
}

const json = (res, status, body, headers = {}) => {
  res.writeHead(status, { 'Content-Type': 'application/json', ...headers });
  res.end(JSON.stringify(body));
};

/** An SSE chunk of the OpenAI streaming format. */
const chunk = (delta, finish) => ({ choices: [{ index: 0, delta, ...(finish ? { finish_reason: finish } : {}) }] });

/**
 * @param {object} opts
 * @param {object|null} opts.status          the /v1/cluster/status answer; null: the endpoint is missing (404)
 * @param {function} opts.route              (url, req) => {status, body}; default: 404, as an old coordinator
 * @param {function} opts.chat               (ctx) => void, handles POST /v1/chat/completions
 * @param {function} opts.completions        (ctx) => void, handles POST /v1/completions
 * Every request is recorded in `requests`: {method, path, query, headers, raw, body}.
 */
async function startCoordinator({ status, route, chat, completions } = {}) {
  const requests = [];
  const state = { status, route, chat, completions };
  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, 'http://x');
    const raw = await readBody(req);
    let body = null;
    try {
      body = raw ? JSON.parse(raw) : null;
    } catch {
      // not JSON
    }
    const ctx = { req, res, url, raw, body, headers: req.headers };
    requests.push({ method: req.method, path: url.pathname, query: Object.fromEntries(url.searchParams), headers: req.headers, raw, body });
    if (url.pathname === '/v1/cluster/status') {
      if (state.status === null) return json(res, 404, { detail: 'Not Found' });
      return json(res, 200, typeof state.status === 'function' ? state.status() : state.status);
    }
    if (url.pathname === '/v1/coding/route') {
      const answer = state.route ? state.route(url, req) : { status: 404, body: { detail: 'Not Found' } };
      return json(res, answer.status, answer.body);
    }
    if (url.pathname === '/v1/chat/completions' && state.chat) return state.chat(ctx);
    if (url.pathname === '/v1/completions' && state.completions) return state.completions(ctx);
    return json(res, 404, { detail: 'Not Found' });
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  return {
    url: `http://127.0.0.1:${server.address().port}`,
    requests,
    state,
    of: (path) => requests.filter((r) => r.path === path),
    close() {
      server.closeAllConnections();
      server.close();
    },
  };
}

/** A /v1/cluster/status answer. */
function statusBody(extra = {}) {
  return {
    status: 'Active',
    global_active_model: 'served.gguf',
    model_loading: { is_loading: false },
    total_vram_gb: 24,
    total_sysram_gb: 32,
    total_memory_gb: 56,
    active_workers_count: 2,
    active_leases_count: 0,
    is_locked_for_edits: false,
    server_version: '2.4.0',
    ...extra,
  };
}

const MEMBERS_ON = { coding_members: { enabled: true, members: 1, ready: 1, slots: 2, busy: 0 } };

/** The route answer for a member (what the coordinator hands out). */
const offer = (member, route = 'route-1') => ({
  status: 200,
  body: { route, pubkey: member.pubB64, alg: 'x25519-hkdf-sha256-chacha20poly1305' },
});

/** Sends a plain SSE answer. */
function plainStream(res, pieces, { done = true } = {}) {
  res.writeHead(200, { 'Content-Type': 'text/event-stream' });
  for (const delta of pieces) {
    res.write(`data: ${JSON.stringify(chunk(typeof delta === 'string' ? { content: delta } : delta))}\n\n`);
  }
  res.write(`data: ${JSON.stringify(chunk({}, 'stop'))}\n\n`);
  if (done) res.write('data: [DONE]\n\n');
  res.end();
}

/**
 * What the member does with a sealed request: opens it, and answers `pieces` sealed. Returns the opened body.
 * `opts.end` false leaves out the sealed end (a cut stream); `opts.tamper` flips a bit in chunk n;
 * `opts.plain` answers in the clear (a member that breaks the contract).
 */
function memberAnswers(ctx, member, pieces, opts = {}) {
  const { body, respKey } = member.open(ctx.body);
  const { res } = ctx;
  res.writeHead(200, { 'Content-Type': 'text/event-stream' });
  const sealer = new MemberSealer(respKey);
  const write = (payload) => {
    if (opts.plain) {
      res.write(`data: ${JSON.stringify(payload)}\n\n`);
      return;
    }
    const sealed = sealer.seal(payload);
    if (opts.tamper === sealed.n) {
      const ct = Buffer.from(sealed.ct, 'base64');
      ct[0] ^= 1;
      sealed.ct = ct.toString('base64');
    }
    res.write(`data: ${JSON.stringify({ dlne_e2e: sealed })}\n\n`);
  };
  for (const delta of pieces) write(chunk(typeof delta === 'string' ? { content: delta } : delta));
  write(chunk({}, 'stop'));
  if (opts.end !== false && !opts.plain) {
    res.write(`data: ${JSON.stringify({ dlne_e2e: sealer.finish() })}\n\n`);
  }
  res.write('data: [DONE]\n\n');
  res.end();
  return body;
}

/** A member that opens the request and then goes away: the stream ends with a plain [DONE] and nothing sealed. */
function memberCuts(ctx, member) {
  const { body } = member.open(ctx.body);
  ctx.res.writeHead(200, { 'Content-Type': 'text/event-stream' });
  ctx.res.end('data: [DONE]\n\n');
  return body;
}

/** A member's non-streamed answer: one sealed object, n = 0, with rs. */
function memberAnswersWhole(ctx, member, answer) {
  const { body, respKey } = member.open(ctx.body);
  json(ctx.res, 200, { dlne_e2e: new MemberSealer(respKey).seal(answer) });
  return body;
}

module.exports = {
  ContractOpener, MemberSealer, MEMBERS_ON, chunk, createMember, json, memberAnswers, memberAnswersWhole, memberCuts, offer, plainStream,
  readBody, realOpenerIsCurrent, startCoordinator, statusBody, useContractOpenerIfNeeded,
};
