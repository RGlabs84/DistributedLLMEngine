/**
 * Asks the coordinator whether a coding member should serve a request (docs/coding-members.md section 5):
 * `GET /v1/coding/route?session=<id>&privacy=<mode>[&own=<trust id>[&prefer=smarter]][&need=<tokens>]` answers
 * `{route, pubkey, alg}` for a member or `{route: null}` for the coordinator's own engine (`own` names this machine's
 * own coding member, which the coordinator prefers while it is fast enough; `prefer=smarter`, from dlne.ownGpu, prefers
 * it instead whenever it runs a smarter model than the coordinator's engine; `need`, from 2.6.x, is how many tokens the
 * request will hold, so only a machine with that much context per request is offered). A coordinator from before
 * coding members answers 404, which is remembered for ten minutes so an old server is asked once, not before every
 * request. One from before `need` ignores it. No vscode import, so tests load it straight from dist/.
 */
import * as http from 'http';
import * as https from 'https';
import { URL } from 'url';

export const E2E_ALG = 'x25519-hkdf-sha256-chacha20poly1305';

export interface RouteOffer {
  /** Opaque and short-lived (10 minutes); sent back as X-DLNE-Route. */
  route: string;
  /** The member's X25519 public key, base64 of 32 raw bytes. */
  pubkey: string;
}

/** A coordinator that does not know the route is not asked again for this long. */
export const UNSUPPORTED_MS = 10 * 60 * 1000;
const LOOKUP_TIMEOUT_MS = 2500;
const MAX_CACHED = 64;

const ROUTE_ID = /^[A-Za-z0-9._~:-]{1,256}$/;
const BASE64 = /^[A-Za-z0-9+/]{43}=?$/;

// ---- how much context a request needs ------------------------------------------------------------------

/**
 * Characters per token for `need`. The same figure the agent's compaction starts from (DEFAULT_CHARS_PER_TOKEN in
 * agent/agentLoop.ts, docs/agent-memory.md 5.1), and the coordinator's own count: it measures a prompt at 3 characters
 * a token when it decides whether the prompt fits a slot. The agent's calibrated figure is not used here: it is
 * usually larger, which would ask for less room than the coordinator will count.
 */
export const NEED_CHARS_PER_TOKEN = 3.0;
/** `need` is rounded up to a multiple of this, so a conversation that grows by a few lines keeps one cache key. */
export const NEED_STEP = 4096;
/** The coordinator ignores a need above 10,000,000; this is the largest multiple of NEED_STEP below that. */
export const NEED_MAX = Math.floor(10_000_000 / NEED_STEP) * NEED_STEP;

/** A string's length, or 0 for anything else. */
function chars(value: unknown): number {
  return typeof value === 'string' ? value.length : 0;
}

/**
 * The characters a request's prompt holds, counted the way the agent's compaction counts them (messageChars in
 * agent/agentLoop.ts, plus the tools' definitions): every message's content and tool calls with 16 characters each
 * for the wrapping, the tools as JSON, and an autocomplete's prompt and suffix.
 */
export function promptChars(payload: Record<string, any>): number {
  let total = chars(payload.prompt) + chars(payload.suffix);
  const messages = Array.isArray(payload.messages) ? payload.messages : [];
  for (const m of messages) {
    total += chars(m?.content) + 16;
    for (const tc of Array.isArray(m?.tool_calls) ? m.tool_calls : []) {
      total += chars(tc?.function?.name) + chars(tc?.function?.arguments) + 16;
    }
  }
  if (Array.isArray(payload.tools) && payload.tools.length > 0) {
    total += JSON.stringify(payload.tools).length;
  }
  return total;
}

/** `tokens` rounded up to a multiple of NEED_STEP and kept within NEED_MAX; 0 (nothing to send) for anything not above 0. */
export function roundNeed(tokens: number): number {
  if (!Number.isFinite(tokens) || tokens <= 0) {
    return 0;
  }
  return Math.min(NEED_MAX, Math.ceil(tokens / NEED_STEP) * NEED_STEP);
}

/**
 * The tokens a request will hold: its prompt, estimated at NEED_CHARS_PER_TOKEN, and the room it asks for its answer
 * (`max_tokens`), rounded up to a multiple of 4,096. A 30,000-character agent prompt with 10,240 tokens of answer is
 * 10,000 + 10,240 = 20,240, sent as 20,480. 0 when there is nothing to estimate.
 */
export function requestNeed(payload: Record<string, any>): number {
  const answer = Number.isInteger(payload.max_tokens) && payload.max_tokens > 0 ? payload.max_tokens : 0;
  return roundNeed(Math.ceil(promptChars(payload) / NEED_CHARS_PER_TOKEN) + answer);
}

/** The member to use, or null for the local engine (also for anything that is not a well-formed offer). */
export function parseRouteOffer(body: unknown): RouteOffer | null {
  if (!body || typeof body !== 'object') {
    return null;
  }
  const o = body as Record<string, unknown>;
  if (typeof o.route !== 'string' || !ROUTE_ID.test(o.route)) {
    return null;
  }
  // 32 bytes of base64 is 43 characters and one '=' of padding (which some encoders leave off).
  if (typeof o.pubkey !== 'string' || !BASE64.test(o.pubkey) || Buffer.from(o.pubkey, 'base64').length !== 32) {
    return null;
  }
  if (o.alg !== undefined && o.alg !== E2E_ALG) {
    return null; // a scheme this client cannot speak: the plain path, never a guess
  }
  return { route: o.route, pubkey: o.pubkey };
}

