"""
No file path in an answer (engine/model_alias.py).

llama-server reports the path it loaded the model from in every answer: ``"model": "/home/<user>/.../X.gguf"``.
Those answers reach other people (a phone through the pool, a developer through a coding member), so every
launch of llama-server names the model with ``--alias <the file's name>`` instead. The launches themselves are
checked next to each launcher's other tests (test_inference_manager, test_app_pool, test_app_member,
test_app_light, test_coding_member); here are the name, every argument builder, and one end-to-end run with a
stand-in llama-server that names its model the way the real one does.
"""

import asyncio
import hashlib
import json
import os
import stat
import subprocess
import sys

import httpx
import pytest

from engine.app_member import app_member_args
from engine.app_pool import LocalAppServer, app_server_args
from engine.coding_member import CodingMember, coding_server_args
from engine.model_alias import DEFAULT_NAME, alias_args, alias_of, alias_problem, model_alias as name_of
from tests import app_pool_fakes
from tests.coding_member_fakes import (FakeDownloader, Opener, arg_after, data_lines, free_port, make_tier,
                                       recorded_starts, seal_request, wait_for)

QWEN = "Qwen3.6-35B-A3B-UD-IQ3_S.gguf"
DEEP_PATHS = (
    f"/home/wubarrk/models/{QWEN}",
    f"/home/alice/.local/share/dlne/models/{QWEN}",
    f"C:\\Users\\Алиса\\AppData\\Local\\dlne\\models\\{QWEN}",
    f"C:/Users/bob/dlne/models/{QWEN}",
    QWEN,
)


# ======================================================================================
# The name
# ======================================================================================

@pytest.mark.parametrize("path", DEEP_PATHS)
def test_the_alias_is_the_files_own_name_whatever_the_path(path):
    assert name_of(path) == QWEN
    assert alias_args(path) == ["--alias", QWEN]


@pytest.mark.parametrize("path, expected", [
    ("/m/a,b.gguf", "a_b.gguf"),           # --alias takes a comma-separated list; a name is one name
    ("/m/a\nb.gguf", "ab.gguf"),
    ("/m/ spaced name.gguf ", "spaced name.gguf"),
    ("/m/model-00001-of-00003.gguf", "model-00001-of-00003.gguf"),
])
def test_the_alias_is_one_plain_name(path, expected):
    assert name_of(path) == expected


@pytest.mark.parametrize("path", ["", None, "/", "/m/", "C:\\m\\", ".", "..", "/m/..", "   "])
def test_a_path_with_no_file_name_gets_a_neutral_name_never_a_directory(path):
    assert name_of(path) == DEFAULT_NAME


def test_the_check_reads_an_argument_list():
    assert alias_of(["b", "-m", "/m/x.gguf", "--alias", "x.gguf"]) == "x.gguf"
    assert alias_problem(["b", "--alias", "x.gguf"]) == ""
    assert alias_of(["b", "-m", "/m/x.gguf"]) is None and "no --alias" in alias_problem(["b", "-m", "/m/x.gguf"])
    assert "no --alias" in alias_problem(["b", "--alias"]), "an alias flag with no name"
    for bad in ("/home/alice/x.gguf", "C:\\Users\\bob\\x.gguf", "a,b"):
        assert alias_problem(["b", "--alias", bad]), bad


# ======================================================================================
# Every argument builder
# ======================================================================================

def builders(path):
    """Every way DLNE builds the arguments of a llama-server, for a model at ``path``."""
    return {
        "phone pool member (the coordinator's own and every volunteer's)": app_server_args("llama-server", path, 8081, 4),
        "phone pool member, pinned to cores": app_server_args("llama-server", path, 8081, 4, cpu_mask="0xf"),
        "volunteer phone member": app_member_args("llama-server", path, 8081, 4),
        "volunteer phone member, light mode": app_member_args("llama-server", path, 8081, 4, light=True),
        "coding member": coding_server_args("llama-server", path, 8081),
        "coding member with the owner's limits": coding_server_args("llama-server", path, 8081, threads=6,
                                                                    extra=["--fit-target", "2048"]),
    }


@pytest.mark.parametrize("path", DEEP_PATHS)
def test_every_argument_builder_names_the_model_by_its_file(path):
    for who, args in builders(path).items():
        assert alias_of(args) == QWEN, who
        assert alias_problem(args) == "", who
        assert args[args.index("-m") + 1] == path, f"{who}: the file loaded is still the one asked for"
        assert path not in args[args.index("--alias"):args.index("--alias") + 2] or path == QWEN, who


# ======================================================================================
# End to end: a llama-server that names its model the way the real one does
# ======================================================================================

_FAKE_LLAMA = r'''#!__PYTHON__
import json, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

argv = sys.argv[1:]
with open(__RECORD__, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(argv) + "\n")


def after(flag):
    return argv[argv.index(flag) + 1] if flag in argv else None


PORT = int(after("--port"))
# What llama-server calls its model in an answer: the --alias, and with none the path it loaded.
NAME = after("--alias") or after("-m")
TIMINGS = {"prompt_n": 2000, "predicted_n": 128, "prompt_per_second": 222.2, "predicted_per_second": 33.3}


class Handler(BaseHTTPRequestHandler):
    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"status": "ok"})
        elif self.path == "/v1/models":
            self._json(200, {"object": "list", "data": [{"id": NAME, "object": "model"}]})
        else:
            self._json(404, {})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        body = json.loads(raw or b"{}")
        if not body.get("stream"):
            return self._json(200, {"id": "c1", "object": "chat.completion", "model": NAME,
                                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "alpha beta"},
                                                 "finish_reason": "stop"}], "timings": TIMINGS})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        events = [{"model": NAME, "choices": [{"index": 0, "delta": {"content": piece}}]} for piece in ("alpha ", "beta")]
        events.append({"model": NAME, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "timings": TIMINGS})
        for event in events:
            self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")

    def log_message(self, *args):
        pass


ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
'''


