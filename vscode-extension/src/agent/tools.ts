import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
import * as child_process from 'child_process';
import { forgetSudoPassword, withAskpass } from './askpass';
import * as os from 'os';
import { getSystemErrorName } from 'util';
import { DiffManager, FileDiffData } from './diffManager';
import { allowedAlways, alwaysPrefix } from './commandRules';
import { ToolDefinition } from '../client/dlneClient';
import { MemoryChange, MemoryMode, checkRemember, memoryMode, rememberFact, riskyFact } from './memory';
import { SkillChange, checkSaveSkill, saveSkill, useSkill } from './skills';
import { ImageApi, ImageArgs, prepareImageArgs, runImageJob, savedText } from './imageTool';
import { REDACTION_MARK, excludedNotice, excludedReason, isExcludedFile, redactSecrets, redactionNote } from '../privacy';
import { isPastedFile } from './pasted';
import { PictureAttachment, pictureFromBytes } from '../client/pictures';
import { VIEW_IMAGE_TOOL_NAME } from './viewImage';

export interface ToolCallResult {
  tool_call_id?: string;
  name: string;
  output: string;
  is_error?: boolean;
  fileDiff?: FileDiffData;
  terminalCommand?: string;
  /** What `remember` changed in a memory file: the chats offer Undo for it (docs/agent-memory.md 7.6). */
  /** A long fact saved in parts (splitFact) is several changes, undone together. A saved skill is one SkillChange (skills.ts). */
  memoryChange?: MemoryChange | MemoryChange[] | SkillChange;
  /** The workspace-relative path of a picture `generate_image` saved: the chats offer to open it. */
  imagePath?: string;
  /** The picture `view_image` opened: the loop puts it in the user's side of the next request (tool results stay text). */
  image?: PictureAttachment;
}

