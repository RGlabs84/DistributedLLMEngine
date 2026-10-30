'use strict';
/**
 * The sidebar chat that "stops running, doesn't continue" (the owner, 2026-10-28), and what mended it in 1.5.0.
 * The causes were found by reading VS Code's source and by running the page's script against fakes (the planner's
 * report, sidebar-stalls.md); these tests run the same things through the real SidebarProvider and the real page script:
 *
 * A1. Hiding the view threw the page away, and nothing sent it the task again: Send showed, Stop and the progress were
 *     gone, the answer, the tool cards and the Continue button never arrived. The page is kept now, and the whole chat
 *     (a task in progress too) is sent again when the view is shown, or when VS Code makes the page anew.
 * A2. Enter, Send or Continue during a task aborted it (AgentLoop.run starts by aborting the live run) and freed the
 *     page while the new task ran. A message never starts over a running task now.
 * A3. A throw out of the run left the page "running" for ever.
 * A4. An error read "Error: [object Object]" (an Error does not survive the trip to the page).
 * A5. The page had no handler for `cleared` and `memoryUndone`.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const stub = require('./helpers/vscode-stub');
const { createPage, openView, deferred, until, toolCall, scriptedClient, say, waiting } = require('./helpers/sidebar-page');
const { ToolRegistry } = require('../dist/agent/tools');
const { SidebarProvider, describeError } = require('../dist/views/sidebarProvider');
const { ServerRefusal } = require('../dist/client/dlneClient');

// The agent reads ~/.dlne/memory.md: a home of its own keeps these runs out of the real one.
process.env.HOME = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-sidebar-run-home-'));
const ROOT = path.join(__dirname, '..');
const STATUS = { state: 'idle', gpu: { name: 'GPU', api: 'vulkan', vramTotalGb: 8 }, vramContributedGb: 0, cpuPool: false };

/** The real provider over a scripted client, its workspace and storage in temp folders, shown in a view. */
async function withSidebar(steps, options, body) {
  const { retain = true, settings = {}, mount = true } = options;
  stub.reset();
  Object.assign(stub.state.settings, settings);
  ToolRegistry.resetApprovals();
  const realApprover = ToolRegistry.approver;
  const realExecute = ToolRegistry.executeTool;
  ToolRegistry.approver = async () => 'once';
  const workspace = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-sidebar-run-ws-'));
  fs.writeFileSync(path.join(workspace, 'notes.txt'), 'hello\n');
  stub.vscode.workspace.workspaceFolders = [{ uri: { fsPath: workspace } }];
  const storage = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-sidebar-run-store-'));
  const memento = new Map();
  const context = {
    extensionUri: { fsPath: ROOT },
    storageUri: { fsPath: storage },
    globalStorageUri: { fsPath: path.join(storage, 'global') },
    subscriptions: [],
    workspaceState: { get: (k) => memento.get(k), update: async (k, v) => { memento.set(k, v); } },
  };
  const client = scriptedClient(steps);
  const provider = new SidebarProvider(context.extensionUri, client, { onStatusChange() {}, getStatus: () => STATUS }, context);
  const view = openView(provider, { retain });
  if (mount) view.mount();
  try {
    return await body({ provider, client, view, workspace, context });
  } finally {
    ToolRegistry.approver = realApprover;
    ToolRegistry.executeTool = realExecute;
    stub.vscode.workspace.workspaceFolders = undefined;
    fs.rmSync(workspace, { recursive: true, force: true });
    fs.rmSync(storage, { recursive: true, force: true });
  }
}

/** A task of three steps: it looks around, waits on the model at `gate`, writes a file, then answers. */
function buildSteps(gate) {
  return [
    say('Let me look at the project. ', [toolCall('list_directory', { path: '.' })]),
    waiting(gate, say('Now the file. ', [toolCall('write_file', { path: 'hello.txt', content: 'hi\n' })]),
      ({ onLoadingProgress }) => onLoadingProgress?.('⏳ Waiting for the swarm to start your answer (3 s).')),
    say('All done: hello.txt is created.'),
  ];
}

const send = (view, text) => { view.page.type(text); view.page.pressEnter(); };
const count = (view, type) => view.fromPage.filter((m) => m.type === type).length;
/** What a person looking at the page sees: the chat and the controls (with this run's folder name taken out). */
const seen = (view, workspace) => ({ chat: view.page.chat().replaceAll(workspace, '<ws>'), controls: view.page.controls() });

// ---- A1: hiding the view ---------------------------------------------------------------------------------------

/**
 * One task, with the view treated as `plan` says, and what the page showed in the middle (the task waiting on the
 * model) and at the end. Every plan must show what a page that was never away shows.
 *  - `awayThroughEnd`: hidden at the model's wait, and shown again once the task has ended;
 *  - `awayAtWait`: hidden and shown again at the model's wait, the task ending in view;
 *  - `moveAtWait`: the view is moved to another side bar (a new view, resolved again) at the wait;
 *  - `retain: false`: the view does not keep its page while hidden, so VS Code makes a new one on show.
 */
