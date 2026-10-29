"""
Phone lanes (2.6) through the coordinator: the phone app's chat answered on the coding engine's phone
lanes, and what that means for coding chat and autocomplete (engine/priority.py LaneBook).

The engine is a stand-in llama-server on a real loopback socket that records every body it is sent and can
hold its answers; the manager is a stub that reports the entry a started engine would have. The CPU pool
member is the same stand-in the other app tests use, so "fell back to the CPU pool" means exactly that.
"""

import asyncio
import json
import logging
import threading
import time
from types import SimpleNamespace

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from httpx import ASGITransport, AsyncClient

from engine import chat_privacy, coordinator, reverse_tunnel
from engine import config as engine_config
from engine.app_pool import AppPool
from engine.priority import CodingGate, LaneBook
from engine.registry import WorkerRegistry
from engine.throughput import throughput
from tests.app_pool_fakes import DONE, FakeLlamaServer, free_port, sse, wait_for

REMOTE = ("203.0.113.9", 40000)
OTHER = ("198.51.100.20", 40001)
THIRD = ("198.51.100.30", 40002)
MODEL = "m.gguf"
CODING_SLOTS, PHONE_SLOTS = 2, 2
CHAT_A = "3f2b9c1e-6a47-4d52-9a0e-1b7c5d8e2f41"
CHAT_B = "9d8c7b6a-5f4e-4d3c-8b2a-1f0e9d8c7b6a"
TIERS = [{"id": "small", "name": "Small 4B", "file": "s.gguf", "min_ram_gb": 8}]


class LaneEngine:
    """The coding engine's llama-server: records bodies per path, answers chat and completions, can hold."""

    def __init__(self) -> None:
        self.port = free_port()
        self.bodies = {"/v1/chat/completions": [], "/v1/completions": []}
        self.release = threading.Event()
        self.release.set()
        self.fails = False          # every chat request is answered 500: the engine cannot serve
        self.script = []            # replies (lists of SSE chunks) for the next chat requests, in order
        self.tokenized = 0
        app = FastAPI()

        async def answer(request: Request):
            body = await request.json()
            self.bodies[request.url.path].append(body)
            if self.fails:
                return JSONResponse({"error": {"message": "the model crashed"}}, status_code=500)
            if request.url.path == "/v1/completions" and not body.get("stream"):
                return JSONResponse({"choices": [{"text": "x", "index": 0, "finish_reason": "stop"}]})

            scripted = self.script.pop(0) if self.script and request.url.path == "/v1/chat/completions" else None

            async def stream():
                if scripted is not None:
                    for chunk in scripted:
                        yield chunk
                    return
                yield sse("engine-one ")
                await asyncio.to_thread(self.release.wait, 20)
                yield sse("engine-two")
                yield DONE

            return StreamingResponse(stream(), media_type="text/event-stream")

        @app.post("/tokenize")
        async def tokenize(request: Request):
            self.tokenized += 1
            return {"tokens": [0] * -(-len((await request.json())["content"]) // 3)}

        app.post("/v1/chat/completions")(answer)
        app.post("/v1/completions")(answer)
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port,
                                                    log_level="critical", lifespan="off"))
        self.thread = threading.Thread(target=self.server.run, daemon=True, name="lane-engine")

    def start(self) -> "LaneEngine":
        self.thread.start()
        assert wait_for(lambda: self.server.started)
        return self

    def stop(self) -> None:
        self.release.set()
        self.server.should_exit = True
        self.thread.join(timeout=5)

    def reset(self) -> None:
        for bodies in self.bodies.values():
            bodies.clear()
        self.release.set()
        self.fails = False
        self.script = []
        self.tokenized = 0

    @property
    def chats(self):
        return self.bodies["/v1/chat/completions"]

    @property
    def completions(self):
        return self.bodies["/v1/completions"]


class StubManager:
    """The inference manager as far as the coordinator reads it: a running engine with, or without, phone lanes."""

    def __init__(self, engine: LaneEngine) -> None:
        self.engine = engine
        self.running = True
        self.phone_slots = PHONE_SLOTS
        self.alive = True
        self.process = SimpleNamespace(poll=lambda: None if self.alive else 1)    # one process per started engine

    def get_active_model_names(self):
        return [MODEL] if self.running else []

    async def asnapshot_model(self, model):
        if not self.running:
            return None
        return {"process": self.process, "port": self.engine.port,
                "safe_context": 109_226, "dynamic_slots": CODING_SLOTS,
                "phone_slots": self.phone_slots, "phone_context": 8192}

    async def aestimate_model_vram_gb(self, model):
        return 1.0

    async def astart_model(self, model, endpoints, vram, sysram, queue_channels=2):
        return True, self.engine.port, "Already running"

    async def aget_loading_telemetry(self, model):
        return {}


@pytest.fixture(scope="module")
def servers():
    engine, cpu = LaneEngine().start(), FakeLlamaServer("cpu").start()
    yield SimpleNamespace(engine=engine, cpu=cpu)
    engine.stop()
    cpu.stop()


