"""
Light mode for a volunteer's CPU-only phone-pool member (engine/app_member.py, docs/coding-members.md section 1).

While the owner games the member does not stop: at once, with no restart, its llama-server drops to the lowest
priority and half its CPUs; after a 10 second drain it is started again lighter (half the threads, one slot, half the
context, no prompt cache); when the load has been over for the settle period it is started again at full strength; a
flickering load costs at most one switch per settle period. The "both" machine's phone-pool half still steps aside
completely, the coordinator host's own member is not touched, and a member whose owner never games is unchanged.
"""

import time
from types import SimpleNamespace

import pytest

from engine import app_member as am
from engine import app_pool, hardware, worker
from engine.app_member import APP_SLOTS, AppMember
from engine.app_pool import app_server_args
from tests.app_pool_fakes import FakeHub, free_port, make_tier, recorded_starts, wait_for, write_fake_llama_binary

DATA = b"GGUF tiny model A " * 64
KEY = "a-network-key-for-tests"


# ======================================================================================
# The pure parts
# ======================================================================================

@pytest.mark.parametrize("threads, expected", [(1, 1), (2, 1), (3, 1), (4, 2), (8, 4), (16, 8)])
def test_light_mode_halves_the_threads_and_keeps_one(threads, expected):
    assert am.light_threads(threads) == expected


@pytest.mark.parametrize("ctx, expected", [(16384, 8192), (8192, 4096), (2048, 1024), (1024, 1024), (10, 1024)])
def test_light_mode_halves_the_context(ctx, expected):
    assert am.light_context(ctx) == expected


@pytest.mark.parametrize("threads", [1, 2, 3, 4, 8])
def test_full_strength_arguments_are_exactly_the_ones_a_member_always_had(threads):
    assert am.app_member_args("/b/llama-server", "/m/a.gguf", 50053, threads) == \
        app_server_args("/b/llama-server", "/m/a.gguf", 50053, threads, slots=APP_SLOTS)
    assert am.app_member_args("/b/llama-server", "/m/a.gguf", 50053, threads, light=False, ctx=8192, slots=3) == \
        app_server_args("/b/llama-server", "/m/a.gguf", 50053, threads, ctx=8192, slots=3)


def test_light_arguments_are_half_the_threads_one_slot_half_the_context_and_no_prompt_cache():
    full = am.app_member_args("/b/llama-server", "/m/a.gguf", 50053, 6)
    light = am.app_member_args("/b/llama-server", "/m/a.gguf", 50053, 6, light=True)

    def value(args, flag):
        return args[args.index(flag) + 1]

    assert (value(full, "-t"), value(full, "-tb"), value(full, "-np"), value(full, "-c")) == ("6", "6", "2", "16384")
    assert (value(light, "-t"), value(light, "-tb"), value(light, "-np"), value(light, "-c")) == ("3", "3", "1", "8192")
    assert light[-2:] == ["--cache-ram", "0"] and "--cache-ram" not in full
    changed = {a for a in full if a not in light} | {a for a in light if a not in full}
    assert changed <= {"6", "3", "2", "1", "16384", "8192", "--cache-ram", "0"}, "everything else is the same"
    assert value(light, "--port") == "50053" and value(light, "-m") == "/m/a.gguf" and "--device" in light


def test_a_single_thread_stays_one_thread():
    light = am.app_member_args("/b/llama-server", "/m/a.gguf", 50053, 1, light=True)
    assert light[light.index("-t") + 1] == "1"


# ---- priority and CPUs, on each operating system -----------------------------------------------------------

class FakeProcess:
    """psutil.Process for the pid under test."""
    instances = {}

    def __init__(self, pid):
        self.pid = pid
        FakeProcess.instances.setdefault(pid, {"nice": [], "affinity": list(range(8)), "log": []})
        self.state = FakeProcess.instances[pid]

    def nice(self, value=None):
        self.state["nice"].append(value)

    def cpu_affinity(self, value=None):
        if value is None:
            return list(self.state["affinity"])
        self.state["log"].append(list(value))
        self.state["affinity"] = list(value)


class FakeProcessNoAffinity(FakeProcess):
    """macOS: psutil has no cpu_affinity there."""

    def __getattribute__(self, name):
        if name == "cpu_affinity":
            raise AttributeError(name)
        return super().__getattribute__(name)


