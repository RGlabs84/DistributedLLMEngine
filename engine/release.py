"""
Releases: how a version of DLNE is built, signed and checked.

The coordinator is the version authority. What it serves at /v1/download/source IS the engine
every worker runs, so that archive needs three properties the hand-built one never had:

* it is REPRODUCIBLE: the same source tree always produces the same bytes, so a hash means
  something and two builds can be compared;
* it is NAMED: a signed manifest says "version X is archive with SHA-256 Y", so a node can
  tell the archive it received is the one the maintainer released;
* it is CONSISTENT: the version the manifest claims is the version written inside the code.

This module is the library for all of that. The operator CLI (scripts/release.py) and the
box-side installer (scripts/release_apply.py) are thin wrappers; the VS Code extension verifies
the same manifest in TypeScript, which is why the signed text is restricted to printable ASCII:
Python's ``json.dumps`` and JavaScript's ``JSON.stringify`` only agree byte for byte there.
"""

import hashlib
import io
import json
import os
import re
import zipfile
from typing import Any, Dict, List, Optional, Tuple

# The Ed25519 public key every node trusts. The matching private key is held by the
# maintainer (never in this repository). Rotating it means shipping a new extension.
RELEASE_PUBKEY = "af3a6d4203ab98e0149603a4d6322360723bd6fb357079ce1fcdafaee084fa6a"

MANIFEST_SCHEMA = 1
ARCHIVE_NAME = "dlne-source.zip"
MANIFEST_NAME = "release.json"

# The llama.cpp ggml-rpc-server every worker runs, one zip per platform, served at
# /v1/download/rpc-bin/<platform>. They are executables, so the signed manifest names each by
# size and SHA-256 (rpc_<platform>_sha256 / rpc_<platform>_size, with "-" in the platform
# written "_"), together with the upstream build they come from (llama_build) and its RPC
# protocol (rpc_proto). The fields are optional so the manifest stays schema 1: an older
# extension still verifies the signature over them and ignores them.
RPC_PLATFORMS = ("win-x64", "linux-x64", "macos-arm64")
RPC_EXECUTABLES = ("ggml-rpc-server", "ggml-rpc-server.exe")
# What an RPC zip may hold: bin/<name>, flat, where name is the server or one of its libraries.
_RPC_MEMBER_RE = re.compile(
    r"^bin/(ggml-rpc-server(\.exe)?|(lib)?ggml[A-Za-z0-9_.-]*\.(dll|so(\.[0-9.]+)?|dylib)|libomp\.dll|LICENSE)$"
)
_PROTO_RE = re.compile(r"^\d+\.\d+\.\d+$")
_BUILD_RE = re.compile(r"^b\d+$")


def rpc_zip_name(platform: str) -> str:
    return f"rpc-bin-{platform}.zip"


def rpc_field(platform: str, what: str) -> str:
    """``rpc_field("win-x64", "sha256") == "rpc_win_x64_sha256"``."""
    return f"rpc_{platform.replace('-', '_')}_{what}"


def proto_major(proto: Any) -> Optional[int]:
    """The major of an RPC protocol version like "8.0.1"; None when it is not one."""
    if isinstance(proto, str) and _PROTO_RE.match(proto):
        return int(proto.split(".")[0])
    return None

# An ALLOWLIST: a file ships because it is named here, never because it happened to be in the
# directory. A stray secrets file, a scratch script or a test fixture cannot leak into a release.
SHIPPED_FILES = ("dlne_server.py", "requirements.txt")
SHIPPED_PACKAGES = ("engine",)

# Every archive entry gets the same timestamp and permissions, so the output depends only on
# names and contents.
_FIXED_DATE_TIME = (1980, 1, 1, 0, 0, 0)
_FILE_MODE = 0o100644 << 16

_VERSION_RE = re.compile(r'^VERSION[ \t]*=[ \t]*"([^"]+)"[ \t]*$', re.MULTILINE)
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ReleaseError(Exception):
    """A release cannot be built or does not verify. The message is safe to show."""


# --------------------------------------------------------------------------------------
# Versions
# --------------------------------------------------------------------------------------

def read_version(repo_root: str) -> str:
    """The version written in engine/version.py (the one place a version lives)."""
    path = os.path.join(repo_root, "engine", "version.py")
    with open(path, "r", encoding="utf-8") as handle:
        match = _VERSION_RE.search(handle.read())
    if not match:
        raise ReleaseError("engine/version.py has no VERSION = \"x.y.z\" line.")
    return match.group(1)


