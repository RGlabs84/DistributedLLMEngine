/**
 * The agent's persistent memory (docs/agent-memory.md section 7): the files that carry over between tasks,
 * chats and restarts, and the `remember` tool's writer.
 *
 *   <workspace root>/AGENTS.md       the project's instructions (written by the user, never by the agent)
 *   <workspace root>/.dlne/memory.md what `remember` saved for this project
 *   ~/.dlne/memory.md                what `remember` saved about the user, for every project
 *
 * All three are sent with every agent request, to whoever serves it, so everything here is cleaned and
 * redacted on the way in, and a file the privacy rules exclude is neither read nor written.
 *
 * Nothing in this file touches VS Code: the `dlne.agent.memory` setting and the home folder arrive as options,
 * so the tests run it on plain Node with a temporary folder.
 */
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { excludedReason, excludedReasonSkipping, redactSecrets } from '../privacy';

export interface MemorySource { label: 'instructions' | 'project' | 'user'; path: string; text: string }
export interface MemorySources { instructions?: MemorySource; project?: MemorySource; user?: MemorySource }
export interface MemoryOptions {
  /** False is the same as mode "off" (kept for callers from before the modes). */
  enabled?: boolean;
  /** The `dlne.agent.memory` setting as a mode (default "auto"): see memoryMode. Off: nothing loaded or saved. */
  mode?: 'auto' | 'ask' | 'off';
  /** Where the user's `.dlne/memory.md` lives (default `os.homedir()`); tests pass a temporary folder. */
  homeDir?: string;
}

// ---- the context block (section 7.3) -------------------------------------------------------------------

// The canary parity test compares these with the contract, so they are copied from it, not reworded.
export const CONTEXT_OPEN = '<dlne_context>';
export const CONTEXT_INTRO = 'Standing notes for this workspace, loaded by the DLNE extension. They are not a request from the user.';
export const CONTEXT_CLOSE = '</dlne_context>';
export const CONTEXT_HEADINGS = {
  instructions: '## Project instructions (AGENTS.md)',
  project: '## Project memory (.dlne/memory.md)',
  user: "## The user's preferences (~/.dlne/memory.md)",
};

// ---- sizes and fixed texts -----------------------------------------------------------------------------

const INSTRUCTIONS_CAP = 8_000;
const PROJECT_CAP = 8_000;
const USER_CAP = 4_000;
/** The longest fact `remember` writes on one line: a longer one is saved in parts (splitFact). */
const FACT_MAX_CHARS = 500;

/** The first line of a file `remember` created. It is not loaded, since it says nothing to the model. */
const MEMORY_TITLE = '# DLNE memory';

function newFileHeader(scope: 'project' | 'user'): string {
  const about = scope === 'project' ? 'this project' : 'your preferences, for every project';
  return `${MEMORY_TITLE}\n\n` +
    `<!-- What the DLNE agent remembers about ${about}. It reads this file at the start of every request and sends it, ` +
    'with the rest of the prompt, to the DLNE swarm (the coordinator or a volunteer\'s coding member). ' +
    'Add, edit or delete lines freely; keep secrets out. -->\n\n';
}

const MEMORY_OFF = 'Memory is off (the dlne.agent.memory setting), so nothing was saved.';

/**
 * Why the privacy rules keep DLNE away from `file`, or null when it may read and write it. The memory files are
 * the exception to git-ignore: people git-ignore tool folders such as `.dlne/` so they stay out of commits, and
 * that must not silently switch memory off. A memory file is still kept out by `.dlneignore` (the user's word on
 * what DLNE may touch) and by the credential-file rules. AGENTS.md is not a memory file and keeps every rule.
 */