/**
 * The coordinator's answer to a sealed request it cannot carry out: HTTP 503 with
 * `{"error": {"code": "dlne_reroute", "reason": "expired" | "gone" | "busy" | "failed" | "privacy"}}`. A sealed body
 * belongs to one member and is used once, so the only remedy is a new route and a new seal. Returns the reason, or
 * null for any other body (a 503 without this code still means "the model is loading").
 */
export function rerouteReason(body: string): string | null {
  try {
    const parsed = JSON.parse(body);
    const error = parsed && typeof parsed === 'object' ? (parsed as any).error : undefined;
    if (error && typeof error === 'object' && error.code === 'dlne_reroute') {
      return typeof error.reason === 'string' ? error.reason.slice(0, 40) : 'unknown';
    }
  } catch {
    // not JSON: not ours
  }
  return null;
}

function getJson(
  url: URL,
  headers: Record<string, string>,
  timeoutMs: number,
  agent?: http.Agent
): Promise<{ status: number; body: unknown }> {
  return new Promise((resolve, reject) => {
    const client = url.protocol === 'https:' ? https : http;
    const req = client.get(url, { headers, timeout: timeoutMs, agent }, (res) => {
      let data = '';
      res.on('data', (chunk) => {
        data += chunk;
        if (data.length > 65_536) {
          req.destroy(new Error('route answer too large'));
        }
      });
      res.on('end', () => {
        let body: unknown = null;
        try {
          body = JSON.parse(data);
        } catch {
          // not JSON: only the status matters
        }
        resolve({ status: res.statusCode ?? 0, body });
      });
      res.on('error', reject);
    });
    req.on('timeout', () => req.destroy(new Error('timed out')));
    req.on('error', reject);
  });
}

export class RouteLookup {
  private unsupportedUntil = new Map<string, number>();
  private cache = new Map<string, { offer: RouteOffer | null; until: number }>();

  /**
   * @param ttlMs how long this answer may be reused for the same session (0: ask every time, which is
   *   right for chat, where the router should see each turn; autocomplete passes a few seconds)
   * @param own the trust id of this machine's own coding member, when the worker is one (32 hex characters;
   *   anything else is not sent)
   * @param preferSmarter dlne.ownGpu is "smarter": ask for this machine's member whenever its model is smarter than
   *   the coordinator's, even if it writes more slowly. Only sent with `own`; alone it would mean nothing.
   * @param need the tokens the request will hold (requestNeed). Rounded up to a multiple of 4,096 here too, and part of
   *   the cache key, so a cached answer is only reused for a request of the same size. Not sent when it is not above 0.
   */
  async lookup(
    base: string,
    headers: Record<string, string>,
    session: string,
    privacy: string,
    ttlMs: number,
    agent?: http.Agent,
    own?: string,
    preferSmarter?: boolean,
    need?: number
  ): Promise<RouteOffer | null> {
    const now = Date.now();
    if ((this.unsupportedUntil.get(base) ?? 0) > now) {
      return null;
    }
    const mine = own && /^[0-9a-f]{32}$/.test(own) ? own : '';
    const prefer = mine && preferSmarter === true ? 'smarter' : '';
    const tokens = roundNeed(need ?? 0);
    const key = `${base}|${session}|${privacy}|${mine}|${prefer}|${tokens}`;
    if (ttlMs > 0) {
      const hit = this.cache.get(key);
      if (hit && hit.until > now) {
        return hit.offer;
      }
    }
    let reply: { status: number; body: unknown };
    try {
      const url = new URL(`${base}/v1/coding/route?session=${encodeURIComponent(session)}&privacy=${encodeURIComponent(privacy)}` +
        (mine ? `&own=${mine}` : '') + (prefer ? `&prefer=${prefer}` : '') + (tokens > 0 ? `&need=${tokens}` : ''));
      reply = await getJson(url, headers, LOOKUP_TIMEOUT_MS, agent);
    } catch {
      return null; // the network is struggling: the plain path now, and ask again next time
    }
    if (reply.status === 404 || reply.status === 405 || reply.status === 501) {
      this.unsupportedUntil.set(base, now + UNSUPPORTED_MS);
      return null;
    }
    if (reply.status !== 200) {
      return null; // 401/403/429/5xx: the request itself will say why
    }
    const offer = parseRouteOffer(reply.body);
    if (ttlMs > 0) {
      if (this.cache.size >= MAX_CACHED) {
        this.cache.clear();
      }
      this.cache.set(key, { offer, until: now + ttlMs });
    }
    return offer;
  }

  /** A member that stopped answering: forget what was learned about this coordinator's routes. */
  invalidate(base: string): void {
    for (const key of [...this.cache.keys()]) {
      if (key.startsWith(`${base}|`)) {
        this.cache.delete(key);
      }
    }
  }
}
