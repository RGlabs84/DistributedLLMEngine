#!/usr/bin/env python3
"""cache_probe.py - does the coding model reuse its prompt cache between turns and keystrokes?

The coding model (Qwen3.6-35B-A3B) is a hybrid: 30 recurrent Gated-DeltaNet layers and 10 attention layers.
A recurrent state cannot be rolled back to the middle of a prompt, so llama.cpp keeps "context checkpoints"
and restores one when a new request shares only a prefix with the cached prompt. This script measures what
that really buys, with the request JSON the VS Code extension sends:

  agent  a live agent loop (the extension's system prompt, tools, history shape and compaction); the model
         drives, the tools run in a throw-away workspace. One row per request: prompt tokens in total,
         tokens re-read (prompt_n) and tokens reused (cache_n), from llama-server's own `timings`.
  fim    ghost-text autocomplete: a developer retyping lines of a real file, one request per few keystrokes,
         prompt built exactly like src/completion/fimProvider.ts (layout psm = what ships; spm and warm are
         the candidate fixes).
  fimq   quality of psm vs spm on the bake-off's 12-case FIM suite (needs --suite-file fim_check.py).
  mixed  two agent sessions and a FIM typist sharing one server (slot choice, host prompt cache).
  log    summarise a llama-server -lv 4 log: slot choice, checkpoint restore / forced full re-read, sizes.

Every request goes to the server named by --base-url (llama-server itself, or a tunnel to it). The
coordinator's own changes to the body (engine/coordinator.py chat_completions) are applied with
--shape coordinator. --pin-slot N adds "id_slot": N to every request (required when the server is TheWall's
live engine, so users' slots stay untouched). Standard library only.

  cache_probe.py agent --base-url http://127.0.0.1:8080 --tasks 1,2 --out agent.json
  cache_probe.py fim   --base-url http://127.0.0.1:8080 --layout spm --out fim-spm.json
  cache_probe.py log   server.log
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# ---------------------------------------------------------------------------------------------------------
# What the extension sends (vscode-extension/src/agent/agentLoop.ts, agent/tools.ts, client/dlneClient.ts).
# tests/test_cache_probe.py compares these copies with the TypeScript so they cannot drift unnoticed.
# ---------------------------------------------------------------------------------------------------------

AGENT_SYSTEM_PROMPT = """You are the DLNE coding agent, a software engineering assistant working inside the user's VS Code workspace.

Use your tools to do the work rather than telling the user to do it:
- read_file(path, start_line, end_line), list_directory(path), search_codebase(query), get_workspace_diagnostics(file_path): look before you change anything.
- edit_file_diff(path, search_block, replace_block): change part of a file; search_block must match the file exactly, with 2-4 lines of context.
- write_file(path, content): create a file, or replace one entirely.
- run_terminal_command(command): run a shell command in the workspace (tests, builds, git).

Paths are relative to the workspace root. The user approves edits, commands, and anything outside the workspace; if a tool result says the user declined, do not retry it - ask or take another approach. After acting, check the result and say briefly what you changed.

Secrets (keys, tokens, passwords) in files and output are shown to you as [REDACTED], and some files (.env, key files, anything in .gitignore or .dlneignore) cannot be read at all. Never write [REDACTED] into a file or put it in a search_block; if you need such a value, ask the user to supply it.

If your output cannot carry native tool calls, emit one as a JSON block:
```json
{"name": "read_file", "arguments": {"path": "src/index.ts"}}
```

