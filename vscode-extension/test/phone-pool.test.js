'use strict';
/**
 * A CPU contributor powers the DLNE phone app (the CPU pool, coordinator 2.3.0): the modal that
 * asks how much RAM to lend, the sidebar, the status bar, and the link to the phone app.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const stub = require('./helpers/vscode-stub');
const { WorkerManager } = require('../dist/worker/workerManager');
const { SidebarProvider } = require('../dist/views/sidebarProvider');
const { ClusterStatusBar } = require('../dist/statusbar/clusterStatusBar');
const { refusalNotice } = require('../dist/agent/agentLoop');
const { phoneChatUrl, DEFAULT_SERVER_URL } = require('../dist/serverUrl');
const pkg = require('../package.json');

const ROOT = path.join(__dirname, '..');
const GIB = 1024 ** 3;
const NOT_FOR_CPU = /unlimited|frontier|coding model|inference unlocked|contribute local gpu|vulkan/i;

const CPU_NODE = { hasGpu: false, name: 'CPU Compute Node', vramTotalGb: 4, api: 'cpu' };
const RTX = { hasGpu: true, name: 'RTX 4060', vramTotalGb: 8, api: 'vulkan' };

/** A WorkerManager on a machine with this GPU and this much RAM (as os.totalmem() reports it, in GiB). */
function machine(gpu, totalGib, settings = {}) {
  stub.reset();
  Object.assign(stub.state.settings, settings);
  const realTotalmem = os.totalmem;
  os.totalmem = () => totalGib * GIB;
  const wm = new WorkerManager({ subscriptions: [] });
  wm.localGpu = gpu; // whatever the host really has, the construction probe found
  // Nor what its pgrep found: a fake worker another test file runs at the same moment is not this machine's.
  wm.currentState = 'idle';
  wm.externalWorker = false;
  return { wm, done: () => { os.totalmem = realTotalmem; wm.dispose(); } };
}

/** Starts the plan on such a machine, the user clicking `choice` in the modal (undefined: closing it). */
async function plan(gpu, totalGib, choice, settings) {
  const { wm, done } = machine(gpu, totalGib, settings);
  stub.state.warningChoice = choice;
  try {
    return { result: await wm.planThisMachine(), wm, warnings: stub.state.warningMessages, updates: stub.state.updates };
  } finally {
    done();
  }
}

// ---- the modal ----------------------------------------------------------------------------

test('a 32 GB machine without a GPU is offered 16 GB first, and 8 GB', async () => {
  const { result, warnings, updates } = await plan(CPU_NODE, 31.2, 'Lend 16 GB (recommended)');
  assert.equal(warnings.length, 1);
  const [options, ...buttons] = warnings[0].items;
  assert.equal(options.modal, true);
  assert.deepEqual(buttons, ['Lend 16 GB (recommended)', 'Lend 8 GB']);
  assert.match(warnings[0].message, /^DLNE: This machine has no usable GPU\. Your computer powers the DLNE phone app/);
  assert.match(options.detail, /^16 GB: runs the main phone model, Qwen3\.6$/m);
  assert.match(options.detail, /^8 GB: runs Qwen3 4B, the fast overflow model$/m);
  assert.match(options.detail, /coding agent 2nd in line, with 300 chats every 5 hours/);
  assert.match(options.detail, /low priority on half your cores \(2 to 8 threads\)/);
  assert.equal(result.cpuOnly, true);
  assert.deepEqual(updates, [['dlne.worker.maxRamGb', 16]]);
});

test('the owner can pick a smaller amount than the recommended one', async () => {
  const { result, updates } = await plan(CPU_NODE, 62.5, 'Lend 8 GB');
  assert.ok(result);
  assert.deepEqual(updates, [['dlne.worker.maxRamGb', 8]]);
});