function memoryExclusion(file: string, isMemoryFile: boolean): string | null {
  try {
    // Skipping the git rules (rather than excusing a git-sourced reason) keeps a .dlneignore entry in force even
    // where a .gitignore entry for the folder would have been found first.
    return isMemoryFile ? excludedReasonSkipping(file, ['.gitignore', '.git/info/exclude']) : excludedReason(file);
  } catch {
    return 'the privacy check could not be completed'; // fail closed, as the rest of the agent does
  }
}

/**
 * True when `file`, with every link in it resolved, lies outside `root`: an AGENTS.md or a `.dlne` folder that
 * links elsewhere would otherwise send that other file to the swarm, or write the user's facts there. A path that
 * does not exist yet is judged by its nearest existing folder.
 */
function leavesRoot(file: string, root: string): boolean {
  const real = (p: string): string => {
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
  };
  const rel = path.relative(real(root), real(file));
  return rel === '' || rel.startsWith('..') || path.isAbsolute(rel);
}

/** Invisible and direction-changing characters: in a fact they could hide text from the approval dialog. */
const HIDDEN_CHARACTERS = /[​-‏‪-‮⁠-⁤⁦-⁩﻿]/;

// ---- loading (section 7.2) -----------------------------------------------------------------------------

/**
 * What the model should see of a memory file's text: HTML comments and the title line `remember` writes are
 * dropped (they are for the person editing the file), then the text is trimmed.
 */
function cleaned(raw: string): string {
  let text = raw.replace(/^﻿/, '');
  const firstBreak = text.indexOf('\n');
  const firstLine = (firstBreak === -1 ? text : text.slice(0, firstBreak)).replace(/\r$/, '');
  if (firstLine === MEMORY_TITLE) text = firstBreak === -1 ? '' : text.slice(firstBreak + 1);
  // The file's text must not fake the structure of the messages it goes into: a `</dlne_context>` or a
  // `<dlne_working_memory>` in it is defused (‹ in place of <), so it cannot end or begin a block.
  return text.replace(/<!--[\s\S]*?-->/g, '').replace(/[ \t]+$/gm, '').replace(/<(\/?)(dlne_)/gi, '‹$1$2').trim();
}

/** Read no more of a file than loading can use (a cut file is marked anyway): a huge one must not stall a request. */
function readHead(file: string, cap: number): string {
  const limit = cap * 4 + 4096; // UTF-8 takes at most 4 bytes a character; the rest covers comments to strip
  const fd = fs.openSync(file, 'r');
  try {
    const buffer = Buffer.alloc(Math.min(limit, Math.max(0, fs.fstatSync(fd).size)));
    const read = fs.readSync(fd, buffer, 0, buffer.length, 0);
    return buffer.subarray(0, read).toString('utf-8');
  } finally {
    fs.closeSync(fd);
  }
}

/** The file's text as it will be sent, or null when it is missing, unreadable, empty, excluded or a link out of `root`. Never throws. */
function loadFile(file: string, cap: number, isMemoryFile: boolean, root: string): string | null {
  try {
    if (memoryExclusion(file, isMemoryFile)) return null;
    if (leavesRoot(file, root)) return null;
    if (!fs.statSync(file).isFile()) return null;

    const text = redactSecrets(cleaned(readHead(file, cap)), file).text;
    if (!text) return null;
    if (text.length <= cap) return text;
    return `${text.slice(0, cap)}\n[… ${text.length - cap} characters cut: the file is longer than DLNE loads]`;
  } catch {
    return null; // a missing or unreadable file is simply absent
  }
}

function userMemoryPath(options: MemoryOptions): string {
  return path.join(options.homeDir ?? os.homedir(), '.dlne', 'memory.md');
}

function projectMemoryPath(workspaceRoot: string): string {
  return path.join(workspaceRoot, '.dlne', 'memory.md');
}

