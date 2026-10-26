// The sidebar's "Donate compute" panel, as plain data: what the sliders offer on this machine, what a choice
// powers, and which settings a choice writes. The webview (views/donateView.ts) only draws the model built here, and
// everything it posts back goes through validateChoice first, so nothing it sends is trusted.

import {
  CPU_LADDER, CPU_MIN_RAM_GB, CPU_POOL_PERK, MAX_LEND_SHARE, defaultRamOffer, installedRamGb, ramChoices, roleMeaning,
} from './contribution';

/** engine/coding_member_select.py MIN_VRAM_GB: a GPU under this joins the GPU pool instead of running a coding model. */
export const MIN_CODING_VRAM_GB = 8;
/** The coordinator admits a CPU contributor with at least this many cores (checked again there). */
export const MIN_CPU_CORES = 4;
/** The smallest RAM amount a whole coding model can be given next to its GPU (its experts live in system RAM). */
export const GPU_RAM_FROM_GB = 16;
export const GPU_RAM_STEP_GB = 8;
/** The RAM the GPU's model may be offered stops this far below the total, so the owner's computer keeps some. */
export const GPU_RAM_KEEP_GB = 4;
/** Fewer threads than this cannot serve the phone pool next to a GPU job (engine/worker.py). */
export const MIN_THREADS = 2;
/** The threads choice (and the dlne.worker.maxCpuThreads setting) for "let the engine decide". */
export const THREADS_AUTO = -1;
/** The GPU RAM choice for "count the RAM that is free when the job starts" (today's behaviour, codingMaxRamGb). */
export const GPU_RAM_AUTO = 0;

/** What the owner gets back for a GPU: the same words as the first-time prompt (lendGpuPrompt). */
export const GPU_PERK = 'the coding agent first in line, with no chat limit, while your GPU has a job';

/** The GPU as the extension found it (WorkerStatus.gpu fits this). */
export interface DonateGpuInfo {
  hasGpu?: boolean;
  name?: string;
  vramTotalGb?: number;
  integratedGpu?: string;
}

export interface DonateMachine {
  gpu: { available: boolean; name: string; vramGb: number; reason: string };
  /** The RAM the machine was sold with, in whole GB (installedRamGb). */
  totalRamGb: number;
  /** Logical CPUs. */
  cpus: number;
}

/** The machine as the panel sees it, from what the worker status, `os.totalmem()` and `os.cpus()` report. */
export function donateMachine(gpu: DonateGpuInfo | undefined, totalRamBytes: number, cpus: number): DonateMachine {
  const name = gpu?.name ?? '';
  let reason = '';
  if (name === 'Probing GPU...') {
    reason = 'Looking for your GPU...';
  } else if (!gpu?.hasGpu) {
    reason = gpu?.integratedGpu
      ? `${gpu.integratedGpu} is an integrated GPU; only a discrete GPU, Intel Arc 140V/140T or AMD Strix Halo counts.`
      : 'No usable GPU was found on this computer.';
  }
  return {
    gpu: {
      available: !!gpu?.hasGpu,
      name: gpu?.hasGpu ? name : '',
      vramGb: gpu?.hasGpu ? Math.max(1, Math.round(gpu.vramTotalGb ?? 0)) : 0,
      reason,
    },
    totalRamGb: installedRamGb(totalRamBytes),
    cpus: Number.isFinite(cpus) ? Math.max(0, Math.floor(cpus)) : 0,
  };
}

/** What the owner has set (the dlne.worker.* settings the panel reads and writes). */
export interface DonateSettings {
  device: string;
  /** -1: all of the GPU's memory. */
  maxVramGb: number;
  /** Only a value the owner set (inspect().globalValue); null when it is still the default (4). */
  maxRamGb: number | null;
  alsoCpuRamGb: number;
  /** -1: automatic. */
  maxCpuThreads: number;
  allowCodingMember: boolean;
}

/** The slice of a workspace configuration that readDonateSettings uses. */
interface ConfigReader {
  get<T>(key: string, fallback?: T): T | undefined;
  inspect?<T>(key: string): { globalValue?: T } | undefined;
}

