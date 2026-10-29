import * as crypto from 'crypto';
import * as vscode from 'vscode';
import { ClusterStatusResult, DlneClient } from '../client/dlneClient';
import { ROLE_LABEL } from '../worker/contribution';
import { WorkerManager, WorkerStatus } from '../worker/workerManager';

/**
 * The DLNE Swarm dashboard: an editor tab with the swarm's live state, for the editor area that otherwise sits
 * empty. Everything on it comes from the coordinator's public /v1/cluster/status (the same call the status bar
 * makes) and this machine's own worker; the two charts are built from samples taken while the tab is open.
 */

/** How often the open, visible dashboard asks the coordinator. */
const POLL_MS = 5000;
/** The charts keep 30 minutes of 5-second samples. */
const MAX_SAMPLES = 360;

export interface Sample {
  t: number;
  tokensPerSecond: number;
  inUse: number;
  waiting: number;
}

type Num = number | null;

function num(value: unknown): Num {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function str(value: unknown): string | null {
  return typeof value === 'string' && value ? value : null;
}

/** What the page shows, from the coordinator's status (`s`, null when it could not be read) and this machine. */
export function dashboardModel(s: any, worker: WorkerStatus | null, error: string | null = null) {
  const gpu = s?.gpu_pool ?? {};
  const cpu = s?.cpu_pool ?? {};
  const queue = s?.coding_queue ?? {};
  const members = s?.coding_members ?? {};
  const app = s?.app_pool ?? {};
  const swap = s?.auto_swap ?? {};
  const loading = s?.model_loading ?? {};
  const reach = gpu.reach ?? {};
  const tiers: any[] = Array.isArray(s?.coding_tiers) ? s.coding_tiers : [];
  const tokensLastHour = (num(gpu.tokens_last_hour) ?? 0) + (num(cpu.tokens_last_hour) ?? 0);
  return {
    online: !!s && !error,
    error,
    serverVersion: str(s?.server_version),
    model: {
      name: str(s?.coding_model?.name) ?? str(s?.global_active_model),
      tokensPerSecond: num(s?.coding_model?.tokens_per_second),
      contextPerRequest: num(s?.policy?.coding_context_tokens),
      loadingPct: loading.is_loading === true ? num(loading.progress_pct) ?? 0 : null,
    },
    swap: { state: str(swap.state) ?? 'unknown', detail: str(swap.detail) ?? '' },
    queue: { slots: num(queue.slots), inUse: num(queue.in_use), waiting: num(queue.waiting) },
    gpuPool: {
      gpus: num(gpu.gpus),
      vramGb: num(gpu.vram_gb),
      standingBy: num(gpu.gpus_standing_by),
      reach: {
        host: num(reach.host) ?? 0, best: num(reach.best) ?? 0, good: num(reach.good) ?? 0,
        usable: num(reach.usable) ?? 0, tooFar: num(reach.too_far) ?? 0, unmeasured: num(reach.unmeasured) ?? 0,
      },
    },
    cpuPool: { machines: num(cpu.machines), cores: num(cpu.cores), ramGb: num(cpu.ram_gb) },
    members: {
      members: num(members.members), ready: num(members.ready), busy: num(members.busy),
      decodeMedian: num(members.decode_tps_median),
    },
    app: {
      ready: num(app.members_ready), model: str(app.active_model), inFlight: num(app.requests_in_flight),
      medianAnswerSeconds: num(app.median_answer_seconds),
    },
    tokensLastHour: s ? tokensLastHour : null,
    tiers: tiers.map((t) => ({
      name: str(t?.name) ?? str(t?.id) ?? '?',
      minVramGb: num(t?.min_vram_gb),
      state: t?.active === true ? 'serving' : t?.enabled !== true ? 'off' : t?.unlocked === true ? 'unlocked' : 'needs more VRAM',
    })),
    machine: worker ? {
      state: worker.state,
      message: worker.message ?? '',
      role: worker.role ? ROLE_LABEL[worker.role] : null,
      lentGb: worker.vramContributedGb || null,
      gpu: worker.gpu?.name ?? null,
    } : null,
    sample: s ? {
      tokensPerSecond: (num(gpu.tokens_per_second) ?? 0) + (num(cpu.tokens_per_second) ?? 0),
      inUse: num(queue.in_use) ?? 0,
      waiting: num(queue.waiting) ?? 0,
    } : null,
  };
}

export type DashboardModel = ReturnType<typeof dashboardModel>;

function unreadable(result: ClusterStatusResult): string {
  switch (result.status) {
    case 'forbidden': return 'The coordinator refused the status request (contribute compute to see the swarm).';
    case 'unreachable': return 'The coordinator cannot be reached.';
    default: return result.message || 'The coordinator answered with an error.';
  }
}

export class SwarmDashboard {
  private static current: SwarmDashboard | undefined;
  private readonly samples: Sample[] = [];
  private readonly timer: NodeJS.Timeout;

  static show(context: vscode.ExtensionContext, client: DlneClient, workerManager: WorkerManager): void {
    if (SwarmDashboard.current) {
      SwarmDashboard.current.panel.reveal();
      return;
    }
    const panel = vscode.window.createWebviewPanel('dlne.swarmDashboard', 'DLNE Swarm', vscode.ViewColumn.Active,
      { enableScripts: true, retainContextWhenHidden: true, localResourceRoots: [] });
    panel.iconPath = vscode.Uri.joinPath(context.extensionUri, 'media', 'dlne_logo.png');
    SwarmDashboard.current = new SwarmDashboard(panel, client, workerManager);
  }

  private constructor(
    private readonly panel: vscode.WebviewPanel,
    private readonly client: DlneClient,
    private readonly workerManager: WorkerManager
  ) {
    panel.webview.html = dashboardHtml(crypto.randomBytes(16).toString('base64'));
    this.timer = setInterval(() => {
      if (this.panel.visible) void this.refresh();
    }, POLL_MS);
    panel.onDidDispose(() => {
      clearInterval(this.timer);
      SwarmDashboard.current = undefined;
    });
    void this.refresh();
  }

  private async refresh(): Promise<void> {
    const result = await this.client.getClusterStatusDetailed();
    const ok = result.status === 'ok' && result.data;
    const model = dashboardModel(ok ? result.data : null, this.workerManager.getStatus(), ok ? null : unreadable(result));
    if (model.sample) {
      this.samples.push({ t: Date.now(), ...model.sample });
      if (this.samples.length > MAX_SAMPLES) this.samples.shift();
    }
    void this.panel.webview.postMessage({ type: 'model', model, samples: this.samples });
  }
}

/**
 * The page. Nothing from the coordinator is ever parsed as markup: the script builds every element and sets text
 * with textContent. The look is dlne.rossgilsonlabs.dev's (dlne.css), always dark; the chart lines are its cyan and
 * purple one step deeper, validated with the data-viz checks on its card colour.
 */
export function dashboardHtml(nonce: string): string {
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'nonce-${nonce}'; script-src 'nonce-${nonce}';">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DLNE Swarm</title>
<style nonce="${nonce}">
  /* The look of dlne.rossgilsonlabs.dev (its dlne.css): always dark, as the site is. */
  :root {
    color-scheme: dark;
    --surface: #07090e;
    --card: rgba(11, 15, 25, 0.75);
    --card-solid: #0a0d16;
    --card-border: rgba(255, 255, 255, 0.12);
    --cyan: #00f0ff;
    --purple: #c084fc;
    --text-primary: #e2e8f0;
    --text-bright: #ffffff;
    --text-secondary: #94a3b8;
    --text-muted: #64748b;
    --hairline: rgba(255, 255, 255, 0.09);
    --grid: rgba(255, 255, 255, 0.07);
    /* Chart lines: the brand cyan and purple one step deeper. The neon steps are too light to pass the data-viz
       lightness band on the card; these pass every check there (dark band, CVD 8.3, contrast >= 3:1). */
    --series-1: #00a5b4;
    --series-2: #ad71e7;
    /* The site's status colours (--ok, --warn, --bad), always beside an icon and a label. */
    --good: #7fcf72; --warning: #e8c25a; --serious: #ee8f4a; --critical: #e0705c;
    --sans: 'Alegreya Sans', 'Segoe UI', system-ui, sans-serif;
    --mono: 'JetBrains Mono', 'Fira Code', ui-monospace, monospace;
  }
  body { background: radial-gradient(ellipse at top, #0b1a24 0%, var(--surface) 60%) fixed; color: var(--text-primary);
         font-family: var(--sans); font-size: 14px; margin: 0; padding: 22px 26px 34px; min-height: 100vh; box-sizing: border-box; }
  header { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; margin-bottom: 18px; }
  h1 { font-family: var(--mono); font-size: 20px; font-weight: 700; letter-spacing: 0.12em; margin: 0; }
  h1 .brand-a { color: var(--cyan); text-shadow: 0 0 15px rgba(0, 240, 255, 0.4); }
  h1 .brand-b { color: var(--purple); text-shadow: 0 0 15px rgba(192, 132, 252, 0.35); }
  h2 { font-family: var(--mono); font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.12em;
       color: var(--text-secondary); margin: 0 0 12px; }
  .muted { color: var(--text-secondary); font-family: var(--mono); font-size: 11.5px; letter-spacing: 0.04em; }
  .pill { display: inline-flex; align-items: center; gap: 6px; padding: 3px 12px; border-radius: 9999px;
          border: 1px solid var(--card-border); font-family: var(--mono); font-size: 11.5px; color: var(--text-primary); }
  .pill.beacon { background: rgba(0, 240, 255, 0.08); border-color: rgba(0, 240, 255, 0.3); color: var(--cyan);
                 font-weight: 600; letter-spacing: 0.04em; }
  .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; flex: none; }
  .grid { display: grid; gap: 14px; margin-bottom: 14px; }
  .kpis { grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); }
  .two { grid-template-columns: repeat(auto-fit, minmax(330px, 1fr)); }
  .three { grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); }
  .card { background: var(--card); border: 1px solid var(--card-border); border-radius: 14px; padding: 18px 20px; min-width: 0;
          box-shadow: 0 8px 30px rgba(0, 0, 0, 0.4); transition: border-color 0.3s, transform 0.3s, box-shadow 0.3s; }
  .tile { display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; min-height: 128px; }
  .tile:hover { border-color: var(--cyan); transform: translateY(-2px); box-shadow: 0 8px 24px rgba(0, 240, 255, 0.2); }
  .tile-label { font-family: var(--mono); font-size: 11px; color: var(--text-secondary); text-transform: uppercase;
                letter-spacing: 0.12em; margin-bottom: 8px; }
  .tile-value { font-size: 24px; font-weight: 800; color: var(--text-bright); line-height: 1.2; margin-bottom: 6px; }
  .tile-value.cyan { color: var(--cyan); text-shadow: 0 0 15px rgba(0, 240, 255, 0.4); }
  .tile-value.purple { color: var(--purple); text-shadow: 0 0 15px rgba(192, 132, 252, 0.4); }
  .tile-sub { color: var(--text-muted); font-size: 12.5px; }
  dl { display: grid; grid-template-columns: auto 1fr; gap: 6px 12px; margin: 0; }
  dt { color: var(--text-secondary); }
  dd { margin: 0; text-align: right; color: var(--text-bright); font-variant-numeric: tabular-nums; }
  table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
  th, td { text-align: left; padding: 7px 8px; border-bottom: 1px solid var(--hairline); }
  th { font-family: var(--mono); font-size: 11px; text-transform: uppercase; letter-spacing: 0.1em; color: var(--text-secondary); font-weight: 600; }
  .meter { height: 6px; border-radius: 3px; background: rgba(0, 240, 255, 0.15); margin-top: 8px; }
  .meter > div { height: 100%; border-radius: 3px; background: var(--series-1); }
  .chart { position: relative; }
  .chart svg { display: block; width: 100%; height: 150px; overflow: visible; }
  .legend { display: flex; gap: 14px; color: var(--text-secondary); font-size: 12.5px; margin: -4px 0 6px; }
  .key { display: inline-block; width: 14px; height: 2px; vertical-align: middle; margin-right: 6px; border-radius: 1px; }
  .tip { position: absolute; pointer-events: none; background: #0f172a; border: 1px solid rgba(0, 240, 255, 0.28);
         border-radius: 8px; padding: 6px 9px; font-size: 12.5px; display: none; white-space: nowrap; z-index: 2; color: var(--text-primary); }
  .tip strong { font-weight: 700; margin-right: 6px; color: var(--text-bright); }
  details { margin-top: 8px; color: var(--text-secondary); font-size: 12.5px; }
  summary { cursor: pointer; }
  details table { margin-top: 6px; }
  .empty { color: var(--text-muted); font-size: 12.5px; padding: 50px 0; text-align: center; }
  .detail { color: var(--text-secondary); margin-top: 10px; line-height: 1.45; }
