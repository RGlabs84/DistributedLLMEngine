"""
The phones' own engine (2.9.0, contract 6c and 6c+): a llama-server the coordinator starts and supervises for the phone
app and the web chat, on PHONE_DEVICE (empty: the media card, the RX 580 on TheWall), separate from the coding engine,
so coding never waits for phones and phones never wait for a coding switch.

    PHONE_ENGINE=shared   (default) nothing is started: phones use the coding engine's lanes, as before 2.9.0
    PHONE_ENGINE=own      this module starts, checks and stops the phone engine

It is modelled on engine/voice_member.py (start, ready check, card check, three strikes, stop) and shares its helpers.
llama-server listens on 127.0.0.1 only, on a free port, runs in an empty private directory and is given an empty
directory for ffmpeg (so a photo it cannot read is never piped to a program found on PATH). Its output is kept only
while it starts. Every wait here has a deadline.

THE LADDER (PHONE_RUNGS)
    qwen35-4b    Qwen3.5 4B + photo reader: the base, on the home card alone
    qwen35-9b    Qwen3.5 9B + photo reader: part of its layers on volunteers
    qwen36-a3b   Qwen3.6 35B-A3B + photo reader: most of its layers (experts) on volunteers
    Each is pinned by Hugging Face revision, size and SHA-256 like the coding ladder (engine/coding_ladder.py).

THE HOME BUDGET NEVER GROWS (6c+)
    PHONE_HOME_BUDGET_MB is the most the phone engine may put on the home card at any rung, the photo reader included
    (0: what the base rung uses). :func:`plan_layout` puts on the home card the layers that fit the budget after the
    fixed costs, and the rest on the volunteers' RPC endpoints (a GPU, or a ``cpu`` endpoint with lent RAM). So voice,
    the picture engine and the photo reader keep their room whatever the phones do.

PHOTOS STAY HOME
    The photo reader (``--mmproj``) is always pinned to the home device (``--mmproj-device``); it is never given to a
    volunteer, and a layout whose home card cannot hold it is refused rather than moved. On a sharded model the photo
    is read at home and only the model's layers run on the volunteers.

WHAT THE REST OF 2.9.0 USES (the seams)
    ``can_read_photos()``  True when the engine is up and has a photo reader (routing 6b / 6e asks this)
    ``lanes_free()``       free lanes, 0 while down, draining or checking its speed
    ``acquire()/release()`` or ``async with lane():`` one phone request's lane; ``base_url`` is where it is sent
    ``switch(...)``        the switcher's one entry (engine/phone_swap.py): drain, restart on a rung, speed check
    ``status()``           the ``phone_engine`` block of /v1/cluster/status
"""

import asyncio
import contextlib
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple

import httpx

from engine.auto_swap import SwapResult
from engine.config import get_base_dir, get_worker_limits
from engine.image_member import (MemberError, StartupOutput, STARTUP_LOG_LINES, _default_media_card,
                                 _default_media_spec, _free_port, exit_text)
from engine.logger import log_coordinator, log_error, log_warning

__all__ = ["PhoneEngine", "PHONE_RUNGS", "phone_rungs", "plan_layout", "volunteer_need_gb", "valid_endpoint",
           "resolve_budget_mb", "rung_files", "UNAVAILABLE", "FAILED"]

DEFAULT_MODEL_SUBDIR = os.path.join("models", "phone")
UNAVAILABLE = "phone_engine_unavailable"
FAILED = "phone_engine_failed"

FAILURES_BEFORE_COOLDOWN = 3
COOLDOWN_SECONDS = 30 * 60
SPEED_CHECK_TOKENS = 48
SPEED_CHECK_TIMEOUT_SECONDS = 60.0
HEALTH_TIMEOUT_SECONDS = 5.0

MIB = 1024 * 1024
# A volunteer's memory is never filled to the brim: a tenth stays free for its own buffers.
VOLUNTEER_USABLE = 0.9
# The engine's fixed cost on the home card besides weights, cache and photo reader (compute buffers).
COMPUTE_MB = 200.0

_ENDPOINT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,252}:[0-9]{1,5}$")

