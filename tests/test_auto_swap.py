"""
Automatic model switching for the GPU coding pool (engine/auto_swap.py).

The controller's decisions are tested with a fake clock, fake tiers, a fake pool and a fake
switcher: no engine, no registry, no disk. The second half checks the coordinator's side:
the interim ladder, the shared switch path behind POST /v1/cluster/swap (unchanged for the
endpoint; verified and rolled back for automatic switches), the status field and the ladder
alias on the chat and completion paths. Engines there are fakes too.
"""

import asyncio
import os
import threading
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

import engine.config as engine_config
from engine import app_pool, auto_swap, coordinator, downloader
from engine.auto_swap import AutoSwapController, SwapResult
from engine.lease import lease_manager
from tests.download_fakes import FakeModelServer, make_blob

T0 = 10_000.0
SHA = "ab" * 32


def tier(tid, gb, enabled=True, **extra):
    entry = {
        "id": tid, "name": f"{gb} GB tier", "file": f"{tid}.gguf",
        "url": f"https://models.example/{tid}.gguf", "size": 1_000_000, "sha256": SHA,
        "min_vram_gb": float(gb), "context": 65536, "enabled": enabled,
    }
    entry.update(extra)
    return entry


def ladder():
    return [tier("t16", 16), tier("t24", 24), tier("t32", 32), tier("t48", 48, enabled=False)]


class Rig:
    """A controller wired to fakes. ``now`` is the clock; ``vram`` the pool."""

    def __init__(self, *, tiers=None, vram=16.0, far=0.0, current="t16.gguf",
                 ready=("t16", "t24", "t32", "t48"), settings=None):
        self.now = T0
        self.tiers = ladder() if tiers is None else tiers
        self.vram = vram            # near GPUs
        self.far = far              # GPUs too far away to count
        self.current = current
        self.ready = set(ready)
        self.settings = {"auto_swap": True, "auto_swap_up_stable_seconds": 600,
                         "auto_swap_down_stable_seconds": 120, "auto_swap_cooldown_seconds": 1800}
        self.settings.update(settings or {})
        self.swaps = []          # (tier id, abort_if_busy, time)
        self.speed_checked = []  # (tier id, check_speed)
        self.result = "swapped"
        self.in_progress = False
        self.busy = False
        self.downloads = []
        self.gate = None         # an asyncio.Event holds downloads until set
        self.download_ok = True
        self.logs = []
        self.engine_short = False   # the engine already runs without the GPUs that left
        self.controller = AutoSwapController(
            tiers_provider=lambda: [dict(t) for t in self.tiers],
            pool_provider=lambda: {"vram_gb": self.vram, "far_vram_gb": self.far, "sysram_gb": 12.0},
            current_model=lambda: self.current,
            swap=self._swap,
            tier_ready=self._ready,
            download=self._download,
            settings=lambda: dict(self.settings),
            swap_in_progress=lambda: self.in_progress,
            queue_busy=lambda: self.busy,
            clock=lambda: self.now,
            log=self.logs.append,
            engine_short=lambda: self.engine_short,
        )

    async def _swap(self, tier, *, drain_seconds, abort_if_busy, check_speed=False):
        self.swaps.append((tier["id"], abort_if_busy, self.now))
        self.speed_checked.append((tier["id"], check_speed))
        if self.result == "swapped":
            self.current = tier["file"]
        return SwapResult(self.result, "from the test")

    async def _ready(self, tier):
        return tier["id"] in self.ready

    async def _download(self, tier):
        self.downloads.append(tier["id"])
        if self.gate is not None:
            await self.gate.wait()
        if self.download_ok:
            self.ready.add(tier["id"])
        return self.download_ok

    async def at(self, t):
        """Tick at clock time ``t``, then let a background download run."""
        self.now = t
        decision = await self.controller.tick()
        for _ in range(3):
            await asyncio.sleep(0)
        return decision

    async def every(self, start, end, step=30.0):
        """Tick every ``step`` seconds from ``start`` to ``end`` inclusive."""
        decision = None
        t = start
        while t <= end:
            decision = await self.at(t)
            t += step
        return decision


# ======================================================================================
# The controller
# ======================================================================================

@pytest.mark.anyio
async def test_a_brief_vram_spike_never_swaps():
    rig = Rig()
    await rig.at(T0)
    rig.vram = 24
    spike = await rig.every(T0 + 30, T0 + 330)            # five minutes at 24 GB
    assert spike.state == "waiting" and spike.target == "t24"
    rig.vram = 16
    await rig.every(T0 + 360, T0 + 3600)
    assert rig.swaps == []


@pytest.mark.anyio
async def test_a_pool_that_stays_big_enough_swaps_once():
    rig = Rig(vram=24)
    await rig.every(T0, T0 + 570)
    assert rig.swaps == []
    await rig.at(T0 + 600)
    assert rig.swaps == [("t24", True, T0 + 600)]
    assert rig.current == "t24.gguf"
    await rig.every(T0 + 630, T0 + 7200)
    assert len(rig.swaps) == 1
    assert rig.controller.status()["state"] == "steady"


@pytest.mark.anyio
async def test_the_cooldown_is_respected_between_up_swaps():
    rig = Rig(vram=24)
    await rig.every(T0, T0 + 600)                         # up to t24 at T0+600
    rig.vram = 32
    await rig.every(T0 + 630, T0 + 1260)                  # t32 stable from T0+1230
    status = rig.controller.status()
    assert status["state"] == "cooldown" and status["target"] == "t32"
    await rig.every(T0 + 1290, T0 + 2370)
    assert [s[0] for s in rig.swaps] == ["t24"]
    await rig.at(T0 + 2400)                               # 1800 s after the last switch
    assert [s[0] for s in rig.swaps] == ["t24", "t32"]


@pytest.mark.anyio
async def test_a_down_swap_ignores_the_cooldown():
    rig = Rig(vram=24)
    await rig.every(T0, T0 + 600)                         # up to t24 at T0+600
    rig.vram = 16                                         # the volunteer leaves
    short = await rig.every(T0 + 630, T0 + 720)
    assert short.state == "waiting" and short.target == "t16"
    assert len(rig.swaps) == 1
    await rig.at(T0 + 750)                                # 120 s short, long before the cooldown ends
    assert rig.swaps[-1] == ("t16", False, T0 + 750)      # down-switches never abort on a busy queue
    assert rig.current == "t16.gguf"


@pytest.mark.anyio
async def test_an_engine_already_rebuilt_without_the_gpu_steps_down_at_once():
    """2026-10-27: the laptop's shard went, the engine was rebuilt on 16 GB at once, and IQ4_XS spilled for the
    two-minute grace that could no longer save anything."""
    rig = Rig(vram=24)
    await rig.every(T0, T0 + 600)                         # up to t24
    rig.vram = 16
    rig.engine_short = True
    await rig.at(T0 + 630)
    assert rig.swaps[-1] == ("t16", False, T0 + 630) and rig.current == "t16.gguf"


@pytest.mark.anyio
async def test_a_pool_that_recovers_in_time_does_not_down_swap():
    rig = Rig(vram=24)
    await rig.every(T0, T0 + 600)
    rig.vram = 16
    await rig.every(T0 + 630, T0 + 720)
    rig.vram = 24
    await rig.every(T0 + 750, T0 + 1200)
    assert [s[0] for s in rig.swaps] == ["t24"]


@pytest.mark.anyio
async def test_a_model_that_is_not_downloaded_is_fetched_first_and_swapped_only_when_ready():
    rig = Rig(vram=24, ready=("t16",))
    rig.gate = asyncio.Event()
    first = await rig.at(T0)
    assert rig.downloads == ["t24"]                       # the download starts as soon as it fits
    assert first.state == "downloading" and first.target == "t24"
    waiting = await rig.every(T0 + 30, T0 + 900)          # long past the stable period
    assert rig.swaps == []
    assert rig.downloads == ["t24"]                       # one download, not one per tick
    assert waiting.state == "downloading"
    rig.gate.set()
    for _ in range(3):
        await asyncio.sleep(0)
    await rig.at(T0 + 930)
    assert rig.swaps == [("t24", True, T0 + 930)]