@pytest.fixture
def platform(monkeypatch):
    FakeProcess.instances = {}
    calls = []
    monkeypatch.setattr(am.os, "setpriority", lambda which, pid, value: calls.append((pid, value)), raising=False)
    monkeypatch.setattr(am.os, "PRIO_PROCESS", 0, raising=False)
    fake_psutil = SimpleNamespace(Process=FakeProcess, IDLE_PRIORITY_CLASS=64, BELOW_NORMAL_PRIORITY_CLASS=16384)
    monkeypatch.setattr(am, "psutil", fake_psutil)

    def choose(windows=False, affinity=True):
        monkeypatch.setattr(am, "_on_windows", lambda: windows)
        fake_psutil.Process = FakeProcess if affinity else FakeProcessNoAffinity

    choose()
    return SimpleNamespace(setpriority=calls, choose=choose, procs=lambda: FakeProcess.instances)


def test_linux_drops_to_niceness_19_and_half_the_cpus_and_a_restart_is_the_way_back_up(platform):
    saved = am.lower_priority(4242)
    assert platform.setpriority == [(4242, 19)]
    assert platform.procs()[4242]["affinity"] == [0, 1, 2, 3] and saved == {"affinity": list(range(8))}
    assert am.restore_priority(4242, saved) is True
    assert platform.setpriority[-1] == (4242, 10) and platform.procs()[4242]["affinity"] == list(range(8))


def test_linux_cannot_raise_a_priority_it_lowered_and_says_so(platform, monkeypatch):
    saved = am.lower_priority(1)

    def refuse(which, pid, value):
        raise PermissionError("niceness cannot be lowered again")

    monkeypatch.setattr(am.os, "setpriority", refuse, raising=False)
    assert am.restore_priority(1, saved) is False
    assert platform.procs()[1]["affinity"] == list(range(8)), "the CPUs come back all the same"


def test_windows_uses_the_idle_priority_class_through_psutil(platform):
    platform.choose(windows=True)
    saved = am.lower_priority(77)
    assert platform.setpriority == [], "os.setpriority is not how Windows does it"
    assert platform.procs()[77]["nice"] == [64] and platform.procs()[77]["affinity"] == [0, 1, 2, 3]
    assert am.restore_priority(77, saved) is True
    assert platform.procs()[77]["nice"] == [64, 16384], "back to below normal"
    assert platform.procs()[77]["affinity"] == list(range(8))


def test_macos_has_no_affinity_and_gets_the_priority_alone(platform):
    platform.choose(affinity=False)
    saved = am.lower_priority(9)
    assert platform.setpriority == [(9, 19)] and saved == {"affinity": None}
    assert platform.procs()[9]["log"] == []
    assert am.restore_priority(9, saved) is True and platform.setpriority[-1] == (9, 10)


@pytest.mark.parametrize("cpus, expected", [(list(range(8)), [0, 1, 2, 3]), ([0, 2, 4, 6, 8, 10], [0, 2, 4]),
                                            ([3, 5], [3]), ([7], [7]), (list(range(3)), [0])])
def test_half_the_cpus_it_may_use_and_at_least_one(platform, cpus, expected):
    FakeProcess.instances[5] = {"nice": [], "affinity": list(cpus), "log": []}
    am.lower_priority(5)
    assert platform.procs()[5]["affinity"] == expected


def test_a_process_that_is_gone_or_a_system_that_refuses_is_survived(platform, monkeypatch):
    def gone(pid):
        raise ProcessLookupError(pid)

    monkeypatch.setattr(am.psutil, "Process", gone)
    monkeypatch.setattr(am.os, "setpriority", lambda *a: (_ for _ in ()).throw(PermissionError()), raising=False)
    assert am.lower_priority(3) == {"affinity": None}
    assert am.restore_priority(3, {"affinity": [0, 1]}) is False


# ======================================================================================
# The member
# ======================================================================================

@pytest.fixture
def env(tmp_path, monkeypatch):
    real_sleep = time.sleep
    monkeypatch.setattr(am, "time", SimpleNamespace(monotonic=time.monotonic, sleep=lambda s: real_sleep(min(s, 0.1))))
    monkeypatch.setattr(am, "YIELD_DRAIN_SECONDS", 0.4)
    monkeypatch.setattr(am, "YIELD_SETTLE_SECONDS", 0.8)
    base = tmp_path / "dlne"
    record = str(tmp_path / "starts.jsonl")
    write_fake_llama_binary(str(base / "bin" / "llama-server"), record)
    tier = make_tier("tiny-a", DATA, 8)
    lowered, restored = [], []
    monkeypatch.setattr(am, "lower_priority", lambda pid: lowered.append(pid) or {"affinity": [0, 1, 2, 3]})
    results = {"restore": True}
    monkeypatch.setattr(am, "restore_priority", lambda pid, saved: restored.append((pid, saved)) or results["restore"])
    member = AppMember(str(base), lambda: [tier], threads=6, port=free_port(), urlopen=FakeHub({tier["url"]: DATA}))
    yield SimpleNamespace(member=member, tier=tier, record=record, lowered=lowered, restored=restored, results=results)
    member.stop()


