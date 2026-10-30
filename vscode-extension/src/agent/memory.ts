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
 *
 * Memory 2 (2026-10-30): every fact the agent saved has a short id, shown to the model as `- [a7f3] <fact>`, so
 * it can name the fact it means in `replaces` without copying the text back. The id is computed from the fact,
 * never stored in the file.
 */
import * as crypto from 'crypto';
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

/**
 * A memory file's text as the model sees it, before redaction and the cap: the agent's lines carry their ids,
 * then comments (the agent's mark among them) and the title are dropped. The cap is measured on this, so a save
 * that fits never pushes the model's own facts out of what is loaded.
 */
function shown(raw: string): string {
  return cleaned(withIds(raw));
}

/** The file's text as it will be sent, or null when it is missing, unreadable, empty, excluded or a link out of `root`. Never throws. */
function loadFile(file: string, cap: number, isMemoryFile: boolean, root: string): string | null {
  try {
    if (memoryExclusion(file, isMemoryFile)) return null;
    if (leavesRoot(file, root)) return null;
    if (!fs.statSync(file).isFile()) return null;

    // Only the memory files have the agent's lines, and so ids. AGENTS.md is the user's alone.
    const raw = readHead(file, cap);
    const text = redactSecrets(isMemoryFile ? shown(raw) : cleaned(raw), file).text;
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

/**
 * A fact reduced to what makes it that fact: no comments (the agent's mark), no list marker, one space between
 * words, lower case. Two facts are the same when their keys are, and a fact's id is made from its key.
 */
function factKey(text: string): string {
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

// ---- fact ids (Memory 2) --------------------------------------------------------------------------------

/** The SHA-256 of a fact's key, in hex. Its first characters are the fact's id. */
function keyHash(text: string): string {
  return crypto.createHash('sha256').update(factKey(text), 'utf8').digest('hex');
}

/** The usual id of a fact or a file line: 4 hex characters. Only a clash in the same file makes ids longer (factIds). */
export function factId(text: string): string {
  return keyHash(text).slice(0, 4);
}

/**
 * The id of every line the agent wrote among `lines` (one file's lines). Ids are 4 characters, unless two of the
 * agent's lines would share one: then every id in the file grows, up to 8, to the first length at which they all
 * differ. engine/canary.py computes them the same way for its decoys.
 */
export function factIds(lines: string[]): Map<string, string> {
  const saved = lines.filter(isAgentLine);
  const hashes = saved.map(keyHash);
  let length = 4;
  while (length < 8 && new Set(hashes.map((hash) => hash.slice(0, length))).size < hashes.length) length++;
  return new Map(saved.map((line, i) => [line, hashes[i].slice(0, length)]));
}

/**
 * The text with each of the agent's lines shown with its id: `- fact <!-- dlne -->` becomes
 * `- [a7f3] fact <!-- dlne -->`. The mark itself goes when comments are stripped (cleaned), so the model sees
 * `- [a7f3] fact` for its own facts and the user's lines as they are.
 */
function withIds(raw: string): string {
  const lines = raw.split('\n');
  const ids = factIds(lines);
  return lines.map((line) => {
    const id = ids.get(line);
    if (!id) return line;
    // A line the user stripped of its bullet still shows as a list item, so every id stands in the same place.
    const [, indent, bullet, rest] = /^(\s*)([-*]\s+)?([\s\S]*)$/.exec(line) as RegExpExecArray;
    return `${indent}${bullet ?? '- '}[${id}] ${rest}`;
  }).join('\n');
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
    /** That line's id, as the model saw it in front of the fact. */
    replacedId: string | null;
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

/** The most saved facts a message to the model lists; the rest are counted. */
const LISTED_FACTS = 40;

/** The opening of a fact, for a list in a message: its first 50 characters, with … when there is more. */
function opening(line: string): string {
  const fact = factOf(line);
  return fact.length > 50 ? `${fact.slice(0, 50)}…` : fact;
}

/** At most LISTED_FACTS items joined by "; ", then " and <n> more" for the rest. */
function listOf(items: string[]): string {
  const listed = items.slice(0, LISTED_FACTS);
  return listed.join('; ') + (items.length > listed.length ? ` and ${items.length - listed.length} more` : '');
}

/** Saved facts with their ids, for a model that has to pick one out again: `[a7f3] "Tests run with…"; …`. */
function savedList(lines: string[], ids: Map<string, string>): string {
  return listOf(lines.map((line) => `[${ids.get(line)}] "${opening(line)}"`));
}

/** `replaces` when it is nothing but an id: `a7f3`, `[a7f3]`, `id: a7f3`. */
const ID_ONLY = /^\[?(?:id[:\s]*)?([0-9a-f]{4,8})\]?$/i;

/**
 * `replaces` as the key of the fact it quotes. Models dress a quote up: they copy the id or the bullet in front
 * of it, say which memory it is in ("project: …", which made a dozen calls in a row fail on 2026-10-30), put it in
 * quotes, or cut it short with an ellipsis. All of that goes. The front is cleaned until nothing more comes off,
 * so `- [a7f3] …` and `project: - …` work as well as the contract's order.
 */
function quotedKey(replaces: string): string {
  let text = replaces.trim();
  for (let before = ''; before !== text;) {
    before = text;
    text = text
      .replace(/^\[[0-9a-f]+\]\s*/i, '')                        // the id the model saw in front of the fact
      .replace(/^[-*]\s+/, '')                                  // the list marker
      .replace(/^(?:project|user)(?:\s+memory)?\s*:\s*/i, '')   // the memory it is in
      .trim();
  }
  const quoted = /^(["'`“‘])([\s\S]*)(["'`”’])$/.exec(text);
  if (quoted) text = quoted[2].trim();
  return factKey(text.replace(/(?:…|\.\.\.)$/, '').replace(/\.$/, ''));
}

/** A key without its full stop, so "Tests run with npm test" names "Tests run with npm test." exactly. */
function withoutStop(key: string): string {
  return key.replace(/\.$/, '');
}

/** The lines `key` names by text: the one it equals, else every one that holds it. */
function byText(lines: string[], key: string): string[] {
  if (!key) return [];
  const exact = lines.filter((line) => withoutStop(factKey(line)) === key);
  return exact.length > 0 ? exact : lines.filter((line) => factKey(line).includes(key));
}

/** The words of a key that the word match counts: letters, digits and _, at least 2 long. */
function wordsOf(key: string): Set<string> {
  return new Set(key.match(/[a-z0-9_]{2,}/g) ?? []);
}

/** Below this share of words in common, a line is not the one `replaces` means; and the best must lead by MARGIN. */
const WORD_SHARE = 0.8;
const WORD_MARGIN = 0.1;
const NEARLY = 1e-9; // 0.9 - 0.8 is 0.0999… in floating point: a lead of exactly 0.1 still counts

/**
 * The lines `key` names by its words, for a quote that is close but not exact (a word changed, one left out).
 * The share of `key`'s words found in a line is its score. One line wins if it scores at least 0.8 and leads the
 * next by 0.1; when the top lines are closer than that, all of them come back and the model is asked which.
 * A key of fewer than 4 words says too little to judge by.
 */
function byWords(lines: string[], key: string): string[] {
  const wanted = wordsOf(key);
  if (wanted.size < 4 || lines.length === 0) return [];
  const scored = lines.map((line) => {
    const words = wordsOf(factKey(line));
    let shared = 0;
    for (const word of wanted) if (words.has(word)) shared++;
    return { line, score: shared / wanted.size };
  }).sort((a, b) => b.score - a.score);
  const best = scored[0].score;
  if (best < WORD_SHARE - NEARLY) return [];
  const second = scored.length > 1 ? scored[1].score : 0;
  if (best - second >= WORD_MARGIN - NEARLY) return [scored[0].line];
  return scored.filter((s) => s.score > best - WORD_MARGIN + NEARLY).map((s) => s.line);
}

/**
 * The line of the agent's own fact that `replaces` names, tried in this order, the first one that names exactly
 * one line winning:
 *   1. its id (`a7f3`, `[a7f3]`, `id: a7f3`);
 *   2. its text, cleaned of what models put around a quote (quotedKey): the whole fact, or a part only it holds;
 *   3. most of its words (byWords).
 * A miss lists the saved facts with their ids, so the model can retry with one without reading the file. A quote
 * of a line the user wrote is refused as such: those lines are theirs.
 */
function findSaved(lines: string[], replaces: string): { line: string; id: string } | { problem: string } {
  const ids = factIds(lines);
  const saved = [...new Set(lines.filter(isAgentLine))]; // a line copied twice by hand is one fact
  const found = (line: string) => ({ line, id: ids.get(line) as string });

  const asId = ID_ONLY.exec(replaces.trim());
  if (asId) {
    const named = saved.filter((line) => ids.get(line) === asId[1].toLowerCase());
    if (named.length === 1) return found(named[0]);
  }

  const key = quotedKey(replaces);
  const byQuote = byText(saved, key);
  if (byQuote.length === 1) return found(byQuote[0]);
  const byWord = byWords(saved, key);
  if (byWord.length === 1) return found(byWord[0]);

  const several = byQuote.length > 1 ? byQuote : byWord;
  if (several.length > 1) {
    return {
      problem: `"${replaces}" matches ${several.length} facts you saved: ${savedList(several, ids)}. ` +
        'Give the id of the one you mean in replaces.',
    };
  }

  // Not one of the agent's: is it one the user wrote? Those are never changed, and the model should hear why.
  const theirs = lines.filter((line) => !isAgentLine(line) && line.trim() !== MEMORY_TITLE && factKey(line) !== '');
  if (byText(theirs, key).length > 0 || byWords(theirs, key).length > 0) {
    return { problem: 'That fact was written by the user, not by DLNE, so DLNE cannot change it. Ask the user, or save a new fact.' };
  }

  const quote = replaces.slice(0, 80);
  if (saved.length === 0) return { problem: `No fact you saved matches "${quote}"; you have not saved any in this file.` };
  return { problem: `No fact you saved matches "${quote}". Yours: ${savedList(saved, ids)}. Give the id in replaces.` };
}

/**
 * What the model is told when a save would pass the cap: how full the file is, and its own facts, largest first,
 * so it can make room by merging, shortening or removing them. 2026-10-30: told only "ask the user to tidy it",
 * a model at 7,541 of 8,000 characters kept trying and failing instead.
 */
function fullMessage(scope: 'project' | 'user', used: number, cap: number, lines: string[]): string {
  const ids = factIds(lines);
  const saved = [...new Set(lines.filter(isAgentLine))];
  const head = `The ${scope} memory is full (${used} of ${cap} characters), so nothing was saved.`;
  if (saved.length === 0) {
    // All of it is the user's, and the agent cannot change their lines: only the user can make room.
    return `${head} None of it was saved by DLNE, so ask the user to tidy it.`;
  }
  // Stable sort: facts of the same size stay in file order.
  const largest = [...saved].sort((a, b) => factOf(b).length - factOf(a).length);
  return `${head} Make room first: merge or shorten facts with replaces, or remove ones that no longer matter. ` +
    `Yours, largest first: ${listOf(largest.map((line) => `[${ids.get(line)}] (${factOf(line).length} chars) "${opening(line)}"`))}`;
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

  // The whole file is read, because a fact is added by rewriting it. One far past its cap is more than DLNE
  // reads at all (section 12): it is the user's to tidy, and no change through remember is offered.
  const cap = scope === 'project' ? PROJECT_CAP : USER_CAP;
  let existing = '';
  try {
    if (fs.statSync(file).size > cap * 4 + 4096) {
      return refuse(`The ${scope} memory is full (its file is larger than DLNE reads), so nothing was saved, ` +
        `and DLNE cannot change it. Ask the user to tidy ${file}.`);
    }
    existing = fs.readFileSync(file, 'utf-8');
  } catch (err: any) {
    if (err?.code !== 'ENOENT') return refuse(`The memory file (${file}) could not be read (${err?.code ?? 'error'}), so nothing was saved.`);
  }

  const lines = existing.split(/\r?\n/);
  // Only a line the agent wrote can be updated or removed: the user's own lines are theirs.
  let replaced: string | null = null;
  let replacedId: string | null = null;
  if (old) {
    const found = findSaved(lines, old);
    if ('problem' in found) return refuse(found.problem);
    replaced = found.line;
    replacedId = found.id;
  }
  let parts: string[] = [];
  if (clean) {
    // A part already in the file (an earlier save of the same long fact, say) is not added twice.
    const kept = new Set(lines.filter((line) => line !== replaced).map(factKey));
    parts = splitFact(clean).filter((part) => !kept.has(factKey(part)));
    if (parts.length === 0) return { ready: false, result: { ok: true, message: `Already remembered: ${clean}` } };
    // Full is judged on what loading would send (ids in, comments out), with the change made. Only growth past
    // the cap is refused: a change that leaves the file no larger is always allowed, even in a file already
    // over it (the user may have filled it by hand), since that is how room is made.
    const added = parts.map(agentLine).join('\n');
    const after = replaced ? existing.replace(replaced, () => added) : `${existing}\n${added}`;
    const used = shown(existing).length;
    const usedAfter = shown(after).length;
    if (usedAfter > cap && usedAfter > used) return refuse(fullMessage(scope, used, cap, lines));
  }
  return {
    ready: true, scope, fact: clean, parts, file, existing, replaced,
    replacedText: replaced && factOf(replaced), replacedId,
  };
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
 * chat's Undo needs (section 7.6), one per part, and the ids of the lines added, as the model will see them.
 */
export function rememberFact(
  scope: 'project' | 'user',
  fact: string,
  workspaceRoot: string | undefined,
  options: MemoryOptions = {},
  replaces?: string
): { ok: boolean; message: string; change?: MemoryChange | MemoryChange[]; ids?: string[] } {
  const check = checkRemember(scope, fact, workspaceRoot, options, replaces);
  if (!check.ready) return check.result;

  const where = scope === 'project' ? 'for this project' : 'for you';
  const added = check.parts.map(agentLine);
  let changes: MemoryChange[];
  let ids: string[];
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
    // The new lines' ids in the file as it is now, which is how the next load will show them.
    const now = factIds(lines);
    ids = added.map((line) => now.get(line) as string);
  } catch (err: any) {
    return { ok: false, message: `The memory file (${check.file}) could not be written (${err?.code ?? 'error'}), so nothing was saved.` };
  }
  // 📝, not the brain emoji: that one stood in for the DLNE logo once, and only the logo is the mark now.
  // Each line says the fact's id, so the model can name it in a later replaces without waiting for a new load.
  const messages = check.parts.map((part, i) => i === 0 && check.replaced
    ? `📝 Updated ${where} [${check.replacedId}] → [${ids[i]}]: ${check.replacedText} → ${part}`
    : `📝 Remembered ${where} [${ids[i]}]: ${part}`);
  if (messages.length === 0) messages.push(`📝 Forgot ${where} [${check.replacedId}]: ${check.replacedText}`);
  // Said outright: a model that saw its fact come back as two lines took it for a failure and kept redoing it.
  if (messages.length > 1) {
    messages.push(`It is over ${FACT_MAX_CHARS} characters, so it was saved as these ${messages.length} facts, one after the other. That is expected: nothing more to do.`);
  }
  return { ok: true, message: messages.join('\n'), change: changes.length === 1 ? changes[0] : changes, ids };
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
