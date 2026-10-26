'use strict';
/**
 * The sidebar's "Donate compute" panel: the model it is drawn from (what each slider offers on a machine, what a
 * choice powers), the checks on what the webview posts, the settings a choice writes, the thread limit in
 * config.txt, the clock notice, and the page keeping its CSP.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const stub = require('./helpers/vscode-stub');
const { WorkerManager } = require('../dist/worker/workerManager');
const { SidebarProvider } = require('../dist/views/sidebarProvider');
const { createDonatePanel } = require('../dist/views/donateView');
const d = require('../dist/worker/donatePanel');
const c = require('../dist/worker/contribution');

const GIB = 1024 ** 3;
const ROOT = path.join(__dirname, '..');
const HOME = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-donate-home-'));
process.env.HOME = HOME;
process.env.USERPROFILE = HOME;

// The constructor's background start-up (probe the GPU, poll the worker, resume) would race these tests.
WorkerManager.prototype.init = async function init() {};

const gpuOf = (name, vramTotalGb) => ({ hasGpu: true, name, vramTotalGb, api: 'vulkan' });
const NO_GPU = { hasGpu: false, name: 'CPU Compute Node', vramTotalGb: 4, api: 'cpu' };
/** A machine with this GPU, this much RAM (the amount it was sold with) and this many logical CPUs. */
const machineOf = (gpu, ramGb, cpus = 8) => d.donateMachine(gpu, ramGb * GIB * 0.94, cpus);
/** The default settings (nothing set), with overrides. */
const settingsOf = (over = {}) => ({ device: 'auto', maxVramGb: -1, maxRamGb: null, alsoCpuRamGb: 0, maxCpuThreads: -1, allowCodingMember: true, ...over });
const panelFor = (machine, over = {}, extra = {}) => d.buildDonatePanel({ machine, settings: settingsOf(over), state: 'idle', ...extra });
const texts = (panel) => panel.powers.map((p) => p.text);
const labels = (slider) => slider.stops.map((s) => s.label);
const values = (slider) => slider.stops.map((s) => s.value);

// ---- the model: machines -------------------------------------------------------------------------------------------

test('a computer without a GPU: the CPU is the offer, and the amounts follow how much RAM it has', () => {
  const small = panelFor(machineOf(NO_GPU, 8, 8));
  assert.equal(small.cpu.available, false);
  assert.match(small.cpu.reason, /16 GB or more; this one has 8 GB/);
  assert.equal(small.cpu.on, false);
  assert.equal(small.gpu.available, false);
  assert.equal(small.gpu.reason, 'No usable GPU was found on this computer.');
  assert.equal(small.button.disabled, true, 'nothing to donate');
  assert.match(small.button.hint, /cannot donate/);
  assert.match(texts(small)[0], /Nothing is switched on/);

  const sixteen = panelFor(machineOf(NO_GPU, 16));
  assert.deepEqual(values(sixteen.cpu.ram), [8], 'half of 16 GB is the minimum, no more');
  assert.equal(sixteen.cpu.on, true);
  assert.equal(sixteen.button.disabled, false);

  const thirtyTwo = panelFor(machineOf(NO_GPU, 32));
  assert.deepEqual(values(thirtyTwo.cpu.ram), [8, 16], 'smallest first');
  assert.equal(thirtyTwo.cpu.ram.index, 1, 'starts on the amount DLNE suggests (16 GB)');
  assert.deepEqual(thirtyTwo.cpu.ram.stops.map((s) => s.sub), ['Qwen3 4B', 'Qwen3.6']);
  assert.ok(texts(thirtyTwo).includes('Phone app: 16 GB runs the main phone model, Qwen3.6.'));
  assert.ok(texts(thirtyTwo).some((t) => t.includes(c.CPU_POOL_PERK)), 'what the donor gets back');

  const sixtyFour = panelFor(machineOf(NO_GPU, 64));
  assert.deepEqual(values(sixtyFour.cpu.ram), [8, 16, 24], 'the same stops ramChoices offers, in order');
  assert.deepEqual(values(sixtyFour.cpu.ram), c.ramChoices(64).map((r) => r.gb).sort((a, b) => a - b));
  assert.equal(sixtyFour.cpu.ram.index, 2);
  assert.deepEqual(sixtyFour.cpu.ram.stops.map((s) => s.sub), ['Qwen3 4B', 'Qwen3.6', 'Qwen3.6 full']);
  assert.ok(texts(sixtyFour).includes('Phone app: 24 GB runs Qwen3.6 at full quality, once 2 volunteers can host it.'));
});

test('a computer with fewer than 4 cores cannot power the phone app', () => {
  const panel = panelFor(machineOf(NO_GPU, 32, 2));
  assert.equal(panel.cpu.available, false);
  assert.match(panel.cpu.reason, /at least 4 CPU cores; this computer has 2/);
});

test('an 8 GB GPU with 32 GB of RAM: a coding member, with RAM stops up to total minus 4 GB', () => {
  const panel = panelFor(machineOf(gpuOf('RTX 4060', 8), 32, 16));
  assert.equal(panel.gpu.available, true);
  assert.equal(panel.gpu.on, true);
  assert.equal(panel.cpu.on, false, 'auto lends the GPU when there is one');
  assert.equal(panel.gpu.detail, 'RTX 4060, 8 GB');
  assert.deepEqual(panel.gpu.vram, { min: 8, max: 8, value: 8, label: 'All 8 GB' });
  assert.deepEqual(labels(panel.gpu.ram), ['Auto', '16 GB', '24 GB']);
  assert.equal(panel.gpu.ram.index, 0, 'Auto: the RAM that is free when the job starts');
  assert.match(texts(panel)[0], /^Coding member: your GPU runs the Qwen3\.6 coding model and answers other people's coding requests\. The swarm's biggest need\./);
  assert.match(texts(panel)[0], /counts the RAM that is free when it starts/);
  assert.match(texts(panel)[0], /speed test when the worker joins decides/);
  assert.doesNotMatch(texts(panel)[0], /tokens|tok\/s|per second|fast/i, 'no speed is promised');
  assert.ok(texts(panel).includes('You get back: the coding agent first in line, with no chat limit, while your GPU has a job.'));
  assert.deepEqual(panel.button, { label: 'Donate compute', action: 'start', disabled: false, hint: '' });
});

