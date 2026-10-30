"""
A worker's side of the CPU pool (2.3.0): serve the phone app with a whole small model.

A worker with no usable GPU becomes a pool member instead of an RPC node. It runs llama-server
from the signed engine zip (beside ggml-rpc-server), on the CPU only and on loopback only; the
coordinator reaches it through the worker's own authenticated tunnel. Which model to serve is
the coordinator's call (its heartbeat answer names a tier); the file comes straight from the
pinned Hugging Face URL in the signed release and runs only if its size and SHA-256 match.
"""

import os
import shutil
import subprocess
import threading
import time
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

import psutil

from engine.app_pool import app_server_args, sha256_ok
from engine.logger import log_error, log_success, log_system, log_warning

APP_PORT = 50053
APP_SLOTS = 2
# After a failure that will not fix itself in seconds (a file that does not verify, a server
# that dies while starting), a tier is not tried again for this long, doubling each time.
BACKOFF_FIRST_SECONDS = 60.0
BACKOFF_MAX_SECONDS = 3600.0

# "Both" (engine/companion.py): when the owner's own load (a game) starts, the phone-pool half of the machine says
# it is not ready at once, gives the answers in flight this long, then stops its model to give the CPU back; when
# the load ends it starts again after the settle period (a load that flickers costs one restart a minute at most).
YIELD_DRAIN_SECONDS = 10.0
YIELD_SETTLE_SECONDS = 60.0


