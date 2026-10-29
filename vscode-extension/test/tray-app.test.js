'use strict';
/**
 * DLNE Donate, the tray app for Windows and Linux (desktop/dlne_donate), seen from the extension:
 *
 * - the Donate panel's "Optional add-on" card, which offers it: download, verify, run. The manifest
 *   (downloads/desktop.json) is checked whole, a download anywhere but https://dlne.rossgilsonlabs.dev/downloads/ is
 *   refused, a wrong size or SHA-256 deletes the file, the card shows only on 64-bit Windows and Linux, and nothing is
 *   fetched before the owner clicks;
 * - the worker manager recognising the app's worker (its command line is `DLNE-Donate --run-worker ...`, which the
 *   older checks missed), showing it by name and never starting a second worker beside it.
 *
 * Nothing here touches the network or a real tray app: the website and the machine are played by the test.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const https = require('node:https');
const childProcess = require('node:child_process');
const { EventEmitter } = require('node:events');

const stub = require('./helpers/vscode-stub');
const t = require('../dist/worker/trayApp');
const d = require('../dist/worker/donatePanel');
const c = require('../dist/worker/contribution');
const { TrayAddon } = require('../dist/worker/trayAddon');
const { WorkerManager } = require('../dist/worker/workerManager');
const { SidebarProvider } = require('../dist/views/sidebarProvider');
const { createDonatePanel } = require('../dist/views/donateView');

const ROOT = path.join(__dirname, '..');
const HOME = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-tray-home-'));
process.env.HOME = HOME;
process.env.USERPROFILE = HOME;
// The constructor's background start-up (probe the GPU, poll the worker, resume) would race these tests.
WorkerManager.prototype.init = async function init() {};

const GIB = 1024 ** 3;

// The builds of 26 Oct 2026 (desktop/packaging), as the website's desktop.json lists them.
const SHA_WINDOWS = '3e0d996015e9e1f6fd53584b9473c5bec1ef9f40b49ee868d7ecdafbd681c13f';
const SHA_LINUX = 'f76199f234e9b8dd9681f2764aa589c029585e7bb4b714caddfd5fd36edac796';
const MANIFEST = {
  versionName: '1.0.0',
  notes: 'Keep donating when VS Code is closed.',
  published: '2026-10-26',
  windows: { url: 'https://dlne.rossgilsonlabs.dev/downloads/DLNE-Donate-1.0.0.exe', sha256: SHA_WINDOWS, size: 34348456 },
  linux: { url: 'https://dlne.rossgilsonlabs.dev/downloads/DLNE-Donate-1.0.0-x86_64.AppImage', sha256: SHA_LINUX, size: 57526776 },
};
const withBuild = (platform, fields) => ({ ...MANIFEST, [platform]: { ...MANIFEST[platform], ...fields } });

/** A fresh, empty folder that plays the owner's Downloads folder. */
const freshFolder = () => fs.mkdtempSync(path.join(HOME, 'downloads-'));
const sha256 = (bytes) => crypto.createHash('sha256').update(bytes).digest('hex');
/** A small "build" (the real ones are 34 and 58 MB) and its published entry. */
function buildOf(bytes, file = 'DLNE-Donate-1.0.0-x86_64.AppImage') {
  return { url: `https://dlne.rossgilsonlabs.dev/downloads/${file}`, file, sha256: sha256(bytes), size: bytes.length };
}

/**
 * Plays the website: answers every GET with `bytes`, in 64 KB chunks. `announce` is the Content-Length it states (null:
 * none), `failAt` the chunk at which the connection drops. `sent` counts the bytes handed out.
 */
function website(bytes, { status = 200, announce = bytes.length, failAt = null, chunk = 64 * 1024 } = {}) {
  const open = async (url) => {
    open.urls.push(url);
    async function* body() {
      for (let at = 0, i = 0; at < bytes.length; at += chunk, i++) {
        if (failAt !== null && i === failAt) throw new Error('connection reset');
        const part = bytes.subarray(at, at + chunk);
        open.sent += part.length;
        yield part;
      }
    }
    return { statusCode: status, headers: announce === null ? {} : { 'content-length': String(announce) }, body: body(), destroy() {} };
  };
  open.urls = [];
  open.sent = 0;
  return open;
}

/** Plays the website for the manifest: `text` is its body. */
function manifestSite(text, status = 200) {
  const bytes = Buffer.from(typeof text === 'string' ? text : JSON.stringify(text));
  return website(bytes, { status, announce: null });
}

async function rejectsWith(promise, problem) {
  await assert.rejects(promise, (err) => {
    assert.ok(err instanceof t.TrayError, `a TrayError, not ${err}`);
    assert.equal(err.problem, problem, err.message);
    return true;
  });
}

// ---- the manifest ---------------------------------------------------------------------------------------------------

test('the manifest the website publishes is read, every field checked', () => {
  const read = t.parseTrayManifest({ ...MANIFEST, extra: 'ignored', windows: { ...MANIFEST.windows, sha256: SHA_WINDOWS.toUpperCase() } });
  assert.equal(read.ok, true, read.reason);
  const m = read.manifest;
  assert.equal(m.versionName, '1.0.0');
  assert.equal(m.published, '2026-10-26');
  assert.equal(m.notes, 'Keep donating when VS Code is closed.');
  assert.deepEqual(m.builds.windows, {
    url: 'https://dlne.rossgilsonlabs.dev/downloads/DLNE-Donate-1.0.0.exe', file: 'DLNE-Donate-1.0.0.exe', sha256: SHA_WINDOWS, size: 34348456,
  }, 'the hash is lowercased; the file is the last part of the address');
  assert.equal(m.builds.linux.file, 'DLNE-Donate-1.0.0-x86_64.AppImage');

  // Notes and the date may be left out, and so may one platform.
  const bare = t.parseTrayManifest({ versionName: '1.0.1', linux: MANIFEST.linux });
  assert.equal(bare.ok, true);
  assert.deepEqual([bare.manifest.notes, bare.manifest.published, bare.manifest.builds.windows], ['', null, undefined]);

  // Notes become one plain line, at most 300 characters.
  const lineSeparator = String.fromCharCode(0x2028);
  const noisy = t.parseTrayManifest({ ...MANIFEST, notes: `Line one\nline\u0007 two${lineSeparator}three   ` + 'x'.repeat(400) });
  assert.equal(noisy.ok, true);
  assert.match(noisy.manifest.notes, /^Line one line two three x+$/);
  assert.equal(noisy.manifest.notes.length, 300);
});

test('a malformed manifest is refused whole, with the reason', () => {
  const cases = [
    [null, /not a JSON object/],
    [[MANIFEST], /not a JSON object/],
    ['{"versionName":"1.0.0"}', /not a JSON object/],
    [{ ...MANIFEST, versionName: undefined }, /versionName/],
    [{ ...MANIFEST, versionName: 'v1' }, /versionName/],
    [{ ...MANIFEST, versionName: 1.0 }, /versionName/],
    [{ ...MANIFEST, notes: 7 }, /notes/],
    [{ ...MANIFEST, notes: 'x'.repeat(2001) }, /notes/],
    [{ ...MANIFEST, published: '2026-02-30' }, /published/],
    [{ ...MANIFEST, published: '2026-13-01' }, /published/],
    [{ ...MANIFEST, published: '26-10-2026' }, /published/],
    [{ ...MANIFEST, published: 20261026 }, /published/],
    [{ ...MANIFEST, windows: null }, /windows is not an object/],
    [{ ...MANIFEST, linux: 'https://dlne.rossgilsonlabs.dev/downloads/x.AppImage' }, /linux is not an object/],
    [withBuild('windows', { sha256: SHA_WINDOWS.slice(1) }), /windows sha256/],
    [withBuild('windows', { sha256: 'z'.repeat(64) }), /windows sha256/],
    [withBuild('linux', { sha256: undefined }), /linux sha256/],
    [withBuild('linux', { size: '57526776' }), /linux size/],
    [withBuild('linux', { size: 57526776.5 }), /linux size/],
    [withBuild('linux', { size: 0 }), /linux size/],
    [withBuild('linux', { size: 999_999 }), /linux size/],
    [withBuild('linux', { size: 600 * 1024 * 1024 }), /linux size/],
    [{ versionName: '1.0.0', notes: 'no builds' }, /no build/],
  ];
  for (const [raw, reason] of cases) {
    const read = t.parseTrayManifest(raw);
    assert.equal(read.ok, false, `accepted ${JSON.stringify(raw)}`);
    assert.match(read.reason, reason, JSON.stringify(raw));
  }
});

