'use strict';
/**
 * A small, independent Node reference for the end-to-end encryption in docs/coding-members.md section 6.
 * tests/test_e2e.py runs it (when node is installed) to prove that engine/e2e.py and Node's own crypto agree,
 * both on the known-answer vectors and on fresh random messages in every direction. It uses Node `crypto` only
 * and shares no code with the TypeScript module (vscode-extension/src/privacy/e2e.ts) or with engine/e2e.py.
 * It checks the cryptography, not the strict input rules: those are tested in each language against the vectors.
 *
 *   node e2e_ref.js vectors <vectors.json>        verify every accepted vector and every "auth" reject
 *   node e2e_ref.js seal-request <memberPubB64>   body JSON on stdin -> {envelope, resp_key_hex}
 *   node e2e_ref.js open-request <memberPrivHex>   wire body on stdin -> {body_utf8, resp_key_hex}
 *   node e2e_ref.js seal-stream <respKeyHex>       {payloads: [str], pad} on stdin -> {objects} (rs on the first,
 *                                                   a sealed [DONE] at the end)
 *   node e2e_ref.js open-stream <respKeyHex>       {objects} on stdin -> {payloads: [str], ended: bool}
 */
const crypto = require('node:crypto');
const fs = require('node:fs');

const SPKI = Buffer.from('302a300506032b656e032100', 'hex');
const PKCS8 = Buffer.from('302e020100300506032b656e04220420', 'hex');

const pubKey = (raw) => crypto.createPublicKey({ key: Buffer.concat([SPKI, raw]), format: 'der', type: 'spki' });
const privKey = (raw) => crypto.createPrivateKey({ key: Buffer.concat([PKCS8, raw]), format: 'der', type: 'pkcs8' });
const rawPub = (priv) => crypto.createPublicKey(priv).export({ type: 'spki', format: 'der' }).subarray(-32);

function keys(priv, peerPubRaw, ephPub, memberPub) {
  const shared = crypto.diffieHellman({ privateKey: priv, publicKey: pubKey(peerPubRaw) });
  const okm = Buffer.from(crypto.hkdfSync('sha256', shared, Buffer.concat([ephPub, memberPub]), 'dlne-e2e-v1', 64));
  return { req: okm.subarray(0, 32), resp: okm.subarray(32) };
}

const streamKey = (respKey, rs) => Buffer.from(crypto.hkdfSync('sha256', respKey, rs, 'dlne-e2e-v1-resp', 32));

function seal(key, nonce, aad, plaintext) {
  const c = crypto.createCipheriv('chacha20-poly1305', key, nonce, { authTagLength: 16 });
  c.setAAD(Buffer.from(aad));
  return Buffer.concat([c.update(plaintext), c.final(), c.getAuthTag()]);
}

function open(key, nonce, aad, data) {
  const d = crypto.createDecipheriv('chacha20-poly1305', key, nonce, { authTagLength: 16 });
  d.setAAD(Buffer.from(aad));
  d.setAuthTag(data.subarray(-16));
  return Buffer.concat([d.update(data.subarray(0, -16)), d.final()]);
}

const counterNonce = (n) => { const b = Buffer.alloc(12); b.writeUInt32BE(n, 8); return b; };
const stdin = () => fs.readFileSync(0);
const padTo = (buf, block) => (block > 1 ? Buffer.concat([buf, Buffer.alloc((block - (buf.length % block)) % block, 0x20)]) : buf);
const sealObject = (key, n, payload) => seal(key, counterNonce(n), 'dlne-resp', payload);
const openObject = (key, o) => open(key, counterNonce(o.n), 'dlne-resp', Buffer.from(o.ct, 'base64'));

function openEnvelope(env, memberPrivRaw) {
  const e = env.dlne_e2e || env;
  const priv = privKey(memberPrivRaw);
  const epk = Buffer.from(e.epk, 'base64');
  const { req, resp } = keys(priv, epk, epk, rawPub(priv));
  return { plaintext: open(req, Buffer.from(e.nonce, 'base64'), 'dlne-req', Buffer.from(e.ct, 'base64')), resp };
}

