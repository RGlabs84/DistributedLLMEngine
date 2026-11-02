"""
Takeover leases (2.7.1): DLNE-Sec borrows hardware for a working session, DLNE keeps answering and gets it all back.

The contract is docs/takeover-contract.md (agreed with the DLNE-Sec session); this module is the coordinator's side of it:

* the **lease** and its file, ``config/takeover.json`` (written atomically; the lease, its expiry, the overlay of limits
  and the RAM limits to put back);
* the **planner**: for a request, what DLNE would do with each card, whether it is allowed, and what DLNE looks like
  afterwards (``make_plan``); a dry run changes nothing;
* the **overlay**: the changes a lease makes to the limits, applied on top of ``config.txt`` by
  ``engine.config.get_worker_limits()`` for as long as the lease is granted or active. ``config.txt`` is never written;
* **staleness**: a lease is stale when it has expired or 45 s have passed without a heartbeat; a stale file is ignored
  by the limits at once and deleted at the coordinator's start;
* **CPU-mask surgery** (cores out of ``--cpu-mask`` and ``IMAGE_CPU_MASK``, with their SMT siblings);
* the **controller** (``TakeoverController``): grant, heartbeat, release and the watcher that ends a lease on its ttl or
  a lost heartbeat. The coordinator hands it what only it knows (the engine, the idle test, the switch) as ``Host``.

This module imports no heavy engine module at import time: ``engine.config`` calls it from ``get_worker_limits()``,
which runs on every request.
"""

import asyncio
import ipaddress
import json
import math
import os
import re
import secrets
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from engine import config as engine_config
from engine.logger import log_coordinator, log_error, log_warning

# --------------------------------------------------------------------------------------
# Numbers the contract fixes, and the ones it leaves to the build
# --------------------------------------------------------------------------------------

HEARTBEAT_EVERY_SECONDS = 15              # contract: PUT every 15 s
HEARTBEAT_LOSS_SECONDS = 45               # contract: two missed (45 s without one) end the lease
IDLE_CHECK_GAP_SECONDS = 5                # contract: two idle checks 5 s apart
IDLE_WAIT_MAX_SECONDS = 600               # contract: at most 10 minutes
DEFAULT_TTL_SECONDS = 14400
MIN_TTL_SECONDS = 60                      # (build) a lease shorter than this could not even be heartbeated
MAX_TTL_SECONDS = 86400                   # (build) one working day; a longer session asks again
DEFAULT_CODING_LANES = 1
MAX_CODING_LANES = 8
DEFAULT_CODING_CONTEXT = 32768            # per lane, in tokens
MIN_CODING_CONTEXT = 2048
MAX_CODING_CONTEXT = 1048576
RESTART_ATTEMPTS = 60                     # (build) a restore tries this often (5 s apart) while another switch runs
WATCH_SECONDS = 5                         # how often the watcher looks at the lease
GRANT_WINDOW_SECONDS = 1200               # (build) a lease still "granting" after this long was abandoned by a crash
HOST_OVERHEAD_GB = 3.0                    # (build) what the coordinator and the engine's host side keep in RAM
MEMORY_HIGH_FRACTION = 0.92               # (build) MemoryHigh is this much of the lowered MemoryMax
SERVICE_UNIT = "dlne.service"             # the systemd user unit whose memory limits a lease lowers
LEASE_FILE_NAME = "takeover.json"

# A lease's state: being granted (the engine is being restarted on the overlay), active, or being given back.
STATE_GRANTING, STATE_ACTIVE, STATE_RESTORING = "granting", "active", "restoring"

CODE_FORBIDDEN = "takeover_forbidden"
CODE_ACTIVE = "takeover_active"
CODE_BUSY = "takeover_busy"
CODE_UNKNOWN_GPU = "takeover_unknown_gpu"
CODE_TOO_MUCH = "takeover_too_much"
# Not in the contract's list (it has no code for these three); added because every refusal carries one.
CODE_BAD_REQUEST = "takeover_bad_request"
CODE_NOT_FOUND = "takeover_not_found"
CODE_FAILED = "takeover_failed"

ACTION_HANDOVER = "handover"
ACTION_MEDIA = "pictures-pause+vision-cpu"
ACTION_ENGINE = "engine-moves"

# Where the tests point these (a temporary directory); on a real host they are the kernel's.
PCI_SYSFS = "/sys/bus/pci/devices"
CPU_SYSFS = "/sys/devices/system/cpu"
SMT_FALLBACK_OFFSET = 6                   # TheWall: core n's SMT sibling is CPU n+6

# The llama-server flags that carry a CPU mask or a thread count (ENGINE_EXTRA_ARGS is allow-listed in config.py).
_MAIN_MASK_FLAGS = ("--cpu-mask", "-C")
_BATCH_MASK_FLAGS = ("--cpu-mask-batch", "-Cb")
_THREAD_FLAGS = ("-t", "--threads", "-tb", "--threads-batch")


class TakeoverError(Exception):
    """A refusal the API answers as ``{"detail", "code"}`` with ``status``."""

    def __init__(self, status: int, code: str, detail: str):
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail


# --------------------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------------------

def now() -> float:
    """Wall-clock seconds; the one place the module reads the time outside the controller (a test replaces it)."""
    return time.time()


def iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


# --------------------------------------------------------------------------------------
# The lease file
# --------------------------------------------------------------------------------------

_FILE_LOCK = threading.RLock()
_cache: Tuple[Any, Optional[dict]] = (None, None)      # ((path, mtime_ns, size), lease)


def lease_path() -> str:
    return os.path.join(engine_config.get_base_dir(), "config", LEASE_FILE_NAME)


def load_lease() -> Optional[dict]:
    """The lease on disk, or None (no file, or one that cannot be read: nothing is applied from a damaged file).
    Cached on the file's stat, because the limits ask on every request."""
    global _cache
    path = lease_path()
    try:
        stat = os.stat(path)
    except OSError:
        _cache = (None, None)
        return None
    stamp = (path, stat.st_mtime_ns, stat.st_size)
    with _FILE_LOCK:
        if _cache[0] == stamp:
            return _cache[1]
        try:
            with open(path, "r", encoding="utf-8") as handle:
                lease = json.load(handle)
            if not isinstance(lease, dict) or not isinstance(lease.get("lease_id"), str):
                lease = None
        except (OSError, ValueError):
            lease = None
        _cache = (stamp, lease)
        return lease


