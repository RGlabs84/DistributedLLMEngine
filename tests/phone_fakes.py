"""Stand-ins for the phone engine tests (engine/phone_engine.py, engine/phone_swap.py): a fake llama-server on a real
loopback socket (GET /health, POST /completion with llama.cpp's timings), and a fake engine for the switcher. Processes
and the spawner are tests/image_fakes.py's. Every wait is bounded."""

import asyncio
import threading
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from engine.auto_swap import SwapResult
from engine.phone_engine import phone_rungs
from tests.app_pool_fakes import free_port, wait_for
from tests.image_fakes import FakeProcess, Spawner      # noqa: F401 - re-exported


class FakeLlamaServer:
    """llama-server's /health and /completion. ``tps`` is what the timings report; ``loading`` makes /health 503."""

    def __init__(self) -> None:
        self.port = free_port()
        self.tps = 20.0
        self.loading = False
        self.bodies: List[Dict[str, Any]] = []
        app = FastAPI()

        @app.get("/health")
        async def health():
            return JSONResponse({"status": "loading model" if self.loading else "ok"}, status_code=503 if self.loading else 200)

        @app.post("/completion")
        async def completion(request: Request):
            self.bodies.append(await request.json())
            return {"content": " three", "timings": {"predicted_per_second": self.tps, "predicted_n": 48}}

        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="critical", lifespan="off"))
        self.thread: Optional[threading.Thread] = None

    def start(self) -> "FakeLlamaServer":
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        assert wait_for(lambda: self.server.started, 10), "the fake llama-server did not start"
        return self

    def stop(self) -> None:
        self.server.should_exit = True
        if self.thread is not None:
            self.thread.join(10)

    def reset(self) -> None:
        self.tps, self.loading = 20.0, False
        self.bodies.clear()


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeEngine:
    """The part of PhoneEngine the switcher reads, with a scripted ``switch``."""

    def __init__(self, clock: FakeClock, base_id: str = "qwen35-4b") -> None:
        self.clock = clock
        self._rungs = phone_rungs()
        self.is_own = True
        self.loaded = False
        self.rung: Optional[Dict[str, Any]] = None
        self.volunteer_ids: List[str] = []
        self.in_flight = 0
        self.switching = False
        self.base_id = base_id
        self.unavailable: Optional[str] = None
        self.calls: List[Dict[str, Any]] = []
        self.outcome = "swapped"            # or busy / failed
        self.on_switch = None

    def own(self) -> bool:
        return self.is_own

    def base_rung(self) -> Dict[str, Any]:
        return next(r for r in self._rungs if r["id"] == self.base_id)

    def unavailable_reason(self, rung=None) -> Optional[str]:
        return self.unavailable

    async def switch(self, rung, volunteers, *, drain_seconds=0.0, abort_if_busy=False, check_speed=False,
                     restore=None, min_tps=None) -> SwapResult:
        self.calls.append({"rung": rung["id"], "volunteers": [v["id"] for v in volunteers], "abort_if_busy": abort_if_busy,
                           "check_speed": check_speed, "restore": restore["id"] if restore else None, "min_tps": min_tps})
        if self.on_switch:
            self.on_switch()
        if self.outcome == "swapped":
            self.loaded, self.rung, self.volunteer_ids = True, rung, [v["id"] for v in volunteers]
            return SwapResult("swapped")
        if self.outcome == "failed":
            base = restore or self.base_rung()
            self.loaded, self.rung, self.volunteer_ids = True, base, []
            return SwapResult("failed", "too slow")
        return SwapResult(self.outcome, "x")

    async def stop(self) -> None:
        self.calls.append({"stop": True})
        self.loaded, self.rung, self.volunteer_ids = False, None, []
