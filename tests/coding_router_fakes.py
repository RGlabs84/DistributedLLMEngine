"""
Stand-ins for coding members, shared by tests/test_coding_*.py.

* :class:`FakeMember` - a member's proxy as the coordinator sees it through the reverse tunnel:
  a FastAPI app served by uvicorn in a thread on a real loopback socket, answering
  ``POST /v1/chat/completions`` and ``POST /v1/completions``. It records every request exactly
  as it arrived (path, headers, raw bytes), so a test can say what a member learned about who
  asked. ``mode`` picks the behaviour: ``ok``, ``500``, ``400``, ``503``, ``json`` (a whole
  answer), ``huge`` (a whole answer of 9 MiB), ``die_before_first_chunk``, ``die_mid_stream``, ``hold_first`` (nothing until
  ``gate`` is set) and ``hold`` (the first chunk, then nothing until ``gate`` is set).
  Every member has an end-to-end ``KeyRing`` (engine/e2e.py): it opens sealed requests with it and
  answers them sealed with a ``ResponseSealer``, so a test can prove the coordinator carried
  bytes it could not read, and a client side ``ResponseOpener`` can read the answer.
* :func:`make_identity` / :func:`sign_block` - a member's long-term identity key and the signature
  on its heartbeat block (docs/coding-members.md section 2), written out here from the contract.
"""

import asyncio
import base64
import hashlib
import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from engine import e2e
from tests.app_pool_fakes import free_port, wait_for

MEMBER_MODEL_PATH = "/home/bob/models/Qwen3.6-35B-A3B-UD-IQ3_S.gguf"
DONE = b"data: [DONE]\n\n"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


# ======================================================================================
# A member's identity (docs/coding-members.md sections 1 and 2)
# ======================================================================================

def make_identity() -> Tuple[Ed25519PrivateKey, str, str]:
    """``(private key, base64 public key, trust id)``: the trust id is the first 32 hex characters of
    sha256 of the raw public key."""
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return key, b64(public), hashlib.sha256(public).hexdigest()[:32]


