"""
Choosing a coding member's model, the speed floors and the join test
(engine/coding_member_select.py, docs/coding-members.md section 3).

The choice is tested against the real ladder (scripts/coding_models.json) on realistic machines;
the join test against a fake llama-server on a loopback port (and a stub client for the odd
answers), so the whole path from "which model" to "does it qualify" runs without a GPU.
"""

import copy
import http.server
import json
import math
import os
import threading
import time

import pytest

from engine import coding_ladder
from engine import coding_member_select as sel
from engine import config as engine_config

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIMITS = {}     # an explicit dict means "the code's defaults": nothing here reads the real config


@pytest.fixture(scope="module")
def ladder():
    return coding_ladder.load_tier_file(os.path.join(REPO_ROOT, "scripts", "coding_models.json"))


def ids(tiers):
    return [t["id"] for t in tiers]


def by_id(tiers, tier_id):
    return next(t for t in tiers if t["id"] == tier_id)


IQ3, IQ4, Q4, Q6 = "qwen36-a3b", "qwen36-a3b-iq4xs", "qwen36-a3b-q4", "qwen36-a3b-q6"
LAPTOP = (7.996, 30.0)      # the owner's RTX 4070 laptop: 8188 MiB, 30 GB of RAM


# ======================================================================================
# Realistic machines against the real ladder
# ======================================================================================

# (name, GPU GiB, RAM GiB, extra machine facts, the tier the smartest-that-is-fast-enough rule gives)
MACHINES = [
    ("no GPU memory to speak of: 4 GB card", 4, 64, {}, None),
    ("6 GB card is under the 8 GB minimum", 6, 32, {}, None),
    ("8 GB card with only 8 GB of RAM cannot hold the model", 8, 8, {}, None),
    ("8 GB card (8188 MiB) with 16 GB of RAM", 7.996, 15.7, {}, IQ3),
    ("8 GB card with 16 GB of RAM", 8, 16, {}, IQ3),
    ("12 GB card with 16 GB of RAM", 12, 16, {}, IQ4),
    ("12 GB card with 32 GB of RAM", 12, 32, {}, Q4),
    ("24 GB card with 32 GB of RAM", 24, 32, {}, Q6),
    ("24 GB card with 64 GB of RAM", 24, 64, {}, Q6),
    ("24 GB card with 8 GB of RAM: weights sit on the card", 24, 8, {}, Q4),
    ("48 GB workstation card with 64 GB of RAM", 48, 64, {}, Q6),
    ("16 GB Mac (12 GB for the GPU) holds nothing", 12, 16, {"unified": True}, None),
    ("32 GB Mac", 24, 32, {"unified": True}, Q4),
    ("64 GB Mac", 48, 64, {"unified": True}, Q6),
    ("128 GB Mac or Strix Halo", 96, 128, {"unified": True}, Q6),
]


@pytest.mark.parametrize("name, vram, ram, facts, expected", MACHINES, ids=[m[0] for m in MACHINES])
def test_each_machine_gets_the_smartest_tier_it_runs_fast_enough(ladder, name, vram, ram, facts, expected):
    chosen = sel.choose_tier(vram, ram, ladder, LIMITS, **facts)
    assert (chosen["id"] if chosen else None) == expected


def test_the_borderline_machines_get_one_of_two_neighbours(ladder):
    # 8 GB card with 30 GB of RAM: the estimate puts IQ4_XS just over the margin (the join test decides);
    # 16 GB card with 32 GB of RAM: Q6_K is on the edge of the decode floor.
    assert sel.choose_tier(*LAPTOP, ladder, LIMITS)["id"] in (IQ3, IQ4)
    assert sel.choose_tier(16, 32, ladder, LIMITS)["id"] in (Q4, Q6)


def test_the_choice_is_the_ladders_own_dict(ladder):
    chosen = sel.choose_tier(24, 32, ladder, LIMITS)
    assert chosen is by_id(ladder, Q6)


def test_a_disabled_tier_is_never_chosen_however_much_memory_there_is(ladder):
    assert not by_id(ladder, "qwen38-flash-next-q4")["enabled"]
    assert sel.choose_tier(96, 128, ladder, LIMITS, unified=True)["id"] == Q6
    assert sel.choose_tier(500, 1024, ladder, LIMITS)["id"] == Q6


