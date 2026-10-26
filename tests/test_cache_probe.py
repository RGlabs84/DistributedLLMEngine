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

@needs_extension
def test_system_prompt_is_the_one_the_agent_sends():
    import re
    src = read_ts("agent", "agentLoop.ts")
    m = re.search(r"content: `(You are the DLNE coding agent.*?)`\s*\}\s*\];", src, re.S)
    assert m, "the system prompt literal moved in agentLoop.ts; update cache_probe.AGENT_SYSTEM_PROMPT"
    assert cp.AGENT_SYSTEM_PROMPT == m.group(1).replace("\\`", "`")


@needs_extension
def test_tool_list_matches_tools_ts():
    src = read_ts("agent", "tools.ts")
    for t in cp.TOOLS:
        fn = t["function"]
        assert f"name: '{fn['name']}'" in src
        assert f"description: '{fn['description']}'" in src
        for prop, spec in fn["parameters"]["properties"].items():
            assert f"{prop}: {{ type: '{spec['type']}', description: '{spec['description']}' }}" in src, prop
        if fn["parameters"].get("required"):
            assert "required: [" + ", ".join(f"'{r}'" for r in fn["parameters"]["required"]) + "]" in src
    assert src.count("name: '") >= len(cp.TOOLS)


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
    assert "temperature: 0.2," in src and "max_tokens: 4096" in src
    body = cp.extension_chat_body([{"role": "user", "content": "x"}])
    assert body["temperature"] == 0.2 and body["max_tokens"] == 4096 and body["stream"] is True and body["tools"] == cp.TOOLS


@needs_extension
def test_compaction_budget_matches_the_agent_loop():
    src = read_ts("agent", "agentLoop.ts")
    assert f"this.compactHistoryIfNeeded({cp.COMPACT_BUDGET});" in src
    assert "keepTail = 6" in src and "Math.ceil((msg.content?.length || 0) / 3.2)" in src


# ------------------------------------------------------------------------------------------- compaction

def history(n_tool_rounds, tool_chars=1200):
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "do it"}]
    for i in range(n_tool_rounds):
        msgs.append({"role": "assistant", "content": f"step {i}",
                     "tool_calls": [{"id": f"c{i}", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"a.ts"}'}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "name": "read_file", "content": ("x" * tool_chars) + str(i)})
    return msgs


def test_under_budget_the_history_is_left_alone():
    msgs = history(3)
    before = json.dumps(msgs)
    cp.compact_history_if_needed(msgs, 11000)
    assert json.dumps(msgs) == before


def test_over_budget_rewrites_the_early_history():
    msgs = history(14, tool_chars=3000)
    assert cp.estimate_total_tokens(msgs) > 11000
    first_tool = dict(msgs[3])
    cp.compact_history_if_needed(msgs, 11000)
    assert msgs[3] != first_tool and "Earlier tool output compacted" in msgs[3]["content"]     # step 2 rewrote an early message
    assert msgs[0]["content"] == "s" and msgs[1]["content"] == "do it"
    assert msgs[-1]["content"].startswith("x" * 100)                                            # the newest result is untouched


def test_over_budget_with_long_replies_collapses_the_middle():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "do it"}]
    for i in range(20):
        msgs.append({"role": "assistant" if i % 2 == 0 else "user", "content": f"{i}" * 2000})
    assert cp.estimate_total_tokens(msgs) > 11000
    cp.compact_history_if_needed(msgs, 11000)
    assert "Context Compaction: Compacted 14 intermediate execution steps" in msgs[2]["content"]
    assert len(msgs) == 2 + 1 + 6


def test_compaction_target_is_where_a_pass_that_fires_ends_up():
    """Shipped: a pass that fires stops as soon as the history is under the budget. With a target it goes lower."""
    def grow(target, budget=4000):
        msgs = history(3, tool_chars=2500)
        fired = []
        for i in range(24):
            msgs.append({"role": "assistant", "content": f"step {i}",
                         "tool_calls": [{"id": f"n{i}", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"b.ts"}'}}]})
            msgs.append({"role": "tool", "tool_call_id": f"n{i}", "name": "read_file", "content": ("y" * 2500) + str(i)})
            before = json.dumps(msgs)
            cp.compact_history_if_needed(msgs, budget, target)
            if json.dumps(msgs) != before:
                fired.append(cp.estimate_total_tokens(msgs))
        return fired
    shipped, targeted = grow(None), grow(2400)
    assert shipped and targeted
    assert all(e <= 4000 for e in shipped) and max(shipped) > 2400          # just under the budget is enough
    assert all(e <= 2400 for e in targeted)                                 # the target is honoured


def test_compaction_keeps_the_prefix_stable_until_it_fires():
    """The property the whole cache argument rests on: while under budget each prompt extends the last one."""
    msgs = history(2)
    sent = []
    for i in range(6):
        cp.compact_history_if_needed(msgs, 11000)
        sent.append(json.dumps(msgs))
        msgs.append({"role": "assistant", "content": f"more {i}"})
        msgs.append({"role": "user", "content": "y" * 800})
    for a, b in zip(sent, sent[1:]):
        if len(json.loads(b)) > len(json.loads(a)) and cp.estimate_total_tokens(json.loads(b)) <= 11000:
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
