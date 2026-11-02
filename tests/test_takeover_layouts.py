"""Named layouts on takeover leases (2.9.0, docs/takeover-contract.md addendum)."""

import logging

import pytest

import engine.config as engine_config
from engine import takeover
from engine.config import get_plain_worker_limits, get_worker_limits
from tests.takeover_fakes import POOL_A, POOL_B, MEDIA, box   # noqa: F401  (box is a fixture)

NORMAL = (
    "DEFAULT_MODEL=Qwen3.6-35B-A3B-UD-IQ4_XS.gguf\n"
    "ALLOWED_GPU_INDICES=0000:03:00.0,0000:09:00.0\n"
    f"MEDIA_GPU={MEDIA}\nENGINE_MMPROJ=/models/mmproj-F16.gguf\nIMAGE_GEN_ENABLED=true\nVOICE_ENABLED=true\n"
    "ENGINE_SLOTS=4\nENGINE_EXTRA_ARGS=--threads 4 --cpu-mask 0xf3c -ts 26,14\n"
    "MAINTENANCE=false\nTAKEOVER_KEY_HASH=" + "ab" * 32 + "\nSTARTER_QUOTA_INFERENCES=77\n"
)
ONE = ("DEFAULT_MODEL=Qwen3.5-9B-Q4_K_M.gguf\nENGINE_MMPROJ=/models/qwen35-9b/mmproj-Qwen3.5-9B-F16.gguf\nALLOWED_GPU_INDICES=" + POOL_B + "\nENGINE_SLOTS=1\nENGINE_CONTEXT=74752\n"
       "ENGINE_EXTRA_ARGS=--threads 2 --cpu-mask 0x30c\nMAINTENANCE=true\nTAKEOVER_KEY_HASH=ffff\n")
TWO = ("ALLOWED_GPU_INDICES=" + MEDIA + "\nENGINE_SLOTS=2\nENGINE_CONTEXT=65536\nIMAGE_GEN_ENABLED=paused\n"
       "VOICE_ENABLED=paused\nENGINE_EXTRA_ARGS=--threads 2\n")


@pytest.fixture
def layouts(box, monkeypatch, tmp_path):
    models = tmp_path / "gguf"
    models.mkdir()
    (models / "Qwen3.5-9B-Q4_K_M.gguf").write_bytes(b"x")
    monkeypatch.setattr(takeover, "model_on_disk", lambda tag: (models / tag).is_file())
    box.config_path.write_text(NORMAL)
    engine_config._cached_stamp = None
    d = box.config_path.parent
    (d / "config.txt.takeover-1card").write_text(ONE)
    (d / "config.txt.takeover-2card").write_text(TWO)
    (d / "config.txt.normal-layout").write_text(NORMAL)
    return box


def test_the_layout_is_chosen_by_the_cards_taken(layouts):
    gpus = layouts.gpus
    pick = lambda g: takeover.layout_for(takeover.parse_request({"gpus": g}), gpus)  # noqa: E731
    assert pick([POOL_A]) == "takeover-1card"
    assert pick([POOL_B, POOL_A]) == "takeover-2card"
    assert pick([POOL_B]) is None and pick([MEDIA]) is None and pick([POOL_A, MEDIA]) is None
    assert takeover.layout_for(takeover.parse_request({"ram_gb": 2}), gpus) is None


def test_the_plan_shows_the_layout(layouts):
    req = takeover.parse_request({"gpus": [POOL_A]})
    plan = takeover.make_plan(req, limits=get_plain_worker_limits(), gpus=layouts.gpus, facts=layouts.facts,
                              memory_now=None, system_ram_gb=32.0)
    assert plan.allowed and plan.keep_result["layout"] == "takeover-1card"
    assert plan.keep_result["engine_on"] == [POOL_B] and plan.keep_result["coding_lanes"] == 1
    assert plan.keep_result["coding_context"] == 74752 and plan.engine_restart
    assert [c["action"] for c in plan.cards] == ["handover"]


def test_keys_are_merged_not_replaced_and_protected_keys_stay(layouts):
    ctrl = layouts.controller()
    import asyncio
    lease = asyncio.run(ctrl.grant({"gpus": [POOL_A]}))
    limits = get_worker_limits()
    assert limits["allowed_gpu_indices"] == POOL_B and limits["engine_slots"] == 1
    assert limits["starter_quota_inferences"] == 77, "a key the layout does not set keeps config.txt's value"
    assert limits["maintenance"] is False, "MAINTENANCE is never taken from a layout"
    assert limits["takeover_key_hash"] == "ab" * 32
    assert engine_config.parse_engine_args("--threads 2 --cpu-mask 0x30c")
    assert limits["engine_extra_args"] == ["--threads", "2", "--cpu-mask", "0x30c"]
    assert lease["dlne_now"]["layout"] == "takeover-1card"
    assert layouts.config_path.read_text() == NORMAL, "config.txt is never written"


