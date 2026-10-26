'use strict';
/**
 * "Both": a GPU machine lends its CPU to the phone app at the same time as its GPU does whatever job the swarm
 * gives it (engine/companion.py on the worker's side). This file is the extension's side: the setting, the plan,
 * the dialog's third button, the RAM offer, what the worker is told, and what the worker says back (DLNE_ALSO).
 * Machines that lend one thing only must behave exactly as before, and several tests pin that.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const childProcess = require('node:child_process');

const stub = require('./helpers/vscode-stub');
const { WorkerManager } = require('../dist/worker/workerManager');
const c = require('../dist/worker/contribution');

process.env.HOME = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-both-home-'));
process.env.USERPROFILE = process.env.HOME;
WorkerManager.prototype.init = async function init() {};

const RTX = { hasGpu: true, name: 'RTX 4060', vramTotalGb: 8, api: 'vulkan' };
const CPU_NODE = { hasGpu: false, name: 'CPU Compute Node', vramTotalGb: 4, api: 'cpu' };
const INTEGRATED = { hasGpu: false, name: 'CPU Compute Node', vramTotalGb: 4, api: 'cpu', integratedGpu: 'AMD Radeon Graphics' };

function globalState(initial = {}) {
  const values = { ...initial };
  return { values, get: (key, fallback) => (key in values ? values[key] : fallback), update: async (key, value) => { values[key] = value; } };
}

function manager({ gpu = RTX, state = globalState(), settings = {} } = {}) {
  stub.reset();
  Object.assign(stub.state.settings, settings);
  const wm = new WorkerManager({ subscriptions: [], globalState: state });
  wm.localGpu = gpu;
  wm.currentState = 'idle';
  wm.externalWorker = false; // a fake worker another test file runs is not this machine's
  wm.vramContributed = 0;
  return { wm, state, done: () => wm.dispose() };
}

// ---- the setting ---------------------------------------------------------------------------

test('dlne.worker.device has a fourth value, "both", with a description of its own', () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'package.json'), 'utf8'));
  const device = manifest.contributes.configuration.properties['dlne.worker.device'];
  assert.deepEqual(device.enum, ['auto', 'gpu', 'cpu', 'both']);
  assert.equal(device.enumDescriptions.length, device.enum.length);
  assert.match(device.enumDescriptions[3], /GPU does the job the swarm gives it, and the CPU also powers the DLNE phone app/);
  assert.match(device.enumDescriptions[3], /Without a usable GPU this is the same as "cpu"/);
  assert.equal(device.default, 'auto');
  const ram = manifest.contributes.configuration.properties['dlne.worker.alsoCpuRamGb'];
  assert.equal(ram.type, 'number');
  assert.equal(ram.default, 0);
  assert.equal(ram.scope, 'application');
  assert.match(ram.description, /separate from dlne\.worker\.maxRamGb/);
});

// ---- the plan ------------------------------------------------------------------------------

test('"both" on a GPU machine runs the GPU and also lends the CPU, once the RAM for it is chosen', () => {
  assert.deepEqual(c.planContribution('both', true, '', 4, undefined, 16),
    { cpuOnly: false, allowedGpus: '', needsMoreRam: false, alsoCpu: true });
  assert.deepEqual(c.planContribution('both', true, '0,1', 4, undefined, 0),
    { cpuOnly: false, allowedGpus: '0,1', needsMoreRam: true, alsoCpu: true }, 'no amount yet: the offer is made');
  assert.equal(c.planContribution('both', true, '', 4, undefined, 6).needsMoreRam, true, 'under the 8 GB floor');
  assert.equal(c.planContribution('both', true, '', 4, undefined, 8).needsMoreRam, false);
});

test('"both" without a usable GPU is "cpu", exactly', () => {
  for (const [hasGpu, integrated] of [[false, undefined], [false, 'AMD Radeon Graphics']]) {
    for (const [maxRam, alsoRam] of [[4, 0], [16, 0], [16, 16]]) {
      assert.deepEqual(c.planContribution('both', hasGpu, '', maxRam, integrated, alsoRam), c.planContribution('cpu', hasGpu, '', maxRam, integrated));
    }
  }
  assert.equal(c.planContribution('both', false, '', 4).cpuOnly, true);
  assert.equal('alsoCpu' in c.planContribution('both', false, '', 16), false);
});

test('machines that lend one thing only get the plan they always got', () => {
  assert.deepEqual(c.planContribution('auto', true, '0', 4), { cpuOnly: false, allowedGpus: '0', needsMoreRam: false });
  assert.deepEqual(c.planContribution('gpu', true, '', 4), { cpuOnly: false, allowedGpus: '', needsMoreRam: false });
  assert.deepEqual(c.planContribution('cpu', true, '', 16), { cpuOnly: true, allowedGpus: 'none', needsMoreRam: false });
  assert.deepEqual(c.planContribution('auto', false, '', 4), { cpuOnly: true, allowedGpus: '', needsMoreRam: true });
  assert.deepEqual(c.planContribution(undefined, true, '', 4), { cpuOnly: false, allowedGpus: '', needsMoreRam: false });
  assert.deepEqual(c.planContribution('nonsense', true, '', 4, undefined, 16), { cpuOnly: false, allowedGpus: '', needsMoreRam: false });
});

// ---- the dialog ----------------------------------------------------------------------------

test('the first-time dialog has a third button, "Lend both", with one plain sentence', () => {
  const { detail, buttons } = c.lendGpuPrompt('RTX 4060');
  assert.deepEqual(buttons, ['Lend my GPU', 'Lend my CPU only', 'Lend both']);
  assert.equal(
    c.LEND_BOTH_SENTENCE,
    '"Lend both" does both at once: your GPU does the job above, and your CPU also powers the DLNE phone app with the RAM you choose ' +
      '(it sets dlne.worker.device to "both"; you are asked how much RAM next).'
  );
  assert.ok(detail.split('\n').includes(c.LEND_BOTH_SENTENCE));
  assert.ok(detail.split('\n').includes(c.CODING_RAM_SENTENCE), 'the RAM sentence for the coding job is still there');
});

test('"Lend both" sets the device to "both" and goes on', async () => {
  const { wm, state, done } = manager();
  try {
    stub.state.warningChoice = c.LEND_BOTH;
    assert.equal(await wm.confirmLendingGpu(), true);
    assert.deepEqual(stub.state.updates, [['dlne.worker.device', 'both']]);
    assert.equal(state.values['dlne.lendGpuNoticeSeen'], true);
  } finally {
    done();
  }
});

test('"Lend my GPU" changes nothing, except that it takes "both" back to the GPU alone', async () => {
  const plain = manager();
  try {
    stub.state.warningChoice = c.LEND_GPU;
    assert.equal(await plain.wm.confirmLendingGpu(), true);
    assert.deepEqual(stub.state.updates, []);
  } finally {
    plain.done();
  }
  const both = manager({ settings: { 'dlne.worker.device': 'both' } });
  try {
    stub.state.warningChoice = c.LEND_GPU;
    assert.equal(await both.wm.confirmLendingGpu(), true);
    assert.deepEqual(stub.state.updates, [['dlne.worker.device', 'gpu']]);
  } finally {
    both.done();
  }
});

// ---- the RAM for the CPU half ---------------------------------------------------------------

function withRam(totalGb, fn) {
  const real = os.totalmem;
  os.totalmem = () => totalGb * 1024 ** 3 * 0.97;
  return Promise.resolve(fn()).finally(() => { os.totalmem = real; });
}

test('"both" asks how much RAM the CPU lends, with the existing offer, and keeps the answer in its own setting', async () => {
  const { wm, done } = manager({ settings: { 'dlne.worker.device': 'both', 'dlne.worker.maxRamGb': 4 } });
  try {
    await withRam(32, async () => {
      stub.state.warningChoice = 'Lend 16 GB (recommended)';
      const plan = await wm.planThisMachine();
      assert.equal(plan.alsoCpu, true);
      assert.deepEqual(stub.state.updates, [['dlne.worker.alsoCpuRamGb', 16]], 'dlne.worker.maxRamGb is not touched');
      const { message, items } = stub.state.warningMessages[0];
      assert.match(message, /You chose to lend your CPU as well as your GPU\./);
      assert.match(items[0].detail, /set dlne\.worker\.alsoCpuRamGb for another amount/);
    });
  } finally {
    done();
  }
});

test('an amount already chosen is not asked again', async () => {
  const { wm, done } = manager({ settings: { 'dlne.worker.device': 'both', 'dlne.worker.alsoCpuRamGb': 12 } });
  try {
    const plan = await wm.planThisMachine();
    assert.equal(plan.alsoCpu, true);
    assert.equal(plan.needsMoreRam, false);
    assert.equal(stub.state.warningMessages.length, 0);
  } finally {
    done();
  }
});

test('closing the RAM offer starts nothing', async () => {
  const { wm, done } = manager({ settings: { 'dlne.worker.device': 'both' } });
  try {
    await withRam(32, async () => {
      stub.state.warningChoice = undefined;
      assert.equal(await wm.planThisMachine(), null);
      assert.deepEqual(stub.state.updates, []);
    });
  } finally {
    done();
  }
});

test('a machine too small to lend its CPU as well lends the GPU alone, and says so', async () => {
  const { wm, done } = manager({ settings: { 'dlne.worker.device': 'both' } });
  try {
    await withRam(8, async () => {
      const plan = await wm.planThisMachine();
      assert.deepEqual(plan, { cpuOnly: false, allowedGpus: '', needsMoreRam: false, alsoCpu: false });
      assert.equal(stub.state.warningMessages.length, 1);
      assert.match(stub.state.warningMessages[0].message, /your CPU is not lent this time; your GPU is lent as usual/);
    });
  } finally {
    done();
  }
});

test('"both" on a CPU machine is the CPU offer for dlne.worker.maxRamGb, as "cpu" is', async () => {
  const { wm, done } = manager({ gpu: CPU_NODE, settings: { 'dlne.worker.device': 'both', 'dlne.worker.maxRamGb': 4 } });
  try {
    await withRam(32, async () => {
      stub.state.warningChoice = 'Lend 16 GB (recommended)';
      const plan = await wm.planThisMachine();
      assert.equal(plan.cpuOnly, true);
      assert.equal('alsoCpu' in plan, false);
      assert.deepEqual(stub.state.updates, [['dlne.worker.maxRamGb', 16]]);
      assert.match(stub.state.warningMessages[0].message, /This machine has no usable GPU\./);
    });
  } finally {
    done();
  }
});

// ---- what the worker is started with ---------------------------------------------------------

/** Start the worker with these settings and an engine whose dlne_server.py says `engineText`; its arguments and config.txt. */
async function started(settings, engineText = '--coding-max-ram --also-cpu-ram', gpu = RTX) {
  const { wm, done } = manager({ gpu, settings: {
    'dlne.worker.pythonPath': process.execPath, 'dlne.autoUpdate': false, 'dlne.serverUrl': 'http://127.0.0.1:9', ...settings } });
  const root = path.join(process.env.HOME, '.dlne', 'runtime');
  fs.rmSync(root, { recursive: true, force: true });
  fs.mkdirSync(path.join(root, 'config'), { recursive: true });
  fs.writeFileSync(path.join(root, 'dlne_server.py'), `// ${engineText}\n`);
  wm.isSystemdActive = () => false;
  wm.isProcessActive = () => false;
  wm.isPortListening = async () => false;
  wm.ensureRpcBinaries = async () => {};
  const realSpawn = childProcess.spawn;
  const spawned = [];
  childProcess.spawn = (...args) => { spawned.push(args); return realSpawn(...args); };
  try {
    assert.equal(await wm.startWorker(), true);
    const configFile = path.join(root, 'config', 'config.txt');
    return { args: spawned[0][1], config: fs.existsSync(configFile) ? fs.readFileSync(configFile, 'utf8') : null, wm, root, lines: stub.state.channels.flatMap((ch) => ch.lines) };
  } finally {
    childProcess.spawn = realSpawn;
    if (wm.workerProcess) wm.workerProcess.kill();
    done();
  }
}