test('a download anywhere but https://dlne.rossgilsonlabs.dev/downloads/ is refused, and so is the whole manifest', () => {
  const refused = [
    'http://dlne.rossgilsonlabs.dev/downloads/DLNE-Donate-1.0.0.exe',
    'https://evil.example/downloads/DLNE-Donate-1.0.0.exe',
    'https://dlne.rossgilsonlabs.dev.evil.example/downloads/DLNE-Donate-1.0.0.exe',
    'https://evil.dlne.rossgilsonlabs.dev/downloads/DLNE-Donate-1.0.0.exe',
    'https://rossgilsonlabs.dev/downloads/DLNE-Donate-1.0.0.exe',
    'https://dlne.rossgilsonIabs.dev/downloads/DLNE-Donate-1.0.0.exe',
    'https://dlne.rossgilsonlabs.dev./downloads/DLNE-Donate-1.0.0.exe',
    'https://203.0.113.7/downloads/DLNE-Donate-1.0.0.exe',
    'https://dlne.rossgilsonlabs.dev:8443/downloads/DLNE-Donate-1.0.0.exe',
    'https://user@dlne.rossgilsonlabs.dev/downloads/DLNE-Donate-1.0.0.exe',
    'https://dlne.rossgilsonlabs.dev/downloads/DLNE-Donate-1.0.0.exe?from=evil',
    'https://dlne.rossgilsonlabs.dev/downloads/DLNE-Donate-1.0.0.exe#x',
    'https://dlne.rossgilsonlabs.dev/elsewhere/DLNE-Donate-1.0.0.exe',
    'https://dlne.rossgilsonlabs.dev/downloads/old/DLNE-Donate-1.0.0.exe',
    'https://dlne.rossgilsonlabs.dev/downloads/../DLNE-Donate-1.0.0.exe',
    'https://dlne.rossgilsonlabs.dev/downloads/%2e%2e/DLNE-Donate-1.0.0.exe',
    'https://dlne.rossgilsonlabs.dev/downloads\\DLNE-Donate-1.0.0.exe',
    'https://dlne.rossgilsonlabs.dev/downloads/DLNE Donate.exe',
    'https://dlne.rossgilsonlabs.dev/downloads/.DLNE-Donate.exe',
    'https://dlne.rossgilsonlabs.dev/downloads/DLNE-Donate-1.0.0-x86_64.AppImage', // not a Windows program
    '//dlne.rossgilsonlabs.dev/downloads/DLNE-Donate-1.0.0.exe',
    'ftp://dlne.rossgilsonlabs.dev/downloads/DLNE-Donate-1.0.0.exe',
    'javascript:alert(1)//dlne.rossgilsonlabs.dev/downloads/x.exe',
    `https://dlne.rossgilsonlabs.dev/downloads/${'a'.repeat(300)}.exe`,
    42,
  ];
  for (const url of refused) {
    assert.equal(t.officialDownload(url, 'windows'), null, `accepted ${url}`);
    const read = t.parseTrayManifest(withBuild('windows', { url }));
    assert.equal(read.ok, false, `the manifest with ${url} was used`);
    assert.match(read.reason, /windows url is not an official DLNE download/);
  }
  // Linux takes an AppImage, not a Windows program.
  assert.equal(t.officialDownload('https://dlne.rossgilsonlabs.dev/downloads/DLNE-Donate-1.0.0.exe', 'linux'), null);
  assert.equal(t.officialDownload('https://dlne.rossgilsonlabs.dev/downloads/DLNE-Donate.appimage', 'linux'), null);

  // The same address written differently is the same address.
  assert.deepEqual(t.officialDownload('https://DLNE.rossgilsonlabs.dev:443/downloads/DLNE-Donate-1.0.0.exe', 'windows'), {
    url: 'https://dlne.rossgilsonlabs.dev/downloads/DLNE-Donate-1.0.0.exe', file: 'DLNE-Donate-1.0.0.exe',
  });
  assert.equal(t.TRAY_MANIFEST_URL, 'https://dlne.rossgilsonlabs.dev/downloads/desktop.json');
});

test('sizes are written as the website writes them', () => {
  assert.equal(t.formatSize(34348456), '34.3 MB');
  assert.equal(t.formatSize(57526776), '57.5 MB');
  assert.equal(t.formatSize(640000), '640 KB');
  assert.equal(t.formatSize(0), '');
});

// ---- fetching ------------------------------------------------------------------------------------------------------

test('the manifest is fetched from the website, and a redirect, an error, junk or a huge answer is not used', async () => {
  const site = manifestSite(MANIFEST);
  const manifest = await t.fetchTrayManifest(site);
  assert.deepEqual(site.urls, [t.TRAY_MANIFEST_URL], 'one GET, of the fixed address');
  assert.equal(manifest.builds.linux.size, 57526776);

  await rejectsWith(t.fetchTrayManifest(manifestSite(MANIFEST, 302)), 'redirected');
  await rejectsWith(t.fetchTrayManifest(manifestSite(MANIFEST, 404)), 'http');
  await rejectsWith(t.fetchTrayManifest(manifestSite('<html>not json</html>')), 'refused');
  await rejectsWith(t.fetchTrayManifest(manifestSite('x'.repeat(17 * 1024))), 'too-large');
  await rejectsWith(t.fetchTrayManifest(manifestSite(withBuild('linux', { url: 'https://evil.example/downloads/x.AppImage' }))), 'refused');
  await rejectsWith(t.fetchTrayManifest(async () => { throw new Error('getaddrinfo ENOTFOUND'); }), 'network');
});

test('the real transport only talks https to dlne.rossgilsonlabs.dev, before any connection is made', async () => {
  const realGet = https.get;
  const gets = [];
  https.get = (...args) => { gets.push(args); throw new Error('no network in tests'); };
  try {
    for (const url of ['http://dlne.rossgilsonlabs.dev/downloads/desktop.json', 'https://example.com/downloads/desktop.json',
      'https://dlne.rossgilsonlabs.dev:8443/downloads/desktop.json', 'not a url']) {
      await rejectsWith(t.httpsOpen(url), 'refused');
    }
  } finally {
    https.get = realGet;
  }
  assert.deepEqual(gets, [], 'nothing was opened');
});

// ---- download and verify ---------------------------------------------------------------------------------------------

