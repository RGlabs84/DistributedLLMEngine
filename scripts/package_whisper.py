#!/usr/bin/env python3
"""
Turn the pinned whisper.cpp source into the voice engine a DLNE release ships (2.8.0).

    scripts/package_whisper.py                 build the pinned tag with Vulkan, then package it
    scripts/package_whisper.py --from DIR      package a build already made (DIR holds whisper-server and LICENSE)

    Heavy (a compile): run it as   ~/valheim-testbed/heavy.sh 8G whisper-build timeout 3600 scripts/package_whisper.py

Why a build and not upstream's binary: whisper.cpp's releases publish ``whisper-bin-ubuntu-x64.tar.gz``, but it is a
CPU-only build (the ggml CPU variants, no Vulkan backend), and the media card needs Vulkan. So the pinned tag is built
here. It is pinned by git COMMIT, not by tag name (a tag can be moved); the Vulkan and SPIR-V headers it needs (the build machine
need not have them) are pinned the same way. Nothing else is fetched.

Output, in release_dist/assets/whisper/ (what `scripts/release.py build|deploy` signs when it is there):

    server-whisper.tar.gz   whisper-server and LICENSE, flat. whisper-server is one executable: ggml, whisper and the
                            Vulkan backend are linked in statically, as are libstdc++ and libgcc, and the RUNPATH is
                            $ORIGIN. It needs only glibc and the system's libvulkan.so.1. Deterministic tar: the same
                            binary always gives the same archive bytes. release_apply.py unpacks it into
                            <install>/bin-whisper/, a folder of its own.
    build.json              {"whisper_build", "server_whisper", "binary_sha256", "source_commit", ...}

The binary's bytes depend on the compiler, so what is signed into the manifest is the archive's SHA-256 (and size) as
packaged, not a hash fixed here: build once, test on TheWall, sign that archive.
"""

import argparse
import gzip
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from typing import Dict, List, Optional, Tuple

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from engine import release  # noqa: E402

UPSTREAM_URL = "https://github.com/ggml-org/whisper.cpp"
WHISPER_BUILD = "v1.9.5"
WHISPER_COMMIT = "d1be6fde11ac6e0407606b4e42fe72d34add8037"          # what the annotated tag v1.9.5 points at
VULKAN_HEADERS_URL = "https://github.com/KhronosGroup/Vulkan-Headers"
VULKAN_HEADERS_TAG = "v1.4.363"
VULKAN_HEADERS_COMMIT = "6802bb4733b63ed5efd3adb308a6c885ef180ea1"
SPIRV_HEADERS_URL = "https://github.com/KhronosGroup/SPIRV-Headers"
SPIRV_HEADERS_TAG = "vulkan-sdk-1.4.363.0"
SPIRV_HEADERS_COMMIT = "496543121ce6419f23d6fa5d7194ba66c36212d2"
OUT_DIR = os.path.join(REPO_ROOT, "release_dist", "assets", "whisper")
UPLOAD_NAME = "server-whisper.tar.gz"
BINARY = "whisper-server"
_MEMBERS = (BINARY, "LICENSE")
_MAX_MEMBER_BYTES = 512 * 1024 * 1024


class PackageError(Exception):
    """Something the operator needs to fix. The message is the whole story."""


def _run(command: List[str], cwd: Optional[str] = None) -> str:
    done = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    if done.returncode != 0:
        tail = "\n".join((done.stdout + done.stderr).strip().splitlines()[-15:])
        raise PackageError(f"{' '.join(command[:3])} failed ({done.returncode}):\n{tail}")
    return done.stdout


def _clone_pinned(url: str, tag: str, commit: str, dest: str) -> None:
    """A shallow clone of ``tag`` that is refused unless it is exactly ``commit``."""
    _run(["git", "clone", "--quiet", "--depth", "1", "--branch", tag, url, dest])
    head = _run(["git", "rev-parse", "HEAD"], cwd=dest).strip()
    if head != commit:
        shutil.rmtree(dest, ignore_errors=True)
        raise PackageError(f"{url} {tag} is commit {head}, not the pinned {commit}; refusing to build it.")


def cmake_configure_args(source: str, build: str, headers: str, prefix: str = "") -> List[str]:
    """The cmake configure line. Static ggml/whisper, Vulkan on, no host-specific tuning, paths mapped out of the binary."""
    flags = (f"-ffile-prefix-map={source}=. -ffile-prefix-map={build}=. -ffile-prefix-map={headers}=. "
             f"-isystem {os.path.join(prefix, 'include')}")     # ggml-vulkan includes spirv/unified1/spirv.hpp
    return ["cmake", "-S", source, "-B", build, "-G", "Ninja",
            "-DCMAKE_BUILD_TYPE=Release",
            "-DBUILD_SHARED_LIBS=OFF",
            "-DGGML_VULKAN=ON",
            "-DGGML_OPENMP=OFF",                  # ggml's own thread pool: no libgomp to find at run time
            "-DGGML_NATIVE=OFF",                  # the binary must run on a CPU other than the builder's
            "-DWHISPER_BUILD_EXAMPLES=ON", "-DWHISPER_BUILD_TESTS=OFF", "-DWHISPER_SDL2=OFF", "-DWHISPER_CURL=OFF",
            f"-DVulkan_INCLUDE_DIR={os.path.join(headers, 'include')}", f"-DCMAKE_PREFIX_PATH={prefix}",
            f"-DCMAKE_C_FLAGS={flags}", f"-DCMAKE_CXX_FLAGS={flags}",
            "-DCMAKE_EXE_LINKER_FLAGS=-static-libstdc++ -static-libgcc",
            "-DCMAKE_BUILD_RPATH_USE_ORIGIN=ON", "-DCMAKE_INSTALL_RPATH=$ORIGIN", "-DCMAKE_BUILD_WITH_INSTALL_RPATH=ON"]


