import os
import sys
import platform
import urllib.request
import zipfile
from typing import Optional
from engine.logger import log_system, log_success, log_error

# The llama.cpp build is not chosen here. It is pinned per release by scripts/package_llama.py
# and named in the coordinator's signed release (llama_build / rpc_proto); sync_rpc_bin below
# installs exactly that build.

def test_binary_compatibility(binary_path: str, _allow_dependency_install: bool = True) -> bool:
    """Tests if a binary can run on the host CPU without an illegal instruction crash or missing libs.

    ``_allow_dependency_install`` is internal: when a missing Vulkan loader is detected the
    function offers to install it and then re-tests the binary, and this flag makes that
    retry a single hop. Without it a loader that installs "successfully" but still does not
    satisfy the binary would send the function into unbounded recursion.
    """
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
            
        elif result.returncode == 127 or result.returncode == 3221225781 or result.returncode == -1073741515:
            log_error(f"Missing Shared Library (DLL) [Code {result.returncode}] for {os.path.basename(binary_path)}: {err_msg}")
            log_error("Hint: Ensure required .dll files (like libgomp-1.dll, libgcc_s_seh-1.dll, or vulkan-1.dll) are in the 'bin' folder.")
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


def sync_rpc_bin(coord_url: str, base_dir: str, platform_key: str, urlopen=urllib.request.urlopen) -> bool:
    """
    Make ``base_dir``/bin hold the ggml-rpc-server build the coordinator's signed release names
    for this platform. Returns True when it installed one, False when the installed one is
    already it.

    These are executables the worker runs, so "the coordinator served it" is not enough: the
    manifest must verify against the baked-in release key, the zip must have exactly the
    signed size and SHA-256, and hold nothing but bin/<server and its libraries>. Raises
    ReleaseError otherwise, having written nothing outside a temporary download that is
    always removed. This is also how a worker follows a llama.cpp upgrade: a new release names
    a new zip, and the next start installs it.
    """
    import json
    from engine import release

    with urlopen(f"{coord_url}/v1/release", timeout=15) as response:
        manifest = json.loads(response.read(1_000_000))
    ok, reason = release.verify_manifest(manifest)
    if not ok:
        raise release.ReleaseError(f"the coordinator's release failed verification ({reason})")
    sha = manifest.get(release.rpc_field(platform_key, "sha256"))
    if sha is None:
        raise release.ReleaseError(f"the coordinator's signed release has no RPC build for {platform_key}")

    record_path = os.path.join(base_dir, RPC_RECORD)
    exe = "ggml-rpc-server.exe" if platform_key.startswith("win") else "ggml-rpc-server"
    try:
        with open(record_path, "r", encoding="utf-8") as handle:
            if json.load(handle).get("sha256") == sha and os.path.exists(os.path.join(base_dir, "bin", exe)):
                return False
    except (OSError, ValueError, AttributeError):
        pass

    zip_dest = os.path.join(base_dir, "rpc-bin.zip.download")
    size_mb = manifest[release.rpc_field(platform_key, "size")] // (1024 * 1024)
    log_system(f"Downloading the signed llama.cpp {manifest.get('llama_build')} RPC engine for {platform_key} ({size_mb} MB)...")
    try:
        with urlopen(f"{coord_url}/v1/download/rpc-bin/{platform_key}", timeout=120) as response, open(zip_dest, "wb") as out:
            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                out.write(chunk)
        ok, reason = release.verify_rpc_bin(manifest, platform_key, zip_dest)
        if not ok:
            raise release.ReleaseError(reason)
        release.check_rpc_bin(zip_dest)
        with zipfile.ZipFile(zip_dest, "r") as zf:
            zf.extractall(base_dir)   # every entry is bin/<flat name>, checked just above
        if not platform_key.startswith("win"):
            os.chmod(os.path.join(base_dir, "bin", exe), 0o755)
        with open(record_path, "w", encoding="utf-8") as handle:
            json.dump({"sha256": sha, "llama_build": manifest.get("llama_build"), "rpc_proto": manifest.get("rpc_proto")}, handle)
        log_success(f"Installed llama.cpp {manifest.get('llama_build')} ggml-rpc-server (RPC {manifest.get('rpc_proto')}).")
        return True
    finally:
        try:
            os.remove(zip_dest)
        except OSError:
            pass


def download_and_extract_engine(role: str, coordinator_url: Optional[str] = None):
    """
    Locates the highest CPU-tier bundled llama.cpp engine binary available in the bin directory.
    role: 'coordinator' (needs llama-server) or 'worker' (needs ggml-rpc-server)
    coordinator_url: for a worker that is not on the coordinator's host, where to fetch the
    signed ggml-rpc-server build from (see sync_rpc_bin).
    """
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

    log_error(f"FATAL: Could not locate a compatible bundled binary for '{target_bin_base}'. Please ensure your CPU supports AVX2.")
    return None
