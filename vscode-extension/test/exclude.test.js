'use strict';
/**
 * Files DLNE never reads for the model (docs/coding-members.md section 11): credentials files by name,
 * anything ignored by the workspace's .gitignore files, and .dlneignore. The agent's read_file, the
 * agent's search, and autocomplete all ask isExcluded() before touching a file.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const stub = require('./helpers/vscode-stub');
const { isExcluded, excludedReason, excludedNotice, clearPrivacyCaches } = require('../dist/privacy/redact');

/** A throwaway project: `files` maps relative paths to contents; a path ending in "/" is an empty folder. */
function project(files, { git = true } = {}) {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-excl-')));
  if (git) fs.mkdirSync(path.join(root, '.git'));
  for (const [rel, content] of Object.entries(files)) {
    const full = path.join(root, rel);
    if (rel.endsWith('/')) {
      fs.mkdirSync(full, { recursive: true });
    } else {
      fs.mkdirSync(path.dirname(full), { recursive: true });
      fs.writeFileSync(full, content);
    }
  }
  clearPrivacyCaches();
  return {
    root,
    at: (rel) => path.join(root, ...rel.split('/')),
    excluded(rel) { return isExcluded(this.at(rel)); },
    cleanup() {
      fs.rmSync(root, { recursive: true, force: true });
      clearPrivacyCaches();
    },
  };
}

async function inProject(files, options, body) {
  if (typeof options === 'function') { body = options; options = {}; }
  const p = project(files, options);
  stub.vscode.workspace.workspaceFolders = options.workspace === false ? undefined : [{ uri: { fsPath: options.workspaceAt ? p.at(options.workspaceAt) : p.root } }];
  try {
    await body(p);
  } finally {
    stub.vscode.workspace.workspaceFolders = undefined;
    p.cleanup();
  }
}

// --- credentials files, by name and place ---------------------------------------------------

test('environment files are excluded, except templates', () => {
  for (const name of ['.env', '.env.local', '.env.production', '.envrc', 'prod.env', 'app/.env', 'a/b/.ENV', '.env.example.local']) {
    assert.ok(isExcluded(`/work/${name}`), name);
  }
  for (const name of ['.env.example', '.env.sample', '.env.template', '.env.dist', 'staging.env.example', 'environment.ts', 'env.py', 'src/environment-notes.txt']) {
    assert.ok(!isExcluded(`/work/${name}`), name);
  }
});

test('key and certificate stores are excluded by extension', () => {
  for (const name of ['server.pem', 'tls/private.key', 'a.p12', 'a.pfx', 'store.jks', 'app.keystore', 'AuthKey_ABC.p8', 'putty.ppk', 'vault.kdbx', 'x.gpg', 'cert.PEM']) {
    assert.ok(isExcluded(`/work/${name}`), name);
  }
  for (const name of ['keys.ts', 'src/key-management.md', 'monkey.txt', 'pem.js', 'docs/pem-format.md', 'keyboard.ts']) {
    assert.ok(!isExcluded(`/work/${name}`), name);
  }
});

test('ssh keys, with or without .pub', () => {
  for (const name of ['id_rsa', 'id_rsa.pub', 'id_ed25519', 'id_ed25519.pub', 'id_ecdsa', 'id_dsa', 'ID_RSA', '.ssh/config', '.ssh/known_hosts', 'home/me/.ssh/authorized_keys']) {
    assert.ok(isExcluded(`/work/${name}`), name);
  }
  assert.ok(!isExcluded('/work/ids_rsa.txt'));
});

test('package manager, cloud and tool credentials', () => {
  const excluded = [
    '.npmrc', 'sub/.npmrc', '.pypirc', '.netrc', '_netrc', '.git-credentials', '.pgpass', '.htpasswd',
    '.aws/credentials', 'home/me/.aws/credentials', '.docker/config.json', 'home/me/.docker/config.json',
    '.kube/config', 'kubeconfig', 'kubeconfig.prod.yaml', 'dev.kubeconfig', '.config/gcloud/credentials.db',
    '.azure/accessTokens.json', '.cargo/credentials.toml', '.gem/credentials', '.m2/settings.xml',
    'credentials.json', 'client_secret_123.json', 'service-account-prod.json', 'serviceAccountKey.json',
    'terraform.tfstate', 'prod.tfvars', 'secrets.yml', 'secrets.json', 'app.secrets', '.vault-token',
    'wg0.conf', 'etc/wireguard/server.conf', 'wg_configs/alice.conf', 'dlne_tunnel.conf', 'dlne_tunnel_bob.conf',
  ];
  for (const name of excluded) assert.ok(isExcluded(`/work/${name}`), name);
  const fine = ['.aws/config', '.docker/compose.yml', 'docker-compose.yml', '.kube/README.md', 'config.json', 'package.json',
    'tsconfig.json', 'credentials.ts', 'src/credentials/index.ts', 'secrets-policy.md', '.npmignore', '.github/workflows/ci.yml',
    '.gitignore', '.gitattributes', '.gitmodules', '.eslintrc', 'README.md'];
  for (const name of fine) assert.ok(!isExcluded(`/work/${name}`), name);
});