test('a 48 GB machine is offered the 24 GB step too', async () => {
  const { warnings } = await plan(CPU_NODE, 46.8, 'Lend 24 GB (recommended)');
  assert.deepEqual(warnings[0].items.slice(1), ['Lend 24 GB (recommended)', 'Lend 16 GB', 'Lend 8 GB']);
});

test('a 16 GB laptop (15.5 GiB reported) is offered 8 GB, half its RAM, with no "recommended" among one choice', async () => {
  const { result, warnings, updates } = await plan(CPU_NODE, 15.5, 'Lend 8 GB');
  assert.deepEqual(warnings[0].items.slice(1), ['Lend 8 GB']);
  assert.ok(result);
  assert.deepEqual(updates, [['dlne.worker.maxRamGb', 8]]);
});

test('closing the modal starts nothing and changes no setting', async () => {
  const { result, wm, updates } = await plan(CPU_NODE, 31.2, undefined);
  assert.equal(result, null);
  assert.deepEqual(updates, []);
  assert.match(wm.getStatus().message, /Not started/);
});

test('a machine with too little RAM is told why, with no modal, and nothing starts', async () => {
  const { result, warnings, updates } = await plan(CPU_NODE, 11.4, 'Lend 8 GB');
  assert.equal(result, null);
  assert.equal(warnings.length, 1);
  assert.equal(warnings[0].items.length, 0, 'a plain warning, not a modal with buttons');
  assert.match(warnings[0].message, /about 12 GB of RAM/);
  assert.match(warnings[0].message, /16 GB or more/);
  assert.deepEqual(updates, []);
});

test('a machine that already lends enough RAM is not asked', async () => {
  const { result, warnings, updates } = await plan(CPU_NODE, 15.5, undefined, { 'dlne.worker.maxRamGb': 8 });
  assert.ok(result);
  assert.equal(result.cpuOnly, true);
  assert.deepEqual(warnings, []);
  assert.deepEqual(updates, []);
});

test('choosing the CPU on a machine with a GPU says so in the modal', async () => {
  const { result, warnings } = await plan(RTX, 31.2, 'Lend 16 GB (recommended)', { 'dlne.worker.device': 'cpu' });
  assert.equal(result.cpuOnly, true);
  assert.equal(result.allowedGpus, 'none');
  assert.match(warnings[0].message, /^DLNE: You chose to contribute your CPU\. Your computer powers the DLNE phone app/);
});

test('a GPU contributor is never asked about RAM', async () => {
  const { result, warnings } = await plan(RTX, 31.2, undefined);
  assert.equal(result.cpuOnly, false);
  assert.deepEqual(warnings, []);
});

// ---- integrated GPUs --------------------------------------------------------------------------

const IGPU_NODE = { ...CPU_NODE, integratedGpu: 'Intel(R) UHD Graphics' };

