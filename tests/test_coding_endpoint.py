"""
The coordinator's coding-member surface (docs/coding-members.md, section 5): POST
/v1/chat/completions and /v1/completions routed to members, GET /v1/coding/route, the heartbeat
answer's ``role`` and ``coding_tier``, and the ``coding_members`` counts in /v1/cluster/status.

Driven through the real ASGI app (httpx ASGITransport, so the real middleware stack runs)
against member stand-ins on real loopback sockets (tests/coding_router_fakes.py). Members come
from a real WorkerRegistry plus a stand-in for the reverse tunnel's relay-port lookup, so
coordinator._coding_members() and CodingRouter.members() run unmodified. The local engine is a
stub that records whether it was reached: "fell back to the local engine" means exactly that.
"""

import asyncio
import ipaddress
import json
import time
from collections import deque
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from engine import canary
from engine import config as engine_config
from engine import coding_router as cr_module
from engine import coordinator, e2e, link_paths, reverse_tunnel
from engine.coding_router import CodingRouter, E2E_ALG
from engine.inference_manager import NETWORK_MAXED_OUT_MESSAGE
from engine.priority import CodingGate, UsageWindow
from engine.throughput import POOL_CODING, throughput
from engine.registry import WorkerRegistry
from engine.version import VERSION
from tests.coding_router_fakes import FakeMember, make_identity, open_sealed_json, open_sealed_stream, sign_block

seal_request = e2e.seal_request
generate_keypair = e2e.generate_keypair

REMOTE = ("203.0.113.9", 40000)
HOUSEHOLD_IP = "198.51.100.200"         # the coordinator's own public address in these tests
MODEL_FILE = "Qwen3.6-35B-A3B-UD-IQ3_S.gguf"
TIER = {"id": "qwen36-a3b", "name": "Qwen3.6 35B-A3B", "file": MODEL_FILE, "min_vram_gb": 15, "enabled": True,
        "context": 65536, "blocked": None, "size": 13676723168, "files": [{"file": MODEL_FILE}]}
CHAT = {"model": MODEL_FILE, "stream": True, "messages": [{"role": "user", "content": "hello"}]}
JSON_HEADERS = {"Content-Type": "application/json"}
ANY = {**JSON_HEADERS, "X-DLNE-Privacy": "any"}
# How the coordinator's own engine answers in these tests: the fake engine cannot fit the swarm's own model, which
# 2.6.2 tells as an engine between restarts (the reconfiguring nudge, "engine_unavailable"), not "switch your chat".
LOCAL_ANSWER = b"reorganising"


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


class LocalEngine:
    """The coordinator's own coding engine, reduced to 'was I asked to start?'. It never can."""

    def __init__(self):
        self.starts = []
        self.endpoints = []         # the RPC endpoints the running engine was started with
        self.breaks = False         # the engine cannot start at all (the handler's HTTP 500)

    @property
    def current_endpoints(self):
        return list(self.endpoints)

    async def aestimate_model_vram_gb(self, model):
        return 1.0

    async def astart_model(self, model, endpoints, vram, sysram, queue_channels=2):
        self.starts.append(model)
        if self.breaks:
            return False, 0, "the engine broke"
        return False, 0, NETWORK_MAXED_OUT_MESSAGE

    def get_active_model_names(self):
        return []

    async def aget_loading_telemetry(self, model):
        return {}

    async def asnapshot_model(self, model):
        return None


class Exploding:
    """Stands in for the router when members are off: touching it at all fails the test."""

    def __getattr__(self, name):
        raise AssertionError(f"the coding router was used ({name}) with coding members off")


@pytest.fixture(autouse=True)
def canary_book(tmp_path):
    """The canary book (flags that stop a member getting traffic) in a private file, never the install's."""
    previous = canary.set_book(canary.CanaryBook(path=str(tmp_path / "canaries.json")))
    yield canary.book()
    canary.set_book(previous)


@pytest.fixture(scope="module")
def fakes():
    servers = {name: FakeMember(name).start() for name in ("a", "b", "c")}
    yield servers
    for server in servers.values():
        server.stop()


@pytest.fixture
def env(fakes, monkeypatch, tmp_path):
    for fake in fakes.values():
        fake.reset()
    clock, wall = Clock(), Clock()
    wall.t = time.time()                     # epoch seconds: the router's last_served_at, the scheduler's "now"
    router = CodingRouter(clock=clock, wall_clock=wall)
    monkeypatch.setattr(coordinator, "coding_router", router)
    registry = WorkerRegistry()
    monkeypatch.setattr(coordinator, "registry", registry)
    relays = {}
    monkeypatch.setattr(reverse_tunnel, "reverse_bridge",
                        SimpleNamespace(get_relay_port=lambda ip, port: relays.get((ip, port))))
    limits = {
        "trust_proxy_headers": False, "enforce_contrib_clause": False, "starter_quota_inferences": 200,
        "network_key": "", "allow_unsigned_workers": True, "coding_members_enabled": True,
        "trusted_members": [], "coding_member_queue_seconds": 3, "auto_swap_max_rtt_ms": 15.0,
        "app_tier_min_members": 2, **engine_config.CPU_NODE_DEFAULTS,
    }
    monkeypatch.setattr(coordinator, "get_worker_limits", lambda: dict(limits))
    monkeypatch.setattr(coordinator, "coding_route_window", UsageWindow())
    throughput.reset()
    monkeypatch.setattr(coordinator, "_engine_http", None)
    monkeypatch.setattr(coordinator, "_coding_tiers", [dict(TIER)])
    monkeypatch.setattr(coordinator, "starter_quota_mgr",
                        coordinator.StarterQuotaManager(str(tmp_path / "quota.json"), default_quota=200))
    local = LocalEngine()
    monkeypatch.setattr(coordinator, "manager", local)
    monkeypatch.setattr(coordinator, "_planned_endpoints", lambda gb: [])
    monkeypatch.setattr(coordinator, "is_swapping_models", False)
    monkeypatch.setattr(coordinator, "is_recalculating_queue", False)
    # This coordinator's household (engine/link_paths.py): public address HOUSEHOLD_IP, LAN 192.168.40.0/24.
    household = link_paths.Household(resolve=lambda: {HOUSEHOLD_IP},
                                      interfaces=lambda: ([ipaddress.IPv4Network("192.168.40.0/24")], {"192.168.40.23"}),
                                      refresh_seconds=1e9)
    household.refresh_now()
    monkeypatch.setattr(link_paths, "household", household)
    monkeypatch.setattr(link_paths, "lan_routes", link_paths.LanRoutes())
    count = {"n": 0}
    ips = {}

    def add(fake, worker_id=None, *, slots=2, busy=0, decode=33.0, prefill=121.0, ready=True, tunnel=True,
            pubkey=True, model=MODEL_FILE, identity="own", signed_at=None, tamper=None, tier="qwen36-a3b"):
        """Register a member whose heartbeat block is signed by the fake's own identity key, or by
        ``identity`` = (key, public b64) of another, or unsigned (None); ``tamper`` changes the block after signing."""
        if worker_id is None:
            count["n"] += 1
            worker_id = f"member-{fake.name}-{count['n']}"
        if worker_id not in ips:
            ips[worker_id] = f"10.8.0.{len(ips) + 2}"
        block = {"model": model, "tier": tier, "ready": ready, "slots": slots, "busy": busy,
                 "decode_tps": decode, "prefill_tps": prefill, "no_trace": True}
        if pubkey and fake.pubkey:
            block["pubkey"] = fake.pubkey
        if identity is not None:
            key, public = (fake.identity_key, fake.identity_pub) if identity == "own" else identity
            block = sign_block({**block, "identity": public}, key, signed_at)
            block.update(tamper or {})
        registry.update_worker(worker_id, ips[worker_id], 50053, f"tok-{worker_id}",
                               {"pool": "coding", "ram_gb": 32.0, "gpu": {"has_gpu": True, "vram_total_gb": 8.0},
                                "coding": block})
        if tunnel:
            relays[(ips[worker_id], 50053)] = fake.port
        else:
            relays.pop((ips[worker_id], 50053), None)
        return worker_id

    return SimpleNamespace(router=router, registry=registry, add=add, limits=limits, fakes=fakes, clock=clock,
                           wall=wall, local=local, relays=relays, ips=ips)


def client(ip=REMOTE, headers=None):
    return AsyncClient(transport=ASGITransport(app=coordinator.app, client=ip), base_url="http://test",
                       headers=headers or {})


async def chat(c, body=None, headers=None):
    return await c.post("/v1/chat/completions", content=json.dumps(body if body is not None else CHAT),
                        headers=headers or ANY)


async def complete(c, body=None, headers=None):
    return await c.post("/v1/completions", content=json.dumps(body or {"prompt": "def f(", "model": MODEL_FILE}),
                        headers=headers or ANY)


def served_by(resp, name):
    return resp.status_code == 200 and f"{name}-one".encode() in resp.content


@pytest.fixture
def tiers(env, monkeypatch):
    env.limits["enforce_contrib_clause"] = True
    monkeypatch.setattr(coordinator.api_key_manager, "verify_key", lambda token: token == "dlne_sk_good")


def member_name(env, pubkey):
    """a, b or c: the fake whose current key this is."""
    return next((name for name, fake in env.fakes.items() if fake.pubkey == pubkey), None)


async def via_member(env, c, *, session="s", privacy="any", body=None, headers=None):
    """What the extension does: ask for a route and, when one is offered, send a sealed request on it.
    The name of the member that answered (a, b or c), or None when no route was offered or the member did
    not answer 200."""
    route = await lookup(c, session, privacy)
    if not route["route"]:
        return None
    _raw, _key, resp = await sealed(env, c, route, body or {"messages": [{"role": "user", "content": "hi"}],
                                                            "stream": True}, headers)
    return member_name(env, route["pubkey"]) if resp.status_code == 200 else None


# ======================================================================================
# A member answers sealed requests, and only sealed requests
# ======================================================================================

@pytest.mark.anyio
async def test_a_sealed_chat_request_is_answered_by_a_member_and_never_touches_the_local_engine(env):
    env.add(env.fakes["a"])
    async with client() as c:
        route = await lookup(c)
        _raw, key, resp = await sealed(env, c, route, {"messages": [{"role": "user", "content": "hi"}], "stream": True})
    assert resp.status_code == 200 and open_sealed_stream(resp.content, key) == ["a-one ", "a-two"]
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["x-dlne-served"] == "member" and resp.headers["cache-control"] == "no-store"
    assert env.fakes["a"].requests[0].path == "/v1/chat/completions"
    assert env.local.starts == [] and env.router.in_flight() == 0


@pytest.mark.anyio
@pytest.mark.parametrize("privacy", [None, "open", "trusted", "any"])
@pytest.mark.parametrize("with_route", [False, True])
@pytest.mark.parametrize("streamed", [True, False])
async def test_a_plain_request_never_goes_to_a_member(env, privacy, with_route, streamed):
    """Not with a route, not with every privacy mode, not streamed or not: only the local engine reads plaintext."""
    env.limits["trusted_members"] = [env.fakes["a"].trust_id]
    env.add(env.fakes["a"], "wa")
    headers = dict(JSON_HEADERS)
    if privacy:
        headers["X-DLNE-Privacy"] = privacy
    async with client() as c:
        if with_route:
            headers["X-DLNE-Route"] = (await lookup(c, "s", "any"))["route"]
        resp = await chat(c, {**CHAT, "stream": streamed}, headers)
    assert env.fakes["a"].requests == [], "the member was sent nothing"
    assert env.local.starts and LOCAL_ANSWER in resp.content, "the coordinator's own engine answered"
    assert "x-dlne-served" not in resp.headers


@pytest.mark.anyio
async def test_a_plain_body_that_mentions_the_envelope_key_is_still_plain(env):
    env.add(env.fakes["a"], "wa")
    body = {**CHAT, "messages": [{"role": "user", "content": 'the key is "dlne_e2e"'}], "dlne_e2e": "not an object"}
    async with client() as c:
        route = await lookup(c)
        resp = await chat(c, body, {**ANY, "X-DLNE-Route": route["route"]})
    assert env.fakes["a"].requests == [] and env.local.starts and LOCAL_ANSWER in resp.content


