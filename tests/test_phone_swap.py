"""
The phone ladder's switcher (engine/phone_swap.py) against a fake clock, fake volunteers and a fake engine: the coding
switcher's safeguards for the phones (stable before up, RAM counted late, near volunteers only, speed check, never mid-answer,
cooldown, flap breaker, at once down) and that coding wins a contest for a volunteer. No server, no sleep.
"""

import asyncio

import pytest

from engine import phone_swap
from engine.config import PHONE_ENGINE_DEFAULTS
from engine.phone_engine import phone_rungs
from engine.phone_swap import PhoneSwapController, eligible_volunteers
from tests.phone_fakes import FakeClock, FakeEngine

UP = 600
COOLDOWN = 1800


def gpu(vid, gb, rtt=20.0, **over):
    v = {"id": vid, "kind": "gpu", "vram_gb": gb, "endpoint": f"10.0.0.{len(vid) + 1}:{50000 + sum(map(ord, vid)) % 1000}",
         "rtt_ms": rtt, "rtt_age_s": 10.0, "present_s": 99999.0}
    v.update(over)
    return v


def cpu(vid, gb, present=99999.0, rtt=20.0):
    return {"id": vid, "kind": "cpu", "ram_lent_gb": gb, "endpoint": f"10.0.1.{len(vid) + 1}:{51000 + sum(map(ord, vid)) % 1000}",
            "rtt_ms": rtt, "rtt_age_s": 10.0, "present_s": present}


class Rig:
    def __init__(self, **settings):
        self.clock = FakeClock()
        self.engine = FakeEngine(self.clock)
        self.volunteers = []
        self.claims = set()
        self.ready = True
        self.logs = []
        self.settings = dict(PHONE_ENGINE_DEFAULTS, phone_engine="own")
        self.settings.update(settings)
        self.engine.is_own = self.settings["phone_engine"] == "own"
        self.downloads = []
        self.ctl = PhoneSwapController(
            engine=self.engine, rungs_provider=phone_rungs, volunteers_provider=lambda: self.volunteers,
            settings=lambda: self.settings, coding_claims=lambda: self.claims, tier_ready=self._tier_ready,
            clock=self.clock, log=self.logs.append, warn=self.logs.append)

    async def _tier_ready(self, rung):
        return self.ready

    async def tick(self, advance=0.0):
        self.clock.advance(advance)
        return await asyncio.wait_for(self.ctl.tick(), 10)

    async def up_to_base(self):
        await self.tick()
        assert self.engine.loaded and self.engine.rung["id"] == "qwen35-4b"

    async def settle(self):
        """Tick until the up-switch has had its stable time (and the cooldown from the last switch)."""
        d = await self.tick()
        d = await self.tick(UP + 1)
        return d


@pytest.fixture(autouse=True)
def no_takeover(monkeypatch):
    monkeypatch.setattr(phone_swap, "_takeover_in_progress", lambda: False)


@pytest.mark.anyio
async def test_shared_mode_does_nothing():
    rig = Rig(phone_engine="shared")
    d = await rig.tick()
    assert d.state == "off" and d.reason == "shared" and rig.engine.calls == []


@pytest.mark.anyio
async def test_a_stopped_engine_is_started_on_the_base_alone_and_without_a_speed_check_or_a_cooldown():
    rig = Rig()
    await rig.up_to_base()
    assert rig.engine.calls == [{"rung": "qwen35-4b", "volunteers": [], "abort_if_busy": False, "check_speed": False,
                                 "restore": "qwen35-4b", "min_tps": None}]
    rig.volunteers = [gpu("a", 8)]
    await rig.tick()
    d = await rig.tick(UP + 1)                 # the boot start does not start a cooldown
    assert d.reason == "switched" and rig.engine.rung["id"] == "qwen35-9b"


@pytest.mark.anyio
async def test_an_engine_that_cannot_start_is_not_hammered():
    rig = Rig()
    rig.engine.unavailable = "llama-server is not installed"
    d = await rig.tick()
    assert d.reason == "unavailable" and rig.engine.calls == []


