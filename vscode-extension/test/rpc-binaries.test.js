'use strict';
/**
 * The GPU engine (bin/ggml-rpc-server) a worker runs on Windows, Linux and macOS. It comes from
 * the coordinator's /v1/download/rpc-bin/<platform>, so it gets the engine's guarantees: only a
 * zip the signed release names for this platform is ever unpacked, a failed check keeps what is
 * installed, and a new signed zip (a llama.cpp upgrade) replaces the old one.
 *
 * The tests run on Linux, so process.platform/arch are set per case, and archive extraction is
 * done with the real unzip (PowerShell's Expand-Archive does not exist here).
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

const stub = require('./helpers/vscode-stub');
const { createSandbox, which } = require('./helpers/sandbox');
const { WorkerManager } = require('../dist/worker/workerManager');

const UNZIP = which('unzip');
const needsUnzip = UNZIP ? {} : { skip: 'unzip is not installed' };

const PLATFORMS = [
  { key: 'win-x64', platform: 'win32', arch: 'x64', exe: 'ggml-rpc-server.exe' },
  { key: 'linux-x64', platform: 'linux', arch: 'x64', exe: 'ggml-rpc-server' },
  { key: 'macos-arm64', platform: 'darwin', arch: 'arm64', exe: 'ggml-rpc-server' },
];

const record = (sb) => path.join(sb.runtimeDir, '.dlne_rpc_bin.json');
const warnings = () => stub.state.warningMessages.map((w) => w.message ?? w).join('\n');
const rpcDownloads = (sb) => sb.release.hits.filter((u) => u.startsWith('/v1/download/rpc-bin')).length;

async function on(p, body) {
  stub.reset();
  const sb = await createSandbox({});
  stub.state.settings['dlne.serverUrl'] = sb.serverUrl;
  const wm = new WorkerManager({ subscriptions: [] }, { releasePublicKeyHex: sb.releasePublicKeyHex });
  wm.extractArchive = async (zip, dest) => {
    execFileSync(UNZIP, ['-q', '-o', zip, '-d', dest]);
    return { ok: true, tail: '' };
  };
  fs.mkdirSync(sb.runtimeDir, { recursive: true });
  const saved = { platform: Object.getOwnPropertyDescriptor(process, 'platform'), arch: Object.getOwnPropertyDescriptor(process, 'arch') };
  Object.defineProperty(process, 'platform', { value: p.platform });
  Object.defineProperty(process, 'arch', { value: p.arch });
  const exe = path.join(sb.runtimeDir, 'bin', p.exe);
  const publish = (marker, opts = {}) => sb.publishRpcBin(marker, { platform: p.key, ...opts });
  try {
    await body({ sb, wm, exe, publish, ensure: () => wm.ensureRpcBinaries(sb.runtimeDir, sb.serverUrl) });
  } finally {
    Object.defineProperty(process, 'platform', saved.platform);
    Object.defineProperty(process, 'arch', saved.arch);
    wm.dispose();
    sb.restore();
  }
}

for (const p of PLATFORMS) {
  test(`${p.key}: the signed build is installed, executable and remembered`, needsUnzip, () =>
    on(p, async ({ sb, exe, publish, ensure }) => {
      publish('v1');
      await ensure();
      assert.equal(fs.readFileSync(exe, 'utf8'), 'rpc v1');
      if (p.key !== 'win-x64') assert.ok(fs.statSync(exe).mode & 0o111, 'the server must be executable');
      assert.equal(JSON.parse(fs.readFileSync(record(sb), 'utf8')).sha256, sb.release.manifest[`rpc_${p.key.replace(/-/g, '_')}_sha256`]);
      assert.equal(fs.existsSync(path.join(sb.runtimeDir, 'rpc-bin.zip')), false, 'the download is cleaned up');
      assert.ok(sb.release.hits.includes(`/v1/download/rpc-bin/${p.key}`));
    }));

  test(`${p.key}: a zip the release does not name is refused and never unpacked`, needsUnzip, () =>
    on(p, async ({ sb, exe, publish, ensure }) => {
      publish('genuine');
      publish('swapped', { sign: false });       // the coordinator now serves a different zip
      await ensure();
      assert.equal(fs.existsSync(exe), false);
      assert.match(warnings(), /does not match the signed release/);
    }));
}

test('another platform\'s signed build is never taken for this one', needsUnzip, () =>
  on(PLATFORMS[1], async ({ sb, exe, ensure }) => {
    sb.publishRpcBin('mac-only', { platform: 'macos-arm64' });
    await ensure();
    assert.equal(fs.existsSync(exe), false);
    assert.match(warnings(), /does not sign GPU engine binaries for linux-x64/);
    assert.equal(rpcDownloads(sb), 0);
  }));

test('a platform DLNE ships no build for gets a clear note and no download', () =>
  on({ key: 'none', platform: 'linux', arch: 'ia32', exe: 'ggml-rpc-server' }, async ({ sb, wm, ensure }) => {
    sb.publishRpcBin('v1', { platform: 'linux-x64' });
    await ensure();
    assert.deepEqual(sb.release.hits, []);
    const log = stub.state.channels.map((c) => c.text()).join('\n');
    assert.match(log, /ships no GPU engine for linux\/ia32/);
  }));

test('installed binaries the release still names are not downloaded again', needsUnzip, () =>
  on(PLATFORMS[1], async ({ sb, publish, ensure }) => {
    publish('v1');
    await ensure();
    await ensure();
    assert.equal(rpcDownloads(sb), 1);
  }));

test('a new signed zip (a llama.cpp upgrade) replaces the installed binaries', needsUnzip, () =>
  on(PLATFORMS[1], async ({ exe, publish, ensure }) => {
    publish('v1');
    await ensure();
    publish('v2');
    await ensure();
    assert.equal(fs.readFileSync(exe, 'utf8'), 'rpc v2');
  }));

test('binaries from before signing (no record) are replaced by the signed ones', needsUnzip, () =>
  on(PLATFORMS[0], async ({ exe, publish, ensure }) => {
    fs.mkdirSync(path.dirname(exe), { recursive: true });
    fs.writeFileSync(exe, 'old unverified binary');
    publish('signed');
    await ensure();
    assert.equal(fs.readFileSync(exe, 'utf8'), 'rpc signed');
  }));

test('a failed check keeps the installed binaries', needsUnzip, () =>
  on(PLATFORMS[2], async ({ exe, publish, ensure }) => {
    publish('v1');
    await ensure();
    publish('v2');
    publish('evil', { sign: false });            // signed v2, but serves something else
    await ensure();
    assert.equal(fs.readFileSync(exe, 'utf8'), 'rpc v1');
    assert.match(warnings(), /existing GPU engine was kept/);
  }));

test('being offline keeps the installed binaries quietly', needsUnzip, () =>
  on(PLATFORMS[1], async ({ sb, exe, publish, ensure }) => {
    publish('v1');
    await ensure();
    sb.release.offline = true;
    await ensure();
    assert.equal(fs.readFileSync(exe, 'utf8'), 'rpc v1');
    assert.equal(warnings(), '');
  }));

test('the engine source is never fetched by the binaries step', needsUnzip, () =>
  on(PLATFORMS[0], async ({ sb, publish, ensure }) => {
    publish('v1');
    await ensure();
    assert.ok(!sb.release.hits.includes('/v1/download/source'), 'an unverified source refresh must not come back');
  }));
