"""
Running the worker, and stopping it for good. This is the Python twin of vscode-extension/src/worker/workerManager.ts:

    ask the coordinator its version, fetch and verify the signed engine and the llama.cpp programs, write config.txt,
    run the worker, read its markers, follow a version change (exit 75), report a refusal (exit 78), and apply a changed
    slider by rewriting config.txt, which the running worker re-reads.

The worker is a child process of this app, started from the VERIFIED engine folder and from nothing else: ``start`` runs
:meth:`WorkerController._prepare` first, which returns a :class:`installer.VerifiedEngine` only after every signature and
hash check, and the spawn takes one. Stopping asks the worker to shut down (it stops its own model server), gives it a
few seconds, then kills whatever is left of its process tree and any program running from this app's engine folder, so
no worker, llama-server or ggml-rpc-server outlives it.
"""

import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Optional

from . import COORDINATOR_URL, PUBLIC_NETWORK_KEY, configfile, installer, markers, paths, tiers

# Two automatic re-syncs within ten minutes (the extension's rule); after that a mismatch is reported, not looped on.
MAX_VERSION_RESTARTS = 2
VERSION_RESTART_WINDOW_S = 10 * 60
# How long a stopped worker has to shut its coding model server down before it is killed (Linux; Windows has no signal).
STOP_GRACE_S = 8.0
STOP_GRACE_WINDOWS_S = 1.0

IDLE, PREPARING, STARTING, DONATING, STOPPING, ERROR = "idle", "preparing", "starting", "donating", "stopping", "error"


class _Cancelled(Exception):
    """Stop was pressed while the engine was downloading."""


TOOLTIP_MAX = 127      # Windows' limit for a tray icon's hover text (NOTIFYICONDATA.szTip is 128 characters with its end mark)


def fit_tooltip(text: str, limit: int = TOOLTIP_MAX) -> str:
    """``text`` as it is when it fits, otherwise cut at a word boundary (never inside a word) with an ellipsis, within ``limit``."""
    if len(text) <= limit:
        return text
    room = text[:limit - 1]
    if text[limit - 1].isspace():                 # the cut falls exactly between two words
        cut = room
    else:
        boundary = max(room.rfind(" "), room.rfind("\n"))
        cut = room[:boundary] if boundary > 0 else room        # one very long word: cut it
    return cut.rstrip(" \n,;:.-") + "…"


@dataclass(frozen=True)
class Status:
    state: str = IDLE
    message: str = "Not donating."
    role: Optional[markers.Role] = None
    also_cpu: bool = False
    dock: Optional[markers.Dock] = None         # docked (wired + mains)? from the worker's DLNE_DOCK line (2.5.0)
    yielding: Optional[markers.Yield] = None    # stepping aside for the owner's own load? DLNE_YIELD (2.5.0)
    progress: Optional[float] = None      # 0..1 while something is downloading
    refusal: str = ""
    clock_notice: str = ""
    activity: str = ""                    # "Downloading Qwen3 4B Instruct 2507: 40%", "Serving ..." (from the worker's log)
    error: str = ""                       # set in the ERROR state
    verification_failed: bool = False     # the release could not be verified: nothing was installed or run
    engine_version: str = ""
    devices: str = ""                     # "GPU", "CPU" or "GPU and CPU"

    @property
    def running(self) -> bool:
        return self.state in (PREPARING, STARTING, DONATING, STOPPING)

    @property
    def tooltip(self) -> str:
        """The tray icon's hover text: what it is doing, in one or two short lines."""
        if self.state == DONATING:
            doing = markers.role_title(self.role) if self.role else "Joined the swarm"
            if self.also_cpu:
                doing += " + phone app"
            if self.yielding is not None and self.yielding.yielding:
                doing += " · stepping aside for you"
            if self.activity.startswith("Downloading"):
                doing += f"\n{self.activity}"
            return fit_tooltip(f"DLNE Donate: donating\n{doing}")
        if self.state == ERROR:
            return fit_tooltip(f"DLNE Donate: problem\n{self.error or self.message}")
        if self.state in (PREPARING, STARTING):
            return fit_tooltip(f"DLNE Donate: starting\n{self.message}")
        if self.state == STOPPING:
            return "DLNE Donate: stopping"
        return "DLNE Donate: idle"


