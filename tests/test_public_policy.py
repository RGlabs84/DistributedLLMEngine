"""
The status carries the rules the websites state (2.4.8): the free trial, what a CPU contributor gets back and what a
machine needs to join, read from the limits the coordinator enforces. The owner's rule is that pages read figures
live; before this they typed "100 free chats" and "300 chats every 5 hours".
"""

import pytest
from httpx import ASGITransport, AsyncClient

import engine.coordinator as coordinator
from engine.coding_member_select import MIN_VRAM_GB


def _client():
    transport = ASGITransport(app=coordinator.app, client=("127.0.0.1", 5000))
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.anyio
async def test_the_status_states_the_rules_the_coordinator_enforces(monkeypatch):
    limits = {"starter_quota_inferences": 100, "cpu_coding_limit": 300, "cpu_coding_window_hours": 5.0,
              "cpu_min_ram_gb": 8.0, "cpu_min_cores": 4, "cpu_min_bandwidth_gbs": 30.0}
    monkeypatch.setattr(coordinator, "get_worker_limits", lambda: dict(limits))
    async with _client() as client:
        policy = (await client.get("/v1/cluster/status")).json()["policy"]
    assert policy == {
        "trial_chats": 100, "cpu_chats": 300, "cpu_window_hours": 5.0,
        "cpu_min_ram_gb": 8.0, "cpu_min_cores": 4, "cpu_min_bandwidth_gbs": 30.0,
        "coding_member_min_vram_gb": float(MIN_VRAM_GB),
    }


@pytest.mark.anyio
async def test_an_owner_who_changes_a_limit_changes_what_the_pages_say(monkeypatch):
    monkeypatch.setattr(coordinator, "get_worker_limits",
                        lambda: {"starter_quota_inferences": 250, "cpu_coding_limit": 120, "cpu_coding_window_hours": 2.5})
    async with _client() as client:
        policy = (await client.get("/v1/cluster/status")).json()["policy"]
    assert policy["trial_chats"] == 250 and policy["cpu_chats"] == 120 and policy["cpu_window_hours"] == 2.5
    # Limits that are not set fall back exactly as the enforcing code does.
    assert policy["cpu_min_ram_gb"] == 8.0 and policy["cpu_min_cores"] == 4 and policy["cpu_min_bandwidth_gbs"] == 30.0


def test_the_policy_holds_numbers_only():
    policy = coordinator._public_policy()
    assert all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in policy.values())
