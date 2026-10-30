"""
Wi-Fi and battery (2.6.4, engine/dock_policy.py; owner's rule of 2026-10-29): mains + wired is docked, mains + Wi-Fi
is docked only while the link passes this coordinator's test (round-trip median, jitter, losses, and the node's own
download figure), battery never. The heartbeat answer tells a Wi-Fi GPU the verdict and asks it for a download test.
"""
import asyncio
import time

import pytest

from engine import config as engine_config
from engine import coordinator, dock_policy, link_paths
from engine.registry import WorkerRegistry, stepping_aside
# The coordinator harness: its fixtures (env, and the autouse canary book) and helpers.
from tests.test_coding_endpoint import (  # noqa: F401 - fixtures are used by name
    _aged, _engine_runs_on, _heartbeat, canary_book, env, fakes, gpu_hw, measured,
)
from tests.test_coordinator_counter import FakeRequest

NOW = 1_800_000_000.0
LIMITS = dict(dock_policy.WIFI_DEFAULTS)
GOOD = [4.0, 5.0, 4.5, 6.0, 5.5, 4.2, 5.1, 4.8, 5.3, 4.9]          # a quiet home link


def wifi(**more):
    return {**gpu_hw(8.0, 32.0), "link": "wifi", "power": "ac", "docked": False, **more}


def fresh(mbps, age=0.0):
    return {"download_mbps": mbps, "reported_at": NOW - age}


# ======================================================================================
# The rule
# ======================================================================================

@pytest.mark.parametrize("facts, passed, docked", [
    ({"link": "wired", "power": "ac", "docked": True}, False, True),
    ({"link": "wifi", "power": "ac", "docked": False}, False, False),      # not tested, or failed
    ({"link": "wifi", "power": "ac", "docked": False}, True, True),        # passed
    ({"link": "wired", "power": "battery", "docked": False}, True, False),  # never on battery
    ({"link": "wifi", "power": "battery", "docked": False}, True, False),
    ({"power": "battery"}, False, False),                                  # battery said even with no link
    ({"link": "cellular", "power": "ac", "docked": False}, True, False),
    ({"power": "ac"}, False, None),                                        # could not say: not held back
    ({}, False, None),
])
def test_the_rule(facts, passed, docked):
    assert dock_policy.effective_docked(facts, passed) is docked


def test_stepping_aside_follows_the_rule():
    assert stepping_aside(wifi()) is True
    assert stepping_aside(wifi(), wifi_passed=True) is False
    assert stepping_aside(wifi(power="battery"), wifi_passed=True) is True
    assert stepping_aside({**gpu_hw(), "docked": False}) is True, "an older worker's word still counts"
    assert stepping_aside(gpu_hw()) is False


# ======================================================================================
# The test of a Wi-Fi link
# ======================================================================================

def judge(samples, measurement=None, max_rtt=80.0, **limits):
    return dock_policy.judge_wifi(samples, measurement if measurement is not None else fresh(300.0),
                                  max_rtt_ms=max_rtt, limits={**LIMITS, **limits}, now=NOW)


def test_a_quiet_fast_link_passes_and_says_its_figures():
    verdict = judge(GOOD)
    assert verdict["ok"] is True
    assert verdict["why"] == "round trip 5 ms, jitter 1 ms, download 300 Mbit/s"
    assert (verdict["lost"], verdict["download_mbps"], verdict["checked_at"]) == (0, 300.0, NOW)
    assert verdict["samples_ms"] == GOOD


@pytest.mark.parametrize("samples, measurement, why", [
    (GOOD[:9] + [None], None, "1 of 10 round trips lost"),
    ([90.0] * 10, None, "round trip 90 ms, over 80"),
    (GOOD[:8] + [60.0, 70.0], None, "jitter 55 ms, over 20"),
    (GOOD, {}, dock_policy.WAITING_FOR_DOWNLOAD),
    (GOOD, fresh(500.0, age=dock_policy.WIFI_DOWNLOAD_MAX_AGE_SECONDS + 1), dock_policy.WAITING_FOR_DOWNLOAD),
    (GOOD, fresh(42.0), "download 42 Mbit/s, under 100"),
    ([None] * 10, None, "no round trip answered"),
])
def test_each_limit_fails_the_link_and_says_which(samples, measurement, why):
    verdict = judge(samples, measurement)
    assert verdict["ok"] is False and verdict["why"] == why


def test_jitter_is_the_90th_percentile_above_the_median():
    """One spike in ten is the 90th percentile itself; two in twenty is not over it."""
    assert judge(GOOD[:9] + [40.0])["ok"] is True, "the top sample alone is above the 90th percentile"
    assert judge(GOOD[:8] + [40.0, 40.0])["why"].startswith("jitter 35 ms")


