"""
The coding member's engine (engine/coding_member.py), docs/coding-members.md section 4.

A volunteer's worker downloads the assigned coding tier from the signed release only, runs it only
if every file matches its signed size and SHA-256, serves it with llama-server on loopback, and
reports itself in the heartbeat's ``coding`` block (section 2). The lifecycle tests run a tiny
Python script in place of llama-server and a fake downloader in place of the network; the proxy
tests talk to a real FastAPI stand-in for llama-server over loopback. No model, no network.
"""

import ast
import base64
import json
import logging
import math
import os
import socket
import subprocess
import sys
import threading
from types import SimpleNamespace

import httpx
import pytest

from engine import coding_member as cm
from engine import member_identity, no_trace
from engine.coding_member import CODING_SLOTS, CodingMember, coding_server_args, process_keyring
from tests.coding_member_fakes import (
    AliveProcess, FakeDownloader, FakeUpstream, Opener, arg_after, data_lines, free_port, make_tier,
    recorded_starts, seal_request, wait_for, write_fake_coding_binary,
)

DATA_A = b"GGUF coding model A " * 64
DATA_B = b"GGUF coding model B, a bit bigger " * 90
SENTINEL = "ZXQ-secret-prompt-7731"

BLOCK_KEYS = {"model", "tier", "ready", "slots", "busy", "decode_tps", "prefill_tps", "pubkey", "no_trace",
              "identity", "signed_at", "sig"}


def check_coding_block(block):
    """Section 2, as the coordinator will read it: exact keys, short strings, sane finite numbers,
    and a signature the coordinator's check accepts."""
    assert set(block) == BLOCK_KEYS
    assert member_identity.verify_block(block) == member_identity.trust_id(block["identity"])
    assert isinstance(block["signed_at"], int) and not isinstance(block["signed_at"], bool)
    assert len(base64.b64decode(block["identity"], validate=True)) == 32
    assert len(base64.b64decode(block["sig"], validate=True)) == 64
    for key in ("model", "tier"):
        assert block[key] is None or (isinstance(block[key], str) and 0 < len(block[key]) <= 128)
    assert isinstance(block["ready"], bool) and isinstance(block["no_trace"], bool)
    assert isinstance(block["slots"], int) and not isinstance(block["slots"], bool) and 0 <= block["slots"] <= 64
    assert block["slots"] >= 1 or block["ready"] is False, "no slots is only ever said by a member that is not ready"
    assert isinstance(block["busy"], int) and not isinstance(block["busy"], bool) and 0 <= block["busy"] <= block["slots"]
    for key in ("decode_tps", "prefill_tps"):
        assert isinstance(block[key], float) and math.isfinite(block[key]) and 0 <= block[key] <= 100000
    assert isinstance(block["pubkey"], str) and len(block["pubkey"]) <= 128
    assert len(base64.b64decode(block["pubkey"], validate=True)) == 32
    json.dumps(block)          # and it must travel as JSON


def settle(member, timeout=30.0):
    assert wait_for(lambda: not member._busy, timeout), "the member never finished converging"