def default_threads(physical_cores: int) -> int:
    """Half the machine, 2 to 8 threads: the owner keeps the other half (generation on a CPU is
    limited by memory bandwidth, which a few threads already use up)."""
    return max(2, min(8, int(physical_cores) // 2))


# ---------------------------------------------------------------------------------------------
# Light mode: while the owner games, a volunteer's CPU-only member runs lighter instead of stopping
# ---------------------------------------------------------------------------------------------
#
# Driven by the same is_yielding probe as everything else, through set_light() from the heartbeat. At once, with
# no restart, the running llama-server drops to the lowest priority and is held to half the cores it may use. After
# YIELD_DRAIN_SECONDS (the answers in flight finish) it is started again in light mode: half the threads, one slot,
# half the context, no prompt cache, lowest priority from the start. When the load has been over for
# YIELD_SETTLE_SECONDS it is started again at full strength; a load that flickers costs one switch per settle period
# at most. Only a CPU-only member of a volunteer's worker does this: the "both" machine's phone-pool half steps aside
# completely (set_yielding), and the coordinator host's own member (engine/app_pool.LocalAppServer) is not touched.

LIGHT_NICE = 19      # the lowest priority on Linux and macOS
NORMAL_NICE = 10     # what a member runs at otherwise ("below normal")


def light_threads(threads: int) -> int:
    """Half the threads, at least one."""
    return max(1, int(threads) // 2)


def light_context(ctx: int) -> int:
    """Half the context."""
    return max(1024, int(ctx) // 2)


def app_member_args(binary: str, model_path: str, port: int, threads: int, light: bool = False,
                    ctx: int = 16384, slots: int = APP_SLOTS) -> List[str]:
    """
    llama-server's arguments for a volunteer's CPU pool member: exactly engine.app_pool.app_server_args at full
    strength; in light mode half the threads, one slot, half the context and no prompt cache (``--cache-ram 0``).
    """
    if not light:
        return app_server_args(binary, model_path, port, threads, ctx=ctx, slots=slots)
    return app_server_args(binary, model_path, port, light_threads(threads), ctx=light_context(ctx), slots=1) + [
        "--cache-ram", "0"]


def _on_windows() -> bool:
    return os.name == "nt"


def lower_priority(pid: int) -> Dict[str, Any]:
    """
    Drop a running process to the lowest priority (niceness 19 on Linux and macOS, IDLE_PRIORITY_CLASS on Windows
    through psutil) and hold it to half the CPUs it may use, at least one (psutil's cpu_affinity, which Linux and
    Windows have). Returns what it changed, for restore_priority. Nothing here may fail the member: a process that
    has gone, or an operating system that refuses, is skipped.
    """
    saved: Dict[str, Any] = {"affinity": None}
    try:
        if _on_windows():
            psutil.Process(pid).nice(getattr(psutil, "IDLE_PRIORITY_CLASS", 0x40))
        elif hasattr(os, "setpriority"):
            os.setpriority(os.PRIO_PROCESS, pid, LIGHT_NICE)
    except Exception:  # noqa: BLE001 - psutil.Error, OSError
        pass
    try:
        proc = psutil.Process(pid)
        if hasattr(proc, "cpu_affinity"):
            cpus = list(proc.cpu_affinity())
            if cpus:
                proc.cpu_affinity(cpus[:max(1, len(cpus) // 2)])
                saved["affinity"] = cpus
    except Exception:  # noqa: BLE001
        saved["affinity"] = None
    return saved


def restore_priority(pid: int, saved: Optional[Dict[str, Any]]) -> bool:
    """
    Undo lower_priority: the member's normal priority and the CPUs it had. False when the priority could not be put
    back: Linux and macOS do not let a program raise its own niceness back down, so there the answer is a restart.
    """
    restored = True
    try:
        if _on_windows():
            psutil.Process(pid).nice(getattr(psutil, "BELOW_NORMAL_PRIORITY_CLASS", 0x4000))
        elif hasattr(os, "setpriority"):
            os.setpriority(os.PRIO_PROCESS, pid, NORMAL_NICE)
    except Exception:  # noqa: BLE001
        restored = False
    if saved and saved.get("affinity"):
        try:
            psutil.Process(pid).cpu_affinity(saved["affinity"])
        except Exception:  # noqa: BLE001
            restored = False
    return restored


class AppMember:
    def __init__(self, base_dir: str, tiers_loader: Callable[[], List[Dict[str, Any]]],
                 threads: int, port: int = APP_PORT, urlopen=urllib.request.urlopen, engine: Optional[Any] = None) -> None:
        # ``engine`` is the worker's engine.engine_flavour.EngineFlavour: where the engine files are on a machine that
        # runs the CUDA engine (2.7.0). None: the install's bin/ folder, as before.
        self.engine = engine
        self.base_dir = base_dir
        self.tiers_loader = tiers_loader
        self.threads = max(1, threads)
        self.port = port
        self.urlopen = urlopen
        self.target: Optional[str] = None
        self.serving: Optional[str] = None
        self.ready = False
        self.process: Optional[subprocess.Popen] = None
        self._log = None
        self._lock = threading.Lock()
        self._busy = False
        self._assigned = False          # assign() has been called at least once
        self._yielding = False          # stepped aside for the owner's own load (set_yielding)
        self._yield_since = 0.0
        self._resume_not_before = 0.0   # monotonic time before which a yielded member does not start again
        # Light mode (set_light): the owner's load is on, so this member runs lighter instead of stopping.
        self._light = False             # the owner's load is on
        self._light_since = 0.0         # when it came on
        self._light_cleared = 0.0       # when it last went off
        self._proc_light = False        # the running llama-server was started in light mode
        self._throttle: Optional[Dict[str, Any]] = None   # what lower_priority changed on a full-strength process
        self._last_switch: Optional[float] = None         # when the running process last changed mode
        self._stopped = threading.Event()
        # tier id -> (failures in a row, monotonic time before which it is not tried again)
        self._backoff: Dict[str, Tuple[int, float]] = {}

    # --------------------------------------------------------------------- status
    def status(self) -> Dict[str, Any]:
        alive = self.process is not None and self.process.poll() is None
        # While the owner's load is on (light mode) the member keeps serving with one slot, so the coordinator keeps
        # routing to it but less; `threads` are the ones its llama-server really runs on (half of them when light).
        slots = 1 if (self._light or self._proc_light) else APP_SLOTS
        threads = light_threads(self.threads) if self._proc_light else self.threads
        return {"model": self.serving, "ready": bool(self.ready and alive and not self._yielding), "slots": slots,
                "threads": threads}

    def settled(self) -> bool:
        """
        True once the member has nothing left to load: it serves, or the coordinator told it to serve nothing, or its
        model failed and is backing off. False before the first assignment and while it downloads or loads. A machine
        that also runs a coding model (docs/coding-members.md, "both") waits for this before it loads that one, so
        the two never read the machine's RAM at the same instant.
        """
        with self._lock:
            if self._busy:
                return False
            if self.process is not None and self.process.poll() is None and self.ready:
                return True
            if not self._assigned:
                return False
            return self.target is None or self._held_back(self.target)

    # --------------------------------------------------------------------- control
    def assign(self, tier_id: Optional[str]) -> None:
        """Called with each heartbeat answer; converges in the background, never blocks.
        Each call retries whatever is not done yet (a failed switch, a server not yet ready),
        except a tier that is backing off."""
        with self._lock:
            if self._stopped.is_set():
                return
            self.target = tier_id
            self._assigned = True
            if self._yielding or time.monotonic() < self._resume_not_before:
                return      # stepped aside for the owner's own load: the model starts again once that is over
            if self._busy:
                return
            action = self._mode_action()
            if action is not None:
                self._busy = True
                threading.Thread(target=self._switch_mode, args=(action,), name="dlne-app-mode", daemon=True).start()
                return
            alive = self.process is not None and self.process.poll() is None
            if tier_id is None and self.process is None:
                return
            if tier_id is not None and tier_id == self.serving and alive and self.ready:
                return
            if tier_id is not None and self._held_back(tier_id):
                return
            self._busy = True
        threading.Thread(target=self._converge, name="dlne-app-member", daemon=True).start()

    def set_yielding(self, yielding: bool) -> None:
        """
        Called with every heartbeat of a "both" machine's phone-pool half (engine/companion.py). While the owner's own
        load is on, the member reports ``ready: false`` at once, so the coordinator sends it nothing new; the answers
        in flight get YIELD_DRAIN_SECONDS, then llama-server is stopped to give the CPU back. When the load ends the
        member waits YIELD_SETTLE_SECONDS, and the next assignment starts the model again.
        """
        yielding = bool(yielding)
        with self._lock:
            if yielding == self._yielding or self._stopped.is_set():
                return
            self._yielding = yielding
            if not yielding:
                self._resume_not_before = time.monotonic() + YIELD_SETTLE_SECONDS
                log_system("The owner's load has ended; the phone-pool member comes back after a short wait.")
                return
            self._yield_since = time.monotonic()
        log_system("The owner's own load is using this machine: the phone-pool member stops taking requests.")
        threading.Thread(target=self._give_way, name="dlne-app-yield", daemon=True).start()

    def _give_way(self) -> None:
        """Give the answers in flight YIELD_DRAIN_SECONDS, then stop llama-server (after a model load in progress
        has finished, so it cannot start the model again behind the stop)."""
        while not self._stopped.is_set():
            with self._lock:
                if not self._yielding:
                    return      # the load went away within the drain: the model stays
                if not self._busy and time.monotonic() - self._yield_since >= YIELD_DRAIN_SECONDS:
                    self._busy = True
                    break
            time.sleep(0.1)
        else:
            return
        try:
            self._stop_process()
            log_system("The phone-pool member has stopped its model until the owner's load is over.")
        finally:
            with self._lock:
                self._busy = False

    # --------------------------------------------------------------------- light mode
    def set_light(self, light: bool) -> None:
        """
        Called with every heartbeat of a volunteer's CPU-only member, with the worker's is_yielding probe. When the
        owner's load comes on, the running llama-server drops to the lowest priority and to half its CPUs at once, with
        no restart, and the member reports one slot; assign() then restarts it in light mode after YIELD_DRAIN_SECONDS.
        When the load has been over for YIELD_SETTLE_SECONDS, assign() puts it back at full strength. A call that
        changes nothing does nothing at all, so a member whose owner never games is never touched.
        """
        light = bool(light)
        with self._lock:
            if light == self._light or self._stopped.is_set() or self._yielding:
                return
            self._light = light
            now = time.monotonic()
            if light:
                self._light_since = now
            else:
                self._light_cleared = now
            proc = self.process
            lower = (light and proc is not None and proc.poll() is None and not self._proc_light
                     and self._throttle is None)
        if light:
            log_system("The owner's own load is on: the phone-pool member runs lighter (lowest priority, fewer cores and "
                       "threads) until it is over.")
            if lower:
                self._throttle = lower_priority(proc.pid)       # at once, no restart
        else:
            log_system("The owner's load has ended; the phone-pool member returns to full strength after a short wait.")

    def serving_light(self) -> bool:
        """
        True while the owner's load is on and the member is serving anyway, lighter. The worker then does not report
        is_yielding for it: the coordinator leaves a yielding pool member out of the routing altogether
        (registry.app_members), and a member that runs lighter instead of stopping is meant to stay in it.
        """
        return bool(self._light and self.status()["ready"])

    def _mode_action(self) -> Optional[str]:
        """What a running, ready member should do about light mode now (called with the lock held): "light" (restart
        in light mode), "full" (restart at full strength), "undo" (put the priority and CPUs back in place) or None.
        A restart is at most one per YIELD_SETTLE_SECONDS, so a load that flickers cannot make a loop of them."""
        proc = self.process
        if self._stopped.is_set() or proc is None or proc.poll() is not None or not self.ready:
            return None
        now = time.monotonic()
        may_restart = self._last_switch is None or now - self._last_switch >= YIELD_SETTLE_SECONDS
        if self._light:
            if not self._proc_light and now - self._light_since >= YIELD_DRAIN_SECONDS and may_restart:
                return "light"
            return None
        if self._light_cleared and now - self._light_cleared >= YIELD_SETTLE_SECONDS:
            if self._proc_light:
                return "full" if may_restart else None
            if self._throttle is not None:
                return "undo"
        return None

    def _switch_mode(self, action: str) -> None:
        """Carry out _mode_action on a thread of its own (the member is marked busy meanwhile)."""
        try:
            with self._lock:
                proc, throttle, tier_id = self.process, self._throttle, self.serving
            if action == "undo":
                if proc is not None and restore_priority(proc.pid, throttle):
                    with self._lock:
                        self._throttle = None
                    return
                action = "full"     # the priority cannot be raised again in place on this system: start afresh
            tier = next((t for t in self.tiers_loader() if t.get("id") == tier_id), None)
            if tier is None:
                return
            path, _ = self._ensure_model(tier)
            if path is None:
                return
            self._start(tier, path, light=(action == "light"))
            with self._lock:
                self._last_switch = time.monotonic()
        except Exception as exc:  # noqa: BLE001 - reported; the next heartbeat decides again
            log_error(f"CPU pool member: {type(exc).__name__}: {exc}")
        finally:
            with self._lock:
                self._busy = False

    def stop(self) -> None:
        self._stopped.set()
        self._stop_process()

    def _stop_process(self) -> None:
        self._throttle, self._proc_light = None, False
        proc, self.process, self.ready = self.process, None, False
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        log, self._log = self._log, None
        if log is not None:
            log.close()

    def _held_back(self, tier_id: str) -> bool:
        failures = self._backoff.get(tier_id)
        return failures is not None and time.monotonic() < failures[1]

    def _failed(self, tier_id: str) -> None:
        count = self._backoff.get(tier_id, (0, 0.0))[0] + 1
        delay = min(BACKOFF_MAX_SECONDS, BACKOFF_FIRST_SECONDS * 2 ** (count - 1))
        self._backoff[tier_id] = (count, time.monotonic() + delay)
        log_warning(f"CPU pool member: will try that model again in {int(delay)} s.")

    def _converge(self) -> None:
        try:
            with self._lock:
                target = self.target
            alive = self.process is not None and self.process.poll() is None
            if target is None:
                self._stop_process()
                self.serving = None
                return
            if target == self.serving and alive:
                if not self.ready:
                    self._wait_ready(target)
                return
            tier = next((t for t in self.tiers_loader() if t.get("id") == target), None)
            if tier is None:
                log_error(f"The coordinator assigned CPU pool tier {target!r}, which its signed release does not list.")
                return
            # The running server, if any, keeps answering until the new model is ready to start.
            path, lasting = self._ensure_model(tier)
            if path is None:
                if lasting:
                    self._failed(target)
                return
            if self._start(tier, path):
                self._backoff.pop(target, None)
            else:
                self._failed(target)
        except Exception as exc:  # noqa: BLE001 - reported, retried on the next assignment
            log_error(f"CPU pool member: {type(exc).__name__}: {exc}")
        finally:
            with self._lock:
                self._busy = False

    # --------------------------------------------------------------------- model file
    def _ensure_model(self, tier: Dict[str, Any]) -> Tuple[Optional[str], bool]:
        """``(path, False)`` when the verified file is in place; ``(None, lasting)`` when not,
        ``lasting`` True for failures not worth retrying at the next heartbeat (no disk space,
        a file that does not verify, a download that broke after data arrived)."""
        models = os.path.join(self.base_dir, "models")
        os.makedirs(models, exist_ok=True)
        path = os.path.join(models, tier["file"])
        size = int(tier["size"])
        if sha256_ok(path, tier["sha256"], size):
            return path, False
        free = shutil.disk_usage(models).free
        if free < size * 1.05:
            log_error(f"Not enough disk space for {tier['name']}: it needs {size / 1e9:.1f} GB, "
                      f"{free / 1e9:.1f} GB is free in {models}.")
            return None, True
        part = path + ".part"
        log_system(f"Downloading {tier['name']} for the CPU pool ({size / 1e9:.1f} GB, once)...")
        done = 0
        try:
            with self.urlopen(tier["url"], timeout=60) as response, open(part, "wb") as out:
                last = 0
                for block in iter(lambda: response.read(4 << 20), b""):
                    if self._stopped.is_set():
                        return None, False
                    out.write(block)
                    done += len(block)
                    if done > size:
                        raise ValueError("the download is larger than the signed size")
                    if done * 10 // size > last:
                        last = done * 10 // size
                        log_system(f"  {tier['name']}: {last * 10}%")
            os.replace(part, path)
        except Exception as exc:  # noqa: BLE001 - reported; retried at once only if nothing arrived
            log_error(f"Downloading {tier['name']} failed: {type(exc).__name__}: {exc}")
            return None, done > 0
        finally:
            if os.path.exists(part):
                try:
                    os.remove(part)
                except OSError:
                    pass
        if not sha256_ok(path, tier["sha256"], size):
            log_error(f"{tier['file']} does not match the signed release; deleted.")
            try:
                os.remove(path)
            except OSError:
                pass
            return None, True
        log_success(f"{tier['name']} downloaded and verified.")
        return path, False

    # --------------------------------------------------------------------- server
    def _binary(self) -> Optional[str]:
        bin_dir = self.engine.bin_dir() if self.engine is not None else os.path.join(self.base_dir, "bin")
        for name in ("llama-server.exe", "llama-server"):
            candidate = os.path.join(bin_dir, name)
            if os.path.exists(candidate):
                if not name.endswith(".exe"):
                    try:
                        os.chmod(candidate, 0o755)
                    except OSError:
                        pass
                return candidate
        return None

    def _start(self, tier: Dict[str, Any], model_path: str, light: Optional[bool] = None) -> bool:
        """Start serving ``tier``. False when the server could not start or died starting (the
        caller backs off); True once it runs, even if it is still loading (later heartbeats
        check on it again). ``light``: start in light mode (default: whether the owner's load is on now)."""
        binary = self._binary()
        if binary is None:
            log_error("This engine has no llama-server; the CPU pool needs DLNE 2.3.0's engine files.")
            with self._lock:
                self.target = None
            return False
        self._stop_process()
        log_dir = os.path.join(self.base_dir, "logs")
        os.makedirs(log_dir, exist_ok=True)
        self._log = open(os.path.join(log_dir, "app_server.log"), "a")
        # The phone pool runs on the CPU: Vulkan is hidden from it, and so is CUDA (the engine may be the CUDA build).
        env = dict(os.environ, GGML_VK_VISIBLE_DEVICES="", CUDA_VISIBLE_DEVICES="")
        light = self._light if light is None else light
        # Below normal priority: the owner's own programs come first on their machine.
        flags = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
        if light:
            flags = getattr(subprocess, "IDLE_PRIORITY_CLASS", flags)       # (Windows; elsewhere 0 either way)
        self.process = subprocess.Popen(app_member_args(binary, model_path, self.port, self.threads, light=light),
                                        stdout=self._log, stderr=subprocess.STDOUT, env=env, creationflags=flags)
        if light:
            lower_priority(self.process.pid)          # the lowest priority and half the CPUs, from the start
            self._proc_light = True
        elif hasattr(os, "setpriority"):
            try:
                os.setpriority(os.PRIO_PROCESS, self.process.pid, 10)
            except OSError:
                pass
        self.serving = tier["id"]
        return self._wait_ready(tier["id"]) is not None

    def _wait_ready(self, tier_id: str, timeout: float = 300.0) -> Optional[bool]:
        """Poll /health: True when ready, False when still loading at the deadline, None when
        the process died."""
        name = next((t.get("name") for t in self.tiers_loader() if t.get("id") == tier_id), tier_id)
        end = time.monotonic() + timeout
        while not self._stopped.is_set():
            process = self.process
            if process is None or process.poll() is not None:
                log_error("The CPU pool model server stopped while starting; see logs/app_server.log.")
                return None
            try:
                with self.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=2) as resp:
                    if resp.status == 200:
                        self.ready = True
                        log_success(f"Serving {name} for the phone app.")
                        return True
            except Exception:  # noqa: BLE001 - not up yet
                pass
            if time.monotonic() >= end:
                break
            time.sleep(1.0)
        log_warning("The CPU pool model server is still loading; it is checked again with the next heartbeat.")
        return False