export function readDonateSettings(config: ConfigReader): DonateSettings {
  const explicitRam = config.inspect?.<number>('worker.maxRamGb')?.globalValue;
  const number = (key: string, fallback: number): number => {
    const value = config.get<number>(key, fallback);
    return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
  };
  return {
    device: config.get<string>('worker.device', 'auto') ?? 'auto',
    maxVramGb: number('worker.maxVramGb', -1),
    maxRamGb: typeof explicitRam === 'number' && Number.isFinite(explicitRam) && explicitRam > 0 ? explicitRam : null,
    alsoCpuRamGb: number('worker.alsoCpuRamGb', 0),
    maxCpuThreads: number('worker.maxCpuThreads', THREADS_AUTO),
    allowCodingMember: config.get<boolean>('worker.allowCodingMember', true) === true,
  };
}

/** What the sliders say. This is what the webview posts, and what the settings are derived from. */
export interface DonateChoice {
  gpuOn: boolean;
  /** Whole GB; the card's full size means "all" (-1 in the setting). */
  vramGb: number;
  /** RAM for the GPU's coding model, GB; GPU_RAM_AUTO (0) counts the RAM that is free when the job starts. */
  gpuRamGb: number;
  cpuOn: boolean;
  /** RAM lent to the phone app, GB (one of the ramChoices stops). */
  cpuRamGb: number;
  /** THREADS_AUTO, or 2 up to the logical CPU count. */
  threads: number;
}

// ---------------------------------------------------------------------------------------------
// What each slider offers on this machine
// ---------------------------------------------------------------------------------------------

export function vramRange(m: DonateMachine): { min: number; max: number } {
  const max = m.gpu.available ? m.gpu.vramGb : 0;
  return { min: Math.min(MIN_CODING_VRAM_GB, max), max };
}

/** Auto, then 16, 24, 32... up to the total RAM less what the owner keeps. */
export function gpuRamStops(m: DonateMachine): number[] {
  const stops = [GPU_RAM_AUTO];
  for (let gb = GPU_RAM_FROM_GB; gb <= m.totalRamGb - GPU_RAM_KEEP_GB; gb += GPU_RAM_STEP_GB) {
    stops.push(gb);
  }
  return stops;
}

/** The amounts a CPU may lend, smallest first (empty when it cannot lend the minimum, or has too few cores). */
export function cpuRamStops(m: DonateMachine): Array<{ gb: number; runs: string }> {
  return m.cpus < MIN_CPU_CORES ? [] : ramChoices(m.totalRamGb).sort((a, b) => a.gb - b.gb);
}

/** THREADS_AUTO, then 2 up to the logical CPU count. */
export function threadStops(m: DonateMachine): number[] {
  const stops = [THREADS_AUTO];
  for (let n = MIN_THREADS; n <= m.cpus; n++) {
    stops.push(n);
  }
  return stops;
}

/** What the engine picks for itself: half the cores, 2 to 8 (never more than there are). */
export function autoThreads(cpus: number): number {
  return Math.min(cpus, Math.max(MIN_THREADS, Math.min(8, Math.floor(cpus / 2))));
}

function cpuUnavailableReason(m: DonateMachine): string {
  if (m.cpus < MIN_CPU_CORES) {
    return `Powering the phone app needs at least ${MIN_CPU_CORES} CPU cores; this computer has ${m.cpus}.`;
  }
  if (cpuRamStops(m).length === 0) {
    return `Powering the phone app needs ${CPU_MIN_RAM_GB} GB lent, at most half your RAM, so a computer with ${CPU_MIN_RAM_GB / MAX_LEND_SHARE} GB or more; this one has ${m.totalRamGb} GB.`;
  }
  return '';
}

/** The stop nearest to `value` (the larger one on a tie). */
function nearest(stops: number[], value: number): number {
  return stops.reduce((best, stop) => {
    const d = Math.abs(stop - value);
    const bestD = Math.abs(best - value);
    return d < bestD || (d === bestD && stop > best) ? stop : best;
  });
}

// ---------------------------------------------------------------------------------------------
// Settings to choice, and a posted choice to a valid one
// ---------------------------------------------------------------------------------------------

