"""
The phone ladder's switcher (2.9.0, contract 6c and 6c+): moves the phone engine (engine/phone_engine.py) up its rungs
when volunteers' memory has held a bigger one, and down the moment that memory goes away.

It is the coding switcher's (engine/auto_swap.py) rules, applied to the phones:

    UP    only when the near volunteers' share has held the rung for PHONE_UP_STABLE_SECONDS (10 min); a volunteer's
          lent RAM (a ``cpu`` endpoint) counts only after it has stayed PHONE_RAM_SETTLE_SECONDS (30 min); the rung's
          files are on disk and verified; PHONE_COOLDOWN_SECONDS since the last switch; the flap breaker not holding;
          never mid-answer (it waits for the lanes to empty, and after a bounded wait drains them, and a drain that
          does not finish calls the switch off); then a speed check before a phone is let in (PHONE_MIN_TPS), and a
          rung that is too slow, or does not come up, puts the base back and is tried again only after a growing hold.
    DOWN  at once (PHONE_DOWN_STABLE_SECONDS, 0) when a volunteer the layers sit on has left, is too far, is no longer
          measured, or is wanted by coding: to the biggest rung the rest still carries, else the base alone. It
          ignores the cooldown, because the layers are gone.
    CODING WINS any contest for a volunteer: ``coding_claims()`` names the volunteers coding uses or wants, and the
          phones neither take nor keep them.
    NEAR  a volunteer counts when its measured round trip is fresh and at most PHONE_SHARD_MAX_RTT_MS (200 ms). A split
          model sends each token across a link wherever its layers change machine, so the speed check decides whether a
          far-ish volunteer is worth it.
    HOME  the home card's budget never grows (phone_engine.plan_layout): a rung "fits" when the volunteers carry the
          layers the budget leaves over.

Everything outside is passed in so the decisions run against a fake clock, fake volunteers and a fake engine. The
controller only starts or restarts the phone engine when PHONE_ENGINE=own.
"""

import asyncio
import math
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

from engine.auto_swap import (DRAIN_SECONDS, FAILED_TIER_HOLD_MAX_SECONDS, FAILED_TIER_HOLD_SECONDS, QUEUE_WAIT_SECONDS,
                              TICK_SECONDS, Decision, FlapBreaker, SwapResult, _duration, _eta, _takeover_in_progress,
                              rtt_is_near)
from engine.phone_engine import PhoneEngine, plan_layout, resolve_budget_mb, rung_files, valid_endpoint

__all__ = ["PhoneSwapController", "coding_claimed_volunteers", "eligible_volunteers", "default_ready", "make_download"]


def coding_claimed_volunteers() -> Set[str]:
    """
    The ids of the volunteers the coding pool uses or is about to use. Coding wins a contest for a volunteer, so the
    phones neither take nor keep these.

    TODO(merge with builder P): answer from the coding pool's layout and its pending up-switch (engine/auto_swap.py
    and P's RAM-aware pool). Until then no volunteer is claimed, so the phones may use any near one.
    """
    return set()


def eligible_volunteers(volunteers: List[Dict[str, Any]], *, max_rtt_ms: float, ram_settle_seconds: float,
                        claimed: Set[str]) -> List[Dict[str, Any]]:
    """
    The volunteers the phones may put layers on. Each input is ``{"id", "kind": "gpu"|"cpu", "endpoint", "vram_gb" |
    "ram_lent_gb", "rtt_ms", "rtt_age_s", "present_s"}`` (``present_s``: how long it has stayed connected). Kept: one coding
    does not claim, with a valid endpoint, a fresh round trip within ``max_rtt_ms``, and for a ``cpu`` endpoint a stay of at
    least ``ram_settle_seconds`` (a lender who just arrived may be gone in a minute).
    """
    out = []
    for v in volunteers or []:
        if not isinstance(v, dict) or not v.get("id") or str(v["id"]) in claimed:
            continue
        if v.get("kind") not in ("gpu", "cpu") or not valid_endpoint(v.get("endpoint")):
            continue
        if not rtt_is_near(v.get("rtt_ms"), v.get("rtt_age_s"), max_rtt_ms):
            continue
        if v["kind"] == "cpu":
            try:
                stayed = float(v.get("present_s") or 0.0)
            except (TypeError, ValueError):
                stayed = 0.0
            if not math.isfinite(stayed) or stayed < ram_settle_seconds:
                continue
        out.append(v)
    return out


