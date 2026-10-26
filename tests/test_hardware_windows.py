"""
Windows video memory and the owner-load ("gaming mode") signal, engine/hardware.py.

* A card's memory on Windows comes from the registry's 64-bit ``HardwareInformation.qwMemorySize``
  whether or not vulkaninfo is installed. ``Win32_VideoController.AdapterRAM`` is 32 bits wide, reads
  4 GB for every card that has more, and is only the last resort: a 16 GB Radeon at 4 GB would fall
  under the coding member's 8 GB floor and never be admitted.
* While our own llama-server is serving, the GPU's total load says nothing about the owner. A
  full-screen game (Windows: ``SHQueryUserNotificationState``) or another process's share of an
  NVIDIA card (``nvidia-smi pmon``) is what makes the member yield.

Everything runs on any machine: the registry, ctypes' ``windll``, ``platform.system`` and
``subprocess.run`` are replaced. What these cannot show is the real Windows registry and shell;
those need a real PC.
"""

import ctypes
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from engine import hardware

GIB = 1024 ** 3


# ======================================================================================
# A fake registry
# ======================================================================================

REG_BINARY, REG_DWORD, REG_QWORD, REG_SZ = 3, 4, 11, 1


class FakeKey:
    def __init__(self, children=None, values=None):
        self.children, self.values = children or {}, values or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def display_class(**adapters):
    """The display class key: one subkey per adapter plus the "Configuration" and "Properties" keys real Windows has."""
    children = {"Configuration": FakeKey(), "Properties": FakeKey()}
    children.update(adapters)
    return FakeKey(children)


def fake_winreg(root_key):
    def open_key(parent, name=None):
        if parent == "HKLM":
            if name != hardware._DISPLAY_CLASS_KEY:
                raise FileNotFoundError(name)
            return root_key
        if name not in parent.children:
            raise FileNotFoundError(name)
        return parent.children[name]

    def enum_key(key, index):
        names = list(key.children)
        if index >= len(names):
            raise OSError("no more data")
        return names[index]

    def query_value_ex(key, name):
        if name not in key.values:
            raise FileNotFoundError(name)
        return key.values[name]

    return SimpleNamespace(HKEY_LOCAL_MACHINE="HKLM", OpenKey=open_key, EnumKey=enum_key, QueryValueEx=query_value_ex,
                           REG_BINARY=REG_BINARY, REG_DWORD=REG_DWORD, REG_QWORD=REG_QWORD)


def adapter(desc, memory, field="HardwareInformation.qwMemorySize", kind=REG_QWORD):
    return FakeKey(values={"DriverDesc": (desc, REG_SZ), field: (memory, kind)})


RADEON_16 = "AMD Radeon RX 6800 XT"
IGPU = "AMD Radeon(TM) Graphics"


@pytest.fixture
def registry(monkeypatch):
    def install(**adapters):
        monkeypatch.setitem(sys.modules, "winreg", fake_winreg(display_class(**adapters)))
    return install


# ---- reading the registry ----------------------------------------------------------------

def test_the_registry_gives_each_adapters_64_bit_size(registry):
    registry(**{
        "0000": adapter(IGPU, 512 * 1024 ** 2),
        "0001": adapter(RADEON_16, 16 * GIB),                                    # REG_QWORD
        "0002": adapter("Some Binary Card", (24 * GIB).to_bytes(8, "little"), kind=REG_BINARY),
    })
    assert hardware._windows_registry_adapters() == [
        (IGPU, 512 * 1024 ** 2), (RADEON_16, 16 * GIB), ("Some Binary Card", 24 * GIB)]


def test_the_old_32_bit_field_is_used_only_when_the_64_bit_one_is_missing(registry):
    registry(**{
        "0000": adapter("Old Card", 2 * GIB, field="HardwareInformation.MemorySize", kind=REG_DWORD),
        "0001": FakeKey(values={"DriverDesc": ("Both", REG_SZ), "HardwareInformation.qwMemorySize": (12 * GIB, REG_QWORD),
                                "HardwareInformation.MemorySize": (4 * GIB - 1, REG_DWORD)}),
        "0002": FakeKey(values={"DriverDesc": ("No size", REG_SZ)}),
    })
    assert hardware._windows_registry_adapters() == [("Old Card", 2 * GIB), ("Both", 12 * GIB)]