# ----------------------------------------------------------------------------------------------------------------
# The ladder, pinned (Hugging Face API at the revision named in each url: size and SHA-256 as the API reports them)
# ----------------------------------------------------------------------------------------------------------------
_Q35_4B = "https://huggingface.co/unsloth/Qwen3.5-4B-GGUF/resolve/e87f176479d0855a907a41277aca2f8ee7a09523/"
_Q35_9B = "https://huggingface.co/unsloth/Qwen3.5-9B-GGUF/resolve/3885219b6810b007914f3a7950a8d1b469d598a5/"
_Q36 = "https://huggingface.co/unsloth/Qwen3.6-35B-A3B-GGUF/resolve/a483e9e6cbd595906af30beda3187c2663a1118c/"

# Smallest first; the first is the base. ``layers`` and ``kv_mb_per_1k`` are planning figures for the home budget (the
# cache is counted at f16, which is more than the engine uses); ``weights_mb`` and ``mmproj_mb`` come from the sizes.
PHONE_RUNGS: List[Dict[str, Any]] = [
    {
        "id": "qwen35-4b", "name": "Qwen3.5 4B", "layers": 32, "kv_mb_per_1k": 32.0,
        "file": "Qwen3.5-4B-Q4_K_M.gguf", "mmproj": "Qwen3.5-4B-mmproj-F16.gguf",
        "files": [
            {"file": "Qwen3.5-4B-Q4_K_M.gguf", "url": _Q35_4B + "Qwen3.5-4B-Q4_K_M.gguf", "size": 2740937888,
             "sha256": "00fe7986ff5f6b463e62455821146049db6f9313603938a70800d1fb69ef11a4"},
            {"file": "Qwen3.5-4B-mmproj-F16.gguf", "url": _Q35_4B + "mmproj-F16.gguf", "size": 672423616,
             "sha256": "cd88edcf8d031894960bb0c9c5b9b7e1fea6ebee02b9f7ce925a00d12891f864"},
        ],
    },
    {
        "id": "qwen35-9b", "name": "Qwen3.5 9B", "layers": 32, "kv_mb_per_1k": 32.0,
        "file": "Qwen3.5-9B-Q4_K_M.gguf", "mmproj": "Qwen3.5-9B-mmproj-F16.gguf",
        "files": [
            {"file": "Qwen3.5-9B-Q4_K_M.gguf", "url": _Q35_9B + "Qwen3.5-9B-Q4_K_M.gguf", "size": 5680522464,
             "sha256": "03b74727a860a56338e042c4420bb3f04b2fec5734175f4cb9fa853daf52b7e8"},
            {"file": "Qwen3.5-9B-mmproj-F16.gguf", "url": _Q35_9B + "mmproj-F16.gguf", "size": 918166080,
             "sha256": "f70dc3509053962b0d0d3ee8a7eacebf5d60aa560cad78254ae8698516ae029f"},
        ],
    },
    {
        # The same file the coding ladder's IQ4_XS rung pins (scripts/coding_models.json qwen36-a3b-iq4xs).
        "id": "qwen36-a3b", "name": "Qwen3.6 35B-A3B", "layers": 40, "kv_mb_per_1k": 20.0,
        "file": "Qwen3.6-35B-A3B-UD-IQ4_XS.gguf", "mmproj": "Qwen3.6-35B-A3B-mmproj-F16.gguf",
        "files": [
            {"file": "Qwen3.6-35B-A3B-UD-IQ4_XS.gguf", "url": _Q36 + "Qwen3.6-35B-A3B-UD-IQ4_XS.gguf",
             "size": 17730509792, "sha256": "649d7508507b84638732c4f52c24c8b15843c6dca2f3ff793ae07c14a67ebbb3"},
            {"file": "Qwen3.6-35B-A3B-mmproj-F16.gguf", "url": _Q36 + "mmproj-F16.gguf", "size": 899283680,
             "sha256": "8971ee4f331ff0a4c609374f32984b3d4e6dc086c0aa35f1d637fad1829e887f"},
        ],
    },
]


def phone_rungs() -> List[Dict[str, Any]]:
    """A copy of the ladder, base first, each rung with its sizes in MiB worked out from the pinned files."""
    rungs: List[Dict[str, Any]] = []
    for rung in PHONE_RUNGS:
        copy = dict(rung)
        copy["files"] = [dict(entry) for entry in rung["files"]]
        sizes = {entry["file"]: entry["size"] for entry in rung["files"]}
        copy["weights_mb"] = sizes[rung["file"]] / MIB
        copy["mmproj_mb"] = sizes[rung["mmproj"]] / MIB
        rungs.append(copy)
    return rungs


