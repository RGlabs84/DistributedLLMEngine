"""
Who goes first on the coding engine, the CPU contributors' rolling limit, and a free trial
that cannot be reset (engine/priority.py and NonContribBlockerMiddleware).

Owner's rules: GPU contributors first, CPU contributors second (100 coding chats per 5-hour
window), everyone else on 200 free chats, once, whatever they clear or rotate.
"""

import asyncio

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from engine import coordinator
from engine import config as engine_config
from engine.priority import CodingGate, UsageWindow, tier_priority


# ======================================================================================
# CodingGate
# ======================================================================================

def test_tier_order_is_gpu_then_cpu_then_trial():
    assert tier_priority("gpu") == tier_priority("key") == tier_priority("host") == 0
    assert tier_priority("cpu") == 1
    assert tier_priority("trial") == 2
    assert tier_priority(None) == tier_priority("made-up") == 2


def test_a_freed_slot_goes_to_the_highest_tier_waiting():
    async def scenario():
        gate = CodingGate(slots=1)
        await gate.acquire(2)                       # someone is being served
        order = []

        async def wait(name, priority):
            await gate.acquire(priority)
            order.append(name)

        # Arrive worst-first: trial, then cpu, then gpu.
        tasks = [asyncio.create_task(wait("trial", 2))]
        await asyncio.sleep(0)
        tasks.append(asyncio.create_task(wait("cpu", 1)))
        await asyncio.sleep(0)
        tasks.append(asyncio.create_task(wait("gpu", 0)))
        await asyncio.sleep(0)
        assert gate.stats()["waiting"] == 3
        for _ in range(3):
            gate.release()
            await asyncio.sleep(0)
        await asyncio.gather(*tasks)
        return order

    assert asyncio.run(scenario()) == ["gpu", "cpu", "trial"]


def test_same_tier_is_first_come_first_served():
    async def scenario():
        gate = CodingGate(slots=1)
        await gate.acquire(1)
        order = []

        async def wait(name):
            await gate.acquire(1)
            order.append(name)

        tasks = []
        for name in ("a", "b", "c"):
            tasks.append(asyncio.create_task(wait(name)))
            await asyncio.sleep(0)
        for _ in range(3):
            gate.release()
            await asyncio.sleep(0)
        await asyncio.gather(*tasks)
        return order

    assert asyncio.run(scenario()) == ["a", "b", "c"]


def test_free_slots_are_taken_without_waiting_and_nobody_is_preempted():
    async def scenario():
        gate = CodingGate(slots=2)
        await gate.acquire(2)
        await gate.acquire(2)                       # two trial users fill both slots
        waiter = asyncio.create_task(gate.acquire(0))
        await asyncio.sleep(0.01)
        assert not waiter.done(), "a GPU contributor waits for a slot; it never takes one mid-answer"
        gate.release()
        await asyncio.wait_for(waiter, 1)
        return gate.stats()

    assert asyncio.run(scenario())["in_use"] == 2


def test_a_request_that_gives_up_waiting_is_skipped():
    async def scenario():
        gate = CodingGate(slots=1)
        await gate.acquire(0)
        quitter = asyncio.create_task(gate.acquire(0))
        stayer = asyncio.create_task(gate.acquire(1))
        await asyncio.sleep(0)
        quitter.cancel()
        await asyncio.sleep(0)
        gate.release()
        await asyncio.wait_for(stayer, 1)
        return gate.stats()

    stats = asyncio.run(scenario())
    assert stats["in_use"] == 1 and stats["waiting"] == 0


def test_the_gate_follows_the_engines_slot_count():
    async def scenario():
        gate = CodingGate(slots=1)
        await gate.acquire(2)
        waiters = [asyncio.create_task(gate.acquire(2)) for _ in range(3)]
        await asyncio.sleep(0)
        await gate.acquire(0, slots=6)              # the engine came back with six lanes
        await asyncio.wait_for(asyncio.gather(*waiters), 1)
        grown = gate.stats()
        gate.resize(2)                              # and back to two: slots retire as freed
        for _ in range(4):
            gate.release()
        return grown, gate.stats()

    grown, shrunk = asyncio.run(scenario())
    assert grown["in_use"] == 5 and grown["slots"] == 6
    assert shrunk["in_use"] == 1 and shrunk["slots"] == 2


