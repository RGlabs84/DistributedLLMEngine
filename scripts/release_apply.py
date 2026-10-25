#!/usr/bin/env python3
"""
Install a release on the coordinator host. Standard library only, so it runs from a bare
Python on the box and is copied there fresh by ``scripts/release.py deploy``.

What it guarantees:

* nothing is touched until the archive has matched its manifest (size, SHA-256 and the version
  written inside the code) and every Python file in it compiles;
* everything it is about to replace is backed up first, in a directory named for the version
  being left, so any release can be rolled back;
* after the restart it does not trust the process: it waits for the coordinator to report the
  new version and to serve the very archive that was installed, and if either does not happen
  it puts the old files back and restarts again.

    release_apply.py apply    --root ~/DLNEv2 --archive A.zip --manifest release.json
    release_apply.py rollback --root ~/DLNEv2 [--backup NAME]
    release_apply.py list     --root ~/DLNEv2
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile
from typing import Any, Callable, Dict, List, Optional, Tuple

ARCHIVE_NAME = "dlne-source.zip"
MANIFEST_NAME = "release.json"
RELEASES_DIR = ".releases"
BACKUP_TAR = "files.tar"
BACKUP_META = "backup.json"

# The llama.cpp binaries a release signs (mirrors engine/release.py, which this stdlib-only
# script cannot import): one ggml-rpc-server zip per worker platform, served for download,
# and the upstream build the coordinator itself runs.
RPC_PLATFORMS = ("win-x64", "linux-x64", "macos-arm64", "win-arm64", "linux-arm64", "macos-x64")
SERVER_LLAMA_UPLOAD = "server-llama.tar.gz"
LLAMA_DIR = "llama"      # root/llama/<build>/ holds each installed server build
BIN_LINK = "bin"         # root/bin -> the server build in use (llama-server, ggml-rpc-server)


def rpc_zip_name(platform: str) -> str:
    return f"rpc-bin-{platform}.zip"


def rpc_field(platform: str, what: str) -> str:
    return f"rpc_{platform.replace('-', '_')}_{what}"


# The files a release owns on the host. Anything else in the install directory (models, config,
# logs, the virtualenv, caches) is never read or written by an install or a rollback.
TOP_LEVEL_FILES = ("dlne_server.py", "requirements.txt", ARCHIVE_NAME, MANIFEST_NAME) + tuple(
    rpc_zip_name(p) for p in RPC_PLATFORMS)
# What releases owned before the binaries were signed. A backup made then never captured the
# RPC zips, so rolling back to it must not delete the ones on disk.
_LEGACY_TOP_LEVEL_FILES = ("dlne_server.py", "requirements.txt", ARCHIVE_NAME, MANIFEST_NAME)
PACKAGE_DIR = "engine"

_VERSION_RE = re.compile(r'^VERSION[ \t]*=[ \t]*"([^"]+)"[ \t]*$', re.MULTILINE)


class ApplyError(Exception):
    """An install failed. The message says what, and whether the old release is back."""


# --------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------

def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_atomic(path: str, data: bytes) -> None:
    """Replace ``path`` in one step, so a crash can never leave half a file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = f"{path}.tmp-{os.getpid()}"
    with open(temp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def installed_version(root: str) -> str:
    """The version of the code currently in ``root`` ('none' if there is none)."""
    try:
        with open(os.path.join(root, PACKAGE_DIR, "version.py"), "r", encoding="utf-8") as handle:
            match = _VERSION_RE.search(handle.read())
        return match.group(1) if match else "unknown"
    except OSError:
        return "none"


def managed_files(root: str) -> List[str]:
    """Relative POSIX paths of every release-owned file currently in ``root``."""
    found: List[str] = []
    for name in TOP_LEVEL_FILES:
        if os.path.isfile(os.path.join(root, name)):
            found.append(name)
    package = os.path.join(root, PACKAGE_DIR)
    if os.path.isdir(package):
        for entry in sorted(os.listdir(package)):
            if entry.endswith(".py") and os.path.isfile(os.path.join(package, entry)):
                found.append(f"{PACKAGE_DIR}/{entry}")
    return sorted(found)


def _archive_members(archive: zipfile.ZipFile) -> Dict[str, bytes]:
    """Every file in the archive, refusing any path that is not plainly inside the install."""
    members: Dict[str, bytes] = {}
    for info in archive.infolist():
        name = info.filename
        if info.is_dir():
            continue
        parts = name.split("/")
        if name.startswith("/") or ".." in parts or "\\" in name or ":" in name:
            raise ApplyError(f"The archive contains an unsafe path: {name!r}")
        if not (name in ("dlne_server.py", "requirements.txt")
                or (len(parts) == 2 and parts[0] == PACKAGE_DIR and parts[1].endswith(".py"))):
            raise ApplyError(f"The archive contains a file a release never ships: {name!r}")
        members[name] = archive.read(info)
    return members


# --------------------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------------------

def check_archive(archive_path: str, manifest: Dict, pubkey_hex: Optional[str] = None) -> Dict[str, bytes]:
    """Prove the archive is the one the manifest describes; return its files."""
    if os.path.getsize(archive_path) != manifest.get("size"):
        raise ApplyError("The archive size does not match the manifest.")
    if _sha256_file(archive_path) != manifest.get("sha256"):
        raise ApplyError("The archive hash does not match the manifest.")
    try:
        with zipfile.ZipFile(archive_path) as archive:
            if archive.testzip() is not None:
                raise ApplyError("The archive is corrupt.")
            members = _archive_members(archive)
    except zipfile.BadZipFile as exc:
        raise ApplyError("The archive is not a valid zip file.") from exc

    version_file = members.get("engine/version.py")
    match = _VERSION_RE.search(version_file.decode("utf-8")) if version_file else None
    if not match or match.group(1) != manifest.get("version"):
        raise ApplyError(
            f"The archive's code says version {match.group(1) if match else 'unknown'} "
            f"but the manifest says {manifest.get('version')}."
        )
    for name, data in members.items():
        if name.endswith(".py"):
            try:
                compile(data, name, "exec")
            except SyntaxError as exc:
                raise ApplyError(f"{name} does not compile: {exc.msg} (line {exc.lineno}).") from exc

    if pubkey_hex:
        try:
            from cryptography.exceptions import InvalidSignature
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        except ImportError:
            pass   # the laptop already verified the signature; the hash chain above still holds
        else:
            unsigned = {k: v for k, v in manifest.items() if k != "signature"}
            payload = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
            try:
                Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey_hex)).verify(
                    bytes.fromhex(manifest.get("signature", "")), payload)
            except (InvalidSignature, ValueError) as exc:
                raise ApplyError("The manifest signature does not verify.") from exc
    return members


