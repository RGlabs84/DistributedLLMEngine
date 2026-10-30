import * as vscode from 'vscode';
import * as http from 'http';
import * as https from 'https';
import { resolveServerUrl } from '../serverUrl';
import { requestedModel } from './modelChoice';
import {
  MemberAnswerError, PreparedRequest, PipelineEnv, SealedAnswer, openWholeAnswer, plainRequest, prepareRequest,
} from './pipeline';
import { QueueInfo, QueueUpdate, parseCodingQueue, parseQueueInfo, queueFromBody, queueProgressLine } from './queue';
import { RouteLookup, rerouteReason } from './route';
import { editorSessionId } from './session';
import { DEFAULT_WAIT_TIMING, WaitTiming, WaitWatcher } from './waitWatcher';
import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';
import { URL } from 'url';
import type { ImageReply } from '../agent/imageTool';

export interface ModelLoadingTelemetry {
  is_loading: boolean;
  model?: string;
  phase?: string;
  loaded_gb?: number;
  total_gb?: number;
  progress_pct?: number;
  network_tx_gb?: number;
  speed_mb_s?: number;
  eta_seconds?: number;
  elapsed_seconds?: number;
}

export interface ClusterStatus {
  status: string;
  global_active_model: string | null;
  /** The coding model by name, from server 2.3.5; older servers do not send it. */
  coding_model?: { name?: string | null } | null;
  running_models: string[];
  recommended_tier?: string;
  recommended_model?: string;
  total_vram_gb: number;
  total_sysram_gb: number;
  total_memory_gb: number;
  active_workers_count: number;
  active_leases_count: number;
  is_locked_for_edits: boolean;
  is_swapping_models?: boolean;
  model_loading?: ModelLoadingTelemetry;
  server_version: string;
  /** The coding engine's slots and who waits for them (counts only). */
  coding_queue?: { slots: number; in_use: number; waiting: number; waiting_by_priority?: Record<string, number> };
  /** Coding members (docs/coding-members.md section 5); absent on a coordinator that predates them. */
  coding_members?: { enabled: boolean; members?: number; ready?: number; slots?: number; busy?: number };
  /**
   * The swarm's public rules (server 2.4.8). From 2.4.9, `coding_context_tokens` is the smallest per-request
   * context among what can serve a coding request; the agent sizes its history by it (docs/agent-memory.md 5.1).
   * From 2.6.x, `coding_context_tokens_max` is the largest, and the agent sizes its history by that instead: every route
   * lookup says how big its request is (`need`, client/route.ts), so a big one goes to a machine that can hold it.
   */
  policy?: { coding_context_tokens?: number; coding_context_tokens_max?: number; [rule: string]: unknown };
  /** What this coordinator offers beyond chat (2.6.1); `image_generation` gates the agent's generate_image tool. */
  capabilities?: { image_generation?: boolean; [capability: string]: unknown };
  /** While image generation is on: its queue and the estimate per size (informational; the tool polls the job). */
  image_generation?: { queue?: number; busy?: boolean; eta_seconds?: Record<string, number>; sizes?: string[]; default_size?: string };
}

/**
 * Whether a coding member may serve a request, as far as a status answer says: only when the coordinator
 * reports coding members as enabled. A coordinator that does not mention them (an older one), one that has
 * them off, and an unknown status all mean the plain path, with no route lookup at all.
 */
export function codingMembersMayServe(status: ClusterStatus | null | undefined): boolean {
  return status?.coding_members?.enabled === true;
}

export function formatLoadingTelemetry(ml?: ModelLoadingTelemetry): string {
  if (!ml || !ml.is_loading) {
    return 'Model ready';
  }
  const modelName = ml.model || 'the model';
  const pct = typeof ml.progress_pct === 'number' ? `${ml.progress_pct.toFixed(1)}%` : '0.0%';
  const gbPart =
    typeof ml.loaded_gb === 'number' && typeof ml.total_gb === 'number' && ml.total_gb > 0
      ? `${ml.loaded_gb.toFixed(2)} / ${ml.total_gb.toFixed(2)} GB`
      : '';
  const netPart =
    typeof ml.network_tx_gb === 'number' && ml.network_tx_gb > 0
      ? ` | RPC Sync: ${ml.network_tx_gb.toFixed(2)} GB`
      : '';
  const speedPart =
    typeof ml.speed_mb_s === 'number' && ml.speed_mb_s > 0
      ? ` @ ${ml.speed_mb_s.toFixed(1)} MB/s`
      : '';
  let etaPart = '';
  if (typeof ml.eta_seconds === 'number' && ml.eta_seconds > 0) {
    const mins = Math.floor(ml.eta_seconds / 60);
    const secs = ml.eta_seconds % 60;
    etaPart = mins > 0 ? ` | ETA: ${mins}m ${secs}s` : ` | ETA: ${secs}s`;
  } else if (typeof ml.elapsed_seconds === 'number' && ml.elapsed_seconds > 0) {
    etaPart = ` | Elapsed: ${ml.elapsed_seconds}s`;
  }
  return `⏳ Loading ${modelName}: ${pct}${gbPart ? ` (${gbPart})` : ''}${netPart}${speedPart}${etaPart}`;
}

export interface StreamedToolCall {
  id: string;
  name: string;
  arguments: string;
}

export interface StreamChatResult {
  content: string;
  toolCalls: StreamedToolCall[];
  /** The model's last finish_reason ("stop", "tool_calls", "length"…), when the stream gave one. "length" = cut off. */
  finishReason?: string;
  /** The prompt's size in tokens, when the stream reported it (see promptTokensOf). The agent sizes its history by it. */
  promptTokens?: number;
}

/** One attempt's answer, before streamChat decides whether to retry. */
type AttemptResult = StreamChatResult & { stillLoading: boolean };

/**
 * The prompt's token count, if a stream chunk carries one: an OpenAI-style `usage.prompt_tokens`, or llama.cpp's
 * `timings` (the tokens it read now plus those it found in its cache). Nothing is added to the request to get it:
 * the request's shape is fixed (docs/agent-memory.md section 1).
 */
export function promptTokensOf(chunk: any): number | undefined {
  const usage = Number(chunk?.usage?.prompt_tokens);
  if (Number.isFinite(usage) && usage > 0) return usage;
  const read = Number(chunk?.timings?.prompt_n);
  const cached = Number(chunk?.timings?.cache_n);
  const total = (Number.isFinite(read) && read > 0 ? read : 0) + (Number.isFinite(cached) && cached > 0 ? cached : 0);
  return total > 0 ? total : undefined;
}

