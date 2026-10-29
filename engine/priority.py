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

    async def acquire(self, priority: int, slots: Optional[int] = None, ticket: Optional[dict] = None) -> None:
        """
        Take a slot, waiting for one in priority order if none is free. A ``ticket`` (an empty dict
        the caller keeps) gets this request's place in the queue once it has to wait, so
        :meth:`position` can say where it stands while another coroutine awaits this one.
        """
        loop = self._sync_loop()
        if slots is not None and int(slots) != self._slots:
            self.resize(slots)
        if self._in_use < self._slots and not self._live_waiters():
            self._in_use += 1
            return
        future = loop.create_future()
        entry = [int(priority), next(self._seq), future]
        if ticket is not None:
            ticket["entry"] = entry
        heapq.heappush(self._waiters, entry)
        try:
            await future
        except asyncio.CancelledError:
            # Handed a slot in the same instant the request went away: give it on.
            if future.done() and not future.cancelled():
                self.release()
            else:
                future.cancel()
            raise

    def try_acquire(self) -> bool:
        """Take a slot right now if one is free and nobody is waiting; never waits (False: none is free).
        Must be called from a running event loop, like :meth:`acquire`. The caller releases it."""
        self._sync_loop()
        if self._in_use < self._slots and not self._live_waiters():
            self._in_use += 1
            return True
        return False

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

    def position(self, ticket: Optional[dict]) -> Optional[int]:
        """
        Where the request waiting under ``ticket`` stands: 1 is next to get a slot, 2 has one request
        ahead of it, and so on, in the order slots are handed out (better priority first, then the
        earliest arrival). None when it is not waiting: it never had to, or was granted or cancelled.
        """
        entry = (ticket or {}).get("entry")
        if entry is None or entry[2].done():
            return None
        return 1 + sum(1 for p, s, f in self._waiters if not f.done() and (p, s) < (entry[0], entry[1]))

    def stats(self) -> Dict[str, int]:
        waiting = [p for p, _s, f in self._waiters if not f.done()]
        return {
            "slots": self._slots,
            "in_use": self._in_use,
            "waiting": len(waiting),
            "waiting_by_priority": {str(p): waiting.count(p) for p in sorted(set(waiting))},
        }


class LaneBook:
    """
    Which of the engine's slots (lanes) each request is pinned to.

    With phone lanes the coordinator gives every engine request an ``id_slot`` itself: left to
    llama-server a request goes to whichever idle slot matches its prompt best, which would put a
    coding prompt on a phone's lane. A busy lane is not rerouted, the request waits for it, so
    ``take`` picks the lane with the fewest requests on it. Coding owns lanes ``0..coding-1`` and
    phones the lanes after them; the gates bound how many run at once, the book only says where.

    Two kinds of holds are counted apart. A chat (the default) keeps its lane for a whole answer and the gates
    allow one per lane; autocomplete and the benchmark (``short=True``) are over in a moment and do not go through
    the gate, so a chat does not count them when it chooses: otherwise a 200 ms completion on the idle lane
    would send the next chat to wait behind the other chat's whole answer (2.6.0). A short hold prefers a lane
    with no chat on it and may share one.
    """

    def __init__(self) -> None:
        self._uses: Dict[int, int] = {}      # every hold, chat or short
        self._short: Dict[int, int] = {}     # the short ones among them

    def take(self, first: int, count: int, *, short: bool = False, prefer: Optional[int] = None) -> int:
        """
        A lane of ``first .. first+count-1``, now counted as used; give it back with :meth:`give` (same ``short``).
        A chat gets the lane with the fewest chats on it, then the fewest short holds, then the lowest index. A
        short hold gets the one with the fewest chats, then the fewest holds of any kind. ``prefer`` (a lane this
        chat used before, for its cache) is taken when it is in range and no chat is on it.
        """
        lanes = range(first, first + max(1, count))
        if prefer in lanes and self._chats(prefer) == 0:
            lane = prefer
        elif short:
            lane = min(lanes, key=lambda index: (self._chats(index), self._uses.get(index, 0), index))
        else:
            lane = min(lanes, key=lambda index: (self._chats(index), self._short.get(index, 0), index))
        self._uses[lane] = self._uses.get(lane, 0) + 1
        if short:
            self._short[lane] = self._short.get(lane, 0) + 1
        return lane

    def give(self, lane: int, *, short: bool = False) -> None:
        if self._uses.get(lane, 0) > 0:
            self._uses[lane] -= 1
            if short and self._short.get(lane, 0) > 0:
                self._short[lane] -= 1

    def _chats(self, lane: int) -> int:
        return self._uses.get(lane, 0) - self._short.get(lane, 0)


