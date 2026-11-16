"""
CODING_ROUTE (2.9.2, owner: "all coding is routed to the wall ... the wall first, everyone else if not avail"): a coding
request is this coordinator's own engine's while it can take it; a member is offered only when it cannot. The environment is
the coding endpoint tests' (tests/test_coding_endpoint.py), with a running engine standing in for its LocalEngine.
"""
from types import SimpleNamespace

import pytest

from engine import coding_speed, config as engine_config, coordinator
from engine.priority import CodingGate
from tests.test_coding_endpoint import MODEL_FILE, TIER, canary_book, client, env, fakes, lookup  # noqa: F401 - fixtures


class Engine:
    """The running coding engine as the route asks about it: 4 lanes of 16,384 tokens."""

    def __init__(self):
        self.running, self.alive = True, True

    def get_active_model_names(self):
        return [MODEL_FILE] if self.running else []

    async def asnapshot_model(self, model):
        return {"process": SimpleNamespace(poll=lambda: None if self.alive else 1), "port": 8081,
                "safe_context": 65536, "dynamic_slots": 4} if self.running else None

    def __getattr__(self, name):
        return []       # current_endpoints and the rest: this test asks nothing else of it


@pytest.fixture
def wall(env, monkeypatch):
    """CODING_ROUTE=wall_first, a member ready to answer, this engine up with its lanes free."""
    env.limits["coding_route"] = "wall_first"
    env.add(env.fakes["a"], "ma")
    engine = Engine()
    listening = {"yes": True}

    async def probe(port):
        return listening["yes"]

    monkeypatch.setattr(coordinator, "manager", engine)
    monkeypatch.setattr(coordinator, "_probe_engine_port", probe)
    monkeypatch.setattr(coordinator, "coding_gate", CodingGate(4))
    return SimpleNamespace(env=env, engine=engine, listening=listening)


def hold_every_lane():
    assert all([coordinator.coding_gate.try_acquire() for _ in range(4)])


@pytest.mark.anyio
async def test_with_a_lane_free_the_engine_answers_though_a_member_is_ready(wall):
    async with client() as c:
        assert (await lookup(c))["route"] is None


@pytest.mark.anyio
async def test_every_lane_busy_offers_the_member(wall):
    hold_every_lane()
    async with client() as c:
        assert (await lookup(c))["route"]


@pytest.mark.anyio
async def test_one_lane_free_is_still_the_engines(wall):
    assert all([coordinator.coding_gate.try_acquire() for _ in range(3)])
    async with client() as c:
        assert (await lookup(c))["route"] is None


@pytest.mark.anyio
async def test_someone_waiting_for_a_lane_offers_the_member(wall, monkeypatch):
    monkeypatch.setattr(coordinator, "coding_gate", SimpleNamespace(stats=lambda: {"slots": 4, "in_use": 2, "waiting": 1}))
    async with client() as c:
        assert (await lookup(c))["route"]


@pytest.mark.anyio
@pytest.mark.parametrize("flag", ["is_swapping_models", "is_recalculating_queue"])
async def test_an_engine_being_rebuilt_offers_the_member(wall, monkeypatch, flag):
    monkeypatch.setattr(coordinator, flag, True)
    async with client() as c:
        assert (await lookup(c))["route"]


@pytest.mark.anyio
@pytest.mark.parametrize("how", ["no engine", "process gone", "not listening"])
async def test_an_engine_that_is_down_offers_the_member(wall, how):
    if how == "no engine":
        wall.engine.running = False
    elif how == "process gone":
        wall.engine.alive = False
    else:
        wall.listening["yes"] = False
    async with client() as c:
        assert (await lookup(c))["route"]


@pytest.mark.anyio
async def test_a_request_too_big_for_a_lane_offers_a_member_that_holds_it(wall):
    async with client() as c:
        fits = (await c.get("/v1/coding/route", params={"session": "s1", "privacy": "any", "need": "16000"})).json()
        big = (await c.get("/v1/coding/route", params={"session": "s2", "privacy": "any", "need": "20000"})).json()
    assert fits["route"] is None and big["route"]


@pytest.mark.anyio
async def test_a_session_a_member_served_comes_back_to_the_engine_when_a_lane_is_free(wall):
    hold_every_lane()
    async with client() as c:
        assert (await lookup(c, session="agent"))["route"]
        for _ in range(4):
            coordinator.coding_gate.release()
        assert (await lookup(c, session="agent"))["route"] is None


@pytest.mark.anyio
async def test_with_no_member_and_no_lane_the_request_still_waits_at_the_engine(wall):
    wall.env.registry.workers.clear()
    hold_every_lane()
    async with client() as c:
        assert (await lookup(c))["route"] is None


@pytest.mark.anyio
async def test_a_person_who_asked_for_their_own_smarter_machine_still_gets_it(wall, monkeypatch):
    smarter = "Qwen3.6-35B-A3B-UD-IQ4_XS.gguf"
    monkeypatch.setattr(coordinator, "_coding_tiers", [dict(TIER), {**TIER, "id": "qwen36-a3b-iq4xs", "file": smarter,
                                                                    "min_vram_gb": 24, "files": [{"file": smarter}]}])
    monkeypatch.setattr(coordinator, "global_active_model", MODEL_FILE)
    monkeypatch.setattr(coding_speed, "meter", SimpleNamespace(speed=lambda file: (65.0, "2026-10-30T09:00:00Z")))
    mine = wall.env.fakes["b"]
    wall.env.add(mine, "mine", slots=4, decode=27.0, model=smarter)
    async with client() as c:
        params = {"session": "own", "privacy": "any", "own": mine.trust_id}
        assert (await c.get("/v1/coding/route", params=params)).json()["route"] is None, "the default rule: the engine first"
        assert (await c.get("/v1/coding/route", params={**params, "prefer": "smarter"})).json()["route"]


@pytest.mark.anyio
async def test_members_first_is_the_order_before_2_9_2(wall):
    wall.env.limits["coding_route"] = "members_first"
    async with client() as c:
        assert (await lookup(c))["route"]


@pytest.mark.parametrize("raw, expected", [("wall_first", "wall_first"), ("MEMBERS_FIRST", "members_first"),
                                           ("members-first", "wall_first"), ("", "wall_first")])
def test_the_setting_is_read_and_a_typo_keeps_the_default(isolated_config, raw, expected):
    from tests.test_auto_swap import _limits_from
    assert _limits_from(isolated_config, f"CODING_ROUTE={raw}\n")["coding_route"] == expected


def test_the_default_is_the_wall_first(isolated_config):
    assert engine_config.get_worker_limits()["coding_route"] == "wall_first"
