"""
What a node reports so that the website can count GPUs and CPU threads (``gpu.count``,
``app.threads``), how the coordinator validates it, and the admission pool's VRAM total, which
counts only the sharded RPC pool.
"""

import pytest

from engine import authn, hardware
from engine.app_member import AppMember
from engine.registry import WorkerRegistry


def cards(*vram):
    return [{"id": i, "name": f"RTX {i}", "vram_total_gb": v, "vram_free_gb": v, "load_percent": 0.0, "integrated": False}
            for i, v in enumerate(vram)]


@pytest.mark.parametrize("sizes", [(24.0,), (8.0, 8.0), (24.0, 12.0, 8.0, 8.0)])
def test_the_gpu_section_says_how_many_gpus_make_up_the_total(monkeypatch, sizes):
    monkeypatch.setattr(hardware, "get_all_gpus", lambda: cards(*sizes))
    monkeypatch.setattr(hardware, "get_worker_limits", lambda: {"allowed_gpu_indices": "-1", "max_vram_gb": "-1"})
    gpu = hardware.get_gpu_info()
    assert gpu["count"] == len(sizes) and gpu["vram_total_gb"] == sum(sizes) and gpu["has_gpu"] is True


def test_a_vram_limit_does_not_change_the_number_of_gpus(monkeypatch):
    monkeypatch.setattr(hardware, "get_all_gpus", lambda: cards(24.0, 24.0))
    monkeypatch.setattr(hardware, "get_worker_limits", lambda: {"allowed_gpu_indices": "-1", "max_vram_gb": "30"})
    gpu = hardware.get_gpu_info()
    assert gpu["count"] == 2 and gpu["vram_total_gb"] == 30.0


def test_the_app_members_status_says_the_threads_it_really_runs_with(tmp_path):
    assert AppMember(str(tmp_path), lambda: [], 6).status()["threads"] == 6
    assert AppMember(str(tmp_path), lambda: [], 0).status()["threads"] == 1, "never fewer than one"


BASE = {"ram_gb": 16.0, "cpu_cores": 8, "cpu_threads": 16}


def valid(hw):
    return authn.validate_hardware_profile(hw)[0]


@pytest.mark.parametrize("count, ok", [(0, True), (1, True), (4, True), (64, True), (65, False), (-1, False),
                                       (True, False), (1.0, False), ("2", False), (None, False)])
def test_the_gpu_count_is_a_small_whole_number(count, ok):
    gpu = {"has_gpu": True, "vram_total_gb": 24.0, "name": "RTX", "count": count}
    assert valid({**BASE, "gpu": gpu}) is ok
    assert valid({**BASE, "gpu": {"has_gpu": True, "vram_total_gb": 24.0, "name": "RTX"}}), "an old worker omits it"


@pytest.mark.parametrize("threads, ok", [(1, True), (6, True), (256, True), (0, False), (257, False), (-2, False),
                                         (True, False), (4.0, False), ("4", False), (None, True)])
def test_the_app_members_threads_are_a_small_whole_number(threads, ok):
    app = {"model": "m", "ready": True, "slots": 2, "threads": threads}
    assert valid({**BASE, "gpu": None, "pool": "app", "app": app}) is ok
    assert valid({**BASE, "gpu": None, "pool": "app", "app": {"model": "m", "ready": True, "slots": 2}}), \
        "an old worker omits it"


def test_other_unknown_app_fields_are_still_refused():
    assert not valid({**BASE, "gpu": None, "pool": "app", "app": {"model": "m", "extra": 1}})


# ======================================================================================
# The admission pool's VRAM is the sharded RPC pool and nothing else
# ======================================================================================

def gpu_node(vram, **extra):
    return {"ram_gb": 32.0, "cpu_cores": 8, "cpu_threads": 16,
            "gpu": {"has_gpu": True, "vram_total_gb": vram, "name": "RTX"}, **extra}


def test_total_vram_counts_only_the_rpc_pool():
    registry = WorkerRegistry()
    add = lambda wid, hw, port=50053: registry.update_worker(wid, f"10.0.0.{len(registry.workers) + 2}", port, "t", hw)
    add("rpc-a", gpu_node(24.0))
    add("rpc-b", gpu_node(8.0))
    add("asleep", gpu_node(16.0, is_yielding=True))
    add("phone", gpu_node(12.0, pool="app", app={"model": "m", "ready": True, "slots": 2}))      # a profile that claims a GPU
    add("coder", gpu_node(8.0, pool="coding"))
    add("rpc-a-cpu", gpu_node(6.0, companion_of="rpc-a"))                                         # no pool field yet
    assert registry.get_total_vram_gb() == 32.0, "24 + 8: the two nodes of the sharded pool that are awake"


def test_total_vram_is_zero_with_only_members():
    registry = WorkerRegistry()
    registry.update_worker("coder", "10.0.0.2", 50053, "t", gpu_node(24.0, pool="coding"))
    registry.update_worker("phone", "10.0.0.3", 50053, "t", gpu_node(24.0, pool="app"))
    assert registry.get_total_vram_gb() == 0.0
