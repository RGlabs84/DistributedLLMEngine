"""
Tests for scripts/cache_probe.py, the prompt-cache measurement script for the coding model.

The script is only worth running if it sends what the VS Code extension really sends, so the first tests
compare its copies of the system prompt, the tool list and the autocomplete budget with the TypeScript
source. The rest check the pieces that produce the numbers (history compaction, the coordinator's body
changes, the FIM prompt windows, the log reader) and run a whole agent turn pair against a fake server.
"""

import http.server
import importlib.util
import json
import os
import re
import threading

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "cache_probe.py")
_spec = importlib.util.spec_from_file_location("cache_probe", SCRIPT)
cp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cp)

EXT = os.path.join(ROOT, "vscode-extension", "src")


def read_ts(*parts):
    with open(os.path.join(EXT, *parts), encoding="utf-8") as f:
        return f.read()


needs_extension = pytest.mark.skipif(not os.path.isdir(EXT), reason="vscode-extension/src is not in this checkout")


# --------------------------------------------------------------------------------- copies match the extension

_TS_STRING = r"'((?:\\.|[^'\\])*)'"


def ts_unescape(raw):
    """The value of a single-quoted TypeScript string literal's body."""
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), raw)


def ts_template_literal(src, declaration):
    """The text of the template literal that follows `declaration`, as JavaScript evaluates it (\\` is a backtick)."""
    start = src.find(declaration)
    assert start >= 0, f"{declaration!r} moved in the TypeScript; update cache_probe.py"
    body, i = [], start + len(declaration)
    while src[i] != "`":
        if src[i] == "\\":
            assert src[i + 1] in "`\\", f"an escape the test does not understand: \\{src[i + 1]}"
            body.append(src[i + 1])
            i += 2
        else:
            assert src[i:i + 2] != "${", "the prompt became a template with substitutions"
            body.append(src[i])
            i += 1
    return "".join(body)


def ts_array_to_python(src, opener):
    """Evaluates the array literal that follows `opener` (single-quoted strings, bare keys, comments, trailing commas)."""
    start = src.index(opener) + len(opener) - 1
    assert src[start] == "["
    token = re.compile(r"\s+|//[^\n]*|" + _TS_STRING + r"|([A-Za-z_]\w*)|(.)", re.S)
    out, depth, pos = [], 0, start
    while True:
        m = token.match(src, pos)
        pos = m.end()
        text = m.group(0)
        if m.group(1) is not None:
            out.append(json.dumps(ts_unescape(m.group(1))))
        elif m.group(2) is not None:
            out.append(json.dumps(text) if src[pos:].lstrip().startswith(":") else text)
        elif m.group(3) is not None:
            out.append(text)
            depth += {"[": 1, "]": -1}.get(text, 0)
            if depth == 0:
                break
    return json.loads(re.sub(r",(\s*[}\]])", r"\1", "".join(out)))


@needs_extension
def test_system_prompt_is_the_one_the_agent_sends():
    src = read_ts("agent", "agentLoop.ts")
    ts = ts_template_literal(src, "export const AGENT_SYSTEM_PROMPT = `")
    assert cp.AGENT_SYSTEM_PROMPT == ts
    assert ts.startswith("You are the DLNE coding agent") and "remember(fact, scope, replaces)" in ts and "<dlne_working_memory>" in ts
    assert "```json" in ts                                     # the inner fence is in the text, unescaped
    assert "{ role: 'system', content: AGENT_SYSTEM_PROMPT }" in src      # and it is the constant the loop puts first


@needs_extension
def test_tool_list_matches_tools_ts():
    src = read_ts("agent", "tools.ts")
    ts_tools = ts_array_to_python(src, "static getToolDefinitions(): ToolDefinition[] {\n    return [")
    assert len(ts_tools) == len(cp.TOOLS) == 8
    assert [t["function"]["name"] for t in ts_tools] == [t["function"]["name"] for t in cp.TOOLS]
    assert cp.TOOLS[-1]["function"]["name"] == "remember"
    for mine, theirs in zip(cp.TOOLS, ts_tools):
        assert mine == theirs, mine["function"]["name"]        # name, description, every parameter, required
    assert json.dumps(cp.TOOLS) == json.dumps(ts_tools)        # and in the same key order: the cached prefix is bytes
    loop =read_ts("agent", "agentLoop.ts")
    valid = set(re.findall(r"'(\w+)'", loop.split("const VALID_TOOLS = new Set([")[1].split("]);")[0]))
    assert valid == {t["function"]["name"] for t in cp.TOOLS}
    assert cp.TOOLS_CHARS == len(json.dumps(cp.TOOLS, separators=(",", ":"), ensure_ascii=False))


@needs_extension
def test_fim_budget_and_stops_match_fim_provider():
    src = read_ts("completion", "fimProvider.ts")
    for name, value in (("PREFIX_LINES", cp.PREFIX_LINES), ("PREFIX_CHARS", cp.PREFIX_CHARS), ("SUFFIX_LINES", cp.SUFFIX_LINES),
                        ("SUFFIX_CHARS", cp.SUFFIX_CHARS), ("MAX_TOKENS_LINE", cp.FIM_MAX_TOKENS_LINE),
                        ("MAX_TOKENS_BLOCK", cp.FIM_MAX_TOKENS_BLOCK)):
        assert f"const {name} = {value};" in src, name
    for stop in ("<|fim_pad|>", "<|file_separator|>", "<|im_end|>"):
        assert f"'{stop}'" in src
    assert "<|fim_prefix|>${" in src and "<|fim_suffix|>${" in src and "<|fim_middle|>" in src
    # closesBlock(): the probe reads "inside {}" the way the extension does (the start of a body, not mid-line).
    assert r"return /^\s*}[\s;),\]]*$/.test(restOfLine);" in src