test('a machine whose only GPU is a plain integrated one is asked as a CPU contributor, and says why', async () => {
  const { result, warnings } = await plan(IGPU_NODE, 31.2, 'Lend 16 GB (recommended)');
  assert.match(warnings[0].message, /^DLNE: This machine's only GPU \(Intel\(R\) UHD Graphics\) is an integrated one, which does not count as a GPU contributor\. Your computer powers the DLNE phone app/);
  assert.equal(result.cpuOnly, true);
  assert.equal(result.allowedGpus, 'none');
});

test('asking for the GPU on such a machine explains the rule instead of starting', async () => {
  const { result } = await plan(IGPU_NODE, 31.2, undefined, { 'dlne.worker.device': 'gpu' });
  assert.equal(result, null);
  assert.match(stub.state.errorMessages[0].message, /Intel\(R\) UHD Graphics is an integrated GPU/);
});

/** What detectLocalGpu finds when `vulkaninfo --summary` prints this (and there is no nvidia-smi). */
function probe(vulkanSummary) {
  stub.reset();
  const bin = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-vk-'));
  fs.writeFileSync(path.join(bin, 'vulkaninfo'), `#!/bin/sh\necho '${vulkanSummary}'\n`, { mode: 0o755 });
  const realPath = process.env.PATH;
  process.env.PATH = bin; // only the fake: no nvidia-smi, no system vulkaninfo, whatever the host has
  try {
    const wm = new WorkerManager({ subscriptions: [] });
    const found = wm.localGpu;
    wm.dispose();
    return found;
  } finally {
    process.env.PATH = realPath;
    fs.rmSync(bin, { recursive: true, force: true });
  }
}
const vk = (name, kind) => `Devices:\n========\nGPU0:\n\tdeviceType         = PHYSICAL_DEVICE_TYPE_${kind}_GPU\n\tdeviceName         = ${name}\n`;

test('detection: a plain integrated GPU is not a GPU, an Arc 140V or Strix Halo is', { skip: process.platform !== 'linux' }, () => {
  const uhd = probe(vk('Intel(R) UHD Graphics', 'INTEGRATED'));
  assert.equal(uhd.hasGpu, false);
  assert.equal(uhd.integratedGpu, 'Intel(R) UHD Graphics');
  assert.equal(uhd.api, 'cpu');
  assert.equal(probe(vk('AMD Radeon 780M', 'INTEGRATED')).hasGpu, false);
  const arc = probe(vk('Intel(R) Arc(TM) 140V GPU (16GB)', 'INTEGRATED'));
  assert.equal(arc.hasGpu, true);
  assert.equal(arc.name, 'Intel(R) Arc(TM) 140V GPU (16GB)');
  assert.equal(probe(vk('AMD Radeon(TM) 8060S Graphics', 'INTEGRATED')).hasGpu, true);
  assert.equal(probe(vk('NVIDIA GeForce RTX 4060', 'DISCRETE')).hasGpu, true);
  assert.equal(probe(vk('llvmpipe (LLVM 17.0.6, 256 bits)', 'CPU')).hasGpu, false);
});

// ---- what the status says ----------------------------------------------------------------

test('the status says whether this machine powers the phone app', () => {
  const cases = [
    [CPU_NODE, {}, true],                                    // no GPU: the CPU
    [RTX, {}, false],                                        // a GPU: the GPU
    [RTX, { 'dlne.worker.device': 'cpu' }, true],            // the owner keeps the GPU
    [CPU_NODE, { 'dlne.worker.device': 'gpu' }, false],      // asked for a GPU there is not
    [{ ...CPU_NODE, name: 'Probing GPU...' }, {}, false],    // not known yet
  ];
  for (const [gpu, settings, expected] of cases) {
    const { wm, done } = machine(gpu, 31.2, settings);
    try {
      assert.equal(wm.getStatus().cpuPool, expected, `${gpu.name} ${JSON.stringify(settings)}`);
    } finally {
      done();
    }
  }
});

test('a running CPU contributor reports the RAM it lends and says what it powers', () => {
  const { wm, done } = machine(CPU_NODE, 31.2, { 'dlne.worker.maxRamGb': 16 });
  try {
    wm.contributionPlan = { cpuOnly: true, allowedGpus: '', needsMoreRam: false };
    wm.markContributing();
    const status = wm.getStatus();
    assert.equal(status.state, 'contributing');
    assert.equal(status.cpuPool, true);
    assert.equal(status.vramContributedGb, 16);
    assert.equal(status.message, 'Your computer powers the DLNE phone app, with 16 GB of RAM!');
  } finally {
    done();
  }
});

test('a worker found running after a window reload is described by the machine, not as a GPU', () => {
  const { wm, done } = machine(CPU_NODE, 31.2, { 'dlne.worker.maxRamGb': 12 });
  try {
    wm.markContributing(); // no plan: this session did not start it
    assert.equal(wm.getStatus().cpuPool, true);
    assert.equal(wm.getStatus().vramContributedGb, 12);
  } finally {
    done();
  }
});

// ---- the sidebar --------------------------------------------------------------------------

const noop = { onStatusChange() {} };
function sidebarFor(status) {
  stub.reset();
  const provider = new SidebarProvider({ fsPath: ROOT }, {}, { ...noop, getStatus: () => status });
  return provider;
}
const statusOf = (gpu, state, cpuPool, gb) => ({ state, gpu, cpuPool, vramContributedGb: gb });

test('the sidebar links to the phone app, with the address as its tooltip', () => {
  const html = sidebarFor(statusOf(RTX, 'idle', false, 0))._getHtmlForWebview();
  assert.match(html, /<a href="#" id="phone-link" title="https:\/\/dlne\.rossgilsonlabs\.dev\/chat">📱 Use DLNE on your phone<\/a>/);
  assert.match(html, /vscode\.postMessage\(\{ type: 'openPhoneApp' \}\)/);
});

test('the phone link follows a private coordinator, and its address is escaped', () => {
  const provider = sidebarFor(statusOf(RTX, 'idle', false, 0));
  stub.state.settings['dlne.serverUrl'] = 'https://dlne.example.org/"><script>';
  assert.ok(provider._getHtmlForWebview().includes('title="https://dlne.example.org/&quot;>&lt;script>/chat"'));
});

// The panel's first model sits in the page as data (the old card's text and buttons are the Donate compute panel's
// now; test/donate-panel.test.js covers the panel itself).
const panelOf = (html) => JSON.parse(/donatePanel\.update\((\{.*\})\);/.exec(html)[1]);

/** The sidebar page for this status on a 32 GB, 8-core computer (what the panel offers depends on the host). */
function pageOn32Gb(status) {
  const realTotalmem = os.totalmem;
  const realCpus = os.cpus;
  os.totalmem = () => 32 * GIB;
  os.cpus = () => new Array(8).fill({});
  try {
    return sidebarFor(status)._getHtmlForWebview();
  } finally {
    os.totalmem = realTotalmem;
    os.cpus = realCpus;
  }
}

test('the sidebar panel tells a CPU contributor their computer powers the phone app', () => {
  const html = pageOn32Gb(statusOf(CPU_NODE, 'idle', true, 0));
  const panel = panelOf(html);
  assert.equal(panel.cpu.on, true);
  assert.equal(panel.button.label, 'Donate compute');
  assert.match(panel.powers.map((p) => p.text).join('\n'), /Phone app: \d+ GB runs/);
  assert.match(panel.powers.map((p) => p.text).join('\n'), /the coding agent 2nd in line, with 300 chats every 5 hours/);
  assert.doesNotMatch(html, /Contribute Local GPU|VULKAN GPU|CPU Compute Node|Power the DLNE Phone App/);
  const active = pageOn32Gb(statusOf(CPU_NODE, 'contributing', true, 16));
  assert.match(active, /16 GB of RAM lent, at low priority on half your cores/);
  assert.doesNotMatch(active, /Inference unlocked/);
  assert.equal(panelOf(active).button.label, 'Stop donating');
});

test('the sidebar panel for a GPU contributor offers the GPU', () => {
  const panel = panelOf(pageOn32Gb(statusOf(RTX, 'idle', false, 0)));
  assert.equal(panel.gpu.on, true);
  assert.equal(panel.gpu.detail, 'RTX 4060, 8 GB');
  assert.doesNotMatch(JSON.stringify(panel), /Contribute Local GPU/);
});

test('the sidebar sends the card wording with every status, and the phone link opens the app', async () => {
  const status = statusOf(CPU_NODE, 'idle', true, 0);
  let listener;
  stub.reset();
  const provider = new SidebarProvider({ fsPath: ROOT }, {}, { onStatusChange: (fn) => { listener = fn; }, getStatus: () => status });
  const posted = [];
  let onMessage;
  provider.resolveWebviewView(
    { webview: { options: {}, html: '', asWebviewUri: (uri) => uri, postMessage: (m) => posted.push(m), onDidReceiveMessage: (fn) => { onMessage = fn; } } },
    {},
    {}
  );
  assert.equal(posted[0].type, 'restoreHistory');
  assert.equal(posted[0].workerCopy.buttonText, '⚡ Power the DLNE Phone App');
  listener({ ...status, state: 'contributing', vramContributedGb: 16 });
  assert.equal(posted[1].type, 'workerStatus');
  assert.match(posted[1].copy.ruleHtml, /16 GB of RAM lent/);
  await onMessage({ type: 'openPhoneApp' });
  assert.deepEqual(stub.state.commands, [['dlne.openPhoneApp']]);
});

// ---- the status bar -----------------------------------------------------------------------

async function statusBarFor(workerStatus, detailed) {
  stub.reset();
  const client = {
    getClusterStatusDetailed: async () => detailed,
    getQuotaRemaining: () => null,
    getServerUrl: () => DEFAULT_SERVER_URL,
  };
  const bar = new ClusterStatusBar(client, { ...noop, getStatus: () => workerStatus });
  await bar.updateStatus();
  bar.dispose();
  return stub.state.statusBarItems[0];
}
const cluster = {
  status: 'ok', global_active_model: 'qwen2.5-coder-14b', total_vram_gb: 24, active_leases_count: 0, is_locked_for_edits: false,
  total_memory_gb: 56, total_sysram_gb: 32, active_workers_count: 3, server_version: '2.3.0',
};

test('the status bar tooltip for a CPU contributor says it powers the phone app', async () => {
  const item = await statusBarFor(statusOf(CPU_NODE, 'contributing', true, 16), { status: 'ok', data: cluster });
  assert.match(item.text, /\[Phone app 16GB\]/);
  assert.doesNotMatch(item.text, /Contributing/);
  assert.match(item.tooltip, /Local Contribution: CPU POOL \(16GB of RAM, low priority, half your cores\)/);
  assert.match(item.tooltip, /Your computer powers the DLNE phone app\. You get the coding agent 2nd in line, with 300 chats every 5 hours\./);
  assert.doesNotMatch(item.tooltip, /VULKAN|CPU Compute Node/);
});

test('the status bar for a GPU contributor is unchanged', async () => {
  const item = await statusBarFor(statusOf(RTX, 'contributing', false, 8), { status: 'ok', data: cluster });
  assert.match(item.text, /\[Contributing 8GB\]/);
  assert.match(item.tooltip, /Local Contribution: CONTRIBUTING \(8GB via VULKAN - RTX 4060\)/);
});

test('when the swarm asks for compute, the tooltip offers the CPU route', async () => {
  const gpuSide = await statusBarFor(statusOf(RTX, 'idle', false, 0), { status: 'forbidden' });
  assert.match(gpuSide.tooltip, /local GPU compute \(or your CPU, to power the DLNE phone app\)/);
  const cpuSide = await statusBarFor(statusOf(CPU_NODE, 'idle', true, 0), { status: 'forbidden' });
  assert.match(cpuSide.tooltip, /Your computer powers the DLNE phone app, and you get the coding agent 2nd in line, with 300 chats every 5 hours\./);
});

test('a spent free trial is told about the CPU route as well', () => {
  const notice = refusalNotice(403, 'DLNE Compute Required: Free starter quota (100/100 inferences) used.');
  assert.match(notice, /Your free trial is used up/);
  assert.match(notice, /No GPU\? Lend your CPU: it powers the DLNE phone app, and you get the coding agent 2nd in line, with 300 chats every 5 hours\./);
});

// ---- the phone link, the release notes, and claims that must not come back ------------------

test('the phone chat page is on the configured coordinator', () => {
  const config = (value) => ({ get: (key) => (key === 'serverUrl' ? value : undefined) });
  assert.equal(phoneChatUrl(config(undefined)), 'https://dlne.rossgilsonlabs.dev/chat');
  assert.equal(phoneChatUrl(config('http://73.215.216.195:11435')), 'https://dlne.rossgilsonlabs.dev/chat');
  assert.equal(phoneChatUrl(config('https://73-215-216-195.sslip.io')), 'https://dlne.rossgilsonlabs.dev/chat');
  assert.equal(phoneChatUrl(config('https://dlne.duckdns.org')), 'https://dlne.rossgilsonlabs.dev/chat');
  assert.equal(phoneChatUrl(config('http://192.168.1.50:11435/')), 'http://192.168.1.50:11435/chat');
  // The coordinator really serves it there (engine/chat_page.py).
  const chatPy = fs.readFileSync(path.join(ROOT, '..', 'engine', 'chat_page.py'), 'utf8');
  assert.match(chatPy, /@chat_router\.(?:get\("\/chat"|api_route\("\/chat", methods=\["GET", "HEAD"\]), include_in_schema=False\)/);
});

test('every command the manifest contributes is registered, including the phone link', () => {
  const source = fs.readFileSync(path.join(ROOT, 'src', 'extension.ts'), 'utf8');
  const ids = pkg.contributes.commands.map((c) => c.command);
  assert.ok(ids.includes('dlne.openPhoneApp'));
  for (const id of ids) {
    assert.ok(source.includes(`registerCommand('${id}'`), `${id} is not registered`);
  }
  assert.equal(pkg.contributes.commands.find((c) => c.command === 'dlne.openPhoneApp').title, 'DLNE: Use DLNE on Your Phone');
});

test('the version is the same in the manifest and the lockfile, and the CHANGELOG describes it', () => {
  const lock = require('../package-lock.json');
  assert.equal(lock.version, pkg.version);
  assert.equal(lock.packages[''].version, pkg.version);
  const changelog = fs.readFileSync(path.join(ROOT, 'CHANGELOG.md'), 'utf8');
  // (An "Unreleased" section above it is work waiting for its release; it is not a version.)
  const releases = [...changelog.matchAll(/^## \[([^\]]+)\]/gm)].map((m) => m[1]).filter((v) => v !== 'Unreleased');
  assert.equal(releases[0], pkg.version, 'the newest released CHANGELOG entry is this version');
  // 1.3.0's coding members, sealed requests and privacy modes need coordinator 2.4.0.
  assert.match(changelog, new RegExp(`^## \\[${pkg.version.replace(/\./g, '\\.')}\\] - \\d{4}-\\d{2}-\\d{2}\\n\\nPairs with DLNE coordinator \\*\\*2\\.4\\.0\\*\\*`, 'm'));
});

test('no text in the extension says contributors get unlimited chats or that a CPU runs the coding model', () => {
  const walk = (dir) => fs.readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
    e.isDirectory() ? walk(path.join(dir, e.name)) : e.name.endsWith('.ts') ? [path.join(dir, e.name)] : []);
  const offenders = walk(path.join(ROOT, 'src')).filter((file) => /unlimited/i.test(fs.readFileSync(file, 'utf8')));
  assert.deepEqual(offenders, []);
  const settings = JSON.stringify(pkg.contributes);
  assert.doesNotMatch(settings, /unlimited/i);
  // The settings page says what a CPU is for, and no longer promises a fixed 12 GB offer.
  const props = pkg.contributes.configuration.properties;
  assert.match(props['dlne.worker.device'].description, /a CPU powers the DLNE phone app/);
  assert.doesNotMatch(props['dlne.worker.maxRamGb'].description, /offered 12/);
  assert.doesNotMatch(Object.values(pkg.contributes.chatParticipants[0].commands).map((c) => c.description).join(' '), /Vulkan GPU compute/);
});

test('the messages a CPU contributor sees never make the GPU-only claims', () => {
  const { workerStartedNotice, ramOfferText, ramChoices, cpuReason } = require('../dist/worker/contribution');
  const { message, detail } = ramOfferText(cpuReason(false), ramChoices(64));
  for (const text of [workerStartedNotice(true), message, detail]) {
    assert.doesNotMatch(text, NOT_FOR_CPU);
  }
});