test('a 24 GB GPU with 64 GB of RAM, both jobs on: the sliders reach the card and the RAM', () => {
  const machine = machineOf(gpuOf('RTX 4090', 24), 64, 24);
  const panel = panelFor(machine, { device: 'both', maxVramGb: 12, maxRamGb: 32, alsoCpuRamGb: 16, maxCpuThreads: 8 });
  assert.deepEqual(panel.gpu.vram, { min: 8, max: 24, value: 12, label: '12 GB' });
  assert.deepEqual(values(panel.gpu.ram), [0, 16, 24, 32, 40, 48, 56], '64 GB less 4 GB: stops to 60');
  assert.equal(panel.gpu.ram.stops[panel.gpu.ram.index].value, 32);
  assert.equal(panel.gpu.on && panel.cpu.on, true);
  assert.equal(panel.cpu.ram.stops[panel.cpu.ram.index].value, 16, 'the CPU amount of "both" is alsoCpuRamGb');
  const lines = texts(panel);
  assert.match(lines[0], /^Coding member:/);
  assert.match(lines[0], /up to 32 GB of your RAM/);
  assert.match(lines[0], /Lending 12 of 24 GB of VRAM\./);
  assert.ok(lines.includes('Phone app: 16 GB runs the main phone model, Qwen3.6.'));
  assert.ok(lines.includes('8 threads of 24 for the swarm; the rest stay yours.'));
  assert.ok(lines.some((l) => l.startsWith('You get back:') && l.includes('first in line, with no chat limit') && l.includes(c.CPU_POOL_PERK)));
  const all = panelFor(machine, { maxVramGb: -1 });
  assert.equal(all.gpu.vram.value, 24);
  assert.equal(all.gpu.vram.label, 'All 24 GB');
});

test('a laptop whose only GPU is integrated: no GPU, with the reason; the CPU is offered', () => {
  const machine = d.donateMachine({ hasGpu: false, name: 'CPU Compute Node', vramTotalGb: 4, api: 'cpu', integratedGpu: 'AMD Radeon Graphics (RADV RENOIR)' }, 32 * GIB * 0.94, 16);
  const panel = panelFor(machine);
  assert.equal(panel.gpu.available, false);
  assert.equal(panel.gpu.on, false);
  assert.match(panel.gpu.reason, /AMD Radeon Graphics \(RADV RENOIR\) is an integrated GPU/);
  assert.equal(panel.gpu.ram, null);
  assert.equal(panel.cpu.on, true);
  assert.equal(panel.button.disabled, false);
});

test('a GPU still being looked for says so', () => {
  const panel = panelFor(d.donateMachine({ hasGpu: false, name: 'Probing GPU...', vramTotalGb: 0, api: 'vulkan' }, 32 * GIB * 0.94, 8));
  assert.equal(panel.gpu.reason, 'Looking for your GPU...');
});

test('a GPU under 8 GB joins the GPU pool, and says it is under what the swarm can use', () => {
  const panel = panelFor(machineOf(gpuOf('GTX 1650', 4), 16));
  assert.deepEqual(panel.gpu.vram, { min: 4, max: 4, value: 4, label: 'All 4 GB' });
  assert.equal(panel.gpu.ram, null, 'a pool member needs little RAM; there is nothing to choose');
  assert.match(texts(panel)[0], /^GPU pool: Your GPU holds a share of one big model/);
  assert.match(texts(panel)[0], /needs at least 8 GB of VRAM to run a coding model by itself; yours has 4 GB, which is under what the swarm can use/);
});

test('a coding-capable GPU on a computer with little RAM is told plainly', () => {
  const panel = panelFor(machineOf(gpuOf('RTX 3070', 8), 12));
  assert.ok(panel.powers.some((p) => p.kind === 'warn' && /12 GB of RAM, under the roughly 16 to 24 GB the coding model needs/.test(p.text)));
  assert.equal(panel.gpu.ram, null, 'no RAM amount to offer');
});

test('the coding-member switch in settings is honoured in the wording', () => {
  const panel = panelFor(machineOf(gpuOf('RTX 4060', 8), 32), { allowCodingMember: false });
  assert.match(texts(panel)[0], /^GPU pool: .*switched off in dlne\.worker\.allowCodingMember/);
});