def test_a_bigger_tier_is_chosen_once_the_owner_enables_it(ladder):
    enabled = copy.deepcopy(ladder)
    for tier in enabled:
        tier["enabled"], tier["blocked"] = True, None
    # 128 GB of unified memory holds Flash-Next Q4_K_XL (111 GB, 82 GB of it matrices), not the 158 GB Q5.
    assert sel.choose_tier(96, 128, enabled, LIMITS, unified=True)["id"] == "qwen38-flash-next-q4"
    assert sel.choose_tier(150, 192, enabled, LIMITS, unified=True)["id"] == "qwen38-flash-next-q5"
    # The 64 GB Mac cannot hold even the smallest of them, but it holds Qwen3-Coder-Next IQ4_XS (the 49 GB
    # rung above the 35B's, enabled only once it is measured better); with that rung off it keeps the 35B.
    assert sel.choose_tier(48, 64, enabled, LIMITS, unified=True)["id"] == "qwen3-coder-next-iq4xs"
    without_coder_next = [t for t in enabled if t["id"] != "qwen3-coder-next-iq4xs"]
    assert sel.choose_tier(48, 64, without_coder_next, LIMITS, unified=True)["id"] == Q6


def test_unified_memory_is_one_pool_not_two(ladder):
    # 24 GB for the GPU plus 32 GB of RAM would be 50 usable; as one 32 GB pool it is 26 and Q6_K (30 GB) is out.
    assert sel.choose_tier(24, 32, ladder, LIMITS)["id"] == Q6
    assert sel.choose_tier(24, 32, ladder, LIMITS, unified=True)["id"] == Q4


def test_the_os_reserve_comes_off_the_ram(ladder):
    # 8 + 13.4 GiB: 7.4 usable, 15.4 in all against the 15.5 the IQ3_S tier needs.
    assert sel.choose_tier(8, 13.4, ladder, LIMITS) is None
    assert sel.choose_tier(8, 13.6, ladder, LIMITS)["id"] == IQ3
    # An operator who knows better can leave the OS less.
    assert sel.choose_tier(8, 13.4, ladder, {"coding_member_ram_reserve_gb": 3.0})["id"] == IQ3
    # ...and the config spelling works too.
    assert sel.choose_tier(8, 13.4, ladder, {"CODING_MEMBER_RAM_RESERVE_GB": 3.0})["id"] == IQ3


def test_eight_gb_means_cards_that_report_just_under_eight(ladder):
    assert sel.choose_tier(7.6, 32, ladder, LIMITS) is not None
    assert sel.choose_tier(7.4, 32, ladder, LIMITS) is None


@pytest.mark.parametrize("vram, ram", [
    (None, None), ("8", "32"), (float("nan"), 32), (8, float("nan")), (-8, 32), (8, -32), (True, True),
    (float("inf"), float("inf")), ([], {}), (0, 0),
])
def test_nonsense_inputs_choose_nothing_and_never_raise(ladder, vram, ram):
    assert sel.choose_tier(vram, ram, ladder, LIMITS) is None


def test_an_empty_or_broken_ladder_chooses_nothing():
    assert sel.choose_tier(24, 64, [], LIMITS) is None
    assert sel.choose_tier(24, 64, None, LIMITS) is None
    broken = [None, "x", {}, {"id": "a", "enabled": True}, {"id": "b", "enabled": True, "size": "big", "context": 1},
              {"id": "c", "enabled": True, "size": 1e9, "context": float("nan")}]
    assert sel.choose_tier(24, 64, broken, LIMITS) is None


def test_the_candidates_are_smartest_first_and_all_expected_to_pass(ladder):
    found = sel.candidate_tiers(24, 32, ladder, LIMITS)
    assert ids(found) == [Q6, Q4, IQ4, IQ3]
    order = {t["id"]: i for i, t in enumerate(ladder)}
    assert [order[i] for i in ids(found)] == sorted((order[i] for i in ids(found)), reverse=True)
    floor_decode, floor_prefill = sel.floors(LIMITS)
    for estimate in sel.assess_tiers(24, 32, ladder, LIMITS):
        if estimate.tier_id in ids(found):
            assert estimate.verdict == "good"
            assert estimate.decode_tps >= floor_decode and estimate.prefill_tps >= floor_prefill


def test_it_steps_down_only_when_the_estimate_says_the_floor_is_missed(ladder):
    # More RAM changes nothing about speed: the 8 GB card's share of the weights is what counts, so Q6_K and Q4_K_M
    # are estimated too slow even on a machine with 64 GB of RAM, and the choice stops at the tier that clears the margin.
    for ram in (30, 64, 128):
        estimates = {e.tier_id: e for e in sel.assess_tiers(7.996, ram, ladder, LIMITS)}
        assert estimates[Q6].verdict != "good" and estimates[Q4].verdict != "good"
        assert sel.choose_tier(7.996, ram, ladder, LIMITS)["id"] in (IQ3, IQ4)
    # Slow memory makes the bigger of the two too slow; the smaller one is chosen.
    assert sel.choose_tier(*LAPTOP, ladder, LIMITS, ram_bw_gbps=20)["id"] == IQ3
    estimates = {e.tier_id: e for e in sel.assess_tiers(*LAPTOP, ladder, LIMITS, ram_bw_gbps=20)}
    assert estimates[IQ4].verdict != "good" and estimates[IQ3].verdict == "good"


