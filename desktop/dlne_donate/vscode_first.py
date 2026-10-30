"""
VS Code first (docs/release-2.7-contract.md section 4b): only one worker runs on a computer, and while VS Code with DLNE is
open it is the extension's. The extension writes ``~/.dlne/editor.json`` when it activates and every 30 seconds after, and
deletes it when it deactivates:

    {"pid": <extension host pid>, "updated": <epoch seconds>, "version": "1.6.0"}

It counts as present while it is less than 90 seconds old AND its pid is alive (a VS Code that was killed leaves a file
behind, and its pid goes). This app only ever reads it. :class:`VsCodeFirst` checks every 10 seconds; when the editor shows
up it stops the worker the way this app's own Stop does, and when the editor is gone it starts the worker again if, and only
if, it was donating before the pause.

Two safeguards keep a computer from ever running two workers. The editor is only taken as gone after two checks in a row miss
it, at least 20 seconds apart (with several VS Code windows the file can vanish for a moment between one window closing and
another's next refresh). And the extension deliberately keeps its own worker alive after VS Code closes or reloads, so the
app does not start (or let the owner start) its worker while any other DLNE worker still runs on this computer.
"""

import json
import os
import socket
import time
from typing import Any, Callable, List, Optional

MAX_AGE_S = 90.0              # the extension refreshes every 30 s; three missed refreshes and it is gone
CHECK_EVERY_S = 10.0
MISSES_TO_BE_GONE = 2         # the editor is gone after this many checks in a row that miss it ...
GONE_AFTER_S = 20.0           # ... the first and the last at least this far apart

# Why the worker is held back (the reason is "" while it is not).
EDITOR = "editor"             # DLNE is open in VS Code
WORKER = "worker"             # VS Code is closed, but its worker still runs
RPC_PORTS = (50052, 50053)    # the GPU pool's RPC port and the phone-app member's (engine/worker.py, engine/app_member.py)


def read_presence(path: str, now: float, pid_alive: Optional[Callable[[int], bool]] = None) -> bool:
    """Whether the editor's presence file at ``path`` says an editor is open right now (see the module's words)."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            facts = json.load(handle)
    except (OSError, ValueError):
        return False
    if not isinstance(facts, dict):
        return False
    pid, updated = facts.get("pid"), facts.get("updated")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if isinstance(updated, bool) or not isinstance(updated, (int, float)) or updated != updated:
        return False
    # A clock that jumped makes a file look like it comes from the future; that is as stale as one from the past.
    if abs(now - updated) >= MAX_AGE_S:
        return False
    return (pid_alive or process_alive)(pid)


def process_alive(pid: int) -> bool:
    """Whether a process with this pid exists (Linux, macOS and Windows). A zombie is not alive."""
    try:
        import psutil
        if not psutil.pid_exists(pid):
            return False
        try:
            return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False
        except psutil.Error:
            return True          # it exists but may not be inspected (another user's): present
    except ImportError:
        return False


def is_dlne_worker_command(cmdline: List[str]) -> bool:
    """Whether a command line runs the engine's worker: ``<python> .../dlne_server.py worker ...`` (an exact match of the arguments)."""
    for index, argument in enumerate(cmdline):
        if os.path.basename(argument.replace("\\", "/")) == "dlne_server.py":
            return "worker" in cmdline[index + 1:]
    return False