class Clock:
    """A clock the test moves by hand (the backoff and the stable-run rules are about minutes)."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        import time as real
        real.sleep(min(seconds, 0.02))


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(cm, "time", SimpleNamespace(monotonic=c.monotonic, sleep=c.sleep))
    return c


# ======================================================================================
# A member with a fake llama-server binary and a fake downloader
# ======================================================================================

@pytest.fixture
def build(tmp_path):
    made = []

    def make(binary=True, crash=False, parts=None, folder=None, **kwargs):
        index = len(made)
        base = tmp_path / (folder or f"dlne{index}")
        record = str(tmp_path / f"starts{index}.jsonl")
        if binary:
            write_fake_coding_binary(str(base / "bin" / "llama-server"), record, crash=crash)
        parts = parts or [DATA_A]
        tier = make_tier("coder-a", parts)
        downloader = FakeDownloader({p["file"]: data for p, data in zip(tier["files"], parts)})
        measured = []

        def measure(url, client):
            measured.append(url)
            return 31.46, 118.04

        tiers = [tier]
        options = {"measure": measure, "poll_seconds": 0.05, "ready_timeout": 20.0, **kwargs}
        member = CodingMember(str(base), lambda: list(tiers), port=free_port(), download=downloader, **options)
        assert member.start(), "the proxy could not listen"
        env = SimpleNamespace(base=base, models=base / "models", record=record, tier=tier, tiers=tiers,
                              downloader=downloader, measured=measured, member=member)
        made.append(env)
        return env

    yield make
    for env in made:
        env.member.stop()


def serve(env, tier=None):
    env.member.assign(tier or env.tier)
    settle(env.member)
    assert env.member.status()["ready"] is True


# ======================================================================================
# The happy path
# ======================================================================================

def test_status_before_any_assignment_is_a_valid_block(build):
    block = build().member.status()
    check_coding_block(block)
    assert block["model"] is None and block["tier"] is None and block["ready"] is False
    assert block["slots"] == CODING_SLOTS == 2 and block["busy"] == 0
    assert block["decode_tps"] == 0.0 and block["prefill_tps"] == 0.0
    assert block["no_trace"] is True


def test_assigning_nothing_at_first_does_nothing(build):
    env = build()
    env.member.assign(None)
    assert env.member._busy is False and env.downloader.calls == []


def test_a_tier_is_downloaded_verified_and_served(build):
    env = build()
    serve(env)
    file = env.tier["file"]
    assert env.downloader.calls == [[file]]
    assert (env.models / file).read_bytes() == DATA_A
    block = env.member.status()
    check_coding_block(block)
    assert (block["model"], block["tier"], block["ready"]) == (file, "coder-a", True)
    assert (block["decode_tps"], block["prefill_tps"]) == (31.5, 118.0), "the join test, one decimal"
    assert len(recorded_starts(env.record)) == 1
    assert env.measured == [env.member.engine_url], "measured against the member's own llama-server, once"
    assert not [n for n in os.listdir(env.models) if n.endswith(".part")]


def test_llama_server_is_started_on_loopback_with_the_coding_settings(build):
    env = build()
    serve(env)
    argv = recorded_starts(env.record)[0]
    assert argv[:2] == ["-m", str(env.models / env.tier["file"])]
    assert arg_after(argv, "--host") == "127.0.0.1"
    assert arg_after(argv, "--port") == str(env.member.llama_port) != str(env.member.port)
    assert arg_after(argv, "-c") == "65536" and arg_after(argv, "-np") == "2"
    assert arg_after(argv, "--fit") == "on"
    assert "-ngl" not in argv and "--n-gpu-layers" not in argv, "a layer count would switch --fit off"
    assert "--no-webui" in argv and "--jinja" in argv
    noise = no_trace.LLAMA_NO_TRACE_ARGS
    assert noise and any(argv[i:i + len(noise)] == noise for i in range(len(argv))), "the no-trace arguments"


def test_the_context_is_split_evenly_between_the_slots(build):
    env = build()
    env.tier["context"] = 65537
    serve(env)
    assert arg_after(recorded_starts(env.record)[0], "-c") == "65536"


def test_a_split_model_downloads_every_part_and_loads_the_first(build):
    parts = [b"part one " * 40, b"part two " * 50, b"part three " * 30]
    env = build(parts=parts)
    serve(env)
    assert env.downloader.calls == [[p["file"] for p in env.tier["files"]]]
    for part, data in zip(env.tier["files"], parts):
        assert (env.models / part["file"]).read_bytes() == data
    assert arg_after(recorded_starts(env.record)[0], "-m") == str(env.models / env.tier["files"][0]["file"])
    assert env.member.status()["model"] == env.tier["files"][0]["file"]


def test_only_the_parts_that_are_missing_are_downloaded(build):
    parts = [b"part one " * 40, b"part two " * 50]
    env = build(parts=parts)
    os.makedirs(env.models)
    (env.models / env.tier["files"][0]["file"]).write_bytes(parts[0])
    serve(env)
    assert env.downloader.calls == [[env.tier["files"][1]["file"]]]


def test_a_verified_file_already_in_place_is_not_downloaded(build):
    env = build()
    os.makedirs(env.models)
    (env.models / env.tier["file"]).write_bytes(DATA_A)
    serve(env)
    assert env.downloader.calls == []


def test_the_server_gets_the_providers_environment_at_low_priority(build, monkeypatch):
    env = build(env_provider=lambda: {"PATH": os.environ["PATH"], "DLNE_TEST_GPU": "1"}, threads=6)
    seen = {}
    real_popen = cm.subprocess.Popen

    def spy(args, **kwargs):
        seen.update(kwargs, args=args)
        return real_popen(args, **kwargs)

    monkeypatch.setattr(cm.subprocess, "Popen", spy)
    priorities = []
    if hasattr(os, "setpriority"):
        monkeypatch.setattr(cm.os, "setpriority", lambda which, pid, value: priorities.append(value))
    serve(env)
    assert seen["env"]["DLNE_TEST_GPU"] == "1"
    assert seen["stdin"] == cm.subprocess.DEVNULL
    assert arg_after(seen["args"], "-t") == "6" and arg_after(seen["args"], "-tb") == "6"
    if hasattr(os, "setpriority"):
        assert priorities == [10]


def test_the_server_log_is_truncated_at_every_start(build, clock):
    env = build()
    serve(env)
    log = env.base / "logs" / "coding_server.log"
    log.write_text("an earlier run\n")
    env.member.process.kill()
    env.member.process.wait()
    env.member.assign(env.tier)          # notices the crash and backs off
    settle(env.member)
    clock.now += 61
    env.member.assign(env.tier)
    settle(env.member)
    assert env.member.status()["ready"] is True and len(recorded_starts(env.record)) == 2
    assert "an earlier run" not in log.read_text()


def test_assigning_nothing_stops_the_server(build):
    env = build()
    serve(env)
    process = env.member.process
    env.member.assign(None)
    settle(env.member)
    assert process.poll() is not None
    block = env.member.status()
    assert (block["model"], block["tier"], block["ready"]) == (None, None, False)
    assert (block["decode_tps"], block["prefill_tps"]) == (0.0, 0.0)
    check_coding_block(block)


def test_a_new_tier_replaces_the_running_one(build):
    env = build()
    tier_b = make_tier("coder-b", [DATA_B])
    env.tiers.append(tier_b)
    env.downloader.contents[tier_b["file"]] = DATA_B
    serve(env)
    first = env.member.process
    serve(env, tier_b)
    assert first.poll() is not None, "the old server is stopped"
    assert env.member.status()["tier"] == "coder-b" and env.member.status()["model"] == tier_b["file"]
    starts = recorded_starts(env.record)
    assert [arg_after(s, "-m") for s in starts] == [str(env.models / env.tier["file"]), str(env.models / tier_b["file"])]


def test_the_running_server_keeps_answering_while_the_next_model_downloads(build):
    env = build()
    tier_b = make_tier("coder-b", [DATA_B])
    env.tiers.append(tier_b)
    env.downloader.contents[tier_b["file"]] = DATA_B
    serve(env)
    release = threading.Event()
    seen = {}
    real = env.downloader.__call__

    def slow(files, dest, progress=None, **options):
        seen["alive_during_download"] = env.member._alive() and env.member.status()["tier"] == "coder-a"
        release.wait(10)
        return real(files, dest, progress, **options)

    env.member._download = slow
    env.member.assign(tier_b)
    assert wait_for(lambda: "alive_during_download" in seen)
    assert seen["alive_during_download"] is True and env.member.status()["ready"] is True
    release.set()
    settle(env.member)
    assert env.member.status()["tier"] == "coder-b"


def test_a_model_change_waits_for_the_requests_in_flight(build, monkeypatch):
    env = build()
    tier_b = make_tier("coder-b", [DATA_B])
    env.tiers.append(tier_b)
    env.downloader.contents[tier_b["file"]] = DATA_B
    serve(env)
    slot = env.member._reserve()
    env.member.assign(tier_b)
    assert wait_for(lambda: env.member.ready is False), "no new request is accepted while it drains"
    assert env.member._alive(), "the old server is still there for the request in flight"
    slot.release()
    settle(env.member)
    assert env.member.status()["tier"] == "coder-b"


# ======================================================================================
# What the coordinator may and may not ask for
# ======================================================================================

def test_the_tier_dict_the_coordinator_sends_is_accepted_when_it_restates_the_signed_one(build):
    env = build()
    serve(env, dict(env.tier))
    assert env.member.status()["tier"] == "coder-a"


def test_a_bare_tier_id_is_accepted(build):
    env = build()
    serve(env, "coder-a")


@pytest.mark.parametrize("edit", [
    lambda t: {**t, "sha256": "0" * 64, "files": [{**t["files"][0], "sha256": "0" * 64}]},
    lambda t: {**t, "file": "evil.gguf", "files": [{**t["files"][0], "file": "evil.gguf"}]},
    lambda t: {**t, "files": [{**t["files"][0], "size": 5}]},
    lambda t: {**t, "files": t["files"] + [dict(t["files"][0], file="extra.gguf")]},
])
def test_a_tier_that_differs_from_the_signed_release_is_refused(build, edit):
    env = build()
    env.member.assign(edit(env.tier))
    settle(env.member)
    assert env.downloader.calls == [] and recorded_starts(env.record) == []
    assert env.member.status()["ready"] is False and env.member._held_back("coder-a")


def test_a_tier_the_release_does_not_list_is_refused(build):
    env = build()
    env.member.assign({"id": "not-in-the-release", "file": "x.gguf"})
    settle(env.member)
    assert env.downloader.calls == [] and recorded_starts(env.record) == []


def test_a_tier_the_release_does_not_enable_is_refused(build):
    env = build()
    env.tier["enabled"] = False
    env.member.assign(env.tier)
    settle(env.member)
    assert env.downloader.calls == [] and recorded_starts(env.record) == []


@pytest.mark.parametrize("junk", [{}, {"id": ""}, {"id": 5}, 5, [], True, ""])
def test_an_assignment_that_names_no_tier_means_nothing(build, junk):
    env = build()
    env.member.assign(junk)
    settle(env.member)
    assert env.downloader.calls == [] and env.member.status()["ready"] is False


# ======================================================================================
# Failures and backoff
# ======================================================================================

def test_a_failed_download_backs_off_and_doubles(build, clock):
    env = build()
    env.downloader.result = False
    env.member.assign(env.tier)
    settle(env.member)
    assert len(env.downloader.calls) == 1
    env.member.assign(env.tier)
    settle(env.member)
    assert len(env.downloader.calls) == 1, "not tried again at the next heartbeat"
    clock.now += 61
    env.member.assign(env.tier)
    settle(env.member)
    assert len(env.downloader.calls) == 2
    clock.now += 61
    env.member.assign(env.tier)
    settle(env.member)
    assert len(env.downloader.calls) == 2, "the second failure waits twice as long"
    clock.now += 60
    env.member.assign(env.tier)
    settle(env.member)
    assert len(env.downloader.calls) == 3
    env.downloader.result = True
    clock.now += 500
    serve(env)
    assert env.member._backoff.get("coder-a") != (0, 0.0)


def test_a_downloader_that_raises_is_a_failure_not_a_crash(build, clock):
    env = build()

    def boom(files, dest, progress=None, **options):
        raise RuntimeError(f"disk on fire {SENTINEL}")

    env.member._download = boom
    env.member.assign(env.tier)
    settle(env.member)
    assert env.member._held_back("coder-a") and env.member.status()["ready"] is False


def test_a_download_that_does_not_verify_is_deleted_and_backs_off(build, clock):
    env = build()
    env.downloader.corrupt = True
    env.member.assign(env.tier)
    settle(env.member)
    assert not (env.models / env.tier["file"]).exists(), "a file that fails its checksum is deleted"
    assert recorded_starts(env.record) == [] and env.member._held_back("coder-a")


def test_not_enough_disk_space_means_no_download(build, monkeypatch, clock):
    env = build()
    monkeypatch.setattr(cm.shutil, "disk_usage", lambda path: SimpleNamespace(free=10, total=10, used=0))
    env.member.assign(env.tier)
    settle(env.member)
    assert env.downloader.calls == [] and env.member._held_back("coder-a")


def test_a_failure_that_will_not_fix_itself_waits_longer(build, clock):
    env = build()
    env.downloader.result, env.downloader.lasting = False, True
    env.member.assign(env.tier)
    settle(env.member)
    clock.now += 61
    env.member.assign(env.tier)
    settle(env.member)
    assert len(env.downloader.calls) == 1, "a minute is not enough when the downloader says a retry cannot help"
    clock.now += cm.BACKOFF_LASTING_SECONDS
    env.member.assign(env.tier)
    settle(env.member)
    assert len(env.downloader.calls) == 2


def test_the_download_is_given_the_members_cancel_event_and_reports_progress(build):
    env = build()
    serve(env)
    assert env.downloader.options == [{"cancel": env.member._stopped}]
    assert env.downloader.updates and env.downloader.updates[-1]["done"] == env.downloader.updates[-1]["total"]


def test_llama_server_settings_in_the_volunteers_environment_are_not_passed_on(build, monkeypatch):
    monkeypatch.setenv("LLAMA_ARG_LOG_FILE", "/tmp/everything.log")
    monkeypatch.setenv("LLAMA_ARG_SLOT_SAVE_PATH", "/tmp/slots")
    monkeypatch.setenv("DLNE_TEST_KEEP", "yes")
    env = build()
    seen = {}
    real_popen = cm.subprocess.Popen

    def spy(args, **kwargs):
        seen.update(kwargs)
        return real_popen(args, **kwargs)

    monkeypatch.setattr(cm.subprocess, "Popen", spy)
    serve(env)
    assert "LLAMA_ARG_LOG_FILE" not in seen["env"] and "LLAMA_ARG_SLOT_SAVE_PATH" not in seen["env"]
    assert seen["env"]["DLNE_TEST_KEEP"] == "yes" and "PATH" in seen["env"]


def test_settings_that_would_leave_a_trace_are_never_run_and_never_claimed_as_no_trace(build, clock, monkeypatch):
    env = build()
    monkeypatch.setattr(no_trace, "LLAMA_NO_TRACE_ARGS", [])
    assert env.member.status()["no_trace"] is False, "the heartbeat does not claim it"
    env.member.assign(env.tier)
    settle(env.member)
    assert recorded_starts(env.record) == [] and env.member._held_back("coder-a")
    assert env.member.status()["ready"] is False


def test_no_trace_is_claimed_while_the_settings_hold(build):
    assert build().member.status()["no_trace"] is True


def test_no_llama_server_binary_backs_off(build, clock):
    env = build(binary=False)
    env.member.assign(env.tier)
    settle(env.member)
    assert env.member.status()["ready"] is False and env.member._held_back("coder-a")


def test_a_server_that_dies_while_starting_backs_off(build, clock):
    env = build(crash=True)
    env.member.assign(env.tier)
    settle(env.member)
    assert len(recorded_starts(env.record)) == 1
    block = env.member.status()
    assert block["ready"] is False and block["model"] is None
    assert env.member._backoff["coder-a"][0] == 1
    env.member.assign(env.tier)
    settle(env.member)
    assert len(recorded_starts(env.record)) == 1, "not restarted at the next heartbeat"
    clock.now += 61
    env.member.assign(env.tier)
    settle(env.member)
    assert len(recorded_starts(env.record)) == 2 and env.member._backoff["coder-a"][0] == 2


def test_a_server_that_crashes_soon_after_becoming_ready_counts_as_a_failure(build, clock):
    env = build()
    serve(env)
    env.member.process.kill()
    env.member.process.wait()
    assert env.member.status()["ready"] is False
    env.member.assign(env.tier)
    settle(env.member)
    assert len(recorded_starts(env.record)) == 1 and env.member._backoff["coder-a"][0] == 1
    clock.now += 61
    env.member.assign(env.tier)
    settle(env.member)
    assert len(recorded_starts(env.record)) == 2 and env.member.status()["ready"] is True


def test_a_server_that_had_been_serving_for_a_while_restarts_at_once(build, clock):
    env = build()
    serve(env)
    clock.now += cm.STABLE_SECONDS + 5
    env.member.process.kill()
    env.member.process.wait()
    env.member.assign(env.tier)
    settle(env.member)
    assert len(recorded_starts(env.record)) == 2 and env.member.status()["ready"] is True
    assert "coder-a" not in env.member._backoff


def test_a_long_run_clears_earlier_failures(build, clock):
    env = build()
    env.member._backoff["coder-a"] = (3, 0.0)
    serve(env)
    clock.now += cm.STABLE_SECONDS + 1
    env.member.assign(env.tier)
    assert "coder-a" not in env.member._backoff


def test_a_slow_loading_server_is_waited_for_by_later_heartbeats(build, tmp_path):
    flag = tmp_path / "ready.flag"
    env = build(ready_timeout=0.3)
    write_fake_coding_binary(str(env.base / "bin" / "llama-server"), env.record, ready_flag=str(flag))
    env.member.assign(env.tier)
    settle(env.member)
    block = env.member.status()
    assert block["ready"] is False and block["tier"] == "coder-a", "started, not ready yet"
    flag.write_text("ok")
    env.member.ready_timeout = 20.0
    env.member.assign(env.tier)
    settle(env.member)
    assert env.member.status()["ready"] is True and len(recorded_starts(env.record)) == 1


# ======================================================================================
# The join test
# ======================================================================================

def test_a_failing_join_test_leaves_zero_speeds_and_is_retried_later(build, clock):
    calls = []

    def flaky(url, client):
        calls.append(url)
        if len(calls) == 1:
            raise RuntimeError(f"timings missing {SENTINEL}")
        return 20.0, 90.0

    env = build(measure=flaky)
    serve(env)
    block = env.member.status()
    assert block["ready"] is True and (block["decode_tps"], block["prefill_tps"]) == (0.0, 0.0)
    env.member.assign(env.tier)
    settle(env.member)
    assert len(calls) == 1, "not retried at once"
    clock.now += 31
    env.member.assign(env.tier)
    settle(env.member)
    assert len(calls) == 2
    assert (env.member.status()["decode_tps"], env.member.status()["prefill_tps"]) == (20.0, 90.0)
    env.member.assign(env.tier)
    settle(env.member)
    assert len(calls) == 2, "measured once it has worked"


def test_an_async_join_test_is_awaited(build):
    async def measure(url, client):
        assert isinstance(client, httpx.AsyncClient)
        return 12.0, 55.5

    env = build(measure=measure)
    serve(env)
    assert (env.member.status()["decode_tps"], env.member.status()["prefill_tps"]) == (12.0, 55.5)


@pytest.mark.parametrize("bad", [(float("nan"), 5.0), (-3.0, "x"), (None, None), (True, False), (float("inf"), 1e12)])
def test_speeds_in_the_block_are_always_finite_and_in_range(build, bad):
    env = build(measure=lambda url, client: bad)
    serve(env)
    check_coding_block(env.member.status())


def test_the_default_join_test_is_the_real_one_and_reads_llama_cpps_timings(build):
    """engine.coding_member_select.measure, against the fake binary's chat route (which answers with timings)."""
    env = build(measure=None)
    serve(env)
    assert (env.member.status()["decode_tps"], env.member.status()["prefill_tps"]) == (33.3, 222.2)


# ======================================================================================
# A server too slow for the floors is replaced by a smaller one
# ======================================================================================