def test_a_machine_the_estimate_doubts_still_gets_the_smallest_tier_for_the_join_test_to_judge(ladder):
    marginal = sel.assess_tiers(*LAPTOP, ladder, LIMITS, ram_bw_gbps=12)
    assert {e.tier_id: e.verdict for e in marginal}[IQ3] == "marginal"
    assert ids(sel.candidate_tiers(*LAPTOP, ladder, LIMITS, ram_bw_gbps=12)) == [IQ3]
    # Far too slow even for that: not a coding member, and no download is wasted finding out.
    assert {e.tier_id: e.verdict for e in sel.assess_tiers(*LAPTOP, ladder, LIMITS, ram_bw_gbps=5)}[IQ3] == "slow"
    assert sel.choose_tier(*LAPTOP, ladder, LIMITS, ram_bw_gbps=5) is None


def test_next_tier_down_walks_the_candidates(ladder):
    walk, tier = [], sel.choose_tier(24, 32, ladder, LIMITS)
    while tier is not None:
        walk.append(tier["id"])
        tier = sel.next_tier_down(tier, 24, 32, ladder, LIMITS)
    assert walk == [Q6, Q4, IQ4, IQ3]
    assert sel.MAX_STEP_DOWNS == 2
    assert sel.next_tier_down({"id": "not-in-the-ladder"}, 24, 32, ladder, LIMITS) is None
    assert sel.next_tier_down(None, 24, 32, ladder, LIMITS) is None


def test_every_tier_of_the_real_ladder_is_known_to_the_estimate_not_guessed(ladder):
    for tier in ladder:
        text = sel._tier_text(tier)
        assert any(pattern.search(text) for pattern, _ in sel.FAMILIES), f"{tier['id']} is not in coding_member_select.FAMILIES"


# ======================================================================================
# The estimate: dense versus mixture of experts, GPU versus RAM
# ======================================================================================

def test_the_estimate_matches_what_was_measured_on_the_laptop(ladder):
    est = sel.estimate_tier(by_id(ladder, IQ3), sel.make_machine(*LAPTOP), LIMITS)
    assert est.fits and est.verdict == "good"
    assert est.decode_tps == pytest.approx(32.5, rel=0.1)       # measured 32.5 tok/s (dlne-coding-bakeoff)
    assert est.prefill_tps == pytest.approx(121.2, rel=0.1)     # measured 121.2 tok/s
    assert 0.35 < est.gpu_fraction < 0.5                        # the card holds under half of the 13.7 GB file


def test_the_estimate_matches_the_dense_14b_measured_on_the_laptop():
    tier = {"id": "baseline-14b", "name": "Qwen2.5 Coder 14B", "file": "qwen2.5-coder-14b-instruct-q4_k_m.gguf",
            "size": 8988110272, "context": 32768, "enabled": True}
    est = sel.estimate_tier(tier, sel.make_machine(*LAPTOP), LIMITS)
    assert est.prefill_tps == pytest.approx(460.5, rel=0.1)     # measured 460.5 tok/s
    # Measured 16.9 tok/s only with a 0.5B draft model at 94% acceptance; plain decode is about a third of that.
    assert 4.0 < est.decode_tps < 7.5


def test_a_dense_model_reads_far_more_per_token_than_an_equal_sized_moe():
    moe = {"id": "m-35b-a3b-q4", "name": "M 35B-A3B", "file": "M-35B-A3B-Q4_K_M.gguf", "size": 20_000_000_000, "context": 65536,
           "enabled": True}
    dense = {"id": "d-27b-q4", "name": "D 27B", "file": "D-27B-Q4_K_M.gguf", "size": 20_000_000_000, "context": 65536,
             "enabled": True}
    for vram, ram in ((8, 32), (12, 32), (24, 32)):
        rig = sel.make_machine(vram, ram)
        assert sel.estimate_tier(moe, rig, LIMITS).decode_tps > 4 * sel.estimate_tier(dense, rig, LIMITS).decode_tps