def rung_files(rung: Dict[str, Any], vision: bool = True) -> List[Dict[str, Any]]:
    """The files a rung needs on disk: the model, and the photo reader when vision is on."""
    files = rung.get("files") or []
    return [entry for entry in files if vision or entry.get("file") != rung.get("mmproj")]


def valid_endpoint(endpoint: Any) -> bool:
    """``host:port`` and nothing else: an RPC endpoint is a command-line argument, so it is never free text."""
    if not isinstance(endpoint, str) or _ENDPOINT_RE.fullmatch(endpoint) is None:
        return False
    return 0 < int(endpoint.rsplit(":", 1)[1]) < 65536


def _fixed_mb(rung: Dict[str, Any], vision: bool) -> float:
    """Costs on the home card that do not shrink when layers move away: the photo reader and the compute buffers."""
    return COMPUTE_MB + (float(rung["mmproj_mb"]) if vision else 0.0)


def _per_layer_mb(rung: Dict[str, Any], lanes: int, context: int) -> float:
    """One layer's weights plus its share of the context cache (``lanes`` x ``context`` tokens)."""
    cache = float(rung["kv_mb_per_1k"]) * lanes * context / 1000.0
    return (float(rung["weights_mb"]) + cache) / int(rung["layers"])


def resolve_budget_mb(limits: Dict[str, Any], rungs: Optional[List[Dict[str, Any]]] = None) -> float:
    """PHONE_HOME_BUDGET_MB; 0 means what the base rung uses on the home card with these lanes, context and vision."""
    try:
        asked = float(limits.get("phone_home_budget_mb") or 0)
    except (TypeError, ValueError):
        asked = 0.0
    if asked > 0:
        return asked
    rungs = rungs or phone_rungs()
    base = next((r for r in rungs if r["id"] == limits.get("phone_model")), rungs[0])
    lanes, context = int(limits.get("phone_lanes", 2)), int(limits.get("phone_context", 8192))
    vision = bool(limits.get("phone_vision", True))
    return _fixed_mb(base, vision) + _per_layer_mb(base, lanes, context) * int(base["layers"])


