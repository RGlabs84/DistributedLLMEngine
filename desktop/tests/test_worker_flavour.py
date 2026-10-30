"""
2.7.0: the engine flavour reaches the real worker process (DLNE_ENGINE_FLAVOR), what the worker says about CUDA reaches the
status, and a changed flavour restarts the worker. Driven like test_worker.py: a signed fake release whose dlne_server.py plays
the worker.
"""

import time
from dataclasses import replace

from test_worker import rig_factory  # noqa: F401  (a fixture)
from dlne_donate import worker
from dlne_donate.tiers import Settings

# The fake worker answers with the flavour it was started with, as the real one will (FAKE_CUDA_FAILS: CUDA cannot start).
FLAVOUR_WORKER = '''
import json, os, time
flavour = os.environ.get("DLNE_ENGINE_FLAVOR", "?")
capable = not os.environ.get("FAKE_NO_CUDA")
print('DLNE_CUDA: ' + json.dumps({"capable": capable, "reason": "" if capable else "no NVIDIA GPU", "gpu": "RTX 4070", "compute_cap": "8.9", "driver": "615.78"}), flush=True)
fallback = "the CUDA engine would not start" if os.environ.get("FAKE_CUDA_FAILS") and flavour == "cuda" else ""
print('DLNE_ENGINE: ' + json.dumps({"flavour": "vulkan" if fallback else flavour, "fallback": fallback}), flush=True)
print('DLNE_ROLE: coding', flush=True)
print('[worker] Heartbeat OK', flush=True)
time.sleep(300)
'''


def engine_is(flavour):
    return lambda s: s.state == worker.DONATING and s.engine is not None and s.engine.flavour == flavour


def started_on(rig):
    return [line.split("(")[1].split(" ")[0] for line in rig.lines if line.startswith("Starting the worker (")]


def lend(**kwargs):
    return Settings(gpu_enabled=True, gpu_vram_gb=8, gpu_ram_gb=16, threads=6, **kwargs)


def test_the_worker_starts_on_vulkan_until_the_owner_agrees_then_restarts_on_cuda(rig_factory):
    rig = rig_factory(FLAVOUR_WORKER)
    rig.controller.start()
    status = rig.wait(engine_is("vulkan"), what="vulkan")
    assert status.cuda is not None and status.cuda.capable and status.cuda.gpu == "RTX 4070"
    assert rig.controller.wanted_flavour() == "vulkan"
    first = rig.controller._process.pid

    rig.settings = replace(rig.settings, cuda_consent="yes")               # the owner said Use CUDA
    rig.controller.apply_settings()
    status = rig.wait(engine_is("cuda"), what="cuda after the restart")
    assert rig.controller._process.pid != first, "the worker was restarted"
    assert status.version_label == "9.9.9+cuda"
    assert started_on(rig) == ["vulkan", "cuda"]

    rig.settings = replace(rig.settings, engine="vulkan")                  # back to Vulkan by choice
    rig.controller.apply_settings()
    rig.wait(engine_is("vulkan"), what="vulkan again")
    assert started_on(rig) == ["vulkan", "cuda", "vulkan"]


def test_a_setting_that_changes_nothing_does_not_restart_the_worker(rig_factory):
    rig = rig_factory(FLAVOUR_WORKER)
    rig.controller.start()
    rig.wait(engine_is("vulkan"), what="vulkan")
    pid = rig.controller._process.pid
    rig.settings = replace(rig.settings, engine="vulkan", threads=5)
    rig.controller.apply_settings()
    rig.settings = replace(rig.settings, cuda_consent="never")
    rig.controller.apply_settings()
    time.sleep(1.0)
    assert rig.controller._process.pid == pid and rig.controller.status().state == worker.DONATING


def test_a_computer_that_cannot_run_cuda_stays_on_vulkan_whatever_the_setting(rig_factory, monkeypatch):
    monkeypatch.setenv("FAKE_NO_CUDA", "1")
    rig = rig_factory(FLAVOUR_WORKER, settings=lend(engine="cuda", cuda_consent="yes"))
    rig.controller.start()
    rig.wait(lambda s: s.cuda is not None and s.state == worker.DONATING, what="the worker's word")
    assert rig.controller.status().cuda.reason == "no NVIDIA GPU"
    assert rig.controller.wanted_flavour() == "vulkan"


def test_a_failed_cuda_start_falls_back_once_and_is_not_tried_again_until_the_setting_or_version_changes(rig_factory, monkeypatch):
    monkeypatch.setenv("FAKE_CUDA_FAILS", "1")
    rig = rig_factory(FLAVOUR_WORKER, settings=lend(engine="cuda", cuda_consent="yes"))
    rig.controller.start()
    status = rig.wait(lambda s: engine_is("vulkan")(s) and s.engine.fallback != "", what="the fall-back")
    assert status.engine.fallback == "the CUDA engine would not start"
    pid = rig.controller._process.pid
    time.sleep(1.0)
    assert rig.controller._process.pid == pid, "no restart loop"
    assert started_on(rig) == ["vulkan", "cuda"], "Vulkan until the worker said it can; CUDA once; then the worker itself fell back"
    assert rig.controller.wanted_flavour() == "vulkan"
    rig.controller.apply_settings()
    assert rig.controller._process.pid == pid

    rig.settings = replace(rig.settings, engine="auto")                    # the setting changed: CUDA may be tried again
    assert rig.controller.wanted_flavour() == "cuda"
    rig.settings = replace(rig.settings, engine="cuda")
    assert rig.controller.wanted_flavour() == "vulkan", "back to the setting that failed: still failed"
    with rig.controller._lock:                                             # a new engine version
        rig.controller._status = replace(rig.controller._status, engine_version="9.9.10")
    assert rig.controller.wanted_flavour() == "cuda"


def test_stop_during_a_flavour_switch_stops_for_good(rig_factory):
    rig = rig_factory(FLAVOUR_WORKER)
    rig.controller.start()
    rig.wait(engine_is("vulkan"), what="vulkan")
    rig.settings = replace(rig.settings, cuda_consent="yes")
    rig.controller.apply_settings()
    rig.controller.stop(wait=True, timeout=30)
    assert rig.controller.status().state == worker.IDLE
    time.sleep(0.5)
    assert rig.controller.status().state == worker.IDLE and rig.controller._process is None


def test_a_computer_that_lends_only_its_cpu_runs_no_gpu_engine(rig_factory):
    rig = rig_factory(FLAVOUR_WORKER, settings=Settings(gpu_enabled=False, cpu_enabled=True, cpu_ram_gb=8, threads=4, engine="cuda", cuda_consent="yes"))
    rig.controller.start()
    rig.wait(lambda s: s.cuda is not None and s.state == worker.DONATING, what="the worker's word")
    assert rig.controller.wanted_flavour() == "vulkan" and started_on(rig) == ["vulkan"]