def test_a_dense_quality_tier_needs_a_big_card():
    dense27 = {"id": "qwen38-27b-q4", "name": "Qwen3.8 27B (Q4_K_M)", "file": "Qwen3.8-27B-Q4_K_M.gguf",
               "size": 16_460_000_000, "context": 65536, "enabled": True}
    for vram in (8, 12, 16):
        assert sel.estimate_tier(dense27, sel.make_machine(vram, 32), LIMITS).verdict == "slow"
    assert sel.estimate_tier(dense27, sel.make_machine(24, 32), LIMITS).verdict == "good"


def test_the_family_comes_from_the_tier_the_table_or_the_name(ladder):
    assert sel.tier_family(by_id(ladder, IQ3)) == sel.Family(35.0, 3.0, 0.71, 0.13)
    flash = sel.tier_family(by_id(ladder, "qwen38-flash-next-q4"))
    assert (flash.params_b, flash.active_params_b, flash.cpu_resident_gb) == (125.0, 6.0, 29.5)
    assert sel.tier_family(by_id(ladder, "qwen38-flash-next-q5")).cpu_resident_gb == 55.1
    # Not in the table: read from the name, with a big KV cache.
    moe = sel.tier_family({"id": "acme-30b-a3b-q4", "name": "Acme", "file": "Acme-30B-A3B-Instruct-Q4_K_M.gguf", "size": 18e9})
    assert (moe.params_b, moe.active_params_b, moe.moe) == (30.0, 3.0, True)
    dense = sel.tier_family({"id": "acme-14b", "name": "Acme", "file": "Acme-14B-Instruct-Q4_K_M.gguf", "size": 9e9})
    assert (dense.params_b, dense.active_params_b, dense.moe) == (14.0, 14.0, False)
    assert dense.kv_gb_per_64k == pytest.approx(6.3)
    nameless = sel.tier_family({"id": "mystery", "name": "Mystery", "file": "mystery.gguf", "size": 9e9})
    assert nameless.moe is False and nameless.params_b == pytest.approx(9e9 / 1e9 * 8 / 4.5)
    # A field on the tier wins over both.
    custom = sel.tier_family({**by_id(ladder, IQ3), "params_b": 40, "active_params_b": 5, "kv_gb_per_64k": 1.5,
                              "recurrent_gb": 0.2, "cpu_resident_gb": 0.5})
    assert custom == sel.Family(40.0, 5.0, 1.5, 0.2, 0.5)


def test_tier_fields_change_the_estimate(ladder):
    base = by_id(ladder, IQ4)
    rig = sel.make_machine(12, 32)
    plain = sel.estimate_tier(base, rig, LIMITS)
    heavier = sel.estimate_tier({**base, "active_params_b": 9}, rig, LIMITS)
    assert heavier.decode_tps < plain.decode_tps and heavier.prefill_tps < plain.prefill_tps
    bigger_kv = sel.estimate_tier({**base, "kv_gb_per_64k": 8.0}, rig, LIMITS)
    assert bigger_kv.need_gb > plain.need_gb and bigger_kv.gpu_fraction < plain.gpu_fraction


def test_the_kv_cache_follows_the_tiers_context(ladder):
    tier = by_id(ladder, IQ3)
    small = sel.estimate_tier({**tier, "context": 16384}, sel.make_machine(*LAPTOP), LIMITS)
    large = sel.estimate_tier({**tier, "context": 262144}, sel.make_machine(*LAPTOP), LIMITS)
    assert large.need_gb > small.need_gb + 2.0
    assert large.gpu_fraction < small.gpu_fraction


def test_a_tier_split_in_parts_is_sized_by_its_parts(ladder):
    tier = {k: v for k, v in by_id(ladder, IQ3).items() if k not in ("size",)}
    tier["files"] = [{"size": 6_000_000_000}, {"size": 7_676_723_168}]
    assert sel.estimate_tier(tier, sel.make_machine(*LAPTOP), LIMITS) == sel.estimate_tier(by_id(ladder, IQ3), sel.make_machine(*LAPTOP), LIMITS)


def test_more_cards_do_not_make_the_weights_faster(ladder):
    # Two 8 GB cards hold 16 GB, but a token still goes through one card's bandwidth at a time.
    one_big = sel.estimate_tier(by_id(ladder, IQ4), sel.make_machine(16, 32), LIMITS)
    two_small = sel.estimate_tier(by_id(ladder, IQ4), sel.make_machine(16, 32, gpu_count=2), LIMITS)
    assert two_small.gpu_fraction == one_big.gpu_fraction
    assert two_small.decode_tps < one_big.decode_tps


def test_known_hardware_figures_replace_the_conservative_ones(ladder):
    tier = by_id(ladder, Q6)
    slow = sel.estimate_tier(tier, sel.make_machine(24, 32), LIMITS)
    fast = sel.estimate_tier(tier, sel.make_machine(24, 32, gpu_bw_gbps=900, gpu_gflops=120000, ram_bw_gbps=70), LIMITS)
    assert fast.decode_tps > slow.decode_tps and fast.prefill_tps > slow.prefill_tps


