"""
DLNE Coordinator — the FastAPI service that owns the cluster.

It authenticates worker heartbeats, tracks the pooled hardware, spawns and supervises the
llama.cpp inference engines, proxies OpenAI-compatible chat traffic to them, and pushes
live cluster telemetry to every connected WebUI over a WebSocket.

Public symbols other modules rely on (do not rename):
    start_coordinator_server(host, port, is_promoted)   imported by main.py
    download_file_multistream_async(url, dest, name, *, broadcast=True)   imported by main.py

Every HTTP route path, WebSocket message ``type`` value and JSON field name in this file is
part of the contract with ``webui/app.js``, which is a shipped client we do not rebuild.
Treat all of them as frozen.
"""

import asyncio
import concurrent.futures
import contextlib
import functools
import ipaddress
import hmac
import json
import logging
import math
import os
import re
import shutil
import socket
import sys
import threading
import time
from collections import deque
from typing import Any, AsyncIterator, Deque, Dict, List, Optional, Set, Tuple

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask
from starlette.middleware.base import BaseHTTPMiddleware

from engine.authn import (
    NonceCache,
    validate_hardware_profile,
    verify_enrollment,
    verify_heartbeat,
)
from engine import benchmark
from engine import gguf
from engine.config import get_base_dir, get_config, get_worker_limits
from engine.landing import LANDING_HTML
from engine.chat_page import chat_router
# Imported as symbols, not copied as string literals. start_model's message is a control
# signal — see the sentinel block at the top of engine/inference_manager.py — and the two
# modules must never grow independent copies of the same set of magic strings.
from engine.inference_manager import (
    ADMISSION_OK_REASON,
    InferenceManager,
    NETWORK_MAXED_OUT_MESSAGE,
    canonical_model_tag,
    is_model_not_on_disk,
    is_retryable_start_failure,
)
from engine.logger import log_coordinator, log_error, log_warning
from engine.registry import WorkerRegistry, is_app_member, is_coding_member, safe_log_text
from engine.app_pool import LocalAppServer, app_pool
from engine.throughput import POOL_CODING, POOL_PHONE, StreamTokenCounter, completion_tokens, throughput
from engine import coding_router as coding_members_lib
from engine.coding_router import coding_router
from engine.downloader import download_and_extract_engine, download_verified
from engine.version import VERSION
from engine import release as release_lib
from engine import coding_ladder, coding_speed
from engine.lease import lease_manager, InferenceLease
from engine.priority import UsageWindow, coding_gate, cpu_coding_window, tier_priority
from engine.api_keys import api_key_manager, hash_admin_key
from engine import canary
from engine.auto_swap import (
    AutoSwapController,
    LinkProbe,
    SwapResult,
    downloadable,
    file_is_pinned,
    rtt_is_near,
    split_pool_by_distance,
    tier_files,
)

# The rich-backed helpers in engine.logger deliberately expose no debug level, because
# everything they emit is meant for an operator watching the console. The update checker
# needs a genuinely quiet channel (an unconfigured manifest URL is the normal state, not a
# problem), so it talks to the underlying stdlib logger directly.
_debug_logger = logging.getLogger("system")

# No /docs, /redoc or /openapi.json: on the public swarm they listed every route, admin ones too.
app = FastAPI(title="DLNE Coordinator", docs_url=None, redoc_url=None, openapi_url=None)
registry = WorkerRegistry()
manager = InferenceManager()


# ======================================================================================
# Background task bookkeeping (bug #27)
# ======================================================================================

# asyncio only holds a *weak* reference to a running task. A bare
# ``asyncio.create_task(...)`` whose result nobody keeps can therefore be garbage collected
# mid-flight, and if it raises, the exception surfaces as an unretrievable "Task exception
# was never retrieved" warning at interpreter shutdown — or not at all. Every task spawned
# here is parked in this set until it finishes, and its exception is logged.
_background_tasks: Set[asyncio.Task] = set()


def _on_background_task_done(task: asyncio.Task) -> None:
    """Drops a finished task from the registry and makes any failure visible."""
    _background_tasks.discard(task)
    if task.cancelled():
        return
    exception = task.exception()
    if exception is not None:
        log_error(f"Background task '{task.get_name()}' failed: {type(exception).__name__}: {exception}")


def spawn_background_task(coro, name: str) -> asyncio.Task:
    """
    Fire-and-forget a coroutine without losing it or its errors.

    Always use this instead of a bare ``asyncio.create_task``; the name shows up in the log
    line above, which is the difference between "something crashed" and "the seed download
    crashed".
    """
    task = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)
    task.add_done_callback(_on_background_task_done)
    return task


# ======================================================================================
# Client identification
# ======================================================================================

def is_local_ip(ip: str) -> bool:
    """
    True when the address belongs to this machine (so the Coordinator host is exempt).

    NOTHING THAT IS NOT ALREADY AN IP LITERAL REACHES ``bind()`` (finding R3-4)
        ``socket.bind((host, 0))`` accepts a *hostname* as well as an address, and when it
        gets one it performs a blocking ``getaddrinfo``. The argument here comes from
        :func:`get_client_ip`, which under ``TRUST_PROXY_HEADERS`` returns whatever token it
        found in an ``X-Forwarded-For`` header — arbitrary attacker-supplied text, never
        validated as an address. Both call sites that matter (``NonContribBlockerMiddleware.
        dispatch`` and the WebSocket handler) are coroutines, so the resolution happened
        directly on the event loop: an NXDOMAIN was measured at ~20 ms, and a resolver that
        *drops* rather than refuses parks the entire coordinator — every stream, broadcast
        and heartbeat — for the full DNS timeout, once per request.

        So the value is parsed with :mod:`ipaddress` first. That is a pure-Python string
        parse with no I/O of any kind; anything it rejects is not an address, cannot be this
        machine, and is refused without ever touching the resolver.

    Only IPv4 literals are probed, which is what the previous implementation effectively did
    too: it built an ``AF_INET`` socket, and binding an IPv6 literal to one always raised. The
    loopback names and ``0.0.0.0`` keep their explicit fast path so the common case never
    allocates a socket at all.
    """
    if ip in ["127.0.0.1", "::1", "localhost", "0.0.0.0"]:
        return True

    try:
        parsed = ipaddress.ip_address(ip)
    except ValueError:
        # Not an address at all — a hostname, a header fragment, or junk. Refused here so it
        # can never become a DNS lookup on the event loop.
        return False

    if parsed.version != 4:
        # An AF_INET bind of an IPv6 literal has always raised, so this is the pre-existing
        # answer, just reached without the exception.
        return False

    try:
        test_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        test_sock.bind((str(parsed), 0))
        test_sock.close()
        return True
    except Exception:
        return False


# How many reverse proxies sit in front of this Coordinator. Only consulted when
# TRUST_PROXY_HEADERS is on. 1 is the overwhelmingly common deployment: a single nginx or
# Caddy in front of DLNE. See get_client_ip for what this index actually means.
DEFAULT_TRUSTED_PROXY_HOPS = 1


def _trusted_proxy_hops() -> int:
    """
    Read ``TRUSTED_PROXY_HOPS`` from config, clamped to something sane.

    Read through ``get_worker_limits()`` — it is the typed view, it is cached on the config
    file's stat, and it carries a ``trusted_proxy_hops`` default, so in production this is
    the only branch that runs. The raw ``get_config()`` fallback remains for callers that
    substitute a partial limits mapping (the test suite does exactly this), so a dict
    without the key degrades to reading the file rather than to ``None``.

    Anything unparseable or non-positive falls back to the default rather than raising: a
    typo in config.txt must not be able to break client identification for every request.
    """
    limits_value = get_worker_limits().get("trusted_proxy_hops")
    raw = limits_value if limits_value is not None else get_config().get("TRUSTED_PROXY_HOPS", "")

    try:
        hops = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_TRUSTED_PROXY_HOPS

    # 0 or negative would index from the wrong end of the list entirely; an absurdly large
    # value is clamped by the bounds check in get_client_ip, but capping here keeps the
    # intent obvious.
    if hops < 1:
        return DEFAULT_TRUSTED_PROXY_HOPS
    return min(hops, 16)


def get_client_ip(request: Request | WebSocket) -> str:
    """
    Resolve the caller's address.

    ``X-Forwarded-For`` is a *client-supplied* header. Honouring it unconditionally — which
    is what this function used to do — let any remote caller send ``X-Forwarded-For:
    127.0.0.1`` and be treated as the Coordinator host, bypassing the Non-Contrib gate
    entirely. It is only meaningful when a trusted reverse proxy is guaranteed to be in
    front of DLNE, so it stays gated behind the explicit ``TRUST_PROXY_HEADERS`` opt-in and
    still defaults to off.

    WHICH ENTRY TO TAKE, AND WHY IT IS THE RIGHT-MOST
        Proxies *append*. With the canonical nginx directive
        ``proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for``, a request that
        arrives already carrying ``X-Forwarded-For: 127.0.0.1`` leaves the proxy as
        ``X-Forwarded-For: 127.0.0.1, <real-client-ip>``. Reading the LEFT-most entry -
        which this function used to do, directly contradicting its own comment - therefore
        returns whatever the *attacker* wrote. That handed any remote caller
        ``is_local_ip() -> True`` (a complete Non-Contrib bypass) and let them inject an
        arbitrary address into ``registry.update_worker(..., client_ip, ...)``, which is
        the value that ends up in ``llama-server --rpc``.

        Only the entries appended by proxies we control can be trusted, and they are at the
        right-hand end. With ``TRUSTED_PROXY_HOPS = n``, the entry appended by the
        outermost trusted proxy is ``parts[-n]``: one hop means the last entry (what our
        single proxy observed as its peer), two means the one before it, and so on. Every
        entry to the left of that was written by something we do not control and is
        ignored.

    A CHAIN SHORTER THAN THE CONFIGURED HOP COUNT FAILS CLOSED (finding R3-3)
        This used to clamp — ``index = max(0, len(parts) - hops)`` — and return
        ``parts[0]``, which with the default one hop and a single-entry header is the
        attacker's own value: exactly the left-most read the fix above exists to stop
        trusting. DLNE binds ``0.0.0.0`` by default, so with ``TRUST_PROXY_HEADERS`` on,
        anyone who can reach port 11435 *directly* rather than through nginx supplies a
        one-entry header they wholly control, and got ``is_local_ip() -> True`` (a complete
        Non-Contrib bypass) plus arbitrary RPC-endpoint injection through
        ``registry.update_worker(..., client_ip, ...)``.

        A short chain is not a header we can partially believe. It means the request did
        **not** traverse the proxies we were told about, so the header is discarded entirely
        and the real transport peer is used. That is the conservative answer in both
        directions: a genuinely proxied request always carries at least ``hops`` entries, and
        a direct one is correctly identified by its socket address.

    A HEADER CAN NEVER *GRANT* LOCALHOST (finding R3-3, the residual)
        The rule above only rejects chains shorter than ``hops``, and that leaves the
        finding's own measured case open on the DEFAULT ``TRUSTED_PROXY_HOPS = 1``: a
        one-entry header is not "short" against one hop, so a client that reaches port 11435
        directly — which the default ``0.0.0.0`` bind allows — still had ``X-Forwarded-For:
        127.0.0.1`` believed, and still got ``is_local_ip() -> True``. Nothing in the header
        itself distinguishes that from the single entry nginx appends for a genuine client.

        The transport peer does, though, and it is the one value in the request the caller
        cannot forge. A local address is a *privilege* here — it is what exempts a caller
        from the Non-Contrib clause — so the header is allowed to change WHICH remote address
        we attribute the request to, but never to promote a remote caller to a local one. If
        the header resolves to something local while the socket peer is not, the header is a
        forgery and the peer wins.

        This costs a legitimate deployment nothing: a proxy on this host reaches us over
        loopback, so its peer is already local, and a proxy forwarding a genuine client
        reports a non-local address that this rule does not touch.

    Returns:
        The best available client address, or ``"unknown"`` when the transport does not
        expose one (which some ASGI test harnesses do not).

    ONLY A PROXY'S OWN CONNECTION MAY FORWARD
        The coordinator stays reachable directly on its port for clients that predate the
        HTTPS proxy, so a caller from the internet can send its own one-entry header and be
        believed - choosing which address its quota and its worker registration are
        attributed to. Proxies sit on this host or the LAN, so the header is only read when
        the socket peer is a loopback or private address; a public peer is the client.
    """
    peer = request.client.host if request.client else "unknown"

    if get_worker_limits().get("trust_proxy_headers", False) and _peer_may_forward(peer):
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            parts = [entry.strip() for entry in forwarded.split(",") if entry.strip()]
            hops = _trusted_proxy_hops()
            if len(parts) >= hops:
                claimed = parts[-hops]
                # Both checks are pure string/ipaddress work — see is_local_ip, which
                # deliberately never resolves a name — so this adds no I/O to the hot path.
                if is_local_ip(claimed) and not is_local_ip(peer):
                    log_warning(
                        "Ignoring an X-Forwarded-For header that claims a local address "
                        f"({safe_log_text(claimed, max_chars=45)}) from non-local peer "
                        f"{safe_log_text(peer, max_chars=45)}. Treating it as the forgery it "
                        "is; the request is attributed to its real socket address."
                    )
                    return peer
                return claimed
    return peer


def _peer_may_forward(peer: str) -> bool:
    """True for a loopback or private socket peer: somewhere a reverse proxy could be."""
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return address.is_loopback or address.is_private


def _as_number(value: Any, default: float = 0.0) -> float:
    """
    Coerce a worker-reported hardware value to a float for comparison.

    Every number in a hardware profile arrives from another machine. ``validate_hardware_profile``
    rejects non-numeric values on the heartbeat path, but the Non-Contrib middleware reads the
    *stored* profile of a worker that may have registered under an older, laxer build — so the
    comparisons that gate access are made defensive rather than trusting. A value that cannot be
    read as a number counts as zero, which fails the contribution test closed.
    """
    if isinstance(value, bool):
        # bool is an int subclass; `True` must not silently register as "1 core".
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def gpu_section(hardware: Any) -> Dict[str, Any]:
    """
    Return a hardware profile's ``gpu`` sub-dictionary, or an empty dict.

    ``hardware.get("gpu", {})`` reads as if it does this, and does not: a ``dict.get``
    default only applies when the key is *absent*. A CPU-only node legitimately reports
    ``{"gpu": null}``, so ``.get`` returns the stored ``None`` and the chained ``.get`` on
    it raises ``AttributeError`` — an unauthenticated remote HTTP 500 (and log flood) on
    the heartbeat path whenever ``ALLOW_UNSIGNED_WORKERS`` is on. ``validate_hardware_profile``
    deliberately permits ``gpu: null``, so the crash was reachable through a *passing*
    validation step. This helper is the single safe accessor; use it everywhere.
    """
    if not isinstance(hardware, dict):
        return {}
    gpu = hardware.get("gpu")
    return gpu if isinstance(gpu, dict) else {}


def cpu_node_shortfalls(hw: Any, limits: Dict[str, Any]) -> List[str]:
    """
    What a node with no usable GPU lacks against the CPU_MIN_* floors, as plain phrases for
    the person whose machine it is ("4 physical cores (this machine has 2)"). Empty when it
    qualifies, or when it has a GPU, which these rules do not cover.

    A missing measurement counts as a shortfall: a node that cannot show its memory
    bandwidth or instruction sets is not admitted on trust, since one slow CPU node holds up
    every token of the pipeline it is placed in.
    """
    hw = hw if isinstance(hw, dict) else {}
    if _as_number(gpu_section(hw).get("vram_total_gb", 0)) > 0:
        return []

    missing: List[str] = []
    min_ram = float(limits.get("cpu_min_ram_gb", 8.0))
    min_cores = int(limits.get("cpu_min_cores", 4))
    min_bandwidth = float(limits.get("cpu_min_bandwidth_gbs", 30.0))

    ram = _as_number(hw.get("ram_gb", 0))
    if ram < min_ram:
        missing.append(f"at least {min_ram:g} GB of RAM lent to DLNE (this machine lends {ram:g} GB)")
    cores = _as_number(hw.get("cpu_cores", 0))
    if cores < min_cores:
        missing.append(f"{min_cores} physical CPU cores (this machine has {cores:g})")

    # AVX2 + FMA is llama.cpp's fast x86 path; ARM CPUs are judged on bandwidth alone.
    if hw.get("cpu_arch", "x86_64") == "x86_64":
        features = hw.get("cpu_features")
        features = set(features) if isinstance(features, list) else set()
        lacking = [f.upper() for f in ("avx2", "fma") if f not in features]
        if lacking:
            missing.append(f"a CPU with AVX2 and FMA (missing: {', '.join(lacking)}; Intel 2013+ or AMD Ryzen)")

    bandwidth = hw.get("mem_bandwidth_gbs")
    if not isinstance(bandwidth, (int, float)) or isinstance(bandwidth, bool):
        missing.append(f"a memory bandwidth measurement of at least {min_bandwidth:g} GB/s (none was reported)")
    elif bandwidth < min_bandwidth:
        missing.append(f"memory bandwidth of at least {min_bandwidth:g} GB/s (measured {bandwidth:g} GB/s)")
    return missing


def cpu_refusal_text(missing: List[str]) -> str:
    return "This machine has no usable GPU, and a CPU-only node needs " + "; ".join(missing) + "."


# Set on a heartbeat refused for the CPU floors, so the worker can tell that apart from any
# other 403 and stop with a reason instead of retrying forever.
CPU_REFUSAL_HEADER = "X-DLNE-Refusal"
CPU_REFUSAL_VALUE = "cpu-minimum"


def get_request_token(request: Request | WebSocket) -> str:
    """Extracts the node token from an Authorization header or the ``token`` query param."""
    if isinstance(request, WebSocket):
        # HTTP requests are checked for a decoy key once, in NonContribBlockerMiddleware.
        canary.check_request(request.headers, request.query_params, lambda: get_client_ip(request))
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth.replace("Bearer ", "").strip()
    return request.query_params.get("token", "").strip()


class StarterQuotaManager:
    """
    Manages free starter quota for new users/evaluators of the DLNE network.
    Grants a starter allocation (default: 100 chat requests) per client before
    enforcing local GPU worker contribution or an authorized API key.
    """

    def __init__(self, relative_path: str = "config/starter_quota.json", default_quota: int = 100):
        self.filename = relative_path
        self.default_quota = default_quota
        self._lock = threading.Lock()
        self._usage: Dict[str, int] = {}
        self._load()

    def _get_path(self) -> str:
        return os.path.join(get_base_dir(), self.filename)

    def _load(self) -> None:
        p = self._get_path()
        try:
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    self._usage = json.load(f)
        except Exception:
            self._usage = {}

    def _save_locked(self) -> None:
        # Written aside and renamed into place: a crash halfway through an in-place write
        # left a file _load could not parse, which reset every client's trial to zero.
        p = self._get_path()
        tmp = f"{p}.tmp"
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._usage, f, indent=2)
            os.replace(tmp, p)
        except Exception:
            pass

    def check_and_consume(self, client_id: str, limit: Optional[int] = None) -> Tuple[bool, int, int]:
        max_quota = limit if limit is not None else self.default_quota
        if max_quota <= 0:
            return False, 0, 0
        with self._lock:
            used = self._usage.get(client_id, 0)
            if used < max_quota:
                used += 1
                self._usage[client_id] = used
                self._save_locked()
                return True, used, max_quota - used
            return False, used, 0

    @staticmethod
    def _mask(client_id: str) -> str:
        """Hide most of an identifier: an IP loses its last octet, a token all but a prefix."""
        parts = client_id.split(".")
        if len(parts) == 4 and all(p.isdigit() for p in parts):
            return ".".join(parts[:3] + ["x"])
        return client_id[:6] + "…" if len(client_id) > 6 else "…"

    def summary(self, limit: int, top: int = 0) -> Dict[str, Any]:
        """Totals for the operator; with ``top`` > 0, also the heaviest users (masked)."""
        with self._lock:
            usage = dict(self._usage)
        result: Dict[str, Any] = {
            "clients": len(usage),
            "exhausted": sum(1 for used in usage.values() if used >= limit),
            "chats_used": sum(usage.values()),
        }
        if top > 0:
            ranked = sorted(usage.items(), key=lambda kv: kv[1], reverse=True)[:top]
            result["top"] = [{"client": self._mask(cid), "used": used} for cid, used in ranked]
        return result

    def reset(self, client_id: Optional[str]) -> int:
        """Forget one client's usage (or everyone's when None); returns how many were reset."""
        with self._lock:
            if client_id is None:
                removed = len(self._usage)
                self._usage.clear()
            else:
                removed = 1 if self._usage.pop(client_id, None) is not None else 0
            if removed:
                self._save_locked()
            return removed

    def peek(self, client_id: str, limit: Optional[int] = None) -> Tuple[bool, int, int]:
        """Same answer as check_and_consume, without spending a unit of the quota."""
        max_quota = limit if limit is not None else self.default_quota
        if max_quota <= 0:
            return False, 0, 0
        with self._lock:
            used = self._usage.get(client_id, 0)
            return used < max_quota, used, max(0, max_quota - used)


starter_quota_mgr = StarterQuotaManager()

# Response header telling clients how much of the free trial is left, so the extension can
# show a counter instead of waiting for the 403 wall.
QUOTA_HEADER = "X-DLNE-Quota-Remaining"


def trial_identity(client_ip: str) -> str:
    """
    Whose free trial a request spends: the network address it came from, never a value
    the client chooses.

    The trial used to be keyed by the bearer token when one was sent, and any token that
    matched no worker or API key fell through to here - so sending a new made-up token with
    every request gave a fresh 200 chats every time. The address is what the coordinator
    actually sees (behind the proxy, the forwarded client address, trusted only from the
    proxy itself). IPv6 is keyed by its /64, the block one home or phone is handed and can
    rotate through freely. The count lives in config/starter_quota.json on the server,
    written atomically and untouched by upgrades, so clearing anything on the user's side
    changes nothing. Everyone behind one shared address shares its trial: the price of a
    free tier that cannot be reset.
    """
    try:
        address = ipaddress.ip_address(client_ip)
    except ValueError:
        return client_ip
    if address.version == 6:
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)


# Rolling-limit headers on a CPU contributor's coding chats: uses left, and the epoch second
# the next one frees up.
LIMIT_REMAINING_HEADER = "X-DLNE-Limit-Remaining"
LIMIT_RESET_HEADER = "X-DLNE-Limit-Reset"


