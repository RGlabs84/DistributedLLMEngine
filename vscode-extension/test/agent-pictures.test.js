'use strict';
/**
 * The agent sees pictures (docs/release-2.7-contract.md 4d, docs/agent-memory.md sections 1 and 3): the picture helpers,
 * how a picture travels in a request (image_url parts, never sealed to a coding member), the agent loop around it (only
 * while the coordinator says `capabilities.vision`, old pictures giving way, a compaction dropping them, a refusal in plain
 * words), the view_image tool, DLNE Swarm in the Chat view, and the panel's paste, drop and attach.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const stub = require('./helpers/vscode-stub');
const lm = require('./helpers/lm-stub');
const swarm = require('./helpers/mock-swarm');

process.env.HOME = fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-pictures-home-'));
const restoreOpener = swarm.useContractOpenerIfNeeded();
test.after(restoreOpener);

const pic = require('../dist/client/pictures');
const msg = require('../dist/client/lmMessages');
const prov = require('../dist/client/modelProvider');
const { DlneClient, ServerRefusal } = require('../dist/client/dlneClient');
const { redactPayload, forgetPrivacyModes } = require('../dist/privacy');
const { prepareRequest } = require('../dist/client/pipeline');
const { AgentLoop, AGENT_SYSTEM_PROMPT } = require('../dist/agent/agentLoop');
const { ToolRegistry } = require('../dist/agent/tools');
const { viewImageToolDefinition, VIEW_IMAGE_PROMPT_LINE, withViewImageLine } = require('../dist/agent/viewImage');

// ---- fake picture files ----------------------------------------------------------------------------------------------

function png(width = 64, height = 48, extra = 0) {
  const b = Buffer.alloc(33 + extra);
  Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]).copy(b);
  b.writeUInt32BE(13, 8);
  b.write('IHDR', 12, 'latin1');
  b.writeUInt32BE(width, 16);
  b.writeUInt32BE(height, 20);
  return b;
}

function jpeg(width = 80, height = 60) {
  const app0 = Buffer.concat([Buffer.from([0xff, 0xe0, 0x00, 0x10]), Buffer.from('JFIF\0\x01\x01\0\0\x01\0\x01\0\0', 'latin1')]);
  const sof = Buffer.from([0xff, 0xc0, 0x00, 0x11, 0x08, height >> 8, height & 255, width >> 8, width & 255, 0x03, 1, 0x22, 0, 2, 0x11, 1, 3, 0x11, 1]);
  return Buffer.concat([Buffer.from([0xff, 0xd8]), app0, sof, Buffer.from([0xff, 0xd9])]);
}

function gif(width = 10, height = 12) {
  const b = Buffer.alloc(16);
  b.write('GIF89a', 0, 'latin1');
  b.writeUInt16LE(width, 6);
  b.writeUInt16LE(height, 8);
  return b;
}

function webp(width = 30, height = 20) {
  const b = Buffer.alloc(34);
  b.write('RIFF', 0, 'latin1');
  b.writeUInt32LE(26, 4);
  b.write('WEBPVP8X', 8, 'latin1');
  b.writeUInt32LE(10, 16);
  b.writeUIntLE(width - 1, 24, 3);
  b.writeUIntLE(height - 1, 27, 3);
  return b;
}

const uriOf = (bytes, mime = 'image/png') => `data:${mime};base64,${bytes.toString('base64')}`;
const attached = (name, bytes = png()) => ({ name, dataUri: uriOf(bytes, bytes[0] === 0xff ? 'image/jpeg' : 'image/png') });

// ---- the helpers -------------------------------------------------------------------------------------------------------

test('a picture is told from its own first bytes, with its size: PNG, JPEG, GIF and WebP', () => {
  assert.deepEqual(pic.sniffPicture(png(64, 48)), { mime: 'image/png', width: 64, height: 48 });
  assert.deepEqual(pic.sniffPicture(jpeg(80, 60)), { mime: 'image/jpeg', width: 80, height: 60 });
  assert.deepEqual(pic.sniffPicture(gif(10, 12)), { mime: 'image/gif', width: 10, height: 12 });
  assert.deepEqual(pic.sniffPicture(webp(30, 20)), { mime: 'image/webp', width: 30, height: 20 });
  for (const other of [Buffer.from('hello world, not a picture at all'), Buffer.alloc(0), Buffer.from([0xff, 0xd8])]) assert.equal(pic.sniffPicture(other), null);
});

test('a picture file is taken as an attachment only when it is a PNG or JPEG within the limits, and refused in plain words otherwise', () => {
  const ok = pic.pictureFromBytes('shots/home page.png', png(64, 48));
  assert.equal(ok.ok, true);
  assert.deepEqual({ ...ok.picture, dataUri: ok.picture.dataUri.slice(0, 22) }, { name: 'home page.png', dataUri: 'data:image/png;base64,', width: 64, height: 48 });
  assert.equal(pic.pictureFromBytes('a.jpg', jpeg()).picture.dataUri.startsWith('data:image/jpeg;base64,'), true);
  assert.match(pic.pictureFromBytes('a.gif', gif()).error, /PNG and JPEG pictures, and this is a GIF: convert it to PNG first/);
  assert.match(pic.pictureFromBytes('a.webp', webp()).error, /this is a WEBP/);
  assert.match(pic.pictureFromBytes('a.txt', Buffer.from('plain text, nothing else')).error, /not a PNG, JPEG, WebP or GIF picture/);
  assert.match(pic.pictureFromBytes('big.png', png(64, 48, 4 * 1024 * 1024)).error, /it is 4\.0 MiB, and a picture may be 4 MiB at most/);
  assert.equal(pic.pictureFromBytes('edge.png', png(64, 48, 4 * 1024 * 1024 - 33)).ok, true, '4 MiB exactly is allowed');
  assert.match(pic.pictureFromBytes('wide.png', png(5000, 100)).error, /5000x100, and a side may be 4096 pixels at most/);
});

test('names are made fit to show: the last part of a path, no brackets or control characters, 60 characters', () => {
  assert.equal(pic.pictureName('C:\\Users\\me\\shot.png'), 'shot.png');
  assert.equal(pic.pictureName('a/b/[x]<y>.png'), 'xy.png');
  assert.equal(pic.pictureName('n\u0000a\nme.png'), 'name.png');
  assert.equal(pic.pictureName('x'.repeat(200)).length, 60);
  assert.equal(pic.pictureName(undefined), 'picture');
  assert.equal(pic.pictureNote('a.png'), '[picture: a.png]');
  assert.equal(pic.shownEarlier('a.png'), '[picture: a.png, shown earlier]');
});

test('what the panel hands over is checked again: well-formed PNG or JPEG data URIs within the limits, at most four', () => {
  const good = [attached('one.png'), attached('two.jpg', jpeg())];
  const mixed = [
    ...good,
    { name: 'link.png', dataUri: 'https://example.org/x.png' },                  // never fetched
    { name: 'svg.svg', dataUri: 'data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=' },
    { name: 'liar.jpg', dataUri: uriOf(png(), 'image/jpeg') },                    // PNG bytes under a JPEG label
    { name: 'text.png', dataUri: uriOf(Buffer.from('not a picture, only words'), 'image/png') },
    null, 7, {},
  ];
  const checked = pic.checkPictures(mixed);
  assert.deepEqual(checked.pictures.map((p) => p.name), ['one.png', 'two.jpg']);
  assert.equal(checked.problems.length, 7);
  assert.match(checked.problems.join('\n'), /liar\.jpg: its bytes are not the JPEG it says/);
  assert.deepEqual(pic.checkPictures('nothing'), { pictures: [], problems: [] });
  const six = pic.checkPictures(Array.from({ length: 6 }, (_, i) => attached(`p${i}.png`)));
  assert.equal(six.pictures.length, 4);
  assert.deepEqual(six.problems, ['p4.png: a message may carry 4 pictures, so this one was left out', 'p5.png: a message may carry 4 pictures, so this one was left out']);
});

test('a request writes a message\'s pictures as image_url parts after its text, and changes nothing else', () => {
  const a = pic.pictureFromBytes('a.png', png()).picture;
  const b = pic.pictureFromBytes('b.jpg', jpeg()).picture;
  const wire = pic.wireMessages([
    { role: 'system', content: 'sys' },
    { role: 'user', content: 'look at these', images: [a, b], pictureTurn: 2 },
    { role: 'user', content: '', images: [a] },
    { role: 'assistant', content: 'ok', images: [a] },        // only the user's side carries pictures
    { role: 'tool', tool_call_id: 'c', content: 'text' },
  ]);
  assert.deepEqual(wire[0], { role: 'system', content: 'sys' });
  assert.deepEqual(wire[1], { role: 'user', content: [
    { type: 'text', text: 'look at these' }, { type: 'image_url', image_url: { url: a.dataUri } }, { type: 'image_url', image_url: { url: b.dataUri } },
  ] });
  assert.deepEqual(wire[2].content, [{ type: 'image_url', image_url: { url: a.dataUri } }], 'no empty text part');
  assert.deepEqual(wire[3], { role: 'assistant', content: 'ok' });
  assert.deepEqual(wire[4], { role: 'tool', tool_call_id: 'c', content: 'text' });
  assert.ok(wire.every((m) => !('images' in m) && !('pictureTurn' in m)), 'what is only the agent\'s is never sent');
  assert.equal(pic.carriesPicture({ messages: wire }), true);
  assert.equal(pic.carriesPicture({ messages: [wire[0], wire[4]] }), false);
  assert.equal(pic.carriesPicture({}), false);
});

test('pictures of old turns become notes, no more than four go out, and the newest are the ones kept', () => {
  const p = (n) => ({ name: `p${n}.png`, dataUri: 'data:image/png;base64,AAAA' });
  const history = [
    { role: 'user', content: 'turn 1', images: [p(1)], pictureTurn: 1 },
    { role: 'assistant', content: 'a' },
    { role: 'user', content: 'turn 2', images: [p(2)], pictureTurn: 2 },
    { role: 'user', content: '', images: [p(3)], pictureTurn: 3 },
    { role: 'user', content: 'turn 4', images: [p(4)], pictureTurn: 4 },
  ];
  const out = pic.withoutOldPictures(history, 4);
  assert.equal(out[0].images, undefined);
  assert.equal(out[0].content, 'turn 1\n[picture: p1.png, shown earlier]');
  assert.equal(out[1], history[1]);
  assert.deepEqual(out.slice(2).map((m) => m.images?.length), [1, 1, 1], 'the last three turns keep theirs');
  assert.equal(history[0].images.length, 1, 'the history itself is not changed');
  // a message with the word "turn" 3 with no text gets only the note when its picture goes
  assert.equal(pic.withoutOldPictures([{ role: 'user', content: '', images: [p(3)], pictureTurn: 1 }], 9)[0].content, '[picture: p3.png, shown earlier]');
  // five pictures in the last three turns: the newest four
  const crowd = [
    { role: 'user', content: 'x', images: [p(1), p(2), p(3)], pictureTurn: 4 },
    { role: 'user', content: 'y', images: [p(4), p(5)], pictureTurn: 4 },
  ];
  const kept = pic.withoutOldPictures(crowd, 4);
  assert.deepEqual(kept[1].images.map((q) => q.name), ['p4.png', 'p5.png']);
  assert.deepEqual(kept[0].images.map((q) => q.name), ['p1.png', 'p2.png']);
  assert.match(kept[0].content, /\[picture: p3\.png, shown earlier\]$/);
  // and for a saved chat: all of them, as notes of the caller's choice
  assert.equal(pic.withoutPictures(crowd, pic.pictureNote)[0].content, 'x\n[picture: p1.png] [picture: p2.png] [picture: p3.png]');
  assert.equal(pic.holdsPictures(crowd), true);
  assert.equal(pic.holdsPictures(pic.withoutPictures(crowd)), false);
  assert.equal(pic.visionOffered({ capabilities: { vision: true } }), true);
  for (const no of [null, {}, { capabilities: {} }, { capabilities: { vision: 'yes' } }, { capabilities: { vision: false } }]) assert.equal(pic.visionOffered(no), false);
});

test('secrets in the text of a picture message are hidden, and the picture itself is left alone', () => {
  const key = 'AKIAIOSFODNN7EXAMPLE';
  const picture = pic.pictureFromBytes('a.png', png()).picture;
  const body = { messages: pic.wireMessages([{ role: 'user', content: `my key is ${key}`, images: [picture] }]) };
  const { payload, count } = redactPayload(body);
  assert.ok(count >= 1);
  assert.ok(!payload.messages[0].content[0].text.includes(key));
  assert.equal(payload.messages[0].content[1].image_url.url, picture.dataUri);
});

// ---- the request: never sealed to a member -------------------------------------------------------------------------------

async function world({ members = true, chat } = {}) {
  stub.reset();
  forgetPrivacyModes();
  const member = swarm.createMember();
  let routes = 0;
  const c = await swarm.startCoordinator({
    status: swarm.statusBody({ ...(members ? swarm.MEMBERS_ON : {}), capabilities: { vision: true } }),
    route: () => swarm.offer(member, `route-${++routes}`),
    chat: chat || ((ctx) => swarm.plainStream(ctx.res, ['seen'])),
  });
  stub.state.settings['dlne.serverUrl'] = c.url;
  return { c, client: new DlneClient(), close: () => c.close() };
}

async function say(client, messages) {
  let out = '';
  await client.streamChat(messages, [], (delta) => { out += delta; }, undefined, undefined, () => {}, {});
  return out;
}

test('a request with a picture goes to the coordinator\'s own engine in plain: no route lookup, no seal, image_url parts', async () => {
  const w = await world();
  try {
    const picture = pic.pictureFromBytes('shot.png', png(64, 48)).picture;
    const out = await say(w.client, [{ role: 'user', content: 'what is this? key AKIAIOSFODNN7EXAMPLE', images: [picture] }]);
    assert.equal(out, 'seen');
    assert.equal(w.c.of('/v1/coding/route').length, 0, 'a member would refuse the picture, so none is asked for');
    const [chat] = w.c.of('/v1/chat/completions');
    assert.equal(chat.body.dlne_e2e, undefined);
    assert.equal(chat.headers['x-dlne-route'], undefined);
    assert.equal(chat.headers['x-dlne-privacy'], 'trusted');
    const parts = chat.body.messages[0].content;
    assert.equal(parts[0].type, 'text');
    assert.ok(!parts[0].text.includes('AKIAIOSFODNN7EXAMPLE'));
    assert.deepEqual(parts[1], { type: 'image_url', image_url: { url: picture.dataUri } });
    // the same chat without the picture is still routed to a member and sealed
    await say(w.client, [{ role: 'user', content: 'and without it' }]);
    assert.ok(w.c.of('/v1/coding/route').length >= 1, 'asked for a route');
    assert.ok(w.c.of('/v1/chat/completions').some((r) => r.body && r.body.dlne_e2e), 'and sealed to the member');
  } finally {
    w.close();
  }
});

test('the pipeline itself skips the route lookup for a body that carries a picture', async () => {
  let asked = 0;
  const env = { base: 'http://x', headers: () => ({}), routes: { lookup: async () => { asked++; return null; } } };
  const picture = pic.pictureFromBytes('a.png', png()).picture;
  const withPicture = { messages: pic.wireMessages([{ role: 'user', content: 'hi', images: [picture] }]) };
  const ctx = { sessionId: 's', membersEnabled: true, routeTtlMs: 0 };
  const plain = await prepareRequest(env, withPicture, ctx);
  assert.equal(asked, 0);
  assert.equal(plain.route, undefined);
  assert.equal(plain.respKey, undefined);
  assert.equal(JSON.parse(plain.body).messages[0].content[1].image_url.url, picture.dataUri);
  await prepareRequest(env, { messages: [{ role: 'user', content: 'hi' }] }, ctx);
  assert.equal(asked, 1, 'a request without one still asks');
});

test('a refusal carries the body\'s code, so a refused picture can be told from any other refusal', async () => {
  const { serverErrorCode } = require('../dist/client/dlneClient');
  assert.equal(serverErrorCode('{"detail":"x","code":"image_quota"}'), 'image_quota');
  for (const none of ['', 'not json', '{"detail":"x"}', '{"code":7}', '{"code":"Not A Code!"}']) assert.equal(serverErrorCode(none), undefined);
  const w = await world({ chat: (ctx) => { ctx.res.writeHead(429, { 'Content-Type': 'application/json' }); ctx.res.end(JSON.stringify({ detail: 'You have used the picture budget.', code: 'image_quota' })); } });
  try {
    await assert.rejects(say(w.client, [{ role: 'user', content: 'hi' }]), (err) => err instanceof ServerRefusal && err.code === 'image_quota' && /picture budget/.test(err.message));
  } finally {
    w.close();
  }
});

// ---- the agent loop --------------------------------------------------------------------------------------------------------

/** A client playing `replies`, keeping every request; `vision` is what the status says. */
function client(replies, { vision = true, contextTokens } = {}) {
  const requests = [];
  let i = 0;
  return {
    requests,
    lastKnownClusterStatus: () => ({ capabilities: { vision }, ...(contextTokens ? { policy: { coding_context_tokens: contextTokens } } : {}) }),
    async streamChat(messages, tools, onDelta) {
      requests.push({ messages: messages.map((m) => ({ ...m })), tools: tools.map((t) => t.function.name) });
      if (tools.length === 0 && String(messages[messages.length - 1].content).includes('<dlne_compact>')) return { content: '### Goal\nPictures.', toolCalls: [] };
      const reply = replies[Math.min(i++, replies.length - 1)];
      const out = typeof reply === 'function' ? reply(messages, tools) : reply;
      if (out.throw) throw out.throw;
      if (out.content) onDelta(out.content);
      return { content: out.content || '', toolCalls: out.toolCalls || [], finishReason: undefined };
    },
  };
}

