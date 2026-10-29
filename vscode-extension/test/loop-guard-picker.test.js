'use strict';
/**
 * The loop guard (src/agent/loopGuard.ts) for DLNE Swarm in VS Code's Chat model picker. VS Code's own agent drives that
 * loop and sends the whole history with every request, so the provider reads the history's trailing tool calls and
 * results, with nothing kept between requests: at the rule's count the system message gets the loop-check note, and when
 * the same loop has gone on two repeats past it, the request is refused with an error in the provider's usual style
 * ("DLNE stopped: the agent kept <what>. Change the request or try again."), before anything is sent to the swarm. That
 * refusal leaves nothing in VS Code's history, so the one request that carries the user's next words, with the loop right
 * before them, gets the stop's note in its system message ("DLNE stopped the last task because you kept <what>; take a
 * different approach."), and nothing else about it changes. Nothing is stored: the next request ends with the model's own step.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const stub = require('./helpers/vscode-stub');
const lm = require('./helpers/lm-stub');
const { generator } = require('./helpers/fake-secrets');

// The DLNE log: the module keeps the first channel it makes, and stub.reset() forgets it, so this stays in place.
const logLines = [];
stub.vscode.window.createOutputChannel = (name) => ({ name, appendLine: (line) => logLines.push(line), show() {}, dispose() {} });

const prov = require('../dist/client/modelProvider');
const { DlneClient } = require('../dist/client/dlneClient');
const { toOpenAiMessages } = require('../dist/client/lmMessages');
const { forgetLogChannel } = require('../dist/log');

const home = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-loop-picker-home-'));
process.env.HOME = home;
process.once('exit', () => fs.rmSync(home, { recursive: true, force: true }));

const { Role, message, makeToken } = lm;
const { LanguageModelTextPart, LanguageModelToolCallPart, LanguageModelToolResultPart } = lm;

const MODEL = { id: 'swarm-auto', name: 'DLNE Swarm' };
const tool = (name) => ({ name, description: `the ${name} tool`, inputSchema: { type: 'object', properties: {} } });
const TOOLS = [tool('read_file'), tool('run_in_terminal'), tool('replace_string_in_file'), tool('grep_search')];

/** A client that answers every request with `answer`, and records what it was sent. */
function fakeClient(answer = 'On it.') {
  const calls = [];
  return {
    calls,
    async streamChat(messages, tools, onDelta) {
      calls.push({ messages, tools });
      onDelta(answer);
      return { content: answer, toolCalls: [], finishReason: 'stop' };
    },
  };
}

/**
 * One request through the provider: what the client was sent, the text shown, and the error VS Code would have been given.
 * The log starts empty (and forgets what it wrote a minute ago). The settings are left alone: a test that talks to a local
 * coordinator has set `dlne.serverUrl`, and a reset here would send the next request to the public swarm.
 */
async function request(client, messages, provider = new prov.DlneChatModelProvider(client)) {
  logLines.length = 0;
  forgetLogChannel();
  const parts = [];
  let error;
  try {
    await provider.provideLanguageModelChatResponse(MODEL, messages, { tools: TOOLS, toolMode: 1 }, { report: (part) => parts.push(part) }, makeToken());
  } catch (err) {
    error = err;
  }
  return { error, text: parts.filter((p) => p instanceof LanguageModelTextPart).map((p) => p.value).join(''), log: logLines.join('\n') };
}

const system = message(Role.System, 'You are the coding agent.');
const task = message(Role.User, 'make the tests pass');
/** One step as VS Code sends it: the assistant's call, then the user turn that carries its result. */
const step = (n, name, input, text) => [
  message(Role.Assistant, new LanguageModelToolCallPart(`call-${n}`, name, input)),
  message(Role.User, new LanguageModelToolResultPart(`call-${n}`, [new LanguageModelTextPart(text)])),
];
const steps = (count, make) => Array.from({ length: count }, (_, n) => step(n, ...make(n))).flat();
const readA = () => ['read_file', { filePath: 'src/a.ts', startLine: 1, endLine: 50 }, 'export const a = 1;'];