test('a build that is exactly the published size and SHA-256 is saved under its own name, executable on Linux', async () => {
  const bytes = crypto.randomBytes(300 * 1024);
  const build = buildOf(bytes);
  const folder = freshFolder();
  const target = path.join(folder, build.file);
  fs.writeFileSync(`${target}.part`, 'left over from an earlier attempt');
  const site = website(bytes);
  const progress = [];

  const saved = await t.saveVerifiedBuild(build, target, { platform: 'linux', open: site, onProgress: (n) => progress.push(n) });
  assert.deepEqual(saved, { path: target, reused: false });
  assert.deepEqual(site.urls, [build.url]);
  assert.ok(fs.readFileSync(target).equals(bytes));
  assert.deepEqual(fs.readdirSync(folder), [build.file], 'only the final file: no part file left');
  assert.equal(fs.statSync(target).mode & 0o777, 0o755, 'an AppImage is run directly, so it is executable');
  assert.equal(progress.at(-1), bytes.length);
  assert.ok(progress.every((n, i) => i === 0 || n > progress[i - 1]), 'progress only goes up');

  // Windows: no mode change is asked for.
  const winFolder = freshFolder();
  const winBuild = buildOf(bytes, 'DLNE-Donate-1.0.0.exe');
  const realChmod = fs.promises.chmod;
  const chmods = [];
  fs.promises.chmod = async (...args) => { chmods.push(args); };
  try {
    await t.saveVerifiedBuild(winBuild, path.join(winFolder, winBuild.file), { platform: 'windows', open: website(bytes) });
  } finally {
    fs.promises.chmod = realChmod;
  }
  assert.deepEqual(chmods, []);
});

test('a wrong size or SHA-256 deletes the download, and nothing gets the final name', async () => {
  const bytes = crypto.randomBytes(300 * 1024);
  const build = buildOf(bytes);
  const cases = [
    ['shorter than published', website(bytes.subarray(0, bytes.length - 1), { announce: null }), 'size'],
    ['longer than published', website(Buffer.concat([bytes, crypto.randomBytes(512 * 1024)]), { announce: null }), 'size'],
    ['announced at another size', website(bytes, { announce: bytes.length + 1 }), 'size'],
    ['the same size, other bytes', website(crypto.randomBytes(bytes.length)), 'hash'],
  ];
  for (const [what, site, problem] of cases) {
    const folder = freshFolder();
    const target = path.join(folder, build.file);
    await rejectsWith(t.saveVerifiedBuild(build, target, { platform: 'linux', open: site }), problem);
    assert.deepEqual(fs.readdirSync(folder), [], `${what}: nothing is left in the folder`);
  }
  // Too many bytes stop the transfer at once, not at the end.
  const endless = website(crypto.randomBytes(5 * 1024 * 1024), { announce: null });
  await rejectsWith(t.saveVerifiedBuild(build, path.join(freshFolder(), build.file), { platform: 'linux', open: endless }), 'size');
  assert.ok(endless.sent <= build.size + 64 * 1024, `read ${endless.sent} bytes of a 5 MB answer`);
});

test('a redirect, an HTTP error, a dropped connection or a cancel keeps nothing', async () => {
  const bytes = crypto.randomBytes(300 * 1024);
  const build = buildOf(bytes);
  const cases = [
    ['a redirect', website(bytes, { status: 302 }), {}, 'redirected'],
    ['a server error', website(bytes, { status: 500 }), {}, 'http'],
    ['a dropped connection', website(bytes, { failAt: 2 }), {}, 'network'],
    ['a cancel', website(bytes), { isCancelled: () => true }, 'cancelled'],
  ];
  for (const [what, site, extra, problem] of cases) {
    const folder = freshFolder();
    await rejectsWith(t.saveVerifiedBuild(build, path.join(folder, build.file), { platform: 'linux', open: site, ...extra }), problem);
    assert.deepEqual(fs.readdirSync(folder), [], `${what}: nothing is left`);
  }
});

test('a file already there that is this build is kept without downloading; any other file there is replaced', async () => {
  const bytes = crypto.randomBytes(200 * 1024);
  const build = buildOf(bytes);
  const folder = freshFolder();
  const target = path.join(folder, build.file);

  fs.writeFileSync(target, bytes, { mode: 0o644 });
  const site = website(bytes);
  assert.deepEqual(await t.saveVerifiedBuild(build, target, { platform: 'linux', open: site }), { path: target, reused: true });
  assert.deepEqual(site.urls, [], 'not downloaded again');
  assert.equal(fs.statSync(target).mode & 0o111, 0o111, 'and made executable');

  fs.writeFileSync(target, crypto.randomBytes(bytes.length));
  const again = website(bytes);
  assert.deepEqual(await t.saveVerifiedBuild(build, target, { platform: 'linux', open: again }), { path: target, reused: false });
  assert.equal(again.urls.length, 1);
  assert.ok(fs.readFileSync(target).equals(bytes), 'the wrong file was replaced by the verified one');
});

// ---- this platform ---------------------------------------------------------------------------------------------------

test('DLNE Donate is offered on 64-bit Windows and Linux only: the card is hidden on macOS and everywhere else', () => {
  assert.equal(t.trayPlatform('win32', 'x64'), 'windows');
  assert.equal(t.trayPlatform('linux', 'x64'), 'linux');
  for (const [platform, arch] of [['darwin', 'arm64'], ['darwin', 'x64'], ['win32', 'arm64'], ['linux', 'arm64'], ['linux', 'ia32'], ['freebsd', 'x64'], ['android', 'arm64']]) {
    assert.equal(t.trayPlatform(platform, arch), null, `${platform} ${arch}`);
  }

  const view = (platform, over = {}) => ({ platform, busy: '', sizeBytes: null, note: '', noteIsError: false, ...over });
  assert.equal(d.buildAddonCard(view(null)), null, 'macOS and the rest: no card');
  assert.equal(d.buildAddonCard(undefined), null);

  const windows = d.buildAddonCard(view('windows'));
  assert.equal(windows.title, 'Optional add-on');
  assert.equal(windows.text, 'Keep donating when VS Code is closed, with DLNE Donate, a small tray app for Windows and Linux.');
  assert.match(windows.warning, /Windows may show "Windows protected your PC"\. Click More info, then Run anyway\./);
  assert.deepEqual(windows.button, { label: 'Download DLNE Donate', disabled: false });
  assert.equal(windows.pageLink, 'Or download it from the DLNE page');
  assert.equal(t.TRAY_PAGE_URL, 'https://dlne.rossgilsonlabs.dev/#dlne-tray', 'the site opens its tray app tab for #dlne-tray');

  const linux = d.buildAddonCard(view('linux', { sizeBytes: 57526776 }));
  assert.equal(linux.warning, '', 'SmartScreen is Windows only');
  assert.deepEqual(linux.button, { label: 'Download DLNE Donate (57.5 MB)', disabled: false }, 'the size, once a click has read it');
  assert.deepEqual(d.buildAddonCard(view('linux', { busy: 'Downloading...' })).button, { label: 'Downloading...', disabled: true });
  const failed = d.buildAddonCard(view('linux', { note: 'The download was not the published size, so it was deleted.', noteIsError: true }));
  assert.equal(failed.noteIsError, true);

  // The panel carries it, and leaves it out where there is none.
  const machine = d.donateMachine({ hasGpu: true, name: 'RTX 4060', vramTotalGb: 8, api: 'vulkan' }, 32 * GIB * 0.94, 8);
  const settings = { device: 'auto', maxVramGb: -1, maxRamGb: null, alsoCpuRamGb: 0, maxCpuThreads: -1, allowCodingMember: true };
  assert.deepEqual(d.buildDonatePanel({ machine, settings, state: 'idle', addon: view('windows') }).addon, windows);
  assert.equal(d.buildDonatePanel({ machine, settings, state: 'idle', addon: view(null) }).addon, null);
  assert.equal(d.buildDonatePanel({ machine, settings, state: 'idle' }).addon, null);
});