def worker_command(engine: "installer.VerifiedEngine", coordinator_url: str) -> List[str]:
    """
    The command that runs the worker. Only a VerifiedEngine can be passed. The app runs itself in ``--run-worker`` mode
    (see run_worker_process): a frozen app has no Python to hand the engine to, so it carries the engine's libraries, and
    the engine's own source comes from the verified folder.
    """
    if not installer.is_verified(engine):
        raise installer.VerificationError("Refusing to run an engine that was not verified.")
    if getattr(sys, "frozen", False):
        base = [sys.executable]
    else:
        base = [sys.executable, "-m", "dlne_donate"]
    return base + ["--run-worker", engine.root, "--", "worker", "--coordinator", coordinator_url]


def worker_environment(extra_vulkan_indices: str = "") -> Dict[str, str]:
    """The environment of the worker: this app's own, plus the settings the extension also sets for the GPU backends."""
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env[PARENT_ENV] = str(os.getpid())
    env.setdefault("COLUMNS", "220")     # the engine's log wraps at 80 columns when it is not a terminal, which cuts its lines in two
    env["GGML_VK_PREVENT_BARRIERS"] = "1"
    env["RADV_PERFTEST"] = "nogttspill"
    if extra_vulkan_indices and not env.get("GGML_VK_VISIBLE_DEVICES"):
        env["GGML_VK_VISIBLE_DEVICES"] = extra_vulkan_indices
    if not getattr(sys, "frozen", False):
        package_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env["PYTHONPATH"] = package_parent + os.pathsep + env.get("PYTHONPATH", "")
    return env


def discrete_vulkan_indices(summary: str) -> str:
    """
    On a computer with both an integrated and a discrete GPU, the discrete ones' Vulkan indices ("0" or "0,1"), so llama.cpp
    does not pick the integrated one (workerManager.ts ``detectDiscreteVulkanIndices``); "" when that does not apply.
    """
    discrete: List[str] = []
    integrated: List[str] = []
    current = ""
    for raw in summary.splitlines():
        line = raw.strip()
        if line.startswith("GPU") and line.endswith(":"):
            number = line[3:-1]
            if number.isdigit():
                current = number
        elif line.startswith("deviceType") and "=" in line and current != "":
            kind = line.split("=", 1)[1].strip()
            if "DISCRETE_GPU" in kind:
                discrete.append(current)
            elif "INTEGRATED_GPU" in kind:
                integrated.append(current)
    return ",".join(discrete) if discrete and integrated else ""


def _detect_vulkan_indices() -> str:
    try:
        flags = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
        done = subprocess.run(["vulkaninfo", "--summary"], capture_output=True, text=True, timeout=4, **flags)
        return discrete_vulkan_indices(done.stdout)
    except (OSError, subprocess.SubprocessError):
        return ""


