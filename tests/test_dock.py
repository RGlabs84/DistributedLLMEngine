"""
Docked (2.5.0, docs/coding-members.md sections 1, 2 and 10): a GPU node reports whether it is on a wired link and
on mains power, so the coordinator shards a docked LAN laptop into its engine and makes an undocked one a coding
member. The probe never guesses: a fact it cannot read is left out, and a change is reported only after it has
held 60 s.
"""
import ctypes
import json
from types import SimpleNamespace

import pytest

from engine import authn, hardware, worker


def fake_linux(monkeypatch, files, dirs=(), links=None, system="Linux"):
    """A synthetic sysfs/proc: `files` maps path -> contents, `dirs` are existing directories, `links` realpaths."""
    files = dict(files)
    dirs = set(dirs)
    links = dict(links or {})
    monkeypatch.setattr(hardware.platform, "system", lambda: system)
    monkeypatch.setattr(hardware, "_read_sysfs", lambda path: files.get(path, ""))
    monkeypatch.setattr(hardware.os.path, "isdir", lambda path: path in dirs)
    monkeypatch.setattr(hardware.os.path, "realpath", lambda path: links.get(path, path))
    listing = {}
    for path in list(files) + list(dirs):
        while "/" in path.strip("/"):                      # every ancestor lists its child
            parent, name = path.rsplit("/", 1)
            listing.setdefault(parent, set()).add(name)
            path = parent
    monkeypatch.setattr(hardware, "_listdir", lambda path: sorted(listing.get(path, ())))


def route(*rows):
    head = "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
    return head + "".join(f"{i}\t{d}\t0128A8C0\t{f:04X}\t0\t0\t{m}\t{mask}\t0\t0\t0\n" for i, d, f, m, mask in rows)


DEFAULT = ("00000000", 0x3, "00000000")        # destination, flags UP|GATEWAY, mask


def laptop_dock(**overrides):
    """The owner's laptop: a USB-C dock's Ethernet carries the default route, mains online, battery full."""
    files = {
        hardware._PROC_NET_ROUTE: route(("enp56s0u1",) + DEFAULT[:1] + (DEFAULT[1], 100, DEFAULT[2]),
                                        ("wlan0",) + DEFAULT[:1] + (DEFAULT[1], 600, DEFAULT[2])),
        "/sys/class/net/enp56s0u1/type": "1", "/sys/class/net/enp56s0u1/carrier": "1",
        "/sys/class/net/enp56s0u1/uevent": "INTERFACE=enp56s0u1\nIFINDEX=4",
        "/sys/class/net/wlan0/type": "1", "/sys/class/net/wlan0/carrier": "1",
        "/sys/class/power_supply/ADP0/type": "Mains", "/sys/class/power_supply/ADP0/online": "1",
        "/sys/class/power_supply/BAT0/type": "Battery", "/sys/class/power_supply/BAT0/status": "Not charging",
        "/sys/class/power_supply/BAT0/scope": "System",
        "/sys/class/dmi/id/sys_vendor": "ASUSTeK COMPUTER INC.", "/sys/class/dmi/id/product_name": "ROG Zephyrus",
    }
    files.update(overrides)
    return files


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    for name in ("_dock_cache", "_dock_reported", "_dock_pending"):
        monkeypatch.setattr(hardware, name, None)
    monkeypatch.setattr(hardware, "_dock_cache_expiry", 0.0)
    monkeypatch.setattr(hardware, "_dock_pending_since", 0.0)


def test_a_docked_laptop_is_wired_and_on_mains(monkeypatch):
    fake_linux(monkeypatch, laptop_dock(), dirs={"/sys/class/net/wlan0/wireless"})
    assert hardware._probe_dock() == {"link": "wired", "power": "ac"}


