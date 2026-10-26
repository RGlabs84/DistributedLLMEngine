// How the sidebar draws the "Donate compute" panel. The model it draws comes from worker/donatePanel.ts; this only
// puts it on the page and posts what the owner moves. Everything in the model reaches the page as text, never as
// markup (the GPU's name comes from a driver), and there are no inline handlers: the sidebar's CSP allows only its
// one nonce'd script.

// The page's own `document`; this file is compiled without the DOM library.
declare const document: any;

/**
 * Builds the panel inside `root` and returns `update(model)`. It is written into the sidebar's script with
 * `.toString()` (like renderMarkdown), so it must not use anything outside itself.
 * `post` receives {type:'donateChoice', commit, choice}, {type:'toggleWorker', choice?} and {type:'donateApply', choice}.
 */
export function createDonatePanel(root: any, post: (message: any) => void) {
  const make = (tag: string, cls?: string, text?: string): any => {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text !== undefined) el.textContent = text;
    return el;
  };
  const add = (parent: any, ...kids: any[]): any => {
    kids.forEach((kid) => parent.appendChild(kid));
    return parent;
  };

  let model: any = null;

  // One section: a switch and a title in a head, then a body of rows.
  const makeSection = (title: string) => {
    const el = make('section', 'dn-sec');
    const head = make('div', 'dn-head');
    const label = make('label', 'dn-switch');
    const box = make('input');
    box.type = 'checkbox';
    box.setAttribute('aria-label', title);
    add(label, box, make('span', 'dn-track'));
    const detail = make('span', 'dn-detail');
    add(head, label, make('strong', 'dn-title', title), detail);
    const blurb = make('p', 'dn-blurb');
    const reason = make('p', 'dn-reason');
    const body = make('div', 'dn-body');
    add(el, head, blurb, reason, body);
    return { el, box, detail, blurb, reason, body };
  };

  // A slider row: label and value above, the range, then either the stops' labels or the two ends.
  const makeRow = (title: string) => {
    const el = make('div', 'dn-row');
    const top = make('div', 'dn-rowtop');
    const value = make('span', 'dn-value');
    add(top, make('span', 'dn-label', title), value);
    const input = make('input', 'dn-range');
    input.type = 'range';
    input.step = '1';
    input.setAttribute('aria-label', title);
    const ticks = make('div', 'dn-ticks');
    const note = make('p', 'dn-note');
    add(el, top, input, ticks, note);
    return { el, input, value, ticks, note, shape: '' };
  };

  const gpu = makeSection('GPU');
  const vram = makeRow('VRAM to donate');
  const gpuRam = makeRow("RAM for the GPU's model");
  add(gpu.body, vram.el, gpuRam.el);

  const cpu = makeSection('CPU');
  const cpuRam = makeRow('RAM to lend');
  const threads = makeRow('Threads for the swarm');
  const requirement = make('p', 'dn-note');
  add(cpu.body, cpuRam.el, threads.el, requirement);

  const powers = make('div', 'dn-powers');
  const powersList = make('div', 'dn-powers-list');
  add(powers, make('h4', undefined, 'What this powers'), powersList);

  const lockedNote = make('p', 'dn-locked');
  const donate = make('button', 'dn-donate');
  const hint = make('p', 'dn-hint');
  const apply = make('button', 'secondary dn-apply');
  add(root, lockedNote, gpu.el, cpu.el, powers, donate, hint, apply);

  // What the sliders say now, as the webview posts it (the extension validates it).
  const stopAt = (slider: any, row: any, fallback: number) => {
    const stops = slider ? slider.stops : [];
    const stop = stops[Number(row.input.value)] || stops[0];
    return stop ? stop.value : fallback;
  };
  const collect = () => ({
    gpuOn: gpu.box.checked,
    vramGb: Number(vram.input.value),
    gpuRamGb: stopAt(model.gpu.ram, gpuRam, model.choice.gpuRamGb),
    cpuOn: cpu.box.checked,
    cpuRamGb: stopAt(model.cpu.ram, cpuRam, model.choice.cpuRamGb),
    threads: stopAt(model.cpu.threads, threads, model.choice.threads),
  });

  // The ticks under a slider: every stop's label (and what it runs) when there are few, else just the two ends.
  const drawTicks = (row: any, slider: any, withStops: boolean) => {
    const shape = JSON.stringify([withStops, slider.stops.map((s: any) => [s.label, s.sub || ''])]);
    if (shape === row.shape) return;
    row.shape = shape;
    row.ticks.textContent = '';
    const last = slider.stops.length - 1;
    row.ticks.className = withStops ? 'dn-ticks dn-ticks-stops' : 'dn-ticks';
    const marks = withStops ? slider.stops : last > 0 ? [slider.stops[0], slider.stops[last]] : [];
    marks.forEach((stop: any, i: number) => {
      const tick = make('span', 'dn-tick');
      add(tick, make('span', 'dn-tick-label', stop.label));
      if (stop.sub) add(tick, make('span', 'dn-tick-sub', stop.sub));
      if (withStops) {
        const at = i / Math.max(1, marks.length - 1);
        if (i === 0) tick.className = 'dn-tick dn-tick-first';
        else if (i === marks.length - 1) tick.className = 'dn-tick dn-tick-last';
        else {
          tick.className = 'dn-tick dn-tick-mid';
          tick.style.left = 'calc(8px + (100% - 16px) * ' + at + ')';
        }
      }
      add(row.ticks, tick);
    });
  };

  // A slider over stops; a slider being dragged is never moved from under the pointer.
  const setStops = (row: any, slider: any, enabled: boolean, withStops: boolean) => {
    row.input.min = '0';
    row.input.max = String(Math.max(0, slider.stops.length - 1));
    if (document.activeElement !== row.input) row.input.value = String(slider.index);
    row.input.disabled = !enabled || slider.stops.length < 2;
    drawTicks(row, slider, withStops);
  };

  const setSection = (sec: any, on: boolean, available: boolean, locked: boolean) => {
    sec.el.className = 'dn-sec' + (on ? '' : ' dn-off') + (available ? '' : ' dn-na');
    sec.box.checked = on;
    sec.box.disabled = !available || locked;
    sec.reason.style.display = available ? 'none' : 'block';
    sec.body.style.display = available ? 'flex' : 'none';
  };

  const setNote = (el: any, text: string) => {
    el.textContent = text;
    el.style.display = text ? 'block' : 'none';
  };

  function update(next: any) {
    model = next;
    const m = next;
    const enabled = !m.locked;

    setSection(gpu, m.gpu.on, m.gpu.available, m.locked);
    gpu.detail.textContent = m.gpu.detail;
    gpu.blurb.textContent = m.gpu.blurb;
    gpu.reason.textContent = m.gpu.reason;
    const v = m.gpu.vram;
    if (document.activeElement !== vram.input) {
      vram.input.min = String(v.min);
      vram.input.max = String(v.max);
      vram.input.value = String(v.value);
    }
    vram.input.disabled = !enabled || !m.gpu.on || v.max <= v.min;
    vram.value.textContent = v.label;
    setNote(vram.note, v.max < 8 ? 'Under 8 GB of VRAM a GPU joins the GPU pool; it cannot run a coding model by itself.' : '');
    drawTicks(vram, { stops: v.max > v.min ? [{ label: v.min + ' GB' }, { label: v.max + ' GB' }] : [] }, false);
    gpuRam.el.style.display = m.gpu.ram ? '' : 'none';
    if (m.gpu.ram) {
      gpuRam.value.textContent = m.gpu.ram.label;
      setStops(gpuRam, m.gpu.ram, enabled && m.gpu.on, false);
      setNote(gpuRam.note, 'Auto counts the RAM that is free when the job starts.');
    }

    setSection(cpu, m.cpu.on, m.cpu.available, m.locked);
    cpu.detail.textContent = '';
    cpu.blurb.textContent = m.cpu.blurb;
    cpu.reason.textContent = m.cpu.reason;
    cpuRam.el.style.display = m.cpu.ram ? '' : 'none';
    if (m.cpu.ram) {
      cpuRam.value.textContent = m.cpu.ram.label;
      setStops(cpuRam, m.cpu.ram, enabled && m.cpu.on, true);
    }
    // Threads limit the CPU's job, and the GPU's coding model too, so they stay in reach while either is on.
    threads.value.textContent = m.cpu.threads.label;
    setStops(threads, m.cpu.threads, enabled && (m.cpu.on || m.gpu.on), false);
    setNote(threads.note, m.cpu.threads.note);
    setNote(requirement, m.cpu.requirement);

    powersList.textContent = '';
    m.powers.forEach((line: any) => add(powersList, make('p', 'dn-line dn-line-' + line.kind, line.text)));

    setNote(lockedNote, m.lockedNote);
    donate.textContent = m.button.label;
    donate.disabled = m.button.disabled;
    donate.className = 'dn-donate' + (m.button.action === 'stop' ? ' dn-stop' : '');
    setNote(hint, m.button.hint);
    apply.textContent = m.apply.label;
    apply.disabled = m.apply.disabled;
    apply.style.display = m.apply.visible ? 'block' : 'none';
  }

  // Moving a slider shows its value at once and asks the extension for the matching text; letting go commits it.
  const hook = (row: any, readout: () => string) => {
    row.input.addEventListener('input', () => {
      if (!model) return;
      row.value.textContent = readout();
      post({ type: 'donateChoice', commit: false, choice: collect() });
    });
    row.input.addEventListener('change', () => {
      if (model) post({ type: 'donateChoice', commit: true, choice: collect() });
    });
  };
  hook(vram, () => (Number(vram.input.value) >= model.gpu.vram.max ? 'All ' + model.gpu.vram.max + ' GB' : vram.input.value + ' GB'));
  hook(gpuRam, () => (model.gpu.ram.stops[Number(gpuRam.input.value)] || model.gpu.ram.stops[0]).label);
  hook(cpuRam, () => (model.cpu.ram.stops[Number(cpuRam.input.value)] || model.cpu.ram.stops[0]).label);
  hook(threads, () => (model.cpu.threads.stops[Number(threads.input.value)] || model.cpu.threads.stops[0]).label);
  [gpu, cpu].forEach((sec) => {
    sec.box.addEventListener('change', () => {
      if (model) post({ type: 'donateChoice', commit: true, choice: collect() });
    });
  });

  donate.addEventListener('click', () => {
    if (!model) return;
    post(model.button.action === 'stop' ? { type: 'toggleWorker' } : { type: 'toggleWorker', choice: collect() });
  });
  apply.addEventListener('click', () => {
    if (model) post({ type: 'donateApply', choice: collect() });
  });

  return { update };
}

