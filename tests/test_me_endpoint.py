"""
GET /v1/me (2.7.0, docs/release-2.7-contract.md section 7): the tier the coordinator gives the caller, so the extension
can tell whether a key holder's unlimited agent turns apply. It must agree with the tier the middleware hands a chat
request, cost a trial caller nothing, and say nothing else.
"""

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from engine import coordinator
from engine.priority import UsageWindow

GPU_NODE = {"worker_id": "gpu-node", "hardware": {"gpu": {"vram_total_gb": 8}, "ram_gb": 16, "cpu_cores": 8}}
CPU_NODE = {"worker_id": "cpu-node", "hardware": {
    "gpu": {"has_gpu": False, "vram_total_gb": 0}, "ram_gb": 12, "cpu_cores": 6, "cpu_arch": "x86_64",
    "cpu_features": ["avx2", "fma"], "mem_bandwidth_gbs": 48.0}}
WEAK_NODE = {"worker_id": "weak-node", "hardware": {"gpu": {"has_gpu": False, "vram_total_gb": 0}, "ram_gb": 1, "cpu_cores": 1}}
REMOTE = ("198.51.100.40", 40000)
LOCAL = ("127.0.0.1", 1)


class Env:
    pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    quota = coordinator.StarterQuotaManager(str(tmp_path / "q.json"), default_quota=3)
    monkeypatch.setattr(coordinator, "starter_quota_mgr", quota)
    monkeypatch.setattr(coordinator, "cpu_coding_window", UsageWindow())
    real = coordinator.get_worker_limits
    limits = {"enforce_contrib_clause": True, "starter_quota_inferences": 3}
    monkeypatch.setattr(coordinator, "get_worker_limits", lambda: {**real(), **limits})
    tokens = {"gpu-token": GPU_NODE, "cpu-token": CPU_NODE, "weak-token": WEAK_NODE}
    monkeypatch.setattr(coordinator.registry, "find_by_token", lambda t: tokens.get(t))
    monkeypatch.setattr(coordinator.api_key_manager, "verify_key", lambda t: t == "dlne_sk_good")

    # the real middleware and the real /v1/me, beside a chat route that reports the tier the middleware gave it
    test_app = FastAPI()
    test_app.add_middleware(coordinator.NonContribBlockerMiddleware)
    test_app.add_api_route("/v1/me", coordinator.get_me, methods=["GET"])

    @test_app.post("/v1/chat/completions")
    async def chat(request: Request):
        return {"tier": getattr(request.state, "dlne_tier", None)}

    e = Env()
    e.app, e.quota, e.limits = test_app, quota, limits
    return e


def caller(env, token=None, ip=REMOTE):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return AsyncClient(transport=ASGITransport(app=env.app, client=ip), base_url="http://t", headers=headers)


CASES = [("dlne_sk_good", REMOTE, "key"), ("gpu-token", REMOTE, "gpu"), ("cpu-token", REMOTE, "cpu"),
         (None, REMOTE, "trial"), ("not-a-key", REMOTE, "trial"), (None, LOCAL, "host")]


@pytest.mark.anyio
@pytest.mark.parametrize("token, ip, tier", CASES)
async def test_me_says_the_tier_the_caller_is_given(env, token, ip, tier):
    async with caller(env, token, ip) as c:
        reply = await c.get("/v1/me")
    assert reply.status_code == 200 and reply.json() == {"tier": tier}
    assert reply.headers["cache-control"] == "no-store"


@pytest.mark.anyio
@pytest.mark.parametrize("token, ip, tier", CASES)
async def test_me_agrees_with_the_tier_a_chat_request_gets(env, token, ip, tier):
    async with caller(env, token, ip) as c:
        chat = await c.post("/v1/chat/completions")
        me = await c.get("/v1/me")
    assert me.json()["tier"] == chat.json()["tier"] == tier


@pytest.mark.anyio
async def test_with_the_contribution_rule_off_everyone_is_open(env):
    env.limits["enforce_contrib_clause"] = False
    async with caller(env) as c:
        assert (await c.get("/v1/me")).json() == {"tier": "open"}


@pytest.mark.anyio
async def test_a_contributor_below_the_floors_has_no_standing(env):
    async with caller(env, "weak-token") as c:
        assert (await c.get("/v1/me")).json() == {"tier": "trial"}


@pytest.mark.anyio
async def test_asking_never_spends_a_trial_chat_and_still_works_when_the_trial_is_used_up(env):
    async with caller(env) as c:
        for _ in range(10):
            assert (await c.get("/v1/me")).json() == {"tier": "trial"}
        assert env.quota.summary(3)["chats_used"] == 0
        for _ in range(3):
            assert (await c.post("/v1/chat/completions")).status_code == 200
        assert (await c.post("/v1/chat/completions")).status_code == 403          # the trial is gone ...
        assert (await c.get("/v1/me")).json() == {"tier": "trial"}               # ... and /v1/me still answers


@pytest.mark.anyio
async def test_the_answer_holds_the_tier_and_nothing_else(env):
    async with caller(env, "dlne_sk_good") as c:
        body = (await c.get("/v1/me")).json()
    assert list(body) == ["tier"]


@pytest.mark.anyio
async def test_the_real_app_serves_it(monkeypatch):
    monkeypatch.setattr(coordinator.api_key_manager, "verify_key", lambda t: t == "dlne_sk_good")
    transport = ASGITransport(app=coordinator.app, client=REMOTE)
    async with AsyncClient(transport=transport, base_url="http://t", headers={"Authorization": "Bearer dlne_sk_good"}) as c:
        reply = await c.get("/v1/me")
    assert reply.status_code == 200 and reply.json() == {"tier": "key"}
