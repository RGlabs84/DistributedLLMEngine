/**
 * The ways into DLNE's chat: DLNE: Open Chat, DLNE: Open Chat Panel and DLNE: Fix Current File Problems. DLNE has
 * two chats. `@dlne` in VS Code's Chat view is the main one; our own chat in the DLNE sidebar is the one for editors
 * with no Chat view, and for anyone who prefers it. These commands send you to the right one.
 */
import * as vscode from 'vscode';
import { isExcludedFile } from '../privacy';
import { pathForAgent } from './chatPaths';

/** The command that opens VS Code's Chat view (it takes `query` and `isPartialQuery`). */
export const OPEN_CHAT_COMMAND = 'workbench.action.chat.open';
/** Shows our sidebar, which holds our own chat. */
export const SIDEBAR_COMMAND = 'workbench.view.extension.dlne-sidebar';

/**
 * The editors whose Chat view is VS Code's own: VS Code itself (any channel, and in the browser) and the Code - OSS
 * builds it is made from. Other editors built on VS Code (Cursor, Windsurf, VSCodium...) have the chat API in them
 * but their own chat, or none, so they are sent to the sidebar chat.
 */
const CHAT_VIEW_HOSTS = /^(Visual Studio Code|Code - OSS)/;

/** Whether VS Code's Chat view can be opened with `@dlne` typed in. */
export async function chatViewAvailable(): Promise<boolean> {
  const chat = (vscode as any).chat;
  // The same test the participant is registered under (nativeChatParticipant.ts).
  if (!chat || typeof chat.createChatParticipant !== 'function') return false;
  if (!CHAT_VIEW_HOSTS.test(String(vscode.env?.appName ?? ''))) return false;
  // "Disable and hide built-in AI features", which takes the Chat view away.
  if (vscode.workspace.getConfiguration('chat').get<boolean>('disableAIFeatures', false) === true) return false;
  try {
    const commands = await vscode.commands.getCommands?.(true);
    if (Array.isArray(commands) && !commands.includes(OPEN_CHAT_COMMAND)) return false;
  } catch {
    return false;
  }
  return true;
}

/** DLNE: Open Chat. The Chat view with `@dlne ` typed in, where there is one; otherwise our own chat. */
export async function openChat(): Promise<void> {
  if (await chatViewAvailable()) {
    try {
      await vscode.commands.executeCommand(OPEN_CHAT_COMMAND, { query: '@dlne ', isPartialQuery: true });
      return;
    } catch {
      // the Chat view did not open: our own chat is the way in
    }
  }
  await openChatPanel();
}

/** DLNE: Open Chat Panel. Always our own chat, in the sidebar. */
export async function openChatPanel(): Promise<void> {
  await vscode.commands.executeCommand(SIDEBAR_COMMAND);
}

/** What `@dlne /fix` asks the agent: refused for a private file, otherwise the prompt. */
export type FixRequest = { excluded: string } | { prompt: string };

/**
 * The request for fixing the file in `editor` (the one `@dlne /fix` and DLNE: Fix Current File Problems both send): the
 * file's name and its errors, not its text, which the agent reads itself (through the same privacy rules). A private
 * file is refused (a cell of a private notebook is judged as the notebook). The file is named the way an attachment is,
 * without this machine's user name: relative to the workspace, or `~/...` under the home folder. With no editor the
 * prompt is the user's own words, or a general one.
 */
export function fixRequest(editor: vscode.TextEditor | undefined, userPrompt: string): FixRequest {
  if (editor && isExcludedFile(editor.document.uri)) {
    return {
      excluded: `\`${vscode.workspace.asRelativePath(editor.document.uri)}\` is excluded for privacy (a credentials file, or ignored by .gitignore or .dlneignore), so DLNE does not read it or send it anywhere.`,
    };
  }
  if (editor) {
    const relPath = pathForAgent(editor.document.uri);
    const diags = vscode.languages
      .getDiagnostics(editor.document.uri)
      .filter((d) => d.severity === vscode.DiagnosticSeverity.Error);
    const diagText = diags.map((d) => `Line ${d.range.start.line + 1}: ${d.message}`).join('\n');
    return {
      prompt:
        `Fix the issues in \`${relPath}\`.\n` +
        (diagText ? `Diagnostics:\n${diagText}\n` : '') +
        (userPrompt ? `Additional instructions: ${userPrompt}` : ''),
    };
  }
  return { prompt: userPrompt || 'Inspect the workspace for issues and fix them.' };
}

/** What the sidebar provider offers a command: send a message into the sidebar chat, once its view is open. */
export interface SidebarChat {
  sendFromCommand(text: string): Promise<void>;
}

/**
 * DLNE: Fix Current File Problems. Sends the active file's errors to the agent: as `@dlne /fix` in the Chat view
 * where there is one (the participant builds the request from the file open in the editor), otherwise as a message
 * in the sidebar chat. It used to open the sidebar and say "Sending…" without sending anything.
 */
export async function fixDiagnostics(sidebar: SidebarChat): Promise<void> {
  const editor = vscode.window.activeTextEditor;
  if (!editor) {
    vscode.window.showWarningMessage('No active editor open.');
    return;
  }
  const request = fixRequest(editor, '');
  if ('excluded' in request) {
    vscode.window.showWarningMessage(request.excluded.replace(/`/g, ''));
    return;
  }
  const errors = vscode.languages
    .getDiagnostics(editor.document.uri)
    .filter((d) => d.severity === vscode.DiagnosticSeverity.Error);
  if (errors.length === 0) {
    vscode.window.showInformationMessage('No errors found in active file.');
    return;
  }
  if (await chatViewAvailable()) {
    try {
      await vscode.commands.executeCommand(OPEN_CHAT_COMMAND, { query: '@dlne /fix', isPartialQuery: false });
      return;
    } catch {
      // the Chat view did not open: the sidebar chat takes it
    }
  }
  await openChatPanel();
  await sidebar.sendFromCommand(request.prompt);
}