/** Reads the three memory files. Synchronous, because it runs at the start of every `run()`, and never throws. */
export function loadMemory(workspaceRoot: string | undefined, options: MemoryOptions = {}): MemorySources {
  const sources: MemorySources = {};
  try {
    if (modeOf(options) === 'off') return sources;

    const userPath = userMemoryPath(options);
    const home = options.homeDir ?? os.homedir();
    // Each file must really be where it says: the project's inside the workspace, the user's inside the home folder.
    const wanted: Array<[MemorySource['label'], string | undefined, number, string | undefined]> = [
      ['instructions', workspaceRoot ? path.join(workspaceRoot, 'AGENTS.md') : undefined, INSTRUCTIONS_CAP, workspaceRoot],
      // A workspace opened at the home folder would otherwise send the same file twice.
      ['project', workspaceRoot && projectMemoryPath(workspaceRoot) !== userPath ? projectMemoryPath(workspaceRoot) : undefined, PROJECT_CAP, workspaceRoot],
      ['user', userPath, USER_CAP, home],
    ];
    for (const [label, file, cap, root] of wanted) {
      if (!file || !root) continue;
      const text = loadFile(file, cap, label !== 'instructions', root);
      if (text) sources[label] = { label, path: file, text };
    }
  } catch {
    // whatever was loaded before the failure still counts
  }
  return sources;
}

/** The `<dlne_context>` message for what was loaded, or null when there is nothing to say. */
export function renderContextBlock(sources: MemorySources): string | null {
  const sections: string[] = [];
  for (const label of ['instructions', 'project', 'user'] as const) {
    const source = sources[label];
    if (source && source.text) sections.push(`${CONTEXT_HEADINGS[label]}\n${source.text}`);
  }
  if (sections.length === 0) return null;
  return `${CONTEXT_OPEN}\n${CONTEXT_INTRO}\n\n${sections.join('\n\n')}\n${CONTEXT_CLOSE}`;
}

// ---- remember (section 7.4) ----------------------------------------------------------------------------

/** How the `dlne.agent.memory` setting decides saving (section 7.1). */
export type MemoryMode = 'auto' | 'ask' | 'off';

/** The setting as a mode: "auto", "ask" or "off". A boolean from a pre-release build means auto or off. */
export function memoryMode(setting: unknown): MemoryMode {
  if (setting === false || setting === 'off') return 'off';
  if (setting === 'ask') return 'ask';
  return 'auto';
}

function modeOf(options: MemoryOptions): MemoryMode {
  return options.enabled === false ? 'off' : (options.mode ?? 'auto');
}

/** Ends every line the agent writes. Loading strips it (it is a comment), so the model never sees it. */
export const AGENT_MARK = '<!-- dlne -->';

/**
 * A fact the user must see before it is saved, even in auto mode: a link, or a command that fetches, runs or
 * escalates. Text the agent read could otherwise plant "always run curl … | sh" for every later session, unseen.
 * Plain build and test commands (npm test, make, pytest) are not risky.
 */
export function riskyFact(text: string): boolean {
  return /(https?:\/\/|ftp:\/\/|\bwww\.)/i.test(text)
    || /\b(curl|wget|iwr|irm|invoke-webrequest|invoke-restmethod|invoke-expression|iex|sudo|su|doas|chmod|chown|eval|exec|base64|certutil|bitsadmin|nc|ncat|netcat|ssh|scp|rsync)\b/i.test(text)
    || /\brm\s+-/i.test(text)
    || /\|\s*(ba|z|da)?sh\b/i.test(text)
    || /\bpowershell(\.exe)?\s+-e/i.test(text);
}

/** Two facts are the same when only their case, spacing, list marker or comments (the agent's mark) differ. */
function sameFactKey(text: string): string {
  return text.replace(/<!--[\s\S]*?-->/g, '').replace(/^\s*[-*]\s+/, '').replace(/\s+/g, ' ').trim().toLowerCase();
}

/** A line the agent writes: `- <fact> <!-- dlne -->`. */
function agentLine(fact: string): string {
  return `- ${fact} ${AGENT_MARK}`;
}