def save_lease(lease: dict) -> None:
    """Writes the lease atomically: a staging file in the same directory, flushed to disk, then renamed over."""
    global _cache
    path = lease_path()
    with _FILE_LOCK:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        staging = f"{path}.{os.getpid()}.{secrets.token_hex(4)}.partial"
        try:
            with open(staging, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(lease, handle, indent=1, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(staging, path)
        except BaseException:
            try:
                os.remove(staging)
            except OSError:
                pass
            raise
        _cache = (None, None)


def delete_lease() -> None:
    global _cache
    with _FILE_LOCK:
        try:
            os.remove(lease_path())
        except FileNotFoundError:
            pass
        _cache = (None, None)


def is_stale(lease: dict, now: float) -> bool:
    """Expired, or without a heartbeat for HEARTBEAT_LOSS_SECONDS. A lease still being granted has no heartbeat
    yet (nobody holds its id): it is stale only when the grant that wrote it is long gone (a crash)."""
    if now >= float(lease.get("expires_at") or 0):
        return True
    last = float(lease.get("last_heartbeat") or 0)
    if lease.get("state") == STATE_GRANTING:
        return now - last > GRANT_WINDOW_SECONDS
    return now - last > HEARTBEAT_LOSS_SECONDS


def overlay_applies(lease: Optional[dict], now: float) -> bool:
    """Whether the limits carry this lease's changes: while it is being granted or active and not stale. A lease that is
    restoring has already given everything back as far as the limits go (the file stays only for the RAM)."""
    return bool(lease) and lease.get("state") in (STATE_GRANTING, STATE_ACTIVE) and not is_stale(lease, now)


def effective_state(lease: dict, now: float) -> str:
    """The state to show: an active lease gone stale is restoring (the watcher is about to say so on disk)."""
    if lease.get("state") == STATE_ACTIVE and is_stale(lease, now):
        return STATE_RESTORING
    return str(lease.get("state"))


def apply_active_overlay(limits: dict) -> dict:
    """``engine.config.get_worker_limits()``'s hook: the limits with the active lease's overlay, or as they are."""
    lease = load_lease()
    if not overlay_applies(lease, now()):
        return limits
    overlay = lease.get("overlay") or {}
    if overlay.get("layout_keys"):
        # A named layout (2.9.0): config.txt's keys with the layout's on top, parsed afresh. config.txt is not written.
        merged = engine_config.get_config()
        merged.update(overlay["layout_keys"])
        limits = engine_config.get_plain_worker_limits(merged)
    return overlay_limits(limits, overlay)


def model_for_restart(lease: Optional[dict], current: str) -> str:
    """The model the engine restarts on: the layout's while a layout lease is granting or active, config.txt's own
    DEFAULT_MODEL (else ``current``) when that lease is being given back, and ``current`` for everything else."""
    overlay = (lease or {}).get("overlay") or {}
    layout = overlay.get("layout_model")
    if not layout:
        return current
    if lease.get("state") in (STATE_GRANTING, STATE_ACTIVE):
        return layout
    return os.path.basename(str(engine_config.get_config().get("DEFAULT_MODEL") or "").strip()) or current


def status_block() -> Dict[str, Any]:
    """
    ``takeover`` in /v1/cluster/status and the admin stats: no lease id, no address. ``layout`` (2.9.0) is the owner's
    named layout the lease runs on (e.g. "takeover-1card"), or None for a generic plan.
    """
    lease = load_lease()
    if not lease:
        return {"active": False, "state": None, "holder": None, "since": None, "expires_at": None, "layout": None}
    current = now()
    return {"active": True, "state": effective_state(lease, current), "holder": lease.get("holder"),
            "since": iso(float(lease.get("since") or 0)), "expires_at": iso(float(lease.get("expires_at") or 0)),
            "layout": (lease.get("overlay") or {}).get("layout")}


def purge_on_start() -> Optional[str]:
    """
    At the coordinator's start: a lease file that is not a live, active lease (expired, no heartbeat for 45 s, still
    "granting" or "restoring" from a process that is gone) is removed, and the RAM limits it lowered are put back.
    Returns a sentence for the log, or None when there was nothing to remove. A live lease is kept: the engine this
    start brings up is built from its overlay.
    """
    lease = load_lease()
    if lease is None:
        if os.path.exists(lease_path()):
            delete_lease()          # a file that cannot be read: nothing can be restored from it, and nothing applies it
            return "A takeover lease file that could not be read was removed."
        return None
    if lease.get("state") == STATE_ACTIVE and not is_stale(lease, now()):
        return None
    restored = _restore_memory(lease)
    delete_lease()
    return (f"A stale takeover lease ({lease.get('state')}, held by {lease.get('holder')!r}) was removed at start"
            f"{'' if restored else '; its RAM limits could not be put back'}.")


# --------------------------------------------------------------------------------------
# RAM: systemctl --user set-property --runtime
# --------------------------------------------------------------------------------------

_UINT64_MAX = 2 ** 64 - 1


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=15)


def read_memory_limits() -> Optional[Dict[str, str]]:
    """The unit's MemoryMax and MemoryHigh as systemd shows them (a byte count, or ``infinity``), or None when they
    cannot be read here (no systemd user manager, no such unit). The one place the coordinator asks; tests replace it."""
    try:
        result = _systemctl("show", SERVICE_UNIT, "-p", "MemoryMax", "-p", "MemoryHigh")
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    values: Dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        if key in ("MemoryMax", "MemoryHigh"):
            value = value.strip()
            if not value or value == "[not set]" or (value.isdigit() and int(value) >= _UINT64_MAX):
                value = "infinity"
            values[key] = value
    return values if set(values) == {"MemoryMax", "MemoryHigh"} else None


def set_memory_limits(values: Dict[str, str]) -> bool:
    """``systemctl --user set-property --runtime dlne.service MemoryMax=… MemoryHigh=…``: runtime only, so a reboot can
    never leave the limit lowered. True when systemd took it. The one place the coordinator sets it; tests replace it."""
    try:
        result = _systemctl("set-property", "--runtime", SERVICE_UNIT,
                            f"MemoryMax={values['MemoryMax']}", f"MemoryHigh={values['MemoryHigh']}")
    except (OSError, subprocess.SubprocessError, KeyError):
        return False
    if result.returncode != 0:
        log_warning(f"systemctl set-property failed: {(result.stderr or result.stdout).strip()[:200]}")
    return result.returncode == 0


def _restore_memory(lease: dict) -> bool:
    """Puts the RAM limits a lease lowered back (three tries); True when there was nothing to put back or it worked."""
    memory = lease.get("memory") or {}
    if not memory.get("applied"):
        return True
    for _ in range(3):
        if set_memory_limits(memory.get("before") or {}):
            return True
        time.sleep(0.2)
    log_error("Takeover: the unit's MemoryMax/MemoryHigh could not be put back "
              f"({memory.get('before')}); they are runtime-only and a reboot clears them.")
    return False


def _bytes(value: str) -> Optional[int]:
    return int(value) if isinstance(value, str) and value.isdigit() else None


# --------------------------------------------------------------------------------------
# CPU cores and masks
# --------------------------------------------------------------------------------------

def logical_cpu_count() -> int:
    return os.cpu_count() or 1


def _parse_cpu_list(text: str) -> Set[int]:
    """``"4,10"`` or ``"4-5,10-11"`` (the kernel's list format) as a set."""
    found: Set[int] = set()
    for part in text.strip().split(","):
        part = part.strip()
        if not part:
            continue
        low, _, high = part.partition("-")
        found.update(range(int(low), int(high or low) + 1))
    return found


def cpu_siblings(cpu: int, ncpu: int) -> Set[int]:
    """The logical CPUs sharing a core with ``cpu`` (itself included): from the kernel's topology when it is there,
    else TheWall's rule, CPU n and n+6 are one core."""
    path = os.path.join(CPU_SYSFS, f"cpu{cpu}", "topology", "thread_siblings_list")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            found = _parse_cpu_list(handle.read())
        if cpu in found:
            return found
    except (OSError, ValueError):
        pass
    sibling = cpu + SMT_FALLBACK_OFFSET if cpu + SMT_FALLBACK_OFFSET < ncpu else cpu - SMT_FALLBACK_OFFSET
    return {cpu} | ({sibling} if 0 <= sibling < ncpu else set())


def cpus_to_free(cores: Iterable[int], ncpu: int) -> List[int]:
    """The logical CPUs a lease of ``cores`` takes: the cores and their SMT siblings, sorted."""
    taken: Set[int] = set()
    for core in cores:
        taken |= cpu_siblings(core, ncpu)
    return sorted(taken)


def parse_mask(text: str) -> Optional[int]:
    """A hex CPU mask (``0x3c`` or ``3c``) as a number; None for anything else."""
    text = (text or "").strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    if not text or any(c not in "0123456789abcdef" for c in text):
        return None
    return int(text, 16)


def all_cpus_mask(ncpu: int) -> int:
    return (1 << ncpu) - 1


def mask_without(text: str, remove: Iterable[int], ncpu: int) -> str:
    """``text`` (a hex mask; "" means every CPU) with the ``remove`` CPUs cleared, as ``0x…``. ValueError when no CPU
    would be left."""
    mask = parse_mask(text) if (text or "").strip() else all_cpus_mask(ncpu)
    if mask is None:
        mask = all_cpus_mask(ncpu)
    for cpu in remove:
        mask &= ~(1 << cpu)
    if mask == 0:
        raise ValueError("no CPU would be left")
    return f"0x{mask:x}"


def args_without_cpus(args: Sequence[str], remove: Iterable[int], ncpu: int) -> List[str]:
    """
    ENGINE_EXTRA_ARGS (already parsed) with the ``remove`` CPUs out of the engine's mask: every ``--cpu-mask`` / ``-C``
    (and ``-Cb`` / ``--cpu-mask-batch``) loses them, and when the engine had no mask it gets one for every other CPU.
    A thread count above the CPUs left is lowered to them. ValueError when no CPU would be left.
    """
    remove = list(remove)
    out: List[str] = []
    saw_main = False
    i = 0
    args = list(args)
    while i < len(args):
        flag = args[i]
        if flag in _MAIN_MASK_FLAGS + _BATCH_MASK_FLAGS and i + 1 < len(args):
            out += [flag, mask_without(args[i + 1], remove, ncpu)]
            saw_main = saw_main or flag in _MAIN_MASK_FLAGS
            i += 2
        else:
            out.append(flag)
            i += 1
    if not saw_main:
        out += ["--cpu-mask", mask_without("", remove, ncpu)]
    left = bin(parse_mask(_main_mask(out)) or 0).count("1")
    for j, flag in enumerate(out[:-1]):
        if flag in _THREAD_FLAGS and out[j + 1].isdigit() and int(out[j + 1]) > left:
            out[j + 1] = str(left)
    return out


def _main_mask(args: Sequence[str]) -> str:
    for j, flag in enumerate(args[:-1]):
        if flag in _MAIN_MASK_FLAGS:
            return args[j + 1]
    return ""


# --------------------------------------------------------------------------------------
# The request
# --------------------------------------------------------------------------------------

@dataclass
class Request:
    gpus: List[str] = field(default_factory=list)
    cpu_cores: List[int] = field(default_factory=list)
    ram_gb: float = 0.0
    coding_lanes: int = DEFAULT_CODING_LANES
    coding_context: int = DEFAULT_CODING_CONTEXT
    ttl_seconds: int = DEFAULT_TTL_SECONDS
    holder: str = ""
    reason: str = ""


def _clean_text(value: Any, limit: int) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(value)).strip()[:limit]