/** What a tool call may need from the run it belongs to; only generate_image uses it. */
export interface ToolContext {
  /** The coordinator's image endpoints. Absent: the tool is not offered, and a call to it is refused. */
  images?: ImageApi;
  /** The coordinator says its engine can read pictures (`capabilities.vision`): view_image is offered, and works. */
  vision?: boolean;
  /** The run was stopped: a running image job is cancelled at the coordinator. */
  signal?: AbortSignal;
  /** The status line while a long tool runs. */
  onProgress?: (text: string) => void;
  /** Replaces the 2 s wait between two polls of an image job (tests). */
  sleep?: (ms: number, signal?: AbortSignal) => Promise<void>;
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

/** `text` with this machine's workspace and home folders replaced by `<workspace>` and `~`. */
export function withoutLocalPaths(text: string, workspace: string | undefined): string {
  let out = String(text);
  for (const [folder, label] of [[workspace, '<workspace>'], [os.homedir(), '~']] as Array<[string | undefined, string]>) {
    if (folder && folder.length > 1) {
      out = out.split(folder).join(label);
    }
  }
  return out;
}

/** What a tool call comes to when Stop was pressed before it ran (the loop answers the rest of a reply with the same words). */
export const STOPPED_NOTICE = 'Not run: the user stopped the task.';

const STOPPED = Symbol('stopped');

/** `pending`'s answer, or STOPPED as soon as `signal` aborts (the wait for `pending` goes on unseen, and its answer is dropped). */
function untilStopped<T>(pending: Promise<T>, signal?: AbortSignal): Promise<T | typeof STOPPED> {
  if (!signal) return pending;
  return new Promise((resolve, reject) => {
    if (signal.aborted) return resolve(STOPPED);
    const onAbort = () => resolve(STOPPED);
    signal.addEventListener('abort', onAbort, { once: true });
    pending.then(
      (value) => { signal.removeEventListener('abort', onAbort); resolve(value); },
      (err) => { signal.removeEventListener('abort', onAbort); reject(err); }
    );
  });
}

/** Whether the process group `pgid` has a member (signal 0 sends nothing: it only asks). EPERM is a member we may not signal. */
function groupAlive(pgid: number): boolean {
  try {
    process.kill(-pgid, 0);
    return true;
  } catch (err) {
    return (err as NodeJS.ErrnoException).code === 'EPERM';
  }
}

function signalGroup(pgid: number, signal: NodeJS.Signals): void {
  try {
    process.kill(-pgid, signal);
  } catch {
    // the last member has just gone
  }
}

/**
 * Ends a command and what it started. Only ever signals what it can show is the command's:
 *  - POSIX: the shell was started as the leader of a process group of its own (runShell), and everything the command
 *    starts joins that group, backgrounded jobs (`npm run dev &`) included. The group is signalled, never a single process
 *    number: the shell may be long gone (a background job holds the output open and the callback waits), and its number
 *    may have been given to something else, but a group number is not reused while the group has a member. With no
 *    member left there is nobody to signal. SIGTERM first; what ignored it gets SIGKILL after `graceMs`, to the same
 *    group and only if it still has a member.
 *  - Windows (no groups): `taskkill /T` follows the tree of the shell, so it is asked only for a shell that is still running;
 *    Node keeps the handle of a process it has not seen exit, so its number cannot have been reused. One that has exited
 *    has no tree to follow, and its number may be someone else's by now.
 */
export function endCommand(child: Pick<child_process.ChildProcess, 'pid' | 'exitCode' | 'signalCode'>, graceMs = 1500): void {
  const pid = child.pid;
  if (!pid || pid <= 0) return;
  if (process.platform === 'win32') {
    if (child.exitCode !== null || child.signalCode !== null) return;
    child_process.execFile('taskkill', ['/pid', String(pid), '/T', '/F'], { windowsHide: true }, () => undefined);
    return;
  }
  if (!groupAlive(pid)) return;
  signalGroup(pid, 'SIGTERM');
  setTimeout(() => {
    if (groupAlive(pid)) signalGroup(pid, 'SIGKILL');
  }, graceMs).unref();
}

/** What a shell command came to, as child_process.exec reports it: its error, with the same fields. */
export interface ShellFailure extends Error {
  code?: number | string | null;
  killed?: boolean;
  signal?: NodeJS.Signals | null;
  cmd?: string;
}

/** A shell command that is running (runShell). */
export interface ShellRun {
  readonly child: child_process.ChildProcess;
  /** Stop: the command and everything it started end now (endCommand), and the callback does not wait for pipes a job holds open. */
  stop(): void;
}

/**
 * Runs `command` in the user's shell exactly as child_process.exec does (the same shell, output, errors, buffer limit, and
 * the same kill of the shell at `timeout`), except that on POSIX the shell leads a process group of its own, so that Stop can
 * end everything the command started (endCommand). exec cannot do that: it has no `detached`. The callback gets exec's
 * arguments; only the shell is signalled at the timeout, as before (a background job a command leaves running is its own
 * business; a Stop is the user's word that it is not).
 */
export function runShell(
  command: string,
  options: { cwd?: string; env?: NodeJS.ProcessEnv; timeout?: number; maxBuffer?: number },
  done: (error: ShellFailure | null, stdout: string, stderr: string) => void
): ShellRun {
  const maxBuffer = options.maxBuffer ?? 1024 * 1024;
  const child = child_process.spawn(command, { cwd: options.cwd, env: options.env, shell: true, detached: process.platform !== 'win32' });
  const out: string[] = [];
  const err: string[] = [];
  let outLength = 0;
  let errLength = 0;
  let killed = false;
  let exited = false;
  let failure: ShellFailure | null = null;
  let timer: ReturnType<typeof setTimeout> | undefined;

  const closePipes = () => {
    child.stdout?.destroy();
    child.stderr?.destroy();
  };
  const finish = (code?: number | null, signal?: NodeJS.Signals | null) => {
    if (exited) return;
    exited = true;
    if (timer) clearTimeout(timer);
    const stdout = out.join('');
    const stderr = err.join('');
    if (!failure && code === 0 && signal === null) {
      done(null, stdout, stderr);
      return;
    }
    failure ||= Object.assign(new Error(`Command failed: ${command}\n${stderr}`), {
      code: typeof code === 'number' && code < 0 ? getSystemErrorName(code) : code,
      killed: child.killed || killed,
      signal,
    }) as ShellFailure;
    failure.cmd = command;
    done(failure, stdout, stderr);
  };
  const killShell = () => {
    closePipes();
    killed = true;
    try {
      child.kill('SIGTERM');
    } catch (e) {
      failure = e as ShellFailure;
      finish();
    }
  };
  const collect = (stream: 'stdout' | 'stderr') => (chunk: string) => {
    const kept = stream === 'stdout' ? out : err;
    const bytes = Buffer.byteLength(chunk);
    const length = stream === 'stdout' ? (outLength += bytes) : (errLength += bytes);
    if (length > maxBuffer) {
      kept.push(chunk.slice(0, maxBuffer - (length - bytes)));
      failure = Object.assign(new RangeError(`${stream} maxBuffer length exceeded`), { code: 'ERR_CHILD_PROCESS_STDIO_MAXBUFFER' }) as ShellFailure;
      killShell();
    } else {
      kept.push(chunk);
    }
  };

  if (options.timeout && options.timeout > 0) {
    timer = setTimeout(() => {
      killShell();
      timer = undefined;
    }, options.timeout);
  }
  child.stdout?.setEncoding('utf8');
  child.stderr?.setEncoding('utf8');
  child.stdout?.on('data', collect('stdout'));
  child.stderr?.on('data', collect('stderr'));
  child.addListener('close', finish);
  child.addListener('error', (e) => {
    failure = e as ShellFailure;
    closePipes();
    finish();
  });

  return {
    child,
    stop() {
      if (exited) return;
      closePipes();
      endCommand(child);
    },
  };
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
  /**
   * The "Always allow …" button, when one is offered: edits inside the project, or one command (see
   * commandRules.ts). Its answer is remembered for this project, across restarts.
   */
  always?: string;
}

export type ApprovalAnswer = 'once' | 'session' | 'always' | 'deny';

const ALLOW = 'Allow';
const ALLOW_SESSION = 'Allow edits for this session';
const ALWAYS_EDITS = 'Always allow edits in this project';
const ALWAYS_REMEMBER = 'Always allow remembering in this project';

async function promptForApproval(req: ApprovalRequest): Promise<ApprovalAnswer> {
  const buttons = [ALLOW];
  if (req.sessionAllowed) buttons.push(ALLOW_SESSION);
  if (req.always) buttons.push(req.always);
  const choice = await vscode.window.showWarningMessage(
    `DLNE agent wants to ${req.summary}`,
    { modal: true, detail: req.detail },
    ...buttons
  );
  if (req.always && choice === req.always) return 'always';
  if (choice === ALLOW_SESSION) return 'session';
  if (choice === ALLOW) return 'once';
  return 'deny';
}

/**
 * What the model is told when sudo gave up for want of a password. Plain `sudo <command>` works in these commands
 * (VS Code asks the user, askpass.ts), but a model that once saw sudo fail tries -n, -S or a piped password, which
 * never reach the user, or tells the user to run the command themselves.
 */
function sudoHint(command: string, output: string): string {
  if (process.platform === 'win32') return '';
  if (/\bpkexec\b/.test(command)) {
    return '\n\nNote: use plain `sudo <command>` rather than pkexec: VS Code asks the user for the password and can ' +
      'remember it for the session, while pkexec asks every time.';
  }
  if (!/sudo: (a password is required|no password was provided|a terminal is required)/.test(output)) return '';
  return '\n\nNote: plain `sudo <command>` works here: VS Code asks the user for the password in a password box. ' +
    'Do not use `sudo -S`, `sudo -n` or a piped or echoed password, and do not ask the user to run the command themselves.';
}

/** Where "Always allow" answers live: the extension's storage for this project (VS Code's workspaceState). */
export interface ApprovalStore {
  get<T>(key: string): T | undefined;
  update(key: string, value: unknown): Thenable<void>;
}

const ALWAYS_EDITS_KEY = 'dlne.agent.alwaysAllowEdits';
const ALWAYS_COMMANDS_KEY = 'dlne.agent.alwaysAllowCommands';
const ALWAYS_REMEMBER_KEY = 'dlne.agent.alwaysAllowRemember';
const MAX_ALWAYS_COMMANDS = 100;

/** Until the extension hands over its project storage (and in tests): kept for this window only. */
function memoryStore(): ApprovalStore {
  const values = new Map<string, unknown>();
  return {
    get: <T>(key: string) => values.get(key) as T | undefined,
    update: async (key: string, value: unknown) => { values.set(key, value); },
  };
}

/**
 * `p` with every symbolic link in it resolved. A path that does not exist yet (a file about to be created) is
 * resolved through its nearest folder that does, so a link in the middle of it still counts.
 */
function realPathOf(p: string): string {
  const missing: string[] = [];
  let current = path.resolve(p);
  for (;;) {
    try {
      return path.join(fs.realpathSync(current), ...missing.reverse());
    } catch {
      const parent = path.dirname(current);
      if (parent === current) return path.resolve(p);
      missing.push(path.basename(current));
      current = parent;
    }
  }
}

/**
 * True when `target` is the workspace root or inside it (never true with no folder open). Judged on where the
 * path really leads: a link in the workspace that points at ~/Documents is not "inside" it, or the approval
 * for anything outside the workspace could be walked round with one `ln -s`.
 */
function insideWorkspace(root: string | undefined, target: string): boolean {
  if (!root) return false;
  const rel = path.relative(realPathOf(root), realPathOf(target));
  return rel === '' || (!rel.startsWith('..') && !path.isAbsolute(rel));
}

/** The `dlne.agent.memory` setting, in the form memory.ts takes it. */
/**
 * True for the files that make up the agent's persistent memory (docs/agent-memory.md section 7): AGENTS.md at the
 * workspace root, anything in its .dlne folder, and ~/.dlne/memory.md, judged on where the path really leads. The
 * file tools never write these, whatever edits are allowed: a fact goes in only through `remember`, which the user
 * approves one by one, so text the agent read cannot plant standing instructions through an "Allow edits" answer.
 */
function isMemoryPath(root: string | undefined, target: string): boolean {
  const caseless = process.platform === 'win32' || process.platform === 'darwin';
  const real = (p: string) => (caseless ? realPathOf(p).toLowerCase() : realPathOf(p));
  const file = real(target);
  if (file === real(path.join(os.homedir(), '.dlne', 'memory.md'))) return true;
  // The user's skills (skills.ts) are written only by save_skill, which asks, just as for the memory file.
  const userSkills = path.relative(real(path.join(os.homedir(), '.dlne', 'skills')), file);
  if (userSkills === '' || (!userSkills.startsWith('..') && !path.isAbsolute(userSkills))) return true;
  if (!root) return false;
  if (file === real(path.join(root, 'AGENTS.md'))) return true;
  const rel = path.relative(real(path.join(root, '.dlne')), file);
  return rel === '' || (!rel.startsWith('..') && !path.isAbsolute(rel));
}

function memoryOptions(): { mode: MemoryMode } {
  return { mode: memoryMode(vscode.workspace.getConfiguration('dlne').get<unknown>('agent.memory', 'auto')) };
}

export class ToolRegistry {

