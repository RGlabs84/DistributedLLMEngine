'use strict';
/**
 * The DLNE Agent panel of extension 1.6.0 (docs/release-2.7-contract.md sections 2 and 3), through the real
 * SidebarProvider and the real page script:
 *
 * 1. Speed or Quality: shown only to a coding member, writes dlne.ownGpu, and follows Settings both ways.
 * 2. Graphics API: Auto / Vulkan / CUDA through engineFlavour.ts; CUDA greyed with its reason where it may not run;
 *    "running on ..." and the fall-back reason next to it.
 * 3. An API-key holder's unlimited-turns switch (dlne.agent.unlimitedTurns) and the "first in line" line.
 * 4. Terminal cards: the command line opens and shuts the card; Copy command and Copy output.
 * 5. Copy on every message (yours, the agent's Markdown source, saved chats drawn again), selectable text, and the
 *    right-click menu left to VS Code.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const stub = require('./helpers/vscode-stub');
const { createPage, openView, scriptedClient, say } = require('./helpers/sidebar-page');
const { SidebarProvider } = require('../dist/views/sidebarProvider');
const { buildAgentControls, PRIORITY_LINE } = require('../dist/views/agentControls');
const engineFlavour = require('../dist/worker/engineFlavour');
const keyTier = require('../dist/agent/keyTier');

// The agent reads ~/.dlne/memory.md: a home of its own keeps these runs out of the real one.
process.env.HOME = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-agent-panel-home-'));
const ROOT = path.join(__dirname, '..');

const IDLE = { state: 'idle', gpu: { hasGpu: true, name: 'GPU', api: 'vulkan', vramTotalGb: 12 }, vramContributedGb: 0, cpuPool: false };
const CODING = { ...IDLE, state: 'contributing', vramContributedGb: 12, role: 'coding', roleDetail: { slots: 2 } };
const CPU_ONLY = { ...IDLE, gpu: { hasGpu: false, name: '', api: 'cpu', vramTotalGb: 0 }, cpuPool: true };
const ENGINE = { setting: 'auto', running: null, capable: null, reason: '', gpu: '', fallback: '', consent: 'unasked' };

/**
 * The real provider shown in a view, with the worker, the engine state and the key tier in the test's hands.
 * `world.status`, `world.engine` and `world.tier` are read when the panel asks; the `fire*` functions are the events.
 */
async function withPanel(options, body) {
  stub.reset();
  Object.assign(stub.state.settings, options.settings || {});
  const world = {
    status: options.status || IDLE,
    engine: { ...ENGINE, ...(options.engine || {}) },
    tier: options.tier === undefined ? null : options.tier,
    engineSets: [],
    statusListeners: [],
    engineListeners: [],
    tierListeners: [],
  };
  const real = { get: engineFlavour.getEngineState, set: engineFlavour.setEngineSetting, on: engineFlavour.onEngineStateChanged,
    tier: keyTier.getKeyTier, onTier: keyTier.onKeyTierChanged };
  engineFlavour.getEngineState = () => world.engine;
  engineFlavour.setEngineSetting = async (value) => { world.engineSets.push(value); world.engine = { ...world.engine, setting: value }; };
  engineFlavour.onEngineStateChanged = (fn) => { world.engineListeners.push(fn); return { dispose() {} }; };
  keyTier.getKeyTier = async () => world.tier;
  keyTier.onKeyTierChanged = (fn) => { world.tierListeners.push(fn); return { dispose() {} }; };
  world.fireStatus = () => world.statusListeners.forEach((fn) => fn(world.status));
  world.fireEngine = () => world.engineListeners.forEach((fn) => fn(world.engine));
  world.fireTier = (tier) => { world.tier = tier; world.tierListeners.forEach((fn) => fn(tier)); };

  const memento = new Map();
  const storage = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-agent-panel-store-'));
  const context = {
    extensionUri: { fsPath: ROOT },
    storageUri: { fsPath: storage },
    globalStorageUri: { fsPath: path.join(storage, 'global') },
    subscriptions: [],
    workspaceState: { get: (k) => memento.get(k), update: async (k, v) => { memento.set(k, v); } },
  };
  const workerManager = { onStatusChange: (fn) => world.statusListeners.push(fn), getStatus: () => world.status };
  const provider = new SidebarProvider(context.extensionUri, scriptedClient([say('x')]), workerManager, context);
  const view = openView(provider);
  view.mount();
  try {
    await view.idle();
    // keyTier.getKeyTier is asked as the view is made; let its answer arrive.
    await new Promise((resolve) => setImmediate(resolve));
    return await body({ view, page: view.page, world, provider });
  } finally {
    Object.assign(engineFlavour, { getEngineState: real.get, setEngineSetting: real.set, onEngineStateChanged: real.on });
    Object.assign(keyTier, { getKeyTier: real.tier, onKeyTierChanged: real.onTier });
    fs.rmSync(storage, { recursive: true, force: true });
  }
}

