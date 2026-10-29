"""
The status carries the rules the websites state (2.4.8): the free trial, what a CPU contributor gets back and what a
machine needs to join, read from the limits the coordinator enforces. The owner's rule is that pages read figures
live; before this they typed "100 free chats" and "300 chats every 5 hours".
"""

from types import SimpleNamespace

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
        "coding_context_tokens": 32768,        # nothing running, no members: the default coding member's 65,536 / 2
        "shared_max_rtt_ms": 80.0, "coding_member_max_rtt_ms": 120.0,    # the owner's limits (defaults)
        "image_input": None,                   # null while the engine cannot read images
    }


@pytest.mark.anyio
async def test_the_rtt_limits_the_pages_show_are_the_ones_enforced(monkeypatch):
    monkeypatch.setattr(coordinator, "get_worker_limits",
                        lambda: {"auto_swap_max_rtt_ms": 40.0, "coding_member_max_rtt_ms": 150.0})
    async with _client() as client:
        policy = (await client.get("/v1/cluster/status")).json()["policy"]
    assert policy["shared_max_rtt_ms"] == 40.0 and policy["coding_member_max_rtt_ms"] == 150.0


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


# --- coding_context_tokens (2.4.9, docs/agent-memory.md section 9.2) -----------------------------------------------

def _members(monkeypatch, *members, enabled=True, tiers=None):
    """Make the coordinator see these ready coding members (tier id, slots) and this tier ladder."""
    limits = {"coding_members_enabled": enabled}
    monkeypatch.setattr(coordinator, "get_worker_limits", lambda: dict(limits))
    monkeypatch.setattr(coordinator, "_coding_members", lambda _limits=None: list(members))
    monkeypatch.setattr(coordinator, "_coding_ladder", lambda: tiers if tiers is not None else [])


def _member(tier="big", slots=2):
    return SimpleNamespace(tier=tier, slots=slots)


def test_with_nothing_known_it_is_the_default_members_per_request_context(monkeypatch):
    _members(monkeypatch)
    assert coordinator._public_policy()["coding_context_tokens"] == 32768


def test_it_is_the_engines_context_per_slot(monkeypatch):
    _members(monkeypatch)
    # TheWall: 5 slots of 32,768 tokens each
    assert coordinator._public_policy(engine_slot_context=32768)["coding_context_tokens"] == 32768
    assert coordinator._public_policy(engine_slot_context=24000)["coding_context_tokens"] == 24000
    # A running engine is what is known, so its figure stands even above the default.
    assert coordinator._public_policy(engine_slot_context=131072)["coding_context_tokens"] == 131072


def test_it_is_the_smallest_among_the_engine_and_every_ready_member(monkeypatch):
    tiers = [{"id": "big", "context": 65536}, {"id": "small", "context": 16384}]
    _members(monkeypatch, _member("big", 2), _member("small", 2), tiers=tiers)
    assert coordinator._public_policy(engine_slot_context=32768)["coding_context_tokens"] == 8192      # 16384 / 2
    _members(monkeypatch, _member("big", 2), tiers=tiers)
    assert coordinator._public_policy(engine_slot_context=20000)["coding_context_tokens"] == 20000
    assert coordinator._public_policy(engine_slot_context=40000)["coding_context_tokens"] == 32768


def test_a_member_is_worked_out_from_its_tier_not_from_the_slots_it_reports(monkeypatch):
    """The context per request is the tier's context over the tier's server_slots (2.5.0): a member the coordinator
    asked to open one slot still gives each request the same context, so the slots it reports do not enter into it."""
    tiers = [{"id": "wide", "context": 81920, "server_slots": 2}, {"id": "odd", "context": 65537}, {"id": "none"}]
    for slots in (1, 2, 0):
        _members(monkeypatch, _member("wide", slots), tiers=tiers)
        assert coordinator._public_policy()["coding_context_tokens"] == 40960
    _members(monkeypatch, _member("odd", 2), tiers=tiers)
    assert coordinator._public_policy()["coding_context_tokens"] == 32768          # 65537 over the default 2 slots
    _members(monkeypatch, _member("none", 4), _member("unknown-tier", 2), tiers=tiers)
    assert coordinator._public_policy()["coding_context_tokens"] == 32768          # default 65536 / 2


def test_members_do_not_count_while_they_are_switched_off(monkeypatch):
    _members(monkeypatch, _member("small", 8), enabled=False, tiers=[{"id": "small", "context": 16384}])
    assert coordinator._public_policy()["coding_context_tokens"] == 32768


def test_a_registry_that_cannot_be_read_does_not_break_the_status(monkeypatch):
    _members(monkeypatch)

    def broken(_limits=None):
        raise RuntimeError("registry")

    monkeypatch.setattr(coordinator, "_coding_members", broken)
    assert coordinator._public_policy(engine_slot_context=30000)["coding_context_tokens"] == 30000


@pytest.mark.anyio
async def test_the_running_engines_slot_context_is_its_context_over_its_slots(monkeypatch):
    async def snapshot(_name):
        return {"safe_context": 163840, "dynamic_slots": 5}

    monkeypatch.setattr(coordinator.manager, "asnapshot_model", snapshot)
    assert await coordinator._engine_slot_context(["m.gguf"]) == 32768
    assert await coordinator._engine_slot_context([]) is None

    async def gone(_name):
        return None

    monkeypatch.setattr(coordinator.manager, "asnapshot_model", gone)
    assert await coordinator._engine_slot_context(["m.gguf"]) is None
