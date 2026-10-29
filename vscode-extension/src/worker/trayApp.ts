// DLNE Donate, the tray app for Windows and Linux (desktop/dlne_donate), as the extension sees it. Two jobs live here:
//
//   1. Offering it ("Optional add-on" in the Donate compute panel): download, verify, run. It follows the Android app's
//      own updater (android/app/src/sideload/java/io/dlne/chat/update): read a small manifest from the DLNE website,
//      accept only an https download on DLNE's own host, hash and count the bytes as they arrive, keep the file only
//      when it is exactly the published size and SHA-256, and run it only when the owner says so. The manifest and the
//      download are fetched when the owner clicks, never before, and nothing else is sent anywhere.
//   2. Recognising the tray app's worker, so the worker manager shows it instead of starting a second worker beside it.
//
// Nothing here imports `vscode`, so all of it runs under plain Node in the tests. worker/trayAddon.ts is the flow the
// panel's button runs, with the dialogs.

import * as child_process from 'child_process';
import * as crypto from 'crypto';
import * as fs from 'fs';
import * as https from 'https';
import * as os from 'os';
import * as path from 'path';

// ---------------------------------------------------------------------------------------------
// Where DLNE Donate comes from
// ---------------------------------------------------------------------------------------------

/** The one host a manifest, and every download it names, may be on. Fixed: it never follows dlne.serverUrl. */
export const TRAY_HOST = 'dlne.rossgilsonlabs.dev';
/** The manifest the website publishes next to downloads/android.json (the phone app's). */
export const TRAY_MANIFEST_URL = `https://${TRAY_HOST}/downloads/desktop.json`;
/** The DLNE page's tray app tab (the site opens that tab for #dlne-tray), for anyone who would rather download it there. */
export const TRAY_PAGE_URL = `https://${TRAY_HOST}/#dlne-tray`;

/** The manifest is a few hundred bytes; anything bigger is not one. */
const MAX_MANIFEST_BYTES = 16 * 1024;
/** The builds are 30 to 60 MB (PyInstaller with Python, Tk and the engine's libraries); under 1 MB is not the app. */
const MIN_BUILD_BYTES = 1_000_000;
const MAX_BUILD_BYTES = 500 * 1024 * 1024;
const MAX_URL_CHARS = 300;
/** Notes longer than this are refused; what is shown is cleaned and cut to MAX_NOTES_CHARS. */
const MAX_RAW_NOTES = 2_000;
const MAX_NOTES_CHARS = 300;

/** The two platforms DLNE Donate is built for. Both builds are x86-64 (PE32+ x86-64, and an x86_64 AppImage). */
export type TrayPlatform = 'windows' | 'linux';

/**
 * This computer's DLNE Donate build, or null when there is none: macOS, every other system, and an ARM computer (an
 * x86-64 AppImage cannot run on ARM Linux, and the Windows build would only run emulated). Null hides the panel's card.
 */
export function trayPlatform(platform: string = process.platform, arch: string = process.arch): TrayPlatform | null {
  if (arch !== 'x64') return null;
  if (platform === 'win32') return 'windows';
  if (platform === 'linux') return 'linux';
  return null;
}

/** "Windows" or "Linux", for the dialogs. */
export function trayPlatformName(platform: TrayPlatform): string {
  return platform === 'windows' ? 'Windows' : 'Linux';
}

// ---------------------------------------------------------------------------------------------
// The manifest: https://dlne.rossgilsonlabs.dev/downloads/desktop.json
// ---------------------------------------------------------------------------------------------

/** One platform's build, every field checked (parseTrayManifest). */
export interface TrayBuild {
  /** https, on TRAY_HOST, exactly `/downloads/<file>`. */
  url: string;
  /** The last part of the URL: the name the download is saved under. */
  file: string;
  /** 64 lowercase hex digits. */
  sha256: string;
  /** Bytes. */
  size: number;
}

