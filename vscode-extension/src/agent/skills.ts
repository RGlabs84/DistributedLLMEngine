/**
 * The agent's skills (docs/agent-memory.md section 7.9, docs/release-2.7-contract.md section 4c): procedures the
 * agent worked out and wrote down, or the user did, to be used again. A skill is a folder with a SKILL.md:
 *
 *   <workspace root>/.dlne/skills/<name>/SKILL.md   this project's
 *   ~/.dlne/skills/<name>/SKILL.md                  the user's, for every project
 *
 *   ---
 *   name: <name>
 *   description: <one line, at most 200 characters>
 *   by: dlne                  (only on a skill the agent wrote: only those can the agent change or remove)
 *   ---
 *   <the instructions in Markdown, at most 12,000 characters>
 *
 * Only the names and descriptions reach the prompt (a `## Skills` section at the end of the `<dlne_context>` block,
 * read once per conversation with the rest of it); the agent loads a skill's instructions with `use_skill` when a
 * task fits. Nothing is trained: the model reads and writes files.
 *
 * The files go to whoever serves the request, so they follow the memory files' rules (memory.ts): the privacy
 * checks, no link out of the workspace or the home folder, secrets redacted on the way in and refused on the way
 * out, the `dlne.agent.memory` setting (off: none loaded or saved; ask: every save asks). Like memory.ts this file
 * touches nothing of VS Code, so the tests run it on plain Node with temporary folders.
 */
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { redactSecrets } from '../privacy';
import {
  HIDDEN_CHARACTERS, MemoryOptions, MemorySource, leavesRoot, memoryExclusion, modeOf, readHead,
  undoMemoryChange, writeAtomically,
} from './memory';

/** A skill's name: also its folder's name, so nothing in it can reach another folder. */
export const SKILL_NAME = /^[a-z0-9][a-z0-9-]{0,47}$/;
/** The longest description, and the most instructions, in characters. */
export const SKILL_DESCRIPTION_MAX = 200;
export const SKILL_INSTRUCTIONS_MAX = 12_000;
/** The most skills in a scope that `save_skill` makes. */
export const SKILLS_PER_SCOPE = 100;
/** What the `## Skills` list holds: at most this many lines and characters, then a count of the rest. */
export const SKILLS_LISTED_LINES = 50;
export const SKILLS_LISTED_CHARS = 6_000;
/** The most folders looked at in a scope, so a folder the user filled with thousands cannot stall a request. */
const SCAN_LIMIT = 400;

const SKILLS_OFF = 'Memory is off (the dlne.agent.memory setting), so skills are neither loaded nor saved.';
const NAME_RULE = 'A skill name is lower-case letters, digits and hyphens, starting with a letter or digit, at most 48 characters (like release-checklist)';

export type SkillScope = 'project' | 'user';

export interface Skill {
  name: string;
  scope: SkillScope;
  /** The folder and the file, absolute. */
  dir: string;
  file: string;
  /** One line, redacted, at most 200 characters. */
  description: string;
  /** The Markdown after the header, redacted, cut to 12,000 characters (with a note when it was longer). */
  instructions: string;
  /** Written by the agent (`by: dlne`): the only skills it may overwrite or remove. */
  byDlne: boolean;
}

// ---- the file --------------------------------------------------------------------------------------------

interface Parsed { name: string; description: string; byDlne: boolean; body: string }

