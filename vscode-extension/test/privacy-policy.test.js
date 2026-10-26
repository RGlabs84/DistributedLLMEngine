'use strict';
/**
 * The privacy mode of a workspace (src/privacy/policy.ts, docs/coding-members.md section 7).
 *
 * Public code may run on any coding member; everything else stays on trusted machines. The rule that
 * matters most is the last one: any doubt is `trusted`. These tests cover how a remote URL is read,
 * what each host's answer means, what is cached and for how long, what the setting overrides, and
 * that the lookup carries nothing that identifies the user.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');

const stub = require('./helpers/vscode-stub');
const policy = require('../dist/privacy/policy');
const pkg = require('../package.json');

const ROOT = path.join(__dirname, '..');
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-privacy-'));
test.after(() => fs.rmSync(TMP, { recursive: true, force: true }));

test.beforeEach(() => {
  stub.reset();
  stub.vscode.workspace.workspaceFolders = undefined;
  policy.resetPrivacyCache();
});

let counter = 0;
function tempDir() {
  const dir = path.join(TMP, `w${counter++}`);
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

const CONFIG_HEAD = '[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n';
const CONFIG_TAIL = '[branch "main"]\n\tremote = origin\n\tmerge = refs/heads/main\n';

/** A folder that is a git repository whose only remote is `origin` at `url`. */
function repoWithRemote(url) {
  const dir = tempDir();
  fs.mkdirSync(path.join(dir, '.git'));
  const remote = url === undefined ? '' : `[remote "origin"]\n\turl = ${url}\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n`;
  fs.writeFileSync(path.join(dir, '.git', 'config'), CONFIG_HEAD + remote + CONFIG_TAIL);
  return dir;
}

function repoWithConfig(text) {
  const dir = tempDir();
  fs.mkdirSync(path.join(dir, '.git'));
  fs.writeFileSync(path.join(dir, '.git', 'config'), text);
  return dir;
}

/** A stand-in for the real GET: `answer(url)` returns {status, body}, throws, or never settles. */
function fakeGet(answer) {
  const calls = [];
  const httpGet = async (url, headers, timeoutMs) => {
    calls.push({ url, headers, timeoutMs });
    return answer(url, headers);
  };
  return { httpGet, calls };
}

const json = (status, body) => ({ status, body: JSON.stringify(body) });
const githubRepo = (over = {}) => ({ full_name: 'octo/hello', private: false, visibility: 'public', fork: false, ...over });
const gitlabProject = (over = {}) => ({ path_with_namespace: 'grp/sub/proj', visibility: 'public', repository_access_level: 'enabled', ...over });
const codebergRepo = (over = {}) => ({ full_name: 'octo/hello', private: false, internal: false, ...over });

// ---- remote URLs -----------------------------------------------------------------------------

test('github, gitlab and codeberg remotes are recognised in https, ssh and scp forms', () => {
  const good = [
    ['https://github.com/octo/hello.git', 'github.com', 'octo/hello'],
    ['https://github.com/octo/hello', 'github.com', 'octo/hello'],
    ['https://github.com/octo/hello/', 'github.com', 'octo/hello'],
    ['https://github.com/octo/hello.git/', 'github.com', 'octo/hello'],
    ['  https://github.com/octo/hello.git\n', 'github.com', 'octo/hello'],
    ['https://GitHub.com/Octo/Hello.git', 'github.com', 'Octo/Hello'],
    ['http://github.com/octo/hello.git', 'github.com', 'octo/hello'],
    ['git://github.com/octo/hello.git', 'github.com', 'octo/hello'],
    ['git@github.com:octo/hello.git', 'github.com', 'octo/hello'],
    ['git@github.com:octo/hello', 'github.com', 'octo/hello'],
    ['org-1234@github.com:octo/hello.git', 'github.com', 'octo/hello'],
    ['ssh://git@github.com/octo/hello.git', 'github.com', 'octo/hello'],
    ['ssh://git@github.com:22/octo/hello.git', 'github.com', 'octo/hello'],
    ['https://github.com/octo/.github', 'github.com', 'octo/.github'],
    ['https://github.com/octo/hello.js.git', 'github.com', 'octo/hello.js'],
    ['https://github.com/octo-cat/hello_world-2.git', 'github.com', 'octo-cat/hello_world-2'],
    ['https://gitlab.com/grp/proj.git', 'gitlab.com', 'grp/proj'],
    ['https://gitlab.com/grp/sub/proj.git', 'gitlab.com', 'grp/sub/proj'],
    ['https://gitlab.com/a/b/c/d/proj', 'gitlab.com', 'a/b/c/d/proj'],
    ['git@gitlab.com:grp/sub/proj.git', 'gitlab.com', 'grp/sub/proj'],
    ['ssh://git@gitlab.com/grp/proj.git', 'gitlab.com', 'grp/proj'],
    ['https://codeberg.org/octo/hello.git', 'codeberg.org', 'octo/hello'],
    ['git@codeberg.org:octo/hello.git', 'codeberg.org', 'octo/hello'],
    ['ssh://git@codeberg.org/octo/hello.git', 'codeberg.org', 'octo/hello'],
  ];
  for (const [url, host, repoPath] of good) {
    assert.deepEqual(policy.parseRemoteUrl(url), { host, path: repoPath }, url);
  }
});

test('a token or user name in a remote URL is dropped, never kept', () => {
  const ref = policy.parseRemoteUrl('https://oauth2:ghp_SECRET0123456789@github.com/octo/hello.git');
  assert.deepEqual(ref, { host: 'github.com', path: 'octo/hello' });
  assert.ok(!JSON.stringify(ref).includes('SECRET'));
  assert.deepEqual(policy.parseRemoteUrl('https://x-access-token:abc@gitlab.com/grp/proj.git'), { host: 'gitlab.com', path: 'grp/proj' });
});

