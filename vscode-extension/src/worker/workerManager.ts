import * as vscode from 'vscode';
import * as child_process from 'child_process';
import * as path from 'path';
import * as fs from 'fs';
import * as net from 'net';
import * as os from 'os';
import * as http from 'http';
import * as https from 'https';
import { EngineVerificationError, FetchResult, RELEASE_PUBKEY_HEX, fetchVerifiedEngine, fetchVerifiedRpcBin, hostRpcPlatform, checkEngineUpdate} from './engineRelease';
import { resolveServerUrl } from '../serverUrl';
import {
  CODING_MEMBER_ALLOWED_KEY, CODING_MEMBER_NOTICE, CPU_BELOW_MINIMUM_EXIT_CODE, CPU_MIN_RAM_GB, CPU_POOL_HEADLINE,
  codingMaxRamGb, ALSO_CPU_REASON, ALSO_CPU_RAM_KEY, LEND_BOTH, alsoCpuConfigUpdate, bothRamTooSmallText, parseAlso,
  ContributionPlan, LEND_CPU_ONLY, LEND_GPU, RoleAssignment, RoleDetail, WorkerRole, cpuReason, installedRamGb,
  lendGpuPrompt, lendLabel, parseRefusal, parseRole, parseVulkanSummary, pickVulkanGpu, planContribution, ramChoices,
  ramOfferText, ramTooSmallText, setConfigValue, workerStartedNotice, powershellExpandCommand, quoteIfSpaced,
  isRpcExecutablePlatform, takeCompleteLines, EXTERNAL_WORKER_LINE, EXTERNAL_WORKER_TEXT, orphanRpcStopCommand,
  WORKER_STOP_GRACE_MS, LEAVING_TEXT, workerStopGraceMs,
  MAX_CPU_THREADS_KEY, maxCpuThreadsConfigUpdate, parseClock, clockMessage, TRAY_WORKER_LINE, TRAY_WORKER_TEXT,
  VCREDIST_MESSAGE, VCREDIST_URL, parseVcredistNeeded, parseDock, parseYield, DockState, YieldState} from './contribution';
import { DonateChoice, DonateMachine, DonateSettings, readDonateSettings, settingsToWrite } from './donatePanel';
import { trayWorkerRunning } from './trayApp';

/** Remembered in the editor's global state: the GPU owner has been told what lending a GPU means. */
const LEND_NOTICE_KEY = 'dlne.lendGpuNoticeSeen';
/** Remembered likewise: the owner has been told their GPU became a coding member. */
const CODING_NOTICE_KEY = 'dlne.codingMemberNoticeSeen';

function downloadFile(urlStr: string, destPath: string): Promise<void> {
  return new Promise((resolve, reject) => {
    fs.mkdirSync(path.dirname(destPath), { recursive: true });
    const file = fs.createWriteStream(destPath);
    const get = (targetUrl: string) => {
      const client = targetUrl.startsWith('https:') ? https : http;
      client.get(targetUrl, (res) => {
        if (res.statusCode && res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
          get(res.headers.location);
          return;
        }
        if (res.statusCode !== 200) {
          file.close();
          try { fs.unlinkSync(destPath); } catch {}
          reject(new Error(`Download failed with HTTP ${res.statusCode}`));
          return;
        }
        res.pipe(file);
        file.on('finish', () => {
          file.close();
          resolve();
        });
      }).on('error', (err) => {
        file.close();
        try { fs.unlinkSync(destPath); } catch {}
        reject(err);
      });
    };
    get(urlStr);
  });
}

/** A worker that is leaving the swarm gets this long before the "Leaving the swarm" notice (a quick stop never shows it). */
const LEAVING_NOTICE_DELAY_MS = 2000;
/** After the kill, how long to wait for the process to be gone before giving up on it. */
const WORKER_KILL_SETTLE_MS = 3000;
/** The worker this editor started (its process id), so a window that opens after a reload knows it as its own. */
const OWN_WORKER_KEY = 'dlne.worker.ownPid';
/**
 * After this editor's worker from another window (or from before a reload) ends, how long to wait for the window
 * that started it to start the next one (an engine update ends a worker so that it is started again) before
 * starting one here: two windows starting one each would fight over the port.
 */
const ADOPTED_RESTART_GRACE_MS = 30000;

function processAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

/** Whether `pid` is alive and is a DLNE worker (its command line runs dlne_server.py worker), not a reused id. */
function isDlneWorkerProcess(pid: number): boolean {
  if (!processAlive(pid)) return false;
  try {
    const command = process.platform === 'win32'
      ? child_process.execFileSync('powershell.exe', ['-NoProfile', '-Command',
        `(Get-CimInstance Win32_Process -Filter "ProcessId=${pid}").CommandLine`], { encoding: 'utf-8', timeout: 4000, windowsHide: true })
      : child_process.execFileSync('ps', ['-o', 'command=', '-p', String(pid)], { encoding: 'utf-8', timeout: 2000 });
    return command.includes('dlne_server.py') && command.includes('worker');
  } catch {
    return false;
  }
}
// The engine exits with this code when the coordinator runs a different version (engine/worker.py).
const VERSION_MISMATCH_EXIT_CODE = 75;
// Two automatic re-syncs within ten minutes; after that a mismatch is reported instead of looped on.
const MAX_VERSION_RESTARTS = 2;
const VERSION_RESTART_WINDOW_MS = 10 * 60 * 1000;

/** GET a small JSON document. Rejects with an Error carrying `.statusCode` on a non-200 reply. */
function getJson(urlStr: string): Promise<unknown> {
  return new Promise((resolve, reject) => {
    const client = urlStr.startsWith('https:') ? https : http;
    const req = client.get(urlStr, { timeout: 15000 }, (res) => {
      let body = '';
      res.on('data', (chunk) => {
        body += chunk;
        if (body.length > 1_000_000) {
          req.destroy(new Error('response too large'));
        }
      });
      res.on('end', () => {
        if (res.statusCode !== 200) {
          const err: any = new Error(`HTTP ${res.statusCode}`);
          err.statusCode = res.statusCode;
          reject(err);
          return;
        }
        try {
          resolve(JSON.parse(body));
        } catch {
          reject(new Error('the response was not valid JSON'));
        }
      });
    });
    req.on('timeout', () => req.destroy(new Error('timed out')));
    req.on('error', reject);
  });
}

export interface LocalGpuInfo {
  hasGpu: boolean;
  name: string;
  vramTotalGb: number;
  api: 'vulkan' | 'cuda' | 'metal' | 'cpu';
  /** The integrated GPU left out because it does not count as a GPU contributor (hasGpu is false). */
  integratedGpu?: string;
}

export type WorkerState = 'idle' | 'starting' | 'contributing' | 'error';

export interface WorkerStatus {
  state: WorkerState;
  gpu: LocalGpuInfo;
  /** GB lent: VRAM for a GPU, RAM when cpuPool. */
  vramContributedGb: number;
  /** This machine contributes (or would) its CPU, which powers the DLNE phone app, not a GPU. */
  cpuPool: boolean;
  message?: string;
  /**
   * The job the coordinator gave this machine, once the worker has said (a CPU machine is always `app`).
   * Undefined while the worker is not running, and for an engine that does not report one.
   */
  role?: WorkerRole;
  roleDetail?: RoleDetail;
  /**
   * "Both": the worker says its CPU also serves the phone app's pool next to the GPU's own job (`role` is still the
   * GPU's job). Undefined on every other machine, and while the worker is not running.
   */
  alsoCpu?: boolean;
  /**
   * The running worker was not started by this editor (a systemd service, a terminal, another editor): it has its own
   * settings, so the status says so instead of describing it from the settings here. Undefined otherwise.
   */
  external?: boolean;
  /**
   * That outside worker is the one the DLNE Donate tray app runs (trayApp.ts trayWorkerRunning): the sidebar says so
   * by name, and it is stopped from the app itself (its tray icon or its window). Undefined otherwise.
   */
  trayApp?: boolean;
  /** The settings this editor's running worker was started with, so the sidebar can tell when a change needs a restart. */
  startedWith?: DonateSettings;
  /** Whether this machine is docked (wired and on mains), from the worker's last DLNE_DOCK line (2.5.0). */
  dock?: DockState;
  /** Whether this machine is stepping aside for its owner's own load, from the last DLNE_YIELD line (2.5.0). */
  yielding?: YieldState;
  /** The coordinator publishes a newer signed engine than the one installed here (2.5.0; checked every half hour). */
  engineUpdate?: { installed: string | null; available: string };
}

export class WorkerManager {
  private workerProcess: child_process.ChildProcess | null = null;
  /** The worker that is running was started outside this editor (see WorkerStatus.external). */
  private externalWorker = false;
  /**
   * The worker an earlier window of this editor started, still running: VS Code keeps the worker across a window
   * reload (see dispose), so it is this editor's own, not one "started outside VS Code". Its output went with the
   * old window, so what it prints (its job, docked, stepping aside) is not shown until it is restarted here.
   */
  private adoptedPid: number | null = null;
  /** When the adopted worker was last seen ending (see ADOPTED_RESTART_GRACE_MS); null otherwise. */
  private adoptedGoneAt: number | null = null;
  /** That outside worker is the DLNE Donate tray app's (see WorkerStatus.trayApp). Only meaningful with externalWorker. */
  private trayWorker = false;
  /**
   * This machine has no user systemd to ask (a container, WSL, a desktop without a user session bus): every
   * `systemctl --user` fails there, so after the first failure the 5-second check stops asking.
   */
  private systemdUnavailable = false;
  private currentState: WorkerState = 'idle';
  private localGpu: LocalGpuInfo = { hasGpu: false, name: 'Probing GPU...', vramTotalGb: 0, api: 'vulkan' };
  private vramContributed = 0;
  private statusMessage = '';
  private outputChannel: vscode.OutputChannel;
  private onStatusChangeEmitter = new vscode.EventEmitter<WorkerStatus>();
  public readonly onStatusChange = this.onStatusChangeEmitter.event;
  private pollInterval: NodeJS.Timeout | null = null;
  private disposed = false;
  private failedChecksCount = 0;

  constructor(
    private context: vscode.ExtensionContext,
    // The trust anchor for engine releases. Production always uses the baked-in key; tests pass
    // their own so they can sign a release without the maintainer's private key.
    private options: { releasePublicKeyHex?: string } = {}
  ) {
    this.outputChannel = vscode.window.createOutputChannel('DLNE Compute Worker');
    // Synchronous immediate detection so initial UI render accurately reflects contributing state. A worker that is
    // already running was not started by this editor: it is described as such, never from the settings here.
    this.adoptedPid = this.ownWorkerFromBefore();
    const trayActive = this.isTrayWorkerActive();
    if (this.adoptedPid !== null) {
      this.currentState = 'contributing';
    } else if (this.isSystemdActive() || this.isProcessActive() || trayActive) {
      this.currentState = 'contributing';
      this.externalWorker = true;
      this.trayWorker = trayActive;
      this.statusMessage = trayActive ? TRAY_WORKER_LINE : EXTERNAL_WORKER_LINE;
    }
    // The owner's coding-member switch reaches a worker that is already running through the engine's
    // config.txt, which the worker re-reads: writing it when the setting changes needs no restart.
    if (typeof vscode.workspace.onDidChangeConfiguration === 'function') {
      this.settingListener = vscode.workspace.onDidChangeConfiguration((e) => {
        if (e.affectsConfiguration('dlne.worker.allowCodingMember')) {
          this.syncCodingMemberSetting();
        }
        // The same for the thread limit: the engine reads MAX_CPU_THREADS again while it runs.
        if (e.affectsConfiguration('dlne.worker.maxCpuThreads')) {
          this.syncMaxCpuThreadsSetting();
        }
      });
    }
    this.init();
  }

  private settingListener: vscode.Disposable | undefined;

  /**
   * Write `dlne.worker.allowCodingMember` into the installed engine's config.txt as
   * `CODING_MEMBER_ALLOWED=true|false` (engine/config.py: only the literal `true` is true). The
   * switch is off unless the setting is exactly `true`, so a setting that is somehow not a boolean
   * fails in the direction that keeps the GPU out of the coding job. Never throws: a config that
   * cannot be written leaves the engine's own default (allowed) and is logged.
   */
  /**
   * "Both": the engine reads the RAM the CPU lends to the phone pool from ALSO_CPU_RAM_GB in config.txt (a worker the
   * editor does not spawn, the systemd service, has no command line to carry it). A machine that lends both writes
   * the amount; any other machine only clears a leftover positive value, and a config without the key is not touched.
   */
  private syncAlsoCpuSetting(plan: ContributionPlan, engineRoot?: string): void {
    const root = engineRoot ?? (this.findEngineScript() ? path.dirname(this.findEngineScript()) : '');
    if (!root) {
      return;
    }
    const ram = vscode.workspace.getConfiguration('dlne').get<number>('worker.alsoCpuRamGb') ?? 0;
    const file = path.join(root, 'config', 'config.txt');
    try {
      const current = fs.existsSync(file) ? fs.readFileSync(file, 'utf-8') : '';
      const next = alsoCpuConfigUpdate(current, plan.alsoCpu === true && ram > 0, ram);
      if (next === null) {
        return;
      }
      fs.mkdirSync(path.dirname(file), { recursive: true });
      const scratch = `${file}.${process.pid}.tmp`;
      try {
        fs.writeFileSync(scratch, next, 'utf-8');
        fs.renameSync(scratch, file);
      } catch {
        try { fs.unlinkSync(scratch); } catch {}
        fs.writeFileSync(file, next, 'utf-8');
      }
      this.outputChannel.appendLine(`[DLNE] ${ALSO_CPU_RAM_KEY} updated in ${file}`);
    } catch (e: any) {
      this.outputChannel.appendLine(`[DLNE] Could not update ${ALSO_CPU_RAM_KEY} in ${file}: ${e?.message ?? e}`);
    }
  }

  private syncCodingMemberSetting(engineRoot?: string): void {
    const root = engineRoot ?? (this.findEngineScript() ? path.dirname(this.findEngineScript()) : '');
    if (!root) {
      return; // no engine installed yet: the first start writes it
    }
    const allow = vscode.workspace.getConfiguration('dlne').get<boolean>('worker.allowCodingMember', true) === true;
    const file = path.join(root, 'config', 'config.txt');
    try {
      fs.mkdirSync(path.dirname(file), { recursive: true });
      const current = fs.existsSync(file) ? fs.readFileSync(file, 'utf-8') : '';
      const next = setConfigValue(current, CODING_MEMBER_ALLOWED_KEY, allow ? 'true' : 'false');
      if (next === current) {
        return;
      }
      // Beside the real file and then renamed over it, so the worker never reads half a config.
      const scratch = `${file}.${process.pid}.tmp`;
      try {
        fs.writeFileSync(scratch, next, 'utf-8');
        fs.renameSync(scratch, file);
      } catch {
        try { fs.unlinkSync(scratch); } catch {}
        fs.writeFileSync(file, next, 'utf-8'); // Windows refuses a rename over a file another program has open
      }
      this.outputChannel.appendLine(`[DLNE] ${CODING_MEMBER_ALLOWED_KEY}=${allow ? 'true' : 'false'} written to ${file}`);
    } catch (e: any) {
      this.outputChannel.appendLine(`[DLNE] Could not write ${CODING_MEMBER_ALLOWED_KEY} to ${file}: ${e?.message ?? e}`);
    }
  }

  /**
   * Write `dlne.worker.maxCpuThreads` into the installed engine's config.txt as MAX_CPU_THREADS, which the running
   * worker re-reads (no restart). Automatic (-1) is written as -1 or not at all, never 0, which the engine reads as
   * one thread. Never throws; a config that cannot be written is logged.
   */
  private syncMaxCpuThreadsSetting(engineRoot?: string): void {
    const root = engineRoot ?? (this.findEngineScript() ? path.dirname(this.findEngineScript()) : '');
    if (!root) {
      return;
    }
    const threads = vscode.workspace.getConfiguration('dlne').get<number>('worker.maxCpuThreads', -1);
    const file = path.join(root, 'config', 'config.txt');
    try {
      const current = fs.existsSync(file) ? fs.readFileSync(file, 'utf-8') : '';
      const next = maxCpuThreadsConfigUpdate(current, threads);
      if (next === null) {
        return;
      }
      fs.mkdirSync(path.dirname(file), { recursive: true });
      const scratch = `${file}.${process.pid}.tmp`;
      try {
        fs.writeFileSync(scratch, next, 'utf-8');
        fs.renameSync(scratch, file);
      } catch {
        try { fs.unlinkSync(scratch); } catch {}
        fs.writeFileSync(file, next, 'utf-8'); // Windows refuses a rename over a file another program has open
      }
      this.outputChannel.appendLine(`[DLNE] ${MAX_CPU_THREADS_KEY} updated in ${file}`);
    } catch (e: any) {
      this.outputChannel.appendLine(`[DLNE] Could not update ${MAX_CPU_THREADS_KEY} in ${file}: ${e?.message ?? e}`);
    }
  }

  private async init() {
    await this.detectLocalGpu();
    await this.refreshWorkerState();
    // Disposed while the probes ran (VS Code shutting down during startup): start no worker and no timer, which
    // nothing would ever clear.
    if (this.disposed) return;

    // Only auto-resume if the user explicitly enabled worker.autoStart
    const autoStart = vscode.workspace.getConfiguration('dlne').get<boolean>('worker.autoStart', false);
    if (autoStart && this.currentState !== 'contributing' && this.currentState !== 'starting') {
      this.outputChannel.appendLine('[DLNE] Auto-resuming compute worker on startup...');
      this.startWorker().catch(() => {});
    }

    // Poll every 5s to keep state synchronized with systemd / background processes
    this.pollInterval = setInterval(() => {
      this.refreshWorkerState();
    }, 5000);

    // A worker that is not running never learns of a new engine (the sync runs at start): ask the
    // coordinator now and every half hour, and let the sidebar's engine button say so.
    void this.refreshEngineUpdate();
    this.updateCheckInterval = setInterval(() => void this.refreshEngineUpdate(), 30 * 60 * 1000);
  }

  /** Where the extension installs the engine (the same folder ensureWorkerRuntime fills). */
  private runtimeRoot(): string {
    const localAppData = process.env.LOCALAPPDATA;
    return process.platform === 'win32' && localAppData
      ? path.join(localAppData, 'DLNE', 'runtime')
      : path.join(os.homedir(), '.dlne', 'runtime');
  }

  /**
   * Compare the installed engine with the coordinator's published release (engineRelease.checkEngineUpdate),
   * without downloading anything. Nothing installed, no signed release, or any failure: no badge.
   */
  public async refreshEngineUpdate(): Promise<void> {
    const before = JSON.stringify(this.engineUpdate);
    try {
      const root = this.runtimeRoot();
      if (!fs.existsSync(path.join(root, 'dlne_server.py'))) {
        this.engineUpdate = null;
      } else {
        const config = vscode.workspace.getConfiguration('dlne');
        const installed = this.installedRelease(root);
        const check = await checkEngineUpdate(resolveServerUrl(config), installed, {
          publicKeyHex: this.options.releasePublicKeyHex ?? RELEASE_PUBKEY_HEX,
          allowUnsigned: config.get<boolean>('worker.allowUnsignedEngine', false),
          fetchJson: getJson,
        });
        this.engineUpdate = check.newer && check.available ? { installed: installed?.version ?? null, available: check.available } : null;
      }
    } catch {
      // offline, or a release that does not verify: the installed engine is what it is; nothing to show
    }
    if (JSON.stringify(this.engineUpdate) !== before && !this.disposed) this.emitChange();
  }

  public getStatus(): WorkerStatus {
    const cpuPool = this.contributesCpu();
    const running = this.currentState === 'contributing' || this.currentState === 'starting';
    const status: WorkerStatus = {
      state: this.currentState,
      gpu: this.localGpu,
      vramContributedGb: this.vramContributed,
      cpuPool,
      message: this.statusMessage
    };
    if (running && this.externalWorker) {
      // A worker started outside VS Code: nothing about it is read from the settings here (no CPU pool, no job).
      status.external = true;
      status.cpuPool = false;
      if (this.trayWorker) status.trayApp = true;
      return status;
    }
    if (running && this.startedWith) {
      status.startedWith = this.startedWith;
    }
    // Only a worker that is running has a job, and a CPU machine's is always the phone app.
    if (running && cpuPool) {
      status.role = 'app';
    } else if (running && this.assignedRole) {
      status.role = this.assignedRole.role;
      const { role: _role, ...detail } = this.assignedRole;
      if (Object.keys(detail).length > 0) status.roleDetail = detail;
    }
    if (running && !cpuPool && this.alsoCpuRunning) {
      status.alsoCpu = true;
    }
    if (running && this.dock) status.dock = this.dock;
    if (running && this.yielding) status.yielding = this.yielding;
    if (this.engineUpdate) status.engineUpdate = this.engineUpdate;
    return status;
  }

  /** What the running worker last said its job is (a DLNE_ROLE line); cleared when the worker stops. */
  private assignedRole: RoleAssignment | null = null;
  /** The running worker's last DLNE_ALSO line says the CPU serves the phone pool too; cleared when it stops. */
  private alsoCpuRunning = false;
  /** The worker's last DLNE_DOCK and DLNE_YIELD lines (2.5.0); cleared with the job when the worker stops. */
  private dock: DockState | null = null;
  private yielding: YieldState | null = null;
  /** A newer signed engine the coordinator publishes (refreshEngineUpdate), null when there is none or nothing is installed. */
  private engineUpdate: { installed: string | null; available: string } | null = null;
  private updateCheckInterval: NodeJS.Timeout | null = null;
  private codingNoticeShown = false;
  /** The settings the running worker was started with (null when none is running, or it is not this editor's). */
  private startedWith: DonateSettings | null = null;
  /** The clock offsets the owner has been told about during this worker run, so each is shown once. */
  private clockOffsetsShown = new Set<number>();
  /** The missing-Visual-C++-Runtime notice was shown for this run of the worker. */
  private vcredistShown = false;