@pytest.mark.anyio
async def test_the_swarms_own_model_not_fitting_is_an_engine_between_restarts(env):
    """2026-10-28 10:11: with the laptop's shard gone, IQ4_XS could not start, and the agent was told "active models
    are: [None]. Please switch your chat", which it took for the model's answer and ended a 330-step task on."""
    async with client() as c:
        resp = await chat(c)
    assert LOCAL_ANSWER in resp.content and b'"dlne_error": "engine_unavailable"' in resp.content
    assert b"switch your chat" not in resp.content


@pytest.mark.anyio
async def test_a_plain_completion_never_goes_to_a_member_either(env):
    env.add(env.fakes["a"], "wa")
    async with client() as c:
        route = await lookup(c)
        resp = await complete(c, headers={**ANY, "X-DLNE-Route": route["route"]})
    assert env.fakes["a"].requests == [] and env.local.starts
    assert resp.status_code == 500          # the stub engine cannot start: that is the local path's answer


@pytest.mark.anyio
@pytest.mark.parametrize("slots, busy, free", [(2, 0, True), (1, 0, False), (2, 1, False)])
async def test_autocomplete_never_takes_a_members_last_free_slot(env, slots, busy, free):
    """A sealed completion needs a slot to spare, so that agents keep theirs: otherwise the re-route code."""
    fake = env.fakes["a"]
    env.add(fake, slots=slots, busy=busy)
    async with client() as c:
        route = await lookup(c)
        envelope, key = seal_request({"prompt": "def f("}, route["pubkey"])
        resp = await c.post("/v1/completions", content=json.dumps(envelope),
                            headers={**JSON_HEADERS, "X-DLNE-Route": route["route"]})
    if free:
        assert resp.status_code == 200 and fake.requests[0].path == "/v1/completions"
    else:
        assert_reroute(resp, "busy")
        assert fake.requests == []


# ======================================================================================
# Anonymity
# ======================================================================================

@pytest.mark.anyio
async def test_a_member_learns_nothing_about_who_asked(env, tiers):
    wid = env.add(env.fakes["a"])
    secret_headers = {
        "Authorization": "Bearer dlne_sk_SECRETKEY", "X-Forwarded-For": "198.51.100.77",
        "X-Real-IP": "198.51.100.77", "Cookie": "session=SECRETCOOKIE", "X-DLNE-Session": "SESSION-ID-123",
        "User-Agent": "my-editor/9.9 (alice-laptop)", "Last-Event-ID": "42", "Referer": "https://alice.example/",
        "X-DLNE-Privacy": "any",
    }
    async with client() as c:
        route = await lookup(c)
        raw, _key, resp = await sealed(env, c, route, {"messages": [{"role": "user", "content": "x"}], "stream": True},
                                       secret_headers)
    assert resp.status_code == 200
    seen = env.fakes["a"].requests[0]
    # Only plumbing headers reach a member: nothing the client, its key or the coordinator added.
    assert set(seen.headers) <= {"host", "accept", "accept-encoding", "connection", "content-type",
                                 "content-length", "user-agent"}
    assert "my-editor" not in seen.headers.get("user-agent", "")
    assert seen.raw == raw
    haystack = json.dumps(seen.headers) + seen.raw.decode()
    for needle in ("SECRETKEY", "198.51.100.77", REMOTE[0], "SECRETCOOKIE", "SESSION-ID-123", "alice", "Last-Event",
                   wid, "X-DLNE"):
        assert needle not in haystack, needle


# ======================================================================================
# Which member (the route lookup picks it)
# ======================================================================================

@pytest.mark.anyio
async def test_a_session_sticks_to_its_member_and_other_sessions_spread(env):
    env.add(env.fakes["a"], "wa", prefill=200.0, slots=8)     # the faster reader wins the first tie
    env.add(env.fakes["b"], "wb", prefill=100.0, slots=8)
    async with client() as c:
        assert await via_member(env, c, session="agent-1") == "a"
        env.add(env.fakes["a"], "wa", prefill=200.0, busy=1, slots=8)     # a is now busier than b: b would be picked...
        assert await via_member(env, c, session="agent-1") == "a", "...but the session stays where its prompt is cached"
        assert await via_member(env, c, session="agent-2") == "b"
        env.clock.t += 30 * 60 + 1                                         # 30 minutes without the session
        assert await via_member(env, c, session="agent-1") == "b"


@pytest.mark.anyio
async def test_a_full_sticky_member_is_left_for_the_least_busy(env):
    env.add(env.fakes["a"], "wa", prefill=200.0)
    env.add(env.fakes["b"], "wb", prefill=100.0)
    async with client() as c:
        assert await via_member(env, c, session="agent-1") == "a"
        env.add(env.fakes["a"], "wa", prefill=200.0, busy=2)              # full
        assert await via_member(env, c, session="agent-1") == "b"


@pytest.mark.anyio
async def test_the_least_busy_member_gets_the_route(env):
    env.add(env.fakes["a"], "wa", slots=2, busy=1)
    env.add(env.fakes["b"], "wb", slots=2, busy=0)
    env.add(env.fakes["c"], "wc", slots=4, busy=2)          # half full, like a
    async with client() as c:
        assert await via_member(env, c) == "b"


@pytest.mark.anyio
async def test_in_flight_requests_count_as_load(env):
    """One request held open on a's only slot: the next route is b's, though a's heartbeat says nothing is busy."""
    env.add(env.fakes["a"], "wa", slots=1, busy=0, prefill=200.0)
    env.add(env.fakes["b"], "wb", slots=2, prefill=100.0)
    env.fakes["a"].mode = "hold"
    env.fakes["a"].gate.clear()
    async with client() as c:
        held = asyncio.create_task(via_member(env, c, session="s1"))
        assert await until(lambda: env.router.in_flight() == 1)
        assert await via_member(env, c, session="s2") == "b"
        env.fakes["a"].gate.set()
        assert await held == "a" and env.router.in_flight() == 0


# ======================================================================================
# Privacy, floors, identity
# ======================================================================================

@pytest.mark.anyio
async def test_the_privacy_mode_decides_which_members_may_be_offered(env):
    env.limits["trusted_members"] = [env.fakes["a"].trust_id]
    env.add(env.fakes["a"], "wa", prefill=100.0, slots=8)
    env.add(env.fakes["b"], "wb", prefill=200.0, slots=8)                 # faster, but not trusted
    async with client() as c:
        for i, privacy in enumerate((None, "trusted", "nonsense", "Open", " open", "")):
            assert await via_member(env, c, session=f"s{i}", privacy=privacy) == "a", privacy    # missing or unknown: trusted
        assert await via_member(env, c, session="open", privacy="open") == "b"
        assert await via_member(env, c, session="any", privacy="any") == "b"


@pytest.mark.anyio
async def test_with_no_trusted_member_a_private_request_is_offered_no_route(env):
    env.add(env.fakes["a"])
    async with client() as c:
        assert await lookup(c, privacy=None) == {"route": None}
        assert await lookup(c, privacy="trusted") == {"route": None}
        assert (await lookup(c, privacy="any"))["route"]


@pytest.mark.anyio
@pytest.mark.parametrize("decode, prefill", [(14.9, 121.0), (33.0, 79.9), (0.0, 0.0)])
async def test_a_member_below_the_speed_floors_is_not_offered(env, decode, prefill):
    env.add(env.fakes["a"], decode=decode, prefill=prefill)
    async with client() as c:
        assert await lookup(c) == {"route": None}


@pytest.mark.anyio
async def test_the_floors_come_from_config(env):
    env.limits["coding_member_min_decode_tps"] = 40.0
    env.add(env.fakes["a"], decode=33.0)
    env.add(env.fakes["b"], decode=45.0)
    async with client() as c:
        assert await via_member(env, c) == "b"


@pytest.mark.anyio
@pytest.mark.parametrize("kwargs", [{"ready": False}, {"tunnel": False}])
async def test_a_member_that_is_not_ready_or_not_reachable_is_not_offered(env, kwargs):
    env.add(env.fakes["a"], **kwargs)
    async with client() as c:
        assert await lookup(c) == {"route": None}


@pytest.mark.anyio
async def test_a_worker_id_in_trusted_members_trusts_nothing(env):
    """Anyone can claim any worker id: only a trust id (from the member's own key) counts."""
    env.limits["trusted_members"] = ["wa", "member-a-1", "wb"]
    env.add(env.fakes["a"], "wa")
    env.add(env.fakes["b"], "wb")
    async with client() as c:
        assert await lookup(c, privacy="trusted") == {"route": None}


@pytest.mark.anyio
async def test_trust_ids_match_whole_and_in_either_case(env):
    trust = env.fakes["a"].trust_id
    env.add(env.fakes["a"], "wa", slots=64)
    async with client() as c:
        for listed in ([trust.upper()], [trust], [f" {trust.upper()} "], [trust[:16]], [trust[:-1]], [trust + "0"],
                       ["0" + trust], [trust.upper()[:16] + trust[16:]]):
            env.limits["trusted_members"] = listed
            offered = bool((await lookup(c, privacy="trusted"))["route"])
            assert offered == (listed[0].strip().lower() == trust), listed
    # And a comma-separated config value parses the same way.
    assert engine_config.parse_trusted_members(f"{trust.upper()}, junk, {trust[:8]}, wa") == [trust]


@pytest.mark.anyio
async def test_an_impostor_with_the_same_worker_id_but_its_own_key_is_not_trusted(env):
    env.limits["trusted_members"] = [env.fakes["a"].trust_id]
    env.add(env.fakes["b"], "wa")                 # b calls itself "wa", signs with its own key
    async with client() as c:
        assert await lookup(c, privacy="trusted") == {"route": None}
        assert await via_member(env, c, privacy="any") == "b", "still a member for work that may go anywhere"


@pytest.mark.anyio
@pytest.mark.parametrize("what", ["unsigned", "tampered_slots", "tampered_pubkey", "tampered_identity",
                                  "stale", "future", "wrong_key"])
async def test_a_block_that_does_not_verify_is_dropped_and_its_member_is_never_offered(env, what):
    fake = env.fakes["a"]
    other = make_identity()
    now = int(time.time())
    kwargs = {
        "unsigned": {"identity": None},
        "tampered_slots": {"tamper": {"slots": 64}},
        "tampered_pubkey": {"tamper": {"pubkey": generate_keypair()[1]}},
        "tampered_identity": {"tamper": {"identity": other[1]}},
        "stale": {"signed_at": now - 400},
        "future": {"signed_at": now + 400},
        "wrong_key": {"identity": (other[0], fake.identity_pub)},       # signed by a key that is not the claimed identity
    }[what]
    env.add(fake, "wa", **kwargs)
    async with client() as c:
        assert await lookup(c) == {"route": None}
    assert fake.requests == []
    status = coordinator._coding_members_status()
    assert (status["members"], status["ready"], status["slots"]) == (1, 0, 0)


@pytest.mark.anyio
async def test_a_block_signed_within_the_skew_is_believed(env):
    env.add(env.fakes["a"], "wa", signed_at=int(time.time()) - 290)
    async with client() as c:
        assert await via_member(env, c) == "a"


@pytest.mark.anyio
async def test_a_dropped_block_gives_a_member_nothing_it_claimed(env):
    env.add(env.fakes["a"], "wa", identity=None, slots=64, busy=0, decode=999.0, prefill=999.0)
    members = env.router.all_members(env.registry, lambda ip, port: 1234, env.limits)
    assert len(members) == 1 and members[0].trust_id is None and not members[0].routable
    assert (members[0].slots, members[0].pubkey, members[0].model, members[0].decode_tps) == (1, None, "", 0.0)


