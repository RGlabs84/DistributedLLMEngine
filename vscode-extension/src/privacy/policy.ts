// Privacy routing policy (docs/coding-members.md, section 7).
//
// Code from a public repository may run on any coding member (a volunteer's machine). Private code
// runs only on trusted machines (TheWall and the members the operator trusts) unless its owner opts
// in. This module decides which of the three modes a workspace is in; the coordinator enforces it
// (engine/privacy_policy.py) and never upgrades what it is told.
//
//   open     the workspace's git remote is a public repository          -> any coding member
//   trusted  everything else, and every doubt                            -> trusted machines only
//   any      the user chose dlne.privacy = "anyMember"                   -> any coding member
//
// THE ONE RULE: any doubt is `trusted`. No remote, several remotes, a host that is not github.com,
// gitlab.com or codeberg.org (an enterprise host, a look-alike), a remote that does not parse, a rate
// limit, no network, a slow answer, a private fork of a public repository: all `trusted`. Only a
// clear "this repository is public" from the host's own API makes a workspace `open`.
//
// WHAT LEAVES THE MACHINE: one unauthenticated GET to the repository host's public API, naming the
// repository the remote already names. No token, no cookie, no code, no user identity. The user's
// credentials (a token embedded in a remote URL, `dlne.apiKey`, a GitHub login) are never read here,
// let alone sent.

import * as fs from 'fs';
import * as http from 'http';
import * as https from 'https';
import * as path from 'path';
import * as vscode from 'vscode';

export type PrivacyMode = 'open' | 'trusted' | 'any';
export type PrivacySetting = 'auto' | 'trustedOnly' | 'anyMember';

/** The key under `dlne.` (the full name is `dlne.privacy`). */
export const PRIVACY_SETTING_KEY = 'privacy';
/** A "public" answer is kept this long, per remote: short, so a repository made private stops being `open` quickly. */
export const PUBLIC_TTL_MS = 60 * 60 * 1000;
/** A "not public" answer (private, or a 404) is kept this long: safe whichever way it later changes. */
export const NOT_PUBLIC_TTL_MS = 24 * 60 * 60 * 1000;
/** A failure (rate limit, offline, timeout, odd answer) is kept briefly so each request does not wait on it again. */
export const FAILURE_TTL_MS = 5 * 60 * 1000;
/** How long the lookup may take before it counts as a failure. */
export const LOOKUP_TIMEOUT_MS = 3000;

const MAX_ANSWER_BYTES = 256 * 1024;
const MAX_CONFIG_BYTES = 1024 * 1024;
const MAX_CACHE_ENTRIES = 256;

// ---------------------------------------------------------------------------------------------
// 1. Remote URLs
// ---------------------------------------------------------------------------------------------

export type RepoHost = 'github.com' | 'gitlab.com' | 'codeberg.org';

export interface RepoRef {
  host: RepoHost;
  /** `owner/repo` (GitLab: `group/subgroup/project`), without `.git`, as written in the remote. */
  path: string;
}

const KNOWN_HOSTS: ReadonlySet<string> = new Set<string>(['github.com', 'gitlab.com', 'codeberg.org']);
const PATH_SEGMENT = /^[A-Za-z0-9._-]+$/;
const MAX_REMOTE_LENGTH = 512;
const MAX_GITLAB_DEPTH = 20;

/** The key a repository is cached under: case does not matter on any of the three hosts. */
export function repoKey(ref: RepoRef): string {
  return `${ref.host}/${ref.path.toLowerCase()}`;
}

/**
 * The repository a git remote URL names, or undefined unless it is plainly on github.com, gitlab.com
 * or codeberg.org. Understands https/http/git/ssh URLs (a token or user name before the host is
 * ignored, never kept) and the scp form `git@host:owner/repo.git`. Enterprise hosts, look-alikes
 * (`github.com.evil.com`, `https://github.com@evil.com/...`), odd ports, query strings, encoded paths
 * and anything that is not exactly an owner/repo path are undefined: the caller treats that as a doubt.
 */
