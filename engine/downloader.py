import errno
import hashlib
import http.client
import os
import random
import re
import shutil
import ssl
import stat
import sys
import platform
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional
from engine.logger import log_system, log_success, log_error, log_warning

# The llama.cpp build is not chosen here. It is pinned per release by scripts/package_llama.py
# and named in the coordinator's signed release (llama_build / rpc_proto); sync_rpc_bin below
# installs exactly that build.

# Windows' loader exits a program with STATUS_DLL_NOT_FOUND (0xC0000135) when a DLL it imports
# is not on the machine. llama.cpp's Windows builds import the Visual C++ runtime
# (VCRUNTIME140, VCRUNTIME140_1, MSVCP140), which a clean Windows does not have.
DLL_NOT_FOUND_CODES = (0xC0000135, -1073741515)
VCREDIST_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
# One line of the worker's output the VS Code extension and the tray app act on (like DLNE_ROLE).
VCREDIST_MARKER = "DLNE_NEEDS: vcredist"
VCREDIST_TEXT = ("The Visual C++ Runtime is missing from this PC, so the DLNE engine cannot start. "
                 f"Install \"Microsoft Visual C++ Redistributable (x64)\" from {VCREDIST_URL} and start DLNE again.")


def is_dll_not_found(code: Optional[int]) -> bool:
    """A Windows exit code meaning \"a DLL this program imports was not found\" (0xC0000135)."""
    return code in DLL_NOT_FOUND_CODES


def vcruntime_present(bin_dir: str = "") -> bool:
    """Whether the Visual C++ runtime the engine needs can be loaded here (always True off Windows).

    True when ``bin_dir`` carries its own copy (a release that ships the runtime beside the
    programs) or when Windows can load VCRUNTIME140 and MSVCP140 from the system.
    """
    if platform.system() != "Windows":
        return True
    if bin_dir and all(os.path.exists(os.path.join(bin_dir, n)) for n in ("vcruntime140.dll", "msvcp140.dll")):
        return True
    try:
        import ctypes
        for name in ("vcruntime140.dll", "msvcp140.dll"):
            ctypes.WinDLL(name)
        return True
    except OSError:
        return False
    except Exception:  # noqa: BLE001 - cannot tell: do not claim it is missing
        return True


def explain_dll_not_found(binary_path: str, code: int) -> str:
    """What to tell the user when a program exits 0xC0000135. Never blames the CPU."""
    if platform.system() == "Windows" and not vcruntime_present(os.path.dirname(binary_path)):
        return VCREDIST_TEXT
    return (f"{os.path.basename(binary_path)} could not start because Windows could not find a DLL it needs "
            f"(code {code}). The most common cause is the Visual C++ Runtime: install it from {VCREDIST_URL}. "
            "A missing Vulkan runtime or a damaged bin folder gives the same code.")


# Why the last candidate in download_and_extract_engine was refused, so its closing message
# names the real cause. "" when there was none worth naming.
_last_failure = ""


def test_binary_compatibility(binary_path: str, _allow_dependency_install: bool = True) -> bool:
    """Tests if a binary can run on the host CPU without an illegal instruction crash or missing libs.

    ``_allow_dependency_install`` is internal: when a missing Vulkan loader is detected the
    function offers to install it and then re-tests the binary, and this flag makes that
    retry a single hop. Without it a loader that installs "successfully" but still does not
    satisfy the binary would send the function into unbounded recursion.
    """
    global _last_failure
    os_name = platform.system()
    if os_name == "Windows":
        import ctypes
        try:
            # 0x0001 (SEM_FAILCRITICALERRORS) | 0x0002 (SEM_NOGPFAULTERRORBOX) | 0x8000 (SEM_NOOPENFILEERRORBOX)
            ctypes.windll.kernel32.SetErrorMode(0x8003)
        except Exception:
            pass
            
    try:
        import subprocess
        kwargs = {}
        if os_name == "Windows":
            kwargs["creationflags"] = 0x08000000 # CREATE_NO_WINDOW
        result = subprocess.run([binary_path, "--help"], capture_output=True, text=True, **kwargs)
        
        if result.returncode == 0 or result.returncode == 1:
            return True
            
        err_msg = result.stderr.strip()
        if "libvulkan" in err_msg.lower() or "vulkan-1" in err_msg.lower():
            log_error("Missing Vulkan Runtime! The engine requires this library for hardware acceleration.")
            # The install conversation — which package manager, which package name, and the
            # consent prompt itself — lives in engine.depends, which is also what the setup
            # wizard's System Check page drives. This path used to carry its own Linux-only
            # copy of that logic, so a Windows node with no Vulkan loader simply died here.
            if _allow_dependency_install:
                from engine import depends
                dependency_id = "vulkan_loader" if os_name == "Linux" else "vulkan_runtime"
                log_system("Without it the engine falls back to a much slower Pure CPU mode.")
                if depends.console_install_flow(dependency_id):
                    log_success("Vulkan runtime installed successfully! Retrying...")
                    return test_binary_compatibility(binary_path, _allow_dependency_install=False)
                log_system("Falling back to Pure CPU tier...")
            return False
            
        elif os_name == "Windows" and is_dll_not_found(result.returncode):
            message =explain_dll_not_found(binary_path, result.returncode)
            _last_failure = "vcredist" if message == VCREDIST_TEXT else "dll"
            log_error(f"Missing DLL [Code {result.returncode}] for {os.path.basename(binary_path)}: {message}")
            if _last_failure == "vcredist":
                print(VCREDIST_MARKER, flush=True)
        elif result.returncode == 127 or is_dll_not_found(result.returncode):
            log_error(f"Missing Shared Library (DLL) [Code {result.returncode}] for {os.path.basename(binary_path)}: {err_msg}")
            log_error("Hint: Ensure required shared libraries (like libgomp, libvulkan or vulkan-1.dll) are in the 'bin' folder.")
        elif result.returncode == 132 or result.returncode == 3221225493 or result.returncode == -1073741795:
            log_error(f"Illegal Instruction Crash [Code {result.returncode}] for {os.path.basename(binary_path)} (Missing CPU instructions like AVX/AVX2).")
            if "-cpu" in binary_path:
                log_error("FATAL: Your CPU does not support AVX2, which is a strict minimum requirement for DLNE. The engine cannot start.")
        else:
            log_error(f"Engine Test Failed (Code {result.returncode}): {err_msg}")
            
        return False
    except Exception as e:
        log_error(f"Failed to execute binary test for {os.path.basename(binary_path)}: {e}")
        return False



RPC_RECORD = ".dlne_rpc_bin.json"   # shared with the VS Code extension, which writes the same record

# Engine flavours (2.7). "vulkan" is the engine as it always was, in <base>/bin. "cuda" is llama.cpp's official CUDA 13
# build of the same tag, signed into the release as cuda_<platform>_*, and it lives in a folder of its own,
# <base>/bin-cuda, with a record of its own: both can sit on a disk while a machine switches, and installing one never
# touches the other. Anything but "cuda" means vulkan (the contract: missing or unknown is vulkan).
CUDA_RECORD = ".dlne_cuda_bin.json"
ENGINE_DIRS = {"vulkan": "bin", "cuda": "bin-cuda"}
_MIN_FREE_FOR_CUDA = 2.3   # the zip, then the files out of it, and some room; times the signed size


def normalize_flavour(value: Any) -> str:
    return "cuda" if isinstance(value, str) and value.strip().lower() == "cuda" else "vulkan"


def engine_dir(base_dir: str, flavour: str = "vulkan") -> str:
    """The folder a flavour's engine programs are in."""
    return os.path.join(base_dir, ENGINE_DIRS[normalize_flavour(flavour)])


