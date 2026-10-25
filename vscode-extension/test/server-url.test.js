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
const { resolveServerUrl, DEFAULT_SERVER_URL, LEGACY_SERVER_URL } = require('../dist/serverUrl');
const pkg = require('../package.json');

const config = (value) => ({ get: (key) => (key === 'serverUrl' ? value : undefined) });

test('the default is the public swarm over HTTPS', () => {
  assert.equal(DEFAULT_SERVER_URL, 'https://73-215-216-195.sslip.io');
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
