"""
The worker's coding member wiring (engine/worker.py): the heartbeat's ``pool: "coding"`` block, the
role the coordinator gives a node in its heartbeat answer ("rpc", "coding", "app" or null), and the
switching between them. A node is an RPC node, a CPU pool member or a coding member, never two at
once, and a switch stops the old role before the new one starts.

The members and the RPC process are fakes; what is tested is the worker's own bookkeeping.
"""

import io
import os
import sys
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from engine import authn, coding_member, downloader, release, worker
from tests.coding_member_fakes import free_port, wait_for

KEY = "a-network-key-for-tests"
GPU_HW = {
    "gpu": {"has_gpu": True, "vram_total_gb": 8.0},
    "ram_gb": 30.0, "cpu_cores": 8, "cpu_threads": 16, "is_yielding": False,
}
CPU_HW = {**GPU_HW, "gpu": {"has_gpu": False, "vram_total_gb": 0}}


class RecordingMember:
    def __init__(self, status=None):
        self.assigned = []
        self.slots_asked = []
        self.stopped = False
        self._status = status or {"model": None, "ready": False}

    def status(self):
        return dict(self._status)

    def assign(self, tier, slots=None):
        if not self.stopped:
            self.assigned.append(tier)
            self.slots_asked.append(slots)

    def stop(self):
        self.stopped = True


class FakeProc:
    def __init__(self, returncode=None):
        self.returncode = returncode
        self.terminated = False
        self.holder_when_stopped = "unset"

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


