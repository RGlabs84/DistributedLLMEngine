'use strict';
/**
 * One DLNE icon everywhere.
 *
 * The owner's rule: the only DLNE mark is the cyan-and-pink brain with "DLNE" under it
 * (media/dlne_logo.png). Anything else is gone for good, so these tests fail if a second mark
 * comes back: another file in media/, a package.json icon that points elsewhere, an emoji
 * standing in for the logo, or a webview that draws its own.
 *
 * The Activity Bar is the one place a colour image cannot go (VS Code draws its icon as a mask in
 * a single theme colour), so it gets media/dlne_activitybar.svg: the same brain, traced from the
 * logo by scripts/make_dlne_icons.py as a silhouette.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const stub = require('./helpers/vscode-stub');
const { SidebarProvider } = require('../dist/views/sidebarProvider');
const pkg = require('../package.json');

const ROOT = path.join(__dirname, '..');
const MEDIA = path.join(ROOT, 'media');
const LOGO = 'dlne_logo.png';
const ACTIVITY_BAR = 'dlne_activitybar.svg';
const ALLOWED_MEDIA = [ACTIVITY_BAR, LOGO];

const BRAIN_EMOJI = '\u{1F9E0}';

function walk(dir, extensions) {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) return entry.name === 'node_modules' ? [] : walk(full, extensions);
    return extensions.some((ext) => entry.name.endsWith(ext)) ? [full] : [];
  });
}

test('media/ holds only the official logo and its Activity Bar silhouette', () => {
  const found = fs.readdirSync(MEDIA, { recursive: true }).map((p) => p.split(path.sep).join('/')).sort();
  assert.deepEqual(found, ALLOWED_MEDIA, `unexpected file(s) in media/: ${found.filter((f) => !ALLOWED_MEDIA.includes(f))}`);
});

test('the logo is the 512 x 512 PNG every other icon is cut from', () => {
  const data = fs.readFileSync(path.join(MEDIA, LOGO));
  assert.equal(data.subarray(0, 8).toString('hex'), '89504e470d0a1a0a');
  assert.equal(data.readUInt32BE(16), 512);
  assert.equal(data.readUInt32BE(20), 512);
});

test('package.json points every icon at the logo or the Activity Bar silhouette', () => {
  assert.equal(pkg.icon, `media/${LOGO}`);
  const containers = Object.values(pkg.contributes.viewsContainers).flat();
  assert.ok(containers.length > 0);
  for (const container of containers) {
    assert.equal(container.icon, `media/${ACTIVITY_BAR}`, `${container.id} must use the Activity Bar silhouette`);
  }
  for (const rel of [pkg.icon, ...containers.map((c) => c.icon)]) {
    assert.ok(fs.existsSync(path.join(ROOT, rel)), `${rel} does not exist`);
  }
});

test('the chat participant uses the logo', () => {
  const source = fs.readFileSync(path.join(ROOT, 'src', 'views', 'nativeChatParticipant.ts'), 'utf8');
  assert.match(source, /participant\.iconPath = vscode\.Uri\.joinPath\(context\.extensionUri, 'media', 'dlne_logo\.png'\)/);
});

test('the source only ever names the two files that are allowed in media/', () => {
  const named = new Set();
  for (const file of walk(path.join(ROOT, 'src'), ['.ts'])) {
    const text = fs.readFileSync(file, 'utf8');
    for (const m of text.matchAll(/'media'\s*,\s*'([^']+)'/g)) named.add(m[1]);
    for (const m of text.matchAll(/media\/([\w.-]+\.(?:png|svg|ico|jpe?g|gif|webp))/g)) named.add(m[1]);
  }
  assert.ok(named.has(LOGO));
  for (const name of named) assert.ok(ALLOWED_MEDIA.includes(name), `src/ uses media/${name}, which is not an official DLNE icon`);
});

test('the Activity Bar icon is one filled vector silhouette: no text, image, gradient or script', () => {
  const svg = fs.readFileSync(path.join(MEDIA, ACTIVITY_BAR), 'utf8');
  assert.match(svg, /^<svg xmlns="http:\/\/www\.w3\.org\/2000\/svg" viewBox="0 0 24 24"/);
  assert.equal((svg.match(/<path\b/g) || []).length, 1, 'one path');
  assert.equal((svg.match(/<[a-z]+/g) || []).filter((t) => t !== '<svg' && t !== '<path').length, 0, 'only svg and path');
  assert.doesNotMatch(svg, /<(text|image|script|style|linearGradient|radialGradient|filter|use|foreignObject)\b|data:|href=/);
  assert.ok(svg.length < 20000, 'a silhouette, not an embedded picture');
  const d = svg.match(/ d="([^"]+)"/)[1];
  const coords = [...d.matchAll(/(-?\d+(?:\.\d+)?) (-?\d+(?:\.\d+)?)/g)].map((m) => [Number(m[1]), Number(m[2])]);
  assert.ok(coords.length > 200, 'traced outline with folds, not a placeholder shape');
  const xs = coords.map((c) => c[0]);
  const ys = coords.map((c) => c[1]);
  assert.ok(Math.min(...xs) >= 0.5 && Math.max(...xs) <= 23.5 && Math.min(...ys) >= 0.5 && Math.max(...ys) <= 23.5, 'inside the 24-unit box');
  assert.ok(Math.max(...xs) - Math.min(...xs) > 20, 'fills the width, so it is readable at 24 px');
});

test('no brain emoji stands in for the logo anywhere in the extension source', () => {
  for (const file of walk(path.join(ROOT, 'src'), ['.ts'])) {
    assert.ok(!fs.readFileSync(file, 'utf8').includes(BRAIN_EMOJI), `${path.relative(ROOT, file)} uses the brain emoji as a mark`);
  }
});

test('the sidebar webview shows the logo image and draws no mark of its own', () => {
  stub.reset();
  const noop = { onStatusChange() {} };
  const status = { state: 'idle', gpu: { hasGpu: true, name: 'RTX 4060', vramTotalGb: 8, api: 'vulkan' }, cpuPool: false, vramContributedGb: 0 };
  const provider = new SidebarProvider({ fsPath: ROOT }, {}, { ...noop, getStatus: () => status });
  const html = provider._getHtmlForWebview();

  assert.ok(!html.includes(BRAIN_EMOJI), 'the brain emoji is not the logo');
  assert.ok(!/&#x?1f9e0;|&#129504;/i.test(html));
  assert.ok(!/<svg|<symbol|<canvas|data:image/i.test(html), 'no inline or embedded mark');
  const images = [...html.matchAll(/<img\b[^>]*>/g)].map((m) => m[0]);
  assert.equal(images.length, 2, 'the card header and the welcome line');
  for (const img of images) assert.match(img, /src="[^"]*media\/dlne_logo\.png"/);
  assert.match(html, /<img class="logo"[^>]*>Donate compute/);
  assert.match(html, /<img class="logo"[^>]*>DLNE Code Assistant/);
});

test('the sidebar loads the logo through the webview, from inside the extension folder', () => {
  const source = fs.readFileSync(path.join(ROOT, 'src', 'views', 'sidebarProvider.ts'), 'utf8');
  assert.match(source, /this\._view\.webview\.asWebviewUri\(logo\)/);
  assert.match(source, /localResourceRoots: \[this\._extensionUri\]/);
});

test('buttons keep a background, text colour and border when the theme leaves the VS Code button colours out', () => {
  const source = fs.readFileSync(path.join(ROOT, 'src', 'views', 'sidebarProvider.ts'), 'utf8');
  const css = (selector) => source.match(new RegExp(`\\n    ${selector.replace(/[.:]/g, '\\$&')} \\{([^}]*)\\}`))[1];
  assert.match(css('button'), /--vscode-button-background, #[0-9a-f]{6}\)/);
  assert.match(css('button'), /--vscode-button-foreground, #[0-9a-f]+\)/);
  assert.match(css('button.secondary'), /--vscode-button-secondaryBackground, #[0-9a-f]{6}\)/);
  assert.match(css('button.secondary'), /--vscode-button-secondaryForeground, #[0-9a-f]+\)/);
  assert.match(css('button.secondary'), /border-color:/);
  assert.match(css('button.secondary:hover'), /--vscode-button-secondaryHoverBackground, #[0-9a-f]{6}\)/);
});
