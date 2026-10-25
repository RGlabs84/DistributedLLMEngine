'use strict';
/**
 * End-to-end: the real installer on a clean machine. Real Python, real `venv`, real `pip install`
 * of the engine's real requirements.txt (network required), then the installed environment must
 * be able to import and start the engine's entrypoint.
 *
 *   npm run test:e2e                 engine source taken from this checkout, served by a local mock
 *   DLNE_LIVE=1 npm run test:e2e     engine source downloaded from the live public coordinator
 *
 * Slow (a minute or two) because pip really downloads and installs.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const stub = require('../helpers/vscode-stub');
const { createSandbox } = require('../helpers/sandbox');
const { WorkerManager } = require('../../dist/worker/workerManager');

const LIVE = process.env.DLNE_LIVE === '1';
const LIVE_URL = 'http://73.215.216.195:11435';

test(`real install on a clean machine (${LIVE ? 'live coordinator source' : 'checkout source'})`, { timeout: 15 * 60 * 1000 }, async (t) => {
  stub.reset();
  const sb = await createSandbox({ python: 'real', sourceFromRepo: true });
  stub.state.settings['dlne.serverUrl'] = LIVE ? LIVE_URL : sb.serverUrl;
  // Against the live coordinator the real, baked-in public key must be used (it verifies the
  // maintainer's real signature); against the local mock, the test's own throwaway key.
  const wm = new WorkerManager({ subscriptions: [] }, LIVE ? {} : { releasePublicKeyHex: sb.releasePublicKeyHex });
  try {
    const result = await wm.ensureWorkerRuntime(false);
    const log = stub.state.channels.map((c) => c.text()).join('\n');
    assert.ok(result, `installer failed.\nErrors: ${JSON.stringify(stub.state.errorMessages)}\n${log.slice(-3000)}`);
    assert.equal(stub.state.errorMessages.length, 0);
    assert.equal(stub.state.infoMessages.filter((m) => /installed successfully/i.test(m)).length, 1);

    // The venv exists, is Python 3.10+, and lives under the runtime dir.
    const py = result.pythonPath;
    assert.ok(py.startsWith(sb.runtimeDir), `venv outside runtime dir: ${py}`);
    const ver = spawnSync(py, ['-c', 'import sys; print("%d.%d" % sys.version_info[:2])'], { encoding: 'utf8' });
    const [major, minor] = ver.stdout.trim().split('.').map(Number);
    assert.ok(major === 3 && minor >= 10, `venv python ${ver.stdout}`);

    // Everything the engine needs at import time was installed - this is what a requirements.txt
    // that has drifted from the code would break.
    const run = (args) => spawnSync(py, args, { cwd: sb.runtimeDir, encoding: 'utf8', timeout: 120000, env: { ...process.env, HOME: sb.home } });
    const imports = run(['-c', 'import fastapi, httpx, uvicorn, psutil, websockets, pydantic; import engine.coordinator, engine.worker, engine.version; print(engine.version.VERSION)']);
    assert.equal(imports.status, 0, `engine does not import in the fresh venv:\n${imports.stderr}`);

    // The entrypoint itself starts and parses arguments.
    const help = run([result.scriptPath, '--help']);
    assert.equal(help.status, 0, `dlne_server.py --help failed:\n${help.stderr}`);
    assert.match(help.stdout, /usage:/i);

    // And the config the worker will read is in place.
    const cfg = fs.readFileSync(path.join(sb.runtimeDir, 'config', 'config.txt'), 'utf8');
    assert.match(cfg, /^NETWORK_KEY=[0-9a-f]{64}$/m);
  } finally {
    wm.dispose();
    sb.restore();
  }
});