@pytest.fixture
def node(monkeypatch, tmp_path):
    import engine.setup_core as setup_core
    monkeypatch.setattr(setup_core, "get_or_create_worker_id", lambda: "worker-under-test")
    monkeypatch.setattr(worker, "get_hardware_profile", lambda: dict(GPU_HW))
    monkeypatch.setattr(worker, "_write_token_to_disk", lambda token: None)
    monkeypatch.setattr(worker, "HEARTBEAT_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(worker, "get_worker_limits", lambda: {"network_key": KEY})
    monkeypatch.setattr(worker, "get_base_dir", lambda: str(tmp_path))
    n = worker.WorkerNode("http://coordinator:11435")
    n.is_running = True
    n.rpc_port = 50052
    yield n
    n._stopped = True     # keep the atexit hook quiet


def roles_running(node):
    """Which of the three roles the node has right now."""
    return sorted(name for name, present in (("rpc", node.rpc_process is not None),
                                              ("app", node.app_member is not None),
                                              ("coding", node.coding_member is not None)) if present)


def settle(node, timeout=10.0):
    assert wait_for(lambda: not node._role_busy, timeout), "the role switch never finished"


def fake_starts(node, monkeypatch, fail=(), raise_=()):
    """Replace the three start methods with recorders that make the matching role; returns the call log."""
    calls = []

    def start_rpc():
        calls.append("rpc")
        if "rpc" in raise_:
            raise RuntimeError("no engine")
        if "rpc" in fail:
            return False
        node.rpc_process = FakeProc()
        node.rpc_proto = "8.0.1"
        return True

    def start_app(keep_port=False):
        calls.append(("app", keep_port))
        if "app" in fail:
            return False
        node.app_member = RecordingMember({"model": None, "ready": False, "slots": 2})
        return True

    def start_coding():
        calls.append("coding")
        if "coding" in raise_:
            raise RuntimeError("no engine")
        if "coding" in fail:
            return False
        node.coding_member = RecordingMember({"model": None, "ready": False})
        return True

    monkeypatch.setattr(node, "start_rpc_server", start_rpc)
    monkeypatch.setattr(node, "start_app_member", start_app)
    monkeypatch.setattr(node, "start_coding_member", start_coding)
    return calls


# ======================================================================================
# The heartbeat
# ======================================================================================

def _verify(payload, hardware=None):
    return authn.verify_heartbeat(KEY, payload["worker_id"], payload["port"], payload["token"],
                                  payload["timestamp"], payload["nonce"], payload["signature"], payload["version"],
                                  payload["hardware"] if hardware is None else hardware,
                                  role=payload["role"], staged_version=payload["staged_version"])


def test_a_coding_member_says_so_inside_the_signature(node):
    node.coding_member = RecordingMember({"model": "m.gguf", "ready": True})
    node.access_token = "tok"
    payload = node._build_heartbeat_payload()
    assert payload["hardware"]["pool"] == "coding"
    assert payload["hardware"]["coding"] == {"model": "m.gguf", "ready": True}
    assert "app" not in payload["hardware"]
    assert _verify(payload)[0] is True
    forged = {**payload["hardware"], "coding": {**payload["hardware"]["coding"], "ready": False}}
    assert _verify(payload, forged)[0] is False
    assert "pool" not in node.hardware_profile, "the probed profile itself is not modified"


def test_a_real_members_block_is_signed_by_its_identity_in_the_heartbeat(node, tmp_path):
    from engine import member_identity
    node.coding_member = coding_member.CodingMember(str(tmp_path), lambda: [], port=free_port())
    payload = node._build_heartbeat_payload()
    block = payload["hardware"]["coding"]
    assert member_identity.verify_block(block) == node.coding_member.trust_id
    assert _verify(payload)[0] is True


def test_the_app_member_and_the_rpc_node_heartbeats_are_unchanged(node):
    assert "pool" not in node._build_heartbeat_payload()["hardware"]
    node.app_member = RecordingMember({"model": "t", "ready": True, "slots": 2})
    hardware = node._build_heartbeat_payload()["hardware"]
    assert hardware["pool"] == "app" and "coding" not in hardware


def test_a_node_in_the_middle_of_leaving_a_role_keeps_naming_it(node):
    """The old member stays referenced (stopped, not ready) until the new role runs: no gap in the heartbeats."""
    node.coding_member = RecordingMember({"model": None, "ready": False})
    node.coding_member.stop()
    assert node._build_heartbeat_payload()["hardware"]["pool"] == "coding"


# ======================================================================================
# What a heartbeat answer does
# ======================================================================================

def _run(node, monkeypatch, replies):
    sent = []

    def fake_post(url, json=None, timeout=None):
        sent.append(json)
        reply = replies[len(sent) - 1]
        if len(sent) >= len(replies):
            node.request_shutdown(0, "test finished")
        request = httpx.Request("POST", url)
        return httpx.Response(200, json=reply, request=request)

    monkeypatch.setattr(worker.httpx, "post", fake_post)
    node.heartbeat_loop()
    return sent


def test_the_coding_tier_in_the_answer_reaches_the_coding_member_only(node, monkeypatch):
    node.coding_member = RecordingMember()
    tier = {"id": "qwen36-a3b", "file": "x.gguf"}
    _run(node, monkeypatch, [{"status": "ok", "role": "coding", "coding_tier": tier, "coding_slots": 1, "app_model": "ignored"},
                             {"status": "ok", "role": "coding", "coding_tier": tier}])
    assert node.coding_member.assigned == [tier, tier]
    assert node.coding_member.slots_asked == [1, None], "coding_slots goes with the tier; an answer without it asks nothing"


@pytest.mark.parametrize("answer", [
    {"status": "ok"},                                                    # nothing about a job at all
    {"status": "ok", "role": None, "coding_tier": None},                 # an explicit "no assignment"
    {"status": "ok", "role": "coding", "coding_tier": None},             # the job, but no tier named (yet)
    {"status": "ok", "role": "coding"},
    {"status": "ok", "coding_tier": None, "app_model": "ignored"},
])
def test_an_answer_with_no_assignment_leaves_the_member_serving(node, monkeypatch, answer):
    """A coordinator that has just restarted answers "no assignment" for about 90 seconds; it must not stop
    the coding model that is serving."""
    calls = fake_starts(node, monkeypatch)
    node.coding_member = RecordingMember()
    tier = {"id": "qwen36-a3b"}
    _run(node, monkeypatch, [{"status": "ok", "role": "coding", "coding_tier": tier}, answer, answer, answer])
    assert node.coding_member.assigned == [tier], "assign(None) was never called"
    assert node.coding_member.stopped is False and roles_running(node) == ["coding"] and calls == []


def test_a_new_coding_tier_moves_the_member_and_a_role_change_still_replaces_it(node, monkeypatch):
    node.coding_member = RecordingMember()
    first, second = {"id": "qwen36-a3b"}, {"id": "qwen36-a3b-iq4xs"}
    for answer in ({"role": "coding", "coding_tier": first}, {"role": "coding"},
                   {"role": "coding", "coding_tier": second}, {"role": "coding", "coding_tier": None}):
        node._follow_assignment(answer)
    assert node.coding_member.assigned == [first, second]
    calls = fake_starts(node, monkeypatch)
    node.rpc_process = None
    node._follow_assignment({"role": "rpc"})              # leaving the job is a role change, not an empty tier
    settle(node)
    assert node.coding_member is None and calls == ["rpc"] and roles_running(node) == ["rpc"]


def test_the_app_model_reaches_the_app_member_only(node, monkeypatch):
    node.app_member = RecordingMember()
    _run(node, monkeypatch, [{"status": "ok", "role": "app", "app_model": "tiny-a", "coding_tier": {"id": "x"}}])
    assert node.app_member.assigned == ["tiny-a"]


@pytest.mark.parametrize("junk", [[1, 2], "ok", 5])
def test_an_answer_that_is_not_an_object_changes_nothing(node, monkeypatch, junk):
    node.coding_member = RecordingMember()
    _run(node, monkeypatch, [junk, {"status": "ok", "coding_tier": {"id": "t"}}])
    assert node.coding_member.assigned == [{"id": "t"}]
    assert roles_running(node) == ["coding"]


def test_the_answer_drives_a_switch_through_the_heartbeat_loop(node, monkeypatch):
    calls = fake_starts(node, monkeypatch)
    node.rpc_process = FakeProc()
    seen = []

    def spy(answer):
        seen.append(answer.get("role") if isinstance(answer, dict) else None)
        worker.WorkerNode._follow_assignment(node, answer)

    monkeypatch.setattr(node, "_follow_assignment", spy)
    node.is_running = True
    _run(node, monkeypatch, [{"status": "ok", "role": "coding"}])
    assert seen == ["coding"]
    # The loop ended the node at once (the test's last reply), so the switch itself is tested below.


# ======================================================================================
# Switching roles
# ======================================================================================

@pytest.mark.parametrize("start, target", [
    ("rpc", "coding"), ("rpc", "app"), ("coding", "rpc"), ("coding", "app"), ("app", "rpc"), ("app", "coding"),
])
def test_a_switch_stops_the_old_role_starts_the_new_one_and_leaves_exactly_one(node, monkeypatch, start, target):
    calls = fake_starts(node, monkeypatch)
    old = {"rpc": FakeProc(), "app": RecordingMember(), "coding": RecordingMember()}[start]
    if start == "rpc":
        node.rpc_process, node.rpc_proto = old, "8.0.1"
    else:
        setattr(node, f"{start}_member", old)
    node._follow_assignment({"status": "ok", "role": target, "coding_tier": {"id": "t"}, "app_model": "a"})
    settle(node)
    assert roles_running(node) == [target]
    assert (old.terminated if start == "rpc" else old.stopped) is True, "the old role was stopped"
    expected = ("app", True) if target == "app" else target
    assert calls == [expected], "the new role was started once, an app member keeping the port its tunnel dials"
    # Told its model as soon as it is up: by the switch itself and, if the heartbeat's own call got there
    # after the member existed, once more (the same answer twice is harmless, so the count is not asserted).
    if target == "coding":
        assert node.coding_member.assigned and all(tier == {"id": "t"} for tier in node.coding_member.assigned)
    if target == "app":
        assert node.app_member.assigned and all(model == "a" for model in node.app_member.assigned)
    if start == "rpc" and target != "rpc":
        assert node.rpc_proto == "" and node.rpc_process is None


def test_the_rpc_process_is_forgotten_before_it_is_stopped(node):
    """The heartbeat loop calls an exited rpc_process it still holds a crash."""
    proc = FakeProc()
    node.rpc_process = proc
    seen = {}
    proc.terminate = lambda: seen.update(holder=node.rpc_process)
    node._stop_rpc_server()
    assert seen["holder"] is None and node.rpc_process is None and node.rpc_proto == ""


def test_a_deliberate_stop_is_not_mistaken_for_a_crash_but_a_real_crash_still_is(node, monkeypatch):
    node.rpc_process = FakeProc()
    node._stop_rpc_server()
    _run(node, monkeypatch, [{"status": "ok"}])
    assert node.exit_code == 0
    crashed = FakeProc(returncode=3)
    node2 = worker.WorkerNode("http://coordinator:11435")
    node2.is_running = True
    node2.rpc_process = crashed
    _run(node2, monkeypatch, [{"status": "ok"}])
    assert node2.exit_code == 3
    node2._stopped = True


def test_the_heartbeats_during_a_switch_away_from_a_member_still_name_it(node, monkeypatch):
    names = []

    def start_rpc():
        names.append(node._build_heartbeat_payload()["hardware"].get("pool"))
        node.rpc_process = FakeProc()
        return True

    monkeypatch.setattr(node, "start_rpc_server", start_rpc)
    node.coding_member = RecordingMember()
    node._follow_assignment({"role": "rpc"})
    settle(node)
    assert names == ["coding"] and roles_running(node) == ["rpc"]


@pytest.mark.parametrize("role", [None, "", "banana", "RPC", " rpc", 5, True, [], {}, "app,coding"])
def test_a_role_that_is_not_one_of_the_three_is_ignored(node, monkeypatch, role):
    calls = fake_starts(node, monkeypatch)
    node.rpc_process = FakeProc()
    node._follow_assignment({"status": "ok", "role": role})
    settle(node)
    assert calls == [] and roles_running(node) == ["rpc"] and not node.rpc_process.terminated


def test_the_role_the_node_already_has_is_not_restarted(node, monkeypatch):
    calls = fake_starts(node, monkeypatch)
    node.coding_member = RecordingMember()
    node._follow_assignment({"role": "coding"})
    node._follow_assignment({"role": "coding"})
    settle(node)
    assert calls == [] and node.coding_member.stopped is False


def test_the_coordinators_own_hybrid_worker_never_changes_role(node, monkeypatch):
    calls = fake_starts(node, monkeypatch)
    node.role = "hybrid"
    node.rpc_process = FakeProc()
    node._follow_assignment({"role": "coding", "coding_tier": {"id": "t"}})
    settle(node)
    assert calls == [] and roles_running(node) == ["rpc"]


def test_a_role_that_cannot_start_returns_to_the_default_and_is_not_retried_at_once(node, monkeypatch):
    monkeypatch.setattr(worker, "ROLE_RETRY_SECONDS", 0.3)
    calls = fake_starts(node, monkeypatch, fail=("coding",))
    node.rpc_process = FakeProc()
    node._follow_assignment({"role": "coding"})
    settle(node)
    assert calls == ["coding", "rpc"] and roles_running(node) == ["rpc"], "back to an RPC node"
    assert node.exit_code == 0 and not node._shutdown_event.is_set()
    for _ in range(3):
        node._follow_assignment({"role": "coding"})
    settle(node)
    assert calls == ["coding", "rpc"], "asking again changes nothing while the role is held off"
    time.sleep(0.35)
    node._follow_assignment({"role": "coding"})
    settle(node)
    assert calls[-2:] == ["coding", "rpc"] and len(calls) == 4, "tried again once the hold-off is over"


def test_a_start_that_raises_is_a_failed_start(node, monkeypatch):
    monkeypatch.setattr(worker, "ROLE_RETRY_SECONDS", 60)
    calls = fake_starts(node, monkeypatch, raise_=("coding",))
    node.rpc_process = FakeProc()
    node._follow_assignment({"role": "coding"})
    settle(node)
    assert calls == ["coding", "rpc"] and roles_running(node) == ["rpc"]


def test_a_cpu_only_node_whose_new_role_fails_goes_back_to_the_pool_it_started_in(node, monkeypatch):
    monkeypatch.setattr(worker, "get_hardware_profile", lambda: dict(CPU_HW))
    node.hardware_profile = dict(CPU_HW)
    calls = fake_starts(node, monkeypatch, fail=("rpc",))
    node.app_member = RecordingMember()
    node._follow_assignment({"role": "rpc"})      # (coding is refused on a CPU-only node, see SR-07 below)
    settle(node)
    assert calls == ["rpc", ("app", True)] and roles_running(node) == ["app"]


def test_a_node_with_no_engine_left_shuts_down_like_one_that_failed_to_start(node, monkeypatch):
    fake_starts(node, monkeypatch, fail=("coding", "rpc"))
    node.rpc_process = FakeProc()
    node._follow_assignment({"role": "coding"})
    settle(node)
    assert node._shutdown_event.is_set() and node.exit_code == worker.FATAL_EXIT_CODE
    assert roles_running(node) == []


def test_a_target_that_changes_during_a_switch_is_followed(node, monkeypatch):
    calls = fake_starts(node, monkeypatch)
    gate = threading.Event()
    real_start_coding = node.start_coding_member

    def slow_start_coding():
        node._follow_assignment({"role": "app"})          # the coordinator changes its mind while this runs
        gate.wait(5)
        return real_start_coding()

    monkeypatch.setattr(node, "start_coding_member", slow_start_coding)
    node.rpc_process = FakeProc()
    node._follow_assignment({"role": "coding"})
    assert wait_for(lambda: node._role_target == "app")
    gate.set()
    settle(node)
    assert roles_running(node) == ["app"]
    assert calls == ["coding", ("app", True)]


def test_no_new_role_is_started_once_the_node_is_shutting_down(node, monkeypatch):
    calls = fake_starts(node, monkeypatch)
    node.rpc_process = FakeProc()
    node.request_shutdown(0, "test")
    node._follow_assignment({"role": "coding"})
    settle(node)
    assert calls == [] and node.rpc_process is not None


def test_stopping_the_node_stops_the_coding_member(node):
    node.coding_member = RecordingMember()
    node.stop()
    assert node.coding_member.stopped


def test_a_member_started_while_the_node_stops_is_stopped_at_once(node, monkeypatch):
    gate = threading.Event()
    made = []

    class SlowMember:
        def __init__(self, *args, **kwargs):
            made.append(self)
            self.stopped = False

        def start(self):
            gate.wait(5)
            return True

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(coding_member, "CodingMember", SlowMember)
    monkeypatch.setattr(worker, "download_and_extract_engine", lambda *a, **k: "bin")
    monkeypatch.setattr(downloader, "fetch_verified_manifest", lambda url: {})
    outcome = []
    thread = threading.Thread(target=lambda: outcome.append(node.start_coding_member()))
    thread.start()
    assert wait_for(lambda: bool(made))
    node.stop()
    gate.set()
    thread.join(5)
    assert outcome == [False] and made[0].stopped and node.coding_member is None


def test_a_switch_that_finishes_after_the_node_stopped_leaves_nothing_running(node, monkeypatch):
    gate = threading.Event()
    started = []

    def start_app(keep_port=False):
        gate.wait(5)
        node.app_member = RecordingMember()
        started.append(node.app_member)
        return True

    monkeypatch.setattr(node, "start_app_member", start_app)
    node.coding_member = RecordingMember()
    node._follow_assignment({"role": "app"})
    assert wait_for(lambda: node.coding_member.stopped)
    node.stop()
    gate.set()
    settle(node)
    assert started and started[0].stopped, "the member the switch made is stopped, not left running"


# ======================================================================================
# The DLNE_ROLE line the extension reads (contract section 10)
# ======================================================================================

def parse_role_like_the_extension(output):
    """vscode-extension/src/worker/contribution.ts parseRole(), rule for rule: the last well-formed line wins."""
    import json
    import re
    found = None
    for line in output.splitlines():
        at = line.find("DLNE_ROLE: ")
        if at == -1:
            continue
        match = re.match(r"^(rpc|coding|app)\b\s*(\{.*\})?\s*$", line[at + len("DLNE_ROLE: "):].strip())
        if not match:
            continue
        found = {"role": match.group(1)}
        if match.group(2):
            try:
                detail = json.loads(match.group(2))
            except ValueError:
                continue
            if isinstance(detail.get("model"), str) and len(detail["model"]) <= 128:
                found["model"] = detail["model"]
            if isinstance(detail.get("tier"), str) and len(detail["tier"]) <= 128:
                found["tier"] = detail["tier"]
            if isinstance(detail.get("slots"), int) and 1 <= detail["slots"] <= 64:
                found["slots"] = detail["slots"]
            if isinstance(detail.get("trust_id"), str) and re.fullmatch(r"[0-9a-f]{32}", detail["trust_id"]):
                found["trust_id"] = detail["trust_id"]
    return found


def test_an_rpc_node_and_an_app_member_print_their_job_once(node, capsys):
    capsys.readouterr()
    node._announce_role()
    node._announce_role()
    assert capsys.readouterr().out == "DLNE_ROLE: rpc\n"
    node.app_member = RecordingMember()
    node._announce_role()
    assert capsys.readouterr().out == "DLNE_ROLE: app\n"


def test_a_coding_member_prints_its_model_tier_slots_and_trust_id(node, tmp_path, capsys):
    member = coding_member.CodingMember(str(tmp_path), lambda: [], port=free_port())
    node.coding_member = member
    node._announce_role()
    first = capsys.readouterr().out
    assert first.startswith("DLNE_ROLE: coding {") and first.count("\n") == 1, "one line"
    assert parse_role_like_the_extension(first) == {"role": "coding", "slots": 2, "trust_id": member.trust_id}
    member.model, member.serving = "Qwen3.6-35B-A3B-UD-IQ3_S.gguf", "qwen36-a3b"
    node._announce_role()
    node._announce_role()
    second = capsys.readouterr().out
    assert second.count("\n") == 1, "printed again once the model is known, and not again until something changes"
    assert parse_role_like_the_extension(second) == {
        "role": "coding", "model": "Qwen3.6-35B-A3B-UD-IQ3_S.gguf", "tier": "qwen36-a3b", "slots": 2,
        "trust_id": member.trust_id}
    assert second.strip() == second.strip().encode("ascii").decode() and " " not in second.strip()[len("DLNE_ROLE: coding "):], \
        "compact JSON"


def test_the_trust_id_only_appears_for_a_coding_member(node, tmp_path, capsys):
    node.app_member = RecordingMember()
    node._announce_role()
    node.app_member = None
    node.rpc_process = FakeProc()
    node._announce_role()
    out = capsys.readouterr().out
    assert "trust_id" not in out and "{" not in out


def test_the_line_carries_nothing_but_names_and_counts(node, tmp_path, capsys):
    member = coding_member.CodingMember(str(tmp_path), lambda: [], port=free_port())
    member.model = "x" * 129          # too long to be a file name the extension accepts: left out, not cut
    node.coding_member = member
    node._announce_role()
    assert "model" not in capsys.readouterr().out


def test_a_role_switch_prints_the_new_job(node, monkeypatch, capsys):
    fake_starts(node, monkeypatch)
    node.rpc_process = FakeProc()
    node._announce_role()
    node._follow_assignment({"role": "coding"})
    settle(node)
    node._follow_assignment({"role": "rpc"})
    settle(node)
    out = capsys.readouterr().out
    assert [line for line in out.splitlines() if line.startswith("DLNE_ROLE")] == [
        "DLNE_ROLE: rpc", "DLNE_ROLE: coding", "DLNE_ROLE: rpc"], "the last line is the current job"
    assert parse_role_like_the_extension(out)["role"] == "rpc"


def test_a_failed_switch_prints_the_job_the_node_fell_back_to(node, monkeypatch, capsys):
    monkeypatch.setattr(worker, "ROLE_RETRY_SECONDS", 60)
    fake_starts(node, monkeypatch, fail=("coding",))
    node.rpc_process = FakeProc()
    node._announce_role()
    node._follow_assignment({"role": "coding"})
    settle(node)
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("DLNE_ROLE")]
    assert lines == ["DLNE_ROLE: rpc"], "never announced a job it did not get; the same job is not printed twice"