const valueOf = (args, flag) => (args.includes(flag) ? args[args.indexOf(flag) + 1] : undefined);

test('"both" tells an engine that reads it how much RAM the CPU lends, and writes it to config.txt', async () => {
  const { args, config } = await started({ 'dlne.worker.device': 'both', 'dlne.worker.alsoCpuRamGb': 16 });
  assert.equal(valueOf(args, '--also-cpu-ram'), '16');
  assert.equal(valueOf(args, '--max-ram'), '4', 'the GPU job\'s limit is what it was');
  assert.match(config, /^ALSO_CPU_RAM_GB=16$/m);
});

test('an engine that does not know the argument is not given it (it would refuse to start), and the GPU is lent alone', async () => {
  const { args, lines } = await started({ 'dlne.worker.device': 'both', 'dlne.worker.alsoCpuRamGb': 16 }, '--coding-max-ram');
  assert.equal(args.includes('--also-cpu-ram'), false);
  assert.ok(lines.some((l) => /too old to lend the CPU next to the GPU/.test(l)));
});

test('a machine that lends one thing is started exactly as before and its config.txt is not touched', async () => {
  for (const settings of [{}, { 'dlne.worker.device': 'gpu' }, { 'dlne.worker.device': 'auto', 'dlne.worker.alsoCpuRamGb': 16 }]) {
    const { args, config } = await started(settings);
    assert.equal(args.includes('--also-cpu-ram'), false);
    assert.ok(config === null || !/ALSO_CPU_RAM_GB/i.test(config), 'no key written');
  }
  const cpu = await started({ 'dlne.worker.device': 'cpu', 'dlne.worker.maxRamGb': 16 }, '--coding-max-ram --also-cpu-ram', CPU_NODE);
  assert.equal(cpu.args.includes('--also-cpu-ram'), false);
  assert.equal(valueOf(cpu.args, '--max-ram'), '16');
});

