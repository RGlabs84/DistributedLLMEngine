"""
The CPU pool's coordinator side (engine/app_pool.py).

What correct means here:
  * tiers are listed smallest first, each with min_ram_gb; a tier unlocks once at least
    ``app_tier_min_members`` members can host it, and the smallest is always on;
  * each member serves min(its biggest capable tier, the unlocked tier);
  * routing prefers free capacity, then the unlocked tier, then bigger tiers, then lower load;
  * a member failing before its first byte is skipped (up to 3 attempts); after the first byte
    the stream ends with an in-band notice; in-flight counters always return to zero;
  * the throttle is one active answer per network plus N requests a minute;
  * the coordinator host's own member is CPU only, loopback only, threads pinned.

The stream() tests run against real llama-server stand-ins on loopback sockets
(tests/app_pool_fakes.py). Tests marked xfail(strict=True) demonstrate bugs.
"""

import asyncio
import builtins
import hashlib
import json
import os
import re
import time
from types import SimpleNamespace

import httpx
import pytest

from engine import app_pool as ap
from engine.app_pool import AppPool, LocalAppServer, Member, app_server_args, sha256_ok
from tests.app_pool_fakes import (
    DONE, ClosingServer, FakeLlamaServer, HangingServer, free_port, notice_texts,
    recorded_starts, sse, sse_events, wait_for, write_fake_llama_binary,
)

T0 = {"id": "small", "name": "Small 4B", "min_ram_gb": 8}
T1 = {"id": "medium", "name": "Medium 14B", "min_ram_gb": 16}
T2 = {"id": "large", "name": "Large 30B-A3B", "min_ram_gb": 24}

BUSY = "No machine in the CPU pool is free right now"
DROPPED = "The machine answering this dropped out"


def pool_with(*tiers) -> AppPool:
    pool = AppPool()
    pool.load_tiers([dict(t) for t in tiers])
    return pool


def member(key, ram=12.0, serving="small", ready=True, slots=2, base_url="http://127.0.0.1:9"):
    return Member(key, ram, serving, ready, slots, base_url)


# ======================================================================================
# Tiers
# ======================================================================================

def test_load_tiers_keeps_only_objects_with_an_id_in_order():
    pool = AppPool()
    pool.load_tiers([T0, "junk", None, {"name": "no id"}, {"id": ""}, T1, 7])
    assert [t["id"] for t in pool.tiers] == ["small", "medium"]
    pool.load_tiers([])
    assert pool.tiers == []


def test_tier_index():
    pool = pool_with(T0, T1, T2)
    assert pool._tier_index("small") == 0
    assert pool._tier_index("large") == 2
    assert pool._tier_index("missing") == -1
    assert pool._tier_index(None) == -1


@pytest.mark.parametrize("ram, expected", [
    (0, -1), (-4, -1), (7.99, -1), (8, 0), (15.9, 0), (16, 1), (23.99, 1), (24, 2), (2048, 2),
    (float("nan"), -1),
])
def test_capable_index_is_the_biggest_tier_the_ram_reaches(ram, expected):
    assert pool_with(T0, T1, T2).capable_index(ram) == expected


def test_capable_index_with_no_tiers():
    assert AppPool().capable_index(512) == -1


def test_active_index_with_no_tiers_is_minus_one():
    assert AppPool().active_index([member("a", 64)], 2) == -1


def test_the_smallest_tier_is_on_with_no_members_at_all():
    assert pool_with(T0, T1, T2).active_index([], 2) == 0


def test_one_big_member_does_not_unlock_a_bigger_tier_by_default():
    assert pool_with(T0, T1, T2).active_index([member("a", 64)], 2) == 0


def test_two_members_able_to_host_it_unlock_the_biggest_tier():
    assert pool_with(T0, T1, T2).active_index([member("a", 64), member("b", 24)], 2) == 2


def test_a_tier_unlocks_when_enough_members_can_host_it_or_more():
    # Both can host medium (one could host large): medium is the biggest two can reach.
    assert pool_with(T0, T1, T2).active_index([member("a", 64), member("b", 16)], 2) == 1


@pytest.mark.parametrize("min_members", [1, 0, -5])
def test_a_threshold_below_one_counts_as_one(min_members):
    assert pool_with(T0, T1, T2).active_index([member("a", 32)], min_members) == 2


def test_members_lending_no_ram_never_count_toward_an_unlock():
    pool = pool_with(T0, T1, T2)
    members = [member("z1", 0), member("z2", 0), member("a", 32)]
    assert pool.active_index(members, 2) == 0
    assert pool.status(members, 2)["tiers"][0]["members_able"] == 1


