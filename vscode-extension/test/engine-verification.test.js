'use strict';
/**
 * The engine the extension installs and keeps updated, against a coordinator that behaves well
 * and one that does not. Whatever the coordinator serves, only a correctly signed, untampered,
 * not-older release may reach the user's disk, and a failed check must never fall back to
 * something less checked.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const stub = require('./helpers/vscode-stub');
const { createSandbox } = require('./helpers/sandbox');
const { makeKeypair, signedManifest } = require('./helpers/release');
const { WorkerManager } = require('../dist/worker/workerManager');

const engineFile = (sb) => path.join(sb.runtimeDir, 'dlne_server.py');
const recordFile = (sb) => path.join(sb.runtimeDir, '.dlne_release.json');
const errors = () => stub.state.errorMessages.map((e) => e.message).join('\n');

async function withSandbox(opts, body) {
  stub.reset();
  const sb = await createSandbox(opts);
  stub.state.settings['dlne.serverUrl'] = sb.serverUrl;
  const wm = new WorkerManager({ subscriptions: [] }, { releasePublicKeyHex: sb.releasePublicKeyHex });
  try {
    await body(sb, wm);
  } finally {
    wm.dispose();
    sb.restore();
  }
}

const install = (wm) => wm.ensureWorkerRuntime(false);
const sync = (wm, sb) => wm.syncEngineSource(sb.runtimeDir, sb.serverUrl);
const downloads = (sb) => sb.release.hits.filter((u) => u === '/v1/download/source').length;

// --- first install ------------------------------------------------------------------------

test('a signed release installs, and the install remembers which release it was', () =>
  withSandbox({}, async (sb, wm) => {
    const result = await install(wm);
    assert.ok(result, errors());
    const record = JSON.parse(fs.readFileSync(recordFile(sb), 'utf8'));
    assert.equal(record.version, '2.0.0');
    assert.equal(record.sha256, sb.release.manifest.sha256);
    assert.deepEqual(sb.release.hits.slice(0, 2), ['/v1/release', '/v1/download/source']);
  }));

test('a coordinator that publishes no signed release is refused, and nothing is downloaded', () =>
  withSandbox({ signed: false }, async (sb, wm) => {
    assert.equal(await install(wm), null);
    assert.match(errors(), /does not publish a signed release/);
    assert.match(errors(), /allowUnsignedEngine/);
    assert.equal(fs.existsSync(engineFile(sb)), false);
    assert.equal(downloads(sb), 0);
  }));

test('the private-coordinator opt-in installs without a signature, and leaves no release record', () =>
  withSandbox({ signed: false }, async (sb, wm) => {
    stub.state.settings['dlne.worker.allowUnsignedEngine'] = true;
    assert.ok(await install(wm), errors());
    assert.equal(fs.existsSync(engineFile(sb)), true);
    assert.equal(fs.existsSync(recordFile(sb)), false);
  }));

test('a tampered archive is refused and never extracted', () =>
  withSandbox({}, async (sb, wm) => {
    const evil = path.join(sb.root, 'evil.zip');
    fs.writeFileSync(evil, 'not the engine that was signed');
    sb.release.zipPath = evil;                       // the manifest still describes the genuine archive
    assert.equal(await install(wm), null);
    assert.match(errors(), /does not match the signed release/);
    assert.match(errors(), /NOT installed/);
    assert.equal(fs.existsSync(engineFile(sb)), false);
    assert.equal(fs.existsSync(path.join(sb.runtimeDir, 'dlne_bundle.zip')), false, 'the bad archive must be deleted');
  }));

test('a manifest signed by someone else is refused before the archive is even requested', () =>
  withSandbox({}, async (sb, wm) => {
    sb.release.manifest = signedManifest(sb.release.zipPath, makeKeypair().privateKey);
    assert.equal(await install(wm), null);
    assert.match(errors(), /failed verification/);
    assert.equal(downloads(sb), 0);
    assert.equal(fs.existsSync(engineFile(sb)), false);
  }));

test('a manifest edited after signing is refused', () =>
  withSandbox({}, async (sb, wm) => {
    sb.release.manifest = { ...sb.release.manifest, version: '9.9.9' };
    assert.equal(await install(wm), null);
    assert.match(errors(), /failed verification/);
    assert.equal(downloads(sb), 0);
  }));

test('a failed verified download never falls back to an unverified bundle', () =>
  withSandbox({}, async (sb, wm) => {
    sb.release.manifest = { ...sb.release.manifest, sha256: '0'.repeat(64) };   // signature now invalid
    assert.equal(await install(wm), null);
    const paths = sb.release.hits;
    assert.ok(!paths.includes('/v1/download/windows'), 'no Windows bundle fallback');
    assert.ok(!paths.includes('/v1/download/worker'), 'no worker bundle fallback');
    assert.equal(downloads(sb), 0);
  }));

test('an unreachable coordinator is reported as a download problem, not a verification verdict', () =>
  withSandbox({}, async (sb, wm) => {
    sb.release.offline = true;
    assert.equal(await install(wm), null);
    assert.match(errors(), /Failed to download DLNE engine/);
    assert.doesNotMatch(errors(), /NOT installed|failed verification/);
  }));

// --- keeping it up to date ----------------------------------------------------------------

test('syncing when already on the current release downloads nothing', () =>
  withSandbox({}, async (sb, wm) => {
    assert.ok(await install(wm));
    const before = downloads(sb);
    await sync(wm, sb);
    assert.equal(downloads(sb), before, 'no second download of the same release');
  }));

test('a newer signed release is installed over the old one and recorded', () =>
  withSandbox({}, async (sb, wm) => {
    assert.ok(await install(wm));
    sb.publishUpdate('2.1.0', { marker: 'NEWER' });
    await sync(wm, sb);
    assert.match(fs.readFileSync(engineFile(sb), 'utf8'), /NEWER/);
    assert.equal(JSON.parse(fs.readFileSync(recordFile(sb), 'utf8')).version, '2.1.0');
    assert.equal(fs.existsSync(path.join(sb.runtimeDir, 'dlne_source_update.zip')), false);
  }));

test('a tampered update is refused, the working engine is kept, and the user is told', () =>
  withSandbox({}, async (sb, wm) => {
    assert.ok(await install(wm));
    const before = fs.readFileSync(engineFile(sb), 'utf8');
    sb.publishUpdate('2.1.0', { marker: 'NEWER' });
    const evil = path.join(sb.root, 'evil.zip');
    fs.writeFileSync(evil, 'malicious replacement');
    sb.release.zipPath = evil;
    stub.state.warnings = [];
    await sync(wm, sb);
    assert.equal(fs.readFileSync(engineFile(sb), 'utf8'), before, 'the installed engine must be untouched');
    assert.equal(JSON.parse(fs.readFileSync(recordFile(sb), 'utf8')).version, '2.0.0');
    assert.equal(fs.existsSync(path.join(sb.runtimeDir, 'dlne_source_update.zip')), false);
    assert.match(stub.state.channels.map((c) => c.text()).join('\n'), /Engine update refused/);
  }));

test('an older (replayed) signed release cannot downgrade the engine', () =>
  withSandbox({}, async (sb, wm) => {
    assert.ok(await install(wm));
    const before = fs.readFileSync(engineFile(sb), 'utf8');
    sb.publishUpdate('1.0.0', { marker: 'OLD-VULNERABLE' });
    await sync(wm, sb);
    assert.equal(fs.readFileSync(engineFile(sb), 'utf8'), before);
    assert.doesNotMatch(fs.readFileSync(engineFile(sb), 'utf8'), /OLD-VULNERABLE/);
    assert.match(stub.state.channels.map((c) => c.text()).join('\n'), /older than the 2\.0\.0 already installed/);
  }));

test('an update signed with the wrong key is refused', () =>
  withSandbox({}, async (sb, wm) => {
    assert.ok(await install(wm));
    const before = fs.readFileSync(engineFile(sb), 'utf8');
    sb.publishUpdate('2.1.0', { marker: 'FORGED' });
    sb.release.manifest = signedManifest(sb.release.zipPath, makeKeypair().privateKey, { version: '2.1.0' });
    await sync(wm, sb);
    assert.equal(fs.readFileSync(engineFile(sb), 'utf8'), before);
  }));

test('a coordinator that goes offline does not stop a worker that already has its engine', () =>
  withSandbox({}, async (sb, wm) => {
    assert.ok(await install(wm));
    const before = fs.readFileSync(engineFile(sb), 'utf8');
    sb.release.offline = true;
    await sync(wm, sb);                                    // must not throw
    assert.equal(fs.readFileSync(engineFile(sb), 'utf8'), before);
    assert.match(stub.state.channels.map((c) => c.text()).join('\n'), /Engine update skipped/);
  }));

test('a coordinator that stops signing does not get to replace the engine', () =>
  withSandbox({}, async (sb, wm) => {
    assert.ok(await install(wm));
    const before = fs.readFileSync(engineFile(sb), 'utf8');
    sb.publishUpdate('2.1.0', { marker: 'UNSIGNED' });
    sb.release.manifest = null;                             // /v1/release now 404s
    await sync(wm, sb);
    assert.equal(fs.readFileSync(engineFile(sb), 'utf8'), before);
    assert.match(stub.state.channels.map((c) => c.text()).join('\n'), /does not publish a signed release/);
  }));