Begin with a short <think> plan."""


def _fn(name, description, properties, required=None):
    params = {"type": "object", "properties": properties}
    if required:
        params["required"] = required
    return {"type": "function", "function": {"name": name, "description": description, "parameters": params}}


TOOLS = [
    _fn("read_file", "Read the contents of a file with line numbers. Supports relative workspace paths or absolute paths.",
        {"path": {"type": "string", "description": "Relative path in workspace or absolute path (e.g. src/app.ts, ~/notes.txt)"},
         "start_line": {"type": "number", "description": "1-based starting line number (optional)"},
         "end_line": {"type": "number", "description": "1-based ending line number (optional)"}}, ["path"]),
    _fn("write_file", "Create a new file or completely overwrite an existing file with new content. Supports relative or absolute paths.",
        {"path": {"type": "string", "description": "Path to the file to create or overwrite"},
         "content": {"type": "string", "description": "Full text content to write"}}, ["path", "content"]),
    _fn("edit_file_diff", "Surgically replace a matching block of code in a file. Include 2-4 lines of surrounding context in search_block.",
        {"path": {"type": "string", "description": "Path to the file to edit"},
         "search_block": {"type": "string", "description": "Exact existing code block to find"},
         "replace_block": {"type": "string", "description": "New code block to replace it with"}}, ["path", "search_block", "replace_block"]),
    _fn("list_directory", "List the files and subdirectories in a directory. Supports relative workspace paths, home (~) or absolute paths.",
        {"path": {"type": "string", "description": "Directory path (use . for root, ~ for home, or absolute path)"}}, ["path"]),
    _fn("search_codebase", "Search workspace files for a regex or text pattern.",
        {"query": {"type": "string", "description": "Text or regex pattern to search for"}}, ["query"]),
    _fn("run_terminal_command", "Execute a shell command locally in the bash/terminal environment (e.g. bash commands, find, cp, ls, tests, git) and return stdout/stderr.",
        {"command": {"type": "string", "description": "Shell command to execute"}}, ["command"]),
    _fn("get_workspace_diagnostics", "Get active compiler, linter, and type-check errors in the workspace or specific file.",
        {"file_path": {"type": "string", "description": "Optional relative file path"}}),
]

# fimProvider.ts
PREFIX_LINES, PREFIX_CHARS, SUFFIX_LINES, SUFFIX_CHARS, FIM_MAX_TOKENS = 50, 4000, 10, 600, 48
FIM_STOP = ["<|fim_pad|>", "<|file_separator|>", "<|im_end|>", "\n\n\n"]
QUANTUM = 8                     # the proposed prefix start granularity, in lines (fim_context(quantum=...))

COMPACT_BUDGET = 11000          # agentLoop.run(): this.compactHistoryIfNeeded(11000)
MAX_TOOL_CHARS = 8000           # tools.ts capOutput
DEFAULT_MODEL = "Qwen3.6-35B-A3B-UD-IQ3_S.gguf"
COORDINATOR_SLOT_CONTEXT = 65536 // 2     # -c 65536 -np 2


# ---------------------------------------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------------------------------------

class ProbeError(RuntimeError):
    pass


def _request(base, path, body, timeout):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base.rstrip("/") + path, data=data, headers={"Content-Type": "application/json"})
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise ProbeError(f"HTTP {e.code} from {path}: {e.read()[:400].decode('utf-8', 'replace')}") from None
    except urllib.error.URLError as e:
        raise ProbeError(f"{path}: {e.reason}") from None


def post_json(base, path, body, timeout=900):
    with _request(base, path, body, timeout) as r:
        return json.load(r)


def get_json(base, path, timeout=30):
    with _request(base, path, None, timeout) as r:
        return json.load(r)


def chat_stream(base, body, timeout=1800):
    """POST /v1/chat/completions with stream true, the way the extension reads it.

    Returns dict(content, reasoning, tool_calls[{id,name,arguments}], timings, usage, finish, ttft, wall)."""
    t0 = time.monotonic()
    out = {"content": "", "reasoning": "", "tool_calls": [], "timings": {}, "usage": {}, "finish": None, "ttft": None}
    calls = {}
    with _request(base, "/v1/chat/completions", body, timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            try:
                obj = json.loads(line[6:])
            except ValueError:
                continue
            if obj.get("timings"):
                out["timings"] = obj["timings"]
            if obj.get("usage"):
                out["usage"] = obj["usage"]
            for ch in obj.get("choices") or []:
                d = ch.get("delta") or {}
                if d.get("content") or d.get("reasoning_content") or d.get("tool_calls"):
                    if out["ttft"] is None:
                        out["ttft"] = time.monotonic() - t0
                out["content"] += d.get("content") or ""
                out["reasoning"] += d.get("reasoning_content") or ""
                for tc in d.get("tool_calls") or []:
                    cur = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "arguments": ""})
                    if tc.get("id"):
                        cur["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        cur["name"] = fn["name"]
                    cur["arguments"] += fn.get("arguments") or ""
                if ch.get("finish_reason"):
                    out["finish"] = ch["finish_reason"]
    out["tool_calls"] = [calls[i] for i in sorted(calls)]
    out["wall"] = time.monotonic() - t0
    return out


def timing_fields(timings, usage=None):
    """The numbers every report uses, from llama-server's `timings` (cache_n = tokens reused)."""
    usage = usage or {}
    prompt_n = int(timings.get("prompt_n", 0) or 0)
    cache_n = int(timings.get("cache_n", 0) or 0)
    total = prompt_n + cache_n
    if not total and usage.get("prompt_tokens"):
        total = int(usage["prompt_tokens"])
    return {
        "prompt_total": total, "prompt_n": prompt_n, "cache_n": cache_n,
        "reuse_pct": round(100.0 * cache_n / total, 1) if total else None,
        "prompt_ms": round(float(timings.get("prompt_ms", 0) or 0), 1),
        "predicted_n": int(timings.get("predicted_n", 0) or 0),
        "predicted_ms": round(float(timings.get("predicted_ms", 0) or 0), 1),
    }


# ---------------------------------------------------------------------------------------------------------
# Request shaping
# ---------------------------------------------------------------------------------------------------------

def extension_chat_body(messages, seed=None, pin_slot=None, template_kwargs=None):
    """streamChat(): {messages, stream, temperature 0.2, max_tokens 4096, tools}."""
    body = {"messages": messages, "stream": True, "temperature": 0.2, "max_tokens": 4096, "tools": TOOLS}
    if seed is not None:
        body["seed"] = seed
    if pin_slot is not None:
        body["id_slot"] = pin_slot
    if template_kwargs:
        body["chat_template_kwargs"] = template_kwargs
    return body


def _est_len(text):
    return max(1, int(len(text or "") / 3.0))


def coordinator_chat_body(body, slot_context=COORDINATOR_SLOT_CONTEXT, model=DEFAULT_MODEL):
    """What engine/coordinator.py chat_completions does to the body before llama-server sees it.

    Ported from lines ~2222-2234 (penalties, model) and ~2398-2507 (the context ceiling). Returns
    (body, overflow) where overflow is the number of old messages the real coordinator would have dropped
    and summarised into the system prompt (that call is not reproduced: a changed system prompt makes the
    whole prompt cache miss, which is the point to know about)."""
    body = dict(body)
    body["frequency_penalty"] = 0.0
    body["presence_penalty"] = 0.0
    body["model"] = model
    messages = [dict(m) for m in body["messages"]]
    system = messages[0] if messages and messages[0].get("role") == "system" else None
    system_tokens = _est_len(system.get("content")) if system else 0
    allowed = max(100, slot_context - 512 - system_tokens)
    rest = messages[1 if system else 0:]
    kept, overflow, cumulative, overflowing = [], 0, 0, False
    for i, msg in enumerate(reversed(rest)):
        newest = i == 0
        tokens = _est_len(msg.get("content"))
        if not overflowing and (cumulative + tokens <= allowed or newest):
            if newest and cumulative + tokens > allowed and msg.get("content"):
                msg["content"] = "...[Text omitted for memory]...\n" + msg["content"][-allowed * 3:]
                cumulative += allowed
            else:
                cumulative += tokens
            kept.append(msg)
        else:
            overflowing = True
            overflow += 1
    kept.reverse()
    body["messages"] = ([system] if system else []) + kept
    return body, overflow


# ---------------------------------------------------------------------------------------------------------
# The agent's history compaction (agentLoop.ts compactHistoryIfNeeded), ported step for step
# ---------------------------------------------------------------------------------------------------------

def estimate_message_tokens(msg):
    tokens = math.ceil(len(msg.get("content") or "") / 3.2)
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        tokens += math.ceil(len(fn.get("name") or "") / 3.2)
        tokens += math.ceil(len(fn.get("arguments") or "") / 3.2)
    return tokens + 4