function callbacks() {
  const shown = { content: '', ended: [] };
  return {
    shown,
    cb: { onThinkingDelta() {}, onContentDelta: (d) => { shown.content += d; }, onToolStart() {}, onToolEnd: (r) => shown.ended.push(r) },
  };
}

const call = (id, name, args) => ({ id, name, arguments: JSON.stringify(args) });
const imagesOf = (message) => (message.images || []).map((p) => p.name);

test('pictures sent with a message ride on it while the engine can read them, and the tool and its prompt line come with vision', async () => {
  const c = client([{ content: 'I see a house.' }]);
  const loop = new AgentLoop(c);
  const { cb } = callbacks();
  assert.equal(await loop.run('what is this?', cb, { images: [attached('house.png'), attached('plan.jpg', jpeg())] }), 'done');
  const [req] = c.requests;
  const user = req.messages.find((m) => m.role === 'user');
  assert.equal(user.content, 'what is this?');
  assert.deepEqual(imagesOf(user), ['house.png', 'plan.jpg']);
  assert.equal(user.pictureTurn, 1);
  assert.equal(req.tools.at(-1), 'view_image');
  assert.equal(req.messages[0].content, withViewImageLine(AGENT_SYSTEM_PROMPT));
  assert.match(req.messages[0].content, new RegExp(`\n${VIEW_IMAGE_PROMPT_LINE.replace(/[()]/g, '\\$&')}\n- run_terminal_command\\(`));
  // what the saved task keeps of them is a placeholder
  const saved = JSON.stringify(loop.exportState());
  assert.ok(!saved.includes('base64'));
  assert.match(saved, /what is this\?\\n\[picture: house\.png\] \[picture: plan\.jpg\]/);
});

