/**
 * What every chat, agent and autocomplete request goes through before it leaves the editor
 * (docs/coding-members.md section 10):
 *
 *   privacy mode  ->  X-DLNE-Privacy
 *   redaction     ->  secrets in messages, tool calls and prompts become [REDACTED]
 *   route lookup  ->  when the coordinator offers a coding member, the body is sealed for it
 *                     (end-to-end) and X-DLNE-Route names it; otherwise the plain body goes as it always did
 *
 * and the answer comes back through `SealedAnswer` when the request was sealed.
 * Nothing here ever logs a body.
 */
import * as http from 'http';
import * as vscode from 'vscode';
import { AnswerOpener, openerFor, privacyModeFor, PrivacyMode, redactPayload, sealForMember } from '../privacy';
import { RouteLookup } from './route';

export interface PreparedRequest {
  /** The bytes to send: the redacted JSON, or the sealed envelope. */
  body: string;
  /** Headers to add to the usual ones (never Authorization). */
  headers: Record<string, string>;
  /** Present when the body is sealed: opens the answer. */
  respKey?: Buffer;
  /** Present when a coding member was chosen. */
  route?: string;
  privacy: PrivacyMode;
  redactions: number;
}

export interface PipelineContext {
  sessionId: string;
  /**
   * The coordinator's status says it has coding members (`coding_members.enabled`). Only then is there a
   * privacy mode to work out, a header to send it in, or a route to ask for. Otherwise the request is the
   * plain one it always was, and nothing about the workspace's repository is looked up or sent.
   */
  membersEnabled: boolean;
  /** Skip the route lookup: the coordinator's own engine only (after members failed). */
  forcePlain?: boolean;
  /** A place in line to keep (X-DLNE-Ticket), from a busy answer. */
  ticket?: string;
  folder?: vscode.WorkspaceFolder;
  /** How long a route answer may be reused (see RouteLookup.lookup). */
  routeTtlMs: number;
}

export interface PipelineEnv {
  base: string;
  headers: () => Record<string, string>;
  routes: RouteLookup;
  agent?: http.Agent;
}

/** The request exactly as it was built, with nothing added: the shape a direct caller gets by default. */
export function plainRequest(payload: Record<string, any>): PreparedRequest {
  return { body: JSON.stringify(payload), headers: {}, privacy: 'trusted', redactions: 0 };
}

/**
 * Never throws: whatever goes wrong inside (a policy lookup, a route lookup, sealing) leaves the plain
 * path, which is the coordinator's own trusted engine.
 */
export async function prepareRequest(
  env: PipelineEnv,
  payload: Record<string, any>,
  ctx: PipelineContext
): Promise<PreparedRequest> {
  const redacted = redactPayload(payload);
  const headers: Record<string, string> = { 'X-DLNE-Session': ctx.sessionId };
  if (ctx.ticket) {
    headers['X-DLNE-Ticket'] = ctx.ticket;
  }
  const prepared: PreparedRequest = {
    body: JSON.stringify(redacted.payload),
    headers,
    privacy: 'trusted',
    redactions: redacted.count,
  };
  if (!ctx.membersEnabled) {
    return prepared;
  }
  // With members on, every coding request says how widely its code may travel (the coordinator never
  // upgrades it, and reads a missing header as "trusted").
  // No folder named means the workspace's only folder; with several open the policy answers "trusted".
  const privacy = await privacyModeFor(ctx.folder);
  prepared.privacy = privacy;
  headers['X-DLNE-Privacy'] = privacy;
  if (ctx.forcePlain) {
    return prepared;
  }
  try {
    const offer = await env.routes.lookup(env.base, env.headers(), ctx.sessionId, privacy, ctx.routeTtlMs, env.agent);
    if (offer) {
      const sealed = sealForMember(redacted.payload, offer.pubkey);
      if (sealed) {
        prepared.body = JSON.stringify(sealed.envelope);
        prepared.respKey = sealed.respKey;
        prepared.route = offer.route;
        headers['X-DLNE-Route'] = offer.route;
      }
    }
  } catch {
    // the plain body is already in place
  }
  return prepared;
}

// ---- the answer ---------------------------------------------------------------------------------------

/**
 * A coding member's answer that cannot be trusted: a chunk that failed to open or came out of order, an
 * answer that was not encrypted, or a stream that stopped before the sealed end (cut off).
 */
export class MemberAnswerError extends Error {
  /** Some of the answer had already reached the user, so the request cannot be quietly retried. */
  partial = false;

  constructor(message: string) {
    super(message);
    this.name = 'MemberAnswerError';
  }
}

export type Unsealed =
  | { kind: 'chunk'; chunk: any }       // act on this chunk
  | { kind: 'end' }                     // the sealed end: the answer is complete
  | { kind: 'ignore' }                  // nothing to act on
  | { kind: 'plain-content' };          // answer content that should have been sealed

/**
 * Reads one sealed answer: the `{"dlne_e2e": {n, ct, rs}}` frames of a stream (or the single object of a
 * non-streamed answer), in order, through the contract's ResponseOpener. The answer is complete only once
 * the sealed end has been opened (`finished`); a stream that just stops, or ends with a plain `[DONE]`, was cut.
 */
export class SealedAnswer {
  private readonly opener: AnswerOpener;

  constructor(respKey: Buffer, options: { streamed?: boolean } = {}) {
    this.opener = openerFor(respKey, options);
  }

  get finished(): boolean {
    return this.opener.finished === true;
  }

  /** `parsed` is the JSON of one `data:` line. Throws MemberAnswerError for a chunk that does not open. */
  handle(parsed: any): Unsealed {
    const sealed = parsed && typeof parsed === 'object' ? parsed.dlne_e2e : undefined;
    if (sealed === undefined) {
      // Not sealed. A frame with no answer in it (a keep-alive, a queue note) is harmless; one that
      // carries the answer is exactly what end-to-end encryption exists to prevent, so it is refused.
      return parsed && typeof parsed === 'object' && 'choices' in parsed ? { kind: 'plain-content' } : { kind: 'ignore' };
    }
    if (!sealed || typeof sealed !== 'object' || !Number.isInteger(sealed.n) || typeof sealed.ct !== 'string') {
      throw new MemberAnswerError('A coding member sent a malformed answer frame, so DLNE discarded the answer.');
    }
    let bytes: Buffer | null;
    try {
      bytes = this.opener.open(sealed);
    } catch {
      // Out of order, repeated, altered: the chunk is not shown and the stream is over.
      throw new MemberAnswerError('A coding member\'s answer failed its integrity check, so DLNE discarded it.');
    }
    if (bytes === null) {
      return { kind: 'end' };
    }
    try {
      return { kind: 'chunk', chunk: JSON.parse(bytes.toString('utf8')) }; // trailing padding spaces are fine
    } catch {
      return { kind: 'ignore' }; // authenticated, but not a JSON chunk (nothing in the stream needs it)
    }
  }
}

/** A non-streamed answer: the whole body is one sealed object (n = 0) and needs no end marker. */
export function openWholeAnswer(body: string, respKey: Buffer): any {
  let parsed: any;
  try {
    parsed = JSON.parse(body);
  } catch {
    throw new MemberAnswerError('A coding member sent an answer that is not valid, so DLNE discarded it.');
  }
  const result = new SealedAnswer(respKey, { streamed: false }).handle(parsed);
  if (result.kind === 'chunk') {
    return result.chunk;
  }
  throw new MemberAnswerError(
    result.kind === 'plain-content'
      ? 'A coding member answered without encrypting, so DLNE discarded the answer.'
      : 'A coding member sent an empty answer.'
  );
}