@pytest.fixture
def ladder(build, monkeypatch):
    """Three tiers, a join test that gets faster as the model gets smaller, and a recording next_tier_down."""
    speeds = [(5.0, 20.0), (40.0, 200.0)]          # the first tier is too slow, the next is fine
    asked = []

    def measure(url, client):
        return speeds.pop(0) if len(speeds) > 1 else speeds[0]

    env = build(measure=measure, machine_provider=lambda: {"vram_gb": 8.0, "ram_gb": 21.5, "unified": False},
                limits_provider=lambda: {})
    for tier_id, data in (("coder-b", DATA_B), ("coder-c", b"GGUF coding model C " * 70)):
        tier = make_tier(tier_id, [data])
        env.tiers.append(tier)
        env.downloader.contents[tier["file"]] = data

    def next_down(failed, vram_gb, ram_gb, tiers, limits=None, **machine):
        asked.append((failed["id"], vram_gb, ram_gb, machine))
        order = [t["id"] for t in tiers]
        position = order.index(failed["id"])
        return tiers[position + 1] if position + 1 < len(tiers) else None

    monkeypatch.setattr(cm.select, "next_tier_down", next_down)
    # By default every tier fits the machine (best first), so only the join test changes anything.
    monkeypatch.setattr(cm.select, "candidate_tiers", lambda vram, ram, tiers, limits=None, **m: list(reversed(tiers)))
    env.asked, env.speeds = asked, speeds
    return env


def test_a_join_test_under_the_floors_steps_down_to_the_next_smaller_tier(ladder):
    env = ladder
    serve(env)                                         # coder-a: 5 tok/s writing, 20 reading: under 15 and 80
    assert env.member.status()["tier"] == "coder-a" and env.member.status()["ready"] is True
    assert env.asked == [("coder-a", 8.0, 21.5, {"unified": False})], "judged on what the machine could spare"
    env.member.assign(env.tier)                        # the coordinator still names coder-a
    settle(env.member)
    block = env.member.status()
    assert block["tier"] == "coder-b" and block["ready"] is True, "the smaller tier takes its place"
    assert [arg_after(s, "-m") for s in recorded_starts(env.record)] == [
        str(env.models / env.tier["file"]), str(env.models / env.tiers[1]["file"])]
    env.member.assign(env.tier)
    settle(env.member)
    assert len(recorded_starts(env.record)) == 2, "and it stays: coder-a is not tried again"


def test_the_member_steps_down_at_most_max_step_downs_times(ladder):
    env = ladder
    env.speeds[:] = [(5.0, 20.0), (6.0, 30.0), (7.0, 40.0)]       # every tier is too slow
    for _ in range(6):
        env.member.assign(env.tier)
        settle(env.member)
    assert cm.select.MAX_STEP_DOWNS == 2
    assert env.member.status()["tier"] == "coder-c", "the last tier it could try"
    assert [asked[0] for asked in env.asked] == ["coder-a", "coder-b"]
    assert len(recorded_starts(env.record)) == 3 and env.member._step_downs == 2


def test_a_tier_that_passes_the_floors_is_kept(ladder):
    env = ladder
    env.member._measure = lambda url, client: (31.4, 118.0)
    serve(env)
    env.member.assign(env.tier)
    settle(env.member)
    assert env.asked == [] and len(recorded_starts(env.record)) == 1


def test_there_is_nothing_to_step_down_to(ladder):
    env = ladder
    env.tiers[:] = env.tiers[:1]
    serve(env)
    env.member.assign(env.tier)
    settle(env.member)
    assert env.member.status()["tier"] == "coder-a" and env.member.status()["decode_tps"] == 5.0
    assert len(recorded_starts(env.record)) == 1, "it keeps serving, and its speeds keep it unrouted"


def test_without_figures_for_the_machine_a_slow_server_just_reports_its_speeds(build):
    env = build(measure=lambda url, client: (5.0, 20.0))
    serve(env)
    env.member.assign(env.tier)
    settle(env.member)
    assert env.member.status()["tier"] == "coder-a" and env.member._step_downs == 0
    assert len(recorded_starts(env.record)) == 1


def test_a_tier_the_machines_own_figures_rule_out_is_replaced_before_anything_is_downloaded(ladder, monkeypatch):
    env = ladder
    env.speeds[:] = [(40.0, 200.0)]
    seen = []
    big = env.tiers[2]                                   # coder-c, the biggest
    monkeypatch.setattr(cm.select, "candidate_tiers",
                        lambda vram, ram, tiers, limits=None, **m: seen.append((vram, ram, m)) or [env.tiers[1], env.tiers[0]])
    env.member.assign(big)
    settle(env.member)
    assert env.member.status()["tier"] == "coder-b", "the smartest tier that fits and is no bigger than asked"
    assert seen[0] == (8.0, 21.5, {"unified": False}), "judged on the RAM the machine can spare"
    assert env.downloader.calls == [[env.tiers[1]["file"]]], "the 'too big' tier was never downloaded"
    env.member.assign(big)
    settle(env.member)
    assert len(recorded_starts(env.record)) == 1, "and the coordinator naming it again changes nothing"


@pytest.mark.parametrize("candidates, asked_for, served", [
    ([], 2, "coder-c"),             # nothing fits by the estimate: the join test is the judge
    ([2, 1, 0], 2, "coder-c"),      # the asked-for tier fits
    ([2], 0, "coder-a"),            # only a bigger tier fits: the smaller one asked for is fine
])
def test_the_assignment_stands_when_the_figures_do_not_rule_it_out(ladder, monkeypatch, candidates, asked_for, served):
    env = ladder
    env.speeds[:] = [(40.0, 200.0)]
    monkeypatch.setattr(cm.select, "candidate_tiers",
                        lambda vram, ram, tiers, limits=None, **m: [env.tiers[i] for i in candidates])
    env.member.assign(env.tiers[asked_for])
    settle(env.member)
    assert env.member.status()["tier"] == served and env.member._replacement == {}


def test_a_machine_provider_that_fails_leaves_the_assignment_alone(ladder):
    env = ladder
    env.speeds[:] = [(40.0, 200.0)]

    def broken():
        raise OSError("no figures")

    env.member.machine_provider = broken
    env.member.assign(env.tiers[2])
    settle(env.member)
    assert env.member.status()["tier"] == "coder-c"


def test_the_machine_is_read_before_the_model_is_loaded(ladder):
    env = ladder
    order = []
    env.member.machine_provider = lambda: order.append(("machine", len(recorded_starts(env.record)))) or {
        "vram_gb": 8.0, "ram_gb": 21.5}
    serve(env)
    assert order and all(entry == ("machine", 0) for entry in order), "never after llama-server has started"


# ======================================================================================
# The owner's own load (a game, a render): the member gives way
# ======================================================================================

def wait_until_stopped(member):
    assert wait_for(lambda: member.process is None, 10), "llama-server was never stopped"


def test_yielding_while_idle_says_not_ready_at_once_and_gives_the_gpu_memory_back(build):
    env = build()
    serve(env)
    process = env.member.process
    env.member.set_yielding(True)
    block = env.member.status()                 # at once, before anything has been stopped
    assert (block["ready"], block["slots"], block["busy"]) == (False, 0, 0)
    check_coding_block(block)
    wait_until_stopped(env.member)
    assert wait_for(lambda: process.poll() is not None, 10), "the process is gone, its memory returned"
    assert wait_for(lambda: env.member.status()["model"] is None, 10) and env.member.status()["ready"] is False
    env.member.assign(env.tier)
    settle(env.member)
    assert len(recorded_starts(env.record)) == 1, "nothing is started while the owner's load lasts"
    refused = httpx.post(f"http://127.0.0.1:{env.member.port}/v1/chat/completions", json=CHAT, trust_env=False)
    assert refused.status_code == 503 and refused.json()["error"]["type"] == "not_ready"


def test_yielding_during_a_request_lets_it_finish_before_the_server_stops(proxy):
    threads, results = _hold_requests(proxy, 1)
    proxy.member.set_yielding(True)
    block = proxy.member.status()
    assert (block["ready"], block["slots"], block["busy"]) == (False, 0, 0), "the router stops sending at once"
    check_coding_block(block)
    refused = proxy.client.post("/v1/chat/completions", json=CHAT)
    assert refused.status_code == 503 and refused.json()["error"]["type"] == "not_ready"
    assert wait_for(lambda: len(proxy.up.received) >= 1, 10)
    assert len(proxy.up.received) == 1, "the new request never reached llama-server"
    assert proxy.member.process is not None, "the request in flight is not cut"
    proxy.up.gate.set()
    for thread in threads:
        thread.join(10)
    assert results == [200], "it finished"
    wait_until_stopped(proxy.member)


def test_a_request_that_will_not_finish_is_cut_after_ten_seconds(proxy, clock):
    threads, results = _hold_requests(proxy, 1)
    proxy.member.set_yielding(True)
    import time as real
    real.sleep(0.3)
    assert proxy.member.process is not None, "still inside the ten seconds"
    clock.now += cm.YIELD_DRAIN_SECONDS + 1
    wait_until_stopped(proxy.member)
    proxy.up.gate.set()
    for thread in threads:
        thread.join(10)


def test_a_load_that_ends_before_the_requests_do_keeps_the_server(proxy, clock):
    threads, results = _hold_requests(proxy, 1)
    proxy.member.set_yielding(True)
    proxy.member.set_yielding(False)
    proxy.up.gate.set()
    for thread in threads:
        thread.join(10)
    assert results == [200]
    import time as real
    real.sleep(0.4)
    assert proxy.member.process is not None, "no restart is needed"
    assert proxy.member.status()["ready"] is False, "but it still waits out the settle period"
    clock.now += cm.DEFAULT_RESUME_SECONDS + 1
    block = proxy.member.status()
    assert (block["ready"], block["slots"]) == (True, 2)


def test_after_the_load_ends_the_member_waits_then_restarts_and_measures_again(build, clock):
    env = build()
    serve(env)
    env.member.set_yielding(True)
    wait_until_stopped(env.member)
    env.member.set_yielding(False)
    block = env.member.status()
    assert (block["ready"], block["slots"]) == (False, 0), "settling"
    env.member.assign(env.tier)
    settle(env.member)
    assert len(recorded_starts(env.record)) == 1
    clock.now += cm.DEFAULT_RESUME_SECONDS - 1
    env.member.assign(env.tier)
    settle(env.member)
    assert len(recorded_starts(env.record)) == 1, "not before the settle period is over"
    clock.now += 2
    env.member.assign(env.tier)
    settle(env.member)
    block = env.member.status()
    assert len(recorded_starts(env.record)) == 2 and (block["ready"], block["slots"]) == (True, 2)
    assert len(env.measured) == 2, "health was checked and the speed measured again before it said ready"
    check_coding_block(block)


def test_a_load_that_comes_and_goes_costs_one_restart_per_settle_period_at_most(build, clock):
    env = build()
    serve(env)
    for _ in range(6):
        env.member.set_yielding(True)
        wait_until_stopped(env.member)
        clock.now += 3
        env.member.set_yielding(False)
        clock.now += 3
        env.member.assign(env.tier)
        settle(env.member)
    assert len(recorded_starts(env.record)) == 1, "the server stayed down through all of it"
    assert env.member.status()["ready"] is False
    clock.now += cm.DEFAULT_RESUME_SECONDS - 3                  # 30 s after the last time the load ended
    env.member.assign(env.tier)
    settle(env.member)
    assert len(recorded_starts(env.record)) == 2 and env.member.status()["ready"] is True
    # And the load comes back right after the restart: the next restart is a whole settle period away.
    env.member.set_yielding(True)
    wait_until_stopped(env.member)
    clock.now += 1
    env.member.set_yielding(False)
    clock.now += cm.DEFAULT_RESUME_SECONDS - 1
    env.member.assign(env.tier)
    settle(env.member)
    assert len(recorded_starts(env.record)) == 2
    clock.now += 2
    env.member.assign(env.tier)
    settle(env.member)
    assert len(recorded_starts(env.record)) == 3


@pytest.mark.parametrize("configured, expected", [(5, 5.0), ("12", 12.0), ("junk", 30.0), (None, 30.0), (-5, 0.0),
                                                  (99999, 3600.0)])
