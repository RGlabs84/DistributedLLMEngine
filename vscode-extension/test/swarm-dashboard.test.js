/**
 * The DLNE Swarm dashboard (src/views/swarmDashboard.ts): an editor tab with the swarm's live state. Its numbers
 * come from the coordinator's public status; the page never treats them as markup.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');

const stub = require('./helpers/vscode-stub');
stub.vscode.ViewColumn = { Active: -1 };       // before the module loads: it keeps the keys it saw then
const { dashboardModel, dashboardHtml, SwarmDashboard } = require('../dist/views/swarmDashboard');
const { FakeElement } = require('./helpers/sidebar-page');

// Trimmed from TheWall's /v1/cluster/status on 2026-10-27 (2.5.3, IQ4_XS with the laptop's 4070 as a LAN shard).
const STATUS = {
  status: 'Active', server_version: '2.5.3', global_active_model: 'Qwen3.6-35B-A3B-UD-IQ4_XS.gguf',
  coding_model: { name: 'Qwen3.6 35B-A3B (IQ4_XS)', context: 163840, tokens_per_second: 37.3 },
  coding_tiers: [
    { id: 'qwen36-a3b', name: 'Qwen3.6 35B-A3B', min_vram_gb: 15, enabled: true, unlocked: true, active: false },
    { id: 'qwen36-a3b-iq4xs', name: 'Qwen3.6 35B-A3B (IQ4_XS)', min_vram_gb: 24, enabled: true, unlocked: true, active: true },
    { id: 'qwen36-a3b-q6', name: 'Qwen3.6 35B-A3B (Q6)', min_vram_gb: 32, enabled: true, unlocked: false, active: false },
    { id: 'qwen38-flash-next-q2', name: 'Qwen3.8 Flash', min_vram_gb: 48, enabled: false, unlocked: false, active: false },
  ],
  coding_queue: { slots: 3, in_use: 1, waiting: 0 },
  app_pool: { members: 1, members_ready: 1, active_model: 'Qwen3.6 35B-A3B', requests_in_flight: 0, median_answer_seconds: null },
  coding_members: { members: 0, ready: 0, busy: 0, decode_tps_median: null },
  gpu_pool: { gpus: 3, vram_gb: 24.0, gpus_standing_by: 0, tokens_per_second: 12.5, tokens_last_hour: 26004,
              reach: { host: 2, best: 1, good: 0, usable: 0, too_far: 0, unmeasured: 0 } },
  cpu_pool: { machines: 1, cores: 8, ram_gb: 16.0, tokens_per_second: 0.5, tokens_last_hour: 300 },
  model_loading: { is_loading: false, progress_pct: 100.0 },
  auto_swap: { enabled: true, state: 'steady', detail: 'Serving Qwen3.6 35B-A3B (IQ4_XS).' },
  policy: { coding_context_tokens: 54613 },
};

const WORKER = { state: 'contributing', gpu: { name: 'RTX 4070 Laptop GPU' }, vramContributedGb: 8, cpuPool: false,
                 role: 'rpc', message: 'Actively contributing 8 GB via VULKAN!' };

test("the dashboard shows the coordinator's live numbers and this machine's worker", () => {
  const m = dashboardModel(STATUS, WORKER);
  assert.equal(m.online, true);
  assert.equal(m.serverVersion, '2.5.3');
  assert.deepEqual(m.model, { name: 'Qwen3.6 35B-A3B (IQ4_XS)', tokensPerSecond: 37.3, contextPerRequest: 54613, contextPerRequestMax: null, loadingPct: null });
  assert.deepEqual(m.queue, { slots: 3, inUse: 1, waiting: 0 });
  assert.equal(m.gpuPool.gpus, 3);
  assert.deepEqual(m.gpuPool.reach, { host: 2, best: 1, good: 0, usable: 0, tooFar: 0, unmeasured: 0 });
  assert.equal(m.tokensLastHour, 26304, 'GPU and CPU pools together');
  assert.deepEqual(m.sample, { tokensPerSecond: 13, inUse: 1, waiting: 0 });
  assert.deepEqual(m.tiers.map((t) => t.state), ['unlocked', 'serving', 'needs more VRAM', 'off']);
  assert.equal(m.machine.gpu, 'RTX 4070 Laptop GPU');
  assert.ok(m.machine.role, 'the job is named');
});

test('an unreachable coordinator or a status with fields missing still renders, with nothing invented', () => {
  const down = dashboardModel(null, null, 'The coordinator cannot be reached.');
  assert.equal(down.online, false);
  assert.equal(down.sample, null, 'no sample: the charts are not fed zeros');
  assert.equal(down.tokensLastHour, null);
  assert.equal(down.machine, null);
  const sparse = dashboardModel({ server_version: '2.6.0', coding_queue: { slots: 'three' } }, null);
  assert.equal(sparse.queue.slots, null);
  assert.deepEqual(sparse.tiers, []);
  const loading = dashboardModel({ ...STATUS, model_loading: { is_loading: true, progress_pct: 42 } }, null);
  assert.equal(loading.model.loadingPct, 42);
});

test('the page is locked down: one nonce for script and style, nothing remote, no data parsed as markup', () => {
  const html = dashboardHtml('NONCE123');
  const csp = /Content-Security-Policy" content="([^"]+)"/.exec(html)[1];
  assert.equal(csp, "default-src 'none'; style-src 'nonce-NONCE123'; script-src 'nonce-NONCE123';");
  assert.match(html, /<script nonce="NONCE123">/);
  assert.match(html, /<style nonce="NONCE123">/);
  const script = html.slice(html.indexOf('<script'));
  assert.doesNotMatch(script, /innerHTML|insertAdjacentHTML|outerHTML|eval\(|new Function/);
  assert.doesNotMatch(html, /(src|href)="https?:/, 'nothing loaded from the network');
});

test('one dashboard tab at a time; it stops asking the coordinator when closed', async () => {
  stub.reset();
  const panels = [];
  stub.vscode.window.createWebviewPanel = (id, title) => {
    const panel = {
      id, title, visible: true, revealed: 0, posted: [], disposeListeners: [],
      webview: { html: '', postMessage: async (m) => { panel.posted.push(m); return true; } },
      reveal() { panel.revealed++; },
      onDidDispose(fn) { panel.disposeListeners.push(fn); },
    };
    panels.push(panel);
    return panel;
  };
  const client = { getClusterStatusDetailed: async () => ({ status: 'ok', data: STATUS }) };
  const workerManager = { getStatus: () => WORKER };
  const context = { extensionUri: { fsPath: '/ext' } };
  try {
    SwarmDashboard.show(context, client, workerManager);
    SwarmDashboard.show(context, client, workerManager);
    assert.equal(panels.length, 1);
    assert.equal(panels[0].revealed, 1);
    assert.equal(panels[0].title, 'DLNE Swarm');
    await new Promise((resolve) => setTimeout(resolve, 20));
    const message = panels[0].posted.at(-1);
    assert.equal(message.type, 'model');
    assert.equal(message.model.queue.inUse, 1);
    assert.equal(message.samples.length, 1);
    panels[0].disposeListeners.forEach((fn) => fn());
    SwarmDashboard.show(context, client, workerManager);
    assert.equal(panels.length, 2, 'closed: the next open is a new tab');
    panels[1].disposeListeners.forEach((fn) => fn());
  } finally {
    delete stub.vscode.window.createWebviewPanel;
  }
});

test('the dashboard has its command, and a graph icon on the DLNE sidebar\'s title bar', () => {
  // 2026-10-27: the menu was first written inside the chat participant's entry, where VS Code ignores it.
  const pkg = require('../package.json');
  const command = pkg.contributes.commands.find((c) => c.command === 'dlne.openSwarmDashboard');
  assert.equal(command.icon, '$(graph)');
  assert.deepEqual(pkg.contributes.menus['view/title'].find((m) => m.command === 'dlne.openSwarmDashboard'),
    { command: 'dlne.openSwarmDashboard', when: 'view == dlne.chatView', group: 'navigation' });
  assert.ok(Object.values(pkg.contributes.views).flat().some((v) => v.id === 'dlne.chatView'));
  assert.ok(pkg.contributes.chatParticipants.every((p) => !('menus' in p)));
});

// ---- the page itself ----------------------------------------------------------------------------------------------
//
// The page's own script, run under vm against a small DOM, with one status message delivered as the extension posts it.
// (Charts are not drawn: with fewer than two samples the page says it is collecting.)

class PageElement extends FakeElement {
  get lastChild() { return this.children[this.children.length - 1] || null; }
}

function dashboardPage(status) {
  const byId = {};
  const online = new PageElement('span');                       // the page expects its dot and its label inside it
  const dot = new PageElement('span');
  dot.classes.add('dot');
  online.append(dot, new PageElement('span'));
  byId.online = online;
  const listeners = {};
  const sandbox = {
    document: {
      getElementById: (id) => (byId[id] ??= new PageElement('div')),
      createElement: (tag) => new PageElement(tag),
      createElementNS: (ns, tag) => new PageElement(tag),
      body: new PageElement('body'),
    },
    window: { addEventListener: (type, fn) => { listeners[type] = fn; } },
    ResizeObserver: class { observe() {} },
  };
  vm.createContext(sandbox);
  const html = dashboardHtml('NONCE');
  vm.runInContext(/<script nonce="NONCE">([\s\S]*?)<\/script>/.exec(html)[1], sandbox);
  listeners.message({ data: { type: 'model', model: dashboardModel(status, WORKER), samples: [] } });
  return byId;
}

/** The automatic-switching chip of the model card ({colour, label}) and the sentence shown under it. */
function swapChip(state, detail) {
  const page = dashboardPage({ ...STATUS, auto_swap: { enabled: true, state, detail } });
  const card = page['model-card'];
  // The page sets class as an attribute (el(tag, {class})), which is how the fake DOM keeps it.
  const withClass = (name) => card.descendants().filter((node) => (node.getAttribute('class') || '').split(' ').includes(name));
  const chip = withClass('pill')[0];
  return { colour: chip.children[0].style.background, label: chip.children[1].textContent,
           details: withClass('detail').map((node) => node.textContent) };
}