test('threads: Auto, then 2 up to the logical CPU count; Auto is half the cores, 2 to 8', () => {
  const machine = machineOf(NO_GPU, 32, 16);
  const panel = panelFor(machine);
  assert.deepEqual(values(panel.cpu.threads), [-1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]);
  assert.equal(panel.cpu.threads.stops[0].label, 'Auto (8)');
  assert.equal(panel.cpu.threads.index, 0);
  assert.ok(texts(panel).includes('Auto: 8 threads of 16 for the swarm; the rest stay yours.'));
  assert.deepEqual([2, 4, 6, 16, 64].map(d.autoThreads), [2, 2, 3, 8, 8]);
  assert.ok(texts(panelFor(machine, { maxCpuThreads: 8 })).includes('8 threads of 16 for the swarm; the rest stay yours.'));
  const all = panelFor(machine, { maxCpuThreads: 16 });
  assert.ok(texts(all).includes('All 16 threads go to the swarm; none are left for you.'));
  assert.equal(all.cpu.threads.label, '16');
  assert.match(panelFor(machineOf(gpuOf('RTX 4060', 8), 32)).cpu.threads.note, /also caps the CPU threads your GPU's coding model uses/);
});

// ---- the model: states -----------------------------------------------------------------------------------------------

test('the button follows the worker, and Apply shows only for a change that needs a restart (never for threads)', () => {
  const machine = machineOf(gpuOf('RTX 4090', 24), 64, 16);
  const started = settingsOf({ device: 'gpu' });
  const running = (over, extra = {}) => d.buildDonatePanel({ machine, settings: settingsOf({ device: 'gpu', ...over }), state: 'contributing', started, ...extra });

  assert.deepEqual(running({}).button, { label: 'Stop donating', action: 'stop', disabled: false, hint: '' });
  assert.equal(running({}).apply.visible, false, 'what is running is what is set');
  assert.equal(running({ maxCpuThreads: 6 }).apply.visible, false, 'threads apply live');
  assert.equal(running({ maxVramGb: 12 }).apply.visible, true);
  assert.equal(running({ maxRamGb: 32 }).apply.visible, true);
  assert.equal(running({ device: 'both', alsoCpuRamGb: 16 }).apply.visible, true);
  assert.equal(running({ maxVramGb: 12 }).apply.label, 'Apply (restarts the worker)');
  assert.equal(d.buildDonatePanel({ machine, settings: settingsOf({ maxVramGb: 12 }), state: 'idle', started }).apply.visible, false, 'nothing runs, nothing to restart');
  assert.equal(running({ maxVramGb: 12 }, { started: null }).apply.visible, false, 'a worker whose settings are not known');

  const external = running({}, { external: true });
  assert.equal(external.locked, true);
  assert.match(external.lockedNote, /started outside VS Code/);
  assert.equal(external.apply.visible, false);
  assert.equal(panelFor(machine, {}, { state: 'starting' }).button.action, 'stop');
});

test('with nothing switched on the button is disabled, with a hint', () => {
  const machine = machineOf(gpuOf('RTX 4060', 8), 32);
  const panel = panelFor(machine, {}, { choice: { gpuOn: false, vramGb: 8, gpuRamGb: 0, cpuOn: false, cpuRamGb: 16, threads: -1 } });
  assert.equal(panel.button.disabled, true);
  assert.equal(panel.button.hint, 'Turn on your GPU or your CPU first.');
  assert.match(texts(panel)[0], /Turn on your GPU or your CPU to donate/);
});

// ---- settings to a choice, and back ---------------------------------------------------------------------------------

test('what dlne.worker.device means for the switches', () => {
  const both = machineOf(gpuOf('RTX 4060', 8), 32);
  const on = (machine, device) => { const ch = d.choiceFromSettings(settingsOf({ device }), machine); return [ch.gpuOn, ch.cpuOn]; };
  assert.deepEqual(on(both, 'auto'), [true, false]);
  assert.deepEqual(on(both, 'gpu'), [true, false]);
  assert.deepEqual(on(both, 'cpu'), [false, true]);
  assert.deepEqual(on(both, 'both'), [true, true]);
  const cpuOnly = machineOf(NO_GPU, 32);
  assert.deepEqual(on(cpuOnly, 'auto'), [false, true]);
  assert.deepEqual(on(cpuOnly, 'both'), [false, true], '"both" without a GPU is "cpu"');
  assert.deepEqual(on(cpuOnly, 'gpu'), [false, true], 'a stale "gpu" on a machine without one does what "auto" does');
});

test('RAM settings are read the way the engine uses them', () => {
  const machine = machineOf(gpuOf('RTX 4090', 24), 64);
  // dlne.worker.maxRamGb is the GPU's model for gpu and both, and the CPU's amount for cpu.
  assert.equal(d.choiceFromSettings(settingsOf({ device: 'gpu', maxRamGb: 24 }), machine).gpuRamGb, 24);
  assert.equal(d.choiceFromSettings(settingsOf({ device: 'cpu', maxRamGb: 24 }), machine).gpuRamGb, 0);
  assert.equal(d.choiceFromSettings(settingsOf({ device: 'cpu', maxRamGb: 24 }), machine).cpuRamGb, 24);
  assert.equal(d.choiceFromSettings(settingsOf({ device: 'both', maxRamGb: 24, alsoCpuRamGb: 8 }), machine).cpuRamGb, 8);
  assert.equal(d.choiceFromSettings(settingsOf({ device: 'gpu', maxRamGb: 8 }), machine).gpuRamGb, 0, 'under the first stop it is Auto');
  assert.equal(d.choiceFromSettings(settingsOf({ device: 'gpu' }), machine).gpuRamGb, 0, 'unset is Auto');
  assert.equal(d.choiceFromSettings(settingsOf({ device: 'cpu' }), machine).cpuRamGb, 24, 'unset: what DLNE suggests');
  assert.equal(d.choiceFromSettings(settingsOf({ device: 'gpu', maxVramGb: 100 }), machine).vramGb, 24, 'clamped to the card');
});

test('readDonateSettings: a maxRamGb the owner never set is not a choice', () => {
  stub.reset();
  const config = stub.vscode.workspace.getConfiguration('dlne');
  assert.deepEqual(d.readDonateSettings(config), settingsOf());
  Object.assign(stub.state.settings, { 'dlne.worker.maxRamGb': 24, 'dlne.worker.maxCpuThreads': 6, 'dlne.worker.device': 'both', 'dlne.worker.allowCodingMember': false });
  const read = d.readDonateSettings(config);
  assert.equal(read.maxRamGb, 24);
  assert.equal(read.maxCpuThreads, 6);
  assert.equal(read.device, 'both');
  assert.equal(read.allowCodingMember, false);
});

// ---- never trusting the webview --------------------------------------------------------------------------------------

test('a posted choice is clamped to the machine and snapped to the stops', () => {
  const machine = machineOf(gpuOf('RTX 4090', 24), 64, 16);
  const fallback = d.choiceFromSettings(settingsOf({ device: 'both', alsoCpuRamGb: 16 }), machine);
  const check = (raw) => d.validateChoice(raw, machine, fallback);

  const wild = check({ gpuOn: true, vramGb: 999, gpuRamGb: 1000, cpuOn: true, cpuRamGb: 1000, threads: 999 });
  assert.deepEqual(wild, { gpuOn: true, vramGb: 24, gpuRamGb: 56, cpuOn: true, cpuRamGb: 24, threads: 16 });

  const low = check({ vramGb: -5, gpuRamGb: -3, cpuRamGb: 1, threads: 1 });
  assert.equal(low.vramGb, 8, 'the card is never offered below 8 GB');
  assert.equal(low.gpuRamGb, 0);
  assert.equal(low.cpuRamGb, 8);
  assert.equal(low.threads, 2, 'one thread is not on the slider');

  assert.equal(check({ threads: 0 }).threads, -1, 'zero threads is never sent on: it would mean ONE thread to the engine');
  assert.equal(check({ threads: -7 }).threads, -1);
  assert.equal(check({ threads: 3.7 }).threads, 4);
  assert.equal(check({ vramGb: 12.6 }).vramGb, 13);
  assert.equal(check({ gpuRamGb: 20 }).gpuRamGb, 24, 'between two stops: the nearer, the larger on a tie');
  assert.equal(check({ gpuRamGb: 18 }).gpuRamGb, 16);
  assert.equal(check({ cpuRamGb: 11 }).cpuRamGb, 8);
  assert.equal(check({ cpuRamGb: 13 }).cpuRamGb, 16);

  // Not numbers, not booleans, not an object: the fallback stands.
  for (const junk of [{ vramGb: '12', gpuRamGb: NaN, cpuRamGb: Infinity, threads: null, gpuOn: 'yes', cpuOn: 1 }, 'text', null, undefined, 42, [1, 2], () => 0]) {
    assert.deepEqual(check(junk), fallback, `${String(junk)} changes nothing`);
  }
});

test('a toggle for something the machine cannot do is off, and its amounts are zero', () => {
  const noGpu = machineOf(NO_GPU, 8, 2);
  const fallback = d.choiceFromSettings(settingsOf(), noGpu);
  const checked = d.validateChoice({ gpuOn: true, cpuOn: true, vramGb: 12, gpuRamGb: 24, cpuRamGb: 16, threads: 8 }, noGpu, fallback);
  assert.deepEqual(checked, { gpuOn: false, vramGb: 0, gpuRamGb: 0, cpuOn: false, cpuRamGb: 0, threads: 2 }, 'threads stay within the 2 logical CPUs');
  const oneCpu = d.validateChoice({ threads: 8 }, machineOf(NO_GPU, 8, 1), fallback);
  assert.equal(oneCpu.threads, -1, 'under 2 logical CPUs there is only Auto');
});

// ---- the settings a choice writes ------------------------------------------------------------------------------------

test('the switches map to dlne.worker.device and the amounts to their settings', () => {
  const machine = machineOf(gpuOf('RTX 4090', 24), 64, 16);
  const choice = { gpuOn: true, vramGb: 12, gpuRamGb: 32, cpuOn: true, cpuRamGb: 16, threads: 8 };
  const writes = (over) => Object.fromEntries(d.settingsToWrite({ ...choice, ...over }, machine));

  assert.deepEqual(writes({}), {
    'worker.device': 'both', 'worker.maxVramGb': 12, 'worker.maxRamGb': 32, 'worker.alsoCpuRamGb': 16, 'worker.maxCpuThreads': 8,
  });
  assert.deepEqual(writes({ cpuOn: false }), {
    'worker.device': 'gpu', 'worker.maxVramGb': 12, 'worker.maxRamGb': 32, 'worker.maxCpuThreads': 8,
  });
  assert.deepEqual(writes({ gpuOn: false }), { 'worker.device': 'cpu', 'worker.maxRamGb': 16, 'worker.maxCpuThreads': 8 }, 'the CPU amount goes to maxRamGb when the CPU is alone');
  assert.deepEqual(writes({ gpuOn: false, cpuOn: false }), { 'worker.maxCpuThreads': 8 }, 'neither: nothing to start, no device written');

  assert.equal(writes({ vramGb: 24 })['worker.maxVramGb'], -1, 'the top of the slider is "all"');
  assert.ok('worker.maxRamGb' in writes({ gpuRamGb: 0 }) && writes({ gpuRamGb: 0 })['worker.maxRamGb'] === undefined, 'Auto clears the setting, so the coding job counts the free RAM');
  assert.equal(writes({ threads: -1 })['worker.maxCpuThreads'], -1);
  for (const over of [{}, { threads: -1 }, { cpuOn: false }, { gpuOn: false }]) {
    assert.notEqual(writes(over)['worker.maxCpuThreads'], 0, 'automatic is -1, never 0');
  }
});

test('writing a choice and reading it back gives the same donation', () => {
  const machine = machineOf(gpuOf('RTX 4090', 24), 64, 16);
  for (const choice of [
    { gpuOn: true, vramGb: 12, gpuRamGb: 32, cpuOn: true, cpuRamGb: 16, threads: 8 },
    { gpuOn: true, vramGb: 24, gpuRamGb: 0, cpuOn: false, cpuRamGb: 24, threads: -1 },
    { gpuOn: false, vramGb: 24, gpuRamGb: 0, cpuOn: true, cpuRamGb: 8, threads: 2 },
  ]) {
    const settings = settingsOf();
    for (const [key, value] of d.settingsToWrite(choice, machine)) {
      const field = { 'worker.device': 'device', 'worker.maxVramGb': 'maxVramGb', 'worker.maxRamGb': 'maxRamGb', 'worker.alsoCpuRamGb': 'alsoCpuRamGb', 'worker.maxCpuThreads': 'maxCpuThreads' }[key];
      settings[field] = value === undefined ? null : value;
    }
    const back = d.choiceFromSettings(settings, machine);
    assert.equal(back.gpuOn, choice.gpuOn);
    assert.equal(back.cpuOn, choice.cpuOn);
    assert.equal(back.threads, choice.threads);
    if (choice.gpuOn) assert.deepEqual([back.vramGb, back.gpuRamGb], [choice.vramGb, choice.gpuRamGb]);
    if (choice.cpuOn) assert.equal(back.cpuRamGb, choice.cpuRamGb);
  }
});

test('the draft the panel keeps follows the settings: it is dropped when they were changed elsewhere', () => {
  const machine = machineOf(gpuOf('RTX 4060', 8), 32, 8);
  const settings = settingsOf({ device: 'gpu' });
  const draft = { ...d.choiceFromSettings(settings, machine), cpuRamGb: 8 };
  assert.equal(d.reconcileDraft(draft, settings, machine), draft, 'the amount of the job that is off is kept');
  assert.equal(d.reconcileDraft(draft, settingsOf({ device: 'cpu' }), machine), undefined);
  assert.equal(d.reconcileDraft(draft, settingsOf({ device: 'gpu', maxCpuThreads: 4 }), machine), undefined);
  const nothing = { ...draft, gpuOn: false };
  assert.equal(d.reconcileDraft(nothing, settings, machine), nothing, 'nothing on has no setting to agree with');
  assert.equal(d.reconcileDraft(undefined, settings, machine), undefined);
});

// ---- the setting itself ----------------------------------------------------------------------------------------------

test('dlne.worker.maxCpuThreads: a number, automatic by default, application-scoped, and in the README', () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(ROOT, 'package.json'), 'utf8'));
  const setting = manifest.contributes.configuration.properties['dlne.worker.maxCpuThreads'];
  assert.equal(setting.type, 'number');
  assert.equal(setting.default, -1);
  assert.equal(setting.scope, 'application', 'a repository cannot set it');
  assert.equal(setting.minimum, -1);
  assert.match(setting.description, /-1 \(the default\) lets the engine decide/);
  const readme = fs.readFileSync(path.join(ROOT, 'README.md'), 'utf8');
  assert.match(readme, /\| `dlne\.worker\.maxCpuThreads` \| `-1` \|/);
  assert.match(readme, /Donate compute/);
});

