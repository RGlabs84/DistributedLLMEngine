/**
 * Who the coordinator says this caller is (GET /v1/me), and whether the agent's turn limit is lifted
 * (docs/release-2.7-contract.md sections 3 and 7). The agent loop asks at the start of a run, the panel asks to show
 * "Priority: first in line" and the turn-limit switch.
 *
 * The answer is asked through the same client as the status bar (so with the same headers: the API key, or the worker
 * token while this computer contributes), kept for a few minutes, and asked again when the API key or the server
 * address changes. A coordinator older than 2.7.0 has no /v1/me and answers 404: the tier is then null, which means
 * "unknown", and nothing that depends on it switches on.
 */
import { createHash } from 'crypto';
import * as vscode from 'vscode';
import type { DlneClient } from '../client/dlneClient';

export type KeyTier = 'key' | 'host' | 'gpu' | 'cpu' | 'trial' | 'open';

const TIERS: ReadonlySet<string> = new Set(['key', 'host', 'gpu', 'cpu', 'trial', 'open']);

/** How long an answer is trusted before it is asked again. */
export const KEY_TIER_REFRESH_MS = 5 * 60 * 1000;
/** After a failed ask (no answer, or a server error) the next try comes sooner. */
const RETRY_AFTER_FAILURE_MS = 30 * 1000;

/** The client the extension uses for status; extension.ts hands it over. Until then nothing is asked (tier unknown). */
let client: Pick<DlneClient, 'whoAmI' | 'getServerUrl'> | undefined;

interface Cached {
  tier: KeyTier | null;
  /** Which coordinator and which credential this answer is for (the key itself is never kept). */
  who: string;
  at: number;
  /** How long it stays good. */
  ttl: number;
}

let cached: Cached | undefined;
let inFlight: { who: string; promise: Promise<KeyTier | null> } | undefined;
const listeners = new Set<(tier: KeyTier | null) => void>();
let started = false;
let timer: ReturnType<typeof setInterval> | undefined;
let settingListener: vscode.Disposable | undefined;

/** The address and a fingerprint of the API key: an answer is only good for the pair it was asked with. */
function whoIsAsking(): string | null {
  if (!client) return null;
  const config = vscode.workspace.getConfiguration('dlne');
  const key = String(config.get<string>('apiKey', '') ?? '').trim();
  const fingerprint = createHash('sha256').update(key).digest('hex').slice(0, 16);
  return `${client.getServerUrl()}|${fingerprint}`;
}

/**
 * Hands over the client the extension already uses for status, so /v1/me is asked with exactly the headers a chat
 * request carries. Also starts the refresh: every few minutes, and at once when the API key or the server address
 * changes. Dispose it with the extension.
 */
export function bindKeyTierClient(statusClient: Pick<DlneClient, 'whoAmI' | 'getServerUrl'>): vscode.Disposable {
  client = statusClient;
  start();
  return { dispose: stop };
}

function start(): void {
  if (started) return;
  started = true;
  timer = setInterval(() => { void refreshKeyTier(); }, KEY_TIER_REFRESH_MS);
  timer.unref?.();       // a timer must never keep the editor's host (or a test) alive
  if (typeof vscode.workspace.onDidChangeConfiguration === 'function') {
    settingListener = vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration('dlne.apiKey') || e.affectsConfiguration('dlne.serverUrl')) void refreshKeyTier();
    });
  }
}

function stop(): void {
  started = false;
  if (timer) clearInterval(timer);
  timer = undefined;
  settingListener?.dispose();
  settingListener = undefined;
}

/** Tells the listeners, and only when the answer changed. */
function remember(next: Cached): void {
  const before = cached?.tier ?? null;
  cached = next;
  if (before !== next.tier) {
    for (const listener of [...listeners]) {
      try { listener(next.tier); } catch { /* a listener's fault is not ours */ }
    }
  }
}

async function ask(who: string): Promise<KeyTier | null> {
  const reply = await client!.whoAmI();
  const named = reply.status === 200 ? reply.body?.tier : undefined;
  if (typeof named === 'string' && TIERS.has(named)) {
    remember({ tier: named as KeyTier, who, at: Date.now(), ttl: KEY_TIER_REFRESH_MS });
    return named as KeyTier;
  }
  if (reply.status === 0 || reply.status >= 500) {
    // No answer: what was known about this same caller stays, and it is asked again soon.
    const known = cached?.who === who ? cached.tier : null;
    remember({ tier: known, who, at: Date.now(), ttl: RETRY_AFTER_FAILURE_MS });
    return known;
  }
  // 404 is a coordinator from before 2.7.0; anything else (a 403, an answer without a tier) is not an answer either.
  remember({ tier: null, who, at: Date.now(), ttl: KEY_TIER_REFRESH_MS });
  return null;
}

/** Asks the coordinator again now, whatever is kept. Never throws; null for unknown. */
export async function refreshKeyTier(): Promise<KeyTier | null> {
  try {
    const who = whoIsAsking();
    if (who === null) return null;
    if (inFlight?.who === who) return await inFlight.promise;
    const promise = ask(who).finally(() => { if (inFlight?.promise === promise) inFlight = undefined; });
    inFlight = { who, promise };
    return await promise;
  } catch {
    return null;
  }
}

/** null: unknown, or a coordinator older than 2.7.0. */
export async function getKeyTier(): Promise<KeyTier | null> {
  try {
    const who = whoIsAsking();
    if (who === null) return null;
    if (cached && cached.who === who && Date.now() - cached.at < cached.ttl) return cached.tier;
  } catch {
    return null;
  }
  return refreshKeyTier();
}

/** dlne.agent.unlimitedTurns is on and the coordinator confirms a key holder (or the host). */
export async function unlimitedTurnsActive(): Promise<boolean> {
  if (!vscode.workspace.getConfiguration('dlne').get<boolean>('agent.unlimitedTurns', true)) return false;
  const tier = await getKeyTier();
  return tier === 'key' || tier === 'host';
}

export function onKeyTierChanged(listener: (tier: KeyTier | null) => void): vscode.Disposable {
  listeners.add(listener);
  return { dispose: () => { listeners.delete(listener); } };
}

/** For tests: forget everything kept and stop the refresh. */
export function resetKeyTierForTests(): void {
  stop();
  client = undefined;
  cached = undefined;
  inFlight = undefined;
  listeners.clear();
}
