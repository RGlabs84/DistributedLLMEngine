/**
 * The adapter between the client and the privacy modules (docs/coding-members.md section 10): the only
 * file in the extension that imports policy.ts, redact.ts and e2e.ts, so the rest of the client never
 * depends on how they are made. It adds what a request pipeline needs on top of them:
 *
 * - a privacy mode that never stalls a request (a lookup that is slow or fails reads as "trusted"),
 * - redaction that fails closed (text that cannot be checked for secrets is withheld, not sent),
 * - an exclusion check that fails closed too,
 * - sealing that reports failure as `null`, so the caller falls back to the plain path.
 */
import * as vscode from 'vscode';
import { ResponseOpener, sealRequest } from './e2e';
import { createPrivacyStatus, getPrivacyMode, PrivacyMode, WorkspaceFolderLike } from './policy';
import { isExcluded, redactSecrets } from './redact';

export { createPrivacyStatus, getPrivacyMode };
export type { PrivacyMode };
// The redaction hooks in the agent, autocomplete and chat (C2) import through here too.
export { REDACTION_MARK, excludedNotice, excludedReason, excludedReasonSkipping, filePathOf, isExcluded, redactSecrets, redactionNote } from './redact';

/** The header values the coordinator reads (contract section 7). */
const MODES: ReadonlySet<string> = new Set(['open', 'trusted', 'any']);

/** What replaces text that could not be checked for secrets. */
export const WITHHELD = '[content withheld by DLNE: it could not be checked for secrets]';

// ---- privacy mode -------------------------------------------------------------------------------

const MODE_TTL_MS = 10_000;
/** A mode that did not come back in time is "trusted", and is asked for again soon. */
const MODE_FALLBACK_TTL_MS = 5_000;
const MODE_WAIT_MS = 1_500;
const modeCache = new Map<string, { mode: PrivacyMode; until: number }>();

function folderKey(folder: WorkspaceFolderLike | undefined): string {
  if (typeof folder === 'string') return folder;
  const anyFolder: any = folder;
  const uri: any = anyFolder && 'uri' in anyFolder ? anyFolder.uri : anyFolder;
  return uri ? String(uri.fsPath ?? uri) : '';
}

/**
 * The privacy mode for a workspace folder: `open` (a public repository), `trusted` (the default for
 * everything else) or `any` (the user opted in). Answers are kept for a minute, and a lookup that is
 * slow, throws or returns something else is "trusted": a request is never held up, and never gets a
 * wider audience, because of it.
 */
export async function privacyModeFor(folder?: WorkspaceFolderLike): Promise<PrivacyMode> {
  const setting = String(vscode.workspace.getConfiguration('dlne').get<string>('privacy', 'auto'));
  const key = `${folderKey(folder)}|${setting}`;
  const cached = modeCache.get(key);
  if (cached && cached.until > Date.now()) {
    return cached.mode;
  }
  let timer: NodeJS.Timeout | undefined;
  let timedOut = false;
  const timeout = new Promise<'trusted'>((resolve) => {
    timer = setTimeout(() => {
      timedOut = true;
      resolve('trusted');
    }, MODE_WAIT_MS);
  });
  let mode: PrivacyMode = 'trusted';
  let answered = false;
  try {
    const found: unknown = await Promise.race([getPrivacyMode(folder), timeout]);
    if (typeof found === 'string' && MODES.has(found)) {
      mode = found as PrivacyMode;
      answered = !timedOut;
    }
  } catch {
    // a failed lookup is "trusted"
  } finally {
    if (timer) {
      clearTimeout(timer);
    }
  }
  modeCache.set(key, { mode, until: Date.now() + (answered ? MODE_TTL_MS : MODE_FALLBACK_TTL_MS) });
  return mode;
}

/** For tests and for a changed workspace: forget the modes learned so far. */
export function forgetPrivacyModes(): void {
  modeCache.clear();
}

/**
 * The status bar item that shows a workspace's privacy mode, kept only while the coordinator has coding
 * members (contract section 7, lead decisions). With members off the mode means nothing, so the item is not
 * made, and with it no public-repository lookup happens: nothing about the repository leaves the machine.
 */
export class PrivacyStatusGate implements vscode.Disposable {
  private item: { dispose(): unknown } | undefined;

  constructor(private readonly create: () => { dispose(): unknown } = () => createPrivacyStatus()) {}

  /** Call with every status answer: whether the coordinator says `coding_members.enabled`. */
  update(membersEnabled: boolean): void {
    if (membersEnabled && !this.item) {
      this.item = this.create();
    } else if (!membersEnabled && this.item) {
      this.item.dispose();
      this.item = undefined;
    }
  }