test('MAX_CPU_THREADS in config.txt: written for a limit, -1 for automatic, never 0', () => {
  const update = c.maxCpuThreadsConfigUpdate;
  assert.equal(update('A=1\n', 8), 'A=1\nMAX_CPU_THREADS=8\n');
  assert.equal(update('A=1\nmax_cpu_threads = 4\nB=2\n', 8), 'A=1\nmax_cpu_threads=8\nB=2\n', 'in place, whatever the casing');
  assert.equal(update('A=1\nMAX_CPU_THREADS=8\n', -1), 'A=1\nMAX_CPU_THREADS=-1\n', 'automatic after a limit: -1');
  assert.equal(update('A=1\n', -1), null, 'automatic and no key: the file is not touched');
  assert.equal(update('', -1), null);
  assert.equal(update('MAX_CPU_THREADS=8\n', 8), null, 'already right');
  assert.equal(update('A=1\nMAX_CPU_THREADS=8\n', 0), 'A=1\nMAX_CPU_THREADS=-1\n', '0 would mean one thread to the engine');
  assert.equal(update('A=1\n', 0), null);
  assert.equal(update('A=1\n# MAX_CPU_THREADS=3\n', -1), null, 'a comment is not the key');
  for (const odd of [undefined, null, 'eight', NaN, -3]) {
    assert.doesNotMatch(update('MAX_CPU_THREADS=8\n', odd) ?? '', /MAX_CPU_THREADS=0/);
  }
  assert.equal(c.MAX_CPU_THREADS_KEY, 'MAX_CPU_THREADS');
});