@pytest.mark.anyio
async def test_a_failed_download_is_retried_later_and_never_swapped_to():
    rig = Rig(vram=24, ready=("t16",))
    rig.download_ok = False
    await rig.at(T0)
    failed = await rig.at(T0 + 30)
    assert failed.state == "waiting" and failed.reason == "download-failed"
    await rig.every(T0 + 60, T0 + 3600)
    assert rig.downloads == ["t24"]
    await rig.at(T0 + 3630)                               # DOWNLOAD_RETRY_SECONDS later
    assert rig.downloads == ["t24", "t24"]
    assert rig.swaps == []


@pytest.mark.anyio
async def test_a_manual_swap_holds_the_controller_off():
    rig = Rig(vram=24)
    await rig.at(T0)
    rig.controller.note_manual_swap("t16.gguf")
    held = await rig.every(T0 + 30, T0 + 900)
    assert held.state == "cooldown" and held.reason == "manual-hold"
    await rig.every(T0 + 930, T0 + 1770)
    assert rig.swaps == []
    await rig.at(T0 + 1800)
    assert [s[0] for s in rig.swaps] == ["t24"]


@pytest.mark.anyio
async def test_a_model_change_made_elsewhere_counts_as_a_manual_swap():
    rig = Rig(vram=24)
    await rig.at(T0)
    rig.current = "something-else.gguf"                   # the WebUI vote, say
    off_ladder = await rig.at(T0 + 30)
    assert off_ladder.state == "steady" and off_ladder.reason == "off-ladder"
    rig.current = "t16.gguf"                              # and back again
    await rig.every(T0 + 60, T0 + 1830)
    assert rig.swaps == []
    await rig.at(T0 + 1860)
    assert [s[0] for s in rig.swaps] == ["t24"]


@pytest.mark.anyio
async def test_auto_swap_false_does_nothing():
    rig = Rig(vram=48, ready=("t16",), settings={"auto_swap": False})
    await rig.every(T0, T0 + 7200)
    assert rig.swaps == [] and rig.downloads == []
    assert rig.controller.status() == {"enabled": False, "state": "off", "target": None,
                                       "detail": "Automatic model switching is off."}


@pytest.mark.anyio
async def test_turning_auto_swap_off_cancels_a_download():
    rig = Rig(vram=24, ready=("t16",))
    rig.gate = asyncio.Event()
    await rig.at(T0)
    assert rig.downloads == ["t24"]
    rig.settings["auto_swap"] = False
    off = await rig.at(T0 + 30)
    assert off.state == "off"
    rig.gate.set()
    for _ in range(3):
        await asyncio.sleep(0)
    assert "t24" not in rig.ready                          # the cancelled download never finished
    assert rig.swaps == []


@pytest.mark.anyio
async def test_disabled_tiers_are_never_targets():
    rig = Rig(vram=64)                                    # t48 would fit, but it is disabled
    await rig.every(T0, T0 + 7200)
    assert [s[0] for s in rig.swaps] == ["t32"]


@pytest.mark.anyio
async def test_only_a_literal_true_enables_a_tier():
    tiers = [tier("t16", 16), tier("t24", 24, enabled="true"), tier("t32", 32, enabled=1)]
    rig = Rig(tiers=tiers, vram=64, ready=("t16",))
    final = await rig.every(T0, T0 + 3600)
    assert rig.swaps == [] and rig.downloads == []
    assert final.state == "steady"


@pytest.mark.anyio
async def test_nothing_happens_while_a_swap_is_in_progress():
    rig = Rig(vram=24)
    rig.in_progress = True
    await rig.every(T0, T0 + 1200)
    assert rig.swaps == []
    assert rig.controller.status()["state"] == "swapping"
    rig.in_progress = False
    await rig.at(T0 + 1230)
    assert [s[0] for s in rig.swaps] == ["t24"]


@pytest.mark.anyio
async def test_no_down_swap_while_a_swap_is_in_progress():
    rig = Rig(vram=8, current="t24.gguf")
    rig.in_progress = True
    await rig.every(T0, T0 + 600)
    assert rig.swaps == []


@pytest.mark.anyio
async def test_a_busy_queue_is_waited_for_then_drained():
    rig = Rig(vram=24)
    rig.busy = True
    waiting = await rig.every(T0, T0 + 1470)
    assert rig.swaps == []
    assert waiting.state == "waiting" and waiting.reason == "queue-busy"
    await rig.at(T0 + 1500)                               # busy since T0+600, for QUEUE_WAIT_SECONDS
    assert rig.swaps == [("t24", True, T0 + 1500)]        # drained, and called off if it does not drain


@pytest.mark.anyio
async def test_an_idle_moment_is_used_straight_away():
    rig = Rig(vram=24)
    rig.busy = True
    await rig.every(T0, T0 + 690)
    rig.busy = False
    await rig.at(T0 + 720)
    assert rig.swaps == [("t24", True, T0 + 720)]


@pytest.mark.anyio
async def test_a_queue_that_would_not_drain_restarts_the_wait():
    rig = Rig(vram=24)
    rig.busy = True
    rig.result = "busy"
    await rig.every(T0, T0 + 1500)
    assert len(rig.swaps) == 1
    await rig.every(T0 + 1530, T0 + 2370)
    assert len(rig.swaps) == 1
    await rig.at(T0 + 2400)                               # another full wait later
    assert len(rig.swaps) == 2


@pytest.mark.anyio
async def test_a_failed_switch_is_not_retried_for_hours():
    rig = Rig(vram=24)
    rig.result = "failed"
    await rig.every(T0, T0 + 600)
    assert len(rig.swaps) == 1
    rig.result = "swapped"
    hold_end = T0 + 600 + auto_swap.FAILED_TIER_HOLD_SECONDS
    await rig.every(T0 + 900, hold_end - 300, step=300.0)
    assert len(rig.swaps) == 1
    await rig.at(hold_end)
    assert len(rig.swaps) == 2 and rig.current == "t24.gguf"


@pytest.mark.anyio
async def test_a_tier_that_keeps_failing_is_held_off_longer_each_time():
    rig = Rig(vram=24)
    rig.result = "failed"
    await rig.every(T0, T0 + 600)
    first_retry = T0 + 600 + auto_swap.FAILED_TIER_HOLD_SECONDS
    await rig.at(first_retry)
    assert len(rig.swaps) == 2                            # second failure: the hold doubles
    await rig.at(first_retry + auto_swap.FAILED_TIER_HOLD_SECONDS)
    assert len(rig.swaps) == 2
    await rig.at(first_retry + 2 * auto_swap.FAILED_TIER_HOLD_SECONDS)
    assert len(rig.swaps) == 3


@pytest.mark.anyio
async def test_a_superseded_switch_holds_off_like_a_manual_one():
    rig = Rig(vram=32)
    rig.result = "superseded"
    await rig.every(T0, T0 + 600)
    assert len(rig.swaps) == 1
    rig.result = "swapped"
    await rig.every(T0 + 630, T0 + 2370)
    assert len(rig.swaps) == 1
    await rig.at(T0 + 2400)
    assert len(rig.swaps) == 2


@pytest.mark.anyio
async def test_with_nothing_smaller_that_fits_it_stays_put():
    rig = Rig(vram=8, current="t24.gguf")
    final = await rig.every(T0, T0 + 600)
    assert rig.swaps == []
    assert final.reason == "no-fallback"
    assert any("staying put" in line for line in rig.logs)


@pytest.mark.anyio
async def test_a_pool_with_no_gpus_never_triggers_a_switch():
    tiers = [tier("t0", 0), tier("t16", 16), tier("t24", 24)]
    rig = Rig(tiers=tiers, vram=0, current="t24.gguf", ready=("t0", "t16", "t24"))
    final = await rig.every(T0, T0 + 1200)
    assert rig.swaps == []
    assert final.reason == "no-gpus"


@pytest.mark.anyio
async def test_a_down_swap_takes_the_biggest_ready_tier_that_fits():
    rig = Rig(vram=24, current="t32.gguf", ready=("t16", "t32"))
    rig.gate = asyncio.Event()                            # t24 fits but is not on disk yet
    await rig.every(T0, T0 + 120)
    assert rig.swaps == [("t16", False, T0 + 120)]
    assert rig.downloads == ["t24"]                       # and the better fit is being fetched


