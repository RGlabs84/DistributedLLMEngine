"""
Which coding model a volunteer machine should run as a coding member, and the join test that
proves it is fast enough (docs/coding-members.md section 3).

THE RULE (owner, 2026-10-26: "best of the best model, start from a smart starting point and
work outwards")
    Pick the SMARTEST enabled tier this one machine can run whole while still meeting the speed
    floors. The ladder (engine/coding_ladder.py) is ordered smallest first and the order is the
    quality order, so "smartest" means "furthest down the list". A tier is a candidate when

      1. it fits: GPU memory plus the usable system memory (all but a reserve for the OS) holds
         the weights, the KV cache for the tier's context and some overhead; llama.cpp's
         ``--fit on`` puts what does not fit on the GPU into system RAM. At least 8 GB of GPU
         memory is needed, whatever the tier;
      2. the speed ESTIMATE for this machine says the floors will be met with some margin
         (CODING_MEMBER_ESTIMATE_MARGIN). The estimate decides which tier to download, so a
         member never fetches 29 GB to find out it is too slow. It does not decide whether the
         member joins: the join test below does.

    A tier the estimate puts a little under the margin ("marginal") is still tried, after every
    tier that clears it, because slots are the top priority and the estimate is only an estimate.
    ``candidate_tiers`` lists them in the order to try; the member runtime tests the first one
    and, when it fails the floors, steps down to the next (at most MAX_STEP_DOWNS times).

THE SPEED ESTIMATE (dense versus mixture of experts, GPU versus RAM)
    Decoding one token reads every weight that token needs, so it is memory-bandwidth bound:
        bytes per token = (weights - CPU-resident embedding) x active/total parameters x amp
        time per token  = bytes x (on-GPU share / GPU bandwidth + in-RAM share / RAM bandwidth)
                          + a fixed per-token cost
    "Active" is the parameters one token touches: 3B of 35B for Qwen3.6-35B-A3B, all of them
    for a dense model, which is why a dense 27B reads 8x the bytes of a 3B-active MoE.
    The on-GPU share is what ``--fit on`` can keep on the GPU: the card minus the KV cache and a
    fixed amount kept free. Prompt reading is compute bound: a MoE's experts that live in RAM are
    computed by the CPU (slow), while a dense model's RAM-resident layers are streamed to the GPU
    a batch at a time (PCIe bound).
    Calibrated on the owner's 8 GB RTX 4070 laptop (30 GB RAM, i9-14900HX) from
    dlne-coding-bakeoff/local/results*: Qwen3.6-35B-A3B IQ3_S 32.5 tok/s decode and 121 tok/s
    prefill (estimate: 32.0 and 119.9); dense Qwen2.5-Coder-14B Q4_K_M 460 tok/s prefill
    (estimate: 436) and 16.9 tok/s decode with its 0.5B draft model at 94% acceptance (estimate
    5.2 without the draft, about a third of that). A different machine is a different number:
    the join test measures the real one. The join test itself (measure) on that laptop and model:
    27.3 and 29.7 tok/s decode, 327 and 352 tok/s prefill (2,108-token prompt, 10 to 22 s in all) with
    the memory the model needs to spare; the same test under a 12 GB memory cap, which forced the
    page cache holding the RAM-resident experts to be re-read, gave 24.2 and 141. So the estimate is
    deliberately the conservative reading, and the machine's FREE memory matters more than its total.

WHAT THE LADDER DOES NOT SAY
    Tiers carry no parameter counts or KV sizes (the signed manifest allows only a fixed set of
    fields), so FAMILIES below knows the models by name. A tier may also carry ``params_b``,
    ``active_params_b``, ``kv_gb_per_64k``, ``recurrent_gb`` or ``cpu_resident_gb``; each one
    overrides the table. A model that is in neither is read from its name ("35B-A3B" is a MoE
    with 3B active, "27B" is dense) and given a deliberately large KV cache.

UNITS
    ``vram_gb`` and ``ram_gb`` are GiB, as engine/hardware.py reports them; tier sizes are bytes.
"""

import math
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from engine import config as _config
from engine import release

GIB = 1024 ** 3
GB = 1_000_000_000

# -- who may be a coding member -------------------------------------------------------------
MIN_VRAM_GB = 8.0
# Cards sold as "8 GB" report 7.6 to 8.0 GiB (the owner's RTX 4070 laptop: 8188 MiB), so the
# minimum is checked with this much slack. A 6 GB card is still refused.
VRAM_TOLERANCE_GB = 0.5

