"""
The GPU coding pool's model ladder: which model a pool of a given size should serve.

The ladder is data, not code. It lives in scripts/coding_models.json, is signed into every
release (engine/release.py, "coding_tiers") and is validated by the same function on every
path in, so the coordinator, the release script and any other reader see one shape.

WHERE THE LADDER COMES FROM, in order
    1. The signed release manifest the coordinator serves (<base>/release.json). This is the
       production path: an installed coordinator has no scripts/ folder of its own.
    2. scripts/coding_models.json under the base directory, for a coordinator run from a
       source checkout (and the test suite).
    3. BUILTIN_TIERS below: the one model live on the public swarm, so a coordinator whose
       release predates the ladder still names the right model instead of none.
    A source that is present but malformed is logged and skipped, never fatal:
    /v1/cluster/status must answer whatever the disk holds.

A TIER, as load_coding_tiers() returns it
    id           "qwen36-a3b"                         unique, [a-z0-9-]
    name         "Qwen3.6 35B-A3B"                    for people
    file         "Qwen3.6-35B-A3B-UD-IQ3_S.gguf"      what llama-server is pointed at (part 1 of a split model)
    url          pinned Hugging Face URL of ``file``
    sha256       of ``file``
    size         bytes of the whole model: the sum over ``files`` (equal to the file's size
                 when the model is one file)
    min_vram_gb  int, the legacy single number (a release before 2.9.0 signs only this one); read as
                 both of the next two when they are absent
    min_total_gb int, GPU memory plus lent system RAM the rung needs together (2.9.0): the experts of a
                 mixture-of-experts model may sit in RAM
    min_gpu_gb   int, what must be on GPUs (attention, shared weights, the KV cache); a dense rung has
                 min_total_gb == min_gpu_gb. A rung fits when BOTH hold (fits(), engine/auto_swap.py)
    rank         int, the measured quality order, bigger is better (2.9.0); "up" means a higher rank, never
                 merely a bigger file
    context      int, the total engine context the tier is meant to run with
    server_slots int, the most requests at once a coding member opens it for (2 unless the tier
                 says otherwise); context / server_slots is the context per request, which a
                 member keeps whatever slot count the coordinator gives it
    server_ctx_checkpoints, server_ubatch, server_spec_type
                 the member's llama-server knobs (--ctx-checkpoints, -ub, --spec-type), each
                 None unless the tier sets it (release.py has the ranges)
    enabled      bool, the owner's switch
    blocked      None, or why the tier is held back (an enabled tier is never blocked)
    files        [{file, url, sha256, size}, ...] every part in order; one entry for a
                 single-file model. Download and verify these, not just ``file``.
Tiers come worst first, best last (rank strictly increasing; a tier with no rank takes its list
position), ids and file names are unique.
"""

import copy
import json
import os
import re
from typing import Any, Dict, List, Optional

from engine import config as _config
from engine import release

CODING_MODELS_FILE = os.path.join("scripts", "coding_models.json")

# The model the public swarm has served since 2026-10-25. Identical to the first tier of
# scripts/coding_models.json (a test pins that), and to the CPU pool's "qwen36-a3b" (which
# has no server_* fields: the member's slots and context are the coding pool's business).
BUILTIN_TIERS: List[Dict[str, Any]] = [
    {
        "id": "qwen36-a3b",
        "name": "Qwen3.6 35B-A3B",
        "min_vram_gb": 15,
        "min_total_gb": 15,
        "min_gpu_gb": 15,
        "rank": 0,
        "context": 81920,
        "server_slots": 2,
        "enabled": True,
        "file": "Qwen3.6-35B-A3B-UD-IQ3_S.gguf",
        "url": "https://huggingface.co/unsloth/Qwen3.6-35B-A3B-GGUF/resolve/a483e9e6cbd595906af30beda3187c2663a1118c/Qwen3.6-35B-A3B-UD-IQ3_S.gguf",
        "sha256": "66a3ca888ce13482b40c333db2432c0ebde3a7b13754fc29f0c6f5e89703ec66",
        "size": 13676723168,
    },
]


