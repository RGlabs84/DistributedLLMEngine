'use strict';
/**
 * Lending a GPU as a coding member: the choice and explanation before it starts, which job the coordinator
 * gave the machine and what that means, the privacy facts a member is shown, and the trust id.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const stub = require('./helpers/vscode-stub');
const { WorkerManager } = require('../dist/worker/workerManager');
const { SidebarProvider } = require('../dist/views/sidebarProvider');
const { ClusterStatusBar } = require('../dist/statusbar/clusterStatusBar');
const c = require('../dist/worker/contribution');

process.env.HOME = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-member-ui-home-'));

const ROOT = path.join(__dirname, '..');
const RTX = { hasGpu: true, name: 'RTX 4060', vramTotalGb: 8, api: 'vulkan' };
const CPU_NODE = { hasGpu: false, name: 'CPU Compute Node', vramTotalGb: 4, api: 'cpu' };
const TRUST = '0123456789abcdef0123456789abcdef';

/** The editor's global state, in memory. */
function globalState(initial = {}) {
  const values = { ...initial };
  return {
    values,
    get: (key, fallback) => (key in values ? values[key] : fallback),
    update: async (key, value) => { values[key] = value; },
  };
}

// The constructor's background start-up (probe the GPU, poll the worker, resume if dlne.worker.autoStart is
// set) would race these tests through the shared stub; every machine here is described by the test instead.
WorkerManager.prototype.init = async function init() {};

function manager({ gpu = RTX, state = globalState(), settings = {} } = {}) {
  stub.reset();
  Object.assign(stub.state.settings, settings);
  const wm = new WorkerManager({ subscriptions: [], globalState: state });
  wm.localGpu = gpu;
  // The constructor believes a real worker on the machine running the tests (pgrep, systemd); these tests
  // describe their own machine.
  wm.currentState = 'idle';
  wm.vramContributed = 0;
  return { wm, state, done: () => wm.dispose() };
}

const view = (state, role, roleDetail, extra = {}) => ({
  state,
  cpuPool: false,
  vramContributedGb: 8,
  gpu: RTX,
  role,
  roleDetail,
  ...extra,
});

// ---- the line the worker prints --------------------------------------------------------------------------------

test('DLNE_ROLE: the job, with optional details', () => {
  assert.deepEqual(c.parseRole('DLNE_ROLE: rpc\n'), { role: 'rpc' });
  assert.deepEqual(c.parseRole('x\nDLNE_ROLE: app\ny'), { role: 'app' });
  assert.deepEqual(
    c.parseRole(`[12:00] DLNE_ROLE: coding {"model":"Qwen3.6-35B-A3B-UD-IQ3_S.gguf","tier":"qwen36-a3b","slots":2,"trust_id":"${TRUST}"}`),
    { role: 'coding', model: 'Qwen3.6-35B-A3B-UD-IQ3_S.gguf', tier: 'qwen36-a3b', slots: 2, trustId: TRUST }
  );
  assert.deepEqual(c.parseRole('DLNE_ROLE: coding'), { role: 'coding' });
  assert.deepEqual(c.parseRole('DLNE_ROLE: coding {not json}'), { role: 'coding' }, 'the role alone stands');
  assert.equal(c.parseRole('DLNE_ROLE: admin'), null);
  assert.equal(c.parseRole('DLNE_ROLE: coding but not json'), null);
  assert.equal(c.parseRole('no marker here'), null);
  assert.deepEqual(c.parseRole('DLNE_ROLE: rpc\nDLNE_ROLE: coding'), { role: 'coding' }, 'the last one wins');
});

test('details from the worker are checked, not trusted', () => {
  const parsed = c.parseRole('DLNE_ROLE: coding {"model":42,"tier":"' + 'x'.repeat(200) + '","slots":9999,"trust_id":"<script>alert(1)</script>"}');
  assert.deepEqual(parsed, { role: 'coding' });
  assert.deepEqual(c.parseRole('DLNE_ROLE: coding {"slots":1.5,"trust_id":"ABCDEF0123456789ABCDEF0123456789"}'), { role: 'coding' }, 'lower-case hex only, whole numbers only');
});

// ---- what each job means -----------------------------------------------------------------------------------------

