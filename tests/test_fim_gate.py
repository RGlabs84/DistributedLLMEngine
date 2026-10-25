"""
Autocomplete admission. Keystroke completions must never queue up, but one user's chat turn
must not switch ghost text off for everyone either: a completion runs when no other completion
is in flight and the engine has a request slot free.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from engine import coordinator
from engine.lease import lease_manager


@pytest.fixture(autouse=True)
def clean_leases():
    with lease_manager._lock:
        lease_manager._leases.clear()
    yield
    with lease_manager._lock:
        lease_manager._leases.clear()


@pytest.fixture
def engine_reached(monkeypatch):
    """Record whether a request got past the gate (to the engine start), and stop it there."""
    reached = []

    async def fake_start(model_path, *args, **kwargs):
        reached.append(model_path)
        return (False, 0, "stopped by the test")

    async def fake_estimate(_model):
        return 1.0

    monkeypatch.setattr(coordinator.manager, "astart_model", fake_start)
    monkeypatch.setattr(coordinator.manager, "aestimate_model_vram_gb", fake_estimate)
    monkeypatch.setattr(coordinator, "is_swapping_models", False)
    monkeypatch.setattr(coordinator, "is_recalculating_queue", False)
    return reached


async def _complete():
    transport = ASGITransport(app=coordinator.app, client=("127.0.0.1", 5000))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/v1/completions", json={"prompt": "def f(", "model": "m.gguf"})


def _busy(resp):
    return resp.status_code == 200 and resp.json()["choices"][0]["finish_reason"] == "busy"


@pytest.mark.anyio
async def test_an_idle_engine_serves_a_completion(engine_reached):
    resp = await _complete()
    assert not _busy(resp)
    assert engine_reached == ["m.gguf"]


@pytest.mark.anyio
async def test_one_chat_in_flight_no_longer_blocks_autocomplete(engine_reached):
    lease = lease_manager.acquire(task_type="chat", timeout=60.0)
    try:
        resp = await _complete()
    finally:
        lease.release()
    assert not _busy(resp)
    assert engine_reached == ["m.gguf"]


@pytest.mark.anyio
async def test_completions_never_stack(engine_reached):
    lease = lease_manager.acquire(task_type="completion", timeout=60.0)
    try:
        resp = await _complete()
    finally:
        lease.release()
    assert _busy(resp)
    assert engine_reached == []


@pytest.mark.anyio
async def test_no_completion_when_every_engine_slot_is_taken(engine_reached):
    leases = [lease_manager.acquire(task_type="chat", timeout=60.0) for _ in range(coordinator.FIM_ENGINE_SLOTS)]
    try:
        resp = await _complete()
    finally:
        for lease in leases:
            lease.release()
    assert _busy(resp)
    assert engine_reached == []


def test_lease_counts_can_be_filtered_by_type():
    a = lease_manager.acquire(task_type="chat", timeout=60.0)
    b = lease_manager.acquire(task_type="completion", timeout=60.0)
    try:
        assert lease_manager.active_count() == 2
        assert lease_manager.active_count(task_type="completion") == 1
        assert lease_manager.active_count(task_type="agent") == 0
    finally:
        a.release()
        b.release()


def test_the_coordinator_keeps_idle_connections_for_reuse(monkeypatch):
    captured = {}

    class Stop(Exception):
        pass

    def fake_config(app, **kwargs):
        captured.update(kwargs)
        raise Stop

    import uvicorn
    from engine import wireguard
    monkeypatch.setattr(uvicorn, "Config", fake_config)
    monkeypatch.setattr(coordinator, "udp_broadcaster", lambda port: None)
    monkeypatch.setattr(wireguard.WireGuardOrchestrator, "warn_if_interface_diverged", lambda self: None)
    with pytest.raises(Stop):
        coordinator.start_coordinator_server("127.0.0.1", 0)
    assert captured["timeout_keep_alive"] >= 60