def split_parts(count=3, **changes):
    """A split model's ``files``, in the ladder's shape; ``changes`` apply to the last part."""
    parts = [{"file": f"t64-0000{i}-of-0000{count}.gguf", "size": 10, "sha256": SHA,
              "url": f"https://models.example/t64-0000{i}-of-0000{count}.gguf"} for i in range(1, count + 1)]
    parts[-1].update(changes)
    return parts


@pytest.mark.anyio
async def test_a_split_model_is_downloaded_like_any_other_and_switched_to_when_ready():
    parts = [{"path": f"t64-0000{i}-of-00003.gguf", "size": 10, "sha256": SHA,
              "hf_url": f"https://models.example/t64-0000{i}-of-00003.gguf"} for i in (1, 2, 3)]
    tiers = ladder()[:3] + [tier("t64", 64, files=parts, file="t64-00001-of-00003.gguf")]
    rig = Rig(tiers=tiers, vram=64, current="t32.gguf", ready=("t16", "t24", "t32"))
    rig.gate = asyncio.Event()
    first = await rig.at(T0)
    assert rig.downloads == ["t64"]                       # fetched as soon as it fits
    assert first.state == "downloading" and first.target == "t64"
    assert "3 files" in first.detail
    waiting = await rig.every(T0 + 30, T0 + 900)          # long past the stable period
    assert rig.downloads == ["t64"] and rig.swaps == []   # one download, and no switch before it is whole
    assert waiting.state == "downloading"
    rig.gate.set()
    for _ in range(3):
        await asyncio.sleep(0)
    await rig.at(T0 + 930)
    assert rig.swaps == [("t64", True, T0 + 930)]


@pytest.mark.anyio
async def test_a_split_model_that_cannot_be_downloaded_is_left_alone_and_does_not_block_smaller_tiers():
    parts = split_parts(url="http://models.example/t64-00003-of-00003.gguf")      # one part is not https
    tiers = ladder()[:3] + [tier("t64", 64, files=parts, file=parts[0]["file"])]
    rig = Rig(tiers=tiers, vram=64, ready=("t16", "t24", "t32"))
    await rig.every(T0, T0 + 1200)
    assert "t64" not in rig.downloads
    assert [s[0] for s in rig.swaps] == ["t32"]


@pytest.mark.anyio
async def test_the_status_names_what_keeps_a_split_model_from_being_downloaded():
    parts = split_parts(sha256="")
    tiers = [tier("t16", 16), tier("t64", 64, files=parts, file=parts[0]["file"])]
    rig = Rig(tiers=tiers, vram=64, ready=("t16",))
    decision = await rig.at(T0)
    assert decision.reason == "top-blocked"
    assert "1 of its 3 files have no pinned download" in decision.detail
    assert rig.downloads == []


@pytest.mark.anyio
async def test_the_download_progress_reaches_the_status_line():
    parts = split_parts()
    tiers = [tier("t16", 16), tier("t64", 64, files=parts, file=parts[0]["file"])]
    rig = Rig(tiers=tiers, vram=64, ready=("t16",))
    rig.gate = asyncio.Event()
    await rig.at(T0)
    rig.controller.note_download_progress("t64", 4_000_000_000, 8_000_000_000)
    decision = await rig.at(T0 + 30)
    assert decision.state == "downloading" and "4 of 8 GB, 3 files" in decision.detail
    rig.controller.note_download_progress("other-tier", 1, 2)                 # not this download: ignored
    assert "4 of 8 GB" not in (await rig.at(T0 + 60)).detail
    rig.gate.set()
    for _ in range(3):
        await asyncio.sleep(0)
    await rig.at(T0 + 90)                                                     # booked: progress forgotten
    assert rig.controller._download_progress is None


@pytest.mark.anyio
async def test_a_split_model_put_in_place_by_hand_can_be_used():
    parts = [{"path": f"t64-0000{i}-of-00003.gguf", "size": 10, "sha256": SHA} for i in (1, 2, 3)]
    tiers = ladder()[:3] + [tier("t64", 64, files=parts)]
    rig = Rig(tiers=tiers, vram=64, ready=("t16", "t24", "t32", "t64"))
    await rig.every(T0, T0 + 600)
    assert [s[0] for s in rig.swaps] == ["t64"]


@pytest.mark.anyio
async def test_the_status_says_when_the_switch_will_happen():
    rig = Rig(vram=24)
    await rig.at(T0)
    await rig.at(T0 + 240)
    status = rig.controller.status()
    assert set(status) == {"enabled", "state", "target", "detail"}
    assert status["enabled"] is True and status["state"] in auto_swap.STATES
    assert status["target"] == "t24"
    assert "Switching to 24 GB tier in 6 min" in status["detail"]


@pytest.mark.anyio
async def test_one_log_line_per_decision_change():
    rig = Rig(vram=24)
    await rig.every(T0, T0 + 570)                         # 20 ticks, one decision
    assert len(rig.logs) == 1


@pytest.mark.anyio
async def test_a_broken_ladder_or_a_failed_tick_does_not_stop_the_loop():
    rig = Rig(vram=24)

    def broken():
        raise ValueError("bad json")

    rig.controller._tiers_provider = broken
    decision = await rig.at(T0)
    assert decision.state == "steady" and decision.reason == "ladder-error"
    assert rig.swaps == []


@pytest.mark.anyio
async def test_serve_as_maps_other_rungs_to_the_served_one():
    rig = Rig(vram=24, current="t24.gguf")
    await rig.at(T0)
    serve_as = rig.controller.serve_as
    assert serve_as("t16.gguf", "t24.gguf") == "t24.gguf"
    assert serve_as("t16", "t24.gguf") == "t24.gguf"
    assert serve_as("t24.gguf", "t24.gguf") == "t24.gguf"
    assert serve_as("phi3-mini.gguf", "t24.gguf") == "phi3-mini.gguf"   # not on the ladder
    assert serve_as("t48.gguf", "t24.gguf") == "t48.gguf"               # a disabled rung
    assert serve_as("t16.gguf", "other.gguf") == "t16.gguf"             # served model off the ladder
    assert serve_as(None, "t24.gguf") is None
    rig.settings["auto_swap"] = False
    assert serve_as("t16.gguf", "t24.gguf") == "t16.gguf"


# ======================================================================================
# The WAN guard: only near GPUs count
# ======================================================================================

@pytest.mark.anyio
async def test_far_gpus_never_unlock_a_bigger_tier():
    rig = Rig(vram=16, far=32, ready=("t16",))           # 32 GB of volunteers across the internet
    final = await rig.every(T0, T0 + 7200)
    assert rig.swaps == [] and rig.downloads == []
    assert final.reason == "top"
    assert "32 GB more is on GPUs too far away" in rig.controller.status()["detail"]


@pytest.mark.anyio
async def test_losing_the_near_gpu_steps_down_even_when_far_ones_remain():
    rig = Rig(vram=24, current="t24.gguf")
    await rig.at(T0)
    rig.vram, rig.far = 16, 16                            # the LAN volunteer left; a WAN one is still here
    await rig.every(T0 + 30, T0 + 120)
    assert rig.swaps == []
    await rig.at(T0 + 150)
    assert rig.swaps == [("t16", False, T0 + 150)]


@pytest.mark.anyio
async def test_only_far_gpus_left_is_not_mistaken_for_no_gpus():
    rig = Rig(vram=0, far=24, current="t16.gguf")
    final = await rig.every(T0, T0 + 600)
    assert rig.swaps == []
    assert final.reason == "no-fallback"                  # the near pool holds nothing; stay put


