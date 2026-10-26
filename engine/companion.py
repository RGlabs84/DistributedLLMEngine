"""
"Both": a GPU machine that lends its CPU to the phone pool at the same time (docs/coding-members.md section 1).

The GPU does whatever job the coordinator gives it (an RPC node for the shared model, or a coding member). The CPU
serves the phone pool through a **companion node**: a second worker identity, ``<worker id>-cpu``, run as a thread of
the same process (engine/worker.py, ``WorkerNode(companion_of=...)``). It heartbeats and tunnels like any CPU pool
member, with ``companion_of`` in its hardware profile so the coordinator counts the machine once. Every node still has
exactly one job, so nothing about roles changes, and a coordinator that has never heard of companions sees two
ordinary nodes.

Turned on by ``ALSO_CPU_RAM_GB`` in config.txt (``dlne_server.py worker --also-cpu-ram <gb>``): the RAM the phone-pool
member lends. This file holds the small pieces that are about sharing one machine between the two jobs.
"""

from typing import Any, Dict, Optional, Tuple

# The line the worker prints for the VS Code extension beside DLNE_ROLE: "DLNE_ALSO: app" while the CPU also serves
# the phone pool, "DLNE_ALSO: none" when it stops. (DLNE_ROLE is the GPU's job and keeps meaning only that.)
ALSO_MARKER = "DLNE_ALSO: "

# The companion's id is the primary's, with this suffix.
COMPANION_SUFFIX = "-cpu"

# How long the coding model waits for the phone-pool model to settle (loaded, or given up on) before it loads anyway.
GATE_TIMEOUT_SECONDS = 20 * 60.0


def also_cpu_ram_gib(limits: Dict[str, Any]) -> Optional[float]:
    """The RAM, in GiB, the phone-pool member of a "both" machine lends; None when "both" is off (the key is
    absent, zero, negative or not a number)."""
    value = limits.get("also_cpu_ram_gb")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value > 0 else None


def default_pool_threads(physical_cores: int) -> int:
    """What a phone-pool member runs on by itself: half the machine, 2 to 8 threads (engine/app_member.py)."""
    return max(2, min(8, int(physical_cores) // 2))


def split_cpu_threads(limit: int, physical_cores: int) -> Optional[Tuple[int, int]]:
    """
    ``(phone-pool threads, coding-model threads)`` for a machine that runs both, so the two jobs do not fight over
    the cores. The total is MAX_CPU_THREADS when the owner set it (``limit`` >= 0), otherwise the physical cores.
    The phone pool gets what it would run on by itself (half the machine, 2 to 8) but never more than half of the
    total; the coding model, which uses the CPU for the layers that spill out of the GPU, gets the rest. None when
    the total is under 2: the two jobs cannot each have a thread of their own inside it, so "both" is not started.
    """
    cores = max(1, int(physical_cores))
    total = int(limit) if isinstance(limit, int) and not isinstance(limit, bool) and limit >= 0 else cores
    if total < 2:
        return None
    pool = max(1, min(default_pool_threads(cores), total // 2))
    return pool, total - pool