def test_a_new_event_loop_starts_clean():
    gate = CodingGate(slots=1)

    async def hold():
        await gate.acquire(0)

    asyncio.run(hold())
    asyncio.run(asyncio.wait_for(hold(), 1))     # would hang if the old loop's slot leaked


# ======================================================================================
# UsageWindow
# ======================================================================================

def test_the_window_allows_the_limit_then_says_when_it_resets():
    w = UsageWindow()
    results = [w.check_and_record("n1", 3, 100, now=t) for t in (0, 10, 20)]
    assert [r[:2] for r in results] == [(True, 2), (True, 1), (True, 0)]
    allowed, remaining, reset_at = w.check_and_record("n1", 3, 100, now=30)
    assert (allowed, remaining, reset_at) == (False, 0, 100)


def test_uses_age_out_one_at_a_time_like_the_big_services():
    w = UsageWindow()
    for t in (0, 10, 20):
        w.check_and_record("n1", 3, 100, now=t)
    assert w.check_and_record("n1", 3, 100, now=100.5)[0] is True     # the use at 0 aged out
    assert w.check_and_record("n1", 3, 100, now=101)[0] is False      # the one at 10 hasn't
    assert w.peek("n1", 3, 100, now=105) == (0, 110)      # uses at 10, 20, 100.5 still count
    assert w.peek("n1", 3, 100, now=150) == (2, 200.5)    # only 100.5 is still inside


def test_each_contributor_has_its_own_window_and_idle_ones_are_forgotten():
    w = UsageWindow()
    w.check_and_record("a", 1, 100, now=0)
    assert w.check_and_record("b", 1, 100, now=0)[0] is True
    w.forget_idle(100, now=500)
    assert w.peek("a", 1, 100, now=500) == (1, None)
    assert w._uses == {}


# ======================================================================================
# The free trial's identity
# ======================================================================================

@pytest.mark.parametrize("ip, identity", [
    ("198.51.100.7", "198.51.100.7"),
    ("2001:db8:1:2:aaaa::1", "2001:db8:1:2::/64"),
    ("2001:db8:1:2:ffff:ffff:ffff:ffff", "2001:db8:1:2::/64"),   # same /64: same trial
    ("2001:db8:1:3::1", "2001:db8:1:3::/64"),
    ("::ffff:198.51.100.7", "198.51.100.7"),
    ("unknown", "unknown"),
])
def test_the_trial_belongs_to_the_network_address(ip, identity):
    assert coordinator.trial_identity(ip) == identity


def test_the_limits_are_the_owners():
    limits = engine_config.get_worker_limits()
    assert limits["cpu_coding_limit"] == 100
    assert limits["cpu_coding_window_hours"] == 5.0
    assert limits["starter_quota_inferences"] == 200


@pytest.mark.parametrize("raw, limit, hours", [
    ({"CPU_CODING_LIMIT": "40", "CPU_CODING_WINDOW_HOURS": "3"}, 40, 3.0),
    ({"CPU_CODING_LIMIT": "0", "CPU_CODING_WINDOW_HOURS": "0"}, 100, 5.0),
    ({"CPU_CODING_LIMIT": "lots", "CPU_CODING_WINDOW_HOURS": "999"}, 100, 5.0),
])
def test_the_cpu_limit_is_configurable(monkeypatch, raw, limit, hours):
    monkeypatch.setattr(engine_config, "get_config", lambda force_reload=False: raw)
    limits = engine_config.get_worker_limits()
    assert (limits["cpu_coding_limit"], limits["cpu_coding_window_hours"]) == (limit, hours)


# ======================================================================================
# Through the real middleware
# ======================================================================================

GPU_NODE = {"worker_id": "gpu-node", "hardware": {"gpu": {"vram_total_gb": 8}, "ram_gb": 16, "cpu_cores": 8}}
CPU_NODE = {"worker_id": "cpu-node", "hardware": {
    "gpu": {"has_gpu": False, "vram_total_gb": 0}, "ram_gb": 12, "cpu_cores": 6, "cpu_arch": "x86_64",
    "cpu_features": ["avx2", "fma"], "mem_bandwidth_gbs": 48.0}}
REMOTE = ("198.51.100.40", 40000)


