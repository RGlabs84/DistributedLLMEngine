"""
Hardware probing for DLNE worker nodes.

``get_hardware_profile()`` is called on *every* worker heartbeat (see
``engine/worker.py``), which means anything expensive in this module is paid several
times a minute per node, and the heartbeat loop is what keeps a worker alive in the
Coordinator's registry. Two things in here used to make that far more expensive than it
needed to be (bug #29):

  * ``psutil.cpu_percent(interval=0.5)`` blocked the calling thread for half a second on
    every single heartbeat. It is now the non-blocking ``interval=None`` form, primed at
    import, which measures usage across the gap between heartbeats instead.
  * physical core detection shelled out to ``wmic``, which Microsoft removed from
    Windows 11 24H2. Detection now prefers a PowerShell CIM query, then ``psutil``, and
    only falls back to ``wmic`` for older systems — and the answer is cached, because
    the number of cores in a machine does not change while it is running.

Audit item 3.5 closed the rest of that hole. Even after the 0.5 s sleep was removed, a
single heartbeat still spawned one to three subprocesses: ``get_gpu_info()`` and
``get_gpu_type()`` each probed the GPU independently, and ``is_gaming_or_heavy_load()``
called ``get_gpu_info()`` a *second* time. On a GPU-less Windows node — where every probe
has to walk its whole fallback chain, ending in a PowerShell CIM query — that cost more
wall-clock than the sleep it replaced. Static facts (GPU identity, vendor, core counts)
are now cached; only genuinely dynamic values (CPU load, NVIDIA utilisation and free VRAM)
are re-measured, and ``get_hardware_profile()`` probes the GPU exactly once per heartbeat
and shares that one result.

The shape of the dict returned by ``get_hardware_profile()`` is part of the wire
protocol: ``engine/registry.py`` and ``engine/coordinator.py`` both destructure it, so
keys are never renamed, removed or re-nested here.
"""

import psutil
import platform
import os
import queue
import re
import subprocess
import threading
import time
import GPUtil
from typing import Dict, Any, List, Optional, Tuple
from engine.config import get_worker_limits
from engine.logger import log_warning


def _hidden_process_kwargs() -> dict:
    """
    Extra ``subprocess`` keyword arguments that suppress the console window on Windows.

    DLNE ships as a windowed executable, so without this flag every probe below flashes
    a console box on screen.
    """
    if platform.system() == "Windows" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def get_system_ram() -> float:
    """Returns total system RAM in GB, capped by config limits."""
    actual_ram = round(psutil.virtual_memory().total / (1024**3), 1)
    limit = get_worker_limits().get('max_ram_gb', -1.0)
    if limit >= 0:
        return min(actual_ram, limit)
    return actual_ram


# --------------------------------------------------------------------------------------
# Physical core detection
# --------------------------------------------------------------------------------------

# Detection can spawn a PowerShell process, which costs a few hundred milliseconds. The
# physical core count of a running machine is immutable, so it is probed once and reused
# for the lifetime of the process; the configured cap is still applied on every call so a
# live config edit still takes effect.
_cached_physical_cores: Optional[int] = None


def _windows_cores_via_cim() -> int:
    """
    Physical core count via PowerShell CIM (the supported replacement for ``wmic``).

    Summing over Win32_Processor instances is what makes this correct on multi-socket
    machines, where ``NumberOfCores`` is reported per package.
    """
    ps_cmd = "(Get-CimInstance -ClassName Win32_Processor | Measure-Object -Property NumberOfCores -Sum).Sum"
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=20, **_hidden_process_kwargs()
        )
        value = result.stdout.strip()
        if value.isdigit():
            return int(value)
    except Exception:
        # PowerShell missing, blocked by policy, or timed out — the caller has fallbacks.
        pass
    return 0


def _windows_cores_via_wmic() -> int:
    """
    Physical core count via the legacy ``wmic`` tool.

    Kept only for Windows 8/10 and early 11 builds that predate a usable PowerShell CIM
    stack; ``wmic`` is deprecated and absent from current Windows 11 installs, so this is
    strictly the last resort.
    """
    total = 0
    try:
        result = subprocess.run(
            ["wmic", "cpu", "get", "NumberOfCores", "/value"],
            capture_output=True, text=True, timeout=20, **_hidden_process_kwargs()
        )
        for line in result.stdout.split('\n'):
            if "NumberOfCores=" in line:
                value = line.split('=', 1)[1].strip()
                if value.isdigit():
                    total += int(value)
    except Exception:
        pass
    return total


def _detect_physical_cores() -> int:
    """
    Determines the number of physical CPU cores, trying the cheapest reliable source first.

    ``psutil.cpu_count(logical=False)`` is correct on Linux/macOS and on most Windows
    machines, but it can return None on unusual Windows configurations — which is why the
    Windows path asks the OS directly first and keeps two more fallbacks behind it.
    """
    global _cached_physical_cores
    if _cached_physical_cores is not None:
        return _cached_physical_cores

    cores = 0
    if platform.system() == "Windows":
        cores = _windows_cores_via_cim()

    if not cores:
        cores = psutil.cpu_count(logical=False) or 0

    if not cores and platform.system() == "Windows":
        cores = _windows_cores_via_wmic()

    if not cores:
        # Last resort: logical threads. Over-reporting cores is better than reporting 0,
        # which would make the Coordinator treat this node as having no CPU at all.
        cores = psutil.cpu_count(logical=True) or 1

    _cached_physical_cores = cores
    return cores


def get_cpu_cores() -> int:
    """Returns number of physical silicon CPU cores, capped by config limits."""
    actual_cores = _detect_physical_cores()

    limit = get_worker_limits().get('max_cpu_threads', -1)
    if limit >= 0:
        return min(actual_cores, limit)
    return actual_cores


# --------------------------------------------------------------------------------------
# GPU probing
# --------------------------------------------------------------------------------------

# The vendor/runtime a machine has does not change while DLNE is running, and determining
# it costs a GPUtil enumeration plus up to one subprocess. Probed once, then reused.
_cached_gpu_type: Optional[str] = None

# How long a GPU enumeration stays valid.
#
# Two TTLs because the two probe paths have completely different characters. nvidia-smi is
# a single cheap call that reports *live* free VRAM and utilisation, so it is refreshed
# often enough to stay meaningful while still collapsing the several calls a single
# heartbeat makes into one. Every other path (vulkaninfo, Win32_VideoController, clinfo,
# system_profiler) is a slow identity lookup that reports a constant 0.0 load and a fixed
# VRAM size — nothing in it can change, so it is held for minutes. The "no GPU at all"
# verdict is the expensive one and falls into this second bucket.
_GPU_LIVE_TTL_SECONDS: float = 2.0
_GPU_STATIC_TTL_SECONDS: float = 300.0

_gpu_cache: Optional[List[Dict[str, Any]]] = None
_gpu_cache_expiry: float = 0.0
# Held across the probe itself, so a burst of callers costs one subprocess rather than one
# each. Nothing inside the probe re-enters this module's GPU functions, so it cannot
# deadlock; the Darwin branch reaches get_system_ram(), which only takes the config lock.
_gpu_cache_lock = threading.Lock()


# /sys/class/drm holds one entry per DRM device the kernel bound a driver to, alongside
# per-connector entries ("card0-DP-1") and render nodes ("renderD128"). Only the bare
# card entries are devices.
_DRM_CARD_PATTERN = re.compile(r"^card\d+$")

# Module-level so tests can point the probe at a synthetic sysfs tree; nothing else
# should reassign it.
_DRM_ROOT = "/sys/class/drm"

# PCI vendor IDs, as /sys reports them.
_PCI_VENDOR_AMD = "0x1002"
_PCI_VENDOR_INTEL = "0x8086"
_PCI_VENDOR_NVIDIA = "0x10de"

# Vulkan device names that are not hardware. A CPU rasteriser answering vulkaninfo is the
# single most dangerous false positive here: the node would advertise GPU capability to the
# coordinator and then run inference on the CPU at a fraction of the promised speed.
_SOFTWARE_VULKAN_DEVICES = ("llvmpipe", "swiftshader", "lavapipe", "software rasterizer")

# Integrated GPUs share the CPU's memory bus and are slower than the cards in the GPU pool,
# so they do not make a GPU contributor (the owner's rule, 2.3.0): such a machine contributes
# its CPU to the phone pool instead. The exceptions are the integrated GPUs fast enough to
# hold their own: Intel Arc 140V/140T, AMD Strix Halo (Radeon 8060S/8050S/8040S, Mesa's
# GFX1151) and Apple Silicon (Metal, unified memory).
_ALLOWED_INTEGRATED = re.compile(r"arc\W*(?:\(tm\)\W*)?140[vt]\b|radeon\W*80[456]0s\b|gfx1151|strix[ _]halo|"
                                 r"ryzen\W*ai\W*max|\bapple\b")
# Mesa's code names in "(RADV ...)": the ones that name an APU's integrated GPU.
_INTEGRATED_CODENAMES = re.compile(r"\((?:radv|anv)?\s*(?:raven2?|picasso|renoir|lucienne|cezanne|barcelo|rembrandt|"
                                   r"mendocino|raphael\w*|phoenix\w*|hawk\w*|strix\w*|krackan\w*|vangogh|"
                                   r"gfx90c|gfx1035|gfx1036|gfx1037|gfx1103|gfx1150|gfx1152|"
                                   r"adl|rpl|mtl|lnl|arl|tgl|icl|cml|kbl|skl|ehl|jsl)\b")
# Names that only integrated GPUs carry (Windows and lspci spell them without a code name).
_INTEGRATED_NAMES = re.compile(r"uhd graphics|iris|intel\(r\) hd graphics|intel.* graphics$|\bvega \d+\b|"
                               r"radeon\(tm\) graphics$|radeon graphics$|radeon \d{3}m\b|adreno|"
                               r"arc\W*(?:\(tm\)\W*)?(?:130v|graphics)\b")
# Discrete parts whose names could otherwise look integrated ("AMD Radeon Graphics (RADV NAVI12)").
_DISCRETE_NAMES = re.compile(r"\bnavi\s?\d|polaris|vega1[02]|gfx1[12]0\d|gfx9[04]\d|radeon\W+(?:rx|pro|vii)|"
                             r"instinct|arc\W*(?:\(tm\)\W*)?(?:a|b)\d{3}|arc\W*(?:\(tm\)\W*)?pro\b|geforce|quadro|rtx|tesla")


def counts_as_gpu(name: str, integrated: Optional[bool] = None) -> bool:
    """
    Whether a GPU makes its machine a GPU contributor. ``integrated`` is Vulkan's own answer
    when known (vulkaninfo's deviceType), which beats any name: TheWall's discrete Navi12
    cards are called "AMD Radeon Graphics (RADV NAVI12)". Without it the name decides, and an
    unknown name counts, so no real graphics card is ever turned away by a guess.
    """
    lowered = (name or "").lower()
    if _ALLOWED_INTEGRATED.search(lowered):
        return True
    if integrated is not None:
        return not integrated
    if _DISCRETE_NAMES.search(lowered):
        return True
    return not (_INTEGRATED_CODENAMES.search(lowered) or _INTEGRATED_NAMES.search(lowered))


def _vulkan_device_types(summary: str) -> List[bool]:
    """``integrated`` for each hardware device in ``vulkaninfo --summary`` output, in order
    (software rasterisers skipped, matching the device list the probes build)."""
    types: List[bool] = []
    pending: Optional[bool] = None
    for line in summary.splitlines():
        line = line.strip()
        if line.startswith("deviceType") and "=" in line:
            pending = "INTEGRATED" in line.split("=", 1)[1]
        elif line.startswith("deviceName") and "=" in line:
            name = line.split("=", 1)[1].strip().lower()
            if not any(token in name for token in _SOFTWARE_VULKAN_DEVICES):
                types.append(bool(pending))
            pending = None
    return types


def _read_sysfs(path: str) -> str:
    """Contents of a sysfs attribute, or "" when it is absent or unreadable."""
    try:
        with open(path, "r") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _lspci_device_names() -> Dict[str, str]:
    """
    ``PCI address -> human-readable name``, from ``lspci -mm``.

    Returns ``{}`` when lspci is not installed, which is normal on a minimal server image.
    The sysfs probe below therefore treats this as decoration, never as the enumeration.
    """
    names: Dict[str, str] = {}
    try:
        result = subprocess.run(["lspci", "-mm"], capture_output=True, text=True,
                                **_hidden_process_kwargs())
        if result.returncode != 0:
            return names
    except Exception:
        return names

    for line in result.stdout.splitlines():
        # 03:00.0 "Display controller" "Advanced Micro Devices..." "Device 7362" -r00 "" ""
        fields = re.findall(r'"([^"]*)"', line)
        address = line.split(" ", 1)[0].strip()
        if len(fields) >= 3 and address:
            vendor, device = fields[1].strip(), fields[2].strip()
            names[address] = f"{vendor} {device}".strip()
    return names


def _probe_linux_drm_cards() -> List[Dict[str, Any]]:
    """
    Enumerates GPUs straight from ``/sys/class/drm``.

    This exists because every other Linux path here depends on a userspace package that a
    compute box has no reason to have installed. ``vulkaninfo`` comes from vulkan-tools and
    ``clinfo`` from an OpenCL runtime; a headless rig — a server image, a mining card with
    no display outputs — routinely has neither, and DLNE then reported "no GPU" on a
    machine holding several. The kernel populates this tree for any card it bound a driver
    to, whether or not anything is plugged into it, so it answers on hardware the other
    probes cannot see.

    ``lspci`` is consulted only to put a readable name on each card. When it is missing the
    card is still enumerated, under its PCI ID.

    Returns:
        One entry per card, in ``/sys/class/drm`` order, each carrying the same keys as the
        other probes plus ``vendor_id`` and ``driver`` for :func:`_detect_gpu_type`.
    """
    drm_root = _DRM_ROOT
    if not os.path.isdir(drm_root):
        return []

    try:
        entries = sorted(name for name in os.listdir(drm_root) if _DRM_CARD_PATTERN.match(name))
    except OSError:
        return []

    pci_names = _lspci_device_names() if entries else {}
    cards: List[Dict[str, Any]] = []

    for index, entry in enumerate(entries):
        device_dir = os.path.join(drm_root, entry, "device")
        vendor_id = _read_sysfs(os.path.join(device_dir, "vendor")).lower()
        if not vendor_id:
            # No PCI vendor attribute means this is not a PCI graphics device: simpledrm,
            # vkms and the other virtual DRM providers land here and must not be counted.
            continue

        device_id = _read_sysfs(os.path.join(device_dir, "device")).lower()

        # amdgpu publishes VRAM size in bytes. Other drivers may not, and 0.0 is honest:
        # get_gpu_info's caller decides what an unknown size is worth.
        vram_bytes = _read_sysfs(os.path.join(device_dir, "mem_info_vram_total"))
        try:
            vram_gb = round(int(vram_bytes) / (1024 ** 3), 1) if vram_bytes else 0.0
        except ValueError:
            vram_gb = 0.0

        driver = ""
        try:
            driver = os.path.basename(os.path.realpath(os.path.join(device_dir, "driver")))
        except OSError:
            pass

        # The symlink target's basename is the PCI address, e.g. "0000:03:00.0"; lspci
        # prints the same address without the domain.
        address = os.path.basename(os.path.realpath(device_dir))
        name = pci_names.get(address) or pci_names.get(address.split(":", 1)[-1])
        if not name:
            name = f"{driver or 'GPU'} device {vendor_id}:{device_id}".strip()

        pci_bus = None
        try:
            parts = address.split(":")
            if len(parts) >= 2:
                pci_bus = int(parts[-2], 16)
        except Exception:
            pass

        cards.append({
            "id": index,
            "name": name,
            "vram_total_gb": vram_gb,
            "vram_free_gb": vram_gb,
            "load_percent": 0.0,
            "vendor_id": vendor_id,
            "driver": driver,
            "pci_bus": pci_bus,
            "address": address,
        })

    return cards


def get_gpu_type() -> str:
    """
    Detects the GPU vendor and capabilities (e.g., nvidia-12, nvidia-11, vulkan, cpu).

    Cached for the lifetime of the process: this is a property of the installed hardware
    and drivers, and re-deriving it on every heartbeat cost a subprocess for an answer
    that cannot have changed. A machine that gains a GPU needs DLNE restarted anyway,
    because the engine binaries selected at startup depend on this same value.
    """
    global _cached_gpu_type
    if _cached_gpu_type is not None:
        return _cached_gpu_type

    _cached_gpu_type = _detect_gpu_type()
    return _cached_gpu_type


def _detect_gpu_type() -> str:
    """Performs the actual vendor detection behind :func:`get_gpu_type`'s cache."""
    os_name = platform.system()

    try:
        gpus = GPUtil.getGPUs()
        if gpus:
            try:
                result = subprocess.run(["nvidia-smi"], capture_output=True, text=True, **_hidden_process_kwargs())
                if "CUDA Version: 11" in result.stdout: return "nvidia-11"
                return "nvidia-12"
            except FileNotFoundError:
                return "nvidia-12"
    except Exception: pass

    try:
        if os_name == "Windows":
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name"],
                capture_output=True, text=True, **_hidden_process_kwargs()
            )
            output = result.stdout.lower()
            if "amd" in output or "radeon" in output or "intel" in output or "arc" in output:
                return "vulkan"
        elif os_name == "Linux":
            # The kernel's own view first. The lspci scan below only matched lines reading
            # "VGA compatible controller" or "3D controller", and a card with no display
            # outputs reports neither — it is a "Display controller" — so a rig built out
            # of compute cards was classified as CPU-only and never loaded a GPU backend.
            for card in _probe_linux_drm_cards():
                if card["vendor_id"] in (_PCI_VENDOR_AMD, _PCI_VENDOR_INTEL):
                    return "vulkan"
                if card["vendor_id"] == _PCI_VENDOR_NVIDIA and card["driver"] == "nouveau":
                    # nouveau gives no CUDA; Vulkan is the only backend it can serve.
                    return "vulkan"

            result = subprocess.run(["lspci"], capture_output=True, text=True)
            output = result.stdout.lower()
            lines = [line for line in output.split('\n')
                     if 'vga' in line or '3d' in line or 'display controller' in line]
            for line in lines:
                if "amd" in line or "radeon" in line or "intel" in line:
                    return "vulkan"
        elif os_name == "Darwin" and get_cpu_arch() == "arm64":
            # Only Apple Silicon: the Intel Mac build of llama.cpp has no Metal backend.
            result = subprocess.run(["system_profiler", "SPDisplaysDataType"], capture_output=True, text=True)
            if "Apple" in result.stdout and "Metal" in result.stdout:
                return "metal"
    except Exception: pass

    return "cpu"

# The registry key where Windows keeps one numbered subkey ("0000", "0001", ...) per display
# adapter. The driver writes the card's memory size there as a 64-bit number, which is the only
# honest figure on Windows: Win32_VideoController.AdapterRAM is 32 bits wide and reads 4 GB for
# every card that has more, and vulkaninfo (which would say) is not installed on most PCs.
_DISPLAY_CLASS_KEY = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"


def _registry_size(value: Any) -> int:
    """A memory size as drivers write it: a number (REG_QWORD / REG_DWORD) or little-endian bytes (REG_BINARY)."""
    if isinstance(value, (bytes, bytearray)):
        return int.from_bytes(bytes(value), "little")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 0


def _windows_registry_adapters() -> List[Tuple[str, int]]:
    """
    ``(driver description, bytes of video memory)`` for each display adapter whose driver wrote its size
    to the registry (``HardwareInformation.qwMemorySize``, 64-bit; the older 32-bit
    ``HardwareInformation.MemorySize`` only when the 64-bit one is missing). Empty off Windows and when
    the registry cannot be read. Never raises.
    """
    try:
        import winreg
    except ImportError:
        return []
    found: List[Tuple[str, int]] = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _DISPLAY_CLASS_KEY) as root:
            index = 0
            while True:
                try:
                    sub = winreg.EnumKey(root, index)
                except OSError:
                    break           # past the last subkey
                index += 1
                if not sub.isdigit():
                    continue        # "Configuration", "Properties": not adapters
                try:
                    with winreg.OpenKey(root, sub) as adapter:
                        size = 0
                        for field in ("HardwareInformation.qwMemorySize", "HardwareInformation.MemorySize"):
                            try:
                                size = _registry_size(winreg.QueryValueEx(adapter, field)[0])
                            except OSError:
                                continue
                            if size > 0:
                                break
                        if size <= 0:
                            continue
                        try:
                            desc = str(winreg.QueryValueEx(adapter, "DriverDesc")[0])
                        except OSError:
                            desc = ""
                        found.append((desc, size))
                except OSError:
                    continue        # an adapter key this user cannot read
    except Exception:  # noqa: BLE001 - detection falls back to the next method
        return found
    return found


def _adapter_name_key(name: str) -> str:
    """A GPU name reduced to lower-case letters and digits, so "AMD Radeon(TM) RX 6800 XT" matches "AMD Radeon RX 6800 XT"."""
    return re.sub(r"[^a-z0-9]", "", re.sub(r"\((?:tm|r)\)", "", str(name).lower()))


def _adapter_bytes(name: str, adapters: List[Tuple[str, int]]) -> Optional[int]:
    """The registry's memory size for the adapter called ``name``: an exact match first, then one name containing the other."""
    wanted = _adapter_name_key(name)
    if not wanted:
        return None
    keyed = [(_adapter_name_key(desc), size) for desc, size in adapters]
    for key, size in keyed:
        if key and key == wanted:
            return size
    for key, size in keyed:
        if key and len(min(key, wanted, key=len)) >= 6 and (key in wanted or wanted in key):
            return size
    return None


def get_all_gpus() -> List[Dict[str, Any]]:
    """
    Returns a raw list of all detected GPUs on the system, cached (see the TTLs above).

    Copies are handed out rather than the cached dicts themselves, because callers such as
    :func:`get_gpu_info` filter and rewrite the entries they are given, and a mutation must
    not be able to poison what the next caller sees.
    """
    global _gpu_cache, _gpu_cache_expiry

    with _gpu_cache_lock:
        now = time.monotonic()
        if _gpu_cache is not None and now < _gpu_cache_expiry:
            return [dict(gpu) for gpu in _gpu_cache]

        gpus, values_are_live = _probe_all_gpus()

        _gpu_cache = [dict(gpu) for gpu in gpus]
        _gpu_cache_expiry = now + (
            _GPU_LIVE_TTL_SECONDS if values_are_live else _GPU_STATIC_TTL_SECONDS
        )
        return [dict(gpu) for gpu in gpus]


def _probe_all_gpus() -> Tuple[List[Dict[str, Any]], bool]:
    """
    Enumerates the machine's GPUs, trying each detection method in turn.

    Returns:
        ``(gpus, values_are_live)``. The flag tells :func:`get_all_gpus` how long the
        answer may be cached: it is True only for the nvidia-smi path, which is the one
        source here that reports genuinely changing numbers (free VRAM, utilisation).
        Every other path returns a fixed identity with ``load_percent`` hardcoded to 0.0.
    """
    gpus = []

    # 1. NVIDIA via nvidia-smi
    try:
        result = subprocess.run(["nvidia-smi", "--query-gpu=index,memory.total,memory.free,utilization.gpu,name", "--format=csv,noheader,nounits"], capture_output=True, text=True, **_hidden_process_kwargs())
        if result.returncode == 0 and result.stdout.strip():
            lines = result.stdout.strip().split('\n')
            for line in lines:
                parts = [p.strip() for p in line.split(',')]
                if len(parts) >= 5:
                    idx = int(parts[0])
                    total_gb = round(int(parts[1]) / 1024, 1)
                    free_gb = round(int(parts[2]) / 1024, 1)
                    load = float(parts[3]) if parts[3].isdigit() else 0.0
                    gpus.append({"id": idx, "name": parts[4], "vram_total_gb": total_gb, "vram_free_gb": free_gb, "load_percent": load})
    except Exception: pass

    if gpus:
        # nvidia-smi is the one source whose numbers actually move between calls.
        return gpus, True

    # 2. Vulkan / WMI / clinfo
    os_name = platform.system()

    # Enumerated once and reused: it names the Vulkan path's VRAM sizes below, and it is
    # the fallback when no userspace probe is installed at all.
    drm_cards = _probe_linux_drm_cards() if os_name == "Linux" else []

    try:
        result = subprocess.run(["vulkaninfo", "--summary"], capture_output=True, text=True, **_hidden_process_kwargs())
        if result.returncode == 0:
            gpu_names = [line.split('=')[1].strip() for line in result.stdout.split('\n') if "deviceName" in line]
            # A CPU rasteriser is not a GPU. Counting llvmpipe here made the node promise
            # the coordinator acceleration it does not have.
            gpu_names = [name for name in gpu_names
                         if not any(token in name.lower() for token in _SOFTWARE_VULKAN_DEVICES)]
            if not gpu_names: raise ValueError()
            # Vulkan's own integrated/discrete verdict per device, in the same order.
            integrated = _vulkan_device_types(result.stdout)
            if len(integrated) != len(gpu_names):
                integrated = []

            gb = 0.0
            windows_adapters: List[Tuple[str, int]] = []
            if os_name == "Windows":
                # Each card's own size when its name is found in the registry, else the largest
                # (the old behaviour); see _windows_registry_adapters.
                windows_adapters = _windows_registry_adapters()
                gb = max((round(size / (1024**3), 1) for _, size in windows_adapters), default=0.0)
            elif len(drm_cards) == len(gpu_names):
                # Query vulkaninfo for PCI bus IDs so each sysfs card knows its corresponding
                # Vulkan device index (GGML_VK_VISIBLE_DEVICES index).
                bus_to_vk = {}
                try:
                    vk_full = subprocess.run(["vulkaninfo"], capture_output=True, text=True, **_hidden_process_kwargs())
                    if vk_full.returncode == 0:
                        v_idx = 0
                        v_name = None
                        v_bus = None
                        for line in vk_full.stdout.splitlines():
                            line = line.strip()
                            if line.startswith("deviceName") and "=" in line:
                                if v_name is not None and not any(t in v_name.lower() for t in _SOFTWARE_VULKAN_DEVICES):
                                    if v_bus is not None:
                                        bus_to_vk[v_bus] = v_idx
                                    v_idx += 1
                                    v_bus = None
                                v_name = line.split("=")[1].strip()
                            elif line.startswith("pciBus") and "=" in line:
                                try:
                                    v_bus = int(line.split("=")[1].strip())
                                except ValueError:
                                    pass
                        if v_name is not None and not any(t in v_name.lower() for t in _SOFTWARE_VULKAN_DEVICES):
                            if v_bus is not None:
                                bus_to_vk[v_bus] = v_idx
                except Exception:
                    pass

                result = []
                for card in drm_cards:
                    c = {key: card[key] for key in
                         ("id", "name", "vram_total_gb", "vram_free_gb", "load_percent", "address")}
                    pci_b = card.get("pci_bus")
                    if pci_b is not None and pci_b in bus_to_vk:
                        c["vk_index"] = bus_to_vk[pci_b]
                    else:
                        c["vk_index"] = card["id"]
                    if 0 <= c["vk_index"] < len(integrated):
                        c["integrated"] = integrated[c["vk_index"]]
                    result.append(c)
                return result, False
            else:
                # The counts disagree, so some card the kernel bound is one Vulkan refused
                # to open, and nothing here can tell which. Size the survivors from the
                # SMALLEST card found: this number is what the node advertises to the
                # coordinator, which hands out layers on the strength of it, so
                # over-reporting buys an out-of-memory kill mid-inference while
                # under-reporting only costs throughput.
                usable = [card["vram_total_gb"] for card in drm_cards if card["vram_total_gb"] > 0]
                gb = min(usable) if usable else 0.0

            for idx, name in enumerate(gpu_names):
                own = _adapter_bytes(name, windows_adapters)
                size_gb = round(own / (1024**3), 1) if own else gb
                gpu = {"id": idx, "name": name, "vram_total_gb": size_gb, "vram_free_gb": size_gb, "load_percent": 0.0}
                if idx < len(integrated):
                    gpu["integrated"] = integrated[idx]
                gpus.append(gpu)
            # A fixed identity with a constant 0.0 load: nothing here can change.
            return gpus, False
    except Exception: pass

    if os_name == "Windows" and not gpus:
        try:
            import json
            ps_cmd = "Get-CimInstance Win32_VideoController | Select-Object -Property DeviceID, Name, AdapterRAM | ConvertTo-Json"
            res = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd], capture_output=True, text=True, **_hidden_process_kwargs())
            if res.returncode == 0 and res.stdout.strip():
                data = json.loads(res.stdout)
                if isinstance(data, dict):
                    data = [data]
                adapters = _windows_registry_adapters()
                for idx, item in enumerate(data):
                    name = item.get("Name", "Unknown GPU")
                    # AdapterRAM is a 32-bit field: every card above 4 GB reads as 4 GB (a 16 GB Radeon
                    # would then fall under the coding member's 8 GB floor). The driver's own 64-bit
                    # figure in the registry comes first; AdapterRAM is the last resort.
                    ram_bytes = _adapter_bytes(name, adapters) or item.get("AdapterRAM") or 0
                    gb = round(ram_bytes / (1024**3), 1)
                    gpus.append({"id": idx, "name": name, "vram_total_gb": gb, "vram_free_gb": gb, "load_percent": 0.0})
        except Exception: pass

    if os_name == "Linux":
        try:
            result = subprocess.run(["clinfo"], capture_output=True, text=True)
            if result.returncode == 0 and "Device Name" in result.stdout:
                gpu_names = []
                gb = 0.0
                for line in result.stdout.split('\n'):
                    if "Device Name" in line:
                        gpu_names.append(line.split(':', 1)[1].strip())
                    if "Global memory size" in line and "bytes" in line:
                        try: gb = max(gb, round(int(line.split('(')[0].split()[-1].strip()) / (1024**3), 1))
                        except Exception: pass
                for idx, name in enumerate(list(set(gpu_names))):
                    gpus.append({"id": idx, "name": name, "vram_total_gb": gb, "vram_free_gb": gb, "load_percent": 0.0})
        except Exception: pass

        # Last resort, and the one that needs nothing installed. Reached when neither
        # vulkaninfo nor clinfo is present — the normal state of a freshly imaged compute
        # box, and previously the point at which DLNE declared a GPU rig CPU-only.
        if not gpus and drm_cards:
            gpus = [{key: card[key] for key in
                     ("id", "name", "vram_total_gb", "vram_free_gb", "load_percent", "address")}
                    for card in drm_cards]

    # Apple Silicon only. llama.cpp's Intel Mac build has no Metal backend, so an Intel Mac's
    # AMD GPU cannot be used and must not be reported: the Coordinator would plan around VRAM
    # the engine never touches. Such a Mac contributes its CPU.
    if os_name == "Darwin" and get_cpu_arch() == "arm64":
        try:
            # Apple Silicon (M1/M2/M3/M4) Unified Memory Architecture
            result = subprocess.run(["system_profiler", "SPDisplaysDataType"], capture_output=True, text=True)
            if "Apple" in result.stdout and "Metal" in result.stdout:
                name = "Apple Silicon (Metal)"
                for line in result.stdout.split('\n'):
                    if "Chipset Model:" in line:
                        name = line.split(':')[1].strip()
                        break
                # Unified memory means the GPU can dynamically access system RAM
                system_ram_gb = get_system_ram()
                # Apple typically allows up to ~75% of system RAM for GPU unified allocations
                unified_vram = round(system_ram_gb * 0.75, 1)
                gpus.append({"id": 0, "name": name, "vram_total_gb": unified_vram, "vram_free_gb": unified_vram, "load_percent": 0.0})
        except Exception: pass

    # Everything that reaches here — including the "no GPU at all" verdict, which is the
    # most expensive answer to produce on Windows — is static.
    return gpus, False

def find_gpu(spec: str, gpus: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    The GPU one ALLOWED_GPU_INDICES or MEDIA_GPU entry names, or None. The entry is a DLNE GPU id
    ("1") or a PCI address ("0000:03:00.0", or "03:00.0" without the domain). The id follows the
    order the kernel lists the cards in, so fitting another card can shift it; the address is where
    the card sits, and stays.

    A PCI address is known only where the probe reads it: on Linux, from sysfs, for the cards the Vulkan
    probe lists (AMD and Intel; nvidia-smi, clinfo, Windows and macOS give ids only). Elsewhere an address
    matches nothing, and the entry is left out (``engine_gpu_devices`` says so in the log); use the id.
    """
    spec = (spec or "").strip().lower()
    if spec.isdigit():
        return next((g for g in gpus if g.get("id") == int(spec)), None)
    if ":" in spec:
        return next((g for g in gpus
                     if (g.get("address") or "").lower() == spec
                     or (g.get("address") or "").lower().endswith(":" + spec)), None)
    return None


def select_gpus(setting: str, gpus: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The GPUs a comma-separated ALLOWED_GPU_INDICES value names, in the order written, each once.
    An entry that matches no card is skipped."""
    chosen: List[Dict[str, Any]] = []
    for spec in setting.split(","):
        gpu = find_gpu(spec, gpus)
        if gpu is not None and gpu not in chosen:
            chosen.append(gpu)
    return chosen


_dropped_warned: set = set()


def _warn_dropped(why: str) -> None:
    """One warning per ALLOWED_GPU_INDICES entry left out of the engine's cards, so a typo or a card that is
    not there is never silent (and a start every few minutes does not repeat it)."""
    if why not in _dropped_warned:
        _dropped_warned.add(why)
        log_warning(f"ALLOWED_GPU_INDICES entry left out, {why}.")


def engine_gpu_devices(allowed: str, media_spec: str = "", shared: bool = False) -> Tuple[str, str, str]:
    """
    The devices the coordinator's engine sees, as ``(cuda_ids, vulkan_devices, media_device)``.

    ``allowed`` is ALLOWED_GPU_INDICES (not "none"; ids and PCI addresses mix freely) and
    ``media_spec`` is MEDIA_GPU. With no limit ("-1") and no media card, all three are "": the engine
    is left to see what Vulkan gives it. The media card comes last in ``vulkan_devices``, after the engine's
    cards, so the engine's cards keep the llama.cpp names Vulkan0, Vulkan1... that the operator's
    ``-ts`` is written for; ``media_device`` is the name llama.cpp gives it (Vulkan<position in the
    list>), or "" when no media card is in use. A card named in both lists is the media card.

    ``shared`` is MEDIA_GPU_SHARED. The list is the same with it on: the media card is still last, now with a
    share of the model as well (the operator's ``-ts`` then has one more number, the last one, for it). Naming
    it in ALLOWED_GPU_INDICES is then no mistake, so that is not warned about.
    """
    try:
        gpus = get_all_gpus()
    except Exception:
        gpus = []

    media = find_gpu(media_spec, gpus)
    cuda: List[str] = []
    vulkan: List[str] = []
    named = allowed.strip() != "-1"          # the operator wrote the list, so a card left out of it is worth saying
    if not named:
        if media is None:
            return "", "", ""
        # Vulkan's own default is every dedicated GPU, which would include the media card, so with
        # one in use the list has to be written out.
        chosen = [g for g in gpus if counts_as_gpu(g.get("name") or "", g.get("integrated"))]
        tokens = [str(g["id"]) for g in chosen]
    else:
        tokens = [t.strip() for t in allowed.split(",") if t.strip()]
    seen: set = set()
    for token in tokens:
        gpu = find_gpu(token, gpus)
        if gpu is not None and media is not None and gpu["id"] == media["id"]:
            if named and not shared:
                _warn_dropped(f"{token}: it is the media card (MEDIA_GPU), which the engine never uses")
            continue                    # listed once, last, below
        if gpu is not None:
            # A card named twice (an index and its address, say) is one device: llama.cpp would split
            # the model across it twice, and CUDA refuses a repeated id.
            if gpu["id"] in seen:
                _warn_dropped(f"{token}: that card is already listed")
                continue
            seen.add(gpu["id"])
            cuda.append(str(gpu["id"]))
            vulkan.append(str(gpu.get("vk_index", gpu["id"])))
        elif token.isdigit():
            # A card the probe did not list (or no probe at all) is passed through as written.
            if ("raw", token) in seen:
                _warn_dropped(f"{token}: that card is already listed")
                continue
            seen.add(("raw", token))
            cuda.append(token)
            vulkan.append(token)
        else:
            _warn_dropped(f"{token}: it names no card here (a PCI address resolves only on Linux, from sysfs, for "
                          "the cards the Vulkan probe lists; elsewhere use the id)")

    media_device = ""
    if media is not None:
        media_device = f"Vulkan{len(vulkan)}"
        vulkan.append(str(media.get("vk_index", media["id"])))
    return ",".join(cuda), ",".join(vulkan), media_device


def media_card(media_spec: str) -> Optional[Dict[str, Any]]:
    """
    The media card (MEDIA_GPU) as image generation needs it: ``{"vk_index", "total_gb", "free_gb"}``, or None when
    ``media_spec`` names no card here. ``vk_index`` is the card's GLOBAL Vulkan device number, the one
    GGML_VK_VISIBLE_DEVICES takes (the same mapping engine_gpu_devices uses for the engine's own list). ``free_gb`` is
    read live from the card (amdgpu's sysfs counters beside its PCI address; nvidia-smi's figure otherwise) and None
    when no live figure exists: the probe's other paths report a size that never changes, which is not "free".
    """
    media = find_gpu(media_spec, get_all_gpus())
    if media is None:
        return None
    free_gb = None
    device_dir = os.path.join("/sys/bus/pci/devices", media.get("address") or "-")
    total, used = _read_sysfs(os.path.join(device_dir, "mem_info_vram_total")), _read_sysfs(os.path.join(device_dir, "mem_info_vram_used"))
    if total.isdigit() and used.isdigit():
        free_gb = max(0.0, (int(total) - int(used)) / (1024 ** 3))
    elif "address" not in media and media.get("vram_free_gb"):
        free_gb = float(media["vram_free_gb"])           # the nvidia-smi path: live
    return {"vk_index": int(media.get("vk_index", media["id"])), "total_gb": float(media.get("vram_total_gb") or 0.0),
            "free_gb": free_gb}


def get_gpu_info() -> Dict[str, Any]:
    """
    Returns a filtered summary dictionary for backwards compatibility.

    The enumeration underneath is cached; the config limits are re-applied on every call,
    so an operator editing ALLOWED_GPU_INDICES or MAX_VRAM_GB still takes effect on the
    very next heartbeat.
    """
    gpus = get_all_gpus()
    if not gpus:
        return {"has_gpu": False, "vram_total_gb": 0, "vram_free_gb": 0, "name": None, "load_percent": 0.0}

    limits = get_worker_limits()
    allowed = limits.get("allowed_gpu_indices", "-1").strip()
    vram_limits = limits.get("max_vram_gb", "-1").strip()

    if allowed.lower() == "none":
        return {"has_gpu": False, "vram_total_gb": 0, "vram_free_gb": 0, "name": None, "load_percent": 0.0}
    every_card = gpus
    if allowed != "-1":
        gpus = select_gpus(allowed, gpus)

    # The media card is not the engine's memory, so the pool this node advertises leaves it out. On a build
    # that is not Vulkan the engine ignores MEDIA_GPU (the card is the model's then), and so does this.
    # A shared media card (MEDIA_GPU_SHARED) is the model's too: it counts, whether or not ALLOWED_GPU_INDICES
    # names it, because the engine is given it either way (engine_gpu_devices).
    from engine.inference_manager import media_gpu_shared, media_gpu_spec
    media = find_gpu(media_gpu_spec(limits), gpus)
    if media is not None:
        gpus = [g for g in gpus if g["id"] != media["id"]]
    if media_gpu_shared(limits):
        shared_card = find_gpu(media_gpu_spec(limits), every_card)
        if shared_card is not None:
            gpus = gpus + [shared_card]

    # Integrated GPUs (other than the allowed few) are not GPU contributions: a machine with
    # only those contributes its CPU (counts_as_gpu).
    gpus = [g for g in gpus if counts_as_gpu(g.get("name") or "", g.get("integrated"))]

    if not gpus:
        return {"has_gpu": False, "vram_total_gb": 0, "vram_free_gb": 0, "name": None, "load_percent": 0.0}

    total_gb = sum(g["vram_total_gb"] for g in gpus)
    free_gb = sum(g["vram_free_gb"] for g in gpus)
    avg_load = sum(g["load_percent"] for g in gpus) / len(gpus)
    max_load = max(g["load_percent"] for g in gpus)       # the yield check judges the busiest card, not the average
    names = " + ".join(list(set(g["name"] for g in gpus)))

    # Apply VRAM limits if specified (e.g. "24,8" -> sum is 32)
    if vram_limits != "-1":
        try:
            limit_sum = sum(float(x.strip()) for x in vram_limits.split(","))
            total_gb = min(total_gb, limit_sum)
            free_gb = min(free_gb, limit_sum)
        except Exception: pass

    return {"has_gpu": True, "vram_total_gb": round(total_gb, 1), "vram_free_gb": round(free_gb, 1), "name": names,
            "load_percent": round(avg_load, 1), "load_max_percent": round(max_load, 1), "count": len(gpus)}


# --------------------------------------------------------------------------------------
# Load / "Gaming Mode" detection
# --------------------------------------------------------------------------------------

# psutil reports CPU usage as the delta between two reads of the *same* object, so the
# very first read of a counter is always a meaningless 0.0. Priming the system counter at
# import lets every later read use the non-blocking interval=None form and still return a
# real number — measured across the gap since the previous heartbeat rather than by
# sleeping for half a second inside the heartbeat itself (bug #29).
try:
    psutil.cpu_percent(interval=None)
except Exception:
    # Priming is an optimisation, never a hard requirement: a failure here at worst costs
    # one heartbeat's worth of 0.0 before the counter settles.
    pass

# The same delta rule applies per process, so the Process handles for the llama.cpp and
# RPC children must survive between heartbeats. Keyed by (pid, create_time) so a recycled
# PID can never be mistaken for the process we were previously tracking. Entries for
# processes that have exited are dropped on each sweep, so this cannot grow unbounded.
_tracked_llama_processes: Dict[Tuple[int, Any], psutil.Process] = {}

# Process names that belong to DLNE's own inference workload rather than to whatever the
# user is doing on their machine. 'rpc-server' also covers 'ggml-rpc-server'.
_LLAMA_PROCESS_TAGS = ("llama-server", "rpc-server")


def _own_process_ids() -> set:
    """
    This process and everything it started: the RPC server, a member's or the pool's llama-server, the
    probes (nvidia-smi, PowerShell), the bandwidth test. All of it is DLNE's own work, never the owner's,
    whatever the processes are called. Never raises.
    """
    try:
        me = psutil.Process(os.getpid())
        return {me.pid} | {child.pid for child in me.children(recursive=True)}
    except Exception:  # noqa: BLE001
        return {os.getpid()}


def _is_engine_process(proc: Any) -> bool:
    """Whether ``proc`` is an inference server (llama-server or an RPC server) rather than another process of
    ours. Unknown (a process that cannot be named) counts as one: the safe side is "DLNE is on the GPU"."""
    try:
        name = proc.info.get("name") if isinstance(getattr(proc, "info", None), dict) else None
        if name is None and callable(getattr(proc, "name", None)):
            name = proc.name()
    except Exception:  # noqa: BLE001
        name = None
    if not name:
        return True
    return any(tag in str(name).lower() for tag in _LLAMA_PROCESS_TAGS)


def _collect_llama_processes() -> List[psutil.Process]:
    """
    Returns live handles for DLNE's own processes, reusing them between calls: this process and its
    descendants (2.5.0: so the worker never yields to its own probes, downloads or bandwidth test), and,
    as a fallback for an engine started by someone else (a co-hosted coordinator), anything named like
    llama.cpp's servers.

    Reuse is what makes the non-blocking CPU measurement meaningful: a handle created in
    this call reports 0.0 (it has no previous read to diff against) and only starts
    contributing real numbers from the next heartbeat onward.
    """
    global _tracked_llama_processes

    own = _own_process_ids()
    current: Dict[Tuple[int, Any], psutil.Process] = {}
    for proc in psutil.process_iter(['name', 'create_time']):
        name = (proc.info.get('name') or "").lower()
        if proc.pid not in own and not any(tag in name for tag in _LLAMA_PROCESS_TAGS):
            continue

        key = (proc.pid, proc.info.get('create_time'))
        tracked = _tracked_llama_processes.get(key)
        if tracked is None:
            tracked = proc
            try:
                # Prime the freshly discovered process so its next read is a real delta.
                tracked.cpu_percent(interval=None)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        current[key] = tracked

    _tracked_llama_processes = current
    return list(current.values())


# SHQueryUserNotificationState's answers that mean the owner is busy with something full-screen:
# QUNS_BUSY (a full-screen application, or Presentation Settings are on) and
# QUNS_RUNNING_D3D_FULL_SCREEN (a Direct3D game). The others (not present, accepts notifications,
# quiet time, a Store app) are not.
_QUNS_BUSY = 2
_QUNS_RUNNING_D3D_FULL_SCREEN = 3


def _windows_fullscreen_app_running() -> bool:
    """
    Windows only: whether the shell reports a full-screen game or application
    (``SHQueryUserNotificationState``). The same question Windows asks before it shows a toast,
    answered for every GPU vendor. False elsewhere, and False when the call fails. Never raises.
    """
    if platform.system() != "Windows":
        return False
    try:
        import ctypes
        state = ctypes.c_int(0)
        query = ctypes.windll.shell32.SHQueryUserNotificationState
        query.argtypes = [ctypes.POINTER(ctypes.c_int)]
        query.restype = ctypes.c_long
        if query(ctypes.byref(state)) != 0:          # an HRESULT: 0 is S_OK
            return False
        return state.value in (_QUNS_BUSY, _QUNS_RUNNING_D3D_FULL_SCREEN)
    except Exception:  # noqa: BLE001 - no signal is not a reason to yield
        return False


def _external_nvidia_load(own_pids: List[int]) -> Optional[float]:
    """
    The highest GPU utilisation (percent of the card's compute) used by processes that are not ``own_pids``,
    per NVIDIA card, from ``nvidia-smi pmon`` (one sample, about a second). None when it cannot be told:
    no nvidia-smi, the command fails, or the driver gives no per-process figures (Windows' WDDM mode
    prints dashes). 0.0 when nothing else is using a card.
    """
    try:
        if not get_gpu_type().startswith("nvidia"):
            return None
        result = subprocess.run(["nvidia-smi", "pmon", "-c", "1", "-s", "u"], capture_output=True, text=True,
                                timeout=10, **_hidden_process_kwargs())
    except Exception:  # noqa: BLE001
        return None
    if result.returncode != 0:
        return None
    mine = set(own_pids)
    per_card: Dict[int, float] = {}
    rows = 0
    known = 0
    for line in (result.stdout or "").splitlines():
        fields = line.split()
        if not fields or fields[0].startswith("#") or len(fields) < 4 or not fields[0].isdigit() or not fields[1].isdigit():
            continue
        rows += 1
        if not fields[3].isdigit():
            continue                                 # "-": the driver does not say for this process
        known += 1
        card = int(fields[0])
        per_card.setdefault(card, 0.0)
        if int(fields[1]) not in mine:
            per_card[card] += float(fields[3])
    if rows and not known:
        return None
    return max(per_card.values(), default=0.0)


def _yield_thresholds() -> Tuple[float, float]:
    """The GPU and CPU load (percent, by anyone but DLNE) above which this machine steps aside (YIELD_*_PERCENT)."""
    try:
        limits = get_worker_limits()
        return float(limits.get("yield_gpu_percent", 60.0)), float(limits.get("yield_cpu_percent", 60.0))
    except Exception:  # noqa: BLE001
        return 60.0, 60.0


def _linux_gpu_busy_max() -> float:
    """The busiest card's load from amdgpu's ``gpu_busy_percent`` in sysfs (AMD on Linux; "" elsewhere), 0.0 when
    no card says. Read fresh: the card list itself is cached for minutes, this number moves every second."""
    if platform.system() != "Linux":
        return 0.0
    busiest = 0.0
    for entry in _listdir(_DRM_ROOT):
        if not _DRM_CARD_PATTERN.match(entry):
            continue
        value = _read_sysfs(os.path.join(_DRM_ROOT, entry, "device", "gpu_busy_percent"))
        if value.isdigit():
            busiest = max(busiest, float(value))
    return busiest


class _Reading(tuple):
    """
    ``(busy, why)``, compared and unpacked as before, plus ``gpu_seen``: whether the owner's load on the GPU
    itself could be read this time (NVIDIA's per-process figures, or the driver's total while nothing of ours is
    on the card). A GPU node goes by that rather than by the CPU when it can (see _YieldDetector.sample).
    """
    gpu_seen: bool = False

    def __new__(cls, busy: bool, why: str, gpu_seen: bool = False) -> "_Reading":
        reading = super().__new__(cls, (busy, why))
        reading.gpu_seen = gpu_seen
        return reading


def _load_signal(gpu_info: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
    """
    One raw reading: whether the machine is busy with work that is not DLNE's own, and why ("fullscreen",
    "gpu", "cpu", or "" when it is not), as a _Reading that also says whether the GPU's own load was seen.
    See is_gaming_or_heavy_load for the rule; the debounce is the detector's (is_yielding_now).
    """
    own_processes = _collect_llama_processes()
    engine_processes = [p for p in own_processes if _is_engine_process(p)]
    gpu_limit, cpu_limit = _yield_thresholds()

    # Windows: a full-screen game (or a presentation) is running. Vendor-neutral, and the one signal
    # that works while our own llama-server is using the GPU, which hides the GPU's total load.
    if _windows_fullscreen_app_running():
        return _Reading(True, "fullscreen")

    if gpu_info is None:
        gpu_info = get_gpu_info()
    gpu_seen = False
    if gpu_info["has_gpu"]:
        if not engine_processes:
            # With no inference running, a hot GPU can only be someone else's workload. The busiest card
            # decides (a game on one of two cards is a game), from the driver's figure or amdgpu's sysfs.
            load = max(float(gpu_info.get("load_max_percent", gpu_info.get("load_percent", 0.0)) or 0.0),
                       _linux_gpu_busy_max())
            if load > gpu_limit:
                return _Reading(True, "gpu", True)
            gpu_seen = get_gpu_type().startswith("nvidia")      # nvidia-smi's figure is a real reading
        else:
            # Our own llama-server is on the GPU, so the total says nothing about the owner's load. On
            # NVIDIA each process's share is known (when the driver tells it): what the other processes
            # use is the owner's. Where it cannot be told (AMD, Intel, Apple; NVIDIA on Windows) this is
            # None and only the signals above and below apply. Known gap on Linux and macOS: a GPU-bound
            # game that leaves the CPU under the limit is not seen there while we are serving.
            outside = _external_nvidia_load([p.pid for p in own_processes])
            if outside is not None and outside > gpu_limit:
                return _Reading(True, "gpu", True)
            gpu_seen = outside is not None

    # Non-blocking: covers the window since the previous reading instead of sleeping.
    total_cpu_percent = psutil.cpu_percent(interval=None)

    own_cpu_percent = 0.0
    for proc in own_processes:
        try:
            own_cpu_percent += proc.cpu_percent(interval=None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            # The process exited between the sweep and this read; it will be gone from
            # the tracking dict on the next call.
            pass

    # Per-process percentages are relative to a single core, system percentage is relative
    # to the whole machine, so the process figure has to be normalised before subtracting.
    actual_threads = psutil.cpu_count(logical=True)
    normalized_own_load = own_cpu_percent / actual_threads if actual_threads else 0
    external_load = total_cpu_percent - normalized_own_load

    if external_load > cpu_limit:
        return _Reading(True, "cpu", gpu_seen)
    return _Reading(False, "", gpu_seen)


def is_gaming_or_heavy_load(gpu_info: Optional[Dict[str, Any]] = None) -> bool:
    """
    Reports whether the machine is busy, right now, with work that is not DLNE's own inference.

    Only *external* load counts: DLNE's own processes (this one and everything it started, and
    llama.cpp's servers by name) are subtracted, otherwise a node would yield because of the very work
    it was asked to do. The limits are YIELD_GPU_PERCENT and YIELD_CPU_PERCENT (60 each by default).
    This is one raw reading; the profile's ``is_yielding`` is the debounced verdict (is_yielding_now).

    Args:
        gpu_info: An already-computed :func:`get_gpu_info` result to reuse. Callers that
            need the profile anyway pass theirs in, which is why the same GPU state is no
            longer looked up twice per heartbeat (audit item 3.5). Left at None the value
            is fetched here, so every existing caller keeps working unchanged.
    """
    return _load_signal(gpu_info)[0]


# One raw reading every _YIELD_SAMPLE_SECONDS, on a thread of its own, shared by every node in this
# process (the two halves of a "both" machine used to each read once per heartbeat, splitting psutil's
# CPU window and sometimes disagreeing). A full-screen app yields at once; other load has to last
# _YIELD_ON_SAMPLES readings, and the owner has to be quiet for _YIELD_OFF_SAMPLES readings before the
# node comes back: one spike used to cost an RPC node a whole engine redistribution, and a coding
# member an unload, a settle, a reload and a new speed measurement.
_YIELD_SAMPLE_SECONDS = 3.0
_YIELD_ON_SAMPLES = 2
_YIELD_OFF_SAMPLES = 10


class _Debounce:
    """One debounced yes/no: on after _YIELD_ON_SAMPLES busy readings in a row (a full-screen app at once), off
    after _YIELD_OFF_SAMPLES quiet ones."""

    def __init__(self) -> None:
        self.on = False
        self.why = ""
        self._busy = 0
        self._quiet = 0

    def feed(self, busy: bool, why: str) -> bool:
        if busy and why == "fullscreen":
            self.on, self.why, self._busy, self._quiet = True, why, _YIELD_ON_SAMPLES, 0
        elif busy:
            self._busy, self._quiet = self._busy + 1, 0
            if self._busy >= _YIELD_ON_SAMPLES:
                self.on, self.why = True, why
        else:
            self._busy, self._quiet = 0, self._quiet + 1
            if self._quiet >= _YIELD_OFF_SAMPLES:
                self.on, self.why = False, ""
        return self.on


class _YieldDetector:
    """
    Two verdicts from the same readings. ``yielding`` is the machine's: any load of the owner's, which is what a
    CPU contributor (and the CPU half of a "both" machine) steps aside for. ``gpu_yielding`` is a GPU node's: the
    same, except that CPU load alone does not count when the GPU's own load was read and found quiet (2.6.2). A
    shard's work is on the GPU, and its stepping aside costs every user an engine rebuild: on 2026-10-28 a laptop
    whose GPU only drove its display left the pool every few minutes while builds and test runs used its CPU.
    Where the GPU's load cannot be read, the CPU still stands in for it, as before.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._machine = _Debounce()
        self._gpu = _Debounce()
        self.yielding = False
        self.why = ""
        self.gpu_yielding = False

    def sample(self, gpu_info: Optional[Dict[str, Any]] = None) -> bool:
        """Take one reading and fold it into both verdicts; returns the machine's."""
        try:
            reading = _load_signal(gpu_info)
            raw, why = reading
        except Exception:  # noqa: BLE001 - a failed reading says nothing
            reading, raw, why = None, False, ""
        gpu_busy = raw and not (why == "cpu" and getattr(reading, "gpu_seen", False))
        with self._lock:
            self.yielding = self._machine.feed(raw, why)
            self.why = self._machine.why
            self.gpu_yielding = self._gpu.feed(gpu_busy, why if gpu_busy else "")
            return self.yielding

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, name="dlne-yield", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            self.sample()
            time.sleep(_YIELD_SAMPLE_SECONDS)


_yield_detector = _YieldDetector()


def is_yielding_now(gpu_info: Optional[Dict[str, Any]] = None, cpu_node: bool = False) -> bool:
    """
    The debounced verdict for the profile's ``is_yielding``; starts the sampler the first time it is asked. A
    machine with a GPU reports its GPU node's verdict (which CPU load alone does not trip when the GPU can be
    read); a machine without one, and the CPU half of a "both" machine (``cpu_node``), the machine's.
    """
    _yield_detector.start()
    if not cpu_node and isinstance(gpu_info, dict) and gpu_info.get("has_gpu"):
        return _yield_detector.gpu_yielding
    return _yield_detector.yielding


def yield_reason() -> str:
    """Why the machine is yielding ("fullscreen", "gpu" or "cpu"), "" when it is not."""
    return _yield_detector.why if _yield_detector.yielding else ""


def get_cpu_threads() -> int:
    """Returns number of logical CPU threads, capped by config limits."""
    actual_threads = psutil.cpu_count(logical=True)
    limit = get_worker_limits().get('max_cpu_threads', -1)
    if limit >= 0:
        return min(actual_threads, limit)
    return actual_threads

# --------------------------------------------------------------------------------------
# CPU instruction sets and memory bandwidth (what a CPU-only node is judged on)
# --------------------------------------------------------------------------------------
#
# Generating a token reads every weight a node holds, so on a CPU the speed is set by
# memory bandwidth far more than by core count, and a slow node holds up every token of the
# pipeline it sits in. The Coordinator therefore admits a CPU-only node on a measured
# bandwidth floor plus the instruction sets llama.cpp's fast CPU paths need (AVX2 + FMA).

# Reported instruction sets, in the names llama.cpp's CPU variants are built around.
CPU_FEATURE_NAMES = ("avx2", "fma", "f16c", "avx512f", "avx_vnni", "amx")

# /proc/cpuinfo spells AMX per sub-feature; any of them means the tile unit is there.
_LINUX_FLAG_ALIASES = {"amx_tile": "amx"}

# Windows exposes no FMA or F16C query. Every x86 CPU with AVX2 has both (Intel Haswell and
# AMD Excavator onward), so they are inferred from AVX2 there.
_WIN_PF_AVX2 = 40
_WIN_PF_AVX512F = 41

_cached_cpu_features: Optional[List[str]] = None


def get_cpu_arch() -> str:
    """'x86_64', 'arm64', or the platform's own spelling of anything else."""
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64", "x64"):
        return "x86_64"
    if machine in ("arm64", "aarch64"):
        return "arm64"
    return machine


def _linux_cpu_features(cpuinfo_text: str) -> List[str]:
    for line in cpuinfo_text.splitlines():
        if line.startswith("flags"):
            flags = {_LINUX_FLAG_ALIASES.get(f, f) for f in line.split(":", 1)[-1].split()}
            return [name for name in CPU_FEATURE_NAMES if name in flags]
    return []


def _windows_cpu_features(present=None) -> List[str]:
    if present is None:
        import ctypes
        present = ctypes.windll.kernel32.IsProcessorFeaturePresent  # type: ignore[attr-defined]
    found = []
    if present(_WIN_PF_AVX2):
        found += ["avx2", "fma", "f16c"]
    if present(_WIN_PF_AVX512F):
        found.append("avx512f")
    return [name for name in CPU_FEATURE_NAMES if name in found]


def _macos_cpu_features() -> List[str]:
    flags = set()
    for key in ("machdep.cpu.features", "machdep.cpu.leaf7_features"):
        result = subprocess.run(["sysctl", "-n", key], capture_output=True, text=True, timeout=5)
        flags.update(f.lower().replace(".", "_") for f in result.stdout.split())
    return [name for name in CPU_FEATURE_NAMES if name in flags]


def get_cpu_features() -> List[str]:
    """
    The subset of CPU_FEATURE_NAMES this CPU has. Empty on ARM (Apple Silicon contributes
    through Metal) and when the probe fails, which the Coordinator treats as "missing".
    Probed once: a CPU does not gain instructions while DLNE runs.
    """
    global _cached_cpu_features
    if _cached_cpu_features is None:
        features: List[str] = []
        try:
            if get_cpu_arch() == "x86_64":
                system = platform.system()
                if system == "Linux":
                    with open("/proc/cpuinfo", "r", encoding="utf-8", errors="replace") as handle:
                        features = _linux_cpu_features(handle.read())
                elif system == "Windows":
                    features = _windows_cpu_features()
                elif system == "Darwin":
                    features = _macos_cpu_features()
        except Exception:  # noqa: BLE001 - an unknown CPU is reported as having nothing
            features = []
        _cached_cpu_features = features
    return list(_cached_cpu_features)


# Each measuring process copies a buffer this large back and forth. 128 MiB is past any
# consumer L3 cache, the 96 MiB of a 3D V-Cache chiplet included, so the copy really goes
# to RAM; two of them per process cap the probe at 2 GiB with eight processes.
BANDWIDTH_CHUNK_BYTES = 128 * 1024 * 1024
BANDWIDTH_PASSES = 4
BANDWIDTH_MAX_PROCESSES = 8

_cached_bandwidth: Optional[float] = None
_bandwidth_measured = False


def _bandwidth_worker(ready, start_event, results, chunk_bytes: int, passes: int) -> None:
    src = bytearray(b"\xa5" * 4096) * (chunk_bytes // 4096)
    dst = bytearray(chunk_bytes)
    dst[:] = src  # touch every page before the clock starts
    ready.put(True)
    start_event.wait()
    began = time.perf_counter()
    for _ in range(passes):
        dst[:] = src
    results.put((time.perf_counter() - began, passes * chunk_bytes * 2))


def measure_memory_bandwidth_gbs(processes: Optional[int] = None,
                                 chunk_bytes: int = BANDWIDTH_CHUNK_BYTES,
                                 passes: int = BANDWIDTH_PASSES) -> float:
    """
    Sustained memory bandwidth in GB/s, counted the way STREAM's copy test counts it (bytes
    read plus bytes written).

    A bytearray slice copy is a C memcpy, but it holds the GIL, so threads would take turns;
    separate processes copy at the same time, the way llama.cpp's threads do. One process
    per physical core up to BANDWIDTH_MAX_PROCESSES: memory saturates long before that.
    """
    import multiprocessing

    if processes is None:
        processes = max(1, min(_detect_physical_cores(), BANDWIDTH_MAX_PROCESSES))
    ctx = multiprocessing.get_context("spawn")
    ready, start_event, results = ctx.Queue(), ctx.Event(), ctx.Queue()
    workers = [ctx.Process(target=_bandwidth_worker, args=(ready, start_event, results, chunk_bytes, passes), daemon=True)
               for _ in range(processes)]
    for worker in workers:
        worker.start()
    try:
        # Every process has allocated and touched its buffers before the clock starts, so
        # start-up cost (interpreter launch, page faults) never counts as slow memory.
        waiting = len(workers)
        deadline = time.monotonic() + 60
        while waiting:
            try:
                ready.get(timeout=0.25)
                waiting -= 1
            except queue.Empty:
                # A process that died (no memory for its buffers, a broken interpreter) will
                # never report ready; fail now rather than at the deadline.
                if any(w.exitcode not in (None, 0) for w in workers) or time.monotonic() > deadline:
                    raise RuntimeError("a bandwidth probe process did not start")
        began = time.perf_counter()
        start_event.set()
        moved = sum(results.get(timeout=120)[1] for _ in workers)
        elapsed = time.perf_counter() - began
    finally:
        for worker in workers:
            worker.join(timeout=5)
            if worker.is_alive():
                worker.terminate()
    return round(moved / elapsed / 1e9, 1)


def get_memory_bandwidth_gbs() -> Optional[float]:
    """measure_memory_bandwidth_gbs(), once per process; None when the probe itself failed."""
    global _cached_bandwidth, _bandwidth_measured
    if not _bandwidth_measured:
        _bandwidth_measured = True
        try:
            _cached_bandwidth = measure_memory_bandwidth_gbs()
        except Exception:  # noqa: BLE001 - the Coordinator refuses an unmeasured CPU node
            _cached_bandwidth = None
    return _cached_bandwidth


# ======================================================================================
# Docked: a wired link and mains power (docs/coding-members.md sections 1 and 2)
# ======================================================================================
#
# A LAN GPU shards into the coordinator's engine only while it is docked: on a wired link and on
# mains power. A laptop on Wi-Fi or on battery becomes a coding member (or nothing), because a
# shard that leaves takes every user's running answer with it. The worker reports the two facts
# and the coordinator decides; a fact that cannot be read is left out of the profile, never
# guessed, and the coordinator then behaves as before this signal existed.

_NET_ROOT = "/sys/class/net"
_POWER_SUPPLY_ROOT = "/sys/class/power_supply"
_PROC_NET_ROUTE = "/proc/net/route"
_DMI_ROOT = "/sys/class/dmi/id"
_PROC_CPUINFO = "/proc/cpuinfo"

# Tunnel interfaces (the same names engine/link_paths.py skips): a default route over one says
# nothing about the physical link underneath it.
_TUNNEL_IFACE_PREFIXES = ("wg", "tun", "tap", "tailscale", "zt", "utun", "ppp")
# USB-tethered phones register as Ethernet-class devices but are cellular links.
_TETHER_DRIVERS = ("rndis_host", "ipheth")
# Hypervisors as DMI and WMI name them. A guest looks wired and has no battery whatever its host does.
_VM_VENDORS = ("qemu", "vmware", "innotek", "virtualbox", "virtual machine", "xen", "kvm", "parallels",
               "bochs", "bhyve", "hyper-v")

_DOCK_TTL_SECONDS: float = 20.0      # Windows and macOS ask a subprocess; sysfs is cheap but needs no faster
# A change is reported only once it has held this long: a dock replugged, a charger brushed, or
# a DHCP renew must not move a shard (a role change costs the engine a restart, section 1).
# Except going onto battery (2.6.4): a GPU never shards on battery, and a laptop just unplugged may
# be closed and asleep before the minute is out, taking its shard and every answer on it along.
# That change is reported at once, so the coordinator's drain starts while the machine is still
# awake; going back onto mains still waits out the hold.
_DOCK_HOLD_SECONDS: float = 60.0

_dock_lock = threading.Lock()
_dock_cache: Optional[Dict[str, str]] = None
_dock_cache_expiry: float = 0.0
_dock_reported: Optional[Dict[str, str]] = None   # what the profile says now
_dock_pending: Optional[Dict[str, str]] = None    # a different reading, waiting out the hold
_dock_pending_since: float = 0.0


def _linux_default_interface() -> str:
    """The interface the default route leaves by (lowest metric), skipping tunnels; "" when none."""
    best: Tuple[int, str] = (1 << 30, "")
    for line in _read_sysfs(_PROC_NET_ROUTE).splitlines()[1:]:
        fields = line.split()
        if len(fields) < 8 or fields[1] != "00000000" or fields[7] != "00000000":
            continue
        try:
            flags, metric = int(fields[3], 16), int(fields[6])
        except ValueError:
            continue
        if not flags & 0x1 or fields[0].lower().startswith(_TUNNEL_IFACE_PREFIXES):
            continue
        if metric < best[0]:
            best = (metric, fields[0])
    return best[1]


def _linux_real_interface(name: str) -> str:
    """The physical interface under a bridge, bond or VLAN (a few levels at most)."""
    for _ in range(4):
        base = os.path.join(_NET_ROOT, name)
        lower = ""
        if os.path.isdir(os.path.join(base, "brif")):
            ports = sorted(_listdir(os.path.join(base, "brif")))
            lower = ports[0] if ports else ""
        elif _read_sysfs(os.path.join(base, "bonding", "slaves")):
            lower = _read_sysfs(os.path.join(base, "bonding", "slaves")).split()[0]
        else:
            lowers = sorted(e[len("lower_"):] for e in _listdir(base) if e.startswith("lower_"))
            lower = lowers[0] if lowers else ""
        if not lower or lower == name:
            return name
        name = lower
    return name


def _listdir(path: str) -> List[str]:
    try:
        return os.listdir(path)
    except OSError:
        return []


def _linux_link() -> str:
    name = _linux_default_interface()
    if not name:
        return "unknown"
    name = _linux_real_interface(name)
    base = os.path.join(_NET_ROOT, name)
    uevent = _read_sysfs(os.path.join(base, "uevent")).lower()
    if os.path.isdir(os.path.join(base, "wireless")) or os.path.isdir(os.path.join(base, "phy80211")) \
            or "devtype=wlan" in uevent:
        return "wifi"
    driver = os.path.basename(os.path.realpath(os.path.join(base, "device", "driver")))
    if "devtype=wwan" in uevent or driver in _TETHER_DRIVERS:
        return "cellular"
    if _read_sysfs(os.path.join(base, "type")) == "1" and _read_sysfs(os.path.join(base, "carrier")) == "1":
        return "wired"       # Ethernet-class with the link up: a NIC, or a dock's USB Ethernet
    return "unknown"


def _linux_power() -> str:
    """ac, battery or unknown from /sys/class/power_supply. No system battery at all is a desktop: ac."""
    ac_online = False
    battery_status = None
    for entry in _listdir(_POWER_SUPPLY_ROOT):
        base = os.path.join(_POWER_SUPPLY_ROOT, entry)
        kind = _read_sysfs(os.path.join(base, "type"))
        if kind == "Mains" or kind.startswith("USB"):
            ac_online = ac_online or _read_sysfs(os.path.join(base, "online")) == "1"
        elif kind == "Battery" and _read_sysfs(os.path.join(base, "scope")) != "Device":   # Device: a mouse, a headset
            battery_status = _read_sysfs(os.path.join(base, "status"))
    if ac_online:
        return "ac"
    if battery_status is None:
        return "ac"
    if battery_status == "Discharging":
        return "battery"
    if battery_status in ("Charging", "Full", "Not charging"):
        return "ac"
    return "unknown"


def _linux_is_vm() -> bool:
    vendor = (_read_sysfs(os.path.join(_DMI_ROOT, "sys_vendor")) + " "
              + _read_sysfs(os.path.join(_DMI_ROOT, "product_name"))).lower()
    if any(v in vendor for v in _VM_VENDORS):
        return True
    for line in _read_sysfs(_PROC_CPUINFO).splitlines():
        if line.startswith("flags"):
            return " hypervisor" in line
    return False


def _windows_power() -> str:
    """ac, battery or unknown from GetSystemPowerStatus (what psutil's sensors_battery reads too)."""
    try:
        import ctypes

        class _PowerStatus(ctypes.Structure):
            _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                        ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                        ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]

        status = _PowerStatus()
        query = ctypes.windll.kernel32.GetSystemPowerStatus
        if not query(ctypes.byref(status)):
            return "unknown"
        if status.BatteryFlag & 128:           # no system battery: a desktop
            return "ac"
        return {1: "ac", 0: "battery"}.get(int(status.ACLineStatus), "unknown")
    except Exception:  # noqa: BLE001 - no signal is left out, never guessed
        return "unknown"


_WINDOWS_LINK_SCRIPT = (
    "$ErrorActionPreference='SilentlyContinue';"
    "$r = Get-NetRoute -DestinationPrefix '0.0.0.0/0' | Sort-Object RouteMetric, InterfaceMetric | Select-Object -First 1;"
    "$a = $r | Get-NetAdapter;"
    "$m = (Get-CimInstance Win32_ComputerSystem).Model;"
    "@{media=[string]$a.PhysicalMediaType; virtual=[bool]$a.Virtual; status=[string]$a.Status; model=[string]$m}"
    " | ConvertTo-Json -Compress"
)


def _windows_link_and_vm() -> Tuple[str, bool]:
    """The default route's adapter medium (802.3 wired, Native 802.11 Wi-Fi, Wireless WAN) and whether
    this is a virtual machine, from one PowerShell call. Never WMI AdapterTypeID (many Wi-Fi drivers
    say Ethernet) and never wmic (gone since Windows 11 24H2)."""
    try:
        import json as _json
        result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_LINK_SCRIPT],
                                capture_output=True, text=True, timeout=20, **_hidden_process_kwargs())
        info = _json.loads(result.stdout.strip() or "{}")
    except Exception:  # noqa: BLE001
        return "unknown", False
    model = str(info.get("model") or "").lower()
    is_vm = any(v in model for v in _VM_VENDORS)
    if info.get("virtual") or str(info.get("status") or "").lower() not in ("up", ""):
        return "unknown", is_vm        # a Hyper-V vEthernet over Wi-Fi reports as Ethernet: no claim
    media = str(info.get("media") or "")
    if media == "802.3":
        return "wired", is_vm
    if "802.11" in media:
        return "wifi", is_vm
    if "wireless wan" in media.lower():
        return "cellular", is_vm
    return "unknown", is_vm


def _macos_power() -> str:
    try:
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5).stdout
    except Exception:  # noqa: BLE001
        return "unknown"
    first = out.splitlines()[0] if out.strip() else ""
    if "AC Power" in first:
        return "ac"
    if "Battery Power" in first:
        return "battery"
    return "unknown"


def _macos_link() -> str:
    try:
        route = subprocess.run(["route", "-n", "get", "default"], capture_output=True, text=True, timeout=5).stdout
        ports = subprocess.run(["networksetup", "-listallhardwareports"], capture_output=True, text=True,
                               timeout=5).stdout
    except Exception:  # noqa: BLE001
        return "unknown"
    device = ""
    for line in route.splitlines():
        if line.strip().startswith("interface:"):
            device = line.split(":", 1)[1].strip()
    if not device or device.startswith(_TUNNEL_IFACE_PREFIXES):
        return "unknown"
    port = ""
    for line in ports.splitlines():
        if line.startswith("Hardware Port:"):
            port = line.split(":", 1)[1].strip().lower()
        elif line.startswith("Device:") and line.split(":", 1)[1].strip() == device:
            if "wi-fi" in port or "airport" in port or "bluetooth" in port:
                return "wifi"
            if "iphone" in port or "ipad" in port:
                return "cellular"
            if "ethernet" in port or "lan" in port or "thunderbolt" in port:
                return "wired"
            return "unknown"
    return "unknown"


def _macos_is_vm() -> bool:
    try:
        return subprocess.run(["sysctl", "-n", "kern.hv_vmm_present"], capture_output=True, text=True,
                              timeout=5).stdout.strip() == "1"
    except Exception:  # noqa: BLE001
        return False


def _probe_dock() -> Dict[str, str]:
    system = platform.system()
    if system == "Linux":
        link, power, is_vm = _linux_link(), _linux_power(), _linux_is_vm()
    elif system == "Windows":
        (link, is_vm), power = _windows_link_and_vm(), _windows_power()
    elif system == "Darwin":
        link, power, is_vm = _macos_link(), _macos_power(), _macos_is_vm()
    else:
        return {"link": "unknown", "power": "unknown"}
    if is_vm:
        link = "unknown"       # a guest's virtio NIC looks wired whatever the host is on
    return {"link": link, "power": power}


def get_dock_state() -> Dict[str, str]:
    """
    ``{"link": wired|wifi|cellular|unknown, "power": ac|battery|unknown}`` for the profile. Probed at most
    every _DOCK_TTL_SECONDS; the first reading is reported at once, a later change only after it has held
    _DOCK_HOLD_SECONDS, so a replugged dock does not move a shard twice. A change onto battery is reported
    at once (2.6.4, see _DOCK_HOLD_SECONDS). Never raises.
    """
    global _dock_cache, _dock_cache_expiry, _dock_reported, _dock_pending, _dock_pending_since
    with _dock_lock:
        now = time.monotonic()
        if _dock_cache is None or now >= _dock_cache_expiry:
            try:
                _dock_cache = _probe_dock()
            except Exception:  # noqa: BLE001
                _dock_cache = {"link": "unknown", "power": "unknown"}
            _dock_cache_expiry = now + _DOCK_TTL_SECONDS
        reading = dict(_dock_cache)
        if _dock_reported is None:
            _dock_reported, _dock_pending = reading, None
        elif reading.get("power") == "battery" and _dock_reported.get("power") != "battery":
            _dock_reported, _dock_pending = reading, None      # just unplugged: no hold
        elif reading == _dock_reported:
            _dock_pending = None
        elif reading != _dock_pending:
            _dock_pending, _dock_pending_since = reading, now
        elif now - _dock_pending_since >= _DOCK_HOLD_SECONDS:
            _dock_reported, _dock_pending = reading, None
        return dict(_dock_reported)


def get_hardware_profile() -> Dict[str, Any]:
    """
    Builds the profile a worker sends on every heartbeat.

    The key names and nesting below are consumed verbatim by ``engine/registry.py`` and
    ``engine/coordinator.py`` (and validated by ``engine/authn.validate_hardware_profile``),
    so this structure must not change.
    """
    # Probed once and shared. This used to be two independent calls — the "gpu" field and
    # the load check inside is_gaming_or_heavy_load — which meant the whole GPU detection
    # chain ran twice for every heartbeat (audit item 3.5).
    gpu_info = get_gpu_info()

    profile = {
        "os": platform.system(),
        "cpu_arch": get_cpu_arch(),
        "cpu_cores": get_cpu_cores(),
        "cpu_threads": get_cpu_threads(),
        "cpu_features": get_cpu_features(),
        "ram_gb": get_system_ram(),
        "gpu": gpu_info,
        "gpu_type": get_gpu_type(),
        "is_yielding": is_yielding_now(gpu_info)
    }
    # Measured only where it decides admission: a node with no usable GPU. It costs a few
    # seconds and up to 2 GiB once, so GPU nodes never pay it.
    if not gpu_info.get("has_gpu"):
        bandwidth = get_memory_bandwidth_gbs()
        if bandwidth is not None:
            profile["mem_bandwidth_gbs"] = bandwidth
    # Docked (2.5.0): a wired link and mains power. A fact that could not be read is left out, and
    # "docked" is only said when both are known; the coordinator keeps its older behaviour otherwise.
    dock = get_dock_state()
    if dock["link"] != "unknown":
        profile["link"] = dock["link"]
    if dock["power"] != "unknown":
        profile["power"] = dock["power"]
    if dock["link"] != "unknown" and dock["power"] != "unknown":
        profile["docked"] = dock["link"] == "wired" and dock["power"] == "ac"
    return profile


# ======================================================================================
# May this machine run the CUDA engine? (2.7.0, docs/release-2.7-contract.md section 1)
# ======================================================================================
#
# The CUDA build is llama.cpp's official CUDA 13 build. It needs an NVIDIA GPU of compute capability 7.5 or newer
# (Turing: RTX 20 / GTX 16 and later; Maxwell, Pascal and Volta stay on Vulkan) and a driver new enough for CUDA 13.
# Both facts come from one nvidia-smi call. The answer is plain words for the editor and the tray to show, so a
# greyed-out "CUDA" always says why.

# The oldest driver the CUDA 13 engine runs on, as nvidia-smi prints it (major.minor, a third part is ignored): 580 for
# Linux and Windows alike, from NVIDIA's CUDA 13.4 release notes (Table 3). The same figure is engine.release.CUDA_MIN_DRIVER
# (packaging); it is repeated here so this module needs no import of the release code.
CUDA13_MIN_DRIVER = "580"

# Compute capability 7.5 is Turing, the oldest generation CUDA 13 still builds kernels for.
CUDA_MIN_COMPUTE_CAP = (7, 5)

_CUDA_PROBE_TIMEOUT_SECONDS = 8.0

# Where nvidia-smi lives on Windows when it is not on PATH (the driver installs it in System32; older drivers use
# the NVSMI folder).
_WINDOWS_NVIDIA_SMI = (
    r"C:\Windows\System32\nvidia-smi.exe",
    r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
)

# One probe per process: the card and its driver do not change while the worker runs (a driver update needs a
# restart of the worker anyway), and the answer is asked for by the start-up, the heartbeat and the status.
_cuda_capability_cache: Optional[Dict[str, Any]] = None
_cuda_capability_lock = threading.Lock()


def _version_tuple(text: Any) -> Tuple[int, ...]:
    """'615.78.08' -> (615, 78, 8); () when there is no number in it."""
    parts = re.findall(r"\d+", str(text or ""))
    return tuple(int(p) for p in parts[:3])


def _find_nvidia_smi() -> Optional[str]:
    """The nvidia-smi to run: the one on PATH, else (Windows) the places the driver puts it, else None."""
    import shutil
    found = shutil.which("nvidia-smi")
    if found:
        return found
    if platform.system() == "Windows":
        for candidate in _WINDOWS_NVIDIA_SMI:
            if os.path.exists(candidate):
                return candidate
    return None


def _query_nvidia_smi(fields: str) -> Optional[List[str]]:
    """The csv lines nvidia-smi prints for ``fields`` (one per GPU), or None when it is missing, fails or hangs."""
    binary = _find_nvidia_smi()
    if binary is None:
        return None
    try:
        result = subprocess.run([binary, f"--query-gpu={fields}", "--format=csv,noheader"],
                                capture_output=True, text=True, timeout=_CUDA_PROBE_TIMEOUT_SECONDS,
                                **_hidden_process_kwargs())
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]


def _read_nvidia_gpus() -> Optional[List[Dict[str, str]]]:
    """Every NVIDIA GPU as {"name", "compute_cap", "driver"}, or None when nvidia-smi gives nothing usable. A driver
    old enough not to know the ``compute_cap`` field is asked again for name and driver alone, with the capability
    left empty, so the reason can still name the driver."""
    lines = _query_nvidia_smi("name,compute_cap,driver_version")
    with_cap = lines is not None
    if lines is None:
        lines = _query_nvidia_smi("name,driver_version")
    gpus: List[Dict[str, str]] = []
    for line in lines or []:
        # From the right: a GPU name may itself hold a comma, the other fields never do.
        parts = [p.strip() for p in line.rsplit(",", 2 if with_cap else 1)]
        if len(parts) != (3 if with_cap else 2):
            continue
        gpus.append({"name": parts[0], "compute_cap": parts[1] if with_cap else "", "driver": parts[-1]})
    return gpus or None


def _judge_cuda(gpus: Optional[List[Dict[str, str]]]) -> Dict[str, Any]:
    """The ``DLNE_CUDA`` answer for what nvidia-smi listed (separate from the probe so it is testable)."""
    if not gpus:
        return {"capable": False, "reason": "no NVIDIA GPU", "gpu": "", "compute_cap": "", "driver": ""}
    # Several GPUs: the newest architecture is the one the CUDA engine would be worth running on.
    best = max(gpus, key=lambda g: _version_tuple(g["compute_cap"]))
    answer = {"capable": False, "reason": "", "gpu": best["name"], "compute_cap": best["compute_cap"],
              "driver": best["driver"]}
    cap, driver = _version_tuple(best["compute_cap"]), _version_tuple(best["driver"])
    if not cap:
        answer["reason"] = "this GPU's compute capability could not be read (its driver is probably too old)"
    elif cap[:2] < CUDA_MIN_COMPUTE_CAP:
        answer["reason"] = f"this GPU is older than Turing (compute capability {best['compute_cap']})"
    elif not driver or driver < _version_tuple(CUDA13_MIN_DRIVER):
        answer["reason"] = (f"driver {best['driver'] or 'unknown'} is older than CUDA 13 needs "
                            f"({CUDA13_MIN_DRIVER} or later)")
    else:
        answer["capable"] = True
    return answer


def cuda_capability(refresh: bool = False) -> Dict[str, Any]:
    """
    May this machine run the CUDA engine, and why not if it may not.

    ``{"capable": bool, "reason": str, "gpu": str, "compute_cap": str, "driver": str}``: capable only for an NVIDIA
    GPU of compute capability 7.5 or newer and a driver of at least CUDA13_MIN_DRIVER. ``reason`` is empty when
    capable, otherwise plain words ("no NVIDIA GPU", "this GPU is older than Turing (compute capability 6.1)",
    "driver 572.16 is older than CUDA 13 needs (580 or later)"). With several GPUs the one with the newest
    compute capability is judged. Never raises; read once per process (``refresh`` asks again).
    """
    global _cuda_capability_cache
    with _cuda_capability_lock:
        if _cuda_capability_cache is None or refresh:
            try:
                _cuda_capability_cache = _judge_cuda(_read_nvidia_gpus())
            except Exception as exc:  # noqa: BLE001 - a probe that breaks means "not capable", never a crash
                _cuda_capability_cache = {"capable": False, "reason": f"the NVIDIA GPU could not be read ({type(exc).__name__})",
                                          "gpu": "", "compute_cap": "", "driver": ""}
        return dict(_cuda_capability_cache)