/** Whether a file line was written by the agent (only those can it update or remove). */
function isAgentLine(line: string): boolean {
  return line.trimEnd().endsWith(AGENT_MARK);
}

/** A change to a memory file, kept so the chat's Undo can put it back (section 7.6). */
export interface MemoryChange {
  file: string;
  scope: 'project' | 'user';
  /** The exact line removed (an update or a removal), or null. */
  removed: string | null;
  /** The exact line added (a new fact or an update), or null. */
  added: string | null;
  /** The line's index in the file: where `added` is now, or where `removed` was. */
  index: number;
}

/** What `checkRemember` found: either the answer for the model right away, or what a write would do. */
export type RememberCheck =
  | { ready: false; result: { ok: boolean; message: string } }
  | {
    ready: true; scope: 'project' | 'user'; fact: string; file: string; existing: string;
    /** The facts to write: `fact`, or its parts when it is long (splitFact), less any part already in the file. */
    parts: string[];
    /** The file line `replaces` matched (an update when `fact` is set, a removal when it is empty), or null. */
    replaced: string | null;
    /** The fact on that line, as the model sees it. */
    replacedText: string | null;
  };

/** A fact, or the fact it replaces, as one trimmed line. */
function oneLine(text: string): string {
  return text.replace(/\r\n|\r|\n/g, ' ').trim();
}

/**
 * A fact over FACT_MAX_CHARS as several: whole sentences packed up to the limit (split only where . ! ? or ; is
 * followed by a space, so "2.5.3" stays whole), and one sentence too long on its own cut at a space. The agent's long
 * note is saved in parts instead of refused (2026-10-27: a 720-character fact ended a 23-minute task as an error).
 */
export function splitFact(fact: string): string[] {
  const clean = oneLine(fact);
  if (clean.length <= FACT_MAX_CHARS) return [clean];
  const parts: string[] = [];
  let current = '';
  for (let sentence of clean.split(/(?<=[.!?;])\s+/)) {
    while (sentence.length > FACT_MAX_CHARS) {
      const space = sentence.lastIndexOf(' ', FACT_MAX_CHARS);
      const at = space > FACT_MAX_CHARS / 2 ? space : FACT_MAX_CHARS;
      if (current) parts.push(current);
      current = '';
      parts.push(sentence.slice(0, at).trim());
      sentence = sentence.slice(at).trim();
    }
    if (!sentence) continue;
    if (current && current.length + 1 + sentence.length > FACT_MAX_CHARS) {
      parts.push(current);
      current = sentence;
    } else {
      current = current ? `${current} ${sentence}` : sentence;
    }
  }
  if (current) parts.push(current);
  return parts;
}

/** Why `clean` cannot be saved as a fact, or null. A long fact is not a problem: it is saved in parts. */
function factProblem(clean: string): string | null {
  // A comment marker in a fact would hide it, and every line after it, when the file is loaded (section 7.2).
  if (clean.includes('<!--') || clean.includes('-->')) {
    return 'A fact cannot contain an HTML comment marker (<!-- or -->), so it was not saved.';
  }
  // Nor DLNE's own block tags, which could fake the structure of a request, nor characters that hide text.
  if (/<\/?dlne_/i.test(clean)) return 'A fact cannot contain a DLNE tag (<dlne_…>), so it was not saved.';
  if (HIDDEN_CHARACTERS.test(clean)) return 'The fact holds invisible or direction-changing characters, so it was not saved.';
  if (redactSecrets(clean).count > 0) return 'That fact holds a secret, so it was not saved.';
  return null;
}

/** Saved facts by their opening words, for a model that has to pick one out again. */
function openings(lines: string[]): string {
  const shown = lines.slice(0, 20).map((line) => {
    const fact = factOf(line);
    return `"${fact.length > 60 ? `${fact.slice(0, 60)}…` : fact}"`;
  });
  return shown.join('; ') + (lines.length > shown.length ? ` and ${lines.length - shown.length} more` : '');
}

