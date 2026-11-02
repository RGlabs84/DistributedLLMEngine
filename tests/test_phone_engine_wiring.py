"""
The phones' own engine wired into the coordinator (2.9.0, docs/release-2.9-contract.md 6b, 6c and 6c+): PHONE_ENGINE=own
answers phones on the phone engine's llama-server instead of the coding engine's phone lanes; the routing seam
(_wall_phone_rung) and the status see it; the switcher's volunteers come from the registry with coding's volunteers held
back; a takeover that takes the phone engine's card stops it; and the switcher stops it when PHONE_ENGINE is not own.

The environment is the phone-lane endpoint tests' (tests/test_phone_lane_endpoint.py) with a second stand-in llama-server
playing the phone engine.
"""
import asyncio
from types import SimpleNamespace

import pytest

from engine import coordinator, phone_swap, takeover
from engine.config import get_plain_worker_limits
from engine.image_member import MemberError
from engine.phone_engine import PhoneEngine, phone_rungs
from engine.priority import CodingGate, LaneAffinity, LaneBook
from engine.registry import WorkerRegistry
from tests.phone_fakes import FakeClock, FakeEngine
from tests.takeover_fakes import MEDIA, POOL_A, box  # noqa: F401 - box is a fixture
from tests.test_phone_lane_endpoint import (  # noqa: F401 - env and servers are fixtures
    CODING_SLOTS, LaneEngine, PHONE_SLOTS, client, env, nothing_held, phone_body, servers, until,
)

PHONE_LANES = 2


@pytest.fixture(scope="module")
def phone_server():
    server = LaneEngine().start()
    yield server
    server.stop()


@pytest.fixture
def own(env, phone_server, monkeypatch):
    """PHONE_ENGINE=own with the phone engine up on its base rung (qwen35-4b), its process a stand-in."""
    phone_server.reset()
    env.limits.update({"phone_engine": "own", "phone_lanes": PHONE_LANES, "phone_context": 8192, "phone_vision": True})
    engine = PhoneEngine(limits=lambda: dict(env.limits))
    engine.process = SimpleNamespace(poll=lambda: None)
    engine.port = phone_server.port
    engine.rung = phone_rungs()[0]
    engine.layout = {"home_layers": 32, "volunteer_layers": {}, "used": []}
    engine.open_for_phones()
    monkeypatch.setattr(coordinator, "own_phone_engine", engine)
    monkeypatch.setattr(coordinator, "own_phone_lanes", LaneBook())
    monkeypatch.setattr(coordinator, "own_phone_affinity", LaneAffinity())
    return SimpleNamespace(engine=engine, server=phone_server, env=env)


async def status():
    async with client(("127.0.0.1", 5000)) as admin:
        return (await admin.get("/v1/cluster/status")).json()


# ======================================================================================
# Phones on the phone engine
# ======================================================================================

@pytest.mark.anyio
async def test_a_phone_is_answered_by_the_phone_engine_not_the_coding_engines_lanes(own):
    async with client() as c:
        resp = await c.post("/v1/app/chat/completions", json=phone_body())
    assert resp.status_code == 200 and b"engine-one" in resp.content
    assert len(own.server.chats) == 1 and own.env.engine.chats == [] and own.env.cpu.payloads == []
    assert own.server.chats[0]["id_slot"] in range(PHONE_LANES), "the phone engine's own lanes start at 0"
    assert own.engine.in_flight == 0 and nothing_held(own.env)


@pytest.mark.anyio
async def test_the_status_shows_the_phone_engine_its_lanes_and_its_model(own):
    body = await status()
    assert body["phone_lanes"]["slots"] == PHONE_LANES and body["phone_lanes"]["available"] is True
    block = body["phone_engine"]
    assert block["mode"] == "own" and block["rung"] == "qwen35-4b" and block["layers"] == {"home": 32, "volunteers": 0}
    assert set(block["switch"]) >= {"enabled", "state", "detail"}
    assert body["app_pool"]["photos"] == "phone_model"
    assert body["app_pool"]["active_model"] == phone_rungs()[0]["name"]


@pytest.mark.anyio
async def test_while_the_phone_engine_is_switching_the_coding_engines_phone_lanes_are_the_backup(own):
    own.engine._switching = True          # noqa: SLF001 - the engine's own flag, as switch() sets it
    async with client() as c:
        resp = await c.post("/v1/app/chat/completions", json=phone_body())
    assert resp.status_code == 200 and own.server.chats == [] and own.env.cpu.payloads == []
    assert own.env.engine.chats and own.env.engine.chats[0]["id_slot"] >= CODING_SLOTS
    lanes = (await status())["phone_lanes"]
    assert lanes["available"] is True and lanes["slots"] == PHONE_SLOTS and nothing_held(own.env)


@pytest.mark.anyio
async def test_with_no_backup_lanes_a_phone_engine_that_is_down_hands_the_phone_to_the_cpu_pool(own):
    own.env.limits["engine_phone_slots"] = 0
    own.env.manager.phone_slots = 0
    own.engine._switching = True          # noqa: SLF001
    async with client() as c:
        resp = await c.post("/v1/app/chat/completions", json=phone_body())
    assert resp.status_code == 200 and own.server.chats == [] and own.env.cpu.payloads
    assert (await status())["phone_lanes"]["starting"] is True