def build_from_source(work: str) -> str:
    """Clone both pins into ``work``, build whisper-server, and return the folder holding it and LICENSE."""
    source, headers, spirv, build = (os.path.join(work, name) for name in ("whisper.cpp", "Vulkan-Headers", "SPIRV-Headers", "build"))
    _clone_pinned(UPSTREAM_URL, WHISPER_BUILD, WHISPER_COMMIT, source)
    _clone_pinned(VULKAN_HEADERS_URL, VULKAN_HEADERS_TAG, VULKAN_HEADERS_COMMIT, headers)
    _clone_pinned(SPIRV_HEADERS_URL, SPIRV_HEADERS_TAG, SPIRV_HEADERS_COMMIT, spirv)
    prefix = os.path.join(work, "prefix")                # SPIRV-Headers is headers and cmake files: installed, not built
    _run(["cmake", "-S", spirv, "-B", os.path.join(work, "spirv-build"), f"-DCMAKE_INSTALL_PREFIX={prefix}"])
    _run(["cmake", "--install", os.path.join(work, "spirv-build")])
    _run(cmake_configure_args(source, build, headers, prefix))
    _run(["cmake", "--build", build, "--target", BINARY, "--parallel", str(min(os.cpu_count() or 2, 8))])
    stage = os.path.join(work, "stage")
    os.makedirs(stage)
    found = os.path.join(build, "bin", BINARY)
    if not os.path.isfile(found):
        raise PackageError(f"The build finished but {found} is not there.")
    shutil.copy2(found, os.path.join(stage, BINARY))
    shutil.copy2(os.path.join(source, "LICENSE"), os.path.join(stage, "LICENSE"))
    return stage


def read_members(directory: str) -> List[Tuple[str, bytes]]:
    """``(name, bytes)`` for whisper-server and LICENSE in ``directory``, sorted. The server must be an ELF executable."""
    found: Dict[str, bytes] = {}
    for name in _MEMBERS:
        path = os.path.join(directory, name)
        if name == BINARY and not os.path.isfile(path):
            raise PackageError(f"{directory} has no {BINARY}.")
        if not os.path.isfile(path):
            continue
        if os.path.getsize(path) > _MAX_MEMBER_BYTES:
            raise PackageError(f"{name} is unreasonably large.")
        with open(path, "rb") as handle:
            found[name] = handle.read()
    if found[BINARY][:4] != b"\x7fELF":
        raise PackageError(f"{BINARY} is not a Linux executable.")
    return sorted(found.items())


def write_tarball(members: List[Tuple[str, bytes]], out_path: str) -> None:
    """A deterministic tar.gz: sorted entries, owner root, time zero, the server 0755 and the rest 0644."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.uid, info.gid, info.uname, info.gname = len(data), 0, 0, 0, "", ""
            info.mode = 0o755 if name == BINARY else 0o644
            tar.addfile(info, io.BytesIO(data))
    with open(out_path, "wb") as handle:
        with gzip.GzipFile(filename="", mode="wb", fileobj=handle, compresslevel=9, mtime=0) as zipped:
            zipped.write(raw.getvalue())


def package(directory: str, out_dir: str = OUT_DIR, whisper_build: str = WHISPER_BUILD) -> Dict:
    """Write server-whisper.tar.gz and build.json into ``out_dir`` from the build in ``directory``."""
    members = read_members(directory)
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir)
    write_tarball(members, os.path.join(out_dir, UPLOAD_NAME))
    info = {"whisper_build": whisper_build, "server_whisper": UPLOAD_NAME,
            "binary_sha256": release.sha256_file(os.path.join(directory, BINARY)),
            "source_commit": WHISPER_COMMIT, "vulkan_headers": f"{VULKAN_HEADERS_TAG} {VULKAN_HEADERS_COMMIT}",
            "spirv_headers": f"{SPIRV_HEADERS_TAG} {SPIRV_HEADERS_COMMIT}",
            "files": [name for name, _data in members]}
    with open(os.path.join(out_dir, "build.json"), "w", encoding="utf-8") as handle:
        json.dump(info, handle, indent=2)
        handle.write("\n")
    return info


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="source", help="a finished build: a folder with whisper-server and LICENSE")
    parser.add_argument("--out", default=OUT_DIR)
    args = parser.parse_args(argv)
    try:
        if args.source:
            info = package(args.source, args.out)
        else:
            with tempfile.TemporaryDirectory() as work:
                info = package(build_from_source(work), args.out)
    except PackageError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Packaged whisper.cpp {info['whisper_build']} into {args.out}")
    for name in info["files"]:
        print(f"  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
