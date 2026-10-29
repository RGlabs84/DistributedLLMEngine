"""ENGINE_CONTEXT and ENGINE_EXTRA_ARGS (2.3.3): the operator's tuning for a coding model the
automatic sizing cannot place, such as Qwen3.6 35B-A3B with some experts in RAM."""

import pytest

from engine import config as engine_config
from engine.config import parse_engine_args

THEWALL = ("--n-cpu-moe 5 -ts 23,17 --threads 4 --threads-batch 4 --cpu-mask 0xc3 --cpu-strict 1 "
           "--chat-template-kwargs '{\"enable_thinking\":false}'")


def test_the_production_line_parses_to_llama_server_arguments():
    assert parse_engine_args(THEWALL) == [
        "--n-cpu-moe", "5", "-ts", "23,17", "--threads", "4", "--threads-batch", "4", "--cpu-mask", "0xc3",
        "--cpu-strict", "1", "--chat-template-kwargs", '{"enable_thinking":false}']


def test_the_prompt_cache_and_checkpoint_limits_can_be_set():
    # what a slot keeps for a conversation it is not running: checkpoints (62.8 MiB each on Qwen3.6 35B-A3B) and saved states
    assert parse_engine_args("--ctx-checkpoints 6 --cache-ram 2048") == ["--ctx-checkpoints", "6", "--cache-ram", "2048"]
    assert parse_engine_args("--cache-ram 0") == ["--cache-ram", "0"]


@pytest.mark.parametrize("raw", [
    "--rpc 203.0.113.9:50052",          # not on the list: the engine's RPC wiring is the coordinator's
    "--n-cpu-moe 5 --model /etc/passwd",
    "-ts",                              # a flag missing its value
    "--n-cpu-moe --threads 4",
    "'unclosed",
])
def test_anything_off_the_list_drops_the_whole_setting(raw):
    assert parse_engine_args(raw) == []


def _limits(monkeypatch, values):
    monkeypatch.setattr(engine_config, "get_config", lambda force_reload=False: dict(values))
    return engine_config.get_worker_limits()


def test_defaults_leave_the_engine_alone(monkeypatch):
    limits = _limits(monkeypatch, {})
    assert limits["engine_context"] == 0 and list(limits["engine_extra_args"]) == []


@pytest.mark.parametrize("raw, expected", [("65536", 65536), ("0", 0), ("100", 0), ("lots", 0), ("99999999", 0)])
def test_engine_context_is_bounded(monkeypatch, raw, expected):
    assert _limits(monkeypatch, {"ENGINE_CONTEXT": raw})["engine_context"] == expected


def test_engine_slots_default_to_automatic(monkeypatch):
    assert _limits(monkeypatch, {})["engine_slots"] == 0


@pytest.mark.parametrize("raw, expected", [
    ("6", 6), ("0", 0), ("32", 32), (" 4 ", 4),
    ("33", 0), ("-1", 0), ("many", 0), ("2.5", 0), ("", 0),   # a typo keeps the engine automatic
])
def test_engine_slots_are_bounded(monkeypatch, raw, expected):
    assert _limits(monkeypatch, {"ENGINE_SLOTS": raw})["engine_slots"] == expected


def test_engine_slots_and_context_are_independent(monkeypatch):
    limits = _limits(monkeypatch, {"ENGINE_SLOTS": "6", "ENGINE_CONTEXT": "196608"})

    assert (limits["engine_slots"], limits["engine_context"]) == (6, 196608)
    assert _limits(monkeypatch, {"ENGINE_CONTEXT": "65536"})["engine_slots"] == 0   # TheWall today


def test_engine_extra_args_reach_the_limits(monkeypatch):
    assert _limits(monkeypatch, {"ENGINE_EXTRA_ARGS": THEWALL})["engine_extra_args"][:2] == ["--n-cpu-moe", "5"]


def test_across_volunteers_gpus_only_the_card_placement_is_left_out():
    # Leaving out everything turned the coding model's thinking back on whenever a volunteer's
    # GPU joined (ladder research, 2026-10-26).
    assert engine_config.engine_args_without_placement(parse_engine_args(THEWALL)) == [
        "--threads", "4", "--threads-batch", "4", "--cpu-mask", "0xc3", "--cpu-strict", "1",
        "--chat-template-kwargs", '{"enable_thinking":false}']
    assert engine_config.engine_args_without_placement(["-ot", "exps=CPU", "--no-mmap", "-sm", "row"]) == ["--no-mmap"]


# --- 2.6: the media card and the vision projector -----------------------------------------------

def test_media_and_vision_are_off_by_default(monkeypatch):
    limits = _limits(monkeypatch, {})
    assert (limits["media_gpu"], limits["engine_mmproj"], limits["engine_image_max_tokens"]) == ("", "", 1024)


def test_media_card_and_projector_are_read_as_written_without_the_spaces(monkeypatch):
    limits = _limits(monkeypatch, {"MEDIA_GPU": " 0000:0c:00.0 ", "ENGINE_MMPROJ": " ~/models/mmproj-F16.gguf "})
    assert limits["media_gpu"] == "0000:0c:00.0" and limits["engine_mmproj"] == "~/models/mmproj-F16.gguf"


@pytest.mark.parametrize("raw, expected", [("768", 768), ("1", 1), ("8192", 8192), ("0", 1024), ("8193", 1024),
                                           ("-5", 1024), ("many", 1024), ("", 1024)])
def test_the_image_budget_keeps_its_default_on_a_typo(monkeypatch, raw, expected):
    assert _limits(monkeypatch, {"ENGINE_IMAGE_MAX_TOKENS": raw})["engine_image_max_tokens"] == expected


@pytest.mark.parametrize("raw, expected", [("30", 30), ("0", 0), ("600", 600), (" 5 ", 5),
                                           ("601", 30), ("-1", 30), ("lots", 30), ("1.5", 30), ("", 30)])
def test_the_image_input_budget_is_bounded_and_a_typo_keeps_the_default(monkeypatch, raw, expected):
    assert _limits(monkeypatch, {"IMAGE_INPUT_PER_HOUR": raw})["image_input_per_hour"] == expected


def test_the_image_input_budget_defaults_to_30_an_hour(monkeypatch):
    assert _limits(monkeypatch, {})["image_input_per_hour"] == 30


def test_phone_lanes_are_off_by_default(monkeypatch):
    limits = _limits(monkeypatch, {})
    assert (limits["engine_phone_slots"], limits["engine_phone_context"]) == (0, 8192)


@pytest.mark.parametrize("raw, expected", [
    ("2", 2), ("0", 0), ("8", 8), (" 3 ", 3),
    ("9", 0), ("-1", 0), ("some", 0), ("1.5", 0), ("", 0),   # a typo keeps the phones off the engine
])
def test_engine_phone_slots_are_bounded(monkeypatch, raw, expected):
    assert _limits(monkeypatch, {"ENGINE_PHONE_SLOTS": raw})["engine_phone_slots"] == expected


@pytest.mark.parametrize("raw, expected", [
    ("8192", 8192), ("4096", 4096), ("65536", 65536),
    ("4095", 8192), ("65537", 8192), ("big", 8192), ("0", 8192),
])
def test_engine_phone_context_is_bounded(monkeypatch, raw, expected):
    assert _limits(monkeypatch, {"ENGINE_PHONE_CONTEXT": raw})["engine_phone_context"] == expected