test('going back to the GPU alone clears a leftover amount, so a service started from config.txt stops lending the CPU', async () => {
  const root = path.join(process.env.HOME, '.dlne', 'runtime');
  const first = await started({ 'dlne.worker.device': 'both', 'dlne.worker.alsoCpuRamGb': 16 });
  assert.match(first.config, /^ALSO_CPU_RAM_GB=16$/m);
  fs.writeFileSync(path.join(root, 'config', 'config.txt'), 'MAX_RAM_GB=4.0\nALSO_CPU_RAM_GB=16\n');
  const wm = manager({ settings: { 'dlne.worker.device': 'gpu' } });
  try {
    wm.wm.syncAlsoCpuSetting({ cpuOnly: false, allowedGpus: '', needsMoreRam: false }, root);
    assert.equal(fs.readFileSync(path.join(root, 'config', 'config.txt'), 'utf8'), 'MAX_RAM_GB=4.0\nALSO_CPU_RAM_GB=0\n');
  } finally {
    wm.done();
  }
});

test('the config edit: set when lending both, cleared only when something positive is there, otherwise left alone', () => {
  assert.equal(c.alsoCpuConfigUpdate('MAX_RAM_GB=4\n', true, 16), 'MAX_RAM_GB=4\nALSO_CPU_RAM_GB=16\n');
  assert.equal(c.alsoCpuConfigUpdate('also_cpu_ram_gb = 8\r\nX=1\r\n', true, 16), 'also_cpu_ram_gb=16\r\nX=1\r\n');
  assert.equal(c.alsoCpuConfigUpdate('ALSO_CPU_RAM_GB=16\n', false, 0), 'ALSO_CPU_RAM_GB=0\n');
  assert.equal(c.alsoCpuConfigUpdate('MAX_RAM_GB=4\n', false, 0), null, 'a config without the key is not touched');
  assert.equal(c.alsoCpuConfigUpdate('', false, 0), null);
  assert.equal(c.alsoCpuConfigUpdate('ALSO_CPU_RAM_GB=0\n', false, 0), null);
  assert.equal(c.alsoCpuConfigUpdate('ALSO_CPU_RAM_GB=16\n', true, 16), null, 'already right');
});