async function runPlan(plan = {}) {
  const gate = deferred();
  return withSidebar(buildSteps(gate), { retain: plan.retain ?? true }, async (sb) => {
    send(sb.view, 'build it');
    await until(() => sb.client.calls.length === 2, 'the task to reach its second step');
    let view = sb.view;
    let middle = null;
    if (plan.awayThroughEnd) {
      view.hide();
    } else {
      if (plan.awayAtWait) {
        view.hide();
        view.show();
      }
      if (plan.moveAtWait) {
        sb.view.dispose();
        view = openView(sb.provider);
        view.mount();
      }
      middle = seen(view, sb.workspace);
    }
    gate.release();
    await Promise.all([sb.view.idle(), view.idle()]);       // the task's message was posted from the first view
    if (plan.awayThroughEnd) view.show();
    return { middle, end: seen(view, sb.workspace), view };
  });
}

test('A1: a task seen again after the view was away shows what a page that never left shows, in the middle of it and at the end', async () => {
  const never = await runPlan();
  // Not vacuous: the page that stayed shows the whole task.
  assert.match(never.middle.chat, /user "build it"/);
  assert.match(never.middle.chat, /tool-pill "✅ list_directory:\s+notes\.txt"/);
  assert.match(never.middle.chat, /tool-pill "⏳ Waiting for the swarm to start your answer \(3 s\)\."/);
  assert.deepEqual(never.middle.controls, { send: 'hidden', stop: 'shown', stopLabel: 'Stop', stopDisabled: false, hint: 'hidden', input: '', queue: 'hidden' });
  assert.match(never.end.chat, /file-path "📄 hello\.txt"/);
  assert.match(never.end.chat, /assistant-body html:<p>All done: hello\.txt is created\.<\/p>/);
  assert.equal(never.end.controls.send, 'shown');
  assert.equal(never.end.controls.stop, 'hidden');

  const plans = {
    'hidden while the task ended, the page kept': { awayThroughEnd: true },
    'hidden while the task ended, the page thrown away': { awayThroughEnd: true, retain: false },
    'hidden and shown again in the middle, the page kept': { awayAtWait: true },
    'hidden and shown again in the middle, the page made anew': { awayAtWait: true, retain: false },
    'the view resolved again in the middle (moved to another side bar)': { moveAtWait: true },
  };
  for (const [name, plan] of Object.entries(plans)) {
    const result = await runPlan(plan);
    if (result.middle) assert.deepEqual(result.middle, never.middle, `${name}: in the middle of the task`);
    assert.deepEqual(result.end, never.end, `${name}: at the end of the task`);
    assert.deepEqual(result.view.page.errors, [], name);
  }
});

test('A1: a task that was running while the view was hidden is not left "running": Send is back, Stop is gone, the answer is there', async () => {
  const gate = deferred();
  await withSidebar(buildSteps(gate), {}, async (sb) => {
    send(sb.view, 'build it');
    await until(() => sb.client.calls.length === 2, 'the second step');
    assert.equal(sb.view.page.controls().stop, 'shown');
    sb.view.hide();
    gate.release();
    await sb.view.idle();
    // What the page holds now is what it had when it was hidden: nothing was delivered to it since.
    assert.equal(sb.view.page.controls().stop, 'shown', 'a hidden page is told nothing');
    assert.doesNotMatch(sb.view.page.chat(), /All done/);
    sb.view.show();
    assert.deepEqual(
      { send: sb.view.page.controls().send, stop: sb.view.page.controls().stop },
      { send: 'shown', stop: 'hidden' });
    assert.match(sb.view.page.chat(), /All done: hello\.txt is created\./);
  });
});

/**
 * Step `n`'s read of notes.txt: another line each time. The same read ten times running is what the loop check stops
 * (loopGuard.ts), and these tests are about the turn limit, which is reached by ten different steps.
 */
const readLine = (n) => toolCall('read_file', { path: 'notes.txt', start_line: n + 1, end_line: n + 1 });