function engine(configText) {
  const root = path.join(HOME, '.dlne', 'runtime');
  fs.rmSync(root, { recursive: true, force: true });
  fs.mkdirSync(path.join(root, 'config'), { recursive: true });
  fs.writeFileSync(path.join(root, 'dlne_server.py'), '');
  if (configText !== undefined) fs.writeFileSync(path.join(root, 'config', 'config.txt'), configText);
  return { root, config: path.join(root, 'config', 'config.txt'), read: () => fs.readFileSync(path.join(root, 'config', 'config.txt'), 'utf8') };
}

function manager(settings = {}, gpu = gpuOf('RTX 4060', 8)) {
  stub.reset();
  Object.assign(stub.state.settings, settings);
  const wm = new WorkerManager({ subscriptions: [], globalState: undefined });
  wm.localGpu = gpu;
  wm.currentState = 'idle';
  wm.externalWorker = false;
  return wm;
}

test('the manager writes the thread limit to config.txt safely, and follows the setting while the worker runs', () => {
  const e = engine('COORDINATOR_URL=https://x\n');
  let listener;
  stub.vscode.workspace.onDidChangeConfiguration = (fn) => { listener = fn; return { dispose() { listener = undefined; } }; };
  try {
    const wm = manager();
    wm.syncMaxCpuThreadsSetting(e.root);
    assert.equal(e.read(), 'COORDINATOR_URL=https://x\n', 'automatic with no key leaves the file alone');

    stub.state.settings['dlne.worker.maxCpuThreads'] = 6;
    listener({ affectsConfiguration: (key) => key === 'dlne.serverUrl' });
    assert.equal(e.read(), 'COORDINATOR_URL=https://x\n', 'an unrelated setting changes nothing');
    listener({ affectsConfiguration: (key) => key === 'dlne.worker.maxCpuThreads' });
    assert.equal(e.read(), 'COORDINATOR_URL=https://x\nMAX_CPU_THREADS=6\n', 'live: no restart');

    stub.state.settings['dlne.worker.maxCpuThreads'] = -1;
    wm.syncMaxCpuThreadsSetting(e.root);
    assert.equal(e.read(), 'COORDINATOR_URL=https://x\nMAX_CPU_THREADS=-1\n');
    assert.deepEqual(fs.readdirSync(path.dirname(e.config)).sort(), ['config.txt'], 'no scratch file is left behind');
    wm.dispose();
  } finally {
    delete stub.vscode.workspace.onDidChangeConfiguration;
  }
});

test('no engine installed yet: the thread limit writes nothing and does not throw; an unwritable config is logged', () => {
  fs.rmSync(path.join(HOME, '.dlne'), { recursive: true, force: true });
  const wm = manager({ 'dlne.worker.maxCpuThreads': 4 });
  assert.doesNotThrow(() => wm.syncMaxCpuThreadsSetting());
  assert.equal(fs.existsSync(path.join(HOME, '.dlne')), false);

  const e = engine('');
  fs.rmSync(e.config);
  fs.mkdirSync(e.config); // a directory where the file should be
  assert.doesNotThrow(() => wm.syncMaxCpuThreadsSetting(e.root));
  assert.match(stub.state.channels[0].text(), /Could not update MAX_CPU_THREADS/);
  wm.dispose();
});

// ---- the clock notice --------------------------------------------------------------------------------------------------

