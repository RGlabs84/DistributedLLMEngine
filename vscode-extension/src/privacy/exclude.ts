/**
 * Which files DLNE never reads for the model (docs/coding-members.md section 11).
 *
 * A file is excluded when it is
 *   - a credentials file by name or place: `.env*`, `*.pem`, `*.key`, `*.p12`, `id_rsa*`, `.npmrc`,
 *     `.aws/credentials`, `.docker/config.json`, kube configs, anything under `.git/` or `.ssh/`, ...;
 *   - ignored by one of the workspace's `.gitignore` files (or `.git/info/exclude`); or
 *   - ignored by a `.dlneignore` file, which has the same syntax. Its rules are applied after the
 *     `.gitignore` of the same folder, so a `!pattern` line there lets the model see a git-ignored
 *     file (for example generated sources). It can never re-include a credentials file.
 *
 * This runs on every autocomplete request, so everything is cached for a few seconds and the common
 * case (a file nobody ignores) costs a map lookup.
 */
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';

/** Anything with a file-system path: a `vscode.Uri`, or just a path string. */
export type UriLike = string | { fsPath?: string; path?: string; scheme?: string };

// ---------------------------------------------------------------------------------------------
// Credentials files, by name and by place
// ---------------------------------------------------------------------------------------------

/** A `.env` file that is a template (no real values) is not excluded: the model needs it. */
const ENV_TEMPLATE = /\.(?:example|sample|template|tmpl|dist|defaults?|schema|stub)$/;

/** Exact (lower-case) file names that hold credentials. */
const CREDENTIAL_NAMES = new Set([
  '.npmrc', '.pypirc', '.netrc', '_netrc', '.git-credentials', '.pgpass', '.my.cnf', '.htpasswd',
  '.dockercfg', '.s3cfg', '.boto', '.vault-pass', '.vault_pass', '.vault-token', 'credentials.json',
  'kubeconfig', 'kube.config', 'secrets.json', 'secrets.yml', 'secrets.yaml', 'secrets.toml',
  'secrets.ini', 'secret.json', 'secret.yml', 'secret.yaml', 'secret.toml', 'terraform.rc',
  'credentials.tfrc.json',
]);

/** Environment files: .env, .env.local, .envrc, .env.production, prod.env, secrets.env. */
const ENV_FILE: RegExp[] = [/^\.env/, /\.env$/];

/** File-name patterns (matched on the lower-case base name). */
const CREDENTIAL_PATTERNS: RegExp[] = [
  /\.(?:pem|key|p12|pfx|pkcs12|jks|keystore|p8|ppk|kdbx|gpg|pgp)$/,
  /^id_(?:rsa|dsa|ecdsa|ed25519)/,                       // SSH keys, with or without .pub
  /^kubeconfig/, /\.kubeconfig$/,
  /^client_secret.*\.json$/,                             // Google OAuth client secrets
  /^service[-_]?account.*\.json$/,                       // GCP service account keys
  /\.tfstate(?:\.backup)?$/, /\.tfvars(?:\.json)?$/,     // Terraform state and variables hold secrets
  /\.secrets?$/,
  /^wg\d*\.conf$/, /^dlne_tunnel.*\.conf$/,              // WireGuard configs hold private and preshared keys
];

/** Places (lower-case, forward slashes) whose contents are credentials, wherever they are. */
const CREDENTIAL_PLACES: RegExp[] = [
  /(?:^|\/)\.aws\/(?:credentials|sso\/cache\/)/,
  /(?:^|\/)\.docker\/config\.json$/,
  /(?:^|\/)\.kube\/(?:config|cache\/)/,
  /(?:^|\/)\.ssh\//,
  /(?:^|\/)\.gnupg\//,
  /(?:^|\/)\.password-store\//,
  /(?:^|\/)\.config\/gcloud\//,
  /(?:^|\/)\.config\/gh\/hosts\.yml$/,
  /(?:^|\/)\.azure\//,
  /(?:^|\/)\.cargo\/credentials(?:\.toml)?$/,
  /(?:^|\/)\.gem\/credentials$/,
  /(?:^|\/)\.m2\/settings(?:-security)?\.xml$/,
  /(?:^|\/)\.gradle\/gradle\.properties$/,
  /(?:^|\/)\.config\/pypoetry\/auth\.toml$/,
  /(?:^|\/)(?:wireguard|wg_configs)\//,
];

