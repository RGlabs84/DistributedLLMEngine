"""
2.9.0, builder P: the pool counts system RAM (engine/pool_memory.py), rungs say what they need in two numbers
(engine/coding_ladder.py, engine/release.py), the layout picker (engine/pool_layout.py) and automatic switching
that is safe to leave on (engine/auto_swap.py). Contract: docs/release-2.9-contract.md sections 1 to 4 and 6.
"""

import copy
import json
import os
from types import SimpleNamespace

import pytest

import engine.config as engine_config
from engine import auto_swap, coding_ladder, coordinator, inference_manager, pool_layout, pool_memory, release
from engine.auto_swap import AutoSwapController, SwapResult

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(REPO_ROOT, "scripts", "coding_models.json"), "r", encoding="utf-8") as _handle:
    SHIPPED = json.load(_handle)["tiers"]
GIB = float(1 << 30)
T0 = 10_000.0


def shipped():
    return release.check_coding_tiers(copy.deepcopy(SHIPPED))


def by_id(tiers, tid):
    return next(t for t in tiers if t["id"] == tid)


# ======================================================================================
# The pool: GPU memory and lent RAM, counted apart
# ======================================================================================

def worker(wid, vram, *, local=False, lent=0.0, stay=0.0):
    return {"id": wid, "vram_gb": vram, "local": local, "lent_ram_gb": lent, "stay_seconds": stay}


def test_the_wall_lends_its_budget_less_what_the_engine_needs_besides_experts():
    assert pool_memory.wall_lent_ram_gb({}) == 8.0                                   # 18 - 10
    assert pool_memory.wall_lent_ram_gb({"pool_wall_ram_budget_gb": 20.0, "pool_wall_ram_reserve_gb": 12.0}) == 8.0
    assert pool_memory.wall_lent_ram_gb({"pool_wall_ram_budget_gb": 5.0}) == 0.0     # never negative


def test_the_wall_alone_is_16_gpu_and_8_ram():
    pool = pool_memory.build_pool_memory([worker("w", 16, local=True)], wall_ram_gb=8.0,
                                         ram_max_rtt_ok=lambda w: True, min_stay_seconds=1800)
    assert (pool["gpu_gb"], pool["ram_gb"], pool["total_gb"], pool["machines"]) == (16.0, 8.0, 24.0, 1)


def test_a_volunteers_ram_counts_only_when_close_and_after_30_minutes():
    near = lambda w: w["id"] != "far"
    ws = [worker("w", 16, local=True), worker("old", 8, lent=8, stay=3600), worker("new", 8, lent=8, stay=60),
          worker("far", 8, lent=8, stay=3600)]
    pool = pool_memory.build_pool_memory(ws, wall_ram_gb=8.0, ram_max_rtt_ok=near, min_stay_seconds=1800)
    assert pool["gpu_gb"] == 40.0                      # every near GPU counts at once
    assert pool["ram_gb"] == 16.0                      # 8 (wall) + 8 (the one that has stayed and is close)
    assert pool["volunteer_ram_gb"] == 8.0
    pending = {m.get("ram_pending_seconds") for m in pool["by_machine"] if "ram_pending_seconds" in m}
    assert pending == {1740}                           # the new one has 29 minutes to go


def test_a_volunteer_that_leaves_stops_counting_at_once():
    ws = [worker("w", 16, local=True), worker("lap", 8, lent=8, stay=3600)]
    both = pool_memory.build_pool_memory(ws, wall_ram_gb=8.0, ram_max_rtt_ok=lambda w: True, min_stay_seconds=1800)
    alone = pool_memory.build_pool_memory(ws[:1], wall_ram_gb=8.0, ram_max_rtt_ok=lambda w: True, min_stay_seconds=1800)
    assert both["total_gb"] == 40.0 and alone["total_gb"] == 24.0


