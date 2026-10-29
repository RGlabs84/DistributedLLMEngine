"""
The flap breaker (``FlapBreaker`` in engine/auto_swap.py, 2.6.2).

When the coding engine has been rebuilt more than AUTO_SWAP_MAX_REBUILDS_PER_HOUR times in an hour, the rebuilds
nobody needs to keep serving (a switch up to a bigger model, a GPU joining a working engine) are held for
AUTO_SWAP_FLAP_HOLD_SECONDS, and the log and the status ("held") say why. A step down, a restart after the engine
stopped and a manual switch are never held, only counted. On 2026-10-28 the coordinator rebuilt its engine five times
in five hours; each rebuild cut every answer in flight.

Four layers, each with its own fakes: the breaker alone (injected clocks), the controller wired to a breaker (a fake
pool and a fake switcher, as tests/test_auto_swap.py does), the coordinator's own rebuilds (the topology branch of the
monitor loop, the restart after a crash, the swap endpoint, the status), and the two config keys.
"""

import asyncio
import time
import types

import pytest
from httpx import ASGITransport, AsyncClient

import engine.config as engine_config
from engine import auto_swap, coordinator
from engine.auto_swap import AutoSwapController, FlapBreaker, SwapResult
from engine.lease import lease_manager

T0 = 10_000.0                  # the monotonic clock's first reading
WALL0 = 1_793_000_000.0        # the wall clock at T0 (epoch seconds), only for the times named in the log
HOUR = 3600.0


def hhmm(wall_seconds):
    """A time of day as the log names it: this machine's own."""
    return time.strftime("%H:%M", time.localtime(wall_seconds))


class Clock:
    """A settable monotonic clock; ``wall`` is the wall clock that moves with it."""

    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def wall(self):
        return WALL0 + (self.now - T0)

    def set(self, seconds_after_start):
        self.now = T0 + seconds_after_start


def make_breaker(**settings):
    """A breaker on a fake clock. ``.values`` is the settings dict it reads: change it to change them."""
    clock = Clock()
    values = {"auto_swap_max_rebuilds_per_hour": 3, "auto_swap_flap_hold_seconds": 3600}
    values.update(settings)
    logs, warns = [], []
    breaker = FlapBreaker(settings=lambda: dict(values), clock=clock, wall_clock=clock.wall,
                          log=logs.append, warn=warns.append)
    return types.SimpleNamespace(breaker=breaker, clock=clock, values=values, logs=logs, warns=warns)


def rebuild_at(rig, seconds, cause="a rebuild"):
    rig.clock.set(seconds)
    rig.breaker.record(cause)


# ======================================================================================
# The breaker alone
# ======================================================================================

def test_the_fourth_rebuild_in_an_hour_trips_it_and_the_third_does_not():
    rig = make_breaker()
    for minute in (0, 10, 20):
        rebuild_at(rig, minute * 60)
        assert rig.breaker.holding() is False, f"tripped on rebuild {minute // 10 + 1}"
    assert rig.warns == []
    rebuild_at(rig, 30 * 60, "the fourth")
    assert rig.breaker.holding() is True
    assert len(rig.warns) == 1


def test_only_the_last_hour_counts():
    rig = make_breaker()
    for seconds in (0, 1200, 2400):
        rebuild_at(rig, seconds)
    rebuild_at(rig, HOUR)                       # the first one is exactly an hour old: it no longer counts
    assert rig.breaker.holding() is False
    assert len(rig.breaker.recent_rebuilds()) == 3
    rebuild_at(rig, HOUR + 1199)                # 1200, 2400, 3600 and now 4799: all within an hour of 4799
    assert rig.breaker.holding() is True

    fresh = make_breaker()
    for seconds in (0, 1200, 2400):
        rebuild_at(fresh, seconds)
    rebuild_at(fresh, HOUR - 1)                 # one second short of an hour: the first still counts
    assert fresh.breaker.holding() is True


def test_the_hold_lasts_the_configured_time_and_then_ends():
    rig = make_breaker(auto_swap_flap_hold_seconds=1800)
    for seconds in (0, 10, 20, 30):
        rebuild_at(rig, seconds)
    tripped_at = 30
    for after in (0, 1, 900, 1799):
        rig.clock.set(tripped_at + after)
        assert rig.breaker.holding() is True, f"released {after} s after the trip"
    rig.clock.set(tripped_at + 1800)
    assert rig.breaker.holding() is False
    assert rig.breaker.detail() == "" and rig.breaker.until_text() == ""


def test_the_trip_is_logged_once_with_how_many_when_why_and_until_when():
    rig = make_breaker()
    causes = ["auto-swap up to 35B IQ4_XS", "a GPU left, engine rebuilt without it",
              "engine started, it was not running (qwen.gguf)", "auto-swap up to 35B IQ4_XS"]
    for seconds, cause in zip((0, 900, 1800, 2700), causes):
        rebuild_at(rig, seconds, cause)
    assert len(rig.warns) == 1 and rig.logs == []
    expected_until = hhmm(WALL0 + 2700 + HOUR)
    assert rig.warns[0] == (
        "Flap breaker: The engine was rebuilt 4 times within an hour: "
        f"{hhmm(WALL0)} auto-swap up to 35B IQ4_XS; "
        f"{hhmm(WALL0 + 900)} a GPU left, engine rebuilt without it; "
        f"{hhmm(WALL0 + 1800)} engine started, it was not running (qwen.gguf); "
        f"{hhmm(WALL0 + 2700)} auto-swap up to 35B IQ4_XS. "
        "So rebuilds that are not needed to keep serving (a switch up to a bigger model, a GPU joining) are held until "
        f"{expected_until} (in 60 min). A step down, a restart after the engine stopped and a manual switch "
        "still go ahead.")


