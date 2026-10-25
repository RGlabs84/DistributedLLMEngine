"""
Tests for the operator (admin) surface BarrkBOT drives: the key gate, /v1/admin/stats,
the free-trial inspect/reset endpoints, and the `adminkey` CLI that mints the key.

The coordinator is exercised through the real ASGI app with a chosen client address, so the
"loopback only unless allowed" rule is tested the way a request would actually hit it.
"""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from engine import api_keys, coordinator

KEY = "dlne_ak_test_key_for_unit_tests"
LOOPBACK = ("127.0.0.1", 40000)
REMOTE = ("203.0.113.50", 40000)   # TEST-NET-3: never local


@pytest.fixture
def admin_env(tmp_path, monkeypatch):
    """Configured operator key, an isolated trial store, and a clean legacy-session table."""
    real_limits = coordinator.get_worker_limits
    state = {
        "admin_key_hash": api_keys.hash_admin_key(KEY),
        "admin_allow_remote": False,
        "starter_quota_inferences": 5,   # matches the 5-chat trial store below
    }

    monkeypatch.setattr(coordinator, "get_worker_limits", lambda: {**real_limits(), **state})
    mgr = coordinator.StarterQuotaManager(str(tmp_path / "starter_quota.json"), default_quota=5)
    monkeypatch.setattr(coordinator, "starter_quota_mgr", mgr)
    return type("Env", (), {"state": state, "quota": mgr})


def _client(ip=LOOPBACK, key=KEY):
    headers = {"Authorization": f"Bearer {key}"} if key is not None else {}
    return AsyncClient(transport=ASGITransport(app=coordinator.app, client=ip), base_url="http://test", headers=headers)


ADMIN_GETS = ["/v1/admin/stats", "/v1/admin/quota", "/v1/admin/telemetry"]


# --- the gate ---------------------------------------------------------------------------

@pytest.mark.anyio
@pytest.mark.parametrize("path", ADMIN_GETS)
async def test_the_operator_key_opens_every_admin_read_from_loopback(admin_env, path):
    async with _client() as c:
        assert (await c.get(path)).status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("path", ADMIN_GETS)
async def test_no_credentials_means_403(admin_env, path):
    async with _client(key=None) as c:
        assert (await c.get(path)).status_code == 403


@pytest.mark.anyio
@pytest.mark.parametrize("bad", ["wrong", "dlne_ak_", KEY + "x", KEY[:-1], ""])
async def test_a_wrong_key_means_403(admin_env, bad):
    async with _client(key=bad) as c:
        assert (await c.get("/v1/admin/stats")).status_code == 403


@pytest.mark.anyio
async def test_the_key_is_refused_from_a_remote_address_by_default(admin_env):
    async with _client(ip=REMOTE) as c:
        resp = await c.get("/v1/admin/stats")
    assert resp.status_code == 403
    assert "not accepted from this address" in resp.json()["detail"]


@pytest.mark.anyio
async def test_remote_admin_works_only_when_explicitly_allowed(admin_env):
    admin_env.state["admin_allow_remote"] = True
    async with _client(ip=REMOTE) as c:
        assert (await c.get("/v1/admin/stats")).status_code == 200


@pytest.mark.anyio
async def test_with_no_key_configured_nothing_matches_not_even_an_empty_token(admin_env):
    admin_env.state["admin_key_hash"] = ""
    async with _client(key=KEY) as c:
        assert (await c.get("/v1/admin/stats")).status_code == 403
    async with _client(key="") as c:
        assert (await c.get("/v1/admin/stats")).status_code == 403


def test_an_empty_token_never_matches_even_if_the_stored_hash_is_the_hash_of_nothing(admin_env):
    """Belt and braces: a mis-set ADMIN_KEY_HASH of sha256("") must not make "" a valid key."""
    admin_env.state["admin_key_hash"] = api_keys.hash_admin_key("")
    assert coordinator._admin_key_matches("") is False


@pytest.mark.anyio
async def test_the_browser_password_login_is_gone(admin_env):
    """The password login and its session tokens went with the web UI; the key is the only door."""
    async with _client(key=None, ip=LOOPBACK) as c:
        resp = await c.post("/v1/admin/auth", json={"password": "anything"})
    assert resp.status_code in (404, 405)
    for name in ("_register_admin_token", "_is_admin_token_valid", "active_admin_tokens", "admin_auth"):
        assert not hasattr(coordinator, name), name


@pytest.mark.anyio
@pytest.mark.parametrize("method,path", [("post", "/v1/admin/apikeys"), ("get", "/v1/admin/apikeys"),
                                         ("delete", "/v1/admin/apikeys/x")])