# -- the memory arithmetic ------------------------------------------------------------------
FIT_OVERHEAD_GIB = 2.0         # CUDA/Vulkan context, compute buffers and the host's side of llama-server
GPU_KEEP_FREE_GIB = 1.8        # what --fit on leaves free on the card: context, compute buffers, its safety margin
UNIFIED_GPU_SHARE = 0.75       # the share of unified memory the GPU may wire when the caller gives no figure

# -- the speed constants (calibrated, see the module docstring) ---------------------------------
# (VRAM per card below this in GiB) -> (effective decode bandwidth GB/s, effective prefill GFLOP/s).
# Conservative for each class (about 60% of the slowest common card's peak): the join test
# corrects a machine that is faster, and a machine that is slower fails it.
_GPU_CLASSES: Tuple[Tuple[float, float, float], ...] = (
    (10.0, 150.0, 20000.0),     # 8 GB: RTX 4060/4070 laptop and similar (measured)
    (14.0, 200.0, 28000.0),     # 12 GB
    (20.0, 250.0, 36000.0),     # 16 GB
    (float("inf"), 450.0, 60000.0),     # 24 GB and up
)
UNIFIED_BANDWIDTH_GBPS = 180.0   # Strix Halo (256 peak) and the Apple base chips; bigger Apple chips are faster
UNIFIED_GFLOPS = 5000.0
RAM_BANDWIDTH_GBPS = 35.0        # dual-channel DDR5 laptop, as measured by the decode of the RAM-resident experts
CPU_GFLOPS = 420.0               # eight P-cores, quantized matmul, as measured by the MoE prefill
PCIE_GBPS = 12.0                 # effective host-to-card copy rate
PREFILL_BATCH = 512              # llama-server's -ub: how many prompt tokens share one pass over the weights
FIXED_MS_PER_TOKEN = 3.0         # launches, sampling, the host's side of each step
READ_AMPLIFICATION_MOE = 1.25    # output head, router, norms and KV reads on top of the active matrices
READ_AMPLIFICATION_DENSE = 1.05

MARGINAL_FRACTION = 0.75         # a tier estimated at this share of the floors (or more) is still worth trying
MAX_STEP_DOWNS = 2               # join test failures the member runtime may answer with a smaller tier

# -- the join test --------------------------------------------------------------------------
JOIN_TEST_BUDGET_S = 60.0
JOIN_PROMPT_TOKENS = 2000
JOIN_GENERATED_TOKENS = 128
MIN_PROMPT_TOKENS_COUNTED = 1000     # a rate over fewer prompt tokens (a cache hit, a cut prompt) proves nothing
MIN_GENERATED_TOKENS_COUNTED = 64    # same for a very short answer
MAX_TOKENS_PER_SECOND = 100000.0     # llama.cpp cannot do this; a server reporting it is lying or broken
WARMUP_BUDGET_S = 15.0
_CHARS_PER_TOKEN = 3.57              # for sizing the filler (Qwen3.6's tokenizer: 8,574 characters were 2,402 tokens); prompt_n is what is checked


# ======================================================================================
# What the tiers are made of
# ======================================================================================

@dataclass(frozen=True)
class Family:
    """What the speed and memory arithmetic needs to know about a model."""
    params_b: float                  # parameters read as matrices, in billions (CPU-kept embedding tables excluded)
    active_params_b: float           # parameters one token goes through (== params_b for a dense model)
    kv_gb_per_64k: float             # KV cache for 65,536 tokens at q8_0, decimal GB
    recurrent_gb: float              # recurrent state of the Gated DeltaNet layers (f32), decimal GB
    cpu_resident_gb: float = 0.0     # weights llama.cpp keeps on the CPU side by design (decimal GB)
    cpu_resident_by_quant: Tuple[Tuple[str, float], ...] = ()    # (substring of the tier id, GB) overriding the above

    @property
    def moe(self) -> bool:
        return self.active_params_b < self.params_b


