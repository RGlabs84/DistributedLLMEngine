"""
Which llama.cpp engine a worker runs: Vulkan or CUDA (DLNE 2.7.0, docs/release-2.7-contract.md sections 1 and 7).

The editor or the tray app starts the worker with ``DLNE_ENGINE_FLAVOR=vulkan|cuda`` (the setting and the owner's consent
already turned into one of the two). This file holds everything the worker then does about it, so engine/worker.py only
has to ask three questions:

  * ``acquire()``   get the engine files of the right flavour, with the CUDA engine's start-up check done;
  * ``fall_back()`` the CUDA engine did not run: say why, run Vulkan from now on;
  * ``confirm()``   the engine runs: say which one (``DLNE_ENGINE``), and drop the other flavour's files.

The rules, as the contract has them:

  * CUDA only where ``engine.hardware.cuda_capability()`` says the machine may (an NVIDIA GPU of compute capability 7.5 or
    newer and a CUDA 13 driver). Not capable is Vulkan, quietly: no log line, ``"fallback": ""``.
  * The CUDA engine must pass its start-up check: its files run, and they see a CUDA device (``llama-server
    --list-devices``, which does not depend on the model server's logging, switched off for coding members). If it does
    not, or its server dies while starting, the worker falls back to Vulkan, says why in its log and on the
    ``DLNE_ENGINE`` line, and never tries CUDA again in this process (the editor decides again at the next start).
  * A machine keeps only the flavour it runs: once the new engine runs, the other flavour's folder is removed.

The markers are printed like ``DLNE_ROLE`` / ``DLNE_DOCK``: one line of compact JSON, once, and again on a change.
"""

import json
import os
import shutil
import subprocess
import threading
from typing import Any, Callable, Dict, Optional

from engine.logger import log_error, log_system, log_warning

# The environment variable the editor and the tray start the worker with.
FLAVOUR_ENV = "DLNE_ENGINE_FLAVOR"
FLAVOURS = ("vulkan", "cuda")

# What the worker prints for the editor and the tray (docs/release-2.7-contract.md section 7).
#   'DLNE_CUDA: {"capable": true, "reason": "", "gpu": "...", "compute_cap": "8.9", "driver": "615.78"}'
#       before any engine is downloaded;
#   'DLNE_ENGINE: {"flavour": "cuda", "fallback": ""}' once the engine runs ("fallback" says why CUDA could not
#       start after a fall-back to Vulkan).
CUDA_MARKER = "DLNE_CUDA: "
ENGINE_MARKER = "DLNE_ENGINE: "

# Where each flavour's engine files live below the install folder. Vulkan keeps the folder every earlier release used.
# The packaging part (engine/downloader.py) owns the real layout: its ``engine_dir(base_dir, flavour)`` is used when it
# has one, and this table (the same two names) is only the fallback for a downloader from before 2.7.
FLAVOUR_FOLDERS = {"vulkan": "bin", "cuda": "bin-cuda"}

# How long the start-up check may take. It loads the CUDA runtime once, which is a second or two on a warm machine
# and longer when the driver has to wake the GPU.
DEVICE_CHECK_SECONDS = 60.0


def requested_flavour(env: Optional[Dict[str, str]] = None) -> str:
    """The flavour the starter asked for: ``cuda`` or ``vulkan``. Missing or anything else is ``vulkan``."""
    value = (os.environ if env is None else env).get(FLAVOUR_ENV, "")
    return "cuda" if str(value).strip().lower() == "cuda" else "vulkan"


def flavour_folder(base_dir: str, flavour: str) -> str:
    """The folder that holds ``flavour``'s engine files in this install."""
    try:
        from engine import downloader
        provided = getattr(downloader, "engine_dir", None)
        if callable(provided):
            return provided(base_dir, flavour)
    except Exception:  # noqa: BLE001 - the table below is always good enough
        pass
    return os.path.join(base_dir, FLAVOUR_FOLDERS.get(flavour, "bin"))


