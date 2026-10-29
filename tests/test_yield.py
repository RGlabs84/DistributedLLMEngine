"""
Yielding v2 (2.5.0): the worker steps aside for its owner's own load, never for its own. One raw reading every few
seconds on a thread shared by every node in the process; a full-screen app yields at once, other load has to last,
and the owner has to be quiet for a while before the node comes back. DLNE's own processes are this process and
everything it started, whatever they are called; the busiest card decides, not the average; AMD cards are read
from sysfs; the limits are configurable.
"""
import os
from types import SimpleNamespace

import pytest

from engine import hardware


class Proc:
    def __init__(self, pid, name="llama-server", cpu=0.0):
        self.pid, self.info, self._cpu = pid, {"name": name, "create_time": 1.0}, cpu

    def cpu_percent(self, interval=None):
        return self._cpu


@pytest.fixture
def quiet(monkeypatch):
    """A Linux machine with no GPU load of its own and the full-screen check off."""
    monkeypatch.setattr(hardware.platform, "system", lambda: "Linux")
    monkeypatch.setattr(hardware, "_windows_fullscreen_app_running", lambda: False)
    monkeypatch.setattr(hardware, "_external_nvidia_load", lambda pids: None)
    monkeypatch.setattr(hardware, "_linux_gpu_busy_max", lambda: 0.0)
    monkeypatch.setattr(hardware.psutil, "cpu_count", lambda logical=True: 8)
    monkeypatch.setattr(hardware, "get_worker_limits", lambda: {})
    state = SimpleNamespace(cpu=5.0, procs=[])
    monkeypatch.setattr(hardware.psutil, "cpu_percent", lambda interval=None: state.cpu)
    monkeypatch.setattr(hardware, "_collect_llama_processes", lambda: list(state.procs))
    return state


GPU = {"has_gpu": True, "vram_total_gb": 8.0, "vram_free_gb": 1.0, "name": "x", "load_percent": 10.0}


def test_the_worker_never_yields_to_its_own_processes(monkeypatch):
    """This process and everything it started are ours; the name tags are only a fallback for an engine
    somebody else started (a co-hosted coordinator)."""
    me = os.getpid()
    tree = {me: Proc(me, "python"), 501: Proc(501, "nvidia-smi"), 502: Proc(502, "llama-server"),
            777: Proc(777, "llama-server"), 778: Proc(778, "Game.exe")}
    monkeypatch.setattr(hardware.psutil, "Process",
                        lambda pid: SimpleNamespace(pid=pid, children=lambda recursive=True: [tree[501], tree[502]]))
    monkeypatch.setattr(hardware.psutil, "process_iter", lambda attrs=None: list(tree.values()))
    monkeypatch.setattr(hardware, "_tracked_llama_processes", {})
    own = sorted(p.pid for p in hardware._collect_llama_processes())
    assert own == sorted([me, 501, 502, 777]), "ours by descent, plus the engine named like ours; never the game"


def test_our_own_cpu_is_subtracted_and_the_owners_counts(quiet):
    quiet.procs = [Proc(os.getpid(), "python", cpu=160.0)]          # 1.6 cores of our own work on 8 threads = 20%
    quiet.cpu = 75.0
    assert hardware._load_signal(GPU) == (False, ""), "75 - 20 = 55% is the owner's: under the limit"
    quiet.cpu = 85.0
    assert hardware._load_signal(GPU) == (True, "cpu"), "85 - 20 = 65%"


def test_the_busiest_card_decides_not_the_average(quiet):
    quiet.procs = []
    two_cards = {**GPU, "load_percent": 50.0, "load_max_percent": 99.0}
    assert hardware._load_signal(two_cards) == (True, "gpu")
    assert hardware._load_signal({**GPU, "load_percent": 50.0, "load_max_percent": 50.0}) == (False, "")


