"""
How many tokens the swarm has generated lately, per pool, for the status the website shows
(``gpu_pool`` and ``cpu_pool`` in ``/v1/cluster/status``).

Only counts are kept: a sliding window of one-second buckets per pool, ``record(pool, n)`` when
tokens arrive and ``rate(pool, 60)`` / ``total(pool, 3600)`` to read them back. Nothing about a
request or an answer (no text, no identity, no address) ever reaches this module.

THE POOLS
    ``coding``  the coding engine: this coordinator's own and every coding member's
    ``phone``   the CPU pool answering the phone app

WHEN TOKENS ARE COUNTED. At arrival, not at the end of an answer, so the live rate is smooth: each
streamed SSE ``data:`` frame that carries generated text counts as one token (llama-server sends
one token per frame), a sealed member stream counts one per sealed frame (:class:`SealedFrameCounter`
leaves out the sealed end marker), and a whole (not streamed) answer counts the
``usage.completion_tokens`` it reports once it is complete (:func:`completion_tokens`).
:class:`StreamTokenCounter` does the first of these without parsing a line's JSON, and keeps nothing
of the line.

Thread-safe: the coordinator's event loop and the phone pool's and members' worker threads all
record here. The clock is injectable for tests.
"""

import re
import threading
import time
from collections import deque
from typing import Callable, Deque, Dict, Optional

BUCKET_SECONDS = 1
MAX_WINDOW_SECONDS = 3600            # the longest span anything asks for (tokens in the last hour)

POOL_CODING = "coding"
POOL_PHONE = "phone"


class Throughput:
    """A sliding window of one-second buckets for each pool."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: Dict[str, Deque[list]] = {}          # pool -> [[second, count], ...] oldest first

    def _now(self, now: Optional[float]) -> int:
        return int(self._clock() if now is None else now)

    def record(self, pool: str, n: int = 1, now: Optional[float] = None) -> None:
        """``n`` tokens arrived now."""
        if n <= 0:
            return
        second = self._now(now)
        with self._lock:
            buckets = self._buckets.setdefault(pool, deque())
            if buckets and buckets[-1][0] == second:
                buckets[-1][1] += int(n)
            elif buckets and buckets[-1][0] > second:
                # A clock that stepped back, or a caller's timestamp out of order: put the tokens
                # in the newest bucket rather than reorder the window.
                buckets[-1][1] += int(n)
            else:
                buckets.append([second, int(n)])
            self._expire(buckets, second)

    @staticmethod
    def _expire(buckets: Deque[list], second: int) -> None:
        horizon = second - MAX_WINDOW_SECONDS
        while buckets and buckets[0][0] <= horizon:
            buckets.popleft()

    def total(self, pool: str, seconds: int = MAX_WINDOW_SECONDS, now: Optional[float] = None) -> int:
        """Tokens generated in the last ``seconds`` seconds (the current second included)."""
        second = self._now(now)
        horizon = second - min(int(seconds), MAX_WINDOW_SECONDS)
        with self._lock:
            buckets = self._buckets.get(pool)
            if not buckets:
                return 0
            self._expire(buckets, second)
            count = 0
            for when, n in reversed(buckets):
                if when <= horizon:
                    break
                count += n
            return count

    def rate(self, pool: str, seconds: int = 60, now: Optional[float] = None) -> float:
        """Tokens per second over the last ``seconds`` seconds."""
        seconds = max(1, min(int(seconds), MAX_WINDOW_SECONDS))
        return self.total(pool, seconds, now) / seconds

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


throughput = Throughput()


# ======================================================================================
# Counting what arrives
# ======================================================================================

# A frame that carries generated text: a non-empty string in delta.content or delta.reasoning_content, or
# tool-call arguments. The first match in the line is enough; llama-server sends one token per frame.
_GENERATED = re.compile(rb'"(?:content|reasoning_content)"\s*:\s*"[^"]|"tool_calls"\s*:\s*\[')
_COMPLETION_TOKENS = re.compile(rb'"completion_tokens"\s*:\s*(\d{1,9})')


def completion_tokens(body: bytes) -> int:
    """``usage.completion_tokens`` of a whole (not streamed) answer, or 0 when it reports none."""
    match = _COMPLETION_TOKENS.search(body[-4096:] if len(body) > 4096 else body)
    return int(match.group(1)) if match else 0


class StreamTokenCounter:
    """
    Counts the tokens in an event stream as its bytes arrive, without parsing a line's JSON and
    without keeping anything but the unfinished last line: one per ``data:`` line that carries
    generated text. ``streamed=False`` is for a request that did not ask for a stream, whose answer
    is a single JSON body: it counts ``usage.completion_tokens`` when the body is complete
    (:meth:`finish`). A counter feeds ``record`` as the bytes come in.
    """

    MAX_LINE = 1 << 20           # a line longer than this is not a token frame; its tail is dropped

    def __init__(self, pool: str, streamed: bool = True, sink: Optional[Throughput] = None) -> None:
        self.pool = pool
        self.streamed = streamed
        self._sink = sink or throughput
        self._pending = b""
        self._tail = b""

    def feed(self, chunk: bytes) -> int:
        """Take the next bytes of the answer; returns how many tokens they counted."""
        if not chunk:
            return 0
        if not self.streamed:
            self._tail = (self._tail + chunk)[-4096:]
            return 0
        data = self._pending + chunk
        *lines, self._pending = data.split(b"\n")
        if len(self._pending) > self.MAX_LINE:
            self._pending = b""
        counted = sum(1 for line in lines if self._carries_text(line))
        if counted:
            self._sink.record(self.pool, counted)
        return counted

    def finish(self) -> int:
        """The answer is complete: count its last unfinished line, or a whole answer's reported tokens."""
        counted = 0
        if self.streamed:
            if self._carries_text(self._pending):
                counted = 1
            self._pending = b""
        else:
            counted = completion_tokens(self._tail)
            self._tail = b""
        if counted:
            self._sink.record(self.pool, counted)
        return counted

    @staticmethod
    def _carries_text(line: bytes) -> bool:
        line = line.strip()
        return line.startswith(b"data:") and b"[DONE]" not in line[:16] and _GENERATED.search(line) is not None


class SealedFrameCounter:
    """
    Counts the frames of a sealed member stream (docs/coding-members.md section 6), one token each. The
    coordinator cannot open them, and the last sealed frame before the plain ``[DONE]`` is the sealed
    end marker, which is not a token: so a frame is counted when the next one arrives, and the last
    is never counted. (A stream cut before its end loses one token of count. Nothing else is lost.)
    """

    def __init__(self, pool: str = POOL_CODING, sink: Optional[Throughput] = None) -> None:
        self.pool = pool
        self._sink = sink or throughput
        self._pending = b""
        self._held = False           # a sealed frame has been seen and is not yet known to be a token

    def feed(self, chunk: bytes) -> int:
        if not chunk:
            return 0
        data = self._pending + chunk
        *lines, self._pending = data.split(b"\n")
        if len(self._pending) > StreamTokenCounter.MAX_LINE:
            self._pending = b""
        counted = 0
        for line in lines:
            line = line.strip()
            if line.startswith(b"data:") and b'"dlne_e2e"' in line:
                if self._held:
                    counted += 1
                self._held = True
        if counted:
            self._sink.record(self.pool, counted)
        return counted
