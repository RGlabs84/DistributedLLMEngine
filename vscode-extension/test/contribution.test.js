'use strict';
/**
 * What a machine contributes (dlne.worker.device), how a CPU-only machine is brought up to
 * the coordinator's floor before it starts (instead of being refused with nothing on screen),
 * how much RAM it is offered to lend, and what a CPU contributor is told.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

require('./helpers/vscode-stub');
const {
  planContribution, parseRefusal, CPU_MIN_RAM_GB, CPU_LADDER, REFUSAL_MARKER, CPU_BELOW_MINIMUM_EXIT_CODE,
  installedRamGb, defaultRamOffer, ramChoices, lendLabel, ramOfferText, ramTooSmallText, workerStartedNotice,
  contributionCopy, contributionTooltip, CPU_POOL_HEADLINE, CPU_POOL_PERK,
  isAllowedIntegratedGpu, countsAsGpu, parseVulkanSummary, pickVulkanGpu, cpuReason,
} = require('../dist/worker/contribution');
const pkg = require('../package.json');

test('auto with a GPU contributes the GPU, as before', () => {
  assert.deepEqual(planContribution('auto', true, '', 4), { cpuOnly: false, allowedGpus: '', needsMoreRam: false });
  assert.deepEqual(planContribution(undefined, true, '0,1', 4), { cpuOnly: false, allowedGpus: '0,1', needsMoreRam: false });
});

test('auto without a GPU contributes the CPU and asks for RAM below the floor', () => {
  assert.deepEqual(planContribution('auto', false, '', 4), { cpuOnly: true, allowedGpus: '', needsMoreRam: true });
  assert.equal(planContribution('auto', false, '', 8).needsMoreRam, false);
  assert.equal(planContribution('auto', false, '', 7.9).needsMoreRam, true);
});

test('cpu hides the GPU from the engine even when there is one', () => {
  assert.deepEqual(planContribution('cpu', true, '0', 12), { cpuOnly: true, allowedGpus: 'none', needsMoreRam: false });
  assert.equal(planContribution('cpu', true, '', 4).needsMoreRam, true);
});

test('gpu without a GPU refuses to start, and says how to fix it', () => {
  const plan = planContribution('gpu', false, '', 12);
  assert.match(plan.error, /no GPU/);
  assert.match(plan.error, /"auto" or "cpu"/);
});

test('an unknown device setting behaves as auto', () => {
  assert.deepEqual(planContribution('tpu', true, '', 4), planContribution('auto', true, '', 4));
});

test('a missing or broken RAM setting counts as below the floor', () => {
  assert.equal(planContribution('cpu', false, '', NaN).needsMoreRam, true);
  assert.equal(planContribution('cpu', false, '', undefined).needsMoreRam, true);
});

test('the floor matches the coordinator and the setting', () => {
  assert.equal(CPU_MIN_RAM_GB, 8);
  const device = pkg.contributes.configuration.properties['dlne.worker.device'];
  assert.deepEqual(device.enum, ['auto', 'gpu', 'cpu']);
  assert.equal(device.default, 'auto');
  const configPy = fs.readFileSync(path.join(__dirname, '..', '..', 'engine', 'config.py'), 'utf8');
  assert.match(configPy, /"cpu_min_ram_gb": 8\.0/);
});

test('the refusal marker and exit code match the worker', () => {
  const workerPy = fs.readFileSync(path.join(__dirname, '..', '..', 'engine', 'worker.py'), 'utf8');
  assert.match(workerPy, new RegExp(`REFUSAL_MARKER = "${REFUSAL_MARKER}"`));
  assert.match(workerPy, new RegExp(`CPU_BELOW_MINIMUM_EXIT_CODE = ${CPU_BELOW_MINIMUM_EXIT_CODE}\\b`));
});

test('the refusal reason is read out of the worker output', () => {
  const out = 'INFO something\nDLNE_REFUSAL: a CPU-only node needs 4 physical CPU cores (this machine has 2).\nmore\n';
  assert.equal(parseRefusal(out), 'a CPU-only node needs 4 physical CPU cores (this machine has 2).');
  assert.equal(parseRefusal('[12:00] DLNE_REFUSAL: reason with prefix\r\n'), 'reason with prefix');
  assert.equal(parseRefusal('nothing here'), null);
  assert.equal(parseRefusal('DLNE_REFUSAL:   '), null);
  assert.equal(parseRefusal(`DLNE_REFUSAL: ${'x'.repeat(1000)}`).length, 400);
});

// ---- how much RAM a CPU-only machine is offered ----------------------------------------------

test('the RAM offer follows the machine\'s total RAM (boundaries at 16, 32 and 48 GB)', () => {
  const table = [
    // [total GB, offer]
    [128, 24], [64, 24], [48, 24],    // 48 GB and up: 24
    [47.9, 16], [40, 16], [32, 16],   // 32 GB up to 48: 16
    [31.9, 12], [24, 12],             // 16 GB up to 32: 12 ...
    [16, 8],                          // ... but never more than half the machine: 16 GB lends 8
  ];
  for (const [total, offer] of table) {
    assert.equal(defaultRamOffer(total), offer, `${total} GB machine`);
  }
});

test('the offer is never more than half the machine\'s RAM', () => {
  assert.equal(defaultRamOffer(23), 11);   // 12 would be more than half of 23
  assert.equal(defaultRamOffer(20), 10);
  assert.equal(defaultRamOffer(17), 8);    // half is 8.5: whole GB, rounded down
  for (let total = 16; total <= 256; total += 0.5) {
    assert.ok(defaultRamOffer(total) <= total / 2, `${total} GB machine`);
    assert.ok(defaultRamOffer(total) >= CPU_MIN_RAM_GB, `${total} GB machine`);
  }
});

test('a machine that cannot lend the minimum within half its RAM is offered nothing', () => {
  for (const total of [15.9, 12, 8, 7.9, 4, 0, -1, NaN, undefined]) {
    assert.equal(defaultRamOffer(total), null, `${total} GB machine`);
    assert.deepEqual(ramChoices(total), [], `${total} GB machine`);
  }
  assert.match(ramTooSmallText(12), /about 12 GB of RAM/);
  assert.match(ramTooSmallText(12), /needs 8 GB lent/);
  assert.match(ramTooSmallText(12), /never offers more than half/);
  assert.match(ramTooSmallText(12), /16 GB or more/);
  assert.match(ramTooSmallText(12), /dlne\.worker\.maxRamGb/);
});

test('the amounts offered are the default and the ladder steps the machine can afford', () => {
  const offered = (total) => ramChoices(total).map((c) => c.gb);
  assert.deepEqual(offered(64), [24, 16, 8]);
  assert.deepEqual(offered(48), [24, 16, 8]);
  assert.deepEqual(offered(47), [16, 8]);   // 24 GB would be more than half
  assert.deepEqual(offered(32), [16, 8]);
  assert.deepEqual(offered(31), [12, 8]);   // 16 GB would be more than half; 12 is the default
  assert.deepEqual(offered(24), [12, 8]);
  assert.deepEqual(offered(20), [10, 8]);
  assert.deepEqual(offered(16), [8]);
});

test('the first amount is the default, and no amount is offered twice or above half the RAM', () => {
  for (let total = 16; total <= 256; total += 0.5) {
    const gbs = ramChoices(total).map((c) => c.gb);
    assert.equal(gbs[0], defaultRamOffer(total), `${total} GB machine`);
    assert.equal(new Set(gbs).size, gbs.length, `${total} GB machine`);
    assert.deepEqual(gbs.slice(1), [...gbs.slice(1)].sort((a, b) => b - a), `${total} GB machine`);
    assert.ok(gbs.every((gb) => gb <= total / 2 && gb >= CPU_MIN_RAM_GB), `${total} GB machine`);
  }
});

test('each amount says what it runs', () => {
  const runs = (total) => Object.fromEntries(ramChoices(total).map((c) => [c.gb, c.runs]));
  assert.deepEqual(runs(64), {
    24: 'Qwen3.6 at full quality, once 2 volunteers can host it',
    16: 'the main phone model, Qwen3.6',
    8: 'Qwen3 4B, the fast overflow model',
  });
  // 12 GB sits between two steps: it runs the smaller model, with room to spare.
  assert.equal(runs(24)[12], 'Qwen3 4B, the fast overflow model, with room to spare');
});

test('the ladder matches the models the server signs into each release', () => {
  const tiers = require('../../scripts/app_models.json').tiers;
  assert.deepEqual(CPU_LADDER.map((r) => r.gb), tiers.map((t) => t.min_ram_gb));
  assert.equal(CPU_LADDER[0].gb, CPU_MIN_RAM_GB);
  assert.match(tiers[0].name, /^Qwen3 4B/);
  assert.match(tiers[1].name, /^Qwen3\.6 35B-A3B$/);
  assert.match(tiers[2].name, /^Qwen3\.6 35B-A3B \(full quality\)$/);
});

test('the OS reads a little under the RAM a machine was sold with, and that is allowed for', () => {
  const GIB = 1024 ** 3;
  // What os.totalmem() typically reports, in GiB, and the offer such a machine should get.
  const readings = [
    [15.5, 8],    // 16 GB laptop
    [14.9, 8],    // 16 GB laptop with a 1 GB integrated GPU carve-out
    [31.2, 16],   // 32 GB
    [46.8, 24],   // 48 GB
    [62.5, 24],   // 64 GB
    [23.4, 12],   // 24 GB
    [11.4, null], // 12 GB
    [7.6, null],  // 8 GB
  ];
  for (const [gib, offer] of readings) {
    assert.equal(defaultRamOffer(installedRamGb(gib * GIB)), offer, `${gib} GiB reported`);
  }
  assert.equal(installedRamGb(16 * GIB), 17);
  assert.equal(installedRamGb(0), 0);
});

test('the modal asks how much RAM to lend and explains each amount', () => {
  const { message, detail } = ramOfferText(cpuReason(false), ramChoices(32));
  assert.equal(
    message,
    'DLNE: This machine has no usable GPU. Your computer powers the DLNE phone app (the CPU pool). How much RAM do you want to lend?'
  );
  assert.equal(detail, [
    'Each amount runs a whole model on your CPU:',
    '16 GB: runs the main phone model, Qwen3.6',
    '8 GB: runs Qwen3 4B, the fast overflow model',
    '',
    'In return you get the coding agent 2nd in line, with 300 chats every 5 hours.',
    'The model server runs at low priority on half your cores (2 to 8 threads), so your computer stays usable.',
    'DLNE offers at most half of this machine\'s RAM; set dlne.worker.maxRamGb for another amount.',
  ].join('\n'));
  assert.match(ramOfferText(cpuReason(true), ramChoices(32)).message, /^DLNE: You chose to contribute your CPU\. Your computer powers/);
  assert.match(
    ramOfferText(cpuReason(false, 'Intel(R) UHD Graphics'), ramChoices(32)).message,
    /^DLNE: This machine's only GPU \(Intel\(R\) UHD Graphics\) is an integrated one, which does not count as a GPU contributor\. Your computer powers/
  );
});

test('the buttons name the amount, and only the default is recommended', () => {
  const choices = ramChoices(64);
  assert.deepEqual(choices.map((c, i) => lendLabel(c, i === 0)), ['Lend 24 GB (recommended)', 'Lend 16 GB', 'Lend 8 GB']);
});

// ---- what a CPU contributor is told ----------------------------------------------------------

/** Claims that are true of GPU contributors only, or of no one. */
const NOT_FOR_CPU = /unlimited|frontier|coding model|inference unlocked|contribute local gpu|vulkan|\bgpu:/i;