@pytest.mark.anyio
async def test_a_crashed_engine_is_started_again_on_the_base():
    rig = Rig()
    await rig.up_to_base()
    rig.engine.loaded = False
    await rig.tick(30)
    assert len(rig.engine.calls) == 2 and rig.engine.calls[1]["rung"] == "qwen35-4b"


@pytest.mark.anyio
async def test_it_moves_up_only_when_the_volunteers_share_has_held_for_the_stable_time():
    rig = Rig()
    await rig.up_to_base()
    rig.volunteers = [gpu("a", 8)]
    d = await rig.tick()
    assert d.state == "waiting" and d.reason == "settling" and d.target == "qwen35-9b"
    d = await rig.tick(UP - 5)
    assert d.reason == "settling" and len(rig.engine.calls) == 1
    d = await rig.tick(10)
    assert d.reason == "switched"
    call = rig.engine.calls[-1]
    assert call["rung"] == "qwen35-9b" and call["volunteers"] == ["a"] and call["check_speed"] and call["abort_if_busy"]
    assert call["restore"] == "qwen35-4b" and call["min_tps"] == 6.0


@pytest.mark.anyio
async def test_a_volunteer_who_comes_and_goes_changes_nothing():
    rig = Rig()
    await rig.up_to_base()
    rig.volunteers = [gpu("a", 8)]
    await rig.tick()
    await rig.tick(400)
    rig.volunteers = []
    await rig.tick(1)
    rig.volunteers = [gpu("a", 8)]
    d = await rig.tick(1)                       # back, but the clock starts again
    assert d.reason == "settling"
    d = await rig.tick(UP - 5)
    assert d.reason == "settling" and len(rig.engine.calls) == 1


@pytest.mark.anyio
async def test_lent_ram_counts_only_after_a_30_minute_stay():
    rig = Rig()
    await rig.up_to_base()
    rig.volunteers = [cpu("c", 16, present=600)]
    d = await rig.tick(UP + 100)
    assert d.reason == "no-volunteers" and len(rig.engine.calls) == 1
    rig.volunteers = [cpu("c", 16, present=1801)]
    d = await rig.tick()
    assert d.reason == "settling"
    d = await rig.tick(UP + 1)
    assert d.reason == "switched" and rig.engine.calls[-1]["volunteers"] == ["c"]


@pytest.mark.anyio
async def test_only_near_measured_volunteers_count_with_the_phone_limit_of_200_ms():
    rig = Rig()
    await rig.up_to_base()
    for far in (gpu("a", 8, rtt=250.0), gpu("a", 8, rtt_ms=None), gpu("a", 8, rtt_age_s=5000.0), gpu("a", 24, endpoint="bad;endpoint")):
        rig.volunteers = [far]
        d = await rig.tick(UP + 1)
        assert d.reason == "no-volunteers"
    rig.volunteers = [gpu("a", 8, rtt=180.0)]          # past the coding pool's 5 ms/80 ms, inside the phones' 200 ms
    await rig.tick()
    d = await rig.tick(UP + 1)
    assert d.reason == "switched"
    rig2 = Rig(phone_shard_max_rtt_ms=100.0)
    await rig2.up_to_base()
    rig2.volunteers = [gpu("a", 8, rtt=180.0)]
    assert (await rig2.tick(UP + 1)).reason == "no-volunteers"


@pytest.mark.anyio
async def test_it_steps_down_at_once_when_a_volunteer_leaves_even_inside_the_cooldown():
    rig = Rig()
    await rig.up_to_base()
    rig.volunteers = [gpu("a", 8)]
    await rig.settle()
    assert rig.engine.rung["id"] == "qwen35-9b"
    rig.volunteers = []
    d = await rig.tick(1)                       # one second after the switch: inside the cooldown
    assert d.reason == "switched" and rig.engine.rung["id"] == "qwen35-4b"
    call = rig.engine.calls[-1]
    assert call["volunteers"] == [] and not call["check_speed"] and not call["abort_if_busy"]