def _bad(detail: str) -> TakeoverError:
    return TakeoverError(400, CODE_BAD_REQUEST, detail)


def _whole_number(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, float) and value == int(value):
            return int(value)
        raise _bad(f"{name} must be a whole number.")
    return value


def parse_request(body: Any) -> Request:
    """The request body as a ``Request``; TakeoverError (400, takeover_bad_request) when it is not what the contract
    describes. Which cards and cores exist is the planner's to say, not this."""
    if not isinstance(body, dict):
        raise _bad("Send a JSON object.")
    req = Request()
    gpus = body.get("gpus", [])
    if not isinstance(gpus, list) or not all(isinstance(g, str) for g in gpus):
        raise _bad("gpus must be a list of PCI addresses such as \"0000:03:00.0\".")
    seen: List[str] = []
    for spec in gpus:
        spec = spec.strip().lower()
        if spec and spec not in seen:
            seen.append(spec)
    req.gpus = seen

    cores = body.get("cpu_cores", [])
    if not isinstance(cores, list):
        raise _bad("cpu_cores must be a list of core numbers.")
    req.cpu_cores = sorted({_whole_number(c, "cpu_cores entries") for c in cores})
    if any(c < 0 for c in req.cpu_cores):
        raise _bad("cpu_cores entries cannot be negative.")

    ram = body.get("ram_gb", 0)
    if isinstance(ram, bool) or not isinstance(ram, (int, float)) or not math.isfinite(ram) or ram < 0:
        raise _bad("ram_gb must be a number, zero or more.")
    req.ram_gb = float(ram)

    keep = body.get("keep", {})
    if keep is None:
        keep = {}
    if not isinstance(keep, dict):
        raise _bad("keep must be an object such as {\"coding_lanes\": 1, \"coding_context\": 32768}.")
    req.coding_lanes = _whole_number(keep.get("coding_lanes", DEFAULT_CODING_LANES), "keep.coding_lanes")
    req.coding_context = _whole_number(keep.get("coding_context", DEFAULT_CODING_CONTEXT), "keep.coding_context")
    if not 1 <= req.coding_lanes <= MAX_CODING_LANES:
        raise _bad(f"keep.coding_lanes must be 1 to {MAX_CODING_LANES}.")
    if not MIN_CODING_CONTEXT <= req.coding_context <= MAX_CODING_CONTEXT:
        raise _bad(f"keep.coding_context must be {MIN_CODING_CONTEXT} to {MAX_CODING_CONTEXT} tokens.")

    req.ttl_seconds = _whole_number(body.get("ttl_seconds", DEFAULT_TTL_SECONDS), "ttl_seconds")
    if not MIN_TTL_SECONDS <= req.ttl_seconds <= MAX_TTL_SECONDS:
        raise _bad(f"ttl_seconds must be {MIN_TTL_SECONDS} to {MAX_TTL_SECONDS}.")
    req.holder = _clean_text(body.get("holder") or "unnamed holder", 80) or "unnamed holder"
    req.reason = _clean_text(body.get("reason") or "", 200)
    if not (req.gpus or req.cpu_cores or req.ram_gb):
        raise _bad("Name something to take: gpus, cpu_cores or ram_gb.")
    return req


# --------------------------------------------------------------------------------------
# Named layouts (2.9.0, docs/takeover-contract.md addendum)
# --------------------------------------------------------------------------------------

LAYOUT_ONE_CARD = "takeover-1card"
LAYOUT_TWO_CARDS = "takeover-2card"
LAYOUT_NORMAL = "normal-layout"
BC160_MARK = "BC-160"
# Never taken from a layout file, even when it sets them: a lease must not touch maintenance, the operator's or the
# takeover key, the layouts switch or the network identity.
LAYOUT_PROTECTED_KEYS = frozenset({"MAINTENANCE", "MAINTENANCE_MESSAGE", "MAINTENANCE_UNTIL", "TAKEOVER_KEY_HASH",
                                   "TAKEOVER_LAYOUTS", "ADMIN_HASH", "NETWORK_KEY"})


def layout_path(name: str) -> str:
    return os.path.join(engine_config.get_base_dir(), "config", f"config.txt.{name}")


def bc160_cards(gpus: List[dict]) -> List[str]:
    """PCI addresses of the BC-160 cards, #1 first (lowest address)."""
    return sorted(_pci(g) for g in gpus if BC160_MARK in str(g.get("name") or ""))


def layout_for(req: "Request", gpus: List[dict]) -> Optional[str]:
    """The named layout a request asks for by the cards it names, or None (generic plan)."""
    if not req.gpus:
        return None
    cards = bc160_cards(gpus)
    asked = set(req.gpus)
    if len(cards) >= 2 and asked == set(cards[:2]):
        return LAYOUT_TWO_CARDS
    if cards and asked == {cards[0]}:
        return LAYOUT_ONE_CARD
    return None