def settle(member, timeout=30.0):
    assert wait_for(lambda: not member._busy, timeout), "the member never finished converging"


def serve(env):
    env.member.assign(env.tier["id"])
    settle(env.member)
    assert env.member.status()["ready"] is True


def flag(argv, name):
    return argv[argv.index(name) + 1]


def test_light_mode_lowers_the_running_server_at_once_and_does_not_restart_it(env):
    serve(env)
    process = env.member.process
    env.member.set_light(True)
    assert env.lowered == [process.pid], "the lowest priority and half the CPUs, now"
    assert env.member.process is process and process.poll() is None and len(recorded_starts(env.record)) == 1
    assert env.member.status() == {"model": env.tier["id"], "ready": True, "slots": 1, "threads": 6}, \
        "routing goes on, with less (the same process: its 6 threads, now on half the CPUs)"
    env.member.set_light(True)
    assert env.lowered == [process.pid], "once"


def test_the_answers_in_flight_get_the_drain_and_then_it_restarts_lighter(env):
    serve(env)
    old = env.member.process
    env.member.set_light(True)
    env.member.assign(env.tier["id"])
    settle(env.member)
    assert env.member.process is old and len(recorded_starts(env.record)) == 1, "not inside the 10 seconds"
    time.sleep(0.5)
    env.member.assign(env.tier["id"])
    settle(env.member)
    assert env.member.process is not old and old.poll() is not None
    argv = recorded_starts(env.record)[1]
    assert (flag(argv, "-t"), flag(argv, "-tb"), flag(argv, "-np"), flag(argv, "-c")) == ("3", "3", "1", "8192")
    assert argv[-2:] == ["--cache-ram", "0"]
    assert env.lowered[-1] == env.member.process.pid, "the new server is lowest priority from the start"
    assert env.member.status() == {"model": env.tier["id"], "ready": True, "slots": 1, "threads": 3}


def test_when_the_load_ends_it_waits_the_settle_period_and_returns_to_full_strength(env):
    serve(env)
    env.member.set_light(True)
    time.sleep(0.5)
    env.member.assign(env.tier["id"])
    settle(env.member)
    light = env.member.process
    lowered_before = list(env.lowered)
    env.member.set_light(False)
    env.member.assign(env.tier["id"])
    settle(env.member)
    assert env.member.process is light and len(recorded_starts(env.record)) == 2, "not before the settle period"
    assert env.member.status()["slots"] == 1
    time.sleep(0.9)
    env.member.assign(env.tier["id"])
    settle(env.member)
    argv = recorded_starts(env.record)[2]
    assert argv == recorded_starts(env.record)[0], "exactly the arguments of the first start"
    assert (flag(argv, "-t"), flag(argv, "-np"), flag(argv, "-c")) == ("6", "2", "16384") and "--cache-ram" not in argv
    assert env.lowered == lowered_before, "the full-strength server is not lowered"
    assert env.member.status() == {"model": env.tier["id"], "ready": True, "slots": APP_SLOTS, "threads": 6}


def test_a_load_that_ends_inside_the_drain_puts_the_priority_back_in_place_after_the_settle_period(env):
    serve(env)
    process = env.member.process
    env.member.set_light(True)
    env.member.set_light(False)
    env.member.assign(env.tier["id"])
    settle(env.member)
    assert env.restored == [], "not yet"
    time.sleep(0.9)
    env.member.assign(env.tier["id"])
    settle(env.member)
    assert env.restored == [(process.pid, {"affinity": [0, 1, 2, 3]})]
    assert env.member.process is process and len(recorded_starts(env.record)) == 1, "no restart was needed"
    assert env.member.status()["slots"] == APP_SLOTS
    env.member.assign(env.tier["id"])
    settle(env.member)
    assert len(env.restored) == 1, "and it is not undone twice"


def test_where_the_priority_cannot_be_raised_again_a_restart_does_it(env):
    env.results["restore"] = False       # Linux: niceness cannot be lowered back
    serve(env)
    old = env.member.process
    env.member.set_light(True)
    env.member.set_light(False)
    time.sleep(0.9)
    env.member.assign(env.tier["id"])
    settle(env.member)
    assert env.member.process is not old and len(recorded_starts(env.record)) == 2
    assert recorded_starts(env.record)[1] == recorded_starts(env.record)[0], "a restart at full strength"