test('the Downloads folder: Windows\' own, the xdg-user-dirs one on Linux, then ~/Downloads, then home', () => {
  const machine = (files, dirs) => ({ readText: (f) => (f in files ? files[f] : null), isDirectory: (f) => dirs.includes(f) });
  assert.equal(t.downloadsDir('win32', 'C:\\Users\\Ann', { USERPROFILE: 'C:\\Users\\Ann' }, machine({}, ['C:\\Users\\Ann\\Downloads'])), 'C:\\Users\\Ann\\Downloads');
  assert.equal(t.downloadsDir('win32', 'C:\\Users\\Ann', { USERPROFILE: 'C:\\Users\\Ann' }, machine({}, [])), 'C:\\Users\\Ann', 'no Downloads folder: home');

  const dirs = '# written by xdg-user-dirs-update\nXDG_DESKTOP_DIR="$HOME/Bureau"\nXDG_DOWNLOAD_DIR="$HOME/Téléchargements"\n';
  const french = machine({ '/home/ann/.config/user-dirs.dirs': dirs }, ['/home/ann/Téléchargements', '/home/ann/Downloads']);
  assert.equal(t.downloadsDir('linux', '/home/ann', {}, french), '/home/ann/Téléchargements', 'the desktop\'s own name for it');
  const elsewhere = machine({ '/cfg/user-dirs.dirs': dirs }, ['/home/ann/Téléchargements']);
  assert.equal(t.downloadsDir('linux', '/home/ann', { XDG_CONFIG_HOME: '/cfg' }, elsewhere), '/home/ann/Téléchargements');
  assert.equal(t.downloadsDir('linux', '/home/ann', {}, machine({}, ['/home/ann/Downloads'])), '/home/ann/Downloads');
  assert.equal(t.downloadsDir('linux', '/home/ann', {}, machine({}, [])), '/home/ann');

  assert.equal(t.parseXdgDownloadDir('XDG_DOWNLOAD_DIR="/data/dl"', '/home/ann'), '/data/dl');
  assert.equal(t.parseXdgDownloadDir('XDG_DOWNLOAD_DIR="$HOME"', '/home/ann'), '/home/ann');
  assert.equal(t.parseXdgDownloadDir('XDG_DOWNLOAD_DIR="Downloads"', '/home/ann'), null, 'relative: not allowed by the format');
  assert.equal(t.parseXdgDownloadDir('XDG_DOWNLOAD_DIR="$OTHER/dl"', '/home/ann'), null);
  assert.equal(t.parseXdgDownloadDir('# XDG_DOWNLOAD_DIR="/data/dl"', '/home/ann'), null);
  assert.equal(t.parseXdgDownloadDir(null, '/home/ann'), null);
});

test('Run starts the app on its own, with its window open, without VS Code\'s own variables', () => {
  const realSpawn = childProcess.spawn;
  const spawned = [];
  let unrefs = 0;
  childProcess.spawn = (...args) => { spawned.push(args); return { unref() { unrefs++; } }; };
  process.env.ELECTRON_RUN_AS_NODE = '1';
  process.env.VSCODE_PID = '4242';
  try {
    t.launchTrayApp('/home/ann/Downloads/DLNE-Donate-1.0.0-x86_64.AppImage');
  } finally {
    childProcess.spawn = realSpawn;
    delete process.env.ELECTRON_RUN_AS_NODE;
    delete process.env.VSCODE_PID;
  }
  assert.equal(spawned.length, 1);
  const [file, args, options] = spawned[0];
  assert.equal(file, '/home/ann/Downloads/DLNE-Donate-1.0.0-x86_64.AppImage');
  assert.deepEqual(args, ['--settings'], 'its window opens, where the owner chooses and clicks Donate');
  assert.equal(options.detached, true, 'it keeps running when VS Code closes');
  assert.equal(options.stdio, 'ignore', 'no pipe ties it to the editor');
  assert.equal(options.cwd, '/home/ann/Downloads');
  assert.equal(options.windowsHide, false);
  assert.equal(options.shell, undefined, 'no shell');
  assert.equal(options.env.ELECTRON_RUN_AS_NODE, undefined);
  assert.equal(options.env.VSCODE_PID, undefined);
  assert.equal(options.env.HOME, process.env.HOME, 'the rest of the environment is the owner\'s');
  assert.equal(unrefs, 1);
});

// ---- recognising the tray app's worker ------------------------------------------------------------------------------

// Command lines as `pgrep -af` printed them while the 26 Oct builds donated, and the ones that must not count.
const TRAY_WORKERS = [
  '/tmp/.mount_DLNE-DocNLdL/usr/bin/DLNE-Donate/DLNE-Donate --run-worker /home/ann/.local/share/dlne-donate/engine -- worker --coordinator https://dlne.rossgilsonlabs.dev',
  '/opt/DLNE-Donate/DLNE-Donate --run-worker /home/ann/My Data/dlne-donate/engine -- worker --coordinator https://dlne.rossgilsonlabs.dev',
  '/usr/bin/python3 -m dlne_donate --run-worker /home/ann/.local/share/dlne-donate/engine -- worker --coordinator https://dlne.rossgilsonlabs.dev',
];
const NOT_TRAY_WORKERS = [
  '/tmp/.mount_DLNE-DocNLdL/usr/bin/DLNE-Donate/DLNE-Donate --settings', // the app itself, not donating
  '/tmp/.mount_DLNE-DocNLdL/usr/bin/DLNE-Donate/DLNE-Donate -B -S -I -c from multiprocessing.resource_tracker import main;main(9)',
  '/home/ann/.dlne/runtime/.venv/bin/python /home/ann/.dlne/runtime/dlne_server.py worker --coordinator https://dlne.rossgilsonlabs.dev',
  '/home/ann/.local/share/dlne-donate/engine/bin/llama-server -m /home/ann/.local/share/dlne-donate/engine/models/Qwen3-4B.gguf',
  `pgrep -f ${t.TRAY_WORKER_PATTERN}`, // the check itself, as another editor's check would see it
];

test('the tray app\'s worker command line is recognised, and nothing else is', () => {
  const pattern = new RegExp(t.TRAY_WORKER_PATTERN);
  for (const line of TRAY_WORKERS) assert.match(line, pattern);
  for (const line of NOT_TRAY_WORKERS) assert.doesNotMatch(line, pattern);
  // What the older check looks for never matches it: that is why the tray app's worker was missed.
  for (const line of TRAY_WORKERS) assert.doesNotMatch(line, /ggml-rpc-server|dlne_server.py worker/);
});

test('as a POSIX extended regular expression (pgrep\'s kind) the pattern means the same', { skip: !hasProgram('grep') || !hasProgram('pgrep') }, () => {
  const count = (lines) => {
    const done = childProcess.spawnSync('grep', ['-E', '-c', '--', t.TRAY_WORKER_PATTERN], { input: lines.join('\n') + '\n', encoding: 'utf8' });
    return Number(done.stdout.trim());
  };
  assert.equal(count(TRAY_WORKERS), TRAY_WORKERS.length);
  assert.equal(count(NOT_TRAY_WORKERS), 0);
  // pgrep takes it (exit 1: nothing matched, 0: something did; 2 would be a pattern it cannot read).
  const status = childProcess.spawnSync('pgrep', ['-f', t.TRAY_WORKER_PATTERN]).status;
  assert.ok(status === 0 || status === 1, `pgrep exited ${status}`);
});