def estimate_total_tokens(messages):
    return sum(estimate_message_tokens(m) for m in messages)


def compact_history_if_needed(messages, max_tokens=COMPACT_BUDGET, target_tokens=None):
    """Mutates and returns `messages`. Under budget nothing changes, so the prompt only ever grows at the end.

    target_tokens=None is the shipping behaviour: once over `max_tokens` every turn shrinks the history to just
    under it, so the next turn is over again and rewrites an early message (the cached prefix is lost every turn).
    With target_tokens (the proposed change) a pass that fires shrinks the history to the target, so the following
    turns are append-only until it has grown back to `max_tokens`."""
    goal = max_tokens if target_tokens is None else target_tokens
    if len(messages) <= 3 or estimate_total_tokens(messages) <= max_tokens:
        return messages
    last_assistant = max((i for i, m in enumerate(messages) if m["role"] == "assistant"), default=-1)
    for i, m in enumerate(messages):                                         # step 1: old <think> blocks
        if m["role"] == "assistant" and i != last_assistant and m.get("content"):
            cleaned = re.sub(r"<think>[\s\S]*?</think>", "", m["content"]).strip()
            if cleaned:
                m["content"] = cleaned
    recent = max(2, len(messages) - 4)                                       # step 2: old tool observations
    for i in range(1, recent):
        m = messages[i]
        c = m.get("content") or ""
        if m["role"] == "tool" and len(c) > 300:
            m["content"] = c[:150] + f"\n... [Earlier tool output compacted: {len(c) - 250} chars omitted] ...\n" + c[-100:]
        elif m["role"] == "user" and "Observation from tool [" in c and len(c) > 400:
            m["content"] = c[:200] + f"\n... [Earlier tool observations compacted: {len(c) - 300} chars omitted] ...\n" + c[-100:]
    if estimate_total_tokens(messages) <= goal:                              # step 3
        return messages
    keep_tail = 6                                                            # step 4: sliding window
    if len(messages) > 2 + keep_tail:
        middle_end = len(messages) - keep_tail
        middle = messages[2:middle_end]
        tools_run = []
        for m in middle:
            if m["role"] == "tool" and m.get("name"):
                tools_run.append(m["name"])
            elif m["role"] == "assistant" and m.get("tool_calls"):
                tools_run += [tc["function"]["name"] for tc in m["tool_calls"] if tc.get("function", {}).get("name")]
        role = "user" if messages[middle_end]["role"] == "assistant" else "assistant"
        unique = list(dict.fromkeys(tools_run))[:6]
        text = (f"[Context Compaction: Compacted {len(middle)} intermediate execution steps to preserve context headroom]:\n"
                f"- Completed operations: {', '.join(unique) if unique else 'file and system operations'}.\n"
                "Proceeding with next active steps toward user objective.")
        messages[:] = [messages[0], messages[1], {"role": role, "content": text}] + messages[middle_end:]
    if estimate_total_tokens(messages) > goal:                               # step 5: cap oversized messages
        for m in messages[1:]:
            c = m.get("content") or ""
            if len(c) > 2500:
                m["content"] = c[:1400] + f"\n... [Message trimmed to prevent context overflow: {len(c) - 2200} chars omitted] ...\n" + c[-800:]
    return messages


# ---------------------------------------------------------------------------------------------------------
# A throw-away workspace and the tools that run in it (tools.ts executeToolInternal, without VS Code)
# ---------------------------------------------------------------------------------------------------------

WORKSPACE_FILES = [
    "package.json", "src/completion/fimProvider.ts", "src/client/pipeline.ts", "src/client/session.ts",
    "src/client/dlneClient.ts", "src/agent/agentLoop.ts", "src/agent/tools.ts", "src/agent/diffManager.ts",
]

TASKS = {
    1: "Autocomplete in this extension builds its prompt in src/completion/fimProvider.ts. Read it, change SUFFIX_LINES from 10 to 12, "
       "and tell me which other files in src mention that constant.",
    2: "Thanks. Now look at src/agent/tools.ts and tell me which tools the agent exposes and whether any of them can write outside the workspace.",
    3: "Read src/client/dlneClient.ts and src/agent/agentLoop.ts and explain, in a short paragraph, how a failed request is retried.",
    4: "Last thing: run `grep -rn SUFFIX_LINES src` and confirm the change is in place.",
    5: "Read src/client/pipeline.ts and src/client/session.ts and describe how the session id is created and where it is sent.",
    6: "Read src/agent/diffManager.ts and explain in three sentences how a fuzzy diff is applied.",
    7: "Compare how src/agent/tools.ts and src/agent/agentLoop.ts each cap or shorten large outputs, and list the limits.",
    8: "Finally, read src/completion/fimProvider.ts once more and tell me what the current prefix and suffix budgets are.",
}
SAFE_COMMANDS = {"grep", "ls", "cat", "wc", "head", "tail", "find", "pwd", "echo", "git", "sed", "awk", "sort", "uniq", "diff"}


def make_workspace(source_dir):
    """Copy the sample files into a temp dir. `source_dir` is the vscode-extension folder."""
    ws = tempfile.mkdtemp(prefix="dlne-probe-ws-")
    for rel in WORKSPACE_FILES:
        src = os.path.join(source_dir, rel)
        if os.path.exists(src):
            os.makedirs(os.path.dirname(os.path.join(ws, rel)), exist_ok=True)
            shutil.copy(src, os.path.join(ws, rel))
    return ws


def _inside(ws, rel):
    path = os.path.realpath(os.path.join(ws, rel))
    return path if path == os.path.realpath(ws) or path.startswith(os.path.realpath(ws) + os.sep) else None


def cap_output(text):
    if len(text) > MAX_TOOL_CHARS:
        omitted = len(text) - 5000
        return (text[:3500] + f"\n\n... [Output truncated: {omitted} characters omitted to preserve context window. "
                "Use start_line/end_line or grep to view specific sections] ...\n\n" + text[-1500:])
    return text