// ---- what the worker says back ---------------------------------------------------------------

test('DLNE_ALSO lines are read on their own, last one wins, and nothing else is a line', () => {
  assert.equal(c.parseAlso(''), null);
  assert.equal(c.parseAlso('DLNE_ROLE: coding {"slots":2}\n'), null);
  assert.equal(c.parseAlso('DLNE_ALSO: app\n'), true);
  assert.equal(c.parseAlso('DLNE_ALSO: app\nDLNE_ALSO: none\r\n'), false);
  assert.equal(c.parseAlso('x DLNE_ALSO: app\nDLNE_ALSO: banana\n'), true, 'an unknown word is ignored');
  assert.equal(c.parseAlso('DLNE_ALSO: none\nDLNE_ALSO: app\n'), true);
});

test('DLNE_ALSO does not disturb DLNE_ROLE, whichever comes last', () => {
  const output = 'DLNE_ROLE: coding {"slots":2}\nDLNE_ALSO: app\n';
  assert.equal(c.parseRole(output).role, 'coding');
  assert.equal(c.parseRole('DLNE_ALSO: app\nDLNE_ROLE: rpc\n').role, 'rpc');
});

test('the status says the CPU serves the pool too while the worker says so, and not after it stops', () => {
  const { wm, done } = manager();
  try {
    wm.currentState = 'contributing';
    wm.contributionPlan = { cpuOnly: false, allowedGpus: '', needsMoreRam: false, alsoCpu: true };
    assert.equal(wm.getStatus().alsoCpu, undefined, 'not until the worker says');
    let changes = 0;
    wm.onStatusChange(() => { changes++; });
    wm.noteWorkerOutput('DLNE_ROLE: rpc\nDLNE_ALSO: app\n', 'stdout');
    assert.equal(wm.getStatus().alsoCpu, true);
    assert.equal(wm.getStatus().role, 'rpc');
    const before = changes;
    wm.noteWorkerOutput('DLNE_ALSO: app\n', 'stdout');
    assert.equal(changes, before, 'the same line twice is no change');
    wm.noteWorkerOutput('DLNE_ALSO: none\n', 'stdout');
    assert.equal(wm.getStatus().alsoCpu, undefined);
    wm.noteWorkerOutput('DLNE_ALSO: app\n', 'stdout');
    wm.onWorkerExit(0);
    assert.equal(wm.getStatus().alsoCpu, undefined, 'a stopped worker lends nothing');
  } finally {
    done();
  }
});

