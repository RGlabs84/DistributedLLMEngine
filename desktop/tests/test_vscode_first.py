"""
VS Code first (docs/release-2.7-contract.md section 4b): ~/.dlne/editor.json says DLNE is open in VS Code; while it does, the
tray's worker is paused (left the way Stop leaves), and it comes back by itself if it was donating before.
"""

import json
import os
import subprocess
import sys
from dataclasses import replace

import pytest

from dlne_donate import engine_flavour, paths, tiers, vscode_first, worker
from dlne_donate.tiers import Hardware
from dlne_donate.vscode_first import (CHECK_EVERY_S, EDITOR, GONE_AFTER_S, MAX_AGE_S, MISSES_TO_BE_GONE, WORKER, VsCodeFirst,
                                      read_presence)

HW = Hardware(gpu_name="RTX 4070", vram_gb=8, gpu_count=1, total_ram_gb=32, physical_cores=8, logical_threads=16)
NOW = 1_800_000_000.0


def write(path, **facts):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"pid": 4242, "updated": NOW, "version": "1.6.0", **facts}, handle)


@pytest.fixture
def editor_file():
    return paths.editor_presence_path()


# ---- the presence file -------------------------------------------------------------------------------------------------

def test_the_presence_file_defaults_to_the_users_dot_dlne_folder(monkeypatch):
    monkeypatch.delenv(paths.EDITOR_FILE_ENV)
    assert paths.editor_presence_path() == os.path.join(os.path.expanduser("~"), ".dlne", "editor.json")


def test_present_means_fresh_and_a_live_pid(editor_file):
    write(editor_file)
    alive = lambda pid: pid == 4242
    assert read_presence(editor_file, NOW + 1, alive)
    assert read_presence(editor_file, NOW + MAX_AGE_S - 0.5, alive), "less than 90 s old"
    assert not read_presence(editor_file, NOW + MAX_AGE_S, alive), "90 s old is gone"
    assert not read_presence(editor_file, NOW + 3600, alive)
    assert not read_presence(editor_file, NOW + 1, lambda pid: False), "fresh, but its VS Code was killed"


def test_a_file_from_the_future_is_as_stale_as_one_from_the_past(editor_file):
    write(editor_file, updated=NOW + 3600)
    assert not read_presence(editor_file, NOW, lambda pid: True)


def test_a_missing_or_damaged_file_is_nobody(editor_file):
    assert not read_presence(editor_file, NOW, lambda pid: True)
    os.makedirs(os.path.dirname(editor_file), exist_ok=True)
    for text in ("", "not json", "[]", "null", '{"pid": 4242}', '{"updated": 1800000000}'):
        open(editor_file, "w", encoding="utf-8").write(text)
        assert not read_presence(editor_file, NOW, lambda pid: True), text
    for bad in ({"pid": "4242"}, {"pid": 0}, {"pid": -5}, {"pid": True}, {"pid": 1.5}, {"updated": "now"}, {"updated": None},
                {"updated": True}):
        write(editor_file, **bad)
        assert not read_presence(editor_file, NOW, lambda pid: True), bad


def test_the_pid_check_is_the_real_one_by_default(editor_file):
    write(editor_file, pid=os.getpid())
    assert read_presence(editor_file, NOW)
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    write(editor_file, pid=child.pid)
    assert not read_presence(editor_file, NOW), "a process that has ended"
    assert vscode_first.process_alive(os.getpid()) and not vscode_first.process_alive(child.pid)


# ---- the state machine, with a fake clock and fake checks -------------------------------------------------------------

class World:
    """A fake clock, a fake presence file, a fake "another worker" check and a fake worker that the machine under test pokes at."""

    def __init__(self, running=False):
        self.now = 0.0
        self.present = False
        self.other = False                 # a DLNE worker that is not the tray's own runs on this computer
        self.running = running
        self.stopping = False
        self.stops = 0
        self.starts = 0
        self.can_start = True
        self.paused_calls = []
        self.checks = 0

    def check(self):
        self.checks += 1
        return self.present

    def stop(self):
        self.stops += 1
        self.running = False

    def start(self):
        self.starts += 1
        if self.can_start:
            self.running = True
        return self.can_start

    def machine(self):
        return VsCodeFirst(self.check, lambda: self.running, lambda: self.stopping, self.stop, self.start,
                           on_change=self.paused_calls.append, other_worker=lambda: self.other, clock=lambda: self.now)

    def advance(self, machine, seconds, step=1.0):
        end = self.now + seconds
        while self.now < end:
            self.now += step
            machine.tick()