def terminate_tree(process: Any, grace_s: float, engine_dir: str = "", psutil_module: Any = None) -> None:
    """
    End ``process`` and everything it started. The worker is asked to stop (SIGTERM; it then stops its model server itself)
    and has ``grace_s`` seconds; whatever is still alive is killed, children first. Then any program that runs from
    ``engine_dir`` (an orphaned llama-server or ggml-rpc-server, matched by its full path, never by name) is killed too.
    """
    psutil = psutil_module
    if psutil is None:
        import psutil as psutil  # type: ignore[no-redef]
    family: List[Any] = []
    try:
        parent = psutil.Process(process.pid)
        family = parent.children(recursive=True)
    except Exception:  # noqa: BLE001 - already gone
        parent = None
    try:
        process.terminate()
    except OSError:
        pass
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline and process.poll() is None:
        time.sleep(0.1)
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=5)
    except Exception:  # noqa: BLE001
        pass
    # Children get a moment to follow their parent, then are killed.
    if parent is not None:
        try:
            family += [c for c in parent.children(recursive=True) if c not in family]
        except Exception:  # noqa: BLE001
            pass
    for child in family:
        try:
            if child.is_running():
                child.terminate()
        except Exception:  # noqa: BLE001
            pass
    try:
        psutil.wait_procs(family, timeout=3)
    except Exception:  # noqa: BLE001
        pass
    for child in family:
        try:
            if child.is_running():
                child.kill()
        except Exception:  # noqa: BLE001
            pass
    if engine_dir:
        kill_programs_under(engine_dir, psutil)


def kill_programs_under(folder: str, psutil_module: Any = None) -> int:
    """Kill every process whose program lives inside ``folder`` (this app's engine folder). Returns how many were killed."""
    psutil = psutil_module
    if psutil is None:
        import psutil as psutil  # type: ignore[no-redef]
    root = os.path.normcase(os.path.abspath(folder)) + os.sep
    killed = 0
    for proc in psutil.process_iter(["pid", "exe"]):
        try:
            exe = proc.info.get("exe") or ""
            if exe and os.path.normcase(os.path.abspath(exe)).startswith(root) and proc.pid != os.getpid():
                proc.kill()
                killed += 1
        except Exception:  # noqa: BLE001
            continue
    return killed


