"""
The phones' own engine on several home cards (2.9.4): PHONE_DEVICE naming more than one card splits the model across
them, the photo reader stays on the FIRST, each card is checked for its own free memory, and one card behaves exactly as
before. TheWall's case: Qwen3.6 35B-A3B IQ3_S over its two 8 GB BC-160s.
"""

import pytest

from engine import config as cfg
from engine.image_member import MemberError
from engine.phone_engine import HOME_CARD_SHARE, COMPUTE_MB, phone_rungs, plan_layout, resolve_budget_mb
from tests.test_phone_engine import env, gpu, limits_of, llama  # noqa: F401  (fixtures and helpers)

BC160 = 8176 / 1024.0          # GiB, as the driver reports a BC-160
TWO = "0000:0f:00.0,0000:13:00.0"


def rung(rung_id):
    return next(r for r in phone_rungs() if r["id"] == rung_id)


def two_card_plan(rung_id, volunteers=(), **over):
    limits = limits_of(**over)
    budgets = [BC160 * 1024 * HOME_CARD_SHARE] * 2
    return plan_layout(rung(rung_id), list(volunteers), budget_mb=resolve_budget_mb(limits), lanes=limits["phone_lanes"],
                       context=limits["phone_context"], vision=limits["phone_vision"], home_budgets_mb=budgets), budgets


# ------------------------------------------------------------------ the plan
def test_iq3_fits_two_8_gb_cards_with_the_photo_reader_and_nothing_on_volunteers():
    p, budgets = two_card_plan("qwen36-a3b-iq3")
    assert p["ok"] and p["home_layers"] == 40 and p["volunteer_layers"] == {} and p["need_mb"] == 0
    assert len(p["home_split"]) == 2 and sum(p["home_split"]) == 40 and p["split"] == p["home_split"]
    assert all(mb <= b + 1e-6 for mb, b in zip(p["home_mbs"], budgets))
    # the first card carries the photo reader, so it gets fewer layers than the second
    assert p["home_split"][0] < p["home_split"][1]
    assert p["home_mbs"][0] - p["home_split"][0] * (p["home_mbs"][1] - COMPUTE_MB) / p["home_split"][1] > COMPUTE_MB + 800
    assert p["home_mb"] == pytest.approx(sum(p["home_mbs"]))


def test_iq4_does_not_fit_two_8_gb_cards_alone_but_does_with_a_volunteer():
    p, _ = two_card_plan("qwen36-a3b")
    assert not p["ok"] and "nowhere to go" in p["reason"]
    p, _ = two_card_plan("qwen36-a3b", [gpu("a", 8, "10.1.1.1:50052")])
    assert p["ok"] and len(p["split"]) == 3 and sum(p["split"]) == 40 and p["volunteer_layers"]["a"] == p["split"][2]


def test_one_home_card_plans_exactly_as_before():
    limits = limits_of()
    args = dict(budget_mb=resolve_budget_mb(limits), lanes=limits["phone_lanes"], context=limits["phone_context"],
                vision=True)
    for rid, volunteers in (("qwen35-4b", []), ("qwen35-9b", []), ("qwen35-9b", [gpu("a", 24, "10.1.1.1:50052")])):
        old = plan_layout(rung(rid), volunteers, **args)
        same = plan_layout(rung(rid), volunteers, **args, home_budgets_mb=[args["budget_mb"]])
        assert old == same and old["home_split"] == [old["home_layers"]]
        if old["ok"]:
            assert old["split"][0] == old["home_layers"] and sum(old["split"]) == rung(rid)["layers"]
        else:
            assert old["split"] == []           # a plan that does not fit has no split, as in 2.9.3


def test_a_too_small_first_card_is_refused_not_moved():
    p = plan_layout(rung("qwen36-a3b-iq3"), [], budget_mb=0, lanes=2, context=8192, vision=True, home_budgets_mb=[500, 8000])
    assert not p["ok"] and "photo reader" in p["reason"]


# ------------------------------------------------------------------ config
def test_the_new_rung_and_a_card_list_parse():
    out = dict(cfg.PHONE_ENGINE_DEFAULTS)
    cfg.apply_phone_engine_config({"PHONE_ENGINE": "own", "PHONE_MODEL": "qwen36-a3b-iq3", "PHONE_DEVICE": f" {TWO} "}, out)
    assert out["phone_model"] == "qwen36-a3b-iq3" and out["phone_device"] == TWO