async def default_ready(engine: PhoneEngine, rung: Dict[str, Any]) -> bool:
    """Every file of the rung is on disk with its pinned size and SHA-256 (the downloader's marker makes a repeat cheap)."""
    from engine.downloader import _file_verified          # late: a big module
    import os
    directory = engine.model_dir()
    limits_vision = bool(engine._limits().get("phone_vision", True))      # noqa: SLF001 - same package, one owner

    def check() -> bool:
        return all(_file_verified(os.path.join(directory, f["file"]), f["sha256"], f["size"])
                   for f in rung_files(rung, limits_vision))
    return await asyncio.to_thread(check)


def make_download(engine: PhoneEngine) -> Callable[[Dict[str, Any]], Awaitable[bool]]:
    """``async (rung) -> bool``: fetch and verify a rung's files (engine.downloader.download_verified, resumable)."""
    async def download(rung: Dict[str, Any]) -> bool:
        from engine.downloader import download_verified
        vision = bool(engine._limits().get("phone_vision", True))          # noqa: SLF001
        return await asyncio.to_thread(download_verified, rung_files(rung, vision), engine.model_dir())
    return download


@dataclass
class _Settings:
    enabled: bool
    up_stable: float
    down_stable: float
    cooldown: float
    ram_settle: float
    max_rtt: float
    min_tps: float


