'use strict';
/**
 * The generate_image agent tool (2.6.1 contract): offered only while /v1/cluster/status says
 * `capabilities.image_generation`, the job flow (POST /v1/images, poll every 2 s, cancel) against a fake coordinator,
 * where the PNG may be saved and the approval it needs (the same as write_file), and how every refusal and failure
 * comes back as a tool error carrying the server's `detail`.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');

const stub = require('./helpers/vscode-stub');
const swarm = require('./helpers/mock-swarm');
const { DlneClient } = require('../dist/client/dlneClient');
const { AgentLoop, AGENT_SYSTEM_PROMPT } = require('../dist/agent/agentLoop');
const { ToolRegistry } = require('../dist/agent/tools');
const image = require('../dist/agent/imageTool');

process.env.HOME = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-image-home-'));

const realApprover = ToolRegistry.approver;
const realStore = ToolRegistry.store;
const AWS_KEY = 'AKIAIOSFODNN7EXAMPLE';
const JOB = 'k3J9xQ_aZ-0123456789abcdefghij';   // 128-bit, URL-safe
/** Not a picture anyone could look at, but it starts like a PNG, which is what the tool checks. */
const PNG = Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), Buffer.from('fake-image-bytes')]);
const done = (extra = {}) => ({ id: JOB, status: 'completed', elapsed_seconds: 79, image: { b64_png: PNG.toString('base64'), width: 768, height: 768, seed: 42 }, ...extra });

/**
 * A coordinator with image endpoints. `jobs.create(body)` answers the POST ({status, body, headers}) and
 * `jobs.poll(n)` the nth GET (n from 1). Every request is recorded.
 */
async function coordinator({ capabilities, create, poll, chat } = {}) {
  const requests = [];
  let polls = 0;
  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, 'http://x');
    let raw = '';
    for await (const piece of req) raw += piece;
    let body = null;
    try { body = raw ? JSON.parse(raw) : null; } catch { /* not JSON */ }
    requests.push({ method: req.method, path: url.pathname, headers: req.headers, body });
    const send = (status, payload, headers = {}) => {
      res.writeHead(status, { 'Content-Type': 'application/json', ...headers });
      res.end(JSON.stringify(payload));
    };
    if (url.pathname === '/v1/cluster/status') {
      return send(200, swarm.statusBody(capabilities === undefined ? {} : { capabilities }));
    }
    if (url.pathname === '/v1/chat/completions' && chat) return chat({ req, res, body });
    if (url.pathname === '/v1/images' && req.method === 'POST') {
      const answer = create ? create(body) : { status: 202, body: { id: JOB, status: 'queued', position: 1, eta_seconds: 80 } };
      return send(answer.status, answer.body, answer.headers);
    }
    if (url.pathname === `/v1/images/${JOB}` && req.method === 'GET') {
      const answer = poll ? poll(++polls) : { status: 200, body: done() };
      return send(answer.status, answer.body, answer.headers);
    }
    if (url.pathname === `/v1/images/${JOB}/cancel` && req.method === 'POST') return send(200, { id: JOB, status: 'cancelled' });
    return send(404, { detail: 'Not Found' });
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const url = `http://127.0.0.1:${server.address().port}`;
  stub.state.settings['dlne.serverUrl'] = url;
  return {
    url,
    requests,
    client: new DlneClient(),
    of: (p) => requests.filter((r) => r.path === p),
    images: () => requests.filter((r) => r.path.startsWith('/v1/images')),
    close() { server.closeAllConnections(); server.close(); },
  };
}

/** A workspace folder, a fresh per-project store, and an approver that answers `answers` in turn (then "deny"). */
async function project(body, { answers = ['once'], folder = true, coord } = {}) {
  stub.reset();
  ToolRegistry.resetApprovals();
  const saved = new Map();
  ToolRegistry.store = { get: (key) => saved.get(key), update: async (key, value) => { saved.set(key, value); } };
  const asked = [];
  ToolRegistry.approver = async (req) => { asked.push(req); return answers.length ? answers.shift() : 'deny'; };
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-image-ws-'));
  const outside = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-image-outside-'));
  stub.vscode.workspace.workspaceFolders = folder ? [{ uri: { fsPath: root } }] : undefined;
  const c = await coordinator(coord);
  try {
    await body({ root, outside, asked, saved, c });
  } finally {
    c.close();
    stub.vscode.workspace.workspaceFolders = undefined;
    ToolRegistry.approver = realApprover;
    ToolRegistry.store = realStore;
    fs.rmSync(root, { recursive: true, force: true });
    fs.rmSync(outside, { recursive: true, force: true });
  }
}