def remove_engine_flavour(base_dir: str, flavour: str) -> bool:
    """
    Delete one flavour's engine and its record, to give the disk back after a switch (the caller does this once the
    other flavour is running). Returns True when something was removed. A folder that is a symlink is never followed
    or removed: on the coordinator's host ``bin`` points at the release's own build and is not a download.
    """
    flavour = normalize_flavour(flavour)
    folder = engine_dir(base_dir, flavour)
    removed = False
    if os.path.isdir(folder) and not os.path.islink(folder):
        shutil.rmtree(folder, ignore_errors=True)
        removed = True
    record = os.path.join(base_dir, CUDA_RECORD if flavour == "cuda" else RPC_RECORD)
    if os.path.isfile(record):
        try:
            os.remove(record)
            removed = True
        except OSError:
            pass
    return removed


def fetch_verified_manifest(coord_url: str, urlopen=urllib.request.urlopen) -> dict:
    """The coordinator's release manifest, verified against the baked-in release key."""
    import json
    from engine import release

    with urlopen(f"{coord_url}/v1/release", timeout=15) as response:
        manifest = json.loads(response.read(1_000_000))
    ok, reason = release.verify_manifest(manifest)
    if not ok:
        raise release.ReleaseError(f"the coordinator's release failed verification ({reason})")
    return manifest