def _check_file(path: str, size: Any, sha: Any, what: str) -> None:
    try:
        actual = os.path.getsize(path)
    except OSError as exc:
        raise ApplyError(f"The release signs {what} but {path} cannot be read.") from exc
    if actual != size:
        raise ApplyError(f"The {what} size does not match the manifest.")
    if _sha256_file(path) != sha:
        raise ApplyError(f"The {what} hash does not match the manifest.")


def check_assets(root: str, manifest: Dict, assets_dir: Optional[str]) -> Dict[str, str]:
    """
    Prove every binary the manifest signs is present and exact; return name -> path.

    RPC zips come from ``assets_dir``, or are the copies already installed in ``root`` when no
    new ones were uploaded. The server build must be uploaded unless that exact build is
    already installed (its directory records the hash it was installed from).
    """
    found: Dict[str, str] = {}
    for platform in RPC_PLATFORMS:
        sha = manifest.get(rpc_field(platform, "sha256"))
        if sha is None:
            continue
        name = rpc_zip_name(platform)
        path = os.path.join(assets_dir, name) if assets_dir else os.path.join(root, name)
        _check_file(path, manifest.get(rpc_field(platform, "size")), sha, f"{platform} RPC archive")
        found[name] = path
    if "server_llama_sha256" in manifest:
        if assets_dir and os.path.exists(os.path.join(assets_dir, SERVER_LLAMA_UPLOAD)):
            path = os.path.join(assets_dir, SERVER_LLAMA_UPLOAD)
            _check_file(path, manifest.get("server_llama_size"), manifest["server_llama_sha256"], "server llama.cpp build")
            found[SERVER_LLAMA_UPLOAD] = path
        elif _installed_build_sha(root, manifest.get("llama_build", "")) != manifest["server_llama_sha256"]:
            raise ApplyError(f"The release signs llama.cpp {manifest.get('llama_build')} for the server, "
                             "but it was not uploaded and is not installed.")
    elif assets_dir and os.path.exists(os.path.join(assets_dir, SERVER_LLAMA_UPLOAD)):
        raise ApplyError("A server llama.cpp build was supplied but the manifest does not sign it.")
    return found