def test_the_limits_are_settings():
    assert judge(GOOD[:8] + [60.0, 70.0], wifi_max_jitter_ms=100.0)["ok"] is True
    assert judge(GOOD, fresh(42.0), wifi_min_download_mbps=40.0)["ok"] is True
    assert judge([90.0] * 10, max_rtt=100.0)["ok"] is True


def test_settled_means_final_for_now():
    assert dock_policy.verdict_settled(judge(GOOD)) is True
    assert dock_policy.verdict_settled(judge([90.0] * 10)) is True
    assert dock_policy.verdict_settled(judge(GOOD, {})) is False, "the download figure is still to come"
    assert dock_policy.verdict_settled({"ok": None, "why": dock_policy.NOT_TESTED}) is False
    assert dock_policy.verdict_settled({}) is False


def test_the_settings_are_read_from_config(monkeypatch):
    monkeypatch.setattr(engine_config, "get_config", lambda force_reload=False: {
        "WIFI_SHARD": "false", "WIFI_MAX_JITTER_MS": "30", "WIFI_MIN_DOWNLOAD_MBPS": "50", "WIFI_PROBE_SAMPLES": "40"})
    limits = engine_config.get_worker_limits()
    assert (limits["wifi_shard"], limits["wifi_max_jitter_ms"], limits["wifi_min_download_mbps"],
            limits["wifi_probe_samples"]) == (False, 30.0, 50.0, 40)


@pytest.mark.parametrize("settings", [{"WIFI_MAX_JITTER_MS": "0"}, {"WIFI_MAX_JITTER_MS": "nan"},
                                      {"WIFI_MIN_DOWNLOAD_MBPS": "fast"}, {"WIFI_PROBE_SAMPLES": "2"},
                                      {"WIFI_PROBE_SAMPLES": "500"}])
def test_a_bad_wifi_setting_keeps_the_default(monkeypatch, settings):
    monkeypatch.setattr(engine_config, "get_config", lambda force_reload=False: dict(settings))
    limits = engine_config.get_worker_limits()
    assert {k: limits[k] for k in dock_policy.WIFI_DEFAULTS} == dock_policy.WIFI_DEFAULTS


# ======================================================================================
# The registry keeps one verdict per Wi-Fi link
# ======================================================================================

def test_a_verdict_lives_as_long_as_the_wifi_link_it_was_made_on():
    registry = WorkerRegistry()
    registry.update_worker("lap", "10.0.0.9", 50052, "t", wifi())
    pending = registry.get_wifi_check("lap")
    assert pending["ok"] is None and pending["why"] == dock_policy.NOT_TESTED and "since" in pending
    assert registry.worker_stepping_aside("lap", wifi()) is True, "not tested: not docked"
    registry.record_wifi_check("lap", judge(GOOD))
    assert registry.wifi_passed("lap") and registry.worker_stepping_aside("lap", wifi()) is False
    assert registry.get_wifi_check("lap")["since"] == pending["since"], "how long it has been on Wi-Fi is kept"
    registry.update_worker("lap", "10.0.0.9", 50052, "t", wifi())             # another heartbeat on Wi-Fi
    assert registry.wifi_passed("lap")
    registry.update_worker("lap", "10.0.0.9", 50052, "t", wifi(link="wired", docked=True))
    assert registry.get_wifi_check("lap") == {}, "off Wi-Fi the verdict goes"
    registry.record_wifi_check("lap", judge(GOOD))
    assert registry.get_wifi_check("lap") == {}, "a verdict is only kept for a node on Wi-Fi"
    registry.update_worker("lap", "10.0.0.9", 50052, "t", wifi())
    assert registry.get_wifi_check("lap")["ok"] is None, "back on Wi-Fi: tested afresh"
    registry.update_worker("lap", "10.0.0.9", 50052, "t", wifi(power="battery"))
    assert registry.get_wifi_check("lap") == {}


def test_a_wifi_gpu_joins_the_plan_only_once_it_passes():
    registry = WorkerRegistry()
    registry.update_worker("lap", "10.0.0.9", 50052, "t", wifi())
    assert registry.get_active_rpc_endpoints() == []
    assert registry.plan_endpoints(1000.0) == []
    registry.record_wifi_check("lap", judge(GOOD))
    assert registry.get_active_rpc_endpoints() == ["10.0.0.9:50052"]
    assert registry.plan_endpoints(1000.0) == ["10.0.0.9:50052"]
    assert registry.endpoint_is_draining("10.0.0.9:50052") is False
    registry.record_wifi_check("lap", judge([90.0] * 10))
    assert registry.endpoint_is_draining("10.0.0.9:50052") is True, "a failed retest drains the shard"