async def test_the_api_key_routes_need_the_key_even_from_the_coordinators_own_host(admin_env, method, path):
    """They used to trust any local caller, and an X-Admin-Token from remote ones."""
    async with _client(key=None, ip=LOOPBACK) as c:
        kwargs = {"json": {"name": "n"}} if method == "post" else {}
        assert (await getattr(c, method)(path, **kwargs)).status_code == 403
    async with _client(key=None, ip=LOOPBACK) as c:
        resp = await getattr(c, method)(path, headers={"X-Admin-Token": "whatever"}, **({"json": {"name": "n"}} if method == "post" else {}))
        assert resp.status_code == 403


@pytest.mark.anyio
async def test_the_key_hash_in_config_is_what_is_compared(admin_env):
    admin_env.state["admin_key_hash"] = api_keys.hash_admin_key("a-different-key")
    async with _client() as c:
        assert (await c.get("/v1/admin/stats")).status_code == 403


# --- /v1/admin/stats --------------------------------------------------------------------

@pytest.mark.anyio
async def test_stats_reports_version_cluster_trial_keys_and_release(admin_env):
    for _ in range(3):
        admin_env.quota.check_and_consume("198.51.100.7", limit=5)
    async with _client() as c:
        data = (await c.get("/v1/admin/stats")).json()

    assert data["server_version"] == coordinator.VERSION
    assert data["uptime_seconds"] >= 0
    assert set(data["cluster"]) >= {"status", "active_model", "running_models", "workers",
                                    "total_vram_gb", "total_sysram_gb", "in_flight_requests", "active_leases"}
    assert data["trial"]["clients"] == 1
    assert data["trial"]["chats_used"] == 3
    assert data["trial"]["exhausted"] == 0
    assert set(data["api_keys"]) == {"active", "revoked"}
    assert "release" in data
    assert "update" not in data, "the consent/rollout updater is gone; the release block replaces it"


@pytest.mark.anyio
async def test_stats_never_leaks_client_addresses_or_tokens(admin_env):
    admin_env.quota.check_and_consume("198.51.100.7", limit=5)
    admin_env.quota.check_and_consume("dlne_wk_supersecrettokenvalue", limit=5)
    async with _client() as c:
        body = (await c.get("/v1/admin/stats")).text
    assert "198.51.100.7" not in body
    assert "supersecrettokenvalue" not in body


# --- trial inspect / reset --------------------------------------------------------------

@pytest.mark.anyio
async def test_quota_view_masks_identifiers_and_ranks_by_use(admin_env):
    for _ in range(4):
        admin_env.quota.check_and_consume("198.51.100.7", limit=5)
    admin_env.quota.check_and_consume("dlne_wk_supersecrettokenvalue", limit=5)
    async with _client() as c:
        resp = await c.get("/v1/admin/quota")
    data = resp.json()
    assert data["top"][0] == {"client": "198.51.100.x", "used": 4}
    assert data["top"][1]["client"] == "dlne_w…"
    assert "supersecrettokenvalue" not in resp.text and "198.51.100.7" not in resp.text


@pytest.mark.anyio
async def test_exhausted_clients_are_counted(admin_env):
    for _ in range(5):
        admin_env.quota.check_and_consume("198.51.100.7", limit=5)
    async with _client() as c:
        data = (await c.get("/v1/admin/stats")).json()
    assert data["trial"]["exhausted"] == 1


@pytest.mark.anyio
async def test_reset_one_client_restores_their_trial(admin_env):
    for _ in range(5):
        admin_env.quota.check_and_consume("198.51.100.7", limit=5)
    admin_env.quota.check_and_consume("203.0.113.9", limit=5)
    async with _client() as c:
        resp = await c.post("/v1/admin/quota/reset", json={"client_id": "198.51.100.7"})
    assert resp.json() == {"status": "reset", "clients_reset": 1}
    assert admin_env.quota.peek("198.51.100.7", limit=5) == (True, 0, 5)
    assert admin_env.quota.peek("203.0.113.9", limit=5) == (True, 1, 4)   # untouched


@pytest.mark.anyio
async def test_reset_all_and_it_persists(admin_env, tmp_path):
    admin_env.quota.check_and_consume("a", limit=5)
    admin_env.quota.check_and_consume("b", limit=5)
    async with _client() as c:
        resp = await c.post("/v1/admin/quota/reset", json={"all": True})
    assert resp.json()["clients_reset"] == 2
    reloaded = coordinator.StarterQuotaManager(str(tmp_path / "starter_quota.json"), default_quota=5)
    assert reloaded.summary(5)["clients"] == 0