def test_the_settle_period_is_the_workers_topology_setting(build, configured, expected):
    member = build(limits_provider=lambda: {"topology_settle_seconds": configured}).member
    assert member._settle_seconds() == expected
    assert build().member._settle_seconds() == cm.DEFAULT_RESUME_SECONDS


def test_the_same_yield_twice_changes_nothing_and_a_stopped_member_ignores_it(build):
    env = build()
    serve(env)
    env.member.set_yielding(True)
    env.member.set_yielding(True)
    wait_until_stopped(env.member)
    env.member.stop()
    env.member.set_yielding(False)
    assert env.member._yielding is True


# ======================================================================================
# The volunteer's limits
# ======================================================================================

def test_the_volunteers_limits_become_launch_settings(build):
    env = build(resources_provider=lambda: {"threads": 3, "extra_args": ["--fit-target", "2048,3072"],
                                            "cache_ram_mib": 512})
    serve(env)
    argv = recorded_starts(env.record)[0]
    assert arg_after(argv, "-t") == "3" and arg_after(argv, "-tb") == "3"
    assert arg_after(argv, "--fit-target") == "2048,3072" and arg_after(argv, "--cache-ram") == "512"
    assert argv[-len(no_trace.LLAMA_NO_TRACE_ARGS):] == no_trace.LLAMA_NO_TRACE_ARGS, "the no-trace arguments stay last"


def test_without_limits_llama_cpps_own_defaults_stand(build):
    env = build(resources_provider=lambda: {"threads": 0, "extra_args": []}, threads=6)
    serve(env)
    argv = recorded_starts(env.record)[0]
    assert "-t" not in argv and "--fit-target" not in argv, "the provider's word is final: 0 threads means no cap"
    assert arg_after(argv, "--cache-ram") == str(cm.PROMPT_CACHE_MIB)


def test_limits_that_cannot_be_read_mean_the_server_is_not_started(build, clock):
    def broken():
        raise OSError("config.txt unreadable")

    env = build(resources_provider=broken)
    env.member.assign(env.tier)
    settle(env.member)
    assert recorded_starts(env.record) == [] and env.member._held_back("coder-a")
    assert env.member.status()["ready"] is False


@pytest.mark.parametrize("totals, limit, expected", [
    ([8.0], None, []),                       # no cap
    ([8.0], 8.0, []), ([8.0], 24.0, []),     # a cap that does not bind
    ([8.0], 6.0, [2048]),                    # 2 GiB of the card is left alone
    ([8.0], 7.5, [1024]),                    # never below llama.cpp's own margin
    ([8.0], 0.0, [8192]),
    ([8.0, 12.0], 10.0, [4096, 6144]),       # shared out in proportion to the cards
    ([], 4.0, []), ([0, -3, "x"], 4.0, []),  # no cards, no margins
])
def test_a_vram_cap_becomes_fit_target_margins(totals, limit, expected):
    assert cm.fit_targets_mib(totals, limit) == expected


def test_a_capped_machine_with_no_tier_that_fits_runs_nothing(ladder, monkeypatch, clock):
    env = ladder
    seen = []
    monkeypatch.setattr(cm.select, "candidate_tiers", lambda vram, ram, tiers, limits=None, **m: seen.append(m) or [])
    env.member.machine_provider = lambda: {"vram_gb": 6.0, "ram_gb": 10.0, "unified": False, "limited": True}
    env.member.assign(env.tier)
    settle(env.member)
    assert env.downloader.calls == [] and recorded_starts(env.record) == [] and env.member._held_back("coder-a")
    assert seen == [{"unified": False}], "the cap flag is ours: candidate_tiers is not given it"
    # The same empty answer without a cap leaves it to the join test, as before.
    env.member.machine_provider = lambda: {"vram_gb": 6.0, "ram_gb": 10.0, "unified": False}
    clock.now += 61
    env.speeds[:] = [(40.0, 200.0)]
    env.member.assign(env.tier)
    settle(env.member)
    assert env.member.status()["tier"] == "coder-a" and env.member.status()["ready"] is True


# ======================================================================================
# "Both": the phone-pool model of the same machine loads first
# ======================================================================================

def test_the_coding_model_waits_for_the_pool_model_before_it_loads_and_measures(build):
    gate = threading.Event()
    env = build(start_gate=gate)
    env.member.assign(env.tier)
    assert wait_for(lambda: env.downloader.calls != [], 10), "the files can be fetched in the meantime"
    import time as real
    real.sleep(0.5)
    assert recorded_starts(env.record) == [] and env.member.status()["ready"] is False, "but nothing is loaded yet"
    gate.set()
    settle(env.member)
    assert len(recorded_starts(env.record)) == 1 and env.member.status()["ready"] is True


def test_a_gate_that_is_already_open_or_absent_changes_nothing(build):
    open_gate = threading.Event()
    open_gate.set()
    for env in (build(start_gate=open_gate), build()):
        serve(env)
        assert len(recorded_starts(env.record)) == 1


def test_stopping_the_member_ends_the_wait_for_the_gate(build):
    env = build(start_gate=threading.Event())
    env.member.assign(env.tier)
    assert wait_for(lambda: env.downloader.calls != [], 10)
    env.member.stop()
    settle(env.member, 10)
    assert recorded_starts(env.record) == []


# ======================================================================================
# Stopping
# ======================================================================================

def test_stop_ends_the_server_and_closes_the_proxy(build):
    env = build()
    serve(env)
    process, port = env.member.process, env.member.port
    env.member.stop()
    assert process.poll() is not None
    assert env.member.status()["ready"] is False
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
    env.member.assign(env.tier)
    assert env.member._busy is False, "a stopped member ignores assignments"
    env.member.stop()        # and stopping again is harmless


def test_stop_cancels_a_download_in_progress(build):
    env = build()
    from engine.downloader import DownloadResult
    seen = {}
    started = threading.Event()

    def endless(files, dest, progress=None, **options):
        started.set()
        seen["cancel"] = options["cancel"]
        seen["was_cancelled"] = options["cancel"].wait(10)
        return DownloadResult(False, "cancelled", cancelled=True)

    env.member._download = endless
    env.member.assign(env.tier)
    assert started.wait(5)
    env.member.stop()
    settle(env.member, 10)
    assert seen["was_cancelled"] is True and seen["cancel"] is env.member._stopped, "stop() sets the downloader's cancel event"
    assert recorded_starts(env.record) == [], "nothing is started after a stop"
    assert env.member.status()["ready"] is False
    assert "coder-a" not in env.member._backoff, "a stop is not a failure of the tier"


def test_the_proxy_cannot_listen_on_a_port_that_is_taken(tmp_path):
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        member = CodingMember(str(tmp_path), lambda: [], port=taken.getsockname()[1])
        assert member.start() is False
        member.stop()


def test_the_proxy_listens_on_loopback_only(build):
    env = build()
    assert env.member._server.config.host == "127.0.0.1"


# ======================================================================================
# The key pair, and the arguments
# ======================================================================================

def test_the_pubkey_is_the_key_rings_and_one_per_process(build):
    a, b = build().member, build().member
    ring = process_keyring()
    assert a.keyring is b.keyring is ring
    assert a.status()["pubkey"] == b.status()["pubkey"] == ring.public_b64, "published exactly as the ring returns it"
    assert len(base64.b64decode(ring.public_b64, validate=True)) == 32


def test_every_status_asks_the_key_ring_whether_it_is_due_for_rotation(build, monkeypatch):
    member = build().member
    asked = []
    monkeypatch.setattr(member.keyring, "maybe_rotate", lambda: asked.append(1) or False)
    member.status()
    member.status()
    assert len(asked) == 2


def test_the_pubkey_in_the_block_follows_a_rotation(build):
    member = build(keyring=cm.KeyRing()).member
    before = member.status()["pubkey"]
    member.keyring.rotate()
    after = member.status()
    assert after["pubkey"] != before and after["pubkey"] == member.keyring.public_b64
    check_coding_block(after)


def test_a_member_can_be_given_its_own_keyring_and_identity(tmp_path):
    class Ring:
        public_b64 = "cHVi"

        def maybe_rotate(self):
            return False

    identity = member_identity.load_or_create_identity(str(tmp_path / "other.key"))
    member = CodingMember(str(tmp_path), lambda: [], port=free_port(), keyring=Ring(), identity=identity)
    assert member.pubkey == "cHVi" and member.status()["pubkey"] == "cHVi"
    assert member.status()["identity"] == identity[1] and member.trust_id == member_identity.trust_id(identity[1])
    assert not os.path.exists(tmp_path / "config"), "a given identity is not read from, or written to, the install"


# ======================================================================================
# The identity key and the signature on the block (sections 1 and 2)
# ======================================================================================

def test_the_identity_key_is_made_once_kept_private_and_reused(build):
    first = build().member
    path = os.path.join(first.base_dir, "config", "coding_identity.key")
    assert os.path.isfile(path)
    if os.name == "posix":
        assert os.stat(path).st_mode & 0o777 == 0o600
    again = CodingMember(first.base_dir, lambda: [], port=free_port())
    assert (again.identity_pub, again.trust_id) == (first.identity_pub, first.trust_id)
    priv_b64 = open(path).read().strip()
    assert priv_b64 not in json.dumps(first.status()) and priv_b64 not in json.dumps(again.status())
    assert member_identity.verify_block(again.status()) == first.trust_id


def test_a_damaged_identity_key_is_refused_not_replaced(tmp_path):
    base = tmp_path / "dlne"
    (base / "config").mkdir(parents=True)
    path = base / "config" / "coding_identity.key"
    path.write_text("this is not a key\n")
    with pytest.raises(member_identity.IdentityError):
        CodingMember(str(base), lambda: [], port=free_port())
    assert path.read_text() == "this is not a key\n", "a new key would silently change the trust id"


