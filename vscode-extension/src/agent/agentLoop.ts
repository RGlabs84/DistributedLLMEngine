import { DlneClient, ChatMessage } from '../client/dlneClient';
import { ToolRegistry } from './tools';
import { CPU_POOL_PERK } from '../worker/contribution';

export interface AgentCallbacks {
  onThinkingDelta: (delta: string) => void;
  onContentDelta: (delta: string) => void;
  onToolStart: (toolName: string, args: Record<string, any>) => void;
  onToolEnd: (result: any) => void;
  onLoadingProgress?: (statusText: string) => void;
  onError?: (error: any) => void;
}

const VALID_TOOLS = new Set([
  'write_file',
  'edit_file_diff',
  'read_file',
  'run_terminal_command',
  'list_directory',
  'search_codebase',
  'get_workspace_diagnostics'
]);

/**
 * What the chat shows when a request fails. A refusal the coordinator explains (a CPU
 * contributor's usage window, "frees up in 42 minutes", or what a machine lacks to contribute
 * its CPU) is shown in its own words; only a spent free trial gets the "contribute" guide.
 */
export function refusalNotice(status: number | undefined, errMsg: string): string {
  const trialUsedUp = /starter quota/i.test(errMsg) || errMsg.includes('NON-CONTRIB')
    || (status === undefined && errMsg.includes('403'));
  if (status === 429 || (status === 403 && !trialUsedUp)) {
    return `\n\n${status === 429 ? '⏳' : '🔒'} ${errMsg}`;
  }
  if (trialUsedUp) {
    return '\n\n🔒 **Your free trial is used up**\n\n' +
      'DLNE is free forever, on one rule: *give compute to receive compute*. Share your idle GPU with the swarm to keep going.\n\n' +
      `- No GPU? Lend your CPU: it powers the DLNE phone app, and you get ${CPU_POOL_PERK}.\n` +
      '- Click the **DLNE status bar item** (or run **`@dlne /contribute`**) and choose **Start Contributing Local Compute**.\n' +
      '- Or set an authorized API key in the `dlne.apiKey` setting.\n' +
      '- Once your worker connects, resend your prompt to continue.';
  }
  return '\n\nError connecting to DLNE cluster: ' + errMsg;
}

export class AgentLoop {
  private messages: ChatMessage[] = [];
  private abortController: AbortController | null = null;

  constructor(private client: DlneClient) {
    this.initSystemPrompt();
  }

  private initSystemPrompt() {
    this.messages = [
      {
        role: 'system',
        content: `You are the DLNE coding agent, a software engineering assistant working inside the user's VS Code workspace.

Use your tools to do the work rather than telling the user to do it:
- read_file(path, start_line, end_line), list_directory(path), search_codebase(query), get_workspace_diagnostics(file_path): look before you change anything.
- edit_file_diff(path, search_block, replace_block): change part of a file; search_block must match the file exactly, with 2-4 lines of context.
- write_file(path, content): create a file, or replace one entirely.
- run_terminal_command(command): run a shell command in the workspace (tests, builds, git).

Paths are relative to the workspace root. The user approves edits, commands, and anything outside the workspace; if a tool result says the user declined, do not retry it - ask or take another approach. After acting, check the result and say briefly what you changed.

If your output cannot carry native tool calls, emit one as a JSON block:
\`\`\`json
{"name": "read_file", "arguments": {"path": "src/index.ts"}}
\`\`\`

Begin with a short <think> plan.`
      }
    ];
  }

  public clearHistory() {
    this.initSystemPrompt();
    ToolRegistry.resetApprovals();
  }

  public abort() {
    if (this.abortController) {
      this.abortController.abort();
      this.abortController = null;
    }
  }

