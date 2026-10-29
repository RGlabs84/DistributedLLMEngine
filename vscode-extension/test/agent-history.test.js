'use strict';
/**
 * The agent's history is the prompt, and the engine reuses the KV cache of whatever prefix it saw last time.
 * Rewriting old messages on every turn threw that cache away and made each turn re-read the conversation. So:
 * under the budget, every request is the previous one plus the new turn, byte for byte; over it, a compaction
 * rewrites the history once and the prefix then stays stable again (docs/agent-memory.md 5.3).
 */
const test = require('node:test');
const assert = require('node:assert/strict');

require('./helpers/vscode-stub');
const { AgentLoop, WORKING_MEMORY_OPEN } = require('../dist/agent/agentLoop');
const { ToolRegistry } = require('../dist/agent/tools');

const realExecute = ToolRegistry.executeTool;
test.afterEach(() => { ToolRegistry.executeTool = realExecute; });

/** A client that asks for `turns` file reads, then answers; notes requests (no tools) get short notes. */
function readingClient(turns, contextTokens) {
  const requests = [];
  let n = 0;
  return {
    requests,
    lastKnownClusterStatus: () => (contextTokens ? { policy: { coding_context_tokens: contextTokens } } : null),
    async streamChat(messages, tools, onDelta) {
      requests.push({ messages: JSON.parse(JSON.stringify(messages)), tools });
      if (tools.length === 0) {
        return { content: '### Goal\nRead the files.\n### Next\nKeep reading.', toolCalls: [] };
      }
      if (n++ < turns) {
        return { content: '', toolCalls: [{ id: `c${n}`, name: 'read_file', arguments: JSON.stringify({ path: `src/f${n}.ts` }) }] };
      }
      onDelta('Done.');
      return { content: 'Done.', toolCalls: [] };
    },
  };
}

const callbacks = { onThinkingDelta() {}, onContentDelta() {}, onToolStart() {}, onToolEnd() {} };

/** True when `now` begins with every message of `before`, unchanged. */
function extends_(before, now) {
  return before.every((m, i) => JSON.stringify(m) === JSON.stringify(now[i]));
}

test('under the budget, each request is the one before it plus the new turn, byte for byte', async () => {
  ToolRegistry.executeTool = async (name, args) => ({ name, output: `1: contents of ${args.path}` });
  const client = readingClient(4);
  await new AgentLoop(client).run('read the files', callbacks);
  const agentRequests = client.requests.filter((r) => r.tools.length > 0);
  assert.equal(agentRequests.length, 5);
  assert.equal(client.requests.length, 5, 'no notes were needed');
  for (let i = 1; i < agentRequests.length; i++) {
    assert.ok(extends_(agentRequests[i - 1].messages, agentRequests[i].messages), `request ${i + 1} rewrote an earlier message`);
  }
});

test('over the budget, the history is compacted, and between compactions it only grows', async () => {
  // A 20,000-token context leaves less than the 6,000-token floor, so the budget is the floor: a few big reads fill it.
  ToolRegistry.executeTool = async (name, args) => ({ name, output: `1: ${'x'.repeat(3000)} ${args.path}` });
  const client = readingClient(12, 20000);
  const loop = new AgentLoop(client);
  await loop.run('read every file', callbacks);

  assert.ok(client.requests.some((r) => r.tools.length === 0), 'no notes were asked for');
  const agentRequests = client.requests.filter((r) => r.tools.length > 0);
  const compacted = agentRequests.findIndex((r) => String(r.messages[1]?.content).startsWith(WORKING_MEMORY_OPEN));
  assert.ok(compacted > 0, 'the history was never compacted');
  for (let i = 1; i < agentRequests.length; i++) {
    const before = agentRequests[i - 1].messages;
    const now = agentRequests[i].messages;
    if (!extends_(before, now)) {
      // Only a compaction rewrites the prompt, and it brings a new working-memory block.
      assert.ok(String(now[1]?.content).startsWith(WORKING_MEMORY_OPEN), `request ${i + 1} was rewritten without a compaction`);
      assert.notEqual(now[1].content, before[1]?.content);
    }
  }
  assert.ok(loop.estimateTotalTokens() <= loop.promptBudget(), 'the conversation is still over its budget');
});
