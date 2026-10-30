"""
The smart sliders: what this computer can give, what each setting powers, and the config.txt keys that carry it.

Everything here is pure (no window, no network, no files), so it is tested without any of them. The rules come from the
code the swarm itself runs:

* a GPU of at least 8 GB VRAM, with system RAM beside it for the model's experts, becomes a coding member
  (engine/coding_member_select.py ``candidate_tiers``; ``MIN_VRAM_GB``); a smaller GPU joins the GPU pool;
* a CPU with at least 4 physical cores, AVX2 and 8 GB of RAM lent becomes a phone-app member, and the RAM lent decides
  the model (the signed ``app_tiers`` of the release, and ``app_pool.tiers`` in the cluster status);
* never more than half the installed RAM is offered by default (the VS Code extension's rule); the owner may drag up to
  the installed RAM minus 4 GB, with a warning;
* threads decide how much of the PC stays free: the default is the physical cores minus 2, at least 4.
"""

import re
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import engine_flavour
from .configfile import format_gb

# The swarm's floors for a CPU member (engine/config.py cpu_min_*) and for a coding member.
CPU_MIN_RAM_GB = 8
CPU_MIN_CORES = 4
CODING_MIN_VRAM_GB = 8.0
MAX_LEND_SHARE = 0.5          # offered by default
KEEP_FREE_GB = 4              # the owner's computer keeps at least this, whatever they drag
MIN_GPU_RAM_GB = 4
GPU_POOL_RAM_GB = 4           # the RAM a GPU-pool share may use (the extension's default --max-ram)

# What the CPU pool serves by the RAM lent, when the swarm cannot be asked (contribution.ts CPU_LADDER).
DEFAULT_APP_TIERS: Tuple[Dict[str, Any], ...] = (
    {"id": "qwen3-4b", "name": "Qwen3 4B Instruct 2507", "min_ram_gb": 8, "unlocked": True},
    {"id": "qwen36-a3b", "name": "Qwen3.6 35B-A3B", "min_ram_gb": 16, "unlocked": True},
    {"id": "qwen36-a3b-q4", "name": "Qwen3.6 35B-A3B (full quality)", "min_ram_gb": 24, "unlocked": False},
)

_STANDARD_RAM_GB = (2, 3, 4, 6, 8, 12, 16, 18, 20, 24, 32, 36, 40, 48, 64, 96, 128, 192, 256, 384, 512, 768, 1024)
_UNIFIED_MEMORY = re.compile(r"\bapple\b|radeon(?:\W|tm)*80[456]0s\b|gfx1151|strix[ _]halo|ryzen\W*ai\W*max")


# --------------------------------------------------------------------------------------------------
# The machine
# --------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Hardware:
    gpu_name: str = ""
    vram_gb: float = 0.0           # 0: no GPU that counts (a discrete one, or one of the few integrated ones the swarm takes)
    gpu_count: int = 0
    total_ram_gb: float = 0.0      # as sold (see installed_ram_gb)
    physical_cores: int = 1
    logical_threads: int = 1
    avx2: bool = True
    integrated_gpu: str = ""       # an integrated GPU that does not count, named so the window can say why

    @property
    def has_gpu(self) -> bool:
        return self.vram_gb > 0 and self.gpu_count > 0

    @property
    def unified(self) -> bool:
        return bool(_UNIFIED_MEMORY.search(self.gpu_name.lower()))

    @property
    def cpu_meets_minimum(self) -> bool:
        return self.physical_cores >= CPU_MIN_CORES and self.avx2


def installed_ram_gb(total_bytes: float) -> int:
    """
    The RAM a machine was sold with, from the bytes the OS reports. The OS reads a little under it (firmware and an
    integrated GPU keep some: a 32 GB desktop reports about 31, a 16 GB laptop about 15.5), which would put such a
    machine a rung too low. The smallest standard size that the reading is at least 88% of; otherwise rounded.
    """
    gib = total_bytes / 1024 ** 3
    for size in _STANDARD_RAM_GB:
        if gib <= size and gib >= 0.88 * size:
            return size
    return int(round(gib))


def default_threads(physical_cores: int, logical_threads: int) -> int:
    """Physical cores minus 2, at least 4, never more than the machine has."""
    return max(1, min(max(4, physical_cores - 2), max(logical_threads, 1)))


def thread_bounds(hw: Hardware) -> Tuple[int, int]:
    return 1, max(1, hw.logical_threads)


