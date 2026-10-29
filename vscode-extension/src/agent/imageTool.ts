/**
 * The `generate_image` agent tool (2.6.1 contract, "Server (coordinator) contract" and "Clients"): the coding-side
 * image endpoints POST /v1/images, GET /v1/images/{id} and POST /v1/images/{id}/cancel. The prompt goes to the
 * coordinator's own image engine only, never to a coding member. The tool is offered to the model only while
 * /v1/cluster/status says `capabilities.image_generation` is true.
 */
import * as path from 'path';
import * as fs from 'fs';
import * as os from 'os';
import { ToolDefinition } from '../client/dlneClient';

/** Sizes the coordinator serves; anything else is refused there, and here before a request is spent on it. */
export const IMAGE_SIZES = ['768x768', '1024x1024', '768x1024', '1024x768'];
const POLL_EVERY_MS = 2000;
/** The server's own job limit is at most 900 s (IMAGE_JOB_TIMEOUT_SECONDS) plus the queue: a job past this is given up on. */
const GIVE_UP_AFTER_MS = 20 * 60 * 1000;
/** Poll answers that failed in a row (network, 5xx) before the tool gives up; one blip must not lose an 80 s job. */
const MAX_POLL_FAILURES = 3;
/** A PNG of 1024x1024 is well under this; a bigger answer is not an image this tool wrote. */
const MAX_IMAGE_BYTES = 30 * 1024 * 1024;
const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

/** The part of /v1/cluster/status the tool reads. */
export interface ImageCapabilityStatus {
  capabilities?: { image_generation?: boolean; [key: string]: unknown };
}

/** True only for an explicit `true`: a coordinator that does not mention it (an older one), or has it off, offers nothing. */
export function imageGenerationOffered(status: ImageCapabilityStatus | null | undefined): boolean {
  return status?.capabilities?.image_generation === true;
}

export const IMAGE_TOOL_NAME = 'generate_image';

export function imageToolDefinition(): ToolDefinition {
  return {
    type: 'function',
    function: {
      name: IMAGE_TOOL_NAME,
      description: 'Create a picture from a text description (an icon, an illustration, a mockup background) and save it as a PNG in the workspace. It takes 1 to 3 minutes, so use it only when the user wants an image. People and anything unsafe are refused.',
      parameters: {
        type: 'object',
        properties: {
          prompt: { type: 'string', description: 'What the picture shows, in plain words (at most 1000 characters)' },
          path: { type: 'string', description: 'Optional workspace path to save to, ending in .png (default: images/<words of the prompt>.png)' },
          size: { type: 'string', enum: IMAGE_SIZES, description: 'Optional: 768x768 (default), 1024x1024, 768x1024 or 1024x768' }
        },
        required: ['prompt']
      }
    }
  };
}

/** The line the system prompt gains while the tool is offered (the static prompt is pinned by the canary parity test). */
export const IMAGE_PROMPT_LINE = '- generate_image(prompt, path, size): create a picture from a description and save it as a PNG in the workspace (about 1-3 minutes); use it only when the user asks for an image.';

/** The agent system prompt with the generate_image line in the tool list, before run_terminal_command's. */
export function withImageLine(systemPrompt: string): string {
  const marker = '- run_terminal_command(';
  return systemPrompt.includes(marker)
    ? systemPrompt.replace(marker, `${IMAGE_PROMPT_LINE}\n${marker}`)
    : `${systemPrompt}\n\n${IMAGE_PROMPT_LINE}`;
}

/** `prompt` as a short file name: lower case words, a-z0-9 and dashes, at most 40 characters; "image" when nothing is left. */
export function slugOfPrompt(prompt: string): string {
  const slug = String(prompt ?? '')
    .normalize('NFKD').replace(/[̀-ͯ]/g, '')
    .toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '')
    .slice(0, 40).replace(/-+$/, '');
  return slug || 'image';
}

/** True when `target` is `root` or inside it by name (the caller judges symbolic links, see tools.ts insideWorkspace). */
function lexicallyInside(root: string, target: string): boolean {
  const rel = path.relative(root, target);
  return rel === '' || (!rel.startsWith('..') && !path.isAbsolute(rel));
}

export type ImageArgs = { prompt: string; path: string; size?: string };

/**
 * The call's arguments as the tool will use them, or the reason to hand back to the model. The file always lives
 * inside the workspace (unlike write_file there is no "outside, with approval"), is a .png, and when the model gave
 * no path the name is images/<slug>.png, numbered if that file exists so a default never overwrites anything.
 */
