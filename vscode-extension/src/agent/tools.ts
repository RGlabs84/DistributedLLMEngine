import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
import * as child_process from 'child_process';
import * as os from 'os';
import { DiffManager, FileDiffData } from './diffManager';
import { ToolDefinition } from '../client/dlneClient';

export interface ToolCallResult {
  tool_call_id?: string;
  name: string;
  output: string;
  is_error?: boolean;
  fileDiff?: FileDiffData;
  terminalCommand?: string;
}

export function resolvePath(rootPath: string | undefined, targetPath: string): string {
  if (!targetPath) return rootPath || os.homedir();
  let expanded = targetPath.trim();
  if (expanded === '~' || expanded.startsWith('~/')) {
    expanded = path.join(os.homedir(), expanded.slice(expanded === '~' ? 1 : 2));
  }
  if (path.isAbsolute(expanded)) {
    return path.normalize(expanded);
  }
  if (rootPath) {
    return path.normalize(path.resolve(rootPath, expanded));
  }
  return path.normalize(path.resolve(os.homedir(), expanded));
}

/** What the user is asked to allow before a tool runs. */
export interface ApprovalRequest {
  tool: string;
  /** One line naming what will happen, e.g. "edit src/app.ts". */
  summary: string;
  /** The exact command, path or content excerpt, shown in full. */
  detail: string;
  /** Whether "allow for the rest of this session" may be offered (edits inside the workspace only). */
  sessionAllowed: boolean;
}

export type ApprovalAnswer = 'once' | 'session' | 'deny';

const ALLOW = 'Allow';
const ALLOW_SESSION = 'Allow edits for this session';

async function promptForApproval(req: ApprovalRequest): Promise<ApprovalAnswer> {
  const buttons = req.sessionAllowed ? [ALLOW, ALLOW_SESSION] : [ALLOW];
  const choice = await vscode.window.showWarningMessage(
    `DLNE agent wants to ${req.summary}`,
    { modal: true, detail: req.detail },
    ...buttons
  );
  if (choice === ALLOW_SESSION) return 'session';
  if (choice === ALLOW) return 'once';
  return 'deny';
}

/** True when `target` is the workspace root or inside it (never true with no folder open). */
function insideWorkspace(root: string | undefined, target: string): boolean {
  if (!root) return false;
  const rel = path.relative(path.resolve(root), path.resolve(target));
  return rel === '' || (!rel.startsWith('..') && !path.isAbsolute(rel));
}

export class ToolRegistry {
  private static terminal: vscode.Terminal | null = null;

  /**
   * The model's tool calls arrive over the network (and the agent reads files that may contain
   * instructions), so nothing that changes the machine or reaches outside the workspace runs
   * without the user's say-so. Tests replace `approver`.
   */
  static approver: (req: ApprovalRequest) => Promise<ApprovalAnswer> = promptForApproval;
  private static editsAllowedForSession = false;

  /** Forget "allow for this session" (called when the conversation is cleared). */
  static resetApprovals(): void {
    this.editsAllowedForSession = false;
  }

