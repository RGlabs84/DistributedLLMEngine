"""
When each GPU worker began standing by, kept across a coordinator restart.

A GPU contributor keeps its perks (first in line, no usage window) while its GPU is serving, and for
``GPU_STANDBY_GRACE_MINUTES`` after it joined or last had a job (engine/coordinator.py, ``_gpu_keeps_perks``).
The clock for that is one number per worker: the moment it was first seen with no job. Held in memory alone, a
coordinator restart (a deploy, a crash) would give every idle GPU a fresh grace, so this book also keeps the
numbers in a small JSON file.

The file (``<install>/config/gpu_standby.json``)::

    {"version": 1, "saved_at": 1761480000.0,
     "workers": {"<worker id>": {"since": 1761478200.0, "seen": 1761479990.0}}}

* Only workers with no job have an entry: a worker that gets a job loses it, and the file stays small.
* The key is the worker id, as it is in memory. Worker ids are per machine, so the book follows the machine.
* ``seen`` is when the worker last reported. An entry for a worker not seen for ``forget_after`` (24 hours) is
  dropped when the file is read or written and whenever the book is full; at ``max_tracked`` entries the one
  seen longest ago goes.
* It is written aside and renamed into place, at most once every ``save_every`` (30 s) seconds when something
  changed (and once every ``refresh_every`` seconds while there are entries, to keep ``seen`` fresh), and by
  ``flush()`` at a clean shutdown. A crash loses at most the last 30 seconds of changes.
* It is read once at start-up. A missing, stale, truncated or hand-edited file means "no grace history": every
  entry is checked, what cannot be trusted is skipped, and nothing here ever raises into a heartbeat.
* A book writes nothing until ``load()`` has run (the coordinator calls it at start-up), so tests and tools that
  merely import the coordinator never touch the installation's files.

The times are wall-clock seconds (``time.time``): a monotonic clock means nothing after a restart. A time in the
future (the machine's clock was set back) is taken as now.
"""

import json
import math
import os
import threading
import time
from typing import Any, Callable, Dict, Optional

FILE_VERSION = 1
SAVE_EVERY_SECONDS = 30.0
REFRESH_EVERY_SECONDS = 600.0
FORGET_AFTER_SECONDS = 24 * 3600.0
MAX_TRACKED = 8192
MAX_ID_CHARS = 200


