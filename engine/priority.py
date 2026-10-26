"""
Who goes first on the GPU coding engine, and how much a CPU contributor may use it.

DLNE's rule is "give compute to receive compute", graded by what is given:

    tier     who                                           priority   coding use
    host     the coordinator's own machine (BarrkBOT)      1st        unlimited
    key      an operator-issued API key                    1st        unlimited
    gpu      a contributor lending a GPU                   1st        unlimited
    open     anyone, when the contribution rule is off     2nd        unlimited
    cpu      a contributor lending a CPU (the phone pool)  2nd        a rolling window
    trial    everyone else, on the 100 free chats          3rd        the free trial

PRIORITY. llama-server runs a fixed number of request slots and serves whatever arrives in
arrival order, so priority has to be decided before a request reaches it. ``CodingGate``
hands out those slots: a request takes one when the engine has a free slot and nobody is
waiting, otherwise it queues, and a freed slot goes to the best-placed waiter (lowest
priority number, then the earliest arrival). Nobody is pre-empted: a running answer always
finishes.

THE LIMITER. The big hosted services cap use in a rolling window ("resets in 4 hours")
rather than a daily counter that cliffs at midnight. ``UsageWindow`` does the same for CPU
contributors' coding chats: N chats in any W-hour span, with the time the oldest one ages out
reported as the reset. It lives in memory, so a coordinator restart forgives everyone, which
errs on the generous side.
"""

import asyncio
import heapq
import itertools
import threading
import time
from collections import OrderedDict, deque
from typing import Deque, Dict, List, Optional, Tuple

TIER_PRIORITY = {"host": 0, "key": 0, "gpu": 0, "open": 1, "cpu": 1, "trial": 2}
LOWEST_PRIORITY = max(TIER_PRIORITY.values())


def tier_priority(tier: Optional[str]) -> int:
    return TIER_PRIORITY.get(tier or "", LOWEST_PRIORITY)


class CodingGate:
    """
    The GPU coding engine's request slots, handed out by priority (see the module docs).

    One asyncio event loop at a time: a different running loop (a restarted server, or a
    test's fresh ``asyncio.run``) starts from a clean state rather than inheriting futures
    bound to a dead loop.
    """

    def __init__(self, slots: int = 2) -> None:
        self._slots = max(1, int(slots))
        self._in_use = 0
        self._waiters: List[list] = []          # heap of [priority, seq, future]
        self._seq = itertools.count()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def _sync_loop(self) -> asyncio.AbstractEventLoop:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._loop, self._in_use, self._waiters = loop, 0, []
        return loop

    def resize(self, slots: int) -> None:
        """Follow the engine's slot count (it is restarted with more lanes under load)."""
        self._slots = max(1, int(slots))
        while self._in_use < self._slots and self._wake_next():
            self._in_use += 1

    async def acquire(self, priority: int, slots: Optional[int] = None) -> None:
        loop = self._sync_loop()
        if slots is not None and int(slots) != self._slots:
            self.resize(slots)
        if self._in_use < self._slots and not self._live_waiters():
            self._in_use += 1
            return
        future = loop.create_future()
        heapq.heappush(self._waiters, [int(priority), next(self._seq), future])
        try:
            await future
        except asyncio.CancelledError:
            # Handed a slot in the same instant the request went away: give it on.
            if future.done() and not future.cancelled():
                self.release()
            else:
                future.cancel()
            raise

    def release(self) -> None:
        if self._loop is not None and self._loop.is_closed():
            return
        # Over capacity (the engine came back with fewer slots): retire the slot instead of
        # handing it on, until the count is back within the engine's.
        if self._in_use > self._slots or not self._wake_next():
            self._in_use = max(0, self._in_use - 1)

    def _wake_next(self) -> bool:
        """Pass a slot straight to the best-placed live waiter. False when nobody waits."""
        while self._waiters:
            _priority, _seq, future = heapq.heappop(self._waiters)
            if not future.done():
                future.set_result(None)
                return True
        return False

    def _live_waiters(self) -> int:
        return sum(1 for _p, _s, f in self._waiters if not f.done())

    def stats(self) -> Dict[str, int]:
        waiting = [p for p, _s, f in self._waiters if not f.done()]
        return {
            "slots": self._slots,
            "in_use": self._in_use,
            "waiting": len(waiting),
            "waiting_by_priority": {str(p): waiting.count(p) for p in sorted(set(waiting))},
        }


class UsageWindow:
    """
    At most ``limit`` uses per key in any ``window_seconds`` (a sliding window).

    Keys are kept least recently used first, so each check also drops the keys that went
    quiet for a whole window from the front: amortised O(1), and the table only ever holds
    the keys active in the last window (the phone throttle sees every network that asks).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._uses: "OrderedDict[str, Deque[float]]" = OrderedDict()

    def check_and_record(self, key: str, limit: int, window_seconds: float,
                         now: Optional[float] = None) -> Tuple[bool, int, float]:
        """
        Spend one use for ``key`` if it has one left. Returns ``(allowed, remaining,
        reset_at)``: ``remaining`` after this use, ``reset_at`` the epoch second the oldest
        counted use ages out (when one more becomes available).
        """
        now = time.time() if now is None else now
        with self._lock:
            while self._uses:
                oldest_key, oldest = next(iter(self._uses.items()))
                if oldest and oldest[-1] > now - window_seconds:
                    break
                del self._uses[oldest_key]
            uses = self._uses.setdefault(key, deque())
            while uses and uses[0] <= now - window_seconds:
                uses.popleft()
            if len(uses) >= limit:
                return False, 0, uses[0] + window_seconds
            uses.append(now)
            self._uses.move_to_end(key)
            return True, limit - len(uses), uses[0] + window_seconds

    def peek(self, key: str, limit: int, window_seconds: float,
             now: Optional[float] = None) -> Tuple[int, Optional[float]]:
        """``(remaining, reset_at)`` without spending anything; reset_at None when unused."""
        now = time.time() if now is None else now
        with self._lock:
            uses = [t for t in self._uses.get(key, ()) if t > now - window_seconds]
        return max(0, limit - len(uses)), (uses[0] + window_seconds if uses else None)

    def forget_idle(self, window_seconds: float, now: Optional[float] = None) -> None:
        """Drop keys with nothing left in the window, so the table tracks only active users."""
        now = time.time() if now is None else now
        with self._lock:
            for key in [k for k, v in self._uses.items() if not v or v[-1] <= now - window_seconds]:
                del self._uses[key]


coding_gate = CodingGate()
cpu_coding_window = UsageWindow()