def _volunteer_capacity_mb(volunteer: Dict[str, Any]) -> float:
    key = "vram_gb" if volunteer.get("kind") == "gpu" else "ram_lent_gb"
    try:
        gb = float(volunteer.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return gb * 1024.0 * VOLUNTEER_USABLE if math.isfinite(gb) and gb > 0 else 0.0


def plan_layout(rung: Dict[str, Any], volunteers: List[Dict[str, Any]], *, budget_mb: float, lanes: int,
                context: int, vision: bool, host_cpu: bool = False) -> Dict[str, Any]:
    """
    Where a rung's layers go. ``volunteers`` are the ones the switcher already found near enough and settled, each
    ``{"id", "kind": "gpu"|"cpu", "endpoint", "vram_gb" (gpu) or "ram_lent_gb" (cpu)}``.

    The home card gets as many layers as fit ``budget_mb`` after the photo reader and the compute buffers; the rest go to
    the volunteers, biggest first, a volunteer's GPU memory or lent RAM at nine tenths. Result::

        {"ok", "reason", "home_layers", "volunteer_layers": {id: n}, "home_mb", "need_mb", "split": [home, v1, ...],
         "used": [volunteer dicts in split order]}

    ``home_mb`` is what the layout puts on the home card, always <= ``budget_mb`` when ``ok``. With ``host_cpu`` (the
    device is the host's processor) nothing is split: the whole model runs on the host and volunteers are not used.
    """
    layers = int(rung["layers"])
    plan: Dict[str, Any] = {"ok": False, "reason": "", "home_layers": 0, "volunteer_layers": {}, "home_mb": 0.0,
                            "need_mb": 0.0, "split": [], "used": []}
    if host_cpu:
        plan.update(ok=True, home_layers=layers, split=[layers])
        return plan
    fixed, per_layer = _fixed_mb(rung, vision), _per_layer_mb(rung, lanes, context)
    room = budget_mb - fixed
    if room < 0:
        plan["reason"] = f"the home budget ({budget_mb:.0f} MB) cannot hold the photo reader and buffers ({fixed:.0f} MB)"
        return plan
    home_layers = min(layers, int(room // per_layer))
    remaining = layers - home_layers
    plan["home_layers"], plan["home_mb"] = home_layers, fixed + home_layers * per_layer
    plan["need_mb"] = remaining * per_layer / VOLUNTEER_USABLE
    split, used = [home_layers], []
    assigned: Dict[str, int] = {}
    if remaining:
        candidates = sorted((v for v in volunteers if isinstance(v, dict) and valid_endpoint(v.get("endpoint"))
                             and v.get("kind") in ("gpu", "cpu") and v.get("id")),
                            key=lambda v: (v.get("kind") != "gpu", -_volunteer_capacity_mb(v)))
        for volunteer in candidates:
            if remaining <= 0:
                break
            take = min(remaining, int(_volunteer_capacity_mb(volunteer) // per_layer))
            if take <= 0:
                continue
            assigned[str(volunteer["id"])] = take
            split.append(take)
            used.append(volunteer)
            remaining -= take
        if remaining > 0:
            plan["reason"] = f"{remaining} layer(s) of {rung['name']} have nowhere to go: the volunteers hold too little"
            return plan
    plan.update(ok=True, volunteer_layers=assigned, split=split, used=used)
    return plan


def volunteer_need_gb(rung: Dict[str, Any], limits: Dict[str, Any]) -> float:
    """What a rung needs from volunteers beyond the home budget, GPU memory and lent RAM together, in GB."""
    plan = plan_layout(rung, [], budget_mb=resolve_budget_mb(limits), lanes=int(limits.get("phone_lanes", 2)),
                       context=int(limits.get("phone_context", 8192)), vision=bool(limits.get("phone_vision", True)))
    return round(plan["need_mb"] / 1024.0, 2)


class PhoneEngine:
    """
    The phone llama-server and everything about running it. The knobs are read from ``limits`` when they are used.
    Collaborators are parameters so tests can stand in for the process, the card, the binary and the clock.
    """

    def __init__(self, limits: Callable[[], Dict[str, Any]] = get_worker_limits,
                 base_dir: Callable[[], str] = get_base_dir,
                 spawn: Callable[..., Any] = subprocess.Popen,
                 media_spec: Callable[[Dict[str, Any]], str] = _default_media_spec,
                 card_of: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
                 binary: Optional[Callable[[], str]] = None,
                 clock: Callable[[], float] = time.monotonic,
                 poll_seconds: float = 0.5,
                 ready_timeout: Optional[float] = None,
                 free_port: Callable[[], int] = _free_port,
                 http_client: Optional[Callable[[int, float], httpx.AsyncClient]] = None,
                 resolve: Optional[Callable[[str], Optional[str]]] = None) -> None:
        self._limits, self._base_dir, self._spawn = limits, base_dir, spawn
        # resolve(endpoint) -> the address llama-server's --rpc reaches it at (the household LAN, direct, or the reverse
        # tunnel's local relay; engine/reverse_tunnel.resolve_endpoint), None when it cannot be reached. None: as given.
        self._resolve = resolve
        self._media_spec = media_spec
        self._card_of = card_of or _default_media_card
        self._binary_of = binary
        self._clock, self._poll, self._ready_override = clock, poll_seconds, ready_timeout
        self._free_port = free_port
        self._http = http_client
        self.process: Optional[Any] = None
        self._output: Optional[StartupOutput] = None
        self.port: Optional[int] = None
        self._workdir: Optional[str] = None
        self.rung: Optional[Dict[str, Any]] = None
        self.layout: Optional[Dict[str, Any]] = None
        self.volunteer_ids: List[str] = []
        self.failures = 0
        self.unavailable_until: Optional[float] = None
        self.last_tps: Optional[float] = None
        self._in_flight = 0
        self._accepting = False             # phones are let in only once the engine is up and has passed its speed check
        self._switching = False
        self._lock: Optional[asyncio.Lock] = None
        self._lock_loop: Optional[asyncio.AbstractEventLoop] = None

    # ------------------------------------------------------------------ settings and files
    def mode(self) -> str:
        return str(self._limits().get("phone_engine") or "shared")

    def own(self) -> bool:
        return self.mode() == "own"

    def model_dir(self, limits: Optional[Dict[str, Any]] = None) -> str:
        asked = str((limits if limits is not None else self._limits()).get("phone_model_dir") or "")
        return asked or os.path.join(self._base_dir(), DEFAULT_MODEL_SUBDIR)

    def binary(self) -> str:
        if self._binary_of is not None:
            return self._binary_of()
        from engine.downloader import engine_dir          # late: it is a big module
        return os.path.join(engine_dir(self._base_dir()), "llama-server")

    def rung_by_id(self, rung_id: Any) -> Optional[Dict[str, Any]]:
        return next((r for r in phone_rungs() if r["id"] == rung_id), None)

    def base_rung(self) -> Dict[str, Any]:
        """The rung PHONE_MODEL names, which is where the engine starts and what it goes back to."""
        return self.rung_by_id(self._limits().get("phone_model")) or phone_rungs()[0]

    def files_present(self, rung: Dict[str, Any], limits: Optional[Dict[str, Any]] = None) -> bool:
        """Every file the rung needs is on disk at its pinned size (the switcher's download step checks the SHA-256)."""
        limits = limits if limits is not None else self._limits()
        directory = self.model_dir(limits)
        for entry in rung_files(rung, bool(limits.get("phone_vision", True))):
            try:
                if os.path.getsize(os.path.join(directory, entry["file"])) != entry["size"]:
                    return False
            except OSError:
                return False
        return True

    def device(self, limits: Optional[Dict[str, Any]] = None) -> str:
        """``"cpu"``, or the card's spec: PHONE_DEVICE, else the media card."""
        limits = limits if limits is not None else self._limits()
        asked = str(limits.get("phone_device") or "").strip()
        if asked.lower() == "cpu":
            return "cpu"
        return asked or self._media_spec(limits)

    def unavailable_reason(self, rung: Optional[Dict[str, Any]] = None) -> Optional[str]:
        limits = self._limits()
        if not self.own():
            return "PHONE_ENGINE is not own"
        now = self._clock()
        if self.unavailable_until is not None:
            if now < self.unavailable_until:
                return f"paused for {int(self.unavailable_until - now) // 60 + 1} more minute(s) after {FAILURES_BEFORE_COOLDOWN} failures in a row"
            self.unavailable_until, self.failures = None, 0
        if not self.device(limits):
            return "no card for the phone engine (PHONE_DEVICE and MEDIA_GPU are both empty)"
        binary = self.binary()
        if not (os.path.isfile(binary) and os.access(binary, os.X_OK)):
            return f"llama-server is not installed ({binary})"
        rung = rung or self.base_rung()
        if not self.files_present(rung, limits):
            return f"the model files of {rung['name']} are missing from {self.model_dir(limits)}"
        return None

    # ------------------------------------------------------------------ lanes (what routing uses)
    @property
    def loaded(self) -> bool:
        return self.process is not None and self.process.poll() is None

    @property
    def ready(self) -> bool:
        return self.loaded and self._accepting and not self._switching

    @property
    def base_url(self) -> Optional[str]:
        return f"http://127.0.0.1:{self.port}" if self.ready and self.port else None

    def lanes(self) -> int:
        return int(self._limits().get("phone_lanes", 2))

    def lanes_free(self) -> int:
        """Free lanes: 0 while the engine is down, switching, draining or being speed-checked."""
        return max(0, self.lanes() - self._in_flight) if self.ready else 0

    def can_read_photos(self) -> bool:
        """The engine is up for phones and its photo reader is loaded (routing 6b and 6e ask this)."""
        return self.ready and bool(self._limits().get("phone_vision", True))

    def acquire(self) -> bool:
        """Take a lane for one request; False when none is free (the caller waits or overflows)."""
        if self.lanes_free() <= 0:
            return False
        self._in_flight += 1
        return True

    def release(self) -> None:
        self._in_flight = max(0, self._in_flight - 1)

    @contextlib.asynccontextmanager
    async def lane(self) -> AsyncIterator[Optional[str]]:
        """``async with engine.lane() as url:`` url is None when no lane was free."""
        if not self.acquire():
            yield None
            return
        try:
            yield self.base_url
        finally:
            self.release()

    @property
    def in_flight(self) -> int:
        return self._in_flight

    def switch_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock, self._lock_loop = asyncio.Lock(), loop
        return self._lock

    @property
    def switching(self) -> bool:
        return self._switching

    # ------------------------------------------------------------------ status
    def status(self) -> Dict[str, Any]:
        """The ``phone_engine`` block: ``{mode, rung, device, lanes, layers: {home, volunteers}, vision}``."""
        limits = self._limits()
        mode = self.mode()
        if mode != "own":
            return {"mode": "shared", "rung": "coding-engine", "device": "coding",
                    "lanes": int(limits.get("engine_phone_slots", 0)), "layers": {"home": None, "volunteers": 0},
                    "vision": None}
        layout = self.layout or {}
        up = self.loaded and self.rung is not None
        return {"mode": "own", "rung": self.rung["id"] if up else None, "device": self.device(limits) or None,
                "lanes": self.lanes(),
                "layers": {"home": layout.get("home_layers", 0) if up else 0,
                           "volunteers": sum(layout.get("volunteer_layers", {}).values()) if up else 0},
                "vision": bool(limits.get("phone_vision", True)) if up else False}

    # ------------------------------------------------------------------ the command
    def command(self, rung: Dict[str, Any], layout: Dict[str, Any], limits: Dict[str, Any], port: int,
                device: str, no_ffmpeg_dir: str) -> List[str]:
        """The whole llama-server command line. The photo reader is always on the home device; volunteers get layers only."""
        directory = self.model_dir(limits)
        lanes, context = int(limits.get("phone_lanes", 2)), int(limits.get("phone_context", 8192))
        args = [self.binary(), "-m", os.path.join(directory, rung["file"]), "--alias", rung["file"],
                "--host", "127.0.0.1", "--port", str(port), "-c", str(lanes * context), "-np", str(lanes),
                "-fa", "on", "--jinja", "--no-webui"]
        vision = bool(limits.get("phone_vision", True))
        if device == "cpu":
            args += ["--device", "none", "-ngl", "0"]
            if vision:
                args += ["--mmproj", os.path.join(directory, rung["mmproj"]), "--no-mmproj-offload",
                         "--video-ffmpeg-dir", no_ffmpeg_dir]
            return args
        endpoints = [v["endpoint"] for v in layout.get("used", [])]
        devices = ["Vulkan0"] + [f"RPC{i}" for i in range(len(endpoints))]
        args += ["-ngl", "99", "--device", ",".join(devices)]
        if endpoints:
            args += ["--rpc", ",".join(endpoints), "-ts", ",".join(str(n) for n in layout["split"])]
        if vision:
            # Always on the home device, whatever the layers do: a photo never reaches a volunteer.
            args += ["--mmproj", os.path.join(directory, rung["mmproj"]), "--mmproj-device", "Vulkan0",
                     "--video-ffmpeg-dir", no_ffmpeg_dir]
        return args

    # ------------------------------------------------------------------ start and stop
    def _client(self, timeout: float) -> httpx.AsyncClient:
        if self._http is not None:
            return self._http(self.port or 0, timeout)
        # trust_env off: a proxy in the environment must never be given a request for the loopback server.
        return httpx.AsyncClient(base_url=f"http://127.0.0.1:{self.port}", timeout=timeout, trust_env=False)

    async def start(self, rung: Dict[str, Any], volunteers: List[Dict[str, Any]]) -> None:
        """
        Start the engine on ``rung`` with the layers ``plan_layout`` gives the ``volunteers``. Raises :class:`MemberError`.
        Phones are NOT let in: :meth:`open_for_phones` does that, after the switcher's speed check. Any engine
        already running is stopped first.
        """
        limits = self._limits()
        if self.process is not None:
            await self.stop()
        reason = self.unavailable_reason(rung)
        if reason:
            log_warning(f"The phone engine was not started: {reason}.")
            raise MemberError(UNAVAILABLE, "The phone engine isn't available right now.")
        device = self.device(limits)
        host_cpu = device == "cpu"
        layout = plan_layout(rung, volunteers, budget_mb=resolve_budget_mb(limits), lanes=int(limits.get("phone_lanes", 2)),
                             context=int(limits.get("phone_context", 8192)), vision=bool(limits.get("phone_vision", True)),
                             host_cpu=host_cpu)
        if not layout["ok"]:
            log_warning(f"The phone engine was not started on {rung['name']}: {layout['reason']}.")
            raise MemberError(UNAVAILABLE, "That phone model does not fit.")
        if layout["used"] and self._resolve is not None:
            reached = []
            for volunteer in layout["used"]:
                address = await asyncio.to_thread(self._resolve, volunteer["endpoint"])
                if not address or not valid_endpoint(address):
                    log_warning(f"The phone engine was not started on {rung['name']}: a volunteer could not be reached.")
                    raise MemberError(UNAVAILABLE, "A volunteer for the phone model could not be reached.")
                reached.append({**volunteer, "endpoint": address})
            layout = {**layout, "used": reached}
        env = dict(os.environ)
        if not host_cpu:
            card = await asyncio.to_thread(self._card_of, device)
            if card is None:
                raise MemberError(UNAVAILABLE, "The phone engine's card was not found.")
            free, need = card.get("free_gb"), layout["home_mb"] / 1024.0 + 0.1
            if free is None or free < need:
                log_warning(f"The home card has {'unknown' if free is None else f'{free:.1f} GiB'} free; the phone engine needs {need:.1f} GiB.")
                raise MemberError(UNAVAILABLE, "The graphics card doesn't have enough free memory for the phones right now.")
            env["GGML_VK_VISIBLE_DEVICES"] = str(card["vk_index"])
        self.port = self._free_port()
        self._workdir = tempfile.mkdtemp(prefix="dlne-phone-")
        no_ffmpeg = os.path.join(self._workdir, "no-ffmpeg")
        os.makedirs(no_ffmpeg, exist_ok=True)
        run_dir = os.path.join(self._workdir, "run")
        os.makedirs(run_dir, exist_ok=True)
        command = self.command(rung, layout, limits, self.port, device, no_ffmpeg)
        began = self._clock()
        try:
            self.process = self._spawn(command, env=env, cwd=run_dir, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as exc:
            await self.stop()
            self._strike()
            log_error(f"The phone engine could not be started ({type(exc).__name__}).")
            raise MemberError(FAILED, "The phone engine could not be started.") from exc
        stream = getattr(self.process, "stdout", None)
        self._output = StartupOutput(stream) if stream is not None else None
        log_coordinator(f"Phone engine starting {rung['name']} on 127.0.0.1:{self.port} "
                        f"({layout['home_layers']} layers home, {sum(layout['volunteer_layers'].values())} on volunteers).")
        timeout = self._ready_override if self._ready_override is not None else float(limits.get("phone_load_timeout_seconds", 180))
        if not await self._wait_ready(timeout):
            await self._log_failed_start()
            await self.stop()
            self._strike()
            raise MemberError(FAILED, "The phone engine did not start.")
        if self._output is not None:
            self._output.stop_keeping()
        self.rung, self.layout, self.volunteer_ids = rung, layout, [str(v["id"]) for v in layout["used"]]
        self.failures = 0
        log_coordinator(f"Phone engine ready in {self._clock() - began:.0f} s.")

    def _strike(self) -> None:
        self.failures += 1
        if self.failures >= FAILURES_BEFORE_COOLDOWN:
            self.unavailable_until = self._clock() + COOLDOWN_SECONDS
            log_error(f"The phone engine failed {self.failures} times in a row; it is off for {COOLDOWN_SECONDS // 60} minutes.")

    async def _log_failed_start(self) -> None:
        process = self.process
        code = process.poll() if process is not None else None
        how = f"it ended with {exit_text(code)}" if code is not None else "it never answered"
        log_error(f"The phone engine did not become ready ({how}).")
        if self._output is not None:
            lines = await asyncio.to_thread(self._output.tail, STARTUP_LOG_LINES, 1.0 if code is not None else 0.0)
            for line in lines:
                log_error(f"llama-server: {line}")

    async def _wait_ready(self, timeout: float) -> bool:
        """llama-server answers ``GET /health`` 503 while it loads and 200 when it can serve. Bounded by ``timeout``."""
        deadline = self._clock() + timeout
        async with self._client(HEALTH_TIMEOUT_SECONDS) as client:
            while self._clock() < deadline:
                if self.process is None or self.process.poll() is not None:
                    return False
                try:
                    if (await client.get("/health")).status_code == 200:
                        return True
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(self._poll)
        return False

    async def measure_tps(self) -> Optional[float]:
        """A short generation timed by llama-server itself (tokens a second), or None when it could not be measured."""
        if not self.loaded:
            return None
        body = {"prompt": "Count from one to twenty in words: one, two,", "n_predict": SPEED_CHECK_TOKENS,
                "temperature": 0, "cache_prompt": False}
        try:
            async with self._client(SPEED_CHECK_TIMEOUT_SECONDS) as client:
                reply = await asyncio.wait_for(client.post("/completion", json=body), SPEED_CHECK_TIMEOUT_SECONDS + 5)
            timings = reply.json().get("timings") if reply.status_code == 200 else None
            value = float(timings["predicted_per_second"])
        except (httpx.HTTPError, asyncio.TimeoutError, ValueError, KeyError, TypeError, AttributeError):
            self.last_tps = None
            return None
        self.last_tps = value if math.isfinite(value) and value > 0 else None
        return self.last_tps

    def open_for_phones(self) -> None:
        self._accepting = True

    def close_to_phones(self) -> None:
        self._accepting = False

    def process_died(self) -> bool:
        """The child ended on its own (the switcher starts the base again)."""
        return self.process is not None and self.process.poll() is not None

    async def _drain(self, seconds: float) -> bool:
        """Wait (bounded) for every request in flight to finish. New ones are already refused. True when none is left."""
        deadline = self._clock() + seconds
        while self._in_flight > 0:
            if self._clock() >= deadline:
                return False
            await asyncio.sleep(self._poll)
        return True

    async def switch(self, rung: Dict[str, Any], volunteers: List[Dict[str, Any]], *, drain_seconds: float = 60.0,
                     abort_if_busy: bool = False, check_speed: bool = False, restore: Optional[Dict[str, Any]] = None,
                     min_tps: Optional[float] = None) -> SwapResult:
        """
        The switcher's one entry. New requests wait (the engine stops taking lanes), requests in flight get
        ``drain_seconds``; if they are not done and ``abort_if_busy``, nothing is touched ("busy"). Otherwise the engine
        restarts on ``rung``. A move that came up is speed-checked before phones are let in; slower than ``min_tps`` (else
        PHONE_MIN_TPS), or not starting at all, puts the BASE rung back alone ("failed").
        """
        limits = self._limits()
        floor = float(limits.get("phone_min_tps", 6.0)) if min_tps is None else float(min_tps)
        async with self.switch_lock():
            self._switching = True
            was_accepting = self._accepting
            self._accepting = False
            try:
                if not await self._drain(drain_seconds):
                    if abort_if_busy:
                        self._accepting = was_accepting
                        return SwapResult("busy", "phones are still being answered")
                    log_warning("The phone engine is switched with answers still running; they are cut short.")
                try:
                    await self.start(rung, volunteers)
                except MemberError as exc:
                    return await self._put_base_back(restore, f"{rung['name']} did not start ({exc.detail})")
                if check_speed and floor > 0:
                    tps = await self.measure_tps()
                    if tps is None or tps < floor:
                        shown = "could not be measured" if tps is None else f"{tps:.1f} tokens a second"
                        return await self._put_base_back(restore, f"{rung['name']} {shown}, under the floor of {floor:g}")
                self._accepting = True
                return SwapResult("swapped", f"now on {rung['name']}")
            finally:
                self._switching = False

    async def _put_base_back(self, restore: Optional[Dict[str, Any]], why: str) -> SwapResult:
        base = restore or self.base_rung()
        try:
            await self.start(base, [])
            self._accepting = True
            return SwapResult("failed", f"{why}; {base['name']} is back on the home card alone")
        except MemberError as exc:
            self._accepting = False
            return SwapResult("failed", f"{why}; and {base['name']} could not be started either ({exc.detail})")

    async def stop(self) -> None:
        """Stop llama-server (terminate, kill after 5 s) and remove its directory. Safe to repeat."""
        process, workdir = self.process, self._workdir
        self.process, self.port, self._workdir = None, None, None
        self.rung, self.layout, self.volunteer_ids, self._accepting = None, None, [], False
        output, self._output = self._output, None
        if process is not None and process.poll() is None:
            await asyncio.to_thread(self._end, process)
        if output is not None:
            await asyncio.to_thread(output.join)
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)

    @staticmethod
    def _end(process: Any) -> None:
        try:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        except Exception:      # noqa: BLE001 - already gone, or not ours to wait for
            pass

    def stop_now(self) -> None:
        """For the coordinator's shutdown: :meth:`stop` without an event loop."""
        process, workdir = self.process, self._workdir
        self.process, self.port, self._workdir = None, None, None
        self.rung, self.layout, self.volunteer_ids, self._accepting = None, None, [], False
        output, self._output = self._output, None
        if process is not None and process.poll() is None:
            self._end(process)
        if output is not None:
            output.join()
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)