test('a wrong clock is announced once per offset, in the worker\'s own words', () => {
  const wm = manager();
  const line = (offset) => `[12:00:00] heartbeat\nDLNE_CLOCK: {"offset_seconds": ${offset}}\n`;
  wm.noteWorkerOutput(line(18000), 'stdout');
  assert.equal(stub.state.warningMessages.length, 1);
  assert.equal(stub.state.warningMessages[0].message, c.clockMessage(18000));
  assert.match(stub.state.warningMessages[0].message, /5 h behind the swarm's/);

  wm.noteWorkerOutput(line(18000), 'stdout');
  wm.noteWorkerOutput(line(18000), 'stderr');
  assert.equal(stub.state.warningMessages.length, 1, 'the same offset is not announced again');

  wm.noteWorkerOutput(line(-3600), 'stdout');
  assert.equal(stub.state.warningMessages.length, 2, 'another offset is news');
  assert.match(stub.state.warningMessages[1].message, /1 h ahead of the swarm's/);

  wm.noteWorkerOutput(line(12), 'stdout');
  wm.noteWorkerOutput('DLNE_CLOCK: {"offset_seconds": "soon"}\nDLNE_CLOCK: nonsense\n', 'stdout');
  assert.equal(stub.state.warningMessages.length, 2, 'inside the 30 s window, or not a number: nothing to say');

  wm.noteWorkerOutput('DLNE_CLOCK: {"offset_seconds": 90', 'stdout');
  assert.equal(stub.state.warningMessages.length, 2, 'half a line waits for the rest');
  wm.noteWorkerOutput('00}\nDLNE_CLOCK: {"offset_seconds": 7200}\n', 'stdout');
  assert.equal(stub.state.warningMessages.length, 4, 'both lines of one chunk are read');
  wm.dispose();
});

test('each worker run announces its offsets afresh', async () => {
  const e = engine('NETWORK_KEY=abc\n');
  // A "python" that is node: it prints the same clock line twice and another once, then ends.
  fs.writeFileSync(
    path.join(e.root, 'dlne_server.py'),
    `const line = (o) => 'DLNE_CLOCK: {"offset_seconds": ' + o + '}\\n';
     process.stdout.write(line(18000) + line(18000));
     setTimeout(() => { process.stdout.write(line(3600)); setTimeout(() => process.exit(0), 100); }, 100);`
  );
  const wm = manager({ 'dlne.worker.pythonPath': process.execPath, 'dlne.worker.device': 'gpu', 'dlne.autoUpdate': false, 'dlne.serverUrl': 'http://127.0.0.1:9' });
  wm.isSystemdActive = () => false;
  wm.isProcessActive = () => false;
  wm.isPortListening = async () => false;
  wm.ensureRpcBinaries = async () => {};
  const finished = async () => {
    const deadline = Date.now() + 15000;
    while (wm.workerProcess && Date.now() < deadline) await new Promise((r) => setTimeout(r, 50));
  };
  try {
    assert.equal(await wm.startWorker(), true);
    await finished();
    assert.equal(stub.state.warningMessages.length, 2, '18000 once, 3600 once');
    assert.equal(await wm.startWorker(), true);
    await finished();
    assert.equal(stub.state.warningMessages.length, 4, 'the next run says them again');
  } finally {
    if (wm.workerProcess) wm.workerProcess.kill();
    wm.dispose();
  }
});

// ---- the sidebar: messages ----------------------------------------------------------------------------------------------

/** Runs `fn` as a 24-core computer with this much RAM, whatever the host is. */
async function onHost(ramGb, cpus, fn) {
  const realTotalmem = os.totalmem;
  const realCpus = os.cpus;
  os.totalmem = () => ramGb * GIB * 0.94;
  os.cpus = () => new Array(cpus).fill({});
  try {
    return await fn();
  } finally {
    os.totalmem = realTotalmem;
    os.cpus = realCpus;
  }
}

function sidebar(wm) {
  const posted = [];
  let onMessage;
  let statusListener;
  const provider = new SidebarProvider({ fsPath: ROOT }, {}, { onStatusChange: (fn) => { statusListener = fn; }, getStatus: () => wm.getStatus(), applyDonateChoice: (a, b) => wm.applyDonateChoice(a, b), toggle: (o) => wm.toggle(o), restartWorker: () => wm.restartWorker() });
  provider.resolveWebviewView(
    { webview: { options: {}, html: '', asWebviewUri: (u) => u, postMessage: (m) => posted.push(m), onDidReceiveMessage: (fn) => { onMessage = fn; } } },
    {},
    {}
  );
  return { provider, posted, send: (m) => onMessage(m), statusListener };
}

const lastModel = (posted) => [...posted].reverse().find((m) => m.type === 'donatePanel').model;

test('the panel is sent with the status, on mount and on every change', () => onHost(32, 8, () => {
  const wm = manager({}, gpuOf('RTX 4060', 8));
  const s = sidebar(wm);
  assert.equal(s.posted[0].type, 'restoreHistory');
  assert.equal(s.posted[0].donate.gpu.detail, 'RTX 4060, 8 GB');
  s.statusListener({ ...wm.getStatus(), state: 'contributing' });
  assert.equal(s.posted[1].type, 'workerStatus');
  assert.equal(s.posted[1].donate.button.label, 'Stop donating');
  wm.dispose();
}));

test('sliders moving only preview; letting go checks the values, writes the settings and answers with the panel', () => onHost(64, 24, async () => {
  const wm = manager({}, gpuOf('RTX 4090', 24));
  const s = sidebar(wm);

  await s.send({ type: 'donateChoice', commit: false, choice: { gpuOn: true, vramGb: 12, gpuRamGb: 32, cpuOn: false, cpuRamGb: 16, threads: 6 } });
  assert.deepEqual(stub.state.updates, [], 'a drag writes nothing');
  assert.equal(lastModel(s.posted).gpu.vram.value, 12);
  assert.match(lastModel(s.posted).powers[0].text, /Lending 12 of 24 GB of VRAM/);

  await s.send({ type: 'donateChoice', commit: true, choice: { gpuOn: true, vramGb: 9999, gpuRamGb: 33, cpuOn: true, cpuRamGb: 1, threads: 0 } });
  assert.deepEqual(stub.state.updates.map(([k, v]) => [k, v]), [
    ['dlne.worker.device', 'both'], ['dlne.worker.maxVramGb', -1], ['dlne.worker.maxRamGb', 32], ['dlne.worker.alsoCpuRamGb', 8], ['dlne.worker.maxCpuThreads', -1],
  ], 'clamped to the card, snapped to the stops, and zero threads is automatic');
  const model = lastModel(s.posted);
  assert.deepEqual(model.choice, { gpuOn: true, vramGb: 24, gpuRamGb: 32, cpuOn: true, cpuRamGb: 8, threads: -1 });
  assert.equal(stub.state.settings['dlne.worker.maxCpuThreads'], -1);
  wm.dispose();
}));

test('a switch turned off keeps the other amounts; nothing on is shown as it is and writes no device', () => onHost(64, 24, async () => {
  const wm = manager({ 'dlne.worker.device': 'both', 'dlne.worker.alsoCpuRamGb': 24 }, gpuOf('RTX 4090', 24));
  const s = sidebar(wm);
  await s.send({ type: 'donateChoice', commit: true, choice: { gpuOn: false, vramGb: 24, gpuRamGb: 0, cpuOn: true, cpuRamGb: 24, threads: -1 } });
  assert.equal(stub.state.settings['dlne.worker.device'], 'cpu');
  assert.equal(stub.state.settings['dlne.worker.maxRamGb'], 24);

  stub.state.updates.length = 0;
  await s.send({ type: 'donateChoice', commit: true, choice: { gpuOn: false, cpuOn: false } });
  assert.equal(stub.state.settings['dlne.worker.device'], 'cpu', 'no setting can say "neither"');
  const model = lastModel(s.posted);
  assert.equal(model.gpu.on || model.cpu.on, false, 'the panel shows what was chosen');
  assert.equal(model.button.disabled, true);
  assert.equal(model.choice.cpuRamGb, 24, 'and remembers the amount');
  wm.dispose();
}));

test('Donate sets the settings from the sliders first, so the old RAM offer does not appear', () => onHost(32, 8, async () => {
  const wm = manager({}, NO_GPU);   // CPU only, maxRamGb never set: the default 4 is under the minimum of 8
  let toggled;
  wm.toggle = async (options) => {
    toggled = { options, settings: { ...stub.state.settings } };
    // What startWorker asks next: the plan for this machine. With the amount set, the RAM modal has no reason to open.
    const plan = await wm.planThisMachine();
    toggled.plan = plan;
    return true;
  };
  const s = sidebar(wm);
  await s.send({ type: 'toggleWorker', choice: { gpuOn: false, vramGb: 0, gpuRamGb: 0, cpuOn: true, cpuRamGb: 16, threads: -1 } });
  assert.deepEqual(toggled.options, { fromPanel: true });
  assert.equal(toggled.settings['dlne.worker.device'], 'cpu');
  assert.equal(toggled.settings['dlne.worker.maxRamGb'], 16);
  assert.equal(toggled.plan.needsMoreRam, false);
  assert.deepEqual(stub.state.warningMessages, [], 'no modal');
  wm.dispose();
}));

test('without the panel\'s amount the old modal still asks (a CPU machine with the default 4 GB)', () => onHost(32, 8, async () => {
  const wm = manager({}, NO_GPU);
  const plan = await wm.planThisMachine();
  assert.equal(plan, null, 'the modal was shown, and nobody answered');
  assert.equal(stub.state.warningMessages.length, 1);
  wm.dispose();
}));

test('Stop needs no sliders, Donate with nothing on starts nothing, and Apply restarts after writing', () => onHost(32, 8, async () => {
  const wm = manager({}, gpuOf('RTX 4060', 8));
  const calls = [];
  wm.toggle = async (options) => { calls.push(['toggle', options, stub.state.updates.length]); return true; };
  wm.restartWorker = async () => { calls.push(['restart', stub.state.updates.length]); return true; };
  const s = sidebar(wm);

  await s.send({ type: 'toggleWorker' });
  assert.deepEqual(calls, [['toggle', undefined, 0]], 'a click without values toggles and writes nothing');

  calls.length = 0;
  await s.send({ type: 'toggleWorker', choice: { gpuOn: false, cpuOn: false } });
  assert.deepEqual(calls, [], 'nothing switched on: nothing starts');
  assert.equal(lastModel(s.posted).button.disabled, true);

  await s.send({ type: 'donateApply', choice: { gpuOn: true, vramGb: 8, gpuRamGb: 24, cpuOn: false, cpuRamGb: 16, threads: 4 } });
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], 'restart');
  assert.ok(calls[0][1] > 0, 'the settings were written before the restart');
  assert.equal(stub.state.settings['dlne.worker.maxRamGb'], 24);
  assert.equal(stub.state.settings['dlne.worker.maxCpuThreads'], 4);
  wm.dispose();
}));