/**
 * The manifest, mirroring downloads/android.json:
 *
 *     {"versionName": "1.0.0", "notes": "...", "published": "2026-10-26",
 *      "windows": {"url": "https://dlne.rossgilsonlabs.dev/downloads/DLNE-Donate-1.0.0.exe", "sha256": "<64 hex>", "size": 34348456},
 *      "linux":   {"url": "https://dlne.rossgilsonlabs.dev/downloads/DLNE-Donate-1.0.0-x86_64.AppImage", "sha256": "<64 hex>", "size": 57526776}}
 *
 * `notes` and `published` may be left out, and so may one of the two platforms (not both).
 */
export interface TrayManifest {
  versionName: string;
  /** One plain line, at most MAX_NOTES_CHARS; '' when there are none. */
  notes: string;
  /** `yyyy-mm-dd`, or null. */
  published: string | null;
  builds: Partial<Record<TrayPlatform, TrayBuild>>;
}

// The same version shape the website's own reader accepts for android.json (src/utils/dlneApp.js), so the two agree.
const VERSION_NAME = /^\d{1,4}(?:\.\d{1,4}){1,3}$/;
const SHA256 = /^[0-9a-fA-F]{64}$/;
const ISO_DAY = /^(\d{4})-(\d{2})-(\d{2})$/;
/** The file each platform downloads: a plain name (no folder, no dots in front) with the platform's extension. */
const FILE_NAME: Record<TrayPlatform, RegExp> = {
  windows: /^[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.exe$/,
  linux: /^[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.AppImage$/,
};
/** Characters no official download address has: spaces and controls, backslashes, percent-escapes, user info, query, fragment. */
const URL_REFUSED_CHARS = /[\s\u0000-\u001f\u007f\\%@?#]/;

/**
 * `raw` as an official download for `platform`, or null: https, host exactly TRAY_HOST on the default port, no user
 * info, query or fragment, and a path of exactly `/downloads/<name>.exe` (Windows) or `/downloads/<name>.AppImage` (Linux).
 */
export function officialDownload(raw: unknown, platform: TrayPlatform): { url: string; file: string } | null {
  if (typeof raw !== 'string' || raw.length > MAX_URL_CHARS || URL_REFUSED_CHARS.test(raw)) return null;
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return null;
  }
  if (url.protocol !== 'https:' || url.hostname !== TRAY_HOST || url.port !== '') return null;
  if (url.username || url.password || url.search || url.hash) return null;
  const match = /^\/downloads\/([^/]+)$/.exec(url.pathname);
  if (!match || !FILE_NAME[platform].test(match[1])) return null;
  return { url: url.href, file: match[1] };
}

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

/** A real calendar day written as yyyy-mm-dd (2026-02-30 is not one). */
function isIsoDay(text: string): boolean {
  const d = ISO_DAY.exec(text);
  return !!d && new Date(Date.UTC(+d[1], +d[2] - 1, +d[3])).getUTCDate() === +d[3] && +d[2] >= 1 && +d[2] <= 12;
}

/** One platform's entry, checked; a string says what is wrong with it. */
function parseBuild(raw: unknown, platform: TrayPlatform): TrayBuild | string {
  if (!isObject(raw)) return `${platform} is not an object`;
  const download = officialDownload(raw.url, platform);
  if (!download) return `${platform} url is not an official DLNE download`;
  if (typeof raw.sha256 !== 'string' || !SHA256.test(raw.sha256)) return `${platform} sha256`;
  const size = raw.size;
  if (typeof size !== 'number' || !Number.isInteger(size) || size < MIN_BUILD_BYTES || size > MAX_BUILD_BYTES) {
    return `${platform} size`;
  }
  return { ...download, sha256: raw.sha256.toLowerCase(), size };
}

/**
 * A manifest as read from the website, checked whole: anything malformed, and any download that is not an official
 * one, refuses all of it (a manifest pointing anywhere else is not used at all). Unknown extra fields are ignored.
 * `reason` is short, for the log line and the panel's note.
 */
export function parseTrayManifest(raw: unknown): { ok: true; manifest: TrayManifest } | { ok: false; reason: string } {
  if (!isObject(raw)) return { ok: false, reason: 'not a JSON object' };
  if (typeof raw.versionName !== 'string' || !VERSION_NAME.test(raw.versionName)) return { ok: false, reason: 'versionName' };

  let notes = '';
  if (raw.notes !== undefined) {
    if (typeof raw.notes !== 'string' || raw.notes.length > MAX_RAW_NOTES) return { ok: false, reason: 'notes' };
    // One plain line: no control characters, no line breaks, at most MAX_NOTES_CHARS. (\s also matches the Unicode
    // line and paragraph separators, so they become spaces too.)
    notes = raw.notes.replace(/[\u0000-\u001f\u007f-\u009f]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, MAX_NOTES_CHARS);
  }
  let published: string | null = null;
  if (raw.published !== undefined) {
    if (typeof raw.published !== 'string' || !isIsoDay(raw.published)) return { ok: false, reason: 'published' };
    published = raw.published;
  }

  const builds: Partial<Record<TrayPlatform, TrayBuild>> = {};
  for (const platform of ['windows', 'linux'] as const) {
    if (raw[platform] === undefined) continue;
    const build = parseBuild(raw[platform], platform);
    if (typeof build === 'string') return { ok: false, reason: build };
    builds[platform] = build;
  }
  if (!builds.windows && !builds.linux) return { ok: false, reason: 'no build for Windows or Linux' };
  return { ok: true, manifest: { versionName: raw.versionName, notes, published, builds } };
}

/** 34348456 -> "34.3 MB", 640000 -> "640 KB": decimal units, as the website writes the APK's size. */
export function formatSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '';
  return bytes < 1e6 ? `${Math.max(1, Math.round(bytes / 1e3))} KB` : `${(bytes / 1e6).toFixed(1)} MB`;
}

// ---------------------------------------------------------------------------------------------
// Fetching: the manifest and the build, over https to TRAY_HOST only, never following a redirect
// ---------------------------------------------------------------------------------------------

/** What went wrong, in a word, so the flow can say it plainly (worker/trayAddon.ts words each one). */
export type TrayProblem = 'network' | 'http' | 'redirected' | 'too-large' | 'refused' | 'size' | 'hash' | 'save' | 'cancelled';

export class TrayError extends Error {
  constructor(public readonly problem: TrayProblem, message: string) {
    super(message);
    this.name = 'TrayError';
  }
}

/** An answer to a GET: the status, the headers, and the body as it streams in. */
export interface TrayResponse {
  statusCode?: number;
  headers: Record<string, string | string[] | undefined>;
  body: AsyncIterable<Buffer | string>;
  /** Stops the transfer (a refused or finished download). */
  destroy?: () => void;
}
/** Opens a GET. The real one is httpsOpen; the tests hand in their own. */
export type TrayOpen = (url: string) => Promise<TrayResponse>;

/**
 * GET `url` over https, if it is on TRAY_HOST: the TLS certificate is checked (Node's default), a redirect is handed
 * back as it is (never followed, so a file can never be served from somewhere else), and a connection that goes
 * quiet for `idleMs` is dropped.
 */
export function httpsOpen(url: string, idleMs: number = 30_000): Promise<TrayResponse> {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return Promise.reject(new TrayError('refused', 'not a web address'));
  }
  if (parsed.protocol !== 'https:' || parsed.hostname !== TRAY_HOST || parsed.port !== '') {
    return Promise.reject(new TrayError('refused', `only https://${TRAY_HOST} is used`));
  }
  return new Promise((resolve, reject) => {
    const request = https.get(parsed, (response) => {
      resolve({ statusCode: response.statusCode, headers: response.headers, body: response, destroy: () => response.destroy() });
    });
    // Covers the body too: destroying the request ends the response stream with an error, which the reader sees.
    request.setTimeout(idleMs, () => request.destroy(new Error('the connection went quiet')));
    request.on('error', (err) => reject(new TrayError('network', err.message)));
  });
}