def port_in_use(port: int) -> bool:
    """Whether something already listens on ``port`` (tried by binding it, which says nothing to whoever owns it)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "posix":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)     # a socket in TIME_WAIT is not a worker
        probe.bind(("0.0.0.0", port))
        return False
    except OSError:
        return True
    finally:
        probe.close()


def other_worker_running(own_pid: Optional[int] = None, psutil_module: Any = None, port_busy: Callable[[int], bool] = port_in_use) -> bool:
    """
    Whether a DLNE worker that is not this app's own runs on this computer: a process whose arguments are exactly
    ``dlne_server.py worker`` (the extension's), or something already on the worker's RPC ports. This app's own worker, which is
    a child of this process, never counts. Nothing is killed on the strength of this; it only keeps this app from starting a second one.
    """
    psutil = psutil_module
    if psutil is None:
        import psutil as psutil  # type: ignore[no-redef]
    own = own_pid or os.getpid()
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            if proc.pid == own or not is_dlne_worker_command(proc.info.get("cmdline") or []):
                continue
            if any(parent.pid == own for parent in proc.parents()):
                continue
            return True
        except Exception:  # noqa: BLE001 - gone while looking, or not ours to look at
            continue
    return any(port_busy(port) for port in RPC_PORTS)


class VsCodeFirst:
    """
    The pause/resume state machine. ``tick`` is called as often as convenient (the app calls it five times a second) and looks
    at the presence file only every ``CHECK_EVERY_S`` seconds.

    ``is_running`` says whether the worker runs (or is starting), ``stop`` and ``start`` are the app's own Stop and Donate,
    ``other_worker`` whether another DLNE worker runs on this computer, ``on_change`` is told the reason the worker is held back
    (``EDITOR``, ``WORKER``, or "" when it is not). Everything that touches the world is handed in, so a test drives it with a
    fake clock and fake checks.
    """

    def __init__(self, check: Callable[[], bool], is_running: Callable[[], bool], is_stopping: Callable[[], bool],
                 stop: Callable[[], None], start: Callable[[], Any], on_change: Optional[Callable[[str], None]] = None,
                 other_worker: Callable[[], bool] = lambda: False,
                 clock: Callable[[], float] = time.monotonic, every_s: float = CHECK_EVERY_S):
        self._check = check
        self._is_running = is_running
        self._is_stopping = is_stopping
        self._stop = stop
        self._start = start
        self._other_worker = other_worker
        self._on_change = on_change or (lambda _reason: None)
        self._clock = clock
        self._every_s = every_s
        self._last_check = float("-inf")
        self._misses = 0                 # checks in a row that found no editor
        self._first_miss = 0.0
        self.reason = ""
        self.resume_wanted = False       # it was donating (or about to) when the worker was held back

    @property
    def paused(self) -> bool:
        return bool(self.reason)

    def tick(self, force: bool = False) -> None:
        now = self._clock()
        if not force and now - self._last_check < self._every_s:
            return
        self._last_check = now
        if self._check():
            self._misses = 0
            if not self.paused:
                # A worker the owner had just stopped is not one to bring back.
                self.resume_wanted = self.resume_wanted or (self._is_running() and not self._is_stopping())
            self._set_reason(EDITOR)
            if self._is_running() and not self._is_stopping():
                self._stop()             # also catches a worker started during the pause by a click that raced the check
            return
        if self._misses == 0:
            self._first_miss = now
        self._misses += 1
        if self.reason == EDITOR and not (self._misses >= MISSES_TO_BE_GONE and now - self._first_miss >= GONE_AFTER_S):
            return                       # perhaps only between one window closing and another's next refresh
        if self.paused and not self._is_running() and self._other_worker():
            self._set_reason(WORKER)     # VS Code left its worker running: a second one must not start
            return
        self._set_reason("")
        if self.resume_wanted:
            self._resume()

    def want_donating(self) -> None:
        """Donating was asked for while held back (the app started with "start donating when the app starts"): do it once it may."""
        if self.paused:
            self.resume_wanted = True

    def _set_reason(self, reason: str) -> None:
        if reason != self.reason:
            self.reason = reason
            self._on_change(reason)

    def _resume(self) -> None:
        if self._is_stopping():
            return                       # the worker is still leaving (the stop gives it a few seconds): try on the next check
        if self._is_running():
            self.resume_wanted = False   # it is going already
        elif self._start():
            self.resume_wanted = False