@pytest.mark.anyio
async def test_a_copied_block_is_believed_for_neither_when_tied_and_only_the_newest_otherwise(env):
    """The identity's owner re-signs every heartbeat; a copy under another worker id is always older."""
    fake = env.fakes["a"]
    now = int(time.time())
    env.add(fake, "real", signed_at=now)
    env.add(fake, "copy", signed_at=now - 20)               # a replayed older block, another worker id
    members = {m.worker_id: m for m in env.router.all_members(env.registry, lambda ip, port: 1234, env.limits)}
    assert members["real"].trust_id == fake.trust_id and members["copy"].trust_id is None
    env.add(fake, "copy", signed_at=now)                    # a tie: nobody can say which is the owner
    members = {m.worker_id: m for m in env.router.all_members(env.registry, lambda ip, port: 1234, env.limits)}
    assert members["real"].trust_id is None and members["copy"].trust_id is None
    env.add(fake, "real", signed_at=now + 5)                # the owner's next heartbeat settles it
    members = {m.worker_id: m for m in env.router.all_members(env.registry, lambda ip, port: 1234, env.limits)}
    assert members["real"].trust_id == fake.trust_id and members["copy"].trust_id is None


@pytest.mark.anyio
async def test_a_blocked_member_is_not_offered(env):
    env.add(env.fakes["a"])
    env.router.block(env.fakes["a"].trust_id)
    async with client() as c:
        assert await lookup(c) == {"route": None}


@pytest.mark.anyio
async def test_a_member_a_canary_caught_receives_nothing_until_the_operator_clears_it(env, canary_book):
    trust = env.fakes["a"].trust_id
    env.add(env.fakes["a"], "wa")
    decoy = canary_book.issue(trust)
    assert canary_book.presented_key(decoy.key, "198.51.100.5") == trust     # the decoy key was used
    async with client() as c:
        assert await lookup(c) == {"route": None}
        assert coordinator._coding_members_status()["ready"] == 0
        assert canary_book.unflag(trust)
        assert await via_member(env, c) == "a"


@pytest.mark.anyio
async def test_a_flag_on_another_identity_does_not_stop_a_member(env, canary_book):
    """Flags are by trust id: a flagged machine's worker id, or anyone else's, means nothing."""
    env.add(env.fakes["a"], "wa")
    canary_book.presented_key(canary_book.issue(env.fakes["b"].trust_id).key, "198.51.100.5")
    async with client() as c:
        assert await via_member(env, c) == "a"


@pytest.mark.anyio
async def test_a_sealed_request_to_a_member_a_canary_caught_must_be_routed_again(env, canary_book):
    env.add(env.fakes["a"], "wa")
    async with client() as c:
        route = await lookup(c)
        canary_book.presented_key(canary_book.issue(env.fakes["a"].trust_id).key, "198.51.100.5")
        _raw, _key, resp = await sealed(env, c, route, {"messages": []})
    assert_reroute(resp, "gone")
    assert env.fakes["a"].requests == []


# ======================================================================================
# Canaries travel the routed path
# ======================================================================================

def canary_scheduler(env):
    import random
    return canary.CanaryScheduler(coordinator._canary_members, coordinator._canary_forward,
                                  trusted=coordinator._canary_trusted, per_day=lambda: 10 ** 6,
                                  rng=random.Random(7), clock=env.wall)


@pytest.mark.anyio
async def test_a_canary_is_sealed_and_handed_to_the_member_along_the_routed_path(env, canary_book):
    fake = env.fakes["a"]
    env.add(fake, "wa")
    async with client() as c:
        assert await via_member(env, c) == "a"                 # it has served a user: it is active
    served_at = env.wall()
    env.wall.t += 60
    assert await canary_scheduler(env).tick() == [fake.trust_id], "the scheduler names a member by trust id"
    real, decoy = fake.requests
    assert decoy.path == "/v1/chat/completions" and decoy.raw.startswith(b'{"dlne_e2e":')
    assert set(decoy.headers) <= {"host", "accept", "accept-encoding", "connection", "content-type",
                                  "content-length", "user-agent"}
    assert b"dlne.rossgilsonlabs.dev/c/" not in decoy.raw, "the decoy is inside the sealed body"
    opened = json.dumps(fake.opened[1])                     # [0] is the user's own sealed request
    assert "https://dlne.rossgilsonlabs.dev/c/" in opened, "and the member finds its own decoy link in it"
    assert "dlne_ck_" not in opened, "(no decoy key: the extension's redactor empties every key, so one would be a tell)"
    assert env.router.in_flight() == 0 and canary_book.sent_since(fake.trust_id, 0) == 1
    (member,) = env.router.members(env.registry, lambda ip, port: 1, env.limits)
    assert member.last_served_at == served_at, "a canary does not make a member look busy serving users"


@pytest.mark.anyio
async def test_a_sealed_member_stream_counts_a_token_per_frame_but_not_the_sealed_end(env):
    fake = env.fakes["a"]
    env.add(fake)
    fake.texts = ["t0 ", "t1 ", "t2 ", "t3 ", "t4"]
    async with client() as c:
        route = await lookup(c)
        _raw, key, resp = await sealed(env, c, route, {"messages": [{"role": "user", "content": "x"}]})
    assert open_sealed_stream(resp.content, key) == fake.texts
    assert throughput.total(POOL_CODING, 3600) == 5, "five tokens; the sealed end marker and [DONE] are not"
    assert throughput.total("phone", 3600) == 0


@pytest.mark.anyio
async def test_a_canarys_answer_is_not_counted(env):
    fake = env.fakes["a"]
    env.add(fake, "wa")
    async with client() as c:
        assert await via_member(env, c) == "a"
    before = throughput.total(POOL_CODING, 3600)
    assert before == 2
    env.wall.t += 60
    assert await canary_scheduler(env).tick() == [fake.trust_id]
    assert len(fake.requests) == 2, "the canary reached the member and was answered"
    assert throughput.total(POOL_CODING, 3600) == before


@pytest.mark.anyio
async def test_canaries_go_only_to_members_that_served_a_user_in_the_last_two_hours(env):
    fake = env.fakes["a"]
    env.add(fake, "wa")
    assert await canary_scheduler(env).tick() == [], "never served anyone"
    async with client() as c:
        assert await via_member(env, c) == "a"
    env.wall.t += 2 * 3600 + 1
    assert await canary_scheduler(env).tick() == [], "last served more than two hours ago"
    env.wall.t -= 60
    assert await canary_scheduler(env).tick() == [fake.trust_id]
    assert len(fake.requests) == 2


@pytest.mark.anyio
async def test_canaries_skip_trusted_members_and_do_nothing_while_members_are_off(env):
    fake = env.fakes["a"]
    env.add(fake, "wa")
    async with client() as c:
        assert await via_member(env, c) == "a"
    env.limits["trusted_members"] = [fake.trust_id.upper()]
    assert await canary_scheduler(env).tick() == []
    env.limits["trusted_members"] = []
    env.limits["coding_members_enabled"] = False
    assert await canary_scheduler(env).tick() == []
    assert len(fake.requests) == 1


@pytest.mark.anyio
async def test_a_canary_needs_a_member_that_can_take_it(env):
    env.add(env.fakes["a"], "wa", slots=1, busy=1)             # full
    env.add(env.fakes["b"], "wb", pubkey=False)                # no key to seal to
    env.add(env.fakes["c"], "wc", ready=False)
    for fake in env.fakes.values():                            # all three have served users recently
        env.router._last_served[fake.trust_id] = env.wall()
    assert await canary_scheduler(env).tick() == []
    assert not any(fake.requests for fake in env.fakes.values())


# ======================================================================================
# Failure
# ======================================================================================

@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["500", "400", "503", "die_before_first_chunk"])
async def test_a_member_that_fails_is_left_out_of_new_routes_for_a_while(env, mode):
    env.add(env.fakes["a"], "wa", prefill=200.0)
    env.add(env.fakes["b"], "wb", prefill=100.0)
    env.fakes["a"].mode = mode
    async with client() as c:
        route = await lookup(c, "s1")
        assert member_name(env, route["pubkey"]) == "a"
        _raw, _key, resp = await sealed(env, c, route, {"messages": []})
        assert_reroute(resp, "busy" if mode == "503" else "failed", "member says", "SECRET")
        assert member_name(env, (await lookup(c, "s2"))["pubkey"]) == "b", "a is left out for half a minute"
        env.clock.t += 31
        env.fakes["a"].mode = "ok"
        assert member_name(env, (await lookup(c, "s3"))["pubkey"]) == "a"
    assert env.local.starts == [] and env.router.in_flight() == 0
    assert len(env.fakes["a"].requests) == 1 and env.fakes["b"].requests == []


@pytest.mark.anyio
async def test_a_member_that_dies_mid_answer_ends_it_with_an_event_and_frees_its_slot(env):
    env.add(env.fakes["a"])
    env.fakes["a"].mode = "die_mid_stream"
    async with client() as c:
        route = await lookup(c)
        _raw, _key, resp = await sealed(env, c, route, {"messages": [{"role": "user", "content": "x"}]})
    assert resp.status_code == 200 and b'"member_dropped"' in resp.content
    assert resp.content.endswith(b"data: [DONE]\n\n")
    assert env.router.in_flight() == 0


@pytest.mark.anyio
async def test_a_whole_answer_over_8_mib_is_a_failed_member_not_a_buffered_answer(env):
    """SR-10: a member cannot make the coordinator hold an unbounded answer in memory."""
    env.add(env.fakes["a"])
    env.fakes["a"].mode = "huge"
    async with client() as c:
        route = await lookup(c)
        _raw, _key, resp = await sealed(env, c, route, {"messages": [{"role": "user", "content": "x"}]})
    assert_reroute(resp, "failed")
    assert len(resp.content) < 1024 and env.router.in_flight() == 0


@pytest.mark.anyio
async def test_a_member_that_never_starts_answering_is_given_up_on_at_the_exchange_deadline(env, monkeypatch):
    monkeypatch.setattr(cr_module, "MEMBER_EXCHANGE_DEADLINE_SECONDS", 0.6)
    fake = env.fakes["a"]
    env.add(fake)
    fake.mode = "hold_first"
    fake.gate.clear()
    try:
        async with client() as c:
            route = await lookup(c)
            started = time.monotonic()
            _raw, _key, resp = await sealed(env, c, route, {"messages": [{"role": "user", "content": "x"}]})
            waited = time.monotonic() - started
    finally:
        fake.gate.set()
    assert_reroute(resp, "failed")
    assert 0.5 <= waited < 5 and env.router.in_flight() == 0


@pytest.mark.anyio
async def test_a_member_that_stalls_mid_answer_is_cut_off_at_the_exchange_deadline(env, monkeypatch):
    monkeypatch.setattr(cr_module, "MEMBER_EXCHANGE_DEADLINE_SECONDS", 0.8)
    fake = env.fakes["a"]
    env.add(fake)
    fake.mode = "hold"                       # the first chunk, then nothing
    fake.gate.clear()
    try:
        async with client() as c:
            route = await lookup(c)
            started = time.monotonic()
            _raw, _key, resp = await sealed(env, c, route, {"messages": [{"role": "user", "content": "x"}]})
            waited = time.monotonic() - started
    finally:
        fake.gate.set()
    assert resp.status_code == 200 and b'"member_dropped"' in resp.content
    assert resp.content.endswith(b"data: [DONE]\n\n")
    assert 0.7 <= waited < 5 and env.router.in_flight() == 0


@pytest.mark.anyio
@pytest.mark.parametrize("raw", ["{", "", "null", "[]", '"x"', '{"messages": []}', '{"messages": "hi"}'])
async def test_a_body_that_is_not_a_chat_request_is_left_to_the_local_path(env, raw):
    env.add(env.fakes["a"])
    async with client() as c:
        resp = await c.post("/v1/chat/completions", content=raw, headers=ANY)
    assert env.fakes["a"].requests == []
    assert resp.status_code in (400, 500, 502, 200)        # whatever the local path says; never a member's


# ======================================================================================
# Off means off
# ======================================================================================

@pytest.mark.anyio
async def test_with_members_off_nothing_changes(env, monkeypatch):
    env.add(env.fakes["a"])
    env.limits["coding_members_enabled"] = False
    monkeypatch.setattr(coordinator, "coding_router", Exploding())
    async with client() as c:
        resp = await chat(c)
        completion = await complete(c)
        route = await c.get("/v1/coding/route", params={"session": "s", "privacy": "any"})
    assert env.fakes["a"].requests == []
    assert env.local.starts and LOCAL_ANSWER in resp.content
    assert completion.status_code == 500
    assert route.json() == {"route": None}