def test_an_amd_card_is_read_from_sysfs_when_nothing_of_ours_is_on_it(quiet, monkeypatch):
    quiet.procs = []
    monkeypatch.setattr(hardware, "_linux_gpu_busy_max", lambda: 85.0)
    assert hardware._load_signal({**GPU, "load_percent": 0.0}) == (True, "gpu")
    quiet.procs = [Proc(9, "llama-server")]
    assert hardware._load_signal({**GPU, "load_percent": 0.0}) == (False, ""), "serving: the total is ours too"


def test_the_sysfs_busy_figure_is_the_busiest_card(monkeypatch):
    monkeypatch.setattr(hardware.platform, "system", lambda: "Linux")
    monkeypatch.setattr(hardware, "_listdir", lambda path: ["card0", "card0-DP-1", "card1", "renderD128"])
    values = {"/sys/class/drm/card0/device/gpu_busy_percent": "12", "/sys/class/drm/card1/device/gpu_busy_percent": "77"}
    monkeypatch.setattr(hardware, "_read_sysfs", lambda path: values.get(path, ""))
    assert hardware._linux_gpu_busy_max() == 77.0
    monkeypatch.setattr(hardware.platform, "system", lambda: "Windows")
    assert hardware._linux_gpu_busy_max() == 0.0


def test_the_limits_come_from_the_config(quiet, monkeypatch):
    quiet.procs = []
    quiet.cpu = 70.0
    assert hardware._load_signal(GPU) == (True, "cpu")
    monkeypatch.setattr(hardware, "get_worker_limits", lambda: {"yield_cpu_percent": 90.0, "yield_gpu_percent": 95.0})
    assert hardware._load_signal(GPU) == (False, "")
    assert hardware._load_signal({**GPU, "load_percent": 96.0}) == (True, "gpu")


def test_the_verdict_is_debounced_both_ways(monkeypatch):
    readings = []
    monkeypatch.setattr(hardware, "_load_signal", lambda gpu_info=None: readings.pop(0))
    detector = hardware._YieldDetector()
    readings[:] = [(True, "cpu")]
    assert detector.sample() is False, "one reading is a spike"
    readings[:] = [(True, "cpu")]
    assert detector.sample() is True, "two in a row is a load"
    assert detector.why == "cpu"
    readings[:] = [(False, "")] * (hardware._YIELD_OFF_SAMPLES - 1)
    for _ in range(hardware._YIELD_OFF_SAMPLES - 1):
        assert detector.sample() is True, "not back yet"
    readings[:] = [(False, "")]
    assert detector.sample() is False and detector.why == ""
    readings[:] = [(True, "fullscreen")]
    assert detector.sample() is True, "a full-screen app yields at once"
    readings[:] = [(True, "gpu"), (False, "")]
    detector.sample()
    assert detector.sample() is True, "a quiet reading in the middle does not end it"


def test_a_failed_reading_says_nothing(monkeypatch):
    def boom(gpu_info=None):
        raise RuntimeError("no psutil")
    monkeypatch.setattr(hardware, "_load_signal", boom)
    detector = hardware._YieldDetector()
    assert detector.sample() is False


def test_the_profile_uses_the_debounced_verdict(monkeypatch):
    monkeypatch.setattr(hardware, "get_gpu_info", lambda: dict(GPU))
    monkeypatch.setattr(hardware, "get_gpu_type", lambda: "nvidia")
    monkeypatch.setattr(hardware, "get_cpu_features", lambda: [])
    monkeypatch.setattr(hardware, "get_dock_state", lambda: {"link": "unknown", "power": "unknown"})
    started = []
    monkeypatch.setattr(hardware._yield_detector, "start", lambda: started.append(1))
    monkeypatch.setattr(hardware._yield_detector, "yielding", True)
    monkeypatch.setattr(hardware._yield_detector, "gpu_yielding", True)
    monkeypatch.setattr(hardware._yield_detector, "why", "gpu")
    assert hardware.get_hardware_profile()["is_yielding"] is True
    assert started == [1] and hardware.yield_reason() == "gpu"
    monkeypatch.setattr(hardware._yield_detector, "yielding", False)
    monkeypatch.setattr(hardware._yield_detector, "gpu_yielding", False)
    assert hardware.get_hardware_profile()["is_yielding"] is False and hardware.yield_reason() == ""