test('the role lines on the sidebar card, for every state and job', () => {
  const idle = c.roleCopy(view('idle'));
  assert.match(idle.roleHtml, /Your GPU's job:<\/strong> the swarm chooses/);
  assert.match(idle.roleHtml, /GPU pool/);
  assert.match(idle.roleHtml, /Coding member/);
  assert.match(c.roleCopy(view('starting')).roleHtml, /choosing a job for your GPU/);

  assert.match(c.roleCopy(view('contributing', 'rpc')).roleHtml, /^<strong>Role:<\/strong> GPU pool\. Your GPU holds a share of one big model/);
  // an engine that never says is a GPU pool member, as it always was
  assert.match(c.roleCopy(view('contributing')).roleHtml, /^<strong>Role:<\/strong> GPU pool\./);

  const coding = c.roleCopy(view('contributing', 'coding', { model: 'Qwen3.6-35B-A3B-UD-IQ3_S.gguf', slots: 2, trustId: TRUST }));
  assert.match(coding.roleHtml, /^<strong>Role:<\/strong> Coding member\. Your GPU runs a whole coding model \(Qwen3\.6-35B-A3B-UD-IQ3_S\) by itself and answers other people's coding requests, about 2 at a time\./);
  assert.match(coding.roleHtml, /anonymous and end-to-end encrypted in transit, and your machine keeps no trace/);
});

test('a coding member sees its trust id with the one-line hint and a Copy link', () => {
  const { trustHtml } = c.roleCopy(view('contributing', 'coding', { trustId: TRUST }));
  assert.match(trustHtml, new RegExp(`<code>${TRUST}</code>`));
  assert.match(trustHtml, new RegExp(`class="copy-trust" data-id="${TRUST}"`));
  assert.ok(trustHtml.includes('Give this to the operator if this is your own machine and you want private repos routed to it.'));
  assert.equal(c.roleCopy(view('contributing', 'coding')).trustHtml, '', 'no trust id yet: nothing to show');
  assert.equal(c.roleCopy(view('contributing', 'rpc', { trustId: TRUST })).trustHtml, '', 'only a coding member has one that matters');
  assert.equal(c.roleCopy(view('idle', 'coding', { trustId: TRUST })).trustHtml, '');
});

test('the privacy facts are plain, complete and honest', () => {
  const facts = c.MEMBER_PRIVACY_FACTS.join('\n');
  assert.match(facts, /other people's coding requests/);
  assert.match(facts, /anonymous/);
  assert.match(facts, /No name, account or IP address/);
  assert.match(facts, /end-to-end encrypted in transit/);
  assert.match(facts, /keeps no trace/);
  assert.match(facts, /private repository goes only to machines the operator trusts/);
  // the member's machine has to read what it runs; the text never says otherwise
  assert.match(facts, /the machine itself sees them while it works/);
  assert.doesNotMatch(facts, /cannot see|can't see|never see/i);
  const html = c.roleCopy(view('idle')).factsHtml;
  assert.match(html, /<details class="role-details"><summary>What does lending my GPU mean\?<\/summary>/);
  for (const fact of c.MEMBER_PRIVACY_FACTS) {
    assert.ok(html.includes(fact.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')));
  }
});

test('a CPU contributor is told it powers the phone app, and is shown nothing about coding requests', () => {
  const copy = c.roleCopy({ state: 'contributing', cpuPool: true, vramContributedGb: 16, gpu: CPU_NODE });
  assert.match(copy.roleHtml, /Phone app member\. Your computer runs a small model that answers questions in the DLNE phone app\./);
  assert.equal(copy.factsHtml, '');
  assert.equal(copy.trustHtml, '');
  assert.doesNotMatch(copy.roleHtml, /coding|vulkan/i);
});

test('device names from a driver cannot break the card', () => {
  const copy = c.roleCopy(view('contributing', 'coding', { model: '<img src=x onerror=alert(1)>.gguf' }));
  assert.ok(!copy.roleHtml.includes('<img'));
  assert.match(copy.roleHtml, /&lt;img/);
});

test('the status bar tooltip says the job, and a coding member\'s trust id', () => {
  assert.deepEqual(c.contributionTooltip(view('contributing')), ['Local Contribution: CONTRIBUTING (8GB via VULKAN - RTX 4060)']);
  const lines = c.contributionTooltip(view('contributing', 'coding', { trustId: TRUST, slots: 2 }));
  assert.equal(lines.length, 4);
  assert.match(lines[1], /^Role: Coding member\. Your GPU runs a whole coding model by itself/);
  assert.equal(lines[2], c.MEMBER_PRIVACY_SUMMARY);
  assert.equal(lines[3], `Trust id: ${TRUST}. ${c.TRUST_ID_HINT}`);
  assert.equal(c.contributionTooltip(view('idle', 'coding')).length, 1, 'a stopped worker has no job');
});

// ---- the choice and explanation before it starts ------------------------------------------------------------------

test('the first click on a GPU explains what lending means and offers a real choice', async () => {
  const { wm, state, done } = manager();
  try {
    stub.state.warningChoice = c.LEND_GPU;
    assert.equal(await wm.confirmLendingGpu(), true);
    assert.equal(stub.state.warningMessages.length, 1);
    const { message, items } = stub.state.warningMessages[0];
    const [options, ...buttons] = items;
    assert.match(message, /lend RTX 4060 to the swarm\?/);
    assert.equal(options.modal, true);
    assert.deepEqual(buttons, ['Lend my GPU', 'Lend my CPU only', 'Lend both']);
    // both jobs, the facts for the second, what it earns, how to stop
    assert.match(options.detail, /GPU pool: Your GPU holds a share of one big model/);
    assert.match(options.detail, /Coding member: Your GPU runs a whole coding model by itself/);
    for (const fact of c.MEMBER_PRIVACY_FACTS) assert.ok(options.detail.includes(fact), fact);
    assert.match(options.detail, /first in line, with no limit/);
    assert.match(options.detail, /stop at any time/);
    assert.match(options.detail, /dlne\.worker\.device/);
    assert.equal(state.values['dlne.lendGpuNoticeSeen'], true);

    // one click from then on
    assert.equal(await wm.confirmLendingGpu(), true);
    assert.equal(stub.state.warningMessages.length, 1);
  } finally {
    done();
  }
});

test('"Lend my CPU only" keeps the GPU out of it', async () => {
  const { wm, state, done } = manager();
  try {
    stub.state.warningChoice = c.LEND_CPU_ONLY;
    assert.equal(await wm.confirmLendingGpu(), true);
    assert.deepEqual(stub.state.updates, [['dlne.worker.device', 'cpu']]);
    assert.equal(state.values['dlne.lendGpuNoticeSeen'], true);
  } finally {
    done();
  }
});

test('closing the dialog starts nothing and is asked again next time', async () => {
  const { wm, state, done } = manager();
  try {
    stub.state.warningChoice = undefined;
    assert.equal(await wm.startWorker({ interactive: true }), false);
    assert.deepEqual(stub.state.updates, [], 'not even worker.autoStart was set');
    assert.equal(state.values['dlne.lendGpuNoticeSeen'], undefined);
    assert.equal(wm.getStatus().state, 'idle');
  } finally {
    done();
  }
});

test('no dialog for a CPU machine, for an owner who already chose the CPU, or when a worker resumes by itself', async () => {
  const cpu = manager({ gpu: CPU_NODE });
  try {
    assert.equal(await cpu.wm.confirmLendingGpu(), true);
  } finally {
    cpu.done();
  }
  const chose = manager({ settings: { 'dlne.worker.device': 'cpu' } });
  try {
    assert.equal(await chose.wm.confirmLendingGpu(), true);
  } finally {
    chose.done();
  }
  assert.equal(stub.state.warningMessages.length, 0);

  const resumed = manager();
  let asked = 0;
  resumed.wm.confirmLendingGpu = async () => { asked++; return true; };
  resumed.wm.ensureWorkerRuntime = async () => null; // nothing is installed or started by this test
  try {
    await resumed.wm.startWorker(); // autoStart on launch, a version restart: nobody clicked
    assert.equal(asked, 0);
    assert.equal(stub.state.warningMessages.length, 0);
  } finally {
    stub.state.settings['dlne.worker.autoStart'] = false; // startWorker records its own start; a later manager's init must not resume it
    resumed.done();
  }
});

// ---- which job the machine got -----------------------------------------------------------------------------------

test('the worker\'s DLNE_ROLE line becomes the machine\'s job, and goes when the worker does', () => {
  const { wm, done } = manager();
  try {
    wm.currentState = 'contributing';
    assert.equal(wm.getStatus().role, undefined, 'nothing said yet');
    wm.noteWorkerOutput(`Heartbeat OK\nDLNE_ROLE: coding {"model":"m.gguf","slots":2,"trust_id":"${TRUST}"}\n`);
    assert.equal(wm.getStatus().role, 'coding');
    assert.deepEqual(wm.getStatus().roleDetail, { model: 'm.gguf', slots: 2, trustId: TRUST });
    wm.onWorkerExit(0);
    assert.equal(wm.getStatus().role, undefined);
    assert.equal(wm.getStatus().roleDetail, undefined);
    // a stopped worker has no job even if one was said before
    wm.assignedRole = { role: 'rpc' };
    wm.currentState = 'idle';
    assert.equal(wm.getStatus().role, undefined);
  } finally {
    done();
  }
});

test('a CPU machine\'s job is always the phone app', () => {
  const { wm, done } = manager({ gpu: CPU_NODE });
  try {
    wm.currentState = 'contributing';
    assert.equal(wm.getStatus().role, 'app');
  } finally {
    done();
  }
});

test('the status reaches the sidebar with the job, and the card is built from it', () => {
  const { wm, done } = manager();
  try {
    wm.currentState = 'contributing';
    const posted = [];
    let listener;
    const provider = new SidebarProvider({ fsPath: ROOT }, {}, { onStatusChange: (fn) => { listener = fn; }, getStatus: () => wm.getStatus() });
    provider.resolveWebviewView(
      { webview: { options: {}, html: '', asWebviewUri: (u) => u, postMessage: (m) => posted.push(m), onDidReceiveMessage() {} } },
      {},
      {}
    );
    assert.match(posted[0].workerRole.roleHtml, /Role:<\/strong> GPU pool/);
    wm.assignedRole = { role: 'coding', trustId: TRUST, slots: 2 };
    listener(wm.getStatus());
    assert.equal(posted[1].type, 'workerStatus');
    assert.match(posted[1].role.roleHtml, /Role:<\/strong> Coding member/);
    assert.match(posted[1].role.trustHtml, new RegExp(TRUST));
  } finally {
    done();
  }
});

test('the first time a GPU becomes a coding member the owner is told, once, and can stop', async () => {
  const state = globalState();
  const { wm, done } = manager({ state });
  let stopped = 0;
  wm.stopWorker = async () => { stopped++; };
  try {
    wm.currentState = 'contributing';
    stub.state.infoChoice = 'Stop contributing';
    wm.noteWorkerOutput('DLNE_ROLE: coding {"slots":2}');
    await new Promise((r) => setImmediate(r));
    assert.deepEqual(stub.state.infoMessages, [c.CODING_MEMBER_NOTICE]);
    assert.match(c.CODING_MEMBER_NOTICE, /anonymous and end-to-end encrypted in transit, and your machine keeps no trace/);
    assert.equal(stopped, 1);
    assert.equal(state.values['dlne.codingMemberNoticeSeen'], true);

    wm.noteWorkerOutput(`DLNE_ROLE: coding {"slots":2,"trust_id":"${TRUST}"}`); // the same job, with more detail
    wm.noteWorkerOutput('DLNE_ROLE: rpc');
    wm.noteWorkerOutput('DLNE_ROLE: coding');
    await new Promise((r) => setImmediate(r));
    assert.equal(stub.state.infoMessages.length, 1, `once per install: ${JSON.stringify(stub.state.infoMessages)}`);
  } finally {
    done();
  }
  const again = manager({ state });
  try {
    again.wm.currentState = 'contributing';
    again.wm.noteWorkerOutput('DLNE_ROLE: coding');
    await new Promise((r) => setImmediate(r));
    assert.equal(stub.state.infoMessages.length, 0, 'a new window does not ask again');
  } finally {
    again.done();
  }
});

// ---- the sidebar and status bar ------------------------------------------------------------------------------------

function sidebarFor(status) {
  stub.reset();
  return new SidebarProvider({ fsPath: ROOT }, {}, { onStatusChange() {}, getStatus: () => status });
}

test('the sidebar card explains the jobs and the privacy facts to a GPU owner before they start', () => {
  const html = sidebarFor({ state: 'idle', gpu: RTX, cpuPool: false, vramContributedGb: 0 })._getHtmlForWebview();
  assert.match(html, /id="role-display">[^]*Your GPU's job:/);
  assert.match(html, /<details class="role-details"><summary>What does lending my GPU mean\?/);
  assert.match(html, /Your machine keeps no trace/);
  assert.match(html, /id="trust-display" style="display:none"/);
  assert.match(html, /⚡ Contribute Local GPU/);
});

test('the sidebar card for a coding member shows the job, the privacy line and the trust id', () => {
  const html = sidebarFor({ state: 'contributing', gpu: RTX, cpuPool: false, vramContributedGb: 8, role: 'coding', roleDetail: { slots: 2, trustId: TRUST } })._getHtmlForWebview();
  assert.match(html, /<strong>Role:<\/strong> Coding member\./);
  assert.match(html, /Requests are anonymous and end-to-end encrypted in transit, and your machine keeps no trace\./);
  assert.match(html, new RegExp(`<code>${TRUST}</code>`));
  assert.match(html, /Give this to the operator if this is your own machine and you want private repos routed to it\./);
  assert.doesNotMatch(html, /id="trust-display" style="display:none"/);
});

test('the sidebar card for a CPU contributor carries no GPU or coding-request text', () => {
  const html = sidebarFor({ state: 'idle', gpu: CPU_NODE, cpuPool: true, vramContributedGb: 0 })._getHtmlForWebview();
  assert.match(html, /Phone app member/);
  assert.doesNotMatch(html, /<details/);
  assert.doesNotMatch(html, /Coding member|end-to-end encrypted|lending my GPU/);
});

test('Copy puts only a trust id on the clipboard', async () => {
  stub.reset();
  const provider = new SidebarProvider({ fsPath: ROOT }, {}, { onStatusChange() {}, getStatus: () => ({ state: 'idle', gpu: RTX, cpuPool: false, vramContributedGb: 0 }) });
  let onMessage;
  provider.resolveWebviewView(
    { webview: { options: {}, html: '', asWebviewUri: (u) => u, postMessage() {}, onDidReceiveMessage: (fn) => { onMessage = fn; } } },
    {},
    {}
  );
  await onMessage({ type: 'copyTrustId', id: 'rm -rf /' });
  assert.equal(stub.state.clipboard, undefined);
  await onMessage({ type: 'copyTrustId', id: TRUST });
  assert.equal(stub.state.clipboard, TRUST);
});

test('the status bar says "Coding member" for a coding member', async () => {
  stub.reset();
  const client = {
    getQuotaRemaining: () => null,
    getServerUrl: () => 'http://coordinator',
    getClusterStatusDetailed: async () => ({
      status: 'ok',
      data: { status: 'Active', global_active_model: 'qwen', total_vram_gb: 24, total_memory_gb: 44, total_sysram_gb: 20, active_workers_count: 2, active_leases_count: 0, is_locked_for_edits: false, server_version: '2.4.0', model_loading: { is_loading: false } },
    }),
  };
  const worker = (role) => ({ onStatusChange() {}, getStatus: () => ({ state: 'contributing', gpu: RTX, cpuPool: false, vramContributedGb: 8, role, roleDetail: { trustId: TRUST } }) });
  for (const [role, tag] of [['coding', /\[Coding member 8GB\]/], ['rpc', /\[Contributing 8GB\]/], [undefined, /\[Contributing 8GB\]/]]) {
    stub.reset();
    const bar = new ClusterStatusBar(client, worker(role));
    await bar.updateStatus();
    const item = stub.state.statusBarItems[0];
    assert.match(item.text, tag);
    if (role === 'coding') assert.match(item.tooltip, new RegExp(`Trust id: ${TRUST}`));
    bar.dispose();
  }
});

test('the wording the extension reads is the wording the contract documents', () => {
  const doc = fs.readFileSync(path.join(ROOT, '..', 'docs', 'coding-members.md'), 'utf8');
  assert.ok(doc.includes('DLNE_ROLE: '), 'section 10 documents the DLNE_ROLE line');
  assert.ok(doc.includes('dlne_queue'), 'section 10 documents the busy answer');
  assert.equal(c.ROLE_MARKER, 'DLNE_ROLE: ');
});