def test_a_higher_threshold():
    pool = pool_with(T0, T1, T2)
    assert pool.active_index([member("a", 32), member("b", 32), member("c", 16)], 3) == 1
    assert pool.active_index([member("a", 32), member("b", 32), member("c", 24)], 3) == 2


def test_assignment_without_tiers_is_none():
    assert AppPool().assignment(64, [member("a", 64)], 2) is None


@pytest.mark.parametrize("ram", [0, 4, 7.9])
def test_a_member_that_can_host_nothing_is_told_none(ram):
    pool = pool_with(T0, T1, T2)
    assert pool.assignment(ram, [member("a", 32), member("b", 32)], 2) is None


def test_while_the_bigger_tier_is_locked_a_big_member_serves_the_unlocked_one():
    pool = pool_with(T0, T1, T2)
    assert pool.assignment(64, [member("a", 64)], 2) == "small"


def test_once_unlocked_big_members_move_up_and_small_ones_stay_as_overflow():
    pool = pool_with(T0, T1, T2)
    members = [member("a", 32), member("b", 24), member("c", 16), member("d", 8)]
    assert pool.active_index(members, 2) == 2
    assert pool.assignment(32, members, 2) == "large"
    assert pool.assignment(16, members, 2) == "medium"
    assert pool.assignment(8, members, 2) == "small"


def test_a_member_is_capped_at_the_unlocked_tier():
    pool = pool_with(T0, T1, T2)
    members = [member("a", 64), member("b", 16)]
    assert pool.assignment(64, members, 2) == "medium"


def test_a_single_tier_pool():
    pool = pool_with(T0)
    assert pool.assignment(64, [], 2) == "small"
    assert pool.status([], 2)["active_tier"] == "small"


# ======================================================================================
# Members (from registry records + the reverse tunnel's relay ports)
# ======================================================================================

class FakeRegistry:
    def __init__(self, records):
        self.records = records

    def app_members(self):
        return [dict(r) for r in self.records]


def _record(worker_id, ip, ram, app, port=50053, **extra):
    hardware = {"pool": "app", "ram_gb": ram, **extra}
    if app is not None:
        hardware["app"] = app
    return {"worker_id": worker_id, "ip": ip, "port": port, "hardware": hardware}


def test_members_from_registry_records_and_relay_ports():
    records = [
        _record("w1", "10.0.0.1", 12.0, {"model": "small", "ready": True, "slots": 4}),
        _record("w2", "10.0.0.2", 32.0, {"model": "large", "ready": True, "slots": 2}),   # no tunnel
        _record("w3", "10.0.0.3", 16.0, {"model": "medium", "ready": False, "slots": 2}),
        _record("w4", "10.0.0.4", 16.0, {"model": 5, "ready": "yes", "slots": 0}),
        {"worker_id": "w5", "ip": "10.0.0.5", "port": 50053, "hardware": "not a dict"},
    ]
    relays = {("10.0.0.1", 50053): 40001, ("10.0.0.3", 50053): 40003, ("10.0.0.4", 50053): 40004,
              ("10.0.0.5", 50053): 40005}
    asked = []

    def relay_lookup(ip, port):
        asked.append((ip, port))
        return relays.get((ip, port))

    found = {m.key: m for m in AppPool().members(FakeRegistry(records), relay_lookup)}
    assert sorted(found) == ["worker:w1", "worker:w2", "worker:w3", "worker:w4", "worker:w5"]
    assert asked == [("10.0.0.1", 50053), ("10.0.0.2", 50053), ("10.0.0.3", 50053),
                     ("10.0.0.4", 50053), ("10.0.0.5", 50053)]

    w1 = found["worker:w1"]
    assert (w1.ram_gb, w1.serving, w1.ready, w1.slots, w1.base_url, w1.local) == \
        (12.0, "small", True, 4, "http://127.0.0.1:40001", False)
    # Ready but unreachable: no tunnel, so not ready and no URL.
    assert (found["worker:w2"].ready, found["worker:w2"].base_url) == (False, None)
    assert found["worker:w3"].ready is False and found["worker:w3"].base_url == "http://127.0.0.1:40003"
    # Malformed app fields degrade to "not serving, not ready, one slot".
    w4 = found["worker:w4"]
    assert (w4.serving, w4.ready, w4.slots) == (None, False, 1)
    w5 = found["worker:w5"]
    assert (w5.ram_gb, w5.serving, w5.ready, w5.slots) == (0.0, None, False, 1)


def test_the_local_member_is_appended():
    pool = AppPool()
    pool.local = SimpleNamespace(as_member=lambda: Member("local", 8.0, "small", True, 2,
                                                          "http://127.0.0.1:8099", local=True))
    found = pool.members(FakeRegistry([_record("w1", "10.0.0.1", 12.0, {"model": "small", "ready": True})]),
                         lambda ip, port: 40001)
    assert [m.key for m in found] == ["worker:w1", "local"]
    assert found[-1].local is True