def test_the_block_is_signed_over_the_canonical_json_of_everything_but_sig(build):
    """The contract's recipe, applied by hand with the cryptography library."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    member = build().member
    block = member.status()
    unsigned = {k: v for k, v in block.items() if k != "sig"}
    message = json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    Ed25519PublicKey.from_public_bytes(base64.b64decode(block["identity"])).verify(base64.b64decode(block["sig"]), message)
    assert {"pubkey", "identity", "signed_at", "no_trace"} <= set(unsigned), "the end-to-end key is inside the signature"


def test_a_copied_block_with_a_swapped_end_to_end_key_does_not_verify(build):
    block = build().member.status()
    assert member_identity.verify_block({**block, "pubkey": "A" * 43 + "="}) is None


def test_the_trust_id_is_logged_once_at_start_and_shown_on_health(tmp_path, monkeypatch):
    lines = []
    monkeypatch.setattr(cm, "log_system", lambda text: lines.append(text))
    member = CodingMember(str(tmp_path), lambda: [], port=free_port())
    try:
        assert member.start()
        mentions = [line for line in lines if member.trust_id in line]
        assert len(mentions) == 1 and "TRUSTED_MEMBERS" in mentions[0]
        assert len(member.trust_id) == 32 and set(member.trust_id) <= set("0123456789abcdef")
        health = httpx.get(f"http://127.0.0.1:{member.port}/health", trust_env=False)
        assert health.json()["trust_id"] == member.trust_id
        assert member.trust_id not in json.dumps(member.status()), "the heartbeat block has no extra keys"
    finally:
        member.stop()


def test_server_arguments(tmp_path):
    args = coding_server_args("/bin/llama-server", "/m/x.gguf", 5555, ctx=32768, slots=2)
    assert args[:3] == ["/bin/llama-server", "-m", "/m/x.gguf"]
    assert arg_after(args, "--fit") == "on" and arg_after(args, "-c") == "32768" and arg_after(args, "-np") == "2"
    assert arg_after(args, "--host") == "127.0.0.1" and arg_after(args, "--port") == "5555"
    assert "-ngl" not in args and "-t" not in args
    assert args[-len(no_trace.LLAMA_NO_TRACE_ARGS):] == no_trace.LLAMA_NO_TRACE_ARGS
    assert arg_after(coding_server_args("b", "m", 1, threads=4), "-t") == "4"


# ======================================================================================
# The member proxy
# ======================================================================================

@pytest.fixture
def proxy(tmp_path):
    up = FakeUpstream("up").start()
    member = CodingMember(str(tmp_path), lambda: [], port=free_port(), llama_port=up.port, keyring=cm.KeyRing())
    assert member.start()
    member.process, member.ready = AliveProcess(), True
    member.serving, member.model = "coder-a", "coder-a-Q4.gguf"
    client = httpx.Client(base_url=f"http://127.0.0.1:{member.port}", trust_env=False, timeout=15.0)
    yield SimpleNamespace(up=up, member=member, client=client)
    client.close()
    member.stop()
    up.stop()


CHAT = {"model": "anything", "messages": [{"role": "user", "content": "hi"}], "echo": SENTINEL}


def test_health(proxy):
    assert proxy.client.get("/health").json() == {"status": "ok", "ready": True, "slots": 2, "busy": 0,
                                                  "trust_id": proxy.member.trust_id}
    proxy.member.ready = False
    response = proxy.client.get("/health")
    assert response.status_code == 503 and response.json()["status"] == "loading"


def test_a_plain_chat_request_goes_through_byte_for_byte(proxy):
    sent = json.dumps(CHAT, separators=(",", ":")).encode()
    response = proxy.client.post("/v1/chat/completions", content=sent, headers={"content-type": "application/json"})
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "up-answer"
    assert [r["path"] for r in proxy.up.received] == ["/v1/chat/completions"]
    assert proxy.up.received[0]["body"] == sent


def test_a_plain_completions_request_goes_through(proxy):
    response = proxy.client.post("/v1/completions", json={"prompt": "def f(", "max_tokens": 4})
    assert response.status_code == 200 and response.json()["choices"][0]["message"]["content"] == "up-answer"
    assert proxy.up.received[0]["path"] == "/v1/completions"


# ------------------------------------------------------------------ SR-08: only the coordinator's tunnel

@pytest.fixture
def tunnel(proxy):
    """A byte pipe from a port of its own to the member's port: what the reverse tunnel does, so the Host the
    coordinator's httpx client writes (127.0.0.1:<relay port>, not the member's port) reaches the member."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    stop = threading.Event()

    def pipe(src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def accept():
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                client, _ = listener.accept()
            except OSError:
                continue
            upstream = socket.create_connection(("127.0.0.1", proxy.member.port))
            threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()
            threading.Thread(target=pipe, args=(upstream, client), daemon=True).start()

    threading.Thread(target=accept, daemon=True).start()
    proxy.relay_port = listener.getsockname()[1]
    yield proxy
    stop.set()
    listener.close()


def test_the_coordinators_requests_through_the_tunnel_pass_whatever_port_they_name(tunnel):
    base = f"http://127.0.0.1:{tunnel.relay_port}"
    assert tunnel.relay_port != tunnel.member.port
    with httpx.Client(base_url=base, trust_env=False, timeout=15.0) as coordinator:       # no Origin, Host 127.0.0.1:<relay>
        assert coordinator.get("/health").status_code == 200
        plain = coordinator.post("/v1/chat/completions", json=CHAT)
        assert plain.status_code == 200 and plain.json()["choices"][0]["message"]["content"] == "up-answer"
        with coordinator.stream("POST", "/v1/chat/completions", json={**CHAT, "stream": True}) as response:
            assert b"".join(response.iter_raw()) == b"".join(tunnel.up.chunks)
        envelope, key = seal_request(CHAT, tunnel.member.pubkey)
        assert coordinator.post("/v1/chat/completions", json=envelope).status_code == 200


@pytest.mark.parametrize("origin", ["https://evil.example", "http://127.0.0.1:50052", "http://localhost", "null", ""])
@pytest.mark.parametrize("method, path", [("POST", "/v1/chat/completions"), ("POST", "/v1/completions"), ("GET", "/health")])
def test_a_request_with_an_origin_header_is_turned_away(proxy, origin, method, path):
    """A web page in the owner's browser: a cross-site POST always carries Origin (even "null")."""
    response = proxy.client.request(method, path, json=CHAT if method == "POST" else None, headers={"Origin": origin})
    assert response.status_code == 421
    assert origin not in response.text or not origin, "nothing the page sent is repeated"
    assert proxy.up.received == [] and proxy.member.status()["busy"] == 0


def test_an_origin_is_turned_away_through_the_tunnel_too(tunnel):
    with httpx.Client(base_url=f"http://127.0.0.1:{tunnel.relay_port}", trust_env=False, timeout=15.0) as client:
        assert client.post("/v1/chat/completions", json=CHAT, headers={"Origin": "https://evil.example"}).status_code == 421
        assert client.post("/v1/chat/completions", json=CHAT).status_code == 200
    assert len(tunnel.up.received) == 1


@pytest.mark.parametrize("host", ["127.0.0.1:50052", "127.0.0.1", "localhost:8080", "LOCALHOST:8080", "[::1]:50052",
                                  "[::1]", "127.0.0.1:65535", "127.0.0.1:1"])
def test_loopback_names_pass(proxy, host):
    assert proxy.client.post("/v1/chat/completions", json=CHAT, headers={"Host": host}).status_code == 200


@pytest.mark.parametrize("host", [
    "evil.example", "evil.example:50052", "127.0.0.1.evil.example:50052", "localhost.evil.example",
    "evil.example#127.0.0.1", "0.0.0.0:50052", "127.0.0.2:50052", "[::2]:1", "127.0.0.1:0", "127.0.0.1:99999",
    "127.0.0.1:abc", "127.0.0.1:1:2", "[::1]x", "localhost@evil.example", "10.0.0.5:50052", "dlne.duckdns.org",
])
def test_any_other_host_is_turned_away_a_rebound_name_included(proxy, host):
    """DNS rebinding: the page's own name resolves to 127.0.0.1, so it is "same origin" and sends no Origin."""
    response = proxy.client.post("/v1/chat/completions", json=CHAT, headers={"Host": host})
    assert response.status_code == 421 and proxy.up.received == []
    assert proxy.client.get("/health", headers={"Host": host}).status_code == 421


@pytest.mark.parametrize("request_line", ["POST /v1/chat/completions HTTP/1.0\r\nContent-Length: 2\r\n\r\n{}",
                                          "GET /health HTTP/1.0\r\n\r\n"])
def test_a_request_with_no_host_at_all_is_turned_away(proxy, request_line):
    with socket.create_connection(("127.0.0.1", proxy.member.port), timeout=5) as raw:
        raw.sendall(request_line.encode())
        status = raw.recv(4096).split(b"\r\n", 1)[0]
    assert b" 421 " in status
    assert proxy.up.received == []


FIM_PROMPT = "<|fim_prefix|>def total(items):\n    return <|fim_suffix|>\n\nprint(total([1]))<|fim_middle|>"
FIM = {"prompt": FIM_PROMPT, "max_tokens": 32, "temperature": 0.2, "echo": SENTINEL}


def _bodies(proxy):
    return [json.loads(r["body"]) for r in proxy.up.received]


def test_ghost_text_is_warmed_up_with_the_text_before_the_cursor_first(proxy):
    sent = json.dumps(FIM, separators=(",", ":")).encode()
    response = proxy.client.post("/v1/completions", content=sent, headers={"content-type": "application/json"})
    assert response.status_code == 200 and response.json()["choices"][0]["message"]["content"] == "up-answer"
    assert [r["path"] for r in proxy.up.received] == ["/v1/completions", "/v1/completions"]
    from engine.fim_warm import fim_warm_body
    assert _bodies(proxy)[0] == fim_warm_body(FIM) == {
        "prompt": "<|fim_prefix|>def total(items):\n    return ", "max_tokens": 0, "temperature": 0.0, "stream": False}
    assert proxy.up.received[1]["body"] == sent, "the real request goes on byte for byte, after the warm-up"
    assert response.json()["id"] == "up-1"           # one answer: the warm-up's is never returned
    assert proxy.member.status()["busy"] == 0


def test_the_warm_up_is_sent_to_llama_server_with_a_twenty_second_limit(proxy, monkeypatch):
    limits = []
    real = cm._send_unless_gone

    async def spy(client, upstream, request):
        limits.append(upstream.extensions["timeout"])
        return await real(client, upstream, request)

    monkeypatch.setattr(cm, "_send_unless_gone", spy)
    proxy.client.post("/v1/completions", json=FIM)
    assert limits[0]["read"] == 20.0 and limits[0]["connect"] == 5.0
    assert limits[1]["read"] == cm.UPSTREAM_READ_TIMEOUT, "the real request keeps its own, long, limit"


def test_a_warm_up_that_fails_does_not_break_the_answer(proxy):
    proxy.up.fail_warmups = True
    response = proxy.client.post("/v1/completions", json=FIM)
    assert response.status_code == 200 and response.json()["choices"][0]["message"]["content"] == "up-answer"
    assert [b.get("max_tokens") for b in _bodies(proxy)] == [0, 32]
    assert proxy.member.status()["busy"] == 0


def test_a_warm_up_that_cannot_connect_does_not_break_the_error_the_caller_gets(proxy):
    proxy.up.stop()
    response = proxy.client.post("/v1/completions", json=FIM)
    assert response.status_code == 502 and response.json()["error"]["type"] == "engine_unavailable"
    assert proxy.member.status()["busy"] == 0


@pytest.mark.parametrize("body", [
    {"prompt": "def f(", "max_tokens": 4},                                          # an ordinary completion
    {"prompt": "<|fim_prefix|>no suffix, no cursor"},                               # not fill-in-the-middle
    {"prompt": "text <|fim_prefix|>a<|fim_suffix|>b<|fim_middle|>"},                # does not start with the token
    {"prompt": ["<|fim_prefix|>a<|fim_suffix|>b"]},                                 # not a string
    {"max_tokens": 4},
])
def test_other_completions_get_no_warm_up(proxy, body):
    assert proxy.client.post("/v1/completions", json=body).status_code == 200
    assert len(proxy.up.received) == 1 and _bodies(proxy)[0] == body


def test_chat_requests_get_no_warm_up(proxy):
    proxy.client.post("/v1/chat/completions", json={**CHAT, "prompt": FIM_PROMPT})
    assert len(proxy.up.received) == 1


def test_the_warm_up_shares_the_requests_slot(proxy):
    """Two held ghost-text requests fill a 2-slot member: had the warm-up taken a slot of its own, the second
    would have been refused while the first was still warming up."""
    proxy.up.mode = "hold"
    proxy.up.gate.clear()
    results = []

    def one():
        with httpx.Client(base_url=f"http://127.0.0.1:{proxy.member.port}", trust_env=False, timeout=30.0) as client:
            results.append(client.post("/v1/completions", json=FIM).status_code)

    first = threading.Thread(target=one)
    first.start()
    assert wait_for(lambda: len(proxy.up.received) == 1), "the first request is held in its warm-up"
    assert _bodies(proxy)[0]["max_tokens"] == 0 and proxy.member.status()["busy"] == 1
    second = threading.Thread(target=one)
    second.start()
    assert wait_for(lambda: proxy.member.status()["busy"] == 2), "the second one was accepted"
    third = proxy.client.post("/v1/completions", json=FIM)
    assert third.status_code == 503 and third.json()["error"]["type"] == "busy"
    proxy.up.gate.set()
    for thread in (first, second):
        thread.join(15)
    assert results == [200, 200] and wait_for(lambda: proxy.member.status()["busy"] == 0)


def test_a_caller_that_hangs_up_during_the_warm_up_frees_the_slot_and_sends_nothing_more(proxy):
    proxy.up.mode = "hold"
    proxy.up.gate.clear()
    with httpx.Client(base_url=f"http://127.0.0.1:{proxy.member.port}", trust_env=False, timeout=0.6) as client:
        with pytest.raises(httpx.ReadTimeout):
            client.post("/v1/completions", json=FIM)
    assert wait_for(lambda: proxy.member.status()["busy"] == 0, 10)
    assert len(proxy.up.received) == 1, "only the warm-up was ever sent"
    assert proxy.up.disconnected.wait(10)


def test_an_encrypted_ghost_text_request_is_warmed_up_too_and_only_its_answer_is_sealed(proxy):
    envelope, key = seal_request(FIM, proxy.member.pubkey)
    response = proxy.client.post("/v1/completions", json=envelope)
    assert response.status_code == 200
    sealed = response.json()["dlne_e2e"]
    assert sealed["n"] == 0 and json.loads(Opener(key).open(sealed))["id"] == "up-1"
    bodies = _bodies(proxy)
    assert [b.get("max_tokens") for b in bodies] == [0, 32], "the warm-up came from the opened request"
    assert "dlne_e2e" not in bodies[0] and bodies[0]["prompt"].startswith("<|fim_prefix|>def total")
    assert SENTINEL.encode() not in response.content


def test_a_streamed_ghost_text_request_is_warmed_up_with_a_plain_request(proxy):
    with proxy.client.stream("POST", "/v1/completions", json={**FIM, "stream": True}) as response:
        raw = b"".join(response.iter_raw())
    assert raw == b"".join(proxy.up.chunks)
    assert _bodies(proxy)[0]["stream"] is False and _bodies(proxy)[1]["stream"] is True


def test_a_plain_stream_is_relayed_unchanged(proxy):
    with proxy.client.stream("POST", "/v1/chat/completions", json={**CHAT, "stream": True}) as response:
        assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream")
        raw = b"".join(response.iter_raw())
    assert raw == b"".join(proxy.up.chunks)
    assert data_lines(raw)[-1] == "[DONE]"


def test_nothing_the_caller_sent_but_the_body_reaches_llama_server(proxy):
    proxy.client.post("/v1/chat/completions", json=CHAT, headers={
        "Authorization": "Bearer dlne_sk_secret", "X-Forwarded-For": "203.0.113.9", "X-DLNE-Route": "r1",
        "X-DLNE-Session": "s1", "Cookie": "a=b", "X-Api-Key": "k", "User-Agent": "someone/1.0"})
    seen = {k.lower() for k in proxy.up.received[0]["headers"]}
    assert not seen & {"authorization", "x-forwarded-for", "x-dlne-route", "x-dlne-session", "cookie", "x-api-key"}
    assert proxy.up.received[0]["headers"]["content-type"] == "application/json"
    assert "user-agent" not in seen or "someone" not in proxy.up.received[0]["headers"].get("user-agent", "")


def test_only_the_two_completion_routes_and_health_are_reachable(proxy):
    for method, path in [("GET", "/props"), ("POST", "/props"), ("GET", "/slots"), ("POST", "/completion"),
                         ("POST", "/v1/embeddings"), ("GET", "/v1/models"), ("GET", "/metrics"), ("GET", "/")]:
        assert proxy.client.request(method, path).status_code in (404, 405), (method, path)
    assert proxy.client.get("/v1/chat/completions").status_code == 405
    assert proxy.up.other_hits == [] and proxy.up.received == []


def test_a_member_that_is_not_ready_says_so(proxy):
    proxy.member.ready = False
    response = proxy.client.post("/v1/chat/completions", json=CHAT)
    assert response.status_code == 503 and response.json()["error"]["type"] == "not_ready"
    assert response.headers["retry-after"] and proxy.up.received == []


@pytest.mark.parametrize("content", [b"not json {" + SENTINEL.encode(), b"[1, 2, 3]", b'"a string"', b"", b"\xff\xfe"])
def test_a_body_that_is_not_a_json_object_is_refused(proxy, content):
    response = proxy.client.post("/v1/chat/completions", content=content, headers={"content-type": "application/json"})
    assert response.status_code == 400 and response.json()["error"]["type"] == "invalid_request"
    assert SENTINEL not in response.text
    assert proxy.up.received == [] and proxy.member.status()["busy"] == 0


def test_a_body_over_the_limit_is_refused(proxy, monkeypatch):
    monkeypatch.setattr(cm, "MAX_BODY_BYTES", 1000)
    big = json.dumps({"messages": "x" * 5000})
    assert proxy.client.post("/v1/chat/completions", content=big,
                             headers={"content-type": "application/json"}).status_code == 413
    chunked = (part for part in (b'{"a": "', b"y" * 3000, b'"}'))
    assert proxy.client.post("/v1/chat/completions", content=chunked,
                             headers={"content-type": "application/json"}).status_code == 413
    assert proxy.up.received == []


# ------------------------------------------------------------------ end to end

def test_an_encrypted_request_is_opened_and_the_answer_sealed(proxy):
    envelope, key = seal_request({**CHAT, "stream": False}, proxy.member.pubkey)
    response = proxy.client.post("/v1/chat/completions", json=envelope)
    assert response.status_code == 200
    sealed = response.json()["dlne_e2e"]
    assert sealed["n"] == 0 and "rs" in sealed, "one sealed object, with the response salt"
    opener = Opener(key)
    answer = json.loads(opener.open(sealed))
    assert answer["choices"][0]["message"]["content"] == "up-answer"
    assert opener.finished is False, "a non-streamed answer needs no end marker"
    assert b"up-answer" not in response.content, "nothing of the answer travels in the clear"
    upstream_body = json.loads(proxy.up.received[0]["body"])
    assert upstream_body == {**CHAT, "stream": False}, "llama-server receives the plain request"
    assert "dlne_e2e" not in upstream_body


def test_an_encrypted_stream_is_sealed_event_by_event_and_ends_with_a_sealed_end(proxy):
    envelope, key = seal_request({**CHAT, "stream": True}, proxy.member.pubkey)
    with proxy.client.stream("POST", "/v1/chat/completions", json=envelope) as response:
        assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream")
        raw = b"".join(response.iter_raw())
    lines = data_lines(raw)
    assert lines[-1] == "[DONE]", "the plain [DONE] comes last"
    opener = Opener(key)
    opened = [opener.open_event(line) for line in lines[:-1]]
    assert opened[-1] is None and opener.finished, "the sealed end marker comes before the plain [DONE]"
    assert opener.next == 3, "two chunks and the end, counted 0, 1, 2 by the member"
    upstream = data_lines(b"".join(proxy.up.chunks))[:-1]
    assert [chunk.decode() for chunk in opened[:-1]] == upstream
    assert b"up-one" not in raw and b"up-two" not in raw


def test_a_stream_the_model_never_finished_has_no_sealed_end(proxy):
    proxy.up.chunks = proxy.up.chunks[:-1]          # llama-server closes without its [DONE]
    envelope, key = seal_request({**CHAT, "stream": True}, proxy.member.pubkey)
    with proxy.client.stream("POST", "/v1/chat/completions", json=envelope) as response:
        raw = b"".join(response.iter_raw())
    lines = data_lines(raw)
    opener = Opener(key)
    sent = [chunk.encode() for chunk in data_lines(b"".join(proxy.up.chunks))]
    assert len(sent) == 2 and [opener.open_event(line) for line in lines] == sent
    assert not opener.finished and b"[DONE]" not in raw, "the editor sees a cut stream, not a finished one"


@pytest.mark.parametrize("encrypted", [False, True])
def test_the_request_is_padded_but_llama_server_gets_plain_json(proxy, encrypted):
    body = CHAT
    if encrypted:
        body, _ = seal_request(CHAT, proxy.member.pubkey)
        raw = base64.b64decode(body["dlne_e2e"]["ct"])
        assert (len(raw) - 16) % 256 == 0, "sealed requests are padded to a multiple of 256 bytes"
    proxy.client.post("/v1/chat/completions", json=body)
    assert json.loads(proxy.up.received[0]["body"]) == CHAT


@pytest.mark.parametrize("stream", [False, True])
def test_an_error_from_llama_server_goes_back_in_the_clear_without_the_request(proxy, stream):
    proxy.up.mode = "500"
    envelope, key = seal_request({**CHAT, "stream": stream}, proxy.member.pubkey)
    response = proxy.client.post("/v1/chat/completions", json=envelope)
    assert response.status_code == 500
    assert response.json()["error"]["type"] == "engine_error" and "dlne_e2e" not in response.json()
    assert SENTINEL not in response.text, "llama-server's error quotes the prompt; the encrypted path never repeats it"
    assert proxy.member.status()["busy"] == 0


def test_busy_and_other_refusals_to_an_encrypted_request_are_in_the_clear(proxy):
    proxy.member.ready = False
    envelope, _ = seal_request(CHAT, proxy.member.pubkey)
    response = proxy.client.post("/v1/chat/completions", json=envelope)
    assert response.status_code == 503 and "dlne_e2e" not in response.json()
    assert SENTINEL not in response.text and envelope["dlne_e2e"]["epk"] not in response.text


def test_the_same_envelope_is_refused_the_second_time(proxy):
    envelope, key = seal_request(CHAT, proxy.member.pubkey)
    assert proxy.client.post("/v1/chat/completions", json=envelope).status_code == 200
    again = proxy.client.post("/v1/chat/completions", json=envelope)
    assert again.status_code == 400 and again.json()["error"]["type"] == "invalid_envelope"
    assert len(proxy.up.received) == 1, "a replay never reaches the model"


def test_a_request_sealed_to_the_previous_key_still_opens_just_after_a_rotation(proxy):
    old = proxy.member.pubkey
    envelope, key = seal_request(CHAT, old)
    proxy.member.keyring.rotate()
    assert proxy.member.pubkey != old
    response = proxy.client.post("/v1/chat/completions", json=envelope)
    assert response.status_code == 200
    assert json.loads(Opener(key).open(response.json()["dlne_e2e"]))["choices"]
    newer, _ = seal_request({**CHAT, "echo": "again"}, proxy.member.pubkey)
    assert proxy.client.post("/v1/chat/completions", json=newer).status_code == 200


@pytest.mark.parametrize("minutes_later, opens", [(14, True), (16, False)])
def test_a_retired_key_opens_requests_for_fifteen_minutes_only(proxy, minutes_later, opens):
    now = [1000.0]
    proxy.member.keyring = cm.KeyRing(clock=lambda: now[0])
    envelope, _ = seal_request(CHAT, proxy.member.pubkey)
    proxy.member.keyring.rotate()
    now[0] += minutes_later * 60
    response = proxy.client.post("/v1/chat/completions", json=envelope)
    if opens:
        assert response.status_code == 200
    else:
        assert response.status_code == 400 and response.json()["error"]["type"] == "invalid_envelope"
        assert proxy.up.received == []


def test_the_key_ring_rotates_on_its_own_every_six_hours_through_the_heartbeat(proxy):
    now = [1000.0]
    proxy.member.keyring = cm.KeyRing(clock=lambda: now[0])
    first = proxy.member.status()["pubkey"]
    now[0] += 5 * 3600
    assert proxy.member.status()["pubkey"] == first
    now[0] += 1.1 * 3600
    assert proxy.member.status()["pubkey"] != first, "the heartbeat's status() is what calls maybe_rotate"


@pytest.mark.parametrize("tamper", ["ct", "nonce", "epk", "version", "shape", "other_member"])
def test_an_envelope_that_does_not_open_is_refused(proxy, tamper):
    envelope, _ = seal_request(CHAT, proxy.member.pubkey)
    inner = envelope["dlne_e2e"]
    if tamper == "ct":
        raw = bytearray(base64.b64decode(inner["ct"]))
        raw[0] ^= 1
        inner["ct"] = base64.b64encode(bytes(raw)).decode()
    elif tamper == "nonce":
        inner["nonce"] = base64.b64encode(b"\x00" * 12).decode()
    elif tamper == "epk":
        inner["epk"] = base64.b64encode(b"\x09" * 32).decode()
    elif tamper == "version":
        inner["v"] = 2
    elif tamper == "shape":
        envelope = {"dlne_e2e": "nonsense"}
    else:
        envelope, _ = seal_request(CHAT, base64.b64encode(os.urandom(32)).decode())
    response = proxy.client.post("/v1/chat/completions", json=envelope)
    assert response.status_code == 400 and response.json()["error"]["type"] == "invalid_envelope"
    assert proxy.up.received == [] and proxy.member.status()["busy"] == 0
    assert SENTINEL not in response.text


def test_each_answer_is_sealed_with_its_own_salt(proxy):
    """Two requests sealed to the same member get unrelated answer keys, and a repeat of the same
    question still gets a fresh salt (so identical answers do not repeat their ciphertext)."""
    first, key_one = seal_request(CHAT, proxy.member.pubkey)
    second, key_two = seal_request(CHAT, proxy.member.pubkey)
    a = proxy.client.post("/v1/chat/completions", json=first).json()["dlne_e2e"]
    b = proxy.client.post("/v1/chat/completions", json=second).json()["dlne_e2e"]
    assert a["rs"] != b["rs"] and a["ct"] != b["ct"]
    with pytest.raises(Exception):
        Opener(key_two).open(a)
    assert json.loads(Opener(key_one).open(a))


# ------------------------------------------------------------------ slots

def _hold_requests(proxy, count):
    """``count`` non-streamed requests that llama-server holds until the gate opens."""
    proxy.up.mode = "hold"
    proxy.up.gate.clear()
    results = []

    def one():
        with httpx.Client(base_url=f"http://127.0.0.1:{proxy.member.port}", trust_env=False, timeout=30.0) as client:
            results.append(client.post("/v1/chat/completions", json=CHAT).status_code)

    threads = [threading.Thread(target=one) for _ in range(count)]
    for thread in threads:
        thread.start()
    assert wait_for(lambda: proxy.member.status()["busy"] == count)
    return threads, results


def test_a_full_member_answers_busy_and_frees_its_slots_afterwards(proxy):
    threads, results = _hold_requests(proxy, 2)
    block = proxy.member.status()
    assert (block["busy"], block["slots"]) == (2, 2)
    check_coding_block(block)
    busy = proxy.client.post("/v1/chat/completions", json=CHAT)
    assert busy.status_code == 503 and busy.json()["error"]["type"] == "busy" and busy.headers["retry-after"]
    # The two held requests may still be on their way to llama-server (their slots are taken first).
    assert wait_for(lambda: len(proxy.up.received) >= 2, 10)
    assert len(proxy.up.received) == 2, "the third request never reached llama-server"
    proxy.up.gate.set()
    for thread in threads:
        thread.join(10)
    assert sorted(results) == [200, 200]
    # A slot is given back as the response's background task, just after the caller has its answer.
    assert wait_for(lambda: proxy.member.status()["busy"] == 0, 10)
    assert proxy.client.post("/v1/chat/completions", json=CHAT).status_code == 200


def test_a_caller_that_hangs_up_before_the_answer_frees_the_slot_and_stops_llama_server(proxy):
    proxy.up.mode = "hold"
    proxy.up.gate.clear()
    with httpx.Client(base_url=f"http://127.0.0.1:{proxy.member.port}", trust_env=False, timeout=0.6) as client:
        with pytest.raises(httpx.ReadTimeout):
            client.post("/v1/chat/completions", json=CHAT)
    assert wait_for(lambda: proxy.member.status()["busy"] == 0, 10)
    assert proxy.up.disconnected.wait(10), "llama-server's connection was closed, so it stops generating"


def test_a_caller_that_hangs_up_mid_stream_frees_the_slot_and_stops_llama_server(proxy):
    proxy.up.mode = "hold"
    proxy.up.gate.clear()
    with proxy.client.stream("POST", "/v1/chat/completions", json={**CHAT, "stream": True}) as response:
        first = next(response.iter_raw())
        assert first.startswith(b"data: ")
        assert proxy.member.status()["busy"] == 1
    assert wait_for(lambda: proxy.member.status()["busy"] == 0, 10)
    assert proxy.up.disconnected.wait(10) or proxy.up.stream_closed.is_set()


def test_a_finished_stream_frees_its_slot(proxy):
    for _ in range(5):
        with proxy.client.stream("POST", "/v1/chat/completions", json={**CHAT, "stream": True}) as response:
            b"".join(response.iter_raw())
    assert wait_for(lambda: proxy.member.status()["busy"] == 0)
    assert proxy.client.post("/v1/chat/completions", json=CHAT).status_code == 200


# ------------------------------------------------------------------ the engine misbehaving

def test_llama_server_being_down_is_a_502_and_frees_the_slot(proxy):
    proxy.up.stop()
    response = proxy.client.post("/v1/chat/completions", json=CHAT)
    assert response.status_code == 502 and response.json()["error"]["type"] == "engine_unavailable"
    assert SENTINEL not in response.text
    assert proxy.member.status()["busy"] == 0


def test_an_error_from_llama_server_is_passed_on_as_it_is(proxy):
    proxy.up.mode = "500"
    response = proxy.client.post("/v1/chat/completions", json=CHAT)
    assert response.status_code == 500 and SENTINEL in response.json()["error"]["message"]


@pytest.mark.parametrize("encrypted", [False, True])
def test_a_stream_that_breaks_off_is_an_error_for_the_caller_and_frees_the_slot(proxy, encrypted):
    proxy.up.mode = "die"
    body = {**CHAT, "stream": True}
    key = None
    if encrypted:
        body, key = seal_request(body, proxy.member.pubkey)
    with pytest.raises(httpx.HTTPError):
        with proxy.client.stream("POST", "/v1/chat/completions", json=body) as response:
            for _ in response.iter_raw():
                pass
    assert wait_for(lambda: proxy.member.status()["busy"] == 0, 10)


# ------------------------------------------------------------------ nothing is logged

class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        text = record.getMessage() + " " + " ".join(map(str, record.args or ()))
        if record.exc_info:
            text += " " + "".join(logging.Formatter().formatException(record.exc_info))
        self.lines.append(text)


@pytest.fixture
def logs(monkeypatch):
    handler = _Capture()
    names = ["", "system", "worker", "coordinator", "uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore",
             "asyncio", "fastapi", "starlette"]
    loggers = [logging.getLogger(n) for n in names]
    saved = [(lg, lg.level) for lg in loggers]
    for lg in loggers:
        lg.addHandler(handler)
        lg.setLevel(logging.DEBUG)
    yield handler
    for lg, level in saved:
        lg.removeHandler(handler)
        lg.setLevel(level)


def test_request_and_response_bodies_never_reach_a_log_or_the_console(proxy, logs, capfd):
    secret_prompt = {"messages": [{"role": "user", "content": SENTINEL}], "echo": SENTINEL}
    # plain, streamed and not
    proxy.client.post("/v1/chat/completions", json=secret_prompt)
    with proxy.client.stream("POST", "/v1/chat/completions", json={**secret_prompt, "stream": True}) as response:
        b"".join(response.iter_raw())
    # encrypted, streamed and not
    for stream in (False, True):
        envelope, _ = seal_request({**secret_prompt, "stream": stream}, proxy.member.pubkey)
        with proxy.client.stream("POST", "/v1/chat/completions", json=envelope) as response:
            b"".join(response.iter_raw())
    # malformed, oversized, unopenable
    proxy.client.post("/v1/chat/completions", content=b"{" + SENTINEL.encode(), headers={"content-type": "application/json"})
    envelope, _ = seal_request(secret_prompt, proxy.member.pubkey)
    envelope["dlne_e2e"]["ct"] = envelope["dlne_e2e"]["ct"][:-4] + "AAAA"
    proxy.client.post("/v1/chat/completions", json=envelope)
    # an engine that echoes the prompt in its errors, then dies mid-stream, then is gone
    proxy.up.mode = "500"
    proxy.client.post("/v1/chat/completions", json=secret_prompt)
    proxy.up.mode = "die"
    try:
        with proxy.client.stream("POST", "/v1/chat/completions", json={**secret_prompt, "stream": True}) as response:
            b"".join(response.iter_raw())
    except httpx.HTTPError:
        pass
    proxy.up.stop()
    proxy.client.post("/v1/chat/completions", json=secret_prompt)
    out, err = capfd.readouterr()
    assert not [line for line in logs.lines if SENTINEL in line], "a log line quoted a body"
    assert SENTINEL not in out and SENTINEL not in err
    assert not [line for line in logs.lines if "dlne_e2e" in line or "Bearer" in line]


_BODY_NAMES = {"body", "raw", "payload", "opened", "sealed", "chunk", "pending", "line", "content", "inner", "envelope"}


def test_no_log_call_in_the_module_names_a_body():
    """A guard for future edits: log calls may carry fixed text, tier ids, ports, counts and exception class names."""
    tree = ast.parse(open(cm.__file__, encoding="utf-8").read())
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
            if name.startswith("log_") or name in ("_note_problem",):
                used = {n.id for arg in node.args for n in ast.walk(arg) if isinstance(n, ast.Name)}
                if used & _BODY_NAMES:
                    offenders.append((node.lineno, sorted(used & _BODY_NAMES)))
    assert offenders == []
    for node in ast.walk(tree):
        # and no exception *message* is ever interpolated into a log line
        if isinstance(node, ast.JoinedStr):
            for part in node.values:
                if isinstance(part, ast.FormattedValue) and isinstance(part.value, ast.Name) and part.value.id in ("exc", "e", "err"):
                    pytest.fail(f"line {node.lineno}: an exception is interpolated into text")


# ======================================================================================
# Windows and macOS
# ======================================================================================
#
# Run on any machine: the module asks `_is_windows()`, which these tests answer. The fake llama-server
# is still a Python script, so Popen is wrapped to record what it was given and drop the Windows-only
# `creationflags` (Linux's Popen refuses them). What none of this can show is what the real Windows or
# macOS kernel does; that needs a real machine (the cross-platform report lists the steps).

@pytest.fixture
def popen_log(monkeypatch):
    real = subprocess.Popen
    calls = []

    def popen(args, **kwargs):
        calls.append({"args": list(args), **kwargs})
        kwargs.pop("creationflags", None)
        return real(args, **kwargs)

    monkeypatch.setattr(cm.subprocess, "Popen", popen)
    return calls


@pytest.fixture
def fake_windows(monkeypatch):
    """The module on a fake Windows. The job object is replaced by a recorder (the real calls need a real kernel32)."""
    monkeypatch.setattr(cm, "_is_windows", lambda: True)
    bound = []
    monkeypatch.setattr(cm, "_bind_to_worker_lifetime", lambda process, api=None: bound.append(process) or True)
    return SimpleNamespace(bound=bound)


NON_ASCII_FOLDER = "José Ñandú DLNE"


def test_on_windows_llama_server_has_no_console_window_and_runs_below_normal(build, popen_log, fake_windows):
    env = build()
    serve(env)
    assert popen_log[0]["creationflags"] == cm.CREATE_NO_WINDOW | cm.BELOW_NORMAL_PRIORITY_CLASS == 0x08004000
    assert popen_log[0]["stdin"] == subprocess.DEVNULL and popen_log[0]["stderr"] == subprocess.STDOUT


def test_the_windows_flag_values_are_the_ones_subprocess_names():
    """The values are spelled out because the names exist only on Windows; check them where they do."""
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        assert cm.CREATE_NO_WINDOW == subprocess.CREATE_NO_WINDOW
        assert cm.BELOW_NORMAL_PRIORITY_CLASS == subprocess.BELOW_NORMAL_PRIORITY_CLASS
    assert (cm.CREATE_NO_WINDOW, cm.BELOW_NORMAL_PRIORITY_CLASS) == (0x08000000, 0x00004000)


def test_off_windows_popen_gets_no_creation_flags(build, popen_log):
    env = build()
    serve(env)
    assert popen_log[0].get("creationflags", 0) == 0
    assert popen_log[0]["cwd"] is None


def test_on_windows_a_model_folder_with_non_ascii_letters_is_given_as_a_bare_file_name(build, popen_log, fake_windows):
    env = build(folder=NON_ASCII_FOLDER)
    serve(env)
    name = env.tier["files"][0]["file"]
    assert name.isascii(), "a model file name is always plain ASCII (the downloader enforces it)"
    argv = recorded_starts(env.record)[0]
    assert arg_after(argv, "-m") == name
    assert popen_log[0]["cwd"] == str(env.models)
    assert all(part.isascii() for part in argv), "nothing the code page could mangle is left on the command line"


def test_on_windows_an_ascii_model_folder_keeps_the_full_path(build, popen_log, fake_windows):
    env = build()
    serve(env)
    assert arg_after(recorded_starts(env.record)[0], "-m") == str(env.models / env.tier["files"][0]["file"])
    assert popen_log[0]["cwd"] is None


def test_off_windows_a_non_ascii_model_folder_keeps_the_full_path(build, popen_log):
    env = build(folder=NON_ASCII_FOLDER)
    serve(env)
    assert arg_after(recorded_starts(env.record)[0], "-m") == str(env.models / env.tier["files"][0]["file"])
    assert popen_log[0]["cwd"] is None


# ---- the model server dies with the worker (a Windows job object) ------------------------

class FakeKernel32:
    """The four kernel32 calls the job object needs, recording what they were given."""

    def __init__(self, job=0x1234, create_ok=True, set_ok=True, assign_ok=True):
        self.job, self.create_ok, self.set_ok, self.assign_ok = job, create_ok, set_ok, assign_ok
        self.created, self.set_calls, self.assigned, self.closed = 0, [], [], []

    def CreateJobObjectW(self, attributes, name):
        self.created += 1
        return self.job if self.create_ok else None

    def SetInformationJobObject(self, job, info_class, reference, size):
        self.set_calls.append({"job": job, "class": info_class, "size": size, "flags": reference._obj.BasicLimitInformation.LimitFlags})
        return 1 if self.set_ok else 0

    def AssignProcessToJobObject(self, job, handle):
        self.assigned.append((job, handle))
        return 1 if self.assign_ok else 0

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return 1


@pytest.fixture
def job(monkeypatch):
    monkeypatch.setattr(cm, "_is_windows", lambda: True)
    monkeypatch.setattr(cm, "_JOB", None)


def test_the_job_limits_structure_is_the_size_windows_expects():
    import ctypes
    if ctypes.sizeof(ctypes.c_size_t) == 8:
        assert ctypes.sizeof(cm._job_limits_structure()) == 144          # JOBOBJECT_EXTENDED_LIMIT_INFORMATION, x64
    assert cm.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE == 0x2000


def test_a_started_server_is_put_in_a_job_that_kills_it_when_the_worker_goes(job):
    api = FakeKernel32()
    first, second = SimpleNamespace(_handle=111), SimpleNamespace(_handle=222)
    assert cm._bind_to_worker_lifetime(first, api) is True
    assert cm._bind_to_worker_lifetime(second, api) is True
    assert api.created == 1, "one job for the whole worker, shared by every server it starts"
    import ctypes
    assert len(api.set_calls) == 1
    call = api.set_calls[0]
    assert call["job"] == 0x1234 and call["class"] == 9, "JobObjectExtendedLimitInformation"
    assert call["flags"] == 0x2000, "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE"
    assert call["size"] == ctypes.sizeof(cm._job_limits_structure())
    assert api.assigned == [(0x1234, 111), (0x1234, 222)]
    assert api.closed == [], "the job handle stays open: closing it is what kills the servers"


@pytest.mark.parametrize("options", [{"create_ok": False}, {"set_ok": False}, {"assign_ok": False}])
def test_a_job_the_system_refuses_is_reported_and_never_raised(job, options):
    api = FakeKernel32(**options)
    assert cm._bind_to_worker_lifetime(SimpleNamespace(_handle=5), api) is False
    if "set_ok" in options:
        assert api.closed == [0x1234], "a job that could not be configured is not kept"
    if "assign_ok" not in options:
        assert api.assigned == []


def test_a_broken_kernel32_is_not_an_error(job):
    class Broken(FakeKernel32):
        def CreateJobObjectW(self, attributes, name):
            raise OSError("no")

    assert cm._bind_to_worker_lifetime(SimpleNamespace(_handle=5), Broken()) is False
    assert cm._bind_to_worker_lifetime(SimpleNamespace(), FakeKernel32()) is False, "no process handle"


def test_off_windows_nothing_is_bound(monkeypatch):
    monkeypatch.setattr(cm, "_is_windows", lambda: False)
    api = FakeKernel32()
    assert cm._bind_to_worker_lifetime(SimpleNamespace(_handle=5), api) is False
    assert api.created == 0


def test_on_windows_the_member_binds_each_server_it_starts(build, popen_log, fake_windows):
    env = build()
    serve(env)
    assert len(fake_windows.bound) == 1 and fake_windows.bound[0] is env.member.process


def test_a_server_that_cannot_be_bound_still_runs_and_the_owner_is_warned(build, popen_log, fake_windows, monkeypatch):
    monkeypatch.setattr(cm, "_bind_to_worker_lifetime", lambda process, api=None: False)
    warnings = []
    monkeypatch.setattr(cm, "log_warning", warnings.append)
    env = build()
    serve(env)
    assert env.member.status()["ready"] is True
    assert [w for w in warnings if "tied to this worker" in w]


# ---- the member port on Windows ----------------------------------------------------------

def test_the_windows_listener_is_loopback_only_and_does_not_set_so_reuseaddr():
    listener = cm._exclusive_loopback_listener(0)
    try:
        host, port = listener.getsockname()
        assert host == "127.0.0.1"
        assert listener.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) == 0
        with pytest.raises(OSError):
            cm._exclusive_loopback_listener(port)         # a port that is listened on is refused, not shared
    finally:
        listener.close()


