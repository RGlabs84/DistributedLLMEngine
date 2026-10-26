"""
A worker's side of the coding members (docs/coding-members.md section 4): this machine's GPU
runs a whole coding model and answers coding requests on its own, about two agent slots per
volunteer. It works like the CPU pool's member (engine/app_member.py): it downloads and
verifies the model, runs llama-server at low priority, backs off after failures, stops
cleanly, and reports itself in the signed heartbeat. Two things differ:

* The coordinator reaches it through the worker's reverse tunnel, which dials the worker's own
  RPC port. llama-server therefore stays on a loopback port of its own, and a small **member
  proxy** answers on the RPC port: ``POST /v1/chat/completions``, ``POST /v1/completions`` and
  ``GET /health``, and nothing else (llama-server's /props, /slots and the rest are never
  reachable through it).
* A request is either plain JSON or an end-to-end envelope (section 6): the proxy opens the
  envelope with this process's key ring (keys that exist only in memory, rotated every six hours)
  and seals every piece of the answer for the editor that asked, ending a stream with a sealed end
  marker. Errors and "busy" answers go back in the clear and carry nothing of the request.

The heartbeat's ``coding`` block is signed with the member's own Ed25519 identity key
(engine/member_identity.py), which is what the operator's TRUSTED_MEMBERS list is matched against.

PRIVACY RULES THIS FILE KEEPS
    * No request or response body, and no text taken from one, ever reaches a log, an error
      message or an exception message: only fixed sentences, status codes and exception class
      names are written.
    * Only the body goes upstream, with a fixed content type. No header the caller sent is passed
      on, so a member learns nothing about who asked.
    * llama-server runs with the no-trace arguments (engine/no_trace.py), its log is truncated at
      every start, and the proxy keeps nothing of a request once it has been answered.

WHICH MODEL TO SERVE is the coordinator's call (its heartbeat answer carries a coding ladder
tier), but the files come only from this node's own copy of the signed release: the tier the
coordinator names is looked up there by id, and a tier whose files differ from the signed ones,
or that the release does not enable, is refused. Every file must match its signed size and
SHA-256 before llama-server is pointed at it.
"""

import asyncio
import contextlib
import inspect
import json
import math
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import anyio
import httpx
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from engine import coding_member_select as select
from engine import member_identity, no_trace
from engine.app_pool import sha256_ok
from engine.model_alias import alias_args
from engine.e2e import KeyRing, ResponseSealer
from engine.fim_warm import fim_warm_body
from engine.logger import log_error, log_success, log_system, log_warning

# Parallel requests the member accepts. The coding context is split evenly between them.
CODING_SLOTS = 2
DEFAULT_CONTEXT = 65536
# Host RAM llama-server may keep old prompts in, so a session coming back finds its prefix
# (the member reads prompts slowly, so that matters). Its own default, 8 GiB, is too much to
# take from a volunteer who already holds part of the model in system memory.
PROMPT_CACHE_MIB = 2048
LIMITED_PROMPT_CACHE_MIB = 512       # the same, while the volunteer has put a cap on the memory they lend
FIT_MARGIN_MIB = 1024                # what --fit leaves free on a card by default (llama.cpp's own figure)
# While the owner's own load makes the worker yield (a game, a render), the member stops answering at once,
# lets the requests in flight finish for this long, then stops llama-server to give the GPU memory back.
YIELD_DRAIN_SECONDS = 10.0
# After the yielding ends, the member waits this long before it restarts and reports ready again, unless the
# worker's limits say otherwise: the coordinator waits the same TOPOLOGY_SETTLE_SECONDS before it rebuilds
# the RPC engine for a node that comes back, so a flapping load costs one restart per this period at most.
DEFAULT_RESUME_SECONDS = 30.0
# After a failure that will not fix itself in seconds, a tier is not tried again for this long,
# doubling each time. A server that dies sooner than STABLE_SECONDS after becoming ready counts
# as a failure too (a crash loop must not reload the model every five seconds).
BACKOFF_FIRST_SECONDS = 60.0
BACKOFF_LASTING_SECONDS = 600.0      # the first wait after a failure that trying again unchanged cannot fix
BACKOFF_MAX_SECONDS = 3600.0
STABLE_SECONDS = 120.0
# A model change waits this long for the requests in flight before the old server is stopped.
DRAIN_SECONDS = 60.0

# How long the first load of the coding model waits for the phone-pool model of the same machine (engine/companion.py).
GATE_WAIT_SECONDS = 25 * 60.0

MAX_BODY_BYTES = 16 << 20
# A prompt of tens of thousands of tokens is read before the first byte comes back.
UPSTREAM_READ_TIMEOUT = 1800.0
PROXY_START_SECONDS = 10.0



# ======================================================================================
# This process's end-to-end keys
# ======================================================================================

_KEYRING: Optional[Any] = None
_KEYRING_LOCK = threading.Lock()


def process_keyring() -> Any:
    """The key ring (engine.e2e.KeyRing): made once per process, kept in memory only, so a role
    change does not strand the routes already issued against its public key."""
    global _KEYRING
    with _KEYRING_LOCK:
        if _KEYRING is None:
            _KEYRING = KeyRing()
        return _KEYRING


IDENTITY_FILE = os.path.join("config", "coding_identity.key")


# ======================================================================================
# llama-server
# ======================================================================================

def coding_server_args(binary: str, model_path: str, port: int, ctx: int = DEFAULT_CONTEXT,
                       slots: int = CODING_SLOTS, threads: int = 0, extra: Sequence[str] = (),
                       cache_ram_mib: int = PROMPT_CACHE_MIB) -> List[str]:
    """
    llama-server for a coding member: loopback only, the whole model fitted to this machine's own
    memory (``--fit on`` places layers and experts between the GPU and system RAM; ``-ngl`` is
    left unset on purpose, it would switch that off), the coding context, the settings of the
    live coding engine, the owner's limits (``threads``, and ``extra`` such as ``--fit-target``), the
    no-trace arguments, and the model file's name as its ``--alias`` (without it every answer would
    carry the path of the owner's model folder, home directory and user name included).
    """
    args = [binary, "-m", model_path, *alias_args(model_path), "--host", "127.0.0.1", "--port", str(port),
            "-c", str(ctx), "-np", str(slots), "--fit", "on",
            "-fa", "on", "-ctk", "q8_0", "-ctv", "q8_0",
            "--cache-reuse", "256", "--cache-ram", str(cache_ram_mib),
            "--jinja"]
    if threads > 0:
        args += ["-t", str(threads), "-tb", str(threads)]
    return args + list(extra) + list(no_trace.LLAMA_NO_TRACE_ARGS)


def fit_targets_mib(device_totals_gib: Sequence[float], limit_gib: Optional[float]) -> List[int]:
    """
    ``--fit-target`` values (MiB to leave free on each card, in the order llama-server sees the cards)
    that keep llama-server inside ``limit_gib`` of GPU memory in all. ``--fit`` fills each card up to
    its free memory minus this margin, so a margin of (card - share of the limit) caps what it takes
    even when other programs already hold some of the card. [] when there is no limit that binds
    (then llama.cpp's own default margin stands).
    """
    totals = [float(t) for t in device_totals_gib if isinstance(t, (int, float)) and t > 0]
    if not totals or limit_gib is None or limit_gib >= sum(totals):
        return []
    share = max(0.0, limit_gib) / sum(totals)
    return [max(FIT_MARGIN_MIB, int(round(total * (1.0 - share) * 1024))) for total in totals]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# --------------------------------------------------------------------------------------