class WorkerController:
    """The state machine behind the tray icon. All of it runs on its own threads; the window only reads ``status()``."""

    _needs_vcredist = False      # the worker said the Visual C++ Runtime is missing (markers.VCREDIST_MARKER)

    def __init__(self, home: str, get_settings: Callable[[], tiers.Settings], get_hardware: Callable[[], tiers.Hardware],
                 base_url: str = COORDINATOR_URL, network_key: str = PUBLIC_NETWORK_KEY,
                 on_change: Optional[Callable[[Status], None]] = None,
                 installer_factory: Optional[Callable[..., Any]] = None,
                 spawn: Optional[Callable[..., Any]] = None,
                 log: Optional[Callable[[str], None]] = None,
                 vulkan_indices: Optional[Callable[[], str]] = None):
        self.home = home
        self.engine_dir = paths.engine_dir(home)
        self._get_settings = get_settings
        self._get_hardware = get_hardware
        self.base_url = base_url.rstrip("/")
        self.network_key = network_key
        self._on_change = on_change or (lambda _s: None)
        self._installer_factory = installer_factory or installer.EngineInstaller
        self._spawn = spawn or subprocess.Popen
        self._log = log or (lambda _m: None)
        self._vulkan_indices = vulkan_indices or _detect_vulkan_indices
        self._lock = threading.RLock()
        self._status = Status()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._process: Optional[Any] = None
        self._restarts: List[float] = []
        self._engine: Optional[installer.VerifiedEngine] = None
        self._devices: Optional[tuple] = None     # (GPU lent, CPU lent) as they were when this donation started

    def devices_changed(self) -> bool:
        """The owner switched the GPU or the CPU on or off after donating began: that applies the next time they donate."""
        settings, hardware = self._get_settings(), self._get_hardware()
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            devices = self._devices
        return bool(running and devices is not None
                    and devices != (bool(settings.gpu_enabled and hardware.has_gpu), bool(settings.cpu_enabled)))

    # ---- status ------------------------------------------------------------------------------------

    def status(self) -> Status:
        with self._lock:
            return self._status

    def _set(self, **changes: Any) -> None:
        with self._lock:
            self._status = replace(self._status, **changes)
            status = self._status
        try:
            self._on_change(status)
        except Exception:  # noqa: BLE001 - a broken window never stops the worker
            pass

    # ---- start / stop ------------------------------------------------------------------------------

    def start(self) -> bool:
        """Begin donating. False when it is already running."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop.clear()
            self._restarts = []
            settings = self._get_settings()
            hardware = self._get_hardware()
            self._devices = (bool(settings.gpu_enabled and hardware.has_gpu), bool(settings.cpu_enabled))
            self._status = Status(state=PREPARING, message="Checking the swarm's version...",
                                  devices=tiers.device_summary(settings, hardware))
            self._thread = threading.Thread(target=self._session, name="donate-session", daemon=True)
            thread = self._thread
        self._on_change(self.status())
        thread.start()
        return True

    def stop(self, wait: bool = False, timeout: float = 15.0) -> None:
        """Stop donating. Returns at once unless ``wait``; the worker and everything it started are gone when it is done."""
        with self._lock:
            thread = self._thread
            running = thread is not None and thread.is_alive()
            if not running:
                if self._status.state in (ERROR, STOPPING):
                    self._set(state=IDLE, message="Not donating.", error="", role=None, also_cpu=False)
                return
            self._stop.set()
            if self._status.state != STOPPING:
                self._set(state=STOPPING, message="Stopping...", progress=None)
            process = self._process
        if process is not None:
            threading.Thread(target=self._terminate, args=(process,), name="donate-stopper", daemon=True).start()
        if wait and thread is not None:
            thread.join(timeout)

    def _terminate(self, process: Any) -> None:
        grace = STOP_GRACE_WINDOWS_S if sys.platform == "win32" else STOP_GRACE_S
        terminate_tree(process, grace, self.engine_dir)

    def shutdown(self) -> None:
        """The app is quitting: stop, and do not return until nothing of ours is left running."""
        self.stop(wait=True)
        kill_programs_under(self.engine_dir)

    # ---- the session -------------------------------------------------------------------------------

    def _session(self) -> None:
        try:
            while not self._stop.is_set():
                engine = self._prepare()
                if engine is None:
                    return
                code = self._run_worker(engine)
                if self._stop.is_set():
                    break
                if code == markers.VERSION_MISMATCH_EXIT_CODE:
                    now = time.monotonic()
                    self._restarts = [t for t in self._restarts if now - t < VERSION_RESTART_WINDOW_S]
                    if len(self._restarts) >= MAX_VERSION_RESTARTS:
                        self._fail("The engine still does not match the swarm after updating twice. Try again later, or update DLNE Donate.")
                        return
                    self._restarts.append(now)
                    self._set(state=PREPARING, message="The swarm runs a different version. Updating the engine...", role=None)
                    continue
                if code == markers.CPU_BELOW_MINIMUM_EXIT_CODE:
                    reason = self.status().refusal or "this computer is below the swarm's minimum."
                    self._fail(f"The swarm can't use this computer yet: {reason}")
                    return
                if self._needs_vcredist:
                    self._fail(markers.VCREDIST_TEXT)
                    return
                self._fail(f"The worker stopped unexpectedly (code {code}). See the log in {paths.log_path(self.home)}.")
                return
        finally:
            with self._lock:
                self._process = None
                if self._status.state != ERROR:
                    self._status = replace(self._status, state=IDLE, message="Not donating.", role=None, also_cpu=False,
                                           progress=None)
            self._on_change(self.status())

    def _fail(self, message: str, verification: bool = False) -> None:
        self._log(f"ERROR: {message}")
        self._set(state=ERROR, message=message, error=message, role=None, also_cpu=False, progress=None,
                  verification_failed=verification)

    def _prepare(self) -> Optional["installer.VerifiedEngine"]:
        """Fetch and verify the engine, write config.txt. None (with the status set) when the worker must not start."""
        def progress(what: str, fraction: float) -> None:
            if self._stop.is_set():
                raise _Cancelled()
            label = "engine" if what == "engine" else "llama.cpp programs"
            self._set(state=PREPARING, message=f"Downloading the {label}... {int(fraction * 100)}%", progress=fraction)

        try:
            inst = self._installer_factory(self.engine_dir, self.base_url, log=self._log, progress=progress)
            engine = inst.ensure()
        except _Cancelled:
            return None
        except installer.VerificationError as error:
            self._fail(f"{error} Nothing was run.", verification=True)
            return None
        except installer.FetchError as error:
            self._fail(f"Could not reach the swarm: {error}")
            return None
        except Exception as error:  # noqa: BLE001
            self._fail(f"Could not prepare the engine: {type(error).__name__}: {error}")
            return None
        if self._stop.is_set():
            return None
        missing = installer.missing_modules(engine.root)
        if missing:
            self._fail(f"This engine ({engine.version}) needs libraries this DLNE Donate does not have ({', '.join(missing)}). "
                       "Please update DLNE Donate from the DLNE website.")
            return None
        self._engine = engine
        self._set(engine_version=engine.version)
        self.write_config(engine)
        return engine

    def write_config(self, engine: Optional["installer.VerifiedEngine"] = None) -> bool:
        """Write the limits for the current settings into the engine's config.txt. The running worker re-reads it."""
        engine = engine or self._engine
        if engine is None:
            return False
        settings = self._get_settings()
        hardware = self._get_hardware()
        devices = self._devices
        if devices is not None:
            # Amounts follow the sliders while the worker runs; which devices are lent was fixed when it started.
            settings = replace(settings, gpu_enabled=devices[0], cpu_enabled=devices[1])
        updates = tiers.config_updates(settings, hardware, self.base_url, self.network_key)
        try:
            return configfile.update_config(engine.config_file, updates)
        except OSError as error:
            self._log(f"Could not write {engine.config_file}: {error}")
            return False

    def apply_settings(self) -> None:
        """A slider moved. Rewrite config.txt; a running worker picks the new limits up by itself."""
        if self._engine is not None:
            self.write_config(self._engine)
            with self._lock:
                running = self._thread is not None and self._thread.is_alive()
            if running:
                self._set(devices=tiers.device_summary(self._get_settings(), self._get_hardware()))

    def _run_worker(self, engine: "installer.VerifiedEngine") -> Optional[int]:
        command = worker_command(engine, self.base_url)
        env = worker_environment(self._vulkan_indices() if self._get_settings().gpu_enabled else "")
        flags: Dict[str, Any] = {}
        if sys.platform == "win32":
            flags["creationflags"] = 0x08000000 | 0x00000200      # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        else:
            flags["start_new_session"] = True
        self._log("Starting the worker: " + " ".join(command[:-2] + ["<coordinator>"]))
        self._needs_vcredist = False
        self._set(state=STARTING, message="Joining the swarm...", progress=None, role=None, also_cpu=False, refusal="",
                  activity="")
        try:
            process = self._spawn(command, cwd=engine.root, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, **flags)
        except OSError as error:
            self._fail(f"The worker could not be started: {error}")
            return None
        with self._lock:
            self._process = process
        reader = markers.OutputReader()
        pump = threading.Thread(target=self._pump, args=(process, reader), name="donate-output", daemon=True)
        pump.start()
        if self._stop.is_set():                 # stop was pressed while the process was being created
            self._terminate(process)
        code = process.wait()
        pump.join(timeout=3)
        with self._lock:
            self._process = None
        self._log(f"The worker exited with code {code}.")
        return code

    def _pump(self, process: Any, reader: markers.OutputReader) -> None:
        stream = process.stdout
        if stream is None:
            return
        for raw in iter(stream.readline, b""):
            text = raw.decode("utf-8", errors="replace")
            self._log("worker: " + text.rstrip())
            changed = reader.feed(text)
            if "vcredist" in changed:
                self._needs_vcredist = True
            if not changed or self._stop.is_set():
                continue
            updates: Dict[str, Any] = {}
            if "role" in changed:
                updates["role"] = changed["role"]
            if "also" in changed:
                updates["also_cpu"] = changed["also"]
            if "refusal" in changed:
                updates["refusal"] = changed["refusal"]
            if "clock" in changed:
                updates["clock_notice"] = markers.clock_notice(changed["clock"])
            if "dock" in changed:
                updates["dock"] = changed["dock"]
            if "yield" in changed:
                updates["yielding"] = changed["yield"]
            if "activity" in changed:
                updates["activity"] = changed["activity"].text
                updates["progress"] = changed["activity"].fraction
            if "heartbeat" in changed and self.status().state in (STARTING, PREPARING):
                # Only now: the worker prints its role before its first heartbeat, and a coordinator it cannot reach would
                # leave "Donating" on screen while the worker retries.
                updates["state"] = DONATING
                updates["message"] = "Donating."
            if updates:
                self._set(**updates)