def test_on_windows_the_proxy_listens_on_the_socket_the_member_made(idle_member, fake_windows, monkeypatch):
    made = []
    real = cm._exclusive_loopback_listener

    def recording(port):
        made.append(real(port))
        return made[-1]

    monkeypatch.setattr(cm, "_exclusive_loopback_listener", recording)
    member = idle_member()
    assert member.start()
    assert len(made) == 1 and made[0].getsockname() == ("127.0.0.1", member.port)
    answer = httpx.get(f"http://127.0.0.1:{member.port}/health", trust_env=False, timeout=5)
    assert answer.status_code == 503 and answer.json()["status"] == "loading"      # up, but no model yet


def test_on_windows_a_port_that_is_taken_makes_start_fail(idle_member, fake_windows):
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        assert idle_member(port=taken.getsockname()[1]).start() is False


def test_on_windows_the_port_can_be_taken_again_right_after_the_member_stopped(tmp_path, fake_windows):
    port = free_port()
    first = CodingMember(str(tmp_path / "a"), lambda: [], port=port)
    assert first.start()
    first.stop()
    second = CodingMember(str(tmp_path / "b"), lambda: [], port=port)
    try:
        assert second.start()
    finally:
        second.stop()


def test_off_windows_uvicorn_still_makes_its_own_socket(idle_member, monkeypatch):
    monkeypatch.setattr(cm, "_exclusive_loopback_listener", lambda port: pytest.fail("only Windows makes its own socket"))
    assert idle_member().start()