export interface ChatMessage {
  role: 'system' | 'user' | 'assistant' | 'tool';
  content: string | null;
  name?: string;
  tool_call_id?: string;
  tool_calls?: Array<{
    id?: string;
    type: 'function';
    function: {
      name: string;
      arguments: string;
    };
  }>;
}

export interface ToolDefinition {
  type: 'function';
  function: {
    name: string;
    description: string;
    parameters: Record<string, any>;
  };
}

export interface ClusterStatusResult {
  status: 'ok' | 'forbidden' | 'unreachable' | 'error';
  data?: ClusterStatus;
  httpStatus?: number;
  message?: string;
}

/**
 * What to tell the user when a chat request is refused: the coordinator's own sentence (its
 * JSON `detail`, e.g. the CPU contributors' "You've used all 300 coding chats… frees up in
 * 42 minutes"), never the raw response body.
 */
/**
 * The coordinator lost its own engine during the request: its frame says `"dlne_error": "engine_dropped"` or
 * `"engine_unavailable"` (2.5.4), or an older coordinator wrote its "Node disconnected mid-response" alert. Not shown
 * as an answer: the agent sends the step again (2026-10-27: one such drop ended a task with the alert as its last word).
 */
export class EngineDropped extends Error {
  constructor(public readonly kind: string, public readonly partial: boolean) {
    super('The connection to the swarm\'s engine dropped before the answer was complete.');
    this.name = 'EngineDropped';
  }
}

const RETRIABLE_ENGINE_ERRORS = new Set(['engine_dropped', 'engine_unavailable']);
const OLD_DROP_ALERT = '[System Alert: Node disconnected mid-response.]';

/** A chat request the coordinator refused, with its HTTP status kept for callers to branch on. */
export class ServerRefusal extends Error {
  /** Set when the coordinator answered a sealed request with `dlne_reroute` (its reason): ask for a new route. */
  public reroute?: string;

  constructor(public readonly status: number | undefined, message: string) {
    super(message);
    this.name = 'ServerRefusal';
  }
}

/**
 * "Every coding slot is taken, come back": a 503 (or 429) whose body carries a `dlne_queue` object
 * (see queue.ts). Not a failure; the client shows the place in line and tries again.
 */
export class ServerBusy extends Error {
  constructor(public readonly queue: QueueInfo, public readonly retryAfterMs: number | undefined) {
    super('The swarm is busy.');
    this.name = 'ServerBusy';
  }
}

/**
 * The most an agent answer may be, in tokens. A file the agent writes arrives whole in one answer, so this is also
 * the biggest file it can write at once: 4096 (to 1.3.4) stopped at about 350 lines, and a game of 380 lines came
 * out cut off. 16K (1.3.5 to 1.4.1) left a 32K coding slot only 15,360 tokens for the prompt, so a long conversation was
 * compacted every few turns, and each compaction made the engine read the whole prompt again. 10K is still above the
 * longest answer seen (8,495 tokens in 1,805 requests) and gives the prompt 21,504 at 32K (agentLoop.promptBudget).
 * engine/canary.py writes its decoys with the same figure (a decoy must look exactly like this request), and
 * test/security-review.test.js holds the two together.
 */
export const AGENT_MAX_TOKENS = 10240;

/** What a streamChat caller can pass besides the messages. */
export interface StreamChatOptions {
  /** `X-DLNE-Session` for this conversation (an AgentLoop makes one per conversation). */
  sessionId?: string;
  workspaceFolder?: vscode.WorkspaceFolder;
  /**
   * Called with the place in line while every coding slot is taken, and with null once the answer starts.
   * A caller that passes this gets queue news here and not as a line of `onLoadingProgress`.
   */
  onQueue?: (queue: QueueUpdate | null) => void;
  /**
   * Called as a native tool call's arguments arrive, with everything received so far. A write_file call is the
   * whole file, which can take a minute to arrive; this lets the caller show that it is being written.
   */
  onToolCallProgress?: (name: string, argumentsSoFar: string) => void;
  /**
   * Caps the request's privacy mode (pipeline.ts capPrivacy): "trusted" keeps it off any volunteer's machine unless the
   * user chose anyMember. For requests that carry what someone else's tools read (VS Code's agent in the model picker).
   */
  maxPrivacy?: 'trusted';
}

/** How one autocomplete request ended: why it failed, or why the model stopped writing. */
export interface CompletionOutcome {
  /** The model's finish_reason. "length" means it stopped at max_tokens, possibly mid-line. */
  finishReason?: string;
  /** Why no suggestion came (an HTTP status, a timeout, a network error). Absent when the request was cancelled. */
  failure?: string;
}

/** A refusal that means "that coding member did not serve it", and a plain retry can: gone, moved, or down. */
function isMemberFailure(err: any): boolean {
  return err instanceof MemberAnswerError
    || (err instanceof ServerRefusal && ([404, 410, 421, 502, 504].includes(err.status ?? 0) || err.reroute !== undefined));
}

export function serverErrorMessage(status: number | undefined, body: string): string {
  try {
    const parsed = JSON.parse(body);
    if (parsed && typeof parsed.detail === 'string' && parsed.detail.trim()) {
      return parsed.detail.trim().slice(0, 600);
    }
  } catch {}
  const text = (body || '').trim();
  return text ? `Server returned ${status}: ${text.slice(0, 300)}` : `Server returned ${status}`;
}

export class DlneClient {
  private workerManager?: any;
  // Chat requests left in the free trial, as last reported by the coordinator; null until the
  // coordinator has sent the header (contributors and API-key holders never receive it).
  private quotaRemaining: number | null = null;

  public getQuotaRemaining(): number | null {
    return this.quotaRemaining;
  }

  private noteQuota(res: http.IncomingMessage): void {
    const raw = res.headers['x-dlne-quota-remaining'];
    const parsed = typeof raw === 'string' ? parseInt(raw, 10) : NaN;
    this.quotaRemaining = Number.isNaN(parsed) ? null : parsed;
  }

  public setWorkerManager(wm: any): void {
    this.workerManager = wm;
  }

  // ---- coding members and the queue (docs/coding-members.md section 10) -----------------------------

  private routes = new RouteLookup();
  private queueListeners: Array<(queue: QueueUpdate | null) => void> = [];
  private queueNow: QueueUpdate | null = null;
  /** Silence before the first "waiting" message and the gap after it; tests shorten them. */
  static waitTiming: WaitTiming = { ...DEFAULT_WAIT_TIMING };

