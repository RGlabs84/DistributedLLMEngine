import * as vscode from 'vscode';
import * as path from 'path';
import { DlneClient, formatLoadingTelemetry } from '../client/dlneClient';
import { WorkerManager } from '../worker/workerManager';
import { CPU_POOL_HEADLINE, CPU_POOL_PERK } from '../worker/contribution';
import { AgentLoop } from '../agent/agentLoop';
import { ToolCallResult } from '../agent/tools';

const TIER_MODELS: Record<string, { file: string; label: string; vram: string }> = {
  'qwen3': {
    file: 'qwen3-coder-30b-instruct-q4_k_m.gguf',
    label: 'Qwen3 Coder 30B MoE (256K Context Tier)',
    vram: '24GB'
  },
  '1m': {
    file: 'qwen2.5-14b-1m.gguf',
    label: 'Qwen 2.5 14B 1M (1 Million Context Tier)',
    vram: '16GB'
  },
  '32b': {
    file: 'qwen2.5-coder-32b-instruct-q4_k_m.gguf',
    label: 'Qwen 2.5 Coder 32B (24GB Frontier Coder Tier)',
    vram: '24GB'
  },
  '14b': {
    file: 'qwen2.5-coder-14b-instruct-q4_k_m.gguf',
    label: 'Qwen 2.5 Coder 14B (16GB Baseline Tier)',
    vram: '16GB'
  },
  'r1': {
    file: 'DeepSeek-R1-Distill-Qwen-32B-Q4_K_M.gguf',
    label: 'DeepSeek-R1 Distill Qwen 32B (24GB Reasoning Tier)',
    vram: '24GB'
  },
  '70b': {
    file: 'Llama-3.3-70B-Instruct-IQ3_M.gguf',
    label: 'Llama 3.3 70B Instruct (128K Big Guns Tier)',
    vram: '32GB'
  },
  'r1-70b': {
    file: 'DeepSeek-R1-Distill-Llama-70B-Q4_K_M.gguf',
    label: 'DeepSeek-R1 Distill Llama 70B (42GB Flagship Reasoning)',
    vram: '42GB'
  },
  '72b': {
    file: 'Qwen2.5-72B-Instruct-Q4_K_M.gguf',
    label: 'Qwen 2.5 72B Instruct (44GB Flagship Omnimodal/Code)',
    vram: '44GB'
  }
};