export function parseRemoteUrl(raw: unknown): RepoRef | undefined {
  if (typeof raw !== 'string') return undefined;
  const text = raw.trim();
  if (!text || text.length > MAX_REMOTE_LENGTH) return undefined;
  if (/[\s\\\u0000-\u001f\u007f?#]/.test(text)) return undefined;

  let host: string;
  let repoPath: string;
  if (text.includes('://')) {
    let url: URL;
    try {
      url = new URL(text);
    } catch {
      return undefined;
    }
    if (!['https:', 'http:', 'git:', 'ssh:'].includes(url.protocol)) return undefined;
    // Default ports parse to ''. The only other port accepted is ssh's own.
    if (url.port && !(url.protocol === 'ssh:' && url.port === '22')) return undefined;
    host = url.hostname.toLowerCase();
    repoPath = url.pathname;
  } else {
    const scp = /^(?:[A-Za-z0-9._-]+@)?([A-Za-z0-9.-]+):(.+)$/.exec(text);
    if (!scp) return undefined;
    host = scp[1].toLowerCase();
    repoPath = scp[2];
  }
  if (!KNOWN_HOSTS.has(host)) return undefined;
  if (repoPath.includes('%')) return undefined;

  let rest = repoPath;
  if (rest.startsWith('/')) rest = rest.slice(1);
  if (rest.endsWith('/')) rest = rest.slice(0, -1);
  if (rest.toLowerCase().endsWith('.git')) rest = rest.slice(0, -4);
  const segments = rest.split('/');
  if (!segments.every((s) => PATH_SEGMENT.test(s) && s !== '.' && s !== '..')) return undefined;
  if (host === 'gitlab.com') {
    if (segments.length < 2 || segments.length > MAX_GITLAB_DEPTH) return undefined;
  } else if (segments.length !== 2) {
    return undefined;
  }
  return { host: host as RepoHost, path: segments.join('/') };
}

// ---------------------------------------------------------------------------------------------
// 2. The workspace's git remote (.git/config)
// ---------------------------------------------------------------------------------------------

export interface GitConfigRemotes {
  urls: string[];
  pushUrls: string[];
  /** The file used something this reader does not follow (an include, a continued line, a value it cannot read). */
  unsure: boolean;
}

const GIT_ESCAPES: Readonly<Record<string, string>> = { n: '\n', t: '\t', b: '\b', '"': '"', '\\': '\\' };

/** A git config value: double quotes group, `#` or `;` outside quotes starts a comment. Undefined when unsure. */
function parseConfigValue(raw: string): string | undefined {
  let out = '';
  let quoted = false;
  for (let i = 0; i < raw.length; i++) {
    const c = raw[i];
    if (c === '\\') {
      const next = raw[i + 1];
      if (next === undefined || !Object.prototype.hasOwnProperty.call(GIT_ESCAPES, next)) return undefined;
      out += GIT_ESCAPES[next];
      i++;
    } else if (c === '"') {
      quoted = !quoted;
    } else if (!quoted && (c === '#' || c === ';')) {
      break;
    } else {
      out += c;
    }
  }
  return quoted ? undefined : out.trim();
}

/** Every `remote.<name>.url` and `.pushurl` in a .git/config. Reads only what it understands; the rest is `unsure`. */
export function parseGitConfigRemotes(text: string): GitConfigRemotes {
  const result: GitConfigRemotes = { urls: [], pushUrls: [], unsure: false };
  let inRemote = false;
  const lines = text.replace(/^﻿/, '').split(/\r\n|\n|\r/);
  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (!line || line.startsWith('#') || line.startsWith(';')) continue;

    const section = /^\[\s*([A-Za-z0-9.-]+)\s*(?:"((?:[^"\\]|\\.)*)")?\s*\](.*)$/.exec(line);
    if (section) {
      const after = section[3].trim();
      if (after && !after.startsWith('#') && !after.startsWith(';')) result.unsure = true; // a key on the header line
      let name = section[1].toLowerCase();
      let sub = section[2];
      if (sub === undefined && name.includes('.')) {
        // The old `[remote.origin]` spelling.
        sub = name.slice(name.indexOf('.') + 1);
        name = name.slice(0, name.indexOf('.'));
      }
      if (name === 'include' || name === 'includeif') result.unsure = true;
      inRemote = name === 'remote' && sub !== undefined;
      continue;
    }

    const entry = /^([A-Za-z][A-Za-z0-9-]*)\s*(?:=\s*(.*))?$/.exec(line);
    if (!entry) {
      result.unsure = true;
      continue;
    }
    if (!inRemote) continue;
    const key = entry[1].toLowerCase();
    if (key !== 'url' && key !== 'pushurl') continue;
    const value = entry[2] === undefined ? undefined : parseConfigValue(entry[2]);
    if (!value) {
      result.unsure = true; // empty (resets the list in git), unreadable or a bare key
      continue;
    }
    (key === 'url' ? result.urls : result.pushUrls).push(value);
  }
  return result;
}