/** The panel's styles. VS Code's theme variables, with fallbacks that read well on its default dark theme. */
export const DONATE_CSS = `
    .dn { display: flex; flex-direction: column; gap: 10px; }
    .dn-sec {
      border: 1px solid var(--vscode-widget-border, rgba(128, 128, 128, 0.35));
      border-radius: 6px;
      padding: 8px 10px 10px;
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    .dn-head { display: flex; align-items: center; gap: 8px; min-width: 0; }
    .dn-title { font-size: 0.95em; }
    .dn-detail {
      margin-left: auto;
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-size: 0.78em;
      color: var(--vscode-descriptionForeground, #9d9d9d);
    }
    .dn-switch { position: relative; display: inline-block; width: 30px; height: 16px; flex: none; }
    .dn-switch input { position: absolute; inset: 0; width: 100%; height: 100%; margin: 0; opacity: 0; cursor: pointer; z-index: 1; }
    .dn-track {
      position: absolute;
      inset: 0;
      border-radius: 8px;
      background: var(--vscode-input-background, #3c3c3c);
      border: 1px solid var(--vscode-checkbox-border, #6b6b6b);
      transition: background 0.15s;
    }
    .dn-track::after {
      content: '';
      position: absolute;
      top: 2px;
      left: 2px;
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: var(--vscode-foreground, #ccc);
      transition: transform 0.15s;
    }
    .dn-switch input:checked + .dn-track { background: var(--vscode-button-background, #0e639c); border-color: var(--vscode-button-background, #0e639c); }
    .dn-switch input:checked + .dn-track::after { transform: translateX(14px); background: #fff; }
    .dn-switch input:focus-visible + .dn-track { outline: 1px solid var(--vscode-focusBorder, #007fd4); outline-offset: 2px; }
    .dn-switch input:disabled { cursor: not-allowed; }
    .dn-switch input:disabled + .dn-track { opacity: 0.4; }
    .dn-blurb, .dn-reason, .dn-note, .dn-hint, .dn-locked { margin: 0; font-size: 0.78em; line-height: 1.4; color: var(--vscode-descriptionForeground, #9d9d9d); }
    .dn-reason, .dn-locked { color: var(--vscode-editorWarning-foreground, #cca700); }
    .dn-body { display: flex; flex-direction: column; gap: 10px; margin-top: 2px; }
    .dn-off .dn-body, .dn-off .dn-blurb { opacity: 0.55; }
    .dn-na .dn-blurb { display: none; }
    .dn-row { display: flex; flex-direction: column; gap: 2px; }
    .dn-rowtop { display: flex; justify-content: space-between; align-items: baseline; font-size: 0.84em; }
    .dn-value { font-weight: 600; font-variant-numeric: tabular-nums; }
    input[type=range].dn-range { width: 100%; margin: 4px 0 0; accent-color: var(--vscode-button-background, #0e639c); cursor: pointer; }
    input[type=range].dn-range:disabled { cursor: not-allowed; opacity: 0.5; }
    .dn-ticks { display: flex; justify-content: space-between; font-size: 0.72em; color: var(--vscode-descriptionForeground, #9d9d9d); }
    .dn-ticks:empty { display: none; }
    .dn-ticks-stops { position: relative; display: block; height: 2.5em; }
    .dn-tick { display: flex; flex-direction: column; line-height: 1.25; }
    .dn-ticks-stops .dn-tick { position: absolute; top: 2px; }
    .dn-tick-first { left: 0; align-items: flex-start; text-align: left; }
    .dn-tick-last { right: 0; align-items: flex-end; text-align: right; }
    .dn-tick-mid { align-items: center; text-align: center; transform: translateX(-50%); }
    .dn-tick-label { color: var(--vscode-foreground, #ccc); font-weight: 600; }
    .dn-powers {
      border-left: 3px solid var(--vscode-button-background, #0e639c);
      background: rgba(127, 127, 127, 0.1);
      border-radius: 0 6px 6px 0;
      padding: 7px 10px 3px;
    }
    .dn-powers h4 {
      margin: 0 0 5px;
      font-size: 0.72em;
      font-weight: 600;
      letter-spacing: 0.6px;
      text-transform: uppercase;
      color: var(--vscode-descriptionForeground, #9d9d9d);
    }
    .dn-line { margin: 0 0 6px; font-size: 0.84em; line-height: 1.4; }
    .dn-line-perk { color: var(--vscode-descriptionForeground, #9d9d9d); }
    .dn-line-warn { color: var(--vscode-editorWarning-foreground, #cca700); }
    button.dn-donate { width: 100%; padding: 9px 12px; font-size: 1em; font-weight: 600; border-radius: 6px; }
    button.dn-donate.dn-stop { background-color: #f43f5e; color: #fff; }
    button.dn-donate:disabled, button.dn-apply:disabled { opacity: 0.5; cursor: not-allowed; }
    button.dn-apply { width: 100%; display: none; }
    .dn-hint { text-align: center; }
`;