  /**
   * The model's tool calls arrive over the network (and the agent reads files that may contain
   * instructions), so nothing that changes the machine or reaches outside the workspace runs
   * without the user's say-so. Tests replace `approver`.
   */
  static approver: (req: ApprovalRequest) => Promise<ApprovalAnswer> = promptForApproval;
  private static editsAllowedForSession = false;
  /** "Always allow" answers for this project (extension.ts hands over workspaceState at activation). */
  static store: ApprovalStore = memoryStore();

  /**
   * The one switch for running every agent action without asking: dlne.autoApproveAll in the user's settings, read
   * at every tool call, so flipping it (status bar, the sidebar's Auto button, "DLNE: Toggle Auto-Approve") takes
   * effect at the agent's next step. User settings only: a project's settings cannot turn it on.
   */
  static autoApproveAllOn(): boolean {
    return vscode.workspace.getConfiguration('dlne').get<boolean>('autoApproveAll', false) === true;
  }

  static async setAutoApproveAll(on: boolean): Promise<void> {
    await vscode.workspace.getConfiguration('dlne').update('autoApproveAll', on, vscode.ConfigurationTarget.Global);
  }

  /** Forget "allow for this session" (called when the conversation is cleared). "Always allow" answers stay. */
  static resetApprovals(): void {
    this.editsAllowedForSession = false;
  }

