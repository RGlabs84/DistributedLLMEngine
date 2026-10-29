"""
``gpu_pool`` and ``cpu_pool`` in /v1/cluster/status: the live figures behind the website's two tiles.

Counts only: nothing names a machine or a user. A GPU is SERVING when it has a job (a node in the running
engine's topology, or a ready coding member) and STANDING BY when it is awake with none; a node handed back
to its owner (yielding) is in neither. The admission pool's ``total_vram_gb`` keeps its own meaning.
"""

import json
import time
from types import SimpleNamespace

import pytest

from engine import coding_speed, coordinator
from engine.app_pool import AppPool, Member
from engine.throughput import POOL_CODING, POOL_PHONE, throughput
from tests.test_coding_endpoint import (LocalEngine, canary_book, client, env, fakes, make_identity,  # noqa: F401
                                        sign_block)

GPU_KEYS = {"gpus", "vram_gb", "gpus_standing_by", "vram_standing_by_gb", "tokens_per_second",
            "tokens_last_hour", "measured_tokens_per_second", "reach"}
NO_REACH = {"host": 0, "best": 0, "good": 0, "usable": 0, "too_far": 0, "unmeasured": 0}
CPU_KEYS = {"machines", "machines_standing_by", "cores", "ram_gb", "tokens_per_second", "tokens_last_hour"}


def gpu_node(vram, count=None, **extra):
    gpu = {"has_gpu": True, "vram_total_gb": vram, "name": "RTX"}
    if count is not None:
        gpu["count"] = count
    return {"ram_gb": 32.0, "cpu_cores": 8, "cpu_threads": 16, "gpu": gpu, **extra}


def phone_node(ram, ready=True, threads=None, cores=12):
    app = {"model": "qwen", "ready": ready, "slots": 2}
    if threads is not None:
        app["threads"] = threads
    return {"pool": "app", "ram_gb": ram, "cpu_cores": cores, "cpu_threads": cores * 2, "gpu": None, "app": app}


@pytest.fixture
def pools(env, monkeypatch):
    """A swarm with every kind of node, and the running engine on two of its GPUs."""
    registry = env.registry

    def add(worker_id, ip, hardware, tunnel=False):
        registry.update_worker(worker_id, ip, 50053, f"tok-{worker_id}", hardware)
        if tunnel:
            env.relays[(ip, 50053)] = 40000

    add("n1", "10.1.0.1", gpu_node(24.0, count=2))              # serving: two GPUs, 24 GB
    add("n2", "10.1.0.2", gpu_node(8.0))                         # serving: an old worker, one GPU
    add("n3", "10.1.0.3", gpu_node(16.0, count=1))              # standing by: awake, not in the engine
    add("n4", "10.1.0.4", gpu_node(12.0, is_yielding=True))     # handed back to its owner: in no count
    env.local.endpoints = ["10.1.0.1:50053", "10.1.0.2:50053"]
    add("p1", "10.2.0.1", phone_node(12.0, threads=6), tunnel=True)             # serving
    add("p2", "10.2.0.2", phone_node(16.0, cores=12), tunnel=True)              # serving, an old worker: default threads
    add("p3", "10.2.0.3", phone_node(8.0, ready=False), tunnel=True)            # standing by
    add("p4", "10.2.0.4", {**phone_node(8.0, threads=2), "is_yielding": True}, tunnel=True)   # yielding: in no count
    pool = AppPool()
    pool.local = SimpleNamespace(as_member=lambda: Member("local", 8.0, "qwen", True, 2, "http://127.0.0.1:9",
                                                          local=True, threads=4))
    monkeypatch.setattr(coordinator, "app_pool", pool)
    env.add(env.fakes["a"], "ca")                                # a ready coding member: serving
    env.add(env.fakes["b"], "cb", ready=False)                   # a verified one with no job: standing by
    env.add(env.fakes["c"], "cc", identity=None)                 # a block that does not verify: advertised nowhere
    monkeypatch.setattr(coding_speed.meter, "speed", lambda file: (33.5, 123.0))
    throughput.record(POOL_CODING, 90)
    throughput.record(POOL_PHONE, 30)
    return env


