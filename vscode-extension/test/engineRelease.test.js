'use strict';
/**
 * The extension's check on the engine it is about to run: signature, archive, no downgrades.
 * Includes a two-way check against the Python tooling that actually signs releases, because the
 * whole scheme depends on both languages computing identical bytes.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const {
  EngineVerificationError,
  RELEASE_PUBKEY_HEX,
  canonicalBytes,
  compareVersions,
  fetchVerifiedEngine,
  fetchVerifiedRpcBin,
  hostRpcPlatform,
  RPC_PLATFORMS,
  rpcField,
  verifyArchive,
  verifyManifest,
  verifyRpcBin,
} = require('../dist/worker/engineRelease');
const { makeKeypair, signedManifest, sha256 } = require('./helpers/release');

const REPO_ROOT = path.resolve(__dirname, '..', '..');
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-release-test-'));
const ARCHIVE = path.join(tmp, 'dlne-source.zip');
fs.writeFileSync(ARCHIVE, 'pretend this is the engine archive');
const { privateKey, publicKeyHex } = makeKeypair();
const GOOD = () => signedManifest(ARCHIVE, privateKey);

// --- the canonical bytes ------------------------------------------------------------------

test('canonical bytes are sorted, compact, and leave the signature out', () => {
  const bytes = canonicalBytes({ version: '2.0.0', schema: 1, signature: 'ignored', file: 'a.zip' }).toString();
  assert.equal(bytes, '{"file":"a.zip","schema":1,"version":"2.0.0"}');
});

test('quotes and backslashes are escaped exactly as Python escapes them', () => {
  const bytes = canonicalBytes({ notes: 'He said "hi" \\ path/x' }).toString();
  assert.equal(bytes, '{"notes":"He said \\"hi\\" \\\\ path/x"}');
});

test('text that is not printable ASCII cannot be canonicalised', () => {
  for (const bad of ['café', 'tab\there', 'new\nline', 'del\x7f', '\u{1F680}']) {
    assert.throws(() => canonicalBytes({ notes: bad }), /printable ASCII/);
  }
  assert.throws(() => canonicalBytes({ 'kéy': 'v' }), /printable ASCII/);
});

test('only strings and integers can be signed', () => {
  for (const bad of [1.5, true, null, [1], { a: 1 }, undefined]) {
    assert.throws(() => canonicalBytes({ value: bad }));
  }
});

// --- the manifest -------------------------------------------------------------------------

test('a genuine signed manifest verifies and is returned', () => {
  const m = GOOD();
  const result = verifyManifest(m, publicKeyHex);
  assert.equal(result.ok, true);
  assert.equal(result.manifest.version, '2.0.0');
});

test('changing any signed field breaks the signature', () => {
  for (const [field, value] of [['version', '2.0.1'], ['sha256', '0'.repeat(64)], ['size', 1], ['notes', 'x'],
    ['git_commit', 'deadbee'], ['built_at', '2030-01-01T00:00:00Z']]) {
    const m = { ...GOOD(), [field]: value };
    const result = verifyManifest(m, publicKeyHex);
    assert.equal(result.ok, false, field);
    assert.match(result.reason, /signature/);
  }
});

test('an extra field breaks the signature', () => {
  assert.equal(verifyManifest({ ...GOOD(), smuggled: 'x' }, publicKeyHex).ok, false);
});

test('a signature from another key is rejected', () => {
  assert.equal(verifyManifest(GOOD(), makeKeypair().publicKeyHex).ok, false);
});

test('the real baked-in key rejects a throwaway-signed manifest', () => {
  assert.equal(verifyManifest(GOOD(), RELEASE_PUBKEY_HEX).ok, false);
});

test('an unsigned or oddly signed manifest is rejected, not thrown', () => {
  const unsigned = { ...GOOD() };
  delete unsigned.signature;
  assert.equal(verifyManifest(unsigned, publicKeyHex).ok, false);
  for (const sig of ['', 'zz', 'AB'.repeat(64), 'ab'.repeat(63), 7, null]) {
    assert.equal(verifyManifest({ ...GOOD(), signature: sig }, publicKeyHex).ok, false);
  }
});

test('garbage is a rejection with a reason', () => {
  for (const junk of [null, undefined, 7, 'text', [], {}, { schema: 1 }]) {
    const result = verifyManifest(junk, publicKeyHex);
    assert.equal(result.ok, false);
    assert.ok(result.reason);
  }
});

test('the shape is enforced before the signature is even tried', () => {
  const cases = [
    ['schema', 2, /schema/], ['version', '2.0', /version/], ['version', 'v2.0.0', /version/],
    ['file', 'evil.zip', /unexpected file/], ['sha256', 'ABC', /sha256/], ['size', 0, /size/],
    ['size', -5, /size/], ['size', 1.5, /size/], ['size', true, /size/], ['notes', 'café', /notes/],
  ];
  for (const [field, value, pattern] of cases) {
    const result = verifyManifest({ ...GOOD(), [field]: value }, publicKeyHex);
    assert.equal(result.ok, false, `${field}=${value}`);
    assert.match(result.reason, pattern, `${field}=${value}`);
  }
});

test('a malformed public key is a rejection, not a crash', () => {
  assert.equal(verifyManifest(GOOD(), 'nothex').ok, false);
  assert.equal(verifyManifest(GOOD(), '').ok, false);
});

// --- the archive --------------------------------------------------------------------------

test('the genuine archive matches', () => {
  assert.deepEqual(verifyArchive(GOOD(), ARCHIVE), { ok: true, reason: 'ok' });
});

test('a changed archive of the same size is caught by the hash, a different size by the size', () => {
  const m = GOOD();
  const same = path.join(tmp, 'same-size.zip');
  fs.writeFileSync(same, 'PRETEND this is the engine archive');
  assert.equal(fs.statSync(same).size, m.size);
  assert.match(verifyArchive(m, same).reason, /hash/);
  const longer = path.join(tmp, 'longer.zip');
  fs.writeFileSync(longer, 'pretend this is the engine archive!');
  assert.match(verifyArchive(m, longer).reason, /size/);
});

test('an unreadable archive is a rejection', () => {
  assert.equal(verifyArchive(GOOD(), path.join(tmp, 'missing.zip')).ok, false);
});

// --- versions -----------------------------------------------------------------------------

test('versions compare numerically', () => {
  assert.equal(compareVersions('1.1.0', '1.1.12'), -1);
  assert.equal(compareVersions('1.10.0', '1.9.0'), 1);
  assert.equal(compareVersions('2.0.0', '2.0.0'), 0);
  assert.equal(compareVersions('banana', '1.0.0'), null);
  assert.equal(compareVersions('1.0', '1.0.0'), null);
});

// --- fetchVerifiedEngine ------------------------------------------------------------------

function harness({ manifest = GOOD(), bytes = fs.readFileSync(ARCHIVE), fetchError = null, downloadError = null } = {}) {
  const calls = { fetched: [], downloaded: [] };
  const destination = path.join(tmp, `download-${Math.random().toString(16).slice(2)}.zip`);
  return {
    calls,
    destination,
    options: (extra = {}) => ({
      publicKeyHex,
      allowUnsigned: false,
      installed: null,
      fetchJson: async (url) => {
        calls.fetched.push(url);
        if (fetchError) throw fetchError;
        return manifest;
      },
      download: async (url, dest) => {
        calls.downloaded.push(url);
        if (downloadError) throw downloadError;
        fs.writeFileSync(dest, bytes);
      },
      ...extra,
    }),
  };
}

test('a verified release is downloaded and left for extraction', async () => {
  const h = harness();
  const result = await fetchVerifiedEngine('http://coordinator:11435/', h.destination, h.options());
  assert.equal(result.status, 'installed');
  assert.equal(result.signed, true);
  assert.equal(result.manifest.version, '2.0.0');
  assert.deepEqual(h.calls.fetched, ['http://coordinator:11435/v1/release']);
  assert.deepEqual(h.calls.downloaded, ['http://coordinator:11435/v1/download/source']);
  assert.ok(fs.existsSync(h.destination));
});

test('a coordinator with no signed release is refused by default, and nothing is downloaded', async () => {
  const h = harness({ fetchError: Object.assign(new Error('HTTP 404'), { statusCode: 404 }) });
  await assert.rejects(fetchVerifiedEngine('http://c', h.destination, h.options()), (err) => {
    assert.ok(err instanceof EngineVerificationError);
    assert.match(err.userMessage, /does not publish a signed release/);
    assert.match(err.userMessage, /allowUnsignedEngine/);
    return true;
  });
  assert.deepEqual(h.calls.downloaded, []);
});

test('with the opt-in, a private coordinator with no signing can be used', async () => {
  const h = harness({ fetchError: Object.assign(new Error('HTTP 404'), { statusCode: 404 }) });
  const result = await fetchVerifiedEngine('http://c', h.destination, h.options({ allowUnsigned: true }));
  assert.deepEqual([result.status, result.signed, result.manifest], ['installed', false, null]);
  assert.equal(h.calls.downloaded.length, 1);
});

test('the opt-in never excuses a signature that is present but wrong', async () => {
  const forged = { ...GOOD(), version: '9.9.9' };
  const h = harness({ manifest: forged });
  await assert.rejects(fetchVerifiedEngine('http://c', h.destination, h.options({ allowUnsigned: true })), EngineVerificationError);
  assert.deepEqual(h.calls.downloaded, []);
});

test('a network failure fetching the manifest is a plain error, not a verification verdict', async () => {
  const h = harness({ fetchError: new Error('connect ECONNREFUSED') });
  await assert.rejects(fetchVerifiedEngine('http://c', h.destination, h.options()), (err) => {
    assert.ok(!(err instanceof EngineVerificationError));
    assert.match(err.message, /ECONNREFUSED/);
    return true;
  });
  assert.deepEqual(h.calls.downloaded, []);
});

test('a 500 or a timeout is not mistaken for "no signed release"', async () => {
  const h = harness({ fetchError: Object.assign(new Error('HTTP 500'), { statusCode: 500 }) });
  await assert.rejects(fetchVerifiedEngine('http://c', h.destination, h.options({ allowUnsigned: true })), (err) => !(err instanceof EngineVerificationError));
  assert.deepEqual(h.calls.downloaded, []);
});

test('a manifest signed by the wrong key is refused before any download', async () => {
  const h = harness();
  await assert.rejects(fetchVerifiedEngine('http://c', h.destination, h.options({ publicKeyHex: makeKeypair().publicKeyHex })), (err) => {
    assert.ok(err instanceof EngineVerificationError);
    assert.match(err.userMessage, /failed verification/);
    assert.match(err.userMessage, /NOT installed/);
    return true;
  });
  assert.deepEqual(h.calls.downloaded, []);
});

test('an archive that does not match the signed hash is deleted and refused', async () => {
  const h = harness({ bytes: Buffer.from('a different archive entirely, same signature') });
  await assert.rejects(fetchVerifiedEngine('http://c', h.destination, h.options()), (err) => {
    assert.ok(err instanceof EngineVerificationError);
    assert.match(err.userMessage, /does not match the signed release/);
    return true;
  });
  assert.equal(fs.existsSync(h.destination), false, 'the bad archive must not be left on disk');
});

test('a download that fails leaves nothing behind', async () => {
  const h = harness({ downloadError: new Error('socket hang up') });
  fs.writeFileSync(h.destination, 'partial');
  await assert.rejects(fetchVerifiedEngine('http://c', h.destination, h.options()), /socket hang up/);
  assert.equal(fs.existsSync(h.destination), false);
});

test('the installed release is not downloaded again', async () => {
  const m = GOOD();
  const h = harness({ manifest: m });
  const result = await fetchVerifiedEngine('http://c', h.destination, h.options({ installed: { version: '2.0.0', sha256: m.sha256 } }));
  assert.equal(result.status, 'up-to-date');
  assert.deepEqual(h.calls.downloaded, []);
});

test('a release older than the installed one is refused (no downgrade replay)', async () => {
  const older = signedManifest(ARCHIVE, privateKey, { version: '1.9.0' });
  const h = harness({ manifest: older });
  await assert.rejects(fetchVerifiedEngine('http://c', h.destination, h.options({ installed: { version: '2.0.0', sha256: 'f'.repeat(64) } })), (err) => {
    assert.ok(err instanceof EngineVerificationError);
    assert.match(err.userMessage, /older than the 2\.0\.0 already installed/);
    return true;
  });
  assert.deepEqual(h.calls.downloaded, []);
});

test('a newer release replaces an older installed one', async () => {
  const h = harness();
  const result = await fetchVerifiedEngine('http://c', h.destination, h.options({ installed: { version: '1.9.0', sha256: 'e'.repeat(64) } }));
  assert.equal(result.status, 'installed');
});

test('the same version with different bytes is installed (a repaired release)', async () => {
  const h = harness();
  const result = await fetchVerifiedEngine('http://c', h.destination, h.options({ installed: { version: '2.0.0', sha256: 'd'.repeat(64) } }));
  assert.equal(result.status, 'installed');
});

// --- Python <-> TypeScript ----------------------------------------------------------------

function findPython() {
  const candidates = [process.env.DLNE_TEST_PYTHON, 'python3', 'python'].filter(Boolean);
  for (const py of candidates) {
    const probe = spawnSync(py, ['-c', 'import cryptography'], { cwd: REPO_ROOT, encoding: 'utf8' });
    if (probe.status === 0) return py;
  }
  return null;
}
const PYTHON = findPython();
const needsPython = PYTHON ? {} : { skip: 'no Python with the cryptography package (set DLNE_TEST_PYTHON)' };

test('a manifest signed by the real Python release tooling verifies here', needsPython, () => {
  const script = `
import json, sys, tempfile, os
sys.path.insert(0, ${JSON.stringify(REPO_ROOT)})
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from engine import release
key = Ed25519PrivateKey.generate()
pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
archive = os.path.join(tempfile.mkdtemp(), "dlne-source.zip")
open(archive, "wb").write(b"engine bytes")
notes = 'He said "hi" \\\\ and used /slashes/ and ~tildes~ {braces} [brackets]'
m = release.sign_manifest(release.make_manifest("2.0.0", archive, "abc1234", "2026-10-25T12:00:00Z", notes), pem)
print(json.dumps({"manifest": m, "pubkey": release.public_key_hex(pem), "canonical": release.canonical_bytes(m).decode()}))
`;
  const run = spawnSync(PYTHON, ['-c', script], { cwd: REPO_ROOT, encoding: 'utf8' });
  assert.equal(run.status, 0, run.stderr);
  const { manifest, pubkey, canonical } = JSON.parse(run.stdout);
  assert.equal(canonicalBytes(manifest).toString(), canonical, 'both languages must produce identical canonical bytes');
  assert.equal(verifyManifest(manifest, pubkey).ok, true, 'the Python signature must verify in TypeScript');
  assert.equal(verifyManifest({ ...manifest, version: '2.0.1' }, pubkey).ok, false);
});

test('a manifest signed here verifies in the Python tooling', needsPython, () => {
  const m = signedManifest(ARCHIVE, privateKey, { notes: 'a "quoted" \\ note' });
  const script = `
import json, sys
sys.path.insert(0, ${JSON.stringify(REPO_ROOT)})
from engine import release
data = json.load(sys.stdin)
print(json.dumps(release.verify_manifest(data["manifest"], data["pubkey"])))
`;
  const run = spawnSync(PYTHON, ['-c', script], { cwd: REPO_ROOT, encoding: 'utf8', input: JSON.stringify({ manifest: m, pubkey: publicKeyHex }) });
  assert.equal(run.status, 0, run.stderr);
  assert.deepEqual(JSON.parse(run.stdout), [true, 'ok']);
});

test('the key baked into the extension is the key the Python engine trusts', needsPython, () => {
  const run = spawnSync(PYTHON, ['-c', `import sys; sys.path.insert(0, ${JSON.stringify(REPO_ROOT)}); from engine import release; print(release.RELEASE_PUBKEY)`], { cwd: REPO_ROOT, encoding: 'utf8' });
  assert.equal(run.status, 0, run.stderr);
  assert.equal(run.stdout.trim(), RELEASE_PUBKEY_HEX);
});

// --- the llama.cpp RPC binaries -----------------------------------------------------------

const RPC = path.join(tmp, 'rpc-bin-linux-x64.zip');
fs.writeFileSync(RPC, 'pretend these are the rpc binaries');
const WITH_RPC = () => signedManifest(ARCHIVE, privateKey, {
  llama_build: 'b11838', rpc_proto: '8.0.1',
  rpc_linux_x64_sha256: sha256(fs.readFileSync(RPC)), rpc_linux_x64_size: fs.statSync(RPC).size,
});

test('rpcField and hostRpcPlatform name platforms exactly as the Python side does', () => {
  assert.equal(rpcField('macos-arm64', 'sha256'), 'rpc_macos_arm64_sha256');
  assert.equal(hostRpcPlatform('win32', 'x64'), 'win-x64');
  assert.equal(hostRpcPlatform('linux', 'x64'), 'linux-x64');
  assert.equal(hostRpcPlatform('darwin', 'arm64'), 'macos-arm64');
  // Added with CPU-only contribution (coordinator 2.1.3).
  assert.equal(hostRpcPlatform('darwin', 'x64'), 'macos-x64');
  assert.equal(hostRpcPlatform('linux', 'arm64'), 'linux-arm64');
  assert.equal(hostRpcPlatform('win32', 'arm64'), 'win-arm64');
  for (const [p, a] of [['freebsd', 'x64'], ['linux', 'ia32'], ['linux', 's390x'], ['android', 'arm64']]) {
    assert.equal(hostRpcPlatform(p, a), null, `${p}/${a}`);
  }
});

test('the extension ships for exactly the platforms the Python release signs', () => {
  const fs = require('node:fs');
  const path = require('node:path');
  const py = fs.readFileSync(path.join(__dirname, '..', '..', 'engine', 'release.py'), 'utf8');
  const pyList = /^RPC_PLATFORMS = \(([^)]*)\)/m.exec(py)[1].match(/"([^"]+)"/g).map((s) => s.slice(1, -1));
  assert.deepEqual([...RPC_PLATFORMS], pyList);
});

test('a manifest that signs RPC binaries verifies, and changing them breaks the signature', () => {
  assert.equal(verifyManifest(WITH_RPC(), publicKeyHex).ok, true);
  for (const [field, value] of [['rpc_linux_x64_sha256', '1'.repeat(64)], ['rpc_linux_x64_size', 3],
    ['llama_build', 'b1'], ['rpc_proto', '9.0.0']]) {
    const result = verifyManifest({ ...WITH_RPC(), [field]: value }, publicKeyHex);
    assert.equal(result.ok, false, field);
    assert.match(result.reason, /signature/);
  }
});

test('malformed or half-present binary fields are rejected by shape', () => {
  for (const [field, value, pattern] of [['rpc_linux_x64_sha256', 'XYZ', /rpc_linux_x64_sha256/],
    ['rpc_linux_x64_size', 0, /rpc_linux_x64_size/], ['rpc_win_x64_size', '5', /rpc_win_x64_sha256/],
    ['server_llama_size', 1.5, /server_llama_sha256/], ['llama_build', 'latest', /llama_build/],
    ['rpc_proto', '8', /rpc_proto/]]) {
    const result = verifyManifest({ ...WITH_RPC(), [field]: value }, publicKeyHex);
    assert.equal(result.ok, false, field);
    assert.match(result.reason, pattern, field);
  }
  const half = WITH_RPC();
  delete half.rpc_linux_x64_size;
  assert.match(verifyManifest(half, publicKeyHex).reason, /rpc_linux_x64_size/);
});

test('verifyRpcBin checks the platform, size and hash', () => {
  assert.deepEqual(verifyRpcBin(WITH_RPC(), 'linux-x64', RPC), { ok: true, reason: 'ok' });
  const other = path.join(tmp, 'other-rpc.zip');
  fs.writeFileSync(other, 'PRETEND these are the rpc binaries');
  assert.match(verifyRpcBin(WITH_RPC(), 'linux-x64', other).reason, /hash/);
  assert.match(verifyRpcBin(WITH_RPC(), 'win-x64', RPC).reason, /does not sign GPU engine binaries for win-x64/);
  assert.match(verifyRpcBin(GOOD(), 'linux-x64', RPC).reason, /does not sign/);
});

function rpcHarness(manifest, bytes = fs.readFileSync(RPC)) {
  const downloaded = [];
  const destination = path.join(tmp, `rpc-${Math.random().toString(16).slice(2)}.zip`);
  return {
    downloaded,
    destination,
    options: (extra = {}) => ({
      publicKeyHex, allowUnsigned: false,
      fetchJson: async () => manifest,
      download: async (url, dest) => { downloaded.push(url); fs.writeFileSync(dest, bytes); },
      ...extra,
    }),
  };
}

test('fetchVerifiedRpcBin downloads the signed zip for the platform and reports its hash', async () => {
  const m = WITH_RPC();
  const h = rpcHarness(m);
  const result = await fetchVerifiedRpcBin('http://c/', h.destination, 'linux-x64', h.options());
  assert.deepEqual(result, { status: 'installed', sha256: m.rpc_linux_x64_sha256 });
  assert.deepEqual(h.downloaded, ['http://c/v1/download/rpc-bin/linux-x64']);
});

test('fetchVerifiedRpcBin skips the download when the installed zip is the signed one', async () => {
  const m = WITH_RPC();
  const h = rpcHarness(m);
  const result = await fetchVerifiedRpcBin('http://c', h.destination, 'linux-x64', h.options({ installedSha256: m.rpc_linux_x64_sha256 }));
  assert.equal(result.status, 'up-to-date');
  assert.deepEqual(h.downloaded, []);
});

test('fetchVerifiedRpcBin deletes and refuses a zip that does not match', async () => {
  const h = rpcHarness(WITH_RPC(), Buffer.from('something else entirely'));
  await assert.rejects(fetchVerifiedRpcBin('http://c', h.destination, 'linux-x64', h.options()), EngineVerificationError);
  assert.equal(fs.existsSync(h.destination), false);
});

test('fetchVerifiedRpcBin refuses a release without this platform before downloading', async () => {
  for (const [m, platform] of [[GOOD(), 'linux-x64'], [WITH_RPC(), 'macos-arm64']]) {
    const h = rpcHarness(m);
    await assert.rejects(fetchVerifiedRpcBin('http://c', h.destination, platform, h.options()),
      new RegExp(`does not sign GPU engine binaries for ${platform}`));
    assert.deepEqual(h.downloaded, []);
  }
});

test('fetchVerifiedRpcBin follows the unsigned opt-in exactly like the engine', async () => {
  const notFound = Object.assign(new Error('HTTP 404'), { statusCode: 404 });
  const h = rpcHarness(null);
  const opts = (extra) => h.options({ fetchJson: async () => { throw notFound; }, ...extra });
  await assert.rejects(fetchVerifiedRpcBin('http://c', h.destination, 'win-x64', opts({})), /does not publish a signed release/);
  assert.deepEqual(await fetchVerifiedRpcBin('http://c', h.destination, 'win-x64', opts({ allowUnsigned: true })), { status: 'installed', sha256: null });
});

test('RPC fields signed by the Python tooling verify here', needsPython, () => {
  const script = `
import json, sys, tempfile, os, zipfile
sys.path.insert(0, ${JSON.stringify(REPO_ROOT)})
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from engine import release
key = Ed25519PrivateKey.generate()
pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
d = tempfile.mkdtemp()
archive = os.path.join(d, "dlne-source.zip"); open(archive, "wb").write(b"engine")
zips = {}
for p in release.RPC_PLATFORMS:
    zips[p] = os.path.join(d, release.rpc_zip_name(p))
    with zipfile.ZipFile(zips[p], "w") as z: z.writestr("bin/ggml-rpc-server", p.encode())
srv = os.path.join(d, "server.tar.gz"); open(srv, "wb").write(b"server")
m = release.sign_manifest(release.make_manifest("2.0.0", archive, "abc1234", "2026-10-25T12:00:00Z", "n",
    rpc_zips=zips, llama_build="b11838", rpc_proto="8.0.1", server_llama=srv), pem)
print(json.dumps({"manifest": m, "pubkey": release.public_key_hex(pem), "canonical": release.canonical_bytes(m).decode()}))
`;
  const run = spawnSync(PYTHON, ['-c', script], { cwd: REPO_ROOT, encoding: 'utf8' });
  assert.equal(run.status, 0, run.stderr);
  const { manifest, pubkey, canonical } = JSON.parse(run.stdout);
  for (const platform of RPC_PLATFORMS) assert.ok(manifest[rpcField(platform, 'sha256')], platform);
  assert.ok(manifest.server_llama_sha256 && manifest.llama_build === 'b11838');
  assert.equal(canonicalBytes(manifest).toString(), canonical);
  assert.equal(verifyManifest(manifest, pubkey).ok, true);
});

test.after(() => fs.rmSync(tmp, { recursive: true, force: true }));
