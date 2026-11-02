"""
Stand-ins for the takeover tests (engine/takeover.py): TheWall's three cards, a clock the tests move by hand, the
coordinator's side of the controller (the engine, the idle test, the switch) and the systemd memory limits.
Nothing here touches the machine: the controller is given a ``Host`` made of these.
"""

import asyncio
from typing import Any, Dict, List, Optional

import pytest

from engine import takeover
from engine.config import get_plain_worker_limits, get_worker_limits

V520_A = {"id": 0, "name": "AMD Radeon Pro V520 (BC-160)", "address": "0000:03:00.0", "vk_index": 0,
          "vram_total_gb": 8.0, "vram_free_gb": 7.98, "load_percent": 0.0, "integrated": False}
V520_B = {"id": 1, "name": "AMD Radeon Pro V520 (BC-160)", "address": "0000:09:00.0", "vk_index": 1,
          "vram_total_gb": 8.0, "vram_free_gb": 7.5, "load_percent": 0.0, "integrated": False}
RX580 = {"id": 2, "name": "AMD Radeon RX 580", "address": "0000:0c:00.0", "vk_index": 2,
         "vram_total_gb": 8.0, "vram_free_gb": 7.1, "load_percent": 0.0, "integrated": False}
IGPU = {"id": 3, "name": "AMD Radeon Graphics (RADV RAPHAEL_MENDOCINO)", "address": "0000:15:00.0", "vk_index": 3,
        "vram_total_gb": 0.5, "vram_free_gb": 0.4, "load_percent": 0.0, "integrated": True}

POOL_A, POOL_B, MEDIA = "0000:03:00.0", "0000:09:00.0", "0000:0c:00.0"

# The default config of a test: the engine on card B, the RX 580 the media card with a projector, pictures on, the
# engine pinned to cores 0 and 1 (0xc3 = CPUs 0, 1 and their siblings 6, 7).
DEFAULT_CONFIG = (
    f"ALLOWED_GPU_INDICES={POOL_B}\n"
    f"MEDIA_GPU={MEDIA}\n"
    "ENGINE_MMPROJ=/models/mmproj-F16.gguf\n"
    "IMAGE_GEN_ENABLED=true\n"
    "ENGINE_EXTRA_ARGS=--threads 4 --cpu-mask 0xc3 -ts 1,1\n"
)


def engine_facts(**overrides) -> takeover.EngineFacts:
    """A 5.5 GB model with a 20 KiB-a-token cache, running 2 lanes of 131,072 tokens."""
    values = dict(model="Qwen3.5-9B-Q4_K_M.gguf", weights_gb=5.5, unplaced_weights_gb=5.8, kv_f16_bytes_per_token=20480,
                  state_bytes_per_slot=0, native_context=262144, slots=2, context_per_slot=131072)
    values.update(overrides)
    return takeover.EngineFacts(**values)


class Clock:
    """Wall time that moves only when a test says so (or a fake ``sleep`` does)."""

    def __init__(self, start: float = 1_800_000_000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds
        await asyncio.sleep(0)


class Memory:
    """The unit's MemoryMax/MemoryHigh, as ``systemctl --user show`` / ``set-property --runtime`` would keep them."""

    def __init__(self):
        self.values: Optional[Dict[str, str]] = {"MemoryMax": "infinity", "MemoryHigh": "infinity"}
        self.sets: List[Dict[str, str]] = []
        self.accepts = True

    def read(self) -> Optional[Dict[str, str]]:
        return dict(self.values) if self.values is not None else None

    def set(self, values: Dict[str, str]) -> bool:
        self.sets.append(dict(values))
        if self.accepts:
            self.values = dict(values)
        return self.accepts


class Box:
    """The coordinator as the controller sees it. ``restarts`` records every engine restart with the limits in force at
    that moment, which is how a test sees the overlay was (or was not) applied when the engine came up."""

    def __init__(self, clock: Clock):
        self.clock = clock
        self.gpus: List[dict] = [dict(V520_A), dict(V520_B), dict(RX580), dict(IGPU)]
        self.facts: Optional[takeover.EngineFacts] = engine_facts()
        self.idle_script: List[bool] = []
        self.idle_default = True
        self.idle_calls = 0
        self.results: List[Dict[str, Any]] = []
        self.restarts: List[Dict[str, Any]] = []
        self.pictures_stopped = 0
        self.observed: Optional[tuple] = (1, 90112)
        self.pool = (16.0, 32.0)
        self.system_ram_gb = 32.0
        self.restart_gate: Optional[asyncio.Event] = None      # a restart waits for it while it is set and unset

    def idle(self) -> bool:
        self.idle_calls += 1
        return self.idle_script.pop(0) if self.idle_script else self.idle_default

    async def get_gpus(self):
        return [dict(g) for g in self.gpus]

    async def get_facts(self):
        return self.facts

    async def restart_engine(self, *, expected_pool, reduced, patient):
        limits = get_worker_limits()
        self.restarts.append({
            "expected_pool": expected_pool, "reduced": reduced, "patient": patient,
            "allowed_gpu_indices": limits["allowed_gpu_indices"], "engine_slots": limits["engine_slots"],
            "engine_context": limits["engine_context"], "engine_mmproj_cpu": limits["engine_mmproj_cpu"],
            "image_gen_paused": limits["image_gen_paused"], "engine_extra_args": list(limits["engine_extra_args"]),
            "image_cpu_mask": limits["image_cpu_mask"], "at": self.clock()})
        if self.restart_gate is not None:
            await self.restart_gate.wait()
        await asyncio.sleep(0)
        return self.results.pop(0) if self.results else {"outcome": "swapped", "success": True, "detail": ""}

    async def stop_pictures(self):
        self.pictures_stopped += 1

    async def observe(self):
        return self.observed

    def host(self) -> takeover.Host:
        return takeover.Host(
            gpus=self.get_gpus, engine_facts=self.get_facts, idle=self.idle, restart_engine=self.restart_engine,
            pool_totals=lambda: self.pool, pause_pictures=self.stop_pictures, observe_engine=self.observe,
            read_limits=get_plain_worker_limits,
            system_ram_gb=lambda: self.system_ram_gb)

    def controller(self) -> takeover.TakeoverController:
        return takeover.TakeoverController(self.host(), clock=self.clock, sleep=self.clock.sleep)


@pytest.fixture
def box(isolated_config, monkeypatch, tmp_path):
    """A temporary installation with TheWall's config, the fakes above in place of the machine, and a clock to move."""
    clock, memory = Clock(), Memory()
    isolated_config.write_text(DEFAULT_CONFIG)
    monkeypatch.setattr(takeover, "now", clock)
    monkeypatch.setattr(takeover, "read_memory_limits", memory.read)
    monkeypatch.setattr(takeover, "set_memory_limits", memory.set)
    monkeypatch.setattr(takeover, "PCI_SYSFS", str(tmp_path / "pci"))
    monkeypatch.setattr(takeover, "CPU_SYSFS", str(tmp_path / "cpu"))
    monkeypatch.setattr(takeover, "logical_cpu_count", lambda: 12)
    made = Box(clock)
    made.mem, made.config_path = memory, isolated_config
    return made