def test_a_load_that_flickers_costs_at_most_one_switch_per_settle_period(env, monkeypatch):
    monkeypatch.setattr(am, "YIELD_DRAIN_SECONDS", 0.0)
    serve(env)
    env.member.set_light(True)
    env.member.assign(env.tier["id"])
    settle(env.member)
    assert len(recorded_starts(env.record)) == 2, "the first switch is at once"
    began = time.monotonic()
    for _ in range(6):                               # the load flickers faster than the settle period
        env.member.set_light(False)
        env.member.assign(env.tier["id"])
        env.member.set_light(True)
        env.member.assign(env.tier["id"])
        settle(env.member)
    env.member.set_light(False)
    env.member.assign(env.tier["id"])
    settle(env.member)
    assert time.monotonic() - began < 0.8
    assert len(recorded_starts(env.record)) == 2, "still light: no restart loop"
    time.sleep(0.9)
    env.member.assign(env.tier["id"])
    settle(env.member)
    assert len(recorded_starts(env.record)) == 3, "one switch back, a settle period after the load ended"
    switched = env.member._last_switch
    env.member.set_light(True)                       # the game starts again straight away
    env.member.assign(env.tier["id"])
    settle(env.member)
    assert len(recorded_starts(env.record)) == 3 and env.member._last_switch == switched, "but not before the settle period"
    time.sleep(0.9)
    env.member.assign(env.tier["id"])
    settle(env.member)
    assert len(recorded_starts(env.record)) == 4
    assert env.member._last_switch - switched >= 0.8, "switches are a settle period apart"


def test_started_while_the_owner_is_already_gaming_it_starts_light(env):
    env.member.set_light(True)
    assert env.lowered == [], "nothing is running yet"
    serve(env)
    argv = recorded_starts(env.record)[0]
    assert (flag(argv, "-t"), flag(argv, "-np"), flag(argv, "-c")) == ("3", "1", "8192")
    assert env.lowered == [env.member.process.pid]
    assert env.member.status()["slots"] == 1


def test_a_stopped_member_and_a_stepped_aside_member_ignore_light_mode(env):
    serve(env)
    env.member.set_yielding(True)            # the companion's full step-aside, a different mode
    env.member.set_light(True)
    assert env.member._light is False and env.lowered == []
    env.member.stop()
    env.member.set_light(True)
    assert env.member._light is False