def test_the_status_sentence_is_the_log_sentence_with_the_time_left():
    rig = make_breaker()
    for seconds in (0, 10, 20, 30):
        rebuild_at(rig, seconds, f"cause {seconds}")
    rig.clock.set(30 + 1800)                                 # half an hour into the hold
    detail = rig.breaker.detail()
    assert detail == rig.warns[0].replace("Flap breaker: ", "", 1).replace("(in 60 min)", "(in 30 min)")
    assert rig.breaker.until_text() == hhmm(WALL0 + 30 + HOUR)


def test_rebuilds_during_a_hold_are_counted_but_neither_extend_it_nor_log_again():
    rig = make_breaker()
    for seconds in (0, 10, 20, 30):
        rebuild_at(rig, seconds)
    rebuild_at(rig, 600, "a crash restart")
    rebuild_at(rig, 900, "another")
    assert len(rig.warns) == 1                               # still the one line
    assert len(rig.breaker.recent_rebuilds()) == 6           # but counted
    rig.clock.set(30 + HOUR)
    assert rig.breaker.holding() is False                    # the hold ended an hour after the trip, as first set


def test_after_a_hold_shorter_than_the_hour_the_next_rebuild_trips_it_again_if_the_hour_is_still_full():
    rig = make_breaker(auto_swap_flap_hold_seconds=600)
    for seconds in (0, 10, 20, 30):
        rebuild_at(rig, seconds)
    rig.clock.set(700)
    assert rig.breaker.holding() is False                    # the hold of 600 s is over, the hour is not
    rebuild_at(rig, 750, "let through")
    assert rig.breaker.holding() is True                     # tripped again by that one
    assert len(rig.warns) == 2


def test_zero_rebuilds_an_hour_turns_the_breaker_off():
    rig = make_breaker(auto_swap_max_rebuilds_per_hour=0)
    for seconds in range(0, 100, 10):
        rebuild_at(rig, seconds)
    assert rig.breaker.holding() is False and rig.breaker.hold_back("anything") is False
    assert rig.warns == [] and rig.logs == []
    assert len(rig.breaker.recent_rebuilds()) == 10          # still counted: turning it on later sees them


def test_turning_it_off_while_it_holds_ends_the_hold():
    rig = make_breaker()
    for seconds in (0, 10, 20, 30):
        rebuild_at(rig, seconds)
    assert rig.breaker.holding() is True
    rig.values["auto_swap_max_rebuilds_per_hour"] = 0
    assert rig.breaker.holding() is False


def test_a_lowered_limit_is_read_at_the_next_rebuild():
    rig = make_breaker()
    rebuild_at(rig, 0)
    rebuild_at(rig, 10)
    assert rig.breaker.holding() is False
    rig.values["auto_swap_max_rebuilds_per_hour"] = 1
    rebuild_at(rig, 20)
    assert rig.breaker.holding() is True


@pytest.mark.parametrize("settings", [
    {"auto_swap_max_rebuilds_per_hour": "many", "auto_swap_flap_hold_seconds": "long"},
    {"auto_swap_max_rebuilds_per_hour": -1, "auto_swap_flap_hold_seconds": 0},
    {"auto_swap_max_rebuilds_per_hour": True, "auto_swap_flap_hold_seconds": False},
    {"auto_swap_max_rebuilds_per_hour": float("nan"), "auto_swap_flap_hold_seconds": float("inf")},
    {},
])
def test_settings_that_are_not_sane_are_the_defaults(settings):
    rig = make_breaker()
    rig.values.clear()
    rig.values.update(settings)
    for seconds in (0, 10, 20):
        rebuild_at(rig, seconds)
    assert rig.breaker.holding() is False                    # 3 is the default limit
    rebuild_at(rig, 30)
    assert rig.breaker.holding() is True
    rig.clock.set(30 + auto_swap.DEFAULT_FLAP_HOLD_SECONDS - 1)
    assert rig.breaker.holding() is True
    rig.clock.set(30 + auto_swap.DEFAULT_FLAP_HOLD_SECONDS)
    assert rig.breaker.holding() is False


def test_settings_that_cannot_be_read_leave_the_breaker_on():
    clock = Clock()
    warns = []

    def broken():
        raise OSError("config.txt is locked")

    breaker = FlapBreaker(settings=broken, clock=clock, wall_clock=clock.wall, log=lambda m: None, warn=warns.append)
    for seconds in (0, 10, 20, 30):
        clock.set(seconds)
        breaker.record("a rebuild")
    assert breaker.holding() is True and len(warns) == 1


def test_an_optional_rebuild_that_is_held_is_named_once_per_hold():
    rig = make_breaker()
    assert rig.breaker.hold_back("redistributing the model") is False       # not tripped: nothing held, nothing said
    for seconds in (0, 10, 20, 30):
        rebuild_at(rig, seconds)
    rig.logs.clear()
    until = hhmm(WALL0 + 30 + HOUR)
    for _ in range(5):
        assert rig.breaker.hold_back("redistributing the model") is True
    assert rig.logs == [f"Flap breaker: not redistributing the model until {until}; the engine was rebuilt too "
                        "often in the last hour."]
    assert rig.breaker.hold_back("something else") is True
    assert len(rig.logs) == 2
    rig.clock.set(30 + HOUR)
    assert rig.breaker.hold_back("redistributing the model") is False