/** Refuses an answer that is not a plain 200 (a 3xx is refused as a redirect, not followed). */
function checkStatus(response: TrayResponse): void {
  const code = response.statusCode ?? 0;
  if (code >= 300 && code < 400) throw new TrayError('redirected', `the server answered ${code} (a redirect)`);
  if (code !== 200) throw new TrayError('http', `the server answered HTTP ${code}`);
}

const asBuffer = (chunk: Buffer | string): Buffer => (Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));

/** Reads, checks and returns the manifest. Throws TrayError for anything that cannot be trusted. */
export async function fetchTrayManifest(open: TrayOpen = httpsOpen): Promise<TrayManifest> {
  let response: TrayResponse | undefined;
  try {
    response = await open(TRAY_MANIFEST_URL);
    checkStatus(response);
    const chunks: Buffer[] = [];
    let total = 0;
    for await (const chunk of response.body) {
      const bytes = asBuffer(chunk);
      total += bytes.length;
      if (total > MAX_MANIFEST_BYTES) throw new TrayError('too-large', 'the download list is too large');
      chunks.push(bytes);
    }
    let raw: unknown;
    try {
      raw = JSON.parse(Buffer.concat(chunks).toString('utf-8'));
    } catch {
      throw new TrayError('refused', 'the download list is not JSON');
    }
    const checked = parseTrayManifest(raw);
    if (!checked.ok) throw new TrayError('refused', checked.reason);
    return checked.manifest;
  } catch (err) {
    throw asTrayError(err);
  } finally {
    response?.destroy?.();
  }
}