test('enterprise hosts, look-alikes and every other host are not recognised', () => {
  const bad = [
    // enterprise and self-hosted
    'https://github.mycorp.com/octo/hello.git',
    'git@github.mycorp.com:octo/hello.git',
    'https://gitlab.example.com/grp/proj.git',
    'https://gitlab.mycorp.io/grp/proj.git',
    'https://git.company.internal/octo/hello.git',
    'https://codeberg.example.org/octo/hello.git',
    'https://bitbucket.org/octo/hello.git',
    'https://dev.azure.com/org/proj/_git/repo',
    'https://sourcehut.org/~octo/hello',
    'https://gitea.com/octo/hello.git',
    // look-alikes
    'https://github.com.evil.com/octo/hello.git',
    'git@github.com.evil.com:octo/hello.git',
    'https://evilgithub.com/octo/hello.git',
    'https://notgithub.com/octo/hello.git',
    'https://www.github.com/octo/hello.git',
    'https://github.com./octo/hello.git',
    'https://api.github.com/repos/octo/hello',
    'https://gist.github.com/octo/abc123',
    'https://github.com@evil.com/octo/hello.git',
    'https://github.com:token@evil.com/octo/hello.git',
    'https://evil.com/github.com/octo/hello.git',
    'https://evil.com/?u=github.com/octo/hello',
    'https://githυb.com/octo/hello.git',
    'ssh://git@evil.com/github.com/octo/hello.git',
    // odd ports and schemes
    'https://github.com:8443/octo/hello.git',
    'ssh://git@github.com:2222/octo/hello.git',
    'ssh://git@github.com:octo/hello.git',
    'ftp://github.com/octo/hello.git',
    'file:///srv/git/hello.git',
    'ext::sh -c evil',
    // not an owner/repo path
    'https://github.com/',
    'https://github.com/octo',
    'https://github.com/octo/hello/tree/main',
    'https://github.com/octo/hello/issues',
    'https://github.com//octo/hello.git',
    'https://github.com/octo//hello.git',
    'https://github.com/octo/hello/.git',
    'https://github.com/../hello',
    'https://github.com/octo/..',
    'https://github.com/./hello',
    'https://github.com/octo/hel lo.git',
    'https://github.com/octo/hello.git?x=1',
    'https://github.com/octo/hello.git#frag',
    'https://github.com/oc%74o/hello.git',
    'https://github.com/octo%2Fhello',
    'https://gitlab.com/onlyone',
    `https://gitlab.com/${Array.from({ length: 21 }, (_, i) => `g${i}`).join('/')}`,
    'https://codeberg.org/octo/hello/extra',
    'https://codeberg.org/octo',
    // not a URL at all
    '', '   ', '../hello.git', '/srv/git/hello.git', './hello', 'C:/repos/hello', 'C:\\repos\\hello',
    'github.com', 'octo/hello', 'https://github.com/octo/hello.git\nhttps://evil.com/x', 'https://github.com/octo/hello\\..\\x',
    'https://github.com/' + 'a'.repeat(600),
  ];
  for (const url of bad) {
    assert.equal(policy.parseRemoteUrl(url), undefined, JSON.stringify(url.slice(0, 80)));
  }
  for (const value of [undefined, null, 42, {}, [], true]) {
    assert.equal(policy.parseRemoteUrl(value), undefined);
  }
});

test('repositories are cached under a key that ignores case', () => {
  const a = policy.repoKey(policy.parseRemoteUrl('https://github.com/Octo/Hello.git'));
  const b = policy.repoKey(policy.parseRemoteUrl('git@github.com:octo/hello'));
  assert.equal(a, b);
  assert.notEqual(a, policy.repoKey(policy.parseRemoteUrl('https://gitlab.com/octo/hello')));
});

// ---- .git/config ------------------------------------------------------------------------------

test('a .git/config is read for its remotes, quotes, comments and the old spelling included', () => {
  const read = policy.parseGitConfigRemotes;
  assert.deepEqual(read(CONFIG_HEAD + '[remote "origin"]\n\turl = https://github.com/a/b.git\n\tfetch = x\n' + CONFIG_TAIL),
    { urls: ['https://github.com/a/b.git'], pushUrls: [], unsure: false });
  assert.deepEqual(read('[remote "origin"]\r\n\turl = https://github.com/a/b.git ; mirror\r\n\tpushurl = "git@github.com:a/b.git"\r\n'),
    { urls: ['https://github.com/a/b.git'], pushUrls: ['git@github.com:a/b.git'], unsure: false });
  assert.deepEqual(read('# comment\n[REMOTE "x"]\n  URL=https://github.com/a/b\n').urls, ['https://github.com/a/b']);
  assert.deepEqual(read('[remote.origin]\n\turl = https://github.com/a/b\n').urls, ['https://github.com/a/b']);
  assert.deepEqual(read('\uFEFF[remote "o"]\nurl = https://github.com/a/b\n').urls, ['https://github.com/a/b']);
  assert.deepEqual(read('[remote "o"]\n\turl = https://github.com/a/b\n[remote "up"]\n\turl = https://github.com/c/d\n').urls,
    ['https://github.com/a/b', 'https://github.com/c/d']);
  assert.deepEqual(read('[core]\n\turl = not-a-remote\n[remote]\n\tpushdefault = origin\n').urls, []);
  assert.deepEqual(read('').urls, []);
});

test('a config the reader does not fully follow is unsure', () => {
  const unsure = (text) => assert.equal(policy.parseGitConfigRemotes(text).unsure, true, text);
  unsure('[include]\n\tpath = ../other\n[remote "o"]\n\turl = https://github.com/a/b\n');
  unsure('[includeIf "gitdir:~/work/"]\n\tpath = ~/.work\n[remote "o"]\n\turl = https://github.com/a/b\n');
  unsure('[remote "o"]\n\turl = https://github.com/a/\\\nb\n');
  unsure('[remote "o"]\n\turl =\n');
  unsure('[remote "o"]\n\turl\n');
  unsure('[remote "o"]\n\turl = "https://github.com/a/b\n');
  unsure('[remote "o"] url = https://github.com/a/b\n');
  unsure('this is not a config line !!\n');
});

test('the remote of a plain repository, a worktree and a submodule is found', async () => {
  const plain = repoWithRemote('https://github.com/octo/hello.git');
  assert.deepEqual(await policy.readGitRemote(plain), { kind: 'ok', url: 'https://github.com/octo/hello.git', pushUrls: [] });

  // A linked worktree: `.git` is a file, the shared config is named by `commondir`.
  const main = repoWithRemote('https://github.com/octo/hello.git');
  const wtGit = path.join(main, '.git', 'worktrees', 'feature');
  fs.mkdirSync(wtGit, { recursive: true });
  fs.writeFileSync(path.join(wtGit, 'commondir'), '../..\n');
  const worktree = tempDir();
  fs.writeFileSync(path.join(worktree, '.git'), `gitdir: ${wtGit}\n`);
  assert.deepEqual(await policy.readGitRemote(worktree), { kind: 'ok', url: 'https://github.com/octo/hello.git', pushUrls: [] });

  // A submodule: `.git` is a file with a relative path, the config sits in that directory.
  const sup = tempDir();
  const modules = path.join(sup, '.git', 'modules', 'lib');
  fs.mkdirSync(modules, { recursive: true });
  fs.writeFileSync(path.join(modules, 'config'), CONFIG_HEAD + '[remote "origin"]\n\turl = git@codeberg.org:octo/lib.git\n');
  const sub = path.join(sup, 'lib');
  fs.mkdirSync(sub);
  fs.writeFileSync(path.join(sub, '.git'), 'gitdir: ../.git/modules/lib\n');
  assert.deepEqual(await policy.readGitRemote(sub), { kind: 'ok', url: 'git@codeberg.org:octo/lib.git', pushUrls: [] });
});

