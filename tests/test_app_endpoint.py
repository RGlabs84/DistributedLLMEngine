"""
The coordinator's CPU pool surface: POST /v1/app/chat/completions (the phone app's chat) and
the heartbeat answer that tells each pool member which model tier to serve.

The endpoint is driven through the real ASGI app (httpx ASGITransport, so the real middleware
stack runs) against llama-server stand-ins on real loopback sockets. Members come from a real
WorkerRegistry plus a stand-in for the reverse tunnel's relay-port lookup, so
coordinator._app_members() and AppPool.members() run unmodified.
"""

import asyncio
import json
import re
import socket
import threading
import time
from types import SimpleNamespace

import pytest
import uvicorn
from httpx import ASGITransport, AsyncClient

from engine import coordinator, reverse_tunnel
from engine import config as engine_config
from engine.app_pool import AppPool
from engine.registry import WorkerRegistry
from engine.version import VERSION
from tests.app_pool_fakes import FakeLlamaServer, HangingServer, free_port, notice_texts, wait_for

REMOTE = ("203.0.113.9", 40000)
TIERS = [
    {"id": "small", "name": "Small 4B", "file": "s.gguf", "min_ram_gb": 8},
    {"id": "large", "name": "Large 30B", "file": "l.gguf", "min_ram_gb": 24},
]
CHAT = {"messages": [{"role": "user", "content": "hello"}], "model": "anything"}
QUALIFYING = {
    "gpu": {"has_gpu": False, "vram_total_gb": 0},
    "ram_gb": 12.0, "cpu_cores": 6, "cpu_threads": 12,
    "cpu_arch": "x86_64", "cpu_features": ["avx2", "fma", "f16c"],
    "mem_bandwidth_gbs": 48.0, "is_yielding": False,
}


@pytest.fixture(scope="module")
def fakes():
    servers = {name: FakeLlamaServer(name).start() for name in ("a", "b")}
    hanger = HangingServer()
    yield {**servers, "hanger": hanger}
    for server in servers.values():
        server.stop()
    hanger.stop()


@pytest.fixture
def env(fakes, monkeypatch):
    for name in ("a", "b"):
        fakes[name].reset()
    pool = AppPool()
    pool.load_tiers([dict(t) for t in TIERS])
    monkeypatch.setattr(coordinator, "app_pool", pool)
    registry = WorkerRegistry()
    monkeypatch.setattr(coordinator, "registry", registry)
    relays = {}
    monkeypatch.setattr(reverse_tunnel, "reverse_bridge",
                        SimpleNamespace(get_relay_port=lambda ip, port: relays.get((ip, port))))
    limits = {
        "trust_proxy_headers": False, "enforce_contrib_clause": True, "starter_quota_inferences": 200,
        "app_rate_per_minute": 6, "app_tier_min_members": 2,
        "network_key": "", "allow_unsigned_workers": True, **engine_config.CPU_NODE_DEFAULTS,
    }
    monkeypatch.setattr(coordinator, "get_worker_limits", lambda: dict(limits))
    monkeypatch.setattr(coordinator, "_engine_http", None)
    count = {"n": 0}

    def add(server, ram=12.0, model="small", ready=True):
        count["n"] += 1
        ip = f"10.8.0.{count['n'] + 1}"
        registry.update_worker(f"w{count['n']}", ip, 50053, f"tok{count['n']}",
                               {"pool": "app", "ram_gb": ram, "gpu": None,
                                "app": {"model": model, "ready": ready, "slots": 2}})
        relays[(ip, 50053)] = server.port

    return SimpleNamespace(pool=pool, registry=registry, add=add, limits=limits, fakes=fakes)


def client(ip=REMOTE):
    return AsyncClient(transport=ASGITransport(app=coordinator.app, client=ip), base_url="http://test")


async def post(c, body=CHAT, raw=None):
    content = raw if raw is not None else json.dumps(body)
    return await c.post("/v1/app/chat/completions", content=content, headers={"Content-Type": "application/json"})


# ======================================================================================
# Bodies
# ======================================================================================

@pytest.mark.anyio
@pytest.mark.parametrize("raw", ["{", "", "null", "[]", "{}", '{"messages": []}', '{"messages": "hi"}',
                                 '{"messages": {"role": "user"}}', '"messages"'])
async def test_a_bad_body_is_400_and_spends_nothing(env, raw):
    env.add(env.fakes["a"])
    async with client() as c:
        for _ in range(8):
            resp = await post(c, raw=raw)
            assert resp.status_code == 400
            assert resp.json()["detail"]
        resp = await post(c)
    assert resp.status_code == 200, "eight bad bodies did not use up the six-a-minute allowance"
    assert env.fakes["a"].payloads and env.pool._active_by_network == {}


