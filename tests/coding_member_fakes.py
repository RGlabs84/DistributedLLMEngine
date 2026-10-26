"""
Stand-ins for the coding member's surroundings, shared by tests/test_coding_member*.py.

Everything talks over real loopback sockets (like tests/app_pool_fakes.py), so httpx's real
failure modes are what the code under test sees.

* :class:`FakeUpstream` - llama-server's HTTP side as a FastAPI app in a thread: streams SSE or
  answers JSON on the two completion routes, records what it was sent, and can fail, hold or die.
* :func:`write_fake_coding_binary` - an executable Python script standing in for the
  ``llama-server`` binary itself: records its argv, serves /health and the two routes.
* :class:`FakeDownloader` - stands in for ``engine.downloader.download_verified``.
* ``seal_request`` / ``Opener`` - the editor's side of docs/coding-members.md section 6 (with its
  "Version 1 additions"), written from the contract and not from engine/e2e.py, so they check
  whatever module is there.
"""

import asyncio
import base64
import hashlib
import json
import os
import socket
import stat
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import uvicorn
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
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
    chunk = {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}
    return f"data: {json.dumps(chunk)}\n\n".encode()


DONE = b"data: [DONE]\n\n"


def data_lines(raw: bytes) -> List[str]:
    """The ``data:`` payloads of an SSE byte stream, in order."""
    out = []
    for block in raw.decode("utf-8").split("\n\n"):
        if block.startswith("data: "):
            out.append(block[len("data: "):])
    return out


# ======================================================================================
# The editor's side of the end-to-end encryption (contract section 6)
# ======================================================================================

def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


PAD = 256


def seal_request(body: Dict[str, Any], member_pub_b64: str, pad: bool = True) -> Tuple[Dict[str, Any], bytes]:
    """``(the envelope to POST, the key the answer is sealed with)``. Padded with trailing spaces to a
    multiple of 256 bytes, as section 6 asks."""
    member_pub = base64.b64decode(member_pub_b64)
    eph = X25519PrivateKey.generate()
    eph_pub = eph.public_key().public_bytes_raw()
    shared = eph.exchange(X25519PublicKey.from_public_bytes(member_pub))
    okm = HKDF(algorithm=hashes.SHA256(), length=64, salt=eph_pub + member_pub, info=b"dlne-e2e-v1").derive(shared)
    nonce = os.urandom(12)
    plain = json.dumps(body).encode()
    if pad:
        plain += b" " * (-len(plain) % PAD)
    ct = ChaCha20Poly1305(okm[:32]).encrypt(nonce, plain, b"dlne-req")
    return {"dlne_e2e": {"v": 1, "epk": _b64(eph_pub), "nonce": _b64(nonce), "ct": _b64(ct)}}, okm[32:]


class Opener:
    """The editor's side of a response (section 6, "Version 1 additions"): the first sealed object
    carries ``rs``, every chunk is sealed with the key derived from it, the counter runs 0, 1, 2, ...
    in order, every plaintext is padded to a multiple of 256 bytes, and a stream is complete only after
    the sealed end marker ``[DONE]``."""

    def __init__(self, resp_key: bytes) -> None:
        self.resp_key = resp_key
        self.key: Optional[bytes] = None
        self.next = 0
        self.finished = False

    def open(self, sealed: Dict[str, Any]) -> Optional[bytes]:
        """The chunk's bytes (trailing padding removed), or None for the sealed end."""
        assert not self.finished, "nothing may follow the sealed end"
        assert sealed["n"] == self.next, "chunks arrive in order, none missing"
        if self.next == 0:
            salt = base64.b64decode(sealed["rs"], validate=True)
            assert len(salt) == 16
            self.key = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=b"dlne-e2e-v1-resp").derive(self.resp_key)
        plain = ChaCha20Poly1305(self.key).decrypt(sealed["n"].to_bytes(12, "big"), base64.b64decode(sealed["ct"]), b"dlne-resp")
        assert len(plain) % PAD == 0, "padded to a multiple of 256 bytes"
        self.next += 1
        plain = plain.rstrip(b" ")
        if plain == b"[DONE]":
            self.finished = True
            return None
        return plain

    def open_event(self, line: str) -> Optional[bytes]:
        return self.open(json.loads(line)["dlne_e2e"])


