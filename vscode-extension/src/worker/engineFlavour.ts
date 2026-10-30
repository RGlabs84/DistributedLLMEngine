/**
 * The engine flavour this computer's worker runs: Vulkan, or CUDA on an NVIDIA GPU that can (docs/release-2.7-contract.md
 * sections 1 and 7). One controller holds the facts (the setting, the answer to the CUDA question, what the worker said
 * about this GPU and about the engine it started) and works out which flavour the worker is started with. The panel
 * calls the three functions at the bottom; the worker manager feeds the controller what the worker prints.
 *
 * The order of events on a computer that may use CUDA and has not chosen yet: the worker starts on Vulkan, prints
 * DLNE_CUDA (before it downloads any engine), the controller asks the question once, and only a "Use CUDA" lets the
 * worker be restarted on CUDA, which is when the 600 MB download happens. Nothing CUDA is fetched before that.
 */
import * as vscode from 'vscode';
import { CudaProbe, EngineReport, parseCudaProbe, parseEngineReport } from './contribution';

export type EngineSetting = 'auto' | 'vulkan' | 'cuda';
export type EngineFlavour = 'vulkan' | 'cuda';

export interface EngineState {
  /** dlne.worker.engine. */
  setting: EngineSetting;
  /** The flavour the worker runs now (its DLNE_ENGINE line); null while no worker runs. */
  running: EngineFlavour | null;
  /** Whether this computer may run CUDA (its DLNE_CUDA line); null until the worker has said. */
  capable: boolean | null;
  /** Why it may not, in plain words; "" when it may. */
  reason: string;
  /** The GPU's name, from DLNE_CUDA. */
  gpu: string;
  /** Why CUDA fell back to Vulkan; "" when it did not. */
  fallback: string;
  /** The answer to the CUDA question. */
  consent: 'yes' | 'never' | 'later' | 'unasked';
}

/** The extension's global state keys. The consent's shape is fixed by the contract (section 7). */
export const CONSENT_KEY = 'dlne.cudaConsent';
const PROBE_KEY = 'dlne.cudaProbe';

/** "Not now" asks again after this long. */
export const CONSENT_LATER_MS = 3 * 24 * 60 * 60 * 1000;

/** The question, word for word from the contract (section 1), and its three answers. */
export const CUDA_QUESTION =
  'Your NVIDIA GPU can run DLNE about 30-50% faster with the CUDA engine. It is a one-time download of about 600 MB. Use CUDA?';
export const CUDA_USE = 'Use CUDA';
export const CUDA_LATER = 'Not now';
export const CUDA_KEEP = 'Keep Vulkan';

/** What the stored answer is: 'yes', 'never', `{ later: <epoch ms to ask again> }`, or nothing yet. */
export type StoredConsent = 'yes' | 'never' | { later: number };

/** The stored answer as the state reports it. A "later" whose time has passed counts as not asked yet. */
export function consentOf(stored: unknown, now: number): EngineState['consent'] {
  if (stored === 'yes' || stored === 'never') return stored;
  if (stored && typeof stored === 'object' && Number.isFinite((stored as { later?: unknown }).later)) {
    return now >= (stored as { later: number }).later ? 'unasked' : 'later';
  }
  return 'unasked';
}

/**
 * The flavour the worker is started with. CUDA needs all of: a setting that allows it (auto or cuda), a GPU the worker
 * says is capable, the owner's "Use CUDA", and no failed start of the CUDA engine since the setting last changed.
 */
export function resolveFlavour(
  setting: EngineSetting,
  consent: EngineState['consent'],
  capable: boolean | null,
  cudaFailed: boolean
): EngineFlavour {
  if (setting === 'vulkan' || cudaFailed) return 'vulkan';
  return capable === true && consent === 'yes' ? 'cuda' : 'vulkan';
}

/** What the panel says after a fall-back (contract section 1). */
export function fallbackText(why: string): string {
  return `CUDA could not start: ${why}; running on Vulkan`;
}

/** `2.7.0+cuda`: the version with the flavour as build metadata. Display only, never compared. */
export function engineLabel(version: string, flavour: EngineFlavour | null | undefined): string {
  return flavour ? `${version}+${flavour}` : version;
}

