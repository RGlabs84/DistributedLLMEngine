"""
Tests for the takeover endpoints (2.7.1, /v1/admin/takeover*, docs/takeover-contract.md): who may call them (the operator
key AND a LAN/WireGuard socket peer, never through a proxy, whatever ADMIN_ALLOW_REMOTE says), the contract's answers and
codes for plan, grant, heartbeat, release and GET, and what /v1/cluster/status and the admin stats show.

The coordinator is the real ASGI app with a chosen client address; the controller behind it is given the stand-ins of
tests/takeover_fakes.py, so no engine, card or systemd unit is touched.
"""

import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient

from engine import api_keys, coordinator, takeover
from tests.takeover_fakes import MEDIA, POOL_A, POOL_B, box          # noqa: F401  (box is a fixture)

KEY = "dlne_ak_test_key_for_unit_tests"
LOOPBACK, LAN, WIREGUARD = ("127.0.0.1", 40000), ("192.168.1.20", 40000), ("10.8.0.7", 40000)
REMOTE = ("203.0.113.50", 40000)       # TEST-NET-3: nobody's LAN

URL = "/v1/admin/takeover"


@pytest.fixture
def env(box, monkeypatch):
    """The operator key configured, a controller on the stand-ins in the coordinator's place, remote admin off."""
    real_limits = coordinator.get_worker_limits
    state = {"admin_key_hash": api_keys.hash_admin_key(KEY), "admin_allow_remote": False, "trust_proxy_headers": False}
    monkeypatch.setattr(coordinator, "get_worker_limits", lambda: {**real_limits(), **state})
    controller = box.controller()
    monkeypatch.setattr(coordinator, "takeover_controller", controller)
    box.state, box.controller_under_test = state, controller
    return box


def client(peer=LOOPBACK, key=KEY, headers=None):
    sent = dict(headers or {})
    if key is not None:
        sent["Authorization"] = f"Bearer {key}"
    return AsyncClient(transport=ASGITransport(app=coordinator.app, client=peer), base_url="http://test", headers=sent)


ALL_CALLS = [("post", f"{URL}/plan"), ("post", URL), ("get", URL), ("put", f"{URL}/tk_x"), ("delete", f"{URL}/tk_x")]


async def call(c, method, path, **kwargs):
    if method == "post":
        kwargs.setdefault("json", {"gpus": [POOL_A]})
    return await getattr(c, method)(path, **kwargs)


# ======================================================================================
# Who may ask
# ======================================================================================

@pytest.mark.anyio
@pytest.mark.parametrize("method,path", ALL_CALLS)
@pytest.mark.parametrize("peer", [LOOPBACK, LAN, WIREGUARD])
async def test_the_operator_key_from_a_lan_or_wireguard_peer_gets_through_the_gate(env, method, path, peer):
    async with client(peer) as c:
        resp = await call(c, method, path)
    assert resp.status_code != 403, resp.text            # 200 or the answer to the call itself (404 for the made-up lease id)


@pytest.mark.anyio
@pytest.mark.parametrize("method,path", ALL_CALLS)
async def test_no_key_a_wrong_key_or_an_empty_one_is_takeover_forbidden(env, method, path):
    for key in (None, "wrong", KEY + "x", ""):
        async with client(key=key) as c:
            resp = await call(c, method, path)
        assert resp.status_code == 403 and resp.json()["code"] == "takeover_forbidden" and resp.json()["detail"], key


@pytest.mark.anyio
@pytest.mark.parametrize("method,path", ALL_CALLS)
async def test_a_peer_outside_the_lan_is_refused_even_with_the_key_and_with_remote_admin_on(env, method, path):
    for remote_allowed in (False, True):
        env.state["admin_allow_remote"] = remote_allowed
        async with client(REMOTE) as c:
            resp = await call(c, method, path)
        assert resp.status_code == 403 and resp.json()["code"] == "takeover_forbidden"
    async with client(REMOTE) as c:
        assert (await c.get("/v1/admin/stats")).status_code == 200, "ADMIN_ALLOW_REMOTE still opens the ordinary admin API"


