'use strict';
/**
 * A URI that is not a `file` one but has a file behind it (a cell of a notebook, a `git:` copy, a remote file) is judged
 * as that file by the exclusion rules (privacy/exclude.ts), so .gitignore, .dlneignore and the credential rules reach it
 * everywhere: what the agent's diagnostics tool lists, what is attached to a message, what /fix is asked about. These
 * rules used to skip every URI that was not a `file` one, so a cell of an ignored notebook was let through. A URI with no
 * file behind it (an untitled buffer, an output channel) has nothing to exclude and is left alone.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');

const stub = require('./helpers/vscode-stub');

// The compiled modules take the top-level members of the vscode module when they load, so what the tests drive is
// installed first. (Each test file runs in its own process.)
let diagnostics = [];
stub.vscode.languages = { getDiagnostics: () => diagnostics };
stub.vscode.DiagnosticSeverity = { Error: 0, Warning: 1 };

const made = [];
const tmp = (prefix) => {
  const dir = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), prefix)));
  made.push(dir);
  return dir;
};
process.on('exit', () => made.forEach((dir) => fs.rmSync(dir, { recursive: true, force: true })));
process.env.HOME = tmp('dlne-schemes-home-');
process.env.USERPROFILE = process.env.HOME;

const { ToolRegistry } = require('../dist/agent/tools');
const { DlneClient } = require('../dist/client/dlneClient');
const { DlneFimProvider } = require('../dist/completion/fimProvider');
const { clearPrivacyCaches, excludedReason, excludedReasonSkipping, filePathOf, isExcluded } = require('../dist/privacy/exclude');

/** A URI of another scheme the way VS Code builds one: its path is the file's. */
const foreign = (scheme, fsPath, fragment = '') => ({
  scheme,
  fsPath,
  path: fsPath.split(path.sep).join('/'),
  fragment,
  toString: () => `${scheme}:${fsPath}${fragment ? `#${fragment}` : ''}`,
});
const fileUri = (fsPath) => ({ scheme: 'file', fsPath, path: fsPath.split(path.sep).join('/'), toString: () => `file://${fsPath}` });

/** A project: a git repository whose .gitignore hides scratch/ and whose .dlneignore hides drafts/. */
function project(body) {
  stub.reset();
  clearPrivacyCaches();
  const root = tmp('dlne-schemes-ws-');
  const files = {
    '.gitignore': 'scratch/\n',
    '.dlneignore': 'drafts/\n',
    'scratch/private.ipynb': '{}',
    'drafts/plan.ipynb': '{}',
    'keep.ipynb': '{}',
    'src/app.ts': 'export {};\n',
    '.env': 'A=1\n',
  };
  for (const [rel, content] of Object.entries(files)) {
    fs.mkdirSync(path.dirname(path.join(root, rel)), { recursive: true });
    fs.writeFileSync(path.join(root, rel), content);
  }
  fs.mkdirSync(path.join(root, '.git'));
  stub.vscode.workspace.workspaceFolders = [{ uri: { fsPath: root, scheme: 'file' } }];
  clearPrivacyCaches();
  const at = (rel) => path.join(root, rel);
  return Promise.resolve(body({ root, at })).finally(() => {
    stub.vscode.workspace.workspaceFolders = undefined;
    diagnostics = [];
    clearPrivacyCaches();
  });
}

const SCHEMES = ['vscode-notebook-cell', 'git', 'vscode-remote', 'vscode-vfs', 'untitled'];

test('a URI of another scheme with a path is judged as the file at that path: .gitignore, .dlneignore and credentials apply', () =>
  project(async ({ at }) => {
    for (const scheme of SCHEMES) {
      const as = (rel) => foreign(scheme, at(rel), 'W0s');
      assert.equal(excludedReason(as('scratch/private.ipynb')), 'ignored by .gitignore', `${scheme}: .gitignore`);
      assert.equal(excludedReason(as('drafts/plan.ipynb')), 'ignored by .dlneignore', `${scheme}: .dlneignore`);
      assert.match(excludedReason(as('.env')), /environment file/, `${scheme}: credentials by name`);
      assert.match(excludedReason(foreign(scheme, '/home/someone/.ssh/id_rsa')), /key or credentials file/, `${scheme}: a key`);
      assert.equal(excludedReason(as('keep.ipynb')), null, `${scheme}: an ordinary notebook`);
      assert.equal(isExcluded(as('scratch/private.ipynb')), true);
      assert.equal(isExcluded(as('src/app.ts')), false);
    }
    // The same answers for a `file` URI and for a plain path, as before.
    assert.equal(excludedReason(fileUri(at('scratch/private.ipynb'))), 'ignored by .gitignore');
    assert.equal(excludedReason(at('scratch/private.ipynb')), 'ignored by .gitignore');
    assert.equal(excludedReason(fileUri(at('keep.ipynb'))), null);
  }));

test('a symbolic link in front of an ignored notebook does not hide it from a cell either', () =>
  project(async ({ root, at }) => {
    try {
      fs.symlinkSync(at('scratch/private.ipynb'), path.join(root, 'link.ipynb'));
    } catch {
      return; // a platform where a link cannot be made without privileges
    }
    clearPrivacyCaches();
    assert.equal(excludedReason(foreign('vscode-notebook-cell', at('link.ipynb'), 'W0s')), 'ignored by .gitignore');
  }));

