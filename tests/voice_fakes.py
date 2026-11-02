"""
Stand-ins for the voice tests (engine/voice_*.py): WAV files of any shape, multipart bodies, and a fake whisper-server on a
real loopback socket speaking the part of whisper.cpp's examples/server API that DLNE uses (GET / and POST /inference).
Processes and the spawner are tests/image_fakes.py's.
"""

import asyncio
import struct
import threading
from typing import Any, Dict, List

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from engine import voice_pool
from tests.app_pool_fakes import free_port, wait_for
from tests.image_fakes import FakeProcess, Spawner      # noqa: F401 - re-exported for the voice tests


def make_wav(seconds: float = 1.0, rate: int = 16000, channels: int = 1, bits: int = 16, tag: int = 1,
             extra_chunk: bool = False, pad: int = 0) -> bytes:
    """A RIFF/WAVE file of silence in the given shape (``pad`` bytes of data past ``seconds``)."""
    data = b"\x00" * (int(rate * seconds) * channels * (bits // 8) + pad)
    fmt = struct.pack("<HHIIHH", tag, channels, rate, rate * channels * bits // 8, channels * bits // 8, bits)
    chunks = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    if extra_chunk:
        chunks += b"LIST" + struct.pack("<I", 5) + b"abcde" + b"\x00"          # odd size, padded
    chunks += b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks


def multipart(fields: Dict[str, Any], boundary: str = "XBOUNDARYX") -> tuple:
    """``(content_type, body)``: bytes values are files (named clip.wav), str values plain fields."""
    body = b""
    for name, value in fields.items():
        body += f"--{boundary}\r\n".encode()
        if isinstance(value, bytes):
            body += (f'Content-Disposition: form-data; name="{name}"; filename="clip.wav"\r\n'
                     "Content-Type: audio/wav\r\n\r\n").encode() + value + b"\r\n"
        else:
            body += f'Content-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
    body += f"--{boundary}--\r\n".encode()
    return f"multipart/form-data; boundary={boundary}", body


class FakeWhisperServer:
    """whisper-server's HTTP API. Records every /inference form. ``mode``: ok, hold (waits for ``gate``), fail, garbage."""

    def __init__(self) -> None:
        self.port = free_port()
        self.forms: List[Dict[str, bytes]] = []
        self.mode = "ok"
        self.text = " Hello  there.\n"
        self.language = "english"
        self.gate = threading.Event()
        self.ready = True
        app = FastAPI()

        @app.get("/")
        async def index():
            return JSONResponse({"page": "whisper.cpp server"}, status_code=200 if self.ready else 503)

        @app.post("/inference")
        async def inference(request: Request):
            form = voice_pool.parse_form(request.headers["content-type"], await request.body())
            self.forms.append(form)
            if self.mode == "hold":
                while not self.gate.is_set():
                    await asyncio.sleep(0.01)
            if self.mode == "fail":
                return JSONResponse({"error": "failed to process audio"}, status_code=500)
            if self.mode == "garbage":
                return JSONResponse(["not", "a", "dict"])
            asked = form.get("language", b"auto").decode()
            # verbose_json names the language it used in full ("english"); a requested code comes back as its name
            return {"task": "transcribe", "language": self.language if asked == "auto" else "german",
                    "duration": 1.0, "text": self.text, "segments": []}

        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="critical", lifespan="off"))
        self.thread = threading.Thread(target=self.server.run, daemon=True, name="fake-whisper")

    def start(self) -> "FakeWhisperServer":
        self.thread.start()
        assert wait_for(lambda: self.server.started)
        return self

    def stop(self) -> None:
        self.gate.set()
        self.server.should_exit = True
        self.thread.join(timeout=5)

    def reset(self) -> None:
        self.forms.clear()
        self.mode = "ok"
        self.text = " Hello  there.\n"
        self.language = "english"
        self.ready = True
        self.gate.clear()