test('without vision the pictures are left out with a word, and the tool and its line are not offered', async () => {
  const c = client([{ content: 'ok' }], { vision: false });
  const loop = new AgentLoop(c);
  const { cb, shown } = callbacks();
  await loop.run('look', cb, { images: [attached('a.png')] });
  const [req] = c.requests;
  assert.deepEqual(req.messages.find((m) => m.role === 'user'), { role: 'user', content: 'look' });
  assert.ok(!req.tools.includes('view_image'));
  assert.equal(req.messages[0].content, AGENT_SYSTEM_PROMPT);
  assert.match(shown.content, /Left out: a\.png: the swarm's engine cannot read pictures right now\./);
  // and a page's bad pictures are named, the good ones kept
  const c2 = client([{ content: 'ok' }]);
  const loop2 = new AgentLoop(c2);
  const second = callbacks();
  await loop2.run('look', second.cb, { images: [attached('a.png'), { name: 'b.png', dataUri: 'data:image/png;base64,AAAA' }] });
  assert.deepEqual(imagesOf(c2.requests[0].messages.find((m) => m.role === 'user')), ['a.png']);
  assert.match(second.shown.content, /Left out: b\.png: it is not a PNG, JPEG, WebP or GIF picture\./);
});

test('pictures older than the last three user turns are a note when a request is built, and never more than four go out', async () => {
  const c = client([{ content: 'seen' }]);
  const loop = new AgentLoop(c);
  for (let n = 1; n <= 5; n++) await loop.run(`turn ${n}`, callbacks().cb, { images: [attached(`p${n}.png`)] });
  const last = c.requests.at(-1).messages.filter((m) => m.role === 'user');
  assert.equal(last.length, 5);
  assert.deepEqual(last.map(imagesOf), [[], [], ['p3.png'], ['p4.png'], ['p5.png']]);
  assert.equal(last[0].content, 'turn 1\n[picture: p1.png, shown earlier]');
  assert.equal(last[2].pictureTurn, 3);
  // the conversation itself still holds all five (a request is built from it), and the size counts each picture
  assert.equal(loop.messages.filter((m) => m.images).length, 3);
  const before = loop.estimateTotalTokens();
  const bare = new AgentLoop(client([{ content: 'x' }]));
  await bare.run('turn', callbacks().cb);
  const withPicture = new AgentLoop(client([{ content: 'x' }]));
  await withPicture.run('turn', callbacks().cb, { images: [attached('p.png')] });
  assert.ok(withPicture.estimateTotalTokens() - bare.estimateTotalTokens() >= pic.PICTURE_TOKENS - 10);
  assert.ok(before > 0);
  // four in one message, then another with four: only the newest four are sent
  const many = client([{ content: 'seen' }]);
  const crowd = new AgentLoop(many);
  const four = (tag) => Array.from({ length: 4 }, (_, i) => attached(`${tag}${i}.png`));
  await crowd.run('first four', callbacks().cb, { images: four('a') });
  await crowd.run('second four', callbacks().cb, { images: four('b') });
  const users = many.requests.at(-1).messages.filter((m) => m.role === 'user');
  assert.deepEqual(users.map(imagesOf), [[], ['b0.png', 'b1.png', 'b2.png', 'b3.png']]);
});

test('a compaction drops every picture', async () => {
  const c = client([{ content: 'seen' }]);
  const loop = new AgentLoop(c);
  await loop.run('turn one', callbacks().cb, { images: [attached('p1.png')] });
  await loop.run('turn two', callbacks().cb, { images: [attached('p2.png')] });
  assert.equal(loop.messages.filter((m) => m.images).length, 2);
  await loop.compactIfNeeded(callbacks().cb, new AbortController().signal, true);
  assert.equal(loop.messages.filter((m) => m.images).length, 0);
  const text = loop.messages.map((m) => m.content).join('\n');
  assert.match(text, /\[picture: p2\.png, shown earlier\]/);
  assert.match(text, /\[picture: p1\.png\]/, 'the working memory remembers that the user showed it');
});

test('a refused picture is said in the coordinator\'s own words and the run goes on without it', async () => {
  const refusal = new ServerRefusal(429, 'Your network has used its pictures for this hour. Try again in 40 minutes.');
  refusal.code = 'image_quota';
  const c = client([{ throw: refusal }, { content: 'Answering without it.' }]);
  const loop = new AgentLoop(c);
  const { cb, shown } = callbacks();
  assert.equal(await loop.run('what is this?', cb, { images: [attached('house.png')] }), 'done');
  assert.equal(c.requests.length, 2);
  assert.deepEqual(imagesOf(c.requests[0].messages.find((m) => m.role === 'user')), ['house.png']);
  const second = c.requests[1].messages.find((m) => m.role === 'user');
  assert.deepEqual(second.images, undefined);
  assert.equal(second.content, 'what is this?\n[picture: house.png, not read]');
  assert.match(shown.content, /Your network has used its pictures for this hour\. Try again in 40 minutes\. Going on without the picture\./);
  assert.match(shown.content, /Answering without it\./);
  // a refusal that is not about a picture still ends the run
  const other = new ServerRefusal(400, 'Something else.');
  const c2 = client([{ throw: other }]);
  assert.equal(await new AgentLoop(c2).run('hi', callbacks().cb), 'error');
  assert.equal(c2.requests.length, 1);
  // and a picture refusal with no picture in the conversation is not retried
  const c3 = client([{ throw: refusal }]);
  assert.equal(await new AgentLoop(c3).run('hi', callbacks().cb), 'error');
  assert.equal(c3.requests.length, 1);
});

// ---- view_image ---------------------------------------------------------------------------------------------------------------

async function inWorkspace(body) {
  stub.reset();
  ToolRegistry.resetApprovals();
  const base = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-pictures-ws-')));
  stub.vscode.workspace.workspaceFolders = [{ uri: { fsPath: base } }];
  const realApprover = ToolRegistry.approver;
  const asked = [];
  ToolRegistry.approver = async (req) => { asked.push(req); return 'once'; };
  try {
    return await body({ base, asked, write: (rel, bytes) => { const f = path.join(base, rel); fs.mkdirSync(path.dirname(f), { recursive: true }); fs.writeFileSync(f, bytes); return f; } });
  } finally {
    ToolRegistry.approver = realApprover;
    stub.vscode.workspace.workspaceFolders = undefined;
    fs.rmSync(base, { recursive: true, force: true });
  }
}

test('view_image shows a picture file: the result is text with its size, the picture rides with it', () =>
  inWorkspace(async ({ asked, write }) => {
    write('docs/shot.png', png(1280, 720));
    write('icon.jpg', jpeg(32, 32));
    const result = await ToolRegistry.executeTool('view_image', { path: 'docs/shot.png' }, false, { vision: true });
    assert.equal(result.is_error, undefined);
    assert.equal(result.output, 'Showing docs/shot.png (1280x720)');
    assert.equal(result.image.name, 'shot.png');
    assert.ok(result.image.dataUri.startsWith('data:image/png;base64,'));
    assert.equal((await ToolRegistry.executeTool('view_image', { path: 'icon.jpg' }, false, { vision: true })).output, 'Showing icon.jpg (32x32)');
    assert.equal(asked.length, 0, 'a picture inside the workspace is read like any file there');
    assert.deepEqual(viewImageToolDefinition().function.parameters.required, ['path']);
  }));

test('view_image refuses what it cannot show, in words, and what the privacy rules keep away', () =>
  inWorkspace(async ({ base, write }) => {
    write('notes.txt', 'words');
    write('anim.gif', gif());
    write('big.png', png(10, 10, 4 * 1024 * 1024));
    write('secret.png', png());
    write('.dlneignore', 'secret.png\n');
    const run = (p, ctx = { vision: true }) => ToolRegistry.executeTool('view_image', { path: p }, false, ctx);
    assert.match((await run('missing.png')).output, /^File not found: missing\.png/);
    assert.match((await run('notes.txt')).output, /Cannot show notes\.txt: it is not a PNG, JPEG, WebP or GIF picture\./);
    assert.match((await run('anim.gif')).output, /Cannot show anim\.gif: the swarm reads PNG and JPEG pictures, and this is a GIF/);
    assert.match((await run('big.png')).output, /Cannot show big\.png: it is 4\.0 MiB/);
    assert.match((await run('.')).output, /is not a file/);
    const hidden = await run('secret.png');
    assert.equal(hidden.is_error, true);
    assert.equal(hidden.image, undefined);
    assert.ok(!hidden.output.includes(base));
    const off = await run('secret.png', {});
    assert.equal(off.is_error, true);
    assert.equal((await run('big.png', {})).output, 'Pictures are not available on this coordinator.');
  }));

test('a picture outside the workspace asks first, like a file outside it', () =>
  inWorkspace(async ({ asked, base }) => {
    const outside = path.join(path.dirname(base), `outside-${path.basename(base)}.png`);
    fs.writeFileSync(outside, png());
    try {
      const result = await ToolRegistry.executeTool('view_image', { path: outside }, false, { vision: true });
      assert.equal(result.is_error, undefined);
      assert.equal(asked.length, 1);
      assert.match(asked[0].summary, /^look at .*\(outside your workspace\)$/);
    } finally {
      fs.rmSync(outside, { force: true });
    }
  }));

test('the agent opens a picture with view_image and sees it with its next step', () =>
  inWorkspace(async ({ write }) => {
    write('shot.png', png(300, 200));
    const c = client([
      { toolCalls: [call('v1', 'view_image', { path: 'shot.png' })] },
      { content: 'A 300 by 200 picture.' },
    ]);
    const loop = new AgentLoop(c);
    const { cb, shown } = callbacks();
    assert.equal(await loop.run('describe shot.png', cb), 'done');
    const second = c.requests[1].messages;
    const tool = second.find((m) => m.role === 'tool');
    assert.equal(tool.content, 'Showing shot.png (300x200)');
    assert.equal(tool.images, undefined, 'tool results stay text');
    const seen = second.at(-1);
    assert.equal(seen.role, 'user');
    assert.deepEqual(imagesOf(seen), ['shot.png']);
    assert.match(seen.content, /^Here is the picture view_image opened: shot\.png\./);
    assert.equal(second.indexOf(seen), second.indexOf(tool) + 1, 'it follows the step\'s tool results');
    // what the chats are shown has no bytes in it
    assert.equal(shown.ended.length, 1);
    assert.equal(shown.ended[0].image, undefined);
    assert.ok(!JSON.stringify(shown.ended).includes('base64'));
    assert.ok(!JSON.stringify(loop.exportState()).includes('base64'));
  }));

test('view_image written inline works too, and the picture rides on the observation', () =>
  inWorkspace(async ({ write }) => {
    write('a.png', png(40, 30));
    const c = client([
      { content: '```json\n{"name": "view_image", "arguments": {"path": "a.png"}}\n```' },
      { content: 'Done.' },
    ]);
    const loop = new AgentLoop(c);
    assert.equal(await loop.run('look', callbacks().cb), 'done');
    const observation = c.requests[1].messages.at(-1);
    assert.match(observation.content, /Observation from tool \[view_image\]:\nShowing a\.png \(40x30\)/);
    assert.deepEqual(imagesOf(observation), ['a.png']);
  }));

// ---- DLNE Swarm in the Chat view -------------------------------------------------------------------------------------------------

const { Role, message } = lm;
const { LanguageModelDataPart } = lm;

test('the Chat view\'s DLNE model reads pictures while the status says vision, and the entry says so', () => {
  const entry = (status) => new prov.DlneChatModelProvider({ lastKnownClusterStatus: () => status }).provideLanguageModelChatInformation()[0];
  assert.deepEqual(entry({ capabilities: { vision: true } }).capabilities, { toolCalling: true, imageInput: true });
  for (const status of [null, {}, { capabilities: { vision: false } }]) assert.deepEqual(entry(status).capabilities, { toolCalling: true, imageInput: false });
});

test('a picture in the Chat view\'s message goes with it while vision is on, and is refused in words when it is not', () => {
  const bytes = new Uint8Array(png(20, 10));
  const ask = (...parts) => [message(Role.User, 'look', ...parts)];
  const out = msg.toOpenAiMessages(ask(LanguageModelDataPart.image(bytes, 'image/png')), { vision: true });
  assert.equal(out.length, 1);
  assert.equal(out[0].content, 'look');
  assert.equal(out[0].images.length, 1);
  assert.ok(out[0].images[0].dataUri.startsWith('data:image/png;base64,'));
  assert.deepEqual([out[0].images[0].width, out[0].images[0].height], [20, 10]);
  assert.throws(() => msg.toOpenAiMessages(ask(LanguageModelDataPart.image(bytes, 'image/png'))), (e) => e instanceof msg.UnsupportedRequest && /cannot read images right now/.test(e.message));
  assert.throws(() => msg.toOpenAiMessages(ask(LanguageModelDataPart.image(new Uint8Array(gif()), 'image/gif')), { vision: true }), (e) => e instanceof msg.UnsupportedRequest && /could not take picture 1: .*GIF/.test(e.message));
  // a tool result's picture is still refused, and only the newest four go out
  assert.throws(() => msg.toOpenAiMessages([message(Role.User, new lm.LanguageModelToolResultPart('c', [LanguageModelDataPart.image(bytes, 'image/png')]))], { vision: true }), msg.UnsupportedRequest);
  const six = [];
  for (let n = 0; n < 6; n++) six.push(message(Role.User, `turn ${n}`, LanguageModelDataPart.image(bytes, 'image/png')), message(Role.Assistant, 'ok'));
  const sent = msg.toOpenAiMessages(six, { vision: true }).filter((m) => m.role === 'user');
  assert.deepEqual(sent.map((m) => m.images?.length ?? 0), [0, 0, 0, 1, 1, 1]);
  assert.match(sent[0].content, /^turn 0\n\[picture: picture 1, shown earlier\]$/);
  // a picture counts toward the Chat view's token budget
  assert.ok(msg.chatChars(out, []) >= pic.PICTURE_CHARS);
});

// ---- the panel -------------------------------------------------------------------------------------------------------------------------

const { createPage } = require('./helpers/sidebar-page');
const { SidebarProvider } = require('../dist/views/sidebarProvider');

/** Browser pieces the fake page lacks: a file reader that answers the file's own data URI, and a picture that loads. */
const browser = () => {
  class FakeFileReader {
    readAsDataURL(file) { setImmediate(() => { this.result = file.dataUrl; this.onload && this.onload(); }); }
  }
  class FakeImage {
    set src(value) {
      this.naturalWidth = this.width = 640;
      this.naturalHeight = this.height = 480;
      setImmediate(() => (String(value).includes('BROKEN') ? this.onerror && this.onerror() : this.onload && this.onload()));
    }
  }
  return { FileReader: FakeFileReader, Image: FakeImage };
};

const settle = async () => { for (let i = 0; i < 60; i++) await new Promise((resolve) => setImmediate(resolve)); };
const file = (name, type, bytes = png(), extra = {}) => ({ name, type, size: bytes.length, dataUrl: uriOf(bytes, type), ...extra });

function panel({ vision = true } = {}) {
  stub.reset();
  const client = { lastKnownClusterStatus: () => ({ capabilities: { vision } }), onClusterStatus: () => () => {} };
  const status = { state: 'idle', gpu: { name: 'GPU', api: 'vulkan', vramTotalGb: 8 }, vramContributedGb: 0, cpuPool: false };
  const root = path.join(__dirname, '..');
  const context = {
    extensionUri: { fsPath: root },
    storageUri: { fsPath: fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-pictures-store-')) },
    globalStorageUri: { fsPath: fs.mkdtempSync(path.join(os.tmpdir(), 'dlne-pictures-gstore-')) },
    subscriptions: [],
    workspaceState: { get: () => undefined, update: async () => {} },
  };
  const provider = new SidebarProvider(context.extensionUri, client, { onStatusChange() {}, getStatus: () => status }, context);
  const posted = [];
  const page = createPage(provider._getHtmlForWebview(), { toHost: (m) => { posted.push(m); }, globals: browser() });
  return { page, posted };
}

test('the panel shows its attach button only while the engine can read pictures, and follows the status', () => {
  const on = panel({ vision: true });
  assert.equal(on.page.el('attach-btn').style.display, 'inline-block');
  const off = panel({ vision: false });
  assert.equal(off.page.el('attach-btn').style.display, 'none');
  off.page.receive({ type: 'vision', on: true });
  assert.equal(off.page.el('attach-btn').style.display, 'inline-block');
  on.page.receive({ type: 'vision', on: false });
  assert.equal(on.page.el('attach-btn').style.display, 'none');
  assert.deepEqual(on.page.errors, []);
});

test('a pasted picture becomes a thumbnail with a way to take it out, and goes with the message', async () => {
  const { page, posted } = panel();
  const item = (f) => ({ kind: 'file', type: f.type, getAsFile: () => f });
  let prevented = 0;
  page.el('prompt-input').dispatch('paste', { clipboardData: { items: [item(file('clip.png', 'image/png'))] }, preventDefault: () => { prevented++; } });
  await settle();
  assert.equal(prevented, 1);
  const strip = page.el('picture-strip');
  assert.equal(strip.style.display, 'flex');
  const thumbs = strip.descendants().filter((e) => e.tag === 'img');
  assert.equal(thumbs.length, 1);
  assert.match(thumbs[0].src, /^data:image\/jpeg;base64,/);
  assert.equal(thumbs[0].alt, 'clip.png');
  // text pastes as ever
  let textPrevented = 0;
  page.el('prompt-input').dispatch('paste', { clipboardData: { items: [{ kind: 'string', type: 'text/plain' }] }, preventDefault: () => { textPrevented++; } });
  assert.equal(textPrevented, 0);
  // send: words and the picture
  page.type('what is this');
  page.clickSend();
  const sent = posted.find((m) => m.type === 'sendMessage');
  assert.equal(sent.text, 'what is this');
  assert.equal(sent.images.length, 1);
  assert.equal(sent.images[0].name, 'clip.png');
  assert.equal(sent.images[0].dataUri, uriOf(png(), 'image/png'), 'a PNG that fits is sent as it is');
  assert.match(sent.images[0].thumb, /^data:image\/jpeg;base64,/);
  assert.equal(strip.style.display, 'none', 'the pictures leave the box with the message');
  assert.deepEqual(page.errors, []);
});

test('a picture alone can be sent, one can be taken back out, and four is the most', async () => {
  const { page, posted } = panel();
  const drop = (...files) => page.el('input-container').dispatch('drop', { dataTransfer: { files, items: [], types: ['Files'] } });
  drop(file('a.png', 'image/png'), file('b.jpg', 'image/jpeg', jpeg()), file('c.png', 'image/png'), file('d.png', 'image/png'), file('e.png', 'image/png'));
  await settle();
  const strip = page.el('picture-strip');
  assert.equal(strip.descendants().filter((e) => e.tag === 'img').length, 4);
  assert.match(page.el('picture-hint').textContent, /a message carries 4 pictures at most/);
  const remove = strip.descendants().filter((e) => e.classes.has('picture-remove'));
  assert.equal(remove.length, 4);
  remove[0].click();
  assert.equal(strip.descendants().filter((e) => e.tag === 'img').length, 3);
  page.clickSend();                                      // no words
  const sent = posted.find((m) => m.type === 'sendMessage');
  assert.equal(sent.text, '');
  assert.deepEqual(sent.images.map((p) => p.name), ['b.jpg', 'c.png', 'd.png']);
});

test('WebP and GIF become PNG, a file that is not a picture or is too big is named in the hint, and nothing is added without vision', async () => {
  const { page, posted } = panel();
  page.el('picture-input').files = [
    file('anim.gif', 'image/gif', gif()), file('photo.webp', 'image/webp', webp()), file('notes.txt', 'text/plain', Buffer.from('words')),
    file('huge.png', 'image/png', png(), { size: 5 * 1024 * 1024 }), file('broken.png', 'image/png', Buffer.from('BROKEN'), { dataUrl: 'data:image/png;base64,BROKEN' }),
  ];
  page.el('picture-input').dispatch('change');
  await settle();
  const hint = page.el('picture-hint').textContent;
  page.clickSend();
  const sent = posted.find((m) => m.type === 'sendMessage');
  assert.deepEqual(sent.images.map((p) => [p.name, p.dataUri.slice(0, 22)]), [['anim.gif', 'data:image/png;base64,'], ['photo.webp', 'data:image/png;base64,']]);
  assert.match(hint, /^Not added: /);
  assert.match(hint, /notes\.txt is not a PNG, JPEG, WebP or GIF picture/);
  assert.match(hint, /huge\.png is 5\.0 MiB, and a picture may be 4 MiB at most/);
  assert.match(hint, /broken\.png is not a picture the editor can open/);

  const off = panel({ vision: false });
  let prevented = 0;
  off.page.el('prompt-input').dispatch('paste', { clipboardData: { items: [{ kind: 'file', type: 'image/png', getAsFile: () => file('x.png', 'image/png') }] }, preventDefault: () => { prevented++; } });
  await settle();
  assert.equal(prevented, 1);
  assert.equal(off.page.el('picture-strip').style.display, 'none');
  assert.match(off.page.el('picture-hint').textContent, /cannot read pictures right now/);
  // and when vision goes while pictures wait, they are taken back out with a word
  page.el('picture-input').files = [file('again.png', 'image/png')];
  page.el('picture-input').dispatch('change');
  await settle();
  assert.equal(page.el('picture-strip').style.display, 'flex');
  page.receive({ type: 'vision', on: false });
  assert.equal(page.el('picture-strip').style.display, 'none');
  assert.match(page.el('picture-hint').textContent, /taken out of your message/);
});

test('a message with pictures is drawn with its thumbnails, and with notes where only the names are kept', () => {
  const { page } = panel();
  page.receive({ type: 'appendUserMessage', text: 'what is this?', pictures: [{ name: 'house.png', thumb: 'data:image/jpeg;base64,QUJD' }] });
  const [message] = page.find('message');
  const img = message.descendants().find((e) => e.tag === 'img');
  assert.equal(img.src, 'data:image/jpeg;base64,QUJD');
  assert.equal(img.alt, '[picture: house.png]');
  page.receive({ type: 'restoreHistory', history: [{ id: 'm1', role: 'user', text: 'earlier', pictures: [{ name: 'plan.png' }] }], run: { busy: false, events: [] } });
  const notes = page.find('picture-note');
  assert.equal(notes.length, 1);
  assert.equal(notes[0].textContent, '[picture: plan.png]');
  assert.deepEqual(page.errors, []);
});
