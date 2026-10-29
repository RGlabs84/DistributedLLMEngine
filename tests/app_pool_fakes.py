"""
Stand-ins for the CPU pool's llama-server members, shared by the tests/test_app_*.py files.

Everything here talks over real loopback sockets, so httpx's real failure modes (a refused
connection, a peer that closes before answering, a chunked body cut short) are what the code
under test sees, not a mock's idea of them.

* :class:`FakeLlamaServer` - a FastAPI app served by uvicorn in a thread (the same pattern
  as tests/test_reverse_tunnel.py) answering ``POST /v1/chat/completions`` with SSE chunks
  (``script`` gives each next request its own reply) and ``POST /tokenize`` (``chars_per_token``).
  Its ``mode`` picks the behaviour: ``ok``, ``500``, ``400``, ``die_before_first_chunk``,
  ``die_mid_stream`` and ``hold`` (pause after the first chunk until ``gate`` is set).
* :class:`ClosingServer` - accepts a connection, reads the request and closes without a
  single byte of response.
* :class:`HangingServer` - accepts and never answers.
* :func:`write_fake_llama_binary` - a tiny executable Python script standing in for the
  ``llama-server`` binary itself (records its argv, serves /health).
"""

import asyncio
import hashlib
import io
import json
import os
import socket
import stat
import sys
import threading
import time
import urllib.request
from typing import Dict, List, Optional

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(predicate, timeout: float = 10.0, interval: float = 0.02) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def sse(text: str) -> bytes:
    """One OpenAI-style streaming chunk, as llama-server sends it."""
    chunk = {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}
    return f"data: {json.dumps(chunk)}\n\n".encode()


DONE = b"data: [DONE]\n\n"


def sse_events(raw: bytes) -> List[str]:
    """The ``data:`` payloads of an SSE byte stream, in order (blank-line separated events)."""
    events = []
    for block in raw.decode("utf-8", errors="replace").split("\n\n"):
        block = block.strip("\n")
        if block.startswith("data: "):
            events.append(block[len("data: "):])
        elif block:
            events.append(block)
    return events


def notice_texts(raw: bytes) -> List[str]:
    """The content of every parseable SSE event, '' for [DONE]; raises on an unparseable one."""
    out = []
    for payload in sse_events(raw):
        if payload == "[DONE]":
            out.append("")
            continue
        chunk = json.loads(payload)
        out.append(chunk["choices"][0]["delta"].get("content", ""))
    return out


class FakeLlamaServer:
    """A member's llama-server: streams SSE on /v1/chat/completions and answers /health."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.port = free_port()
        self.payloads: List[dict] = []
        self.gate = threading.Event()
        self.entered = threading.Event()
        self.reset()

        app = FastAPI()

        @app.post("/v1/chat/completions")
        async def chat(request: Request):
            payload = await request.json()
            self.payloads.append(payload)
            self.entered.set()
            mode = self.mode
            chunks = self.script.pop(0) if self.script else list(self.chunks)
            if mode == "500":
                return JSONResponse({"error": {"message": "the model crashed"}}, status_code=500)
            if mode == "400":
                return JSONResponse({"error": {"message": "bad request"}}, status_code=400)

            async def body():
                if mode == "die_before_first_chunk":
                    raise RuntimeError("died before answering")
                for i, chunk in enumerate(chunks):
                    if mode == "die_mid_stream" and i == self.die_after:
                        raise RuntimeError("died mid-answer")
                    if mode == "hold" and i == 1:
                        await asyncio.to_thread(self.gate.wait, 20)
                    yield chunk
                    await asyncio.sleep(0.005)

            return StreamingResponse(body(), media_type="text/event-stream")

        @app.post("/tokenize")
        async def tokenize(request: Request):
            body = await request.json()
            self.tokenized.append(body)
            if self.chars_per_token is None:
                return JSONResponse({"error": "no tokenizer"}, status_code=404)
            return {"tokens": [0] * -(-len(body["content"]) // self.chars_per_token)}

        @app.get("/health")
        async def health():
            return {"status": "ok"}

        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port,
                                                    log_level="critical", lifespan="off"))
        self.thread = threading.Thread(target=self.server.run, daemon=True, name=f"fake-llama-{name}")

    def reset(self) -> None:
        self.mode = "ok"
        self.die_after = 1
        self.chunks = [sse(f"{self.name}-one "), sse(f"{self.name}-two"), DONE]
        self.script: List[List[bytes]] = []     # replies for the next requests, one list of chunks each
        self.chars_per_token: Optional[int] = None   # what /tokenize counts by; None: it answers 404
        self.tokenized: List[dict] = []
        self.payloads.clear()
        self.gate.set()
        self.entered.clear()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def body(self) -> bytes:
        return b"".join(self.chunks)

    def start(self) -> "FakeLlamaServer":
        self.thread.start()
        assert wait_for(lambda: self.server.started), f"fake llama-server {self.name} did not start"
        return self

    def stop(self) -> None:
        self.gate.set()
        self.server.should_exit = True
        self.thread.join(timeout=5)


class _RawServer:
    """A listening socket whose per-connection behaviour subclasses define."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.connections = 0
        self.held: List[socket.socket] = []
        self._stop = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _serve(self) -> None:
        self.sock.settimeout(0.1)
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            self.connections += 1
            self.handle(conn)

    def handle(self, conn: socket.socket) -> None:  # pragma: no cover - overridden
        conn.close()

    @staticmethod
    def read_request(conn: socket.socket) -> bytes:
        conn.settimeout(2)
        data = b""
        try:
            while b"\r\n\r\n" not in data:
                part = conn.recv(65536)
                if not part:
                    return data
                data += part
            head, _, rest = data.partition(b"\r\n\r\n")
            length = 0
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":", 1)[1])
            while len(rest) < length:
                part = conn.recv(65536)
                if not part:
                    break
                rest += part
        except OSError:
            pass
        return data

    def stop(self) -> None:
        self._stop.set()
        for conn in self.held:
            try:
                conn.close()
            except OSError:
                pass
        self.sock.close()