@pytest.mark.anyio
async def test_it_steps_down_to_the_rung_the_rest_still_carries():
    rig = Rig()
    await rig.up_to_base()
    rig.volunteers = [gpu("a", 48), gpu("bb", 24)]
    await rig.settle()
    assert rig.engine.rung["id"] == "qwen36-a3b"
    rig.volunteers = [gpu("a", 48)]
    rig.engine.volunteer_ids = ["a", "bb"]
    await rig.tick(1)
    assert rig.engine.rung["id"] == "qwen36-a3b" or rig.engine.calls[-1]["rung"] in ("qwen36-a3b", "qwen35-9b")
    rig.volunteers = [gpu("a", 8)]
    await rig.tick(1)
    assert rig.engine.calls[-1]["rung"] == "qwen35-9b" and rig.engine.calls[-1]["volunteers"] == ["a"]
    rig.volunteers = []
    await rig.tick(1)
    assert rig.engine.calls[-1]["rung"] == "qwen35-4b"


@pytest.mark.anyio
async def test_coding_wins_a_contest_for_a_volunteer_both_ways():
    rig = Rig()
    await rig.up_to_base()
    rig.volunteers = [gpu("a", 8)]
    rig.claims = {"a"}
    d = await rig.tick(UP + 1)
    assert d.reason == "no-volunteers" and len(rig.engine.calls) == 1        # never taken
    rig.claims = set()
    await rig.settle()
    assert rig.engine.rung["id"] == "qwen35-9b"
    rig.claims = {"a"}                                                       # coding wants it back
    await rig.tick(1)
    assert rig.engine.rung["id"] == "qwen35-4b"


@pytest.mark.anyio
async def test_a_rung_too_slow_to_use_is_held_off_with_a_growing_hold_and_the_base_is_back():
    rig = Rig()
    await rig.up_to_base()
    rig.volunteers = [gpu("a", 8)]
    rig.engine.outcome = "failed"
    await rig.tick()
    d = await rig.tick(UP + 1)
    assert d.reason == "switch-failed" and rig.engine.rung["id"] == "qwen35-4b"
    n = len(rig.engine.calls)
    for _ in range(3):
        await rig.tick(COOLDOWN + 1)
    assert len(rig.engine.calls) == n                                         # held off for hours
    await rig.tick(6 * 3600)
    d = await rig.tick(UP + 1)
    assert len(rig.engine.calls) > n


@pytest.mark.anyio
async def test_it_never_cuts_an_answer_to_move_up():
    rig = Rig()
    await rig.up_to_base()
    rig.volunteers = [gpu("a", 8)]
    await rig.tick()
    rig.engine.in_flight = 1
    d = await rig.tick(UP + 1)
    assert d.reason == "queue-busy" and len(rig.engine.calls) == 1
    d = await rig.tick(901)                       # a bounded wait, then a drain that gives up if the answer does not end
    assert rig.engine.calls[-1]["abort_if_busy"] is True and rig.engine.calls[-1]["rung"] == "qwen35-9b"
    rig2 = Rig()
    await rig2.up_to_base()
    rig2.volunteers = [gpu("a", 8)]
    rig2.engine.outcome = "busy"
    await rig2.tick()
    d = await rig2.tick(UP + 1)
    assert d.reason == "queue-busy" and rig2.engine.rung["id"] == "qwen35-4b"


@pytest.mark.anyio
async def test_a_cooldown_separates_two_moves_up():
    rig = Rig()
    await rig.up_to_base()
    rig.volunteers = [gpu("a", 8)]
    await rig.settle()
    assert rig.engine.rung["id"] == "qwen35-9b"
    rig.volunteers = [gpu("a", 48), gpu("bb", 24)]
    await rig.tick()
    d = await rig.tick(UP + 1)
    assert d.reason == "cooldown" and rig.engine.rung["id"] == "qwen35-9b"
    d = await rig.tick(COOLDOWN)
    assert d.reason == "switched" and rig.engine.rung["id"] == "qwen36-a3b"