def _build_dir(root: str, build: str) -> str:
    if not re.match(r"^b\d+$", str(build)):
        raise ApplyError(f"{build!r} is not a llama.cpp build tag.")
    return os.path.join(root, LLAMA_DIR, build)


def _installed_build_sha(root: str, build: str) -> Optional[str]:
    try:
        with open(os.path.join(_build_dir(root, build), ".installed-from"), "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except (OSError, ApplyError):
        return None


def install_server_llama(root: str, build: str, tarball: str, sha256: str) -> str:
    """
    Unpack the upstream build into root/llama/<build>/ (once) and return the directory that
    holds llama-server. The tarball's entries must all sit inside one top directory, as
    upstream ships it; anything that would land elsewhere refuses the whole install.
    """
    target = _build_dir(root, build)
    if _installed_build_sha(root, build) != sha256:
        staging = f"{target}.partial-{os.getpid()}"
        if os.path.isdir(staging):
            shutil.rmtree(staging)
        os.makedirs(staging)
        with tarfile.open(tarball, "r:gz") as tar:
            for member in tar.getmembers():
                parts = member.name.split("/")
                if member.name.startswith("/") or ".." in parts or not (member.isfile() or member.isdir() or member.issym()):
                    raise ApplyError(f"The llama.cpp build contains an unsafe entry: {member.name!r}")
                if member.issym() and (member.linkname.startswith("/") or ".." in member.linkname.split("/")):
                    raise ApplyError(f"The llama.cpp build contains an unsafe link: {member.name!r}")
            tar.extractall(staging, filter="tar")
        with open(os.path.join(staging, ".installed-from"), "w", encoding="utf-8") as handle:
            handle.write(sha256 + "\n")
        if os.path.isdir(target):
            shutil.rmtree(target)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        os.replace(staging, target)
    for dirpath, _dirs, files in os.walk(target):
        if "llama-server" in files:
            return dirpath
    raise ApplyError(f"llama.cpp {build} has no llama-server.")


def current_bin_target(root: str) -> Optional[str]:
    """Where root/bin points (None when it is not a symlink)."""
    link = os.path.join(root, BIN_LINK)
    return os.readlink(link) if os.path.islink(link) else None


def point_bin_at(root: str, target: str) -> None:
    """Switch root/bin to ``target`` in one step. A real bin/ directory is moved aside, never deleted."""
    link = os.path.join(root, BIN_LINK)
    if os.path.lexists(link) and not os.path.islink(link):
        os.replace(link, os.path.join(root, LLAMA_DIR, f"previous-bin-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}"))
    temp = f"{link}.new-{os.getpid()}"
    if os.path.lexists(temp):
        os.remove(temp)
    os.symlink(target, temp)
    os.replace(temp, link)


# --------------------------------------------------------------------------------------
# Backup / install / rollback
# --------------------------------------------------------------------------------------

def make_backup(root: str, label: str) -> str:
    """Tar every release-owned file into ``.releases/<label>`` and return that directory."""
    backup_dir = os.path.join(root, RELEASES_DIR, label)
    os.makedirs(backup_dir, exist_ok=True)
    files = managed_files(root)
    with tarfile.open(os.path.join(backup_dir, BACKUP_TAR), "w") as tar:
        for relative in files:
            tar.add(os.path.join(root, *relative.split("/")), arcname=relative)
    meta = {"version": installed_version(root), "files": files, "owns": list(TOP_LEVEL_FILES),
            "bin_target": current_bin_target(root),
            "made_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    _write_atomic(os.path.join(backup_dir, BACKUP_META), json.dumps(meta, indent=2).encode())
    return backup_dir


def restore_backup(root: str, backup_dir: str) -> List[str]:
    """Put a backup's files back and remove release-owned files it did not contain."""
    with open(os.path.join(backup_dir, BACKUP_META), "r", encoding="utf-8") as handle:
        meta = json.load(handle)
    wanted = set(meta["files"])
    owned = set(meta.get("owns", _LEGACY_TOP_LEVEL_FILES))
    with tarfile.open(os.path.join(backup_dir, BACKUP_TAR), "r") as tar:
        for member in tar.getmembers():
            if member.name not in wanted or not member.isfile():
                raise ApplyError(f"The backup contains an unexpected entry: {member.name!r}")
            data = tar.extractfile(member).read()
            _write_atomic(os.path.join(root, *member.name.split("/")), data)
    removed = []
    for relative in managed_files(root):
        if "/" not in relative and relative not in owned:
            continue  # a file this backup's era did not manage: it is not the backup's to remove
        if relative not in wanted:
            os.remove(os.path.join(root, *relative.split("/")))
            removed.append(relative)
    # The engine binaries the backup ran with (the build directories themselves are kept).
    if meta.get("bin_target") and meta["bin_target"] != current_bin_target(root):
        point_bin_at(root, meta["bin_target"])
    return removed


def _install(root: str, members: Dict[str, bytes], archive_path: str, manifest: Dict, prune: bool,
             assets: Optional[Dict[str, str]] = None) -> List[str]:
    """Write the new files, then drop engine files the release no longer ships. Returns pruned."""
    assets = assets or {}
    for name, data in members.items():
        _write_atomic(os.path.join(root, *name.split("/")), data)
    _write_atomic(os.path.join(root, ARCHIVE_NAME), open(archive_path, "rb").read())
    for name, path in assets.items():
        if name != SERVER_LLAMA_UPLOAD and os.path.abspath(path) != os.path.abspath(os.path.join(root, name)):
            _write_atomic(os.path.join(root, name), open(path, "rb").read())
    if "server_llama_sha256" in manifest:
        server_dir = install_server_llama(root, manifest["llama_build"], assets.get(SERVER_LLAMA_UPLOAD, ""),
                                          manifest["server_llama_sha256"])
        point_bin_at(root, server_dir)
    _write_atomic(os.path.join(root, MANIFEST_NAME), (json.dumps(manifest, indent=2) + "\n").encode())
    pruned: List[str] = []
    if prune:
        for relative in managed_files(root):
            if relative.startswith(PACKAGE_DIR + "/") and relative not in members:
                os.remove(os.path.join(root, *relative.split("/")))
                pruned.append(relative)
    return pruned


Restart = Callable[[], None]
Health = Callable[[str], Tuple[bool, str]]


def apply_release(root: str, archive_path: str, manifest: Dict, restart: Restart, health: Health,
                  pubkey_hex: Optional[str] = None, prune: bool = True,
                  log: Callable[[str], None] = print, assets_dir: Optional[str] = None) -> Dict:
    """
    Install ``archive_path`` into ``root``. On any failure after the first file is replaced the
    previous release is restored and restarted before the error is raised.

    The llama.cpp binaries the manifest signs come from ``assets_dir`` (as uploaded by
    scripts/release.py); without it the copies already installed are used. Either way every
    one must match its signed hash before anything is touched.
    """
    version = manifest["version"]
    previous = installed_version(root)
    log(f"Installing {version} over {previous}.")

    members = check_archive(archive_path, manifest, pubkey_hex)
    log(f"Archive verified: {len(members)} files, sha256 {manifest['sha256'][:12]}...")
    assets = check_assets(root, manifest, assets_dir)
    if manifest.get("llama_build"):
        log(f"llama.cpp {manifest['llama_build']} (RPC {manifest.get('rpc_proto')}) verified: {', '.join(sorted(assets)) or 'already installed'}.")

    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    backup_dir = make_backup(root, f"{previous}-{stamp}")
    log(f"Backed up the current release to {os.path.relpath(backup_dir, root)}.")

    try:
        pruned = _install(root, members, archive_path, manifest, prune, assets)
        if pruned:
            log(f"Removed {len(pruned)} file(s) the release no longer ships: {', '.join(pruned)}")
        log("Files installed. Restarting.")
        restart()
        ok, detail = health(version)
        if not ok:
            raise ApplyError(f"The coordinator did not come up as {version}: {detail}")
    except Exception as exc:
        log(f"FAILED: {exc}. Restoring {previous}.")
        try:
            restore_backup(root, backup_dir)
            restart()
            back_ok, back_detail = health(previous) if previous not in ("none", "unknown") else (True, "no previous version to check")
        except Exception as restore_exc:  # noqa: BLE001 - report both, the operator must know
            raise ApplyError(
                f"Install failed ({exc}) AND the rollback failed ({restore_exc}). "
                f"The backup is in {backup_dir}; restore it by hand."
            ) from restore_exc
        state = "the previous release is running again" if back_ok else f"the previous release did NOT come back ({back_detail})"
        raise ApplyError(f"Install failed: {exc}. Rolled back; {state}.") from exc

    log(f"Release {version} is live.")
    return {"version": version, "previous": previous, "backup": os.path.basename(backup_dir), "pruned": pruned}


def list_backups(root: str) -> List[Dict]:
    base = os.path.join(root, RELEASES_DIR)
    found = []
    if os.path.isdir(base):
        for name in sorted(os.listdir(base)):
            meta_path = os.path.join(base, name, BACKUP_META)
            if os.path.isfile(meta_path):
                with open(meta_path, "r", encoding="utf-8") as handle:
                    found.append({"name": name, **json.load(handle)})
    return found


def rollback_release(root: str, backup_name: Optional[str], restart: Restart, health: Health,
                     log: Callable[[str], None] = print) -> Dict:
    """Restore a named backup (or the newest) and restart. Backs up the current state first."""
    backups = list_backups(root)
    if not backups:
        raise ApplyError("There are no backups to roll back to.")
    chosen = next((b for b in backups if b["name"] == backup_name), None) if backup_name else backups[-1]
    if chosen is None:
        raise ApplyError(f"No backup named {backup_name!r}. Available: {', '.join(b['name'] for b in backups)}")
    current = installed_version(root)
    safety = make_backup(root, f"{current}-before-rollback-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}")
    log(f"Saved the current state to {os.path.relpath(safety, root)}.")
    removed = restore_backup(root, os.path.join(root, RELEASES_DIR, chosen["name"]))
    log(f"Restored {chosen['name']} (version {chosen['version']}); removed {len(removed)} newer file(s). Restarting.")
    restart()
    ok, detail = health(chosen["version"])
    if not ok:
        raise ApplyError(f"Restored the files but the coordinator is not healthy: {detail}")
    return {"restored": chosen["name"], "version": chosen["version"]}


# --------------------------------------------------------------------------------------
# Real restart and health (the parts tests replace)
# --------------------------------------------------------------------------------------

def systemd_restart(unit: str, user: bool = True) -> Restart:
    command = ["systemctl"] + (["--user"] if user else []) + ["restart", unit]

    def restart() -> None:
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            raise ApplyError(f"Restarting {unit} failed: {result.stderr.strip() or result.stdout.strip()}")

    return restart


def _http_get(url: str, timeout: float = 5.0) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - loopback URL chosen by the operator
        return response.read()


def http_health(base_url: str, manifest_for: Callable[[str], Optional[Dict]], timeout: float = 120.0,
                poll: float = 2.0, sleep: Callable[[float], None] = time.sleep) -> Health:
    """
    Healthy means: the coordinator answers, reports the expected version, serves a release
    manifest with the expected hash, and the archive it serves hashes to that value.
    """
    base = base_url.rstrip("/")

    def health(expected_version: str) -> Tuple[bool, str]:
        deadline = time.monotonic() + timeout
        last = "no answer yet"
        while time.monotonic() < deadline:
            try:
                status = json.loads(_http_get(f"{base}/v1/cluster/status"))
                running = status.get("server_version")
                if running != expected_version:
                    last = f"it reports version {running!r}, expected {expected_version!r}"
                else:
                    expected = manifest_for(expected_version)
                    if expected is None:
                        return (True, "up (no release manifest to compare)")
                    served = json.loads(_http_get(f"{base}/v1/release"))
                    if served.get("sha256") != expected.get("sha256"):
                        return (False, "it serves a different release manifest than the one installed")
                    digest = _sha256_bytes(_http_get(f"{base}/v1/download/source", timeout=30.0))
                    if digest != expected.get("sha256"):
                        return (False, "the archive it serves does not hash to the manifest")
                    for platform in RPC_PLATFORMS:
                        sha = expected.get(rpc_field(platform, "sha256"))
                        if sha is None:
                            continue
                        digest = _sha256_bytes(_http_get(f"{base}/v1/download/rpc-bin/{platform}", timeout=60.0))
                        if digest != sha:
                            return (False, f"the {platform} RPC binaries it serves do not hash to the manifest")
                    return (True, "up, serving the installed release")
            except Exception as exc:  # noqa: BLE001 - connection refused while it boots is expected
                last = f"{type(exc).__name__}: {exc}"
            sleep(poll)
        return (False, f"timed out after {int(timeout)}s ({last})")

    return health


# --------------------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------------------

def _manifest_from_root(root: str) -> Callable[[str], Optional[Dict]]:
    def read(_version: str) -> Optional[Dict]:
        try:
            with open(os.path.join(root, MANIFEST_NAME), "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return None

    return read


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("apply", "rollback", "list"):
        p = sub.add_parser(name)
        p.add_argument("--root", required=True)
        if name != "list":
            p.add_argument("--unit", default="dlne.service")
            p.add_argument("--system", action="store_true", help="system unit instead of a user unit")
            p.add_argument("--url", default="http://127.0.0.1:11435")
            p.add_argument("--timeout", type=float, default=120.0)
    sub.choices["apply"].add_argument("--archive", required=True)
    sub.choices["apply"].add_argument("--manifest", required=True)
    sub.choices["apply"].add_argument("--pubkey", default=None)
    sub.choices["apply"].add_argument("--assets", default=None, help="directory holding the uploaded llama.cpp binaries")
    sub.choices["apply"].add_argument("--keep-stale", action="store_true", help="do not remove engine files the release dropped")
    sub.choices["rollback"].add_argument("--backup", default=None)
    args = parser.parse_args(argv)
    root = os.path.abspath(os.path.expanduser(args.root))

    try:
        if args.command == "list":
            for b in list_backups(root):
                print(f"{b['name']}  version {b['version']}  ({len(b['files'])} files)")
            return 0
        restart = systemd_restart(args.unit, user=not args.system)
        health = http_health(args.url, _manifest_from_root(root), timeout=args.timeout)
        if args.command == "apply":
            with open(args.manifest, "r", encoding="utf-8") as handle:
                manifest = json.load(handle)
            apply_release(root, args.archive, manifest, restart, health, pubkey_hex=args.pubkey,
                          prune=not args.keep_stale, assets_dir=args.assets)
        else:
            rollback_release(root, args.backup, restart, health)
        return 0
    except ApplyError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