def test_a_missing_or_bad_ram_lent_field_is_zero_and_a_big_one_is_clamped():
    assert pool_memory.volunteer_lent_ram_gb({"ram_gb": 32}) == 0.0
    assert pool_memory.volunteer_lent_ram_gb({"ram_gb": 32, "ram_lent_gb": "lots"}) == 0.0
    assert pool_memory.volunteer_lent_ram_gb({"ram_gb": 32, "ram_lent_gb": True}) == 0.0
    assert pool_memory.volunteer_lent_ram_gb({"ram_gb": 32, "ram_lent_gb": float("nan")}) == 0.0
    assert pool_memory.volunteer_lent_ram_gb({"ram_gb": 32, "ram_lent_gb": 8}) == 8.0
    assert pool_memory.volunteer_lent_ram_gb({"ram_gb": 10, "ram_lent_gb": 8}) == 6.0      # 4 GB stay with the owner
    assert pool_memory.volunteer_lent_ram_gb(None) == 0.0


def test_the_heartbeat_field_is_bounded():
    from engine.authn import validate_hardware_profile
    ok, _ = validate_hardware_profile({"ram_gb": 32, "ram_lent_gb": 8})
    assert ok
    ok, why = validate_hardware_profile({"ram_gb": 32, "ram_lent_gb": -1})
    assert not ok and "ram_lent_gb" in why
    ok, _ = validate_hardware_profile({"ram_gb": 32, "ram_lent_gb": 99999})
    assert not ok


@pytest.fixture
def lan_pool(monkeypatch):
    from engine.registry import WorkerRegistry

    registry = WorkerRegistry()
    for wid, ip, vram, lent in (("local", "127.0.0.1", 16.0, 0), ("lap", "192.168.1.20", 8.0, 8),
                                ("newbie", "192.168.1.21", 8.0, 8), ("wan", "203.0.113.7", 8.0, 8),
                                ("quiet", "192.168.1.22", 8.0, None)):
        hardware = {"gpu": {"has_gpu": True, "vram_total_gb": vram}, "ram_gb": 32.0}
        if lent is not None:
            hardware["ram_lent_gb"] = lent
        registry.update_worker(worker_id=wid, ip=ip, port=50052, token=f"t-{wid}", hardware_profile=hardware)
    for wid in ("lap", "newbie", "quiet"):
        registry.record_probe(wid, 0.8, "lan")
    registry.record_probe("wan", 30.0, "direct")                      # near for GPUs (80 ms), too far for RAM (5 ms)
    for wid in ("lap", "wan", "quiet"):
        registry.workers[wid]["registered_at"] -= 7200                # stayed two hours
    monkeypatch.setattr(coordinator, "registry", registry)
    return registry


def test_the_coordinators_pool_has_both_numbers(lan_pool):
    pool = coordinator._coding_pool()
    assert pool["vram_gb"] == 16 + 8 + 8 + 8 + 8       # every near GPU
    # wall 8 + the laptop that stayed and is on the LAN. Not: newbie (just arrived), wan (30 ms), quiet (no field).
    assert pool["ram_gb"] == 16.0
    assert pool["volunteer_ram_gb"] == 8.0 and pool["wall_ram_gb"] == 8.0
    status = coordinator._pool_memory_status(pool)
    assert set(status) >= {"gpu_gb", "ram_gb", "total_gb", "machines"}
    assert status["total_gb"] == 64.0 and status["machines"] == 5
    assert all("id" not in m for m in status["by_machine"]), "the public status names no machine"


def test_pool_ram_settings_parse_and_are_range_checked(isolated_config):
    def limits(text):
        isolated_config.write_text(text, encoding="utf-8")
        engine_config._cached_stamp = None
        return engine_config.get_worker_limits()
    base = limits("")
    assert (base["pool_ram_max_rtt_ms"], base["pool_ram_min_stay_seconds"]) == (5.0, 1800)
    assert (base["pool_wall_ram_budget_gb"], base["pool_wall_ram_reserve_gb"]) == (18.0, 10.0)
    got = limits("POOL_RAM_MAX_RTT_MS=2\nPOOL_RAM_MIN_STAY_SECONDS=600\nPOOL_WALL_RAM_BUDGET_GB=24\nPOOL_WALL_RAM_RESERVE_GB=10\n")
    assert (got["pool_ram_max_rtt_ms"], got["pool_ram_min_stay_seconds"], got["pool_wall_ram_budget_gb"]) == (2.0, 600.0, 24.0)
    bad = limits("POOL_RAM_MAX_RTT_MS=0\nPOOL_RAM_MIN_STAY_SECONDS=-5\nPOOL_WALL_RAM_BUDGET_GB=nan\nPOOL_WALL_RAM_RESERVE_GB=x\n")
    assert (bad["pool_ram_max_rtt_ms"], bad["pool_ram_min_stay_seconds"]) == (5.0, 1800)
    assert (bad["pool_wall_ram_budget_gb"], bad["pool_wall_ram_reserve_gb"]) == (18.0, 10.0)


