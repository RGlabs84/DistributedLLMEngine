// VS Code first (2.7.0, docs/release-2.7-contract.md section 4b): while VS Code with DLNE is open, DLNE Donate (the tray
// app, 1.1.0 and later) pauses its own worker and leaves the computer to this editor. The tray knows we are here from a
// small file, ~/.dlne/editor.json:
//
//     {"pid": <the extension host's process id>, "updated": <epoch seconds>, "version": "1.6.0"}
//
// written when the extension activates, refreshed every 30 s, and removed when it deactivates. The tray counts the
// editor as present while the file is under 90 s old and its pid is alive. Several windows each run an extension host
// and each refreshes the same file, so the last writer's pid is in it; a window only removes the file when its own pid
// is the one in it.
//
// Nothing here imports `vscode`, so all of it runs under plain Node in the tests.

import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';

/** How often the file is refreshed; the tray treats it as stale at three times this. */
export const PRESENCE_REFRESH_MS = 30_000;

/** Where the file lives: the DLNE folder in the home directory (HOME / USERPROFILE, read when asked, never cached). */
export function presenceFile(home: string = os.homedir()): string {
  return path.join(home, '.dlne', 'editor.json');
}

export interface PresenceFacts {
  pid: number;
  /** Epoch seconds of the last refresh. */
  updated: number;
  version: string;
}

/** The facts in a presence file's text, or null for anything that is not one. */
export function parsePresence(text: string | null): PresenceFacts | null {
  if (!text) return null;
  try {
    const raw = JSON.parse(text);
    if (!raw || !Number.isInteger(raw.pid) || raw.pid <= 0 || typeof raw.updated !== 'number') return null;
    return { pid: raw.pid, updated: raw.updated, version: typeof raw.version === 'string' ? raw.version : '' };
  } catch {
    return null;
  }
}

/** What an EditorPresence needs from the machine; the tests hand in their own. */
export interface PresenceEnvironment {
  home(): string;
  pid: number;
  /** Epoch milliseconds. */
  now(): number;
  setInterval(handler: () => void, ms: number): unknown;
  clearInterval(handle: unknown): void;
}

const realEnvironment: PresenceEnvironment = {
  home: () => os.homedir(),
  pid: process.pid,
  now: () => Date.now(),
  setInterval: (handler, ms) => {
    const timer = setInterval(handler, ms);
    // The refresh must never be what keeps a process alive.
    timer.unref?.();
    return timer;
  },
  clearInterval: (handle) => clearInterval(handle as NodeJS.Timeout),
};

export class EditorPresence {
  private timer: unknown = null;

  constructor(private readonly version: string, private readonly env: PresenceEnvironment = realEnvironment) {}

  /** Writes the file now and every PRESENCE_REFRESH_MS after, until stop(). */
  public start(): void {
    this.write();
    if (this.timer === null) this.timer = this.env.setInterval(() => this.write(), PRESENCE_REFRESH_MS);
  }

  /** Writes (or rewrites) the file. Never throws: a home folder that cannot be written only leaves the tray running. */
  public write(): void {
    const file = presenceFile(this.env.home());
    const facts: PresenceFacts = { pid: this.env.pid, updated: Math.floor(this.env.now() / 1000), version: this.version };
    const scratch = `${file}.${this.env.pid}.tmp`;
    try {
      fs.mkdirSync(path.dirname(file), { recursive: true });
      // 0600 from the start: the file is made private before anything is in it.
      fs.writeFileSync(scratch, JSON.stringify(facts), { mode: 0o600 });
      fs.renameSync(scratch, file);
      // rename keeps the scratch file's mode; chmod too for a file that existed with another one (Windows ignores it).
      try { fs.chmodSync(file, 0o600); } catch { /* not supported here */ }
    } catch {
      try { fs.unlinkSync(scratch); } catch { /* nothing to remove */ }
    }
  }

  /** Stops refreshing and removes the file, but only when it names this process (another window may own it now). */
  public stop(): void {
    if (this.timer !== null) {
      this.env.clearInterval(this.timer);
      this.timer = null;
    }
    const file = presenceFile(this.env.home());
    try {
      const facts = parsePresence(fs.readFileSync(file, 'utf-8'));
      if (facts && facts.pid === this.env.pid) fs.unlinkSync(file);
    } catch {
      // already gone, or unreadable: nothing of ours to remove
    }
  }
}
