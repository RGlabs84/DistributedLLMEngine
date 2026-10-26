/**
 * "The swarm is busy": what the coordinator can tell a waiting request, and what the user is shown.
 * No vscode import, so tests load it straight from dist/.
 *
 * The coordinator hands out the coding engine's slots through a priority queue (engine/priority.py).
 * A request that finds every slot taken waits there; DLNE's rule is that it never waits in silence.
 * The place in line reaches the client in one shape, the `dlne_queue` object, carried by whichever of
 * three channels the coordinator offers (documented in docs/coding-members.md section 10):
 *
 *   1. in the stream: while the request waits, the coordinator answers 200 text/event-stream at once and
 *      sends `data: {"dlne_queue": {...}}` frames, then the real chunks (no ticket is needed);
 *   2. as a refusal: HTTP 503 with `Retry-After` and a JSON body `{"detail": "...", "dlne_queue": {...}}`;
 *      the client retries, presenting `ticket` as `X-DLNE-Ticket` to keep its place;
 *   3. nothing: a coordinator that says nothing leaves the client to read `coding_queue` from
 *      /v1/cluster/status (served today) and to estimate its place from that.
 *
 * Wire names are snake_case (`in_use`, `eta_seconds`); inside the client they are camelCase.
 */

export interface QueueInfo {
  /** 1 = next to get a slot. */
  position?: number;
  /** Requests waiting, this one included. */
  waiting?: number;
  /** The coding engine's slots, and how many are taken. */
  slots?: number;
  inUse?: number;
  etaSeconds?: number;
  /** Opaque; sent back as X-DLNE-Ticket on a retry to keep the place. */
  ticket?: string;
}

export type QueueSource = 'stream' | 'response' | 'status';

/** What the UI is told: the numbers, where they came from, and how long this request has waited. */
export interface QueueUpdate extends QueueInfo {
  source: QueueSource;
  waitedMs: number;
  /** The request's priority class as far as the client can tell (0 = GPU contributors / keys, 1 = CPU, 2 = free trial). */
  priority: number;
}

/** The part of /v1/cluster/status that describes the coding queue (counts only, no identities). */
export interface CodingQueueStats {
  slots: number;
  in_use: number;
  waiting: number;
  waiting_by_priority?: Record<string, number>;
}

function count(value: unknown, max = 1_000_000): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= max ? Math.floor(value) : undefined;
}

/** A `dlne_queue` object from the wire, or null when `raw` is not one. Bad fields are dropped, never trusted. */
export function parseQueueInfo(raw: unknown): QueueInfo | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    return null;
  }
  const o = raw as Record<string, unknown>;
  const info: QueueInfo = {};
  const position = count(o.position);
  if (position !== undefined && position >= 1) info.position = position;
  const waiting = count(o.waiting);
  if (waiting !== undefined) info.waiting = waiting;
  const slots = count(o.slots);
  if (slots !== undefined && slots >= 1) info.slots = slots;
  const inUse = count(o.in_use);
  if (inUse !== undefined) info.inUse = inUse;
  const eta = count(o.eta_seconds, 86_400);
  if (eta !== undefined) info.etaSeconds = eta;
  if (typeof o.ticket === 'string' && /^[A-Za-z0-9._~-]{1,200}$/.test(o.ticket)) info.ticket = o.ticket;
  return info;
}

/** The `dlne_queue` of a refusal body (503/429), or null: a refusal without one is not a "busy" answer. */
export function queueFromBody(body: string): QueueInfo | null {
  try {
    const parsed = JSON.parse(body);
    return parsed && typeof parsed === 'object' ? parseQueueInfo((parsed as any).dlne_queue) : null;
  } catch {
    return null;
  }
}

/** `coding_queue` from a /v1/cluster/status answer; null when the coordinator does not report it. */
export function parseCodingQueue(status: unknown): CodingQueueStats | null {
  const q: any = (status as any)?.coding_queue;
  if (!q || typeof q !== 'object') {
    return null;
  }
  const slots = count(q.slots);
  const inUse = count(q.in_use);
  const waiting = count(q.waiting);
  if (slots === undefined || slots < 1 || inUse === undefined || waiting === undefined) {
    return null;
  }
  const byPriority: Record<string, number> = {};
  if (q.waiting_by_priority && typeof q.waiting_by_priority === 'object') {
    for (const [priority, n] of Object.entries(q.waiting_by_priority)) {
      const c = count(n);
      if (c !== undefined && /^\d{1,2}$/.test(priority)) byPriority[priority] = c;
    }
  }
  return { slots, in_use: inUse, waiting, waiting_by_priority: byPriority };
}

