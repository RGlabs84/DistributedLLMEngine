"""
Where a rung's experts go, for the pool the coordinator really has (2.9.0, contract section 2).

``plan_layout(tier, pool, shape)`` answers three things before any restart:

    * does the rung fit in practice: its experts that do not fit on the GPUs must have RAM to sit in;
    * how many blocks' experts go to system RAM (``--n-cpu-moe N``);
    * the engine arguments that say so, checked with ``config.parse_engine_args`` (one flag it does not allow
      would make the engine drop the WHOLE ENGINE_EXTRA_ARGS line, so a layout that does not round-trip is
      refused here, not discovered at start).

THE RULE (deliberately simple)
    GPU budget for weights = pool GPU memory x POOL_SAFETY_FRACTION (0.9, the engine's own admission rule)
    less KV_RESERVE_GB (3 GB for the KV cache, state and compute buffers). The model's weights on GPUs
    (the file less the token embedding, plus the runtime's overhead) that exceed it are the experts that must
    leave: the first N blocks, N the smallest count whose experts cover the excess (llama.cpp's
    ``--n-cpu-moe N`` moves the first N blocks' routed experts to the host's CPU). Those bytes sit in THIS
    HOST's RAM, so they must fit in ``wall_ram_gb``.

    A volunteer's lent RAM is in the pool's numbers (pool_memory) but is NOT yet usable by the layout: placing
    experts on a volunteer's RPC CPU device needs ``-ot`` rules naming that device's buffer, which only the
    running RPC server knows. ``plan_layout`` takes the mapping (``rpc_cpu_buffers``: endpoint -> buffer name)
    and ``rpc_cpu_caps_gb`` (endpoint -> RAM that machine lends; ggml-rpc-server --device CPU advertises the WHOLE
    machine's RAM and has no cap flag, so these numbers are the only cap and no block is placed past one) and builds
    the ``-ot`` rules from them; without one, a rung that needs more than this host's RAM is reported
    as NOT realizable, so automatic switching never moves to a rung whose experts would be squeezed into
    TheWall's own RAM past its limit.

    With ``shape`` None (the file is not on disk yet) the experts are priced as 93 percent of the file and
    ``n_cpu_moe`` is None: the number is chosen when the file can be read.
"""

import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from engine import config as _config
from engine.inference_manager import POOL_SAFETY_FRACTION, RUNTIME_OVERHEAD_GB

GIB = float(1 << 30)
KV_RESERVE_GB = 3.0
EXPERT_SHARE_OF_FILE = 0.93      # used only when the tensor table cannot be read
EMBEDDING_GUESS_GB = 0.5         # likewise: the token embedding stays in RAM

_BUFFER_RE = re.compile(r"^[A-Za-z0-9_.:\-\[\]]{1,64}$")


@dataclass
class Layout:
    tier_id: str
    realizable: bool
    why: str
    n_cpu_moe: Optional[int] = None          # None: not known yet (file unreadable) or not needed
    args: List[str] = field(default_factory=list)
    experts_in_ram_gb: float = 0.0
    host_ram_gb: float = 0.0
    volunteer_ram_gb: float = 0.0

    def as_status(self) -> Dict[str, Any]:
        return {"tier": self.tier_id, "realizable": self.realizable, "why": self.why,
                "n_cpu_moe": self.n_cpu_moe, "args": list(self.args),
                "experts_in_ram_gb": round(self.experts_in_ram_gb, 2)}


def _gb(value: float) -> str:
    return f"{value:.1f}"


def _checked(args: List[str]) -> bool:
    """The arguments survive the allow-list unchanged (an empty list always does)."""
    if not args:
        return True
    import shlex
    return _config.parse_engine_args(" ".join(shlex.quote(a) for a in args)) == args