const gpu = { name: 'RTX 4060', api: 'vulkan', vramTotalGb: 8 };
const view = (state, cpuPool, gb = 16) => ({ state, cpuPool, vramContributedGb: gb, gpu });

test('the sidebar card for a GPU contributor reads as it always did', () => {
  assert.deepEqual(contributionCopy(view('idle', false)), {
    deviceHtml: '<strong>VULKAN GPU:</strong> RTX 4060 (8 GB)',
    ruleHtml: '<strong>Core Rule:</strong> You must give compute to receive compute.',
    buttonText: '⚡ Contribute Local GPU',
  });
  assert.equal(contributionCopy(view('starting', false)).buttonText, '⏳ Initializing...');
  assert.equal(contributionCopy(view('starting', false)).ruleHtml, '⏳ Connecting local worker to cluster coordinator...');
  assert.equal(contributionCopy(view('contributing', false, 8)).buttonText, '⏹ Stop Contributing');
  assert.equal(
    contributionCopy(view('contributing', false, 8)).ruleHtml,
    '✅ <strong>Active:</strong> Contributing 8 GB to cluster. Inference unlocked!'
  );
});

test('the sidebar card for a CPU contributor says the computer powers the phone app', () => {
  assert.deepEqual(contributionCopy(view('idle', true)), {
    deviceHtml: '<strong>CPU pool:</strong> Your computer powers the DLNE phone app',
    ruleHtml: '<strong>You get:</strong> the coding agent 2nd in line, with 300 chats every 5 hours.',
    buttonText: '⚡ Power the DLNE Phone App',
  });
  assert.equal(contributionCopy(view('starting', true)).ruleHtml, '⏳ Joining the CPU pool...');
  const active = contributionCopy(view('contributing', true, 16));
  assert.equal(active.buttonText, '⏹ Stop Contributing');
  assert.equal(
    active.ruleHtml,
    '✅ <strong>Active:</strong> 16 GB of RAM lent, at low priority on half your cores. You get the coding agent 2nd in line, with 300 chats every 5 hours.'
  );
  for (const state of ['idle', 'starting', 'contributing', 'error']) {
    const copy = contributionCopy(view(state, true));
    assert.doesNotMatch(Object.values(copy).join('\n'), NOT_FOR_CPU, state);
  }
});