  /** Forget every "Always allow" answer for this project (the command "DLNE: Forget Agent Approvals"). */
  static async forgetAlwaysApprovals(): Promise<void> {
    await this.store.update(ALWAYS_EDITS_KEY, undefined);
    await this.store.update(ALWAYS_COMMANDS_KEY, undefined);
    await this.store.update(ALWAYS_REMEMBER_KEY, undefined);
    forgetSudoPassword();
  }

  /** What "Always allow" holds for this project, for showing it to the user. */
  static alwaysApprovals(): { edits: boolean; commands: string[]; remember: boolean } {
    const commands = this.store.get<unknown>(ALWAYS_COMMANDS_KEY);
    return {
      edits: this.store.get<unknown>(ALWAYS_EDITS_KEY) === true,
      remember: this.store.get<unknown>(ALWAYS_REMEMBER_KEY) === true,
      commands: Array.isArray(commands) ? commands.filter((c): c is string => typeof c === 'string') : [],
    };
  }

  private static async rememberCommand(prefix: string): Promise<void> {
    const commands = this.alwaysApprovals().commands.filter((c) => c !== prefix);
    commands.push(prefix);
    await this.store.update(ALWAYS_COMMANDS_KEY, commands.slice(-MAX_ALWAYS_COMMANDS));
  }