  public estimateMessageTokens(msg: ChatMessage): number {
    let tokens = Math.ceil((msg.content?.length || 0) / 3.2);
    if (msg.tool_calls && Array.isArray(msg.tool_calls)) {
      for (const tc of msg.tool_calls) {
        tokens += Math.ceil((tc.function?.name?.length || 0) / 3.2);
        tokens += Math.ceil((tc.function?.arguments?.length || 0) / 3.2);
      }
    }
    return tokens + 4;
  }

  public estimateTotalTokens(): number {
    return this.messages.reduce((sum, msg) => sum + this.estimateMessageTokens(msg), 0);
  }

  public compactHistoryIfNeeded(maxPromptTokens: number = 11000): void {
    if (this.messages.length <= 3) {
      return;
    }

    // Under budget, the history is left exactly as it was sent last time. The engine keeps the
    // KV cache of the longest prefix it has already seen, so an append-only prompt only pays
    // for the new tokens; rewriting an early message (as steps 1-2 do) makes it re-read
    // everything after that message, seconds per turn on a long agent run.
    if (this.estimateTotalTokens() <= maxPromptTokens) {
      return;
    }

    // Step 1: Strip older <think> blocks from previous assistant messages
    // Completed thinking traces are scratchpads only needed during generation.
    let lastAssistantIdx = -1;
    for (let i = this.messages.length - 1; i >= 0; i--) {
      if (this.messages[i].role === 'assistant') {
        lastAssistantIdx = i;
        break;
      }
    }

    for (let i = 0; i < this.messages.length; i++) {
      const msg = this.messages[i];
      if (msg.role === 'assistant' && i !== lastAssistantIdx && msg.content) {
        const cleaned = msg.content.replace(/<think>[\s\S]*?<\/think>/g, '').trim();
        if (cleaned) {
          msg.content = cleaned;
        }
      }
    }

    // Step 2: Prune older tool observations (> 4 messages away from active end)
    const recentThreshold = Math.max(2, this.messages.length - 4);
    for (let i = 1; i < recentThreshold; i++) {
      const msg = this.messages[i];
      if (msg.role === 'tool' && msg.content && msg.content.length > 300) {
        msg.content =
          msg.content.slice(0, 150) +
          `\n... [Earlier tool output compacted: ${msg.content.length - 250} chars omitted] ...\n` +
          msg.content.slice(-100);
      } else if (
        msg.role === 'user' &&
        msg.content &&
        msg.content.includes('Observation from tool [') &&
        msg.content.length > 400
      ) {
        msg.content =
          msg.content.slice(0, 200) +
          `\n... [Earlier tool observations compacted: ${msg.content.length - 300} chars omitted] ...\n` +
          msg.content.slice(-100);
      }
    }

    // Step 3: Check total tokens
    let totalTokens = this.estimateTotalTokens();
    if (totalTokens <= maxPromptTokens) {
      return;
    }

    // Step 4: Sliding window compaction of intermediate turns
    const keepTail = 6;
    if (this.messages.length > 2 + keepTail) {
      const middleStart = 2;
      const middleEnd = this.messages.length - keepTail;
      const middleMessages = this.messages.slice(middleStart, middleEnd);

      const toolsExecuted: string[] = [];
      for (const m of middleMessages) {
        if (m.role === 'tool' && m.name) {
          toolsExecuted.push(m.name);
        } else if (m.role === 'assistant' && m.tool_calls) {
          for (const tc of m.tool_calls) {
            if (tc.function?.name) toolsExecuted.push(tc.function.name);
          }
        }
      }

      const tailFirstRole = this.messages[middleEnd].role;
      const summaryRole = tailFirstRole === 'assistant' ? 'user' : 'assistant';
      const summaryText =
        `[Context Compaction: Compacted ${middleMessages.length} intermediate execution steps to preserve context headroom]:\n` +
        `- Completed operations: ${toolsExecuted.length > 0 ? Array.from(new Set(toolsExecuted)).slice(0, 6).join(', ') : 'file and system operations'}.\n` +
        `Proceeding with next active steps toward user objective.`;

      const summaryMessage: ChatMessage = {
        role: summaryRole as any,
        content: summaryText
      };

      this.messages = [
        this.messages[0],
        this.messages[1],
        summaryMessage,
        ...this.messages.slice(middleEnd)
      ];
    }

    // Step 5: Aggressively cap any individually oversized message
    totalTokens = this.estimateTotalTokens();
    if (totalTokens > maxPromptTokens) {
      for (let i = 1; i < this.messages.length; i++) {
        const msg = this.messages[i];
        if (msg.content && msg.content.length > 2500) {
          msg.content =
            msg.content.slice(0, 1400) +
            `\n... [Message trimmed to prevent context overflow: ${msg.content.length - 2200} chars omitted] ...\n` +
            msg.content.slice(-800);
        }
      }
    }
  }

