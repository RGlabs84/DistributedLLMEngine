"""
Regression tests for what a real Windows 11 test of the first build found (windowed process with no stdout/stderr,
a window taller than the screen, wrong text on a computer that cannot donate, "Donating" before the swarm said yes,
a stale tray label, a cut tooltip, a first launch that showed nothing).

The window tests need Tk and a display; without one they are skipped (run them under a private Xvfb).
"""

import os
import subprocess
import sys
import time

import pytest

from conftest import DESKTOP, REPO
from dlne_donate import markers, tiers, trayicon, worker
from dlne_donate.tiers import Hardware, Settings

from test_tiers import APP, machine
from test_worker import DONATING, HW, Rig, rig_factory, server  # noqa: F401 - rig_factory is a fixture


# ---- 1. no stdout / stderr -------------------------------------------------------------------------------------

def run_without_streams(code: str) -> subprocess.CompletedProcess:
    script = "import sys\nsys.stdout = None\nsys.stderr = None\n" + code
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(DESKTOP), str(REPO)]))
    return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, timeout=60)


def test_flushing_at_exit_survives_a_process_with_no_standard_streams():
    done = run_without_streams("from dlne_donate.__main__ import flush_standard_streams\nflush_standard_streams()\nprint('ok')\n")
    assert done.returncode == 0, done.stderr
    assert done.stdout == ""            # nothing could be printed, and nothing raised


def test_flushing_ignores_a_broken_stream(monkeypatch):
    from dlne_donate.__main__ import flush_standard_streams

    class Broken:
        def flush(self):
            raise ValueError("I/O operation on closed file")
    monkeypatch.setattr(sys, "stdout", Broken())
    monkeypatch.setattr(sys, "stderr", None)
    flush_standard_streams()


def test_a_second_launch_exits_cleanly_with_no_standard_streams(tmp_path):
    """The second launch prints "already running" and returns: it must not touch None streams (the crash dialog of the test)."""
    home = tmp_path / "second"
    home.mkdir()
    code = (
        "from dlne_donate import instance\n"
        f"lock = instance.InstanceLock({str(home)!r})\n"
        "assert lock.acquire()\n"
        "from dlne_donate.app import App\n"
        f"code = App(home={str(home)!r}).run()\n"
        "from dlne_donate.__main__ import flush_standard_streams\n"
        "flush_standard_streams()\n"
        "raise SystemExit(code)\n")
    done = run_without_streams(code)
    assert done.returncode == 0, done.stderr


def test_the_app_does_not_use_the_standard_streams_unguarded():
    """No ``sys.stdout.`` / ``sys.stderr.`` attribute use anywhere in the app (print() is safe: it does nothing without a stream)."""
    package = os.path.join(str(DESKTOP), "dlne_donate")
    offenders = []
    for name in sorted(os.listdir(package)):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(package, name), encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                code = line.split("#")[0]
                if ("sys.stdout." in code or "sys.stderr." in code) and "for stream in" not in code:
                    offenders.append(f"{name}:{number}: {line.strip()}")
    assert offenders == []


# ---- 3. a computer that cannot donate ---------------------------------------------------------------------------

def test_four_gb_without_a_gpu_says_it_cannot_donate_and_why():
    hw = machine(total_ram_gb=4, physical_cores=4, logical_threads=4)
    desc = tiers.describe(Settings(), hw, [], APP)
    assert not desc.can_donate and not desc.cpu_usable
    assert desc.machine_blocker.startswith("This computer can't donate")
    assert "no GPU" in desc.machine_blocker and "4 GB of RAM" in desc.machine_blocker and "12 GB" in desc.machine_blocker
    assert desc.blocker == desc.machine_blocker                      # the text for the disabled Donate button
    assert "CPU can still help" not in desc.gpu_line                 # the CPU cannot
    assert desc.gpu_line == "No GPU found on this computer."
    assert "Too little RAM" in desc.cpu_line                         # the CPU line is no longer dropped


def test_a_cpu_that_is_too_weak_is_named_too():
    desc = tiers.describe(Settings(), machine(physical_cores=2, avx2=False), [], APP)
    assert desc.machine_blocker and "2 physical cores" in desc.machine_blocker and "AVX2" in desc.machine_blocker