test('with no file behind it a URI is left alone: an untitled buffer, an output channel, a path-less URI', () =>
  project(async ({ at }) => {
    assert.equal(excludedReason({ scheme: 'untitled', fsPath: 'Untitled-1' }), null);
    assert.equal(excludedReason({ scheme: 'output', path: 'extension-output-dlne' }), null);
    assert.equal(excludedReason({ scheme: 'output', fsPath: 'extension-output-dlne', path: 'extension-output-dlne' }), null);
    // Only an absolute fsPath says a file is behind a URI (a real Uri always has one): a path alone does not.
    assert.equal(excludedReason({ scheme: 'vscode-vfs', path: at('scratch/x.ts') }), null);
    assert.equal(excludedReason({ scheme: 'vscode-vfs', fsPath: '', path: '' }), null);
    // Its name can still be a credentials file's.
    assert.match(excludedReason({ scheme: 'untitled', fsPath: '.env', path: '.env' }), /environment file/);
    for (const given of [undefined, null, '', {}, { scheme: 'git' }, { fsPath: '' }]) assert.equal(isExcluded(given), false);
    assert.equal(filePathOf({ fsPath: 'relative/a.ts' }), undefined);
    assert.equal(filePathOf(undefined), undefined);
  }));

test('the memory files\' variant judges a URI by its file too, and skips only the ignore files it is told to', () =>
  project(async ({ at }) => {
    const skipGit = ['.gitignore', '.git/info/exclude'];
    const cell = (rel) => foreign('vscode-notebook-cell', at(rel), 'W0s');
    assert.equal(excludedReasonSkipping(cell('scratch/private.ipynb'), skipGit), null, 'ignored by .gitignore only: the skipped file');
    assert.equal(excludedReasonSkipping(cell('drafts/plan.ipynb'), skipGit), 'ignored by .dlneignore');
    assert.match(excludedReasonSkipping(cell('.env'), skipGit), /environment file/);
    assert.equal(excludedReasonSkipping(cell('scratch/private.ipynb'), []), 'ignored by .gitignore');
    assert.equal(excludedReasonSkipping({ scheme: 'untitled', fsPath: 'Untitled-1' }, []), null);
  }));

// ---- what the agent's diagnostics tool lists --------------------------------------------------------------------

test('get_workspace_diagnostics lists no cell of an ignored notebook, and still lists the others', () =>
  project(async ({ at }) => {
    const diag = (message, line) => ({ severity: 0, message, range: { start: { line } } });
    diagnostics = [
      [foreign('vscode-notebook-cell', at('scratch/private.ipynb'), 'W0s'), [diag('NameError: secret_cell', 2)]],
      [foreign('vscode-notebook-cell', at('drafts/plan.ipynb'), 'W1s'), [diag('NameError: draft_cell', 0)]],
      [foreign('git', at('.env')), [diag('bad value in env', 0)]],
      [foreign('vscode-notebook-cell', at('keep.ipynb'), 'W2s'), [diag('NameError: kept_cell', 4)]],
      [fileUri(at('src/app.ts')), [diag('Cannot find name y.', 6)]],
      [fileUri(at('scratch/private.ipynb')), [diag('the file itself', 1)]],
    ];
    const result = await ToolRegistry.executeTool('get_workspace_diagnostics', {});
    assert.equal(result.is_error, undefined);
    assert.equal(
      result.output,
      '[ERROR] keep.ipynb:5: NameError: kept_cell\n[ERROR] src/app.ts:7: Cannot find name y.'
    );
  }));

// ---- autocomplete -------------------------------------------------------------------------------------------

function document(text, uri) {
  const lines = text.split('\n');
  const offsetOf = (p) => lines.slice(0, p.line).reduce((n, l) => n + l.length + 1, 0) + p.character;
  return { uri, lineCount: lines.length, lineAt: (i) => ({ text: lines[i] }), getText: (range) => text.slice(offsetOf(range.start), offsetOf(range.end)) };
}
const position = (line, character) => new stub.vscode.Position(line, character);
const token = () => ({ isCancellationRequested: false, onCancellationRequested() { return { dispose() {} }; } });

async function coordinator() {
  const requests = [];
  const server = http.createServer((req, res) => {
    let body = '';
    req.on('data', (c) => (body += c));
    req.on('end', () => {
      requests.push(JSON.parse(body || '{}'));
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ choices: [{ text: 'x' }] }));
    });
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  stub.state.settings['dlne.serverUrl'] = `http://127.0.0.1:${server.address().port}`;
  stub.state.settings['dlne.inlineCompletionDelay'] = 75;
  return { requests, close() { server.closeAllConnections(); server.close(); } };
}

test('autocomplete: a cell of an ignored notebook gets no completion request, and an ordinary file still does', () =>
  project(async ({ at }) => {
    const c = await coordinator();
    try {
      const provider = new DlneFimProvider(new DlneClient());
      const text = 'secret = load()\nvalue = ';
      for (const rel of ['scratch/private.ipynb', 'drafts/plan.ipynb']) {
        for (const scheme of ['vscode-notebook-cell', 'git', 'vscode-remote']) {
          const items = await provider.provideInlineCompletionItems(document(text, foreign(scheme, at(rel), 'W0s')), position(1, 8), {}, token());
          assert.equal(items, null, `${scheme} ${rel}`);
        }
      }
      assert.equal(c.requests.length, 0, 'nothing was sent');
      const ok = await provider.provideInlineCompletionItems(document('const x = ', fileUri(at('src/app.ts'))), position(0, 10), {}, token());
      assert.equal(ok[0].insertText, 'x');
      assert.equal(c.requests.length, 1);
    } finally {
      c.close();
    }
  }));