def test_the_heartbeat_loop_prints_a_job_that_changed_under_it(node, monkeypatch, capsys):
    member = RecordingMember()
    member.role_detail = lambda: {"model": "m.gguf", "slots": 2}
    node.coding_member = member
    _run(node, monkeypatch, [{"status": "ok"}])
    assert 'DLNE_ROLE: coding {"model":"m.gguf","slots":2}' in capsys.readouterr().out


def test_start_announces_the_job_before_the_first_heartbeat(node, monkeypatch, capsys):
    order = []
    monkeypatch.setattr(node, "start_rpc_server", lambda: order.append("started") or True)

    def heartbeat():
        order.append(capsys.readouterr().out)
        node.request_shutdown(0, "test")

    monkeypatch.setattr(node, "heartbeat_loop", heartbeat)
    import engine.reverse_tunnel as tunnel
    monkeypatch.setattr(tunnel, "start_worker_reverse_tunnel", lambda *a, **k: None)
    node.start()
    assert order[0] == "started" and "DLNE_ROLE: rpc\n" in order[1]


# ======================================================================================
# start_coding_member
# ======================================================================================

def test_start_coding_member_wires_the_member(node, monkeypatch, tmp_path):
    engines, fetched, made = [], [], []
    monkeypatch.setattr(worker, "download_and_extract_engine",
                        lambda role, coordinator_url=None: engines.append((role, coordinator_url)) or "x")
    monkeypatch.setattr(downloader, "fetch_verified_manifest", lambda url: fetched.append(url) or {"m": 1})
    monkeypatch.setattr(release, "coding_tiers", lambda manifest: [{"id": "qwen36-a3b", "manifest": manifest}])
    monkeypatch.setattr(worker, "get_worker_limits", lambda: {"network_key": KEY, "max_cpu_threads": 6})
    monkeypatch.setattr(node, "_gpu_environment", lambda: ({"CUDA_VISIBLE_DEVICES": "0"}, "0"))

    class Member:
        def __init__(self, base_dir, tiers_loader, port, **kwargs):
            made.append(SimpleNamespace(base_dir=base_dir, tiers_loader=tiers_loader, port=port, **kwargs))

        def start(self):
            return True

    monkeypatch.setattr(coding_member, "CodingMember", Member)
    node.coordinator_url = "http://coordinator:11435/"
    assert node.start_coding_member() is True
    assert engines == [("worker", "http://coordinator:11435/")]
    member = made[0]
    assert (member.base_dir, member.port, member.threads) == (str(tmp_path), 50052, 6)
    assert member.env_provider() == {"CUDA_VISIBLE_DEVICES": "0"}
    assert member.machine_provider == node._machine_info, "what the machine can spare, for the step-down"
    assert member.limits_provider()["max_cpu_threads"] == 6, "the worker's limits, which hold the speed floors"
    assert member.tiers_loader() == member.tiers_loader() == [{"id": "qwen36-a3b", "manifest": {"m": 1}}]
    assert fetched == ["http://coordinator:11435"], "the signed release is fetched once, then cached"
    assert isinstance(node.coding_member, Member)