@pytest.mark.anyio
async def test_reset_of_an_unknown_client_resets_nothing(admin_env):
    async with _client() as c:
        resp = await c.post("/v1/admin/quota/reset", json={"client_id": "never-seen"})
    assert resp.json()["clients_reset"] == 0


@pytest.mark.anyio
@pytest.mark.parametrize("payload", [{}, {"client_id": ""}, {"client_id": 7}, {"all": "yes"}, [1, 2]])
async def test_reset_rejects_malformed_requests(admin_env, payload):
    async with _client() as c:
        assert (await c.post("/v1/admin/quota/reset", json=payload)).status_code == 400


@pytest.mark.anyio
async def test_reset_rejects_a_body_that_is_not_json(admin_env):
    async with _client() as c:
        resp = await c.post("/v1/admin/quota/reset", content="{", headers={"Content-Type": "application/json"})
    assert resp.status_code == 400


@pytest.mark.anyio
async def test_reset_requires_the_key(admin_env):
    async with _client(key=None) as c:
        assert (await c.post("/v1/admin/quota/reset", json={"all": True})).status_code == 403


# --- quota manager units ----------------------------------------------------------------

def test_mask_hides_the_last_octet_and_most_of_a_token():
    mask = coordinator.StarterQuotaManager._mask
    assert mask("10.1.2.3") == "10.1.2.x"
    assert mask("dlne_wk_abcdef") == "dlne_w…"
    assert mask("abc") == "…"


def test_summary_without_top_has_no_client_list(tmp_path):
    mgr = coordinator.StarterQuotaManager(str(tmp_path / "q.json"), default_quota=5)
    mgr.check_and_consume("x", limit=5)
    assert "top" not in mgr.summary(5)
    assert "top" in mgr.summary(5, top=3)


# --- key minting and the CLI ------------------------------------------------------------

def test_minted_keys_are_prefixed_long_and_unique():
    keys = {api_keys.mint_admin_key() for _ in range(50)}
    assert len(keys) == 50
    assert all(k.startswith("dlne_ak_") and len(k) >= 48 for k in keys)


def test_hash_is_stable_hex_sha256():
    h = api_keys.hash_admin_key("abc")
    assert h == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert api_keys.hash_admin_key("abc") == h


def _run_cli(monkeypatch, capsys, action, quiet):
    import dlne_server

    written = {}
    monkeypatch.setattr(dlne_server, "set_config_values", lambda updates: written.update(updates) or True)
    args = type("A", (), {"admin_action": action, "quiet": quiet})()
    dlne_server.cmd_adminkey(args)
    return written, capsys.readouterr().out


def test_cli_create_quiet_prints_only_the_key_and_stores_only_its_hash(monkeypatch, capsys):
    written, out = _run_cli(monkeypatch, capsys, "create", quiet=True)
    key = out.strip()
    assert key.startswith("dlne_ak_") and "\n" not in key
    assert written == {"ADMIN_KEY_HASH": api_keys.hash_admin_key(key)}
    assert key not in json.dumps(written)


def test_cli_create_verbose_shows_the_key_once_with_a_warning(monkeypatch, capsys):
    written, out = _run_cli(monkeypatch, capsys, "create", quiet=False)
    assert "Admin Key: dlne_ak_" in out
    assert "cannot be shown again" in out


def test_cli_revoke_clears_the_hash(monkeypatch, capsys):
    written, out = _run_cli(monkeypatch, capsys, "revoke", quiet=False)
    assert written == {"ADMIN_KEY_HASH": ""}


def test_a_key_minted_by_the_cli_opens_the_gate(monkeypatch, capsys):
    """End to end: what `adminkey create --quiet` prints is accepted by the coordinator."""
    written, out = _run_cli(monkeypatch, capsys, "create", quiet=True)
    real_limits = coordinator.get_worker_limits
    monkeypatch.setattr(coordinator, "get_worker_limits",
                        lambda: {**real_limits(), "admin_key_hash": written["ADMIN_KEY_HASH"]})
    assert coordinator._admin_key_matches(out.strip()) is True
    assert coordinator._admin_key_matches(out.strip() + "x") is False


def test_config_parses_the_admin_settings(monkeypatch):
    from engine import config

    monkeypatch.setattr(config, "get_config", lambda force_reload=False: {})
    limits = config.get_worker_limits()
    assert limits["admin_key_hash"] == "" and limits["admin_allow_remote"] is False

    monkeypatch.setattr(config, "get_config",
                        lambda force_reload=False: {"ADMIN_KEY_HASH": " ABC123 ", "ADMIN_ALLOW_REMOTE": "true"})
    limits = config.get_worker_limits()
    assert limits["admin_key_hash"] == "abc123" and limits["admin_allow_remote"] is True
