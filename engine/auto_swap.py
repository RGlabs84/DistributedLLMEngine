"""
Automatic model switching for the GPU coding pool.

WHAT IT DOES
    The coding pool serves one model at a time, picked from a ladder of tiers (smallest
    first), each with the pooled GPU memory it needs. When volunteers' GPUs join and the pool
    *stays* big enough for a bigger enabled tier, this controller moves the coordinator up to
    that tier's model. When the pool shrinks below what the served model needs, it steps
    down to the biggest enabled tier that still fits.

WHY IT IS THIS CAUTIOUS
    DLNE is live with public traffic, and every switch stops the engine: coding users wait
    while the new weights load. So an up-switch needs all of these at once:

    * the pool has held the target continuously for ``AUTO_SWAP_UP_STABLE_SECONDS``
      (10 min by default), so a volunteer who joins for a few minutes changes nothing;
    * the target's model file is on disk and matches its pinned size and SHA-256. The
      download starts in the background as soon as the tier fits, so the switch itself is
      quick, and a model that is not ready is never switched to;
    * no other switch or engine restart is in progress;
    * ``AUTO_SWAP_COOLDOWN_SECONDS`` (30 min) have passed since the last switch, of any kind;
    * the coding queue is idle. If it has been busy for ``QUEUE_WAIT_SECONDS`` the switch
      drains it instead: new requests wait, in-flight ones get ``DRAIN_SECONDS`` to finish,
      and if they don't, the switch is called off and nothing is interrupted.

    "The pool" means the *near* GPUs only: the host's own and volunteers within
    ``AUTO_SWAP_MAX_RTT_MS`` (15 ms, a LAN), measured by the coordinator (see "Near and far
    GPUs" below). A volunteer across the internet never unlocks a bigger tier.

    And the switch itself is bounded: if the new engine is not answering within
    ``AUTO_SWAP_LOAD_TIMEOUT_SECONDS`` (3 min), the previous model is put back.

    A down-switch only needs the pool to have been short for ``AUTO_SWAP_DOWN_STABLE_SECONDS``
    (2 min). It ignores the cooldown, because the served model cannot run properly any more.
    After a manual switch (``POST /v1/cluster/swap``, or anything else that changes the served
    model behind the controller's back) it holds off in both directions for the cooldown.

    It only ever moves between *enabled* rungs of the ladder, and only while the served
    model is one of them: an operator who picked some other model keeps it.

THE OFF SWITCH
    ``AUTO_SWAP=false`` in config.txt. It is read on every tick (config.txt is re-read when
    it changes), so it takes effect within ``TICK_SECONDS`` and needs no restart. Off means
    off: no switches, no downloads (a download in progress is cancelled).

HOW IT IS WIRED
    Everything that touches the outside world is passed in (see ``AutoSwapController``), so
    the decisions can be tested with fake clocks, fake tiers, a fake pool and a fake switcher,
    and the module does not import the coordinator or the ladder. ``engine/coordinator.py``
    builds the one real instance and runs :meth:`AutoSwapController.run` as a background task.

A TIER is a dict from the ladder (``engine/coding_ladder.load_coding_tiers``)::

    {"id", "name", "file", "url", "size", "sha256", "min_vram_gb", "context", "enabled",
     "blocked", "files": [{"file", "url", "sha256", "size"}, ...]}

``files`` lists every part (one for a single-file model). A split model counts as ready only
when every part is on disk and verified. It is downloaded like any other tier, part by part,
resumably and with each part's size and SHA-256 checked (``engine.downloader.download_verified``,
reached through the ``download`` callback the coordinator passes in); a tier is only left alone
when it *cannot* be downloaded, which is when any one part is not pinned (a bare file name, a
size, a SHA-256 and an https URL).
"""

import asyncio
import math
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

from engine.inference_manager import canonical_model_tag

# How often the controller looks at the pool. Every timing below is measured in ticks of
# this size, so a "600 s" wait really means "the first tick at least 600 s later".
TICK_SECONDS = 30.0

# Defaults for the config.txt settings (engine/config.py parses and range-checks them).
DEFAULT_UP_STABLE_SECONDS = 600.0
DEFAULT_DOWN_STABLE_SECONDS = 120.0
DEFAULT_COOLDOWN_SECONDS = 1800.0

# How long an up-switch waits for an idle coding queue before it drains the queue itself.
QUEUE_WAIT_SECONDS = 900.0

# How long a drain holds new coding requests while in-flight ones finish. An up-switch that
# cannot drain in this time is called off (nobody's answer is cut off for an upgrade); a
# down-switch goes ahead regardless, like a manual switch does.
DRAIN_SECONDS = 60.0

# A tier whose switch failed (the engine did not come up, so the previous model was restored)
# is not tried again for this long, doubling with each further failure up to the cap.
# Retrying every cooldown would mean a failed load, and its downtime, every half hour; a tier
# that keeps failing (a volunteer's link too slow to receive its share of the weights, say)
# soon stops costing anything at all.
FAILED_TIER_HOLD_SECONDS = 6 * 3600.0
FAILED_TIER_HOLD_MAX_SECONDS = 48 * 3600.0