def test_a_proxy_that_cannot_listen_leaves_the_node_without_a_coding_member(node, monkeypatch):
    monkeypatch.setattr(worker, "download_and_extract_engine", lambda *a, **k: "x")

    class Member:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return False

    monkeypatch.setattr(coding_member, "CodingMember", Member)
    assert node.start_coding_member() is False and node.coding_member is None


def test_start_app_member_keeps_the_port_when_asked(node, monkeypatch):
    monkeypatch.setattr(worker, "download_and_extract_engine", lambda *a, **k: "x")
    from engine import hardware
    monkeypatch.setattr(hardware, "get_cpu_cores", lambda: 6)
    node.rpc_port = 50052
    assert node.start_app_member(keep_port=True) is True
    assert node.rpc_port == 50052 and node.app_member.port == 50052
    node.app_member = None
    assert node.start_app_member() is True
    assert node.rpc_port == node.app_member.port != 50052


# ======================================================================================
# What the machine can spare
# ======================================================================================

@pytest.mark.parametrize("name, unified", [
    ("NVIDIA GeForce RTX 4070 Laptop GPU", False), ("AMD Radeon RX 7900 XTX", False), (None, False),
    ("Apple M3 Max", True), ("AMD Radeon 8060S Graphics", True), ("AMD Radeon(TM) 8050S", True),
    ("Ryzen AI Max+ 395", True),
])
def test_machine_info_reports_the_free_ram_and_whether_memory_is_shared(node, monkeypatch, name, unified):
    import psutil
    monkeypatch.setattr(psutil, "virtual_memory", lambda: SimpleNamespace(available=12 * 2 ** 30))
    node.hardware_profile = {"gpu": {"vram_total_gb": 8.0, "name": name}, "ram_gb": 31.0}
    assert node._machine_info() == {"vram_gb": 8.0, "ram_gb": 12.0, "unified": unified}


def test_machine_info_falls_back_to_the_probed_total_and_never_raises(node, monkeypatch):
    import psutil

    def broken():
        raise OSError("no /proc")

    monkeypatch.setattr(psutil, "virtual_memory", broken)
    node.hardware_profile = {"gpu": {"vram_total_gb": 8.0}, "ram_gb": 31.0}
    assert node._machine_info() == {"vram_gb": 8.0, "ram_gb": 31.0, "unified": False}
    node.hardware_profile = "not a profile"
    assert node._machine_info() == {"vram_gb": 0, "ram_gb": 0, "unified": False}


# ======================================================================================
# The volunteer's limits (MAX_VRAM_GB, MAX_RAM_GB, MAX_CPU_THREADS)
# ======================================================================================

def _limits(monkeypatch, **limits):
    monkeypatch.setattr(worker, "get_worker_limits", lambda: {"network_key": KEY, **limits})


def _memory(monkeypatch, total_gib, available_gib):
    import psutil
    monkeypatch.setattr(psutil, "virtual_memory", lambda: SimpleNamespace(
        total=int(total_gib * 2 ** 30), available=int(available_gib * 2 ** 30)))


def test_a_vram_cap_lowers_what_the_machine_offers_the_tier_choice(node, monkeypatch):
    _memory(monkeypatch, 32, 20)
    node.hardware_profile = {"gpu": {"vram_total_gb": 24.0, "name": "NVIDIA RTX 4090"}, "ram_gb": 32.0}
    _limits(monkeypatch, max_vram_gb="12", max_ram_gb=-1.0)
    assert node._machine_info() == {"vram_gb": 12.0, "ram_gb": 20.0, "unified": False, "limited": True}
    _limits(monkeypatch, max_vram_gb="8,4")          # two cards' worth: 12 in all
    assert node._machine_info()["vram_gb"] == 12.0


def test_a_ram_cap_lowers_it_too(node, monkeypatch):
    _memory(monkeypatch, 32, 20)
    node.hardware_profile = {"gpu": {"vram_total_gb": 8.0, "name": "x"}, "ram_gb": 32.0}
    _limits(monkeypatch, max_ram_gb=10.0)
    assert node._machine_info() == {"vram_gb": 8.0, "ram_gb": 10.0, "unified": False, "limited": True}


@pytest.mark.parametrize("limits", [{}, {"max_vram_gb": "-1", "max_ram_gb": -1.0}, {"max_vram_gb": "16", "max_ram_gb": 64.0},
                                    {"max_vram_gb": "junk"}])