@pytest.mark.anyio
async def test_the_default_is_off(env, monkeypatch):
    """No CODING_MEMBERS_ENABLED key at all (a limits dict that predates it) is off."""
    env.add(env.fakes["a"])
    del env.limits["coding_members_enabled"]
    monkeypatch.setattr(coordinator, "coding_router", Exploding())
    async with client() as c:
        await chat(c)
    assert env.fakes["a"].requests == [] and env.local.starts


# ======================================================================================
# GET /v1/coding/route and sealed requests
# ======================================================================================

async def lookup(c, session="agent-1", privacy="any"):
    params = {"session": session}
    if privacy is not None:
        params["privacy"] = privacy
    resp = await c.get("/v1/coding/route", params=params)
    assert resp.status_code == 200
    return resp.json()


@pytest.mark.anyio
async def test_a_route_names_a_member_by_an_opaque_id_and_its_key(env):
    wid = env.add(env.fakes["a"])
    async with client() as c:
        route = await lookup(c)
    assert set(route) == {"route", "pubkey", "alg"}
    assert route["pubkey"] == env.fakes["a"].pubkey and route["alg"] == E2E_ALG == "x25519-hkdf-sha256-chacha20poly1305"
    assert len(route["route"]) >= 20 and wid not in route["route"] and "member" not in route["route"]


@pytest.mark.anyio
async def test_no_route_means_the_local_engine(env):
    async with client() as c:
        assert await lookup(c) == {"route": None}                            # no members at all
        env.add(env.fakes["a"])
        assert await lookup(c, privacy="trusted") == {"route": None}         # none trusted
        assert await lookup(c, privacy="whatever") == {"route": None}        # unknown: trusted
        assert (await lookup(c, privacy="any"))["route"]


@pytest.mark.anyio
async def test_the_requesters_own_member_is_preferred_while_it_is_as_fast_as_the_engine(env, monkeypatch):
    """``own=<trust id>`` (2.5.0): the requester's own machine serves them unless this coordinator's engine would
    write faster for them right now (its measured rate shared among the requests it is answering)."""
    from engine import coding_speed
    mine, other = env.fakes["a"], env.fakes["b"]
    env.add(mine, "mine", slots=4, decode=25.0, prefill=121.0)
    env.add(other, "other", slots=4, decode=40.0, prefill=300.0)
    monkeypatch.setattr(coding_speed, "meter", SimpleNamespace(speed=lambda file: (None, None)))

    async def chosen(c, session, own):
        params = {"session": session, "privacy": "any"}
        if own is not None:
            params["own"] = own
        route = (await c.get("/v1/coding/route", params=params)).json()
        env.clock.t += 31                       # the route's hold on the member runs out: a clean slate
        return member_name(env, route["pubkey"])

    async with client() as c:
        assert await chosen(c, "s1", None) == "b", "nothing said: the faster reader"
        assert await chosen(c, "s2", mine.trust_id) == "a", "no engine speed measured yet: own"
        monkeypatch.setattr(coding_speed, "meter", SimpleNamespace(speed=lambda file: (41.5, "2026-10-27T13:00:00Z")))
        assert await chosen(c, "s3", mine.trust_id) == "b", "the engine writes 41.5 for one user: not own"
        monkeypatch.setattr(coordinator.coding_gate, "stats", lambda: {"slots": 5, "in_use": 2, "waiting": 0})
        assert await chosen(c, "s4", mine.trust_id) == "a", "shared by the two requests it is answering, 20.75: own"
        assert await chosen(c, "s4", None) == "a", "and own is the session's sticky member from then on"
        for odd in (mine.trust_id.upper(), mine.trust_id[:31], "x" * 32, "", other.trust_id):
            assert await chosen(c, f"s-{odd[:8]}", odd) == "b", odd


@pytest.mark.anyio
async def test_prefer_smarter_sends_the_requesters_own_requests_to_their_smarter_model(env, monkeypatch):
    """``prefer=smarter`` (dlne.ownGpu): the owner's laptop on IQ4_XS answers them although this coordinator's engine,
    on IQ3_S, writes faster; only while the laptop's model is further down the ladder, and never past privacy."""
    from engine import coding_speed
    smarter = "Qwen3.6-35B-A3B-UD-IQ4_XS.gguf"
    monkeypatch.setattr(coordinator, "_coding_tiers", [dict(TIER), {**TIER, "id": "qwen36-a3b-iq4xs", "file": smarter,
                                                                    "min_vram_gb": 24, "files": [{"file": smarter}]}])
    monkeypatch.setattr(coordinator, "global_active_model", MODEL_FILE)
    monkeypatch.setattr(coding_speed, "meter", SimpleNamespace(speed=lambda file: (65.0, "2026-10-30T09:00:00Z")))
    mine, other = env.fakes["a"], env.fakes["b"]
    env.add(mine, "mine", slots=4, decode=27.0, prefill=121.0, model=smarter)
    env.add(other, "other", slots=4, decode=40.0, prefill=300.0)
    count = {"n": 0}

    async def chosen(c, privacy="any", **params):
        count["n"] += 1                         # a new session each time: no sticky member
        reply = (await c.get("/v1/coding/route", params={"session": f"s{count['n']}", "privacy": privacy, **params})).json()
        env.clock.t += 31
        return member_name(env, reply["pubkey"]) if reply["route"] else None

    async with client() as c:
        assert await chosen(c, own=mine.trust_id) == "b", "faster (the default): the engine writes 65, the laptop 27"
        assert await chosen(c, own=mine.trust_id, prefer="smarter") == "a", "smarter: the laptop's IQ4_XS"
        assert await chosen(c, prefer="smarter") == "b", "prefer without own means nothing"
        for odd in ("Smarter", "smarter ", "1", "", "faster"):
            assert await chosen(c, own=mine.trust_id, prefer=odd) == "b", odd
        # The private repository: only trusted members, and the laptop is not (yet) one of them.
        env.limits["trusted_members"] = [other.trust_id]
        assert await chosen(c, "trusted", own=mine.trust_id, prefer="smarter") == "b"
        env.limits["trusted_members"] = []
        assert await chosen(c, "trusted", own=mine.trust_id, prefer="smarter") is None, "this engine answers"
        env.limits["trusted_members"] = [mine.trust_id]
        assert await chosen(c, "trusted", own=mine.trust_id, prefer="smarter") == "a", "trusted: the laptop may have it"
        # An automatic switch moved this coordinator's engine up to IQ4_XS too: the laptop is no smarter now.
        monkeypatch.setattr(coordinator, "global_active_model", smarter)
        assert await chosen(c, own=mine.trust_id, prefer="smarter") == "b"


@pytest.mark.anyio
async def test_need_offers_only_a_member_whose_context_holds_the_request(env, monkeypatch):
    """``need=<tokens>`` ("context is everything", 2026-10-30): the tokens the request will hold, prompt and answer.
    Only a member whose context per request (its tier's context over its server_slots) holds them is offered; with
    none, this coordinator's engine answers. Anything but a whole number from 1 to 10,000,000 is ignored."""
    smarter = "Qwen3.6-35B-A3B-UD-IQ4_XS.gguf"
    monkeypatch.setattr(coordinator, "_coding_tiers", [
        {**TIER, "context": 81920, "server_slots": 2},                                       # 40,960 a request
        {**TIER, "id": "qwen36-a3b-iq4xs", "file": smarter, "min_vram_gb": 24, "context": 262144, "server_slots": 2,
         "files": [{"file": smarter}]}])                                                     # 131,072 a request
    env.add(env.fakes["a"], "small", slots=4, prefill=300.0)
    env.add(env.fakes["b"], "big", slots=4, prefill=100.0, model=smarter, tier="qwen36-a3b-iq4xs")
    count = {"n": 0}

    async def chosen(c, session=None, **params):
        count["n"] += 1
        reply = (await c.get("/v1/coding/route", params={"session": session or f"s{count['n']}", "privacy": "any",
                                                          **params})).json()
        env.clock.t += 31                       # the route's hold on the member runs out: a clean slate
        return member_name(env, reply["pubkey"]) if reply["route"] else None

    async with client() as c:
        assert await chosen(c) == "a", "no need: the faster reader, as before"
        assert await chosen(c, need="40960") == "a", "exactly its context fits"
        assert await chosen(c, need="40961") == "b"
        assert await chosen(c, need="131072") == "b"
        assert await chosen(c, need="131073") is None, "no member holds it: this engine answers"
        for odd in ("0", "-65536", "10000001", "000065536", "65536.0", "1e5", " 65536", "+65536", "0x10000", "abc", ""):
            assert await chosen(c, need=odd) == "a", odd
        # A session whose request outgrew its member moves to one that holds it, and stays there (its prompt is
        # cached there now) while it fits.
        assert await chosen(c, "agent", need="8192") == "a"
        assert await chosen(c, "agent", need="65536") == "b"
        assert await chosen(c, "agent", need="8192") == "b"
        # The requester's own member is passed over too when it cannot hold the request.
        mine = env.fakes["a"].trust_id
        assert await chosen(c, own=mine, need="8192") == "a"
        assert await chosen(c, own=mine, need="65536") == "b"


@pytest.mark.anyio
async def test_a_member_with_no_key_is_offered_no_route_and_gets_nothing_in_the_clear(env):
    env.add(env.fakes["a"], pubkey=False)
    async with client() as c:
        assert await lookup(c) == {"route": None}
        resp = await chat(c)
    assert env.fakes["a"].requests == [] and LOCAL_ANSWER in resp.content


@pytest.mark.anyio
async def test_the_key_handed_out_is_the_members_latest(env):
    """Members rotate their key every 6 hours; the lookup reads the newest heartbeat."""
    fake = env.fakes["a"]
    env.add(fake, "wa")
    async with client() as c:
        before = await lookup(c)
        old_key = fake.pubkey
        fake.keyring.rotate()
        env.add(fake, "wa")                                        # the heartbeat that carries the new key
        after = await lookup(c, session="agent-2")
        assert before["pubkey"] == old_key != after["pubkey"] == fake.pubkey
        # A route issued just before the rotation still opens: the member keeps the old key for 15 minutes.
        _raw, resp_key, resp = await sealed(env, c, before, {"messages": [{"role": "user", "content": "x"}]})
        assert resp.status_code == 200 and open_sealed_stream(resp.content, resp_key) == ["a-one ", "a-two"]


@pytest.mark.anyio
async def test_a_route_is_bound_to_the_key_it_was_issued_with(env, monkeypatch):
    """A member keeps a retired key 15 minutes. A route older than that, whose member has since changed key,
    could no longer be opened: it is refused with the re-route code instead of being forwarded."""
    monkeypatch.setattr(cr_module, "ROUTE_TTL_SECONDS", 3600)          # so that a route can outlive the overlap
    fake = env.fakes["a"]
    env.add(fake, "wa")
    async with client() as c:
        route = await lookup(c)
        steady = await lookup(c, session="agent-2")                    # same key, never rotated away from
        fake.keyring.rotate()
        env.add(fake, "wa")
        env.clock.t += 14 * 60
        _raw, key, resp = await sealed(env, c, route, {"messages": [{"role": "user", "content": "x"}]})
        assert resp.status_code == 200 and open_sealed_stream(resp.content, key) == ["a-one ", "a-two"]
        env.clock.t += 2 * 60
        _raw, _key, late = await sealed(env, c, route, {"messages": []})
        assert_reroute(late, "expired")
        fresh = await lookup(c, session="agent-3")
        assert fresh["pubkey"] == fake.pubkey != route["pubkey"]
        _raw, key, resp = await sealed(env, c, fresh, {"messages": [{"role": "user", "content": "y"}]})
        assert resp.status_code == 200 and open_sealed_stream(resp.content, key) == ["a-one ", "a-two"]
    assert len(fake.requests) == 2, "the refused body was never forwarded"
    assert steady["pubkey"] == route["pubkey"]


