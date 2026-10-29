import * as crypto from 'crypto';
import * as fs from 'fs';
import * as http from 'http';
import * as os from 'os';
import * as path from 'path';
import * as vscode from 'vscode';

/**
 * sudo for the agent's commands. They run without a terminal (child_process.exec), so sudo cannot ask for a
 * password and stops with "a terminal is required to read the password". sudo's own answer is an askpass helper:
 * with SUDO_ASKPASS set it runs the helper instead. Ours (askpassMain.ts, run by VS Code's own runtime) asks this
 * extension over a socket in a private folder, and the extension asks the user: a centred dialog first (the input
 * box sits at the top of the window, where nobody looking at the chat sees it), then the password box. The
 * password goes from the user to sudo: never into the conversation, never to the model, never to disk.
 *
 * "Enter and Remember for This Session" keeps it in memory until VS Code closes or "DLNE: Forget Agent Approvals",
 * and it then answers every sudo the agent runs, under Auto too; the dialog says so before the user chooses.
 */

export interface PasswordAnswer {
  password: string;
  remember: boolean;
}

interface Running {
  command: string;
  asks: number;
  /** Cancelled when the command ends (or times out): a prompt still open for it is withdrawn. */
  cancel: Cancellable;
}

interface Cancellable {
  token: vscode.CancellationToken;
  cancel(): void;
}

function cancellable(): Cancellable {
  const listeners: Array<() => void> = [];
  const token = {
    isCancellationRequested: false,
    onCancellationRequested: (listener: () => void) => {
      listeners.push(listener);
      return { dispose() {} };
    },
  };
  return {
    token: token as unknown as vscode.CancellationToken,
    cancel() {
      if (token.isCancellationRequested) return;
      token.isCancellationRequested = true;
      listeners.forEach((listener) => listener());
    },
  };
}

/** How the user is asked (tests replace `ask`). */
export const sudoPrompt = {
  ask: askPassword as (prompt: string, command: string, token: vscode.CancellationToken) => Promise<PasswordAnswer | undefined>,
};

let helper: { env: Record<string, string>; running: Map<string, Running> } | null = null;
let remembered: string | null = null;

/** Forget the remembered password (the command "DLNE: Forget Agent Approvals"). */
export function forgetSudoPassword(): void {
  remembered = null;
}

/**
 * Run `run` with the environment that lets sudo ask through VS Code. The helper answers only while the command
 * runs, and names it to the user. After `timeoutMs` a prompt still open is withdrawn: sudo, waiting on its
 * helper, ignores the signal that ends a command that ran too long, and would hold the agent forever.
 */
export async function withAskpass<T>(command: string, timeoutMs: number,
  run: (env: Record<string, string>) => Promise<T>): Promise<T> {
  const h = startHelper();
  if (!h) return run({});
  const id = crypto.randomBytes(16).toString('hex');
  const entry: Running = { command, asks: 0, cancel: cancellable() };
  h.running.set(id, entry);
  const timer = setTimeout(() => entry.cancel.cancel(), timeoutMs);
  try {
    return await run({ ...h.env, DLNE_ASKPASS_ID: id });
  } finally {
    clearTimeout(timer);
    entry.cancel.cancel();
    h.running.delete(id);
  }
}

function startHelper(): typeof helper {
  if (helper || process.platform === 'win32') return helper;
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-askpass-'));   // 0700: no other user can reach the socket
  const socket = path.join(dir, 'askpass.sock');
  const script = path.join(dir, 'askpass.sh');
  fs.writeFileSync(script, '#!/bin/sh\nELECTRON_RUN_AS_NODE=1 exec "$DLNE_ASKPASS_NODE" "$DLNE_ASKPASS_MAIN" "$@"\n', { mode: 0o700 });
  const running = new Map<string, Running>();

  const server = http.createServer((req, res) => {
    let body = '';
    req.setEncoding('utf8');
    req.on('data', (chunk) => {
      body += chunk;
      if (body.length > 4096) req.destroy();
    });
    req.on('end', async () => {
      const entry = running.get(String(req.headers['x-dlne-askpass'] ?? ''));
      const answer = entry ? await answerFor(entry, body.trim()) : undefined;
      res.writeHead(answer === undefined ? 403 : 200);
      res.end(answer ?? '');
    });
  });
  server.listen(socket);
  server.unref();
  process.once('exit', () => fs.rmSync(dir, { recursive: true, force: true }));

  helper = {
    running,
    env: {
      SUDO_ASKPASS: script,
      DLNE_ASKPASS_NODE: process.execPath,
      DLNE_ASKPASS_MAIN: path.join(__dirname, 'askpassMain.js'),
      DLNE_ASKPASS_HANDLE: socket,
    },
  };
  return helper;
}

async function answerFor(entry: Running, prompt: string): Promise<string | undefined> {
  entry.asks++;
  // A second ask for the same command means sudo refused the last answer: a remembered one was wrong.
  if (entry.asks > 1) remembered = null;
  if (remembered !== null) return remembered;
  const answer = await sudoPrompt.ask(prompt, entry.command, entry.cancel.token);
  if (!answer || entry.cancel.token.isCancellationRequested) return undefined;
  if (answer.remember) remembered = answer.password;
  return answer.password;
}

/** A centred dialog naming the command (with the Remember choice), then the password box, already focused. */
async function askPassword(prompt: string, command: string, token: vscode.CancellationToken): Promise<PasswordAnswer | undefined> {
  const enter = 'Enter Password';
  const remember = 'Enter and Remember for This Session';
  const choice = await vscode.window.showWarningMessage('The DLNE agent needs your sudo password', {
    modal: true,
    detail: `To run: ${command.slice(0, 300)}\n\n"Remember" keeps the password in memory until VS Code closes (or ` +
      '"DLNE: Forget Agent Approvals"): the agent\'s later sudo commands, Auto runs included, will not ask again.',
  }, enter, remember);
  if (!choice || token.isCancellationRequested) return undefined;
  const password = await vscode.window.showInputBox({
    title: 'DLNE agent: sudo password',
    prompt: `For: ${command.slice(0, 200)}`,
    placeHolder: prompt || 'Password',
    password: true,
    ignoreFocusOut: true,
  }, token);
  return password === undefined ? undefined : { password, remember: choice === remember };
}