def read_layout_keys(name: str) -> Optional[Dict[str, str]]:
    """The keys of ``config/config.txt.<name>`` less the protected ones, or None (missing or unreadable)."""
    try:
        with open(layout_path(name), "r", encoding="utf-8", errors="replace") as handle:
            parsed = engine_config._parse_config_lines(handle.read().splitlines())
    except OSError:
        return None
    return {k: v for k, v in parsed.items() if k not in LAYOUT_PROTECTED_KEYS} or None


def merged_config(layout_keys: Dict[str, str]) -> Dict[str, str]:
    """config.txt's keys with the layout's on top (keys the layout does not set stay)."""
    merged = engine_config.get_config()
    merged.update(layout_keys)
    return merged


def layout_problem(layout_keys: Dict[str, str]) -> Optional[str]:
    """Why a layout cannot be used, or None: ONE disallowed engine flag drops the whole ENGINE_EXTRA_ARGS line."""
    raw = merged_config(layout_keys).get("ENGINE_EXTRA_ARGS", "")
    if raw.strip() and not engine_config.parse_engine_args(raw):
        return "its ENGINE_EXTRA_ARGS holds a flag the engine does not allow, which would drop the whole line"
    return None


def _model_on_disk_default(tag: str) -> bool:
    """Standalone check (tests): the model is a file in the models folder. The coordinator replaces this with the
    inference manager's own resolver, which knows every root the engine searches."""
    name = os.path.basename(tag)
    root = str(engine_config.get_plain_worker_limits().get("custom_gguf_dir") or "")
    return bool(name) and os.path.isfile(os.path.join(root, name))


model_on_disk: Callable[[str], bool] = _model_on_disk_default


def layout_model(keys: Dict[str, str]) -> str:
    """The model file name a layout serves, or "" when it does not name one (the engine keeps its model)."""
    return os.path.basename(str(keys.get("DEFAULT_MODEL") or "").strip())


def choose_layout(req: "Request", gpus: List[dict]) -> Optional[Tuple[str, Dict[str, str]]]:
    """(name, keys) of the layout this lease applies, or None for the generic plan (with a log line when a layout was
    asked for but cannot be used)."""
    if not engine_config.get_config().get("TAKEOVER_LAYOUTS", "true").strip().lower() == "true":
        return None
    name = layout_for(req, gpus)
    if name is None:
        return None
    keys = read_layout_keys(name)
    why = "the file is missing or empty" if keys is None else layout_problem(keys)
    if not why and layout_model(keys) and not model_on_disk(layout_model(keys)):
        why = f"its model {layout_model(keys)} is not on disk"
    if why:
        log_warning(f"Takeover: layout {name} not used ({why}); using the generic plan.")
        return None
    return name, keys


# --------------------------------------------------------------------------------------
# The planner
# --------------------------------------------------------------------------------------

@dataclass
class EngineFacts:
    """What the coordinator knows of the running coding engine, for the planner's arithmetic."""
    model: str
    weights_gb: float                   # what the model charges the cards now (inference_manager.estimate_model_vram_gb)
    unplaced_weights_gb: float          # the same with no --n-cpu-moe placement: the file and the runtime's buffers
    kv_f16_bytes_per_token: int
    state_bytes_per_slot: int
    native_context: int
    slots: Optional[int] = None         # the running engine's coding lanes
    context_per_slot: Optional[int] = None
    phone_slots: int = 0
    phone_context: int = 8192
    kv_type: str = "auto"


@dataclass
class Plan:
    allowed: bool
    problems: List[Dict[str, str]]
    cards: List[Dict[str, Any]]
    keep_result: Dict[str, Any]
    cpu_cores: List[int]
    ram_gb: float
    # What the grant needs and the answer does not show:
    overlay: Dict[str, Any] = field(default_factory=dict)
    memory: Optional[Dict[str, Any]] = None        # {"before": {...}, "after": {...}} when RAM is taken
    engine_restart: bool = False
    pause_pictures: bool = False
    pause_voice: bool = False          # 2.8.0: the voice engine (whisper-server) shares the media card
    # 2.9.0: the phones' own engine (engine/phone_engine.py) is stopped for the lease: its card is lent (the media card,
    # or the card PHONE_DEVICE names), or the named layout turns PHONE_ENGINE=own off.
    stop_phone_engine: bool = False
    removed_vram_gb: float = 0.0
    request: Optional[Request] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"allowed": self.allowed, "problems": self.problems, "cards": self.cards,
                "keep_result": self.keep_result, "cpu_cores": self.cpu_cores, "ram_gb": self.ram_gb}

    def first_error(self) -> Optional[TakeoverError]:
        if self.allowed or not self.problems:
            return None
        first = self.problems[0]
        status = {CODE_ACTIVE: 409, CODE_BUSY: 503}.get(first["code"], 400)
        return TakeoverError(status, first["code"], first["detail"])


def read_free_vram_gb(pci: str, fallback: Optional[float] = None) -> Optional[float]:
    """A card's free VRAM now, read from amdgpu's sysfs counters beside its PCI address; ``fallback`` where there are none."""
    folder = os.path.join(PCI_SYSFS, pci or "-")
    try:
        with open(os.path.join(folder, "mem_info_vram_total"), encoding="utf-8") as handle:
            total = handle.read().strip()
        with open(os.path.join(folder, "mem_info_vram_used"), encoding="utf-8") as handle:
            used = handle.read().strip()
        if total.isdigit() and used.isdigit():
            return max(0.0, (int(total) - int(used)) / (1024 ** 3))
    except OSError:
        pass
    return fallback


def card_name(gpu: Dict[str, Any]) -> str:
    return str(gpu.get("name") or "GPU")


def _pci(gpu: Dict[str, Any]) -> str:
    return str(gpu.get("address") or gpu.get("id"))


def engine_cards(limits: dict, gpus: List[dict]) -> List[dict]:
    """The cards the coordinator's engine runs on, as engine_gpu_devices would list them: ALLOWED_GPU_INDICES (or every
    discrete card), less the media card unless it is shared. As in inference_manager._engine_devices, a media card that
    is the only card left IS the engine's (TheWall since 2026-11-01: the 9B alone on the RX 580): leaving it out here
    would hand the engine's own card to a lease as a media card, with the engine still on it."""
    from engine import hardware
    allowed = str(limits.get("allowed_gpu_indices", "-1")).strip().lower()
    if allowed == "none":
        return []
    if allowed == "-1" or not allowed:
        chosen = [g for g in gpus if hardware.counts_as_gpu(g.get("name") or "", g.get("integrated"))]
    else:
        chosen = hardware.select_gpus(allowed, gpus)
    media = media_card_of(limits, gpus)
    if media is not None and not limits.get("media_gpu_shared"):
        without_media = [g for g in chosen if g["id"] != media["id"]]
        if without_media or not any(g["id"] == media["id"] for g in chosen):
            return without_media
        return [media]                  # nothing else to run the model on: the media card is the engine's after all
    return chosen


def media_card_of(limits: dict, gpus: List[dict]) -> Optional[dict]:
    from engine import hardware
    spec = str(limits.get("media_gpu") or "").strip()
    return hardware.find_gpu(spec, gpus) if spec else None


