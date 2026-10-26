// What this machine contributes (dlne.worker.device), decided before the worker starts,
// and what a CPU contributor is told about it.

export type DeviceChoice = 'auto' | 'gpu' | 'cpu';

/** The coordinator's floor for RAM lent by a CPU-only node (CPU_MIN_RAM_GB, engine/config.py). */
export const CPU_MIN_RAM_GB = 8;
/** DLNE never offers to lend more than this share of a machine's RAM; the owner keeps the rest. */
export const MAX_LEND_SHARE = 0.5;

/**
 * The CPU pool's models by the RAM a member lends (min_ram_gb in scripts/app_models.json, signed
 * into every release). The server decides which model a member serves; this only tells the owner.
 */
export const CPU_LADDER: ReadonlyArray<{ gb: number; runs: string }> = [
  { gb: CPU_MIN_RAM_GB, runs: 'Qwen3 4B, the fast overflow model' },
  { gb: 16, runs: 'the main phone model, Qwen3.6' },
  { gb: 24, runs: 'Qwen3.6 at full quality, once 2 volunteers can host it' },
];

/** What a machine with at least this much RAM is offered to lend, biggest first. */
const OFFER_BY_TOTAL_RAM: ReadonlyArray<{ totalGb: number; offerGb: number }> = [
  { totalGb: 48, offerGb: 24 },
  { totalGb: 32, offerGb: 16 },
  { totalGb: 16, offerGb: 12 },
];

/** The line the worker prints when the coordinator refuses this machine (engine/worker.py REFUSAL_MARKER). */
export const REFUSAL_MARKER = 'DLNE_REFUSAL: ';
/** The worker's exit code for that refusal (engine/worker.py CPU_BELOW_MINIMUM_EXIT_CODE). */
export const CPU_BELOW_MINIMUM_EXIT_CODE = 78;

export interface ContributionPlan {
  /** The worker will run on the CPU (no GPU, or the user chose CPU). */
  cpuOnly: boolean;
  /** What to pass as --allowed-gpus ('' for "all detected"). */
  allowedGpus: string;
  /** dlne.worker.maxRamGb is below the CPU floor; ask before starting. */
  needsMoreRam: boolean;
  /** Set when the worker must not start at all; written to be shown as-is. */
  error?: string;
}

/**
 * Whether a Vulkan device counts as a GPU contributor. An integrated GPU does not, except Intel
 * Arc 140V / 140T (Lunar Lake, Arrow Lake) and AMD Strix Halo (Radeon 8060S / 8050S / 8040S: Ryzen
 * AI Max). The name is what the driver reports: Windows says "Radeon(TM) 8060S", Mesa on Linux
 * names the chip instead ("Radeon Graphics (RADV GFX1151)", gfx1151 being Strix Halo).
 */
export function isAllowedIntegratedGpu(name: string): boolean {
  const n = name.replace(/\((?:r|tm)\)/gi, '').replace(/\s+/g, ' ').toLowerCase();
  return /\barc 140[vt]\b/.test(n) || /\bradeon\b.*\b80(?:40|50|60)s\b/.test(n) || /\bgfx1151\b|\bstrix[ _]halo\b/.test(n);
}

export function countsAsGpu(name: string, discrete: boolean): boolean {
  return discrete || isAllowedIntegratedGpu(name);
}

export interface VulkanDevice {
  name: string;
  isDiscrete: boolean;
}

/** The devices in `vulkaninfo --summary`, software renderers (llvmpipe) left out. */
export function parseVulkanSummary(output: string): VulkanDevice[] {
  const devices: VulkanDevice[] = [];
  let pendingType = '';
  for (const rawLine of output.split('\n')) {
    const line = rawLine.trim();
    if (line.startsWith('deviceType') && line.includes('=')) {
      pendingType = line.split('=')[1]?.trim() || '';
    } else if (line.startsWith('deviceName') && line.includes('=')) {
      const name = line.split('=')[1]?.trim() || '';
      if (name && !name.toLowerCase().includes('llvmpipe') && !name.toLowerCase().includes('software')) {
        devices.push({ name, isDiscrete: pendingType.includes('DISCRETE_GPU') });
      }
      pendingType = '';
    }
  }
  return devices;
}

/**
 * The GPU this machine contributes (a discrete one first), or, when every device is an integrated
 * GPU that does not count, the name of the first one, so the machine can say why it contributes its CPU.
 */
export function pickVulkanGpu(devices: VulkanDevice[]): { gpu?: VulkanDevice; integratedGpu?: string } {
  const counted = devices.filter((d) => countsAsGpu(d.name, d.isDiscrete));
  if (counted.length > 0) {
    return { gpu: counted.find((d) => d.isDiscrete) || counted[0] };
  }
  return devices.length > 0 ? { integratedGpu: devices[0].name } : {};
}

/**
 * `integratedGpu` is set when the only GPU found is an integrated one that does not count
 * (hasGpu is then false): the worker is told to hide it, so the machine joins as a CPU contributor.
 */
export function planContribution(
  device: string | undefined,
  hasGpu: boolean,
  allowedGpus: string,
  maxRamGb: number,
  integratedGpu?: string
): ContributionPlan {
  const choice: DeviceChoice = device === 'gpu' || device === 'cpu' ? device : 'auto';
  if (choice === 'gpu' && !hasGpu) {
    return {
      cpuOnly: false,
      allowedGpus,
      needsMoreRam: false,
      error: integratedGpu
        ? `DLNE found no GPU that can contribute: ${integratedGpu} is an integrated GPU, and only a discrete GPU, Intel Arc 140V/140T or AMD Strix Halo counts. ` +
          'dlne.worker.device is set to "gpu"; set it to "auto" or "cpu" to power the DLNE phone app with the CPU instead.'
        : 'DLNE found no GPU on this machine, and dlne.worker.device is set to "gpu". Set it to "auto" or "cpu" to contribute the CPU instead.',
    };
  }
  const cpuOnly = choice === 'cpu' || !hasGpu;
  return {
    cpuOnly,
    allowedGpus: choice === 'cpu' || (!hasGpu && integratedGpu) ? 'none' : allowedGpus,
    needsMoreRam: cpuOnly && !(maxRamGb >= CPU_MIN_RAM_GB),
  };
}

/** The coordinator's reason for refusing this machine, from worker output, or null. */
export function parseRefusal(output: string): string | null {
  for (const line of output.split(/\r?\n/)) {
    const at = line.indexOf(REFUSAL_MARKER);
    if (at !== -1) {
      const reason = line.slice(at + REFUSAL_MARKER.length).trim();
      // Shown in a notification: one line, bounded, whatever the coordinator sent.
      return reason ? reason.slice(0, 400) : null;
    }
  }
  return null;
}

// ---------------------------------------------------------------------------------------------
// How much RAM a CPU-only machine is offered to lend
// ---------------------------------------------------------------------------------------------

/**
 * The RAM a machine was sold with, from the bytes the OS reports. os.totalmem() reads a little
 * under it (firmware and an integrated GPU keep some: a 16 GB laptop reports about 15.5), which
 * would put such a machine a rung too low, and a 16 GB laptop under the half-RAM rule below.
 * Allows 6% for that and rounds to whole GB.
 */
export function installedRamGb(totalBytes: number): number {
  return Math.round(totalBytes / 1024 ** 3 / 0.94);
}

/**
 * The RAM to suggest a CPU-only machine lends, from its total: 24 GB from 48, 16 GB from 32,
 * 12 GB from 16, otherwise the 8 GB minimum. Never more than half the total, so the owner keeps
 * the other half; null when that leaves less than the minimum (a machine under 16 GB).
 */
export function defaultRamOffer(totalGb: number): number | null {
  const step = OFFER_BY_TOTAL_RAM.find((s) => totalGb >= s.totalGb);
  const offer = Math.min(step ? step.offerGb : CPU_MIN_RAM_GB, Math.floor(totalGb * MAX_LEND_SHARE));
  return offer >= CPU_MIN_RAM_GB ? offer : null;
}

export interface RamChoice {
  gb: number;
  /** What a member lending this much runs, for the sentence "<gb> GB: runs <runs>". */
  runs: string;
}

/** What a member lending `gb` runs: the biggest ladder model that fits. */
function describeRam(gb: number): string {
  const rung = [...CPU_LADDER].reverse().find((r) => r.gb <= gb) ?? CPU_LADDER[0];
  return rung.gb === gb ? rung.runs : `${rung.runs}, with room to spare`;
}

/**
 * The amounts to offer: the default first, then every other ladder step the machine can afford,
 * biggest first. Empty when the machine cannot lend the minimum.
 */
export function ramChoices(totalGb: number): RamChoice[] {
  const preferred = defaultRamOffer(totalGb);
  if (preferred === null) {
    return [];
  }
  const others = CPU_LADDER.map((r) => r.gb)
    .filter((gb) => gb !== preferred && gb <= totalGb * MAX_LEND_SHARE)
    .sort((a, b) => b - a);
  return [preferred, ...others].map((gb) => ({ gb, runs: describeRam(gb) }));
}

// ---------------------------------------------------------------------------------------------
// What a CPU contributor is told, in one place
// ---------------------------------------------------------------------------------------------

export const CPU_POOL_HEADLINE = 'Your computer powers the DLNE phone app';
/** What a CPU contributor gets back; GPU contributors are 1st in line, with no limit. */
export const CPU_POOL_PERK = 'the coding agent 2nd in line, with 300 chats every 5 hours';
export const CPU_POOL_PERFORMANCE = 'The model server runs at low priority on half your cores (2 to 8 threads), so your computer stays usable';

/** The button label for one offered amount, e.g. "Lend 16 GB (recommended)". */
export function lendLabel(choice: RamChoice, recommended: boolean): string {
  return `Lend ${choice.gb} GB${recommended ? ' (recommended)' : ''}`;
}

/** Why this machine contributes its CPU, as the first sentence of the modal. */
export function cpuReason(hasGpu: boolean, integratedGpu?: string): string {
  if (hasGpu) {
    return 'You chose to contribute your CPU.';
  }
  return integratedGpu
    ? `This machine's only GPU (${integratedGpu}) is an integrated one, which does not count as a GPU contributor.`
    : 'This machine has no usable GPU.';
}