export type RemoteReading =
  | { kind: 'ok'; url: string; pushUrls: string[] }
  | { kind: 'none' | 'several' | 'unreadable' };

/** Where `<folder>/.git` keeps its config: the folder itself, or, for a worktree or submodule, what the `.git` file points at. */
async function findGitConfig(folder: string): Promise<string | undefined> {
  const dotGit = path.join(folder, '.git');
  const info = await fs.promises.stat(dotGit);
  if (info.isDirectory()) return path.join(dotGit, 'config');
  if (!info.isFile() || info.size > 4096) return undefined;
  const pointer = /^gitdir:\s*(.+?)\s*$/m.exec(await fs.promises.readFile(dotGit, 'utf8'));
  if (!pointer) return undefined;
  const gitDir = path.resolve(folder, pointer[1]);
  try {
    // A linked worktree shares its repository's config, named by `commondir`.
    const common = (await fs.promises.readFile(path.join(gitDir, 'commondir'), 'utf8')).trim();
    if (common) return path.join(path.resolve(gitDir, common), 'config');
  } catch {
    // Not a linked worktree (a submodule, for one): the config is next to the pointer's target.
  }
  return path.join(gitDir, 'config');
}

/**
 * The one remote of the repository at `folder` (not a parent folder: VS Code itself asks before it
 * adopts a parent's repository, and a dotfiles repository in `~` must not make every folder under it public).
 */
export async function readGitRemote(folder: string): Promise<RemoteReading> {
  let configFile: string | undefined;
  try {
    configFile = await findGitConfig(folder);
  } catch (err) {
    const code = (err as NodeJS.ErrnoException).code;
    return { kind: code === 'ENOENT' || code === 'ENOTDIR' ? 'none' : 'unreadable' };
  }
  if (!configFile) return { kind: 'unreadable' };
  let text: string;
  try {
    if ((await fs.promises.stat(configFile)).size > MAX_CONFIG_BYTES) return { kind: 'unreadable' };
    text = await fs.promises.readFile(configFile, 'utf8');
  } catch {
    return { kind: 'unreadable' };
  }
  const found = parseGitConfigRemotes(text);
  if (found.unsure) return { kind: 'unreadable' };
  if (found.urls.length === 0) return { kind: 'none' };
  if (found.urls.length > 1) return { kind: 'several' };
  return { kind: 'ok', url: found.urls[0], pushUrls: found.pushUrls };
}

// ---------------------------------------------------------------------------------------------
// 3. Is the repository public? One unauthenticated lookup, cached
// ---------------------------------------------------------------------------------------------

export type Visibility = 'public' | 'notPublic' | 'unknown';
export interface HttpReply {
  status: number;
  body: string;
}
/** A plain GET that follows no redirect and carries only the headers it is given. */
export type HttpGet = (url: string, headers: Record<string, string>, timeoutMs: number) => Promise<HttpReply>;