test('anything under .git is excluded; .github and .gitignore are not', () => {
  for (const name of ['.git/config', '.git/HEAD', '.git/hooks/pre-commit', 'sub/.git/config', '.git/objects/ab/cdef']) {
    assert.ok(isExcluded(`/work/${name}`), name);
  }
  assert.ok(!isExcluded('/work/.github/workflows/ci.yml'));
  assert.ok(!isExcluded('/work/.gitignore'));
});

test('Windows-style paths work the same', () => {
  assert.ok(isExcluded('C:\\Users\\me\\.aws\\credentials'));
  assert.ok(isExcluded('C:\\Users\\me\\project\\.env'));
  assert.ok(isExcluded('C:\\Users\\me\\project\\.git\\config'));
  assert.ok(!isExcluded('C:\\Users\\me\\project\\src\\app.ts'));
});

test('accepts a Uri-like object, a path string, and ignores nothing-paths', () => {
  assert.ok(isExcluded({ fsPath: '/work/.env', scheme: 'file' }));
  assert.ok(isExcluded({ path: '/work/.env' }));
  assert.ok(isExcluded('/work/.env'));
  for (const v of [undefined, null, '', {}, { scheme: 'file' }, { fsPath: '' }]) assert.equal(isExcluded(v), false);
});

test('buffers that are not files (untitled, output panels) are not judged by the ignore files', () => {
  assert.equal(isExcluded({ scheme: 'untitled', fsPath: 'Untitled-1' }), false);
  assert.equal(isExcluded({ scheme: 'output', path: 'extension-output-dlne' }), false);
});

test('the reason names why, and the notice tells the model the file is excluded for privacy', () => {
  assert.match(excludedReason('/work/.env'), /environment file/);
  assert.match(excludedReason('/work/server.pem'), /key/);
  assert.match(excludedReason('/work/.git/config'), /\.git/);
  assert.equal(excludedReason('/work/src/app.ts'), null);
  const notice = excludedNotice('src/.env', 'an environment file that may hold secrets');
  assert.match(notice, /excluded for privacy/);
  assert.match(notice, /src\/\.env/);
  assert.match(notice, /Do not retry/);
});

// --- .gitignore ------------------------------------------------------------------------------

test('files ignored by the workspace .gitignore are excluded', () =>
  inProject({
    '.gitignore': '# build output\nnode_modules/\ndist\n*.log\n\n/secret-notes.txt\n',
    'src/app.ts': '',
    'node_modules/pkg/index.js': '',
    'dist/bundle.js': '',
    'logs/server.log': '',
    'secret-notes.txt': '',
    'docs/secret-notes.txt': '',
  }, (p) => {
    assert.equal(p.excluded('src/app.ts'), false);
    assert.equal(p.excluded('node_modules/pkg/index.js'), true);
    assert.equal(p.excluded('dist/bundle.js'), true);
    assert.equal(p.excluded('logs/server.log'), true);
    assert.equal(p.excluded('secret-notes.txt'), true);
    assert.equal(p.excluded('docs/secret-notes.txt'), false);   // anchored with a leading slash
    assert.match(excludedReason(p.at('dist/bundle.js')), /ignored by \.gitignore/);
  }));