function forwardSlashes(p: string): string {
  return p.replace(/\\/g, '/');
}

/**
 * The copies an editor or a person leaves beside a file: `.env.bak`, `server.pem.old`, `id_rsa~`. They hold the
 * same secrets as the file they were made from.
 */
const BACKUP_SUFFIXES = /(?:\.(?:bak|backup|old|orig|save|swp|swo|tmp|copy)|~)$/;

/**
 * A name as the file system will read it. Windows drops trailing dots and spaces (`server.pem.` opens
 * server.pem), so those are not a way round the rules; and a backup copy is judged as the file it copies.
 */
function nameForRules(segment: string, isFileName: boolean): string {
  let name = segment;
  for (let i = 0; i < 8; i++) {
    const next = (isFileName ? name.replace(BACKUP_SUFFIXES, '') : name).replace(/[. ]+$/, (dots) => (name === dots ? dots : ''));
    if (next === name) break;
    name = next;
  }
  return name;
}

/** Why `p` (any path) is a credentials file, or null. Pure string work, no file-system access. */
function credentialReason(p: string): string | null {
  const lower = forwardSlashes(p).toLowerCase();
  const segments = lower.split('/').map((s, i, all) => nameForRules(s, i === all.length - 1));
  if (segments.includes('.git')) return 'inside a .git directory';
  const base = segments[segments.length - 1];
  if (ENV_FILE.some((re) => re.test(base))) {
    if (!ENV_TEMPLATE.test(base)) return 'an environment file that may hold secrets';
  } else if (CREDENTIAL_NAMES.has(base)) {
    return 'a credentials file';
  } else if (CREDENTIAL_PATTERNS.some((re) => re.test(base))) {
    return 'a key or credentials file';
  }
  const asRead = segments.join('/');
  for (const place of CREDENTIAL_PLACES) {
    if (place.test(asRead)) return 'a credentials location';
  }
  return null;
}

// ---------------------------------------------------------------------------------------------
// gitignore syntax
// ---------------------------------------------------------------------------------------------

interface IgnoreRule {
  re: GlobMatcher;
  negate: boolean;
  dirOnly: boolean;
  /** The file the rule came from, for the message ("ignored by .gitignore"). */
  source: string;
}

/** One folder's rules: `dir` is the folder the patterns are relative to (forward slashes). */
interface DirRules {
  dir: string;
  rules: IgnoreRule[];
}

const CASE_INSENSITIVE_FS = process.platform === 'win32' || process.platform === 'darwin';

function escapeRe(c: string): string {
  return /[\\^$.*+?()[\]{}|/-]/.test(c) ? '\\' + c : c;
}

/** Index of the `]` closing the class that opens at `open`, or -1 (then `[` is an ordinary character). */
function classEnd(glob: string, open: number): number {
  let j = open + 1;
  if (glob[j] === '!' || glob[j] === '^') j++;
  if (glob[j] === ']') j++;
  while (j < glob.length && glob[j] !== ']') j++;
  return j < glob.length ? j : -1;
}

/** gitignore glob -> regular expression source (matching a slash-separated relative path). */
function globToSource(glob: string): string {
  let out = '';
  let i = 0;
  while (i < glob.length) {
    const c = glob[i];
    if (c === '\\') {
      if (i + 1 < glob.length) {
        out += escapeRe(glob[i + 1]);
        i += 2;
      } else {
        out += '\\\\';
        i++;
      }
    } else if (c === '*') {
      if (glob[i + 1] === '*') {
        let end = i + 2;
        while (glob[end] === '*') end++;
        const afterSlash = i === 0 || glob[i - 1] === '/';
        if (afterSlash && glob[end] === '/') {
          out += '(?:.*/)?';                // "**/": any number of directories, including none
          i = end + 1;
        } else if (afterSlash && end >= glob.length) {
          out += '.*';                       // trailing "/**": everything inside
          i = end;
        } else {
          out += '[^/]*';                    // "a**b" behaves like "a*b"
          i = end;
        }
      } else {
        out += '[^/]*';
        i++;
      }
    } else if (c === '?') {
      out += '[^/]';
      i++;
    } else if (c === '[') {
      const close = classEnd(glob, i);
      if (close < 0) {
        out += '\\[';
        i++;
      } else {
        let body = glob.slice(i + 1, close);
        let negate = false;
        if (body[0] === '!' || body[0] === '^') {
          negate = true;
          body = body.slice(1);
        }
        body = body.replace(/\\/g, '\\\\').replace(/\]/g, '\\]').replace(/\[/g, '\\[');
        out += `(?!/)[${negate ? '^' : ''}${body}]`;
        i = close + 1;
      }
    } else {
      out += escapeRe(c);
      i++;
    }
  }
  return out;
}