def test_split_pool_counts_only_near_gpus():
    workers = [
        {"endpoint": "127.0.0.1:50052", "vram_gb": 16.0, "near": True},
        {"endpoint": "192.168.1.20:50052", "vram_gb": 8.0, "near": True},
        {"endpoint": "203.0.113.7:50052", "vram_gb": 8.0, "near": False},
        {"endpoint": "198.51.100.3:50052", "vram_gb": "lots", "near": True},   # unreadable: skipped
        {"endpoint": "198.51.100.4:50052", "vram_gb": 8.0},                    # is_near raises: far
    ]
    assert auto_swap.split_pool_by_distance(workers, lambda w: w["near"]) == {"vram_gb": 24.0, "far_vram_gb": 16.0}


@pytest.mark.parametrize("rtt, age, expected", [
    (0.6, 10, True), (15.0, 10, True), (15.1, 10, False),        # the 15 ms line
    (0.6, auto_swap.LINK_MEASUREMENT_MAX_AGE_SECONDS + 1, False),  # stale
    (None, 10, False), (0.6, None, False), ("fast", 10, False), (-1, 10, False),   # unknown is far
])
def test_rtt_is_near_needs_a_fresh_measurement_within_the_limit(rtt, age, expected):
    assert auto_swap.rtt_is_near(rtt, age, 15.0) is expected


@pytest.mark.anyio
async def test_the_link_probe_measures_each_volunteer_every_interval_and_files_only_answers():
    clock = [T0]
    asked, filed = [], []
    answers = {"192.168.1.20:50052": 0.7, "203.0.113.7:50052": None}

    def measure(endpoint):
        asked.append(endpoint)
        return answers[endpoint]

    probe = auto_swap.LinkProbe(measure=measure, record=lambda e, rtt: filed.append((e, rtt)),
                                clock=lambda: clock[0], interval=300.0)
    endpoints = list(answers)
    measured = await probe.refresh(endpoints)
    assert measured == {"192.168.1.20:50052": 0.7, "203.0.113.7:50052": None}
    assert filed == [("192.168.1.20:50052", 0.7)]          # an unreachable volunteer files nothing
    clock[0] += 60
    await probe.refresh(endpoints)
    assert len(asked) == 2                                 # not due yet
    clock[0] += 240                                        # 300 s after the first round
    answers["192.168.1.30:50052"] = 1.2                    # and a volunteer that just joined
    await probe.refresh(endpoints + ["192.168.1.30:50052"])
    assert sorted(asked[2:]) == sorted(endpoints + ["192.168.1.30:50052"])


@pytest.fixture
def lan_and_wan(monkeypatch):
    """A registry with this machine's 16 GB, a measured LAN volunteer, and four that do not count."""
    from engine.registry import WorkerRegistry

    registry = WorkerRegistry()
    for worker_id, ip, vram in (("local", "127.0.0.1", 16.0), ("lan", "192.168.1.20", 8.0),
                                ("wan", "203.0.113.7", 8.0), ("stale", "192.168.1.30", 8.0),
                                ("slow", "192.168.1.40", 8.0), ("liar", "198.51.100.9", 8.0)):
        registry.update_worker(worker_id=worker_id, ip=ip, port=50052, token=f"t-{worker_id}",
                               hardware_profile={"gpu": {"has_gpu": True, "vram_total_gb": vram}})
    registry.record_probe("lan", 0.6, "lan")
    registry.record_link_measurement("lan", {"rtt_min_ms": 0.6, "download_mbps": 900})
    registry.record_probe("stale", 0.5, "direct")
    registry.link_probes["stale"]["measured_at"] -= 3600                # an hour old
    registry.record_probe("slow", 100.0, "tunnel")                     # usable for a member, too far to share
    registry.record_link_measurement("liar", {"rtt_min_ms": 0.5})      # says it is close; nobody measured it
    monkeypatch.setattr(coordinator, "registry", registry)
    return registry


def test_the_coordinator_pool_counts_this_machine_and_near_volunteers_only(lan_and_wan):
    pool = coordinator._coding_pool()
    assert pool["vram_gb"] == 24.0                        # 16 local + 8 on the LAN
    assert pool["far_vram_gb"] == 32.0                    # unmeasured, stale, slow, and one that only says it is near


def test_a_probe_result_is_filed_as_the_coordinators_own_measurement(lan_and_wan):
    coordinator._probe_paths["192.168.1.20:50052"] = "lan"
    coordinator._record_probed_rtt("192.168.1.20:50052", 0.9)
    probe = lan_and_wan.get_probe("lan")
    assert probe["rtt_ms"] == 0.9 and probe["via"] == "lan"
    measurement = lan_and_wan.get_link_measurement("lan")               # what the worker said is left alone
    assert measurement["rtt_min_ms"] == 0.6 and measurement["download_mbps"] == 900
    coordinator._record_probed_rtt("203.0.113.7:50052", 70.0)           # a WAN volunteer within 80 ms
    assert coordinator._coding_pool()["vram_gb"] == 32.0


def test_the_shared_pool_limit_is_80_ms_by_default(lan_and_wan):
    coordinator._record_probed_rtt("203.0.113.7:50052", 80.0)
    coordinator._record_probed_rtt("192.168.1.40:50052", 80.1)
    assert coordinator._max_shared_rtt() == 80.0
    assert coordinator._coding_pool()["vram_gb"] == 32.0                # 16 + LAN 8 + the 80 ms one


@pytest.mark.parametrize("rtt, grade", [
    (0.4, "best"), (40.0, "best"), (40.1, "good"), (80.0, "good"), (80.1, "usable"), (120.0, "usable"),
    (120.1, "too_far"), (900.0, "too_far"), (None, "unmeasured"), ("fast", "unmeasured"), (-1, "unmeasured"),
])
def test_the_owners_link_grades(rtt, grade):
    assert auto_swap.rtt_grade(rtt) == grade


def test_the_probe_skips_this_machine(lan_and_wan):
    targets = coordinator._probe_targets()
    assert "127.0.0.1:50052" not in targets and "192.168.1.20:50052" in targets


def test_tier_helpers():
    single = tier("t16", 16)
    assert auto_swap.downloadable(single)
    assert not auto_swap.downloadable({**single, "url": "http://insecure.example/t16.gguf"})
    assert not auto_swap.downloadable({**single, "sha256": ""})
    assert not auto_swap.downloadable({**single, "size": 0})
    assert not auto_swap.downloadable({**single, "file": "../escape.gguf"})
    assert auto_swap.tier_size(single) == 1_000_000
    assert auto_swap.fits_by_vram(single, {"vram_gb": 16.0})
    assert not auto_swap.fits_by_vram(single, {"vram_gb": 15.9})
    assert not auto_swap.fits_by_vram({**single, "min_vram_gb": "lots"}, {"vram_gb": 99})


# ======================================================================================
# Settings (engine/config.py)
# ======================================================================================

def _limits_from(isolated_config, text):
    isolated_config.write_text(text, encoding="utf-8")
    engine_config._cached_stamp = None
    return engine_config.get_worker_limits()


def test_auto_swap_settings_default_on(isolated_config):
    limits = engine_config.get_worker_limits()
    assert limits["auto_swap"] is True
    assert limits["auto_swap_up_stable_seconds"] == 600
    assert limits["auto_swap_down_stable_seconds"] == 120
    assert limits["auto_swap_cooldown_seconds"] == 1800


@pytest.mark.parametrize("raw, expected", [("false", False), ("true", True), ("TRUE", True),
                                           ("yes", False), ("", False), ("0", False)])
def test_auto_swap_switch_parses_like_its_neighbours(isolated_config, raw, expected):
    assert _limits_from(isolated_config, f"AUTO_SWAP={raw}\n")["auto_swap"] is expected


def test_auto_swap_timings_are_range_checked(isolated_config):
    limits = _limits_from(isolated_config, (
        "AUTO_SWAP_UP_STABLE_SECONDS=900\n"
        "AUTO_SWAP_DOWN_STABLE_SECONDS=5\n"          # below the floor: default kept
        "AUTO_SWAP_COOLDOWN_SECONDS=soon\n"          # not a number: default kept
    ))
    assert limits["auto_swap_up_stable_seconds"] == 900
    assert limits["auto_swap_down_stable_seconds"] == 120
    assert limits["auto_swap_cooldown_seconds"] == 1800


@pytest.mark.parametrize("raw, expected", [("5", 5.0), ("2.5", 2.5), ("15", 15.0), ("0", 80.0),
                                           ("2000", 80.0), ("near", 80.0), ("nan", 80.0)])