# ======================================================================================
# Rungs: two numbers, a rank
# ======================================================================================

def test_a_rung_fits_only_when_both_numbers_hold():
    iq4 = by_id(shipped(), "qwen36-a3b-iq4xs")                    # 24 total, 12 on GPUs
    assert (iq4["min_total_gb"], iq4["min_gpu_gb"]) == (24, 12)
    assert coding_ladder.fits(iq4, 16, 24)
    assert not coding_ladder.fits(iq4, 16, 23.9)                  # not enough in all
    assert not coding_ladder.fits(iq4, 11.9, 40)                  # lots of RAM, too little on the cards
    assert auto_swap.fits_by_pool(iq4, {"vram_gb": 16, "ram_gb": 8})
    assert not auto_swap.fits_by_pool(iq4, {"vram_gb": 12, "ram_gb": 4})
    assert not auto_swap.fits_by_pool(iq4, {"vram_gb": float("nan"), "ram_gb": 8})


def test_an_old_rung_with_one_number_means_both():
    old = {"id": "x", "min_vram_gb": 24}
    assert coding_ladder.fits(old, 24, 24)
    assert not coding_ladder.fits(old, 16, 40)                    # RAM never stands in for the one number
    assert auto_swap.fits_by_pool(old, {"vram_gb": 24}) and not auto_swap.fits_by_pool(old, {"vram_gb": 16, "ram_gb": 99})


def test_old_entries_and_manifests_get_defaults_and_new_fields_travel_in_the_manifest(tmp_path):
    entry = {k: v for k, v in SHIPPED[1].items() if k not in ("min_total_gb", "min_gpu_gb", "rank")}
    old = release.coding_tier(entry)
    assert (old["min_total_gb"], old["min_gpu_gb"], old["rank"]) == (24, 24, None)
    tiers = shipped()
    flat = {}
    for t in tiers:
        flat.update(release._flatten_coding_tier(t))
    assert flat["coding_qwen36_a3b_iq4xs_min_total_gb"] == 24
    assert flat["coding_qwen36_a3b_iq4xs_min_gpu_gb"] == 12
    assert flat["coding_qwen36_a3b_iq4xs_rank"] == 1
    assert all(isinstance(v, (str, int)) and not isinstance(v, bool) for v in flat.values())
    manifest = {"coding_tiers": ",".join(t["id"] for t in tiers), **flat}
    assert release.coding_tiers(manifest) == tiers
    # a manifest from before 2.9.0 has none of the three keys
    for key in [k for k in manifest if k.endswith(("_min_total_gb", "_min_gpu_gb", "_rank"))]:
        del manifest[key]
    legacy = release.coding_tiers(manifest)
    assert [t["rank"] for t in legacy] == list(range(len(tiers)))
    assert all(t["min_total_gb"] == t["min_gpu_gb"] == t["min_vram_gb"] for t in legacy)


def test_ranks_must_increase_and_gpu_cannot_exceed_total():
    entries = copy.deepcopy(SHIPPED)
    entries[2]["rank"] = 0
    with pytest.raises(release.ReleaseError, match="rank"):
        release.check_coding_tiers(entries)
    entries = copy.deepcopy(SHIPPED)
    entries[1]["min_gpu_gb"] = 99
    with pytest.raises(release.ReleaseError, match="min_gpu_gb"):
        release.check_coding_tiers(entries)
    entries = copy.deepcopy(SHIPPED)
    entries[1]["min_total_gb"] = True
    with pytest.raises(release.ReleaseError, match="min_total_gb"):
        release.check_coding_tiers(entries)