# What differs by platform (tested by faking the platform; the real thing needs a real Windows PC or Mac)
# --------------------------------------------------------------------------------------

def _is_windows() -> bool:
    return sys.platform == "win32"


# subprocess.CREATE_NO_WINDOW and friends exist only on Windows, so their values are spelled out.
CREATE_NO_WINDOW = 0x08000000
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000


def _child_creation_flags() -> int:
    """
    The Popen ``creationflags`` for llama-server. Windows: no console window (llama-server is a
    console program; started by a worker that itself has no visible console, which is how the editor
    runs it, it would get a black window of its own for as long as it runs) and the below-normal
    priority class (the equivalent of ``nice``). Elsewhere 0, which is the only value Popen accepts.
    """
    return (CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS) if _is_windows() else 0


def _exclusive_loopback_listener(port: int) -> socket.socket:
    """
    A listening socket on 127.0.0.1:``port`` that no other program can share. uvicorn makes its own socket
    with SO_REUSEADDR, and on Windows that flag does not mean what it means elsewhere: it lets several
    programs listen on the very same port at once, the newest one winning, silently. A port that is
    already taken would then look free and either steal another program's connections or lose ours. Here the
    flag is left off, which on Windows is already exclusive for the pair (a bind fails while the port is
    listened on by anyone). SO_EXCLUSIVEADDRUSE is not used either: it also refuses a port that still
    has closed connections in TIME_WAIT, which would stop a worker that restarts within a few minutes.
    Raises OSError when the port is taken.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", port))
        listener.listen(2048)
    except OSError:
        listener.close()
        raise
    return listener


def _model_argument(model_path: str) -> Tuple[str, Optional[str]]:
    """
    ``(the -m argument, the working directory for llama-server)``. Windows hands a program its
    arguments in the ANSI code page unless the program asks for UTF-16, so a model folder under
    a user name with an accented, Cyrillic or Japanese letter may arrive with ``?`` in it and the
    model would not load. A model file's name is always plain ASCII (engine.downloader's
    safe_file_name), so on Windows, for such a folder, llama-server is started inside it and given the bare
    file name. Anywhere else, and for an ASCII path, the full path is passed as before.
    """
    if _is_windows() and not model_path.isascii():
        return os.path.basename(model_path), os.path.dirname(model_path)
    return model_path, None


# Windows has no "die with your parent": a child outlives the process that started it, however that
# ended (the editor stops a worker with TerminateProcess, which runs no cleanup of any kind), and
# an orphaned llama-server would keep gigabytes of the owner's video memory and keep serving. A job
# object with this flag is the answer: the system kills every process in it when the last handle to
# the job closes, and that handle closes when this process ends, however it ended.
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9      # the information class SetInformationJobObject is told about
_JOB: Optional[int] = None                       # the job's handle; open for as long as this process lives
_JOB_LOCK = threading.Lock()


def _job_limits_structure() -> Any:
    """JOBOBJECT_EXTENDED_LIMIT_INFORMATION, laid out as the 64-bit Windows header has it (144 bytes)."""
    import ctypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32),
                    ("SchedulingClass", ctypes.c_uint32)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    return ExtendedLimits


def _kernel32() -> Any:
    """kernel32 with the four calls below declared (without argument types ctypes passes handles as 32-bit ints)."""
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    api.CreateJobObjectW.restype = wintypes.HANDLE
    api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    api.SetInformationJobObject.restype = wintypes.BOOL
    api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    api.AssignProcessToJobObject.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    return api


def _bind_to_worker_lifetime(process: Any, api: Any = None) -> bool:
    """
    Windows only: put ``process`` into this worker's kill-on-close job object (made on first use), so it
    dies with the worker. True when it is in the job. Never raises; False elsewhere, and False when the
    system refuses (a job the worker's own parent forbids nesting in, before Windows 8).
    """
    global _JOB
    if not _is_windows():
        return False
    try:
        import ctypes
        api = api if api is not None else _kernel32()
        with _JOB_LOCK:
            if _JOB is None:
                job = api.CreateJobObjectW(None, None)
                if not job:
                    return False
                limits = _job_limits_structure()()
                limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                if not api.SetInformationJobObject(job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                                   ctypes.byref(limits), ctypes.sizeof(limits)):
                    api.CloseHandle(job)
                    return False
                _JOB = job
            job = _JOB
        return bool(api.AssignProcessToJobObject(job, int(process._handle)))
    except Exception:  # noqa: BLE001 - a child that is not tied to the worker is a smaller problem than no child
        return False


# Where the member writes down which process is its llama-server, so a later worker can recognise and
# stop one that the last worker left behind. macOS and Linux have no job object and no "die with your
# parent" for a child, and a worker ended by SIGKILL, a crash or the editor's TerminateProcess runs
# no cleanup; without this the orphan would keep gigabytes of the owner's memory until they reboot.
SERVER_RECORD = os.path.join("run", "coding_server.json")


def _same_program(left: Any, right: Any) -> bool:
    try:
        return os.path.normcase(os.path.realpath(str(left))) == os.path.normcase(os.path.realpath(str(right)))
    except (OSError, ValueError):
        return False


def _remember_server(base_dir: str, process: Any) -> None:
    """Record ``process`` (pid, start time, program, command line) in ``<base_dir>/run``. Best effort."""
    try:
        import psutil
        info = psutil.Process(process.pid)
        try:
            cmdline: Optional[List[str]] = info.cmdline()
        except psutil.Error:
            cmdline = None
        record = {"pid": info.pid, "created": info.create_time(), "exe": info.exe(), "cmdline": cmdline}
        path = os.path.join(base_dir, SERVER_RECORD)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
    except Exception:  # noqa: BLE001 - without the record only the cleanup of a leftover is lost
        pass


def _forget_recorded_server(base_dir: str, pid: Optional[int] = None) -> None:
    """Delete the record (only if it names ``pid`` when one is given)."""
    path = os.path.join(base_dir, SERVER_RECORD)
    try:
        if pid is not None:
            with open(path, "r", encoding="utf-8") as handle:
                if json.load(handle).get("pid") != pid:
                    return
        os.remove(path)
    except (OSError, ValueError, AttributeError):
        pass


def reap_stale_server(base_dir: str) -> bool:
    """
    Stop the llama-server that an earlier worker of this install left running, if there is one; True when
    one was stopped. It is stopped only if it is provably the same process: the pid, the start time (to
    a second), the program and the whole command line must all match what was recorded, so a pid the
    system has since given to another program is never touched. The record is removed either way. Call
    it only once this worker owns the member port, so no other live worker can own the recorded server.
    Never raises.
    """
    path = os.path.join(base_dir, SERVER_RECORD)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            record = json.load(handle)
        pid, created = int(record["pid"]), float(record["created"])
        exe, cmdline = record.get("exe"), record.get("cmdline")
    except FileNotFoundError:
        return False
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        _forget_recorded_server(base_dir)
        return False
    stopped = False
    try:
        import psutil
        proc = psutil.Process(pid)
        if (abs(proc.create_time() - created) <= 1.0 and (exe is None or _same_program(proc.exe(), exe))
                and (cmdline is None or proc.cmdline() == cmdline)):
            proc.terminate()
            try:
                proc.wait(10)
            except psutil.TimeoutExpired:
                proc.kill()
                proc.wait(5)
            stopped = True
    except Exception:  # noqa: BLE001 - gone already, not ours to touch, or no psutil: nothing to stop
        pass
    _forget_recorded_server(base_dir)
    return stopped


def _speed(value: Any) -> float:
    """A speed as the heartbeat carries it: finite, not negative, one decimal."""
    if isinstance(value, bool):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number) or number < 0:
        return 0.0
    return round(min(number, 100000.0), 1)


def _model_parts(tier: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every file of a tier, in order: ``files`` for a split model, else the tier's one file."""
    files = tier.get("files")
    if isinstance(files, list) and files:
        return [dict(part) for part in files]
    return [{key: tier.get(key) for key in ("file", "url", "sha256", "size")}]


# ======================================================================================
# The member
# ======================================================================================

class CodingMember:
    def __init__(self, base_dir: str, tiers_loader: Callable[[], List[Dict[str, Any]]], port: int,
                 slots: int = CODING_SLOTS, threads: int = 0, llama_port: Optional[int] = None,
                 env_provider: Optional[Callable[[], Dict[str, str]]] = None,
                 download: Optional[Callable[..., bool]] = None,
                 measure: Optional[Callable[..., Any]] = None,
                 keyring: Optional[Any] = None,
                 identity: Optional[Tuple[bytes, str]] = None,
                 machine_provider: Optional[Callable[[], Dict[str, Any]]] = None,
                 limits_provider: Optional[Callable[[], Dict[str, Any]]] = None,
                 resources_provider: Optional[Callable[[], Dict[str, Any]]] = None,
                 start_gate: Optional[threading.Event] = None,
                 ready_timeout: float = 300.0, poll_seconds: float = 1.0) -> None:
        """
        ``port`` is the worker's RPC port (the proxy listens there). ``tiers_loader`` returns the
        signed release's coding ladder. ``download``, ``measure``, ``keyring`` and ``identity`` default to
        engine.downloader.fetch_verified, engine.coding_member_select.measure, this process's key
        ring and the identity key in ``<base_dir>/config`` (made on first use); they are parameters so
        tests need no network and no model. Raises member_identity.IdentityError when the identity
        key file exists but cannot be used.

        ``machine_provider`` returns what this machine can spare, as engine.coding_member_select
        wants it: ``{"vram_gb": ..., "ram_gb": <available RAM>, "unified": bool}``. With it, a
        server whose join test misses the speed floors (``limits_provider`` gives the limits, default
        the config's) is replaced by the next smaller tier, at most MAX_STEP_DOWNS times. Without it
        the member just reports its speeds and is not routed to. A ``"limited": True`` in what it returns
        means the volunteer has capped the memory lent: then a tier that does not fit the capped figures
        is refused outright instead of left to the join test.

        ``resources_provider`` is read at every server start and returns the volunteer's limits as launch
        settings: ``{"threads": n (0 = llama.cpp's default), "extra_args": [...], "cache_ram_mib": n}``.
        """
        self.base_dir = base_dir
        self.tiers_loader = tiers_loader
        self.port = port
        self.slots = max(1, slots)
        self.threads = max(0, threads)
        self._fixed_llama_port = llama_port
        self.llama_port: Optional[int] = llama_port
        self.env_provider = env_provider
        self._download = download
        self._measure = measure
        self.keyring = keyring if keyring is not None else process_keyring()
        self._identity_priv, self.identity_pub = (
            identity if identity is not None
            else member_identity.load_or_create_identity(os.path.join(base_dir, IDENTITY_FILE)))
        self.trust_id = member_identity.trust_id(self.identity_pub)
        self.machine_provider = machine_provider
        self.limits_provider = limits_provider
        self.resources_provider = resources_provider
        # "Both" (engine/companion.py): opened when the phone-pool model of the same machine has settled; the first
        # load of the coding model waits for it, so the two never read the machine's free RAM at the same moment.
        self.start_gate = start_gate
        self.ready_timeout = ready_timeout
        self.poll_seconds = poll_seconds

        # While the owner's own load makes the worker yield, the member is unavailable (set_yielding), and for
        # a settle period after it ends. Nothing is started before _resume_not_before.
        self._yielding = False
        self._yield_since = 0.0
        self._resume_not_before = 0.0

        self.target: Optional[Dict[str, Any]] = None     # what this member serves toward (see _effective)
        self._tier: Optional[Dict[str, Any]] = None      # the signed tier the running server was started for
        self._machine: Optional[Dict[str, Any]] = None   # what the machine could spare when it started
        # A tier this machine cannot run well is replaced by a smaller one, whatever the coordinator keeps
        # naming: tier id -> the id that replaces it. Two reasons: it does not fit what the machine can
        # spare (checked before downloading), or its join test missed the speed floors (_step_downs counts those).
        self._replacement: Dict[str, str] = {}
        self._step_downs = 0
        self.serving: Optional[str] = None               # tier id the running server was started for
        self.model: Optional[str] = None                 # its file name
        self.ready = False
        self.decode_tps = 0.0
        self.prefill_tps = 0.0
        self.process: Optional[subprocess.Popen] = None
        self._log = None
        self._lock = threading.Lock()                    # target, convergence, busy count
        self._engine_lock = threading.RLock()            # starting and stopping the process
        self._busy = False                               # a converge thread is running
        self._inflight = 0
        self._stopped = threading.Event()
        self._ready_since: Optional[float] = None
        self._measure_after = 0.0                        # monotonic time before which the speed test is not retried
        self._measure_failures = 0
        self._last_warning = 0.0
        # tier id -> (failures in a row, monotonic time before which it is not tried again)
        self._backoff: Dict[str, Tuple[int, float]] = {}
        self._server: Optional[uvicorn.Server] = None
        self._thread: Optional[threading.Thread] = None

    # --------------------------------------------------------------------- status
    @property
    def pubkey(self) -> str:
        """The current end-to-end public key (it changes when the key ring rotates)."""
        return self.keyring.public_b64

    @property
    def engine_url(self) -> str:
        return f"http://127.0.0.1:{self.llama_port}"

    def _alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def is_ready(self) -> bool:
        return bool(self.ready and self._alive() and self._available() and not self._stopped.is_set())

    # --------------------------------------------------------------------- yielding to the owner
    def _available(self) -> bool:
        """False while the worker yields to the owner's own load, and for the settle period after it."""
        return not self._yielding and time.monotonic() >= self._resume_not_before

    def _settle_seconds(self) -> float:
        """How long to wait after the yielding ends: the worker's TOPOLOGY_SETTLE_SECONDS, which is how long
        the coordinator waits before it rebuilds the RPC engine for a node that comes back."""
        try:
            limits = self.limits_provider() if self.limits_provider is not None else {}
            value = float(limits.get("topology_settle_seconds", DEFAULT_RESUME_SECONDS))
        except (TypeError, ValueError, AttributeError):
            return DEFAULT_RESUME_SECONDS
        return min(max(value, 0.0), 3600.0)

    def set_yielding(self, yielding: bool) -> None:
        """
        Called with every heartbeat. While ``yielding`` the member says ``ready: false`` with ``slots: 0`` at
        once and takes no new request; the requests in flight get YIELD_DRAIN_SECONDS to finish (then they are
        cut), and then llama-server is stopped to give the GPU memory back. When the yielding ends the
        member waits the settle period, restarts, checks health and measures again, and only then reports
        ready. A load that comes and goes therefore costs one restart per settle period at most.
        """
        yielding = bool(yielding)
        with self._lock:
            if yielding == self._yielding or self._stopped.is_set():
                return
            self._yielding = yielding
            if not yielding:
                self._resume_not_before = max(self._resume_not_before, time.monotonic() + self._settle_seconds())
                log_system("The owner's load has ended; the coding member comes back after a short wait.")
                return
            self._yield_since = time.monotonic()
        log_system("The owner's own load is using this machine: the coding member stops taking requests.")
        threading.Thread(target=self._give_way, name="dlne-coding-yield", daemon=True).start()

    def _give_way(self) -> None:
        """Let the requests in flight finish (cut after YIELD_DRAIN_SECONDS), then stop llama-server."""
        while self._yielding and not self._stopped.is_set():
            with self._lock:
                idle = self._inflight == 0
            if idle or time.monotonic() - self._yield_since >= YIELD_DRAIN_SECONDS:
                break
            time.sleep(0.1)
        if not self._yielding or self._stopped.is_set():
            return          # the load went away within the drain: the engine stays, ready returns after the settle period
        self._stop_engine()
        self._forget_server()
        log_system("The coding model server is stopped while the owner's load lasts.")

    def _block(self) -> Dict[str, Any]:
        """The ``coding`` block without its identity signature."""
        self.keyring.maybe_rotate()
        with self._lock:
            busy = self._inflight
        # No slots while yielding or settling: the router stops sending before anything else happens.
        slots = self.slots if self._available() else 0
        return {
            "model": self.model,
            "tier": self.serving,
            "ready": self.is_ready(),
            "slots": slots,
            "busy": min(busy, slots),
            "decode_tps": _speed(self.decode_tps),
            "prefill_tps": _speed(self.prefill_tps),
            "pubkey": self.pubkey,
            # True only while the arguments this member starts llama-server with pass the no-trace check.
            "no_trace": not no_trace.no_trace_violations(coding_server_args("llama-server", "model.gguf", 1)),
        }

    def status(self) -> Dict[str, Any]:
        """The ``coding`` block of the heartbeat (section 2), signed with the identity key as of now.
        Called for every heartbeat, which is also what rotates the end-to-end key when it is due."""
        return member_identity.sign_block(self._block(), self._identity_priv)

    def role_detail(self) -> Dict[str, Any]:
        """What the worker prints for the extension on its DLNE_ROLE line (contract section 10): the model
        file, the tier, the slots and this member's trust id; names and counts only, a field left out
        while it is not known."""
        detail = {"model": self.model, "tier": self.serving, "slots": self.slots, "trust_id": self.trust_id}
        return {key: value for key, value in detail.items()
                if value is not None and not (isinstance(value, str) and len(value) > 128)}

    # --------------------------------------------------------------------- the proxy
    def start(self) -> bool:
        """Open the member proxy on the worker's port. False when the port cannot be taken."""
        if self._server is not None:
            return True
        config = uvicorn.Config(make_proxy_app(self), host="127.0.0.1", port=self.port, log_level="critical",
                                access_log=False, lifespan="off", ws="none", server_header=False,
                                date_header=False, timeout_graceful_shutdown=2)
        server = uvicorn.Server(config)
        # Windows: our own socket, not uvicorn's (see _exclusive_loopback_listener). Elsewhere uvicorn binds.
        sockets: Optional[List[socket.socket]] = None
        if _is_windows():
            try:
                sockets = [_exclusive_loopback_listener(self.port)]
            except OSError:
                log_error(f"The coding member could not listen on 127.0.0.1:{self.port}.")
                return False

        def serve() -> None:
            try:
                server.run(sockets=sockets)
            except SystemExit:      # uvicorn exits like a program when it cannot bind; here that is "False" below
                pass

        thread = threading.Thread(target=serve, name="dlne-coding-proxy", daemon=True)
        thread.start()
        end = time.monotonic() + PROXY_START_SECONDS
        while not server.started and thread.is_alive() and time.monotonic() < end:
            time.sleep(0.05)
        if not server.started:
            server.should_exit = True
            for listener in sockets or []:
                listener.close()
            log_error(f"The coding member could not listen on 127.0.0.1:{self.port}.")
            return False
        self._server, self._thread = server, thread
        # This worker owns the member port now, so no other live worker of this install can own a
        # recorded llama-server: any that is still running was left by one that was ended abruptly.
        if reap_stale_server(self.base_dir):
            log_system("Stopped a coding model server that an earlier worker left running when it was ended abruptly.")
        log_system(f"Coding member trust id: {self.trust_id} (list it in TRUSTED_MEMBERS on the coordinator "
                   "to have private code sent to this machine).")
        return True

    def _reserve(self) -> Optional["_Slot"]:
        with self._lock:
            if self._inflight >= self.slots:
                return None
            self._inflight += 1
        return _Slot(self)

    def _release(self) -> None:
        with self._lock:
            self._inflight = max(0, self._inflight - 1)

    def _note_problem(self, kind: str) -> None:
        """One warning at most every 30 seconds, naming only the kind of problem."""
        now = time.monotonic()
        if now - self._last_warning >= 30.0:
            self._last_warning = now
            log_warning(f"Coding member: a request could not be served ({kind}).")

    # --------------------------------------------------------------------- control
    def assign(self, tier: Any) -> None:
        """
        Called with each heartbeat answer's ``coding_tier``; converges in the background and
        never blocks. ``None`` means this member should not serve. Each call retries whatever is
        not done yet, except a tier that is backing off.
        """
        wanted = self._effective(_wanted(tier))
        with self._lock:
            if self._stopped.is_set():
                return
            self.target = wanted
            if self._busy or not self._available():
                return          # busy, or yielding / settling: nothing is started until it is over
            alive = self._alive()
            if wanted is None and self.process is None:
                return
            if alive and self.ready and self._ready_since is not None \
                    and time.monotonic() - self._ready_since >= STABLE_SECONDS:
                self._backoff.pop(self.serving, None)      # it has proved itself: forget earlier failures
            if wanted is not None:
                if wanted["id"] == self.serving and alive and self.ready and not self._needs_measure():
                    return
                if self._held_back(wanted["id"]):
                    return
            self._busy = True
        threading.Thread(target=self._converge, name="dlne-coding-member", daemon=True).start()

    def stop(self) -> None:
        """Close the proxy and stop llama-server. Final: a stopped member ignores assign()."""
        self._stopped.set()
        server, thread = self._server, self._thread
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=10)
            if thread.is_alive():
                server.force_exit = True
                thread.join(timeout=2)
        self._stop_engine()

    def _stop_engine(self) -> None:
        with self._engine_lock:
            proc, self.process, self.ready = self.process, None, False
            self._ready_since = None
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            if proc is not None:
                _forget_recorded_server(self.base_dir, proc.pid)
            log, self._log = self._log, None
            if log is not None:
                log.close()

    def _held_back(self, tier_id: str) -> bool:
        entry = self._backoff.get(tier_id)
        return entry is not None and time.monotonic() < entry[1]

    def _effective(self, wanted: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """What to serve toward: the tier the coordinator named, unless this machine has already replaced
        it (or what replaced it) with a smaller one."""
        if wanted is None:
            return None
        tier_id, seen = wanted["id"], set()
        while tier_id in self._replacement and tier_id not in seen:
            seen.add(tier_id)
            tier_id = self._replacement[tier_id]
        return wanted if tier_id == wanted["id"] else {"id": tier_id}

    def _failed(self, tier_id: str, lasting: bool = False) -> None:
        """Back off from a tier: a minute, doubling; ``lasting`` (a retry cannot help for a while) starts at ten."""
        count = self._backoff.get(tier_id, (0, 0.0))[0] + 1
        delay = min(BACKOFF_MAX_SECONDS, max(BACKOFF_LASTING_SECONDS if lasting else 0.0,
                                             BACKOFF_FIRST_SECONDS * 2 ** (count - 1)))
        self._backoff[tier_id] = (count, time.monotonic() + delay)
        log_warning(f"Coding member: will try that model again in {int(delay)} s.")

    def _converge(self) -> None:
        try:
            with self._lock:
                wanted = self.target
            if not self._available():
                return
            if wanted is None:
                self._drain()
                self._stop_engine()
                self._forget_server()
                return
            tier_id = wanted["id"]
            if self.process is not None and not self._alive() and not self._stopped.is_set():
                self._note_exit()
                if self._held_back(tier_id):
                    return
            if self._alive() and tier_id == self.serving:
                if not self.ready:
                    if self._wait_ready() is None:
                        self._note_exit()
                elif self._needs_measure():
                    self._join_test(self.process)
                return
            tier = self._resolve(wanted)
            if tier is None:
                return
            tier = self._fit(tier)
            if tier is None:
                self._failed(tier_id)
                return
            if tier["id"] != tier_id:
                # Smaller than what was asked: the next heartbeat's assignment resolves to it as well.
                tier_id = tier["id"]
                if self._alive() and tier_id == self.serving:
                    return
            # The running server, if any, keeps answering until the new model is in place.
            path = self._ensure_model(tier)
            if path is not None:
                self._start(tier, path)
        except Exception as exc:  # noqa: BLE001 - reported by class and retried on the next assignment
            log_error(f"Coding member: {type(exc).__name__} while changing models.")
        finally:
            with self._lock:
                self._busy = False

    def _forget_server(self) -> None:
        self.serving = self.model = None
        self.decode_tps = self.prefill_tps = 0.0

    def _note_exit(self) -> None:
        """llama-server died on its own: count it against its tier unless it had been serving for a while."""
        if self._stopped.is_set():
            return
        tier_id, ready_since = self.serving, self._ready_since
        code = self.process.returncode if self.process is not None else None
        log_warning(f"The coding model server stopped (exit code {code}); see logs/coding_server.log.")
        self._stop_engine()
        self._forget_server()
        stable = ready_since is not None and time.monotonic() - ready_since >= STABLE_SECONDS
        if tier_id is not None:
            if stable:
                self._backoff.pop(tier_id, None)
            else:
                self._failed(tier_id)

    def _drain(self) -> None:
        """Let the requests in flight finish (no new ones are accepted) before the server stops."""
        if not self._alive():
            return
        self.ready = False
        end = time.monotonic() + DRAIN_SECONDS
        while self._inflight and time.monotonic() < end and not self._stopped.is_set():
            time.sleep(0.2)

    # --------------------------------------------------------------------- which model
    def _resolve(self, wanted: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """The signed release's tier for what the coordinator asked, or None (refused, with a backoff)."""
        tier_id = wanted["id"]
        signed = next((t for t in self.tiers_loader() if t.get("id") == tier_id), None)
        problem = None
        if signed is None:
            problem = "its signed release does not list that tier"
        elif not signed.get("enabled"):
            problem = "its signed release does not enable that tier"
        elif not _same_files(wanted, signed):
            problem = "the tier's files differ from the signed release"
        if problem is not None:
            log_error(f"The coordinator assigned the coding tier {tier_id[:64]!r}, but {problem}; ignored.")
            self._failed(tier_id)
            return None
        return signed

    def _fit(self, tier: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        ``tier``, or a smaller one when this machine's own figures (the GPU memory, and the RAM it can really
        spare right now: the coordinator only sees the total) say ``tier`` will not run well here. Checked
        before anything is downloaded, so a member never fetches 29 GB to find out. The smartest tier that
        engine.coding_member_select.candidate_tiers allows and that is no bigger than the one asked for; the
        assignment stands when the machine has no figures, no tier fits (then the join test is the judge) or
        the asked-for tier fits.

        The figures already carry the volunteer's caps (``max_vram_gb``, ``max_ram_gb``). When a cap is in
        force (``"limited"``) and no tier fits inside it, the answer is None: the member does not run a model
        that would take more memory than it was allowed, whatever the join test might say.
        """
        if self.machine_provider is None:
            return tier
        try:
            machine = dict(self.machine_provider())
            limits = self.limits_provider() if self.limits_provider is not None else {}
            ladder = self.tiers_loader()
            extra = {key: value for key, value in machine.items() if key not in ("vram_gb", "ram_gb", "limited")}
            found = select.candidate_tiers(machine.get("vram_gb"), machine.get("ram_gb"), ladder, limits, **extra)
        except Exception:  # noqa: BLE001 - no usable figures: the assignment stands
            return tier
        if not found and machine.get("limited"):
            log_error(f"{tier['name']} does not fit within the memory limits set for this machine; not run.")
            return None
        order = [t.get("id") for t in ladder]
        if not found or tier["id"] not in order:
            return tier
        index = order.index(tier["id"])
        fits = [t for t in found if t.get("id") in order and order.index(t["id"]) <= index]
        if not fits or fits[0].get("id") == tier["id"]:
            return tier
        smaller = fits[0]
        log_system(f"{tier['name']} is more than this machine can spare memory for; serving {smaller['name']} instead.")
        self._replacement[tier["id"]] = smaller["id"]
        return smaller

    def _ensure_model(self, tier: Dict[str, Any]) -> Optional[str]:
        """The path of the tier's first file once every file is in place and verified, else None."""
        models = os.path.join(self.base_dir, "models")
        os.makedirs(models, exist_ok=True)
        parts = _model_parts(tier)

        def intact(part: Dict[str, Any]) -> bool:
            return sha256_ok(os.path.join(models, part["file"]), part["sha256"], int(part["size"]))

        missing = [part for part in parts if not intact(part)]
        path = os.path.join(models, parts[0]["file"])
        if not missing:
            return path
        needed = sum(int(part["size"]) for part in missing)
        free = shutil.disk_usage(models).free
        if free < needed * 1.05:
            log_error(f"Not enough disk space for {tier['name']}: it needs {needed / 1e9:.1f} GB, "
                      f"{free / 1e9:.1f} GB is free in {models}.")
            self._failed(tier["id"])
            return None
        log_system(f"Downloading {tier['name']} for the coding pool ({needed / 1e9:.1f} GB, once)...")
        try:
            if self._stopped.is_set():
                return None
            result = self._download_files(missing, models, tier["name"])
        except Exception as exc:  # noqa: BLE001 - reported by class; backs off
            log_error(f"Downloading {tier['name']} failed: {type(exc).__name__}.")
            self._failed(tier["id"])
            return None
        if self._stopped.is_set():
            return None
        if not result:
            # The downloader already retried what could be retried; ``lasting`` says a retry cannot help for now.
            reason = getattr(result, "reason", "")
            log_error(f"Downloading {tier['name']} failed" + (f": {reason}." if reason else "."))
            self._failed(tier["id"], lasting=bool(getattr(result, "lasting", False)))
            return None
        bad = [part for part in parts if not intact(part)]
        if bad:
            log_error(f"{bad[0]['file']} does not match the signed release; deleted.")
            for part in bad:
                with contextlib.suppress(OSError):
                    os.remove(os.path.join(models, part["file"]))
            self._failed(tier["id"])
            return None
        log_success(f"{tier['name']} downloaded and verified.")
        return path

    def _download_files(self, parts: List[Dict[str, Any]], dest: str, name: str) -> Any:
        """engine.downloader.fetch_verified: resumable, every part checked, nothing half-written. Returns its
        DownloadResult (true when every file is in place; ``reason`` and ``lasting`` say what went wrong).
        Stopping this member sets the Event it is given, which ends the download (the .part is kept)."""
        fetch = self._download
        if fetch is None:
            from engine.downloader import fetch_verified as fetch
        reached = [0]

        def progress(update: Dict[str, Any]) -> None:
            total = update.get("total") or 0
            if update.get("phase") == "downloading" and total:
                tenth = int(update.get("done", 0)) * 10 // total
                if tenth > reached[0]:
                    reached[0] = tenth
                    log_system(f"  {name}: {tenth * 10}%")

        return fetch(parts, dest, progress, cancel=self._stopped)

    # --------------------------------------------------------------------- server
    def _binary(self) -> Optional[str]:
        for name in ("llama-server.exe", "llama-server"):
            candidate = os.path.join(self.base_dir, "bin", name)
            if os.path.exists(candidate):
                if not name.endswith(".exe"):
                    with contextlib.suppress(OSError):
                        os.chmod(candidate, 0o755)
                return candidate
        return None

    def _start(self, tier: Dict[str, Any], model_path: str) -> None:
        """Serve ``tier``. A server that cannot start, or dies starting, puts its tier into backoff;
        one that is still loading is checked again by later heartbeats."""
        binary = self._binary()
        if binary is None:
            log_error("This engine has no llama-server; the coding pool needs the engine files of the coordinator's release.")
            self._failed(tier["id"])
            return
        # "Both": the phone-pool model of this machine loads first. Its RAM is taken before this one reads what is free.
        gate = self.start_gate
        if gate is not None:
            end = time.monotonic() + GATE_WAIT_SECONDS
            while not gate.is_set() and not self._stopped.is_set() and time.monotonic() < end:
                gate.wait(1.0)
            if self._stopped.is_set() or not self._available():
                return
        # What this machine can spare is read before the model is loaded (afterwards the model's own
        # memory would count against it); a step-down after a slow join test is judged on this.
        self._machine = None
        if self.machine_provider is not None:
            try:
                self._machine = dict(self.machine_provider())
            except Exception:  # noqa: BLE001 - no figures, no step-down
                self._machine = None
        self._drain()
        # The volunteer's limits (CPU threads, GPU memory, RAM) as launch settings, read now so an edit takes effect.
        resources: Dict[str, Any] = {}
        if self.resources_provider is not None:
            try:
                resources = dict(self.resources_provider() or {})
            except Exception as exc:  # noqa: BLE001 - without them the server would ignore the limits: do not start
                log_error(f"The volunteer's limits could not be read ({type(exc).__name__}); the coding model is not started.")
                self._failed(tier["id"])
                return
        with self._engine_lock:
            if self._stopped.is_set() or not self._available():
                return          # stopped, or the owner's load began while this was being prepared
            self._stop_engine()
            self.llama_port = self._fixed_llama_port or _free_port()
            ctx = tier.get("context")
            ctx = ctx if isinstance(ctx, int) and ctx >= 2048 else DEFAULT_CONTEXT
            ctx -= ctx % self.slots
            model_arg, workdir = _model_argument(model_path)
            threads = resources.get("threads", self.threads)
            args = coding_server_args(binary, model_arg, self.llama_port, ctx, self.slots,
                                      threads if isinstance(threads, int) and threads > 0 else 0,
                                      extra=[str(a) for a in resources.get("extra_args", [])],
                                      cache_ram_mib=int(resources.get("cache_ram_mib", PROMPT_CACHE_MIB)))
            problems = no_trace.no_trace_violations(args)
            if problems:
                # Never run, and never claim "no_trace", with settings that would keep a prompt.
                log_error("The coding model server would not run without a trace: " + "; ".join(problems) + ".")
                self._failed(tier["id"])
                return
            log_dir = os.path.join(self.base_dir, "logs")
            os.makedirs(log_dir, exist_ok=True)
            # Truncated at every start: the log only ever describes the current run.
            self._log = open(os.path.join(log_dir, "coding_server.log"), "w")
            # LLAMA_ARG_* variables in the volunteer's shell could switch logging back on unseen.
            env = no_trace.no_trace_env(self.env_provider() if self.env_provider is not None else os.environ)
            # Below normal priority (the owner's own programs come first on their machine), and on
            # Windows no console window either; see _child_creation_flags.
            self.process = subprocess.Popen(
                args, stdin=subprocess.DEVNULL, stdout=self._log, stderr=subprocess.STDOUT, env=env,
                cwd=workdir, creationflags=_child_creation_flags())
            if _is_windows() and not _bind_to_worker_lifetime(self.process):
                log_warning("Coding member: the model server could not be tied to this worker, so if the worker is "
                            "ended abruptly it may keep running until the next start.")
            _remember_server(self.base_dir, self.process)
            if hasattr(os, "setpriority"):
                with contextlib.suppress(OSError):
                    os.setpriority(os.PRIO_PROCESS, self.process.pid, 10)
            self.serving, self.model, self._tier = tier["id"], tier["file"], tier
            self.decode_tps = self.prefill_tps = 0.0
            self._measure_after, self._measure_failures = 0.0, 0
        if self._wait_ready() is None:
            self._note_exit()

    def _wait_ready(self) -> Optional[bool]:
        """Poll llama-server's /health: True when ready (and measured), False when still loading at
        the deadline or when stopped, None when the process died."""
        end = time.monotonic() + self.ready_timeout
        while not self._stopped.is_set() and not self._yielding:
            process = self.process
            if process is None or process.poll() is not None:
                return None
            try:
                if httpx.get(f"{self.engine_url}/health", timeout=2.0, trust_env=False).status_code == 200:
                    self.ready = True
                    self._ready_since = time.monotonic()
                    log_success(f"Serving {self.model} to the coding pool.")
                    self._join_test(process)
                    return True
            except httpx.HTTPError:
                pass            # not up yet
            if time.monotonic() >= end:
                log_warning("The coding model server is still loading; it is checked again with the next heartbeat.")
                break
            time.sleep(self.poll_seconds)
        return False

    def _needs_measure(self) -> bool:
        """The join test has not produced numbers yet and its retry time has come."""
        return self.decode_tps == 0.0 and self.prefill_tps == 0.0 and time.monotonic() >= self._measure_after

    def _join_test(self, process: Optional[subprocess.Popen]) -> None:
        """Measure this machine once per server: writing and reading speed, from llama.cpp's timings.
        Until it works the member reports zero speeds and so gets no traffic; it is retried with backoff."""
        measure = self._measure
        if measure is None:
            from engine.coding_member_select import measure
        try:
            if inspect.iscoroutinefunction(measure):
                async def run() -> Any:
                    async with httpx.AsyncClient(timeout=600.0, trust_env=False) as client:
                        return await measure(self.engine_url, client)
                result = asyncio.run(run())
            else:
                with httpx.Client(timeout=600.0, trust_env=False) as client:
                    result = measure(self.engine_url, client)
            decode, prefill = result
        except Exception as exc:  # noqa: BLE001 - reported by class only
            self._measure_failures += 1
            self._measure_after = time.monotonic() + min(600.0, 30.0 * 2 ** (self._measure_failures - 1))
            if not self._stopped.is_set():
                log_warning(f"Coding member: the speed test failed ({type(exc).__name__}); not offering this machine yet.")
            return
        if self.process is process:
            self.decode_tps, self.prefill_tps = _speed(decode), _speed(prefill)
            if self.decode_tps == 0.0 and self.prefill_tps == 0.0:
                self._measure_failures += 1
                self._measure_after = time.monotonic() + min(600.0, 30.0 * 2 ** (self._measure_failures - 1))
            else:
                log_system(f"Coding member speed: {self.decode_tps} tok/s writing, {self.prefill_tps} tok/s reading.")
                self._check_floors()

    def _check_floors(self) -> None:
        """A server measured under the speed floors would never be routed to. When this machine's figures are
        known, take the next smaller tier instead (the next heartbeat's assignment picks it up through
        _effective), at most MAX_STEP_DOWNS times; otherwise keep serving and let the speeds speak."""
        limits = self.limits_provider() if self.limits_provider is not None else {}     # {}: the default floors
        if select.meets_floor(self.decode_tps, self.prefill_tps, limits) or self._tier is None:
            return
        tier, machine = self._tier, self._machine
        log_warning(f"{self.model} measured {self.decode_tps} tok/s writing and {self.prefill_tps} tok/s reading on this "
                    "machine, under the speed floors.")
        if machine is None or self._step_downs >= select.MAX_STEP_DOWNS:
            return
        extra = {key: value for key, value in machine.items() if key not in ("vram_gb", "ram_gb", "limited")}
        smaller = select.next_tier_down(tier, machine.get("vram_gb"), machine.get("ram_gb"), self.tiers_loader(),
                                        limits, **extra)
        if smaller is None:
            log_warning("There is no smaller coding model for this machine to try.")
            return
        self._step_downs += 1
        self._replacement[tier["id"]] = smaller["id"]
        log_system(f"Trying {smaller['name']} instead.")


def _wanted(tier: Any) -> Optional[Dict[str, Any]]:
    """What the heartbeat answer asked for, as a dict with an ``id``, or None for nothing."""
    if isinstance(tier, str) and tier:
        return {"id": tier}
    if isinstance(tier, dict) and isinstance(tier.get("id"), str) and tier["id"]:
        return tier
    return None


def _same_files(wanted: Dict[str, Any], signed: Dict[str, Any]) -> bool:
    """A tier the coordinator sends may only restate the signed tier's files, never change them."""
    if not any(key in wanted for key in ("file", "files", "sha256", "size")):
        return True       # it names the tier by id only

    def key(tier: Dict[str, Any]) -> List[Tuple[Any, Any, Any]]:
        return [(part.get("file"), part.get("sha256"), _int(part.get("size"))) for part in _model_parts(tier)]

    return key(wanted) == key(signed)


def _int(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class _Slot:
    """One of the member's request slots, taken until released (releasing twice is harmless)."""

    def __init__(self, member: CodingMember) -> None:
        self._member = member
        self._done = False

    def release(self) -> None:
        if not self._done:
            self._done = True
            self._member._release()


# ======================================================================================
# The member proxy
# ======================================================================================

class _ClientGone(Exception):
    """The caller hung up before the model answered."""


class _Cleanup:
    """Everything a request holds: its slot, the upstream response and the client. Idempotent."""

    def __init__(self, slot: _Slot, client: httpx.AsyncClient) -> None:
        self.slot = slot
        self.client = client
        self.response: Optional[httpx.Response] = None

    async def run(self) -> None:
        self.slot.release()          # first and without waiting: a cancelled request must free its slot
        try:
            with anyio.CancelScope(shield=True):
                if self.response is not None:
                    await self.response.aclose()
                await self.client.aclose()
        except Exception:  # noqa: BLE001 - nothing useful to do about a close that fails
            pass


def _error(status: int, kind: str, message: str, retry_after: Optional[int] = None) -> JSONResponse:
    """A proxy-made error. Fixed sentences only: it never repeats anything from a request."""
    headers = {"Retry-After": str(retry_after)} if retry_after else None
    return JSONResponse({"error": {"message": message, "type": kind, "code": status}},
                        status_code=status, headers=headers)


async def _read_body(request: Request) -> Optional[bytes]:
    """The request body, or None when it is larger than MAX_BODY_BYTES."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return None
    chunks: List[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _wrap(sealed: Dict[str, Any]) -> Dict[str, Any]:
    """A sealed piece as it goes on the wire: ``{"dlne_e2e": {...}}``."""
    return sealed if "dlne_e2e" in sealed else {"dlne_e2e": sealed}


def _event(sealed: Dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(_wrap(sealed), separators=(",", ":")).encode("ascii") + b"\n\n"


async def _send_unless_gone(client: httpx.AsyncClient, upstream: httpx.Request, request: Request) -> httpx.Response:
    """Send to llama-server and wait for its answer's headers, giving up when the caller hangs up
    (a prompt can take minutes to read, and nobody should be generated for after that)."""
    task = asyncio.ensure_future(client.send(upstream, stream=True))
    task.add_done_callback(lambda t: t.cancelled() or t.exception())   # never "exception was never retrieved"
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=0.5)
            if done:
                return task.result()
            if await request.is_disconnected():
                raise _ClientGone()
    finally:
        if not task.done():
            task.cancel()


WARM_UP_TIMEOUT = httpx.Timeout(20.0, connect=5.0)


async def _warm_up(client: httpx.AsyncClient, member: "CodingMember", path: str, body: Dict[str, Any],
                   request: Request) -> None:
    """
    Ghost text (a fill-in-the-middle completion) is warmed up first, as the coordinator does for its own
    engine (engine/fim_warm.py): the text before the cursor goes to llama-server as a request of its own
    so the engine's state is parked there, and the real request then reads only what follows the cursor.
    Best effort (20 s, any failure is ignored) and it is part of the same request: same slot, nothing of it
    is returned. A caller that hangs up meanwhile still ends the request.
    """
    warm = fim_warm_body(body) if path == "/v1/completions" else None
    if warm is None:
        return
    try:
        upstream = client.build_request("POST", member.engine_url + path, content=json.dumps(warm).encode("utf-8"),
                                        headers={"content-type": "application/json"}, timeout=WARM_UP_TIMEOUT)
        response = await _send_unless_gone(client, upstream, request)
        try:
            await response.aread()
        finally:
            await response.aclose()
    except _ClientGone:
        raise
    except Exception:  # noqa: BLE001 - the real request is complete without it
        pass


async def _relay(response: httpx.Response, cleanup: _Cleanup):
    try:
        async for chunk in response.aiter_raw():
            yield chunk
    finally:
        await cleanup.run()


async def _relay_sealed(response: httpx.Response, sealer: Any, cleanup: _Cleanup):
    """Each ``data:`` line's payload becomes one sealed event. The end of the model's answer
    (``[DONE]``) becomes the sealed end marker followed by the plain ``[DONE]``; a stream that
    breaks off before that has no sealed end, and the editor treats it as cut."""

    def event(line: bytes) -> Optional[bytes]:
        line = line.rstrip(b"\r")
        if not line.startswith(b"data:"):
            return None
        payload = line[5:]
        if payload.startswith(b" "):
            payload = payload[1:]
        if payload.strip() == b"[DONE]":
            return _event(sealer.finish()) + b"data: [DONE]\n\n"
        return _event(sealer.seal(payload))

    pending = b""
    try:
        async for chunk in response.aiter_raw():
            pending += chunk
            while True:
                end = pending.find(b"\n")
                if end < 0:
                    break
                line, pending = pending[:end], pending[end + 1:]
                out = event(line)
                if out is not None:
                    yield out
        if pending.strip():
            out = event(pending)
            if out is not None:
                yield out
    finally:
        await cleanup.run()


async def _proxy_request(member: CodingMember, request: Request, path: str) -> Response:
    if not member.is_ready():
        return _error(503, "not_ready", "this member's model is not ready yet", retry_after=5)
    raw = await _read_body(request)
    if raw is None:
        return _error(413, "invalid_request", "the request body is too large")
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        return _error(400, "invalid_request", "the request body is not valid JSON")
    if not isinstance(body, dict):
        return _error(400, "invalid_request", "the request body must be a JSON object")

    resp_key: Optional[bytes] = None
    payload = raw
    request_body: Dict[str, Any] = body              # what llama-server will be asked: the plain body, or the opened one
    if "dlne_e2e" in body:
        try:
            # The key ring tries the current key, then the previous one, and refuses a client key it has seen.
            opened, resp_key = member.keyring.open_request(body)
            if not isinstance(opened, dict):
                raise ValueError
            payload = json.dumps(opened).encode("utf-8")
            request_body = opened
        except Exception:  # noqa: BLE001 - any failure to open is the same answer; the reason could hint at the content
            return _error(400, "invalid_envelope", "the encrypted request could not be opened")

    slot = member._reserve()
    if slot is None:
        return _error(503, "busy", "all of this member's slots are busy", retry_after=2)
    client = httpx.AsyncClient(timeout=httpx.Timeout(UPSTREAM_READ_TIMEOUT, connect=5.0, pool=5.0), trust_env=False)
    cleanup = _Cleanup(slot, client)
    try:
        await _warm_up(client, member, path, request_body, request)        # best effort, inside this request's slot
        upstream = client.build_request("POST", member.engine_url + path, content=payload,
                                        headers={"content-type": "application/json"})
        response = await _send_unless_gone(client, upstream, request)
        cleanup.response = response
    except _ClientGone:
        await cleanup.run()
        return Response(status_code=499)
    except Exception as exc:  # noqa: BLE001 - llama-server is down or broke off; only the class is noted
        await cleanup.run()
        member._note_problem(type(exc).__name__)
        return _error(502, "engine_unavailable", "this member's model server did not answer")
    except BaseException:
        await cleanup.run()        # cancelled (the proxy is shutting down): free the slot, then carry on
        raise

    content_type = response.headers.get("content-type", "application/json")
    if resp_key is None:
        return StreamingResponse(_relay(response, cleanup), status_code=response.status_code,
                                 headers={"content-type": content_type}, background=BackgroundTask(cleanup.run))
    if response.status_code != 200:
        # An encrypted request gets no unsealed word of llama-server's error back: it can quote the
        # prompt. The status code says what happened; the text is ours.
        await cleanup.run()
        status = response.status_code if 400 <= response.status_code <= 599 else 502
        return _error(status, "engine_error", "this member's model server could not answer the request")
    sealer = ResponseSealer(resp_key)
    if content_type.startswith("text/event-stream"):
        return StreamingResponse(_relay_sealed(response, sealer, cleanup), status_code=200,
                                 headers={"content-type": "text/event-stream", "cache-control": "no-cache"},
                                 background=BackgroundTask(cleanup.run))
    try:
        sealed = sealer.seal(await response.aread())      # one sealed answer, no end marker
    except Exception as exc:  # noqa: BLE001 - the answer broke off; nothing of it is repeated
        member._note_problem(type(exc).__name__)
        return _error(502, "engine_unavailable", "this member's model server did not answer")
    finally:
        await cleanup.run()
    return JSONResponse(_wrap(sealed), status_code=200)


_LOOPBACK_NAMES = ("127.0.0.1", "localhost", "[::1]")


def _host_is_loopback(value: str) -> bool:
    """A ``Host`` header that names this machine: 127.0.0.1, localhost or [::1], with a port or without.
    Which port is not checked: the coordinator reaches the member through its relay port, and the tunnel
    passes the bytes (and so the coordinator's ``Host: 127.0.0.1:<relay port>``) on unchanged."""
    host = value.strip().lower()
    if host.startswith("["):
        name, _, rest = host.partition("]")
        name += "]"
        if rest and not rest.startswith(":"):
            return False
        port = rest[1:]
    else:
        name, _, port = host.partition(":")
    if name not in _LOOPBACK_NAMES:
        return False
    return not port or (port.isascii() and port.isdigit() and 1 <= int(port) <= 65535)


async def _only_the_tunnel(request: Request) -> None:
    """
    SR-08: a page in the owner's browser can send a POST to 127.0.0.1:<the member's port> (and a page on
    a host name that resolves to 127.0.0.1 can do it as "same origin"). The coordinator's requests come
    through the reverse tunnel from a program, and carry neither an ``Origin`` header nor any Host but a
    loopback one. A browser always sends ``Origin`` with a cross-site POST, and a rebound name shows in
    ``Host``, so either one is turned away before the body is read. (A dependency, not middleware:
    BaseHTTPMiddleware breaks the disconnect detection the proxy relies on.)
    """
    if "origin" in request.headers or not _host_is_loopback(request.headers.get("host", "")):
        raise HTTPException(status_code=421, detail="this member answers its coordinator only")


def make_proxy_app(member: CodingMember) -> FastAPI:
    """The member proxy: two POST routes to llama-server and a health check, nothing else."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, dependencies=[Depends(_only_the_tunnel)])

    async def guarded(request: Request, path: str) -> Response:
        try:
            return await _proxy_request(member, request, path)
        except Exception as exc:  # noqa: BLE001 - never let an exception (and what it might quote) escape
            member._note_problem(type(exc).__name__)
            return _error(500, "member_error", "the member could not handle the request")

    @app.get("/health")
    async def health() -> Response:
        status = member._block()
        # trust_id is for the owner's own eyes (the editor reads it from this loopback port): it is
        # derived from the identity key the heartbeat's block carries, and is not part of that block.
        body = {"status": "ok" if status["ready"] else "loading", "ready": status["ready"],
                "slots": status["slots"], "busy": status["busy"], "trust_id": member.trust_id}
        return JSONResponse(body, status_code=200 if status["ready"] else 503)

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        return await guarded(request, "/v1/chat/completions")

    @app.post("/v1/completions")
    async def completions(request: Request) -> Response:
        return await guarded(request, "/v1/completions")

    return app