test('no repository, no remote, several remotes or a strange config are each a doubt', async () => {
  assert.deepEqual(await policy.readGitRemote(tempDir()), { kind: 'none' });
  assert.deepEqual(await policy.readGitRemote(path.join(TMP, 'does-not-exist')), { kind: 'none' });
  assert.deepEqual(await policy.readGitRemote(repoWithRemote(undefined)), { kind: 'none' });
  const two = repoWithConfig('[remote "origin"]\n\turl = https://github.com/a/b.git\n[remote "upstream"]\n\turl = https://github.com/c/d.git\n');
  assert.deepEqual(await policy.readGitRemote(two), { kind: 'several' });
  const twoUrls = repoWithConfig('[remote "origin"]\n\turl = https://github.com/a/b.git\n\turl = https://github.com/c/d.git\n');
  assert.deepEqual(await policy.readGitRemote(twoUrls), { kind: 'several' });
  const included = repoWithConfig('[include]\n\tpath = more\n[remote "origin"]\n\turl = https://github.com/a/b.git\n');
  assert.deepEqual(await policy.readGitRemote(included), { kind: 'unreadable' });
  const noConfig = tempDir();
  fs.mkdirSync(path.join(noConfig, '.git'));
  assert.deepEqual(await policy.readGitRemote(noConfig), { kind: 'unreadable' });
  const badPointer = tempDir();
  fs.writeFileSync(path.join(badPointer, '.git'), 'not a pointer');
  assert.deepEqual(await policy.readGitRemote(badPointer), { kind: 'unreadable' });
});

test('a folder inside a repository does not take that repository\'s remote', async () => {
  const top = repoWithRemote('https://github.com/octo/hello.git');
  const inner = path.join(top, 'packages', 'app');
  fs.mkdirSync(inner, { recursive: true });
  assert.deepEqual(await policy.readGitRemote(inner), { kind: 'none' });
  const { httpGet, calls } = fakeGet(() => json(200, githubRepo()));
  assert.equal(await policy.getPrivacyMode(inner, { httpGet }), 'trusted');
  assert.equal(calls.length, 0);
});

// ---- what a host's answer means ----------------------------------------------------------------

const GH = policy.parseRemoteUrl('https://github.com/octo/hello.git');
const GL = policy.parseRemoteUrl('https://gitlab.com/grp/sub/proj.git');
const CB = policy.parseRemoteUrl('https://codeberg.org/octo/hello.git');

test('each host is asked its own public API', () => {
  assert.equal(policy.apiUrl(GH), 'https://api.github.com/repos/octo/hello');
  assert.equal(policy.apiUrl(GL), 'https://gitlab.com/api/v4/projects/grp%2Fsub%2Fproj');
  assert.equal(policy.apiUrl(CB), 'https://codeberg.org/api/v1/repos/octo/hello');
});

test('github: only a 200 that names this repository and says private:false is public', () => {
  const read = (status, body) => policy.interpretReply(GH, status, typeof body === 'string' ? body : JSON.stringify(body));
  assert.equal(read(200, githubRepo()), 'public');
  assert.equal(read(200, githubRepo({ full_name: 'Octo/Hello' })), 'public'); // case is not significant
  assert.equal(read(200, githubRepo({ visibility: undefined })), 'public');
  assert.equal(read(200, githubRepo({ private: true, visibility: 'private' })), 'notPublic');
  assert.equal(read(200, githubRepo({ private: false, visibility: 'internal' })), 'notPublic');
  assert.equal(read(404, { message: 'Not Found' }), 'notPublic'); // private, or not there: not public
  assert.equal(read(403, { message: 'API rate limit exceeded for 1.2.3.4' }), 'unknown');
  assert.equal(read(429, ''), 'unknown');
  assert.equal(read(401, { message: 'Bad credentials' }), 'unknown');
  assert.equal(read(451, { message: 'Repository access blocked' }), 'unknown');
  assert.equal(read(500, ''), 'unknown');
  assert.equal(read(503, 'Service Unavailable'), 'unknown');
  assert.equal(read(301, { message: 'Moved Permanently', url: 'https://api.github.com/repositories/1' }), 'unknown');
  assert.equal(read(302, ''), 'unknown');
  assert.equal(read(0, ''), 'unknown');
  assert.equal(read(200, githubRepo({ full_name: 'someone/else' })), 'unknown'); // a different repository
  assert.equal(read(200, githubRepo({ full_name: undefined })), 'unknown');
  assert.equal(read(200, { private: false }), 'unknown');
  assert.equal(read(200, { full_name: 'octo/hello' }), 'unknown');
  assert.equal(read(200, githubRepo({ private: 'false' })), 'unknown'); // not the boolean
  assert.equal(read(200, githubRepo({ private: null })), 'unknown');
  assert.equal(read(200, 'not json'), 'unknown');
  assert.equal(read(200, ''), 'unknown');
  assert.equal(read(200, '[]'), 'unknown');
  assert.equal(read(200, 'null'), 'unknown');
  assert.equal(read(200, '"public"'), 'unknown');
});

test('gitlab: a public project whose repository is open is public', () => {
  const read = (status, body) => policy.interpretReply(GL, status, JSON.stringify(body));
  assert.equal(read(200, gitlabProject()), 'public');
  assert.equal(read(200, gitlabProject({ repository_access_level: undefined })), 'public');
  assert.equal(read(200, gitlabProject({ path_with_namespace: 'GRP/Sub/Proj' })), 'public');
  assert.equal(read(200, gitlabProject({ visibility: 'private' })), 'notPublic');
  assert.equal(read(200, gitlabProject({ visibility: 'internal' })), 'notPublic');
  assert.equal(read(200, gitlabProject({ repository_access_level: 'private' })), 'notPublic');
  assert.equal(read(200, gitlabProject({ repository_access_level: 'disabled' })), 'notPublic');
  assert.equal(read(200, gitlabProject({ visibility: undefined })), 'unknown');
  assert.equal(read(200, gitlabProject({ path_with_namespace: 'other/proj' })), 'unknown');
  assert.equal(read(200, { visibility: 'public' }), 'unknown');
  assert.equal(read(404, { message: '404 Project Not Found' }), 'notPublic');
  assert.equal(read(401, { message: '401 Unauthorized' }), 'unknown');
  assert.equal(read(403, ''), 'unknown');
  assert.equal(read(429, 'Retry later'), 'unknown');
  assert.equal(read(500, ''), 'unknown');
});