  /**
   * null when the call may run; otherwise the refusal to hand back to the model. `signal`: Stop was pressed. A call
   * that was waiting for the user's answer is over at once (VS Code has no way to withdraw a modal dialog, so it stays
   * until the user closes it, but nothing waits for it and whatever it is answered later changes nothing).
   */
  private static async checkApproval(name: string, args: Record<string, any>, root: string | undefined,
    approveAll: boolean, signal?: AbortSignal): Promise<string | null> {
    const config = vscode.workspace.getConfiguration('dlne');
    // The full auto switch (off by default, user settings only), or the chat's own "approve everything" (the Chat view's
    // permission picker, the sidebar's Auto button): every tool runs without asking.
    if (approveAll || this.autoApproveAllOn()) return null;
    const autoReads = config.get<boolean>('autoApproveReads', true);
    const always = this.alwaysApprovals();
    let req: ApprovalRequest | null = null;
    let commandPrefix: string | null = null;

    switch (name) {
      case 'read_file':
      case 'view_image':
      case 'list_directory': {
        const target = resolvePath(root, args.path || '.');
        const inside = insideWorkspace(root, target);
        if (inside && autoReads) return null;
        // A paste the user made, kept whole by DLNE because it was too long to send (pasted.ts): read like the workspace.
        if (name === 'read_file' && autoReads && isPastedFile(target)) return null;
        req = {
          tool: name,
          summary: `${name === 'read_file' ? 'read' : name === 'view_image' ? 'look at' : 'list'} ${inside ? path.relative(root!, target) || '.' : target}${inside ? '' : ' (outside your workspace)'}`,
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
        if (inside && (this.editsAllowedForSession || always.edits)) return null;
        const excerpt = String(name === 'write_file' ? args.content ?? '' : args.replace_block ?? '').slice(0, 600);
        req = {
          tool: name,
          summary: `${name === 'write_file' ? (fs.existsSync(target) ? 'overwrite' : 'create') : 'edit'} ${inside ? path.relative(root!, target) : target}${inside ? '' : ' (OUTSIDE your workspace)'}`,
          detail: `${target}\n\n${excerpt}`,
          sessionAllowed: inside,
          // Only inside a project: edits elsewhere on the machine are asked about every time.
          always: inside ? ALWAYS_EDITS : undefined,
        };
        break;
      }
      case 'generate_image': {
        // The same answer as write_file for the file it saves (executeTool has made `args.path` absolute and inside the
        // workspace), asked before the picture is made: a declined save should not cost minutes of the coordinator's GPU.
        const target = String(args.path);
        if (this.editsAllowedForSession || always.edits) return null;
        req = {
          tool: name,
          summary: `${fs.existsSync(target) ? 'overwrite' : 'create'} ${path.relative(root!, target)} with a generated image`,
          detail: `${target}\n\nPrompt: ${String(args.prompt).slice(0, 600)}\n\nThe prompt is sent to the DLNE coordinator's image engine (not to a volunteer's machine). The picture takes 1 to 3 minutes.`,
          sessionAllowed: true,
          always: ALWAYS_EDITS,
        };
        break;
      }
      case 'run_terminal_command': {
        const command = String(args.command ?? '');
        // Remembered per project: with no folder open, a command is asked about every time.
        if (root && allowedAlways(command, always.commands)) return null;
        commandPrefix = root ? alwaysPrefix(command) : null;
        req = {
          tool: name,
          summary: 'run a command in your shell',
          detail: `${command}\n\nIn: ${root || os.homedir()}` +
            (commandPrefix ? `\n\n"Always allow" lets "${commandPrefix}" run again in this project, with any arguments, without asking.` : ''),
          sessionAllowed: false,
          always: commandPrefix ? `Always allow "${commandPrefix}"` : undefined,
        };
        break;
      }
      case 'remember': {
        // A fact that would be refused anyway (bad arguments, memory off, a secret, a duplicate, a full file) is not
        // asked about: the call goes on and executeToolInternal hands the model the reason.
        const options = memoryOptions();
        const check = checkRemember(args.scope, args.fact ?? '', root, options, args.replaces);
        if (!check.ready) return null;
        const whole = check.fact;
        // The user's own file asks every time. A workspace opened at the home folder makes the project's file the
        // user's own, so that one asks every time too.
        const projectFile = check.scope === 'project'
          && path.resolve(check.file) !== path.resolve(path.join(os.homedir(), '.dlne', 'memory.md'));
        // A link or a fetch-and-run command is shown first, whatever the mode: text the agent read could plant one.
        const risky = riskyFact(`${whole} ${check.replacedText ?? ''}`);
        // Automatic (the default): project facts are saved at once, and the chat shows each with an Undo.
        if (projectFile && !risky && (options.mode === 'auto' || always.remember)) return null;
        const where = check.scope === 'project' ? 'for this project' : 'about you, for every project';
        // An update or a removal names the old fact by its id too: it is how the agent picked it out, and the
        // user who opens the file can find the same fact by its text.
        req = {
          tool: name,
          summary: !check.replaced
            ? `remember ${where}: "${whole}"`
            : check.fact
              ? `update a remembered fact ${where}: [${check.replacedId}] "${check.replacedText}" → "${whole}"`
              : `forget a remembered fact ${where}: [${check.replacedId}] "${check.replacedText}"`,
          detail: `${check.file}\n\nIt is sent with every DLNE request, to the coordinator or a volunteer's coding member.`,
          sessionAllowed: false,
          always: projectFile ? ALWAYS_REMEMBER : undefined,
        };
        break;
      }
      case 'save_skill': {
        // As for remember: a save that would be refused anyway is not asked about; the call goes on and the model is told why.
        const options = memoryOptions();
        const check = checkSaveSkill(args, root, options);
        if (!check.ready) return null;
        const userSkills = path.resolve(path.join(os.homedir(), '.dlne', 'skills'));
        const rel = path.relative(userSkills, path.resolve(check.file));
        // The user's own skills ask every time, and so does a workspace opened at the home folder (its project folder is theirs).
        const projectFile = check.scope === 'project' && (rel.startsWith('..') || path.isAbsolute(rel));
        // Steps that fetch, run or escalate, or a link, are shown first whatever the mode: text the agent read could plant one.
        const risky = check.after !== null && riskyFact(`${check.description} ${check.instructions}`);
        if (projectFile && !risky && !check.hides && options.mode === 'auto') return null;
        const where = check.scope === 'project' ? 'for this project' : 'about you, for every project';
        const excerpt = check.instructions.slice(0, 600) + (check.instructions.length > 600 ? '…' : '');
        req = {
          tool: name,
          summary: check.after === null
            ? `remove a skill ${where}: "${check.name}"`
            : `${check.before === null ? 'save' : 'update'} a skill ${where}: "${check.name}" - ${check.description}`,
          detail: `${check.file}\n\n${excerpt}\n\nThe skill's name and description are sent with every DLNE request, and its steps when the agent uses it, ` +
            "to the coordinator or a volunteer's coding member." +
            (check.hides ? '\n\nIt hides your own skill of the same name.' : ''),
          sessionAllowed: false,
        };
        break;
      }
      default:
        return null; // unknown tools fail on their own in executeToolInternal; use_skill only reads, so it never asks
    }

    if (signal?.aborted) return STOPPED_NOTICE;
    const answer = await untilStopped(this.approver(req), signal);
    // Stopped while the dialog was open, or answered just after: not run, and an "Always allow" is not remembered.
    if (answer === STOPPED || signal?.aborted) return STOPPED_NOTICE;
    if (answer === 'deny') {
      return `The user declined: ${req.summary}. Do not retry it; ask the user or choose another approach.`;
    }
    if (answer === 'session' && req.sessionAllowed) this.editsAllowedForSession = true;
    if (answer === 'always' && req.always) {
      if (name === 'run_terminal_command' && commandPrefix) {
        await this.rememberCommand(commandPrefix);
      } else if (name === 'write_file' || name === 'edit_file_diff' || name === 'generate_image') {
        await this.store.update(ALWAYS_EDITS_KEY, true);
      } else if (name === 'remember') {
        await this.store.update(ALWAYS_REMEMBER_KEY, true);
      }
    }
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
      },
      // Kept word for word as docs/agent-memory.md section 3 has it: the coordinator's canary decoys copy it.
      {
        type: 'function',
        function: {
          name: 'remember',
          description: 'Save, update or remove one lasting fact for later sessions. scope "project" keeps it in this project\'s .dlne/memory.md (how to build and test it, where things live, its conventions, the user\'s corrections); it is saved at once and shown to the user, who can undo it. scope "user" keeps it in ~/.dlne/memory.md, for the user\'s own preferences that hold in every project; the user approves it. Facts you saved show with an id in front, like [a7f3]. To update one, give its id in replaces and the new text in fact; to remove it, give its id in replaces and leave fact empty. Prefer updating a fact to adding a near-copy.',
          parameters: {
            type: 'object',
            properties: {
              fact: { type: 'string', description: 'One self-contained fact, a sentence or two (at most 1,000 characters); empty to remove the fact given in replaces.' },
              scope: { type: 'string', enum: ['project', 'user'], description: 'Where it is kept: project or user.' },
              replaces: { type: 'string', description: 'Optional: the id of a fact you saved earlier (the [a7f3] in front of it), or its opening words.' }
            },
            required: ['fact', 'scope']
          }
        }
      },
      // Skills (extension 1.6.0, docs/agent-memory.md section 3 and 7.9): kept word for word like remember, for the same reason.
      {
        type: 'function',
        function: {
          name: 'use_skill',
          description: 'Load the step-by-step instructions of a skill listed under Skills in your notes, when the task at hand matches its description. Returns the instructions and the skill\'s folder, where other files can be read with read_file.',
          parameters: {
            type: 'object',
            properties: {
              name: { type: 'string', description: 'The skill\'s name, as listed under Skills.' }
            },
            required: ['name']
          }
        }
      },
      {
        type: 'function',
        function: {
          name: 'save_skill',
          description: 'Save, update or remove a skill: a procedure you worked out that is likely to be needed again (how to release this project, how its tests run, a chore that recurs), written short and step by step. scope "project" keeps it in this project\'s .dlne/skills/<name>/SKILL.md; it is saved at once and shown to the user, who can undo it. scope "user" keeps it in ~/.dlne/skills/<name>/SKILL.md, for every project; the user approves it. You can only change a skill you wrote yourself; to remove one, give its name and leave instructions empty. Never put secrets in a skill.',
          parameters: {
            type: 'object',
            properties: {
              name: { type: 'string', description: 'Lower-case letters, digits and hyphens, at most 48 characters (like release-checklist).' },
              description: { type: 'string', description: 'One line, at most 200 characters, saying what the skill is for and when to use it.' },
              instructions: { type: 'string', description: 'The steps, in Markdown, at most 12,000 characters; empty to remove the skill.' },
              scope: { type: 'string', enum: ['project', 'user'], description: 'Where it is kept: project or user.' }
            },
            required: ['name', 'description', 'instructions', 'scope']
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

  /**
   * Secrets never leave the editor: a file excluded for privacy is not read (the model is told so, and
   * the user is not asked to approve a read that cannot happen), and a rewrite that would put the
   * `[REDACTED]` marker into a real file is refused. null when the call may go on.
   */
  private static privacyRefusal(name: string, args: Record<string, any>, root: string | undefined): string | null {
    if (typeof args?.path !== 'string' || !args.path) return null;
    const target = resolvePath(root, args.path);
    if (name === 'read_file' || name === 'edit_file_diff' || name === VIEW_IMAGE_TOOL_NAME) {
      let reason: string | null;
      try {
        reason = excludedReason(target);
      } catch {
        reason = 'the privacy check could not be completed'; // fail closed
      }
      if (reason) return excludedNotice(args.path, reason);
    }
    if (name === 'write_file' || name === 'edit_file_diff' || name === 'generate_image') {
      if (isMemoryPath(root, target)) {
        return `Refused: ${args.path} is part of the agent's persistent memory, which the file tools never change. ` +
          'Save a fact with the remember tool, or a procedure with save_skill, instead (the user approves what is for every project); AGENTS.md is the user\'s to edit.';
      }
      const writes = [args.content, args.search_block, args.replace_block].map((s) => String(s ?? '')).join('\n');
      if (writes.includes(REDACTION_MARK)) {
        let existing = '';
        try { existing = fs.readFileSync(target, 'utf-8'); } catch { /* a new file: nothing to lose */ }
        if (!existing.includes(REDACTION_MARK) && redactSecrets(existing, target).count > 0) {
          return `Refused: the text contains ${REDACTION_MARK}, which stands for a secret DLNE hid from you, and this file holds such secrets. ` +
            'Writing it would replace the real values. Edit only the lines you need to change and leave the lines with secrets out.';
        }
      }
    }
    return null;
  }

  /** What a tool returns goes to the model, so secrets are removed (read_file already did, before numbering lines). */
  private static scrub(result: ToolCallResult): ToolCallResult {
    if (result.name === 'read_file' || !result.output) return result;
    const { text, count } = redactSecrets(result.output);
    if (count > 0) result.output = text + redactionNote(count);
    return result;
  }

  /** `approveAll`: the chat this call comes from approves everything for now (see checkApproval). */
  static async executeTool(name: string, args: Record<string, any>, approveAll = false, context: ToolContext = {}): Promise<ToolCallResult> {
    if (context.signal?.aborted) return { name, output: STOPPED_NOTICE, is_error: true };
    const folders = vscode.workspace.workspaceFolders;
    const root = folders && folders.length > 0 ? folders[0].uri.fsPath : undefined;
    if (name === 'generate_image') {
      if (!context.images) {
        return { name, output: 'Image generation is not available on this coordinator.', is_error: true };
      }
      // From here on `args` is what will happen: a cleaned prompt, a valid size, the absolute path of a .png inside the workspace.
      const prepared = prepareImageArgs(args || {}, root);
      if (!prepared.ok) return { name, output: prepared.error, is_error: true };
      // Judged on where the path really leads: a link in the workspace that points elsewhere is not inside it.
      if (!insideWorkspace(root, prepared.args.path)) {
        return { name, output: `Error: ${args.path} leads outside the workspace: pictures are saved inside it.`, is_error: true };
      }
      // Secrets never leave the editor, and this prompt goes to the coordinator.
      args = { ...prepared.args, prompt: redactSecrets(prepared.args.prompt).text };
    }
    // An excluded file is refused before the user is even asked: its contents are not the user's to give away.
    const hidden = this.privacyRefusal(name, args || {}, root);
    if (hidden) return { name, output: hidden, is_error: true };
    const refusal = await this.checkApproval(name, args || {}, root, approveAll, context.signal);
    if (refusal) return { name, output: refusal, is_error: true };
    const result = await this.executeToolInternal(name, args, context);
    // A skill's instructions (at most 12,000 characters) are the point of the call: they are not cut to the usual 8,000.
    return name === 'use_skill' ? this.scrub(result) : this.capOutput(this.scrub(result));
  }

  private static async executeToolInternal(name: string, args: Record<string, any>, context: ToolContext = {}): Promise<ToolCallResult> {
    const workspaceFolders = vscode.workspace.workspaceFolders;
    const rootPath = (workspaceFolders && workspaceFolders.length > 0) ? workspaceFolders[0].uri.fsPath : os.homedir();

    try {
      switch (name) {
        case 'read_file': {
          const filePath = resolvePath(rootPath, args.path);
          if (!fs.existsSync(filePath)) {
            return { name, output: `File not found: ${args.path}`, is_error: true };
          }
          // Redact the whole file before numbering: a private key spans lines, and redaction keeps line counts.
          const { text, count: hidden } = redactSecrets(fs.readFileSync(filePath, 'utf-8'), filePath);
          const lines = text.split(/\r?\n/);

          const start = Math.max(1, args.start_line || 1) - 1;
          const end = args.end_line ? Math.min(lines.length, args.end_line) : lines.length;

          const numbered = lines.slice(start, end).map((line, idx) => `${start + idx + 1}: ${line}`);
          return { name, output: numbered.join('\n') + redactionNote(hidden) };
        }

        case 'view_image': {
          if (!context.vision) return { name, output: 'Pictures are not available on this coordinator.', is_error: true };
          const filePath = resolvePath(rootPath, args.path);
          if (!fs.existsSync(filePath)) return { name, output: `File not found: ${args.path}`, is_error: true };
          const stat = fs.statSync(filePath);
          if (!stat.isFile()) return { name, output: `${args.path} is not a file.`, is_error: true };
          if (stat.size > 4 * 1024 * 1024) {
            return { name, output: `Cannot show ${args.path}: it is ${(stat.size / 1048576).toFixed(1)} MiB, and a picture may be 4 MiB at most.`, is_error: true };
          }
          const picture = pictureFromBytes(path.basename(filePath), fs.readFileSync(filePath));
          if (!picture.ok) return { name, output: `Cannot show ${args.path}: ${picture.error}.`, is_error: true };
          const { width, height } = picture.picture;
          return {
            name,
            output: `Showing ${args.path}${width && height ? ` (${width}x${height})` : ''}`,
            image: picture.picture,
          };
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
          // No explorer refresh: VS Code's file watcher shows the new file by itself, and the refresh command
          // switches the side bar to the Explorer, which hid the DLNE chat in the middle of the agent's run.

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

        case 'generate_image': {
          // executeTool has checked the arguments and the path, and the user has approved the save.
          const image = args as ImageArgs;
          const outcome = await runImageJob(context.images!, image, { signal: context.signal, onProgress: context.onProgress, sleep: context.sleep });
          if (!outcome.ok || !outcome.png) {
            return { name, output: outcome.text, is_error: true };
          }
          fs.mkdirSync(path.dirname(image.path), { recursive: true });
          fs.writeFileSync(image.path, outcome.png);
          const relPath = path.relative(rootPath, image.path).split(path.sep).join('/');
          return { name, output: savedText(relPath, outcome), imagePath: relPath };
        }

        case 'edit_file_diff': {
          const filePath = resolvePath(rootPath, args.path);
          const relPath = rootPath ? path.relative(rootPath, filePath) : filePath;
          if (!fs.existsSync(filePath)) {
            return { name, output: `File not found: ${args.path}`, is_error: true };
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
            return { name, output: `Directory not found: ${args.path || '.'}`, is_error: true };
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
            if (isExcludedFile(uri)) continue; // a match in a private file would tell the model what it holds
            try {
              const data = await vscode.workspace.fs.readFile(uri);
              // Searched as the model would see the file (read_file hides secrets): matching the raw text would
              // let it recover a hidden value one prefix at a time, by asking whether "pass...x" occurs.
              const text = redactSecrets(new TextDecoder().decode(data), uri.fsPath).text;
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

          // Nothing is typed into a VS Code terminal: the chats show the command and its output. An echo there once
          // commented out only a command's first line, so bash ran the rest of a heredoc (`import struct` started
          // ImageMagick's screen grab, and a `for …:` line left the shell waiting).
          // Execute synchronously via child_process and return actual stdout and stderr to the model.
          // There is no terminal, so sudo asks for its password through VS Code (askpass.ts).
          const cwd = rootPath;
          const signal = context.signal;
          const run = await withAskpass(cmd, 60000, (askpassEnv) => new Promise<{ text: string; stopped: boolean }>((resolve) => {
            const shell = runShell(
              cmd,
              { cwd, timeout: 60000, maxBuffer: 10 * 1024 * 1024, env: { ...process.env, ...askpassEnv } },
              (error, stdout, stderr) => {
                signal?.removeEventListener('abort', stop);
                const outStr = stdout ? stdout.trim() : '';
                const errStr = stderr ? stderr.trim() : '';
                if (error) {
                  const combined = [outStr, errStr, error.message].filter(Boolean).join('\n');
                  resolve({ text: `Command failed (exit code ${error.code ?? 1}):\n${combined}`, stopped: false });
                } else {
                  const combined = [outStr, errStr].filter(Boolean).join('\n');
                  resolve({ text: combined || '(Command completed successfully with no output)', stopped: false });
                }
              }
            );
            // Stop: the command and everything it started are ended (runShell/endCommand: its process group, jobs it left in
            // the background included), and the run does not wait for them to say so (VS Code closes the response a second
            // after Stop).
            function stop(): void {
              resolve({ text: '', stopped: true });
              shell.stop();
            }
            if (signal?.aborted) stop();
            else signal?.addEventListener('abort', stop, { once: true });
          }));
          if (run.stopped) {
            return { name, output: 'Stopped: the user stopped the task while this command was running, so it was ended.', terminalCommand: cmd, is_error: true };
          }

          return {
            name,
            output: run.text + sudoHint(cmd, run.text),
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
            if (isExcludedFile(uri)) continue;
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

        case 'remember': {
          // The workspace root, not rootPath: with no folder open there is no project to remember anything for.
          const root = workspaceFolders && workspaceFolders.length > 0 ? workspaceFolders[0].uri.fsPath : undefined;
          const replaces = typeof args.replaces === 'string' ? args.replaces : undefined;
          const saved = rememberFact(args.scope, args.fact ?? '', root, memoryOptions(), replaces);
          return { name, output: saved.message, is_error: saved.ok ? undefined : true, memoryChange: saved.change };
        }

        case 'use_skill': {
          const root = workspaceFolders && workspaceFolders.length > 0 ? workspaceFolders[0].uri.fsPath : undefined;
          const used = useSkill(args.name, root, memoryOptions());
          return { name, output: used.message, is_error: used.ok ? undefined : true };
        }

        case 'save_skill': {
          const root = workspaceFolders && workspaceFolders.length > 0 ? workspaceFolders[0].uri.fsPath : undefined;
          const saved = saveSkill(args, root, memoryOptions());
          return { name, output: saved.message, is_error: saved.ok ? undefined : true, memoryChange: saved.change };
        }

        default:
          return { name, output: `Unknown tool: ${name}`, is_error: true };
      }
    } catch (err: any) {
      // Node's messages quote absolute paths ("ENOENT: ... open '/home/alice/work/x'"), and this text goes to
      // the model, which may be on a stranger's machine: the machine's user name stays here.
      return { name, output: `Tool execution failed: ${withoutLocalPaths(err.message || String(err), rootPath)}`, is_error: true };
    }
  }
}