def run_tool(ws, name, args):
    """-> (output, is_error)."""
    try:
        if name == "read_file":
            p = _inside(ws, str(args.get("path", "")))
            if not p or not os.path.isfile(p):
                return f"File not found: {args.get('path')} (resolved: {p})", True
            lines = open(p, encoding="utf-8", errors="replace").read().split("\n")
            start = max(1, int(args.get("start_line") or 1)) - 1
            end = min(len(lines), int(args["end_line"])) if args.get("end_line") else len(lines)
            return cap_output("\n".join(f"{start + i + 1}: {l}" for i, l in enumerate(lines[start:end]))), False
        if name == "list_directory":
            p = _inside(ws, str(args.get("path") or "."))
            if not p or not os.path.isdir(p):
                return f"Directory not found: {args.get('path') or '.'} (resolved: {p})", True
            ents = sorted(os.scandir(p), key=lambda e: e.name)
            return cap_output("\n".join(("[DIR] " if e.is_dir() else "      ") + e.name for e in ents) or "Empty directory"), False
        if name == "search_codebase":
            q = str(args.get("query", ""))
            hits = []
            for root, _dirs, files in os.walk(ws):
                for f in sorted(files):
                    fp = os.path.join(root, f)
                    try:
                        if q and q in open(fp, encoding="utf-8").read():
                            hits.append(os.path.relpath(fp, ws))
                    except (UnicodeDecodeError, OSError):
                        pass
            return (f"Matches found in {len(hits)} files:\n" + "\n".join(hits)) if hits else f'No matches found for "{q}"', False
        if name == "edit_file_diff":
            p = _inside(ws, str(args.get("path", "")))
            if not p or not os.path.isfile(p):
                return f"File not found: {args.get('path')} (resolved: {p})", True
            text = open(p, encoding="utf-8").read()
            s = str(args.get("search_block", ""))
            if not s or s not in text:
                return "Diff Error: search_block not found in file", True
            open(p, "w", encoding="utf-8").write(text.replace(s, str(args.get("replace_block", "")), 1))
            return f"Successfully patched {os.path.relpath(p, ws)} (exact match)", False
        if name == "write_file":
            p = _inside(ws, str(args.get("path", "")))
            if not p:
                return "Refused: path outside the workspace", True
            existed = os.path.exists(p)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            content = str(args.get("content", ""))
            open(p, "w", encoding="utf-8").write(content)
            n = content.count("\n") + 1
            return f"Successfully {'updated' if existed else 'created'} file: {os.path.relpath(p, ws)} (+{n} lines, -0 lines)", False
        if name == "run_terminal_command":
            cmd = str(args.get("command", "")).strip()
            first = cmd.split()[0] if cmd.split() else ""
            if first not in SAFE_COMMANDS or any(t in cmd for t in ("rm ", ">", "|sh", "| sh", "sudo", "curl", "wget")):
                return "Command failed (exit code 126):\nthis probe only runs read-only commands (grep, ls, cat, wc, head, tail, find)", True
            r = subprocess.run(cmd, shell=True, cwd=ws, capture_output=True, text=True, timeout=20)
            out = "\n".join(x for x in (r.stdout.strip(), r.stderr.strip()) if x)
            return cap_output(out or "(Command completed successfully with no output)") if r.returncode == 0 else \
                f"Command failed (exit code {r.returncode}):\n{out}", r.returncode != 0
        if name == "get_workspace_diagnostics":
            return "No errors or warnings found.", False
        return f"Unknown tool: {name}", True
    except Exception as e:  # noqa: BLE001 - a tool failure is an observation, as in the extension
        return f"Tool execution failed: {e}", True


def parse_args_json(raw):
    try:
        v = json.loads(raw) if raw else {}
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


# ---------------------------------------------------------------------------------------------------------
# The agent session
# ---------------------------------------------------------------------------------------------------------

def common_prefix_len(a, b):
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


class Sender:
    """One place that turns messages into the body a given deployment sees, sends it, and keeps the rendered prompt."""

    def __init__(self, base, shape="direct", pin_slot=None, seed=1234, template_kwargs=None, render=True, timeout=1800):
        self.base, self.shape, self.pin, self.seed = base, shape, pin_slot, seed
        self.template_kwargs, self.render, self.timeout = template_kwargs, render, timeout

    def body_for(self, messages):
        body = extension_chat_body(messages, seed=self.seed, pin_slot=self.pin, template_kwargs=self.template_kwargs)
        overflow = 0
        if self.shape == "coordinator":
            body, overflow = coordinator_chat_body(body)
        return body, overflow

    def rendered_prompt(self, body):
        """The exact text llama-server will tokenize (POST /apply-template), or None if the endpoint is absent."""
        if not self.render:
            return None
        try:
            probe = {k: body[k] for k in ("messages", "tools", "chat_template_kwargs") if k in body}
            return post_json(self.base, "/apply-template", probe, timeout=60).get("prompt")
        except ProbeError:
            self.render = False
            return None

    def count_tokens(self, text):
        try:
            return len(post_json(self.base, "/tokenize", {"content": text, "parse_special": True}, timeout=60).get("tokens", []))
        except ProbeError:
            return None