const NOTE = 'Loop check: you have been reading `src/a.ts` again and again. That is not working. Try a different approach, or stop and tell the user what is blocking you.';
const STOPPED = 'DLNE stopped: the agent kept reading `src/a.ts` again and again. Change the request or try again.';
const STOP_NOTE = 'DLNE stopped the last task because you kept reading `src/a.ts` again and again; take a different approach.';

test('at the third repeat the system message gets the loop-check note, and the request goes on with the rest as it was', async () => {
  const client = fakeClient();
  const sent = await request(client, [system, task, ...steps(3, readA)]);
  assert.equal(sent.error, undefined);
  assert.equal(sent.text, 'On it.');
  assert.equal(client.calls.length, 1);
  const body = client.calls[0].messages;
  assert.deepEqual(body[0], { role: 'system', content: `You are the coding agent.\n\n${NOTE}` });
  assert.equal(body.filter((m) => m.role === 'system').length, 1, 'still one system message, first');
  assert.equal(body.length, 1 + 1 + 6, 'the system message, the task and three steps of two messages: nothing else was added');
  assert.equal(body[1].content, 'make the tests pass');
  assert.match(sent.log, /DLNE Swarm: loop check told the agent to change approach \(read_file, the same call with the same result, 3 times in the last 12 steps\)\./);
});

test('before the third repeat there is no note, and a fourth keeps it', async () => {
  const two = fakeClient();
  await request(two, [system, task, ...steps(2, readA)]);
  assert.equal(two.calls[0].messages[0].content, 'You are the coding agent.');
  const four = fakeClient();
  const sent = await request(four, [system, task, ...steps(4, readA)]);
  assert.equal(sent.error, undefined);
  assert.equal(four.calls[0].messages[0].content, `You are the coding agent.\n\n${NOTE}`);
});

test('two repeats past the count the request is refused, in the provider\'s style, before anything is sent', async () => {
  const client = fakeClient();
  const sent = await request(client, [system, task, ...steps(5, readA)]);
  assert.ok(sent.error instanceof Error);
  assert.equal(sent.error.message, STOPPED);
  assert.equal(sent.error.stack, undefined, 'a sentence for the user: the Chat view would print a stack under it');
  assert.equal(client.calls.length, 0, 'nothing went to the swarm');
  assert.equal(sent.text, '');
  // The log names the tool and the numbers, never the file.
  assert.match(sent.log, /DLNE Swarm: a request failed: loop check stopped the agent \(read_file, the same call with the same result, 5 times in the last 12 steps\)/);
  assert.ok(!sent.log.includes('src/a.ts'), sent.log);
  assert.ok(!sent.log.includes('DLNE stopped:'), 'the sentence is for the chat, not the log');
});

test('the user answering after a stop is the way on: the request goes through, with the stop\'s note in the system message and nothing else changed', async () => {
  const history = [system, task, ...steps(5, readA)];
  assert.equal((await request(fakeClient(), history)).error.message, STOPPED, 'the refusal, which VS Code keeps nothing of');
  const typed = message(Role.User, 'try the tests in test/ instead');
  const client = fakeClient();
  const sent = await request(client, [...history, typed]);
  assert.equal(sent.error, undefined);
  assert.equal(sent.text, 'On it.');
  assert.equal(client.calls.length, 1);
  const body = client.calls[0].messages;
  assert.deepEqual(body[0], { role: 'system', content: `You are the coding agent.\n\n${STOP_NOTE}` });
  // The history is otherwise as VS Code sent it: the same messages in the same order, the user's words last.
  assert.deepEqual(body.slice(1), toOpenAiMessages([...history, typed]).slice(1));
  assert.equal(body.at(-1).content, 'try the tests in test/ instead');
  // Said by name and number only in the log.
  assert.match(sent.log, /DLNE Swarm: loop check told the agent why its last task was stopped \(read_file, the same call with the same result, 5 times in the last 12 steps\)\./);
  assert.ok(!sent.log.includes('src/a.ts'), sent.log);
});