/** The public API URL that says whether `ref` is public. The path segments are already limited to [A-Za-z0-9._-]. */
export function apiUrl(ref: RepoRef): string {
  switch (ref.host) {
    case 'github.com':
      return `https://api.github.com/repos/${ref.path}`;
    case 'codeberg.org':
      return `https://codeberg.org/api/v1/repos/${ref.path}`;
    case 'gitlab.com':
      return `https://gitlab.com/api/v4/projects/${encodeURIComponent(ref.path)}`;
  }
}

/** The headers of the lookup. Nothing identifying: no Authorization, no Cookie. */
export function lookupHeaders(): Record<string, string> {
  return {
    Accept: 'application/vnd.github+json, application/json',
    'User-Agent': 'dlne-code-assistant (public repository check)',
    Connection: 'close',
  };
}

/**
 * What a host's answer says. Only a 200 that names this very repository and says it is public is
 * `public`. A 404 is how all three hosts answer for a private repository (and for one that does
 * not exist): `notPublic`. A rate limit (403, 429), a redirect (renamed or transferred), a server
 * error or a body that is not what was asked for is `unknown`.
 */
export function interpretReply(ref: RepoRef, status: number, body: string): Visibility {
  if (status === 404) return 'notPublic';
  if (status !== 200) return 'unknown';
  let data: unknown;
  try {
    data = JSON.parse(body);
  } catch {
    return 'unknown';
  }
  if (!data || typeof data !== 'object' || Array.isArray(data)) return 'unknown';
  const d = data as Record<string, unknown>;
  const wanted = ref.path.toLowerCase();

  if (ref.host === 'gitlab.com') {
    if (typeof d.path_with_namespace !== 'string' || d.path_with_namespace.toLowerCase() !== wanted) return 'unknown';
    if (typeof d.visibility !== 'string') return 'unknown';
    if (d.visibility !== 'public') return 'notPublic'; // private, internal
    // A public project can still keep its repository to members.
    if (typeof d.repository_access_level === 'string' && d.repository_access_level !== 'enabled') return 'notPublic';
    return 'public';
  }

  // GitHub and Codeberg (Forgejo/Gitea) answer with the same `full_name` and `private` fields.
  if (typeof d.full_name !== 'string' || d.full_name.toLowerCase() !== wanted) return 'unknown';
  if (d.private === true || d.internal === true) return 'notPublic';
  if (d.private !== false) return 'unknown';
  if (ref.host === 'github.com' && d.visibility !== undefined && d.visibility !== 'public') return 'notPublic';
  return 'public';
}

/** The real GET, over node's http(s) (VS Code's proxy support applies to it), capped in time and size, never following a redirect. */
export const defaultHttpGet: HttpGet = (url, headers, timeoutMs) =>
  new Promise<HttpReply>((resolve, reject) => {
    const transport = url.startsWith('https:') ? https : http;
    let settled = false;
    const finish = (fn: () => void) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      fn();
    };
    const req = transport.get(url, { headers }, (res) => {
      const chunks: Buffer[] = [];
      let size = 0;
      res.on('data', (chunk: Buffer) => {
        size += chunk.length;
        if (size > MAX_ANSWER_BYTES) {
          finish(() => reject(new Error('answer too large')));
          req.destroy();
          return;
        }
        chunks.push(chunk);
      });
      res.on('end', () => finish(() => resolve({ status: res.statusCode ?? 0, body: Buffer.concat(chunks).toString('utf8') })));
      res.on('error', (err) => finish(() => reject(err)));
    });
    const timer = setTimeout(() => {
      finish(() => reject(new Error('timeout')));
      req.destroy();
    }, timeoutMs);
    req.on('error', (err) => finish(() => reject(err)));
  });

/** Answers kept per repository: "public" for an hour, "not public" for a day, a failure for a few minutes, a lookup in flight shared. */
export class PublicRepoCache {
  private entries = new Map<string, { visibility: Visibility; expires: number }>();
  private inflight = new Map<string, Promise<Visibility>>();

  get(key: string, now: number): Visibility | undefined {
    const hit = this.entries.get(key);
    if (!hit) return undefined;
    if (hit.expires <= now) {
      this.entries.delete(key);
      return undefined;
    }
    return hit.visibility;
  }

