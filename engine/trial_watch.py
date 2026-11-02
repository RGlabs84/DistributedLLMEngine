"""
Trial events (2.9.0 section 6d): who is starting, halving and using up the free coding trial, and when,
so a string of fresh networks burnt one after another can be seen. Nothing is blocked.

The record of the trial itself (config/starter_quota.json) keeps no times. This keeps a small event
log beside it, config/trial_events.json: ``{"t": epoch, "tag": "<masked network>", "kind": "start|half|used_up"}``,
each kind at most once per masked network. The network is never written: only a keyed hash
(HMAC-SHA256 with a random key in config/trial_key, created if missing, mode 0600, never logged).
"""
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from typing import Any, Callable, Dict, List, Optional

KINDS = ("start", "half", "used_up")
KEEP_SECONDS = 35 * 86400          # older events are dropped when the file is next written
DEFAULT_BURST_PER_HOUR = 10


class TrialWatch:
    def __init__(self, config_dir: Callable[[], str], log: Callable[[str], None],
                 warn: Callable[[str], None], clock: Callable[[], float] = time.time):
        self._config_dir = config_dir
        self._log = log
        self._warn = warn
        self._clock = clock
        self._lock = threading.Lock()
        self._events: Optional[List[Dict[str, Any]]] = None
        self._key: Optional[bytes] = None
        self._seen: set = set()
        self._warned_bucket = -1

    # -- files -------------------------------------------------------------------------
    def _path(self, name: str) -> str:
        return os.path.join(self._config_dir(), name)

    def _load_key(self) -> bytes:
        if self._key is not None:
            return self._key
        p = self._path("trial_key")
        try:
            with open(p, "r", encoding="ascii") as f:
                text = f.read().strip()
            if len(text) >= 32:
                self._key = bytes.fromhex(text)
                return self._key
        except (OSError, ValueError):
            pass
        key = secrets.token_bytes(32)
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as f:
                f.write(key.hex())
        except OSError:
            pass                    # unwritable: a per-process key still masks, tags just change at restart
        self._key = key
        return key

    def _load(self) -> List[Dict[str, Any]]:
        if self._events is None:
            events: List[Dict[str, Any]] = []
            try:
                with open(self._path("trial_events.json"), "r", encoding="utf-8") as f:
                    raw = json.load(f)
                for e in raw if isinstance(raw, list) else []:
                    if isinstance(e, dict) and e.get("kind") in KINDS and isinstance(e.get("tag"), str) \
                            and isinstance(e.get("t"), (int, float)):
                        events.append({"t": float(e["t"]), "tag": e["tag"], "kind": e["kind"]})
            except (OSError, ValueError):
                pass
            self._events = events
            self._seen = {(e["tag"], e["kind"]) for e in events}
        return self._events

    def _save(self) -> None:
        p = self._path("trial_events.json")
        tmp = f"{p}.tmp"
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._events, f)
            os.replace(tmp, p)
        except OSError:
            pass

    # -- events ------------------------------------------------------------------------
    def mask(self, network: str) -> str:
        return hmac.new(self._load_key(), network.encode("utf-8"), hashlib.sha256).hexdigest()[:12]

    def record(self, network: str, used_before: int, used_after: int, limit: int) -> None:
        """Called after a chat was charged. Logs each of start / half / used up once per network."""
        if limit <= 0 or used_after <= used_before:
            return
        kinds = []
        if used_before == 0 and used_after >= 1:
            kinds.append("start")
        half = (limit + 1) // 2
        if used_before < half <= used_after and limit > 1:
            kinds.append("half")
        if used_before < limit <= used_after:
            kinds.append("used_up")
        if not kinds:
            return
        with self._lock:
            events = self._load()
            tag = self.mask(network)
            now = self._clock()
            fresh = [k for k in kinds if (tag, k) not in self._seen]
            if not fresh:
                return
            for k in fresh:
                self._seen.add((tag, k))
                events.append({"t": now, "tag": tag, "kind": k})
                self._log(f"Trial {k.replace('_', ' ')}: network {tag}.")
            cutoff = now - KEEP_SECONDS
            if events and events[0]["t"] < cutoff:
                events[:] = [e for e in events if e["t"] >= cutoff]
            self._save()
            if "start" in fresh:
                self._check_burst(now)

    def _starts_since(self, since: float) -> int:
        return sum(1 for e in self._load() if e["kind"] == "start" and e["t"] >= since)

    def _check_burst(self, now: float) -> None:
        limit = burst_limit()
        n = self._starts_since(now - 3600)
        bucket = int(now // 3600)
        if n > limit and bucket != self._warned_bucket:
            self._warned_bucket = bucket
            self._warn(f"Trial burst: {n} fresh networks started the free trial in the last hour "
                       f"(limit {limit}). Nothing is blocked.")

    # -- stats -------------------------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        with self._lock:
            events = list(self._load())
            now = self._clock()
        lt = time.localtime(now)
        midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        week = now - 7 * 86400
        limit = burst_limit()

        def count(kind: str, since: float) -> int:
            return sum(1 for e in events if e["kind"] == kind and e["t"] >= since)

        starts = [e["t"] for e in events if e["kind"] == "start"]
        by_hour: Dict[int, int] = {}
        for t in starts:
            by_hour[int(t // 3600)] = by_hour.get(int(t // 3600), 0) + 1
        busiest = max(by_hour.items(), key=lambda kv: kv[1]) if by_hour else None
        last_hour = sum(1 for t in starts if t >= now - 3600)
        return {
            "started_today": count("start", midnight),
            "started_week": count("start", week),
            "half_today": count("half", midnight),
            "used_up_today": count("used_up", midnight),
            "used_up_week": count("used_up", week),
            "fresh_last_hour": last_hour,
            "busiest_hour": {"fresh": busiest[1], "hour_start": int(busiest[0] * 3600)} if busiest else None,
            "burst_limit_per_hour": limit,
            "burst": last_hour > limit,
        }


def burst_limit() -> int:
    """TRIAL_BURST_PER_HOUR from config.txt (default 10)."""
    try:
        from engine.config import get_worker_limits
        value = int(get_worker_limits().get("trial_burst_per_hour", DEFAULT_BURST_PER_HOUR))
        return value if value > 0 else DEFAULT_BURST_PER_HOUR
    except Exception:
        return DEFAULT_BURST_PER_HOUR