@pytest.mark.anyio
@pytest.mark.parametrize("header", ["X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "Forwarded", "Via", "X-Real-IP"])
@pytest.mark.parametrize("method,path", ALL_CALLS)
async def test_anything_that_came_through_a_proxy_is_refused(env, method, path, header):
    """The HTTPS proxy sits on loopback and adds these, so its peer address alone would look like a LAN host."""
    for peer in (LOOPBACK, LAN):
        async with client(peer, headers={header: "198.51.100.9"}) as c:
            resp = await call(c, method, path)
        assert resp.status_code == 403 and resp.json()["code"] == "takeover_forbidden", (header, peer)


@pytest.mark.anyio
async def test_trusting_proxy_headers_does_not_make_a_forwarded_address_the_peer(env):
    """get_client_ip would believe X-Forwarded-For under TRUST_PROXY_HEADERS; this gate looks at the socket and refuses the header."""
    env.state["trust_proxy_headers"] = True
    async with client(REMOTE, headers={"X-Forwarded-For": "192.168.1.5"}) as c:
        assert (await c.get(URL)).status_code == 403
    async with client(LOOPBACK, headers={"X-Forwarded-For": "192.168.1.5"}) as c:
        assert (await c.get(URL)).status_code == 403


@pytest.mark.anyio
async def test_a_refused_request_changes_nothing(env):
    async with client(REMOTE) as c:
        await c.post(URL, json={"gpus": [MEDIA]})
    async with client(LOOPBACK, key="wrong") as c:
        await c.post(URL, json={"gpus": [MEDIA]})

    assert takeover.load_lease() is None and env.restarts == [] and env.idle_calls == 0


# ======================================================================================
# The calls
# ======================================================================================

@pytest.mark.anyio
async def test_plan_is_a_dry_run_that_answers_the_contracts_shape(env):
    async with client() as c:
        resp = await c.post(f"{URL}/plan", json={"gpus": [POOL_A, MEDIA], "holder": "DLNE-Sec on portoBOX"})

    body = resp.json()
    assert resp.status_code == 200 and set(body) == {"allowed", "problems", "cards", "keep_result", "cpu_cores", "ram_gb"}
    assert body["allowed"] is True and body["problems"] == []
    assert [c["action"] for c in body["cards"]] == ["handover", "pictures-pause+vision-cpu"]
    assert set(body["cards"][0]) == {"pci", "vulkan_index", "name", "vram_gb", "free_vram_gb", "action"}
    assert body["keep_result"]["engine_restart"] is True and body["keep_result"]["pictures"] == "paused"
    assert takeover.load_lease() is None and env.restarts == [] and env.pictures_stopped == 0


@pytest.mark.anyio
async def test_plan_says_why_when_the_answer_is_no_and_still_answers_200(env):
    async with client() as c:
        resp = await c.post(f"{URL}/plan", json={"gpus": ["0000:ff:00.0"]})

    body = resp.json()
    assert resp.status_code == 200 and body["allowed"] is False and body["cards"] == []
    assert body["problems"][0]["code"] == "takeover_unknown_gpu" and body["problems"][0]["detail"]


@pytest.mark.anyio
async def test_grant_heartbeat_get_and_release_follow_the_contract(env):
    async with client() as c:
        granted = await c.post(URL, json={"gpus": [POOL_A, MEDIA], "holder": "DLNE-Sec on portoBOX", "reason": "security scan session"})
        lease = granted.json()
        assert granted.status_code == 200 and lease["state"] == "active" and lease["active"] is True
        assert set(lease) == {"active", "lease_id", "state", "holder", "granted", "dlne_now", "since", "expires_at",
                              "heartbeat_every_seconds", "cards"}
        assert lease["lease_id"].startswith("tk_") and lease["heartbeat_every_seconds"] == 15
        lease_id = lease["lease_id"]

        env.clock.t += 15
        beat = await c.put(f"{URL}/{lease_id}")
        assert beat.status_code == 200 and beat.json()["lease_id"] == lease_id and beat.json()["state"] == "active"

        current = (await c.get(URL)).json()
        assert current["lease_id"] == lease_id and current["state"] == "active" and len(current["cards"]) == 2

        env.restart_gate = asyncio.Event()           # the restore's engine restart is slow: GET sees it meanwhile
        released = await c.delete(f"{URL}/{lease_id}")
        assert released.status_code == 200 and released.json()["state"] == "restoring"
        assert (await c.get(URL)).json()["state"] == "restoring"
        env.restart_gate.set()
        await env.controller_under_test._restore_task
        assert (await c.get(URL)).json() == {"active": False}
        assert (await c.put(f"{URL}/{lease_id}")).status_code == 404


@pytest.mark.anyio
async def test_a_second_grant_is_409_takeover_active(env):
    async with client() as c:
        await c.post(URL, json={"gpus": [POOL_A]})
        resp = await c.post(URL, json={"gpus": [MEDIA]})
    assert resp.status_code == 409 and resp.json()["code"] == "takeover_active" and resp.json()["detail"]


@pytest.mark.anyio
async def test_an_unknown_card_is_400_takeover_unknown_gpu(env):
    async with client() as c:
        resp = await c.post(URL, json={"gpus": ["0000:ff:00.0"]})
    assert resp.status_code == 400 and resp.json()["code"] == "takeover_unknown_gpu"


@pytest.mark.anyio
async def test_a_floor_that_cannot_fit_is_400_takeover_too_much(env):
    async with client() as c:
        resp = await c.post(URL, json={"gpus": [POOL_B], "keep": {"coding_lanes": 8, "coding_context": 1048576}})
    assert resp.status_code == 400 and resp.json()["code"] == "takeover_too_much"


@pytest.mark.anyio
async def test_a_grant_that_never_gets_an_idle_moment_is_503_takeover_busy(env):
    env.idle_default = False
    async with client() as c:
        resp = await c.post(URL, json={"gpus": [POOL_B]})

    assert resp.status_code == 503 and resp.json()["code"] == "takeover_busy" and takeover.load_lease() is None
    assert env.restarts == []


@pytest.mark.anyio
async def test_a_body_that_is_not_the_contracts_shape_is_a_400_with_a_code(env):
    async with client() as c:
        assert (await c.post(URL, content=b"{not json")).json()["code"] == "takeover_bad_request"
        assert (await c.post(f"{URL}/plan", json=[1, 2])).status_code == 400
        resp = await c.post(URL, json={"gpus": [POOL_A], "ttl_seconds": 1})
    assert resp.status_code == 400 and resp.json()["code"] == "takeover_bad_request"


@pytest.mark.anyio
async def test_a_heartbeat_or_release_for_a_lease_that_is_not_there_is_404(env):
    async with client() as c:
        for resp in (await c.put(f"{URL}/tk_nothing"), await c.delete(f"{URL}/tk_nothing"), await c.put(f"{URL}/tk_é")):
            assert resp.status_code == 404 and resp.json()["code"] == "takeover_not_found"


@pytest.mark.anyio
async def test_get_with_no_lease_is_active_false(env):
    async with client() as c:
        resp = await c.get(URL)
    assert resp.status_code == 200 and resp.json() == {"active": False}


# ======================================================================================
# What the status and the admin stats show
# ======================================================================================

@pytest.mark.anyio
async def test_the_admin_stats_show_the_lease_without_its_id_or_a_card(env):
    async with client() as c:
        assert (await c.get("/v1/admin/stats")).json()["takeover"] == {
            "active": False, "state": None, "holder": None, "since": None, "expires_at": None}
        lease = (await c.post(URL, json={"gpus": [POOL_A], "holder": "DLNE-Sec on portoBOX"})).json()
        block = (await c.get("/v1/admin/stats")).json()["takeover"]

    assert block == {"active": True, "state": "active", "holder": "DLNE-Sec on portoBOX", "since": lease["since"],
                     "expires_at": lease["expires_at"]}
    assert lease["lease_id"] not in json.dumps(block)


@pytest.mark.anyio
async def test_cluster_status_has_the_takeover_block_and_no_id_or_address(env, monkeypatch):
    async with client() as c:
        before = (await c.get("/v1/cluster/status")).json()["takeover"]
        lease = (await c.post(URL, json={"gpus": [POOL_A], "holder": "DLNE-Sec on portoBOX"})).json()
        # The public status is read by anyone: no key, a remote address.
        async with client(REMOTE, key=None) as public:
            body = (await public.get("/v1/cluster/status")).json()

    assert before == {"active": False, "state": None, "holder": None, "since": None, "expires_at": None}
    assert body["takeover"] == {"active": True, "state": "active", "holder": "DLNE-Sec on portoBOX", "since": lease["since"],
                                "expires_at": lease["expires_at"]}
    text = json.dumps(body["takeover"])
    assert lease["lease_id"] not in text and POOL_A not in text and "tk_" not in text


@pytest.mark.anyio
async def test_the_status_shows_restoring_after_a_release_and_nothing_after_the_restore(env):
    async with client() as c:
        lease = (await c.post(URL, json={"gpus": [MEDIA]})).json()
        env.restart_gate = asyncio.Event()
        await c.delete(f"{URL}/{lease['lease_id']}")
        assert (await c.get("/v1/cluster/status")).json()["takeover"]["state"] == "restoring"
        env.restart_gate.set()
        await env.controller_under_test._restore_task
        assert (await c.get("/v1/cluster/status")).json()["takeover"]["active"] is False


# ======================================================================================
# The coordinator's side of the controller (the switch, the idle test)
# ======================================================================================

@pytest.fixture
def switch(monkeypatch):
    """_swap_model_core replaced by a recorder, and a model being served."""
    calls = []

    async def core(target, req_gb, total_vram, total_sysram, **options):
        calls.append((target, req_gb, total_vram, total_sysram, options))
        return {"outcome": "swapped", "success": True, "detail": ""}

    async def estimate(tag):
        return 6.0

    monkeypatch.setattr(coordinator, "_swap_model_core", core)
    monkeypatch.setattr(coordinator.manager, "aestimate_model_vram_gb", estimate)
    monkeypatch.setattr(coordinator.registry, "get_total_vram_gb", lambda: 16.0)
    monkeypatch.setattr(coordinator.registry, "get_total_sysram_gb", lambda: 32.0)
    monkeypatch.setattr(coordinator, "global_active_model", "Qwen3.5-9B-Q4_K_M.gguf")
    return calls


@pytest.mark.anyio
async def test_a_grants_restart_is_the_trusted_switch_of_the_same_model_called_off_if_busy(switch):
    result = await coordinator._takeover_restart_engine(expected_pool=(8.0, 30.0), reduced=True, patient=False)

    assert result["success"] is True
    target, req_gb, vram, sysram, options = switch[0]
    assert target == coordinator.canonical_model_tag("Qwen3.5-9B-Q4_K_M.gguf") and req_gb == 6.0
    assert (vram, sysram) == (8.0, 30.0), "the smaller pool: the registry has not heard of the lease yet"
    assert options["hold_first"] is True and options["abort_if_busy"] is True and options["rollback_model"] == target
    assert options["ready_timeout"] == coordinator._auto_swap_load_timeout() and options["drain_timeout"] == 15.0


@pytest.mark.anyio
async def test_a_restores_restart_is_patient_and_uses_the_pool_from_before_the_lease(switch, monkeypatch):
    monkeypatch.setattr(coordinator.registry, "get_total_vram_gb", lambda: 8.0)      # the registry still says "reduced"
    await coordinator._takeover_restart_engine(expected_pool=(16.0, 32.0), reduced=False, patient=True)

    _target, _req, vram, sysram, options = switch[0]
    assert (vram, sysram) == (16.0, 32.0)
    assert options["hold_first"] is True and options["abort_if_busy"] is False and options["drain_timeout"] >= 15.0


@pytest.mark.anyio
async def test_with_no_model_being_served_there_is_nothing_to_restart(switch, monkeypatch):
    monkeypatch.setattr(coordinator, "global_active_model", "")
    result = await coordinator._takeover_restart_engine(expected_pool=None)

    assert result["success"] is False and switch == []


def test_the_engine_is_idle_only_when_no_switch_request_or_picture_is_running(monkeypatch):
    monkeypatch.setattr(coordinator, "_coding_reconfiguration_running", lambda: False)
    monkeypatch.setattr(coordinator, "_coding_queue_busy", lambda: False)
    assert coordinator._takeover_idle() is True

    monkeypatch.setattr(coordinator, "_coding_queue_busy", lambda: True)
    assert coordinator._takeover_idle() is False
    monkeypatch.setattr(coordinator, "_coding_queue_busy", lambda: False)
    monkeypatch.setattr(coordinator, "_coding_reconfiguration_running", lambda: True)
    assert coordinator._takeover_idle() is False
    monkeypatch.setattr(coordinator, "_coding_reconfiguration_running", lambda: False)
    monkeypatch.setattr(coordinator.image_service, "active", object())
    assert coordinator._takeover_idle() is False