class AgentSession:
    def __init__(self, sender, ws, compact_budget=COMPACT_BUDGET, compact_target=None, name="A"):
        """compact_budget 0 turns the agent's history compaction off; compact_target is the proposed hysteresis."""
        self.sender, self.ws, self.name = sender, ws, name
        self.compact_budget, self.compact_target = compact_budget, compact_target
        self.messages = [{"role": "system", "content": _tag("chat") + AGENT_SYSTEM_PROMPT}]
        self.turn = 0
        self.prev_prompt = None
        self.last_body = None
        self.records = []

    def cold_read(self):
        """What the last request would have cost with no cache at all: the same body with cache_prompt false, one token out.
        This is what a conversation pays when it lands on a machine that has never seen it."""
        body = dict(self.last_body, stream=False, max_tokens=1, cache_prompt=False)
        t0 = time.monotonic()
        r = post_json(self.sender.base, "/v1/chat/completions", body, timeout=self.sender.timeout)
        rec = timing_fields(r.get("timings") or {}, r.get("usage"))
        rec["wall_s"] = round(time.monotonic() - t0, 2)
        return rec

    def user(self, text):
        self.messages.append({"role": "user", "content": text})

    def step(self):
        """One model request plus the tools it asked for. Returns (record, finished)."""
        before = json.dumps(self.messages)
        if self.compact_budget:
            compact_history_if_needed(self.messages, self.compact_budget, self.compact_target)
        compacted = json.dumps(self.messages) != before
        body, overflow = self.sender.body_for(self.messages)
        self.last_body = body
        prompt = self.sender.rendered_prompt(body)
        res = chat_stream(self.sender.base, body, timeout=self.sender.timeout)
        self.turn += 1
        rec = {"session": self.name, "turn": self.turn, "compacted": compacted, "messages": len(self.messages),
               "est_history_tokens": estimate_total_tokens(self.messages),
               "coordinator_overflow": overflow, "finish": res["finish"], "gen_chars": len(res["content"]),
               "wall_s": round(res["wall"], 2), "ttft_s": round(res["ttft"], 2) if res["ttft"] is not None else None,
               "tools": [tc["name"] for tc in res["tool_calls"]]}
        rec.update(timing_fields(res["timings"], res["usage"]))
        if prompt is not None:
            rec["prompt_chars"] = len(prompt)
            if self.prev_prompt is not None:
                lcp = common_prefix_len(self.prev_prompt, prompt)
                rec["lcp_chars_with_previous_prompt"] = lcp
                rec["previous_prompt_chars"] = len(self.prev_prompt)
                rec["previous_prompt_tail_not_shared"] = self.prev_prompt[lcp:lcp + 80]
                rec["new_prompt_text_at_divergence"] = prompt[lcp:lcp + 80]
                lcp_tok = self.sender.count_tokens(prompt[:lcp])
                if lcp_tok is not None:
                    rec["lcp_tokens_with_previous_prompt"] = lcp_tok
            self.prev_prompt = prompt
        self.records.append(rec)
        if not res["tool_calls"]:
            self.messages.append({"role": "assistant", "content": res["content"]})
            return rec, True
        calls, observations = [], []
        for tc in res["tool_calls"]:
            args = parse_args_json(tc["arguments"])
            call_id = tc["id"] or f"call_{self.turn}_{len(calls)}"
            calls.append({"id": call_id, "type": "function",
                          "function": {"name": tc["name"], "arguments": json.dumps(args, separators=(",", ":"), ensure_ascii=False)}})
            out, _err = run_tool(self.ws, tc["name"], args)
            observations.append({"role": "tool", "tool_call_id": call_id, "name": tc["name"], "content": out})
        self.messages.append({"role": "assistant", "content": res["content"], "tool_calls": calls})
        self.messages.extend(observations)
        return rec, False

    def run_task(self, text, max_turns=8):
        self.user(text)
        for _ in range(max_turns):
            rec, done = self.step()
            print_agent_row(rec)
            if done:
                break
        return self.records


def print_agent_row(r):
    reuse = "-" if r["reuse_pct"] is None else f"{r['reuse_pct']:5.1f}%"
    print(f"  {r['session']}{r['turn']:>2} msgs={r['messages']:>3} prompt={r['prompt_total']:>6} re-read={r['prompt_n']:>6} reused={r['cache_n']:>6} "
          f"({reuse}) prefill={r['prompt_ms'] / 1000:6.2f}s gen={r['predicted_n']:>4} wall={r['wall_s']:6.1f}s tools={','.join(r['tools']) or '(answer)'}"
          + ("  COMPACTED" if r.get("compacted") else "")
          + (f"  OVERFLOW={r['coordinator_overflow']}" if r.get("coordinator_overflow") else ""), flush=True)


def cmd_agent(a):
    ws = make_workspace(a.source)
    sender = Sender(a.base_url, a.shape, a.pin_slot, a.seed, json.loads(a.kwargs) if a.kwargs else None)
    sess = AgentSession(sender, ws, compact_budget=a.compact_budget, compact_target=a.compact_target)
    print(f"[agent] shape={a.shape} pin_slot={a.pin_slot} kwargs={a.kwargs or '-'} compaction budget={a.compact_budget or 'off'} "
          f"target={a.compact_target or '(budget)'} workspace={ws}")
    error = None
    try:
        for t in [int(x) for x in a.tasks.split(",")]:
            print(f"[task {t}] {TASKS[t][:90]}")
            sess.run_task(TASKS[t], a.max_turns)
    except ProbeError as e:           # e.g. the prompt outgrew the slot: keep what was measured so far
        error = str(e)
        print(f"[agent] stopped: {error}")
    finally:
        shutil.rmtree(ws, ignore_errors=True)
    summarize_agent(sess.records)
    cold = None
    if a.cold_check and sess.last_body is not None and error is None:
        cold = sess.cold_read()
        print(f"[agent] the same last request with no cache: prompt {cold['prompt_total']} tokens, all read ({cold['prompt_n']}) in {cold['prompt_ms'] / 1000:.1f} s "
              f"= {cold['prompt_n'] / max(cold['prompt_ms'] / 1000, 1e-9):.0f} tok/s")
    write_out(a.out, {"kind": "agent", "args": args_dict(a), "records": sess.records, "error": error, "cold_read": cold})


def summarize_agent(records):
    later = [r for r in records if r["turn"] > 1]
    tot = sum(r["prompt_total"] for r in records)
    reread = sum(r["prompt_n"] for r in records)
    if not tot:
        print("[agent] no timings in the answers")
        return
    print(f"[agent] {len(records)} requests; prompt tokens sent {tot}, re-read {reread} ({100.0 * reread / tot:.1f}%)")
    t2 = sum(r["prompt_total"] for r in later)
    if t2:
        r2 = sum(r["prompt_n"] for r in later)
        print(f"[agent] from the 2nd request on: sent {t2}, re-read {r2} ({100.0 * r2 / t2:.1f}%)")


# ---------------------------------------------------------------------------------------------------------
# FIM (ghost text)
# ---------------------------------------------------------------------------------------------------------