/** executeTool with the tool switched on against the fake coordinator, polls not waited for. */
const generate = (c, args, extra = {}) =>
  ToolRegistry.executeTool('generate_image', args, false, {
    images: { request: (m, p, b, s) => c.client.imageRequest(m, p, b, s) },
    sleep: async () => {},
    ...extra,
  });

// ---- the capability gate ---------------------------------------------------------------------------------------

test('the tool is offered only when /v1/cluster/status says capabilities.image_generation is true', () => {
  assert.equal(image.imageGenerationOffered({ capabilities: { image_generation: true } }), true);
  for (const status of [undefined, null, {}, { capabilities: {} }, { capabilities: { image_generation: false } },
    { capabilities: { image_generation: 'true' } }, { capabilities: { image_generation: 1 } }, { image_generation: { queue: 0 } }]) {
    assert.equal(image.imageGenerationOffered(status), false, JSON.stringify(status));
  }
  const names = ToolRegistry.getToolDefinitions().map((t) => t.function.name);
  assert.ok(!names.includes('generate_image'), 'the fixed list (pinned by the canary parity test) does not carry it');
  const def = image.imageToolDefinition().function;
  assert.equal(def.name, 'generate_image');
  assert.deepEqual(Object.keys(def.parameters.properties), ['prompt', 'path', 'size']);
  assert.deepEqual(def.parameters.required, ['prompt']);
});

async function agentRequest(capabilities) {
  stub.reset();
  const bodies = [];
  const c = await coordinator({ capabilities, chat: ({ res, body }) => { bodies.push(body); swarm.plainStream(res, ['ok']); } });
  try {
    await c.client.getClusterStatusDetailed();   // what the status bar's poll does
    const loop = new AgentLoop(c.client);
    await loop.run('hello', { onThinkingDelta() {}, onContentDelta() {}, onToolStart() {}, onToolEnd() {} });
    return bodies[0];
  } finally {
    c.close();
  }
}

test('the agent sends generate_image and its prompt line only to a coordinator that reports the capability', async () => {
  for (const capabilities of [undefined, {}, { image_generation: false }]) {
    const body = await agentRequest(capabilities);
    assert.ok(!body.tools.some((t) => t.function.name === 'generate_image'), JSON.stringify(capabilities));
    assert.equal(body.messages[0].content, AGENT_SYSTEM_PROMPT, 'the prompt is exactly the pinned one');
  }
  const body = await agentRequest({ image_generation: true });
  assert.equal(body.tools.at(-1).function.name, 'generate_image');
  assert.equal(body.tools.length, ToolRegistry.getToolDefinitions().length + 1);
  assert.match(body.messages[0].content, /- generate_image\(prompt, path, size\)/);
  assert.ok(body.messages[0].content.startsWith(AGENT_SYSTEM_PROMPT.slice(0, 400)));
});

test('a call to the tool while it is not offered is refused, with nothing sent', async () => {
  await project(async ({ c }) => {
    const r = await ToolRegistry.executeTool('generate_image', { prompt: 'a red fox' });
    assert.equal(r.is_error, true);
    assert.match(r.output, /not available/);
    assert.equal(c.images().length, 0);
  });
});

// ---- the job flow ----------------------------------------------------------------------------------------------