const controls = (page) => page.el('agent-controls');
/** Drawn: neither it nor anything it sits in (up to the controls) is hidden. */
const shown = (el) => { for (let node = el; node; node = node.parent) if (node.style.display === 'none') return false; return true; };
/** The buttons of the controls by their words. */
const buttonsOf = (page) => Object.fromEntries(controls(page).descendants().filter((e) => e.tag === 'button').map((b) => [b.textContent, b]));
const pressed = (button) => button.getAttribute('aria-pressed') === 'true';
const textOf = (page) => controls(page).descendants().filter((e) => shown(e)).map((e) => e.ownText).filter(Boolean).join(' | ');
const settle = () => new Promise((resolve) => setImmediate(resolve));

// ---- 1. Speed or Quality -----------------------------------------------------------------------------------------

test('Speed or Quality is shown to a coding member and to nobody else', async () => {
  await withPanel({ status: IDLE }, async ({ page, world }) => {
    assert.equal(shown(controls(page).children[0]), false, 'a computer that is not a coding member has no use for it');
    world.status = { ...IDLE, state: 'contributing', role: 'rpc' };
    world.fireStatus();
    assert.equal(shown(controls(page).children[0]), false, 'a GPU pool member neither');
    world.status = CODING;
    world.fireStatus();
    assert.equal(shown(controls(page).children[0]), true, 'a coding member does');
    assert.deepEqual(Object.keys(buttonsOf(page)).slice(0, 2), ['Speed', 'Quality']);
    world.status = IDLE;
    world.fireStatus();
    assert.equal(shown(controls(page).children[0]), false, 'and it goes when the worker stops');
  });
});

test('Speed or Quality shows dlne.ownGpu, writes it when clicked, and follows Settings', async () => {
  await withPanel({ status: CODING }, async ({ page, view }) => {
    let b = buttonsOf(page);
    assert.equal(pressed(b.Speed), true, 'the default is faster = Speed');
    assert.equal(pressed(b.Quality), false);
    assert.match(textOf(page), /Speed: your requests go to whichever answers sooner/);

    b.Quality.click();
    await view.idle();
    assert.deepEqual(stub.state.updates, [['dlne.ownGpu', 'smarter']]);
    assert.equal(pressed(b.Quality), true);
    assert.equal(pressed(b.Speed), false);
    assert.match(textOf(page), /Quality: your requests go to your own GPU/);

    // Clicking the choice already made writes nothing.
    stub.state.updates.length = 0;
    b.Quality.click();
    await view.idle();
    assert.deepEqual(stub.state.updates, []);

    // Changed in Settings (or settings.json): the panel follows.
    stub.state.settings['dlne.ownGpu'] = 'faster';
    stub.fireConfigChange('dlne.ownGpu');
    b = buttonsOf(page);
    assert.equal(pressed(b.Speed), true);
    assert.equal(pressed(b.Quality), false);
  });
});

test('a post that is not "faster" or "smarter" writes nothing', async () => {
  await withPanel({ status: CODING }, async ({ view }) => {
    for (const value of ['fastest', '', null, 7, { a: 1 }, undefined]) await view.toHost({ type: 'setOwnGpu', value });
    assert.deepEqual(stub.state.updates, []);
  });
});

// ---- 2. Graphics API ---------------------------------------------------------------------------------------------