def test_the_rtt_limit_is_range_checked(isolated_config, raw, expected):
    assert _limits_from(isolated_config, f"AUTO_SWAP_MAX_RTT_MS={raw}\n")["auto_swap_max_rtt_ms"] == expected


@pytest.mark.parametrize("raw, expected", [("200", 200.0), ("60", 60.0), ("0", 120.0),
                                           ("9000", 120.0), ("far", 120.0), ("nan", 120.0)])
def test_the_member_rtt_limit_is_range_checked(isolated_config, raw, expected):
    assert _limits_from(isolated_config, f"CODING_MEMBER_MAX_RTT_MS={raw}\n")["coding_member_max_rtt_ms"] == expected


def test_the_rtt_limits_default_to_the_owners_choice(isolated_config):
    limits = engine_config.get_worker_limits()
    assert limits["auto_swap_max_rtt_ms"] == 80.0 and limits["coding_member_max_rtt_ms"] == 120.0


@pytest.mark.parametrize("raw, expected", [("300", 300), ("30", 30), ("5", 180), ("99999", 180), ("soon", 180)])
def test_the_load_limit_is_range_checked(isolated_config, raw, expected):
    assert _limits_from(isolated_config, f"AUTO_SWAP_LOAD_TIMEOUT_SECONDS={raw}\n")["auto_swap_load_timeout_seconds"] == expected


# ======================================================================================
# The coordinator's side
# ======================================================================================

def test_auto_swap_works_from_the_coding_ladder():
    tiers = coordinator._coding_tiers_for_swap()
    assert tiers == coordinator._coding_ladder()
    enabled = [t for t in tiers if t["enabled"] is True]
    assert enabled and enabled[0]["file"] == "Qwen3.6-35B-A3B-UD-IQ3_S.gguf"
    minimums = [t["min_vram_gb"] for t in tiers]
    assert minimums == sorted(minimums)
    for tier in tiers:
        # The ladder's "files" is exactly the shape the controller reads.
        assert auto_swap.tier_files(tier) == [dict(part) for part in tier["files"]]
        # Every ladder tier is pinned, split ones included, so every tier can be fetched.
        assert auto_swap.downloadable(tier) is True
        assert auto_swap.tier_size(tier) == tier["size"]


def test_split_tiers_are_downloadable_only_when_every_part_is():
    def split(**changes):
        parts = split_parts(**changes)
        return tier("t64", 64, files=parts, file=parts[0]["file"])

    assert auto_swap.downloadable(split())
    assert auto_swap.tier_size(split()) == 30
    assert not auto_swap.downloadable(split(url="http://models.example/part3.gguf"))
    assert not auto_swap.downloadable(split(url=""))
    assert not auto_swap.downloadable(split(sha256="nope"))
    assert not auto_swap.downloadable(split(size=0))
    assert not auto_swap.downloadable(split(file="../escape.gguf"))
    assert not auto_swap.downloadable(split(file="t64-00001-of-00003.gguf"))        # the same name twice
    assert not auto_swap.downloadable(tier("t64", 64, files=[], file=""))
    assert not auto_swap.downloadable(tier("t64", 64, files=["not a dict"]))
    assert auto_swap.tier_size(split(size=None)) == 0
    assert "1 of its 3 files" in auto_swap.why_not_downloadable(split(url="http://x/y.gguf"))
    assert "share a name" in auto_swap.why_not_downloadable(split(file="t64-00001-of-00003.gguf"))
    assert auto_swap.why_not_downloadable(tier("t16", 16, url="http://x/y")).startswith("no pinned download")


class FakeProcess:
    def __init__(self, dead):
        self.returncode = 1 if dead else None

    def poll(self):
        return self.returncode


class FakeEngines:
    """Stands in for the InferenceManager: engines are entries in a dict."""

    def __init__(self):
        self.running = {}
        self.calls = []
        self.dies = set()       # models whose engine exits while loading
        self.refuse = set()     # models astart_model refuses
        self.healthy = True
        self.size_gb = {}
        self.endpoints_used = []

    async def stop_all(self):
        self.calls.append("stop")
        self.running.clear()

    async def start(self, model, endpoints, vram, sysram, queue_channels=2):
        self.calls.append(("start", model))
        self.endpoints_used.append(list(endpoints))
        if model in self.refuse:
            return False, 0, "Network Maxed Out"
        self.running[model] = FakeProcess(dead=model in self.dies)
        return True, 8123, "Started"

    async def snapshot(self, model):
        process = self.running.get(model)
        return None if process is None else {"process": process, "port": 8123}

    async def resolve(self, tag):
        return f"/models/{tag}"

    async def estimate(self, tag):
        return self.size_gb.get(tag, 10.0)


@pytest.fixture
def engines(monkeypatch):
    fake = FakeEngines()
    monkeypatch.setattr(coordinator.manager, "astop_all_models", fake.stop_all)
    monkeypatch.setattr(coordinator.manager, "astart_model", fake.start)
    monkeypatch.setattr(coordinator.manager, "asnapshot_model", fake.snapshot)
    monkeypatch.setattr(coordinator.manager, "aresolve_model_path", fake.resolve)
    monkeypatch.setattr(coordinator.manager, "aestimate_model_vram_gb", fake.estimate)
    monkeypatch.setattr(coordinator.registry, "get_total_vram_gb", lambda: 24.0)
    monkeypatch.setattr(coordinator.registry, "get_total_sysram_gb", lambda: 12.0)
    monkeypatch.setattr(coordinator, "_planned_endpoints", lambda gb: ["127.0.0.1:50052"])
    monkeypatch.setattr(coordinator, "spawn_vram_progress_task", lambda port, model: None)

    async def health(port):
        return fake.healthy

    monkeypatch.setattr(coordinator, "_engine_health_ok", health)
    monkeypatch.setattr(coordinator, "ENGINE_READY_POLL_SECONDS", 0.01)
    # AUTO_SWAP_LOAD_TIMEOUT_SECONDS, shortened (config.txt would not accept under 30 s).
    real_limits = coordinator.get_worker_limits
    monkeypatch.setattr(coordinator, "get_worker_limits",
                        lambda: {**real_limits(), "auto_swap_load_timeout_seconds": 0.3})
    monkeypatch.setattr(coordinator, "global_active_model", "old.gguf")
    monkeypatch.setattr(coordinator, "cluster_status", "Active")
    monkeypatch.setattr(coordinator, "is_swapping_models", False)
    monkeypatch.setattr(coordinator, "is_recalculating_queue", False)
    monkeypatch.setattr(coordinator, "_reconfiguration_task", None)
    broadcasts = []

    async def fake_broadcast(payload):
        broadcasts.append(payload)

    monkeypatch.setattr(coordinator, "broadcast", fake_broadcast)
    fake.broadcasts = broadcasts
    with lease_manager._lock:
        lease_manager._leases.clear()
    yield fake
    with lease_manager._lock:
        lease_manager._leases.clear()


def _client():
    transport = ASGITransport(app=coordinator.app, client=("127.0.0.1", 5000))
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.anyio
async def test_the_swap_endpoint_behaves_as_before_and_holds_auto_swap_off(engines, monkeypatch, tmp_path):
    model_file = tmp_path / "new.gguf"
    model_file.write_bytes(b"GGUF")

    async def resolve(tag):
        return str(model_file)

    monkeypatch.setattr(coordinator.manager, "aresolve_model_path", resolve)
    noted = []
    monkeypatch.setattr(coordinator.auto_swapper, "note_manual_swap", lambda model=None: noted.append(model))

    async with _client() as client:
        resp = await client.post("/v1/cluster/swap", json={"model": "new"})

    assert resp.status_code == 200
    assert resp.json() == {"status": "swapped", "active_model": "new.gguf", "detail": "Started",
                           "required_gb": 10.0, "total_vram_gb": 24.0}
    assert engines.calls == ["stop", ("start", "new.gguf")]
    assert coordinator.is_swapping_models is False
    assert coordinator.cluster_status == "Active"
    assert engines.broadcasts[-1] == {"type": "swap_complete", "model": "new.gguf"}
    assert noted == ["new.gguf"]