test('codeberg: the same private flag, and an internal repository is not public', () => {
  const read = (status, body) => policy.interpretReply(CB, status, JSON.stringify(body));
  assert.equal(read(200, codebergRepo()), 'public');
  assert.equal(read(200, codebergRepo({ internal: undefined })), 'public');
  assert.equal(read(200, codebergRepo({ private: true })), 'notPublic');
  assert.equal(read(200, codebergRepo({ internal: true })), 'notPublic');
  assert.equal(read(200, codebergRepo({ full_name: 'someone/else' })), 'unknown');
  assert.equal(read(404, { message: 'The target couldn\'t be found.' }), 'notPublic');
  assert.equal(read(403, ''), 'unknown');
  assert.equal(read(429, ''), 'unknown');
  assert.equal(read(502, ''), 'unknown');
});

// ---- the mode, end to end ----------------------------------------------------------------------

test('a public github repository is open', async () => {
  const dir = repoWithRemote('git@github.com:octo/hello.git');
  const { httpGet, calls } = fakeGet(() => json(200, githubRepo()));
  assert.equal(await policy.getPrivacyMode(dir, { httpGet }), 'open');
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, 'https://api.github.com/repos/octo/hello');
});

test('public gitlab and codeberg repositories are open too', async () => {
  const gl = repoWithRemote('https://gitlab.com/grp/sub/proj.git');
  const getGl = fakeGet(() => json(200, gitlabProject()));
  assert.equal(await policy.getPrivacyMode(gl, { httpGet: getGl.httpGet }), 'open');
  assert.equal(getGl.calls[0].url, 'https://gitlab.com/api/v4/projects/grp%2Fsub%2Fproj');

  const cb = repoWithRemote('https://codeberg.org/octo/hello.git');
  const getCb = fakeGet(() => json(200, codebergRepo()));
  assert.equal(await policy.getPrivacyMode(cb, { httpGet: getCb.httpGet }), 'open');
  assert.equal(getCb.calls[0].url, 'https://codeberg.org/api/v1/repos/octo/hello');
});

test('a private repository, a 404, a rate limit, a failure or a strange answer is trusted', async () => {
  const dir = repoWithRemote('https://github.com/octo/hello.git');
  const answers = {
    private: () => json(200, githubRepo({ private: true, visibility: 'private' })),
    '404': () => json(404, { message: 'Not Found' }),
    'rate limited (403)': () => json(403, { message: 'API rate limit exceeded' }),
    'rate limited (429)': () => ({ status: 429, body: '' }),
    'server error': () => ({ status: 500, body: 'oops' }),
    redirect: () => ({ status: 301, body: '' }),
    'garbled body': () => ({ status: 200, body: '<html>captive portal</html>' }),
    'another repository': () => json(200, githubRepo({ full_name: 'other/thing' })),
    offline: () => { throw new Error('getaddrinfo ENOTFOUND api.github.com'); },
    'refused': () => Promise.reject(new Error('ECONNREFUSED')),
  };
  for (const [name, answer] of Object.entries(answers)) {
    policy.resetPrivacyCache();
    const { httpGet } = fakeGet(answer);
    assert.equal(await policy.getPrivacyMode(dir, { httpGet }), 'trusted', name);
  }
});

test('a lookup that takes too long is trusted, and does not hold the request up', async () => {
  const dir = repoWithRemote('https://github.com/octo/hello.git');
  const httpGet = () => new Promise(() => {}); // never answers, and ignores any signal
  const started = Date.now();
  assert.equal(await policy.getPrivacyMode(dir, { httpGet, timeoutMs: 40 }), 'trusted');
  assert.ok(Date.now() - started < 2000, 'came back in about the timeout');
});

test('a lookup that fails after the timeout has already answered raises nothing', async () => {
  const dir = repoWithRemote('https://github.com/octo/hello.git');
  const httpGet = () => new Promise((_, reject) => setTimeout(() => reject(new Error('late')), 80));
  assert.equal(await policy.getPrivacyMode(dir, { httpGet, timeoutMs: 20 }), 'trusted');
  await new Promise((resolve) => setTimeout(resolve, 120)); // an unhandled rejection would fail the run
});

test('the default timeout is short', () => {
  assert.ok(policy.LOOKUP_TIMEOUT_MS >= 1000 && policy.LOOKUP_TIMEOUT_MS <= 5000);
  const dir = repoWithRemote('https://github.com/octo/hello.git');
  const { httpGet, calls } = fakeGet(() => json(200, githubRepo()));
  return policy.getPrivacyMode(dir, { httpGet }).then(() => assert.equal(calls[0].timeoutMs, policy.LOOKUP_TIMEOUT_MS));
});

test('no remote, several remotes, or a push URL elsewhere is trusted without asking anyone', async () => {
  const { httpGet, calls } = fakeGet(() => json(200, githubRepo()));
  assert.equal(await policy.getPrivacyMode(tempDir(), { httpGet }), 'trusted');
  assert.equal(await policy.getPrivacyMode(repoWithRemote(undefined), { httpGet }), 'trusted');
  const fork = repoWithConfig('[remote "origin"]\n\turl = https://github.com/me/hello.git\n[remote "upstream"]\n\turl = https://github.com/octo/hello.git\n');
  assert.equal(await policy.getPrivacyMode(fork, { httpGet }), 'trusted');
  const elsewhere = repoWithConfig('[remote "origin"]\n\turl = https://github.com/octo/hello.git\n\tpushurl = https://github.com/me/private-mirror.git\n');
  assert.equal(await policy.getPrivacyMode(elsewhere, { httpGet }), 'trusted');
  const pushHost = repoWithConfig('[remote "origin"]\n\turl = https://github.com/octo/hello.git\n\tpushurl = git@git.company.internal:octo/hello.git\n');
  assert.equal(await policy.getPrivacyMode(pushHost, { httpGet }), 'trusted');
  assert.equal(calls.length, 0);
});

test('a push URL for the same repository changes nothing', async () => {
  const dir = repoWithConfig('[remote "origin"]\n\turl = https://github.com/octo/hello.git\n\tpushurl = git@github.com:octo/hello.git\n');
  const { httpGet } = fakeGet(() => json(200, githubRepo()));
  assert.equal(await policy.getPrivacyMode(dir, { httpGet }), 'open');
});

