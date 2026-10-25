import * as crypto from 'crypto';
import * as fs from 'fs';

/**
 * Verifying the engine before running it.
 *
 * The coordinator serves the Python engine that this extension then runs on the user's machine,
 * so "the coordinator said so" cannot be the whole story. Each release carries a manifest
 * (version, SHA-256, size) signed with the maintainer's Ed25519 key, whose public half is
 * baked into this extension. Nothing is extracted unless:
 *
 *   1. the manifest's signature verifies against RELEASE_PUBKEY_HEX,
 *   2. the downloaded archive has exactly the signed size and SHA-256, and
 *   3. the release is not older than the engine already installed (versions only go up, so a
 *      genuine but outdated signed release cannot be replayed to downgrade a node).
 *
 * The signed text is restricted to printable ASCII on purpose: Python's json.dumps and
 * JavaScript's JSON.stringify only agree byte for byte there, and the manifest is signed in
 * Python (engine/release.py) and verified here.
 */

/** The maintainer's release public key (Ed25519, hex). Rotating it means shipping a new extension. */
export const RELEASE_PUBKEY_HEX = 'af3a6d4203ab98e0149603a4d6322360723bd6fb357079ce1fcdafaee084fa6a';

const MANIFEST_SCHEMA = 1;
const ARCHIVE_NAME = 'dlne-source.zip';
const SEMVER = /^\d+\.\d+\.\d+$/;
const SHA256 = /^[0-9a-f]{64}$/;

// DER prefix that turns a raw 32-byte Ed25519 public key into the SubjectPublicKeyInfo Node expects.
const ED25519_SPKI_PREFIX = Buffer.from('302a300506032b6570032100', 'hex');

export interface ReleaseManifest {
  schema: number;
  version: string;
  file: string;
  sha256: string;
  size: number;
  git_commit: string;
  built_at: string;
  notes: string;
  /** The pinned llama.cpp build the release's binaries come from, and its RPC protocol. */
  llama_build?: string;
  rpc_proto?: string;
  /** rpc_<platform>_sha256 / rpc_<platform>_size for each RPC zip, server_llama_* for the coordinator. */
  [binaryField: string]: unknown;
  signature: string;
}

/** The platforms DLNE ships ggml-rpc-server for, as named in the release (engine/release.py). */
export const RPC_PLATFORMS = ['win-x64', 'linux-x64', 'macos-arm64'] as const;
export type RpcPlatform = (typeof RPC_PLATFORMS)[number];

export function rpcField(platform: string, what: 'sha256' | 'size'): string {
  return `rpc_${platform.replace(/-/g, '_')}_${what}`;
}

/** This machine's RPC platform, or null when DLNE ships no ggml-rpc-server for it. */
export function hostRpcPlatform(platform: string = process.platform, arch: string = process.arch): RpcPlatform | null {
  if (platform === 'win32' && arch === 'x64') return 'win-x64';
  if (platform === 'linux' && arch === 'x64') return 'linux-x64';
  if (platform === 'darwin' && arch === 'arm64') return 'macos-arm64';
  return null;
}

/** The release could not be trusted. `userMessage` is written to be shown as-is. */
export class EngineVerificationError extends Error {
  constructor(public readonly userMessage: string) {
    super(userMessage);
    this.name = 'EngineVerificationError';
  }
}

function printableAscii(value: string): boolean {
  for (let i = 0; i < value.length; i++) {
    const code = value.charCodeAt(i);
    if (code < 0x20 || code > 0x7e) return false;
  }
  return true;
}

/**
 * The exact bytes the signature covers: every field except `signature`, keys sorted, no
 * whitespace. Identical to Python's json.dumps(sort_keys=True, separators=(",", ":")) for the
 * printable-ASCII text a manifest is allowed to contain.
 */
export function canonicalBytes(manifest: Record<string, unknown>): Buffer {
  const parts: string[] = [];
  for (const key of Object.keys(manifest).filter((k) => k !== 'signature').sort()) {
    const value = manifest[key];
    if (typeof value === 'string') {
      if (!printableAscii(key) || !printableAscii(value)) {
        throw new Error('manifest text must be printable ASCII');
      }
    } else if (typeof value !== 'number' || !Number.isInteger(value)) {
      throw new Error('manifest values must be strings or integers');
    }
    parts.push(`${JSON.stringify(key)}:${JSON.stringify(value)}`);
  }
  return Buffer.from(`{${parts.join(',')}}`, 'utf-8');
}

