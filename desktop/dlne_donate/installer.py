"""
Fetching the engine, and refusing to run anything that is not the maintainer's.

This is vscode-extension/src/worker/engineRelease.ts (and the install half of workerManager.ts) in Python:

1. ``GET <coordinator>/v1/release`` is the signed manifest. Its Ed25519 signature must verify against the key baked
   into this app (engine/release.py ``RELEASE_PUBKEY``). Not signed, or signed by anyone else: stop.
2. The source archive (``/v1/download/source``) must have exactly the signed size and SHA-256 and the signed version
   inside it. Its entries must be the files a release is allowed to hold (``dlne_server.py``, ``requirements.txt``,
   ``engine/*.py``, ``engine/static/*.png``), nothing else, so an archive cannot write outside the engine folder.
3. The same for the llama.cpp programs for this platform (``/v1/download/rpc-bin/<platform>``): signed size and hash,
   and nothing in the zip but ``bin/<the servers and their libraries>``.
4. A release never goes backwards: the engine already installed is never replaced by an older one.

``ensure()`` returns a :class:`VerifiedEngine`, and the worker controller only starts a worker from one of those. There
is no way to build one except by passing through the checks above, or by re-checking every file already on disk against
the hashes recorded when it was verified.
"""

import hashlib
import io
import json
import os
import re
import functools
import shutil
import ssl
import stat
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import __version__, bundled

RELEASE_RECORD = ".dlne_release.json"
RPC_RECORD = ".dlne_rpc_bin.json"

_MAX_MANIFEST_BYTES = 1_000_000
_MAX_MEMBER_BYTES = 32 * 1024 * 1024         # no engine source file or icon is anywhere near this
_MAX_ARCHIVE_UNPACKED = 128 * 1024 * 1024    # a release is about 1.2 MB packed
_MAX_RPC_UNPACKED = 1024 * 1024 * 1024       # llama.cpp's CPU + GPU backends are a few hundred MB unpacked

# What a source release may contain (engine/release.py: SHIPPED_FILES, SHIPPED_PACKAGES, SHIPPED_ASSET_DIRS).
_SOURCE_MEMBER_RE = re.compile(r"^(dlne_server\.py|requirements\.txt|engine/[A-Za-z0-9_]+\.py|engine/static/[A-Za-z0-9_.-]+\.png)$")


class VerificationError(Exception):
    """The release cannot be trusted. ``str(error)`` is written to be shown to the owner as it is."""


class FetchError(Exception):
    """The coordinator or a download could not be reached (says nothing about signing)."""


class _HttpStatus(Exception):
    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.status = status


# --------------------------------------------------------------------------------------------------
# Network (replaceable, so tests never touch one)
# --------------------------------------------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def tls_context() -> ssl.SSLContext:
    """
    What HTTPS certificates are checked against: the system's own roots, plus Mozilla's list from ``certifi``, which
    the app carries anyway (the engine's httpx uses it).

    A fresh Windows fetches root certificates only when Windows' own networking first needs them. Python reads the store
    as it is, so on a clean install the root behind the coordinator's certificate (Let's Encrypt's) can simply be
    missing, and every request failed with "certificate verify failed" (found on a blank Windows 11 VM). certifi makes
    that root always present; the system store stays, so a network that adds its own root (a company proxy) still works.
    """
    context = ssl.create_default_context()
    try:
        import certifi
        context.load_verify_locations(cafile=certifi.where())
    except (ImportError, OSError, ssl.SSLError):
        pass                    # the system store alone, as before
    return context


