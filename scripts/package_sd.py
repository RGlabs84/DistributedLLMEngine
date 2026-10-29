#!/usr/bin/env python3
"""
Turn the pinned stable-diffusion.cpp release into the image engine a DLNE release ships (2.6.1).

    scripts/package_sd.py                  download the pinned release asset, verify, package
    scripts/package_sd.py --from ZIP       use the asset already downloaded (its SHA-256 is still checked)

Output, in release_dist/assets/sd/ (what `scripts/release.py build|deploy` signs when it is there):

    server-sd.tar.gz   sd-server and the libraries it loads (libggml*, libstable-diffusion, libwebp...), flat, no sd-cli.
                       Deterministic: the same upstream asset always gives the same bytes. release_apply.py unpacks it
                       into <install>/bin-sd/, a folder of its own: these libraries have llama.cpp's names and must
                       never be mixed with llama.cpp's (sd-server is started with LD_LIBRARY_PATH set to this folder).
    build.json         {"sd_build", "server_sd", ...}

The asset is pinned by its SHA-256, not by what GitHub says about it today: the digest below was read from the build
that was measured on TheWall (docs/image-generation.md). Moving to a newer build is a deliberate act: change the three
constants, run the image tests, and measure.
"""

import argparse
import gzip
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from typing import Dict, List, Tuple

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from engine import release  # noqa: E402

UPSTREAM = "leejet/stable-diffusion.cpp"
SD_BUILD = "master-1017-be48649"
ASSET = "sd-master-be48649-bin-Linux-Ubuntu-24.04-x86_64-vulkan.zip"
ASSET_SHA256 = "794c913d077aaf72025e772b2acfb6a30f49156e92d072461ae15ce958d95c14"
OUT_DIR = os.path.join(REPO_ROOT, "release_dist", "assets", "sd")
UPLOAD_NAME = "server-sd.tar.gz"

# What the engine needs from the asset: the server, and shared libraries (flat names, whatever folder they sat in).
# sd-cli and everything else is left out.
_WANTED = re.compile(r"^(sd-server|lib[A-Za-z0-9_+-]+\.so(\.[0-9]+)*|LICENSE[A-Za-z0-9._-]*)$")
_EXECUTABLE = re.compile(r"^(sd-server|lib.*\.so.*)$")
_MAX_MEMBER_BYTES = 512 * 1024 * 1024


class PackageError(Exception):
    """Something the operator needs to fix. The message is the whole story."""


def fetch(dest: str) -> str:
    """Download the pinned asset with gh; it is checked against the pinned SHA-256 by :func:`package`."""
    got = subprocess.run(["gh", "release", "download", SD_BUILD, "-R", UPSTREAM, "-p", ASSET, "-D", dest, "--clobber"],
                         capture_output=True, text=True)
    if got.returncode != 0:
        raise PackageError(f"Download of {ASSET} failed: {got.stderr.strip()}")
    return os.path.join(dest, ASSET)


def read_members(zip_path: str) -> List[Tuple[str, bytes]]:
    """``(name, bytes)`` for every file the engine needs, sorted, names flat; a second file of one name is refused."""
    found: Dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(zip_path) as archive:
            for info in archive.infolist():
                name = os.path.basename(info.filename.rstrip("/"))
                if info.is_dir() or not _WANTED.match(name):
                    continue
                if info.file_size > _MAX_MEMBER_BYTES:
                    raise PackageError(f"{info.filename} in the asset is unreasonably large.")
                if name in found:
                    raise PackageError(f"The asset holds {name} twice.")
                found[name] = archive.read(info)
    except zipfile.BadZipFile as exc:
        raise PackageError(f"{zip_path} is not a valid zip file.") from exc
    if "sd-server" not in found:
        raise PackageError("The asset has no sd-server.")
    if not any(name.startswith("libstable-diffusion") or name.startswith("libggml") for name in found):
        raise PackageError("The asset has none of the libraries sd-server loads.")
    return sorted(found.items())


def write_tarball(members: List[Tuple[str, bytes]], out_path: str) -> None:
    """A deterministic tar.gz: sorted entries, owner root, time zero, executables 0755 and the rest 0644."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.uid, info.gid, info.uname, info.gname = len(data), 0, 0, 0, "", ""
            info.mode = 0o755 if _EXECUTABLE.match(name) else 0o644
            tar.addfile(info, io.BytesIO(data))
    with open(out_path, "wb") as handle:
        with gzip.GzipFile(filename="", mode="wb", fileobj=handle, compresslevel=9, mtime=0) as zipped:
            zipped.write(raw.getvalue())


def package(zip_path: str, out_dir: str = OUT_DIR, sd_build: str = SD_BUILD, expected_sha256: str = ASSET_SHA256) -> Dict:
    """Verify ``zip_path`` against its pinned digest and write server-sd.tar.gz and build.json into ``out_dir``."""
    if not os.path.isfile(zip_path) or release.sha256_file(zip_path) != expected_sha256:
        raise PackageError(f"{zip_path} is not the pinned asset {ASSET} (SHA-256 {expected_sha256}); refusing to ship it.")
    members = read_members(zip_path)
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir)
    write_tarball(members, os.path.join(out_dir, UPLOAD_NAME))
    info = {"sd_build": sd_build, "server_sd": UPLOAD_NAME, "upstream_asset": ASSET, "upstream_sha256": expected_sha256,
            "files": [name for name, _data in members]}
    with open(os.path.join(out_dir, "build.json"), "w", encoding="utf-8") as handle:
        json.dump(info, handle, indent=2)
        handle.write("\n")
    return info


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="source", help="the asset already downloaded (its SHA-256 is still checked)")
    parser.add_argument("--out", default=OUT_DIR)
    args = parser.parse_args(argv)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            info = package(args.source or fetch(tmp), args.out)
    except PackageError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Packaged stable-diffusion.cpp {info['sd_build']} into {args.out}")
    for name in info["files"]:
        print(f"  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
