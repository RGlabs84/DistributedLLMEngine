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

from engine.app_pool import app_server_args, sha256_ok
from engine.logger import log_error, log_success, log_system, log_warning

APP_PORT = 50053
APP_SLOTS = 2
# After a failure that will not fix itself in seconds (a file that does not verify, a server
# that dies while starting), a tier is not tried again for this long, doubling each time.
BACKOFF_FIRST_SECONDS = 60.0
BACKOFF_MAX_SECONDS = 3600.0


def default_threads(physical_cores: int) -> int:
    """Half the machine, 2 to 8 threads: the owner keeps the other half (generation on a CPU is
    limited by memory bandwidth, which a few threads already use up)."""
    return max(2, min(8, int(physical_cores) // 2))


class AppMember:
    def __init__(self, base_dir: str, tiers_loader: Callable[[], List[Dict[str, Any]]],
                 threads: int, port: int = APP_PORT, urlopen=urllib.request.urlopen) -> None:
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
        self._stopped = threading.Event()
        # tier id -> (failures in a row, monotonic time before which it is not tried again)
        self._backoff: Dict[str, Tuple[int, float]] = {}

    # --------------------------------------------------------------------- status
    def status(self) -> Dict[str, Any]:
        alive = self.process is not None and self.process.poll() is None
        return {"model": self.serving, "ready": bool(self.ready and alive), "slots": APP_SLOTS}

    # --------------------------------------------------------------------- control
    def assign(self, tier_id: Optional[str]) -> None:
        """Called with each heartbeat answer; converges in the background, never blocks.
        Each call retries whatever is not done yet (a failed switch, a server not yet ready),
        except a tier that is backing off."""
        with self._lock:
            if self._stopped.is_set():
                return
            self.target = tier_id
            if self._busy:
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

    def stop(self) -> None:
        self._stopped.set()
        self._stop_process()

    def _stop_process(self) -> None:
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
        for name in ("llama-server.exe", "llama-server"):
            candidate = os.path.join(self.base_dir, "bin", name)
            if os.path.exists(candidate):
                if not name.endswith(".exe"):
                    try:
                        os.chmod(candidate, 0o755)
                    except OSError:
                        pass
                return candidate
        return None

    def _start(self, tier: Dict[str, Any], model_path: str) -> bool:
        """Start serving ``tier``. False when the server could not start or died starting (the
        caller backs off); True once it runs, even if it is still loading (later heartbeats
        check on it again)."""
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
        env = dict(os.environ, GGML_VK_VISIBLE_DEVICES="")
        # Below normal priority: the owner's own programs come first on their machine.
        flags = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
        self.process = subprocess.Popen(app_server_args(binary, model_path, self.port, self.threads, slots=APP_SLOTS),
                                        stdout=self._log, stderr=subprocess.STDOUT, env=env, creationflags=flags)
        if hasattr(os, "setpriority"):
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