# ======================================================================================
# llama-server's HTTP side
# ======================================================================================

class FakeUpstream:
    """
    ``mode``: ``ok`` (SSE chunks or one JSON answer), ``500`` (an error whose text echoes the prompt,
    as a real server's can), ``hold`` (answer after the gate opens; streams send their first chunk
    at once), ``die`` (the stream breaks after its first chunk).
    """

    def __init__(self, name: str = "up") -> None:
        self.name = name
        self.port = free_port()
        self.received: List[Dict[str, Any]] = []        # {"path", "body" (bytes), "headers"}
        self.other_hits: List[str] = []                  # routes the proxy must never forward to
        self.gate = threading.Event()
        self.entered = threading.Event()
        self.disconnected = threading.Event()
        self.reset()
        app = FastAPI()

        async def answer(request: Request, path: str):
            raw = await request.body()
            self.received.append({"path": path, "body": raw, "headers": dict(request.headers)})
            self.entered.set()
            body = json.loads(raw)
            if self.fail_warmups and body.get("max_tokens") == 0:
                return JSONResponse({"error": {"message": "cannot warm up"}}, status_code=500)
            if self.mode == "500":
                return JSONResponse({"error": {"message": f"failed on: {body.get('echo', '')}"}}, status_code=500)
            streaming = bool(body.get("stream"))
            if not streaming:
                if self.mode == "hold":
                    while not self.gate.is_set():
                        if await request.is_disconnected():
                            self.disconnected.set()
                            return JSONResponse({}, status_code=499)
                        await asyncio.sleep(0.02)
                return JSONResponse({"id": f"{self.name}-1", "model": "fake",
                                     "choices": [{"index": 0, "finish_reason": "stop",
                                                  "message": {"role": "assistant", "content": self.answer_text}}],
                                     "usage": {"prompt_tokens": 3, "completion_tokens": 2}})

            async def events():
                try:
                    for i, chunk in enumerate(self.chunks):
                        if self.mode == "die" and i == 1:
                            raise RuntimeError("the model died")
                        if self.mode == "hold" and i == 1:
                            while not self.gate.is_set():
                                if await request.is_disconnected():
                                    self.disconnected.set()
                                    return
                                await asyncio.sleep(0.02)
                        yield chunk
                        await asyncio.sleep(0.005)
                finally:
                    self.stream_closed.set()

            return StreamingResponse(events(), media_type="text/event-stream")

        @app.post("/v1/chat/completions")
        async def chat(request: Request):
            return await answer(request, "/v1/chat/completions")

        @app.post("/v1/completions")
        async def completions(request: Request):
            return await answer(request, "/v1/completions")

        @app.get("/health")
        async def health():
            return {"status": "ok"}

        for route in ("/props", "/slots", "/v1/models", "/completion", "/metrics"):
            def make(route=route):
                async def other():
                    self.other_hits.append(route)
                    return {"route": route}
                return other
            app.get(route)(make())
            app.post(route)(make())

        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port,
                                                    log_level="critical", lifespan="off"))
        self.thread = threading.Thread(target=self.server.run, daemon=True, name=f"fake-upstream-{name}")

    def reset(self) -> None:
        self.mode = "ok"
        self.fail_warmups = False        # answer the prefix-only warm-up (max_tokens 0) with an error
        self.answer_text = f"{self.name}-answer"
        self.chunks = [sse(f"{self.name}-one "), sse(f"{self.name}-two"), DONE]
        self.received.clear()
        self.other_hits.clear()
        self.gate.set()
        self.entered.clear()
        self.disconnected.clear()
        self.stream_closed = threading.Event()

    def start(self) -> "FakeUpstream":
        self.thread.start()
        assert wait_for(lambda: self.server.started), "the fake upstream did not start"
        return self

    def stop(self) -> None:
        self.gate.set()
        self.server.should_exit = True
        self.thread.join(timeout=5)


# ======================================================================================
# The llama-server binary
# ======================================================================================