/** The modal that asks a CPU-only machine how much RAM to lend. The choices come from ramChoices. */
export function ramOfferText(why: string, choices: RamChoice[]): { message: string; detail: string } {
  return {
    message: `DLNE: ${why} ${CPU_POOL_HEADLINE} (the CPU pool). How much RAM do you want to lend?`,
    detail: [
      'Each amount runs a whole model on your CPU:',
      ...choices.map((c) => `${c.gb} GB: runs ${c.runs}`),
      '',
      `In return you get ${CPU_POOL_PERK}.`,
      `${CPU_POOL_PERFORMANCE}.`,
      'DLNE offers at most half of this machine\'s RAM; set dlne.worker.maxRamGb for another amount.',
    ].join('\n'),
  };
}

/** Shown instead of the modal when the machine cannot lend the minimum within half its RAM. */
export function ramTooSmallText(totalGb: number): string {
  return `DLNE: this machine has about ${totalGb} GB of RAM. Powering the DLNE phone app needs ${CPU_MIN_RAM_GB} GB lent, ` +
    `and DLNE never offers more than half of your RAM, so it needs a machine with ${CPU_MIN_RAM_GB / MAX_LEND_SHARE} GB or more. ` +
    `To lend more anyway, set dlne.worker.maxRamGb yourself.`;
}

/** The toast when the worker starts. */
export function workerStartedNotice(cpuPool: boolean): string {
  return cpuPool
    ? `⚡ DLNE Worker Started! ${CPU_POOL_HEADLINE}. You get ${CPU_POOL_PERK}.`
    : '⚡ DLNE Worker Started! Actively contributing compute to the cluster.';
}

/** What the worker status shows, which is all the text below needs (WorkerStatus fits it). */
export interface ContributionView {
  state: string;
  /** This machine contributes its CPU (it powers the phone app), not a GPU. */
  cpuPool: boolean;
  /** GB lent: VRAM for a GPU, RAM for the CPU pool. */
  vramContributedGb: number;
  /** integratedGpu: the integrated GPU that was left out because it does not count (isAllowedIntegratedGpu). */
  gpu: { name: string; api: string; vramTotalGb: number; integratedGpu?: string };
}

/** The sidebar's contribution card: two HTML lines and the button. */
export interface ContributionCopy {
  deviceHtml: string;
  ruleHtml: string;
  buttonText: string;
}

/** A device name from the driver goes into the sidebar's HTML. */
function escapeHtml(text: string): string {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

export function contributionCopy(st: ContributionView): ContributionCopy {
  const active = st.state === 'contributing';
  const starting = st.state === 'starting';
  const buttonText = (idle: string) => (active ? '⏹ Stop Contributing' : starting ? '⏳ Initializing...' : idle);
  if (st.cpuPool) {
    return {
      deviceHtml: `<strong>CPU pool:</strong> ${CPU_POOL_HEADLINE}` +
        (st.gpu.integratedGpu ? ` (${escapeHtml(st.gpu.integratedGpu)} is an integrated GPU, which does not count)` : ''),
      ruleHtml: active
        ? `✅ <strong>Active:</strong> ${st.vramContributedGb} GB of RAM lent, at low priority on half your cores. You get ${CPU_POOL_PERK}.`
        : starting
        ? '⏳ Joining the CPU pool...'
        : `<strong>You get:</strong> ${CPU_POOL_PERK}.`,
      buttonText: buttonText('⚡ Power the DLNE Phone App'),
    };
  }
  const apiTag = st.gpu.api ? st.gpu.api.toUpperCase() : 'VULKAN';
  return {
    deviceHtml: `<strong>${apiTag} GPU:</strong> ${st.gpu.name || 'Local GPU'} (${st.gpu.vramTotalGb || 8} GB)`,
    ruleHtml: active
      ? `✅ <strong>Active:</strong> Contributing ${st.vramContributedGb} GB to cluster. Inference unlocked!`
      : starting
      ? '⏳ Connecting local worker to cluster coordinator...'
      : '<strong>Core Rule:</strong> You must give compute to receive compute.',
    buttonText: buttonText('⚡ Contribute Local GPU'),
  };
}

/** The "Local Contribution" lines of the status bar tooltip. */
export function contributionTooltip(st: ContributionView): string[] {
  const active = st.state === 'contributing';
  if (st.cpuPool) {
    return [
      active
        ? `Local Contribution: CPU POOL (${st.vramContributedGb}GB of RAM, low priority, half your cores)`
        : 'Local Contribution: IDLE (your CPU can power the DLNE phone app)',
      `${CPU_POOL_HEADLINE}. You get ${CPU_POOL_PERK}.`,
    ];
  }
  const api = st.gpu.api.toUpperCase();
  return [active ? `Local Contribution: CONTRIBUTING (${st.vramContributedGb}GB via ${api} - ${st.gpu.name})` : `Local Contribution: IDLE (${st.gpu.name} - ${api})`];
}