# ------------------------------------------------------------------ the engine
def cards(env, first_free=8.1, second_free=8.1):
    table = {"0000:0f:00.0": {"vk_index": 3, "free_gb": first_free, "total_gb": BC160},
             "0000:13:00.0": {"vk_index": 4, "free_gb": second_free, "total_gb": BC160}}
    env.engine._card_of = lambda spec: table.get(spec)
    env.limits = limits_of(phone_device=TWO, phone_model="qwen36-a3b-iq3")


def test_home_cards_and_budgets(env):
    cards(env)
    assert env.engine.home_cards() == ["0000:0f:00.0", "0000:13:00.0"]
    assert env.engine.home_budgets() == [pytest.approx(BC160 * 1024 * HOME_CARD_SHARE)] * 2
    env.limits = limits_of(phone_device=TWO, phone_home_budget_mb=6000)
    assert env.engine.home_budgets() == [6000.0, 6000.0]
    env.limits = limits_of(phone_device="0000:0f:00.0")
    assert env.engine.home_budgets() is None and env.engine.home_cards() == ["0000:0f:00.0"]
    env.limits = limits_of(phone_device="cpu")
    assert env.engine.home_cards() == [] and env.engine.home_budgets() is None


@pytest.mark.anyio
async def test_two_cards_split_the_model_and_the_photo_reader_stays_on_the_first(env):
    cards(env)
    await env.engine.start(rung("qwen36-a3b-iq3"), [])
    start = env.spawner.starts[0]
    command = start["command"]
    assert start["env"]["GGML_VK_VISIBLE_DEVICES"] == "3,4"
    assert command[command.index("--device") + 1] == "Vulkan0,Vulkan1" and "--rpc" not in command
    split = [int(n) for n in command[command.index("-ts") + 1].split(",")]
    assert len(split) == 2 and sum(split) == 40 and split == env.engine.layout["home_split"]
    assert command[command.index("--mmproj-device") + 1] == "Vulkan0"
    assert command[command.index("--mmproj") + 1].endswith("Qwen3.6-35B-A3B-mmproj-F16.gguf")
    assert command[command.index("-m") + 1].endswith("Qwen3.6-35B-A3B-UD-IQ3_S.gguf")
    st = env.engine.status()
    assert st["rung"] == "qwen36-a3b-iq3" and st["device"] == TWO and st["layers"] == {"home": 40, "volunteers": 0}
    await env.engine.stop()


@pytest.mark.anyio
async def test_each_home_card_is_checked_for_its_own_share(env):
    cards(env, second_free=2.0)
    with pytest.raises(MemberError):
        await env.engine.start(rung("qwen36-a3b-iq3"), [])
    cards(env, first_free=None)
    with pytest.raises(MemberError):
        await env.engine.start(rung("qwen36-a3b-iq3"), [])
    cards(env)
    env.engine._card_of = lambda spec: {"vk_index": 3, "free_gb": 8.1, "total_gb": BC160} if spec == "0000:0f:00.0" else None
    with pytest.raises(MemberError):
        await env.engine.start(rung("qwen36-a3b-iq3"), [])
    assert env.spawner.starts == []


@pytest.mark.anyio
async def test_the_same_card_named_twice_by_two_specs_is_refused(env):
    cards(env)
    env.limits = limits_of(phone_device="0000:0f:00.0,3", phone_model="qwen36-a3b-iq3")
    env.engine._card_of = lambda spec: {"vk_index": 3, "free_gb": 8.1, "total_gb": BC160}
    with pytest.raises(MemberError):
        await env.engine.start(rung("qwen36-a3b-iq3"), [])
    assert env.spawner.starts == []


@pytest.mark.anyio
async def test_one_card_starts_exactly_as_before(env):
    await env.engine.start(rung("qwen35-4b"), [])
    command = env.spawner.starts[0]["command"]
    assert command[command.index("--device") + 1] == "Vulkan0" and "-ts" not in command
    assert env.spawner.starts[0]["env"]["GGML_VK_VISIBLE_DEVICES"] == "2"
    await env.engine.stop()