def test_the_shipped_ladder_is_the_qwen_family_only_with_the_pinned_files():
    tiers = shipped()
    assert [t["id"] for t in tiers[:6]] == ["qwen36-a3b", "qwen36-a3b-iq4xs", "qwen36-a3b-q4", "qwen36-a3b-q6",
                                            "qwen3-coder-next-iq4xs", "qwen3-coder-next-q4ks"]
    enabled = [t["id"] for t in tiers if t["enabled"]]
    assert enabled == ["qwen36-a3b", "qwen36-a3b-iq4xs", "qwen36-a3b-q4", "qwen36-a3b-q6"]
    names = " ".join(t["name"] + t["file"] for t in tiers).lower()
    assert not any(word in names for word in ("gemma", "nemotron", "gpt-oss"))
    assert all(not t["enabled"] and t["blocked"] for t in tiers if t["id"].startswith("qwen38"))
    ranks = [t["rank"] for t in tiers]
    assert ranks == sorted(set(ranks))
    # Sizes and SHA-256 as the Hugging Face API gave them on 2026-11-02.
    pins = {
        "qwen36-a3b-iq4xs": (17730509792, "649d7508507b84638732c4f52c24c8b15843c6dca2f3ff793ae07c14a67ebbb3"),
        "qwen36-a3b-q4": (22134528992, "ac0e2c1189e055faa36eff361580e79c5bd6f8e76bffb4ce547f167d53e31a61"),
        "qwen36-a3b-q6": (29308320736, "4fe53b148b46f9b88830e2a3055c5b15c3a4d1e3ddc9a1384a108d8b9d59f043"),
        "qwen3-coder-next-iq4xs": (38429272064, "abf56d7fe8a0a99c15d220c13de4aa57b69cfba6ef4c2a007b56e34d7b40cd11"),
        "qwen3-coder-next-q4ks": (45531949088, "95e490ddf8d25eb911557ee1f4604ab7207e43f00a2bcf16223040ca0484cdb5"),
    }
    for tid, (size, sha) in pins.items():
        t = by_id(tiers, tid)
        assert (t["size"], t["sha256"]) == (size, sha)
        assert "/resolve/a483e9e6cbd595906af30beda3187c2663a1118c/" in t["url"] or "/resolve/ce09c67b53bc8739eef83fe67b2f5d293c270632/" in t["url"]
    assert by_id(tiers, "qwen3-coder-next-q4ks")["file"] == "Qwen3-Coder-Next-Q4_K_S.gguf"
    assert not by_id(tiers, "qwen3-coder-next-q4ks")["enabled"], "not measured, so not enabled"
    assert not by_id(tiers, "qwen3-coder-next-iq4xs")["enabled"], "measured, but left off until the harder round ranks it"


def test_tier_for_pool_takes_the_best_rank_that_fits():
    tiers = shipped()
    assert coding_ladder.tier_for_pool(tiers, 16, 24)["id"] == "qwen36-a3b-iq4xs"        # TheWall alone
    assert coding_ladder.tier_for_pool(tiers, 24, 40)["id"] == "qwen36-a3b-q6"
    assert coding_ladder.tier_for_pool(tiers, 24, 60)["id"] == "qwen36-a3b-q6", "the Coder-Next rungs are off"
    on = [dict(t, enabled=True, blocked=None) if t["id"] == "qwen3-coder-next-iq4xs" else t for t in tiers]
    assert coding_ladder.tier_for_pool(on, 24, 50)["id"] == "qwen3-coder-next-iq4xs"
    assert coding_ladder.tier_for_pool(tiers, 8, 8) is None


# ======================================================================================
# Layout picking
# ======================================================================================