@needs_extension
def test_request_options_match_stream_chat():
    src = read_ts("client", "dlneClient.ts")
    assert "temperature: 0.2," in src and "max_tokens: AGENT_MAX_TOKENS" in src
    assert "export const AGENT_MAX_TOKENS = 10240;" in src
    body = cp.extension_chat_body([{"role": "user", "content": "x"}])
    assert body["temperature"] == 0.2 and body["max_tokens"] == 10240 and body["stream"] is True and body["tools"] == cp.TOOLS


@needs_extension
def test_compaction_constants_match_the_agent_loop():
    src = read_ts("agent", "agentLoop.ts")

    def ts_number(name, text=src):
        m = re.search(rf"\bconst {name} = (\d+(?:\.\d+)?);", text)
        assert m, f"{name} moved in the TypeScript; update cache_probe.py"
        return float(m.group(1))

    for name in ("DEFAULT_CONTEXT_TOKENS", "CONTEXT_SAFETY_TOKENS", "MIN_PROMPT_BUDGET", "DEFAULT_CHARS_PER_TOKEN", "USER_WORDS_MAX_CHARS",
                 "FIRST_WORDS_MAX_CHARS", "NOTES_MAX_CHARS", "TAIL_SHARE", "TAIL_RESULT_MAX_CHARS", "TAIL_ARGUMENT_MAX_CHARS", "HUGE_MESSAGE_CHARS"):
        assert getattr(cp, name) == ts_number(name), name
    assert cp.DEFAULT_CHARS_PER_TOKEN == 3.0 and cp.TAIL_SHARE == 0.35
    assert cp.AGENT_MAX_TOKENS == ts_number("AGENT_MAX_TOKENS", read_ts("client", "dlneClient.ts"))
    assert cp.LEDGER_MAX_CHARS == ts_number("LEDGER_MAX_CHARS", read_ts("agent", "ledger.ts"))
    assert cp.CONTEXT_PROMPT_SHARE == ts_number("CONTEXT_PROMPT_SHARE") == 0.72
    # the budget formula (1.4.2: the reply's room taken off, capped at CONTEXT_PROMPT_SHARE of the context), the
    # estimate and the tail's shape, as written in the loop
    assert "Math.min(context - AGENT_MAX_TOKENS - CONTEXT_SAFETY_TOKENS, Math.floor(context * CONTEXT_PROMPT_SHARE))" in src
    assert "Math.max(MIN_PROMPT_BUDGET," in src
    assert "reported >= 4096" in src
    assert "Math.ceil((messageChars(messages) + (withTools ? this.toolsChars : 0)) / this.charsPerToken)" in src
    assert "(m.content?.length ?? 0) + 16" in src and "(tc.function?.arguments?.length ?? 0) + 16" in src
    assert "0.7 * this.charsPerToken + 0.3 * measured" in src and "Math.min(5, Math.max(2," in src and "promptTokens < 200" in src
    assert "Math.floor(budget * TAIL_SHARE)" in src and "this.estimateTokens(request, false) > this.contextTokens() - 2048" in src
    # the block's parts are sized to the budget, then two shrink steps; the notes request is not repeated within 3 turns
    assert "const room = budget * this.charsPerToken;" in src
    assert "Math.min(USER_WORDS_MAX_CHARS, Math.floor(room * 0.18))" in src
    assert "Math.min(NOTES_MAX_CHARS, Math.floor(room * 0.13))" in src
    assert "Math.min(LEDGER_MAX_CHARS, Math.floor(room * 0.11))" in src
    assert "this.renderWorkingMemory(Math.floor(wordsMax / 2), ledgerMax, notesMax)" in src
    assert "this.renderWorkingMemory(Math.floor(wordsMax / 2), Math.floor(ledgerMax / 2), notesMax)" in src
    assert "if (this.turnsSinceCompaction >= 3) {" in src and "this.turnsSinceCompaction = 0;" in src and "this.turnsSinceCompaction++;" in src
    assert "private turnsSinceCompaction = Number.POSITIVE_INFINITY;" in src
    assert "if (!notes || isOverflowAlert(notes) || /<tool_call|<function-call>|<tools>/.test(notes)) return null;" in src
    assert "measured < this.charsPerToken / 3 || measured > this.charsPerToken * 3" in src
    assert "Math.max(800, Math.min(TAIL_RESULT_MAX_CHARS, Math.floor((share * this.charsPerToken * 0.8) / results)))" in src
    assert "Math.max(1, tail.filter((m) => m.role === 'tool').length)" in src
    assert "cutMiddle(copy.content, resultMax, Math.floor(resultMax * 0.6), Math.floor(resultMax * 0.25))" in src
    assert "cutMiddle(copy.content, TAIL_RESULT_MAX_CHARS, 2500, 1000)" not in src
    assert "cutMiddle(m.content, HUGE_MESSAGE_CHARS, 8000, 3000)" in src and "cutMiddle(raw, TAIL_ARGUMENT_MAX_CHARS, 600, 200)" in src
    # the old compaction is gone, and so must it be from the probe
    assert "compactHistoryIfNeeded" not in src and "keepTail" not in src and "/ 3.2" not in src
    assert not hasattr(cp, "COMPACT_BUDGET") and not hasattr(cp, "estimate_total_tokens")


