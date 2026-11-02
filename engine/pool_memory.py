"""
The coding pool's two kinds of memory (2.9.0): GPU memory and lent system RAM, counted apart.

A mixture-of-experts model uses a few billion parameters per token, so most of its weight (the experts) can
sit in system RAM and be read by the CPU at a usable speed. The pool therefore reports two numbers and a rung
(scripts/coding_models.json) asks for two: ``min_total_gb`` (GPU + lent RAM) and ``min_gpu_gb`` (attention,
shared weights, the KV cache). See docs/release-2.9-contract.md sections 1 and 2.

WHO LENDS RAM
    This host (TheWall): the DLNE service's memory budget (``POOL_WALL_RAM_BUDGET_GB``, its systemd
    MemoryMax, 18 GB) less what the engine needs besides experts (``POOL_WALL_RAM_RESERVE_GB``, 10 GB: the
    6 GB prompt cache, the token embedding, runtime buffers, Python). That is 8 GB, the room today's
    ``--n-cpu-moe`` placement uses. It is counted when this host has a GPU in the pool.
    A volunteer: the ``ram_lent_gb`` its heartbeat reports in ``hardware`` (0 when absent: an older worker),
    never more than its RAM less 4 GB it keeps for itself (the coordinator does not take a worker's word),
    and only while ALL of these hold:
      * its GPU already counts as near and docked (the GPU rules of engine/auto_swap.py),
      * its round trip is at most ``POOL_RAM_MAX_RTT_MS`` (5 ms: the same LAN or a fast WireGuard hop; every
        token's activations cross the link once per expert layer, so this is stricter than the 80 ms GPU rule),
      * it has stayed ``POOL_RAM_MIN_STAY_SECONDS`` (30 minutes) in the pool, so a laptop that joins for a
        few minutes never unlocks a rung that depends on its RAM. It stops counting the moment it leaves
        or stops being near, and the rung it carried steps down at once (``engine_short``, as for GPUs).
"""

import math
from typing import Any, Dict, List, Optional

DEFAULT_WALL_RAM_BUDGET_GB = 18.0
DEFAULT_WALL_RAM_RESERVE_GB = 10.0
DEFAULT_RAM_MAX_RTT_MS = 5.0
DEFAULT_RAM_MIN_STAY_SECONDS = 1800.0
VOLUNTEER_KEEPS_GB = 4.0


def _number(value: Any) -> float:
    """A finite, non-negative float, else 0.0 (booleans, text and NaN are not numbers here)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    value = float(value)
    return value if math.isfinite(value) and value > 0 else 0.0


def wall_lent_ram_gb(limits: Dict[str, Any]) -> float:
    """The RAM this host lends: the service's budget less the engine's other needs, never negative."""
    budget = limits.get("pool_wall_ram_budget_gb", DEFAULT_WALL_RAM_BUDGET_GB)
    reserve = limits.get("pool_wall_ram_reserve_gb", DEFAULT_WALL_RAM_RESERVE_GB)
    return max(0.0, _number(budget) - _number(reserve))


def volunteer_lent_ram_gb(hardware: Any) -> float:
    """
    The RAM a volunteer lends according to its heartbeat (``hardware["ram_lent_gb"]``), clamped to its own RAM
    less VOLUNTEER_KEEPS_GB. 0 when it reports none (an older worker) or something that is not a number.
    """
    if not isinstance(hardware, dict):
        return 0.0
    reported = _number(hardware.get("ram_lent_gb"))
    own = _number(hardware.get("ram_gb"))
    cpu = cpu_endpoint_ram(hardware)
    if cpu:       # a worker that lists its rpc-servers lends at most what its cpu entries say
        reported = min(reported, sum(cpu.values()))
    return max(0.0, min(reported, own - VOLUNTEER_KEEPS_GB))


def cpu_endpoint_ram(hardware: Any) -> Dict[int, float]:
    """``{port: ram_gb}`` of the cpu rpc-servers a heartbeat lists in ``hardware["endpoints"]`` (builder W): the
    RAM lent on each. The server advertises the machine's WHOLE RAM, so this number is the only cap there is."""
    out: Dict[int, float] = {}
    entries = hardware.get("endpoints") if isinstance(hardware, dict) else None
    for entry in entries if isinstance(entries, list) else ():
        if isinstance(entry, dict) and entry.get("type") == "cpu" and isinstance(entry.get("port"), int) \
                and not isinstance(entry.get("port"), bool):
            out[entry["port"]] = _number(entry.get("ram_gb"))
    return out


def build_pool_memory(workers: List[Dict[str, Any]], *, wall_ram_gb: float, ram_max_rtt_ok, min_stay_seconds: float) -> Dict[str, Any]:
    """
    The pool as ``{gpu_gb, ram_gb, total_gb, machines, wall_ram_gb, volunteer_ram_gb, by_machine}``.

    ``workers`` are the GPU workers that already count as near (the caller applies the GPU rules), each
    ``{"id", "vram_gb", "local": bool, "lent_ram_gb": float, "stay_seconds": float}``.
    ``ram_max_rtt_ok(worker) -> bool`` is the caller's strict round-trip test for RAM. ``wall_ram_gb`` is added
    once, when any local worker is present. ``machines`` is how many machines contribute either number.
    """
    gpu = 0.0
    volunteer_ram = 0.0
    host_present = False
    by_machine: List[Dict[str, Any]] = []
    for worker in workers:
        vram = _number(worker.get("vram_gb"))
        local = bool(worker.get("local"))
        host_present = host_present or local
        counted = 0.0
        pending: Optional[float] = None
        if not local:
            lent = _number(worker.get("lent_ram_gb"))
            if lent > 0:
                try:
                    close = bool(ram_max_rtt_ok(worker))
                except Exception:  # noqa: BLE001 - unknown is far
                    close = False
                stay = _number(worker.get("stay_seconds"))
                if close and stay >= min_stay_seconds:
                    counted = lent
                elif close:
                    pending = max(0.0, min_stay_seconds - stay)
        gpu += vram
        volunteer_ram += counted
        entry = {"id": str(worker.get("id") or "")[:8], "gpu_gb": round(vram, 2), "ram_gb": round(counted, 2)}
        if pending is not None:
            entry["ram_pending_seconds"] = int(math.ceil(pending))
        if vram > 0 or counted > 0:
            by_machine.append(entry)
    wall = _number(wall_ram_gb) if host_present else 0.0
    if wall > 0 and not any(w.get("local") for w in workers if _number(w.get("vram_gb")) > 0):
        wall = 0.0       # a host with no GPU in the pool lends nothing: there is no engine of ours to give RAM to
    ram = wall + volunteer_ram
    return {
        "gpu_gb": round(gpu, 2),
        "ram_gb": round(ram, 2),
        "total_gb": round(gpu + ram, 2),
        "machines": len(by_machine),
        "wall_ram_gb": round(wall, 2),
        "volunteer_ram_gb": round(volunteer_ram, 2),
        "by_machine": by_machine,
    }