function asTrayError(err: unknown): TrayError {
  return err instanceof TrayError ? err : new TrayError('network', (err as Error)?.message ?? String(err));
}

/** Whether `file` is already exactly `build` (its size, then its SHA-256). False for anything missing or unreadable. */
async function sameAsBuild(file: string, build: TrayBuild): Promise<boolean> {
  try {
    const stat = await fs.promises.stat(file);
    if (!stat.isFile() || stat.size !== build.size) return false;
    const hash = crypto.createHash('sha256');
    for await (const chunk of fs.createReadStream(file)) hash.update(chunk as Buffer);
    return hash.digest('hex') === build.sha256;
  } catch {
    return false;
  }
}

export interface SaveOptions {
  platform: TrayPlatform;
  open?: TrayOpen;
  /** Bytes received so far (of build.size). */
  onProgress?: (bytes: number) => void;
  /** Checked between chunks; true stops the download and keeps nothing. */
  isCancelled?: () => boolean;
}

/**
 * Downloads `build` to `target` and keeps it only if it is exactly the published file. The bytes go to `<target>.part`
 * beside it, counted and hashed as they arrive (more than the published size stops the transfer at once). The part
 * file is deleted on any failure (wrong size, wrong SHA-256, a redirect, a network error, a cancel); only a file that
 * matches both is renamed to `target`, so a half or wrong file never has the final name. On Linux it is made
 * executable first (an AppImage is run directly). A `target` that already is this exact build is kept as it is.
 */