# Figures: /tmp/claude-1000/model-ladder/PROPOSAL.md sections 5.1 and 6.1 (header, vendor and
# estimate figures there are labelled) and the memory note dlne-coding-at-scale (Qwen3.6 KV is
# 0.71 GB per 65,536 tokens at q8_0).
FAMILIES: Tuple[Tuple["re.Pattern[str]", Family], ...] = (
    (re.compile(r"qwen3\.?6.*a3b"), Family(35.0, 3.0, 0.71, 0.13)),
    # 176B in all, of which 125B are matrices (6B active) and 51B an n-gram embedding that is only looked up.
    (re.compile(r"flash-next"), Family(125.0, 6.0, 0.86, 0.22, cpu_resident_gb=29.5,
                                       cpu_resident_by_quant=(("q5", 55.1),))),
    (re.compile(r"qwen3\.?8.*27b"), Family(27.0, 27.0, 2.28, 0.30)),
    # Qwen3-Coder-Next is Qwen3-Next 80B-A3B: 48 layers, 12 of them attention (2 KV heads x 256), 36 Gated
    # DeltaNet. KV about 12 KB per token at q8_0 (0.85 GB per 65,536); recurrent state scaled from Qwen3.6.
    (re.compile(r"coder-?next"), Family(80.0, 3.0, 0.85, 0.16)),
)
_MOE_NAME_RE = re.compile(r"(\d+(?:\.\d+)?)b[-_ ]a(\d+(?:\.\d+)?)b")
_DENSE_NAME_RE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)b(?![a-z0-9])")
_UNKNOWN_BITS_PER_WEIGHT = 4.5
# A model the table does not know is assumed to be full attention, which has the biggest KV cache:
# about 0.45 GB per billion parameters for 65,536 tokens at q8_0 when dense (a 14B: 3.4 GB at 32K), about
# 0.1 GB for a MoE (fewer KV heads). Guessing high means "does not fit", never an out-of-memory crash.
_UNKNOWN_DENSE_KV_GB_PER_B = 0.45
_UNKNOWN_MOE_KV_GB_PER_B = 0.10


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _positive(value: Any) -> Optional[float]:
    number = _finite(value)
    return number if number is not None and number > 0 else None


def _non_negative(value: Any) -> Optional[float]:
    number = _finite(value)
    return number if number is not None and number >= 0 else None


def _tier_text(tier: Dict[str, Any]) -> str:
    return f"{tier.get('id') or ''} {tier.get('file') or ''} {tier.get('name') or ''}".lower()


def tier_family(tier: Dict[str, Any]) -> Family:
    """The model arithmetic for a tier: the tier's own fields, else FAMILIES, else its name, else a cautious guess."""
    text = _tier_text(tier)
    size_gb = (_positive(tier.get("size")) or 0.0) / GB
    base: Optional[Family] = None
    for pattern, family in FAMILIES:
        if pattern.search(text):
            base = family
            break
    if base is None:
        moe = _MOE_NAME_RE.search(text)
        dense = _DENSE_NAME_RE.search(text)
        if moe:
            total = float(moe.group(1))
            base = Family(total, float(moe.group(2)), _UNKNOWN_MOE_KV_GB_PER_B * total, 0.0)
        elif dense:
            params = float(dense.group(1))
            base = Family(params, params, _UNKNOWN_DENSE_KV_GB_PER_B * params, 0.0)
        else:
            params = max(1.0, size_gb * 8 / _UNKNOWN_BITS_PER_WEIGHT)
            base = Family(params, params, _UNKNOWN_DENSE_KV_GB_PER_B * params, 0.0)
    cpu_resident = base.cpu_resident_gb
    for marker, gigabytes in base.cpu_resident_by_quant:
        if marker in str(tier.get("id") or "").lower():
            cpu_resident = gigabytes
    params = _positive(tier.get("params_b")) or base.params_b
    active = _positive(tier.get("active_params_b")) or base.active_params_b
    kv = _positive(tier.get("kv_gb_per_64k"))
    recurrent = _non_negative(tier.get("recurrent_gb"))
    override_resident = _non_negative(tier.get("cpu_resident_gb"))
    return Family(
        params_b=params,
        active_params_b=min(active, params),
        kv_gb_per_64k=kv if kv is not None else base.kv_gb_per_64k,
        recurrent_gb=recurrent if recurrent is not None else base.recurrent_gb,
        cpu_resident_gb=override_resident if override_resident is not None else cpu_resident,
    )


# ======================================================================================
# The machine
# ======================================================================================

