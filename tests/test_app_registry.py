"""
CPU pool members never reach the 14B pipeline (engine/registry.py).

A pool member runs a whole small model for phone requests and has no rpc-server, so its
address must never appear in llama-server's --rpc list and its RAM/cores must never be counted
in the 14B memory budget. registry.app_members() is the one place that lists them.
"""

import pytest

from engine.registry import WorkerRegistry, is_app_member

GPU = {"gpu": {"has_gpu": True, "vram_total_gb": 24.0}, "ram_gb": 32.0, "cpu_cores": 8, "cpu_threads": 16,
       "is_yielding": False, "rpc_proto": "3.0.0"}
CPU_MAIN = {"gpu": None, "ram_gb": 16.0, "cpu_cores": 6, "cpu_threads": 12, "is_yielding": False,
            "rpc_proto": "3.0.0"}


def app(ram, **extra):
    return {"gpu": None, "ram_gb": ram, "cpu_cores": 12, "cpu_threads": 24, "is_yielding": False,
            "rpc_proto": "3.0.0", "pool": "app", "app": {"model": "small", "ready": True, "slots": 2}, **extra}


@pytest.fixture
def registry():
    reg = WorkerRegistry()
    reg.update_worker("gpu", "10.0.0.1", 50052, "t1", GPU)
    reg.update_worker("cpu", "10.0.0.2", 50052, "t2", CPU_MAIN)
    reg.update_worker("app1", "10.0.0.3", 50053, "t3", app(64.0))
    reg.update_worker("app2", "10.0.0.4", 50053, "t4", app(32.0))
    # Even a member that also has a GPU stays out of the pipeline once it says it is a pool member.
    reg.update_worker("app-gpu", "10.0.0.5", 50053, "t5",
                      app(16.0, gpu={"has_gpu": True, "vram_total_gb": 12.0}))
    reg.update_worker("app-yield", "10.0.0.6", 50053, "t6", app(8.0, is_yielding=True))
    return reg


PIPELINE = ["10.0.0.1:50052", "10.0.0.2:50052"]


def test_is_app_member():
    assert is_app_member({"pool": "app"})
    for hw in ({}, {"pool": "rpc"}, {"pool": None}, None, "pool", ["pool"], {"app": {}}):
        assert not is_app_member(hw)


def test_endpoint_lists_never_name_a_pool_member(registry):
    assert registry.get_active_rpc_endpoints() == PIPELINE
    assert sorted(registry.plan_endpoints(1000.0, policy="always")) == PIPELINE
    assert set(registry.plan_endpoints(1000.0)) <= set(PIPELINE)
    assert set(registry.plan_endpoints(0.1)) <= set(PIPELINE)


def test_memory_and_cpu_totals_never_count_a_pool_member(registry):
    assert registry.get_total_vram_gb() == 24.0
    assert registry.get_total_sysram_gb() == 48.0
    assert registry.get_total_cpu_cores() == 14
    assert registry.get_total_cpu_threads() == 28


def test_a_matching_rpc_protocol_does_not_bring_a_member_back(registry):
    registry.required_rpc_major = 3
    assert registry.get_active_rpc_endpoints() == PIPELINE
    assert registry.get_total_sysram_gb() == 48.0
    assert not registry.rpc_compatible(app(64.0))
    assert registry.rpc_compatible(GPU)


def test_app_members_lists_only_live_unyielding_members(registry):
    found = registry.app_members()
    assert sorted(r["worker_id"] for r in found) == ["app-gpu", "app1", "app2"]
    for record in found:
        assert record["port"] == 50053 and record["hardware"]["pool"] == "app"


def test_app_members_are_copies(registry):
    registry.app_members()[0]["ip"] = "tampered"
    assert all(r["ip"] != "tampered" for r in registry.app_members())


def test_dead_members_are_pruned(registry):
    registry.workers["app1"]["last_heartbeat"] -= 3600
    assert sorted(r["worker_id"] for r in registry.app_members()) == ["app-gpu", "app2"]
    assert "app1" not in registry.workers


def test_a_registry_of_only_pool_members_has_an_empty_pipeline():
    reg = WorkerRegistry()
    reg.update_worker("app1", "10.0.0.3", 50053, "t3", app(64.0))
    assert reg.get_active_rpc_endpoints() == []
    assert reg.plan_endpoints(10.0) == []
    assert (reg.get_total_vram_gb(), reg.get_total_sysram_gb(), reg.get_total_cpu_cores(),
            reg.get_total_cpu_threads()) == (0.0, 0.0, 0, 0)
    assert [r["worker_id"] for r in reg.app_members()] == ["app1"]


def test_a_member_that_leaves_the_pool_rejoins_the_pipeline():
    reg = WorkerRegistry()
    reg.update_worker("w", "10.0.0.3", 50053, "t", app(16.0))
    assert reg.get_active_rpc_endpoints() == []
    reg.update_worker("w", "10.0.0.3", 50052, "t", CPU_MAIN)
    assert reg.get_active_rpc_endpoints() == ["10.0.0.3:50052"]
    assert reg.app_members() == []