export async function saveVerifiedBuild(build: TrayBuild, target: string, options: SaveOptions): Promise<{ path: string; reused: boolean }> {
  const makeRunnable = (file: string) => (options.platform === 'linux' ? fs.promises.chmod(file, 0o755) : Promise.resolve());
  if (await sameAsBuild(target, build)) {
    await makeRunnable(target);
    return { path: target, reused: true };
  }

  const part = `${target}.part`;
  const open = options.open ?? httpsOpen;
  let handle: fs.promises.FileHandle | undefined;
  let response: TrayResponse | undefined;
  try {
    await fs.promises.rm(part, { force: true });
    response = await open(build.url);
    checkStatus(response);
    const announced = response.headers['content-length'];
    if (typeof announced === 'string' && Number(announced) !== build.size) {
      throw new TrayError('size', `the server announced ${announced} bytes, not ${build.size}`);
    }

    handle = await fs.promises.open(part, 'w', 0o600);
    const hash = crypto.createHash('sha256');
    let total = 0;
    for await (const chunk of response.body) {
      if (options.isCancelled?.()) throw new TrayError('cancelled', 'cancelled');
      const bytes = asBuffer(chunk);
      total += bytes.length;
      if (total > build.size) throw new TrayError('size', `more than the published ${build.size} bytes`);
      hash.update(bytes);
      await handle.write(bytes);
      options.onProgress?.(total);
    }
    await handle.close();
    handle = undefined;
    if (total !== build.size) throw new TrayError('size', `${total} bytes, not the published ${build.size}`);
    if (hash.digest('hex') !== build.sha256) throw new TrayError('hash', 'the SHA-256 does not match the published one');

    await makeRunnable(part);
    try {
      await fs.promises.rename(part, target);
    } catch (err) {
      // Windows refuses to replace a program that is running (an older copy of DLNE Donate started from this folder).
      throw new TrayError('save', (err as Error)?.message ?? String(err));
    }
    return { path: target, reused: false };
  } catch (err) {
    await handle?.close().catch(() => {});
    await fs.promises.rm(part, { force: true }).catch(() => {});
    throw asTrayError(err);
  } finally {
    response?.destroy?.();
  }
}

// ---------------------------------------------------------------------------------------------
// The Downloads folder, and running the app
// ---------------------------------------------------------------------------------------------

/**
 * The download folder named in xdg-user-dirs' `user-dirs.dirs` (`XDG_DOWNLOAD_DIR="$HOME/Downloads"`), or null. The
 * format allows only `$HOME/...` or an absolute path, in double quotes.
 */
export function parseXdgDownloadDir(text: string | null, home: string): string | null {
  if (!text) return null;
  const match = /^[ \t]*XDG_DOWNLOAD_DIR[ \t]*=[ \t]*"((?:[^"\\]|\\.)*)"[ \t]*$/m.exec(text);
  if (!match) return null;
  let value = match[1].replace(/\\(.)/g, '$1');
  if (value === '$HOME' || value.startsWith('$HOME/')) value = home + value.slice('$HOME'.length);
  return value.startsWith('/') && !value.includes('$') ? path.posix.normalize(value) : null;
}

/** The slice of the file system downloadsDir looks at, so tests can describe a machine. */
export interface FolderProbe {
  readText(file: string): string | null;
  isDirectory(folder: string): boolean;
}

const realFolders: FolderProbe = {
  readText: (file) => {
    try {
      return fs.readFileSync(file, 'utf-8');
    } catch {
      return null;
    }
  },
  isDirectory: (folder) => {
    try {
      return fs.statSync(folder).isDirectory();
    } catch {
      return false;
    }
  },
};

/**
 * The owner's Downloads folder, when it exists, else their home folder. Windows: `%USERPROFILE%\Downloads` (OneDrive's
 * folder backup moves Desktop, Documents and Pictures, not Downloads). Linux: the xdg-user-dirs folder (its name is
 * translated on many desktops), then `~/Downloads`.
 */
export function downloadsDir(
  platform: string = process.platform,
  home: string = os.homedir(),
  env: NodeJS.ProcessEnv = process.env,
  folders: FolderProbe = realFolders
): string {
  const candidates: string[] = [];
  if (platform === 'win32') {
    candidates.push(path.win32.join(env.USERPROFILE || home, 'Downloads'));
  } else {
    const config = env.XDG_CONFIG_HOME || path.posix.join(home, '.config');
    const xdg = parseXdgDownloadDir(folders.readText(path.posix.join(config, 'user-dirs.dirs')), home);
    if (xdg) candidates.push(xdg);
    candidates.push(path.posix.join(home, 'Downloads'));
  }
  return candidates.find((folder) => folders.isDirectory(folder)) ?? home;
}