def shape(layers=40, per_layer_gb=0.3, other_gb=4.0, embedding_gb=0.5):
    """A fake gguf.ModelShape: ``layers`` blocks each with ``per_layer_gb`` of experts."""
    expert = tuple((i, int(per_layer_gb * GIB)) for i in range(layers))
    total = int((other_gb + embedding_gb) * GIB) + sum(s for _, s in expert)

    class Shape(SimpleNamespace):
        def experts_on_cpu_bytes(self, n):
            return sum(s for i, s in self.expert_bytes_by_layer if i < n) if n > 0 else 0

        def gpu_bytes(self, n=0):
            return self.file_bytes - self.embedding_bytes - self.experts_on_cpu_bytes(n)

    return Shape(has_tensor_table=True, expert_bytes_by_layer=expert, file_bytes=total,
                 embedding_bytes=int(embedding_gb * GIB), embedding_tied=False, host_only_bytes=0)


def test_the_wall_alone_places_iq4xs_with_experts_in_its_own_ram():
    tier = by_id(shipped(), "qwen36-a3b-iq4xs")
    layout = pool_layout.plan_layout(tier, {"gpu_gb": 16.0, "wall_ram_gb": 8.0, "volunteer_ram_gb": 0.0}, shape())
    assert layout.realizable, layout.why
    assert layout.n_cpu_moe and layout.args == ["--n-cpu-moe", str(layout.n_cpu_moe)]
    assert engine_config.parse_engine_args(" ".join(layout.args)) == layout.args      # the allow-list takes it whole
    assert 0 < layout.experts_in_ram_gb <= 8.0
    # what stays on the cards fits the engine's own rule: 0.9 of the pool less the KV reserve
    on_gpu = shape().gpu_bytes(layout.n_cpu_moe) / GIB + inference_manager.RUNTIME_OVERHEAD_GB
    assert on_gpu <= 16 * inference_manager.POOL_SAFETY_FRACTION - pool_layout.KV_RESERVE_GB + 0.3


def test_a_rung_that_fits_on_the_cards_needs_no_flags():
    tier = by_id(shipped(), "qwen36-a3b")
    layout = pool_layout.plan_layout(tier, {"gpu_gb": 40.0, "wall_ram_gb": 8.0}, shape())
    assert layout.realizable and layout.args == [] and layout.n_cpu_moe == 0


def test_more_experts_than_this_hosts_ram_is_not_realizable_without_a_volunteer_device():
    tier = by_id(shipped(), "qwen36-a3b-q6")
    big = shape(layers=40, per_layer_gb=0.6)                       # 24 GB of experts
    pool = {"gpu_gb": 16.0, "wall_ram_gb": 8.0, "volunteer_ram_gb": 16.0}
    layout = pool_layout.plan_layout(tier, pool, big)
    assert not layout.realizable and "volunteer" in layout.why and layout.args == []
    with_device = pool_layout.plan_layout(tier, pool, big, rpc_cpu_buffers={"192.168.1.20:50053": "RPC0"},
                                          rpc_cpu_caps_gb={"192.168.1.20:50053": 16.0})
    assert with_device.realizable, with_device.why
    assert "-ot" in with_device.args and any(a.endswith("=CPU") for a in with_device.args)
    assert any(a.endswith("=RPC0") for a in with_device.args)
    assert engine_config.parse_engine_args(" ".join(__import__("shlex").quote(a) for a in with_device.args)) == with_device.args
    nothing = pool_layout.plan_layout(tier, {**pool, "volunteer_ram_gb": 0.0}, big, rpc_cpu_buffers={"x:1": "RPC0"},
                                      rpc_cpu_caps_gb={"x:1": 16.0})
    assert not nothing.realizable


def _gb_placed(layout, shape_, target):
    """GB of experts the -ot rules send to ``target``."""
    import re
    total = 0.0
    sizes = dict(shape_.expert_bytes_by_layer)
    for flag, rule in zip(layout.args[::2], layout.args[1::2]):
        match = re.search(r"blk\\.(\d+)\\.ffn_.*=(\S+)$", rule)
        if flag == "-ot" and match and match.group(2) == target:
            total += sizes[int(match.group(1))] / GIB
    return total