/** Every slot taken and somebody waiting: a request sent now queues. */
export function isBusy(stats: CodingQueueStats | null): stats is CodingQueueStats {
  return !!stats && stats.in_use >= stats.slots && stats.waiting > 0;
}

/**
 * The place in line, from counts alone. Everyone waiting at a better priority goes first, and so does
 * everyone already waiting at this request's own (arrival order). People who arrive later at the same
 * priority are behind it, so the figure only falls within one wait, as people ahead are served, instead
 * of rising with every newcomer; it does rise when a better-placed request arrives. The estimate is a
 * little pessimistic by design, and the UI says "about".
 */
export class PlaceEstimator {
  private sameAhead: number | null = null;

  estimate(stats: CodingQueueStats, priority: number): number {
    const breakdown = Object.entries(stats.waiting_by_priority || {});
    // Without a breakdown, all that is known is that `waiting` includes this request.
    if (breakdown.length === 0) {
      return Math.max(1, stats.waiting);
    }
    let better = 0;
    let same = 0;
    for (const [p, n] of breakdown) {
      if (Number(p) < priority) better += n;
      else if (Number(p) === priority) same = n;
    }
    this.sameAhead = this.sameAhead === null ? same : Math.min(this.sameAhead, same);
    return better + Math.max(1, this.sameAhead);
  }
}

/** A QueueUpdate from the status' counts (channel 3). */
export function queueFromStats(stats: CodingQueueStats, estimator: PlaceEstimator, priority: number, waitedMs: number): QueueUpdate {
  return {
    source: 'status',
    waitedMs,
    priority,
    position: estimator.estimate(stats, priority),
    waiting: stats.waiting,
    slots: stats.slots,
    inUse: stats.in_use,
  };
}

// ---- wording ------------------------------------------------------------------------------------------

export function formatWait(seconds: number): string {
  if (seconds < 90) return `${Math.max(1, Math.round(seconds))} s`;
  const minutes = Math.round(seconds / 60);
  return minutes < 90 ? `${minutes} min` : `${Math.round(minutes / 60)} h`;
}

const slotsPhrase = (q: QueueInfo) => (q.slots !== undefined ? `All ${q.slots} coding slots are busy` : 'The swarm is busy');
/** An estimate (channel 3) says "about". */
const placePhrase = (q: QueueUpdate) => (q.source === 'status' ? `about #${q.position}` : `#${q.position}`);

/** The first line: where the request stands. */
export function queueHeadline(q: QueueUpdate): string {
  return q.position === undefined
    ? `${slotsPhrase(q)}. Your request is waiting its turn.`
    : `${slotsPhrase(q)}. You are ${placePhrase(q)} in line.`;
}

/** The second line: the numbers behind it, and what the user can do about it. */
export function queueDetail(q: QueueUpdate): string {
  const facts: string[] = [];
  if (q.waiting !== undefined) facts.push(`${q.waiting} waiting`);
  if (q.etaSeconds !== undefined) facts.push(`about ${formatWait(q.etaSeconds)} to go`);
  else if (q.waitedMs >= 5000) facts.push(`you have waited ${formatWait(q.waitedMs / 1000)}`);
  const sentences: string[] = [];
  if (facts.length > 0) {
    const text = facts.join(', ');
    sentences.push(text.charAt(0).toUpperCase() + text.slice(1));
  }
  sentences.push(
    q.priority === 0
      ? 'You are in the first class, ahead of free-trial and CPU-pool users'
      : 'GPU contributors go first: lending your GPU skips the line'
  );
  return `${sentences.join('. ')}.`;
}

/** One line, for places that can show only one (a chat participant's progress line). */
export function queueProgressLine(q: QueueUpdate): string {
  const where = q.position !== undefined ? `You are ${placePhrase(q)} in line` : 'Your request is waiting its turn';
  const waiting = q.waiting !== undefined ? ` (${q.waiting} waiting)` : '';
  const eta = q.etaSeconds !== undefined ? `, about ${formatWait(q.etaSeconds)} to go` : '';
  const hint = q.priority === 0 ? '' : ' GPU contributors go first.';
  return `\u23f3 ${slotsPhrase(q)}. ${where}${waiting}${eta}.${hint}`;
}

/** The status bar's short form. */
export function queueStatusText(q: QueueUpdate): string {
  return q.position !== undefined ? `${q.source === 'status' ? '~' : ''}#${q.position} in line` : 'waiting for a slot';
}