def test_a_second_trip_names_what_it_holds_again():
    rig = make_breaker(auto_swap_flap_hold_seconds=600)
    for seconds in (0, 10, 20, 30):
        rebuild_at(rig, seconds)
    rig.breaker.hold_back("redistributing the model")
    rig.clock.set(700)
    rebuild_at(rig, 710)                                      # the hour is still full: tripped again
    rig.logs.clear()
    assert rig.breaker.hold_back("redistributing the model") is True
    assert len(rig.logs) == 1


def test_the_sentence_names_the_latest_eight_rebuilds():
    rig = make_breaker(auto_swap_max_rebuilds_per_hour=11)
    for index in range(12):
        rebuild_at(rig, index * 60, f"cause {index}")
    sentence = rig.warns[0]
    assert "rebuilt 12 times within an hour: 4 earlier, then " in sentence
    assert "cause 3" not in sentence and "cause 4" in sentence and "cause 11" in sentence


def test_reset_forgets_every_rebuild_and_any_hold():
    rig = make_breaker()
    for seconds in (0, 10, 20, 30):
        rebuild_at(rig, seconds)
    rig.breaker.reset()
    assert rig.breaker.holding() is False and rig.breaker.recent_rebuilds() == []
    for seconds in (40, 50, 60):
        rebuild_at(rig, seconds)
    assert rig.breaker.holding() is False


def test_the_defaults_are_the_owners_choice():
    assert auto_swap.DEFAULT_MAX_REBUILDS_PER_HOUR == 3
    assert auto_swap.DEFAULT_FLAP_HOLD_SECONDS == 3600.0
    assert auto_swap.FLAP_WINDOW_SECONDS == 3600.0
    assert "held" in auto_swap.STATES and len(set(auto_swap.STATES)) == len(auto_swap.STATES)


# ======================================================================================
# The controller wired to a breaker
# ======================================================================================

def tier(tid, gb, **extra):
    entry = {"id": tid, "name": f"{gb} GB tier", "file": f"{tid}.gguf", "url": f"https://models.example/{tid}.gguf",
             "size": 1_000_000, "sha256": "ab" * 32, "min_vram_gb": float(gb), "context": 65536, "enabled": True}
    entry.update(extra)
    return entry


# The shortest timings config.txt accepts, so a pool that flaps can be driven in a few minutes of fake time.
FAST = {"auto_swap_up_stable_seconds": 60, "auto_swap_down_stable_seconds": 30, "auto_swap_cooldown_seconds": 300}


class Rig:
    """An auto-swap controller on fakes, with a breaker of its own on the same clock."""

    def __init__(self, *, vram=16.0, current="t16.gguf", settings=None):
        self.clock = Clock()
        self.vram = vram
        self.current = current
        self.settings = {"auto_swap": True, **FAST, "auto_swap_max_rebuilds_per_hour": 3,
                         "auto_swap_flap_hold_seconds": 3600}
        self.settings.update(settings or {})
        self.result = "swapped"
        self.swaps = []
        self.logs, self.warns = [], []
        self.breaker = FlapBreaker(settings=lambda: dict(self.settings), clock=self.clock, wall_clock=self.clock.wall,
                                   log=self.logs.append, warn=self.warns.append)
        self.controller = AutoSwapController(
            tiers_provider=lambda: [tier("t16", 16), tier("t24", 24)],
            pool_provider=lambda: {"vram_gb": self.vram, "far_vram_gb": 0.0, "sysram_gb": 12.0},
            current_model=lambda: self.current,
            swap=self._swap,
            tier_ready=self._ready,
            download=self._download,
            settings=lambda: dict(self.settings),
            swap_in_progress=lambda: False,
            queue_busy=lambda: False,
            clock=self.clock,
            log=self.logs.append,
            warn=self.warns.append,
            breaker=self.breaker,
        )

    async def _swap(self, chosen, *, drain_seconds, abort_if_busy, check_speed=False):
        self.swaps.append((chosen["id"], self.clock.now - T0))
        if self.result == "swapped":
            self.current = chosen["file"]
        return SwapResult(self.result, "from the test")

    async def _ready(self, chosen):
        return True

    async def _download(self, chosen):
        return True

    async def at(self, seconds):
        self.clock.set(seconds)
        return await self.controller.tick()

    async def every(self, start, end, step=30):
        decision = None
        for seconds in range(start, end + 1, step):
            decision = await self.at(seconds)
        return decision

    def swapped_to(self):
        return [tid for tid, _ in self.swaps]


async def flap(rig):
    """Four switches in eight minutes: up, down, up, down. The pool is 24 GB, then 16, then 24, then 16 again."""
    rig.vram = 24
    await rig.every(0, 60)                        # up to t24 at +60 (rebuild 1)
    rig.vram = 16
    await rig.every(90, 120)                      # down to t16 at +120 (rebuild 2), the cooldown does not apply
    rig.vram = 24
    await rig.every(150, 420)                     # up to t24 at +420, the cooldown after the last switch (rebuild 3)
    rig.vram = 16
    await rig.every(450, 480)                     # down to t16 at +480 (rebuild 4)