test('hostile posts change nothing they should not', () => onHost(32, 8, async () => {
  const wm = manager({}, gpuOf('RTX 4060', 8));
  const s = sidebar(wm);
  for (const choice of ['__proto__', null, 7, [], { gpuOn: { x: 1 }, vramGb: '<script>', threads: {}, __proto__: { polluted: true } }, { constructor: { prototype: { bad: 1 } } }]) {
    await s.send({ type: 'donateChoice', commit: true, choice });
    await s.send({ type: 'donateChoice', commit: false });
  }
  const model = lastModel(s.posted);
  assert.equal(model.choice.gpuOn, true);
  assert.equal({}.polluted, undefined);
  for (const [, value] of stub.state.updates) assert.ok(value === undefined || ['string', 'number'].includes(typeof value));
  wm.dispose();
}));

test('Stop while the old worker shuts down: the restart waits for it, and the stop is quiet', async () => {
  const wm = manager({ 'dlne.worker.device': 'gpu' });
  const order = [];
  wm.stopWorker = async (quiet) => { order.push(['stop', quiet]); };
  wm.isProcessActive = () => false;
  let listening = 2;
  wm.isPortListening = async () => { order.push(['port']); return listening-- > 0; };
  wm.startWorker = async (options) => { order.push(['start', options]); return true; };
  assert.equal(await wm.restartWorker(), true);
  assert.deepEqual(order, [['stop', true], ['port'], ['port'], ['port'], ['start', { interactive: false }]]);
  wm.dispose();
});

test('from the panel, "Lend my GPU" in the first-time prompt keeps what the switches chose', async () => {
  for (const [fromPanel, expected] of [[false, 'gpu'], [true, 'both']]) {
    const wm = manager({ 'dlne.worker.device': 'both' });
    wm.context.globalState = { get: () => false, update: async () => {} };
    stub.state.warningChoice = c.LEND_GPU;
    assert.equal(await wm.confirmLendingGpu(fromPanel), true);
    assert.equal(stub.state.settings['dlne.worker.device'], expected);
    wm.dispose();
  }
});

// ---- the page ------------------------------------------------------------------------------------------------------------

const HOSTILE = '</script><img src=x onerror=alert(1)>"\'&';

function pageFor(status) {
  stub.reset();
  const provider = new SidebarProvider({ fsPath: ROOT }, {}, { onStatusChange() {}, getStatus: () => status });
  return provider._getHtmlForWebview();
}