def _warn(message: str) -> None:
    try:
        from engine.logger import log_warning
        log_warning(message)
    except Exception:  # noqa: BLE001 - a missing logger must never stop the ladder loading
        pass


def load_tier_file(path: str) -> List[Dict[str, Any]]:
    """The validated tiers of a coding_models.json file. Raises ReleaseError, OSError or ValueError."""
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise release.ReleaseError(f"{path} is not a JSON object with a tiers list")
    return release.check_coding_tiers(data.get("tiers"))


def load_coding_tiers(base_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    The ladder, smallest first, validated. Never raises and never returns an empty list:
    the signed release, else the source checkout's file, else the built-in tier.
    Each call returns fresh dicts, so a caller may keep or change them freely.
    """
    base = base_dir if base_dir is not None else _config.get_base_dir()
    manifest = release.load_manifest(os.path.join(base, release.MANIFEST_NAME))
    if manifest is not None and "coding_tiers" in manifest:
        try:
            tiers = release.coding_tiers(manifest)
            if tiers:
                return tiers
        except release.ReleaseError as exc:
            _warn(f"The release's coding model ladder is invalid ({exc}); trying {CODING_MODELS_FILE}.")
    path = os.path.join(base, CODING_MODELS_FILE)
    if os.path.isfile(path):
        try:
            return load_tier_file(path)
        except (release.ReleaseError, OSError, ValueError) as exc:
            _warn(f"{CODING_MODELS_FILE} is invalid ({type(exc).__name__}: {exc}); using the built-in tier.")
    return release.check_coding_tiers(copy.deepcopy(BUILTIN_TIERS))


def tier_for_vram(tiers: List[Dict[str, Any]], vram_gb: Any, enabled_only: bool = True) -> Optional[Dict[str, Any]]:
    """The biggest tier a pool of ``vram_gb`` can hold (only enabled ones by default), or None."""
    try:
        vram = float(vram_gb)
    except (TypeError, ValueError):
        return None
    if vram != vram:  # NaN
        return None
    best = None
    for tier in tiers:
        if enabled_only and not tier.get("enabled"):
            continue
        if vram >= tier.get("min_vram_gb", float("inf")):
            best = tier   # tiers are smallest first, so the last fit is the biggest
    return best


def fits(tier: Dict[str, Any], gpu_gb: Any, total_gb: Any) -> bool:
    """
    Whether a pool with ``gpu_gb`` of near GPU memory and ``total_gb`` of GPU memory plus lent RAM holds
    ``tier``: both numbers must hold. A tier from an old manifest has only min_vram_gb, read as both.
    """
    try:
        gpu, total = float(gpu_gb), float(total_gb)
        legacy = tier.get("min_vram_gb")
        need_gpu = float(tier["min_gpu_gb"] if tier.get("min_gpu_gb") is not None else legacy)
        need_total = float(tier["min_total_gb"] if tier.get("min_total_gb") is not None else legacy)
    except (TypeError, ValueError, KeyError):
        return False
    if gpu != gpu or total != total:  # NaN
        return False
    return gpu >= need_gpu and total >= need_total


def tier_for_pool(tiers: List[Dict[str, Any]], gpu_gb: Any, total_gb: Any,
                  enabled_only: bool = True) -> Optional[Dict[str, Any]]:
    """The best-ranked tier the pool holds (the ladder is in rank order, so the last fit), or None."""
    best = None
    for tier in tiers:
        if enabled_only and not tier.get("enabled"):
            continue
        if fits(tier, gpu_gb, total_gb):
            best = tier
    return best


def default_tier(tiers: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The smallest enabled tier (the first tier when none is enabled); None for no tiers."""
    for tier in tiers:
        if tier.get("enabled"):
            return tier
    return tiers[0] if tiers else None


def _file_key(file: Any) -> str:
    """A model reference as a comparable file name: path, ".gguf" and case do not matter."""
    if not isinstance(file, str) or not file.strip():
        return ""
    name = os.path.basename(file.strip().replace("\\", "/")).replace(":", "-").lower()
    return name if name.endswith(".gguf") else name + ".gguf"


def tier_for_file(tiers: List[Dict[str, Any]], file: Any) -> Optional[Dict[str, Any]]:
    """The tier whose model is ``file`` (any part of it; a path or a bare stem also match), or None."""
    key = _file_key(file)
    if not key:
        return None
    for tier in tiers:
        parts = tier.get("files") or [{"file": tier.get("file")}]
        if any(_file_key(part.get("file")) == key for part in parts):
            return tier
    return None


def rank(tiers: List[Dict[str, Any]], file: Any) -> Optional[int]:
    """
    How smart the model ``file`` is, as its place on the ladder: 0 for the first tier, and a
    bigger number is a smarter model (ladder order is quality order). None when the file is on
    no tier: an unknown model is never called smarter or less smart than another.
    """
    tier = tier_for_file(tiers or [], file)
    if tier is None:
        return None
    return next(i for i, t in enumerate(tiers) if t is tier)


def tier_label(tier: Dict[str, Any]) -> str:
    """How a tier is named in recommended_tier: "Qwen3.6 35B-A3B (15 GB+ VRAM)", or for a rung that
    keeps experts in RAM "Qwen3.6 35B-A3B (IQ4_XS) (24 GB+ GPU and RAM)"."""
    total, gpu = tier.get("min_total_gb"), tier.get("min_gpu_gb")
    if total is None or gpu is None or total == gpu:
        return f"{tier['name']} ({tier['min_vram_gb'] if total is None else total} GB+ VRAM)"
    return f"{tier['name']} ({total} GB+ GPU and RAM)"


# The parts of a GGUF file name that are not the model's name. The quantization is the last
# segment (Q4_K_M, IQ3_S, Q8_0, F16, BF16, MXFP4...), optionally after unsloth's "UD-" marker;
# a split model also carries -00001-of-00003.
_SPLIT_SUFFIX_RE = re.compile(r"-\d{5}-of-\d{5}$")
_QUANT_SUFFIX_RE = re.compile(r"[-_.](?:UD[-_])?(?:I?Q\d(?:_[A-Z0-9]+)*|BF16|FP16|F16|F32|MXFP4(?:_MOE)?)$", re.IGNORECASE)
_SIZE_RE = re.compile(r"^\d+(?:\.\d+)?[BM]$", re.IGNORECASE)
_ACTIVE_RE = re.compile(r"^A\d+(?:\.\d+)?[BM]$", re.IGNORECASE)


def _derived_name(file: str) -> str:
    base = os.path.basename(file.strip().replace("\\", "/"))
    stem = base[:-5] if base.lower().endswith(".gguf") else base
    stem = _SPLIT_SUFFIX_RE.sub("", stem)
    stem = _QUANT_SUFFIX_RE.sub("", stem)
    words = [word for word in re.split(r"[-_]", stem) if word]
    if not words:
        return base
    name = words[0]
    for previous, word in zip(words, words[1:]):
        # "35B-A3B" (total-active parameters) stays one word; every other separator is a space.
        name += ("-" if _SIZE_RE.match(previous) and _ACTIVE_RE.match(word) else " ") + word
    return name


def display_name(file: Any, tiers: Optional[List[Dict[str, Any]]] = None) -> str:
    """
    The name to show for a model file: its tier's name when the ladder has it, otherwise one
    derived from the file name ("Qwen3.6-35B-A3B-UD-IQ3_S.gguf" -> "Qwen3.6 35B-A3B").
    "" for no file.
    """
    if not isinstance(file, str) or not file.strip():
        return ""
    tier = tier_for_file(tiers or [], file)
    if tier is not None:
        return tier["name"]
    return _derived_name(file)
