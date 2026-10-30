'use strict';
/**
 * The agent's working memory (docs/agent-memory.md sections 1, 4, 5, 6 and 8), driven through AgentLoop.run with a
 * scripted client: what each request holds, when and how the history is compacted, what survives it, what a
 * cut-off reply or a Stop does, the pause report and Continue, and the saved conversation.
 */
const test = require('node:test');
const assert = require('node:assert/strict');

require('./helpers/vscode-stub');
const {
  AgentLoop, AGENT_SYSTEM_PROMPT, COMPACT_REQUEST, CUT_OFF_NOTICE, WORKING_MEMORY_OPEN, WORKING_MEMORY_CLOSE,
  DEFAULT_CONTEXT_TOKENS, renderUserWords, pausePrompt, withoutThinking,
} = require('../dist/agent/agentLoop');
const { ToolRegistry } = require('../dist/agent/tools');
const { AGENT_MAX_TOKENS } = require('../dist/client/dlneClient');

const realExecute = ToolRegistry.executeTool;
let executed;
test.beforeEach(() => {
  executed = [];
  ToolRegistry.executeTool = async (name, args) => {
    executed.push({ name, args });
    if (name === 'read_file') return { name, output: `1: ${'y'.repeat(args.size ?? 40)}\n2: end of ${args.path}` };
    if (name === 'write_file') return { name, output: `Successfully created file: ${args.path} (+3 lines, -0 lines)`, fileDiff: { path: args.path, additions: 3, deletions: 0, lines: [] } };
    if (name === 'run_terminal_command') return { name, output: 'Command failed (exit code 1):\nFAIL test/x.test.js', terminalCommand: args.command };
    return { name, output: 'ok' };
  };
});
test.afterEach(() => { ToolRegistry.executeTool = realExecute; });

const read = (path, size) => ({ name: 'read_file', arguments: JSON.stringify({ path, size }) });

/**
 * A client playing `script`: one entry per agent request (requests with tools), each a reply
 * { content, toolCalls, finishReason, promptTokens, throw, abort } or a function of the request returning one.
 * Requests without tools (notes and reports) get `notes`, or `notesReply`'s answer.
 */
function scriptedClient(script, { contextTokens, notes = '### Goal\nThe goal.\n### Next\nThe next step.', notesReply } = {}) {
  const requests = [];
  let i = 0;
  return {
    requests,
    lastKnownClusterStatus: () => (contextTokens ? { policy: { coding_context_tokens: contextTokens } } : null),
    async streamChat(messages, tools, onDelta, _start, signal) {
      const req = { messages: JSON.parse(JSON.stringify(messages)), tools: tools.map((t) => t.function.name) };
      requests.push(req);
      if (tools.length === 0 && String(messages[messages.length - 1].content).includes('<dlne_compact>')) {
        if (notesReply) return notesReply(req);
        return { content: notes, toolCalls: [] };
      }
      let reply = script[Math.min(i++, script.length - 1)];
      if (typeof reply === 'function') reply = reply(req, signal);
      if (reply.throw) throw Object.assign(new Error(reply.throw), { status: reply.status });
      if (reply.content) onDelta(reply.content);
      return { content: reply.content || '', toolCalls: reply.toolCalls || [], finishReason: reply.finishReason, promptTokens: reply.promptTokens };
    },
  };
}

function recorder() {
  const shown = { content: '', progress: [], review: [], tools: [], states: 0 };
  return {
    shown,
    callbacks: {
      onThinkingDelta() {},
      onContentDelta: (d) => { shown.content += d; },
      onToolStart: (name) => shown.tools.push(name),
      onToolEnd() {},
      onLoadingProgress: (line) => shown.progress.push(line),
      onReviewProgress: (line) => shown.review.push(line),
      onStateChanged: () => { shown.states++; },
    },
  };
}

const agentRequests = (client) => client.requests.filter((r) => r.tools.length > 0);
const notesRequests = (client) => client.requests.filter((r) => String(r.messages[r.messages.length - 1].content).includes('<dlne_compact>'));

test('a first request is the system prompt and the user\'s words, with every tool, remember last', async () => {
  const client = scriptedClient([{ content: 'Hello.' }]);
  const { callbacks } = recorder();
  assert.equal(await new AgentLoop(client).run('hi there', callbacks), 'done');
  const [req] = client.requests;
  assert.deepEqual(req.messages, [{ role: 'system', content: AGENT_SYSTEM_PROMPT }, { role: 'user', content: 'hi there' }]);
  assert.equal(req.tools.length, 8);
  assert.equal(req.tools[7], 'remember');
});