/** A YAML value as plain text: surrounding quotes taken off. */
function unquoted(value: string): string {
  const quoted = /^(["'])([\s\S]*)\1$/.exec(value);
  return (quoted ? quoted[2] : value).trim();
}

/**
 * A SKILL.md as its parts, or null when it does not parse: no `---` header on the first line and closed by another
 * `---` line, no name or no description, or no instructions. (A description written as a folded block, `>` or `|`
 * and indented lines, is read as one line.) The name must be the folder's, which the caller checks.
 */
export function parseSkillFile(raw: string): Parsed | null {
  const lines = raw.replace(/^﻿/, '').replace(/\r\n?/g, '\n').split('\n');
  if (lines[0].trimEnd() !== '---') return null;
  const close = lines.findIndex((line, i) => i > 0 && line.trimEnd() === '---');
  if (close === -1) return null;
  const values: Record<string, string> = {};
  for (let i = 1; i < close; i++) {
    const m = /^([A-Za-z][A-Za-z0-9_-]*)\s*:\s*(.*)$/.exec(lines[i]);
    if (!m) continue;
    let value = m[2].trim();
    if (/^[>|][+-]?$/.test(value)) {
      const folded: string[] = [];
      while (i + 1 < close && /^\s+\S/.test(lines[i + 1])) folded.push(lines[++i].trim());
      value = folded.join(' ');
    }
    const key = m[1].toLowerCase();
    if (!(key in values)) values[key] = unquoted(value);
  }
  const name = values.name ?? '';
  const description = (values.description ?? '').replace(/\s+/g, ' ').trim();
  const body = lines.slice(close + 1).join('\n').trim();
  if (!name || !description || !body) return null;
  return { name, description, byDlne: (values.by ?? '').toLowerCase() === 'dlne', body };
}

/** The text of a skill file the agent writes. */
export function skillFileText(name: string, description: string, instructions: string): string {
  return `---\nname: ${name}\ndescription: ${description}\nby: dlne\n---\n\n${instructions}\n`;
}

/** Text from a file as the model may see it: tags that could fake the structure of a message are defused. */
function defused(text: string): string {
  return text.replace(/<(\/?)(dlne_)/gi, '‹$1$2');
}

function oneLine(text: string): string {
  return text.replace(/\s+/g, ' ').trim();
}

// ---- where they are -----------------------------------------------------------------------------------------

function baseOf(scope: SkillScope, root: string | undefined, options: MemoryOptions): string | undefined {
  return scope === 'project' ? root : (options.homeDir ?? os.homedir());
}

function skillsDirOf(scope: SkillScope, root: string | undefined, options: MemoryOptions): string | undefined {
  const base = baseOf(scope, root, options);
  return base ? path.join(base, '.dlne', 'skills') : undefined;
}

/** A workspace opened at the home folder has one skills folder, not two. */
function sameFolder(a: string | undefined, b: string | undefined): boolean {
  return !!a && !!b && path.resolve(a) === path.resolve(b);
}

/** The skill in `<skills>/<name>/SKILL.md`, or null when it is missing, excluded, a link out, or does not parse. Never throws. */
function readSkill(scope: SkillScope, name: string, root: string | undefined, options: MemoryOptions): Skill | null {
  try {
    const dir = skillsDirOf(scope, root, options);
    const base = baseOf(scope, root, options);
    if (!dir || !base || !SKILL_NAME.test(name)) return null;
    const file = path.join(dir, name, 'SKILL.md');
    if (memoryExclusion(file, true) || leavesRoot(file, base)) return null;
    if (!fs.statSync(file).isFile()) return null;
    const parsed = parseSkillFile(readHead(file, SKILL_INSTRUCTIONS_MAX));
    if (!parsed || parsed.name !== name) return null;
    const body = redactSecrets(parsed.body, file).text;
    const instructions = body.length > SKILL_INSTRUCTIONS_MAX
      ? `${body.slice(0, SKILL_INSTRUCTIONS_MAX)}\n[… ${body.length - SKILL_INSTRUCTIONS_MAX} characters cut: a skill holds at most ${SKILL_INSTRUCTIONS_MAX.toLocaleString('en-US')} characters]`
      : body;
    const description = defused(redactSecrets(parsed.description, file).text).slice(0, SKILL_DESCRIPTION_MAX);
    return { name, scope, dir: path.dirname(file), file, description, instructions: defused(instructions), byDlne: parsed.byDlne };
  } catch {
    return null;
  }
}

/** Every skill in one scope that loads, by name. Never throws. */
function scanScope(scope: SkillScope, root: string | undefined, options: MemoryOptions): Skill[] {
  try {
    const dir = skillsDirOf(scope, root, options);
    if (!dir) return [];
    const names = fs.readdirSync(dir).filter((n) => SKILL_NAME.test(n)).sort().slice(0, SCAN_LIMIT);
    return names.map((n) => readSkill(scope, n, root, options)).filter((s): s is Skill => s !== null);
  } catch {
    return [];
  }
}

/**
 * The skills that load: the project's, then the user's, a user skill hidden by a project skill of the same name.
 * Nothing loads while memory is off.
 */
export function loadSkills(root: string | undefined, options: MemoryOptions = {}): Skill[] {
  if (modeOf(options) === 'off') return [];
  const project = scanScope('project', root, options);
  // A workspace opened at the home folder would otherwise list every skill twice.
  const same = sameFolder(skillsDirOf('project', root, options), skillsDirOf('user', root, options));
  const user = same ? [] : scanScope('user', root, options);
  const taken = new Set(project.map((s) => s.name));
  return [...project, ...user.filter((s) => !taken.has(s.name))];
}

/**
 * The text of the `## Skills` section: `- <name> (project|user): <description>` for each skill, the project's first, at
 * most 50 lines and 6,000 characters, then `… and <n> more (use_skill works for any)`. Null when there are none.
 */
export function renderSkillList(skills: Skill[]): string | null {
  if (skills.length === 0) return null;
  const lines: string[] = [];
  let chars = 0; // the lines joined by newlines
  for (const skill of skills) {
    const line = `- ${skill.name} (${skill.scope}): ${skill.description}`;
    const next = chars + (lines.length > 0 ? 1 : 0) + line.length;
    if (lines.length >= SKILLS_LISTED_LINES || next > SKILLS_LISTED_CHARS) break;
    lines.push(line);
    chars = next;
  }
  if (lines.length < skills.length) lines.push(`… and ${skills.length - lines.length} more (use_skill works for any)`);
  return lines.join('\n');
}

/** The skills section of the context block, as a memory source, or undefined when there is nothing to list. */
export function loadSkillsSource(root: string | undefined, options: MemoryOptions = {}): MemorySource | undefined {
  const skills = loadSkills(root, options);
  const text = renderSkillList(skills);
  return text ? { label: 'skills', path: skillsDirOf('project', root, options) ?? '', text } : undefined;
}

// ---- use_skill ----------------------------------------------------------------------------------------------

/** The folder as the model is told it: relative to the workspace, or under `~`, so no user name goes with the request. */
function shownFolder(skill: Skill): string {
  return skill.scope === 'project' ? `.dlne/skills/${skill.name}` : `~/.dlne/skills/${skill.name}`;
}

/** `use_skill {name}`: the skill's instructions with its folder, or why there are none. Read-only. */
export function useSkill(name: unknown, root: string | undefined, options: MemoryOptions = {}): { ok: boolean; message: string } {
  if (modeOf(options) === 'off') return { ok: false, message: SKILLS_OFF };
  const wanted = typeof name === 'string' ? name.trim() : '';
  const skills = loadSkills(root, options);
  const skill = skills.find((s) => s.name === wanted);
  if (!skill) {
    const names = skills.map((s) => s.name);
    const listed = names.slice(0, 40).join(', ') + (names.length > 40 ? ` and ${names.length - 40} more` : '');
    return {
      ok: false,
      message: `There is no skill called "${wanted.slice(0, 60)}". ` + (names.length ? `Skills: ${listed}.` : 'There are no skills yet.'),
    };
  }
  return {
    ok: true,
    message: `Skill ${skill.name} (${skill.scope === 'project' ? 'this project' : 'yours, for every project'})\n` +
      `Folder: ${shownFolder(skill)} (files beside SKILL.md can be read with read_file)\n` +
      `${skill.description}\n\n${skill.instructions}`,
  };
}

// ---- save_skill ---------------------------------------------------------------------------------------------

/** A change to a skill file, kept so the chat's Undo can put it back. The whole file before and after (null: no file). */
export interface SkillChange {
  kind: 'skill';
  file: string;
  scope: SkillScope;
  name: string;
  before: string | null;
  after: string | null;
}

export type SkillCheck =
  | { ready: false; result: { ok: boolean; message: string } }
  | {
    ready: true; scope: SkillScope; name: string; description: string; instructions: string; file: string;
    /** The agent's skill of this name that this save replaces (or removes), as the file held it, or null. */
    before: string | null;
    /** The text to write, or null when the skill is removed. */
    after: string | null;
    /** A user-scope skill of the same name that a project skill would hide. */
    hides: boolean;
  };

/** The model's arguments as plain values, or the refusal. */
function skillProblem(name: string, description: string, instructions: string): string | null {
  if (!SKILL_NAME.test(name)) return `${NAME_RULE}, so nothing was saved.`;
  if (instructions === '') return null; // a removal needs nothing else
  if (!description) return 'A skill needs a one-line description, so nothing was saved.';
  if (description.length > SKILL_DESCRIPTION_MAX) {
    return `The description is ${description.length} characters; the limit is ${SKILL_DESCRIPTION_MAX}. Shorten it to one line, so nothing was saved.`;
  }
  if (instructions.length > SKILL_INSTRUCTIONS_MAX) {
    return `The instructions are ${instructions.length} characters; the limit is ${SKILL_INSTRUCTIONS_MAX.toLocaleString('en-US')}. Make them shorter, or split them into two skills, so nothing was saved.`;
  }
  for (const text of [description, instructions]) {
    if (/<\/?dlne_/i.test(text)) return 'A skill cannot contain a DLNE tag (<dlne_…>), so it was not saved.';
    if (HIDDEN_CHARACTERS.test(text)) return 'The skill holds invisible or direction-changing characters, so it was not saved.';
  }
  if (redactSecrets(`${description}\n${instructions}`).count > 0) return 'That skill holds a secret, so it was not saved.';
  return null;
}

/** Whether a file this large would be read whole by a save: more than DLNE loads is the user's to handle. */
const MAX_SKILL_FILE = SKILL_INSTRUCTIONS_MAX * 4 + 4096;

/**
 * Every check `saveSkill` makes before it writes, without writing: the tool asks it before it asks the user, so the user
 * is never asked about a skill that would be refused anyway. Empty `instructions` removes the agent's skill of that name.
 */
export function checkSaveSkill(
  args: { name?: unknown; description?: unknown; instructions?: unknown; scope?: unknown },
  root: string | undefined,
  options: MemoryOptions = {}
): SkillCheck {
  const refuse = (message: string): SkillCheck => ({ ready: false, result: { ok: false, message } });
  if (modeOf(options) === 'off') return refuse(SKILLS_OFF);
  const scope = args.scope;
  if (scope !== 'project' && scope !== 'user') return refuse('The scope must be "project" or "user".');
  if (scope === 'project' && !root) return refuse('There is no project folder open, so nothing was saved for a project.');
  if (typeof args.name !== 'string') return refuse('The skill name must be text.');
  if (args.description !== undefined && args.description !== null && typeof args.description !== 'string') return refuse('The description must be text.');
  if (args.instructions !== undefined && args.instructions !== null && typeof args.instructions !== 'string') return refuse('The instructions must be text.');

  const name = args.name.trim();
  const description = oneLine(typeof args.description === 'string' ? args.description : '');
  const instructions = (typeof args.instructions === 'string' ? args.instructions : '').replace(/\r\n?/g, '\n').trim();
  const problem = skillProblem(name, description, instructions);
  if (problem) return refuse(problem);

  const dir = skillsDirOf(scope, root, options) as string;
  const base = baseOf(scope, root, options) as string;
  const file = path.join(dir, name, 'SKILL.md');
  const reason = memoryExclusion(file, true);
  if (reason) return refuse(`The skill file (${file}) is excluded by DLNE's privacy rules (${reason}), so nothing was saved.`);
  if (leavesRoot(file, base)) {
    return refuse(`The skill file (${file}) leads outside ${scope === 'project' ? 'the project' : 'the home folder'} through a link, so nothing was saved.`);
  }

  // What is there now. A file DLNE did not write (no `by: dlne`, or one that does not parse) is the user's.
  let existing: string | null = null;
  try {
    if (fs.statSync(file).size > MAX_SKILL_FILE) existing = '';
    else existing = fs.readFileSync(file, 'utf-8');
  } catch (err: any) {
    if (err?.code !== 'ENOENT' && err?.code !== 'ENOTDIR') return refuse(`The skill file (${file}) could not be read (${err?.code ?? 'error'}), so nothing was saved.`);
  }
  const mine = existing !== null && !!(parseSkillFile(existing)?.byDlne);
  if (existing !== null && !mine) {
    return refuse(`A skill called "${name}" already exists and was not written by DLNE, so DLNE cannot change it. Ask the user, or choose another name.`);
  }

  if (instructions === '') {
    if (existing === null) return refuse(`No skill of yours is called "${name}" in ${scope === 'project' ? 'this project' : 'your skills'}, so nothing was removed.`);
    return { ready: true, scope, name, description, instructions, file, before: existing, after: null, hides: false };
  }

  const after = skillFileText(name, description, instructions);
  if (existing === after) return { ready: false, result: { ok: true, message: `Already saved: the skill "${name}" is as it is.` } };

  if (existing === null) {
    const all = scanScope(scope, root, options);
    if (all.length >= SKILLS_PER_SCOPE) {
      const yours = all.filter((s) => s.byDlne).map((s) => s.name);
      return refuse(`There are already ${SKILLS_PER_SCOPE} skills ${scope === 'project' ? 'for this project' : 'of yours'} (the most), so nothing was saved. ` +
        (yours.length > 0
          ? `Remove one that no longer matters (save_skill with empty instructions). Yours: ${yours.slice(0, 40).join(', ')}${yours.length > 40 ? ` and ${yours.length - 40} more` : ''}.`
          : 'None of them was saved by DLNE, so ask the user to tidy them.'));
    }
  }
  // A project skill of a name the user also has hides theirs; the user is asked first (see ToolRegistry.checkApproval).
  const hides = scope === 'project' && !sameFolder(dir, skillsDirOf('user', root, options)) && readSkill('user', name, root, options) !== null;
  return { ready: true, scope, name, description, instructions, file, before: existing, after, hides };
}

/** The first line of the model's answer for a change. */
function savedLine(kind: 'saved' | 'updated' | 'removed', scope: SkillScope, name: string): string {
  return `📘 Skill ${kind} ${scope === 'project' ? 'for this project' : 'for you'}: ${name}`;
}

/** Writes a skill file in one step, making its folders. The user's folders are private to the user. */
function writeSkillFile(file: string, scope: SkillScope, content: string): void {
  fs.mkdirSync(path.dirname(file), { recursive: true, mode: scope === 'user' ? 0o700 : 0o755 });
  writeAtomically(file, content, scope === 'user' ? 0o600 : 0o644);
}

/** Takes SKILL.md away, and its folder when nothing else is in it. */
function removeSkillFile(file: string): void {
  fs.unlinkSync(file);
  try { fs.rmdirSync(path.dirname(file)); } catch { /* the user keeps other files beside it */ }
}

/**
 * Saves, updates or removes one skill (any approval has already been given). Never throws: the result is the model's
 * answer, with the change the chat's Undo needs.
 */
export function saveSkill(
  args: { name?: unknown; description?: unknown; instructions?: unknown; scope?: unknown },
  root: string | undefined,
  options: MemoryOptions = {}
): { ok: boolean; message: string; change?: SkillChange } {
  const check = checkSaveSkill(args, root, options);
  if (!check.ready) return check.result;
  try {
    if (check.after === null) removeSkillFile(check.file);
    else writeSkillFile(check.file, check.scope, check.after);
  } catch (err: any) {
    return { ok: false, message: `The skill file (${check.file}) could not be written (${err?.code ?? 'error'}), so nothing was saved.` };
  }
  const kind = check.after === null ? 'removed' : check.before === null ? 'saved' : 'updated';
  return {
    ok: true,
    message: savedLine(kind, check.scope, check.name),
    change: { kind: 'skill', file: check.file, scope: check.scope, name: check.name, before: check.before, after: check.after },
  };
}

// ---- Undo -----------------------------------------------------------------------------------------------------

/**
 * Puts back a skill change (the chat's Undo). Only a DLNE skill file, only skills DLNE wrote, and only while the file
 * is still exactly what the change left: anything the user or the agent changed since is left alone.
 */
export function undoSkillChange(change: unknown): { ok: boolean; message: string } {
  const c = change as Partial<SkillChange> | null;
  const cannot = (why: string) => ({ ok: false, message: `Cannot undo: ${why}` });
  if (!c || typeof c !== 'object' || c.kind !== 'skill' || typeof c.file !== 'string' || typeof c.name !== 'string'
    || (c.scope !== 'project' && c.scope !== 'user') || !(c.before === null || typeof c.before === 'string')
    || !(c.after === null || typeof c.after === 'string') || (c.before === null && c.after === null)) {
    return cannot('that is not a change DLNE made.');
  }
  const { file, before, after } = c as SkillChange;
  const sep = '[\\\\/]';
  if (!new RegExp(`${sep}\\.dlne${sep}skills${sep}${SKILL_NAME.source.slice(1, -1)}${sep}SKILL\\.md$`).test(file)
    || path.basename(path.dirname(file)) !== c.name) {
    return cannot('that is not a DLNE skill file.');
  }
  for (const text of [before, after]) {
    if (text !== null && !parseSkillFile(text)?.byDlne) return cannot('that skill was not written by DLNE.');
  }
  let current: string | null = null;
  try {
    if (fs.lstatSync(file).isSymbolicLink()) return cannot('the skill file is a link.');
    current = fs.readFileSync(file, 'utf-8');
  } catch (err: any) {
    if (err?.code !== 'ENOENT') return cannot(`the skill file could not be read (${err?.code ?? 'error'}).`);
  }
  if (current !== after) {
    if (after === null && current === before) return { ok: true, message: `Already back: the skill "${c.name}".` };
    return cannot('the skill was changed since, so it was left as it is.');
  }
  try {
    if (before === null) removeSkillFile(file);
    else writeSkillFile(file, c.scope, before);
  } catch (err: any) {
    return cannot(`the skill file could not be written (${err?.code ?? 'error'}).`);
  }
  return before === null
    ? { ok: true, message: `Undone: the skill "${c.name}" is no longer saved.` }
    : { ok: true, message: `Undone: the skill "${c.name}" is back.` };
}

/** The chat's Undo for any change the agent made to its memory or its skills. */
export function undoChange(change: unknown): { ok: boolean; message: string } {
  return change && typeof change === 'object' && (change as { kind?: unknown }).kind === 'skill'
    ? undoSkillChange(change)
    : undoMemoryChange(change);
}