def test_a_machine_that_can_donate_keeps_the_helpful_text():
    desc = tiers.describe(Settings(cpu_enabled=True, cpu_ram_gb=8), machine(total_ram_gb=16), [], APP)
    assert desc.machine_blocker == "" and desc.cpu_usable and desc.can_donate
    assert desc.gpu_line == "No GPU found on this computer. Your CPU can still help."
    gpu = tiers.describe(Settings(), machine(gpu_name="RTX", vram_gb=8, gpu_count=1), [], APP)
    assert gpu.machine_blocker == ""


def test_a_machine_that_can_donate_but_has_nothing_switched_on_is_not_blocked():
    desc = tiers.describe(Settings(), machine(total_ram_gb=16), [], APP)
    assert not desc.can_donate and desc.machine_blocker == "" and desc.blocker == "Turn on the GPU or the CPU to donate."


class FakeStatus:
    def __init__(self, running=False):
        self.running = running


def make_tray(running, blockable, calls):
    state = {"running": running, "ok": not blockable}
    tray = trayicon.Tray(lambda: FakeStatus(state["running"]), lambda: False, {"toggle": lambda label_running: calls.append(label_running)},
                         donate_enabled=lambda: state["ok"])
    return tray, state


def test_the_tray_donate_entry_is_disabled_on_a_computer_that_cannot_donate():
    tray, state = make_tray(False, True, [])
    assert tray._toggle_label() == "Donate" and tray._toggle_enabled() is False
    state["running"] = True                           # "Stop donating" is always available
    assert tray._toggle_label() == "Stop donating" and tray._toggle_enabled() is True
    ok, _ = make_tray(False, False, [])
    assert ok._toggle_enabled() is True


# ---- 5. a click does what its label said --------------------------------------------------------------------------

def test_the_tray_click_reports_the_label_the_owner_saw():
    calls = []
    tray, state = make_tray(True, False, calls)
    tray._toggle_label()                              # the menu was drawn reading "Stop donating"
    state["running"] = False                          # ... and donating stopped by itself before the click
    tray._toggle_clicked()
    tray._toggle_label()                              # redrawn: "Donate"
    tray._toggle_clicked()
    assert calls == [True, False]


class FakeModel:
    def __init__(self, running):
        self.running = running
        self.stopped = 0
        self.donated = 0

    def status(self):
        return worker.Status(state=worker.DONATING if self.running else worker.IDLE)

    def describe(self):
        return tiers.Description(can_donate=True)

    def stop(self):
        self.stopped += 1
        self.running = False

    def donate(self):
        self.donated += 1


class FakeWindow:
    def __init__(self):
        self.shown = 0

    def _confirm_lending(self):
        return True

    def show(self, near_pointer=False):
        self.shown += 1


def make_app(running, tmp_path):
    from dlne_donate.app import App

    app = App(home=str(tmp_path))
    app.model, app.window = FakeModel(running), FakeWindow()
    return app


def test_a_stop_click_never_starts_a_donation(tmp_path):
    app = make_app(running=False, tmp_path=tmp_path)           # donating ended between the label and the click
    app._toggle(stop=True)
    assert app.model.donated == 0 and app.model.stopped == 0


def test_a_stop_click_stops_and_a_donate_click_donates(tmp_path):
    app = make_app(running=True, tmp_path=tmp_path)
    app._toggle(stop=True)
    assert app.model.stopped == 1
    idle = make_app(running=False, tmp_path=tmp_path)
    idle._toggle(stop=False)
    assert idle.model.donated == 1


def test_a_donate_click_that_arrives_once_donating_began_does_nothing_more(tmp_path):
    app = make_app(running=True, tmp_path=tmp_path)
    app._toggle(stop=False)
    assert app.model.stopped == 0 and app.model.donated == 0


# ---- 6. the tooltip -------------------------------------------------------------------------------------------------

def test_a_long_tooltip_is_cut_at_a_word_with_an_ellipsis_within_windows_limit():
    error = "The worker could not be started because the Microsoft Visual C++ runtime is missing from this computer and cannot be found anywhere"
    tip = worker.Status(state=worker.ERROR, error=error).tooltip
    assert len(tip) <= 127 and tip.endswith("…")
    body = tip[:-1]
    assert error.startswith(body.split("\n", 1)[1])             # a prefix of the message ...
    assert error[len(body.split("\n", 1)[1])] == " "           # ... that stopped exactly at a word boundary