def _install_cuda_zip(zip_path: str, base_dir: str) -> None:
    """
    Unpack a verified CUDA zip (bin/<flat name> entries) into <base>/bin-cuda. It is unpacked beside the old folder
    and swapped in, so a failed unpack leaves what was installed as it was and a running engine is never half-replaced.
    """
    target = engine_dir(base_dir, "cuda")
    staging = f"{target}.new"
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging)
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = info.filename[len("bin/"):]    # check_cuda_bin has proved every entry is bin/<flat name>
                with zf.open(info) as src, open(os.path.join(staging, name), "wb") as out:
                    shutil.copyfileobj(src, out, 1024 * 1024)
        if not sys.platform.startswith("win"):
            for name in os.listdir(staging):
                if name in ("ggml-rpc-server", "llama-server"):
                    os.chmod(os.path.join(staging, name), 0o755)
        shutil.rmtree(target, ignore_errors=True)
        os.replace(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def sync_local_cuda_bin(base_dir: str, platform_key: str) -> bool:
    """
    The coordinator's own host (2.7): install the CUDA engine from the zip the release installer put beside the code
    (<base>/cuda-bin-<platform>.zip), checked against the signed manifest in <base>/release.json. Nothing is
    downloaded. Returns True when it installed one, False when the installed one is already it; raises ReleaseError
    when the release signs no CUDA engine for this platform or the zip is not the signed one.
    """
    import json
    from engine import release

    manifest = release.load_manifest(os.path.join(base_dir, release.MANIFEST_NAME))
    ok, reason = release.verify_manifest(manifest) if manifest is not None else (False, "there is no release manifest here")
    if not ok:
        raise release.ReleaseError(f"this install's release failed verification ({reason})")
    sha = manifest.get(release.cuda_field(platform_key, "sha256"))
    if sha is None:
        raise release.ReleaseError(f"this install's signed release has no CUDA engine for {platform_key}")
    exe = "llama-server.exe" if platform_key.startswith("win") else "llama-server"
    try:
        with open(os.path.join(base_dir, CUDA_RECORD), "r", encoding="utf-8") as handle:
            if json.load(handle).get("sha256") == sha and os.path.exists(os.path.join(engine_dir(base_dir, "cuda"), exe)):
                return False
    except (OSError, ValueError, AttributeError):
        pass
    zip_path = os.path.join(base_dir, release.cuda_zip_name(platform_key))
    ok, reason = release.verify_cuda_bin(manifest, platform_key, zip_path)
    if not ok:
        raise release.ReleaseError(reason)
    release.check_cuda_bin(zip_path)
    _install_cuda_zip(zip_path, base_dir)
    with open(os.path.join(base_dir, CUDA_RECORD), "w", encoding="utf-8") as handle:
        json.dump({"sha256": sha, "llama_build": manifest.get("llama_build"), "rpc_proto": manifest.get("rpc_proto"),
                   "cuda_version": manifest.get("cuda_version")}, handle)
    log_success(f"Installed the CUDA {manifest.get('cuda_version')} engine (llama.cpp {manifest.get('llama_build')}).")
    return True


def sync_rpc_bin(coord_url: str, base_dir: str, platform_key: str, urlopen=urllib.request.urlopen,
                 flavour: str = "vulkan") -> bool:
    """
    Make ``base_dir``/bin hold the ggml-rpc-server build the coordinator's signed release names
    for this platform. Returns True when it installed one, False when the installed one is
    already it.

    ``flavour="cuda"`` (2.7) does the same for the CUDA engine the release signs as cuda_<platform>_*: it is fetched
    from /v1/download/cuda-bin/<platform>, held to the same checks, and installed in <base>/bin-cuda with
    its own record, leaving ``bin`` exactly as it was.

    These are executables the worker runs, so "the coordinator served it" is not enough: the
    manifest must verify against the baked-in release key, the zip must have exactly the
    signed size and SHA-256, and hold nothing but bin/<server and its libraries>. Raises
    ReleaseError otherwise, having written nothing outside a temporary download that is
    always removed. This is also how a worker follows a llama.cpp upgrade: a new release names
    a new zip, and the next start installs it.
    """
    import json
    from engine import release

    cuda = normalize_flavour(flavour) == "cuda"
    field = release.cuda_field if cuda else release.rpc_field
    manifest = fetch_verified_manifest(coord_url, urlopen)
    sha = manifest.get(field(platform_key, "sha256"))
    if sha is None:
        raise release.ReleaseError(f"the coordinator's signed release has no {'CUDA engine' if cuda else 'RPC build'} for {platform_key}")

    record_path = os.path.join(base_dir, CUDA_RECORD if cuda else RPC_RECORD)
    exe = "ggml-rpc-server.exe" if platform_key.startswith("win") else "ggml-rpc-server"
    try:
        with open(record_path, "r", encoding="utf-8") as handle:
            if json.load(handle).get("sha256") == sha and os.path.exists(os.path.join(engine_dir(base_dir, flavour), exe)):
                return False
    except (OSError, ValueError, AttributeError):
        pass

    zip_dest = os.path.join(base_dir, "cuda-bin.zip.download" if cuda else "rpc-bin.zip.download")
    size = manifest[field(platform_key, "size")]
    size_mb = size // (1024 * 1024)
    if cuda:
        # 600 MB, then the same again unpacked: say so before starting rather than after an hour of download.
        free = shutil.disk_usage(base_dir).free
        if free < size * _MIN_FREE_FOR_CUDA:
            raise release.ReleaseError(f"not enough free disk space for the CUDA engine: it needs about "
                                       f"{int(size * _MIN_FREE_FOR_CUDA) // (1024 * 1024)} MB and {free // (1024 * 1024)} MB are free")
    log_system(f"Downloading the signed llama.cpp {manifest.get('llama_build')} {'CUDA ' + str(manifest.get('cuda_version')) if cuda else 'RPC'} "
               f"engine for {platform_key} ({size_mb} MB)...")
    try:
        with urlopen(f"{coord_url}/v1/download/{'cuda-bin' if cuda else 'rpc-bin'}/{platform_key}", timeout=120) as response, \
                open(zip_dest, "wb") as out:
            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                out.write(chunk)
        ok, reason = (release.verify_cuda_bin if cuda else release.verify_rpc_bin)(manifest, platform_key, zip_dest)
        if not ok:
            raise release.ReleaseError(reason)
        if cuda:
            release.check_cuda_bin(zip_dest)
            _install_cuda_zip(zip_dest, base_dir)
            with open(record_path, "w", encoding="utf-8") as handle:
                json.dump({"sha256": sha, "llama_build": manifest.get("llama_build"), "rpc_proto": manifest.get("rpc_proto"),
                           "cuda_version": manifest.get("cuda_version")}, handle)
            log_success(f"Installed the CUDA {manifest.get('cuda_version')} engine (llama.cpp {manifest.get('llama_build')}, "
                        f"RPC {manifest.get('rpc_proto')}).")
            return True
        release.check_rpc_bin(zip_dest)
        with zipfile.ZipFile(zip_dest, "r") as zf:
            zf.extractall(base_dir)   # every entry is bin/<flat name>, checked just above
        if not platform_key.startswith("win"):
            os.chmod(os.path.join(base_dir, "bin", exe), 0o755)
            # The CPU pool's server ships in the same zip (2.3.0).
            app_server = os.path.join(base_dir, "bin", "llama-server")
            if os.path.exists(app_server):
                os.chmod(app_server, 0o755)
        with open(record_path, "w", encoding="utf-8") as handle:
            json.dump({"sha256": sha, "llama_build": manifest.get("llama_build"), "rpc_proto": manifest.get("rpc_proto")}, handle)
        log_success(f"Installed llama.cpp {manifest.get('llama_build')} ggml-rpc-server (RPC {manifest.get('rpc_proto')}).")
        return True
    finally:
        try:
            os.remove(zip_dest)
        except OSError:
            pass


def _download_cuda_engine(role: str, coordinator_url: Optional[str], base_dir: str, os_name: str, ext: str,
                          target_bin_base: str, allowed_gpu: str) -> Optional[str]:
    """
    The flavour="cuda" half of download_and_extract_engine: install the CUDA engine the signed release names for
    this platform into <base>/bin-cuda (from the coordinator, or on the coordinator's own host from the zip beside
    the code) and return the program, or None when this machine cannot have it. It never falls back to the Vulkan
    folder by itself: whoever asked for CUDA decides what a "no" means (the worker starts Vulkan and says why).
    """
    global _last_failure
    from engine import release

    if allowed_gpu == 'none':
        log_system("CUDA engine skipped: this worker is set to CPU only.")
        return None
    platform_key = release.host_cuda_platform()
    if platform_key is None:
        log_error(f"DLNE ships no CUDA engine for {platform.system()} {platform.machine()}.")
        return None
    try:
        if role != "coordinator" and coordinator_url:
            sync_rpc_bin(coordinator_url.rstrip("/"), base_dir, platform_key, flavour="cuda")
        else:
            sync_local_cuda_bin(base_dir, platform_key)
    except Exception as sync_err:  # noqa: BLE001 - an installed CUDA engine keeps working offline
        log_error(f"Could not update the CUDA engine: {sync_err}")
    target_path = os.path.join(engine_dir(base_dir, "cuda"), target_bin_base + ext)
    if not os.path.exists(target_path):
        log_error(f"The CUDA engine is not installed ({target_path} is missing).")
        return None
    if os_name != "Windows":
        try:
            os.chmod(target_path, 0o755)
        except Exception:
            pass
    if test_binary_compatibility(target_path):
        log_system(f"Selected hardware-compatible engine binary: {os.path.basename(target_path)} (CUDA {release.CUDA_VERSION})")
        return target_path
    log_system(f"Capability mismatch for the CUDA {os.path.basename(target_path)}.")
    return None


def download_and_extract_engine(role: str, coordinator_url: Optional[str] = None, flavour: str = "vulkan"):
    """
    Locates the highest CPU-tier bundled llama.cpp engine binary available in the bin directory.
    role: 'coordinator' (needs llama-server) or 'worker' (needs ggml-rpc-server)
    coordinator_url: for a worker that is not on the coordinator's host, where to fetch the
    signed ggml-rpc-server build from (see sync_rpc_bin).
    flavour: 'vulkan' (the default; exactly the engine as before) or 'cuda' (2.7): the signed CUDA 13 build in
    <base>/bin-cuda, fetched, verified and unpacked on first use. Anything but 'cuda' is vulkan. A CUDA request
    that cannot be met returns None and never hands back the Vulkan engine in its place.
    """
    global _last_failure
    _last_failure = ""
    os_name = platform.system()
    ext = ".exe" if os_name == "Windows" else ""
    target_bin_base = "llama-server" if role == "coordinator" else "ggml-rpc-server"
    
    # Check if user explicitly wants pure CPU
    try:
        from engine.config import get_worker_limits
        allowed_gpu = get_worker_limits().get('allowed_gpu_indices', '-1').lower()
    except Exception:
        allowed_gpu = '-1'

    if allowed_gpu == 'none':
        # CPU only. The pinned llama.cpp build carries its CPU backends beside the GPU one
        # and picks the best for this processor itself, so it is the same binary; the
        # worker hides the GPUs from it (GGML_VK_VISIBLE_DEVICES=""). A separate "-cpu"
        # build is only looked for after it, for installs that predate the pinned builds.
        candidates = [target_bin_base + ext, target_bin_base + "-cpu" + ext]
    else:
        # We test in descending order of hardware requirements (AVX2 is minimum)
        candidates = [
            target_bin_base + ext,                 # Tier 1 (Vulkan/CUDA + AVX2)
            target_bin_base + "-cpu" + ext         # Tier 2 (Pure CPU + AVX2)
        ]
    
    from engine.config import get_base_dir
    base_dir = get_base_dir()

    if normalize_flavour(flavour) == "cuda":
        return _download_cuda_engine(role, coordinator_url, base_dir, os_name, ext, target_bin_base, allowed_gpu)

    # A plain worker follows the coordinator's signed release: when it names a new llama.cpp
    # build for this platform, install it before looking. Not on the coordinator's own host
    # (coordinator_url is None there), whose bin/ the release installer manages.
    if role != "coordinator" and coordinator_url:
        from engine import release
        platform_key = release.host_rpc_platform()
        if platform_key is None:
            log_error(f"DLNE ships no ggml-rpc-server for {platform.system()} {platform.machine()}.")
        else:
            try:
                sync_rpc_bin(coordinator_url.rstrip("/"), base_dir, platform_key)
            except Exception as sync_err:  # noqa: BLE001 - an installed engine keeps working offline
                log_error(f"Could not update ggml-rpc-server from the coordinator: {sync_err}")

    # The install's own bin/ first: that is where the release puts the engine. A bin/ in
    # whatever directory the process happened to start in comes after it.
    possible_dirs = [
        os.path.join(base_dir, "bin"),
        os.path.join(os.getcwd(), "bin"),
        os.path.join(os.path.dirname(sys.executable), "bin") if hasattr(sys, 'frozen') else ""
    ]

    for bin_dir in possible_dirs:
        if not bin_dir:
            continue
            
        for candidate in candidates:
            target_path = os.path.join(bin_dir, candidate)

            if os.path.exists(target_path):
                if os_name != "Windows":
                    try:
                        os.chmod(target_path, 0o755)
                    except Exception:
                        pass
                
                if test_binary_compatibility(target_path):
                    log_system(f"Selected hardware-compatible engine binary: {candidate}")
                    return target_path
                else:
                    log_system(f"Capability mismatch for {candidate}. Checking fallback tier...")

    if _last_failure == "vcredist":
        log_error(f"FATAL: {target_bin_base} cannot start. {VCREDIST_TEXT}")
    elif _last_failure == "dll":
        log_error(f"FATAL: {target_bin_base} cannot start because a DLL it needs is missing (see above). "
                  f"Installing the Visual C++ Runtime from {VCREDIST_URL} fixes the usual case.")
    else:
        log_error(f"FATAL: Could not locate a compatible bundled binary for '{target_bin_base}'. "
                  "Check the lines above for the reason (an engine that crashes with an illegal instruction needs a CPU with AVX2).")
    return None


# --------------------------------------------------------------------------------------
# The coordinator's own engine flavour (ENGINE_FLAVOR, 2.7)
# --------------------------------------------------------------------------------------

# What this process settled on for its own engine: "" until the first resolution, then "vulkan" or "cuda". The status
# endpoint reads it (engine_flavour), and so does engine_build_is_vulkan, so both describe the engine that really runs.
_coordinator_flavour = ""


def cuda_allowed_here():
    """
    (allowed, reason) for this host running the CUDA engine: an NVIDIA GPU of compute capability 7.5 or newer and a
    driver of the 580 series or later, which engine.hardware.cuda_capability() reads from nvidia-smi (the same
    answer a worker gets). ``allowed`` is None when that detection is not available here at all, which callers treat
    as "cannot tell" (auto then stays on Vulkan; an operator's explicit ``cuda`` is trusted).
    """
    from engine import release

    if release.host_cuda_platform() is None:
        return False, f"DLNE ships no CUDA engine for {platform.system()} {platform.machine()}"
    try:
        from engine import hardware
        detect = getattr(hardware, "cuda_capability", None)
        if detect is None:
            return None, "this build cannot check for CUDA"
        info = detect()
    except Exception as exc:  # noqa: BLE001 - a failed check is "no", never a crash of the engine start
        return False, f"the CUDA check failed ({type(exc).__name__})"
    if not isinstance(info, dict):
        return False, "the CUDA check gave no answer"
    return bool(info.get("capable")), str(info.get("reason") or "")


def resolve_engine_flavour(setting: Any) -> str:
    """
    ENGINE_FLAVOR as the engine it means: "vulkan" or "cuda". ``vulkan`` is vulkan; ``auto`` is cuda only where this
    host may run it; ``cuda`` is cuda unless the host plainly cannot (then it says so and runs Vulkan, rather than
    download 600 MB for an engine with no card to run on). Anything else is vulkan.
    """
    setting = str(setting or "vulkan").strip().lower()
    if setting not in ("auto", "cuda"):
        return "vulkan"
    allowed, reason = cuda_allowed_here()
    if allowed:
        return "cuda"
    if allowed is None and setting == "cuda":
        return "cuda"
    if setting == "cuda":
        log_warning(f"ENGINE_FLAVOR=cuda, but this host cannot run the CUDA engine ({reason}); using Vulkan.")
    return "vulkan"


def coordinator_engine_flavour() -> str:
    """
    The flavour of the coordinator's own engine: what was last chosen for it, or (before any engine start) what
    ENGINE_FLAVOR resolves to. Decided once per process, so a status poll never runs nvidia-smi.
    """
    global _coordinator_flavour
    if not _coordinator_flavour:
        try:
            from engine.config import get_worker_limits
            setting = get_worker_limits().get("engine_flavor", "vulkan")
        except Exception:  # noqa: BLE001 - an unreadable config is the default
            setting = "vulkan"
        _coordinator_flavour = resolve_engine_flavour(setting)
    return _coordinator_flavour


def set_coordinator_engine_flavour(flavour: str) -> None:
    """Record the flavour the coordinator's engine actually started with (after a fall back, Vulkan)."""
    global _coordinator_flavour
    _coordinator_flavour = normalize_flavour(flavour)


# ======================================================================================
# Verified, resumable downloads of model files (one file, or a model in several parts)
# ======================================================================================
#
# download_verified() is what coding members and automatic model switching use to fetch a
# model. A model above about 50 GB ships as several GGUF parts, and every part must be here,
# whole and exactly what the signed ladder or release pinned, before an engine is pointed at
# the first one.
#
#   files    [{"file", "url", "size", "sha256"}, ...] every part, in order. The manifest is
#            untrusted: a file name is a bare name from a short safe alphabet (no folders, no
#            "..", nothing that collides with our own .part/.lock/sidecar files), a URL is
#            https:// with a host and no password, a size is a positive integer, a hash is
#            64 hex digits. Anything else refuses the whole call before any network or disk use.
#   dest_dir where the parts go, all in the one folder (llama.cpp finds part 2 by name, next
#            to part 1).
#
# THE LIFE OF A PART
#   1. Already here? Size matches and the SHA-256 matches (or the "<file>.dlne-sha256" marker
#      says it did, keyed by size and mtime: the same marker engine.app_pool.sha256_ok reads
#      and writes, so a model is hashed once). Then it is skipped.
#   2. Otherwise it is fetched into "<file>.part" with an HTTP Range request from wherever a
#      previous attempt stopped. The SHA-256 is computed while the bytes stream in (the bytes
#      already in a .part are hashed first), so there is no second pass over a 50 GB file.
#   3. Only a part of exactly the pinned size with exactly the pinned hash is renamed
#      ("os.replace", atomic) to its real name. The real name therefore never holds a partial
#      or wrong file: an engine, or another caller, sees nothing or the whole verified part.
#   4. A failure that a later call can pick up (network down, cancelled, disk full) leaves the
#      .part where it is, as the resume point. Anything known to be wrong (bad hash, wrong
#      size, a server that sends more than was signed) is deleted. keep_partial=False deletes
#      the .part on every failure instead.
#
# CONCURRENCY
#   One lock file per part ("<file>.lock", an OS file lock released by the kernel if the
#   holder dies, plus a thread lock for callers in the same process). A second caller asking
#   for the same part waits, then finds it verified and skips it, so two callers never write
#   the same .part. Waiting is cancellable and reported as the "waiting" phase.
#
# PROGRESS
#   progress(update: dict) is called from the downloading thread, at most about twice a
#   second plus at every change of phase; an exception it raises is ignored.
#       phase       "checking" (hashing a file already on disk), "waiting" (another caller
#                   holds the file), "downloading", "retrying" (adds "error", "retry_in"),
#                   "done", "failed" (adds "error")
#       file        name of the part being worked on ("" when none)
#       index/count 1-based position of that part, and how many parts there are
#       file_done / file_size    bytes of this part that are on disk, and its pinned size
#       done / total             the same for all parts together
#       speed       smoothed bytes per second over the network (0.0 until measured)
#
# Blocking: run it on a thread (asyncio.to_thread). Stop it with a threading.Event passed as
# ``cancel``; it notices within about one block (1 MiB) and leaves the .part for next time.

PART_SUFFIX = ".part"             # a download in progress, and its resume point
LOCK_SUFFIX = ".lock"             # held while one caller downloads or replaces a file
SIDECAR_SUFFIX = ".dlne-sha256"   # engine.app_pool.sha256_ok's marker: "<sha256> <size> <mtime>"

# Names a manifest may not use: they are ours (or older staging names), and the manifest must
# not be able to point one download at another's bookkeeping.
_RESERVED_SUFFIXES = (PART_SUFFIX, LOCK_SUFFIX, SIDECAR_SUFFIX, ".dlne-download", ".tmp")
_SAFE_FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+=,@~()-]{0,199}")
_WINDOWS_DEVICE_NAMES = frozenset(
    ["con", "prn", "aux", "nul"] + [f"com{i}" for i in range(1, 10)] + [f"lpt{i}" for i in range(1, 10)]
)
_SHA256_HEX = re.compile(r"[0-9a-fA-F]{64}")