@pytest.mark.anyio
async def test_grant_restarts_on_the_layout_and_release_restores_config_txt(layouts):
    ctrl = layouts.controller()
    lease = await ctrl.grant({"gpus": [POOL_A, POOL_B]})
    assert layouts.restarts[0]["allowed_gpu_indices"] == MEDIA and layouts.restarts[0]["engine_slots"] == 2
    assert layouts.restarts[0]["image_gen_paused"] is True and layouts.pictures_stopped == 1
    await ctrl.release(lease["lease_id"])
    await ctrl._restore_task
    assert len(layouts.restarts) == 2
    assert layouts.restarts[1]["allowed_gpu_indices"] == "0000:03:00.0,0000:09:00.0"
    assert layouts.restarts[1]["engine_slots"] == 4 and layouts.restarts[1]["image_gen_paused"] is False
    assert layouts.config_path.read_text() == NORMAL


@pytest.mark.anyio
async def test_a_missing_layout_file_falls_back_to_the_generic_plan_with_a_log_line(layouts, monkeypatch):
    (layouts.config_path.parent / "config.txt.takeover-1card").unlink()
    seen = []
    monkeypatch.setattr(takeover, "log_warning", seen.append)
    ctrl = layouts.controller()
    lease = await ctrl.grant({"gpus": [POOL_A]})
    assert any("takeover-1card not used" in m for m in seen)
    assert "layout" not in lease["dlne_now"]
    assert "layout_keys" not in (takeover.load_lease() or {}).get("overlay", {})


def test_one_disallowed_flag_makes_the_layout_unusable(layouts, monkeypatch):
    bad = ONE.replace("--threads 2", "--threads 2 --not-a-real-flag")
    (layouts.config_path.parent / "config.txt.takeover-1card").write_text(bad)
    seen = []
    monkeypatch.setattr(takeover, "log_warning", seen.append)
    assert takeover.choose_layout(takeover.parse_request({"gpus": [POOL_A]}), layouts.gpus) is None
    assert any("allow" in m for m in seen)


def test_named_layouts_can_be_switched_off(layouts):
    layouts.config_path.write_text(NORMAL + "TAKEOVER_LAYOUTS=false\n")
    engine_config._cached_stamp = None
    assert takeover.choose_layout(takeover.parse_request({"gpus": [POOL_A]}), layouts.gpus) is None


@pytest.mark.anyio
async def test_a_1card_lease_restarts_on_the_9b_with_its_projector_and_args_and_release_goes_back(layouts):
    ctrl = layouts.controller()
    lease = await ctrl.grant({"gpus": [POOL_A]})
    limits = get_worker_limits()
    assert takeover.model_for_restart(takeover.load_lease(), "Qwen3.6-35B-A3B-UD-IQ4_XS.gguf") == "Qwen3.5-9B-Q4_K_M.gguf"
    assert limits["engine_mmproj"] == "/models/qwen35-9b/mmproj-Qwen3.5-9B-F16.gguf"
    assert layouts.restarts[0]["engine_extra_args"] == ["--threads", "2", "--cpu-mask", "0x30c"]
    await ctrl.release(lease["lease_id"])
    # while restoring, the restart goes back to config.txt's own model
    assert takeover.model_for_restart(takeover.load_lease(), "Qwen3.5-9B-Q4_K_M.gguf") == "Qwen3.6-35B-A3B-UD-IQ4_XS.gguf"
    await ctrl._restore_task
    assert layouts.restarts[1]["engine_extra_args"] == ["--threads", "4", "--cpu-mask", "0xf3c", "-ts", "26,14"]
    assert get_worker_limits()["engine_mmproj"] == "/models/mmproj-F16.gguf"
    assert takeover.model_for_restart(takeover.load_lease(), "x.gguf") == "x.gguf"


def test_a_layout_whose_model_is_not_on_disk_falls_back_with_a_log_line(layouts, monkeypatch):
    monkeypatch.setattr(takeover, "model_on_disk", lambda tag: False)
    seen = []
    monkeypatch.setattr(takeover, "log_warning", seen.append)
    assert takeover.choose_layout(takeover.parse_request({"gpus": [POOL_A]}), layouts.gpus) is None
    assert any("Qwen3.5-9B-Q4_K_M.gguf is not on disk" in m and "generic plan" in m for m in seen)