def test_member_slots_are_at_least_one():
    assert Member("k", 8, None, True, 0, None).slots == 1
    assert Member("k", 8, None, True, -3, None).slots == 1


# ======================================================================================
# Routing
# ======================================================================================

def test_pick_returns_none_when_nobody_is_ready():
    pool = pool_with(T0, T1)
    assert pool.pick([], "small", []) is None
    assert pool.pick([member("a", ready=False), member("b", base_url=None)], "small", []) is None


def test_pick_skips_excluded_members():
    pool = pool_with(T0, T1)
    a, b = member("a"), member("b")
    assert pool.pick([a, b], "small", ["a"]) is b
    assert pool.pick([a, b], "small", ["a", "b"]) is None


def test_free_capacity_comes_before_the_unlocked_tier():
    pool = pool_with(T0, T1)
    busy_big = member("big", ram=16, serving="medium", slots=1)
    free_small = member("small", ram=8, serving="small", slots=1)
    pool._inflight["big"] = 1
    assert pool.pick([busy_big, free_small], "medium", []) is free_small


def test_among_free_members_the_unlocked_tier_comes_first():
    pool = pool_with(T0, T1)
    on_small = member("s", serving="small")
    on_medium = member("m", serving="medium")
    pool._inflight["m"] = 1          # half loaded, still free
    assert pool.pick([on_small, on_medium], "medium", []) is on_medium


def test_after_the_unlocked_tier_bigger_tiers_come_first():
    pool = pool_with(T0, T1, T2)
    small, large = member("s", serving="small"), member("l", serving="large")
    assert pool.pick([small, large], "medium", []) is large


def test_within_a_tier_the_least_loaded_wins():
    pool = pool_with(T0, T1)
    a, b = member("a", serving="medium", slots=4), member("b", serving="medium", slots=4)
    pool._inflight.update({"a": 2, "b": 1})
    assert pool.pick([a, b], "medium", []) is b


def test_when_everyone_is_full_the_least_loaded_still_gets_it():
    pool = pool_with(T0)
    a, b = member("a", slots=1), member("b", slots=1)
    pool._inflight.update({"a": 3, "b": 1})
    assert pool.pick([a, b], "small", []) is b


def test_members_serving_a_model_no_tier_lists_rank_last():
    pool = pool_with(T0, T1)
    stale = member("stale", serving="retired-model")
    small = member("s", serving="small")
    assert pool.pick([stale, small], "medium", []) is small
    assert pool.pick([stale], "medium", []) is stale


# ======================================================================================
# Throttle
# ======================================================================================

def test_one_answer_at_a_time_per_network():
    pool = AppPool()
    assert pool.admit("198.51.100.7", 6) == (True, "")
    allowed, why = pool.admit("198.51.100.7", 6)
    assert not allowed and "One answer at a time" in why


def test_networks_are_throttled_independently():
    pool = AppPool()
    assert pool.admit("198.51.100.7", 6)[0]
    assert pool.admit("198.51.100.8", 6)[0]
    assert pool.admit("2001:db8:1:2::/64", 6)[0]


def test_finished_frees_the_network_and_never_goes_negative():
    pool = AppPool()
    assert pool.admit("n", 6)[0]
    pool.finished("n")
    assert pool._active_by_network == {}
    pool.finished("n")
    pool.finished("never-admitted")
    assert pool._active_by_network == {}
    assert pool.admit("n", 6)[0]
    assert pool._active_by_network == {"n": 1}


def test_requests_per_minute_are_limited():
    pool = AppPool()
    for _ in range(3):
        assert pool.admit("n", 3) == (True, "")
        pool.finished("n")
    allowed, why = pool.admit("n", 3)
    assert not allowed and why.startswith("Slow down")
    wait = int(re.search(r"try again in (\d+) seconds", why).group(1))
    assert 1 <= wait <= 60
    assert pool._active_by_network == {}


def test_a_refusal_for_concurrency_does_not_spend_a_minute_slot():
    pool = AppPool()
    assert pool.admit("n", 2)[0]
    for _ in range(5):
        assert pool.admit("n", 2)[0] is False
    pool.finished("n")
    assert pool.admit("n", 2)[0], "the second of two per minute is still available"


def test_the_minute_window_slides():
    pool = AppPool()
    for _ in range(2):
        assert pool.admit("n", 2)[0]
        pool.finished("n")
    assert pool.admit("n", 2)[0] is False
    for uses in pool.throttle_window._uses.values():
        for i in range(len(uses)):
            uses[i] -= 61.0
    assert pool.admit("n", 2)[0]