def card_view(gpu: Dict[str, Any], action: str) -> Dict[str, Any]:
    """One entry of ``cards``, with the card's free VRAM read now."""
    pci = _pci(gpu)
    free = read_free_vram_gb(pci, gpu.get("vram_free_gb"))
    return {"pci": pci, "vulkan_index": int(gpu.get("vk_index", gpu.get("id", 0))), "name": card_name(gpu),
            "vram_gb": round(float(gpu.get("vram_total_gb") or 0.0), 1),
            "free_vram_gb": round(float(free if free is not None else 0.0), 2), "action": action}


def refresh_cards(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The cards stored in a lease, with ``free_vram_gb`` read now (the rest was fixed at the grant)."""
    out = []
    for card in cards:
        card = dict(card)
        free = read_free_vram_gb(card["pci"], card.get("free_vram_gb"))
        card["free_vram_gb"] = round(float(free if free is not None else 0.0), 2)
        out.append(card)
    return out


def _fit_engine(facts: EngineFacts, lanes: int, floor: int, vram_gb: float, sysram_gb: float) -> Tuple[bool, str, int, float]:
    """
    Whether the engine, moved to ``vram_gb`` of cards (0: the CPU) with ``sysram_gb`` of RAM, keeps ``lanes`` coding lanes
    of ``floor`` tokens, by the same admission and sizing rules the inference manager applies at its start.
    Returns ``(fits, why_not, context_per_lane, ram_needed_gb)``.
    """
    from engine.inference_manager import (AUTO_KV_CLAIM_FRACTION, MIN_KV_GB, POOL_SAFETY_FRACTION,
                                          plan_slots_and_context)

    weights = facts.unplaced_weights_gb
    shape = dict(kv_f16_bytes_per_token=facts.kv_f16_bytes_per_token, state_bytes_per_slot=facts.state_bytes_per_slot,
                 native_context=facts.native_context, requested_slots=lanes, fixed_slots=lanes,
                 phone_slots=facts.phone_slots, phone_context=facts.phone_context, kv_type=facts.kv_type)
    if vram_gb <= 0:
        sized = plan_slots_and_context(budget_gb=0.0, fixed_context=lanes * floor, **shape)
        need = weights + sized.kv_gb
        if need > sysram_gb * POOL_SAFETY_FRACTION:
            return False, (f"on the CPU the kept engine needs about {need:.1f} GB of RAM and DLNE would have "
                           f"{sysram_gb * POOL_SAFETY_FRACTION:.1f} GB"), sized.context_per_slot, need + HOST_OVERHEAD_GB
        return True, "", sized.context_per_slot, need + HOST_OVERHEAD_GB
    vram_budget = vram_gb * POOL_SAFETY_FRACTION
    if weights + MIN_KV_GB <= vram_budget:
        kv_budget, spilled, on_cards = (vram_budget - weights) * AUTO_KV_CLAIM_FRACTION, 0.0, True
    else:
        free = (vram_gb + sysram_gb) * POOL_SAFETY_FRACTION - weights
        if free < MIN_KV_GB:
            return False, (f"the model needs about {weights:.1f} GB and the cards left ({vram_gb:.1f} GB) with RAM "
                           f"({sysram_gb:.1f} GB) cannot hold it"), 0, weights + HOST_OVERHEAD_GB
        kv_budget, spilled, on_cards = max(MIN_KV_GB, free * 0.30), max(0.0, weights - vram_budget), False
    sized = plan_slots_and_context(budget_gb=kv_budget, fixed_context=0, **shape)
    need = HOST_OVERHEAD_GB + spilled + (0.0 if on_cards else sized.kv_gb)
    if sized.context_per_slot < floor:
        return False, (f"the kept engine would hold {sized.context_per_slot} tokens a lane on the cards left, under the "
                       f"{floor} asked"), sized.context_per_slot, need
    return True, "", sized.context_per_slot, need


def make_plan(req: Request, *, limits: dict, gpus: List[dict], facts: Optional[EngineFacts],
              memory_now: Optional[Dict[str, str]], system_ram_gb: float, lease_active: bool = False,
              ncpu: Optional[int] = None) -> Plan:
    """
    What a lease for ``req`` would do. ``limits`` is the plain ``config.txt`` view (no overlay), ``gpus`` the machine's
    cards (hardware.get_all_gpus), ``facts`` the running engine (None when there is none), ``memory_now`` the unit's
    memory limits (None: cannot be set here) and ``system_ram_gb`` the machine's RAM. Changes nothing.
    """
    from engine import hardware
    ncpu = ncpu or logical_cpu_count()
    problems: List[Dict[str, str]] = []
    plain_limits = limits
    layout = choose_layout(req, gpus)
    if layout is not None:
        # The plan is the layout's: its limits are what DLNE will be, the named cards are handed over as they are.
        limits = engine_config.get_plain_worker_limits(merged_config(layout[1]))

    def problem(code: str, detail: str) -> None:
        problems.append({"code": code, "detail": detail})

    if lease_active:
        problem(CODE_ACTIVE, "A takeover lease is already active (or being given back); one lease at a time.")

    # --- the cards ---
    engine = engine_cards(limits, gpus)
    engine_ids = {g["id"] for g in engine}
    media = media_card_of(limits, gpus)
    shared = bool(limits.get("media_gpu_shared"))
    taken: List[dict] = []
    for spec in req.gpus:
        gpu = hardware.find_gpu(spec, gpus) if ":" in spec else None
        if gpu is None:
            problem(CODE_UNKNOWN_GPU, f"No card at {spec} on this machine (a PCI address such as 0000:03:00.0 is needed).")
        elif gpu["id"] not in {t["id"] for t in taken}:
            taken.append(gpu)
    media_taken = media is not None and media["id"] in {g["id"] for g in taken}
    taken_engine = [g for g in taken if g["id"] in engine_ids]
    remaining = [g for g in engine if g["id"] not in {t["id"] for t in taken}]
    engine_moves = bool(taken_engine)

    cards: List[Dict[str, Any]] = []
    for gpu in taken:
        if gpu["id"] in engine_ids:
            action = ACTION_ENGINE
        elif media is not None and gpu["id"] == media["id"]:
            action = ACTION_MEDIA
        else:
            action = ACTION_HANDOVER
        cards.append(card_view(gpu, action))
    # the cards in the request's order (taken is that order, less duplicates and unknowns)

    # --- vision and pictures ---
    vision_configured = bool(str(limits.get("engine_mmproj") or "").strip())
    engine_on_cpu = engine_moves and not remaining
    vision_cpu = vision_configured and (media_taken or engine_on_cpu)
    pause_pictures = media_taken and bool(limits.get("image_gen_enabled"))      # already paused or off: nothing to pause
    if limits.get("image_gen_paused") or pause_pictures:
        pictures = "paused"
    else:
        pictures = "on" if limits.get("image_gen_enabled") else "off"
    vision = "cpu" if vision_cpu else ("gpu" if vision_configured else "off")
    # Voice (2.8.0) shares the media card with pictures and is paused the same way. Mentioned in dlne_now only where the
    # operator has set voice up at all (on or paused), so a server without it answers exactly as before.
    voice_set_up = bool(limits.get("voice_enabled") or limits.get("voice_paused"))
    pause_voice = media_taken and bool(limits.get("voice_enabled"))
    phone_own = str(plain_limits.get("phone_engine") or "shared") == "own"
    phone_card = str(plain_limits.get("phone_device") or "").strip()
    if not phone_card:
        phone_card_taken = media_taken
    elif phone_card.lower() == "cpu":
        phone_card_taken = False
    else:                                   # PHONE_DEVICE is a DLNE GPU id or a PCI address, as MEDIA_GPU is
        named = hardware.find_gpu(phone_card, gpus)
        phone_card_taken = named is not None and named["id"] in {g["id"] for g in taken}
    stop_phone = phone_own and phone_card_taken
    voice = "paused" if (limits.get("voice_paused") or pause_voice) else "on"

    # --- CPU cores ---
    overlay: Dict[str, Any] = {}
    removed: List[int] = []
    if req.cpu_cores:
        missing = [c for c in req.cpu_cores if c >= ncpu]
        if missing:
            problem(CODE_TOO_MUCH, f"This machine has CPUs 0 to {ncpu - 1}; there is no core {missing[0]}.")
        else:
            removed = cpus_to_free(req.cpu_cores, ncpu)
            try:
                args_without_cpus(limits.get("engine_extra_args") or (), removed, ncpu)
                mask_without(str(limits.get("image_cpu_mask") or ""), removed, ncpu)
            except ValueError:
                problem(CODE_TOO_MUCH, "Those cores (with their SMT siblings) are every core DLNE's engine may use.")
                removed = []
    if removed:
        overlay["remove_cpus"] = removed

    # --- the engine's new place, and what it keeps ---
    lanes, floor = req.coding_lanes, req.coding_context
    engine_restart = bool(facts) and (engine_moves or (media_taken and vision_configured) or bool(removed))
    keep_lanes: Optional[int] = facts.slots if facts else None
    keep_context: Optional[int] = facts.context_per_slot if facts else None
    engine_on = [_pci(g) for g in (remaining if engine_moves else engine)]
    memory_after_ram = max(0.0, system_ram_gb - req.ram_gb)
    ram_need = HOST_OVERHEAD_GB
    removed_vram = sum(float(g.get("vram_total_gb") or 0.0) for g in taken_engine)
    if engine_moves:
        overlay["allowed_gpu_indices"] = ",".join(_pci(g) for g in remaining) if remaining else "none"
        overlay["engine_slots"] = lanes
        overlay["engine_context"] = lanes * floor if not remaining else 0
        overlay["drop_placement_args"] = True
        keep_lanes, keep_context = lanes, floor
        if facts is not None:
            vram_left = sum(float(g.get("vram_total_gb") or 0.0) for g in remaining)
            fits, why, per_lane, ram_need = _fit_engine(facts, lanes, floor, vram_left, memory_after_ram)
            keep_context = per_lane if per_lane else floor
            if not fits:
                problem(CODE_TOO_MUCH, f"What is left cannot keep {lanes} lane(s) of {floor} tokens: {why}.")
    if vision_cpu:
        overlay["vision_cpu"] = True
    if pause_pictures:
        overlay["pictures_paused"] = True
    if pause_voice:
        overlay["voice_paused"] = True
    if facts is not None and not engine_moves:
        # The engine stays on its cards; whatever of the model already lives in RAM has to stay inside the new limit.
        vram_now = sum(float(g.get("vram_total_gb") or 0.0) for g in engine)
        ram_need += max(0.0, facts.weights_gb - vram_now * 0.90)

    # --- RAM ---
    memory: Optional[Dict[str, Any]] = None
    if req.ram_gb > 0:
        if memory_now is None:
            problem(CODE_TOO_MUCH, "DLNE cannot lower its own memory limit on this host (no systemd user unit "
                                   f"{SERVICE_UNIT} to set it on).")
        else:
            gib = 1024 ** 3
            old_max = _bytes(memory_now.get("MemoryMax", "infinity"))
            old_high = _bytes(memory_now.get("MemoryHigh", "infinity"))
            base = old_max if old_max is not None else int(system_ram_gb * gib)
            new_max = int(base - req.ram_gb * gib)
            if new_max < ram_need * gib:
                problem(CODE_TOO_MUCH, f"Lowering DLNE's memory by {req.ram_gb:g} GB would leave {max(new_max, 0) / gib:.1f} GB; "
                                       f"the kept engine needs about {ram_need:.1f} GB.")
            else:
                new_high = int(new_max * MEMORY_HIGH_FRACTION)
                if old_high is not None:
                    new_high = min(new_high, old_high)
                memory = {"before": {"MemoryMax": memory_now["MemoryMax"], "MemoryHigh": memory_now["MemoryHigh"]},
                          "after": {"MemoryMax": str(new_max), "MemoryHigh": str(new_high)}}

    if layout is not None:
        overlay["layout"] = layout[0]
        overlay["layout_keys"] = dict(layout[1])
        if layout_model(layout[1]):
            overlay["layout_model"] = layout_model(layout[1])
        engine_restart = bool(facts)
        pause_pictures = bool(plain_limits.get("image_gen_enabled")) and not limits.get("image_gen_enabled")
        pause_voice = bool(plain_limits.get("voice_enabled")) and not limits.get("voice_enabled")
        stop_phone = stop_phone or (phone_own and str(limits.get("phone_engine") or "shared") != "own")
        slots, total = int(limits.get("engine_slots") or 0), int(limits.get("engine_context") or 0)
        if slots > 0:
            keep_lanes = slots
            keep_context = total // slots if total > 0 else keep_context
    keep_result = {"engine_on": engine_on, "coding_lanes": keep_lanes, "coding_context": keep_context,
                   "pictures": pictures, "vision": vision, "engine_restart": engine_restart}
    if layout is not None:
        keep_result["layout"] = layout[0]
    if voice_set_up:
        keep_result["voice"] = voice
    return Plan(allowed=not problems, problems=problems, cards=cards, keep_result=keep_result,
                cpu_cores=list(req.cpu_cores), ram_gb=req.ram_gb, overlay=overlay, memory=memory,
                engine_restart=engine_restart, pause_pictures=pause_pictures, pause_voice=pause_voice,
                stop_phone_engine=stop_phone, removed_vram_gb=removed_vram, request=req)


# --------------------------------------------------------------------------------------
# The overlay
# --------------------------------------------------------------------------------------

def overlay_limits(limits: dict, overlay: Dict[str, Any], ncpu: Optional[int] = None) -> dict:
    """``limits`` with a lease's changes on top (a new dict; ``limits`` is not touched)."""
    ncpu = ncpu or logical_cpu_count()
    out = dict(limits)
    for key in ("allowed_gpu_indices", "engine_slots", "engine_context"):
        if key in overlay:
            out[key] = overlay[key]
    extra = list(out.get("engine_extra_args") or ())
    if overlay.get("drop_placement_args"):
        extra = engine_config.engine_args_without_placement(extra)
        out["engine_extra_args"] = extra
    remove = [int(c) for c in overlay.get("remove_cpus") or ()]
    if remove:
        try:
            out["engine_extra_args"] = args_without_cpus(extra, remove, ncpu)
            out["image_cpu_mask"] = mask_without(str(out.get("image_cpu_mask") or ""), remove, ncpu)
        except ValueError:
            log_warning("Takeover: the lease's cores would leave DLNE none; its CPU masks are left as configured.")
    if overlay.get("vision_cpu"):
        out["engine_mmproj_cpu"] = True
    if overlay.get("pictures_paused"):
        out["image_gen_enabled"] = False
        out["image_gen_paused"] = True
    if overlay.get("voice_paused"):
        out["voice_enabled"] = False
        out["voice_paused"] = True
    return out


# --------------------------------------------------------------------------------------
# Peer rule for the API (the coordinator applies it; the pure part lives here so a test can reach it)
# --------------------------------------------------------------------------------------

FORWARDING_HEADERS = ("forwarded", "via", "x-real-ip")


def came_through_a_proxy(headers: Any) -> bool:
    """Any header a reverse proxy adds: X-Forwarded-*, Forwarded, Via, X-Real-IP."""
    for name in headers.keys():
        lowered = name.lower()
        if lowered.startswith("x-forwarded-") or lowered in FORWARDING_HEADERS:
            return True
    return False


# Spelled out rather than ipaddress's is_private, which also counts the documentation ranges (192.0.2.0/24, 198.51.100.0/24,
# 203.0.113.0/24) and other special blocks that are nobody's LAN. 10.8.0.0/24, the WireGuard tunnel, is inside 10.0.0.0/8.
_LAN_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "::1/128", "fc00::/7", "fe80::/10"))