function settingOf(raw: unknown): EngineSetting {
  return raw === 'vulkan' || raw === 'cuda' ? raw : 'auto';
}

export class EngineFlavourController {
  private probe: CudaProbe | null = null;
  private running: EngineFlavour | null = null;
  private fallback = '';
  /** The CUDA engine failed its start-up check: Vulkan until the setting (or the answer) changes. */
  private cudaFailed = false;
  /** The flavour the current worker was started with; null while none runs. */
  private startedFlavour: EngineFlavour | null = null;
  /** The question was put (or is on screen) in this session; a restart of the worker does not put it again. */
  private askedThisSession = false;
  private lastSetting: EngineSetting;
  private readonly emitter = new vscode.EventEmitter<EngineState>();
  private readonly settingListener: vscode.Disposable | undefined;

  /** Set by the worker manager: the resolved flavour no longer matches the running worker's, restart it. */
  public onRestartNeeded: (() => void) | undefined;

  constructor(private readonly store: vscode.Memento | undefined, private readonly now: () => number = Date.now) {
    this.lastSetting = this.setting();
    const saved = store?.get<CudaProbe>(PROBE_KEY);
    if (saved && typeof saved.capable === 'boolean') this.probe = saved;
    if (typeof vscode.workspace.onDidChangeConfiguration === 'function') {
      this.settingListener = vscode.workspace.onDidChangeConfiguration((e) => {
        if (e.affectsConfiguration('dlne.worker.engine')) void this.settingChanged();
      });
    }
  }

  public readonly onChanged = this.emitter.event;

  public setting(): EngineSetting {
    return settingOf(vscode.workspace.getConfiguration('dlne').get<string>('worker.engine', 'auto'));
  }

  private consent(): EngineState['consent'] {
    return consentOf(this.store?.get<StoredConsent>(CONSENT_KEY), this.now());
  }

  public getState(): EngineState {
    const setting = this.setting();
    const consent = this.consent();
    let reason = '';
    if (this.probe && !this.probe.capable) reason = this.probe.reason;
    else if (setting === 'cuda' && consent !== 'yes') reason = 'CUDA needs your OK to download the engine first';
    return {
      setting,
      running: this.running,
      capable: this.probe ? this.probe.capable : null,
      reason,
      gpu: this.probe?.gpu ?? '',
      fallback: this.fallback,
      consent,
    };
  }

  /** The flavour a worker started now gets. */
  public resolve(): EngineFlavour {
    return resolveFlavour(this.setting(), this.consent(), this.probe ? this.probe.capable : null, this.cudaFailed);
  }

  /**
   * A worker is about to start: the flavour it gets in DLNE_ENGINE_FLAVOR. A machine that lends its CPU only runs no
   * GPU engine, so it is always Vulkan.
   */
  public beginStart(cpuOnly: boolean = false): EngineFlavour {
    const flavour = cpuOnly ? 'vulkan' : this.resolve();
    this.startedFlavour = flavour;
    this.running = null;
    this.fallback = '';
    return flavour;
  }

  /** The worker ended (stopped, crashed, or about to be restarted). */
  public noteStopped(): void {
    if (this.running === null && this.startedFlavour === null) return;
    this.running = null;
    this.startedFlavour = null;
    this.emitter.fire(this.getState());
  }

  /** Everything the worker printed: the marker lines among it feed the state. */
  public noteOutput(text: string): void {
    const probe = parseCudaProbe(text);
    if (probe) this.noteProbe(probe);
    const engine = parseEngineReport(text);
    if (engine) this.noteEngine(engine);
  }

  private noteProbe(probe: CudaProbe): void {
    const changed = JSON.stringify(probe) !== JSON.stringify(this.probe);
    this.probe = probe;
    if (changed) {
      void this.store?.update(PROBE_KEY, probe);
      this.emitter.fire(this.getState());
    }
    void this.evaluate();
  }

  private noteEngine(report: EngineReport): void {
    this.running = report.flavour;
    this.fallback = report.fallback;
    if (report.fallback) {
      // The worker started Vulkan instead and will not try CUDA again; neither does the extension until a change.
      this.cudaFailed = true;
      this.startedFlavour = 'vulkan';
    } else if (report.flavour === 'cuda') {
      this.cudaFailed = false;
    }
    this.emitter.fire(this.getState());
  }