def test_a_machine_without_winreg_or_a_readable_key_has_no_registry_adapters(monkeypatch):
    monkeypatch.setitem(sys.modules, "winreg", None)             # import winreg -> ImportError, as on Linux
    assert hardware._windows_registry_adapters() == []
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg(FakeKey()))
    monkeypatch.setattr(sys.modules["winreg"], "OpenKey", lambda *a: (_ for _ in ()).throw(PermissionError("denied")))
    assert hardware._windows_registry_adapters() == []


@pytest.mark.parametrize("asked, expected", [
    ("AMD Radeon RX 6800 XT", 16 * GIB),
    ("AMD Radeon(TM) RX 6800 XT", 16 * GIB),                      # the registry and Vulkan spell it differently
    ("AMD Radeon RX 6800 XT (RDNA2)", 16 * GIB),
    ("AMD Radeon(TM) Graphics", 512 * 1024 ** 2),
    ("NVIDIA GeForce RTX 4090", None),
    ("", None),
])
def test_an_adapter_is_found_by_its_name(asked, expected):
    adapters = [(IGPU, 512 * 1024 ** 2), ("AMD Radeon(R) RX 6800 XT", 16 * GIB)]
    assert hardware._adapter_bytes(asked, adapters) == expected


# ---- a whole probe on a fake Windows -----------------------------------------------------

def win32_video_controller(name, adapter_ram):
    return json.dumps({"DeviceID": "VideoController1", "Name": name, "AdapterRAM": adapter_ram})


@pytest.fixture
def windows_pc(monkeypatch):
    """_probe_all_gpus on Windows: no nvidia-smi; vulkaninfo and the CIM query answer as given."""
    def install(vulkaninfo=None, cim=None):
        calls = []

        def run(argv, **kwargs):
            calls.append(list(argv))
            tool = argv[0]
            if tool == "vulkaninfo" and vulkaninfo is not None:
                return SimpleNamespace(returncode=0, stdout=vulkaninfo, stderr="")
            if tool == "powershell" and cim is not None and "Win32_VideoController" in argv[-1]:
                return SimpleNamespace(returncode=0, stdout=cim, stderr="")
            raise FileNotFoundError(tool)

        monkeypatch.setattr(hardware.subprocess, "run", run)
        monkeypatch.setattr(hardware.platform, "system", lambda: "Windows")
        return calls

    return install


def vulkan_summary(*names):
    return "\n".join(f"GPU{i}:\n    deviceName         = {name}\n    deviceType         = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU"
                     for i, name in enumerate(names))


def test_a_16_gb_radeon_reports_16_not_the_4_gb_adapterram_cap(windows_pc, registry):
    registry(**{"0000": adapter(RADEON_16, 16 * GIB)})
    windows_pc(cim=win32_video_controller(RADEON_16, 4293918720))     # what AdapterRAM says for any card over 4 GB
    gpus, live = hardware._probe_all_gpus()
    assert live is False
    assert [(g["name"], g["vram_total_gb"], g["vram_free_gb"]) for g in gpus] == [(RADEON_16, 16.0, 16.0)]


def test_the_registry_is_read_even_when_vulkaninfo_is_installed_and_each_card_gets_its_own_size(windows_pc, registry):
    registry(**{"0000": adapter(IGPU, 512 * 1024 ** 2), "0001": adapter(RADEON_16, 16 * GIB)})
    windows_pc(vulkaninfo=vulkan_summary(RADEON_16, IGPU))
    gpus, _ = hardware._probe_all_gpus()
    assert [(g["name"], g["vram_total_gb"]) for g in gpus] == [(RADEON_16, 16.0), (IGPU, 0.5)]


def test_a_card_the_registry_does_not_name_gets_the_largest_size_as_before(windows_pc, registry):
    registry(**{"0000": adapter(RADEON_16, 16 * GIB)})
    windows_pc(vulkaninfo=vulkan_summary("AMD Radeon Mystery Edition"))
    gpus, _ = hardware._probe_all_gpus()
    assert gpus[0]["vram_total_gb"] == 16.0


def test_adapterram_is_only_the_last_fallback(windows_pc, monkeypatch):
    monkeypatch.setitem(sys.modules, "winreg", None)                  # no registry to read
    windows_pc(cim=win32_video_controller("Intel(R) Arc(TM) A380", 4293918720))
    gpus, _ = hardware._probe_all_gpus()
    assert gpus[0]["vram_total_gb"] == 4.0