def peer_is_lan(peer: Optional[str]) -> bool:
    """The socket peer is loopback, on a private (LAN) range or in the WireGuard tunnel subnet."""
    try:
        address = ipaddress.ip_address(peer or "")
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    return any(address in network for network in _LAN_NETWORKS if network.version == address.version)


# --------------------------------------------------------------------------------------
# The controller
# --------------------------------------------------------------------------------------

@dataclass
class Host:
    """What the coordinator hands the controller: the things only it can see and do."""
    gpus: Callable[[], Awaitable[List[dict]]]
    engine_facts: Callable[[], Awaitable[Optional[EngineFacts]]]
    idle: Callable[[], bool]
    # restart_engine(expected_pool=(vram_gb, sysram_gb) or None, reduced=bool, patient=bool) -> {"outcome", "success",
    # "detail"}: the same model restarted on the limits as they are now. ``reduced``: the pool just got smaller;
    # ``patient``: a restore, which waits out other switches and may cut a request that never ends.
    restart_engine: Callable[..., Awaitable[Dict[str, Any]]]
    pool_totals: Callable[[], Tuple[float, float]]
    pause_pictures: Callable[[], Awaitable[None]]
    observe_engine: Callable[[], Awaitable[Optional[Tuple[int, int]]]]       # (coding lanes, context per lane) or None
    read_limits: Callable[[], dict] = engine_config.get_plain_worker_limits
    system_ram_gb: Callable[[], float] = lambda: 0.0
    # 2.9.0: stop the phones' own engine for a lease that takes its card (None: there is none to stop). The phone
    # switcher starts it again once the lease is over.
    stop_phone_engine: Optional[Callable[[], Awaitable[None]]] = None


