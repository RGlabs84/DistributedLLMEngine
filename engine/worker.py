"""
DLNE Worker Node - lends this machine's GPU/CPU to the cluster.

The node does three things:

1. Downloads (if necessary) and launches ``ggml-rpc-server``, the process that actually
   serves tensors to the Coordinator's ``llama-server``.
2. Heartbeats every 5 seconds with a *signed* statement of "who I am, where my RPC
   endpoint is, and what hardware I am currently willing to lend".
3. Shuts the node down cleanly the moment either of those becomes impossible.

TWO DESIGN POINTS TO KNOW BEFORE EDITING THIS FILE
--------------------------------------------------
**Shutdown has to cross a thread boundary.** ``heartbeat_loop`` always runs in a daemon
thread, and ``start()`` itself may run in a daemon thread too (``main.py`` starts it that
way for the hybrid and redundancy modes). ``sys.exit()`` raises ``SystemExit``, which
unwinds *only the thread that raised it* - so calling it from the heartbeat thread killed
the heartbeats while the rest of the process carried on forever with a dead engine, still
listed in the cluster registry as an available worker. Every fatal condition therefore
goes through :meth:`WorkerNode.request_shutdown`, which sets a ``threading.Event`` that
``start()``'s idle loop is waiting on. Only ``start()`` - and only when it is running on
the main thread - is allowed to call ``sys.exit()``.

**The heartbeat is authenticated.** Every heartbeat carries a timestamp, a fresh nonce and
an HMAC-SHA256 signature over the canonical string defined in ``engine/authn.py``. Without
them the Coordinator has no way to distinguish this node from anything else that can reach
its port, which previously allowed free cluster membership, forged hardware reports and
outright RPC hijacking.
"""

import json
import re
import time
import httpx
import subprocess
import threading
import sys
import os
from typing import Any, Dict, Optional, Tuple
from engine.logger import log_worker, log_error, log_system, log_warning
from engine.hardware import get_hardware_profile, _hidden_process_kwargs
from engine.downloader import download_and_extract_engine
from engine.version import VERSION
from engine.config import get_base_dir, get_worker_limits
from engine.authn import sign_heartbeat
from engine.release import is_older
from engine.companion import (
    ALSO_MARKER, COMPANION_SUFFIX, GATE_TIMEOUT_SECONDS, also_cpu_ram_gib, split_cpu_threads,
)

import secrets
import atexit


def _write_token_to_disk(token: str) -> None:
    targets = [
        os.path.join(get_base_dir(), "config", ".dlne_token"),
        os.path.join(os.path.expanduser("~"), ".dlne", "config", ".dlne_token"),
        os.path.join(os.path.expanduser("~"), ".dlne", ".dlne_token"),
    ]
    for target in targets:
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                f.write(token)
        except Exception:
            pass


def _remove_token_from_disk() -> None:
    targets = [
        os.path.join(get_base_dir(), "config", ".dlne_token"),
        os.path.join(os.path.expanduser("~"), ".dlne", "config", ".dlne_token"),
        os.path.join(os.path.expanduser("~"), ".dlne", ".dlne_token"),
    ]
    for target in targets:
        try:
            if os.path.exists(target):
                os.remove(target)
        except Exception:
            pass


# Do not delete token on atexit: keeping the last valid token on disk prevents
# spurious 403s during worker restarts and reload cycles (the Coordinator's registry
# already prunes dead workers after heartbeat timeouts and honors a grace window).
# atexit.register(_remove_token_from_disk)


# Seconds between heartbeats. The Coordinator's registry marks a worker dead after a small
# multiple of this value, so changing it here silently changes liveness detection for the
# whole cluster.
HEARTBEAT_INTERVAL_SECONDS = 5.0

# Seconds an ephemeral access token stays valid before it is regenerated and rewritten to
# config/.dlne_token (the Client Portal reads that file to prove local contribution).
TOKEN_ROTATION_SECONDS = 60.0

# How often start()'s idle loop wakes to re-check the shutdown flag. Kept at one second so
# Ctrl+C is still handled just as promptly as it was under the old time.sleep(1) loop.
SHUTDOWN_POLL_SECONDS = 1.0

# Exit code used for fatal conditions that do not carry a code of their own (a rejected
# version, rejected credentials, or an engine binary we could not obtain).
FATAL_EXIT_CODE = 1

# Exit code meaning "the coordinator runs a different engine version than this node". It is
# not a failure to report to a supervisor: it asks to be started again after the engine has
# been brought in line. The VS Code extension does exactly that (it re-fetches the verified
# engine from the coordinator, then restarts the worker). 75 is EX_TEMPFAIL in sysexits.h.
VERSION_MISMATCH_EXIT_CODE = 75

# Exit code meaning "the coordinator will not admit this machine as a CPU-only node" (no
# usable GPU, and below its CPU_MIN_* floors). Retrying cannot help: the hardware is what it
# is. The worker prints the reason on a line starting REFUSAL_MARKER, which the VS Code
# extension shows the user. 78 is EX_CONFIG in sysexits.h.
CPU_BELOW_MINIMUM_EXIT_CODE = 78
REFUSAL_MARKER = "DLNE_REFUSAL: "

# The job this machine has, printed for the VS Code extension the way REFUSAL_MARKER is
# (docs/coding-members.md section 10): "DLNE_ROLE: rpc", "DLNE_ROLE: app" or
# 'DLNE_ROLE: coding {"model": ..., "tier": ..., "slots": 2, "trust_id": "<32 hex>"}'. One line of compact
# JSON with names and counts only; the last line printed wins, and a new one follows every change.
ROLE_MARKER = "DLNE_ROLE: "

# How long a node that is AHEAD of its coordinator waits between heartbeats. The coordinator
# is probably mid-restart onto the same version; hammering it helps nobody.
AHEAD_OF_COORDINATOR_RETRY_SECONDS = 30.0

# The roles the coordinator can give a node in a heartbeat answer ("role"): an RPC node (a share
# of the big pipeline), a CPU pool member (phone app) or a coding member (a whole coding model).
POOL_ROLES = ("rpc", "coding", "app")

# After a role that could not start, the coordinator asking for it again is ignored for this long.
ROLE_RETRY_SECONDS = 300.0


def _vram_cap_gib(limits: Dict[str, Any]) -> Optional[float]:
    """The owner's MAX_VRAM_GB as one number of GiB ("24,8" lends 32 in all), or None for no cap."""
    raw = str(limits.get("max_vram_gb", "-1")).strip()
    if raw in ("", "-1"):
        return None
    try:
        return sum(float(part.strip()) for part in raw.split(","))
    except ValueError:
        return None


def _coding_ram_cap_gib(limits: Dict[str, Any]) -> Optional[float]:
    """
    The GiB of system RAM the CODING job may use, or None for no cap. CODING_MAX_RAM_GB when config.txt
    has it (-1 means no cap: the job then counts only the RAM that is free), otherwise MAX_RAM_GB, which an
    older extension wrote alone. MAX_RAM_GB stays what the GPU's other job (an RPC node) and the phone pool
    are held to; the extension's default for it is small and was never meant for a whole coding model.
    """
    def number(value: Any) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    coding = limits.get("coding_max_ram_gb")
    cap = coding if number(coding) else limits.get("max_ram_gb", -1.0)
    return float(cap) if number(cap) and cap >= 0 else None