class PhoneSwapController:
    """
    Args (all keyword):
        engine: the :class:`PhoneEngine` (or a stand-in with ``own()``, ``loaded``, ``process_died()``, ``rung``,
            ``volunteer_ids``, ``in_flight``, ``switching``, ``base_rung()``, ``unavailable_reason(rung)``, ``stop()`` and
            ``switch(rung, volunteers, drain_seconds=, abort_if_busy=, check_speed=, restore=, min_tps=)``).
        rungs_provider: ``() -> [rung]``, base first (``phone_engine.phone_rungs``).
        volunteers_provider: ``() -> [volunteer]`` as :func:`eligible_volunteers` reads them.
        coding_claims: ``() -> {volunteer id}``; defaults to :func:`coding_claimed_volunteers` (a stub for the merge).
        settings: ``() -> dict`` of the ``phone_*`` limits (``engine.config.get_worker_limits``).
        tier_ready / download: ``async (rung) -> bool``; ``download`` None: a rung not on disk is passed over.
        clock, log, warn, interval, breaker: as the coding switcher's.
    """

    def __init__(self, *, engine: Any, rungs_provider: Callable[[], List[Dict[str, Any]]],
                 volunteers_provider: Callable[[], List[Dict[str, Any]]],
                 settings: Callable[[], Dict[str, Any]],
                 coding_claims: Callable[[], Set[str]] = coding_claimed_volunteers,
                 tier_ready: Optional[Callable[[Dict[str, Any]], Awaitable[bool]]] = None,
                 download: Optional[Callable[[Dict[str, Any]], Awaitable[bool]]] = None,
                 clock: Callable[[], float] = time.monotonic,
                 log: Callable[[str], None] = print, warn: Optional[Callable[[str], None]] = None,
                 interval: float = TICK_SECONDS, breaker: Optional[FlapBreaker] = None) -> None:
        self._engine = engine
        self._rungs_provider, self._volunteers_provider = rungs_provider, volunteers_provider
        self._settings, self._coding_claims = settings, coding_claims
        self._ready = tier_ready or (lambda rung: default_ready(engine, rung))
        self._download = download
        self._clock, self._log = clock, log
        self._warn = warn or log
        self._interval = interval
        self._breaker = breaker or FlapBreaker(settings=self._breaker_settings, clock=clock, log=log, warn=self._warn)
        self._fit_since: Dict[str, float] = {}
        self._last_swap_at: Optional[float] = None
        self._busy_since: Optional[float] = None
        self._short_since: Optional[float] = None
        self._failed_until: Dict[str, float] = {}
        self._failures: Dict[str, int] = {}
        self._download_task: Optional[asyncio.Task] = None
        self._download_rung: Optional[str] = None
        self._download_retry_at: Dict[str, float] = {}
        self._swapping = False
        self._decision = Decision("steady", None, "starting", "Phone model switching is starting.")
        self._logged_key: Optional[tuple] = None

    # ------------------------------------------------------------------ public
    def _breaker_settings(self) -> Dict[str, Any]:
        """The flap breaker reads the coding switcher's two keys; the phones' own are handed over under those names."""
        raw = self._settings() or {}
        return {"auto_swap_max_rebuilds_per_hour": raw.get("phone_max_rebuilds_per_hour", 3),
                "auto_swap_flap_hold_seconds": raw.get("phone_flap_hold_seconds", 3600)}

    def status(self) -> Dict[str, Any]:
        enabled = self._read_settings().enabled
        d = self._decision
        if self._swapping:
            return {"enabled": enabled, "state": "swapping", "target": d.target, "detail": d.detail}
        if not enabled:
            return {"enabled": False, "state": "off", "target": None, "detail": "Phone model switching is off."}
        if self._breaker.holding():
            return {"enabled": True, "state": "held", "target": d.target if d.state == "held" else None,
                    "detail": self._breaker.detail()}
        return {"enabled": True, "state": d.state, "target": d.target, "detail": d.detail}

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one failed tick must not end the loop
                self._warn(f"Phone swap: an evaluation failed ({type(exc).__name__}: {exc}); trying again in {self._interval:.0f} s.")
            await asyncio.sleep(self._interval)

    def shutdown(self) -> None:
        self._stop_download()

    # ------------------------------------------------------------------ one evaluation
    async def tick(self) -> Decision:
        now = self._clock()
        settings = self._read_settings()
        engine = self._engine
        if not engine.own():
            self._reset()
            if engine.loaded and not engine.switching:
                # PHONE_ENGINE went back to shared (config.txt, or a named layout's lease): the card is given back.
                self._log("PHONE_ENGINE is not own any more; the phone engine is stopped.")
                await engine.stop()
            return self._decide("off", None, "shared", "The phones use the coding engine's lanes (PHONE_ENGINE=shared).")
        if _takeover_in_progress():
            self._reset()
            return self._decide("steady", None, "takeover", "A takeover lease is borrowing hardware, so the phone engine does not switch.")
        if engine.switching:
            return self._decide("swapping", None, "busy-elsewhere", "The phone engine is being switched.")
        self._collect_download()
        rungs = [r for r in (self._rungs_provider() or []) if isinstance(r, dict)]
        if not rungs:
            return self._decide("steady", None, "ladder-error", "The phone ladder is empty, so nothing switches.")
        base = engine.base_rung()
        start = next((i for i, r in enumerate(rungs) if r["id"] == base["id"]), 0)
        rungs = rungs[start:]                  # PHONE_MODEL is the base: the ladder is it and what is above it

        # Not running (never started, or it died): the base comes up alone, whatever the switch setting says.
        if not engine.loaded:
            return await self._start_base(base, now)

        if not settings.enabled:
            self._fit_since.clear()
            return self._decide("off", None, "off", "Phone model switching is off (PHONE_AUTO_SWAP=false).")

        claimed = set(self._coding_claims() or set())
        eligible = eligible_volunteers(self._volunteers_provider() or [], max_rtt_ms=settings.max_rtt,
                                       ram_settle_seconds=settings.ram_settle, claimed=claimed)
        limits = self._limits()
        plans = {r["id"]: self._plan(r, eligible, limits, rungs) for r in rungs}
        for rung in rungs:
            if plans[rung["id"]]["ok"]:
                self._fit_since.setdefault(rung["id"], now)
            else:
                self._fit_since.pop(rung["id"], None)

        current = engine.rung
        index = next((i for i, r in enumerate(rungs) if current and r["id"] == current["id"]), None)
        if index is None:
            return self._decide("steady", None, "off-ladder", "The phone engine runs a model that is not on the phone ladder.")

        eligible_ids = {str(v["id"]) for v in eligible}
        lost = [vid for vid in engine.volunteer_ids if vid not in eligible_ids]
        if index == 0 and not lost:
            self._short_since = None
            if not plans[current["id"]]["ok"]:
                return self._decide("steady", None, "base-misfit", "The base phone model does not fit the home budget; nothing to step down to.")
        elif lost or not plans[current["id"]]["ok"]:
            return await self._consider_down(rungs, index, plans, lost, now, settings)
        self._short_since = None
        return await self._consider_up(rungs, index, plans, eligible, now, settings)

    # ------------------------------------------------------------------ down
    async def _consider_down(self, rungs, index, plans, lost, now, settings) -> Decision:
        if self._short_since is None:
            self._short_since = now
        waited = now - self._short_since
        target = None
        for i in range(index, -1, -1):             # the rung that still fits: the current one with fewer volunteers first
            rung = rungs[i]
            if i == 0 or (plans[rung["id"]]["ok"] and await self._ready(rung)
                          and self._failed_until.get(rung["id"], float("-inf")) <= now):
                target = rung
                break
        why = "a volunteer left, is too far, or is wanted by coding" if lost else "the volunteers no longer carry it"
        if waited < settings.down_stable:
            return self._decide("waiting", target["id"], "volunteer-short",
                                f"{why.capitalize()}; stepping down to {target['name']} {_eta(settings.down_stable - waited)}.")
        volunteers = plans[target["id"]]["used"] if target is not rungs[0] else []
        return await self._switch(target, volunteers, "down", now, settings, rungs)

    # ------------------------------------------------------------------ up
    async def _consider_up(self, rungs, index, plans, eligible, now, settings) -> Decision:
        served = rungs[index]
        target = None
        for rung in reversed(rungs[index + 1:]):
            if rung["id"] not in self._fit_since:
                continue
            if self._failed_until.get(rung["id"], float("-inf")) > now:
                continue
            target = rung
            break
        if target is None:
            self._busy_since = None
            return self._decide("steady", None, "top" if index == len(rungs) - 1 else "no-volunteers",
                                f"Serving {served['name']}; the near volunteers carry nothing bigger yet.")
        if not await self._ready(target):
            self._busy_since = None
            return self._fetch(target, now)
        settle_left = settings.up_stable - (now - self._fit_since[target["id"]])
        cooldown_left = 0.0 if self._last_swap_at is None else max(0.0, self._last_swap_at + settings.cooldown - now)
        if settle_left > 0:
            self._busy_since = None
            return self._decide("waiting", target["id"], "settling",
                                f"Moving to {target['name']} {_eta(max(settle_left, cooldown_left))} if the volunteers stay.")
        if cooldown_left > 0:
            self._busy_since = None
            return self._decide("cooldown", target["id"], "cooldown",
                                f"Moving to {target['name']} {_eta(cooldown_left)} (cooldown after the last switch).")
        if self._breaker.holding():
            self._busy_since = None
            return self._decide("held", target["id"], "flap-hold",
                                f"Not moving to {target['name']} for now: the phone engine was rebuilt too often, "
                                f"so a move up is held until {self._breaker.until_text()}.")
        if self._engine.in_flight > 0:
            if self._busy_since is None:
                self._busy_since = now
            if now - self._busy_since < QUEUE_WAIT_SECONDS:
                return self._decide("waiting", target["id"], "queue-busy",
                                    f"Moving to {target['name']} as soon as no phone is being answered.")
        return await self._switch(target, plans[target["id"]]["used"], "up", now, settings, rungs)

    # ------------------------------------------------------------------ switching
    async def _start_base(self, base: Dict[str, Any], now: float) -> Decision:
        reason = self._engine.unavailable_reason(base)
        if reason:
            return self._decide("steady", None, "unavailable", f"The phone engine is not started: {reason}.")
        label = base["name"]
        self._swapping = True
        self._decide("swapping", base["id"], "start", f"Starting the phone engine on {label}.")
        try:
            result = await self._engine.switch(base, [], drain_seconds=0.0, abort_if_busy=False, check_speed=False, restore=base)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            result = SwapResult("failed", f"{type(exc).__name__}: {exc}")
        finally:
            self._swapping = False
        self._breaker.record(f"phone engine start on {label}")
        if result.status == "swapped":
            return self._decide("steady", None, "started", f"The phone engine is up on {label}.")
        return self._decide("waiting", base["id"], "start-failed", f"The phone engine did not start ({result.detail}).")

    async def _switch(self, rung, volunteers, direction, now, settings, rungs) -> Decision:
        label = rung["name"]
        base = rungs[0]
        self._swapping = True
        self._decide("swapping", rung["id"], f"switch-{direction}", f"Switching the phones to {label} now.")
        try:
            result = await self._engine.switch(rung, volunteers, drain_seconds=DRAIN_SECONDS,
                                               abort_if_busy=(direction == "up"), check_speed=(direction == "up"),
                                               restore=base, min_tps=settings.min_tps)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            result = SwapResult("failed", f"{type(exc).__name__}: {exc}")
        finally:
            self._swapping = False
        now = self._clock()
        if not isinstance(result, SwapResult):
            result = SwapResult("failed", "the engine returned no result")
        if result.status == "swapped":
            self._last_swap_at, self._busy_since, self._short_since = now, None, None
            self._failures.pop(rung["id"], None)
            decision = self._decide("steady", None, "switched", f"The phones are now on {label}.")
            self._breaker.record(f"phone switch {direction} to {label}")
            return decision
        if result.status == "busy":
            self._busy_since = now
            return self._decide("waiting", rung["id"], "queue-busy",
                                f"Moving to {label} as soon as no phone is being answered ({result.detail}).")
        failures = self._failures.get(rung["id"], 0) + 1
        self._failures[rung["id"]] = failures
        hold = min(FAILED_TIER_HOLD_SECONDS * 2 ** (failures - 1), FAILED_TIER_HOLD_MAX_SECONDS)
        self._failed_until[rung["id"]] = now + hold
        self._last_swap_at, self._short_since = now, None
        self._warn(f"Phone swap: {label} did not work ({result.status}: {result.detail}). It is not tried again {_eta(hold)}.")
        decision = self._decide("waiting", rung["id"], "switch-failed",
                                f"{label} did not work ({result.detail or result.status}); not trying it again {_eta(hold)}.")
        self._breaker.record(f"phone switch {direction} to {label}, which did not work")
        self._breaker.record(f"{base['name']} put back")
        return decision

    # ------------------------------------------------------------------ downloads (bounded to one at a time)
    def _fetch(self, target: Dict[str, Any], now: float) -> Decision:
        label = target["name"]
        if self._download is None:
            return self._decide("steady", None, "not-on-disk", f"{label} would fit, but its files are not on disk.")
        if self._download_task is None and self._download_retry_at.get(target["id"], 0.0) <= now:
            self._download_rung = target["id"]
            self._download_task = asyncio.ensure_future(self._download(target))
            self._log(f"Phone swap: fetching {label} in the background.")
        return self._decide("waiting", target["id"], "downloading", f"Fetching {label} before moving the phones to it.")

    def _collect_download(self) -> None:
        task = self._download_task
        if task is None or not task.done():
            return
        self._download_task = None
        try:
            ok = task.result()
        except Exception:  # noqa: BLE001
            ok = False
        if not ok and self._download_rung:
            self._download_retry_at[self._download_rung] = self._clock() + 3600.0

    def _stop_download(self) -> None:
        if self._download_task is not None and not self._download_task.done():
            self._download_task.cancel()
        self._download_task = None

    # ------------------------------------------------------------------ helpers
    def _limits(self) -> Dict[str, Any]:
        return dict(self._settings() or {})

    def _plan(self, rung: Dict[str, Any], eligible: List[Dict[str, Any]], limits: Dict[str, Any],
              rungs: List[Dict[str, Any]]) -> Dict[str, Any]:
        return plan_layout(rung, eligible if rung["id"] != rungs[0]["id"] else [], budget_mb=resolve_budget_mb(limits, self._rungs_provider()),
                           lanes=int(limits.get("phone_lanes", 2)), context=int(limits.get("phone_context", 8192)),
                           vision=bool(limits.get("phone_vision", True)),
                           host_cpu=str(limits.get("phone_device") or "").strip().lower() == "cpu")

    def _read_settings(self) -> _Settings:
        try:
            raw = self._settings() or {}
        except Exception:  # noqa: BLE001 - unreadable settings mean off
            return _Settings(False, 600.0, 0.0, 1800.0, 1800.0, 200.0, 6.0)

        def number(key: str, default: float) -> float:
            try:
                value = float(raw.get(key, default))
            except (TypeError, ValueError):
                return default
            return value if math.isfinite(value) and value >= 0 else default
        return _Settings(enabled=raw.get("phone_auto_swap", True) is True,
                         up_stable=number("phone_up_stable_seconds", 600.0), down_stable=number("phone_down_stable_seconds", 0.0),
                         cooldown=number("phone_cooldown_seconds", 1800.0), ram_settle=number("phone_ram_settle_seconds", 1800.0),
                         max_rtt=number("phone_shard_max_rtt_ms", 200.0), min_tps=number("phone_min_tps", 6.0))

    def _reset(self) -> None:
        self._stop_download()
        self._fit_since.clear()
        self._busy_since = None
        self._short_since = None

    def _decide(self, state: str, target: Optional[str], reason: str, detail: str) -> Decision:
        decision = Decision(state, target, reason, detail)
        self._decision = decision
        key = (state, target, reason)
        if key != self._logged_key:
            self._logged_key = key
            self._log(f"Phone swap: {detail}")
        return decision
