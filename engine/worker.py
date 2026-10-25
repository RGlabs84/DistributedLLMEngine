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

import re
import time
import httpx
import subprocess
import threading
import sys
import os
from typing import Dict
from engine.logger import log_worker, log_error, log_system, log_warning
from engine.hardware import get_hardware_profile
from engine.downloader import download_and_extract_engine
from engine.version import VERSION
from engine.config import get_base_dir, get_worker_limits
from engine.authn import sign_heartbeat
from engine.release import is_older

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

# How long a node that is AHEAD of its coordinator waits between heartbeats. The coordinator
# is probably mid-restart onto the same version; hammering it helps nobody.
AHEAD_OF_COORDINATOR_RETRY_SECONDS = 30.0


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
            [binary_path, "--help"], capture_output=True, text=True, timeout=30
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
                 verbose_heartbeats: bool = True, role: str = "worker"):
        self.coordinator_url = coordinator_url
        self.hardware_profile = get_hardware_profile()
        self.rpc_process = None
        # The RPC protocol the running ggml-rpc-server announced, e.g. "8.0.1" ("" = unknown).
        self.rpc_proto = ""
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
                    timeout=5
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
                env=env
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
                    self.hardware_profile = get_hardware_profile()

                    # Check if RPC server crashed! Done before building the payload so a
                    # dead engine is never announced to the cluster as available capacity.
                    if self.rpc_process and self.rpc_process.poll() is not None:
                        err_code = self.rpc_process.returncode
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
                        if token_rotated:
                            _write_token_to_disk(self.access_token)
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
                            log_error(f"The coordinator will not admit this machine: {reason}")
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

        if not self.start_rpc_server():
            # No engine means no contribution. Fall through to the same clean teardown as
            # any other fatal condition instead of heartbeating a phantom endpoint.
            self.request_shutdown(FATAL_EXIT_CODE, "RPC engine could not be started")
        else:
            # Read the key here as well as in each heartbeat so the "unsigned" warning is
            # printed once during startup, next to the rest of the boot output, rather than
            # five seconds later in the middle of the heartbeat stream.
            self._get_network_key()

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
                    )
                except Exception:
                    pass

            log_worker("Worker Node initialized and broadcasting to network.")

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