def test_short_tooltips_are_untouched_and_the_limit_is_exact():
    assert worker.Status().tooltip == "DLNE Donate: idle"
    exact = "x " * 63 + "x"
    assert len(exact) == 127 and worker.fit_tooltip(exact) == exact
    assert len(worker.fit_tooltip(exact + "y")) <= 127
    assert worker.fit_tooltip("a" * 300) == "a" * 126 + "…"


# ---- 4. "Donating" only once the swarm has taken the machine ----------------------------------------------------

def test_the_role_line_and_the_local_engine_do_not_count_as_joined():
    reader = markers.OutputReader()
    changed = reader.feed("DLNE_ROLE: coding {\"model\":\"m.gguf\"}\n[worker] RPC Server started successfully.\n")
    assert "role" in changed and "heartbeat" not in changed and not reader.heartbeat_ok
    assert "heartbeat" not in reader.feed("Cannot reach Primary Coordinator (boom). Retrying in 5 seconds...\n")
    assert "heartbeat" not in reader.feed("Heartbeat ACK: [Worker-1] 1.2.3.4:5\n")           # the coordinator's own log line
    assert "heartbeat" in reader.feed("[worker] Heartbeat OK | Status: Active\n")


UNREACHABLE = '''
import json, time
print('DLNE_ROLE: coding ' + json.dumps({"model": "Fake-Model.gguf", "tier": "t1", "slots": 2}), flush=True)
print('Cannot reach Primary Coordinator (connect failed). Retrying in 5 seconds...', flush=True)
time.sleep(300)
'''


def test_an_unreachable_coordinator_leaves_joining_on_screen(rig_factory):
    rig = rig_factory(UNREACHABLE)
    assert rig.controller.start()
    status = rig.wait(lambda s: s.role is not None, what="the role line")
    time.sleep(0.5)
    status = rig.controller.status()
    assert status.state == worker.STARTING and "Joining" in status.message


def test_the_first_heartbeat_turns_joining_into_donating(rig_factory):
    rig = rig_factory(DONATING)
    rig.controller.start()
    rig.wait(lambda s: s.state == worker.DONATING, what="donating")


# ---- window: fits the screen, scrolls, is placed after it is mapped; first launch shows it -----------------------

@pytest.fixture(autouse=True)
def collect_tk_objects_on_the_main_thread():
    """A Tk variable or image freed by the garbage collector on another thread (the worker tests start threads) can crash Tcl."""
    yield
    import gc
    gc.collect()


def tk_root():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError as error:
        pytest.skip(f"no display: {error}")
    root.withdraw()
    return root


class WindowModel:
    """What the window reads from the model, for a 4 GB machine with no GPU (the one of the test)."""

    def __init__(self, hardware):
        from dlne_donate import settings as settings_store
        self.hardware = hardware
        self.settings = settings_store.default_settings(hardware, APP)
        self.swarm = type("Swarm", (), {"coding_tiers": [], "app_tiers": APP, "app_sizes": {}})()
        self.select = None
        self.controller = type("C", (), {"devices_changed": lambda self: False})()
        self._status = worker.Status()

    def describe(self):
        return tiers.describe(self.settings, self.hardware, [], APP)

    def status(self):
        return self._status

    def refresh_swarm(self, *a, **k):
        pass

    def autostart_enabled(self):
        return False


def open_window(root, hardware, work_area):
    from dlne_donate import ui

    window = ui.SettingsWindow(root, WindowModel(hardware), on_quit=lambda: None)
    window._work_area = lambda: work_area
    return window