_USER_AGENT = "DLNE-downloader/1"
_BLOCK = 1 << 20                  # network read size
_HASH_BLOCK = 8 << 20             # disk read size when hashing
_SYNC_EVERY = 64 << 20            # fsync the .part this often, so a crash costs little
_MIN_FREE_MARGIN = 1 << 30        # free space to leave beyond what is needed (default)
_MAX_RETRY_AFTER = 300.0          # longest "Retry-After" a server can make us wait, in seconds
_LOCK_POLL = 0.25                 # how often a waiting caller looks at the lock and at ``cancel``
_PROGRESS_EVERY = 0.5             # seconds between progress callbacks while bytes flow

try:                              # POSIX
    import fcntl
except ImportError:               # Windows
    fcntl = None
try:                              # Windows
    import msvcrt
except ImportError:               # POSIX
    msvcrt = None


class DownloadError(Exception):
    """A download that failed, with what a caller needs to decide whether to try again."""

    def __init__(self, message: str, *, lasting: bool = True, discard: bool = False, file: str = ""):
        super().__init__(message)
        self.lasting = lasting      # trying again unchanged cannot help (bad manifest, disk full, bad hash)
        self.discard = discard      # the partial file is known to be wrong: delete it
        self.file = file


class _Cancelled(Exception):
    """``cancel`` was set."""


