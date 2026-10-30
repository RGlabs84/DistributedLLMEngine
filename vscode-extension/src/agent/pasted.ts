/**
 * Text pasted into a Chat-view message that is too long to send whole (1.5.3). VS Code hands a big paste to @dlne as
 * an attachment ("Pasted text #1"); one message carries at most ATTACHED_MAX_CHARS of attachments, and before 1.5.3 the
 * rest was cut off with no way for the agent to see it: an 18,000-line log arrived as its first 12,000 characters
 * (the owner, 2026-10-30). The whole paste, its secrets already hidden, is now kept in a file of the extension's own,
 * and the agent pages through it with read_file, which reads this folder without asking (tools.ts): nothing is in it
 * but what the user pasted. Nothing here imports `vscode`, so the tests run it under plain Node.
 */
import { randomBytes } from 'crypto';
import * as fs from 'fs';
import * as path from 'path';

/** The newest this many pastes are kept, none older than a week. */
export const MAX_PASTES = 20;
export const PASTE_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;

let folder: string | undefined;

/** Where pastes are kept: the extension's own storage (extension.ts sets it at activation). Undefined: none are kept. */
export function setPastedDir(dir: string | undefined): void {
  folder = dir ? path.resolve(dir) : undefined;
}

export function pastedDir(): string | undefined {
  return folder;
}

/** Whether `file` is a saved paste: a plain file directly in the folder, judged on where the path really leads. */
export function isPastedFile(file: string): boolean {
  if (!folder) return false;
  try {
    const real = fs.realpathSync(file);
    return path.dirname(real) === fs.realpathSync(folder) && /^pasted-[\w-]+\.txt$/.test(path.basename(real)) &&
      fs.statSync(real).isFile();
  } catch {
    return false;
  }
}

/**
 * Keeps `text` (secrets already hidden by the caller) in a new file readable by the user only, and returns its path,
 * or undefined when there is nowhere to keep it or it could not be written. Old pastes are let go on the way.
 */
export function savePasted(text: string, now: number = Date.now()): string | undefined {
  if (!folder) return undefined;
  try {
    fs.mkdirSync(folder, { recursive: true, mode: 0o700 });
    prunePasted(now);
    const stamp = new Date(now).toISOString().replace(/[:.]/g, '-');
    const file = path.join(folder, `pasted-${stamp}-${randomBytes(3).toString('hex')}.txt`);
    fs.writeFileSync(file, text, { mode: 0o600 });
    return file;
  } catch {
    return undefined;
  }
}

/** Deletes pastes past a week old and all but the newest MAX_PASTES - 1 (room for the one being saved). */
export function prunePasted(now: number = Date.now()): void {
  if (!folder) return;
  let names: string[];
  try {
    names = fs.readdirSync(folder).filter((n) => /^pasted-[\w-]+\.txt$/.test(n));
  } catch {
    return;
  }
  const files = names
    .map((name) => {
      const file = path.join(folder!, name);
      try {
        return { file, modified: fs.statSync(file).mtimeMs };
      } catch {
        return null;
      }
    })
    .filter((f): f is { file: string; modified: number } => f !== null)
    .sort((a, b) => b.modified - a.modified);
  files.forEach((f, index) => {
    if (index >= MAX_PASTES - 1 || now - f.modified > PASTE_MAX_AGE_MS) {
      try {
        fs.rmSync(f.file, { force: true });
      } catch {
        // tried again with the next paste
      }
    }
  });
}