class ClosingServer(_RawServer):
    """Reads the request and closes the connection without answering a single byte."""

    def handle(self, conn: socket.socket) -> None:
        self.read_request(conn)
        conn.close()


class HangingServer(_RawServer):
    """Accepts, reads the request, and never answers (a wedged llama-server)."""

    def handle(self, conn: socket.socket) -> None:
        self.read_request(conn)
        self.held.append(conn)


# ======================================================================================
# The llama-server binary and the model file
# ======================================================================================

_FAKE_BINARY = r'''#!__PYTHON__
import json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

RECORD = __RECORD__
READY_FLAG = __READY_FLAG__
with open(RECORD, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\n")
if __CRASH__:
    sys.exit(3)
port = int(sys.argv[sys.argv.index("--port") + 1])


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            ok = READY_FLAG is None or os.path.exists(READY_FLAG)
            body = b'{"status":"ok"}' if ok else b'{"error":"loading model"}'
            self.send_response(200 if ok else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


HTTPServer(("127.0.0.1", port), Handler).serve_forever()
'''


def write_fake_llama_binary(path: str, record: str, crash: bool = False,
                            ready_flag: Optional[str] = None) -> str:
    """Write an executable stand-in for llama-server at ``path``; each start appends its argv to ``record``."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    source = (_FAKE_BINARY.replace("__PYTHON__", sys.executable)
              .replace("__RECORD__", repr(record))
              .replace("__READY_FLAG__", repr(ready_flag))
              .replace("__CRASH__", repr(crash)))
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(source)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def recorded_starts(record: str) -> List[List[str]]:
    try:
        with open(record, "r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]
    except OSError:
        return []


def make_tier(tier_id: str, data: bytes, min_ram_gb: int = 8, name: Optional[str] = None) -> Dict:
    """A CPU pool tier for ``data`` (the model file's bytes), shaped like release.app_tiers()."""
    file = f"{tier_id}-Q4_K_M.gguf"
    return {
        "id": tier_id,
        "name": name or f"Tiny {tier_id}",
        "file": file,
        "url": f"https://huggingface.co/acme/{tier_id}-GGUF/resolve/{'a' * 40}/{file}",
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "min_ram_gb": min_ram_gb,
    }


# Loopback requests (the member's /health) must never go through an environment proxy.
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class FakeHub:
    """
    Stands in for ``urllib.request.urlopen`` in AppMember: serves model bytes for the signed
    Hugging Face URLs and passes loopback requests (the /health probe) through for real.
    """

    def __init__(self, files: Optional[Dict[str, bytes]] = None) -> None:
        self.files: Dict[str, bytes] = dict(files or {})
        self.calls: List[str] = []

    def __call__(self, url, timeout=None):
        if isinstance(url, str) and url.startswith("http://127.0.0.1"):
            return _LOCAL_OPENER.open(url, timeout=timeout)
        self.calls.append(url)
        if url not in self.files:
            raise OSError(f"no such file on the fake hub: {url}")
        return io.BytesIO(self.files[url])