test('A1: a Continue button that came while the view was hidden is there when it is shown', async () => {
  // Ten steps reach the limit set below; the report that follows asks for no tools; a later request carries on.
  const gate = deferred();
  const pausing = async (ctx) => {
    if (ctx.index === 4) return waiting(gate, say('', [readLine(ctx.index)]))(ctx);
    if (ctx.index === 10) return say('Report: I read notes.txt ten times.')(ctx);
    if (ctx.index > 10) return say('Carrying on: done.')(ctx);
    return say('', [readLine(ctx.index)])(ctx);
  };
  await withSidebar([pausing], { settings: { 'dlne.agent.maxTurns': 10 } }, async (sb) => {
    send(sb.view, 'read it a lot');
    await until(() => sb.client.calls.length === 5, 'the fifth step');
    sb.view.hide();
    gate.release();
    await sb.view.idle();
    sb.view.show();
    assert.equal(sb.view.page.find('continue-btn').length, 1, 'the Continue button is shown');
    assert.match(sb.view.page.chat(), /Paused after 10 steps/);
    assert.equal(sb.view.page.controls().send, 'shown');

    // And it works: the click sends "continue" without touching what is typed in the box.
    sb.view.page.type('a draft for later');
    sb.view.page.find('continue-btn')[0].click();
    assert.equal(sb.view.fromPage.filter((m) => m.type === 'sendMessage').at(-1).text, 'continue');
    assert.equal(sb.view.page.controls().input, 'a draft for later', 'Continue does not use the box');
    await sb.view.idle();
    assert.match(sb.view.page.chat(), /Carrying on: done\./);
    assert.equal(sb.view.page.find('continue-btn').length, 0, 'the old Continue button is gone');
  });
});

test('A1: showing the view with nothing missed leaves the page as it is, and only sends the small state again', async () => {
  await withSidebar([say('Hello. It is nothing much.')], {}, async (sb) => {
    send(sb.view, 'hi');
    await sb.view.idle();
    const page = sb.view.page;
    const firstBubble = page.el('chat-container').children[0];
    const before = page.chat();
    const redraws = sb.view.posted('restoreHistory').length;
    sb.view.hide();
    sb.view.show();
    assert.equal(sb.view.posted('restoreHistory').length, redraws, 'the chat is not sent again: nothing changed while it was away');
    assert.equal(page.el('chat-container').children[0], firstBubble, 'and the page did not redraw it (its scroll and open drawers stay)');
    assert.equal(page.chat(), before);
    // The small state may have changed unseen, so it is sent again.
    for (const type of ['workerStatus', 'modelLine', 'autoApprove', 'queue']) {
      assert.ok(sb.view.posted(type).length >= 1, `${type} is sent on show`);
    }
  });
});

test('A1: a page VS Code makes anew asks for the chat, and gets the last task back exactly as it was drawn live', async () => {
  const gate = deferred();
  gate.release();
  await withSidebar(buildSteps(gate), {}, async (sb) => {
    assert.equal(sb.view.fromPage[0].type, 'ready', 'a page that starts asks the extension for the chat');
    send(sb.view, 'build it');
    await sb.view.idle();
    const live = sb.view.page.chat();
    sb.view.reloadPage();               // Developer: Reload Webviews: no visibility change, no resolve
    assert.equal(sb.view.page.chat(), live);
    assert.equal(sb.view.page.controls().send, 'shown');
  });
});

test('A1: older tasks come back from the saved chat and the latest one from what the page was told', async () => {
  await withSidebar([say('First answer.'), say('Second answer.')], {}, async (sb) => {
    send(sb.view, 'one');
    await sb.view.idle();
    send(sb.view, 'two');
    await sb.view.idle();
    sb.view.reloadPage();
    const restored = sb.view.posted('restoreHistory').at(-1);
    assert.deepEqual(restored.history.map((h) => h.text), ['one', 'First answer.'], 'the first task is the saved chat');
    assert.deepEqual(restored.run.events.map((e) => e.type), ['appendUserMessage', 'contentDelta', 'turnComplete'], 'the second is played back');
    const chat = sb.view.page.chat();
    for (const words of ['"one"', 'First answer.', '"two"', 'Second answer.']) assert.ok(chat.includes(words), words);
  });
});

test('A1: the chat is sent again in few, light messages: a run of text is one, whole files and arguments stay home', async () => {
  const words = Array.from({ length: 40 }, (_, i) => `word${i} `);
  const big = 'x'.repeat(5000);
  const talkative = async ({ onDelta }) => {
    words.forEach((w) => onDelta(w));
    return { content: words.join(''), toolCalls: [toolCall('write_file', { path: 'big.txt', content: big })], finishReason: 'tool_calls' };
  };
  await withSidebar([talkative, say('Written.')], {}, async (sb) => {
    send(sb.view, 'write it');
    await sb.view.idle();
    sb.view.toHost({ type: 'ready' });
    await sb.view.idle();
    const sync = sb.view.posted('restoreHistory').at(-1);
    assert.deepEqual(sync.run.events.map((e) => e.type), ['appendUserMessage', 'contentDelta', 'toolStart', 'toolEnd', 'contentDelta', 'turnComplete']);
    assert.equal(sync.run.events[1].delta, words.join(''), 'forty words, one message');
    const text = JSON.stringify(sync);
    assert.doesNotMatch(text, /originalContent|modifiedContent/, 'the page draws the changed lines, not the two whole files');
    assert.deepEqual(sync.run.events[2], { type: 'toolStart', toolName: 'write_file' }, 'the arguments (the whole file) stay home');
    assert.ok(text.length < 4 * big.length, `${text.length} characters for a 5,000 character file`);
    // What the host keeps is whole: the saved chat still holds both files for anything that needs them.
    const kept = sb.provider.conversationHistory.flatMap((m) => m.toolCalls || []).find((c) => c.name === 'write_file');
    assert.equal(kept.args.content, big);
    assert.ok(kept.result.fileDiff.modifiedContent);
  });
});