GONE = GONE_AFTER_S + CHECK_EVERY_S           # long enough for the editor to count as gone, with a look to spare


def test_it_looks_every_ten_seconds_and_no_more_often():
    world = World()
    machine = world.machine()
    machine.tick()
    assert world.checks == 1, "the first look is at once"
    world.advance(machine, 9.0)
    assert world.checks == 1
    world.advance(machine, 1.0)
    assert world.checks == 2
    world.advance(machine, 100.0)
    assert world.checks == 2 + int(100 // CHECK_EVERY_S)


def test_a_donating_worker_is_stopped_when_vs_code_appears_and_comes_back_when_it_goes():
    world = World(running=True)
    machine = world.machine()
    world.advance(machine, 25)
    assert (world.stops, world.starts, machine.paused) == (0, 0, False)

    world.present = True
    world.advance(machine, 10)
    assert machine.paused and machine.reason == EDITOR and world.stops == 1 and not world.running
    assert world.paused_calls == [EDITOR]
    world.advance(machine, 120)
    assert world.starts == 0 and world.stops == 1, "stays down for as long as VS Code stays"

    world.present = False
    world.advance(machine, GONE)
    assert not machine.paused and world.starts == 1 and world.running
    assert world.paused_calls == [EDITOR, ""]
    world.advance(machine, 60)
    assert world.starts == 1, "resumes once"


def test_a_tray_that_was_not_donating_stays_off_when_vs_code_goes():
    world = World(running=False)
    machine = world.machine()
    world.present = True
    world.advance(machine, 30)
    assert machine.paused and world.stops == 0, "nothing to stop"
    world.present = False
    world.advance(machine, GONE + 10)
    assert not machine.paused and world.starts == 0, "it was not donating before the pause, so it does not start"


def test_a_worker_the_owner_was_stopping_is_not_brought_back():
    world = World(running=True)
    world.stopping = True
    machine = world.machine()
    world.present = True
    world.advance(machine, 10)
    assert machine.paused and world.stops == 0 and not machine.resume_wanted
    world.present = False
    world.stopping = False
    world.running = False
    world.advance(machine, GONE + 10)
    assert world.starts == 0


def test_a_worker_still_leaving_is_waited_for_before_the_resume():
    world = World(running=True)
    machine = world.machine()
    world.present = True
    world.advance(machine, 10)
    world.running, world.stopping = True, True           # the worker needs a few seconds to leave
    world.present = False
    world.advance(machine, GONE + 10)
    assert world.starts == 0 and machine.resume_wanted, "waits, remembers"
    world.running = world.stopping = False
    world.advance(machine, 10)
    assert world.starts == 1 and not machine.resume_wanted


def test_a_failed_start_is_tried_again_on_the_next_look():
    world = World(running=True)
    machine = world.machine()
    world.present = True
    world.advance(machine, 10)
    world.present = False
    world.can_start = False
    world.advance(machine, GONE + 20)
    assert world.starts >= 3 and machine.resume_wanted
    tried = world.starts
    world.can_start = True
    world.advance(machine, 10)
    assert world.starts == tried + 1 and world.running and not machine.resume_wanted


def test_a_worker_started_during_the_pause_is_stopped_again():
    world = World(running=True)
    machine = world.machine()
    world.present = True
    world.advance(machine, 10)
    world.running = True                                  # a click that raced the look
    world.advance(machine, 10)
    assert world.stops == 2 and not world.running


def test_flapping_presence_pauses_and_resumes_each_time():
    world = World(running=True)
    machine = world.machine()
    for _ in range(3):
        world.present = True
        world.advance(machine, 10)
        assert machine.paused and not world.running
        world.present = False
        world.advance(machine, GONE)
        assert not machine.paused and world.running
    assert (world.stops, world.starts) == (3, 3)


def test_a_launch_that_wanted_to_donate_waits_for_the_editor():
    world = World(running=False)
    machine = world.machine()
    world.present = True
    machine.tick()
    machine.want_donating()                               # "start donating when the app starts", but VS Code is open
    world.present = False
    world.advance(machine, GONE)
    assert world.starts == 1 and world.running
    machine.want_donating()
    assert not machine.resume_wanted, "only while paused"


def test_forcing_a_look_ignores_the_ten_seconds():
    world = World()
    machine = world.machine()
    machine.tick()
    world.present = True
    machine.tick()
    assert not machine.paused
    machine.tick(force=True)
    assert machine.paused


# ---- the three safeguards builder C asked for ---------------------------------------------------------------------------

def test_a_file_that_vanishes_for_one_look_does_not_resume_the_worker():
    """Item 3: several VS Code windows; one closes (the file goes), another refreshes it at its next 30 s beat."""
    world = World(running=True)
    machine = world.machine()
    world.present = True
    world.advance(machine, 10)
    assert machine.paused
    world.present = False
    world.advance(machine, 10)                            # one miss: not gone
    assert machine.paused and world.starts == 0
    world.present = True                                  # the other window's refresh
    world.advance(machine, 10)
    assert machine.paused and world.starts == 0 and world.stops == 1
    world.present = False
    world.advance(machine, 10)
    world.present = True
    world.advance(machine, 10)
    assert machine.paused and world.starts == 0, "misses are counted in a row: a hit starts the count over"


def test_the_editor_is_gone_only_after_two_misses_at_least_twenty_seconds_apart():
    world = World(running=True)
    machine = world.machine()
    world.present = True
    world.advance(machine, 10)
    world.present = False
    looks = []
    while machine.paused:
        world.now += 1
        before = world.checks
        machine.tick()
        if world.checks > before:
            looks.append(world.now)
        assert world.now < 200
    assert len(looks) >= MISSES_TO_BE_GONE and looks[-1] - looks[0] >= GONE_AFTER_S
    assert world.starts == 1


def test_it_does_not_resume_while_another_dlne_worker_still_runs():
    """Item 2: the extension keeps its worker alive after VS Code closes; a second worker must not start beside it."""
    world = World(running=True)
    machine = world.machine()
    world.present = True
    world.advance(machine, 10)
    world.present = False
    world.other = True                                    # VS Code is closed, its worker is not
    world.advance(machine, GONE)
    assert machine.paused and machine.reason == WORKER and world.starts == 0
    assert world.paused_calls == [EDITOR, WORKER]
    checks = world.checks
    world.advance(machine, 120)
    assert machine.reason == WORKER and world.starts == 0, "rechecked every ten seconds, still there"
    assert world.checks - checks >= 11
    world.other = False                                   # the extension's worker ended
    world.advance(machine, 10)
    assert not machine.paused and world.starts == 1 and world.running
    assert world.paused_calls == [EDITOR, WORKER, ""]


def test_an_editor_coming_back_during_the_worker_wait_pauses_for_the_editor_again():
    world = World(running=True)
    machine = world.machine()
    world.present = True
    world.advance(machine, 10)
    world.present = False
    world.other = True
    world.advance(machine, GONE)
    assert machine.reason == WORKER
    world.present = True
    world.advance(machine, 10)
    assert machine.reason == EDITOR and machine.resume_wanted
    world.present, world.other = False, False
    world.advance(machine, GONE)
    assert not machine.paused and world.starts == 1


def test_another_worker_does_not_hold_back_a_tray_that_never_saw_an_editor():
    """Nothing was paused: it is the owner's own click that starts the worker, and that click checks nothing here."""
    world = World(running=False)
    machine = world.machine()
    world.other = True
    world.advance(machine, 60)
    assert machine.reason == "" and world.starts == 0 and world.paused_calls == []


def test_a_tray_that_was_not_donating_waits_for_the_worker_too():
    world = World(running=False)
    machine = world.machine()
    world.present = True
    world.advance(machine, 10)
    world.present = False
    world.other = True
    world.advance(machine, GONE)
    assert machine.reason == WORKER, "still held back (Donate stays off), though nothing will be started"
    world.other = False
    world.advance(machine, 10)
    assert not machine.paused and world.starts == 0


def test_the_other_worker_check_matches_the_arguments_exactly():
    own = ["/usr/bin/python3", "-m", "dlne_donate", "--run-worker", "/x/engine", "--", "worker", "--coordinator", "https://c"]
    theirs = ["/usr/bin/python3", "/home/u/.dlne/engine/dlne_server.py", "worker", "--coordinator", "https://c"]
    windows = ["C:\\Python\\python.exe", "C:\\Users\\u\\.dlne\\engine\\dlne_server.py", "worker"]
    assert vscode_first.is_dlne_worker_command(theirs) and vscode_first.is_dlne_worker_command(windows)
    assert not vscode_first.is_dlne_worker_command(own)
    assert not vscode_first.is_dlne_worker_command(["vim", "dlne_server.py"])
    assert not vscode_first.is_dlne_worker_command(["python", "dlne_server.py", "coordinator"])
    assert not vscode_first.is_dlne_worker_command(["python", "my_dlne_server.py", "worker"])
    assert not vscode_first.is_dlne_worker_command([])


class FakeProc:
    def __init__(self, pid, cmdline, parents=()):
        self.pid = pid
        self.info = {"pid": pid, "cmdline": cmdline}
        self._parents = list(parents)

    def parents(self):
        return self._parents


class FakePsutil:
    def __init__(self, procs):
        self.procs = procs

    def process_iter(self, _attrs=None):
        return iter(self.procs)


def test_another_worker_is_a_process_with_the_workers_arguments_or_a_busy_rpc_port():
    theirs = ["python3", "/e/dlne_server.py", "worker"]
    free = lambda port: False
    assert vscode_first.other_worker_running(100, FakePsutil([FakeProc(7, theirs)]), free)
    assert not vscode_first.other_worker_running(100, FakePsutil([FakeProc(7, ["python3", "-m", "http.server"])]), free)
    assert not vscode_first.other_worker_running(100, FakePsutil([]), free)
    busy = lambda port: port == vscode_first.RPC_PORTS[0]
    assert vscode_first.other_worker_running(100, FakePsutil([]), busy), "its RPC port is in use"


def test_the_trays_own_worker_never_counts_as_another():
    me = FakeProc(100, ["python3", "x"])
    child = FakeProc(101, ["python3", "/e/dlne_server.py", "worker"], parents=[me])
    assert not vscode_first.other_worker_running(100, FakePsutil([child]), lambda port: False)
    assert not vscode_first.other_worker_running(100, FakePsutil([FakeProc(100, ["python3", "dlne_server.py", "worker"])]),
                                                 lambda port: False)


def test_a_process_that_cannot_be_inspected_is_skipped_not_a_crash():
    class Ended(FakeProc):
        def parents(self):
            raise RuntimeError("it ended")

    theirs = ["python3", "/e/dlne_server.py", "worker"]
    assert not vscode_first.other_worker_running(100, FakePsutil([Ended(8, theirs)]), lambda port: False)


def test_the_port_probe_sees_a_listening_socket():
    import socket
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("0.0.0.0", 0))
    listener.listen(1)
    try:
        assert vscode_first.port_in_use(listener.getsockname()[1])
    finally:
        listener.close()