test('gitignore syntax: negation, directory-only patterns, anchoring, **, ?, classes, escapes', () =>
  inProject({
    '.gitignore': [
      '*.log', '!keep.log',
      'build/',                // a directory only
      'docs/*.tmp',            // anchored: has a slash
      '**/cache',              // any depth
      'a/**/z',                // a/z, a/b/z, a/b/c/z
      'out/**',                // everything inside out/
      'file?.txt',
      '[abc]data.bin',
      '\\#hash.txt',
      'trailing.txt   ',
      '!/important/',
      '*.pyc',
    ].join('\n'),
    'a.log': '', 'keep.log': '', 'sub/keep.log': '', 'sub/b.log': '',
    'build/x.js': '', 'src/build': '',
    'docs/a.tmp': '', 'docs/deeper/a.tmp': '',
    'cache': '', 'x/y/cache/z.txt': '',
    'a/z': '', 'a/b/c/z': '', 'a/b/c/y': '',
    'out/one.txt': '', 'out/deep/two.txt': '',
    'file1.txt': '', 'file12.txt': '',
    'adata.bin': '', 'ddata.bin': '',
    '#hash.txt': '',
    'trailing.txt': '',
    'important/a.pyc': '',
  }, (p) => {
    const expected = {
      'a.log': true, 'keep.log': false, 'sub/keep.log': false, 'sub/b.log': true,
      'build/x.js': true, 'src/build': false,
      'docs/a.tmp': true, 'docs/deeper/a.tmp': false,
      'cache': true, 'x/y/cache/z.txt': true,
      'a/z': true, 'a/b/c/z': true, 'a/b/c/y': false,
      'out/one.txt': true, 'out/deep/two.txt': true,
      'file1.txt': true, 'file12.txt': false,
      'adata.bin': true, 'ddata.bin': false,
      '#hash.txt': true,
      'trailing.txt': true,
      'important/a.pyc': true, // *.pyc still matches the file itself
    };
    for (const [rel, want] of Object.entries(expected)) assert.equal(p.excluded(rel), want, rel);
  }));

test('a file cannot be re-included when its folder is ignored', () =>
  inProject({
    '.gitignore': 'vendor/\n!vendor/keep.js\n',
    'vendor/keep.js': '', 'vendor/other.js': '',
  }, (p) => {
    assert.equal(p.excluded('vendor/keep.js'), true);
    assert.equal(p.excluded('vendor/other.js'), true);
  }));

test('nested .gitignore files apply below their folder and can override the parent', () =>
  inProject({
    '.gitignore': '*.gen.ts\n',
    'pkg/.gitignore': '!api.gen.ts\nlocal/\n',
    'pkg/api.gen.ts': '', 'pkg/other.gen.ts': '', 'top.gen.ts': '',
    'pkg/local/a.ts': '', 'local/a.ts': '',
  }, (p) => {
    assert.equal(p.excluded('top.gen.ts'), true);
    assert.equal(p.excluded('pkg/other.gen.ts'), true);
    assert.equal(p.excluded('pkg/api.gen.ts'), false);
    assert.equal(p.excluded('pkg/local/a.ts'), true);
    assert.equal(p.excluded('local/a.ts'), false);
  }));

test('.git/info/exclude counts too', () =>
  inProject({ '.git/info/exclude': 'private-notes/\n', 'private-notes/a.md': '', 'public/a.md': '' }, (p) => {
    assert.equal(p.excluded('private-notes/a.md'), true);
    assert.equal(p.excluded('public/a.md'), false);
  }));

test('a workspace that is not a git repository still honours its own .gitignore', () =>
  inProject({ '.gitignore': 'tmp/\n', 'tmp/a.txt': '', 'src/a.txt': '' }, { git: false }, (p) => {
    assert.equal(p.excluded('tmp/a.txt'), true);
    assert.equal(p.excluded('src/a.txt'), false);
  }));

test('a workspace opened inside a larger repository honours the repository root .gitignore', () =>
  inProject({ '.gitignore': 'generated/\n', 'packages/app/generated/a.ts': '', 'packages/app/src/a.ts': '' },
    { workspaceAt: 'packages/app' }, (p) => {
      assert.equal(p.excluded('packages/app/generated/a.ts'), true);
      assert.equal(p.excluded('packages/app/src/a.ts'), false);
    }));