def _number(value: Any) -> Optional[float]:
    """A finite non-negative number from a JSON value (a bool is not a number here), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0.0 else None


class StandbyBook:
    """Worker id -> when it began standing by, with the file behind it. Thread-safe."""

    def __init__(self, path_fn: Optional[Callable[[], str]] = None, clock: Callable[[], float] = time.time, *,
                 max_tracked: int = MAX_TRACKED, forget_after: float = FORGET_AFTER_SECONDS,
                 save_every: float = SAVE_EVERY_SECONDS, refresh_every: float = REFRESH_EVERY_SECONDS) -> None:
        self.path_fn = path_fn
        self.clock = clock
        self.max_tracked = max(1, int(max_tracked))
        self.forget_after = float(forget_after)
        self.save_every = float(save_every)
        self.refresh_every = float(refresh_every)
        self._lock = threading.Lock()
        self._io_lock = threading.Lock()
        self._since: Dict[str, float] = {}
        self._seen: Dict[str, float] = {}
        self._dirty = False
        self._armed = False
        self._last_save: Optional[float] = None

    # ------------------------------------------------------------------ the clock

    def now(self) -> float:
        return float(self.clock())

    def since(self, worker_id: str) -> Optional[float]:
        with self._lock:
            return self._since.get(worker_id)

    def __len__(self) -> int:
        with self._lock:
            return len(self._since)

    def tracked(self) -> Dict[str, float]:
        """A copy: worker id -> when it began standing by."""
        with self._lock:
            return dict(self._since)

    def note(self, worker_id: str, has_job: bool) -> Optional[float]:
        """
        Record that ``worker_id`` was just seen. With a job its entry is removed and None comes back; without
        one the answer is when it began standing by (now, the first time it is seen without a job).
        """
        now = self.now()
        with self._lock:
            if has_job:
                if self._since.pop(worker_id, None) is not None:
                    self._dirty = True
                self._seen.pop(worker_id, None)
                since: Optional[float] = None
            else:
                since = self._since.get(worker_id)
                # An entry from the future (the clock was set back) or for a worker not seen for a day is no
                # history: the worker starts again, as a new one does.
                gone = since is not None and (since > now or now - self._seen.get(worker_id, since) >= self.forget_after)
                if since is None or gone:
                    if since is None:
                        self._make_room(now)
                    since = self._since[worker_id] = now
                    self._dirty = True
                self._seen[worker_id] = now
        self.maybe_save()
        return since

    def _make_room(self, now: float) -> None:
        """Before adding an entry: drop the ones not seen for a day, then the one seen longest ago."""
        if len(self._since) < self.max_tracked:
            return
        for worker_id in [w for w in self._since if now - self._seen.get(w, 0.0) >= self.forget_after]:
            self._drop(worker_id)
        while len(self._since) >= self.max_tracked:
            self._drop(min(self._since, key=lambda w: self._seen.get(w, 0.0)))

    def _drop(self, worker_id: str) -> None:
        self._since.pop(worker_id, None)
        self._seen.pop(worker_id, None)
        self._dirty = True

    # ------------------------------------------------------------------ the file

    @property
    def armed(self) -> bool:
        return self._armed

    def load(self) -> int:
        """
        Read the file once, at start-up, and allow writing from now on. Returns how many workers' history was
        taken. Whatever is wrong with the file (absent, unreadable, not JSON, the wrong shape, an entry that
        makes no sense, one not seen for a day) is "no history" for that part of it, never an error.
        """
        taken = 0
        try:
            path = self.path_fn() if self.path_fn is not None else ""
            document = None
            if path and os.path.exists(path):
                with open(path, "r", encoding="utf-8") as handle:
                    document = json.load(handle)
            taken = self._adopt(document)
        except Exception:  # noqa: BLE001 - an unreadable file is no history, whatever the reason
            taken = 0
        self._armed = True
        return taken

    def _adopt(self, document: Any) -> int:
        if not isinstance(document, dict) or document.get("version") != FILE_VERSION:
            return 0
        workers = document.get("workers")
        if not isinstance(workers, dict):
            return 0
        now = self.now()
        found = []
        for worker_id, record in workers.items():
            if not isinstance(worker_id, str) or not 0 < len(worker_id) <= MAX_ID_CHARS or not isinstance(record, dict):
                continue
            since, seen = _number(record.get("since")), _number(record.get("seen"))
            if since is None or seen is None:
                continue
            since, seen = min(since, now), min(seen, now)
            if now - seen >= self.forget_after:
                continue
            found.append((seen, worker_id, since))
        found.sort(reverse=True)                      # the most recently seen survive a file with too many
        taken = 0
        with self._lock:
            for seen, worker_id, since in found[:self.max_tracked]:
                if worker_id not in self._since:      # what this process has already seen is newer than the file
                    self._since[worker_id], self._seen[worker_id] = since, seen
                    taken += 1
        return taken

    def maybe_save(self) -> bool:
        """Write the file when it is armed, something changed, and the last write is at least ``save_every`` ago."""
        if not self._armed:
            return False
        now = self.now()
        with self._lock:
            if self._last_save is not None and 0.0 <= now - self._last_save < self.save_every:
                return False
            refresh = bool(self._since) and (self._last_save is None or now - self._last_save >= self.refresh_every)
            if not self._dirty and not refresh:
                return False
        return self.save()

    def flush(self) -> bool:
        """Write the file now, whatever the schedule says (clean shutdown). Does nothing before ``load()``."""
        return self.save()

    def save(self) -> bool:
        """Write the file atomically; True when it was written. A failure is reported as False, never raised."""
        if not self._armed or self.path_fn is None:
            return False
        now = self.now()
        with self._lock:
            for worker_id in [w for w in self._since if now - self._seen.get(w, 0.0) >= self.forget_after]:
                self._drop(worker_id)
            workers = {w: {"since": since, "seen": self._seen.get(w, since)} for w, since in self._since.items()}
            self._dirty = False
            self._last_save = now
        document = {"version": FILE_VERSION, "saved_at": now, "workers": workers}
        with self._io_lock:
            temporary = ""
            try:
                path = self.path_fn()
                os.makedirs(os.path.dirname(path), exist_ok=True)
                temporary = f"{path}.{os.getpid()}.tmp"
                with open(temporary, "w", encoding="utf-8") as handle:
                    json.dump(document, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
                return True
            except Exception:  # noqa: BLE001 - the clock is still right in memory; try again at the next change
                with self._lock:
                    self._dirty = True
                try:
                    if temporary:
                        os.remove(temporary)
                except OSError:
                    pass
                return False