# ---- the model and the controller ---------------------------------------------------------------------------------------

class FakeController:
    def __init__(self, home, get_settings, get_hardware, **kwargs):
        self.on_change = kwargs["on_change"]
        self.s = worker.Status()
        self.starts = self.stops = 0

    def apply_settings(self):
        pass

    def status(self):
        return self.s

    def start(self):
        self.starts += 1
        self.s = replace(self.s, state=worker.DONATING)
        return True

    def stop(self, wait=False):
        self.stops += 1
        self.s = replace(self.s, state=worker.IDLE)

    def set_paused(self, paused, reason="editor"):
        self.s = replace(self.s, paused=paused, pause_reason=reason if paused else "")


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make_model(app_home, present, other=None):
    from dlne_donate.model import Model

    clock = Clock()
    model = Model(str(app_home), hardware=HW, controller_factory=FakeController, fetch_info=False,
                  editor_present=lambda: present["now"], other_worker=(lambda: other["now"]) if other else None, clock=clock)
    model.clock = clock
    return model


def look(model, seconds):
    """Looks for the editor every ten seconds for ``seconds`` (a forced look needs no ten-second wait, but the clock is real to the machine)."""
    for _ in range(int(seconds // CHECK_EVERY_S)):
        model.clock.t += CHECK_EVERY_S
        model.check_editor(force=True)


def test_the_model_pauses_stops_and_resumes_the_worker(app_home):
    present = {"now": False}
    model = make_model(app_home, present)
    assert model.donate() and model.controller.s.state == worker.DONATING
    present["now"] = True
    model.check_editor(force=True)
    assert model.paused and model.controller.stops == 1 and model.controller.s.paused
    assert model.controller.s.tooltip == f"DLNE Donate\n{engine_flavour.PAUSED_TEXT}"
    assert not model.donate(), "no Donate while VS Code is open"
    assert model.controller.starts == 1
    present["now"] = False
    look(model, GONE)
    assert not model.paused and not model.controller.s.paused
    assert model.controller.starts == 2 and model.controller.s.state == worker.DONATING


def test_a_donate_asked_for_while_paused_happens_when_vs_code_closes(app_home):
    present = {"now": True}
    model = make_model(app_home, present)
    model.check_editor(force=True)
    assert model.paused and not model.donate()
    present["now"] = False
    look(model, GONE)
    assert model.controller.starts == 1


def test_a_resume_with_nothing_left_to_lend_is_dropped(app_home):
    present = {"now": False}
    model = make_model(app_home, present)
    model.donate()
    present["now"] = True
    model.check_editor(force=True)
    model.update(gpu_enabled=False, cpu_enabled=False)    # the owner changed it during the pause
    present["now"] = False
    look(model, GONE)
    assert model.controller.starts == 1 and not model.vscode_first.resume_wanted


def test_the_model_keeps_the_worker_down_while_vs_codes_worker_still_runs(app_home):
    present, other = {"now": False}, {"now": False}
    model = make_model(app_home, present, other)
    model.donate()
    present["now"] = True
    model.check_editor(force=True)
    present["now"], other["now"] = False, True
    look(model, GONE)
    assert model.paused and model.controller.s.pause_reason == WORKER and model.controller.starts == 1
    assert model.controller.s.tooltip == f"DLNE Donate\n{engine_flavour.WORKER_STILL_TEXT}"
    assert engine_flavour.WORKER_STILL_TEXT == "Paused: DLNE's worker from VS Code is still running"
    other["now"] = False
    look(model, CHECK_EVERY_S)
    assert not model.paused and model.controller.starts == 2


def test_the_state_file_carries_the_trays_version_and_says_paused_while_paused(app_home):
    present = {"now": False}
    model = make_model(app_home, present)
    model.donate()
    model._write_state(model.status())
    state = json.load(open(paths.state_path(str(app_home)), encoding="utf-8"))
    assert state["version"] == "1.1.0" and state["paused"] is False and state["state"] == worker.DONATING
    present["now"] = True
    model.check_editor(force=True)
    model._write_state(model.status())
    state = json.load(open(paths.state_path(str(app_home)), encoding="utf-8"))
    assert state["version"] == "1.1.0" and state["state"] == "paused" and state["paused"] is True and state["pid"] == os.getpid()
    # A worker that is still leaving is not "paused" yet: the extension waits for it.
    model.controller.s = replace(model.controller.s, state=worker.STOPPING)
    model._write_state(model.status())
    state = json.load(open(paths.state_path(str(app_home)), encoding="utf-8"))
    assert state["state"] == "stopping" and state["paused"] is True


def test_the_tooltip_and_the_window_text_say_why_it_is_paused():
    assert engine_flavour.PAUSED_TEXT == "Paused: DLNE in VS Code is using this computer"
    status = worker.Status(state=worker.STOPPING, paused=True)
    assert status.tooltip == "DLNE Donate\nPaused: DLNE in VS Code is using this computer"
    assert len(status.tooltip) <= worker.TOOLTIP_MAX
    assert worker.Status(state=worker.IDLE, paused=True).tooltip.endswith("using this computer")
    assert "Paused" not in worker.Status().tooltip


def test_the_controller_does_not_start_while_paused(app_home):
    settings = tiers.default_settings(HW, [])
    controller = worker.WorkerController(str(app_home), lambda: settings, lambda: HW, base_url="https://example.test")
    controller.set_paused(True)
    assert controller.status().paused and controller.start() is False
    assert controller.status().state == worker.IDLE
    controller.set_paused(False)
    assert not controller.status().paused