def test_the_profile_carries_the_three_facts_only_when_known(monkeypatch):
    monkeypatch.setattr(hardware, "get_dock_state", lambda: {"link": "wired", "power": "ac"})
    monkeypatch.setattr(hardware, "get_gpu_info", lambda: {"has_gpu": True, "vram_total_gb": 8.0, "vram_free_gb": 1.0,
                                                           "name": "x", "load_percent": 0.0})
    monkeypatch.setattr(hardware, "get_gpu_type", lambda: "nvidia")
    monkeypatch.setattr(hardware, "is_gaming_or_heavy_load", lambda info: False)
    monkeypatch.setattr(hardware, "get_cpu_features", lambda: [])
    profile = hardware.get_hardware_profile()
    assert (profile["link"], profile["power"], profile["docked"]) == ("wired", "ac", True)
    assert authn.validate_hardware_profile(profile)[0] is True

    monkeypatch.setattr(hardware, "get_dock_state", lambda: {"link": "wifi", "power": "battery"})
    profile = hardware.get_hardware_profile()
    assert (profile["link"], profile["power"], profile["docked"]) == ("wifi", "battery", False)

    monkeypatch.setattr(hardware, "get_dock_state", lambda: {"link": "unknown", "power": "ac"})
    profile = hardware.get_hardware_profile()
    assert "link" not in profile and "docked" not in profile and profile["power"] == "ac", "never guessed"
    assert authn.validate_hardware_profile(profile)[0] is True


def test_wifi_on_battery_is_undocked(monkeypatch):
    files = laptop_dock(**{
        hardware._PROC_NET_ROUTE: route(("wlan0",) + DEFAULT[:1] + (DEFAULT[1], 600, DEFAULT[2])),
        "/sys/class/power_supply/ADP0/online": "0", "/sys/class/power_supply/BAT0/status": "Discharging",
    })
    fake_linux(monkeypatch, files, dirs={"/sys/class/net/wlan0/wireless"})
    assert hardware._probe_dock() == {"link": "wifi", "power": "battery"}


def test_a_default_route_over_a_vpn_is_looked_past(monkeypatch):
    files = laptop_dock(**{hardware._PROC_NET_ROUTE: route(("wg-laptop",) + DEFAULT[:1] + (DEFAULT[1], 50, DEFAULT[2]),
                                                           ("enp56s0u1",) + DEFAULT[:1] + (DEFAULT[1], 100, DEFAULT[2]))})
    fake_linux(monkeypatch, files)
    assert hardware._linux_link() == "wired"


def test_a_bridge_or_bond_is_judged_by_the_port_under_it(monkeypatch):
    files = laptop_dock(**{hardware._PROC_NET_ROUTE: route(("br0",) + DEFAULT[:1] + (DEFAULT[1], 100, DEFAULT[2])),
                           "/sys/class/net/wlan0/type": "1", "/sys/class/net/wlan0/carrier": "1"})
    fake_linux(monkeypatch, files, dirs={"/sys/class/net/br0/brif", "/sys/class/net/br0/brif/wlan0",
                                         "/sys/class/net/wlan0/wireless"})
    assert hardware._linux_link() == "wifi", "a bridge over Wi-Fi is Wi-Fi"
    files = laptop_dock(**{hardware._PROC_NET_ROUTE: route(("bond0",) + DEFAULT[:1] + (DEFAULT[1], 100, DEFAULT[2])),
                           "/sys/class/net/bond0/bonding/slaves": "enp56s0u1 enp108s0"})
    fake_linux(monkeypatch, files)
    assert hardware._linux_link() == "wired"


def test_a_tethered_phone_is_cellular_and_a_dead_link_is_unknown(monkeypatch):
    files = laptop_dock()
    fake_linux(monkeypatch, files, links={"/sys/class/net/enp56s0u1/device/driver": "/sys/bus/usb/drivers/ipheth"})
    assert hardware._linux_link() == "cellular"
    fake_linux(monkeypatch, laptop_dock(**{"/sys/class/net/enp56s0u1/carrier": "0"}))
    assert hardware._linux_link() == "unknown"