@pytest.mark.anyio
async def test_a_pool_that_flaps_trips_the_breaker_at_the_fourth_switch_and_holds_the_next_one_up():
    rig = Rig()
    await flap(rig)
    assert rig.swapped_to() == ["t24", "t16", "t24", "t16"]
    assert rig.breaker.holding() is True
    assert len([w for w in rig.warns if w.startswith("Flap breaker:")]) == 1

    rig.vram = 24                                  # the pool is big enough for t24 again, and has been long enough
    held = await rig.every(510, 4050)              # an hour of ticks, all inside the hold (480 + 3600)
    assert rig.swapped_to() == ["t24", "t16", "t24", "t16"], "a switch up went ahead while held"
    assert held.state == "held" and held.reason == "flap-hold" and held.target == "t24"
    assert sum("Not switching to 24 GB tier for now" in line for line in rig.logs) == 1      # logged once, not per tick

    await rig.at(4080)                             # the hold ends an hour after the fourth switch
    assert rig.swapped_to() == ["t24", "t16", "t24", "t16", "t24"]
    assert rig.current == "t24.gguf"


@pytest.mark.anyio
async def test_the_third_switch_in_an_hour_does_not_hold_anything():
    rig = Rig()
    rig.vram = 24
    await rig.every(0, 60)
    rig.vram = 16
    await rig.every(90, 120)
    rig.vram = 24
    await rig.every(150, 450)
    assert rig.swapped_to() == ["t24", "t16", "t24"]
    assert rig.breaker.holding() is False and rig.warns == []
    assert rig.controller.status()["state"] == "steady"


@pytest.mark.anyio
async def test_a_step_down_goes_ahead_while_held_and_is_counted():
    rig = Rig(vram=24, current="t24.gguf")
    for seconds in (0, 10, 20, 30):
        rig.clock.set(seconds)
        rig.breaker.record("an earlier rebuild")     # the engine has just been rebuilt four times
    assert rig.breaker.holding() is True
    await rig.at(0)
    rig.vram = 16                                    # a GPU left: the pool cannot hold what is served
    await rig.every(30, 90)
    assert rig.swapped_to() == ["t16"]               # stepped down although the breaker holds
    assert rig.current == "t16.gguf"
    assert rig.breaker.recent_rebuilds()[-1] == "auto-swap down to 16 GB tier"
    assert len(rig.breaker.recent_rebuilds()) == 5   # counted
    assert rig.breaker.holding() is True


@pytest.mark.anyio
async def test_the_status_says_held_with_the_sentence_while_the_breaker_holds():
    rig = Rig()
    await flap(rig)
    status = rig.controller.status()
    assert set(status) == {"enabled", "state", "target", "detail"}
    assert status["enabled"] is True and status["state"] == "held" and status["state"] in auto_swap.STATES
    assert status["target"] is None                  # nothing is waiting for the hold yet
    assert status["detail"] == rig.breaker.detail()
    assert "rebuilt 4 times within an hour" in status["detail"]
    for cause in ("auto-swap up to 24 GB tier", "auto-swap down to 16 GB tier"):
        assert cause in status["detail"]
    assert f"held until {rig.breaker.until_text()}" in status["detail"]

    rig.vram = 24
    await rig.every(510, 900)
    waiting = rig.controller.status()
    assert waiting["state"] == "held" and waiting["target"] == "t24" and waiting["detail"] == rig.breaker.detail()


@pytest.mark.anyio
async def test_the_status_is_not_held_once_the_hold_ends_even_before_the_next_tick():
    rig = Rig()
    await flap(rig)
    rig.vram = 24
    await rig.every(510, 1200)
    assert rig.controller.status()["state"] == "held"
    rig.clock.set(480 + 3600)                        # the hold is over; the controller has not ticked yet
    status = rig.controller.status()
    assert status["state"] == "steady" and status["target"] is None and "ended" in status["detail"]


@pytest.mark.anyio
async def test_off_and_swapping_are_still_said_before_held():
    rig = Rig()
    await flap(rig)
    rig.settings["auto_swap"] = False
    assert rig.controller.status() == {"enabled": False, "state": "off", "target": None,
                                       "detail": "Automatic model switching is off."}
    rig.settings["auto_swap"] = True
    rig.controller._swapping = True
    assert rig.controller.status()["state"] == "swapping"


@pytest.mark.anyio
async def test_a_breaker_that_is_off_holds_nothing_through_the_controller():
    rig = Rig(settings={"auto_swap_max_rebuilds_per_hour": 0})
    await flap(rig)
    rig.vram = 24
    await rig.every(510, 900)
    assert rig.swapped_to() == ["t24", "t16", "t24", "t16", "t24"]
    assert rig.warns == [] and rig.controller.status()["state"] != "held"


@pytest.mark.anyio
@pytest.mark.parametrize("result, rebuilt", [("swapped", 1), ("failed", 2), ("superseded", 1), ("busy", 0), ("refused", 0)])
async def test_the_controller_counts_what_a_switch_really_rebuilt(result, rebuilt):
    rig = Rig(vram=24, settings={"auto_swap_max_rebuilds_per_hour": 100})
    rig.result = result
    await rig.every(0, 60)                           # the switch up is tried at +60
    assert rig.swapped_to() == ["t24"]
    assert len(rig.breaker.recent_rebuilds()) == rebuilt