export function prepareImageArgs(args: Record<string, any>, root: string | undefined): { ok: true; args: ImageArgs } | { ok: false; error: string } {
  const prompt = typeof args?.prompt === 'string' ? args.prompt.trim() : '';
  if (!prompt) return { ok: false, error: 'Error: generate_image needs a prompt describing the picture.' };
  if (!root) return { ok: false, error: 'Error: open a folder first: generate_image saves the picture into the workspace.' };
  let size: string | undefined;
  if (args.size !== undefined && args.size !== null && args.size !== '') {
    size = String(args.size).trim().toLowerCase().replace('×', 'x');
    if (!IMAGE_SIZES.includes(size)) {
      return { ok: false, error: `Error: size must be one of ${IMAGE_SIZES.join(', ')}.` };
    }
  }
  let rel: string;
  if (typeof args.path === 'string' && args.path.trim()) {
    rel = args.path.trim();
    if (rel === '~' || rel.startsWith('~/')) rel = path.join(os.homedir(), rel.slice(rel === '~' ? 1 : 2));   // as resolvePath does
    // A folder, not a file name: ".png" inside it would be a hidden file nobody asked for.
    if (/[\\/]$/.test(rel) || ['.', '..', ''].includes(path.basename(rel))) {
      return { ok: false, error: `Error: ${args.path} is a folder: give the picture's file name, ending in .png.` };
    }
    const ext = path.extname(rel).toLowerCase();
    if (ext && ext !== '.png') return { ok: false, error: 'Error: the picture is saved as a PNG: the path must end in .png.' };
    if (!ext) rel += '.png';
  } else {
    const base = path.join('images', slugOfPrompt(prompt));
    rel = `${base}.png`;
    for (let n = 2; fs.existsSync(path.resolve(root, rel)) && n < 1000; n++) rel = `${base}-${n}.png`;
  }
  const target = path.resolve(root, rel);
  if (!lexicallyInside(root, target) || target === path.resolve(root)) {
    return { ok: false, error: `Error: ${args.path ?? rel} is outside the workspace: pictures are saved inside it.` };
  }
  return { ok: true, args: { prompt, path: target, size } };
}

/** What one HTTP call to the coordinator came back with. `status` 0: no answer (unreachable, timed out). */
export interface ImageReply {
  status: number;
  body: any;
  /** Retry-After in seconds, when the coordinator sent one. */
  retryAfter?: number;
}

export interface ImageApi {
  request(method: 'GET' | 'POST', apiPath: string, body?: unknown, signal?: AbortSignal): Promise<ImageReply>;
}

export interface ImageRun {
  signal?: AbortSignal;
  /** The status line: elapsed time against the estimate. */
  onProgress?: (text: string) => void;
  /** Tests replace these. */
  sleep?: (ms: number, signal?: AbortSignal) => Promise<void>;
  now?: () => number;
}

export interface ImageOutcome {
  ok: boolean;
  /** What the model is told. */
  text: string;
  png?: Buffer;
  width?: number;
  height?: number;
  seed?: number;
}

function abortableSleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal?.aborted) return resolve();
    const timer = setTimeout(done, ms);
    function done() {
      clearTimeout(timer);
      signal?.removeEventListener('abort', done);
      resolve();
    }
    signal?.addEventListener('abort', done, { once: true });
  });
}

/** The server's own sentence (`detail`), with how long to wait when it said so; never the raw body. */
function refusalText(reply: ImageReply): string {
  const detail = typeof reply.body?.detail === 'string' && reply.body.detail.trim()
    ? reply.body.detail.trim()
    : reply.status === 0 ? String(reply.body?.detail || 'The coordinator could not be reached.') : `The coordinator answered HTTP ${reply.status}.`;
  const wait = reply.retryAfter && reply.status === 429 ? ` Try again in ${reply.retryAfter < 90 ? `${reply.retryAfter} s` : `${Math.ceil(reply.retryAfter / 60)} min`}.` : '';
  return `${detail}${wait}`;
}

const WORKING = new Set(['queued', 'checking', 'generating', 'reviewing']);

