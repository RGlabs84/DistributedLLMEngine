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

  private pipelineEnv(base: string, agent?: http.Agent): PipelineEnv {
    return { base, headers: () => this.getHeaders(), routes: this.routes, agent };
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
      max_tokens: 4096
    };
    if (model) {
      payload.model = model;
    }
    if (tools && tools.length > 0) {
      payload.tools = tools;
    }

    let forbiddenRetries = 0;
    let networkRetries = 0;
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
      let attemptResult: { content: string; toolCalls: StreamedToolCall[]; stillLoading: boolean };
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
          { onQueue: options.onQueue, priority: this.queuePriority() }
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

        // Automatic fallback if requested model crashes or exceeds available VRAM pool
        if ((msg.includes('500') || msg.includes('503') || msg.includes('504')) &&
            (msg.includes('crashed during startup') || msg.includes('timed out while loading') || msg.includes('Model swap in progress'))) {
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
        toolCalls: attemptResult.toolCalls
      };
    }

    clearBusy();
    return { content: '', toolCalls: [] };
  }

  /** How long a request keeps asking while the coordinator answers "busy" before giving up (tests shorten it). */
  static busyGiveUpMs = 15 * 60 * 1000;
  /** Coding members tried for one request before it goes to the coordinator's own engine. */
  private static readonly MEMBER_TRIES = 2;

  private executeSingleStreamAttempt(
    base: string,
    payload: Record<string, any>,
    onDelta: (delta: string, reasoningDelta?: string) => void,
    onToolCallStart?: (name: string) => void,
    signal?: AbortSignal,
    onLoadingProgress?: (statusText: string) => void,
    forceWorkerToken: boolean = false,
    prepared?: PreparedRequest,
    wait?: { onQueue?: (queue: QueueUpdate | null) => void; priority: number }
  ): Promise<{ content: string; toolCalls: StreamedToolCall[]; stillLoading: boolean }> {
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
        timeout: 120000
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
                const choice = parsed.choices?.[0];
                if (!choice) continue;
                if (!answerStarted) {
                  answerStarted = true;
                  stopWaiting();
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
            stillLoading
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
      req.write(postData);
      req.end();
    });
  }

  /**
   * One FIM completion ('' on any failure: autocomplete degrades silently). `signal` aborts it
   * when a newer keystroke wants the slot. Completions reuse pooled keep-alive connections,
   * saving a TCP handshake per keystroke burst; a pooled socket the coordinator already closed
   * fails before any response byte, and such a request is retried once on a fresh socket.
   * That is safe here because autocomplete never spends the free-trial quota.
   */
  async getCompletion(
    prompt: string,
    maxTokens: number = 64,
    stop: string[] = [],
    signal?: AbortSignal,
    options: { sessionId?: string; workspaceFolder?: vscode.WorkspaceFolder } = {}
  ): Promise<string> {
    const base = this.getServerUrl();
    const config = vscode.workspace.getConfiguration('dlne');
    const model = requestedModel(config.get<string>('fimModel', ''));

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
              res.on('end', () => (rerouteReason(refusal) !== null ? retryOnAnotherMember() : resolve('')));
              res.on('error', () => resolve(''));
            } else {
              res.resume();
              resolve('');
            }
            return;
          }
          let data = '';
          res.on('data', (chunk) => data += chunk);
          res.on('end', () => {
            try {
              const parsed = prepared.respKey ? openWholeAnswer(data, prepared.respKey) : JSON.parse(data);
              resolve(parsed.choices?.[0]?.text || '');
            } catch (err) {
              if (err instanceof MemberAnswerError && prepared.route) {
                retryOnAnotherMember();
              } else {
                resolve('');
              }
            }
          });
          res.on('error', () => resolve(''));
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
          resolve('');
        });
        req.on('timeout', () => {
          req.destroy();
          resolve('');
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