test('A1: a view closed in the middle of a task does not end the task, and the next view is shown it', async () => {
  const gate = deferred();
  await withSidebar(buildSteps(gate), {}, async (sb) => {
    send(sb.view, 'build it');
    await until(() => sb.client.calls.length === 2, 'the second step');
    sb.view.failPosts = true;          // the view is gone, and using it throws, as the typings say
    gate.release();
    await sb.view.idle();              // the task ends all the same
    assert.match(sb.provider.conversationHistory.at(-1).text, /All done: hello\.txt is created\./);
    sb.view.failPosts = false;
    sb.view.dispose();

    const next = openView(sb.provider);
    next.mount();
    assert.match(next.page.chat(), /All done: hello\.txt is created\./);
    assert.equal(next.page.controls().send, 'shown');
    assert.match(next.page.chat(), /file-card/);
  });
});

test('A1: the old view being disposed after a new one was resolved does not cut the new one off', async () => {
  await withSidebar([say('The answer.')], {}, async (sb) => {
    const second = openView(sb.provider);
    second.mount();
    sb.view.dispose();                 // VS Code announces the old view's end after the new view exists
    send(second, 'go');
    await second.idle();
    assert.match(second.page.chat(), /The answer\./, 'the new view gets the task');
    assert.equal(second.posted('turnComplete').length, 1);
  });
});

test('A1: the view is registered to keep its page while hidden', () => {
  const source = fs.readFileSync(path.join(ROOT, 'src', 'extension.ts'), 'utf8');
  assert.match(source, /registerWebviewViewProvider\('dlne\.chatView', sidebarProvider, \{\s*webviewOptions: \{ retainContextWhenHidden: true \},?\s*\}\)/);
});

// ---- A2: a message while a task runs ---------------------------------------------------------------------------

test('A2: Enter and Send during a task send nothing, keep the words, and never stop the task', async () => {
  const gate = deferred();
  await withSidebar(buildSteps(gate), {}, async (sb) => {
    send(sb.view, 'build it');
    await until(() => sb.client.calls.length === 2, 'the second step');
    const page = sb.view.page;
    page.type('continue');
    page.pressEnter();
    page.clickSend();                  // hidden while the task runs, but a click on it still reaches its listener
    assert.equal(count(sb.view, 'sendMessage'), 1, 'only the first message was sent');
    assert.equal(page.controls().input, 'continue', 'the words stay in the box');
    assert.equal(page.controls().hint, 'shown', 'and the page says why');
    assert.equal(sb.client.calls[1].signal.aborted, false, 'the task was not stopped');

    gate.release();
    await sb.view.idle();
    assert.equal(sb.client.calls.length, 3, 'one task of three steps, nothing started over it');
    assert.match(page.chat(), /All done: hello\.txt is created\./);
    assert.equal(page.controls().hint, 'hidden');
    assert.equal(page.controls().input, 'continue', 'the words are still there to send now');
    const users = sb.provider.agentLoop.messages.filter((m) => m.role === 'user').map((m) => m.content);
    assert.deepEqual(users.filter((c) => c === 'build it' || c === 'continue'), ['build it']);

    page.pressEnter();                 // free now
    assert.equal(count(sb.view, 'sendMessage'), 2);
    assert.equal(page.controls().input, '');
    await sb.view.idle();
  });
});

test('A2: a message that reaches the host while a task runs is not started, and goes back to the page with the task', async () => {
  const gate = deferred();
  await withSidebar(buildSteps(gate), {}, async (sb) => {
    send(sb.view, 'build it');
    await until(() => sb.client.calls.length === 2, 'the second step');
    // A page that lost track of the task (it was away) would let this through; the host must not.
    sb.view.toHost({ type: 'sendMessage', text: 'continue' });
    await until(() => sb.view.posted('refused').length === 1, 'the refusal');
    assert.equal(sb.view.posted('refused')[0].text, 'continue');
    assert.equal(sb.client.calls[1].signal.aborted, false, 'the live task was not stopped');
    assert.equal(sb.provider.conversationHistory.length, 2, 'no second task was begun');
    const resync = sb.view.posted('restoreHistory').at(-1);
    assert.equal(resync.run.busy, true, 'the page is shown the task that is running');
    assert.equal(sb.view.page.controls().input, 'continue', 'its words come back');
    assert.equal(sb.view.page.controls().hint, 'shown');

    gate.release();
    await sb.view.idle();
    assert.equal(sb.client.calls.length, 3);
    assert.equal(count(sb.view, 'sendMessage'), 2, 'the stray message was posted, and refused');
    assert.equal(sb.view.posted('appendUserMessage').length, 1);
  });
});

