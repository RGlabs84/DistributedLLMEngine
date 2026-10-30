#!/usr/bin/env python3
"""
Cut and ship a DLNE release. Run from the repository on the maintainer's machine.

    scripts/release.py status                    what is here, and what the coordinator serves
    scripts/release.py bump minor|major|patch|X.Y.Z
    scripts/release.py build  [--notes "..."]    build + sign into release_dist/
    scripts/release.py deploy [--notes "..."]    build, sign, install on the coordinator, verify

The llama.cpp binaries are not built from this repository: scripts/package_llama.py turns one
pinned upstream build into release_dist/assets/llama/ (an RPC zip per platform, the server's
own build, build.json). Every release signs that folder into the manifest (or --binaries DIR),
and deploy refuses to ship a release without it unless told --no-binaries.
Since 2.7 the same folder also holds the CUDA engine (cuda-bin-<platform>.zip for win-x64 and linux-x64, about 600 MB
each; package_llama.py --no-cuda leaves it out): it is signed into the manifest as cuda_<platform>_sha256 / _size and
served at /v1/download/cuda-bin/<platform>. A deploy sends a CUDA zip only when the host does not hold that exact file.
The image engine (stable-diffusion.cpp's sd-server, 2.6.1) is packaged the same way by scripts/package_sd.py into
release_dist/assets/sd/ (or --sd-assets DIR). It is optional: a release is signed with it when that folder holds a
build, and without it everything else is unchanged. --no-binaries leaves it out too.
    scripts/release.py backups                  releases that can be rolled back to
    scripts/release.py rollback [--backup NAME]

One rule runs through all of it: a version number names exactly one archive. `deploy` refuses
to ship the same version twice, or an older one, so "2.0.0" always means the same bytes
everywhere it appears. The release key never leaves your machine and is never printed.
"""

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from engine import release  # noqa: E402

DEFAULT_KEY = os.path.expanduser("~/WubarrkCODING/DLNE_RELEASE_KEYS/dlne_release_key.pem")
DEFAULT_HOST = "archserver-wg"
DEFAULT_ROOT = "~/DLNEv2"
DEFAULT_URL = "https://dlne.rossgilsonlabs.dev"
OUT_DIR = "release_dist"
DEFAULT_BINARIES = os.path.join(REPO_ROOT, OUT_DIR, "assets", "llama")
# The name the server's llama.cpp build is uploaded under (release_apply.py expects it).
SERVER_LLAMA_UPLOAD = "server-llama.tar.gz"
DEFAULT_SD_ASSETS = os.path.join(REPO_ROOT, OUT_DIR, "assets", "sd")
# The name the image engine is uploaded under (release_apply.py expects it).
SERVER_SD_UPLOAD = "server-sd.tar.gz"


def load_sd_assets(directory: str, required: bool = False) -> Optional[Dict]:
    """
    The packaged image engine in ``directory`` (see scripts/package_sd.py): {"sd_build", "server_sd": path}, or None
    when there is no build there and none was asked for by name (``required``: the operator pointed at this folder).
    """
    try:
        with open(os.path.join(directory, "build.json"), "r", encoding="utf-8") as handle:
            info = json.load(handle)
    except (OSError, ValueError) as exc:
        if required:
            raise CliError(f"No packaged image engine in {directory} ({type(exc).__name__}). "
                           "Run scripts/package_sd.py first.") from exc
        return None
    archive = os.path.join(directory, str(info.get("server_sd", "")))
    if not info.get("server_sd") or not os.path.isfile(archive) or not info.get("sd_build"):
        raise CliError(f"{directory} is incomplete; missing: {info.get('server_sd') or SERVER_SD_UPLOAD}.")
    return {"sd_build": info["sd_build"], "server_sd": archive}