@pytest.fixture
def env(servers, monkeypatch, isolated_config, dlne_root, tmp_path):
    servers.engine.reset()
    servers.cpu.reset()
    # the salt's secret lives under the install's config folder: a throwaway one, in the module that bound it
    monkeypatch.setattr(chat_privacy, "get_base_dir", lambda: str(dlne_root))
    monkeypatch.setattr(chat_privacy, "_secret", None)

    pool = AppPool()
    pool.load_tiers([dict(t) for t in TIERS])
    monkeypatch.setattr(coordinator, "app_pool", pool)
    registry = WorkerRegistry()
    monkeypatch.setattr(coordinator, "registry", registry)
    relays = {}
    monkeypatch.setattr(reverse_tunnel, "reverse_bridge",
                        SimpleNamespace(get_relay_port=lambda ip, port: relays.get((ip, port))))
    registry.update_worker("w1", "10.8.0.2", 50053, "tok1",
                           {"pool": "app", "ram_gb": 12.0, "gpu": None,
                            "app": {"model": "small", "ready": True, "slots": 2}})
    relays[("10.8.0.2", 50053)] = servers.cpu.port

    limits = {
        "trust_proxy_headers": False, "enforce_contrib_clause": False, "starter_quota_inferences": 200,
        "app_rate_per_minute": 60, "app_tier_min_members": 2, "network_key": "", "allow_unsigned_workers": True,
        "engine_phone_slots": PHONE_SLOTS, "engine_phone_context": 8192, "coding_members_enabled": False,
        **engine_config.CPU_NODE_DEFAULTS,
    }
    monkeypatch.setattr(coordinator, "get_worker_limits", lambda: dict(limits))
    monkeypatch.setattr(coordinator, "_engine_http", None)
    monkeypatch.setattr(coordinator, "starter_quota_mgr",
                        coordinator.StarterQuotaManager(str(tmp_path / "quota.json"), default_quota=200))
    manager = StubManager(servers.engine)
    monkeypatch.setattr(coordinator, "manager", manager)
    monkeypatch.setattr(coordinator, "_planned_endpoints", lambda gb: [])
    monkeypatch.setattr(coordinator, "is_swapping_models", False)
    monkeypatch.setattr(coordinator, "is_recalculating_queue", False)
    monkeypatch.setattr(coordinator, "spawn_vram_progress_task", lambda *a, **k: None)

    async def probe(port):
        return servers.engine.server.started and manager.alive

    monkeypatch.setattr(coordinator, "_probe_engine_port", probe)
    monkeypatch.setattr(coordinator, "coding_gate", CodingGate(CODING_SLOTS))
    monkeypatch.setattr(coordinator, "phone_gate", CodingGate(PHONE_SLOTS))
    monkeypatch.setattr(coordinator, "lane_book", LaneBook())
    throughput.reset()
    return SimpleNamespace(pool=pool, registry=registry, limits=limits, manager=manager, **vars(servers))


def client(ip=REMOTE):
    return AsyncClient(transport=ASGITransport(app=coordinator.app, client=ip), base_url="http://test")


def phone_body(text="hello", chat_id=CHAT_A, **extra):
    body = {"messages": [{"role": "user", "content": text}], **extra}
    if chat_id:
        body["chat_id"] = chat_id
    return body


async def phone(c, body=None):
    return await c.post("/v1/app/chat/completions", json=body or phone_body())


async def code(c, body=None, headers=None):
    body = body or {"model": MODEL, "stream": True, "messages": [{"role": "user", "content": "write code"}]}
    return await c.post("/v1/chat/completions", json=body, headers=headers)