test('a job is submitted, polled until it completes, and saved: progress is elapsed time against the estimate', async () => {
  const states = [
    { id: JOB, status: 'queued', position: 1, eta_seconds: 80, elapsed_seconds: 0 },
    { id: JOB, status: 'checking', elapsed_seconds: 2 },
    { id: JOB, status: 'generating', elapsed_seconds: 10, eta_seconds: 80 },
    { id: JOB, status: 'reviewing', elapsed_seconds: 78 },
  ];
  await project(async ({ root, asked, c }) => {
    const lines = [];
    const r = await generate(c, { prompt: 'A red fox, in a snowy forest!', size: '768x768' }, { onProgress: (t) => lines.push(t) });
    assert.equal(r.is_error, undefined, r.output);
    assert.equal(r.output, 'Saved images/a-red-fox-in-a-snowy-forest.png (768×768, seed 42)');
    assert.equal(r.imagePath, 'images/a-red-fox-in-a-snowy-forest.png');
    assert.deepEqual(fs.readFileSync(path.join(root, r.imagePath)), PNG);

    const [post, ...rest] = c.images();
    assert.equal(post.method, 'POST');
    assert.deepEqual(post.body, { prompt: 'A red fox, in a snowy forest!', size: '768x768' }, 'no chat_id, nothing else');
    assert.deepEqual(rest.map((x) => `${x.method} ${x.path}`), Array(5).fill(`GET /v1/images/${JOB}`));
    assert.ok(lines.some((l) => /Waiting for the image engine \(place 1\)/.test(l)));
    assert.ok(lines.some((l) => /Creating your image\.\.\. 10 s of about 80 s/.test(l)), lines.join('|'));
    assert.ok(lines.some((l) => /Checking the prompt/.test(l)) && lines.some((l) => /Checking the picture/.test(l)));
    assert.equal(asked.length, 1);
    assert.equal(c.of('/v1/coding/route').length, 0, 'the prompt goes to the coordinator itself, never to a coding member');
  }, { coord: { poll: (n) => ({ status: 200, body: n <= states.length ? states[n - 1] : done() }) } });
});

test('through the real client: auth header, a 2 s poll by default, and the size is left out when none is asked for', async () => {
  await project(async ({ root, c }) => {
    stub.state.settings['dlne.apiKey'] = 'key-123';
    const realSetTimeout = global.setTimeout;
    const waits = [];
    global.setTimeout = (fn, ms, ...rest) => { if (ms === 2000) { waits.push(ms); return realSetTimeout(fn, 0); } return realSetTimeout(fn, ms, ...rest); };
    try {
      const r = await ToolRegistry.executeTool('generate_image', { prompt: 'icon' }, false, {
        images: { request: (m, p, b, s) => c.client.imageRequest(m, p, b, s) },
      });
      assert.equal(r.is_error, undefined, r.output);
      assert.equal(r.imagePath, 'images/icon.png');
    } finally {
      global.setTimeout = realSetTimeout;
    }
    assert.deepEqual(waits, [2000]);
    assert.equal(c.images()[0].body.size, undefined);
    assert.equal(c.images()[0].headers.authorization, 'Bearer key-123');
    assert.ok(fs.existsSync(path.join(root, 'images', 'icon.png')));
  });
});

test('stopping the agent run cancels the job at the coordinator and saves nothing', async () => {
  await project(async ({ root, c }) => {
    const stop = new AbortController();
    const r = await generate(c, { prompt: 'a long wait' }, { signal: stop.signal, sleep: async () => { stop.abort(); } });
    assert.equal(r.is_error, true);
    assert.match(r.output, /^Cancelled/);
    assert.deepEqual(c.images().map((x) => `${x.method} ${x.path}`), ['POST /v1/images', `POST /v1/images/${JOB}/cancel`]);
    assert.ok(!fs.existsSync(path.join(root, 'images')));
  });
});

test('a prompt that holds a secret goes out with it hidden', async () => {
  await project(async ({ c }) => {
    await generate(c, { prompt: `a poster saying ${AWS_KEY}` });
    assert.ok(!JSON.stringify(c.images()[0].body).includes(AWS_KEY));
  });
});

// ---- where the picture may be saved, and who has to say yes --------------------------------------------------