/** The choice the settings describe on this machine (what the panel shows before the owner touches it). */
export function choiceFromSettings(s: DonateSettings, m: DonateMachine): DonateChoice {
  const hasGpu = m.gpu.available;
  const cpuStops = cpuRamStops(m);
  const cpuOk = cpuStops.length > 0;
  let gpuOn = false;
  let cpuOn = false;
  switch (s.device) {
    case 'gpu': gpuOn = hasGpu; break;
    case 'cpu': cpuOn = cpuOk; break;
    case 'both': gpuOn = hasGpu; cpuOn = cpuOk; break;
    default: gpuOn = hasGpu; cpuOn = !hasGpu && cpuOk;   // "auto": the GPU if there is one, else the CPU
  }
  // A stale device: "gpu" on a computer that has none does what "auto" does, instead of leaving nothing to donate.
  if (!gpuOn && !cpuOn && !hasGpu && cpuOk && s.device !== 'cpu') {
    cpuOn = true;
  }

  const range = vramRange(m);
  const vramGb = s.maxVramGb > 0 ? Math.min(range.max, Math.max(range.min, Math.round(s.maxVramGb))) : range.max;

  // dlne.worker.maxRamGb is the CPU's amount when the CPU lends alone (also "both" without a usable GPU, which is
  // "cpu"), and the GPU's model otherwise.
  const cpuAlone = s.device === 'cpu' || !hasGpu;
  const gpuRamGb = !cpuAlone && s.maxRamGb !== null && s.maxRamGb >= GPU_RAM_FROM_GB ? nearest(gpuRamStops(m), s.maxRamGb) : GPU_RAM_AUTO;
  const wanted = cpuAlone ? s.maxRamGb : s.alsoCpuRamGb > 0 ? s.alsoCpuRamGb : null;
  const cpuRamGb = cpuOk ? nearest(cpuStops.map((c) => c.gb), wanted ?? defaultRamOffer(m.totalRamGb) ?? cpuStops[0].gb) : 0;

  const threads = s.maxCpuThreads >= 1 ? Math.min(Math.max(MIN_THREADS, Math.round(s.maxCpuThreads)), Math.max(m.cpus, MIN_THREADS)) : THREADS_AUTO;
  return { gpuOn, vramGb, gpuRamGb, cpuOn, cpuRamGb, threads: m.cpus >= MIN_THREADS ? threads : THREADS_AUTO };
}

/**
 * A choice the webview posted, made valid for this machine: a toggle for something the machine cannot do is off,
 * every amount is clamped to its range or snapped to its nearest stop, and a field that is missing or not a
 * number falls back to `fallback`. Threads are never 0: an out-of-range value is "automatic", not "one" or "all".
 */
export function validateChoice(raw: unknown, m: DonateMachine, fallback: DonateChoice): DonateChoice {
  const src = raw && typeof raw === 'object' && !Array.isArray(raw) ? (raw as Record<string, unknown>) : {};
  const flag = (key: string, base: boolean) => (typeof src[key] === 'boolean' ? (src[key] as boolean) : base);
  const num = (key: string, base: number) => (typeof src[key] === 'number' && Number.isFinite(src[key]) ? (src[key] as number) : base);

  const range = vramRange(m);
  const cpuStops = cpuRamStops(m);
  const threads = num('threads', fallback.threads);
  return {
    gpuOn: m.gpu.available && flag('gpuOn', fallback.gpuOn),
    vramGb: Math.min(range.max, Math.max(range.min, Math.round(num('vramGb', fallback.vramGb)))),
    gpuRamGb: num('gpuRamGb', fallback.gpuRamGb) <= 0 ? GPU_RAM_AUTO : nearest(gpuRamStops(m), num('gpuRamGb', fallback.gpuRamGb)),
    cpuOn: cpuStops.length > 0 && flag('cpuOn', fallback.cpuOn),
    cpuRamGb: cpuStops.length > 0 ? nearest(cpuStops.map((c) => c.gb), num('cpuRamGb', fallback.cpuRamGb)) : 0,
    threads: threads < 1 || m.cpus < MIN_THREADS ? THREADS_AUTO : Math.min(Math.max(MIN_THREADS, Math.round(threads)), m.cpus),
  };
}

