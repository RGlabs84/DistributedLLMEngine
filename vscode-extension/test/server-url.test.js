'use strict';
/**
 * The coordinator address: HTTPS by default, the old plain-HTTP public address upgraded, and
 * a private coordinator left exactly as the user set it.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

require('./helpers/vscode-stub');
const { resolveServerUrl, DEFAULT_SERVER_URL, LEGACY_SERVER_URL, SSLIP_SERVER_URL, DUCKDNS_SERVER_URL } = require('../dist/serverUrl');
const pkg = require('../package.json');

const config = (value) => ({ get: (key) => (key === 'serverUrl' ? value : undefined) });

test('the default is the public swarm over HTTPS, by name', () => {
  assert.equal(DEFAULT_SERVER_URL, 'https://dlne.rossgilsonlabs.dev');
  assert.equal(resolveServerUrl(config(undefined)), DEFAULT_SERVER_URL);
  assert.equal(resolveServerUrl(config('')), DEFAULT_SERVER_URL);
  assert.equal(resolveServerUrl(config('   ')), DEFAULT_SERVER_URL);
});

test('the setting in package.json agrees with the code', () => {
  assert.equal(pkg.contributes.configuration.properties['dlne.serverUrl'].default, DEFAULT_SERVER_URL);
});

test('the old plain-HTTP public address is upgraded to HTTPS', () => {
  assert.equal(resolveServerUrl(config(LEGACY_SERVER_URL)), DEFAULT_SERVER_URL);
  assert.equal(resolveServerUrl(config(LEGACY_SERVER_URL + '/')), DEFAULT_SERVER_URL);
});

test('the 1.2.0-1.2.3 sslip.io address is read as the named one', () => {
  assert.equal(SSLIP_SERVER_URL, 'https://73-215-216-195.sslip.io');
  assert.equal(resolveServerUrl(config(SSLIP_SERVER_URL)), DEFAULT_SERVER_URL);
  assert.equal(resolveServerUrl(config(SSLIP_SERVER_URL + '/')), DEFAULT_SERVER_URL);
});

test('the 1.2.4-1.3.3 DuckDNS name is read as the project name', () => {
  assert.equal(DUCKDNS_SERVER_URL, 'https://dlne.duckdns.org');
  assert.equal(resolveServerUrl(config(DUCKDNS_SERVER_URL)), DEFAULT_SERVER_URL);
  assert.equal(resolveServerUrl(config(DUCKDNS_SERVER_URL + '/')), DEFAULT_SERVER_URL);
});

test('a private coordinator is used as set, minus trailing slashes', () => {
  assert.equal(resolveServerUrl(config('http://192.168.1.50:11435')), 'http://192.168.1.50:11435');
  assert.equal(resolveServerUrl(config('https://dlne.example.org//')), 'https://dlne.example.org');
  // Same host, different port: someone's own setup, not the legacy default.
  assert.equal(resolveServerUrl(config('http://73.215.216.195:8000')), 'http://73.215.216.195:8000');
});

test('the worker is given the full coordinator URL, scheme included', () => {
  // dlne_server.py prefixes http:// to a bare host; stripping https:// sent workers to port
  // 80, where the proxy redirects and the worker's POSTs never register.
  const source = fs.readFileSync(path.join(__dirname, '..', 'src', 'worker', 'workerManager.ts'), 'utf8');
  assert.match(source, /'--coordinator', coordinatorArg\]/);
  assert.match(source, /const coordinatorArg = serverUrl;/);
  assert.doesNotMatch(source, /serverUrl\.replace\(\/\^https\?/);
});

test('no source file hardcodes the old address any more', () => {
  const offenders = [];
  const walk = (dir) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (entry.name.endsWith('.ts') && entry.name !== 'serverUrl.ts' &&
               fs.readFileSync(full, 'utf8').includes('73.215.216.195')) offenders.push(full);
    }
  };
  walk(path.join(__dirname, '..', 'src'));
  assert.deepEqual(offenders, []);
});

test('no source file looks for an engine in the maintainer\'s own folders', () => {
  // A path like ~/DLNEv2 or the development checkout ships to every user, names the maintainer's machines,
  // and on the maintainer's own laptop makes any DLNE install run the development tree.
  const offenders = [];
  const walk = (dir) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (entry.name.endsWith('.ts') && /WubarrkCODING|DLNEv2|valheim-testbed|archserver/.test(fs.readFileSync(full, 'utf8'))) {
        offenders.push(full);
      }
    }
  };
  walk(path.join(__dirname, '..', 'src'));
  assert.deepEqual(offenders, []);
});

test('a refused chat shows the coordinator\'s sentence, not raw JSON', () => {
  const { serverErrorMessage } = require('../dist/client/dlneClient');
  const detail = "You've used all 300 coding chats for this 5-hour window (the allowance for CPU contributors). The next one frees up in 42 minutes. Contributing a GPU removes the limit.";
  assert.equal(serverErrorMessage(429, JSON.stringify({ detail })), detail);
  assert.equal(serverErrorMessage(502, '<html>Bad Gateway</html>'), 'Server returned 502: <html>Bad Gateway</html>');
  assert.equal(serverErrorMessage(500, ''), 'Server returned 500');
  assert.equal(serverErrorMessage(403, JSON.stringify({ detail: 'x'.repeat(2000) })).length, 600);
});

test('the chat explains each kind of refusal in its own words', () => {
  const { refusalNotice } = require('../dist/agent/agentLoop');
  const window = "You've used all 300 coding chats for this 5-hour window (the allowance for CPU contributors). The next one frees up in 42 minutes.";
  assert.equal(refusalNotice(429, window), `\n\n⏳ ${window}`);
  const floors = 'This machine has no usable GPU, and a CPU-only node needs 4 physical CPU cores (this machine has 2).';
  assert.equal(refusalNotice(403, floors), `\n\n🔒 ${floors}`);
  const trial = 'DLNE Compute Required: Free starter quota (100/100 inferences) used.';
  assert.match(refusalNotice(403, trial), /Your free trial is used up/);
  assert.match(refusalNotice(undefined, 'Server returned 403: old coordinator'), /Your free trial is used up/);
  assert.match(refusalNotice(undefined, 'connect ECONNREFUSED'), /^\n\nError connecting to DLNE cluster: connect ECONNREFUSED$/);
});
