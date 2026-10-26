"""
Who may choose the coding model (2.3.6). A public caller's request runs on the model the swarm
is serving, whatever its body names, and only the operator may swap the cluster's model: on
launch night a free-trial caller could do either, and one request naming another model would
have swapped the live one out for everybody.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from engine import coordinator
from engine.lease import lease_manager

PUBLIC = ("203.0.113.9", 5000)
HOST = ("127.0.0.1", 5000)
CHAT = {"messages": [{"role": "user", "content": "hi"}], "model": "other-model.gguf"}


@pytest.fixture(autouse=True)
def clean_leases():
    with lease_manager._lock:
        lease_manager._leases.clear()
    yield
    with lease_manager._lock:
        lease_manager._leases.clear()


@pytest.fixture
def engine(monkeypatch):
    """The model each request reached the engine start with, and any swap that began."""
    seen = {"started": [], "stopped": 0}

    async def fake_start(model_path, *args, **kwargs):
        seen["started"].append(model_path)
        return (False, 0, "stopped by the test")

    async def fake_estimate(_model):
        return 1.0

    async def fake_stop_all(*args, **kwargs):
        seen["stopped"] += 1

    monkeypatch.setattr(coordinator.manager, "astart_model", fake_start)
    monkeypatch.setattr(coordinator.manager, "aestimate_model_vram_gb", fake_estimate)
    monkeypatch.setattr(coordinator.manager, "astop_all_models", fake_stop_all)
    monkeypatch.setattr(coordinator, "is_swapping_models", False)
    monkeypatch.setattr(coordinator, "is_recalculating_queue", False)
    monkeypatch.setattr(coordinator, "global_active_model", "live-model.gguf")
    # A public caller with free chats left (the trial tier), without touching any quota file.
    monkeypatch.setattr(coordinator, "use_starter_quota", lambda *args, **kwargs: (True, 99))
    return seen


async def _post(client_addr, path, body):
    transport = ASGITransport(app=coordinator.app, client=client_addr)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, json=body)


@pytest.mark.anyio
async def test_a_public_chat_runs_on_the_live_model_whatever_it_names(engine):
    await _post(PUBLIC, "/v1/chat/completions", CHAT)
    assert engine["started"] == ["live-model.gguf"]


@pytest.mark.anyio
async def test_a_public_completion_runs_on_the_live_model_whatever_it_names(engine):
    await _post(PUBLIC, "/v1/completions", {"prompt": "def f(", "model": "other-model.gguf"})
    assert engine["started"] == ["live-model.gguf"]


@pytest.mark.anyio
async def test_this_machine_may_still_name_a_model(engine):
    await _post(HOST, "/v1/chat/completions", CHAT)
    assert engine["started"] == ["other-model.gguf"]


@pytest.mark.anyio
async def test_a_public_caller_cannot_swap_the_cluster_model(engine):
    resp = await _post(PUBLIC, "/v1/cluster/swap", {"model": "other-model.gguf"})
    assert resp.status_code == 403
    assert engine["stopped"] == 0 and engine["started"] == []


@pytest.mark.anyio
async def test_the_operator_reaches_the_swap(engine):
    resp = await _post(HOST, "/v1/cluster/swap", {"model": ""})
    assert resp.status_code == 400, "past the gate, refused only for the missing name"