test('automatic switching shows as a chip with an icon, a label and a colour for each state the coordinator reports', () => {
  const chips = {
    steady: ['var(--good)', '✓ Steady'],
    swapping: ['var(--serious)', '↻ Switching model'],
    waiting: ['var(--warning)', '◷ Waiting'],
    downloading: ['var(--warning)', '◷ Downloading'],
    cooldown: ['var(--warning)', '◷ Cooldown'],
    held: ['var(--warning)', '◷ Held'],
    off: ['var(--warning)', '◷ Off'],
    unknown: ['var(--warning)', '◷ Off'],
  };
  for (const [state, [colour, label]] of Object.entries(chips)) {
    const chip = swapChip(state, '');
    assert.deepEqual([chip.colour, chip.label], [colour, label], state);
  }
  // A state this version does not know is shown as it came, as an error: it is never mistaken for a calm one.
  const unknown = swapChip('frozen', '');
  assert.deepEqual([unknown.colour, unknown.label], ['var(--critical)', '✕ frozen']);
});

test('"held" is a warning chip, not an error, and the coordinator\'s sentence is shown under it', () => {
  // What server 2.6.2's flap breaker sends: the engine was rebuilt too often, so rebuilds nobody needs are held.
  const detail = 'The engine was rebuilt 4 times within an hour: 06:31 auto-swap up to Qwen3.6 35B-A3B (IQ4_XS); ' +
                 '06:46 a GPU left, engine rebuilt without it. So rebuilds that are not needed to keep serving are held ' +
                 'until 07:16 (in 60 min). A step down, a restart after the engine stopped and a manual switch still go ahead.';
  const held = swapChip('held', detail);
  assert.equal(held.colour, 'var(--warning)');
  assert.equal(held.label, '◷ Held');
  assert.deepEqual(held.details, [detail], 'the sentence is the line under the chip');
  assert.equal(dashboardModel({ ...STATUS, auto_swap: { enabled: true, state: 'held', detail } }, null).swap.state, 'held');
});