def sign_block(block: Dict[str, Any], key: Ed25519PrivateKey, signed_at: Optional[int] = None) -> Dict[str, Any]:
    """The block with ``signed_at`` and ``sig`` set: Ed25519 over the block without ``sig`` as canonical JSON."""
    signed = {k: v for k, v in block.items() if k != "sig"}
    signed["signed_at"] = int(time.time()) if signed_at is None else signed_at
    canonical = json.dumps(signed, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    signed["sig"] = b64(key.sign(canonical))
    return signed


# ======================================================================================
# Reading a sealed answer the way the extension does
# ======================================================================================

def open_sealed_stream(raw: bytes, resp_key: bytes) -> List[str]:
    """The texts of a sealed SSE answer, in order. Requires what the contract requires of a client: the
    first object carries ``rs``, the counter runs 0, 1, 2 ..., the sealed end arrives, and the plain
    ``[DONE]`` is last."""
    opener = e2e.ResponseOpener(resp_key)
    texts, plain_done = [], False
    for line in raw.decode().split("\n"):
        if not line.startswith("data: "):
            continue
        payload = line[len("data: "):]
        assert not plain_done, "a line after [DONE]"
        if payload == "[DONE]":
            plain_done = True
            continue
        data = opener.open(json.loads(payload))
        if data is None:
            continue
        chunk = json.loads(data)
        texts.append(chunk["choices"][0]["delta"].get("content", ""))
    assert plain_done, "the answer never said [DONE]"
    opener.close()              # raises unless the sealed end was seen
    return texts


def open_sealed_json(body: Dict[str, Any], resp_key: bytes) -> Dict[str, Any]:
    """A whole (non-streamed) sealed answer."""
    return json.loads(e2e.ResponseOpener(resp_key, streamed=False).open(body))


# ======================================================================================
# The member
# ======================================================================================

def member_chunk(text: str, final: bool = False) -> bytes:
    """One OpenAI-style streaming chunk as llama-server sends it: with its own file path in ``model``."""
    chunk: Dict[str, Any] = {"model": MEMBER_MODEL_PATH, "choices": [{"index": 0, "delta": {"content": text},
                                                                      "finish_reason": "stop" if final else None}]}
    if final:
        chunk["timings"] = {"predicted_per_second": 33.1, "prompt_per_second": 121.0}
    return f"data: {json.dumps(chunk)}\n\n".encode()


@dataclass
class Seen:
    """One request exactly as the member received it."""

    path: str
    headers: Dict[str, str]     # lower-case names
    raw: bytes

    @property
    def json(self) -> Any:
        return json.loads(self.raw)


class FakeMember:
    def __init__(self, name: str) -> None:
        self.name = name
        self.port = free_port()
        self.keyring = e2e.KeyRing()
        self.identity_key, self.identity_pub, self.trust_id = make_identity()
        self.requests: List[Seen] = []
        self.opened: List[Dict[str, Any]] = []     # bodies it opened from sealed requests
        self.gate = threading.Event()
        self.entered = threading.Event()
        self.reset()

        app = FastAPI()

        async def handle(request: Request, path: str):
            raw = await request.body()
            self.requests.append(Seen(path, {k.lower(): v for k, v in request.headers.items()}, raw))
            self.entered.set()
            mode = self.mode
            if mode in ("500", "400", "503"):
                return JSONResponse({"error": {"message": f"member says {mode} SECRET-DETAIL 10.8.0.99"}},
                                    status_code=int(mode))
            if mode == "huge":          # a whole answer far bigger than any member should send
                return Response(b" " * (9 * 1024 * 1024), media_type="application/json")
            body = json.loads(raw)
            sealer = None
            if isinstance(body, dict) and "dlne_e2e" in body:
                body, resp_key = self.keyring.open_request(body)
                self.opened.append(body)
                sealer = e2e.ResponseSealer(resp_key)
            if mode == "json":
                answer = {"model": MEMBER_MODEL_PATH, "timings": {"predicted_per_second": 33.1},
                          "choices": [{"index": 0, "message": {"role": "assistant", "content": f"{self.name}-whole"}}]}
                if sealer is not None:
                    return JSONResponse({"dlne_e2e": sealer.seal(json.dumps(answer).encode())})
                return JSONResponse(answer)
            pieces = list(self.texts)

            async def stream():
                if mode == "hold_first":
                    await asyncio.to_thread(self.gate.wait, 20)
                if mode == "die_before_first_chunk":
                    raise RuntimeError("died before answering")
                for i, text in enumerate(pieces):
                    if mode == "die_mid_stream" and i == 1:
                        raise RuntimeError("died mid-answer")
                    if mode == "hold" and i == 1:
                        await asyncio.to_thread(self.gate.wait, 20)
                    line = member_chunk(text, final=i == len(pieces) - 1)
                    if sealer is not None:
                        sealed = sealer.seal(line[len(b"data: "):].strip())
                        line = f"data: {json.dumps({'dlne_e2e': sealed})}\n\n".encode()
                    yield line
                    await asyncio.sleep(0.005)
                if sealer is not None:
                    yield f"data: {json.dumps({'dlne_e2e': sealer.finish()})}\n\n".encode()
                yield DONE

            return StreamingResponse(stream(), media_type="text/event-stream")

        @app.post("/v1/chat/completions")
        async def chat(request: Request):
            return await handle(request, "/v1/chat/completions")

        @app.post("/v1/completions")
        async def completions(request: Request):
            return await handle(request, "/v1/completions")

        @app.get("/health")
        async def health():
            return Response("ok")

        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port,
                                                    log_level="critical", lifespan="off"))
        self.thread = threading.Thread(target=self.server.run, daemon=True, name=f"fake-member-{name}")

    def reset(self) -> None:
        self.mode = "ok"
        self.texts = [f"{self.name}-one ", f"{self.name}-two"]
        self.requests.clear()
        self.opened.clear()
        self.gate.set()
        self.entered.clear()

    @property
    def pubkey(self) -> str:
        """The key the member advertises in its heartbeat now."""
        return self.keyring.public_b64

    def start(self) -> "FakeMember":
        self.thread.start()
        assert wait_for(lambda: self.server.started), f"fake member {self.name} did not start"
        return self

    def stop(self) -> None:
        self.gate.set()
        self.server.should_exit = True
        self.thread.join(timeout=5)
