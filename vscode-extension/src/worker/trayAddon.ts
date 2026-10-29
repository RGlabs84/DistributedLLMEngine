// The Donate panel's "Optional add-on" button: download, verify, run DLNE Donate, the tray app for Windows and Linux.
//
//   1. Read the manifest (https://dlne.rossgilsonlabs.dev/downloads/desktop.json) and check it whole (trayApp.ts).
//   2. Ask, with the version and the download size: the size is only known now, because nothing is fetched before
//      the owner clicks.
//   3. Download this platform's build next to its final name in the Downloads folder, with VS Code's progress
//      notification (it can be cancelled). The size and SHA-256 are checked; a mismatch deletes the file.
//   4. Ask "Run it now?" with Run and Show file. Run starts the app detached from VS Code.
//
// Nothing installs or runs without the owner's click, and the only network traffic is the manifest and the download.
// The panel shows what happened through view(); onChange asks the sidebar to draw the panel again.

import * as vscode from 'vscode';
import * as path from 'path';
import { ChildProcess } from 'child_process';
import { TrayAddonView } from './donatePanel';
import {
  SaveOptions, TrayBuild, TrayError, TrayManifest, TrayPlatform, downloadsDir, fetchTrayManifest, formatSize,
  launchTrayApp, saveVerifiedBuild, trayPlatform, trayPlatformName,
} from './trayApp';

/** An exit this soon after Run means the app did not start (a missing system library, a refused AppImage). */
const EARLY_EXIT_MS = 15_000;

/** The pieces the flow uses, so the tests can play the website and the machine. */
export interface TrayAddonDeps {
  platform(): TrayPlatform | null;
  fetchManifest(): Promise<TrayManifest>;
  downloadsDir(): string;
  save(build: TrayBuild, target: string, options: SaveOptions): Promise<{ path: string; reused: boolean }>;
  launch(file: string): ChildProcess;
}

const realDeps: TrayAddonDeps = {
  platform: () => trayPlatform(),
  fetchManifest: () => fetchTrayManifest(),
  downloadsDir: () => downloadsDir(),
  save: (build, target, options) => saveVerifiedBuild(build, target, options),
  launch: (file) => launchTrayApp(file),
};

/** Why a step stopped, in the panel's plain words. In every case nothing was kept. */
export function trayProblemText(err: unknown, step: 'manifest' | 'download', file: string = ''): string {
  const problem = err instanceof TrayError ? err.problem : 'network';
  const detail = (err as Error)?.message ?? String(err);
  if (step === 'manifest') {
    switch (problem) {
      case 'refused': return `The download list was refused (${detail}). Nothing was downloaded.`;
      case 'redirected': return 'The download list was redirected, so it was not used. Nothing was downloaded.';
      case 'too-large': return 'The download list is too large to be real, so it was not used. Nothing was downloaded.';
      default: return `Could not check the download (${detail}). Nothing was downloaded.`;
    }
  }
  switch (problem) {
    case 'cancelled': return 'Download cancelled. Nothing was kept.';
    case 'size': return 'The download was not the published size, so it was deleted.';
    case 'hash': return 'The download did not match its published SHA-256, so it was deleted.';
    case 'redirected': return 'The download was redirected, so it was refused. Nothing was kept.';
    case 'refused': return `The download was refused (${detail}). Nothing was kept.`;
    case 'save': return `Could not save ${file} (${detail}). If an older DLNE Donate runs from that folder, quit it and try again.`;
    default: return `The download failed (${detail}). Nothing was kept.`;
  }
}

export class TrayAddon {
  /** A click is being handled (from the manifest to the end of the download): another click does nothing. */
  private running = false;
  private busy = '';
  private note = '';
  private noteIsError = false;
  private sizeBytes: number | null = null;

  constructor(private readonly onChange: () => void, private readonly deps: TrayAddonDeps = realDeps) {}

  /** What the panel's card shows now (donatePanel.ts buildAddonCard). */
  public view(): TrayAddonView {
    return { platform: this.deps.platform(), busy: this.busy, sizeBytes: this.sizeBytes, note: this.note, noteIsError: this.noteIsError };
  }

  /** Changes what the card shows, and has the panel drawn again. */
  private update(changes: { busy?: string; note?: string; noteIsError?: boolean }): void {
    if (changes.busy !== undefined) this.busy = changes.busy;
    if (changes.note !== undefined) this.note = changes.note;
    if (changes.noteIsError !== undefined) this.noteIsError = changes.noteIsError;
    this.onChange();
  }