  public emergencyPrune(): void {
    if (this.messages.length <= 2) return;
    const system = this.messages[0];
    const initialUser = this.messages[1];
    const lastMsg = this.messages[this.messages.length - 1];
    const cleanLastContent = (lastMsg?.content || '')
      .replace(/\[System Alert\][\s\S]*?increasing it\./g, '')
      .trim();

    this.messages = [
      system,
      initialUser,
      {
        role: 'assistant',
        content: '[Context auto-pruned to clear slot memory limit. Resuming autonomous execution.]'
      }
    ];

    if (cleanLastContent && lastMsg && lastMsg.role !== 'assistant') {
      this.messages.push({
        role: lastMsg.role,
        content: cleanLastContent
      });
    }
  }

  public async run(userPrompt: string, callbacks: AgentCallbacks): Promise<void> {
    this.abort();
    this.abortController = new AbortController();

    this.messages.push({ role: 'user', content: userPrompt });
    const tools = ToolRegistry.getToolDefinitions();

    let turns = 0;
    const maxTurns = 50;
    let contextOverflowRetries = 0;

    while (turns < maxTurns) {
      turns++;
      if (this.abortController.signal.aborted) {
        break;
      }

      // Preemptive Context Compaction: ensure message history stays well within slot token limit (11000 max)
      this.compactHistoryIfNeeded(11000);

      let inThinking = false;
      let inToolTag = false;
      let activeClosingTag = '';
      let streamedRaw = '';
      let buffer = '';
      let streamResult = { content: '', toolCalls: [] as any[] };

      try {
        streamResult = await this.client.streamChat(
          this.messages,
          tools,
          (delta: string, reasoningDelta?: string) => {
            if (reasoningDelta) {
              callbacks.onThinkingDelta(reasoningDelta);
            }
            if (!delta) return;

            // Suppress internal engine context overflow alerts from leaking into user chat stream
            if (delta.includes('[System Alert]') && delta.includes('context size')) {
              return;
            }

            streamedRaw += delta;
            buffer += delta;

            while (buffer.length > 0) {
              if (inThinking) {
                const thinkEnd = buffer.indexOf('</think>');
                if (thinkEnd !== -1) {
                  const thinkChunk = buffer.slice(0, thinkEnd);
                  if (thinkChunk) callbacks.onThinkingDelta(thinkChunk);
                  inThinking = false;
                  buffer = buffer.slice(thinkEnd + 8);
                } else {
                  if ('</think>'.startsWith(buffer) && buffer !== '<') {
                    break;
                  }
                  callbacks.onThinkingDelta(buffer);
                  buffer = '';
                }
              } else if (inToolTag) {
                const closeIdx = buffer.indexOf(activeClosingTag);
                if (closeIdx !== -1) {
                  inToolTag = false;
                  buffer = buffer.slice(closeIdx + activeClosingTag.length);
                  activeClosingTag = '';
                } else {
                  if (activeClosingTag.startsWith(buffer) && buffer !== '<') {
                    break;
                  }
                  buffer = '';
                }
              } else {
                const thinkStart = buffer.indexOf('<think>');
                const toolsStart = buffer.indexOf('<tools>');
                const toolCallStart = buffer.indexOf('<tool_call>');
                const funcCallStart = buffer.indexOf('<function-call>');

                type TagMatch = { index: number; tag: string; closing: string; type: 'think' | 'tool' };
                const candidates: TagMatch[] = [];
                if (thinkStart !== -1) candidates.push({ index: thinkStart, tag: '<think>', closing: '</think>', type: 'think' });
                if (toolsStart !== -1) candidates.push({ index: toolsStart, tag: '<tools>', closing: '</tools>', type: 'tool' });
                if (toolCallStart !== -1) candidates.push({ index: toolCallStart, tag: '<tool_call>', closing: '</tool_call>', type: 'tool' });
                if (funcCallStart !== -1) candidates.push({ index: funcCallStart, tag: '<function-call>', closing: '</function-call>', type: 'tool' });

                if (candidates.length > 0) {
                  candidates.sort((a, b) => a.index - b.index);
                  const match = candidates[0];
                  const textBefore = buffer.slice(0, match.index);
                  if (textBefore) {
                    callbacks.onContentDelta(textBefore);
                  }
                  buffer = buffer.slice(match.index + match.tag.length);
                  if (match.type === 'think') {
                    inThinking = true;
                  } else {
                    inToolTag = true;
                    activeClosingTag = match.closing;
                  }
                } else {
                  const prefixes = ['<think>', '<tools>', '<tool_call>', '<function-call>'];
                  let matchedPrefix = false;
                  for (const p of prefixes) {
                    for (let len = 1; len < p.length; len++) {
                      if (buffer.endsWith(p.slice(0, len))) {
                        const safeLen = buffer.length - len;
                        if (safeLen > 0) {
                          callbacks.onContentDelta(buffer.slice(0, safeLen));
                          buffer = buffer.slice(safeLen);
                        }
                        matchedPrefix = true;
                        break;
                      }
                    }
                    if (matchedPrefix) break;
                  }
                  if (!matchedPrefix) {
                    callbacks.onContentDelta(buffer);
                    buffer = '';
                  } else {
                    break;
                  }
                }
              }
            }
          },
          undefined,
          this.abortController.signal,
          callbacks.onLoadingProgress
        );

        if (inThinking && buffer) {
          callbacks.onThinkingDelta(buffer);
        } else if (!inThinking && !inToolTag && buffer) {
          callbacks.onContentDelta(buffer);
        }
      } catch (err: any) {
        const errMsg = err?.message || String(err);
        if (errMsg.includes('exceeds the available context size') || errMsg.includes('context size')) {
          if (contextOverflowRetries < 3) {
            contextOverflowRetries++;
            if (callbacks.onLoadingProgress) {
              callbacks.onLoadingProgress('⚠️ Context window limit reached. Compacting history and retrying...');
            }
            this.emergencyPrune();
            continue;
          }
        }
        if (callbacks.onError) {
          callbacks.onError(err);
        }
        callbacks.onContentDelta(refusalNotice((err as { status?: number })?.status, errMsg));
        return;
      }

      // Check if server returned a context overflow alert inside streamResult
      const isContextAlert =
        streamResult.content.includes('exceeds the available context size') ||
        streamedRaw.includes('exceeds the available context size') ||
        (streamResult.content.includes('[System Alert]') && streamResult.content.includes('context size'));

      if (isContextAlert) {
        if (contextOverflowRetries < 3) {
          contextOverflowRetries++;
          if (callbacks.onLoadingProgress) {
            callbacks.onLoadingProgress('⚠️ Context window limit reached. Compacting conversation history and retrying step...');
          }
          this.emergencyPrune();
          continue;
        } else {
          callbacks.onContentDelta(
            '\n\n⚠️ **Context limit reached.** The accumulated history exceeded the engine slot capacity.\n' +
            '👉 Run **`@dlne /clear`** to reset the agent history and continue.'
          );
          return;
        }
      }

      // 1. Check if native OpenAI tool calls were streamed
      if (streamResult.toolCalls && streamResult.toolCalls.length > 0) {
        const validatedToolCalls: any[] = [];
        const toolObservationMessages: any[] = [];

        for (const tc of streamResult.toolCalls) {
          const args = this.repairAndParseArgs(tc.arguments);
          const cleanArguments = JSON.stringify(args);
          const callId = tc.id || `call_${Date.now()}`;

          validatedToolCalls.push({
            id: callId,
            type: 'function',
            function: {
              name: tc.name,
              arguments: cleanArguments
            }
          });

          callbacks.onToolStart(tc.name, args);
          const result = await ToolRegistry.executeTool(tc.name, args);
          callbacks.onToolEnd(result);

          toolObservationMessages.push({
            role: 'tool',
            tool_call_id: callId,
            name: tc.name,
            content: result.output
          });
        }

        this.messages.push({
          role: 'assistant',
          content: streamResult.content || '',
          tool_calls: validatedToolCalls
        });

        for (const obs of toolObservationMessages) {
          this.messages.push(obs);
        }

        continue;
      }

      // 2. Fallback: Check for inline JSON / <tool_call> / <tools> / <function-call> in content
      const bodyWithoutThink = streamedRaw.replace(/<think>[\s\S]*?<\/think>/g, '').trim();
      const inlineCalls = this.parseInlineToolCalls(bodyWithoutThink);
      if (inlineCalls.length > 0) {
        this.messages.push({ role: 'assistant', content: streamedRaw });
        const observations: string[] = [];

        for (const call of inlineCalls) {
          callbacks.onToolStart(call.name, call.args);
          const result = await ToolRegistry.executeTool(call.name, call.args);
          callbacks.onToolEnd(result);
          observations.push(`Observation from tool [${call.name}]:\n${result.output}`);
        }

        this.messages.push({
          role: 'user',
          content: observations.join('\n\n') + '\n\nContinue with the next step or summarize your completed changes.'
        });
        continue;
      }

      // Plain message with no tool calls -> turn complete
      this.messages.push({ role: 'assistant', content: streamedRaw });
      break;
    }
  }

