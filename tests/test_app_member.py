"""
A worker's side of the CPU pool (engine/app_member.py).

The worker downloads the tier's model only from the signed URL, runs it only if its size and
SHA-256 match, and serves it with llama-server on the CPU and on loopback. The coordinator's
heartbeat answer drives assign(); convergence happens on a background thread.

Model "downloads" come from tests/app_pool_fakes.FakeHub (no network); llama-server is a tiny
executable Python script that records its argv and answers /health on the port it was given.
Tests marked xfail(strict=True) demonstrate bugs.
"""

import builtins
import os
import time
from types import SimpleNamespace

import pytest

from engine import app_member as am
from engine.app_member import APP_SLOTS, AppMember
from engine.app_pool import app_server_args
from tests.app_pool_fakes import FakeHub, free_port, make_tier, recorded_starts, wait_for, write_fake_llama_binary

DATA_A = b"GGUF tiny model A " * 64
DATA_B = b"GGUF tiny model B, a little bigger " * 80


@pytest.fixture
def env(tmp_path, monkeypatch):
    # The /health poll sleeps a second between tries; a tenth keeps the suite quick.
    real_sleep = time.sleep
    monkeypatch.setattr(am, "time", SimpleNamespace(monotonic=time.monotonic, sleep=lambda s: real_sleep(min(s, 0.1))))
    base = tmp_path / "dlne"
    record = str(tmp_path / "starts.jsonl")
    binary = write_fake_llama_binary(str(base / "bin" / "llama-server"), record)
    a, b = make_tier("tiny-a", DATA_A, 8), make_tier("tiny-b", DATA_B, 16)
    hub = FakeHub({a["url"]: DATA_A, b["url"]: DATA_B})
    tiers = [a, b]
    member = AppMember(str(base), lambda: list(tiers), threads=3, port=free_port(), urlopen=hub)
    yield SimpleNamespace(base=base, models=base / "models", record=record, binary=binary, a=a, b=b,
                          hub=hub, tiers=tiers, member=member, tmp=tmp_path)
    member.stop()


def settle(member, timeout=30.0):
    assert wait_for(lambda: not member._busy, timeout), "the member never finished converging"


def serve(env, tier):
    env.member.assign(tier["id"])
    settle(env.member)
    assert env.member.status() == {"model": tier["id"], "ready": True, "slots": APP_SLOTS, "threads": 3}


def leftovers(env):
    return sorted(os.listdir(env.models)) if env.models.exists() else []


# ======================================================================================
# The happy path
# ======================================================================================

def test_status_before_any_assignment(env):
    assert env.member.status() == {"model": None, "ready": False, "slots": 2, "threads": 3}


def test_assigning_nothing_at_first_does_nothing(env):
    env.member.assign(None)
    assert env.member._busy is False and env.hub.calls == []


def test_a_tier_is_downloaded_verified_and_served(env):
    serve(env, env.a)
    assert env.hub.calls == [env.a["url"]], "only ever the signed URL"
    path = env.models / env.a["file"]
    assert path.read_bytes() == DATA_A
    assert leftovers(env) == [env.a["file"], env.a["file"] + ".dlne-sha256"], "no .part left behind"
    starts = recorded_starts(env.record)
    assert len(starts) == 1
    expected = app_server_args(str(env.base / "bin" / "llama-server"), str(path), env.member.port, 3, slots=APP_SLOTS)
    assert starts[0] == expected[1:]
    args = starts[0]
    assert args[args.index("--device") + 1] == "none" and args[args.index("-ngl") + 1] == "0"
    assert args[args.index("--host") + 1] == "127.0.0.1"
    assert args[args.index("--alias") + 1] == env.a["file"], "answers carry the file's name, not the volunteer's folder"
    assert (env.base / "logs" / "app_server.log").exists()


def test_the_same_assignment_again_changes_nothing(env):
    serve(env, env.a)
    process = env.member.process
    for _ in range(3):
        env.member.assign(env.a["id"])
        assert env.member._busy is False
    assert env.member.process is process and env.hub.calls == [env.a["url"]]
    assert len(recorded_starts(env.record)) == 1


def test_a_file_already_verified_is_not_downloaded_again(env):
    env.models.mkdir(parents=True)
    (env.models / env.a["file"]).write_bytes(DATA_A)
    serve(env, env.a)
    assert env.hub.calls == []


def test_reassignment_restarts_on_the_other_tier(env):
    serve(env, env.a)
    old = env.member.process
    serve(env, env.b)
    assert env.member.process is not old and old.poll() is not None, "the old server was stopped"
    starts = recorded_starts(env.record)
    assert len(starts) == 2 and starts[1][starts[1].index("-m") + 1].endswith(env.b["file"])
    assert env.hub.calls == [env.a["url"], env.b["url"]]