def _response_detail(response: httpx.Response) -> str:
    """
    Best-effort extraction of FastAPI's ``{"detail": ...}`` message from an error response.

    Parsing is guarded because this runs on the *failure* path: a Coordinator sitting
    behind a reverse proxy can answer with an HTML error page, and an unguarded
    ``response.json()`` would turn a clear "your build is out of date" message into a
    JSONDecodeError swallowed by the outer handler. Falls back to a truncated body so the
    operator still gets something to act on.
    """
    try:
        body = response.json()
        if isinstance(body, dict) and "detail" in body:
            return str(body["detail"])
    except Exception:
        pass
    try:
        return response.text.strip()[:200]
    except Exception:
        return ""


# Answers per binary path, because the probe spawns a process and the answer cannot change
# for a given file. A node that restarts its RPC server repeatedly must not pay for it.
_RPC_CACHE_SUPPORT: Dict[str, bool] = {}
_RPC_CACHE_SUPPORT_LOCK = threading.Lock()


def _rpc_server_supports_cache(binary_path: str) -> bool:
    """
    Whether this ggml-rpc-server accepts ``--cache``, asked of the binary itself.

    Not assumed from the pinned llama.cpp version. The engine binary is downloaded at run
    time and an operator can point DLNE at their own build, so the only trustworthy source
    for what it accepts is its own ``--help``. Passing a flag an older build does not know
    makes it exit immediately, which would turn a performance improvement into a node that
    refuses to start — the one outcome not worth any speed at all.

    Never raises: an unanswerable probe means "assume not supported" and the node runs
    exactly as it did before the cache existed.
    """
    with _RPC_CACHE_SUPPORT_LOCK:
        if binary_path in _RPC_CACHE_SUPPORT:
            return _RPC_CACHE_SUPPORT[binary_path]

    supported = False
    try:
        result = subprocess.run(
            [binary_path, "--help"], capture_output=True, text=True, timeout=30, **_hidden_process_kwargs()
        )
        # Exit status is not consistent across these tools and both streams are used for
        # usage text depending on the build, so the output is what is trusted.
        supported = "--cache" in f"{result.stdout or ''}{result.stderr or ''}"
    except Exception:
        supported = False

    with _RPC_CACHE_SUPPORT_LOCK:
        _RPC_CACHE_SUPPORT[binary_path] = supported
    return supported


_RPC_BANNER_RE = re.compile(r"Starting RPC server v(\d+\.\d+\.\d+)")


