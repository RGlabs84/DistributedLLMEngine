#!/usr/bin/env python3
"""
Turn one pinned upstream llama.cpp build into the binaries a DLNE release ships.

    scripts/package_llama.py b11838            download, verify, package
    scripts/package_llama.py b11838 --from DIR use archives already downloaded into DIR

Output, in release_dist/assets/llama/ (what `scripts/release.py build|deploy` signs):

    rpc-bin-win-x64.zip, rpc-bin-linux-x64.zip, rpc-bin-macos-arm64.zip
        ggml-rpc-server and the ggml libraries it loads, under bin/. Deterministic: the
        same upstream archives always give the same bytes.
    llama-<build>-bin-ubuntu-vulkan-x64.tar.gz
        the upstream build the coordinator itself runs, unchanged.
    build.json
        {"llama_build": ..., "rpc_proto": ...}. The protocol is read from the Linux
        ggml-rpc-server's own startup banner, never typed in.

Every upstream archive is checked against the SHA-256 digest GitHub publishes for it before
anything is read from it. The upstream tag is pinned on purpose: a coordinator and its workers
must speak the same RPC protocol major, so "whatever is latest" is never acceptable.
"""

import argparse
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from typing import Dict, List, Tuple

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from engine import release  # noqa: E402

UPSTREAM = "ggml-org/llama.cpp"
OUT_DIR = os.path.join(REPO_ROOT, "release_dist", "assets", "llama")

# platform -> the upstream archive its RPC build is taken from
SOURCES = {
    "win-x64": "llama-{b}-bin-win-vulkan-x64.zip",
    "linux-x64": "llama-{b}-bin-ubuntu-vulkan-x64.tar.gz",
    "macos-arm64": "llama-{b}-bin-macos-arm64.tar.gz",
}
SERVER_SOURCE = "llama-{b}-bin-ubuntu-vulkan-x64.tar.gz"

_FIXED_DATE_TIME = (1980, 1, 1, 0, 0, 0)
_BANNER_RE = re.compile(r"Starting RPC server v(\d+\.\d+\.\d+)")


class PackageError(Exception):
    """Something the operator needs to fix. The message is the whole story."""


def fetch(build: str, dest: str) -> Dict[str, str]:
    """Download the needed upstream archives with gh and check each against GitHub's digest."""
    names = sorted({s.format(b=build) for s in SOURCES.values()} | {SERVER_SOURCE.format(b=build)})
    query = subprocess.run(
        ["gh", "api", f"repos/{UPSTREAM}/releases/tags/{build}", "--jq",
         '.assets[] | .name + " " + (.digest // "")'],
        capture_output=True, text=True)
    if query.returncode != 0:
        raise PackageError(f"Could not read release {build} from GitHub: {query.stderr.strip()}")
    digests = dict(line.split(" ", 1) for line in query.stdout.strip().splitlines() if " " in line)
    paths = {}
    for name in names:
        digest = digests.get(name, "")
        if not digest.startswith("sha256:"):
            raise PackageError(f"GitHub publishes no SHA-256 digest for {name}; refusing to use it.")
        got = subprocess.run(["gh", "release", "download", build, "-R", UPSTREAM, "-p", name, "-D", dest, "--clobber"],
                             capture_output=True, text=True)
        if got.returncode != 0:
            raise PackageError(f"Download of {name} failed: {got.stderr.strip()}")
        path = os.path.join(dest, name)
        if release.sha256_file(path) != digest.split(":", 1)[1]:
            raise PackageError(f"{name} does not match the digest GitHub publishes for it.")
        paths[name] = path
    return paths


def _wanted(name: str) -> bool:
    return bool(release._RPC_MEMBER_RE.match(f"bin/{name}"))


def _read_members(archive_path: str) -> List[Tuple[str, bytes]]:
    """(basename, bytes) for every file an RPC zip should carry; symlinks are resolved to copies."""
    found: Dict[str, bytes] = {}
    if archive_path.endswith(".zip"):
        with zipfile.ZipFile(archive_path) as z:
            for info in z.infolist():
                base = os.path.basename(info.filename)
                if not info.is_dir() and _wanted(base):
                    found[base] = z.read(info)
    else:
        with tarfile.open(archive_path, "r:gz") as t:
            members = {m.name: m for m in t.getmembers()}
            for m in t.getmembers():
                base = os.path.basename(m.name)
                if not _wanted(base):
                    continue
                target = m
                hops = 0
                while target.issym() and hops < 8:   # follow libggml.so.0 -> libggml.so.0.27.0
                    target = members.get(os.path.normpath(os.path.join(os.path.dirname(target.name), target.linkname)))
                    hops += 1
                    if target is None:
                        break
                if target is not None and target.isfile():
                    found[base] = t.extractfile(target).read()
    if not any(name in found for name in release.RPC_EXECUTABLES):
        raise PackageError(f"{os.path.basename(archive_path)} has no ggml-rpc-server.")
    return sorted(found.items())


