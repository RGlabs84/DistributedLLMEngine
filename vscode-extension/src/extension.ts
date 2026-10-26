import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
import * as os from 'os';
import * as https from 'https';
import { DlneClient } from './client/dlneClient';
import { DlneFimProvider } from './completion/fimProvider';
import { SidebarProvider } from './views/sidebarProvider';
import { registerNativeChatParticipant } from './views/nativeChatParticipant';
import { ClusterStatusBar } from './statusbar/clusterStatusBar';
import { WorkerManager } from './worker/workerManager';
import { CPU_POOL_HEADLINE, CPU_POOL_PERK, workerStartedNotice } from './worker/contribution';
import { phoneChatUrl } from './serverUrl';

function isNewerVersion(current: string, remote: string): boolean {
  const cParts = current.split('.').map((n) => parseInt(n, 10) || 0);
  const rParts = remote.split('.').map((n) => parseInt(n, 10) || 0);
  for (let i = 0; i < Math.max(cParts.length, rParts.length); i++) {
    const c = cParts[i] || 0;
    const r = rParts[i] || 0;
    if (r > c) return true;
    if (r < c) return false;
  }
  return false;
}

function httpsGetJson(url: string): Promise<any> {
  return new Promise((resolve, reject) => {
    https
      .get(url, { headers: { 'User-Agent': 'DLNE-VSCode-Extension' }, timeout: 8000 }, (res) => {
        if (res.statusCode && res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
          return httpsGetJson(res.headers.location).then(resolve, reject);
        }
        if (res.statusCode !== 200) {
          res.resume();
          return reject(new Error(`HTTP ${res.statusCode}`));
        }
        let body = '';
        res.on('data', (chunk) => (body += chunk));
        res.on('end', () => {
          try {
            resolve(JSON.parse(body));
          } catch (e) {
            reject(e);
          }
        });
      })
      .on('error', reject);
  });
}

function downloadVsix(url: string, destPath: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const request = (targetUrl: string) => {
      https
        .get(targetUrl, { headers: { 'User-Agent': 'DLNE-VSCode-Extension' }, timeout: 30000 }, (res) => {
          if (res.statusCode && res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
            res.resume();
            return request(res.headers.location);
          }
          if (res.statusCode !== 200) {
            res.resume();
            return reject(new Error(`HTTP ${res.statusCode}`));
          }
          const file = fs.createWriteStream(destPath);
          res.pipe(file);
          file.on('finish', () => file.close(() => resolve()));
          file.on('error', (err) => {
            try { fs.unlinkSync(destPath); } catch {}
            reject(err);
          });
        })
        .on('error', reject);
    };
    request(url);
  });
}

async function checkAndAutoUpdateExtension(context: vscode.ExtensionContext): Promise<void> {
  try {
    const currentVersion: string = context.extension.packageJSON?.version || '1.0.0';
    const meta = await httpsGetJson('https://open-vsx.org/api/dlne/dlne-code-assistant');
    const remoteVersion: string | undefined = meta?.version;
    const downloadUrl: string | undefined = meta?.files?.download;

    if (!remoteVersion || !downloadUrl || !isNewerVersion(currentVersion, remoteVersion)) {
      return;
    }

    const autoUpdate = vscode.workspace.getConfiguration('dlne').get<boolean>('autoUpdate', true);

    const installAndPromptReload = async () => {
      const tmpVsix = path.join(os.tmpdir(), `dlne-code-assistant-${remoteVersion}.vsix`);
      await downloadVsix(downloadUrl, tmpVsix);
      await vscode.commands.executeCommand('workbench.extensions.installExtension', vscode.Uri.file(tmpVsix));
      try { fs.unlinkSync(tmpVsix); } catch {}
      const reloadChoice = await vscode.window.showInformationMessage(
        `🎉 DLNE Assistant updated to v${remoteVersion} (from v${currentVersion})! Reload window to activate the update.`,
        'Reload Window',
        'Later'
      );
      if (reloadChoice === 'Reload Window') {
        await vscode.commands.executeCommand('workbench.action.reloadWindow');
      }
    };

    if (autoUpdate) {
      await installAndPromptReload();
    } else {
      const choice = await vscode.window.showInformationMessage(
        `🚀 A new version of DLNE Frontier Coding Assistant (v${remoteVersion}) is available! (Current: v${currentVersion})`,
        'Update Now',
        'Later'
      );
      if (choice === 'Update Now') {
        await installAndPromptReload();
      }
    }
  } catch {}
}