test('a private fork of a public repository is trusted (its own page is a 404)', async () => {
  const dir = repoWithRemote('https://github.com/me/hello-fork.git');
  const { httpGet, calls } = fakeGet(() => json(404, { message: 'Not Found' }));
  assert.equal(await policy.getPrivacyMode(dir, { httpGet }), 'trusted');
  assert.equal(calls[0].url, 'https://api.github.com/repos/me/hello-fork'); // asks about the fork, not its parent
});

test('enterprise and unknown hosts are trusted and nothing is requested', async () => {
  const { httpGet, calls } = fakeGet(() => json(200, githubRepo()));
  for (const url of [
    'https://github.mycorp.com/octo/hello.git',
    'git@gitlab.example.com:grp/proj.git',
    'https://github.com.evil.com/octo/hello.git',
    'https://bitbucket.org/octo/hello.git',
    '../sibling.git',
  ]) {
    assert.equal(await policy.getPrivacyMode(repoWithRemote(url), { httpGet }), 'trusted', url);
  }
  assert.equal(calls.length, 0);
});

// ---- nothing identifying leaves ------------------------------------------------------------------

test('the lookup names the repository and carries no credential, cookie or identity', async () => {
  stub.state.settings['dlne.apiKey'] = 'dlne_sk_SECRET_API_KEY';
  const dir = repoWithRemote('https://oauth2:ghp_SECRET_TOKEN@github.com/octo/hello.git');
  const { httpGet, calls } = fakeGet(() => json(200, githubRepo()));
  assert.equal(await policy.getPrivacyMode(dir, { httpGet }), 'open');
  assert.equal(calls.length, 1);
  const sent = JSON.stringify(calls[0]);
  assert.ok(!sent.includes('SECRET'), sent);
  assert.ok(!sent.includes(dir), 'the local path stays local');
  const names = Object.keys(calls[0].headers).map((h) => h.toLowerCase());
  for (const forbidden of ['authorization', 'cookie', 'proxy-authorization', 'x-dlne-privacy', 'x-dlne-session']) {
    assert.ok(!names.includes(forbidden), forbidden);
  }
  assert.ok(names.includes('user-agent'), 'api.github.com refuses a request without one');
});

// ---- the cache -----------------------------------------------------------------------------------

const HOUR = 3600 * 1000;

test('the lifetimes are an hour for public, a day for not public, minutes for a failure', () => {
  assert.equal(policy.PUBLIC_TTL_MS, HOUR);
  assert.equal(policy.NOT_PUBLIC_TTL_MS, 24 * HOUR);
  assert.equal(policy.FAILURE_TTL_MS, 5 * 60 * 1000);
  assert.ok(policy.FAILURE_TTL_MS < policy.PUBLIC_TTL_MS && policy.PUBLIC_TTL_MS < policy.NOT_PUBLIC_TTL_MS);
  assert.equal(policy.ANSWER_TTL_MS, undefined, 'no single "answer" lifetime is left');
});

test('a public answer is kept for an hour per remote, then asked again', async () => {
  const dir = repoWithRemote('https://github.com/octo/hello.git');
  let clock = 1_000_000;
  let reply = json(200, githubRepo());
  const { httpGet, calls } = fakeGet(() => reply);
  const deps = { httpGet, now: () => clock };

  assert.equal(await policy.getPrivacyMode(dir, deps), 'open');
  assert.equal(calls.length, 1);

  reply = json(200, githubRepo({ private: true, visibility: 'private' })); // the repository went private
  clock += HOUR - 1;
  assert.equal(await policy.getPrivacyMode(dir, deps), 'open', 'still inside the hour');
  assert.equal(calls.length, 1);

  clock += 1;
  assert.equal(await policy.getPrivacyMode(dir, deps), 'trusted', 'a repository made private stops being open after an hour');
  assert.equal(calls.length, 2);
  assert.equal(await policy.getPrivacyMode(dir, deps), 'trusted');
  assert.equal(calls.length, 2);
});

test('a not-public answer is kept for 24 hours, whether it was a private flag or a 404', async () => {
  for (const notPublic of [json(404, { message: 'Not Found' }), json(200, githubRepo({ private: true, visibility: 'private' }))]) {
    policy.resetPrivacyCache();
    const dir = repoWithRemote('https://github.com/octo/hello.git');
    let clock = 2_000_000;
    let reply = notPublic;
    const { httpGet, calls } = fakeGet(() => reply);
    const deps = { httpGet, now: () => clock };

    assert.equal(await policy.getPrivacyMode(dir, deps), 'trusted');
    reply = json(200, githubRepo()); // it was made public
    clock += HOUR; // past the public lifetime, still inside the not-public one
    assert.equal(await policy.getPrivacyMode(dir, deps), 'trusted');
    clock += 23 * HOUR - 1;
    assert.equal(await policy.getPrivacyMode(dir, deps), 'trusted');
    assert.equal(calls.length, 1);
    clock += 1;
    assert.equal(await policy.getPrivacyMode(dir, deps), 'open', 'asked again after 24 hours');
    assert.equal(calls.length, 2);
  }
});

test('the cache is per remote, and a changed remote is a new question', async () => {
  const a = repoWithRemote('https://github.com/octo/hello.git');
  const same = repoWithRemote('git@github.com:Octo/Hello.git'); // same repository, spelled differently
  const other = repoWithRemote('https://github.com/octo/other.git');
  const { httpGet, calls } = fakeGet((url) => json(200, githubRepo({ full_name: url.endsWith('/hello') ? 'octo/hello' : 'octo/other' })));
  const deps = { httpGet };
  assert.equal(await policy.getPrivacyMode(a, deps), 'open');
  assert.equal(await policy.getPrivacyMode(same, deps), 'open');
  assert.equal(calls.length, 1);
  assert.equal(await policy.getPrivacyMode(other, deps), 'open');
  assert.equal(calls.length, 2);

  // The remote is read every time: pointing it at a private repository is noticed at once.
  fs.writeFileSync(path.join(a, '.git', 'config'), CONFIG_HEAD + '[remote "origin"]\n\turl = https://github.com/octo/secret.git\n');
  const secret = fakeGet(() => json(404, {}));
  assert.equal(await policy.getPrivacyMode(a, { httpGet: secret.httpGet }), 'trusted');
  assert.equal(secret.calls.length, 1);
});

