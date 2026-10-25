// What this machine contributes (dlne.worker.device), decided before the worker starts.

export type DeviceChoice = 'auto' | 'gpu' | 'cpu';

/** The coordinator's floor for RAM lent by a CPU-only node (CPU_MIN_RAM_GB, engine/config.py). */
export const CPU_MIN_RAM_GB = 8;
/** What the extension offers to lend when a CPU-only node is below the floor. */
export const CPU_OFFER_RAM_GB = 12;

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

export function planContribution(
  device: string | undefined,
  hasGpu: boolean,
  allowedGpus: string,
  maxRamGb: number
): ContributionPlan {
  const choice: DeviceChoice = device === 'gpu' || device === 'cpu' ? device : 'auto';
  if (choice === 'gpu' && !hasGpu) {
    return {
      cpuOnly: false,
      allowedGpus,
      needsMoreRam: false,
      error: 'DLNE found no GPU on this machine, and dlne.worker.device is set to "gpu". Set it to "auto" or "cpu" to contribute the CPU instead.',
    };
  }
  const cpuOnly = choice === 'cpu' || !hasGpu;
  return {
    cpuOnly,
    allowedGpus: choice === 'cpu' ? 'none' : allowedGpus,
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