def test_the_throttle_forgets_networks_that_went_quiet():
    pool = AppPool()
    for i in range(500):
        net = f"198.51.{i // 256}.{i % 256}"
        assert pool.admit(net, 6)[0]
        pool.finished(net)
    for uses in pool.throttle_window._uses.values():
        for i in range(len(uses)):
            uses[i] -= 3600.0
    assert pool.admit("203.0.113.1", 6)[0]
    pool.finished("203.0.113.1")
    assert len(pool.throttle_window._uses) <= 10


# ======================================================================================
# Status
# ======================================================================================

def test_status_with_no_tiers_and_no_members():
    assert AppPool().status([], 2) == {
        "members": 0, "members_ready": 0, "active_tier": None, "active_model": None,
        "tiers": [], "requests_in_flight": 0, "median_answer_seconds": None,
        "serving": None, "engine_requests_in_flight": 0,
    }


def test_status_reports_the_ladder():
    pool = pool_with(T0, T1, T2)
    members = [member("a", 32), member("b", 32, ready=False), member("c", 8)]
    status = pool.status(members, 2)
    assert status["members"] == 3 and status["members_ready"] == 2
    assert status["active_tier"] == "large" and status["active_model"] == "Large 30B-A3B"
    assert status["tiers"] == [
        {"id": "small", "name": "Small 4B", "min_ram_gb": 8, "members_able": 3, "unlocked": True},
        {"id": "medium", "name": "Medium 14B", "min_ram_gb": 16, "members_able": 2, "unlocked": True},
        {"id": "large", "name": "Large 30B-A3B", "min_ram_gb": 24, "members_able": 2, "unlocked": True},
    ]
    one_big = pool.status([member("a", 32), member("c", 8)], 2)
    assert [t["unlocked"] for t in one_big["tiers"]] == [True, False, False]
    assert one_big["active_tier"] == "small"


def test_status_counts_requests_in_flight_and_the_recent_median():
    pool = pool_with(T0)
    pool._inflight.update({"a": 2, "b": 1})
    now = time.monotonic()
    for seconds in (1.0, 3.0, 2.0):
        pool._note_speed(now - seconds)
    pool._speeds.append((time.time() - 1000, 99.0))      # older than 15 minutes: ignored
    status = pool.status([], 2)
    assert status["requests_in_flight"] == 3
    assert 1.9 <= status["median_answer_seconds"] <= 2.2
    json.dumps(status)  # what /v1/cluster/status serves must serialise


ENGINE = {"model": "Qwen3.6 35B-A3B", "tier": "qwen36-35b"}


def test_status_with_the_engine_answering_phones_counts_it_as_a_serving_machine_and_names_its_model():
    """The 2.6.0 bug: lanes on, the CPU copy off, and the status said no machine was online and "Qwen3 4B"."""
    pool = pool_with(T0, T1, T2)
    status = pool.status([], 2, engine=ENGINE)

    assert status["members"] == 1 and status["members_ready"] == 1
    assert status["active_model"] == "Qwen3.6 35B-A3B" and status["active_tier"] == "qwen36-35b"
    assert status["serving"] == "engine"
    assert [t["unlocked"] for t in status["tiers"]] == [True, True, True], "the CPU tiers are all below the engine's model"
    assert [t["members_able"] for t in status["tiers"]] == [0, 0, 0], "and they are still counted as CPU machines"
    json.dumps(status)


def test_status_with_the_engine_and_cpu_members_adds_one_machine_and_keeps_the_ladder():
    pool = pool_with(T0, T1, T2)
    status = pool.status([member("a", 32), member("b", 32, ready=False)], 2, engine=ENGINE)

    assert status["members"] == 3 and status["members_ready"] == 2
    assert status["active_model"] == "Qwen3.6 35B-A3B"


def test_status_without_the_engine_is_the_cpu_pools_alone_and_says_who_serves():
    pool = pool_with(T0, T1)
    assert pool.status([member("a", 8)], 2)["serving"] == "cpu"
    idle = pool.status([member("a", 8, ready=False)], 2)
    assert idle["serving"] is None and idle["members_ready"] == 0
    assert pool.status([member("a", 8)], 2, engine=None)["active_model"] == "Small 4B"


def test_status_says_what_in_flight_and_the_median_count():
    pool = pool_with(T0)
    pool._inflight.update({"worker:1": 1, ap.ENGINE_KEY: 2})
    status = pool.status([], 2, engine=ENGINE)

    assert status["requests_in_flight"] == 3, "model requests running now on any machine, the engine's lanes included"
    assert status["engine_requests_in_flight"] == 2
    assert "requests_in_flight" in AppPool.status.__doc__ and "median_answer_seconds" in AppPool.status.__doc__