@pytest.mark.anyio
async def test_a_route_whose_member_kept_its_key_is_good_for_its_whole_life(env, monkeypatch):
    monkeypatch.setattr(cr_module, "ROUTE_TTL_SECONDS", 3600)
    env.add(env.fakes["a"], "wa")
    async with client() as c:
        route = await lookup(c)
        env.clock.t += 40 * 60
        _raw, key, resp = await sealed(env, c, route, {"messages": [{"role": "user", "content": "x"}]})
    assert resp.status_code == 200 and open_sealed_stream(resp.content, key) == ["a-one ", "a-two"]


@pytest.mark.anyio
async def test_a_lookup_follows_the_sticky_session(env):
    env.add(env.fakes["a"], "wa", prefill=200.0, slots=4)
    env.add(env.fakes["b"], "wb", prefill=100.0)
    async with client() as c:
        first = await lookup(c, "agent-1")
        env.add(env.fakes["a"], "wa", prefill=200.0, busy=1, slots=4)       # busier than b, but not full
        again = await lookup(c, "agent-1")
        other = await lookup(c, "agent-2")
    assert first["pubkey"] == again["pubkey"] == env.fakes["a"].pubkey
    assert other["pubkey"] == env.fakes["b"].pubkey


@pytest.mark.anyio
async def test_routes_in_a_burst_are_spread_over_the_members_slots(env):
    """A route counts as load until it is used: 12 agents asking at once do not all get one member."""
    env.add(env.fakes["a"], "wa", slots=2)
    env.add(env.fakes["b"], "wb", slots=1)
    async with client() as c:
        keys = [(await lookup(c, f"agent-{i}")).get("pubkey") for i in range(5)]
    assert keys.count(env.fakes["a"].pubkey) == 2 and keys.count(env.fakes["b"].pubkey) == 1
    assert keys[3:] == [None, None]


@pytest.mark.anyio
async def test_a_held_route_is_given_back_after_thirty_seconds(env):
    env.add(env.fakes["a"], "wa", slots=1)
    async with client() as c:
        assert (await lookup(c, "s1"))["route"]
        assert (await lookup(c, "s2"))["route"] is None
        env.clock.t += 31
        assert (await lookup(c, "s3"))["route"]


@pytest.mark.anyio
async def test_route_lookups_are_never_charged_to_the_free_trial(env, tiers):
    env.add(env.fakes["a"])
    before = coordinator.starter_quota_mgr.peek(REMOTE[0], limit=200)
    async with client() as c:
        for i in range(coordinator.TRIAL_ROUTE_LOOKUPS):
            await lookup(c, f"s{i}")
    assert coordinator.starter_quota_mgr.peek(REMOTE[0], limit=200) == before


@pytest.mark.anyio
async def test_a_free_trial_caller_holds_no_member_slots_with_route_lookups(env, tiers):
    """SR-09: anyone may ask for a route, so a stranger's lookups must not keep agents' slots from them."""
    env.add(env.fakes["a"], "wa", slots=1)
    async with client() as c:
        for i in range(coordinator.TRIAL_ROUTE_LOOKUPS):
            assert (await lookup(c, f"stranger-{i}"))["route"], "no hold: the single slot is still free"
        assert env.router.load(env.router.members(env.registry, lambda ip, port: 1, env.limits)[0]) == 0
        # An operator key holder's lookup does hold the slot (the burst-spreading behaviour), and so
        # the next agent is told there is no room instead of being sent to a full member.
        key = {"Authorization": "Bearer dlne_sk_good"}
        async with client(headers=key) as keyed:
            assert (await lookup(keyed, "agent-1"))["route"]
            assert (await lookup(keyed, "agent-2")) == {"route": None}


@pytest.mark.anyio
async def test_a_free_trial_caller_may_only_look_up_a_few_routes_in_a_while(env, tiers):
    env.add(env.fakes["a"], "wa", slots=64)
    limit = coordinator.TRIAL_ROUTE_LOOKUPS
    async with client() as c, client(ip=("203.0.113.77", 40001)) as other, \
            client(headers={"Authorization": "Bearer dlne_sk_good"}) as keyed:
        for i in range(limit):
            assert (await c.get("/v1/coding/route", params={"session": f"s{i}", "privacy": "any"})).status_code == 200
        refused = await c.get("/v1/coding/route", params={"session": "one-too-many", "privacy": "any"})
        assert refused.status_code == 429 and 1 <= int(refused.headers["retry-after"]) <= 30
        assert refused.json() == {"detail": "Too many route lookups. Try again shortly."}
        assert (await lookup(other, "a-different-client"))["route"], "the limit is per client"
        for i in range(limit * 3):
            assert (await lookup(keyed, f"agent-{i}"))["route"], "and only for the free trial"
    assert len(env.router._routes) == limit + 1 + limit * 3, "the refused lookup made no route"


@pytest.mark.anyio
async def test_a_route_lookup_is_refused_once_the_trial_is_spent(env, tiers):
    env.add(env.fakes["a"])
    for _ in range(200):
        coordinator.starter_quota_mgr.check_and_consume(REMOTE[0], limit=200)
    async with client() as c:
        resp = await c.get("/v1/coding/route", params={"privacy": "any"})
    assert resp.status_code == 403


async def sealed(env, c, route, body, headers=None, name="a"):
    envelope, resp_key = seal_request(body, route["pubkey"])
    raw = json.dumps(envelope).encode()
    resp = await c.post("/v1/chat/completions", content=raw,
                        headers={**JSON_HEADERS, "X-DLNE-Route": route["route"], **(headers or {})})
    return raw, resp_key, resp


@pytest.mark.anyio
async def test_a_sealed_request_is_carried_to_its_member_untouched_and_never_read(env):
    fake = env.fakes["a"]
    env.add(fake)
    prompt = {"messages": [{"role": "user", "content": "SECRET-PROMPT-TEXT"}], "stream": True, "user": "alice"}
    async with client() as c:
        route = await lookup(c)
        raw, resp_key, resp = await sealed(env, c, route, prompt)
    assert resp.status_code == 200
    seen = fake.requests[0]
    assert seen.raw == raw, "the member got exactly the bytes the client sealed"
    assert set(seen.headers) <= {"host", "accept", "accept-encoding", "connection", "content-type",
                                 "content-length", "user-agent"}
    assert fake.opened == [prompt], "it opens, with its own key, exactly what the client sealed (user and all)"
    assert b"SECRET-PROMPT-TEXT" not in seen.raw and b"SECRET-PROMPT-TEXT" not in resp.content
    assert open_sealed_stream(resp.content, resp_key) == ["a-one ", "a-two"], "and the answer opens at the client"
    assert env.local.starts == [] and env.router.in_flight() == 0


@pytest.mark.anyio
async def test_a_sealed_body_is_used_once_and_a_second_send_is_not_covered_for(env):
    """The member refuses an envelope it has seen; the coordinator neither retries it elsewhere nor falls back."""
    env.add(env.fakes["a"], "wa", prefill=200.0)
    env.add(env.fakes["b"], "wb", prefill=100.0)
    async with client() as c:
        route = await lookup(c)
        raw, _key, first = await sealed(env, c, route, {"messages": [{"role": "user", "content": "x"}]})
        assert first.status_code == 200
        again = await c.post("/v1/chat/completions", content=raw,
                             headers={**JSON_HEADERS, "X-DLNE-Route": route["route"]})
    assert_reroute(again, "failed", "replay")
    assert len(env.fakes["a"].requests) == 2 and env.fakes["b"].requests == [] and env.local.starts == []


@pytest.mark.anyio
async def test_a_whole_sealed_answer_is_carried_too(env):
    fake = env.fakes["a"]
    env.add(fake)
    fake.mode = "json"
    async with client() as c:
        route = await lookup(c)
        _raw, resp_key, resp = await sealed(env, c, route, {"messages": [{"role": "user", "content": "hi"}]})
    answer = open_sealed_json(resp.json(), resp_key)
    assert answer["choices"][0]["message"]["content"] == "a-whole"


@pytest.mark.anyio
async def test_a_sealed_completion_is_carried_to_the_members_completion_endpoint(env):
    fake = env.fakes["a"]
    env.add(fake)
    async with client() as c:
        route = await lookup(c)
        envelope, _resp_key = seal_request({"prompt": "def f("}, route["pubkey"])
        resp = await c.post("/v1/completions", content=json.dumps(envelope),
                            headers={**JSON_HEADERS, "X-DLNE-Route": route["route"]})
    assert resp.status_code == 200 and fake.requests[0].path == "/v1/completions"


def assert_reroute(resp, reason, *private):
    """The one answer a sealed request gets when it cannot be carried out: unsealed, and bare."""
    assert resp.status_code == 503
    error = resp.json()["error"]
    assert error["code"] == "dlne_reroute" and error["reason"] == reason
    assert set(resp.json()) == {"error"} and set(error) == {"code", "message", "reason"}
    for needle in (REMOTE[0], "127.0.0.1", "10.8.0.", "member-", "worker", "SECRET", "x-dlne", "trust") + private:
        assert needle.lower() not in resp.text.lower(), needle
    assert "x-dlne-served" not in resp.headers


@pytest.mark.anyio
async def test_a_sealed_body_without_a_route_is_refused_plainly(env):
    env.add(env.fakes["a"])
    envelope, _ = seal_request({"messages": []}, env.fakes["a"].pubkey)
    async with client() as c:
        resp = await c.post("/v1/chat/completions", content=json.dumps(envelope), headers=ANY)
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "dlne_route_required"
    assert env.fakes["a"].requests == [] and env.local.starts == [], "it can never be read by the local engine"


@pytest.mark.anyio
async def test_an_unknown_route_must_be_asked_for_again(env):
    env.add(env.fakes["a"])
    envelope, _ = seal_request({"messages": []}, env.fakes["a"].pubkey)
    async with client() as c:
        resp = await c.post("/v1/chat/completions", content=json.dumps(envelope),
                            headers={**ANY, "X-DLNE-Route": "not-a-route"})
    assert_reroute(resp, "expired")
    assert env.fakes["a"].requests == [] and env.local.starts == []


@pytest.mark.anyio
async def test_a_route_older_than_ten_minutes_is_refused_with_the_reroute_code(env):
    env.add(env.fakes["a"])
    async with client() as c:
        route = await lookup(c)
        env.clock.t += 9 * 60
        _raw, _key, resp = await sealed(env, c, route, {"messages": []})
        assert resp.status_code == 200, "still good at nine minutes"
        env.clock.t += 2 * 60
        _raw, _key, resp = await sealed(env, c, route, {"messages": []})
    assert_reroute(resp, "expired")
    assert len(env.fakes["a"].requests) == 1


@pytest.mark.anyio
async def test_a_route_whose_member_left_must_be_asked_for_again(env):
    wid = env.add(env.fakes["a"])
    env.add(env.fakes["b"], "wb")
    async with client() as c:
        route = await lookup(c)
        env.registry.workers.pop(wid)
        _raw, _key, resp = await sealed(env, c, route, {"messages": []})
    assert_reroute(resp, "gone")
    assert env.fakes["a"].requests == [] and env.fakes["b"].requests == [], "never sent to another member"


@pytest.mark.anyio
async def test_a_route_whose_member_is_full_must_be_asked_for_again(env):
    env.add(env.fakes["a"], "wa", slots=1)
    env.add(env.fakes["b"], "wb", slots=1)
    async with client() as c:
        route = await lookup(c)
        env.add(env.fakes["a"], "wa", slots=1, busy=1)
        _raw, _key, resp = await sealed(env, c, route, {"messages": []})
    assert_reroute(resp, "busy")
    assert resp.headers["retry-after"] == "3"
    assert env.fakes["a"].requests == [] and env.fakes["b"].requests == []


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["500", "400", "503", "die_before_first_chunk"])
async def test_a_failing_member_is_never_covered_for_by_another_or_by_the_local_engine(env, mode):
    env.add(env.fakes["a"], "wa", prefill=200.0)
    env.add(env.fakes["b"], "wb", prefill=100.0)
    env.fakes["a"].mode = mode
    async with client() as c:
        route = await lookup(c)
        assert route["pubkey"] == env.fakes["a"].pubkey
        _raw, _key, resp = await sealed(env, c, route, {"messages": []})
    assert_reroute(resp, "busy" if mode == "503" else "failed", "member says", "SECRET")
    assert len(env.fakes["a"].requests) == 1, "the sealed body was sent once, to its member"
    assert env.fakes["b"].requests == [] and env.local.starts == []
    assert env.router.in_flight() == 0