test('A2: a page made anew in the middle of a task knows it is running, and keeps Enter from starting another', async () => {
  const gate = deferred();
  await withSidebar(buildSteps(gate), {}, async (sb) => {
    send(sb.view, 'build it');
    await until(() => sb.client.calls.length === 2, 'the second step');
    sb.view.reloadPage();              // a page made anew after the task began: it asked, and was told
    assert.equal(sb.view.page.controls().stop, 'shown');
    sb.view.page.type('and also this');
    sb.view.page.pressEnter();
    assert.equal(count(sb.view, 'sendMessage'), 1, 'it knows a task is running');
    gate.release();
    await sb.view.idle();
  });
});

test('A2: a Continue button from an earlier task goes away when the next task begins', async () => {
  const pausing = async (ctx) => {
    if (ctx.index === 10) return say('Report: I read notes.txt ten times.')(ctx);
    if (ctx.index > 10) return say('Different answer.')(ctx);
    return say('', [readLine(ctx.index)])(ctx);
  };
  await withSidebar([pausing], { settings: { 'dlne.agent.maxTurns': 10 } }, async (sb) => {
    send(sb.view, 'read it a lot');
    await sb.view.idle();
    assert.equal(sb.view.page.find('continue-btn').length, 1);
    send(sb.view, 'do something else instead');
    assert.equal(sb.view.page.find('continue-btn').length, 0, 'it cannot be pressed over the new task');
    await sb.view.idle();
  });
});

// ---- Stop and Clear --------------------------------------------------------------------------------------------

test('Stop ends the task for real: the page says Stopping until it has, then it is free', async () => {
  const never = deferred();
  await withSidebar(buildSteps(never), {}, async (sb) => {
    send(sb.view, 'build it');
    await until(() => sb.client.calls.length === 2, 'the second step');
    sb.view.page.clickStop();
    assert.deepEqual(
      { send: sb.view.page.controls().send, stop: sb.view.page.controls().stop, label: sb.view.page.controls().stopLabel, disabled: sb.view.page.controls().stopDisabled },
      { send: 'hidden', stop: 'shown', label: 'Stopping...', disabled: true });
    await sb.view.idle();
    assert.deepEqual(sb.view.page.controls(), { send: 'shown', stop: 'hidden', stopLabel: 'Stop', stopDisabled: false, hint: 'hidden', input: '', queue: 'hidden' });
    assert.deepEqual(sb.view.sent.map((s) => s.message.type).filter((t) => ['stopping', 'taskAborted', 'turnComplete'].includes(t)),
      ['stopping', 'turnComplete'], 'the page hears the end from the task itself, once');
    send(sb.view, 'try again');
    assert.equal(count(sb.view, 'sendMessage'), 2, 'and it is free to send');
    await sb.view.idle();
  });
});

test('Stop on a page that thinks a task runs, when none does, puts it back to idle', async () => {
  await withSidebar([say('x')], {}, async (sb) => {
    sb.view.page.receive({ type: 'appendUserMessage', text: 'a task the extension never began' });
    assert.equal(sb.view.page.controls().stop, 'shown');
    sb.view.page.clickStop();
    await sb.view.idle();
    assert.equal(sb.view.page.controls().send, 'shown');
    assert.equal(sb.view.page.controls().stop, 'hidden');
    assert.equal(sb.view.posted('taskAborted').length, 1);
  });
});

test('New chat during a task (1.5.3): the task goes on in its own session, the new chat stays clean, and the list says so', async () => {
  const gate = deferred();
  // This model does not answer at once: its words arrive after the New chat, as a slow reply's would.
  const slow = async (ctx) => {
    await gate.promise;
    ctx.onDelta('late words');
    return { content: 'late words', toolCalls: [], finishReason: 'stop' };
  };
  await withSidebar([say('Looking. ', [toolCall('list_directory', { path: '.' })]), slow], {}, async (sb) => {
    send(sb.view, 'build it');
    await until(() => sb.client.calls.length === 2, 'the second step');
    const first = sb.view.posted('sessions').at(-1).active;
    sb.view.page.clickClear();                                   // the button says "New chat" now
    await until(() => sb.view.posted('restoreHistory').some((m) => m.session.active !== first), 'the new chat');
    const fresh = sb.view.posted('restoreHistory').at(-1);
    assert.equal(sb.view.page.chat(), '', 'the new chat is empty');
    assert.equal(sb.view.page.controls().send, 'shown', 'and free to use: the task runs in the other session');
    assert.equal(sb.view.page.el('session-title').textContent, 'New chat');
    assert.deepEqual(fresh.session.list.map((r) => [r.title, r.running, r.active]),
      [['New chat', false, true], ['build it', true, false]]);

    gate.release();
    await sb.view.idle();
    assert.equal(sb.view.page.chat(), '', 'what the task said is not drawn in the new chat');
    assert.equal(sb.view.posted('sessions').at(-1).list.find((r) => r.id === first).running, false, 'the list says it ended');

    // Back to it from the list: its row, clicked on the page.
    sb.view.page.el('sessions-btn').click();
    assert.equal(sb.view.page.el('session-list').style.display, 'flex');
    assert.equal(sb.view.page.el('chat-container').style.display, 'none');
    const row = sb.view.page.el('session-list').children.find((r) => r.children[0].textContent === 'build it');
    row.click();
    await sb.view.idle();
    assert.match(sb.view.page.chat(), /late words/, 'the task as it ended');
    assert.equal(sb.view.page.el('session-title').textContent, 'build it');
    assert.equal(sb.view.page.el('chat-container').style.display, 'flex', 'the chat is shown again');
    assert.equal(sb.provider.conversationHistory.length, 2);
    assert.deepEqual(sb.view.page.errors, []);
  });
});