class Http:
    """The two requests the installer makes. Tests pass an object with the same two methods."""

    user_agent = f"DLNE-Donate/{__version__}"

    def get_json(self, url: str, timeout: float = 15.0) -> Tuple[int, Any]:
        """``(status, parsed JSON)``; JSON that does not parse is ``(status, None)``. Raises FetchError when unreachable."""
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=tls_context()) as response:
                body = response.read(_MAX_MANIFEST_BYTES + 1)
                status = response.status
        except urllib.error.HTTPError as error:
            return error.code, None
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise FetchError(f"Could not reach {url}: {getattr(error, 'reason', error)}") from error
        if len(body) > _MAX_MANIFEST_BYTES:
            raise FetchError("the coordinator's answer was too large")
        try:
            return status, json.loads(body.decode("utf-8"))
        except ValueError:
            return status, None

    def download(self, url: str, destination: str, expected_size: int,
                 progress: Optional[Callable[[int, int], None]] = None, timeout: float = 60.0) -> None:
        """Save ``url`` to ``destination``. Never writes more than ``expected_size`` bytes (the signed size)."""
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        written = 0
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=tls_context()) as response, \
                    open(destination, "wb") as out:
                while True:
                    block = response.read(1 << 20)
                    if not block:
                        break
                    written += len(block)
                    if written > expected_size:
                        raise FetchError("the download is larger than the signed release says; it was not kept")
                    out.write(block)
                    if progress:
                        progress(written, expected_size)
        except urllib.error.HTTPError as error:
            raise FetchError(f"The download failed with HTTP {error.code}") from error
        except (urllib.error.URLError, OSError) as error:
            raise FetchError(f"The download failed: {getattr(error, 'reason', error)}") from error


# --------------------------------------------------------------------------------------------------
# The verified engine
# --------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class VerifiedEngine:
    """An engine folder whose files were checked against a signed release. Only :class:`EngineInstaller` makes one."""
    root: str
    version: str
    manifest: Dict[str, Any]
    rpc_platform: Optional[str]
    _token: object = field(repr=False, default=None, compare=False)

    @property
    def script(self) -> str:
        return os.path.join(self.root, "dlne_server.py")

    @property
    def config_file(self) -> str:
        return os.path.join(self.root, "config", "config.txt")