# ======================================================================================
# Streaming
# ======================================================================================

@pytest.mark.anyio
async def test_the_members_answer_streams_through(env):
    env.add(env.fakes["a"])
    async with client() as c:
        resp = await post(c)
    assert resp.status_code == 200
    assert resp.content == env.fakes["a"].body
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["cache-control"] == "no-store" and resp.headers["x-accel-buffering"] == "no"
    sent = env.fakes["a"].payloads[0]
    assert "model" not in sent and sent["stream"] is True and sent["max_tokens"] == 1024
    assert env.pool._active_by_network == {} and env.pool._inflight == {}


@pytest.mark.anyio
async def test_a_failing_member_is_skipped_for_the_next(env):
    env.fakes["a"].mode = "500"
    env.add(env.fakes["a"])
    env.add(env.fakes["b"])
    async with client() as c:
        resp = await post(c)
    assert resp.status_code == 200 and resp.content == env.fakes["b"].body


@pytest.mark.anyio
async def test_members_without_a_tunnel_or_not_ready_are_not_used(env):
    env.registry.update_worker("loner", "10.9.9.9", 50053, "t", {"pool": "app", "ram_gb": 12.0,
                                                                  "app": {"model": "small", "ready": True}})
    env.add(env.fakes["a"], ready=False)
    async with client() as c:
        resp = await post(c)
    assert resp.status_code == 200
    assert "No machine in the CPU pool is free" in notice_texts(resp.content)[0]
    assert env.fakes["a"].payloads == []
    assert env.pool._active_by_network == {}


@pytest.mark.anyio
async def test_the_busiest_member_is_avoided(env):
    env.add(env.fakes["a"])
    env.add(env.fakes["b"])
    env.pool._inflight["worker:w1"] = 2     # w1 (a) is full
    async with client() as c:
        resp = await post(c)
    assert resp.content == env.fakes["b"].body and env.fakes["a"].payloads == []


# ======================================================================================
# Throttle
# ======================================================================================

async def _hold_first_request(env, first_ip, second_ip):
    a = env.fakes["a"]
    a.mode = "hold"
    a.gate.clear()
    env.add(a)
    env.add(env.fakes["b"])     # free capacity for an admitted second request (a is busy holding)
    async with client(first_ip) as c1, client(second_ip) as c2:
        first = asyncio.create_task(post(c1))
        for _ in range(1000):
            if a.entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert a.entered.is_set(), "the first request never reached the member"
        second = await post(c2)
        a.gate.set()
        first_resp = await first
    return first_resp, second


@pytest.mark.anyio
async def test_a_second_concurrent_request_from_the_same_address_is_429(env):
    first, second = await _hold_first_request(env, REMOTE, (REMOTE[0], 40001))
    assert second.status_code == 429
    assert "One answer at a time" in second.json()["detail"]
    assert second.headers["retry-after"]
    assert first.status_code == 200 and first.content == env.fakes["a"].body
    assert env.pool._active_by_network == {}
    async with client() as c:
        env.fakes["a"].reset()
        assert (await post(c)).status_code == 200, "the slot is free again once the answer ends"


@pytest.mark.anyio
async def test_one_ipv6_slash_64_is_one_network(env):
    first, second = await _hold_first_request(env, ("2001:db8:0:1::5", 1), ("2001:db8:0:1:ffff::6", 1))
    assert second.status_code == 429 and first.status_code == 200


@pytest.mark.anyio
async def test_another_address_is_not_held_up(env):
    first, second = await _hold_first_request(env, REMOTE, ("198.51.100.20", 1))
    assert second.status_code == 200 and first.status_code == 200
    assert second.content == env.fakes["b"].body and first.content == env.fakes["a"].body


@pytest.mark.anyio
async def test_the_seventh_request_in_a_minute_is_429(env):
    env.add(env.fakes["a"])
    async with client() as c:
        for i in range(6):
            assert (await post(c)).status_code == 200, f"request {i + 1}"
        resp = await post(c)
    assert resp.status_code == 429
    assert resp.json()["detail"].startswith("Slow down")
    assert len(env.fakes["a"].payloads) == 6
    assert env.pool._active_by_network == {}