def test_power_rules(monkeypatch):
    desktop = {"/sys/class/power_supply/ADP0/type": "Mains", "/sys/class/power_supply/ADP0/online": "1"}
    fake_linux(monkeypatch, desktop)
    assert hardware._linux_power() == "ac"
    fake_linux(monkeypatch, {})
    assert hardware._linux_power() == "ac", "no system battery at all is a desktop"
    mouse = {"/sys/class/power_supply/hid-aa/type": "Battery", "/sys/class/power_supply/hid-aa/scope": "Device",
             "/sys/class/power_supply/hid-aa/status": "Discharging"}
    fake_linux(monkeypatch, mouse)
    assert hardware._linux_power() == "ac", "a mouse's battery is not the machine's"
    fake_linux(monkeypatch, {"/sys/class/power_supply/BAT0/type": "Battery", "/sys/class/power_supply/BAT0/status": "Unknown"})
    assert hardware._linux_power() == "unknown"


def test_a_virtual_machine_never_claims_a_link(monkeypatch):
    fake_linux(monkeypatch, laptop_dock(**{"/sys/class/dmi/id/sys_vendor": "QEMU"}))
    assert hardware._probe_dock() == {"link": "unknown", "power": "ac"}
    fake_linux(monkeypatch, laptop_dock(**{"/proc/cpuinfo": "flags\t\t: fpu vme hypervisor sse"}))
    assert hardware._probe_dock()["link"] == "unknown"


def test_a_change_is_reported_only_after_it_has_held_a_minute(monkeypatch):
    clock = SimpleNamespace(t=1000.0)
    monkeypatch.setattr(hardware.time, "monotonic", lambda: clock.t)
    readings = [{"link": "wired", "power": "ac"}]
    monkeypatch.setattr(hardware, "_probe_dock", lambda: dict(readings[-1]))
    assert hardware.get_dock_state() == {"link": "wired", "power": "ac"}, "the first reading is reported at once"
    readings.append({"link": "wifi", "power": "ac"})                # the dock's cable is pulled, still on mains
    clock.t += hardware._DOCK_TTL_SECONDS + 1
    assert hardware.get_dock_state() == {"link": "wired", "power": "ac"}, "not yet"
    clock.t += 30
    assert hardware.get_dock_state() == {"link": "wired", "power": "ac"}
    readings.append({"link": "wired", "power": "ac"})               # replugged inside the hold: nothing moved
    clock.t += hardware._DOCK_TTL_SECONDS + 1
    assert hardware.get_dock_state() == {"link": "wired", "power": "ac"}
    readings.append({"link": "wifi", "power": "ac"})                # unplugged for good
    clock.t += hardware._DOCK_TTL_SECONDS + 1
    hardware.get_dock_state()
    clock.t += hardware._DOCK_HOLD_SECONDS + 1
    assert hardware.get_dock_state() == {"link": "wifi", "power": "ac"}


def test_going_onto_battery_is_reported_at_once_and_back_onto_mains_waits(monkeypatch):
    """2.6.4: never shard on battery. A laptop just unplugged may be shut and asleep within the minute, so the
    coordinator hears of it at once and its drain starts while the machine is awake; plugging back in waits."""
    clock = SimpleNamespace(t=1000.0)
    monkeypatch.setattr(hardware.time, "monotonic", lambda: clock.t)
    readings = [{"link": "wifi", "power": "ac"}]
    monkeypatch.setattr(hardware, "_probe_dock", lambda: dict(readings[-1]))
    assert hardware.get_dock_state() == {"link": "wifi", "power": "ac"}
    readings.append({"link": "wifi", "power": "battery"})           # unplugged
    clock.t += hardware._DOCK_TTL_SECONDS + 1
    assert hardware.get_dock_state() == {"link": "wifi", "power": "battery"}, "no hold onto battery"
    readings.append({"link": "wifi", "power": "ac"})                # plugged back in
    clock.t += hardware._DOCK_TTL_SECONDS + 1
    assert hardware.get_dock_state() == {"link": "wifi", "power": "battery"}, "the way back waits out the hold"
    clock.t += hardware._DOCK_HOLD_SECONDS + 1
    assert hardware.get_dock_state() == {"link": "wifi", "power": "ac"}
    readings.append({"link": "unknown", "power": "battery"})        # a fact lost on the way does not delay it
    clock.t += hardware._DOCK_TTL_SECONDS + 1
    assert hardware.get_dock_state() == {"link": "unknown", "power": "battery"}