// ---------------------------------------------------------------------------------------------
// Matching a glob: a regular expression, or, for a pattern with many stars, a matcher that cannot blow up
// ---------------------------------------------------------------------------------------------

export interface GlobMatcher {
  test(rel: string): boolean;
}

/** A pattern with this many `*` or more is matched by `wildcardMatcher`, not by a regular expression. */
export const WILDCARD_STARS = 4;

/** The regular-expression form (exact and fast for ordinary patterns; `*a*a*a*a*b` makes it take seconds). */
export function regexMatcher(glob: string, anchored: boolean): GlobMatcher {
  const pattern = globToSource(glob);
  return new RegExp(anchored ? `^${pattern}$` : `^(?:.*/)?${pattern}$`, CASE_INSENSITIVE_FS ? 'i' : '');
}

type GlobToken =
  | { kind: 'lit'; c: string }
  | { kind: 'one' }                       // ?
  | { kind: 'cls'; re: RegExp }           // [abc]: one character that is not a slash
  | { kind: 'star' }                      // *: any run of characters but a slash
  | { kind: 'dirs' }                      // **/ : any number of whole folders, or none
  | { kind: 'tail' };                     // trailing /** : anything

/** The tokens of a glob, read exactly as globToSource reads it. */
function globTokens(glob: string): GlobToken[] {
  const tokens: GlobToken[] = [];
  let i = 0;
  while (i < glob.length) {
    const c = glob[i];
    if (c === '\\') {
      if (i + 1 < glob.length) {
        tokens.push({ kind: 'lit', c: glob[i + 1] });
        i += 2;
      } else {
        tokens.push({ kind: 'lit', c: '\\' });
        i++;
      }
    } else if (c === '*') {
      let end = i + 1;
      if (glob[i + 1] === '*') {
        end = i + 2;
        while (glob[end] === '*') end++;
        const afterSlash = i === 0 || glob[i - 1] === '/';
        if (afterSlash && glob[end] === '/') {
          tokens.push({ kind: 'dirs' });
          i = end + 1;
          continue;
        }
        if (afterSlash && end >= glob.length) {
          tokens.push({ kind: 'tail' });
          i = end;
          continue;
        }
      }
      tokens.push({ kind: 'star' });
      i = end;
    } else if (c === '?') {
      tokens.push({ kind: 'one' });
      i++;
    } else if (c === '[') {
      const close = classEnd(glob, i);
      if (close < 0) {
        tokens.push({ kind: 'lit', c: '[' });
        i++;
      } else {
        let body = glob.slice(i + 1, close);
        let negate = false;
        if (body[0] === '!' || body[0] === '^') {
          negate = true;
          body = body.slice(1);
        }
        body = body.replace(/\\/g, '\\\\').replace(/\]/g, '\\]').replace(/\[/g, '\\[');
        tokens.push({ kind: 'cls', re: new RegExp(`^[${negate ? '^' : ''}${body}]$`, CASE_INSENSITIVE_FS ? 'i' : '') });
        i = close + 1;
      }
    } else {
      tokens.push({ kind: 'lit', c });
      i++;
    }
  }
  return tokens;
}

/**
 * `.` in a regular expression (as in `.*` for `**`): anything but a line break. `[^/]` does match one.
 * (The two quoted characters below are U+2028 LINE SEPARATOR and U+2029 PARAGRAPH SEPARATOR.)
 */
const isLineBreak = (ch: string) => ch === '\n' || ch === '\r' || ch === ' ' || ch === ' ';

/**
 * The same matching as the regular expression, done by walking the pattern's states alongside the text:
 * the work is (pattern tokens x text length) however many stars there are, where a regular expression
 * tries every way to split the text among them (a `.gitignore` of `*a*a*a*a*a*a*b` took seconds per file
 * name, and a repository can ship both the pattern and the name).
 */