/**
 * The line of the agent's own fact that `old` names: the whole fact, or any part of it (its opening words are
 * enough) that no other saved fact holds. A local model copying a 500-character fact back word for word got it
 * wrong, so a miss lists the saved facts' openings and the model can retry without reading the file.
 */
function findSaved(lines: string[], old: string): { line: string } | { problem: string } {
  const saved = lines.filter(isAgentLine);
  const key = sameFactKey(old);
  const exact = saved.find((line) => sameFactKey(line) === key);
  if (exact) return { line: exact };
  const matches = saved.filter((line) => sameFactKey(line).includes(key));
  if (matches.length === 1) return { line: matches[0] };
  if (matches.length > 1) {
    return { problem: `"${old}" is in ${matches.length} facts you saved: ${openings(matches)}. Give more of the one you mean in replaces.` };
  }
  if (saved.length === 0) return { problem: `No fact you saved matches "${old}"; only facts DLNE saved can be updated or removed.` };
  return {
    problem: `No fact you saved matches "${old}"; only facts DLNE saved can be updated or removed. ` +
      `Yours begin: ${openings(saved)}. Give the opening words of the one you mean in replaces.`,
  };
}

/**
 * Every check `rememberFact` makes before it writes, without writing. The tool asks it before it asks the user,
 * so the user is never asked about a fact that would be refused anyway. `replaces` names a fact the agent saved
 * earlier: with `fact` it is updated, with an empty `fact` it is removed.
 */
export function checkRemember(
  scope: unknown,
  fact: unknown,
  workspaceRoot: string | undefined,
  options: MemoryOptions = {},
  replaces?: unknown
): RememberCheck {
  const refuse = (message: string): RememberCheck => ({ ready: false, result: { ok: false, message } });

  if (modeOf(options) === 'off') return refuse(MEMORY_OFF);
  if (scope !== 'project' && scope !== 'user') return refuse('The scope must be "project" or "user".');
  if (scope === 'project' && !workspaceRoot) return refuse('There is no project folder open, so nothing was saved for a project.');
  if (typeof fact !== 'string') return refuse('The fact must be text.');
  if (replaces !== undefined && replaces !== null && typeof replaces !== 'string') return refuse('replaces must be text.');

  const clean = oneLine(fact);
  const old = typeof replaces === 'string' ? oneLine(replaces) : '';
  if (!clean && !old) return refuse('The fact is empty, so nothing was saved.');
  if (clean) {
    const problem = factProblem(clean);
    if (problem) return refuse(problem);
  }

  const file = scope === 'project' ? projectMemoryPath(workspaceRoot as string) : userMemoryPath(options);
  const reason = memoryExclusion(file, true);
  if (reason) return refuse(`The memory file (${file}) is excluded by DLNE's privacy rules (${reason}), so nothing was saved.`);
  const root = scope === 'project' ? (workspaceRoot as string) : (options.homeDir ?? os.homedir());
  if (leavesRoot(file, root)) {
    return refuse(`The memory file (${file}) leads outside ${scope === 'project' ? 'the project' : 'the home folder'} through a link, so nothing was saved.`);
  }

  // The whole file is read, because a fact is added by rewriting it; one far past its cap is simply full.
  let existing = '';
  try {
    if (fs.statSync(file).size > (scope === 'project' ? PROJECT_CAP : USER_CAP) * 4 + 4096) {
      return refuse(`The memory file is full (${file}); ask the user to tidy it.`);
    }
    existing = fs.readFileSync(file, 'utf-8');
  } catch (err: any) {
    if (err?.code !== 'ENOENT') return refuse(`The memory file (${file}) could not be read (${err?.code ?? 'error'}), so nothing was saved.`);
  }

  const lines = existing.split(/\r?\n/);
  // Only a line the agent wrote can be updated or removed: the user's own lines are theirs.
  let replaced: string | null = null;
  if (old) {
    const found = findSaved(lines, old);
    if ('problem' in found) return refuse(found.problem);
    replaced = found.line;
  }
  let parts: string[] = [];
  if (clean) {
    // A part already in the file (an earlier save of the same long fact, say) is not added twice.
    const kept = new Set(lines.filter((line) => line !== replaced).map(sameFactKey));
    parts = splitFact(clean).filter((part) => !kept.has(sameFactKey(part)));
    if (parts.length === 0) return { ready: false, result: { ok: true, message: `Already remembered: ${clean}` } };
    // Full is judged on what loading would keep: the text without comments, with the change made.
    const cap = scope === 'project' ? PROJECT_CAP : USER_CAP;
    const added = parts.map(agentLine).join('\n');
    const after = replaced ? existing.replace(replaced, () => added) : `${existing}\n${added}`;
    if (cleaned(after).length > cap) {
      return refuse(`The memory file is full (${file}); ask the user to tidy it.`);
    }
  }
  return { ready: true, scope, fact: clean, parts, file, existing, replaced, replacedText: replaced && factOf(replaced) };
}