@pytest.mark.anyio
async def test_a_route_issued_under_any_is_refused_when_the_request_says_trusted(env):
    env.add(env.fakes["a"])
    async with client() as c:
        route = await lookup(c, privacy="any")
        _raw, _key, ok = await sealed(env, c, route, {"messages": []})
        assert ok.status_code == 200
        _raw, _key, resp = await sealed(env, c, route, {"messages": []}, {"X-DLNE-Privacy": "trusted"})
    assert_reroute(resp, "privacy")
    assert len(env.fakes["a"].requests) == 1


@pytest.mark.anyio
async def test_a_route_is_judged_by_the_mode_it_was_issued_under_when_the_request_says_nothing(env):
    env.limits["trusted_members"] = [env.fakes["a"].trust_id]
    env.add(env.fakes["a"], "wa")
    async with client() as c:
        route = await lookup(c, privacy="trusted")
        _raw, _key, resp = await sealed(env, c, route, {"messages": []})
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_a_plain_body_with_a_dead_route_goes_to_the_local_engine(env):
    env.add(env.fakes["a"])
    async with client() as c:
        resp = await chat(c, headers={**ANY, "X-DLNE-Route": "stale"})
    assert env.fakes["a"].requests == [] and LOCAL_ANSWER in resp.content


# ======================================================================================
# The heartbeat answer: what a worker should be
# ======================================================================================

def _heartbeat(worker_id, hardware, ip, port=50053):
    from tests.test_coordinator_counter import FakeRequest
    payload = coordinator.HeartbeatPayload(worker_id=worker_id, version=VERSION, port=port,
                                           token=f"tok-{worker_id}", hardware=hardware)
    return asyncio.run(coordinator.worker_heartbeat(FakeRequest(client_host=ip), payload))


def gpu_hw(vram=8.0, ram=32.0):
    return {"gpu": {"has_gpu": True, "vram_total_gb": vram, "name": "RTX 4070"}, "ram_gb": ram, "cpu_cores": 8,
            "cpu_threads": 16, "is_yielding": False}


CPU_HW = {"gpu": {"has_gpu": False, "vram_total_gb": 0}, "ram_gb": 12.0, "cpu_cores": 6, "cpu_threads": 12,
          "cpu_arch": "x86_64", "cpu_features": ["avx2", "fma", "f16c"], "mem_bandwidth_gbs": 48.0,
          "is_yielding": False}


def _aged(env, worker_id, seconds):
    """Pretend ``seconds`` have passed since the coordinator first saw and registered ``worker_id``."""
    env.router._first_seen[worker_id] = time.time() - seconds
    if worker_id in env.registry.workers:
        env.registry.workers[worker_id]["registered_at"] = time.time() - seconds


def measured(env, worker_id, ip, rtt_ms, hardware=None):
    """A worker the coordinator has heard from and measured itself (a probe is only kept for a registered node)."""
    env.registry.update_worker(worker_id, ip, 50053, f"tok-{worker_id}", hardware or gpu_hw())
    env.registry.record_probe(worker_id, rtt_ms, "direct")


def test_a_gpu_close_to_the_coordinator_is_an_rpc_worker(env):
    measured(env, "near", "10.0.0.41", 3.0, gpu_hw(24.0))
    assert _heartbeat("near", gpu_hw(24.0), "10.0.0.41") == {"status": "ok", "role": "rpc", "coding_tier": None}


def test_a_far_gpu_with_8_gb_is_a_coding_member_with_the_tier_it_can_hold(env):
    measured(env, "far", "10.0.0.42", 80.0)
    answer = _heartbeat("far", gpu_hw(8.0, 32.0), "10.0.0.42")
    assert answer["status"] == "ok" and answer["role"] == "coding"
    assert answer["coding_tier"]["id"] == TIER["id"] and answer["coding_tier"]["file"] == MODEL_FILE
    assert answer["coding_slots"] in (1, 2), "how many requests at once to open the tier for (2.5.0)"


def test_the_slot_count_comes_from_choose_slots_and_only_comes_with_a_tier(env, monkeypatch):
    from engine import coding_member_select
    seen = []

    def slots(tier, vram, ram):
        seen.append((tier["id"], vram, ram))
        return 1

    monkeypatch.setattr(coding_member_select, "choose_slots", slots)
    measured(env, "far", "10.0.0.42", 80.0)
    answer = _heartbeat("far", gpu_hw(8.0, 32.0), "10.0.0.42")
    assert answer["coding_slots"] == 1 and seen == [(TIER["id"], 8.0, 32.0)], "sized on the memory the tier was"
    measured(env, "near", "10.0.0.41", 3.0, gpu_hw(24.0))
    assert "coding_slots" not in _heartbeat("near", gpu_hw(24.0), "10.0.0.41"), "no tier, no slot count"
    assert "coding_slots" not in _heartbeat("cpu", CPU_HW, "10.0.0.49")


def test_the_tier_comes_from_choose_tier(env, monkeypatch):
    from engine import coding_member_select
    seen = []

    def choose(vram, ram, tiers):
        seen.append((vram, ram, [t["id"] for t in tiers]))
        return tiers[0]

    monkeypatch.setattr(coding_member_select, "choose_tier", choose)
    measured(env, "far", "10.0.0.42", 80.0)
    assert _heartbeat("far", gpu_hw(12.0, 20.0), "10.0.0.42")["coding_tier"]["id"] == TIER["id"]
    assert seen == [(12.0, 20.0, [TIER["id"]])]


@pytest.mark.parametrize("vram, ram", [(6.0, 64.0), (8.0, 8.0)])
def test_a_far_gpu_too_small_for_any_tier_is_given_no_role(env, vram, ram):
    measured(env, "small", "10.0.0.43", 80.0)
    assert _heartbeat("small", gpu_hw(vram, ram), "10.0.0.43") == {"status": "ok", "role": None, "coding_tier": None}


def test_a_gpu_nobody_has_measured_waits_before_it_is_called_far(env):
    first = _heartbeat("new", gpu_hw(), "10.0.0.44")
    assert first["role"] is None, "a LAN machine must not be sent away in the gap before the probe"
    _aged(env, "new", 600)
    assert _heartbeat("new", gpu_hw(), "10.0.0.44")["role"] == "coding"


def test_a_member_that_already_serves_is_not_made_to_wait_after_a_coordinator_restart(env):
    """A restart forgets when every worker was first seen and every measurement: a worker whose heartbeat
    already says it is a coding member keeps its role, instead of being told 'no assignment' and unloading."""
    key, identity, _trust = make_identity()
    block = sign_block({"model": MODEL_FILE, "tier": "qwen36-a3b", "ready": True, "slots": 2, "busy": 0,
                        "decode_tps": 33.0, "prefill_tps": 121.0, "pubkey": generate_keypair()[1],
                        "no_trace": True, "identity": identity}, key)
    serving = {**gpu_hw(8.0, 32.0), "pool": "coding", "coding": block}
    newcomer = gpu_hw(8.0, 32.0)
    assert env.router._first_seen == {}, "a fresh coordinator: nobody has been seen, nobody is measured"
    answer = _heartbeat("serving", serving, "10.0.0.60")
    assert answer["role"] == "coding" and answer["coding_tier"]["id"] == TIER["id"]
    assert _heartbeat("newcomer", newcomer, "10.0.0.61")["role"] is None, "an unknown machine still waits"
    assert "serving" not in env.router._first_seen, "no grace clock was even started for the member"


def test_a_measurement_that_arrives_ends_the_wait(env):
    assert _heartbeat("new", gpu_hw(), "10.0.0.45")["role"] is None
    env.registry.record_probe("new", 2.0, "direct")                     # registered by the heartbeat above
    assert _heartbeat("new", gpu_hw(), "10.0.0.45")["role"] == "rpc"


def test_a_stale_measurement_moves_nobody_but_counts_as_far_in_the_pool(env):
    """2.6.2: an aged reading says nothing about distance now (a tunnel busy carrying a shard is never pinged), so it
    changes no role ("stay as you are"); only a fresh reading over the limit does. Auto-swap still leaves it out."""
    measured(env, "old", "10.0.0.46", 2.0)
    env.registry.link_probes["old"]["measured_at"] = time.time() - 3600
    assert _heartbeat("old", gpu_hw(), "10.0.0.46")["role"] is None
    assert coordinator._coding_pool()["vram_gb"] == 0.0
    measured(env, "old", "10.0.0.46", 60.0)                              # fresh, and over this env's 15 ms
    assert _heartbeat("old", gpu_hw(), "10.0.0.46")["role"] == "coding"


def _engine_runs_on(monkeypatch, *endpoints):
    monkeypatch.setattr(type(coordinator.manager), "current_endpoints",
                        property(lambda self: ["127.0.0.1:50052", *endpoints]), raising=False)


def test_a_shard_whose_reading_aged_out_under_load_stays_a_shard_and_stays_in_the_pool(env, monkeypatch):
    """2026-10-28 06:46 and 10:11: the tunnel carrying the laptop's shard was too busy to ping for 15 minutes, its
    reading aged out, it was told to become a coding member and stopped its RPC server under the engine."""
    _engine_runs_on(monkeypatch, "10.0.0.82:50053")
    measured(env, "shard", "10.0.0.82", 2.0, gpu_hw(8.0, 32.0))
    env.registry.link_probes["shard"]["measured_at"] = time.time() - 3600
    assert _heartbeat("shard", gpu_hw(8.0, 32.0), "10.0.0.82")["role"] == "rpc"
    assert coordinator._coding_pool()["vram_gb"] == 8.0, "auto-swap does not see the pool shrink either"

    # Between two engines (every rebuild empties the engine's list for a few seconds; found in review): an old reading
    # moves nobody, so the next engine can still be planned onto it. It only stops counting in auto-swap's pool.
    _engine_runs_on(monkeypatch)
    assert _heartbeat("shard", gpu_hw(8.0, 32.0), "10.0.0.82")["role"] is None, "stay as you are"
    assert coordinator._coding_pool()["vram_gb"] == 0.0


def test_a_shard_the_engine_runs_on_with_no_reading_yet_is_near(env, monkeypatch):
    """A coordinator that has just restarted has no reading, and its engine may be on the GPU before the probe can
    ping the busy tunnel: no reading is not "far" for a GPU the engine is running on (found in review)."""
    _engine_runs_on(monkeypatch, "10.0.0.85:50053")
    env.registry.update_worker("fresh", "10.0.0.85", 50053, "tok-fresh", gpu_hw(8.0, 32.0))
    assert coordinator._coding_pool()["vram_gb"] == 8.0
    assert _heartbeat("fresh", gpu_hw(8.0, 32.0), "10.0.0.85")["role"] == "rpc"


def test_a_shard_measured_far_keeps_its_role_until_the_engine_has_been_moved_off_it(env, monkeypatch):
    _engine_runs_on(monkeypatch, "10.0.0.83:50053")
    measured(env, "shard", "10.0.0.83", 60.0, gpu_hw(8.0, 32.0))       # fresh, and over this env's 15 ms
    assert _heartbeat("shard", gpu_hw(8.0, 32.0), "10.0.0.83")["role"] == "rpc", "a role change would stop the engine"
    assert coordinator._coding_pool()["vram_gb"] == 0.0, "but auto-swap's pool leaves it out, so the engine moves first"


@pytest.mark.parametrize("aside, after", [({"docked": False}, "coding"), ({"is_yielding": True}, None),
                                          ({"leaving": True}, "rpc")])