  private repairAndParseArgs(rawArgs: string | Record<string, any>): Record<string, any> {
    if (!rawArgs) return {};
    if (typeof rawArgs === 'object' && rawArgs !== null) {
      return rawArgs;
    }
    try {
      return JSON.parse(rawArgs);
    } catch {
      let repaired = String(rawArgs).trim();
      if (!repaired.endsWith('"}')) {
        if (repaired.includes('"content":') && !repaired.endsWith('"')) {
          repaired += '"';
        }
        if (!repaired.endsWith('}')) {
          repaired += '}';
        }
      }
      try {
        return JSON.parse(repaired);
      } catch {
        const args: Record<string, any> = {};
        const matchPath = rawArgs.match(/"path"\s*:\s*"([^"]+)"/);
        const matchQuery = rawArgs.match(/"query"\s*:\s*"([^"]+)"/);
        const matchCommand = rawArgs.match(/"command"\s*:\s*"([^"]+)"/);
        const matchContent = rawArgs.match(/"content"\s*:\s*"([\s\S]*)/);
        if (matchPath) args.path = matchPath[1];
        if (matchQuery) args.query = matchQuery[1];
        if (matchCommand) args.command = matchCommand[1];
        if (matchContent) {
          args.content = matchContent[1]
            .replace(/\\n/g, '\n')
            .replace(/\\"/g, '"')
            .replace(/"\s*\}?\s*$/, '');
        }
        return args;
      }
    }
  }