export function registerNativeChatParticipant(
  context: vscode.ExtensionContext,
  client: DlneClient,
  workerManager: WorkerManager
): void {
  const chatNs = (vscode as any).chat;
  if (!chatNs || typeof chatNs.createChatParticipant !== 'function') {
    return;
  }

  const agentLoop = new AgentLoop(client);

  const handler = async (
    request: any,
    _chatContext: any,
    stream: any,
    token: vscode.CancellationToken
  ) => {
    const cmd: string | undefined = request.command;
    const userPrompt: string = (request.prompt || '').trim();

    // 1. /status command
    if (cmd === 'status') {
      stream.progress('Querying DLNE Cluster status...');
      const detailed = await client.getClusterStatusDetailed();
      const worker = workerManager.getStatus();
      const extVersion =
        vscode.extensions.getExtension('dlne.dlne-code-assistant')?.packageJSON?.version || '1.1.0';

      if (detailed.status === 'forbidden') {
        stream.markdown(
          [
            `### 🔒 DLNE Cluster: Compute Required`,
            `The DLNE Cluster at \`${client.getServerUrl()}\` is online and actively enforcing the **Non-Contrib Policy** (HTTP 403).`,
            ``,
            `- **Coordinator**: \`${client.getServerUrl()}\``,
            `- **Local Compute Worker**: \`${worker.state === 'contributing' ? 'Running' : 'Idle (Not Contributing)'}\` (${worker.gpu.name})`,
            `- **Policy Rule**: *You must give compute to receive compute.*`,
            ``,
            `👉 Use \`@dlne /contribute\` or click the status bar item to start contributing: a GPU powers the coding agent (first in line), and a computer without one powers the DLNE phone app (coding agent second in line).`
          ].join('\n')
        );
        return { metadata: { command: 'status' } };
      }

      if (detailed.status === 'unreachable' || !detailed.data) {
        stream.markdown(
          `### ❌ DLNE Cluster Offline\nCould not connect to DLNE Cluster at \`${client.getServerUrl()}\`.\nCheck your internet connection or verify \`dlne.serverUrl\` in settings.`
        );
        return { metadata: { command: 'status' } };
      }

      const status = detailed.data;

      const contribBadge = worker.cpuPool
        ? worker.state === 'contributing'
          ? `✅ **CONTRIBUTING** (CPU pool, ${worker.vramContributedGb} GB of RAM: ${CPU_POOL_HEADLINE})`
          : `⏸️ **IDLE** (your CPU can power the DLNE phone app)`
        : worker.state === 'contributing'
        ? `✅ **CONTRIBUTING** (${worker.vramContributedGb} GB via ${worker.gpu.api.toUpperCase()} — ${worker.gpu.name})`
        : `⏸️ **IDLE** (${worker.gpu.name})`;

      const loadingRow = status.model_loading?.is_loading
        ? `| **Model Sync Status** | **${formatLoadingTelemetry(status.model_loading)}** |\n`
        : `| **Model Sync Status** | ✅ **Ready in Distributed VRAM** |\n`;

      stream.markdown(
        [
          `### DLNE Distributed Cluster Status`,
          `| Metric | Value |`,
          `| :--- | :--- |`,
          `| **Cluster State** | \`${status.status}\` |`,
          `| **Active Model** | \`${status.global_active_model}\` |`,
          loadingRow.trimEnd(),
          `| **Total Compute Pool** | **${status.total_memory_gb} GB** (${status.total_vram_gb} GB VRAM + ${status.total_sysram_gb} GB SysRAM) |`,
          `| **Active RPC Nodes** | ${status.active_workers_count} node(s) |`,
          `| **Local Contribution** | ${contribBadge} |`,
          `| **Active Leases** | ${status.active_leases_count} |`,
          `| **Extension Version** | \`v${extVersion}\` |`,
          `| **Server Version** | \`v${status.server_version}\` |`,
          ``,
          `> **Core Rule**: *You must give compute to receive compute.*`
        ].join('\n')
      );
      return { metadata: { command: 'status' } };
    }

    // 2. /contribute command
    if (cmd === 'contribute') {
      stream.progress('Toggling local compute contribution...');
      await workerManager.toggle();
      const updated = workerManager.getStatus();
      if (updated.state === 'contributing' && updated.cpuPool) {
        stream.markdown(
          `### 📱 Compute Contribution Active!\n${CPU_POOL_HEADLINE}: **${updated.vramContributedGb} GB** of RAM lent, at low priority on half your cores. You get ${CPU_POOL_PERK}.`
        );
      } else if (updated.state === 'contributing') {
        stream.markdown(
          `### ⚡ Compute Contribution Active!\nContributing **${updated.vramContributedGb} GB VRAM** (${updated.gpu.name}) via **${updated.gpu.api.toUpperCase()}** to the DLNE cluster. Frontier inference unlocked!`
        );
      } else {
        stream.markdown(`### ⏹️ Compute Contribution Stopped\nYour local worker is now idle.`);
      }
      return { metadata: { command: 'contribute' } };
    }

    // 3. /clear or /reset command
    if (cmd === 'clear' || cmd === 'reset') {
      agentLoop.clearHistory();
      stream.markdown('🧹 **Agent conversation history cleared.** DLNE Frontier Agent context is fresh and ready for your next prompt.');
      return { metadata: { command: cmd } };
    }

    // 3. /model command
    if (cmd === 'model') {
      stream.progress('Checking Swarm Auto-Tiering status...');
      const detailed = await client.getClusterStatusDetailed();
      const status = detailed.data;
      const activeModel = status?.global_active_model || 'qwen2.5-coder-14b-instruct-q4_k_m.gguf';
      const vram = status?.total_vram_gb || 24.0;
      const nodes = status?.active_workers_count || 2;

      const lines = [
        `### 🤖 DLNE Swarm Auto-Tiering Active`,
        `Model selection is managed **fully automatically** by the swarm based on currently active compute nodes.`,
        ``,
        `| Metric | Current Swarm State |`,
        `| :--- | :--- |`,
        `| **Active Model** | \`${activeModel}\` |`,
        `| **Active Tier** | **${status?.recommended_tier || '16GB-24GB Baseline (14B)'}** |`,
        `| **Pooled Hardware** | **${vram} GB VRAM** across ${nodes} worker node(s) |`,
        ``,
        `#### 📈 Dynamic Auto-Scaling Thresholds:`,
        `- **14B Coder Tier** (16–24 GB VRAM) → ✅ **Active Now** (fastest, optimized with FlashAttention & speculative decoding)`,
        `- **32B Frontier Coder Tier** (≥ 28 GB VRAM) → 🔒 Unlocks automatically when 3+ nodes contribute (e.g. RTX 5070/4060+)`,
        `- **70B / 72B Flagship Tier** (≥ 44 GB VRAM) → 🔒 Unlocks automatically when cluster expands`,
        ``,
        `> **Zero Configuration Needed**: Whenever nodes join or leave, DLNE automatically adapts the model tier to match the exact VRAM envelope—preventing out-of-memory crashes.`
      ];
      stream.markdown(lines.join('\n'));
      return { metadata: { command: 'model' } };
    }

    // 4. Build final prompt (attach active editor / diagnostics for /fix or contextual references)
    let fullPrompt = userPrompt;
    if (cmd === 'fix') {
      const editor = vscode.window.activeTextEditor;
      if (editor) {
        const relPath = vscode.workspace.asRelativePath(editor.document.uri);
        const diags = vscode.languages
          .getDiagnostics(editor.document.uri)
          .filter((d) => d.severity === vscode.DiagnosticSeverity.Error);
        const diagText = diags.map((d) => `Line ${d.range.start.line + 1}: ${d.message}`).join('\n');
        fullPrompt =
          `Fix the issues in \`${relPath}\`.\n` +
          (diagText ? `Diagnostics:\n${diagText}\n` : '') +
          (userPrompt ? `Additional instructions: ${userPrompt}` : '');
      } else if (!fullPrompt) {
        fullPrompt = 'Inspect the workspace for issues and fix them.';
      }
    }

    if (!fullPrompt) {
      stream.markdown(
        `Ask **@dlne** to build features, edit files, or run commands — or try \`/status\`, \`/contribute\`, \`/model\`, or \`/fix\`.`
      );
      return { metadata: { command: cmd || 'chat' } };
    }

    token.onCancellationRequested(() => {
      agentLoop.abort();
    });

    let isStreamingThought = false;
    stream.progress('Connecting to DLNE Distributed Vulkan Cluster...');

    await agentLoop.run(fullPrompt, {
      onLoadingProgress: (statusText: string) => {
        stream.progress(statusText);
      },
      onThinkingDelta: (delta: string) => {
        if (!isStreamingThought) {
          isStreamingThought = true;
          stream.markdown(`\n> **Thought Process:**\n> `);
        }
        stream.markdown(delta.replace(/\n/g, '\n> '));
      },
      onContentDelta: (delta: string) => {
        if (isStreamingThought) {
          isStreamingThought = false;
          stream.markdown(`\n\n---\n\n`);
        }
        // Hide raw inline JSON/XML tool call blocks from the user's visible chat stream
        stream.markdown(delta);
      },
      onToolStart: (toolName: string, args: Record<string, any>) => {
        if (isStreamingThought) {
          isStreamingThought = false;
          stream.markdown(`\n\n---\n\n`);
        }
        const target = args.path || args.command || args.query || '';
        stream.progress(`🔧 Executing ${toolName}${target ? ` (${target})` : ''}...`);
      },
      onToolEnd: (result: ToolCallResult) => {
        if (result.fileDiff) {
          const diff = result.fileDiff;
          const folders = vscode.workspace.workspaceFolders;
          if (folders && folders.length > 0) {
            const fullPath = path.isAbsolute(diff.path)
              ? diff.path
              : path.join(folders[0].uri.fsPath, diff.path);
            const fileUri = vscode.Uri.file(fullPath);
            if (typeof stream.reference === 'function') {
              stream.reference(fileUri);
            }
            const formattedLines = diff.lines
              .slice(0, 60)
              .map((l) => {
                if (l.type === 'add') return `+ ${l.text}`;
                if (l.type === 'del') return `- ${l.text}`;
                if (l.type === 'header') return l.text;
                return `  ${l.text}`;
              })
              .join('\n');
            stream.markdown(
              `\n\n📄 **Modified \`${diff.path}\`** (+${diff.additions} / -${diff.deletions})\n\`\`\`diff\n${formattedLines}\n\`\`\`\n`
            );
            if (typeof stream.button === 'function') {
              stream.button({
                command: 'vscode.open',
                arguments: [fileUri],
                title: `📂 Open ${diff.path}`
              });
            }
          }
        } else if (result.terminalCommand) {
          stream.markdown(
            `\n\n💻 **Executed \`${result.terminalCommand}\`**:\n\`\`\`text\n${result.output.slice(0, 1000)}\n\`\`\`\n`
          );
        } else {
          stream.markdown(`\n\n✅ **\`${result.name}\`**: ${result.output.slice(0, 200)}\n\n`);
        }
      }
    });

    return { metadata: { command: cmd || 'chat' } };
  };

  const participant = chatNs.createChatParticipant('dlne.agent', handler);
  participant.iconPath = vscode.Uri.joinPath(context.extensionUri, 'media', 'dlne_logo.png');

  participant.followupProvider = {
    provideFollowups(_result: any, _context: any, _token: vscode.CancellationToken) {
      return [
        {
          prompt: '',
          command: 'status',
          label: '🌐 Cluster Status'
        },
        {
          prompt: '',
          command: 'clear',
          label: '🧹 Clear Agent History'
        },
        {
          prompt: '',
          command: 'contribute',
          label: '⚡ Toggle Contribution'
        }
      ];
    }
  };

  context.subscriptions.push(participant);

  // Also register DLNE models into VS Code's Native Model Picker Dropdown (vscode.lm API)
  const lmNs = (vscode as any).lm;
  if (lmNs) {
    const registerFn =
      typeof lmNs.registerChatModelProvider === 'function'
        ? lmNs.registerChatModelProvider.bind(lmNs)
        : typeof lmNs.registerLanguageModelChatProvider === 'function'
        ? lmNs.registerLanguageModelChatProvider.bind(lmNs)
        : null;

    if (registerFn) {
      for (const [tierKey, info] of Object.entries(TIER_MODELS)) {
        try {
          const disp = registerFn(
            `dlne-${tierKey}`,
            {
              async provideLanguageModelResponse(
                messages: any[],
                _options: any,
                _extensionId: string,
                progress: any,
                token: vscode.CancellationToken
              ) {
                await vscode.workspace
                  .getConfiguration('dlne')
                  .update('chatModel', info.file, vscode.ConfigurationTarget.Global);
                const mapped = messages.map((m: any) => ({
                  role: (m.role === 1 || m.role === 'user' ? 'user' : 'assistant') as 'user' | 'assistant',
                  content: typeof m.content === 'string' ? m.content : JSON.stringify(m.content)
                }));
                await client.streamChat(
                  mapped,
                  [],
                  (delta: string) => {
                    if (delta && progress && typeof progress.report === 'function') {
                      progress.report({ index: 0, part: delta });
                    }
                  },
                  undefined,
                  undefined
                );
              },
              async provideTokenCount(_text: string) {
                return Math.ceil((_text || '').length / 4);
              }
            },
            {
              vendor: 'dlne',
              name: info.label,
              family: 'dlne-vulkan',
              version: '1.0.3',
              maxInputTokens: 32768,
              maxOutputTokens: 4096,
              isDefault: tierKey === '14b',
              isUserSelectable: true
            }
          );
          if (disp) context.subscriptions.push(disp);
        } catch {}
      }
    }
  }
}