_MINT = object()   # only this module holds it


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_record(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_json_atomic(path: str, data: Dict[str, Any]) -> None:
    scratch = f"{path}.{os.getpid()}.tmp"
    with open(scratch, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    os.replace(scratch, path)


def version_tuple(version: str) -> Optional[Tuple[int, int, int]]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", str(version))
    return (int(match.group(1)), int(match.group(2)), int(match.group(3))) if match else None


def _safe_target(root: str, member: str) -> str:
    """The path ``member`` lands on under ``root``; raises when it would land anywhere else."""
    if not member or member.startswith(("/", "\\")) or "\\" in member or ":" in member or "\x00" in member:
        raise VerificationError(f"The release holds a file with an unsafe name ({member[:60]!r}); it was NOT installed.")
    parts = member.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise VerificationError(f"The release holds a file with an unsafe name ({member[:60]!r}); it was NOT installed.")
    target = os.path.normpath(os.path.join(root, *parts))
    if os.path.commonpath([os.path.abspath(root), os.path.abspath(target)]) != os.path.abspath(root):
        raise VerificationError(f"The release holds a file that would leave the engine folder ({member[:60]!r}); it was NOT installed.")
    return target


class EngineInstaller:
    """Brings ``root`` up to the coordinator's signed release, or says why it will not."""

    def __init__(self, root: str, base_url: str, http: Any = None, public_key: Optional[str] = None,
                 platform: Optional[str] = None, log: Callable[[str], None] = lambda _m: None,
                 progress: Optional[Callable[[str, float], None]] = None):
        self.root = root
        self.base = base_url.rstrip("/")
        self.http = http or Http()
        self.public_key = public_key
        self.release = bundled.load_release()
        self.platform = platform if platform is not None else self.release.host_rpc_platform()
        self.log = log
        self.progress = progress or (lambda _what, _fraction: None)

    # ---- the signed manifest -----------------------------------------------------------------------

    def fetch_manifest(self) -> Dict[str, Any]:
        """The coordinator's release manifest, verified. Raises VerificationError if it is not the maintainer's."""
        status, body = self.http.get_json(f"{self.base}/v1/release")
        if status == 404:
            raise VerificationError("This coordinator does not publish a signed release, so DLNE Donate will not run its engine.")
        if status != 200 or body is None:
            raise FetchError(f"The coordinator's release manifest could not be read (HTTP {status}).")
        ok, reason = self.release.verify_manifest(body, self.public_key)
        if not ok:
            raise VerificationError(f"The engine release from {self.base} failed verification ({reason}) and was NOT installed.")
        return body

    # ---- installing -------------------------------------------------------------------------------

    def ensure(self, want_rpc: bool = True) -> VerifiedEngine:
        """
        The verified manifest, then the engine and the llama.cpp programs it names, installed if they are not already.
        Nothing under ``root`` changes unless every check passed.
        """
        os.makedirs(self.root, exist_ok=True)
        manifest = self.fetch_manifest()
        self._ensure_source(manifest)
        rpc_platform = None
        if want_rpc:
            rpc_platform = self._ensure_rpc(manifest)
        engine = self.check_on_disk(manifest, rpc_platform)
        return engine

    def _ensure_source(self, manifest: Dict[str, Any]) -> None:
        record = _read_record(os.path.join(self.root, RELEASE_RECORD))
        if record:
            order = self._compare(manifest["version"], str(record.get("version", "")))
            if order is not None and order < 0:
                raise VerificationError(
                    f"The coordinator offers engine {manifest['version']}, which is older than the {record.get('version')} "
                    "already installed. Releases only move forward, so it was NOT installed.")
            if record.get("sha256") == manifest["sha256"] and self._files_intact(record):
                self.log(f"Engine {manifest['version']} is already installed and verified.")
                return
        self.log(f"Downloading engine {manifest['version']} ({manifest['size'] // 1024} KB)...")
        self.progress("engine", 0.0)
        scratch = tempfile.mkdtemp(prefix="engine-", dir=self.root)
        try:
            archive = os.path.join(scratch, "dlne-source.zip")
            self.http.download(f"{self.base}/v1/download/source", archive, int(manifest["size"]),
                               lambda done, total: self.progress("engine", done / max(total, 1)))
            ok, reason = self.release.verify_archive(manifest, archive)
            if not ok:
                cap = reason[:1].upper() + reason[1:]
                raise VerificationError(f"{cap}; it was NOT installed.")
            members = self._read_source_members(archive)
            self._install_source(manifest, members, record)
            self.progress("engine", 1.0)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    @staticmethod
    def _compare(a: str, b: str) -> Optional[int]:
        ta, tb = version_tuple(a), version_tuple(b)
        if ta is None or tb is None:
            return None
        return (ta > tb) - (ta < tb)

    def _read_source_members(self, archive_path: str) -> Dict[str, bytes]:
        """Every file of the (already hash-verified) archive, held in memory after checking each name and size."""
        members: Dict[str, bytes] = {}
        total = 0
        with zipfile.ZipFile(archive_path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                name = info.filename
                _safe_target(self.root, name)
                if not _SOURCE_MEMBER_RE.match(name):
                    raise VerificationError(f"The release holds a file a release never ships ({name[:60]!r}); it was NOT installed.")
                if info.file_size > _MAX_MEMBER_BYTES:
                    raise VerificationError(f"The release holds an unexpectedly large file ({name[:60]!r}); it was NOT installed.")
                total += info.file_size
                if total > _MAX_ARCHIVE_UNPACKED:
                    raise VerificationError("The release unpacks to far more than a release is; it was NOT installed.")
                with archive.open(info) as handle:
                    data = handle.read(_MAX_MEMBER_BYTES + 1)
                if len(data) != info.file_size:
                    raise VerificationError(f"The release is damaged ({name[:60]!r}); it was NOT installed.")
                members[name] = data
        for needed in ("dlne_server.py", "engine/worker.py", "engine/version.py"):
            if needed not in members:
                raise VerificationError(f"The release has no {needed}; it was NOT installed.")
        return members

    def _install_source(self, manifest: Dict[str, Any], members: Dict[str, bytes], old: Optional[Dict[str, Any]]) -> None:
        hashes: Dict[str, str] = {}
        for name, data in sorted(members.items()):
            target = _safe_target(self.root, name)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            scratch = f"{target}.{os.getpid()}.new"
            with open(scratch, "wb") as handle:
                handle.write(data)
            os.replace(scratch, target)
            hashes[name] = _sha256_bytes(data)
        # A file of the previous release that the new one dropped must not stay behind to be imported.
        old_files = (old or {}).get("files")
        for name in (old_files if isinstance(old_files, dict) else {}):
            if name not in members and _SOURCE_MEMBER_RE.match(name):
                try:
                    os.remove(_safe_target(self.root, name))
                except OSError:
                    pass
        _write_json_atomic(os.path.join(self.root, RELEASE_RECORD), {
            "version": manifest["version"], "sha256": manifest["sha256"], "files": hashes,
            "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        self.log(f"Engine {manifest['version']} installed (signature, size and SHA-256 verified).")

    def _files_intact(self, record: Dict[str, Any]) -> bool:
        files = record.get("files")
        if not isinstance(files, dict) or not files:
            return False
        for name, expected in files.items():
            try:
                target = _safe_target(self.root, name)
                if _sha256_file(target) != expected:
                    return False
            except (OSError, VerificationError):
                return False
        return True

    # ---- the llama.cpp programs --------------------------------------------------------------------

    def _ensure_rpc(self, manifest: Dict[str, Any]) -> Optional[str]:
        platform = self.platform
        if not platform:
            raise VerificationError("DLNE ships no GPU/CPU engine programs for this operating system and processor.")
        sha = manifest.get(self.release.rpc_field(platform, "sha256"))
        if not isinstance(sha, str):
            raise VerificationError(f"The release from {self.base} does not sign the engine programs for {platform}, so they were NOT installed.")
        record = _read_record(os.path.join(self.root, RPC_RECORD))
        exe = "ggml-rpc-server.exe" if platform.startswith("win") else "ggml-rpc-server"
        if record and record.get("sha256") == sha and os.path.exists(os.path.join(self.root, "bin", exe)) \
                and self._rpc_files_intact(record):
            self.log("The llama.cpp programs are already installed and verified.")
            return platform
        size = int(manifest[self.release.rpc_field(platform, "size")])
        self.log(f"Downloading the llama.cpp {manifest.get('llama_build', '')} programs for {platform} ({size // (1024 * 1024)} MB)...")
        self.progress("programs", 0.0)
        scratch = tempfile.mkdtemp(prefix="rpc-", dir=self.root)
        try:
            archive = os.path.join(scratch, "rpc-bin.zip")
            self.http.download(f"{self.base}/v1/download/rpc-bin/{platform}", archive, size,
                               lambda done, total: self.progress("programs", done / max(total, 1)), timeout=120.0)
            ok, reason = self.release.verify_rpc_bin(manifest, platform, archive)
            if not ok:
                cap = reason[:1].upper() + reason[1:]
                raise VerificationError(f"{cap}; it was NOT installed.")
            try:
                self.release.check_rpc_bin(archive)
            except self.release.ReleaseError as error:
                raise VerificationError(f"{error}; it was NOT installed.") from error
            hashes = self._unpack_rpc(archive)
            _write_json_atomic(os.path.join(self.root, RPC_RECORD), {
                "sha256": sha, "llama_build": manifest.get("llama_build"), "rpc_proto": manifest.get("rpc_proto"),
                "files": hashes, "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            })
            self.progress("programs", 1.0)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        return platform

    def _rpc_files_intact(self, record: Dict[str, Any]) -> bool:
        files = record.get("files")
        if not isinstance(files, dict) or not files:
            return False   # installed by an older tool, or not by us: replaced by the signed ones
        for name, expected in files.items():
            try:
                if _sha256_file(_safe_target(self.root, name)) != expected:
                    return False
            except (OSError, VerificationError):
                return False
        return True

    def _unpack_rpc(self, archive_path: str) -> Dict[str, str]:
        hashes: Dict[str, str] = {}
        total = 0
        with zipfile.ZipFile(archive_path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                target = _safe_target(self.root, info.filename)
                total += info.file_size
                if total > _MAX_RPC_UNPACKED:
                    raise VerificationError("The engine programs unpack to far more than they should; they were NOT installed.")
                os.makedirs(os.path.dirname(target), exist_ok=True)
                scratch = f"{target}.{os.getpid()}.new"
                digest = hashlib.sha256()
                with archive.open(info) as source, open(scratch, "wb") as out:
                    for block in iter(lambda: source.read(1 << 20), b""):
                        digest.update(block)
                        out.write(block)
                if os.name == "posix" and os.path.basename(target).startswith(("ggml-rpc-server", "llama-server")) \
                        and not target.endswith((".so", ".dylib")):
                    os.chmod(scratch, os.stat(scratch).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                os.replace(scratch, target)
                hashes[info.filename] = digest.hexdigest()
        return hashes

    # ---- the gate ---------------------------------------------------------------------------------

    def check_on_disk(self, manifest: Dict[str, Any], rpc_platform: Optional[str]) -> VerifiedEngine:
        """Re-hash every installed file against the record written when it was verified; mint the VerifiedEngine."""
        record = _read_record(os.path.join(self.root, RELEASE_RECORD))
        if not record or record.get("sha256") != manifest.get("sha256") or not self._files_intact(record):
            raise VerificationError("The installed engine does not match the signed release; it will not be run.")
        if rpc_platform:
            rpc = _read_record(os.path.join(self.root, RPC_RECORD))
            if not rpc or rpc.get("sha256") != manifest.get(self.release.rpc_field(rpc_platform, "sha256")) \
                    or not self._rpc_files_intact(rpc):
                raise VerificationError("The installed engine programs do not match the signed release; they will not be run.")
        return VerifiedEngine(root=self.root, version=str(manifest["version"]), manifest=manifest,
                              rpc_platform=rpc_platform, _token=_MINT)


def is_verified(engine: Any) -> bool:
    """Whether ``engine`` is a VerifiedEngine this module made (a look-alike object fails)."""
    return isinstance(engine, VerifiedEngine) and engine._token is _MINT


# --------------------------------------------------------------------------------------------------
# Can this app's frozen libraries run this engine?
# --------------------------------------------------------------------------------------------------

def missing_modules(engine_root: str, available: Optional[Callable[[str], bool]] = None) -> List[str]:
    """
    The third-party modules the engine imports that this app does not carry. The app is built with the libraries the
    engine of its day needs; a later engine that wants another one cannot run here, and the owner is told to update
    the app instead of the worker dying with an ImportError.
    """
    import ast
    import importlib.util
    import sys

    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    optional = {"gi"}     # the engine's Linux dependency probe imports it lazily, inside a try
    find = available or (lambda name: importlib.util.find_spec(name) is not None)
    needed = set()
    paths = [os.path.join(engine_root, "dlne_server.py")]
    engine_pkg = os.path.join(engine_root, "engine")
    if os.path.isdir(engine_pkg):
        paths += [os.path.join(engine_pkg, name) for name in sorted(os.listdir(engine_pkg)) if name.endswith(".py")]
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                top = name.split(".")[0]
                if top not in stdlib and top != "engine" and top not in optional:
                    needed.add(top)
    return sorted(name for name in needed if not find(name))