def plan_layout(tier: Dict[str, Any], pool: Dict[str, Any], shape: Any = None,
                rpc_cpu_buffers: Optional[Dict[str, str]] = None,
                rpc_cpu_caps_gb: Optional[Dict[str, float]] = None) -> Layout:
    """See the module docstring. ``pool`` is ``{"gpu_gb" | "vram_gb", "wall_ram_gb", "volunteer_ram_gb"}``;
    ``shape`` an engine.gguf.ModelShape or None."""
    tid = str(tier.get("id") or "")
    gpu_gb = float(pool.get("gpu_gb", pool.get("vram_gb", 0.0)) or 0.0)
    wall = float(pool.get("wall_ram_gb", pool.get("ram_gb", 0.0)) or 0.0)
    volunteers = float(pool.get("volunteer_ram_gb", 0.0) or 0.0) if "wall_ram_gb" in pool else 0.0
    budget = gpu_gb * POOL_SAFETY_FRACTION - KV_RESERVE_GB

    table = shape is not None and getattr(shape, "has_tensor_table", False)
    if table:
        on_gpu = shape.gpu_bytes(0) / GIB + RUNTIME_OVERHEAD_GB
    else:
        size_gb = float(tier.get("size") or 0) / GIB
        on_gpu = size_gb - EMBEDDING_GUESS_GB + RUNTIME_OVERHEAD_GB
    excess = on_gpu - budget
    if excess <= 0:
        return Layout(tid, True, "everything fits on the GPUs", 0, [], 0.0, wall, volunteers)

    # Experts available to move: all of them, so the excess cannot be larger than that.
    if table:
        experts_total = sum(size for _, size in shape.expert_bytes_by_layer) / GIB
    else:
        experts_total = float(tier.get("size") or 0) / GIB * EXPERT_SHARE_OF_FILE
    if excess > experts_total:
        return Layout(tid, False, f"even with every expert in RAM {_gb(on_gpu - experts_total)} GB must sit on the GPUs "
                                  f"and they offer {_gb(budget)} GB", None, [], experts_total, wall, volunteers)

    n: Optional[int] = None
    moved = excess
    if table:
        covered = 0.0
        n = 0
        for _, size in sorted(shape.expert_bytes_by_layer):
            if covered >= excess:
                break
            covered += size / GIB
            n += 1
        moved = covered

    if moved <= wall:
        args = ["--n-cpu-moe", str(n)] if n else []
        if not _checked(args):
            return Layout(tid, False, "the layout's arguments are not allowed by ENGINE_EXTRA_ARGS' allow-list",
                          n, [], moved, wall, volunteers)
        return Layout(tid, True, f"{_gb(moved)} GB of experts go to this host's RAM ({_gb(wall)} GB lent)",
                      n, args, moved, wall, volunteers)

    # More than this host lends: only a volunteer's RAM could hold the rest, through -ot rules.
    rest = moved - wall
    buffers = {e: b for e, b in (rpc_cpu_buffers or {}).items() if isinstance(b, str) and _BUFFER_RE.match(b)}
    # ggml-rpc-server --device CPU advertises the machine's WHOLE RAM and has no cap flag, so the cap on each cpu
    # endpoint is enforced here: its lent ram_gb (rpc_cpu_caps_gb[endpoint]; an endpoint with no cap lends nothing).
    caps = {e: max(0.0, float((rpc_cpu_caps_gb or {}).get(e, 0.0) or 0.0)) for e in buffers}
    usable = sum(caps.values())
    if rest > min(volunteers, usable) or not buffers or n is None:
        reason = (f"{_gb(moved)} GB of experts must leave the GPUs and this host lends {_gb(wall)} GB"
                  if rest > volunteers else
                  f"{_gb(rest)} GB of experts would sit in a volunteer's RAM, and no RPC CPU device is known to place them on")
        return Layout(tid, False, reason, n, [], moved, wall, volunteers)
    args = _volunteer_rules(shape, n, wall, {buffers[e]: caps[e] for e in buffers})
    if not args or not _checked(args):
        return Layout(tid, False, "the volunteer placement rules are not allowed by ENGINE_EXTRA_ARGS' allow-list",
                      n, [], moved, wall, volunteers)
    return Layout(tid, True, f"{_gb(wall)} GB of experts in this host's RAM, {_gb(rest)} GB in volunteers' RAM",
                  n, args, moved, wall, volunteers)


def _volunteer_rules(shape: Any, n: int, wall_gb: float, caps_by_buffer: Dict[str, float]) -> List[str]:
    """
    ``--n-cpu-moe`` is not used; one ``-ot`` rule per block sends the first ``n`` blocks' experts to this host's CPU
    while its lent RAM lasts, then to a volunteer's CPU buffer while ITS lent RAM (``caps_by_buffer``) lasts. A block
    is never put where it would pass that buffer's cap. Returns [] when the blocks do not all fit under the caps.
    """
    room = dict(sorted(caps_by_buffer.items()))
    hosted = 0.0
    args: List[str] = []
    for layer, size in sorted(shape.expert_bytes_by_layer)[:n]:
        gb = size / GIB
        if hosted + gb <= wall_gb:
            target = "CPU"
            hosted += gb
        else:
            target = next((name for name, left in room.items() if left >= gb), None)
            if target is None:
                return []
            room[target] -= gb
        args += ["-ot", f"blk\\.{layer}\\.ffn_(up|down|gate)_exps\\.={target}"]
    return args


def with_at_least(layout: Layout, operator_n: int, shape: Any) -> Layout:
    """
    The operator's own ``--n-cpu-moe`` (ENGINE_EXTRA_ARGS, tuned on TheWall against its real KV cache and context) is
    a floor: when it keeps MORE blocks' experts in RAM than the picked layout and this host's lent RAM still holds
    them, the operator's number is used. A layout with ``-ot`` rules, or a file whose table cannot be read, is left
    as it is.
    """
    if (not layout.realizable or operator_n <= (layout.n_cpu_moe or 0) or "-ot" in layout.args
            or shape is None or not getattr(shape, "has_tensor_table", False)):
        return layout
    moved = shape.experts_on_cpu_bytes(operator_n) / GIB
    if moved > layout.host_ram_gb:
        return layout
    args = ["--n-cpu-moe", str(operator_n)]
    if not _checked(args):
        return layout
    return Layout(layout.tier_id, True, f"the operator's --n-cpu-moe {operator_n} ({_gb(moved)} GB of experts in this host's RAM)",
                  operator_n, args, moved, layout.host_ram_gb, layout.volunteer_ram_gb)
