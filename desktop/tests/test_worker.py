"""
The worker controller, driven with real child processes: a signed fake release whose dlne_server.py plays the worker.
It runs through the same path as the real one (verify, unpack, config.txt, ``-m dlne_donate --run-worker``, markers).
"""

import json
import os
import sys
import time

import pytest

from conftest import Release, release, small_source_zip
from dlne_donate import configfile, installer, markers, tiers, worker
from dlne_donate.tiers import Hardware, Settings

posix_only = pytest.mark.skipif(os.name != "posix", reason="process-tree checks use POSIX signals")

CONFIG_STUB = b'''
import os
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def get_base_dir(): return _root
CONFIG_DIR = os.path.join(_root, "config")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.txt")
_CONFIG_TMP_FILE = CONFIG_FILE + ".tmp"
'''

HW = Hardware(gpu_name="RTX", vram_gb=8, gpu_count=1, total_ram_gb=32, physical_cores=8, logical_threads=16)


def server(body: str) -> dict:
    return {"dlne_server.py": body.encode(), "engine/config.py": CONFIG_STUB}


class Rig:
    def __init__(self, tmp_path, home, extra, settings=None, **release_kwargs):
        folder = tmp_path / "rel"
        folder.mkdir(exist_ok=True)
        self.served = Release(folder, source=small_source_zip(extra=extra), **release_kwargs)
        self.http = self.served.http()
        self.settings = settings or Settings(gpu_enabled=True, gpu_vram_gb=8, gpu_ram_gb=16, threads=6)
        self.changes = []
        self.lines = []
        self.home = str(home)
        self.controller = worker.WorkerController(
            self.home, lambda: self.settings, lambda: HW, base_url="https://example.test", on_change=self.changes.append,
            installer_factory=self.make_installer, log=self.lines.append, vulkan_indices=lambda: "")

    def make_installer(self, root, url, **kwargs):
        return installer.EngineInstaller(root, url, http=self.http, public_key=self.served.public_key, platform="linux-x64", **kwargs)

    def wait(self, predicate, seconds=20.0, what=""):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if predicate(self.controller.status()):
                return self.controller.status()
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for {what or 'state'}; status is {self.controller.status()}")

    def stop(self):
        self.controller.stop(wait=True, timeout=30)
        worker.kill_programs_under(self.controller.engine_dir)


@pytest.fixture
def rig_factory(tmp_path, app_home):
    rigs = []

    def make(body, **kwargs):
        rig = Rig(tmp_path, app_home, server(body), **kwargs)
        rigs.append(rig)
        return rig
    yield make
    for rig in rigs:
        rig.stop()


DONATING = '''
import json, subprocess, sys, time
print('DLNE_ROLE: coding ' + json.dumps({"model": "Fake-Model.gguf", "tier": "t1", "slots": 2}), flush=True)
print('[worker] Heartbeat OK', flush=True)
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
print("GRANDCHILD %d" % child.pid, flush=True)
time.sleep(300)
'''


def test_donating_end_to_end_then_stop_leaves_nothing_behind(rig_factory):
    import psutil

    rig = rig_factory(DONATING)
    pids = []
    controller = rig.controller
    assert controller.start()
    status = rig.wait(lambda s: s.state == worker.DONATING and s.role is not None, what="a role")
    assert status.role.role == "coding" and status.role.slots == 2
    assert "Coding member" in status.tooltip and "Fake-Model" in status.tooltip
    assert status.engine_version == "9.9.9"

    # config.txt was written from the sliders before the worker started
    cfg = configfile.read_config(os.path.join(controller.engine_dir, "config", "config.txt"))
    assert cfg["MAX_VRAM_GB"] == "8" and cfg["CODING_MAX_RAM_GB"] == "16" and cfg["MAX_CPU_THREADS"] == "6"
    assert cfg["COORDINATOR_URL"] == "https://example.test"

    process = controller._process
    assert process is not None
    family = psutil.Process(process.pid).children(recursive=True)
    deadline = time.monotonic() + 10
    while not family and time.monotonic() < deadline:
        time.sleep(0.1)
        family = psutil.Process(process.pid).children(recursive=True)
    assert family, "the fake worker should have started a grandchild"
    pids = [process.pid] + [c.pid for c in family]

    started = time.monotonic()
    controller.stop(wait=True, timeout=30)
    assert time.monotonic() - started < 15
    assert controller.status().state == worker.IDLE
    time.sleep(0.2)
    assert [pid for pid in pids if psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE] == []