test('a dotfiles repository at the home directory does not swallow the workspace', async () => {
  const home = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-home-')));
  const realHome = process.env.HOME;
  process.env.HOME = home;
  try {
    fs.mkdirSync(path.join(home, '.git'));
    fs.writeFileSync(path.join(home, '.gitignore'), '*\n');                  // the dotfiles trick: ignore everything
    fs.mkdirSync(path.join(home, 'work', 'proj'), { recursive: true });
    fs.writeFileSync(path.join(home, 'work', 'proj', 'app.ts'), '');
    stub.vscode.workspace.workspaceFolders = [{ uri: { fsPath: path.join(home, 'work', 'proj') } }];
    clearPrivacyCaches();
    assert.equal(isExcluded(path.join(home, 'work', 'proj', 'app.ts')), false);
    stub.vscode.workspace.workspaceFolders = undefined;                       // and a file outside any workspace
    clearPrivacyCaches();
    assert.equal(isExcluded(path.join(home, 'work', 'proj', 'app.ts')), false);
  } finally {
    process.env.HOME = realHome;
    stub.vscode.workspace.workspaceFolders = undefined;
    fs.rmSync(home, { recursive: true, force: true });
    clearPrivacyCaches();
  }
});

test('a file outside every workspace and every repository is not judged by any .gitignore', () =>
  inProject({ '.gitignore': '*\n', 'a.txt': '' }, { git: false, workspace: false }, (p) => {
    assert.equal(p.excluded('a.txt'), false);
  }));

test('a file outside the workspace but inside a repository still honours that repository', () =>
  inProject({ '.gitignore': 'out.txt\n', 'out.txt': '', 'in.txt': '' }, { workspace: false }, (p) => {
    assert.equal(p.excluded('out.txt'), true);
    assert.equal(p.excluded('in.txt'), false);
  }));

test('a missing file is judged by its path', () =>
  inProject({ '.gitignore': '*.secret-ish\n' }, (p) => {
    assert.equal(p.excluded('not-yet.secret-ish'), true);
    assert.equal(p.excluded('not-yet.txt'), false);
  }));

test('an edited .gitignore takes effect once the cache is cleared', () =>
  inProject({ '.gitignore': '', 'a.txt': '' }, (p) => {
    assert.equal(p.excluded('a.txt'), false);
    fs.writeFileSync(p.at('.gitignore'), 'a.txt\n');
    clearPrivacyCaches();
    assert.equal(p.excluded('a.txt'), true);
  }));

test('an unreadable or hostile .gitignore does not break anything', () =>
  inProject({ '.gitignore': '[unclosed\n\\\n***\n(((\n' + 'x'.repeat(10000) + '\n!\n/\n', 'a.txt': '' }, (p) => {
    assert.doesNotThrow(() => p.excluded('a.txt'));
    assert.doesNotThrow(() => p.excluded('nested/dir/b.txt'));
  }));

test('agrees with `git check-ignore` on generated repositories', (t) => {
  const { execFileSync } = require('node:child_process');
  try {
    execFileSync('git', ['--version'], { stdio: 'ignore' });
  } catch {
    t.skip('git is not installed');
    return;
  }
  let seed = 7;
  const rnd = () => ((seed = (seed * 1664525 + 1013904223) >>> 0) / 2 ** 32);
  const pick = (list) => list[Math.floor(rnd() * list.length)];
  const names = ['a', 'b', 'src', 'build', 'dist', 'lib', 'x.log', 'y.txt', 'z.tmp', 'node_modules', 'cache', 'keep', 'data1', 'data2', 'tmp.o', 'My File.txt', 'q'];
  const patterns = ['*.log', '!keep.log', 'build/', '/dist', 'src/*.tmp', '**/cache', 'a/**/q', 'lib/**', 'data?', 'data[12]', 'data[!1]',
    '*.o', '!*.o', 'node_modules', '/q', 'b/', '!b/keep', '*.txt', '!y.txt', 'x*', '**/z.tmp', 'src/**/build', '\\#x', 'My\\ File.txt', 'q/'];
  let compared = 0;
  const mismatches = [];
  for (let round = 0; round < 12; round++) {
    const dir = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-gitcheck-')));
    try {
      execFileSync('git', ['init', '-q'], { cwd: dir });
      const rules = Array.from({ length: 3 + Math.floor(rnd() * 6) }, () => pick(patterns));
      fs.writeFileSync(path.join(dir, '.gitignore'), rules.join('\n') + '\n');
      fs.mkdirSync(path.join(dir, 'src'), { recursive: true });
      if (rnd() < 0.5) fs.writeFileSync(path.join(dir, 'src', '.gitignore'), [pick(patterns), pick(patterns)].join('\n') + '\n');
      const files = [];
      for (let i = 0; i < 60; i++) {
        const parts = Array.from({ length: 1 + Math.floor(rnd() * 3) }, () => pick(names));
        const rel = parts.join('/');
        const blocked = parts.some((_, k) => k > 0 && fs.existsSync(path.join(dir, ...parts.slice(0, k))) && !fs.statSync(path.join(dir, ...parts.slice(0, k))).isDirectory());
        if (blocked || fs.existsSync(path.join(dir, rel))) continue;
        fs.mkdirSync(path.dirname(path.join(dir, rel)), { recursive: true });
        fs.writeFileSync(path.join(dir, rel), 'x');
        files.push(rel);
      }
      stub.vscode.workspace.workspaceFolders = [{ uri: { fsPath: dir } }];
      clearPrivacyCaches();
      for (const rel of files) {
        let git = true;
        try { execFileSync('git', ['check-ignore', '-q', '--no-index', rel], { cwd: dir }); } catch { git = false; }
        compared++;
        if (git !== isExcluded(path.join(dir, rel))) mismatches.push(`${rel} (git says ${git}) with ${JSON.stringify(rules)}`);
      }
    } finally {
      stub.vscode.workspace.workspaceFolders = undefined;
      fs.rmSync(dir, { recursive: true, force: true });
      clearPrivacyCaches();
    }
  }
  t.diagnostic(`${compared} paths compared with git`);
  assert.ok(compared > 200);
  assert.deepEqual(mismatches, []);
});