test('a CPU-only machine never reports a second job', () => {
  const { wm, done } = manager({ gpu: CPU_NODE, settings: { 'dlne.worker.device': 'both' } });
  try {
    wm.currentState = 'contributing';
    wm.contributionPlan = { cpuOnly: true, allowedGpus: '', needsMoreRam: false };
    wm.noteWorkerOutput('DLNE_ALSO: app\n', 'stdout');
    assert.equal(wm.getStatus().alsoCpu, undefined);
    assert.equal(wm.getStatus().cpuPool, true);
  } finally {
    done();
  }
});

// ---- two jobs on screen ------------------------------------------------------------------------

const { SidebarProvider } = require('../dist/views/sidebarProvider');
const { ClusterStatusBar } = require('../dist/statusbar/clusterStatusBar');

const ROOT = path.join(__dirname, '..');
const gpuView = (extra = {}) => ({ state: 'contributing', cpuPool: false, vramContributedGb: 8, gpu: RTX, ...extra });

test('the sidebar card shows both jobs when both run, and a single job exactly as before', () => {
  const both = c.contributionCopy(gpuView({ role: 'rpc', alsoCpu: true }));
  assert.match(both.ruleHtml, /Contributing 8 GB to cluster\. Inference unlocked!<br>📱 <strong>Also:<\/strong> Your CPU also powers the DLNE phone app/);
  assert.match(c.roleCopy(gpuView({ role: 'rpc', alsoCpu: true })).roleHtml,
    /^<strong>Role:<\/strong> GPU pool\..*<br><strong>Also:<\/strong> Phone app member\. Your CPU also powers the DLNE phone app/);
  assert.match(c.roleCopy(gpuView({ role: 'coding', alsoCpu: true, roleDetail: { slots: 2 } })).roleHtml, /Coding member\..*Also:<\/strong> Phone app member/);
  const alone = gpuView({ role: 'coding', roleDetail: { slots: 2 } });
  assert.equal(c.contributionCopy(alone).ruleHtml, '✅ <strong>Active:</strong> Contributing 8 GB to cluster. Inference unlocked!');
  assert.doesNotMatch(c.roleCopy(alone).roleHtml, /Also/);
  assert.doesNotMatch(c.roleCopy(gpuView({ role: 'rpc', alsoCpu: false })).roleHtml, /Also/);
  for (const state of ['idle', 'starting']) {
    assert.doesNotMatch(c.contributionCopy(gpuView({ state, alsoCpu: true })).ruleHtml, /Also/, 'only a running worker has two jobs');
  }
});

test('the status bar tooltip lists the second job, and only when the worker says it runs', () => {
  const lines = c.contributionTooltip(gpuView({ role: 'rpc', alsoCpu: true }));
  assert.equal(lines.at(-1), `Also: Phone app member. ${c.ALSO_CPU_LINE}`);
  assert.deepEqual(c.contributionTooltip(gpuView()), ['Local Contribution: CONTRIBUTING (8GB via VULKAN - RTX 4060)']);
  assert.equal(c.contributionTooltip(gpuView({ state: 'idle', alsoCpu: true })).length, 1);
});