test('the app\'s own folder and state.json are found and read as the app writes them', () => {
  assert.equal(t.trayAppDataDir('win32', { LOCALAPPDATA: 'C:\\Users\\Ann\\AppData\\Local' }, 'C:\\Users\\Ann'), 'C:\\Users\\Ann\\AppData\\Local\\DLNE Donate');
  assert.equal(t.trayAppDataDir('win32', {}, 'C:\\Users\\Ann'), 'C:\\Users\\Ann\\AppData\\Local\\DLNE Donate');
  assert.equal(t.trayAppDataDir('linux', { XDG_DATA_HOME: '/data' }, '/home/ann'), '/data/dlne-donate');
  assert.equal(t.trayAppDataDir('linux', {}, '/home/ann'), '/home/ann/.local/share/dlne-donate');
  assert.equal(t.trayAppDataDir('linux', { DLNE_DONATE_HOME: '/srv/donate' }, '/home/ann'), '/srv/donate');

  const state = (fields) => JSON.stringify({ message: 'Donating.', role: '', model: '', also_cpu: false, error: '', ...fields });
  for (const s of ['preparing', 'starting', 'donating', 'stopping']) {
    assert.deepEqual(t.parseTrayState(state({ state: s, pid: 4242 })), { active: true, pid: 4242 }, s);
  }
  for (const s of ['idle', 'error']) assert.deepEqual(t.parseTrayState(state({ state: s, pid: 4242 })), { active: false, pid: 4242 });
  for (const junk of [null, '', 'not json', '[]', state({ state: 'donating' }), state({ state: 'donating', pid: '4242' }), state({ state: 'donating', pid: -1 }), state({ pid: 4242 })]) {
    assert.equal(t.parseTrayState(junk), null, String(junk));
  }

  assert.equal(t.isTrayAppCommandLine('/tmp/.mount_DLNE-x/usr/bin/DLNE-Donate/DLNE-Donate\0--settings\0'), true);
  assert.equal(t.isTrayAppCommandLine('/usr/bin/python3\0-m\0dlne_donate\0'), true);
  assert.equal(t.isTrayAppCommandLine('/usr/sbin/sshd\0-D\0'), false);
  assert.equal(t.isTrayAppCommandLine(''), false);
  assert.equal(t.tasklistImage('"System","4","Services","0","144 K"\r\n"DLNE-Donate-1.0.0.exe","4242","Console","1","45,120 K"\r\n', 4242), 'DLNE-Donate-1.0.0.exe');
  assert.equal(t.tasklistImage('INFO: No tasks are running which match the specified criteria.\r\n', 4242), null);
});

/** A machine for trayWorkerRunning: files it holds, what its programs answer, and every call made. */
function machineWith({ files = {}, programs = {} } = {}) {
  const calls = [];
  return {
    calls,
    readText(file) { calls.push(['read', file]); return file in files ? files[file] : null; },
    run(program, args) { calls.push(['run', program, args]); return program in programs ? programs[program](args) : null; },
  };
}
const STATE_LINUX = '/home/ann/.local/share/dlne-donate/state.json';
const STATE_WINDOWS = 'C:\\Users\\Ann\\AppData\\Local\\DLNE Donate\\state.json';
const donating = (pid = 4242) => JSON.stringify({ state: 'donating', pid });

test('Linux: the tray app\'s worker is found from its state.json, or from its command line', () => {
  const env = {};
  const appCmdline = '/tmp/.mount_DLNE-x/usr/bin/DLNE-Donate/DLNE-Donate\0--settings\0';
  const fromState = machineWith({ files: { [STATE_LINUX]: donating(), '/proc/4242/cmdline': appCmdline } });
  assert.equal(t.trayWorkerRunning('linux', fromState, env, '/home/ann'), true);
  assert.ok(!fromState.calls.some(([kind]) => kind === 'run'), 'the state file said it: no pgrep needed');

  // A crash left "donating" behind and the process number now belongs to something else.
  const stale = machineWith({ files: { [STATE_LINUX]: donating(), '/proc/4242/cmdline': '/usr/sbin/sshd\0-D\0' } });
  assert.equal(t.trayWorkerRunning('linux', stale, env, '/home/ann'), false);
  const gone = machineWith({ files: { [STATE_LINUX]: donating() } });
  assert.equal(t.trayWorkerRunning('linux', gone, env, '/home/ann'), false);
  const idle = machineWith({ files: { [STATE_LINUX]: JSON.stringify({ state: 'idle', pid: 4242 }), '/proc/4242/cmdline': appCmdline } });
  assert.equal(t.trayWorkerRunning('linux', idle, env, '/home/ann'), false);
  assert.ok(!idle.calls.some(([, file]) => file === '/proc/4242/cmdline'), 'an idle app is not looked up');

  // An app that keeps its files elsewhere: its worker's command line is enough.
  const pgrep = machineWith({ programs: { pgrep: () => '31337\n' } });
  assert.equal(t.trayWorkerRunning('linux', pgrep, env, '/home/ann'), true);
  assert.deepEqual(pgrep.calls.find(([kind]) => kind === 'run'), ['run', 'pgrep', ['-f', t.TRAY_WORKER_PATTERN]]);
});

test('Windows: the state.json says donating and tasklist says that process is DLNE Donate', () => {
  const env = { LOCALAPPDATA: 'C:\\Users\\Ann\\AppData\\Local' };
  const listed = (image) => () => `"${image}","4242","Console","1","45,120 K"\r\n`;
  const yes = machineWith({ files: { [STATE_WINDOWS]: donating() }, programs: { tasklist: listed('DLNE-Donate-1.0.0.exe') } });
  assert.equal(t.trayWorkerRunning('win32', yes, env, 'C:\\Users\\Ann'), true);
  assert.deepEqual(yes.calls.filter(([kind]) => kind === 'run'), [['run', 'tasklist', ['/fi', 'PID eq 4242', '/fo', 'csv', '/nh']]]);
  assert.equal(t.trayWorkerRunning('win32', machineWith({ files: { [STATE_WINDOWS]: donating() }, programs: { tasklist: listed('DLNE-Donate.exe') } }), env, 'C:\\Users\\Ann'), true);

  // After a power cut the old process number may belong to another program, or to nothing.
  const reused = machineWith({ files: { [STATE_WINDOWS]: donating() }, programs: { tasklist: listed('svchost.exe') } });
  assert.equal(t.trayWorkerRunning('win32', reused, env, 'C:\\Users\\Ann'), false);
  const none = machineWith({ files: { [STATE_WINDOWS]: donating() }, programs: { tasklist: () => 'INFO: No tasks are running which match the specified criteria.\r\n' } });
  assert.equal(t.trayWorkerRunning('win32', none, env, 'C:\\Users\\Ann'), false);
  // Not donating, or no app at all: no program is run.
  const idle = machineWith({ files: { [STATE_WINDOWS]: JSON.stringify({ state: 'idle', pid: 4242 }) }, programs: { tasklist: listed('DLNE-Donate.exe') } });
  assert.equal(t.trayWorkerRunning('win32', idle, env, 'C:\\Users\\Ann'), false);
  const nothing = machineWith();
  assert.equal(t.trayWorkerRunning('win32', nothing, env, 'C:\\Users\\Ann'), false);
  for (const m of [idle, nothing]) assert.ok(!m.calls.some(([kind]) => kind === 'run'), 'no tasklist, no pgrep');
});

test('macOS has no tray app: nothing is looked at', () => {
  const m = machineWith({ programs: { pgrep: () => '1\n' } });
  assert.equal(t.trayWorkerRunning('darwin', m, {}, '/Users/ann'), false);
  assert.deepEqual(m.calls, []);
});

test('the real probe runs its programs hidden and without a shell, and will not read a big file', () => {
  const realExecFile = childProcess.execFileSync;
  const runs = [];
  childProcess.execFileSync = (file, args, options) => { runs.push({ file, args, options }); return 'ok'; };
  try {
    assert.equal(t.realTrayProbe.run('tasklist', ['/fi', 'PID eq 4242', '/fo', 'csv', '/nh']), 'ok');
  } finally {
    childProcess.execFileSync = realExecFile;
  }
  assert.equal(runs[0].options.windowsHide, true, 'tasklist would flash a console window every few seconds');
  assert.equal(runs[0].options.shell, undefined);

  const small = path.join(HOME, 'state.json');
  fs.writeFileSync(small, donating());
  assert.equal(t.realTrayProbe.readText(small), donating());
  const big = path.join(HOME, 'big.json');
  fs.writeFileSync(big, 'x'.repeat(65 * 1024));
  assert.equal(t.realTrayProbe.readText(big), null);
  assert.equal(t.realTrayProbe.readText(path.join(HOME, 'missing.json')), null);
  assert.equal(t.realTrayProbe.run('dlne-no-such-program', []), null);
});