test('Graphics API shows dlne.worker.engine, and a click goes to engineFlavour.setEngineSetting', async () => {
  await withPanel({ engine: { setting: 'vulkan' } }, async ({ page, view, world }) => {
    const b = buttonsOf(page);
    assert.deepEqual([pressed(b.Auto), pressed(b.Vulkan), pressed(b.CUDA)], [false, true, false]);
    b.CUDA.click();
    await view.idle();
    assert.deepEqual(world.engineSets, ['cuda']);
    assert.equal(pressed(buttonsOf(page).CUDA), true, 'the panel shows the state engineFlavour now holds');
    assert.equal(pressed(buttonsOf(page).Vulkan), false);
    b.Auto.click();
    await view.idle();
    assert.deepEqual(world.engineSets, ['cuda', 'auto']);
    b.Auto.click();
    await view.idle();
    assert.deepEqual(world.engineSets, ['cuda', 'auto'], 'the choice already made is not set again');
  });
});

test('Graphics API follows the engine state: what runs, and the fall-back reason', async () => {
  await withPanel({ engine: { setting: 'auto' } }, async ({ page, world }) => {
    assert.doesNotMatch(textOf(page), /running on/, 'no worker, nothing runs');
    world.engine = { ...world.engine, running: 'cuda' };
    world.fireEngine();
    assert.match(textOf(page), /running on CUDA/);
    world.engine = { ...world.engine, running: 'vulkan', fallback: 'no CUDA device was found.', setting: 'cuda' };
    world.fireEngine();
    assert.match(textOf(page), /CUDA could not start: no CUDA device was found; running on Vulkan/);
    assert.equal(pressed(buttonsOf(page).CUDA), true, 'the setting is still what the user chose');
    world.engine = { ...world.engine, running: null };
    world.fireEngine();
    assert.match(textOf(page), /CUDA could not start last time: no CUDA device was found/);
    world.engine = { ...world.engine, running: 'vulkan', fallback: '' };
    world.fireEngine();
    assert.match(textOf(page), /running on Vulkan/);
    assert.doesNotMatch(textOf(page), /could not start/);
  });
});

test('CUDA is greyed with the reason where this computer may not use it, and a forged click does nothing', async () => {
  await withPanel({ engine: { capable: false, reason: 'this GPU is older than Turing (compute capability 6.1)' } }, async ({ page, view, world }) => {
    const b = buttonsOf(page);
    assert.equal(b.CUDA.disabled, true);
    assert.equal(b.Vulkan.disabled, false);
    assert.equal(b.Auto.disabled, false);
    assert.match(b.CUDA.title, /this GPU is older than Turing/);
    assert.match(textOf(page), /CUDA is not available here: this GPU is older than Turing \(compute capability 6\.1\)\./, 'the reason is in the words, not only a tooltip');
    b.CUDA.click();
    await view.idle();
    assert.deepEqual(view.fromPage.filter((m) => m.type === 'setEngine'), [], 'a greyed button posts nothing');
    await view.toHost({ type: 'setEngine', value: 'cuda' });
    assert.deepEqual(world.engineSets, [], 'and the extension does not act on a post that got past it');
    await view.toHost({ type: 'setEngine', value: 'vulkan' });
    assert.deepEqual(world.engineSets, ['vulkan']);
    // The worker finds the GPU can run it after all (a new driver): the button is back.
    world.engine = { ...world.engine, capable: true, reason: '' };
    world.fireEngine();
    assert.equal(buttonsOf(page).CUDA.disabled, false);
    assert.doesNotMatch(textOf(page), /not available here/);
  });
});

test('CUDA is open while the worker has not yet said whether it may be used', async () => {
  await withPanel({ engine: { capable: null } }, async ({ page }) => {
    assert.equal(buttonsOf(page).CUDA.disabled, false);
  });
});

test('a post that is not auto, vulkan or cuda sets nothing', async () => {
  await withPanel({}, async ({ view, world }) => {
    for (const value of ['rocm', '', null, 3, undefined]) await view.toHost({ type: 'setEngine', value });
    assert.deepEqual(world.engineSets, []);
  });
});

test('a computer that lends only its CPU has no Graphics API to choose', async () => {
  await withPanel({ status: CPU_ONLY }, async ({ page, world }) => {
    assert.equal(shown(controls(page)), false, 'nothing at all to show');
    world.status = IDLE;
    world.fireStatus();
    assert.equal(shown(controls(page)), true);
  });
});