def load_binaries(directory: str) -> Dict:
    """
    The packaged llama.cpp binaries in ``directory`` (see scripts/package_llama.py):
    {"llama_build", "rpc_proto", "rpc_zips": {platform: path}, "server_llama": path}, and "cuda_zips":
    {platform: path} when the folder was packaged with the CUDA engine (2.7; build.json then has a "cuda" entry).
    """
    try:
        with open(os.path.join(directory, "build.json"), "r", encoding="utf-8") as handle:
            info = json.load(handle)
    except (OSError, ValueError) as exc:
        raise CliError(f"No packaged llama.cpp build in {directory} ({type(exc).__name__}). "
                       "Run scripts/package_llama.py <build> first.") from exc
    zips = {p: os.path.join(directory, release.rpc_zip_name(p)) for p in release.RPC_PLATFORMS}
    # A CUDA engine is optional: a folder packaged with --no-cuda signs a release exactly as before. When build.json
    # says it was packaged, every platform it names must be there.
    cuda = info.get("cuda") if isinstance(info.get("cuda"), dict) else {}
    cuda_zips = {p: os.path.join(directory, release.cuda_zip_name(p)) for p in (cuda.get("zips") or {})}
    missing = [os.path.basename(z) for z in list(zips.values()) + list(cuda_zips.values()) if not os.path.isfile(z)]
    server = os.path.join(directory, str(info.get("server_llama", "")))
    if not info.get("server_llama") or not os.path.isfile(server):
        missing.append(str(info.get("server_llama") or "the server build"))
    if missing:
        raise CliError(f"{directory} is incomplete; missing: {', '.join(missing)}.")
    found = {"llama_build": info.get("llama_build"), "rpc_proto": info.get("rpc_proto"),
             "rpc_zips": zips, "server_llama": server}
    if cuda_zips:
        found["cuda_zips"] = cuda_zips
    return found
APPLY_SCRIPT = os.path.join(REPO_ROOT, "scripts", "release_apply.py")