// ---- the worker manager -------------------------------------------------------------------------------------------

/** A manager whose machine-wide checks say what the test wants, already in place while it is constructed. */
function manager({ tray = false, other = false } = {}) {
  stub.reset();
  Object.assign(stub.state.settings, {
    'dlne.worker.device': 'cpu', 'dlne.worker.maxRamGb': 16, 'dlne.worker.pythonPath': process.execPath,
    'dlne.autoUpdate': false, 'dlne.serverUrl': 'http://127.0.0.1:9',
  });
  const proto = WorkerManager.prototype;
  const real = { sysd: proto.isSystemdActive, proc: proto.isProcessActive, tray: proto.isTrayWorkerActive };
  proto.isSystemdActive = () => false;
  proto.isProcessActive = () => other;
  proto.isTrayWorkerActive = () => tray;
  try {
    const wm = new WorkerManager({ subscriptions: [], globalState: undefined });
    wm.isSystemdActive = () => false;
    wm.isProcessActive = () => other;
    wm.isTrayWorkerActive = () => tray;
    wm.isPortListening = async () => false;
    wm.localGpu = { hasGpu: false, name: 'CPU Compute Node', vramTotalGb: 4, api: 'cpu' };
    return wm;
  } finally {
    proto.isSystemdActive = real.sysd;
    proto.isProcessActive = real.proc;
    proto.isTrayWorkerActive = real.tray;
  }
}

test('a running DLNE Donate worker is shown as the tray app\'s, and VS Code starts no second worker', async () => {
  const wm = manager({ tray: true });
  const realSpawn = childProcess.spawn;
  const spawned = [];
  childProcess.spawn = (...args) => { spawned.push(args); return realSpawn(...args); };
  try {
    const st = wm.getStatus();
    assert.deepEqual([st.state, st.external, st.trayApp, st.message], ['contributing', true, true, c.TRAY_WORKER_LINE], 'found at start-up');
    assert.equal(await wm.startWorker(), true);
    assert.equal(spawned.length, 0, 'no second worker beside it');
  } finally {
    childProcess.spawn = realSpawn;
    wm.dispose();
  }
  assert.ok(stub.state.infoMessages.includes(`DLNE: ${c.TRAY_WORKER_TEXT}`), 'the owner is told whose worker it is');
  assert.ok(c.TRAY_WORKER_TEXT.startsWith('Donating from the DLNE Donate tray app'));
});

test('Stop does not touch the tray app\'s worker: the owner is told where to stop it', async () => {
  const wm = manager({ tray: true });
  const stops = [];
  wm.stopWorker = async () => { stops.push(1); };
  try {
    assert.equal(await wm.toggle(), true, 'still donating, from the tray app');
    assert.deepEqual(stops, []);
    assert.ok(stub.state.infoMessages.includes(`DLNE: ${c.TRAY_WORKER_TEXT}`));
    assert.equal(wm.getStatus().state, 'contributing');
  } finally {
    wm.dispose();
  }
  // Another outside worker is stopped as before.
  const other = manager({ other: true });
  other.stopWorker = async () => { stops.push('other'); };
  try {
    assert.equal(await other.toggle(), false);
    assert.deepEqual(stops, ['other']);
  } finally {
    other.dispose();
  }
});

test('the status follows the tray app: found later, named, told apart from other workers, and dropped when it stops', async () => {
  const wm = manager();
  try {
    assert.equal(wm.getStatus().state, 'idle');
    wm.isTrayWorkerActive = () => true;
    await wm.refreshWorkerState();
    let st = wm.getStatus();
    assert.deepEqual([st.state, st.external, st.trayApp, st.message], ['contributing', true, true, c.TRAY_WORKER_LINE]);

    // The tray app stopped but a terminal worker runs: still outside VS Code, no longer the tray app's.
    wm.isTrayWorkerActive = () => false;
    wm.isProcessActive = () => true;
    await wm.refreshWorkerState();
    st = wm.getStatus();
    assert.deepEqual([st.external, st.trayApp, st.message], [true, undefined, c.EXTERNAL_WORKER_LINE]);

    wm.isProcessActive = () => false;
    for (let i = 0; i < 3; i++) await wm.refreshWorkerState();
    st = wm.getStatus();
    assert.deepEqual([st.state, st.external, st.trayApp], ['idle', undefined, undefined]);
  } finally {
    wm.dispose();
  }
});

test('the Donate panel says "Donating from the DLNE Donate tray app", and its button cannot stop it', () => {
  const machine = d.donateMachine({ hasGpu: true, name: 'RTX 4060', vramTotalGb: 8, api: 'vulkan' }, 32 * GIB * 0.94, 8);
  const settings = { device: 'auto', maxVramGb: -1, maxRamGb: null, alsoCpuRamGb: 0, maxCpuThreads: -1, allowCodingMember: true };
  const panel = d.buildDonatePanel({ machine, settings, state: 'contributing', external: true, trayApp: true });
  assert.equal(panel.locked, true, 'its settings are not these');
  assert.match(panel.lockedNote, /^Donating from the DLNE Donate tray app/);
  assert.deepEqual(panel.button, { label: 'Donating from the tray app', action: 'stop', disabled: true, hint: c.TRAY_WORKER_STOP });
  assert.match(c.TRAY_WORKER_STOP, /Stop donating in DLNE Donate/, 'the app\'s own label');
  assert.equal(panel.apply.visible, false);

  // Any other outside worker keeps its Stop button.
  const other = d.buildDonatePanel({ machine, settings, state: 'contributing', external: true });
  assert.deepEqual([other.button.label, other.button.disabled], ['Stop donating', false]);
  assert.match(other.lockedNote, /started outside VS Code/);

  // The card's status line and the status bar's tooltip name it too.
  const view = { state: 'contributing', cpuPool: false, vramContributedGb: 0, external: true, trayApp: true, gpu: { name: 'RTX 4060', api: 'vulkan', vramTotalGb: 8 } };
  assert.match(c.contributionCopy(view).deviceHtml, /DLNE Donate/);
  assert.match(c.contributionCopy(view).ruleHtml, /DLNE Donate, the tray app, is donating this computer/);
  assert.match(c.contributionTooltip(view).join('\n'), /DLNE DONATE TRAY APP/);
});

// ---- the add-on's button ---------------------------------------------------------------------------------------------

/** Answers the editor's information messages in turn (the modal question, then "Run it now?"), recording each. */
function answering(...answers) {
  const asked = [];
  const real = stub.vscode.window.showInformationMessage;
  stub.vscode.window.showInformationMessage = async (message, ...rest) => {
    asked.push({ message, rest });
    return answers.shift();
  };
  return { asked, restore: () => { stub.vscode.window.showInformationMessage = real; } };
}