# ---- a server left behind by a worker that was ended abruptly (all platforms) -------------

@pytest.fixture
def leftover():
    """Stand-ins for a llama-server that an earlier worker left running: a process that sleeps."""
    started = []

    def start():
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
        started.append(proc)
        return proc

    yield start
    for proc in started:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


@pytest.fixture
def idle_member(tmp_path):
    """A member with no ladder that has only opened its proxy (reaping happens there)."""
    made = []

    def make(base=None, port=None):
        member = CodingMember(str(base or tmp_path / "dlne"), lambda: [], port=port or free_port())
        made.append(member)
        return member

    yield make
    for member in made:
        member.stop()


def record_path(base):
    return os.path.join(str(base), cm.SERVER_RECORD)


def test_a_server_the_last_worker_left_running_is_stopped_when_the_next_one_starts(tmp_path, leftover, idle_member):
    base = tmp_path / "dlne"
    old = leftover()
    cm._remember_server(str(base), old)
    assert json.load(open(record_path(base)))["pid"] == old.pid
    member = idle_member(base)
    assert member.start()
    assert wait_for(lambda: old.poll() is not None, 20), "the leftover server is still running"
    assert not os.path.exists(record_path(base))


@pytest.mark.parametrize("change", [
    {"exe": "/usr/bin/some-other-program"},                  # the pid now belongs to another program
    {"cmdline": ["python", "something", "else"]},            # the same program, run differently
    {"created": 1.0},                                        # the same program, started at another time
], ids=["other program", "other command line", "other start time"])
def test_a_process_that_is_not_provably_the_recorded_server_is_never_touched(tmp_path, leftover, idle_member, change):
    base = tmp_path / "dlne"
    other = leftover()
    cm._remember_server(str(base), other)
    record = json.load(open(record_path(base)))
    record.update(change)
    json.dump(record, open(record_path(base), "w"))
    assert idle_member(base).start()
    assert other.poll() is None, "a process that merely has the recorded pid was stopped"
    assert not os.path.exists(record_path(base)), "the stale record is cleaned up anyway"