def write_version(repo_root: str, version: str) -> None:
    """Set the version in engine/version.py, leaving the rest of the file untouched."""
    if not _SEMVER_RE.match(version):
        raise ReleaseError(f"'{version}' is not a version like 2.0.0.")
    path = os.path.join(repo_root, "engine", "version.py")
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    if not _VERSION_RE.search(text):
        raise ReleaseError("engine/version.py has no VERSION = \"x.y.z\" line.")
    updated = _VERSION_RE.sub(lambda _m: f'VERSION = "{version}"', text, count=1)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(updated)


def parse_version(version: str) -> Tuple[int, int, int]:
    if not _SEMVER_RE.match(str(version)):
        raise ReleaseError(f"'{version}' is not a version like 2.0.0.")
    major, minor, patch = (int(part) for part in version.split("."))
    return (major, minor, patch)


def bump_version(current: str, part: str) -> str:
    """``bump_version("1.1.12", "minor") == "1.2.0"``; ``part`` may also be an explicit version."""
    if _SEMVER_RE.match(part):
        return part
    major, minor, patch = parse_version(current)
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ReleaseError("Choose major, minor, patch, or an explicit version like 2.0.0.")


def is_older(candidate: str, current: str) -> bool:
    """True when ``candidate`` is strictly lower than ``current``. Unparseable is never older."""
    try:
        return parse_version(candidate) < parse_version(current)
    except ReleaseError:
        return False


# --------------------------------------------------------------------------------------
# The archive
# --------------------------------------------------------------------------------------

def shipped_files(repo_root: str) -> List[str]:
    """Relative POSIX paths of every file in a release, sorted. Errors if one is missing."""
    found: List[str] = []
    for name in SHIPPED_FILES:
        if not os.path.isfile(os.path.join(repo_root, name)):
            raise ReleaseError(f"{name} is missing; a release cannot be built without it.")
        found.append(name)
    for package in SHIPPED_PACKAGES:
        base = os.path.join(repo_root, package)
        if not os.path.isdir(base):
            raise ReleaseError(f"{package}/ is missing; a release cannot be built without it.")
        for entry in sorted(os.listdir(base)):
            if entry.endswith(".py") and os.path.isfile(os.path.join(base, entry)):
                found.append(f"{package}/{entry}")
    return sorted(found)