def fim_context(text, offset, quantum=0):
    """Port of buildFimContext(): (prefix, suffix, midLine) around the cursor at `offset` in `text`.

    quantum=0 is what ships: the prefix starts exactly PREFIX_LINES above the cursor line, so every new line the
    developer types moves the start of the prompt by one line. quantum=N (the proposal) starts it at a multiple of
    N lines, so the front of the prompt stays put for N lines of typing, and over the character budget it drops
    whole N-line blocks from the top instead of slicing characters off one at a time."""
    before, after = text[:offset], text[offset:]
    lines = before.split("\n")
    cursor_line = len(lines) - 1
    start = max(0, cursor_line - PREFIX_LINES)
    if quantum:
        start = start // quantum * quantum
        while start < cursor_line and len("\n".join(lines[start:])) > PREFIX_CHARS:
            start = min(cursor_line, start + quantum)
    prefix = "\n".join(lines[start:])
    if len(prefix) > PREFIX_CHARS:
        prefix = prefix[len(prefix) - PREFIX_CHARS:]
    alines = after.split("\n")
    suffix = "\n".join(alines[:SUFFIX_LINES + 1])
    if len(suffix) > SUFFIX_CHARS:
        suffix = suffix[:SUFFIX_CHARS]
    return prefix, suffix, bool(alines[0].strip())


# Set by main() when requests are pinned to a slot of a live engine: a unique first line, so that a probe prompt can
# never share a prefix with a user's cached prompt (llama-server would otherwise be free to load a user's saved
# state into the probe's slot, taking it out of the shared host prompt cache).
ISOLATION_TAG = ""


def _tag(kind):
    if not ISOLATION_TAG:
        return ""
    return f"[{ISOLATION_TAG}]\n" if kind == "chat" else f"// {ISOLATION_TAG}\n"


def fim_prompt(prefix, suffix, layout):
    if layout.startswith("spm"):      # llama.cpp's --spm-infill order: suffix first, so typing at the cursor only changes the end
        return f"<|fim_suffix|>{_tag('fim')}{suffix}<|fim_prefix|>{prefix}<|fim_middle|>"
    return f"<|fim_prefix|>{_tag('fim')}{prefix}<|fim_suffix|>{suffix}<|fim_middle|>"


def fim_prefix_only(prefix):
    """The 'warm' request: just the text before the cursor, so the server's state ends exactly there."""
    return f"<|fim_prefix|>{_tag('fim')}{prefix}"


def fim_request(base, prompt, mid_line, pin_slot, seed=None, max_tokens=FIM_MAX_TOKENS, timeout=300):
    stop = FIM_STOP + (["\n"] if mid_line else [])
    body = {"prompt": prompt, "max_tokens": max_tokens, "temperature": 0.1, "stream": False, "stop": stop}
    if pin_slot is not None:
        body["id_slot"] = pin_slot
    if seed is not None:
        body["seed"] = seed
    t0 = time.monotonic()
    r = post_json(base, "/v1/completions", body, timeout)
    wall = time.monotonic() - t0
    rec = timing_fields(r.get("timings") or {}, r.get("usage"))
    rec["wall_s"] = round(wall, 3)
    rec["text"] = (r.get("choices") or [{}])[0].get("text", "")
    return rec


def typing_script(lines, first, count, chunk):
    """Yield (text, offset, kind) as a developer retypes lines[first:first+count]: `kind` is 'first' for the first
    request on a new line and 'type' after more characters on the same line."""
    head, tail = lines[:first], lines[first + count:]
    for j in range(count):
        target = lines[first + j]
        done = lines[first:first + j]
        n = len(target) - len(target.lstrip())      # the editor's auto-indent arrives with the new line
        kind = "first"
        while True:
            typed = target[:n]
            offset = len("\n".join(head + done + [typed]))
            yield "\n".join(head + done + [typed] + tail), offset, kind
            if n >= len(target):
                break
            n = min(len(target), n + chunk)
            kind = "type"


def cmd_fim(a):
    src = os.path.join(a.source, a.file)
    lines = open(src, encoding="utf-8").read().split("\n")
    first = a.line
    if first < PREFIX_LINES or first + a.lines + SUFFIX_LINES >= len(lines):
        raise SystemExit(f"--line must leave {PREFIX_LINES} lines above and {SUFFIX_LINES} below ({len(lines)} lines in file)")
    recs = []
    print(f"[fim] layout={a.layout} file={a.file} lines {first + 1}-{first + a.lines} chunk={a.chunk} pin_slot={a.pin_slot}")
    # one cold request first so the typed requests below start from "the user already typed here once"
    for i, (text, off, kind) in enumerate(typing_script(lines, first, a.lines, a.chunk)):
        prefix, suffix, mid = fim_context(text, off, QUANTUM if a.layout in ("spm-stable", "warm-stable") else 0)
        row = {"i": i, "kind": kind, "layout": a.layout, "prompt_chars": len(prefix) + len(suffix)}
        if a.layout.startswith("warm"):
            warm = fim_request(a.base_url, fim_prefix_only(prefix), mid, a.pin_slot, a.seed, max_tokens=a.warm_tokens)
            real = fim_request(a.base_url, fim_prompt(prefix, suffix, "psm"), mid, a.pin_slot, a.seed)
            row["warm"] = {k: warm[k] for k in ("prompt_total", "prompt_n", "cache_n", "wall_s")}
            row.update({k: real[k] for k in ("prompt_total", "prompt_n", "cache_n", "reuse_pct", "prompt_ms", "predicted_n", "text")})
            row["wall_s"] = round(warm["wall_s"] + real["wall_s"], 3)
            row["wall_real_only_s"] = real["wall_s"]
        else:
            real = fim_request(a.base_url, fim_prompt(prefix, suffix, "spm" if a.layout.startswith("spm") else "psm"), mid, a.pin_slot, a.seed)
            row.update(real)
        recs.append(row)
        w = f" warm(re-read {row['warm']['prompt_n']})" if "warm" in row else ""
        print(f"  {i:>3} {kind:5s} prompt={row['prompt_total']:>5} re-read={row['prompt_n']:>5} reused={row['cache_n']:>5} "
              f"wall={row['wall_s']:5.2f}s{w}  {row.get('text', '')[:40]!r}", flush=True)
        time.sleep(a.think)
    summary = summarize_fim(recs)
    print("[fim] " + json.dumps(summary))
    write_out(a.out, {"kind": "fim", "args": args_dict(a), "summary": summary, "records": recs})


