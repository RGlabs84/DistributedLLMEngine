"""
Stand-ins for image generation (2.6.1), shared by the tests/test_image_*.py files.

* :func:`sd_png` - a PNG the way sd-server writes one: pixels in IDAT and the whole prompt in a ``parameters`` tEXt chunk
  (plus a time stamp, a compressed text chunk and an EXIF chunk, which must be stripped as well).
* :class:`FakeSdServer` - the native /sdcpp/v1 API on a real loopback socket (uvicorn in a thread, as
  tests/app_pool_fakes.py does for llama-server). ``mode`` picks the behaviour: ``ok``, ``hold`` (generating until
  ``gate`` is set), ``fail`` (the job fails), ``badpng`` (completed with something that is not a PNG).
* :class:`FakeProcess` and :func:`fake_spawn` - the child process, as far as the member reads it.
"""

import base64
import struct
import threading
import time
import zlib
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from tests.app_pool_fakes import free_port, wait_for


def chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def sd_png(width: int = 8, height: int = 8, prompt: str = "a red fox", colour: int = 200) -> bytes:
    """A real, decodable RGB PNG carrying what sd-server puts in one."""
    rows = b"".join(b"\x00" + bytes([colour, 40, 40]) * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"sRGB", b"\x00")
            + chunk(b"tEXt", b"parameters\x00" + prompt.encode() + b"\nSteps: 8, Seed: 42, Model: z_image_turbo")
            + chunk(b"tIME", struct.pack(">HBBBBB", 2026, 10, 28, 5, 0, 0))
            + chunk(b"zTXt", b"Comment\x00\x00" + zlib.compress(prompt.encode()))
            + chunk(b"eXIf", b"MM\x00*" + prompt.encode())
            + chunk(b"IDAT", zlib.compress(rows))
            + chunk(b"IEND", b""))


class FakeSdServer:
    """sd-server's native API. Records every img_gen body and every cancel."""

    def __init__(self) -> None:
        self.port = free_port()
        self.bodies: List[Dict[str, Any]] = []
        self.cancels: List[str] = []
        self.mode = "ok"
        self.ignore_cancel = False
        self.gate = threading.Event()
        self.ready = True
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._cancelled: set = set()
        app = FastAPI()

        @app.get("/sdcpp/v1/capabilities")
        async def capabilities():
            return JSONResponse({"supported_modes": ["img_gen"]}, status_code=200 if self.ready else 503)

        @app.post("/sdcpp/v1/img_gen")
        async def img_gen(request: Request):
            body = await request.json()
            self.bodies.append(body)
            job_id = f"job_{len(self.bodies):04d}"
            self._jobs[job_id] = body
            return JSONResponse({"id": job_id, "kind": "img_gen", "status": "queued", "created": 1,
                                 "poll_url": f"/sdcpp/v1/jobs/{job_id}"}, status_code=202)

        @app.get("/sdcpp/v1/jobs/{job_id}")
        async def job(job_id: str):
            body = self._jobs.get(job_id)
            if body is None:
                return JSONResponse({"error": "unknown job"}, status_code=404)
            if job_id in self._cancelled:
                return {"id": job_id, "status": "cancelled", "result": None, "error": None}
            if self.mode == "hold" and not self.gate.is_set():
                return {"id": job_id, "status": "generating", "result": None, "error": None}
            if self.mode == "fail":
                return {"id": job_id, "status": "failed", "result": None, "error": {"message": "out of memory"}}
            data = b"not a png" if self.mode == "badpng" else sd_png(body["width"], body["height"], body["prompt"])
            return {"id": job_id, "status": "completed", "started": 1000, "completed": 2000,
                    "result": {"images": [{"index": 0, "b64_json": base64.b64encode(data).decode()}], "output_format": "png"},
                    "error": None}

        @app.post("/sdcpp/v1/jobs/{job_id}/cancel")
        async def cancel(job_id: str):
            self.cancels.append(job_id)
            if not self.ignore_cancel:           # True: keeps generating after a cancel, as when it is past a checkpoint
                self._cancelled.add(job_id)
            return {"id": job_id, "cancel_requested": True}

        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="critical", lifespan="off"))
        self.thread = threading.Thread(target=self.server.run, daemon=True, name="fake-sd")

    def start(self) -> "FakeSdServer":
        self.thread.start()
        assert wait_for(lambda: self.server.started)
        return self

    def stop(self) -> None:
        self.gate.set()
        self.server.should_exit = True
        self.thread.join(timeout=5)

    def reset(self) -> None:
        self.bodies.clear()
        self.cancels.clear()
        self._jobs.clear()
        self._cancelled.clear()
        self.mode = "ok"
        self.ignore_cancel = False
        self.ready = True
        self.gate.clear()


class FakeProcess:
    """A child that is alive until it is told to stop (or ``die()``)."""

    def __init__(self) -> None:
        self.returncode: Optional[int] = None
        self.terminated = False
        self.killed = False
        self.waited = 0

    def poll(self) -> Optional[int]:
        return self.returncode

    def die(self, code: int = 1) -> None:
        self.returncode = code

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: Optional[float] = None) -> int:
        self.waited += 1
        return self.returncode if self.returncode is not None else 0


class Spawner:
    """``subprocess.Popen`` as the member calls it: remembers every start (command and keyword arguments)."""

    def __init__(self) -> None:
        self.starts: List[Dict[str, Any]] = []
        self.processes: List[FakeProcess] = []
        self.fail = False

    def __call__(self, command, **kwargs):
        if self.fail:
            raise FileNotFoundError("no such binary")
        self.starts.append({"command": list(command), **kwargs})
        process = FakeProcess()
        self.processes.append(process)
        return process