test('the status bar tooltip tells a CPU contributor what the machine is doing and what it gets', () => {
  assert.deepEqual(contributionTooltip(view('contributing', false, 8)), ['Local Contribution: CONTRIBUTING (8GB via VULKAN - RTX 4060)']);
  assert.deepEqual(contributionTooltip(view('idle', false)), ['Local Contribution: IDLE (RTX 4060 - VULKAN)']);
  assert.deepEqual(contributionTooltip(view('contributing', true, 16)), [
    'Local Contribution: CPU POOL (16GB of RAM, low priority, half your cores)',
    'Your computer powers the DLNE phone app. You get the coding agent 2nd in line, with 300 chats every 5 hours.',
  ]);
  assert.deepEqual(contributionTooltip(view('idle', true)), [
    'Local Contribution: IDLE (your CPU can power the DLNE phone app)',
    'Your computer powers the DLNE phone app. You get the coding agent 2nd in line, with 300 chats every 5 hours.',
  ]);
  assert.doesNotMatch([...contributionTooltip(view('contributing', true)), ...contributionTooltip(view('idle', true))].join('\n'), NOT_FOR_CPU);
});

test('the start notice says what a CPU contributor is doing', () => {
  assert.equal(
    workerStartedNotice(true),
    '⚡ DLNE Worker Started! Your computer powers the DLNE phone app. You get the coding agent 2nd in line, with 300 chats every 5 hours.'
  );
  assert.equal(workerStartedNotice(false), '⚡ DLNE Worker Started! Actively contributing compute to the cluster.');
  assert.doesNotMatch(workerStartedNotice(true), NOT_FOR_CPU);
});