@pytest.mark.anyio
async def test_the_swap_endpoint_still_leaves_a_failed_start_paused_without_rolling_back(engines, monkeypatch, tmp_path):
    model_file = tmp_path / "new.gguf"
    model_file.write_bytes(b"GGUF")

    async def resolve(tag):
        return str(model_file)

    monkeypatch.setattr(coordinator.manager, "aresolve_model_path", resolve)
    monkeypatch.setattr(coordinator.auto_swapper, "note_manual_swap", lambda model=None: None)
    engines.refuse.add("new.gguf")

    async with _client() as client:
        resp = await client.post("/v1/cluster/swap", json={"model": "new"})

    assert resp.json()["status"] == "queued"
    assert engines.calls == ["stop", ("start", "new.gguf")]
    assert coordinator.global_active_model == "new.gguf"
    assert coordinator.cluster_status == "Paused (Network Maxed Out)"


@pytest.mark.anyio
async def test_an_automatic_switch_keeps_a_model_that_comes_up(engines):
    result = await coordinator._auto_swap_to(tier("t24", 24), drain_seconds=1.0, abort_if_busy=True)
    assert result.status == "swapped"
    assert engines.calls == ["stop", ("start", "t24.gguf")]
    assert coordinator.global_active_model == "t24.gguf"
    assert coordinator.is_swapping_models is False
    assert engines.broadcasts[-1] == {"type": "swap_complete", "model": "t24.gguf"}


@pytest.mark.anyio
async def test_an_automatic_switch_rolls_back_when_the_engine_dies_loading(engines):
    engines.dies.add("t24.gguf")
    result = await coordinator._auto_swap_to(tier("t24", 24), drain_seconds=1.0, abort_if_busy=True)
    assert result.status == "failed" and "exited while loading" in result.detail
    assert engines.calls == ["stop", ("start", "t24.gguf"), "stop", ("start", "old.gguf")]
    assert coordinator.global_active_model == "old.gguf"
    assert coordinator.cluster_status == "Active"
    assert coordinator.is_swapping_models is False


@pytest.mark.anyio
async def test_an_automatic_switch_rolls_back_when_the_engine_never_gets_ready(engines):
    engines.healthy = False
    result = await coordinator._auto_swap_to(tier("t24", 24), drain_seconds=1.0, abort_if_busy=True)
    assert result.status == "failed" and "not ready" in result.detail
    assert engines.calls[-1] == ("start", "old.gguf")
    assert coordinator.global_active_model == "old.gguf"
    assert coordinator.is_swapping_models is False


@pytest.mark.anyio
async def test_an_automatic_switch_rolls_back_when_the_start_is_refused(engines):
    engines.refuse.add("t24.gguf")
    result = await coordinator._auto_swap_to(tier("t24", 24), drain_seconds=1.0, abort_if_busy=True)
    assert result.status == "failed"
    assert engines.calls == ["stop", ("start", "t24.gguf"), "stop", ("start", "old.gguf")]
    assert coordinator.global_active_model == "old.gguf"


@pytest.mark.anyio
async def test_an_up_switch_is_called_off_when_the_queue_does_not_drain(engines):
    lease = lease_manager.acquire(task_type="chat", timeout=60.0)
    try:
        result = await coordinator._auto_swap_to(tier("t24", 24), drain_seconds=0.2, abort_if_busy=True)
    finally:
        lease.release()
    assert result.status == "busy"
    assert engines.calls == []
    assert coordinator.global_active_model == "old.gguf"
    assert coordinator.is_swapping_models is False


@pytest.mark.anyio
async def test_a_down_switch_goes_ahead_after_the_drain(engines):
    lease = lease_manager.acquire(task_type="chat", timeout=60.0)
    try:
        result = await coordinator._auto_swap_to(tier("t16", 16), drain_seconds=0.2, abort_if_busy=False)
    finally:
        lease.release()
    assert result.status == "swapped"
    assert engines.calls == ["stop", ("start", "t16.gguf")]


@pytest.mark.anyio
async def test_an_automatic_switch_never_overlaps_another_reconfiguration(engines, monkeypatch):
    monkeypatch.setattr(coordinator, "is_swapping_models", True)
    result = await coordinator._auto_swap_to(tier("t24", 24), drain_seconds=1.0, abort_if_busy=True)
    assert result.status == "busy"
    assert engines.calls == []
    assert coordinator.is_swapping_models is True        # still the other switch's


@pytest.mark.anyio
async def test_an_automatic_switch_refuses_a_model_the_pool_cannot_admit(engines):
    engines.size_gb["t99.gguf"] = 40.0                    # the pool is 24 GB VRAM + 12 GB RAM
    result = await coordinator._auto_swap_to(tier("t99", 99), drain_seconds=1.0, abort_if_busy=True)
    assert result.status == "refused"
    assert engines.calls == []


@pytest.mark.anyio
async def test_an_automatic_switch_is_refused_when_the_plan_reaches_a_far_gpu(engines, lan_and_wan, monkeypatch):
    monkeypatch.setattr(lan_and_wan, "get_total_vram_gb", lambda: 48.0)
    monkeypatch.setattr(lan_and_wan, "get_total_sysram_gb", lambda: 12.0)
    monkeypatch.setattr(coordinator, "_planned_endpoints",
                        lambda gb: ["127.0.0.1:50052", "192.168.1.20:50052", "203.0.113.7:50052"])
    result = await coordinator._auto_swap_to(tier("t24", 24), drain_seconds=1.0, abort_if_busy=True)
    assert result.status == "refused" and "too far away" in result.detail
    assert engines.calls == []
    assert coordinator.is_swapping_models is False


@pytest.mark.anyio
async def test_an_automatic_switch_runs_on_the_near_gpus_it_checked(engines, lan_and_wan, monkeypatch):
    monkeypatch.setattr(lan_and_wan, "get_total_vram_gb", lambda: 24.0)
    monkeypatch.setattr(lan_and_wan, "get_total_sysram_gb", lambda: 12.0)
    monkeypatch.setattr(coordinator, "_planned_endpoints", lambda gb: ["127.0.0.1:50052", "192.168.1.20:50052"])
    result = await coordinator._auto_swap_to(tier("t24", 24), drain_seconds=1.0, abort_if_busy=True)
    assert result.status == "swapped"
    assert engines.endpoints_used == [["127.0.0.1:50052", "192.168.1.20:50052"]]


@pytest.mark.anyio
async def test_a_load_slower_than_the_limit_is_rolled_back_promptly(engines):
    engines.healthy = False                              # the new engine never answers
    assert coordinator._auto_swap_load_timeout() == 0.3  # AUTO_SWAP_LOAD_TIMEOUT_SECONDS
    started = asyncio.get_running_loop().time()
    result = await coordinator._auto_swap_to(tier("t24", 24), drain_seconds=1.0, abort_if_busy=True)
    elapsed = asyncio.get_running_loop().time() - started
    assert result.status == "failed" and "not ready after" in result.detail
    assert engines.calls[:2] == ["stop", ("start", "t24.gguf")]
    assert engines.calls[2:] == ["stop", ("start", "old.gguf")]
    assert coordinator.global_active_model == "old.gguf"
    assert elapsed < 5.0                                  # held for the limit, not ten minutes


def test_the_load_limit_defaults_to_three_minutes(isolated_config):
    assert engine_config.get_worker_limits()["auto_swap_load_timeout_seconds"] == 180


# --- the speed check after a move up (2.4.9) -------------------------------------------------------------

@pytest.fixture
def measured_speed(monkeypatch):
    """The engine's speed as llama.cpp would time it: set ``.rate`` (or ``.result``) per test."""
    probe = SimpleNamespace(rate=30.0, result=None, runs=[], delay=0.0)

    async def run(manager, model_path, prompt=None, max_tokens=128, source="api"):
        probe.runs.append((model_path, max_tokens, source))
        if probe.delay:
            await asyncio.sleep(probe.delay)
        return probe.result if probe.result is not None else {"ok": True, "tokens_per_second": probe.rate}

    monkeypatch.setattr(coordinator.benchmark, "run_inference_benchmark", run)
    return probe


