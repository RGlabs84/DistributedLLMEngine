import json
import pytest
from httpx import AsyncClient, ASGITransport

from engine import coordinator
from engine.lease import lease_manager
from engine.api_keys import api_key_manager


@pytest.fixture
def test_app():
    return coordinator.app


@pytest.fixture(autouse=True)
def clean_leases_and_keys():
    with lease_manager._lock:
        lease_manager._leases.clear()
    yield
    with lease_manager._lock:
        lease_manager._leases.clear()


@pytest.mark.anyio
async def test_cluster_status_endpoint(test_app):
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Acquire a dummy lease to test status reporting
        lease = lease_manager.acquire(task_type="agent_test", timeout=60.0, client_id="test_client")
        try:
            resp = await client.get("/v1/cluster/status")
            assert resp.status_code == 200
            data = resp.json()
            assert "status" in data
            assert "active_leases_count" in data
            assert data["active_leases_count"] == 1
            assert data["is_locked_for_edits"] is True
            assert len(data["in_flight_leases"]) == 1
            assert data["in_flight_leases"][0]["task_type"] == "agent_test"
        finally:
            lease.release()

        # Check status after release
        resp2 = await client.get("/v1/cluster/status")
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert data2["active_leases_count"] == 0
        assert data2["is_locked_for_edits"] is False


@pytest.mark.anyio
async def test_the_public_status_names_no_address(test_app, monkeypatch):
    """
    Until 2.4.9 /v1/cluster/status, which anyone can read, listed every worker's ip:port
    (``active_endpoints``: a volunteer's home address) and the IP address of every caller with a
    request in flight (``in_flight_leases[].client_id``).
    """
    from engine.registry import WorkerRegistry
    registry = WorkerRegistry()
    registry.update_worker("home-gpu", "73.215.216.195", 50052, "tok-home",
                           {"gpu": {"has_gpu": True, "vram_total_gb": 8.0}, "ram_gb": 30.0})
    monkeypatch.setattr(coordinator, "registry", registry)
    lease = lease_manager.acquire(task_type="chat", timeout=60.0, client_id="203.0.113.44")
    try:
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
            data = (await client.get("/v1/cluster/status")).json()
    finally:
        lease.release()
    assert data["active_workers_count"] == 1
    assert "active_endpoints" not in data
    assert data["in_flight_leases"] == [{"task_type": "chat", "elapsed_seconds": data["in_flight_leases"][0]["elapsed_seconds"],
                                         "remaining_seconds": data["in_flight_leases"][0]["remaining_seconds"]}]
    text = json.dumps(data)
    for needle in ("73.215.216.195", "50052", "203.0.113.44", "tok-home", "home-gpu"):
        assert needle not in text, needle


@pytest.mark.anyio
async def test_admin_api_keys_crud(test_app, monkeypatch):
    from engine.api_keys import hash_admin_key

    operator_key = "dlne_ak_crud_test_key"
    real_limits = coordinator.get_worker_limits
    monkeypatch.setattr(coordinator, "get_worker_limits",
                        lambda: {**real_limits(), "admin_key_hash": hash_admin_key(operator_key)})
    admin = {"Authorization": f"Bearer {operator_key}"}

    # The key routes need the operator key even from the coordinator's own host.
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.post("/v1/admin/apikeys", json={"name": "x"})).status_code == 403
        assert (await client.get("/v1/admin/apikeys")).status_code == 403
        assert (await client.delete("/v1/admin/apikeys/anything")).status_code == 403

        # 1. Create a key
        create_resp = await client.post("/v1/admin/apikeys", json={"name": "vscode-extension"}, headers=admin)
        assert create_resp.status_code == 200
        key_data = create_resp.json()
        assert "api_key" in key_data
        raw_key = key_data["api_key"]
        key_id = key_data["key_info"]["id"]
        assert raw_key.startswith("dlne_sk_")

        # 2. List keys
        list_resp = await client.get("/v1/admin/apikeys", headers=admin)
        assert list_resp.status_code == 200
        keys = list_resp.json()["keys"]
        matching = [k for k in keys if k["id"] == key_id]
        assert len(matching) == 1
        assert matching[0]["name"] == "vscode-extension"
        assert not matching[0]["revoked"]

        # 3. Use the Bearer key to access protected endpoints
        auth_headers = {"Authorization": f"Bearer {raw_key}"}
        status_resp = await client.get("/v1/cluster/status", headers=auth_headers)
        assert status_resp.status_code == 200

        # 4. Revoke the key
        revoke_resp = await client.delete(f"/v1/admin/apikeys/{key_id}", headers=admin)
        assert revoke_resp.status_code == 200
        assert revoke_resp.json()["status"] == "revoked"

        # 5. Revoking again returns 404
        revoke_again = await client.delete(f"/v1/admin/apikeys/{key_id}", headers=admin)
        assert revoke_again.status_code == 404


def test_zero_rework_topology_with_lease_and_policy(monkeypatch):
    """
    Verifies that:
    1. An active lease blocks topology gains from rebuilding the engine.
    2. Zero-rework policy (rebalance_on_join=False) keeps running model undisturbed.
    3. Dropping an active worker still triggers immediate rebuild for recovery.
    """
    monkeypatch.setattr(coordinator, "_pending_topology_since", 0.0)
    monkeypatch.setattr(
        coordinator.manager.__class__,
        "current_endpoints",
        property(lambda self: ["127.0.0.1:50052"]),
    )

    # Case 1: Active node dies -> always returns True immediately
    assert coordinator._topology_change_is_settled([]) is True

    # Case 2: New node joins, but active lease is held -> returns False (no mid-edit rebuild)
    with lease_manager.acquire(task_type="chat"):
        monkeypatch.setattr(coordinator, "get_worker_limits",
                           lambda: {"topology_settle_seconds": 0, "shard_policy": "auto", "rebalance_on_join": True})
        assert coordinator._topology_change_is_settled(["127.0.0.1:50052", "10.8.0.4:50052"]) is False

    # Case 3: Zero-rework policy enabled (rebalance_on_join=False) -> returns False
    monkeypatch.setattr(coordinator, "get_worker_limits",
                       lambda: {"topology_settle_seconds": 0, "shard_policy": "auto", "rebalance_on_join": False})
    assert coordinator._topology_change_is_settled(["127.0.0.1:50052", "10.8.0.4:50052"]) is False


@pytest.mark.anyio
async def test_completions_endpoint_validation(test_app):
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Missing prompt
        resp = await client.post("/v1/completions", json={"model": "test-model"})
        assert resp.status_code == 400
        assert "No prompt specified" in resp.json()["detail"]

        # Invalid JSON
        resp2 = await client.post("/v1/completions", content="not json", headers={"Content-Type": "application/json"})
        assert resp2.status_code == 400