/**
 * Starts the saved app on its own, detached from VS Code, so it keeps running (and donating) after VS Code closes.
 * `--settings` opens its window at once, where the owner chooses what to lend and clicks Donate (desktop/dlne_donate
 * __main__.py; when the app is already running, it asks that one to open its window and exits). No pipes are kept (a
 * pipe to a closed editor could end it), and the variables that only make sense inside VS Code's extension host are
 * left out: ELECTRON_RUN_AS_NODE would make any Electron program the app opens run as plain Node.
 * The caller watches 'error' and 'exit' to say when it did not start.
 */
export function launchTrayApp(file: string): child_process.ChildProcess {
  const env: NodeJS.ProcessEnv = {};
  for (const [key, value] of Object.entries(process.env)) {
    if (key !== 'ELECTRON_RUN_AS_NODE' && !key.startsWith('VSCODE_')) env[key] = value;
  }
  const child = child_process.spawn(file, ['--settings'], {
    cwd: path.dirname(file),
    env,
    detached: true,
    stdio: 'ignore',
    // A window app: hiding it would hand Windows' "start hidden" to its first window.
    windowsHide: false,
  });
  child.unref();
  return child;
}

// ---------------------------------------------------------------------------------------------
// Recognising the tray app's worker
// ---------------------------------------------------------------------------------------------

/**
 * The command line of the tray app's worker (desktop/dlne_donate/worker.py, worker_command): the app runs itself as
 * `<...>/DLNE-Donate --run-worker <engine folder> -- worker --coordinator <url>` (the built app; its program is called
 * DLNE-Donate whatever the AppImage file is called), or `python -m dlne_donate --run-worker ...` from a checkout.
 * An extended regular expression for `pgrep -f`. The extension's own pattern, "dlne_server.py worker", never matches
 * it: the engine's script is run inside that process, not named on its command line.
 */
export const TRAY_WORKER_PATTERN = '(DLNE-Donate|dlne_donate) --run-worker .+ -- worker( |$)';

/** The states the app writes to state.json while it donates or is about to (worker.py: PREPARING to STOPPING). */
export const TRAY_ACTIVE_STATES: readonly string[] = ['preparing', 'starting', 'donating', 'stopping'];

/**
 * The tray app's own folder, exactly as desktop/dlne_donate/paths.py app_data_dir works it out: DLNE_DONATE_HOME when
 * set, else `%LOCALAPPDATA%\DLNE Donate` on Windows and `$XDG_DATA_HOME/dlne-donate` (`~/.local/share/dlne-donate`)
 * elsewhere.
 */
export function trayAppDataDir(platform: string, env: NodeJS.ProcessEnv, home: string): string {
  const paths = platform === 'win32' ? path.win32 : path.posix;
  if (env.DLNE_DONATE_HOME) return paths.resolve(env.DLNE_DONATE_HOME);
  if (platform === 'win32') {
    return paths.join(env.LOCALAPPDATA || paths.join(home, 'AppData', 'Local'), 'DLNE Donate');
  }
  return paths.join(env.XDG_DATA_HOME || paths.join(home, '.local', 'share'), 'dlne-donate');
}

/**
 * What the app's state.json says (model.py _write_state: `state`, and `pid`, the app's own process): whether it is
 * donating, and which process said so. Null for anything that is not that.
 */
export function parseTrayState(text: string | null): { active: boolean; pid: number } | null {
  if (!text) return null;
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch {
    return null;
  }
  if (!isObject(raw) || typeof raw.state !== 'string') return null;
  const pid = raw.pid;
  if (typeof pid !== 'number' || !Number.isInteger(pid) || pid <= 0) return null;
  return { active: TRAY_ACTIVE_STATES.includes(raw.state), pid };
}

/** Whether a Linux /proc/<pid>/cmdline (arguments separated by NUL) is the tray app: the DLNE-Donate program, or `-m dlne_donate`. */
export function isTrayAppCommandLine(cmdline: string): boolean {
  const args = cmdline.split('\0').filter((arg) => arg.length > 0);
  if (args.length === 0) return false;
  if (path.posix.basename(args[0]) === 'DLNE-Donate') return true;
  const module = args.indexOf('-m');
  return module !== -1 && args[module + 1] === 'dlne_donate';
}

