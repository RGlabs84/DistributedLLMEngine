import time
import pytest
from engine.lease import InferenceLeaseManager, lease_manager


def test_lease_acquire_and_release():
    mgr = InferenceLeaseManager()
    assert not mgr.is_locked()
    assert mgr.active_count() == 0

    lease = mgr.acquire(task_type="chat", timeout=10.0, client_id="vscode_user")
    assert mgr.is_locked()
    assert mgr.active_count() == 1

    summary = mgr.active_leases_summary()
    assert len(summary) == 1
    assert summary[0]["lease_id"] == lease.lease_id
    assert summary[0]["client_id"] == "vscode_user"

    lease.release()
    assert not mgr.is_locked()
    assert mgr.active_count() == 0


def test_lease_context_managers():
    mgr = InferenceLeaseManager()
    with mgr.acquire(task_type="edit", timeout=5.0) as lease:
        assert mgr.is_locked()
        assert lease.task_type == "edit"
    assert not mgr.is_locked()


@pytest.mark.anyio
async def test_lease_async_context_manager():
    mgr = InferenceLeaseManager()
    async with mgr.acquire(task_type="agent_tool", timeout=5.0) as lease:
        assert mgr.is_locked()
    assert not mgr.is_locked()


def test_lease_expiration():
    mgr = InferenceLeaseManager()
    lease = mgr.acquire(task_type="short", timeout=0.05)
    assert mgr.is_locked()
    time.sleep(0.08)
    assert not mgr.is_locked()
    assert mgr.active_count() == 0


@pytest.mark.anyio
async def test_wait_for_idle():
    mgr = InferenceLeaseManager()
    lease = mgr.acquire(task_type="stream", timeout=0.2)
    assert mgr.is_locked()
    idle = await mgr.wait_for_idle(timeout=1.0, poll_interval=0.05)
    assert idle is True
    assert not mgr.is_locked()

