import * as vscode from 'vscode';
import * as path from 'path';
import { DlneClient, formatLoadingTelemetry } from '../client/dlneClient';
import { WorkerManager } from '../worker/workerManager';
import {
  ALSO_CPU_LINE, CPU_POOL_HEADLINE, CPU_POOL_PERK, EXTERNAL_WORKER_LINE, EXTERNAL_WORKER_TEXT, MEMBER_PRIVACY_SUMMARY, ROLE_LABEL,
  TRAY_WORKER_LINE, TRAY_WORKER_TEXT, TRUST_ID_HINT, roleMeaning,
} from '../worker/contribution';
import { logError } from '../agent/agentLoop';
import { ChatThreads, Thread, readTranscript } from '../agent/chatThreads';
import { logLine } from '../log';
import { IMAGE_TOOL_NAME } from '../agent/imageTool';
import { ToolCallResult, ToolRegistry } from '../agent/tools';
import { redactSecrets } from '../privacy';
import { memoryMode } from '../agent/memory';
import { AUTO_BUTTON, observeApprovals } from './chatAuto';
import { fixRequest } from './chatEntry';
import { attachedContext } from './chatReferences';
import { ToolJsonFilter, diffBlock } from './chatText';

/**
 * The Chat view's permission picker puts its level on each request (`permissionLevel`; it is not in the
 * public typings, so it is read off the untyped request). These two are the levels that approve everything.
 */
function chatApprovesAll(request: any): boolean {
  return request?.permissionLevel === 'autoApprove' || request?.permissionLevel === 'autopilot';
}

/** The tools the agent loop runs when a model writes the call as text: the ones it offers, and the picture tool. */
const TOOL_NAMES = new Set([...ToolRegistry.getToolDefinitions().map((tool) => tool.function.name), IMAGE_TOOL_NAME]);

