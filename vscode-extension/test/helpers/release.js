'use strict';
/**
 * Signing a release in the tests, with a throwaway key, the same way the maintainer's tool does:
 * Ed25519 over the canonical bytes of the manifest. The canonical form itself is checked against
 * Python's elsewhere (engineRelease.test.js), so signing here with the TypeScript canonicaliser
 * is sound.
 */
const crypto = require('node:crypto');
const fs = require('node:fs');

const { canonicalBytes } = require('../../dist/worker/engineRelease');

function makeKeypair() {
  const { publicKey, privateKey } = crypto.generateKeyPairSync('ed25519');
  // The last 32 bytes of the SubjectPublicKeyInfo DER are the raw public key.
  const publicKeyHex = publicKey.export({ format: 'der', type: 'spki' }).subarray(-32).toString('hex');
  return { privateKey, publicKeyHex };
}

function sha256(buffer) {
  return crypto.createHash('sha256').update(buffer).digest('hex');
}

/** A signed manifest for the archive at `zipPath`. `overrides` are applied BEFORE signing. */
function signedManifest(zipPath, privateKey, overrides = {}) {
  const data = fs.readFileSync(zipPath);
  const manifest = {
    schema: 1,
    version: '2.0.0',
    file: 'dlne-source.zip',
    sha256: sha256(data),
    size: data.length,
    git_commit: 'abc1234',
    built_at: '2026-10-25T12:00:00Z',
    notes: 'test release',
    ...overrides,
  };
  manifest.signature = crypto.sign(null, canonicalBytes(manifest), privateKey).toString('hex');
  return manifest;
}

module.exports = { makeKeypair, signedManifest, sha256 };