def load_app_models(repo: str) -> Optional[List[Dict]]:
    """The CPU pool's model tiers from scripts/app_models.json at ``repo``, or None without one."""
    path = os.path.join(repo, "scripts", "app_models.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            tiers = json.load(handle).get("tiers")
    except (OSError, ValueError, AttributeError) as exc:
        raise CliError(f"{path} is not a readable model list ({type(exc).__name__}).") from exc
    if not isinstance(tiers, list) or not tiers:
        raise CliError(f"{path} lists no tiers.")
    return tiers


def load_coding_models(repo: str) -> Optional[List[Dict]]:
    """The GPU coding pool's ladder from scripts/coding_models.json at ``repo``, or None without one."""
    path = os.path.join(repo, "scripts", "coding_models.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            tiers = json.load(handle).get("tiers")
    except (OSError, ValueError, AttributeError) as exc:
        raise CliError(f"{path} is not a readable model list ({type(exc).__name__}).") from exc
    if not isinstance(tiers, list) or not tiers:
        raise CliError(f"{path} lists no tiers.")
    return tiers


class CliError(Exception):
    """Something the operator needs to fix. The message is the whole story."""


# --------------------------------------------------------------------------------------
# Git and the working tree
# --------------------------------------------------------------------------------------

def _git(repo: str, *args: str) -> str:
    result = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise CliError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def check_tree(repo: str, allow_dirty: bool) -> str:
    """
    The release must be exactly what is committed. Only the shipped paths matter: an
    unrelated scratch file elsewhere in the repository cannot affect the archive.
    Returns the short commit hash the release is built from.
    """
    commit = _git(repo, "rev-parse", "--short=7", "HEAD")
    paths = list(release.SHIPPED_FILES) + list(release.SHIPPED_PACKAGES)
    dirty = _git(repo, "status", "--porcelain", "--", *paths)
    if dirty and not allow_dirty:
        raise CliError(
            "These shipped files have uncommitted changes, so the release would not match "
            "any commit:\n  " + "\n  ".join(dirty.splitlines()[:12]) +
            "\nCommit them first (or pass --allow-dirty for a throwaway build)."
        )
    return commit if not dirty else f"{commit}-dirty"


def default_notes(repo: str) -> str:
    """The subject of the latest commit, reduced to printable ASCII (the signature needs it)."""
    try:
        subject = _git(repo, "log", "-1", "--format=%s")
    except CliError:
        subject = ""
    cleaned = "".join(ch if " " <= ch <= "~" else "?" for ch in subject)
    return cleaned[:200]


# --------------------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------------------

def load_key(path: str) -> bytes:
    if not os.path.isfile(path):
        raise CliError(f"No release key at {path}. Set DLNE_RELEASE_KEY or pass --key.")
    with open(path, "rb") as handle:
        return handle.read()


def build_release(repo: str, out_dir: str, key_pem: bytes, notes: str, allow_dirty: bool = False,
                  expected_pubkey: Optional[str] = None,
                  now: Callable[[], float] = time.time, binaries: Optional[Dict] = None) -> Dict:
    """Build the archive, sign the manifest, and prove the result before returning it."""
    commit = check_tree(repo, allow_dirty)
    pubkey = release.public_key_hex(key_pem)
    expected = expected_pubkey if expected_pubkey is not None else release.RELEASE_PUBKEY
    if pubkey != expected:
        raise CliError(
            "This key does not match the public key built into the engine and the extension. "
            "A release signed with it would be rejected by every node, so it was not built."
        )
    version = release.read_version(repo)
    os.makedirs(out_dir, exist_ok=True)
    archive_path = os.path.join(out_dir, release.ARCHIVE_NAME)
    manifest_path = os.path.join(out_dir, release.MANIFEST_NAME)

    try:
        release.build_source_archive(repo, archive_path)
        built_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now()))
        b = binaries or {}
        manifest = release.make_manifest(version, archive_path, commit, built_at, notes,
                                         rpc_zips=b.get("rpc_zips"), llama_build=b.get("llama_build"),
                                         rpc_proto=b.get("rpc_proto"), server_llama=b.get("server_llama"),
                                         app_models=load_app_models(repo),
                                         coding_models=load_coding_models(repo),
                                         sd_build=b.get("sd_build"), server_sd=b.get("server_sd"),
                                         cuda_zips=b.get("cuda_zips"))
    except release.ReleaseError as exc:
        raise CliError(str(exc)) from exc
    signed = release.sign_manifest(manifest, key_pem)

    ok, reason = release.verify_manifest(signed, pubkey)
    if not ok:
        raise CliError(f"The manifest just built does not verify ({reason}); not publishing it.")
    ok, reason = release.verify_archive(signed, archive_path)
    if not ok:
        raise CliError(f"The archive just built does not match its manifest ({reason}).")
    for platform, path in sorted(((binaries or {}).get("rpc_zips") or {}).items()):
        ok, reason = release.verify_rpc_bin(signed, platform, path)
        if not ok:
            raise CliError(f"The {platform} RPC archive does not match the manifest just built ({reason}).")
    for platform, path in sorted(((binaries or {}).get("cuda_zips") or {}).items()):
        ok, reason = release.verify_cuda_bin(signed, platform, path)
        if not ok:
            raise CliError(f"The {platform} CUDA archive does not match the manifest just built ({reason}).")
    if binaries:
        ok, reason = release.verify_server_llama(signed, binaries["server_llama"])
        if not ok:
            raise CliError(f"The server llama.cpp build does not match the manifest just built ({reason}).")
        if binaries.get("server_sd"):
            ok, reason = release.verify_server_sd(signed, binaries["server_sd"])
            if not ok:
                raise CliError(f"The image engine archive does not match the manifest just built ({reason}).")

    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump(signed, handle, indent=2)
        handle.write("\n")
    return {"version": version, "archive": archive_path, "manifest_path": manifest_path,
            "manifest": signed, "commit": commit, "binaries": binaries}


# --------------------------------------------------------------------------------------
# The coordinator host
# --------------------------------------------------------------------------------------

class Remote:
    """ssh/scp to the coordinator host. Tests substitute a fake with the same three methods."""

    def __init__(self, host: str, root: str):
        self.host, self.root = host, root

    def run(self, command: str, stream: bool = False) -> Tuple[int, str]:
        if stream:
            code = subprocess.call(["ssh", self.host, command])
            return code, ""
        result = subprocess.run(["ssh", "-o", "BatchMode=yes", self.host, command], capture_output=True, text=True)
        return result.returncode, (result.stdout + result.stderr).strip()

    def put(self, local: str, remote_path: str) -> None:
        result = subprocess.run(["scp", "-q", local, f"{self.host}:{remote_path}"], capture_output=True, text=True)
        if result.returncode != 0:
            raise CliError(f"Could not upload {os.path.basename(local)}: {result.stderr.strip()}")