def test_this_machines_own_gpus_need_no_wifi_test():
    registry = WorkerRegistry()
    registry.update_worker("self", "127.0.0.1", 50052, "t", wifi())
    assert registry.wifi_passed("self") and registry.get_active_rpc_endpoints() == ["127.0.0.1:50052"]


# ======================================================================================
# The coordinator: heartbeat answers and roles
# ======================================================================================

def test_a_wifi_gpu_is_told_it_is_being_tested_and_asked_for_a_download_test(env):
    measured(env, "lap", "10.0.0.60", 3.0, wifi())
    answer = _heartbeat("lap", wifi(), "10.0.0.60")
    assert answer["wifi_check"] == {"ok": None, "why": dock_policy.NOT_TESTED, "speed_test": True}
    assert answer["role"] is None, "no new job while its link is being tested"


def test_a_wifi_gpu_that_passes_shares_the_model(env):
    measured(env, "lap", "10.0.0.61", 3.0, wifi())
    env.registry.record_link_measurement("lap", {"download_mbps": 300.0})
    env.registry.record_wifi_check("lap", dock_policy.judge_wifi(
        GOOD, env.registry.get_link_measurement("lap"), max_rtt_ms=15.0, limits=LIMITS, now=time.time()))
    answer = _heartbeat("lap", wifi(), "10.0.0.61")
    assert answer["role"] == "rpc"
    assert answer["wifi_check"]["ok"] is True and answer["wifi_check"]["speed_test"] is False


def test_a_wifi_gpu_that_fails_is_judged_like_a_far_one(env):
    measured(env, "lap", "10.0.0.62", 3.0, wifi())
    env.registry.record_wifi_check("lap", judge(GOOD[:8] + [60.0, 70.0]))
    answer = _heartbeat("lap", wifi(), "10.0.0.62")
    assert answer["role"] == "coding", "8 GB: a whole model of its own instead"
    assert answer["wifi_check"]["ok"] is False and answer["wifi_check"]["why"].startswith("jitter")


def test_the_testing_grace_runs_out(env):
    measured(env, "lap", "10.0.0.63", 3.0, wifi())
    env.registry.wifi_checks["lap"]["since"] = time.time() - 600
    assert _heartbeat("lap", wifi(), "10.0.0.63")["role"] == "coding", "a link never tested is not docked"


def test_a_member_keeps_its_job_while_its_new_wifi_link_is_tested(env):
    measured(env, "mem", "10.0.0.64", 3.0, wifi(pool="coding", coding_ram_gb=30.0))
    assert _heartbeat("mem", wifi(pool="coding", coding_ram_gb=30.0), "10.0.0.64")["role"] == "coding"


def test_a_gpu_on_battery_never_shares_the_model(env):
    on_battery = {**gpu_hw(8.0, 32.0), "link": "wired", "power": "battery", "docked": False}
    measured(env, "lap", "10.0.0.65", 3.0, on_battery)
    answer = _heartbeat("lap", on_battery, "10.0.0.65")
    assert answer["role"] == "coding" and "wifi_check" not in answer


def test_a_shard_that_goes_onto_battery_drains(env, monkeypatch):
    _engine_runs_on(monkeypatch, "10.0.0.66:50053")
    wired = {**gpu_hw(8.0, 32.0), "link": "wired", "power": "ac", "docked": True}
    measured(env, "lap", "10.0.0.66", 3.0, wired)
    assert "draining" not in _heartbeat("lap", wired, "10.0.0.66")
    answer = _heartbeat("lap", {**wired, "power": "battery", "docked": False}, "10.0.0.66")
    assert answer["draining"] is True and answer["role"] == "rpc", "answers finish before it is moved"


def test_a_shard_is_not_asked_for_a_download_test(env, monkeypatch):
    _engine_runs_on(monkeypatch, "10.0.0.67:50053")
    measured(env, "lap", "10.0.0.67", 3.0, wifi())
    env.registry.record_wifi_check("lap", judge(GOOD))
    answer = _heartbeat("lap", wifi(), "10.0.0.67")
    assert answer["wifi_check"]["speed_test"] is False and "draining" not in answer


def test_wifi_sharding_can_be_switched_off(env):
    env.limits["wifi_shard"] = False
    measured(env, "lap", "10.0.0.68", 3.0, wifi())
    answer = _heartbeat("lap", wifi(), "10.0.0.68")
    assert answer["wifi_check"]["ok"] is False and answer["wifi_check"]["speed_test"] is False
    assert answer["role"] == "coding"