test('the shared phrases say what the server does', () => {
  assert.equal(CPU_POOL_HEADLINE, 'Your computer powers the DLNE phone app');
  assert.match(CPU_POOL_PERK, /2nd in line/);
  assert.match(CPU_POOL_PERK, /300 chats every 5 hours/);
  // The model server's defaults (engine/app_member.py default_threads): half the cores, 2 to 8.
  const memberPy = fs.readFileSync(path.join(__dirname, '..', '..', 'engine', 'app_member.py'), 'utf8');
  assert.match(memberPy, /max\(2, min\(8, int\(physical_cores\) \/\/ 2\)\)/);
  assert.match(memberPy, /BELOW_NORMAL_PRIORITY_CLASS/);
  assert.match(memberPy, /os\.setpriority\(os\.PRIO_PROCESS, self\.process\.pid, 10\)/);
});

// ---- integrated GPUs do not count as a GPU contributor, with two exceptions ------------------

test('an integrated GPU counts only if it is an Intel Arc 140V/140T or an AMD Strix Halo', () => {
  const counts = [
    'Intel(R) Arc(TM) 140V GPU',
    'Intel(R) Arc(TM) 140V GPU (16GB)',
    'Intel(R) Arc(TM) 140T GPU (16GB)',
    'Intel Arc 140V',
    'AMD Radeon 8060S Graphics',
    'AMD Radeon(TM) 8060S Graphics',
    'AMD Radeon(TM) 8050S Graphics',
    'AMD Radeon(TM) 8040S Graphics',
    'AMD Radeon Graphics (RADV GFX1151)',   // Mesa on Linux names the chip: gfx1151 is Strix Halo
    'AMD Radeon Graphics (RADV STRIX_HALO)',
  ];
  const doesNot = [
    'Intel(R) UHD Graphics',
    'Intel(R) Iris(R) Xe Graphics',
    'Intel(R) Arc(TM) Graphics',            // Meteor Lake: no 140
    'Intel(R) Arc(TM) Graphics (MTL)',
    'Intel(R) Arc(TM) 130V GPU',            // Lunar Lake's smaller part is not on the list
    'AMD Radeon 780M',
    'AMD Radeon(TM) 780M Graphics',
    'AMD Radeon(TM) 890M Graphics',         // Strix Point, not Strix Halo
    'AMD Radeon Graphics (RADV GFX1150)',
    'AMD Radeon Graphics (RADV PHOENIX)',
    'Radeon Vega 8',
    'AMD Radeon(TM) Vega 8 Graphics',
    'Apple M2',
    '',
  ];
  for (const name of counts) assert.equal(isAllowedIntegratedGpu(name), true, name);
  for (const name of doesNot) assert.equal(isAllowedIntegratedGpu(name), false, name);
});