test('the note is for that one request: the next one ends with the model\'s own new step, and nothing is stored between them', async () => {
  const client = fakeClient();
  const provider = new prov.DlneChatModelProvider(client); // one provider for all four: nothing it keeps may matter
  const history = [system, task, ...steps(5, readA)];
  assert.equal((await request(client, history, provider)).error.message, STOPPED);
  const typed = message(Role.User, 'go on, differently');
  assert.equal((await request(client, [...history, typed], provider)).error, undefined);
  assert.equal(client.calls.at(-1).messages[0].content, `You are the coding agent.\n\n${STOP_NOTE}`);
  const next = [...history, typed, ...step(20, 'grep_search', { query: 'TODO' }, 'No results')];
  assert.equal((await request(client, next, provider)).error, undefined);
  assert.equal(client.calls.at(-1).messages[0].content, 'You are the coding agent.', 'the model\'s own step ends the history: no note');
  // If it then repeats itself again, that is a new loop, counted from its own first step.
  const bReads = Array.from({ length: 3 }, (_, i) => step(50 + i, 'read_file', { filePath: 'src/b.ts', startLine: 1, endLine: 50 }, 'export const b = 2;')).flat();
  assert.equal((await request(client, [...history, typed, ...bReads], provider)).error, undefined);
  assert.equal(client.calls.at(-1).messages[0].content,
    'You are the coding agent.\n\nLoop check: you have been reading `src/b.ts` again and again. That is not working. Try a different approach, or stop and tell the user what is blocking you.',
    'three reads of src/b.ts after the user\'s words: a nudge for them, and not a word of the old loop');
});

test('only a loop that went as far as a refusal is told: a nudged one was never stopped', async () => {
  for (const count of [3, 4]) {
    const client = fakeClient();
    const sent = await request(client, [system, task, ...steps(count, readA), message(Role.User, 'what is going on?')]);
    assert.equal(sent.error, undefined);
    assert.equal(client.calls[0].messages[0].content, 'You are the coding agent.', `${count} reads, then the user's words`);
  }
});

test('a clean history, or an old loop with anything clean after it, gets no note', async () => {
  const typed = message(Role.User, 'and now?');
  const clean = (count) => steps(count, (n) => ['read_file', { filePath: `src/clean${n}.ts` }, `text ${n}`]);
  const loop = steps(5, readA);
  const cases = {
    'a clean history': [system, task, ...clean(8), typed],
    'a loop and one clean step after it': [system, task, ...loop, ...step(40, 'grep_search', { query: 'TODO' }, 'No results'), typed],
    'a loop and a whole clean window after it': [system, task, ...loop, ...clean(12), typed],
    'a loop, the agent\'s answer, then the user': [system, task, ...loop, message(Role.Assistant, 'I could not do it.'), typed],
    'a loop, the user, one clean step, the user again': [system, task, ...loop, message(Role.User, 'go on'), ...step(41, 'grep_search', { query: 'TODO' }, 'No results'), typed],
  };
  for (const [name, history] of Object.entries(cases)) {
    const client = fakeClient();
    const sent = await request(client, history);
    assert.equal(sent.error, undefined, name);
    assert.equal(client.calls[0].messages[0].content, 'You are the coding agent.', name);
  }
});