def test_a_record_of_a_server_that_is_gone_or_unreadable_is_just_removed(tmp_path):
    done = subprocess.run([sys.executable, "-c", "pass"])
    assert done.returncode == 0
    path = record_path(tmp_path)
    os.makedirs(os.path.dirname(path))
    for text in ('{"pid": 99999999, "created": 1.0, "exe": null, "cmdline": null}', "not json {", "[1, 2]", '{"pid": "x"}', ""):
        with open(path, "w") as handle:
            handle.write(text)
        assert cm.reap_stale_server(str(tmp_path)) is False
        assert not os.path.exists(path), text
    assert cm.reap_stale_server(str(tmp_path)) is False, "no record at all"


def test_without_psutil_nothing_is_stopped_and_nothing_breaks(tmp_path, leftover, monkeypatch):
    other = leftover()
    cm._remember_server(str(tmp_path), other)
    assert os.path.exists(record_path(tmp_path))
    monkeypatch.setitem(sys.modules, "psutil", None)          # import psutil -> ImportError
    assert cm.reap_stale_server(str(tmp_path)) is False
    assert other.poll() is None
    cm._remember_server(str(tmp_path / "elsewhere"), other)    # and writing a record is silently skipped
    assert not os.path.exists(record_path(tmp_path / "elsewhere"))


def test_a_worker_that_could_not_take_the_port_leaves_the_record_for_the_worker_that_has_it(tmp_path, leftover, idle_member):
    base = tmp_path / "dlne"
    live = leftover()
    cm._remember_server(str(base), live)
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        assert idle_member(base, port=taken.getsockname()[1]).start() is False
    assert live.poll() is None and os.path.exists(record_path(base))


def test_the_member_records_the_server_it_starts_and_forgets_it_when_it_stops(build):
    env = build()
    serve(env)
    record = json.load(open(record_path(env.base)))
    assert record["pid"] == env.member.process.pid
    assert record["created"] > 0 and record["exe"] and record["cmdline"] and "--port" in record["cmdline"]
    pid = env.member.process.pid
    env.member.stop()
    assert not os.path.exists(record_path(env.base))
    assert pid != 0


def test_a_record_that_belongs_to_another_server_is_not_deleted_by_stopping_this_one(build, tmp_path, leftover):
    env = build()
    serve(env)
    cm._forget_recorded_server(str(env.base), pid=env.member.process.pid + 1)
    assert os.path.exists(record_path(env.base))
    cm._forget_recorded_server(str(env.base), pid=env.member.process.pid)
    assert not os.path.exists(record_path(env.base))