export function activate(context: vscode.ExtensionContext) {
  // VS Code's own extensions.autoUpdate is the user's choice for every extension they have, so it
  // is left alone; dlne.autoUpdate covers this extension through the Open VSX check below.

  // Check Open VSX for newer extension releases on startup and every 30 minutes
  setTimeout(() => checkAndAutoUpdateExtension(context), 3000);
  const updateTimer = setInterval(() => checkAndAutoUpdateExtension(context), 30 * 60 * 1000);
  context.subscriptions.push({ dispose: () => clearInterval(updateTimer) });

  const client = new DlneClient();
  const workerManager = new WorkerManager(context);
  client.setWorkerManager(workerManager);
  context.subscriptions.push(workerManager);

  // 1. Status Bar
  const statusBar = new ClusterStatusBar(client, workerManager);
  statusBar.startPolling();
  context.subscriptions.push(statusBar);

  // 2. FIM Inline Completion Provider (All programming languages)
  const fimProvider = new DlneFimProvider(client);
  const inlineDisposable = vscode.languages.registerInlineCompletionItemProvider(
    { pattern: '**' },
    fimProvider
  );
  context.subscriptions.push(inlineDisposable);

  // 3a. Frontier Reasoning Agent Sidebar Webview (Dedicated Toolbar Panel)
  const sidebarProvider = new SidebarProvider(context.extensionUri, client, workerManager, context);
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider('dlne.chatView', sidebarProvider)
  );

  // 3b. Native Built-in VS Code Chat Participant (@dlne in Ctrl+Alt+I Chat Panel)
  registerNativeChatParticipant(context, client, workerManager);

  // 4. Commands
  context.subscriptions.push(
    vscode.commands.registerCommand('dlne.refreshStatus', async () => {
      await statusBar.updateStatus();
      vscode.window.showInformationMessage('DLNE Cluster status refreshed.');
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand('dlne.openChat', () => {
      vscode.commands.executeCommand('workbench.view.extension.dlne-sidebar');
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand('dlne.toggleWorker', async () => {
      const active = await workerManager.toggle();
      if (active) {
        vscode.window.showInformationMessage(workerStartedNotice(workerManager.getStatus().cpuPool));
      } else {
        vscode.window.showInformationMessage('DLNE Worker Stopped.');
      }
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand('dlne.installSuite', async () => {
      const runtime = await workerManager.ensureWorkerRuntime(true);
      if (runtime) {
        vscode.window.showInformationMessage('✅ DLNE Compute Engine is ready!');
        const startNow = await vscode.window.showInformationMessage(
          'Would you like to start contributing now? A GPU powers the coding agent; a computer without one powers the DLNE phone app.',
          'Start Contributing (Recommended)',
          'Later'
        );
        if (startNow === 'Start Contributing (Recommended)') {
          await workerManager.startWorker();
        }
      }
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand('dlne.openPhoneApp', async () => {
      await vscode.env.openExternal(vscode.Uri.parse(phoneChatUrl(vscode.workspace.getConfiguration('dlne'))));
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand('dlne.clusterMenu', async () => {
      const workerStatus = workerManager.getStatus();
      const isContrib = workerStatus.state === 'contributing';
      const clusterDetailed = await client.getClusterStatusDetailed();
      const cluster = clusterDetailed.data;
      // Unknown is shown as unknown: invented numbers here once made an offline cluster look healthy.
      const activeModel = cluster?.global_active_model || 'unknown model';
      const tier = cluster?.recommended_tier || 'cluster unreachable';
      const totalVram = cluster?.total_vram_gb ?? '?';
      const nodes = cluster?.active_workers_count ?? '?';

      const items: vscode.QuickPickItem[] = [
        {
          label: isContrib ? '$(debug-stop) Stop Contributing Compute' : '$(zap) Start Contributing Local Compute',
          description: workerStatus.cpuPool
            ? isContrib ? `Powering the DLNE phone app with ${workerStatus.vramContributedGb}GB of RAM` : CPU_POOL_HEADLINE
            : isContrib ? `Actively contributing ${workerStatus.vramContributedGb}GB` : `Join ${workerStatus.gpu.name} to pool`,
          detail: workerStatus.cpuPool
            ? `You get ${CPU_POOL_PERK}.`
            : 'Core Rule: You must give compute to receive compute.'
        },
        {
          label: `$(server) Swarm Auto-Tiering: ${tier}`,
          description: `Serving ${activeModel}`,
          detail: `Managed automatically by DLNE swarm (${totalVram} GB pooled VRAM across ${nodes} nodes)`
        },
        {
          label: '$(comment-discussion) Open DLNE Agent Chat',
          description: 'Launch autonomous distributed coding chat'
        },
        {
          label: '$(device-mobile) Use DLNE on your phone',
          description: phoneChatUrl(vscode.workspace.getConfiguration('dlne'))
        },
        {
          label: '$(gear) Set Up Compute Engine',
          description: 'Installs the signed DLNE engine used to contribute compute'
        },
        {
          label: '$(refresh) Refresh Cluster Telemetry',
          description: 'Re-query Coordinator status and model state'
        }
      ];

      const chosen = await vscode.window.showQuickPick(items, {
        placeHolder: 'DLNE Cluster Swarm Actions'
      });

      if (!chosen) return;

      if (chosen.label.includes('Swarm Auto-Tiering')) {
        vscode.window.showInformationMessage(
          `🤖 DLNE Swarm Auto-Tiering: Currently running ${activeModel} (${tier}) across ${totalVram} GB VRAM. Tiers scale automatically as nodes join.`
        );
      } else if (chosen.label.includes('Contributing')) {
        await workerManager.toggle();
      } else if (chosen.label.includes('Set Up Compute Engine')) {
        vscode.commands.executeCommand('dlne.installSuite');
      } else if (chosen.label.includes('Open DLNE Agent')) {
        vscode.commands.executeCommand('dlne.openChat');
      } else if (chosen.label.includes('on your phone')) {
        vscode.commands.executeCommand('dlne.openPhoneApp');
      } else if (chosen.label.includes('Refresh')) {
        await statusBar.updateStatus();
      }
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand('dlne.fixDiagnostics', async () => {
      const activeEditor = vscode.window.activeTextEditor;
      if (!activeEditor) {
        vscode.window.showWarningMessage('No active editor open.');
        return;
      }
      const diags = vscode.languages.getDiagnostics(activeEditor.document.uri);
      const errors = diags.filter(d => d.severity === vscode.DiagnosticSeverity.Error);
      if (errors.length === 0) {
        vscode.window.showInformationMessage('No errors found in active file.');
        return;
      }
      vscode.commands.executeCommand('workbench.view.extension.dlne-sidebar');
      vscode.window.showInformationMessage(`Sending ${errors.length} error(s) to DLNE Agent...`);
    })
  );
}

export function deactivate() {}
