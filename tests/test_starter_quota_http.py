"""
HTTP-level tests for the free starter trial: the real ``NonContribBlockerMiddleware`` driven as
a *remote* client, so the quota, the ``X-DLNE-Quota-Remaining`` header and the 403 wall are
exercised exactly as the VS Code extension sees them.

The chat/completions handlers need a running engine. These tests deliberately send a
malformed body so the handler rejects the request immediately; the middleware has already
decided and stamped the header by then, which is all that is under test. The handler's own
status code is irrelevant here and is never asserted beyond "not the trial 403".
"""

import pytest
from httpx import ASGITransport, AsyncClient

from engine import coordinator

REMOTE = ("203.0.113.9", 40000)  # TEST-NET-3: never a local/coordinator address
HEADER = "x-dlne-quota-remaining"
TRIAL = 3


@pytest.fixture
def trial(tmp_path, monkeypatch):
    """A tiny isolated trial: 3 chat requests, usage file in tmp_path (never the real install)."""
    mgr = coordinator.StarterQuotaManager(str(tmp_path / "starter_quota.json"), default_quota=TRIAL)
    monkeypatch.setattr(coordinator, "starter_quota_mgr", mgr)

    real_limits = coordinator.get_worker_limits

    def limits():
        return {**real_limits(), "enforce_contrib_clause": True, "starter_quota_inferences": TRIAL}

    monkeypatch.setattr(coordinator, "get_worker_limits", limits)
    return mgr


def _client(ip=REMOTE, headers=None):
    transport = ASGITransport(app=coordinator.app, client=ip)
    return AsyncClient(transport=transport, base_url="http://test", headers=headers or {})


async def _chat(client):
    return await client.post("/v1/chat/completions", content="{", headers={"Content-Type": "application/json"})


async def _complete(client):
    return await client.post("/v1/completions", content="{", headers={"Content-Type": "application/json"})


@pytest.mark.anyio
async def test_chat_spends_the_trial_and_header_counts_down(trial):
    async with _client() as c:
        remaining = []
        for _ in range(TRIAL):
            resp = await _chat(c)
            assert resp.status_code != 403
            remaining.append(int(resp.headers[HEADER]))
        assert remaining == [2, 1, 0]


@pytest.mark.anyio
async def test_exhausted_trial_gets_403_with_zero_header(trial):
    async with _client() as c:
        for _ in range(TRIAL):
            await _chat(c)
        resp = await _chat(c)
        assert resp.status_code == 403
        assert resp.headers[HEADER] == "0"
        assert "starter quota" in resp.json()["detail"].lower()
        assert "contribute" in resp.json()["detail"].lower()


@pytest.mark.anyio
async def test_autocomplete_is_never_charged(trial):
    async with _client() as c:
        for _ in range(40):
            resp = await _complete(c)
            assert resp.status_code != 403
            assert resp.headers[HEADER] == str(TRIAL)
        # The trial is untouched, so chat still has its full allowance.
        resp = await _chat(c)
        assert resp.headers[HEADER] == str(TRIAL - 1)
    assert trial.peek(REMOTE[0], limit=TRIAL) == (True, 1, TRIAL - 1)


@pytest.mark.anyio
async def test_autocomplete_stops_once_the_chat_trial_is_spent(trial):
    async with _client() as c:
        for _ in range(TRIAL):
            await _chat(c)
        resp = await _complete(c)
        assert resp.status_code == 403
        assert resp.headers[HEADER] == "0"


@pytest.mark.anyio
async def test_clients_have_independent_trials(trial):
    async with _client(("203.0.113.10", 1)) as a, _client(("203.0.113.11", 1)) as b:
        for _ in range(TRIAL):
            await _chat(a)
        assert (await _chat(a)).status_code == 403
        resp = await _chat(b)
        assert resp.status_code != 403
        assert resp.headers[HEADER] == str(TRIAL - 1)


@pytest.mark.anyio
async def test_authorized_api_key_bypasses_the_trial(trial, monkeypatch):
    monkeypatch.setattr(coordinator.api_key_manager, "verify_key", lambda token: token == "dlne_sk_good")
    async with _client(headers={"Authorization": "Bearer dlne_sk_good"}) as c:
        for _ in range(TRIAL + 5):
            resp = await _chat(c)
            assert resp.status_code != 403
            assert HEADER not in resp.headers
    assert trial._usage == {}


@pytest.mark.anyio
async def test_active_contributor_bypasses_the_trial(trial, monkeypatch):
    worker = {"hardware": {"gpu": {"vram_total_gb": 8}, "ram_gb": 16, "cpu_cores": 8}}
    monkeypatch.setattr(coordinator.registry, "find_by_token", lambda t: worker if t == "worker-token" else None)
    async with _client(headers={"Authorization": "Bearer worker-token"}) as c:
        for _ in range(TRIAL + 5):
            resp = await _chat(c)
            assert resp.status_code != 403
            assert HEADER not in resp.headers
    assert trial._usage == {}


@pytest.mark.anyio
async def test_coordinator_host_is_exempt(trial):
    async with _client(("127.0.0.1", 1)) as c:
        for _ in range(TRIAL + 5):
            resp = await _chat(c)
            assert resp.status_code != 403
            assert HEADER not in resp.headers


@pytest.mark.anyio
async def test_trial_survives_a_coordinator_restart(trial, tmp_path):
    async with _client() as c:
        await _chat(c)
        await _chat(c)
    reloaded = coordinator.StarterQuotaManager(str(tmp_path / "starter_quota.json"), default_quota=TRIAL)
    assert reloaded.peek(REMOTE[0], limit=TRIAL) == (True, 2, 1)


@pytest.mark.anyio
async def test_disabling_the_clause_removes_the_wall(trial, monkeypatch):
    real_limits = coordinator.get_worker_limits
    monkeypatch.setattr(coordinator, "get_worker_limits", lambda: {**real_limits(), "enforce_contrib_clause": False})
    async with _client() as c:
        for _ in range(TRIAL + 5):
            assert (await _chat(c)).status_code != 403


def test_every_default_says_200(monkeypatch):
    """The config default (no override present) and the manager default must agree on 200."""
    from engine import config

    monkeypatch.setattr(config, "get_config", lambda force_reload=False: {})
    assert config.get_worker_limits()["starter_quota_inferences"] == 200
    assert coordinator.StarterQuotaManager.__init__.__defaults__[-1] == 200


def test_config_override_is_honoured(monkeypatch):
    from engine import config

    monkeypatch.setattr(config, "get_config", lambda force_reload=False: {"STARTER_QUOTA_INFERENCES": "7"})
    assert config.get_worker_limits()["starter_quota_inferences"] == 7