def build_source_archive(repo_root: str, out_path: str) -> Dict[str, Any]:
    """
    Write the release archive and return ``{"sha256", "size", "files"}``.

    Deterministic: entries are sorted, every timestamp and permission is fixed, and nothing
    about the machine or the moment of the build is recorded. Building twice from the same
    tree yields identical bytes.
    """
    files = shipped_files(repo_root)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for relative in files:
            with open(os.path.join(repo_root, *relative.split("/")), "rb") as handle:
                data = handle.read()
            info = zipfile.ZipInfo(relative, date_time=_FIXED_DATE_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = _FILE_MODE
            info.create_system = 3
            archive.writestr(info, data, compresslevel=9)
    payload = buffer.getvalue()
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "wb") as handle:
        handle.write(payload)
    return {"sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload), "files": files}


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def archive_version(archive_path: str) -> str:
    """The VERSION written inside the archive's engine/version.py."""
    try:
        with zipfile.ZipFile(archive_path) as archive:
            text = archive.read("engine/version.py").decode("utf-8")
    except (KeyError, zipfile.BadZipFile, OSError) as exc:
        raise ReleaseError(f"The archive has no readable engine/version.py ({type(exc).__name__}).") from exc
    match = _VERSION_RE.search(text)
    if not match:
        raise ReleaseError("The archive's engine/version.py has no VERSION line.")
    return match.group(1)


# --------------------------------------------------------------------------------------
# The manifest
# --------------------------------------------------------------------------------------

def _require_printable_ascii(field: str, value: str) -> None:
    if not all(" " <= ch <= "~" for ch in value):
        raise ReleaseError(
            f"'{field}' may only contain printable ASCII (the signature is verified in more "
            "than one language, and only ASCII canonicalises identically)."
        )


def rpc_members(names: List[str]) -> List[str]:
    """Names in an RPC zip that it may not contain (empty when every entry is allowed)."""
    return [name for name in names if not _RPC_MEMBER_RE.match(name)]


def check_rpc_bin(path: str) -> None:
    """An RPC zip is a readable zip holding the server and its libraries under bin/, nothing else."""
    label = os.path.basename(path)
    try:
        with zipfile.ZipFile(path) as archive:
            names = [info.filename for info in archive.infolist() if not info.is_dir()]
            if archive.testzip() is not None:
                raise ReleaseError(f"{label} is corrupt.")
    except (zipfile.BadZipFile, OSError) as exc:
        raise ReleaseError(f"{label} is not a readable zip ({type(exc).__name__}).") from exc
    unexpected = rpc_members(names)
    if unexpected:
        raise ReleaseError(f"{label} contains files a release never ships: {', '.join(unexpected[:5])}")
    if not any(name in (f"bin/{exe}" for exe in RPC_EXECUTABLES) for name in names):
        raise ReleaseError(f"{label} has no bin/ggml-rpc-server.")


def make_manifest(version: str, archive_path: str, git_commit: str, built_at: str, notes: str = "",
                  rpc_zips: Optional[Dict[str, str]] = None, llama_build: Optional[str] = None,
                  rpc_proto: Optional[str] = None, server_llama: Optional[str] = None) -> Dict[str, Any]:
    """
    The unsigned manifest for ``archive_path``. Every string is validated as printable ASCII.

    ``rpc_zips`` maps platform -> RPC zip; with it come ``llama_build`` (the upstream tag, e.g.
    "b11838") and ``rpc_proto`` (its RPC protocol, e.g. "8.0.1"). ``server_llama`` is the
    upstream Linux build the coordinator itself runs, installed by release_apply.
    """
    parse_version(version)
    manifest: Dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "version": version,
        "file": ARCHIVE_NAME,
        "sha256": sha256_file(archive_path),
        "size": os.path.getsize(archive_path),
        "git_commit": git_commit,
        "built_at": built_at,
        "notes": notes,
    }
    if rpc_zips or server_llama:
        if not (isinstance(llama_build, str) and _BUILD_RE.match(llama_build)):
            raise ReleaseError("Binaries need the llama.cpp build they come from, like b11838.")
        if proto_major(rpc_proto) is None:
            raise ReleaseError("Binaries need their RPC protocol version, like 8.0.1.")
        manifest["llama_build"] = llama_build
        manifest["rpc_proto"] = rpc_proto
    for platform, path in sorted((rpc_zips or {}).items()):
        if platform not in RPC_PLATFORMS:
            raise ReleaseError(f"Unknown RPC platform {platform!r}; expected one of {', '.join(RPC_PLATFORMS)}.")
        check_rpc_bin(path)
        manifest[rpc_field(platform, "sha256")] = sha256_file(path)
        manifest[rpc_field(platform, "size")] = os.path.getsize(path)
    if server_llama is not None:
        manifest["server_llama_sha256"] = sha256_file(server_llama)
        manifest["server_llama_size"] = os.path.getsize(server_llama)
    for field in ("version", "file", "sha256", "git_commit", "built_at", "notes"):
        _require_printable_ascii(field, str(manifest[field]))
    return manifest


