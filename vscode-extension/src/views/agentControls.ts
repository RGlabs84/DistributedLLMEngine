// The controls at the top of the DLNE Agent panel (extension 1.6.0, docs/release-2.7-contract.md sections 2 and 3):
// Speed or Quality, the Graphics API, and, for an API-key holder, unlimited turns and the priority line.
//
// The extension decides what is shown and says it in words (buildAgentControls); the page only draws that model and
// posts what was clicked. Everything in the model reaches the page as text, never as markup (a GPU's reason comes
// from a driver), and there are no inline handlers: the sidebar's CSP allows only its one nonce'd script.
import type { EngineSetting, EngineState } from '../worker/engineFlavour';
import type { KeyTier } from '../agent/keyTier';
import type { WorkerStatus } from '../worker/workerManager';

// The page's own `document`; this file is compiled without the DOM library.
declare const document: any;

export type OwnGpuChoice = 'faster' | 'smarter';

/** What the panel shows right now. A row with `show: false` is not drawn at all. */
export interface AgentControlsModel {
  /** Speed or Quality (dlne.ownGpu): only a coding member does anything with it. */
  speed: { show: boolean; value: OwnGpuChoice; hint: string };
  /** Auto, Vulkan or CUDA (dlne.worker.engine). */
  engine: {
    show: boolean;
    value: EngineSetting;
    /** CUDA is greyed: this computer may not use it (`cudaReason` says why). */
    cudaOff: boolean;
    cudaReason: string;
    /** "running on CUDA", or why CUDA fell back to Vulkan; "" while no worker runs. */
    note: string;
  };
  /** The unlimited-turns switch (dlne.agent.unlimitedTurns): only for an API-key holder. */
  turns: { show: boolean; on: boolean };
  /** "Priority: first in line" for a key holder; "" otherwise. */
  priority: string;
}

const SPEED_HINT: Record<OwnGpuChoice, string> = {
  faster: 'Speed: your requests go to whichever answers sooner, usually the swarm\'s own engine.',
  smarter: 'Quality: your requests go to your own GPU whenever it runs a smarter model than the swarm\'s engine, even if it writes more slowly.',
};

export const PRIORITY_LINE = 'Priority: first in line';

/** An API-key holder, or the host itself: the two who may have unlimited turns and who go first in every queue. */
export function isKeyHolder(tier: KeyTier | null): boolean {
  return tier === 'key' || tier === 'host';
}

const engineName = (flavour: 'vulkan' | 'cuda') => (flavour === 'cuda' ? 'CUDA' : 'Vulkan');

/** A reason from the worker, as a clause: no closing full stop (the sentence adds its own). */
const clause = (text: string) => text.trim().replace(/[\s.;]+$/, '');

/** The line next to the Graphics API choice: what the worker runs on, and why when CUDA could not start. */
function engineNote(engine: EngineState): string {
  const fallback = clause(engine.fallback);
  if (fallback && engine.running === 'vulkan') return `CUDA could not start: ${fallback}; running on Vulkan`;
  if (fallback && engine.running === null) return `CUDA could not start last time: ${fallback}`;
  return engine.running ? `running on ${engineName(engine.running)}` : '';
}

/**
 * The panel for this computer: `status` is the worker's (its role is known once it runs), `ownGpu` and
 * `unlimitedTurns` are the settings as read (anything but the known values reads as the default), `engine` is
 * engineFlavour.ts's state and `tier` is keyTier.ts's answer (null: not known, or an older coordinator).
 */
export function buildAgentControls(input: {
  status: Pick<WorkerStatus, 'role' | 'cpuPool'>;
  ownGpu: unknown;
  engine: EngineState;
  tier: KeyTier | null;
  unlimitedTurns: unknown;
}): AgentControlsModel {
  const { status, engine, tier } = input;
  const ownGpu: OwnGpuChoice = input.ownGpu === 'smarter' ? 'smarter' : 'faster';
  const cudaOff = engine.capable === false;
  const holder = isKeyHolder(tier);
  return {
    speed: { show: status.role === 'coding', value: ownGpu, hint: SPEED_HINT[ownGpu] },
    engine: {
      // A computer that lends only its CPU has no graphics API to choose.
      show: status.cpuPool !== true,
      value: engine.setting === 'vulkan' || engine.setting === 'cuda' ? engine.setting : 'auto',
      cudaOff,
      cudaReason: cudaOff ? `CUDA is not available here: ${clause(engine.reason) || 'this GPU cannot run it'}.` : '',
      note: engineNote(engine),
    },
    turns: { show: holder, on: input.unlimitedTurns !== false },
    priority: holder ? PRIORITY_LINE : '',
  };
}