@pytest.mark.anyio
async def test_a_failed_switch_counts_as_out_and_back_and_names_both():
    rig = Rig(vram=24, settings={"auto_swap_max_rebuilds_per_hour": 100})
    rig.result = "failed"
    await rig.every(0, 60)
    assert rig.breaker.recent_rebuilds() == ["auto-swap up to 24 GB tier, which did not come up",
                                             "16 GB tier put back"]


@pytest.mark.anyio
async def test_a_failed_switch_and_manual_switches_can_trip_it_together():
    rig = Rig(vram=24)
    rig.result = "failed"
    await rig.every(0, 60)                           # out and back: 2 rebuilds
    assert rig.breaker.holding() is False
    rig.controller.note_manual_swap("t16.gguf")      # 3
    assert rig.breaker.holding() is False
    rig.controller.note_manual_swap("t16.gguf")      # 4
    assert rig.breaker.holding() is True


def test_a_manual_switch_is_counted_by_name_and_never_held():
    rig = Rig()
    rig.controller._enabled_tiers = [tier("t16", 16), tier("t24", 24)]
    for _ in range(6):
        rig.controller.note_manual_swap("t24.gguf")  # the operator may switch as often as they like
    assert rig.breaker.recent_rebuilds() == ["manual switch to 24 GB tier"] * 6
    assert rig.breaker.holding() is True
    rig.controller.note_manual_swap("some-other-model.gguf")
    assert rig.breaker.recent_rebuilds()[-1] == "manual switch to some-other-model.gguf"
    rig.controller.note_manual_swap()
    assert rig.breaker.recent_rebuilds()[-1] == "manual switch to another model"


def test_a_controller_given_no_breaker_keeps_its_own_on_its_own_settings():
    clock = Clock()
    logs = []
    settings = {"auto_swap": True, "auto_swap_max_rebuilds_per_hour": 1, "auto_swap_flap_hold_seconds": 600}

    async def nothing(*args, **kwargs):
        return True

    controller = AutoSwapController(
        tiers_provider=lambda: [], pool_provider=lambda: {}, current_model=lambda: "", swap=nothing,
        tier_ready=nothing, download=nothing, settings=lambda: dict(settings), swap_in_progress=lambda: False,
        queue_busy=lambda: False, clock=clock, log=logs.append)
    controller.note_manual_swap("a.gguf")
    controller.note_manual_swap("b.gguf")
    assert controller.status()["state"] == "held"
    clock.set(600)
    assert controller.status()["state"] == "steady"


# ======================================================================================
# The coordinator's own rebuilds
# ======================================================================================

def trip(breaker, count=4, cause="an earlier rebuild"):
    for _ in range(count):
        breaker.record(cause)
    assert breaker.holding() is True


@pytest.fixture
def breaker(monkeypatch):
    """The coordinator's breaker, replaced by one on a fake clock that logs where the test can see it."""
    rig = make_breaker()
    monkeypatch.setattr(coordinator, "flap_breaker", rig.breaker)
    monkeypatch.setattr(coordinator.auto_swapper, "_breaker", rig.breaker)
    return rig


def set_live_endpoints(monkeypatch, endpoints):
    """What ``manager.current_endpoints`` says the engine runs on (a read-only property, so patched on the class)."""
    monkeypatch.setattr(coordinator.InferenceManager, "current_endpoints",
                        property(lambda self: list(endpoints)), raising=False)


@pytest.fixture
def settle_env(monkeypatch, breaker):
    monkeypatch.setattr(coordinator, "_pending_topology", None)
    monkeypatch.setattr(coordinator, "_pending_topology_since", 0.0)
    monkeypatch.setattr(coordinator, "_pending_drain", None)
    monkeypatch.setattr(coordinator, "_pending_drain_since", 0.0)
    monkeypatch.setattr(coordinator, "get_worker_limits",
                        lambda: {"topology_settle_seconds": 30, "shard_policy": "auto", "rebalance_on_join": True})
    set_live_endpoints(monkeypatch, ["127.0.0.1:50052"])
    with lease_manager._lock:
        lease_manager._leases.clear()
    return monkeypatch


JOINED = ["127.0.0.1:50052", "10.8.0.4:50052"]


def test_a_gpu_that_only_joins_is_not_redistributed_while_the_breaker_holds(settle_env, breaker):
    trip(breaker.breaker)
    assert coordinator._topology_change_is_settled(JOINED) is False          # first seen
    coordinator._pending_topology_since -= 31                                # the cluster has settled
    assert coordinator._topology_change_is_settled(JOINED) is False          # but the breaker holds it
    assert coordinator._topology_change_is_settled(JOINED) is False
    held = [line for line in breaker.logs if "redistributing the model for the new GPU(s)" in line]
    assert len(held) == 1 and "until" in held[0]                             # said once, not every two seconds


def test_a_held_gpu_is_taken_the_moment_the_hold_ends(settle_env, breaker):
    trip(breaker.breaker)
    coordinator._topology_change_is_settled(JOINED)
    coordinator._pending_topology_since -= 31
    assert coordinator._topology_change_is_settled(JOINED) is False
    breaker.clock.set(auto_swap.DEFAULT_FLAP_HOLD_SECONDS)                   # the hold is over
    assert coordinator._topology_change_is_settled(JOINED) is True           # no second settle wait


def test_the_same_gpu_joining_is_redistributed_as_before_when_nothing_is_held(settle_env, breaker):
    assert coordinator._topology_change_is_settled(JOINED) is False
    coordinator._pending_topology_since -= 31
    assert coordinator._topology_change_is_settled(JOINED) is True
    assert breaker.logs == []