  get shown(): boolean {
    return this.item !== undefined;
  }

  dispose(): void {
    this.item?.dispose();
    this.item = undefined;
  }
}

// ---- redaction ------------------------------------------------------------------------------------

export function redactText(text: string): { text: string; count: number } {
  try {
    const result = redactSecrets(text);
    if (result && typeof result.text === 'string') {
      return { text: result.text, count: Number(result.count) || 0 };
    }
  } catch {
    // fall through: unchecked text is not sent
  }
  return { text: WITHHELD, count: 1 };
}

/**
 * A copy of a chat or completion body with secrets hidden from everything the model would be given:
 * message text, the arguments of earlier tool calls, and a completion prompt. The original is not
 * touched (the agent keeps its real history), and the same text always redacts to the same text, so
 * the engine's cache of the conversation prefix still matches from one turn to the next.
 */
export function redactPayload<T extends Record<string, any>>(payload: T): { payload: T; count: number } {
  let count = 0;
  const clean = (value: unknown): unknown => {
    if (typeof value !== 'string' || value === '') {
      return value;
    }
    const result = redactText(value);
    count += result.count;
    return result.text;
  };
  const out: Record<string, any> = { ...payload };
  if (Array.isArray(payload.messages)) {
    out.messages = payload.messages.map((message: any) => {
      if (!message || typeof message !== 'object') {
        return message;
      }
      const copy: Record<string, any> = { ...message, content: clean(message.content) };
      if (Array.isArray(message.tool_calls)) {
        copy.tool_calls = message.tool_calls.map((call: any) =>
          call && call.function
            ? { ...call, function: { ...call.function, arguments: clean(call.function.arguments) } }
            : call
        );
      }
      return copy;
    });
  }
  if (typeof payload.prompt === 'string') {
    out.prompt = clean(payload.prompt);
  }
  return { payload: out as T, count };
}

// ---- excluded files ---------------------------------------------------------------------------------

/**
 * Whether a file is one whose contents never leave the editor (.gitignore'd, .dlneignore, .env*, keys).
 * A file with no path on disk (an untitled or virtual document) has nothing to exclude. If the check
 * itself fails, the file counts as excluded.
 */
export function isExcludedFile(target: vscode.Uri | string): boolean {
  try {
    const uri: any = typeof target === 'string' ? vscode.Uri.file(target) : target;
    if (!uri || typeof uri.fsPath !== 'string' || !uri.fsPath) {
      return false;
    }
    return Boolean(isExcluded(uri));
  } catch {
    return true;
  }
}

// ---- sealing ---------------------------------------------------------------------------------------

export interface Sealed {
  /** What to send in place of the JSON body: `{"dlne_e2e": {...}}`. */
  envelope: { dlne_e2e: unknown };
  /** Opens the answer. */
  respKey: Buffer;
}

/**
 * The request sealed for a member's public key, or null if it could not be (then the plain path is used).
 * An envelope is good for one send to one member: every attempt seals afresh, nothing is ever resent.
 * (sealRequest pads the request itself.)
 */
export function sealForMember(body: object, memberPubB64: string): Sealed | null {
  try {
    const sealed: { envelope: any; respKey: Uint8Array } = sealRequest(body, memberPubB64);
    const envelope = sealed.envelope && typeof sealed.envelope === 'object' && 'dlne_e2e' in sealed.envelope
      ? sealed.envelope
      : { dlne_e2e: sealed.envelope };
    return { envelope, respKey: Buffer.from(sealed.respKey) };
  } catch {
    return null;
  }
}

/** One sealed object of an answer as it arrives: `{"n": 0, "ct": "...", "rs": "..."}` (rs on the first). */
export interface SealedChunk {
  n: number;
  ct: string;
  rs?: string;
}

/**
 * Reads one answer (contract section 6, "Version 1 additions"): `open` takes each sealed object in the order
 * it arrives and returns its bytes, or null for the sealed end; `finished` says the sealed end has been seen.
 * It throws on anything that does not authenticate or is out of order, and after that refuses everything.
 * A stream that ends without `finished` was cut, whatever else it carried.
 */
export interface AnswerOpener {
  open(sealed: SealedChunk): Buffer | null;
  readonly finished: boolean;
}

/**
 * A reader for one answer, keyed with the `respKey` of the request it answers. `streamed: false` is for a
 * non-streamed answer: one sealed object and no end marker.
 */
export function openerFor(respKey: Buffer, options: { streamed?: boolean } = {}): AnswerOpener {
  // typed here by the contract, not by the module: the adapter is the only place that depends on its shape
  return new (ResponseOpener as any)(respKey, options) as AnswerOpener;
}