  set(key: string, visibility: Visibility, now: number): void {
    this.entries.delete(key);
    const ttl = visibility === 'public' ? PUBLIC_TTL_MS : visibility === 'notPublic' ? NOT_PUBLIC_TTL_MS : FAILURE_TTL_MS;
    this.entries.set(key, { visibility, expires: now + ttl });
    while (this.entries.size > MAX_CACHE_ENTRIES) {
      this.entries.delete(this.entries.keys().next().value as string);
    }
  }

  /** The cached answer, or the lookup already running for this key, or a new one started with `lookup`. */
  async resolve(key: string, now: () => number, lookup: () => Promise<Visibility>): Promise<Visibility> {
    const hit = this.get(key, now());
    if (hit) return hit;
    const running = this.inflight.get(key);
    if (running) return running;
    const started = lookup().then(
      (visibility) => {
        this.set(key, visibility, now());
        return visibility;
      },
      () => {
        this.set(key, 'unknown', now());
        return 'unknown' as Visibility;
      },
    ).finally(() => this.inflight.delete(key));
    this.inflight.set(key, started);
    return started;
  }

  clear(): void {
    this.entries.clear();
    this.inflight.clear();
  }
}

export interface PrivacyDeps {
  /** Replaces the real GET (tests). */
  httpGet?: HttpGet;
  now?: () => number;
  timeoutMs?: number;
  /** Replaces the shared cache (tests). */
  cache?: PublicRepoCache;
}

const sharedCache = new PublicRepoCache();

/** Forget every cached answer (tests, and a way to force a fresh look). */
export function resetPrivacyCache(): void {
  sharedCache.clear();
}

async function lookupVisibility(ref: RepoRef, deps: PrivacyDeps): Promise<Visibility> {
  const get = deps.httpGet ?? defaultHttpGet;
  const timeoutMs = deps.timeoutMs ?? LOOKUP_TIMEOUT_MS;
  let timer: NodeJS.Timeout | undefined;
  try {
    const pending = get(apiUrl(ref), lookupHeaders(), timeoutMs);
    pending.catch(() => undefined); // if the timeout wins, a late failure is nobody's news
    const reply = await Promise.race([
      pending,
      new Promise<never>((_, reject) => {
        timer = setTimeout(() => reject(new Error('timeout')), timeoutMs);
      }),
    ]);
    return interpretReply(ref, reply.status, reply.body);
  } catch {
    return 'unknown';
  } finally {
    if (timer) clearTimeout(timer);
  }
}

/** True only when the host clearly says `ref` is public. Cached per repository. */
export async function isPublicRepo(ref: RepoRef, deps: PrivacyDeps = {}): Promise<boolean> {
  const cache = deps.cache ?? sharedCache;
  const visibility = await cache.resolve(repoKey(ref), deps.now ?? Date.now, () => lookupVisibility(ref, deps));
  return visibility === 'public';
}

/** Is the repository checked out at `folder` public? False on any doubt. */
export async function isPublicWorkspace(folder: string, deps: PrivacyDeps = {}): Promise<boolean> {
  const remote = await readGitRemote(folder);
  if (remote.kind !== 'ok') return false;
  const ref = parseRemoteUrl(remote.url);
  if (!ref) return false;
  // A push URL that goes somewhere else is a second repository: a doubt.
  for (const push of remote.pushUrls) {
    const other = parseRemoteUrl(push);
    if (!other || repoKey(other) !== repoKey(ref)) return false;
  }
  return isPublicRepo(ref, deps);
}

// ---------------------------------------------------------------------------------------------
// 4. The setting and the mode
// ---------------------------------------------------------------------------------------------

/**
 * What `dlne.privacy` holds. Unset is `auto`. A value that is not one of the three (a typo, a
 * hand-edited settings file) is `invalid`, and the mode for it is `trusted`: garbage never relaxes anything.
 */