test('the chat follows the answer only while it is at the bottom; scrolled up, it stays put until Latest (1.5.3)', async () => {
  await withSidebar([say('x')], {}, async (sb) => {
    const page = createPage(sb.view.webview.html, { toHost: () => {} });
    const chat = page.el('chat-container');
    const scrolled = (top) => { chat.scrollTop = top; chat.dispatch('scroll'); };
    chat.clientHeight = 200;
    chat.scrollHeight = 1000;
    page.receive({ type: 'appendUserMessage', text: 'go' });
    assert.equal(chat.scrollTop, 1000, 'your own message is shown');

    scrolled(300);                                               // the owner scrolls up to read
    chat.scrollHeight = 1400;
    page.receive({ type: 'contentDelta', delta: 'more words' });
    page.receive({ type: 'toolStart', toolName: 'read_file', args: {} });
    page.flushTimers();
    assert.equal(chat.scrollTop, 300, 'the chat does not fight the reader');
    assert.equal(page.el('jump-btn').style.display, 'inline-block', 'Latest is offered');

    page.el('jump-btn').click();
    assert.equal(chat.scrollTop, 1400);
    assert.equal(page.el('jump-btn').style.display, 'none');
    chat.scrollHeight = 1800;
    page.receive({ type: 'loadingProgress', statusText: 'working' });
    assert.equal(chat.scrollTop, 1800, 'at the bottom again, it follows again');

    scrolled(500);
    scrolled(1800 - 200 - 10);                                   // scrolled back down by hand: following again
    chat.scrollHeight = 2000;
    page.receive({ type: 'loadingProgress', statusText: 'still working' });
    assert.equal(chat.scrollTop, 2000);
    assert.deepEqual(page.errors, []);
  });
});

test('Stop and Send act on the chat shown: a task in another session is not touched', async () => {
  const gate = deferred();
  const slow = async (ctx) => {
    await gate.promise;
    ctx.onDelta('first done');
    return { content: 'first done', toolCalls: [], finishReason: 'stop' };
  };
  await withSidebar([slow, say('second done')], {}, async (sb) => {
    send(sb.view, 'first task');
    await until(() => sb.client.calls.length === 1, 'the first task to ask the swarm');
    const first = sb.view.posted('sessions').at(-1).active;
    sb.view.page.clickClear();
    // (Not view.idle(): that waits for the first task too, which is still running, as it should.)
    await until(() => sb.view.posted('restoreHistory').some((m) => m.session.active !== first), 'the new chat');
    send(sb.view, 'second task');
    await until(() => sb.view.posted('turnComplete').length === 1, 'the second task to end');
    assert.match(sb.view.page.chat(), /second done/);
    sb.view.page.clickStop();                                   // nothing runs here: the first task is left alone
    gate.release();
    await sb.view.idle();
    assert.equal(sb.view.posted('sessions').at(-1).list.find((r) => r.id === first).title, 'first task');
    await sb.view.toHost({ type: 'openSession', id: first });
    assert.match(sb.view.page.chat(), /first done/, 'it ran to its end');
  });
});

// ---- A3: a throw -----------------------------------------------------------------------------------------------

test('A3: a tool call that throws is an error the model reads, and the task carries on', async () => {
  // Since the 1.5.0 merge the agent loop turns a throwing tool into an error result (the core builder's fix of the
  // same hang), so the task is not ended: the model reads the error and its next step finishes the task.
  await withSidebar([say('Reading it. ', [toolCall('read_file', { path: 'notes.txt' })]), say('Done after all.')], {}, async (sb) => {
    ToolRegistry.executeTool = async () => { throw new TypeError('targetPath.trim is not a function'); };
    send(sb.view, 'read it');
    await sb.view.idle();
    assert.match(sb.view.page.chat(), /Done after all\./);
    assert.deepEqual(sb.view.page.controls(), { send: 'shown', stop: 'hidden', stopLabel: 'Stop', stopDisabled: false, hint: 'hidden', input: '', queue: 'hidden' });
    assert.equal(sb.view.posted('turnComplete').length, 1, 'the page was told the task ended');
  });
});