export function wildcardMatcher(glob: string, anchored: boolean): GlobMatcher {
  // `**/` is "any characters, then a slash", the whole of it optional: a run of any characters followed by a
  // literal slash. Only arriving at the run may skip both; having taken a character, the slash is still owed.
  const tokens: Array<GlobToken | { kind: 'run' }> = [];
  const parts = globTokens(glob);
  if (!anchored) parts.unshift({ kind: 'dirs' });
  for (const part of parts) {
    if (part.kind === 'dirs') tokens.push({ kind: 'run' }, { kind: 'lit', c: '/' });
    else tokens.push(part);
  }
  const m = tokens.length;
  const same = CASE_INSENSITIVE_FS ? (a: string, b: string) => a.toLowerCase() === b.toLowerCase() : (a: string, b: string) => a === b;

  /** Arrive at token `t` from the one before it (or the start): tokens that can match nothing are passed through. */
  const enter = (set: Uint8Array, t: number) => {
    set[t] = 1;
    if (t >= m) return;
    if (tokens[t].kind === 'run') {
      set[t + 1] = 1;          // no characters in the run: the slash is next
      enter(set, t + 2);       // and the whole `**/` may match nothing
    } else if (tokens[t].kind === 'star' || tokens[t].kind === 'tail') {
      enter(set, t + 1);
    }
  };

  return {
    test(rel: string): boolean {
      let active = new Uint8Array(m + 1);
      enter(active, 0);
      for (let p = 0; p < rel.length; p++) {
        const ch = rel[p];
        const next = new Uint8Array(m + 1);
        let any = false;
        for (let s = 0; s < m; s++) {
          if (!active[s]) continue;
          const token = tokens[s];
          switch (token.kind) {
            case 'lit':
              if (same(ch, token.c)) { enter(next, s + 1); any = true; }
              break;
            case 'one':
              if (ch !== '/') { enter(next, s + 1); any = true; }
              break;
            case 'cls':
              if (ch !== '/' && token.re.test(ch)) { enter(next, s + 1); any = true; }
              break;
            case 'star':
              if (ch !== '/') { next[s] = 1; enter(next, s + 1); any = true; }
              break;
            case 'tail':
              if (!isLineBreak(ch)) { next[s] = 1; enter(next, s + 1); any = true; }     // a trailing `**` may stop anywhere
              break;
            case 'run':
              if (!isLineBreak(ch)) { next[s] = 1; next[s + 1] = 1; any = true; }        // one more character; the slash is still owed
              break;
          }
        }
        if (!any) return false;
        active = next;
      }
      return active[m] === 1;
    },
  };
}

/** A glob as a matcher: a regular expression, or the wildcard walker for a pattern with many stars. */
export function compileGlob(glob: string, anchored: boolean): GlobMatcher {
  const stars = (glob.match(/\*/g) || []).length;
  return stars >= WILDCARD_STARS ? wildcardMatcher(glob, anchored) : regexMatcher(glob, anchored);
}

/** One line of an ignore file -> a rule, or null for blanks, comments and unusable lines. */
function parseRule(rawLine: string, source: string): IgnoreRule | null {
  let line = rawLine.replace(/\r$/, '');
  if (!line || line.startsWith('#')) return null;
  line = line.replace(/(?<!\\)[ \t]+$/, '').replace(/\\ /g, ' ');
  if (!line) return null;
  let negate = false;
  if (line.startsWith('!')) {
    negate = true;
    line = line.slice(1);
  } else if (line.startsWith('\\!') || line.startsWith('\\#')) {
    line = line.slice(1);
  }
  let dirOnly = false;
  if (line.endsWith('/')) {
    dirOnly = true;
    line = line.replace(/\/+$/, '');
  }
  if (!line) return null;
  const anchored = line.includes('/');
  if (line.startsWith('/')) line = line.slice(1);
  if (!line) return null;
  try {
    const re = compileGlob(line, anchored);
    return re ? { re, negate, dirOnly, source } : null;
  } catch {
    return null; // a pattern we cannot compile is skipped, never fatal
  }
}

function parseIgnoreFile(text: string, source: string): IgnoreRule[] {
  const rules: IgnoreRule[] = [];
  for (const line of text.split('\n')) {
    const rule = parseRule(line, source);
    if (rule) rules.push(rule);
  }
  return rules;
}

// ---------------------------------------------------------------------------------------------
// Caches (a few seconds, so an edited .gitignore takes effect quickly and keystrokes stay cheap)
// ---------------------------------------------------------------------------------------------