async def status(c):
    resp = await c.get("/v1/cluster/status")
    assert resp.status_code == 200
    return resp.json()


@pytest.mark.anyio
async def test_the_status_has_both_pools_with_exactly_these_fields(pools):
    async with client() as c:
        body = await status(c)
    assert set(body["gpu_pool"]) == GPU_KEYS and set(body["cpu_pool"]) == CPU_KEYS
    gpu, cpu = body["gpu_pool"], body["cpu_pool"]
    for key in ("gpus", "gpus_standing_by", "tokens_last_hour"):
        assert type(gpu[key]) is int, key
    for key in ("vram_gb", "vram_standing_by_gb", "tokens_per_second"):
        assert type(gpu[key]) is float, key
    for key in ("machines", "machines_standing_by", "cores", "tokens_last_hour"):
        assert type(cpu[key]) is int, key
    for key in ("ram_gb", "tokens_per_second"):
        assert type(cpu[key]) is float, key
    print("\nSTATUS_EXAMPLE " + json.dumps({"gpu_pool": gpu, "cpu_pool": cpu, "total_vram_gb": body["total_vram_gb"],
                                          "coding_members": body["coding_members"]}, indent=2))


@pytest.mark.anyio
async def test_gpus_are_counted_serving_or_standing_by_and_yielding_ones_in_neither(pools):
    async with client() as c:
        gpu = (await status(c))["gpu_pool"]
    # serving: n1's two GPUs (24 GB) + n2's one (8 GB) + the ready coding member's one (8 GB)
    assert (gpu["gpus"], gpu["vram_gb"]) == (4, 40.0)
    # standing by: n3 (16 GB) + the coding member without a job (8 GB); n4 is yielding, the unverified one is hidden
    assert (gpu["gpus_standing_by"], gpu["vram_standing_by_gb"]) == (2, 24.0)


@pytest.mark.anyio
async def test_the_tokens_are_the_windows_and_the_measurement_is_the_single_user_figure(pools):
    async with client() as c:
        body = await status(c)
    assert body["gpu_pool"]["tokens_per_second"] == 1.5 and body["gpu_pool"]["tokens_last_hour"] == 90
    assert body["gpu_pool"]["measured_tokens_per_second"] == 33.5 == body["coding_model"]["tokens_per_second"]
    assert body["cpu_pool"]["tokens_per_second"] == 0.5 and body["cpu_pool"]["tokens_last_hour"] == 30


@pytest.mark.anyio
async def test_older_tokens_count_in_the_hour_but_not_in_the_rate(env):
    throughput.record(POOL_CODING, 600, now=time.time() - 600)
    async with client() as c:
        gpu = (await status(c))["gpu_pool"]
    assert gpu["tokens_per_second"] == 0.0 and gpu["tokens_last_hour"] == 600


@pytest.mark.anyio
async def test_cpu_machines_serving_and_standing_by_with_their_threads_and_ram(pools):
    async with client() as c:
        cpu = (await status(c))["cpu_pool"]
    # serving: p1 (6 threads, 12 GB), p2 (an old worker: half of 12 cores, 16 GB), the coordinator's own member (4, 8 GB),
    # and p4: its owner is gaming but it is ready, so it serves in light mode (2.4.2; registry.app_members) (2, 8 GB)
    assert (cpu["machines"], cpu["cores"], cpu["ram_gb"]) == (4, 18, 44.0)
    assert cpu["machines_standing_by"] == 1, "p3 is registered but not ready"


@pytest.mark.anyio
async def test_the_admission_pool_keeps_its_meaning(pools):
    """total_vram_gb is the sharded RPC pool (n1, n2, n3 awake); members and yielding nodes are not part of it."""
    async with client() as c:
        body = await status(c)
    assert body["total_vram_gb"] == 48.0 and body["gpu_pool"]["vram_gb"] == 40.0
    assert pools.registry.get_total_vram_gb() == 48.0