def test_the_floors_and_margin_set_the_verdict(ladder):
    tier, rig = by_id(ladder, IQ3), sel.make_machine(*LAPTOP)      # about 32 tok/s decode, 120 prefill
    assert sel.estimate_tier(tier, rig, {"coding_member_min_decode_tps": 25}).verdict == "good"
    assert sel.estimate_tier(tier, rig, {"coding_member_min_decode_tps": 30}).verdict == "marginal"
    assert sel.estimate_tier(tier, rig, {"coding_member_min_decode_tps": 60}).verdict == "slow"
    assert sel.estimate_tier(tier, rig, {"coding_member_min_prefill_tps": 110}).verdict == "marginal"
    assert sel.estimate_tier(tier, rig, {"coding_member_min_prefill_tps": 110, "coding_member_estimate_margin": 1.0}).verdict == "good"


def test_a_limits_dict_without_the_keys_gets_the_configs_defaults():
    assert sel.floors({}) == (15.0, 80.0)
    assert sel._limit({}, "coding_member_ram_reserve_gb") == 6.0
    assert sel._limit({}, "coding_member_estimate_margin") == 1.2
    assert sel._limit({"coding_member_ram_reserve_gb": -2}, "coding_member_ram_reserve_gb") == 6.0


# ======================================================================================
# The floors
# ======================================================================================

def test_the_floors_default_to_15_decode_and_80_prefill():
    assert sel.meets_floor(15, 80, LIMITS)
    assert sel.meets_floor(32.5, 121.2, LIMITS)
    assert not sel.meets_floor(14.9, 500, LIMITS)
    assert not sel.meets_floor(500, 79.9, LIMITS)
    assert not sel.meets_floor(14.9, 79.9, LIMITS)


@pytest.mark.parametrize("decode, prefill", [
    (None, 100), (100, None), ("32", "121"), (float("nan"), 100), (100, float("nan")), (0, 100), (100, 0), (-5, 100),
    (100, -5), (True, True), (float("-inf"), 100), ([], {}),
])
def test_a_missing_or_nonsense_speed_never_meets_the_floor(decode, prefill):
    assert not sel.meets_floor(decode, prefill, LIMITS)


def test_the_floors_come_from_the_limits_by_either_spelling():
    assert sel.meets_floor(20, 100, {"coding_member_min_decode_tps": 20.0})
    assert not sel.meets_floor(19, 100, {"coding_member_min_decode_tps": 20.0})
    assert not sel.meets_floor(20, 100, {"CODING_MEMBER_MIN_PREFILL_TPS": 150})
    assert sel.meets_floor(20, 150, {"CODING_MEMBER_MIN_PREFILL_TPS": 150, "coding_member_min_decode_tps": 20})
    # Nonsense in the limits keeps the default floor.
    assert sel.floors({"coding_member_min_decode_tps": "fast", "coding_member_min_prefill_tps": float("nan")}) == (15.0, 80.0)


def test_with_no_limits_the_floors_are_read_from_the_config(isolated_config):
    isolated_config.write_text("CODING_MEMBER_MIN_DECODE_TPS=25\nCODING_MEMBER_MIN_PREFILL_TPS=200\n", encoding="utf-8")
    assert sel.floors() == (25.0, 200.0)
    assert sel.meets_floor(26, 201)
    assert not sel.meets_floor(24, 201)
    isolated_config.write_text("", encoding="utf-8")
    engine_config.get_config(force_reload=True)
    assert sel.floors() == (15.0, 80.0)


# ======================================================================================
# The settings
# ======================================================================================

def test_the_coding_member_settings_have_defaults():
    limits = engine_config.get_worker_limits()
    assert limits["coding_member_min_decode_tps"] == 15.0
    assert limits["coding_member_min_prefill_tps"] == 80.0
    assert limits["coding_member_ram_reserve_gb"] == 6.0
    assert limits["coding_member_estimate_margin"] == 1.2