test('a discrete GPU always counts, whatever its name', () => {
  assert.equal(countsAsGpu('NVIDIA GeForce RTX 4060 Laptop GPU', true), true);
  assert.equal(countsAsGpu('Intel(R) Arc(TM) A770 Graphics', true), true);
  assert.equal(countsAsGpu('Intel(R) UHD Graphics', false), false);
  assert.equal(countsAsGpu('Intel(R) Arc(TM) 140V GPU', false), true);
});

/** `vulkaninfo --summary` for these devices: [name, 'DISCRETE' | 'INTEGRATED' | 'CPU']. */
function summary(...devices) {
  return 'Devices:\n========\n' + devices.map(([name, kind], i) =>
    `GPU${i}:\n\tapiVersion         = 1.3.274\n\tvendorID           = 0x8086\n` +
    `\tdeviceType         = PHYSICAL_DEVICE_TYPE_${kind}_GPU\n\tdeviceName         = ${name}\n`).join('');
}

test('the GPU is picked from what vulkaninfo lists', () => {
  const pick = (...devices) => pickVulkanGpu(parseVulkanSummary(summary(...devices)));
  // A laptop with an RTX beside its iGPU: the RTX.
  assert.equal(pick(['Intel(R) UHD Graphics', 'INTEGRATED'], ['NVIDIA GeForce RTX 4060', 'DISCRETE']).gpu.name, 'NVIDIA GeForce RTX 4060');
  // Only an iGPU that does not count: no GPU, and the iGPU is named so the machine can say why.
  assert.deepEqual(pick(['Intel(R) UHD Graphics', 'INTEGRATED']), { integratedGpu: 'Intel(R) UHD Graphics' });
  assert.deepEqual(pick(['AMD Radeon(TM) Graphics', 'INTEGRATED']), { integratedGpu: 'AMD Radeon(TM) Graphics' });
  // The iGPUs that count.
  assert.equal(pick(['Intel(R) Arc(TM) 140V GPU (16GB)', 'INTEGRATED']).gpu.name, 'Intel(R) Arc(TM) 140V GPU (16GB)');
  assert.equal(pick(['AMD Radeon(TM) 8060S Graphics', 'INTEGRATED']).gpu.isDiscrete, false);
  // A discrete GPU is preferred over an iGPU that counts.
  assert.equal(pick(['Intel(R) Arc(TM) 140V GPU', 'INTEGRATED'], ['NVIDIA GeForce RTX 4060', 'DISCRETE']).gpu.name, 'NVIDIA GeForce RTX 4060');
  // A software renderer is no device at all.
  assert.deepEqual(pick(['llvmpipe (LLVM 17.0.6, 256 bits)', 'CPU']), {});
  assert.deepEqual(pick(['llvmpipe (LLVM 17.0.6, 256 bits)', 'CPU'], ['Intel(R) UHD Graphics', 'INTEGRATED']), { integratedGpu: 'Intel(R) UHD Graphics' });
  assert.deepEqual(pickVulkanGpu(parseVulkanSummary('')), {});
});

