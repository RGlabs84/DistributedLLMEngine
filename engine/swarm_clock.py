"""
The coordinator's time, as a worker sees it.

Every signed message a worker sends (its heartbeats, a tunnel enrollment, a coding member's identity block)
carries Unix epoch seconds, and the coordinator refuses anything more than 30 s from its own clock: that window
is what makes a captured message useless soon after (engine/authn.py). Epoch seconds are the same in every time
zone, so a correctly set clock anywhere in the world is fine. A clock that is simply wrong is not:

* a machine without automatic time sync drifts by seconds, then minutes;
* a dual-boot PC whose hardware clock Windows keeps in local time while Linux reads it as UTC (or the reverse)
  runs off by its UTC offset: whole hours, 4 or 5 in New York, 8 in Taipei, 5 h 30 min in India;
* a wrong time zone with the wall time set right by hand is off the same way.

Such a worker was refused at its first heartbeat and stopped, telling its owner that the network key was wrong.

So the worker keeps an offset (the coordinator's time minus its own) learned from the coordinator's answers to
its requests: the ``X-DLNE-Time`` header the coordinator puts on every ``/worker/`` answer (Unix seconds,
millisecond resolution), else the standard ``Date`` header, which HTTP always gives in GMT. The offset is applied
to every timestamp the worker signs, and it is learned again from every answer, so a clock that is corrected or
jumps later is followed too. The coordinator's own clock stays the reference and its 30 s window is unchanged.

Trust: the offset comes only from the coordinator's own answers to this worker. Over HTTPS (the public swarm)
nobody can alter them; over plain HTTP an on-path attacker could, but such an attacker already reads the worker's
token in every heartbeat.
"""

import email.utils
import math
import threading
import time
from typing import Any, Callable, Optional

CLOCK_HEADER = "X-DLNE-Time"

# An answer that took longer than this to arrive says too little about when the coordinator wrote its time.
MAX_ROUND_TRIP_SECONDS = 10.0
# Coordinator times outside these bounds are nonsense (2025-01-01 .. 2100-01-01), never an offset to apply.
_PLAUSIBLE = (1_735_689_600.0, 4_102_444_800.0)
# The coordinator's window is 30 s. Below this the clock needs no word to its owner.
REPORT_FROM_SECONDS = 30.0
# Time zones are whole multiples of 15 minutes from UTC; an offset this close to one is most likely a zone.
_ZONE_STEP_SECONDS = 15 * 60
_ZONE_TOLERANCE_SECONDS = 90.0


def _duration(seconds: float) -> str:
    """'5 h', '5 h 30 min', '3 min 12 s', '45 s'."""
    whole = int(round(abs(seconds)))
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    if minutes:
        return f"{minutes} min {secs} s" if secs else f"{minutes} min"
    return f"{secs} s"


def looks_like_time_zone(offset: float) -> bool:
    """True when the offset is (nearly) a whole number of time-zone steps, at least half an hour."""
    size = abs(offset)
    if size < 30 * 60 - _ZONE_TOLERANCE_SECONDS:
        return False
    nearest = round(size / _ZONE_STEP_SECONDS) * _ZONE_STEP_SECONDS
    return abs(size - nearest) <= _ZONE_TOLERANCE_SECONDS


def describe(offset: float) -> str:
    """
    What to tell the machine's owner about a clock ``offset`` seconds behind (positive) or ahead of (negative) the
    coordinator's; empty when it is within the coordinator's window.
    """
    if not math.isfinite(offset) or abs(offset) < REPORT_FROM_SECONDS:
        return ""
    where = "behind" if offset > 0 else "ahead of"
    text = (f"This computer's clock is {_duration(offset)} {where} the swarm's. DLNE corrects for it, so contributing "
            "works. ")
    if looks_like_time_zone(offset):
        text += ("A difference of whole hours usually comes from a time zone: the computer's time zone is set wrong, "
                 "or, on a PC that also runs Windows, Windows keeps the hardware clock in local time while Linux reads "
                 "it as UTC. Turning on automatic time and time zone in the system settings fixes the clock itself.")
    else:
        text += "Turning on automatic time sync in the system settings fixes the clock itself."
    return text