/** The setting changes a choice makes, as [key under "dlne", value]; undefined clears a setting back to its default. */
export function settingsToWrite(c: DonateChoice, m: DonateMachine): Array<[string, unknown]> {
  const writes: Array<[string, unknown]> = [];
  if (c.gpuOn || c.cpuOn) {
    writes.push(['worker.device', c.gpuOn && c.cpuOn ? 'both' : c.gpuOn ? 'gpu' : 'cpu']);
  }
  if (c.gpuOn) {
    writes.push(['worker.maxVramGb', c.vramGb >= vramRange(m).max ? -1 : c.vramGb]);
    writes.push(['worker.maxRamGb', c.gpuRamGb > 0 ? c.gpuRamGb : undefined]);
    if (c.cpuOn) writes.push(['worker.alsoCpuRamGb', c.cpuRamGb]);
  } else if (c.cpuOn) {
    writes.push(['worker.maxRamGb', c.cpuRamGb]);
  }
  writes.push(['worker.maxCpuThreads', c.threads]);
  return writes;
}

/**
 * Whether a worker started with `started` needs a restart to follow `current`. Threads are left out: the engine
 * reads them from config.txt while it runs. Amounts of a job that is off do not count.
 */
export function needsRestart(started: DonateSettings | null | undefined, current: DonateChoice, m: DonateMachine): boolean {
  return !!started && donationShape(choiceFromSettings(started, m), false) !== donationShape(current, false);
}

/** What a choice donates: the amounts of a job that is off are left out, because no setting holds them. */
function donationShape(c: DonateChoice, withThreads: boolean): string {
  return JSON.stringify([
    c.gpuOn, c.gpuOn ? c.vramGb : 0, c.gpuOn ? c.gpuRamGb : 0, c.cpuOn, c.cpuOn ? c.cpuRamGb : 0, withThreads ? c.threads : 0,
  ]);
}

/**
 * The sliders' last words (`draft`) are kept while the panel is open because the settings cannot hold them all: the
 * amount of a job that is switched off, and a choice with nothing on. It stays only as long as the settings still say
 * the same about what is switched on; if they were changed elsewhere, the settings win.
 */
export function reconcileDraft(draft: DonateChoice | undefined, s: DonateSettings, m: DonateMachine): DonateChoice | undefined {
  if (!draft) return undefined;
  if (!draft.gpuOn && !draft.cpuOn) return draft;
  return donationShape(draft, true) === donationShape(choiceFromSettings(s, m), true) ? draft : undefined;
}

// ---------------------------------------------------------------------------------------------
// The model the webview draws
// ---------------------------------------------------------------------------------------------

export interface SliderStop {
  value: number;
  label: string;
  /** Written under the label when the slider shows its stops (what the amount runs). */
  sub?: string;
}

export interface StopSlider {
  stops: SliderStop[];
  index: number;
}

export interface PowerLine {
  kind: 'gpu' | 'cpu' | 'threads' | 'perk' | 'warn';
  text: string;
}

export interface DonatePanelModel {
  state: string;
  /** A worker this editor did not start is running: its settings are not these, so nothing here is editable. */
  locked: boolean;
  /** Said when locked. */
  lockedNote: string;
  choice: DonateChoice;
  gpu: {
    available: boolean;
    reason: string;
    /** "RTX 4060, 8 GB". */
    detail: string;
    on: boolean;
    blurb: string;
    vram: { min: number; max: number; value: number; label: string };
    /** Null when the GPU's RAM does not matter (a card under 8 GB, or no RAM to spare for it). */
    ram: (StopSlider & { label: string }) | null;
  };
  cpu: {
    available: boolean;
    reason: string;
    on: boolean;
    blurb: string;
    requirement: string;
    ram: (StopSlider & { label: string }) | null;
    threads: StopSlider & { label: string; note: string };
  };
  powers: PowerLine[];
  button: { label: string; action: 'start' | 'stop'; disabled: boolean; hint: string };
  apply: { visible: boolean; disabled: boolean; label: string };
}