  /**
   * Called whenever a request's place in line changes, and with null once nobody is queued, so the status
   * bar can show it without being asked. Returns the unsubscribe.
   */
  onQueue(listener: (queue: QueueUpdate | null) => void): () => void {
    this.queueListeners.push(listener);
    return () => {
      this.queueListeners = this.queueListeners.filter((l) => l !== listener);
    };
  }

  /** The place in line of the request that is waiting now, if one is. */
  currentQueue(): QueueUpdate | null {
    return this.queueNow;
  }

  private publishQueue(update: QueueUpdate | null, perCall?: (queue: QueueUpdate | null) => void, progress?: (text: string) => void): void {
    this.queueNow = update;
    for (const listener of this.queueListeners) {
      try { listener(update); } catch {}
    }
    if (perCall) {
      try { perCall(update); } catch {}
    } else if (update && progress) {
      progress(queueProgressLine(update));
    }
  }

  /**
   * Where this user's requests rank at the coordinator, as far as the client can tell: 0 for an API key or
   * a GPU contributor, 1 for a CPU contributor, 2 for the free trial (engine/priority.py). Only used to
   * estimate a place in line from the status' counts.
   */
  private queuePriority(): number {
    const wm = this.workerManager;
    if (wm?.isContributing?.()) {
      return wm.getStatus?.()?.cpuPool ? 1 : 0;
    }
    return vscode.workspace.getConfiguration('dlne').get<string>('apiKey', '').trim() ? 0 : 2;
  }

  /** The trust id of this machine's own coding member while the worker runs as one (DLNE_ROLE, contract section 10). */
  private ownTrustId(): string | undefined {
    const status = this.workerManager?.getStatus?.();
    return status?.role === 'coding' ? status.roleDetail?.trustId : undefined;
  }

  /**
   * dlne.ownGpu: "smarter" sends this user's requests to their own coding member whenever it runs a smarter model than
   * the coordinator, even if it writes more slowly; "faster" (the default) only while it is as fast. Read at every
   * lookup, so a change applies to the next request without a reload.
   */
  private prefersSmarterOwnGpu(): boolean {
    return vscode.workspace.getConfiguration('dlne').get<string>('ownGpu', 'faster') === 'smarter';
  }

  private pipelineEnv(base: string, agent?: http.Agent): PipelineEnv {
    return {
      base,
      headers: () => this.getHeaders(),
      routes: this.routes,
      agent,
      own: () => this.ownTrustId(),
      preferSmarter: () => this.prefersSmarterOwnGpu(),
    };
  }

  public getServerUrl(): string {
    return resolveServerUrl(vscode.workspace.getConfiguration('dlne'));
  }

  public getLocalWorkerToken(): string {
    const candidatePaths = [
      path.join(os.homedir(), '.dlne', 'config', '.dlne_token'),
      path.join(os.homedir(), '.dlne', '.dlne_token'),
    ];
    const folders = vscode.workspace.workspaceFolders;
    if (folders && folders.length > 0) {
      candidatePaths.unshift(path.join(folders[0].uri.fsPath, 'config', '.dlne_token'));
      candidatePaths.unshift(path.join(folders[0].uri.fsPath, '.dlne_token'));
    }
    for (const p of candidatePaths) {
      try {
        if (fs.existsSync(p)) {
          const content = fs.readFileSync(p, 'utf-8').trim();
          if (content) return content;
        }
      } catch {}
    }
    return '';
  }