# ======================================================================================
# stream(): forwarding with failover, over real sockets
# ======================================================================================

@pytest.fixture(scope="module")
def fakes():
    servers = {name: FakeLlamaServer(name).start() for name in ("a", "b", "c")}
    closer, hanger = ClosingServer(), HangingServer()
    yield {**servers, "closer": closer, "hanger": hanger}
    for server in servers.values():
        server.stop()
    closer.stop()
    hanger.stop()


@pytest.fixture
def srv(fakes):
    for name in ("a", "b", "c"):
        fakes[name].reset()
    fakes["closer"].connections = 0
    fakes["hanger"].connections = 0
    return fakes


def at(server, key=None, serving="small", slots=2, ready=True):
    return Member(key or f"worker:{getattr(server, 'name', 'x')}", 12.0, serving, ready, slots, server.url)


BODY = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hello"}], "temperature": 0.2}


async def run_stream(pool, body, members, active="small"):
    async with httpx.AsyncClient() as client:
        chunks = [chunk async for chunk in pool.stream(body, members, active, client)]
    return b"".join(chunks)


@pytest.mark.anyio
async def test_a_healthy_member_streams_its_answer_through(srv):
    pool = pool_with(T0)
    body = dict(BODY)
    out = await run_stream(pool, body, [at(srv["a"])])
    assert out == srv["a"].body
    sent = srv["a"].payloads[0]
    assert sent["stream"] is True and sent["max_tokens"] == ap.MAX_OUTPUT_TOKENS
    assert "model" not in sent, "the client's model name must not reach the member"
    system, *rest = sent["messages"]
    assert system["role"] == "system" and "Today is" in system["content"], "the model is told the date"
    assert rest == BODY["messages"]
    assert sent["temperature"] == ap.SAMPLING["temperature"], "sampling is the server's, not the client's"
    assert "tools" not in sent, "no lookup tools without a runner"
    assert body == BODY, "the caller's body is not mutated"
    assert pool._inflight == {}
    assert pool.status([], 2)["median_answer_seconds"] is not None


@pytest.mark.anyio
@pytest.mark.parametrize("asked, forwarded", [
    (None, 1024), (5000, 1024), (1024, 1024), (100, 100), ("300", 300), ("lots", 1024),
    (0, 1024), (True, 1024), (99.9, 99),
])
async def test_max_tokens_is_capped(srv, asked, forwarded):
    body = {"messages": [{"role": "user", "content": "hi"}]}
    if asked is not None:
        body["max_tokens"] = asked
    await run_stream(pool_with(T0), body, [at(srv["a"])])
    assert srv["a"].payloads[0]["max_tokens"] == forwarded


@pytest.mark.anyio
async def test_stream_true_is_forced(srv):
    await run_stream(pool_with(T0), {**BODY, "stream": False}, [at(srv["a"])])
    assert srv["a"].payloads[0]["stream"] is True


@pytest.mark.anyio
async def test_a_member_answering_500_is_skipped(srv):
    srv["a"].mode = "500"
    pool = pool_with(T0)
    out = await run_stream(pool, BODY, [at(srv["a"]), at(srv["b"])])
    assert out == srv["b"].body
    assert len(srv["a"].payloads) == 1 and len(srv["b"].payloads) == 1
    assert pool._inflight == {}


@pytest.mark.anyio
async def test_a_member_that_closes_before_the_first_byte_is_skipped(srv):
    pool = pool_with(T0)
    out = await run_stream(pool, BODY, [at(srv["closer"], key="worker:closer"), at(srv["b"])])
    assert out == srv["b"].body
    assert srv["closer"].connections == 1
    assert pool._inflight == {}


@pytest.mark.anyio
async def test_a_member_that_dies_after_its_headers_but_before_a_chunk_is_skipped(srv):
    srv["a"].mode = "die_before_first_chunk"
    pool = pool_with(T0)
    out = await run_stream(pool, BODY, [at(srv["a"]), at(srv["b"])])
    assert out == srv["b"].body
    assert pool._inflight == {}


@pytest.mark.anyio
async def test_a_member_refusing_connections_is_skipped(srv):
    nobody = SimpleNamespace(url=f"http://127.0.0.1:{free_port()}", name="nobody")
    pool = pool_with(T0)
    out = await run_stream(pool, BODY, [at(nobody), at(srv["b"])])
    assert out == srv["b"].body
    assert pool._inflight == {}


