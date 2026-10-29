"""
Which networks have a computer contributing to DLNE, kept across a coordinator restart (2.6.2).

A phone's picture costs one free-trial chat (engine/coordinator.py, ``_charge_phone_image``), except on a contributor's
network: one that any registered worker (a GPU or a CPU, in any role) has sent a heartbeat from now or in the last
``FORGET_AFTER`` (24 hours). The owner's phone on the home Wi-Fi is the owner's laptop's network, so it is never charged
while the laptop lends compute, and for a day after it stops. This book is that memory: network -> when a heartbeat last
came from it.

A network is the free trial's own identity (``coordinator.trial_identity``: an IPv4 address, or an IPv6 /64), so the two
always agree about who is who. Only a heartbeat the coordinator accepted gets here (signed, the right version, a plausible
machine, above the CPU floors, registered), and a heartbeat from this machine itself is left out by the caller: its own
clients are never charged, so it says nothing about anyone else.

The file (``<install>/config/contributor_networks.json``)::

    {"version": 1, "saved_at": 1761480000.0,
     "networks": {"203.0.113.9": 1761479990.0, "2001:db8:1:2::/64": 1761470000.0}}

* The value is when a worker's heartbeat last came from that network (wall-clock seconds, ``time.time``: a monotonic
  clock means nothing after a restart). A time in the future (the machine's clock was set back) is taken as now.
* An entry not seen for ``forget_after`` (24 hours) is dropped when the file is read or written and whenever the book is
  full; at ``max_tracked`` (10,000) entries the one seen longest ago goes. A network's day is judged from this value, so
  after a restart it can run out up to ``refresh_every`` seconds early, never late.
* It is written aside and renamed into place, at most once every ``save_every`` (30 s) seconds when a network was added
  or dropped (and once every ``refresh_every`` seconds while there are entries, to keep the times fresh: a worker's
  heartbeat comes every few seconds and the file does not need each of them), and by ``flush()`` at a clean shutdown.
  A crash loses at most the last 30 seconds of changes.
* It is read once at start-up. A missing, stale, truncated or hand-edited file means "no history": every entry is
  checked, what cannot be trusted is skipped, and nothing here ever raises into a heartbeat.
* A book writes nothing until ``load()`` has run (the coordinator calls it at start-up), so tests and tools that merely
  import the coordinator never touch the installation's files.

The file holds network addresses, like ``config/starter_quota.json`` does, and nothing else: no worker id, no hardware.
"""

import ipaddress
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
MAX_TRACKED = 10_000
MAX_ID_CHARS = 64          # an IPv6 /64 written out is 43


def _number(value: Any) -> Optional[float]:
    """A finite non-negative number from a JSON value (a bool is not a number here), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0.0 else None


def _is_network(value: Any) -> bool:
    """Whether ``value`` is something ``coordinator.trial_identity`` can make of an address: an IPv4 address or an IPv6 /64,
    written the way Python writes them. Anything else ("unknown", a header fragment, junk in a hand-edited file) is no
    network and never becomes a contributor."""
    if not isinstance(value, str) or not 0 < len(value) <= MAX_ID_CHARS:
        return False
    try:
        if "/" not in value:
            address = ipaddress.ip_address(value)
            return address.version == 4 and str(address) == value
        network = ipaddress.ip_network(value, strict=True)
    except ValueError:
        return False
    return network.version == 6 and network.prefixlen == 64 and str(network) == value


class ContributorBook:
    """Network -> when a worker's heartbeat last came from it, with the file behind it. Thread-safe."""

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
        self._seen: Dict[str, float] = {}
        self._dirty = False
        self._armed = False
        self._last_save: Optional[float] = None

    # ------------------------------------------------------------------ the book

    def now(self) -> float:
        return float(self.clock())

    def __len__(self) -> int:
        with self._lock:
            return len(self._seen)

    def tracked(self) -> Dict[str, float]:
        """A copy: network -> when a heartbeat last came from it."""
        with self._lock:
            return dict(self._seen)

    def seen(self, network: str) -> Optional[float]:
        if not isinstance(network, str):
            return None
        with self._lock:
            return self._seen.get(network)

    def note(self, network: str) -> None:
        """A heartbeat the coordinator accepted just came from ``network``. A value that is no network is ignored."""
        if not _is_network(network):
            return
        now = self.now()
        with self._lock:
            before = self._seen.get(network)
            if before is None:
                self._make_room(now)
            if before is None or now - min(before, now) >= self.forget_after:
                self._dirty = True              # new, or back after a day: what the file holds is wrong or missing
            self._seen[network] = now
        self.maybe_save()

    def is_contributor(self, network: str) -> bool:
        """Whether a heartbeat came from ``network`` now or in the last ``forget_after`` seconds."""
        seen = self.seen(network)
        now = self.now()
        return seen is not None and now - min(seen, now) < self.forget_after

    def _make_room(self, now: float) -> None:
        """Before adding an entry: drop the ones not seen for a day, then the one seen longest ago."""
        if len(self._seen) < self.max_tracked:
            return
        for network in [n for n, seen in self._seen.items() if now - min(seen, now) >= self.forget_after]:
            self._drop(network)
        while len(self._seen) >= self.max_tracked:
            self._drop(min(self._seen, key=self._seen.get))

    def _drop(self, network: str) -> None:
        self._seen.pop(network, None)
        self._dirty = True

    # ------------------------------------------------------------------ the file

    @property
    def armed(self) -> bool:
        return self._armed

    def load(self) -> int:
        """
        Read the file once, at start-up, and allow writing from now on. Returns how many networks were taken. Whatever is
        wrong with the file (absent, unreadable, not JSON, the wrong shape, an entry that makes no sense, one not seen for
        a day) is "no history" for that part of it, never an error.
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
        networks = document.get("networks")
        if not isinstance(networks, dict):
            return 0
        now = self.now()
        found = []
        for network, value in networks.items():
            seen = _number(value)
            if seen is None or not _is_network(network):
                continue
            seen = min(seen, now)
            if now - seen >= self.forget_after:
                continue
            found.append((seen, network))
        found.sort(reverse=True)                      # the most recently seen survive a file with too many
        taken = 0
        with self._lock:
            for seen, network in found[:self.max_tracked]:
                if network not in self._seen:         # what this process has already seen is newer than the file
                    self._seen[network] = seen
                    taken += 1
        return taken

    def maybe_save(self) -> bool:
        """Write the file when it is armed, something changed (or it is due a refresh), and the last write is at least
        ``save_every`` ago."""
        if not self._armed:
            return False
        now = self.now()
        with self._lock:
            if self._last_save is not None and 0.0 <= now - self._last_save < self.save_every:
                return False
            refresh = bool(self._seen) and (self._last_save is None or now - self._last_save >= self.refresh_every)
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
            for network in [n for n, seen in self._seen.items() if now - min(seen, now) >= self.forget_after]:
                self._drop(network)
            networks = dict(self._seen)
            self._dirty = False
            self._last_save = now
        document = {"version": FILE_VERSION, "saved_at": now, "networks": networks}
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
            except Exception:  # noqa: BLE001 - the book is still right in memory; try again at the next change
                with self._lock:
                    self._dirty = True
                try:
                    if temporary:
                        os.remove(temporary)
                except OSError:
                    pass
                return False