  /** The question, put once. The answer is stored; null when the question was closed without one. */
  private async ask(): Promise<'yes' | 'later' | 'never' | null> {
    const choice = await vscode.window.showInformationMessage(CUDA_QUESTION, CUDA_USE, CUDA_LATER, CUDA_KEEP);
    let answer: 'yes' | 'later' | 'never' | null = null;
    if (choice === CUDA_USE) {
      answer = 'yes';
      await this.store?.update(CONSENT_KEY, 'yes');
    } else if (choice === CUDA_LATER) {
      answer = 'later';
      await this.store?.update(CONSENT_KEY, { later: this.now() + CONSENT_LATER_MS });
    } else if (choice === CUDA_KEEP) {
      answer = 'never';
      await this.store?.update(CONSENT_KEY, 'never');
    }
    // A new answer can change the flavour, and the panel shows the consent.
    this.cudaFailed = false;
    this.emitter.fire(this.getState());
    return answer;
  }

  /** What follows any change: the question when it is due, then a restart when the flavour no longer fits the worker. */
  private async evaluate(): Promise<void> {
    if (
      this.setting() === 'auto' && this.probe?.capable === true && this.consent() === 'unasked' && !this.askedThisSession
    ) {
      this.askedThisSession = true;
      await this.ask();
    }
    if (this.startedFlavour !== null && this.resolve() !== this.startedFlavour) this.onRestartNeeded?.();
  }

  /** The setting changed (in Settings, in settings.json, or by setEngineSetting). */
  private async settingChanged(): Promise<void> {
    const setting = this.setting();
    if (setting === this.lastSetting) return;
    this.lastSetting = setting;
    this.cudaFailed = false;
    // CUDA written by hand into settings.json still needs the owner's OK before anything is downloaded.
    if (setting === 'cuda' && this.consent() !== 'yes' && this.probe?.capable !== false) {
      if ((await this.ask()) !== 'yes') {
        await vscode.workspace.getConfiguration('dlne').update('worker.engine', 'auto', vscode.ConfigurationTarget.Global);
        this.lastSetting = 'auto';
      }
    }
    this.emitter.fire(this.getState());
    await this.evaluate();
  }

  /** Sets dlne.worker.engine; turning CUDA on asks the consent question first. */
  public async setSetting(value: EngineSetting): Promise<void> {
    const wanted = settingOf(value);
    if (wanted === 'cuda' && this.consent() !== 'yes' && this.probe?.capable !== false) {
      if ((await this.ask()) !== 'yes') return;
    }
    await vscode.workspace.getConfiguration('dlne').update('worker.engine', wanted, vscode.ConfigurationTarget.Global);
    // The editor tells us too (onDidChangeConfiguration), and a second look finds nothing new.
    await this.settingChanged();
  }

  public dispose(): void {
    this.settingListener?.dispose();
    this.emitter.dispose();
  }
}

let current: EngineFlavourController | undefined;
let forwarding: vscode.Disposable | undefined;
/** What the panel listens to: it keeps working when the controller behind it is replaced. */
const changed = new vscode.EventEmitter<EngineState>();

function adopt(next: EngineFlavourController): EngineFlavourController {
  forwarding?.dispose();
  current?.dispose();
  current = next;
  forwarding = next.onChanged((state) => changed.fire(state));
  return next;
}

/** The controller for this editor window; the worker manager makes it with the extension's global state. */
export function attachEngineFlavour(store: vscode.Memento | undefined): EngineFlavourController {
  return adopt(new EngineFlavourController(store));
}

function controller(): EngineFlavourController {
  return current ?? adopt(new EngineFlavourController(undefined));
}

export function getEngineState(): EngineState {
  return controller().getState();
}

/** Sets dlne.worker.engine; turning CUDA on asks the consent question first. */
export async function setEngineSetting(value: EngineSetting): Promise<void> {
  await controller().setSetting(value);
}

export function onEngineStateChanged(listener: (state: EngineState) => void): vscode.Disposable {
  return changed.event(listener);
}