def canonical_bytes(manifest: Dict[str, Any]) -> bytes:
    """The exact bytes the signature covers: everything but ``signature``, sorted, compact."""
    unsigned = {key: value for key, value in manifest.items() if key != "signature"}
    return json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_manifest(manifest: Dict[str, Any], private_key_pem: bytes) -> Dict[str, Any]:
    """Return a copy of ``manifest`` with an Ed25519 ``signature`` (hex) added."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = serialization.load_pem_private_key(private_key_pem, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ReleaseError("The release key is not an Ed25519 key.")
    signed = {k: v for k, v in manifest.items() if k != "signature"}
    signed["signature"] = key.sign(canonical_bytes(signed)).hex()
    return signed


def public_key_hex(private_key_pem: bytes) -> str:
    """The public half of a PEM private key, as the hex a node embeds."""
    from cryptography.hazmat.primitives import serialization

    key = serialization.load_pem_private_key(private_key_pem, password=None)
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    ).hex()


def verify_manifest(manifest: Any, pubkey_hex: Optional[str] = None) -> Tuple[bool, str]:
    """
    Check shape and signature against ``pubkey_hex`` (default: the baked-in RELEASE_PUBKEY,
    looked up at call time). Total: any malformed input is a rejection with a reason, never
    an exception, because callers run this on data from the network.
    """
    pubkey_hex = pubkey_hex if pubkey_hex is not None else RELEASE_PUBKEY
    if not isinstance(manifest, dict):
        return (False, "the release manifest is not a JSON object")
    if manifest.get("schema") != MANIFEST_SCHEMA:
        return (False, f"unsupported manifest schema {manifest.get('schema')!r}")
    version = manifest.get("version")
    if not isinstance(version, str) or not _SEMVER_RE.match(version):
        return (False, "the manifest has no valid version")
    if manifest.get("file") != ARCHIVE_NAME:
        return (False, "the manifest names an unexpected file")
    sha = manifest.get("sha256")
    if not isinstance(sha, str) or not _SHA256_RE.match(sha):
        return (False, "the manifest has no valid sha256")
    size = manifest.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        return (False, "the manifest has no valid size")
    for field in ("git_commit", "built_at", "notes"):
        value = manifest.get(field)
        if not isinstance(value, str) or not all(" " <= ch <= "~" for ch in value):
            return (False, f"the manifest field '{field}' is missing or not printable ASCII")
    binary_pairs = [(rpc_field(p, "sha256"), rpc_field(p, "size")) for p in RPC_PLATFORMS]
    binary_pairs.append(("server_llama_sha256", "server_llama_size"))
    has_binaries = False
    for sha_key, size_key in binary_pairs:
        if sha_key in manifest or size_key in manifest:
            has_binaries = True
            sha, size = manifest.get(sha_key), manifest.get(size_key)
            if not isinstance(sha, str) or not _SHA256_RE.match(sha):
                return (False, f"the manifest has no valid {sha_key}")
            if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
                return (False, f"the manifest has no valid {size_key}")
    if has_binaries or "llama_build" in manifest or "rpc_proto" in manifest:
        build = manifest.get("llama_build")
        if not isinstance(build, str) or not _BUILD_RE.match(build):
            return (False, "the manifest has no valid llama_build")
        if proto_major(manifest.get("rpc_proto")) is None:
            return (False, "the manifest has no valid rpc_proto")
    signature = manifest.get("signature")
    if not isinstance(signature, str) or not signature:
        return (False, "the release manifest is not signed")
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey_hex))
        key.verify(bytes.fromhex(signature), canonical_bytes(manifest))
    except InvalidSignature:
        return (False, "the release signature does not verify")
    except Exception as exc:  # noqa: BLE001 - any structural or crypto failure is a rejection
        return (False, f"the release signature could not be checked ({type(exc).__name__})")
    return (True, "ok")


def verify_archive(manifest: Dict[str, Any], archive_path: str) -> Tuple[bool, str]:
    """The archive on disk is the one the (already verified) manifest describes."""
    try:
        size = os.path.getsize(archive_path)
    except OSError as exc:
        return (False, f"the archive cannot be read ({type(exc).__name__})")
    if size != manifest.get("size"):
        return (False, "the archive size does not match the manifest")
    if sha256_file(archive_path) != manifest.get("sha256"):
        return (False, "the archive hash does not match the manifest")
    try:
        inside = archive_version(archive_path)
    except ReleaseError as exc:
        return (False, str(exc))
    if inside != manifest.get("version"):
        return (False, f"the archive contains version {inside} but the manifest says {manifest.get('version')}")
    return (True, "ok")


def verify_rpc_bin(manifest: Dict[str, Any], platform: str, path: str) -> Tuple[bool, str]:
    """The RPC zip on disk is the one the (already verified) manifest names for ``platform``."""
    return _verify_asset(manifest, rpc_field(platform, "sha256"), rpc_field(platform, "size"), path,
                         f"the release does not sign RPC binaries for {platform}", "RPC archive")


def verify_server_llama(manifest: Dict[str, Any], path: str) -> Tuple[bool, str]:
    """The coordinator's llama.cpp build on disk is the one the (verified) manifest names."""
    return _verify_asset(manifest, "server_llama_sha256", "server_llama_size", path,
                         "the release does not sign a server llama.cpp build", "llama.cpp build")


def _verify_asset(manifest: Dict[str, Any], sha_key: str, size_key: str, path: str,
                  absent: str, what: str) -> Tuple[bool, str]:
    if sha_key not in manifest:
        return (False, absent)
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return (False, f"the {what} cannot be read ({type(exc).__name__})")
    if size != manifest.get(size_key):
        return (False, f"the {what} size does not match the manifest")
    if sha256_file(path) != manifest.get(sha_key):
        return (False, f"the {what} hash does not match the manifest")
    return (True, "ok")


def host_rpc_platform(system: Optional[str] = None, machine: Optional[str] = None) -> Optional[str]:
    """This machine's RPC platform key, or None when DLNE ships no RPC build for it."""
    import platform as _platform

    system = system if system is not None else _platform.system()
    machine = (machine if machine is not None else _platform.machine()).lower()
    if system == "Windows" and machine in ("amd64", "x86_64"):
        return "win-x64"
    if system == "Linux" and machine in ("x86_64", "amd64"):
        return "linux-x64"
    if system == "Darwin" and machine in ("arm64", "aarch64"):
        return "macos-arm64"
    return None


def load_manifest(path: str) -> Optional[Dict[str, Any]]:
    """Read a manifest file; None when absent or unreadable (callers decide what that means)."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