export const AGENT_CONTROLS_CSS = `
    /* The controls at the top, above the Donate card: Speed or Quality, Graphics API, and a key holder's two lines. */
    .agent-controls {
      flex-shrink: 0;
      display: flex;
      flex-direction: column;
      gap: 6px;
      padding: 8px 12px;
      border: 1px solid var(--card-border);
      border-radius: 6px;
      background-color: var(--card-bg);
      font-size: 0.86em;
    }
    .ac-row {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 6px 10px;
    }
    .ac-label { font-weight: 600; }
    .ac-seg { display: inline-flex; }
    .ac-seg button {
      border-radius: 0;
      padding: 3px 10px;
      margin-left: -1px;
    }
    .ac-seg button:first-child { border-radius: 4px 0 0 4px; margin-left: 0; }
    .ac-seg button:last-child { border-radius: 0 4px 4px 0; }
    .ac-seg button:disabled { opacity: 0.45; cursor: not-allowed; }
    .ac-note, .ac-hint, .ac-priority {
      color: var(--vscode-descriptionForeground);
      line-height: 1.35;
    }
    .ac-priority { font-weight: 600; color: var(--vscode-charts-green, #3fb950); }
`;

/**
 * Builds the controls inside `root` and returns `update(model)`. It is written into the sidebar's script with
 * `.toString()` (like createDonatePanel), so it must not use anything outside itself.
 * `post` receives {type:'setOwnGpu', value}, {type:'setEngine', value} and {type:'setUnlimitedTurns', on}; the
 * extension checks each before it writes a setting.
 */
export function createAgentControls(root: any, post: (message: any) => void) {
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

  // A row of buttons of which one is chosen (aria-pressed, like the Auto button).
  const makeChoice = (label: string, options: Array<{ value: string; text: string }>, type: string) => {
    const row = make('div', 'ac-row');
    const group = make('div', 'ac-seg');
    group.setAttribute('role', 'group');
    group.setAttribute('aria-label', label);
    const buttons: any = {};
    options.forEach((option) => {
      const button = make('button', 'secondary toggle', option.text);
      button.setAttribute('aria-pressed', 'false');
      button.addEventListener('click', () => {
        if (!button.disabled && button.getAttribute('aria-pressed') !== 'true') post({ type, value: option.value });
      });
      buttons[option.value] = button;
      add(group, button);
    });
    add(row, make('span', 'ac-label', label), group);
    const choose = (value: string) => {
      options.forEach((option) => buttons[option.value].setAttribute('aria-pressed', option.value === value ? 'true' : 'false'));
    };
    return { row, buttons, choose };
  };

  const speed = makeChoice('Speed or quality', [{ value: 'faster', text: 'Speed' }, { value: 'smarter', text: 'Quality' }], 'setOwnGpu');
  const speedHint = make('div', 'ac-hint');

  const engine = makeChoice('Graphics API', [
    { value: 'auto', text: 'Auto' }, { value: 'vulkan', text: 'Vulkan' }, { value: 'cuda', text: 'CUDA' },
  ], 'setEngine');
  const engineNote = make('span', 'ac-note');
  engineNote.setAttribute('role', 'status');
  add(engine.row, engineNote);
  const cudaReason = make('div', 'ac-note');

  const turnsRow = make('div', 'ac-row');
  const turns = make('button', 'secondary toggle', 'Unlimited turns: on');
  turns.setAttribute('aria-pressed', 'true');
  turns.title = 'On: the agent has no limit on its steps in one task. Off: it pauses at the limit set in dlne.agent.maxTurns and offers Continue. Stop and the loop check still work.';
  turns.addEventListener('click', () => post({ type: 'setUnlimitedTurns', on: turns.getAttribute('aria-pressed') !== 'true' }));
  add(turnsRow, turns);
  const priority = make('div', 'ac-priority');

  add(root, speed.row, speedHint, engine.row, cudaReason, turnsRow, priority);

  const shown = (el: any, on: boolean) => {
    el.style.display = on ? '' : 'none';
  };
  const text = (el: any, value: string) => {
    el.textContent = value;
    shown(el, value !== '');
  };

  function update(model: AgentControlsModel) {
    shown(speed.row, model.speed.show);
    shown(speedHint, model.speed.show);
    speed.choose(model.speed.value);
    speedHint.textContent = model.speed.hint;

    shown(engine.row, model.engine.show);
    engine.choose(model.engine.value);
    engine.buttons.cuda.disabled = model.engine.cudaOff;
    engine.buttons.cuda.title = model.engine.cudaOff ? model.engine.cudaReason : '';
    text(engineNote, model.engine.note);
    text(cudaReason, model.engine.show ? model.engine.cudaReason : '');
    if (!model.engine.show) engineNote.style.display = 'none';

    shown(turnsRow, model.turns.show);
    turns.setAttribute('aria-pressed', model.turns.on ? 'true' : 'false');
    turns.textContent = model.turns.on ? 'Unlimited turns: on' : 'Unlimited turns: off';
    text(priority, model.priority);

    shown(root, model.speed.show || model.engine.show || model.turns.show || model.priority !== '');
  }

  return { update };
}
