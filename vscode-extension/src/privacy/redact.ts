/**
 * Secrets never leave the editor (docs/coding-members.md section 11).
 *
 * `redactSecrets(text, fileName?)` replaces API keys, tokens, private keys and passwords with `[REDACTED]`
 * before any text is sent to TheWall or to a coding member (the file name is optional and only tells it
 * whether unquoted `name = value` pairs can be credentials). `isExcluded(uri)` says which files must not
 * be read for the model at all (see ./exclude.ts).
 *
 * What it finds, and why it is built this way:
 *
 *  1. Tokens with a distinctive shape (AWS `AKIA...`, GitHub `ghp_...`, Stripe `sk_live_...`, JWTs,
 *     PEM private keys, DLNE's own `dlne_sk_...`, ...). These come from the gitleaks default rule set,
 *     the high-signal ones only. A shape this specific is a secret wherever it appears, so these are
 *     replaced without looking at context.
 *  2. Credentials with no recognisable shape (`password = "hunter2"`, `API_TOKEN=...`, a password in a
 *     connection string, `Authorization: Bearer ...`). These are found by the NAME they are assigned to,
 *     and only the value is replaced. Placeholders, references (`process.env.X`, `${PASSWORD}`),
 *     type names and UI labels are left alone, because rewriting ordinary code is worse than useless.
 *
 *  There is deliberately no "looks random, so it is a secret" rule: hashes in lockfiles, UUIDs and
 *  base64 test data are everywhere in code and must survive.
 *
 * Cost: every pattern is compiled once, vendor patterns run only when their literal prefix occurs in
 * the text (a plain `includes`), and the name-based patterns start from a handful of literal words.
 * Redaction never changes the number of lines, so line numbers shown to the model stay true.
 */
import { clearPrivacyCaches, excludedNotice, excludedReason, isExcluded } from './exclude';

export { clearPrivacyCaches, excludedNotice, excludedReason, isExcluded };
export type { UriLike } from './exclude';

/** What replaces a secret. */
export const REDACTION_MARK = '[REDACTED]';

export interface RedactionResult {
  /** The text with every secret replaced by `[REDACTED]`. */
  text: string;
  /** How many secrets were replaced. */
  count: number;
}

// ---------------------------------------------------------------------------------------------
// Telling placeholders and code from real values (used by the name-based rules only)
// ---------------------------------------------------------------------------------------------

/** Words that are never a secret on their own: the value of `password: string` is a type, not a password. */
const PLACEHOLDER_WORDS = new Set([
  'password', 'passwd', 'pass', 'pwd', 'passphrase', 'secret', 'token', 'key', 'apikey', 'api_key', 'api-key',
  'secretkey', 'secret_key', 'changeme', 'change_me', 'change-me', 'changeit', 'example', 'test', 'testing',
  'dummy', 'sample', 'default', 'redacted', 'null', 'none', 'nil', 'undefined', 'true', 'false', 'yes', 'no',
  'string', 'str', 'number', 'int', 'integer', 'float', 'bool', 'boolean', 'bytes', 'any', 'object', 'unknown',
  'required', 'optional', 'include', 'omit', 'same-origin', 'empty', 'todo', 'tbd', 'foo', 'bar', 'baz',
  'username', 'localhost', 'enabled', 'disabled', 'hidden', 'masked', 'secure', 'unset', 'mypassword',
  'mysecret', 'mytoken', 'secretvalue', 'tokenvalue',
]);