def test_a_gpu_that_left_is_rebuilt_at_once_even_while_the_breaker_holds(settle_env, breaker):
    set_live_endpoints(settle_env, JOINED)
    trip(breaker.breaker)
    assert coordinator._topology_change_is_settled(["127.0.0.1:50052"]) is True
    assert breaker.logs == []                                                # nothing was held, so nothing said


def test_a_change_that_loses_a_gpu_and_gains_another_counts_as_a_loss(settle_env, breaker):
    set_live_endpoints(settle_env, JOINED)
    trip(breaker.breaker)
    assert coordinator._topology_change_is_settled(["127.0.0.1:50052", "10.8.0.9:50052"]) is True


def test_what_changed_is_named_without_naming_a_machine(monkeypatch):
    set_live_endpoints(monkeypatch, ["127.0.0.1:50052", "10.8.0.4:50052"])
    assert coordinator._topology_rebuild_cause(["127.0.0.1:50052"]) == "a GPU left, engine rebuilt without it"
    assert coordinator._topology_rebuild_cause(JOINED + ["10.8.0.7:50052"]) == "a GPU joined, engine rebuilt to use it"
    assert coordinator._topology_rebuild_cause(["127.0.0.1:50052", "10.8.0.7:50052"]) == (
        "GPUs joined and left, engine rebuilt")
    assert coordinator._topology_rebuild_cause(JOINED) == "topology change, engine rebuilt"
    for cause in (coordinator._topology_rebuild_cause(["10.8.0.7:50052"]),
                  coordinator._topology_rebuild_cause(JOINED + ["10.8.0.7:50052"])):
        assert "10.8" not in cause and "50052" not in cause


class RecordingEngines:
    """The manager's stop and start, as recorders."""

    def __init__(self, succeed=True, message="Started"):
        self.calls = []
        self.succeed = succeed
        self.message = message

    async def stop_all(self):
        self.calls.append("stop")

    async def start(self, model, endpoints, vram, sysram, queue_channels=2):
        self.calls.append(("start", model, list(endpoints)))
        return self.succeed, 8081 if self.succeed else 0, self.message if self.succeed else "Binary Missing"


@pytest.fixture
def engines(monkeypatch):
    fake = RecordingEngines()
    monkeypatch.setattr(coordinator.manager, "astop_all_models", fake.stop_all)
    monkeypatch.setattr(coordinator.manager, "astart_model", fake.start)
    monkeypatch.setattr(coordinator, "spawn_vram_progress_task", lambda port, model: None)
    monkeypatch.setattr(coordinator, "cluster_status", "Active")
    return fake


@pytest.mark.anyio
async def test_a_topology_rebuild_is_counted_with_what_changed(engines, breaker, monkeypatch):
    set_live_endpoints(monkeypatch, ["127.0.0.1:50052", "10.8.0.4:50052"])
    await coordinator._apply_topology_change("m.gguf", ["127.0.0.1:50052"], 16.0, 32.0, "Network topology changed.")
    set_live_endpoints(monkeypatch, ["127.0.0.1:50052"])
    await coordinator._apply_topology_change("m.gguf", JOINED, 32.0, 32.0, "Network topology changed.")
    assert breaker.breaker.recent_rebuilds() == ["a GPU left, engine rebuilt without it",
                                                 "a GPU joined, engine rebuilt to use it"]
    assert engines.calls[0] == "stop"


@pytest.mark.anyio
async def test_a_restart_after_the_engine_stopped_is_counted_once_it_started(engines, breaker):
    await coordinator._auto_deploy_model("phi3-mini.gguf", ["127.0.0.1:50052"], 16.0, 32.0, 4.0)
    assert breaker.breaker.recent_rebuilds() == ["engine started, it was not running (phi3-mini.gguf)"]


@pytest.mark.anyio
async def test_a_deploy_that_built_nothing_is_not_counted(engines, breaker):
    engines.succeed = False
    for _ in range(10):                                  # a start that is refused every two seconds is not a rebuild
        await coordinator._auto_deploy_model("phi3-mini.gguf", ["127.0.0.1:50052"], 16.0, 32.0, 4.0)
    assert breaker.breaker.recent_rebuilds() == [] and breaker.breaker.holding() is False


@pytest.mark.anyio
async def test_a_restart_after_a_crash_is_counted_and_can_trip_it_but_is_never_held(engines, breaker):
    for _ in range(3):
        breaker.breaker.record("an earlier rebuild")
    await coordinator._auto_deploy_model("phi3-mini.gguf", ["127.0.0.1:50052"], 16.0, 32.0, 4.0)
    assert breaker.breaker.holding() is True             # the fourth, so the breaker tripped on it
    await coordinator._auto_deploy_model("phi3-mini.gguf", ["127.0.0.1:50052"], 16.0, 32.0, 4.0)
    assert engines.calls == [("start", "phi3-mini.gguf", ["127.0.0.1:50052"])] * 2     # and it still starts it
    assert len(breaker.breaker.recent_rebuilds()) == 5


# --- through the monitor loop ----------------------------------------------------------------------------------