def test_a_slider_change_rewrites_config_txt_of_the_running_worker(rig_factory):
    rig = rig_factory(DONATING)
    rig.controller.start()
    rig.wait(lambda s: s.state == worker.DONATING)
    path = os.path.join(rig.controller.engine_dir, "config", "config.txt")
    assert configfile.read_config(path)["MAX_VRAM_GB"] == "8"
    rig.settings = Settings(gpu_enabled=True, gpu_vram_gb=6, gpu_ram_gb=20, threads=10)
    rig.controller.apply_settings()
    cfg = configfile.read_config(path)
    assert cfg["MAX_VRAM_GB"] == "6" and cfg["CODING_MAX_RAM_GB"] == "20" and cfg["MAX_CPU_THREADS"] == "10" and cfg["ALSO_CPU_RAM_GB"] == "0"
    assert rig.controller.status().state == worker.DONATING        # no restart


def test_switching_a_device_while_donating_waits_for_the_next_start(rig_factory):
    rig = rig_factory(DONATING)
    rig.controller.start()
    rig.wait(lambda s: s.state == worker.DONATING)
    path = os.path.join(rig.controller.engine_dir, "config", "config.txt")
    assert not rig.controller.devices_changed()
    rig.settings = Settings(gpu_enabled=False, cpu_enabled=True, cpu_ram_gb=16, gpu_vram_gb=8, gpu_ram_gb=12, threads=9)
    rig.controller.apply_settings()
    cfg = configfile.read_config(path)
    assert cfg["ALLOWED_GPU_INDICES"] == "-1" and cfg["MAX_VRAM_GB"] == "8"        # still lending the GPU, as started
    assert cfg["CODING_MAX_RAM_GB"] == "12" and cfg["MAX_CPU_THREADS"] == "9"      # the amounts follow the sliders
    assert rig.controller.devices_changed()
    rig.controller.stop(wait=True, timeout=30)
    rig.controller.start()                                                         # the next start uses the new choice
    rig.wait(lambda s: s.state == worker.DONATING)
    cfg = configfile.read_config(path)
    assert cfg["ALLOWED_GPU_INDICES"] == "none" and cfg["MAX_RAM_GB"] == "16"


def test_the_worker_runs_from_the_verified_folder_and_is_told_where_its_config_lives(rig_factory):
    rig = rig_factory('''
import os, sys
from engine import config
print("BASE " + config.get_base_dir(), flush=True)
print("ARGV " + " ".join(sys.argv[1:]), flush=True)
print("CWD " + os.getcwd(), flush=True)
print("DLNE_ROLE: app", flush=True)
import time; time.sleep(30)
''')
    rig.controller.start()
    rig.wait(lambda s: s.role is not None)
    engine_dir = rig.controller.engine_dir
    said = "\n".join(rig.lines)
    assert f"worker: BASE {engine_dir}" in said                    # engine.config points at the verified folder, not at the app
    assert "worker: ARGV worker --coordinator https://example.test" in said
    assert f"worker: CWD {engine_dir}" in said


def test_nothing_unverified_runs(rig_factory):
    """A release that does not verify: no worker is started, whatever else is on disk, and the owner is told."""
    rig = rig_factory(DONATING, sign=False)
    spawned = []
    rig.controller._spawn = lambda *a, **k: spawned.append(a) or (_ for _ in ()).throw(AssertionError("spawned an unverified engine"))
    rig.controller.start()
    status = rig.wait(lambda s: s.state == worker.ERROR, what="the verification error")
    assert spawned == []
    assert status.verification_failed and "Nothing was run" in status.error
    assert not os.path.exists(os.path.join(rig.controller.engine_dir, "dlne_server.py"))


def test_a_tampered_engine_on_disk_is_never_run(rig_factory):
    rig = rig_factory(DONATING)
    rig.controller.start()
    rig.wait(lambda s: s.state == worker.DONATING)
    rig.controller.stop(wait=True, timeout=30)
    script = os.path.join(rig.controller.engine_dir, "dlne_server.py")
    with open(script, "a", encoding="utf-8") as handle:
        handle.write("\nprint('INJECTED')\n")
    rig.controller.start()
    rig.wait(lambda s: s.state == worker.DONATING)
    assert "INJECTED" not in open(script, encoding="utf-8").read()    # it was put back from the signed archive before it ran