  /** The unfinished last line of each stream of the running worker (a pipe may cut a line in two chunks). */
  private outputTail: { stdout: string; stderr: string } = { stdout: '', stderr: '' };

  /**
   * Read what the worker prints: the coordinator's refusal of this machine, and the job it was given.
   * Given the stream it came from, only whole lines are read (a line that arrives in two pieces is read
   * when it is complete; Windows ends lines with CRLF, which the parsers accept); without one, `text` is
   * taken as it is.
   */
  private noteWorkerOutput(text: string, stream?: 'stdout' | 'stderr'): void {
    if (stream) {
      const { complete, rest } = takeCompleteLines(this.outputTail[stream], text);
      this.outputTail[stream] = rest;
      text = complete;
      if (!text) {
        return;
      }
    }
    this.lastRefusal = parseRefusal(text) ?? this.lastRefusal;
    const assignment = parseRole(text);
    if (assignment) {
      this.applyRole(assignment);
    }
    const also = parseAlso(text);
    if (also !== null && also !== this.alsoCpuRunning) {
      this.alsoCpuRunning = also;
      this.emitChange();
    }
    const dock = parseDock(text);
    if (dock && JSON.stringify(dock) !== JSON.stringify(this.dock)) {
      this.dock = dock;
      this.emitChange();
    }
    const yielding = parseYield(text);
    if (yielding && JSON.stringify(yielding) !== JSON.stringify(this.yielding)) {
      this.yielding = yielding;
      this.emitChange();
    }
    // A Windows PC without the Visual C++ Runtime cannot start the engine: say so once, with the installer.
    if (parseVcredistNeeded(text) && !this.vcredistShown) {
      this.vcredistShown = true;
      this.currentState = 'error';
      this.statusMessage = 'Install the Visual C++ Runtime to contribute';
      this.emitChange();
      void vscode.window.showErrorMessage(VCREDIST_MESSAGE, 'Download').then((choice) => {
        if (choice === 'Download') void vscode.env.openExternal(vscode.Uri.parse(VCREDIST_URL));
      });
    }
    // A wrong clock is worth one warning per offset: the worker corrects for it and prints a line only when it changes.
    for (const line of text.split(/\r?\n/)) {
      const offset = parseClock(line);
      const message = offset === null ? null : clockMessage(offset);
      if (offset !== null && message && !this.clockOffsetsShown.has(offset)) {
        this.clockOffsetsShown.add(offset);
        void vscode.window.showWarningMessage(message);
      }
    }
  }

  private applyRole(assignment: RoleAssignment): void {
    const before = JSON.stringify(this.assignedRole);
    this.assignedRole = assignment;
    if (JSON.stringify(assignment) === before) {
      return;
    }
    this.emitChange();
    if (assignment.role === 'coding') {
      this.tellCodingMember();
    }
  }

  /** Once per install: the owner is told their GPU now answers other people's requests, and what protects them. */
  private tellCodingMember(): void {
    const state = this.context.globalState;
    if (state ? state.get<boolean>(CODING_NOTICE_KEY, false) : this.codingNoticeShown) {
      return;
    }
    this.codingNoticeShown = true;
    void Promise.resolve(state?.update(CODING_NOTICE_KEY, true)).catch(() => {});
    void vscode.window.showInformationMessage(CODING_MEMBER_NOTICE, 'Stop contributing').then((choice) => {
      if (choice === 'Stop contributing') {
        void this.stopWorker();
      }
    });
  }

  /**
   * The first time a GPU owner clicks to contribute: what the swarm may do with the GPU, and a choice
   * between lending it and lending only the CPU. Resolves false when the owner closes the dialog (nothing
   * starts). Only asked for a GPU machine, from a click (never when a worker resumes by itself), and only once.
   */
  private async confirmLendingGpu(fromPanel: boolean = false): Promise<boolean> {
    const state = this.context.globalState;
    if (state?.get<boolean>(LEND_NOTICE_KEY, false)) {
      return true;
    }
    const config = vscode.workspace.getConfiguration('dlne');
    if (config.get<string>('worker.device') === 'cpu') {
      return true; // the owner already keeps the GPU out of it
    }
    const gpu = this.localGpu.name === 'Probing GPU...' ? await this.detectLocalGpu() : this.localGpu;
    if (!gpu.hasGpu) {
      return true; // a CPU machine has its own dialog (the RAM offer)
    }
    const { message, detail, buttons } = lendGpuPrompt(gpu.name);
    const choice = await vscode.window.showWarningMessage(message, { modal: true, detail }, ...buttons);
    if (choice !== LEND_GPU && choice !== LEND_CPU_ONLY && choice !== LEND_BOTH) {
      return false;
    }
    if (choice === LEND_CPU_ONLY) {
      await config.update('worker.device', 'cpu', vscode.ConfigurationTarget.Global);
    } else if (choice === LEND_BOTH) {
      await config.update('worker.device', 'both', vscode.ConfigurationTarget.Global);
    } else if (!fromPanel && config.get<string>('worker.device') === 'both') {
      // "Lend my GPU": the GPU alone. (From the Donate panel the owner already chose what to lend with its switches.)
      await config.update('worker.device', 'gpu', vscode.ConfigurationTarget.Global);
    }
    try {
      await state?.update(LEND_NOTICE_KEY, true);
    } catch {
      // asked again next time; never fatal
    }
    return true;
  }

  /**
   * Whether this machine's contribution is its CPU. A running worker answers for itself; before
   * one starts (or after a window reload, with no plan to hand), it follows dlne.worker.device
   * and the GPU found, once the probe has finished.
   */
  private contributesCpu(): boolean {
    if (this.contributionPlan && (this.currentState === 'contributing' || this.currentState === 'starting')) {
      return this.contributionPlan.cpuOnly;
    }
    if (this.localGpu.name === 'Probing GPU...') {
      return false;
    }
    const device = vscode.workspace.getConfiguration('dlne').get<string>('worker.device');
    return planContribution(device, this.localGpu.hasGpu, '', CPU_MIN_RAM_GB, this.localGpu.integratedGpu).cpuOnly;
  }

  public isContributing(): boolean {
    return this.currentState === 'contributing';
  }

  private isPortListening(port: number, host: string = '127.0.0.1', timeoutMs: number = 800): Promise<boolean> {
    return new Promise((resolve) => {
      const socket = new net.Socket();
      let resolved = false;
      socket.setTimeout(timeoutMs);

      socket.once('connect', () => {
        resolved = true;
        socket.destroy();
        resolve(true);
      });

      socket.once('timeout', () => {
        if (!resolved) {
          resolved = true;
          socket.destroy();
          resolve(false);
        }
      });

      socket.once('error', () => {
        if (!resolved) {
          resolved = true;
          socket.destroy();
          resolve(false);
        }
      });

      socket.connect(port, host);
    });
  }

  private isSystemdActive(): boolean {
    if (process.platform !== 'linux' || this.systemdUnavailable) return false;
    try {
      const out = child_process.execSync('systemctl --user is-active dlne-worker', {
        encoding: 'utf-8',
        timeout: 1500,
        // Its complaints ("Failed to connect to user scope bus") went to the extension's log every 5 s.
        stdio: ['ignore', 'pipe', 'ignore']
      }).trim();
      return out === 'active';
    } catch (err: any) {
      // `is-active` exits non-zero for every state but active and still names the state ("inactive", "failed").
      // Nothing named, and not a timeout (a busy machine), means systemd itself could not be asked.
      const said = String(err?.stdout ?? '').trim();
      if (!said && !err?.signal) {
        this.systemdUnavailable = true;
      }
      return false;
    }
  }

  private isProcessActive(): boolean {
    try {
      if (process.platform === 'win32') {
        // windowsHide: the editor has no console, so a console program (this runs every 5 s) would flash a window.
        const out = child_process.execSync('tasklist', { encoding: 'utf-8', timeout: 2000, windowsHide: true });
        return out.includes('ggml-rpc-server') || out.includes('dlne');
      } else {
        const out = child_process.execSync('pgrep -f "ggml-rpc-server|dlne_server.py worker"', {
          encoding: 'utf-8',
          timeout: 1500
        }).trim();
        return out.length > 0;
      }
    } catch {
      return false;
    }
  }

  /**
   * The DLNE Donate tray app's worker is running (or the app is about to start it). The patterns above miss it: its
   * command line is the app's own (`DLNE-Donate --run-worker ...`), not `dlne_server.py worker`, and as a coding member
   * or a phone-app member it runs llama-server, not ggml-rpc-server, so nothing listens on 50052 either.
   */
  private isTrayWorkerActive(): boolean {
    return trayWorkerRunning();
  }

  private ownWorkerFromBefore(): number | null {
    const pid = this.context?.globalState?.get<number>(OWN_WORKER_KEY);
    return typeof pid === 'number' && pid > 0 && isDlneWorkerProcess(pid) ? pid : null;
  }

  public async refreshWorkerState(): Promise<WorkerStatus> {
    // A worker being stopped (a leaving one takes up to 90 s) is still our own, not one "started outside VS Code".
    if (this.stopping) {
      return this.getStatus();
    }
    // This editor's own worker, starting or running: what it says is the status. (Requiring 'contributing' here let a
    // poll during its first seconds find its process and call it "started outside VS Code", for good.)
    if (this.workerProcess && !this.workerProcess.killed) {
      return this.getStatus();
    }
    if (this.adoptedPid !== null && !processAlive(this.adoptedPid)) {
      this.adoptedPid = null;
      this.currentState = 'idle';
      this.adoptedGoneAt = Date.now();
    }
    if (this.adoptedPid === null) this.adoptedPid = this.ownWorkerFromBefore();
    if (this.adoptedPid !== null) {
      this.adoptedGoneAt = null;
      if (this.currentState !== 'contributing' || this.externalWorker) {
        this.externalWorker = false;
        this.trayWorker = false;
        this.markContributing();
      }
      return this.getStatus();
    }
    if (this.adoptedGoneAt !== null) {
      // The window that started it may be starting the next one: wait, then start one here if none appeared.
      if (Date.now() - this.adoptedGoneAt < ADOPTED_RESTART_GRACE_MS) return this.getStatus();
      this.adoptedGoneAt = null;
      if (vscode.workspace.getConfiguration('dlne').get<boolean>('worker.autoStart', false) && !this.isProcessActive()) {
        void this.startWorker();
        return this.getStatus();
      }
    }

    const sysdActive = this.isSystemdActive();
    const processActive = this.isProcessActive();
    const portActive = await this.isPortListening(50052);
    const trayActive = this.isTrayWorkerActive();

    const isRunning = sysdActive || processActive || portActive || trayActive;

    if (isRunning) {
      this.failedChecksCount = 0;
      // Not this editor's own worker (that case returned above): one started outside VS Code. It is described again
      // when it turns out to be (or no longer to be) the tray app's.
      if (this.currentState !== 'contributing' || !this.externalWorker || this.trayWorker !== trayActive) {
        this.externalWorker = true;
        this.trayWorker = trayActive;
        this.markContributing();
      }
    } else {
      this.failedChecksCount++;
      // Debounce: require 3 consecutive failed checks before switching to idle
      if (this.failedChecksCount >= 3) {
        if (this.currentState === 'contributing') {
          this.currentState = 'idle';
          this.externalWorker = false;
          this.trayWorker = false;
          this.vramContributed = 0;
          this.statusMessage = 'Worker is idle.';
          this.emitChange();
        }
      }
    }

    return this.getStatus();
  }