def write_llama_that_names_its_model(path, record):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    source = _FAKE_LLAMA.replace("__PYTHON__", sys.executable).replace("__RECORD__", repr(record))
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(source)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def ask(port, stream=False):
    """A chat request straight to a llama-server on loopback: the raw bytes of what it answered."""
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=15.0) as client:
        response = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}],
                                                              "stream": stream})
    assert response.status_code == 200
    return response.content


DATA = b"GGUF a model that lives in a home directory " * 64


def test_the_stand_in_names_its_model_by_path_when_it_is_given_no_alias(tmp_path):
    """The control: without --alias the answer carries the path, as the real llama-server's does."""
    record = str(tmp_path / "starts.jsonl")
    binary = write_llama_that_names_its_model(str(tmp_path / "bin" / "llama-server"), record)
    port = free_port()
    process = subprocess.Popen([binary, "-m", f"/home/alice/models/{QWEN}", "--port", str(port)])
    try:
        assert wait_for(lambda: _answers(port))
        assert json.loads(ask(port))["model"] == f"/home/alice/models/{QWEN}"
        assert b"/home/alice" in ask(port, stream=True)
    finally:
        process.kill()
        process.wait(timeout=5)


def _answers(port):
    try:
        return httpx.get(f"http://127.0.0.1:{port}/health", trust_env=False, timeout=1.0).status_code == 200
    except httpx.HTTPError:
        return False


def test_a_coding_members_sealed_answer_names_the_file_and_no_directory(tmp_path):
    """A member in a home directory serves a request; the requester decrypts the answer: no /home/ in it."""
    base = tmp_path / "home" / "alice" / "dlne"
    record = str(tmp_path / "starts.jsonl")
    binary = write_llama_that_names_its_model(str(base / "bin" / "llama-server"), record)
    tier = make_tier("coder-a", [DATA])
    member = CodingMember(str(base), lambda: [tier], port=free_port(),
                          download=FakeDownloader({tier["file"]: DATA}), measure=lambda url, client: (31.0, 118.0),
                          poll_seconds=0.05, ready_timeout=20.0)
    assert member.start()
    try:
        member.assign(tier)
        assert wait_for(lambda: not member._busy, 30.0) and member.status()["ready"] is True
        argv = recorded_starts(record)[0]
        loaded = arg_after(argv, "-m")
        assert loaded.startswith(str(base)) and "/home/alice/" in loaded, "the model really is under a home directory"
        assert arg_after(argv, "--alias") == tier["file"]
        with httpx.Client(base_url=f"http://127.0.0.1:{member.port}", trust_env=False, timeout=15.0) as client:
            envelope, key = seal_request({"messages": [{"role": "user", "content": "hi"}], "stream": False},
                                         member.pubkey)
            whole = client.post("/v1/chat/completions", json=envelope)
            plain = Opener(key).open(whole.json()["dlne_e2e"])
            assert json.loads(plain)["model"] == tier["file"]
            assert b"/home/" not in plain and str(base).encode() not in plain

            envelope, key = seal_request({"messages": [{"role": "user", "content": "hi"}], "stream": True}, member.pubkey)
            with client.stream("POST", "/v1/chat/completions", json=envelope) as response:
                raw = b"".join(response.iter_raw())
            opener = Opener(key)
            chunks = [opener.open_event(line) for line in data_lines(raw)[:-1]]
            texts = [chunk for chunk in chunks if chunk is not None]
            assert len(texts) == 3 and opener.finished
            assert {json.loads(chunk)["model"] for chunk in texts} == {tier["file"]}
            assert all(b"/home/" not in chunk for chunk in texts)
            assert member.status()["model"] == tier["file"], "what the heartbeat says is unchanged"
    finally:
        member.stop()


def test_the_coordinator_hosts_phone_member_answers_with_the_files_name_and_no_directory(tmp_path):
    models = tmp_path / "home" / "bob" / "models"
    models.mkdir(parents=True)
    (models / "t0.gguf").write_bytes(DATA)
    record = str(tmp_path / "starts.jsonl")
    binary = write_llama_that_names_its_model(str(tmp_path / "bin" / "llama-server"), record)
    tier = {"id": "t0", "name": "Tier t0", "file": "t0.gguf", "sha256": hashlib.sha256(DATA).hexdigest(),
            "size": len(DATA), "min_ram_gb": 8}
    server = LocalAppServer(str(models), lambda: binary, str(tmp_path / "logs"), free_port(), 3, 8.0, None)
    try:
        server.ensure(tier)
        assert wait_for(lambda: app_pool_fakes.recorded_starts(record))

        async def ready():
            async with httpx.AsyncClient() as client:
                return await server.wait_ready(client, timeout=20.0)

        assert asyncio.run(ready()) is True
        assert any("/home/bob/models/" in arg for arg in app_pool_fakes.recorded_starts(record)[0]), \
            "the model really is loaded from a home directory"
        for stream in (False, True):
            answer = ask(server.port, stream=stream)
            assert b"/home/" not in answer and b"t0.gguf" in answer
    finally:
        server.stop()