@pytest.mark.anyio
async def test_a_move_up_that_is_fast_enough_stays(engines, measured_speed):
    result = await coordinator._auto_swap_to(tier("t24", 24), drain_seconds=1.0, abort_if_busy=True, check_speed=True)
    assert result.status == "swapped"
    assert engines.calls == ["stop", ("start", "t24.gguf")]
    # A warm-up, then the timed run, both on the new model while requests still wait.
    assert [(m, n) for m, n, _ in measured_speed.runs] == [("t24.gguf", 8), ("t24.gguf", 64)]


@pytest.mark.anyio
async def test_a_move_up_that_crawls_is_rolled_back(engines, measured_speed):
    measured_speed.rate = 6.2
    result = await coordinator._auto_swap_to(tier("t24", 24), drain_seconds=1.0, abort_if_busy=True, check_speed=True)
    assert result.status == "failed" and "6.2 tokens a second" in result.detail and "10" in result.detail
    assert engines.calls == ["stop", ("start", "t24.gguf"), "stop", ("start", "old.gguf")]
    assert coordinator.global_active_model == "old.gguf"
    assert coordinator.is_swapping_models is False


@pytest.mark.anyio
@pytest.mark.parametrize("result", [{"ok": False, "detail": "The engine answered HTTP 500."},
                                    {"ok": True, "tokens_per_second": None}, {"ok": True, "tokens_per_second": True}])
async def test_a_speed_that_cannot_be_measured_counts_as_too_slow(engines, measured_speed, result):
    measured_speed.result = result
    out = await coordinator._auto_swap_to(tier("t24", 24), drain_seconds=1.0, abort_if_busy=True, check_speed=True)
    assert out.status == "failed" and "could not be measured" in out.detail
    assert coordinator.global_active_model == "old.gguf"


@pytest.mark.anyio
async def test_a_speed_check_that_hangs_is_cut_short(engines, measured_speed, monkeypatch):
    monkeypatch.setattr(coordinator, "SPEED_CHECK_TIMEOUT_SECONDS", 0.2)
    measured_speed.delay = 5.0
    started = asyncio.get_running_loop().time()
    out = await coordinator._auto_swap_to(tier("t24", 24), drain_seconds=1.0, abort_if_busy=True, check_speed=True)
    assert out.status == "failed" and "did not finish" in out.detail
    assert asyncio.get_running_loop().time() - started < 3.0
    assert coordinator.global_active_model == "old.gguf"


@pytest.mark.anyio
async def test_a_floor_of_zero_and_a_move_down_are_not_speed_checked(engines, measured_speed, monkeypatch):
    out = await coordinator._auto_swap_to(tier("t16", 16), drain_seconds=1.0, abort_if_busy=False)
    assert out.status == "swapped" and measured_speed.runs == []
    limits = coordinator.get_worker_limits
    monkeypatch.setattr(coordinator, "get_worker_limits", lambda: {**limits(), "auto_swap_min_decode_tps": 0.0})
    out = await coordinator._auto_swap_to(tier("t24", 24), drain_seconds=1.0, abort_if_busy=True, check_speed=True)
    assert out.status == "swapped" and measured_speed.runs == []


@pytest.mark.anyio
async def test_the_controller_speed_checks_moves_up_only():
    rig = Rig(vram=24)
    await rig.every(T0, T0 + 600)                         # up to t24
    rig.vram = 16
    await rig.every(T0 + 630, T0 + 750)                   # back down to t16
    assert rig.speed_checked == [("t24", True), ("t16", False)]


@pytest.mark.anyio
async def test_a_tier_that_was_too_slow_is_held_off_like_any_failed_switch():
    rig = Rig(vram=24)
    rig.result = "failed"
    await rig.every(T0, T0 + 600)
    assert [s[0] for s in rig.swaps] == ["t24"]
    status = rig.controller.status()
    assert "not trying it again" in status["detail"]
    await rig.every(T0 + 630, T0 + 4 * 3600)             # well inside the 6 h hold
    assert [s[0] for s in rig.swaps] == ["t24"]


def test_the_speed_floor_defaults_to_ten_and_is_range_checked(isolated_config):
    assert engine_config.get_worker_limits()["auto_swap_min_decode_tps"] == 10.0
    for raw, expected in (("15", 15.0), ("0", 0.0), ("-1", 10.0), ("fast", 10.0), ("5000", 10.0)):
        assert _limits_from(isolated_config, f"AUTO_SWAP_MIN_DECODE_TPS={raw}\n")["auto_swap_min_decode_tps"] == expected


@pytest.mark.anyio
async def test_a_manual_switch_during_an_automatic_one_wins(engines, monkeypatch):
    engines.healthy = False

    async def manual_switch_lands(port):
        # A manual switch takes over while the automatic one waits for its engine.
        coordinator._model_swap_generation += 1
        return False

    monkeypatch.setattr(coordinator, "_engine_health_ok", manual_switch_lands)
    result = await coordinator._auto_swap_to(tier("t24", 24), drain_seconds=1.0, abort_if_busy=True)
    assert result.status == "superseded"
    assert ("start", "old.gguf") not in engines.calls     # no rollback over the manual switch
    assert coordinator.is_swapping_models is True         # the newer switch owns the flag


@pytest.mark.anyio
async def test_cluster_status_reports_auto_swap():
    async with _client() as client:
        resp = await client.get("/v1/cluster/status")
    block = resp.json()["auto_swap"]
    assert set(block) == {"enabled", "state", "target", "detail"}
    assert block["state"] in auto_swap.STATES
    assert isinstance(block["detail"], str) and block["detail"]


@pytest.fixture
def ladder_rungs(monkeypatch):
    """The coordinator's controller, as if its last tick saw t16 and t24 enabled."""
    monkeypatch.setattr(coordinator.auto_swapper, "_enabled_tiers", [tier("t16", 16), tier("t24", 24)])
    monkeypatch.setattr(coordinator.auto_swapper, "_settings", lambda: {"auto_swap": True})
    monkeypatch.setattr(coordinator, "global_active_model", "t24.gguf")
    monkeypatch.setattr(coordinator, "is_swapping_models", False)
    monkeypatch.setattr(coordinator, "is_recalculating_queue", False)
    started = []

    async def fake_start(model_path, *args, **kwargs):
        started.append(model_path)
        return (False, 0, "stopped by the test")

    async def fake_estimate(_model):
        return 1.0

    monkeypatch.setattr(coordinator.manager, "astart_model", fake_start)
    monkeypatch.setattr(coordinator.manager, "aestimate_model_vram_gb", fake_estimate)
    with lease_manager._lock:
        lease_manager._leases.clear()
    yield started
    with lease_manager._lock:
        lease_manager._leases.clear()


@pytest.mark.anyio
async def test_a_chat_naming_the_previous_rung_is_served_by_the_current_one(ladder_rungs):
    async with _client() as client:
        await client.post("/v1/chat/completions",
                          json={"model": "t16.gguf", "messages": [{"role": "user", "content": "hi"}]})
    assert ladder_rungs == ["t24.gguf"]


@pytest.mark.anyio
async def test_a_chat_naming_a_model_off_the_ladder_is_untouched(ladder_rungs):
    async with _client() as client:
        await client.post("/v1/chat/completions",
                          json={"model": "phi3-mini.gguf", "messages": [{"role": "user", "content": "hi"}]})
    assert ladder_rungs == ["phi3-mini.gguf"]


@pytest.mark.anyio
async def test_a_completion_naming_the_previous_rung_is_served_by_the_current_one(ladder_rungs):
    async with _client() as client:
        await client.post("/v1/completions", json={"prompt": "def f(", "model": "t16.gguf"})
    assert ladder_rungs == ["t24.gguf"]


# ======================================================================================
# Split tiers downloaded for real (engine/downloader.py behind the coordinator's callback)
# ======================================================================================
#
# A local HTTPS server (tests/download_fakes.py) serves the parts; the downloader, the
# coordinator's download function, the models folder and the controller are the real ones.

