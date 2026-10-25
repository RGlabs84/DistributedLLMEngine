'use strict';
/**
 * Builds a throwaway machine for the first-run installer: a temp HOME, a PATH made only of the
 * tools we choose to expose (so "no unzip" / "no python" / "python 3.9" are real conditions, not
 * mocks), and a mock coordinator that serves the engine source zip.
 */
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const { execFileSync, spawnSync } = require('node:child_process');
const { makeKeypair, signedManifest } = require('./release');

const REPO_ROOT = path.resolve(__dirname, '..', '..', '..');

function which(cmd) {
  const r = spawnSync('sh', ['-c', `command -v ${cmd}`], { encoding: 'utf8' });
  return r.status === 0 ? r.stdout.trim() : null;
}

// Resolved now, while PATH is intact: a sandbox replaces PATH, and tests build zips inside it.
const ZIP_BIN = which('zip') || 'zip';

function writeExecutable(file, body) {
  fs.writeFileSync(file, body, { mode: 0o755 });
}

// A fake interpreter whose behaviour is driven by FAKE_* environment variables, so one script
// can play "python 3.9", "venv module missing", "pip offline" and "all good".
const FAKE_SYSTEM_PYTHON = (realPython) => `#!/bin/sh
case "$*" in
  *sys.version_info*) echo "\${FAKE_PYVER:-3.12}"; exit 0;;
  *"-m zipfile"*) exec ${realPython || 'false'} "$@";;
  "-m venv "*)
    if [ "$FAKE_VENV" = fail ]; then
      echo "The virtual environment was not created successfully because ensurepip is not available. On Debian/Ubuntu systems, you need to install the python3-venv package" >&2
      exit 1
    fi
    mkdir -p "$3/bin" && cp "$FAKE_VENV_PY" "$3/bin/python" && chmod +x "$3/bin/python"
    exit 0;;
esac
exit 0
`;

const FAKE_VENV_PYTHON = `#!/bin/sh
case "$*" in
  *"-m pip install"*)
    if [ "$FAKE_PIP" = fail ]; then
      echo "ERROR: Could not find a version that satisfies the requirement fastapi (from versions: none)" >&2
      exit 1
    fi
    exit 0;;
  *"import fastapi"*)
    if [ "$FAKE_IMPORT" = fail ]; then
      echo "ModuleNotFoundError: No module named 'fastapi'" >&2
      exit 1
    fi
    exit 0;;
esac
exit 0
`;

/** Starts a coordinator that serves `zipPath` at /v1/download/source. */
function serveRelease(state) {
  const server = http.createServer((req, res) => {
    state.hits.push(req.url);
    if (state.offline) {
      req.socket.destroy();
      return;
    }
    if (req.url === '/v1/release') {
      if (!state.manifest) {
        res.writeHead(404, { 'Content-Type': 'application/json' }).end('{"detail":"no signed release"}');
        return;
      }
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(state.manifest));
    } else if (req.url === '/v1/download/source') {
      res.writeHead(200, { 'Content-Type': 'application/zip' });
      fs.createReadStream(state.zipPath).pipe(res);
    } else if (req.url.startsWith('/v1/download/rpc-bin/') && state.rpcZipPath && req.url.endsWith(`/${state.rpcPlatform}`)) {
      res.writeHead(200, { 'Content-Type': 'application/zip' });
      fs.createReadStream(state.rpcZipPath).pipe(res);
    } else {
      res.writeHead(404).end();
    }
  });
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve(server)));
}

/** Zips `files` (paths relative to `cwd`) into `out`, excluding bytecode caches. */
function makeZip(out, cwd, files) {
  execFileSync(ZIP_BIN, ['-qr', out, ...files, '-x', '*__pycache__*', '-x', '*.pyc'], { cwd });
}

/**
 * @param {object} opts
 * @param {'fake'|'real'|'none'} opts.python  which interpreter the sandbox exposes
 * @param {boolean} opts.unzip                expose the unzip binary
 * @param {string} opts.requirements          requirements.txt content for the fake source zip
 * @param {object} opts.sourceFromRepo        zip the real repo source instead of a stub
 * @param {boolean} opts.signed               publish a signed /v1/release (false: a coordinator that publishes none)
 * @param {string} opts.version               the released version
 *
 * The coordinator's state is exposed as `release` so a test can stage an update, an attack or an
 * outage: swap `release.manifest` / `release.zipPath`, or set `release.offline`.
 */