const PLACEHOLDER_PATTERNS: RegExp[] = [
  /^(.)\1{3,}$/,                                                        // xxxxxxxx  ********  00000000
  /^[*x.#•\-_]{4,}$/i,
  /^(?:your|my|the|some|an?|insert|enter|put|add|replace)[_\- ]/i,       // your_api_key, my-secret
  /(?:^|[_\-. ])(?:here|example|placeholder|sample|dummy|fake|changeme|redacted|todo|xxx+)(?:$|[_\-. ])/i,
  /^<.*>$|^\{.*\}$|^\[.*\]$|^\(.*\)$/,                                  // <token> {token} [token]
  /\$\{|\{\{|\}\}|\{[^{}]*\}|%\(|%s|%d|\$\(|<%|#\{|\$[A-Za-z_]|^%[A-Za-z_]+%$|^@[A-Za-z_.]+@$/, // templates, f-strings, variables
  /^[A-Za-z_][\w.]*\[.*\]$/,                                            // Optional[str]: a type, not a value
  /^\.\.\.|\.\.\.$/,
  /^(?:process\.env|os\.environ|env\(|getenv|System\.getenv|import\.meta)/i,
];

function isPlaceholder(value: string): boolean {
  if (PLACEHOLDER_WORDS.has(value.toLowerCase())) return true;
  for (const re of PLACEHOLDER_PATTERNS) if (re.test(value)) return true;
  return false;
}

const IDENTIFIER_PATH = /^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*$/;

/**
 * `req.body.password`, `hashedPassword`, `password_hash`, `x25519.X25519PrivateKey`: a name in code, not
 * a literal secret. A bare word with a digit (`hunter2`) is not taken for a name; a dotted path always is.
 */
function isCodeReference(value: string): boolean {
  return IDENTIFIER_PATH.test(value) && (value.includes('.') || !/\d/.test(value));
}
/** `password_required`, `auth.invalid-password`: a message key or enum value, not a secret. */
const MESSAGE_KEY = /^[a-z]+(?:[_\-.][a-z]+)+$/;
const GOOGLE_APP_PASSWORD = /^[a-z]{4}(?: [a-z]{4}){3}$/;
/** `パスワード`, `Contraseña`, `Пароль`: a word in another language, as in a translation table. */
const TRANSLATED_LABEL = /^(?=[\p{L}\p{M}]*[^\x00-\x7F])[\p{L}\p{M}]+$/u;
/** `^1.2.3`, `workspace:*`, `https://...`, `/usr/bin`, `./relative`: a version, a link or a path. */
const NOT_A_SECRET_SHAPE = /^(?:(?:[~^<>=v]+\d+(?:\.[\dxX*]+)*|\d+(?:\.[\dxX*]+)+)(?:[-+][\w.]+)?|(?:workspace|npm|file|link|git\+?|github|portal|patch):.*|[a-z][a-z0-9+.\-]*:\/\/.*|\.{0,2}\/.*|~\/.*|[A-Za-z]:[\\/].*)$/;

/** Shannon entropy in bits per character. */
function entropy(s: string): number {
  const counts = new Map<string, number>();
  for (const c of s) counts.set(c, (counts.get(c) || 0) + 1);
  let h = 0;
  for (const n of counts.values()) {
    const p = n / s.length;
    h -= p * Math.log2(p);
  }
  return h;
}

/** Whether a value assigned to a `token`-like name looks generated, not like a word. */
function looksGenerated(value: string): boolean {
  if (/\d/.test(value) && /[A-Za-z]/.test(value) && value.length >= 8) return true;
  return value.length >= 24 && entropy(value) >= 4;
}

// ---------------------------------------------------------------------------------------------
// 1. Tokens with a distinctive shape
// ---------------------------------------------------------------------------------------------

interface ShapeRule {
  id: string;
  /** Literals of which at least one must occur in the text, or the pattern is not even tried. */
  needs: readonly string[];
  re: RegExp;
}

const shape = (id: string, needs: readonly string[], re: RegExp): ShapeRule => ({ id, needs, re });

const SHAPE_RULES: ShapeRule[] = [
  shape('anthropic-key', ['sk-ant-'], /\bsk-ant-[A-Za-z0-9]{2,12}-[A-Za-z0-9_\-]{20,}/g),
  shape('openai-project-key', ['sk-proj-', 'sk-svcacct-', 'sk-admin-', 'sk-or-v1-'], /(?<![\w-])sk-(?:proj|svcacct|admin|or-v1)-[A-Za-z0-9_\-]{20,}/g),
  shape('openai-key', ['sk-'], /(?<![\w-])sk-[A-Za-z0-9]{32,64}(?![\w-])/g),
  shape('aws-access-key-id', ['AKIA', 'ASIA', 'ABIA', 'ACCA', 'A3T'], /\b(?:A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA)[A-Z2-7]{16}\b/g),
  shape('gcp-api-key', ['AIza'], /\bAIza[0-9A-Za-z_\-]{35}(?![0-9A-Za-z_\-])/g),
  shape('gcp-oauth-client-secret', ['GOCSPX-'], /\bGOCSPX-[0-9A-Za-z_\-]{28}(?![0-9A-Za-z_\-])/g),
  shape('gcp-access-token', ['ya29.'], /\bya29\.[0-9A-Za-z_\-]{30,}/g),
  shape('azure-ad-client-secret', ['Q~'], /(?<![A-Za-z0-9_~.\-])[A-Za-z0-9_~.]{3}\dQ~[A-Za-z0-9_~.\-]{31,34}(?![A-Za-z0-9_~.\-])/g),
  shape('github-token', ['ghp_', 'gho_', 'ghu_', 'ghs_', 'ghr_'], /\bgh[pousr]_[A-Za-z0-9]{36,255}/g),
  shape('github-fine-grained-pat', ['github_pat_'], /\bgithub_pat_[A-Za-z0-9_]{22,255}/g),
  shape('gitlab-token', ['glpat-', 'glptt-', 'glrt-', 'gldt-', 'glcbt-', 'glimt-', 'glagent-', 'glsoat-', 'glft-', 'glffct-', 'glwt-', 'gloas-'],
    /\bgl(?:pat|ptt|rt|dt|cbt|imt|agent|soat|ft|ffct|wt|oas)-[A-Za-z0-9_\-]{20,}/g),
  shape('atlassian-token', ['ATATT3', 'ATCTT3', 'ATBB'], /\b(?:AT(?:ATT|CTT)3xFfGF0[A-Za-z0-9_\-=.]{30,}|ATBB[A-Za-z0-9_=.\-]{32,})/g),
  shape('slack-token', ['xox', 'xapp-'], /\b(?:xox[abposr]-[A-Za-z0-9\-]{10,}|xapp-\d-[A-Za-z0-9\-]{10,}|xoxe(?:\.xox[bp])?-\d-[A-Za-z0-9]{40,})/g),
  shape('slack-webhook', ['hooks.slack.com'], /https:\/\/hooks\.slack\.com\/(?:services|workflows|triggers)\/[A-Za-z0-9+\/_\-]{16,}/g),
  shape('discord-webhook', ['/api/webhooks/'], /https:\/\/(?:ptb\.|canary\.)?discord(?:app)?\.com\/api\/webhooks\/\d+\/[A-Za-z0-9_\-]{40,}/g),
  shape('discord-bot-token', ['.'], /(?<![\w.\-])[MNO][A-Za-z0-9_\-]{23,25}\.[A-Za-z0-9_\-]{6}\.(?=[A-Za-z0-9_\-]*\d)[A-Za-z0-9_\-]{27,38}(?![\w\-])/g),
  shape('telegram-bot-token', [':AA'], /(?<!\d)\d{8,10}:AA[A-Za-z0-9_\-]{33}(?![A-Za-z0-9_\-])/g), // also inside api.telegram.org/bot<token>/
  shape('stripe-key', ['sk_', 'rk_'], /\b(?:sk|rk)_(?:test|live|prod)_[A-Za-z0-9]{10,99}(?![A-Za-z0-9])/g),
  shape('stripe-webhook-secret', ['whsec_'], /\bwhsec_[A-Za-z0-9]{24,}/g),
  shape('huggingface-token', ['hf_'], /\bhf_[A-Za-z]{30,40}(?![A-Za-z])/g),
  shape('groq-key', ['gsk_'], /\bgsk_[A-Za-z0-9]{40,}/g),
  shape('perplexity-key', ['pplx-'], /\bpplx-[A-Za-z0-9]{40,}/g),
  shape('xai-key', ['xai-'], /\bxai-[A-Za-z0-9]{70,}/g),
  shape('nvidia-key', ['nvapi-'], /\bnvapi-[A-Za-z0-9_\-]{60,}/g),
  shape('langsmith-key', ['lsv2_'], /\blsv2_(?:pt|sk)_[a-f0-9]{32}(?:_[a-f0-9]{10})?/g),
  shape('replicate-token', ['r8_'], /\br8_[A-Za-z0-9]{37}(?![A-Za-z0-9])/g),
  shape('npm-token', ['npm_'], /\bnpm_[A-Za-z0-9]{36}(?![A-Za-z0-9])/g),
  shape('pypi-token', ['pypi-AgE'], /\bpypi-AgE[A-Za-z0-9_\-]{50,}/g),
  shape('docker-pat', ['dckr_pat_'], /\bdckr_pat_[A-Za-z0-9_\-]{20,}/g),
  shape('rubygems-token', ['rubygems_'], /\brubygems_[a-f0-9]{48}\b/g),
  shape('sendgrid-key', ['SG.'], /\bSG\.[A-Za-z0-9_\-]{22}\.[A-Za-z0-9_\-]{43}(?![A-Za-z0-9_\-])/g),
  shape('mailchimp-key', ['-us'], /\b[0-9a-f]{32}-us\d{1,2}\b/g),
  shape('mailgun-key', ['key-'], /(?<![\w-])key-[0-9a-f]{32}\b/g),
  shape('shopify-token', ['shpat_', 'shpca_', 'shppa_', 'shpss_'], /\bshp(?:at|ca|pa|ss)_[a-fA-F0-9]{32}\b/g),
  shape('square-token', ['sq0atp-', 'sq0csp-', 'EAAA'], /\b(?:sq0(?:atp|csp)-[A-Za-z0-9_\-]{22,43}|EAAA[A-Za-z0-9_\-]{60})(?![A-Za-z0-9_\-])/g),
  shape('braintree-token', ['access_token$'], /access_token\$(?:production|sandbox)\$[0-9a-z]{16}\$[0-9a-f]{32}/g),
  shape('digitalocean-token', ['_v1_'], /\bdo[por]_v1_[a-f0-9]{64}\b/g),
  shape('linear-key', ['lin_api_'], /\blin_api_[A-Za-z0-9]{40}\b/g),
  shape('postman-key', ['PMAK-'], /\bPMAK-[a-f0-9]{24}-[a-f0-9]{34}\b/g),
  shape('supabase-token', ['sbp_'], /\bsbp_[a-f0-9]{40}\b/g),
  shape('databricks-token', ['dapi'], /\bdapi[a-f0-9]{32}(?:-\d)?\b/g),
  shape('heroku-token', ['HRKU-'], /\bHRKU-AA[A-Za-z0-9_\-]{58}/g),
  shape('doppler-token', ['dp.'], /\bdp\.(?:pt|st|sa|ct|scim|audit)\.[A-Za-z0-9]{40,}/g),
  shape('sentry-token', ['sntry'], /\bsntry[su]_[A-Za-z0-9+\/=_\-]{40,}/g),
  shape('pulumi-token', ['pul-'], /\bpul-[a-f0-9]{40}\b/g),
  shape('grafana-token', ['glc_', 'glsa_'], /\b(?:glc_[A-Za-z0-9+\/=]{32,}|glsa_[A-Za-z0-9]{32}_[a-f0-9]{8})/g),
  shape('notion-token', ['secret_', 'ntn_'], /\b(?:secret_[A-Za-z0-9]{43}|ntn_[A-Za-z0-9]{40,})(?![A-Za-z0-9])/g),
  shape('vault-token', ['hvs.', 'hvb.'], /\bhv[sb]\.[A-Za-z0-9_\-]{24,}/g),
  shape('terraform-cloud-token', ['.atlasv1.'], /\b[A-Za-z0-9]{14}\.atlasv1\.[A-Za-z0-9_\-=]{60,70}/g),
  shape('airtable-token', ['pat'], /\bpat[A-Za-z0-9]{14}\.[a-f0-9]{64}\b/g),
  shape('fly-token', ['fo1_', 'FlyV1'], /\b(?:fo1_[A-Za-z0-9_\-]{40,}|FlyV1 fm\d_[A-Za-z0-9+\/=_\-]{40,})/g),
  shape('dlne-key', ['dlne_'], /(?<![A-Za-z0-9])dlne_[a-z]{2}_[A-Za-z0-9_\-]{16,}/g),
  shape('jwt', ['eyJ'], /\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}(?:\.[A-Za-z0-9_\-]*)?/g),
];

/** Private-key blocks span lines; they are replaced by one mark plus blank lines, keeping line numbers. */
const PRIVATE_KEY_BLOCK = /-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----[A-Za-z0-9+\/=\s\\:,\-]{16,}?(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----|$)/g;
/** A block whose END line is missing (cut off by a size limit): the header plus the base64 lines after it. */
const PRIVATE_KEY_OPEN = /-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----(?:[ \t]*(?:\r?\n|\\n)[ \t]*(?:[A-Za-z0-9+\/=]{16,}|[A-Za-z-]+: [^\r\n]*))+/g;

// ---------------------------------------------------------------------------------------------
// 2. Credentials found by the name they are assigned to
// ---------------------------------------------------------------------------------------------

/**
 * A key name ends with one of these words ("db_password", "clientSecret", "OPENAI_API_KEY", "_authToken").
 * A bare "pass" is only a name after a separator ("db_pass"): on its own it is also the PASS of
 * `ok ? "PASS" : "FAIL"` and of "confirmed this pass: ...".
 */
const NAME_WORDS =
  'pass(?:word|wd|phrase|code)|pwd|(?<=[_\\-.])pass|secret[_\\-.]?key[_\\-.]?base|secret(?:[_\\-.]?(?:access[_\\-.]?)?key)?|' +
  'api[_\\-.]?(?:key|secret)|apikey|access[_\\-.]?key|auth[_\\-.]?key|private[_\\-.]?key|signing[_\\-.]?key|' +
  'encryption[_\\-.]?key|master[_\\-.]?key|hmac[_\\-.]?key|license[_\\-.]?key|account[_\\-.]?key|shared[_\\-.]?key|preshared[_\\-.]?key|' +
  'network[_\\-.]?key|admin[_\\-.]?hash|token'; // network key and admin hash: DLNE's config.txt

type NameFamily = 'password' | 'token' | 'secret';

function familyOf(word: string): NameFamily {
  const w = word.toLowerCase();
  if (w.startsWith('pass') || w === 'pwd') return 'password';
  return w === 'token' ? 'token' : 'secret';
}

const isDigit = (c: number) => c >= 48 && c <= 57;
const isUpper = (c: number) => c >= 65 && c <= 90;
const isLower = (c: number) => c >= 97 && c <= 122;

/** Names that look like a secret's but are not one: .NET `PublicKeyToken=...`, `nextPageToken`, `syncToken`. */
const NOT_A_SECRET_NAME = /(?:public[_\-.]?key|page|next|prev|previous|continuation|sync|cursor)[_\-.]?$/i;

/**
 * The name word at `at` starts a name (`db_password`, `dbPassword`, `"password"`, `PGPASSWORD`) rather than
 * ending a longer word (`jsonwebtoken`, `compass`), and the name is not a known non-secret.
 */
function startsName(text: string, at: number, word: string): boolean {
  if (at === 0) return true;
  if (NOT_A_SECRET_NAME.test(text.slice(Math.max(0, at - 16), at))) return false;
  const prev = text.charCodeAt(at - 1);
  if (!(isDigit(prev) || isUpper(prev) || isLower(prev))) return true;
  if (isLower(prev) && isUpper(text.charCodeAt(at))) return true;       // dbPassword
  // An all-capitals name may run its words together: PGPASSWORD, MYSQLPASSWORD, GITHUBTOKEN.
  return isUpper(prev) && word.length >= 5 && word === word.toUpperCase();
}

/** Whether a literal assigned to a name of this family is a secret worth hiding. */
function worthHiding(family: NameFamily, value: string, quoted: boolean): boolean {
  const minimum = family === 'password' ? (quoted ? 4 : 6) : 8;
  if (value.length < minimum) return false;
  // Only a Google app password ("abcd efgh ijkl mnop") is a secret with spaces in it.
  if (/\s/.test(value) && !(quoted && GOOGLE_APP_PASSWORD.test(value))) return false;
  if (isPlaceholder(value) || NOT_A_SECRET_SHAPE.test(value)) return false;
  if (quoted ? MESSAGE_KEY.test(value) || TRANSLATED_LABEL.test(value) : isCodeReference(value)) return false;
  if (family === 'token' && !looksGenerated(value)) return false;
  return true;
}

// `password = "hunter2"`, `"api_key": "..."`, `connect(password='...')`, `token := "..."`
const QUOTED_ASSIGNMENT = new RegExp(
  String.raw`(${NAME_WORDS})(["']?\]?[ \t]*(?:=>|:=|=|:)[ \t]*[rRbBfFuU@]{0,2})(["'\`])((?:[^"'\`\\\r\n]|\\.){4,500})\3`,
  'gi'
);

// A value written without quotes is a literal only in config formats, so these two are skipped for
// source-code files (where `password = hash(x)` or `password: Type` is code, not a secret).
//
// `DB_PASSWORD=Tr0ub4dor&3` at the start of a line (.env, shell, ini, properties, docker-compose): the
// value is the rest of the line, so a `&` or `;` inside a password does not cut it short
const ENV_LINE_ASSIGNMENT = new RegExp(
  String.raw`^([ \t]*(?:export[ \t]+|-[ \t]+)?["']?[A-Za-z0-9_.\-]*?)(${NAME_WORDS})(["']?\]?[ \t]*=[ \t]*)(?![\s*|>!@%$:"'\`\[\]{}()<>,#=])([^\s"'\`()\[\]{}<>]{6,500})(?=[ \t]+#|[ \t]*$)`,
  'gim'
);
// `docker run -e API_TOKEN=abc123def`, `PGPASSWORD=abc psql`, `?token=...` (a URL query): no spaces around
// the `=`, and the value ends at the first delimiter. (`password = value` in the middle of a line is code.)
const BARE_ASSIGNMENT = new RegExp(
  String.raw`(${NAME_WORDS})(["']?\]?=(?![=>~]))(?![\s*&|>!@%$:"'\`\[\]{}()<>,;#=])([^\s"'\`,;&()\[\]{}<>\\|]{6,200})(?=[\s;,&)\]}"'#<]|$)`,
  'gi'
);
// `password: hunter2` (yaml, ini): the name starts its line and the value runs to the end of it
const YAML_ASSIGNMENT = new RegExp(
  String.raw`^([ \t]*(?:-[ \t]+)?["']?[A-Za-z0-9_.\-]*?)(${NAME_WORDS})(["']?:[ \t]+)(?![\s*&|>!@%$:"'\`\[\]{}()<>,;#=])([^\s"'\`,;&()\[\]{}<>\\|]{6,200})(?=[ \t]*(?:#.*)?$)`,
  'gim'
);

/** Source-code languages. Config and shell formats (yaml, ini, env, sh, Dockerfile, ...) are not in the list. */
const SOURCE_CODE_FILE = /\.(?:py|pyi|pyw|ts|tsx|mts|cts|js|jsx|mjs|cjs|go|java|kt|kts|scala|cs|fs|vb|rs|c|h|cc|cpp|cxx|hpp|m|mm|swift|rb|php|lua|dart|r|jl|ex|exs|erl|hs|ml|clj|groovy|gradle|sql|vue|svelte)$/i;

// <password>hunter2</password>, <clientSecret>...</clientSecret> (Maven settings, Spring, .NET config)
const XML_ELEMENT = new RegExp(
  String.raw`(<[A-Za-z0-9_.:\-]*?(?:${NAME_WORDS})>)([^<>\r\n]{4,200})(?=<\/)`,
  'gi'
);
const XML_NAME_END = new RegExp(String.raw`(${NAME_WORDS})>$`, 'i');

// A password inside a connection string or URL: postgres://user:PASSWORD@host, redis://:PASSWORD@host
const URL_CREDENTIALS = /\b([a-z][a-z0-9+.\-]{1,15}:\/\/[^\s:@\/?#"'<>`\\]*:)([^\s@\/?#"'<>`\\]{3,})(?=@)/gi;

// Authorization: Bearer <token> / Basic <base64> / Token <token>
const AUTH_HEADER = /\b((?:Proxy-)?Authorization["']?[ \t]*[:=][ \t]*["']?(?:Bearer|Basic|Token|Digest|ApiKey)[ \t]+)([A-Za-z0-9._~+\/=\-]{16,})/gi;
const BEARER_BARE = /\b(Bearer[ \t]+)([A-Za-z0-9._~+\/=\-]{32,})/g;

// Cloud connection strings: AccountKey=..., SharedAccessKey=..., and ?sig=... in a shared-access URL
const AZURE_KEY = /\b((?:AccountKey|SharedAccessKey|SharedSecretValue|SharedAccessSignature)[ \t]*=[ \t]*)([A-Za-z0-9+\/=%_\-]{20,})/g;
const SAS_SIGNATURE = /([?&;]sig=)([A-Za-z0-9%+\/=_\-]{30,})/g;

// Credentials given to well-known tools and protocols: curl -u user:PASSWORD, sshpass -p PASSWORD,
// mysql -pPASSWORD, a netrc line, an API-key HTTP header, a Cookie header
const TOOL_PASSWORDS: Array<{ needs: readonly string[]; re: RegExp }> = [
  // The gap between the command and its flag is bounded: an unbounded lazy scan to the end of the line,
  // started again at every "curl" on that line, is quadratic (200 KB of "curl curl ..." froze the editor for seconds).
  { needs: ['curl'], re: /(\bcurl\b[^\n]{0,2000}?[ \t](?:-u|--user)[ \t]+['"]?[^\s:'"$]+:)([^\s'"$]{3,})/g },
  { needs: ['sshpass'], re: /(\bsshpass\b[ \t]+-p[ \t]*['"]?)([^\s'"$]{3,})/g },
  { needs: ['mysql'], re: /(\b(?:mysql|mysqldump|mysqladmin)\b[^\n]{0,2000}?[ \t]-p)([^\s'"$\-][^\s'"$]{2,})/g },
  { needs: ['machine'], re: /(\bmachine[ \t]+\S+[ \t]+login[ \t]+\S+[ \t]+password[ \t]+)(\S{3,})/g },
  { needs: ['SecureString'], re: /(\bConvertTo-SecureString[ \t]+["'])([^"'\r\n$]{3,})(?=["'][ \t]+-AsPlainText)/gi },
  {
    needs: ['-Key', '-key', '-KEY', '-Token', '-token', '-TOKEN', '-Secret', '-secret'],
    re: /\b((?:x-api-key|api-key|x-auth-token|x-access-token|x-api-token|x-token|private-token|x-auth-key|x-goog-api-key|ocp-apim-subscription-key|x-vault-token|x-consul-token|x-amz-security-token|x-secret)["']?[ \t]*:[ \t]*["']?)([A-Za-z0-9._~+\/=\-]{16,})/gi,
  },
  // A cookie header is hidden whole when a value-sized run of characters occurs in its first 400. The look-ahead
  // is bounded and the value is consumed once: the old "anything, a run, anything" form was quadratic on a line
  // of repeated "Cookie:" (200 KB took 8 s, 400 KB took 35 s).
  { needs: ['ookie'], re: /(\b(?:Set-)?Cookie["']?[ \t]*:[ \t]*["']?)(?=[^\r\n"']{0,400}?[A-Za-z0-9%._~+\/=\-]{16})([^\r\n"']+)/gi },
];

// Command-line flags: --password hunter2, --token=abc123, --api-key abc123
const CLI_FLAG = /((?:^|[\s'"])--?(?:password|passwd|pwd|token|secret|client-secret|api-?key|access-?token|auth-?token)(?:=|[ \t]+))([^\s"'`$<>|;&()\-][^\s"'`$<>|;&()]{5,199})/gim;

// ---------------------------------------------------------------------------------------------
// The redactor
// ---------------------------------------------------------------------------------------------

/** Any name word at all? Text without one skips every name-based pattern. */
const HAS_KEYWORD = new RegExp(NAME_WORDS, 'i');

/** Blank lines standing in for a multi-line block, so the text keeps its line count. */
function blankLinesFor(block: string): string {
  let newlines = 0;
  for (let i = block.indexOf('\n'); i !== -1; i = block.indexOf('\n', i + 1)) newlines++;
  return REDACTION_MARK + '\n'.repeat(newlines);
}

/**
 * Replace every secret in `text` with `[REDACTED]`.
 *
 * `fileName` is optional: when the text is a source-code file, unquoted `name = value` pairs are not
 * treated as credentials (they are expressions there). Everything else applies to any text.
 */
export function redactSecrets(text: string, fileName?: string): RedactionResult {
  if (typeof text !== 'string' || text.length < 8) return { text, count: 0 };
  let count = 0;
  let out = text;
  const isSourceCode = !!fileName && SOURCE_CODE_FILE.test(fileName);

  // Private keys first: their base64 body must not be mistaken for anything else.
  if (out.includes('PRIVATE KEY')) {
    out = out.replace(PRIVATE_KEY_BLOCK, (block) => { count++; return blankLinesFor(block); });
    out = out.replace(PRIVATE_KEY_OPEN, (block) => { count++; return blankLinesFor(block); });
  }

  for (const rule of SHAPE_RULES) {
    let present = false;
    for (const needle of rule.needs) {
      if (out.includes(needle)) { present = true; break; }
    }
    if (!present) continue;
    out = out.replace(rule.re, () => { count++; return REDACTION_MARK; });
  }

  if (out.includes('://')) {
    out = out.replace(URL_CREDENTIALS, (match, head: string, password: string) => {
      if (isPlaceholder(password)) return match;
      count++;
      return head + REDACTION_MARK;
    });
  }
  if (out.includes('uthorization') || out.includes('UTHORIZATION')) {
    out = out.replace(AUTH_HEADER, (match, head: string, value: string) => {
      if (isPlaceholder(value)) return match;
      count++;
      return head + REDACTION_MARK;
    });
  }
  if (out.includes('Bearer')) {
    out = out.replace(BEARER_BARE, (match, head: string, value: string) => {
      if (isPlaceholder(value) || !looksGenerated(value)) return match;
      count++;
      return head + REDACTION_MARK;
    });
  }
  if (out.includes('Key') || out.includes('Signature') || out.includes('SecretValue')) {
    out = out.replace(AZURE_KEY, (match, head: string, value: string) => {
      if (isPlaceholder(value) || !/[0-9+\/=]/.test(value)) return match;
      count++;
      return head + REDACTION_MARK;
    });
  }
  if (out.includes('sig=')) {
    out = out.replace(SAS_SIGNATURE, (_m, head: string) => { count++; return head + REDACTION_MARK; });
  }
  for (const tool of TOOL_PASSWORDS) {
    if (!tool.needs.some((needle) => out.includes(needle))) continue;
    out = out.replace(tool.re, (match, head: string, value: string) => {
      if (isPlaceholder(value) || isCodeReference(value)) return match;
      count++;
      return head + REDACTION_MARK;
    });
  }

  if (HAS_KEYWORD.test(out)) {
    out = out.replace(QUOTED_ASSIGNMENT, (match, word: string, glue: string, quote: string, value: string, at: number, whole: string) => {
      if (!startsName(whole, at, word) || !worthHiding(familyOf(word), value, true)) return match;
      count++;
      return word + glue + quote + REDACTION_MARK + quote;
    });
    if (!isSourceCode) {
      out = out.replace(ENV_LINE_ASSIGNMENT, (match, head: string, word: string, glue: string, value: string, at: number, whole: string) => {
        if (!startsName(whole, at + head.length, word) || !worthHiding(familyOf(word), value, false)) return match;
        count++;
        return head + word + glue + REDACTION_MARK;
      });
      out = out.replace(BARE_ASSIGNMENT, (match, word: string, glue: string, value: string, at: number, whole: string) => {
        if (!startsName(whole, at, word) || !worthHiding(familyOf(word), value, false)) return match;
        count++;
        return word + glue + REDACTION_MARK;
      });
      out = out.replace(YAML_ASSIGNMENT, (match, head: string, word: string, glue: string, value: string, at: number, whole: string) => {
        if (!startsName(whole, at + head.length, word) || !worthHiding(familyOf(word), value, false)) return match;
        count++;
        return head + word + glue + REDACTION_MARK;
      });
    }
    if (out.includes('</')) {
      out = out.replace(XML_ELEMENT, (match, open: string, value: string) => {
        const name = XML_NAME_END.exec(open);
        if (!worthHiding(familyOf(name ? name[1] : 'secret'), value.trim(), true)) return match;
        count++;
        return open + REDACTION_MARK;
      });
    }
    if (out.includes('-')) {
      out = out.replace(CLI_FLAG, (match, head: string, value: string) => {
        // `--password option` in help text is prose; `--password hunter2` has a digit or symbol.
        if (!/[^A-Za-z]/.test(value) || isPlaceholder(value) || NOT_A_SECRET_SHAPE.test(value)) return match;
        if (isCodeReference(value)) return match;
        count++;
        return head + REDACTION_MARK;
      });
    }
  }

  return { text: out, count };
}

/**
 * The line appended to a tool result that had secrets removed, so the model understands `[REDACTED]`
 * and does not write it back into the user's files. Empty when nothing was removed.
 */
export function redactionNote(count: number): string {
  return count > 0
    ? `\n\n[DLNE hid ${count} secret value${count === 1 ? '' : 's'} above as ${REDACTION_MARK} for privacy. They still exist in the file; never write ${REDACTION_MARK} into a file.]`
    : '';
}
