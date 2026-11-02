"""
The takeover-only key (2.8.0, owner 2026-11-02: "give Duka a permanent key, no exposure here"). TAKEOVER_KEY_HASH opens
/v1/admin/takeover* and nothing else, so DLNE-Sec's Duka never holds the operator key; `dlne_server.py takeoverkey create
--out FILE` mints it straight into a private file without showing it.
"""

import os
import stat

import pytest

from engine import api_keys, coordinator
from tests.takeover_fakes import POOL_A, box          # noqa: F401  (box is a fixture)
from tests.test_takeover_api import KEY, LAN, REMOTE, URL, client

TAKEOVER_KEY = "dlne_ak_duka_takeover_only_key_for_tests"


@pytest.fixture
def env(box, monkeypatch):
    real_limits = coordinator.get_worker_limits
    state = {"admin_key_hash": api_keys.hash_admin_key(KEY), "takeover_key_hash": api_keys.hash_admin_key(TAKEOVER_KEY),
             "admin_allow_remote": True, "trust_proxy_headers": False}
    monkeypatch.setattr(coordinator, "get_worker_limits", lambda: {**real_limits(), **state})
    monkeypatch.setattr(coordinator, "takeover_controller", box.controller())
    box.state = state
    return box


@pytest.mark.anyio
async def test_the_takeover_key_opens_the_takeover_endpoints_from_the_lan(env):
    async with client(peer=LAN, key=TAKEOVER_KEY) as c:
        assert (await c.get(URL)).status_code == 200
        assert (await c.post(f"{URL}/plan", json={"gpus": [POOL_A]})).status_code == 200


@pytest.mark.anyio
async def test_the_takeover_key_opens_no_other_admin_endpoint_even_with_remote_admin_on(env):
    async with client(peer=LAN, key=TAKEOVER_KEY) as c:
        for path in ("/v1/admin/stats", "/v1/admin/telemetry"):
            assert (await c.get(path)).status_code == 403, path
    async with client(peer=LAN, key=KEY) as c:
        assert (await c.get("/v1/admin/stats")).status_code == 200, "the operator key still opens them"


@pytest.mark.anyio
async def test_the_takeover_key_keeps_every_takeover_rule(env):
    async with client(peer=REMOTE, key=TAKEOVER_KEY) as c:
        r = await c.get(URL)
        assert r.status_code == 403 and r.json()["code"] == "takeover_forbidden", "not from outside the LAN"
    async with client(peer=LAN, key=TAKEOVER_KEY, headers={"X-Forwarded-For": "192.168.1.9"}) as c:
        assert (await c.get(URL)).status_code == 403, "never through a proxy"


@pytest.mark.anyio
async def test_with_no_takeover_key_set_only_the_operator_key_works(env):
    env.state["takeover_key_hash"] = ""
    async with client(peer=LAN, key=TAKEOVER_KEY) as c:
        assert (await c.get(URL)).status_code == 403
    async with client(peer=LAN, key=KEY) as c:
        assert (await c.get(URL)).status_code == 200


def _cli(monkeypatch, capsys, **kw):
    import dlne_server
    written = {}
    monkeypatch.setattr(dlne_server, "set_config_values", lambda updates: written.update(updates) or True)
    args = type("A", (), {"takeover_action": "create", "out": None, "quiet": False, **kw})()
    dlne_server.cmd_takeoverkey(args)
    return written, capsys.readouterr().out


def test_create_out_writes_the_key_to_a_private_file_and_never_prints_it(monkeypatch, capsys, tmp_path):
    out_file = tmp_path / "duka-takeover.key"
    written, out = _cli(monkeypatch, capsys, out=str(out_file))
    key = out_file.read_text()
    assert key.startswith("dlne_ak_") and key not in out, "the key is not shown"
    assert stat.S_IMODE(os.stat(out_file).st_mode) == 0o600
    assert written == {"TAKEOVER_KEY_HASH": api_keys.hash_admin_key(key)}
    assert "ADMIN_KEY_HASH" not in written, "the operator key is untouched"


def test_revoke_clears_only_the_takeover_key(monkeypatch, capsys):
    written, out = _cli(monkeypatch, capsys, takeover_action="revoke")
    assert written == {"TAKEOVER_KEY_HASH": ""} and "revoked" in out