export function registerNativeChatParticipant(
  context: vscode.ExtensionContext,
  client: DlneClient,
  workerManager: WorkerManager
): void {
  const chatNs = (vscode as any).chat;
  if (!chatNs || typeof chatNs.createChatParticipant !== 'function') {
    return;
  }

  // One conversation per Chat thread, each with its saved task: a VS Code restart does not end a task, Continue works
  // after reopening, and a second thread no longer clears the first. (Persistent memory, AGENTS.md and the memory files,
  // goes with every request: docs/agent-memory.md section 7.)
  const threads = new ChatThreads(context, client);
  context.subscriptions.push({ dispose: () => threads.dispose() });

  const respond = async (
    request: any,
    chatContext: any,
    stream: any,
    token: vscode.CancellationToken,
    threadId: string,
    /** Filled in once the request's thread is known: what the answer names if the request then ends in an exception. */
    ran: { thread?: Thread }
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

      const contribBadge = worker.external && worker.state === 'contributing'
        ? `🔌 **${worker.trayApp ? TRAY_WORKER_LINE : EXTERNAL_WORKER_LINE}**`
        : worker.cpuPool
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
          ...(status.coding_queue
            ? [`| **Coding Slots** | ${status.coding_queue.in_use} of ${status.coding_queue.slots} busy, ${status.coding_queue.waiting} waiting |`]
            : []),
          ...(status.coding_members?.enabled
            ? [`| **Coding Members** | ${status.coding_members.ready ?? 0} ready of ${status.coding_members.members ?? 0}, ${status.coding_members.busy ?? 0} of ${status.coding_members.slots ?? 0} slots busy |`]
            : []),
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
      if (updated.state === 'contributing' && updated.external) {
        stream.markdown(`### 🔌 A worker is already running\n${updated.trayApp ? TRAY_WORKER_TEXT : EXTERNAL_WORKER_TEXT}`);
      } else if (updated.state === 'contributing' && updated.cpuPool) {
        stream.markdown(
          `### 📱 Compute Contribution Active!\n${CPU_POOL_HEADLINE}: **${updated.vramContributedGb} GB** of RAM lent, at low priority on half your cores. You get ${CPU_POOL_PERK}.`
        );
      } else if (updated.state === 'contributing') {
        // The job the swarm gave this GPU, once the worker has said (an older engine never does).
        const role = updated.role ?? 'rpc';
        const job = `\n\n**Your GPU's job: ${ROLE_LABEL[role]}.** ${roleMeaning(role, updated.roleDetail)}` +
          (role === 'coding'
            ? ` ${MEMBER_PRIVACY_SUMMARY}` +
              (updated.roleDetail?.trustId ? `\n\nTrust id: \`${updated.roleDetail.trustId}\`. ${TRUST_ID_HINT}` : '')
            : '') +
          (updated.alsoCpu ? `\n\n**Also: ${ROLE_LABEL.app}.** ${ALSO_CPU_LINE}` : '');
        stream.markdown(
          `### ⚡ Compute Contribution Active!\nContributing **${updated.vramContributedGb} GB VRAM** (${updated.gpu.name}) via **${updated.gpu.api.toUpperCase()}** to the DLNE cluster. Your GPU powers the coding agent, and you are first in line while the swarm uses it.${job}`
        );
      } else {
        stream.markdown(`### ⏹️ Compute Contribution Stopped\nYour local worker is now idle.`);
      }
      return { metadata: { command: 'contribute' } };
    }

    // 3. /clear or /reset command
    if (cmd === 'clear' || cmd === 'reset') {
      threads.forget(threadId);
      stream.markdown('🧹 **Agent conversation history cleared.** DLNE Code Assistant context is fresh and ready for your next prompt.');
      return { metadata: { command: cmd } };
    }

    // 3. /model command
    if (cmd === 'model') {
      stream.progress('Checking Swarm Auto-Tiering status...');
      const detailed = await client.getClusterStatusDetailed();
      const status = detailed.data;
      const activeModel = status?.global_active_model || 'not reported';
      const vram = status?.total_vram_gb ?? '?';
      const nodes = status?.active_workers_count ?? '?';

      const lines = [
        `### 🤖 DLNE Swarm Auto-Tiering Active`,
        `Model selection is managed **fully automatically** by the swarm based on currently active compute nodes.`,
        ``,
        `| Metric | Current Swarm State |`,
        `| :--- | :--- |`,
        `| **Active Model** | \`${activeModel}\` |`,
        `| **Active Tier** | **${status?.recommended_tier || 'not reported'}** |`,
        `| **Pooled Hardware** | **${vram} GB VRAM** across ${nodes} worker node(s) |`,
        ``,
        `#### 📈 Dynamic Auto-Scaling:`,
        `- **Now**: the model above, on the GPUs contributors have lent.`,
        `- **Later**: bigger models unlock automatically as more GPUs join.`,
        ``,
        `> **Zero Configuration Needed**: Whenever nodes join or leave, DLNE automatically adapts the model to match the exact VRAM envelope—preventing out-of-memory crashes.`
      ];
      stream.markdown(lines.join('\n'));
      return { metadata: { command: 'model' } };
    }

    // 4. Build final prompt (attach active editor / diagnostics for /fix or contextual references)
    let fullPrompt = userPrompt;
    if (cmd === 'fix') {
      // The same request DLNE: Fix Current File Problems sends to the sidebar chat (chatEntry.ts).
      const fix = fixRequest(vscode.window.activeTextEditor, userPrompt);
      if ('excluded' in fix) {
        stream.markdown(fix.excluded);
        return { metadata: { command: 'fix' } };
      }
      fullPrompt = fix.prompt;
    }

    if (!fullPrompt) {
      stream.markdown(
        `Ask **@dlne** to build features, edit files, or run commands — or try \`/status\`, \`/contribute\`, \`/model\`, or \`/fix\`.`
      );
      return { metadata: { command: cmd || 'chat' } };
    }

    // What the user attached (`#file`, a selection, files dragged in) is read here, by the rules of the agent's read_file.
    const attached = await attachedContext(request.references);
    if (token.isCancellationRequested) return { metadata: { command: cmd || 'chat' } };
    for (const note of attached.skipped) stream.markdown(`${note}\n\n`);
    if (typeof stream.reference === 'function') {
      for (const used of attached.used) stream.reference(used);
    }
    fullPrompt += attached.text;

    // Each Chat thread has its own conversation (chatThreads.ts): a new thread is new memory, and /status, /model and
    // /contribute never touch the agent, so the memory reset the old shared agent needed is gone. The thread is found
    // below, by ChatThreads.run, once the transcript has been checked against it (a chat that was gone back in does not
    // get the memory of the answers that were taken out).

    let isStreamingThought = false;
    stream.progress('Connecting to DLNE Distributed Vulkan Cluster...');

    const maxTurns = vscode.workspace.getConfiguration('dlne').get<number>('agent.maxTurns');
    const approves = chatApprovesAll(request);
    // VS Code shows each progress message as a step ("Completed 1802 steps in 23m"). An update that only changes a
    // number (a growing size, a token count) is not a new step: it is passed on at most every 15 seconds.
    let lastShape = '';
    let lastProgressAt = 0;
    const progress = (text: string) => {
      const shape = text.replace(/[\d.,]+/g, '#');
      const now = Date.now();
      if (shape === lastShape && now - lastProgressAt < 15000) return;
      lastShape = shape;
      lastProgressAt = now;
      stream.progress(text);
    };
    // The model's answer, without the tool calls it writes as fenced JSON (the agent loop runs those: the chat shows each
    // as a step). Its held text is let go at the end of each step and of the run.
    const answer = new ToolJsonFilter((text) => stream.markdown(text), (name) => TOOL_NAMES.has(name));
    // The first time the agent has to ask before it acts (Auto is off), the response offers the switch.
    let autoOffered = false;
    let lookingBack = false;
    const approvals = observeApprovals(() => {
      if (autoOffered || lookingBack || approves || typeof stream.button !== 'function') return;
      autoOffered = true;
      stream.button(AUTO_BUTTON);
    });
    // The thread saves its task itself (Thread.run) and then calls onStateChanged, where the held answer text goes out.
    const run = await threads.run(threadId, chatContext?.history, fullPrompt, {
      onStateChanged: () => answer.flush(),
      onLoadingProgress: (statusText: string) => {
        // A step sent again after the connection dropped says so here: what it had streamed so far is not to be joined
        // to the new attempt's text.
        answer.flush();
        progress(statusText);
      },
      // After the answer: its questions (remember asks for a fact about the user) are not the run's, so no Auto button.
      onReviewProgress: () => { lookingBack = true; },
      // The model's thinking is not part of the answer. A line says it is thinking; its text stays out of the chat (the
      // Chat view's own thinking block is an API proposal an extension in the stores cannot use).
      onThinkingDelta: () => {
        if (isStreamingThought) return;
        isStreamingThought = true;
        progress('Thinking...');
      },
      onContentDelta: (delta: string) => {
        isStreamingThought = false;
        answer.push(delta);
      },
      onToolStart: (toolName: string, args: Record<string, any>) => {
        isStreamingThought = false;
        answer.flush();
        const target = args.path || args.command || args.query || '';
        progress(`🔧 Executing ${toolName}${target ? ` (${target})` : ''}...`);
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
            stream.markdown(diffBlock(diff));
            if (typeof stream.button === 'function') {
              stream.button({
                command: 'vscode.open',
                arguments: [fileUri],
                title: `📂 Open ${diff.path}`
              });
            }
          }
        } else if (result.imagePath) {
          // A picture from generate_image: opened with the same button a changed file gets (VS Code shows a PNG itself).
          const folders = vscode.workspace.workspaceFolders;
          stream.markdown(`\n\n🎨 **${result.output}**\n`);
          if (folders && folders.length > 0) {
            const imageUri = vscode.Uri.file(path.join(folders[0].uri.fsPath, result.imagePath));
            if (typeof stream.reference === 'function') stream.reference(imageUri);
            if (typeof stream.button === 'function') {
              stream.button({ command: 'vscode.open', arguments: [imageUri], title: `🖼 Open ${result.imagePath}` });
            }
          }
        } else if (result.terminalCommand) {
          stream.markdown(
            `\n\n💻 **Executed \`${result.terminalCommand}\`**:\n\`\`\`text\n${result.output.slice(0, 1000)}\n\`\`\`\n`
          );
        } else {
          stream.markdown(`\n\n${result.is_error ? '⚠️' : '✅'} **\`${result.name}\`**: ${result.output.slice(0, 200)}\n\n`);
        }
        // A change to the agent's memory is made at once, so it comes with a way back (docs/agent-memory.md 7.6).
        if (result.memoryChange && typeof stream.button === 'function') {
          stream.button({ command: 'dlne.memory.undo', arguments: [result.memoryChange], title: '↩ Undo' });
        }
      }
    }, { maxTurns, reflect: memoryMode(vscode.workspace.getConfiguration('dlne').get('agent.memory', 'auto')) !== 'off',
      approveAll: () => approves,
      // Stop, and a message sent during the run ("steer": VS Code sets `yieldRequested` on this request and sends the
      // message when this one has ended): the run ends after its current step and the message carries on from here.
      token, yieldRequested: () => chatContext?.yieldRequested === true, onWait: (text: string) => stream.progress(text),
      // A chat that cannot be followed starts a new conversation, and says so before anything else.
      onNote: (text: string) => stream.markdown(text),
      onThread: (found: Thread) => { ran.thread = found; } })
      .finally(() => approvals.dispose());
    const outcome = run.outcome;
    try {
      answer.flush();
    } catch {
      // The stream was closed under us (Stop): there is nobody left to show the held text to.
    }
    if (outcome === 'yielded') {
      try {
        stream.markdown('\n\n_(Stopped after this step to take your newer message.)_');
      } catch {
        // The stream was closed under us. An answer with errorDetails would keep VS Code from sending that message.
      }
    }

    // `paused`: the run stopped at its turn limit, or after a step to take a newer message; `failed`: it ended on an
    // error. The follow-ups below offer to carry on from where it stopped. `threadId`, `turn` and `mark` say which
    // conversation this answer belongs to, and where in the chat and the conversation it stands (see chatThreads.ts).
    return {
      metadata: {
        command: cmd || 'chat', paused: outcome === 'paused' || outcome === 'yielded', failed: outcome === 'error',
        threadId: run.thread.id, turn: run.turn, mark: run.mark,
      },
    };
  };

  // Every answer carries its thread's id in its result metadata, which VS Code hands back in `chatContext.history` with
  // the next message of that thread: that is how the conversation is found again, after a reload too. An answer of the
  // agent also carries its turn (its place among the chat's answers) and the mark of the conversation it left: that is
  // how a chat that was gone back in, or forked, is told from one that was only continued.
  const handler = async (request: any, chatContext: any, stream: any, token: vscode.CancellationToken) => {
    const threadId = threads.idFor(chatContext?.history);
    const ran: { thread?: Thread } = {};
    try {
      const result = await respond(request, chatContext, stream, token, threadId, ran);
      return { ...result, metadata: { threadId, ...result?.metadata } };
    } catch (err: any) {
      // Whatever went wrong, the answer still names its thread (an answer without a result loses it) and offers Continue.
      // It names the thread the request ran on, which is not the one its history named when the chat was cut back.
      const message = redactSecrets(String(err?.message ?? err)).text;
      const on = ran.thread;
      logLine(`agent thread ${(on?.id ?? threadId).slice(0, 8)}: the request failed with an exception: ${logError(err?.message ?? err)}`);
      return {
        errorDetails: { message },
        metadata: {
          command: request?.command || 'chat', failed: true, threadId: on?.id ?? threadId,
          ...(on ? { turn: readTranscript(chatContext?.history).answered + 1, mark: on.lastMark?.id } : {}),
        },
      };
    }
  };

  const participant = chatNs.createChatParticipant('dlne.agent', handler);
  participant.iconPath = vscode.Uri.joinPath(context.extensionUri, 'media', 'dlne_logo.png');

  participant.followupProvider = {
    provideFollowups(result: any, _context: any, _token: vscode.CancellationToken) {
      // A run that stopped (at its turn limit, to take a newer message, or on an error) carries on from where it stopped,
      // with everything it has read.
      const carryOn = result?.metadata?.paused || result?.metadata?.failed ? [{ prompt: 'continue', label: '▶ Continue' }] : [];
      return [
        ...carryOn,
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
}
