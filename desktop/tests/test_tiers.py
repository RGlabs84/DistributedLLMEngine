"""The smart sliders: tier stops, the half-RAM rule, defaults, and what each setting powers."""

import json
import os

import pytest

from conftest import release
from dlne_donate import bundled, tiers
from dlne_donate.tiers import Hardware, Settings

APP = tiers.app_tiers_from_status({"app_pool": {"tiers": [
    {"id": "qwen3-4b", "name": "Qwen3 4B Instruct 2507", "min_ram_gb": 8, "unlocked": True},
    {"id": "qwen36-a3b", "name": "Qwen3.6 35B-A3B", "min_ram_gb": 16, "unlocked": True},
    {"id": "qwen36-a3b-q4", "name": "Qwen3.6 35B-A3B (full quality)", "min_ram_gb": 24, "unlocked": False}]}})


def machine(**kwargs) -> Hardware:
    base = dict(gpu_name="", vram_gb=0.0, gpu_count=0, total_ram_gb=32, physical_cores=8, logical_threads=16, avx2=True)
    base.update(kwargs)
    return Hardware(**base)


def coding_ladder():
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with open(os.path.join(here, "scripts", "coding_models.json"), encoding="utf-8") as handle:
        entries = json.load(handle)
    entries = entries if isinstance(entries, list) else entries["tiers"]
    entries = [{k: v for k, v in e.items() if not k.startswith("_")} for e in entries]
    return [t for t in release.check_coding_tiers(entries) if t["enabled"]]


@pytest.fixture(scope="module")
def select():
    return bundled.import_snapshot("coding_member_select")


# ---- the machine ------------------------------------------------------------------------------------

@pytest.mark.parametrize("reported_gib,sold", [(31.0, 32), (15.5, 16), (7.6, 8), (63.2, 64), (23.4, 24), (11.6, 12), (127.0, 128)])
def test_installed_ram_is_what_the_machine_was_sold_with(reported_gib, sold):
    assert tiers.installed_ram_gb(reported_gib * 1024 ** 3) == sold


def test_threads_default_to_physical_cores_minus_two_at_least_four():
    assert tiers.default_threads(24, 32) == 22
    assert tiers.default_threads(8, 16) == 6
    assert tiers.default_threads(6, 12) == 4
    assert tiers.default_threads(4, 8) == 4
    assert tiers.default_threads(2, 2) == 2      # never more than the machine has


# ---- the RAM stops ----------------------------------------------------------------------------------

def test_the_cpu_slider_snaps_to_the_swarms_tiers_and_labels_each():
    stops = tiers.cpu_stops(machine(total_ram_gb=32), APP)
    assert [(s.gb, s.runs) for s in stops] == [(8, "Qwen3 4B"), (16, "Qwen3.6"), (24, "full quality")]
    assert [s.beyond_half for s in stops] == [False, False, True]     # 24 of 32 GB is over half: allowed, with a warning


def test_the_owner_keeps_four_gb_whatever_they_drag():
    assert [s.gb for s in tiers.cpu_stops(machine(total_ram_gb=16), APP)] == [8]       # 16 would leave 0; 12 is the ceiling
    assert [s.gb for s in tiers.cpu_stops(machine(total_ram_gb=28), APP)] == [8, 16, 24]
    assert [s.gb for s in tiers.cpu_stops(machine(total_ram_gb=27), APP)] == [8, 16]
    assert tiers.cpu_stops(machine(total_ram_gb=8), APP) == []


def test_the_default_offer_never_exceeds_half_the_ram():
    for total in (16, 24, 32, 48, 64):
        s = tiers.default_settings(machine(total_ram_gb=total), APP)
        assert s.cpu_ram_gb <= total / 2, total


def test_the_live_tiers_replace_the_built_in_ladder():
    live = tiers.app_tiers_from_status({"app_pool": {"tiers": [{"id": "x", "name": "Big Model", "min_ram_gb": 12, "unlocked": True}]}})
    assert [s.gb for s in tiers.cpu_stops(machine(total_ram_gb=32), live)] == [12]
    assert [t["min_ram_gb"] for t in tiers.app_tiers_from_status(None)] == [8, 16, 24]      # swarm unreachable: the built-in ladder


def test_short_model_names():
    assert tiers.short_model_name("Qwen3 4B Instruct 2507") == "Qwen3 4B"
    assert tiers.short_model_name("Qwen3.6 35B-A3B") == "Qwen3.6"
    assert tiers.short_model_name("Qwen3.6 35B-A3B (full quality)") == "full quality"


# ---- defaults and clamping --------------------------------------------------------------------------