def remote_version(remote) -> str:
    code, out = remote.run(f"grep -m1 '^VERSION' {remote.root}/engine/version.py")
    if code != 0 or '"' not in out:
        return "none"
    return out.split('"')[1]


def deploy(repo: str, remote, key_pem: bytes, notes: str, allow_dirty: bool = False,
           allow_downgrade: bool = False, expected_pubkey: Optional[str] = None,
           public_url: Optional[str] = None, out_dir: Optional[str] = None,
           log: Callable[[str], None] = print, binaries: Optional[Dict] = None) -> Dict:
    built = build_release(repo, out_dir or os.path.join(repo, OUT_DIR), key_pem, notes, allow_dirty, expected_pubkey,
                          binaries=binaries)
    manifest = built["manifest"]
    version = built["version"]
    current = remote_version(remote)
    log(f"Built {version} from {built['commit']}; the coordinator is running {current}.")

    if current == version:
        raise CliError(f"{version} is already deployed. A version names exactly one archive; bump it first.")
    if release.is_older(version, current) and not allow_downgrade:
        raise CliError(f"{version} is OLDER than the running {current}. Pass --allow-downgrade if you mean it.")

    incoming = f"{remote.root}/.releases/incoming"
    code, out = remote.run(f"mkdir -p {incoming}")
    if code != 0:
        raise CliError(f"Could not prepare the coordinator host: {out}")
    remote.put(built["archive"], f"{incoming}/{release.ARCHIVE_NAME}")
    remote.put(built["manifest_path"], f"{incoming}/{release.MANIFEST_NAME}")
    remote.put(APPLY_SCRIPT, f"{incoming}/release_apply.py")

    # Everything the installer will trust, with the hash the manifest signs for it.
    expected = {release.ARCHIVE_NAME: manifest["sha256"]}
    if binaries:
        for platform, path in sorted(binaries["rpc_zips"].items()):
            remote.put(path, f"{incoming}/{release.rpc_zip_name(platform)}")
            expected[release.rpc_zip_name(platform)] = manifest[release.rpc_field(platform, "sha256")]
        for platform, path in sorted((binaries.get("cuda_zips") or {}).items()):
            # About 600 MB each: sent only when the host does not already hold exactly this file (a release that
            # changes the code but not the engine uploads nothing). release_apply then uses the installed copy.
            digest = manifest[release.cuda_field(platform, "sha256")]
            code, out = remote.run(f"sha256sum {remote.root}/{release.cuda_zip_name(platform)}")
            if code == 0 and out.split()[:1] == [digest]:
                log(f"The coordinator host already holds the {platform} CUDA engine; not sending it again.")
                continue
            remote.put(path, f"{incoming}/{release.cuda_zip_name(platform)}")
            expected[release.cuda_zip_name(platform)] = digest
        remote.put(binaries["server_llama"], f"{incoming}/{SERVER_LLAMA_UPLOAD}")
        expected[SERVER_LLAMA_UPLOAD] = manifest["server_llama_sha256"]
        if binaries.get("server_sd"):
            remote.put(binaries["server_sd"], f"{incoming}/{SERVER_SD_UPLOAD}")
            expected[SERVER_SD_UPLOAD] = manifest["server_sd_sha256"]

    for name, digest in expected.items():
        code, out = remote.run(f"sha256sum {incoming}/{name}")
        uploaded = out.split()[0] if code == 0 and out else ""
        if uploaded != digest:
            raise CliError(f"{name} arrived on the coordinator host different from what was sent.")
    log("Upload verified. Installing (this restarts the coordinator).")

    pubkey = expected_pubkey if expected_pubkey is not None else release.RELEASE_PUBKEY
    command = (
        f"cd {remote.root} && {remote.root}/.venv/bin/python3 {incoming}/release_apply.py apply "
        f"--root {remote.root} --archive {incoming}/{release.ARCHIVE_NAME} "
        f"--manifest {incoming}/{release.MANIFEST_NAME} --pubkey {shlex.quote(pubkey)}"
    )
    if binaries:
        command += f" --assets {incoming}"
    code, _ = remote.run(command, stream=True)
    if code != 0:
        raise CliError("The install failed; see above. The coordinator was rolled back if it had been touched.")

    result = {"version": version, "previous": current, "commit": built["commit"]}
    if public_url:
        result["public_check"] = verify_public(public_url, built["manifest"], expected_pubkey)
        log(f"Public check: {result['public_check']}")
    log(f"Release {version} is live.")
    return result