def summarize_fim(recs):
    def stats(rows):
        if not rows:
            return None
        w = sorted(r["wall_s"] for r in rows)
        return {"n": len(rows), "median_wall_s": round(statistics.median(w), 3), "p90_wall_s": round(w[min(len(w) - 1, int(0.9 * len(w)))], 3),
                "median_prompt": int(statistics.median(r["prompt_total"] for r in rows)),
                "median_re_read": int(statistics.median(r["prompt_n"] for r in rows)),
                "mean_reuse_pct": round(statistics.mean(r["reuse_pct"] or 0 for r in rows), 1)}
    steady = recs[1:]
    return {"all_but_first": stats(steady), "same_line_keystrokes": stats([r for r in steady if r["kind"] == "type"]),
            "first_on_new_line": stats([r for r in steady if r["kind"] == "first"])}


def cmd_fimq(a):
    """Quality of the prompt layouts on the bake-off's FIM suite (its own judge runs the spliced code)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("fim_check", a.suite_file)
    fc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fc)
    results = {}
    for layout in a.layouts.split(","):
        ok = total = overrun = 0
        rows = []
        for rep in range(a.repeat):
            for cid, kind, text, check in fc.CASES:
                before, after, prefix, suffix, mid = fc.build_context(text)
                if layout.startswith("warm"):     # the shipping prompt, preceded by a prefix-only request that parks the state at the cursor
                    fim_request(a.base_url, fim_prefix_only(prefix), mid, a.pin_slot, seed=1000 + rep, max_tokens=0)
                r = fim_request(a.base_url, fim_prompt(prefix, suffix, "psm" if layout.startswith("warm") else layout), mid, a.pin_slot, seed=1000 + rep)
                comp = r["text"]
                if not comp.strip():
                    passed, detail = False, "empty"
                else:
                    passed, detail = fc.judge(kind, before + comp + after, check)
                    if passed is False and "\n" in comp.strip("\n"):
                        ls = comp.split("\n")
                        for keep in range(len(ls) - 1, 0, -1):
                            if fc.judge(kind, before + "\n".join(ls[:keep]) + after, check)[0]:
                                overrun += 1
                                break
                if passed is None:
                    continue
                total += 1
                ok += 1 if passed else 0
                rows.append({"case": cid, "rep": rep, "passed": bool(passed), "detail": detail, "text": comp[:120], "wall_s": r["wall_s"], "prompt_n": r["prompt_n"]})
                print(f"  {layout:4s} {cid:26s} {'PASS' if passed else 'FAIL'} {r['wall_s']:5.2f}s {comp.strip()[:50]!r} {'' if passed else detail}", flush=True)
        results[layout] = {"passed": ok, "total": total, "rate": round(ok / total, 3) if total else None, "overrun_but_right": overrun, "rows": rows}
        print(f"[fimq] {layout}: {ok}/{total}")
    write_out(a.out, {"kind": "fimq", "args": args_dict(a), "results": results})


# ---------------------------------------------------------------------------------------------------------
# Mixed load: two agent sessions and a FIM typist on one server (laptop only: no pinning)
# ---------------------------------------------------------------------------------------------------------

def cmd_mixed(a):
    ws_a, ws_b = make_workspace(a.source), make_workspace(a.source)
    sender = Sender(a.base_url, a.shape, None, a.seed, json.loads(a.kwargs) if a.kwargs else None)
    A, B = AgentSession(sender, ws_a, name="A"), AgentSession(sender, ws_b, name="B")
    lines = open(os.path.join(a.source, a.file), encoding="utf-8").read().split("\n")
    typing = typing_script(lines, a.line, a.lines, a.chunk)
    fim_recs = []

    def burst(n):
        for _ in range(n):
            try:
                text, off, kind = next(typing)
            except StopIteration:
                return
            prefix, suffix, mid = fim_context(text, off, QUANTUM if a.layout.endswith("stable") else 0)
            r = fim_request(a.base_url, fim_prompt(prefix, suffix, a.layout), mid, None, a.seed)
            r.update(kind=kind, session="fim")
            fim_recs.append(r)
            print(f"  fim {kind:5s} prompt={r['prompt_total']:>5} re-read={r['prompt_n']:>5} reused={r['cache_n']:>5} wall={r['wall_s']:.2f}s", flush=True)

    A.user(TASKS[a.task_a])
    B.user(TASKS[a.task_b])
    done = {"A": False, "B": False}
    errors = {}
    order = [A, B, "fim", A, B, "fim", A, B, "fim", A, B]
    for who in order:
        if who == "fim":
            burst(a.burst)
        elif not done[who.name]:
            try:
                rec, fin = who.step()
            except ProbeError as e:      # e.g. a small slot overflowed: stop that session, keep the rest of the run
                errors[who.name] = str(e)
                done[who.name] = True
                print(f"  {who.name}: stopped: {e}")
                continue
            done[who.name] = fin
            print_agent_row(rec)
    shutil.rmtree(ws_a, ignore_errors=True)
    shutil.rmtree(ws_b, ignore_errors=True)
    write_out(a.out, {"kind": "mixed", "args": args_dict(a), "agent": A.records + B.records, "fim": fim_recs, "errors": errors})


# ---------------------------------------------------------------------------------------------------------
# llama-server -lv 4 log: what the server decided for each task
# ---------------------------------------------------------------------------------------------------------

_LINE = re.compile(r"slot\s+(?P<fn>\S+)\s*: id\s+(?P<slot>\d+) \| task (?P<task>-?\d+) \| (?P<msg>.*)")


def parse_server_log(lines):
    """-> list of per-task dicts, in order. Works on the -lv 4 output of llama.cpp b11838."""
    tasks, pending, cur = [], {}, {}
    for line in lines:
        m = _LINE.search(line)
        if not m:
            continue
        slot, task, msg = int(m["slot"]), int(m["task"]), m["msg"]
        if task == -1 and msg.startswith("selected slot"):
            pending[slot] = msg.split(",")[0].replace("selected slot ", "")
            if "f_sim_best" in msg:
                pending[slot] += " " + re.search(r"f_sim_best = ([\d.]+)", msg).group(0)
            continue
        if msg.startswith("processing task"):
            cur[task] = {"task": task, "slot": slot, "selected": pending.pop(slot, "?"), "checkpoints_created": [], "restored": None,
                         "forced_full_reread": False, "erased": 0}
            tasks.append(cur[task])
            continue
        t = cur.get(task)
        if t is None:
            continue
        if msg.startswith("new prompt"):
            t["prompt_tokens"] = int(re.search(r"task\.n_tokens = (\d+)", msg)[1])
        elif msg.startswith("cached n_tokens") and "n_past" not in t:
            t["n_past"] = int(re.search(r"cached n_tokens = (\d+)", msg)[1])
        elif msg.startswith("restored context checkpoint"):
            t["restored"] = int(re.search(r"n_tokens = (\d+)", msg)[1])
        elif msg.startswith("forcing full prompt re-processing"):
            t["forced_full_reread"] = True
        elif msg.startswith("erased invalidated context checkpoint"):
            t["erased"] += 1
        elif msg.startswith("created context checkpoint"):
            t["checkpoints_created"].append((int(re.search(r"n_tokens = (\d+)", msg)[1]), float(re.search(r"size = ([\d.]+) MiB", msg)[1])))
            t["checkpoints_held"] = int(re.search(r"checkpoint (\d+) of", msg)[1])      # how many this slot holds after this one
    return tasks


def cmd_log(a):
    rows = parse_server_log(open(a.log, encoding="utf-8", errors="replace"))
    print(f"{'task':>5} {'slot':>4} {'prompt':>6} {'n_past':>6} {'restored':>8} {'full-reread':>11} {'held':>4} selected / checkpoints created (n_tokens, MiB)")
    for t in rows:
        print(f"{t['task']:>5} {t['slot']:>4} {t.get('prompt_tokens', '-'):>6} {t.get('n_past', '-'):>6} {str(t['restored'] or '-'):>8} "
              f"{'YES' if t['forced_full_reread'] else '-':>11} {t.get('checkpoints_held', '-'):>4} {t['selected']} / {t['checkpoints_created']}")


# ---------------------------------------------------------------------------------------------------------

def args_dict(a):
    d = {k: v for k, v in vars(a).items() if k != "fn"}
    d["isolation_tag"] = ISOLATION_TAG
    return d


def write_out(path, obj):
    if path:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=1)
        print(f"[wrote {path}]")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, fim=False):
        sp.add_argument("--base-url", default=os.environ.get("WALL_ENGINE_URL", "http://127.0.0.1:8080"))
        sp.add_argument("--pin-slot", type=int, default=None, help='send "id_slot": N with every request')
        sp.add_argument("--seed", type=int, default=1234)
        sp.add_argument("--source", default=os.path.join(REPO, "vscode-extension"), help="folder the sample files are copied/read from")
        sp.add_argument("--out", default=None)
        sp.add_argument("--no-isolate", action="store_true",
                        help="with --pin-slot the prompts start with a unique tag line so they never match a user's cached prompt; this turns that off")
        sp.add_argument("--isolate", action="store_true", help="the same unique first line without pinning (runs that must not share a cached prefix)")
        if fim:
            sp.add_argument("--file", default="src/client/dlneClient.ts")
            sp.add_argument("--line", type=int, default=450, help="0-based first line to retype")
            sp.add_argument("--lines", type=int, default=3)
            sp.add_argument("--chunk", type=int, default=3, help="characters typed between requests")
            sp.add_argument("--think", type=float, default=0.0, help="seconds between requests")

    sp = sub.add_parser("agent")
    common(sp)
    sp.add_argument("--shape", choices=["direct", "coordinator"], default="direct")
    sp.add_argument("--tasks", default="1,2")
    sp.add_argument("--max-turns", type=int, default=8)
    sp.add_argument("--cold-check", action="store_true", help="at the end, resend the last request with cache_prompt false to time a full read of the history")
    sp.add_argument("--compact-budget", type=int, default=COMPACT_BUDGET, help="estimated tokens the history may reach before compaction (0 = never); the extension ships 11000")
    sp.add_argument("--compact-target", type=int, default=None, help="shrink to this when a compaction fires (the proposed hysteresis); default = the budget, as shipped")
    sp.add_argument("--kwargs", default=None, help='chat_template_kwargs JSON for the request body, e.g. {"preserve_thinking":true}')
    sp.set_defaults(fn=cmd_agent)

    sp = sub.add_parser("fim")
    common(sp, fim=True)
    sp.add_argument("--layout", choices=["psm", "spm", "spm-stable", "warm", "warm-stable"], default="psm",
                    help="psm = shipping; spm = suffix first; *-stable = prefix window starts at a multiple of 8 lines; warm = prefix-only request before each real one")
    sp.add_argument("--warm-tokens", type=int, default=0)
    sp.set_defaults(fn=cmd_fim)

    sp = sub.add_parser("fimq")
    common(sp)
    sp.add_argument("--suite-file", required=True, help="dlne-coding-bakeoff/eval/fim_check.py")
    sp.add_argument("--layouts", default="psm,spm")
    sp.add_argument("--repeat", type=int, default=3)
    sp.set_defaults(fn=cmd_fimq)

    sp = sub.add_parser("mixed")
    common(sp, fim=True)
    sp.add_argument("--shape", choices=["direct", "coordinator"], default="direct")
    sp.add_argument("--layout", choices=["psm", "spm", "spm-stable"], default="psm")
    sp.add_argument("--burst", type=int, default=6, help="FIM requests between agent steps")
    sp.add_argument("--task-a", type=int, default=1)
    sp.add_argument("--task-b", type=int, default=2)
    sp.add_argument("--kwargs", default=None)
    sp.set_defaults(fn=cmd_mixed)

    sp = sub.add_parser("log")
    sp.add_argument("log")
    sp.set_defaults(fn=cmd_log)

    a = p.parse_args(argv)
    global ISOLATION_TAG
    if getattr(a, "isolate", False) or (getattr(a, "pin_slot", None) is not None and not a.no_isolate):
        ISOLATION_TAG = f"probe-{int(time.time())}-{os.getpid()}"
    try:
        a.fn(a)
    except ProbeError as e:
        print(f"cache_probe: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