def test_only_a_wifi_gpu_hears_about_wifi(env):
    measured(env, "desk", "10.0.0.69", 3.0, {**gpu_hw(), "link": "wired", "power": "ac", "docked": True})
    assert "wifi_check" not in _heartbeat("desk", {**gpu_hw(), "link": "wired", "power": "ac", "docked": True},
                                          "10.0.0.69")


# ======================================================================================
# The coordinator: the probe and the download report
# ======================================================================================

def test_the_probe_times_a_wifi_gpu_many_times_and_files_a_verdict(env, monkeypatch):
    measured(env, "lap", "10.0.0.70", 3.0, wifi())
    env.registry.record_link_measurement("lap", {"download_mbps": 300.0})
    asked = []
    monkeypatch.setattr(link_paths, "measure_series", lambda endpoint, n: asked.append((endpoint, n)) or (GOOD, "lan"))
    monkeypatch.setattr(link_paths, "measure", lambda endpoint: pytest.fail("a Wi-Fi GPU gets the long test"))
    assert coordinator._probe_rpc_rtt("10.0.0.70:50053") == 4.0, "the best sample is its round trip"
    assert asked == [("10.0.0.70:50053", 20)]
    assert env.registry.get_wifi_check("lap")["ok"] is True and coordinator._probe_paths["10.0.0.70:50053"] == "lan"


def test_the_probe_leaves_a_wifi_shards_verdict_alone(env, monkeypatch):
    _engine_runs_on(monkeypatch, "10.0.0.71:50053")
    measured(env, "lap", "10.0.0.71", 3.0, wifi())
    env.registry.record_wifi_check("lap", judge(GOOD))
    monkeypatch.setattr(link_paths, "measure_series", lambda *a: pytest.fail("the shard's own traffic would be timed"))
    monkeypatch.setattr(link_paths, "measure", lambda endpoint: (3.5, "lan"))
    assert coordinator._probe_rpc_rtt("10.0.0.71:50053") == 3.5
    assert env.registry.get_wifi_check("lap")["ok"] is True


def test_a_probe_that_reaches_nothing_keeps_the_last_verdict(env, monkeypatch):
    measured(env, "lap", "10.0.0.72", 3.0, wifi())
    env.registry.record_wifi_check("lap", judge(GOOD))
    monkeypatch.setattr(link_paths, "measure_series", lambda *a: None)
    assert coordinator._probe_rpc_rtt("10.0.0.72:50053") is None
    assert env.registry.get_wifi_check("lap")["ok"] is True


def test_a_download_report_is_judged_at_once(env, monkeypatch):
    monkeypatch.setattr(coordinator.benchmark, "append_record", lambda record: True)   # never the install's log
    measured(env, "lap", "10.0.0.73", 3.0, wifi())
    env.registry.record_wifi_check("lap", dock_policy.judge_wifi(GOOD, {}, max_rtt_ms=15.0, limits=LIMITS,
                                                                 now=time.time()))
    assert env.registry.get_wifi_check("lap")["why"] == dock_policy.WAITING_FOR_DOWNLOAD
    request = FakeRequest(body={"measurement": {"download_mbps": 312.0}}, headers={"Authorization": "Bearer tok-lap"})
    assert asyncio.run(coordinator.worker_link_report(request)) == {"ok": True}
    assert env.registry.get_wifi_check("lap")["ok"] is True
    assert _heartbeat("lap", wifi(), "10.0.0.73")["role"] == "rpc"


# ======================================================================================
# The path the series is taken along
# ======================================================================================

def test_the_series_takes_the_engines_path():
    routes = link_paths.LanRoutes()
    routes.offer("203.0.113.5", 50052, ["192.168.40.9"])
    calls = []

    def connect(host, port, n):
        calls.append(host)
        return GOOD if host == "192.168.40.9" else None

    assert link_paths.measure_series("203.0.113.5:50052", 10, routes=routes, connect=connect,
                                     tunnel=lambda *a: pytest.fail("the LAN answered")) == (GOOD, "lan")
    assert link_paths.measure_series("198.51.100.4:50052", 10, routes=link_paths.LanRoutes(),
                                     connect=lambda *a: None, tunnel=lambda h, p, n: [9.0] * n) == ([9.0] * 10, "tunnel")
    assert link_paths.measure_series("198.51.100.4:50052", 10, routes=link_paths.LanRoutes(),
                                     connect=lambda *a: None, tunnel=lambda *a: None) is None
    assert link_paths.measure_series("no-port", 10) is None