test('the context per request reads "up to" the largest machine\'s when the swarm reports one bigger than the smallest', () => {
  // 2.6.x: TheWall's coding lanes hold 131,072 tokens and members 40,960; a big request goes where it fits.
  const contextLine = (policy) => {
    const rows = dashboardPage({ ...STATUS, policy })['model-card'].descendants();
    const at = rows.findIndex((node) => node.textContent === 'Context per request');
    return rows[at + 1].textContent;
  };
  const both = dashboardModel({ ...STATUS, policy: { coding_context_tokens: 40960, coding_context_tokens_max: 131072 } }, null);
  assert.equal(both.model.contextPerRequest, 40960);
  assert.equal(both.model.contextPerRequestMax, 131072);
  assert.equal(contextLine({ coding_context_tokens: 40960, coding_context_tokens_max: 131072 }),
    'up to 131.1K tokens (41.0K on the smallest machine)');
  // An old coordinator, or a largest that is no bigger (or not a number): one figure, as before.
  assert.equal(contextLine({ coding_context_tokens: 54613 }), '54.6K tokens');
  assert.equal(contextLine({ coding_context_tokens: 54613, coding_context_tokens_max: 54613 }), '54.6K tokens');
  const odd = dashboardModel({ ...STATUS, policy: { coding_context_tokens: 54613, coding_context_tokens_max: '1M' } }, null);
  assert.equal(odd.model.contextPerRequestMax, null);
  assert.equal(contextLine({}), '–');
});