export function normalizePrivacySetting(value: unknown): PrivacySetting | 'invalid' {
  if (value === undefined || value === null) return 'auto';
  if (typeof value !== 'string') return 'invalid';
  const v = value.trim();
  return v === 'auto' || v === 'trustedOnly' || v === 'anyMember' ? v : 'invalid';
}

/** From the narrowest audience to the widest. */
const SETTING_RANK: Readonly<Record<PrivacySetting, number>> = { trustedOnly: 0, auto: 1, anyMember: 2 };

/**
 * The setting that applies: the USER's own choice (or the default), narrowed by a folder's or workspace's
 * value but never widened by one. A cloned repository's .vscode/settings.json is read like the user's own by
 * `get()`, so without this a repository could write {"dlne.privacy": "anyMember"} and, once the folder is
 * trusted, decide who may read the user's code. A value that is not one of the three is `invalid` (trusted).
 * A configuration object without `inspect` (a test double) is read with `get()`.
 */
export function effectivePrivacySetting(config: {
  get<T>(key: string, fallback?: T): T | undefined;
  inspect?<T>(key: string): { globalValue?: T; workspaceValue?: T; workspaceFolderValue?: T } | undefined;
}): PrivacySetting | 'invalid' {
  const seen = typeof config.inspect === 'function' ? config.inspect<unknown>(PRIVACY_SETTING_KEY) : undefined;
  if (!seen) return normalizePrivacySetting(config.get<unknown>(PRIVACY_SETTING_KEY, 'auto'));
  const chosen = [seen.globalValue === undefined ? 'auto' : seen.globalValue, seen.workspaceValue, seen.workspaceFolderValue]
    .filter((value) => value !== undefined)
    .map((value) => normalizePrivacySetting(value));
  if (chosen.includes('invalid')) return 'invalid';
  return (chosen as PrivacySetting[]).reduce((narrowest, next) => (SETTING_RANK[next] < SETTING_RANK[narrowest] ? next : narrowest));
}

export type WorkspaceFolderLike =
  | vscode.WorkspaceFolder
  | { uri: { fsPath: string; scheme?: string } }
  | { fsPath: string; scheme?: string }
  | string;

interface ResolvedFolder {
  fsPath: string;
  uri?: vscode.Uri;
}

function resolveFolder(folder: WorkspaceFolderLike | undefined): ResolvedFolder | undefined {
  if (typeof folder === 'string') return folder ? { fsPath: folder } : undefined;
  if (!folder || typeof folder !== 'object') return undefined;
  const uri = ('uri' in folder ? folder.uri : folder) as { fsPath?: unknown; scheme?: unknown } | undefined;
  if (!uri || typeof uri.fsPath !== 'string' || !uri.fsPath) return undefined;
  // A virtual or remote-only folder (vscode-vfs://, untitled:) has no .git to read here.
  if (uri.scheme !== undefined && uri.scheme !== 'file') return undefined;
  return { fsPath: uri.fsPath, uri: uri as vscode.Uri };
}

/** The folder to judge when the caller names none: the workspace's only folder. Several folders: no answer, so `trusted`. */
function soleWorkspaceFolder(): vscode.WorkspaceFolder | undefined {
  const folders = vscode.workspace.workspaceFolders;
  return folders && folders.length === 1 ? folders[0] : undefined;
}

/**
 * The privacy mode of a workspace folder (docs/coding-members.md, section 7).
 *
 *   dlne.privacy = "trustedOnly"  -> 'trusted'
 *   dlne.privacy = "anyMember"    -> 'any'
 *   dlne.privacy = "auto" (default) -> 'open' if the folder's git remote is a public repository, else 'trusted'
 *   anything else, or any error   -> 'trusted'
 *
 * Never throws. `deps` is for tests.
 */
export async function getPrivacyMode(workspaceFolder?: WorkspaceFolderLike, deps: PrivacyDeps = {}): Promise<PrivacyMode> {
  try {
    const folder = resolveFolder(workspaceFolder ?? soleWorkspaceFolder());
    const setting = effectivePrivacySetting(vscode.workspace.getConfiguration('dlne', folder?.uri));
    if (setting === 'anyMember') return 'any';
    if (setting !== 'auto') return 'trusted'; // trustedOnly, or a value we do not know
    if (!folder) return 'trusted';
    return (await isPublicWorkspace(folder.fsPath, deps)) ? 'open' : 'trusted';
  } catch {
    return 'trusted';
  }
}