@pytest.mark.anyio
async def test_with_every_phone_engine_lane_busy_the_phone_waits_then_gets_the_429(own, monkeypatch):
    monkeypatch.setattr(coordinator, "PHONE_LANE_WAIT_SECONDS", 0.2)
    monkeypatch.setattr(coordinator, "phone_gate", CodingGate(PHONE_LANES))
    assert all([coordinator.phone_gate.try_acquire() for _ in range(PHONE_LANES)])
    try:
        async with client() as c:
            resp = await c.post("/v1/app/chat/completions", json=phone_body())
    finally:
        for _ in range(PHONE_LANES):
            coordinator.phone_gate.release()
    assert resp.status_code == 429 and own.server.chats == [] and own.engine.in_flight == 0


@pytest.mark.anyio
async def test_a_phone_that_hangs_up_gives_the_phone_engines_lane_back(own):
    own.server.release.clear()
    async with client() as c:
        task = asyncio.create_task(c.post("/v1/app/chat/completions", json=phone_body()))
        assert await until(lambda: len(own.server.chats) == 1 and own.engine.in_flight == 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        own.server.release.set()
        assert await until(lambda: own.engine.in_flight == 0 and coordinator.phone_gate.stats()["in_use"] == 0)
    assert nothing_held(own.env) and all(v == 0 for v in coordinator.own_phone_lanes._uses.values())


@pytest.mark.anyio
async def test_shared_mode_keeps_todays_coding_engine_lanes(env):
    env.limits["phone_engine"] = "shared"
    async with client() as c:
        resp = await c.post("/v1/app/chat/completions", json=phone_body())
    assert b"engine-one" in resp.content and env.engine.chats and env.engine.chats[0]["id_slot"] >= CODING_SLOTS
    assert (await status())["phone_engine"]["mode"] == "shared"


@pytest.mark.anyio
async def test_the_routing_seam_answers_for_the_phone_engine(own):
    rung = await coordinator._wall_phone_rung(photos=True)
    assert rung.available and rung.free and rung.reads_photos and rung.engine["own_phone_engine"] is True
    own.env.limits["phone_vision"] = False
    assert not (await coordinator._wall_phone_rung(photos=True)).reads_photos
    assert coordinator._phone_photos_where() in ("coding_lane", "none")
    own.engine.close_to_phones()
    backup = await coordinator._wall_phone_rung()
    assert backup.available and not backup.engine.get("own_phone_engine"), "the coding engine's phone lanes"
    own.env.limits["engine_phone_slots"] = 0
    own.env.manager.phone_slots = 0
    assert not (await coordinator._wall_phone_rung()).available


# ======================================================================================
# The switcher's volunteers, and coding winning
# ======================================================================================

@pytest.fixture
def lenders(monkeypatch):
    registry = WorkerRegistry()
    gpu = {"gpu": {"has_gpu": True, "vram_total_gb": 8.0}, "ram_gb": 32.0}
    registry.update_worker(worker_id="local", ip="127.0.0.1", port=50052, token="t0", hardware_profile=dict(gpu))
    registry.update_worker(worker_id="lap", ip="192.168.1.20", port=50052, token="t1", hardware_profile={
        **gpu, "ram_lent_gb": 8.0, "endpoints": [{"type": "gpu", "port": 50052}, {"type": "cpu", "port": 50053, "ram_gb": 8.0}]})
    registry.update_worker(worker_id="old", ip="192.168.1.21", port=50052, token="t2", hardware_profile=dict(gpu))
    registry.update_worker(worker_id="ramonly", ip="192.168.1.22", port=50060, token="t3", hardware_profile={
        "gpu": {"has_gpu": False}, "ram_gb": 16.0, "ram_lent_gb": 6.0,
        "endpoints": [{"type": "cpu", "port": 50060, "ram_gb": 6.0}]})
    registry.update_worker(worker_id="phonepool", ip="192.168.1.23", port=50070, token="t4", hardware_profile={
        "pool": "app", "ram_gb": 12.0, "gpu": None, "app": {"model": "small", "ready": True, "slots": 2}})
    for wid, rtt in (("lap", 1.0), ("old", 120.0), ("ramonly", 2.0)):
        registry.record_probe(wid, rtt, "lan")
    monkeypatch.setattr(coordinator, "registry", registry)
    monkeypatch.setattr(coordinator, "manager", SimpleNamespace(current_endpoints=[]))
    return registry


def test_each_rpc_server_a_volunteer_runs_is_one_entry_and_this_machine_and_pool_members_are_left_out(lenders):
    entries = {(v["id"], v["kind"]): v for v in coordinator._phone_volunteers()}
    assert set(entries) == {("lap", "gpu"), ("lap:ram", "cpu"), ("old", "gpu"), ("ramonly:ram", "cpu")}
    assert entries[("lap:ram", "cpu")]["endpoint"] == "192.168.1.20:50053" and entries[("lap:ram", "cpu")]["ram_lent_gb"] == 8.0
    assert entries[("old", "gpu")]["endpoint"] == "192.168.1.21:50052", "a worker from before 2.9.0 is its main port"
    assert entries[("ramonly:ram", "cpu")]["ram_lent_gb"] == 6.0       # capped at its own RAM less what it keeps: 16 - 4
    assert entries[("lap", "gpu")]["rtt_ms"] == 1.0 and entries[("lap", "gpu")]["rtt_age_s"] < 60


def test_the_link_probe_measures_volunteers_that_lend_only_ram(lenders):
    assert "192.168.1.22:50060" in coordinator._probe_targets()
    coordinator._record_probed_rtt("192.168.1.22:50060", 7.0)
    assert lenders.get_probe("ramonly")["rtt_ms"] == 7.0


def test_coding_claims_its_near_gpus_with_their_ram_and_leaves_the_rest_to_the_phones(lenders, monkeypatch):
    monkeypatch.setattr(coordinator, "_max_shared_rtt", lambda: 80.0)
    claimed = coordinator._phone_coding_claims()
    assert claimed == {"lap", "lap:ram"}, "near for coding (1 ms); old is 120 ms away and ramonly has no GPU"
    eligible = phone_swap.eligible_volunteers(coordinator._phone_volunteers(), max_rtt_ms=200.0, ram_settle_seconds=0.0,
                                              claimed=claimed)
    assert {v["id"] for v in eligible} == {"old", "ramonly:ram"}


# ======================================================================================
# Starting, stopping, and takeovers
# ======================================================================================

@pytest.mark.anyio
async def test_a_volunteer_the_engine_cannot_reach_stops_the_start(monkeypatch, tmp_path):
    rung = phone_rungs()[0]
    limits = {"phone_engine": "own", "phone_home_budget_mb": 0, "phone_lanes": 2, "phone_context": 8192, "phone_vision": True}
    engine = PhoneEngine(limits=lambda: dict(limits), resolve=lambda endpoint: None)
    monkeypatch.setattr(engine, "unavailable_reason", lambda rung=None: None)
    volunteers = [{"id": "v", "kind": "gpu", "endpoint": "10.0.0.5:50052", "vram_gb": 64.0}]
    monkeypatch.setattr("engine.phone_engine.plan_layout",
                        lambda *a, **k: {"ok": True, "used": volunteers, "home_layers": 1, "home_mb": 100.0,
                                         "volunteer_layers": {"v": 31}, "split": [1, 31]})
    with pytest.raises(MemberError):
        await engine.start(rung, volunteers)
    assert engine.process is None


@pytest.mark.anyio
async def test_the_switcher_stops_the_engine_when_phone_engine_is_not_own(monkeypatch):
    monkeypatch.setattr(phone_swap, "_takeover_in_progress", lambda: False)
    engine = FakeEngine(FakeClock())
    engine.loaded, engine.rung, engine.is_own = True, phone_rungs()[0], False
    ctl = phone_swap.PhoneSwapController(engine=engine, rungs_provider=phone_rungs, volunteers_provider=list,
                                         settings=lambda: {"phone_engine": "shared"}, coding_claims=set)
    decision = await ctl.tick()
    assert decision.reason == "shared" and engine.loaded is False and {"stop": True} in engine.calls


def _configure(box, text):
    from engine import config as engine_config
    box.config_path.write_text(text)
    engine_config._cached_stamp = None


def _plan(box, gpus):
    return takeover.make_plan(takeover.parse_request({"gpus": gpus, "holder": "DLNE-Sec on portoBOX"}),
                              limits=get_plain_worker_limits(), gpus=box.gpus, facts=box.facts, memory_now=box.mem.read(),
                              system_ram_gb=box.system_ram_gb, lease_active=False)


def test_a_lease_that_takes_the_media_card_stops_the_phone_engine_only_when_it_runs_there(box):
    from tests.takeover_fakes import DEFAULT_CONFIG
    _configure(box, DEFAULT_CONFIG + "PHONE_ENGINE=own\n")
    assert _plan(box, [MEDIA]).stop_phone_engine is True
    assert _plan(box, [POOL_A]).stop_phone_engine is False
    _configure(box, DEFAULT_CONFIG + "PHONE_ENGINE=own\nPHONE_DEVICE=cpu\n")
    assert _plan(box, [MEDIA]).stop_phone_engine is False, "on the host's CPU it holds no card"
    _configure(box, DEFAULT_CONFIG)
    assert _plan(box, [MEDIA]).stop_phone_engine is False, "shared: there is no phone engine"


@pytest.mark.anyio
async def test_the_grant_stops_the_phone_engine_when_the_plan_says_so(box):
    from tests.takeover_fakes import DEFAULT_CONFIG
    _configure(box, DEFAULT_CONFIG + "PHONE_ENGINE=own\n")
    stopped = []

    async def stop():
        stopped.append(True)

    ctrl = box.controller()
    ctrl.host.stop_phone_engine = stop
    await ctrl.grant({"gpus": [MEDIA], "holder": "h"})
    assert stopped == [True]