def cpu_limit_text(limit: int, hours: float, reset_at: float) -> str:
    minutes = max(1, int((reset_at - time.time()) // 60) + 1)
    when = f"{minutes} minute{'s' if minutes != 1 else ''}" if minutes < 120 else f"{minutes // 60} hours {minutes % 60} minutes"
    return (
        f"You've used all {limit} coding chats for this {hours:g}-hour window (the allowance for CPU "
        f"contributors). The next one frees up in {when}. Contributing a GPU removes the limit."
    )


def use_starter_quota(request: Request, client_id: str, limit: int) -> Tuple[bool, int]:
    """
    Applies the free starter trial to one request and returns (allowed, remaining).

    Ghost-text autocomplete (/v1/completions) fires after every typing pause, so charging it
    would burn the whole trial in minutes. It is let through while the trial still has chat
    requests left but is never charged; only chat requests spend the quota.
    """
    if request.url.path.startswith(("/v1/completions", "/v1/coding/")):
        allowed, _used, remaining = starter_quota_mgr.peek(client_id, limit=limit)
    else:
        allowed, _used, remaining = starter_quota_mgr.check_and_consume(client_id, limit=limit)
    if allowed:
        request.state.starter_quota_remaining = remaining
    return allowed, remaining


# --------------------------------------------------------------------------------------
# GPU perks follow use
# --------------------------------------------------------------------------------------
#
# A contributor lending a GPU is first in line and has no usage window, for as long as that GPU is
# actually used. A GPU the swarm has no job for (it is not in the running engine's topology, and is not a
# ready coding member) keeps the perks for GPU_STANDBY_GRACE_MINUTES (default 30) from when it joined or
# last had a job, so a newcomer keeps them while the swarm places it; after that its owner is a CPU
# contributor like any other, with the CPU rolling window. Getting a job gives the perks back and resets
# the clock. A GPU handed back to its owner for a game (yielding) is not idle: it keeps them.

_gpu_standing_by_since: Dict[str, float] = {}      # worker id -> when it began standing by (_standby_clock)
MAX_STANDBY_TRACKED = 8192


def _standby_clock() -> float:
    return time.monotonic()


def _gpu_has_job(worker: Dict[str, Any]) -> bool:
    """Whether a GPU worker is serving (the same rule as gpu_pool in /v1/cluster/status): it is a node of the
    running engine's topology, or a ready coding member; or it is yielding to its owner, which is not idling."""
    hardware = worker.get("hardware") if isinstance(worker.get("hardware"), dict) else {}
    if hardware.get("is_yielding") is True:
        return True
    if is_coding_member(hardware):
        if not get_worker_limits().get("coding_members_enabled", False):
            return False
        return any(m.worker_id == worker.get("worker_id") for m in _coding_members())
    return f"{worker.get('ip')}:{worker.get('port')}" in set(manager.current_endpoints)


def _gpu_note_job_state(worker: Dict[str, Any]) -> Optional[float]:
    """Keep this GPU worker's standing-by clock: None (and the clock cleared) when it has a job, else when it
    began standing by (now, the first time it is seen with none). A node with no worker id has no clock."""
    worker_id = worker.get("worker_id")
    if not isinstance(worker_id, str) or not worker_id:
        return None
    if _gpu_has_job(worker):
        _gpu_standing_by_since.pop(worker_id, None)
        return None
    since = _gpu_standing_by_since.get(worker_id)
    if since is None:
        if len(_gpu_standing_by_since) >= MAX_STANDBY_TRACKED:
            for stale in [w for w in _gpu_standing_by_since if w not in registry.workers]:
                del _gpu_standing_by_since[stale]
            if len(_gpu_standing_by_since) >= MAX_STANDBY_TRACKED:
                _gpu_standing_by_since.pop(next(iter(_gpu_standing_by_since)))
        since = _gpu_standing_by_since[worker_id] = _standby_clock()
    return since


def _gpu_keeps_perks(worker: Dict[str, Any], limits: Dict[str, Any]) -> bool:
    """Whether a GPU contributor still gets the GPU tier: it has a job, or has stood by for less than the grace."""
    since = _gpu_note_job_state(worker)
    if since is None:
        return True
    grace = float(limits.get("gpu_standby_grace_minutes", 30.0)) * 60.0
    return _standby_clock() - since < grace


class NonContribBlockerMiddleware(BaseHTTPMiddleware):
    """Blocks WebUI and chat traffic from anyone who is not contributing compute, while granting a free starter quota."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # Canary tripwire: a decoy key flags the member it was given to, and the request is
        # then handled like any other invalid key (engine/canary.py).
        canary.check_request(request.headers, request.query_params, lambda: get_client_ip(request))
        is_cluster_control = path.startswith("/v1/cluster/swap")
        # /v1/coding/ (route lookups for coding members) is gated like the requests it precedes,
        # but never charged: use_starter_quota only looks.
        if (path.startswith("/v1/chat") or path.startswith("/v1/completions") or path.startswith("/v1/coding/")
                or is_cluster_control):
            client_ip = get_client_ip(request)
            client_token = get_request_token(request)
            # Every request that reaches the coding engine carries its tier, which decides
            # its place in the engine's queue (engine/priority.py).
            if not get_worker_limits().get("enforce_contrib_clause", True):
                request.state.dlne_tier = "open"
                return await call_next(request)

            # Coordinator host is automatically granted access
            if is_local_ip(client_ip):
                request.state.dlne_tier = "host"
            else:
                # Bearer API Key holders (VS Code extension / IDE clients) are authorized
                if client_token and api_key_manager.verify_key(client_token):
                    request.state.dlne_tier = "key"
                    return await call_next(request)

                # Active contributing worker nodes are authorized
                worker = registry.find_by_token(client_token) if client_token else None
                if not worker:
                    # Check free starter quota for new users/evaluators
                    request.state.dlne_tier = "trial"
                    quota_limit = int(get_worker_limits().get("starter_quota_inferences", 100))
                    if quota_limit > 0:
                        allowed, remaining = use_starter_quota(request, trial_identity(client_ip), quota_limit)
                        if allowed:
                            response = await call_next(request)
                            response.headers[QUOTA_HEADER] = str(remaining)
                            return response

                    if path.startswith("/v1/"):
                        return JSONResponse(
                            {
                                "detail": (
                                    f"DLNE Compute Required: Free starter quota ({quota_limit}/{quota_limit} inferences) used. "
                                    "Run '@dlne /contribute' to donate idle GPU compute or provide an authorized API key."
                                )
                            },
                            status_code=403,
                            headers={QUOTA_HEADER: "0"}
                        )
                    return HTMLResponse("<h1>Access Denied</h1><p>You must be an active compute node or present a valid DLNE API key to access the network.</p>", status_code=403)

                # Enforce Non-Contrib Clause: a CPU-only node earns access on the same floors
                # it was admitted on (a node registered under older, laxer rules does not).
                limits = get_worker_limits()
                missing = cpu_node_shortfalls(worker.get("hardware", {}), limits)
                if missing:
                    return JSONResponse({"detail": cpu_refusal_text(missing)}, status_code=403)

                # A GPU contributor's perks (first in line, no window) last while its GPU is used, or for
                # GPU_STANDBY_GRACE_MINUTES after it joined or last had a job (_gpu_keeps_perks). A GPU
                # that has stood idle longer is a CPU contributor like any other; it is never refused
                # for that (cpu_node_shortfalls above passes any node with a GPU).
                if (_as_number(gpu_section(worker.get("hardware", {})).get("vram_total_gb", 0)) > 0
                        and _gpu_keeps_perks(worker, limits)):
                    request.state.dlne_tier = "gpu"
                else:
                    # A CPU contributor: second in the queue, and its coding chats counted in a
                    # rolling window. Autocomplete is never counted (it only runs on a free slot).
                    request.state.dlne_tier = "cpu"
                    if path.startswith("/v1/chat"):
                        return await self._cpu_window(request, call_next, str(worker.get("worker_id")), limits)

        return await call_next(request)

    @staticmethod
    async def _cpu_window(request: Request, call_next, worker_id: str, limits: Dict[str, Any]):
        limit = int(limits.get("cpu_coding_limit", 50))
        hours = float(limits.get("cpu_coding_window_hours", 5.0))
        allowed, remaining, reset_at = cpu_coding_window.check_and_record(worker_id, limit, hours * 3600)
        headers = {LIMIT_REMAINING_HEADER: str(remaining), LIMIT_RESET_HEADER: str(int(reset_at))}
        if not allowed:
            wait = max(1, int(reset_at - time.time()))
            return JSONResponse(
                {"detail": cpu_limit_text(limit, hours, reset_at)},
                status_code=429,
                headers={**headers, "Retry-After": str(wait)},
            )
        response = await call_next(request)
        response.headers.update(headers)
        return response


app.add_middleware(NonContribBlockerMiddleware)


# ======================================================================================
# Landing page
# ======================================================================================

@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
async def root():
    """
    The landing page. DLNE is a VS Code extension; the browser chat room that used to live
    here is gone, so anyone who finds the address gets pointed at the extension (and the phone
    chat at /chat) and a live view of the swarm. Public by design: it is a signpost, not a service.
    """
    return HTMLResponse(LANDING_HTML)


# The phone chat at /chat (engine/chat_page.py): an installable page over the CPU pool.
app.include_router(chat_router)


@app.api_route("/c/{token}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
               include_in_schema=False)
async def canary_link(token: str, request: Request):
    """
    A canary link (engine/canary.py): the address inside a decoy coding request. Opening it flags
    the coding member that was sent it. It answers exactly as an address that does not exist does
    (same status and body, for every method and every token), so the visit tells the visitor
    nothing, and it is never linked from anywhere a real user would go.
    """
    canary.fetched(token, lambda: get_client_ip(request))
    raise HTTPException(status_code=404, detail="Not Found")


# ======================================================================================
# Global cluster state
# ======================================================================================

active_websockets: List[WebSocket] = []

# The GPU coding pool's model ladder (engine/coding_ladder.py). Read once: it comes from the
# signed release this process runs, which only changes with a restart. Tests replace it.
_coding_tiers: Optional[List[Dict[str, Any]]] = None


def _coding_ladder() -> List[Dict[str, Any]]:
    global _coding_tiers
    if _coding_tiers is None:
        _coding_tiers = coding_ladder.load_coding_tiers(get_base_dir())
    return _coding_tiers


def _default_coding_model() -> str:
    """The ladder's first enabled tier: what this coordinator serves when nothing names a model."""
    tier = coding_ladder.default_tier(_coding_ladder())
    return tier["file"] if tier else ""


# DEFAULT_MODEL in config.txt wins; a fresh coordinator starts on the ladder's first tier.
global_active_model: str = canonical_model_tag(get_config().get("DEFAULT_MODEL") or _default_coding_model())
cluster_status: str = "Waiting for Nodes"
has_pulled_seed = False
is_downloading_seed = False
is_swapping_models = False
active_inference_requests = 0
is_recalculating_queue = False

# ``active_inference_requests`` is incremented on entry to /v1/chat/completions and
# decremented from streaming generators that finish at arbitrary later points. Those are all
# coroutines on one event loop, but they interleave at every ``await``, so a bare ``+= 1``
# is still a read-modify-write that can be split. The lock makes the update atomic and the
# helper clamps at zero so a stray double-decrement can never drive the counter negative
# (which used to permanently disable the viral-traffic queue recalculation).
_inference_counter_lock = asyncio.Lock()


async def _change_inference_count(delta: int) -> int:
    """Adjusts the in-flight inference counter under the lock and returns the new value."""
    global active_inference_requests
    async with _inference_counter_lock:
        active_inference_requests = max(0, active_inference_requests + delta)
        return active_inference_requests


# ======================================================================================
# WebSocket fan-out
# ======================================================================================

async def broadcast(payload: dict) -> None:
    """
    Send one JSON payload to every connected WebUI, pruning sockets that fail.

    This is the *only* sanctioned way to talk to ``active_websockets``. Iterating that list
    directly is a bug: every ``await ws.send_json(...)`` is a suspension point at which the
    disconnect handler of another socket can run and mutate the list, and mutating a list
    while a ``for`` loop walks it silently skips entries. Snapshotting with ``list(...)``
    makes the iteration immune to that, and collecting failures for removal *after* the loop
    means a dead client is reaped instead of raising on every future broadcast.
    """
    if not active_websockets:
        return

    failed: List[WebSocket] = []
    for ws in list(active_websockets):
        try:
            await ws.send_json(payload)
        except Exception:
            # A send failure means the peer is gone (or the socket is half-closed). There is
            # nothing to log per-message; the disconnect itself is already handled by the
            # endpoint's finally block. Just stop broadcasting to it.
            failed.append(ws)

    for ws in failed:
        try:
            active_websockets.remove(ws)
        except ValueError:
            # Already removed by its own handler's finally block — benign race.
            pass


# ``download_file_multistream_async`` takes a keyword-only ``broadcast: bool`` flag (main.py's
# redundancy sync passes False because it runs on a private event loop). That parameter
# shadows the function name for the whole body of that function, so it needs this alias to
# reach the helper above.
_broadcast = broadcast


class HeartbeatPayload(BaseModel):
    """Body of ``POST /worker/heartbeat``.

    ``timestamp``/``nonce``/``signature`` are the Contract A authentication fields. They
    default to empty so that a heartbeat from a node with no ``NETWORK_KEY`` still parses —
    the Coordinator then decides whether to accept it based on ``ALLOW_UNSIGNED_WORKERS``,
    rather than returning a confusing 422 validation error.
    """
    worker_id: str
    version: str
    port: int
    token: str = ""
    hardware: Dict[str, Any]
    timestamp: float = 0.0
    nonce: str = ""
    signature: str = ""
    # Wire format v3. ``staged_version`` belonged to the retired update rollout and is always
    # empty now; it stays because it is part of the signed canonical string, so removing it
    # would be a wire break for no benefit. Defaults keep unsigned/legacy bodies parsing -
    # signed traffic cannot omit them anyway, because both sides fold the same defaults
    # into the canonical string.
    role: str = "worker"
    staged_version: str = ""


@app.websocket("/ws/rpc_tunnel/{worker_id}")
async def rpc_tunnel_endpoint(websocket: WebSocket, worker_id: str, port: int = 50052):
    """
    Persistent outbound reverse RPC WebSocket tunnel for NAT / firewall-isolated workers.
    Bridges `llama-server` on the Coordinator (`127.0.0.1:501xx`) directly to the worker's
    local `127.0.0.1:50052` (`ggml-rpc-server`) without requiring inbound ports on the worker.

    ONLY A REGISTERED WORKER OPENS ITS OWN TUNNEL (2.1.4)
        The caller presents its current heartbeat token (``Authorization: Bearer``), and the
        registry must hold that token for the worker named in the path. The tunnel is then
        keyed by the worker's REGISTERED address, never by what the caller claims. Before
        this, anyone could connect under any worker id, which closed that worker's tunnel,
        or open tunnels until the hundred relay ports ran out. ``port`` is still accepted
        from older clients and ignored.
    """
    from engine.reverse_tunnel import reverse_bridge

    client_ip = get_client_ip(websocket)
    worker = registry.find_by_token(get_request_token(websocket))
    if not worker or worker.get("worker_id") != worker_id:
        log_warning(
            f"Refused a reverse tunnel for {safe_log_text(worker_id, max_chars=64)} from "
            f"{safe_log_text(client_ip, max_chars=45)}: no registered worker holds that token."
        )
        # Closing before accept answers the handshake with HTTP 403.
        await websocket.close(code=1008)
        return
    await reverse_bridge.handle_websocket(
        websocket, worker_id, str(worker.get("ip", client_ip)), int(worker.get("port", port)),
        source_ip=client_ip,
    )


@app.websocket("/ws/events")
async def websocket_endpoint(websocket: WebSocket):
    global global_active_model
    global is_swapping_models
    await websocket.accept()

    client_ip = get_client_ip(websocket)
    client_token = get_request_token(websocket)
    if get_worker_limits().get("enforce_contrib_clause", True):
        if not is_local_ip(client_ip):
            registry.prune_dead_workers()
            if registry.find_by_token(client_token) is None:
                # Rejected before registration, so there is nothing to clean up here.
                try:
                    await websocket.send_json({
                        "type": "ban",
                        "message": "NON-CONTRIB BAN: You must actively run a DLNE Worker Node and contribute hardware to access the network."
                    })
                    await websocket.close()
                except Exception:
                    pass
                return

    # The registration and the whole conversation live inside one try/finally. Previously
    # the append sat outside the try while four handshake sends sat between them: a client
    # that vanished during the handshake left its socket in ``active_websockets`` forever,
    # and every subsequent broadcast paid for (and logged around) a dead peer.
    active_websockets.append(websocket)
    try:
        session_id = f"sess_{os.urandom(4).hex()}"
        worker_id = None
        identified = registry.find_by_ip(client_ip)
        if identified:
            worker_id = identified.get("worker_id")

        await websocket.send_json({
            "type": "identity",
            "session_id": session_id,
            "worker_id": worker_id
        })

        # Send initial state immediately so the UI doesn't hang at 0/0/0
        await websocket.send_json({
            "type": "cluster_stats",
            "nodes": registry.count(),
            "vram": registry.get_total_vram_gb(),
            "sysram": registry.get_total_sysram_gb(),
            "cpu_cores": registry.get_total_cpu_cores(),
            "cpu_threads": registry.get_total_cpu_threads(),
            "status": cluster_status
        })
        await websocket.send_json({
            "type": "cluster_sync",
            "active_model": global_active_model,
            "status": cluster_status
        })
        await websocket.send_json({
            "type": "library_sync",
            "library": load_models_json()
        })

        while True:
            data = await websocket.receive_json()
            action = data.get("action")

            if action == "ping":
                await websocket.send_json({"type": "pong"})

            elif action == "vote_request":
                model_name = data.get("model")
                if len(active_websockets) <= 1:
                    # Auto-approve for single user
                    await websocket.send_json({"type": "vote_end", "model": model_name, "approved": True})
                else:
                    await broadcast({"type": "vote_prompt", "model": model_name})

            elif action == "vote_result":
                model_name = data.get("model")
                if data.get("approved"):
                    # Broadcast approval to all so they can execute swap/download
                    await broadcast({"type": "vote_end", "model": model_name, "approved": True})

            elif action == "execute_swap":
                # Only execute once if multiple clients send this simultaneously.
                if global_active_model != data.get("model") and not is_swapping_models:
                    if lease_manager.is_locked():
                        log_coordinator("Model swap requested while active inference leases are in flight. Waiting for idle drain...")
                        await lease_manager.wait_for_idle(timeout=15.0)
                    is_swapping_models = True
                    try:
                        global_active_model = data.get("model")
                        # astop_all_models offloads the blocking terminate/wait to a thread.
                        # Because this is now a real await, other coroutines actually get to
                        # observe is_swapping_models — which is the entire point of the flag.
                        await manager.astop_all_models()
                    finally:
                        # try/finally, not a plain assignment: an exception here used to be
                        # able to leave the cluster stuck in "swapping" forever, silently
                        # parking every future chat request in its wait loop.
                        is_swapping_models = False
                    await broadcast({"type": "swap_complete", "model": global_active_model})

    except WebSocketDisconnect:
        # Normal client navigation/refresh. Nothing to report.
        pass
    except Exception as e:
        # Previously ``except (WebSocketDisconnect, Exception)`` swallowed every genuine
        # error — a malformed frame, a JSON decode failure, a bug in a handler — as if it
        # were a routine disconnect.
        # Both halves are remote-influenced: client_ip can be an X-Forwarded-For token, and
        # the exception text can quote a malformed frame the peer sent. See safe_log_text.
        log_error(
            f"WebSocket session for {safe_log_text(client_ip, max_chars=64)} ended with an "
            f"error: {type(e).__name__}: {safe_log_text(e, max_chars=200)}"
        )
    finally:
        try:
            active_websockets.remove(websocket)
        except ValueError:
            # Already pruned by broadcast() after a failed send.
            pass


# ======================================================================================
# Worker heartbeat (Contract A)
# ======================================================================================

# Replay guard for signed heartbeats. The TTL comfortably exceeds verify_heartbeat's 30s
# skew window, so a nonce can never be forgotten while its heartbeat would still be
# considered fresh.
_nonce_cache = NonceCache(ttl_seconds=120.0, max_entries=10000)

# worker_ids we have already warned about running unauthenticated. Bounded, because the
# worker_id is attacker-controlled in exactly the deployment this set is used in.
_unsigned_worker_warnings: Set[str] = set()
_UNSIGNED_WARNING_CAP = 256


class EnrollmentPayload(BaseModel):
    """Body of ``POST /worker/enroll``.

    Deliberately tiny. Enrollment establishes tunnel membership and nothing else; hardware,
    ports and roles are the heartbeat's business, and accepting them here would create a
    second, less-examined path into the registry.
    """
    worker_id: str
    public_key: str
    timestamp: float = 0.0
    nonce: str = ""
    signature: str = ""


# A WireGuard public key is 32 bytes of Curve25519 in base64: 43 characters plus '='. The
# value is written verbatim into coordinator_wg0.conf, so it is validated in shape before
# it is ever appended - a key containing a newline would let a caller inject arbitrary
# WireGuard directives, including an AllowedIPs claiming the whole subnet.
_WG_PUBKEY_PATTERN = re.compile(r"^[A-Za-z0-9+/]{42}[A-Za-z0-9+/=]=$")


@app.post("/worker/enroll")
async def worker_enroll(request: Request, payload: EnrollmentPayload):
    """
    Assign a self-generated public key its address on the DLNE tunnel.

    WHY THIS ENDPOINT EXISTS
        Before it, a tunnel address could only be handed out by an operator running the
        setup wizard, which minted a complete invite - private key included - per node.
        That does not scale past the operator's patience, and the obvious shortcut of
        copying one invite to every tester is broken by construction: WireGuard identifies
        a peer by public key, so N nodes sharing a keypair are one peer, and the address
        flaps to whichever of them handshook last.

        Here the node generates its own keypair, keeps the private half on its own disk,
        and presents only the public half. What it has to prove is cluster membership, and
        the NETWORK_KEY already proves exactly that. One join file, carrying no private
        key, therefore serves any number of nodes.

    WHY THE ORDER MATCHES THE HEARTBEAT'S
        Authentication precedes every use of the body, for the same reason as there: the
        public key in this request gets written into the Coordinator's WireGuard interface
        and routed traffic. An unauthenticated caller must not be able to place a key
        there, and an on-path attacker must not be able to swap one in flight - which is
        why the key itself is inside the MAC.

    IDEMPOTENCY IS PART OF THE CONTRACT
        A node re-enrolls whenever it restarts or setup is re-run. Repeat requests return
        the address the key already holds rather than allocating another, so a machine that
        reboots nightly does not walk up the /24 until it is full.

    ALLOW_UNSIGNED_WORKERS DOES NOT APPLY HERE
        That escape hatch exists to keep pre-Contract-A workers heartbeating during a
        migration. There are no pre-existing unsigned enrollments to migrate - the protocol
        is new - so accepting unauthenticated ones would only mean handing tunnel addresses
        to anonymous callers. A Coordinator with no NETWORK_KEY refuses to enroll anybody.
    """
    client_ip = get_client_ip(request)
    safe_ip = safe_log_text(client_ip, max_chars=64)
    safe_id = safe_log_text(payload.worker_id, max_chars=64)

    # --- 1. Authentication ------------------------------------------------------------
    network_key = get_worker_limits().get("network_key", "")
    if not network_key:
        log_error(f"Rejected tunnel enrollment from {safe_ip} ({safe_id}): "
                  "no NETWORK_KEY configured on this Coordinator.")
        raise HTTPException(
            status_code=401,
            detail=("This Coordinator has no NETWORK_KEY configured, so tunnel enrollment "
                    "cannot be authenticated. Set NETWORK_KEY in config/config.txt and "
                    "re-issue the join file."),
        )

    ok, reason = verify_enrollment(
        network_key, payload.worker_id, payload.public_key,
        payload.timestamp, payload.nonce, payload.signature,
    )
    if not ok:
        log_error(f"Rejected tunnel enrollment from {safe_ip} ({safe_id}): {safe_log_text(reason)}")
        raise HTTPException(status_code=401, detail=f"Enrollment rejected: {reason}")

    if _nonce_cache.seen(payload.nonce):
        log_error(f"Rejected tunnel enrollment from {safe_ip} ({safe_id}): replayed nonce")
        raise HTTPException(status_code=401, detail="Enrollment rejected: replayed nonce")

    # --- 2. Shape of the key ----------------------------------------------------------
    # Authenticated, but not therefore well-formed: a legitimate node with a broken `wg`
    # install could send junk, and this value is about to be written into a config file
    # WireGuard parses.
    if not _WG_PUBKEY_PATTERN.match(payload.public_key):
        log_error(f"Rejected tunnel enrollment from {safe_ip} ({safe_id}): malformed public key")
        raise HTTPException(
            status_code=400,
            detail="Enrollment rejected: that is not a valid WireGuard public key.",
        )

    # --- 3. Assign ---------------------------------------------------------------------
    from engine.wireguard import (
        WG_SUBNET_CIDR,
        WG_SUBNET_PREFIX,
        WireGuardOrchestrator,
        WireGuardUnavailableError,
    )

    limits = get_worker_limits()
    wg_port = int(limits.get("wg_listen_port", 51820) or 51820)

    try:
        orchestrator = WireGuardOrchestrator()
        coordinator_pubkey = orchestrator.coordinator_public_key()
        if not coordinator_pubkey:
            raise WireGuardUnavailableError(
                "This Coordinator has no WireGuard interface configured, so it cannot "
                "enroll tunnel members. Re-run setup and choose a WAN-facing role."
            )
        octet = await asyncio.to_thread(
            orchestrator.enroll_peer, payload.public_key, f"worker {safe_id}"
        )
    except WireGuardUnavailableError as exc:
        log_error(f"Tunnel enrollment for {safe_id} failed: {exc}")
        # 503, not 500: the request was valid and the operator can fix the condition
        # (subnet full, no interface). A worker that retries later may well succeed.
        raise HTTPException(status_code=503, detail=str(exc))

    log_coordinator(f"Tunnel enrollment: '{safe_id}' assigned {WG_SUBNET_PREFIX}.{octet}.")

    # WG_WAN_IP when the operator configured a WAN-facing hub; otherwise the address this
    # request arrived from, which is this Coordinator as seen from the joining node — the
    # right answer on a LAN and the only one available without asking the operator.
    external_host = str(limits.get("wg_wan_ip", "") or "").strip() or client_ip
    return {
        "tunnel_address": f"{WG_SUBNET_PREFIX}.{octet}",
        "subnet_cidr": WG_SUBNET_CIDR,
        "coordinator_pubkey": coordinator_pubkey,
        "coordinator_endpoint": f"{external_host}:{wg_port}",
    }


@app.post("/worker/heartbeat")
async def worker_heartbeat(request: Request, payload: HeartbeatPayload):
    """
    Endpoint for worker nodes to announce their existence and hardware status.

    Order of operations is fixed by Contract A and matters:
      1. authentication, 2. version, 3. hardware plausibility, 4. contribution baseline,
      5. registration.
    Authentication comes before *any* use of the body's contents, because the reported
    ``ip:port`` is handed to llama-server as ``--rpc`` and the reported hardware drives the
    cluster's memory budget. Neither may be influenced by an unauthenticated caller.

    WHY THE VERSION CHECK IS STEP 2 AND NOT STEP 1 (finding R3-2)
        ``engine/worker.py`` answers HTTP 426 by calling ``request_shutdown(FATAL_EXIT_CODE,
        ...)`` — the node stops. That makes this response a remote kill switch, and running
        it *before* the signature check meant the kill switch was reachable without the
        network key: heartbeats are plain HTTP and an on-path attacker is explicitly in the
        threat model, so rewriting the one ``version`` field in flight took a node offline.

        Wire format v2 folded ``version`` into the MAC precisely so that could not happen,
        but a MAC that is verified *after* the field has already been acted on protects
        nothing. With the check moved below, a rewritten ``version`` now changes the
        canonical string, fails verification, and comes back 401 — while a genuine
        old-build worker, whose signature is valid over its own true version, still gets the
        426 that drives the upgrade path. The claims in ``engine/authn.py`` and
        ``engine/worker.py`` that ``version`` "can no longer be altered in flight" are true
        as of this ordering; they were not before it.

        Unauthenticated deployments (``ALLOW_UNSIGNED_WORKERS`` with no ``NETWORK_KEY``) get
        no protection from this, and cannot: with no key there is nothing to verify. That is
        the documented cost of that mode, not a gap in this ordering.
    """
    client_ip = get_client_ip(request)

    # Every value below that is interpolated into a log message came off the network, and
    # engine.logger renders through rich with markup enabled. safe_log_text coerces, strips
    # control characters and escapes markup, so a worker_id of "[/dim]" or a version string
    # containing a newline cannot raise out of a log call or forge console output. See
    # finding R3-1, which is the same defect one module over.
    safe_ip = safe_log_text(client_ip, max_chars=64)
    safe_id = safe_log_text(payload.worker_id, max_chars=64)

    # --- 1. Authentication ------------------------------------------------------------
    limits = get_worker_limits()
    network_key = limits.get("network_key", "")

    if network_key:
        # version and hardware are part of the signed set as of wire format v2. Passing
        # them here is not optional: engine.authn builds the canonical string in exactly
        # one function, which the Worker also reaches through sign_heartbeat, so omitting
        # a field on this side would make every heartbeat in the fleet fail with
        # "invalid signature".
        ok, reason = verify_heartbeat(
            network_key,
            payload.worker_id,
            payload.port,
            payload.token,
            payload.timestamp,
            payload.nonce,
            payload.signature,
            payload.version,
            payload.hardware,
            payload.role,
            payload.staged_version,
        )
        if not ok:
            log_error(f"Rejected Worker Heartbeat from {safe_ip} ({safe_id}): {safe_log_text(reason)}")
            raise HTTPException(status_code=401, detail=f"Heartbeat rejected: {reason}")

        # Freshness alone does not stop replay — a captured heartbeat is a valid bearer
        # credential until its timestamp goes stale. The nonce cache closes that window.
        if _nonce_cache.seen(payload.nonce):
            log_error(f"Rejected Worker Heartbeat from {safe_ip} ({safe_id}): replayed nonce")
            raise HTTPException(status_code=401, detail="Heartbeat rejected: replayed nonce")
    else:
        if not limits.get("allow_unsigned_workers", False):
            log_error(
                f"Rejected Worker Heartbeat from {safe_ip} ({safe_id}): "
                "no NETWORK_KEY configured on this Coordinator."
            )
            raise HTTPException(
                status_code=401,
                detail=(
                    "This Coordinator has no NETWORK_KEY configured, so worker heartbeats "
                    "cannot be authenticated. Set NETWORK_KEY in config/config.txt on the "
                    "Coordinator and in every worker's invite, or set "
                    "ALLOW_UNSIGNED_WORKERS=true to accept unauthenticated nodes."
                ),
            )

        # Migration escape hatch. Warn once per node so the operator sees the exposure in
        # the log without the heartbeat loop (every 5s, per node) flooding it.
        if payload.worker_id not in _unsigned_worker_warnings:
            if len(_unsigned_worker_warnings) < _UNSIGNED_WARNING_CAP:
                _unsigned_worker_warnings.add(payload.worker_id)
                log_warning(
                    f"UNAUTHENTICATED WORKER ACCEPTED: '{safe_id}' from {safe_ip}. "
                    "ALLOW_UNSIGNED_WORKERS is true and NETWORK_KEY is empty — anyone who can "
                    "reach this port can join the cluster and receive prompts."
                )
            # At the cap we stop tracking and stop warning: the operator has been told 256
            # times already, and an unbounded set here is a memory-exhaustion vector because
            # worker_id is unauthenticated attacker-controlled input on this exact path.

    # --- 2. Version check -------------------------------------------------------------
    # Deliberately below the signature check — see this function's docstring. A 426 no
    # longer *terminates* the worker (it now drives the consent-gated self-update flow),
    # but it still takes the node out of the swarm, so it must only ever be sent to a
    # node whose claimed version has been authenticated. The required version travels in
    # a header as well as the human-readable detail so the worker's updater can act on it
    # without parsing prose.
    if payload.version != VERSION:
        log_error(
            f"Rejected Worker Heartbeat from {safe_ip} ({safe_id}): "
            f"Version Mismatch ({safe_log_text(payload.version, max_chars=32)} != {VERSION})"
        )
        raise HTTPException(
            status_code=426,
            detail=f"DLNE Executable is out of date. Required: {VERSION}",
            headers={"X-DLNE-Required-Version": VERSION},
        )

    # --- 3. Hardware plausibility -----------------------------------------------------
    hardware_ok, hardware_reason = validate_hardware_profile(payload.hardware)
    if not hardware_ok:
        log_error(f"Rejected Worker Heartbeat from {safe_ip} ({safe_id}): {safe_log_text(hardware_reason)}")
        raise HTTPException(status_code=400, detail=f"Implausible hardware profile: {hardware_reason}")

    # --- 4. Contribution baseline -----------------------------------------------------
    # gpu_section, not hw.get("gpu", {}): a CPU-only node sends an explicit JSON null,
    # which validate_hardware_profile deliberately allows, and the dict default does not
    # apply to a key that is present-but-null. Chaining .get onto that None raised
    # AttributeError — a remote, unauthenticated HTTP 500 whenever ALLOW_UNSIGNED_WORKERS
    # was on, and a log flood either way.
    missing = cpu_node_shortfalls(payload.hardware, get_worker_limits())
    if missing:
        log_error(f"Rejected CPU-only worker {safe_id} from {safe_ip}: needs {'; '.join(missing)}.")
        raise HTTPException(status_code=403, detail=cpu_refusal_text(missing),
                            headers={CPU_REFUSAL_HEADER: CPU_REFUSAL_VALUE})

    # --- 5. Registration --------------------------------------------------------------
    registry.update_worker(
        payload.worker_id, client_ip, payload.port, payload.token, payload.hardware,
        role=payload.role, staged_version=payload.staged_version,
    )

    # Keep the GPU perks' clock: a GPU that has a job has no standing-by time, one that has none starts it.
    if _as_number(gpu_section(payload.hardware).get("vram_total_gb", 0)) > 0:
        _gpu_note_job_state({"worker_id": payload.worker_id, "ip": client_ip, "port": payload.port,
                             "hardware": payload.hardware})

    # A CPU pool member is told which model tier to serve (engine/app_pool.py).
    answer: Dict[str, Any] = {"status": "ok"}
    if is_app_member(payload.hardware):
        tier = app_pool.assignment(_as_number(payload.hardware.get("ram_gb", 0)), _app_members(),
                                   int(get_worker_limits().get("app_tier_min_members", 2)))
        answer = {"status": "ok", "app_model": tier}

    # With coding members on, every worker is also told what to be (engine/coding_router.assign_role).
    # Off, the answer is exactly what it always was.
    if get_worker_limits().get("coding_members_enabled", False):
        answer.update(_coding_role_answer(payload.worker_id, client_ip, payload.port, payload.hardware))
    return answer


def check_contrib_clause(request: Request):
    """Enforces the Non-Contrib Clause: blocks any IP not actively running a Worker or presenting a valid API key,
    while permitting new community users a free starter quota (default: 100 chat requests) for evaluation."""
    if not get_worker_limits().get("enforce_contrib_clause", True):
        return

    # If already consumed/checked by NonContribBlockerMiddleware
    if hasattr(request.state, "starter_quota_remaining"):
        return

    ip = get_client_ip(request)
    if is_local_ip(ip):
        return  # Primary Coordinator is exempt

    token = get_request_token(request)
    if token and api_key_manager.verify_key(token):
        return  # Bearer API Key is authorized

    registry.prune_dead_workers()
    if token and registry.find_by_token(token) is not None:
        return  # Active contributing worker node

    # Starter trial quota for new community users / evaluation
    quota_limit = int(get_worker_limits().get("starter_quota_inferences", 100))
    if quota_limit > 0:
        allowed, _remaining = use_starter_quota(request, trial_identity(ip), quota_limit)
        if allowed:
            return

    raise HTTPException(
        status_code=403,
        detail=(
            f"DLNE Compute Required: Your free starter quota ({quota_limit}/{quota_limit} inferences) "
            "has been used. Under DLNE's policy ('Give compute to receive compute'), please run "
            "'@dlne /contribute' to donate idle GPU VRAM and unlock unlimited frontier coding access, "
            "or configure an authorized API key."
        )
    )


# ======================================================================================
# Model catalogue
# ======================================================================================

@app.get("/v1/models")
async def get_models(request: Request):
    """
    OpenAI API compatible endpoint to list discovered GGUF models.

    ``aget_available_models``, not the synchronous original: the listing globs the models
    directory and stats every file in it, which on a network-mounted models folder is
    unbounded blocking I/O. Run straight from this coroutine it stalls every other request,
    broadcast and heartbeat the loop is serving.
    """
    total_vram = registry.get_total_vram_gb()
    total_sysram = registry.get_total_sysram_gb()
    return await manager.aget_available_models(total_vram_gb=total_vram, total_sysram_gb=total_sysram)


@app.get("/v1/models.json")
async def get_models_json(request: Request):
    """Endpoint for Redundancy Nodes to sync the active models.json registry."""
    models_json_path = os.path.join(get_base_dir(), "models", "models.json")
    if not os.path.exists(models_json_path):
        load_models_json()  # forces creation
    return FileResponse(models_json_path, media_type="application/json")


@app.get("/v1/release")
async def get_release_manifest():
    """
    The signed manifest of the engine archive served at /v1/download/source: its version, its
    SHA-256 and the maintainer's Ed25519 signature. A node checks all three before it runs a
    byte of what it downloaded. Public on purpose: it carries no secret, only proof.
    """
    manifest = release_lib.load_manifest(os.path.join(get_base_dir(), release_lib.MANIFEST_NAME))
    if manifest is None:
        raise HTTPException(status_code=404, detail="This coordinator has no signed release manifest.")
    return manifest


def _release_summary() -> Dict[str, Any]:
    """What the operator needs to know about the served release, for /v1/admin/stats."""
    manifest = release_lib.load_manifest(os.path.join(get_base_dir(), release_lib.MANIFEST_NAME))
    if manifest is None:
        return {"published": False}
    verified, reason = release_lib.verify_manifest(manifest, release_lib.RELEASE_PUBKEY)
    return {
        "published": True,
        "version": manifest.get("version"),
        "sha256": manifest.get("sha256"),
        "git_commit": manifest.get("git_commit"),
        "built_at": manifest.get("built_at"),
        "signature_valid": verified,
        "detail": reason,
        "llama_build": manifest.get("llama_build"),
        "rpc_proto": manifest.get("rpc_proto"),
        "matches_running_code": manifest.get("version") == VERSION,
    }


@app.get("/v1/download/source")
async def download_engine_source():
    """Serves the lightweight DLNE engine Python source archive (~850 KB)."""
    zip_path = os.path.join(get_base_dir(), "dlne-source.zip")
    if os.path.exists(zip_path):
        return FileResponse(zip_path, filename="dlne-source.zip", media_type="application/zip")
    raise HTTPException(status_code=404, detail="Source archive not found")


def _apply_rpc_requirement() -> None:
    """
    Route only to workers whose rpc-server speaks this Coordinator's RPC protocol major.

    Taken from the signed release this Coordinator serves (rpc_proto), which names the very
    llama.cpp build the coordinator runs and ships to workers. Without a verified release
    there is no requirement, as before.
    """
    manifest = release_lib.load_manifest(os.path.join(get_base_dir(), release_lib.MANIFEST_NAME))
    major = None
    if manifest is not None and release_lib.verify_manifest(manifest)[0]:
        major = release_lib.proto_major(manifest.get("rpc_proto"))
    registry.required_rpc_major = major
    if major is not None:
        log_coordinator(f"Workers must run llama.cpp RPC protocol {major}.x ({manifest.get('llama_build')}).")


@app.get("/v1/download/rpc-bin/{platform}")
async def download_rpc_bin(platform: str):
    """
    Serves the ggml-rpc-server build for one platform (win-x64, linux-x64, macos-arm64). Each
    zip's size and SHA-256 are signed into /v1/release, which is what a node checks.
    """
    if platform not in release_lib.RPC_PLATFORMS:
        raise HTTPException(status_code=404, detail="No RPC build for that platform.")
    zip_path = os.path.join(get_base_dir(), release_lib.rpc_zip_name(platform))
    if os.path.exists(zip_path):
        return FileResponse(zip_path, filename=release_lib.rpc_zip_name(platform), media_type="application/zip")
    raise HTTPException(status_code=404, detail="RPC binary archive not found")


@app.get("/v1/download/rpc-bin")
async def download_windows_rpc_bin():
    """The Windows build, at the address extensions before 1.2.0 ask for."""
    return await download_rpc_bin("win-x64")


CURATED_MODELS = {
    "phi3-mini": 2.3,
    "qwen3:4b": 2.5,
    "gemma3:4b": 2.5,
    "llama3.1:8b": 4.8
}


async def get_remote_model_size_gb_async(model_name: str) -> float:
    """Pre-flight check: Connects to HuggingFace to extract the exact file size before downloading."""
    try:
        library = load_models_json()
        hf_url = ""
        for m in library:
            if m.get("id") == model_name:
                hf_url = m.get("hf_url", "")
                break

        if not hf_url:
            return 0.0

        async with httpx.AsyncClient(follow_redirects=True, timeout=5.0) as client:
            response = await client.head(hf_url)
            if response.status_code == 200:
                length = int(response.headers.get("content-length", 0))
                if length > 0:
                    return length / (1024**3)
    except Exception as e:
        log_error(f"Failed to get remote model size for {safe_log_text(model_name)}: {safe_log_text(e, max_chars=200)}")
    return 0.0


class ModelRequestPayload(BaseModel):
    model_name: str
    disk_gb: float = 5.0  # fallback
    model_config = {"protected_namespaces": ()}


@app.post("/v1/models/request", dependencies=[Depends(check_contrib_clause)])
async def request_model(payload: ModelRequestPayload, request: Request):
    limits = get_worker_limits()
    if limits.get("enforce_model_whitelist", False):
        if payload.model_name not in CURATED_MODELS:
            raise HTTPException(status_code=403, detail=f"Model {payload.model_name} is not on the admin whitelist.")

    # Check if model is already downloaded locally. aresolve_model_path: resolution does a
    # realpath and an isfile per search root, which is filesystem work this coroutine must
    # not do on the event loop.
    model_exists = False
    resolved = await manager.aresolve_model_path(payload.model_name)
    if resolved and os.path.exists(resolved):
        model_exists = True

    if not model_exists:
        # 1. Pre-flight Math Check
        actual_size_gb = await get_remote_model_size_gb_async(payload.model_name)
        if actual_size_gb == 0.0:
            # Fallback to the payload estimate if the pre-flight HEAD request was refused
            actual_size_gb = payload.disk_gb

        # Network Pool Check
        total_vram = registry.get_total_vram_gb()
        total_sysram = registry.get_total_sysram_gb()
        network_capacity = total_vram + total_sysram
        estimated_vram_needed = actual_size_gb * 1.2

        if estimated_vram_needed > network_capacity:
            raise HTTPException(status_code=400, detail=f"Network Pool Exceeded! Model requires {estimated_vram_needed:.1f}GB memory, but network only has {network_capacity:.1f}GB available.")

        # SSD Buffer Check.
        # Measured against the directory the model will actually be written to, not the
        # process CWD: the CWD is wherever the user double-clicked from (or C:\Windows\System32
        # for a service), so ``disk_usage(".")`` could easily report a completely different
        # volume than the one about to receive a multi-GB file.
        models_dir = os.path.join(get_base_dir(), "models")
        try:
            os.makedirs(models_dir, exist_ok=True)
            _total, _used, free = shutil.disk_usage(models_dir)
        except OSError:
            # If the models directory cannot be created or stat'd, fall back to the install
            # root, which certainly exists.
            _total, _used, free = shutil.disk_usage(get_base_dir())

        free_gb = free / (1024**3)
        expected_free_gb = free_gb - actual_size_gb

        if expected_free_gb < 20.0:
            log_error(f"Rejected model request for {safe_log_text(payload.model_name)}: Model size ({actual_size_gb:.1f}GB) would leave Coordinator SSD with < 20GB free (Currently {free_gb:.1f}GB).")
            raise HTTPException(status_code=400, detail=f"Cluster Storage Limit Reached! The Coordinator SSD must maintain a 20GB buffer. Downloading this model would leave it with only {expected_free_gb:.1f}GB.")

        log_coordinator(f"\n[bold yellow]*** NEW MODEL REQUEST ***[/bold yellow]\nClient requested model: {safe_log_text(payload.model_name)} (Accurate Size: {actual_size_gb:.2f}GB)\nInitiating autonomous download...\n")

    # Spawn background pull as an asyncio task to stream progress
    async def background_pull_async(model_name: str):
        try:
            success = False
            library = load_models_json()
            hf_url = ""
            dflash_url = ""
            for m in library:
                if m.get("id") == model_name:
                    hf_url = m.get("hf_url", "")
                    dflash_url = m.get("dflash_url", "")
                    break

            if not hf_url:
                log_error(f"Cannot download {safe_log_text(model_name)}: No HuggingFace URL found in models.json.")
                await broadcast({"type": "download_progress", "model": model_name, "percent": -1})
                return

            custom_dir = os.path.join(get_base_dir(), "models")
            os.makedirs(custom_dir, exist_ok=True)

            # Sanitize filename for Windows (':' is not a legal path character there)
            safe_model_name = model_name.replace(":", "-")
            dest_path = os.path.join(custom_dir, f"{safe_model_name}.gguf")

            success = await download_file_multistream_async(hf_url, dest_path, model_name)

            if success and dflash_url:
                dflash_dest_path = os.path.join(custom_dir, f"{safe_model_name}_dflash.gguf")
                log_coordinator(f"Main model downloaded. Now downloading DFlash companion for {safe_log_text(model_name)}...")
                success = await download_file_multistream_async(dflash_url, dflash_dest_path, f"{model_name} (DFlash)")

            if success:
                log_coordinator(f"Successfully downloaded {safe_log_text(model_name)}! It is now available to the cluster.")
            else:
                log_error(f"Failed to download {safe_log_text(model_name)}. Please check the model name and try again.")

            await broadcast({"type": "download_progress", "model": model_name, "percent": 100 if success else -1})
        except Exception as e:
            log_error(f"Failed to pull model {safe_log_text(model_name)}: {safe_log_text(e, max_chars=200)}")
            await broadcast({"type": "download_progress", "model": model_name, "percent": -1})

    spawn_background_task(background_pull_async(payload.model_name), f"model-pull:{payload.model_name}")
    return {"status": "ok", "message": "Model download initiated."}


# ======================================================================================
# Operator (admin) API
# ======================================================================================
#
# One credential opens everything under /v1/admin/*: the operator key (ADMIN_KEY_HASH), held
# by BarrkBOT on the coordinator's own host. The browser password login that used to live
# here went with the web UI it served.

# When this process started, for the uptime figure in /v1/admin/stats.
SERVER_STARTED_AT = time.time()


def _admin_key_matches(token: str) -> bool:
    """
    True when ``token`` is the operator key whose SHA-256 is stored as ADMIN_KEY_HASH.

    The key is a long random string (see ``dlne_server.py adminkey create``), so a plain
    SHA-256 is enough: there is no password to guess, and only the hash is kept on disk.
    The comparison is constant-time so response timing reveals nothing about the hash.
    """
    expected = get_worker_limits().get("admin_key_hash", "")
    if not expected or not token:
        return False
    return hmac.compare_digest(hash_admin_key(token), expected)


@app.get("/v1/admin/telemetry")
async def get_admin_telemetry(request: Request):
    _require_admin(request)

    # Return deep dive stats
    return {
        "active_model": global_active_model,
        "cluster_status": cluster_status,
        "total_nodes": registry.count(),
        "total_vram_gb": registry.get_total_vram_gb(),
        "total_sysram_gb": registry.get_total_sysram_gb(),
        # snapshot(), not the live dict: FastAPI serialises this after the handler
        # returns, by which point a prune could be halfway through mutating it.
        "nodes": registry.snapshot(),
        "running_inference_endpoints": manager.current_endpoints
    }


def _may_choose_model(request: Request) -> bool:
    """
    Whether this caller may name the model its request runs on, or swap the cluster's: this
    machine, holders of an operator-issued API key, or anyone on a cluster that does not
    enforce the contribution clause. Everyone else gets the model the swarm is serving. A model
    named by a public caller used to be started for them, swapping the live model out for
    everybody when the pool was full (ops watch, launch night); extension 1.2.4 and older name
    one on every request.
    """
    return getattr(getattr(request, "state", None), "dlne_tier", None) in ("host", "key", "open")


def _require_admin(request: Request) -> None:
    """
    Bearer gate for every /v1/admin/* endpoint (telemetry, stats, API keys, trial resets,
    benchmark).

    The only credential is the operator key (ADMIN_KEY_HASH), sent as
    ``Authorization: Bearer <key>``. It is honoured only from this machine unless
    ADMIN_ALLOW_REMOTE is on, so a leaked key is useless from the internet. With no key
    configured every call is refused.
    """
    auth = request.headers.get("Authorization", "")
    token = auth[7:].strip() if auth.startswith("Bearer ") else ""

    if token and _admin_key_matches(token):
        if get_worker_limits().get("admin_allow_remote") or is_local_ip(get_client_ip(request)):
            return
        raise HTTPException(status_code=403, detail="The admin key is not accepted from this address.")

    raise HTTPException(status_code=403, detail="Unauthorized")


@app.get("/v1/admin/stats")
async def admin_stats(request: Request):
    """
    One compact snapshot for the operator's Discord bot: version, uptime, cluster, free-trial
    usage, API keys and any pending update. Deliberately omits per-node addresses and every
    client identifier (that is what /v1/admin/telemetry is for).
    """
    _require_admin(request)

    chat_limit = int(get_worker_limits().get("starter_quota_inferences", 100))
    keys = api_key_manager.list_keys()
    return {
        "server_version": VERSION,
        "uptime_seconds": int(time.time() - SERVER_STARTED_AT),
        "cluster": {
            "status": cluster_status,
            "active_model": global_active_model,
            "running_models": manager.get_active_model_names(),
            "workers": registry.count(),
            "total_vram_gb": registry.get_total_vram_gb(),
            "total_sysram_gb": registry.get_total_sysram_gb(),
            "in_flight_requests": active_inference_requests,
            "active_leases": lease_manager.active_count(),
        },
        "trial": {"chat_limit": chat_limit, **starter_quota_mgr.summary(chat_limit)},
        "api_keys": {
            "active": sum(1 for k in keys if not k.get("revoked")),
            "revoked": sum(1 for k in keys if k.get("revoked")),
        },
        "release": _release_summary(),
    }


@app.get("/v1/admin/quota")
async def admin_quota(request: Request):
    """Free-trial usage: totals plus the heaviest users, with identifiers masked."""
    _require_admin(request)
    chat_limit = int(get_worker_limits().get("starter_quota_inferences", 100))
    return {"chat_limit": chat_limit, **starter_quota_mgr.summary(chat_limit, top=10)}


@app.post("/v1/admin/quota/reset")
async def admin_quota_reset(request: Request):
    """Give one client (``{"client_id": "..."}``) or everyone (``{"all": true}``) a fresh trial."""
    _require_admin(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Request body is not valid JSON.")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object.")

    if body.get("all") is True:
        removed = starter_quota_mgr.reset(None)
    elif isinstance(body.get("client_id"), str) and body["client_id"]:
        removed = starter_quota_mgr.reset(body["client_id"])
    else:
        raise HTTPException(status_code=400, detail='Send {"client_id": "..."} or {"all": true}.')
    log_coordinator(f"Admin reset the free trial for {removed} client(s).")
    return {"status": "reset", "clients_reset": removed}


@app.get("/v1/admin/canaries")
async def admin_canaries(request: Request):
    """Coding members flagged by a canary (a decoy key or link was used), the latest detections,
    and how many decoys have been sent. See engine/canary.py."""
    _require_admin(request)
    return canary.admin_view()


@app.delete("/v1/admin/canaries/{trust_id}")
async def admin_canary_clear(trust_id: str, request: Request):
    """Clear a flagged member (by its trust id, as listed above) after the operator has looked into
    it; it may receive traffic again."""
    _require_admin(request)
    if not canary.book().unflag(trust_id):
        raise HTTPException(status_code=404, detail="That member is not flagged.")
    return {"status": "cleared", "trust_id": trust_id}


@app.post("/v1/admin/benchmark")
async def admin_benchmark(request: Request):
    """
    Measure the running engine and file the result.

    Admin-gated because it occupies a generation slot on a shared cluster — a benchmark is
    a real inference request, and on a small cluster it is one another user is not getting.

    The model must already be loaded. Starting it here would fold the load time into the
    generation rate and produce a number that means nothing, and the load time is worth
    measuring separately anyway.
    """
    _require_admin(request)

    try:
        body = await request.json()
    except Exception:
        body = {}

    model_path = body.get("model") or global_active_model
    if not model_path:
        raise HTTPException(
            status_code=400,
            detail="No model specified and no model is currently selected on the cluster.",
        )

    result = await benchmark.run_inference_benchmark(
        manager,
        model_path,
        prompt=body.get("prompt") or benchmark.DEFAULT_PROMPT,
        max_tokens=int(body.get("max_tokens") or benchmark.DEFAULT_MAX_TOKENS),
        source=body.get("source") or "admin",
    )
    return result


@app.get("/v1/admin/benchmark")
async def admin_benchmark_history(request: Request):
    """Recent results from both ends, newest last, for the WebUI's table."""
    _require_admin(request)
    return {"results": await asyncio.to_thread(benchmark.read_records, 50)}


@app.api_route("/v1/net/probe", methods=["GET", "POST"],
               dependencies=[Depends(check_contrib_clause)])
async def net_probe(request: Request):
    """
    A deliberately boring endpoint whose only job is to move bytes so a node can time it.

    POST a payload and it reports how much arrived; GET with ``?bytes=`` and it sends that
    many back. That is the whole of it — no work, no state, nothing to interpret. A node
    measuring its own link needs a peer that will reliably shift a known quantity of data,
    and inventing one out of the existing endpoints would mean timing something whose cost
    is dominated by what it does rather than by the network.

    Gated by the contribution clause like every other public route, and capped: without a
    ceiling, ``?bytes=`` is an arbitrary allocation request pointed at the Coordinator.
    """
    if request.method == "POST":
        payload = await request.body()
        return {"received_bytes": len(payload)}

    try:
        requested = int(request.query_params.get("bytes", benchmark.DEFAULT_PROBE_BYTES))
    except (TypeError, ValueError):
        requested = benchmark.DEFAULT_PROBE_BYTES
    requested = max(1, min(requested, benchmark.MAX_PROBE_BYTES))
    return Response(content=b"\0" * requested, media_type="application/octet-stream")


@app.post("/worker/link_report")
async def worker_link_report(request: Request):
    """
    A node telling the Coordinator what its link is worth, so both ends agree on one story.

    Authenticated by the worker's own registry token rather than an admin credential: the
    node is reporting a fact about itself, and requiring an operator credential would mean
    the measurement only ever existed on machines the operator was sitting at.

    Stored on the worker's registry record and filed alongside the Coordinator's own
    results, which is what makes ``logs/benchmarks.jsonl`` a complete picture of a cluster
    rather than a log of one end of it.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Request body is not valid JSON.")

    token = get_request_token(request)
    registry.prune_dead_workers()
    worker = registry.find_by_token(token)
    if worker is None:
        raise HTTPException(status_code=403, detail="Unknown or expired worker token.")

    measurement = body.get("measurement")
    if not isinstance(measurement, dict):
        raise HTTPException(status_code=400, detail="No measurement in the request body.")

    worker_id = worker.get("worker_id", "")
    registry.record_link_measurement(worker_id, measurement)

    record = dict(measurement)
    record.update({
        "kind": "link",
        "worker_id": worker_id,
        "worker_ip": worker.get("ip", ""),
        "recorded_at": time.time(),
        "source": "worker-report",
    })
    await asyncio.to_thread(benchmark.append_record, record)

    log_coordinator(
        f"Link report from {safe_log_text(worker_id.split('-')[0][:4].upper(), max_chars=8)}: "
        f"{measurement.get('rtt_min_ms')} ms RTT, "
        f"{measurement.get('download_mbps')} Mbps down, "
        f"{measurement.get('upload_mbps')} Mbps up."
    )
    return {"ok": True}


# ======================================================================================
# Inference
# ======================================================================================

# What a user is told when their request collided with the cluster rearranging itself — a
# worker joining or leaving, or an engine being restarted onto a new topology. It is not an
# error: the request simply needs sending again once the dust settles. Delivered in-band as
# SSE text rather than as an HTTP status, because a chat client renders text and turns a 500
# into a lost message and an error banner.
CLUSTER_RECONFIGURING_NUDGE = (
    "\n\n[System] The cluster is reorganising itself right now (a node joined or left, or an "
    "engine is being restarted). Nothing has gone wrong — please send your message again in "
    "a moment."
)

# What a user is told while the engine is still reading a model off disk.
#
# llama.cpp answers 503 for the whole of that window, and every non-200 used to be funnelled
# into one branch that prefixed it with "[System Alert] Error from internal engine". So a
# model part-way through loading — the single most normal thing that happens after choosing
# one — was announced in the same words as a crashed engine, with no indication that waiting
# would fix it. Loading a multi-gigabyte model takes minutes when the weights are on a
# network share or a slow disk, and every message sent during those minutes said "Error".
#
# Not logged as an error either, for the same reason: an operator scanning for the cause of a
# real fault should not have to wade through hundreds of lines describing a model loading
# normally.
MODEL_LOADING_NUDGE = (
    "\n\n[System] The engine is still loading this model. Large models take a few minutes to "
    "read from disk, and nothing has gone wrong — please send your message again shortly."
)

# How long the Coordinator waits on its own connection to llama-server.
#
# This was a single `timeout=60.0`, which httpx applies to connect, read, write and pool
# alike — and one number cannot be right for all four. Sixty seconds is absurdly generous
# for a connect to a loopback port, and far too mean for a first token: an engine that is
# still streaming weights out to a remote RPC backend answers nothing for minutes, which is
# normal and not a fault. Measured on the live cluster on 2026-08-10, a sharded request
# failed at exactly 60.49 s and was reported to the user as a crashed node.
#
# The read budget is deliberately long enough to cover a genuine distributed load. It is a
# backstop against a truly wedged engine, not a latency target — the thing that makes a slow
# load *visible* is the loading nudge, not a short timeout.
ENGINE_CONNECT_TIMEOUT_SECONDS = 10.0
ENGINE_READ_TIMEOUT_SECONDS = 900.0

# The pooled client for the Coordinator's calls into its own llama-server engines.
#
# A new httpx.AsyncClient per request loads the CA bundle (about 3 ms, with spikes near
# 30 ms, measured on TheWall) and opens a fresh connection, all for a loopback hop.
# Idle connections are dropped after 2 s, inside llama-server's 5 s keep-alive, so a
# request never lands on a socket the engine has already closed. Keyed by event loop
# because pooled connections cannot cross loops (each TestClient runs its own).
_engine_http: Optional[Tuple[Any, httpx.AsyncClient]] = None


@contextlib.asynccontextmanager
async def engine_client():
    """Yields the shared engine client; leaving the block does not close it."""
    global _engine_http
    loop = asyncio.get_running_loop()
    if _engine_http is None or _engine_http[0] is not loop or _engine_http[1].is_closed:
        _engine_http = (loop, httpx.AsyncClient(limits=httpx.Limits(keepalive_expiry=2.0)))
    yield _engine_http[1]

# Request slots llama-server is started with for ordinary traffic, used before the engine has reported
# its own count. Autocomplete only runs while one is free; once a chat has sized the gate to the engine's
# real slots (ENGINE_SLOTS, or as many as fit), it follows that instead (_fim_engine_slots).
FIM_ENGINE_SLOTS = 2


def _fim_engine_slots() -> int:
    """The engine's request slots as the coding gate knows them, never fewer than FIM_ENGINE_SLOTS."""
    return max(FIM_ENGINE_SLOTS, int(coding_gate.stats().get("slots") or 0))

# Ghost text on the hybrid coding model: a prefix-only warm-up goes ahead of each fill-in-the-middle
# completion (engine/fim_warm.py; coding members do the same on their own engine).
from engine.fim_warm import fim_warm_body  # noqa: E402 - kept beside the autocomplete settings it serves


# Bound at import, deliberately: the test-suite replaces ``coordinator.httpx`` wholesale with
# a stub namespace, and an ``except httpx.ReadTimeout`` resolved at raise-time against that
# stub would fail with AttributeError while handling another exception. These names are
# resolved once, here, from the real module.
_ENGINE_TIMEOUT_ERRORS = (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)
_ENGINE_CONNECT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout)
_ENGINE_DROPPED_ERRORS = (
    httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError,
    ConnectionResetError, ConnectionAbortedError, BrokenPipeError,
)


def _engine_failure_report(exc: BaseException, model_path: str) -> Tuple[str, str, bool]:
    """
    Turn an exception from the llama-server stream into an honest log line and user message.

    THE MESSAGE THIS REPLACES ASSERTED THINGS IT NEVER CHECKED
        Every failure here used to produce the single string "[System Alert: Node
        disconnected. Insufficient VRAM. Model evicted.]" — a three-part diagnosis of a
        cause the code had not established. On the failure that prompted this it was none
        of the three: no node had disconnected, VRAM was never short, nothing was evicted.
        The engine had simply not answered inside a sixty-second budget while loading a
        model across the tunnel. The real exception was logged and the user was told a
        confident, wrong story, which is worse than saying nothing — it sends whoever is
        debugging to look at hardware that is fine. That cost days on the live cluster.

    So each branch below reports only what the exception actually proves, and the fallback
    names the exception type rather than guessing. A dropped connection is still called a
    disconnection, because for that class it is genuinely what happened.

    Returns:
        ``(log_line, user_message, is_error)``. ``is_error`` is False for the states that
        are transient and expected, so they do not fill an operator's log with alarms.
    """
    model = safe_log_text(model_path)
    detail = safe_log_text(exc, max_chars=200)

    if isinstance(exc, _ENGINE_TIMEOUT_ERRORS):
        return (
            f"Engine for '{model}' did not respond within "
            f"{ENGINE_READ_TIMEOUT_SECONDS:.0f}s ({detail}). It is most likely still "
            "distributing the model to remote nodes.",
            "\n\n[System] The engine has not answered yet. This usually means it is still "
            "distributing this model across the cluster, which takes longer the more of it "
            "has to travel over the network. Nothing has crashed — please try again in a "
            "few minutes, or pick a smaller model.",
            False,
        )

    if isinstance(exc, _ENGINE_CONNECT_ERRORS):
        return (
            f"Could not open a connection to the engine for '{model}' ({detail}). The "
            "process is up but not accepting connections yet.",
            MODEL_LOADING_NUDGE,
            False,
        )

    if isinstance(exc, _ENGINE_DROPPED_ERRORS):
        return (
            f"Connection to the engine for '{model}' dropped mid-stream ({detail}).",
            "\n\n[System Alert: Node disconnected mid-response.]\n\nThe cluster will "
            "reorganise itself automatically. Please send your message again.",
            True,
        )

    return (
        f"Engine stream for '{model}' failed with {type(exc).__name__}: {detail}",
        f"\n\n[System Alert] The engine stream failed ({type(exc).__name__}). "
        "This has been logged. Please send your message again.",
        True,
    )


async def _probe_engine_port(port: int) -> bool:
    """
    True when something is accepting TCP connections on ``127.0.0.1:port``.

    This is how the chat handler decides a freshly spawned llama-server has finished loading
    its weights and bound its socket. It is a named function rather than four lines inline so
    that the readiness loop reads as one decision, and so the test-suite can replace the
    probe without having to monkey-patch ``asyncio.open_connection`` globally.
    """
    try:
        _reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:
        # Connection refused is the overwhelmingly common case and simply means "not bound
        # yet"; anything else (a socket error, a cancelled connect) is equally "not ready".
        return False


# The "viral traffic" reboot (more than 12 requests in flight -> stop the engine, start it again with 6 slots)
# was removed in 2.4. The engine sizes its slots from the model's real KV cost when it starts (ENGINE_SLOTS, or
# as many 32,768-token slots as fit); a reboot could only repeat that count or, with ENGINE_CONTEXT fixed, split
# the same context over more slots (6 over 65,536 is 10.9K each), and it parked every request while it ran
# (load test: 4 reboots in 100 s with 20 agents, the engine down or booting for 60 of them). Waiting requests
# queue in coding_gate instead. is_recalculating_queue stays, always False, for the code that waits on it.


# Recognises a model's function-call output, in the two shapes the catalog's model families
# emit it: Llama 3.x writes a bare {"name": ..., "parameters": {...}} object as plain content,
# while Qwen and Hermes wrap {"name": ..., "arguments": {...}} in <tool_call> tags.
_TOOL_CALL_TAG_RE = re.compile(r"</?(tool_call|function_call|tool▁call)[^>]*>", re.IGNORECASE)


def summary_is_usable(text: str) -> bool:
    """
    Decides whether a context summary is prose worth injecting into a conversation.

    WHY THIS EXISTS
        The memory-module pass asks the *user's own model* to summarise the overflowing
        history, and splices whatever comes back into the next prompt. Nothing checked
        that the reply was prose. A model that answered with a function call instead —
        ``{"name": "good", "parameters": {}}`` was the observed case — had that object
        injected verbatim, and a model that finds a tool call sitting in its context
        obligingly produces more of them. The symptom was a chat that answered in JSON
        for several turns and then "recovered" once the block aged out of the window.

        Model switching is what surfaced it: ``safe_context`` is computed per model, so
        changing models can collapse the per-slot budget and trip this summarisation pass
        on the first message after the switch, on a history that fit fine a moment ago.

        Rejecting is always safe. The summary is an enhancement — without it the older
        turns are simply forgotten, which is the behaviour anyone would expect from a
        full context window. Injecting a tool call is not safe, so an unusable summary is
        dropped rather than repaired.
    """
    if not isinstance(text, str):
        return False

    stripped = text.strip()
    if not stripped:
        return False

    # A tag-wrapped call never survives, even with prose around it: the tags alone are
    # enough to teach the model that this conversation is one where tool calls belong.
    if _TOOL_CALL_TAG_RE.search(stripped):
        return False

    # A whole reply that parses as a JSON object or array is structured output, not a
    # summary. Prose that merely *quotes* JSON still fails to parse as a whole and passes.
    if stripped[0] in "{[":
        try:
            json.loads(stripped)
        except ValueError:
            pass
        else:
            return False

    return True


@app.post("/v1/chat/completions", dependencies=[Depends(check_contrib_clause)])
async def chat_completions(request: Request):
    """
    OpenAI API proxy endpoint.
    Starts the model with active RPC workers and proxies the stream.

    A coding member answers a SEALED request (``X-DLNE-Route`` and a ``dlne_e2e`` body) when members
    are on (engine/coding_router.py): a member-served request holds none of the local engine's
    resources, waits for none of its swaps and does not count towards its load. Every plain request is
    this coordinator's own engine's to answer: a member is never sent a body in the clear.

    A STREAMED request from the DLNE extension (it sends ``X-DLNE-Session``) takes its engine slot
    here, so that one that has to wait can say so: it is answered at once with
    ``data: {"dlne_queue": {...}}`` frames showing its place in line, then the model's chunks
    (docs/coding-members.md section 10; :class:`_QueuedChat`). Every other caller waits silently, as
    it always has: a generic OpenAI client would read a chunk without ``choices`` as a broken
    answer, and a 200 sent before the engine is ready would turn its errors into model text.
    """
    members_on = bool(get_worker_limits().get("coding_members_enabled", False))
    streamed = False
    try:
        body = await request.json()
        streamed = isinstance(body, dict) and body.get("stream") is True
    except Exception:  # noqa: BLE001 - the handler reports a body that is not JSON
        body = None

    if members_on:
        routed = await _route_to_member(request, "/v1/chat/completions")
        if routed is not None:
            return routed
    if not streamed or not request.headers.get("x-dlne-session"):
        return await _chat_completions_impl(request)
    if coding_gate.try_acquire():
        return await _chat_completions_impl(request, slot_held=True)
    queued = _QueuedChat(request, body, get_worker_limits())
    return StreamingResponse(queued.stream(), media_type="text/event-stream", background=BackgroundTask(queued.cleanup),
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


async def _chat_completions_impl(request: Request, slot_held: bool = False):
    """
    The local engine's answer to a chat request. ``slot_held`` means the caller already waited for
    one of the engine's request slots and passes it on: this request releases it, with everything
    else it holds, however it ends.
    """
    global is_swapping_models
    global is_recalculating_queue

    if slot_held:
        # Cancelled while a swap is under way, before release_slot exists to give the slot back.
        try:
            while is_swapping_models or is_recalculating_queue:
                await asyncio.sleep(1)
        except BaseException:
            coding_gate.release()
            raise

    while is_swapping_models or is_recalculating_queue:
        await asyncio.sleep(1)

    client_ip = get_client_ip(request)
    chat_lease = lease_manager.acquire(task_type="chat", timeout=300.0, client_id=client_ip)

    # The in-flight counter must be released exactly once per request, on every path —
    # including the malformed-body path and the 400/500/504 rejections, which previously
    # incremented and never decremented and so permanently inflated the number the
    # viral-traffic detector reads.
    await _change_inference_count(1)

    slot_released = False   # release_slot() is idempotent: at most one decrement per request
    slot_handed_off = False  # True once a StreamingResponse owns the release
    gate_held = slot_held    # True while this request holds an engine slot (coding_gate)
    gate_since = time.monotonic()    # when it took it (for the wait estimate, _coding_eta)

    async def release_slot() -> None:
        """
        Give this request's in-flight slot back, at most once.

        Idempotency matters because up to three different owners may call this for the same
        request: the handler's own ``finally``, the streaming generator's ``finally``, and
        the response's ``BackgroundTask``. Exactly one decrement must happen.

        ORDERING (item 1.6). The flag is claimed *before* the await so two callers can never
        both get past the guard and double-decrement — but it is put back if the decrement
        itself fails. Setting it and leaving it set on failure converted a retryable leak
        into a permanent one: the slot would be marked returned while the counter still held
        it, and nothing would ever try again.
        """
        nonlocal slot_released, gate_held
        if gate_held:
            gate_held = False
            _note_coding_hold(time.monotonic() - gate_since)
            coding_gate.release()
        if slot_released:
            return
        slot_released = True
        try:
            chat_lease.release()
            await _change_inference_count(-1)
        except BaseException:
            slot_released = False
            raise

    def in_band_nudge(text: str) -> StreamingResponse:
        """
        Answer with ``text`` as a one-shot SSE stream instead of an HTTP error status.

        Used for the situations that are not the user's fault and not a bug: the cluster is
        full, or it is mid-reconfiguration. A chat client shows the text; an HTTP 500 shows
        an error banner and loses the message. The generator shape (a chunk, an ``await``,
        the terminator) is the one described at length in ``stream_generator``'s docstring —
        the ``await`` between the two yields is what gives cancellation a delivery point
        inside the generator so the slot is not leaked when the client disconnects.

        Ownership of the in-flight slot passes to the returned response, which is why
        ``slot_handed_off`` is set here rather than at the call sites.
        """
        nonlocal slot_handed_off

        async def nudge_generator():
            try:
                chunk = {"choices": [{"delta": {"content": text}}]}
                yield f"data: {json.dumps(chunk)}\n\n".encode()
                # await between adjacent yields — see stream_generator for why.
                await asyncio.sleep(0)
                yield b"data: [DONE]\n\n"
            finally:
                await release_slot()

        slot_handed_off = True
        return StreamingResponse(
            nudge_generator(),
            media_type="text/event-stream",
            # The authoritative release. Attached to the *response* so it does not depend on
            # the generator above ever being resumed — see stream_generator's docstring.
            background=BackgroundTask(release_slot),
        )

    global global_active_model
    try:
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Request body is not valid JSON.")

        model_path = body.get("model")

        # Strip dangerous penalties that cause smaller models (like phi3) to hallucinate
        body["frequency_penalty"] = 0.0
        body["presence_penalty"] = 0.0

        requested = body.get("model") if _may_choose_model(request) else None
        model_path = canonical_model_tag(requested or global_active_model or _default_coding_model())
        if requested:
            # A caller who may choose, naming another rung of the coding ladder (a status read
            # just before an automatic switch), is served by the rung in use: starting the old
            # one would swap the cluster straight back (engine/auto_swap.py). Public callers'
            # model names are already ignored above.
            model_path = auto_swapper.serve_as(model_path, global_active_model)
        body["model"] = model_path

        # 1. Grab planned workers based on sharding policy and calculate resources
        req_gb = await manager.aestimate_model_vram_gb(model_path)
        active_endpoints = _planned_endpoints(req_gb)
        total_vram = registry.get_total_vram_gb()
        total_sysram = registry.get_total_sysram_gb()

        # 2. Try to spawn or locate the running model instance (Baseline 2 slots).
        #    astart_model runs the blocking Popen/terminate work in a thread so the event
        #    loop keeps serving other clients while an engine boots.
        success, assigned_port, status_msg = await manager.astart_model(
            model_path, active_endpoints, total_vram, total_sysram, queue_channels=2
        )

        # RETRY ONCE ON A RETRYABLE FAILURE (finding R3-11).
        # start_model's message is a control signal, not prose — see the sentinel block at
        # the top of engine/inference_manager.py. A "retryable" outcome means nothing is
        # wrong with the model, the request or the cluster: another thread's teardown or
        # start simply won a race, which is exactly what a routine topology restart does
        # when a worker joins or leaves mid-request. Turning that into an HTTP 500 (which is
        # what every non-"Network Maxed Out" failure used to do) surfaced a normal cluster
        # event to a user as a server error. The topology may well have changed underneath
        # us, so the retry re-reads it rather than reusing the stale list.
        if not success and is_retryable_start_failure(status_msg):
            log_coordinator(
                f"Start of '{safe_log_text(model_path)}' lost a race with a concurrent "
                f"cluster change ({safe_log_text(status_msg)}). Retrying once."
            )
            active_endpoints = _planned_endpoints(req_gb)
            total_vram = registry.get_total_vram_gb()
            total_sysram = registry.get_total_sysram_gb()
            success, assigned_port, status_msg = await manager.astart_model(
                model_path, active_endpoints, total_vram, total_sysram, queue_channels=2
            )

        if not success and status_msg == NETWORK_MAXED_OUT_MESSAGE:
            # If another model is sitting idle in VRAM (no other request holds a lease) and
            # the newly requested model fits within the total cluster memory pool, automatically
            # hot-swap out the idle model so IDE/API model switches succeed seamlessly.
            running_now = manager.get_active_model_names()
            if running_now and lease_manager.active_count() <= 1 and req_gb <= (total_vram + total_sysram):
                log_coordinator(
                    f"Auto-swapping idle model(s) {running_now} -> '{safe_log_text(model_path)}' "
                    f"({req_gb:.1f}GB required, {total_vram + total_sysram:.1f}GB pool)."
                )
                await manager.astop_all_models()
                active_endpoints = _planned_endpoints(req_gb)
                total_vram = registry.get_total_vram_gb()
                total_sysram = registry.get_total_sysram_gb()
                success, assigned_port, status_msg = await manager.astart_model(
                    model_path, active_endpoints, total_vram, total_sysram, queue_channels=2
                )

        if success:
            global_active_model = canonical_model_tag(model_path)
            # spawn_vram_progress_task, not a bare spawn: astart_model also returns success
            # for "Already running", so a busy model used to get one new SSE reader (and one
            # new open connection to llama-server) per chat request, none of which ever
            # retired on their own.
            spawn_vram_progress_task(assigned_port, model_path)

        if not success:
            if status_msg == NETWORK_MAXED_OUT_MESSAGE:
                # Graceful Rejection (The "TV Channel" Nudge). Matched against the module's
                # exported sentinel rather than a copied literal, so the two cannot drift.
                active_models = ", ".join(manager.get_active_model_names()) or "None"
                return in_band_nudge(
                    "\n\n[System] Sorry, the distributed network is currently maxed out. The "
                    f"currently active models are: [{active_models}]. Please switch your chat "
                    "to one of those models for immediate responses."
                )

            if is_retryable_start_failure(status_msg):
                # Lost the race twice. Still not an error the user caused or can act on, so
                # it gets the same in-band treatment as the maxed-out case rather than a 500.
                return in_band_nudge(CLUSTER_RECONFIGURING_NUDGE)

            if is_model_not_on_disk(status_msg):
                # The weights are still downloading. Also not a server fault — and a 500 here
                # is what made a first-run cluster look broken while it was merely busy.
                return in_band_nudge(
                    "\n\n[System] This model has not finished downloading to the cluster yet. "
                    "It will start automatically once the weights are in place — please try "
                    "again shortly."
                )

            log_error(
                f"Failed to start inference engine for '{safe_log_text(model_path)}': "
                f"{safe_log_text(status_msg)}"
            )
            raise HTTPException(status_code=500, detail="Failed to initialize inference engine.")

        # 3. Wait briefly for llama-server API to become responsive while monitoring process health
        llama_url = f"http://127.0.0.1:{assigned_port}/v1/chat/completions"

        # ONE COHERENT SNAPSHOT, TAKEN UNDER THE MANAGER'S LOCK (finding R3-9).
        # This used to be `manager.running_models.get(model_path, {}).get("process")`, an
        # unsynchronised read of another thread's dictionary straight from the event loop,
        # and it was read a second time further down for the context sizing. Two problems,
        # both observed:
        #   * When a concurrent astop_all_models/reap_model had drained the entry in the gap,
        #     `proc` came back None, and BOTH the readiness loop and the `if not engine_ready`
        #     guard were nested inside `if proc:` — so the whole block was skipped without
        #     raising and the request was proxied to a port with nothing listening.
        #   * The second read then yielded {} and fell back to safe_context=8192 /
        #     dynamic_slots=4 rather than the engine's real values, silently truncating the
        #     user's prompt to half the budget it actually had.
        # asnapshot_model returns a consistent copy or None, and None is now handled
        # explicitly instead of being indistinguishable from "no process, carry on".
        model_data = await manager.asnapshot_model(model_path)
        if model_data is None:
            # The engine we were just told about is already gone: a teardown won the race
            # between astart_model returning and this line. Same class of event as a
            # retryable start failure, so it gets the same graceful answer.
            log_warning(
                f"Engine for '{safe_log_text(model_path)}' disappeared between start and "
                "readiness check (concurrent teardown). Asking the client to retry."
            )
            return in_band_nudge(CLUSTER_RECONFIGURING_NUDGE)

        engine_ready = False
        proc = model_data.get("process")

        if proc:
            for _ in range(120):  # Up to 120 seconds for large models to load into VRAM
                if proc.poll() is not None:
                    # Process died!
                    log_error(f"Engine process for {safe_log_text(model_path)} died unexpectedly during startup (Exit code: {proc.returncode}).")

                    # Let the InferenceManager do the teardown: it closes the log file,
                    # hands the port back to its allocator and drops the entry. Doing it
                    # here meant a concurrent stop_all_models() could remove the entry
                    # first, making the bare `del` raise KeyError and skip the port
                    # release, which burned that port for the life of the process.
                    #
                    # areap_model, not reap_model (finding R3-6): the synchronous version
                    # takes the manager's _state_lock, and calling it straight from this
                    # coroutine froze the entire uvicorn event loop for as long as that lock
                    # was contended — measured at 11.81 s behind a single stop_all_models
                    # teardown, during which no request, broadcast or heartbeat was served.
                    await manager.areap_model(model_path)

                    raise HTTPException(status_code=500, detail="Inference engine crashed during startup.")

                if await _probe_engine_port(assigned_port):
                    engine_ready = True
                    break
                await asyncio.sleep(1.0)

            if not engine_ready:
                raise HTTPException(status_code=504, detail="Inference engine timed out while loading the model.")

        safe_context = model_data.get("safe_context", 8192)
        dynamic_slots = model_data.get("dynamic_slots", 4)
        slot_context = safe_context // dynamic_slots

        # Take one of the engine's request slots, by tier: GPU contributors (and API keys
        # and this host) first, CPU contributors second, free-trial users third. Held until
        # release_slot() gives it back with the rest of this request's resources.
        tier = getattr(getattr(request, "state", None), "dlne_tier", None)
        if slot_held:
            coding_gate.resize(dynamic_slots)        # already ours: only follow the engine's slot count
        else:
            await coding_gate.acquire(tier_priority(tier), slots=dynamic_slots)
        gate_held = True
        gate_since = time.monotonic()

        # Estimate tokens in the messages and truncate if they exceed (slot_context - 100)
        messages = body.get("messages", [])
        if messages:
            system_prompt = None
            system_tokens = 0
            if messages[0].get("role") == "system":
                system_prompt = messages[0]
                system_tokens = max(1, int(len(system_prompt.get("content") or "") / 3.0))

            allowed_tokens = slot_context - 512 - system_tokens
            if allowed_tokens < 100:
                allowed_tokens = 100

            truncated_history = []
            overflow_history = []
            cumulative_tokens = 0

            start_idx = 1 if system_prompt else 0
            is_overflowing = False
            messages_to_process = messages[start_idx:]
            for i, msg in enumerate(reversed(messages_to_process)):
                is_newest = (i == 0)
                msg_content = msg.get("content") or ""
                msg_tokens = max(1, int(len(msg_content) / 3.0))

                if not is_overflowing and (cumulative_tokens + msg_tokens <= allowed_tokens or is_newest):
                    if is_newest and (cumulative_tokens + msg_tokens > allowed_tokens):
                        # Forcefully slice the newest message's text so it fits the context window
                        max_chars = allowed_tokens * 3
                        if msg.get("content"):
                            msg["content"] = "...[Text omitted for memory]...\n" + msg["content"][-max_chars:]
                        cumulative_tokens += allowed_tokens
                    else:
                        cumulative_tokens += msg_tokens
                    truncated_history.append(msg)
                else:
                    is_overflowing = True
                    overflow_history.append(msg)

            truncated_history.reverse()
            overflow_history.reverse()

            final_messages = []
            if system_prompt:
                final_messages.append(system_prompt)

            if overflow_history and engine_ready:
                log_coordinator(f"Context ceiling reached. Summarizing {len(overflow_history)} older messages into a dense recall string...")
                history_text = ""
                for m in overflow_history:
                    history_text += f"{m.get('role', 'user').upper()}: {m.get('content') or ''}\n\n"

                max_summary_chars = max(1000, (slot_context - 600) * 3)
                if len(history_text) > max_summary_chars:
                    half = max_summary_chars // 2
                    history_text = history_text[:half] + "\n...[Middle text omitted for memory constraints]...\n" + history_text[-half:]

                summary_payload = {
                    "model": model_path,
                    "messages": [
                        # A coding conversation: keep the task and its state, nothing personal.
                        {"role": "user", "content": f"Summarize the earlier part of this coding conversation for the assistant continuing it: the user's goal, decisions made, files and code involved, and what is still open. Be concise and factual:\n\n{history_text}"}
                    ],
                    # Generated before the user's request is even forwarded, so it is kept short.
                    "max_tokens": 400,
                    "temperature": 0.1,
                    "stream": True
                }
                try:
                    async with engine_client() as client:
                        async with client.stream("POST", llama_url, json=summary_payload, timeout=120.0) as resp:
                            if resp.status_code == 200:
                                summary_text = ""
                                async for line in resp.aiter_lines():
                                    if line.startswith("data: "):
                                        data_str = line[6:]
                                        if data_str == "[DONE]":
                                            break
                                        try:
                                            chunk = json.loads(data_str)
                                            content = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                                            if content:
                                                summary_text += content
                                        except json.JSONDecodeError:
                                            pass

                                if not summary_is_usable(summary_text):
                                    # Not an error the admin can act on, and not fatal: the
                                    # overflowing turns are simply forgotten, as they would be
                                    # without a memory module at all.
                                    log_warning(
                                        "Discarding a context summary that was not prose - the "
                                        "model answered the memory-module prompt with structured "
                                        "output. Older turns will be dropped instead."
                                    )
                                elif summary_text:
                                    recall_str = f"\n\n[Summary of the earlier conversation: {summary_text.strip()}]"
                                    if final_messages and final_messages[0].get("role") == "system":
                                        final_messages[0]["content"] += recall_str
                                    elif truncated_history:
                                        truncated_history[0]["content"] = recall_str.strip() + "\n\n" + truncated_history[0]["content"]
                                    log_coordinator("Summary injected successfully.")
                            else:
                                err_text = await resp.aread()
                                log_error(f"Failed to summarize context (HTTP {resp.status_code}): {safe_log_text(err_text.decode('utf-8', errors='replace'), max_chars=200)}")
                except Exception as e:
                    log_error(f"Failed to summarize context ({type(e).__name__}): {e}")

            final_messages.extend(truncated_history)
            body["messages"] = final_messages

        # 4. Proxy the request to llama-server and stream the response back to WebUI
        forwarded_headers = {}
        last_event_id = request.headers.get("Last-Event-ID")
        if last_event_id:
            forwarded_headers["Last-Event-ID"] = last_event_id

        async def stream_generator():
            """
            Proxy the engine's SSE stream to the client, returning the slot when it ends.

            WHY EVERY PAIR OF ADJACENT YIELDS HAS AN ``await`` BETWEEN THEM
                A generator's ``finally`` runs only when the generator is resumed or
                closed. Starlette consumes this one with ``async for chunk in
                body_iterator: await send(chunk)``, and when the client disconnects it
                cancels that consuming task. If the cancellation lands while the generator
                is parked on a bare ``yield`` — which is exactly what two adjacent yields
                with no ``await`` between them maximise the chance of — nothing ever resumes
                the generator, the ``finally`` below never runs, and the slot is leaked for
                the life of the process. Inserting ``await asyncio.sleep(0)`` gives
                cancellation a delivery point *inside* the generator body.

                The happy path (``async for ... : yield chunk``) already has an await
                between yields, courtesy of ``aiter_bytes``; it was only the three
                early-exit branches that did not.

                That mitigation is not sufficient on its own, which is why the response also
                carries ``background=BackgroundTask(release_slot)``: Starlette awaits the
                background task after its internal task group exits, and the disconnect
                cancel is a *scoped* cancel that the ``async with`` absorbs — so the release
                happens even when this generator is abandoned mid-yield. ``release_slot`` is
                idempotent, so the two mechanisms cannot double-decrement.
            """
            try:
                async with engine_client() as client:
                    # Split, not a single scalar: connect is to a loopback port and should
                    # fail fast, while read has to survive an engine still pushing weights
                    # to remote RPC backends. See ENGINE_READ_TIMEOUT_SECONDS.
                    engine_timeout = httpx.Timeout(
                        ENGINE_READ_TIMEOUT_SECONDS,
                        connect=ENGINE_CONNECT_TIMEOUT_SECONDS,
                    )
                    async with client.stream("POST", llama_url, json=body, headers=forwarded_headers, timeout=engine_timeout) as response:
                        if response.status_code == 403:
                            msg = "\n\n[System] Feature Disabled by Coordinator Administrator (403)."
                            chunk = {"choices": [{"delta": {"content": msg}}]}
                            yield f"data: {json.dumps(chunk)}\n\n".encode()
                            await asyncio.sleep(0)
                            yield b"data: [DONE]\n\n"
                            return
                        elif response.status_code == 503:
                            # Transient by definition: llama.cpp serves 503 while a model is
                            # loading and while every request slot is busy. Both clear on
                            # their own, and both used to be reported as engine errors.
                            log_coordinator(
                                f"Engine not ready yet for '{safe_log_text(model_path)}' "
                                "(still loading, or all slots busy). Told the client to retry."
                            )
                            chunk = {"choices": [{"delta": {"content": MODEL_LOADING_NUDGE}}]}
                            yield f"data: {json.dumps(chunk)}\n\n".encode()
                            await asyncio.sleep(0)
                            yield b"data: [DONE]\n\n"
                            return
                        elif response.status_code != 200:
                            try:
                                error_bytes = await response.aread()
                                error_json = json.loads(error_bytes.decode('utf-8'))
                                error_msg = error_json.get("error", {}).get("message", f"Status code {response.status_code}")
                            except Exception:
                                error_msg = f"Status code {response.status_code}"
                            log_error(f"Inference engine error {response.status_code}: {safe_log_text(error_msg, max_chars=200)}")
                            msg = f"\n\n[System Alert] Error from internal engine: {error_msg}."
                            chunk = {"choices": [{"delta": {"content": msg}}]}
                            yield f"data: {json.dumps(chunk)}\n\n".encode()
                            await asyncio.sleep(0)
                            yield b"data: [DONE]\n\n"
                            return
                        # The answer's last few KB carry llama-server's closing "timings", the
                        # coding model's real speed (engine/coding_speed.py). Only a rolling tail
                        # is kept and nothing is parsed per chunk; it is read once, at the end.
                        tail = bytearray()
                        # Tokens are counted as they arrive (engine/throughput.py), for the website's
                        # live figure: only whether a frame carries text, never what it says.
                        counter = StreamTokenCounter(POOL_CODING, streamed=body.get("stream") is True)
                        async for chunk in response.aiter_bytes():
                            counter.feed(chunk)
                            tail += chunk
                            if len(tail) > 2 * coding_speed.TAIL_BYTES:
                                del tail[:-coding_speed.TAIL_BYTES]
                            yield chunk
                        counter.finish()
                        _measure_coding_speed(model_path, tail)
            except Exception as stream_e:
                # Report what the exception proves, and nothing beyond it. See
                # _engine_failure_report for what the single fabricated message this
                # replaces cost in debugging time.
                log_line, msg, is_error = _engine_failure_report(stream_e, model_path)
                if is_error:
                    log_error(log_line)
                else:
                    log_coordinator(log_line)
                chunk = {"choices": [{"delta": {"content": msg}}]}
                yield f"data: {json.dumps(chunk)}\n\n".encode()
                await asyncio.sleep(0)
                yield b"data: [DONE]\n\n"
            finally:
                await release_slot()

        slot_handed_off = True
        return StreamingResponse(
            stream_generator(),
            media_type="text/event-stream",
            # The authoritative release. Attached to the *response* so it does not depend on
            # the generator above ever being resumed — see stream_generator's docstring.
            background=BackgroundTask(release_slot),
        )

    except HTTPException:
        # Already a deliberate, well-described client/server error — pass it through
        # untouched. The finally block below still returns the slot.
        raise
    except Exception as e:
        log_error(f"Failed to initiate request to inference engine: {type(e).__name__}: {e}")
        raise HTTPException(status_code=502, detail="Inference engine crashed or disconnected.")
    finally:
        # Only release here if no generator took ownership; the generators call the same
        # idempotent helper from their own finally blocks.
        if not slot_handed_off:
            await release_slot()


# ======================================================================================
# FIM Text Completions (OpenAI Compatible)
# ======================================================================================

@app.post("/v1/completions", dependencies=[Depends(check_contrib_clause)])
async def completions(request: Request):
    """
    OpenAI API compatible endpoint for text completions (Fill-In-The-Middle / FIM).
    Starts or routes to the active model and proxies the completion stream or JSON response.
    Guarded by InferenceLeaseManager to prevent mid-completion restarts.
    """
    global is_swapping_models
    global is_recalculating_queue

    # Autocomplete goes to a member only while it has a slot to spare (engine/coding_router.py).
    if get_worker_limits().get("coding_members_enabled", False):
        routed = await _route_to_member(request, "/v1/completions")
        if routed is not None:
            return routed

    while is_swapping_models or is_recalculating_queue:
        await asyncio.sleep(1)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Request body is not valid JSON.")

    prompt = body.get("prompt")
    if prompt is None:
        raise HTTPException(status_code=400, detail="No prompt specified in request body.")

    # Never pile up keystroke completions: one at a time, and only while the engine has a
    # request slot free. This used to refuse whenever ANY lease was active, so one user's
    # chat turn switched ghost text off for everybody until it finished.
    # Autocomplete never queues: it runs only on a slot nobody is waiting for, so it can
    # never delay a chat at any tier (engine/priority.CodingGate).
    if (lease_manager.active_count(task_type="completion") > 0
            or lease_manager.active_count() >= _fim_engine_slots()
            or coding_gate.stats()["waiting"] > 0):
        return {"choices": [{"text": "", "index": 0, "finish_reason": "busy"}]}

    client_ip = get_client_ip(request)
    comp_lease = lease_manager.acquire(task_type="completion", timeout=15.0, client_id=client_ip)

    await _change_inference_count(1)
    slot_released = False
    slot_handed_off = False

    async def release_slot() -> None:
        nonlocal slot_released
        if slot_released:
            return
        slot_released = True
        try:
            comp_lease.release()
            await _change_inference_count(-1)
        except BaseException:
            slot_released = False
            raise

    try:
        requested = body.get("model") if _may_choose_model(request) else None
        model_path = requested or global_active_model
        if requested:
            # Same ladder rule as the chat path: another rung of the coding ladder means the rung in use.
            model_path = auto_swapper.serve_as(model_path, global_active_model)
        if not model_path:
            raise HTTPException(status_code=400, detail="No model specified and no global active model.")

        body["model"] = model_path
        is_stream = bool(body.get("stream", False))

        req_gb = await manager.aestimate_model_vram_gb(model_path)
        active_endpoints = _planned_endpoints(req_gb)
        total_vram = registry.get_total_vram_gb()
        total_sysram = registry.get_total_sysram_gb()

        success, assigned_port, status_msg = await manager.astart_model(
            model_path, active_endpoints, total_vram, total_sysram, queue_channels=2
        )

        if not success and is_retryable_start_failure(status_msg):
            active_endpoints = _planned_endpoints(req_gb)
            total_vram = registry.get_total_vram_gb()
            total_sysram = registry.get_total_sysram_gb()
            success, assigned_port, status_msg = await manager.astart_model(
                model_path, active_endpoints, total_vram, total_sysram, queue_channels=2
            )

        if not success:
            log_error(f"Failed to start engine for completion '{safe_log_text(model_path)}': {safe_log_text(status_msg)}")
            raise HTTPException(status_code=500, detail=f"Failed to initialize inference engine: {status_msg}")

        # Probe readiness
        llama_url = f"http://127.0.0.1:{assigned_port}/v1/completions"
        engine_ready = False
        for _ in range(30):
            if await _probe_engine_port(assigned_port):
                engine_ready = True
                break
            await asyncio.sleep(0.5)

        if not engine_ready:
            raise HTTPException(status_code=504, detail="Inference engine timed out while loading the model.")

        forwarded_headers = {"Content-Type": "application/json"}

        warm = fim_warm_body(body)
        if warm is not None:
            try:
                async with engine_client() as client:
                    await client.post(llama_url, json=warm, headers=forwarded_headers,
                                      timeout=httpx.Timeout(20.0, connect=ENGINE_CONNECT_TIMEOUT_SECONDS))
            except Exception:  # noqa: BLE001 - best effort; the real request below is complete without it
                pass

        if is_stream:
            async def stream_generator():
                try:
                    async with engine_client() as client:
                        engine_timeout = httpx.Timeout(ENGINE_READ_TIMEOUT_SECONDS, connect=ENGINE_CONNECT_TIMEOUT_SECONDS)
                        async with client.stream("POST", llama_url, json=body, headers=forwarded_headers, timeout=engine_timeout) as response:
                            if response.status_code != 200:
                                err = await response.aread()
                                yield f"data: {json.dumps({'error': err.decode('utf-8', errors='ignore')})}\n\n".encode()
                                return
                            counter = StreamTokenCounter(POOL_CODING)
                            async for chunk in response.aiter_bytes():
                                counter.feed(chunk)
                                yield chunk
                            counter.finish()
                except Exception as e:
                    log_error(f"Error streaming completion: {e}")
                    yield f"data: {json.dumps({'error': str(e)})}\n\n".encode()
                finally:
                    await release_slot()

            slot_handed_off = True
            return StreamingResponse(
                stream_generator(),
                media_type="text/event-stream",
                background=BackgroundTask(release_slot),
            )
        else:
            try:
                async with engine_client() as client:
                    engine_timeout = httpx.Timeout(ENGINE_READ_TIMEOUT_SECONDS, connect=ENGINE_CONNECT_TIMEOUT_SECONDS)
                    resp = await client.post(llama_url, json=body, headers=forwarded_headers, timeout=engine_timeout)
                    if resp.status_code == 200:
                        throughput.record(POOL_CODING, completion_tokens(resp.content))
                    return Response(
                        content=resp.content,
                        status_code=resp.status_code,
                        media_type="application/json",
                        background=BackgroundTask(release_slot),
                    )
            except Exception as e:
                log_error(f"Error proxying completion: {e}")
                raise HTTPException(status_code=500, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        log_error(f"Failed to initiate completion request: {type(e).__name__}: {e}")
        raise HTTPException(status_code=502, detail="Inference engine crashed or disconnected.")
    finally:
        if not slot_handed_off:
            await release_slot()


# ======================================================================================
# Cluster Telemetry & API Key Management
# ======================================================================================

# The coding pool's tiers come from the model ladder (engine/coding_ladder.py,
# scripts/coding_models.json). Advisory: nothing here swaps a model; recommended_tier and
# recommended_model say what the pool could hold.

def _get_recommended_tier(total_vram_gb: float) -> Optional[Dict[str, Any]]:
    """The biggest enabled tier the pool can hold; the ladder's first tier when it holds none."""
    tiers = _coding_ladder()
    return coding_ladder.tier_for_vram(tiers, total_vram_gb) or coding_ladder.default_tier(tiers)


def _measure_coding_speed(model_path: str, tail: bytearray) -> None:
    """
    Record a finished coding answer's decode rate (engine/coding_speed.py) from its last
    bytes; the speed file is written off the event loop. Never raises: a measurement must
    not be able to break an answer that has already been delivered.
    """
    try:
        if coding_speed.meter.observe(canonical_model_tag(model_path), bytes(tail[-coding_speed.TAIL_BYTES:])):
            spawn_background_task(asyncio.to_thread(coding_speed.meter.save), "coding-speed-save")
    except Exception as exc:  # noqa: BLE001
        log_warning(f"Could not record the coding model's speed ({type(exc).__name__}: {exc}).")


async def _coding_status(running_names: List[str], total_vram_gb: float) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    ``coding_model`` and ``coding_tiers`` for /v1/cluster/status (websites and apps render
    them as they are, so the shape is fixed):

        coding_model: {file, name, context, tokens_per_second, speed_measured_at}
        coding_tiers: [{id, name, min_vram_gb, enabled, unlocked, active}]

    The model is the running engine's, or the one the next request will start. ``context`` is
    the running engine's own (its snapshot's safe_context) and null while none runs; the
    speed is the measured median for that model file only, null until one is measured.
    """
    tiers = _coding_ladder()
    file = canonical_model_tag(running_names[0] if running_names else global_active_model) or None
    context = None
    if file and file in running_names:
        snapshot = await manager.asnapshot_model(file)
        value = (snapshot or {}).get("safe_context")
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            context = value
    tokens_per_second, measured_at = coding_speed.meter.speed(file)
    active = coding_ladder.tier_for_file(tiers, file)
    coding_model = {
        "file": file,
        "name": coding_ladder.display_name(file, tiers) or None,
        "context": context,
        "tokens_per_second": tokens_per_second,
        "speed_measured_at": measured_at,
    }
    coding_tiers = [
        {
            "id": tier["id"],
            "name": tier["name"],
            "min_vram_gb": tier["min_vram_gb"],
            "enabled": tier["enabled"],
            "unlocked": total_vram_gb >= tier["min_vram_gb"],
            "active": active is not None and tier["id"] == active["id"],
        }
        for tier in tiers
    ]
    return coding_model, coding_tiers


_NO_CODING_MODEL = {"file": None, "name": None, "context": None, "tokens_per_second": None, "speed_measured_at": None}


@app.get("/v1/cluster/status")
async def get_cluster_status(request: Request):
    """
    Returns real-time cluster health, active model, memory stats, and in-flight lease metrics.
    Used by VS Code extension status bar and IDE tooling.
    """
    total_vram = registry.get_total_vram_gb()
    total_sysram = registry.get_total_sysram_gb()
    active_endpoints = registry.get_active_rpc_endpoints()
    running_names = manager.get_active_model_names()
    active_model_name = running_names[0] if running_names else global_active_model
    rec = _get_recommended_tier(total_vram)
    loading_telemetry = await manager.aget_loading_telemetry(active_model_name)
    try:
        coding_model, coding_tiers = await _coding_status(running_names, total_vram)
    except Exception as exc:  # noqa: BLE001 - the status must answer even if this part cannot
        log_warning(f"Coding model status unavailable ({type(exc).__name__}: {exc}).")
        coding_model, coding_tiers = dict(_NO_CODING_MODEL), []

    return {
        "status": cluster_status,
        "global_active_model": active_model_name,
        "running_models": running_names,
        "recommended_tier": coding_ladder.tier_label(rec) if rec else None,
        "recommended_model": rec["file"] if rec else None,
        # The coding model as people should see it: its name, its real context and its
        # measured speed (engine/coding_speed.py), and the ladder it sits on.
        "coding_model": coding_model,
        "coding_tiers": coding_tiers,
        "total_vram_gb": round(total_vram, 2),
        "total_sysram_gb": round(total_sysram, 2),
        "total_memory_gb": round(total_vram + total_sysram, 2),
        "active_workers_count": len(active_endpoints),
        "active_endpoints": active_endpoints,
        "active_leases_count": lease_manager.active_count(),
        # The coding engine's slots and who is waiting for them, by priority (0 = GPU
        # contributors, 1 = CPU contributors, 2 = free trial). Counts only, no identities.
        "coding_queue": coding_gate.stats(),
        # The phone app's CPU pool: members, the unlocked model tier and the ladder. Counts
        # and model names only.
        "app_pool": app_pool.status(_app_members(), int(get_worker_limits().get("app_tier_min_members", 2))),
        # Coding members (engine/coding_router.py), counts only: {enabled, members, ready, slots, busy}.
        "coding_members": _coding_members_status(),
        # What the swarm lends and does right now, for the website's two tiles. Counts only, nothing names a
        # machine or a user: GPUs serving (and standing by without a job), the VRAM of the serving ones, the
        # coding tokens generated per second over the last minute and in the last hour; the same for the
        # phone pool's CPUs. Yielding nodes are in no count. See docs/coding-members.md section 5.
        "gpu_pool": _pool_status_or_empty(lambda: _gpu_pool_status(coding_model.get("tokens_per_second")),
                                          dict(_EMPTY_GPU_POOL), "GPU pool"),
        "cpu_pool": _pool_status_or_empty(_cpu_pool_status, dict(_EMPTY_CPU_POOL), "CPU pool"),
        # The phone model's lookups: which tools are on and today's counts. No key, no URL,
        # nothing about any network (engine/search.search_status).
        "app_search": _app_search_status(),
        "is_locked_for_edits": lease_manager.is_locked(),
        "in_flight_leases": lease_manager.active_leases_summary(),
        "is_swapping_models": is_swapping_models,
        "model_loading": loading_telemetry,
        "server_version": VERSION,
        # Automatic model switching (engine/auto_swap.py): {enabled, state, target, detail},
        # state one of off|steady|waiting|downloading|swapping|cooldown, detail a sentence
        # such as "Switching to X in 6 min if the pool keeps 24 GB."
        "auto_swap": auto_swapper.status(),
    }


@app.get("/v1/cluster/tiers")
async def get_cluster_tiers(request: Request):
    """
    The coding pool's model ladder with the pool's eligibility, smallest tier first. Each row
    keeps the fields this endpoint always had (min_vram_gb, tier, model, alt_model, unlocked,
    cached_on_disk; alt_model is null now, the ladder names one model per tier) and adds the
    ladder's own (id, name, context, enabled, blocked, size, parts).
    """
    total_vram = registry.get_total_vram_gb()
    rec = _get_recommended_tier(total_vram)
    tiers_out = []
    for t in _coding_ladder():
        cached = True
        for part in t["files"]:
            resolved = await manager.aresolve_model_path(part["file"])
            cached = cached and bool(resolved and os.path.exists(resolved))
        tiers_out.append({
            "id": t["id"],
            "name": t["name"],
            "min_vram_gb": t["min_vram_gb"],
            "tier": coding_ladder.tier_label(t),
            "model": t["file"],
            "alt_model": None,
            "context": t["context"],
            "enabled": t["enabled"],
            "blocked": t["blocked"],
            "size": t["size"],
            "parts": len(t["files"]),
            "unlocked": total_vram >= t["min_vram_gb"],
            "cached_on_disk": cached,
        })
    return {
        "total_vram_gb": round(total_vram, 2),
        "recommended_tier": coding_ladder.tier_label(rec) if rec else None,
        "recommended_model": rec["file"] if rec else None,
        "tiers": tiers_out,
    }


class SwapModelPayload(BaseModel):
    model: str
    model_config = {"protected_namespaces": ()}


@app.post("/v1/cluster/swap", dependencies=[Depends(check_contrib_clause)])
async def swap_cluster_model(payload: SwapModelPayload, request: Request):
    """
    Hot-swap the cluster's active model. Stops currently running models (after draining
    active leases) and boots the requested model. Operator only (this machine, an API key, or
    the admin key): it changes the model for everyone, and until 2.3.6 a free-trial caller
    could do it.

    The switch itself is :func:`_swap_model_core`, which automatic switching uses too.
    """
    if not _may_choose_model(request):
        _require_admin(request)

    target_model = canonical_model_tag(payload.model or "")
    if not target_model:
        raise HTTPException(status_code=400, detail="Model name is required.")

    resolved = await manager.aresolve_model_path(target_model)
    if not resolved or not os.path.exists(resolved):
        raise HTTPException(status_code=404, detail=f"Model '{target_model}' is not cached on the coordinator.")

    req_gb = await manager.aestimate_model_vram_gb(target_model)
    total_vram = registry.get_total_vram_gb()
    total_sysram = registry.get_total_sysram_gb()
    if req_gb > (total_vram + total_sysram):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Model '{target_model}' requires ~{req_gb:.1f} GB, but cluster currently has "
                f"{total_vram + total_sysram:.1f} GB total ({total_vram:.1f} GB VRAM + {total_sysram:.1f} GB RAM)."
            ),
        )

    swap = await _swap_model_core(target_model, req_gb, total_vram, total_sysram)
    # Automatic switching holds off for its cooldown after a manual switch, so it never
    # undoes what somebody just chose (engine/auto_swap.py).
    auto_swapper.note_manual_swap(target_model)
    return {
        "status": "swapped" if swap["success"] else "queued",
        "active_model": global_active_model,
        "detail": swap["detail"],
        "required_gb": round(req_gb, 2),
        "total_vram_gb": round(total_vram, 2),
    }


# ======================================================================================
# Model switching, manual and automatic
# ======================================================================================

# Bumped by every switch. A switch that finds it changed under it knows another switch took
# over, and then leaves the cluster to that one: it neither rolls back nor clears
# ``is_swapping_models`` (which the newer switch owns).
_model_swap_generation = 0

ENGINE_READY_POLL_SECONDS = 1.0


def _auto_swap_load_timeout() -> float:
    """
    How long an automatic switch holds requests while the new engine loads, before it gives up
    and restores the previous model (AUTO_SWAP_LOAD_TIMEOUT_SECONDS, 3 min by default). Only
    near GPUs unlock a tier, so a load that takes longer than this is a load gone wrong.
    """
    return float(get_worker_limits().get("auto_swap_load_timeout_seconds", 180))


async def _swap_model_core(
    target_model: str,
    req_gb: float,
    total_vram: float,
    total_sysram: float,
    *,
    drain_timeout: float = 15.0,
    hold_first: bool = False,
    abort_if_busy: bool = False,
    ready_timeout: float = 0.0,
    rollback_model: Optional[str] = None,
    endpoints: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Stop every engine and start ``target_model``: the one code path every model switch takes.

    With the defaults this is exactly what ``POST /v1/cluster/swap`` has always done: wait up
    to 15 s for in-flight leases, then raise ``is_swapping_models`` (which parks new chat and
    completion requests), stop, start, lower the flag, and tell the WebUIs.

    Automatic switching (engine/auto_swap.py) passes the options, which make it safer for a
    switch nobody asked for:

    * ``hold_first``: claim the cluster first (refusing if any other switch or engine restart
      is running), so no new request starts while in-flight ones drain. With
      ``abort_if_busy`` a queue that does not drain within ``drain_timeout`` calls the switch
      off before anything is touched.
    * ``ready_timeout``: keep the flag raised until the new engine answers ``/health``, so
      requests wait for a model that works rather than queueing on one that is still loading.
    * ``rollback_model``: if the new engine does not start, dies while loading, or is not
      ready in time, start this one again.
    * ``endpoints``: the RPC endpoints to start it on, already checked by the caller
      (default: the shard planner's choice, as always).

    Returns ``{"outcome", "success", "detail"}``, outcome being ``swapped``, ``failed``,
    ``busy`` (nothing touched) or ``superseded`` (another switch took over).
    """
    global global_active_model, is_swapping_models, cluster_status, _model_swap_generation

    if hold_first:
        if is_swapping_models or is_recalculating_queue or reconfiguration_in_flight():
            return {"outcome": "busy", "success": False,
                    "detail": "another model switch or engine restart is in progress"}
        is_swapping_models = True
        _model_swap_generation += 1
        generation = _model_swap_generation
        drained = True
        try:
            if lease_manager.is_locked():
                drained = await lease_manager.wait_for_idle(timeout=drain_timeout)
        except BaseException:
            if generation == _model_swap_generation:
                is_swapping_models = False
            raise
        if generation != _model_swap_generation:
            return {"outcome": "superseded", "success": False, "detail": "another model switch took over"}
        if not drained and abort_if_busy:
            is_swapping_models = False
            return {"outcome": "busy", "success": False,
                    "detail": f"coding requests were still running after {drain_timeout:.0f} s"}
    else:
        if lease_manager.is_locked():
            await lease_manager.wait_for_idle(timeout=drain_timeout)
        is_swapping_models = True
        _model_swap_generation += 1
        generation = _model_swap_generation

    success = False
    status_msg = ""
    outcome = "failed"
    try:
        global_active_model = target_model
        cluster_status = f"Swapping to {target_model}..."
        await manager.astop_all_models()
        active_endpoints = endpoints if endpoints is not None else _planned_endpoints(req_gb)
        success, assigned_port, status_msg = await manager.astart_model(
            target_model, active_endpoints, total_vram, total_sysram, queue_channels=2
        )
        if success:
            cluster_status = "Active"
            spawn_vram_progress_task(assigned_port, target_model)
        else:
            cluster_status = f"Paused ({status_msg})"

        if success and ready_timeout > 0:
            success, why = await _wait_for_engine_ready(target_model, ready_timeout, generation)
            if not success:
                status_msg = why

        if generation != _model_swap_generation:
            outcome = "superseded"
        elif success:
            outcome = "swapped"
        elif rollback_model and canonical_model_tag(rollback_model) != target_model:
            await _restore_model(canonical_model_tag(rollback_model), target_model, status_msg,
                                 ready_timeout or _auto_swap_load_timeout(), generation)
    finally:
        # Only the newest switch lowers the flag: an older one finishing late must not let
        # requests through while the newer one is still stopping and starting engines.
        if generation == _model_swap_generation:
            is_swapping_models = False

    if not (hold_first and outcome == "superseded"):
        await broadcast({"type": "swap_complete", "model": global_active_model})
    return {"outcome": outcome, "success": success, "detail": status_msg}


async def _engine_health_ok(port: int) -> bool:
    """True when the llama-server on ``port`` answers ``/health`` with 200 (weights loaded)."""
    if port <= 0:
        return False
    try:
        async with engine_client() as client:
            response = await client.get(f"http://127.0.0.1:{port}/health", timeout=2.0)
        return response.status_code == 200
    except Exception:  # noqa: BLE001 - not listening yet, or still loading
        return False


async def _wait_for_engine_ready(model_path: str, timeout: float, generation: int) -> Tuple[bool, str]:
    """
    Wait until ``model_path``'s engine has loaded and answers, it dies, or ``timeout`` passes.

    Returns ``(ready, why_not)``. Gives up early when another switch takes over.
    """
    deadline = time.monotonic() + timeout
    while True:
        if generation != _model_swap_generation:
            return False, "another model switch took over"
        snapshot = await manager.asnapshot_model(model_path)
        if snapshot is None:
            return False, "the engine stopped while loading"
        process = snapshot.get("process")
        if process is not None and process.poll() is not None:
            return False, f"the engine exited while loading (exit code {process.returncode})"
        if await _engine_health_ok(int(snapshot.get("port") or 0)):
            return True, ""
        if time.monotonic() >= deadline:
            return False, f"the engine was not ready after {timeout:.0f} s"
        await asyncio.sleep(ENGINE_READY_POLL_SECONDS)


async def _restore_model(model_path: str, failed_model: str, why: str,
                         ready_timeout: float, generation: int) -> None:
    """
    Put the model that was being served before a failed automatic switch back.

    Called with ``is_swapping_models`` still raised. If even this fails, the cluster is left
    paused and the monitor loop takes over once the flag drops, exactly as it would after
    any engine crash.
    """
    global global_active_model, cluster_status

    log_warning(
        f"Auto-swap: {safe_log_text(failed_model)} did not come up ({safe_log_text(why)}); "
        f"going back to {safe_log_text(model_path)}."
    )
    global_active_model = model_path
    cluster_status = f"Restoring {model_path}..."
    await manager.astop_all_models()
    req_gb = await manager.aestimate_model_vram_gb(model_path)
    ok, port, msg = await manager.astart_model(
        model_path, _planned_endpoints(req_gb), registry.get_total_vram_gb(),
        registry.get_total_sysram_gb(), queue_channels=2
    )
    if ok:
        spawn_vram_progress_task(port, model_path)
        ok, msg = await _wait_for_engine_ready(model_path, ready_timeout, generation)
    if generation != _model_swap_generation:
        return  # a manual switch took over; the cluster's state is its to report
    if ok:
        cluster_status = "Active"
        log_coordinator(f"Auto-swap: {safe_log_text(model_path)} is being served again.")
    else:
        cluster_status = f"Paused ({safe_log_text(msg, max_chars=48)})"
        log_error(f"Auto-swap: could not restore {safe_log_text(model_path)} either ({safe_log_text(msg)}).")


def _coding_tiers_for_swap() -> List[Dict[str, Any]]:
    """
    The coding model ladder automatic switching works from: the same tiers /v1/cluster/status
    shows (engine/coding_ladder.load_coding_tiers, read once per process), smallest first.
    """
    return _coding_ladder()


def _coding_gpu_workers() -> List[Dict[str, Any]]:
    """
    The GPU workers the coding pool can use, the same ones registry.get_total_vram_gb() adds
    up, each as ``{"worker_id", "endpoint", "vram_gb", "class"}``. ``class`` is
    engine/benchmark.classify_endpoint's: "local" is this machine.
    """
    registry.prune_dead_workers()
    workers = []
    for worker_id, data in registry.snapshot().items():
        hardware = data.get("hardware")
        if not isinstance(hardware, dict) or hardware.get("is_yielding", False):
            continue
        if not registry.rpc_compatible(hardware):
            continue
        gpu = gpu_section(hardware)
        if not gpu.get("has_gpu", False):
            continue
        endpoint = f"{data.get('ip', '')}:{data.get('port', '')}"
        workers.append({
            "worker_id": worker_id,
            "endpoint": endpoint,
            "vram_gb": _as_number(gpu.get("vram_total_gb", 0.0)),
            "class": benchmark.classify_endpoint(endpoint),
        })
    return workers


def _coding_worker_is_near(worker: Dict[str, Any], max_rtt_ms: float) -> bool:
    """
    This machine's own GPUs, or a volunteer whose last measured round trip (its own link
    report, or the coordinator's probe below) is fresh and at most ``max_rtt_ms``. A
    volunteer nobody has measured is far.
    """
    if worker.get("class") == "local":
        return True
    measurement = registry.get_link_measurement(worker.get("worker_id", ""))
    reported_at = measurement.get("reported_at")
    age = time.time() - reported_at if isinstance(reported_at, (int, float)) else None
    return rtt_is_near(measurement.get("rtt_min_ms"), age, max_rtt_ms)


def _coding_pool() -> Dict[str, float]:
    """
    The pool automatic switching sizes the coding model for: ``vram_gb`` counts only near
    GPUs (AUTO_SWAP_MAX_RTT_MS), ``far_vram_gb`` the rest, which is reported but never used.
    """
    max_rtt = float(get_worker_limits().get("auto_swap_max_rtt_ms", 15.0))
    pool = split_pool_by_distance(_coding_gpu_workers(), lambda w: _coding_worker_is_near(w, max_rtt))
    pool["sysram_gb"] = registry.get_total_sysram_gb()
    return pool


def _probe_rpc_rtt(endpoint: str) -> Optional[float]:
    """
    Round trip to a volunteer's RPC port, timed as TCP connects (engine/benchmark.measure_rtt_ms),
    or None when it cannot be reached directly. A volunteer behind NAT, reached only through its
    reverse tunnel, is never reached directly and so stays far: its weights and tokens would
    cross the internet. Blocking; run on a thread.
    """
    host, _, port = endpoint.rpartition(":")
    try:
        result = benchmark.measure_rtt_ms(host, int(port), samples=3, timeout=1.0)
    except (ValueError, OSError):
        return None
    return result.get("rtt_min_ms") if result.get("ok") else None


def _record_probed_rtt(endpoint: str, rtt_ms: float) -> None:
    """
    File a probe result as the worker's link measurement, next to anything it reported about
    itself. The shard planner already orders volunteers by this figure, so the engine is put
    on the near GPUs first, the same ones that unlocked the tier.
    """
    for worker in _coding_gpu_workers():
        if worker["endpoint"] == endpoint:
            measurement = registry.get_link_measurement(worker["worker_id"])
            measurement["rtt_min_ms"] = rtt_ms
            registry.record_link_measurement(worker["worker_id"], measurement)


def _probe_targets() -> List[str]:
    """Every volunteer GPU endpoint (this machine's own needs no measuring)."""
    return [w["endpoint"] for w in _coding_gpu_workers() if w["class"] != "local"]


link_probe = LinkProbe(measure=_probe_rpc_rtt, record=_record_probed_rtt)


def _coding_reconfiguration_running() -> bool:
    """Any switch or engine restart under way: auto-swap waits for it to finish."""
    return is_swapping_models or is_recalculating_queue or reconfiguration_in_flight()


def _coding_queue_busy() -> bool:
    """Any coding request in flight (every chat and completion holds a lease while it runs)."""
    return lease_manager.is_locked() or active_inference_requests > 0


# (path, size, mtime) of files that failed their checksum, so a multi-GB file is not hashed
# again every tick until it changes.
_coding_files_refused: Set[Tuple[str, int, int]] = set()


def _coding_tier_on_disk(tier: Dict[str, Any]) -> bool:
    """
    Every file of ``tier`` is where the engine will look for it, with the pinned size and
    SHA-256. Blocking (the first check of a file hashes it; a marker beside it remembers the
    result, the same one the CPU pool uses).
    """
    from engine.app_pool import sha256_ok

    files = tier_files(tier)
    if not files or not all(file_is_pinned(entry) for entry in files):
        return False
    for entry in files:
        path = manager.resolve_model_path(entry["file"])
        if not path:
            return False
        try:
            stat = os.stat(path)
        except OSError:
            return False
        stamp = (path, stat.st_size, int(stat.st_mtime))
        if stamp in _coding_files_refused:
            return False
        if not sha256_ok(path, entry["sha256"], int(entry["size"])):
            _coding_files_refused.add(stamp)
            log_warning(f"Auto-swap: {safe_log_text(path)} does not match the coding ladder's size and "
                        "SHA-256, so it will not be switched to.")
            return False
    return True


async def _coding_tier_ready(tier: Dict[str, Any]) -> bool:
    return await asyncio.to_thread(_coding_tier_on_disk, tier)


# Free space a download must leave on the models drive, as the model request path requires.
CODING_DOWNLOAD_FREE_BUFFER_GB = 20.0


async def _download_split_coding_tier(tier: Dict[str, Any], files: List[Dict[str, Any]]) -> bool:
    """
    Fetch every part of a split model (engine/downloader.py ``download_verified``): resumable,
    each part's size and SHA-256 checked while it streams in, renamed into place only when it
    is whole and right, and one caller per part. The same free-space rule as a single file.
    """
    models_dir = manager._model_search_roots()[0]
    total = sum(int(entry["size"]) for entry in files)
    margin = int(total * 0.1 + CODING_DOWNLOAD_FREE_BUFFER_GB * (1024 ** 3))
    cancel = threading.Event()

    def progress(update: Dict[str, Any]) -> None:
        auto_swapper.note_download_progress(str(tier.get("id") or ""), update["done"], update["total"])

    try:
        # The download is blocking and runs on a thread; a cancelled task (auto-swap switched
        # off, coordinator shutdown) stops it at its next block and keeps the partial part.
        return await asyncio.to_thread(download_verified, files, models_dir, progress,
                                       cancel=cancel, free_margin=margin)
    except asyncio.CancelledError:
        cancel.set()
        raise


async def _download_coding_tier(tier: Dict[str, Any]) -> bool:
    """
    Fetch a tier's model file(s) into the models folder and verify them.

    A single file is downloaded under a temporary name and only renamed into place once its
    size and SHA-256 match; a split model goes through :func:`_download_split_coding_tier`
    (every part the same way). Either way the engine can never be pointed at a partial or
    wrong file.
    """
    from engine.app_pool import sha256_ok

    if not downloadable(tier):
        return False
    files = tier_files(tier)
    if len(files) > 1:
        return await _download_split_coding_tier(tier, files)
    entry = files[0]
    name, size, label = entry["file"], int(entry["size"]), str(tier.get("name") or entry["file"])
    models_dir = manager._model_search_roots()[0]
    dest = os.path.join(models_dir, name)
    staging = dest + ".dlne-download"
    try:
        os.makedirs(models_dir, exist_ok=True)
        free = shutil.disk_usage(models_dir).free
    except OSError as exc:
        log_error(f"Auto-swap: cannot use {safe_log_text(models_dir)} for {safe_log_text(label)}: {exc}")
        return False
    need = size * 1.1 + CODING_DOWNLOAD_FREE_BUFFER_GB * (1024 ** 3)
    if free < need:
        log_error(f"Auto-swap: not downloading {safe_log_text(label)}: it needs {size / 1e9:.1f} GB plus a "
                  f"{CODING_DOWNLOAD_FREE_BUFFER_GB:.0f} GB buffer, and {free / 1e9:.1f} GB is free.")
        return False

    if not await download_file_multistream_async(entry["url"], staging, label):
        return False
    try:
        if not await asyncio.to_thread(sha256_ok, staging, entry["sha256"], size):
            log_error(f"Auto-swap: the download of {safe_log_text(label)} does not match its pinned size "
                      "and SHA-256; deleted.")
            for leftover in (staging, staging + ".dlne-sha256"):
                if os.path.exists(leftover):
                    os.remove(leftover)
            return False
        os.replace(staging, dest)
        # The verification marker names size and mtime, not the path, and a rename keeps both.
        if os.path.exists(staging + ".dlne-sha256"):
            os.replace(staging + ".dlne-sha256", dest + ".dlne-sha256")
    except OSError as exc:
        log_error(f"Auto-swap: could not put {safe_log_text(label)} in place: {exc}")
        return False
    return True


async def _auto_swap_to(tier: Dict[str, Any], *, drain_seconds: float, abort_if_busy: bool) -> SwapResult:
    """
    Switch the coding pool to ``tier`` for engine/auto_swap.py, through the same
    :func:`_swap_model_core` the manual endpoint uses, with the safety options on.
    """
    from engine.inference_manager import MIN_KV_GB, POOL_SAFETY_FRACTION

    target = canonical_model_tag(tier.get("file") or "")
    if not target:
        return SwapResult("refused", "the tier names no model file")
    if not await manager.aresolve_model_path(target):
        return SwapResult("refused", f"{target} is not on disk")

    # The engine's own admission rule, applied before anything is stopped: a model the
    # inference manager would refuse must not cost users the model they have now.
    req_gb = await manager.aestimate_model_vram_gb(target)
    total_vram = registry.get_total_vram_gb()
    total_sysram = registry.get_total_sysram_gb()
    room = (total_vram + total_sysram) * POOL_SAFETY_FRACTION
    if req_gb + MIN_KV_GB > room:
        return SwapResult("refused", f"{target} needs about {req_gb:.1f} GB and the pool can give {room:.1f} GB")

    # The same near-only rule that unlocked the tier, applied to where the engine will really
    # run: if the shard planner would spread it onto a GPU that is too far away, do not switch.
    planned = _planned_endpoints(req_gb)
    max_rtt = float(get_worker_limits().get("auto_swap_max_rtt_ms", 15.0))
    near = {w["endpoint"] for w in _coding_gpu_workers() if _coding_worker_is_near(w, max_rtt)}
    far = [endpoint for endpoint in planned
           if endpoint not in near and benchmark.classify_endpoint(endpoint) != "local"]
    if far:
        return SwapResult("refused", f"{target} would be spread onto {len(far)} GPU(s) too far away "
                                     f"(over {max_rtt:g} ms or never measured)")

    swap = await _swap_model_core(
        target, req_gb, total_vram, total_sysram,
        drain_timeout=drain_seconds,
        hold_first=True,
        abort_if_busy=abort_if_busy,
        ready_timeout=_auto_swap_load_timeout(),
        rollback_model=global_active_model,
        endpoints=planned,
    )
    return SwapResult(swap["outcome"], swap["detail"])


auto_swapper = AutoSwapController(
    tiers_provider=_coding_tiers_for_swap,
    pool_provider=_coding_pool,
    current_model=lambda: global_active_model,
    swap=_auto_swap_to,
    tier_ready=_coding_tier_ready,
    download=_download_coding_tier,
    settings=get_worker_limits,
    swap_in_progress=_coding_reconfiguration_running,
    queue_busy=_coding_queue_busy,
    log=lambda message: log_coordinator(safe_log_text(message, max_chars=500)),
    warn=lambda message: log_warning(safe_log_text(message, max_chars=500)),
)

# The controller's loop, started with the other monitors and cancelled on shutdown.
_auto_swap_task: Optional[asyncio.Task] = None
_auto_swap_tasks: List[asyncio.Task] = []


class CreateApiKeyPayload(BaseModel):
    name: str = "vscode-client"
    model_config = {"protected_namespaces": ()}


@app.post("/v1/admin/apikeys")
async def create_api_key(payload: CreateApiKeyPayload, request: Request):
    """Create a new Bearer API Key for VS Code extension or external clients."""
    _require_admin(request)

    raw_token, meta = api_key_manager.generate_key(payload.name)
    return {"api_key": raw_token, "key_info": meta}


@app.get("/v1/admin/apikeys")
async def list_api_keys(request: Request):
    """List all registered API keys."""
    _require_admin(request)

    return {"keys": api_key_manager.list_keys()}


@app.delete("/v1/admin/apikeys/{key_id}")
async def revoke_api_key(key_id: str, request: Request):
    """Revoke an API key."""
    _require_admin(request)

    success = api_key_manager.revoke_key(key_id)
    if not success:
        raise HTTPException(status_code=404, detail="Key ID not found or already revoked.")
    return {"status": "revoked", "id": key_id}


# ======================================================================================
# LAN discovery
# ======================================================================================

def udp_broadcaster(api_port: int):
    """
    Continuously broadcasts the Coordinator's presence on the LAN.

    THE FORMAT IS NOT WRITTEN HERE, DELIBERATELY.
        Beacons are signed with the same NETWORK_KEY as worker heartbeats, so they carry
        their own domain-separation tag (``DLNE-DISC|v1|``) to keep a signature captured
        from one protocol from ever being a valid message in the other — heartbeats tag
        theirs ``DLNE-HB|v2|``.

        This function used to assemble that string and compute its HMAC inline, which made
        it a *second* implementation of a format whose only other implementation —
        producer and consumer both — lives in ``engine/discovery.py``. Two
        components speak this protocol (this sender and ``setup_core.discover_coordinator``,
        which the redundancy node's promotion-abort path also uses), and they are edited by
        different people at different times. Two
        copies of a signed wire format drift, and when this one drifts the failure is
        silent: beacons keep being sent, every receiver quietly discards them, and LAN
        discovery simply stops working with nothing logged anywhere.

        So the canonical builder is called instead. It is the same principle already
        applied to the heartbeat MAC in ``engine/authn.py``: exactly one function
        canonicalises, and both sides reach it.

    The beacon format lives in :mod:`engine.discovery`, the one module that both builds and
    parses it.
    """
    from engine.discovery import DISCOVERY_PORT, build_discovery_beacon

    time.sleep(3)  # Wait for Uvicorn to bind the port fully to prevent connection race conditions
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    while True:
        try:
            # Re-read every cycle (the config view is cached on the file's stat) so an
            # operator who sets NETWORK_KEY starts broadcasting signed beacons without a
            # restart — matching what the listener does on the receiving side.
            network_key = get_worker_limits().get("network_key", "")
            message = build_discovery_beacon(api_port, network_key).encode()

            # DISCOVERY_PORT, not a literal: the listeners bind this constant, and a
            # hardcoded number here is the other half of the same drift problem.
            sock.sendto(message, ('<broadcast>', DISCOVERY_PORT))
        except Exception:
            pass
        time.sleep(2)


# Live VRAM-progress readers, keyed by (model_name, port). One per engine is all that is
# ever useful — they all publish the same "vram_progress" broadcast for the same model — and
# without this table the coordinator spawned a fresh one on *every* chat request, including
# the (very common) case where astart_model returned success because the model was already
# running. Since bug #27's fix keeps strong references to background tasks, both the task set
# and the open connections to llama-server grew without bound for the life of the process.
_vram_progress_streams: Dict[tuple, asyncio.Task] = {}

# Hard ceiling on how long one progress reader may stay attached. llama-server's /models/sse
# feed has no read timeout of its own and a model that never finishes loading never sends the
# terminal event, so without this the reader would hold its connection open forever. Five
# minutes comfortably exceeds the 120 s the chat path is willing to wait for an engine.
VRAM_PROGRESS_TIMEOUT_SECONDS = 300.0


def _forget_vram_progress_task(key: tuple, task: asyncio.Task) -> None:
    """Drops a finished progress reader from the table, unless it has already been replaced."""
    if _vram_progress_streams.get(key) is task:
        _vram_progress_streams.pop(key, None)


def spawn_vram_progress_task(port: int, model_name: str) -> None:
    """
    Start at most one VRAM-progress reader per (model, port).

    A call for a pair that already has a live reader is a no-op, which is what turns an
    unbounded per-request spawn into a bounded per-engine one.
    """
    key = (model_name, int(port))

    existing = _vram_progress_streams.get(key)
    if existing is not None and not existing.done():
        return

    task = spawn_background_task(
        stream_vram_progress_async(port, model_name),
        f"vram-progress:{model_name}",
    )
    _vram_progress_streams[key] = task
    task.add_done_callback(lambda finished, k=key: _forget_vram_progress_task(k, finished))


async def stream_vram_progress_async(port: int, model_name: str):
    """
    Connects to the llama-server /models/sse feed to stream real-time VRAM loading progress
    to the WebUI, then disconnects.

    Bounded by :data:`VRAM_PROGRESS_TIMEOUT_SECONDS` overall. Use
    :func:`spawn_vram_progress_task` rather than spawning this directly, so that a model
    that is already running does not accumulate readers.
    """
    try:
        # asyncio.timeout covers the whole conversation — connect, headers and every line of
        # the feed. httpx's own timeout is deliberately None for reads (the feed is meant to
        # be long-lived), so this is the only thing that can end a stalled reader.
        async with asyncio.timeout(VRAM_PROGRESS_TIMEOUT_SECONDS):
            await _consume_vram_progress_stream(port, model_name)
    except (asyncio.TimeoutError, TimeoutError):
        _debug_logger.debug(
            f"VRAM progress reader for {model_name} on port {port} timed out after "
            f"{VRAM_PROGRESS_TIMEOUT_SECONDS:.0f}s and was closed."
        )
    except asyncio.CancelledError:
        # Shutdown, or the task being replaced. Nothing to report, but it must not be
        # swallowed by the catch-all below.
        raise
    except Exception:
        pass  # Stream closed or failed to connect


async def _consume_vram_progress_stream(port: int, model_name: str) -> None:
    """Reads the SSE feed and broadcasts progress until it reports 100% or ends."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=5.0)) as client:
            async with client.stream("GET", f"http://127.0.0.1:{port}/models/sse") as response:
                if response.status_code != 200:
                    return

                def find_progress(d):
                    if isinstance(d, dict):
                        # Try exact match keys
                        if "progress" in d:
                            return float(d["progress"])
                        if "loaded" in d and "total" in d and float(d["total"]) > 0:
                            return float(d["loaded"]) / float(d["total"])
                        # Recursive hunt
                        for v in d.values():
                            res = find_progress(v)
                            if res is not None:
                                return res
                    return None

                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        try:
                            payload_str = line[5:].strip()
                            if not payload_str:
                                continue
                            payload = json.loads(payload_str)

                            progress = find_progress(payload)
                            if progress is not None:
                                percent = min(100.0, progress * 100 if progress <= 1.0 else progress)
                                await broadcast({"type": "vram_progress", "model": model_name, "percent": percent})
                                if percent >= 100.0:
                                    break
                            elif payload.get("status") == "ok":
                                await broadcast({"type": "vram_progress", "model": model_name, "percent": 100.0})
                                break
                        except Exception:
                            pass
    except Exception:
        # Stream closed, engine not listening yet, or malformed feed. Progress reporting is
        # cosmetic, so none of these are worth surfacing. CancelledError is a BaseException
        # and deliberately not caught here, so the timeout wrapper above still fires.
        pass


# ======================================================================================
# Model library
# ======================================================================================

DEFAULT_MODEL_LIBRARY = [
    { "id": "qwen2.5-coder:0.5b", "name": "Qwen 2.5 Coder (0.5B)", "family": "Qwen", "vram": 0.5, "modalities": ["text"], "max_context": 32768, "hf_url": "https://huggingface.co/Qwen/Qwen2.5-Coder-0.5B-Instruct-GGUF/resolve/main/qwen2.5-coder-0.5b-instruct-q4_k_m.gguf" },
    { "id": "llama3.2:1b", "name": "Llama 3.2 (1B)", "family": "Llama", "vram": 0.8, "modalities": ["text"], "max_context": 131072, "hf_url": "https://huggingface.co/bartowski/Llama-3.2-1B-Instruct-GGUF/resolve/main/Llama-3.2-1B-Instruct-Q4_K_M.gguf" },
    { "id": "qwen2.5:1.5b", "name": "Qwen 2.5 (1.5B)", "family": "Qwen", "vram": 1.0, "modalities": ["text"], "max_context": 131072, "hf_url": "https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf" },
    { "id": "gemma2:2b", "name": "Gemma 2 (2B)", "family": "Gemma", "vram": 1.5, "modalities": ["text"], "max_context": 8192, "hf_url": "https://huggingface.co/bartowski/gemma-2-2b-it-GGUF/resolve/main/gemma-2-2b-it-Q4_K_M.gguf" },
    { "id": "phi3:mini", "name": "Phi-3 Mini (3.8B)", "family": "Phi", "vram": 2.3, "modalities": ["text"], "max_context": 131072, "hf_url": "https://huggingface.co/microsoft/Phi-3-mini-4k-instruct-gguf/resolve/main/Phi-3-mini-4k-instruct-q4.gguf" },
    { "id": "qwen3:4b", "name": "Qwen 3 (4B)", "family": "Qwen", "vram": 2.5, "modalities": ["text"], "max_context": 131072, "hf_url": "https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/qwen2.5-3b-instruct-q4_k_m.gguf" },
    { "id": "gemma3:4b", "name": "Gemma 3 (4B)", "family": "Gemma", "vram": 2.5, "modalities": ["text"], "max_context": 8192, "hf_url": "https://huggingface.co/bartowski/gemma-2-9b-it-GGUF/resolve/main/gemma-2-9b-it-Q4_K_M.gguf" },
    { "id": "llama3.2:3b", "name": "Llama 3.2 (3B)", "family": "Llama", "vram": 2.0, "modalities": ["text"], "max_context": 131072, "hf_url": "https://huggingface.co/bartowski/Llama-3.2-3B-Instruct-GGUF/resolve/main/Llama-3.2-3B-Instruct-Q4_K_M.gguf" },
    { "id": "phi4:mini", "name": "Phi-4 Mini (3.8B)", "family": "Phi", "vram": 2.3, "modalities": ["text"], "max_context": 131072, "hf_url": "https://huggingface.co/bartowski/Phi-3.5-mini-instruct-GGUF/resolve/main/Phi-3.5-mini-instruct-Q4_K_M.gguf" },
    { "id": "llama3.1:8b", "name": "Llama 3.1 (8B)", "family": "Llama", "vram": 4.7, "modalities": ["text"], "max_context": 131072, "hf_url": "https://huggingface.co/lmstudio-community/Meta-Llama-3.1-8B-Instruct-GGUF/resolve/main/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf", "dflash_url": "https://huggingface.co/bartowski/Llama-3.2-1B-Instruct-GGUF/resolve/main/Llama-3.2-1B-Instruct-Q4_K_M.gguf" },
    { "id": "mistral:7b", "name": "Mistral (7B)", "family": "Mistral", "vram": 4.1, "modalities": ["text"], "max_context": 32768, "hf_url": "https://huggingface.co/TheBloke/Mistral-7B-Instruct-v0.2-GGUF/resolve/main/mistral-7b-instruct-v0.2.Q4_K_M.gguf" },
    { "id": "gemma2:9b", "name": "Gemma 2 (9B)", "family": "Gemma", "vram": 5.5, "modalities": ["text"], "max_context": 8192, "hf_url": "https://huggingface.co/bartowski/gemma-2-9b-it-GGUF/resolve/main/gemma-2-9b-it-Q4_K_M.gguf", "dflash_url": "https://huggingface.co/bartowski/gemma-2-2b-it-GGUF/resolve/main/gemma-2-2b-it-Q4_K_M.gguf" },
    { "id": "qwen2.5:14b", "name": "Qwen 2.5 (14B)", "family": "Qwen", "vram": 8.5, "modalities": ["text"], "max_context": 131072, "hf_url": "https://huggingface.co/bartowski/Qwen2.5-14B-Instruct-GGUF/resolve/main/Qwen2.5-14B-Instruct-Q4_K_M.gguf", "dflash_url": "https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf" },
    { "id": "phi4:14b", "name": "Phi-4 (14B)", "family": "Phi", "vram": 8.5, "modalities": ["text"], "max_context": 131072, "hf_url": "https://huggingface.co/bartowski/phi-4-GGUF/resolve/main/phi-4-Q4_K_M.gguf" },
    { "id": "mixtral:8x7b", "name": "Mixtral (8x7B MoE)", "family": "Mistral", "vram": 26.0, "modalities": ["text"], "max_context": 32768, "hf_url": "https://huggingface.co/TheBloke/Mixtral-8x7B-Instruct-v0.1-GGUF/resolve/main/mixtral-8x7b-instruct-v0.1.Q4_K_M.gguf" },
    { "id": "qwen2.5:32b", "name": "Qwen 2.5 (32B)", "family": "Qwen", "vram": 19.5, "modalities": ["text"], "max_context": 131072, "hf_url": "https://huggingface.co/bartowski/Qwen2.5-32B-Instruct-GGUF/resolve/main/Qwen2.5-32B-Instruct-Q4_K_M.gguf", "dflash_url": "https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf" },
    { "id": "llama3:70b", "name": "Llama 3 (70B)", "family": "Llama", "vram": 40.0, "modalities": ["text"], "max_context": 8192, "hf_url": "https://huggingface.co/lmstudio-community/Meta-Llama-3.1-70B-Instruct-GGUF/resolve/main/Meta-Llama-3.1-70B-Instruct-Q4_K_M.gguf", "dflash_url": "https://huggingface.co/bartowski/Llama-3.2-3B-Instruct-GGUF/resolve/main/Llama-3.2-3B-Instruct-Q4_K_M.gguf" },
    { "id": "qwen2.5:72b", "name": "Qwen 2.5 (72B)", "family": "Qwen", "vram": 41.0, "modalities": ["text"], "max_context": 131072, "hf_url": "https://huggingface.co/bartowski/Qwen2.5-72B-Instruct-GGUF/resolve/main/Qwen2.5-72B-Instruct-Q4_K_M.gguf", "dflash_url": "https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf" },
    { "id": "command-r-plus:104b", "name": "Command R+ (104B)", "family": "Cohere", "vram": 58.0, "modalities": ["text"], "max_context": 131072, "hf_url": "https://huggingface.co/pmysl/c4ai-command-r-plus-GGUF/resolve/main/command-r-plus-Q3_K_S.gguf" },
    { "id": "gemma4:12b", "name": "Gemma 4 (12B)", "family": "Gemma", "vram": 7.6, "modalities": ["text", "image"], "max_context": 262144, "hf_url": "https://huggingface.co/bartowski/gemma-2-27b-it-GGUF/resolve/main/gemma-2-27b-it-Q4_K_M.gguf" },
    { "id": "gemma4:26b", "name": "Gemma 4 (26B)", "family": "Gemma", "vram": 18.0, "modalities": ["text", "image"], "max_context": 262144, "hf_url": "https://huggingface.co/bartowski/gemma-2-27b-it-GGUF/resolve/main/gemma-2-27b-it-Q4_K_M.gguf" },
    { "id": "gemma4:31b", "name": "Gemma 4 (31B)", "family": "Gemma", "vram": 20.0, "modalities": ["text", "image"], "max_context": 262144, "hf_url": "https://huggingface.co/bartowski/gemma-2-27b-it-GGUF/resolve/main/gemma-2-27b-it-Q4_K_M.gguf" },
    { "id": "deepseek-coder-v2:16b", "name": "DeepSeek Coder v2 (16B)", "family": "DeepSeek", "vram": 8.9, "modalities": ["text"], "max_context": 131072, "hf_url": "https://huggingface.co/bartowski/DeepSeek-Coder-V2-Lite-Instruct-GGUF/resolve/main/DeepSeek-Coder-V2-Lite-Instruct-Q4_K_M.gguf" },
    { "id": "deepseek-coder-v2:236b", "name": "DeepSeek Coder v2 (236B)", "family": "DeepSeek", "vram": 133.0, "modalities": ["text"], "max_context": 131072, "hf_url": "https://huggingface.co/mradermacher/DeepSeek-Coder-V2-Instruct-GGUF/resolve/main/DeepSeek-Coder-V2-Instruct.Q4_K_M.gguf" },
    { "id": "mistral-medium-3.5:128b", "name": "Mistral Medium 3.5 (128B)", "family": "Mistral", "vram": 80.0, "modalities": ["text", "image"], "max_context": 32768, "hf_url": "https://huggingface.co/TheBloke/Mistral-7B-Instruct-v0.2-GGUF/resolve/main/mistral-7b-instruct-v0.2.Q4_K_M.gguf" },
    { "id": "north-mini-code-1.0:latest", "name": "North Mini Code 1.0", "family": "North", "vram": 2.5, "modalities": ["text"], "max_context": 8192, "hf_url": "https://huggingface.co/Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF/resolve/main/qwen2.5-coder-1.5b-instruct-q4_k_m.gguf" },
    { "id": "translategemma:4b", "name": "TranslateGemma (4B)", "family": "TranslateGemma", "vram": 3.3, "modalities": ["text", "image"], "max_context": 8192, "hf_url": "https://huggingface.co/bartowski/gemma-2-2b-it-GGUF/resolve/main/gemma-2-2b-it-Q4_K_M.gguf" },
    { "id": "translategemma:12b", "name": "TranslateGemma (12B)", "family": "TranslateGemma", "vram": 8.1, "modalities": ["text", "image"], "max_context": 8192, "hf_url": "https://huggingface.co/bartowski/gemma-2-9b-it-GGUF/resolve/main/gemma-2-9b-it-Q4_K_M.gguf" },
    { "id": "translategemma:27b", "name": "TranslateGemma (27B)", "family": "TranslateGemma", "vram": 17.0, "modalities": ["text", "image"], "max_context": 8192, "hf_url": "https://huggingface.co/bartowski/gemma-2-27b-it-GGUF/resolve/main/gemma-2-27b-it-Q4_K_M.gguf" },
    { "id": "qwen3.6:27b", "name": "Qwen 3.6 (27B)", "family": "Qwen", "vram": 17.0, "modalities": ["text", "image"], "max_context": 131072, "hf_url": "https://huggingface.co/Qwen/Qwen2.5-32B-Instruct-GGUF/resolve/main/qwen2.5-32b-instruct-q4_k_m.gguf" },
    { "id": "qwen3.6:35b", "name": "Qwen 3.6 (35B)", "family": "Qwen", "vram": 24.0, "modalities": ["text", "image"], "max_context": 131072, "hf_url": "https://huggingface.co/Qwen/Qwen2.5-32B-Instruct-GGUF/resolve/main/qwen2.5-32b-instruct-q4_k_m.gguf" },
    { "id": "llama3.1:405b", "name": "Llama 3.1 (405B)", "family": "Llama", "vram": 230.0, "modalities": ["text"], "max_context": 131072, "hf_url": "https://huggingface.co/mradermacher/Meta-Llama-3.1-405B-Instruct-GGUF/resolve/main/Meta-Llama-3.1-405B-Instruct.Q4_K_M.gguf" },
    { "id": "nemotron-4:340b", "name": "Nemotron-4 (340B)", "family": "Nemotron", "vram": 190.0, "modalities": ["text"], "max_context": 131072, "hf_url": "https://huggingface.co/mradermacher/Nemotron-4-340B-Instruct-GGUF/resolve/main/Nemotron-4-340B-Instruct.Q4_K_M.gguf" },
    { "id": "grok-1:314b", "name": "Grok-1 (314B)", "family": "Grok", "vram": 180.0, "modalities": ["text"], "max_context": 131072, "hf_url": "https://huggingface.co/mradermacher/grok-1-GGUF/resolve/main/grok-1.Q4_K_M.gguf" },
    { "id": "mistral-large:123b", "name": "Mistral Large (123B)", "family": "Mistral", "vram": 70.0, "modalities": ["text"], "max_context": 131072, "hf_url": "https://huggingface.co/mradermacher/Mistral-Large-Instruct-2407-GGUF/resolve/main/Mistral-Large-Instruct-2407.Q4_K_M.gguf" },
    { "id": "wizardlm2:8x22b", "name": "WizardLM 2 (8x22B)", "family": "WizardLM", "vram": 80.0, "modalities": ["text"], "max_context": 65536, "hf_url": "https://huggingface.co/mradermacher/WizardLM-2-8x22B-GGUF/resolve/main/WizardLM-2-8x22B.Q4_K_M.gguf" }
]


def _sanitize_model_library(library: List[dict]) -> List[dict]:
    """
    Returns a copy of the library with every ``id`` made filesystem-safe.

    ``:`` is illegal in a Windows path, so a model is stored on disk as ``phi3-mini.gguf``
    and every consumer (the WebUI, resolve_model_path, the redundancy sync) refers to it by
    the sanitised id. The raw ids only ever existed inside this module's literal.
    """
    import copy

    safe_library = copy.deepcopy(library)
    for entry in safe_library:
        if "id" in entry:
            entry["id"] = entry["id"].replace(":", "-")
    return safe_library


def load_models_json():
    """
    Reads (creating if needed) ``models/models.json`` and returns it with sanitised ids.

    The file written on first run is the *sanitised* copy. Writing raw ids while returning
    sanitised ones meant the on-disk file never matched the ids the API served: the
    InferenceManager's ``max_context`` lookup never hit, so every model silently fell back
    to 8192 tokens, and main.py's redundancy sync never found an ``hf_url`` for any model
    whose id contained a ``:``.
    """
    m_dir = os.path.join(get_base_dir(), "models")
    m_path = os.path.join(m_dir, "models.json")

    if not os.path.exists(m_path):
        sanitized_default = _sanitize_model_library(DEFAULT_MODEL_LIBRARY)
        try:
            os.makedirs(m_dir, exist_ok=True)
        except Exception:
            pass
        try:
            with open(m_path, "w", encoding="utf-8") as f:
                json.dump(sanitized_default, f, indent=4)
        except Exception as e:
            log_error(f"Failed to write models.json: {e}")
        return sanitized_default

    try:
        with open(m_path, "r", encoding="utf-8") as f:
            # Still sanitised on read: an operator-authored or older file may contain raw ids.
            return _sanitize_model_library(json.load(f))
    except Exception as e:
        log_error(f"Failed to parse models.json: {e}")
        return _sanitize_model_library(DEFAULT_MODEL_LIBRARY)


# ======================================================================================
# Multi-stream downloader
# ======================================================================================

def _stitch_parts(dest_path: str, num_streams: int) -> None:
    """
    Concatenates ``dest_path.part0`` .. ``.part{num_streams-1}`` into ``dest_path``.

    Assembles into a ``.tmp`` and renames, so nothing watching for the final name can ever
    observe a half-written model — which is the entire failure this exists to prevent. Parts
    are removed as they are consumed, so an interrupted stitch leaves less behind each time.
    """
    tmp_path = f"{dest_path}.tmp"
    with open(tmp_path, "wb") as outfile:
        for i in range(num_streams):
            part_file = f"{dest_path}.part{i}"
            with open(part_file, "rb") as infile:
                shutil.copyfileobj(infile, outfile, 1024 * 1024 * 10)
            os.remove(part_file)
    # os.replace, not os.rename: rename onto an existing path raises on Windows.
    os.replace(tmp_path, dest_path)


def model_file_is_usable(path: str) -> bool:
    """
    True when ``path`` is a model an engine can actually be launched against.

    Existence is not enough, and treating it as enough is what turned an interrupted
    download into a node that announced "model ready" and then crash-looped an engine every
    four seconds with no explanation. A GGUF header parse is a few milliseconds against a
    multi-gigabyte file — it reads the first blocks and stops — and it rejects the whole
    family of half-written, truncated and wrong-format files in one check.
    """
    try:
        if not os.path.isfile(path) or os.path.getsize(path) <= 0:
            return False
    except OSError:
        return False
    return gguf.read_metadata(path) is not None


async def download_file_multistream_async(url: str, dest_path: str, model_name: str,
                                          *, broadcast: bool = True) -> bool:
    """
    Downloads one file using 8 parallel HTTP range requests and stitches the parts.

    Args:
        url: Source URL (redirects are followed once, up front, so all 8 streams hit the
            same resolved LFS endpoint).
        dest_path: Final path for the assembled file.
        model_name: Label used in progress messages.
        broadcast: When False, no WebSocket progress is emitted. main.py's redundancy sync
            calls this from a plain thread via ``asyncio.run()``, which creates a *private*
            event loop; the WebSocket objects in ``active_websockets`` belong to the
            Coordinator's loop and touching them from another one corrupts both. Keyword-only
            so the flag can never be passed by accident as a positional argument.

    Returns:
        True on success. On any failure the partial ``.partN`` files are removed and False
        is returned — the caller decides whether to retry.
    """
    try:
        # Delete any existing file so the Coordinator's background loop cannot pick up a
        # stale or half-written model while the download is in flight.
        if os.path.exists(dest_path):
            try:
                os.remove(dest_path)
            except Exception:
                pass

        # Resolve the final redirected URL synchronously in a thread
        def get_total_size():
            with httpx.Client(follow_redirects=True, timeout=10) as client:
                r = client.head(url)
                return int(r.headers.get("content-length", 0)), str(r.url)

        total_size, final_url = await asyncio.to_thread(get_total_size)

        if total_size <= 0:
            log_error(f"Could not determine size for {safe_log_text(model_name)} from LFS endpoint.")
            return False

        num_streams = 8
        chunk_size = total_size // num_streams
        ranges = []
        for i in range(num_streams):
            start = i * chunk_size
            end = start + chunk_size - 1 if i < num_streams - 1 else total_size - 1
            ranges.append((start, end))

        # Recover or discard what a previous run left behind.
        #
        # The failure handler below deletes partial files, but it only runs for exceptions.
        # A node that is killed mid-download - Ctrl-C, a crash, the power going out, the OOM
        # killer - leaves eight .partN files on disk with nothing to clean them up. They then
        # sit there indefinitely: the next run re-downloads into the same names and, worse,
        # the model looks present enough to fool a caller into launching an engine against a
        # file that was never assembled.
        #
        # If every part is present and they sum to exactly the size the server reports, the
        # download did finish and only the stitch was lost, so stitch and be done - that is a
        # multi-gigabyte transfer saved on a home connection. Anything else is unusable:
        # delete it, because a partial part cannot be resumed from (the range each stream
        # holds is not recorded anywhere) and keeping it only invites the bug above.
        existing_parts = [f"{dest_path}.part{i}" for i in range(num_streams)]
        if all(os.path.exists(p) for p in existing_parts):
            recovered = sum(os.path.getsize(p) for p in existing_parts)
            if recovered == total_size:
                log_coordinator(
                    f"Found a complete but unassembled download for {safe_log_text(model_name)} "
                    f"({recovered / (1024 ** 3):.2f}GB). Stitching it rather than downloading again."
                )
                await asyncio.to_thread(_stitch_parts, dest_path, num_streams)
                return True
            log_warning(
                f"Discarding an incomplete download for {safe_log_text(model_name)}: "
                f"{recovered / (1024 ** 3):.2f}GB of {total_size / (1024 ** 3):.2f}GB. "
                "Partial streams cannot be resumed, so it starts again."
            )
        for stale in existing_parts:
            if os.path.exists(stale):
                try:
                    os.remove(stale)
                except Exception:
                    pass

        progress_array = [0] * num_streams

        # Cooperative stop flag for the eight worker threads. A ThreadPoolExecutor cannot
        # interrupt a thread that is already running, so "cancelling" a download means asking
        # it to stop at its next chunk boundary. Without this, a failed download left seven
        # threads still writing into `.partN` files while the cleanup handler below deleted
        # those very files — on Windows the delete then fails outright (the handle is open),
        # and on POSIX it succeeds while the thread keeps writing into an unlinked inode,
        # so the bytes and the file descriptors are both leaked until the process exits.
        stop_downloads = threading.Event()

        def download_range_sync(idx, start, end):
            headers = {"Range": f"bytes={start}-{end}"}
            part_file = f"{dest_path}.part{idx}"

            with httpx.Client(follow_redirects=False, timeout=120) as client:
                with client.stream("GET", final_url, headers=headers) as r:
                    if r.status_code not in (200, 206):
                        raise Exception(f"Server returned status {r.status_code}")
                    with open(part_file, "wb") as f:
                        # 2MB chunks. Network/Disk I/O releases the GIL automatically.
                        for chunk in r.iter_bytes(chunk_size=1024 * 1024 * 2):
                            if stop_downloads.is_set():
                                # A sibling stream failed (or the whole download was
                                # abandoned). Stop here so the file handle is closed by the
                                # `with` above before anyone tries to delete this part.
                                return
                            if chunk:
                                f.write(chunk)
                                progress_array[idx] += len(chunk)

        # Run downloads in a true OS thread pool, completely escaping the asyncio event loop.
        loop = asyncio.get_running_loop()
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=num_streams)
        try:
            tasks = [loop.run_in_executor(executor, download_range_sync, i, r[0], r[1])
                     for i, r in enumerate(ranges)]

            while not all(t.done() for t in tasks):
                downloaded = sum(progress_array)
                percent = (downloaded / total_size) * 100
                print(f"\rDownloading {model_name} (8 streams): {percent:.1f}%", end="")
                if broadcast:
                    # _broadcast, not broadcast: the keyword-only flag above shadows the
                    # helper's name inside this function.
                    await _broadcast({"type": "download_progress", "model": model_name, "percent": percent})
                await asyncio.sleep(0.5)

            # Ensure all tasks completed successfully (re-raises the first failure).
            for t in tasks:
                await t
        finally:
            # The executor used to be shut down only on the success path, so every failed
            # download leaked 8 OS threads for the lifetime of the process.
            #
            # Order matters here. Signal first, so any thread still streaming stops at its
            # next 2 MB boundary, then join with wait=True so no thread is still holding a
            # `.partN` file open when the cleanup handler removes it. The join is pushed to a
            # worker thread because shutdown(wait=True) blocks, and blocking the event loop
            # would stall every other request for the duration.
            stop_downloads.set()
            try:
                await asyncio.to_thread(executor.shutdown, True, cancel_futures=True)
            except BaseException:
                # The join itself was interrupted (task cancellation during shutdown).
                # shutdown() is idempotent, so ask again without waiting rather than leaving
                # the pool un-shut-down entirely, and let the interruption propagate.
                executor.shutdown(wait=False, cancel_futures=True)
                raise

        print("\nStitching chunks together...")
        await asyncio.to_thread(_stitch_parts, dest_path, num_streams)
        return True
    except Exception as e:
        log_error(f"Multi-stream download failed for {safe_log_text(model_name)}: {safe_log_text(e, max_chars=200)}")
        for i in range(8):
            part_file = f"{dest_path}.part{i}"
            if os.path.exists(part_file):
                try:
                    os.remove(part_file)
                except Exception:
                    pass
        return False


# ======================================================================================
# Background monitors
# ======================================================================================

async def monitor_models_json_async():
    """Watches models.json for changes every 15s and pushes to connected WebUI clients."""
    models_json_path = os.path.join(get_base_dir(), "models", "models.json")
    last_mtime = 0
    while True:
        try:
            if os.path.exists(models_json_path):
                mtime = os.stat(models_json_path).st_mtime
                if mtime > last_mtime:
                    last_mtime = mtime
                    await broadcast({"type": "library_sync", "library": load_models_json()})
            else:
                # File deleted, recreate it
                load_models_json()
        except Exception as e:
            log_error(f"models.json monitor loop error: {type(e).__name__}: {e}")
        await asyncio.sleep(15)


# Seed-download retry backoff (bug #26). A failed multi-GB pull used to reset both flags
# immediately, so the 2s monitor loop restarted the whole download on its very next pass —
# hammering HuggingFace and the local disk indefinitely when the real problem was no
# internet. These are monotonic-clock deadlines so an NTP step cannot skip the backoff.
SEED_RETRY_BASE_SECONDS = 60.0
SEED_RETRY_MAX_SECONDS = 1800.0  # 30 minutes
_seed_retry_not_before = 0.0
_seed_retry_delay = 0.0


# ======================================================================================
# Cluster reconfiguration — never on the monitor loop's own timeline (finding R3-5)
# ======================================================================================
#
# THE BUG THIS STRUCTURE EXISTS TO PREVENT
#     monitor_workers_async runs every two seconds and is the only thing that prunes dead
#     workers and pushes cluster telemetry to the WebUI. It used to `await
#     manager.astart_model(...)` directly in its body. That call can legitimately take
#     minutes — it downloads the llama.cpp release on a cold host, spawns the engine and
#     waits for a multi-GB weight load — and when another thread is already starting the same
#     model it parks on that thread's completion event for up to START_HANDOFF_TIMEOUT_SECONDS
#     (300 s). For the entire duration the loop's own `await asyncio.sleep(2)` was never
#     reached: prune_dead_workers stopped running, both broadcasts stopped, every live worker
#     eventually timed out of the registry and the WebUI went stale — all while nothing was
#     actually wrong.
#
# THE RULE
#     The monitor decides *what* the cluster should do and hands the work to a background
#     task. It never waits for the result. A tick that finds a reconfiguration already in
#     flight skips its model-management section entirely and comes back in two seconds; the
#     pruning and the broadcasts happen on every tick regardless. "Skip the tick and let the
#     next one pick it up" is the whole design.
#
# WHY A SINGLE SLOT RATHER THAN A TASK PER DECISION
#     Every operation here is cluster-wide: stop everything, or start the one globally active
#     model. Two of them running concurrently would fight over the same engines, and the
#     manager would serialise them anyway. One at a time is both correct and the behaviour
#     the code had before — it just no longer costs the loop its heartbeat.

# How long monitor_workers_async waits between passes. Named rather than inlined because the
# whole point of finding R3-5 is that this interval is a promise: the registry is pruned and
# the WebUI is refreshed *this often*, unconditionally, whatever the cluster is doing.
MONITOR_TICK_SECONDS = 2

_reconfiguration_task: Optional[asyncio.Task] = None


def reconfiguration_in_flight() -> bool:
    """True while a cluster reconfiguration spawned by the monitor is still running."""
    return _reconfiguration_task is not None and not _reconfiguration_task.done()


def start_reconfiguration(coro, name: str) -> bool:
    """
    Hand a cluster reconfiguration to a background task, if none is already running.

    Returns True when the work was accepted, False when it was declined because another
    reconfiguration is still in flight. Declining is the normal, safe outcome: the monitor
    re-evaluates from scratch two seconds later, so nothing is lost by not queueing.

    The coroutine object is closed on the decline path, otherwise Python emits a
    "coroutine was never awaited" RuntimeWarning for work we deliberately chose not to do.
    """
    global _reconfiguration_task

    if reconfiguration_in_flight():
        coro.close()
        return False

    _reconfiguration_task = spawn_background_task(coro, name)
    return True


async def _apply_topology_change(model_path: str, endpoints: List[str],
                                 total_vram: float, total_sysram: float, reason: str) -> None:
    """
    Restart ``model_path`` across a changed set of RPC endpoints.

    Runs as a background task so the monitor loop keeps ticking through what can be several
    minutes of teardown and weight loading. ``cluster_status`` is updated on the way out so
    the next broadcast carries the result.

    NOTE ON PORTS: the replacement engine is allocated its port before the outgoing one has
    finished dying (a live engine keeps its port until it is really gone), so a model
    ping-pongs between two ports across restarts rather than climbing. Never cache a port
    across a restart — ``astart_model``'s return value is the authority, which is why the
    VRAM-progress reader below is spawned with the port this call returned.
    """
    global cluster_status

    log_coordinator(reason)
    await manager.astop_all_models()
    success, port, msg = await manager.astart_model(model_path, endpoints, total_vram, total_sysram)
    if success:
        spawn_vram_progress_task(port, model_path)
    else:
        # A restart that cannot be completed leaves the cluster with nothing running, so the
        # status has to say so rather than keeping the stale "Active" from before the change.
        if is_model_not_on_disk(msg):
            cluster_status = "Paused (Awaiting Model Download)"
        else:
            log_error(
                f"Failed to restart {safe_log_text(model_path)} after a topology change: "
                f"{safe_log_text(msg)}"
            )
            cluster_status = f"Paused ({safe_log_text(msg, max_chars=48)})"


async def _auto_deploy_model(model_path: str, endpoints: List[str], total_vram: float,
                             total_sysram: float, required_memory: float) -> None:
    """
    Bring the globally selected model up because the cluster now has room for it.

    Background task, for the reasons in the section header above. ``can_admit`` has already
    said yes on the monitor's behalf, so the only failures expected here are races and a
    model whose weights are still downloading.
    """
    global cluster_status

    log_coordinator(f"Auto-Deploying target model: {safe_log_text(model_path)}...")
    success, port, msg = await manager.astart_model(model_path, endpoints, total_vram, total_sysram)

    if success:
        spawn_vram_progress_task(port, model_path)
        if required_memory <= total_vram:
            cluster_status = "Active"
        elif total_vram == 0:
            cluster_status = "Active (CPU-Only)"
        else:
            cluster_status = "Active (Spillover)"
        return

    if is_model_not_on_disk(msg):
        # Not an error: the weights are still being fetched. The gate above normally catches
        # this before we get here (can_admit resolves the path too), so reaching it means the
        # file vanished between the two checks.
        cluster_status = "Paused (Awaiting Model Download)"
    elif is_retryable_start_failure(msg):
        # Another thread's teardown or start won the race. The next monitor tick re-evaluates
        # from scratch, so there is nothing to report and nothing to do.
        cluster_status = f"Paused (Needs {required_memory:.1f}GB Total)"
    else:
        log_error(f"Failed to start model {safe_log_text(model_path)}: {safe_log_text(msg)}")
        cluster_status = f"Paused ({safe_log_text(msg, max_chars=48)})"


async def _pause_cluster_for_memory(model_path: str, required_memory: float,
                                    available_memory: float) -> None:
    """
    Tear every engine down because the pooled memory no longer covers the active model.

    Backgrounded like the other two: ``astop_all_models`` costs up to a few seconds per
    engine in terminate/wait, and the monitor must not stop pruning workers to pay for it.

    ``model_path`` is passed rather than read from ``global_active_model`` because this runs
    later than the decision that spawned it, and an operator swapping models in between would
    otherwise put the wrong name in the alert.
    """
    log_coordinator(
        f"ALERT: Total memory ({available_memory}GB) fell below required ({required_memory}GB) "
        f"for {safe_log_text(model_path)}. PAUSING CLUSTER!"
    )
    await manager.astop_all_models()


# The endpoint set the monitor last saw, and when it first saw it. Module-level rather than
# loop-local because the loop body runs fresh every two seconds and the whole point is to
# remember what the previous ticks looked like.
_pending_topology: Optional[Set[str]] = None
_pending_topology_since: float = 0.0


def _planned_endpoints(required_gb: float) -> List[str]:
    """The endpoints a model this size should run on, under the configured shard policy."""
    return registry.plan_endpoints(
        required_gb, get_worker_limits().get("shard_policy", "auto")
    )


def _topology_change_is_settled(planned: List[str]) -> bool:
    """
    True when the engine should be rebuilt for a changed set of endpoints.

    A rebuild is not free: every remote backend is re-sent its share of the weights, which
    on a WAN node is gigabytes over somebody's home connection. The old test rebuilt the
    instant the set differed, on a loop that runs twice a second, so a node whose heartbeat
    stuttered could cost the cluster a full redistribution — repeatedly.

    The two directions genuinely deserve different answers, so they get them:

    * **Losing an endpoint acts immediately.** The engine is already broken; it holds a
      backend that is gone, and every second of debounce is a second of outage.
    * **Gaining one waits** for the set to hold still for ``TOPOLOGY_SETTLE_SECONDS``. The
      cluster is serving correctly in the meantime — the new node is a bonus, not a repair —
      and a node that is flapping should finish deciding before anything is rebuilt for it.

    Comparing against the *planned* set rather than every registered endpoint matters on its
    own: under the fit-aware policy a node can join, be correctly judged unnecessary, and
    change nothing. That used to force a full restart for no reason at all.
    """
    global _pending_topology, _pending_topology_since

    current = set(manager.current_endpoints)
    target = set(planned)

    if current == target:
        _pending_topology = None
        return False

    # Something the engine is using has gone away. Nothing to wait for.
    if current - target:
        _pending_topology = None
        return True

    # Active inference leases guard running models from mid-task restarts.
    # While leases are held, never rebuild for a gain.
    if lease_manager.is_locked():
        return False

    limits = get_worker_limits()
    # Zero-rework policy: if rebalance_on_join is disabled, newly joined nodes are
    # placed into the reserve compute pool without restarting the active model.
    if not limits.get("rebalance_on_join", True):
        return False

    settle = float(get_worker_limits().get("topology_settle_seconds", 30))
    if settle <= 0:
        return True

    now = time.time()
    if _pending_topology != target:
        # A different set from the one we were waiting on: restart the clock rather than
        # inheriting a timer that was counting toward a topology that no longer exists.
        _pending_topology = target
        _pending_topology_since = now
        log_coordinator(
            f"{len(target - current)} new endpoint(s) available. Waiting {settle:.0f}s for "
            "the cluster to settle before redistributing the model."
        )
        return False

    if now - _pending_topology_since >= settle:
        _pending_topology = None
        return True

    return False


async def monitor_workers_async():
    """
    Async background loop to purge disconnected workers, evict models, and broadcast cluster stats.

    Runs every two seconds for the life of the process. Two invariants matter more than
    anything the loop actually decides:

    * **It never blocks.** Nothing in the body may wait on a model start, a teardown or a
      lock. Every one of those is handed to a background task through
      :func:`start_reconfiguration` (finding R3-5), and every InferenceManager call that
      takes ``_state_lock`` or touches the filesystem is made through the manager's
      ``a``-prefixed wrapper so the work happens on a thread. The two exceptions are
      deliberate: ``manager.current_endpoints`` and ``manager.get_active_model_names()``
      take no lock and do nothing but one C-level ``list(dict.values())`` copy, which is
      atomic with respect to the worker threads that mutate the dict.
    * **Pruning and broadcasting happen on every tick,** whatever else is going on. They are
      what keeps the registry honest and the WebUI live, and they used to be the collateral
      damage whenever a model start went slow.
    """
    global global_active_model, cluster_status, has_pulled_seed, is_downloading_seed
    while True:
        try:
            registry.prune_dead_workers()

            total_vram = registry.get_total_vram_gb()
            total_sysram = registry.get_total_sysram_gb()

            seed_ready_to_retry = time.monotonic() >= _seed_retry_not_before
            if ((total_vram + total_sysram) > 0 and not has_pulled_seed and not is_downloading_seed
                    and global_active_model == "phi3-mini" and seed_ready_to_retry):
                is_downloading_seed = True
                log_coordinator("Compute detected! Bootstrapping the default Seed Model (phi3-mini) in the background...")

                async def background_seed_pull_async():
                    global has_pulled_seed, is_downloading_seed
                    global _seed_retry_delay, _seed_retry_not_before

                    await asyncio.sleep(5.0)

                    custom_dir = os.path.join(get_base_dir(), "models")
                    phi3_path = os.path.join(custom_dir, "phi3-mini.gguf")
                    if model_file_is_usable(phi3_path):
                        log_coordinator("Seed model 'phi3-mini' already exists locally. Skipping download.")
                        has_pulled_seed = True
                        is_downloading_seed = False
                        return
                    if os.path.exists(phi3_path):
                        # Present but not a readable GGUF: a truncated download, a failed
                        # stitch, or an HTML error page saved under a .gguf name. Skipping the
                        # download on the strength of the filename alone is what produced a
                        # node that reported the model ready and then respawned a dying engine
                        # indefinitely. Remove it and pull it again.
                        log_warning(
                            "Seed model 'phi3-mini' exists but is not a readable GGUF file. "
                            "Removing it and downloading again."
                        )
                        try:
                            os.remove(phi3_path)
                        except OSError as e:
                            log_error(f"Could not remove the unusable seed model: {e}")
                            is_downloading_seed = False
                            return

                    success = await download_file_multistream_async(
                        "https://huggingface.co/microsoft/Phi-3-mini-4k-instruct-gguf/resolve/main/Phi-3-mini-4k-instruct-q4.gguf",
                        phi3_path,
                        "phi3-mini"
                    )

                    if success:
                        log_coordinator("Seed model 'phi3-mini' downloaded and ready.")
                        has_pulled_seed = True
                        is_downloading_seed = False
                        _seed_retry_delay = 0.0
                        _seed_retry_not_before = 0.0
                        await broadcast({"type": "download_progress", "model": "phi3-mini", "percent": 100})
                    else:
                        # Exponential backoff: 60s, 120s, 240s ... capped at 30 minutes.
                        _seed_retry_delay = min(
                            SEED_RETRY_MAX_SECONDS,
                            _seed_retry_delay * 2 if _seed_retry_delay else SEED_RETRY_BASE_SECONDS
                        )
                        _seed_retry_not_before = time.monotonic() + _seed_retry_delay
                        log_error(
                            "Seed pull failed. Check internet connection. "
                            f"Retrying in {_seed_retry_delay / 60:.0f} minute(s)."
                        )
                        has_pulled_seed = False
                        is_downloading_seed = False

                spawn_background_task(background_seed_pull_async(), "seed-pull")

            # 1. Global Sync & Spillover/Pause Logic
            #
            # Skipped wholesale while the cluster is already being reconfigured — by a task
            # this loop spawned, by an operator-driven model swap, or by the viral-traffic
            # queue recalculation. Re-deciding on top of an in-flight change would have two
            # cluster-wide operations fighting over the same engines, and there is nothing to
            # lose by waiting: the next tick is two seconds away and re-evaluates from
            # scratch.
            if global_active_model and not (reconfiguration_in_flight()
                                            or is_swapping_models
                                            or is_recalculating_queue):
                # aestimate_model_vram_gb (finding R3-12): the synchronous version stats the
                # config file, realpaths every model search root and getsizes the weights.
                # Run straight on the loop at 0.5 Hz forever, that is unbounded blocking I/O
                # — and on a network-mounted models directory a single realpath can stall the
                # whole coordinator.
                required_memory = await manager.aestimate_model_vram_gb(global_active_model)

                # asnapshot_model (finding R3-9): `list(manager.running_models.keys())` was an
                # unsynchronised read of a dict that worker threads mutate. The snapshot is
                # taken under the manager's own lock and answers the only question asked of
                # it here — is the globally selected model up right now?
                running_entry = await manager.asnapshot_model(global_active_model)

                if running_entry is not None:
                    # It is currently running. Check if we have enough resources.
                    if required_memory > (total_vram + total_sysram):
                        # Status is set here rather than in the task so this tick's broadcast
                        # already tells the WebUI the cluster is pausing.
                        cluster_status = f"Paused (Needs {required_memory:.1f}GB Total)"
                        start_reconfiguration(
                            _pause_cluster_for_memory(global_active_model, required_memory,
                                                      total_vram + total_sysram),
                            "cluster-pause",
                        )
                    elif required_memory > total_vram:
                        # Fits, but spills over to SysRAM. Let it run, but check topology.
                        cluster_status = "Active (CPU-Only)" if total_vram == 0 else "Active (Spillover)"
                        current_rpc = _planned_endpoints(required_memory)
                        if _topology_change_is_settled(current_rpc):
                            start_reconfiguration(
                                _apply_topology_change(
                                    global_active_model, current_rpc, total_vram, total_sysram,
                                    f"Network topology changed. Restarting "
                                    f"{safe_log_text(global_active_model)} for SysRAM Spillover.",
                                ),
                                "topology-restart",
                            )
                    else:
                        cluster_status = "Active"
                        current_rpc = _planned_endpoints(required_memory)
                        if _topology_change_is_settled(current_rpc):
                            start_reconfiguration(
                                _apply_topology_change(
                                    global_active_model, current_rpc, total_vram, total_sysram,
                                    f"Network topology changed. Re-distributing "
                                    f"{safe_log_text(global_active_model)} to new VRAM pool.",
                                ),
                                "topology-restart",
                            )
                else:
                    # It is NOT running. Can we run it?
                    #
                    # Ask the InferenceManager rather than recomputing. This gate used to be
                    # `required_memory <= (total_vram + total_sysram)`: weights only, against
                    # the raw uncorrected pool, with no POOL_SAFETY_FRACTION, no term for
                    # memory already committed by other running models, and no KV-cache term.
                    # That is a *different, larger* number than the budget start_model then
                    # applies, so the coordinator would announce "Auto-Deploying", call
                    # astart_model, and get "Network Maxed Out" back — every two seconds,
                    # forever. can_admit runs exactly start_model's arithmetic, under the same
                    # lock, from literally the same function, so there is one formula in the
                    # codebase instead of two.
                    #
                    # The topology is passed as well: a changed worker set forces a restart,
                    # and can_admit can only account for that restart's memory if it knows
                    # which endpoints the caller intends to use.
                    current_rpc = _planned_endpoints(required_memory)
                    admissible, admit_reason = await manager.acan_admit(
                        global_active_model, total_vram, total_sysram, current_rpc
                    )
                    if admissible:
                        if is_downloading_seed and global_active_model == "phi3-mini":
                            cluster_status = "Downloading Model"
                        else:
                            # We have enough memory to auto-resume. The deployment itself
                            # runs as a background task; this tick carries on and broadcasts.
                            start_reconfiguration(
                                _auto_deploy_model(global_active_model, current_rpc,
                                                   total_vram, total_sysram, required_memory),
                                "auto-deploy",
                            )
                    elif is_model_not_on_disk(admit_reason):
                        # can_admit resolves the model path now (it did not before), so the
                        # "queued but not downloaded yet" case is refused *here* instead of
                        # being announced as "Auto-Deploying" every two seconds and then
                        # failing inside start_model. This is the correct status for it, and
                        # it is what the old suppressed-log branch was trying to express.
                        cluster_status = "Paused (Awaiting Model Download)"
                    elif admit_reason in (NETWORK_MAXED_OUT_MESSAGE, ADMISSION_OK_REASON):
                        # The ordinary "not enough hardware yet" case, whose wording the
                        # WebUI expects to show.
                        cluster_status = f"Paused (Needs {required_memory:.1f}GB Total)"
                    else:
                        # Anything else can_admit reported — no free port, for instance — is
                        # surfaced rather than silently rendered as a memory shortfall.
                        cluster_status = f"Paused ({safe_log_text(admit_reason, max_chars=48)})"

            # Broadcast Cluster Stats & Global State
            nodes = registry.count()
            await broadcast({
                "type": "cluster_stats",
                "nodes": nodes,
                "vram": total_vram,
                "sysram": total_sysram,
                "cpu_cores": registry.get_total_cpu_cores(),
                "cpu_threads": registry.get_total_cpu_threads(),
                "status": cluster_status
            })
            await broadcast({
                "type": "cluster_sync",
                "active_model": global_active_model,
                "status": cluster_status
            })

            await asyncio.sleep(MONITOR_TICK_SECONDS)
        except Exception as e:
            log_error(f"CRITICAL: monitor_workers_async loop exception: {e}")
            await asyncio.sleep(MONITOR_TICK_SECONDS)


# ======================================================================================
# Coding members (docs/coding-members.md, engine/coding_router.py)
# ======================================================================================

def _coding_members(limits: Optional[Dict[str, Any]] = None):
    """The members that can be sent work now, with TRUSTED_MEMBERS applied to the router."""
    from engine.reverse_tunnel import reverse_bridge
    limits = limits if limits is not None else get_worker_limits()
    coding_router.trusted_ids = set(limits.get("trusted_members") or ())
    return coding_router.members(registry, reverse_bridge.get_relay_port, limits)


def _gpu_pool_status(measured_tokens_per_second: Optional[float]) -> Dict[str, Any]:
    """
    ``gpu_pool`` for /v1/cluster/status (the website's GPU tile): counts only, nothing names a machine.

    SERVING GPUs are those with a job: the nodes of the sharded RPC pool that the running engine was started
    with (``manager.current_endpoints``; this host's own GPUs are among them while its engine runs on them),
    and every ready coding member's. STANDING BY are live GPU nodes with no job: awake but not in the running
    engine's topology, or a verified coding member that is not ready. A node handed back to its owner
    (yielding) is in neither. ``tokens_per_second`` is what the coding engine and the members generated over
    the last 60 seconds (engine/throughput.py); ``measured_tokens_per_second`` the single-user measurement.
    """
    limits = get_worker_limits()
    in_engine = set(manager.current_endpoints)
    members_on = bool(limits.get("coding_members_enabled", False))
    ready_members = {m.worker_id for m in _coding_members(limits)} if members_on else set()
    verified = {m.worker_id for m in _all_coding_members(limits) if m.trust_id and not coding_router.is_blocked(m)}
    serving = {"gpus": 0, "vram": 0.0}
    standing = {"gpus": 0, "vram": 0.0}
    for node in registry.gpu_inventory():
        if node["pool"] == "coding":
            if members_on and node["worker_id"] in ready_members:
                bucket = serving
            elif node["worker_id"] in verified:
                bucket = standing
            else:
                continue            # a block that does not verify, or a member a canary caught: not advertised
        else:
            bucket = serving if node["endpoint"] in in_engine else standing
        bucket["gpus"] += node["gpus"]
        bucket["vram"] += node["vram_gb"]
    return {
        "gpus": serving["gpus"],
        "vram_gb": round(serving["vram"], 1),
        "gpus_standing_by": standing["gpus"],
        "vram_standing_by_gb": round(standing["vram"], 1),
        "tokens_per_second": round(throughput.rate(POOL_CODING, 60), 2),
        "tokens_last_hour": throughput.total(POOL_CODING, 3600),
        "measured_tokens_per_second": measured_tokens_per_second,
    }


def _cpu_pool_status() -> Dict[str, Any]:
    """
    ``cpu_pool`` for /v1/cluster/status (the website's CPU tile): the phone pool's members, this
    coordinator's own included. ``machines`` are those ready and serving, ``machines_standing_by`` those
    registered but not ready; ``cores`` (CPU threads the phone model runs on) and ``ram_gb`` (RAM lent) are
    the serving machines'. Counts only.
    """
    members = _app_members()
    serving = [m for m in members if m.ready]
    return {
        "machines": len(serving),
        "machines_standing_by": len(members) - len(serving),
        "cores": sum(m.threads for m in serving),
        "ram_gb": round(sum(m.ram_gb for m in serving), 1),
        "tokens_per_second": round(throughput.rate(POOL_PHONE, 60), 2),
        "tokens_last_hour": throughput.total(POOL_PHONE, 3600),
    }


def _all_coding_members(limits: Dict[str, Any]):
    from engine.reverse_tunnel import reverse_bridge
    coding_router.trusted_ids = set(limits.get("trusted_members") or ())
    return coding_router.all_members(registry, reverse_bridge.get_relay_port, limits)


def _pool_status_or_empty(build, empty: Dict[str, Any], label: str) -> Dict[str, Any]:
    """A pool figure for the status, or all zeros if it cannot be built: the status must always answer."""
    try:
        return build()
    except Exception as exc:  # noqa: BLE001
        log_warning(f"The {label} status is unavailable ({type(exc).__name__}: {exc}).")
        return empty


_EMPTY_GPU_POOL = {"gpus": 0, "vram_gb": 0.0, "gpus_standing_by": 0, "vram_standing_by_gb": 0.0,
                   "tokens_per_second": 0.0, "tokens_last_hour": 0, "measured_tokens_per_second": None}
_EMPTY_CPU_POOL = {"machines": 0, "machines_standing_by": 0, "cores": 0, "ram_gb": 0.0,
                   "tokens_per_second": 0.0, "tokens_last_hour": 0}


def _coding_members_status() -> Dict[str, Any]:
    from engine.reverse_tunnel import reverse_bridge
    limits = get_worker_limits()
    try:
        return coding_router.status(registry, reverse_bridge.get_relay_port, limits,
                                    bool(limits.get("coding_members_enabled", False)))
    except Exception as exc:  # noqa: BLE001 - the status must answer even if this part cannot
        log_warning(f"Coding member status unavailable ({type(exc).__name__}: {exc}).")
        return {"enabled": bool(limits.get("coding_members_enabled", False)),
                "members": 0, "ready": 0, "slots": 0, "busy": 0}


# Canaries (engine/canary.py): the scheduler is given the routable members (CodingMember: trust id,
# key, last_served_at in epoch seconds) and decides for itself who is due, never a trusted or idle one.

def _canary_members() -> List[Any]:
    """The members canaries may be sent to: none while coding members are off."""
    if not get_worker_limits().get("coding_members_enabled", False):
        return []
    return _coding_members()


def _canary_trusted() -> Set[str]:
    """Trust ids the operator listed in TRUSTED_MEMBERS: those members are never sent canaries."""
    return set(coding_members_lib.normalize_trust_ids(get_worker_limits().get("trusted_members") or ()))


async def _canary_forward(member: Any, wire: bytes, headers: Dict[str, str]) -> Any:
    """A canary request handed to a member along the path routed traffic takes (engine/canary.py)."""
    async with engine_client() as client:
        return await coding_router.forward_to(client, _coding_members, member, wire, headers)


# How long recent answers held a local engine slot, for the wait estimate in the queue frames.
_coding_hold_seconds: Deque[float] = deque(maxlen=40)

# A streamed request that has to wait tells its client where it stands this often (the first time at
# once), and gives up after this long (the extension gives up on its side after 15 minutes).
QUEUE_FRAME_SECONDS = 3.0
QUEUE_POLL_SECONDS = 1.0
QUEUE_MAX_WAIT_SECONDS = 15 * 60.0


def _note_coding_hold(seconds: float) -> None:
    if 0 < seconds < 3600:
        _coding_hold_seconds.append(seconds)


def _coding_eta(position: int, slots: int) -> Optional[int]:
    """A best guess, in seconds, at how long until the request at ``position`` gets a slot: the typical
    time recent answers held one, times the position, over the slots working in turn. None until a few
    answers have been seen."""
    holds = sorted(_coding_hold_seconds)
    if len(holds) < 3:
        return None
    return max(1, min(3600, int(round(holds[len(holds) // 2] * position / max(1, slots)))))


def _sse_notice_bytes(text: str) -> bytes:
    chunk = {"choices": [{"delta": {"content": text}}]}
    return f"data: {json.dumps(chunk)}\n\n".encode() + b"data: [DONE]\n\n"


class _QueuedChat:
    """
    A streamed chat request that has to wait for a coding slot (docs/coding-members.md section 10).

    It is answered at once (HTTP 200, text/event-stream) and, while it waits, sent
    ``data: {"dlne_queue": {"position", "waiting", "slots", "in_use", "eta_seconds"}}`` frames, the first
    immediately and then every few seconds, before any chunk of the model's. They come from this
    coordinator, so they are never sealed, and carry only counts: nothing of the request, the client or a
    member. ``position`` is the exact place in the queue slots are handed out in (best tier first, then
    the earliest arrival; 1 = next) at the local engine's gate. Only the local engine serves a plain
    request, so that is the only queue there is.

    The request then goes to the usual handler, which takes over the slot. Failing before a slot is
    won (the client leaves) frees everything it held.
    """

    def __init__(self, request: Request, parsed: Any, limits: Dict[str, Any]) -> None:
        self.request, self.parsed, self.limits = request, parsed, limits
        self.priority = tier_priority(getattr(getattr(request, "state", None), "dlne_tier", None))
        self.gate_ticket: Dict[str, Any] = {}
        self.gate_task: Optional["asyncio.Task"] = None
        self.counted = False
        self.winner: Optional[str] = None

    # ---------------------------------------------------------------- what the frames say
    def state(self) -> Dict[str, int]:
        stats = coding_gate.stats()
        slots, in_use = stats["slots"], stats["in_use"]
        place = coding_gate.position(self.gate_ticket) or 1
        state = {"position": place, "waiting": max(stats["waiting"], place), "slots": slots,
                 "in_use": min(in_use, slots)}
        eta = _coding_eta(place, slots)
        if eta is not None:
            state["eta_seconds"] = eta
        return state

    @staticmethod
    def frame(state: Dict[str, int]) -> bytes:
        return f"data: {json.dumps({'dlne_queue': state})}\n\n".encode()

    # ---------------------------------------------------------------- the waiting
    def _start_waiting(self) -> None:
        """Join the queue for the local engine's slots."""
        self.gate_ticket.clear()
        self.gate_task = asyncio.create_task(coding_gate.acquire(self.priority, ticket=self.gate_ticket))

    @staticmethod
    def _gate_granted(task: Optional["asyncio.Task"]) -> bool:
        return task is not None and task.done() and not task.cancelled() and task.exception() is None

    # Leaving the queue is synchronous on purpose: a client that disconnects cancels this coroutine, and
    # nothing that gives a slot back may wait for anything. A wait that is still pending is cancelled
    # and gives back what it was granted in that same instant itself (CodingGate.acquire).

    def _drop_gate(self) -> None:
        """Leave the engine's queue; a slot already granted and not passed on is given back."""
        task, self.gate_task = self.gate_task, None
        if task is None:
            return
        if not task.done():
            task.cancel()
        elif self._gate_granted(task):
            coding_gate.release()

    async def cleanup(self) -> None:
        """Give back everything still held (the queue place, a slot just granted, the in-flight count).
        Safe to call again: it runs when the stream ends and again as the response's background task."""
        self._drop_gate()
        if self.counted:
            self.counted = False
            await _change_inference_count(-1)

    async def stream(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self._run():
                yield chunk
        finally:
            await self.cleanup()

    async def _wait(self, deadline: float) -> AsyncIterator[bytes]:
        """Frames until the engine's slot is ours; then ``self.winner`` is "engine" (None: gave up)."""
        loop = asyncio.get_running_loop()
        self.winner = None
        self._start_waiting()
        last_frame: Optional[float] = None
        await asyncio.wait([self.gate_task], timeout=0.05)
        while True:
            if self._gate_granted(self.gate_task):
                self.winner = "engine"
                return
            if self.gate_task.done() or loop.time() >= deadline:
                return
            now = loop.time()
            if last_frame is None or now - last_frame >= QUEUE_FRAME_SECONDS:
                yield self.frame(self.state())
                last_frame = now
            await asyncio.wait([self.gate_task], timeout=QUEUE_POLL_SECONDS)

    async def _run(self) -> AsyncIterator[bytes]:
        loop = asyncio.get_running_loop()
        await _change_inference_count(1)       # waiting counts as in flight: the traffic detector and auto-swap see it
        self.counted = True
        async for frame in self._wait(loop.time() + QUEUE_MAX_WAIT_SECONDS):
            yield frame
        if self.winner != "engine":
            yield _sse_notice_bytes("\n\n[System] Still no free coding slot after 15 minutes. Please try again later.")
            return

        # The local engine's slot is ours: the usual handler takes it over from here.
        self.gate_task = None
        self.counted = False
        await _change_inference_count(-1)      # the handler counts the request itself
        try:
            response = await _chat_completions_impl(self.request, slot_held=True)
        except HTTPException as exc:
            yield _sse_notice_bytes(f"\n\n[System Alert] {exc.detail}")
            return
        except Exception as exc:  # noqa: BLE001 - the 200 has gone out: say so in the stream
            log_error(f"Queued chat request failed: {type(exc).__name__}: {exc}")
            yield _sse_notice_bytes("\n\n[System Alert] The inference engine failed to answer. Please send your message again.")
            return
        try:
            async for chunk in response.body_iterator:
                yield chunk
        finally:
            if getattr(response, "background", None) is not None:
                await response.background()


def _coding_role_near(worker_id: str, ip: str, port: Any, settled: bool = False) -> Optional[bool]:
    """
    Whether a GPU worker is near enough to join the sharded engine (auto-swap's rule, the
    LinkProbe round trip against AUTO_SWAP_MAX_RTT_MS), False when it is far, and None when
    nobody has measured it yet and it only just arrived: the probe runs within a minute of a
    worker joining, and a LAN machine must not be sent away as "far" in that gap.

    A ``settled`` worker, one whose heartbeat already says it is a coding member, gets no such
    grace: this coordinator may just have restarted (its memory of when it first heard of every
    worker, and of every measurement, is gone), and a member that has been serving must not be told
    "no assignment" and unload its model for the minute and a half that would take.
    """
    max_rtt = float(get_worker_limits().get("auto_swap_max_rtt_ms", 15.0))
    endpoint = f"{ip}:{port}"
    worker = {"worker_id": worker_id, "endpoint": endpoint, "class": benchmark.classify_endpoint(endpoint)}
    if _coding_worker_is_near(worker, max_rtt):
        return True
    if (not settled and registry.get_link_measurement(worker_id).get("rtt_min_ms") is None
            and time.time() - coding_router.first_seen(worker_id) < coding_members_lib.ROLE_GRACE_SECONDS):
        return None
    return False


def _coding_role_answer(worker_id: str, ip: str, port: Any, hardware: Dict[str, Any]) -> Dict[str, Any]:
    """``role`` and ``coding_tier`` for a worker's heartbeat answer (engine/coding_router.assign_role)."""
    is_gpu = _as_number(gpu_section(hardware).get("vram_total_gb", 0)) > 0 and not is_app_member(hardware)
    near = _coding_role_near(worker_id, ip, port, settled=hardware.get("pool") == "coding") if is_gpu else True
    role, tier = coding_members_lib.assign_role(hardware, near, _coding_ladder())
    return {"role": role, "coding_tier": tier}


def _member_response(served) -> Response:
    """A member's answer (engine/coding_router.Served) as the response to send."""
    response_headers = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
    if served.status == 200:
        response_headers["X-DLNE-Served"] = "member"
    if served.retry_after:
        response_headers["Retry-After"] = str(served.retry_after)
    if served.stream is not None:
        return StreamingResponse(served.stream, status_code=served.status, media_type=served.media_type,
                                 headers=response_headers, background=BackgroundTask(served.release))
    return Response(content=served.body, status_code=served.status, media_type=served.media_type,
                    headers=response_headers)


async def _route_to_member(request: Request, path: str) -> Optional[Response]:
    """
    The request's answer from a coding member, or None for the local engine to answer.

    Only a SEALED request (``{"dlne_e2e": ...}``, selected by ``X-DLNE-Route``) is ever sent to a
    member, passed on untouched; if its member cannot take it the answer is the re-route code, never
    another member or this engine (engine/coding_router.py). Every plain request, whatever it
    carries, is this coordinator's own engine's to answer: what a volunteer's machine is sent has
    always been encrypted on the way.
    """
    limits = get_worker_limits()
    try:
        raw = await request.body()
        # Cheap for the usual plain request: no need to parse a long conversation to find it is not sealed.
        parsed = json.loads(raw) if raw and b'"dlne_e2e"' in raw else None
    except Exception:  # noqa: BLE001 - not JSON: the local path gives its usual 400
        return None
    if parsed is None:
        return None
    reserve = 0 if path.endswith("/chat/completions") else coding_members_lib.COMPLETION_RESERVE_SLOTS
    async with engine_client() as client:
        served = await coding_router.serve(
            client=client, members_fn=lambda: _coding_members(limits), path=path, raw_body=raw, parsed=parsed,
            privacy=request.headers.get("X-DLNE-Privacy"), route_id=request.headers.get("X-DLNE-Route"),
            reserve=reserve, trusted_ids=limits.get("trusted_members"))
    return None if served is None else _member_response(served)


# Route lookups by free-trial callers: this many per window per client (SR-09).
TRIAL_ROUTE_LOOKUPS = 6
TRIAL_ROUTE_WINDOW_SECONDS = 30.0
coding_route_window = UsageWindow()


@app.get("/v1/coding/route")
async def coding_route(request: Request):
    """
    ``GET /v1/coding/route?session=<id>&privacy=<mode>``: a coding member for the client to seal
    its next request to (docs/coding-members.md, sections 5 and 6), or ``{"route": null}`` when the
    local engine should answer. The route id is opaque and good for 10 minutes; the client sends it
    back as ``X-DLNE-Route`` with the sealed body. Counts as a little load on the member until used.
    """
    no_store = {"Cache-Control": "no-store"}
    limits = get_worker_limits()
    if not limits.get("coding_members_enabled", False):
        return JSONResponse({"route": None}, headers=no_store)
    # A free-trial caller is any stranger: its lookups are limited per client (a few per 30 seconds) and
    # hold no member's slots (SR-09), so a flood of them can neither fill the route table nor keep agents out.
    trial = getattr(getattr(request, "state", None), "dlne_tier", None) == "trial"
    if trial:
        allowed, _remaining, reset_at = coding_route_window.check_and_record(
            trial_identity(get_client_ip(request)), TRIAL_ROUTE_LOOKUPS, TRIAL_ROUTE_WINDOW_SECONDS)
        if not allowed:
            wait = max(1, int(reset_at - time.time()))
            return JSONResponse({"detail": "Too many route lookups. Try again shortly."}, status_code=429,
                                headers={**no_store, "Retry-After": str(wait)})
    session = request.query_params.get("session")
    privacy = request.query_params.get("privacy")
    member = coding_router.pick(_coding_members(limits), session, privacy, need_pubkey=True)
    if member is None:
        return JSONResponse({"route": None}, headers=no_store)
    route = coding_router.issue_route(member, session, privacy, hold=not trial)
    return JSONResponse({"route": route, "pubkey": member.pubkey, "alg": coding_members_lib.E2E_ALG},
                        headers=no_store)


# ======================================================================================
# The CPU pool (phone app)
# ======================================================================================

def _app_members():
    from engine.reverse_tunnel import reverse_bridge
    return app_pool.members(registry, reverse_bridge.get_relay_port)


def _load_app_tiers() -> None:
    manifest = release_lib.load_manifest(os.path.join(get_base_dir(), release_lib.MANIFEST_NAME))
    app_pool.load_tiers(release_lib.app_tiers(manifest) if manifest else [])
    if app_pool.tiers:
        log_coordinator("CPU pool tiers: " + ", ".join(t.get("name") or t["id"] for t in app_pool.tiers) + ".")


async def monitor_app_pool_async() -> None:
    """
    Keeps the coordinator host's own CPU pool member serving the tier it should (the owner's
    APP_LOCAL_* allocation), restarting it if it dies or when a bigger tier unlocks.
    """
    limits = get_worker_limits()
    models_dir = limits.get("custom_gguf_dir") or os.path.join(get_base_dir(), "models")
    app_pool.local = LocalAppServer(
        models_dir=models_dir,
        binary_resolver=lambda: download_and_extract_engine("coordinator"),
        log_dir=os.path.join(get_base_dir(), "logs"),
        port=int(limits.get("app_local_port", 8099)),
        threads=int(limits.get("app_local_threads", 4)),
        ram_gb=float(limits.get("app_local_ram_gb", 8.0)),
        cpu_mask=limits.get("app_local_cpu_mask") or None,
    )
    while True:
        try:
            local = app_pool.local
            min_members = int(get_worker_limits().get("app_tier_min_members", 2))
            tier_id = app_pool.assignment(local.ram_gb, _app_members(), min_members)
            tier = next((t for t in app_pool.tiers if t["id"] == tier_id), None)
            before = (local.serving, local.process)
            await asyncio.to_thread(local.ensure, tier)
            if (local.serving, local.process) != before and local.process is not None:
                async with engine_client() as client:
                    if await local.wait_ready(client):
                        log_coordinator(f"CPU pool (local): ready, serving {tier.get('name')}.")
                    else:
                        log_error("CPU pool (local): the model server did not become ready; see logs/app_server.log.")
        except Exception as exc:  # noqa: BLE001 - the monitor must outlive any one failure
            log_error(f"CPU pool monitor: {type(exc).__name__}: {exc}")
        await asyncio.sleep(20)


@app.post("/v1/app/chat/completions")
async def app_chat_completions(request: Request):
    """
    The phone app's chat: answered by the CPU pool (engine/app_pool.py). Free for anyone,
    throttled per network address; never touches the GPU coding engine.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="The request body must be JSON.")
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list) or not body["messages"]:
        raise HTTPException(status_code=400, detail="Send a non-empty 'messages' list.")

    network = trial_identity(get_client_ip(request))
    limits = get_worker_limits()
    per_minute = int(limits.get("app_rate_per_minute", 6))
    allowed, why = app_pool.admit(network, per_minute)
    if not allowed:
        return JSONResponse({"detail": why}, status_code=429,
                            headers={"Retry-After": str(app_pool.retry_after(network, per_minute))})

    members = _app_members()
    active = app_pool.status(members, int(limits.get("app_tier_min_members", 2)))["active_tier"]
    tools, run_tool = _app_lookup_tools(network)

    async def generate():
        try:
            async with engine_client() as client:
                async for chunk in app_pool.stream(body, members, active, client, tools=tools,
                                                   run_tool=run_tool and functools.partial(run_tool, client=client)):
                    yield chunk
        finally:
            app_pool.finished(network)

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


def _app_search_status() -> Optional[Dict[str, Any]]:
    try:
        from engine import search
    except ImportError:
        return None
    return search.search_status()


def _app_lookup_tools(network: str):
    """The phone model's lookup tools and their runner (engine/search.py), or (None, None)
    when search is off or not installed."""
    try:
        from engine import search
    except ImportError:
        return None, None
    if not search.search_status().get("enabled"):
        return None, None
    return search.TOOLS, functools.partial(search.run_tool, network_id=network)


# Reports of phone answers (Play's policy for AI apps): kept on this machine for the operator.
APP_REPORTS_FILE = "app_reports.jsonl"
APP_REPORTS_MAX_BYTES = 50 * 1024 * 1024
APP_REPORT_REASONS = ("harmful", "wrong", "offensive", "other")
app_report_window = UsageWindow()


@app.post("/v1/app/report")
async def app_report(request: Request):
    """A phone user reports an answer. Appended to config/app_reports.jsonl; read with
    GET /v1/admin/app_reports. Throttled per network; the file is capped in size."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="The request body must be JSON.")
    if not isinstance(body, dict) or body.get("reason") not in APP_REPORT_REASONS:
        raise HTTPException(status_code=400, detail=f"reason must be one of: {', '.join(APP_REPORT_REASONS)}.")
    fields = {"question": 4000, "answer": 8000, "note": 500}
    report = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "reason": body["reason"]}
    for key, cap in fields.items():
        value = body.get(key, "")
        if not isinstance(value, str):
            raise HTTPException(status_code=400, detail=f"{key} must be text.")
        report[key] = value[:cap]
    if not report["answer"].strip():
        raise HTTPException(status_code=400, detail="Say which answer you are reporting.")

    network = trial_identity(get_client_ip(request))
    allowed, _remaining, reset_at = app_report_window.check_and_record(network, 10, 3600.0)
    if not allowed:
        wait = max(1, math.ceil(reset_at - time.time()))
        return JSONResponse({"detail": "Thanks; that's a lot of reports from here. Please try again later."},
                            status_code=429, headers={"Retry-After": str(wait)})
    path = os.path.join(get_base_dir(), "config", APP_REPORTS_FILE)

    def append() -> bool:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) > APP_REPORTS_MAX_BYTES:
            return False
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(report, ensure_ascii=False) + "\n")
        return True

    if not await asyncio.to_thread(append):
        log_warning("Phone answer reports: the file is full; read and clear config/app_reports.jsonl.")
    return {"ok": True}


@app.get("/v1/admin/app_reports")
async def admin_app_reports(request: Request, limit: int = 100):
    """The newest phone answer reports, for the operator."""
    _require_admin(request)
    path = os.path.join(get_base_dir(), "config", APP_REPORTS_FILE)

    def read() -> List[Dict[str, Any]]:
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()[-max(1, min(int(limit), 1000)):]
        return [json.loads(line) for line in lines if line.strip()]

    return {"reports": await asyncio.to_thread(read)}


@app.on_event("startup")
async def startup_event():
    """Starts the long-lived monitors once uvicorn's event loop is running."""
    global _auto_swap_task
    log_coordinator("Proactive Worker Tracking & Event Broadcaster is active.")
    _apply_rpc_requirement()
    spawn_background_task(monitor_workers_async(), "monitor-workers")
    spawn_background_task(monitor_models_json_async(), "monitor-models-json")
    log_coordinator("Coding model ladder: " + ", ".join(
        f"{t['name']} ({t['min_vram_gb']} GB{'' if t['enabled'] else ', off'})" for t in _coding_ladder()) + ".")
    # The saved speeds are read once, off the event loop, before the first status asks for them.
    spawn_background_task(asyncio.to_thread(coding_speed.meter.load), "coding-speed-load")
    _load_app_tiers()
    if get_worker_limits().get("app_local_enabled", False) and app_pool.tiers:
        spawn_background_task(monitor_app_pool_async(), "app-pool")
    # Automatic model switching for the coding pool (engine/auto_swap.py). It always runs and
    # reads AUTO_SWAP on every tick, so config.txt can turn it off or on without a restart.
    # The link probe measures how far each volunteer's GPU is, which decides whether it counts.
    _auto_swap_task = spawn_background_task(auto_swapper.run(), "auto-swap")
    _auto_swap_tasks.append(spawn_background_task(
        link_probe.run(_probe_targets, warn=lambda m: log_warning(safe_log_text(m, max_chars=300))),
        "auto-swap-link-probe"))
    # Canary requests to untrusted coding members (engine/canary.py). The members list is empty
    # while CODING_MEMBERS_ENABLED is off, so this does nothing until the owner turns members on.
    _auto_swap_tasks.append(spawn_background_task(
        canary.CanaryScheduler(_canary_members, _canary_forward, trusted=_canary_trusted).run(),
        "coding-canaries"))


@app.on_event("shutdown")
async def shutdown_event():
    """Stops automatic model switching (and any model download it started)."""
    auto_swapper.shutdown()
    for task in [_auto_swap_task, *_auto_swap_tasks]:
        if task is not None and not task.done():
            task.cancel()


# ======================================================================================
# Server entry point
# ======================================================================================

def start_coordinator_server(host: str = "0.0.0.0", port: int = 11435, is_promoted: bool = False):
    """Starts the FastAPI web server. Blocks until the server is asked to exit."""
    log_coordinator(f"Starting Coordinator API on {host}:{port}...")

    # Start the LAN Auto-Discovery Broadcaster.
    threading.Thread(target=udp_broadcaster, args=(port,), daemon=True).start()
    log_coordinator("LAN Auto-Discovery (UDP Broadcast) is active.")

    # Said once, at the only moment an operator is looking. A running tunnel interface whose
    # key differs from the one in coordinator_wg0.conf is the failure with no symptom: this
    # Coordinator hands the *configured* key to every node that enrolls, the interface
    # answers with the one it was started with, and every tunnel built against the mismatch
    # silently never handshakes. Diagnostic only, and it fails open on every uncertainty.
    try:
        from engine.wireguard import WireGuardOrchestrator

        WireGuardOrchestrator().warn_if_interface_diverged()
    except Exception:
        pass

    import uvicorn
    # Keep idle client connections for 75 s (uvicorn's default is 5 s): the extension reuses one
    # connection across a burst of autocomplete requests, which saves a handshake each time.
    config = uvicorn.Config(app, host=host, port=port, log_level="error", timeout_keep_alive=75)
    server = uvicorn.Server(config)

    def reclaim_listener():
        """Listens for the True Primary's return so a promoted node can step back down."""
        import hashlib
        import hmac

        admin_hash = get_worker_limits().get("admin_hash", "")
        if not admin_hash:
            return

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("", 11436))  # Port for Reclaim signals
        while True:
            try:
                data, addr = sock.recvfrom(1024)
                msg = data.decode()
                if msg.startswith("DLNE_RECLAIM:"):
                    parts = msg.split(":")
                    if len(parts) == 3:
                        timestamp = parts[1]
                        signature = parts[2]
                        if abs(time.time() - float(timestamp)) > 30:
                            continue
                        expected = hmac.new(admin_hash.encode(), timestamp.encode(), hashlib.sha256).hexdigest()
                        if hmac.compare_digest(expected, signature):
                            log_coordinator("VALID RECLAIM SIGNAL RECEIVED. True Primary returned!")
                            log_coordinator("Stepping down to Worker mode. Shutting down Coordinator API...")
                            server.should_exit = True
                            break
            except Exception:
                pass

    if is_promoted:
        threading.Thread(target=reclaim_listener, daemon=True).start()

    def send_reclaim():
        """Announces on boot that the True Primary is back, so any promoted node stands down."""
        import hashlib
        import hmac

        admin_hash = get_worker_limits().get("admin_hash", "")
        if not admin_hash:
            return

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        timestamp = str(time.time())
        signature = hmac.new(admin_hash.encode(), timestamp.encode(), hashlib.sha256).hexdigest()
        msg = f"DLNE_RECLAIM:{timestamp}:{signature}".encode()
        try:
            sock.sendto(msg, ('<broadcast>', 11436))
            log_coordinator("Ghost Primary Reclaimer: Sent Reclaim Signal to network.")
        except Exception:
            pass

    if not is_promoted:
        threading.Thread(target=send_reclaim, daemon=True).start()

    # Run server (Blocks until server.should_exit is True)
    #
    # Ctrl-C is a normal way to stop a node, and it must look like one. uvicorn installs
    # its own SIGINT handler, sets should_exit, and then re-raises KeyboardInterrupt as its
    # capture_signals context manager unwinds — so the exception escapes server.run() even
    # though the shutdown itself was orderly. Left uncaught it climbed out of main() and
    # PyInstaller printed a twelve-frame traceback ending in "Failed to execute script
    # 'main'", which reads like a crash to anyone who just pressed Ctrl-C.
    try:
        server.run()
    except KeyboardInterrupt:
        log_coordinator("Interrupt received — Coordinator API shutting down.")
    log_coordinator("Coordinator API successfully stepped down.")