function progressLine(job: any, elapsed: number): string {
  const eta = Number(job?.eta_seconds);
  const hasEta = Number.isFinite(eta) && eta > 0;
  switch (job?.status) {
    case 'queued': {
      const place = Number(job.position);
      return `🎨 Waiting for the image engine${Number.isFinite(place) && place > 0 ? ` (place ${place})` : ''}... ${elapsed} s${hasEta ? ` of about ${Math.round(eta)} s` : ''}`;
    }
    case 'checking': return `🎨 Checking the prompt... ${elapsed} s`;
    case 'reviewing': return `🎨 Checking the picture... ${elapsed} s`;
    default: return `🎨 Creating your image... ${elapsed} s${hasEta ? ` of about ${Math.round(eta)} s` : ''}`;
  }
}

/**
 * Submit the job, poll it every 2 s and return the PNG. Stopping the run cancels the job at the coordinator. Every
 * refusal and failure comes back as `ok: false` with the server's `detail`.
 */
export async function runImageJob(api: ImageApi, args: ImageArgs, run: ImageRun = {}): Promise<ImageOutcome> {
  const sleep = run.sleep ?? abortableSleep;
  const now = run.now ?? Date.now;
  const fail = (text: string): ImageOutcome => ({ ok: false, text });

  const body: Record<string, unknown> = { prompt: args.prompt };
  if (args.size) body.size = args.size;
  const created = await api.request('POST', '/v1/images', body, run.signal);
  if (created.status !== 202 && created.status !== 200) return fail(refusalText(created));
  const id = created.body?.id;
  // The id goes into a URL: only what the contract says an id looks like (random, URL-safe) is used.
  if (typeof id !== 'string' || !/^[A-Za-z0-9_-]{8,128}$/.test(id)) return fail('The coordinator did not return a valid image job.');

  const cancel = async () => {
    try { await api.request('POST', `/v1/images/${id}/cancel`); } catch { /* the job times out on its own */ }
  };
  const started = now();
  let failures = 0;
  let job: any = created.body;
  for (;;) {
    if (run.signal?.aborted) {
      await cancel();
      return fail('Cancelled: the user stopped the task, and the image job was cancelled.');
    }
    if (now() - started > GIVE_UP_AFTER_MS) {
      await cancel();
      return fail('The image took too long and was cancelled. Try again later.');
    }
    await sleep(POLL_EVERY_MS, run.signal);
    if (run.signal?.aborted) continue;
    const reply = await api.request('GET', `/v1/images/${id}`, undefined, run.signal);
    if (run.signal?.aborted) continue;
    if (reply.status !== 200) {
      // The job is gone (404) or refused us (4xx): final. A blip (network, 5xx) is retried a few times.
      if (reply.status >= 400 && reply.status < 500) return fail(refusalText(reply));
      if (++failures >= MAX_POLL_FAILURES) { await cancel(); return fail(refusalText(reply)); }
      continue;
    }
    failures = 0;
    job = reply.body;
    const status = job?.status;
    if (WORKING.has(status)) {
      const serverElapsed = Number(job.elapsed_seconds);
      run.onProgress?.(progressLine(job, Number.isFinite(serverElapsed) ? Math.round(serverElapsed) : Math.round((now() - started) / 1000)));
      continue;
    }
    if (status === 'completed') {
      const image = job.image;
      const png = typeof image?.b64_png === 'string' ? Buffer.from(image.b64_png, 'base64') : null;
      if (!png || png.length < PNG_SIGNATURE.length || png.length > MAX_IMAGE_BYTES || !png.subarray(0, 8).equals(PNG_SIGNATURE)) {
        return fail('The coordinator returned something that is not a PNG picture.');
      }
      return { ok: true, text: '', png, width: Number(image.width), height: Number(image.height), seed: Number(image.seed) };
    }
    if (status === 'cancelled') return fail('The image job was cancelled.');
    if (status === 'expired') return fail('The picture expired at the coordinator before it was fetched. Ask again.');
    // refused, failed, or a status this client does not know: the server's own sentence.
    const detail = typeof job?.error?.detail === 'string' && job.error.detail.trim() ? job.error.detail.trim() : '';
    if (status === 'refused') return fail(detail || 'The image request was refused.');
    return fail(detail || (status === 'failed' ? 'The image could not be created.' : `Unexpected image job status: ${String(status)}.`));
  }
}

/** The line the model gets after a save: "Saved images/x.png (768×768, seed 123)". */
export function savedText(relPath: string, outcome: ImageOutcome): string {
  const dims = Number.isFinite(outcome.width) && Number.isFinite(outcome.height) ? `${outcome.width}×${outcome.height}` : 'size unknown';
  const seed = Number.isFinite(outcome.seed) ? `, seed ${outcome.seed}` : '';
  return `Saved ${relPath} (${dims}${seed})`;
}