class TakeoverController:
    """Grant, heartbeat, release and the watcher. All state is the lease file, so a restarted coordinator picks a live
    lease up where it was; the only thing in memory is that a grant or a restore is running."""

    def __init__(self, host: Host, *, clock: Callable[[], float] = lambda: now(),
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.host = host
        self.clock = clock
        self.sleep = sleep
        self._granting = False
        self._restore_task: Optional[asyncio.Task] = None
        self._tasks: Set[asyncio.Task] = set()
        self._lock = asyncio.Lock()

    # ----------------------------------------------------------------- views
    def current(self) -> Optional[dict]:
        return load_lease()

    def lease_busy(self) -> bool:
        return self._granting or load_lease() is not None

    def view(self, lease: dict) -> Dict[str, Any]:
        """A lease as the API answers it (grant, heartbeat and GET)."""
        return {"active": True, "lease_id": lease["lease_id"], "state": effective_state(lease, self.clock()),
                "holder": lease.get("holder"), "granted": lease.get("granted"), "dlne_now": lease.get("dlne_now"),
                "since": iso(float(lease.get("since") or 0)), "expires_at": iso(float(lease.get("expires_at") or 0)),
                "heartbeat_every_seconds": HEARTBEAT_EVERY_SECONDS, "cards": refresh_cards(lease.get("cards") or [])}

    def get(self) -> Dict[str, Any]:
        lease = load_lease()
        return self.view(lease) if lease else {"active": False}

    # ----------------------------------------------------------------- plan
    async def _plan(self, req: Request) -> Plan:
        gpus = await self.host.gpus()
        facts = await self.host.engine_facts()
        memory_now = await asyncio.to_thread(read_memory_limits) if req.ram_gb > 0 else None
        return make_plan(req, limits=self.host.read_limits(), gpus=gpus, facts=facts, memory_now=memory_now,
                         system_ram_gb=self.host.system_ram_gb(), lease_active=self.lease_busy())

    async def plan(self, body: Any) -> Dict[str, Any]:
        """The dry run. Changes nothing."""
        return (await self._plan(parse_request(body))).to_dict()

    # ----------------------------------------------------------------- idle
    async def _idle_moment(self) -> bool:
        """Two idle checks IDLE_CHECK_GAP_SECONDS apart, within IDLE_WAIT_MAX_SECONDS. False when there was none."""
        deadline = self.clock() + IDLE_WAIT_MAX_SECONDS
        while True:
            first = self.host.idle()
            await self.sleep(IDLE_CHECK_GAP_SECONDS)
            if first and self.host.idle():
                return True
            if self.clock() >= deadline:
                return False

    async def _restart_patiently(self, expected_pool: Tuple[float, float]) -> Dict[str, Any]:
        """The engine restarted on the plain limits; another switch under way is waited out (it answers ``busy``)."""
        result: Dict[str, Any] = {"outcome": "busy", "success": False, "detail": "another switch kept running"}
        for _ in range(RESTART_ATTEMPTS):
            result = await self.host.restart_engine(expected_pool=expected_pool, reduced=False, patient=True)
            if result.get("outcome") != "busy":
                break
            await self.sleep(IDLE_CHECK_GAP_SECONDS)
        return result

    # ----------------------------------------------------------------- grant
    async def grant(self, body: Any) -> Dict[str, Any]:
        req = parse_request(body)
        if self._granting or load_lease() is not None:
            raise TakeoverError(409, CODE_ACTIVE, "A takeover lease is already active (or being given back); one lease at a time.")
        self._granting = True
        # Run to the end even if the caller goes away: a half-made grant would leave the hardware changed.
        task = asyncio.ensure_future(self._grant(req))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return await asyncio.shield(task)

    async def _grant(self, req: Request) -> Dict[str, Any]:
        try:
            plan = await self._plan_for_grant(req)
            # Whatever stops something that is running waits for DLNE to be idle: the engine restarting, or sd-server
            # letting go of the media card (the idle test includes a picture being made).
            needs_idle = plan.engine_restart or plan.pause_pictures or plan.pause_voice or plan.stop_phone_engine
            if needs_idle and not await self._idle_moment():
                raise TakeoverError(503, CODE_BUSY, f"DLNE was never idle in {IDLE_WAIT_MAX_SECONDS // 60} minutes; nothing was changed.")
            if needs_idle:
                plan = await self._plan_for_grant(req)       # an idle moment later: look again
            return await self._apply(req, plan)
        finally:
            self._granting = False

    async def _plan_for_grant(self, req: Request) -> Plan:
        gpus = await self.host.gpus()
        facts = await self.host.engine_facts()
        memory_now = await asyncio.to_thread(read_memory_limits) if req.ram_gb > 0 else None
        plan = make_plan(req, limits=self.host.read_limits(), gpus=gpus, facts=facts, memory_now=memory_now,
                         system_ram_gb=self.host.system_ram_gb(), lease_active=False)
        error = plan.first_error()
        if error is not None:
            raise error
        return plan

    async def _apply(self, req: Request, plan: Plan) -> Dict[str, Any]:
        now = self.clock()
        vram_before, sysram_before = self.host.pool_totals()
        lease: Dict[str, Any] = {
            "version": 1, "lease_id": "tk_" + secrets.token_hex(16), "state": STATE_GRANTING,
            "holder": req.holder, "reason": req.reason,
            "granted": {"gpus": [c["pci"] for c in plan.cards], "cpu_cores": plan.cpu_cores, "ram_gb": plan.ram_gb},
            "keep": {"coding_lanes": req.coding_lanes, "coding_context": req.coding_context},
            "overlay": plan.overlay, "cards": plan.cards, "engine_restart": plan.engine_restart,
            "memory": {"applied": False, **(plan.memory or {})},
            "pool_before": {"vram_gb": vram_before, "sysram_gb": sysram_before},
            "dlne_now": dict(plan.keep_result), "ttl_seconds": req.ttl_seconds,
            "since": now, "expires_at": now + req.ttl_seconds, "last_heartbeat": now,
        }
        lease["dlne_now"].pop("engine_restart", None)
        save_lease(lease)              # first: a crash from here on is put right at the next start
        log_coordinator(f"Takeover: granting a lease to {req.holder!r} ({len(plan.cards)} card(s), cores {plan.cpu_cores}, "
                        f"{plan.ram_gb:g} GB of RAM).")
        backed_out = False
        engine_touched = False
        try:
            if plan.memory:
                if not await asyncio.to_thread(set_memory_limits, plan.memory["after"]):
                    raise TakeoverError(503, CODE_FAILED, "systemd would not lower DLNE's memory limit; nothing was changed.")
                lease["memory"]["applied"] = True
                save_lease(lease)
            if plan.pause_pictures or plan.pause_voice:
                await self.host.pause_pictures()          # the host lets go of the media card: sd-server and whisper-server
            if plan.stop_phone_engine and self.host.stop_phone_engine is not None:
                await self.host.stop_phone_engine()       # 2.9.0: the phones' own llama-server lets go of its card
            if plan.engine_restart:
                expected = (max(0.0, vram_before - plan.removed_vram_gb), max(0.0, sysram_before - plan.ram_gb))
                engine_touched = True
                result = await self.host.restart_engine(expected_pool=expected, reduced=True, patient=False)
                if result.get("outcome") != "swapped" or not result.get("success"):
                    outcome = result.get("outcome")
                    backed_out = True
                    await self._back_out(lease, restart_needed=outcome != "busy")
                    if outcome == "busy":
                        raise TakeoverError(503, CODE_BUSY, f"DLNE was not idle ({result.get('detail')}); nothing was changed.")
                    raise TakeoverError(503, CODE_FAILED, "DLNE's engine did not come up on the reduced hardware "
                                                          f"({result.get('detail')}); it was put back as it was.")
        except BaseException:
            if not backed_out:
                await self._back_out(lease, restart_needed=engine_touched)
            raise
        observed = await self.host.observe_engine() if plan.engine_restart else None
        if observed:
            lease["dlne_now"]["coding_lanes"], lease["dlne_now"]["coding_context"] = observed
        done = self.clock()
        lease.update(state=STATE_ACTIVE, since=done, expires_at=done + req.ttl_seconds, last_heartbeat=done)
        save_lease(lease)
        log_coordinator(f"Takeover: lease granted to {req.holder!r}; it ends in {req.ttl_seconds // 60} min unless released.")
        return self.view(lease)

    async def _back_out(self, lease: dict, restart_needed: bool) -> None:
        """A grant that failed after it began: the overlay off, the RAM back, the engine on the plain config again."""
        log_warning("Takeover: the grant failed; putting DLNE back as it was.")
        delete_lease()
        if lease.get("memory", {}).get("applied"):
            await asyncio.to_thread(_restore_memory, lease)
        if restart_needed:
            try:
                before = lease.get("pool_before") or {}
                await self._restart_patiently((float(before.get("vram_gb") or 0.0), float(before.get("sysram_gb") or 0.0)))
            except Exception as exc:       # noqa: BLE001 - the monitor loop takes over from an engine that is down
                log_error(f"Takeover: the engine could not be restarted on the plain config ({type(exc).__name__}: {exc}).")

    # ----------------------------------------------------------------- heartbeat, release
    def _own(self, lease_id: str) -> dict:
        lease = load_lease()
        if lease is None or not secrets.compare_digest(str(lease.get("lease_id")).encode(), str(lease_id).encode()):
            raise TakeoverError(404, CODE_NOT_FOUND, "No such lease (it may have ended).")
        return lease

    async def heartbeat(self, lease_id: str) -> Dict[str, Any]:
        async with self._lock:
            lease = self._own(lease_id)
            now = self.clock()
            if lease.get("state") == STATE_ACTIVE:
                if is_stale(lease, now):
                    await self._begin_restore(lease, "it expired or its heartbeat was lost")
                    raise TakeoverError(404, CODE_NOT_FOUND, "The lease ended (its time or its heartbeat ran out).")
                lease["last_heartbeat"] = now
                save_lease(lease)
            return self.view(lease)

    async def release(self, lease_id: str) -> Dict[str, Any]:
        async with self._lock:
            lease = self._own(lease_id)
            if lease.get("state") == STATE_ACTIVE:
                await self._begin_restore(lease, "released")
                lease = load_lease() or lease
            return self.view(lease)

    # ----------------------------------------------------------------- ending
    async def _begin_restore(self, lease: dict, why: str) -> None:
        """The lease is over: say so at once, give everything back in the background."""
        if lease.get("state") != STATE_RESTORING:
            lease["state"] = STATE_RESTORING
            save_lease(lease)
        log_coordinator(f"Takeover: lease of {lease.get('holder')!r} ended ({why}); restoring DLNE.")
        if self._restore_task is None or self._restore_task.done():
            self._restore_task = asyncio.ensure_future(self._restore(lease))
            self._tasks.add(self._restore_task)
            self._restore_task.add_done_callback(self._tasks.discard)

    async def _restore(self, lease: dict) -> None:
        try:
            if lease.get("memory", {}).get("applied"):
                await asyncio.to_thread(_restore_memory, lease)       # first: a restarting engine may want it
            if lease.get("engine_restart"):
                if not await self._idle_moment():
                    log_warning("Takeover: DLNE was never idle in 10 minutes; restoring the engine anyway.")
                before = lease.get("pool_before") or {}
                result = await self._restart_patiently((float(before.get("vram_gb") or 0.0),
                                                        float(before.get("sysram_gb") or 0.0)))
                if not result.get("success"):
                    log_error(f"Takeover: the engine did not come up on the plain config ({result.get('detail')}); "
                              "the monitor loop will keep trying.")
            delete_lease()
            log_coordinator("Takeover: DLNE is restored.")
        except Exception as exc:      # noqa: BLE001 - the file stays "restoring"; the next start cleans it
            log_error(f"Takeover: restoring failed ({type(exc).__name__}: {exc}).")

    async def tick(self) -> None:
        """One look of the watcher: an active lease past its ttl or its heartbeat is ended; a restore that has no task
        (the coordinator was restarted mid-restore with the file kept) is run."""
        lease = load_lease()
        if lease is None:
            return
        async with self._lock:
            lease = load_lease()
            if lease is None:
                return
            now = self.clock()
            if lease.get("state") == STATE_ACTIVE and is_stale(lease, now):
                expired = now >= float(lease.get("expires_at") or 0)
                await self._begin_restore(lease, "its time ran out" if expired else "its heartbeat was lost")
            elif lease.get("state") == STATE_RESTORING and (self._restore_task is None or self._restore_task.done()):
                await self._begin_restore(lease, "restore resumed")

    async def run(self) -> None:
        while True:
            await asyncio.sleep(WATCH_SECONDS)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:      # noqa: BLE001 - the watcher must outlive one bad look
                log_error(f"Takeover watcher: {type(exc).__name__}: {exc}")