@pytest.mark.anyio
async def test_without_a_running_engine_every_rpc_gpu_is_standing_by(pools):
    pools.local.endpoints = []
    async with client() as c:
        gpu = (await status(c))["gpu_pool"]
    assert (gpu["gpus"], gpu["vram_gb"]) == (1, 8.0), "only the ready coding member has a job"
    assert (gpu["gpus_standing_by"], gpu["vram_standing_by_gb"]) == (5, 56.0), "n1 (2, 24 GB), n2, n3 and the idle member"


@pytest.mark.anyio
async def test_coding_members_are_not_serving_while_members_are_off(pools):
    pools.limits["coding_members_enabled"] = False
    async with client() as c:
        gpu = (await status(c))["gpu_pool"]
    assert (gpu["gpus"], gpu["vram_gb"]) == (3, 32.0)


@pytest.mark.anyio
async def test_a_member_a_canary_caught_is_advertised_nowhere(pools, canary_book):
    canary_book.presented_key(canary_book.issue(pools.fakes["a"].trust_id).key, "198.51.100.5")
    async with client() as c:
        gpu = (await status(c))["gpu_pool"]
    assert (gpu["gpus"], gpu["vram_gb"]) == (3, 32.0) and gpu["gpus_standing_by"] == 2


@pytest.mark.anyio
async def test_each_gpu_is_graded_by_how_far_this_coordinator_measured_it(pools):
    """The owner's grades (2026-10-26): 40 ms and under best, 80 good, 120 usable, more too far."""
    registry = pools.registry
    registry.record_probe("n1", 0.6, "lan")          # two GPUs
    registry.record_probe("n2", 75.0, "tunnel")
    registry.record_probe("n3", 300.0, "direct")
    registry.record_probe("cb", 110.0, "tunnel")
    async with client() as c:
        gpu = (await status(c))["gpu_pool"]
    # n4 is yielding and cc does not verify: neither is in any count, so neither is graded.
    assert gpu["reach"] == {"host": 0, "best": 2, "good": 1, "usable": 1, "too_far": 1, "unmeasured": 1}
    assert sum(gpu["reach"].values()) == gpu["gpus"] + gpu["gpus_standing_by"]


@pytest.mark.anyio
async def test_an_empty_swarm_still_has_the_whole_shape(env):
    env.limits["coding_members_enabled"] = False
    async with client() as c:
        body = await status(c)
    assert body["gpu_pool"] == {"gpus": 0, "vram_gb": 0.0, "gpus_standing_by": 0, "vram_standing_by_gb": 0.0,
                                "tokens_per_second": 0.0, "tokens_last_hour": 0, "measured_tokens_per_second": None,
                                "reach": NO_REACH}
    assert body["cpu_pool"] == {"machines": 0, "machines_standing_by": 0, "cores": 0, "ram_gb": 0.0,
                                "tokens_per_second": 0.0, "tokens_last_hour": 0}


@pytest.mark.anyio
async def test_a_failure_in_a_pool_figure_leaves_the_status_answering(env, monkeypatch):
    def boom():
        raise RuntimeError("no inventory today")

    monkeypatch.setattr(env.registry, "gpu_inventory", boom)
    async with client() as c:
        body = await status(c)
    assert set(body["gpu_pool"]) == GPU_KEYS and body["gpu_pool"]["gpus"] == 0
    assert set(body["cpu_pool"]) == CPU_KEYS


@pytest.mark.anyio
async def test_the_pool_figures_name_no_machine_and_no_user(pools):
    async with client() as c:
        body = await status(c)
    text = json.dumps({"gpu_pool": body["gpu_pool"], "cpu_pool": body["cpu_pool"]})
    for needle in ("n1", "p1", "ca", "10.1.", "10.2.", "10.8.", "tok-", pools.fakes["a"].trust_id, pools.fakes["a"].pubkey):
        assert needle not in text, needle