@dataclass(frozen=True)
class Machine:
    """One volunteer machine, as far as the choice cares."""
    vram_gb: float                       # GPU memory in GiB (all cards together); for unified memory, what the GPU may use
    ram_gb: float                        # system memory in GiB; for unified memory, the one pool
    unified: bool = False                # GPU and CPU share one memory (Apple Silicon, Strix Halo): never add the two
    gpu_count: int = 1
    gpu_bw_gbps: Optional[float] = None  # effective decode bandwidth of one card; None = a conservative figure for its size
    gpu_gflops: Optional[float] = None   # effective prefill throughput of one card
    ram_bw_gbps: Optional[float] = None  # engine.hardware.get_memory_bandwidth_gbs() is the measured figure when there is one


def make_machine(vram_gb: Any, ram_gb: Any, unified: bool = False, gpu_count: int = 1, gpu_bw_gbps: Any = None,
                 gpu_gflops: Any = None, ram_bw_gbps: Any = None) -> Machine:
    """A Machine from whatever the hardware probe gave: nonsense (NaN, negative, text) counts as nothing."""
    count = gpu_count if isinstance(gpu_count, int) and not isinstance(gpu_count, bool) and gpu_count >= 1 else 1
    return Machine(
        vram_gb=_non_negative(vram_gb) or 0.0,
        ram_gb=_non_negative(ram_gb) or 0.0,
        unified=bool(unified),
        gpu_count=count,
        gpu_bw_gbps=_positive(gpu_bw_gbps),
        gpu_gflops=_positive(gpu_gflops),
        ram_bw_gbps=_positive(ram_bw_gbps),
    )


def _gpu_speeds(machine: Machine) -> Tuple[float, float, float]:
    """(GPU decode GB/s, GPU prefill GFLOP/s, RAM decode GB/s) for the machine."""
    if machine.unified:
        bandwidth = machine.gpu_bw_gbps or UNIFIED_BANDWIDTH_GBPS
        return bandwidth, machine.gpu_gflops or UNIFIED_GFLOPS, machine.ram_bw_gbps or bandwidth
    per_card = machine.vram_gb / machine.gpu_count
    bandwidth, gflops = _GPU_CLASSES[-1][1], _GPU_CLASSES[-1][2]
    for ceiling, class_bandwidth, class_gflops in _GPU_CLASSES:
        if per_card < ceiling:
            bandwidth, gflops = class_bandwidth, class_gflops
            break
    return machine.gpu_bw_gbps or bandwidth, machine.gpu_gflops or gflops, machine.ram_bw_gbps or RAM_BANDWIDTH_GBPS


# ======================================================================================
# Limits
# ======================================================================================