def verify_public(url: str, manifest: Dict, pubkey: Optional[str] = None, fetch: Optional[Callable[[str], bytes]] = None) -> str:
    """Ask the coordinator over the network what it is serving, as a node would."""
    get = fetch or (lambda u: urllib.request.urlopen(u, timeout=15).read())  # noqa: S310
    try:
        served = json.loads(get(f"{url.rstrip('/')}/v1/release"))
    except Exception as exc:  # noqa: BLE001 - reported, not raised: the install itself succeeded
        return f"could not fetch /v1/release ({type(exc).__name__})"
    ok, reason = release.verify_manifest(served, pubkey)
    if not ok:
        return f"served manifest does not verify ({reason})"
    if served.get("sha256") != manifest.get("sha256"):
        return "served manifest is not the one just installed"
    return "the public coordinator serves the new signed release"


# --------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------

def _args_key(args) -> bytes:
    return load_key(args.key or os.environ.get("DLNE_RELEASE_KEY") or DEFAULT_KEY)


def _args_binaries(args) -> Optional[Dict]:
    """The packaged llama.cpp binaries to sign into this release, or None when left out."""
    if args.no_binaries:
        return None
    binaries = load_binaries(args.binaries or DEFAULT_BINARIES)
    named = getattr(args, "sd_assets", None)
    sd = load_sd_assets(named or DEFAULT_SD_ASSETS, required=bool(named))
    return {**binaries, **sd} if sd else binaries


def _remote(args) -> Remote:
    return Remote(args.host or os.environ.get("DLNE_DEPLOY_HOST") or DEFAULT_HOST, args.root or DEFAULT_ROOT)


def cmd_status(args) -> int:
    version = release.read_version(REPO_ROOT)
    print(f"This checkout:   {version}  ({_git(REPO_ROOT, 'rev-parse', '--abbrev-ref', 'HEAD')} @ {_git(REPO_ROOT, 'rev-parse', '--short=7', 'HEAD')})")
    try:
        check_tree(REPO_ROOT, allow_dirty=False)
        print("Working tree:    clean for everything a release ships")
    except CliError:
        print("Working tree:    shipped files have uncommitted changes")
    remote = _remote(args)
    print(f"Coordinator:     {remote_version(remote)} (code on {remote.host})")
    try:
        served = json.loads(urllib.request.urlopen(f"{(args.url or DEFAULT_URL).rstrip('/')}/v1/release", timeout=10).read())  # noqa: S310
        ok, reason = release.verify_manifest(served)
        print(f"Serving:         {served.get('version')}  sha256 {str(served.get('sha256'))[:12]}...  signature {'valid' if ok else 'INVALID: ' + reason}")
    except Exception as exc:  # noqa: BLE001
        print(f"Serving:         no signed release ({type(exc).__name__})")
    return 0


def cmd_bump(args) -> int:
    current = release.read_version(REPO_ROOT)
    try:
        new = release.bump_version(current, args.part)
        release.write_version(REPO_ROOT, new)
    except release.ReleaseError as exc:
        raise CliError(str(exc)) from exc
    print(f"Version {current} -> {new} (engine/version.py). Review and commit it before building.")
    return 0