@pytest.mark.parametrize("raw, key, expected", [
    ({"CODING_MEMBER_MIN_DECODE_TPS": "20"}, "coding_member_min_decode_tps", 20.0),
    ({"CODING_MEMBER_MIN_PREFILL_TPS": "150.5"}, "coding_member_min_prefill_tps", 150.5),
    ({"CODING_MEMBER_RAM_RESERVE_GB": "4"}, "coding_member_ram_reserve_gb", 4.0),
    ({"CODING_MEMBER_ESTIMATE_MARGIN": "1.5"}, "coding_member_estimate_margin", 1.5),
    ({"CODING_MEMBER_MIN_DECODE_TPS": "0"}, "coding_member_min_decode_tps", 15.0),
    ({"CODING_MEMBER_MIN_DECODE_TPS": "-3"}, "coding_member_min_decode_tps", 15.0),
    ({"CODING_MEMBER_MIN_DECODE_TPS": "fast"}, "coding_member_min_decode_tps", 15.0),
    ({"CODING_MEMBER_MIN_DECODE_TPS": "nan"}, "coding_member_min_decode_tps", 15.0),
    ({"CODING_MEMBER_MIN_PREFILL_TPS": "1e9"}, "coding_member_min_prefill_tps", 80.0),
    ({"CODING_MEMBER_RAM_RESERVE_GB": "0"}, "coding_member_ram_reserve_gb", 6.0),
    ({"CODING_MEMBER_ESTIMATE_MARGIN": "0.5"}, "coding_member_estimate_margin", 1.2),
    ({"CODING_MEMBER_ESTIMATE_MARGIN": "9"}, "coding_member_estimate_margin", 1.2),
])
def test_the_coding_member_settings_are_configurable_and_typo_safe(monkeypatch, raw, key, expected):
    monkeypatch.setattr(engine_config, "get_config", lambda force_reload=False: raw)
    assert engine_config.get_worker_limits()[key] == expected


# ======================================================================================
# The join test, against a fake llama-server
# ======================================================================================

def timings(prompt_n=2010, prompt_ps=121.2, predicted_n=128, predicted_ps=32.5):
    """The ``timings`` object llama-server ends every answer with."""
    return {"cache_n": 0, "prompt_n": prompt_n, "prompt_ms": 1000.0 * prompt_n / prompt_ps, "prompt_per_token_ms": 8.2,
            "prompt_per_second": prompt_ps, "predicted_n": predicted_n, "predicted_ms": 1000.0 * predicted_n / predicted_ps,
            "predicted_per_token_ms": 30.7, "predicted_per_second": predicted_ps}