@pytest.mark.anyio
async def test_a_member_that_never_answers_is_abandoned_at_the_read_timeout(srv, monkeypatch):
    monkeypatch.setattr(ap, "_timeout", lambda client: httpx.Timeout(5.0, connect=2.0, read=0.5))
    pool = pool_with(T0)
    started = time.monotonic()
    out = await run_stream(pool, BODY, [at(srv["hanger"], key="worker:hanger"), at(srv["b"])])
    assert out == srv["b"].body
    assert time.monotonic() - started < 5
    assert pool._inflight == {}


@pytest.mark.anyio
async def test_three_attempts_then_the_busy_notice(srv):
    for name in ("a", "b", "c"):
        srv[name].mode = "500"
    pool = pool_with(T0)
    members = [at(srv["a"]), at(srv["b"]), at(srv["c"]), at(srv["closer"], key="worker:closer")]
    out = await run_stream(pool, BODY, members)
    texts = notice_texts(out)
    assert len(texts) == 2 and BUSY in texts[0] and texts[1] == ""
    assert sse_events(out)[-1] == "[DONE]"
    assert [len(srv[n].payloads) for n in ("a", "b", "c")] == [1, 1, 1]
    assert srv["closer"].connections == 0, "a fourth member is never tried"
    assert pool._inflight == {}


@pytest.mark.anyio
async def test_no_ready_member_means_the_busy_notice(srv):
    pool = pool_with(T0)
    for members in ([], [at(srv["a"], ready=False)]):
        out = await run_stream(pool, BODY, members)
        assert BUSY in notice_texts(out)[0]
    assert srv["a"].payloads == []


@pytest.mark.anyio
async def test_a_member_dying_mid_answer_ends_the_stream_with_a_notice(srv):
    srv["a"].mode = "die_mid_stream"
    pool = pool_with(T0)
    out = await run_stream(pool, BODY, [at(srv["a"]), at(srv["b"])])
    assert out.startswith(srv["a"].chunks[0])
    texts = notice_texts(out)
    assert DROPPED in texts[-2] and texts[-1] == ""
    assert srv["b"].payloads == [], "an answer that started is never restarted elsewhere"
    assert pool._inflight == {}


@pytest.mark.anyio
async def test_in_flight_answers_steer_the_next_pick_and_drain_to_zero(srv):
    srv["a"].mode = "hold"
    srv["a"].gate.clear()
    pool = pool_with(T0)
    a, b = at(srv["a"], slots=1), at(srv["b"], slots=1)

    async def consume():
        return await run_stream(pool, BODY, [a, b])

    task = asyncio.create_task(consume())
    for _ in range(500):
        if srv["a"].entered.is_set():
            break
        await asyncio.sleep(0.01)
    assert pool._inflight == {"worker:a": 1}
    assert pool.pick([a, b], "small", []) is b, "a full member loses to a free one"
    assert pool.status([a, b], 2)["requests_in_flight"] == 1
    srv["a"].gate.set()
    assert await task == srv["a"].body
    assert pool._inflight == {}


@pytest.mark.anyio
async def test_an_abandoned_stream_releases_its_member(srv):
    srv["a"].mode = "hold"
    srv["a"].gate.clear()
    pool = pool_with(T0)
    async with httpx.AsyncClient() as client:
        agen = pool.stream(BODY, [at(srv["a"])], "small", client)
        first = await agen.__anext__()
        assert first == srv["a"].chunks[0]
        assert pool._inflight == {"worker:a": 1}
        await agen.aclose()
    assert pool._inflight == {}
    srv["a"].gate.set()


# ---- bugs ----------------------------------------------------------------------------

@pytest.mark.anyio
@pytest.mark.parametrize("extra", [{"max_tokens": -1}, {"n_predict": 100000}, {"max_tokens": float("inf")}],
                         ids=["negative", "n_predict", "infinite"])
async def test_a_client_cannot_lift_the_output_cap(srv, extra):
    await run_stream(pool_with(T0), {**BODY, **extra}, [at(srv["a"])])
    sent = srv["a"].payloads[0]
    assert 1 <= sent["max_tokens"] <= ap.MAX_OUTPUT_TOKENS
    assert sent.get("n_predict") is None or 1 <= sent["n_predict"] <= ap.MAX_OUTPUT_TOKENS


@pytest.mark.anyio
async def test_the_dropped_out_notice_is_its_own_event_after_a_partial_one(srv):
    srv["a"].mode = "die_mid_stream"
    srv["a"].chunks = [sse("Hel"), b'data: {"choices":[{"index":0,"delta":{"content":"lo wor', DONE]
    srv["a"].die_after = 2
    out = await run_stream(pool_with(T0), BODY, [at(srv["a"])])
    parsed = []
    for payload in sse_events(out):
        try:
            parsed.append(json.loads(payload)["choices"][0]["delta"]["content"])
        except (ValueError, KeyError):
            pass
    assert any(DROPPED in text for text in parsed)