def cmd_build(args) -> int:
    notes = args.notes if args.notes is not None else default_notes(REPO_ROOT)
    built = build_release(REPO_ROOT, os.path.join(REPO_ROOT, OUT_DIR), _args_key(args), notes, args.allow_dirty,
                          binaries=_args_binaries(args))
    m = built["manifest"]
    print(f"Built {built['version']} from {built['commit']}")
    print(f"  {built['archive']}  ({m['size']} bytes)")
    print(f"  sha256 {m['sha256']}")
    if built["binaries"]:
        print(f"  llama.cpp {m['llama_build']} (RPC protocol {m['rpc_proto']}):")
        for platform in release.RPC_PLATFORMS:
            print(f"    {release.rpc_zip_name(platform)}  sha256 {m[release.rpc_field(platform, 'sha256')]}")
        print(f"    server build  sha256 {m['server_llama_sha256']}")
        for platform in release.CUDA_PLATFORMS:
            if release.cuda_field(platform, "sha256") in m:
                print(f"    CUDA {m['cuda_version']}  {release.cuda_zip_name(platform)}  "
                      f"sha256 {m[release.cuda_field(platform, 'sha256')]}")
        if "sd_build" in m:
            print(f"  image engine {m['sd_build']}  sha256 {m['server_sd_sha256']}")
    print(f"  signed manifest {built['manifest_path']}")
    return 0


def cmd_deploy(args) -> int:
    notes = args.notes if args.notes is not None else default_notes(REPO_ROOT)
    deploy(REPO_ROOT, _remote(args), _args_key(args), notes, allow_dirty=args.allow_dirty,
           allow_downgrade=args.allow_downgrade, public_url=args.url or DEFAULT_URL, binaries=_args_binaries(args))
    return 0


def _remote_apply(args, subcommand: str, extra: str = "") -> int:
    remote = _remote(args)
    code, _ = remote.run(
        f"{remote.root}/.venv/bin/python3 {remote.root}/.releases/incoming/release_apply.py {subcommand} --root {remote.root} {extra}",
        stream=True)
    return code


def cmd_backups(args) -> int:
    return _remote_apply(args, "list")


def cmd_rollback(args) -> int:
    extra = f"--backup {shlex.quote(args.backup)}" if args.backup else ""
    return _remote_apply(args, "rollback", extra)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", help=f"coordinator ssh host (default {DEFAULT_HOST}, or $DLNE_DEPLOY_HOST)")
    parser.add_argument("--root", help=f"install directory on the host (default {DEFAULT_ROOT})")
    parser.add_argument("--url", help=f"public coordinator URL (default {DEFAULT_URL})")
    parser.add_argument("--key", help="release private key (default $DLNE_RELEASE_KEY or the maintainer's key folder)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status").set_defaults(func=cmd_status)
    bump = sub.add_parser("bump")
    bump.add_argument("part", help="major, minor, patch, or an explicit version like 2.0.0")
    bump.set_defaults(func=cmd_bump)
    for name, func in (("build", cmd_build), ("deploy", cmd_deploy)):
        p = sub.add_parser(name)
        p.add_argument("--notes", help="release notes, printable ASCII (default: the last commit subject)")
        p.add_argument("--allow-dirty", action="store_true", help="build even with uncommitted changes (throwaway builds only)")
        p.add_argument("--binaries", help=f"packaged llama.cpp build to sign (default {os.path.relpath(DEFAULT_BINARIES, REPO_ROOT)})")
        p.add_argument("--no-binaries", action="store_true", help="release without llama.cpp binaries (workers cannot install one)")
        p.add_argument("--sd-assets", help=f"packaged image engine to sign (default {os.path.relpath(DEFAULT_SD_ASSETS, REPO_ROOT)} "
                       "when it holds a build; a release without one is built as before)")
        if name == "deploy":
            p.add_argument("--allow-downgrade", action="store_true")
        p.set_defaults(func=func)
    sub.add_parser("backups").set_defaults(func=cmd_backups)
    rb = sub.add_parser("rollback")
    rb.add_argument("--backup", help="backup name from `backups` (default: the newest)")
    rb.set_defaults(func=cmd_rollback)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except CliError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
