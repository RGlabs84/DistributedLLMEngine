import * as vscode from 'vscode';

/**
 * The "DLNE" output channel (View > Output > DLNE) for things that go wrong quietly, such as an autocomplete
 * request that failed: they are written down here instead of interrupting anyone. The channel is made the
 * first time something is written to it.
 */
let channel: vscode.OutputChannel | null = null;
const lastWritten = new Map<string, number>();
const MAX_KEYS = 200;

/**
 * Writes `line` to the DLNE log, but the same `key` at most once per `everyMs`: a server that is down shows up as
 * one line a minute, not one per keystroke. Returns whether the line was written.
 */
export function logOnce(key: string, line: string, everyMs = 60_000, now = Date.now()): boolean {
  const last = lastWritten.get(key);
  if (last !== undefined && now - last < everyMs) {
    return false;
  }
  lastWritten.set(key, now);
  if (lastWritten.size > MAX_KEYS) {
    const oldest = lastWritten.keys().next().value;
    if (oldest !== undefined) lastWritten.delete(oldest);
  }
  channel ??= vscode.window.createOutputChannel('DLNE');
  channel.appendLine(`[${new Date(now).toISOString()}] ${line}`);
  return true;
}