def test_assigning_nothing_stops_the_server(env):
    serve(env, env.a)
    process = env.member.process
    env.member.assign(None)
    settle(env.member)
    assert process.poll() is not None and env.member.process is None
    assert env.member.status() == {"model": None, "ready": False, "slots": 2, "threads": 3}


def test_a_crashed_server_is_restarted_on_the_next_assignment(env):
    serve(env, env.a)
    env.member.process.kill()
    env.member.process.wait(timeout=5)
    assert env.member.status()["ready"] is False
    serve(env, env.a)
    assert len(recorded_starts(env.record)) == 2 and env.hub.calls == [env.a["url"]]


def test_stop_terminates_and_later_assignments_do_nothing(env):
    serve(env, env.a)
    process = env.member.process
    env.member.stop()
    assert process.poll() is not None and env.member.status()["ready"] is False
    env.member.assign(env.b["id"])
    settle(env.member)
    assert len(recorded_starts(env.record)) == 1 and env.hub.calls == [env.a["url"]]


# ======================================================================================
# Refusals
# ======================================================================================

@pytest.mark.parametrize("served", [b"X" * len(DATA_A), DATA_A[:-7]], ids=["wrong bytes", "short"])
def test_a_download_that_does_not_match_is_deleted_and_never_run(env, served):
    env.hub.files[env.a["url"]] = served
    env.member.assign(env.a["id"])
    settle(env.member)
    assert leftovers(env) == []
    assert env.member.process is None and recorded_starts(env.record) == []
    assert env.member.status() == {"model": None, "ready": False, "slots": 2, "threads": 3}


def test_an_oversize_download_is_aborted_early(env):
    reads = []

    class Endless:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, n):
            reads.append(n)
            return b"\0" * n

    env.member.urlopen = lambda url, timeout=None: Endless()
    env.member.assign(env.a["id"])
    settle(env.member)
    assert len(reads) == 1, "stopped at the first block past the signed size"
    assert leftovers(env) == [] and env.member.process is None


def test_not_enough_disk_space_means_no_download(env, monkeypatch):
    usage = SimpleNamespace(total=10 ** 12, used=10 ** 12 - 100, free=100)
    monkeypatch.setattr(am, "shutil", SimpleNamespace(disk_usage=lambda path: usage))
    env.member.assign(env.a["id"])
    settle(env.member)
    assert env.hub.calls == [] and leftovers(env) == [] and env.member.process is None


def test_a_missing_llama_server_is_reported_not_crashed(env):
    os.remove(env.binary)
    env.member.assign(env.a["id"])
    settle(env.member)
    assert env.member.process is None and env.member.target is None
    assert env.member.status() == {"model": None, "ready": False, "slots": 2, "threads": 3}
    assert (env.models / env.a["file"]).exists(), "the verified model is kept for later"


def test_a_tier_the_signed_release_does_not_list_is_refused(env):
    env.member.assign("ghost")
    settle(env.member)
    assert env.hub.calls == [] and env.member.process is None


def test_a_download_stops_when_the_member_stops(env):
    big = make_tier("big", b"\0" * (40 << 20))
    env.tiers.append(big)
    reads = []

    class Slow:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, n):
            reads.append(n)
            time.sleep(0.05)
            return b"\0" * n

    env.member.urlopen = lambda url, timeout=None: Slow()
    env.member.assign("big")
    assert wait_for(lambda: reads)
    env.member.stop()
    settle(env.member)
    assert len(reads) < 10 and leftovers(env) == []


def test_tier_list_failures_are_survived(env):
    def broken():
        raise OSError("coordinator unreachable")

    env.member.tiers_loader = broken
    env.member.assign(env.a["id"])
    settle(env.member)
    assert env.member.process is None
    env.member.tiers_loader = lambda: list(env.tiers)
    serve(env, env.a)


# ======================================================================================
# Bugs
# ======================================================================================

def test_a_failed_download_is_not_retried_on_the_very_next_heartbeat(env):
    env.hub.files[env.a["url"]] = b"X" * len(DATA_A)
    env.member.assign(env.a["id"])
    settle(env.member)
    env.member.assign(env.a["id"])
    settle(env.member)
    assert len(env.hub.calls) == 1