def test_a_limit_that_does_not_bind_changes_nothing(node, monkeypatch, limits):
    _memory(monkeypatch, 32, 20)
    node.hardware_profile = {"gpu": {"vram_total_gb": 8.0, "name": "x"}, "ram_gb": 32.0}
    _limits(monkeypatch, **limits)
    assert node._machine_info() == {"vram_gb": 8.0, "ram_gb": 20.0, "unified": False}


@pytest.mark.parametrize("threads, expected", [(-1, 0), (0, 1), (1, 1), (6, 6), ("x", 0), (None, 0), (True, 0)])
def test_the_thread_cap_reaches_llama_server_and_zero_never_means_all(node, monkeypatch, threads, expected):
    _limits(monkeypatch, max_cpu_threads=threads)
    _memory(monkeypatch, 32, 20)
    assert node._coding_resources()["threads"] == expected


def test_a_vram_cap_becomes_fit_target_margins_on_the_cards_llama_server_will_see(node, monkeypatch):
    _memory(monkeypatch, 32, 20)
    monkeypatch.setattr(worker.WorkerNode, "_gpu_totals_gib", staticmethod(lambda limits: [8.0, 12.0]))
    _limits(monkeypatch, max_vram_gb="10")
    assert node._coding_resources()["extra_args"] == ["--fit-target", "4096,6144"]
    _limits(monkeypatch, max_vram_gb="-1")
    assert node._coding_resources()["extra_args"] == []
    _limits(monkeypatch, max_vram_gb="40")
    assert node._coding_resources()["extra_args"] == []


def test_a_ram_cap_that_binds_shrinks_the_prompt_cache(node, monkeypatch):
    from engine.coding_member import LIMITED_PROMPT_CACHE_MIB
    _memory(monkeypatch, 32, 20)
    _limits(monkeypatch, max_ram_gb=16.0)
    assert node._coding_resources()["cache_ram_mib"] == LIMITED_PROMPT_CACHE_MIB
    _limits(monkeypatch, max_ram_gb=64.0)
    assert "cache_ram_mib" not in node._coding_resources()
    _limits(monkeypatch, max_ram_gb=-1.0)
    assert "cache_ram_mib" not in node._coding_resources()


def test_the_cards_are_listed_in_the_order_llama_server_numbers_them(monkeypatch):
    from engine import hardware
    cards = [{"id": 0, "name": "Radeon Graphics", "integrated": True, "vram_total_gb": 0.5},
             {"id": 1, "name": "RTX A", "vram_total_gb": 8.0}, {"id": 2, "name": "RTX B", "vram_total_gb": 12.0}]
    monkeypatch.setattr(hardware, "get_all_gpus", lambda: cards)
    monkeypatch.setattr(hardware, "counts_as_gpu", lambda name, integrated=None: not integrated)
    totals = worker.WorkerNode._gpu_totals_gib
    assert totals({"allowed_gpu_indices": "-1"}) == [8.0, 12.0], "the integrated GPU is not one of them"
    assert totals({"allowed_gpu_indices": "2,1"}) == [12.0, 8.0], "as ALLOWED_GPU_INDICES lists them"
    assert totals({"allowed_gpu_indices": "1"}) == [8.0]
    assert totals({"allowed_gpu_indices": "none"}) == []
    assert totals({"allowed_gpu_indices": "9"}) == []


@pytest.fixture
def vulkan_install(tmp_path, monkeypatch):
    """An install whose bin/ holds a Vulkan build, so MEDIA_GPU applies. engine_build_is_vulkan reads <base>/bin:
    without this the test reads whatever the checkout or install it runs from holds."""
    from engine import inference_manager
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "libggml-vulkan.so").write_bytes(b"")
    monkeypatch.setattr(inference_manager, "get_base_dir", lambda: str(tmp_path))
    return tmp_path


@pytest.fixture
def cuda_install(tmp_path, monkeypatch):
    from engine import inference_manager
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "libggml-cuda.so").write_bytes(b"")
    monkeypatch.setattr(inference_manager, "get_base_dir", lambda: str(tmp_path))
    monkeypatch.setattr(inference_manager, "_media_gpu_warned", False)
    monkeypatch.setattr(inference_manager, "log_warning", lambda *a, **k: None)
    return tmp_path


def test_on_a_cuda_build_the_coding_members_cards_include_the_media_card(cuda_install, monkeypatch):
    from engine import hardware
    cards = [{"id": 0, "name": "RTX A", "address": "0000:03:00.0", "vram_total_gb": 8.0},
             {"id": 1, "name": "RTX B", "address": "0000:0c:00.0", "vram_total_gb": 12.0}]
    monkeypatch.setattr(hardware, "get_all_gpus", lambda: cards)
    monkeypatch.setattr(hardware, "counts_as_gpu", lambda name, integrated=None: True)

    # the engine ignores MEDIA_GPU on this build (the card is the model's), so the totals must count it
    assert worker.WorkerNode._gpu_totals_gib({"allowed_gpu_indices": "-1", "media_gpu": "0000:0c:00.0"}) == [8.0, 12.0]


def test_the_media_card_is_no_part_of_the_cards_the_coding_model_may_see(vulkan_install, monkeypatch):
    from engine import hardware
    cards = [{"id": 0, "name": "RTX A", "address": "0000:03:00.0", "vram_total_gb": 8.0},
             {"id": 1, "name": "RX 580", "address": "0000:0c:00.0", "vram_total_gb": 8.0},
             {"id": 2, "name": "RTX B", "address": "0000:09:00.0", "vram_total_gb": 12.0}]
    monkeypatch.setattr(hardware, "get_all_gpus", lambda: cards)
    monkeypatch.setattr(hardware, "counts_as_gpu", lambda name, integrated=None: True)
    totals = worker.WorkerNode._gpu_totals_gib
    assert totals({"allowed_gpu_indices": "-1", "media_gpu": "0000:0c:00.0"}) == [8.0, 12.0]
    assert totals({"allowed_gpu_indices": "1,2,0", "media_gpu": "1"}) == [12.0, 8.0]
    assert totals({"allowed_gpu_indices": "0000:09:00.0,0000:03:00.0"}) == [12.0, 8.0], "PCI addresses name cards too"


def test_the_coding_member_is_given_the_limits_reader(node, monkeypatch):
    made = []
    monkeypatch.setattr(worker, "download_and_extract_engine", lambda *a, **k: "x")

    class Member:
        def __init__(self, base_dir, tiers_loader, port, **kwargs):
            made.append(kwargs)

        def start(self):
            return True

    monkeypatch.setattr(coding_member, "CodingMember", Member)
    assert node.start_coding_member() is True
    assert made[0]["resources_provider"] == node._coding_resources


# ======================================================================================
# Yielding to the owner's load
# ======================================================================================

class YieldRecorder(RecordingMember):
    def __init__(self):
        super().__init__()
        self.yields = []

    def set_yielding(self, value):
        self.yields.append(value)


@pytest.mark.parametrize("flag, expected", [(True, True), (False, False), ("true", False), (1, False), (None, False)])
def test_every_heartbeat_tells_the_member_whether_the_worker_is_yielding(node, flag, expected):
    node.coding_member = YieldRecorder()
    node.hardware_profile = {**GPU_HW, "is_yielding": flag}
    node._build_heartbeat_payload()
    assert node.coding_member.yields == [expected]


def test_the_yield_is_told_before_the_block_is_built(node):
    order = []

    class Member(YieldRecorder):
        def set_yielding(self, value):
            order.append("yield")

        def status(self):
            order.append("status")
            return {"ready": False, "slots": 0}

    node.coding_member = Member()
    node.hardware_profile = {**GPU_HW, "is_yielding": True}
    node._build_heartbeat_payload()
    assert order == ["yield", "status"]