def test_a_cpu_endpoint_never_gets_more_than_the_ram_it_lent():
    """ggml-rpc-server --device CPU advertises the machine's whole RAM and has no cap flag: the layout is the cap."""
    tier = by_id(shipped(), "qwen36-a3b-q6")
    big = shape(layers=40, per_layer_gb=0.6)
    pool = {"gpu_gb": 16.0, "wall_ram_gb": 8.0, "volunteer_ram_gb": 16.0}
    layout = pool_layout.plan_layout(tier, pool, big, rpc_cpu_buffers={"a:1": "RPC0", "b:2": "RPC1"},
                                     rpc_cpu_caps_gb={"a:1": 4.8, "b:2": 12.0})
    assert layout.realizable, layout.why
    assert _gb_placed(layout, big, "CPU") <= 8.0 + 1e-9
    assert _gb_placed(layout, big, "RPC0") <= 4.8 + 1e-9
    assert _gb_placed(layout, big, "RPC1") <= 12.0 + 1e-9
    assert _gb_placed(layout, big, "RPC0") + _gb_placed(layout, big, "RPC1") > 0
    # caps that cannot hold what must leave the cards: refused, never squeezed past a cap
    too_small = pool_layout.plan_layout(tier, pool, big, rpc_cpu_buffers={"a:1": "RPC0"}, rpc_cpu_caps_gb={"a:1": 1.0})
    assert not too_small.realizable
    # an endpoint with no stated cap lends nothing
    assert not pool_layout.plan_layout(tier, pool, big, rpc_cpu_buffers={"a:1": "RPC0"}).realizable


def test_a_workers_cpu_entry_caps_what_it_can_lend_and_endpoints_are_validated():
    hw = {"ram_gb": 64, "ram_lent_gb": 16, "endpoints": [{"type": "gpu", "port": 50052}, {"type": "cpu", "port": 50053, "ram_gb": 8.0}]}
    assert pool_memory.volunteer_lent_ram_gb(hw) == 8.0
    assert pool_memory.cpu_endpoint_ram(hw) == {50053: 8.0}
    from engine.authn import validate_hardware_profile
    assert validate_hardware_profile(hw)[0]
    for bad in ("x", [{"type": "tpu", "port": 1}], [{"type": "cpu", "port": 0}], [{"type": "cpu", "port": True}],
                [{"type": "cpu", "port": 5, "ram_gb": -1}], [{"type": "cpu", "port": 5, "ram_gb": 5000}],
                [{"type": "gpu", "port": 1}] * 9):
        assert not validate_hardware_profile({"endpoints": bad})[0], bad
    assert not validate_hardware_profile({"ram_lent_gb": 2000})[0]


def test_a_model_that_cannot_fit_even_with_every_expert_in_ram_is_not_realizable():
    tier = by_id(shipped(), "qwen36-a3b-iq4xs")
    heavy = shape(layers=4, per_layer_gb=0.3, other_gb=30.0)       # 30 GB of non-expert weight
    assert not pool_layout.plan_layout(tier, {"gpu_gb": 16.0, "wall_ram_gb": 8.0}, heavy).realizable


def test_without_the_file_the_layout_is_priced_from_the_pinned_size():
    tier = by_id(shipped(), "qwen36-a3b-iq4xs")
    layout = pool_layout.plan_layout(tier, {"gpu_gb": 16.0, "wall_ram_gb": 8.0}, None)
    assert layout.realizable and layout.n_cpu_moe is None and layout.args == []
    assert not pool_layout.plan_layout(tier, {"gpu_gb": 16.0, "wall_ram_gb": 2.0}, None).realizable


def test_the_operators_own_n_cpu_moe_is_a_floor():
    tier = by_id(shipped(), "qwen36-a3b-iq4xs")
    s = shape()
    layout = pool_layout.plan_layout(tier, {"gpu_gb": 16.0, "wall_ram_gb": 8.0}, s)
    more = pool_layout.with_at_least(layout, layout.n_cpu_moe + 2, s)
    assert more.args == ["--n-cpu-moe", str(layout.n_cpu_moe + 2)]
    assert pool_layout.with_at_least(layout, 1, s) is layout
    assert pool_layout.with_at_least(layout, 40, s) is layout          # 12 GB of experts: more than the 8 lent


