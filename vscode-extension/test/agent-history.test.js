'use strict';
/**
 * The agent's history is the prompt, and the engine reuses the KV cache of whatever prefix it
 * saw last time. Rewriting old messages on every turn (stripping <think>, shortening old tool
 * output) threw that cache away and made each turn re-read the conversation. Under the token
 * budget the history must stay byte-for-byte as sent; over it, compaction still happens.
 */
const test = require('node:test');
const assert = require('node:assert/strict');

require('./helpers/vscode-stub');
const { AgentLoop } = require('../dist/agent/agentLoop');

function loopWith(messages) {
  const loop = new AgentLoop({});
  loop.messages = [loop.messages[0], ...messages];
  return loop;
}

const longTool = 'x'.repeat(2000);
const turns = () => [
  { role: 'user', content: 'fix the bug' },
  { role: 'assistant', content: '<think>planning</think>I will read the file.' },
  { role: 'tool', name: 'read_file', content: longTool },
  { role: 'assistant', content: '<think>more</think>Now editing.' },
  { role: 'tool', name: 'edit_file_diff', content: 'ok' },
  { role: 'assistant', content: 'Done?' },
  { role: 'user', content: 'yes' },
];

test('under the budget the history is left exactly as it was', () => {
  const loop = loopWith(turns());
  const before = JSON.stringify(loop.messages);
  loop.compactHistoryIfNeeded(11000);
  assert.equal(JSON.stringify(loop.messages), before);
});

test('over the budget, old think blocks and tool output are still compacted', () => {
  const loop = loopWith(turns());
  loop.compactHistoryIfNeeded(100);
  const text = JSON.stringify(loop.messages);
  assert.doesNotMatch(text, /<think>planning<\/think>/);
  assert.ok(loop.estimateTotalTokens() < loopWith(turns()).estimateTotalTokens());
});