def test_a_real_member_reports_no_slots_in_the_signed_heartbeat_while_the_worker_yields(node, tmp_path):
    from engine import member_identity
    member = coding_member.CodingMember(str(tmp_path), lambda: [], port=free_port())
    node.coding_member = member
    node.hardware_profile = {**GPU_HW, "is_yielding": True}
    block = node._build_heartbeat_payload()["hardware"]["coding"]
    assert (block["ready"], block["slots"], block["busy"]) == (False, 0, 0)
    assert member_identity.verify_block(block) == member.trust_id
    member._stopped.set()


# ======================================================================================
# The opt-out: CODING_MEMBER_ALLOWED
# ======================================================================================

@pytest.mark.parametrize("line, allowed", [
    ("", True), ("CODING_MEMBER_ALLOWED=true", True), ("CODING_MEMBER_ALLOWED=TRUE", True),
    ("CODING_MEMBER_ALLOWED=false", False), ("CODING_MEMBER_ALLOWED=False", False),
    ("CODING_MEMBER_ALLOWED=", False), ("CODING_MEMBER_ALLOWED=yes", False), ("CODING_MEMBER_ALLOWED=1", False),
    ("CODING_MEMBER_ALLOWED = true", True),
])
def test_the_config_key_is_parsed_like_the_other_switches(isolated_config, monkeypatch, line, allowed):
    from engine import config as engine_config
    isolated_config.write_text(line + "\n", encoding="utf-8")
    monkeypatch.setattr(engine_config, "_cached_stamp", None)
    assert engine_config.get_worker_limits()["coding_member_allowed"] is allowed


@pytest.mark.parametrize("allowed", [True, False])
def test_the_heartbeat_says_whether_the_owner_allows_it_inside_the_signature(node, monkeypatch, allowed):
    _limits(monkeypatch, coding_member_allowed=allowed)
    payload = node._build_heartbeat_payload()
    assert payload["hardware"]["coding_ok"] is allowed
    assert _verify(payload)[0] is True
    assert _verify(payload, {**payload["hardware"], "coding_ok": not allowed})[0] is False, "it cannot be flipped in flight"
    assert "coding_ok" not in node.hardware_profile


def test_a_node_without_the_key_in_its_config_allows_it(node, monkeypatch):
    _limits(monkeypatch)
    assert node._build_heartbeat_payload()["hardware"]["coding_ok"] is True


# --- 2.4.9: the RAM a coding model may use, and the LAN addresses a household coordinator asks for --------

@pytest.mark.parametrize("limits, expected", [
    ({"max_ram_gb": 4.0, "coding_max_ram_gb": -1.0}, 30.0),     # the extension's defaults: 4 GB lent to RPC, coding uncapped
    ({"max_ram_gb": 4.0}, 4.0),                                 # an old config with only MAX_RAM_GB: it caps both
    ({"max_ram_gb": 4.0, "coding_max_ram_gb": 12.0}, 12.0),
    ({}, 30.0),
])
def test_the_heartbeat_reports_the_ram_the_coding_job_may_use(node, monkeypatch, limits, expected):
    _memory(monkeypatch, 30, 9)              # the total, not what is free this second
    _limits(monkeypatch, **limits)
    payload = node._build_heartbeat_payload()
    assert payload["hardware"]["coding_ram_gb"] == expected
    assert _verify(payload)[0] is True
    assert _verify(payload, {**payload["hardware"], "coding_ram_gb": 512.0})[0] is False, "signed"


def test_a_node_without_a_gpu_reports_no_coding_ram(node, monkeypatch):
    _memory(monkeypatch, 30, 9)
    _limits(monkeypatch)
    node.hardware_profile = {"gpu": {"has_gpu": False, "vram_total_gb": 0}, "ram_gb": 30.0}
    assert "coding_ram_gb" not in node._build_heartbeat_payload()["hardware"]


def test_the_lan_addresses_are_sent_only_after_the_coordinator_asks(node, monkeypatch):
    _limits(monkeypatch)
    monkeypatch.setattr(worker, "_lan_ipv4_addresses", lambda: ["192.168.40.50"])
    assert "lan_ipv4" not in node._build_heartbeat_payload()["hardware"]
    node._follow_assignment({"status": "ok", "send_lan": True})
    payload = node._build_heartbeat_payload()
    assert payload["hardware"]["lan_ipv4"] == ["192.168.40.50"]
    assert _verify(payload)[0] is True
    assert "lan_ipv4" not in node.hardware_profile
    node._follow_assignment({"status": "ok"})                  # a coordinator that stops asking
    assert "lan_ipv4" not in node._build_heartbeat_payload()["hardware"]
    node._follow_assignment({"status": "ok", "send_lan": "yes"})
    assert "lan_ipv4" not in node._build_heartbeat_payload()["hardware"]


@pytest.mark.parametrize("value, expected", [
    ("ws://192.168.40.23:11435", "ws://192.168.40.23:11435"),
    ("ws://10.0.0.5:8080/", "ws://10.0.0.5:8080"),
    ("wss://192.168.40.23:11435", None),              # plain ws only: nothing else on the LAN speaks TLS here
    ("ws://8.8.8.8:11435", None),                     # never a public address: the tunnel carries the token
    ("ws://127.0.0.1:11435", None),
    ("ws://169.254.1.1:11435", None),
    ("ws://192.168.40.23", None),
    ("ws://192.168.40.23:11435/elsewhere", None),
    ("ws://user@192.168.40.23:11435", None),
    ("ws://dlne.example:11435", None),
    ("ws://192.168.40.23:99999", None),
    (None, None), (42, None), ("", None),
])
def test_only_a_plain_ws_url_to_a_private_address_is_taken_for_the_lan_tunnel(value, expected):
    assert worker._lan_tunnel_url(value) == expected


def test_the_lan_tunnel_follows_the_coordinators_answers(node, monkeypatch):
    _limits(monkeypatch)
    assert node._lan_tunnel is None
    node._follow_assignment({"status": "ok", "send_lan": True, "lan_tunnel": "ws://192.168.40.23:11435"})
    assert node._lan_tunnel == "ws://192.168.40.23:11435"
    node._follow_assignment({"status": "ok"})
    assert node._lan_tunnel is None


def test_the_lan_addresses_are_private_ipv4_only(monkeypatch):
    import socket as socket_module
    import psutil

    def entry(address, family=socket_module.AF_INET):
        return SimpleNamespace(family=family, address=address, netmask="255.255.255.0")

    monkeypatch.setattr(psutil, "net_if_addrs", lambda: {
        "lo": [entry("127.0.0.1")], "wlan0": [entry("192.168.40.50"), entry("fe80::1", socket_module.AF_INET6)],
        "docker0": [entry("172.17.0.1")], "tun0": [entry("100.64.0.2")], "eth9": [entry("73.215.216.195")],
        "x": [entry("169.254.9.9")],
    })
    assert worker._lan_ipv4_addresses() == ["192.168.40.50", "172.17.0.1"]


def test_the_coding_job_is_refused_quietly_when_the_owner_says_no(node, monkeypatch):
    _limits(monkeypatch, coding_member_allowed=False)
    calls = fake_starts(node, monkeypatch)
    node.rpc_process = FakeProc()
    for _ in range(5):
        node._follow_assignment({"status": "ok", "role": "coding", "coding_tier": {"id": "t"}})
    settle(node)
    assert calls == [] and roles_running(node) == ["rpc"] and not node.rpc_process.terminated
    assert node._role_hold == {}, "no hold-off: there is nothing to retry"
    assert node._role_target is None and node._role_busy is False


def test_the_refusal_is_logged_once_until_the_owner_changes_their_mind(node, monkeypatch):
    lines = []
    monkeypatch.setattr(worker, "log_worker", lambda text: lines.append(text))
    state = {"allowed": False}
    monkeypatch.setattr(worker, "get_worker_limits", lambda: {"network_key": KEY, "coding_member_allowed": state["allowed"]})
    calls = fake_starts(node, monkeypatch)
    node.rpc_process = FakeProc()
    for _ in range(3):
        node._follow_assignment({"role": "coding"})
    assert len([line for line in lines if "CODING_MEMBER_ALLOWED" in line]) == 1
    state["allowed"] = True                      # the owner edits config.txt: the next offer is taken
    node._follow_assignment({"role": "coding"})
    settle(node)
    assert calls == ["coding"] and roles_running(node) == ["coding"]