  /** The button was clicked. Never throws: whatever goes wrong is said in the card. */
  public async download(): Promise<void> {
    const platform = this.deps.platform();
    if (!platform || this.running) return;
    this.running = true;
    let saved: string | null = null;
    try {
      saved = await this.fetchAndSave(platform);
    } catch (err) {
      this.update({ note: trayProblemText(err, 'download'), noteIsError: true });
    } finally {
      this.running = false;
      if (this.busy) this.update({ busy: '' });
    }
    // Asked after the click is done with, so a question left unanswered never blocks the button.
    if (saved) await this.offerRun(saved, platform);
  }

  /** Steps 1 to 3. The saved file's path, or null when nothing was saved (the card says why). */
  private async fetchAndSave(platform: TrayPlatform): Promise<string | null> {
    this.update({ busy: 'Checking...', note: '', noteIsError: false });
    let manifest: TrayManifest;
    try {
      manifest = await this.deps.fetchManifest();
    } catch (err) {
      this.update({ busy: '', note: trayProblemText(err, 'manifest'), noteIsError: true });
      return null;
    }
    const name = trayPlatformName(platform);
    const build = manifest.builds[platform];
    if (!build) {
      this.update({ busy: '', note: `There is no DLNE Donate download for ${name} yet.`, noteIsError: true });
      return null;
    }

    // From here the button shows the size.
    this.sizeBytes = build.size;
    const size = formatSize(build.size);
    const folder = this.deps.downloadsDir();
    const target = path.join(folder, build.file);
    this.update({ busy: '' });

    const confirm = `Download (${size})`;
    const detail = [
      `It is saved in ${folder}. Its size and SHA-256 are checked against the DLNE website, and nothing runs unless you choose Run.`,
      manifest.notes,
    ].filter((line) => line).join('\n\n');
    const answer = await vscode.window.showInformationMessage(
      `Download DLNE Donate ${manifest.versionName} for ${name} (${size})?`, { modal: true, detail }, confirm);
    if (answer !== confirm) return null;

    this.update({ busy: 'Downloading...' });
    try {
      const saved = await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: `Downloading DLNE Donate ${manifest.versionName} (${size})`, cancellable: true },
        (progress, token) => {
          let shown = 0;
          return this.deps.save(build, target, {
            platform,
            onProgress: (bytes) => {
              const percent = Math.floor((bytes / build.size) * 100);
              if (percent > shown) {
                progress.report({ increment: percent - shown, message: `${percent}%` });
                shown = percent;
              }
            },
            isCancelled: () => token?.isCancellationRequested === true,
          });
        });
      this.update({ busy: '', note: `Saved ${build.file} in ${folder}.`, noteIsError: false });
      return saved.path;
    } catch (err) {
      const cancelled = err instanceof TrayError && err.problem === 'cancelled';
      this.update({ busy: '', note: trayProblemText(err, 'download', build.file), noteIsError: !cancelled });
      return null;
    }
  }

  /** Step 4: Run, or Show file, or nothing. */
  private async offerRun(file: string, platform: TrayPlatform): Promise<void> {
    const smartScreen = platform === 'windows'
      ? ' If Windows says "Windows protected your PC", click More info, then Run anyway.'
      : '';
    const choice = await vscode.window.showInformationMessage(`DLNE Donate is saved. Run it now?${smartScreen}`, 'Run', 'Show file');
    if (choice === 'Run') {
      this.start(file);
    } else if (choice === 'Show file') {
      this.reveal(file);
    }
  }

  /** Starts the app detached, and says so if it did not start (it could not be run, or it closed at once). */
  private start(file: string): void {
    let reported = false;
    const didNotStart = (reason: string) => {
      if (reported) return;
      reported = true;
      this.update({ note: `DLNE Donate did not start (${reason}).`, noteIsError: true });
      void vscode.window.showErrorMessage(`DLNE: DLNE Donate did not start (${reason}).`, 'Show file').then((choice) => {
        if (choice === 'Show file') this.reveal(file);
      });
    };
    let child: ChildProcess;
    try {
      child = this.deps.launch(file);
    } catch (err) {
      didNotStart((err as Error)?.message ?? String(err));
      return;
    }
    const startedAt = Date.now();
    child.once('error', (err) => didNotStart(err.message));
    child.once('exit', (code) => {
      if (code !== null && code !== 0 && Date.now() - startedAt < EARLY_EXIT_MS) didNotStart(`it closed at once, exit code ${code}`);
    });
    this.update({ note: 'DLNE Donate is starting. Choose what to lend in its window, then click Donate.', noteIsError: false });
  }

  private reveal(file: string): void {
    void vscode.commands.executeCommand('revealFileInOS', vscode.Uri.file(file));
  }
}