def _header(headers: Any, name: str) -> Optional[str]:
    """A header's value, whatever the case of its name (httpx's headers already ignore it; a plain dict does not)."""
    try:
        value = headers.get(name)
        if value is None:
            wanted = name.lower()
            value = next((v for k, v in headers.items() if str(k).lower() == wanted), None)
    except Exception:  # noqa: BLE001 - whatever this is, it holds no clock
        return None
    return None if value is None else str(value)


def server_time_from(headers: Any) -> Optional[float]:
    """
    The coordinator's time from an answer's headers, as Unix seconds: ``X-DLNE-Time``, else ``Date``. A ``Date``
    header names its zone (always GMT in HTTP) and is read as such, never as this machine's local time. None when
    neither is present and sensible.
    """
    if headers is None:
        return None
    exact = _header(headers, CLOCK_HEADER)
    if exact:
        try:
            value = float(str(exact).strip())
            if math.isfinite(value) and _PLAUSIBLE[0] <= value <= _PLAUSIBLE[1]:
                return value
        except ValueError:
            pass
    date = _header(headers, "Date")
    if date:
        try:
            parsed = email.utils.parsedate_to_datetime(str(date))
        except (TypeError, ValueError, IndexError):
            parsed = None
        if parsed is not None and parsed.tzinfo is not None:
            # Whole seconds: the coordinator wrote it somewhere within that second.
            value = parsed.timestamp() + 0.5
            if _PLAUSIBLE[0] <= value <= _PLAUSIBLE[1]:
                return value
    return None


class SwarmClock:
    """This machine's clock corrected to the coordinator's. Thread-safe."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._offset = 0.0
        self._learned = False
        self._reported: Optional[float] = None
        self._lock = threading.Lock()

    def local(self) -> float:
        """This machine's own clock (what to pass as ``sent_at`` and ``received_at``)."""
        return float(self._clock())

    def now(self) -> float:
        """The coordinator's time as best known: what every signed timestamp uses."""
        with self._lock:
            return self.local() + self._offset

    @property
    def offset(self) -> float:
        """The coordinator's time minus this machine's, in seconds (positive: this clock is behind)."""
        with self._lock:
            return self._offset

    def learn(self, server_time: Optional[float], sent_at: float, received_at: float) -> bool:
        """
        Takes the offset from one exchange: the coordinator's time against the middle of the request's round trip.
        False (nothing changed) when the answer gives no usable time or took too long to arrive.
        """
        if server_time is None or not all(math.isfinite(v) for v in (server_time, sent_at, received_at)):
            return False
        round_trip = received_at - sent_at
        if round_trip < 0 or round_trip > MAX_ROUND_TRIP_SECONDS:
            return False
        with self._lock:
            self._offset = server_time - (sent_at + received_at) / 2.0
            self._learned = True
        return True

    def learn_from(self, response: Any, sent_at: float, received_at: float) -> bool:
        """``learn`` from an HTTP response's headers (anything with ``.headers``, or the headers themselves)."""
        return self.learn(server_time_from(getattr(response, "headers", response)), sent_at, received_at)

    def report(self) -> str:
        """
        What to tell the owner, once per change: the message for the current offset when it is outside the
        coordinator's window and has moved by more than that window since it was last told; else empty.
        """
        with self._lock:
            offset, learned, last = self._offset, self._learned, self._reported
            if not learned:
                return ""
            if abs(offset) < REPORT_FROM_SECONDS:
                self._reported = None          # back in step: a later drift is worth a word again
                return ""
            if last is not None and abs(offset - last) < REPORT_FROM_SECONDS:
                return ""
            self._reported = offset
        return describe(offset)

    def reset(self) -> None:
        """Forget the offset (tests)."""
        with self._lock:
            self._offset, self._learned, self._reported = 0.0, False, None


# The one clock every signer in this process uses.
swarm_clock = SwarmClock()