def test_a_failed_switch_is_retried_while_the_old_tier_keeps_serving(env):
    serve(env, env.a)
    del env.hub.files[env.b["url"]]               # the network drops during the upgrade
    env.member.assign(env.b["id"])
    settle(env.member)
    assert env.member.status()["model"] == env.a["id"], "still answering with the old tier meanwhile"
    env.hub.files[env.b["url"]] = DATA_B          # the network is back
    env.member.assign(env.b["id"])
    settle(env.member)
    assert env.member.status()["model"] == env.b["id"]


def test_a_server_that_dies_on_start_is_not_restarted_in_a_loop(env, monkeypatch):
    write_fake_llama_binary(env.binary, env.record, crash=True)
    real_sleep = time.sleep
    monkeypatch.setattr(am, "time", SimpleNamespace(monotonic=time.monotonic, sleep=lambda s: real_sleep(min(s, 0.05))))
    env.member.assign(env.a["id"])
    real_sleep(1.5)
    starts = len(recorded_starts(env.record))
    env.member.stop()
    settle(env.member)
    assert starts <= 2


def test_a_slow_starting_server_becomes_ready_later(env, monkeypatch):
    flag = env.tmp / "loaded"
    write_fake_llama_binary(env.binary, env.record, ready_flag=str(flag))
    clock = {"t": 0.0}

    def monotonic():
        clock["t"] += 100.0
        return clock["t"]

    monkeypatch.setattr(am, "time", SimpleNamespace(monotonic=monotonic, sleep=lambda s: time.sleep(0.05)))
    env.member.assign(env.a["id"])
    settle(env.member)
    assert env.member.process is not None and env.member.status()["ready"] is False
    flag.write_text("model loaded")
    env.member.assign(env.a["id"])
    settle(env.member)
    assert wait_for(lambda: env.member.status()["ready"], timeout=3)


def test_the_server_log_handle_is_not_leaked(env, monkeypatch):
    opened = []

    def tracking_open(path, *args, **kwargs):
        handle = builtins.open(path, *args, **kwargs)
        if str(path).endswith("app_server.log"):
            opened.append(handle)
        return handle

    monkeypatch.setattr(am, "open", tracking_open, raising=False)
    serve(env, env.a)
    serve(env, env.b)
    env.member.stop()
    assert opened and all(handle.closed for handle in opened)


# ======================================================================================
# "Both": the phone-pool half steps aside while the owner games (engine/companion.py)
# ======================================================================================

def test_a_yielding_member_is_not_ready_at_once_and_stops_its_model_after_the_drain(env, monkeypatch):
    monkeypatch.setattr(am, "YIELD_DRAIN_SECONDS", 0.3)
    serve(env, env.a)
    process = env.member.process
    env.member.set_yielding(True)
    assert env.member.status()["ready"] is False, "the coordinator sends it nothing new from the next heartbeat"
    assert process.poll() is None, "the answers in flight get the drain"
    assert wait_for(lambda: process.poll() is not None, 10), "then the model stops and the CPU is the owner's"
    assert env.member.process is None


def test_assignments_while_yielding_start_nothing(env, monkeypatch):
    monkeypatch.setattr(am, "YIELD_DRAIN_SECONDS", 0.0)
    serve(env, env.a)
    env.member.set_yielding(True)
    assert wait_for(lambda: env.member.process is None, 10)
    settle(env.member)
    for _ in range(3):
        env.member.assign(env.a["id"])
        assert env.member._busy is False and env.member.process is None
    assert len(recorded_starts(env.record)) == 1


def test_the_model_comes_back_after_the_load_ends_and_the_settle_period_passes(env, monkeypatch):
    monkeypatch.setattr(am, "YIELD_DRAIN_SECONDS", 0.0)
    monkeypatch.setattr(am, "YIELD_SETTLE_SECONDS", 0.5)
    serve(env, env.a)
    env.member.set_yielding(True)
    assert wait_for(lambda: env.member.process is None, 10)
    settle(env.member)
    env.member.set_yielding(False)
    env.member.assign(env.a["id"])
    assert env.member.process is None and env.member._busy is False, "not before the settle period"
    assert wait_for(lambda: time.monotonic() >= env.member._resume_not_before, 5)
    serve(env, env.a)
    assert len(recorded_starts(env.record)) == 2 and env.hub.calls == [env.a["url"]], "the file is not fetched again"


def test_a_load_that_ends_within_the_drain_leaves_the_model_running(env, monkeypatch):
    monkeypatch.setattr(am, "YIELD_DRAIN_SECONDS", 5.0)
    serve(env, env.a)
    process = env.member.process
    env.member.set_yielding(True)
    env.member.set_yielding(False)
    time.sleep(0.4)
    assert env.member.process is process and process.poll() is None
    assert env.member.status()["ready"] is True