# 2.6.2: a GPU node does not step aside for CPU load alone when it can read its GPU's own load. On 2026-10-28 the
# owner's laptop, whose 4070 only drove its display, left the shared pool every few minutes while builds and test runs
# used its CPU, and auto-swap's ten stable minutes never came.

@pytest.mark.parametrize("seen, gpu_verdict", [(True, False), (False, True)])
def test_cpu_load_trips_a_gpu_node_only_when_its_gpu_cannot_be_read(monkeypatch, seen, gpu_verdict):
    readings = [hardware._Reading(True, "cpu", seen), hardware._Reading(True, "cpu", seen)]
    monkeypatch.setattr(hardware, "_load_signal", lambda gpu_info=None: readings.pop(0))
    detector = hardware._YieldDetector()
    detector.sample()
    assert detector.sample() is True and detector.why == "cpu", "the machine's verdict: a CPU contributor yields"
    assert detector.gpu_yielding is gpu_verdict


def test_gpu_load_and_full_screen_still_trip_a_gpu_node(monkeypatch):
    readings = [hardware._Reading(True, "gpu", True), hardware._Reading(True, "gpu", True)]
    monkeypatch.setattr(hardware, "_load_signal", lambda gpu_info=None: readings.pop(0))
    detector = hardware._YieldDetector()
    detector.sample()
    detector.sample()
    assert detector.gpu_yielding is True
    readings[:] = [hardware._Reading(True, "fullscreen")]
    fresh = hardware._YieldDetector()
    fresh.sample()
    assert fresh.gpu_yielding is True, "a full-screen app at once"


def test_a_reading_says_whether_the_gpu_itself_was_seen(quiet, monkeypatch):
    quiet.procs = [Proc(9, "ggml-rpc-server")]                       # serving a shard
    quiet.cpu = 95.0
    monkeypatch.setattr(hardware, "_external_nvidia_load", lambda pids: 19.0)   # the compositor, per NVIDIA's figures
    reading = hardware._load_signal(GPU)
    assert reading == (True, "cpu") and reading.gpu_seen is True
    monkeypatch.setattr(hardware, "_external_nvidia_load", lambda pids: None)   # the driver gives no per-process figures
    assert hardware._load_signal(GPU).gpu_seen is False
    monkeypatch.setattr(hardware, "_external_nvidia_load", lambda pids: 75.0)   # the owner's game on the GPU
    assert hardware._load_signal(GPU) == (True, "gpu")


def test_the_cpu_half_of_a_both_machine_reports_the_machines_verdict(monkeypatch):
    from engine import worker
    monkeypatch.setattr(hardware._yield_detector, "start", lambda: None)
    monkeypatch.setattr(hardware._yield_detector, "yielding", True)        # the owner's build is using the CPU
    monkeypatch.setattr(hardware._yield_detector, "gpu_yielding", False)   # the GPU is quiet
    monkeypatch.setattr(worker, "get_hardware_profile",
                        lambda: {"gpu": dict(GPU), "is_yielding": hardware.is_yielding_now(dict(GPU))})
    monkeypatch.setattr(hardware, "get_memory_bandwidth_gbs", lambda: None)
    gpu_node = SimpleNamespace(companion_of="", companion_ram_gb=0.0)
    cpu_half = SimpleNamespace(companion_of="w1", companion_ram_gb=8.0)
    assert worker.WorkerNode._probe_hardware(gpu_node)["is_yielding"] is False, "the shard stays"
    assert worker.WorkerNode._probe_hardware(cpu_half)["is_yielding"] is True, "the phone model steps aside"
    assert hardware.is_yielding_now(None) is True, "no GPU said: the machine's verdict"
