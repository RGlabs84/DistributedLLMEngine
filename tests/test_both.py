"""
"Both": a GPU machine whose CPU also serves the phone pool (engine/companion.py, docs/coding-members.md section 1).

The GPU node does whatever job the coordinator gives it; the CPU is a second node of the same process, the companion
(``<worker id>-cpu``, ``companion_of`` in its hardware profile), which has exactly one job, the phone pool's. These
tests are about: the companion's identity and profile, the thread split, RAM ordering, giving way to the owner, the
coordinator counting the machine once, and every single-job node staying exactly as it was.
"""

import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from engine import authn, companion, hardware, worker
from engine import config as engine_config
from engine.app_member import AppMember
from engine.registry import WorkerRegistry, is_app_member, is_companion

KEY = "a-network-key-for-tests"
GPU_HW = {"gpu": {"has_gpu": True, "vram_total_gb": 8.0, "name": "NVIDIA GeForce RTX 4070 Laptop GPU"},
          "ram_gb": 4.0, "cpu_cores": 8, "cpu_threads": 16, "cpu_arch": "x86_64", "cpu_features": ["avx2", "fma", "f16c"],
          "is_yielding": False}


@pytest.fixture
def nodes(monkeypatch, tmp_path):
    """A factory for worker nodes with the machine's probes replaced."""
    import engine.setup_core as setup_core
    monkeypatch.setattr(setup_core, "get_or_create_worker_id", lambda: "gpu-worker-id")
    profile = dict(GPU_HW)
    monkeypatch.setattr(worker, "get_hardware_profile", lambda: dict(profile))
    monkeypatch.setattr(hardware, "get_memory_bandwidth_gbs", lambda: 42.5)
    monkeypatch.setattr(worker, "_write_token_to_disk", lambda token: None)
    monkeypatch.setattr(worker, "HEARTBEAT_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(worker, "get_worker_limits", lambda: {"network_key": KEY})
    monkeypatch.setattr(worker, "get_base_dir", lambda: str(tmp_path))
    made = []

    def make(**kwargs):
        node = worker.WorkerNode("http://coordinator:11435", **kwargs)
        node.is_running = True
        made.append(node)
        return node

    make.profile = profile
    yield make
    for node in made:
        node._stopped = True


def _limits(monkeypatch, **limits):
    monkeypatch.setattr(worker, "get_worker_limits", lambda: {"network_key": KEY, **limits})


class Member:
    """A stand-in for an app or coding member."""

    def __init__(self, status=None, settled=False):
        self._status = status or {"model": None, "ready": False, "slots": 2}
        self._settled = settled
        self.assigned, self.stopped = [], False

    def status(self):
        return dict(self._status)

    def settled(self):
        return self._settled

    def assign(self, value, slots=None):
        self.assigned.append(value)

    def stop(self):
        self.stopped = True


def _run_beats(node, monkeypatch, replies):
    sent = []

    def fake_post(url, json=None, timeout=None):
        sent.append(json)
        reply = replies[len(sent) - 1]
        if len(sent) >= len(replies):
            node.request_shutdown(0, "test finished")
        request = httpx.Request("POST", url)
        if isinstance(reply, httpx.Response):
            reply.request = request
            return reply
        return httpx.Response(200, json=reply, request=request)

    monkeypatch.setattr(worker.httpx, "post", fake_post)
    node.heartbeat_loop()
    return sent


# ======================================================================================
# The pieces: the thread split, the RAM lent, the config key
# ======================================================================================

@pytest.mark.parametrize("limit, cores, expected", [
    (-1, 16, (8, 8)), (-1, 8, (4, 4)), (-1, 4, (2, 2)), (-1, 2, (1, 1)), (-1, 32, (8, 24)), (-1, 6, (3, 3)),
    (8, 16, (4, 4)), (6, 16, (3, 3)), (3, 16, (1, 2)), (2, 16, (1, 1)), (1, 16, None), (0, 16, None),
    (12, 8, (4, 8)), (24, 8, (4, 20)), (-1, 1, None), (None, 8, (4, 4)),
])
def test_the_thread_split_gives_the_pool_its_share_and_the_coding_model_the_rest(limit, cores, expected):
    split = companion.split_cpu_threads(limit, cores)
    assert split == expected
    if split:
        total = limit if isinstance(limit, int) and limit >= 0 else cores
        assert sum(split) == total, "MAX_CPU_THREADS is the total, never exceeded"
        assert split[0] >= 1 and split[1] >= 1


@pytest.mark.parametrize("value, expected", [
    (16, 16.0), (12.5, 12.5), (0.1, 0.1), (0, None), (-1, None), (None, None), ("16", None), (True, None), ("x", None),
])
def test_the_ram_lent_to_the_pool_is_off_unless_positive(value, expected):
    assert companion.also_cpu_ram_gib({"also_cpu_ram_gb": value}) == expected
    assert companion.also_cpu_ram_gib({}) is None


@pytest.mark.parametrize("line, expected", [
    ("", None), ("ALSO_CPU_RAM_GB=16", 16.0), ("ALSO_CPU_RAM_GB=12.5", 12.5), ("ALSO_CPU_RAM_GB=0", 0.0),
    ("ALSO_CPU_RAM_GB=junk", None), ("ALSO_CPU_RAM_GB=nan", None), ("ALSO_CPU_RAM_GB=", None),
])
def test_the_config_key_is_a_number_and_absent_means_off(isolated_config, monkeypatch, line, expected):
    isolated_config.write_text("MAX_RAM_GB=4.0\n" + line + "\n", encoding="utf-8")
    monkeypatch.setattr(engine_config, "_cached_stamp", None)
    limits = engine_config.get_worker_limits()
    assert limits["also_cpu_ram_gb"] == expected and limits["max_ram_gb"] == 4.0


def test_the_command_line_writes_the_key(isolated_config, monkeypatch):
    import sys
    import dlne_server
    written = []
    monkeypatch.setattr(dlne_server, "set_config_values", lambda updates: written.append(dict(updates)))

    class Fake:
        def __init__(self, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(worker, "WorkerNode", Fake)
    for flags, expected in [((), []), (("--also-cpu-ram", "16"), [{"ALSO_CPU_RAM_GB": "16.0"}]),
                            (("--also-cpu-ram", "0"), [{"ALSO_CPU_RAM_GB": "0.0"}]),
                            (("--max-ram", "4", "--also-cpu-ram", "12"), [{"MAX_RAM_GB": "4.0", "ALSO_CPU_RAM_GB": "12.0"}])]:
        written.clear()
        monkeypatch.setattr(sys, "argv", ["dlne_server.py", "worker", "--coordinator", "http://c:1", "--rpc-port", "50052", *flags])
        dlne_server.main()
        assert written == expected


# ======================================================================================
# The companion node
# ======================================================================================

def test_a_companion_is_a_cpu_only_phone_pool_node_named_after_its_gpu_node(nodes):
    gate = threading.Event()
    node = nodes(companion_of="gpu-worker-id", companion_ram_gb=16.0, companion_threads=4, companion_gate=gate)
    assert node.worker_id == "gpu-worker-id-cpu" and node.companion_of == "gpu-worker-id"
    profile = node.hardware_profile
    assert profile["gpu"]["has_gpu"] is False and profile["gpu"]["vram_total_gb"] == 0
    assert profile["ram_gb"] == 16.0, "what it lends, not what the GPU node reports"
    assert profile["companion_of"] == "gpu-worker-id" and profile["mem_bandwidth_gbs"] == 42.5
    assert profile["cpu_cores"] == 8 and profile["cpu_features"] == ["avx2", "fma", "f16c"], "the rest is the machine's"
    assert node._serves_cpu_pool() is True, "so it starts the phone pool's member, not an RPC server"


def test_a_companions_heartbeat_is_a_signed_app_member_that_names_its_gpu_node(nodes):
    node = nodes(companion_of="gpu-worker-id", companion_ram_gb=16.0, companion_threads=4)
    node.app_member = Member({"model": "qwen36-a3b", "ready": True, "slots": 2})
    node.rpc_port = 50053
    payload = node._build_heartbeat_payload()
    hw = payload["hardware"]
    assert payload["worker_id"] == "gpu-worker-id-cpu" and payload["port"] == 50053
    assert hw["pool"] == "app" and hw["app"]["model"] == "qwen36-a3b" and hw["companion_of"] == "gpu-worker-id"
    assert hw["coding_ok"] is False, "a companion is never offered the coding job"
    assert is_app_member(hw) and is_companion(hw)
    assert authn.validate_hardware_profile(hw) == (True, "ok")
    ok, _ = authn.verify_heartbeat(KEY, payload["worker_id"], payload["port"], payload["token"], payload["timestamp"],
                                   payload["nonce"], payload["signature"], payload["version"], hw,
                                   role=payload["role"], staged_version=payload["staged_version"])
    assert ok is True
    assert authn.verify_heartbeat(KEY, payload["worker_id"], payload["port"], payload["token"], payload["timestamp"],
                                  payload["nonce"], payload["signature"], payload["version"],
                                  {**hw, "companion_of": "someone-else"}, role=payload["role"],
                                  staged_version=payload["staged_version"])[0] is False, "it cannot be re-pointed in flight"


@pytest.mark.parametrize("bad", ["", 5, ["x"], "x" * 65, True])
def test_companion_of_must_be_a_short_id_and_only_a_pool_member_has_one(bad):
    hw = {"pool": "app", "app": {"model": None, "ready": False, "slots": 2}, "companion_of": bad}
    assert authn.validate_hardware_profile(hw)[0] is False
    assert authn.validate_hardware_profile({"companion_of": "gpu-id"})[0] is False
    assert authn.validate_hardware_profile({"pool": "app", "companion_of": "gpu-id"}) == (True, "ok")


def test_only_the_gpu_node_writes_the_token_the_editor_reads(nodes, monkeypatch):
    written = []
    monkeypatch.setattr(worker, "_write_token_to_disk", lambda token: written.append(token))
    primary, companion_node = nodes(), nodes(companion_of="gpu-worker-id", companion_ram_gb=16.0)
    _run_beats(companion_node, monkeypatch, [{"status": "ok"}])
    assert written == []
    _run_beats(primary, monkeypatch, [{"status": "ok"}])
    assert len(written) == 1 and written[0] == primary.access_token


def test_a_companion_takes_its_phone_model_and_ignores_the_gpu_nodes_job(nodes, monkeypatch, capsys):
    node = nodes(companion_of="gpu-worker-id", companion_ram_gb=16.0)
    node.app_member = Member()
    switched = []
    monkeypatch.setattr(node, "_switch_role", lambda target: switched.append(target))
    capsys.readouterr()
    node._follow_assignment({"role": "coding", "coding_tier": {"id": "t"}, "app_model": "qwen36-a3b"})
    node._follow_assignment({"role": "rpc"})
    assert switched == [] and node.app_member.assigned == ["qwen36-a3b", None]
    node._announce_role()
    node._announce_role()
    assert capsys.readouterr().out == "DLNE_ALSO: app\n", "its own marker, once, and never DLNE_ROLE"


def test_a_companion_the_coordinator_refuses_stops_itself_and_does_not_tell_the_editor_to_stop(nodes, monkeypatch, capsys):
    refusal = httpx.Response(403, headers={"X-DLNE-Refusal": "cpu-minimum"}, json={"detail": "needs 8 GB"})
    companion_node = nodes(companion_of="gpu-worker-id", companion_ram_gb=4.0)
    capsys.readouterr()
    _run_beats(companion_node, monkeypatch, [refusal])
    assert worker.REFUSAL_MARKER not in capsys.readouterr().out
    assert companion_node._shutdown_event.is_set()
    primary = nodes()
    _run_beats(primary, monkeypatch, [httpx.Response(403, headers={"X-DLNE-Refusal": "cpu-minimum"}, json={"detail": "needs 8 GB"})])
    assert worker.REFUSAL_MARKER + "needs 8 GB" in capsys.readouterr().out, "an ordinary node still tells the editor"


def test_a_companions_app_member_runs_on_its_threads_and_the_port_the_pool_dials(nodes, monkeypatch):
    node = nodes(companion_of="gpu-worker-id", companion_ram_gb=16.0, companion_threads=3)
    monkeypatch.setattr(worker, "download_and_extract_engine", lambda *a, **k: "x")
    monkeypatch.setattr(hardware, "get_cpu_cores", lambda: 8)
    assert node.start_app_member() is True
    assert isinstance(node.app_member, AppMember) and node.app_member.threads == 3 and node.rpc_port == 50053
    plain = nodes()
    assert plain.start_app_member() is True and plain.app_member.threads == 4, "a CPU-only node: half the cores, as before"


# ======================================================================================
# The GPU node plans the second job
# ======================================================================================

def test_a_gpu_node_with_a_ram_to_lend_plans_the_companion(nodes, monkeypatch):
    _limits(monkeypatch, also_cpu_ram_gb=16.0, max_cpu_threads=-1)
    monkeypatch.setattr(hardware, "get_cpu_cores", lambda: 8)
    node = nodes()
    node._plan_companion()
    assert node._thread_split == (4, 4) and node._companion_lent == 16.0
    assert isinstance(node._companion_gate, threading.Event) and not node._companion_gate.is_set()


@pytest.mark.parametrize("limits, mutate", [
    ({}, None),                                                    # nothing lent: a single-job node
    ({"also_cpu_ram_gb": 0.0}, None), ({"also_cpu_ram_gb": -1.0}, None),
    ({"also_cpu_ram_gb": 16.0, "max_cpu_threads": 1}, None),       # too few threads for two jobs
    ({"also_cpu_ram_gb": 16.0}, "cpu_only"),                       # no usable GPU: "both" acts as "cpu"
    ({"also_cpu_ram_gb": 16.0}, "hybrid"),                         # the coordinator's own worker
    ({"also_cpu_ram_gb": 16.0}, "companion"),                      # a companion has no companion
])
def test_otherwise_nothing_is_planned_and_the_node_is_as_it_always_was(nodes, monkeypatch, limits, mutate):
    _limits(monkeypatch, **limits)
    monkeypatch.setattr(hardware, "get_cpu_cores", lambda: 8)
    if mutate == "cpu_only":
        nodes.profile["gpu"] = {"has_gpu": False, "vram_total_gb": 0}
    node = nodes(companion_of="gpu-worker-id", companion_ram_gb=16.0) if mutate == "companion" else nodes()
    if mutate == "hybrid":
        node.role = "hybrid"
    node._plan_companion()
    assert (node._thread_split, node._companion_gate, node._companion_lent) == (None, None, 0.0)
    node._launch_companion()
    assert node._companion is None


def test_the_companion_is_launched_with_its_share_and_ended_with_the_node(nodes, monkeypatch, capsys):
    _limits(monkeypatch, also_cpu_ram_gb=16.0, max_cpu_threads=8)
    monkeypatch.setattr(hardware, "get_cpu_cores", lambda: 8)
    release, started = threading.Event(), []

    def fake_start(self):
        started.append(self)
        release.wait(10)

    monkeypatch.setattr(worker.WorkerNode, "start", fake_start)
    node = nodes()
    node._plan_companion()
    capsys.readouterr()
    node._launch_companion()
    deadline = time.monotonic() + 10
    while not started and time.monotonic() < deadline:
        time.sleep(0.01)
    child = started[0]
    assert (child.worker_id, child.companion_of, child.companion_ram_gb, child.companion_threads) == (
        "gpu-worker-id-cpu", "gpu-worker-id", 16.0, 4)
    assert child.companion_gate is node._companion_gate and child.verbose_heartbeats is False
    assert node._companion is child
    stopped = []
    monkeypatch.setattr(child, "stop", lambda: stopped.append(True))
    node.stop()
    assert stopped == [True] and child._shutdown_event.is_set(), "stopping the GPU node ends its CPU half"
    release.set()


def test_a_companion_that_ends_prints_none_and_lets_the_coding_model_go(nodes, monkeypatch, capsys):
    _limits(monkeypatch, also_cpu_ram_gb=16.0)
    monkeypatch.setattr(hardware, "get_cpu_cores", lambda: 8)
    monkeypatch.setattr(worker.WorkerNode, "start", lambda self: None)
    node = nodes()
    node._plan_companion()
    capsys.readouterr()
    node._run_companion()
    assert capsys.readouterr().out == "DLNE_ALSO: none\n"
    assert node._companion_gate.is_set(), "a pool model that is gone must not hold the coding model back"


def test_a_companion_that_cannot_be_built_leaves_the_gpu_node_working(nodes, monkeypatch):
    _limits(monkeypatch, also_cpu_ram_gb=16.0)
    monkeypatch.setattr(hardware, "get_cpu_cores", lambda: 8)
    node = nodes()
    node._plan_companion()

    def broken(self):
        raise OSError("no engine")

    monkeypatch.setattr(worker.WorkerNode, "start", broken)
    node._run_companion()
    assert node._companion_gate.is_set() and node.exit_code == 0 and not node._shutdown_event.is_set()


# ======================================================================================
# The threads and the RAM, ordered
# ======================================================================================

def test_the_coding_model_gets_the_threads_the_pool_does_not(nodes, monkeypatch):
    _limits(monkeypatch, also_cpu_ram_gb=16.0, max_cpu_threads=10)
    monkeypatch.setattr(hardware, "get_cpu_cores", lambda: 10)
    node = nodes()
    node._plan_companion()
    pool, coding = node._thread_split
    assert pool + coding == 10 and node._coding_resources()["threads"] == coding
    plain = nodes()
    assert plain._coding_resources()["threads"] == 10, "a single-job node: MAX_CPU_THREADS itself, as before"


def test_a_single_job_nodes_coding_threads_are_untouched(nodes, monkeypatch):
    for limit, expected in ((-1, 0), (0, 1), (6, 6)):
        _limits(monkeypatch, max_cpu_threads=limit)
        assert nodes()._coding_resources()["threads"] == expected


def test_while_the_pool_model_is_still_to_load_the_coding_model_is_chosen_for_what_will_be_left(nodes, monkeypatch):
    import psutil
    monkeypatch.setattr(psutil, "virtual_memory", lambda: SimpleNamespace(total=32 * 2 ** 30, available=24 * 2 ** 30))
    _limits(monkeypatch, also_cpu_ram_gb=16.0)
    monkeypatch.setattr(hardware, "get_cpu_cores", lambda: 8)
    node = nodes()
    node._plan_companion()
    assert node._machine_info()["ram_gb"] == 8.0, "24 GB free, 16 of them about to go to the phone pool"
    node._companion_gate.set()
    assert node._machine_info()["ram_gb"] == 24.0, "once it has loaded the free RAM is simply measured"
    single = nodes()
    assert single._machine_info()["ram_gb"] == 24.0


def test_the_pool_model_settles_the_gate_when_it_serves_gives_up_or_takes_too_long(nodes, monkeypatch):
    gate = threading.Event()
    node = nodes(companion_of="gpu-worker-id", companion_ram_gb=16.0, companion_gate=gate)
    node.app_member = Member(settled=False)
    node._follow_assignment({"app_model": "qwen36-a3b"})
    assert not gate.is_set(), "still downloading or loading"
    node.app_member._settled = True
    node._follow_assignment({"app_model": "qwen36-a3b"})
    assert gate.is_set()
    slow = threading.Event()
    late = nodes(companion_of="gpu-worker-id", companion_ram_gb=16.0, companion_gate=slow)
    late.app_member = Member(settled=False)
    monkeypatch.setattr(worker, "GATE_TIMEOUT_SECONDS", -1.0)
    late._follow_assignment({"app_model": "qwen36-a3b"})
    assert slow.is_set(), "a pool model that never settles does not hold the GPU's job forever"


def _app_member(tmp_path):
    member = AppMember(str(tmp_path), lambda: [], threads=2, port=50053)
    return member


class _Alive:
    returncode = None
    pid = 0

    def poll(self):
        return None


def test_an_app_member_is_settled_once_it_serves_or_was_told_to_serve_nothing_or_gave_up(tmp_path):
    member = _app_member(tmp_path)
    assert member.settled() is False, "before the first assignment"
    member._assigned, member.target = True, None
    assert member.settled() is True, "told to serve nothing"
    member.target = "qwen36-a3b"
    assert member.settled() is False, "told to serve something that is not there yet"
    member._busy = True
    assert member.settled() is False, "downloading or loading"
    member._busy = False
    member.process, member.ready = _Alive(), True
    assert member.settled() is True, "serving"
    member.process, member.ready = None, False
    member._backoff["qwen36-a3b"] = (1, time.monotonic() + 60)
    assert member.settled() is True, "its model failed and is backing off"


# ======================================================================================
# Giving way to the owner, for both jobs
# ======================================================================================

def test_when_the_owner_games_both_halves_report_it_and_the_coding_model_gives_way(nodes):
    nodes.profile["is_yielding"] = True
    gpu = nodes()
    gpu.coding_member = SimpleNamespace(yields=[], status=lambda: {"ready": False, "slots": 0},
                                        set_yielding=lambda value: gpu.coding_member.yields.append(value),
                                        stop=lambda: None)
    pool = nodes(companion_of="gpu-worker-id", companion_ram_gb=16.0)
    pool.app_member = Member()
    assert gpu._build_heartbeat_payload()["hardware"]["is_yielding"] is True
    assert gpu.coding_member.yields == [True]
    pool_hw = pool._build_heartbeat_payload()["hardware"]
    assert pool_hw["is_yielding"] is True and pool_hw["pool"] == "app"


def test_the_phone_pool_half_steps_aside_with_its_gpu_half_and_a_single_job_member_does_not(nodes):
    nodes.profile["is_yielding"] = True
    pool = nodes(companion_of="gpu-worker-id", companion_ram_gb=16.0)
    pool.app_member = Member()
    pool.app_member.yields = []
    pool.app_member.set_yielding = pool.app_member.yields.append
    pool._build_heartbeat_payload()
    assert pool.app_member.yields == [True], "the companion's model gives the CPU back (AppMember.set_yielding)"
    single = nodes()
    single.app_member = Member()
    single.app_member.yields = []
    single.app_member.set_yielding = single.app_member.yields.append
    single._build_heartbeat_payload()
    assert single.app_member.yields == [], "a machine that lends only its CPU behaves exactly as before"


# ======================================================================================
# Roles of the GPU node are not the companion's business
# ======================================================================================

def _fake_starts(node, monkeypatch):
    calls = []

    def rpc():
        calls.append("rpc")
        node.rpc_process = SimpleNamespace(poll=lambda: None, terminate=lambda: None, wait=lambda timeout=None: 0)
        return True

    def coding():
        calls.append("coding")
        node.coding_member = Member({"model": None, "ready": False})
        return True

    monkeypatch.setattr(node, "start_rpc_server", rpc)
    monkeypatch.setattr(node, "start_coding_member", coding)
    return calls


@pytest.mark.parametrize("start, target", [("rpc", "coding"), ("coding", "rpc")])
def test_the_gpu_nodes_job_switches_while_its_cpu_half_serves_the_pool(nodes, monkeypatch, start, target):
    _limits(monkeypatch, also_cpu_ram_gb=16.0)
    monkeypatch.setattr(hardware, "get_cpu_cores", lambda: 8)
    node = nodes()
    node._plan_companion()
    half = node._companion = Member({"model": "qwen36-a3b", "ready": True, "slots": 2})
    calls = _fake_starts(node, monkeypatch)
    if start == "rpc":
        node.rpc_process = SimpleNamespace(poll=lambda: None, terminate=lambda: None, wait=lambda timeout=None: 0)
    else:
        node.coding_member = Member()
    node._follow_assignment({"role": target, "coding_tier": {"id": "t"}})
    deadline = time.monotonic() + 10
    while node._role_busy and time.monotonic() < deadline:
        time.sleep(0.02)
    assert calls == [target] and node._current_pool() == target
    assert half.stopped is False, "switching the GPU's job never touches the phone pool's half"
    assert node._companion is half and node._companion_gate is not None


# ======================================================================================
# The coordinator counts the machine once
# ======================================================================================

def _registry():
    reg = WorkerRegistry()
    reg.update_worker("gpu", "10.0.0.1", 50052, "t1", {**GPU_HW, "ram_gb": 32.0, "rpc_proto": "3.0.0"})
    return reg


COMPANION_HW = {"gpu": None, "ram_gb": 16.0, "cpu_cores": 8, "cpu_threads": 16, "is_yielding": False,
                "rpc_proto": "3.0.0", "pool": "app", "app": {"model": "qwen36-a3b", "ready": True, "slots": 2},
                "companion_of": "gpu"}


def test_the_machine_is_counted_once_and_its_totals_are_not_doubled():
    reg = _registry()
    before = (reg.count(), reg.get_total_vram_gb(), reg.get_total_sysram_gb(), reg.get_total_cpu_cores(),
              reg.get_total_cpu_threads(), reg.get_active_rpc_endpoints())
    reg.update_worker("gpu-cpu", "10.0.0.1", 50053, "t2", COMPANION_HW)
    assert (reg.count(), reg.get_total_vram_gb(), reg.get_total_sysram_gb(), reg.get_total_cpu_cores(),
            reg.get_total_cpu_threads(), reg.get_active_rpc_endpoints()) == before
    assert before[0] == 1 and before[1] == 8.0 and before[2] == 32.0


def test_the_phone_pool_counts_the_companion_as_a_member():
    reg = _registry()
    reg.update_worker("gpu-cpu", "10.0.0.1", 50053, "t2", COMPANION_HW)
    members = reg.app_members()
    assert [m["port"] for m in members] == [50053] and is_companion(members[0]["hardware"])


def test_other_nodes_are_counted_exactly_as_before():
    reg = _registry()
    reg.update_worker("cpu", "10.0.0.2", 50053, "t3", {**COMPANION_HW, "companion_of": None})
    reg.update_worker("old", "10.0.0.3", 50052, "t4", {**GPU_HW, "rpc_proto": "3.0.0"})
    assert reg.count() == 3, "a CPU pool member of its own, and a node that has never heard of companions"
    for hw in ({}, None, "x", {"companion_of": ""}, {"companion_of": None}, {"companion_of": 5}):
        assert not is_companion(hw)


# ======================================================================================
# Single-job nodes behave as they always did
# ======================================================================================

def test_an_ordinary_nodes_heartbeat_has_no_trace_of_companions(nodes):
    node = nodes()
    hw = node._build_heartbeat_payload()["hardware"]
    assert "companion_of" not in hw and hw["coding_ok"] is True and "pool" not in hw
    assert node.hardware_profile == dict(GPU_HW), "the probe's profile, untouched"
    assert (node.companion_of, node.companion_gate, node._companion, node._thread_split) == ("", None, None, None)
    assert node.worker_id == "gpu-worker-id"


def test_an_ordinary_cpu_pool_node_is_unchanged(nodes):
    nodes.profile["gpu"] = {"has_gpu": False, "vram_total_gb": 0}
    node = nodes()
    node.app_member = Member({"model": "t", "ready": True, "slots": 2})
    hw = node._build_heartbeat_payload()["hardware"]
    assert hw["pool"] == "app" and "companion_of" not in hw and hw["ram_gb"] == 4.0 and hw["gpu"] == {
        "has_gpu": False, "vram_total_gb": 0}


def test_an_ordinary_node_prints_dlne_role_and_never_dlne_also(nodes, capsys):
    node = nodes()
    capsys.readouterr()
    node._announce_role()
    assert capsys.readouterr().out == "DLNE_ROLE: rpc\n"


def test_a_coding_member_without_a_companion_is_given_no_gate(nodes, monkeypatch):
    made = []
    monkeypatch.setattr(worker, "download_and_extract_engine", lambda *a, **k: "x")
    from engine import coding_member

    class Fake:
        def __init__(self, base_dir, tiers_loader, port, **kwargs):
            made.append(kwargs)

        def start(self):
            return True

    monkeypatch.setattr(coding_member, "CodingMember", Fake)
    node = nodes()
    assert node.start_coding_member() is True and made[0]["start_gate"] is None
    _limits(monkeypatch, also_cpu_ram_gib=None, also_cpu_ram_gb=16.0)
    monkeypatch.setattr(hardware, "get_cpu_cores", lambda: 8)
    both = nodes()
    both._plan_companion()
    both.start_coding_member()
    assert made[1]["start_gate"] is both._companion_gate
