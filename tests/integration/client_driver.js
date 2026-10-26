'use strict';
/**
 * The editor's side of the integration run: the compiled extension (vscode-extension/dist/client, the real
 * DlneClient with its pipeline, route lookup, sealing, answer reader, retry and queue code) driven from node under
 * the extension tests' `vscode` stand-in. Nothing of the client is imitated; only the editor around it is.
 *
 *   node client_driver.js <config.json>
 *
 * The config names the coordinator (the wire tap's address), a fake HOME, optional settings, and a list of ops
 * that run one after another on ONE client (so a route cache, a session or a place in line carries from one to
 * the next):
 *
 *   {op: 'chat',     id, text, sessionId?, maxTokens?, tools?}   streamChat; one event per delta / progress / queue
 *   {op: 'complete', id, prompt, sessionId?, maxTokens?}         getCompletion (a sealed whole answer)
 *   {op: 'status',   id}                                         getClusterStatus (what the status bar polls)
 *   {op: 'sleep',    id, ms}
 *
 * Output is one JSON object per line on stdout: {ev: 'delta'|'progress'|'queue'|'done'|'error'|'ready'|..., op, ...}.
 * The one outside thing the extension would reach for is the public-repository check (api.github.com): with
 * `fakeGithub` the answer for that host comes from here, and nothing else leaves the machine.
 */
const fs = require('node:fs');
const path = require('node:path');

const cfg = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
process.env.HOME = cfg.home;
process.env.USERPROFILE = cfg.home;

const emit = (obj) => process.stdout.write(JSON.stringify(obj) + '\n');

// ---- the one thing that may not leave the machine: the public-repository lookup ---------------------------
const https = require('node:https');
const { EventEmitter } = require('node:events');
https.get = function (url, options, callback) {
  const target = typeof url === 'string' ? new URL(url) : url;
  if (target.hostname === 'api.github.com') {
    const req = new EventEmitter();
    req.destroy = () => {};
    const answer = cfg.fakeGithub && cfg.fakeGithub[target.pathname];
    setImmediate(() => {
      const res = new EventEmitter();
      res.statusCode = answer ? 200 : 404;
      const cb = typeof options === 'function' ? options : callback;
      cb(res);
      res.emit('data', Buffer.from(JSON.stringify(answer || { message: 'Not Found' })));
      res.emit('end');
    });
    return req;
  }
  throw new Error(`the integration client may not reach ${target.hostname}`);
};
https.request = function (url) {
  const host = typeof url === 'string' ? new URL(url).hostname : url.hostname || url.host;
  throw new Error(`the integration client may not reach ${host}`);
};

const ext = cfg.extensionRoot;
const stub = require(path.join(ext, 'test', 'helpers', 'vscode-stub'));
stub.state.settings['dlne.serverUrl'] = cfg.serverUrl;
for (const [key, value] of Object.entries(cfg.settings || {})) {
  stub.state.settings[`dlne.${key}`] = value;
}
if (cfg.workspaceFolder) {
  stub.vscode.workspace.workspaceFolders = [
    { uri: stub.vscode.Uri.file(cfg.workspaceFolder), name: path.basename(cfg.workspaceFolder), index: 0 },
  ];
}

const { DlneClient } = require(path.join(ext, 'dist', 'client', 'dlneClient'));
if (cfg.busyGiveUpMs) DlneClient.busyGiveUpMs = cfg.busyGiveUpMs;
if (cfg.waitTiming) DlneClient.waitTiming = { ...DlneClient.waitTiming, ...cfg.waitTiming };

const client = new DlneClient();

async function runOp(op) {
  const id = op.id;
  const started = Date.now();
  switch (op.op) {
    case 'chat': {
      let content = '';
      const options = {
        sessionId: op.sessionId,
        onQueue: (queue) => emit({ ev: 'queue', op: id, queue, at: Date.now() - started }),
      };
      const messages = op.messages || [{ role: 'user', content: op.text }];
      const ctrl = new AbortController();
      let deltas = 0;
      try {
        const result = await client.streamChat(
          messages,
          op.tools || [],
          (delta) => {
            content += delta;
            deltas++;
            emit({ ev: 'delta', op: id, text: delta, n: deltas, at: Date.now() - started });
            if (op.abortAfterDeltas && deltas >= op.abortAfterDeltas) ctrl.abort();
          },
          (name) => emit({ ev: 'tool', op: id, name }),
          ctrl.signal,
          (line) => emit({ ev: 'progress', op: id, line, at: Date.now() - started }),
          options
        );
        emit({ ev: 'done', op: id, content: result.content, toolCalls: result.toolCalls, deltas, ms: Date.now() - started });
      } catch (err) {
        emit({
          ev: 'error', op: id, name: err && err.name, message: String(err && err.message),
          status: err && err.status, partial: err && err.partial, shown: content, deltas, ms: Date.now() - started,
        });
      }
      return;
    }
    case 'complete': {
      const text = await client.getCompletion(op.prompt, op.maxTokens || 24, op.stop || [], undefined, { sessionId: op.sessionId });
      emit({ ev: 'done', op: id, text, ms: Date.now() - started });
      return;
    }
    case 'status': {
      const status = await client.getClusterStatusDetailed();
      emit({ ev: 'done', op: id, status });
      return;
    }
    case 'sleep': {
      await new Promise((resolve) => setTimeout(resolve, op.ms));
      emit({ ev: 'done', op: id });
      return;
    }
    default:
      emit({ ev: 'error', op: id, message: `unknown op ${op.op}` });
  }
}

(async () => {
  emit({ ev: 'ready' });
  try {
    for (const op of cfg.ops) {
      await runOp(op);
    }
  } catch (err) {
    emit({ ev: 'fatal', message: String(err && err.stack || err) });
  }
  emit({ ev: 'exit' });
  process.exit(0);
})();