// ---------------------------------------------------------------------------------------------
// 5. A small hint: which mode is this workspace in?
// ---------------------------------------------------------------------------------------------

export function describePrivacyMode(mode: PrivacyMode): { text: string; tooltip: string } {
  switch (mode) {
    case 'open':
      return {
        text: '$(globe) Public',
        tooltip:
          "DLNE privacy: this workspace's repository is public, so its code may run on any coding member in the swarm. That includes changes you have not pushed yet: for unpublished work set dlne.privacy to \"trustedOnly\".\nClick to change dlne.privacy.",
      };
    case 'any':
      return {
        text: '$(unlock) Any machine',
        tooltip:
          'DLNE privacy: dlne.privacy is "anyMember", so this workspace\'s code may run on any coding member in the swarm, even if the repository is private.\nClick to change dlne.privacy.',
      };
    default:
      return {
        text: '$(lock) Trusted',
        tooltip:
          "DLNE privacy: this workspace's code runs only on DLNE's own machines and members the operator trusts. DLNE did not find a public repository here (or dlne.privacy is \"trustedOnly\").\nClick to change dlne.privacy.",
      };
  }
}

type Listen = (listener: () => unknown) => { dispose(): unknown };

/** A status bar item (right, next to the cluster item) that shows the current workspace's mode. Click opens the setting. */
export class PrivacyStatus implements vscode.Disposable {
  private readonly item: vscode.StatusBarItem;
  private readonly subscriptions: { dispose(): unknown }[] = [];
  private generation = 0;
  /** Settles when the first look at the workspace is on screen. */
  readonly ready: Promise<void>;

  constructor(private readonly deps: PrivacyDeps = {}) {
    this.item = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 99);
    this.item.command = { command: 'workbench.action.openSettings', title: 'DLNE privacy', arguments: ['dlne.' + PRIVACY_SETTING_KEY] };
    const refresh = () => void this.refresh();
    const listen = (event: Listen | undefined) => {
      if (typeof event === 'function') this.subscriptions.push(event(refresh));
    };
    listen(vscode.workspace.onDidChangeWorkspaceFolders as Listen | undefined);
    listen(vscode.window.onDidChangeActiveTextEditor as Listen | undefined);
    if (typeof vscode.workspace.onDidChangeConfiguration === 'function') {
      this.subscriptions.push(
        vscode.workspace.onDidChangeConfiguration((e) => {
          if (e.affectsConfiguration('dlne.' + PRIVACY_SETTING_KEY)) refresh();
        }),
      );
    }
    this.ready = this.refresh();
  }

  private currentFolder(): vscode.WorkspaceFolder | undefined {
    const doc = vscode.window.activeTextEditor?.document;
    const owner = doc && typeof vscode.workspace.getWorkspaceFolder === 'function' ? vscode.workspace.getWorkspaceFolder(doc.uri) : undefined;
    return owner ?? vscode.workspace.workspaceFolders?.[0];
  }

  async refresh(): Promise<void> {
    const mine = ++this.generation;
    const folder = this.currentFolder();
    if (!folder) {
      this.item.hide();
      return;
    }
    const mode = await getPrivacyMode(folder, this.deps);
    if (mine !== this.generation) return; // a newer refresh is on its way
    const shown = describePrivacyMode(mode);
    this.item.text = shown.text;
    this.item.tooltip = shown.tooltip;
    this.item.show();
  }

  dispose(): void {
    this.generation++;
    for (const s of this.subscriptions) s.dispose();
    this.subscriptions.length = 0;
    this.item.dispose();
  }
}

/** Create the hint. Add the result to `context.subscriptions`. */
export function createPrivacyStatus(deps: PrivacyDeps = {}): PrivacyStatus {
  return new PrivacyStatus(deps);
}
