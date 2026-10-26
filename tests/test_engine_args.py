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


def test_engine_extra_args_reach_the_limits(monkeypatch):
    assert _limits(monkeypatch, {"ENGINE_EXTRA_ARGS": THEWALL})["engine_extra_args"][:2] == ["--n-cpu-moe", "5"]


def test_across_volunteers_gpus_only_the_card_placement_is_left_out():
    # Leaving out everything turned the coding model's thinking back on whenever a volunteer's
    # GPU joined (ladder research, 2026-10-26).
    assert engine_config.engine_args_without_placement(parse_engine_args(THEWALL)) == [
        "--threads", "4", "--threads-batch", "4", "--cpu-mask", "0xc3", "--cpu-strict", "1",
        "--chat-template-kwargs", '{"enable_thinking":false}']
    assert engine_config.engine_args_without_placement(["-ot", "exps=CPU", "--no-mmap", "-sm", "row"]) == ["--no-mmap"]