  public getHeaders(forceWorkerToken: boolean = false): Record<string, string> {
    const config = vscode.workspace.getConfiguration('dlne');
    let apiKey = config.get<string>('apiKey', '').trim();
    const workerToken = this.getLocalWorkerToken();

    // Prioritize the live local worker token if forceWorkerToken is set, or if no API key
    // was entered, or if the worker is actively contributing compute (so an old/revoked
    // static dlne.apiKey in settings never blocks a legitimate contributing user).
    if (forceWorkerToken || !apiKey || (workerToken && this.workerManager?.isContributing())) {
      apiKey = workerToken || apiKey;
    }
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      'Accept': 'application/json'
    };
    if (apiKey) {
      headers['Authorization'] = `Bearer ${apiKey}`;
    }
    return headers;
  }

  async getClusterStatusDetailed(): Promise<ClusterStatusResult> {
    const base = this.getServerUrl();
    const url = new URL(`${base}/v1/cluster/status`);
    return new Promise((resolve) => {
      const client = url.protocol === 'https:' ? https : http;
      const req = client.get(url, { headers: this.getHeaders(), timeout: 10000 }, (res) => {
        let data = '';
        res.on('data', (chunk) => (data += chunk));
        res.on('end', () => {
          if (res.statusCode === 200) {
            try {
              const parsed = JSON.parse(data);
              this.lastStatus = { data: parsed, at: Date.now(), server: base };
              this.statusListeners.forEach((listener) => {
                try { listener(parsed); } catch {}
              });
              resolve({ status: 'ok', data: parsed, httpStatus: 200 });
            } catch (err: any) {
              resolve({ status: 'error', httpStatus: 200, message: err?.message || 'Invalid JSON' });
            }
          } else if (res.statusCode === 403) {
            let detail =
              'NON-CONTRIB BAN: You must actively run a DLNE Worker Node or provide a valid API key to access the network.';
            try {
              const parsed = JSON.parse(data);
              if (parsed.detail) detail = parsed.detail;
            } catch {}
            resolve({ status: 'forbidden', httpStatus: 403, message: detail });
          } else {
            resolve({ status: 'error', httpStatus: res.statusCode, message: `HTTP ${res.statusCode}` });
          }
        });
      });
      req.on('error', (err) => resolve({ status: 'unreachable', message: err.message }));
      req.on('timeout', () => {
        req.destroy();
        resolve({ status: 'unreachable', message: 'Connection timed out' });
      });
    });
  }

  /**
   * One JSON call to the coordinator's image endpoints (agent/imageTool.ts), with the same auth as a chat request. It
   * goes to the coordinator itself, never through the route lookup: an image prompt is never sent to a coding member.
   * Never throws: no answer is status 0 with a `detail`.
   */
  imageRequest(method: 'GET' | 'POST', apiPath: string, body?: unknown, signal?: AbortSignal): Promise<ImageReply> {
    const url = new URL(`${this.getServerUrl()}${apiPath}`);
    const payload = body === undefined ? undefined : JSON.stringify(body);
    return new Promise((resolve) => {
      const lib = url.protocol === 'https:' ? https : http;
      const headers = this.getHeaders();
      if (payload !== undefined) headers['Content-Length'] = String(Buffer.byteLength(payload));
      const req = lib.request(url, { method, headers, timeout: 30000, signal }, (res) => {
        const chunks: Buffer[] = [];
        res.on('data', (chunk) => chunks.push(chunk));
        res.on('end', () => {
          let parsed: any = {};
          try { parsed = JSON.parse(Buffer.concat(chunks).toString('utf-8')); } catch { /* not JSON: the status speaks */ }
          const wait = Number(res.headers['retry-after']);
          resolve({ status: res.statusCode ?? 0, body: parsed, retryAfter: Number.isFinite(wait) && wait > 0 ? wait : undefined });
        });
        res.on('error', (err) => resolve({ status: 0, body: { detail: err.message } }));
      });
      req.on('error', (err) => resolve({ status: 0, body: { detail: err.message } }));
      req.on('timeout', () => {
        req.destroy();
        resolve({ status: 0, body: { detail: 'The coordinator did not answer in time.' } });
      });
      if (payload !== undefined) req.write(payload);
      req.end();
    });
  }

  async getClusterStatus(): Promise<ClusterStatus | null> {
    const res = await this.getClusterStatusDetailed();
    return res.data || null;
  }

  // The status bar polls /v1/cluster/status every 10 s through this client; a chat turn reuses a
  // recent answer instead of spending a round trip on its own before every request.
  private lastStatus: { data: ClusterStatus; at: number; server: string } | null = null;
  private static readonly STATUS_REUSE_MS = 15000;

  private async recentClusterStatus(): Promise<ClusterStatus | null> {
    const s = this.lastStatus;
    if (s && s.server === this.getServerUrl() && Date.now() - s.at < DlneClient.STATUS_REUSE_MS) {
      return s.data;
    }
    return this.getClusterStatus();
  }

  private statusListeners: Array<(status: ClusterStatus) => void> = [];

  /**
   * Called with every successful /v1/cluster/status answer (the status bar polls it every 10 s),
   * so the sidebar can show the live model without polling on its own. Returns the unsubscribe.
   */
  onClusterStatus(listener: (status: ClusterStatus) => void): () => void {
    this.statusListeners.push(listener);
    return () => {
      this.statusListeners = this.statusListeners.filter((l) => l !== listener);
    };
  }

  /** The most recent status answer from the current coordinator, if there has been one. */
  lastKnownClusterStatus(): ClusterStatus | null {
    const s = this.lastStatus;
    return s && s.server === this.getServerUrl() ? s.data : null;
  }

  private async waitUntilModelReady(
    onLoadingProgress?: (statusText: string) => void,
    signal?: AbortSignal
  ): Promise<void> {
    const startTime = Date.now();
    const maxWaitMs = 15 * 60 * 1000; // up to 15 minutes for cold WAN shard sync
    while (Date.now() - startTime < maxWaitMs) {
      if (signal?.aborted) {
        return;
      }
      const status = await this.getClusterStatus();
      if (!status?.model_loading?.is_loading && !status?.is_swapping_models) {
        return;
      }
      if (onLoadingProgress) {
        if (status?.model_loading?.is_loading) {
          onLoadingProgress(formatLoadingTelemetry(status.model_loading));
        } else {
          onLoadingProgress('⏳ Swapping distributed cluster model across Vulkan GPUs...');
        }
      }
      await new Promise((r) => setTimeout(r, 1000));
    }
  }

  async streamChat(
    messages: ChatMessage[],
    tools: ToolDefinition[],
    onDelta: (delta: string, reasoningDelta?: string) => void,
    onToolCallStart?: (name: string) => void,
    signal?: AbortSignal,
    onLoadingProgress?: (statusText: string) => void,
    options: StreamChatOptions = {}
  ): Promise<StreamChatResult> {
    const base = this.getServerUrl();
    const config = vscode.workspace.getConfiguration('dlne');
    // No `model` unless the user set one: the coordinator then serves the model it has active. Naming
    // one (even the active one, from a status answer a few seconds old) can make it swap models.
    const model = requestedModel(config.get<string>('chatModel', ''));
    let cluster: ClusterStatus | null = null;
    try {
      cluster = await this.recentClusterStatus();
      if (cluster?.model_loading?.is_loading || cluster?.is_swapping_models) {
        await this.waitUntilModelReady(onLoadingProgress, signal);
      }
    } catch {}
    // A coordinator that says it has no coding members is not asked for a route at all.
    const membersMayServe = codingMembersMayServe(cluster);
    const sessionId = options.sessionId ?? editorSessionId();

    const payload: Record<string, any> = {
      messages,
      stream: true,
      temperature: 0.2,
      max_tokens: AGENT_MAX_TOKENS
    };
    if (model) {
      payload.model = model;
    }
    if (tools && tools.length > 0) {
      payload.tools = tools;
    }

    let forbiddenRetries = 0;
    let networkRetries = 0;
    let engineStartRetries = 0;     // the engine crashed or was still loading: a few tries, never the loop's 180
    let memberFailures = 0;         // coding members that did not serve it; after two, the coordinator's own engine
    let ticket: string | undefined; // the place in line a busy answer promised to keep
    let busySince: number | null = null;
    // Whatever way this call ends, a place in line it showed is taken down.
    const clearBusy = () => {
      if (busySince !== null) {
        busySince = null;
        this.publishQueue(null, options.onQueue);
      }
    };
    for (let attempt = 0; attempt < 180; attempt++) {
      if (signal?.aborted) {
        clearBusy();
        return { content: '', toolCalls: [] };
      }
      let attemptResult: AttemptResult;
      let prepared: PreparedRequest | undefined;
      try {
        // Sealed afresh on every attempt: a key and nonce must never serve two requests.
        prepared = await prepareRequest(this.pipelineEnv(base), payload, {
          sessionId,
          membersEnabled: membersMayServe,
          forcePlain: memberFailures >= DlneClient.MEMBER_TRIES,
          ticket,
          folder: options.workspaceFolder,
          routeTtlMs: 0,
          maxPrivacy: options.maxPrivacy,
        });
        attemptResult = await this.executeSingleStreamAttempt(
          base,
          payload,
          onDelta,
          onToolCallStart,
          signal,
          onLoadingProgress,
          forbiddenRetries > 0,
          prepared,
          { onQueue: options.onQueue, priority: this.queuePriority(), onToolCallProgress: options.onToolCallProgress }
        );
      } catch (err: any) {
        // Every slot is taken and the coordinator said so (with the place in line): show it, wait, ask
        // again holding the ticket. This is not a failed attempt, so it does not use one up.
        if (err instanceof ServerBusy) {
          busySince = busySince ?? Date.now();
          if (Date.now() - busySince > DlneClient.busyGiveUpMs) {
            clearBusy();
            throw new ServerRefusal(503, 'The swarm has stayed busy for 15 minutes. Try again in a few minutes, or lend your GPU to go first.');
          }
          ticket = err.queue.ticket ?? ticket;
          this.publishQueue(
            { ...err.queue, source: 'response', waitedMs: Date.now() - busySince, priority: this.queuePriority() },
            options.onQueue,
            onLoadingProgress
          );
          // (Stop must not wait out the pause.)
          await new Promise<void>((resolve) => {
            const timer = setTimeout(resolve, Math.min(15000, Math.max(1000, err.retryAfterMs ?? 3000)));
            signal?.addEventListener('abort', () => { clearTimeout(timer); resolve(); }, { once: true });
          });
          attempt--;
          continue;
        }
        // The member the coordinator picked did not serve it (gone, moved, or its answer failed the
        // integrity check). Nothing of the answer had reached the user yet, so the request is asked
        // for again: a new route lookup (it may name another member) and a new seal, because an
        // envelope is good for one send and is never resent. After two such failures, the coordinator's
        // own engine in plain. An answer that was already partly shown cannot be quietly redone; it is
        // reported (the agent shows the user the error and does not record the half answer).
        if (prepared?.route && memberFailures < DlneClient.MEMBER_TRIES && isMemberFailure(err) && !(err instanceof MemberAnswerError && err.partial)) {
          memberFailures++;
          this.routes.invalidate(base);
          onLoadingProgress?.(
            memberFailures >= DlneClient.MEMBER_TRIES
              ? 'The coding member did not answer. Retrying on the main coding engine...'
              : 'The coding member did not answer. Asking the swarm for another one...'
          );
          continue;
        }
        // A 403 right after the local worker (re)starts or rotates its ephemeral token is
        // a race, not a ban: the Coordinator registers the token on the next heartbeat
        // (every 5 s). Re-read the token and retry briefly before surfacing the error.
        const msg: string = err?.message || '';
        if ((err?.status === 403 || msg.includes('403')) && forbiddenRetries < 6) {
          forbiddenRetries++;
          // Only retry if the worker is already contributing (waiting for heartbeat token exchange).
          // NEVER auto-start the worker unless the user explicitly requested it in VS Code!
          if (this.workerManager && this.workerManager.isContributing()) {
            if (onLoadingProgress) {
              onLoadingProgress(`🔐 Authenticating worker with cluster (attempt ${forbiddenRetries}/6)...`);
            }
            await new Promise((r) => setTimeout(r, 2000));
            continue;
          }
          clearBusy();
          throw err;
        }
        // Transient socket failures (stale keep-alive, WAN blip, coordinator restart):
        // retry a few times with backoff before surfacing the error.
        const code: string = err?.code || '';
        const transient =
          ['ECONNRESET', 'EPIPE', 'ECONNREFUSED', 'ETIMEDOUT', 'EAI_AGAIN'].includes(code) ||
          /ECONNRESET|socket hang up|EPIPE/i.test(msg);
        if (transient && networkRetries < 4) {
          networkRetries++;
          if (onLoadingProgress) {
            onLoadingProgress(`🔄 Connection to DLNE cluster dropped, retrying (${networkRetries}/4)...`);
          }
          await new Promise((r) => setTimeout(r, 1000 * networkRetries));
          continue;
        }

        // Automatic fallback if requested model crashes or exceeds available VRAM pool. The status is the error's own:
        // a refusal's message is the server's `detail` alone, so "500" was never in it and a crash during startup
        // ended the task instead of waiting for the engine (2026-10-28 10:11). Capped (review): every try is a charged
        // request, and a 504 holds the server up to 120 s, so an engine that never comes up is an error after a few.
        if (engineStartRetries < DlneClient.ENGINE_START_TRIES &&
            ([500, 503, 504].includes(err?.status) || msg.includes('500') || msg.includes('503') || msg.includes('504')) &&
            (msg.includes('crashed during startup') || msg.includes('timed out while loading') || msg.includes('Model swap in progress'))) {
          engineStartRetries++;
          if (onLoadingProgress) {
            onLoadingProgress('🔄 Model adapting to active VRAM pool, synchronizing with cluster...');
          }
          await new Promise((r) => setTimeout(r, 2000));
          // Whatever the swarm has active now: ask for it by leaving the model out.
          delete payload.model;
          continue;
        }
        clearBusy();
        throw err;
      }
      if (attemptResult.stillLoading) {
        // Server triggered a cold load or shard sync; poll live telemetry & auto-retry
        const status = await this.getClusterStatus();
        if (onLoadingProgress) {
          if (status?.model_loading?.is_loading) {
            onLoadingProgress(formatLoadingTelemetry(status.model_loading));
          } else {
            onLoadingProgress(
              `⏳ Loading ${model || 'the model'} into distributed GPU pool...`
            );
          }
        }
        await new Promise((r) => setTimeout(r, 1000));
        await this.waitUntilModelReady(onLoadingProgress, signal);
        continue;
      }
      clearBusy();
      return {
        content: attemptResult.content,
        toolCalls: attemptResult.toolCalls,
        finishReason: attemptResult.finishReason,
        promptTokens: attemptResult.promptTokens
      };
    }

    clearBusy();
    return { content: '', toolCalls: [] };
  }

  /** How long a request keeps asking while the coordinator answers "busy" before giving up (tests shorten it). */
  static busyGiveUpMs = 15 * 60 * 1000;
  /**
   * How long a chat request may hear nothing at all from the coordinator, once the answer has started, before it is
   * ended as a drop (tests shorten it). Any byte resets the clock, the coordinator's keep-alive comments included.
   * The longest wait seen for a first word was 99 s; the coordinator's own cap on a wedged engine is 15 minutes and it
   * sent nothing in between, so a dead network path (a laptop that slept, a NAT that forgot the connection) never ended
   * the step and the chat waited for ever.
   */
  static streamSilenceMs = 120_000;
  /**
   * The same limit before the answer has started (tests shorten it). Longer, because a long prompt is read before the
   * first word, and a volunteer's machine reads 80 to 120 tokens a second without saying anything: 30,000 tokens is
   * four minutes of silence that is not a drop. The coordinator's own engine says "still working" every 15 s while it
   * reads (server 2.6.2), and those bytes, like the place-in-line frames, reset this clock without ending the wait: only
   * the first piece of the answer itself switches to the shorter limit. This is the limit for the coordinator's own
   * engine and the least for a coding member, whose wait grows with the prompt (firstByteLimitMs).
   */
  static firstByteSilenceMs = 300_000;
  /**
   * What a coding member is taken to read a prompt at, in tokens a second (members measure 80 to 120, so this leaves a quarter
   * to spare). The wait before a member's answer starts is long enough to read the whole prompt at this speed (tests shorten it).
   */
  static memberReadTokensPerSecond = 60;
  /** Added to a member's reading time, for it to load, unseal and begin (tests shorten it). */
  static memberStartAllowanceMs = 60_000;
  /** The most any request waits for its first word, however big the prompt: the coordinator's own cap on a wedged engine (tests shorten it). */
  static firstByteCapMs = 15 * 60 * 1000;

  /**
   * How long a request may hear nothing before its answer starts. For the coordinator's own engine that is
   * firstByteSilenceMs: it says "still working" every 15 s while it reads, and those lines reset the clock. A coding
   * member's stream is sealed and does not pass through those lines, so it says nothing until its first word, and a
   * member reads at 80 to 120 tokens a second: a 29,000 token prompt is silent for four to six minutes, and a fixed 300 s
   * cut it off and sent it again (and again: up to 8 times, each one a request the member had to read all over). For a
   * request a member answers the limit grows with the prompt instead: its tokens at memberReadTokensPerSecond and a
   * minute to start, never less than firstByteSilenceMs and never more than firstByteCapMs.
   */
  static firstByteLimitMs(promptTokens: number, member: boolean): number {
    const base = DlneClient.firstByteSilenceMs;
    if (!member) return base;
    const reading = Math.ceil((promptTokens * 1000) / Math.max(1, DlneClient.memberReadTokensPerSecond)) + DlneClient.memberStartAllowanceMs;
    return Math.min(Math.max(base, reading), Math.max(base, DlneClient.firstByteCapMs));
  }

  /** What a prompt costs in tokens, for the wait above: 3 characters a token, which is what the coordinator counts at and so errs long. */
  private static promptTokensOf(payload: Record<string, any>): number {
    try {
      return Math.ceil(JSON.stringify({ messages: payload.messages, tools: payload.tools }).length / 3);
    } catch {
      return 0;
    }
  }

  /** Coding members tried for one request before it goes to the coordinator's own engine. */
  private static readonly MEMBER_TRIES = 2;
  /** Tries after the engine crashed during startup or timed out loading, before the refusal is shown (2.6.2 review). */
  static readonly ENGINE_START_TRIES = 5;

  private executeSingleStreamAttempt(
    base: string,
    payload: Record<string, any>,
    onDelta: (delta: string, reasoningDelta?: string) => void,
    onToolCallStart?: (name: string) => void,
    signal?: AbortSignal,
    onLoadingProgress?: (statusText: string) => void,
    forceWorkerToken: boolean = false,
    prepared?: PreparedRequest,
    wait?: {
      onQueue?: (queue: QueueUpdate | null) => void;
      priority: number;
      onToolCallProgress?: (name: string, argumentsSoFar: string) => void;
    }
  ): Promise<AttemptResult> {
    return new Promise((resolve, reject) => {
      const url = new URL(`${base}/v1/chat/completions`);
      const client = url.protocol === 'https:' ? https : http;
      // Called directly (not through streamChat), the request goes exactly as given.
      const request = prepared ?? plainRequest(payload);
      const headers = this.getHeaders(forceWorkerToken);
      headers['Accept'] = 'text/event-stream';
      Object.assign(headers, request.headers);

      const postData = request.body;
      headers['Content-Length'] = Buffer.byteLength(postData).toString();
      const sealedAnswer = request.respKey ? new SealedAnswer(request.respKey) : null;

      let fullContent = '';
      let stillLoading = false;
      let engineDropped: string | null = null;
      let finishReason: string | undefined;
      let promptTokens: number | undefined;
      const toolCallsMap: Map<number, StreamedToolCall> = new Map();
      const notifiedToolStarts = new Set<string>();

      // Until the first word of the answer arrives the user is told what the wait is: their place in
      // line if every coding slot is taken, otherwise that the swarm is working on it.
      let queueShown = false;
      const showQueue = (update: QueueUpdate | null) => {
        queueShown = update !== null;
        this.publishQueue(update, wait?.onQueue, onLoadingProgress);
      };
      const watcher = new WaitWatcher({
        fetchQueue: async () => parseCodingQueue(await this.getClusterStatus()),
        priority: wait?.priority ?? 2,
        progress: onLoadingProgress,
        queue: showQueue,
        timing: DlneClient.waitTiming,
      });
      let answerStarted = false;
      let sealedFrames = 0;
      let unsealedAnswerFrames = 0;
      let failure: Error | null = null;
      const stopWaiting = () => {
        watcher.stop();
        if (queueShown) {
          showQueue(null);
        }
      };
      if (onLoadingProgress || wait?.onQueue) {
        watcher.start();
      }

      const req = client.request(url, {
        method: 'POST',
        headers,
        // Fresh socket per chat request: VS Code's global agent may hand us a pooled
        // keep-alive socket that the coordinator (uvicorn, 5 s idle timeout) has already
        // closed, which surfaces as "read ECONNRESET" before the request is even sent.
        agent: false,
        // The long limit until the answer starts (longer still for a member reading a big prompt), then the short one (see
        // where answerStarted is set). The prompt is only measured for a member: the coordinator's own engine keeps the stream alive.
        timeout: DlneClient.firstByteLimitMs(prepared?.route ? DlneClient.promptTokensOf(payload) : 0, Boolean(prepared?.route))
      }, (res) => {
        this.noteQuota(res);
        res.on('error', (err) => {
          stopWaiting();
          if (answerStarted && !failure) {
            // Part of the answer is already on the user's screen, so the request is not quietly redone:
            // the answer is reported as cut off.
            const cut = new MemberAnswerError('The answer stopped part-way because the connection dropped, so DLNE is not treating it as finished.');
            cut.partial = true;
            reject(cut);
          } else {
            reject(err);
          }
        });
        if (res.statusCode !== 200) {
          // The refusal is read in full: a 503 or 429 that carries a `dlne_queue` is "busy, here is your
          // place" and not a failure; any other 503 means the model is still loading.
          let errBody = '';
          res.on('data', (c) => errBody += c);
          res.on('end', () => {
            stopWaiting();
            // Refusals and busy answers are never sealed (contract section 6, item 7).
            const text = errBody;
            const queue = res.statusCode === 503 || res.statusCode === 429 ? queueFromBody(text) : null;
            const reroute = res.statusCode === 503 && request.respKey ? rerouteReason(text) : null;
            if (queue) {
              const retryAfter = parseInt(String(res.headers['retry-after'] ?? ''), 10);
              reject(new ServerBusy(queue, Number.isFinite(retryAfter) && retryAfter > 0 ? retryAfter * 1000 : undefined));
            } else if (reroute !== null) {
              // The coordinator could not hand the sealed body to its member (gone, full, failed, not allowed, route
              // expired). That is a member failure, not "the model is loading": ask for a route and seal again.
              const refusal = new ServerRefusal(503, 'The coding member could not take the request.');
              refusal.reroute = reroute;
              reject(refusal);
            } else if (res.statusCode === 503) {
              resolve({ content: '', toolCalls: [], stillLoading: true });
            } else {
              reject(new ServerRefusal(res.statusCode, serverErrorMessage(res.statusCode, text)));
            }
          });
          return;
        }

        let buffer = '';
        res.on('data', (chunk: Buffer) => {
          buffer += chunk.toString('utf-8');
          const lines = buffer.split('\n');
          buffer = lines.pop() || '';

          for (const line of lines) {
            const trimmed = line.trim();
            if (!trimmed || trimmed.startsWith(':')) continue;
            if (trimmed === 'data: [DONE]') continue;

            if (trimmed.startsWith('data: ')) {
              try {
                let parsed = JSON.parse(trimmed.slice(6));
                // The coordinator lost its engine: the step is sent again (EngineDropped), and the alert is not shown.
                if (parsed && typeof parsed === 'object' && RETRIABLE_ENGINE_ERRORS.has(parsed.dlne_error)) {
                  engineDropped = parsed.dlne_error;
                  continue;
                }
                // The coordinator, while the request waits for a slot: where it stands.
                if (parsed && typeof parsed === 'object' && 'dlne_queue' in parsed) {
                  const info = parseQueueInfo(parsed.dlne_queue);
                  if (info && !answerStarted) watcher.report(info, 'stream');
                  continue;
                }
                // A sealed answer is opened frame by frame; one that fails to authenticate, or that
                // should have been sealed and was not, ends the attempt (the caller retries in plain).
                if (sealedAnswer) {
                  const opened = sealedAnswer.handle(parsed);
                  if (opened.kind === 'plain-content') {
                    unsealedAnswerFrames++;
                    continue;
                  }
                  if (opened.kind === 'ignore') continue;
                  if (opened.kind === 'end') {
                    sealedFrames++;
                    continue; // sealedAnswer.finished is now true
                  }
                  sealedFrames++;
                  parsed = opened.chunk;
                }
                // A usage chunk may come with no choices at all, so this is read first.
                promptTokens = promptTokensOf(parsed) ?? promptTokens;
                const choice = parsed.choices?.[0];
                if (!choice) continue;
                if (typeof choice.finish_reason === 'string' && choice.finish_reason) {
                  finishReason = choice.finish_reason;
                }
                if (!answerStarted) {
                  answerStarted = true;
                  stopWaiting();
                  // The answer is coming: from here on the socket may be silent for the shorter limit only.
                  req.setTimeout(DlneClient.streamSilenceMs);
                }

                const deltaContent = choice.delta?.content || '';
                const deltaReasoning =
                  choice.delta?.reasoning_content ||
                  choice.delta?.reasoning ||
                  '';

                if (
                  deltaContent.includes('[System] The engine is still loading this model') ||
                  deltaContent.includes('Engine not ready yet')
                ) {
                  stillLoading = true;
                  continue;
                }
                if (deltaContent.includes(OLD_DROP_ALERT)) {   // a coordinator from before 2.5.4
                  engineDropped = 'engine_dropped';
                  continue;
                }

                if (deltaContent || deltaReasoning) {
                  fullContent += deltaContent;
                  onDelta(deltaContent, deltaReasoning);
                }

                // Parse OpenAI-compatible streamed tool_calls
                if (choice.delta?.tool_calls && Array.isArray(choice.delta.tool_calls)) {
                  for (const tc of choice.delta.tool_calls) {
                    const idx = tc.index ?? 0;
                    const existing = toolCallsMap.get(idx) || {
                      id: tc.id || `call_${idx}_${Date.now()}`,
                      name: '',
                      arguments: ''
                    };
                    if (tc.id) existing.id = tc.id;
                    if (tc.function?.name) {
                      existing.name = tc.function.name;
                      if (!notifiedToolStarts.has(existing.name)) {
                        notifiedToolStarts.add(existing.name);
                        if (onToolCallStart) onToolCallStart(existing.name);
                      }
                    }
                    if (tc.function?.arguments) {
                      existing.arguments += tc.function.arguments;
                      wait?.onToolCallProgress?.(existing.name, existing.arguments);
                    }
                    toolCallsMap.set(idx, existing);
                  }
                }
              } catch (err) {
                if (err instanceof MemberAnswerError) {
                  err.partial = answerStarted;
                  failure = err;
                  stopWaiting();
                  reject(err);
                  req.destroy();
                  return;
                }
                // Ignore partial JSON chunks
              }
            }
          }
        });

        res.on('end', () => {
          stopWaiting();
          if (failure) return;
          if (engineDropped) {
            reject(new EngineDropped(engineDropped, fullContent.length > 0 || toolCallsMap.size > 0));
            return;
          }
          // A member that answers in plain when the request was sealed has broken the contract; what it
          // said is not shown.
          if (sealedAnswer && sealedFrames === 0 && unsealedAnswerFrames > 0) {
            reject(new MemberAnswerError('A coding member answered without encrypting, so DLNE discarded the answer.'));
            return;
          }
          // A sealed answer is complete only at its sealed end. A plain [DONE] or a closed connection
          // before it is a stream that was cut, and it is not passed off as a finished answer.
          if (sealedAnswer && !sealedAnswer.finished) {
            const cut = new MemberAnswerError(
              'The coding member\'s answer stopped before it was complete (the connection was cut), so DLNE is not treating it as finished.'
            );
            cut.partial = answerStarted;
            reject(cut);
            return;
          }
          resolve({
            content: fullContent,
            toolCalls: Array.from(toolCallsMap.values()),
            stillLoading,
            finishReason,
            promptTokens
          });
        });
      });

      if (signal) {
        signal.addEventListener('abort', () => {
          stopWaiting();
          req.destroy();
          resolve({
            content: fullContent,
            toolCalls: Array.from(toolCallsMap.values()),
            stillLoading: false
          });
        });
      }

      req.on('error', (err) => {
        stopWaiting();
        reject(err);
      });
      // `timeout` above only reports that the socket was idle for that long (Node leaves it open, so a stream that says
      // nothing was never ended). Silence that long ends the attempt as a drop, and the agent sends the step again
      // after the usual pause. Any bytes reset the clock, the coordinator's queue frames included.
      req.on('timeout', () => {
        stopWaiting();
        if (failure) return;
        reject(new EngineDropped('stream_silent', fullContent.length > 0 || toolCallsMap.size > 0));
        req.destroy();
      });
      req.write(postData);
      req.end();
    });
  }

  /**
   * One FIM completion ('' on any failure: the editor just shows no suggestion; `onOutcome` hears why, and how a
   * completion ended). `signal` aborts it when a newer keystroke wants the slot. Completions reuse pooled keep-alive connections,
   * saving a TCP handshake per keystroke burst; a pooled socket the coordinator already closed
   * fails before any response byte, and such a request is retried once on a fresh socket.
   * That is safe here because autocomplete never spends the free-trial quota.
   */
  async getCompletion(
    prompt: string,
    maxTokens: number = 64,
    stop: string[] = [],
    signal?: AbortSignal,
    options: {
      sessionId?: string;
      workspaceFolder?: vscode.WorkspaceFolder;
      onOutcome?: (outcome: CompletionOutcome) => void;
    } = {}
  ): Promise<string> {
    const base = this.getServerUrl();
    const config = vscode.workspace.getConfiguration('dlne');
    const model = requestedModel(config.get<string>('fimModel', ''));
    // A cancelled request (a newer keystroke took over) is not a failure, and is not reported as one.
    const failed = (why: string): string => {
      if (!signal?.aborted) options.onOutcome?.({ failure: why });
      return '';
    };

    const payload: Record<string, any> = {
      prompt,
      max_tokens: maxTokens,
      temperature: 0.1,
      stream: false,
      stop
    };
    if (model) {
      payload.model = model;
    }
    const url = new URL(`${base}/v1/completions`);
    const isHttps = url.protocol === 'https:';
    const agent = isHttps ? DlneClient.fimHttpsAgent : DlneClient.fimHttpAgent;
    // Autocomplete asks about coding members only when the last status the status bar polled says the
    // coordinator has them: it must not pay a round trip per keystroke to find out that it has none.
    const membersMayServe = codingMembersMayServe(this.lastKnownClusterStatus());
    const sessionId = options.sessionId ?? editorSessionId();

    const attempt = async (retried: boolean, memberFailures: number): Promise<string> => {
      if (signal?.aborted) {
        return '';
      }
      // Sealed afresh on every attempt, including a retry on a new socket: an envelope is never resent.
      const prepared = await prepareRequest(this.pipelineEnv(base, agent), payload, {
        sessionId,
        membersEnabled: membersMayServe,
        forcePlain: memberFailures >= DlneClient.MEMBER_TRIES,
        folder: options.workspaceFolder,
        routeTtlMs: DlneClient.FIM_ROUTE_TTL_MS,
      });
      if (signal?.aborted) {
        return '';
      }
      return new Promise<string>((resolve) => {
        const headers = this.getHeaders();
        Object.assign(headers, prepared.headers);
        headers['Content-Length'] = Buffer.byteLength(prepared.body).toString();
        let answered = false;
        // The member did not serve it: ask for a route again and seal again (twice at most, then the
        // coordinator's own engine in plain).
        const retryOnAnotherMember = () => {
          this.routes.invalidate(base);
          resolve(attempt(retried, memberFailures + 1));
        };

        const req = (isHttps ? https : http).request(url, {
          method: 'POST',
          headers,
          agent,
          timeout: 10000
        }, (res) => {
          answered = true;
          this.noteQuota(res);
          if (res.statusCode !== 200) {
            if (prepared.route && [404, 410, 421, 502, 504].includes(res.statusCode ?? 0)) {
              res.resume();
              retryOnAnotherMember();
            } else if (prepared.route && res.statusCode === 503) {
              // 503 is the coordinator's "ask for a new route" for a sealed request (dlne_reroute); any other 503
              // is not about the member and autocomplete simply gives up this time.
              let refusal = '';
              res.on('data', (chunk) => { if (refusal.length < 4096) refusal += chunk; });
              res.on('end', () => (rerouteReason(refusal) !== null ? retryOnAnotherMember() : resolve(failed('HTTP 503'))));
              res.on('error', (err: any) => resolve(failed(`connection error (${err?.code || err?.message || err})`)));
            } else {
              res.resume();
              resolve(failed(`HTTP ${res.statusCode}`));
            }
            return;
          }
          let data = '';
          res.on('data', (chunk) => data += chunk);
          res.on('end', () => {
            try {
              const parsed = prepared.respKey ? openWholeAnswer(data, prepared.respKey) : JSON.parse(data);
              const choice = parsed.choices?.[0];
              options.onOutcome?.({ finishReason: typeof choice?.finish_reason === 'string' ? choice.finish_reason : undefined });
              resolve(choice?.text || '');
            } catch (err) {
              if (err instanceof MemberAnswerError && prepared.route) {
                retryOnAnotherMember();
              } else {
                resolve(failed('the answer could not be read'));
              }
            }
          });
          res.on('error', (err: any) => resolve(failed(`connection error (${err?.code || err?.message || err})`)));
        });

        const onAbort = () => req.destroy();
        signal?.addEventListener('abort', onAbort, { once: true });
        req.on('close', () => signal?.removeEventListener('abort', onAbort));
        req.on('error', (err: any) => {
          const staleSocket = req.reusedSocket && !answered && !signal?.aborted &&
            (err?.code === 'ECONNRESET' || err?.code === 'EPIPE');
          if (staleSocket && !retried) {
            resolve(attempt(true, memberFailures));
            return;
          }
          resolve(failed(`could not reach ${base} (${err?.code || err?.message || err})`));
        });
        req.on('timeout', () => {
          req.destroy();
          resolve(failed('no answer within 10 s'));
        });
        req.write(prepared.body);
        req.end();
      });
    };

    return attempt(false, 0);
  }

  /** A route answer is reused for this long by autocomplete (chat asks every turn). */
  private static readonly FIM_ROUTE_TTL_MS = 20_000;

  // Shared by every completion request. Idle sockets are dropped after 30 s, inside the
  // coordinator's 75 s keep-alive (a 2.0.0 coordinator closes after 5 s; the retry above
  // absorbs that).
  private static readonly fimHttpAgent = new http.Agent({ keepAlive: true, maxSockets: 2, timeout: 30000 });
  private static readonly fimHttpsAgent = new https.Agent({ keepAlive: true, maxSockets: 2, timeout: 30000 });
}