/** Shape + signature. Total: anything wrong is a rejection with a reason, never a throw. */
export function verifyManifest(
  manifest: unknown,
  publicKeyHex: string
): { ok: true; manifest: ReleaseManifest } | { ok: false; reason: string } {
  if (typeof manifest !== 'object' || manifest === null || Array.isArray(manifest)) {
    return { ok: false, reason: 'the release manifest is not a JSON object' };
  }
  const m = manifest as Record<string, unknown>;
  if (m.schema !== MANIFEST_SCHEMA) return { ok: false, reason: `unsupported manifest schema ${String(m.schema)}` };
  if (typeof m.version !== 'string' || !SEMVER.test(m.version)) return { ok: false, reason: 'the manifest has no valid version' };
  if (m.file !== ARCHIVE_NAME) return { ok: false, reason: 'the manifest names an unexpected file' };
  if (typeof m.sha256 !== 'string' || !SHA256.test(m.sha256)) return { ok: false, reason: 'the manifest has no valid sha256' };
  if (typeof m.size !== 'number' || !Number.isInteger(m.size) || m.size <= 0) return { ok: false, reason: 'the manifest has no valid size' };
  for (const field of ['git_commit', 'built_at', 'notes']) {
    if (typeof m[field] !== 'string' || !printableAscii(m[field] as string)) {
      return { ok: false, reason: `the manifest field '${field}' is missing or not printable ASCII` };
    }
  }
  const pairs: Array<[string, string]> = RPC_PLATFORMS.map((p) => [rpcField(p, 'sha256'), rpcField(p, 'size')]);
  pairs.push(['server_llama_sha256', 'server_llama_size']);
  let hasBinaries = false;
  for (const [shaKey, sizeKey] of pairs) {
    if (shaKey in m || sizeKey in m) {
      hasBinaries = true;
      const sha = m[shaKey];
      const size = m[sizeKey];
      if (typeof sha !== 'string' || !SHA256.test(sha)) return { ok: false, reason: `the manifest has no valid ${shaKey}` };
      if (typeof size !== 'number' || !Number.isInteger(size) || size <= 0) return { ok: false, reason: `the manifest has no valid ${sizeKey}` };
    }
  }
  if (hasBinaries || 'llama_build' in m || 'rpc_proto' in m) {
    if (typeof m.llama_build !== 'string' || !/^b\d+$/.test(m.llama_build)) return { ok: false, reason: 'the manifest has no valid llama_build' };
    if (typeof m.rpc_proto !== 'string' || !SEMVER.test(m.rpc_proto)) return { ok: false, reason: 'the manifest has no valid rpc_proto' };
  }
  if (typeof m.signature !== 'string' || !/^[0-9a-f]{128}$/.test(m.signature)) {
    return { ok: false, reason: 'the release manifest is not signed' };
  }
  try {
    const key = crypto.createPublicKey({
      key: Buffer.concat([ED25519_SPKI_PREFIX, Buffer.from(publicKeyHex, 'hex')]),
      format: 'der',
      type: 'spki',
    });
    const valid = crypto.verify(null, canonicalBytes(m), key, Buffer.from(m.signature, 'hex'));
    if (!valid) return { ok: false, reason: 'the release signature does not verify' };
  } catch {
    return { ok: false, reason: 'the release signature could not be checked' };
  }
  return { ok: true, manifest: m as unknown as ReleaseManifest };
}

function verifyFile(filePath: string, size: number | undefined, sha256: string | undefined, what: string): { ok: boolean; reason: string } {
  let data: Buffer;
  try {
    data = fs.readFileSync(filePath);
  } catch {
    return { ok: false, reason: `the downloaded ${what} could not be read` };
  }
  if (data.length !== size) return { ok: false, reason: `the downloaded ${what} size does not match the signed release` };
  const digest = crypto.createHash('sha256').update(data).digest('hex');
  if (digest !== sha256) return { ok: false, reason: `the downloaded ${what} hash does not match the signed release` };
  return { ok: true, reason: 'ok' };
}

/** The archive on disk is byte-for-byte the one the manifest signed. */
export function verifyArchive(manifest: ReleaseManifest, archivePath: string): { ok: boolean; reason: string } {
  return verifyFile(archivePath, manifest.size, manifest.sha256, 'archive');
}

/** The RPC zip on disk is byte-for-byte the one the manifest signed for `platform`. */
export function verifyRpcBin(manifest: ReleaseManifest, platform: string, zipPath: string): { ok: boolean; reason: string } {
  const sha = manifest[rpcField(platform, 'sha256')];
  if (typeof sha !== 'string') return { ok: false, reason: `the release does not sign GPU engine binaries for ${platform}` };
  return verifyFile(zipPath, manifest[rpcField(platform, 'size')] as number, sha, 'GPU engine');
}

/** -1, 0 or 1 comparing two x.y.z versions numerically; null if either is not a version. */
export function compareVersions(a: string, b: string): number | null {
  if (!SEMVER.test(a) || !SEMVER.test(b)) return null;
  const pa = a.split('.').map(Number);
  const pb = b.split('.').map(Number);
  for (let i = 0; i < 3; i++) {
    if (pa[i] !== pb[i]) return pa[i] < pb[i] ? -1 : 1;
  }
  return 0;
}

export interface FetchOptions {
  /** Trust anchor. Defaults to the baked-in key; tests pass their own. */
  publicKeyHex: string;
  /** Private coordinators with no signing: install without verification (off by default). */
  allowUnsigned: boolean;
  /** The release currently installed, if any, from the file written after its install. */
  installed?: { version: string; sha256: string } | null;
  fetchJson: (url: string) => Promise<unknown>;
  download: (url: string, destination: string) => Promise<void>;
}