/** Writes `content` to `file` in one step, so a crash never leaves half a memory file. */
function writeAtomically(file: string, content: string, mode: number): void {
  const temp = `${file}.${process.pid}.${Date.now()}.tmp`;
  try {
    fs.writeFileSync(temp, content, { encoding: 'utf-8', mode });
    fs.renameSync(temp, file);
  } catch (err) {
    try { fs.unlinkSync(temp); } catch { /* it may never have been created */ }
    throw err;
  }
}

/** Rewrites a memory file in one step, keeping its target (if it is a link) and its mode (the user's stays private). */
function rewrite(file: string, scope: 'project' | 'user', content: string): void {
  // If the file is a link, the target is replaced, not the link.
  let target = file;
  try { target = fs.realpathSync(file); } catch { /* a new file */ }
  // The user's file is private to the user; the project's keeps its owner's choice, or the usual default.
  let mode = scope === 'user' ? 0o600 : 0o644;
  if (scope === 'project') {
    try { mode = fs.statSync(target).mode & 0o777; } catch { /* a new file */ }
  }
  writeAtomically(target, content, mode);
}

/**
 * Saves, updates or removes one fact (any approval has already been given). A long fact is saved in parts, one after
 * the other, where the fact it replaces was. Never throws: the result is the model's answer, with the change the
 * chat's Undo needs (section 7.6), one per part.
 */
export function rememberFact(
  scope: 'project' | 'user',
  fact: string,
  workspaceRoot: string | undefined,
  options: MemoryOptions = {},
  replaces?: string
): { ok: boolean; message: string; change?: MemoryChange | MemoryChange[] } {
  const check = checkRemember(scope, fact, workspaceRoot, options, replaces);
  if (!check.ready) return check.result;

  const where = scope === 'project' ? 'for this project' : 'for you';
  const added = check.parts.map(agentLine);
  let changes: MemoryChange[];
  try {
    fs.mkdirSync(path.dirname(check.file), { recursive: true, mode: scope === 'user' ? 0o700 : 0o755 });
    const body = check.existing
      ? check.existing.replace(/\r\n/g, '\n') + (check.existing.endsWith('\n') ? '' : '\n')
      : newFileHeader(scope);
    const lines = body.split('\n'); // ends with '' (the final newline)
    const index = check.replaced ? lines.indexOf(check.replaced) : lines.length - 1;
    lines.splice(index, check.replaced ? 1 : 0, ...added);
    changes = added.length === 0
      ? [{ file: check.file, scope, removed: check.replaced, added: null, index }]
      : added.map((line, i) => ({ file: check.file, scope, removed: i === 0 ? check.replaced : null, added: line, index: index + i }));
    rewrite(check.file, scope, lines.join('\n'));
  } catch (err: any) {
    return { ok: false, message: `The memory file (${check.file}) could not be written (${err?.code ?? 'error'}), so nothing was saved.` };
  }
  // 📝, not the brain emoji: that one stood in for the DLNE logo once, and only the logo is the mark now.
  const messages = check.parts.map((part, i) => i === 0 && check.replaced
    ? `📝 Updated ${where}: ${check.replacedText} → ${part}`
    : `📝 Remembered ${where}: ${part}`);
  if (messages.length === 0) messages.push(`📝 Forgot ${where}: ${check.replacedText}`);
  // Said outright: a model that saw its fact come back as two lines took it for a failure and kept redoing it.
  if (messages.length > 1) {
    messages.push(`It is over ${FACT_MAX_CHARS} characters, so it was saved as these ${messages.length} facts, one after the other. That is expected: nothing more to do.`);
  }
  return { ok: true, message: messages.join('\n'), change: changes.length === 1 ? changes[0] : changes };
}