test('the page keeps its CSP: one nonce\'d script, no inline handlers, nothing a driver wrote becomes markup', () => onHost(32, 8, () => {
  const html = pageFor({ state: 'idle', gpu: gpuOf(HOSTILE, 8), cpuPool: false, vramContributedGb: 0 });

  const csp = /<meta http-equiv="Content-Security-Policy" content="([^"]*)">/.exec(html)[1];
  const nonce = /script-src 'nonce-([^']+)'/.exec(csp)[1];
  assert.match(csp, /default-src 'none'/);
  assert.doesNotMatch(csp, /script-src[^;]*unsafe/);
  const scripts = [...html.matchAll(/<script\b[^>]*>/g)].map((m) => m[0]);
  assert.deepEqual(scripts, [`<script nonce="${nonce}">`], 'the one script block');
  assert.equal(html.split('</script>').length - 1, 1, 'and nothing a driver wrote ends it');

  const markup = html.replace(/<script[\s\S]*?<\/script>/g, '').replace(/<style[\s\S]*?<\/style>/g, '');
  assert.doesNotMatch(markup, /\son[a-z]+\s*=/i, 'no inline event handler');
  assert.doesNotMatch(markup, /javascript:/i);
  assert.ok(!markup.includes(HOSTILE) && !markup.includes('<img src=x'), 'the name is not in the markup at all');

  const script = /<script\b[^>]*>([\s\S]*)<\/script>/.exec(html)[1];
  assert.ok(!script.includes(HOSTILE), 'in the script the name only appears escaped');
  assert.ok(script.includes('\\u003c/script\\u003e\\u003cimg src=x onerror=alert(1)\\u003e'));
  const model = JSON.parse(/donatePanel\.update\((\{.*\})\);/.exec(script)[1]);
  assert.equal(model.gpu.detail, `${HOSTILE}, 8 GB`, 'and parses back to exactly what the driver said');
}));

/** A page stand-in that records what the panel script does, and refuses innerHTML. */
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
  const nodes = () => root.all();
  return {
    root, posted, panel,
    ranges: () => nodes().filter((el) => el.tag === 'input' && el.type === 'range'),
    boxes: () => nodes().filter((el) => el.tag === 'input' && el.type === 'checkbox'),
    button: (cls) => nodes().find((el) => el.tag === 'button' && el.className.includes(cls)),
    text: () => root.textContent,
  };
}

test('the panel script draws the model as text, posts the sliders, and never touches innerHTML', () => {
  const machine = machineOf(gpuOf(HOSTILE, 24), 64, 16);
  const model = panelFor(machine, { device: 'both', alsoCpuRamGb: 16, maxRamGb: 32 });
  const page = fakePage();
  try {
    page.panel.update(model);
    assert.ok(page.text().includes(`${HOSTILE}, 24 GB`), 'the name is shown, as text');
    assert.ok(page.text().includes('Coding member: your GPU runs the Qwen3.6 coding model'));
    const [vram, gpuRam, cpuRam, threads] = page.ranges();
    assert.deepEqual([vram.min, vram.max, vram.value], ['8', '24', '24']);
    assert.deepEqual([gpuRam.min, gpuRam.max, gpuRam.value], ['0', '6', '3'], 'stops 0,16,24,32,...: 32 GB is the fourth');
    assert.equal(cpuRam.value, '1');
    assert.equal(threads.value, '0');

    // A drag previews; letting go commits. Values are the stops', not the slider's positions.
    vram.value = '12';
    vram.fire('input');
    assert.equal(page.text().includes('12 GB'), true, 'the value shows at once');
    gpuRam.value = '2';
    threads.value = '7';
    vram.fire('change');
    assert.deepEqual(page.posted.map((m) => [m.type, m.commit]), [['donateChoice', false], ['donateChoice', true]]);
    assert.deepEqual(page.posted[1].choice, { gpuOn: true, vramGb: 12, gpuRamGb: 24, cpuOn: true, cpuRamGb: 16, threads: 8 });

    const [gpuBox, cpuBox] = page.boxes();
    cpuBox.checked = false;
    cpuBox.fire('change');
    assert.equal(page.posted[2].choice.cpuOn, false);
    assert.equal(gpuBox.checked, true);

    // The big button: Donate posts the sliders, Stop posts nothing else.
    page.button('dn-donate').fire('click');
    assert.equal(page.posted[3].type, 'toggleWorker');
    assert.equal(page.posted[3].choice.gpuOn, true);
    page.panel.update({ ...model, button: { label: 'Stop donating', action: 'stop', disabled: false, hint: '' }, apply: { visible: true, disabled: false, label: d.APPLY_LABEL } });
    assert.equal(page.button('dn-donate').textContent, 'Stop donating');
    page.button('dn-donate').fire('click');
    assert.deepEqual(page.posted[4], { type: 'toggleWorker' });
    assert.equal(page.button('dn-apply').style.display, 'block');
    page.button('dn-apply').fire('click');
    assert.equal(page.posted[5].type, 'donateApply');

    // A slider being dragged is not moved from under the pointer by an update.
    document.activeElement = vram;
    vram.value = '15';
    page.panel.update(model);
    assert.equal(vram.value, '15');
  } finally {
    delete global.document;
  }
});

test('the panel script shows the reasons, and disables what the machine cannot do', () => {
  const page = fakePage();
  try {
    page.panel.update(panelFor(machineOf(NO_GPU, 8)));
    const [gpuBox, cpuBox] = page.boxes();
    assert.equal(gpuBox.disabled, true);
    assert.equal(cpuBox.disabled, true);
    assert.ok(page.text().includes('No usable GPU was found on this computer.'));
    assert.equal(page.button('dn-donate').disabled, true);
    assert.ok(page.text().includes('This computer cannot donate yet.'));
    page.panel.update(panelFor(machineOf(gpuOf('RTX 4060', 8), 32), {}, { external: true, state: 'contributing' }));
    assert.equal(page.boxes()[0].disabled, true, 'a worker from outside VS Code: its settings are not these');
    assert.ok(page.text().includes('started outside VS Code'));
  } finally {
    delete global.document;
  }
});

test('the sources never write HTML from the model', () => {
  const view = fs.readFileSync(path.join(ROOT, 'src', 'views', 'donateView.ts'), 'utf8');
  assert.doesNotMatch(view, /innerHTML|insertAdjacentHTML|outerHTML|document\.write|\son[a-z]+\s*=/);
});

test('the changelog announces the panel and the new setting', () => {
  const changelog = fs.readFileSync(path.join(ROOT, 'CHANGELOG.md'), 'utf8');
  const entry = changelog.slice(changelog.indexOf('## [1.3.4]'), changelog.indexOf('## [1.3.3]'));
  assert.match(entry, /### Added[\s\S]*\*\*Donate compute\.\*\*/);
  assert.match(entry, /dlne\.worker\.maxCpuThreads/);
});