class EngineFlavour:
    """
    The engine flavour of one worker process (a "both" machine's phone-pool half shares its GPU half's object).

    ``fetch(flavour)`` returns the path of the flavour's ``ggml-rpc-server`` (None when it could not be had); the worker
    supplies it so that this file never imports the downloader on its own. ``capability`` defaults to
    ``engine.hardware.cuda_capability``; ``run`` to ``subprocess.run`` (both are parameters so tests need no GPU).
    """

    def __init__(self, base_dir: str, requested: str, fetch: Callable[[str], Optional[str]],
                 hybrid: bool = False, capability: Optional[Callable[[], Dict[str, Any]]] = None,
                 run: Callable[..., Any] = subprocess.run) -> None:
        self.base_dir = base_dir
        # The coordinator's own host (its hybrid worker) uses the build its release installer put there, and
        # ENGINE_FLAVOR in config.txt is the coordinator's business (it never follows the editor's setting).
        self.hybrid = hybrid
        self.requested = "cuda" if requested == "cuda" and not hybrid else "vulkan"
        self._fetch = fetch
        self._capability = capability
        self._run = run
        self._lock = threading.RLock()
        self.failure = ""               # why CUDA could not start in this process ("" = it has not failed)
        self.path: Optional[str] = None  # the ggml-rpc-server of the engine acquired last
        self.running = "vulkan"         # the flavour acquired last (its engine may not have started yet)
        self.confirmed = "vulkan"       # the flavour whose engine has actually run (what the heartbeat reports)
        self._verified: Optional[str] = None   # the CUDA engine folder whose start-up check passed
        self._announced_cuda: Optional[Dict[str, Any]] = None
        self._announced_engine: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------ what this machine may do
    def capability(self) -> Dict[str, Any]:
        if self._capability is not None:
            return self._capability()
        from engine.hardware import cuda_capability
        return cuda_capability()

    def wants_cuda(self) -> bool:
        """CUDA was asked for, this machine may, and it has not failed here before."""
        return self.requested == "cuda" and not self.failure and bool(self.capability().get("capable"))

    def announce_capability(self) -> None:
        """Print DLNE_CUDA once per change, before any engine is downloaded."""
        facts = self.capability()
        with self._lock:
            if facts == self._announced_cuda:
                return
            self._announced_cuda = dict(facts)
        print(CUDA_MARKER + json.dumps(facts, separators=(",", ":")), flush=True)

    # ------------------------------------------------------------------ getting the engine
    def acquire(self) -> Optional[str]:
        """
        The path of the ``ggml-rpc-server`` to run, from the flavour this machine runs: the CUDA engine when it may and
        its start-up check passes, otherwise Vulkan (after saying why when CUDA was wanted). None when no engine could
        be had at all.
        """
        with self._lock:
            if self.wants_cuda():
                path = self._fetch_flavour("cuda")
                problem = "the CUDA engine could not be downloaded or would not run" if not path else self._check_cuda(path)
                if not problem:
                    self.path, self.running = path, "cuda"
                    return path
                self.fall_back(problem, switch=False)
            path = self._fetch_flavour("vulkan")
            self.path, self.running = path, "vulkan"
            return path

    def _fetch_flavour(self, flavour: str) -> Optional[str]:
        try:
            return self._fetch(flavour)
        except TypeError:
            # A downloader from before 2.7 knows no flavours: it can give Vulkan and nothing else.
            return self._fetch("vulkan") if flavour == "vulkan" else None
        except Exception as exc:  # noqa: BLE001 - a failed download is "no engine", handled by the caller
            log_error(f"Could not get the {flavour} engine: {type(exc).__name__}: {exc}")
            return None

    def bin_dir(self) -> str:
        """The folder with the engine files in use: where the last acquired ``ggml-rpc-server`` lives (its
        ``llama-server`` is beside it), or the Vulkan folder before anything was acquired."""
        if self.path:
            return os.path.dirname(self.path)
        return flavour_folder(self.base_dir, "vulkan")

    def _check_cuda(self, rpc_server: str) -> str:
        """The CUDA engine's start-up check: "" when its files run and see a CUDA device, otherwise why not."""
        folder = os.path.dirname(rpc_server)
        if self._verified == folder:
            return ""
        names = ("llama-server.exe", "llama-server")
        probe = next((os.path.join(folder, n) for n in names if os.path.exists(os.path.join(folder, n))), None)
        if probe is None:
            return ""           # nothing to ask; the engine's own start (banner, health) is the check
        try:
            result = self._run([probe, "--list-devices"], capture_output=True, text=True,
                               timeout=DEVICE_CHECK_SECONDS, **_quiet_kwargs())
        except subprocess.TimeoutExpired:
            return "the CUDA engine did not answer the device check"
        except OSError as exc:
            return f"the CUDA engine would not start ({exc.strerror or type(exc).__name__})"
        text = f"{result.stdout or ''}\n{result.stderr or ''}"
        if any(line.strip().startswith("CUDA") and ":" in line for line in text.splitlines()):
            self._verified = folder
            return ""
        if result.returncode != 0:
            return f"the CUDA engine would not start (exit code {result.returncode}{_last_line(text)})"
        return "the CUDA engine found no CUDA device"

    # ------------------------------------------------------------------ the verdicts
    def fall_back(self, reason: str, switch: bool = True) -> None:
        """
        CUDA did not run: remember why, say so, and run Vulkan from now on. ``switch`` also fetches the Vulkan engine
        now (a server that died after the CUDA files were chosen); ``acquire`` passes False because it does that next.
        """
        with self._lock:
            if self.failure:
                return
            self.failure = reason or "the CUDA engine did not run"
            self._verified = None
            self.running = "vulkan"
            log_warning(f"CUDA could not start: {self.failure}; running on Vulkan.")
            if switch:
                self.acquire()

    def confirm(self) -> None:
        """
        The engine of ``running`` has started and works: say which flavour runs (DLNE_ENGINE, once per change) and remove
        the other flavour's folder, so the machine keeps only what it runs.
        """
        with self._lock:
            self.confirmed = self.running
            facts = {"flavour": self.confirmed, "fallback": self.failure if self.confirmed == "vulkan" else ""}
            changed = facts != self._announced_engine
            self._announced_engine = facts
        if changed:
            print(ENGINE_MARKER + json.dumps(facts, separators=(",", ":")), flush=True)
        self._remove_other_flavour()

    def _remove_other_flavour(self) -> None:
        if self.hybrid or not self.path:
            return          # the coordinator's host: its release installer owns the folder
        other = "vulkan" if self.confirmed == "cuda" else "cuda"
        folder = flavour_folder(self.base_dir, other)
        running = os.path.abspath(os.path.dirname(self.path))
        root = os.path.abspath(self.base_dir)
        target = os.path.abspath(folder)
        # Only a folder of this install, and never the one in use.
        if target == running or target == root or not target.startswith(root + os.sep) or not os.path.isdir(target):
            return
        removed = False
        try:
            from engine import downloader
            remove = getattr(downloader, "remove_engine_flavour", None)     # also drops the install record; never follows a symlink
            if callable(remove):
                remove(self.base_dir, other)
                removed = True
        except Exception:  # noqa: BLE001 - the plain removal below is always enough
            pass
        if not removed:
            shutil.rmtree(target, ignore_errors=True)
        if not os.path.isdir(target):
            log_system(f"Removed the {other} engine: this machine runs {self.confirmed}.")


def _quiet_kwargs() -> Dict[str, Any]:
    """No console window on Windows for the check (engine.hardware has the same helper)."""
    from engine.hardware import _hidden_process_kwargs
    return _hidden_process_kwargs()


def _last_line(text: str) -> str:
    """The last non-empty line of a program's output, as ", <line>" for an error message ('' when there is none)."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return f", {lines[-1][:120]}" if lines else ""