def _limits(limits: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return limits if isinstance(limits, dict) else _config.get_worker_limits()


def _limit(limits: Dict[str, Any], key: str) -> float:
    """
    ``limits[key]`` (the typed name, or the CODING_MEMBER_* config spelling) as a positive
    number, else the default engine.config.CODING_MEMBER_DEFAULTS gives, so a limits dict that
    lacks the key (a test's, an older caller's) or holds nonsense still has an answer.
    """
    for name in (key, key.upper()):
        value = _positive(limits.get(name))
        if value is not None:
            return value
    return float(_config.CODING_MEMBER_DEFAULTS[key])


def floors(limits: Optional[Dict[str, Any]] = None) -> Tuple[float, float]:
    """(minimum decode tok/s, minimum prefill tok/s) from the limits, which default to the config's."""
    limits = _limits(limits)
    return _limit(limits, "coding_member_min_decode_tps"), _limit(limits, "coding_member_min_prefill_tps")


def meets_floor(decode_tps: Any, prefill_tps: Any, limits: Optional[Dict[str, Any]] = None) -> bool:
    """
    Whether a member's measured speeds clear both floors, CODING_MEMBER_MIN_DECODE_TPS and
    CODING_MEMBER_MIN_PREFILL_TPS (``limits`` is engine.config.get_worker_limits(); None reads
    the config). A missing, non-numeric, NaN or non-positive speed is a miss.
    """
    decode, prefill = _positive(decode_tps), _positive(prefill_tps)
    if decode is None or prefill is None:
        return False
    min_decode, min_prefill = floors(limits)
    return decode >= min_decode and prefill >= min_prefill


# ======================================================================================
# Choosing a tier
# ======================================================================================

@dataclass(frozen=True)
class TierEstimate:
    """What the arithmetic says about one tier on one machine."""
    tier_id: str
    verdict: str                  # good | marginal | slow | too_big | no_gpu | disabled | invalid
    fits: bool
    need_gb: float                # weights + KV + overhead, GiB
    have_gb: float                # GPU memory + usable system memory, GiB
    gpu_fraction: float           # share of the weights that can live on the GPU
    decode_tps: float             # estimated; 0 when the tier does not fit
    prefill_tps: float


def _tier_size(tier: Dict[str, Any]) -> Optional[float]:
    size = _positive(tier.get("size"))
    if size is None and isinstance(tier.get("files"), list):
        parts = [_positive(part.get("size")) for part in tier["files"] if isinstance(part, dict)]
        if parts and all(p is not None for p in parts):
            size = float(sum(parts))   # type: ignore[arg-type]
    return size


def estimate_tier(tier: Dict[str, Any], machine: Machine, limits: Optional[Dict[str, Any]] = None) -> TierEstimate:
    """The fit and the speed estimate of one tier on one machine (any tier, enabled or not)."""
    limits = _limits(limits)
    tier_id = str(tier.get("id") or "") if isinstance(tier, dict) else ""
    size_bytes = _tier_size(tier) if isinstance(tier, dict) else None
    context = _positive(tier.get("context")) if isinstance(tier, dict) else None
    if size_bytes is None or context is None:
        return TierEstimate(tier_id, "invalid", False, 0.0, 0.0, 0.0, 0.0, 0.0)

    reserve = _limit(limits, "coding_member_ram_reserve_gb")
    margin = _limit(limits, "coding_member_estimate_margin")
    min_decode, min_prefill = floors(limits)

    family = tier_family(tier)
    size_gib = size_bytes / GIB
    resident_gib = min(family.cpu_resident_gb * GB / GIB, size_gib)
    gpu_able_gib = size_gib - resident_gib
    kv_gib = (family.kv_gb_per_64k * context / 65536 + family.recurrent_gb) * GB / GIB

    usable_ram = max(0.0, machine.ram_gb - reserve)
    if machine.unified:
        gpu_total = machine.vram_gb or UNIFIED_GPU_SHARE * machine.ram_gb
        have = usable_ram
        gpu_cap = min(gpu_total, usable_ram)
    else:
        gpu_total = machine.vram_gb
        have = machine.vram_gb + usable_ram
        gpu_cap = machine.vram_gb
    need = size_gib + kv_gib + FIT_OVERHEAD_GIB
    # The CPU-resident part (an embedding table) must be in system memory, whatever else is.
    resident_fits = machine.unified or resident_gib <= usable_ram

    if gpu_total + VRAM_TOLERANCE_GB < MIN_VRAM_GB:
        return TierEstimate(tier_id, "no_gpu", False, need, have, 0.0, 0.0, 0.0)
    if need > have or not resident_fits:
        return TierEstimate(tier_id, "too_big", False, need, have, 0.0, 0.0, 0.0)

    on_gpu = min(gpu_able_gib, max(0.0, gpu_cap - GPU_KEEP_FREE_GIB - kv_gib))
    fraction = on_gpu / gpu_able_gib if gpu_able_gib > 0 else 1.0

    gpu_bw, gpu_gflops, ram_bw = _gpu_speeds(machine)
    active_fraction = family.active_params_b / family.params_b
    amplification = READ_AMPLIFICATION_MOE if family.moe else READ_AMPLIFICATION_DENSE
    active_gb = (size_bytes / GB - family.cpu_resident_gb) * active_fraction * amplification
    decode_ms = 1000.0 * active_gb * (fraction / gpu_bw + (1.0 - fraction) / ram_bw) + FIXED_MS_PER_TOKEN

    flops_g = 2.0 * family.active_params_b          # GFLOP per prompt token
    gpu_ms = 1000.0 * flops_g / gpu_gflops
    if family.moe:
        # Experts that live in RAM are computed by the CPU.
        prefill_ms = fraction * gpu_ms + (1.0 - fraction) * 1000.0 * flops_g / CPU_GFLOPS
    else:
        # A dense model's RAM-resident layers are copied to the GPU once per batch of prompt tokens.
        copied_gb = (1.0 - fraction) * gpu_able_gib * GIB / GB
        prefill_ms = gpu_ms + (0.0 if machine.unified else 1000.0 * copied_gb / PCIE_GBPS / PREFILL_BATCH)
    decode_tps, prefill_tps = 1000.0 / decode_ms, 1000.0 / prefill_ms

    if decode_tps >= min_decode * margin and prefill_tps >= min_prefill * margin:
        verdict = "good"
    elif decode_tps >= min_decode * MARGINAL_FRACTION and prefill_tps >= min_prefill * MARGINAL_FRACTION:
        verdict = "marginal"
    else:
        verdict = "slow"
    return TierEstimate(tier_id, verdict, True, need, have, fraction, round(decode_tps, 1), round(prefill_tps, 1))


def assess_tiers(vram_gb: Any, ram_gb: Any, tiers: Sequence[Dict[str, Any]], limits: Optional[Dict[str, Any]] = None,
                 **machine: Any) -> List[TierEstimate]:
    """Every tier's estimate on this machine, in ladder order (disabled ones are marked ``disabled``), for logs and status."""
    limits = _limits(limits)
    rig = make_machine(vram_gb, ram_gb, **machine)
    out = []
    for tier in tiers or []:
        if not isinstance(tier, dict):
            continue
        if not tier.get("enabled"):
            out.append(TierEstimate(str(tier.get("id") or ""), "disabled", False, 0.0, 0.0, 0.0, 0.0, 0.0))
        else:
            out.append(estimate_tier(tier, rig, limits))
    return out


def candidate_tiers(vram_gb: Any, ram_gb: Any, tiers: Sequence[Dict[str, Any]], limits: Optional[Dict[str, Any]] = None,
                    **machine: Any) -> List[Dict[str, Any]]:
    """
    The tiers to try on this machine, best first: every tier whose estimate clears the floors
    with the margin, smartest first. Only when there is none, the marginal tiers (estimated at
    75% of the floors or more), fastest first, because then the join test is the only judge left
    and a tier bigger than the smallest one that fits would only waste a download. Empty when the
    machine cannot be a coding member. The member runtime tests the first and, when the join test
    misses the floors, takes the next (at most MAX_STEP_DOWNS times).
    """
    limits = _limits(limits)
    ladder = [tier for tier in (tiers or []) if isinstance(tier, dict)]      # assess_tiers skips the same entries
    rated = list(zip(ladder, assess_tiers(vram_gb, ram_gb, ladder, limits, **machine)))
    good = [tier for tier, est in reversed(rated) if est.verdict == "good"]
    if good:
        return good
    min_decode, min_prefill = floors(limits)
    marginal = [(tier, est) for tier, est in rated if est.verdict == "marginal"]
    marginal.sort(key=lambda pair: min(pair[1].decode_tps / min_decode, pair[1].prefill_tps / min_prefill), reverse=True)
    return [tier for tier, _ in marginal]


def choose_tier(vram_gb: Any, ram_gb: Any, tiers: Sequence[Dict[str, Any]], limits: Optional[Dict[str, Any]] = None,
                **machine: Any) -> Optional[Dict[str, Any]]:
    """
    The smartest enabled tier this one machine can run whole and fast enough, or None.

    ``tiers`` is the coding ladder (engine.coding_ladder.load_coding_tiers()), smallest and
    least smart first. Optional machine facts: ``unified=True`` for Apple Silicon and Strix Halo
    (one memory pool, so ``ram_gb`` is that pool and ``vram_gb`` what the GPU may use),
    ``gpu_count``, and ``gpu_bw_gbps``, ``gpu_gflops``, ``ram_bw_gbps`` when they are known.
    Returns the ladder's own dict (not a copy).
    """
    found = candidate_tiers(vram_gb, ram_gb, tiers, limits, **machine)
    return found[0] if found else None


def choose_slots(tier: Dict[str, Any], vram_gb: Any, ram_gb: Any, limits: Optional[Dict[str, Any]] = None,
                 **machine: Any) -> int:
    """
    How many requests at once this machine should open ``tier`` for (2.5.0): the most, up to the
    tier's ``server_slots``, that it is estimated to run fast enough with every slot answering. The
    context per request is the tier's (``context // server_slots``) whatever the count, so n slots
    cost n times the KV cache and n answers at once share the machine: the estimate runs at n times
    the per-request context against floors n times higher. 1 when no count clears them (the join
    test is the judge then) and 1 for a tier or machine the estimate cannot read; never raises.
    """
    try:
        most = tier.get("server_slots")
        if not isinstance(most, int) or isinstance(most, bool) or most < 1:
            most = release.CODING_DEFAULT_SERVER_SLOTS
        per_request = int(tier["context"]) // most
        limits = _limits(limits)
        min_decode, min_prefill = floors(limits)
        rig = make_machine(vram_gb, ram_gb, **machine)
        for n in range(most, 1, -1):
            scaled = {**limits, "coding_member_min_decode_tps": min_decode * n, "coding_member_min_prefill_tps": min_prefill * n}
            if estimate_tier({**tier, "context": per_request * n}, rig, scaled).verdict == "good":
                return n
    except Exception:  # noqa: BLE001 - no context, no size, odd figures: one slot
        pass
    return 1


def next_tier_down(failed: Dict[str, Any], vram_gb: Any, ram_gb: Any, tiers: Sequence[Dict[str, Any]],
                   limits: Optional[Dict[str, Any]] = None, **machine: Any) -> Optional[Dict[str, Any]]:
    """The tier to try after ``failed`` missed the floors in the join test, or None when there is none."""
    found = candidate_tiers(vram_gb, ram_gb, tiers, limits, **machine)
    failed_id = failed.get("id") if isinstance(failed, dict) else None
    for position, tier in enumerate(found):
        if tier.get("id") == failed_id:
            return found[position + 1] if position + 1 < len(found) else None
    return None


# ======================================================================================
# The join test
# ======================================================================================

@dataclass(frozen=True)
class JoinTestResult:
    ok: bool                     # both speeds were measured on a valid run
    decode_tps: float            # generation speed, one user
    prefill_tps: float           # prompt reading speed
    prompt_tokens: int
    generated_tokens: int
    seconds: float               # wall time of the whole test
    reason: str                  # why not ok ("" when ok)


_NOUNS = ("user", "order", "invoice", "session", "ticket", "account", "device", "report", "payment", "message",
          "project", "booking", "coupon", "shipment", "profile", "invoice_line", "audit", "queue")
_VERBS = ("load", "check", "build", "sync", "close", "reopen", "archive", "export", "refresh", "validate")


def _filler_code(tokens: int) -> str:
    """About ``tokens`` tokens of varied, plausible Python (fixed seed: the same text every time)."""
    rng = random.Random(20261026)
    parts: List[str] = []
    chars = 0
    index = 0
    while chars < tokens * _CHARS_PER_TOKEN:
        noun, verb = rng.choice(_NOUNS), rng.choice(_VERBS)
        other = rng.choice(_NOUNS)
        block = (f"def {verb}_{noun}_{index}(request, db):\n"
                 f"    {noun} = db.get_{noun}(request.{noun}_id)\n"
                 f"    if {noun} is None:\n"
                 f"        raise LookupError('unknown {noun} %s' % request.{noun}_id)\n"
                 f"    rows = db.query('select * from {other}_events where owner = ? and kind <> ?', {noun}.id, {rng.randint(1, 99)})\n"
                 f"    return [{{'id': r.id, 'kind': r.kind, 'at': r.at.isoformat()}} for r in rows if r.visible]\n\n")
        parts.append(block)
        chars += len(block)
        index += 1
    return "".join(parts)


def _request_body(user_text: str, max_tokens: int) -> Dict[str, Any]:
    return {
        "messages": [{"role": "user", "content": user_text}],
        "max_tokens": max_tokens,
        "temperature": 0.2,
        "stream": False,
        "cache_prompt": False,          # nothing may be answered from the prompt cache
        "ignore_eos": True,             # a short answer must not cut the generation below 128 tokens
        "chat_template_kwargs": {"enable_thinking": False},
    }


def _timings(client: Any, url: str, body: Dict[str, Any], timeout: float) -> Tuple[Optional[Dict[str, Any]], str]:
    """(llama.cpp's ``timings`` object, "") for one request, or (None, why not)."""
    try:
        response = client.post(url, json=body, timeout=timeout)
        status = getattr(response, "status_code", 200)
        if status != 200:
            return None, f"the server answered HTTP {status}"
        data = response.json()
    except Exception as exc:   # noqa: BLE001 - any failure of the request means "does not qualify", never a crash
        return None, f"the request failed ({type(exc).__name__})"
    timings = data.get("timings") if isinstance(data, dict) else None
    if not isinstance(timings, dict):
        return None, "the server reported no timings"
    return timings, ""


def _rate(timings: Dict[str, Any], key: str) -> Optional[float]:
    value = _finite(timings.get(key))
    return value if value is not None and 0.0 < value <= MAX_TOKENS_PER_SECOND else None


def _count(timings: Dict[str, Any], key: str) -> int:
    value = timings.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def run_join_test(base_url: str, client: Any, budget_s: float = JOIN_TEST_BUDGET_S,
                  prompt_tokens: int = JOIN_PROMPT_TOKENS, generated_tokens: int = JOIN_GENERATED_TOKENS) -> JoinTestResult:
    """
    The join test against a member's own llama-server, with the reason when it does not
    qualify. One tiny request first (the first request pays for lazy allocations), then one
    request with a ~2,000-token prompt and a 128-token generation: llama.cpp's ``timings``
    give the prompt reading speed and the generation speed of that very request. If the answer
    came out too short to time, one short request measures the generation alone.

    Everything is bounded by ``budget_s`` (60 s); a member that cannot finish inside it is too
    slow for the floors anyway (2,000 tokens at 80 tok/s plus 128 at 15 tok/s is 34 s).
    ``client`` is an ``httpx.Client`` (anything with ``post(url, json=, timeout=)``).
    """
    started = time.monotonic()
    deadline = started + max(1.0, float(budget_s))

    def fail(reason: str, **seen: Any) -> JoinTestResult:
        return JoinTestResult(False, seen.get("decode", 0.0), seen.get("prefill", 0.0), seen.get("prompt", 0),
                              seen.get("generated", 0), round(time.monotonic() - started, 1), reason)

    url = str(base_url).rstrip("/") + "/v1/chat/completions"
    nonce = f"{random.getrandbits(32):08x}"

    warm, why = _timings(client, url, _request_body(f"[{nonce}] Say ok.", 4), min(WARMUP_BUDGET_S, max(0.5, deadline - time.monotonic())))
    if warm is None:
        return fail(f"warm-up: {why}")

    remaining = deadline - time.monotonic()
    if remaining <= 1.0:
        return fail("out of time before the test could start")
    text = (f"[{nonce}] Below are some Python handlers. Describe in detail, one handler after another, what each "
            f"one does, and keep writing until you are stopped.\n\n{_filler_code(prompt_tokens)}")
    timings, why = _timings(client, url, _request_body(text, generated_tokens), remaining)
    if timings is None:
        return fail(why)

    prefill = _rate(timings, "prompt_per_second")
    decode = _rate(timings, "predicted_per_second")
    prompt_n, predicted_n = _count(timings, "prompt_n"), _count(timings, "predicted_n")
    if prefill is None or prompt_n < MIN_PROMPT_TOKENS_COUNTED:
        return fail(f"the prompt was not read as a long prompt (prompt_n={prompt_n})", prompt=prompt_n, generated=predicted_n)
    if decode is None or predicted_n < MIN_GENERATED_TOKENS_COUNTED:
        # Too short an answer to time: measure the generation on its own with what is left.
        remaining = deadline - time.monotonic()
        if remaining <= 1.0:
            return fail("the answer was too short to time and no time was left", prefill=prefill, prompt=prompt_n, generated=predicted_n)
        second, why = _timings(client, url, _request_body(f"[{nonce}] Count from 1 upwards, one number per line.", generated_tokens), remaining)
        if second is None:
            return fail(why, prefill=prefill, prompt=prompt_n, generated=predicted_n)
        decode, predicted_n = _rate(second, "predicted_per_second"), _count(second, "predicted_n")
        if decode is None or predicted_n < MIN_GENERATED_TOKENS_COUNTED:
            return fail(f"the generation was too short to time (predicted_n={predicted_n})", prefill=prefill, prompt=prompt_n, generated=predicted_n)
    return JoinTestResult(True, round(decode, 1), round(prefill, 1), prompt_n, predicted_n,
                          round(time.monotonic() - started, 1), "")


def measure(base_url: str, client: Any, budget_s: float = JOIN_TEST_BUDGET_S) -> Tuple[float, float]:
    """
    ``(decode_tps, prefill_tps)`` of the member's llama-server at ``base_url``: the join test.
    Any failure (the server is down, answers an error, times out, reports no timings or an
    implausible number) is ``(0.0, 0.0)``, which :func:`meets_floor` refuses: "does not qualify".
    Use :func:`run_join_test` for the reason.
    """
    result = run_join_test(base_url, client, budget_s)
    return (result.decode_tps, result.prefill_tps) if result.ok else (0.0, 0.0)