  /** null when the call may run; otherwise the refusal to hand back to the model. */
  private static async checkApproval(name: string, args: Record<string, any>, root: string | undefined): Promise<string | null> {
    const autoReads = vscode.workspace.getConfiguration('dlne').get<boolean>('autoApproveReads', true);
    let req: ApprovalRequest | null = null;

    switch (name) {
      case 'read_file':
      case 'list_directory': {
        const target = resolvePath(root, args.path || '.');
        const inside = insideWorkspace(root, target);
        if (inside && autoReads) return null;
        req = {
          tool: name,
          summary: `${name === 'read_file' ? 'read' : 'list'} ${inside ? path.relative(root!, target) || '.' : target}${inside ? '' : ' (outside your workspace)'}`,
          detail: `${target}\n\nIts contents will be sent to the DLNE coordinator as part of the conversation.`,
          sessionAllowed: false,
        };
        break;
      }
      case 'search_codebase':
      case 'get_workspace_diagnostics':
        if (autoReads) return null;
        req = { tool: name, summary: name === 'search_codebase' ? `search the workspace for "${args.query}"` : 'read the workspace diagnostics', detail: '', sessionAllowed: false };
        break;
      case 'write_file':
      case 'edit_file_diff': {
        const target = resolvePath(root, args.path);
        const inside = insideWorkspace(root, target);
        if (inside && this.editsAllowedForSession) return null;
        const excerpt = String(name === 'write_file' ? args.content ?? '' : args.replace_block ?? '').slice(0, 600);
        req = {
          tool: name,
          summary: `${name === 'write_file' ? (fs.existsSync(target) ? 'overwrite' : 'create') : 'edit'} ${inside ? path.relative(root!, target) : target}${inside ? '' : ' (OUTSIDE your workspace)'}`,
          detail: `${target}\n\n${excerpt}`,
          sessionAllowed: inside,
        };
        break;
      }
      case 'run_terminal_command':
        req = {
          tool: name,
          summary: 'run a command in your shell',
          detail: `${String(args.command ?? '')}\n\nIn: ${root || os.homedir()}`,
          sessionAllowed: false,
        };
        break;
      default:
        return null; // unknown tools fail on their own in executeToolInternal
    }

    const answer = await this.approver(req);
    if (answer === 'deny') {
      return `The user declined: ${req.summary}. Do not retry it; ask the user or choose another approach.`;
    }
    if (answer === 'session' && req.sessionAllowed) this.editsAllowedForSession = true;
    return null;
  }

  static getToolDefinitions(): ToolDefinition[] {
    return [
      {
        type: 'function',
        function: {
          name: 'read_file',
          description: 'Read the contents of a file with line numbers. Supports relative workspace paths or absolute paths.',
          parameters: {
            type: 'object',
            properties: {
              path: { type: 'string', description: 'Relative path in workspace or absolute path (e.g. src/app.ts, ~/notes.txt)' },
              start_line: { type: 'number', description: '1-based starting line number (optional)' },
              end_line: { type: 'number', description: '1-based ending line number (optional)' }
            },
            required: ['path']
          }
        }
      },
      {
        type: 'function',
        function: {
          name: 'write_file',
          description: 'Create a new file or completely overwrite an existing file with new content. Supports relative or absolute paths.',
          parameters: {
            type: 'object',
            properties: {
              path: { type: 'string', description: 'Path to the file to create or overwrite' },
              content: { type: 'string', description: 'Full text content to write' }
            },
            required: ['path', 'content']
          }
        }
      },
      {
        type: 'function',
        function: {
          name: 'edit_file_diff',
          description: 'Surgically replace a matching block of code in a file. Include 2-4 lines of surrounding context in search_block.',
          parameters: {
            type: 'object',
            properties: {
              path: { type: 'string', description: 'Path to the file to edit' },
              search_block: { type: 'string', description: 'Exact existing code block to find' },
              replace_block: { type: 'string', description: 'New code block to replace it with' }
            },
            required: ['path', 'search_block', 'replace_block']
          }
        }
      },
      {
        type: 'function',
        function: {
          name: 'list_directory',
          description: 'List the files and subdirectories in a directory. Supports relative workspace paths, home (~) or absolute paths.',
          parameters: {
            type: 'object',
            properties: {
              path: { type: 'string', description: 'Directory path (use . for root, ~ for home, or absolute path)' }
            },
            required: ['path']
          }
        }
      },
      {
        type: 'function',
        function: {
          name: 'search_codebase',
          description: 'Search workspace files for a regex or text pattern.',
          parameters: {
            type: 'object',
            properties: {
              query: { type: 'string', description: 'Text or regex pattern to search for' }
            },
            required: ['query']
          }
        }
      },
      {
        type: 'function',
        function: {
          name: 'run_terminal_command',
          description: 'Execute a shell command locally in the bash/terminal environment (e.g. bash commands, find, cp, ls, tests, git) and return stdout/stderr.',
          parameters: {
            type: 'object',
            properties: {
              command: { type: 'string', description: 'Shell command to execute' }
            },
            required: ['command']
          }
        }
      },
      {
        type: 'function',
        function: {
          name: 'get_workspace_diagnostics',
          description: 'Get active compiler, linter, and type-check errors in the workspace or specific file.',
          parameters: {
            type: 'object',
            properties: {
              file_path: { type: 'string', description: 'Optional relative file path' }
            }
          }
        }
      }
    ];
  }