# --------------------------------------------------------------------------------------------------
# The worker process itself (this app, run with --run-worker)
# --------------------------------------------------------------------------------------------------

def patch_engine_config(root: str) -> None:
    """
    A frozen engine treats the folder of its executable as its install root (engine/config.py ``get_base_dir``), which
    for this app is the app's own install folder, not the verified engine folder. Point the engine's config module at
    ``root`` before anything else reads it. The engine's other modules take their paths from this one.
    """
    import engine.config as engine_config

    engine_config.get_base_dir = lambda: root                                   # type: ignore[assignment]
    engine_config.CONFIG_DIR = os.path.join(root, "config")
    engine_config.CONFIG_FILE = os.path.join(engine_config.CONFIG_DIR, "config.txt")
    engine_config._CONFIG_TMP_FILE = engine_config.CONFIG_FILE + ".tmp"
    if os.path.abspath(engine_config.get_base_dir()) != os.path.abspath(root):
        raise RuntimeError("could not point the engine at its folder")


PARENT_ENV = "DLNE_DONATE_PARENT"
PARENT_CHECK_S = 2.0


def watch_parent(pid_text: str, check_s: Optional[float] = None) -> Optional[threading.Thread]:
    """
    If the app that started this worker dies without stopping it (killed, crashed, power button on a laptop dock), the worker
    must not go on lending the computer on its own: it ends itself within a couple of seconds. On Linux that is SIGTERM, which
    the worker answers by shutting its model server down; on Windows the process ends and its model server (in a job object
    the engine makes) goes with it.
    """
    try:
        pid = int(pid_text)
    except ValueError:
        return None
    if pid <= 0 or pid == os.getpid():
        return None
    import signal

    interval = PARENT_CHECK_S if check_s is None else check_s

    def watch() -> None:
        import psutil
        while True:
            time.sleep(interval)
            if not psutil.pid_exists(pid):
                if os.name == "posix":
                    os.kill(os.getpid(), signal.SIGTERM)
                    time.sleep(10)          # the worker's own shutdown has this long; then
                os._exit(1)
    thread = threading.Thread(target=watch, name="donate-parent-watch", daemon=True)
    thread.start()
    return thread


def run_worker_process(root: str, argv: List[str]) -> int:
    """
    Run ``dlne_server.py <argv>`` of the verified engine in this process. Called with ``--run-worker <engine folder> -- ...``
    by the controller; never reached except from a command line the controller built from a VerifiedEngine.
    """
    import runpy

    root = os.path.abspath(root)
    script = os.path.join(root, "dlne_server.py")
    if not os.path.isfile(script):
        print(f"DLNE Donate: {script} is missing", file=sys.stderr)
        return 2
    sys.path.insert(0, root)
    os.chdir(root)
    patch_engine_config(root)
    watch_parent(os.environ.get(PARENT_ENV, ""))
    sys.argv = [script] + list(argv)
    try:
        runpy.run_path(script, run_name="__main__")
    except SystemExit as stop:
        code = stop.code
        return code if isinstance(code, int) else (0 if code is None else 1)
    return 0
