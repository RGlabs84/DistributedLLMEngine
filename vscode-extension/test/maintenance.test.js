'use strict';
/** Maintenance mode (coordinator 2.8.5): the notice, the panel's card and the status bar item. */
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const stub = require('./helpers/vscode-stub');
const m = require('../dist/client/maintenance');
const { SidebarProvider } = require('../dist/views/sidebarProvider');
const { ClusterStatusBar } = require('../dist/statusbar/clusterStatusBar');

const ROOT = path.join(__dirname, '..');
const noop = { onStatusChange() {}, getStatus: () => ({ state: 'idle', gpu: { hasGpu: false, name: 'CPU Compute Node', vramTotalGb: 4, api: 'cpu' }, cpuPool: false, vramContributedGb: 0 }) };

test('a status that says maintenance is on gives the notice, and one that does not gives none', () => {
  const n = m.maintenanceFromStatus({ maintenance: { active: true, message: ' Upgrading. ', until: '14:00 UTC' } });
  assert.deepEqual(n, { message: 'Upgrading.', until: '14:00 UTC' });
  assert.equal(m.maintenanceFromStatus({}), null);
  assert.equal(m.maintenanceFromStatus({ maintenance: { active: false } }), null);
  assert.equal(m.maintenanceFromStatus(null), null);
});

test('a blank message gets the web chat\'s line', () => {
  assert.equal(m.maintenanceFromStatus({ maintenance: { active: true } }).message, 'DLNE is down for a short upgrade. Back soon.');
});

test('a 429 maintenance body gives the notice; other bodies do not', () => {
  const body = JSON.stringify({ detail: 'x', code: 'maintenance', maintenance: { message: 'Back soon', until: '15:30 UTC' } });
  assert.deepEqual(m.maintenanceFromBody(body), { message: 'Back soon', until: '15:30 UTC' });
  assert.equal(m.maintenanceFromBody(JSON.stringify({ detail: 'Slow down', code: 'rate_limited' })), null);
  assert.equal(m.maintenanceFromBody('not json'), null);
  assert.equal(m.maintenanceUntilLine({ message: '', until: '15:30 UTC' }), 'Expected back: 15:30 UTC');
  assert.equal(m.maintenanceUntilLine({ message: '', until: '' }), '');
});

test('the panel carries the maintenance card with the web chat\'s wording, hidden until told', () => {
  stub.reset();
  const html = new SidebarProvider({ fsPath: ROOT }, {}, noop)._getHtmlForWebview();
  assert.match(html, /id="maint-card"/);
  assert.match(html, />Maintenance<\/p>/);
  assert.match(html, /DLNE is being upgraded/);
  assert.match(html, /Expected back: /);
  assert.match(html, /This page checks every 15 seconds and opens again by itself\./);
  assert.match(html, /case 'maintenance':/);
});

test('the status bar says "DLNE: maintenance" while it is on and goes back after', async () => {
  stub.reset();
  let notice = { message: 'Upgrading.', until: '14:00 UTC' };
  const client = {
    getClusterStatusDetailed: async () => ({ status: 'ok', data: { status: 'ok', global_active_model: 'q', total_vram_gb: 1, active_leases_count: 0, is_locked_for_edits: false } }),
    currentMaintenance: () => notice,
    getQuotaRemaining: () => null,
    getServerUrl: () => 'https://dlne.example.org',
  };
  const bar = new ClusterStatusBar(client, noop);
  await bar.updateStatus();
  const item = stub.state.statusBarItems[0];
  assert.equal(item.text, 'DLNE: maintenance');
  assert.match(String(item.tooltip), /Expected back: 14:00 UTC/);
  notice = null;
  await bar.updateStatus();
  assert.notEqual(item.text, 'DLNE: maintenance');
  bar.dispose();
});

test('the panel is told about maintenance only when it starts or ends', () => {
  stub.reset();
  let listener;
  const client = {
    lastKnownClusterStatus: () => null,
    currentMaintenance: () => null,
    onMaintenance: (fn) => { listener = fn; return () => {}; },
  };
  const provider = new SidebarProvider({ fsPath: ROOT }, {}, noop);
  provider.client = client;
  const posted = [];
  provider.resolveWebviewView(
    { webview: { options: {}, html: '', asWebviewUri: (u) => u, postMessage: (x) => posted.push(x), onDidReceiveMessage() {} }, onDidDispose() {} },
    {},
    {}
  );
  assert.ok(!posted.some((p) => p.type === 'maintenance'), 'nothing while it is off');
  listener({ message: 'Upgrading.', until: '' });
  assert.deepEqual(posted.at(-1), { type: 'maintenance', notice: { message: 'Upgrading.', until: '' } });
  listener(null);
  assert.deepEqual(posted.at(-1), { type: 'maintenance', notice: null });
  provider._stopMaintenanceCheck();
});