test('the stop\'s note works the way the nudge\'s does: one system message first, made when there is none, and for two messages of the user in a row once', async () => {
  const none = fakeClient();
  await request(none, [task, ...steps(5, readA), message(Role.User, 'go on')]);
  assert.deepEqual(none.calls[0].messages[0], { role: 'system', content: STOP_NOTE });
  assert.equal(none.calls[0].messages[1].content, 'make the tests pass');

  const two = fakeClient();
  await request(two, [system, task, ...steps(5, readA), message(Role.User, 'one'), message(Role.User, 'two')]);
  const body = two.calls[0].messages;
  assert.equal(body.filter((m) => m.role === 'system').length, 1);
  assert.equal(body[0].content, `You are the coding agent.\n\n${STOP_NOTE}`);
  assert.deepEqual(body.slice(-2).map((m) => m.content), ['one', 'two']);
});

test('a file read again is told too, by the rule that stopped it: 6 reads of the same lines', async () => {
  const reads = (n) => ['read_file', { filePath: 'src/a.ts', startLine: 1, endLine: 10 + n }, `version ${n}`];
  const five = fakeClient();
  await request(five, [system, task, ...steps(5, reads), message(Role.User, 'go on')]);
  assert.equal(five.calls[0].messages[0].content, 'You are the coding agent.', 'five were only a nudge');
  const six = fakeClient();
  await request(six, [system, task, ...steps(6, reads), message(Role.User, 'go on')]);
  assert.equal(six.calls[0].messages[0].content, `You are the coding agent.\n\n${STOP_NOTE}`);
});

test('a step that does something else ends the verdict: the loop is not held against the agent for ever', async () => {
  const client = fakeClient();
  const sent = await request(client, [system, task, ...steps(4, readA), ...step(9, 'grep_search', { query: 'TODO' }, 'No results')]);
  assert.equal(sent.error, undefined);
  assert.equal(client.calls[0].messages[0].content, 'You are the coding agent.');
});

test('with no system message in the request, the note is one, first', async () => {
  const client = fakeClient();
  const sent = await request(client, [task, ...steps(3, readA)]);
  assert.equal(sent.error, undefined);
  const body = client.calls[0].messages;
  assert.deepEqual(body[0], { role: 'system', content: NOTE });
  assert.equal(body[1].content, 'make the tests pass');
});

test('a command that is run again after each edit is work, whichever way VS Code edits', async () => {
  const run = (n) => ['run_in_terminal', { command: 'npm test', explanation: `Run the tests again (${n})`, isBackground: false }, 'tests ran'];
  const edit = (n) => ['replace_string_in_file', { filePath: 'src/a.ts', oldString: `old ${n}`, newString: `new ${n}` }, 'The file was edited.'];
  const client = fakeClient();
  const sent = await request(client, [system, task, ...steps(9, (n) => (n % 2 ? edit(n) : run(n)))]);
  assert.equal(sent.error, undefined);
  assert.equal(client.calls[0].messages[0].content, 'You are the coding agent.');
  // Without the edits it is the same command, with the same output, again and again, and an explanation worded afresh each time does not hide it.
  const bare = fakeClient();
  await request(bare, [system, task, ...steps(3, run)]);
  assert.equal(bare.calls[0].messages[0].content,
    'You are the coding agent.\n\nLoop check: you have been running `npm test` and getting the same output each time. That is not working. Try a different approach, or stop and tell the user what is blocking you.');
});

test('a file read in other parts each time is how a long file is read, not a loop', async () => {
  const chunk = (n) => ['read_file', { filePath: 'src/big.ts', startLine: n * 100 + 1, endLine: n * 100 + 100 }, `text ${n}`];
  const client = fakeClient();
  const sent = await request(client, [system, task, ...steps(9, chunk)]);
  assert.equal(sent.error, undefined);
  assert.equal(client.calls[0].messages[0].content, 'You are the coding agent.');
});