class LaneAffinity:
    """
    The lane each chat last used (a small least-recently-used table), so a chat's next turn can go back to the
    lane that still holds its cached prompt. Keyed by the chat's HMAC tag (engine/chat_privacy.chat_tag), never
    the id the phone sent; nothing about a network or a person.
    """

    def __init__(self, size: int = 512) -> None:
        self._size = size
        self._lanes: "OrderedDict[str, int]" = OrderedDict()

    def lane_for(self, key: Optional[str]) -> Optional[int]:
        return self._lanes.get(key) if key else None

    def remember(self, key: Optional[str], lane: int) -> None:
        if not key:
            return
        self._lanes[key] = lane
        self._lanes.move_to_end(key)
        while len(self._lanes) > self._size:
            self._lanes.popitem(last=False)


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
                         now: Optional[float] = None, count: int = 1) -> Tuple[bool, int, float]:
        """
        Spend ``count`` uses (default one) for ``key`` if it has that many left, all or none. Returns
        ``(allowed, remaining, reset_at)``: ``remaining`` after this use, ``reset_at`` the epoch second
        the oldest counted use ages out (when one more becomes available); when refused, the second
        enough have aged out for ``count`` to fit (never, if ``count`` is over the limit: now + the window).
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
            if len(uses) + count > limit:
                aging = len(uses) + count - limit        # this many of the oldest must leave the window first
                reset = uses[aging - 1] + window_seconds if 0 < aging <= len(uses) else now + window_seconds
                return False, max(0, limit - len(uses)), reset
            uses.extend([now] * count)
            self._uses.move_to_end(key)
            return True, limit - len(uses), uses[0] + window_seconds

    def refund(self, key: str, count: int = 1) -> None:
        """Gives back the newest ``count`` uses of ``key``: spent on a request that was then refused, or that the
        engine never answered."""
        with self._lock:
            uses = self._uses.get(key)
            for _ in range(min(count, len(uses) if uses else 0)):
                uses.pop()

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


class DistinctWindow:
    """
    At most ``limit`` DIFFERENT names per key in any ``window_seconds``, where a name that is already counted
    inside the window is free to use again: the image budget (a photo sent again with the next question is the
    same photo, not another one). A name counts from the moment it was first spent until ``window_seconds``
    later, however often it comes back meanwhile, and is new again after that.

    Like :class:`UsageWindow`, each check also drops the keys that went quiet for a whole window from the front,
    so the table only holds the keys active in the last window; a key never holds more than ``limit`` names.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # key -> {name: when it was first spent}, the oldest name first; the keys by the time of their newest name
        self._names: "OrderedDict[str, OrderedDict[str, float]]" = OrderedDict()

    def check_and_record(self, key: str, names: List[str], limit: int, window_seconds: float,
                         now: Optional[float] = None) -> Tuple[bool, int, float]:
        """
        Spend the names of ``names`` that ``key`` has not spent inside the window (each name once, however many
        times it is listed), all or none, if ``limit`` allows. Returns ``(allowed, remaining, reset_at)`` as
        :meth:`UsageWindow.check_and_record` does: ``remaining`` after this use, ``reset_at`` the epoch second
        the oldest counted name ages out; when refused, the second enough have aged out for the new ones to fit
        (never, if they alone are over the limit: now + the window).
        """
        now = time.time() if now is None else now
        wanted = list(dict.fromkeys(names))
        with self._lock:
            while self._names:
                oldest_key, spent = next(iter(self._names.items()))
                if spent and next(reversed(spent.values())) > now - window_seconds:
                    break
                del self._names[oldest_key]
            spent = self._names.setdefault(key, OrderedDict())
            while spent and next(iter(spent.values())) <= now - window_seconds:
                spent.popitem(last=False)
            new = [name for name in wanted if name not in spent]
            if len(spent) + len(new) > limit:
                aging = len(spent) + len(new) - limit        # this many of the oldest must leave the window first
                times = list(spent.values())
                reset = times[aging - 1] + window_seconds if 0 < aging <= len(times) else now + window_seconds
                return False, max(0, limit - len(spent)), reset
            for name in new:
                spent[name] = now
            if new:
                self._names.move_to_end(key)
            return True, limit - len(spent), next(iter(spent.values()), now) + window_seconds


coding_gate = CodingGate()
phone_gate = CodingGate()       # the phone lanes (2.6): a phone never waits behind coding, nor coding behind a phone
lane_book = LaneBook()
lane_affinity = LaneAffinity()
cpu_coding_window = UsageWindow()