def test_the_probe_is_cached_and_never_raises(monkeypatch):
    calls = []
    def boom():
        calls.append(1)
        raise RuntimeError("sysfs vanished")
    monkeypatch.setattr(hardware, "_probe_dock", boom)
    assert hardware.get_dock_state() == {"link": "unknown", "power": "unknown"}
    assert hardware.get_dock_state() == {"link": "unknown", "power": "unknown"}
    assert calls == [1], "one probe per TTL"


# ---- Windows (faked ctypes and PowerShell, like tests/test_hardware_windows.py) -----------------------------

@pytest.fixture
def windows(monkeypatch):
    state = SimpleNamespace(ac=1, flag=0, ok=1, json='{"media":"802.3","virtual":false,"status":"Up","model":"Precision 5570"}')

    def power_status(reference):
        reference._obj.ACLineStatus = state.ac
        reference._obj.BatteryFlag = state.flag
        return state.ok

    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=SimpleNamespace(GetSystemPowerStatus=power_status)),
                        raising=False)

    def run(argv, **kwargs):
        assert argv[:3] == ["powershell", "-NoProfile", "-NonInteractive"]
        return SimpleNamespace(returncode=0, stdout=state.json, stderr="")

    monkeypatch.setattr(hardware.subprocess, "run", run)
    monkeypatch.setattr(hardware.platform, "system", lambda: "Windows")
    return state


def test_windows_docked(windows):
    assert hardware._probe_dock() == {"link": "wired", "power": "ac"}


def test_windows_wifi_on_battery_and_the_odd_cases(windows):
    windows.ac, windows.json = 0, '{"media":"Native 802.11","virtual":false,"status":"Up","model":"XPS 13"}'
    assert hardware._probe_dock() == {"link": "wifi", "power": "battery"}
    windows.flag = 128                                                        # no system battery: a desktop
    assert hardware._probe_dock()["power"] == "ac"
    windows.json = '{"media":"802.3","virtual":true,"status":"Up","model":"XPS 13"}'   # Hyper-V vEthernet
    assert hardware._probe_dock()["link"] == "unknown"
    windows.json = '{"media":"802.3","virtual":false,"status":"Up","model":"Virtual Machine"}'
    assert hardware._probe_dock()["link"] == "unknown", "a VM never claims a link"
    windows.ok = 0
    assert hardware._probe_dock()["power"] == "unknown"


# ---- validation and the worker's marker ------------------------------------------------------------------

@pytest.mark.parametrize("profile, ok", [
    ({"link": "wired", "power": "ac", "docked": True}, True),
    ({"link": "wifi", "power": "battery", "docked": False}, True),
    ({"link": "cellular"}, True),
    ({"docked": True}, True),
    ({"link": "unknown"}, False),
    ({"power": "unknown"}, False),
    ({"link": "ethernet"}, False),
    ({"docked": "yes"}, False),
    ({"docked": 1}, False),
])
def test_the_coordinator_accepts_only_the_known_values(profile, ok):
    assert authn.validate_hardware_profile(profile)[0] is ok


class _Node:
    """Just enough of WorkerNode for _report_dock."""
    companion_of = None
    hardware_profile = {}
    _wifi_check = None
    _report_dock = worker.WorkerNode._report_dock