</style>
</head>
<body>
<header>
  <h1><span class="brand-a">DLNE</span> <span class="brand-b">SWARM</span></h1>
  <span class="pill" id="online"><span class="dot"></span><span></span></span>
  <span class="muted" id="meta"></span>
</header>
<p class="detail" id="error" hidden></p>
<section class="grid kpis" id="kpis"></section>
<section class="grid two">
  <div class="card"><h2>Live throughput (tokens per second)</h2><div class="chart" id="chart-tps"></div></div>
  <div class="card"><h2>Coding requests</h2><div class="chart" id="chart-queue"></div></div>
</section>
<section class="grid two">
  <div class="card" id="model-card"></div>
  <div class="card" id="machine-card"></div>
</section>
<section class="grid three" id="pools"></section>
<section class="card" id="ladder"></section>
<script nonce="${nonce}">
(function () {
  const SVGNS = 'http://www.w3.org/2000/svg';
  function el(tag, attrs, text) {
    const node = document.createElement(tag);
    if (attrs) for (const k in attrs) node.setAttribute(k, attrs[k]);
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function svg(tag, attrs) {
    const node = document.createElementNS(SVGNS, tag);
    for (const k in attrs) node.setAttribute(k, attrs[k]);
    return node;
  }
  function compact(n) {
    if (n === null || n === undefined) return '\\u2013';
    if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(1) + 'M';
    if (Math.abs(n) >= 1e3) return (n / 1e3).toFixed(1) + 'K';
    return Number.isInteger(n) ? String(n) : n.toFixed(1);
  }
  function orDash(v, unit) { return v === null || v === undefined ? '\\u2013' : v + (unit || ''); }
  function clock(t) { return new Date(t).toLocaleTimeString(); }

  // Status: a fixed colour, always with an icon and a label (never colour alone).
  const STATUS = {
    good: ['var(--good)', '\\u2713'], warning: ['var(--warning)', '\\u25F7'],
    serious: ['var(--serious)', '\\u21BB'], critical: ['var(--critical)', '\\u2715'],
  };
  function swapStatus(state) {
    if (state === 'steady') return ['good', 'Steady'];
    if (state === 'swapping') return ['serious', 'Switching model'];
    // "held" (server 2.6.2): the engine was rebuilt too often, so the server holds the rebuilds nobody needs; the detail
    // under the chip says how many, why and until when. A warning, not an error: the swarm is serving.
    if (state === 'waiting' || state === 'downloading' || state === 'cooldown' || state === 'held') return ['warning', state.charAt(0).toUpperCase() + state.slice(1)];
    if (state === 'off' || state === 'unknown') return ['warning', 'Off'];
    return ['critical', state];
  }
  function statusChip(kind, label) {
    const chip = el('span', { class: 'pill' });
    const dot = el('span', { class: 'dot' });
    dot.style.background = STATUS[kind][0];
    chip.append(dot, el('span', null, STATUS[kind][1] + ' ' + label));
    return chip;
  }

  function tile(label, value, sub, tone) {
    const card = el('div', { class: 'card tile' });
    card.append(el('div', { class: 'tile-label' }, label), el('div', { class: 'tile-value' + (tone ? ' ' + tone : '') }, value));
    if (sub) card.append(el('div', { class: 'tile-sub' }, sub));
    return card;
  }
  function list(title, rows, after) {
    const card = el('div', { class: 'card' });
    card.append(el('h2', null, title));
    const dl = el('dl');
    rows.forEach(function (r) { dl.append(el('dt', null, r[0]), el('dd', null, r[1])); });
    card.append(dl);
    if (after) card.append(after);
    return card;
  }

  // A line/area chart over the session's samples: 2px lines, 10% area wash, hairline grid, an end dot with a
  // surface ring and an end label, and a crosshair that snaps to the nearest sample with one tooltip for every series.
  function chart(host, samples, series, opts) {
    host.textContent = '';
    if (samples.length < 2) {
      host.append(el('div', { class: 'empty' }, 'Collecting: one sample every 5 seconds while this tab is open.'));
      return;
    }
    // Two series: the legend carries identity and the current values (end labels would collide where the lines meet).
    const latest = samples[samples.length - 1];
    if (series.length > 1) {
      const legend = el('div', { class: 'legend' });
      series.forEach(function (s) {
        const item = el('span');
        const key = el('span', { class: 'key' });
        key.style.background = s.color;
        item.append(key, document.createTextNode(s.label + ' ' + compact(latest[s.key])));
        legend.append(item);
      });
      host.append(legend);
    }
    const W = Math.max(host.clientWidth, 260), H = 150, L = 34, R = 56, T = 8, B = 20;
    const t0 = samples[0].t, t1 = samples[samples.length - 1].t;
    let max = opts.minMax || 1;
    samples.forEach(function (p) { series.forEach(function (s) { max = Math.max(max, p[s.key]); }); });
    const step = max <= 5 ? 1 : Math.pow(10, Math.floor(Math.log10(max))) * (max / Math.pow(10, Math.floor(Math.log10(max))) > 5 ? 2 : 1);
    max = Math.ceil(max / step) * step;
    const x = function (t) { return L + (t1 === t0 ? 0 : (t - t0) / (t1 - t0)) * (W - L - R); };
    const y = function (v) { return T + (1 - v / max) * (H - T - B); };
    const root = svg('svg', { viewBox: '0 0 ' + W + ' ' + H, role: 'img', 'aria-label': opts.title });
    for (let v = 0; v <= max + 1e-9; v += step) {
      root.append(svg('line', { x1: L, x2: W - R, y1: y(v), y2: y(v), stroke: 'var(--grid)', 'stroke-width': 1 }));
      const tick = svg('text', { x: L - 6, y: y(v) + 4, 'text-anchor': 'end', 'font-size': 11, fill: 'var(--text-muted)' });
      tick.textContent = compact(v);
      root.append(tick);
    }
    [t0, t1].forEach(function (t, i) {
      const label = svg('text', { x: x(t), y: H - 4, 'text-anchor': i ? 'end' : 'start', 'font-size': 11, fill: 'var(--text-muted)' });
      label.textContent = clock(t);
      root.append(label);
    });
    series.forEach(function (s) {
      const points = samples.map(function (p) { return x(p.t) + ',' + y(p[s.key]); });
      if (opts.area) {
        root.append(svg('polygon', { points: x(t0) + ',' + y(0) + ' ' + points.join(' ') + ' ' + x(t1) + ',' + y(0),
                                      fill: s.color, 'fill-opacity': 0.1 }));
      }
      root.append(svg('polyline', { points: points.join(' '), fill: 'none', stroke: s.color, 'stroke-width': 2,
                                     'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));
      const last = samples[samples.length - 1];
      root.append(svg('circle', { cx: x(last.t), cy: y(last[s.key]), r: 4, fill: s.color, stroke: 'var(--card-solid)', 'stroke-width': 2 }));
      if (series.length === 1) {
        const end = svg('text', { x: x(last.t) + 8, y: y(last[s.key]) + 4, 'font-size': 11, fill: 'var(--text-primary)' });
        end.textContent = compact(last[s.key]);
        root.append(end);
      }
    });
    const cross = svg('line', { y1: T, y2: H - B, stroke: 'var(--text-muted)', 'stroke-width': 1, visibility: 'hidden' });
    root.append(cross);
    const tip = el('div', { class: 'tip' });
    root.addEventListener('pointermove', function (e) {
      const box = root.getBoundingClientRect();
      const px = (e.clientX - box.left) * (W / box.width);
      let best = samples[0];
      samples.forEach(function (p) { if (Math.abs(x(p.t) - px) < Math.abs(x(best.t) - px)) best = p; });
      cross.setAttribute('x1', x(best.t)); cross.setAttribute('x2', x(best.t));
      cross.setAttribute('visibility', 'visible');
      tip.textContent = '';
      tip.append(el('div', { class: 'muted' }, clock(best.t)));
      series.forEach(function (s) {
        const row = el('div');
        const key = el('span', { class: 'key' });
        key.style.background = s.color;
        row.append(key, el('strong', null, compact(best[s.key])), document.createTextNode(s.label));
        tip.append(row);
      });
      tip.style.display = 'block';
      const left = (x(best.t) / W) * box.width;
      tip.style.left = Math.min(left + 12, box.width - 150) + 'px';
      tip.style.top = '24px';
    });
    root.addEventListener('pointerleave', function () { cross.setAttribute('visibility', 'hidden'); tip.style.display = 'none'; });
    host.append(root, tip);

    // The same numbers without hovering.
    const details = el('details');
    details.append(el('summary', null, 'Show data'));
    const table = el('table');
    const head = el('tr');
    head.append(el('th', null, 'Time'));
    series.forEach(function (s) { head.append(el('th', null, s.label)); });
    table.append(head);
    samples.slice(-12).reverse().forEach(function (p) {
      const tr = el('tr');
      tr.append(el('td', null, clock(p.t)));
      series.forEach(function (s) { tr.append(el('td', null, compact(p[s.key]))); });
      table.append(tr);
    });
    details.append(table);
    host.append(details);
  }

  let last = null;
  function render(m, samples) {
    const online = document.getElementById('online');
    online.className = m.online ? 'pill beacon' : 'pill';
    online.querySelector('.dot').style.background = m.online ? 'var(--good)' : 'var(--critical)';
    online.lastChild.textContent = m.online ? '\\u2713 Online' : '\\u2715 Unreachable';
    document.getElementById('meta').textContent =
      (m.serverVersion ? 'coordinator ' + m.serverVersion + ' \\u00B7 ' : '') + 'updated ' + new Date().toLocaleTimeString();
    const error = document.getElementById('error');
    error.hidden = !m.error;
    error.textContent = m.error || '';

    const kpis = document.getElementById('kpis');
    kpis.textContent = '';
    kpis.append(
      tile('Writing speed', orDash(m.model.tokensPerSecond, ' tok/s'), 'measured on ' + (m.model.name || 'the engine'), 'cyan'),
      tile('Coding slots in use', m.queue.slots === null ? '\\u2013' : orDash(m.queue.inUse) + ' of ' + m.queue.slots,
           orDash(m.queue.waiting) + ' waiting'),
      tile('GPU pool', orDash(m.gpuPool.gpus, ' GPUs'), orDash(m.gpuPool.vramGb, ' GB of GPU memory'), 'purple'),
      tile('Tokens in the last hour', compact(m.tokensLastHour), 'GPU and CPU pools together'));

    chart(document.getElementById('chart-tps'), samples,
          [{ key: 'tokensPerSecond', label: 'tokens per second', color: 'var(--series-1)' }],
          { area: true, title: 'Live throughput', minMax: 10 });
    chart(document.getElementById('chart-queue'), samples,
          [{ key: 'inUse', label: 'in progress', color: 'var(--series-1)' },
           { key: 'waiting', label: 'waiting', color: 'var(--series-2)' }],
          { title: 'Coding requests', minMax: Math.max(m.queue.slots || 1, 1) });

    const modelCard = document.getElementById('model-card');
    modelCard.textContent = '';
    modelCard.append(el('h2', null, 'Model'));
    const s = swapStatus(m.swap.state);
    const modelRows = el('dl');
    modelRows.append(el('dt', null, 'Serving'), el('dd', null, m.model.name || '\\u2013'),
                     el('dt', null, 'Context per request'), el('dd', null, m.model.contextPerRequest ? compact(m.model.contextPerRequest) + ' tokens' : '\\u2013'),
                     el('dt', null, 'Automatic switching'), el('dd', null));
    modelRows.lastChild.append(statusChip(s[0], s[1]));
    modelCard.append(modelRows);
    if (m.model.loadingPct !== null) {
      modelCard.append(el('div', { class: 'detail' }, 'Loading the model: ' + Math.round(m.model.loadingPct) + '%'));
      const meter = el('div', { class: 'meter' });
      const fill = el('div');
      fill.style.width = Math.max(0, Math.min(100, m.model.loadingPct)) + '%';
      meter.append(fill);
      modelCard.append(meter);
    }
    if (m.swap.detail) modelCard.append(el('div', { class: 'detail' }, m.swap.detail));

    const machine = document.getElementById('machine-card');
    machine.textContent = '';
    machine.append(el('h2', null, 'This machine'));
    if (m.machine) {
      const rows = el('dl');
      rows.append(el('dt', null, 'Worker'), el('dd', null, m.machine.state === 'contributing' ? 'Contributing' : m.machine.state),
                  el('dt', null, 'GPU'), el('dd', null, m.machine.gpu || '\\u2013'),
                  el('dt', null, 'Lending'), el('dd', null, m.machine.lentGb ? m.machine.lentGb + ' GB' : '\\u2013'),
                  el('dt', null, 'Job'), el('dd', null, m.machine.role || '\\u2013'));
      machine.append(rows);
      if (m.machine.message) machine.append(el('div', { class: 'detail' }, m.machine.message));
    }

    const r = m.gpuPool.reach;
    const pools = document.getElementById('pools');
    pools.textContent = '';
    pools.append(
      list('GPU pool', [
        ['GPUs', orDash(m.gpuPool.gpus)], ['Memory', orDash(m.gpuPool.vramGb, ' GB')],
        ['Standing by', orDash(m.gpuPool.standingBy)],
        ['On the host', r.host], ['Near enough to share the model', r.best + r.good],
        ['Usable / too far / unmeasured', r.usable + ' / ' + r.tooFar + ' / ' + r.unmeasured]]),
      list('Coding members', [
        ['Members', orDash(m.members.members)], ['Ready', orDash(m.members.ready)], ['Busy', orDash(m.members.busy)],
        ['Median writing speed', orDash(m.members.decodeMedian, ' tok/s')]]),
      list('Phone app and CPU pool', [
        ['Phone app model', m.app.model || '\\u2013'], ['Machines ready', orDash(m.app.ready)],
        ['Answers in progress', orDash(m.app.inFlight)],
        ['Median answer', m.app.medianAnswerSeconds === null ? '\\u2013' : m.app.medianAnswerSeconds.toFixed(1) + ' s'],
        ['CPU machines', orDash(m.cpuPool.machines)], ['CPU cores / memory', orDash(m.cpuPool.cores) + ' / ' + orDash(m.cpuPool.ramGb, ' GB')]]));

    const ladder = document.getElementById('ladder');
    ladder.textContent = '';
    ladder.append(el('h2', null, 'Model ladder'));
    const table = el('table');
    const head = el('tr');
    ['Model', 'Needs', 'State'].forEach(function (h) { head.append(el('th', null, h)); });
    table.append(head);
    m.tiers.forEach(function (t) {
      const tr = el('tr');
      tr.append(el('td', null, t.name), el('td', null, t.minVramGb === null ? '\\u2013' : t.minVramGb + ' GB'));
      const state = el('td');
      if (t.state === 'serving') state.append(statusChip('good', 'Serving'));
      else state.textContent = t.state === 'off' ? 'Off' : t.state === 'unlocked' ? 'Available' : 'Needs more GPU memory';
      tr.append(state);
      table.append(tr);
    });
    ladder.append(table);
  }

  window.addEventListener('message', function (event) {
    if (event.data && event.data.type === 'model') {
      last = event.data;
      render(last.model, last.samples);
    }
  });
  // Charts are drawn to the width: redraw when it changes (not on height, which rendering itself changes).
  let width = 0;
  new ResizeObserver(function () {
    if (last && document.body.clientWidth !== width) {
      width = document.body.clientWidth;
      render(last.model, last.samples);
    }
  }).observe(document.body);
})();
</script>
</body>
</html>`;
}