@pytest.mark.anyio
async def test_a_request_the_member_rejects_as_bad_is_not_retried_elsewhere(srv):
    srv["a"].mode = "400"
    out = await run_stream(pool_with(T0), BODY, [at(srv["a"]), at(srv["b"])])
    assert srv["b"].payloads == []
    assert BUSY not in out.decode()


def test_a_silent_member_is_abandoned_after_the_first_byte_timeout():
    assert ap._timeout(None).read <= ap.FIRST_BYTE_TIMEOUT_SECONDS


# ======================================================================================
# sha256_ok and the llama-server command line
# ======================================================================================

def test_sha256_ok(tmp_path, monkeypatch):
    data = b"gguf" + bytes(range(256)) * 4
    good = hashlib.sha256(data).hexdigest()
    path = tmp_path / "m.gguf"
    assert sha256_ok(str(path), good, len(data)) is False          # missing
    path.write_bytes(data)
    assert sha256_ok(str(path), good, len(data) + 1) is False      # wrong size
    assert not (tmp_path / "m.gguf.dlne-sha256").exists()
    assert sha256_ok(str(path), "0" * 64, len(data)) is False      # wrong hash
    assert not (tmp_path / "m.gguf.dlne-sha256").exists()
    assert sha256_ok(str(path), good, len(data)) is True
    marker = (tmp_path / "m.gguf.dlne-sha256").read_text()
    assert marker.startswith(good + f" {len(data)} ")

    # A good marker is trusted: no second hash of an 18 GB file.
    monkeypatch.setattr(ap.hashlib, "sha256", lambda: (_ for _ in ()).throw(AssertionError("rehashed")))
    assert sha256_ok(str(path), good, len(data)) is True
    monkeypatch.undo()

    # The marker is keyed by the expected hash, size and mtime: a changed file is hashed again.
    path.write_bytes(data[:-1] + b"X")
    os.utime(path, (time.time() + 5, time.time() + 5))
    assert sha256_ok(str(path), good, len(data)) is False


def test_app_server_args_are_cpu_only_and_loopback_only():
    args = app_server_args("/bin/llama-server", "/m/x.gguf", 8099, 6)
    assert args == ["/bin/llama-server", "-m", "/m/x.gguf", "--alias", "x.gguf", "--host", "127.0.0.1", "--port", "8099",
                    "-c", "16384", "-np", "2", "-t", "6", "-tb", "6", "-n", str(ap.MAX_OUTPUT_TOKENS),
                    "--device", "none", "-ngl", "0", "--jinja", "--no-webui"]


def test_app_server_args_pin_threads_with_a_mask():
    args = app_server_args("llama-server", "x.gguf", 1, 4, ctx=8192, slots=3, cpu_mask="0xf0")
    assert args[args.index("-c") + 1] == "8192" and args[args.index("-np") + 1] == "3"
    tail = args[args.index("--no-webui") + 1:]
    assert tail == ["--cpu-mask", "0xf0", "--cpu-strict", "1", "--cpu-mask-batch", "0xf0", "--cpu-strict-batch", "1"]


# ======================================================================================
# LocalAppServer: the coordinator host's own member
# ======================================================================================