def pump(root, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(0.02)


def test_the_window_fits_a_small_work_area_and_its_content_scrolls():
    root = tk_root()
    try:
        area = (0, 0, 1280, 500)             # a work area far shorter than the content
        window = open_window(root, machine(total_ram_gb=4, physical_cores=4, logical_threads=4), area)
        window.show()
        pump(root, 1.3)                       # past the settle passes
        win = window.win
        assert win.winfo_y() >= area[1] and win.winfo_y() + win.winfo_height() <= area[3]
        assert win.winfo_x() + win.winfo_width() <= area[2]
        assert window.scrollbar.winfo_ismapped()                     # content taller than the window: it scrolls
        assert window.button.winfo_ismapped()
        # the Donate button and the start-up options (the pinned part) are inside the window's bottom edge
        assert window.button.winfo_rooty() + window.button.winfo_height() <= win.winfo_rooty() + win.winfo_height()
        window.canvas.yview_moveto(1.0)
        root.update()
        assert window.canvas.yview()[1] == pytest.approx(1.0)          # the last setting can be scrolled to
    finally:
        root.destroy()


def test_the_window_needs_no_scrollbar_when_the_screen_is_tall_enough():
    root = tk_root()
    try:
        window = open_window(root, machine(total_ram_gb=4, physical_cores=4, logical_threads=4), (0, 0, 1920, 1400))
        window.show()
        pump(root, 0.5)
        assert not window.scrollbar.winfo_ismapped()
        assert window.win.winfo_height() <= 1400
    finally:
        root.destroy()


def test_the_window_is_put_back_when_the_system_moves_it_after_mapping():
    root = tk_root()
    try:
        area = (0, 0, 1280, 752)
        window = open_window(root, machine(total_ram_gb=4, physical_cores=4, logical_threads=4), area)
        window.show()
        root.update()
        window.win.geometry("+78+40")          # Windows' own default position, applied after the first map
        pump(root, 1.3)
        _w, _h, x, y = window._target()
        assert (window.win.winfo_x(), window.win.winfo_y()) == (x, y)
        assert (x, y) != (78, 40)
    finally:
        root.destroy()


def test_the_window_says_why_a_four_gb_machine_cannot_donate():
    root = tk_root()
    try:
        window = open_window(root, machine(total_ram_gb=4, physical_cores=4, logical_threads=4), (0, 0, 1280, 752))
        window.show()
        text = window.status_text.cget("text")
        assert text.startswith("This computer can't donate")
        assert "RAM" in text and str(window.button.cget("text")) == "Donate"
        assert "disabled" in window.button.state()
    finally:
        root.destroy()


def test_a_normal_launch_shows_the_window_and_an_autostart_launch_does_not(tmp_path, monkeypatch):
    import tkinter as tk

    from dlne_donate.app import App
    from dlne_donate.model import Model

    pytest.importorskip("pystray")
    seen = {}

    class FakeTray:
        def __init__(self, *a, **k):
            pass

        def start(self):
            return True

        def visible(self):
            return True

        def update(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(trayicon, "Tray", FakeTray)
    probe = tk_root()
    probe.destroy()

    def run(autostart):
        app = App(home=str(tmp_path / ("a" if autostart else "n")), autostart=autostart,
                  model=Model(str(tmp_path / ("ma" if autostart else "mn")), hardware=machine(total_ram_gb=4), fetch_info=False))

        def fake_mainloop(self, n=0):
            seen[autostart] = app.window.win.state() == "normal"
            app.root.destroy()
        monkeypatch.setattr(tk.Misc, "mainloop", fake_mainloop)
        app._run_locked()
        app.model.controller.shutdown()

    run(autostart=False)
    run(autostart=True)
    assert seen[False] is True and seen[True] is False


# ---- 8. HTTPS on a fresh Windows ---------------------------------------------------------------------------------

def test_the_lets_encrypt_root_is_always_trusted_even_when_the_system_store_lacks_it(monkeypatch):
    """A blank Windows 11 had no ISRG Root X1 in the store Python reads, so every request failed certificate checks."""
    import ssl
    from dlne_donate import installer

    empty = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)          # a system store with nothing in it
    monkeypatch.setattr(installer.ssl, "create_default_context", lambda: empty)
    installer.tls_context.cache_clear()
    try:
        context = installer.tls_context()
        subjects = [dict(field[0] for field in cert["subject"]).get("commonName") for cert in context.get_ca_certs()]
        assert "ISRG Root X1" in subjects
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname is True
    finally:
        installer.tls_context.cache_clear()


def test_every_request_the_app_makes_uses_that_context(monkeypatch, tmp_path):
    from dlne_donate import installer

    seen = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, size=-1):
            data, self.data = getattr(self, "data", b"{}"), b""
            return data

    def urlopen(request, timeout=None, context=None):
        seen.append(context)
        return Response()

    monkeypatch.setattr(installer.urllib.request, "urlopen", urlopen)
    installer.Http().get_json("https://dlne.rossgilsonlabs.dev/v1/release")
    installer.Http().download("https://dlne.rossgilsonlabs.dev/x", str(tmp_path / "x"), 10)
    assert seen == [installer.tls_context(), installer.tls_context()]
