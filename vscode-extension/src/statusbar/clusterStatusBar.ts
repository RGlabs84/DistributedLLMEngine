import * as vscode from 'vscode';
import { DlneClient, formatLoadingTelemetry } from '../client/dlneClient';
import { WorkerManager } from '../worker/workerManager';
import { CPU_POOL_HEADLINE, CPU_POOL_PERK, contributionTooltip } from '../worker/contribution';

export class ClusterStatusBar {
  private statusBarItem: vscode.StatusBarItem;
  private pollInterval: NodeJS.Timeout | null = null;
  private unreachableCount = 0;

  constructor(
    private client: DlneClient,
    private workerManager: WorkerManager
  ) {
    this.statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
    this.statusBarItem.command = 'dlne.clusterMenu';
    this.statusBarItem.tooltip = 'Click for DLNE Cluster & Compute Contribution Menu';
    this.statusBarItem.text = 'DLNE: Initializing...';
    this.statusBarItem.show();

    this.workerManager.onStatusChange(() => {
      this.updateStatus();
    });
  }

  startPolling() {
    this.updateStatus();
    this.pollInterval = setInterval(() => this.updateStatus(), 10000);
  }

  stopPolling() {
    if (this.pollInterval) {
      clearInterval(this.pollInterval);
      this.pollInterval = null;
    }
  }

  async updateStatus() {
    const detailed = await this.client.getClusterStatusDetailed();
    const workerStatus = this.workerManager.getStatus();
    const extVersion =
      vscode.extensions.getExtension('dlne.dlne-code-assistant')?.packageJSON?.version || '1.0.28';

    if (detailed.status === 'forbidden') {
      this.statusBarItem.text = `$(shield) DLNE v${extVersion}: Compute Required`;
      this.statusBarItem.tooltip = [
        `DLNE Cluster: Connected (${this.client.getServerUrl()})`,
        `Access: Gated by Non-Contrib Policy (HTTP 403)`,
        `Local Compute: ${workerStatus.state === 'contributing' ? `Contributing (${workerStatus.vramContributedGb}GB)` : 'Idle (Not Contributing)'}`,
        ``,
        `Core Rule: You must give compute to receive compute!`,
        workerStatus.cpuPool
          ? `Click to start contributing. ${CPU_POOL_HEADLINE}, and you get ${CPU_POOL_PERK}. Or configure an API key.`
          : `Click to start contributing local GPU compute (or your CPU, to power the DLNE phone app) or configure an API key.`
      ].join('\n');
      return;
    }

    if (detailed.status === 'unreachable' || !detailed.data) {
      this.unreachableCount++;
      if (this.unreachableCount >= 2) {
        this.statusBarItem.text = '$(error) DLNE: Offline';
        this.statusBarItem.tooltip = `Could not connect to DLNE Cluster at ${this.client.getServerUrl()}.\nCheck your internet connection or verify dlne.serverUrl in settings.`;
      }
      return;
    }

    this.unreachableCount = 0;

    const status = detailed.data;
    const modelName = status.global_active_model || 'No Model Active';
    const vram = status.total_vram_gb;
    const leases = status.active_leases_count;

    // A status bar item is text only, so it cannot carry the DLNE logo: it says DLNE, and shows
    // a state glyph only when there is a state to show.
    let icon = '';
    if (workerStatus.state === 'contributing') {
      icon = '⚡ ';
    } else if (status.is_locked_for_edits) {
      icon = '🔒 ';
    }

    const quota = this.client.getQuotaRemaining();
    const contribTag = workerStatus.state === 'contributing'
      ? workerStatus.cpuPool ? ` [Phone app ${workerStatus.vramContributedGb}GB]` : ` [Contributing ${workerStatus.vramContributedGb}GB]`
      : quota !== null ? ` [Free trial: ${quota} chats left]` : '';
    if (status.model_loading?.is_loading) {
      const ml = status.model_loading;
      const pct = typeof ml.progress_pct === 'number' ? `${ml.progress_pct.toFixed(0)}%` : '';
      const spd = typeof ml.speed_mb_s === 'number' && ml.speed_mb_s > 0 ? ` ${ml.speed_mb_s.toFixed(1)}MB/s` : '';
      this.statusBarItem.text = `$(sync~spin) DLNE v${extVersion}: Loading ${modelName} ${pct}${spd}`;
    } else {
      this.statusBarItem.text = `${icon}DLNE v${extVersion}: ${modelName} (${vram}GB)${contribTag}`;
    }
    this.statusBarItem.tooltip = [
      `DLNE Cluster Status: ${status.status}`,
      `Active Model: ${modelName}`,
      status.model_loading?.is_loading ? formatLoadingTelemetry(status.model_loading) : `Model Sync: Ready in Distributed VRAM`,
      `Total Compute: ${status.total_memory_gb}GB (${vram}GB VRAM, ${status.total_sysram_gb}GB SysRAM)`,
      `Workers in Pool: ${status.active_workers_count} node(s)`,
      ...contributionTooltip(workerStatus),
      `Active Leases: ${leases}`,
      `Extension Version: ${extVersion}`,
      `Server Version: ${status.server_version}`,
      `Rule: You must give compute to receive compute!`
    ].join('\n');
  }

  dispose() {
    this.stopPolling();
    this.statusBarItem.dispose();
  }
}