def test_a_shard_stepping_aside_drains_before_it_is_given_another_role(env, monkeypatch, aside, after):
    _engine_runs_on(monkeypatch, "10.0.0.84:50053")
    measured(env, "shard", "10.0.0.84", 2.0, gpu_hw(8.0, 32.0))
    answer = _heartbeat("shard", {**gpu_hw(8.0, 32.0), **aside}, "10.0.0.84")
    assert answer["draining"] is True and answer["role"] == "rpc"
    _engine_runs_on(monkeypatch)                      # rebuilt without it: now it is told what to be next
    assert _heartbeat("shard", {**gpu_hw(8.0, 32.0), **aside}, "10.0.0.84")["role"] == after


def test_the_rtt_limit_is_auto_swaps(env):
    env.limits["auto_swap_max_rtt_ms"] = 100.0
    measured(env, "mid", "10.0.0.47", 80.0)
    assert _heartbeat("mid", gpu_hw(), "10.0.0.47")["role"] == "rpc"


def test_a_worker_that_only_says_its_link_is_short_is_not_near(env):
    """/worker/link_report is the worker's own story: it never makes a GPU near enough to share the model."""
    _heartbeat("boast", gpu_hw(), "10.0.0.50")
    env.registry.record_link_measurement("boast", {"rtt_min_ms": 0.3})
    _aged(env, "boast", 600)                 # past the grace
    assert _heartbeat("boast", gpu_hw(), "10.0.0.50")["role"] == "coding"


@pytest.mark.parametrize("rtt, role", [(100.0, "coding"), (120.0, "coding"), (121.0, None), (400.0, None)])
def test_a_far_gpu_runs_a_coding_model_of_its_own_only_within_the_member_limit(env, rtt, role):
    measured(env, "away", "10.0.0.51", rtt)
    assert _heartbeat("away", gpu_hw(), "10.0.0.51")["role"] == role


def test_the_member_limit_is_a_setting(env):
    env.limits["coding_member_max_rtt_ms"] = 200.0
    measured(env, "away", "10.0.0.52", 150.0)
    assert _heartbeat("away", gpu_hw(), "10.0.0.52")["role"] == "coding"


def test_the_coding_model_is_sized_on_the_coding_ram_not_the_rpc_ram_cap(env):
    """ram_gb is what the RPC node lends (MAX_RAM_GB, 4 GB by default): 8 GB of VRAM and 4 GB fit no model.
    coding_ram_gb is what the coding job may use, and it is the figure a model is chosen for."""
    measured(env, "laptop", "10.0.0.53", 90.0)
    assert _heartbeat("laptop", gpu_hw(8.0, 4.0), "10.0.0.53")["role"] is None
    answer = _heartbeat("laptop", {**gpu_hw(8.0, 4.0), "coding_ram_gb": 30.0}, "10.0.0.53")
    assert answer["role"] == "coding" and answer["coding_tier"]["id"] == TIER["id"]


def test_a_gpu_in_this_household_is_asked_for_its_lan_addresses(env):
    answer = _heartbeat("home", gpu_hw(), HOUSEHOLD_IP, port=50052)
    assert answer["send_lan"] is True
    _heartbeat("home", {**gpu_hw(), "lan_ipv4": ["172.17.0.1", "192.168.40.50", "10.13.13.2", "192.168.122.1"]},
               HOUSEHOLD_IP, port=50052)
    # Only the address on one of this machine's own subnets is kept (not docker, WireGuard or libvirt).
    assert link_paths.lan_routes.offers(HOUSEHOLD_IP, 50052) == ["192.168.40.50"]


def test_a_household_gpu_is_given_the_coordinators_lan_address_for_its_tunnel(env):
    """2.5.2: a host firewall drops the coordinator's dial, but the worker's own connection out gets through."""
    assert "lan_tunnel" not in _heartbeat("home", gpu_hw(), HOUSEHOLD_IP, port=50052), "no LAN address of its own yet"
    answer = _heartbeat("home", {**gpu_hw(), "lan_ipv4": ["172.17.0.1", "192.168.40.50"]}, HOUSEHOLD_IP, port=50052)
    assert answer["lan_tunnel"] == "ws://192.168.40.23:11435"
    stranger = _heartbeat("stranger", {**gpu_hw(), "lan_ipv4": ["192.168.40.50"]}, "203.0.113.77", port=50052)
    assert "lan_tunnel" not in stranger


def test_nobody_else_is_asked_and_nobody_else_can_offer_one(env):
    answer = _heartbeat("stranger", {**gpu_hw(), "lan_ipv4": ["192.168.40.50"]}, "203.0.113.77", port=50052)
    assert "send_lan" not in answer
    assert link_paths.lan_routes.offers("203.0.113.77", 50052) == []
    cpu = _heartbeat("cpu-home", CPU_HW, HOUSEHOLD_IP)
    assert "send_lan" not in cpu, "a machine with no GPU has nothing to reach over the LAN"


def test_a_cpu_only_machine_is_a_phone_pool_member_as_before(env):
    answer = _heartbeat("cpu1", {**CPU_HW, "pool": "app", "app": {"model": None, "ready": False, "slots": 2}},
                        "10.0.0.48")
    assert answer == {"status": "ok", "app_model": "small" if False else answer["app_model"], "role": "app",
                      "coding_tier": None}
    assert _heartbeat("cpu2", CPU_HW, "10.0.0.49") == {"status": "ok", "role": "app", "coding_tier": None}


def test_with_members_off_the_heartbeat_answer_is_what_it_always_was(env):
    env.limits["coding_members_enabled"] = False
    measured(env, "far", "10.0.0.50", 80.0)
    assert _heartbeat("far", gpu_hw(), "10.0.0.50") == {"status": "ok"}
    assert _heartbeat("cpu", CPU_HW, "10.0.0.51") == {"status": "ok"}


def test_a_coding_member_heartbeat_is_registered_but_never_joins_the_pipeline(env):
    key, identity, _trust = make_identity()
    block = sign_block({"model": MODEL_FILE, "tier": "qwen36-a3b", "ready": True, "slots": 2, "busy": 0,
                        "decode_tps": 33.0, "prefill_tps": 121.0, "pubkey": generate_keypair()[1],
                        "no_trace": True, "identity": identity}, key)
    measured(env, "cm", "10.0.0.52", 80.0)
    answer = _heartbeat("cm", {**gpu_hw(8.0, 32.0), "pool": "coding", "coding": block}, "10.0.0.52")
    assert answer["role"] == "coding"
    assert [r["worker_id"] for r in env.registry.coding_members()] == ["cm"]
    assert env.registry.get_active_rpc_endpoints() == []
    assert env.registry.get_total_vram_gb() == 0.0 and env.registry.get_total_sysram_gb() == 0.0
    assert env.registry.get_total_cpu_cores() == 0 and env.registry.get_total_cpu_threads() == 0


def test_an_implausible_coding_section_is_refused(env):
    bad = {**gpu_hw(), "pool": "coding", "coding": {"model": "m", "ready": "yes", "slots": 2}}
    with pytest.raises(HTTPException) as refused:
        _heartbeat("bad", bad, "10.0.0.53")
    assert refused.value.status_code == 400
    assert env.registry.coding_members() == []


# ======================================================================================
# /v1/cluster/status
# ======================================================================================

@pytest.mark.anyio
async def test_the_status_counts_members_slots_and_busy(env):
    env.add(env.fakes["a"], "wa", slots=2, busy=1)
    env.add(env.fakes["b"], "wb", slots=4, busy=0)
    env.add(env.fakes["c"], "wc", slots=2, ready=False)
    env.add(env.fakes["c"], "wd", slots=2, decode=5.0)            # below the floor
    async with client() as c:
        resp = await c.get("/v1/cluster/status")
    assert resp.status_code == 200
    assert resp.json()["coding_members"] == {
        "enabled": True, "members": 4, "ready": 2, "slots": 6, "busy": 1,
        "decode_tps_median": 33.0, "decode_tps_min": 33.0, "prefill_tps_median": 121.0, "prefill_tps_min": 121.0}


@pytest.mark.anyio
async def test_the_status_says_when_members_are_off(env):
    env.limits["coding_members_enabled"] = False
    async with client() as c:
        resp = await c.get("/v1/cluster/status")
    assert resp.json()["coding_members"] == {
        "enabled": False, "members": 0, "ready": 0, "slots": 0, "busy": 0,
        "decode_tps_median": None, "decode_tps_min": None, "prefill_tps_median": None, "prefill_tps_min": None}


@pytest.mark.anyio
async def test_the_status_counts_say_nothing_about_who_the_members_are(env):
    env.add(env.fakes["a"], "wa")
    async with client() as c:
        resp = await c.get("/v1/cluster/status")
    block = json.dumps(resp.json()["coding_members"])
    assert "wa" not in block and "10.8" not in block and env.fakes["a"].pubkey not in block


# ======================================================================================
# A streamed request that has to wait says where it stands (docs/coding-members.md section 10)
# ======================================================================================

KEY = {"Authorization": "Bearer dlne_sk_good"}