  private static capOutput(result: ToolCallResult): ToolCallResult {
    const MAX_TOOL_CHARS = 8000;
    if (result.output && result.output.length > MAX_TOOL_CHARS) {
      const omitted = result.output.length - 5000;
      result.output =
        result.output.slice(0, 3500) +
        `\n\n... [Output truncated: ${omitted} characters omitted to preserve context window. Use start_line/end_line or grep to view specific sections] ...\n\n` +
        result.output.slice(-1500);
    }
    return result;
  }

  static async executeTool(name: string, args: Record<string, any>): Promise<ToolCallResult> {
    const folders = vscode.workspace.workspaceFolders;
    const root = folders && folders.length > 0 ? folders[0].uri.fsPath : undefined;
    const refusal = await this.checkApproval(name, args || {}, root);
    if (refusal) return { name, output: refusal, is_error: true };
    const result = await this.executeToolInternal(name, args);
    return this.capOutput(result);
  }

  private static async executeToolInternal(name: string, args: Record<string, any>): Promise<ToolCallResult> {
    const workspaceFolders = vscode.workspace.workspaceFolders;
    const rootPath = (workspaceFolders && workspaceFolders.length > 0) ? workspaceFolders[0].uri.fsPath : os.homedir();

    try {
      switch (name) {
        case 'read_file': {
          const filePath = resolvePath(rootPath, args.path);
          if (!fs.existsSync(filePath)) {
            return { name, output: `File not found: ${args.path} (resolved: ${filePath})`, is_error: true };
          }
          const text = fs.readFileSync(filePath, 'utf-8');
          const lines = text.split(/\r?\n/);

          const start = Math.max(1, args.start_line || 1) - 1;
          const end = args.end_line ? Math.min(lines.length, args.end_line) : lines.length;

          const numbered = lines.slice(start, end).map((line, idx) => `${start + idx + 1}: ${line}`);
          return { name, output: numbered.join('\n') };
        }

        case 'write_file': {
          const filePath = resolvePath(rootPath, args.path);
          const relPath = rootPath ? path.relative(rootPath, filePath) : filePath;

          let originalText = '';
          const exists = fs.existsSync(filePath);
          if (exists) {
            try {
              originalText = fs.readFileSync(filePath, 'utf-8');
            } catch {}
          }

          // Ensure parent directory exists
          const parentDir = path.dirname(filePath);
          if (!fs.existsSync(parentDir)) {
            fs.mkdirSync(parentDir, { recursive: true });
          }

          fs.writeFileSync(filePath, args.content, 'utf-8');

          try {
            await vscode.commands.executeCommand('workbench.files.action.refreshFilesExplorer');
          } catch {}

          const fileDiff = DiffManager.generateFileDiff(
            relPath,
            originalText,
            args.content,
            exists ? 'modified' : 'created'
          );

          return {
            name,
            output: `Successfully ${exists ? 'updated' : 'created'} file: ${relPath} (+${fileDiff.additions} lines, -${fileDiff.deletions} lines)`,
            fileDiff
          };
        }

        case 'edit_file_diff': {
          const filePath = resolvePath(rootPath, args.path);
          const relPath = rootPath ? path.relative(rootPath, filePath) : filePath;
          if (!fs.existsSync(filePath)) {
            return { name, output: `File not found: ${args.path} (resolved: ${filePath})`, is_error: true };
          }
          const originalText = fs.readFileSync(filePath, 'utf-8');

          const result = DiffManager.applyFuzzyDiff(originalText, args.search_block, args.replace_block, relPath);
          if (!result.success || !result.modifiedContent) {
            return { name, output: `Diff Error: ${result.message}`, is_error: true };
          }

          fs.writeFileSync(filePath, result.modifiedContent, 'utf-8');
          return {
            name,
            output: `Successfully patched ${relPath} (${result.message})`,
            fileDiff: result.fileDiff
          };
        }

        case 'list_directory': {
          const dirPath = resolvePath(rootPath, args.path || '.');
          if (!fs.existsSync(dirPath)) {
            return { name, output: `Directory not found: ${args.path || '.'} (resolved: ${dirPath})`, is_error: true };
          }
          const entries = fs.readdirSync(dirPath, { withFileTypes: true });
          const listing = entries.map(ent => {
            return `${ent.isDirectory() ? '[DIR] ' : '      '}${ent.name}`;
          });
          return { name, output: listing.join('\n') || 'Empty directory' };
        }

        case 'search_codebase': {
          const query = args.query;
          const uris = await vscode.workspace.findFiles('**/*', '**/node_modules/**,**/.git/**', 50);
          const matches: string[] = [];

          for (const uri of uris) {
            try {
              const data = await vscode.workspace.fs.readFile(uri);
              const text = new TextDecoder().decode(data);
              if (text.includes(query)) {
                matches.push(path.relative(rootPath, uri.fsPath));
              }
            } catch {
              // Ignore binary files
            }
          }
          return {
            name,
            output: matches.length > 0
              ? `Matches found in ${matches.length} files:\n${matches.join('\n')}`
              : `No matches found for "${query}"`
          };
        }

        case 'run_terminal_command': {
          const cmd = args.command;
          if (!cmd || typeof cmd !== 'string') {
            return { name, output: 'Error: No command provided', is_error: true };
          }

          // Echo command to VS Code terminal for transparency
          try {
            if (!this.terminal || this.terminal.exitStatus !== undefined) {
              this.terminal = vscode.window.createTerminal({ name: 'DLNE Agent Terminal' });
            }
            this.terminal.show(true);
            this.terminal.sendText(`# DLNE Agent executing: ${cmd}`);
          } catch {}

          // Execute synchronously via child_process and return actual stdout and stderr to the model
          const cwd = rootPath;
          const output = await new Promise<string>((resolve) => {
            child_process.exec(
              cmd,
              { cwd, timeout: 60000, maxBuffer: 10 * 1024 * 1024 },
              (error, stdout, stderr) => {
                const outStr = stdout ? stdout.trim() : '';
                const errStr = stderr ? stderr.trim() : '';
                if (error) {
                  const combined = [outStr, errStr, error.message].filter(Boolean).join('\n');
                  resolve(`Command failed (exit code ${error.code ?? 1}):\n${combined}`);
                } else {
                  const combined = [outStr, errStr].filter(Boolean).join('\n');
                  resolve(combined || '(Command completed successfully with no output)');
                }
              }
            );
          });

          return {
            name,
            output,
            terminalCommand: cmd
          };
        }

        case 'get_workspace_diagnostics': {
          const allDiagnostics = vscode.languages.getDiagnostics();
          const reports: string[] = [];

          for (const [uri, diags] of allDiagnostics) {
            if (args.file_path && !uri.fsPath.endsWith(args.file_path)) {
              continue;
            }
            for (const d of diags) {
              if (d.severity === vscode.DiagnosticSeverity.Error || d.severity === vscode.DiagnosticSeverity.Warning) {
                const relPath = path.relative(rootPath, uri.fsPath);
                reports.push(`[${d.severity === vscode.DiagnosticSeverity.Error ? 'ERROR' : 'WARN'}] ${relPath}:${d.range.start.line + 1}: ${d.message}`);
              }
            }
          }
          return {
            name,
            output: reports.length > 0 ? reports.join('\n') : 'No errors or warnings found.'
          };
        }

        default:
          return { name, output: `Unknown tool: ${name}`, is_error: true };
      }
    } catch (err: any) {
      return { name, output: `Tool execution failed: ${err.message || String(err)}`, is_error: true };
    }
  }
}