def _tier(tid, data):
    return {"id": tid, "name": f"Tier {tid}", "file": f"{tid}.gguf",
            "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "min_ram_gb": 8}


@pytest.fixture
def local(tmp_path):
    models = tmp_path / "models"
    models.mkdir()
    record = str(tmp_path / "starts.jsonl")
    binary = write_fake_llama_binary(str(tmp_path / "bin" / "llama-server"), record)
    data0, data1 = b"model zero " * 50, b"model one " * 60
    (models / "t0.gguf").write_bytes(data0)
    (models / "t1.gguf").write_bytes(data1)
    resolved = []

    def resolver():
        resolved.append(1)
        return binary

    server = LocalAppServer(str(models), resolver, str(tmp_path / "logs"), free_port(), 3, 8.0, "0x3")
    env = SimpleNamespace(server=server, models=models, record=record, binary=binary,
                          t0=_tier("t0", data0), t1=_tier("t1", data1), resolved=resolved, tmp=tmp_path)
    yield env
    server.stop()


async def _wait_ready(server, timeout=20.0):
    async with httpx.AsyncClient() as client:
        return await server.wait_ready(client, timeout=timeout)


def test_local_ensure_none_without_a_process_is_a_no_op(local):
    local.server.ensure(None)
    assert local.server.process is None and local.server.serving is None
    assert local.resolved == []


def test_local_missing_model_is_a_problem_not_a_start(local):
    os.remove(local.models / "t0.gguf")
    local.server.ensure(local.t0)
    assert local.server.process is None
    assert "t0.gguf" in local.server.problem
    assert local.resolved == [], "the binary is not even looked for"


def test_local_mismatched_model_is_refused(local):
    (local.models / "t0.gguf").write_bytes(b"X" * local.t0["size"])
    local.server.ensure(local.t0)
    assert local.server.process is None and "does not match" in local.server.problem


def test_local_missing_binary_is_reported(local):
    local.server.binary_resolver = lambda: None
    local.server.ensure(local.t0)
    assert local.server.process is None and local.server.problem == "no llama-server binary"


def test_local_member_starts_pinned_and_cpu_only_and_becomes_ready(local):
    srv = local.server
    srv.ensure(local.t0)
    assert srv.serving == "t0" and srv.problem is None
    assert wait_for(lambda: recorded_starts(local.record))
    args = recorded_starts(local.record)[0]
    assert [local.binary] + args == app_server_args(local.binary, str(local.models / "t0.gguf"), srv.port, 3,
                                                    cpu_mask="0x3")
    for flag, value in (("--device", "none"), ("-ngl", "0"), ("--host", "127.0.0.1"),
                        ("--cpu-mask", "0x3"), ("--cpu-strict", "1"), ("--alias", "t0.gguf")):
        assert args[args.index(flag) + 1] == value
    assert srv.as_member().ready is False, "not ready until /health answers"
    assert asyncio.run(_wait_ready(srv)) is True
    m = srv.as_member()
    assert (m.key, m.ready, m.local, m.slots, m.base_url, m.serving, m.ram_gb) == \
        ("local", True, True, 2, f"http://127.0.0.1:{srv.port}", "t0", 8.0)
    assert (local.tmp / "logs" / "app_server.log").exists()


def test_local_same_tier_alive_is_left_alone_and_another_tier_restarts(local):
    srv = local.server
    srv.ensure(local.t0)
    first = srv.process
    assert wait_for(lambda: len(recorded_starts(local.record)) == 1)
    srv.ensure(local.t0)
    assert srv.process is first
    srv.ensure(local.t1)
    assert srv.process is not first and first.poll() is not None, "the old server was stopped"
    assert srv.serving == "t1"
    assert wait_for(lambda: len(recorded_starts(local.record)) == 2)
    assert recorded_starts(local.record)[1][1].endswith("t1.gguf")


def test_local_dead_process_is_not_ready_and_is_restarted(local):
    srv = local.server
    srv.ensure(local.t0)
    assert asyncio.run(_wait_ready(srv))
    srv.process.kill()
    srv.process.wait(timeout=5)
    assert srv.as_member().ready is False
    srv.ensure(local.t0)
    assert srv.process.poll() is None and srv.ready is False
    assert asyncio.run(_wait_ready(srv))


def test_local_ensure_none_and_stop_terminate_the_server(local):
    srv = local.server
    srv.ensure(local.t0)
    proc = srv.process
    srv.ensure(None)
    assert proc.poll() is not None and srv.process is None and srv.serving is None
    srv.ensure(local.t0)
    proc = srv.process
    srv.stop()
    assert proc.poll() is not None and srv.process is None and srv.ready is False


def test_local_wait_ready_is_false_when_the_server_exits(local):
    write_fake_llama_binary(local.binary, local.record, crash=True)
    local.server.ensure(local.t0)
    assert asyncio.run(_wait_ready(local.server, timeout=10)) is False


def test_local_unverifiable_new_tier_keeps_the_old_one_serving(local):
    srv = local.server
    srv.ensure(local.t0)
    os.remove(local.models / "t1.gguf")
    srv.ensure(local.t1)
    assert srv.process is not None and srv.process.poll() is None
    assert srv.serving == "t0"


def test_local_server_does_not_leak_its_log_handle(local, monkeypatch):
    opened = []

    def tracking_open(path, *args, **kwargs):
        handle = builtins.open(path, *args, **kwargs)
        if str(path).endswith("app_server.log"):
            opened.append(handle)
        return handle

    monkeypatch.setattr(ap, "open", tracking_open, raising=False)
    local.server.ensure(local.t0)
    local.server.ensure(local.t1)
    local.server.stop()
    assert opened and all(handle.closed for handle in opened)


def test_local_mismatched_model_is_not_rehashed_every_tick(local, monkeypatch):
    (local.models / "t0.gguf").write_bytes(b"X" * local.t0["size"])
    calls = []
    real = hashlib.sha256
    monkeypatch.setattr(ap.hashlib, "sha256", lambda *a: calls.append(1) or real(*a))
    local.server.ensure(local.t0)
    local.server.ensure(local.t0)
    local.server.ensure(local.t0)
    assert len(calls) == 1