  public async detectLocalGpu(): Promise<LocalGpuInfo> {
    const isWin = process.platform === 'win32';
    const isMac = process.platform === 'darwin';

    // 1. NVIDIA discrete GPU check via nvidia-smi first (avoids iGPU shadowing dGPU on hybrid Ryzen/Intel + RTX systems)
    try {
      const smiCmd = isWin
        ? 'nvidia-smi.exe --query-gpu=name,memory.total --format=csv,noheader,nounits'
        : 'nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits';
      const out = child_process.execSync(smiCmd, { encoding: 'utf-8', timeout: 2500, windowsHide: true });
      const lines = out.trim().split('\n');
      if (lines.length > 0 && lines[0].includes(',')) {
        const [name, mem] = lines[0].split(',').map((s) => s.trim());
        const totalGb = Math.round((parseInt(mem, 10) / 1024) * 10) / 10;
        this.localGpu = { hasGpu: true, name, vramTotalGb: totalGb > 0 ? totalGb : 8.0, api: 'vulkan' };
        this.emitChange();
        return this.localGpu;
      }
    } catch {}

    // 2. Check Vulkan API (Universal - prefers DISCRETE_GPU; an integrated GPU counts only if
    // contribution.ts lists it, otherwise the machine contributes its CPU and says why)
    let integratedGpu: string | undefined;
    try {
      const vkCmd = isWin ? 'vulkaninfo.exe --summary' : 'vulkaninfo --summary';
      const out = child_process.execSync(vkCmd, { encoding: 'utf-8', timeout: 3000, windowsHide: true });
      const picked = pickVulkanGpu(parseVulkanSummary(out));
      integratedGpu = picked.integratedGpu;

      if (picked.gpu) {
        const preferred = picked.gpu;
        let vramGb = 8.0;
        if (isWin) {
          try {
            const psCmd =
              'powershell -NoProfile -Command "Get-ChildItem \'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Class\\{4d36e968-e325-11ce-bfc1-08002be10318}\' -ErrorAction SilentlyContinue | Get-ItemProperty -Name \'HardwareInformation.qwMemorySize\' -ErrorAction SilentlyContinue | Select-Object -ExpandProperty \'HardwareInformation.qwMemorySize\'"';
            const ramBytes = child_process.execSync(psCmd, { encoding: 'utf-8', timeout: 3000, windowsHide: true }).trim();
            let maxBytes = 0;
            for (const line of ramBytes.split('\n')) {
              const val = parseInt(line.trim(), 10);
              if (!isNaN(val) && val > maxBytes) {
                maxBytes = val;
              }
            }
            if (maxBytes > 0) {
              vramGb = Math.round((maxBytes / 1024 ** 3) * 10) / 10;
            }
          } catch {}
        } else {
          try {
            const drmDir = '/sys/class/drm';
            if (fs.existsSync(drmDir)) {
              let maxBytes = 0;
              for (const entry of fs.readdirSync(drmDir)) {
                const memPath = path.join(drmDir, entry, 'device', 'mem_info_vram_total');
                if (fs.existsSync(memPath)) {
                  const b = parseInt(fs.readFileSync(memPath, 'utf-8').trim(), 10);
                  if (!isNaN(b) && b > maxBytes) {
                    maxBytes = b;
                  }
                }
              }
              if (maxBytes > 0) {
                vramGb = Math.round((maxBytes / 1024 ** 3) * 10) / 10;
              }
            }
          } catch {}
        }

        this.localGpu = {
          hasGpu: true,
          name: preferred.name,
          vramTotalGb: vramGb > 0 ? vramGb : 8.0,
          api: 'vulkan'
        };
        this.emitChange();
        return this.localGpu;
      }
    } catch {}

    // 3. Apple Metal check: Apple Silicon only. llama.cpp's Intel Mac build has no Metal
    // backend, so an Intel Mac contributes its CPU whatever GPU it has.
    if (isMac && process.arch === 'arm64') {
      try {
        const out = child_process.execSync('system_profiler SPDisplaysDataType', { encoding: 'utf-8', timeout: 3000 });
        if (out.includes('Apple') || out.includes('Metal')) {
          this.localGpu = { hasGpu: true, name: 'Apple Silicon (Metal)', vramTotalGb: 16.0, api: 'metal' };
          this.emitChange();
          return this.localGpu;
        }
      } catch {}
    }

    // 4. Host CPU RAM fallback
    this.localGpu = { hasGpu: false, name: 'CPU Compute Node', vramTotalGb: 4.0, api: 'cpu', integratedGpu };
    this.emitChange();
    return this.localGpu;
  }

  private resolvePythonPath(configuredPath: string, rootWorkspace?: string): string {
    if (configuredPath && configuredPath !== 'python3' && configuredPath !== 'python') {
      if (fs.existsSync(configuredPath)) return configuredPath;
    }

    const homeDir = process.env.HOME || process.env.USERPROFILE || '';
    const localAppData = process.env.LOCALAPPDATA || '';
    const candidates = [
      localAppData ? path.join(localAppData, 'DLNE', 'runtime', '.venv', 'Scripts', 'python.exe') : '',
      localAppData ? path.join(localAppData, 'DLNE', 'runtime', 'DistributedLLMEngine-main', '.venv', 'Scripts', 'python.exe') : '',
      path.join(homeDir, '.dlne', 'runtime', '.venv', 'bin', 'python'),
      path.join(homeDir, '.dlne', 'runtime', '.venv', 'Scripts', 'python.exe'),
      path.join(homeDir, '.dlne', 'runtime', 'DistributedLLMEngine-main', '.venv', 'bin', 'python'),
      path.join(homeDir, '.venvs', 'dlne', 'bin', 'python'),
      path.join(homeDir, '.venvs', 'dlne', 'Scripts', 'python.exe'),
      // Never the open workspace's .venv: any cloned repository could ship one.
      path.join(localAppData, 'Programs', 'Python', 'Python311', 'python.exe'),
      path.join(localAppData, 'Programs', 'Python', 'Python312', 'python.exe'),
      path.join(localAppData, 'Programs', 'Python', 'Python310', 'python.exe'),
      'C:\\Python311\\python.exe',
      'C:\\Python312\\python.exe'
    ];

    for (const cand of candidates) {
      if (cand && fs.existsSync(cand)) {
        return cand;
      }
    }

    return process.platform === 'win32' ? 'python' : 'python3';
  }

  private resolveNetworkKey(configuredKey: string): string {
    if (configuredKey) return configuredKey;

    // Only the installed engine's config: never the open workspace's, which any cloned repository could ship.
    const homeDir = process.env.HOME || process.env.USERPROFILE || '';
    const localAppData = process.env.LOCALAPPDATA || '';
    const configPaths = [
      localAppData ? path.join(localAppData, 'DLNE', 'runtime', 'config', 'config.txt') : '',
      localAppData ? path.join(localAppData, 'DLNE', 'runtime', 'DistributedLLMEngine-main', 'config', 'config.txt') : '',
      path.join(homeDir, '.dlne', 'runtime', 'config', 'config.txt'),
      path.join(homeDir, '.dlne', 'runtime', 'DistributedLLMEngine-main', 'config', 'config.txt'),
    ];

    for (const cp of configPaths) {
      if (cp && fs.existsSync(cp)) {
        try {
          const content = fs.readFileSync(cp, 'utf-8');
          for (const line of content.split('\n')) {
            const trimmed = line.trim();
            if (trimmed.startsWith('NETWORK_KEY=')) {
              return trimmed.split('=')[1].trim();
            }
          }
        } catch {}
      }
    }
    // Default to the official public cluster network key
    return 'bd1d9bbf50b93e10a963fa2d0b6ee44e1138ca20f4036b765dbc51dd7bc7f083';
  }

  /**
   * The installed engine's dlne_server.py, or '' if there is none. Only places the user or this
   * extension put an engine: never the open workspace, which could be any cloned repository, and
   * never the retired standalone DLNE-Node executables, which nothing verifies.
   */
  private findEngineScript(): string {
    const homeDir = process.env.HOME || process.env.USERPROFILE || '';
    const localAppData = process.env.LOCALAPPDATA || '';
    const candidates = [
      localAppData ? path.join(localAppData, 'DLNE', 'runtime', 'dlne_server.py') : '',
      path.join(homeDir, '.dlne', 'runtime', 'dlne_server.py'),
      path.join(homeDir, 'DistributedLLMEngine', 'dlne_server.py'),
      'C:\\Program Files\\DLNE\\dlne_server.py'
    ];
    return candidates.find((c) => c && fs.existsSync(c)) || '';
  }