@pytest.mark.parametrize("gpu", [None, {}, {"has_gpu": False, "vram_total_gb": 0}, "not a gpu"])
def test_a_node_with_no_usable_gpu_refuses_the_coding_job(node, monkeypatch, gpu):
    """SR-07: the coordinator is not the only thing that can send role "coding"; a CPU-only machine is a
    phone-pool member and must not start a coding model on its CPU."""
    _limits(monkeypatch, coding_member_allowed=True)
    lines = []
    monkeypatch.setattr(worker, "log_worker", lambda text: lines.append(text))
    node.hardware_profile = {**CPU_HW, "gpu": gpu}
    calls = fake_starts(node, monkeypatch)
    node.app_member = RecordingMember()
    for _ in range(3):
        node._follow_assignment({"status": "ok", "role": "coding", "coding_tier": {"id": "t"}})
    settle(node)
    assert calls == [] and roles_running(node) == ["app"] and node._role_hold == {}
    assert len([line for line in lines if "no usable GPU" in line]) == 1, "said once"


def test_a_node_with_a_gpu_still_takes_the_coding_job(node, monkeypatch):
    _limits(monkeypatch, coding_member_allowed=True)
    calls = fake_starts(node, monkeypatch)
    node.rpc_process = FakeProc()
    node._follow_assignment({"role": "coding"})
    settle(node)
    assert calls == ["coding"] and roles_running(node) == ["coding"]


def test_the_heartbeat_does_not_offer_a_cpu_only_node_for_coding(node, monkeypatch):
    _limits(monkeypatch, coding_member_allowed=True)
    node.hardware_profile = dict(CPU_HW)
    assert node._build_heartbeat_payload()["hardware"]["coding_ok"] is False
    node.hardware_profile = dict(GPU_HW)
    assert node._build_heartbeat_payload()["hardware"]["coding_ok"] is True
    _limits(monkeypatch, coding_member_allowed=False)
    assert node._build_heartbeat_payload()["hardware"]["coding_ok"] is False


def test_a_node_that_loses_its_gpu_gives_the_coding_job_up(node, monkeypatch):
    _limits(monkeypatch, coding_member_allowed=True)
    calls = fake_starts(node, monkeypatch)
    old = node.coding_member = RecordingMember()
    node.hardware_profile = dict(CPU_HW)
    node._follow_assignment({"role": "coding", "coding_tier": {"id": "t"}})
    settle(node)
    assert old.stopped and calls == [("app", True)] and roles_running(node) == ["app"]


def test_the_other_jobs_are_not_affected_by_the_switch(node, monkeypatch):
    _limits(monkeypatch, coding_member_allowed=False)
    calls = fake_starts(node, monkeypatch)
    node.rpc_process = FakeProc()
    node._follow_assignment({"role": "app", "app_model": "a"})
    settle(node)
    assert calls == [("app", True)] and roles_running(node) == ["app"]


def test_a_member_whose_owner_switches_coding_off_goes_back_to_its_default_role(node, monkeypatch):
    _limits(monkeypatch, coding_member_allowed=False)
    calls = fake_starts(node, monkeypatch)
    old = node.coding_member = RecordingMember()
    node._follow_assignment({"role": "coding", "coding_tier": {"id": "t"}})      # even while the answer still asks for it
    settle(node)
    assert old.stopped and calls == ["rpc"] and roles_running(node) == ["rpc"]
    assert node._role_hold == {}


def test_the_coordinators_own_hybrid_worker_is_left_alone_by_the_switch(node, monkeypatch):
    _limits(monkeypatch, coding_member_allowed=False)
    calls = fake_starts(node, monkeypatch)
    node.role = "hybrid"
    node.coding_member = RecordingMember()
    node._follow_assignment({"role": "coding"})
    settle(node)
    assert calls == [] and node.coding_member.stopped is False


# ======================================================================================
# The GPU environment (moved out of start_rpc_server so both roles use it)
# ======================================================================================

VULKAN = "GPU0:\n\tdeviceType = PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU\nGPU1:\n\tdeviceType = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU\n"


@pytest.fixture
def clean_env(monkeypatch):
    for name in ("CUDA_VISIBLE_DEVICES", "GGML_VK_VISIBLE_DEVICES"):
        monkeypatch.delenv(name, raising=False)


def test_no_gpu_limit_leaves_the_environment_alone(node, monkeypatch, clean_env):
    monkeypatch.setattr(worker.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout=""))
    env, allowed = node._gpu_environment()
    assert allowed == "-1" and "CUDA_VISIBLE_DEVICES" not in env and "GGML_VK_VISIBLE_DEVICES" not in env


def test_a_gpu_limit_is_applied(node, monkeypatch, clean_env):
    monkeypatch.setattr(worker, "get_worker_limits", lambda: {"allowed_gpu_indices": "1,2"})
    env, allowed = node._gpu_environment()
    assert allowed == "1,2" and env["CUDA_VISIBLE_DEVICES"] == env["GGML_VK_VISIBLE_DEVICES"] == "1,2"


def test_cpu_only_isolation_hides_every_gpu(node, monkeypatch, clean_env):
    monkeypatch.setattr(worker, "get_worker_limits", lambda: {"allowed_gpu_indices": "NONE"})
    env, allowed = node._gpu_environment()
    assert allowed == "none" and env["CUDA_VISIBLE_DEVICES"] == "" and env["GGML_VK_VISIBLE_DEVICES"] == ""


def test_vulkan_is_kept_off_an_integrated_gpu_when_a_discrete_one_is_present(node, monkeypatch, clean_env):
    monkeypatch.setattr(worker.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=VULKAN))
    env, _ = node._gpu_environment()
    assert env["GGML_VK_VISIBLE_DEVICES"] == "1"


def test_cards_named_by_pci_address_reach_the_process_as_ids(node, monkeypatch, clean_env):
    from engine import hardware
    monkeypatch.setattr(hardware, "get_all_gpus", lambda: [
        {"id": 0, "name": "A", "address": "0000:03:00.0", "vram_total_gb": 8.0, "vk_index": 1},
        {"id": 1, "name": "B", "address": "0000:09:00.0", "vram_total_gb": 8.0, "vk_index": 2}])
    monkeypatch.setattr(worker, "get_worker_limits", lambda: {"allowed_gpu_indices": "0000:09:00.0,0000:03:00.0"})
    env, allowed = node._gpu_environment()
    assert allowed == "0000:09:00.0,0000:03:00.0"
    assert env["CUDA_VISIBLE_DEVICES"] == "1,0" and env["GGML_VK_VISIBLE_DEVICES"] == "2,1"


def test_the_media_card_is_hidden_from_the_nodes_own_process(node, monkeypatch, clean_env, vulkan_install):
    from engine import hardware
    monkeypatch.setattr(hardware, "get_all_gpus", lambda: [
        {"id": 0, "name": "A", "address": "0000:03:00.0", "vram_total_gb": 8.0, "vk_index": 1},
        {"id": 1, "name": "B", "address": "0000:0c:00.0", "vram_total_gb": 8.0, "vk_index": 3}])
    monkeypatch.setattr(hardware, "counts_as_gpu", lambda name, integrated=None: True)
    monkeypatch.setattr(worker, "get_worker_limits", lambda: {"allowed_gpu_indices": "-1", "media_gpu": "0000:0c:00.0"})
    env, _ = node._gpu_environment()
    assert env["CUDA_VISIBLE_DEVICES"] == "0" and env["GGML_VK_VISIBLE_DEVICES"] == "1"