@pytest.fixture
def parts_server(tmp_path, monkeypatch):
    for variable in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    certs = tmp_path / "certs"
    certs.mkdir()
    with FakeModelServer(certs) as running:
        yield running


def served_split_tier(server, size=200_000):
    blobs = [make_blob(size, seed) for seed in range(3)]
    parts = [server.serve(f"t64-0000{i + 1}-of-00003.gguf", blob) for i, blob in enumerate(blobs)]
    return tier("t64", 64, files=parts, file=parts[0]["file"], size=3 * size), parts, blobs


@pytest.fixture
def coordinator_downloads(parts_server, tmp_path, monkeypatch):
    """The coordinator's download path pointed at a temporary models folder and the local server."""
    folder = tmp_path / "models"
    calls = []
    finished = threading.Event()

    def recording(files, dest, progress=None, **options):
        calls.append({"files": files, "dest": dest, "options": dict(options)})
        try:
            return downloader.download_verified(files, dest, progress, ssl_context=parts_server.client_context,
                                                timeout=5.0, backoff=0.01, max_backoff=0.05, **options)
        finally:
            finished.set()

    monkeypatch.setattr(coordinator.manager, "_model_search_roots", lambda: [str(folder)])
    monkeypatch.setattr(coordinator, "download_verified", recording)
    monkeypatch.setattr(downloader, "_disk_free", lambda path: 10 ** 12)
    return folder, calls, finished


@pytest.mark.anyio
async def test_the_coordinator_downloads_every_part_of_a_split_tier(parts_server, coordinator_downloads, monkeypatch):
    folder, calls, _ = coordinator_downloads
    split, parts, blobs = served_split_tier(parts_server)
    noted = []
    monkeypatch.setattr(coordinator.auto_swapper, "note_download_progress", lambda *a: noted.append(a))

    assert await coordinator._download_coding_tier(split) is True

    for part, blob in zip(parts, blobs):
        assert (folder / part["file"]).read_bytes() == blob
        assert app_pool.sha256_ok(str(folder / part["file"]), part["sha256"], part["size"])
    total = 3 * 200_000
    assert len(calls) == 1 and calls[0]["dest"] == str(folder)
    assert [f["file"] for f in calls[0]["files"]] == [p["file"] for p in parts]
    # The same free-space rule as a single file: 10% and a 20 GB buffer.
    assert calls[0]["options"]["free_margin"] == int(total * 0.1 + coordinator.CODING_DOWNLOAD_FREE_BUFFER_GB * 1024 ** 3)
    assert noted and noted[-1] == ("t64", total, total)
    # And the coordinator's own readiness check now agrees that the whole tier is on disk.
    assert coordinator._coding_tier_on_disk(split) is True
    # Nothing but the parts and their markers is left in the folder.
    assert sorted(os.listdir(folder)) == sorted([p["file"] for p in parts] + [p["file"] + ".dlne-sha256" for p in parts])


@pytest.mark.anyio
async def test_a_second_download_of_a_split_tier_fetches_nothing(parts_server, coordinator_downloads):
    split, _, _ = served_split_tier(parts_server)
    assert await coordinator._download_coding_tier(split) is True
    parts_server.requests.clear()
    assert await coordinator._download_coding_tier(split) is True
    assert parts_server.requests == []


@pytest.mark.anyio
async def test_a_split_tier_is_not_downloaded_onto_a_disk_that_is_too_small(parts_server, coordinator_downloads, monkeypatch):
    folder, _, _ = coordinator_downloads
    split, _, _ = served_split_tier(parts_server)
    monkeypatch.setattr(downloader, "_disk_free", lambda path: 5 * 1024 ** 3)       # under the 20 GB buffer
    assert await coordinator._download_coding_tier(split) is False
    assert parts_server.requests == []
    assert [n for n in os.listdir(folder) if n.endswith(".gguf")] == []


@pytest.mark.anyio
async def test_a_split_tier_with_a_wrong_part_is_not_reported_as_downloaded(parts_server, coordinator_downloads):
    folder, _, _ = coordinator_downloads
    split, parts, _ = served_split_tier(parts_server)
    split["files"][2] = dict(split["files"][2], sha256="cd" * 32)
    assert await coordinator._download_coding_tier(split) is False
    assert coordinator._coding_tier_on_disk(split) is False
    assert not (folder / parts[2]["file"]).exists()


@pytest.mark.anyio
async def test_single_file_tiers_still_take_the_single_file_path(parts_server, coordinator_downloads, monkeypatch):
    seen = []

    async def single(url, dest, label, **kw):
        seen.append(url)
        return False

    monkeypatch.setattr(coordinator, "download_file_multistream_async", single)
    monkeypatch.setattr(coordinator.shutil, "disk_usage", lambda path: type("Usage", (), {"free": 10 ** 12})())
    assert await coordinator._download_coding_tier(tier("t16", 16)) is False
    assert seen == ["https://models.example/t16.gguf"]
    assert coordinator_downloads[1] == []                                            # download_verified not used


@pytest.mark.anyio
async def test_cancelling_the_task_stops_the_download_and_keeps_the_partial_part(parts_server, coordinator_downloads):
    folder, _, finished = coordinator_downloads
    split, parts, _ = served_split_tier(parts_server, size=400_000)
    parts_server.rule(parts[0]["file"], delay=0.02)                                  # about half a second
    task = asyncio.create_task(coordinator._download_coding_tier(split))
    partial = folder / (parts[0]["file"] + ".part")
    for _ in range(300):
        await asyncio.sleep(0.02)
        if partial.exists():
            break
    assert partial.exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(finished.wait, 10)                                # the thread really stopped
    assert not (folder / parts[0]["file"]).exists()
    assert parts_server.count(parts[1]["file"]) == 0
    assert [n for n in os.listdir(folder) if n.endswith(".lock")] == []


@pytest.mark.anyio
async def test_the_controller_fetches_a_split_tier_with_the_real_downloader_and_then_switches(parts_server, tmp_path):
    split, parts, blobs = served_split_tier(parts_server)
    parts_server.rule(parts[1]["file"], delay=0.01)
    folder = tmp_path / "models"
    clock = {"now": T0}
    swaps = []
    served = {"model": "t32.gguf"}
    logs = []

    async def swap(chosen, *, drain_seconds, abort_if_busy, check_speed=False):
        swaps.append(chosen["id"])
        served["model"] = chosen["file"]
        return SwapResult("swapped", "from the test")

    async def ready(chosen):
        if chosen["id"] != "t64":
            return True
        files = auto_swap.tier_files(chosen)
        return await asyncio.to_thread(
            lambda: all(app_pool.sha256_ok(str(folder / f["file"]), f["sha256"], f["size"]) for f in files))

    async def download(chosen):
        return await asyncio.to_thread(
            downloader.download_verified, auto_swap.tier_files(chosen), str(folder),
            lambda update: controller.note_download_progress(chosen["id"], update["done"], update["total"]),
            ssl_context=parts_server.client_context, timeout=5.0, backoff=0.01, max_backoff=0.05)

    controller = AutoSwapController(
        tiers_provider=lambda: [tier("t16", 16), tier("t32", 32), dict(split)],
        pool_provider=lambda: {"vram_gb": 64.0, "far_vram_gb": 0.0, "sysram_gb": 12.0},
        current_model=lambda: served["model"],
        swap=swap, tier_ready=ready, download=download,
        settings=lambda: {"auto_swap": True, "auto_swap_up_stable_seconds": 0,
                          "auto_swap_down_stable_seconds": 120, "auto_swap_cooldown_seconds": 0},
        swap_in_progress=lambda: False, queue_busy=lambda: False,
        clock=lambda: clock["now"], log=logs.append,
    )
    details = []
    for _ in range(400):
        clock["now"] += 30
        details.append((await controller.tick()).detail)
        if swaps:
            break
        await asyncio.sleep(0.05)

    assert swaps == ["t64"]
    assert any(d.startswith("Downloading") and "3 files" in d for d in details)
    for part, blob in zip(parts, blobs):
        assert (folder / part["file"]).read_bytes() == blob
    assert all(parts_server.count(p["file"]) == 1 for p in parts)
    assert any("is downloaded and verified" in line for line in logs)