const TTL_MS = 3000;
const MAX_IGNORE_FILE_BYTES = 1024 * 1024;
const MAX_CACHED_PATHS = 2000;

const rulesCache = new Map<string, { checked: number; signature: string; value: DirRules }>();
const gitRootCache = new Map<string, { checked: number; has: boolean }>();
const verdictCache = new Map<string, { checked: number; reason: string | null }>();

/** Forget everything cached (tests, and after settings change). */
export function clearPrivacyCaches(): void {
  rulesCache.clear();
  gitRootCache.clear();
  verdictCache.clear();
}

function statSignature(file: string): string {
  try {
    const s = fs.statSync(file);
    return `${s.mtimeMs}:${s.size}`;
  } catch {
    return '-';
  }
}

function hasGit(dir: string): boolean {
  const now = Date.now();
  const hit = gitRootCache.get(dir);
  if (hit && now - hit.checked < TTL_MS) return hit.has;
  let has = false;
  try {
    fs.statSync(path.join(dir, '.git'));
    has = true;
  } catch {
    has = false;
  }
  gitRootCache.set(dir, { checked: now, has });
  return has;
}

function readIgnoreFile(file: string): string {
  try {
    const s = fs.statSync(file);
    if (!s.isFile() || s.size > MAX_IGNORE_FILE_BYTES) return '';
    return fs.readFileSync(file, 'utf-8');
  } catch {
    return '';
  }
}

/** The ignore rules of one folder: .git/info/exclude (repository root only), .gitignore, then .dlneignore. */
function loadDirRules(dir: string, isRepoRoot: boolean): DirRules {
  const now = Date.now();
  const key = `${dir}\0${isRepoRoot ? 1 : 0}`;
  const files = [
    ...(isRepoRoot ? [path.join(dir, '.git', 'info', 'exclude')] : []),
    path.join(dir, '.gitignore'),
    path.join(dir, '.dlneignore'),
  ];
  const hit = rulesCache.get(key);
  if (hit && now - hit.checked < TTL_MS) return hit.value;
  const signature = files.map(statSignature).join('|');
  if (hit && hit.signature === signature) {
    hit.checked = now;
    return hit.value;
  }
  const rules: IgnoreRule[] = [];
  for (const f of files) rules.push(...parseIgnoreFile(readIgnoreFile(f), path.basename(f) === 'exclude' ? '.git/info/exclude' : path.basename(f)));
  // No trailing slash, so a repository at the file-system root ("/") still joins as "/x".
  const value: DirRules = { dir: forwardSlashes(dir).replace(/\/+$/, ''), rules };
  rulesCache.set(key, { checked: now, signature, value });
  return value;
}

// ---------------------------------------------------------------------------------------------
// Which folders' rules apply to a file
// ---------------------------------------------------------------------------------------------

/** Open workspace folders, when running inside VS Code (nothing otherwise, e.g. in unit tests). */
function workspaceRoots(): string[] {
  try {
    // Loaded lazily so this module can be used (and tested) outside the editor.
    const vscode = require('vscode');
    const folders = vscode?.workspace?.workspaceFolders;
    if (!Array.isArray(folders)) return [];
    return folders.map((f: any) => f?.uri?.fsPath).filter((p: any) => typeof p === 'string' && p);
  } catch {
    return [];
  }
}

function isInside(root: string, target: string): boolean {
  const rel = path.relative(root, target);
  return rel === '' || (!rel.startsWith('..') && !path.isAbsolute(rel));
}

/**
 * The folders whose ignore files govern `file`, shallowest first. That is every folder from the
 * repository root down to the file's folder; or, with no repository, from the workspace folder
 * down. A repository above the workspace is honoured, except the user's home directory, which is
 * often a dotfiles repository whose `*` rule would hide the whole workspace.
 */