@pytest.mark.anyio
async def test_the_rate_comes_from_the_config(env):
    env.limits["app_rate_per_minute"] = 2
    env.add(env.fakes["a"])
    async with client() as c:
        codes = [(await post(c)).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


@pytest.mark.anyio
async def test_retry_after_matches_the_per_minute_wait(env):
    env.add(env.fakes["a"])
    async with client() as c:
        for _ in range(6):
            await post(c)
        resp = await post(c)
    wait = int(re.search(r"try again in (\d+) seconds", resp.json()["detail"]).group(1))
    assert int(resp.headers["retry-after"]) >= wait


@pytest.mark.anyio
async def test_an_absurd_max_tokens_is_capped_not_fatal(env):
    env.add(env.fakes["a"])
    async with client() as c:
        resp = await post(c, raw='{"messages": [{"role": "user", "content": "hi"}], "max_tokens": 1e999}')
    assert resp.status_code == 200 and resp.content == env.fakes["a"].body
    assert env.fakes["a"].payloads[0]["max_tokens"] == 1024


def test_a_client_that_hangs_up_frees_its_network_at_once(env):
    """Over a real socket: the slot is released when the phone gives up, not when the member answers."""
    hanger = env.fakes["hanger"]
    hanger.connections = 0
    env.add(hanger)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(coordinator.app, host="127.0.0.1", port=port,
                                           log_level="critical", lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        assert wait_for(lambda: server.started)
        body = json.dumps(CHAT).encode()
        request = (b"POST /v1/app/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                   b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        phone = socket.create_connection(("127.0.0.1", port))
        phone.sendall(request)
        assert wait_for(lambda: hanger.connections == 1)
        assert env.pool._active_by_network == {"127.0.0.1": 1}
        assert sum(env.pool._inflight.values()) == 1
        phone.close()
        assert wait_for(lambda: env.pool._active_by_network == {} and env.pool._inflight == {}, timeout=5)
    finally:
        server.should_exit = True
        thread.join(timeout=5)


# ======================================================================================
# The heartbeat answer: which tier a member serves
# ======================================================================================

def _heartbeat(worker_id, hardware, ip):
    from tests.test_coordinator_counter import FakeRequest
    payload = coordinator.HeartbeatPayload(worker_id=worker_id, version=VERSION, port=50053,
                                           token=f"tok-{worker_id}", hardware=hardware)
    return asyncio.run(coordinator.worker_heartbeat(FakeRequest(client_host=ip), payload))


def _member_hw(ram, model=None, ready=False):
    return {**QUALIFYING, "ram_gb": ram, "pool": "app", "app": {"model": model, "ready": ready, "slots": 2}}


def test_a_pool_member_is_told_its_tier(env):
    assert _heartbeat("m1", _member_hw(12.0), "10.0.0.11") == {"status": "ok", "app_model": "small"}


def test_a_bigger_tier_reaches_members_once_two_can_host_it(env):
    assert _heartbeat("big1", _member_hw(32.0), "10.0.0.21")["app_model"] == "small"
    assert _heartbeat("big2", _member_hw(32.0), "10.0.0.22")["app_model"] == "large"
    assert _heartbeat("big1", _member_hw(32.0), "10.0.0.21")["app_model"] == "large"
    assert _heartbeat("little", _member_hw(12.0), "10.0.0.23")["app_model"] == "small"


def test_a_member_too_small_for_any_tier_is_told_none(env):
    env.limits["cpu_min_ram_gb"] = 4.0
    assert _heartbeat("tiny", _member_hw(6.0), "10.0.0.31") == {"status": "ok", "app_model": None}


def test_an_rpc_worker_gets_no_tier_and_a_member_never_joins_the_pipeline(env):
    gpu = {"gpu": {"has_gpu": True, "vram_total_gb": 24.0}, "ram_gb": 32.0, "cpu_cores": 8, "cpu_threads": 16,
           "is_yielding": False}
    assert _heartbeat("gpu1", gpu, "10.0.0.41") == {"status": "ok"}
    _heartbeat("m1", _member_hw(64.0), "10.0.0.42")
    assert env.registry.get_active_rpc_endpoints() == ["10.0.0.41:50053"]
    assert env.registry.get_total_sysram_gb() == 32.0
    assert [r["worker_id"] for r in env.registry.app_members()] == ["m1"]


def test_an_implausible_app_section_is_refused(env):
    from fastapi import HTTPException
    bad = {**QUALIFYING, "pool": "app", "app": {"model": "small", "ready": "yes", "slots": 2}}
    with pytest.raises(HTTPException) as refused:
        _heartbeat("m1", bad, "10.0.0.51")
    assert refused.value.status_code == 400
    assert env.registry.app_members() == []