def test_worker_command_refuses_anything_but_a_verified_engine():
    fake = installer.VerifiedEngine(root="/tmp/x", version="1.0.0", manifest={}, rpc_platform=None)
    with pytest.raises(installer.VerificationError):
        worker.worker_command(fake, "https://example.test")
    with pytest.raises(installer.VerificationError):
        worker.worker_command(object(), "https://example.test")     # type: ignore[arg-type]


def test_version_mismatch_restarts_with_a_fresh_engine_and_gives_up_after_two(rig_factory):
    rig = rig_factory('''
import os, sys
counter = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs.txt")
with open(counter, "a") as handle: handle.write("x")
sys.exit(75)
''')
    rig.controller.start()
    status = rig.wait(lambda s: s.state == worker.ERROR, what="giving up")
    runs = open(os.path.join(rig.controller.engine_dir, "runs.txt")).read()
    assert len(runs) == 3                                  # the first run and two automatic restarts
    assert "still does not match" in status.error


def test_a_refusal_is_reported_with_the_coordinators_reason(rig_factory):
    rig = rig_factory('''
import sys
print("DLNE_REFUSAL: this CPU has 2 physical cores; the swarm needs 4", flush=True)
sys.exit(78)
''')
    rig.controller.start()
    status = rig.wait(lambda s: s.state == worker.ERROR)
    assert "this CPU has 2 physical cores; the swarm needs 4" in status.error and "can't use this computer" in status.error


def test_a_missing_visual_cpp_runtime_is_named_with_the_installer_link(rig_factory):
    rig = rig_factory('''
import sys
print("DLNE_NEEDS: vcredist", flush=True)
sys.exit(1)
''')
    rig.controller.start()
    status = rig.wait(lambda s: s.state == worker.ERROR)
    assert "Visual C++" in status.error and "https://aka.ms/vs/17/release/vc_redist.x64.exe" in status.error
    assert "AVX" not in status.error and "code 1" not in status.error


def test_a_crash_is_an_error_not_a_loop(rig_factory):
    rig = rig_factory("import sys; sys.exit(3)\n")
    rig.controller.start()
    status = rig.wait(lambda s: s.state == worker.ERROR)
    assert "code 3" in status.error
    assert not rig.controller.status().running


def test_clock_notice_and_also_cpu_are_shown(rig_factory):
    rig = rig_factory('''
import time
print('DLNE_CLOCK: {"offset_seconds": 7200}', flush=True)
print("DLNE_ROLE: coding", flush=True)
print("DLNE_ALSO: app", flush=True)
print('[worker] Heartbeat OK', flush=True)          # the swarm took the machine: only now is it "donating"
time.sleep(60)
''')
    rig.controller.start()
    status = rig.wait(lambda s: s.state == worker.DONATING and s.also_cpu and s.clock_notice != "")
    assert "2.0 hours behind" in status.clock_notice and "phone app" in status.tooltip


def test_stop_while_the_engine_is_still_downloading(rig_factory):
    rig = rig_factory(DONATING)

    class Slow:
        def __init__(self, inner):
            self.inner = inner
            self.requests = []

        def get_json(self, url, timeout=15.0):
            return self.inner.get_json(url)

        def download(self, url, destination, expected_size, progress=None, timeout=60.0):
            for step in range(100):
                time.sleep(0.05)
                if progress:
                    progress(step, 100)
            self.inner.download(url, destination, expected_size)

    rig.http = Slow(rig.http)
    rig.controller.start()
    rig.wait(lambda s: s.state == worker.PREPARING and s.progress is not None)
    rig.controller.stop(wait=True, timeout=10)
    assert rig.controller.status().state == worker.IDLE
    assert rig.controller._process is None


def test_start_twice_does_not_start_two(rig_factory):
    rig = rig_factory(DONATING)
    assert rig.controller.start()
    assert not rig.controller.start()


def test_discrete_vulkan_indices_only_on_a_hybrid_machine():
    hybrid = "GPU0:\n\tdeviceType = PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU\nGPU1:\n\tdeviceType = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU\n"
    assert worker.discrete_vulkan_indices(hybrid) == "1"
    assert worker.discrete_vulkan_indices("GPU0:\n\tdeviceType = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU\n") == ""


def test_tooltips_say_what_it_is_doing():
    assert worker.Status().tooltip == "DLNE Donate: idle"
    donating = worker.Status(state=worker.DONATING, role=markers.Role("coding", model="Qwen3.6.gguf"))
    assert "Coding member" in donating.tooltip and "Qwen3.6" in donating.tooltip
    assert "problem" in worker.Status(state=worker.ERROR, error="boom").tooltip