@needs_extension
def test_working_memory_markers_and_compact_request_match_the_agent_loop():
    src = read_ts("agent", "agentLoop.ts")
    assert f"export const WORKING_MEMORY_OPEN = '{cp.WORKING_MEMORY_OPEN}';" in src
    assert f"export const WORKING_MEMORY_CLOSE = '{cp.WORKING_MEMORY_CLOSE}';" in src
    intro = re.search(r"const WORKING_MEMORY_INTRO =\s*" + _TS_STRING + ";", src)
    assert intro and cp.WORKING_MEMORY_INTRO == ts_unescape(intro.group(1))
    body = src.split("export const COMPACT_REQUEST = [")[1].split("].join('\\n');")[0]
    assert cp.COMPACT_REQUEST == "\n".join(ts_unescape(s) for s in re.findall(_TS_STRING, body))
    # the block's layout (renderWorkingMemory) and the section titles
    assert "parts.push('', \"## The user's words\", words)" in src
    assert "parts.push('', '## Your notes', this.notes.length <= notesMax ? this.notes : `${this.notes.slice(0, notesMax)}\\n[… notes cut]`);" in src
    assert "parts.push('', '## Ledger', this.ledger.render(ledgerMax))" in src and "parts.join('\\n')" in src
    assert "this.buildMessages({ role: 'user', content: COMPACT_REQUEST })" in src
    assert "this.client.streamChat(request, [], " in src          # the notes request sends no tools


# ------------------------------------------------------------------------------------------- compaction