test('a failure is remembered for a few minutes only', async () => {
  const dir = repoWithRemote('https://github.com/octo/hello.git');
  let clock = 5_000_000;
  let reply = json(403, { message: 'API rate limit exceeded' });
  const { httpGet, calls } = fakeGet(() => reply);
  const deps = { httpGet, now: () => clock };

  assert.equal(await policy.getPrivacyMode(dir, deps), 'trusted');
  assert.equal(await policy.getPrivacyMode(dir, deps), 'trusted');
  assert.equal(calls.length, 1, 'the rate limit is not hit again for every request');

  reply = json(200, githubRepo());
  clock += policy.FAILURE_TTL_MS - 1;
  assert.equal(await policy.getPrivacyMode(dir, deps), 'trusted');
  assert.equal(calls.length, 1);
  clock += 1;
  assert.equal(await policy.getPrivacyMode(dir, deps), 'open', 'asked again once the short wait is over');
  assert.equal(calls.length, 2);
});

test('a private repository\'s 404 is an answer, not a failure', async () => {
  const dir = repoWithRemote('https://github.com/octo/hello.git');
  const { httpGet, calls } = fakeGet(() => json(404, { message: 'Not Found' }));
  const deps = { httpGet };
  assert.equal(await policy.getPrivacyMode(dir, deps), 'trusted');
  assert.equal(await policy.getPrivacyMode(dir, deps), 'trusted');
  assert.equal(calls.length, 1);
});

test('requests that arrive together share one lookup', async () => {
  const dir = repoWithRemote('https://github.com/octo/hello.git');
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const { httpGet, calls } = fakeGet(async () => { await gate; return json(200, githubRepo()); });
  const pending = [1, 2, 3, 4, 5].map(() => policy.getPrivacyMode(dir, { httpGet }));
  await new Promise((resolve) => setImmediate(resolve));
  release();
  assert.deepEqual(await Promise.all(pending), ['open', 'open', 'open', 'open', 'open']);
  assert.equal(calls.length, 1);
});

test('the cache holds a bounded number of remotes, oldest first out', async () => {
  const cache = new policy.PublicRepoCache();
  let asked = 0;
  const lookup = async () => { asked++; return 'public'; };
  for (let i = 0; i < 300; i++) await cache.resolve(`github.com/o/r${i}`, () => 1, lookup);
  assert.equal(asked, 300);
  await cache.resolve('github.com/o/r299', () => 1, lookup);
  assert.equal(asked, 300, 'the newest is still there');
  await cache.resolve('github.com/o/r0', () => 1, lookup);
  assert.equal(asked, 301, 'the oldest was dropped');
});

test('an injected cache is used instead of the shared one', async () => {
  const dir = repoWithRemote('https://github.com/octo/hello.git');
  const { httpGet, calls } = fakeGet(() => json(200, githubRepo()));
  const mine = new policy.PublicRepoCache();
  await policy.getPrivacyMode(dir, { httpGet, cache: mine });
  await policy.getPrivacyMode(dir, { httpGet, cache: new policy.PublicRepoCache() });
  assert.equal(calls.length, 2);
  await policy.getPrivacyMode(dir, { httpGet, cache: mine });
  assert.equal(calls.length, 2);
});

// ---- the setting ----------------------------------------------------------------------------------

test('trustedOnly is trusted even for a public repository, without a lookup', async () => {
  stub.state.settings['dlne.privacy'] = 'trustedOnly';
  const { httpGet, calls } = fakeGet(() => json(200, githubRepo()));
  assert.equal(await policy.getPrivacyMode(repoWithRemote('https://github.com/octo/hello.git'), { httpGet }), 'trusted');
  assert.equal(calls.length, 0);
});

test('anyMember is any, for a private repository, no repository or no folder, without a lookup', async () => {
  stub.state.settings['dlne.privacy'] = 'anyMember';
  const { httpGet, calls } = fakeGet(() => json(404, {}));
  assert.equal(await policy.getPrivacyMode(repoWithRemote('https://github.com/octo/secret.git'), { httpGet }), 'any');
  assert.equal(await policy.getPrivacyMode(repoWithRemote('https://github.mycorp.com/o/r.git'), { httpGet }), 'any');
  assert.equal(await policy.getPrivacyMode(tempDir(), { httpGet }), 'any');
  assert.equal(await policy.getPrivacyMode(undefined, { httpGet }), 'any');
  assert.equal(calls.length, 0);
});

test('auto, and the setting left unset, decide by the repository', async () => {
  const dir = repoWithRemote('https://github.com/octo/hello.git');
  const open = fakeGet(() => json(200, githubRepo()));
  assert.equal(await policy.getPrivacyMode(dir, { httpGet: open.httpGet }), 'open'); // unset
  policy.resetPrivacyCache();
  stub.state.settings['dlne.privacy'] = 'auto';
  assert.equal(await policy.getPrivacyMode(dir, { httpGet: open.httpGet }), 'open');
  policy.resetPrivacyCache();
  const closed = fakeGet(() => json(404, {}));
  assert.equal(await policy.getPrivacyMode(dir, { httpGet: closed.httpGet }), 'trusted');
});

test('a setting that is not one of the three is trusted, never relaxed', async () => {
  const dir = repoWithRemote('https://github.com/octo/hello.git');
  const { httpGet, calls } = fakeGet(() => json(200, githubRepo()));
  for (const bad of ['', 'any', 'open', 'public', 'AUTO', 'Auto', 'anymember', 'trusted', 'everyone', 'true', 1, 0, true, false, [], {}, ['anyMember']]) {
    stub.state.settings['dlne.privacy'] = bad;
    assert.equal(await policy.getPrivacyMode(dir, { httpGet }), 'trusted', JSON.stringify(bad));
  }
  assert.equal(calls.length, 0);
  // Spaces around a good value are forgiven.
  stub.state.settings['dlne.privacy'] = ' anyMember ';
  assert.equal(await policy.getPrivacyMode(dir, { httpGet }), 'any');
});

test('normalizePrivacySetting', () => {
  assert.equal(policy.normalizePrivacySetting(undefined), 'auto');
  assert.equal(policy.normalizePrivacySetting(null), 'auto');
  assert.equal(policy.normalizePrivacySetting('auto'), 'auto');
  assert.equal(policy.normalizePrivacySetting('trustedOnly'), 'trustedOnly');
  assert.equal(policy.normalizePrivacySetting('anyMember'), 'anyMember');
  assert.equal(policy.normalizePrivacySetting('nope'), 'invalid');
  assert.equal(policy.normalizePrivacySetting(7), 'invalid');
});

