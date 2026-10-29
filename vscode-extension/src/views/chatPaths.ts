/**
 * How the agent is told a file's name. What the user attaches (chatReferences.ts) and the request `/fix` makes
 * (chatEntry.ts) both put a file's name into text that goes to the model, which may be on another person's machine, so
 * the name never carries this machine's user name. Which file a URI stands for is decided in one place, privacy/exclude.ts
 * (`filePathOf`), because the privacy rules are judged on that file too.
 */
import * as vscode from 'vscode';
import * as os from 'os';
import * as path from 'path';
import { filePathOf } from '../privacy';

export interface UriLike {
  scheme: string;
  fsPath?: string;
  path?: string;
}

/** `target` relative to `folder` with forward slashes, or null when it is not inside it. */
function insideFolder(folder: string | undefined, target: string): string | null {
  if (!folder) return null;
  const relative = path.relative(folder, target);
  return relative && !relative.startsWith('..') && !path.isAbsolute(relative) ? relative.split(path.sep).join('/') : null;
}

/**
 * The name the agent's tools take for a file, without this machine's user name in it: inside the workspace relative to
 * it, under the home folder as `~/...` (read_file takes that), otherwise the path as it is. A URI with no file behind it
 * is named by its last part only (`Untitled-1`), never by a path.
 */
export function pathForAgent(uri: UriLike): string {
  const file = filePathOf(uri);
  if (file === undefined) return path.posix.basename(uri.path ?? '') || uri.scheme;
  const inWorkspace = insideFolder(vscode.workspace.workspaceFolders?.[0]?.uri?.fsPath, file);
  if (inWorkspace) return inWorkspace;
  const underHome = insideFolder(os.homedir(), file);
  return underHome ? `~/${underHome}` : file;
}