test('the controls are in the first page, before the extension says anything, and come again with a restored chat', async () => {
  await withPanel({ status: CODING, settings: { 'dlne.ownGpu': 'smarter' } }, async ({ view }) => {
    const html = view.webview.html;
    assert.match(html, /agentControls\.update\(\{"speed":\{"show":true,"value":"smarter"/);
    view.reloadPage();
    assert.equal(pressedIn(view.page, 'Quality'), true);
    assert.ok(view.posted('restoreHistory').at(-1).controls.speed.show);
  });
});
const pressedIn = (page, name) => pressed(buttonsOf(page)[name]);

// ---- 3. API-key holders ------------------------------------------------------------------------------------------

test('unlimited turns and the priority line are shown to a key holder (or the host), and to nobody else', async () => {
  await withPanel({ tier: null }, async ({ page, world }) => {
    for (const tier of [null, 'gpu', 'cpu', 'trial', 'open']) {
      world.fireTier(tier);
      assert.doesNotMatch(textOf(page), /Unlimited turns|Priority/, `tier ${tier}`);
      assert.equal(shown(buttonsOf(page)['Unlimited turns: on']), false);
    }
    for (const tier of ['key', 'host']) {
      world.fireTier(tier);
      assert.equal(shown(buttonsOf(page)['Unlimited turns: on']), true, `tier ${tier}`);
      assert.match(textOf(page), /Priority: first in line/);
    }
    world.fireTier(null);
    assert.doesNotMatch(textOf(page), /Priority/, 'and it goes when the coordinator no longer says so');
  });
});

test('a key holder is shown the switch from the first answer of keyTier.getKeyTier', async () => {
  await withPanel({ tier: 'key' }, async ({ page }) => {
    assert.match(textOf(page), /Priority: first in line/);
    assert.equal(shown(buttonsOf(page)['Unlimited turns: on']), true, 'on by default: the setting is read with a default of true');
  });
});

test('the unlimited-turns switch writes dlne.agent.unlimitedTurns and follows Settings', async () => {
  await withPanel({ tier: 'key' }, async ({ page, view }) => {
    buttonsOf(page)['Unlimited turns: on'].click();
    await view.idle();
    assert.deepEqual(stub.state.updates, [['dlne.agent.unlimitedTurns', false]]);
    const off = buttonsOf(page)['Unlimited turns: off'];
    assert.ok(off);
    assert.equal(pressed(off), false);
    off.click();
    await view.idle();
    assert.deepEqual(stub.state.updates.at(-1), ['dlne.agent.unlimitedTurns', true]);
    stub.state.settings['dlne.agent.unlimitedTurns'] = false;
    stub.fireConfigChange('dlne.agent.unlimitedTurns');
    assert.ok(buttonsOf(page)['Unlimited turns: off'], 'changed in Settings');
  });
});

test('the unlimited-turns setting is written only for a key holder, and only with true or false', async () => {
  await withPanel({ tier: 'gpu' }, async ({ view, world }) => {
    await view.toHost({ type: 'setUnlimitedTurns', on: false });
    assert.deepEqual(stub.state.updates, [], 'not a key holder: the post is not acted on');
    world.fireTier('host');
    for (const on of ['yes', 1, null, undefined]) await view.toHost({ type: 'setUnlimitedTurns', on });
    assert.deepEqual(stub.state.updates, []);
    await view.toHost({ type: 'setUnlimitedTurns', on: false });
    assert.deepEqual(stub.state.updates, [['dlne.agent.unlimitedTurns', false]]);
  });
});

test('the panel model, on its own', () => {
  const model = (over) => buildAgentControls({ status: IDLE, ownGpu: 'faster', engine: ENGINE, tier: null, unlimitedTurns: true, ...over });
  assert.equal(model({ status: CODING }).speed.show, true);
  assert.equal(model({ status: IDLE }).speed.show, false);
  assert.equal(model({ ownGpu: 'bogus' }).speed.value, 'faster', 'an unknown setting reads as the default');
  assert.equal(model({ unlimitedTurns: undefined }).turns.on, true, 'on unless the setting says false');
  assert.equal(model({ unlimitedTurns: false, tier: 'key' }).turns.on, false);
  assert.equal(model({ tier: 'host' }).priority, PRIORITY_LINE);
  assert.equal(model({ tier: 'gpu' }).priority, '');
  assert.equal(model({ engine: { ...ENGINE, setting: 'weird' } }).engine.value, 'auto');
  assert.equal(model({ engine: { ...ENGINE, capable: false, reason: '' } }).engine.cudaReason, 'CUDA is not available here: this GPU cannot run it.');
});

test('what the worker or a driver says is shown as text, and cannot end the page script', async () => {
  const nasty = '</script><img src=x onerror=alert(1)>';
  await withPanel({ engine: { capable: false, reason: nasty, fallback: nasty, running: 'vulkan' } }, async ({ page, view }) => {
    assert.equal(view.webview.html.split('</script>').length, 2, 'one script block, however the reason ends');
    assert.equal(controls(page).descendants().filter((e) => e.tag === 'img').length, 0);
    assert.ok(textOf(page).includes(nasty), 'the words are there, as words');
    assert.equal(controls(page).descendants().filter((e) => e.ownHtml).length, 0, 'nothing in the controls is set as HTML');
  });
});

// ---- 4. terminal cards -------------------------------------------------------------------------------------------

const LONG = Array.from({ length: 60 }, (_, i) => `line ${i}`).join('\n');
const terminal = (page, command, output, is_error) => {
  page.receive({ type: 'appendUserMessage', text: 'run it' });
  page.receive({ type: 'toolEnd', result: { name: 'run_terminal_command', output, terminalCommand: command, is_error } });
};

test('clicking the command line opens the card to its whole output, and again shuts it', async () => {
  await withPanel({}, async ({ page }) => {
    terminal(page, 'seq 60', LONG);
    const [card] = page.find('terminal-card');
    const [cmd] = page.find('terminal-cmd');
    assert.equal(card.classes.has('expanded'), false, 'it starts at its 140 px');
    assert.equal(cmd.getAttribute('aria-expanded'), 'false');
    assert.equal(cmd.getAttribute('role'), 'button');
    assert.equal(cmd.getAttribute('tabindex'), '0', 'the keyboard reaches it');
    cmd.click();
    assert.equal(card.classes.has('expanded'), true);
    assert.equal(cmd.getAttribute('aria-expanded'), 'true');
    cmd.click();
    assert.equal(card.classes.has('expanded'), false);
    assert.equal(cmd.getAttribute('aria-expanded'), 'false');
  });
});

test('Enter and Space on the command line open and shut the card; other keys do not', async () => {
  await withPanel({}, async ({ page }) => {
    terminal(page, 'seq 60', LONG);
    const [card] = page.find('terminal-card');
    const [cmd] = page.find('terminal-cmd');
    const press = (key) => { const prevented = []; cmd.dispatch('keydown', { key, preventDefault: () => prevented.push(key) }); return prevented.length; };
    assert.equal(press('Enter'), 1);
    assert.equal(card.classes.has('expanded'), true);
    assert.equal(press(' '), 1, 'Space must not scroll the chat as well');
    assert.equal(card.classes.has('expanded'), false);
    assert.equal(press('a'), 0);
    assert.equal(press('Tab'), 0);
    assert.equal(card.classes.has('expanded'), false);
  });
});

test('the collapsed card is 140 px, and the open one has no limit', () => {
  const html = fs.readFileSync(path.join(ROOT, 'src/views/sidebarProvider.ts'), 'utf8');
  assert.match(html, /\.terminal-out \{[^}]*max-height: 140px;/);
  assert.match(html, /\.terminal-card\.expanded \.terminal-out \{ max-height: none; \}/);
});

test('Copy command and Copy output put the command and the whole output on the clipboard', async () => {
  await withPanel({}, async ({ page, view }) => {
    terminal(page, 'seq 60 && echo "<done>"', LONG);
    const [card] = page.find('terminal-card');
    const [copyCommand] = card.querySelectorAll('.term-copy-command');
    const [copyOutput] = card.querySelectorAll('.term-copy-output');
    assert.equal(copyCommand.textContent, 'Copy command');
    assert.equal(copyOutput.textContent, 'Copy output');
    copyCommand.click();
    await view.idle();
    assert.equal(stub.state.clipboard, 'seq 60 && echo "<done>"', 'the command alone, without the card\'s words');
    assert.equal(copyCommand.textContent, 'Copied');
    copyOutput.click();
    await view.idle();
    assert.equal(stub.state.clipboard, LONG, 'all of the output, not the 140 px that show');
    page.flushTimers();
    assert.equal(copyOutput.textContent, 'Copy output', 'the button says what it is again');
    assert.equal(card.classes.has('expanded'), false, 'copying does not open the card');
  });
});

test('a command that printed nothing has nothing to copy, and says so by greying the button', async () => {
  await withPanel({}, async ({ page }) => {
    terminal(page, 'true', '');
    const [copyOutput] = page.find('terminal-card')[0].querySelectorAll('.term-copy-output');
    assert.equal(copyOutput.disabled, true);
    assert.equal(page.find('terminal-card')[0].querySelectorAll('.term-copy-command')[0].disabled, false);
  });
});

test('the card and its buttons are the same when a saved chat is drawn again', async () => {
  const result = { name: 'run_terminal_command', output: LONG, terminalCommand: 'seq 60' };
  await withPanel({}, async ({ page, view }) => {
    page.receive({ type: 'restoreHistory', session: null, history: [
      { id: 'u', role: 'user', text: 'count' },
      { id: 'a', role: 'assistant', text: 'Done.', toolCalls: [{ name: 'run_terminal_command', args: {}, result }] },
    ], run: { busy: false, events: [] } });
    const [card] = page.find('terminal-card');
    assert.ok(card, 'drawn');
    card.querySelectorAll('.terminal-cmd')[0].click();
    assert.equal(card.classes.has('expanded'), true);
    card.querySelectorAll('.term-copy-output')[0].click();
    card.querySelectorAll('.term-copy-command')[0].click();
    await view.idle();
    assert.deepEqual(view.fromPage.filter((m) => m.type === 'copyCode').map((m) => m.text), [LONG, 'seq 60']);
  });
});

test('the terminal card keeps the words its tests read: the mark, "Terminal:" and the command', async () => {
  await withPanel({}, async ({ page }) => {
    terminal(page, 'sleep 60', 'Stopped.', true);
    assert.equal(page.find('terminal-cmd')[0].ownText, '⚠️ Terminal: sleep 60');
  });
});

// ---- 5. Copy from the chat ---------------------------------------------------------------------------------------

test('your own message has a Copy button that copies exactly what you typed', async () => {
  await withPanel({}, async ({ page, view }) => {
    const typed = 'fix <b>this</b>\n  and keep the spaces & the "quotes"';
    page.receive({ type: 'appendUserMessage', text: typed });
    const [user] = page.find('user');
    const copy = user.querySelectorAll('.msg-copy');
    assert.equal(copy.length, 1);
    assert.equal(copy[0].textContent, 'Copy');
    assert.equal(copy[0].getAttribute('aria-label'), 'Copy this message');
    copy[0].click();
    await view.idle();
    assert.equal(stub.state.clipboard, typed, 'without the button\'s own word');
    assert.equal(copy[0].textContent, 'Copied');
    page.flushTimers();
    assert.equal(copy[0].textContent, 'Copy');
  });
});

test('the agent\'s message copies its Markdown source, not the rendered page', async () => {
  await withPanel({}, async ({ page, view }) => {
    page.receive({ type: 'appendUserMessage', text: 'hi' });
    page.receive({ type: 'contentDelta', delta: '# Title\n\nSome **bold** and ' });
    page.receive({ type: 'contentDelta', delta: '`code`.\n\n```js\nlet a = 1 < 2;\n```\n' });
    page.flushTimers();
    const [reply] = page.find('assistant');
    assert.match(page.find('assistant-body')[0].innerHTML, /<strong>bold<\/strong>/, 'drawn as Markdown');
    reply.querySelectorAll('.msg-copy')[0].click();
    await view.idle();
    assert.equal(stub.state.clipboard, '# Title\n\nSome **bold** and `code`.\n\n```js\nlet a = 1 < 2;\n```\n');
  });
});

test('an answer written in pieces between tool calls copies as one text, and the tool lines are not in it', async () => {
  await withPanel({}, async ({ page, view }) => {
    page.receive({ type: 'appendUserMessage', text: 'go' });
    page.receive({ type: 'contentDelta', delta: 'First, a look.' });
    page.receive({ type: 'toolStart', toolName: 'list_directory' });
    page.receive({ type: 'toolEnd', result: { name: 'list_directory', output: 'notes.txt' } });
    page.receive({ type: 'contentDelta', delta: 'Then **done**.' });
    page.receive({ type: 'turnComplete' });
    const [reply] = page.find('assistant');
    assert.equal(reply.querySelectorAll('.msg-copy').length, 1, 'one button for the message');
    reply.querySelectorAll('.msg-copy')[0].click();
    await view.idle();
    assert.equal(stub.state.clipboard, 'First, a look.\n\nThen **done**.');
  });
});

test('a reply with no words of its own (only tool results) has no Copy button, and a status line or an error never does', async () => {
  await withPanel({}, async ({ page }) => {
    page.receive({ type: 'appendUserMessage', text: 'go' });
    page.receive({ type: 'loadingProgress', statusText: 'Waiting for the swarm' });
    page.receive({ type: 'toolEnd', result: { name: 'list_directory', output: 'notes.txt' } });
    page.receive({ type: 'error', error: 'Error: out of ideas' });
    page.receive({ type: 'turnComplete' });
    assert.equal(page.find('assistant')[0].querySelectorAll('.msg-copy').length, 0);
    const errorLine = page.find('message').find((m) => m.textContent === 'Error: out of ideas');
    assert.ok(errorLine, 'the error line keeps its words exactly');
    assert.equal(errorLine.querySelectorAll('.msg-copy').length, 0);
  });
});

test('a text-less reply that starts with a tool call loses a Copy button it was given for words it then dropped', async () => {
  await withPanel({}, async ({ page }) => {
    page.receive({ type: 'appendUserMessage', text: 'go' });
    // The model begins with a tool call written as text; the page removes it when the call is announced.
    page.receive({ type: 'contentDelta', delta: '```json\n{"name": "read_file", "arguments": {}}\n```' });
    page.flushTimers();
    assert.equal(page.find('assistant')[0].querySelectorAll('.msg-copy').length, 1);
    page.receive({ type: 'toolStart', toolName: 'read_file' });
    assert.equal(page.find('assistant')[0].querySelectorAll('.msg-copy').length, 0);
  });
});

test('a saved chat drawn again has the same Copy buttons, and they copy the same text', async () => {
  await withPanel({}, async ({ page, view }) => {
    page.receive({ type: 'restoreHistory', session: null, history: [
      { id: 'u', role: 'user', text: 'explain\nthis' },
      { id: 'a', role: 'assistant', text: 'It is **simple**:\n\n- one\n- two' },
    ], run: { busy: false, events: [] } });
    const copies = page.find('msg-copy');
    assert.equal(copies.length, 2);
    copies[0].click();
    await view.idle();
    assert.equal(stub.state.clipboard, 'explain\nthis');
    copies[1].click();
    await view.idle();
    assert.equal(stub.state.clipboard, 'It is **simple**:\n\n- one\n- two');
  });
});

test('Copy works on a reply while it is still being written', async () => {
  await withPanel({}, async ({ page, view }) => {
    page.receive({ type: 'appendUserMessage', text: 'go' });
    page.receive({ type: 'contentDelta', delta: 'Part one, ' });
    page.flushTimers();
    page.receive({ type: 'contentDelta', delta: 'part two' });
    page.flushTimers();
    page.find('assistant')[0].querySelectorAll('.msg-copy')[0].click();
    await view.idle();
    assert.equal(stub.state.clipboard, 'Part one, part two');
  });
});

test('the code blocks\' own Copy still copies just the code', async () => {
  await withPanel({}, async ({ page, view }) => {
    page.receive({ type: 'appendUserMessage', text: 'go' });
    page.receive({ type: 'contentDelta', delta: 'text' });
    page.flushTimers();
    const body = page.find('assistant-body')[0];
    // The page's markdown/copy-button pass works on real DOM; here its button is made by hand, as it would make it.
    const wrap = createWrap(body);
    wrap.querySelectorAll('.copy-btn')[0].click();
    await view.idle();
    assert.equal(stub.state.clipboard, 'let a = 1;');
  });
});
function createWrap(body) {
  const { FakeElement } = require('./helpers/sidebar-page');
  const wrap = new FakeElement('div');
  wrap.className = 'code-wrap';
  const pre = new FakeElement('pre');
  const code = new FakeElement('code');
  code.textContent = 'let a = 1;';
  pre.appendChild(code);
  wrap.appendChild(pre);
  const button = new FakeElement('button');
  button.className = 'secondary copy-btn';
  wrap.appendChild(button);
  body.appendChild(wrap);
  return wrap;
}

test('text in the chat can be selected, and the page leaves Ctrl+C, Cmd+C and the right-click menu to VS Code', async () => {
  await withPanel({}, async ({ view }) => {
    const html = view.webview.html;
    // Nothing that holds the chat's words switches selection off (the thought drawer's heading, a toggle, is the only user-select: none).
    const rules = [...html.matchAll(/([^{}]+)\{([^{}]*)\}/g)].filter(([, , body]) => /user-select:\s*none/.test(body)).map(([, selector]) => selector.trim());
    // ...and the Copy buttons, whose word must not end up in a copied selection of the message.
    assert.deepEqual(rules, ['.msg-copy, .terminal-actions', '.thinking-drawer summary']);
    assert.match(html, /\.message, \.terminal-card \{\s*-webkit-user-select: text;\s*user-select: text;/);
    // VS Code shows Cut / Copy / Paste on a right-click in a webview by itself, unless the page asks it not to
    // (data-vscode-context with preventDefaultContextMenuItems) or takes the event over (a contextmenu handler).
    assert.doesNotMatch(html, /preventDefaultContextMenuItems/);
    assert.doesNotMatch(html, /data-vscode-context/);
    assert.doesNotMatch(html, /['"]contextmenu['"]/);
    // And no key handler on the chat takes the Copy shortcut.
    const keyHandlers = [...html.matchAll(/addEventListener\('keydown', \(e\) => \{([\s\S]*?)\n    \}\);/g)].map((m) => m[1]);
    assert.ok(keyHandlers.length >= 1);
    for (const body of keyHandlers) assert.doesNotMatch(body, /ctrlKey|metaKey|key === 'c'/);
  });
});

test('the Copy button is out of sight until the message is pointed at or focused, and never when it would copy nothing', async () => {
  await withPanel({}, async ({ view }) => {
    const html = view.webview.html;
    assert.match(html, /\.msg-copy \{[^}]*opacity: 0;/);
    assert.match(html, /\.message:hover \.msg-copy, \.message:focus-within \.msg-copy \{ opacity: 0\.85; \}/);
    assert.match(html, /\.msg-copy:hover, \.msg-copy:focus-visible \{ opacity: 1; \}/);
  });
});

test('the page still has no inline handlers, and keeps its one nonce\'d script', async () => {
  await withPanel({ status: CODING, tier: 'key' }, async ({ view }) => {
    const html = view.webview.html;
    assert.doesNotMatch(html, /\son[a-z]+\s*=\s*["']/i);
    assert.equal((html.match(/<script/g) || []).length, 1);
    assert.match(html, /script-src 'nonce-[^']+'/);
  });
});

test('the new controls are reachable without the mouse: real buttons, with names', async () => {
  await withPanel({ status: CODING, tier: 'key' }, async ({ page }) => {
    const all = controls(page).descendants().filter((e) => e.tag === 'button');
    assert.deepEqual(all.map((b) => b.textContent), ['Speed', 'Quality', 'Auto', 'Vulkan', 'CUDA', 'Unlimited turns: on']);
    for (const button of all) assert.ok(button.getAttribute('aria-pressed') !== null, `${button.textContent} says whether it is chosen`);
    assert.deepEqual(controls(page).descendants().filter((e) => e.getAttribute('role') === 'group').map((g) => g.getAttribute('aria-label')),
      ['Speed or quality', 'Graphics API']);
  });
});

test('createPage still builds when the extension has said nothing (a page from an older host)', async () => {
  await withPanel({}, async ({ view }) => {
    const page = createPage(view.webview.html, { toHost: () => {} });
    page.receive({ type: 'restoreHistory', session: null, history: [], run: { busy: false, events: [] } });
    assert.deepEqual(page.errors, []);
  });
});