class _Transient(Exception):
    """A failure worth retrying with backoff (``retry_after``: seconds the server asked us to wait)."""

    def __init__(self, reason: str, retry_after: Optional[float] = None):
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after


class _BadData(Exception):
    """The bytes in the .part cannot be trusted: start the part again from zero (once)."""


@dataclass
class DownloadResult:
    """What :func:`fetch_verified` reports. ``ok`` is all that :func:`download_verified` returns."""
    ok: bool
    reason: str = ""            # one line for logs and status; "" on success
    lasting: bool = False       # True when trying again unchanged cannot help
    cancelled: bool = False
    failed_file: str = ""
    bytes_fetched: int = 0      # bytes received over the network in this call (skipped and resumed bytes excluded)
    paths: List[str] = field(default_factory=list)   # the verified files, in order, on success

    def __bool__(self) -> bool:
        return self.ok


def safe_file_name(name: Any) -> bool:
    """A bare file name from a short safe alphabet, and not one of our own bookkeeping names."""
    if not isinstance(name, str) or _SAFE_FILE_NAME.fullmatch(name) is None:
        return False
    lowered = name.lower()
    if ".." in name or lowered.endswith(".") or lowered.endswith(_RESERVED_SUFFIXES):
        return False
    return lowered.split(".", 1)[0] not in _WINDOWS_DEVICE_NAMES


def _url_problem(url: Any) -> str:
    """Why ``url`` may not be fetched, or "" when it may: https only, a host, no password."""
    if (not isinstance(url, str) or not url or len(url) > 16384 or not url.isascii()
            or any(ch <= " " or ch == "\x7f" for ch in url)):
        return "is not a valid URL"
    try:
        parts = urllib.parse.urlsplit(url)
        parts.port  # noqa: B018 - raises ValueError for a bad port
    except ValueError:
        return "is not a valid URL"
    if parts.scheme.lower() != "https":
        return "is not an https:// URL"
    if not parts.hostname:
        return "has no host"
    if parts.username is not None or parts.password is not None:
        return "carries a user name or password"
    return ""


def _shown(value: Any) -> str:
    """An untrusted value for an error message: short, quoted, no markup surprises."""
    text = repr(value)
    return text if len(text) <= 70 else text[:67] + "..."


def validate_files(files: Any) -> List[Dict[str, Any]]:
    """
    The manifest's parts as clean ``{"file", "url", "size", "sha256"}`` dicts (hash lowercased).
    Raises :class:`DownloadError` for anything unsafe or incomplete, before anything is fetched.
    """
    if not isinstance(files, (list, tuple)) or not files:
        raise DownloadError("there are no files to download")
    entries: List[Dict[str, Any]] = []
    seen = set()
    for number, entry in enumerate(files, 1):
        if not isinstance(entry, dict):
            raise DownloadError(f"file {number} of the manifest is not an object")
        name, url, size, sha = entry.get("file"), entry.get("url"), entry.get("size"), entry.get("sha256")
        if not safe_file_name(name):
            raise DownloadError(f"{_shown(name)} is not a safe file name; refusing the whole download")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise DownloadError(f"{name} has no valid size", file=name)
        if not isinstance(sha, str) or _SHA256_HEX.fullmatch(sha) is None:
            raise DownloadError(f"{name} has no valid SHA-256", file=name)
        problem = _url_problem(url)
        if problem:
            raise DownloadError(f"the download address of {name} {problem}", file=name)
        if name.lower() in seen:
            raise DownloadError(f"{name} is listed twice", file=name)
        seen.add(name.lower())
        entries.append({"file": name, "url": url, "size": size, "sha256": sha.lower()})
    return entries


# --------------------------------------------------------------------------------------
# Files on disk: the verification marker
# --------------------------------------------------------------------------------------

def _is_windows() -> bool:
    return os.name == "nt"


# Windows refuses to rename over, delete or open a file that another program holds open without
# the "delete" share flag, and right after a big file is written several programs do for a moment:
# Defender scans every file when it is closed (minutes, for an 18 GB model), then the search indexer
# and any backup agent look at it. POSIX lets a rename go through under all of them. The errors are
# "access denied" (5), "sharing violation" (32) and "lock violation" (33); all of them surface in
# Python as PermissionError.
_BUSY_PATIENCE = 300.0            # how long a rename of a big downloaded file waits for a scanner, in seconds
_BUSY_FIRST_PAUSE = 0.05
_BUSY_LAST_PAUSE = 2.0


def _busy(exc: OSError) -> bool:
    """``exc`` is Windows saying the file is in use at the moment (and so worth another try)."""
    return _is_windows() and (isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in (5, 32, 33))


def _retry_while_busy(operation: Callable[[], Any], cancel: Optional[threading.Event] = None, *,
                      patience: float = _BUSY_PATIENCE, sleep: Optional[Callable[[float], Any]] = None,
                      clock: Optional[Callable[[], float]] = None) -> Any:
    """
    ``operation()``, tried again with growing pauses for up to ``patience`` seconds while Windows
    says a file it touches is busy. The last error is raised when the time is up; every other error,
    and every error on POSIX, is raised at once. ``cancel`` ends the wait early with :class:`_Cancelled`.
    """
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    deadline = clock() + patience
    pause = _BUSY_FIRST_PAUSE
    while True:
        try:
            return operation()
        except OSError as exc:
            if not _busy(exc) or clock() >= deadline:
                raise
        if cancel is not None and cancel.is_set():
            raise _Cancelled()
        sleep(pause)
        pause = min(pause * 2, _BUSY_LAST_PAUSE)


def _replace_file(source: str, target: str, cancel: Optional[threading.Event] = None, *,
                  patience: float = _BUSY_PATIENCE, **timing: Any) -> None:
    """``os.replace`` (atomic), patient with a file Windows reports as busy (see ``_retry_while_busy``)."""
    _retry_while_busy(lambda: os.replace(source, target), cancel, patience=patience, **timing)


def _write_marker(path: str, sha256: str, size: int, mtime: int) -> None:
    """Remember a good check beside ``path`` (atomically; best effort: a missing marker only costs a re-hash)."""
    marker = path + SIDECAR_SUFFIX
    scratch = f"{marker}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(scratch, "w", encoding="utf-8") as handle:
            handle.write(f"{sha256} {size} {mtime}")
        _replace_file(scratch, marker, patience=3.0)
    except OSError:
        try:
            os.remove(scratch)
        except OSError:
            pass


def _file_verified(path: str, sha256: str, size: int, cancel: Optional[threading.Event] = None,
                   on_bytes: Optional[Callable[[int], None]] = None) -> bool:
    """
    ``path`` is a regular file of exactly ``size`` bytes with exactly ``sha256``. The marker
    beside it remembers a good check (keyed by size and mtime, the format of
    ``engine.app_pool.sha256_ok``), so an 18 GB model is hashed once.
    """
    try:
        info = os.stat(path)
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode) or info.st_size != size:
        return False
    stamp = f"{sha256} {info.st_size} {int(info.st_mtime)}"
    try:
        with open(path + SIDECAR_SUFFIX, "r", encoding="utf-8") as handle:
            if handle.read().strip() == stamp:
                return True
    except OSError:
        pass
    digest = hashlib.sha256()
    done = 0
    try:
        with open(path, "rb") as handle:
            while True:
                if cancel is not None and cancel.is_set():
                    raise _Cancelled()
                block = handle.read(_HASH_BLOCK)
                if not block:
                    break
                digest.update(block)
                done += len(block)
                if on_bytes is not None:
                    on_bytes(done)
    except OSError:
        return False
    if done != size or digest.hexdigest() != sha256:
        return False
    _write_marker(path, sha256, info.st_size, int(info.st_mtime))
    return True


def _disk_free(path: str) -> Optional[int]:
    """Free bytes where ``path`` lives, or None when the system will not say."""
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def _write_block(handle, block: bytes) -> None:
    handle.write(block)


def _sync_directory(path: str) -> None:
    """Make a rename durable (POSIX; Windows has no directory handles to sync)."""
    if _is_windows():
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