@pytest.mark.anyio
async def test_the_flap_breaker_holds_moves_up_but_never_a_step_down():
    rig = Rig()
    await rig.up_to_base()
    for i in range(4):
        rig.ctl._breaker.record(f"rebuild {i}")
    rig.volunteers = [gpu("a", 8)]
    await rig.tick()
    d = await rig.tick(UP + 1)
    assert d.state == "held" and d.reason == "flap-hold" and len(rig.engine.calls) == 1
    assert rig.ctl.status()["state"] == "held"
    rig.engine.loaded, rig.engine.rung, rig.engine.volunteer_ids = True, phone_rungs()[1], ["a"]
    rig.volunteers = []
    await rig.tick(1)
    assert rig.engine.calls[-1]["rung"] == "qwen35-4b"                       # the step down went through


@pytest.mark.anyio
async def test_a_rung_not_on_disk_is_fetched_first_or_passed_over():
    rig = Rig()
    await rig.up_to_base()
    rig.ready = False
    rig.volunteers = [gpu("a", 8)]
    d = await rig.tick(UP + 1)
    assert d.reason == "not-on-disk"
    fetched = []

    async def download(rung):
        fetched.append(rung["id"])
        rig.ready = True
        return True
    rig.ctl._download = download
    d = await rig.tick()
    assert d.reason == "downloading"
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await rig.tick()
    d = await rig.tick(UP + 1)
    assert fetched == ["qwen35-9b"] and d.reason == "switched"


@pytest.mark.anyio
async def test_phone_model_names_the_base_and_the_ladder_starts_there():
    rig = Rig(phone_model="qwen35-9b")
    rig.engine.base_id = "qwen35-9b"
    await rig.tick()
    assert rig.engine.calls[0]["rung"] == "qwen35-9b" and rig.engine.calls[0]["restore"] == "qwen35-9b"
    rig.volunteers = [gpu("a", 64)]
    await rig.tick()
    await rig.tick(UP + 1)
    assert rig.engine.calls[-1]["rung"] == "qwen36-a3b" and rig.engine.calls[-1]["restore"] == "qwen35-9b"


@pytest.mark.anyio
async def test_off_and_takeover_and_status(monkeypatch):
    rig = Rig()
    await rig.up_to_base()
    rig.settings["phone_auto_swap"] = False
    rig.volunteers = [gpu("a", 8)]
    d = await rig.tick(UP + 1)
    assert d.state == "off" and rig.ctl.status()["enabled"] is False and len(rig.engine.calls) == 1
    rig.settings["phone_auto_swap"] = True
    monkeypatch.setattr(phone_swap, "_takeover_in_progress", lambda: True)
    assert (await rig.tick(UP + 1)).reason == "takeover"
    monkeypatch.setattr(phone_swap, "_takeover_in_progress", lambda: False)
    status = rig.ctl.status()
    assert set(status) == {"enabled", "state", "target", "detail"}


@pytest.mark.anyio
async def test_the_loop_survives_a_failed_tick_and_stops_when_cancelled():
    rig = Rig()
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("provider broke")
    rig.ctl._rungs_provider = boom
    rig.ctl._interval = 0.01
    task = asyncio.ensure_future(rig.ctl.run())
    await asyncio.sleep(0.15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert len(calls) >= 2


def test_eligibility_is_a_pure_function_of_distance_age_and_claims():
    vs = [gpu("a", 8), gpu("b", 8, rtt=300.0), cpu("c", 8, present=10), cpu("d", 8, present=2000), gpu("e", 8)]
    out = eligible_volunteers(vs, max_rtt_ms=200.0, ram_settle_seconds=1800.0, claimed={"e"})
    assert [v["id"] for v in out] == ["a", "d"]
