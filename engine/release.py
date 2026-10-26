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
RPC_PLATFORMS = ("win-x64", "linux-x64", "macos-arm64", "win-arm64", "linux-arm64", "macos-x64")
RPC_EXECUTABLES = ("ggml-rpc-server", "ggml-rpc-server.exe")
# llama-server, which a CPU pool member runs with its whole model (2.3.0), ships in the same zip.
APP_SERVER_EXECUTABLES = ("llama-server", "llama-server.exe")
# What an RPC zip may hold: bin/<name>, flat, where name is one of the two servers or one of
# their libraries (ggml's; llama-server's own: llama, llama-common, llama-server-impl, mtmd).
_RPC_MEMBER_RE = re.compile(
    r"^bin/(ggml-rpc-server(\.exe)?|llama-server(\.exe)?"
    r"|(lib)?ggml[A-Za-z0-9_.-]*\.(dll|so(\.[0-9.]+)?|dylib)"
    r"|(lib)?(llama|llama-common|llama-server-impl|mtmd)(\.[0-9]+)*\.(dll|dylib|so(\.[0-9]+)*)"
    r"|libomp\.dll|LICENSE)$"
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
    if isinstance(proto, str) and _PROTO_RE.fullmatch(proto):
        return int(proto.split(".")[0])
    return None

# An ALLOWLIST: a file ships because it is named here, never because it happened to be in the
# directory. A stray secrets file, a scratch script or a test fixture cannot leak into a release.
SHIPPED_FILES = ("dlne_server.py", "requirements.txt")
SHIPPED_PACKAGES = ("engine",)
# Binary files a release carries beside the code: directory -> the one extension allowed in it,
# directly inside (no subfolders). engine/static holds the DLNE icons the coordinator serves
# (engine/icons.py); without them it refuses to start, so a release that dropped them would be
# rolled back at deploy. Never extend this to a directory that can hold secrets or scratch files.
SHIPPED_ASSET_DIRS = {"engine/static": ".png"}

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
    if not _SEMVER_RE.fullmatch(version):
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
    if not _SEMVER_RE.fullmatch(str(version)):
        raise ReleaseError(f"'{version}' is not a version like 2.0.0.")
    major, minor, patch = (int(part) for part in version.split("."))
    return (major, minor, patch)


def bump_version(current: str, part: str) -> str:
    """``bump_version("1.1.12", "minor") == "1.2.0"``; ``part`` may also be an explicit version."""
    if _SEMVER_RE.fullmatch(part):
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
    for directory, extension in SHIPPED_ASSET_DIRS.items():
        base = os.path.join(repo_root, *directory.split("/"))
        if not os.path.isdir(base):
            continue   # a tree without the assets still builds; the real repository's test pins that it has them
        for entry in sorted(os.listdir(base)):
            if entry.endswith(extension) and os.path.isfile(os.path.join(base, entry)):
                found.append(f"{directory}/{entry}")
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


# ---------------------------------------------------------------------------------------
# The CPU pool's model tiers (what phone requests are answered with)
# ---------------------------------------------------------------------------------------
#
# Signed as flat text and integer fields (never nested objects or floats), the shape every
# verifier in the field already canonicalises identically: "app_tiers" lists the tier ids
# smallest first, and each id has app_<id>_{name,file,url,sha256,size,min_ram_gb}. Members
# download the file from ``url`` (a Hugging Face URL pinned to one revision) and run it only
# if it has exactly ``size`` bytes and ``sha256``.

_APP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
_APP_FILE_RE = re.compile(r"^[A-Za-z0-9._-]{1,200}\.gguf$")
_APP_URL_RE = re.compile(r"^https://huggingface\.co/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/resolve/[0-9a-f]{40}/[A-Za-z0-9._-]+\.gguf$")


def app_field(tier_id: str, what: str) -> str:
    """``app_field("qwen3-4b", "sha256") == "app_qwen3_4b_sha256"``."""
    return f"app_{tier_id.replace('-', '_')}_{what}"


def _check_app_model(model: Dict[str, Any]) -> Optional[str]:
    tier_id = model.get("id")
    # fullmatch, never match: "$" also matches before a trailing newline, which the extension's
    # verifier (JavaScript) rejects, so Python would sign what the field refuses.
    if not isinstance(tier_id, str) or not _APP_ID_RE.fullmatch(tier_id):
        return f"bad tier id {tier_id!r}"
    if not isinstance(model.get("name"), str) or not model["name"] or not all(" " <= c <= "~" for c in model["name"]):
        return f"tier {tier_id}: name must be printable ASCII"
    if not isinstance(model.get("file"), str) or not _APP_FILE_RE.fullmatch(model["file"]):
        return f"tier {tier_id}: bad file name"
    if not isinstance(model.get("url"), str) or not _APP_URL_RE.fullmatch(model["url"]) or not model["url"].endswith("/" + model["file"]):
        return f"tier {tier_id}: the url must be a Hugging Face file pinned to a revision, ending in the file name"
    if not isinstance(model.get("sha256"), str) or not _SHA256_RE.fullmatch(model["sha256"]):
        return f"tier {tier_id}: bad sha256"
    for key, top in (("size", 1 << 40), ("min_ram_gb", 512)):
        value = model.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or not 0 < value <= top:
            return f"tier {tier_id}: bad {key}"
    return None


def _check_tier_list(tiers: List[Dict[str, Any]]) -> Optional[str]:
    """Tier ids unique, and the list smallest first (the pool's unlock ladder depends on it)."""
    ids = [tier.get("id") for tier in tiers]
    if len(set(ids)) != len(ids):
        return "tier ids must be unique"
    rams = [tier.get("min_ram_gb") for tier in tiers]
    if any(later < earlier for earlier, later in zip(rams, rams[1:])):
        return "tiers must be listed smallest first (min_ram_gb never decreasing)"
    return None


def app_tiers(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The CPU pool's tiers named by ``manifest``, smallest first; [] when it names none."""
    ids = manifest.get("app_tiers")
    if not isinstance(ids, str) or not ids:
        return []
    tiers = []
    for tier_id in ids.split(","):
        tiers.append({"id": tier_id, **{what: manifest.get(app_field(tier_id, what))
                                         for what in ("name", "file", "url", "sha256", "size", "min_ram_gb")}})
    return tiers


# ---------------------------------------------------------------------------------------
# The GPU coding pool's model ladder (what /v1/chat/completions is answered with)
# ---------------------------------------------------------------------------------------
#
# scripts/coding_models.json lists the tiers smallest first; engine/coding_ladder.py is what
# the coordinator reads them through. Signed the same way as the CPU pool's tiers, as flat
# text and integer fields only: "coding_tiers" lists the ids, and each id has
# coding_<id>_{name,min_vram_gb,context,enabled,blocked,parts} plus
# coding_<id>_part<k>_{file,url,sha256,size} for k = 1..parts.
#
# NEVER A BOOLEAN IN THE MANIFEST. The VS Code extension's verifier (engineRelease.ts
# canonicalBytes) throws on any value that is not a string or an integer, so one `true` here
# would make every extension in the field reject the whole release. "enabled" is signed as
# 1 or 0 and read back as a bool.
#
# A tier is one GGUF file or a llama.cpp split set (<stem>-00001-of-0000N.gguf ...): very large
# models are only published in parts, and llama.cpp loads part 1 and finds the rest by name.

_CODING_URL_RE = re.compile(
    r"^https://huggingface\.co/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/resolve/[0-9a-f]{40}/"
    r"(?:[A-Za-z0-9_-][A-Za-z0-9._-]*/)?[A-Za-z0-9._-]+\.gguf$"   # at most one folder, never "." or ".."
)
_SPLIT_PART_RE = re.compile(r"^(?P<stem>.+)-(?P<part>\d{5})-of-(?P<count>\d{5})\.gguf$")
_CODING_PART_KEYS = ("file", "url", "sha256", "size")
_CODING_KEYS = {"id", "name", "min_vram_gb", "context", "enabled", "blocked", "files", *_CODING_PART_KEYS}
CODING_MAX_PARTS = 16
CODING_MAX_CONTEXT = 1 << 21          # 2M tokens: room for the 1M-context models being researched
CODING_MAX_VRAM_GB = 4096


def coding_field(tier_id: str, what: str) -> str:
    """``coding_field("qwen36-a3b", "part1_sha256") == "coding_qwen36_a3b_part1_sha256"``."""
    return f"coding_{tier_id.replace('-', '_')}_{what}"


def _is_int(value: Any, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _printable(value: Any, longest: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= longest and all(" " <= c <= "~" for c in value)


def _coding_part(tier_id: str, part: Any) -> Dict[str, Any]:
    if not isinstance(part, dict):
        raise ReleaseError(f"tier {tier_id}: each file must be an object")
    unknown = sorted(k for k in part if k not in _CODING_PART_KEYS)
    if unknown:
        raise ReleaseError(f"tier {tier_id}: unknown file field {unknown[0]!r}")
    file, url = part.get("file"), part.get("url")
    if not isinstance(file, str) or not _APP_FILE_RE.fullmatch(file):
        raise ReleaseError(f"tier {tier_id}: bad file name")
    if not isinstance(url, str) or not _CODING_URL_RE.fullmatch(url) or not url.endswith("/" + file):
        raise ReleaseError(f"tier {tier_id}: the url must be a Hugging Face file pinned to a revision, ending in the file name")
    if not isinstance(part.get("sha256"), str) or not _SHA256_RE.fullmatch(part["sha256"]):
        raise ReleaseError(f"tier {tier_id}: bad sha256")
    if not _is_int(part.get("size"), 1, 1 << 40):
        raise ReleaseError(f"tier {tier_id}: bad size")
    return {"file": file, "url": url, "sha256": part["sha256"], "size": part["size"]}


def coding_tier(entry: Any) -> Dict[str, Any]:
    """
    One validated coding tier in the shape engine/coding_ladder.py hands out, from an entry of
    scripts/coding_models.json. Raises ReleaseError (naming the tier) when anything is wrong.

    An entry names its model either with file/url/sha256/size (one file) or with "files", a
    list of such objects (a split set, in part order). Keys starting with "_" are comments.
    The result always has both: "files" lists every part, "file"/"url"/"sha256" are part 1
    (the file llama-server is pointed at) and "size" is the total of all parts.
    """
    if not isinstance(entry, dict):
        raise ReleaseError("each tier must be an object")
    tier_id = entry.get("id")
    if not isinstance(tier_id, str) or not _APP_ID_RE.fullmatch(tier_id):
        raise ReleaseError(f"bad tier id {tier_id!r}")
    unknown = sorted(k for k in entry if k not in _CODING_KEYS and not str(k).startswith("_"))
    if unknown:
        raise ReleaseError(f"tier {tier_id}: unknown field {unknown[0]!r}")
    if not _printable(entry.get("name"), 80):
        raise ReleaseError(f"tier {tier_id}: name must be printable ASCII, 1 to 80 characters")
    if not _is_int(entry.get("min_vram_gb"), 1, CODING_MAX_VRAM_GB):
        raise ReleaseError(f"tier {tier_id}: bad min_vram_gb (a whole number of GB)")
    if not _is_int(entry.get("context"), 1024, CODING_MAX_CONTEXT):
        raise ReleaseError(f"tier {tier_id}: bad context")
    if not isinstance(entry.get("enabled"), bool):
        raise ReleaseError(f"tier {tier_id}: enabled must be true or false")
    blocked = entry.get("blocked")
    if blocked is not None and not _printable(blocked, 200):
        raise ReleaseError(f"tier {tier_id}: blocked must be printable ASCII, 1 to 200 characters")
    if entry["enabled"] and blocked:
        raise ReleaseError(f"tier {tier_id}: enabled but still marked blocked ({blocked}); clear one of them")

    has_single = any(key in entry for key in _CODING_PART_KEYS)
    if "files" in entry:
        if has_single:
            raise ReleaseError(f"tier {tier_id}: give either file/url/sha256/size or files, not both")
        raw_parts = entry["files"]
        if not isinstance(raw_parts, list) or not 1 <= len(raw_parts) <= CODING_MAX_PARTS:
            raise ReleaseError(f"tier {tier_id}: files must list 1 to {CODING_MAX_PARTS} parts")
    else:
        raw_parts = [{key: entry.get(key) for key in _CODING_PART_KEYS}]
    parts = [_coding_part(tier_id, part) for part in raw_parts]
    if len(parts) > 1:
        # llama.cpp opens part 1 and derives the other names from it, so they must be exactly
        # <stem>-0000k-of-0000N.gguf, in order, one stem.
        stems = set()
        for number, part in enumerate(parts, start=1):
            match = _SPLIT_PART_RE.fullmatch(part["file"])
            if not match or int(match["part"]) != number or int(match["count"]) != len(parts):
                raise ReleaseError(f"tier {tier_id}: part {number} must be named <stem>-{number:05d}-of-{len(parts):05d}.gguf")
            stems.add(match["stem"])
        if len(stems) != 1:
            raise ReleaseError(f"tier {tier_id}: every part must share one name stem")
    return {
        "id": tier_id,
        "name": entry["name"],
        "file": parts[0]["file"],
        "url": parts[0]["url"],
        "sha256": parts[0]["sha256"],
        "size": sum(part["size"] for part in parts),
        "min_vram_gb": entry["min_vram_gb"],
        "context": entry["context"],
        "enabled": entry["enabled"],
        "blocked": blocked or None,
        "files": parts,
    }


def check_coding_tiers(entries: Any) -> List[Dict[str, Any]]:
    """Every entry through :func:`coding_tier`, then the list rules. Raises ReleaseError."""
    if not isinstance(entries, list) or not entries:
        raise ReleaseError("the coding model list is empty")
    tiers = [coding_tier(entry) for entry in entries]
    ids = [tier["id"] for tier in tiers]
    if len(set(ids)) != len(ids):
        raise ReleaseError("tier ids must be unique")
    files = [part["file"].lower() for tier in tiers for part in tier["files"]]
    if len(set(files)) != len(files):
        raise ReleaseError("two tiers name the same file")
    vram = [tier["min_vram_gb"] for tier in tiers]
    if any(later < earlier for earlier, later in zip(vram, vram[1:])):
        raise ReleaseError("tiers must be listed smallest first (min_vram_gb never decreasing)")
    return tiers


def _flatten_coding_tier(tier: Dict[str, Any]) -> Dict[str, Any]:
    fields: Dict[str, Any] = {
        coding_field(tier["id"], "name"): tier["name"],
        coding_field(tier["id"], "min_vram_gb"): tier["min_vram_gb"],
        coding_field(tier["id"], "context"): tier["context"],
        coding_field(tier["id"], "enabled"): 1 if tier["enabled"] else 0,   # never a bool, see above
        coding_field(tier["id"], "blocked"): tier["blocked"] or "",
        coding_field(tier["id"], "parts"): len(tier["files"]),
    }
    for number, part in enumerate(tier["files"], start=1):
        for what in _CODING_PART_KEYS:
            fields[coding_field(tier["id"], f"part{number}_{what}")] = part[what]
    return fields


def coding_tiers(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    The coding pool's tiers named by ``manifest``, validated, smallest first; [] when it names
    none. Raises ReleaseError when the manifest names tiers that are malformed.
    """
    ids = manifest.get("coding_tiers")
    if not isinstance(ids, str) or not ids:
        return []
    entries = []
    for tier_id in ids.split(","):
        enabled = manifest.get(coding_field(tier_id, "enabled"))
        if not _is_int(enabled, 0, 1):
            raise ReleaseError(f"tier {tier_id}: enabled must be signed as 1 or 0")
        count = manifest.get(coding_field(tier_id, "parts"))
        if not _is_int(count, 1, CODING_MAX_PARTS):
            raise ReleaseError(f"tier {tier_id}: bad parts count")
        blocked = manifest.get(coding_field(tier_id, "blocked"))
        if not isinstance(blocked, str):
            raise ReleaseError(f"tier {tier_id}: blocked must be text")
        entries.append({
            "id": tier_id,
            "name": manifest.get(coding_field(tier_id, "name")),
            "min_vram_gb": manifest.get(coding_field(tier_id, "min_vram_gb")),
            "context": manifest.get(coding_field(tier_id, "context")),
            "enabled": enabled == 1,
            "blocked": blocked or None,
            "files": [{what: manifest.get(coding_field(tier_id, f"part{number}_{what}")) for what in _CODING_PART_KEYS}
                      for number in range(1, count + 1)],
        })
    return check_coding_tiers(entries)


def make_manifest(version: str, archive_path: str, git_commit: str, built_at: str, notes: str = "",
                  rpc_zips: Optional[Dict[str, str]] = None, llama_build: Optional[str] = None,
                  rpc_proto: Optional[str] = None, server_llama: Optional[str] = None,
                  app_models: Optional[List[Dict[str, Any]]] = None,
                  coding_models: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """
    The unsigned manifest for ``archive_path``. Every string is validated as printable ASCII.

    ``rpc_zips`` maps platform -> RPC zip; with it come ``llama_build`` (the upstream tag, e.g.
    "b11838") and ``rpc_proto`` (its RPC protocol, e.g. "8.0.1"). ``server_llama`` is the
    upstream Linux build the coordinator itself runs, installed by release_apply.
    ``app_models`` and ``coding_models`` are the tier lists of scripts/app_models.json and
    scripts/coding_models.json.
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
        if not (isinstance(llama_build, str) and _BUILD_RE.fullmatch(llama_build)):
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
    if app_models:
        ids = []
        for model in app_models:
            problem = _check_app_model(model)
            if problem:
                raise ReleaseError(f"CPU pool model list: {problem}.")
            ids.append(model["id"])
            for what in ("name", "file", "url", "sha256", "size", "min_ram_gb"):
                manifest[app_field(model["id"], what)] = model[what]
        problem = _check_tier_list(app_models)
        if problem:
            raise ReleaseError(f"CPU pool model list: {problem}.")
        manifest["app_tiers"] = ",".join(ids)
    if coding_models:
        try:
            tiers = check_coding_tiers(coding_models)
        except ReleaseError as exc:
            raise ReleaseError(f"Coding model list: {exc}.") from exc
        for tier in tiers:
            manifest.update(_flatten_coding_tier(tier))
        manifest["coding_tiers"] = ",".join(tier["id"] for tier in tiers)
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
    if not isinstance(version, str) or not _SEMVER_RE.fullmatch(version):
        return (False, "the manifest has no valid version")
    if manifest.get("file") != ARCHIVE_NAME:
        return (False, "the manifest names an unexpected file")
    sha = manifest.get("sha256")
    if not isinstance(sha, str) or not _SHA256_RE.fullmatch(sha):
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
            if not isinstance(sha, str) or not _SHA256_RE.fullmatch(sha):
                return (False, f"the manifest has no valid {sha_key}")
            if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
                return (False, f"the manifest has no valid {size_key}")
    if has_binaries or "llama_build" in manifest or "rpc_proto" in manifest:
        build = manifest.get("llama_build")
        if not isinstance(build, str) or not _BUILD_RE.fullmatch(build):
            return (False, "the manifest has no valid llama_build")
        if proto_major(manifest.get("rpc_proto")) is None:
            return (False, "the manifest has no valid rpc_proto")
    if "app_tiers" in manifest:
        tiers = app_tiers(manifest)
        if not tiers:
            return (False, "the manifest's app_tiers is empty")
        for tier in tiers:
            problem = _check_app_model(tier)
            if problem:
                return (False, f"the manifest's CPU pool tiers are invalid: {problem}")
        problem = _check_tier_list(tiers)
        if problem:
            return (False, f"the manifest's CPU pool tiers are invalid: {problem}")
    if "coding_tiers" in manifest:
        try:
            if not coding_tiers(manifest):
                return (False, "the manifest's coding_tiers is empty")
        except ReleaseError as exc:
            return (False, f"the manifest's coding tiers are invalid: {exc}")
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
    arch = "x64" if machine in ("amd64", "x86_64") else "arm64" if machine in ("arm64", "aarch64") else None
    os_key = {"Windows": "win", "Linux": "linux", "Darwin": "macos"}.get(system)
    key = f"{os_key}-{arch}" if os_key and arch else None
    return key if key in RPC_PLATFORMS else None


def load_manifest(path: str) -> Optional[Dict[str, Any]]:
    """Read a manifest file; None when absent or unreadable (callers decide what that means)."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