# --------------------------------------------------------------------------------------
# One caller per file: the lock
# --------------------------------------------------------------------------------------

_THREAD_LOCKS: Dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _try_lock(fd: int) -> Optional[bool]:
    """True: locked. False: someone else holds it. None: this filesystem cannot lock files."""
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        return True
    except OSError as exc:
        if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES, errno.EDEADLK):
            return False
        return None


def _unlock(fd: int) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        else:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


def _same_file(fd: int, path: str) -> bool:
    """The open ``fd`` is still what ``path`` names (a previous holder deletes the lock file on release)."""
    try:
        held, named = os.fstat(fd), os.stat(path)
    except OSError:
        return False
    return (held.st_dev, held.st_ino) == (named.st_dev, named.st_ino)


class _PartLock:
    """
    Exclusive hold on one file's lock path, across threads and across processes.

    The OS file lock goes when its holder dies, so a crashed download never leaves a stale lock
    that something has to clean up. The lock file itself is deleted on release; a waiter that
    wakes up holding a deleted file notices (``_same_file``) and locks the new one. Waiting polls,
    so it can be cancelled and can give up after ``timeout`` seconds.
    """

    def __init__(self, path: str, cancel: threading.Event, timeout: float,
                 on_wait: Optional[Callable[[], None]] = None) -> None:
        self.path = path
        self.cancel = cancel
        self.timeout = timeout
        self.on_wait = on_wait
        self._fd: Optional[int] = None
        self._thread_lock: Optional[threading.Lock] = None

    def __enter__(self) -> "_PartLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()

    def _check(self, deadline: float) -> None:
        if self.cancel.is_set():
            raise _Cancelled()
        if time.monotonic() >= deadline:
            raise DownloadError("another download of this file has had it locked for too long; "
                                "not waiting any longer", lasting=False)

    def _waiting(self) -> None:
        if self.on_wait is not None:
            self.on_wait()

    def acquire(self) -> None:
        deadline = time.monotonic() + self.timeout
        key = os.path.normcase(os.path.realpath(self.path))
        with _THREAD_LOCKS_GUARD:
            thread_lock = _THREAD_LOCKS.setdefault(key, threading.Lock())
        while not thread_lock.acquire(timeout=_LOCK_POLL):
            self._check(deadline)
            self._waiting()
        self._thread_lock = thread_lock
        try:
            self._acquire_file(deadline)
        except BaseException:
            self._thread_lock = None
            thread_lock.release()
            raise

    def _acquire_file(self, deadline: float) -> None:
        while True:
            # On Windows the previous holder's delete can still be pending (a scanner has the file
            # open), and creating it again is refused for a moment: wait that out, briefly.
            fd = _retry_while_busy(lambda: os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644), self.cancel,
                                   patience=10.0)
            try:
                while True:
                    locked = _try_lock(fd)
                    if locked is None:
                        # No file locking here (an odd network drive): the thread lock still
                        # keeps this process's callers apart.
                        self._fd = fd
                        return
                    if locked:
                        break
                    self._check(deadline)
                    self._waiting()
                    self.cancel.wait(_LOCK_POLL)
                if _same_file(fd, self.path):
                    self._fd = fd
                    return
            except BaseException:
                os.close(fd)
                raise
            os.close(fd)   # the holder before us deleted this file; lock the one that replaced it

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            if not _is_windows():
                try:
                    os.remove(self.path)   # while still locked: a waiter then sees a different file
                except OSError:
                    pass
            _unlock(fd)
            os.close(fd)
            if _is_windows():
                try:
                    os.remove(self.path)   # fails while a waiter has it open, which is fine
                except OSError:
                    pass
        thread_lock, self._thread_lock = self._thread_lock, None
        if thread_lock is not None:
            thread_lock.release()


# --------------------------------------------------------------------------------------
# The network side
# --------------------------------------------------------------------------------------

class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Hugging Face answers a model URL with a redirect to its CDN; that is fine, but never to plain http."""

    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        problem = _url_problem(urllib.parse.urljoin(req.full_url, newurl))
        if problem:
            raise DownloadError(f"the server redirected the download to an address that {problem}; refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _build_opener(context: Optional[ssl.SSLContext]) -> urllib.request.OpenerDirector:
    """An opener that speaks https only (no http, ftp or file handlers) and follows https redirects."""
    opener = urllib.request.OpenerDirector()
    for handler in (urllib.request.ProxyHandler(), urllib.request.UnknownHandler(),
                    urllib.request.HTTPDefaultErrorHandler(),
                    urllib.request.HTTPSHandler(context=context or ssl.create_default_context()),
                    _HttpsOnlyRedirects(), urllib.request.HTTPErrorProcessor()):
        opener.add_handler(handler)
    return opener


_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+|\*)")


def _parse_content_range(value: Optional[str]):
    """``(first, last, total or None)`` of a Content-Range header, or None."""
    match = _CONTENT_RANGE.fullmatch((value or "").strip())
    if match is None:
        return None
    total = None if match.group(3) == "*" else int(match.group(3))
    return int(match.group(1)), int(match.group(2)), total


def _retry_after(headers: Any) -> Optional[float]:
    try:
        value = float(headers.get("Retry-After"))
    except (AttributeError, TypeError, ValueError):
        return None
    return max(0.0, min(value, _MAX_RETRY_AFTER)) if value == value else None


def _describe(exc: BaseException) -> str:
    text = str(exc).strip()
    return type(exc).__name__ if not text else f"{type(exc).__name__}: {text[:100]}"


def _gb(value: float) -> str:
    return f"{value / 1e9:.1f}"


class _PartState:
    """What is known about the bytes in one ``.part``: kept across retries so they are hashed once."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.hasher = hashlib.sha256()
        self.nbytes = 0       # bytes hashed, which is also the length of the .part
        self.pieces = 0       # responses that brought some of those bytes
        self.untrusted = False   # some of them were found on disk, written by something else