test('the setting is declared in package.json, with a description for each choice', () => {
  const prop = pkg.contributes.configuration.properties['dlne.privacy'];
  assert.ok(prop, 'dlne.privacy is declared');
  assert.equal(prop.type, 'string');
  assert.deepEqual(prop.enum, ['auto', 'trustedOnly', 'anyMember']);
  assert.equal(prop.default, 'auto');
  assert.equal(prop.scope, 'resource', 'a folder can be set apart in a multi-root workspace');
  assert.equal(prop.enumDescriptions.length, prop.enum.length);
  for (const text of prop.enumDescriptions) assert.ok(text.length > 40, text);
  assert.ok((prop.markdownDescription || prop.description).length > 40);
  // DLNE waits for workspace trust, so a cloned repository's own .vscode/settings.json cannot widen it
  // first ("limited" would let the agent, which never checks workspace.isTrusted, run in untrusted repos).
  const untrusted = pkg.capabilities && pkg.capabilities.untrustedWorkspaces;
  assert.ok(untrusted, 'capabilities.untrustedWorkspaces is declared');
  assert.equal(untrusted.supported, false);
  assert.ok(untrusted.description.length > 20);
  // The setting key and its choices are the ones the code reads.
  assert.equal('dlne.' + policy.PRIVACY_SETTING_KEY, 'dlne.privacy');
  for (const choice of prop.enum) assert.notEqual(policy.normalizePrivacySetting(choice), 'invalid');
});

// ---- which folder ----------------------------------------------------------------------------------

test('a folder may be given as a path, a workspace folder or a Uri', async () => {
  const dir = repoWithRemote('https://github.com/octo/hello.git');
  const { httpGet } = fakeGet(() => json(200, githubRepo()));
  assert.equal(await policy.getPrivacyMode(dir, { httpGet }), 'open');
  assert.equal(await policy.getPrivacyMode({ uri: { fsPath: dir, scheme: 'file' }, name: 'x', index: 0 }, { httpGet }), 'open');
  assert.equal(await policy.getPrivacyMode({ uri: { fsPath: dir } }, { httpGet }), 'open');
  assert.equal(await policy.getPrivacyMode({ fsPath: dir, scheme: 'file' }, { httpGet }), 'open');
});

test('without a folder the workspace\'s only folder is used; with several, or none, it is trusted', async () => {
  const dir = repoWithRemote('https://github.com/octo/hello.git');
  const other = repoWithRemote('https://github.com/octo/other.git');
  const { httpGet, calls } = fakeGet(() => json(200, githubRepo()));

  stub.vscode.workspace.workspaceFolders = [{ uri: { fsPath: dir, scheme: 'file' }, name: 'a', index: 0 }];
  assert.equal(await policy.getPrivacyMode(undefined, { httpGet }), 'open');

  stub.vscode.workspace.workspaceFolders = [
    { uri: { fsPath: dir, scheme: 'file' }, name: 'a', index: 0 },
    { uri: { fsPath: other, scheme: 'file' }, name: 'b', index: 1 },
  ];
  const before = calls.length;
  assert.equal(await policy.getPrivacyMode(undefined, { httpGet }), 'trusted');
  assert.equal(calls.length, before);

  stub.vscode.workspace.workspaceFolders = undefined;
  assert.equal(await policy.getPrivacyMode(undefined, { httpGet }), 'trusted');
  stub.vscode.workspace.workspaceFolders = [];
  assert.equal(await policy.getPrivacyMode(undefined, { httpGet }), 'trusted');
});

test('a virtual or remote-only folder, or nonsense, is trusted and never throws', async () => {
  const { httpGet, calls } = fakeGet(() => json(200, githubRepo()));
  const dir = repoWithRemote('https://github.com/octo/hello.git');
  assert.equal(await policy.getPrivacyMode({ uri: { fsPath: dir, scheme: 'vscode-vfs' } }, { httpGet }), 'trusted');
  assert.equal(await policy.getPrivacyMode({ uri: { fsPath: dir, scheme: 'untitled' } }, { httpGet }), 'trusted');
  for (const junk of ['', 42, {}, { uri: null }, { uri: { fsPath: 7 } }, { uri: { fsPath: '' } }, [], true]) {
    assert.equal(await policy.getPrivacyMode(junk, { httpGet }), 'trusted', JSON.stringify(junk));
  }
  assert.equal(calls.length, 0);
});

test('getPrivacyMode answers trusted even if the machinery under it throws', async () => {
  const dir = repoWithRemote('https://github.com/octo/hello.git');
  const httpGet = () => { throw new TypeError('boom'); };
  assert.equal(await policy.getPrivacyMode(dir, { httpGet }), 'trusted');
  const saved = stub.vscode.workspace.getConfiguration;
  stub.vscode.workspace.getConfiguration = () => { throw new Error('settings are unavailable'); };
  try {
    assert.equal(await policy.getPrivacyMode(dir, { httpGet: fakeGet(() => json(200, githubRepo())).httpGet }), 'trusted');
  } finally {
    stub.vscode.workspace.getConfiguration = saved;
  }
});

// ---- the real GET ------------------------------------------------------------------------------------

function serve(handler) {
  return new Promise((resolve) => {
    const seen = [];
    const server = http.createServer((req, res) => { seen.push(req); handler(req, res); });
    server.listen(0, '127.0.0.1', () => {
      resolve({
        url: `http://127.0.0.1:${server.address().port}`,
        seen,
        close: () => new Promise((done) => { server.closeAllConnections?.(); server.close(() => done()); }),
      });
    });
  });
}

test('the real GET returns the status and body and sends only the headers it is given', async () => {
  const server = await serve((req, res) => { res.writeHead(200, { 'content-type': 'application/json' }); res.end('{"private":false}'); });
  try {
    const reply = await policy.defaultHttpGet(`${server.url}/repos/o/r`, policy.lookupHeaders(), 2000);
    assert.deepEqual(reply, { status: 200, body: '{"private":false}' });
    const sent = server.seen[0].headers;
    assert.equal(sent['user-agent'], policy.lookupHeaders()['User-Agent']);
    for (const forbidden of ['authorization', 'cookie', 'proxy-authorization']) assert.equal(sent[forbidden], undefined, forbidden);
  } finally {
    await server.close();
  }
});