test('the <dlne_context> block, when there is one, is message 1, and it is refreshed for each request', async () => {
  let memory = '<dlne_context>\nStanding notes.\n## Project memory (.dlne/memory.md)\n- one\n</dlne_context>';
  const client = scriptedClient([{ content: 'A.' }, { content: 'B.' }]);
  const loop = new AgentLoop(client, { contextProvider: () => memory });
  const { callbacks } = recorder();
  await loop.run('first', callbacks);
  memory = memory.replace('- one', '- one\n- two');
  await loop.run('second', callbacks);
  assert.equal(client.requests[0].messages[1].content, '<dlne_context>\nStanding notes.\n## Project memory (.dlne/memory.md)\n- one\n</dlne_context>');
  assert.match(client.requests[1].messages[1].content, /- two/);
  assert.equal(client.requests[1].messages[2].content, 'first', 'the conversation follows the context block');
});

test('a context provider that throws, or gives nothing, adds no message and never stops the agent', async () => {
  const client = scriptedClient([{ content: 'A.' }, { content: 'B.' }]);
  const { callbacks } = recorder();
  assert.equal(await new AgentLoop(client, { contextProvider: () => { throw new Error('EACCES'); } }).run('x', callbacks), 'done');
  assert.equal(await new AgentLoop(client, { contextProvider: () => null }).run('y', callbacks), 'done');
  assert.equal(client.requests[0].messages.length, 2);
  assert.equal(client.requests[1].messages.length, 2);
});

test('the budget follows the coordinator\'s policy.coding_context_tokens, and 32,768 when it does not say', () => {
  assert.equal(DEFAULT_CONTEXT_TOKENS - AGENT_MAX_TOKENS - 1024, 21504, 'a 32K slot: the reply\'s room and the margin taken off');
  assert.equal(new AgentLoop(scriptedClient([])).promptBudget(), 21504);
  assert.equal(new AgentLoop(scriptedClient([], { contextTokens: 40960 })).promptBudget(), 29491,
    'TheWall at four slots: capped at 72% of the context, under the coordinator\'s 3-characters-a-token count');
  assert.equal(new AgentLoop(scriptedClient([], { contextTokens: 65536 })).promptBudget(), Math.floor(65536 * 0.72));
  assert.equal(new AgentLoop(scriptedClient([], { contextTokens: 12000 })).promptBudget(), 6000, 'never below the floor');
  assert.equal(new AgentLoop({}).promptBudget(), 21504, 'a client with no status at all');
});

test('the budget follows policy.coding_context_tokens_max when it is bigger (TheWall\'s 131,072), and nothing changes without it', () => {
  const loopWith = (policy) => new AgentLoop({ lastKnownClusterStatus: () => ({ policy }) });
  assert.equal(loopWith({ coding_context_tokens: 40960, coding_context_tokens_max: 131072 }).promptBudget(), Math.floor(131072 * 0.72),
    'the largest machine\'s context: a request too big for a member is routed to one that holds it');
  // An old coordinator, or a largest figure that is no use: the smallest, exactly as before.
  const before = loopWith({ coding_context_tokens: 40960 }).promptBudget();
  assert.equal(before, 29491);
  for (const max of [40960, 32768, 131072.5, '131072', null, -1, NaN, 2_000_000]) {
    assert.equal(loopWith({ coding_context_tokens: 40960, coding_context_tokens_max: max }).promptBudget(), before, String(max));
  }
  // The largest is only believed alongside a usable smallest; without one, 32,768 as before.
  assert.equal(loopWith({ coding_context_tokens_max: 131072 }).promptBudget(), 21504);
  assert.equal(loopWith({ coding_context_tokens: 2000, coding_context_tokens_max: 131072 }).promptBudget(), 21504);
});

test('a conversation that fits its slot is never compacted (1.4.1 compacted at 15,360 of a 32K slot)', async () => {
  const turns = [];
  for (let n = 1; n <= 12; n++) turns.push({ toolCalls: [{ id: `c${n}`, ...read(`src/f${n}.ts`, 3500) }] });
  turns.push({ content: 'All read.' });
  const client = scriptedClient(turns);               // no policy: a 32,768 slot
  const loop = new AgentLoop(client);
  assert.equal(await loop.run('read all twelve files', recorder().callbacks), 'done');
  const size = loop.estimateTotalTokens();
  assert.ok(size > 15360 && size <= loop.promptBudget(), `over the old budget and under the new one: ${size} of ${loop.promptBudget()}`);
  assert.equal(notesRequests(client).length, 0, 'no notes request');
  assert.equal(agentRequests(client).at(-1).messages[1].content, 'read all twelve files', 'no working-memory block before the words');
});