async def until(predicate, seconds=5.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


def queue_frames(content: bytes):
    """The ``dlne_queue`` objects of an event stream, in order."""
    return [json.loads(line[len("data: "):])["dlne_queue"] for line in content.decode().split("\n")
            if line.startswith("data: ") and '"dlne_queue"' in line]


@pytest.fixture
def queue(env, monkeypatch):
    """The local engine has one request slot (a fresh gate), frames come fast, members are off. Requests
    come from the DLNE extension (X-DLNE-Session), the only caller that is sent frames."""
    monkeypatch.setitem(globals(), "ANY", {**ANY, "X-DLNE-Session": "queue-test-session"})
    gate = CodingGate(slots=1)
    monkeypatch.setattr(coordinator, "coding_gate", gate)
    monkeypatch.setattr(coordinator, "QUEUE_FRAME_SECONDS", 0.15)
    monkeypatch.setattr(coordinator, "QUEUE_POLL_SECONDS", 0.05)
    monkeypatch.setattr(coordinator, "_coding_hold_seconds", deque(maxlen=40))
    env.limits["coding_members_enabled"] = False
    return gate


@pytest.mark.anyio
async def test_a_streamed_request_that_must_wait_is_told_its_place_before_the_answer(env, queue):
    assert queue.try_acquire()                                    # the engine's only slot is taken
    base = coordinator.active_inference_requests
    async with client() as c:
        task = asyncio.create_task(chat(c))
        assert await until(lambda: queue.stats()["waiting"] == 1)
        assert coordinator.active_inference_requests == base + 1, "waiting counts as in flight"
        await asyncio.sleep(0.5)                                  # a few frames' worth
        queue.release()
        resp = await task
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/event-stream")
    frames = queue_frames(resp.content)
    assert len(frames) >= 3
    assert frames[0] == {"position": 1, "waiting": 1, "slots": 1, "in_use": 1}
    for frame in frames:
        assert set(frame) <= {"position", "waiting", "slots", "in_use", "eta_seconds"}
        assert all(type(value) is int for value in frame.values())
    text = resp.text
    assert text.rindex('"dlne_queue"') < text.index(LOCAL_ANSWER.decode()), "the frames come before the first real chunk"
    assert queue.stats()["in_use"] == 0 and queue.stats()["waiting"] == 0
    assert coordinator.active_inference_requests == base
    assert env.local.starts, "and then the engine's own answer"


@pytest.mark.anyio
async def test_the_frames_carry_counts_only(env, queue):
    queue.try_acquire()
    async with client() as c:
        task = asyncio.create_task(chat(c, {**CHAT, "messages": [{"role": "user", "content": "SECRET-PROMPT-TEXT"}],
                                            "user": "alice"}, {**ANY, "Authorization": "Bearer dlne_sk_SECRETKEY",
                                                               "X-DLNE-Session": "SESSION-ID-123"}))
        assert await until(lambda: queue.stats()["waiting"] == 1)
        await asyncio.sleep(0.3)
        queue.release()
        resp = await task
    frame_lines = [line for line in resp.text.split("\n") if '"dlne_queue"' in line]
    assert frame_lines
    for needle in ("SECRET", "alice", "SESSION-ID", REMOTE[0], "member", "worker"):
        assert not any(needle in line for line in frame_lines), needle


@pytest.mark.anyio
async def test_the_place_is_the_exact_place_in_the_tiered_queue(env, queue, tiers):
    queue.try_acquire()
    async with client() as c:
        trial = asyncio.create_task(chat(c))
        assert await until(lambda: queue.stats()["waiting"] == 1)
        await asyncio.sleep(0.25)
        keyed = asyncio.create_task(chat(c, {**CHAT, "model": "Key-Model.gguf"}, {**ANY, **KEY}))
        assert await until(lambda: queue.stats()["waiting"] == 2)
        await asyncio.sleep(0.4)
        queue.release()
        keyed_resp, trial_resp = await asyncio.gather(keyed, trial)
    trial_places = [f["position"] for f in queue_frames(trial_resp.content)]
    keyed_places = [f["position"] for f in queue_frames(keyed_resp.content)]
    assert trial_places[0] == 1 and 2 in trial_places and trial_places == sorted(trial_places), \
        "the free trial was first, and fell back a place when a key holder arrived"
    assert set(keyed_places) == {1}, "the key holder goes straight to the front"
    assert env.local.starts[0] == "Key-Model.gguf", "and was served first"
    assert queue.stats() == {"slots": 1, "in_use": 0, "waiting": 0, "waiting_by_priority": {}}


@pytest.mark.anyio
async def test_the_estimate_is_left_out_until_it_is_known_then_follows_the_position(env, queue):
    queue.try_acquire()
    async with client() as c:
        first = asyncio.create_task(chat(c))
        assert await until(lambda: queue.stats()["waiting"] == 1)
        await asyncio.sleep(0.2)
        coordinator._coding_hold_seconds.extend([10.0, 12.0, 14.0])
        await asyncio.sleep(0.3)
        queue.release()
        resp = await first
    frames = queue_frames(resp.content)
    assert "eta_seconds" not in frames[0]
    assert frames[-1]["eta_seconds"] == 12 * frames[-1]["position"] // frames[-1]["slots"]


def test_the_estimate(monkeypatch):
    monkeypatch.setattr(coordinator, "_coding_hold_seconds", deque(maxlen=40))
    assert coordinator._coding_eta(3, 2) is None
    for held in (30.0, 10.0, 20.0):
        coordinator._note_coding_hold(held)
    for nonsense in (1e9, -1.0, 0.0):
        coordinator._note_coding_hold(nonsense)
    assert sorted(coordinator._coding_hold_seconds) == [10.0, 20.0, 30.0], "nonsense is not recorded"
    assert coordinator._coding_eta(1, 1) == 20 and coordinator._coding_eta(3, 2) == 30
    assert coordinator._coding_eta(1, 0) == 20, "no slots is read as one"
    assert coordinator._coding_eta(10 ** 6, 1) == 3600, "capped at an hour"


@pytest.mark.anyio
async def test_a_request_with_a_free_slot_is_not_queued_and_gets_no_frames(env, queue):
    async with client() as c:
        resp = await chat(c)
    assert queue_frames(resp.content) == [] and LOCAL_ANSWER in resp.content
    assert queue.stats()["in_use"] == 0, "the slot taken at the door was given back"


@pytest.mark.anyio
async def test_a_request_that_is_not_streamed_never_gets_frames(env, queue):
    queue.try_acquire()
    async with client() as c:
        resp = await chat(c, {**CHAT, "stream": False})
    assert queue_frames(resp.content) == [] and resp.status_code == 200
    assert queue.stats() == {"slots": 1, "in_use": 1, "waiting": 0, "waiting_by_priority": {}}


@pytest.mark.anyio
async def test_a_caller_that_is_not_the_extension_takes_the_old_path_and_gets_no_frames(env, queue, monkeypatch):
    """A generic OpenAI client (no X-DLNE-Session) would read a chunk without "choices" as a broken
    answer, so it never enters the frame-sending queue (no early 200, no frames): it waits for the
    engine inside the handler exactly as in 2.3.7."""
    def no_frames_queue(*args, **kwargs):
        raise AssertionError("a caller that is not the extension must not be queued with frames")

    monkeypatch.setattr(coordinator, "_QueuedChat", no_frames_queue)
    queue.try_acquire()
    plain = {k: v for k, v in ANY.items() if k != "X-DLNE-Session"}
    async with client() as c:
        task = asyncio.create_task(chat(c, headers=plain))
        await asyncio.sleep(0.3)
        queue.release()
        resp = await asyncio.wait_for(task, 10)
    assert resp.status_code == 200 and queue_frames(resp.content) == [] and LOCAL_ANSWER in resp.content


@pytest.mark.anyio
async def test_a_client_that_leaves_the_queue_takes_nothing_with_it(env, queue):
    queue.try_acquire()
    base = coordinator.active_inference_requests
    async with client() as c:
        task = asyncio.create_task(chat(c))
        assert await until(lambda: queue.stats()["waiting"] == 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await until(lambda: queue.stats()["waiting"] == 0)
    assert queue.stats()["in_use"] == 1 and coordinator.active_inference_requests == base
    queue.release()
    assert queue.try_acquire(), "the slot is free again for the next in line"


@pytest.mark.anyio
async def test_an_engine_that_cannot_start_ends_a_queued_request_in_the_stream_and_frees_its_slot(env, queue):
    env.local.breaks = True
    queue.try_acquire()
    async with client() as c:
        task = asyncio.create_task(chat(c))
        assert await until(lambda: queue.stats()["waiting"] == 1)
        queue.release()
        resp = await task
    assert resp.status_code == 200 and b"Failed to initialize inference engine" in resp.content
    assert resp.content.endswith(b"data: [DONE]\n\n")
    assert queue.stats()["in_use"] == 0


@pytest.mark.anyio
async def test_a_coding_members_owner_queues_like_a_gpu_contributor(env, queue, tiers):
    """The owner's extension presents its worker's token, and the contribution middleware already knows a
    registered worker with a GPU as tier "gpu" (first in line). A coding member is such a worker, so its
    owner needs nothing new: no link between a member and its owner's requests is invented here."""
    env.add(env.fakes["a"], "wa")
    env.limits["coding_members_enabled"] = False                  # this is about the queue, not about routing
    queue.try_acquire()
    async with client() as c:
        trial = asyncio.create_task(chat(c))
        assert await until(lambda: queue.stats()["waiting"] == 1)
        await asyncio.sleep(0.25)
        owner = asyncio.create_task(chat(c, headers={**ANY, "Authorization": "Bearer tok-wa"}))
        assert await until(lambda: queue.stats()["waiting"] == 2)
        assert queue.stats()["waiting_by_priority"] == {"0": 1, "2": 1}
        await asyncio.sleep(0.4)
        queue.release()
        owner_resp, trial_resp = await asyncio.gather(owner, trial)
    assert {f["position"] for f in queue_frames(owner_resp.content)} == {1}
    assert 2 in [f["position"] for f in queue_frames(trial_resp.content)]


@pytest.mark.anyio
async def test_the_queue_counts_stay_in_the_cluster_status(env, queue):
    queue.try_acquire()
    async with client() as c:
        task = asyncio.create_task(chat(c))
        assert await until(lambda: queue.stats()["waiting"] == 1)
        status = (await c.get("/v1/cluster/status")).json()
        queue.release()
        await task
    assert status["coding_queue"] == {"slots": 1, "in_use": 1, "waiting": 1, "waiting_by_priority": {"1": 1}}


@pytest.mark.anyio
async def test_a_queued_request_waits_for_the_engine_only_though_a_member_has_a_free_slot(env, queue):
    """Only a sealed request goes to a member, so a plain one that has to wait has one queue: the engine's."""
    env.limits["coding_members_enabled"] = True
    env.add(env.fakes["a"], "wa")
    queue.try_acquire()
    async with client() as c:
        task = asyncio.create_task(chat(c))
        assert await until(lambda: queue.stats()["waiting"] == 1)
        await asyncio.sleep(0.3)
        queue.release()
        resp = await task
    frames = queue_frames(resp.content)
    assert frames and frames[0] == {"position": 1, "waiting": 1, "slots": 1, "in_use": 1}, "the engine's slots only"
    assert LOCAL_ANSWER in resp.content and env.fakes["a"].requests == []
    assert queue.stats()["in_use"] == 0



# ---- 2.5.0: docked laptops shard; a member is not a member for good -----------------------------------------

def test_the_grace_starts_over_when_a_worker_leaves_and_comes_back(env):
    """Pruned after 20 s of silence and registered again: its first beat is not "far", however long ago this
    coordinator first heard of it."""
    assert _heartbeat("back", gpu_hw(), "10.0.0.70")["role"] is None
    _aged(env, "back", 600)
    assert _heartbeat("back", gpu_hw(), "10.0.0.70")["role"] == "coding", "past the grace: far"
    env.registry.workers["back"]["last_heartbeat"] -= 3600
    env.registry.prune_dead_workers()
    assert "back" not in env.registry.workers
    assert _heartbeat("back", gpu_hw(), "10.0.0.70")["role"] is None, "a fresh registration waits for the probe again"


def test_a_serving_member_that_is_measured_near_is_called_back_into_the_shared_engine(env):
    key, identity, _trust = make_identity()
    block = sign_block({"model": MODEL_FILE, "tier": "qwen36-a3b", "ready": True, "slots": 2, "busy": 0,
                        "decode_tps": 33.0, "prefill_tps": 121.0, "pubkey": generate_keypair()[1],
                        "no_trace": True, "identity": identity}, key)
    serving = {**gpu_hw(8.0, 32.0), "pool": "coding", "coding": block, "docked": True}
    assert _heartbeat("home", serving, "10.0.0.71")["role"] == "coding", "unmeasured: it keeps serving"
    assert "10.0.0.71:50053" in coordinator._probe_targets(), "a member is measured like any other GPU"
    coordinator._record_probed_rtt("10.0.0.71:50053", 2.0)
    assert _heartbeat("home", serving, "10.0.0.71")["role"] == "rpc", "near: it shards again"


def test_an_undocked_near_laptop_does_not_unlock_a_bigger_model(env):
    measured(env, "docked", "10.0.0.72", 2.0, {**gpu_hw(8.0), "docked": True})
    measured(env, "undocked", "10.0.0.73", 2.0, {**gpu_hw(8.0), "docked": False})
    measured(env, "unsaid", "10.0.0.74", 2.0, gpu_hw(8.0))
    pool = coordinator._coding_pool()
    assert pool["vram_gb"] == 16.0, "the docked one and the one that could not say; never the undocked one"


def test_an_rpc_node_stepping_aside_is_told_it_is_draining_while_the_engine_still_runs_on_it(env, monkeypatch):
    monkeypatch.setattr(type(coordinator.manager), "current_endpoints",
                        property(lambda self: ["127.0.0.1:50052", "10.0.0.80:50053"]), raising=False)
    measured(env, "shard", "10.0.0.80", 2.0, gpu_hw(8.0))
    quiet = _heartbeat("shard", gpu_hw(8.0), "10.0.0.80")
    assert quiet["shard"] is True and "draining" not in quiet
    assert _heartbeat("shard", {**gpu_hw(8.0), "is_yielding": True}, "10.0.0.80")["draining"] is True
    assert _heartbeat("shard", {**gpu_hw(8.0), "leaving": True}, "10.0.0.80")["draining"] is True
    other = _heartbeat("other", {**gpu_hw(8.0), "is_yielding": True}, "10.0.0.81")
    assert "draining" not in other and "shard" not in other, "the engine does not run on it"


def test_a_near_laptop_off_its_dock_is_a_coding_member_and_a_docked_one_shards(env):
    measured(env, "docked", "10.0.0.90", 2.0, {**gpu_hw(8.0, 32.0), "docked": True})
    measured(env, "loose", "10.0.0.91", 2.0, {**gpu_hw(8.0, 32.0), "docked": False})
    assert _heartbeat("docked", {**gpu_hw(8.0, 32.0), "docked": True}, "10.0.0.90")["role"] == "rpc"
    answer = _heartbeat("loose", {**gpu_hw(8.0, 32.0), "docked": False}, "10.0.0.91")
    assert answer["role"] == "coding" and answer["coding_tier"]["id"] == TIER["id"]
    assert _heartbeat("loose", {**gpu_hw(8.0, 32.0), "docked": False, "is_yielding": True}, "10.0.0.91")["role"] is None