/** The program name tasklist gives `pid` in its CSV output (`"DLNE-Donate.exe","1234","Console","1","45,000 K"`), or null. */
export function tasklistImage(csv: string, pid: number): string | null {
  for (const line of csv.split(/\r?\n/)) {
    const match = /^"([^"]*)","(\d+)"/.exec(line.trim());
    if (match && Number(match[2]) === pid) return match[1];
  }
  return null;
}

/** A Windows program name that is DLNE Donate's: the download is called DLNE-Donate-<version>.exe (or DLNE-Donate.exe). */
export function isTrayImageName(image: string): boolean {
  return /donate/i.test(image);
}

/** How the checks below look at the machine; the tests hand in their own. */
export interface TrayProbe {
  /** A small text file, or null when it is missing, unreadable or larger than a state file can be. */
  readText(file: string): string | null;
  /** The program's output, or null when it is missing, fails, or (pgrep) finds nothing. */
  run(program: string, args: string[]): string | null;
}

const MAX_STATE_BYTES = 64 * 1024;

export const realTrayProbe: TrayProbe = {
  readText(file) {
    try {
      // /proc files report a size of 0 and are read to their end; a state.json this big is not the app's.
      if (fs.statSync(file).size > MAX_STATE_BYTES) return null;
      return fs.readFileSync(file, 'utf-8');
    } catch {
      return null;
    }
  },
  run(program, args) {
    try {
      // No shell: the pattern reaches pgrep as one argument. windowsHide: tasklist is a console program, and the
      // editor has no console, so without it a window would flash every time the status is checked.
      return child_process.execFileSync(program, args, {
        encoding: 'utf-8',
        timeout: 2000,
        windowsHide: true,
        stdio: ['ignore', 'pipe', 'ignore'],
      });
    } catch {
      return null;
    }
  },
};

/**
 * The app's state.json says it donates, and the process that wrote it is still the app. The second half matters after
 * a crash or a power cut: the file still says "donating", and the process number may since belong to another program.
 * Linux reads that process's command line; Windows asks tasklist for its program name (reading another process's
 * command line on Windows takes PowerShell, too slow for a check every few seconds).
 */
function stateFileSaysDonating(platform: string, probe: TrayProbe, env: NodeJS.ProcessEnv, home: string): boolean {
  const paths = platform === 'win32' ? path.win32 : path.posix;
  const state = parseTrayState(probe.readText(paths.join(trayAppDataDir(platform, env, home), 'state.json')));
  if (!state || !state.active) return false;
  if (platform === 'linux') {
    const cmdline = probe.readText(`/proc/${state.pid}/cmdline`);
    return cmdline !== null && isTrayAppCommandLine(cmdline);
  }
  const listed = probe.run('tasklist', ['/fi', `PID eq ${state.pid}`, '/fo', 'csv', '/nh']);
  const image = listed === null ? null : tasklistImage(listed, state.pid);
  return image !== null && isTrayImageName(image);
}

/**
 * Whether DLNE Donate's worker is running (or the app is about to start it) on this computer. Two ways, either is
 * enough: the app's state.json (above), and on Linux its worker's own command line through pgrep, which also finds
 * an app that keeps its files somewhere else. False on every other system: there is no tray app for them.
 */
export function trayWorkerRunning(
  platform: string = process.platform,
  probe: TrayProbe = realTrayProbe,
  env: NodeJS.ProcessEnv = process.env,
  home: string = os.homedir()
): boolean {
  if (platform !== 'win32' && platform !== 'linux') return false;
  if (stateFileSaysDonating(platform, probe, env, home)) return true;
  return platform === 'linux' && probe.run('pgrep', ['-f', TRAY_WORKER_PATTERN]) !== null;
}