def test_a_card_missing_from_the_registry_falls_back_to_adapterram_for_that_card(windows_pc, registry):
    registry(**{"0000": adapter(RADEON_16, 16 * GIB)})
    windows_pc(cim=json.dumps([{"Name": RADEON_16, "AdapterRAM": 4293918720}, {"Name": "Basic Display Adapter", "AdapterRAM": 1 * GIB}]))
    gpus, _ = hardware._probe_all_gpus()
    assert [(g["name"], g["vram_total_gb"]) for g in gpus] == [(RADEON_16, 16.0), ("Basic Display Adapter", 1.0)]


# ======================================================================================
# Is the owner busy?
# ======================================================================================

class Process:
    def __init__(self, pid, cpu=0.0):
        self.pid, self.cpu = pid, cpu

    def cpu_percent(self, interval=None):
        return self.cpu


@pytest.fixture
def machine(monkeypatch):
    """A machine for is_gaming_or_heavy_load: quiet CPU, our llama-server (pid 100) running, an NVIDIA card whose
    total load is ours. Returns an object whose attributes the tests change."""
    state = SimpleNamespace(system="Linux", notification=5, hresult=0, pmon=None, pmon_rc=0, llama=[Process(100)],
                            cpu=5.0, gpu_type="nvidia-12", pmon_calls=[])
    monkeypatch.setattr(hardware, "_collect_llama_processes", lambda: state.llama)
    monkeypatch.setattr(hardware.psutil, "cpu_percent", lambda interval=None: state.cpu)
    monkeypatch.setattr(hardware.psutil, "cpu_count", lambda logical=True: 8)
    monkeypatch.setattr(hardware.platform, "system", lambda: state.system)
    monkeypatch.setattr(hardware, "get_gpu_type", lambda: state.gpu_type)

    def shell_query(reference):
        reference._obj.value = state.notification
        return state.hresult

    shell_query.argtypes = shell_query.restype = None
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(shell32=SimpleNamespace(SHQueryUserNotificationState=shell_query)),
                        raising=False)

    def run(argv, **kwargs):
        if argv[:2] == ["nvidia-smi", "pmon"]:
            state.pmon_calls.append(list(argv))
            if state.pmon is None:
                raise FileNotFoundError("nvidia-smi")
            return SimpleNamespace(returncode=state.pmon_rc, stdout=state.pmon, stderr="")
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(hardware.subprocess, "run", run)
    return state


BUSY_CARD = {"has_gpu": True, "load_percent": 95.0, "vram_total_gb": 12, "vram_free_gb": 6, "name": "NVIDIA GeForce RTX 4070"}
NO_GPU = {"has_gpu": False, "load_percent": 0.0, "vram_total_gb": 0, "vram_free_gb": 0, "name": None}

PMON_HEADER = "# gpu         pid   type     sm    mem    enc    dec    command\n# Idx           #    C/G      %      %      %      %    name\n"


def pmon(*rows):
    return PMON_HEADER + "".join(f"    {gpu}  {pid:>7}  {kind:>6}  {sm:>4}     10      0      0    {name}\n"
                                 for gpu, pid, kind, sm, name in rows)


# ---- Windows: the full-screen signal ------------------------------------------------------

def test_a_member_that_is_serving_with_no_game_does_not_yield(machine):
    machine.system = "Windows"
    machine.notification = 5                      # QUNS_ACCEPTS_NOTIFICATIONS: the desktop, nothing full-screen
    machine.pmon = None                           # no per-process figures (WDDM)
    assert hardware.is_gaming_or_heavy_load(BUSY_CARD) is False, "the card is 95% busy, all of it ours"


@pytest.mark.parametrize("state, yields", [
    (1, False),    # QUNS_NOT_PRESENT
    (2, True),     # QUNS_BUSY
    (3, True),     # QUNS_RUNNING_D3D_FULL_SCREEN
    (4, False),    # QUNS_PRESENTATION_MODE
    (5, False),    # QUNS_ACCEPTS_NOTIFICATIONS
    (6, False),    # QUNS_QUIET_TIME
    (7, False),    # QUNS_APP
])
def test_a_full_screen_game_makes_a_serving_member_yield(machine, state, yields):
    machine.system = "Windows"
    machine.notification = state
    assert hardware.is_gaming_or_heavy_load(BUSY_CARD) is yields


def test_the_full_screen_signal_works_without_a_gpu_reading_and_without_llama_running(machine):
    machine.system = "Windows"
    machine.notification = 3
    machine.llama = []
    assert hardware.is_gaming_or_heavy_load(NO_GPU) is True