def write_rpc_zip(members: List[Tuple[str, bytes]], out_path: str) -> None:
    """A deterministic zip: sorted entries, fixed dates, executables 0755 and the rest 0644."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name, data in members:
            info = zipfile.ZipInfo(f"bin/{name}", date_time=_FIXED_DATE_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (0o100755 if name in release.RPC_EXECUTABLES else 0o100644) << 16
            z.writestr(info, data, compresslevel=9)
    with open(out_path, "wb") as handle:
        handle.write(buffer.getvalue())
    release.check_rpc_bin(out_path)


def read_proto(linux_tarball: str) -> str:
    """Start the Linux ggml-rpc-server on a free loopback port (CPU only) and read its banner."""
    if not sys.platform.startswith("linux"):
        raise PackageError("Reading the RPC protocol needs a Linux machine; pass --rpc-proto.")
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(linux_tarball, "r:gz") as t:
            t.extractall(tmp, filter="data")
        exe = next((os.path.join(root, f) for root, _d, files in os.walk(tmp) for f in files if f == "ggml-rpc-server"), None)
        if exe is None:
            raise PackageError("The Linux build has no ggml-rpc-server.")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        log = os.path.join(tmp, "banner.log")
        with open(log, "w") as out:
            proc = subprocess.Popen([exe, "-H", "127.0.0.1", "-p", str(port), "-d", "CPU"], stdout=out, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                match = _BANNER_RE.search(open(log, errors="replace").read())
                if match:
                    return match.group(1)
                if proc.poll() is not None:
                    break
                time.sleep(0.1)
        finally:
            proc.kill()
            proc.wait()
    raise PackageError("ggml-rpc-server did not announce its protocol version.")


def package(build: str, sources: Dict[str, str], out_dir: str, rpc_proto: str = "") -> Dict:
    if not re.match(r"^b\d+$", build):
        raise PackageError(f"{build!r} is not a llama.cpp build tag like b11838.")
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir)
    for platform, pattern in SOURCES.items():
        write_rpc_zip(_read_members(sources[pattern.format(b=build)]), os.path.join(out_dir, release.rpc_zip_name(platform)))
    server_name = SERVER_SOURCE.format(b=build)
    shutil.copyfile(sources[server_name], os.path.join(out_dir, server_name))
    proto = rpc_proto or read_proto(sources[SOURCES["linux-x64"].format(b=build)])
    if release.proto_major(proto) is None:
        raise PackageError(f"{proto!r} is not an RPC protocol version like 8.0.1.")
    info = {"llama_build": build, "rpc_proto": proto, "server_llama": server_name}
    with open(os.path.join(out_dir, "build.json"), "w") as handle:
        json.dump(info, handle, indent=2)
        handle.write("\n")
    return info


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("build", help="the pinned upstream tag, e.g. b11838")
    parser.add_argument("--from", dest="source_dir", help="directory already holding the upstream archives (digests still checked)")
    parser.add_argument("--rpc-proto", default="", help="only when not on Linux: the protocol, e.g. 8.0.1")
    parser.add_argument("--out", default=OUT_DIR)
    args = parser.parse_args(argv)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            dest = args.source_dir or tmp
            sources = fetch(args.build, dest) if not args.source_dir else _verify_existing(args.build, dest)
            info = package(args.build, sources, args.out, args.rpc_proto)
    except PackageError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Packaged llama.cpp {info['llama_build']} (RPC protocol {info['rpc_proto']}) into {args.out}")
    for name in sorted(os.listdir(args.out)):
        print(f"  {name}")
    return 0


def _verify_existing(build: str, directory: str) -> Dict[str, str]:
    """Like fetch(), for archives already on disk: each must still match GitHub's digest."""
    query = subprocess.run(["gh", "api", f"repos/{UPSTREAM}/releases/tags/{build}", "--jq",
                            '.assets[] | .name + " " + (.digest // "")'], capture_output=True, text=True)
    if query.returncode != 0:
        raise PackageError(f"Could not read release {build} from GitHub: {query.stderr.strip()}")
    digests = dict(line.split(" ", 1) for line in query.stdout.strip().splitlines() if " " in line)
    paths = {}
    for name in sorted({s.format(b=build) for s in SOURCES.values()} | {SERVER_SOURCE.format(b=build)}):
        path = os.path.join(directory, name)
        digest = digests.get(name, "")
        if not os.path.isfile(path) or not digest.startswith("sha256:") or release.sha256_file(path) != digest.split(":", 1)[1]:
            raise PackageError(f"{name} is missing from {directory} or does not match GitHub's digest.")
        paths[name] = path
    return paths


if __name__ == "__main__":
    sys.exit(main())