test('a machine whose only GPU is a plain integrated one contributes its CPU, and the worker is told to hide the GPU', () => {
  const igpu = 'Intel(R) UHD Graphics';
  assert.deepEqual(planContribution('auto', false, '', 4, igpu), { cpuOnly: true, allowedGpus: 'none', needsMoreRam: true });
  assert.deepEqual(planContribution('cpu', false, '0', 16, igpu), { cpuOnly: true, allowedGpus: 'none', needsMoreRam: false });
  // Without an iGPU in the picture nothing changes: a machine the probe could not see into leaves the worker to look.
  assert.deepEqual(planContribution('auto', false, '', 4), { cpuOnly: true, allowedGpus: '', needsMoreRam: true });
  // A GPU that counts is never hidden.
  assert.deepEqual(planContribution('auto', true, '0', 4, igpu), { cpuOnly: false, allowedGpus: '0', needsMoreRam: false });
  const asked = planContribution('gpu', false, '', 16, igpu);
  assert.match(asked.error, /Intel\(R\) UHD Graphics is an integrated GPU/);
  assert.match(asked.error, /Intel Arc 140V\/140T or AMD Strix Halo/);
  assert.match(asked.error, /"auto" or "cpu"/);
});

test('the sidebar says why an integrated GPU was not used, and escapes the name', () => {
  const gpuInfo = { name: 'CPU Compute Node', api: 'cpu', vramTotalGb: 4, integratedGpu: 'Intel(R) UHD <Graphics>' };
  const copy = contributionCopy({ state: 'idle', cpuPool: true, vramContributedGb: 0, gpu: gpuInfo });
  assert.equal(
    copy.deviceHtml,
    '<strong>CPU pool:</strong> Your computer powers the DLNE phone app (Intel(R) UHD &lt;Graphics&gt; is an integrated GPU, which does not count)'
  );
  assert.equal(
    contributionCopy({ state: 'idle', cpuPool: true, vramContributedGb: 0, gpu: { ...gpuInfo, integratedGpu: undefined } }).deviceHtml,
    '<strong>CPU pool:</strong> Your computer powers the DLNE phone app'
  );
});