def test_the_worker_prints_the_dock_marker_once_per_change(capsys):
    node = _Node()
    node.hardware_profile = {"os": "Linux", "link": "wired", "power": "ac", "docked": True}
    node._report_dock()
    node._report_dock()
    node.hardware_profile = {"os": "Linux", "link": "wifi", "power": "battery", "docked": False}
    node._report_dock()
    node.hardware_profile = {"os": "Linux", "power": "ac"}                   # the link could not be read
    node._report_dock()
    lines = [l for l in capsys.readouterr().out.splitlines() if l.startswith(worker.DOCK_MARKER)]
    assert [json.loads(l[len(worker.DOCK_MARKER):]) for l in lines] == [
        {"link": "wired", "power": "ac", "docked": True},
        {"link": "wifi", "power": "battery", "docked": False},
        {"power": "ac"},
    ]
    node.companion_of = "gpu-node"
    node.hardware_profile = {"link": "wired", "power": "ac", "docked": True}
    node._report_dock()
    assert not capsys.readouterr().out, "a companion stays quiet"


def test_on_wifi_and_mains_the_marker_carries_the_coordinators_verdict(capsys):
    """2.6.4: the editor and the tray app say why a Wi-Fi GPU does or does not share the model."""
    node = _Node()
    node.hardware_profile = {"link": "wifi", "power": "ac", "docked": False}
    node._report_dock()                                                      # no answer yet: being tested
    node._wifi_check = {"ok": True, "why": "round trip 4 ms, jitter 2 ms, download 312 Mbit/s"}
    node._report_dock()
    node._report_dock()
    node.hardware_profile = {"link": "wifi", "power": "battery", "docked": False}
    node._report_dock()                                                      # on battery: no verdict to give
    lines = [l for l in capsys.readouterr().out.splitlines() if l.startswith(worker.DOCK_MARKER)]
    assert [json.loads(l[len(worker.DOCK_MARKER):]) for l in lines] == [
        {"link": "wifi", "power": "ac", "docked": False, "wifi_ok": None, "wifi_why": ""},
        {"link": "wifi", "power": "ac", "docked": False, "wifi_ok": True,
         "wifi_why": "round trip 4 ms, jitter 2 ms, download 312 Mbit/s"},
        {"link": "wifi", "power": "battery", "docked": False},
    ]


class _AnswerNode:
    """Just enough of WorkerNode for _note_wifi_check and _start_speed_test (the test itself is not run)."""
    _wifi_check = None
    _speed_test_running = False
    _last_speed_test = float("-inf")
    _note_wifi_check = worker.WorkerNode._note_wifi_check
    _start_speed_test = worker.WorkerNode._start_speed_test

    def __init__(self):
        self.started = 0

    def _run_speed_test(self):
        self.started += 1


def test_the_worker_keeps_the_verdict_and_runs_the_speed_test_it_is_asked_for(monkeypatch):
    clock = SimpleNamespace(t=1000.0)
    monkeypatch.setattr(worker.time, "monotonic", lambda: clock.t)
    started = []
    monkeypatch.setattr(worker.threading, "Thread", lambda target, **_: SimpleNamespace(start=lambda: started.append(target)))
    node = _AnswerNode()
    node._note_wifi_check({"wifi_check": {"ok": None, "why": "the link test has not run yet", "speed_test": True}})
    assert node._wifi_check == {"ok": None, "why": "the link test has not run yet"}
    assert len(started) == 1 and node._speed_test_running
    node._speed_test_running = False                       # that test finished
    node._note_wifi_check({"wifi_check": {"ok": False, "why": "waiting for this machine's download test", "speed_test": True}})
    assert len(started) == 1, "at most one test every WIFI_SPEED_TEST_MIN_GAP_SECONDS"
    clock.t += worker.WIFI_SPEED_TEST_MIN_GAP_SECONDS
    node._note_wifi_check({"wifi_check": {"ok": "yes", "why": 7, "speed_test": True}})
    assert node._wifi_check == {"ok": None, "why": ""}, "checked, not trusted"
    assert len(started) == 2
    node._note_wifi_check({"status": "ok"})
    assert node._wifi_check is None, "no verdict sent, none kept"