/** Make the agent loop itself throw (an exception the loop does not turn into a result) for the rest of `body`. */
async function withRunThrowing(error, body) {
  const { AgentLoop } = require('../dist/agent/agentLoop');
  const run = AgentLoop.prototype.run;
  AgentLoop.prototype.run = async function () { throw error; };
  try {
    await body();
  } finally {
    AgentLoop.prototype.run = run;
  }
}

test('A3: a task that throws ends with the error in words, and the chat carries on', async () => {
  await withSidebar([say('Done after all.')], {}, async (sb) => {
    await withRunThrowing(new TypeError('targetPath.trim is not a function'), async () => {
      send(sb.view, 'read it');
      await sb.view.idle();
    });
    const lines = sb.view.page.find('message').map((m) => m.textContent);
    const error = lines.find((t) => /unexpected error/.test(t));
    assert.equal(error, 'The task stopped because of an unexpected error: targetPath.trim is not a function. Everything so far is kept: say continue to try again.');
    assert.deepEqual(sb.view.page.controls(), { send: 'shown', stop: 'hidden', stopLabel: 'Stop', stopDisabled: false, hint: 'hidden', input: '', queue: 'hidden' });
    assert.equal(sb.view.posted('turnComplete').length, 1, 'the page was told the task ended');
    // The host is free: the next message runs.
    send(sb.view, 'continue');
    await sb.view.idle();
    assert.match(sb.view.page.chat(), /Done after all\./);
  });
});

test('A3: the task that threw is still there for a page that was away', async () => {
  await withSidebar([say('unused')], {}, async (sb) => {
    await withRunThrowing(new Error('the disk fell off. '), async () => {   // (its own full stop is not doubled)
      send(sb.view, 'read it');
      await sb.view.idle();
    });
    const live = sb.view.page.chat();
    assert.match(live, /unexpected error: the disk fell off\. Everything so far is kept/);
    sb.view.reloadPage();
    assert.equal(sb.view.page.chat(), live, 'the error line comes back with the task');
  });
});

test('A3: a tool call with a path that is not text, with Auto off, ends the task and frees the page whichever way the tool handles it', async () => {
  // (ToolRegistry.checkApproval used to throw "targetPath.trim is not a function" for this.)
  await withSidebar([say('Reading. ', [{ id: 'bad', name: 'read_file', arguments: '{"path": 5}' }]), say('Gave up.')], {}, async (sb) => {
    send(sb.view, 'read it');
    await sb.view.idle();
    assert.deepEqual({ send: sb.view.page.controls().send, stop: sb.view.page.controls().stop }, { send: 'shown', stop: 'hidden' });
    assert.equal(sb.view.posted('turnComplete').length, 1);
    send(sb.view, 'again');
    assert.equal(count(sb.view, 'sendMessage'), 2, 'the host takes the next message');
    await sb.view.idle();
  });
});

test('A3: a throw before the first step still ends the turn', async () => {
  await withSidebar([say('Back to normal.')], {}, async (sb) => {
    // The agent cannot even start: its run rejects at once.
    const loop = sb.provider.agentLoop;
    const realRun = loop.run;
    loop.run = async () => { throw new RangeError('out of ideas'); };
    send(sb.view, 'go');
    await sb.view.idle();
    loop.run = realRun;
    assert.match(sb.view.page.chat(), /unexpected error: out of ideas\./);
    assert.equal(sb.view.page.controls().send, 'shown');
    send(sb.view, 'go again');
    await sb.view.idle();
    assert.match(sb.view.page.chat(), /Back to normal\./);
  });
});

test('A3: a message the page cannot draw does not leave it stuck', async () => {
  await withSidebar([say('x')], {}, async (sb) => {
    const page = sb.view.page;
    page.receive({ type: 'appendUserMessage', text: 'a task' });
    page.receive({ type: 'toolEnd', result: { name: 'odd' } });          // no output: the page cannot draw this
    assert.equal(page.errors.length, 1, 'the page noticed');
    page.receive({ type: 'turnComplete', paused: false });
    assert.equal(page.controls().send, 'shown');
  });
});

// ---- A4: the words of an error ---------------------------------------------------------------------------------