def answer(t=None):
    t = timings() if t is None else t
    return {"id": "chatcmpl-1", "object": "chat.completion", "model": "x",
            "choices": [{"index": 0, "finish_reason": "length", "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": t["prompt_n"], "completion_tokens": t["predicted_n"], "total_tokens": t["prompt_n"] + t["predicted_n"]},
            "timings": t}


def is_warmup(body):
    return body["max_tokens"] <= 4


def healthy(n, body):
    """What a healthy member says: the warm-up is quick, the long prompt gets the measured laptop figures."""
    if is_warmup(body):
        return 200, answer(timings(prompt_n=14, prompt_ps=50.0, predicted_n=4, predicted_ps=30.0)), 0
    return 200, answer(), 0


class FakeLlamaServer:
    """A llama-server stand-in on a loopback port. ``respond(call_number, request_body) -> (status, payload, delay_s)``."""

    def __init__(self, respond=healthy):
        self.requests = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append({"path": self.path, "body": body})
                status, payload, delay = respond(len(outer.requests), body)
                if delay:
                    time.sleep(delay)
                raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass    # the client gave up, which is what the timeout test wants

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def serve():
    servers = []

    def start(respond=healthy):
        servers.append(FakeLlamaServer(respond))
        return servers[-1]

    yield start
    for server in servers:
        server.close()


@pytest.fixture
def client():
    import httpx
    with httpx.Client() as c:
        yield c


def test_measure_reads_both_speeds_from_the_servers_timings(serve, client):
    server = serve()
    assert sel.measure(server.url, client) == (32.5, 121.2)


def test_what_the_join_test_sends(serve, client):
    server = serve()
    result = sel.run_join_test(server.url + "/", client)
    assert result.ok and result.reason == ""
    assert (result.decode_tps, result.prefill_tps) == (32.5, 121.2)
    assert (result.prompt_tokens, result.generated_tokens) == (2010, 128)
    warm, main = server.requests
    assert warm["path"] == main["path"] == "/v1/chat/completions"
    assert warm["body"]["max_tokens"] == 4
    body = main["body"]
    assert body["max_tokens"] == 128
    assert body["stream"] is False and body["cache_prompt"] is False
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    prompt = body["messages"][0]["content"]
    # About 2,000 tokens of code, a different nonce every run so nothing is answered from a cache.
    assert 7000 < len(prompt) < 10000 and "def " in prompt
    other = serve()
    sel.measure(other.url, client)
    assert other.requests[1]["body"]["messages"][0]["content"][:12] != prompt[:12]


def test_the_filler_is_the_same_varied_code_every_time():
    text = sel._filler_code(2000)
    assert text == sel._filler_code(2000)
    assert abs(len(text) / sel._CHARS_PER_TOKEN - 2000) < 150
    assert len({line for line in text.splitlines() if line.startswith("def ")}) > 15
    assert len(sel._filler_code(4000)) > 1.8 * len(text)


def test_a_member_that_clears_the_floors_qualifies(serve, client):
    server = serve()
    assert sel.meets_floor(*sel.measure(server.url, client), LIMITS)


def test_a_slow_member_measures_slow_and_does_not_qualify(serve, client):
    def slow(n, body):
        if is_warmup(body):
            return healthy(n, body)
        return 200, answer(timings(prompt_ps=61.0, predicted_ps=9.8)), 0
    decode, prefill = sel.measure(serve(slow).url, client)
    assert (decode, prefill) == (9.8, 61.0)
    assert not sel.meets_floor(decode, prefill, LIMITS)


@pytest.mark.parametrize("what, respond", [
    ("a server error", lambda n, body: (500, {"error": {"message": "boom"}}, 0)),
    ("still loading the model", lambda n, body: (503, {"error": {"message": "Loading model"}}, 0)),
    ("an answer that is not JSON", lambda n, body: (200, b"<html>not llama</html>", 0)),
    ("an answer with no timings", lambda n, body: (200, {"choices": [], "usage": {}}, 0)),
    ("timings that are not an object", lambda n, body: (200, {"timings": "fast"}, 0)),
    ("a list", lambda n, body: (200, [1, 2, 3], 0)),
])
def test_a_member_that_cannot_be_timed_does_not_qualify(serve, client, what, respond):
    server = serve(respond)
    assert sel.measure(server.url, client) == (0.0, 0.0)
    result = sel.run_join_test(server.url, client)
    assert not result.ok and result.reason
    assert not sel.meets_floor(*sel.measure(server.url, client), LIMITS)


def test_a_member_that_is_not_listening_does_not_qualify(client):
    assert sel.measure("http://127.0.0.1:9", client) == (0.0, 0.0)
    result = sel.run_join_test("http://127.0.0.1:9", client)
    assert not result.ok and result.reason.startswith("warm-up")


def test_a_failure_on_the_long_request_alone_is_a_failure(serve, client):
    def breaks_on_the_long_prompt(n, body):
        return healthy(n, body) if is_warmup(body) else (500, {"error": "context overflow"}, 0)
    result = sel.run_join_test(serve(breaks_on_the_long_prompt).url, client)
    assert not result.ok and "HTTP 500" in result.reason


@pytest.mark.parametrize("bad", [
    {"predicted_per_second": float("nan")}, {"predicted_per_second": 0}, {"predicted_per_second": -3.0},
    {"predicted_per_second": 1e9}, {"predicted_per_second": "32"}, {"predicted_per_second": None}, {"predicted_per_second": True},
    {"prompt_per_second": float("nan")}, {"prompt_per_second": 0}, {"prompt_per_second": -1}, {"prompt_per_second": 1e9},
    {"prompt_per_second": "121"}, {"prompt_per_second": None},
])
def test_an_implausible_number_is_not_a_measurement(serve, client, bad):
    def lies(n, body):
        if is_warmup(body):
            return healthy(n, body)
        t = timings()
        t.update(bad)
        # A server that reports a nonsense generation speed gets its short-answer fallback, which lies the same way.
        return 200, answer(t), 0
    assert sel.measure(serve(lies).url, client) == (0.0, 0.0)


def test_a_prompt_answered_from_a_cache_proves_nothing(serve, client):
    def cached(n, body):
        if is_warmup(body):
            return healthy(n, body)
        return 200, answer(timings(prompt_n=7, prompt_ps=9000.0)), 0     # llama.cpp only counts the tokens it had to read
    result = sel.run_join_test(serve(cached).url, client)
    assert not result.ok and "prompt_n=7" in result.reason
    assert sel.measure(serve(cached).url, client) == (0.0, 0.0)


def test_an_answer_too_short_to_time_is_timed_again_on_its_own(serve, client):
    def short_then_long(n, body):
        if is_warmup(body):
            return healthy(n, body)
        if "Count from 1" in body["messages"][0]["content"]:
            return 200, answer(timings(prompt_n=30, predicted_n=128, predicted_ps=28.0)), 0
        return 200, answer(timings(predicted_n=9, predicted_ps=400.0)), 0     # stopped after 9 tokens: its rate is noise
    server = serve(short_then_long)
    result = sel.run_join_test(server.url, client)
    assert result.ok
    assert (result.decode_tps, result.prefill_tps, result.generated_tokens) == (28.0, 121.2, 128)
    assert len(server.requests) == 3


def test_an_answer_that_stays_too_short_does_not_qualify(serve, client):
    def always_short(n, body):
        return healthy(n, body) if is_warmup(body) else (200, answer(timings(predicted_n=9, predicted_ps=400.0)), 0)
    result = sel.run_join_test(serve(always_short).url, client)
    assert not result.ok and "too short" in result.reason
    assert result.prefill_tps == 121.2     # what was read is still reported


def test_a_member_too_slow_to_finish_in_the_budget_does_not_qualify(serve, client):
    def stalls(n, body):
        return healthy(n, body) if is_warmup(body) else (200, answer(), 4.0)
    server = serve(stalls)
    started = time.monotonic()
    result = sel.run_join_test(server.url, client, budget_s=1.5)
    elapsed = time.monotonic() - started
    assert not result.ok and "request failed" in result.reason
    assert elapsed < 3.5, "the budget must cut the test off"
    assert sel.measure(server.url, client, budget_s=1.5) == (0.0, 0.0)


def test_the_budget_is_a_minute_by_default_and_fits_a_member_at_the_floors():
    assert sel.JOIN_TEST_BUDGET_S == 60.0
    floor_decode, floor_prefill = sel.floors(LIMITS)
    # 2,000 prompt tokens at the prefill floor, 128 generated at the decode floor, plus the warm-up.
    seconds = sel.JOIN_PROMPT_TOKENS / floor_prefill + sel.JOIN_GENERATED_TOKENS / floor_decode
    assert seconds < 40 and seconds + sel.WARMUP_BUDGET_S < sel.JOIN_TEST_BUDGET_S


class StubResponse:
    def __init__(self, status_code=200, payload=None, boom=None):
        self.status_code, self._payload, self._boom = status_code, payload, boom

    def json(self):
        if self._boom:
            raise self._boom
        return self._payload


class StubClient:
    """Anything with post(url, json=, timeout=) is a client."""

    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        response = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(response, Exception):
            raise response
        return response


def test_any_client_will_do_and_every_request_has_a_timeout():
    stub = StubClient(StubResponse(payload=answer(timings(prompt_n=14, predicted_n=4))), StubResponse(payload=answer()))
    assert sel.measure("http://member", stub) == (32.5, 121.2)
    assert [c["url"] for c in stub.calls] == ["http://member/v1/chat/completions"] * 2
    assert all(0 < c["timeout"] <= sel.JOIN_TEST_BUDGET_S for c in stub.calls)
    assert stub.calls[0]["timeout"] <= sel.WARMUP_BUDGET_S


@pytest.mark.parametrize("failure", [
    RuntimeError("anything"), TimeoutError("slow"), OSError("down"), ValueError("bad"),
    StubResponse(status_code=404), StubResponse(boom=ValueError("not json")), StubResponse(payload=None),
])
def test_whatever_goes_wrong_is_does_not_qualify_never_an_exception(failure):
    assert sel.measure("http://member", StubClient(failure)) == (0.0, 0.0)
    ok_then_failure = StubClient(StubResponse(payload=answer(timings(prompt_n=14, predicted_n=4))), failure)
    assert sel.measure("http://member", ok_then_failure) == (0.0, 0.0)


def test_a_client_without_post_does_not_qualify():
    assert sel.measure("http://member", object()) == (0.0, 0.0)


def test_the_result_says_how_long_it_took(serve, client):
    result = sel.run_join_test(serve().url, client)
    assert 0.0 <= result.seconds < 5.0


def test_a_member_chosen_and_measured_end_to_end(ladder, serve, client):
    """The path the member runtime follows: choose, test, step down on a miss, give up after two steps."""
    speeds = {Q6: (11.0, 140.0), Q4: (14.0, 100.0), IQ4: (22.0, 95.0)}       # the first two miss a floor, the third clears both
    tier, steps, qualified = sel.choose_tier(24, 32, ladder, LIMITS), 0, None
    while tier is not None:
        decode, prefill = speeds[tier["id"]]
        server = serve(lambda n, body, d=decode, p=prefill: healthy(n, body) if is_warmup(body) else (200, answer(timings(prompt_ps=p, predicted_ps=d)), 0))
        if sel.meets_floor(*sel.measure(server.url, client), LIMITS):
            qualified = tier["id"]
            break
        if steps == sel.MAX_STEP_DOWNS:
            break
        steps += 1
        tier = sel.next_tier_down(tier, 24, 32, ladder, LIMITS)
    assert (qualified, steps) == (IQ4, 2)