# --------------------------------------------------------------------------------------------------
# What the swarm can use (from the signed release and the live cluster status)
# --------------------------------------------------------------------------------------------------

def short_model_name(name: str) -> str:
    """'Qwen3 4B Instruct 2507' -> 'Qwen3 4B', 'Qwen3.6 35B-A3B (full quality)' -> 'full quality', 'Qwen3.6 35B-A3B' -> 'Qwen3.6'."""
    paren = re.search(r"\(([^)]+)\)\s*$", name)
    if paren:
        return paren.group(1).strip()
    cleaned = re.sub(r"\s+Instruct\b.*$", "", name, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+\d+(?:\.\d+)?B-A\d+(?:\.\d+)?B\b", "", cleaned)
    return cleaned.strip() or name


def app_tiers_from_status(status: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The CPU pool's tiers from ``/v1/cluster/status`` (``app_pool.tiers``), smallest first; the built-in ladder if absent."""
    tiers: List[Dict[str, Any]] = []
    pool = status.get("app_pool") if isinstance(status, dict) else None
    raw = pool.get("tiers") if isinstance(pool, dict) else None
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        ram = entry.get("min_ram_gb")
        if isinstance(ram, (int, float)) and not isinstance(ram, bool) and ram > 0 and isinstance(entry.get("name"), str):
            tiers.append({"id": str(entry.get("id") or ""), "name": entry["name"], "min_ram_gb": float(ram),
                          "unlocked": bool(entry.get("unlocked"))})
    tiers.sort(key=lambda t: t["min_ram_gb"])
    return tiers or [dict(t) for t in DEFAULT_APP_TIERS]


@dataclass(frozen=True)
class RamStop:
    gb: int
    title: str           # "16 GB"
    runs: str            # "Qwen3.6"
    beyond_half: bool    # more than half the installed RAM: allowed, with a warning


def cpu_stops(hw: Hardware, tiers: Sequence[Dict[str, Any]]) -> List[RamStop]:
    """The RAM amounts the CPU slider snaps to: each tier's RAM, up to the installed RAM minus 4 GB."""
    ceiling = hw.total_ram_gb - KEEP_FREE_GB
    stops: List[RamStop] = []
    for tier in tiers:
        gb = int(round(tier["min_ram_gb"]))
        if gb < CPU_MIN_RAM_GB or gb > ceiling:
            continue
        stops.append(RamStop(gb=gb, title=f"{gb} GB", runs=short_model_name(tier["name"]),
                             beyond_half=gb > hw.total_ram_gb * MAX_LEND_SHARE))
    return stops


def gpu_ram_bounds(hw: Hardware) -> Tuple[int, int, int]:
    """(lowest, most offered by default, most allowed) GB of RAM that goes with the GPU."""
    hard = int(hw.total_ram_gb - KEEP_FREE_GB)
    soft = int(hw.total_ram_gb * MAX_LEND_SHARE)
    return min(MIN_GPU_RAM_GB, max(hard, 1)), max(min(soft, hard), 1), max(hard, 1)


def gpu_vram_bounds(hw: Hardware) -> Tuple[float, float]:
    return (1.0, hw.vram_gb) if hw.has_gpu else (0.0, 0.0)


# --------------------------------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Settings:
    gpu_enabled: bool = False
    gpu_vram_gb: float = 0.0
    gpu_ram_gb: float = 16.0
    cpu_enabled: bool = False
    cpu_ram_gb: float = 8.0
    threads: int = 4
    resume_donating: bool = False       # start donating when the app starts (off: donating is always the owner's click)
    lend_notice_seen: bool = False      # the owner has been told what lending a GPU means
    # 2.7.0, the engine flavour (engine_flavour.py): auto | vulkan | cuda, and the answer to the CUDA question
    # (yes | never | later | unasked; for "later" the epoch milliseconds when it is asked again).
    engine: str = "auto"
    cuda_consent: str = "unasked"
    cuda_later_ms: int = 0
    auto_update: bool = False           # "Update automatically" (4a): install a newer DLNE Donate at the next idle moment; off by default


def default_settings(hw: Hardware, tiers: Sequence[Dict[str, Any]]) -> Settings:
    """
    What a machine starts with: a GPU that can be a coding member is lent (all its VRAM, half the RAM, up to 24 GB); a
    machine without one lends its CPU at the biggest stop within half its RAM. Nothing runs until the owner clicks Donate.
    """
    _low, soft, _hard = gpu_ram_bounds(hw)
    stops = cpu_stops(hw, tiers)
    within_half = [s for s in stops if not s.beyond_half]
    cpu_ram = float((within_half[-1] if within_half else stops[0]).gb) if stops else float(CPU_MIN_RAM_GB)
    gpu_on = hw.has_gpu
    cpu_on = (not hw.has_gpu) and hw.cpu_meets_minimum and bool(within_half)
    return Settings(
        gpu_enabled=gpu_on,
        gpu_vram_gb=hw.vram_gb if gpu_on else 0.0,
        gpu_ram_gb=float(min(24, soft)),
        cpu_enabled=cpu_on,
        cpu_ram_gb=cpu_ram,
        threads=default_threads(hw.physical_cores, hw.logical_threads),
    )


def _number(value: Any, fallback: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or abs(value) == float("inf"):
        return fallback
    return float(value)


def settings_from_json(data: Any, hw: Hardware, tiers: Sequence[Dict[str, Any]]) -> Settings:
    """Settings read from the JSON file: anything missing, mistyped or out of range falls back to the default / the nearest allowed value."""
    base = default_settings(hw, tiers)
    if not isinstance(data, dict):
        return base
    def flag(key: str, fallback: bool) -> bool:
        return data[key] if isinstance(data.get(key), bool) else fallback
    gpu = data.get("gpu") if isinstance(data.get("gpu"), dict) else {}
    cpu = data.get("cpu") if isinstance(data.get("cpu"), dict) else {}
    loaded = Settings(
        gpu_enabled=gpu["enabled"] if isinstance(gpu.get("enabled"), bool) else base.gpu_enabled,
        gpu_vram_gb=_number(gpu.get("vram_gb"), base.gpu_vram_gb),
        gpu_ram_gb=_number(gpu.get("ram_gb"), base.gpu_ram_gb),
        cpu_enabled=cpu["enabled"] if isinstance(cpu.get("enabled"), bool) else base.cpu_enabled,
        cpu_ram_gb=_number(cpu.get("ram_gb"), base.cpu_ram_gb),
        threads=int(_number(data.get("threads"), base.threads)),
        resume_donating=flag("resume_donating", False),
        lend_notice_seen=flag("lend_notice_seen", False),
        engine=engine_flavour.clean_engine(data.get("engine")),
        auto_update=flag("auto_update", False),
    )
    consent, later_ms = engine_flavour.consent_from_json(data.get("cuda_consent"))
    return clamp_settings(replace(loaded, cuda_consent=consent, cuda_later_ms=later_ms), hw, tiers)


def settings_to_json(settings: Settings) -> Dict[str, Any]:
    return {
        "version": 1,
        "gpu": {"enabled": settings.gpu_enabled, "vram_gb": settings.gpu_vram_gb, "ram_gb": settings.gpu_ram_gb},
        "cpu": {"enabled": settings.cpu_enabled, "ram_gb": settings.cpu_ram_gb},
        "threads": settings.threads,
        "resume_donating": settings.resume_donating,
        "lend_notice_seen": settings.lend_notice_seen,
        "engine": settings.engine,
        "cuda_consent": engine_flavour.consent_to_json(settings.cuda_consent, settings.cuda_later_ms),
        "auto_update": settings.auto_update,
    }


def snap_cpu_ram(gb: float, hw: Hardware, tiers: Sequence[Dict[str, Any]]) -> float:
    """The nearest stop to ``gb`` (the stops are the only amounts the CPU slider offers); 8 when the machine has none."""
    stops = cpu_stops(hw, tiers)
    if not stops:
        return float(CPU_MIN_RAM_GB)
    return float(min(stops, key=lambda s: abs(s.gb - gb)).gb)


def clamp_settings(settings: Settings, hw: Hardware, tiers: Sequence[Dict[str, Any]]) -> Settings:
    """Settings this machine can honour: no GPU means no GPU lending, amounts inside the sliders' ranges, whole-GB RAM."""
    low, _soft, hard = gpu_ram_bounds(hw)
    vram_low, vram_high = gpu_vram_bounds(hw)
    gpu_on = settings.gpu_enabled and hw.has_gpu
    cpu_on = settings.cpu_enabled and hw.cpu_meets_minimum and bool(cpu_stops(hw, tiers))
    vram = min(max(settings.gpu_vram_gb or vram_high, vram_low), vram_high) if hw.has_gpu else 0.0
    ram = float(min(max(int(round(settings.gpu_ram_gb)), low), hard))
    lo_t, hi_t = thread_bounds(hw)
    consent, later_ms = engine_flavour.consent_from_json(
        {"later": settings.cuda_later_ms} if settings.cuda_consent == "later" else settings.cuda_consent)
    return replace(settings, gpu_enabled=gpu_on, gpu_vram_gb=round(vram, 1), gpu_ram_gb=ram, cpu_enabled=cpu_on,
                   cpu_ram_gb=snap_cpu_ram(settings.cpu_ram_gb, hw, tiers), threads=min(max(settings.threads, lo_t), hi_t),
                   engine=engine_flavour.clean_engine(settings.engine), cuda_consent=consent, cuda_later_ms=later_ms)


# --------------------------------------------------------------------------------------------------
# What a setting powers (the live line under the sliders)
# --------------------------------------------------------------------------------------------------

@dataclass
class Description:
    gpu_line: str = ""
    cpu_line: str = ""
    threads_line: str = ""
    warnings: List[str] = field(default_factory=list)
    disk_gb: float = 0.0               # the model a coding member would download, kept on disk
    can_donate: bool = False
    blocker: str = ""                  # why Donate is disabled, when it is
    coding_tier: str = ""              # the tier the estimate picked ("" when none)
    cpu_usable: bool = False           # the CPU could be lent on this computer (cores, AVX2 and enough RAM), switched on or not
    machine_blocker: str = ""          # why this computer cannot donate at all, whatever is switched on ("" when it can)


def _gib(value_bytes: float) -> float:
    return value_bytes / 1024 ** 3


def _too_little_ram(hw: Hardware) -> str:
    return (f"Too little RAM for the CPU to help: this computer has {hw.total_ram_gb:g} GB, and lending the smallest share "
            f"({CPU_MIN_RAM_GB} GB) must leave {KEEP_FREE_GB} GB for itself, so it needs at least {CPU_MIN_RAM_GB + KEEP_FREE_GB} GB.")


def _cannot_donate(hw: Hardware) -> str:
    """The one sentence for a computer that can lend nothing: no GPU the swarm can use, and a CPU it cannot use either."""
    gpu = ("it has no GPU the swarm can use" if not hw.integrated_gpu
           else f"its only GPU ({hw.integrated_gpu}) is an integrated one the swarm does not take")
    if not hw.cpu_meets_minimum:
        cpu = (f"its CPU has {hw.physical_cores} physical cores{'' if hw.avx2 else ' and no AVX2'}, and the swarm needs at least "
               f"{CPU_MIN_CORES} cores and AVX2")
    else:
        cpu = (f"it has {hw.total_ram_gb:g} GB of RAM, and a CPU that lends {CPU_MIN_RAM_GB} GB must leave {KEEP_FREE_GB} GB "
               f"for the computer itself ({CPU_MIN_RAM_GB + KEEP_FREE_GB} GB needed)")
    return f"This computer can't donate: {gpu}, and {cpu}."


def estimate_coding(settings: Settings, hw: Hardware, coding_tiers: Sequence[Dict[str, Any]],
                    select: Any) -> Optional[Dict[str, Any]]:
    """
    ``{"tier", "decode_tps", "prefill_tps", "verdict"}`` for the tier the worker would run with these limits, or None.
    ``select`` is engine.coding_member_select (the snapshot built into the app). The figures the worker gives it are the
    GPU memory it may use and the RAM it may use; here those are the two sliders.
    """
    if select is None or not coding_tiers:
        return None
    sized = [t for t in coding_tiers if isinstance(t, dict) and (t.get("files") or t.get("size"))]
    if not sized:
        return None
    ram = min(settings.gpu_ram_gb, hw.total_ram_gb)
    extra = {"unified": hw.unified, "gpu_count": max(1, hw.gpu_count)}
    found = select.candidate_tiers(settings.gpu_vram_gb, ram, sized, {}, **extra)
    if not found:
        return None
    best = found[0]
    estimates = {e.tier_id: e for e in select.assess_tiers(settings.gpu_vram_gb, ram, sized, {}, **extra)}
    est = estimates.get(best["id"])
    return {"tier": best, "decode_tps": est.decode_tps if est else 0.0, "prefill_tps": est.prefill_tps if est else 0.0,
            "verdict": est.verdict if est else ""}


def describe(settings: Settings, hw: Hardware, coding_tiers: Sequence[Dict[str, Any]], app_tiers: Sequence[Dict[str, Any]],
             select: Any = None, app_sizes: Optional[Dict[str, int]] = None) -> Description:
    """What the current sliders power, in the owner's words, and whether Donate may be pressed."""
    out = Description()
    powers = 0
    ram_total = hw.total_ram_gb
    cpu_stops_now = cpu_stops(hw, app_tiers)
    out.cpu_usable = hw.cpu_meets_minimum and bool(cpu_stops_now)
    if not hw.has_gpu and not out.cpu_usable:
        out.machine_blocker = _cannot_donate(hw)

    if settings.gpu_enabled and hw.has_gpu:
        if settings.gpu_vram_gb < CODING_MIN_VRAM_GB - 0.5:
            out.gpu_line = (f"Below what a coding member needs ({CODING_MIN_VRAM_GB:g} GB of VRAM): your GPU can still join the "
                            "GPU pool and hold a share of one big model that the swarm runs together on many volunteers' GPUs.")
            powers += 1
        else:
            est = estimate_coding(settings, hw, coding_tiers, select)
            if est:
                tier = est["tier"]
                out.coding_tier = tier["id"]
                speed = f", about {est['decode_tps']:.0f} tokens per second" if est["decode_tps"] > 0 else ""
                note = "" if est["verdict"] == "good" else " (a tight fit: the swarm tests it first and may pick a smaller model)"
                out.gpu_line = f"Powers a coding member: your GPU runs {tier['name']} by itself and answers other people's coding requests{speed}.{note}"
                size = tier.get("size") or sum(p.get("size", 0) for p in tier.get("files", []))
                out.disk_gb = round(_gib(float(size)), 1) if size else 0.0
                powers += 1
            elif not coding_tiers:
                out.gpu_line = ("Powers a coding member if the amount of RAM beside the GPU is enough (about 16 to 24 GB): "
                                "your GPU runs a whole coding model and answers other people's coding requests.")
                powers += 1
            else:
                out.gpu_line = ("Not enough RAM beside the GPU for a whole coding model (it needs about 16 to 24 GB): "
                                "your GPU joins the GPU pool instead and holds a share of one big model run together with other volunteers.")
                powers += 1
    elif hw.has_gpu:
        out.gpu_line = "Off: your GPU stays yours."
    elif hw.integrated_gpu:
        out.gpu_line = (f"No GPU the swarm can use: {hw.integrated_gpu} is an integrated GPU, and only a discrete GPU "
                        "(or Intel Arc 140V/140T, AMD Strix Halo) counts. Your CPU can still help.")
    else:
        out.gpu_line = "No GPU found on this computer." + (" Your CPU can still help." if out.cpu_usable else "")

    if settings.cpu_enabled:
        stops = cpu_stops_now
        chosen = None
        for tier in app_tiers:
            if tier["min_ram_gb"] <= settings.cpu_ram_gb + 1e-9:
                chosen = tier
        if not hw.cpu_meets_minimum:
            out.cpu_line = (f"Below what the swarm can use: the phone app needs at least {CPU_MIN_CORES} physical cores and AVX2 "
                            f"(this CPU has {hw.physical_cores} cores{'' if hw.avx2 else ', no AVX2'}).")
        elif chosen is None or not stops:
            out.cpu_line = f"Below what the swarm can use: the phone app needs at least {CPU_MIN_RAM_GB} GB of RAM lent."
        else:
            name = short_model_name(chosen["name"])
            extra = "" if chosen.get("unlocked", True) else " (the swarm switches this level on once enough volunteers can host it)"
            out.cpu_line = f"Powers the DLNE phone app: your computer runs {name} and answers questions from phones{extra}."
            powers += 1
            if app_sizes and chosen.get("id") in app_sizes and not out.disk_gb:
                out.disk_gb = round(_gib(float(app_sizes[chosen["id"]])), 1)
    elif hw.has_gpu or out.cpu_usable:
        out.cpu_line = "Off."
    elif not hw.cpu_meets_minimum:
        out.cpu_line = (f"This CPU is below what the swarm can use (at least {CPU_MIN_CORES} physical cores and AVX2).")
    else:
        out.cpu_line = _too_little_ram(hw)

    ask = (settings.gpu_ram_gb if settings.gpu_enabled and hw.has_gpu else 0) + (settings.cpu_ram_gb if settings.cpu_enabled else 0)
    if ask > ram_total - KEEP_FREE_GB + 1e-9:
        out.warnings.append(f"Together the two ask for {ask:g} GB of your {ram_total:g} GB of RAM. Lower one so your own programs keep at least {KEEP_FREE_GB} GB.")
    if settings.gpu_enabled and hw.has_gpu and settings.gpu_ram_gb > ram_total * MAX_LEND_SHARE + 1e-9:
        out.warnings.append(f"{settings.gpu_ram_gb:g} GB is more than half of your RAM: your own programs may slow down while you donate.")
    if settings.cpu_enabled and settings.cpu_ram_gb > ram_total * MAX_LEND_SHARE + 1e-9:
        out.warnings.append(f"{settings.cpu_ram_gb:g} GB is more than half of your RAM: your own programs may slow down while you donate.")

    spare = hw.logical_threads - settings.threads
    if settings.threads >= hw.logical_threads:
        out.threads_line = "Uses every thread: your computer may feel slow while you donate."
    else:
        out.threads_line = f"Uses {settings.threads} of {hw.logical_threads} threads, so about {spare} stay free for you."

    out.can_donate = powers > 0
    if not out.can_donate:
        if out.machine_blocker:
            out.blocker = out.machine_blocker
        elif not settings.gpu_enabled and not settings.cpu_enabled:
            out.blocker = "Turn on the GPU or the CPU to donate."
        else:
            out.blocker = "What is switched on is below what the swarm can use. Raise it, or switch on something else."
    return out


# --------------------------------------------------------------------------------------------------
# config.txt
# --------------------------------------------------------------------------------------------------

def config_updates(settings: Settings, hw: Hardware, coordinator_url: str, network_key: str) -> Dict[str, str]:
    """
    The engine's config.txt keys for these settings (engine/config.py ``get_worker_limits``); the worker re-reads the file
    while it runs, so writing it again is how a slider change takes effect.

    GPU on, CPU off: the GPU is lent with its VRAM and RAM limits. CPU on, GPU off: no GPU is visible to the worker, so the
    machine is a phone-app member with the RAM lent. Both: the GPU does the job the swarm gives it and the CPU also serves
    the phone app with its own RAM (``ALSO_CPU_RAM_GB``).
    """
    gpu = settings.gpu_enabled and hw.has_gpu
    cpu = settings.cpu_enabled
    updates: Dict[str, str] = {
        "COORDINATOR_URL": coordinator_url,
        "NETWORK_KEY": network_key,
        "MAX_CPU_THREADS": str(int(settings.threads)),
        "CODING_MEMBER_ALLOWED": "true" if gpu else "false",
    }
    if gpu:
        updates["ALLOWED_GPU_INDICES"] = "-1"
        updates["MAX_VRAM_GB"] = format_gb(settings.gpu_vram_gb)
        # MAX_RAM_GB is what the GPU pool's share of a big model may use (the extension's default is the same 4 GB:
        # a bigger figure would make the swarm plan more of that model into this computer's RAM); the RAM slider is
        # the coding job's own limit.
        updates["MAX_RAM_GB"] = format_gb(min(GPU_POOL_RAM_GB, settings.gpu_ram_gb))
        updates["CODING_MAX_RAM_GB"] = format_gb(settings.gpu_ram_gb)
        updates["ALSO_CPU_RAM_GB"] = format_gb(settings.cpu_ram_gb) if cpu else "0"
    else:
        updates["ALLOWED_GPU_INDICES"] = "none"
        updates["MAX_VRAM_GB"] = "-1"
        updates["MAX_RAM_GB"] = format_gb(settings.cpu_ram_gb)     # the RAM a phone-app member lends picks its model
        updates["CODING_MAX_RAM_GB"] = "-1"
        updates["ALSO_CPU_RAM_GB"] = "0"
    return updates


def device_summary(settings: Settings, hw: Hardware) -> str:
    gpu = settings.gpu_enabled and hw.has_gpu
    if gpu and settings.cpu_enabled:
        return "GPU and CPU"
    if gpu:
        return "GPU"
    if settings.cpu_enabled:
        return "CPU"
    return "nothing"