  private parseInlineToolCalls(raw: string): Array<{ name: string; args: Record<string, any> }> {
    const calls: Array<{ name: string; args: Record<string, any> }> = [];

    // Format 1: <tool_call> ... </tool_call> OR <tools> ... </tools> OR <function-call> ... </function-call>
    const tagRegex = /<(?:tool_call|tools|function-call)>\s*([\s\S]*?)\s*<\/(?:tool_call|tools|function-call)>/g;
    let match: RegExpExecArray | null;
    while ((match = tagRegex.exec(raw)) !== null) {
      const inner = match[1].trim();
      const parsed = this.tryExtractJsonTool(inner);
      if (parsed) {
        calls.push(parsed);
      } else {
        const jsonBlocks = inner.match(/\{[\s\S]*?\}(?=\s*\{|\s*$)/g);
        if (jsonBlocks) {
          for (const b of jsonBlocks) {
            const p = this.tryExtractJsonTool(b);
            if (p) calls.push(p);
          }
        }
      }
    }
    if (calls.length > 0) return calls;

    // Format 2: ```(?:json|tool_call)?\s*(\{[\s\S]*?\})\s*```
    const fencedRegex = /```(?:json|tool_call)?\s*(\{[\s\S]*?\})\s*```/g;
    while ((match = fencedRegex.exec(raw)) !== null) {
      const parsed = this.tryExtractJsonTool(match[1]);
      if (parsed) calls.push(parsed);
    }
    if (calls.length > 0) return calls;

    // Format 3: <tool_call name="..."> <param name="...">...</param> </tool_call>
    const xmlMatch = raw.match(/<tool_call\s+name=["']([^"']+)["']>([\s\S]*?)<\/tool_call>/);
    if (xmlMatch) {
      const toolName = xmlMatch[1].trim();
      const body = xmlMatch[2];
      const args: Record<string, any> = {};
      const paramRegex = /<param\s+name=["']([^"']+)["']>([\s\S]*?)<\/param>/g;
      let pm: RegExpExecArray | null;
      while ((pm = paramRegex.exec(body)) !== null) {
        args[pm[1].trim()] = pm[2].trim();
      }
      if (VALID_TOOLS.has(toolName)) {
        calls.push({ name: toolName, args });
        return calls;
      }
    }

    // Format 4: Bare JSON object {"name": "run_terminal_command", ...}
    const bareMatch = raw.match(/(\{\s*"name"\s*:\s*"(?:write_file|edit_file_diff|read_file|run_terminal_command|list_directory|search_codebase|get_workspace_diagnostics)"[\s\S]*\})/);
    if (bareMatch) {
      const parsed = this.tryExtractJsonTool(bareMatch[1]);
      if (parsed) calls.push(parsed);
    }

    return calls;
  }

  private tryExtractJsonTool(jsonCandidate: string): { name: string; args: Record<string, any> } | null {
    try {
      const obj = JSON.parse(jsonCandidate);
      const name = obj.name || obj.tool || obj.function?.name;
      if (name && VALID_TOOLS.has(name)) {
        let args = obj.arguments ?? obj.parameters ?? obj.args ?? obj.function?.arguments;
        if (!args) {
          // Flattened format: { "name": "run_terminal_command", "command": "..." }
          const copy = { ...obj };
          delete copy.name;
          delete copy.tool;
          delete copy.function;
          args = copy;
        }
        if (typeof args === 'string') {
          args = this.repairAndParseArgs(args);
        }
        return { name, args: args || {} };
      }
    } catch {
      const nameMatch = jsonCandidate.match(/"name"\s*:\s*"([^"]+)"/);
      if (nameMatch && VALID_TOOLS.has(nameMatch[1])) {
        const name = nameMatch[1];
        const argsMatch = jsonCandidate.match(/"(?:arguments|parameters|args)"\s*:\s*(\{[\s\S]*)/);
        const args = argsMatch ? this.repairAndParseArgs(argsMatch[1]) : {};
        return { name, args };
      }
    }
    return null;
  }
}