def read_rpc_banner(log_path: str, offset: int, process=None, timeout: float = 10.0,
                    poll: float = 0.1) -> str:
    """
    The RPC protocol version ggml-rpc-server announces as it starts ("Starting RPC server
    v8.0.1"), read from its log after ``offset``; "" if it never says (an old build, or a
    process that died). The binary has no --version, and the protocol is what decides
    whether a Coordinator's llama-server can use this node at all.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
                handle.seek(offset)
                match = _RPC_BANNER_RE.search(handle.read(64 * 1024))
            if match:
                return match.group(1)
        except OSError:
            pass
        if time.monotonic() >= deadline or (process is not None and process.poll() is not None):
            return ""
        time.sleep(poll)


class WorkerNode:
    def __init__(self, coordinator_url: str, rpc_ip: str = "0.0.0.0", rpc_port: int = 50052,
                 verbose_heartbeats: bool = True, role: str = "worker", companion_of: str = "",
                 companion_ram_gb: float = 0.0, companion_threads: int = 0,
                 companion_gate: Optional[threading.Event] = None):
        self.coordinator_url = coordinator_url
        # "Both" (engine/companion.py): a companion node is the CPU half of a GPU machine, a second worker
        # identity in the same process that serves the phone pool with ``companion_ram_gb`` of RAM and
        # ``companion_threads`` threads. ``companion_gate`` is opened when its model has settled, so the GPU
        # node's coding model loads after it, not at the same moment. All empty/zero on every ordinary node.
        self.companion_of = companion_of
        self.companion_ram_gb = companion_ram_gb
        self.companion_threads = companion_threads
        self.companion_gate = companion_gate
        self._companion_started = time.monotonic()
        self.hardware_profile = self._probe_hardware()
        self.rpc_process = None
        # The RPC protocol the running ggml-rpc-server announced, e.g. "8.0.1" ("" = unknown).
        self.rpc_proto = ""
        # Set when this node serves the CPU pool (no usable GPU) instead of running an RPC
        # engine: engine/app_member.py.
        self.app_member = None
        # Set when this node serves the coding pool: a whole coding model on its own GPU,
        # engine/coding_member.py. At most one of rpc_process, app_member and coding_member runs.
        self.coding_member = None
        # The CPU half of a "both" machine (a companion WorkerNode), the gate its model opens, the thread split
        # between the two jobs and the RAM lent to the phone pool. None / 0 on a node that is not running both.
        self._companion: Optional["WorkerNode"] = None
        self._companion_gate: Optional[threading.Event] = None
        self._thread_split: Optional[Tuple[int, int]] = None
        self._companion_lent = 0.0
        self._role_lock = threading.Lock()
        self._role_busy = False
        self._role_target: Optional[str] = None
        self._role_hold: Dict[str, float] = {}
        self._latest_answer: Dict[str, Any] = {}
        self._announced = ""        # the last DLNE_ROLE line printed
        self._coding_refused_logged = False
        self.is_running = False
        self.rpc_ip = rpc_ip
        self.rpc_port = rpc_port
        self.verbose_heartbeats = verbose_heartbeats
        self.access_token = ""
        self.last_token_rotation = 0.0

        # Cluster role, signed into every heartbeat (wire v3). The rollout machinery on
        # the primary builds its wait-set from this — a redundancy node that mislabels
        # itself "worker" opts out of the pre-stage protection it exists to provide.
        self.role = role if role in ("worker", "redundancy", "hybrid") else "worker"

        # Part of the signed heartbeat (wire format v3) and always empty now: staged updates
        # belonged to the retired self-updater. Kept so the signed canonical string, which
        # both ends build, does not change.
        self._staged_version = ""

        # --- Cross-thread shutdown plumbing -------------------------------------------
        # The heartbeat thread can discover fatal conditions (dead engine, rejected build,
        # rejected credentials) but it cannot terminate the process itself. It sets this
        # Event; start()'s idle loop waits on it, tears the node down and produces the
        # exit code. exit_code is public so an embedder can inspect why start() returned.
        self._shutdown_event = threading.Event()
        self.exit_code = 0
        self._shutdown_lock = threading.Lock()

        # stop() is reachable from three directions (start()'s teardown, atexit, and an
        # embedder). This makes the second and third calls cheap no-ops instead of a
        # second round of terminate()/wait() and duplicate log lines.
        self._stopped = False

        # Latch for the "this node is running unauthenticated" warning, so an empty
        # NETWORK_KEY produces one clear line at startup rather than one every 5 seconds.
        self._unsigned_warning_logged = False

        atexit.register(self.stop)

        # Load or generate stable Worker ID. The implementation lives in setup_core because
        # tunnel enrollment runs during setup, before any WorkerNode exists, and has to
        # present the same identity this node will heartbeat under.
        from engine.setup_core import get_or_create_worker_id

        self.worker_id = get_or_create_worker_id()
        if self.companion_of:
            self.worker_id = f"{self.companion_of}{COMPANION_SUFFIX}"

    # ----------------------------------------------------------------------------------
    # The hardware this node reports
    # ----------------------------------------------------------------------------------

    def _probe_hardware(self) -> Dict[str, Any]:
        """The hardware profile of this node. A companion reports the CPU half of the machine only: no GPU, the RAM
        it lends, its memory bandwidth (a CPU-only node is admitted on it), and ``companion_of``, which is how the
        coordinator counts the machine once. Every other node reports exactly what the probe found."""
        profile = get_hardware_profile()
        if not self.companion_of or not isinstance(profile, dict):
            return profile
        from engine.hardware import get_memory_bandwidth_gbs

        profile = dict(profile)
        profile["gpu"] = {"has_gpu": False, "vram_total_gb": 0, "vram_free_gb": 0, "name": None, "load_percent": 0.0}
        profile["gpu_type"] = "cpu"
        profile["ram_gb"] = round(self.companion_ram_gb, 1)
        profile["companion_of"] = self.companion_of
        bandwidth = get_memory_bandwidth_gbs()
        if bandwidth is not None:
            profile["mem_bandwidth_gbs"] = bandwidth
        return profile

    # ----------------------------------------------------------------------------------
    # Shutdown coordination
    # ----------------------------------------------------------------------------------

    def request_shutdown(self, exit_code: int = 0, reason: str = "") -> None:
        """
        Ask the node to shut down, from any thread.

        This is the *only* correct way for the heartbeat thread to end the node. It records
        the exit code, clears ``is_running`` (which ends the heartbeat loop) and wakes
        ``start()``, which owns teardown and, when it is on the main thread, the actual
        process exit.

        Args:
            exit_code: Process exit status to finish with. ``0`` means "this was a normal,
                requested shutdown". The first caller wins: a later graceful request must
                not overwrite the failure code of whatever went wrong first.
            reason: Short human-readable cause, logged once.
        """
        with self._shutdown_lock:
            first_request = not self._shutdown_event.is_set()
            if first_request:
                self.exit_code = int(exit_code)

            # Order matters: clear the run flag *before* setting the Event, so the
            # heartbeat loop can never observe "shutdown signalled but still running".
            self.is_running = False
            self._shutdown_event.set()

        # Logged outside the lock - the logger writes to a rotating file, and holding a
        # lock across file I/O is how a shutdown path turns into a deadlock.
        if first_request and reason:
            log_worker(f"Shutdown requested: {reason}")

    # ----------------------------------------------------------------------------------
    # Cluster authentication
    # ----------------------------------------------------------------------------------

    def _get_network_key(self) -> str:
        """
        Returns the pre-shared cluster secret, warning once if there is not one.

        Read fresh (from the cached config) on every heartbeat rather than captured at
        construction time, so an operator who pastes a key into config.txt while the node
        is running starts signing on the next beat instead of after a restart.

        An empty key is not treated as an error: the node still heartbeats, with
        ``signature: ""``, so that a cluster migrating to authentication can run with
        ``ALLOW_UNSIGNED_WORKERS=true`` until every node has the key. The Coordinator, not
        the worker, decides whether an unsigned heartbeat is acceptable.
        """
        try:
            key = get_worker_limits().get("network_key", "") or ""
        except Exception as e:
            # A config read failure must not kill the heartbeat; degrade to unsigned and
            # let the Coordinator's policy reject us if that is not allowed.
            log_error(f"Could not read NETWORK_KEY from config: {e}")
            key = ""

        key = str(key).strip()

        if not key and not self._unsigned_warning_logged:
            self._unsigned_warning_logged = True
            log_warning(
                "NETWORK_KEY is not set - this node's heartbeats are UNSIGNED. It will only "
                "be accepted by a Coordinator running with ALLOW_UNSIGNED_WORKERS=true. Ask "
                "your admin for an invite file to secure this node."
            )

        return key

    def _build_heartbeat_payload(self) -> dict:
        """
        Assembles one signed heartbeat body.

        The JSON field names are part of the wire contract with the Coordinator's
        ``HeartbeatPayload`` model and must not be renamed. ``timestamp``, ``nonce`` and
        ``signature`` are what make the body unforgeable:

        * ``timestamp`` bounds how long a captured heartbeat stays usable (the Coordinator
          rejects anything outside a 30 s skew window),
        * ``nonce`` is fresh per beat so the Coordinator can refuse an exact replay inside
          that window,
        * ``signature`` is HMAC-SHA256 over *every other field in this body* -
          worker_id, port, token, timestamp, nonce, version and a digest of hardware - so
          none of them can be altered in flight.

        ``version`` and ``hardware`` joined the signed set in wire format v2. Under v1 they
        travelled unsigned over plain HTTP, which meant an on-path attacker could rewrite
        the hardware profile to poison the cluster's memory pool, or corrupt ``version`` to
        make the Coordinator answer 426 and this node shut itself down. The profile is
        signed as a digest rather than inline so the canonical string stays bounded; see
        ``engine.authn.hardware_digest``.

        The profile is captured into a local first and the *same* object is both signed and
        sent. Reading ``self.hardware_profile`` twice would let the heartbeat thread's own
        refresh land between the two reads and produce a signature for a profile that was
        never transmitted - a 401 that no log line would explain.

        The timestamp is generated here, immediately before the POST, to spend as little of
        the skew window as possible on our own processing.
        """
        timestamp = time.time()
        nonce = secrets.token_hex(16)
        hardware = self.hardware_profile
        if isinstance(hardware, dict) and self.rpc_proto:
            # Inside the signed profile, so the protocol a node claims cannot be rewritten in
            # flight; the Coordinator routes only to nodes that speak its own major.
            hardware = {**hardware, "rpc_proto": self.rpc_proto}
        app_member = getattr(self, "app_member", None)
        coding_member = getattr(self, "coding_member", None)
        if isinstance(hardware, dict):
            # Whether this machine may be a coding member (the owner allows it, CODING_MEMBER_ALLOWED, and it
            # has a GPU): the coordinator assigns the job only to nodes that say yes.
            hardware = {**hardware, "coding_ok": not self._coding_refusal()}
        if coding_member is not None:
            # The owner's own load (a game, a render) makes the member stop at once; see CodingMember.set_yielding.
            set_yielding = getattr(coding_member, "set_yielding", None)
            if callable(set_yielding):
                set_yielding(isinstance(hardware, dict) and hardware.get("is_yielding") is True)
        if isinstance(hardware, dict) and coding_member is not None:
            # A coding member says so, and what it serves, inside the signed profile.
            hardware = {**hardware, "pool": "coding", "coding": coding_member.status()}
        elif isinstance(hardware, dict) and app_member is not None:
            # A CPU pool member says so, and what it serves, inside the signed profile.
            hardware = {**hardware, "pool": "app", "app": app_member.status()}
        # Captured once for the same both-signed-and-sent reason as the profile: the
        # staging thread updates this the moment an update finishes verifying.
        staged_version = self._staged_version

        signature = sign_heartbeat(
            self._get_network_key(),
            self.worker_id,
            self.rpc_port,
            self.access_token,
            timestamp,
            nonce,
            VERSION,
            hardware,
            self.role,
            staged_version,
        )

        return {
            "worker_id": self.worker_id,
            "version": VERSION,
            "port": self.rpc_port,
            "token": self.access_token,
            "hardware": hardware,
            "timestamp": timestamp,
            "nonce": nonce,
            "signature": signature,
            "role": self.role,
            "staged_version": staged_version,
        }

    # ----------------------------------------------------------------------------------
    # RPC engine subprocess
    # ----------------------------------------------------------------------------------

    # ----------------------------------------------------------------------------------
    # CPU pool member (no usable GPU)
    # ----------------------------------------------------------------------------------

    def _serves_cpu_pool(self) -> bool:
        """
        A node with no usable GPU serves the phone app's CPU pool instead of joining the 14B
        pipeline (the owner's tiers: GPU = coding, CPU = phone app). Never the coordinator
        host's own hybrid worker: its pool member is run by the coordinator itself.
        """
        if self.role == "hybrid":
            return False
        gpu = self.hardware_profile.get("gpu") if isinstance(self.hardware_profile, dict) else None
        return not (isinstance(gpu, dict) and gpu.get("has_gpu"))

    def start_app_member(self, keep_port: bool = False) -> bool:
        from engine import release
        from engine.app_member import APP_PORT, AppMember, default_threads
        from engine.downloader import fetch_verified_manifest
        from engine.hardware import get_cpu_cores

        # Same signed engine zip as an RPC node; llama-server ships in it beside ggml-rpc-server.
        download_and_extract_engine(
            "worker", coordinator_url=None if self.role == "hybrid" else self.coordinator_url)

        cache: Dict[str, Any] = {"at": 0.0, "tiers": []}

        def tiers() -> list:
            # The tier list comes from the coordinator's signed release, re-read at most
            # every ten minutes.
            if time.time() - cache["at"] > 600:
                cache["tiers"] = release.app_tiers(fetch_verified_manifest(self.coordinator_url.rstrip("/")))
                cache["at"] = time.time()
            return cache["tiers"]

        threads = int(get_worker_limits().get("app_member_threads", 0)) or default_threads(get_cpu_cores())
        if self.companion_of and self.companion_threads > 0:
            threads = self.companion_threads      # its share of the machine's threads, next to the coding model's
        # The heartbeat names this port, and the tunnel carries the coordinator to it. A node that
        # joined as something else keeps the port its tunnel already dials.
        port = self.rpc_port if keep_port else APP_PORT
        self.app_member = AppMember(get_base_dir(), tiers, threads=threads, port=port)
        self.rpc_port = port
        if self.companion_of:
            log_worker(f"This machine's CPU also serves the phone app's pool, with {self.companion_ram_gb:g} GB of RAM and "
                       f"{threads} threads, next to its GPU's own job. The coordinator assigns the model with the first heartbeat.")
        else:
            log_worker("No usable GPU: this node serves the phone app's CPU pool. "
                       "The coordinator assigns its model with the first heartbeat.")
        return True

    # ----------------------------------------------------------------------------------
    # Coding member (a whole coding model on this machine's GPU), engine/coding_member.py
    # ----------------------------------------------------------------------------------

    def start_coding_member(self) -> bool:
        """
        Opens the member proxy on this node's RPC port. The coordinator names the model with a
        heartbeat answer; nothing is downloaded or started before that. False when the proxy
        cannot listen (the node then carries on in its previous role).
        """
        from engine import release
        from engine.coding_member import CodingMember
        from engine.downloader import fetch_verified_manifest

        # llama-server ships in the signed engine zip beside ggml-rpc-server, as for the CPU pool.
        download_and_extract_engine(
            "worker", coordinator_url=None if self.role == "hybrid" else self.coordinator_url)

        cache: Dict[str, Any] = {"at": 0.0, "tiers": []}

        def tiers() -> list:
            # The ladder comes from the coordinator's signed release, re-read at most every ten
            # minutes; it is what a tier named in a heartbeat answer is checked against.
            if time.time() - cache["at"] > 600:
                cache["tiers"] = release.coding_tiers(fetch_verified_manifest(self.coordinator_url.rstrip("/")))
                cache["at"] = time.time()
            return cache["tiers"]

        threads = get_worker_limits().get("max_cpu_threads", -1)
        member = CodingMember(
            get_base_dir(), tiers, port=self.rpc_port,
            threads=threads if isinstance(threads, int) and threads > 0 else 0,
            env_provider=lambda: self._gpu_environment()[0],
            machine_provider=self._machine_info,
            limits_provider=get_worker_limits,
            resources_provider=self._coding_resources,
            start_gate=self._companion_gate,
        )
        if not member.start():
            return False
        with self._role_lock:
            if self._stopped or self._shutdown_event.is_set():
                member.stop()
                return False
            self.coding_member = member
        log_worker("This node serves the coding pool: a whole coding model on its own GPU. "
                   "The coordinator assigns the model with a heartbeat answer.")
        return True

    # Hardware whose GPU shares the CPU's memory: Apple Silicon and AMD Strix Halo (the names engine/hardware.py allows).
    _UNIFIED_MEMORY = re.compile(r"\bapple\b|radeon(?:\W|tm)*80[456]0s\b|gfx1151|strix[ _]halo|ryzen\W*ai\W*max")

    def _machine_info(self) -> Dict[str, Any]:
        """
        What this machine can spare for a coding model, as engine/coding_member_select.py wants it:
        the GPU's memory, the RAM that is really free right now (not the total), and ``unified`` for
        a GPU that shares the CPU's memory (then ``ram_gb`` is that one pool). The owner's limits
        cap both figures, and ``limited`` says that one of them did: MAX_VRAM_GB for the GPU memory, and for
        the RAM CODING_MAX_RAM_GB (-1: none, the figure is just what is free right now) or, when config.txt
        lacks it, MAX_RAM_GB. MAX_RAM_GB itself, and what the RPC node and the phone pool report, are not
        touched by the coding job's cap.
        """
        hardware = self.hardware_profile if isinstance(self.hardware_profile, dict) else {}
        gpu = hardware.get("gpu") if isinstance(hardware.get("gpu"), dict) else {}
        vram = gpu.get("vram_total_gb", 0)
        ram = hardware.get("ram_gb", 0)
        try:
            import psutil
            ram = psutil.virtual_memory().available / 2 ** 30
        except Exception:
            pass
        if self._companion_gate is not None and not self._companion_gate.is_set() and isinstance(ram, (int, float)):
            # "Both": the phone-pool model has not been loaded yet and is about to take its RAM; the coding model
            # is chosen for what will be left, and measures the real figure again once the pool model has loaded.
            ram = max(0.0, ram - self._companion_lent)
        limits = get_worker_limits()
        limited = False
        vram_cap = _vram_cap_gib(limits)
        if vram_cap is not None and isinstance(vram, (int, float)) and vram > vram_cap:
            vram, limited = vram_cap, True
        ram_cap = _coding_ram_cap_gib(limits)
        if ram_cap is not None and isinstance(ram, (int, float)) and ram > ram_cap:
            ram, limited = ram_cap, True
        info = {"vram_gb": vram, "ram_gb": ram,
                "unified": bool(self._UNIFIED_MEMORY.search(str(gpu.get("name") or "").lower()))}
        if limited:
            info["limited"] = True
        return info

    def _coding_resources(self) -> Dict[str, Any]:
        """
        The owner's limits as llama-server launch settings, read at every start of the coding model so an
        edit of config.txt takes effect: MAX_CPU_THREADS caps its threads (0 means one, never "all"),
        MAX_VRAM_GB becomes ``--fit-target`` margins so ``--fit`` leaves the rest of each card alone, and a
        coding RAM cap that binds (CODING_MAX_RAM_GB, else MAX_RAM_GB; none by default) shrinks the prompt
        cache that lives in system memory. The model itself is chosen to fit the capped figures
        (``_machine_info``).
        """
        from engine.coding_member import LIMITED_PROMPT_CACHE_MIB, fit_targets_mib

        limits = get_worker_limits()
        resources: Dict[str, Any] = {"threads": 0, "extra_args": []}
        threads = limits.get("max_cpu_threads", -1)
        if isinstance(threads, int) and not isinstance(threads, bool) and threads >= 0:
            resources["threads"] = max(1, threads)
        if self._thread_split is not None:
            resources["threads"] = self._thread_split[1]      # "both": the threads left after the phone pool's share
        cap = _vram_cap_gib(limits)
        if cap is not None:
            margins = fit_targets_mib(self._gpu_totals_gib(limits), cap)
            if margins:
                resources["extra_args"] = ["--fit-target", ",".join(str(m) for m in margins)]
        ram_cap = _coding_ram_cap_gib(limits)
        if ram_cap is not None:
            try:
                import psutil
                if ram_cap < psutil.virtual_memory().total / 2 ** 30:
                    resources["cache_ram_mib"] = LIMITED_PROMPT_CACHE_MIB
            except Exception:
                resources["cache_ram_mib"] = LIMITED_PROMPT_CACHE_MIB
        return resources

    @staticmethod
    def _gpu_totals_gib(limits: Dict[str, Any]) -> list:
        """GPU memory per card the coding model may see, in the order llama-server numbers the cards
        (the owner's ALLOWED_GPU_INDICES, integrated GPUs that engine/hardware.py does not count left out)."""
        from engine import hardware

        allowed = str(limits.get("allowed_gpu_indices", "-1")).strip()
        if allowed.lower() == "none":
            return []
        cards = list(hardware.get_all_gpus() or [])
        if allowed != "-1":
            by_id = {card.get("id"): card for card in cards}
            cards = [by_id[int(x)] for x in allowed.split(",") if x.strip().isdigit() and int(x) in by_id]
        cards = [c for c in cards if hardware.counts_as_gpu(c.get("name") or "", c.get("integrated"))]
        return [c.get("vram_total_gb", 0) for c in cards]

    def _coding_allowed(self) -> bool:
        """The volunteer's switch (CODING_MEMBER_ALLOWED in config.txt, on by default)."""
        try:
            return bool(get_worker_limits().get("coding_member_allowed", True))
        except Exception:
            return True

    def _coding_refusal(self) -> str:
        """Why this node may not be a coding member, or "" when it may: the owner said no, or there is no
        usable GPU (a CPU-only machine is a phone-pool member; a whole coding model needs a GPU)."""
        if not self._coding_allowed():
            return "CODING_MEMBER_ALLOWED is false here"
        if self._serves_cpu_pool():
            return "this machine has no usable GPU"
        return ""

    # ----------------------------------------------------------------------------------
    # Roles: RPC node, CPU pool member or coding member - never two at once
    # ----------------------------------------------------------------------------------

    # ----------------------------------------------------------------------------------
    # "Both": the CPU of a GPU machine also serves the phone pool (engine/companion.py)
    # ----------------------------------------------------------------------------------

    def _plan_companion(self) -> None:
        """
        Whether this GPU node's CPU also serves the phone pool, decided from ALSO_CPU_RAM_GB (the RAM it lends) and
        MAX_CPU_THREADS. Sets the thread split between the two jobs, the RAM the pool model is about to take and the
        gate the coding model waits on. Nothing for a node without a GPU (it already is the phone pool's CPU
        member, "both" acts as "cpu" there), for the coordinator's own hybrid worker, or for a companion.
        """
        if self.companion_of or self.role == "hybrid" or self._serves_cpu_pool():
            return
        limits = get_worker_limits()
        lent = also_cpu_ram_gib(limits)
        if lent is None:
            return
        from engine.hardware import get_cpu_cores

        split = split_cpu_threads(limits.get("max_cpu_threads", -1), get_cpu_cores())
        if split is None:
            log_worker("MAX_CPU_THREADS leaves fewer than 2 threads, too few for the CPU to serve the phone pool "
                       "next to the GPU's own job; the GPU does its job alone.")
            return
        self._thread_split, self._companion_lent, self._companion_gate = split, lent, threading.Event()

    def _launch_companion(self) -> None:
        if self._companion_gate is None or self._thread_split is None:
            return
        threading.Thread(target=self._run_companion, name="dlne-companion", daemon=True).start()

    def _run_companion(self) -> None:
        """The companion node, on a thread of its own: the same process, a second worker identity, one job."""
        gate = self._companion_gate
        try:
            companion = WorkerNode(
                self.coordinator_url, rpc_ip=self.rpc_ip, verbose_heartbeats=False, role="worker",
                companion_of=self.worker_id, companion_ram_gb=self._companion_lent,
                companion_threads=self._thread_split[0] if self._thread_split else 0, companion_gate=gate,
            )
            with self._role_lock:
                if self._stopped:
                    return
                self._companion = companion
            companion.start()
        except Exception as exc:  # noqa: BLE001 - the GPU's own job goes on without it
            log_error(f"The phone-pool half of this machine stopped: {type(exc).__name__}: {exc}")
        finally:
            if gate is not None:
                gate.set()          # a pool model that will not come must not hold the coding model back
            if not self._stopped:
                print(ALSO_MARKER + "none", flush=True)

    def _default_pool(self) -> str:
        """The role a node starts in: the CPU pool without a usable GPU, otherwise an RPC node."""
        return "app" if self._serves_cpu_pool() else "rpc"

    def _current_pool(self) -> str:
        if self.coding_member is not None:
            return "coding"
        if self.app_member is not None:
            return "app"
        return "rpc"

    def _announce_role(self) -> None:
        """Print the DLNE_ROLE line for the job this node has now, if it differs from the last one printed.
        Cheap, so it is called at start, after every role switch and with every heartbeat answer (the
        model a coding member serves is only known after the coordinator has named a tier)."""
        if self.companion_of:
            # A companion is the CPU half of a "both" machine: it says so with its own marker and never touches
            # DLNE_ROLE, which is the GPU's job.
            with self._role_lock:
                if self._announced:
                    return
                self._announced = ALSO_MARKER + "app"
            print(self._announced, flush=True)
            return
        pool = self._current_pool()
        detail: Dict[str, Any] = {}
        if pool == "coding":
            reporter = getattr(self.coding_member, "role_detail", None)
            detail = reporter() if callable(reporter) else {}
        line = ROLE_MARKER + pool
        if detail:
            line += " " + json.dumps(detail, separators=(",", ":"))      # ASCII only, one line
        with self._role_lock:
            if line == self._announced:
                return
            self._announced = line
        print(line, flush=True)

    def _follow_assignment(self, answer: Any) -> None:
        """
        What a heartbeat answer asks of this node. ``role`` ("rpc", "coding", "app" or null for
        "stay as you are") switches the node over in the background; ``coding_tier`` and
        ``app_model`` tell the running member which model to serve.
        """
        if not isinstance(answer, dict):
            return
        self._latest_answer = answer
        if self.companion_of:
            # The CPU half of a "both" machine has one job, the phone pool's: it takes its model and nothing else
            # (the GPU node's role is the GPU node's business), and tells the GPU node's coding model when it is up.
            if self.app_member is not None:
                self.app_member.assign(answer.get("app_model"))
                if self.companion_gate is not None and not self.companion_gate.is_set() and (
                        self.app_member.settled() or time.monotonic() - self._companion_started > GATE_TIMEOUT_SECONDS):
                    self.companion_gate.set()
            return
        role = answer.get("role")
        refusal = self._coding_refusal()
        if refusal:
            # The owner has switched coding off, or this machine has no GPU to lend: the job is refused quietly
            # (no hold-off, nothing to retry) and a node that already has it goes back to the role it would
            # start in.
            if role == "coding":
                role = None
                if not self._coding_refused_logged:
                    self._coding_refused_logged = True
                    log_worker(f"The coordinator offered the coding job, but {refusal}; ignored.")
            if self.coding_member is not None and self.role != "hybrid":
                self._switch_role(self._default_pool())
        else:
            self._coding_refused_logged = False
        if (role in POOL_ROLES and self.role != "hybrid" and role != self._current_pool()
                and time.monotonic() >= self._role_hold.get(role, 0.0)):
            self._switch_role(role)
        # A coding tier moves the member; an answer without one ("no assignment", which a coordinator that
        # has just restarted sends for a while) leaves it serving what it serves. Stopping a coding member is
        # a role change.
        tier = answer.get("coding_tier")
        if self.coding_member is not None and tier is not None:
            self.coding_member.assign(tier)
        if self.app_member is not None:
            self.app_member.assign(answer.get("app_model"))

    def _switch_role(self, target: str) -> None:
        with self._role_lock:
            self._role_target = target
            if self._role_busy or self._stopped:
                return
            self._role_busy = True
        threading.Thread(target=self._role_worker, name="dlne-role-switch", daemon=True).start()

    def _role_worker(self) -> None:
        try:
            while not self._shutdown_event.is_set():
                with self._role_lock:
                    target = self._role_target
                if target is None or target == self._current_pool() or time.monotonic() < self._role_hold.get(target, 0.0):
                    return
                self._become(target)
        except Exception as exc:  # noqa: BLE001 - a role switch must never take the heartbeat down with it
            log_error(f"Switching roles failed: {type(exc).__name__}: {exc}")
        finally:
            with self._role_lock:
                self._role_busy = False

    def _become(self, target: str) -> None:
        """
        Stop the role this node has and start ``target``, as the coordinator asked. The old role
        is stopped before the new one starts (they share this node's RPC port and its GPU), so the
        two never overlap. A target that cannot start is not tried again for ROLE_RETRY_SECONDS,
        and the node falls back to the role it would have started in.
        """
        previous = self._current_pool()
        log_worker(f"The coordinator asked this node to serve as '{target}' instead of '{previous}'.")
        old_coding, old_app = self.coding_member, self.app_member
        try:
            if old_coding is not None:
                old_coding.stop()
            elif old_app is not None:
                old_app.stop()
            else:
                self._stop_rpc_server()
        except Exception as exc:  # noqa: BLE001 - the new role is started regardless
            log_error(f"Error stopping the '{previous}' role: {type(exc).__name__}: {exc}")
        started = False
        try:
            if target == "coding":
                started = self.start_coding_member()
            elif target == "app":
                started = self.start_app_member(keep_port=True)
            else:
                started = self.start_rpc_server()
        except Exception as exc:  # noqa: BLE001 - treated like any other failed start
            log_error(f"Could not start the '{target}' role: {type(exc).__name__}: {exc}")
        if started:
            # The old member stayed referenced (stopped, reporting "not ready") until now, so the
            # heartbeats in between kept naming the old role instead of showing a gap.
            with self._role_lock:
                if target != "coding":
                    self.coding_member = None
                if target != "app":
                    self.app_member = None
                gone = self._stopped
            if gone:
                # stop() ran while this switch was starting the new role: nothing may outlive it.
                for member in (self.coding_member, self.app_member):
                    if member is not None:
                        member.stop()
                self._stop_rpc_server()
                return
            self._announce_role()
            if self._latest_answer:
                self._follow_assignment(self._latest_answer)
            return
        self._role_hold[target] = time.monotonic() + ROLE_RETRY_SECONDS
        log_error(f"The '{target}' role could not start; this node goes back to '{self._default_pool()}'.")
        with self._role_lock:
            self.coding_member = self.app_member = None
        fallback = self._default_pool()
        ok = False
        if fallback != target:
            try:
                ok = self.start_app_member(keep_port=True) if fallback == "app" else self.start_rpc_server()
            except Exception as exc:  # noqa: BLE001
                log_error(f"Could not start the '{fallback}' role: {type(exc).__name__}: {exc}")
        if ok:
            self._announce_role()
        elif not self._shutdown_event.is_set():
            self.request_shutdown(FATAL_EXIT_CODE, f"no engine could be started after leaving the '{previous}' role")

    def _stop_rpc_server(self) -> None:
        """Stops ggml-rpc-server for a role change (the node keeps heartbeating)."""
        # The reference is cleared first: the heartbeat loop treats an exited rpc_process it still
        # holds as a crash, and a deliberate stop must not look like one.
        process, self.rpc_process = self.rpc_process, None
        self.rpc_proto = ""
        if process is not None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            except Exception as e:
                log_error(f"Error terminating RPC process: {e}")
        log_file, self.rpc_log_file = getattr(self, "rpc_log_file", None), None
        if log_file:
            try:
                log_file.close()
            except Exception:
                pass

    def _gpu_environment(self) -> Tuple[Dict[str, str], str]:
        """
        The environment for a process that uses this node's GPU, and the ``allowed_gpu_indices``
        limit it was built from ("-1" for no limit, "none" for CPU only). Applies the owner's GPU
        limit, and keeps Vulkan off an integrated GPU when a discrete one is present.
        """
        # Inject GPU isolation environment variables
        env = os.environ.copy()
        allowed_gpu = get_worker_limits().get('allowed_gpu_indices', '-1').lower()
        if allowed_gpu != '-1' and allowed_gpu != 'none':
            env['CUDA_VISIBLE_DEVICES'] = str(allowed_gpu)
            env['GGML_VK_VISIBLE_DEVICES'] = str(allowed_gpu)
            log_worker(f"Hardware Isolated to GPU ID(s): {allowed_gpu}")
        elif allowed_gpu == 'none':
            env['CUDA_VISIBLE_DEVICES'] = ""
            env['GGML_VK_VISIBLE_DEVICES'] = ""
            log_worker("Hardware Isolated to CPU Only.")
        elif 'GGML_VK_VISIBLE_DEVICES' not in env:
            # On hybrid iGPU + dGPU systems (e.g. AMD Radeon 880M iGPU + NVIDIA RTX 5070 dGPU),
            # automatically isolate Vulkan to discrete GPU(s) so the 0.5 GB iGPU is not bound.
            try:
                vk_res = subprocess.run(
                    ["vulkaninfo", "--summary"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    **_hidden_process_kwargs()
                )
                if vk_res.returncode == 0:
                    discrete_indices = []
                    integrated_indices = []
                    current_idx = None
                    for line in vk_res.stdout.splitlines():
                        s = line.strip()
                        if s.startswith("GPU") and s.endswith(":"):
                            num = s[3:-1]
                            if num.isdigit():
                                current_idx = int(num)
                        elif s.startswith("deviceType") and "=" in s and current_idx is not None:
                            dtype = s.split("=", 1)[1].strip()
                            if "DISCRETE_GPU" in dtype:
                                discrete_indices.append(str(current_idx))
                            elif "INTEGRATED_GPU" in dtype:
                                integrated_indices.append(str(current_idx))
                    if discrete_indices and integrated_indices:
                        vk_devs = ",".join(discrete_indices)
                        env['GGML_VK_VISIBLE_DEVICES'] = vk_devs
                        log_worker(f"Auto-isolated discrete Vulkan GPU index(es): {vk_devs} (ignoring integrated GPU {','.join(integrated_indices)})")
            except Exception:
                pass
        return env, allowed_gpu

    def start_rpc_server(self) -> bool:
        """
        Downloads and starts the ggml-rpc-server binary.

        Returns:
            True when a subprocess is running, False when the node has no engine and must
            not pretend to be a usable worker.

        This returns a status instead of calling ``sys.exit()`` because ``start()`` is
        routinely invoked on a daemon thread, where SystemExit would silently kill only
        that thread and leave the node half-alive. The caller turns a False into a proper
        shutdown request.
        """
        # A worker on the coordinator's own host ("hybrid") uses the build the release installer
        # put there; any other worker follows the coordinator's signed release.
        binary_path = download_and_extract_engine(
            "worker", coordinator_url=None if self.role == "hybrid" else self.coordinator_url)
        if not binary_path:
            log_error("Could not obtain ggml-rpc-server. Exiting.")
            return False

        log_worker(f"Starting RPC Server on {self.rpc_ip}:{self.rpc_port}...")

        env, allowed_gpu = self._gpu_environment()

        # THE TENSOR CACHE IS WHAT MAKES A REMOTE NODE PRACTICAL.
        #
        # llama-server does not read weights from the node — it *pushes* them, over the RPC
        # socket, every time the engine starts. A node holding half of an 8B model receives
        # something like 2.5 GB before it can serve a single token, bounded by this
        # machine's download speed, and the Coordinator rebuilds the engine whenever the
        # worker set changes. Without a cache that transfer is paid again on every restart,
        # which is why a WAN cluster spent almost all of its time loading.
        #
        # ggml-rpc-server stores received tensors on disk keyed by hash, so the second load
        # of the same model is a local read. The flag is a bare toggle and the location
        # comes from LLAMA_CACHE, which is pointed inside the install so the cache is
        # visible, portable and removable with everything else DLNE owns rather than hidden
        # in a user profile.
        cache_args = []
        if _rpc_server_supports_cache(binary_path):
            cache_dir = os.path.join(get_base_dir(), "rpc_cache")
            try:
                os.makedirs(cache_dir, exist_ok=True)
                env["LLAMA_CACHE"] = cache_dir
                cache_args = ["--cache"]
                log_worker(f"RPC tensor cache enabled at {cache_dir}.")
            except OSError as exc:
                log_warning(f"Could not create the RPC tensor cache directory: {exc}")
        else:
            log_warning(
                "This build of ggml-rpc-server has no tensor cache, so the Coordinator "
                "re-sends this node's share of the model on every engine restart."
            )

        # Start the subprocess and redirect output to a file for debugging crashes
        try:
            log_dir = os.path.join(get_base_dir(), "logs")
            os.makedirs(log_dir, exist_ok=True)
            log_path = os.path.join(log_dir, "rpc_server.log")
            self.rpc_log_file = open(log_path, "a")
            banner_offset = self.rpc_log_file.tell()
            # CPU only: name the device rather than only hiding GPUs from Vulkan, which would
            # leave Metal (macOS) and any other backend in play.
            device_args = ["--device", "CPU"] if allowed_gpu == "none" else []
            self.rpc_process = subprocess.Popen(
                [binary_path, "--host", self.rpc_ip, "--port", str(self.rpc_port)] + cache_args + device_args,
                stdout=self.rpc_log_file,
                stderr=subprocess.STDOUT,
                env=env,
                **_hidden_process_kwargs()
            )
            log_worker("RPC Server started successfully.")
            self.rpc_proto = read_rpc_banner(log_path, banner_offset, self.rpc_process)
            if self.rpc_proto:
                log_worker(f"ggml-rpc-server speaks RPC protocol {self.rpc_proto}.")
            else:
                log_warning("Could not read the ggml-rpc-server protocol version; the Coordinator "
                            "will not route to this node until it can.")
            return True
        except Exception as e:
            log_error(f"Failed to start RPC server: {e}")
            # Without this the node used to keep heartbeating with rpc_process = None, so
            # the crash check below never fired and the cluster kept routing tensors to an
            # RPC endpoint that was never listening.
            return False

    # ----------------------------------------------------------------------------------
    # Heartbeat
    # ----------------------------------------------------------------------------------

    def heartbeat_loop(self):
        """
        Continuously sends hardware availability and yielding status to Coordinator.

        Runs in a daemon thread. Fatal conditions end the node through
        :meth:`request_shutdown` - never through ``sys.exit()``, which from here would
        unwind this thread only and leave the process running without heartbeats.
        """
        try:
            while self.is_running and not self._shutdown_event.is_set():
                try:
                    current_time = time.time()

                    # Ephemeral Token Rotation (60 seconds). The new token is only written to
                    # disk AFTER the Coordinator has accepted a heartbeat carrying it, so the
                    # extension never reads a token the Coordinator does not know yet.
                    token_rotated = False
                    if current_time - self.last_token_rotation >= TOKEN_ROTATION_SECONDS:
                        self.access_token = secrets.token_hex(32)
                        self.last_token_rotation = current_time
                        token_rotated = True

                    # Refresh hardware profile to check for yielding (Gaming Mode)
                    self.hardware_profile = self._probe_hardware()

                    # Check if RPC server crashed! Done before building the payload so a
                    # dead engine is never announced to the cluster as available capacity.
                    # A role change clears self.rpc_process before it stops the process, so an
                    # engine stopped on purpose is never mistaken for a crash.
                    rpc_process = self.rpc_process
                    if rpc_process and rpc_process.poll() is not None and self.rpc_process is rpc_process:
                        err_code = rpc_process.returncode
                        log_error(f"CRITICAL: Engine subprocess crashed unexpectedly with error code {err_code}!")
                        log_error("CRITICAL: This node cannot continue processing. Stopping worker...")
                        # A crashed engine must never report success to a supervisor, so a
                        # 0/None return code is promoted to a generic failure code. A
                        # negative code (a POSIX signal) is passed through untouched.
                        self.request_shutdown(
                            err_code if err_code else FATAL_EXIT_CODE,
                            f"RPC engine subprocess exited with code {err_code}",
                        )
                        return

                    payload = self._build_heartbeat_payload()

                    status_msg = "Yielding to external load" if self.hardware_profile['is_yielding'] else "Active"

                    try:
                        # Announce to coordinator
                        safe_url = self.coordinator_url.rstrip('/')
                        # 5-second socket connection timeout and 10-second read timeout to handle network drops
                        response = httpx.post(f"{safe_url}/worker/heartbeat", json=payload, timeout=httpx.Timeout(10.0, connect=5.0))
                        response.raise_for_status()
                        if token_rotated and not self.companion_of:
                            # (A companion has tokens of its own for its own tunnel; the file the editor reads to
                            # prove "I contribute" is the GPU node's.)
                            _write_token_to_disk(self.access_token)
                        # The coordinator says which role this node plays and, for a pool member,
                        # which model it serves.
                        try:
                            answer = response.json()
                        except ValueError:
                            answer = {}
                        self._follow_assignment(answer)
                        self._announce_role()
                        if self.verbose_heartbeats:
                            log_worker(f"Heartbeat OK | Status: {status_msg}")

                    except httpx.HTTPStatusError as e:
                        if e.response.status_code == 426:
                            # The coordinator runs a different engine version than this node.
                            required = e.response.headers.get("X-DLNE-Required-Version", "").strip()

                            if required and is_older(required, VERSION):
                                # We are AHEAD of the coordinator: it is mid-restart onto this
                                # same release, or it was rolled back. Going "up" to an older
                                # build is not a fix, so wait and keep trying.
                                log_warning(
                                    f"Coordinator requires {required} but this node runs the newer "
                                    f"{VERSION}. Waiting for the coordinator to converge..."
                                )
                                if self._shutdown_event.wait(AHEAD_OF_COORDINATOR_RETRY_SECONDS):
                                    return
                                continue

                            # We are behind, so the engine on disk is what is out of date.
                            # Replacing it is the launcher's job: the VS Code extension
                            # fetches the coordinator's signed release and starts us again.
                            # This process cannot fix itself and every further beat would be
                            # refused, so it ends with the code that asks for that restart.
                            log_error(
                                f"The coordinator runs engine {required or 'a different version'}; "
                                f"this node runs {VERSION}."
                            )
                            log_system("Restart this worker so it can fetch the matching engine.")
                            self.request_shutdown(
                                VERSION_MISMATCH_EXIT_CODE,
                                "engine version does not match the coordinator",
                            )
                            return
                        elif e.response.status_code == 401:
                            # Retrying cannot fix a credential mismatch - it only spams the
                            # Coordinator's log every 5 seconds and hides the real problem,
                            # so this is fatal exactly like the version rejection above.
                            log_error(f"FATAL: The Coordinator rejected this node's credentials! {_response_detail(e.response)}")
                            log_system("Your NETWORK_KEY is missing or does not match the Coordinator - ask your admin for a fresh invite file.")
                            self.request_shutdown(FATAL_EXIT_CODE, "Coordinator rejected worker authentication (HTTP 401)")
                            return
                        elif (e.response.status_code == 403
                              and e.response.headers.get("X-DLNE-Refusal", "") == "cpu-minimum"):
                            # Below the swarm's floor for CPU-only nodes. The hardware will not
                            # change between heartbeats, so stop and say exactly what is missing.
                            reason = _response_detail(e.response) or "this machine is below the swarm's minimum for CPU-only nodes"
                            log_error(f"The coordinator will not admit this machine{' as a phone-pool member' if self.companion_of else ''}: {reason}")
                            if not self.companion_of:
                                # (The editor shows this to the owner and stops; a companion's refusal ends the
                                # companion only, and the GPU keeps working.)
                                print(f"{REFUSAL_MARKER}{reason}", flush=True)
                            self.request_shutdown(CPU_BELOW_MINIMUM_EXIT_CODE, "below the minimum for CPU-only nodes")
                            return
                        else:
                            log_error(f"Coordinator responded with error: {e.response.status_code}")
                    except httpx.RequestError as e:
                        log_error(f"Cannot reach Primary Coordinator ({e}). Retrying in 5 seconds...")
                except Exception as outer_e:
                    log_error(f"WARNING: Heartbeat loop encountered an unhandled exception: {outer_e}")

                # Waiting on the Event instead of sleeping keeps the 5-second cadence while
                # letting a shutdown request from another thread take effect immediately.
                self._shutdown_event.wait(HEARTBEAT_INTERVAL_SECONDS)
        finally:
            # If the loop ever ends without an explicit request - is_running cleared by an
            # external stop(), or an unrecoverable error escaping the guards above - wake
            # start() anyway. A silently dead heartbeat thread under a still-running
            # process is the exact failure this rewrite exists to eliminate.
            self.request_shutdown(0, "heartbeat loop ended")

    # ----------------------------------------------------------------------------------
    # Lifecycle
    # ----------------------------------------------------------------------------------

    def _stop_on_termination_signal(self) -> None:
        """
        Turn SIGTERM (what the editor and ``kill`` send on macOS and Linux) and SIGBREAK (Ctrl+Break on
        Windows) into the ordinary shutdown. Python's default for SIGTERM is to die on the spot, running
        no atexit hook and no ``finally``, which left a coding member's llama-server running with the
        owner's memory after "Stop contributing". Only the main thread may install a handler; a node
        that runs on a background thread (hybrid, redundancy) leaves the signals to its host.
        """
        if threading.current_thread() is not threading.main_thread():
            return
        import signal

        def handler(signum, frame) -> None:
            self.request_shutdown(0, "terminated")

        for name in ("SIGTERM", "SIGBREAK"):
            number = getattr(signal, name, None)
            if number is not None:
                try:
                    signal.signal(number, handler)
                except (ValueError, OSError):
                    pass

    def start(self):
        """
        Boots the node and blocks until it shuts down.

        Called on the main thread in ``worker_only`` mode and on a daemon thread in the
        hybrid/redundancy modes (where the Coordinator owns the main thread). That
        difference is why the process exit at the end is conditional: killing the process
        from a background thread would take a perfectly healthy Coordinator down with the
        worker component.
        """
        self.is_running = True
        self._stop_on_termination_signal()

        started = self.start_app_member() if self._serves_cpu_pool() else self.start_rpc_server()
        if not started:
            # No engine means no contribution. Fall through to the same clean teardown as
            # any other fatal condition instead of heartbeating a phantom endpoint.
            self.request_shutdown(FATAL_EXIT_CODE, "RPC engine could not be started")
        else:
            # Read the key here as well as in each heartbeat so the "unsigned" warning is
            # printed once during startup, next to the rest of the boot output, rather than
            # five seconds later in the middle of the heartbeat stream.
            self._get_network_key()

            # Tell the extension what this machine's job is (the coordinator may change it later).
            self._announce_role()

            # "Both": decided before the first heartbeat, so the coding model's thread share, its RAM figure and
            # its gate are in place whenever the coordinator assigns the job.
            self._plan_companion()

            # Start heartbeat in a background thread
            heartbeat_thread = threading.Thread(target=self.heartbeat_loop, daemon=True, name="dlne-worker-heartbeat")
            heartbeat_thread.start()

            if "127.0.0.1" not in self.coordinator_url and "localhost" not in self.coordinator_url:
                try:
                    from engine.reverse_tunnel import start_worker_reverse_tunnel
                    start_worker_reverse_tunnel(
                        self.coordinator_url,
                        self.worker_id,
                        self.rpc_port,
                        self._shutdown_event,
                        # Read at each connect: the token rotates every minute.
                        token_provider=lambda: self.access_token,
                    )
                except Exception:
                    pass

            log_worker("Worker Node initialized and broadcasting to network.")

            self._launch_companion()

        try:
            # Wait on the shutdown Event rather than sleeping forever. The heartbeat thread
            # cannot terminate this loop with sys.exit() - SystemExit unwinds only its own
            # thread - so the Event is the one signal that reliably crosses over. The
            # one-second timeout keeps Ctrl+C as responsive as the old time.sleep(1) loop.
            while not self._shutdown_event.wait(SHUTDOWN_POLL_SECONDS):
                pass
        except KeyboardInterrupt:
            self.request_shutdown(0, "keyboard interrupt")
        finally:
            self.stop()

        # Only the main thread may end the process. On a background thread SystemExit would
        # be swallowed by the threading machinery, so we say plainly that the worker
        # component is gone and let whatever owns the main thread carry on.
        if self.exit_code:
            if threading.current_thread() is threading.main_thread():
                sys.exit(self.exit_code)
            log_error(
                f"Worker component has stopped (exit code {self.exit_code}). This node is no "
                "longer contributing hardware to the cluster."
            )

    def stop(self):
        self.is_running = False

        # Idempotent: start()'s teardown, the atexit hook and any embedder can all call
        # this, and terminating an already-reaped process or double-closing the log file
        # would only produce noise.
        with self._shutdown_lock:
            if self._stopped:
                return
            self._stopped = True

        log_worker("Shutting down Worker Node...")
        # Under the role lock so a role switch in flight either shows up here or sees _stopped
        # (set above) and stops the member it just made.
        with self._role_lock:
            self._role_target = None
            coding_member = self.coding_member
            companion = self._companion
        if companion is not None:
            try:
                companion.request_shutdown(0, "the GPU node of this machine stopped")
                companion.stop()
            except Exception as e:
                log_error(f"Error stopping the phone-pool half of this machine: {e}")
        if coding_member is not None:
            try:
                coding_member.stop()
            except Exception as e:
                log_error(f"Error stopping the coding member: {e}")
        if self.app_member is not None:
            try:
                self.app_member.stop()
            except Exception as e:
                log_error(f"Error stopping the CPU pool model server: {e}")
        if self.rpc_process:
            try:
                self.rpc_process.terminate()
                self.rpc_process.wait(timeout=2)
            except Exception as e:
                log_error(f"Error terminating RPC process: {e}")
        if hasattr(self, 'rpc_log_file') and self.rpc_log_file:
            try:
                self.rpc_log_file.close()
            except Exception:
                pass
        log_worker("Shutdown complete.")