def test_a_gpu_machine_starts_lending_its_gpu_and_a_cpu_machine_its_cpu():
    gpu = tiers.default_settings(machine(gpu_name="RTX", vram_gb=8, gpu_count=1), APP)
    assert gpu.gpu_enabled and not gpu.cpu_enabled and gpu.gpu_vram_gb == 8 and gpu.gpu_ram_gb == 16
    cpu = tiers.default_settings(machine(), APP)
    assert cpu.cpu_enabled and not cpu.gpu_enabled and cpu.cpu_ram_gb == 16


def test_ram_alone_disk_and_bandwidth_are_not_offered():
    weak = machine(physical_cores=2, logical_threads=2)
    s = tiers.clamp_settings(Settings(cpu_enabled=True, gpu_enabled=True), weak, APP)
    assert not s.cpu_enabled and not s.gpu_enabled          # too few cores, no GPU: nothing to lend


def test_clamping_keeps_values_inside_the_sliders():
    hw = machine(gpu_name="RTX", vram_gb=8, gpu_count=1, total_ram_gb=32)
    s = tiers.clamp_settings(Settings(gpu_enabled=True, gpu_vram_gb=99, gpu_ram_gb=500, cpu_enabled=True, cpu_ram_gb=13, threads=400), hw, APP)
    assert s.gpu_vram_gb == 8 and s.gpu_ram_gb == 28 and s.cpu_ram_gb == 16 and s.threads == 16
    s = tiers.clamp_settings(Settings(gpu_enabled=True, gpu_vram_gb=0.2, gpu_ram_gb=0, threads=0), hw, APP)
    assert s.gpu_vram_gb == 1 and s.gpu_ram_gb == 4 and s.threads == 1


def test_settings_from_garbage_json_fall_back_to_defaults():
    hw = machine(gpu_name="RTX", vram_gb=8, gpu_count=1)
    default = tiers.default_settings(hw, APP)
    assert tiers.settings_from_json("not a dict", hw, APP) == default
    assert tiers.settings_from_json({"gpu": {"enabled": "yes", "vram_gb": "lots"}, "threads": float("nan")}, hw, APP).gpu_vram_gb == 8


def test_settings_round_trip():
    hw = machine(gpu_name="RTX", vram_gb=8, gpu_count=1)
    s = Settings(gpu_enabled=True, gpu_vram_gb=6, gpu_ram_gb=20, cpu_enabled=True, cpu_ram_gb=8, threads=6, resume_donating=True)
    assert tiers.settings_from_json(tiers.settings_to_json(s), hw, APP) == s


# ---- what a setting powers --------------------------------------------------------------------------

def test_a_big_enough_gpu_with_ram_powers_a_coding_member_with_a_speed_estimate(select):
    hw = machine(gpu_name="RTX 4070", vram_gb=8, gpu_count=1)
    s = Settings(gpu_enabled=True, gpu_vram_gb=8, gpu_ram_gb=16, threads=6)
    d = tiers.describe(s, hw, coding_ladder(), APP, select)
    assert "Powers a coding member" in d.gpu_line and "Qwen3.6" in d.gpu_line and "tokens per second" in d.gpu_line
    assert d.can_donate and d.coding_tier and d.disk_gb > 5


def test_a_gpu_below_8_gb_joins_the_gpu_pool_instead(select):
    hw = machine(gpu_name="GTX", vram_gb=6, gpu_count=1)
    d = tiers.describe(Settings(gpu_enabled=True, gpu_vram_gb=6, gpu_ram_gb=16, threads=6), hw, coding_ladder(), APP, select)
    assert "Below what a coding member needs" in d.gpu_line and "GPU pool" in d.gpu_line
    assert d.can_donate and not d.coding_tier


def test_lending_vram_below_8_gb_of_a_big_card_also_leaves_the_coding_job(select):
    hw = machine(gpu_name="RTX 4090", vram_gb=24, gpu_count=1, total_ram_gb=64)
    d = tiers.describe(Settings(gpu_enabled=True, gpu_vram_gb=4, gpu_ram_gb=24, threads=6), hw, coding_ladder(), APP, select)
    assert "Below what a coding member needs" in d.gpu_line


def test_too_little_ram_beside_the_gpu_is_said(select):
    hw = machine(gpu_name="RTX 4070", vram_gb=8, gpu_count=1, total_ram_gb=16)
    d = tiers.describe(Settings(gpu_enabled=True, gpu_vram_gb=8, gpu_ram_gb=4, threads=6), hw, coding_ladder(), APP, select)
    assert "Not enough RAM" in d.gpu_line and "GPU pool" in d.gpu_line


def test_cpu_ram_picks_the_phone_model():
    hw = machine(total_ram_gb=32)
    low = tiers.describe(Settings(cpu_enabled=True, cpu_ram_gb=8, threads=6), hw, [], APP)
    mid = tiers.describe(Settings(cpu_enabled=True, cpu_ram_gb=16, threads=6), hw, [], APP)
    top = tiers.describe(Settings(cpu_enabled=True, cpu_ram_gb=24, threads=6), hw, [], APP)
    assert "Qwen3 4B" in low.cpu_line and "Qwen3.6" in mid.cpu_line and "full quality" in top.cpu_line
    assert all(d.can_donate for d in (low, mid, top))
    assert "switches this level on" in top.cpu_line          # the swarm has not unlocked it yet