@pytest.fixture
def monitor(monkeypatch, breaker):
    """``monitor_workers_async`` with every collaborator faked, one tick costing milliseconds (as
    tests/test_coordinator_counter.py's monitor_env does)."""
    # ``live`` is what the engine runs on: a rebuild changes it, as a real one does.
    state = types.SimpleNamespace(running=None, planned=["10.0.0.2:50052"], live=["10.0.0.2:50052"],
                                  admissible=True, calls=[],
                                  limits={"topology_settle_seconds": 0, "rebalance_on_join": True,
                                          "shard_policy": "auto", "topology_rejoin_max_seconds": 600})

    monkeypatch.setattr(coordinator, "MONITOR_TICK_SECONDS", 0.005)
    monkeypatch.setattr(coordinator, "_reconfiguration_task", None)
    monkeypatch.setattr(coordinator, "_background_tasks", set())
    monkeypatch.setattr(coordinator, "global_active_model", "phi3-mini")
    monkeypatch.setattr(coordinator, "cluster_status", "Active")
    monkeypatch.setattr(coordinator, "is_swapping_models", False)
    monkeypatch.setattr(coordinator, "is_recalculating_queue", False)
    monkeypatch.setattr(coordinator, "has_pulled_seed", True)
    monkeypatch.setattr(coordinator, "is_downloading_seed", False)
    monkeypatch.setattr(coordinator, "_pending_topology", None)
    monkeypatch.setattr(coordinator, "_pending_topology_since", 0.0)
    monkeypatch.setattr(coordinator, "_pending_drain", None)
    monkeypatch.setattr(coordinator, "_pending_drain_since", 0.0)
    monkeypatch.setattr(coordinator, "get_worker_limits", lambda: state.limits)
    for name, value in (("prune_dead_workers", lambda: None), ("get_total_vram_gb", lambda: 24.0),
                        ("get_total_sysram_gb", lambda: 32.0), ("get_total_cpu_cores", lambda: 8),
                        ("get_total_cpu_threads", lambda: 16), ("count", lambda: 2),
                        ("get_active_rpc_endpoints", lambda: list(state.planned)),
                        ("endpoint_is_draining", lambda endpoint: False)):
        monkeypatch.setattr(coordinator.registry, name, value)
    monkeypatch.setattr(coordinator.registry, "plan_endpoints", lambda required_gb, policy="auto": list(state.planned))
    monkeypatch.setattr(coordinator.InferenceManager, "current_endpoints",
                        property(lambda self: list(state.live)), raising=False)

    async def broadcast(payload):
        return None

    async def estimate(model_path):
        return 4.0

    async def snapshot(model_path):
        return state.running

    async def admit(model_path, vram, sysram, endpoints=None):
        return (state.admissible, "ok")

    async def stop_all():
        state.calls.append("stop")

    async def start(model, endpoints, vram, sysram, queue_channels=2):
        state.calls.append(("start", model, list(endpoints)))
        state.live = list(endpoints)
        state.running = {"process": object(), "port": 8081}
        return True, 8081, "Started"

    async def no_progress(port, model_name):
        return None

    monkeypatch.setattr(coordinator, "broadcast", broadcast)
    monkeypatch.setattr(coordinator.manager, "aestimate_model_vram_gb", estimate)
    monkeypatch.setattr(coordinator.manager, "asnapshot_model", snapshot)
    monkeypatch.setattr(coordinator.manager, "acan_admit", admit)
    monkeypatch.setattr(coordinator.manager, "astop_all_models", stop_all)
    monkeypatch.setattr(coordinator.manager, "astart_model", start)
    monkeypatch.setattr(coordinator, "stream_vram_progress_async", no_progress)
    monkeypatch.setattr(coordinator, "_vram_progress_streams", {})
    with lease_manager._lock:
        lease_manager._leases.clear()
    return state


async def run_monitor(seconds=0.15):
    task = asyncio.create_task(coordinator.monitor_workers_async())
    await asyncio.sleep(seconds)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    pending = list(coordinator._background_tasks)
    for background in pending:
        background.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.anyio
async def test_the_monitor_restarts_a_stopped_engine_while_the_breaker_holds(monitor, breaker):
    trip(breaker.breaker)
    monitor.running = None                               # the engine died: nothing is running
    await run_monitor()
    assert ("start", "phi3-mini", ["10.0.0.2:50052"]) in monitor.calls
    assert breaker.breaker.recent_rebuilds()[-1] == "engine started, it was not running (phi3-mini)"


@pytest.mark.anyio
async def test_the_monitor_does_not_rebuild_for_a_gpu_that_only_joins_while_the_breaker_holds(monitor, breaker):
    trip(breaker.breaker)
    monitor.running = {"process": object(), "port": 8081}
    monitor.planned = ["10.0.0.2:50052", "10.0.0.3:50052"]       # a GPU joined a working engine
    await run_monitor()
    assert monitor.calls == []                                   # no stop, no start
    assert any("redistributing the model for the new GPU(s)" in line for line in breaker.logs)


@pytest.mark.anyio
async def test_the_monitor_rebuilds_for_a_gpu_that_only_joins_when_nothing_is_held(monitor, breaker):
    monitor.running = {"process": object(), "port": 8081}
    monitor.planned = ["10.0.0.2:50052", "10.0.0.3:50052"]
    await run_monitor()
    assert monitor.calls[0] == "stop" and monitor.calls[1] == ("start", "phi3-mini", monitor.planned)
    assert breaker.breaker.recent_rebuilds() == ["a GPU joined, engine rebuilt to use it"]