class _Job:
    """One call of :func:`fetch_verified`."""

    def __init__(self, entries, dest_dir, progress, cancel, *, free_margin, timeout, retries,
                 backoff, max_backoff, lock_timeout, keep_partial, ssl_context) -> None:
        self.entries = entries
        self.dest = os.path.abspath(dest_dir)
        self.progress = progress
        self.cancel = cancel
        self.free_margin = free_margin
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.max_backoff = max_backoff
        self.lock_timeout = lock_timeout
        self.keep_partial = keep_partial
        self.opener = _build_opener(ssl_context)
        self.total = sum(entry["size"] for entry in entries)
        self.file_done = {entry["file"]: 0 for entry in entries}
        self.fetched = 0
        self.finals: Dict[str, str] = {}
        self._rejected: Dict[str, Any] = {}    # file -> (size, mtime_ns) of a file that failed its hash
        self._last_emit = float("-inf")
        self._speed = 0.0
        self._mark = (time.monotonic(), 0)
        self._progress_failed = False

    # ---- progress
    def _emit(self, phase: str, entry: Optional[Dict[str, Any]] = None, *, force: bool = False, **extra) -> None:
        if self.progress is None:
            return
        now = time.monotonic()
        if not force and now - self._last_emit < _PROGRESS_EVERY:
            return
        elapsed = now - self._mark[0]
        if elapsed >= _PROGRESS_EVERY:
            instant = (self.fetched - self._mark[1]) / elapsed
            self._speed = instant if self._speed == 0.0 else 0.7 * self._speed + 0.3 * instant
            self._mark = (now, self.fetched)
        self._last_emit = now
        name = entry["file"] if entry else ""
        update = {
            "phase": phase,
            "file": name,
            "index": self.entries.index(entry) + 1 if entry else 0,
            "count": len(self.entries),
            "file_done": self.file_done.get(name, 0),
            "file_size": entry["size"] if entry else 0,
            "done": sum(self.file_done.values()),
            "total": self.total,
            "speed": self._speed,
        }
        update.update(extra)
        try:
            self.progress(update)
        except Exception as exc:  # noqa: BLE001 - a broken progress callback must not stop a download
            if not self._progress_failed:
                self._progress_failed = True
                log_warning(f"The download's progress callback failed ({_describe(exc)}); ignoring it.")

    def _check_cancelled(self) -> None:
        if self.cancel.is_set():
            raise _Cancelled()

    # ---- the whole call
    def run(self) -> DownloadResult:
        try:
            os.makedirs(self.dest, exist_ok=True)
        except OSError as exc:
            raise self._disk_error(exc)
        real_dest = os.path.normcase(os.path.realpath(self.dest))
        for entry in self.entries:
            path = os.path.join(self.dest, entry["file"])
            # The name is already a bare safe name; this catches a symlink planted at its place.
            if os.path.normcase(os.path.dirname(os.path.realpath(path))) != real_dest:
                raise DownloadError(f"{entry['file']} would be written outside {self.dest}; refused", file=entry["file"])
            self.finals[entry["file"]] = path

        pending = []
        for entry in self.entries:
            if self._verified(entry):
                continue
            pending.append(entry)
        if not pending:
            log_system(f"All {len(self.entries)} file(s) are already downloaded and verified.")
            self._emit("done", force=True)
            return self._success()

        self._check_space(pending)
        log_system(f"Downloading {len(pending)} of {len(self.entries)} file(s), {_gb(self.total)} GB in all, "
                   f"into {self.dest}...")
        for entry in pending:
            self._fetch_one(entry)
        self._emit("done", force=True)
        log_success(f"Downloaded and verified {len(self.entries)} file(s) ({_gb(self.total)} GB).")
        return self._success()

    def _success(self) -> DownloadResult:
        return DownloadResult(True, bytes_fetched=self.fetched, paths=[self.finals[e["file"]] for e in self.entries])

    def _verified(self, entry) -> bool:
        name, size = entry["file"], entry["size"]

        def checked(n: int) -> None:
            self.file_done[name] = n
            self._emit("checking", entry)

        path = self.finals[name]
        try:
            info = os.stat(path)
            stamp = (info.st_size, info.st_mtime_ns)
        except OSError:
            stamp = None
        if stamp is not None and self._rejected.get(name) == stamp:
            return False     # hashed a moment ago (before taking the lock) and found wrong
        self._emit("checking", entry, force=True)
        ok = _file_verified(path, entry["sha256"], size, self.cancel, checked)
        self.file_done[name] = size if ok else 0
        if not ok and stamp is not None and stamp[0] == size:
            self._rejected[name] = stamp
        return ok

    def _check_space(self, pending) -> None:
        need = 0
        for entry in pending:
            try:
                have = os.path.getsize(self.finals[entry["file"]] + PART_SUFFIX)
            except OSError:
                have = 0
            need += entry["size"] - (have if have <= entry["size"] else 0)
        if need <= 0:
            return
        margin = self.free_margin if self.free_margin is not None else max(_MIN_FREE_MARGIN, need // 50)
        free = _disk_free(self.dest)
        if free is not None and free < need + margin:
            raise DownloadError(f"not enough disk space in {self.dest}: {_gb(need)} GB still to download plus "
                                f"{_gb(margin)} GB to spare, and {_gb(free)} GB is free", lasting=True)

    def _disk_error(self, exc: OSError) -> DownloadError:
        full = getattr(errno, "EDQUOT", None)
        if exc.errno in (errno.ENOSPC, full) or getattr(exc, "winerror", None) in (39, 112):
            return DownloadError(f"the disk is full ({self.dest})", lasting=True)
        return DownloadError(f"could not write in {self.dest}: {exc.strerror or type(exc).__name__}", lasting=True)

    def _discard(self, part: str) -> None:
        try:
            os.remove(part)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log_warning(f"Could not delete {os.path.basename(part)}: {_describe(exc)}")

    # ---- one part
    def _fetch_one(self, entry) -> None:
        name, final = entry["file"], self.finals[entry["file"]]

        def waiting() -> None:
            self._emit("waiting", entry, reason="another download of this file is running")

        try:
            with _PartLock(final + LOCK_SUFFIX, self.cancel, self.lock_timeout, on_wait=waiting):
                # Another caller may have finished it while this one waited its turn.
                if self._verified(entry):
                    return
                self._download_into(entry, final)
        except DownloadError as exc:
            exc.file = exc.file or name
            raise
        except OSError as exc:
            raise self._disk_error(exc)

    def _backoff_delay(self, failures: int, retry_after: Optional[float]) -> float:
        delay = min(self.max_backoff, self.backoff * (2 ** (failures - 1)))
        delay *= 0.75 + 0.5 * random.random()
        return max(delay, retry_after) if retry_after else delay

    def _download_into(self, entry, final: str) -> None:
        name = entry["file"]
        part = final + PART_SUFFIX
        state = _PartState()
        failures = 0
        best = 0                  # the most bytes this part has held so far: only getting past it is progress
        restarts_left = 1
        finished = False
        try:
            while True:
                self._check_cancelled()
                try:
                    self._attempt(entry, final, part, state)
                    finished = True
                    return
                except _Transient as exc:
                    # Net progress, not bytes received: a server that ignores Range and sends the
                    # first kilobyte of the file again each time must still run out of retries.
                    progressed = state.nbytes > best
                    best = max(best, state.nbytes)
                    failures = 0 if progressed else failures + 1
                    if failures > self.retries:
                        raise DownloadError(f"{exc.reason}; gave up after {self.retries} "
                                            f"retr{'y' if self.retries == 1 else 'ies'}", lasting=False)
                    delay = self._backoff_delay(max(failures, 1), exc.retry_after)
                    log_warning(f"Downloading {name}: {exc.reason}; trying again in {delay:.0f} s.")
                    self._emit("retrying", entry, force=True, error=exc.reason, retry_in=delay)
                    if self.cancel.wait(delay):
                        raise _Cancelled()
                except _BadData as exc:
                    self._discard(part)
                    state.reset()
                    best = 0
                    self.file_done[name] = 0
                    if restarts_left <= 0:
                        raise DownloadError(f"{name}: {exc}; starting it over did not help", lasting=True)
                    restarts_left -= 1
                    log_warning(f"Downloading {name}: {exc}; starting this file again from the beginning.")
                except DownloadError as exc:
                    if exc.discard:
                        self._discard(part)
                    raise
        finally:
            if not finished and not self.keep_partial:
                self._discard(part)

    def _attempt(self, entry, final: str, part: str, state: _PartState) -> None:
        """One request for the rest of a part, streamed to its .part; finishes the part when complete."""
        name, size = entry["file"], entry["size"]
        try:
            on_disk = os.path.getsize(part)
        except OSError:
            on_disk = 0
        if on_disk > size:
            self._discard(part)
            on_disk = 0
        if on_disk != state.nbytes:
            # Bytes from an earlier run (or none): hash what is there before adding to it.
            state.reset()
            if on_disk:
                self._hash_prefix(entry, part, on_disk, state)
        offset = state.nbytes
        self.file_done[name] = offset
        if offset == size:
            self._finish(entry, final, part, state)
            return

        response = self._open(entry, offset)
        try:
            status = response.status
            if status == 200 and offset:
                # The server ignored the Range header and is sending the whole file: start over.
                self._discard(part)
                state.reset()
                offset = 0
                self.file_done[name] = 0
            elif status == 206:
                span = _parse_content_range(response.headers.get("Content-Range"))
                if span is None or span[0] != offset or span[1] != size - 1 or span[2] not in (None, size):
                    raise _BadData("the server's answer to the resume request does not match the file")
            elif status != 200:
                raise DownloadError(f"the server answered HTTP {status}", lasting=True)
            expected = size - offset
            try:
                length = int(response.headers.get("Content-Length"))
            except (TypeError, ValueError):
                length = None
            if length is not None and length != expected:
                raise DownloadError(f"the server's copy of {name} is {length + offset} bytes long, but "
                                    f"{size} were signed", lasting=True, discard=True)
            self._stream(response, entry, part, state, append=offset > 0)
        finally:
            response.close()
        self._finish(entry, final, part, state)

    def _hash_prefix(self, entry, part: str, count: int, state: _PartState) -> None:
        name = entry["file"]
        try:
            with open(part, "rb") as handle:
                remaining = count
                while remaining > 0:
                    self._check_cancelled()
                    block = handle.read(min(_HASH_BLOCK, remaining))
                    if not block:
                        break
                    state.hasher.update(block)
                    state.nbytes += len(block)
                    remaining -= len(block)
                    self.file_done[name] = state.nbytes
                    self._emit("checking", entry)
        except OSError as exc:
            raise self._disk_error(exc)
        if state.nbytes != count:    # shrank while being read: nothing here can be trusted
            self._discard(part)
            state.reset()
        else:
            state.untrusted = True

    def _open(self, entry, offset: int):
        headers = {"User-Agent": _USER_AGENT, "Accept-Encoding": "identity"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        request = urllib.request.Request(entry["url"], headers=headers)
        try:
            return self.opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            code, wait = exc.code, _retry_after(exc.headers)
            exc.close()
            if code == 416 and offset:
                raise _BadData("the server refused to resume at the saved point")
            if code in (408, 425, 429) or code >= 500:
                raise _Transient(f"the server answered HTTP {code}", wait)
            raise DownloadError(f"the server answered HTTP {code}", lasting=True)
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, ssl.SSLCertVerificationError):
                raise DownloadError("the server's certificate could not be verified", lasting=True)
            raise _Transient(f"could not reach the server ({_describe(exc.reason)})")
        except ssl.SSLCertVerificationError:
            raise DownloadError("the server's certificate could not be verified", lasting=True)
        except (OSError, http.client.HTTPException) as exc:
            raise _Transient(f"could not reach the server ({_describe(exc)})")

    def _stream(self, response, entry, part: str, state: _PartState, *, append: bool) -> None:
        name, size = entry["file"], entry["size"]
        encoding = (response.headers.get("Content-Encoding") or "identity").strip().lower()
        if encoding != "identity":
            raise DownloadError("the server compressed the download, which cannot be resumed or verified", lasting=True)
        received = 0
        unsynced = 0
        # read1 returns as soon as some bytes are there; read(n) would wait for n of them,
        # which is a megabyte of silence on a slow link before a cancel or a progress update.
        read = getattr(response, "read1", response.read)
        try:
            with open(part, "ab" if append else "wb") as out:
                while True:
                    self._check_cancelled()
                    try:
                        block = read(_BLOCK)
                    except (OSError, http.client.HTTPException) as exc:
                        raise _Transient(f"the connection broke ({_describe(exc)})")
                    if not block:
                        break
                    if state.nbytes + len(block) > size:
                        raise DownloadError(f"the server sent more than the {size} bytes signed for {name}",
                                            lasting=True, discard=True)
                    _write_block(out, block)
                    state.hasher.update(block)
                    state.nbytes += len(block)
                    received += len(block)
                    unsynced += len(block)
                    self.fetched += len(block)
                    self.file_done[name] = state.nbytes
                    if unsynced >= _SYNC_EVERY:
                        out.flush()
                        os.fsync(out.fileno())
                        unsynced = 0
                    self._emit("downloading", entry)
                out.flush()
                os.fsync(out.fileno())
        except OSError as exc:
            raise self._disk_error(exc)
        finally:
            if received:
                state.pieces += 1    # one more separate source of the bytes in the .part
        if state.nbytes < size:
            raise _Transient(f"the connection ended after {state.nbytes} of {size} bytes")

    def _finish(self, entry, final: str, part: str, state: _PartState) -> None:
        """The .part holds ``size`` bytes: it is the file only if its SHA-256 is the pinned one."""
        name, size, sha = entry["file"], entry["size"], entry["sha256"]
        if state.hasher.hexdigest() != sha:
            self._discard(part)
            if state.pieces > 1 or state.untrusted:
                # Part of it came from an earlier run or an earlier connection: one clean
                # attempt from zero tells whether the file or the leftovers were wrong.
                raise _BadData("the bytes saved earlier and the bytes just received do not make the signed file")
            raise DownloadError(f"{name} does not match its signed SHA-256 (it came to {size} bytes); deleted",
                                lasting=True)
        try:
            info = os.stat(part)
            if info.st_size != size:
                raise _BadData("the saved file is not the size that was written")
            # The marker goes first, keyed by the .part's size and mtime, which the rename keeps:
            # whoever sees the file under its real name can already trust it.
            _write_marker(final, sha, size, int(info.st_mtime))
            _replace_file(part, final, self.cancel)
            _sync_directory(self.dest)
        except OSError as exc:
            raise self._disk_error(exc)
        self.file_done[name] = size
        log_system(f"{name}: {_gb(size)} GB downloaded and verified.")
        self._emit("downloading", entry, force=True)


def fetch_verified(files: Any, dest_dir: str, progress: Optional[Callable[[Dict[str, Any]], None]] = None, *,
                   cancel: Optional[threading.Event] = None, free_margin: Optional[int] = None,
                   timeout: float = 60.0, retries: int = 5, backoff: float = 2.0, max_backoff: float = 60.0,
                   lock_timeout: float = 6 * 3600.0, keep_partial: bool = True,
                   ssl_context: Optional[ssl.SSLContext] = None) -> DownloadResult:
    """
    Download every file of ``files`` into ``dest_dir``, verified; see the section comment above for
    the whole story. Never raises for a failed download: the result says why it failed.

    ``cancel``       a threading.Event; set it to stop (the .part is kept).
    ``free_margin``  bytes of free space to leave beyond what is needed (default: 1 GiB or 2%).
    ``timeout``      seconds the server may stay silent (connecting, or between blocks).
    ``retries``      how many times to retry after a failure that sent no new data. A failed
                     attempt that did bring data does not count, so a flaky link that keeps
                     making progress keeps going. Waits ``backoff`` seconds, doubling, up to
                     ``max_backoff`` (a server's Retry-After wins, up to 5 minutes).
    ``lock_timeout`` how long to wait for another caller that is fetching the same file.
    ``keep_partial`` False deletes the .part on any failure instead of keeping it to resume.
    ``ssl_context``  certificates to trust (default: the system's).
    """
    cancel = cancel if cancel is not None else threading.Event()
    job: Optional[_Job] = None
    try:
        entries = validate_files(files)
        job = _Job(entries, dest_dir, progress, cancel, free_margin=free_margin, timeout=timeout,
                   retries=retries, backoff=backoff, max_backoff=max_backoff, lock_timeout=lock_timeout,
                   keep_partial=keep_partial, ssl_context=ssl_context)
        return job.run()
    except _Cancelled:
        result = DownloadResult(False, "cancelled", cancelled=True)
        log_system("Download cancelled; what was fetched is kept to resume from.")
    except DownloadError as exc:
        result = DownloadResult(False, str(exc), lasting=exc.lasting, failed_file=exc.file)
        log_error(f"Download failed: {result.reason}")
    except Exception as exc:  # noqa: BLE001 - the contract is a result, not an exception
        result = DownloadResult(False, f"unexpected {_describe(exc)}", lasting=False)
        log_error(f"Download failed: {result.reason}")
    if job is not None:
        result.bytes_fetched = job.fetched
        job._emit("failed", None, force=True, error=result.reason)
    return result


def download_verified(files: Any, dest_dir: str, progress: Optional[Callable[[Dict[str, Any]], None]] = None,
                      **options: Any) -> bool:
    """
    All parts of a model, resumable, each part's size and SHA-256 checked, atomically renamed into
    ``dest_dir``; nothing half-written under a real name. True when every file is there and
    verified (including when they already were). ``options`` are :func:`fetch_verified`'s
    keyword arguments; use that function to learn why a download failed.
    """
    return fetch_verified(files, dest_dir, progress, **options).ok