def test_on_a_cuda_build_the_media_card_stays_in_the_nodes_environment(node, monkeypatch, tmp_path, clean_env):
    from engine import hardware, inference_manager
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "ggml-cuda.dll").write_bytes(b"")
    monkeypatch.setattr(inference_manager, "get_base_dir", lambda: str(tmp_path))
    monkeypatch.setattr(inference_manager, "_media_gpu_warned", False)
    warnings = []
    monkeypatch.setattr(inference_manager, "log_warning", warnings.append)
    monkeypatch.setattr(hardware, "get_all_gpus", lambda: [
        {"id": 0, "name": "A", "address": "0000:03:00.0", "vram_total_gb": 8.0, "vk_index": 1},
        {"id": 1, "name": "B", "address": "0000:0c:00.0", "vram_total_gb": 8.0, "vk_index": 3}])
    monkeypatch.setattr(hardware, "counts_as_gpu", lambda name, integrated=None: True)
    monkeypatch.setattr(worker, "get_worker_limits", lambda: {"allowed_gpu_indices": "-1", "media_gpu": "0000:0c:00.0"})
    monkeypatch.setattr(worker.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout=""))
    env, _ = node._gpu_environment()
    node._gpu_environment()
    assert "CUDA_VISIBLE_DEVICES" not in env and "GGML_VK_VISIBLE_DEVICES" not in env     # nothing hidden, nothing named
    assert len(warnings) == 1 and "not a Vulkan build" in warnings[0]


def test_start_rpc_server_still_uses_that_environment(node, monkeypatch, tmp_path, clean_env):
    seen = {}
    monkeypatch.setattr(worker, "download_and_extract_engine", lambda *a, **k: "/bin/ggml-rpc-server")
    monkeypatch.setattr(worker, "_rpc_server_supports_cache", lambda binary: False)
    monkeypatch.setattr(worker, "read_rpc_banner", lambda *a, **k: "8.0.1")
    monkeypatch.setattr(node, "_gpu_environment", lambda: ({"MARK": "1"}, "-1"))

    def popen(args, **kwargs):
        seen.update(args=args, env=kwargs["env"])
        return FakeProc()

    monkeypatch.setattr(worker.subprocess, "Popen", popen)
    assert node.start_rpc_server() is True
    assert seen["env"] == {"MARK": "1"} and seen["args"][:3] == ["/bin/ggml-rpc-server", "--host", node.rpc_ip]
    assert node.rpc_proto == "8.0.1"
    node.rpc_log_file.close()


# ---- 2.5.0: leaving the swarm gracefully, and handing the GPU back after a long yield ------------------------

class _Alive:
    def poll(self):
        return None


def test_a_normal_stop_of_an_rpc_node_tells_the_coordinator_first(node, monkeypatch):
    _limits(monkeypatch)
    node.rpc_process = _Alive()
    node._latest_answer = {"status": "ok", "shard": True}                 # the engine runs on this node
    node.request_shutdown(0, "the owner clicked Stop")
    assert node.is_running and not node._shutdown_event.is_set(), "not stopped yet"
    payload = node._build_heartbeat_payload()
    assert payload["hardware"]["leaving"] is True and _verify(payload)[0] is True
    node._after_answer({"status": "ok", "draining": True})
    assert node.is_running, "the engine still runs on this shard: wait"
    node._after_answer({"status": "ok"})
    assert not node.is_running and node._shutdown_event.is_set() and node.exit_code == 0


def test_a_heartbeat_loop_that_ends_does_not_wait_on_itself(node, monkeypatch):
    """2026-10-27: a window reload broke the worker's stdout, Rich answered with SystemExit, the loop ended, and its
    own stop was deferred to the loop that had just ended: the node hung for minutes with its RPC server gone."""
    _limits(monkeypatch)
    node.rpc_process = _Alive()
    node._latest_answer = {"status": "ok", "shard": True}

    def broken_pipe():
        raise SystemExit(1)

    monkeypatch.setattr(node, "_probe_hardware", broken_pipe)
    with pytest.raises(SystemExit):
        node.heartbeat_loop()
    assert not node.is_running and node._shutdown_event.is_set() and not node._leaving


def test_a_closed_stdout_does_not_stop_the_node(monkeypatch):
    """The editor that started a worker can go away (a window reload) while the worker runs on, by design."""
    from engine import logger
    read_end, write_end = os.pipe()
    os.close(read_end)
    stream = logger._StdoutThatSurvives(os.fdopen(write_end, "w"))
    monkeypatch.setattr(sys, "stdout", stream)
    print("DLNE_DOCK: {}", flush=True)
    logger.log_error("still here")          # Rich used to answer the broken pipe with SystemExit
    logger.log_worker("and here")
    print("DLNE_YIELD: {}", flush=True)


def test_the_leave_ends_when_the_grace_is_over_or_on_a_second_request(node, monkeypatch):
    _limits(monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr(worker.time, "monotonic", lambda: clock[0])
    node.rpc_process = _Alive()
    node._latest_answer = {"status": "ok", "shard": True}
    node.request_shutdown(0, "stop")
    clock[0] += worker.LEAVE_GRACE_SECONDS + 1
    node._after_answer({"status": "ok", "draining": True})
    assert not node.is_running, "the grace is over"
    other = worker.WorkerNode("http://coordinator:11435")
    other.is_running = True
    other.rpc_process = _Alive()
    other._latest_answer = {"status": "ok", "shard": True}
    other.request_shutdown(0, "stop")
    assert other.is_running
    other.request_shutdown(0, "stop again")
    assert not other.is_running, "a second request stops at once"
    other._stopped = True
    reserve = worker.WorkerNode("http://coordinator:11435")
    reserve.is_running = True
    reserve.rpc_process = _Alive()
    reserve._latest_answer = {"status": "ok", "role": "rpc"}
    reserve.request_shutdown(0, "stop")
    assert not reserve.is_running, "the engine does not run on it: nothing to drain, stop at once"
    reserve._stopped = True
    member = worker.WorkerNode("http://coordinator:11435")
    member.is_running = True
    member.request_shutdown(0, "stop")
    assert not member.is_running, "a node without an RPC server (a member) stops at once"
    member._stopped = True


def test_a_yield_that_outlasts_the_drain_hands_the_vram_back_once(node, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(worker.time, "monotonic", lambda: clock[0])
    restarts = []
    monkeypatch.setattr(node, "_restart_rpc_server_for_owner", lambda: restarts.append(1))
    node.rpc_process = _Alive()
    node.hardware_profile = {**GPU_HW, "is_yielding": True}
    node._after_answer({"status": "ok", "draining": True})
    clock[0] += worker.LEAVE_GRACE_SECONDS + 1
    node._after_answer({"status": "ok", "draining": True})
    assert restarts == [], "the engine still runs on it: the coordinator is draining"
    node._after_answer({"status": "ok"})
    assert restarts == [1]
    node._after_answer({"status": "ok"})
    assert restarts == [1], "once"
    node.hardware_profile = dict(GPU_HW)
    node._after_answer({"status": "ok"})
    node.hardware_profile = {**GPU_HW, "is_yielding": True}
    node._after_answer({"status": "ok"})
    clock[0] += worker.LEAVE_GRACE_SECONDS + 1
    node._after_answer({"status": "ok"})
    assert restarts == [1, 1], "a new yield, a new grace"


def test_the_worker_prints_the_yield_marker_once_per_change(node, monkeypatch, capsys):
    import json
    monkeypatch.setattr(worker, "hardware_yield_reason", lambda: "gpu")
    node.hardware_profile = {**GPU_HW, "is_yielding": True}
    node._report_yield()
    node._report_yield()
    node.hardware_profile = dict(GPU_HW)
    node._report_yield()
    lines = [l for l in capsys.readouterr().out.splitlines() if l.startswith(worker.YIELD_MARKER)]
    assert [json.loads(l[len(worker.YIELD_MARKER):]) for l in lines] == [
        {"yielding": True, "why": "gpu"}, {"yielding": False, "why": ""}]
