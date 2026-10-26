"""
How fast the coding model really answers, measured on real answers.

llama-server ends every answer with a "timings" object: in a stream it is the last field of
the final chunk (the one carrying finish_reason, just before "data: [DONE]"); in a plain
response it is part of the body. Checked against TheWall's engine (llama.cpp b11838) on
2026-10-25:

    data: {"choices":[{"finish_reason":"length",...}],...,"timings":{"cache_n":0,"prompt_n":20,
          ...,"predicted_n":16,"predicted_ms":396.896,...,"predicted_per_second":37.79...}}
    data: [DONE]

The proxy keeps only a rolling tail of the bytes it forwards (TAIL_BYTES; nothing is parsed
per chunk) and hands the tail to :meth:`SpeedMeter.observe` once the answer has ended. Only
answers of at least MIN_PREDICTED_TOKENS tokens count: a short answer's rate is dominated by
start-up noise. The last WINDOW samples per model are kept, the median is what is reported,
and each model's samples and median are saved to config/coding_speed.json (atomically) so a
restart does not blank the number.
"""

import json
import math
import os
import re
import statistics
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from engine import config as _config

SPEED_FILE = "coding_speed.json"
MIN_PREDICTED_TOKENS = 64
WINDOW = 20
TAIL_BYTES = 8192
MAX_MODELS = 32                 # files remembered; the oldest measurement goes first
MAX_TOKENS_PER_SECOND = 100000.0

_TIMINGS_KEY = b'"timings"'
_AFTER_KEY_RE = re.compile(rb"\s*:\s*\{")


def parse_timings(tail: bytes) -> Optional[Tuple[int, float]]:
    """
    ``(predicted_n, predicted_per_second)`` from the last llama-server "timings" object in
    ``tail``, or None when there is none or it is not well formed.

    The model's own text cannot fake one: inside a JSON string the quotes are escaped
    (``\\"timings\\"``), so the raw bytes ``"timings"`` only ever come from the server, and
    the server writes them last.
    """
    if not isinstance(tail, (bytes, bytearray)):
        return None
    start = tail.rfind(_TIMINGS_KEY)
    if start < 0:
        return None
    # "timings" then ":" then "{", with or without whitespace between (llama-server writes
    # compact JSON today; a formatting change must not silently stop the measurement).
    match = _AFTER_KEY_RE.match(tail, start + len(_TIMINGS_KEY))
    if match is None:
        return None
    opening = match.end() - 1
    closing = tail.find(b"}", opening)   # the timings object is flat: its first "}" ends it
    if closing < 0:
        return None
    try:
        timings = json.loads(bytes(tail[opening:closing + 1]))
    except ValueError:
        return None
    if not isinstance(timings, dict):
        return None
    count, rate = timings.get("predicted_n"), timings.get("predicted_per_second")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        return None
    if not isinstance(rate, (int, float)) or isinstance(rate, bool):
        return None
    rate = float(rate)
    if not math.isfinite(rate) or not 0.0 < rate <= MAX_TOKENS_PER_SECOND:
        return None
    return count, rate


def _utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _valid_rate(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and 0.0 < value <= MAX_TOKENS_PER_SECOND)


class SpeedMeter:
    """
    The last WINDOW decode rates per model file, their median, and the file that keeps them.

    Thread-safe: ``observe`` runs on the event loop, ``save`` in a worker thread. ``path`` is
    resolved when first needed (default <base>/config/coding_speed.json, looked up through
    engine.config at that moment so a test's redirected base directory is honoured).
    """

    def __init__(self, path: Optional[str] = None):
        self._path = path
        self._lock = threading.Lock()
        self._save_lock = threading.Lock()
        self._models: Dict[str, Dict[str, Any]] = {}
        self._loaded = False

    # -- storage -------------------------------------------------------------------------

    @property
    def path(self) -> str:
        if self._path is None:
            self._path = os.path.join(_config.get_base_dir(), "config", SPEED_FILE)
        return self._path

    def load(self) -> None:
        """Read the saved measurements now (the coordinator does it at startup, off the event loop)."""
        with self._lock:
            self._ensure_loaded()

    def _ensure_loaded(self) -> None:
        """Read the saved measurements once. A missing or damaged file means "none yet"."""
        if self._loaded:
            return
        restored: Dict[str, Dict[str, Any]] = {}
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            models = data.get("models") if isinstance(data, dict) else None
            for file, entry in (models.items() if isinstance(models, dict) else []):
                if not isinstance(file, str) or not file or len(file) > 255 or not isinstance(entry, dict):
                    continue
                saved = entry.get("samples")
                samples = [float(v) for v in saved if _valid_rate(v)][-WINDOW:] if isinstance(saved, list) else []
                if not samples and _valid_rate(entry.get("tokens_per_second")):
                    samples = [float(entry["tokens_per_second"])]
                measured_at = entry.get("measured_at")
                if not samples or not isinstance(measured_at, str) or len(measured_at) > 40:
                    continue
                restored[file] = {"samples": samples, "measured_at": measured_at}
        except (OSError, ValueError, TypeError, AttributeError):
            restored = {}
        self._models = dict(list(restored.items())[-MAX_MODELS:])
        self._loaded = True

    def save(self) -> None:
        """Write every model's samples and median to the speed file, atomically."""
        with self._lock:
            self._ensure_loaded()
            payload = {
                "_comment": "Decode speed of the coding model on real answers (engine/coding_speed.py). Safe to delete.",
                "models": {
                    file: {
                        "tokens_per_second": round(statistics.median(entry["samples"]), 2),
                        "measured_at": entry["measured_at"],
                        "samples": [round(v, 3) for v in entry["samples"]],
                    }
                    for file, entry in self._models.items()
                },
            }
        target = self.path
        with self._save_lock:
            directory = os.path.dirname(target)
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".coding_speed.", suffix=".tmp", dir=directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, indent=2)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, target)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

    # -- measuring -----------------------------------------------------------------------

    def record(self, file: str, predicted_n: int, tokens_per_second: float, at: Optional[str] = None) -> bool:
        """Add one answer's rate. True when it counted (and the file should be saved)."""
        if not isinstance(file, str) or not file or predicted_n < MIN_PREDICTED_TOKENS:
            return False
        if not _valid_rate(tokens_per_second):
            return False
        with self._lock:
            self._ensure_loaded()
            entry = self._models.pop(file, None) or {"samples": []}
            entry["samples"] = (entry["samples"] + [float(tokens_per_second)])[-WINDOW:]
            entry["measured_at"] = at or _utc_now_iso()
            self._models[file] = entry           # most recently measured last
            while len(self._models) > MAX_MODELS:
                self._models.pop(next(iter(self._models)))
        return True

    def observe(self, file: str, tail: bytes) -> bool:
        """Record the rate in a finished answer's last bytes, if it has one that counts."""
        timings = parse_timings(tail)
        if timings is None:
            return False
        return self.record(file, timings[0], timings[1])

    def speed(self, file: Optional[str]) -> Tuple[Optional[float], Optional[str]]:
        """(median tokens per second, ISO-8601 time of the newest sample) for ``file``, or (None, None)."""
        if not file:
            return (None, None)
        with self._lock:
            self._ensure_loaded()
            entry = self._models.get(file)
            if not entry or not entry["samples"]:
                return (None, None)
            return (round(statistics.median(entry["samples"]), 1), entry["measured_at"])

    def samples(self, file: str) -> List[float]:
        with self._lock:
            self._ensure_loaded()
            return list(self._models.get(file, {}).get("samples", []))


# The coordinator's meter. Tests replace it (monkeypatch engine.coding_speed.meter).
meter = SpeedMeter()