@pytest.fixture
def app(tmp_path, monkeypatch):
    """The real middleware in front of a route that reports the tier it was handed."""
    mgr = coordinator.StarterQuotaManager(str(tmp_path / "q.json"), default_quota=3)
    monkeypatch.setattr(coordinator, "starter_quota_mgr", mgr)
    monkeypatch.setattr(coordinator, "cpu_coding_window", UsageWindow())
    real = coordinator.get_worker_limits
    monkeypatch.setattr(coordinator, "get_worker_limits", lambda: {
        **real(), "enforce_contrib_clause": True, "starter_quota_inferences": 3, "cpu_coding_limit": 2})
    tokens = {"gpu-token": GPU_NODE, "cpu-token": CPU_NODE}
    monkeypatch.setattr(coordinator.registry, "find_by_token", lambda t: tokens.get(t))
    monkeypatch.setattr(coordinator.api_key_manager, "verify_key", lambda t: t == "dlne_sk_good")

    test_app = FastAPI()
    test_app.add_middleware(coordinator.NonContribBlockerMiddleware)

    @test_app.post("/v1/chat/completions")
    async def chat(request: Request):
        return {"tier": getattr(request.state, "dlne_tier", None)}

    @test_app.post("/v1/completions")
    async def complete(request: Request):
        return {"tier": getattr(request.state, "dlne_tier", None)}

    test_app.state.quota = mgr
    return test_app


def _client(app, token=None, ip=REMOTE):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return AsyncClient(transport=ASGITransport(app=app, client=ip), base_url="http://t", headers=headers)


@pytest.mark.anyio
@pytest.mark.parametrize("token, ip, tier", [
    ("gpu-token", REMOTE, "gpu"), ("cpu-token", REMOTE, "cpu"), ("dlne_sk_good", REMOTE, "key"),
    (None, REMOTE, "trial"), (None, ("127.0.0.1", 1), "host"),
])
async def test_the_endpoint_is_handed_the_callers_tier(app, token, ip, tier):
    async with _client(app, token, ip) as c:
        assert (await c.post("/v1/chat/completions")).json() == {"tier": tier}


@pytest.mark.anyio
async def test_a_cpu_contributor_gets_its_window_then_a_clear_429(app):
    async with _client(app, "cpu-token") as c:
        first = await c.post("/v1/chat/completions")
        second = await c.post("/v1/chat/completions")
        third = await c.post("/v1/chat/completions")
    assert first.headers["x-dlne-limit-remaining"] == "1"
    assert second.headers["x-dlne-limit-remaining"] == "0"
    assert third.status_code == 429
    assert int(third.headers["retry-after"]) > 0
    detail = third.json()["detail"]
    assert "all 2 coding chats" in detail and "frees up in" in detail and "GPU removes the limit" in detail


@pytest.mark.anyio
async def test_a_cpu_contributors_autocomplete_is_never_counted(app):
    async with _client(app, "cpu-token") as c:
        for _ in range(10):
            assert (await c.post("/v1/completions")).status_code == 200
        assert (await c.post("/v1/chat/completions")).headers["x-dlne-limit-remaining"] == "1"


@pytest.mark.anyio
async def test_a_gpu_contributor_has_no_limit(app):
    async with _client(app, "gpu-token") as c:
        for _ in range(10):
            resp = await c.post("/v1/chat/completions")
            assert resp.status_code == 200 and "x-dlne-limit-remaining" not in resp.headers


@pytest.mark.anyio
async def test_rotating_made_up_tokens_cannot_reset_the_free_trial(app):
    """The hole: a new random bearer per request used to mean a fresh 200 chats."""
    statuses = []
    for i in range(6):
        async with _client(app, f"random-{i}") as c:
            statuses.append((await c.post("/v1/chat/completions")).status_code)
    assert statuses == [200, 200, 200, 403, 403, 403]


@pytest.mark.anyio
async def test_rotating_ipv6_addresses_inside_one_block_cannot_reset_it(app):
    statuses = []
    for i in range(5):
        async with _client(app, ip=(f"2001:db8:9:9::{i + 1}", 1)) as c:
            statuses.append((await c.post("/v1/chat/completions")).status_code)
    assert statuses == [200, 200, 200, 403, 403]


@pytest.mark.anyio
async def test_the_spent_trial_survives_a_coordinator_restart(app, tmp_path):
    async with _client(app) as c:
        for _ in range(3):
            await c.post("/v1/chat/completions")
    reloaded = coordinator.StarterQuotaManager(str(tmp_path / "q.json"), default_quota=3)
    assert reloaded.peek(REMOTE[0], limit=3) == (False, 3, 0)