function governingFolders(file: string, roots: string[]): DirRules[] {
  const workspace = roots
    .filter((r) => isInside(r, file))
    .sort((a, b) => b.length - a.length)[0];

  const dirs: string[] = [];        // deepest first
  let gitRoot: string | null = null;
  let dir = path.dirname(file);
  for (let depth = 0; depth < 64; depth++) {
    dirs.push(dir);
    if (hasGit(dir)) {
      gitRoot = dir;
      break;
    }
    const parent = path.dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }

  // A repository at the home directory or the drive root is a dotfiles repo, not this project's.
  const broad = gitRoot !== null && (gitRoot === os.homedir() || gitRoot === path.parse(gitRoot).root);
  let chain = dirs;
  if (workspace) {
    const wsIndex = dirs.indexOf(workspace);
    if (wsIndex >= 0 && (!gitRoot || broad)) chain = dirs.slice(0, wsIndex + 1);
  } else if (!gitRoot || broad) {
    return []; // outside every workspace and outside any project repository: no ignore files apply
  }

  const shallowFirst = chain.slice().reverse();
  return shallowFirst.map((d, i) => loadDirRules(d, gitRoot !== null && i === 0 && d === gitRoot));
}

/**
 * The file that ignores `file` (".gitignore", ".dlneignore", ".git/info/exclude"), or null. Parent
 * folders are considered one by one, as git does: an ignored folder hides everything under it.
 */
function ignoredBy(chain: DirRules[], file: string, fileIsDir: boolean): string | null {
  if (chain.length === 0) return null;
  const top = chain[0].dir;
  const target = forwardSlashes(file);
  const norm = (s: string) => (CASE_INSENSITIVE_FS ? s.toLowerCase() : s);
  if (!norm(target).startsWith(norm(top) + '/')) return null;
  const segments = target.slice(top.length + 1).split('/');

  let current = top;
  for (let i = 0; i < segments.length; i++) {
    current = `${current}/${segments[i]}`;
    const isDir = i === segments.length - 1 ? fileIsDir : true;
    let source: string | null = null;
    for (const entry of chain) {
      if (!norm(current).startsWith(norm(entry.dir) + '/')) continue;
      const rel = current.slice(entry.dir.length + 1);
      for (const rule of entry.rules) {
        if (rule.dirOnly && !isDir) continue;
        if (rule.re.test(rel)) source = rule.negate ? null : rule.source;
      }
    }
    // git cannot re-include a file whose folder is ignored, so the first ignored folder decides.
    if (source) return source;
  }
  return null;
}

// ---------------------------------------------------------------------------------------------
// The public check
// ---------------------------------------------------------------------------------------------

function toFsPath(uri: UriLike): string {
  if (typeof uri === 'string') return uri;
  if (!uri) return '';
  return (typeof uri.fsPath === 'string' && uri.fsPath) || (typeof uri.path === 'string' ? uri.path : '');
}

function reasonFor(file: string, scheme: string | undefined): string | null {
  const named = credentialReason(file);
  if (named) return named;
  if (scheme && scheme !== 'file') return null; // untitled buffers, output panels, virtual file systems
  let isDir = false;
  try {
    isDir = fs.statSync(file).isDirectory();
  } catch {
    // does not exist (yet): treat it as a file
  }
  const source = ignoredBy(governingFolders(file, workspaceRoots()), file, isDir);
  return source ? `ignored by ${source}` : null;
}

/**
 * Why DLNE must not read this file for the model, or null when it may. The reason is a short phrase
 * for the message shown to the model and the user.
 */
export function excludedReason(uri: UriLike): string | null {
  const file = toFsPath(uri);
  if (!file) return null;
  const scheme = typeof uri === 'string' ? undefined : uri.scheme;

  const now = Date.now();
  const cacheKey = `${scheme ?? ''}\0${file}`;
  const hit = verdictCache.get(cacheKey);
  if (hit && now - hit.checked < TTL_MS) return hit.reason;

  let reason = reasonFor(file, scheme);
  if (!reason) {
    // A symbolic link must not be a way round the rules: judge where it really points as well.
    try {
      const real = fs.realpathSync(file);
      if (real !== file) reason = reasonFor(real, scheme);
    } catch {
      // not an existing file: nothing to resolve
    }
  }

  if (verdictCache.size >= MAX_CACHED_PATHS) verdictCache.clear();
  verdictCache.set(cacheKey, { checked: now, reason });
  return reason;
}

/** True when the file is excluded for privacy. */
export function isExcluded(uri: UriLike): boolean {
  return excludedReason(uri) !== null;
}

/** What the model is told when it asks for an excluded file (never a silent failure). */
export function excludedNotice(displayPath: string, reason: string = 'a private file'): string {
  return (
    `This file is excluded for privacy (${reason}): ${displayPath}. DLNE does not read it and nothing from it is ` +
    'sent to the model. Do not retry; if you need something from it, ask the user to paste the part that is not secret.'
  );
}