test('the default name is images/<slug of the prompt>.png, numbered when it is taken, and a path without .png gets it', () => {
  assert.equal(image.slugOfPrompt('A Red Fox -- in the snow!!'), 'a-red-fox-in-the-snow');
  assert.equal(image.slugOfPrompt('Café  naïve'), 'cafe-naive');
  assert.equal(image.slugOfPrompt('!!!'), 'image');
  assert.ok(image.slugOfPrompt('word '.repeat(30)).length <= 40);
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-image-slug-'));
  try {
    const first = image.prepareImageArgs({ prompt: 'a fox' }, root);
    assert.equal(first.args.path, path.join(root, 'images', 'a-fox.png'));
    fs.mkdirSync(path.join(root, 'images'));
    fs.writeFileSync(path.join(root, 'images', 'a-fox.png'), 'old');
    assert.equal(image.prepareImageArgs({ prompt: 'a fox' }, root).args.path, path.join(root, 'images', 'a-fox-2.png'), 'a default never overwrites');
    assert.equal(image.prepareImageArgs({ prompt: 'a fox', path: 'art/logo' }, root).args.path, path.join(root, 'art', 'logo.png'));
    assert.equal(image.prepareImageArgs({ prompt: 'a fox', size: '1024×768' }, root).args.size, '1024x768');
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('the path must be a .png inside the workspace, the size one the coordinator serves: refused before anything is sent', async () => {
  await project(async ({ root, outside, asked, c }) => {
    fs.symlinkSync(outside, path.join(root, 'link'), 'dir');
    const refused = [
      [{ prompt: 'x', path: '../escape.png' }, /outside the workspace/],
      [{ prompt: 'x', path: path.join(outside, 'abs.png') }, /outside the workspace/],
      [{ prompt: 'x', path: '~/pic.png' }, /./],
      [{ prompt: 'x', path: 'link/pic.png' }, /leads outside the workspace/],
      [{ prompt: 'x', path: 'notes.txt' }, /must end in \.png/],
      [{ prompt: 'x', path: '.' }, /./],
      [{ prompt: 'x', path: '.dlne/memory.png' }, /persistent memory/],
      [{ prompt: 'x', size: '512x512' }, /size must be one of/],
      [{ prompt: '   ' }, /needs a prompt/],
      [{}, /needs a prompt/],
    ];
    for (const [args, message] of refused) {
      const r = await generate(c, args);
      assert.equal(r.is_error, true, JSON.stringify(args));
      assert.match(r.output, message, JSON.stringify(args));
    }
    assert.equal(c.images().length, 0, 'no job was spent on any of them');
    assert.equal(asked.length, 0);
    assert.deepEqual(fs.readdirSync(outside), []);
  });
});

test('with no folder open there is nowhere to save: refused', async () => {
  await project(async ({ c }) => {
    const r = await generate(c, { prompt: 'a fox' });
    assert.equal(r.is_error, true);
    assert.match(r.output, /open a folder/);
    assert.equal(c.images().length, 0);
  }, { folder: false });
});

test('the save is asked about like write_file, before the job: declined means no job and no file', async () => {
  await project(async ({ root, asked, c }) => {
    const r = await generate(c, { prompt: 'a fox', path: 'art/fox.png' });
    assert.equal(r.is_error, true);
    assert.match(r.output, /^The user declined: create art[\\/]fox\.png with a generated image/);
    assert.equal(asked.length, 1);
    assert.equal(asked[0].tool, 'generate_image');
    assert.equal(asked[0].sessionAllowed, true);
    assert.match(asked[0].always, /Always allow edits/);
    assert.match(asked[0].detail, /Prompt: a fox/);
    assert.equal(c.images().length, 0);
    assert.ok(!fs.existsSync(path.join(root, 'art')));
  }, { answers: ['deny'] });
});

test('an existing file is overwritten only after the user is asked to overwrite it', async () => {
  await project(async ({ root, asked, c }) => {
    fs.writeFileSync(path.join(root, 'logo.png'), 'precious');
    const denied = await generate(c, { prompt: 'a logo', path: 'logo.png' });
    assert.equal(denied.is_error, true);
    assert.match(asked[0].summary, /^overwrite logo\.png/);
    assert.equal(fs.readFileSync(path.join(root, 'logo.png'), 'utf8'), 'precious');
    assert.equal(c.images().length, 0);
    const allowed = await generate(c, { prompt: 'a logo', path: 'logo.png' });
    assert.equal(allowed.is_error, undefined, allowed.output);
    assert.deepEqual(fs.readFileSync(path.join(root, 'logo.png')), PNG);
  }, { answers: ['deny', 'once'] });
});

test('"allow edits" (this session, or always) covers pictures inside the workspace, as it covers write_file', async () => {
  await project(async ({ saved, asked, c }) => {
    const first = await generate(c, { prompt: 'one' });
    assert.equal(first.is_error, undefined, first.output);
    const second = await generate(c, { prompt: 'two' });
    assert.equal(second.is_error, undefined, second.output);
    assert.equal(asked.length, 1, 'the session answer holds for the next picture');
    assert.equal(saved.size, 0);
  }, { answers: ['session'] });
  await project(async ({ saved, asked, c }) => {
    await generate(c, { prompt: 'one' });
    assert.equal(saved.get('dlne.agent.alwaysAllowEdits'), true, '"Always allow edits" is remembered for the project');
    await generate(c, { prompt: 'two' });
    assert.equal(asked.length, 1);
  }, { answers: ['always'] });
  await project(async ({ asked, c }) => {
    await ToolRegistry.executeTool('generate_image', { prompt: 'three' }, true, {
      images: { request: (m, p, b, s) => c.client.imageRequest(m, p, b, s) }, sleep: async () => {},
    });
    assert.equal(asked.length, 0, 'a chat that approves everything is not asked');
  });
});

// ---- errors ----------------------------------------------------------------------------------------------------

const fake = (replies) => {
  const calls = [];
  return {
    calls,
    request: async (method, apiPath, body) => {
      calls.push(`${method} ${apiPath}`);
      const reply = replies.shift();
      return typeof reply === 'function' ? reply(body) : reply;
    },
  };
};
const args = { prompt: 'a fox', path: '/w/a.png' };
const run = (api) => image.runImageJob(api, args, { sleep: async () => {} });
const accepted = { status: 202, body: { id: JOB, status: 'queued', position: 1, eta_seconds: 80 } };

test('a refused submission comes back with the server\'s detail: 400, 429 (and how long to wait), 503', async () => {
  for (const [status, body, headers, expected] of [
    [400, { detail: 'That prompt is empty.', code: 'prompt_empty' }, {}, 'That prompt is empty.'],
    [400, { detail: 'I can\'t draw that, but I can help with something else.', code: 'prompt_refused', category: 'violence' }, {}, 'I can\'t draw that, but I can help with something else.'],
    [429, { detail: 'You have reached the hourly limit for pictures.', code: 'image_quota' }, { retryAfter: 1500 }, 'You have reached the hourly limit for pictures. Try again in 25 min.'],
    [429, { detail: 'The image engine is busy.', code: 'image_busy' }, { retryAfter: 30 }, 'The image engine is busy. Try again in 30 s.'],
    [503, { detail: 'Image generation is not available right now.', code: 'image_generation_unavailable' }, {}, 'Image generation is not available right now.'],
    [500, 'not json', {}, 'The coordinator answered HTTP 500.'],
  ]) {
    const api = fake([{ status, body, ...headers }]);
    const out = await run(api);
    assert.equal(out.ok, false);
    assert.equal(out.text, expected);
    assert.deepEqual(api.calls, ['POST /v1/images'], 'no polling after a refusal');
  }
});

test('refused, failed, cancelled and expired jobs are tool errors with the server\'s own words', async () => {
  for (const [job, expected] of [
    [{ status: 'refused', error: { detail: 'I can\'t make pictures of real people.', code: 'result_refused' } }, 'I can\'t make pictures of real people.'],
    [{ status: 'failed', error: { detail: 'The image engine had a problem. Try again.', code: 'image_failed' } }, 'The image engine had a problem. Try again.'],
    [{ status: 'failed' }, 'The image could not be created.'],
    [{ status: 'cancelled' }, 'The image job was cancelled.'],
    [{ status: 'expired' }, /expired/],
    [{ status: 'mystery' }, /Unexpected image job status: mystery/],
  ]) {
    const out = await run(fake([accepted, { status: 200, body: { id: JOB, ...job } }]));
    assert.equal(out.ok, false);
    if (expected instanceof RegExp) assert.match(out.text, expected); else assert.equal(out.text, expected);
  }
});

test('a poll that fails is retried a few times, a job that is gone is final, and something that is not a PNG is refused', async () => {
  const blip = { status: 0, body: { detail: 'socket hang up' } };
  const working = { status: 200, body: { id: JOB, status: 'generating', elapsed_seconds: 5, eta_seconds: 80 } };
  const ok = await run(fake([accepted, blip, blip, working, blip, { status: 200, body: done() }]));
  assert.equal(ok.ok, true, 'two blips in a row, then work, then one more: not three in a row');
  assert.equal(ok.width, 768);

  const dead = fake([accepted, blip, blip, blip, { status: 200, body: done() }]);
  const gaveUp = await run(dead);
  assert.equal(gaveUp.ok, false);
  assert.equal(gaveUp.text, 'socket hang up');
  assert.equal(dead.calls.at(-1), `POST /v1/images/${JOB}/cancel`, 'and the job is cancelled');

  const gone = await run(fake([accepted, { status: 404, body: { detail: 'Not Found' } }]));
  assert.equal(gone.text, 'Not Found');

  for (const png of ['', 'bm90IGEgcG5n', Buffer.alloc(8).toString('base64')]) {
    const bad = await run(fake([accepted, { status: 200, body: done({ image: { b64_png: png, width: 768, height: 768, seed: 1 } }) }]));
    assert.equal(bad.ok, false, png);
    assert.match(bad.text, /not a PNG/);
  }
  const noImage = await run(fake([accepted, { status: 200, body: { id: JOB, status: 'completed' } }]));
  assert.match(noImage.text, /not a PNG/);
});

test('a job id that could be a path or a query is never put into a URL', async () => {
  for (const id of ['../../v1/chat/completions', 'a b c d e f g h', '', 5, null]) {
    const api = fake([{ status: 202, body: { id, status: 'queued' } }]);
    const out = await run(api);
    assert.equal(out.ok, false);
    assert.deepEqual(api.calls, ['POST /v1/images']);
  }
});

test('through the tool: an error from the coordinator is a tool error, no file is written, and the model reads the detail', async () => {
  await project(async ({ root, c }) => {
    const r = await generate(c, { prompt: 'a fox' });
    assert.equal(r.is_error, true);
    assert.equal(r.output, 'You have reached the hourly limit for pictures. Try again in 10 min.');
    assert.ok(!fs.existsSync(path.join(root, 'images')));
  }, { coord: { create: () => ({ status: 429, body: { detail: 'You have reached the hourly limit for pictures.', code: 'image_quota' }, headers: { 'Retry-After': '600' } }) } });
  await project(async ({ root, c }) => {
    const r = await generate(c, { prompt: 'a fox' });
    assert.equal(r.is_error, true);
    assert.equal(r.output, 'I can\'t make pictures of real people.');
    assert.ok(!fs.existsSync(path.join(root, 'images')));
  }, { coord: { poll: () => ({ status: 200, body: { id: JOB, status: 'refused', error: { detail: 'I can\'t make pictures of real people.', code: 'result_refused' } } }) } });
  await project(async ({ c }) => {
    const r = await generate(c, { prompt: 'a fox' });
    assert.equal(r.output, 'Image generation is not available right now.');
  }, { coord: { create: () => ({ status: 503, body: { detail: 'Image generation is not available right now.', code: 'image_generation_unavailable' } }) } });
});

test('the agent loop knows the tool: an inline call is recognised, and the saved task is written at once after one', () => {
  const loop = fs.readFileSync(path.join(__dirname, '..', 'src', 'agent', 'agentLoop.ts'), 'utf8');
  assert.match(loop, /VALID_TOOLS = new Set\([\s\S]*IMAGE_TOOL_NAME/);
  assert.match(loop, /MACHINE_CHANGING_TOOLS = new Set\([^)]*IMAGE_TOOL_NAME/);
  assert.match(loop, /remember\|use_skill\|save_skill\|generate_image\|view_image\)"/);
});