export type FetchResult =
  | { status: 'installed'; manifest: ReleaseManifest | null; signed: boolean }
  | { status: 'up-to-date'; manifest: ReleaseManifest };

/**
 * The coordinator's verified release manifest, or null when it publishes none and unsigned
 * installs were explicitly allowed. Throws EngineVerificationError for anything untrustworthy.
 */
async function fetchSignedManifest(base: string, options: FetchOptions): Promise<ReleaseManifest | null> {
  let raw: unknown;
  try {
    raw = await options.fetchJson(`${base}/v1/release`);
  } catch (err: any) {
    // A 404 means "this coordinator publishes no signed release". Anything else (network down,
    // junk) is not evidence about signing, so it is reported as a plain failure.
    if (!(err && err.statusCode === 404)) {
      throw new Error(`Could not reach the coordinator's release manifest: ${err?.message ?? err}`);
    }
    if (!options.allowUnsigned) {
      throw new EngineVerificationError(
        'This coordinator does not publish a signed release, so DLNE will not install its engine. ' +
          "If you run your own private coordinator, turn on the setting 'dlne.worker.allowUnsignedEngine'."
      );
    }
    return null;
  }

  const checked = verifyManifest(raw, options.publicKeyHex);
  if (!checked.ok) {
    throw new EngineVerificationError(
      `The engine release from ${base} failed verification (${checked.reason}) and was NOT installed.`
    );
  }
  return checked.manifest;
}

/**
 * Download the coordinator's engine archive to `destination` only if it is a verified release.
 * Resolves with the manifest (or null when an unsigned install was explicitly allowed).
 * Throws EngineVerificationError for anything untrustworthy, leaving no archive behind.
 */
export async function fetchVerifiedEngine(serverUrl: string, destination: string, options: FetchOptions): Promise<FetchResult> {
  const base = serverUrl.replace(/\/+$/, '');
  const remove = () => {
    try { fs.unlinkSync(destination); } catch { /* nothing to remove */ }
  };

  const manifest = await fetchSignedManifest(base, options);
  if (manifest === null) {
    await options.download(`${base}/v1/download/source`, destination);
    return { status: 'installed', manifest: null, signed: false };
  }


  const installed = options.installed;
  if (installed) {
    const order = compareVersions(manifest.version, installed.version);
    if (order !== null && order < 0) {
      throw new EngineVerificationError(
        `The coordinator offers engine ${manifest.version}, which is older than the ${installed.version} already installed. ` +
          'Releases only move forward, so it was NOT installed.'
      );
    }
    if (installed.sha256 === manifest.sha256) return { status: 'up-to-date', manifest };
  }

  try {
    await options.download(`${base}/v1/download/source`, destination);
  } catch (err) {
    remove();
    throw err;
  }
  const archive = verifyArchive(manifest, destination);
  if (!archive.ok) {
    remove();
    throw new EngineVerificationError(`${capitalize(archive.reason)}; it was NOT installed.`);
  }
  return { status: 'installed', manifest, signed: true };
}

function capitalize(text: string): string {
  return `${text.charAt(0).toUpperCase()}${text.slice(1)}`;
}

export type RpcBinResult =
  | { status: 'installed'; sha256: string | null }
  | { status: 'up-to-date'; sha256: string };

/**
 * Download the coordinator's Windows ggml-rpc-server zip to `destination`, only if the signed
 * release names it (rpc_bin_sha256 / rpc_bin_size) and the bytes match. These are executables
 * the worker runs, so they get exactly the engine's guarantees. `installedSha256` is the zip
 * installed last time; when the release still names it, nothing is downloaded.
 */
export async function fetchVerifiedRpcBin(
  serverUrl: string,
  destination: string,
  platform: RpcPlatform,
  options: FetchOptions & { installedSha256?: string | null }
): Promise<RpcBinResult> {
  const base = serverUrl.replace(/\/+$/, '');
  const url = `${base}/v1/download/rpc-bin/${platform}`;
  const remove = () => {
    try { fs.unlinkSync(destination); } catch { /* nothing to remove */ }
  };

  const manifest = await fetchSignedManifest(base, options);
  if (manifest === null) {
    await options.download(url, destination);
    return { status: 'installed', sha256: null };
  }
  const sha = manifest[rpcField(platform, 'sha256')];
  if (typeof sha !== 'string') {
    throw new EngineVerificationError(
      `The release from ${base} does not sign GPU engine binaries for ${platform}, so they were NOT installed.`
    );
  }
  if (options.installedSha256 === sha) {
    return { status: 'up-to-date', sha256: sha };
  }

  try {
    await options.download(url, destination);
  } catch (err) {
    remove();
    throw err;
  }
  const checked = verifyRpcBin(manifest, platform, destination);
  if (!checked.ok) {
    remove();
    throw new EngineVerificationError(`${capitalize(checked.reason)}; it was NOT installed.`);
  }
  return { status: 'installed', sha256: sha };
}