def test_the_picked_layout_replaces_the_operators_placement_in_the_engine_arguments(monkeypatch):
    limits = {"engine_extra_args": ["--n-cpu-moe", "16", "-t", "6", "--chat-template-kwargs", "{}"]}
    assert inference_manager.get_layout_override() is None
    assert inference_manager.effective_engine_args(limits, False) == limits["engine_extra_args"]
    assert inference_manager.effective_engine_args(limits, True) == ["-t", "6", "--chat-template-kwargs", "{}"]
    previous = inference_manager.set_layout_override(["--n-cpu-moe", "20"])
    try:
        assert previous is None
        for remote in (False, True):
            assert inference_manager.effective_engine_args(limits, remote) == [
                "-t", "6", "--chat-template-kwargs", "{}", "--n-cpu-moe", "20"]
        assert inference_manager.cpu_moe_layers(inference_manager.effective_engine_args(limits, True)) == 20
    finally:
        inference_manager.set_layout_override(None)


# ======================================================================================
# Automatic switching with two-number pools
# ======================================================================================

class Rig:
    def __init__(self, tiers, *, current, gpu, ram, fits=auto_swap.fits_by_pool):
        self.now = T0
        self.tiers = tiers
        self.current = current
        self.gpu, self.ram = gpu, ram
        self.swaps = []
        self.engine_short = False
        self.controller = AutoSwapController(
            tiers_provider=lambda: [dict(t) for t in self.tiers],
            pool_provider=lambda: {"vram_gb": self.gpu, "far_vram_gb": 0.0, "ram_gb": self.ram},
            current_model=lambda: self.current, swap=self._swap, tier_ready=self._ready, download=self._ready,
            settings=lambda: {"auto_swap": True, "auto_swap_up_stable_seconds": 600,
                              "auto_swap_down_stable_seconds": 120, "auto_swap_cooldown_seconds": 1800},
            swap_in_progress=lambda: False, queue_busy=lambda: False, clock=lambda: self.now,
            log=lambda m: None, engine_short=lambda: self.engine_short, fits=fits)

    async def _swap(self, tier, *, drain_seconds, abort_if_busy, check_speed=False):
        self.swaps.append(tier["id"])
        self.current = tier["file"]
        return SwapResult("swapped", "")

    async def _ready(self, tier):
        return True

    async def every(self, seconds, step=30.0):
        end = self.now + seconds
        decision = None
        while self.now <= end:
            decision = await self.controller.tick()
            self.now += step
        return decision


@pytest.mark.anyio
async def test_the_walls_real_numbers_never_step_down():
    """TheWall alone: 16 GB of cards, about 8 GB of experts in RAM, serving IQ4_XS. With switching ON nothing moves."""
    tiers = shipped()
    rig = Rig(tiers, current="Qwen3.6-35B-A3B-UD-IQ4_XS.gguf", gpu=16.0, ram=8.0)
    decision = await rig.every(6 * 3600)
    assert rig.swaps == []
    assert decision.state == "steady" and decision.reason == "top"
    assert "16 GB of GPU memory and 8 GB of RAM" in decision.detail


@pytest.mark.anyio
async def test_counting_gpu_memory_alone_would_have_stepped_it_down():
    """Why automatic switching was off: the old test, on the same pool, steps IQ4_XS down within two minutes."""
    rig = Rig(shipped(), current="Qwen3.6-35B-A3B-UD-IQ4_XS.gguf", gpu=16.0, ram=8.0,
              fits=lambda tier, pool: float(tier["min_vram_gb"]) <= pool["vram_gb"])
    await rig.every(600)
    assert rig.swaps == ["qwen36-a3b"]


@pytest.mark.anyio
async def test_a_laptops_gpu_and_ram_move_the_wall_up_after_the_stable_wait_and_back_down_when_it_leaves():
    rig = Rig(shipped(), current="Qwen3.6-35B-A3B-UD-IQ4_XS.gguf", gpu=16.0, ram=8.0)
    rig.gpu, rig.ram = 24.0, 16.0                       # a laptop with 8 GB of GPU lending 8 GB of RAM: 24 / 40
    await rig.every(540)
    assert rig.swaps == [], "not before 10 minutes"
    await rig.every(120)
    assert rig.swaps == ["qwen36-a3b-q6"]
    rig.engine_short = True
    rig.gpu, rig.ram = 16.0, 8.0                        # it leaves: down at once, no cooldown wait
    await rig.every(30)
    assert rig.swaps[-1] == "qwen36-a3b-iq4xs"