_FAKE_BINARY = r'''#!__PYTHON__
import json, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RECORD = __RECORD__
READY_FLAG = __READY_FLAG__
with open(RECORD, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\n")
if __CRASH__:
    sys.exit(3)
port = int(sys.argv[sys.argv.index("--port") + 1])


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            ok = READY_FLAG is None or os.path.exists(READY_FLAG)
            self._send(200 if ok else 503, {"status": "ok"} if ok else {"error": "loading model"})
        else:
            self._send(404, {})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        body = json.loads(raw or b"{}")
        if self.path == "/v1/chat/completions" and not body.get("stream"):
            # What the join test reads: llama.cpp's own timings, for a 2,000-token prompt and 128 tokens out.
            return self._send(200, {"choices": [{"message": {"role": "assistant", "content": "alpha beta"}}],
                                    "timings": {"prompt_n": 2000, "predicted_n": 128,
                                                "prompt_per_second": 222.2, "predicted_per_second": 33.3}})
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for piece in ("alpha ", "beta"):
                chunk = {"choices": [{"index": 0, "delta": {"content": piece}}]}
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            self._send(200, {"choices": [{"message": {"role": "assistant", "content": "alpha beta"}}]})

    def log_message(self, *args):
        pass


ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
'''


def write_fake_coding_binary(path: str, record: str, crash: bool = False, ready_flag: Optional[str] = None) -> str:
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


def arg_after(argv: List[str], flag: str) -> Optional[str]:
    return argv[argv.index(flag) + 1] if flag in argv else None


# ======================================================================================
# Tiers and the downloader
# ======================================================================================

def make_tier(tier_id: str, parts: List[bytes], context: int = 65536, enabled: bool = True) -> Dict[str, Any]:
    """A coding ladder tier for ``parts`` (the model files' bytes), shaped like release.coding_tiers()."""
    stem = f"{tier_id}-Q4"
    names = ([f"{stem}.gguf"] if len(parts) == 1
             else [f"{stem}-{i:05d}-of-{len(parts):05d}.gguf" for i in range(1, len(parts) + 1)])
    files = [{"file": name, "url": f"https://huggingface.co/acme/{tier_id}/resolve/{'a' * 40}/{name}",
              "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
             for name, data in zip(names, parts)]
    return {"id": tier_id, "name": f"Test {tier_id}", "min_vram_gb": 8, "context": context, "enabled": enabled,
            "blocked": None, "files": files, **files[0], "size": sum(len(p) for p in parts)}


class FakeDownloader:
    """``engine.downloader.download_verified`` without a network: serves bytes by file name."""

    def __init__(self, contents: Dict[str, bytes]) -> None:
        self.contents = dict(contents)
        self.calls: List[List[str]] = []         # the file names asked for, per call
        self.result = True
        self.lasting = False                     # when it fails: "trying again unchanged cannot help"
        self.corrupt = False                     # write wrong bytes (and say it worked)
        self.options: List[Dict[str, Any]] = []   # the keyword arguments of each call (cancel=...)
        self.updates: List[Dict[str, Any]] = []   # what the progress callback received

    def __call__(self, files, dest_dir, progress=None, **options):
        """Like engine.downloader.fetch_verified: returns its DownloadResult."""
        from engine.downloader import DownloadResult
        self.calls.append([part["file"] for part in files])
        self.options.append(options)
        if not self.result:
            return DownloadResult(False, "the network is down", lasting=self.lasting)
        os.makedirs(dest_dir, exist_ok=True)
        total = sum(len(self.contents[part["file"]]) for part in files)
        done = 0
        for part in files:
            data = self.contents[part["file"]]
            if self.corrupt:
                data = b"X" * len(data)
            with open(os.path.join(dest_dir, part["file"]), "wb") as out:
                out.write(data)
            done += len(data)
            if progress is not None:
                update = {"phase": "downloading", "done": done, "total": total}
                self.updates.append(update)
                progress(update)
        return DownloadResult(True)


class AliveProcess:
    """A process that is running as far as the member can tell (for tests that fake a ready engine)."""

    pid = 0

    def __init__(self) -> None:
        self.returncode: Optional[int] = None

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode
