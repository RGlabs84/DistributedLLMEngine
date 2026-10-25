"""
DLNE runtime dependency preflight — the one place that knows what a node needs installed.

WHY THIS MODULE EXISTS
----------------------
DLNE ships its own engine binaries, but those binaries are not self-contained: the Vulkan
builds need a Vulkan loader that comes from the GPU driver, the CUDA builds need an NVIDIA
driver new enough for the CUDA runtime they were linked against, the GUI wizard needs a
web view host (WebView2 on Windows, WebKit2GTK on Linux), and WAN roles need WireGuard.

Before this module those checks lived in three unrelated places and mostly did not exist:

* ``engine/downloader.py`` had an inline, Linux-only Vulkan installer that was reached only
  *after* an engine binary had already failed to start;
* ``setup_cli.auto_install_wireguard`` had its own package-manager logic;
* nothing on Windows checked for Vulkan, WebView2 or a CUDA-capable driver at all — the
  first symptom was a cryptic DLL-load failure at inference time.

Everything now routes here: the setup wizard (console and GUI) shows a System Check page
built from :func:`preflight`, the downloader's runtime fallback calls
:func:`console_install_flow`, and the installer covers the same list ahead of first launch.

WHERE THINGS ARE DOWNLOADED FROM ("the right place")
----------------------------------------------------
Every Windows payload is fetched from the vendor's own canonical URL — Microsoft's
``go.microsoft.com`` / ``aka.ms`` redirectors, LunarG's SDK endpoint, wireguard.com — and
never from a DLNE-controlled mirror by default. On Linux nothing is downloaded at all: the
system package manager is asked for the distribution's own package, which is the only
correct source there.

``DEPENDENCY_MIRROR_URL`` (a config knob that existed, was tested, and until now was read
by nothing) overrides the vendor host for air-gapped or LAN-only swarms. The mirror is
expected to serve the *same filenames* under one base URL; only the host and path prefix
change, so an operator can mirror the five installers with a plain static file server.

CONSENT MODEL
-------------
Identical to the updater's: always ask, never silent. :func:`install` refuses to run with
``consent=False`` — the GUI's install button and the console's ``Y/n`` are the only ways a
third-party installer ever executes, and each dependency's ``why`` text is what the user is
consenting to. Dependencies that must not be auto-installed at all (GPU drivers) report
``action_required`` with instructions instead of an install path.

FAILURE POSTURE
---------------
A dependency probe that cannot answer returns ``unknown``, never ``missing``: this module
must not scare a user (or block an installer) because ``nvidia-smi`` was slow. Nothing here
raises into a caller — every entry point returns a status structure, because both consumers
(a wizard page and a mid-download fallback) have to keep going regardless.
"""

import ctypes
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Callable, Dict, List, Optional

from engine.config import get_base_dir, get_worker_limits
from engine.logger import log_error, log_success, log_system, log_warning

# --------------------------------------------------------------------------------------
# Status vocabulary
# --------------------------------------------------------------------------------------
# These strings cross a process boundary twice (into the GUI wizard's JSON bridge and into
# the portal's status payloads), so they are part of this module's contract and must not be
# renamed casually.
STATUS_OK = "ok"                            # present and usable
STATUS_MISSING = "missing"                  # absent, and this module can install it
STATUS_ACTION_REQUIRED = "action_required"  # absent/wrong, but a human must fix it
STATUS_UNKNOWN = "unknown"                  # the probe could not decide — never blocks

# --------------------------------------------------------------------------------------
# NVIDIA driver floors for the CUDA variants
# --------------------------------------------------------------------------------------
# A CUDA runtime refuses to initialise against a driver older than its family's minimum.
# These are the Windows minimums published by NVIDIA for the CUDA versions the release
# variants are built against (Linux floors are slightly lower; the higher Windows number is
# used for both so the check errs toward "update your driver" rather than a runtime crash).
CUDA_DRIVER_FLOORS = {
    "11.8": 452.39,
    "12": 527.41,
    "13": 580.65,
}

# --------------------------------------------------------------------------------------
# Vendor download endpoints ("the right place")
# --------------------------------------------------------------------------------------
WEBVIEW2_BOOTSTRAP_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
WEBVIEW2_BOOTSTRAP_FILENAME = "MicrosoftEdgeWebview2Setup.exe"
VULKAN_RUNTIME_URL = "https://sdk.lunarg.com/sdk/download/latest/windows/vulkan-runtime.exe"
VULKAN_RUNTIME_FILENAME = "vulkan-runtime.exe"
WIREGUARD_WINDOWS_URL = "https://download.wireguard.com/windows-client/wireguard-installer.exe"
WIREGUARD_WINDOWS_FILENAME = "wireguard-installer.exe"
VCREDIST_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
VCREDIST_FILENAME = "vc_redist.x64.exe"

# WebView2 registers its version under this fixed client GUID. Both the per-machine
# (HKLM, plus the WOW6432 view a 64-bit process has to ask for explicitly) and per-user
# (HKCU) installs count — pywebview only needs the runtime to be resolvable.
WEBVIEW2_CLIENT_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"

DOWNLOAD_TIMEOUT_SECONDS = 300.0


def _hidden_process_kwargs() -> dict:
    """Suppress the console window Windows would flash for each probe."""
    if platform.system() == "Windows" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def _result(status: str, detail: str = "", **extra: Any) -> Dict[str, Any]:
    """Uniform probe/install return shape."""
    payload = {"status": status, "detail": detail}
    payload.update(extra)
    return payload


# --------------------------------------------------------------------------------------
# Mirror handling
# --------------------------------------------------------------------------------------

def mirror_base_url() -> str:
    """
    The configured ``DEPENDENCY_MIRROR_URL``, normalised to have no trailing slash.

    Empty (the default) means "use the vendor URLs", which is what every internet-connected
    install should do — the mirror exists for LAN-only swarms that cannot reach Microsoft.
    """
    try:
        base = str(get_worker_limits().get("dependency_mirror_url", "") or "").strip()
    except Exception:
        # get_worker_limits reads config from disk; a broken config must not stop a probe.
        return ""
    return base.rstrip("/")


def resolve_download_url(vendor_url: str, filename: str) -> str:
    """
    Where a given vendor payload should actually be fetched from.

    With no mirror configured this is the vendor URL verbatim. With a mirror configured the
    *filename* — not the vendor path, which may be an opaque redirector like
    ``fwlink/p/?LinkId=...`` — is appended to the mirror base. That is why every dependency
    carries an explicit filename alongside its URL: a mirror operator needs to know exactly
    what to name the file they host.
    """
    base = mirror_base_url().rstrip("/")
    if not base:
        return vendor_url
    return f"{base}/{filename}"


def _download(url: str, destination_path: str) -> Dict[str, Any]:
    """
    Streamed download of a third-party installer to ``destination_path``.

    No hash is checked here and that is a deliberate, bounded decision: unlike DLNE's own
    release assets (which the updater verifies against a signed manifest), these payloads
    are vendor artefacts that change whenever the vendor rebuilds them, so there is no
    stable digest to pin. Their integrity comes from HTTPS plus the vendor's own
    Authenticode signature, which Windows verifies when the installer is executed.
    """
    try:
        import httpx
    except ImportError:
        return _result(STATUS_UNKNOWN, "httpx is unavailable, cannot download.")

    try:
        with httpx.stream("GET", url, timeout=DOWNLOAD_TIMEOUT_SECONDS,
                          follow_redirects=True) as response:
            response.raise_for_status()
            with open(destination_path, "wb") as handle:
                for chunk in response.iter_bytes(chunk_size=1024 * 256):
                    handle.write(chunk)
    except Exception as exc:
        return _result(STATUS_MISSING, f"Download failed: {exc}")

    if not os.path.exists(destination_path) or os.path.getsize(destination_path) == 0:
        return _result(STATUS_MISSING, "Download produced an empty file.")
    return _result(STATUS_OK, destination_path)


def _run_installer(command: List[str], timeout: float = 900.0) -> Dict[str, Any]:
    """Execute a downloaded installer and translate its exit code into a status."""
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return _result(STATUS_UNKNOWN, "The installer did not finish in time.")
    except Exception as exc:
        return _result(STATUS_MISSING, f"Could not launch the installer: {exc}")

    if completed.returncode == 0:
        return _result(STATUS_OK, "Installed.")
    # 3010 is the universal Windows "success, reboot required".
    if completed.returncode == 3010:
        return _result(STATUS_OK, "Installed — a reboot is required to complete it.")
    message = (completed.stderr or completed.stdout or "").strip()[:300]
    return _result(STATUS_MISSING, f"Installer exited with code {completed.returncode}. {message}".strip())


# --------------------------------------------------------------------------------------
# Linux package managers — factored once, out of downloader.py's inline copy
# --------------------------------------------------------------------------------------

# Package names differ per distribution family, so each dependency declares a name per
# manager rather than assuming Debian's. A dependency with no entry for the detected
# manager is reported as action_required instead of guessed at.
PACKAGE_MANAGERS = (
    {"id": "apt", "probe": "apt-get",
     "install": ["sudo", "apt-get", "install", "-y"],
     "refresh": ["sudo", "apt-get", "update"]},
    {"id": "pamac", "probe": "pamac",
     "install": ["sudo", "pamac", "install", "--no-confirm"],
     "refresh": None},
    {"id": "pacman", "probe": "pacman",
     "install": ["sudo", "pacman", "-S", "--noconfirm"],
     "refresh": ["sudo", "pacman", "-Sy"]},
    {"id": "dnf", "probe": "dnf",
     "install": ["sudo", "dnf", "install", "-y"],
     "refresh": None},
    {"id": "zypper", "probe": "zypper",
     "install": ["sudo", "zypper", "install", "-y"],
     "refresh": None},
)


def detect_package_manager() -> Optional[Dict[str, Any]]:
    """
    The first supported package manager present on this system, or ``None``.

    Order matters: ``pamac`` is checked before ``pacman`` because a Manjaro box has both and
    pamac is the one that resolves AUR/repo state the way that distribution expects.
    """
    for manager in PACKAGE_MANAGERS:
        if shutil.which(manager["probe"]):
            return manager
    return None


def _install_packages(package_map: Dict[str, str]) -> Dict[str, Any]:
    """Install a distribution package via the detected manager."""
    manager = detect_package_manager()
    if not manager:
        return _result(STATUS_ACTION_REQUIRED,
                       "No supported package manager was found (apt, pamac, pacman, dnf, zypper). "
                       "Install the package manually.")

    package = package_map.get(manager["id"])
    if not package:
        return _result(STATUS_ACTION_REQUIRED,
                       f"No known package name for this dependency on {manager['id']}. "
                       "Install it manually using your distribution's package.")

    log_system(f"Installing '{package}' via {manager['id']} (sudo may prompt for your password)...")
    try:
        if manager["refresh"]:
            subprocess.run(manager["refresh"], timeout=300)
        completed = subprocess.run(manager["install"] + package.split(), timeout=900)
    except subprocess.TimeoutExpired:
        return _result(STATUS_UNKNOWN, f"The {manager['id']} install did not finish in time.")
    except Exception as exc:
        return _result(STATUS_MISSING, f"Could not run {manager['id']}: {exc}")

    if completed.returncode == 0:
        return _result(STATUS_OK, f"Installed '{package}' via {manager['id']}.")
    return _result(STATUS_MISSING,
                   f"{manager['id']} failed to install '{package}' (exit {completed.returncode}).")


# --------------------------------------------------------------------------------------
# Individual probes
# --------------------------------------------------------------------------------------

def _library_loadable(*candidates: str) -> bool:
    """True when any of the named shared libraries can actually be loaded by this process."""
    for name in candidates:
        try:
            if platform.system() == "Windows":
                ctypes.WinDLL(name)  # type: ignore[attr-defined]
            else:
                ctypes.CDLL(name)
            return True
        except OSError:
            continue
        except Exception:
            continue
    return False


def check_vulkan() -> Dict[str, Any]:
    """
    Whether a Vulkan loader is present.

    Loading the library is the only honest test: a GPU driver can be installed and still not
    register an ICD, and the file existing on disk says nothing about whether it links.
    """
    if platform.system() == "Windows":
        if _library_loadable("vulkan-1.dll"):
            return _result(STATUS_OK, "vulkan-1.dll loaded successfully.")
        return _result(STATUS_MISSING, "vulkan-1.dll could not be loaded.")
    if _library_loadable("libvulkan.so.1", "libvulkan.so"):
        return _result(STATUS_OK, "libvulkan loaded successfully.")
    return _result(STATUS_MISSING, "libvulkan could not be loaded.")


def _install_vulkan_windows(consent: bool) -> Dict[str, Any]:
    """
    Install the LunarG Vulkan runtime.

    This is the fallback, not the fix: on Windows the Vulkan loader normally arrives with the
    GPU driver, so a missing loader usually means the driver is old or was installed without
    its Vulkan component. The runtime installer restores the loader either way, and the
    surfaced guidance still tells the user to update their driver.
    """
    url = resolve_download_url(VULKAN_RUNTIME_URL, VULKAN_RUNTIME_FILENAME)
    destination = os.path.join(tempfile.gettempdir(), VULKAN_RUNTIME_FILENAME)
    downloaded = _download(url, destination)
    if downloaded["status"] != STATUS_OK:
        return downloaded
    try:
        return _run_installer([destination, "/S"])
    finally:
        try:
            os.remove(destination)
        except OSError:
            pass


def check_webview2() -> Dict[str, Any]:
    """
    Whether the Edge WebView2 runtime is installed (the GUI wizard's host).

    Windows 11 ships it, Windows 10 usually has it via Edge, but a freshly imaged or LTSC
    machine may not — and without it the pywebview window silently fails to open, which is
    exactly the situation the console fallback exists for. Detecting it up front turns that
    into an offer instead of a mystery.
    """
    if platform.system() != "Windows":
        return _result(STATUS_UNKNOWN, "Not applicable on this platform.")
    try:
        import winreg  # type: ignore
    except ImportError:
        return _result(STATUS_UNKNOWN, "The registry is unreadable from this process.")

    locations = (
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}"),
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}"),
        (winreg.HKEY_CURRENT_USER, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}"),
    )
    for hive, path in locations:
        try:
            with winreg.OpenKey(hive, path) as key:
                version, _ = winreg.QueryValueEx(key, "pv")
                # EdgeUpdate leaves the key behind with pv="0.0.0.0" after a removal.
                if version and str(version) != "0.0.0.0":
                    return _result(STATUS_OK, f"WebView2 runtime {version}.")
        except OSError:
            continue
    return _result(STATUS_MISSING, "The WebView2 runtime is not installed.")


def _install_webview2(consent: bool) -> Dict[str, Any]:
    """Run Microsoft's evergreen bootstrapper, which pulls the runtime itself."""
    url = resolve_download_url(WEBVIEW2_BOOTSTRAP_URL, WEBVIEW2_BOOTSTRAP_FILENAME)
    destination = os.path.join(tempfile.gettempdir(), WEBVIEW2_BOOTSTRAP_FILENAME)
    downloaded = _download(url, destination)
    if downloaded["status"] != STATUS_OK:
        return downloaded
    try:
        return _run_installer([destination, "/silent", "/install"])
    finally:
        try:
            os.remove(destination)
        except OSError:
            pass


def _nvidia_driver_version() -> Optional[float]:
    """
    The installed NVIDIA driver version as a float (``580.65`` from ``580.65.06``), or None.

    Only the first two components are compared, because that is the granularity NVIDIA
    publishes CUDA minimums at.
    """
    if not shutil.which("nvidia-smi"):
        return None
    try:
        completed = subprocess.run(
            ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=15, **_hidden_process_kwargs()
        )
    except Exception:
        return None
    if completed.returncode != 0:
        return None

    raw = (completed.stdout or "").strip().splitlines()
    if not raw:
        return None
    parts = raw[0].strip().split(".")
    if len(parts) < 2:
        return None
    try:
        return float(f"{int(parts[0])}.{parts[1].strip()}")
    except ValueError:
        return None


def cuda_floor_for_variant(variant: str) -> Optional[float]:
    """The driver floor a CUDA release variant needs, or None for non-CUDA variants."""
    if "cuda" not in (variant or ""):
        return None
    for family, floor in sorted(CUDA_DRIVER_FLOORS.items(), key=lambda item: -len(item[0])):
        if f"cuda-{family}" in variant:
            return floor
    return None


def check_nvidia_driver(variant: str) -> Dict[str, Any]:
    """
    Whether the NVIDIA driver is new enough for the CUDA build this node is running.

    A driver below the floor is reported as ``action_required``, never as something to
    install: pushing a GPU driver onto someone's machine unattended can black-screen it, and
    the honest alternative — run the Vulkan variant instead — costs the user nothing.
    """
    floor = cuda_floor_for_variant(variant)
    if floor is None:
        return _result(STATUS_OK, "Not a CUDA build.")

    installed = _nvidia_driver_version()
    if installed is None:
        return _result(STATUS_ACTION_REQUIRED,
                       "No NVIDIA driver was detected. A CUDA build cannot run without one — "
                       "install the driver from nvidia.com, or switch to the Vulkan build.")
    if installed + 1e-9 < floor:
        return _result(STATUS_ACTION_REQUIRED,
                       f"NVIDIA driver {installed:g} is older than the {floor:g} this CUDA build "
                       "requires. Update the driver from nvidia.com, or switch to the Vulkan build "
                       "which has no driver floor.")
    return _result(STATUS_OK, f"NVIDIA driver {installed:g} meets the {floor:g} floor.")


def check_wireguard() -> Dict[str, Any]:
    """Whether WireGuard is installed (WAN roles route through it)."""
    system = platform.system()
    if system == "Windows":
        if shutil.which("wireguard") or os.path.exists(
                os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "WireGuard", "wireguard.exe")):
            return _result(STATUS_OK, "WireGuard is installed.")
        return _result(STATUS_MISSING, "WireGuard is not installed.")
    if shutil.which("wg") and shutil.which("wg-quick"):
        return _result(STATUS_OK, "wireguard-tools is installed.")
    return _result(STATUS_MISSING, "wireguard-tools is not installed.")


def _install_wireguard(consent: bool) -> Dict[str, Any]:
    """
    Install WireGuard for the current platform.

    This is the install half of what ``setup_cli.auto_install_wireguard`` used to do inline;
    that function now brings up the *tunnel* and delegates the install here, so there is one
    implementation of "get WireGuard onto this machine".
    """
    system = platform.system()
    if system == "Windows":
        url = resolve_download_url(WIREGUARD_WINDOWS_URL, WIREGUARD_WINDOWS_FILENAME)
        destination = os.path.join(tempfile.gettempdir(), WIREGUARD_WINDOWS_FILENAME)
        downloaded = _download(url, destination)
        if downloaded["status"] != STATUS_OK:
            return downloaded
        try:
            # /S is silent but still elevates — the UAC prompt is the user's second consent.
            return _run_installer([destination, "/S"])
        finally:
            try:
                os.remove(destination)
            except OSError:
                pass

    if system == "Darwin":
        if not shutil.which("brew"):
            return _result(STATUS_ACTION_REQUIRED,
                           "Homebrew is not installed. Install Homebrew, then run "
                           "'brew install wireguard-tools'.")
        try:
            completed = subprocess.run(["brew", "install", "wireguard-tools"], timeout=900)
        except Exception as exc:
            return _result(STATUS_MISSING, f"brew failed: {exc}")
        return (_result(STATUS_OK, "Installed wireguard-tools via Homebrew.")
                if completed.returncode == 0
                else _result(STATUS_MISSING, f"brew exited with code {completed.returncode}."))

    return _install_packages({
        "apt": "wireguard",
        "pamac": "wireguard-tools",
        "pacman": "wireguard-tools",
        "dnf": "wireguard-tools",
        "zypper": "wireguard-tools",
    })


def check_webkit_gtk() -> Dict[str, Any]:
    """
    Whether Linux can host the pywebview GUI wizard.

    Two things are needed and both are commonly absent on a server install: the PyGObject
    bindings and the WebKit2GTK library itself. Missing either is not an error — it means the
    wizard uses its console fallback — so this is surfaced as an offer, not a blocker.
    """
    if platform.system() != "Linux":
        return _result(STATUS_UNKNOWN, "Not applicable on this platform.")
    try:
        import gi  # type: ignore  # noqa: F401
    except ImportError:
        return _result(STATUS_MISSING, "PyGObject (python3-gi) is not installed.")
    if _library_loadable("libwebkit2gtk-4.1.so.0", "libwebkit2gtk-4.0.so.37", "libwebkit2gtk-6.0.so.4"):
        return _result(STATUS_OK, "WebKit2GTK is available.")
    return _result(STATUS_MISSING, "WebKit2GTK is not installed.")


def _install_webkit_gtk(consent: bool) -> Dict[str, Any]:
    return _install_packages({
        "apt": "python3-gi python3-gi-cairo gir1.2-webkit2-4.1",
        "pamac": "python-gobject webkit2gtk",
        "pacman": "python-gobject webkit2gtk",
        "dnf": "python3-gobject webkit2gtk4.1",
        "zypper": "python3-gobject typelib-1_0-WebKit2-4_1",
    })


def _install_vulkan_linux(consent: bool) -> Dict[str, Any]:
    return _install_packages({
        "apt": "libvulkan1",
        "pamac": "vulkan-icd-loader",
        "pacman": "vulkan-icd-loader",
        "dnf": "vulkan-loader",
        "zypper": "libvulkan1",
    })


def check_vcredist() -> Dict[str, Any]:
    """
    Whether the Microsoft Visual C++ runtime the engine binaries link against is present.

    Almost every Windows machine has it, which is why this is a cheap DLL-load check rather
    than a registry walk: if ``vcruntime140.dll`` loads, the engine's imports resolve.
    """
    if platform.system() != "Windows":
        return _result(STATUS_UNKNOWN, "Not applicable on this platform.")
    if _library_loadable("vcruntime140.dll", "vcruntime140_1.dll"):
        return _result(STATUS_OK, "The Visual C++ runtime is present.")
    return _result(STATUS_MISSING, "The Visual C++ 2015-2022 runtime is missing.")


def _install_vcredist(consent: bool) -> Dict[str, Any]:
    url = resolve_download_url(VCREDIST_URL, VCREDIST_FILENAME)
    destination = os.path.join(tempfile.gettempdir(), VCREDIST_FILENAME)
    downloaded = _download(url, destination)
    if downloaded["status"] != STATUS_OK:
        return downloaded
    try:
        return _run_installer([destination, "/install", "/quiet", "/norestart"])
    finally:
        try:
            os.remove(destination)
        except OSError:
            pass


# --------------------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------------------

# Written next to the install by the old bundle builder; says which build the node is. Source
# runs have no such file and fall back to detecting the hardware.
RELEASE_INFO_FILE = "release_info.json"

# The build variants a node can be, as the dependency checks below name them.
KNOWN_VARIANTS = (
    "windows-vulkan",
    "windows-cuda-11.8",
    "windows-cuda-13",
    "linux-vulkan",
    "mac-applesilicon",
)


def resolve_variant(install_dir: Optional[str] = None) -> str:
    """
    Which build variant this installation is, so the right dependencies are checked.

    ``release_info.json`` is authoritative when present: it records what was actually
    installed, so a user who chose the Vulkan build on NVIDIA hardware keeps Vulkan checks.
    Only when the file is missing (source runs, hand-rolled installs) does hardware detection
    guess, through the same ``get_gpu_type()`` the engine-binary selector already trusts.
    """
    base = install_dir or get_base_dir()
    info_path = os.path.join(base, RELEASE_INFO_FILE)
    try:
        with open(info_path, "r", encoding="utf-8") as handle:
            info = json.load(handle)
        variant = str(info.get("variant", "")).strip().lower()
        if variant in KNOWN_VARIANTS:
            return variant
        if variant:
            log_warning(f"{RELEASE_INFO_FILE} names an unknown variant - falling back to hardware detection.")
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        log_warning(f"Could not read {RELEASE_INFO_FILE}: {exc} - falling back to hardware detection.")

    system = platform.system()
    if system == "Darwin":
        return "mac-applesilicon"
    if system == "Linux":
        return "linux-vulkan"

    # Windows: the CUDA build matching the detected driver generation, otherwise Vulkan, which
    # runs on every vendor. Deferred import: engine.hardware probes subprocesses at
    # import-adjacent times and this module must stay importable in minimal environments.
    try:
        from engine.hardware import get_gpu_type
        gpu_type = get_gpu_type()
    except Exception:
        gpu_type = "cpu"
    if gpu_type == "nvidia-12":
        return "windows-cuda-13"
    if gpu_type == "nvidia-11":
        return "windows-cuda-11.8"
    return "windows-vulkan"


def build_context(variant: str = "", mode: str = "", needs_gui: bool = False,
                  needs_wan: bool = False) -> Dict[str, Any]:
    """
    Describe the machine and the install being checked.

    Args:
        variant: A release variant id (``windows-vulkan``, ``windows-cuda-13``, ...). Empty
            resolves it from ``release_info.json`` / hardware detection (:func:`resolve_variant`),
            which is what a running node wants; the installer passes the variant it is about
            to download instead, since that build is not on disk yet.
        mode: The DLNE role this node will run (``worker_only``, ``coordinator_hybrid``, ...).
        needs_gui: True when the caller intends to open the pywebview wizard.
        needs_wan: True when the node routes over the internet and therefore needs WireGuard.
    """
    if not variant:
        try:
            variant = resolve_variant()
        except Exception:
            # Variant resolution is advisory here; an unknown variant simply skips the
            # variant-specific entries rather than failing the whole preflight.
            variant = ""
    return {
        "system": platform.system(),
        "variant": variant,
        "mode": mode,
        "needs_gui": bool(needs_gui),
        "needs_wan": bool(needs_wan),
        "mirror": mirror_base_url(),
    }


def _is_windows(ctx: Dict[str, Any]) -> bool:
    return ctx.get("system") == "Windows"


def _is_linux(ctx: Dict[str, Any]) -> bool:
    return ctx.get("system") == "Linux"


# Each entry is: what it is, why the user should care, when it applies, how to test it, and
# (optionally) how to install it. Anything with install=None is informational-only and can
# never be auto-installed — see check_nvidia_driver for why that matters.
DEPENDENCIES: List[Dict[str, Any]] = [
    {
        "id": "vcredist",
        "label": "Visual C++ Runtime",
        "why": "The DLNE engine binaries are compiled C++ and will not start without it.",
        "applies": lambda ctx: _is_windows(ctx),
        "check": lambda ctx: check_vcredist(),
        "install": _install_vcredist,
    },
    {
        "id": "vulkan_runtime",
        "label": "Vulkan Runtime",
        "why": "GPU acceleration for the Vulkan build. Without it DLNE falls back to CPU-only, "
               "which is dramatically slower.",
        "applies": lambda ctx: _is_windows(ctx) and "cuda" not in ctx.get("variant", ""),
        "check": lambda ctx: check_vulkan(),
        "install": _install_vulkan_windows,
        "hint": "Vulkan normally comes with your GPU driver — updating the driver is the better fix.",
    },
    {
        "id": "vulkan_loader",
        "label": "Vulkan Loader (libvulkan)",
        "why": "GPU acceleration on Linux. Without it DLNE falls back to CPU-only.",
        "applies": lambda ctx: _is_linux(ctx),
        "check": lambda ctx: check_vulkan(),
        "install": _install_vulkan_linux,
    },
    {
        "id": "nvidia_driver",
        "label": "NVIDIA Driver",
        "why": "The CUDA build refuses to initialise on a driver older than its CUDA version needs.",
        "applies": lambda ctx: "cuda" in ctx.get("variant", ""),
        "check": lambda ctx: check_nvidia_driver(ctx.get("variant", "")),
        "install": None,
    },
    {
        "id": "webview2",
        "label": "Edge WebView2 Runtime",
        "why": "Hosts the graphical setup wizard. Without it setup runs in the console instead.",
        "applies": lambda ctx: _is_windows(ctx) and ctx.get("needs_gui"),
        "check": lambda ctx: check_webview2(),
        "install": _install_webview2,
    },
    {
        "id": "webkit2gtk",
        "label": "WebKit2GTK + PyGObject",
        "why": "Hosts the graphical setup wizard on Linux. Without it setup runs in the console instead.",
        "applies": lambda ctx: _is_linux(ctx) and ctx.get("needs_gui"),
        "check": lambda ctx: check_webkit_gtk(),
        "install": _install_webkit_gtk,
    },
    {
        "id": "wireguard",
        "label": "WireGuard",
        "why": "Encrypts this node's link to the Coordinator when it joins over the internet.",
        "applies": lambda ctx: bool(ctx.get("needs_wan")),
        "check": lambda ctx: check_wireguard(),
        "install": _install_wireguard,
    },
]


def _find(dependency_id: str) -> Optional[Dict[str, Any]]:
    for dependency in DEPENDENCIES:
        if dependency["id"] == dependency_id:
            return dependency
    return None


def preflight(ctx: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """
    Check every dependency that applies to ``ctx`` and return one status entry each.

    The entries are JSON-safe on purpose: the GUI wizard's System Check page renders them
    directly and the console wizard prints the same fields as a table, so neither surface can
    drift from the other's idea of what is required.
    """
    ctx = ctx or build_context()
    report: List[Dict[str, Any]] = []
    for dependency in DEPENDENCIES:
        try:
            if not dependency["applies"](ctx):
                continue
        except Exception:
            continue

        try:
            outcome = dependency["check"](ctx)
        except Exception as exc:
            # A probe that throws is a bug in the probe, not a missing dependency; saying
            # "unknown" keeps the wizard usable while still showing something is off.
            outcome = _result(STATUS_UNKNOWN, f"The check failed: {exc}")

        report.append({
            "id": dependency["id"],
            "label": dependency["label"],
            "why": dependency["why"],
            "hint": dependency.get("hint", ""),
            "status": outcome["status"],
            "detail": outcome.get("detail", ""),
            "can_install": dependency["install"] is not None and outcome["status"] == STATUS_MISSING,
        })
    return report


def install(dependency_id: str, ctx: Optional[Dict[str, Any]] = None,
            consent: bool = False) -> Dict[str, Any]:
    """
    Install one dependency — only ever with explicit consent.

    ``consent`` is a required act, not a formality: this function downloads and executes a
    third-party installer, so it refuses outright rather than defaulting to yes. The caller
    passing ``True`` is asserting that a human clicked a button or typed ``Y`` after reading
    the dependency's ``why`` text.
    """
    dependency = _find(dependency_id)
    if not dependency:
        return _result(STATUS_UNKNOWN, f"Unknown dependency '{dependency_id}'.")
    if dependency["install"] is None:
        return _result(STATUS_ACTION_REQUIRED,
                       f"{dependency['label']} cannot be installed automatically. "
                       f"{dependency['why']}")
    if not consent:
        return _result(STATUS_ACTION_REQUIRED,
                       f"{dependency['label']} was not installed because consent was not given.")

    ctx = ctx or build_context()
    log_system(f"Installing dependency: {dependency['label']}...")
    try:
        outcome = dependency["install"](True)
    except Exception as exc:
        return _result(STATUS_MISSING, f"The installation failed: {exc}")

    if outcome["status"] == STATUS_OK:
        # Re-probe rather than trusting the installer's exit code: a silent installer can
        # return 0 having installed nothing useful (wrong architecture, blocked by policy),
        # and the wizard must show what is actually true now.
        verified = dependency["check"](ctx)
        if verified["status"] == STATUS_OK:
            log_success(f"{dependency['label']} is installed.")
            return _result(STATUS_OK, verified.get("detail", "Installed."))
        log_warning(f"{dependency['label']} installer succeeded but the check still fails.")
        return _result(verified["status"],
                       f"The installer completed, but {dependency['label']} is still not detected. "
                       f"{verified.get('detail', '')}".strip())

    log_error(f"{dependency['label']} could not be installed: {outcome.get('detail', '')}")
    return outcome


def console_install_flow(dependency_id: str, ctx: Optional[Dict[str, Any]] = None,
                         prompt: Optional[Callable[[str], str]] = None) -> bool:
    """
    Ask on the console whether to install a missing dependency, then do it.

    This is the entry point for code that discovers a missing dependency *at runtime* rather
    than during setup — chiefly ``engine.downloader``, which used to carry its own Linux-only
    copy of this conversation. Returns True only when the dependency ends up present.

    A non-interactive process (a service, a piped launch) gets False without hanging on a
    prompt that nobody will ever answer.
    """
    dependency = _find(dependency_id)
    if not dependency:
        return False

    ctx = ctx or build_context()
    try:
        current = dependency["check"](ctx)
    except Exception:
        return False
    if current["status"] == STATUS_OK:
        return True

    if dependency["install"] is None or current["status"] == STATUS_ACTION_REQUIRED:
        log_error(f"{dependency['label']}: {current.get('detail', '')}")
        return False

    if not sys.stdin or not sys.stdin.isatty():
        log_warning(f"{dependency['label']} is missing and there is no terminal to ask on. "
                    "Re-run setup to install it.")
        return False

    print(f"\n[?] Missing dependency: {dependency['label']}")
    print(f"    {dependency['why']}")
    if dependency.get("hint"):
        print(f"    {dependency['hint']}")
    try:
        answer = (prompt or input)("    Install it now? (Y/n): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    if answer not in ("", "y", "yes"):
        log_system(f"User declined the {dependency['label']} install.")
        return False

    return install(dependency_id, ctx, consent=True)["status"] == STATUS_OK


def summarize(report: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Roll a preflight report up into the two numbers a wizard page header needs.

    ``blocking`` counts only ``action_required`` entries: a *missing* dependency is one the
    user can fix right there with a button, whereas ``action_required`` means they have to
    leave and do something (update a driver, install Homebrew) before this node runs well.
    """
    missing = [entry for entry in report if entry["status"] == STATUS_MISSING]
    blocking = [entry for entry in report if entry["status"] == STATUS_ACTION_REQUIRED]
    return {
        "total": len(report),
        "ok": len([entry for entry in report if entry["status"] == STATUS_OK]),
        "missing": len(missing),
        "blocking": len(blocking),
        "all_clear": not missing and not blocking,
    }