@pytest.mark.anyio
async def test_a_rung_needing_ram_does_not_unlock_on_gpu_alone():
    # 11 GB of cards and a lot of RAM: Q6_K needs 14 on the cards, whatever the RAM
    rig = Rig(shipped(), current="Qwen3.6-35B-A3B-UD-IQ4_XS.gguf", gpu=11.0, ram=60.0)
    await rig.every(3600)
    assert "qwen36-a3b-q6" not in rig.swaps and "qwen3-coder-next-iq4xs" not in rig.swaps


@pytest.mark.anyio
async def test_up_means_a_higher_rank_not_a_bigger_file():
    tiers = shipped()
    # make Q6_K measure WORSE than Q4_K_M: swap their ranks and their order in the list
    q4, q6 = by_id(tiers, "qwen36-a3b-q4"), by_id(tiers, "qwen36-a3b-q6")
    q4["rank"], q6["rank"] = 3, 2
    reordered = [t for t in tiers if t not in (q4, q6)]
    reordered[2:2] = [q6, q4]
    rig = Rig(reordered, current="Qwen3.6-35B-A3B-UD-IQ4_XS.gguf", gpu=24.0, ram=16.0)     # holds Q6_K (39) and Q4_K_M (29)
    await rig.every(700)
    assert rig.swaps == ["qwen36-a3b-q4"], "the better-measured rung wins although it is the smaller file"


@pytest.mark.anyio
async def test_the_coordinators_fit_test_also_needs_a_layout(monkeypatch):
    tiers = shipped()
    iq4 = by_id(tiers, "qwen36-a3b-iq4xs")
    monkeypatch.setattr(coordinator, "_rung_layout", lambda tier, pool: pool_layout.Layout(tier["id"], False, "no room"))
    assert not coordinator._rung_fits(iq4, {"vram_gb": 16.0, "ram_gb": 8.0})
    monkeypatch.setattr(coordinator, "_rung_layout", lambda tier, pool: pool_layout.Layout(tier["id"], True, "ok"))
    assert coordinator._rung_fits(iq4, {"vram_gb": 16.0, "ram_gb": 8.0})
    assert not coordinator._rung_fits(iq4, {"vram_gb": 16.0, "ram_gb": 4.0}), "the numbers still have to hold"


def test_the_walls_real_rung_has_a_layout_on_the_wall_alone_with_the_coordinators_own_fit_test(monkeypatch):
    """The coordinator's own fit test (numbers AND layout), no fakes but the model file's header."""
    iq4 = by_id(shipped(), "qwen36-a3b-iq4xs")
    monkeypatch.setattr(coordinator.manager, "resolve_model_path", lambda tag: "")     # file not on this machine
    pool = {"vram_gb": 16.0, "ram_gb": 8.0, "wall_ram_gb": 8.0, "volunteer_ram_gb": 0.0}
    assert coordinator._rung_fits(iq4, pool)


def test_the_status_rows_say_both_numbers_and_unlocked_follows_both(monkeypatch):
    import asyncio
    monkeypatch.setattr(coordinator, "_coding_tiers", shipped())
    _, rows = asyncio.run(coordinator._coding_status([], {"vram_gb": 16.0, "ram_gb": 8.0}))
    row = {r["id"]: r for r in rows}
    assert (row["qwen36-a3b-iq4xs"]["min_total_gb"], row["qwen36-a3b-iq4xs"]["min_gpu_gb"]) == (24, 12)
    assert row["qwen36-a3b-iq4xs"]["unlocked"] and not row["qwen36-a3b-q4"]["unlocked"]
    assert row["qwen36-a3b-q4"]["rank"] == 2
    _, old_style = asyncio.run(coordinator._coding_status([], 16.0))          # a plain number: GPU memory alone
    assert not {r["id"]: r for r in old_style}["qwen36-a3b-iq4xs"]["unlocked"]