def read_round(i, tool_chars=3000, name="read_file"):
    path = f"src/f{i}.ts"
    return [{"role": "assistant", "content": f"step {i}",
             "tool_calls": [{"id": f"c{i}", "type": "function", "function": {"name": name, "arguments": json.dumps({"path": path})}}]},
            {"role": "tool", "tool_call_id": f"c{i}", "name": name, "content": "\n".join(f"{n}: " + "x" * 60 for n in range(1, tool_chars // 64))}]


def memory_with(rounds, tool_chars=3000, budget=None, words=("do it",), context_tokens=cp.DEFAULT_CONTEXT_TOKENS):
    mem = cp.AgentMemory(context_tokens, budget=budget)
    for w in words:
        mem.user_words.append(w)
        mem.history.append({"role": "user", "content": w})
    for i in range(rounds):
        a, t = read_round(i, tool_chars)
        mem.history += [a, t]
        mem.ledger.record("read_file", {"path": f"src/f{i}.ts"}, t["content"])
    return mem


def never_asked(_request):
    raise AssertionError("no notes request was expected")


def test_the_budget_is_derived_from_the_context():
    assert cp.prompt_budget() == cp.prompt_budget(32768) == 32768 - 10240 - 1024 == 21504
    assert cp.prompt_budget(40960) == int(40960 * 0.72) == 29491, "the 72% cap binds (1.4.2)"
    assert cp.prompt_budget(65536) == int(65536 * 0.72)
    assert cp.prompt_budget(8000) == cp.MIN_PROMPT_BUDGET == 6000 and cp.prompt_budget(4096) == 6000      # the floor
    assert cp.prompt_budget(None) == cp.prompt_budget("x") == cp.prompt_budget(1000) == 21504             # unusable -> the default context
    assert cp.prompt_budget(32768, lowered=11520) == 11520 and cp.prompt_budget(32768, lowered=100) == 6000
    assert cp.AgentMemory().prompt_budget() == 21504
    assert cp.AgentMemory(budget=5000).prompt_budget() == 5000          # an explicit --compact-budget overrides it


def test_the_estimate_is_chars_over_three_plus_the_tool_list():
    mem = cp.AgentMemory()
    mem.history.append({"role": "user", "content": "x" * 300})
    chars = (len(cp.AGENT_SYSTEM_PROMPT) + 16) + (300 + 16)
    assert mem.estimate_tokens(with_tools=False) == -(-chars // 3) and mem.chars_per_token == 3.0
    assert mem.estimate_tokens() == -(-(chars + cp.TOOLS_CHARS) // 3)
    mem.history.append({"role": "assistant", "content": "", "tool_calls": [{"id": "1", "type": "function", "function": {"name": "abc", "arguments": "12345"}}]})
    assert cp.message_chars(mem.history[-1:]) == 16 + 3 + 5 + 16
    mem.calibrate(30000, 10000)                     # a reply says the prompt really had 3 chars per token: no change
    assert abs(mem.chars_per_token - 3.0) < 1e-9
    mem.calibrate(50000, 10000)                     # 5 chars per token: 0.7*3 + 0.3*5
    assert abs(mem.chars_per_token - 3.6) < 1e-9
    mem.calibrate(1000, 100)                        # a prompt under 200 tokens says nothing
    assert abs(mem.chars_per_token - 3.6) < 1e-9


def test_under_budget_nothing_changes_and_no_notes_are_asked_for():
    mem = memory_with(3, tool_chars=1200)
    before = json.dumps(mem.build_messages())
    assert mem.estimate_tokens() < mem.prompt_budget()
    assert cp.compact_history_if_needed(mem, never_asked) is False
    assert json.dumps(mem.build_messages()) == before and mem.working_memory is None and mem.notes == ""


def test_over_budget_asks_for_notes_then_freezes_the_working_memory_and_keeps_a_tail():
    mem = memory_with(14, tool_chars=3000, budget=15360)      # the 1.4.1 budget: these rounds overflow it
    original = list(mem.history)
    budget = mem.prompt_budget()
    assert mem.estimate_tokens() > budget
    asked = []

    def notes(request):
        asked.append(request)
        return "<think>hmm</think>\n### Goal\nFix the thing\n### Next\nRun tests"

    assert cp.compact_history_if_needed(mem, notes) is True
    (request,) = asked
    assert request[0]["role"] == "system" and request[-1] == {"role": "user", "content": cp.COMPACT_REQUEST}
    assert request[1:-1] == original                                     # the notes request carries the whole history, then the ask
    assert mem.notes == "### Goal\nFix the thing\n### Next\nRun tests"   # the <think> is removed

    wm = mem.working_memory
    assert wm.startswith(cp.WORKING_MEMORY_OPEN + "\n" + cp.WORKING_MEMORY_INTRO + "\n\n## The user's words\n1. do it\n\n## Your notes\n### Goal")
    assert "\n\n## Ledger\nRead:\n- src/f0.ts (whole file, " in wm and wm.endswith("\n" + cp.WORKING_MEMORY_CLOSE)
    # the tail: starts at an assistant turn, is a word-for-word suffix of the old history, fits 35% of the budget
    assert mem.history[0]["role"] == "assistant" and mem.history == original[-len(mem.history):]
    assert 2 <= len(mem.history) < len(original) and mem.tokens_of(mem.history) <= int(budget * cp.TAIL_SHARE)
    assert mem.history[-1] == original[-1]
    ids = {tc["id"] for m in mem.history for tc in m.get("tool_calls") or []}
    assert all(m["tool_call_id"] in ids for m in mem.history if m["role"] == "tool")        # no orphaned tool result
    # the rebuilt prompt: system, the working-memory block as a user message, the tail
    built = mem.build_messages()
    assert [m["role"] for m in built[:2]] == ["system", "user"] and built[1]["content"] == wm and built[2:] == mem.history
    assert mem.estimate_tokens() <= budget
    # it is frozen: the next turns only append, and nothing fires until the budget is reached again
    before = json.dumps(built)
    a, t = read_round(99, 200)
    mem.history += [a, t]
    assert json.dumps(mem.build_messages()).startswith(before[:-1])
    assert cp.compact_history_if_needed(mem, never_asked) is False and mem.working_memory == wm


def test_a_failed_or_empty_notes_request_keeps_the_previous_notes():
    for reply in (None, "", "<think>only thinking</think>", "<think>unfinished"):
        mem = memory_with(14, budget=15360)
        mem.notes = "### Goal\nold notes"
        assert cp.compact_history_if_needed(mem, lambda _r, reply=reply: reply)
        assert mem.notes == "### Goal\nold notes" and "## Your notes\n### Goal\nold notes" in mem.working_memory
    mem = memory_with(14, budget=15360)
    cp.compact_history_if_needed(mem, lambda _r: "n" * 7000)
    assert mem.notes == "n" * cp.NOTES_MAX_CHARS + "\n[… notes cut]"


def test_the_blocks_parts_are_sized_to_the_budget():
    mem = memory_with(14, budget=15360, words=("w" * 6000, "second request"))
    mem.notes = "n" * 6000
    cp.compact_history_if_needed(mem, never_asked_unless_notes)
    wm = mem.working_memory                                         # at 15,360 tokens: words 8,000, notes 5,990, ledger 5,000
    assert "n" * 5990 + "\n[… notes cut]" in wm and "n" * 5991 not in wm
    small = memory_with(14, budget=6000, words=("w" * 6000, "second request"))
    small.notes = "n" * 6000
    cp.compact_history_if_needed(small, never_asked_unless_notes)
    # room = 6000 * 3.0 = 18,000 characters: words 3,240, notes 2,340, ledger 1,980 (the words may be halved to fit)
    assert "n" * 2340 + "\n[… notes cut]" in small.working_memory and "n" * 2341 not in small.working_memory


def never_asked_unless_notes(_request):
    return None


def test_notes_are_not_asked_for_again_within_three_turns():
    asked = []
    mem = memory_with(14, budget=15360)
    assert cp.compact_history_if_needed(mem, lambda r: asked.append(r) or "### Goal\none")
    assert len(asked) == 1 and mem.turns_since_compaction == 0
    for _ in range(5):
        a, t = read_round(50, 3000)
        mem.history += [a, t]
    assert cp.compact_history_if_needed(mem, never_asked, force=True)          # a compaction right after: working memory rebuilt only
    mem.turns_since_compaction = 3
    assert cp.compact_history_if_needed(mem, lambda r: asked.append(r) or "### Goal\ntwo", force=True)
    assert len(asked) == 2 and mem.notes == "### Goal\ntwo"


def test_replies_that_are_not_notes_are_refused():
    for reply in ("", "<think>x</think>", None, "[System Alert] the prompt exceeds the context size",
                  "the request exceeds the available context size", "<tool_call>{}</tool_call>", "ok <function-call>x</function-call>", "<tools>x</tools>"):
        assert cp.accepted_notes(reply) is None, reply
    assert cp.accepted_notes("<think>t</think> ### Goal\nfine ") == "### Goal\nfine"
    mem = memory_with(14)
    mem.notes = "old"
    cp.compact_history_if_needed(mem, lambda _r: "<tool_call>x</tool_call>")
    assert mem.notes == "old"


def test_a_wild_chars_per_token_measurement_is_ignored():
    mem = cp.AgentMemory()
    mem.calibrate(100000, 1000)                 # 100 chars per token: more than 3x the current 3.0
    mem.calibrate(10000, 1000)                  # 10: still more than 3x away from 3.0 -> ignored
    assert mem.chars_per_token == 3.0
    mem.calibrate(8000, 1000)                   # 8 is within 3x of 3.0 (limit 9): 0.7*3 + 0.3*8 = 4.5
    assert abs(mem.chars_per_token - 4.5) < 1e-9
    mem.calibrate(1000, 1000)                   # 1 < 4.5/3: ignored
    assert abs(mem.chars_per_token - 4.5) < 1e-9


def test_no_notes_request_when_even_that_request_would_not_fit_the_context():
    mem = memory_with(14, tool_chars=3000, context_tokens=4096)                 # budget 6000 (the floor) but only 2048 tokens fit a notes request
    assert mem.estimate_tokens(mem.request_for_notes(), with_tools=False) > mem.context_tokens - 2048
    assert cp.compact_history_if_needed(mem, never_asked) is True
    assert mem.notes == "" and mem.working_memory.startswith(cp.WORKING_MEMORY_OPEN)


def test_force_compacts_a_history_that_fits():
    mem = memory_with(2, tool_chars=500)
    assert cp.compact_history_if_needed(mem, lambda _r: "### Goal\nx", force=True) is True
    assert mem.notes and mem.working_memory and mem.history[0]["role"] == "assistant"


def test_an_explicit_budget_overrides_the_derived_one():
    mem = memory_with(6, tool_chars=1500, budget=cp.prompt_budget() * 4)
    assert cp.compact_history_if_needed(mem, never_asked) is False
    mem = memory_with(6, tool_chars=1500, budget=4500)
    assert mem.estimate_tokens() > 4500
    assert cp.compact_history_if_needed(mem, lambda _r: "n") is True and mem.estimate_tokens() <= 4500


def test_a_newest_turn_over_its_share_is_slimmed():
    mem = cp.AgentMemory(budget=6000)
    mem.user_words.append("write it")
    mem.history = [{"role": "user", "content": "write it"},
                   {"role": "assistant", "content": "ok", "tool_calls": [
                       {"id": "w", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": "big.ts", "content": "c" * 5000})}},
                       {"id": "r", "type": "function", "function": {"name": "read_file", "arguments": json.dumps({"path": "x.ts"})}}]},
                   {"role": "tool", "tool_call_id": "w", "name": "write_file", "content": "Successfully created file: big.ts (+100 lines, -0 lines)"},
                   {"role": "tool", "tool_call_id": "r", "name": "read_file", "content": "1: " + "r" * 9000}]
    assert cp.compact_history_if_needed(mem, lambda _r: "n")
    assistant, written, read = mem.history
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {
        "path": "big.ts", "content": "[… 5000 characters, left out to make room; the file on disk has them]"}
    assert assistant["tool_calls"][1] == {"id": "r", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "x.ts"}'}}
    # share 2,100 tokens, two results: resultMax = floor(2100 * 3.0 * 0.8 / 2) = 2520 -> first 1,512 and last 630 characters
    assert read["content"] == "1: " + "r" * 1509 + "\n[… 6861 characters cut]\n" + "r" * 630
    assert written["content"].startswith("Successfully created")
    assert mem.history[0]["role"] == "assistant"


def test_each_result_of_a_big_turn_gets_an_equal_part_of_the_room():
    def big_turn(n_results, chars):
        mem = cp.AgentMemory(budget=6000)
        calls = [{"id": f"c{i}", "type": "function", "function": {"name": "read_file", "arguments": json.dumps({"path": f"f{i}.ts"})}} for i in range(n_results)]
        mem.history = [{"role": "user", "content": "go"}, {"role": "assistant", "content": "", "tool_calls": calls}] + \
                      [{"role": "tool", "tool_call_id": f"c{i}", "name": "read_file", "content": "t" * chars} for i in range(n_results)]
        return [m["content"] for m in mem.select_tail(6000)[1:]]
    twelve = big_turn(12, 6000)                      # floor(2100 * 3 * 0.8 / 12) = 420 -> the floor of 800
    assert all(c == "t" * 480 + "\n[… 5320 characters cut]\n" + "t" * 200 for c in twelve)
    one = big_turn(1, 20000)                         # the room would be 5,040: the cap of 4,000 -> first 2,400, last 1,000
    assert one == ["t" * 2400 + "\n[… 16600 characters cut]\n" + "t" * 1000]
    fits = big_turn(2, 1000)                         # 2,000 characters is under the share: untouched
    assert fits == ["t" * 1000] * 2


def test_the_users_words_keep_the_first_request_and_the_newest_messages():
    assert cp.render_user_words([]) == "" and cp.render_user_words(["a", "b"]) == "1. a\n2. b"
    words = ["first " + "f" * 6000] + [f"msg{i} " + "m" * 900 for i in range(1, 12)]
    out = cp.render_user_words(words)
    lines = out.split("\n")
    assert lines[0] == "1. " + words[0][:4000] + " [… 2006 characters cut]"
    assert lines[1].startswith("[… ") and lines[1].endswith(" characters of earlier messages left out]")
    assert lines[-1] == f"12. {words[-1]}" and len(out) <= cp.USER_WORDS_MAX_CHARS + 100
    assert [l.split(".")[0] for l in lines[2:]] == [str(n) for n in range(12 - len(lines[2:]) + 1, 13)]     # the newest, in order, whole
    huge = cp.render_user_words(["a", "b" * 20000])
    assert huge.startswith("1. a\n2. b") and huge.endswith(" characters cut]") and len(huge) <= cp.USER_WORDS_MAX_CHARS


def test_the_ledger_renders_what_the_tools_did():
    led = cp.Ledger()
    assert led.is_empty() and led.render() == ""
    lines120 = "\n".join(f"{n}: line" for n in range(1, 121))
    led.record("read_file", {"path": "src/a.ts", "start_line": 1, "end_line": 120}, lines120)
    led.record("read_file", {"path": "src/a.ts", "start_line": 300, "end_line": 380}, "\n".join(f"{n}: l" for n in range(300, 381)))
    led.record("read_file", {"path": "src/b.ts"}, "\n".join(f"{n}: l" for n in range(1, 213)))
    led.record("read_file", {"path": "gone.ts"}, "File not found: gone.ts (resolved: x)", True)
    led.record("edit_file_diff", {"path": "src/a.ts"}, "Successfully patched src/a.ts (+14 lines, -3 lines)")
    led.record("edit_file_diff", {"path": "src/c.ts"}, "Diff Error: search_block not found in file", True)
    led.record("write_file", {"path": "src/new.ts"}, "Successfully created file: src/new.ts (+80 lines, -0 lines)")
    led.record("run_terminal_command", {"command": "npm test"}, "Command failed (exit code 1):\n\nFAIL test/x.test.js\nmore")
    led.record("run_terminal_command", {"command": "git status"}, "clean")
    led.record("search_codebase", {"query": "parseQueueInfo"}, "Matches found in 3 files:\na\nb\nc")
    led.record("list_directory", {"path": "src"}, "x")
    led.record("list_directory", {"path": "src/agent"}, "x")
    led.record("get_workspace_diagnostics", {}, "[ERROR] a\n[ERROR] b\n[WARN] c")
    assert led.render() == "\n".join([
        "Read:", "- src/a.ts lines 1-120, 300-380", "- src/b.ts (whole file, 212 lines)",
        "Changed:", "- src/a.ts: 1 edit (+14 -3)", "- src/new.ts: created (+80)",
        "Ran:", "- `npm test` (exit 1): FAIL test/x.test.js", "- `git status` (exit 0)",
        "Searched:", '- "parseQueueInfo": 3 files',
        "Listed: src, src/agent",
        "Diagnostics:", "- workspace: 2 errors, 1 warning",
        "Refused or failed:", "- read of gone.ts failed: file not found", "- edit to src/c.ts did not apply"])


def test_the_ledger_shortens_oldest_first_and_never_passes_its_limit():
    led = cp.Ledger()
    for i in range(60):
        led.record("run_terminal_command", {"command": f"echo {i} " + "z" * 100}, "ok")
    out = led.render(1500)
    assert len(out) <= 1500 and out.startswith("Ran:\n- … ") and out.endswith("echo 59 " + "z" * 100 + "` (exit 0)")
    assert len(led.ran) == 40                                            # retention: 40 commands
    assert len(cp.Ledger().render(100)) == 0


def test_compaction_keeps_the_prefix_stable_until_it_fires():
    """The property the whole cache argument rests on: while under budget each prompt extends the last one."""
    mem = memory_with(2, tool_chars=800)
    sent = []
    for i in range(6):
        assert cp.compact_history_if_needed(mem, never_asked) is False
        sent.append(json.dumps(mem.build_messages()))
        mem.history.append({"role": "assistant", "content": f"more {i}"})
        mem.history.append({"role": "user", "content": "y" * 800})
    assert len(sent) == 6
    for a, b in zip(sent, sent[1:]):
        assert b.startswith(a[:-1])


# ------------------------------------------------------------------------------------ coordinator shaping

def test_coordinator_adds_penalties_and_model_and_nothing_else_for_small_bodies():
    body = cp.extension_chat_body([{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}])
    shaped, overflow = cp.coordinator_chat_body(body)
    assert overflow == 0
    assert shaped["frequency_penalty"] == 0.0 and shaped["presence_penalty"] == 0.0 and shaped["model"] == cp.DEFAULT_MODEL
    assert shaped["messages"] == body["messages"]
    assert {k: v for k, v in shaped.items() if k not in ("frequency_penalty", "presence_penalty", "model")} == body
    assert "cache_prompt" not in shaped and "id_slot" not in shaped


def test_coordinator_drops_old_messages_when_the_estimate_passes_a_slot():
    big = "w" * 3000
    msgs = [{"role": "system", "content": "s"}] + [{"role": "user", "content": big} for _ in range(40)]
    shaped, overflow = cp.coordinator_chat_body(cp.extension_chat_body(msgs), slot_context=8192)
    assert overflow > 0 and shaped["messages"][0]["role"] == "system"
    assert shaped["messages"][1:] == msgs[-len(shaped["messages"][1:]):]


# ------------------------------------------------------------------------------------------------- FIM

def test_fim_context_windows():
    text = "\n".join(f"line {i:03d} " + "c" * 20 for i in range(200))
    off = text.index("line 100") + 4
    prefix, suffix, mid = cp.fim_context(text, off)
    assert prefix.count("\n") == cp.PREFIX_LINES and prefix.startswith("line 050") and prefix.endswith("line")
    assert suffix.startswith(" 100") and suffix.count("\n") <= cp.SUFFIX_LINES and mid is True
    long_text = "a" * 9000 + "|" + "b" * 9000
    prefix, suffix, _ = cp.fim_context(long_text, 9000)
    assert len(prefix) == cp.PREFIX_CHARS and len(suffix) == cp.SUFFIX_CHARS
    # Just before the `}` the editor typed is the start of a body: not mid-line.
    body = "function f(text) {}"
    assert cp.fim_context(body, len(body) - 1)[2] is False
    assert cp.fim_context("foo(bar)", 4)[2] is True


def test_quantized_prefix_start_holds_still_while_the_shipped_one_moves_every_line():
    text = "\n".join(f"line {i:03d}" for i in range(300))
    starts, quantized = [], []
    for cursor_line in range(200, 216):
        off = text.index(f"line {cursor_line:03d}") + 4
        starts.append(cp.fim_context(text, off)[0].split("\n")[0])
        quantized.append(cp.fim_context(text, off, cp.QUANTUM)[0].split("\n")[0])
    assert len(set(starts)) == 16                       # a new first line of the prompt for every line typed
    assert len(set(quantized)) == 3                     # one per block of 8 lines (16 lines straddle 3 blocks)
    for cursor_line in range(200, 216):
        off = text.index(f"line {cursor_line:03d}") + 4
        n = cp.fim_context(text, off, cp.QUANTUM)[0].count("\n")
        assert cp.PREFIX_LINES <= n < cp.PREFIX_LINES + cp.QUANTUM
    # over the character budget it drops whole blocks from the top, so the start still holds still
    fat = "\n".join("x" * 150 + f" {i:03d}" for i in range(300))
    firsts = {cp.fim_context(fat, fat.index(f" {c:03d}") + 3, cp.QUANTUM)[0].split("\n")[0][-3:] for c in range(200, 208)}
    assert len(firsts) <= 2


def test_fim_layouts_put_the_cursor_text_where_they_claim():
    assert cp.fim_prompt("P", "S", "psm") == "<|fim_prefix|>P<|fim_suffix|>S<|fim_middle|>"
    assert cp.fim_prompt("P", "S", "spm") == "<|fim_suffix|>S<|fim_prefix|>P<|fim_middle|>"


def test_typing_script_grows_the_line_and_the_prompts_share_a_prefix_only_in_spm():
    lines = [f"    statement_{i}();" for i in range(80)]
    steps = list(cp.typing_script(lines, 60, 2, 3))
    assert steps[0][2] == "first" and all(k == "type" for _, _, k in steps[1:5])
    assert [s[2] for s in steps].count("first") == 2
    (t1, o1, k1), (t2, o2, k2) = steps[3], steps[4]
    assert k1 == k2 == "type" and o2 - o1 == 3
    p1, s1, _ = cp.fim_context(t1, o1)
    p2, s2, _ = cp.fim_context(t2, o2)
    assert s1 == s2 and p2.startswith(p1)
    psm = (cp.fim_prompt(p1, s1, "psm"), cp.fim_prompt(p2, s2, "psm"))
    spm = (cp.fim_prompt(p1, s1, "spm"), cp.fim_prompt(p2, s2, "spm"))
    # typing inside a line: the old prompt is a prefix of the new one up to just before <|fim_middle|> only in spm layout
    assert cp.common_prefix_len(*psm) < len(psm[0]) - len(s1) - len("<|fim_suffix|><|fim_middle|>") + 1
    assert cp.common_prefix_len(*spm) >= len(spm[0]) - len("<|fim_middle|>")


# --------------------------------------------------------------------------------------------- the tools

def test_tools_run_in_a_sandbox(tmp_path):
    ws = str(tmp_path)
    (tmp_path / "a.ts").write_text("one\ntwo\nthree\n")
    out, err = cp.run_tool(ws, "read_file", {"path": "a.ts", "start_line": 2, "end_line": 3})
    assert (out, err) == ("2: two\n3: three", False)
    assert cp.run_tool(ws, "read_file", {"path": "../../etc/passwd"})[1] is True
    assert cp.run_tool(ws, "list_directory", {"path": "."})[0] == "      a.ts"
    assert "Matches found in 1 files:\na.ts" == cp.run_tool(ws, "search_codebase", {"query": "two"})[0]
    assert cp.run_tool(ws, "edit_file_diff", {"path": "a.ts", "search_block": "two", "replace_block": "2"})[1] is False
    assert (tmp_path / "a.ts").read_text().startswith("one\n2\n")
    assert cp.run_tool(ws, "edit_file_diff", {"path": "a.ts", "search_block": "nope", "replace_block": "x"})[1] is True
    assert cp.run_tool(ws, "write_file", {"path": "../x", "content": "no"})[1] is True
    assert cp.run_tool(ws, "run_terminal_command", {"command": "rm -rf ."})[1] is True
    assert cp.run_tool(ws, "run_terminal_command", {"command": "grep -n three a.ts"})[0] == "3:three"
    big = cp.cap_output("z" * 9000)
    assert len(big) < 9000 and "Output truncated: 4000 characters omitted" in big


# ------------------------------------------------------------------------------------------- the log reader

LOG = """0.21.352.514 I slot get_availabl: id  0 | task -1 | selected slot by LRU, n_tokens = 94, t_last = 54764668173
0.21.352.601 I slot launch_slot_: id  0 | task 62 | processing task, is_child = 0
0.21.402.240 I slot   operator(): id  0 | task 62 | new prompt, n_ctx_slot = 32768, n_keep = 0, task.n_tokens = 69
0.21.402.244 I slot   operator(): id  0 | task 62 | forcing full prompt re-processing due to lack of cache data (likely due to SWA or hybrid/recurrent memory, see x)
0.21.402.244 I slot   operator(): id  0 | task 62 | erased invalidated context checkpoint (pos_min = 44, pos_max = 44, n_tokens = 45, n_swa = 0, pos_next = 0, size = 62.813 MiB)
0.21.404.486 I slot   operator(): id  0 | task 62 | cached n_tokens = 0, memory_seq_rm [0, end)
0.22.156.376 I slot create_check: id  0 | task 62 | created context checkpoint 1 of 32 (pos_min = 64, pos_max = 64, n_tokens = 65, size = 62.813 MiB)
0.30.000.000 I slot get_availabl: id  1 | task -1 | selected slot by LCP similarity, f_sim_best = 0.950 (> 0.100 thold), f_keep = 0.981
0.30.000.100 I slot launch_slot_: id  1 | task 70 | processing task, is_child = 0
0.30.000.200 I slot   operator(): id  1 | task 70 | new prompt, n_ctx_slot = 32768, n_keep = 0, task.n_tokens = 900
0.30.000.300 I slot   operator(): id  1 | task 70 | restored context checkpoint (pos_min = 40, pos_max = 40, n_tokens = 41, n_past = 41, size = 62.813 MiB)
0.30.000.400 I slot   operator(): id  1 | task 70 | cached n_tokens = 41, memory_seq_rm [41, end)
"""


def test_log_reader():
    rows = cp.parse_server_log(LOG.splitlines())
    assert [r["task"] for r in rows] == [62, 70]
    a, b = rows
    assert a["forced_full_reread"] and a["n_past"] == 0 and a["prompt_tokens"] == 69 and a["erased"] == 1
    assert a["checkpoints_created"] == [(65, 62.813)] and a["selected"] == "by LRU"
    assert b["restored"] == 41 and b["n_past"] == 41 and not b["forced_full_reread"] and b["selected"].startswith("by LCP similarity")


def test_timing_fields():
    f = cp.timing_fields({"prompt_n": 30, "cache_n": 970, "prompt_ms": 80.04, "predicted_n": 12, "predicted_ms": 400.0})
    assert f["prompt_total"] == 1000 and f["reuse_pct"] == 97.0 and f["prompt_ms"] == 80.0
    assert cp.timing_fields({}, {"prompt_tokens": 55})["prompt_total"] == 55


# --------------------------------------------------------------------------- a whole session, fake server

class FakeEngine(http.server.BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/apply-template":
            text = "".join(f"<{m['role']}>{m.get('content') or ''}" for m in body["messages"])
            return self._json({"prompt": text})
        if self.path == "/tokenize":
            return self._json({"tokens": list(range(len(body["content"]) // 4))})
        FakeEngine.seen.append(body)
        n = len(FakeEngine.seen)
        if n == 1:
            chunks = [{"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "abc", "function": {"name": "read_file", "arguments": ""}}]}}]},
                      {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"path":"a.ts"}'}}]}}]},
                      {"choices": [{"delta": {}, "finish_reason": "tool_calls"}], "timings": {"prompt_n": 500, "cache_n": 0, "prompt_ms": 900.0, "predicted_n": 20, "predicted_ms": 600.0}}]
        else:
            chunks = [{"choices": [{"delta": {"content": "done"}}]},
                      {"choices": [{"delta": {}, "finish_reason": "stop"}], "timings": {"prompt_n": 40, "cache_n": 520, "prompt_ms": 90.0, "predicted_n": 3, "predicted_ms": 80.0}}]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for c in chunks:
            self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")

    def _json(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def fake_engine():
    FakeEngine.seen = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeEngine)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_agent_session_builds_the_extension_history_and_reads_timings(fake_engine, tmp_path):
    (tmp_path / "a.ts").write_text("hello\n")
    sender = cp.Sender(fake_engine, shape="coordinator", pin_slot=1, seed=7)
    sess = cp.AgentSession(sender, str(tmp_path))
    sess.user("look at a.ts")
    r1, done1 = sess.step()
    r2, done2 = sess.step()
    assert (done1, done2) == (False, True)
    first, second = FakeEngine.seen
    assert first["id_slot"] == 1 and first["seed"] == 7 and first["frequency_penalty"] == 0.0 and first["stream"] is True
    assert [m["role"] for m in second["messages"]] == ["system", "user", "assistant", "tool"]
    assert second["messages"][2]["tool_calls"] == [{"id": "abc", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"a.ts"}'}}]
    assert second["messages"][3] == {"role": "tool", "tool_call_id": "abc", "name": "read_file", "content": "1: hello\n2: "}
    assert second["messages"][:2] == first["messages"]                       # append-only
    assert (r1["prompt_total"], r1["cache_n"], r1["tools"]) == (500, 0, ["read_file"])
    assert (r2["prompt_total"], r2["prompt_n"], r2["reuse_pct"]) == (560, 40, 92.9)
    assert r2["lcp_chars_with_previous_prompt"] == len("<system>" + cp.AGENT_SYSTEM_PROMPT + "<user>look at a.ts")


class NotesEngine(FakeEngine):
    """Answers a request without tools with the model's notes (the probe's notes request), one with tools with a plain answer."""
    seen = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/apply-template":
            return self._json({"prompt": "".join(f"<{m['role']}>{m.get('content') or ''}" for m in body["messages"])})
        if self.path == "/tokenize":
            return self._json({"tokens": []})
        NotesEngine.seen.append(body)
        text = "<think>t</think>### Goal\nlook at a.ts" if "tools" not in body else "done"
        chunks = [{"choices": [{"delta": {"content": text}}]},
                  {"choices": [{"delta": {}, "finish_reason": "stop"}], "timings": {"prompt_n": 900, "cache_n": 0, "prompt_ms": 50.0, "predicted_n": 5, "predicted_ms": 10.0}}]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for c in chunks:
            self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


def test_a_live_compaction_sends_the_notes_request_without_tools_then_the_working_memory(tmp_path):
    NotesEngine.seen = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), NotesEngine)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        sender = cp.Sender(f"http://127.0.0.1:{srv.server_address[1]}", seed=3)
        sess = cp.AgentSession(sender, str(tmp_path), compact_budget=1500)       # tiny budget: the first request is already over it
        sess.user("look at a.ts")
        rec, done = sess.step()
    finally:
        srv.shutdown()
        srv.server_close()
    notes_body, turn_body = NotesEngine.seen
    assert "tools" not in notes_body and notes_body["max_tokens"] == 10240 and notes_body["stream"] is True and notes_body["seed"] == 3
    assert notes_body["messages"][-1] == {"role": "user", "content": cp.COMPACT_REQUEST}
    assert [m["role"] for m in notes_body["messages"]] == ["system", "user", "user"]
    assert turn_body["tools"] == cp.TOOLS and turn_body["max_tokens"] == 10240
    assert [m["role"] for m in turn_body["messages"]] == ["system", "user", "user"]
    wm = turn_body["messages"][1]["content"]
    assert wm.startswith(cp.WORKING_MEMORY_OPEN) and "## The user's words\n1. look at a.ts" in wm and "## Your notes\n### Goal\nlook at a.ts" in wm
    assert turn_body["messages"][2] == {"role": "user", "content": "look at a.ts"}
    notes_rec, turn_rec = sess.records
    assert notes_rec["notes_request"] and notes_rec["tools"] == ["(notes request)"] and (turn_rec["compacted"], done) == (True, True)
    assert sess.mem.chars_per_token != cp.DEFAULT_CHARS_PER_TOKEN               # the answer's prompt size calibrates the estimate