/** A TrayAddon on `platform`, whose website serves `bytes` as the build; every change is recorded as the card's view. */
function addonOn(platform, bytes, { site, manifest, launch, fetchManifest } = {}) {
  const folder = freshFolder();
  const file = platform === 'windows' ? 'DLNE-Donate-1.0.0.exe' : 'DLNE-Donate-1.0.0-x86_64.AppImage';
  const build = buildOf(bytes, file);
  const served = site || website(bytes);
  const fetches = [];
  const launches = [];
  const views = [];
  const deps = {
    platform: () => platform,
    fetchManifest: fetchManifest || (async () => { fetches.push(1); return manifest || { versionName: '1.0.0', notes: '', published: null, builds: { [platform]: build } }; }),
    downloadsDir: () => folder,
    save: (b, target, options) => t.saveVerifiedBuild(b, target, { ...options, open: served }),
    launch: (f) => { launches.push(f); return launch ? launch(f) : Object.assign(new EventEmitter(), { unref() {} }); },
  };
  const addon = new TrayAddon(() => views.push(addon.view()), deps);
  return { addon, folder, build, target: path.join(folder, file), fetches, launches, views, site: served };
}

test('nothing is fetched until the button is clicked', () => {
  const a = addonOn('linux', Buffer.from('x'));
  const machine = d.donateMachine(undefined, 32 * GIB * 0.94, 8);
  const settings = { device: 'auto', maxVramGb: -1, maxRamGb: null, alsoCpuRamGb: 0, maxCpuThreads: -1, allowCodingMember: true };
  for (let i = 0; i < 3; i++) d.buildDonatePanel({ machine, settings, state: 'idle', addon: a.addon.view() });
  assert.deepEqual(a.fetches, []);
  assert.deepEqual(a.site.urls, []);

  // The sidebar draws the card and makes no request; only its message starts the flow.
  stub.reset();
  const posted = [];
  let onMessage;
  const status = { state: 'idle', gpu: { hasGpu: false, name: 'CPU Compute Node', vramTotalGb: 4, api: 'cpu' }, cpuPool: true, vramContributedGb: 0 };
  const provider = new SidebarProvider({ fsPath: ROOT }, {}, { onStatusChange() {}, getStatus: () => status });
  const clicks = [];
  provider.trayAddon = { view: () => ({ platform: 'linux', busy: '', sizeBytes: null, note: '', noteIsError: false }), download: async () => { clicks.push(1); } };
  provider.resolveWebviewView(
    { webview: { options: {}, html: '', asWebviewUri: (u) => u, postMessage: (m) => posted.push(m), onDidReceiveMessage: (fn) => { onMessage = fn; } } }, {}, {});
  assert.equal(posted[0].donate.addon.title, 'Optional add-on');
  assert.deepEqual(clicks, []);
  onMessage({ type: 'trayAddon' });
  assert.deepEqual(clicks, [1]);
});

test('a click: the manifest, a question with the size, a verified download, then "Run it now?"', async () => {
  const bytes = crypto.randomBytes(200 * 1024);
  const a = addonOn('linux', bytes);
  const size = t.formatSize(bytes.length);
  const ui = answering(`Download (${size})`, 'Show file');
  try {
    await a.addon.download();
  } finally {
    ui.restore();
  }
  assert.equal(a.fetches.length, 1);
  const [question, runNow] = ui.asked;
  assert.equal(question.message, `Download DLNE Donate 1.0.0 for Linux (${size})?`);
  assert.equal(question.rest[0].modal, true);
  assert.match(question.rest[0].detail, new RegExp(`It is saved in ${a.folder.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\.`));
  assert.match(question.rest[0].detail, /nothing runs unless you choose Run/);
  assert.deepEqual(question.rest.slice(1), [`Download (${size})`]);
  assert.equal(runNow.message, 'DLNE Donate is saved. Run it now?');
  assert.deepEqual(runNow.rest, ['Run', 'Show file']);

  assert.ok(fs.readFileSync(a.target).equals(bytes), 'saved, verified, under its own name');
  assert.deepEqual(fs.readdirSync(a.folder), [path.basename(a.target)]);
  assert.deepEqual(a.launches, [], 'Show file runs nothing');
  assert.deepEqual(stub.state.commands.at(-1)[0], 'revealFileInOS');
  assert.equal(stub.state.commands.at(-1)[1].fsPath, a.target);

  // The card went: checking, the size known, downloading, saved.
  assert.deepEqual(a.views.map((v) => v.busy).filter((b, i, all) => b !== all[i - 1]), ['Checking...', '', 'Downloading...', '']);
  assert.ok(a.views.some((v) => v.sizeBytes === bytes.length && v.busy === ''), 'the button showed the size');
  const last = a.views.at(-1);
  assert.deepEqual([last.note, last.noteIsError], [`Saved ${path.basename(a.target)} in ${a.folder}.`, false]);
});

test('Run starts it, and a start that fails is said plainly', async () => {
  stub.reset();
  const bytes = crypto.randomBytes(100 * 1024);
  const size = t.formatSize(bytes.length);

  // It starts and keeps running.
  let child = Object.assign(new EventEmitter(), { unref() {} });
  let a = addonOn('linux', bytes, { launch: () => child });
  let ui = answering(`Download (${size})`, 'Run');
  try {
    await a.addon.download();
  } finally {
    ui.restore();
  }
  assert.deepEqual(a.launches, [a.target]);
  assert.match(a.views.at(-1).note, /DLNE Donate is starting\. Choose what to lend in its window, then click Donate\./);
  child.emit('exit', 0, null);   // a second launch hands over to the running app and exits 0: fine
  assert.deepEqual(stub.state.errorMessages, []);

  // It closes at once (on Linux, an AppImage the system cannot run).
  stub.reset();
  child = Object.assign(new EventEmitter(), { unref() {} });
  a = addonOn('linux', bytes, { launch: () => child });
  ui = answering(`Download (${size})`, 'Run');
  try {
    await a.addon.download();
  } finally {
    ui.restore();
  }
  child.emit('exit', 127, null);
  child.emit('error', new Error('spawn EACCES'));
  assert.equal(stub.state.errorMessages.length, 1, 'said once');
  assert.equal(stub.state.errorMessages[0].message, 'DLNE: DLNE Donate did not start (it closed at once, exit code 127).');
  assert.deepEqual(stub.state.errorMessages[0].items, ['Show file']);
  assert.deepEqual([a.views.at(-1).note, a.views.at(-1).noteIsError], ['DLNE Donate did not start (it closed at once, exit code 127).', true]);

  // It cannot be started at all.
  stub.reset();
  a = addonOn('linux', bytes, { launch: () => { throw new Error('spawn EACCES'); } });
  ui = answering(`Download (${size})`, 'Run');
  try {
    await a.addon.download();
  } finally {
    ui.restore();
  }
  assert.equal(stub.state.errorMessages[0].message, 'DLNE: DLNE Donate did not start (spawn EACCES).');
});

test('saying no, a refused manifest, or no build for this computer downloads nothing', async () => {
  const bytes = crypto.randomBytes(100 * 1024);

  let a = addonOn('linux', bytes);
  let ui = answering(undefined);
  try {
    await a.addon.download();
  } finally {
    ui.restore();
  }
  assert.deepEqual(a.site.urls, [], 'closing the question downloads nothing');
  assert.deepEqual(fs.readdirSync(a.folder), []);
  assert.equal(a.views.at(-1).note, '');

  a = addonOn('linux', bytes, { fetchManifest: async () => { throw new t.TrayError('refused', 'linux url is not an official DLNE download'); } });
  ui = answering();
  try {
    await a.addon.download();
  } finally {
    ui.restore();
  }
  assert.deepEqual(ui.asked, [], 'no question is asked');
  assert.deepEqual(a.site.urls, []);
  assert.deepEqual([a.views.at(-1).note, a.views.at(-1).noteIsError], [
    'The download list was refused (linux url is not an official DLNE download). Nothing was downloaded.', true,
  ]);

  a = addonOn('linux', bytes, { manifest: { versionName: '1.0.0', notes: '', published: null, builds: { windows: buildOf(bytes, 'DLNE-Donate-1.0.0.exe') } } });
  ui = answering();
  try {
    await a.addon.download();
  } finally {
    ui.restore();
  }
  assert.equal(a.views.at(-1).note, 'There is no DLNE Donate download for Linux yet.');
  assert.deepEqual(a.site.urls, []);
});