def test_a_member_whose_owner_never_games_is_not_touched(env, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("priority or CPUs were touched")

    monkeypatch.setattr(am, "restore_priority", explode)
    serve(env)
    process = env.member.process
    for _ in range(5):
        env.member.set_light(False)
        env.member.assign(env.tier["id"])
        settle(env.member)
    assert env.lowered == [] and env.member.process is process
    assert env.member.status() == {"model": env.tier["id"], "ready": True, "slots": APP_SLOTS, "threads": 6}
    assert len(recorded_starts(env.record)) == 1
    argv = recorded_starts(env.record)[0]
    assert argv == app_server_args(env.member._binary(), str(env.member.base_dir) + "/models/" + env.tier["file"],
                                   env.member.port, 6, slots=APP_SLOTS)[1:]


# ======================================================================================
# Which nodes do it
# ======================================================================================

@pytest.fixture
def nodes(monkeypatch, tmp_path):
    import engine.setup_core as setup_core
    monkeypatch.setattr(setup_core, "get_or_create_worker_id", lambda: "node-id")
    profile = {"gpu": {"has_gpu": False, "vram_total_gb": 0}, "ram_gb": 16.0, "cpu_cores": 8, "cpu_threads": 16,
               "cpu_arch": "x86_64", "cpu_features": ["avx2", "fma"], "is_yielding": False}
    monkeypatch.setattr(worker, "get_hardware_profile", lambda: dict(profile))
    monkeypatch.setattr(hardware, "get_memory_bandwidth_gbs", lambda: 40.0)
    monkeypatch.setattr(worker, "get_worker_limits", lambda: {"network_key": KEY})
    monkeypatch.setattr(worker, "get_base_dir", lambda: str(tmp_path))
    made = []

    def make(**kwargs):
        node = worker.WorkerNode("http://coordinator:11435", **kwargs)
        made.append(node)
        return node

    make.profile = profile
    yield make
    for node in made:
        node._stopped = True


class Recorder:
    def __init__(self, serving_light=False):
        self.calls = []
        self.lit = serving_light

    def status(self):
        # A member serving lighter is ready with one slot; one that is not serving (downloading, say) is not ready.
        if self.lit:
            return {"model": "qwen36-a3b", "ready": True, "slots": 1}
        return {"model": None, "ready": False, "slots": 2}

    def set_light(self, value):
        self.calls.append(("light", value))

    def serving_light(self):
        return self.lit

    def set_yielding(self, value):
        self.calls.append(("yield", value))


@pytest.mark.parametrize("probe, expected", [(True, True), (False, False), ("yes", False), (1, False), (None, False)])
def test_a_cpu_only_node_tells_its_member_whether_the_owner_is_gaming_and_nothing_else(nodes, probe, expected):
    nodes.profile["is_yielding"] = probe
    node = nodes()
    node.app_member = Recorder()
    hw = node._build_heartbeat_payload()["hardware"]
    assert node.app_member.calls == [("light", expected)], "light mode, never the full step-aside"
    assert hw["pool"] == "app" and set(hw["app"]) == {"model", "ready", "slots"}, "no new heartbeat fields"


def test_a_member_that_serves_lighter_still_says_the_owner_is_gaming(nodes):
    """The heartbeat tells the truth; whether the member is routed to is the coordinator's call from its status."""
    nodes.profile["is_yielding"] = True
    node = nodes()
    node.app_member = Recorder(serving_light=True)
    hw = node._build_heartbeat_payload()["hardware"]
    assert hw["is_yielding"] is True and hw["pool"] == "app" and hw["app"] == {"model": "qwen36-a3b", "ready": True, "slots": 1}
    node.app_member = Recorder(serving_light=False)
    assert node._build_heartbeat_payload()["hardware"]["is_yielding"] is True


def test_the_coordinator_keeps_a_member_that_runs_lighter_in_the_routing(nodes):
    from engine.registry import WorkerRegistry
    nodes.profile["is_yielding"] = True
    node = nodes()
    registry = WorkerRegistry()
    node.app_member = Recorder(serving_light=True)
    registry.update_worker("light", "10.0.0.7", 50053, "t", node._build_heartbeat_payload()["hardware"])
    node.app_member = Recorder(serving_light=False)
    registry.update_worker("away", "10.0.0.8", 50053, "t", node._build_heartbeat_payload()["hardware"])
    assert [r["worker_id"] for r in registry.app_members()] == ["light"]


def test_the_member_says_when_it_serves_lighter(env):
    assert env.member.serving_light() is False
    serve(env)
    assert env.member.serving_light() is False
    env.member.set_light(True)
    assert env.member.serving_light() is True, "at once, while it is only lowered"
    env.member.set_light(False)
    assert env.member.serving_light() is False


def test_the_both_machines_phone_pool_half_still_steps_aside_completely(nodes):
    nodes.profile["is_yielding"] = True
    companion = nodes(companion_of="node-id", companion_ram_gb=16.0)
    companion.app_member = Recorder()
    companion._build_heartbeat_payload()
    assert companion.app_member.calls == [("yield", True)]


def test_a_gpu_node_with_a_coding_member_does_not_light_anything(nodes):
    nodes.profile["gpu"] = {"has_gpu": True, "vram_total_gb": 8.0}
    nodes.profile["is_yielding"] = True
    node = nodes()
    node.coding_member = SimpleNamespace(status=lambda: {"ready": False, "slots": 0}, set_yielding=lambda value: None)
    node.app_member = Recorder()
    node._build_heartbeat_payload()
    assert node.app_member.calls == []


def test_the_coordinator_hosts_own_member_has_no_light_mode_and_its_arguments_are_unchanged():
    assert not hasattr(app_pool.LocalAppServer, "set_light") and not hasattr(app_pool.LocalAppServer, "set_yielding")
    assert app_server_args("/b/ls", "/m/a.gguf", 8099, 4) == [
        "/b/ls", "-m", "/m/a.gguf", "--host", "127.0.0.1", "--port", "8099", "-c", "16384", "-np", "2", "-t", "4", "-tb", "4",
        "-n", str(app_pool.MAX_OUTPUT_TOKENS), "--device", "none", "-ngl", "0", "--jinja", "--no-webui"]


def test_a_member_that_cannot_be_lit_does_not_break_the_heartbeat(nodes):
    nodes.profile["is_yielding"] = True
    node = nodes()
    node.app_member = SimpleNamespace(status=lambda: {"model": None, "ready": False, "slots": 2})
    assert node._build_heartbeat_payload()["hardware"]["pool"] == "app"


def test_slots_of_one_are_accepted_by_the_coordinators_checks():
    from engine import authn
    hw = {"pool": "app", "app": {"model": "qwen36-a3b", "ready": True, "slots": 1}}
    assert authn.validate_hardware_profile(hw) == (True, "ok")
    member = app_pool.Member("w", 16.0, "qwen36-a3b", True, 1, "http://127.0.0.1:1")
    assert member.slots == 1