const GPU_BLURB =
  'A GPU is what the swarm needs most: with 8 GB of VRAM or more it can run a whole coding model for other people, and a smaller one holds a share of the shared model.';
const CPU_BLURB = 'A CPU with RAM powers the DLNE phone app: the RAM you lend picks the model it runs.';
const CPU_REQUIREMENT = `Needs at least ${MIN_CPU_CORES} cores, AVX2 and enough memory bandwidth; the swarm checks when you join.`;
const THREADS_NOTE = 'More threads add little: the work waits on memory speed, not on cores. A change applies at once.';
export const APPLY_LABEL = 'Apply (restarts the worker)';

/** The smallest ladder model a member lending `gb` runs, in a few words (for the label under a slider stop). */
function shortModel(gb: number): string {
  const rung = [...CPU_LADDER].reverse().find((r) => r.gb <= gb) ?? CPU_LADDER[0];
  return rung.short;
}

function stopSlider(values: number[], selected: number, label: (v: number) => string, sub?: (v: number) => string): StopSlider {
  const index = Math.max(0, values.indexOf(nearest(values, selected)));
  return { stops: values.map((v) => ({ value: v, label: label(v), ...(sub ? { sub: sub(v) } : {}) })), index };
}

function powerLines(c: DonateChoice, m: DonateMachine, s: DonateSettings): PowerLine[] {
  const lines: PowerLine[] = [];
  if (!c.gpuOn && !c.cpuOn) {
    return [{ kind: 'warn', text: 'Nothing is switched on yet. Turn on your GPU or your CPU to donate.' }];
  }
  if (c.gpuOn) {
    const range = vramRange(m);
    const lending = c.vramGb < range.max ? ` Lending ${c.vramGb} of ${range.max} GB of VRAM.` : '';
    if (range.max < MIN_CODING_VRAM_GB) {
      lines.push({
        kind: 'gpu',
        text: `GPU pool: ${roleMeaning('rpc')} A GPU needs at least ${MIN_CODING_VRAM_GB} GB of VRAM to run a coding model by itself; yours has ${range.max} GB, which is under what the swarm can use for that.${lending}`,
      });
    } else if (!s.allowCodingMember) {
      lines.push({ kind: 'gpu', text: `GPU pool: ${roleMeaning('rpc')} The coding-member job is switched off in dlne.worker.allowCodingMember.${lending}` });
    } else {
      const ram = c.gpuRamGb > 0 ? `It may use up to ${c.gpuRamGb} GB of your RAM next to the GPU.` : 'It counts the RAM that is free when it starts.';
      lines.push({
        kind: 'gpu',
        text: `Coding member: your GPU runs the Qwen3.6 coding model and answers other people's coding requests. The swarm's biggest need. ${ram}${lending} A short speed test when the worker joins decides whether your GPU takes this job.`,
      });
      if (m.totalRamGb < GPU_RAM_FROM_GB) {
        lines.push({
          kind: 'warn',
          text: `This computer has ${m.totalRamGb} GB of RAM, under the roughly ${GPU_RAM_FROM_GB} to 24 GB the coding model needs next to the GPU, so your GPU will most likely join the GPU pool instead.`,
        });
      }
    }
  }
  if (c.cpuOn) {
    const stop = cpuRamStops(m).find((r) => r.gb === c.cpuRamGb);
    lines.push({ kind: 'cpu', text: `Phone app: ${c.cpuRamGb} GB runs ${stop ? stop.runs : 'a small model'}.` });
  }
  const auto = autoThreads(m.cpus);
  lines.push({
    kind: 'threads',
    text: c.threads === THREADS_AUTO
      ? `Auto: ${auto} threads of ${m.cpus} for the swarm; the rest stay yours.`
      : c.threads >= m.cpus
      ? `All ${m.cpus} threads go to the swarm; none are left for you.`
      : `${c.threads} threads of ${m.cpus} for the swarm; the rest stay yours.`,
  });
  const back = c.gpuOn && c.cpuOn
    ? `${GPU_PERK}, and otherwise ${CPU_POOL_PERK}`
    : c.gpuOn ? GPU_PERK : CPU_POOL_PERK;
  lines.push({ kind: 'perk', text: `You get back: ${back}.` });
  return lines;
}