/** A coordinator on a local port that answers every chat request with `answer`, and keeps what it was sent. */
async function coordinator(answer) {
  const bodies = [];
  const server = http.createServer((req, res) => {
    if (req.url === '/v1/cluster/status') {
      res.setHeader('Content-Type', 'application/json');
      res.end(JSON.stringify({ status: 'Active', global_active_model: 'served.gguf', model_loading: { is_loading: false } }));
      return;
    }
    let data = '';
    req.on('data', (chunk) => (data += chunk));
    req.on('end', () => {
      bodies.push(JSON.parse(data));
      res.writeHead(200, { 'Content-Type': 'text/event-stream' });
      res.write(`data: ${JSON.stringify({ choices: [{ delta: { content: answer } }] })}\n\n`);
      res.end('data: [DONE]\n\n');
    });
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  return { bodies, url: `http://127.0.0.1:${server.address().port}`, close: () => { server.closeAllConnections(); server.close(); } };
}

test('on the wire, through the real client: the note is in the system message the coordinator gets, and a stopped request never reaches it', async () => {
  stub.reset();
  const c = await coordinator('Trying something else.');
  stub.state.settings['dlne.serverUrl'] = c.url;
  try {
    const client = new DlneClient();
    // Never the public swarm: this is a fake conversation.
    assert.match(client.getServerUrl(), /^http:\/\/127\.0\.0\.1:\d+$/);
    const nudged = await request(client, [system, task, ...steps(3, readA)]);
    assert.equal(nudged.error, undefined);
    assert.equal(nudged.text, 'Trying something else.');
    assert.equal(c.bodies.length, 1);
    assert.deepEqual(c.bodies[0].messages.map((m) => m.role), ['system', 'user', 'assistant', 'tool', 'assistant', 'tool', 'assistant', 'tool']);
    assert.equal(c.bodies[0].messages[0].content, `You are the coding agent.\n\n${NOTE}`);

    const history = [system, task, ...steps(5, readA)];
    const stopped = await request(client, history);
    assert.equal(stopped.error.message, STOPPED);
    assert.equal(c.bodies.length, 1, 'the coordinator heard nothing of the second request');

    // The user writes after the refusal: this request goes, with the stop's note in its system message, and the history as it was.
    const typed = message(Role.User, 'try the tests in test/ instead');
    const carried = await request(client, [...history, typed]);
    assert.equal(carried.error, undefined);
    assert.equal(c.bodies.length, 2);
    assert.equal(c.bodies[1].messages[0].content, `You are the coding agent.\n\n${STOP_NOTE}`);
    assert.deepEqual(c.bodies[1].messages.map((m) => m.role), ['system', 'user', ...Array(5).fill(['assistant', 'tool']).flat(), 'user']);
    assert.equal(c.bodies[1].messages.at(-1).content, 'try the tests in test/ instead');
  } finally {
    c.close();
  }
});

test('what the chat is told has no secret in it, and neither has the log', async () => {
  const token = 'gh' + 'p_' + generator(1519)('alnum', 36);
  const command = `curl -s -H "Authorization: Bearer ${token}" https://example.test/api/health`;
  const run = () => ['run_in_terminal', { command }, 'curl: (22) The requested URL returned error: 401'];
  const stopped = await request(fakeClient(), [system, task, ...steps(5, run)]);
  assert.ok(stopped.error);
  assert.ok(!stopped.error.message.includes(token.slice(0, 12)), stopped.error.message);
  assert.ok(stopped.error.message.includes('[REDACTED]'), stopped.error.message);
  assert.ok(!stopped.log.includes(token.slice(0, 12)));
  const client = fakeClient();
  await request(client, [system, task, ...steps(3, run)]);
  assert.ok(!client.calls[0].messages[0].content.includes(token.slice(0, 12)));
  // And the note that follows a refusal.
  const after = fakeClient();
  const told = await request(after, [system, task, ...steps(5, run), message(Role.User, 'go on')]);
  const systemMessage = after.calls[0].messages[0].content;
  assert.match(systemMessage, /DLNE stopped the last task because you kept running `curl .*\[REDACTED\]/);
  assert.ok(!systemMessage.includes(token.slice(0, 12)), systemMessage);
  assert.ok(!told.log.includes(token.slice(0, 12)));
});
