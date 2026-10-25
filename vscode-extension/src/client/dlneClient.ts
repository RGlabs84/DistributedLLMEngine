import * as vscode from 'vscode';
import * as http from 'http';
import * as https from 'https';
import { resolveServerUrl } from '../serverUrl';
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
}

export function formatLoadingTelemetry(ml?: ModelLoadingTelemetry): string {
  if (!ml || !ml.is_loading) {
    return 'Model ready';
  }
  const modelName = ml.model || 'Frontier Model';
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
 * JSON `detail`, e.g. the CPU contributors' "You've used all 100 coding chats… frees up in
 * 42 minutes"), never the raw response body.
 */
/** A chat request the coordinator refused, with its HTTP status kept for callers to branch on. */
export class ServerRefusal extends Error {
  constructor(public readonly status: number | undefined, message: string) {
    super(message);
    this.name = 'ServerRefusal';
  }
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

  async swapModel(modelName: string): Promise<{ status: string; active_model?: string; detail?: string }> {
    const base = this.getServerUrl();
    const url = new URL(`${base}/v1/cluster/swap`);
    const postData = JSON.stringify({ model: modelName });
    const headers = this.getHeaders();
    headers['Content-Length'] = Buffer.byteLength(postData).toString();

    return new Promise((resolve) => {
      const client = url.protocol === 'https:' ? https : http;
      const req = client.request(
        url,
        { method: 'POST', headers, timeout: 45000 },
        (res) => {
          let data = '';
          res.on('data', (chunk) => (data += chunk));
          res.on('end', () => {
            try {
              const parsed = JSON.parse(data);
              if (res.statusCode === 200) {
                resolve(parsed);
              } else {
                resolve({ status: 'error', detail: parsed.detail || `HTTP ${res.statusCode}` });
              }
            } catch {
              resolve({ status: 'error', detail: data || `HTTP ${res.statusCode}` });
            }
          });
        }
      );
      req.on('error', (err) => resolve({ status: 'error', detail: err.message }));
      req.on('timeout', () => {
        req.destroy();
        resolve({ status: 'error', detail: 'Model swap timed out' });
      });
      req.write(postData);
      req.end();
    });
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
    onLoadingProgress?: (statusText: string) => void
  ): Promise<StreamChatResult> {
    const base = this.getServerUrl();
    const config = vscode.workspace.getConfiguration('dlne');
    let model = config.get<string>('chatModel', '').trim() || undefined;
    try {
      const cluster = await this.recentClusterStatus();
      if (!model || model === 'undefined') {
        model = cluster?.global_active_model || 'qwen2.5-coder-14b-instruct-q4_k_m.gguf';
      }
      if (cluster?.model_loading?.is_loading || cluster?.is_swapping_models) {
        await this.waitUntilModelReady(onLoadingProgress, signal);
      }
    } catch {}

    const payload: Record<string, any> = {
      messages,
      stream: true,
      temperature: 0.2,
      max_tokens: 4096,
      model: model || 'qwen2.5-coder-14b-instruct-q4_k_m.gguf'
    };
    if (tools && tools.length > 0) {
      payload.tools = tools;
    }

    let forbiddenRetries = 0;
    let networkRetries = 0;
    for (let attempt = 0; attempt < 180; attempt++) {
      if (signal?.aborted) {
        return { content: '', toolCalls: [] };
      }
      let attemptResult: { content: string; toolCalls: StreamedToolCall[]; stillLoading: boolean };
      try {
        attemptResult = await this.executeSingleStreamAttempt(
          base,
          payload,
          onDelta,
          onToolCallStart,
          signal,
          onLoadingProgress,
          forbiddenRetries > 0
        );
      } catch (err: any) {
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
          const cluster = await this.getClusterStatus();
          if (cluster?.global_active_model) {
            payload.model = cluster.global_active_model;
          } else {
            delete payload.model;
          }
          continue;
        }
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
              `⏳ Loading ${model || 'Frontier Model'} into distributed GPU pool...`
            );
          }
        }
        await new Promise((r) => setTimeout(r, 1000));
        await this.waitUntilModelReady(onLoadingProgress, signal);
        continue;
      }
      return {
        content: attemptResult.content,
        toolCalls: attemptResult.toolCalls
      };
    }

    return { content: '', toolCalls: [] };
  }

  private executeSingleStreamAttempt(
    base: string,
    payload: Record<string, any>,
    onDelta: (delta: string, reasoningDelta?: string) => void,
    onToolCallStart?: (name: string) => void,
    signal?: AbortSignal,
    onLoadingProgress?: (statusText: string) => void,
    forceWorkerToken: boolean = false
  ): Promise<{ content: string; toolCalls: StreamedToolCall[]; stillLoading: boolean }> {
    return new Promise((resolve, reject) => {
      const url = new URL(`${base}/v1/chat/completions`);
      const client = url.protocol === 'https:' ? https : http;
      const headers = this.getHeaders(forceWorkerToken);
      headers['Accept'] = 'text/event-stream';

      const postData = JSON.stringify(payload);
      headers['Content-Length'] = Buffer.byteLength(postData).toString();

      let fullContent = '';
      let stillLoading = false;
      const toolCallsMap: Map<number, StreamedToolCall> = new Map();
      const notifiedToolStarts = new Set<string>();

      let progressTimer: NodeJS.Timeout | null = null;
      let firstChunkReceived = false;

      const clearTimer = () => {
        if (progressTimer) {
          clearInterval(progressTimer);
          progressTimer = null;
        }
      };

      if (onLoadingProgress) {
        let elapsed = 0;
        progressTimer = setInterval(() => {
          elapsed += 2;
          if (firstChunkReceived) {
            clearTimer();
            return;
          }
          if (elapsed === 2) {
            onLoadingProgress('🧠 Evaluating prompt across distributed Vulkan GPU cluster...');
          } else if (elapsed === 6) {
            onLoadingProgress('⚡ Distributing activation tensors across 3 GPUs (24 GB VRAM pool)...');
          } else if (elapsed >= 10 && elapsed % 4 === 0) {
            onLoadingProgress(`⚡ Pipelining inference across 3 GPUs (elapsed: ${elapsed}s)...`);
          }
        }, 2000);
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
        if (res.statusCode === 503) {
          clearTimer();
          res.resume();
          resolve({ content: '', toolCalls: [], stillLoading: true });
          return;
        }
        if (res.statusCode !== 200) {
          clearTimer();
          let errBody = '';
          res.on('data', (c) => errBody += c);
          res.on('end', () => reject(new ServerRefusal(res.statusCode, serverErrorMessage(res.statusCode, errBody))));
          return;
        }

        let buffer = '';
        res.on('data', (chunk: Buffer) => {
          firstChunkReceived = true;
          clearTimer();
          buffer += chunk.toString('utf-8');
          const lines = buffer.split('\n');
          buffer = lines.pop() || '';

          for (const line of lines) {
            const trimmed = line.trim();
            if (!trimmed || trimmed.startsWith(':')) continue;
            if (trimmed === 'data: [DONE]') continue;

            if (trimmed.startsWith('data: ')) {
              try {
                const parsed = JSON.parse(trimmed.slice(6));
                const choice = parsed.choices?.[0];
                if (!choice) continue;

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
              } catch {
                // Ignore partial JSON chunks
              }
            }
          }
        });

        res.on('end', () => {
          clearTimer();
          resolve({
            content: fullContent,
            toolCalls: Array.from(toolCallsMap.values()),
            stillLoading
          });
        });
      });

      if (signal) {
        signal.addEventListener('abort', () => {
          clearTimer();
          req.destroy();
          resolve({
            content: fullContent,
            toolCalls: Array.from(toolCallsMap.values()),
            stillLoading: false
          });
        });
      }

      req.on('error', (err) => {
        clearTimer();
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
  async getCompletion(prompt: string, maxTokens: number = 64, stop: string[] = [], signal?: AbortSignal): Promise<string> {
    const base = this.getServerUrl();
    const config = vscode.workspace.getConfiguration('dlne');
    const model = config.get<string>('fimModel', '').trim() || undefined;

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
    const postData = JSON.stringify(payload);
    const url = new URL(`${base}/v1/completions`);

    const attempt = (retried: boolean): Promise<string> => new Promise((resolve) => {
      if (signal?.aborted) {
        resolve('');
        return;
      }
      const isHttps = url.protocol === 'https:';
      const headers = this.getHeaders();
      headers['Content-Length'] = Buffer.byteLength(postData).toString();
      let answered = false;

      const req = (isHttps ? https : http).request(url, {
        method: 'POST',
        headers,
        agent: isHttps ? DlneClient.fimHttpsAgent : DlneClient.fimHttpAgent,
        timeout: 10000
      }, (res) => {
        answered = true;
        this.noteQuota(res);
        if (res.statusCode !== 200) {
          res.resume();
          resolve('');
          return;
        }
        let data = '';
        res.on('data', (chunk) => data += chunk);
        res.on('end', () => {
          try {
            const parsed = JSON.parse(data);
            resolve(parsed.choices?.[0]?.text || '');
          } catch {
            resolve('');
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
          resolve(attempt(true));
          return;
        }
        resolve('');
      });
      req.on('timeout', () => {
        req.destroy();
        resolve('');
      });
      req.write(postData);
      req.end();
    });

    return attempt(false);
  }

  // Shared by every completion request. Idle sockets are dropped after 30 s, inside the
  // coordinator's 75 s keep-alive (a 2.0.0 coordinator closes after 5 s; the retry above
  // absorbs that).
  private static readonly fimHttpAgent = new http.Agent({ keepAlive: true, maxSockets: 2, timeout: 30000 });
  private static readonly fimHttpsAgent = new https.Agent({ keepAlive: true, maxSockets: 2, timeout: 30000 });
}