  public async ensureWorkerRuntime(interactive: boolean = true): Promise<{ scriptPath: string; pythonPath: string } | null> {
    const homeDir = process.env.HOME || process.env.USERPROFILE || '';
    const localAppData = process.env.LOCALAPPDATA || '';
    const rootWorkspace = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
    const scriptPath = this.findEngineScript();

    const config = vscode.workspace.getConfiguration('dlne');
    const serverUrl = resolveServerUrl(config);
    const rawPython = config.get<string>('worker.pythonPath') || '';
    let pythonPath = this.resolvePythonPath(rawPython, rootWorkspace);

    if (scriptPath && pythonPath && fs.existsSync(pythonPath)) {
      await this.ensureRpcBinaries(path.dirname(scriptPath), serverUrl);
      return { scriptPath, pythonPath };
    }

    if (interactive) {
      const choice = await vscode.window.showInformationMessage(
        '⚡ DLNE Compute Engine is required to share your computer. A GPU powers the coding agent; a computer without one powers the DLNE phone app. Would you like to install it automatically now?',
        'Install Automatically',
        'Not Now'
      );
      if (choice !== 'Install Automatically') {
        return null;
      }
    }

    return await vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title: 'DLNE Compute Engine Setup',
        cancellable: false
      },
      async (progress) => {
        const isWin = process.platform === 'win32';
        const baseRuntimeDir = isWin && localAppData
          ? path.join(localAppData, 'DLNE', 'runtime')
          : path.join(homeDir, '.dlne', 'runtime');

        fs.mkdirSync(baseRuntimeDir, { recursive: true });

        const cleanServer = serverUrl.replace(/\/+$/, '');

        // 1. Fetch Engine Codebase
        progress.report({ message: '1/3 Downloading DLNE compute engine...', increment: 30 });
        const engineRoot = baseRuntimeDir;
        const mainScript = path.join(engineRoot, 'dlne_server.py');

        if (!fs.existsSync(mainScript)) {
          const zipPath = path.join(baseRuntimeDir, 'dlne_bundle.zip');

          // The engine comes from one place only: the coordinator's signed source release.
          // There is deliberately no fallback to an unverified bundle - one would let a failed
          // (or sabotaged) verified download quietly hand the user unverified code instead.
          let fetched: FetchResult;
          try {
            fetched = await this.fetchEngineArchive(cleanServer, zipPath, baseRuntimeDir);
          } catch (e: any) {
            try { fs.unlinkSync(zipPath); } catch {}
            if (e instanceof EngineVerificationError) {
              this.outputChannel.appendLine(`[DLNE] Engine verification failed: ${e.userMessage}`);
              this.reportSetupFailure(e.userMessage, '');
            } else {
              this.outputChannel.appendLine(`[DLNE] Engine download failed: ${e?.message ?? e}`);
              vscode.window.showErrorMessage(`Failed to download DLNE engine: ${e?.message ?? e}`);
            }
            return null;
          }

          progress.report({ message: '2/3 Extracting engine archive...', increment: 30 });
          const extracted = await this.extractArchive(zipPath, baseRuntimeDir);
          try { fs.unlinkSync(zipPath); } catch {}
          if (!extracted.ok) {
            this.reportSetupFailure('Could not unpack the downloaded engine archive.', extracted.tail);
            return null;
          }
          this.recordInstalledRelease(baseRuntimeDir, fetched);
        }

        if (!fs.existsSync(mainScript)) {
          vscode.window.showErrorMessage('DLNE engine installation failed: dlne_server.py could not be located.');
          return null;
        }

        // The GPU engine (ggml-rpc-server) for this platform, from the signed release.
        progress.report({ message: '2/4 Downloading the GPU engine (~20 MB)...', increment: 15 });
        await this.ensureRpcBinaries(engineRoot, cleanServer);

        // 2. Discover or Install Python
        progress.report({ message: '2/4 Configuring Python environment...', increment: 20 });
        let sysPython = '';
        const pythonCandidates = isWin
          // A path is quoted: "C:\Users\Jane Doe\AppData\..." would otherwise run "C:\Users\Jane".
          ? ['py -3', 'python', 'python3', quoteIfSpaced(path.join(localAppData, 'Programs', 'Python', 'Python311', 'python.exe'))]
          : ['python3', 'python'];

        for (const cand of pythonCandidates) {
          try {
            const ver = child_process.execSync(`${cand} -c "import sys; print('%d.%d' % sys.version_info[:2])"`, {
              encoding: 'utf-8',
              timeout: 3000,
              windowsHide: true
            }).trim();
            const [major, minor] = ver.split('.').map(Number);
            if (major === 3 && minor >= 10) {
              sysPython = cand;
              break;
            }
            this.outputChannel.appendLine(`[DLNE] Skipping '${cand}': Python ${ver} is older than 3.10.`);
          } catch {}
        }

        if (!sysPython && isWin) {
          progress.report({ message: '2/4 Installing official Python 3.11 via winget...', increment: 10 });
          try {
            child_process.execSync(
              'winget install --id Python.Python.3.11 -e --silent --accept-package-agreements --accept-source-agreements',
              { timeout: 120000, stdio: 'ignore', windowsHide: true }
            );
            sysPython = 'python';
          } catch {}
        }

        if (!sysPython) {
          vscode.window.showErrorMessage(
            'Python 3.10+ is required by DLNE Compute Worker. Please install Python from https://www.python.org/downloads/ and retry.'
          );
          return null;
        }

        // 3. Create Virtualenv and Install Dependencies
        progress.report({ message: '3/4 Installing Python dependencies...', increment: 20 });
        const venvDir = path.join(engineRoot, '.venv');
        const venvPython = isWin
          ? path.join(venvDir, 'Scripts', 'python.exe')
          : path.join(venvDir, 'bin', 'python');

        if (!fs.existsSync(venvPython)) {
          const venv = await this.runSetupStep(`${sysPython} -m venv "${venvDir}"`, 'Create virtual environment', 120000);
          if (!venv.ok) {
            // Without a venv we would pip-install into the system Python, which modern Linux
            // distros refuse (externally-managed-environment). Stop with a usable message.
            const hint = /ensurepip|python3-venv|No module named venv/i.test(venv.tail)
              ? ' On Debian/Ubuntu run: sudo apt install python3-venv'
              : '';
            this.reportSetupFailure(`Could not create the Python virtual environment.${hint}`, venv.tail);
            return null;
          }
        }

        const activePy = venvPython;

        const reqPath = path.join(engineRoot, 'requirements.txt');
        const pipCommand = fs.existsSync(reqPath)
          ? `"${activePy}" -m pip install -r "${reqPath}"`
          : `"${activePy}" -m pip install httpx fastapi uvicorn requests numpy pydantic websockets psutil`;
        progress.report({ message: '3/4 Installing Python dependencies (can take a few minutes)...' });
        const pip = await this.runSetupStep(pipCommand, 'Install Python dependencies', 600000);
        if (!pip.ok) {
          this.reportSetupFailure('Installing the Python dependencies failed. Check your internet connection and retry.', pip.tail);
          return null;
        }

        // Prove the install works before telling the user it did.
        const verify = await this.runSetupStep(`"${activePy}" -c "import fastapi, httpx, uvicorn"`, 'Verify dependencies', 30000);
        if (!verify.ok) {
          this.reportSetupFailure('The Python dependencies installed but could not be imported.', verify.tail);
          return null;
        }

        // 4. Configure Swarm Configuration File
        progress.report({ message: '4/4 Finalizing configuration...', increment: 10 });
        const configDir = path.join(engineRoot, 'config');
        const configFile = path.join(configDir, 'config.txt');
        try {
          fs.mkdirSync(configDir, { recursive: true });
          if (!fs.existsSync(configFile)) {
            const defaultCfg = [
              `COORDINATOR_URL=${cleanServer}`,
              'NETWORK_KEY=bd1d9bbf50b93e10a963fa2d0b6ee44e1138ca20f4036b765dbc51dd7bc7f083',
              'MAX_RAM_GB=8.0',
              ''
            ].join('\n');
            fs.writeFileSync(configFile, defaultCfg, 'utf-8');
          }
        } catch {}

        try {
          await config.update('worker.pythonPath', activePy, vscode.ConfigurationTarget.Global);
        } catch {}

        vscode.window.showInformationMessage('🎉 DLNE Compute Engine installed successfully!');
        return { scriptPath: mainScript, pythonPath: activePy };
      }
    );
  }

  /**
   * Runs one setup command, streaming its output into the "DLNE Compute Worker" channel.
   * `tail` keeps the last few lines so a failure can show the user the actual error instead
   * of a silent "installed successfully".
   */
  private runSetupStep(command: string, label: string, timeoutMs: number): Promise<{ ok: boolean; tail: string }> {
    return new Promise((resolve) => {
      this.outputChannel.appendLine(`[DLNE Setup] ${label}: ${command}`);
      const lines: string[] = [];
      const child = child_process.spawn(command, { shell: true, windowsHide: true });
      let timedOut = false;
      const timer = setTimeout(() => {
        timedOut = true;
        child.kill();
      }, timeoutMs);

      const onData = (chunk: Buffer) => {
        const text = chunk.toString();
        this.outputChannel.append(text);
        lines.push(...text.split(/\r?\n/).filter((l) => l.trim()));
        if (lines.length > 15) lines.splice(0, lines.length - 15);
      };
      child.stdout?.on('data', onData);
      child.stderr?.on('data', onData);

      child.on('error', (err) => {
        clearTimeout(timer);
        resolve({ ok: false, tail: err.message });
      });
      child.on('close', (code) => {
        clearTimeout(timer);
        const tail = timedOut ? `Timed out after ${Math.round(timeoutMs / 1000)}s.\n${lines.join('\n')}` : lines.join('\n');
        this.outputChannel.appendLine(`[DLNE Setup] ${label}: ${code === 0 ? 'ok' : `failed (exit ${code})`}`);
        resolve({ ok: code === 0 && !timedOut, tail });
      });
    });
  }

  /**
   * Unpacks a zip with whatever the machine has. `unzip` is not installed on many minimal Linux
   * systems, and GNU tar cannot read zips, so Python's own zipfile module (Python is a hard
   * requirement of the engine anyway) and bsdtar-style `tar -xf` are tried next.
   */
  private async extractArchive(zipPath: string, destDir: string): Promise<{ ok: boolean; tail: string }> {
    const attempts = process.platform === 'win32'
      ? [
          powershellExpandCommand(zipPath, destDir),
          `tar -xf "${zipPath}" -C "${destDir}"`
        ]
      : [
          `unzip -q -o "${zipPath}" -d "${destDir}"`,
          `python3 -m zipfile -e "${zipPath}" "${destDir}"`,
          `python -m zipfile -e "${zipPath}" "${destDir}"`,
          `tar -xf "${zipPath}" -C "${destDir}"`
        ];
    let last = { ok: false, tail: 'No archive extractor is available on this machine.' };
    for (const command of attempts) {
      last = await this.runSetupStep(command, 'Extract engine archive', 120000);
      if (last.ok) return last;
    }
    return last;
  }

  private reportSetupFailure(message: string, tail: string) {
    const lastLine = tail.split('\n').filter((l) => l.trim()).pop() || '';
    vscode.window
      .showErrorMessage(`DLNE setup failed: ${message}${lastLine ? ` (${lastLine.slice(0, 160)})` : ''}`, 'Show Log')
      .then((choice) => {
        if (choice === 'Show Log') this.outputChannel.show(true);
      });
  }

  /** Where the installed engine records which signed release it came from. */
  private releaseRecordPath(engineRoot: string): string {
    return path.join(engineRoot, '.dlne_release.json');
  }

  private installedRelease(engineRoot: string): { version: string; sha256: string } | null {
    try {
      const record = JSON.parse(fs.readFileSync(this.releaseRecordPath(engineRoot), 'utf-8'));
      if (typeof record.version === 'string' && typeof record.sha256 === 'string') {
        return { version: record.version, sha256: record.sha256 };
      }
    } catch {
      // no record: the engine predates signed releases, or was installed unsigned
    }
    return null;
  }

  private recordInstalledRelease(engineRoot: string, fetched: FetchResult): void {
    const file = this.releaseRecordPath(engineRoot);
    try {
      if (fetched.manifest) {
        fs.writeFileSync(
          file,
          JSON.stringify({ version: fetched.manifest.version, sha256: fetched.manifest.sha256, installed_at: new Date().toISOString() }),
          'utf-8'
        );
      } else {
        fs.unlinkSync(file); // an unsigned install has no release to remember
      }
    } catch {
      // the record is a convenience (skip re-downloading, block downgrades); never fatal
    }
  }

  /** Download the coordinator's engine archive to `destination`, but only if it is a verified release. */
  private fetchEngineArchive(serverUrl: string, destination: string, engineRoot: string): Promise<FetchResult> {
    const config = vscode.workspace.getConfiguration('dlne');
    return fetchVerifiedEngine(serverUrl, destination, {
      publicKeyHex: this.options.releasePublicKeyHex ?? RELEASE_PUBKEY_HEX,
      allowUnsigned: config.get<boolean>('worker.allowUnsignedEngine', false),
      installed: this.installedRelease(engineRoot),
      fetchJson: getJson,
      download: downloadFile,
    });
  }

  /**
   * Bring the installed engine up to the coordinator's current signed release. A failure of any
   * kind keeps the engine that is already installed: an update that cannot be verified is never
   * applied, and being offline must not stop a worker that already has working code.
   */
  private async syncEngineSource(engineRoot: string, serverUrl: string): Promise<void> {
    const cleanServer = serverUrl.replace(/\/+$/, '');
    const srcZip = path.join(engineRoot, 'dlne_source_update.zip');
    // Up to 2.5.0 every release's files were dated 1980-01-01, and Python trusts a cached .pyc while the
    // source's mtime and size match, so a module that kept its size kept the old release's code. Drop the
    // bytecode on every start: the code on disk is the code that runs.
    try { fs.rmSync(path.join(engineRoot, 'engine', '__pycache__'), { recursive: true, force: true }); } catch {}
    try {
      const fetched = await this.fetchEngineArchive(cleanServer, srcZip, engineRoot);
      if (fetched.status === 'up-to-date') return;
      const extracted = await this.extractArchive(srcZip, engineRoot);
      if (!extracted.ok) {
        this.outputChannel.appendLine(`[DLNE] Could not unpack the engine update: ${extracted.tail}`);
        return;
      }
      this.recordInstalledRelease(engineRoot, fetched);
      this.engineUpdate = null;                 // what was published is what is installed now
      this.outputChannel.appendLine(`[DLNE] Engine updated to ${fetched.manifest ? fetched.manifest.version : 'the coordinator\'s unsigned build'}.`);
    } catch (e: any) {
      if (e instanceof EngineVerificationError) {
        this.outputChannel.appendLine(`[DLNE] Engine update refused: ${e.userMessage}`);
        vscode.window.showWarningMessage(`DLNE: ${e.userMessage} Your existing engine was kept.`);
      } else {
        this.outputChannel.appendLine(`[DLNE] Engine update skipped (${e?.message ?? e}); using the installed engine.`);
      }
    } finally {
      try { if (fs.existsSync(srcZip)) fs.unlinkSync(srcZip); } catch {}
    }
  }

  /** Where the engine records which signed RPC zip its bin/ came from (the Python worker shares it). */
  private rpcBinRecordPath(engineRoot: string): string {
    return path.join(engineRoot, '.dlne_rpc_bin.json');
  }

  private installedRpcBinSha(engineRoot: string): string | null {
    try {
      const record = JSON.parse(fs.readFileSync(this.rpcBinRecordPath(engineRoot), 'utf-8'));
      return typeof record.sha256 === 'string' ? record.sha256 : null;
    } catch {
      return null; // never installed, or installed before the binaries were signed
    }
  }

  /**
   * Keep bin/ggml-rpc-server (Windows, Linux or macOS) equal to the build the coordinator's
   * signed release names for this platform. It is an executable the worker runs, so it gets
   * the engine's guarantees: the signed manifest names its zip, the bytes must match, and a
   * failure keeps what is installed. This is also how a worker follows a llama.cpp upgrade
   * (the coordinator only routes to workers on its own RPC protocol), and binaries from before
   * signing (no record) are replaced by the signed ones.
   */
  private async ensureRpcBinaries(engineRoot: string, serverUrl: string): Promise<void> {
    const platform = hostRpcPlatform();
    if (!platform) {
      this.outputChannel.appendLine(`[DLNE] DLNE ships no GPU engine for ${process.platform}/${process.arch}; contributing is not available on this machine.`);
      return;
    }
    const exeName = isRpcExecutablePlatform(platform) ? 'ggml-rpc-server.exe' : 'ggml-rpc-server';
    const rpcExe = path.join(engineRoot, 'bin', exeName);
    const present = fs.existsSync(rpcExe);
    const cleanServer = serverUrl.replace(/\/+$/, '');
    const binZip = path.join(engineRoot, 'rpc-bin.zip');
    const config = vscode.workspace.getConfiguration('dlne');

    const install = async () => {
      try {
        const fetched = await fetchVerifiedRpcBin(cleanServer, binZip, platform, {
          publicKeyHex: this.options.releasePublicKeyHex ?? RELEASE_PUBKEY_HEX,
          allowUnsigned: config.get<boolean>('worker.allowUnsignedEngine', false),
          installedSha256: present ? this.installedRpcBinSha(engineRoot) : null,
          fetchJson: getJson,
          download: downloadFile,
        });
        if (fetched.status === 'up-to-date') return;
        const extracted = await this.extractArchive(binZip, engineRoot);
        if (!extracted.ok) {
          // On Windows a running ggml-rpc-server.exe cannot be overwritten; the next start retries.
          this.outputChannel.appendLine(`[DLNE] Could not unpack the GPU engine binaries: ${extracted.tail}`);
          return;
        }
        if (!isRpcExecutablePlatform(platform)) {
          // Not every extractor keeps the executable bit (Python's zipfile does not).
          try { fs.chmodSync(rpcExe, 0o755); } catch {}
        }
        try {
          if (fetched.sha256) {
            fs.writeFileSync(this.rpcBinRecordPath(engineRoot), JSON.stringify({ sha256: fetched.sha256, installed_at: new Date().toISOString() }), 'utf-8');
          } else {
            fs.unlinkSync(this.rpcBinRecordPath(engineRoot));
          }
        } catch {
          // the record only saves a re-download; never fatal
        }
        this.outputChannel.appendLine(`[DLNE] GPU engine (${platform}) installed${fetched.sha256 ? ' (signed release)' : ' (unsigned, allowed by setting)'}.`);
      } catch (e: any) {
        if (e instanceof EngineVerificationError) {
          this.outputChannel.appendLine(`[DLNE] GPU engine binaries refused: ${e.userMessage}`);
          vscode.window.showWarningMessage(`DLNE: ${e.userMessage}${present ? ' Your existing GPU engine was kept.' : ''}`);
        } else {
          this.outputChannel.appendLine(`[DLNE] GPU engine binaries not updated (${e?.message ?? e})${present ? '; using the installed ones.' : '.'}`);
        }
      } finally {
        try { if (fs.existsSync(binZip)) fs.unlinkSync(binZip); } catch {}
      }
    };

    if (present) {
      await install(); // usually a single small manifest request
      return;
    }
    this.outputChannel.appendLine(`[DLNE] Downloading the GPU engine for ${platform}...`);
    await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: 'DLNE: Downloading GPU engine (~20 MB)...', cancellable: false },
      install
    );
  }

  private detectDiscreteVulkanIndices(): string {
    try {
      const vkCmd = process.platform === 'win32' ? 'vulkaninfo.exe --summary' : 'vulkaninfo --summary';
      const out = child_process.execSync(vkCmd, { encoding: 'utf-8', timeout: 3000, windowsHide: true });
      const discrete: string[] = [];
      const integrated: string[] = [];
      let currentIdx = '';
      for (const rawLine of out.split('\n')) {
        const s = rawLine.trim();
        if (s.startsWith('GPU') && s.endsWith(':')) {
          const num = s.slice(3, -1);
          if (/^\d+$/.test(num)) currentIdx = num;
        } else if (s.startsWith('deviceType') && s.includes('=') && currentIdx !== '') {
          const dtype = s.split('=')[1]?.trim() || '';
          if (dtype.includes('DISCRETE_GPU')) discrete.push(currentIdx);
          else if (dtype.includes('INTEGRATED_GPU')) integrated.push(currentIdx);
        }
      }
      if (discrete.length > 0 && integrated.length > 0) {
        return discrete.join(',');
      }
    } catch {}
    return '';
  }

  /**
   * @param options.interactive the owner asked for this (a click or a command), so a GPU owner who has not
   *   yet been told what lending a GPU means is told now, and may keep the GPU out of it. A worker that
   *   resumes by itself, or restarts after a version change, never asks.
   * @param options.fromPanel the settings were just set by the sidebar's Donate panel, so the prompt does not undo them.
   */
  public async startWorker(options: { interactive?: boolean; fromPanel?: boolean } = {}): Promise<boolean> {
    if (options.interactive && !(await this.confirmLendingGpu(options.fromPanel === true))) {
      return false;
    }
    try {
      await vscode.workspace.getConfiguration('dlne').update('worker.autoStart', true, vscode.ConfigurationTarget.Global);
    } catch {}

    // This editor's own worker is already running (started here, or by this editor's window before a reload).
    if ((this.workerProcess && !this.workerProcess.killed) || (this.adoptedPid !== null && processAlive(this.adoptedPid))) {
      this.markContributing();
      return true;
    }
    // A worker started outside VS Code (a systemd service, a terminal, the DLNE Donate tray app) is running: a second
    // one would fight it for the port or donate the same computer twice, and that one does not use the settings here.
    // Say so; Stop Contributing stops it (the tray app's is stopped in the app).
    const portOpen = await this.isPortListening(50052);
    const sysdActive = this.isSystemdActive();
    const processActive = this.isProcessActive();
    const trayActive = this.isTrayWorkerActive();
    if (portOpen || sysdActive || processActive || trayActive) {
      this.externalWorker = true;
      this.trayWorker = trayActive;
      this.markContributing();
      vscode.window.showInformationMessage(`DLNE: ${trayActive ? TRAY_WORKER_TEXT : EXTERNAL_WORKER_TEXT}`);
      return true;
    }

    // What this machine contributes. Without a GPU the coordinator only admits a CPU that
    // meets its floors; the RAM one is the user's setting, so it is settled here, before a
    // worker starts only to be refused.
    const plan = await this.planThisMachine();
    if (!plan) {
      return false;
    }
    this.contributionPlan = plan;
    // What this worker is started with (after the plan, which may have just set a RAM amount): the sidebar compares
    // the settings with it to say when a change needs a restart.
    this.startedWith = readDonateSettings(vscode.workspace.getConfiguration('dlne'));
    // An engine that is already installed reads the owner's coding-member switch from its config.txt; a first-time
    // install gets it written before the spawn.
    this.syncCodingMemberSetting();
    this.syncAlsoCpuSetting(plan);
    this.syncMaxCpuThreadsSetting();

    // The worker is always this editor's own child, started with the settings here. (A dlne-worker systemd service,
    // if the machine has one, is never started from here: it runs with its own fixed arguments, and starting it
    // used to turn "lend my CPU, 16 GB" into whatever that service was set up to lend.)
    this.externalWorker = false;
    this.trayWorker = false;
    const config = vscode.workspace.getConfiguration('dlne');
    const serverUrl: string = resolveServerUrl(config);
    const rootWorkspace = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;

    const rawKey = config.get<string>('worker.networkKey') || '';
    const networkKey = this.resolveNetworkKey(rawKey);

    const allowedGpus: string = plan.allowedGpus;
    const maxVram: number = config.get<number>('worker.maxVramGb') || -1;
    const maxRam: number = config.get<number>('worker.maxRamGb') || 4.0;
    // The coding job is not held to that default (it is the RPC job's, and small): only to a value the user set.
    const codingMaxRam: number = codingMaxRamGb(config.inspect<number>('worker.maxRamGb')?.globalValue);

    // The full URL, scheme included: dlne_server.py only adds http:// to a bare host, so
    // stripping https:// here sent workers to port 80, where the proxy answers with a redirect.
    const coordinatorArg = serverUrl;

    this.currentState = 'starting';
    this.statusMessage = plan.cpuOnly ? 'Joining the CPU pool that powers the DLNE phone app...' : 'Initializing local Vulkan compute worker...';
    this.emitChange();

    let scriptPath = this.findEngineScript();
    let rawPython = config.get<string>('worker.pythonPath') || '';
    let pythonPath = this.resolvePythonPath(rawPython, rootWorkspace);

    if (!scriptPath || !pythonPath || !fs.existsSync(pythonPath)) {
      const runtime = await this.ensureWorkerRuntime(true);
      if (runtime) {
        scriptPath = runtime.scriptPath;
        pythonPath = runtime.pythonPath;
      } else {
        this.currentState = 'idle';
        this.statusMessage = 'DLNE worker setup was cancelled.';
        this.emitChange();
        return false;
      }
    }

    const autoUpdate = config.get<boolean>('autoUpdate', true);
    await this.ensureRpcBinaries(path.dirname(scriptPath), serverUrl);
    if (autoUpdate && (scriptPath.includes(path.join('DLNE', 'runtime')) || scriptPath.includes(path.join('.dlne', 'runtime')))) {
      await this.syncEngineSource(path.dirname(scriptPath), serverUrl);
    }
    this.syncCodingMemberSetting(path.dirname(scriptPath));
    this.syncAlsoCpuSetting(plan, path.dirname(scriptPath));
    this.syncMaxCpuThreadsSetting(path.dirname(scriptPath));

    const spawnCmd = pythonPath;
    const args = [scriptPath, 'worker', '--coordinator', coordinatorArg];

    if (networkKey) args.push('--network-key', networkKey);
    if (allowedGpus) args.push('--allowed-gpus', allowedGpus);
    if (maxVram > 0) args.push('--max-vram', maxVram.toString());
    if (maxRam > 0) args.push('--max-ram', maxRam.toString());
    // An engine older than 2.4.0 refuses arguments it does not know (and then never starts), so the flag goes only
    // to an engine that reads it; an older one keeps holding the coding job to --max-ram, as it always did.
    let engineSource = '';
    try { engineSource = fs.readFileSync(scriptPath, 'utf8'); } catch {}
    if (engineSource.includes('--coding-max-ram')) args.push('--coding-max-ram', codingMaxRam.toString());
    // "Both": the RAM the CPU lends to the phone pool next to the GPU's job, for an engine that reads it (the same
    // guard: an older one refuses unknown arguments). Nothing is added for a machine that lends one thing only.
    const alsoCpuRam: number = config.get<number>('worker.alsoCpuRamGb') ?? 0;
    if (plan.alsoCpu && alsoCpuRam > 0) {
      if (engineSource.includes('--also-cpu-ram')) {
        args.push('--also-cpu-ram', alsoCpuRam.toString());
      } else {
        this.outputChannel.appendLine('[DLNE] This engine is too old to lend the CPU next to the GPU; the GPU is lent alone.');
      }
    }

    this.outputChannel.show(true);
    const safeArgs = args.map((a, idx) => (idx > 0 && args[idx - 1] === '--network-key' ? '***' : a));
    this.outputChannel.appendLine(`[DLNE] Spawning worker: ${spawnCmd} ${safeArgs.join(' ')}`);

    try {
      const cwd = path.dirname(scriptPath);
      const spawnEnv: NodeJS.ProcessEnv = {
        ...process.env,
        PYTHONUNBUFFERED: '1',
        GGML_VK_PREVENT_BARRIERS: '1',
        RADV_PERFTEST: 'nogttspill',
        DLNE_AUTO_UPDATE: autoUpdate ? 'true' : 'false'
      };
      if (!allowedGpus && !spawnEnv.GGML_VK_VISIBLE_DEVICES) {
        const discreteVk = this.detectDiscreteVulkanIndices();
        if (discreteVk) {
          spawnEnv.GGML_VK_VISIBLE_DEVICES = discreteVk;
          this.outputChannel.appendLine(`[DLNE] Hybrid GPU detected: isolating discrete Vulkan GPU index(es) ${discreteVk}`);
        }
      }

      // Windows: python.exe is a console program and this process has no console, so without windowsHide it
      // would sit in a black window for as long as the worker runs. And the worker's output is UTF-8
      // whatever the console code page is, so a log line with an accented letter cannot raise in a pipe.
      spawnEnv.PYTHONIOENCODING = 'utf-8';
      this.workerProcess = child_process.spawn(spawnCmd, args, {
        cwd,
        env: spawnEnv,
        windowsHide: true
      });
      void this.context?.globalState?.update(OWN_WORKER_KEY, this.workerProcess.pid);

      this.lastRefusal = null;
      this.assignedRole = null;
      this.dock = null;
      this.yielding = null;
      this.alsoCpuRunning = false;
      this.clockOffsetsShown = new Set();
      this.vcredistShown = false;
      this.outputTail = { stdout: '', stderr: '' };
      this.workerProcess.stdout?.on('data', (data) => {
        const text = data.toString();
        this.outputChannel.append(text);
        this.noteWorkerOutput(text, 'stdout');
        if (text.includes('Heartbeat OK') || text.includes('Heartbeat ACK:') || text.includes('RPC Server started')) {
          this.markContributing();
        }
      });

      this.workerProcess.stderr?.on('data', (data) => {
        const text = data.toString();
        this.outputChannel.append(text);
        this.noteWorkerOutput(text, 'stderr');
        if (text.includes('Heartbeat OK') || text.includes('Heartbeat ACK:')) {
          this.markContributing();
        }
      });

      this.workerProcess.on('error', (err) => {
        this.outputChannel.appendLine(`[DLNE ERROR] Process error: ${err.message}`);
        this.currentState = 'error';
        this.statusMessage = `Worker failed: ${err.message}`;
        this.workerProcess = null;
        this.emitChange();
      });

      this.workerProcess.on('exit', (code) => this.onWorkerExit(code));

      return true;
    } catch (err: any) {
      this.currentState = 'error';
      this.statusMessage = `Failed to start worker: ${err.message}`;
      this.outputChannel.appendLine(`[DLNE ERROR] ${err.message}`);
      this.emitChange();
      return false;
    }
  }

  /**
   * The worker process ended. Exit code 75 is the engine's way of saying "the coordinator runs
   * a different version than I do" (see VERSION_MISMATCH_EXIT_CODE in engine/worker.py): that is
   * not a crash to report, it is a request to bring the engine in line and start again.
   */
  private onWorkerExit(code: number | null): void {
    this.outputChannel.appendLine(`[DLNE] Worker process exited with code ${code}`);
    this.currentState = this.vcredistShown ? 'error' : 'idle';
    this.assignedRole = null;
    this.dock = null;
    this.yielding = null;
    this.alsoCpuRunning = false;
    this.startedWith = null;
    this.vramContributed = 0;
    this.statusMessage = this.vcredistShown ? 'Install the Visual C++ Runtime to contribute' : `Worker stopped (code ${code})`;
    this.workerProcess = null;
    this.emitChange();

    if (code === VERSION_MISMATCH_EXIT_CODE) {
      void this.restartAfterVersionMismatch();
    } else if (code === CPU_BELOW_MINIMUM_EXIT_CODE) {
      // The coordinator will not admit this machine as a CPU-only node, and says why.
      const reason = this.lastRefusal || 'this machine is below the swarm\'s minimum for powering the DLNE phone app.';
      this.currentState = 'error';
      this.statusMessage = 'This machine is below the minimum for powering the DLNE phone app';
      this.emitChange();
      void vscode.window.showErrorMessage(`DLNE can't use this machine yet: ${reason}`, 'Show Log').then((choice) => {
        if (choice === 'Show Log') this.outputChannel.show(true);
      });
    }
  }

  /** The coordinator's reason for its last refusal of this machine (from the worker's output). */
  private lastRefusal: string | null = null;
  /** What the running (or last started) worker contributes. */
  private contributionPlan: ContributionPlan | null = null;

  private markContributing(): void {
    // A leaving worker still prints heartbeats: they must not turn "Leaving the swarm" back into "Actively contributing".
    if (this.stopping) return;
    this.currentState = 'contributing';
    if (this.externalWorker) {
      // What it lends is its own settings' business, not these.
      this.vramContributed = 0;
      this.statusMessage = this.trayWorker ? TRAY_WORKER_LINE : EXTERNAL_WORKER_LINE;
      this.emitChange();
      return;
    }
    if (this.contributesCpu()) {
      const ram = vscode.workspace.getConfiguration('dlne').get<number>('worker.maxRamGb') || CPU_MIN_RAM_GB;
      this.vramContributed = ram;
      this.statusMessage = `${CPU_POOL_HEADLINE}, with ${ram} GB of RAM!`;
    } else {
      this.vramContributed = this.localGpu.hasGpu ? this.localGpu.vramTotalGb : 4.0;
      this.statusMessage = `Actively contributing ${this.vramContributed} GB via ${this.localGpu.api.toUpperCase()}!`;
    }
    this.emitChange();
  }

  /**
   * Decide what to contribute (dlne.worker.device and the detected GPU), and make sure a CPU-only
   * node lends the RAM the coordinator requires. Null when the worker must not start.
   */
  private async planThisMachine(): Promise<ContributionPlan | null> {
    const config = vscode.workspace.getConfiguration('dlne');
    const gpu = this.localGpu.name === 'Probing GPU...' ? await this.detectLocalGpu() : this.localGpu;
    const plan = planContribution(
      config.get<string>('worker.device'),
      gpu.hasGpu,
      config.get<string>('worker.allowedGpus') || '',
      config.get<number>('worker.maxRamGb') ?? 4.0,
      gpu.integratedGpu,
      config.get<number>('worker.alsoCpuRamGb') ?? 0
    );
    if (plan.error) {
      this.currentState = 'idle';
      this.statusMessage = 'No GPU to contribute';
      this.emitChange();
      void vscode.window.showErrorMessage(plan.error);
      return null;
    }
    if (plan.needsMoreRam) {
      // The amounts follow the pool's model ladder and what this machine can spare (contribution.ts).
      const totalGb = installedRamGb(os.totalmem());
      const choices = ramChoices(totalGb);
      if (choices.length === 0 && plan.alsoCpu) {
        // "Both" on a machine too small to lend the CPU as well: the GPU is lent alone, and the owner is told.
        void vscode.window.showWarningMessage(bothRamTooSmallText(totalGb));
        plan.alsoCpu = false;
        plan.needsMoreRam = false;
        return plan;
      }
      if (choices.length === 0) {
        this.currentState = 'idle';
        this.statusMessage = 'Not started: not enough RAM to power the DLNE phone app';
        this.emitChange();
        void vscode.window.showWarningMessage(ramTooSmallText(totalGb));
        return null;
      }
      const labels = choices.map((c, i) => lendLabel(c, i === 0 && choices.length > 1));
      const { message, detail } = ramOfferText(
        plan.alsoCpu ? ALSO_CPU_REASON : cpuReason(gpu.hasGpu, gpu.integratedGpu), choices,
        plan.alsoCpu ? 'dlne.worker.alsoCpuRamGb' : undefined);
      const picked = await vscode.window.showWarningMessage(message, { modal: true, detail }, ...labels);
      const chosen = picked === undefined ? -1 : labels.indexOf(picked);
      if (chosen < 0) {
        this.currentState = 'idle';
        this.statusMessage = plan.alsoCpu ? 'Not started: say how much RAM the CPU lends, or lend the GPU alone' : 'Not started: a CPU-only machine needs more RAM lent';
        this.emitChange();
        return null;
      }
      // The amount for the CPU half of "both" is its own setting: dlne.worker.maxRamGb stays the GPU job's limit.
      await config.update(plan.alsoCpu ? 'worker.alsoCpuRamGb' : 'worker.maxRamGb', choices[chosen].gb, vscode.ConfigurationTarget.Global);
    }
    return plan;
  }

  /** Recent automatic restarts for a version mismatch, so a mismatch that never resolves cannot loop forever. */
  private versionRestarts: number[] = [];

  private async restartAfterVersionMismatch(): Promise<void> {
    const now = Date.now();
    this.versionRestarts = this.versionRestarts.filter((t) => now - t < VERSION_RESTART_WINDOW_MS);

    if (!vscode.workspace.getConfiguration('dlne').get<boolean>('autoUpdate', true)) {
      this.statusMessage = 'The coordinator runs a different engine version';
      this.outputChannel.appendLine('[DLNE] The coordinator runs a different engine version, and dlne.autoUpdate is off, so the engine was not updated.');
      vscode.window.showWarningMessage(
        'DLNE: the coordinator runs a different engine version. Turn on the setting dlne.autoUpdate (or update the engine yourself), then start contributing again.'
      );
      this.emitChange();
      return;
    }

    if (this.versionRestarts.length >= MAX_VERSION_RESTARTS) {
      this.currentState = 'error';
      this.statusMessage = 'Could not match the coordinator\'s engine version';
      this.outputChannel.appendLine('[DLNE] The engine still does not match the coordinator after automatic updates. See the lines above for why.');
      vscode.window.showWarningMessage(
        "DLNE: the engine still doesn't match the coordinator after updating twice. Open the 'DLNE Compute Worker' log to see why."
      );
      this.emitChange();
      return;
    }

    this.versionRestarts.push(now);
    this.outputChannel.appendLine('[DLNE] The coordinator runs a different engine version. Updating the engine and restarting the worker...');
    this.statusMessage = 'Updating the engine to match the coordinator...';
    this.emitChange();
    await this.startWorker();
  }

  /** @param quiet no "paused" toast: the worker is about to start again (restartWorker). */
  public stopWorker(quiet: boolean = false): Promise<void> {
    // A stop already under way (a worker leaving the swarm): the second one forces it.
    if (this.stopping) {
      this.outputChannel.appendLine('[DLNE] Stop again: ending the worker now.');
      this.forceStop?.();
      return this.stopping;
    }
    const stopping = this.stopWorkerGracefully(quiet).finally(() => {
      this.stopping = null;
      this.forceStop = null;
    });
    this.stopping = stopping;
    return stopping;
  }

  /** The stop under way (see stopWorker), and what a second Stop calls to end the worker at once. */
  private stopping: Promise<void> | null = null;
  private forceStop: (() => void) | null = null;

  /**
   * SIGTERM, then wait for the worker to be gone before anything else is touched. A worker that carries a shard
   * of the model leaves the swarm first (engine/worker.py: it keeps its RPC server up while the answers running
   * on it finish, 90 s at most), so it gets that long plus a margin before the kill; any other worker gets the
   * few seconds it needs to stop its model server. The orphan clean-up runs only once the worker has exited:
   * until then its RPC server is not an orphan but the worker's live child.
   */
  private async stopWorkerGracefully(quiet: boolean): Promise<void> {
    this.statusMessage = 'Stopping worker...';
    this.outputChannel.appendLine('[DLNE] Stopping compute worker...');

    try {
      await vscode.workspace.getConfiguration('dlne').update('worker.autoStart', false, vscode.ConfigurationTarget.Global);
    } catch {}

    // Stop systemd unit if active
    if (this.isSystemdActive()) {
      try {
        child_process.execSync('systemctl --user stop dlne-worker', { timeout: 3000 });
      } catch {}
    }

    // This editor's worker from before a window reload is SIGTERMed as well, so it leaves the swarm gracefully.
    // Its output went with the old window, so its job is not known: it gets the leave in case it carries a shard.
    const adopted = this.adoptedPid;
    this.adoptedPid = null;
    if (adopted !== null) {
      try { process.kill(adopted, 'SIGTERM'); } catch {}
    }
    const child = this.workerProcess;
    // Windows has no graceful SIGTERM: kill() is TerminateProcess either way, and the model server is in a job
    // object that dies with the worker (engine/coding_member.py), so the quick stop stays there (workerStopGraceMs).
    child?.kill('SIGTERM');

    const graceMs = workerStopGraceMs(adopted !== null || this.assignedRole?.role === 'rpc', process.platform);
    const leaving = graceMs > WORKER_STOP_GRACE_MS;
    if (leaving) {
      this.statusMessage = LEAVING_TEXT;
      this.outputChannel.appendLine(`[DLNE] ${LEAVING_TEXT}`);
      this.emitChange();
    }

    // Gone: the process has exited (the child's exit event, or the adopted pid no longer there).
    const gone = Promise.all([
      child
        ? new Promise<void>((resolve) => {
            if (child.exitCode !== null || child.signalCode !== null) resolve();
            else child.once('exit', () => resolve());
          })
        : null,
      adopted !== null
        ? new Promise<void>((resolve) => {
            const check = () => (processAlive(adopted) ? setTimeout(check, 250) : resolve());
            check();
          })
        : null
    ]).then(() => true);

    let forceKill: () => void = () => {};
    const killed = new Promise<void>((resolve) => {
      forceKill = () => {
        child?.kill('SIGKILL');
        if (adopted !== null) {
          try { process.kill(adopted, 'SIGKILL'); } catch {}
        }
        resolve();
      };
    });
    this.forceStop = forceKill;
    const killTimer = setTimeout(forceKill, graceMs);
    // Gone, or (when the kill did not take either) given up on: only a worker seen to exit is cleaned up after.
    const exited = Promise.race([
      gone,
      killed.then(() => new Promise<boolean>((resolve) => setTimeout(() => resolve(false), WORKER_KILL_SETTLE_MS)))
    ]);

    // A worker that is still leaving after a moment is not stuck: say so, and let Cancel force it like a second Stop.
    let noticeTimer: NodeJS.Timeout | undefined;
    if (leaving) {
      noticeTimer = setTimeout(() => {
        void vscode.window.withProgress(
          { location: vscode.ProgressLocation.Notification, title: `DLNE: ${LEAVING_TEXT}`, cancellable: true },
          (_progress, token) => {
            token?.onCancellationRequested(forceKill);
            return exited;
          }
        );
      }, LEAVING_NOTICE_DELAY_MS);
    }

    const workerExited = child || adopted !== null ? await exited : true;
    clearTimeout(killTimer);
    clearTimeout(noticeTimer);
    if (this.workerProcess === child) {
      this.workerProcess = null;
    }

    // An orphaned ggml-rpc-server of THIS install (its worker was killed before it could stop it) would keep port
    // 50052: it is stopped by its full path, never by name (another program's, or another install's, is not ours).
    // Only after the worker has exited: while it lives, its RPC server is its own child, still draining answers.
    const engineScript = this.findEngineScript();
    const orphanStop = engineScript && workerExited
      ? orphanRpcStopCommand(
          path.join(path.dirname(engineScript), 'bin', process.platform === 'win32' ? 'ggml-rpc-server.exe' : 'ggml-rpc-server'),
          process.platform)
      : null;
    if (!workerExited) {
      this.outputChannel.appendLine('[DLNE] The worker did not exit after being killed; its engine is left alone.');
    }
    if (orphanStop) {
      try {
        child_process.execFileSync(orphanStop[0], orphanStop[1], { timeout: 4000, windowsHide: true, stdio: 'ignore' });
      } catch {}   // pkill exits 1 when nothing matched
    }

    this.currentState = 'idle';
    this.externalWorker = false;
    this.trayWorker = false;
    this.assignedRole = null;
    this.dock = null;
    this.yielding = null;
    this.alsoCpuRunning = false;
    this.startedWith = null;
    this.vramContributed = 0;
    this.statusMessage = 'Worker stopped.';
    this.emitChange();
    if (!quiet) {
      vscode.window.showInformationMessage('DLNE compute contribution paused.');
    }
  }

  /** @param options.fromPanel the click came from the sidebar's Donate panel (see startWorker). */
  public async toggle(options: { fromPanel?: boolean } = {}): Promise<boolean> {
    await this.refreshWorkerState();
    if (this.currentState === 'contributing' || this.currentState === 'starting') {
      // The tray app's worker is not this editor's to stop (its app would only report that it died): it keeps
      // donating, and the owner is told where to stop it.
      if (this.externalWorker && this.trayWorker) {
        vscode.window.showInformationMessage(`DLNE: ${TRAY_WORKER_TEXT}`);
        return true;
      }
      await this.stopWorker();
      return false;
    } else {
      return await this.startWorker({ interactive: true, fromPanel: options.fromPanel });
    }
  }

  /**
   * Write what the Donate panel chose (already validated by validateChoice) into the settings, and the thread limit
   * into the engine's config.txt at once, so a running worker follows it without a restart.
   */
  public async applyDonateChoice(choice: DonateChoice, machine: DonateMachine): Promise<void> {
    const config = vscode.workspace.getConfiguration('dlne');
    for (const [key, value] of settingsToWrite(choice, machine)) {
      await config.update(key, value, vscode.ConfigurationTarget.Global);
    }
    this.syncMaxCpuThreadsSetting();
  }

  /**
   * Stop this editor's worker and start it again with the settings as they are now. A worker that is still shutting
   * down holds the port, and a start that found it taken would take it for a worker started outside VS Code, so the
   * start waits for the process to be gone.
   */
  public async restartWorker(): Promise<boolean> {
    await this.stopWorker(true);
    const giveUp = Date.now() + WORKER_STOP_GRACE_MS + 3000;
    while ((this.workerProcess || this.isProcessActive() || (await this.isPortListening(50052))) && Date.now() < giveUp) {
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    return this.startWorker({ interactive: false });
  }

  private emitChange() {
    this.onStatusChangeEmitter.fire(this.getStatus());
  }

  public dispose() {
    this.disposed = true;
    if (this.pollInterval) {
      clearInterval(this.pollInterval);
      this.pollInterval = null;
    }
    if (this.updateCheckInterval) {
      clearInterval(this.updateCheckInterval);
      this.updateCheckInterval = null;
    }
    this.settingListener?.dispose();
    this.settingListener = undefined;
    // Retain worker across window reloads: do NOT call stopWorker() on dispose.
    // Background compute should remain active unless the user explicitly stops it.
    this.outputChannel.dispose();
    this.onStatusChangeEmitter.dispose();
  }
}