async function createSandbox({ python = 'fake', unzip = true, requirements = 'fastapi\nhttpx\nuvicorn\n', sourceFromRepo = false, signed = true, version = '2.0.0' } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-sandbox-'));
  const home = path.join(root, 'home');
  const bin = path.join(root, 'bin');
  fs.mkdirSync(home);
  fs.mkdirSync(bin);

  // Only the system tools the installer and the fake interpreters need.
  const exposed = ['sh', 'mkdir', 'cp', 'chmod', 'cat', 'tar', 'ls', 'env'];
  if (unzip) exposed.push('unzip');
  for (const tool of exposed) {
    const real = which(tool);
    if (real) fs.symlinkSync(real, path.join(bin, tool));
  }

  const realPython = which('python3');
  if (python === 'fake') {
    writeExecutable(path.join(root, 'fake_venv_python'), FAKE_VENV_PYTHON);
    for (const name of ['python3', 'python']) {
      writeExecutable(path.join(bin, name), FAKE_SYSTEM_PYTHON(realPython));
    }
  } else if (python === 'real') {
    fs.symlinkSync(realPython, path.join(bin, 'python3'));
  } // 'none': no interpreter at all

  const src = path.join(root, 'src');
  fs.mkdirSync(src);
  let zipPath = path.join(root, 'dlne-source.zip');
  if (sourceFromRepo) {
    makeZip(zipPath, REPO_ROOT, ['dlne_server.py', 'requirements.txt', 'engine']);
  } else {
    fs.writeFileSync(path.join(src, 'dlne_server.py'), 'print("stub engine")\n');
    fs.writeFileSync(path.join(src, 'requirements.txt'), requirements);
    makeZip(zipPath, src, ['dlne_server.py', 'requirements.txt']);
  }

  // The release the mock coordinator publishes, signed with a throwaway key whose public half
  // the test hands to the WorkerManager (production uses the maintainer's baked-in key).
  const { privateKey, publicKeyHex } = makeKeypair();
  const state = {
    zipPath,
    manifest: signed ? signedManifest(zipPath, privateKey, { version }) : null,
    offline: false,
    hits: [],
  };
  const server = await serveRelease(state);

  // Save and swap process-wide state; restore() puts it back.
  const saved = { HOME: process.env.HOME, PATH: process.env.PATH };
  const fakeVars = ['FAKE_PYVER', 'FAKE_VENV', 'FAKE_PIP', 'FAKE_IMPORT', 'FAKE_VENV_PY'];
  fakeVars.forEach((k) => (saved[k] = process.env[k]));
  process.env.HOME = home;
  process.env.PATH = bin;
  process.env.FAKE_VENV_PY = path.join(root, 'fake_venv_python');

  return {
    root,
    home,
    runtimeDir: path.join(home, '.dlne', 'runtime'),
    serverUrl: `http://127.0.0.1:${server.address().port}`,
    release: state,
    privateKey,
    releasePublicKeyHex: publicKeyHex,
    /** Build a new signed release of the stub engine (e.g. an update) and return its zip path. */
    publishUpdate(version, { marker = version } = {}) {
      const dir = path.join(root, `update-${version}`);
      fs.mkdirSync(dir, { recursive: true });
      fs.writeFileSync(path.join(dir, 'dlne_server.py'), `print("stub engine ${marker}")\n`);
      fs.writeFileSync(path.join(dir, 'requirements.txt'), requirements);
      const zip = path.join(root, `dlne-source-${version}.zip`);
      makeZip(zip, dir, ['dlne_server.py', 'requirements.txt']);
      state.zipPath = zip;
      state.manifest = signedManifest(zip, privateKey, { version });
      return zip;
    },
    /**
     * Serve an RPC zip for `platform` whose bin/ggml-rpc-server(.exe) holds `marker`, and
     * (unless `sign: false`) re-sign the current release so its manifest names that zip.
     */
    publishRpcBin(marker, { sign = true, platform = 'win-x64' } = {}) {
      const exe = platform === 'win-x64' ? 'ggml-rpc-server.exe' : 'ggml-rpc-server';
      const dir = path.join(root, `rpc-${platform}-${marker}`);
      fs.mkdirSync(path.join(dir, 'bin'), { recursive: true });
      fs.writeFileSync(path.join(dir, 'bin', exe), `rpc ${marker}`);
      fs.writeFileSync(path.join(dir, 'bin', platform === 'win-x64' ? 'ggml-base.dll' : 'libggml-base.so'), `lib ${marker}`);
      const zip = path.join(root, `rpc-bin-${platform}-${marker}.zip`);
      makeZip(zip, dir, ['bin']);
      state.rpcZipPath = zip;
      state.rpcPlatform = platform;
      if (sign) {
        const data = fs.readFileSync(zip);
        const key = platform.replace(/-/g, '_');
        state.manifest = signedManifest(state.zipPath, privateKey, {
          version: state.manifest ? state.manifest.version : version,
          llama_build: 'b11838',
          rpc_proto: '8.0.1',
          [`rpc_${key}_sha256`]: require('./release').sha256(data),
          [`rpc_${key}_size`]: data.length,
        });
      }
      return zip;
    },
    set(vars) {
      Object.assign(process.env, vars);
    },
    restore() {
      server.close();
      for (const [k, v] of Object.entries(saved)) {
        if (v === undefined) delete process.env[k];
        else process.env[k] = v;
      }
      fs.rmSync(root, { recursive: true, force: true });
    },
  };
}

module.exports = { createSandbox, REPO_ROOT, which };