async def until(predicate, seconds=5.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


def system_of(body):
    return body["messages"][0]["content"]


# ======================================================================================
# Phones on the engine's lanes
# ======================================================================================

@pytest.mark.anyio
async def test_a_phone_is_answered_by_the_engine_on_a_phone_lane(env):
    async with client() as c:
        resp = await phone(c)

    assert resp.status_code == 200 and b"engine-one" in resp.content
    assert env.cpu.payloads == [], "the CPU pool is not asked"
    (sent,) = env.engine.chats
    assert sent["id_slot"] in range(CODING_SLOTS, CODING_SLOTS + PHONE_SLOTS)
    assert system_of(sent).endswith(chat_privacy.salt_line(CHAT_A))
    assert "cache_prompt" not in sent and sent["n_cache_reuse"] == 0
    assert env.pool._active_by_network == {} and coordinator.phone_gate.stats()["in_use"] == 0
    assert coordinator.lane_book._uses.get(sent["id_slot"], 0) == 0


@pytest.mark.anyio
async def test_the_salt_reaches_the_engine_and_nowhere_else(env, caplog):
    caplog.set_level(0)
    async with client() as c:
        first = await phone(c, phone_body(chat_id=CHAT_A))
        await phone(c, phone_body(chat_id=CHAT_B))
        await phone(c, phone_body("again", chat_id=CHAT_A))
    a1, b, a2 = env.engine.chats
    tag = chat_privacy.salt_line(CHAT_A).split()[2]
    secret = open(f"{chat_privacy.get_base_dir()}/config/chat_salt.key").read().strip()

    assert system_of(a1) == system_of(a2) != system_of(b)
    assert tag.encode() not in first.content
    assert tag not in caplog.text and secret not in caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize("chat_id", [None, "short", "not a token at all!!", "abcdefgh", "alice-the-user",
                                     "00000000-0000-0000-0000-000000000000", CHAT_A.replace("-", ""), 12345, [CHAT_A]])
async def test_a_phone_without_a_usable_chat_id_gets_no_cache(env, chat_id):
    async with client() as c:
        await phone(c, phone_body(chat_id=None) | ({"chat_id": chat_id} if chat_id else {}))

    (sent,) = env.engine.chats
    assert sent["cache_prompt"] is False and "internal reference" not in system_of(sent)


@pytest.mark.anyio
async def test_a_phones_own_id_slot_is_never_forwarded(env):
    async with client() as c:
        await phone(c, phone_body(id_slot=0, cache_prompt=True))

    (sent,) = env.engine.chats
    assert sent["id_slot"] >= CODING_SLOTS


@pytest.mark.anyio
async def test_phones_spread_over_the_phone_lanes(env):
    env.engine.release.clear()
    async with client() as c:
        tasks = [asyncio.create_task(phone(c)), asyncio.create_task(phone(AsyncClient(
            transport=ASGITransport(app=coordinator.app, client=OTHER), base_url="http://test")))]
        assert await until(lambda: len(env.engine.chats) == 2)
        env.engine.release.set()
        await asyncio.gather(*tasks)

    assert {b["id_slot"] for b in env.engine.chats} == {2, 3}


@pytest.mark.anyio
async def test_phones_wait_at_most_a_little_for_a_lane_then_get_the_usual_429(env, monkeypatch):
    monkeypatch.setattr(coordinator, "PHONE_LANE_WAIT_SECONDS", 0.3)
    env.engine.release.clear()
    nets = [REMOTE, OTHER, THIRD]
    async with client(nets[0]) as c1, client(nets[1]) as c2, client(nets[2]) as c3:
        held = [asyncio.create_task(phone(c1)), asyncio.create_task(phone(c2))]
        assert await until(lambda: len(env.engine.chats) == 2)
        assert coordinator.phone_gate.stats()["in_use"] == PHONE_SLOTS
        refused = await phone(c3)
        env.engine.release.set()
        results = await asyncio.gather(*held)

    assert refused.status_code == 429 and refused.json()["detail"] and refused.headers["retry-after"]
    assert [r.status_code for r in results] == [200, 200]
    assert len(env.engine.chats) == 2, "the third never reached the engine"
    assert env.pool._active_by_network == {}, "the refused phone's network is free again"
    assert coordinator.phone_gate.stats() == {**coordinator.phone_gate.stats(), "in_use": 0, "waiting": 0}


@pytest.mark.anyio
async def test_a_phone_that_waits_gets_the_lane_when_one_frees(env):
    env.engine.release.clear()
    clients = [client(REMOTE), client(OTHER), client(THIRD)]
    try:
        tasks = [asyncio.create_task(phone(c)) for c in clients[:2]]
        assert await until(lambda: len(env.engine.chats) == 2)
        waiter = asyncio.create_task(phone(clients[2]))
        assert await until(lambda: coordinator.phone_gate.stats()["waiting"] == 1)
        env.engine.release.set()
        results = await asyncio.gather(*tasks, waiter)
    finally:
        for c in clients:
            await c.aclose()

    assert [r.status_code for r in results] == [200, 200, 200] and len(env.engine.chats) == 3


@pytest.mark.anyio
async def test_a_phone_that_hangs_up_gives_its_lane_back(env):
    env.engine.release.clear()
    async with client() as c:
        task = asyncio.create_task(phone(c))
        assert await until(lambda: len(env.engine.chats) == 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        env.engine.release.set()
        assert await until(lambda: coordinator.phone_gate.stats()["in_use"] == 0)
    assert env.pool._active_by_network == {} and coordinator.lane_book._uses.get(2, 0) == 0


# ======================================================================================
# Coding never waits for a phone, a phone never waits for coding
# ======================================================================================

@pytest.mark.anyio
async def test_coding_chat_runs_on_a_coding_lane_while_both_phone_lanes_are_busy(env):
    env.engine.release.clear()
    clients = [client(REMOTE), client(OTHER)]
    try:
        phones = [asyncio.create_task(phone(c)) for c in clients]
        assert await until(lambda: len(env.engine.chats) == 2)
        env.engine.release.set()
        async with client(("127.0.0.1", 5000)) as c:
            resp = await code(c)
        await asyncio.gather(*phones)
    finally:
        for c in clients:
            await c.aclose()

    assert resp.status_code == 200 and b"engine-one" in resp.content
    coding = [b for b in env.engine.chats if b.get("model") == MODEL]
    assert len(coding) == 1 and coding[0]["id_slot"] in range(CODING_SLOTS)
    assert coordinator.coding_gate.stats()["in_use"] == 0
    assert all(v == 0 for v in coordinator.lane_book._uses.values())


@pytest.mark.anyio
async def test_a_phone_is_answered_while_every_coding_slot_is_busy(env):
    held = [coordinator.coding_gate.try_acquire() for _ in range(CODING_SLOTS)]
    assert all(held)
    async with client() as c:
        resp = await asyncio.wait_for(phone(c), 5)

    assert resp.status_code == 200 and b"engine-one" in resp.content


@pytest.mark.anyio
async def test_a_coding_request_always_names_its_own_lane_and_drops_the_clients(env):
    body = {"model": MODEL, "stream": True, "id_slot": 3, "messages": [{"role": "user", "content": "write code"}]}
    async with client(("127.0.0.1", 5000)) as c:
        for _ in range(CODING_SLOTS):
            await code(c, dict(body))
        await code(c, {**body, "id_slot": 99})

    assert len(env.engine.chats) == CODING_SLOTS + 1
    assert all(b["id_slot"] in range(CODING_SLOTS) for b in env.engine.chats)


@pytest.mark.anyio
async def test_two_coding_requests_at_once_use_both_coding_lanes(env):
    env.engine.release.clear()
    async with client(("127.0.0.1", 5000)) as c:
        tasks = [asyncio.create_task(code(c)) for _ in range(2)]
        assert await until(lambda: len(env.engine.chats) == 2)
        env.engine.release.set()
        await asyncio.gather(*tasks)

    assert sorted(b["id_slot"] for b in env.engine.chats) == [0, 1]


@pytest.mark.anyio
async def test_the_summary_request_goes_to_the_same_coding_lane(env):
    big = "word " * 40_000        # 200k chars: far past a 54,613-token request
    body = {"model": MODEL, "stream": True, "messages": [
        {"role": "user", "content": big}, {"role": "assistant", "content": big}, {"role": "user", "content": "now?"}]}
    async with client(("127.0.0.1", 5000)) as c:
        await code(c, body)

    summary, answer = env.engine.chats
    assert summary["max_tokens"] == 400 and summary["id_slot"] == answer["id_slot"] in range(CODING_SLOTS)


@pytest.mark.anyio
async def test_autocomplete_takes_a_coding_lane_without_waiting_and_gives_it_back(env):
    held = [coordinator.phone_gate.try_acquire() for _ in range(PHONE_SLOTS)]
    assert all(held)
    prompt = "<|fim_prefix|>def f(<|fim_suffix|>)<|fim_middle|>"
    async with client(("127.0.0.1", 5000)) as c:
        resp = await c.post("/v1/completions", json={"model": MODEL, "prompt": prompt, "stream": False, "id_slot": 3})

    assert resp.status_code == 200 and resp.json()["choices"][0]["finish_reason"] == "stop"
    assert len(env.engine.completions) == 2, "the warm-up and the completion"
    assert all(b["id_slot"] in range(CODING_SLOTS) for b in env.engine.completions)
    assert len({b["id_slot"] for b in env.engine.completions}) == 1
    assert all(v == 0 for v in coordinator.lane_book._uses.values())
    assert coordinator.phone_gate.stats()["in_use"] == PHONE_SLOTS, "autocomplete holds no phone slot"


@pytest.mark.anyio
async def test_a_streamed_autocomplete_gives_its_lane_back_too(env):
    async with client(("127.0.0.1", 5000)) as c:
        resp = await c.post("/v1/completions", json={"model": MODEL, "prompt": "def f(", "stream": True})

    assert resp.status_code == 200 and env.engine.completions[0]["id_slot"] in range(CODING_SLOTS)
    assert await until(lambda: all(v == 0 for v in coordinator.lane_book._uses.values()))


# ======================================================================================
# The status and the advertised policy
# ======================================================================================

@pytest.mark.anyio
async def test_the_status_shows_the_phone_lanes_apart_and_the_coding_numbers_stay_coding(env):
    env.engine.release.clear()
    async with client() as c:
        held = asyncio.create_task(phone(c))
        assert await until(lambda: len(env.engine.chats) == 1)
        async with client(("127.0.0.1", 5000)) as admin:
            status = (await admin.get("/v1/cluster/status")).json()
        env.engine.release.set()
        await held

    assert status["phone_lanes"] == {"slots": PHONE_SLOTS, "in_use": 1, "waiting": 0, "available": True, "starting": False}
    assert status["coding_queue"]["slots"] == CODING_SLOTS and status["coding_queue"]["in_use"] == 0
    assert status["coding_model"]["context"] == 109_226
    assert status["policy"]["coding_context_tokens"] == 54_613


# ======================================================================================
# Everything that sends a phone to the CPU pool as before
# ======================================================================================

@pytest.mark.anyio
async def test_lanes_off_is_the_cpu_pool_and_the_status_has_no_phone_lanes(env):
    env.limits["engine_phone_slots"] = 0
    env.manager.phone_slots = 0
    async with client() as c:
        resp = await phone(c, phone_body(id_slot=1))
        async with client(("127.0.0.1", 5000)) as admin:
            status = (await admin.get("/v1/cluster/status")).json()

    assert resp.status_code == 200 and env.engine.chats == []
    (sent,) = env.cpu.payloads
    assert "id_slot" not in sent and "internal reference" not in system_of(sent) and "cache_prompt" not in sent
    assert "phone_lanes" not in status


@pytest.mark.anyio
async def test_lanes_off_still_drops_a_clients_id_slot_and_touches_nothing_else(env):
    env.limits["engine_phone_slots"] = 0
    env.manager.phone_slots = 0
    async with client(("127.0.0.1", 5000)) as c:
        await code(c, {"model": MODEL, "stream": True, "id_slot": 1, "messages": [{"role": "user", "content": "hi"}]})
        await c.post("/v1/completions", json={"model": MODEL, "prompt": "def f(", "stream": False, "id_slot": 1})

    # 2.6.1: the slot an engine request runs on is never the client's, lanes or not
    assert "id_slot" not in env.engine.chats[0] and "id_slot" not in env.engine.completions[0]
    assert coordinator.lane_book._uses == {}


@pytest.mark.anyio
@pytest.mark.parametrize("how", ["down", "not_restarted", "swapping", "dead", "silent"])
async def test_a_phone_falls_back_to_the_cpu_pool_when_the_lanes_cannot_answer(env, monkeypatch, how):
    if how == "down":
        env.manager.running = False
    elif how == "not_restarted":
        env.manager.phone_slots = 0
    elif how == "swapping":
        monkeypatch.setattr(coordinator, "is_swapping_models", True)
    elif how == "dead":
        env.manager.alive = False
    elif how == "silent":
        async def probe(port):
            return False
        monkeypatch.setattr(coordinator, "_probe_engine_port", probe)
    async with client() as c:
        resp = await phone(c)

    assert resp.status_code == 200 and b"cpu-one" in resp.content
    assert env.engine.chats == [] and coordinator.phone_gate.stats()["in_use"] == 0
    assert env.pool._active_by_network == {}


@pytest.mark.anyio
async def test_the_phone_throttle_still_comes_first(env):
    env.limits["app_rate_per_minute"] = 1
    async with client() as c:
        assert (await phone(c)).status_code == 200
        refused = await phone(c)

    assert refused.status_code == 429 and refused.json()["detail"].startswith("Slow down")
    assert len(env.engine.chats) == 1


# ======================================================================================
# The engine fails before saying anything
# ======================================================================================

@pytest.mark.anyio
async def test_a_failing_engine_hands_the_phone_to_the_ready_cpu_members(env):
    env.engine.fails = True
    async with client() as c:
        resp = await phone(c)

    assert resp.status_code == 200 and b"cpu-one" in resp.content and b"No machine" not in resp.content
    assert len(env.engine.chats) >= 1
    (sent,) = env.cpu.payloads
    assert "id_slot" not in sent and "cache_prompt" not in sent
    assert coordinator.phone_gate.stats()["in_use"] == 0 and env.pool._active_by_network == {}
    assert all(v == 0 for v in coordinator.lane_book._uses.values())


@pytest.mark.anyio
async def test_a_failing_engine_and_no_cpu_member_says_dlne_is_restarting(env):
    env.engine.fails = True
    env.registry.update_worker("w1", "10.8.0.2", 50053, "tok1",
                               {"pool": "app", "ram_gb": 12.0, "gpu": None,
                                "app": {"model": "small", "ready": False, "slots": 2}})
    async with client() as c:
        resp = await phone(c)

    assert resp.status_code == 200 and env.cpu.payloads == []
    assert b"DLNE is restarting, try again in a moment" in resp.content
    assert b"No machine" not in resp.content and b"swarm is loading" not in resp.content
    assert resp.content.endswith(b"data: [DONE]\n\n")
    assert coordinator.phone_gate.stats()["in_use"] == 0 and env.pool._active_by_network == {}


@pytest.mark.anyio
async def test_the_engine_counts_the_phones_prompt(env):
    async with client() as c:
        await phone(c)

    assert env.engine.tokenized >= 1


# ======================================================================================
# Review fixes: nothing leaks on an exception, a stale engine is not used, the quiet check, the FIM pin
# ======================================================================================

def nothing_held(env):
    return (env.pool._active_by_network == {} and coordinator.phone_gate.stats()["in_use"] == 0
            and all(v == 0 for v in coordinator.lane_book._uses.values()))


def boom(*args, **kwargs):
    raise RuntimeError("injected")


@pytest.mark.anyio
@pytest.mark.parametrize("where", ["lookup_tools", "phone_engine", "lane_take", "engine_lane", "app_members",
                                   "pool_status", "member", "second_look"])
async def test_an_exception_at_any_step_gives_everything_back(env, monkeypatch, where):
    if where == "lookup_tools":
        monkeypatch.setattr(coordinator, "_app_lookup_tools", boom)
    elif where == "phone_engine":
        monkeypatch.setattr(coordinator, "_phone_engine", boom)
    elif where == "lane_take":
        monkeypatch.setattr(coordinator.lane_book, "take", boom)
    elif where == "engine_lane":
        monkeypatch.setattr(coordinator, "EngineLane", boom)
    elif where == "app_members":
        monkeypatch.setattr(coordinator, "_app_members", boom)
    elif where == "pool_status":
        monkeypatch.setattr(env.pool, "status", boom)
    elif where == "member":
        monkeypatch.setattr(coordinator, "Member", boom)
    else:                                     # the look after the wait fails, the first one did not
        real, calls = coordinator._phone_engine, []

        async def second_fails():
            calls.append(1)
            if len(calls) > 1:
                raise RuntimeError("injected")
            return await real()

        monkeypatch.setattr(coordinator, "_phone_engine", second_fails)
    async with client() as c:
        with pytest.raises(RuntimeError, match="injected"):
            await phone(c)
    assert nothing_held(env), "the gate, the lane and the network are all free"


@pytest.mark.anyio
async def test_a_cancelled_wait_for_a_lane_leaves_nothing_held(env):
    held = [coordinator.phone_gate.try_acquire() for _ in range(PHONE_SLOTS)]
    assert all(held)
    async with client() as c:
        task = asyncio.create_task(phone(c))
        assert await until(lambda: coordinator.phone_gate.stats()["waiting"] == 1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert await until(lambda: coordinator.phone_gate.stats()["waiting"] == 0 and env.pool._active_by_network == {})
    for _ in range(PHONE_SLOTS):
        coordinator.phone_gate.release()
    assert nothing_held(env)


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["restart", "port", "lanes", "swap"])
async def test_an_engine_that_changed_during_the_wait_is_not_used_the_cpu_pool_answers(env, change):
    held = [coordinator.phone_gate.try_acquire() for _ in range(PHONE_SLOTS)]
    assert all(held)
    async with client() as c:
        task = asyncio.create_task(phone(c))
        assert await until(lambda: coordinator.phone_gate.stats()["waiting"] == 1)
        if change == "restart":
            env.manager.process = SimpleNamespace(poll=lambda: None)
        elif change == "port":
            env.manager.engine = SimpleNamespace(port=env.engine.port + 1)
        elif change == "lanes":
            env.manager.phone_slots = PHONE_SLOTS + 1
        else:
            coordinator.is_swapping_models = True
        for _ in range(PHONE_SLOTS):
            coordinator.phone_gate.release()
        resp = await task
        coordinator.is_swapping_models = False

    assert resp.status_code == 200 and b"cpu-one" in resp.content
    assert env.engine.chats == []
    (sent,) = env.cpu.payloads
    assert "id_slot" not in sent
    assert nothing_held(env)


@pytest.mark.anyio
async def test_an_engine_that_stayed_the_same_during_the_wait_is_used(env):
    held = [coordinator.phone_gate.try_acquire() for _ in range(PHONE_SLOTS)]
    async with client() as c:
        task = asyncio.create_task(phone(c))
        assert await until(lambda: coordinator.phone_gate.stats()["waiting"] == 1)
        for _ in range(PHONE_SLOTS):
            coordinator.phone_gate.release()
        resp = await task
    assert resp.status_code == 200 and b"engine-one" in resp.content and env.cpu.payloads == []
    assert nothing_held(env) and all(held)


@pytest.mark.anyio
async def test_the_auto_swap_quiet_check_counts_a_phone_answer_on_a_lane(env):
    assert coordinator._coding_queue_busy() is False
    env.engine.release.clear()
    async with client() as c:
        task = asyncio.create_task(phone(c))
        assert await until(lambda: len(env.engine.chats) == 1)
        assert coordinator._coding_queue_busy() is True, "a phone is streaming: not quiet"
        env.engine.release.set()
        await task
    assert coordinator._coding_queue_busy() is False


@pytest.mark.anyio
async def test_autocomplete_pins_on_the_running_engines_lanes_not_on_the_setting(env):
    prompt = "<|fim_prefix|>def f(<|fim_suffix|>)<|fim_middle|>"
    env.limits["engine_phone_slots"] = 0                 # the setting says off; the running engine has lanes
    async with client(("127.0.0.1", 5000)) as c:
        resp = await c.post("/v1/completions", json={"model": MODEL, "prompt": prompt, "stream": False})
    assert resp.status_code == 200
    assert all(b.get("id_slot") in range(CODING_SLOTS) for b in env.engine.completions) and env.engine.completions
    env.engine.completions.clear()
    env.limits["engine_phone_slots"] = PHONE_SLOTS       # the setting says on; the running engine has none
    env.manager.phone_slots = 0
    async with client(("127.0.0.1", 5000)) as c:
        resp = await c.post("/v1/completions", json={"model": MODEL, "prompt": prompt, "stream": False, "id_slot": 3})
    assert resp.status_code == 200 and env.engine.completions
    assert "id_slot" not in env.engine.completions[-1], "no lanes: the client's id_slot is dropped all the same"


# ======================================================================================
# 2.6.1: lane choice, the CPU fallback, the status, the restart wording, swaps, huge numbers
# ======================================================================================

@pytest.mark.anyio
async def test_a_chat_goes_back_to_the_lane_it_used_when_that_one_is_free(env):
    """Without affinity both free lanes tie and the lowest wins, so chat A's second turn would land on lane 2."""
    env.engine.release.clear()
    async with client(REMOTE) as ca, client(OTHER) as cb:
        holder = asyncio.create_task(phone(cb, phone_body(chat_id=CHAT_B)))       # B takes the first lane
        assert await until(lambda: len(env.engine.chats) == 1)
        first = asyncio.create_task(phone(ca, phone_body(chat_id=CHAT_A)))        # A gets the other
        assert await until(lambda: len(env.engine.chats) == 2)
        env.engine.release.set()
        await asyncio.gather(holder, first)
        await phone(ca, phone_body("and then?", chat_id=CHAT_A))                  # both lanes free again

    b_lane, a_lane, a_again = (b["id_slot"] for b in env.engine.chats)
    assert {b_lane, a_lane} == {2, 3} and a_again == a_lane


@pytest.mark.anyio
async def test_a_chat_whose_lane_is_busy_takes_the_other_one(env):
    env.engine.release.clear()
    async with client(REMOTE) as ca, client(OTHER) as cb:
        await_a = asyncio.create_task(phone(ca, phone_body(chat_id=CHAT_A)))
        assert await until(lambda: len(env.engine.chats) == 1)
        await_b = asyncio.create_task(phone(cb, phone_body(chat_id=CHAT_B)))
        assert await until(lambda: len(env.engine.chats) == 2)
        env.engine.release.set()
        await asyncio.gather(await_a, await_b)
        env.engine.release.clear()
        busy_b = asyncio.create_task(phone(cb, phone_body("more", chat_id=CHAT_B)))   # B's lane is held again
        assert await until(lambda: len(env.engine.chats) == 3)
        again_a = asyncio.create_task(phone(ca, phone_body("more", chat_id=CHAT_A)))
        assert await until(lambda: len(env.engine.chats) == 4)
        env.engine.release.set()
        await asyncio.gather(busy_b, again_a)

    assert len({b["id_slot"] for b in env.engine.chats[2:]}) == 2, "two chats at once are on two lanes"


@pytest.mark.anyio
async def test_the_cpu_fallback_for_a_chat_has_no_salt_the_cpu_history_cap_and_frees_the_lane_while_the_cpu_answers(env):
    env.engine.fails = True
    env.cpu.mode = "hold"
    env.cpu.gate.clear()
    body = phone_body(chat_id=CHAT_A)
    body["messages"] = [{"role": "user", "content": "x" * 20_000}]
    async with client() as c:
        task = asyncio.create_task(phone(c, body))
        assert await until(lambda: env.cpu.entered.is_set()), "the CPU member was asked"
        # the engine's lane and gate slot are free while the CPU member is still answering
        assert coordinator.phone_gate.stats()["in_use"] == 0
        assert all(v == 0 for v in coordinator.lane_book._uses.values())
        assert env.pool._active_by_network != {}, "the phone's own one-at-a-time slot is still its"
        env.cpu.gate.set()
        resp = await task

    assert resp.status_code == 200 and b"cpu-one" in resp.content
    assert "internal reference" in system_of(env.engine.chats[0]) and env.engine.chats[0]["n_cache_reuse"] == 0
    (sent,) = env.cpu.payloads
    assert "internal reference" not in json.dumps(sent)
    assert not {"id_slot", "cache_prompt", "n_cache_reuse"} & set(sent)
    assert sum(len(m["content"]) for m in sent["messages"][1:]) <= 8000, "the CPU pool's history cap"
    assert nothing_held(env)


@pytest.mark.anyio
async def test_a_second_phone_gets_the_lane_the_fallback_gave_back(env):
    env.engine.fails = True
    env.cpu.mode = "hold"
    env.cpu.gate.clear()
    async with client(REMOTE) as c1, client(OTHER) as c2, client(THIRD) as c3:
        slow = [asyncio.create_task(phone(c1)), asyncio.create_task(phone(c2))]
        assert await until(lambda: len(env.cpu.payloads) == 2)
        assert coordinator.phone_gate.stats()["in_use"] == 0, "both lanes are free though both phones are on the CPU"
        env.engine.fails = False
        resp = await asyncio.wait_for(phone(c3), 5)
        env.cpu.gate.set()
        await asyncio.gather(*slow)
    assert resp.status_code == 200 and b"engine-one" in resp.content


def lanes_off_cpu_member_gone(env):
    env.registry.update_worker("w1", "10.8.0.2", 50053, "tok1",
                               {"pool": "app", "ram_gb": 12.0, "gpu": None,
                                "app": {"model": "small", "ready": False, "slots": 2}})


async def status_of():
    async with client(("127.0.0.1", 5000)) as admin:
        return (await admin.get("/v1/cluster/status")).json()


@pytest.mark.anyio
async def test_the_status_counts_the_engines_lanes_as_a_serving_machine_with_the_cpu_copy_off(env, monkeypatch):
    """TheWall 2026-10-28: lanes on, APP_LOCAL off, and the status said no machine was online and Qwen3 4B."""
    lanes_off_cpu_member_gone(env)
    monkeypatch.setattr(coordinator, "_coding_ladder", lambda: [
        {"id": "qwen36-35b", "name": "Qwen3.6 35B-A3B", "min_vram_gb": 15, "enabled": True, "file": "m.gguf"}])
    status = await status_of()
    pool = status["app_pool"]

    assert pool["members_ready"] >= 1 and pool["members"] >= 1
    assert pool["active_model"] == "Qwen3.6 35B-A3B" and pool["active_tier"] == "qwen36-35b"
    assert pool["serving"] == "engine" and all(t["unlocked"] for t in pool["tiers"])
    assert status["phone_lanes"] == {"slots": PHONE_SLOTS, "in_use": 0, "waiting": 0, "available": True, "starting": False}


@pytest.mark.anyio
async def test_the_status_names_a_model_off_the_ladder_by_its_file(env):
    lanes_off_cpu_member_gone(env)
    pool = (await status_of())["app_pool"]
    assert pool["members_ready"] == 1 and pool["active_model"] and pool["active_tier"] == "m.gguf"


@pytest.mark.anyio
async def test_the_status_keeps_the_phone_lanes_through_an_engine_restart_and_the_cpu_pool_speaks_for_itself(env, monkeypatch):
    monkeypatch.setattr(coordinator, "_last_phone_slots", 0)
    assert (await status_of())["phone_lanes"]["slots"] == PHONE_SLOTS
    held = coordinator.phone_gate.try_acquire()                     # a stream that is dying with the engine
    env.manager.running = False
    status = await status_of()
    coordinator.phone_gate.release()

    assert held and status["phone_lanes"] == {"slots": PHONE_SLOTS, "in_use": 0, "waiting": 0,
                                              "available": False, "starting": True}
    assert status["app_pool"]["serving"] == "cpu" and status["app_pool"]["active_model"] == "Small 4B"


@pytest.mark.anyio
async def test_the_status_of_a_restart_before_any_engine_was_seen_uses_the_configured_lanes(env, monkeypatch):
    monkeypatch.setattr(coordinator, "_last_phone_slots", 0)
    env.manager.running = False
    lanes = (await status_of())["phone_lanes"]
    assert lanes == {"slots": PHONE_SLOTS, "in_use": 0, "waiting": 0, "available": False, "starting": True}


@pytest.mark.anyio
async def test_the_status_of_a_running_engine_with_lanes_but_the_setting_off_shows_them_unavailable(env):
    env.limits["engine_phone_slots"] = 0
    status = await status_of()
    assert status["phone_lanes"] == {"slots": PHONE_SLOTS, "in_use": 0, "waiting": 0, "available": False, "starting": False}
    assert status["app_pool"]["serving"] == "cpu", "phones are answered by the CPU pool: the engine is not counted"


@pytest.mark.anyio
async def test_the_status_of_an_engine_started_before_lanes_were_configured_is_starting(env, monkeypatch):
    monkeypatch.setattr(coordinator, "_last_phone_slots", 0)
    env.manager.phone_slots = 0
    status = await status_of()
    assert status["phone_lanes"]["starting"] is True and status["phone_lanes"]["available"] is False
    assert status["app_pool"]["serving"] == "cpu"


@pytest.mark.anyio
async def test_a_phone_is_told_dlne_is_restarting_when_the_engine_is_down_and_no_cpu_member_is_ready(env):
    env.manager.running = False
    lanes_off_cpu_member_gone(env)
    async with client() as c:
        resp = await phone(c)

    assert resp.status_code == 200 and b"DLNE is restarting, try again in a moment" in resp.content
    assert b"No machine" not in resp.content and resp.content.endswith(b"data: [DONE]\n\n")
    assert nothing_held(env)


@pytest.mark.anyio
async def test_a_phone_is_told_dlne_is_restarting_while_the_engine_swaps(env, monkeypatch):
    monkeypatch.setattr(coordinator, "is_swapping_models", True)
    lanes_off_cpu_member_gone(env)
    async with client() as c:
        resp = await phone(c)
    assert b"DLNE is restarting" in resp.content


@pytest.mark.anyio
async def test_with_lanes_off_and_no_cpu_member_the_old_notice_stands(env):
    env.limits["engine_phone_slots"] = 0
    env.manager.phone_slots = 0
    lanes_off_cpu_member_gone(env)
    async with client() as c:
        resp = await phone(c)
    assert b"No machine in the CPU pool is free right now" in resp.content and b"restarting" not in resp.content


@pytest.mark.anyio
async def test_the_cpu_pool_still_answers_while_the_engine_restarts(env):
    env.manager.running = False
    async with client() as c:
        resp = await phone(c)
    assert b"cpu-one" in resp.content


@pytest.mark.anyio
async def test_a_phone_with_a_huge_number_in_its_body_is_answered_not_a_500(env):
    async with client() as c:
        resp = await phone(c, phone_body(max_tokens=10**400, location={"lat": 10**400, "lon": 10**400}))
        raw = '{"messages": [{"role": "user", "content": "hi"}], "max_tokens": 1e999, "location": {"lat": 1e999, "lon": 0}}'
        resp2 = await c.post("/v1/app/chat/completions", content=raw, headers={"content-type": "application/json"})
    assert resp.status_code == 200 and resp2.status_code == 200
    assert b"engine-one" in resp.content and env.engine.chats[0]["max_tokens"] == 1024
    assert nothing_held(env)


@pytest.mark.anyio
async def test_a_phone_waiting_for_the_swap_to_finish_is_waited_for_by_the_swap(env):
    assert await coordinator._wait_for_phone_answers(0.1) is True
    held = coordinator.phone_gate.try_acquire()
    assert held and await coordinator._wait_for_phone_answers(0.2, poll=0.02) is False
    asyncio.get_running_loop().call_later(0.1, coordinator.phone_gate.release)
    assert await coordinator._wait_for_phone_answers(3.0, poll=0.02) is True


@pytest.mark.anyio
async def test_an_automatic_switch_is_called_off_while_a_phone_is_mid_answer_and_nothing_is_stopped(env, monkeypatch):
    stopped = []

    async def stop_all():
        stopped.append(1)

    monkeypatch.setattr(env.manager, "astop_all_models", stop_all, raising=False)
    env.engine.release.clear()
    async with client() as c:
        task = asyncio.create_task(phone(c))
        assert await until(lambda: len(env.engine.chats) == 1)
        swap = await coordinator._swap_model_core("other.gguf", 1.0, 0.0, 0.0, drain_timeout=0.2, hold_first=True,
                                                  abort_if_busy=True)
        env.engine.release.set()
        resp = await task

    assert swap["outcome"] == "busy" and stopped == [] and coordinator.is_swapping_models is False
    assert resp.status_code == 200 and b"engine-two" in resp.content, "the phone's answer was not cut"


@pytest.mark.anyio
async def test_a_manual_switch_waits_for_the_phone_answer_before_it_stops_the_engine(env, monkeypatch):
    order = []

    async def stop_all():
        order.append(("stop", env.engine.release.is_set()))
        raise RuntimeError("stop here")

    monkeypatch.setattr(env.manager, "astop_all_models", stop_all, raising=False)
    env.engine.release.clear()
    async with client() as c:
        task = asyncio.create_task(phone(c))
        assert await until(lambda: len(env.engine.chats) == 1)
        swap = asyncio.create_task(coordinator._swap_model_core("other.gguf", 1.0, 0.0, 0.0, drain_timeout=0.2))
        await asyncio.sleep(0.5)
        assert order == [], "still waiting for the phone's answer"
        assert coordinator.is_swapping_models is True, "new phones already go to the CPU pool meanwhile"
        env.engine.release.set()
        resp = await task
        with pytest.raises(RuntimeError, match="stop here"):
            await swap
    coordinator.is_swapping_models = False

    assert order == [("stop", True)] and b"engine-two" in resp.content


@pytest.mark.anyio
async def test_a_coding_chat_that_waited_for_a_slot_is_pinned_on_the_engine_that_is_running_now(env):
    """It queued while the engine had no lanes; the engine was restarted with lanes during the wait."""
    env.manager.phone_slots = 0
    held = [coordinator.coding_gate.try_acquire() for _ in range(CODING_SLOTS)]
    assert all(held)
    body = {"model": MODEL, "stream": True, "id_slot": 3, "messages": [{"role": "user", "content": "write code"}]}
    async with client(("127.0.0.1", 5000)) as c:
        task = asyncio.create_task(code(c, body))
        assert await until(lambda: coordinator.coding_gate.stats()["waiting"] == 1)
        env.manager.phone_slots = PHONE_SLOTS                                    # restarted with lanes
        env.manager.process = SimpleNamespace(poll=lambda: None)
        for _ in range(CODING_SLOTS):
            coordinator.coding_gate.release()
        resp = await task

    assert resp.status_code == 200
    (sent,) = env.engine.chats
    assert sent["id_slot"] in range(CODING_SLOTS), "a coding lane of the new engine, not the client's id_slot 3"
    assert all(v == 0 for v in coordinator.lane_book._uses.values())


@pytest.mark.anyio
async def test_a_coding_chat_is_not_pinned_behind_another_chat_while_autocomplete_holds_the_idle_lane(env):
    book = coordinator.lane_book
    chat_a = book.take(0, CODING_SLOTS)
    fim = book.take(0, CODING_SLOTS, short=True)
    env.engine.release.set()
    async with client(("127.0.0.1", 5000)) as c:
        resp = await code(c)
    (sent,) = env.engine.chats
    assert resp.status_code == 200 and sent["id_slot"] not in (chat_a,), "the lane with no chat on it"
    book.give(fim, short=True)
    book.give(chat_a)