function checkVectors(file) {
  const v = JSON.parse(fs.readFileSync(file, 'utf8'));
  let checked = 0;
  const must = (cond, what) => { if (!cond) { console.error('FAIL: ' + what); process.exit(1); } checked += 1; };
  const fails = (fn) => { try { fn(); } catch { return true; } return false; };

  for (const r of v.requests) {
    const { plaintext, resp } = openEnvelope(r.envelope, Buffer.from(r.member_priv_hex, 'hex'));
    must(plaintext.toString('utf8') === r.plaintext_utf8, `request ${r.name} plaintext`);
    must(resp.toString('hex') === r.resp_key_hex, `request ${r.name} response key`);
  }
  for (const k of v.stream_keys) {
    must(streamKey(Buffer.from(k.resp_key_hex, 'hex'), Buffer.from(k.rs_b64, 'base64')).toString('hex') === k.stream_key_hex, 'stream key');
  }
  for (const r of v.responses) {
    const key = streamKey(Buffer.from(r.resp_key_hex, 'hex'), Buffer.from(r.rs_b64, 'base64'));
    must(key.toString('hex') === r.stream_key_hex, `response ${r.name} stream key`);
    r.objects.forEach((o, i) => {
      must((o.sealed.rs !== undefined) === (i === 0), `response ${r.name} object ${i} carries rs only first`);
      must(o.sealed.n === i, `response ${r.name} object ${i} counter`);
      must(openObject(key, o.sealed).toString('utf8') === o.plaintext_utf8, `response ${r.name} object ${i} plaintext`);
      must(sealObject(key, i, Buffer.from(o.plaintext_utf8, 'utf8')).toString('base64') === o.sealed.ct, `response ${r.name} object ${i} sealed`);
    });
  }
  for (const l of v.low_level) {
    const key = Buffer.from(l.key_hex, 'hex');
    must(openObject(key, l).toString('utf8') === l.plaintext_utf8, `low-level ${l.name} plaintext`);
    must(sealObject(key, l.n, Buffer.from(l.plaintext_utf8, 'utf8')).toString('base64') === l.ct, `low-level ${l.name} sealed`);
  }
  for (const r of v.request_rejects.filter((x) => x.code === 'auth')) {
    must(fails(() => openEnvelope(r.envelope, Buffer.from(r.member_priv_hex, 'hex'))), `request reject ${r.name}`);
  }
  for (const r of v.response_rejects.filter((x) => x.code === 'auth')) {
    must(fails(() => openObject(Buffer.from(r.key_hex, 'hex'), r)), `payload reject ${r.name}`);
  }
  for (const r of v.stream_rejects.filter((x) => x.code === 'auth')) {
    const key = streamKey(Buffer.from(r.resp_key_hex, 'hex'), Buffer.from(r.objects[0].rs, 'base64'));
    must(fails(() => openObject(key, r.objects[r.fail_at])), `stream reject ${r.name}`);
  }
  console.log(`ok ${checked}`);
}

const [mode, a] = process.argv.slice(2);
if (mode === 'vectors') {
  checkVectors(a);
} else if (mode === 'seal-request') {
  const memberPub = Buffer.from(a, 'base64');
  const eph = crypto.generateKeyPairSync('x25519');
  const ephPub = rawPub(eph.privateKey);
  const { req, resp } = keys(eph.privateKey, memberPub, ephPub, memberPub);
  const nonce = crypto.randomBytes(12);
  const ct = seal(req, nonce, 'dlne-req', stdin());
  const envelope = { dlne_e2e: { v: 1, epk: ephPub.toString('base64'), nonce: nonce.toString('base64'), ct: ct.toString('base64') } };
  console.log(JSON.stringify({ envelope, resp_key_hex: resp.toString('hex') }));
} else if (mode === 'open-request') {
  const { plaintext, resp } = openEnvelope(JSON.parse(stdin().toString('utf8')), Buffer.from(a, 'hex'));
  console.log(JSON.stringify({ body_utf8: plaintext.toString('utf8'), resp_key_hex: resp.toString('hex') }));
} else if (mode === 'seal-stream') {
  const { payloads, pad } = JSON.parse(stdin().toString('utf8'));
  const rs = crypto.randomBytes(16);
  const key = streamKey(Buffer.from(a, 'hex'), rs);
  const items = [...payloads.map((p) => Buffer.from(p, 'utf8')), Buffer.from('[DONE]')];
  const objects = items.map((item, n) => {
    const o = { n };
    if (n === 0) o.rs = rs.toString('base64');
    o.ct = sealObject(key, n, padTo(item, pad)).toString('base64');
    return o;
  });
  console.log(JSON.stringify({ objects }));
} else if (mode === 'open-stream') {
  const { objects } = JSON.parse(stdin().toString('utf8'));
  const key = streamKey(Buffer.from(a, 'hex'), Buffer.from(objects[0].rs, 'base64'));
  const payloads = [];
  let ended = false;
  objects.forEach((o, i) => {
    if (o.n !== i) throw new Error('counter out of order');
    const data = openObject(key, o);
    if (data.toString('utf8').replace(/ +$/, '') === '[DONE]') ended = true;
    else payloads.push(data.toString('utf8'));
  });
  console.log(JSON.stringify({ payloads, ended }));
} else {
  console.error('usage: e2e_ref.js vectors|seal-request|open-request|seal-stream|open-stream ...');
  process.exit(2);
}
