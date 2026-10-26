/**
 * "Always allow" for the agent's shell commands: what an answer remembers, and when a remembered answer applies.
 *
 * A remembered answer is a command prefix for one project, such as `npm test`, `git status` or `python -m pytest`:
 * it lets that command run again with any arguments (`npm test -- --watch`) without asking. The model's commands
 * arrive over the network (with coding members, from a volunteer's machine), so the rules are narrow on purpose:
 * - only a simple command qualifies: no `;`, `&&`, `|`, redirection, `$(...)` or backticks, which could tack a
 *   second command onto an allowed one;
 * - a prefix names a tool and what it does (`npm run build`, not `npm run`), and an interpreter only with a
 *   script or module (`python -m pytest`, never `python -c ...`);
 * - some programs are never remembered at all: they delete, escalate, fetch from the network, open a shell or
 *   control the system, and each use of them is worth a look.
 * Allowing a command still allows what the project makes it do (`npm test` runs package.json's test script).
 */

/** Programs whose every use is asked about: deleting, privileges, shells, network transfers, system control. */
const NEVER_REMEMBERED = new Set([
  'sudo', 'su', 'doas', 'rm', 'rmdir', 'del', 'erase', 'rd', 'dd', 'mkfs', 'shred', 'format',
  'chmod', 'chown', 'chgrp', 'icacls', 'takeown',
  'curl', 'wget', 'invoke-webrequest', 'iwr', 'ssh', 'scp', 'sftp', 'rsync', 'nc', 'ncat', 'netcat', 'telnet', 'ftp',
  'sh', 'bash', 'zsh', 'fish', 'dash', 'ksh', 'csh', 'tcsh', 'pwsh', 'powershell', 'cmd', 'eval', 'exec', 'source',
  'kill', 'killall', 'pkill', 'taskkill', 'shutdown', 'reboot', 'halt', 'poweroff', 'systemctl', 'service',
  'crontab', 'at', 'schtasks', 'reg', 'find', 'xargs', 'env', 'nohup', 'timeout', 'watch', 'setx',
]);

/** Tools whose first argument is the thing they do (`git status`, `npm test`, `cargo build`). */
const SUBCOMMAND_TOOLS = new Set([
  'npm', 'pnpm', 'yarn', 'bun', 'git', 'cargo', 'go', 'dotnet', 'docker', 'kubectl', 'pip', 'pip3', 'poetry', 'uv',
  'gradle', 'gradlew', './gradlew', 'gradlew.bat', 'mvn', 'mvnw', './mvnw', 'bundle', 'composer', 'flutter', 'dart',
  'swift', 'mix', 'rustup', 'terraform', 'helm', 'deno', 'make', 'conda', 'brew', 'gh',
]);

/** Subcommands that run something named next (`npm run build`, `pnpm exec vitest`): the name is part of the rule. */
const RUNS_NEXT = new Set(['run', 'exec', 'x', 'dlx', 'run-script']);

/** Package runners: the package they run is part of the rule (`npx eslint`). */
const RUNNERS = new Set(['npx', 'pnpx', 'bunx', 'uvx', 'pipx']);

/** Interpreters: remembered only with a script or `-m module`, never with inline code (`-c`, `-e`). */
const INTERPRETERS = new Set(['python', 'python3', 'py', 'node', 'ruby', 'perl', 'php']);

/** True when `command` is one command: nothing that chains, pipes, redirects or substitutes another. */
export function isSimpleCommand(command: string): boolean {
  const text = String(command ?? '').trim();
  return text.length > 0 && text.length <= 400 && !/[;&|`$<>\r\n()]/.test(text);
}

/** The words of a simple command (quotes are not interpreted: a quoted word just stays one word with its quotes). */
function words(command: string): string[] {
  return command.trim().split(/\s+/);
}

/**
 * What "Always allow" would remember for `command`, or null when it is not offered: `npm test` for
 * `npm test -- --watch`, `git status` for `git status --short`, `python -m pytest` for `python -m pytest -q`.
 */
export function alwaysPrefix(command: string): string | null {
  if (!isSimpleCommand(command)) return null;
  const [first, second, third] = words(command);
  const tool = first.toLowerCase();
  const base = tool.replace(/^.*[\\/]/, '').replace(/\.(exe|cmd|bat)$/, '');
  if (NEVER_REMEMBERED.has(base)) return null;
  const isFlag = (word: string | undefined) => !word || word.startsWith('-') || word.startsWith('/');

  if (INTERPRETERS.has(base)) {
    if (second === '-m' && !isFlag(third)) return `${first} -m ${third}`;
    return isFlag(second) ? null : `${first} ${second}`;
  }
  if (RUNNERS.has(base)) {
    return isFlag(second) ? null : `${first} ${second}`;
  }
  if (SUBCOMMAND_TOOLS.has(tool) || SUBCOMMAND_TOOLS.has(base)) {
    if (isFlag(second)) return null;
    if (RUNS_NEXT.has(second.toLowerCase())) return isFlag(third) ? null : `${first} ${second} ${third}`;
    return `${first} ${second}`;
  }
  return first;
}

/** True when a remembered prefix lets `command` run without asking. */
export function allowedAlways(command: string, prefixes: readonly string[]): boolean {
  if (!isSimpleCommand(command)) return false;
  // The command's own rule must still be one that could be remembered (rm never is, however it was stored).
  if (alwaysPrefix(command) === null) return false;
  const normalized = words(command).join(' ');
  return prefixes.some((prefix) => typeof prefix === 'string' && prefix.length > 0
    && (normalized === prefix || normalized.startsWith(prefix + ' ')));
}