test('A4: an error from the swarm is shown in its own words, never as "[object Object]"', async () => {
  const refused = () => { throw new ServerRefusal(500, 'Inference engine crashed during startup.'); };
  await withSidebar([refused], {}, async (sb) => {
    send(sb.view, 'build it');
    await sb.view.idle();
    const lines = sb.view.page.find('message').map((m) => m.textContent);
    assert.ok(lines.includes('Error: Inference engine crashed during startup. (HTTP 500)'), lines.join(' | '));
    assert.ok(lines.every((t) => !t.includes('[object Object]')));
    assert.match(sb.view.page.chat(), /Error connecting to DLNE cluster: Inference engine crashed during startup\./);
    assert.equal(sb.view.page.controls().send, 'shown');
  });
});

test('A4: what an error says: its message, its HTTP status, a string, an object with a message, and at worst a plain sentence', () => {
  assert.equal(describeError(new Error('boom')), 'boom');
  assert.equal(describeError(new ServerRefusal(503, 'The swarm is busy.')), 'The swarm is busy. (HTTP 503)');
  assert.equal(describeError(new ServerRefusal(429, 'Too many requests (429).')), 'Too many requests (429).', 'a status already in the words is not said twice');
  assert.equal(describeError(new TypeError()), 'TypeError');
  assert.equal(describeError('plain words'), 'plain words');
  assert.equal(describeError({ message: 'from an object' }), 'from an object');
  assert.equal(describeError({ code: 7 }), '{"code":7}');
  for (const nothing of [undefined, null, {}, '', '  ', 42]) {
    assert.equal(describeError(nothing), 'something went wrong, and nothing more is known', String(nothing));
  }
  const circular = {};
  circular.self = circular;
  assert.equal(describeError(circular), 'something went wrong, and nothing more is known');
  for (const value of [new Error('x'), { a: 1 }, 'y', null]) assert.doesNotMatch(describeError(value), /\[object Object\]/);
});

test('A4: the page shows the words it is sent, and a plain sentence for an error line that carries none', async () => {
  await withSidebar([say('x')], {}, async (sb) => {
    const page = sb.view.page;
    page.receive({ type: 'error', error: 'Error: the words' });
    page.receive({ type: 'error', error: {} });                           // what an old host's Error became on the way
    const lines = page.find('message').map((m) => m.textContent);
    assert.deepEqual(lines.slice(-2), ['Error: the words', 'Error: something went wrong.']);
  });
});

// ---- A5: the two answers the page did not handle ---------------------------------------------------------------

test('A5: Undo reports on its own button, and a New chat leaves nothing of the old one on the page', async () => {
  await withSidebar([say('x')], {}, async (sb) => {
    const page = sb.view.page;
    // An Undo the extension refuses (it is not a change DLNE made): the button says so.
    page.receive({ type: 'appendUserMessage', text: 'remember it' });
    page.receive({ type: 'toolEnd', result: { name: 'remember', output: 'Saved a fact.', memoryChange: { nothing: 'real' } } });
    page.receive({ type: 'turnComplete' });
    const [undo] = page.find('undo-btn');
    assert.equal(undo.textContent, '↩ Undo');
    undo.click();
    assert.equal(undo.disabled, true);
    await sb.view.idle();
    assert.equal(undo.textContent, 'Not undone');
    assert.equal(undo.title, 'Cannot undo: that is not a change DLNE made.');
    assert.equal(stub.state.warningMessages.length, 1, 'the toast is still shown');

    // A page from before 1.5.3 still understands `cleared`: it empties itself.
    page.receive({ type: 'cleared' });
    assert.equal(page.chat(), '');
    assert.equal(page.find('undo-btn').length, 0);
  });
});

test('A5: two Undos in a row are answered in turn, each on its own button', async () => {
  await withSidebar([say('x')], {}, async (sb) => {
    // A page on its own (the real extension answers at once, so the order could not be told apart).
    const posted = [];
    const page = createPage(sb.view.webview.html, { toHost: (m) => { posted.push(m); } });
    page.receive({ type: 'appendUserMessage', text: 'remember two things' });
    page.receive({ type: 'toolEnd', result: { name: 'remember', output: 'Saved a.', memoryChange: { n: 1 } } });
    page.receive({ type: 'toolEnd', result: { name: 'remember', output: 'Saved b.', memoryChange: { n: 2 } } });
    const [first, second] = page.find('undo-btn');
    first.click();
    second.click();
    assert.deepEqual(posted.filter((m) => m.type === 'undoMemory').map((m) => m.change.n), [1, 2]);
    page.receive({ type: 'memoryUndone', ok: true, message: 'Undone: "a" is no longer remembered.' });
    page.receive({ type: 'memoryUndone', ok: false, message: 'Cannot undo: b was changed since.' });
    assert.deepEqual([first.textContent, second.textContent], ['✓ Undone', 'Not undone']);
    assert.equal(second.title, 'Cannot undo: b was changed since.');
    page.receive({ type: 'memoryUndone', ok: true, message: 'a stray answer' });   // none is waiting: nothing happens
    assert.deepEqual(page.errors, []);
  });
});