/** A file line as the fact it holds: no list marker, no comments. */
function factOf(line: string): string {
  return line.replace(/<!--[\s\S]*?-->/g, '').replace(/^\s*[-*]\s+/, '').trim();
}

/**
 * Puts back a change the agent made (the chat's Undo, section 7.6). Only a DLNE memory file, only lines DLNE wrote,
 * and only while the added line is still there exactly: anything changed since is left alone.
 */
export function undoMemoryChange(change: unknown): { ok: boolean; message: string } {
  if (Array.isArray(change)) {
    // A long fact saved in parts (splitFact): every part, the last one first.
    if (change.length === 0 || change.length > 20) return { ok: false, message: 'Cannot undo: that is not a change DLNE made.' };
    const results = [...change].reverse().map((part) => undoMemoryChange(part));
    return results.find((r) => !r.ok) ?? { ok: true, message: results.map((r) => r.message).join(' ') };
  }
  const c = change as Partial<MemoryChange> | null;
  const cannot = (why: string) => ({ ok: false, message: `Cannot undo: ${why}` });
  if (!c || typeof c !== 'object' || typeof c.file !== 'string' || (c.scope !== 'project' && c.scope !== 'user')
    || !(c.removed === null || typeof c.removed === 'string') || !(c.added === null || typeof c.added === 'string')
    || typeof c.index !== 'number' || (c.removed === null && c.added === null)) {
    return cannot('that is not a change DLNE made.');
  }
  if (!/[\\/]\.dlne[\\/]memory\.md$/.test(c.file)) return cannot('that is not a DLNE memory file.');
  for (const line of [c.removed, c.added]) {
    if (line !== null && !isAgentLine(line as string)) return cannot('that line was not written by DLNE.');
  }

  let lines: string[];
  try {
    lines = fs.readFileSync(c.file, 'utf-8').replace(/\r\n/g, '\n').split('\n');
  } catch (err: any) {
    return cannot(`the memory file could not be read (${err?.code ?? 'error'}).`);
  }
  if (c.added !== null) {
    const at = lines.indexOf(c.added as string);
    if (at === -1) return cannot('the fact was changed since, so it was left as it is.');
    if (c.removed !== null) lines[at] = c.removed as string; else lines.splice(at, 1);
  } else {
    if (lines.includes(c.removed as string)) return { ok: true, message: `Already back: "${factOf(c.removed as string)}".` };
    lines.splice(Math.min(Math.max(0, c.index), Math.max(0, lines.length - 1)), 0, c.removed as string);
  }
  try {
    rewrite(c.file, c.scope, lines.join('\n'));
  } catch (err: any) {
    return cannot(`the memory file could not be written (${err?.code ?? 'error'}).`);
  }
  return c.added !== null && c.removed === null
    ? { ok: true, message: `Undone: "${factOf(c.added as string)}" is no longer remembered.` }
    : { ok: true, message: `Undone: "${factOf(c.removed as string)}" is back.` };
}