test('a download that does not match is deleted, said so, and never offered to run', async () => {
  const bytes = crypto.randomBytes(100 * 1024);
  const a = addonOn('linux', bytes, { site: website(crypto.randomBytes(bytes.length)) });
  const ui = answering(`Download (${t.formatSize(bytes.length)})`, 'Run');
  try {
    await a.addon.download();
  } finally {
    ui.restore();
  }
  assert.deepEqual(fs.readdirSync(a.folder), []);
  assert.deepEqual([a.views.at(-1).note, a.views.at(-1).noteIsError], ['The download did not match its published SHA-256, so it was deleted.', true]);
  assert.equal(ui.asked.length, 1, 'no "Run it now?"');
  assert.deepEqual(a.launches, []);
});

test('Windows: the Run question also says how to get past SmartScreen', async () => {
  const bytes = crypto.randomBytes(100 * 1024);
  const a = addonOn('windows', bytes);
  const size = t.formatSize(bytes.length);
  const ui = answering(`Download (${size})`, undefined);
  try {
    await a.addon.download();
  } finally {
    ui.restore();
  }
  assert.equal(ui.asked[0].message, `Download DLNE Donate 1.0.0 for Windows (${size})?`);
  assert.equal(ui.asked[1].message, 'DLNE Donate is saved. Run it now? If Windows says "Windows protected your PC", click More info, then Run anyway.');
  assert.ok(fs.existsSync(a.target));
});

test('two quick clicks run one download', async () => {
  const bytes = crypto.randomBytes(100 * 1024);
  const a = addonOn('linux', bytes);
  const ui = answering(`Download (${t.formatSize(bytes.length)})`, undefined, `Download (${t.formatSize(bytes.length)})`, undefined);
  try {
    await Promise.all([a.addon.download(), a.addon.download()]);
  } finally {
    ui.restore();
  }
  assert.equal(a.fetches.length, 1);
  assert.equal(a.site.urls.length, 1);
});

// ---- the page --------------------------------------------------------------------------------------------------------

/** A page stand-in that records what the panel script does, and refuses innerHTML (as in donate-panel.test.js). */
function fakePage() {
  class El {
    constructor(tag) {
      Object.assign(this, { tag, children: [], style: {}, attrs: {}, listeners: {}, className: '', value: '', checked: false, disabled: false, type: '', min: '', max: '', _text: '' });
    }
    appendChild(child) { this.children.push(child); return child; }
    setAttribute(key, value) { this.attrs[key] = value; }
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
    set innerHTML(value) { throw new Error(`innerHTML used: ${value}`); }
    get textContent() { return this._text + this.children.map((child) => child.textContent).join(''); }
    set textContent(value) { this._text = String(value); this.children = []; }
    fire(type) { (this.listeners[type] || []).forEach((fn) => fn({})); }
    all() { return [this, ...this.children.flatMap((child) => child.all())]; }
  }
  global.document = { createElement: (tag) => new El(tag), activeElement: null };
  const root = new El('div');
  const posted = [];
  const panel = createDonatePanel(root, (message) => posted.push(message));
  return {
    root, posted, panel,
    find: (cls) => root.all().find((el) => el.className.split(' ').includes(cls)),
  };
}

test('the panel script draws the add-on card as text and posts only the click', () => {
  const machine = d.donateMachine({ hasGpu: true, name: 'RTX 4060', vramTotalGb: 8, api: 'vulkan' }, 32 * GIB * 0.94, 8);
  const settings = { device: 'auto', maxVramGb: -1, maxRamGb: null, alsoCpuRamGb: 0, maxCpuThreads: -1, allowCodingMember: true };
  const model = (addon) => d.buildDonatePanel({ machine, settings, state: 'idle', addon });
  const page = fakePage();
  try {
    page.panel.update(model({ platform: 'windows', busy: '', sizeBytes: 34348456, note: '', noteIsError: false }));
    const card = page.find('dn-addon');
    assert.equal(card.style.display, 'flex');
    assert.ok(card.textContent.includes('Optional add-on'));
    assert.ok(card.textContent.includes(d.ADDON_TEXT));
    assert.ok(card.textContent.includes('Click More info, then Run anyway.'));
    const button = page.find('dn-addon-btn');
    assert.equal(button.textContent, 'Download DLNE Donate (34.3 MB)');
    button.fire('click');
    assert.deepEqual(page.posted, [{ type: 'trayAddon' }], 'only the click: the extension does the rest');
    const link = page.find('dn-addon-link');
    assert.equal(link.textContent, 'Or download it from the DLNE page');
    link.fire('click');
    assert.deepEqual(page.posted[1], { type: 'trayPage' }, 'no address in the message: the extension opens its own');
    page.posted.pop();

    // Busy: the button is disabled and posts nothing; a failure is shown as a warning.
    page.panel.update(model({ platform: 'windows', busy: 'Downloading...', sizeBytes: 34348456, note: 'x', noteIsError: true }));
    assert.equal(button.disabled, true);
    button.fire('click');
    assert.equal(page.posted.length, 1);
    assert.equal(page.find('dn-addon-note').className, 'dn-addon-note dn-addon-error');

    // Linux: no SmartScreen line. macOS and the rest: no card.
    page.panel.update(model({ platform: 'linux', busy: '', sizeBytes: null, note: '', noteIsError: false }));
    assert.equal(page.find('dn-addon-warn').style.display, 'none');
    page.panel.update(model({ platform: null, busy: '', sizeBytes: null, note: '', noteIsError: false }));
    assert.equal(card.style.display, 'none');
  } finally {
    delete global.document;
  }
});

// ---- no other traffic ------------------------------------------------------------------------------------------------

test('the add-on reaches nothing but the DLNE website, and sends nothing about the owner', () => {
  for (const file of ['trayApp.ts', 'trayAddon.ts']) {
    const source = fs.readFileSync(path.join(ROOT, 'src', 'worker', file), 'utf8');
    const urls = source.match(/\bhttps?:\/\/[^\s'"`)]+/g) || [];
    for (const url of urls) assert.match(url, /^https:\/\/(dlne\.rossgilsonlabs\.dev|\$\{TRAY_HOST\})(\/|$)/, `${file}: ${url}`);
    assert.doesNotMatch(source, /\.post\(|method:\s*'POST'|https\.request\(/, `${file} sends nothing`);
  }
});

test('the page link opens the fixed DLNE page, whatever the message says', () => {
  const source = fs.readFileSync(path.join(ROOT, 'src', 'views', 'sidebarProvider.ts'), 'utf8');
  const handler = source.slice(source.indexOf("case 'trayPage'"), source.indexOf('break;', source.indexOf("case 'trayPage'")));
  assert.match(handler, /vscode\.env\.openExternal\(vscode\.Uri\.parse\(TRAY_PAGE_URL\)\)/);
  assert.doesNotMatch(handler, /data\./, 'nothing from the webview message is used');
});

test('the changelog and the README describe the add-on', () => {
  const changelog = fs.readFileSync(path.join(ROOT, 'CHANGELOG.md'), 'utf8');
  const unreleased = changelog.slice(changelog.indexOf('## [1.4.0]'), changelog.indexOf('## [1.3.5]'));
  assert.match(unreleased, /DLNE Donate/);
  assert.match(unreleased, /SHA-256/);
  assert.match(fs.readFileSync(path.join(ROOT, 'README.md'), 'utf8'), /\*\*Optional add-on\*\*/);
});

function hasProgram(name) {
  return childProcess.spawnSync('sh', ['-c', `command -v ${name}`]).status === 0;
}