test('over the budget the model writes notes, then older steps give way to the working-memory block', async () => {
  const turns = [];
  for (let n = 1; n <= 9; n++) turns.push({ toolCalls: [{ id: `c${n}`, ...read(`src/f${n}.ts`, 3000) }] });
  turns.push({ content: 'All read.' });
  const client = scriptedClient(turns, { contextTokens: 20000, notes: '<think>hmm</think>### Goal\nRead nine files.\n### Next\nAnswer.' });
  const loop = new AgentLoop(client);
  const { shown, callbacks } = recorder();
  assert.equal(await loop.run('please read all nine files', callbacks), 'done');

  const notes = notesRequests(client);
  assert.ok(notes.length >= 1, 'no notes were asked for');
  assert.deepEqual(notes[0].tools, [], 'the notes request carries no tools');
  assert.equal(notes[0].messages[notes[0].messages.length - 1].content, COMPACT_REQUEST);
  assert.ok(shown.progress.includes('📝 Writing notes to make room…'));

  const last = agentRequests(client).at(-1).messages;
  const wm = last[1];
  assert.equal(wm.role, 'user');
  assert.ok(wm.content.startsWith(`${WORKING_MEMORY_OPEN}\nOlder steps of this task were replaced`));
  assert.ok(wm.content.endsWith(WORKING_MEMORY_CLOSE));
  assert.match(wm.content, /## The user's words\n1\. please read all nine files/);
  assert.match(wm.content, /## Your notes\n### Goal\nRead nine files\./);
  assert.doesNotMatch(wm.content, /<think>/, 'the model\'s scratchpad is not kept');
  assert.match(wm.content, /## Ledger\nRead:\n- src\/f1\.ts/);
  assert.equal(last[2].role, 'assistant', 'the word-for-word tail starts at an assistant turn');
  for (let k = 2; k < last.length; k++) {
    if (last[k].role === 'tool') {
      const call = last.slice(0, k).reverse().find((m) => m.role === 'assistant');
      assert.ok(call.tool_calls.some((tc) => tc.id === last[k].tool_call_id), 'a tool result whose call is gone');
    }
  }
  assert.ok(loop.estimateTotalTokens() <= loop.promptBudget());
  assert.ok(shown.states > 0);
});

test('when the notes request fails, the compaction still happens and earlier notes stay', async () => {
  const turns = [];
  for (let n = 1; n <= 9; n++) turns.push({ toolCalls: [{ id: `c${n}`, ...read(`src/f${n}.ts`, 3000) }] });
  turns.push({ content: 'Done.' });
  const client = scriptedClient(turns, { contextTokens: 20000, notesReply: () => { throw new Error('busy'); } });
  const loop = new AgentLoop(client);
  const { callbacks } = recorder();
  assert.equal(await loop.run('read them', callbacks), 'done');
  const last = agentRequests(client).at(-1).messages;
  assert.ok(last[1].content.startsWith(WORKING_MEMORY_OPEN));
  assert.doesNotMatch(last[1].content, /## Your notes/, 'no notes came, so there are none to show');
  assert.match(last[1].content, /## Ledger/);
});

test('one turn that reads twelve big files still fits after the compaction (seen live, 2026-10-26)', async () => {
  const calls = [];
  for (let n = 1; n <= 12; n++) calls.push({ id: `c${n}`, ...read(`src/f${n}.ts`, 5000) });
  const client = scriptedClient([{ toolCalls: calls }, { content: 'Summary.' }]);
  const loop = new AgentLoop(client);
  const { callbacks } = recorder();
  assert.equal(await loop.run('read these twelve files', callbacks), 'done');
  assert.equal(notesRequests(client).length, 1);
  const last = agentRequests(client).at(-1).messages;
  assert.ok(last[1].content.startsWith(WORKING_MEMORY_OPEN));
  assert.equal(last.filter((m) => m.role === 'tool').length, 12, 'the turn keeps all its results');
  assert.ok(loop.estimateTotalTokens() <= loop.promptBudget(), `still over: ${loop.estimateTotalTokens()} > ${loop.promptBudget()}`);
});

test('a write too big for the tail keeps its call but leaves the file\'s contents to the disk', async () => {
  const content = 'z'.repeat(30000);
  const client = scriptedClient([
    { toolCalls: [{ id: 'w1', name: 'write_file', arguments: JSON.stringify({ path: 'big.txt', content }) }] },
    { content: 'Written.' },
  ], { contextTokens: 20000 });
  const loop = new AgentLoop(client);
  const { callbacks } = recorder();
  assert.equal(await loop.run('write a big file', callbacks), 'done');
  const last = agentRequests(client).at(-1).messages;
  const write = last.find((m) => m.role === 'assistant' && m.tool_calls);
  const args = JSON.parse(write.tool_calls[0].function.arguments);
  assert.equal(args.path, 'big.txt');
  assert.match(args.content, /^\[… 30000 characters, left out to make room; the file on disk has them\]$/);
});

test('an overflow lowers the budget, makes room and retries the same step, without using up a turn', async () => {
  let failures = 0;
  const client = scriptedClient([
    () => (failures++ < 2 ? { throw: 'the request exceeds the available context size, try increasing it' } : { content: 'Fine now.' }),
  ]);
  const loop = new AgentLoop(client);
  const before = loop.promptBudget();
  const { shown, callbacks } = recorder();
  assert.equal(await loop.run('go', callbacks, { maxTurns: 10 }), 'done');
  assert.equal(failures, 3);
  // Lowered twice, then the turn that went through gives 5% back.
  assert.equal(loop.promptBudget(), Math.floor(Math.floor(Math.floor(before * 0.75) * 0.75) * 1.05));
  assert.ok(shown.progress.some((l) => l.includes('outgrew the model\'s context')));
  assert.doesNotMatch(shown.content, /Error connecting/);
});

test('a fourth overflow in a row ends the run with the context-limit message', async () => {
  const client = scriptedClient([{ throw: 'exceeds the available context size' }]);
  const { shown, callbacks } = recorder();
  assert.equal(await new AgentLoop(client).run('go', callbacks), 'error');
  assert.match(shown.content, /Context limit reached/);
  assert.doesNotMatch(shown.content, /Error connecting/);
});

test('a reply cut off in the middle of a tool call runs nothing, says why, and the next turn goes on', async () => {
  const client = scriptedClient([
    { toolCalls: [{ id: 'w1', name: 'write_file', arguments: '{"path": "a.ts", "content": "half a fi' }], finishReason: 'length' },
    { toolCalls: [{ id: 'w2', name: 'write_file', arguments: JSON.stringify({ path: 'a.ts', content: 'part one' }) }] },
    { content: 'Written in parts.' },
  ]);
  const loop = new AgentLoop(client);
  const before = loop.promptBudget();
  const { callbacks } = recorder();
  assert.equal(await loop.run('write a.ts', callbacks), 'done');
  assert.deepEqual(executed.map((e) => e.args.content), ['part one'], 'the cut-off call was not run');
  const second = agentRequests(client)[1].messages;
  assert.equal(second.at(-1).content, CUT_OFF_NOTICE);
  assert.equal(second.at(-2).role, 'assistant');
  assert.equal(second.at(-2).tool_calls, undefined, 'the cut-off call is kept as plain text, with no call to answer');
  assert.ok(loop.promptBudget() < before);
});

test('an inline tool call cut off at the length limit is not run either', async () => {
  const client = scriptedClient([
    { content: '<tool_call>{"name": "run_terminal_command", "arguments": {"command": "rm -rf bu', finishReason: 'length' },
    { content: 'Stopped.' },
  ]);
  const { callbacks } = recorder();
  assert.equal(await new AgentLoop(client).run('clean up', callbacks), 'done');
  assert.deepEqual(executed, []);
});

test('Stop while a reply arrives: nothing from it runs or is kept', async () => {
  const loop = new AgentLoop(scriptedClient([]));
  const client = scriptedClient([
    () => { loop.abort(); return { toolCalls: [{ id: 'w1', name: 'write_file', arguments: '{"path": "a.ts", "content": "par' }] }; },
  ]);
  loop.client = client; // the scripted reply needs the loop to press Stop on
  const { callbacks } = recorder();
  assert.equal(await loop.run('write', callbacks), 'aborted');
  assert.deepEqual(executed, []);
  assert.equal(loop.messages.at(-1).content, 'write', 'the half reply is not in the history');
});

test('Stop between the calls of one reply: the rest are answered "not run", so every call keeps a result', async () => {
  const loop = new AgentLoop(scriptedClient([]));
  loop.client = scriptedClient([{ toolCalls: [{ id: 'a', ...read('a.ts') }, { id: 'b', ...read('b.ts') }] }]);
  ToolRegistry.executeTool = async (name, args) => { executed.push(args.path); loop.abort(); return { name, output: '1: x' }; };
  const { callbacks } = recorder();
  assert.equal(await loop.run('read two', callbacks), 'aborted');
  assert.deepEqual(executed, ['a.ts']);
  const tools = loop.messages.filter((m) => m.role === 'tool');
  assert.deepEqual(tools.map((m) => m.tool_call_id), ['a', 'b']);
  assert.equal(tools[1].content, 'Not run: the user stopped the task.');
});

test('at the turn limit: a report without tools becomes the notes, then Continue carries on with the history', async () => {
  const turns = [];
  for (let n = 1; n <= 10; n++) turns.push({ toolCalls: [{ id: `c${n}`, ...read(`src/f${n}.ts`) }] });
  turns.push((req) => (req.tools.length === 0
    ? { content: '<think>sum up</think>Read ten files; found the bug in f7.ts. Left: fix it.' }
    : { content: 'unexpected' }));
  turns.push({ content: 'Fixed.' });
  const client = scriptedClient(turns);
  // The pause report goes out with no tools: the scripted client sees it as an agent request only by its content.
  client.streamChat = ((orig) => async (messages, tools, ...rest) => {
    if (tools.length === 0 && String(messages.at(-1).content).includes('used its limit')) {
      client.requests.push({ messages, tools: [] });
      rest[0]('Read ten files; found the bug in f7.ts. Left: fix it.');
      return { content: '<think>sum up</think>Read ten files; found the bug in f7.ts. Left: fix it.', toolCalls: [] };
    }
    return orig(messages, tools, ...rest);
  })(client.streamChat.bind(client));
  const loop = new AgentLoop(client);
  const { shown, callbacks } = recorder();
  assert.equal(await loop.run('find the bug', callbacks, { maxTurns: 10 }), 'paused');
  assert.equal(executed.length, 10);
  const report = client.requests.at(-1);
  assert.match(String(report.messages.at(-1).content), new RegExp(pausePrompt(10).replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
  assert.match(shown.content, /Paused after 10 steps/);
  assert.equal(loop.exportState().notes, 'Read ten files; found the bug in f7.ts. Left: fix it.');

  assert.equal(await loop.run('continue', callbacks, { maxTurns: 10 }), 'done');
  const resumed = client.requests.at(-1).messages;
  assert.equal(resumed[1].content, 'find the bug', 'Continue keeps the conversation');
  assert.equal(resumed.at(-1).content, 'continue');
});

test('a retry after an overflow is not a turn: the limit counts replies only', async () => {
  // One script entry serves every request (the client moves on per request): two overflows, then ten reads,
  // then the pause report, which is asked for without tools.
  let calls = 0;
  const client = scriptedClient([(req) => {
    calls++;
    if (calls <= 2) return { throw: 'context size exceeded' };
    if (req.tools.length === 0) return { content: 'report' };
    return { toolCalls: [{ id: `c${calls}`, ...read(`f${calls}.ts`) }] };
  }]);
  const { callbacks } = recorder();
  assert.equal(await new AgentLoop(client).run('go', callbacks, { maxTurns: 10 }), 'paused');
  assert.equal(executed.length, 10, 'ten replies ran tools: the two overflowed attempts did not count');
});

test('the saved conversation round-trips, working memory, ledger and all; malformed state changes nothing', async () => {
  const turns = [];
  for (let n = 1; n <= 9; n++) turns.push({ toolCalls: [{ id: `c${n}`, ...read(`src/f${n}.ts`, 3000) }] });
  turns.push({ content: 'Done.', promptTokens: 4000 });
  const loop = new AgentLoop(scriptedClient(turns, { contextTokens: 20000 }));
  const { callbacks } = recorder();
  await loop.run('read them all', callbacks);
  const saved = JSON.parse(JSON.stringify(loop.exportState()));
  assert.equal(saved.version, 1);
  assert.ok(saved.messages[0].content.startsWith(WORKING_MEMORY_OPEN));
  assert.deepEqual(saved.userWords, ['read them all']);
  assert.ok(saved.notes.length > 0);
  assert.ok(saved.ledger && saved.charsPerToken >= 2 && saved.charsPerToken <= 5);

  const restored = new AgentLoop(scriptedClient([{ content: 'Again.' }]));
  assert.equal(restored.importState(saved), true);
  assert.deepEqual(restored.messages, loop.messages);
  assert.equal(restored.getSessionId(), loop.getSessionId());

  const untouched = new AgentLoop(scriptedClient([]));
  const id = untouched.getSessionId();
  for (const bad of [null, 7, {}, { ...saved, version: 2 }, { ...saved, messages: 'x' }, { ...saved, messages: [{ role: 'system', content: 'x' }] }]) {
    assert.equal(untouched.importState(bad), false);
  }
  assert.equal(untouched.getSessionId(), id);
  assert.equal(untouched.messages.length, 1);
});

test('a long task ends with a look back: only remember runs, and nothing of it stays in the history', async () => {
  const { REFLECT_REQUEST } = require('../dist/agent/agentLoop');
  const turns = [];
  for (let n = 1; n <= 5; n++) turns.push({ toolCalls: [{ id: `c${n}`, ...read(`f${n}.ts`) }] });
  turns.push({ content: 'All done.' });
  turns.push((req) => {
    assert.equal(req.messages.at(-1).content, REFLECT_REQUEST);
    assert.equal(req.tools.length, 8, 'the same tools as every request');
    return { toolCalls: [
      { id: 'r1', name: 'remember', arguments: JSON.stringify({ fact: 'Tests run with make test.', scope: 'project' }) },
      { id: 'r2', name: 'read_file', arguments: JSON.stringify({ path: 'sneaky.ts' }) },
    ] };
  });
  const client = scriptedClient(turns);
  const loop = new AgentLoop(client);
  const { shown, callbacks } = recorder();
  assert.equal(await loop.run('do the thing', callbacks, { reflect: true }), 'done');
  assert.deepEqual(executed.map((e) => e.name), ['read_file', 'read_file', 'read_file', 'read_file', 'read_file', 'remember']);
  assert.ok(shown.review.includes('📝 Looking back for anything worth remembering…'));
  // Nothing after the answer goes through onLoadingProgress: in VS Code's Chat view a progress line after the
  // answer folds the answer into "Completed N steps" (2026-10-27: the summary was there, but hidden).
  assert.ok(!shown.progress.some((line) => line.includes('Looking back')));
  assert.equal(loop.messages.at(-1).content, 'All done.', 'the look back is not part of the conversation');

  // A short task, or a loop not asked to, does not look back.
  const short = scriptedClient([{ content: 'Quick answer.' }]);
  await new AgentLoop(short).run('hi', recorder().callbacks, { reflect: true });
  assert.equal(short.requests.length, 1);
});

test('the user\'s words: the first request, then the newest, with what is left out counted', () => {
  assert.equal(renderUserWords([]), '');
  assert.equal(renderUserWords(['a', 'b', 'c']), '1. a\n2. b\n3. c');
  // The first request keeps 4,000 characters; "old" (5,000) does not fit what is left, the two newest do.
  const long = renderUserWords(['first '.repeat(1000), 'old '.repeat(1250), 'newer', 'newest'], 8000);
  assert.ok(long.startsWith('1. first first'));
  assert.match(long, /\[… 2000 characters cut\]\n\[… 5000 characters of earlier messages left out\]\n3\. newer\n4\. newest$/);
  assert.ok(long.length <= 8000 + 100);
  const cut = renderUserWords(['x'.repeat(9000)], 8000);
  assert.match(cut, /\[… 5000 characters cut\]$/);
});

test('the model\'s scratchpad is removed from notes, finished or not', () => {
  assert.equal(withoutThinking('<think>a</think>Notes<think>b'), 'Notes');
  assert.equal(withoutThinking('  plain  '), 'plain');
});

test('a task that did work but ends with an empty reply is asked once, without tools, for a report', async () => {
  // 2026-10-27: 23 minutes of steps ended with nothing said; the last thing shown was a failed remember.
  const { REPORT_REQUEST } = require('../dist/agent/agentLoop');
  const client = scriptedClient([
    { toolCalls: [{ id: 'c1', ...read('a.ts') }] },
    { content: '' },
    (req) => (req.tools.length === 0 ? { content: 'Read a.ts; it needed no change.' } : { content: 'unexpected' }),
  ]);
  const loop = new AgentLoop(client);
  const { shown, callbacks } = recorder();
  assert.equal(await loop.run('check a.ts', callbacks), 'done');
  const last = client.requests.at(-1);
  assert.deepEqual(last.tools, [], 'no tools for the report');
  assert.equal(last.messages.at(-1).content, REPORT_REQUEST);
  assert.match(shown.content, /Read a\.ts; it needed no change\./);
  assert.equal(executed.length, 1);

  // A task that ends with words, or one that did no work, is not asked again.
  const said = scriptedClient([{ toolCalls: [{ id: 'c1', ...read('a.ts') }] }, { content: 'Done: a.ts is fine.' }]);
  await new AgentLoop(said).run('check a.ts', recorder().callbacks);
  assert.equal(said.requests.length, 2);
  const quiet = scriptedClient([{ content: '' }]);
  await new AgentLoop(quiet).run('hi', recorder().callbacks);
  assert.equal(quiet.requests.length, 1);
});

test('the look back after a task shows only facts it saved, never a failure, so the answer stays last', async () => {
  const turns = [];
  for (let n = 1; n <= 5; n++) turns.push({ toolCalls: [{ id: `c${n}`, ...read(`f${n}.ts`) }] });
  turns.push({ content: 'All done.' });
  turns.push({ toolCalls: [
    { id: 'r1', name: 'remember', arguments: JSON.stringify({ fact: 'refused', scope: 'project' }) },
    { id: 'r2', name: 'remember', arguments: JSON.stringify({ fact: 'saved', scope: 'project' }) },
  ] });
  ToolRegistry.executeTool = async (name, args) => {
    executed.push({ name, args });
    if (name !== 'remember') return { name, output: '1: x' };
    return args.fact === 'refused'
      ? { name, output: 'The fact is 720 characters; the limit is 500.', is_error: true }
      : { name, output: '📝 Remembered for this project: saved', memoryChange: { file: '/p/.dlne/memory.md', scope: 'project', removed: null, added: '- saved', index: 3 } };
  };
  const ended = [];
  const { shown, callbacks } = recorder();
  callbacks.onToolEnd = (result) => ended.push(result);
  assert.equal(await new AgentLoop(scriptedClient(turns)).run('do it', callbacks, { reflect: true }), 'done');
  const shownAfterAnswer = ended.filter((r) => r.name === 'remember');
  assert.deepEqual(shownAfterAnswer.map((r) => r.output), ['📝 Remembered for this project: saved']);
  assert.equal(shown.tools.filter((t) => t === 'remember').length, 0, 'not announced as a step');
});

// ---- remember failing again and again (Memory 2) -------------------------------------------------------------
// 2026-10-30: a model tidying a nearly full memory file failed a dozen remember calls in a row, and nothing stopped it.

const REMEMBER_STOPPED = 'remember has failed 3 times in a row in this task, so DLNE stopped running it. Carry on with '
  + 'the task, and tell the user what you wanted to save.';

/** A fake remember: a fact starting "bad" fails, "no" is declined by the user, anything else is saved. */
function fakeRemember() {
  ToolRegistry.executeTool = async (name, args) => {
    executed.push({ name, args });
    if (name !== 'remember') return { name, output: `1: ${args.path}` };
    if (args.fact.startsWith('bad')) return { name, output: `No fact you saved matches "${args.fact}".`, is_error: true };
    if (args.fact.startsWith('no')) {
      return { name, output: `The user declined: remember for you: "${args.fact}". Do not retry it; ask the user or choose another approach.`, is_error: true };
    }
    return { name, output: `📝 Remembered for this project [abcd]: ${args.fact}` };
  };
}

/** One step of remember calls, written natively, one per fact. */
const rememberStep = (step, facts) => ({
  toolCalls: facts.map((fact, k) => ({ id: `m${step}-${k}`, name: 'remember', arguments: JSON.stringify({ fact, scope: 'project' }) })),
});

const remembered = () => executed.filter((e) => e.name === 'remember').map((e) => e.args.fact);

test('after 3 failed remember calls in a row, later ones in the run are answered, not run', async () => {
  fakeRemember();
  const client = scriptedClient([
    rememberStep(1, ['bad one', 'bad two', 'bad three', 'Tests run with make test.']),
    rememberStep(2, ['Build with make.']),
    { content: 'Done; I could not save two facts: the test and build commands.' },
  ]);
  const log = [];
  const loop = new AgentLoop(client, { log: (line) => log.push(line) });
  const ended = [];
  const { shown, callbacks } = recorder();
  callbacks.onToolEnd = (result) => ended.push(result);
  assert.equal(await loop.run('tidy the memory', callbacks), 'done');

  assert.deepEqual(remembered(), ['bad one', 'bad two', 'bad three'], 'the fourth and fifth were not run');
  // Every call keeps its answer, so the history is one a saved task can be taken back from.
  const answers = loop.messages.filter((m) => m.role === 'tool');
  assert.deepEqual(answers.map((m) => m.tool_call_id), ['m1-0', 'm1-1', 'm1-2', 'm1-3', 'm2-0']);
  assert.equal(answers[3].content, REMEMBER_STOPPED);
  assert.equal(answers[4].content, REMEMBER_STOPPED);
  assert.ok(new AgentLoop(scriptedClient([])).importState(JSON.parse(JSON.stringify(loop.exportState()))));
  // Each call is still shown, as the error it is.
  assert.equal(shown.tools.length, 5);
  assert.deepEqual(ended.slice(3).map((r) => [r.output, r.is_error]), [[REMEMBER_STOPPED, true], [REMEMBER_STOPPED, true]]);
  // The log says so with names and numbers only, never the facts.
  assert.ok(log.some((l) => /remember check: stopped running remember after step 1 \(3 failures in a row\)/.test(l)));
  assert.equal(log.filter((l) => /remember check: a remember call in step \d+ was not run/.test(l)).length, 2);
  assert.ok(!log.some((l) => /make test|bad one/.test(l)), 'no fact text in the log');

  // The next run, a Continue included, starts again from nothing.
  loop.client = scriptedClient([rememberStep(3, ['Build with make.']), { content: 'Saved.' }]);
  assert.equal(await loop.run('continue', recorder().callbacks), 'done');
  assert.equal(remembered().at(-1), 'Build with make.');
});

test('a saved fact starts the count again', async () => {
  fakeRemember();
  const client = scriptedClient([
    rememberStep(1, ['bad one', 'bad two', 'Tests run with make test.']),
    rememberStep(2, ['bad three', 'bad four', 'bad five', 'bad six']),
    { content: 'Done.' },
  ]);
  assert.equal(await new AgentLoop(client).run('tidy', recorder().callbacks), 'done');
  assert.deepEqual(remembered(), ['bad one', 'bad two', 'Tests run with make test.', 'bad three', 'bad four', 'bad five']);
});

test('the user saying no is not a failure: it neither counts nor starts the count again', async () => {
  fakeRemember();
  const client = scriptedClient([
    rememberStep(1, ['bad one', 'no thanks', 'no again', 'bad two', 'no third', 'bad three', 'Build with make.']),
    { content: 'Done.' },
  ]);
  const loop = new AgentLoop(client);
  assert.equal(await loop.run('tidy', recorder().callbacks), 'done');
  assert.deepEqual(remembered(), ['bad one', 'no thanks', 'no again', 'bad two', 'no third', 'bad three']);
  assert.equal(loop.messages.filter((m) => m.role === 'tool').at(-1).content, REMEMBER_STOPPED);
});

test('a call written as text is answered the same way once remember is stopped', async () => {
  fakeRemember();
  const inline = (fact) => `<tool_call>{"name": "remember", "arguments": {"fact": "${fact}", "scope": "project"}}</tool_call>`;
  const client = scriptedClient([
    { content: inline('bad one') }, { content: inline('bad two') }, { content: inline('bad three') },
    { content: inline('Build with make.') },
    { content: 'Done.' },
  ]);
  const loop = new AgentLoop(client);
  assert.equal(await loop.run('tidy', recorder().callbacks), 'done');
  assert.deepEqual(remembered(), ['bad one', 'bad two', 'bad three']);
  const observation = agentRequests(client)[4].messages.at(-1).content;
  assert.ok(observation.startsWith(`Observation from tool [remember]:\n${REMEMBER_STOPPED}`));
});

/** `first` steps, reads up to five steps, the answer, then the look back's reply. */
function taskThenLookBack(first, lookBack) {
  const turns = [...first];
  for (let n = turns.length + 1; n <= 5; n++) turns.push({ toolCalls: [{ id: `c${n}`, ...read(`f${n}.ts`) }] });
  turns.push({ content: 'All done.' });
  turns.push(lookBack);
  return scriptedClient(turns);
}

test('the look back sends the Memory 2 request, and shares the task\'s remember count', async () => {
  fakeRemember();
  // Two failures in the task; the look back's first failure is the third, so its next call is not run.
  let sent;
  const client = taskThenLookBack([rememberStep(1, ['bad one', 'bad two'])], (req) => {
    sent = req.messages.at(-1).content;
    return rememberStep(9, ['bad three', 'Tests run with make test.']);
  });
  const log = [];
  const ended = [];
  const { callbacks } = recorder();
  callbacks.onToolEnd = (result) => ended.push(result);
  assert.equal(await new AgentLoop(client, { log: (line) => log.push(line) }).run('do it', callbacks, { reflect: true }), 'done');
  assert.equal(sent, [
    '<dlne_reflect>',
    'The task is finished. Save, with remember, what a later session in this project would otherwise have to rediscover: how to build, test or run it (exact commands), where things live, the conventions its code follows, and any correction or preference the user gave. Look at Project memory first: update a fact that is now wrong or incomplete (its id in replaces) instead of adding another, and remove one that no longer holds. One fact per call; usually one to three changes are worth making. Skip details that mattered only for this task. If there is truly nothing new, reply with just: nothing.',
    '</dlne_reflect>',
  ].join('\n'));
  assert.deepEqual(remembered(), ['bad one', 'bad two', 'bad three']);
  assert.ok(log.some((l) => /remember check: stopped running remember after the look back \(3 failures in a row\)/.test(l)));
  assert.ok(log.some((l) => /remember check: a remember call in the look back was not run/.test(l)));
  assert.ok(!ended.some((r) => r.output === REMEMBER_STOPPED), 'after the answer nothing but a saved fact is shown');

  // A task that already stopped remember: the look back runs none.
  executed = [];
  const stopped = taskThenLookBack([rememberStep(1, ['bad one', 'bad two', 'bad three'])],
    rememberStep(9, ['Tests run with make test.']));
  assert.equal(await new AgentLoop(stopped).run('do it', recorder().callbacks, { reflect: true }), 'done');
  assert.deepEqual(remembered(), ['bad one', 'bad two', 'bad three']);
});