def test_a_failing_shell_call_is_not_a_reason_to_yield(machine, monkeypatch):
    machine.system = "Windows"
    machine.notification = 3
    machine.hresult = 0x80004005                 # E_FAIL
    assert hardware.is_gaming_or_heavy_load(BUSY_CARD) is False
    monkeypatch.delattr(ctypes, "windll")        # and no windll at all
    assert hardware._windows_fullscreen_app_running() is False


def test_the_full_screen_signal_is_never_asked_off_windows(machine, monkeypatch):
    machine.notification = 3
    monkeypatch.delattr(ctypes, "windll")
    for system in ("Linux", "Darwin"):
        machine.system = system
        assert hardware._windows_fullscreen_app_running() is False


# ---- NVIDIA: another process's share of the card ------------------------------------------

def test_load_from_another_process_makes_a_serving_member_yield(machine):
    machine.pmon = pmon((0, 100, "C", 90, "llama-server"), (0, 4242, "C+G", 85, "game.exe"))
    assert hardware.is_gaming_or_heavy_load(BUSY_CARD) is True
    assert machine.pmon_calls and machine.pmon_calls[0][:2] == ["nvidia-smi", "pmon"]


def test_our_own_load_alone_does_not_make_the_member_yield(machine):
    machine.pmon = pmon((0, 100, "C", 97, "llama-server"))
    assert hardware.is_gaming_or_heavy_load(BUSY_CARD) is False


def test_a_light_load_from_another_process_does_not(machine):
    machine.pmon = pmon((0, 100, "C", 90, "llama-server"), (0, 4242, "C+G", 20, "browser.exe"))
    assert hardware.is_gaming_or_heavy_load(BUSY_CARD) is False


def test_each_card_is_judged_on_its_own(machine):
    machine.pmon = pmon((0, 100, "C", 90, "llama-server"), (0, 4242, "G", 40, "a.exe"), (1, 4243, "G", 40, "b.exe"))
    assert hardware.is_gaming_or_heavy_load(BUSY_CARD) is False, "40% on each card is not 80% on one"
    machine.pmon = pmon((0, 100, "C", 90, "llama-server"), (1, 4243, "G", 75, "b.exe"))
    assert hardware.is_gaming_or_heavy_load(BUSY_CARD) is True


def test_where_the_driver_gives_no_per_process_figures_the_member_relies_on_the_other_signals(machine):
    machine.pmon = pmon((0, 100, "C", "-", "llama-server"), (0, 4242, "C+G", "-", "game.exe"))
    assert hardware._external_nvidia_load([100]) is None
    assert hardware.is_gaming_or_heavy_load(BUSY_CARD) is False
    machine.pmon_rc = 1
    assert hardware._external_nvidia_load([100]) is None
    machine.pmon = None                                         # no nvidia-smi at all
    assert hardware._external_nvidia_load([100]) is None


def test_a_card_nobody_uses_is_not_busy(machine):
    machine.pmon = PMON_HEADER
    assert hardware._external_nvidia_load([100]) == 0.0
    assert hardware.is_gaming_or_heavy_load(BUSY_CARD) is False


def test_other_vendors_are_not_asked_for_nvidia_figures(machine):
    machine.gpu_type = "vulkan"
    machine.pmon = pmon((0, 4242, "G", 99, "game.exe"))
    assert hardware.is_gaming_or_heavy_load({**BUSY_CARD, "name": "AMD Radeon RX 6800 XT"}) is False
    assert machine.pmon_calls == []


# ---- what did not change ------------------------------------------------------------------

def test_with_no_llama_running_a_hot_gpu_is_still_someone_elses(machine):
    machine.llama = []
    assert hardware.is_gaming_or_heavy_load(BUSY_CARD) is True
    assert hardware.is_gaming_or_heavy_load({**BUSY_CARD, "load_percent": 30.0}) is False
    assert machine.pmon_calls == [], "nothing of ours is running: no per-process question"


def test_the_cpu_signal_is_unchanged_with_our_share_subtracted(machine):
    machine.cpu = 70.0
    machine.llama = [Process(100, cpu=0.0)]
    assert hardware.is_gaming_or_heavy_load(NO_GPU) is True
    machine.llama = [Process(100, cpu=8 * 40.0)]                # llama-server explains 40 of the 70 points
    assert hardware.is_gaming_or_heavy_load(NO_GPU) is False


def test_the_answer_is_still_a_plain_bool(machine):
    for state in (2, 5):
        machine.system, machine.notification = "Windows", state
        assert isinstance(hardware.is_gaming_or_heavy_load(BUSY_CARD), bool)