// --- .dlneignore -----------------------------------------------------------------------------

test('.dlneignore excludes with the same syntax', () =>
  inProject({ '.dlneignore': '# keep the model out of these\nprivate/\n*.draft.md\n', 'private/a.ts': '', 'notes.draft.md': '', 'src/a.ts': '' }, (p) => {
    assert.equal(p.excluded('private/a.ts'), true);
    assert.equal(p.excluded('notes.draft.md'), true);
    assert.equal(p.excluded('src/a.ts'), false);
    assert.match(excludedReason(p.at('private/a.ts')), /\.dlneignore/);
  }));

test('.dlneignore can share a git-ignored file with a ! line, but never a credentials file', () =>
  inProject({
    '.gitignore': 'generated/\n.env\n',
    '.dlneignore': '!generated/\n!.env\n',
    'generated/api.ts': '', '.env': '',
  }, (p) => {
    assert.equal(p.excluded('generated/api.ts'), false);
    assert.equal(p.excluded('.env'), true);
  }));

// --- links -----------------------------------------------------------------------------------

test('a symbolic link cannot be used to read an excluded file', (t) =>
  inProject({ '.gitignore': 'ignored.txt\n', '.env': 'X=1', 'ignored.txt': 'x', 'plain.txt': 'x' }, (p) => {
    try {
      fs.symlinkSync(p.at('.env'), p.at('notes.txt'));
      fs.symlinkSync(p.at('ignored.txt'), p.at('innocent.txt'));
      fs.symlinkSync(p.at('plain.txt'), p.at('fine.txt'));
    } catch {
      t.skip('symlinks are not available here');
      return;
    }
    clearPrivacyCaches();
    assert.equal(p.excluded('notes.txt'), true);
    assert.equal(p.excluded('innocent.txt'), true);
    assert.equal(p.excluded('fine.txt'), false);
  }));

// --- speed -----------------------------------------------------------------------------------

test('the check is cached: a repeated call (every keystroke) costs microseconds', (t) =>
  inProject({ '.gitignore': Array.from({ length: 300 }, (_, i) => `ignored-${i}/\n*.x${i}`).join('\n'), 'src/deep/er/file.ts': '' }, (p) => {
    const file = p.at('src/deep/er/file.ts');
    isExcluded(file);
    const runs = 20000;
    const t0 = process.hrtime.bigint();
    for (let i = 0; i < runs; i++) isExcluded(file);
    const each = Number(process.hrtime.bigint() - t0) / 1e3 / runs;
    t.diagnostic(`${each.toFixed(2)} microseconds per cached call`);
    assert.ok(each < 50, `${each.toFixed(1)} microseconds`);
    // The first, uncached call walks the folders and reads the ignore files.
    clearPrivacyCaches();
    const cold0 = process.hrtime.bigint();
    isExcluded(file);
    const cold = Number(process.hrtime.bigint() - cold0) / 1e6;
    t.diagnostic(`${cold.toFixed(2)} ms for the first call`);
    assert.ok(cold < 100, `${cold.toFixed(1)} ms`);
  }));