# A failed or rejected download is retried after this long.
DOWNLOAD_RETRY_SECONDS = 3600.0

STATES = ("off", "steady", "waiting", "downloading", "swapping", "cooldown")

# ======================================================================================
# Near and far GPUs
# ======================================================================================
#
# Every tier above the host's own 16 GB needs a volunteer's GPU, reached over RPC. Across the
# internet that is a bad trade: the volunteer's share of the weights has to be uploaded
# before the model answers (minutes to hours on a home uplink), and every token then crosses
# the link, so the "bigger" model can easily decode slower than today's local one. So only
# GPUs that are *near* count toward the coding pool: the host's own, and volunteers whose
# measured round trip is at most AUTO_SWAP_MAX_RTT_MS (15 ms, a LAN). A volunteer nobody has
# measured is far.

DEFAULT_MAX_RTT_MS = 15.0

# How often each volunteer's RTT is measured, and how long a good measurement stays valid
# when later probes fail (one lost probe must not make a LAN machine "far" and trigger a
# switch).
LINK_PROBE_INTERVAL_SECONDS = 300.0
LINK_MEASUREMENT_MAX_AGE_SECONDS = 900.0
LINK_PROBE_TICK_SECONDS = 60.0


def split_pool_by_distance(workers: List[Dict[str, Any]],
                           is_near: Callable[[Dict[str, Any]], bool]) -> Dict[str, float]:
    """
    ``{"vram_gb": near, "far_vram_gb": far}``: the GPU memory of ``workers`` split by
    ``is_near``. Each worker is ``{"vram_gb", ...}`` plus whatever ``is_near`` reads. A worker
    ``is_near`` cannot answer for counts as far.
    """
    near = far = 0.0
    for worker in workers:
        try:
            vram = float(worker.get("vram_gb") or 0.0)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(vram) or vram <= 0:
            continue
        try:
            close = bool(is_near(worker))
        except Exception:  # noqa: BLE001 - unknown is far
            close = False
        if close:
            near += vram
        else:
            far += vram
    return {"vram_gb": near, "far_vram_gb": far}


def rtt_is_near(rtt_ms: Any, measured_age_seconds: Any, max_rtt_ms: float,
                max_age_seconds: float = LINK_MEASUREMENT_MAX_AGE_SECONDS) -> bool:
    """A measured round trip that is fresh enough and at most ``max_rtt_ms``. Anything else is far."""
    try:
        rtt, age = float(rtt_ms), float(measured_age_seconds)
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(rtt) and math.isfinite(age)) or rtt < 0:
        return False
    return age <= max_age_seconds and rtt <= max_rtt_ms


class LinkProbe:
    """
    Measures how far each volunteer's GPU is, from the coordinator, every few minutes.

    ``measure(endpoint) -> rtt_ms or None`` is blocking (it is run on a thread); ``record(
    endpoint, rtt_ms)`` is told every successful measurement. A failed measurement records
    nothing, so the last good one stands until it ages out.
    """

    def __init__(self, *, measure: Callable[[str], Optional[float]],
                 record: Callable[[str, float], None],
                 clock: Callable[[], float] = time.monotonic,
                 interval: float = LINK_PROBE_INTERVAL_SECONDS) -> None:
        self._measure = measure
        self._record = record
        self._clock = clock
        self._interval = interval
        self._attempted: Dict[str, float] = {}

    def due(self, endpoints: List[str]) -> List[str]:
        now = self._clock()
        return [e for e in endpoints if now - self._attempted.get(e, float("-inf")) >= self._interval]

    async def refresh(self, endpoints: List[str]) -> Dict[str, Optional[float]]:
        """Measure every endpoint that is due, all at once; returns what each measured."""
        todo = self.due(endpoints)
        now = self._clock()
        for endpoint in todo:
            self._attempted[endpoint] = now
        for gone in set(self._attempted) - set(endpoints):
            del self._attempted[gone]
        results = await asyncio.gather(*(asyncio.to_thread(self._measure, e) for e in todo),
                                       return_exceptions=True)
        measured: Dict[str, Optional[float]] = {}
        for endpoint, result in zip(todo, results):
            ok = (isinstance(result, (int, float)) and not isinstance(result, bool)
                  and math.isfinite(result) and result >= 0)
            measured[endpoint] = float(result) if ok else None
            if ok:
                self._record(endpoint, float(result))
        return measured

    async def run(self, endpoints: Callable[[], List[str]],
                  warn: Callable[[str], None] = print,
                  tick: float = LINK_PROBE_TICK_SECONDS) -> None:
        while True:
            try:
                await self.refresh(list(endpoints()))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the probe must outlive any one failure
                warn(f"Auto-swap: measuring the volunteers' links failed ({type(exc).__name__}: {exc}).")
            await asyncio.sleep(tick)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass
class SwapResult:
    """
    What the switcher reports back.

    ``status`` is one of:
        ``"swapped"``     the new model is up and answering;
        ``"busy"``        nothing was touched: the queue did not drain, or another
                          reconfiguration was already running;
        ``"failed"``      the new model did not come up and the previous one was restored;
        ``"refused"``     nothing was touched: a pre-flight check failed (file gone, pool
                          too small for the engine's own admission rule);
        ``"superseded"``  a manual switch took over part-way through.
    """
    status: str
    detail: str = ""


@dataclass
class Decision:
    """One tick's verdict. ``reason`` is a stable code; a change of it is what gets logged."""
    state: str
    target: Optional[str]
    reason: str
    detail: str


@dataclass
class _Settings:
    enabled: bool
    up_stable: float
    down_stable: float
    cooldown: float


# ======================================================================================
# Tier helpers (shared with the coordinator's readiness and download code)
# ======================================================================================

def tier_id(tier: Dict[str, Any]) -> str:
    return str(tier.get("id") or "")


def tier_label(tier: Optional[Dict[str, Any]]) -> str:
    """The name people see, falling back to the id."""
    if not tier:
        return "nothing"
    return str(tier.get("name") or tier.get("id") or tier.get("file") or "an unnamed model")


def tier_files(tier: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    The files a tier needs, each as ``{"file", "url", "size", "sha256"}``.

    A tier lists them in ``files`` when the model is split into several GGUFs; otherwise its
    own ``file``/``url``/``size``/``sha256`` describe the single file. A malformed ``files``
    entry yields an empty list, which nothing treats as ready.
    """
    files = tier.get("files")
    if isinstance(files, list) and files:
        out = []
        for entry in files:
            if not isinstance(entry, dict):
                return []
            out.append({
                "file": entry.get("file") or entry.get("path") or "",
                "url": entry.get("url") or entry.get("hf_url") or "",
                "size": entry.get("size"),
                "sha256": str(entry.get("sha256") or "").lower(),
            })
        return out
    return [{
        "file": tier.get("file") or "",
        "url": tier.get("url") or "",
        "size": tier.get("size"),
        "sha256": str(tier.get("sha256") or "").lower(),
    }]


def file_is_pinned(entry: Dict[str, Any]) -> bool:
    """
    True when a file entry can be verified: a bare file name (no folders), a positive byte
    size and a SHA-256. Anything less is never treated as ready, because "the file exists"
    is how a half-downloaded or wrong model ends up being served.
    """
    name = entry.get("file")
    if not isinstance(name, str) or not name or canonical_model_tag(name) != name:
        return False
    size = entry.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        return False
    return isinstance(entry.get("sha256"), str) and bool(_SHA256_RE.match(entry["sha256"]))


def _part_is_downloadable(entry: Dict[str, Any]) -> bool:
    url = entry.get("url")
    return file_is_pinned(entry) and isinstance(url, str) and url.startswith("https://")


def downloadable(tier: Dict[str, Any]) -> bool:
    """
    True when DLNE can fetch the tier itself: every file of it (one, or each part of a split
    model) is pinned and has an https URL, and no two parts share a name.
    """
    files = tier_files(tier)
    if not files or not all(_part_is_downloadable(entry) for entry in files):
        return False
    return len({entry["file"].lower() for entry in files}) == len(files)


def tier_size(tier: Dict[str, Any]) -> int:
    """Bytes of the whole tier: the sum over its files (0 when a size is missing)."""
    total = 0
    for entry in tier_files(tier):
        size = entry.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            return 0
        total += size
    return total


def why_not_downloadable(tier: Dict[str, Any]) -> str:
    """Words for the status line when :func:`downloadable` is False."""
    files = tier_files(tier)
    if len(files) > 1:
        bad = sum(1 for entry in files if not _part_is_downloadable(entry))
        if bad:
            return (f"{bad} of its {len(files)} files have no pinned download (https URL, size and SHA-256)")
        return "two of its files share a name"
    return "no pinned download (https URL, size and SHA-256)"


def fits_by_vram(tier: Dict[str, Any], pool: Dict[str, Any]) -> bool:
    """
    The default fit test: the pooled GPU memory covers the tier's ``min_vram_gb``.

    It is a parameter of the controller rather than a fixed rule because some tiers will
    need more than VRAM (a model that keeps a large table in host RAM, for example).
    """
    try:
        need = float(tier.get("min_vram_gb"))
        have = float(pool.get("vram_gb") or 0.0)
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(need) and math.isfinite(have)):
        return False
    return have >= need


def _bare(tag: Any) -> str:
    """A model name as the coordinator stores it (``canonical_model_tag``), or ``""``."""
    return canonical_model_tag(tag) if isinstance(tag, str) else ""


def _gb(value: float) -> str:
    text = f"{value:.1f}"
    return text[:-2] if text.endswith(".0") else text


def _duration(seconds: float) -> str:
    """'6 min' or '6 h', rounded up; never '0 min'."""
    if seconds <= 60:
        return "under a minute"
    minutes = int(math.ceil(seconds / 60.0))
    if minutes < 120:
        return f"{minutes} min"
    return f"{_gb(math.ceil(seconds / 360.0) / 10.0)} h"


def _eta(seconds: float) -> str:
    """'in 6 min' style, for the status line websites show."""
    return f"in {_duration(seconds)}"


# ======================================================================================
# The controller
# ======================================================================================

class AutoSwapController:
    """
    Decides when the coding pool moves to a bigger or smaller tier, and does it.

    Args (all keyword):
        tiers_provider: ``() -> List[tier]``, smallest first.
        pool_provider: ``() -> {"vram_gb": float, "sysram_gb": float}``.
        current_model: ``() -> str``, the model the coordinator serves.
        swap: ``async (tier, *, drain_seconds, abort_if_busy) -> SwapResult``.
        tier_ready: ``async (tier) -> bool``, every file on disk and verified.
        download: ``async (tier) -> bool``, fetch and verify the tier's file.
        settings: ``() -> dict`` with ``auto_swap`` and the ``auto_swap_*_seconds`` keys
            (``engine.config.get_worker_limits``).
        swap_in_progress: ``() -> bool``, any switch or engine restart under way.
        queue_busy: ``() -> bool``, any coding request in flight.
        fits: ``(tier, pool) -> bool``; defaults to :func:`fits_by_vram`.
        clock: monotonic seconds.
        log / warn: one-line loggers.
    """

    def __init__(
        self,
        *,
        tiers_provider: Callable[[], List[Dict[str, Any]]],
        pool_provider: Callable[[], Dict[str, Any]],
        current_model: Callable[[], str],
        swap: Callable[..., Awaitable[SwapResult]],
        tier_ready: Callable[[Dict[str, Any]], Awaitable[bool]],
        download: Callable[[Dict[str, Any]], Awaitable[bool]],
        settings: Callable[[], Dict[str, Any]],
        swap_in_progress: Callable[[], bool],
        queue_busy: Callable[[], bool],
        fits: Callable[[Dict[str, Any], Dict[str, Any]], bool] = fits_by_vram,
        clock: Callable[[], float] = time.monotonic,
        log: Callable[[str], None] = print,
        warn: Optional[Callable[[str], None]] = None,
        interval: float = TICK_SECONDS,
    ) -> None:
        self._tiers_provider = tiers_provider
        self._pool_provider = pool_provider
        self._current_model = current_model
        self._swap = swap
        self._tier_ready = tier_ready
        self._download = download
        self._settings = settings
        self._swap_in_progress = swap_in_progress
        self._queue_busy = queue_busy
        self._fits = fits
        self._clock = clock
        self._log = log
        self._warn = warn or log
        self._interval = interval

        # The enabled rungs as of the last tick. serve_as() reads this on the request path,
        # so it must never call the provider (which may read files).
        self._enabled_tiers: List[Dict[str, Any]] = []
        # tier id -> when the pool started fitting it, in the current unbroken run.
        self._fit_since: Dict[str, float] = {}
        # When the pool stopped fitting the served model (None while it fits).
        self._unfit_since: Optional[float] = None
        # When an up-switch first found the queue busy (None when not waiting on it).
        self._busy_since: Optional[float] = None
        # The last switch of any kind, and the end of a manual hold-off.
        self._last_swap_at: Optional[float] = None
        self._hold_until: float = float("-inf")
        # The model the controller last saw being served. A difference it did not cause is
        # somebody else's switch, and is respected like a manual one.
        self._expected_model: Optional[str] = None
        # tier id -> clock time before which it is not tried again, and how often it failed.
        self._failed_until: Dict[str, float] = {}
        self._failures: Dict[str, int] = {}
        self._download_retry_at: Dict[str, float] = {}
        self._download_task: Optional[asyncio.Task] = None
        self._download_tier: Optional[Dict[str, Any]] = None
        # (tier id, bytes done, bytes in all) of the running download, as its downloader reports it.
        self._download_progress: Optional[tuple] = None
        self._swapping = False
        self._decision = Decision("steady", None, "starting", "Automatic model switching is starting.")
        self._logged_key: Optional[tuple] = None

    # ----------------------------------------------------------------------------------
    # Public surface
    # ----------------------------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        """The ``auto_swap`` block of ``GET /v1/cluster/status``."""
        enabled = self._read_settings().enabled
        decision = self._decision
        if self._swapping:
            return {"enabled": enabled, "state": "swapping", "target": decision.target, "detail": decision.detail}
        if not enabled:
            return {"enabled": False, "state": "off", "target": None,
                    "detail": "Automatic model switching is off."}
        if decision.state == "off":
            # Switched back on since the last tick.
            return {"enabled": True, "state": "steady", "target": None,
                    "detail": "Automatic model switching is starting."}
        return {"enabled": True, "state": decision.state, "target": decision.target, "detail": decision.detail}

    def note_manual_swap(self, model: Optional[str] = None) -> None:
        """
        Called after a manual switch. Holds off for the cooldown in both directions, so the
        controller never undoes what an operator (or a client) just chose.
        """
        settings = self._read_settings()
        self._hold(self._clock(), settings.cooldown)
        if model:
            self._expected_model = _bare(model)
        if settings.enabled:
            self._log(f"Auto-swap: a manual switch to {_bare(model) or 'another model'}; "
                      f"holding off for {_duration(settings.cooldown)}.")

    def serve_as(self, requested: Any, current: Any) -> Any:
        """
        The model a coding request should be served by.

        A client that names another enabled rung of the ladder gets the rung being served.
        Clients pick the model name from a status they fetched a few seconds earlier, so for
        a moment after every switch some of them still name the old one. Starting that model
        would undo the switch: the chat path swaps an idle cluster to whatever a request
        names. Everything else is returned unchanged.

        Reads only what the last tick cached: this runs on every chat and completion.
        """
        if not requested or not current or not self._read_settings().enabled:
            return requested
        rungs = {_bare(t.get("file")) for t in self._enabled_tiers}
        wanted, served = _bare(requested), _bare(current)
        if wanted and served and wanted != served and wanted in rungs and served in rungs:
            return served
        return requested

    async def run(self) -> None:
        """The background loop. One failed tick is logged and the next one tries again."""
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the controller must outlive any one failure
                self._warn(f"Auto-swap: an evaluation failed ({type(exc).__name__}: {exc}); "
                           f"trying again in {self._interval:.0f} s.")
            await asyncio.sleep(self._interval)

    def shutdown(self) -> None:
        """Cancels a download in progress (coordinator shutdown)."""
        self._stop_download(None)

    # ----------------------------------------------------------------------------------
    # One evaluation
    # ----------------------------------------------------------------------------------

    async def tick(self) -> Decision:
        now = self._clock()
        settings = self._read_settings()

        if not settings.enabled:
            self._stop_download("automatic switching was turned off")
            self._fit_since.clear()
            self._unfit_since = None
            self._busy_since = None
            self._expected_model = None
            return self._decide("off", None, "off", "Automatic model switching is off (AUTO_SWAP=false).")

        self._collect_download(now)

        try:
            tiers = [t for t in (self._tiers_provider() or []) if isinstance(t, dict)]
        except Exception as exc:  # noqa: BLE001 - a broken ladder must not stop the loop
            self._enabled_tiers = []   # and serve_as() stops relying on the last good copy
            return self._decide("steady", None, "ladder-error",
                                f"The coding model ladder could not be read ({type(exc).__name__}: {exc}), "
                                "so nothing switches.")
        # Only a literal True enables a rung (a typo in the ladder leaves it off), and a rung
        # marked blocked never counts, whatever its switch says.
        enabled = [t for t in tiers if t.get("enabled") is True and not t.get("blocked")
                   and tier_id(t) and _bare(t.get("file"))]
        self._enabled_tiers = enabled

        # vram_gb is the *near* pool (see split_pool_by_distance): GPUs too far away to be
        # worth a model's weights never count, in either direction.
        pool = dict(self._pool_provider() or {})
        vram = float(pool.get("vram_gb") or 0.0)
        far = float(pool.get("far_vram_gb") or 0.0)
        current = _bare(self._current_model())

        self._watch_for_outside_switch(current, now, settings)
        self._track_fits(enabled, pool, now)

        index = next((i for i, t in enumerate(enabled) if _bare(t.get("file")) == current), None)
        if index is None:
            self._unfit_since = None
            self._busy_since = None
            return self._decide("steady", None, "off-ladder",
                                f"Serving {current or 'no model'}, which is not an enabled tier of the coding "
                                "ladder, so nothing switches automatically.")

        if self._swap_in_progress():
            return self._decide("swapping", None, "busy-elsewhere",
                                "A model switch or engine restart is in progress.")

        if vram + far <= 0:
            # No GPU at all is "waiting for nodes" (the host's own worker restarting, say),
            # not a reason to switch: nothing could run anyway.
            self._unfit_since = None
            self._busy_since = None
            return self._decide("steady", None, "no-gpus",
                                "No GPUs are connected right now; waiting for the pool to come back.")

        served = enabled[index]
        hold_left = max(0.0, self._hold_until - now)
        cooldown_left = 0.0
        if self._last_swap_at is not None:
            cooldown_left = max(0.0, self._last_swap_at + settings.cooldown - now)

        if not self._fits_safely(served, pool):
            return await self._consider_down(served, enabled, pool, vram, now, settings, hold_left)
        self._unfit_since = None
        return await self._consider_up(served, enabled, index, pool, vram, now, settings,
                                       hold_left, cooldown_left)

    # ----------------------------------------------------------------------------------
    # Down
    # ----------------------------------------------------------------------------------

    async def _consider_down(self, served, enabled, pool, vram, now, settings, hold_left) -> Decision:
        if self._unfit_since is None:
            self._unfit_since = now
        short_for = now - self._unfit_since

        candidates = [t for t in enabled if t is not served and tier_id(t) in self._fit_since
                      and self._failed_until.get(tier_id(t), float("-inf")) <= now]
        fallback = None
        for tier in reversed(candidates):  # biggest first
            if await self._tier_ready(tier):
                fallback = tier
                break
        if candidates:
            # Fetch the biggest tier that fits if it is not on disk, so there is somewhere (or
            # somewhere better) to go if the pool stays this small.
            await self._prefetch(candidates[-1], now)

        pool_text = f"The pool ({_gb(vram)} GB of GPU memory) can no longer hold {tier_label(served)}"
        if short_for < settings.down_stable:
            if fallback is not None:
                detail = (f"{pool_text}; switching to {tier_label(fallback)} "
                          f"{_eta(settings.down_stable - short_for)} unless it recovers.")
            else:
                detail = f"{pool_text}; no smaller model that fits is ready to switch to."
            return self._decide("waiting", tier_id(fallback) if fallback else None, "pool-short", detail)

        if hold_left > 0:
            return self._decide("cooldown", tier_id(fallback) if fallback else None, "manual-hold",
                                f"{pool_text}, but a manual switch was made recently; "
                                f"holding off {_eta(hold_left)}.")

        if fallback is None:
            return self._decide("waiting", None, "no-fallback",
                                f"{pool_text} and no smaller enabled model that fits is on disk; staying put.")

        return await self._switch(fallback, "down", now, settings)

    # ----------------------------------------------------------------------------------
    # Up
    # ----------------------------------------------------------------------------------

    async def _consider_up(self, served, enabled, index, pool, vram, now, settings,
                           hold_left, cooldown_left) -> Decision:
        # The target is the biggest enabled tier the pool holds right now, skipping tiers that
        # could never be switched to: one that failed recently, and one that is not on disk
        # and cannot be downloaded (an entry without a pinned file, or a split model with a
        # part like that).
        target = None
        passed_over = []
        for tier in reversed([t for t in enabled[index + 1:] if tier_id(t) in self._fit_since]):
            if self._failed_until.get(tier_id(tier), float("-inf")) > now:
                passed_over.append(f"{tier_label(tier)} failed recently")
                continue
            if downloadable(tier) or await self._tier_ready(tier):
                target = tier
                break
            passed_over.append(f"{tier_label(tier)} is not on disk and {why_not_downloadable(tier)}")

        if target is None:
            self._busy_since = None
            if passed_over:
                return self._decide("steady", None, "top-blocked",
                                    f"Serving {tier_label(served)}; the pool ({_gb(vram)} GB) could hold more, "
                                    f"but {passed_over[0]}.")
            far = float(pool.get("far_vram_gb") or 0.0)
            far_note = (f" ({_gb(far)} GB more is on GPUs too far away to share a model with)"
                        if far > 0 else "")
            return self._decide("steady", None, "top",
                                f"Serving {tier_label(served)}; the nearby GPU pool ({_gb(vram)} GB) has no "
                                f"bigger enabled model to unlock{far_note}.")

        # Fetch first, as soon as the tier fits, so that the switch itself is quick.
        if not await self._tier_ready(target):
            self._busy_since = None
            return self._fetch(target, served, now)

        settle_left = settings.up_stable - (now - self._fit_since[tier_id(target)])
        if settle_left > 0:
            self._busy_since = None
            return self._decide("waiting", tier_id(target), "settling",
                                f"Switching to {tier_label(target)} {_eta(max(settle_left, hold_left, cooldown_left))} "
                                f"if the pool keeps {_gb(vram)} GB.")

        if hold_left > 0:
            self._busy_since = None
            return self._decide("cooldown", tier_id(target), "manual-hold",
                                f"Switching to {tier_label(target)} {_eta(max(hold_left, cooldown_left))}; "
                                "holding off after a manual model switch.")

        if cooldown_left > 0:
            self._busy_since = None
            return self._decide("cooldown", tier_id(target), "cooldown",
                                f"Switching to {tier_label(target)} {_eta(cooldown_left)} "
                                "(cooldown after the last switch).")

        if self._queue_busy():
            if self._busy_since is None:
                self._busy_since = now
            if now - self._busy_since < QUEUE_WAIT_SECONDS:
                return self._decide("waiting", tier_id(target), "queue-busy",
                                    f"Switching to {tier_label(target)} as soon as the coding queue is quiet.")
            # Busy for the whole bounded wait: drain instead. If in-flight requests do not
            # finish within DRAIN_SECONDS the switch is called off and nothing is cut short.

        return await self._switch(target, "up", now, settings)

    # ----------------------------------------------------------------------------------
    # Switching
    # ----------------------------------------------------------------------------------

    async def _switch(self, tier: Dict[str, Any], direction: str, now: float, settings: _Settings) -> Decision:
        label = tier_label(tier)
        why = " because the pool shrank" if direction == "down" else ""
        self._swapping = True
        self._decide("swapping", tier_id(tier), f"switch-{direction}", f"Switching to {label} now{why}.")
        try:
            result = await self._swap(tier, drain_seconds=DRAIN_SECONDS, abort_if_busy=(direction == "up"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reported like any other failed switch
            result = SwapResult("failed", f"{type(exc).__name__}: {exc}")
        finally:
            self._swapping = False

        if not isinstance(result, SwapResult):
            result = SwapResult("failed", "the switcher returned no result")

        now = self._clock()
        current = _bare(self._current_model())
        self._expected_model = current

        if result.status == "swapped":
            self._last_swap_at = now
            self._busy_since = None
            self._unfit_since = None
            self._failures.pop(tier_id(tier), None)
            return self._decide("steady", None, "switched", f"Switched to {label}{why}.")

        if result.status == "busy":
            # Start the bounded wait again, so a queue that would not drain is not held up
            # again on the very next tick.
            self._busy_since = now
            return self._decide("waiting", tier_id(tier), "queue-busy",
                                f"Switching to {label} as soon as the coding queue is quiet "
                                f"({result.detail or 'it was busy'}).")

        if result.status == "superseded":
            self._hold(now, settings.cooldown)
            return self._decide("cooldown", None, "manual-hold",
                                f"A manual model switch took over; holding off {_eta(settings.cooldown)}.")

        # failed or refused
        failures = self._failures.get(tier_id(tier), 0) + 1
        self._failures[tier_id(tier)] = failures
        hold = min(FAILED_TIER_HOLD_SECONDS * 2 ** (failures - 1), FAILED_TIER_HOLD_MAX_SECONDS)
        self._failed_until[tier_id(tier)] = now + hold
        if result.status == "failed":
            # The engine was restarted twice (out and back): that is a disruption, so the
            # cooldown applies to whatever comes next.
            self._last_swap_at = now
        self._warn(f"Auto-swap: switching to {label} did not work ({result.status}: {result.detail}); "
                   f"serving {current or 'no model'}. {label} is not tried again {_eta(hold)}.")
        return self._decide("waiting", tier_id(tier), "switch-failed",
                            f"Switching to {label} did not work ({result.detail or result.status}); "
                            f"not trying it again {_eta(hold)}.")

    # ----------------------------------------------------------------------------------
    # Downloads
    # ----------------------------------------------------------------------------------

    def _fetch(self, target: Dict[str, Any], served: Dict[str, Any], now: float) -> Decision:
        """The decision for a target that is not on disk, starting its download if possible."""
        label = tier_label(target)
        files = tier_files(target)
        if not downloadable(target):
            # Only a tier with a file that is not pinned (a split model's parts included) ends
            # up here: nothing else keeps a tier from being fetched, however many files it has.
            return self._decide("waiting", tier_id(target), "no-download",
                                f"{label} is not on disk and {why_not_downloadable(target)}; "
                                f"staying on {tier_label(served)}.")

        running = self._download_task is not None and not self._download_task.done()
        if running and tier_id(self._download_tier) != tier_id(target):
            return self._decide("downloading", tier_id(target), "download-queued",
                                f"Finishing the download of {tier_label(self._download_tier)} before "
                                f"fetching {label}.")
        retry_at = self._download_retry_at.get(tier_id(target), float("-inf"))
        if not running and retry_at > now:
            return self._decide("waiting", tier_id(target), "download-failed",
                                f"Downloading {label} did not work; trying again {_eta(retry_at - now)}.")
        if not running:
            self._start_download(target)
        size = tier_size(target)
        parts = f", {len(files)} files" if len(files) > 1 else ""
        progress = self._download_progress
        if progress is not None and progress[0] == tier_id(target) and progress[2] > 0:
            how_far = f"{_gb(progress[1] / 1e9)} of {_gb(progress[2] / 1e9)} GB{parts}"
        else:
            how_far = f"{_gb(size / 1e9)} GB{parts}"
        return self._decide("downloading", tier_id(target), "downloading",
                            f"Downloading {label} ({how_far}) so it is ready to switch to.")

    async def _prefetch(self, tier: Dict[str, Any], now: float) -> None:
        """Start a background download of ``tier`` if it needs one and nothing else is downloading."""
        if not downloadable(tier):
            return
        if self._download_task is not None and not self._download_task.done():
            return
        if self._download_retry_at.get(tier_id(tier), float("-inf")) > now:
            return
        if await self._tier_ready(tier):
            return
        self._start_download(tier)

    def note_download_progress(self, tier: str, done: int, total: int) -> None:
        """
        How far the running download has got, for the status line. Called by whatever does the
        downloading (from its thread), as often as it likes: it only remembers the last figures.
        """
        self._download_progress = (str(tier), int(done), int(total))

    def _start_download(self, tier: Dict[str, Any]) -> None:
        size = tier_size(tier)
        parts = len(tier_files(tier))
        self._log(f"Auto-swap: downloading {tier_label(tier)} ({_gb(size / 1e9)} GB"
                  f"{f', {parts} files' if parts > 1 else ''}) in the background.")
        self._download_progress = None
        self._download_tier = tier
        self._download_task = asyncio.get_running_loop().create_task(
            self._download(tier), name=f"auto-swap-download:{tier_id(tier)}"
        )

    def _collect_download(self, now: float) -> None:
        """Book the result of a finished download (once)."""
        task, tier = self._download_task, self._download_tier
        if task is None or not task.done():
            return
        self._download_task = None
        self._download_tier = None
        self._download_progress = None
        try:
            ok = bool(task.result())
            problem = "" if ok else "it did not complete or did not verify"
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001 - reported, retried later
            ok, problem = False, f"{type(exc).__name__}: {exc}"
        if ok:
            self._download_retry_at.pop(tier_id(tier), None)
            self._log(f"Auto-swap: {tier_label(tier)} is downloaded and verified.")
        else:
            self._download_retry_at[tier_id(tier)] = now + DOWNLOAD_RETRY_SECONDS
            self._warn(f"Auto-swap: downloading {tier_label(tier)} failed ({problem}); "
                       f"trying again {_eta(DOWNLOAD_RETRY_SECONDS)}.")

    def _stop_download(self, why: Optional[str]) -> None:
        task = self._download_task
        if task is not None and not task.done():
            task.cancel()
            if why:
                self._log(f"Auto-swap: cancelled the download of {tier_label(self._download_tier)}: {why}.")
        self._download_task = None
        self._download_tier = None
        self._download_progress = None

    # ----------------------------------------------------------------------------------
    # Bookkeeping
    # ----------------------------------------------------------------------------------

    def _read_settings(self) -> _Settings:
        """config.txt's AUTO_SWAP_* values, re-checked so a partial or odd dict cannot hurt."""
        try:
            raw = self._settings() or {}
        except Exception:  # noqa: BLE001 - unreadable settings mean "off", never "on"
            return _Settings(False, DEFAULT_UP_STABLE_SECONDS, DEFAULT_DOWN_STABLE_SECONDS, DEFAULT_COOLDOWN_SECONDS)

        def seconds(key: str, default: float) -> float:
            try:
                value = float(raw.get(key, default))
            except (TypeError, ValueError):
                return default
            return value if math.isfinite(value) and value >= 0 else default

        return _Settings(
            enabled=raw.get("auto_swap", True) is True,
            up_stable=seconds("auto_swap_up_stable_seconds", DEFAULT_UP_STABLE_SECONDS),
            down_stable=seconds("auto_swap_down_stable_seconds", DEFAULT_DOWN_STABLE_SECONDS),
            cooldown=seconds("auto_swap_cooldown_seconds", DEFAULT_COOLDOWN_SECONDS),
        )

    def _fits_safely(self, tier: Dict[str, Any], pool: Dict[str, Any]) -> bool:
        try:
            return bool(self._fits(tier, pool))
        except Exception:  # noqa: BLE001 - an unanswerable fit is "does not fit"
            return False

    def _track_fits(self, enabled: List[Dict[str, Any]], pool: Dict[str, Any], now: float) -> None:
        """Remember since when each enabled tier has fitted; forget it the moment it stops."""
        seen = set()
        for tier in enabled:
            key = tier_id(tier)
            seen.add(key)
            if self._fits_safely(tier, pool):
                self._fit_since.setdefault(key, now)
            else:
                self._fit_since.pop(key, None)
        for key in set(self._fit_since) - seen:
            self._fit_since.pop(key, None)

    def _watch_for_outside_switch(self, current: str, now: float, settings: _Settings) -> None:
        """
        A change of the served model that this controller did not make (the swap endpoint,
        the WebUI's vote, a client naming another model on an idle cluster) is respected
        like a manual switch.
        """
        if self._expected_model is None:
            self._expected_model = current
            return
        if current != self._expected_model:
            self._expected_model = current
            self._hold(now, settings.cooldown)
            self._log(f"Auto-swap: the served model changed to {current or 'nothing'} outside automatic "
                      f"switching; holding off for {_duration(settings.cooldown)}.")

    def _hold(self, now: float, seconds: float) -> None:
        self._hold_until = max(self._hold_until, now + seconds)
        self._last_swap_at = now

    def _decide(self, state: str, target: Optional[str], reason: str, detail: str) -> Decision:
        """Record the verdict; log it when the state, target or reason changed."""
        decision = Decision(state, target, reason, detail)
        self._decision = decision
        key = (state, target, reason)
        if key != self._logged_key:
            self._logged_key = key
            self._log(f"Auto-swap: {detail}")
        return decision