def test_a_cpu_below_the_floors_says_so_and_cannot_donate():
    weak = machine(physical_cores=2, logical_threads=4)
    d = tiers.describe(Settings(cpu_enabled=True, cpu_ram_gb=8, threads=2), weak, [], APP)
    assert "Below what the swarm can use" in d.cpu_line and not d.can_donate
    noavx = machine(avx2=False)
    assert "AVX2" in tiers.describe(Settings(cpu_enabled=True, cpu_ram_gb=8, threads=4), noavx, [], APP).cpu_line


def test_nothing_switched_on_cannot_donate():
    d = tiers.describe(Settings(), machine(), [], APP)
    assert not d.can_donate and "Turn on" in d.blocker


def test_warnings_for_more_than_half_and_for_both_together():
    hw = machine(gpu_name="RTX", vram_gb=8, gpu_count=1, total_ram_gb=32)
    d = tiers.describe(Settings(cpu_enabled=True, cpu_ram_gb=24, threads=6), hw, [], APP)
    assert any("more than half" in w for w in d.warnings)
    both = tiers.describe(Settings(gpu_enabled=True, gpu_vram_gb=8, gpu_ram_gb=20, cpu_enabled=True, cpu_ram_gb=16, threads=6), hw, [], APP)
    assert any("Together" in w for w in both.warnings)


def test_threads_line_says_how_much_stays_free():
    hw = machine()
    assert "about 10 stay free" in tiers.describe(Settings(cpu_enabled=True, cpu_ram_gb=8, threads=6), hw, [], APP).threads_line
    assert "every thread" in tiers.describe(Settings(cpu_enabled=True, cpu_ram_gb=8, threads=16), hw, [], APP).threads_line


# ---- config.txt -------------------------------------------------------------------------------------

URL, KEY = "https://example.test", "k" * 64


def test_gpu_only_limits():
    hw = machine(gpu_name="RTX", vram_gb=8, gpu_count=1)
    u = tiers.config_updates(Settings(gpu_enabled=True, gpu_vram_gb=7.5, gpu_ram_gb=20, threads=6), hw, URL, KEY)
    assert u["MAX_VRAM_GB"] == "7.5" and u["CODING_MAX_RAM_GB"] == "20" and u["MAX_RAM_GB"] == "4"
    assert u["ALLOWED_GPU_INDICES"] == "-1" and u["ALSO_CPU_RAM_GB"] == "0" and u["MAX_CPU_THREADS"] == "6"
    assert u["CODING_MEMBER_ALLOWED"] == "true" and u["COORDINATOR_URL"] == URL and u["NETWORK_KEY"] == KEY


def test_cpu_only_hides_the_gpu_from_the_worker():
    hw = machine(gpu_name="RTX", vram_gb=8, gpu_count=1)
    u = tiers.config_updates(Settings(gpu_enabled=False, cpu_enabled=True, cpu_ram_gb=16, threads=8), hw, URL, KEY)
    assert u["ALLOWED_GPU_INDICES"] == "none" and u["MAX_RAM_GB"] == "16" and u["MAX_VRAM_GB"] == "-1"
    assert u["ALSO_CPU_RAM_GB"] == "0" and u["CODING_MEMBER_ALLOWED"] == "false"


def test_both_lends_the_cpu_next_to_the_gpu():
    hw = machine(gpu_name="RTX", vram_gb=8, gpu_count=1)
    u = tiers.config_updates(Settings(gpu_enabled=True, gpu_vram_gb=8, gpu_ram_gb=16, cpu_enabled=True, cpu_ram_gb=8, threads=8), hw, URL, KEY)
    assert u["ALSO_CPU_RAM_GB"] == "8" and u["CODING_MAX_RAM_GB"] == "16" and u["ALLOWED_GPU_INDICES"] == "-1"


def test_every_config_key_is_one_the_engine_reads():
    """Drift guard against engine/config.py: a renamed key would silently stop limiting anything."""
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with open(os.path.join(here, "engine", "config.py"), encoding="utf-8") as handle:
        text = handle.read()
    hw = machine(gpu_name="RTX", vram_gb=8, gpu_count=1)
    for key in tiers.config_updates(Settings(gpu_enabled=True, gpu_vram_gb=8, threads=6), hw, URL, KEY):
        if key == "COORDINATOR_URL":
            continue    # written as the extension writes it; the worker takes its coordinator from --coordinator
        assert f'"{key}"' in text, key