export interface BuildInput {
  machine: DonateMachine;
  settings: DonateSettings;
  /** The worker's state ('idle', 'starting', 'contributing', 'error'). */
  state: string;
  /** A worker this editor did not start is running. */
  external?: boolean;
  /** The settings the running worker was started with (null: not known). */
  started?: DonateSettings | null;
  /** What the sliders say now, when it is not what the settings say (a draft, or a preview while dragging). */
  choice?: DonateChoice;
}

export function buildDonatePanel(input: BuildInput): DonatePanelModel {
  const { machine: m, settings: s } = input;
  const base = choiceFromSettings(s, m);
  const c = input.choice ? validateChoice(input.choice, m, base) : base;
  const running = input.state === 'contributing' || input.state === 'starting';
  const locked = running && input.external === true;

  const range = vramRange(m);
  const ramStops = gpuRamStops(m);
  const gpuRamUseful = m.gpu.available && range.max >= MIN_CODING_VRAM_GB && s.allowCodingMember && ramStops.length > 1;
  const cpuStops = cpuRamStops(m);
  const cpuReason = cpuUnavailableReason(m);
  const threadValues = threadStops(m);
  const thread = stopSlider(threadValues, c.threads, (n) => (n === THREADS_AUTO ? `Auto (${autoThreads(m.cpus)})` : String(n)));

  const nothing = !c.gpuOn && !c.cpuOn;
  const stopping = running;
  let hint = '';
  if (!stopping && nothing) {
    // When neither can be offered the sections above say why; here it is only said that nothing can start.
    hint = m.gpu.available || cpuStops.length > 0 ? 'Turn on your GPU or your CPU first.' : 'This computer cannot donate yet.';
  }
  const restart = running && !locked && needsRestart(input.started, c, m);

  return {
    state: input.state,
    locked,
    lockedNote: locked ? 'A DLNE worker started outside VS Code is running, with its own settings. Stop it here, then donate again with the settings below.' : '',
    choice: c,
    gpu: {
      available: m.gpu.available,
      reason: m.gpu.available ? '' : m.gpu.reason,
      detail: m.gpu.available ? `${m.gpu.name}, ${m.gpu.vramGb} GB` : '',
      on: c.gpuOn,
      blurb: GPU_BLURB,
      vram: { min: range.min, max: range.max, value: c.vramGb, label: c.vramGb >= range.max ? `All ${range.max} GB` : `${c.vramGb} GB` },
      ram: gpuRamUseful
        ? { ...stopSlider(ramStops, c.gpuRamGb, (gb) => (gb === GPU_RAM_AUTO ? 'Auto' : `${gb} GB`)), label: c.gpuRamGb > 0 ? `${c.gpuRamGb} GB` : 'Auto' }
        : null,
    },
    cpu: {
      available: cpuStops.length > 0,
      reason: cpuReason,
      on: c.cpuOn,
      blurb: CPU_BLURB,
      requirement: CPU_REQUIREMENT,
      ram: cpuStops.length > 0
        ? { ...stopSlider(cpuStops.map((r) => r.gb), c.cpuRamGb, (gb) => `${gb} GB`, shortModel), label: `${c.cpuRamGb} GB` }
        : null,
      threads: {
        ...thread,
        label: thread.stops[thread.index].label,
        // The thread limit caps the GPU's coding model too, so it matters with the CPU switched off.
        note: c.gpuOn && !c.cpuOn ? `${THREADS_NOTE} It also caps the CPU threads your GPU's coding model uses.` : THREADS_NOTE,
      },
    },
    powers: powerLines(c, m, s),
    button: stopping
      ? { label: 'Stop donating', action: 'stop', disabled: false, hint: '' }
      : { label: 'Donate compute', action: 'start', disabled: nothing, hint },
    apply: { visible: restart, disabled: nothing, label: APPLY_LABEL },
  };
}