test('the status bar names both jobs in its tag', async () => {
  const client = {
    getQuotaRemaining: () => null,
    getServerUrl: () => 'http://coordinator',
    getClusterStatusDetailed: async () => ({
      status: 'ok',
      data: { status: 'Active', global_active_model: 'qwen', total_vram_gb: 24, total_memory_gb: 44, total_sysram_gb: 20, active_workers_count: 2, active_leases_count: 0, is_locked_for_edits: false, server_version: '2.4.0', model_loading: { is_loading: false } },
    }),
  };
  const worker = (role, alsoCpu) => ({ onStatusChange() {}, getStatus: () => ({ ...gpuView({ role, alsoCpu }) }) });
  for (const [role, alsoCpu, tag] of [
    ['coding', true, /\[Coding member 8GB \+ Phone app\]/], ['rpc', true, /\[Contributing 8GB \+ Phone app\]/],
    [undefined, true, /\[Contributing 8GB \+ Phone app\]/],
    ['coding', undefined, /\[Coding member 8GB\]/], ['rpc', false, /\[Contributing 8GB\]/], [undefined, undefined, /\[Contributing 8GB\]/],
  ]) {
    stub.reset();
    const bar = new ClusterStatusBar(client, worker(role, alsoCpu));
    await bar.updateStatus();
    assert.match(stub.state.statusBarItems[0].text, tag);
    if (!alsoCpu) assert.doesNotMatch(stub.state.statusBarItems[0].text, /Phone app/);
    bar.dispose();
  }
});

test('the sidebar receives both jobs in the status it renders', () => {
  const { wm, done } = manager();
  try {
    wm.currentState = 'contributing';
    wm.contributionPlan = { cpuOnly: false, allowedGpus: '', needsMoreRam: false, alsoCpu: true };
    wm.noteWorkerOutput('DLNE_ROLE: rpc\nDLNE_ALSO: app\n', 'stdout');
    const posted = [];
    new SidebarProvider({ fsPath: ROOT }, {}, { onStatusChange() {}, getStatus: () => wm.getStatus() }).resolveWebviewView(
      { webview: { options: {}, html: '', asWebviewUri: (u) => u, postMessage: (m) => posted.push(m), onDidReceiveMessage() {} } }, {}, {});
    assert.match(posted[0].workerRole.roleHtml, /Role:<\/strong> GPU pool\..*Also:<\/strong> Phone app member/);
  } finally {
    done();
  }
});

test('/contribute and the quick pick say both jobs too', () => {
  const chat = fs.readFileSync(path.join(ROOT, 'src', 'views', 'nativeChatParticipant.ts'), 'utf8');
  assert.match(chat, /updated\.alsoCpu \? `\\n\\n\*\*Also: \$\{ROLE_LABEL\.app\}\.\*\* \$\{ALSO_CPU_LINE\}` : ''/);
  const ext = fs.readFileSync(path.join(ROOT, 'src', 'extension.ts'), 'utf8');
  assert.match(ext, /workerStatus\.alsoCpu \? ' and powering the phone app with the CPU' : ''/);
});

test('the wording the extension reads is the wording the worker prints', () => {
  const py = fs.readFileSync(path.join(ROOT, '..', 'engine', 'companion.py'), 'utf8');
  assert.ok(py.includes(`ALSO_MARKER = "${c.ALSO_MARKER}"`));
  const worker = fs.readFileSync(path.join(ROOT, '..', 'engine', 'worker.py'), 'utf8');
  assert.ok(worker.includes('ALSO_MARKER + "app"') && worker.includes('ALSO_MARKER + "none"'));
  const config = fs.readFileSync(path.join(ROOT, '..', 'engine', 'config.py'), 'utf8');
  assert.ok(config.includes('ALSO_CPU_RAM_GB') && config.includes(`"also_cpu_ram_gb"`));
  assert.equal(c.ALSO_CPU_RAM_KEY, 'ALSO_CPU_RAM_GB');
  const server = fs.readFileSync(path.join(ROOT, '..', 'dlne_server.py'), 'utf8');
  assert.ok(server.includes('--also-cpu-ram'));
});
