"""Single instance, autostart, the settings file, the icon states, and the app's state file."""

import json
import os
import subprocess
import sys

import pytest

from conftest import DESKTOP
from dlne_donate import autostart, instance, paths, settings as settings_store, tiers, trayicon, worker
from dlne_donate.tiers import Hardware, Settings

HW = Hardware(gpu_name="RTX", vram_gb=8, gpu_count=1, total_ram_gb=32, physical_cores=8, logical_threads=16)
APP = tiers.app_tiers_from_status(None)


# ---- single instance --------------------------------------------------------------------------------

def test_only_one_instance_holds_the_lock(app_home):
    first, second = instance.InstanceLock(str(app_home)), instance.InstanceLock(str(app_home))
    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


def test_the_lock_is_held_across_processes_and_dies_with_its_holder(app_home):
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sys, time; sys.path.insert(0, %r)\n"
         "from dlne_donate import instance\n"
         "l = instance.InstanceLock(%r)\nassert l.acquire()\nprint('held', flush=True)\ntime.sleep(60)\n" % (str(DESKTOP), str(app_home))],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        assert not instance.InstanceLock(str(app_home)).acquire()
    finally:
        holder.kill()
        holder.wait()
    third = instance.InstanceLock(str(app_home))
    assert third.acquire()                       # the operating system dropped the lock when the process died
    third.release()


def test_a_second_launch_asks_the_first_to_show_its_settings(app_home):
    assert not instance.take_show_request(str(app_home))
    instance.ask_running_instance_to_show(str(app_home))
    assert instance.take_show_request(str(app_home))
    assert not instance.take_show_request(str(app_home))


# ---- autostart --------------------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="the Linux autostart entry")
def test_linux_autostart_entry_is_written_and_removed():
    assert not autostart.is_enabled()
    result = autostart.set_enabled(True, ["/opt/dlne donate/DLNE-Donate", "--autostart"])
    assert result["ok"] and autostart.is_enabled()
    text = open(autostart.linux_autostart_path(), encoding="utf-8").read()
    assert "Exec='/opt/dlne donate/DLNE-Donate' --autostart" in text and "Terminal=false" in text and "Name=DLNE Donate" in text
    assert autostart.set_enabled(False)["ok"] and not autostart.is_enabled()


def test_windows_run_command_quotes_a_path_with_spaces():
    assert autostart._windows_command([r"C:\Users\Jane Doe\AppData\Local\DLNE Donate\DLNE-Donate.exe", "--autostart"]) == \
        r'"C:\Users\Jane Doe\AppData\Local\DLNE Donate\DLNE-Donate.exe" --autostart'


def test_launch_command_inside_an_appimage_is_the_appimage(tmp_path, monkeypatch):
    image = tmp_path / "DLNE-Donate.AppImage"
    image.write_text("x")
    monkeypatch.setenv("APPIMAGE", str(image))
    assert autostart.launch_command(["--autostart"]) == [str(image), "--autostart"]


# ---- settings file ----------------------------------------------------------------------------------

def test_settings_are_saved_and_loaded(app_home):
    path = paths.settings_path()
    s = Settings(gpu_enabled=True, gpu_vram_gb=6, gpu_ram_gb=18, cpu_enabled=True, cpu_ram_gb=8, threads=5, resume_donating=True)
    settings_store.save(s, path)
    assert json.loads(open(path, encoding="utf-8").read())["gpu"]["ram_gb"] == 18
    assert settings_store.load(HW, APP, path) == s
    assert [n for n in os.listdir(os.path.dirname(path)) if n.endswith(".tmp")] == []


def test_a_broken_settings_file_gives_the_defaults(app_home):
    path = paths.settings_path()
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("{ this is not json")
    assert settings_store.load(HW, APP, path) == tiers.default_settings(HW, APP)


def test_settings_that_do_not_fit_this_machine_are_clamped_on_load(app_home):
    path = paths.settings_path()
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"gpu": {"enabled": True, "vram_gb": 80, "ram_gb": 900}, "threads": 4000}, handle)
    loaded = settings_store.load(HW, APP, path)
    assert loaded.gpu_vram_gb == 8 and loaded.gpu_ram_gb == 28 and loaded.threads == 16


# ---- the tray icon ----------------------------------------------------------------------------------

def test_every_state_has_its_own_badge_colour():
    pytest.importorskip("PIL")
    seen = {}
    for state in (worker.IDLE, worker.DONATING, worker.PREPARING, worker.ERROR):
        image = trayicon.make_icon(state, 64)
        assert image.size == (64, 64)
        seen[state] = image.getpixel((54, 54))[:3]
    assert seen[worker.DONATING] == (46, 158, 79) and seen[worker.ERROR] == (209, 52, 56)
    assert len(set(seen.values())) == 4


# ---- the model ties it together ---------------------------------------------------------------------

def test_the_model_saves_changes_applies_them_and_writes_a_state_file(app_home):
    from dlne_donate.model import Model

    applied = []

    class FakeController:
        def __init__(self, home, get_settings, get_hardware, **kwargs):
            self.on_change = kwargs["on_change"]
            self.s = worker.Status()

        def apply_settings(self):
            applied.append(1)

        def status(self):
            return self.s

        def start(self):
            self.s = worker.Status(state=worker.DONATING, role=None, message="Donating.")
            self.on_change(self.s)
            return True

        def stop(self, wait=False):
            self.s = worker.Status()
            self.on_change(self.s)

    model = Model(str(app_home), hardware=HW, controller_factory=FakeController, fetch_info=False)
    model.update(gpu_vram_gb=6.0, threads=99)
    assert model.settings.gpu_vram_gb == 6.0 and model.settings.threads == 16        # clamped
    assert applied
    saved = json.loads(open(paths.settings_path(str(app_home)), encoding="utf-8").read())
    assert saved["gpu"]["vram_gb"] == 6.0
    assert model.donate()
    state = json.loads(open(paths.state_path(str(app_home)), encoding="utf-8").read())
    assert state["state"] == "donating"
    model.stop()
    assert json.loads(open(paths.state_path(str(app_home)), encoding="utf-8").read())["state"] == "idle"


def test_donate_is_refused_when_nothing_can_be_lent(app_home):
    from dlne_donate.model import Model

    started = []

    class FakeController:
        def __init__(self, *a, **k): pass
        def status(self): return worker.Status()
        def start(self): started.append(1) or True
        def apply_settings(self): pass

    weak = Hardware(total_ram_gb=8, physical_cores=2, logical_threads=2)
    model = Model(str(app_home), hardware=weak, controller_factory=FakeController, fetch_info=False)
    assert not model.donate() and started == []
