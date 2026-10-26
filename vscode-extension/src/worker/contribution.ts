// What this machine contributes (dlne.worker.device), decided before the worker starts,
// and what a CPU contributor is told about it.

export type DeviceChoice = 'auto' | 'gpu' | 'cpu' | 'both';

/** The coordinator's floor for RAM lent by a CPU-only node (CPU_MIN_RAM_GB, engine/config.py). */
export const CPU_MIN_RAM_GB = 8;
/** DLNE never offers to lend more than this share of a machine's RAM; the owner keeps the rest. */
export const MAX_LEND_SHARE = 0.5;

/**
 * The CPU pool's models by the RAM a member lends (min_ram_gb in scripts/app_models.json, signed
 * into every release). The server decides which model a member serves; this only tells the owner.
 */
export const CPU_LADDER: ReadonlyArray<{ gb: number; runs: string; short: string }> = [
  { gb: CPU_MIN_RAM_GB, runs: 'Qwen3 4B, the fast overflow model', short: 'Qwen3 4B' },
  { gb: 16, runs: 'the main phone model, Qwen3.6', short: 'Qwen3.6' },
  { gb: 24, runs: 'Qwen3.6 at full quality, once 2 volunteers can host it', short: 'Qwen3.6 full' },
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
  /** dlne.worker.maxRamGb is below the CPU floor (for "both": dlne.worker.alsoCpuRamGb is); ask before starting. */
  needsMoreRam: boolean;
  /**
   * "Both" on a machine with a usable GPU: the GPU does whatever job the swarm gives it, and the CPU serves the
   * phone app's pool at the same time with the RAM in dlne.worker.alsoCpuRamGb. Never set with cpuOnly: on a
   * machine without a usable GPU "both" is "cpu".
   */
  alsoCpu?: boolean;
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
  integratedGpu?: string,
  alsoCpuRamGb: number = 0
): ContributionPlan {
  const choice: DeviceChoice = device === 'gpu' || device === 'cpu' || device === 'both' ? device : 'auto';
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
  if (choice === 'both' && !hasGpu) {
    // "Both" without a usable GPU is "cpu", exactly.
    return planContribution('cpu', hasGpu, allowedGpus, maxRamGb, integratedGpu);
  }
  if (choice === 'both' && hasGpu) {
    return {
      cpuOnly: false,
      allowedGpus,
      needsMoreRam: !(alsoCpuRamGb >= CPU_MIN_RAM_GB),
      alsoCpu: true,
    };
  }
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
// Windows details: quoting, executable names, lines that arrive in pieces
// ---------------------------------------------------------------------------------------------

/** A PowerShell single-quoted string literal: an apostrophe inside it (a user called O'Brien) is doubled. */
export function powershellQuote(value: string): string {
  return `'${value.replace(/'/g, "''")}'`;
}

/** The command that unpacks a zip with Windows PowerShell. `-LiteralPath` so `[` and `]` in a folder name are not wildcards. */
export function powershellExpandCommand(zipPath: string, destDir: string): string {
  return `powershell -NoProfile -Command "Expand-Archive -LiteralPath ${powershellQuote(zipPath)} -DestinationPath ${powershellQuote(destDir)} -Force"`;
}

/**
 * How Stop removes an orphaned ggml-rpc-server (its worker was killed before it could stop it): only the one this
 * install runs, matched by its full path, never another program's or another install's (a machine-wide
 * `killall ggml-rpc-server` would). `[file, args]` for execFileSync, or null when there is no path to match.
 */
export function orphanRpcStopCommand(rpcExe: string, platform: string): [string, string[]] | null {
  if (!rpcExe) return null;
  if (platform === 'win32') {
    const script = `Get-Process -Name ggml-rpc-server -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq ${powershellQuote(rpcExe)} } | Stop-Process -Force`;
    return ['powershell', ['-NoProfile', '-Command', script]];
  }
  // pkill matches an extended regular expression against the whole command line: the path, then an argument or the end.
  const escaped = rpcExe.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return ['pkill', ['-9', '-f', `^${escaped}( |$)`]];
}

/** `value` in double quotes when it contains whitespace (a command line splits an unquoted path at the first space). */
export function quoteIfSpaced(value: string): string {
  return /\s/.test(value) && !value.startsWith('"') ? `"${value}"` : value;
}

/** Whether this RPC platform's zip holds `ggml-rpc-server.exe` (every Windows build: win-x64 and win-arm64). */
export function isRpcExecutablePlatform(platform: string): boolean {
  return platform.startsWith('win');
}

/**
 * Split worker output into the lines that are complete and the unfinished rest: `pending` is what was left
 * over from the chunk before. A pipe may cut a line anywhere, and a line printed by the Windows worker ends
 * in CRLF, which stays attached to its line (the parsers trim it). The rest is capped so a worker that
 * never prints a newline cannot grow it without bound.
 */
export function takeCompleteLines(pending: string, chunk: string): { complete: string; rest: string } {
  const text = pending + chunk;
  const end = text.lastIndexOf('\n');
  if (end === -1) {
    return { complete: '', rest: text.slice(-65536) };
  }
  return { complete: text.slice(0, end + 1), rest: text.slice(end + 1).slice(-65536) };
}

// ---------------------------------------------------------------------------------------------
// The volunteer's coding-member switch, kept in the engine's config.txt
// ---------------------------------------------------------------------------------------------

/** The config.txt key engine/config.py reads (only the literal `true`, any casing, is true). */
export const CODING_MEMBER_ALLOWED_KEY = 'CODING_MEMBER_ALLOWED';
/** The engine's config.txt key for the RAM a "both" machine's CPU lends to the phone pool (0: off). */
export const ALSO_CPU_RAM_KEY = 'ALSO_CPU_RAM_GB';

/**
 * The new text of config.txt for ALSO_CPU_RAM_GB, or null when it needs no change: a machine that lends both
 * writes the amount; every other machine only clears a leftover positive value (so a systemd-started worker does
 * not keep lending the CPU after the owner went back to the GPU alone). A config without the key is left alone,
 * so a single-job machine's config.txt is never touched.
 */
export function alsoCpuConfigUpdate(text: string, lendBoth: boolean, ramGb: number): string | null {
  const wanted = lendBoth ? String(ramGb) : '0';
  const existing = /^\s*ALSO_CPU_RAM_GB\s*=\s*(.*?)\s*$/im.exec(text);
  if (!lendBoth && (!existing || !(parseFloat(existing[1]) > 0))) {
    return null;
  }
  const next = setConfigValue(text, ALSO_CPU_RAM_KEY, wanted);
  return next === text ? null : next;
}

/** The engine's config.txt key for the most CPU threads the worker may use (engine/config.py). */
export const MAX_CPU_THREADS_KEY = 'MAX_CPU_THREADS';

/**
 * The new text of config.txt for MAX_CPU_THREADS, or null when it needs no change. The engine reads -1 (or no key)
 * as "automatic" and 0 as ONE thread, never "all", so automatic is written as -1 and never as 0. A config without
 * the key stays untouched when the setting is automatic, so a machine that never set a limit never gets one.
 */
export function maxCpuThreadsConfigUpdate(text: string, threads: unknown): string | null {
  const manual = typeof threads === 'number' && Number.isFinite(threads) && threads >= 1;
  if (!manual && !/^\s*MAX_CPU_THREADS\s*=/im.test(text)) {
    return null;
  }
  const next = setConfigValue(text, MAX_CPU_THREADS_KEY, manual ? String(Math.floor(threads as number)) : '-1');
  return next === text ? null : next;
}

/**
 * `text` (a config.txt) with `key` set to `value`, the way engine.config.set_config_values does it:
 * keys match in any casing, comments, blank lines and other keys are copied through untouched,
 * the first match is edited in place, a duplicate further down is dropped (the engine's parser is
 * last-write-wins, so a stale duplicate would override the new value), and a missing key is
 * appended. The file's line endings (a Windows editor may have saved it with CRLF) and a leading
 * byte-order mark are handled: the mark is removed (the engine would read it as part of the first
 * key's name) and the result ends with exactly one line ending.
 */
export function setConfigValue(text: string, key: string, value: string): string {
  const eol = text.includes('\r\n') ? '\r\n' : '\n';
  const lines = text.replace(/^﻿/, '').split(/\r?\n/);
  if (lines[lines.length - 1] === '') lines.pop();
  const wanted = key.trim().toUpperCase();
  const out: string[] = [];
  let written = false;
  for (const raw of lines) {
    const stripped = raw.trim();
    if (!stripped || stripped.startsWith('#') || !stripped.includes('=')) {
      out.push(raw);
      continue;
    }
    const name = stripped.slice(0, stripped.indexOf('=')).trim();
    if (name.toUpperCase() !== wanted) {
      out.push(raw);
    } else if (!written) {
      out.push(`${name}=${value}`);
      written = true;
    }
  }
  if (!written) out.push(`${wanted}=${value}`);
  return out.join(eol) + eol;
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

/**
 * What `--coding-max-ram` carries: the RAM, in GB, the coding job may use. dlne.worker.maxRamGb has a small
 * default (4) that is right for the GPU's other job, an RPC node for the shared model, and wrong for a whole
 * coding model (an 8 GB GPU needs about 14 GB of RAM counted), so the coding job is uncapped (-1: it counts only
 * the RAM that is free when it starts) unless the user set the setting themselves. `explicit` is that value
 * (`inspect().globalValue`), undefined when they did not; zero or less counts as not set, as it does for `--max-ram`.
 */
export function codingMaxRamGb(explicit: number | null | undefined): number {
  return typeof explicit === 'number' && Number.isFinite(explicit) && explicit > 0 ? explicit : -1;
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
/** What a CPU contributor gets back; GPU contributors are 1st in line, with no limit, while their GPU has a job. */
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
export function ramOfferText(why: string, choices: RamChoice[], setting: string = 'dlne.worker.maxRamGb'): { message: string; detail: string } {
  return {
    message: `DLNE: ${why} ${CPU_POOL_HEADLINE} (the CPU pool). How much RAM do you want to lend?`,
    detail: [
      'Each amount runs a whole model on your CPU:',
      ...choices.map((c) => `${c.gb} GB: runs ${c.runs}`),
      '',
      `In return you get ${CPU_POOL_PERK}.`,
      `${CPU_POOL_PERFORMANCE}.`,
      `DLNE offers at most half of this machine's RAM; set ${setting} for another amount.`,
    ].join('\n'),
  };
}

/** Shown instead of the modal when the machine cannot lend the minimum within half its RAM. */
export function ramTooSmallText(totalGb: number): string {
  return `DLNE: this machine has about ${totalGb} GB of RAM. Powering the DLNE phone app needs ${CPU_MIN_RAM_GB} GB lent, ` +
    `and DLNE never offers more than half of your RAM, so it needs a machine with ${CPU_MIN_RAM_GB / MAX_LEND_SHARE} GB or more. ` +
    `To lend more anyway, set dlne.worker.maxRamGb yourself.`;
}

/**
 * A DLNE worker this editor did not start (a systemd service, a terminal, another editor) is running. It has its
 * own settings, so nothing about it is described from the settings here: what it lends is not known to this editor.
 * Stop Contributing stops the dlne-worker service, never another program's process, so a worker started from a
 * terminal or another editor is stopped where it was started.
 */
export const EXTERNAL_WORKER_LINE = 'A DLNE worker started outside VS Code is running on this machine, with its own settings, not the ones here.';
export const EXTERNAL_WORKER_TEXT =
  `${EXTERNAL_WORKER_LINE} Stop Contributing stops it if it is the dlne-worker service; one started from a terminal ` +
  'or another editor stops there. Then start again to contribute with your VS Code settings.';

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
  /** The job the coordinator gave this machine, once the worker has said (see parseRole). */
  role?: WorkerRole;
  roleDetail?: RoleDetail;
  /** "Both": the worker says the CPU also serves the phone pool next to the GPU's job (see parseAlso). */
  alsoCpu?: boolean;
  /** The running worker was started outside this editor (EXTERNAL_WORKER_LINE); its settings are not these. */
  external?: boolean;
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
  if (active && st.external) {
    return {
      deviceHtml: `<strong>Outside VS Code:</strong> ${escapeHtml(EXTERNAL_WORKER_LINE)}`,
      ruleHtml: 'Stop Contributing stops it; start again to contribute with your settings here.',
      buttonText: buttonText(''),
    };
  }
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
      ? `✅ <strong>Active:</strong> Contributing ${st.vramContributedGb} GB to cluster. Inference unlocked!` +
        (st.alsoCpu ? `<br>📱 <strong>Also:</strong> ${ALSO_CPU_LINE}` : '')
      : starting
      ? '⏳ Connecting local worker to cluster coordinator...'
      : '<strong>Core Rule:</strong> You must give compute to receive compute.',
    buttonText: buttonText('⚡ Contribute Local GPU'),
  };
}

/** The "Local Contribution" lines of the status bar tooltip. */
export function contributionTooltip(st: ContributionView): string[] {
  const active = st.state === 'contributing';
  if (active && st.external) {
    return ['Local Contribution: A WORKER STARTED OUTSIDE VS CODE (its own settings)', EXTERNAL_WORKER_TEXT];
  }
  if (st.cpuPool) {
    return [
      active
        ? `Local Contribution: CPU POOL (${st.vramContributedGb}GB of RAM, low priority, half your cores)`
        : 'Local Contribution: IDLE (your CPU can power the DLNE phone app)',
      `${CPU_POOL_HEADLINE}. You get ${CPU_POOL_PERK}.`,
    ];
  }
  const api = st.gpu.api.toUpperCase();
  const lines = [active ? `Local Contribution: CONTRIBUTING (${st.vramContributedGb}GB via ${api} - ${st.gpu.name})` : `Local Contribution: IDLE (${st.gpu.name} - ${api})`];
  // The job the swarm gave this GPU, once the worker has said what it is.
  if (active && st.role) {
    lines.push(`Role: ${ROLE_LABEL[st.role]}. ${roleMeaning(st.role, st.roleDetail)}`);
    if (st.role === 'coding') {
      lines.push(MEMBER_PRIVACY_SUMMARY);
      if (st.roleDetail?.trustId) {
        lines.push(`Trust id: ${st.roleDetail.trustId}. ${TRUST_ID_HINT}`);
      }
    }
  }
  if (active && st.alsoCpu) {
    lines.push(`Also: ${ROLE_LABEL.app}. ${ALSO_CPU_LINE}`);
  }
  return lines;
}

// ---------------------------------------------------------------------------------------------
// The job the swarm gives a GPU: GPU pool, coding member, or (for a CPU) phone app member
// ---------------------------------------------------------------------------------------------

/**
 * What the coordinator made of this machine (docs/coding-members.md section 10): `rpc` is a share of one
 * big model run together with other volunteers' GPUs, `coding` a whole coding model on this GPU alone that
 * answers other people's coding requests, `app` the phone app's CPU pool.
 */
export type WorkerRole = 'rpc' | 'coding' | 'app';

export interface RoleDetail {
  model?: string;
  tier?: string;
  /** Coding requests the member runs at once. */
  slots?: number;
  /**
   * The member's trust id (contract section 1: 32 hex characters derived from its own identity key). The
   * operator lists the trust ids of machines they trust to receive private code; a volunteer who owns the
   * machine gives theirs to the operator.
   */
  trustId?: string;
}

/** The hint shown beside a trust id. */
export const TRUST_ID_HINT = 'Give this to the operator if this is your own machine and you want private repos routed to it.';

export interface RoleAssignment extends RoleDetail {
  role: WorkerRole;
}

/**
 * The line the worker prints when the coordinator has given it a job, which the extension reads from its
 * output the way it reads DLNE_REFUSAL:
 * `DLNE_ROLE: coding {"model": "...", "tier": "...", "slots": 2, "trust_id": "<32 hex>"}`.
 * The JSON part is optional; `DLNE_ROLE: rpc` alone is a whole line. A worker that never prints one (an
 * engine from before coding members) is a GPU pool member, which is what it was.
 */
export const ROLE_MARKER = 'DLNE_ROLE: ';

/** The last job the worker announced in `output`, or null. Anything malformed is ignored. */
export function parseRole(output: string): RoleAssignment | null {
  let found: RoleAssignment | null = null;
  for (const line of output.split(/\r?\n/)) {
    const at = line.indexOf(ROLE_MARKER);
    if (at === -1) continue;
    const match = /^(rpc|coding|app)\b\s*(\{.*\})?\s*$/.exec(line.slice(at + ROLE_MARKER.length).trim());
    if (!match) continue;
    const assignment: RoleAssignment = { role: match[1] as WorkerRole };
    if (match[2]) {
      try {
        const detail = JSON.parse(match[2]);
        if (typeof detail.model === 'string' && detail.model.length <= 128) assignment.model = detail.model;
        if (typeof detail.tier === 'string' && detail.tier.length <= 128) assignment.tier = detail.tier;
        if (Number.isInteger(detail.slots) && detail.slots >= 1 && detail.slots <= 64) assignment.slots = detail.slots;
        if (typeof detail.trust_id === 'string' && /^[0-9a-f]{32}$/.test(detail.trust_id)) assignment.trustId = detail.trust_id;
      } catch {
        // the role alone stands
      }
    }
    found = assignment;
  }
  return found;
}

/**
 * The line the worker prints beside DLNE_ROLE on a machine that also lends its CPU to the phone app ("both"):
 * `DLNE_ALSO: app` while the CPU serves the pool, `DLNE_ALSO: none` when it stops. DLNE_ROLE stays the GPU's job.
 */
export const ALSO_MARKER = 'DLNE_ALSO: ';

/** Whether the CPU is serving the phone pool next to the GPU's own job, from the last DLNE_ALSO line; null if none. */
export function parseAlso(output: string): boolean | null {
  let found: boolean | null = null;
  for (const line of output.split(/\r?\n/)) {
    const at = line.indexOf(ALSO_MARKER);
    if (at === -1) continue;
    const word = line.slice(at + ALSO_MARKER.length).trim();
    if (word === 'app') found = true;
    else if (word === 'none') found = false;
  }
  return found;
}

/**
 * The line the worker prints (engine/worker.py CLOCK_MARKER, 2.4.6+) when this computer's clock is outside the
 * coordinator's 30 s window: `DLNE_CLOCK: {"offset_seconds": 18000}`, positive when the computer is behind the
 * swarm. The worker corrects for it and keeps contributing; the owner is told so the clock itself gets fixed. The
 * worker prints it once per change, so every line is news.
 */
export const CLOCK_MARKER = 'DLNE_CLOCK: ';

/** The last clock offset in `output`, in whole seconds (positive: behind the swarm), or null. Nonsense is ignored. */
export function parseClock(output: string): number | null {
  let found: number | null = null;
  for (const line of output.split(/\r?\n/)) {
    const at = line.indexOf(CLOCK_MARKER);
    if (at === -1) continue;
    try {
      const offset = JSON.parse(line.slice(at + CLOCK_MARKER.length).trim())?.offset_seconds;
      // Ten years either way is already absurd; anything past it is not a clock reading.
      if (Number.isFinite(offset) && Math.abs(offset) <= 10 * 365 * 86400) found = Math.round(offset);
    } catch {
      // not a clock line after all
    }
  }
  return found;
}

/** '5 h', '5 h 30 min', '3 min 12 s', '45 s' (engine/swarm_clock.py _duration). */
function clockDuration(seconds: number): string {
  const whole = Math.round(Math.abs(seconds));
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const secs = whole % 60;
  if (hours) return minutes ? `${hours} h ${minutes} min` : `${hours} h`;
  if (minutes) return secs ? `${minutes} min ${secs} s` : `${minutes} min`;
  return `${secs} s`;
}

/** A whole number of 15-minute time-zone steps (90 s either way), at least half an hour (swarm_clock.looks_like_time_zone). */
function looksLikeTimeZone(offset: number): boolean {
  const size = Math.abs(offset);
  if (size < 30 * 60 - 90) return false;
  const nearest = Math.round(size / (15 * 60)) * (15 * 60);
  return Math.abs(size - nearest) <= 90;
}

/**
 * What the owner is told about a clock `offset` s behind (positive) or ahead of (negative) the swarm: the worker's
 * own words (engine/swarm_clock.py describe), or null inside the coordinator's 30 s window.
 */
export function clockMessage(offset: number): string | null {
  if (!Number.isFinite(offset) || Math.abs(offset) < 30) return null;
  const where = offset > 0 ? 'behind' : 'ahead of';
  let text = `This computer's clock is ${clockDuration(offset)} ${where} the swarm's. DLNE corrects for it, so contributing works. `;
  text += looksLikeTimeZone(offset)
    ? 'A difference of whole hours usually comes from a time zone: the computer\'s time zone is set wrong, or, on a PC '
      + 'that also runs Windows, Windows keeps the hardware clock in local time while Linux reads it as UTC. Turning on '
      + 'automatic time and time zone in the system settings fixes the clock itself.'
    : 'Turning on automatic time sync in the system settings fixes the clock itself.';
  return text;
}

export const ROLE_LABEL: Record<WorkerRole, string> = {
  rpc: 'GPU pool',
  coding: 'Coding member',
  app: 'Phone app member',
};

const modelTitle = (file: string) => file.replace(/\.gguf$/i, '');

/** What a role means for the machine that has it, in one sentence. */
export function roleMeaning(role: WorkerRole, detail?: RoleDetail): string {
  switch (role) {
    case 'rpc':
      return 'Your GPU holds a share of one big model that the swarm runs together on many volunteers\' GPUs.';
    case 'coding': {
      const model = detail?.model ? ` (${modelTitle(detail.model)})` : '';
      const at = detail?.slots ? `, about ${detail.slots} at a time` : '';
      return `Your GPU runs a whole coding model${model} by itself and answers other people's coding requests${at}.`;
    }
    case 'app':
      return 'Your computer runs a small model that answers questions in the DLNE phone app.';
  }
}

/** The privacy facts a coding member is shown, plainly (contract sections 6 to 8). */
export const MEMBER_PRIVACY_FACTS: readonly string[] = [
  'You run other people\'s coding requests. Your GPU has to read their prompts and code to answer, so the machine itself sees them while it works.',
  'The requests are anonymous. No name, account or IP address of the person asking reaches your machine.',
  'They are end-to-end encrypted in transit: each request is sealed for your machine and opened only by the DLNE worker on it, so the network and DLNE\'s server cannot read it.',
  'Your machine keeps no trace: the worker writes no prompts or answers to disk or to its logs.',
  'Code from a private repository goes only to machines the operator trusts, unless its owner allows any member. Public repositories can reach any member.',
  'The swarm sometimes sends test requests holding decoy credentials; a member that ever uses one is removed.',
];

/** The short form of the same facts, for the card and the tooltip. */
export const MEMBER_PRIVACY_SUMMARY = 'Requests are anonymous and end-to-end encrypted in transit, and your machine keeps no trace.';

export const LEND_GPU = 'Lend my GPU';
export const LEND_CPU_ONLY = 'Lend my CPU only';
export const LEND_BOTH = 'Lend both';

/** One plain sentence for the third button of the lend dialog. */
export const LEND_BOTH_SENTENCE =
  `"${LEND_BOTH}" does both at once: your GPU does the job above, and your CPU also powers the DLNE phone app with the RAM you choose ` +
  '(it sets dlne.worker.device to "both"; you are asked how much RAM next).';

/** Why this machine also lends its CPU, as the first sentence of the RAM offer. */
export const ALSO_CPU_REASON = 'You chose to lend your CPU as well as your GPU.';

/** The sidebar and chat line for a GPU machine whose CPU also serves the pool. */
export const ALSO_CPU_LINE = 'Your CPU also powers the DLNE phone app (the CPU pool), at low priority, next to your GPU\'s job.';

/** Shown when "both" is chosen on a machine that cannot lend the minimum RAM: the GPU is lent alone. */
export function bothRamTooSmallText(totalGb: number): string {
  return `DLNE: this machine has about ${totalGb} GB of RAM. Powering the DLNE phone app needs ${CPU_MIN_RAM_GB} GB lent, ` +
    `and DLNE never offers more than half of your RAM, so your CPU is not lent this time; your GPU is lent as usual.`;
}

/** The one sentence the lend dialog gives about the RAM a coding member may use (see codingMaxRamGb). */
export const CODING_RAM_SENTENCE =
  'The coding job uses the memory that is free when it starts (it steps aside when you game); set dlne.worker.maxRamGb to limit it.';

/**
 * The modal a GPU owner sees the first time they click to contribute: what happens, the two jobs the swarm
 * may give the GPU, the privacy facts for the second, and a way to keep the GPU out of it. ("About 2 at a
 * time" is the usual figure for a coding member, given as an example; the sidebar shows the real one.)
 */
export function lendGpuPrompt(gpuName: string): { message: string; detail: string; buttons: [string, string, string] } {
  return {
    message: `DLNE: lend ${gpuName || 'your GPU'} to the swarm?`,
    detail: [
      'The swarm decides what your GPU does, and the sidebar shows you which:',
      `- ${ROLE_LABEL.rpc}: ${roleMeaning('rpc')}`,
      `- ${ROLE_LABEL.coding}: ${roleMeaning('coding', { slots: 2 })}`,
      '',
      'If it becomes a coding member, you should know:',
      ...MEMBER_PRIVACY_FACTS.map((fact) => `- ${fact}`),
      '',
      CODING_RAM_SENTENCE,
      'In return the coding agent is yours first in line, with no limit, while the swarm uses your GPU (and for its first 30 minutes while it finds it a job). You can stop at any time from the sidebar.',
      `"${LEND_CPU_ONLY}" keeps your GPU yours: your computer then powers the DLNE phone app instead (it sets dlne.worker.device to "cpu").`,
      LEND_BOTH_SENTENCE,
    ].join('\n'),
    buttons: [LEND_GPU, LEND_CPU_ONLY, LEND_BOTH],
  };
}

/** The toast when the swarm makes a GPU a coding member (once per install). */
export const CODING_MEMBER_NOTICE =
  'DLNE: the swarm made your GPU a coding member. It answers other people\'s coding requests. ' + MEMBER_PRIVACY_SUMMARY;

/** The sidebar's role lines: one line on the card, and the details behind it. */
export interface RoleCopy {
  roleHtml: string;
  /** Empty for a CPU contributor, who is not shown GPU-only facts. */
  factsHtml: string;
  /** A coding member's trust id with its one-line hint and a Copy link; empty otherwise. */
  trustHtml: string;
}

export function roleCopy(st: ContributionView): RoleCopy {
  const active = st.state === 'contributing';
  const starting = st.state === 'starting';
  if (active && st.external) {
    // Its job is in its own log; this editor did not start it and cannot say.
    return { roleHtml: '<strong>Role:</strong> not known here (that worker reports to its own log).', factsHtml: '', trustHtml: '' };
  }
  if (st.cpuPool) {
    return { roleHtml: `<strong>Role:</strong> ${ROLE_LABEL.app}. ${escapeHtml(roleMeaning('app'))}`, factsHtml: '', trustHtml: '' };
  }
  const facts =
    '<details class="role-details"><summary>What does lending my GPU mean?</summary><ul>' +
    `<li><strong>${ROLE_LABEL.rpc}:</strong> ${escapeHtml(roleMeaning('rpc'))}</li>` +
    `<li><strong>${ROLE_LABEL.coding}:</strong> ${escapeHtml(roleMeaning('coding', { slots: 2 }))} If you become one:<ul>` +
    MEMBER_PRIVACY_FACTS.map((fact) => `<li>${escapeHtml(fact)}</li>`).join('') +
    '</ul></li></ul></details>';
  if (active) {
    // An engine from before coding members never says; it is a GPU pool member, as it always was.
    const role = st.role ?? 'rpc';
    const privacy = role === 'coding' ? ` ${escapeHtml(MEMBER_PRIVACY_SUMMARY)}` : '';
    const trustId = role === 'coding' ? st.roleDetail?.trustId : undefined;
    const also = st.alsoCpu ? `<br><strong>Also:</strong> ${ROLE_LABEL.app}. ${escapeHtml(ALSO_CPU_LINE)}` : '';
    return {
      roleHtml: `<strong>Role:</strong> ${ROLE_LABEL[role]}. ${escapeHtml(roleMeaning(role, st.roleDetail))}${privacy}${also}`,
      factsHtml: facts,
      trustHtml: trustId
        ? `<strong>Trust id:</strong> <code>${escapeHtml(trustId)}</code> <a href="#" class="copy-trust" data-id="${escapeHtml(trustId)}">Copy</a><br>${escapeHtml(TRUST_ID_HINT)}`
        : '',
    };
  }
  if (starting) {
    return { roleHtml: '⏳ The swarm is choosing a job for your GPU...', factsHtml: facts, trustHtml: '' };
  }
  return {
    roleHtml: `<strong>Your GPU's job:</strong> the swarm chooses, a share of one big model (${ROLE_LABEL.rpc}) or a whole coding model for other people's requests (${ROLE_LABEL.coding}).`,
    factsHtml: facts,
    trustHtml: '',
  };
}