test('the real GET reports a 404 and a rate limit as they are, and follows no redirect', async () => {
  const server = await serve((req, res) => {
    if (req.url === '/missing') { res.writeHead(404); res.end('{"message":"Not Found"}'); return; }
    if (req.url === '/limited') { res.writeHead(403, { 'x-ratelimit-remaining': '0' }); res.end('{"message":"rate limit"}'); return; }
    if (req.url === '/moved') { res.writeHead(301, { location: '/final' }); res.end(); return; }
    res.writeHead(200); res.end('final');
  });
  try {
    assert.equal((await policy.defaultHttpGet(`${server.url}/missing`, {}, 2000)).status, 404);
    assert.equal((await policy.defaultHttpGet(`${server.url}/limited`, {}, 2000)).status, 403);
    const moved = await policy.defaultHttpGet(`${server.url}/moved`, {}, 2000);
    assert.equal(moved.status, 301);
    assert.notEqual(moved.body, 'final');
    assert.equal(server.seen.filter((r) => r.url === '/final').length, 0);
  } finally {
    await server.close();
  }
});

test('the real GET gives up on a server that does not answer, and on one that sends too much', async () => {
  const slow = await serve(() => {}); // accepts, never answers
  const flood = await serve((req, res) => { res.writeHead(200); res.end('x'.repeat(400 * 1024)); });
  try {
    const started = Date.now();
    await assert.rejects(policy.defaultHttpGet(`${slow.url}/x`, {}, 60), /timeout/);
    assert.ok(Date.now() - started < 2000);
    await assert.rejects(policy.defaultHttpGet(`${flood.url}/x`, {}, 2000), /too large/);
  } finally {
    await slow.close();
    await flood.close();
  }
  await assert.rejects(policy.defaultHttpGet('http://127.0.0.1:1/x', {}, 500)); // nothing listens: offline
});

test('a whole lookup through the real GET: a slow or refusing host is trusted', async () => {
  // The API URL is fixed to the three hosts, so this goes through lookup with the server's address swapped in.
  const slow = await serve(() => {});
  try {
    const httpGet = (url, headers, timeoutMs) => policy.defaultHttpGet(url.replace(/^https:\/\/[^/]+/, slow.url), headers, timeoutMs);
    const dir = repoWithRemote('https://github.com/octo/hello.git');
    assert.equal(await policy.getPrivacyMode(dir, { httpGet, timeoutMs: 80 }), 'trusted');
  } finally {
    await slow.close();
  }
});

// ---- the hint ------------------------------------------------------------------------------------------

test('each mode has its own short label and an explanation', () => {
  const texts = new Set();
  for (const mode of ['open', 'trusted', 'any']) {
    const shown = policy.describePrivacyMode(mode);
    assert.ok(shown.text.length > 0 && shown.text.length < 24, shown.text);
    assert.ok(shown.tooltip.includes('dlne.privacy'));
    texts.add(shown.text);
  }
  assert.equal(texts.size, 3);
  // `open` covers the working copy, unpushed changes included: the text says so and names the way out.
  assert.match(policy.describePrivacyMode('open').tooltip, /not pushed/);
  assert.match(policy.describePrivacyMode('open').tooltip, /trustedOnly/);
  assert.deepEqual(policy.describePrivacyMode('whatever'), policy.describePrivacyMode('trusted'));
});

function observe(item) {
  item.visible = null;
  item.show = () => { item.visible = true; };
  item.hide = () => { item.visible = false; };
  item.disposed = false;
  item.dispose = () => { item.disposed = true; };
  return item;
}

test('the status item shows the mode of the workspace and is hidden without one', async () => {
  const dir = repoWithRemote('https://github.com/octo/hello.git');
  const { httpGet } = fakeGet(() => json(200, githubRepo()));
  stub.vscode.workspace.workspaceFolders = [{ uri: { fsPath: dir, scheme: 'file' }, name: 'a', index: 0 }];

  const status = policy.createPrivacyStatus({ httpGet });
  const item = observe(stub.state.statusBarItems.at(-1));
  assert.equal(stub.state.statusBarItems.length, 1);
  await status.ready; // the first look, started by the constructor, is finished before the test moves on
  assert.deepEqual(item.command, { command: 'workbench.action.openSettings', title: 'DLNE privacy', arguments: ['dlne.privacy'] });

  await status.refresh();
  assert.equal(item.text, policy.describePrivacyMode('open').text);
  assert.equal(item.tooltip, policy.describePrivacyMode('open').tooltip);
  assert.equal(item.visible, true);

  stub.state.settings['dlne.privacy'] = 'trustedOnly';
  await status.refresh();
  assert.equal(item.text, policy.describePrivacyMode('trusted').text);

  stub.state.settings['dlne.privacy'] = 'anyMember';
  await status.refresh();
  assert.equal(item.text, policy.describePrivacyMode('any').text);

  stub.vscode.workspace.workspaceFolders = undefined;
  await status.refresh();
  assert.equal(item.visible, false);

  status.dispose();
  assert.equal(item.disposed, true);
});

test('the status item follows a change of the setting', async () => {
  const dir = repoWithRemote('https://github.com/octo/hello.git');
  const { httpGet } = fakeGet(() => json(404, {}));
  stub.vscode.workspace.workspaceFolders = [{ uri: { fsPath: dir, scheme: 'file' }, name: 'a', index: 0 }];
  const changes = new stub.vscode.EventEmitter();
  stub.vscode.workspace.onDidChangeConfiguration = changes.event;
  try {
    const status = policy.createPrivacyStatus({ httpGet });
    const item = stub.state.statusBarItems.at(-1);
    await status.ready;
    assert.equal(item.text, policy.describePrivacyMode('trusted').text);

    stub.state.settings['dlne.privacy'] = 'anyMember';
    changes.fire({ affectsConfiguration: (key) => key === 'dlne.privacy' });
    for (let i = 0; i < 50 && item.text !== policy.describePrivacyMode('any').text; i++) {
      await new Promise((resolve) => setTimeout(resolve, 5));
    }
    assert.equal(item.text, policy.describePrivacyMode('any').text);

    // A change to some other setting leaves it alone.
    stub.state.settings['dlne.privacy'] = 'trustedOnly';
    changes.fire({ affectsConfiguration: (key) => key === 'dlne.serverUrl' });
    await new Promise((resolve) => setTimeout(resolve, 30));
    assert.equal(item.text, policy.describePrivacyMode('any').text);

    status.dispose();
  } finally {
    delete stub.vscode.workspace.onDidChangeConfiguration;
  }
});

// ---- who may import it ------------------------------------------------------------------------------------

test('policy.ts reads no credential and imports no other DLNE module', () => {
  const source = fs.readFileSync(path.join(ROOT, 'src', 'privacy', 'policy.ts'), 'utf8');
  const code = source.split('\n').filter((line) => !line.trim().startsWith('//')).join('\n');
  assert.doesNotMatch(code, /apiKey|networkKey|getSession|authentication\./i);
  assert.doesNotMatch(code, /from '\.\.?\//, 'a leaf module: nothing of the extension is imported');
});