@pytest.mark.anyio
async def test_the_monitor_rebuilds_for_a_lost_gpu_while_the_breaker_holds(monitor, breaker):
    trip(breaker.breaker)
    monitor.live = ["10.0.0.2:50052", "10.0.0.3:50052"]
    monitor.running = {"process": object(), "port": 8081}
    monitor.planned = ["10.0.0.2:50052"]                         # the second GPU is gone
    await run_monitor()
    assert monitor.calls == ["stop", ("start", "phi3-mini", ["10.0.0.2:50052"])]      # once, at once
    assert breaker.breaker.recent_rebuilds()[-1] == "a GPU left, engine rebuilt without it"
    assert breaker.logs == []                                    # nothing was held, so nothing said


# --- the swap endpoint and the status --------------------------------------------------------------------------

def _client():
    transport = ASGITransport(app=coordinator.app, client=("127.0.0.1", 5000))
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.anyio
async def test_a_manual_swap_through_the_endpoint_is_counted_and_never_held(breaker, monkeypatch, tmp_path,
                                                                            isolated_config):
    model_file = tmp_path / "new.gguf"
    model_file.write_bytes(b"GGUF")
    swapped = []

    async def resolve(tag):
        return str(model_file)

    async def estimate(tag):
        return 10.0

    async def core(target, req_gb, total_vram, total_sysram, **options):
        swapped.append(target)
        return {"outcome": "swapped", "success": True, "detail": "Started"}

    monkeypatch.setattr(coordinator.manager, "aresolve_model_path", resolve)
    monkeypatch.setattr(coordinator.manager, "aestimate_model_vram_gb", estimate)
    monkeypatch.setattr(coordinator.registry, "get_total_vram_gb", lambda: 24.0)
    monkeypatch.setattr(coordinator.registry, "get_total_sysram_gb", lambda: 12.0)
    monkeypatch.setattr(coordinator, "_swap_model_core", core)
    trip(breaker.breaker)

    async with _client() as client:
        resp = await client.post("/v1/cluster/swap", json={"model": "new"})

    assert resp.status_code == 200 and resp.json()["status"] == "swapped"
    assert swapped == ["new.gguf"]                               # the operator's switch went ahead while held
    assert breaker.breaker.recent_rebuilds()[-1] == "manual switch to new.gguf"
    assert len(breaker.breaker.recent_rebuilds()) == 5


@pytest.mark.anyio
async def test_cluster_status_reports_held_with_the_sentence_and_names_no_machine(breaker, isolated_config):
    for index, cause in enumerate(["auto-swap up to 35B IQ4_XS", "a GPU left, engine rebuilt without it",
                                   "engine started, it was not running (qwen.gguf)", "a GPU joined, engine rebuilt to use it"]):
        breaker.clock.set(index * 60)
        breaker.breaker.record(cause)
    assert breaker.breaker.holding() is True
    breaker.clock.set(600)
    async with _client() as client:
        resp = await client.get("/v1/cluster/status")
    block = resp.json()["auto_swap"]
    assert set(block) == {"enabled", "state", "target", "detail"}
    assert block["state"] == "held" and block["state"] in auto_swap.STATES
    assert block["detail"] == breaker.breaker.detail()
    assert "rebuilt 4 times within an hour" in block["detail"]
    for needle in ("auto-swap up to 35B IQ4_XS", "a GPU left", "engine started", "a GPU joined", "held until"):
        assert needle in block["detail"]
    assert "50052" not in block["detail"] and "127.0.0.1" not in block["detail"]


# ======================================================================================
# The settings (engine/config.py)
# ======================================================================================

def _limits_from(isolated_config, text):
    isolated_config.write_text(text, encoding="utf-8")
    engine_config._cached_stamp = None
    return engine_config.get_worker_limits()


def test_the_breaker_defaults_to_three_rebuilds_an_hour_held_for_an_hour(isolated_config):
    limits = engine_config.get_worker_limits()
    assert limits["auto_swap_max_rebuilds_per_hour"] == 3
    assert limits["auto_swap_flap_hold_seconds"] == 3600


@pytest.mark.parametrize("raw, expected", [("0", 0), ("1", 1), ("3", 3), ("10", 10), ("100", 100),
                                           ("101", 3), ("-1", 3), ("2.5", 3), ("many", 3), ("", 3)])
def test_the_rebuild_limit_is_range_checked_and_zero_means_off(isolated_config, raw, expected):
    assert _limits_from(isolated_config, f"AUTO_SWAP_MAX_REBUILDS_PER_HOUR={raw}\n")[
        "auto_swap_max_rebuilds_per_hour"] == expected


@pytest.mark.parametrize("raw, expected", [("300", 300), ("1800", 1800), ("3600", 3600), ("604800", 604800),
                                           ("299", 3600), ("0", 3600), ("604801", 3600), ("-5", 3600),
                                           ("an hour", 3600), ("", 3600)])
def test_the_hold_is_range_checked(isolated_config, raw, expected):
    assert _limits_from(isolated_config, f"AUTO_SWAP_FLAP_HOLD_SECONDS={raw}\n")[
        "auto_swap_flap_hold_seconds"] == expected


def test_the_two_keys_are_independent_of_each_other_and_of_their_neighbours(isolated_config):
    limits = _limits_from(isolated_config, "AUTO_SWAP_MAX_REBUILDS_PER_HOUR=5\nAUTO_SWAP_COOLDOWN_SECONDS=900\n")
    assert limits["auto_swap_max_rebuilds_per_hour"] == 5 and limits["auto_swap_flap_hold_seconds"] == 3600
    assert limits["auto_swap_cooldown_seconds"] == 900
