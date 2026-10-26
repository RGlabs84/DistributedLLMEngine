"""
The model and the llama.cpp build the integration harness runs.

THE MODEL is pinned: Qwen2.5-Coder-0.5B-Instruct, Q4_K_M, 491,400,064 bytes, from the official Qwen
repository at one revision. Its SHA-256 below is the Hugging Face git-LFS object id at that revision (the same
pin as ~/WubarrkCODING/dlne-coding-bakeoff/models.lock.tsv). A file is used only after its size and its SHA-256
have been checked against these figures; a file that does not match is refused, never used.

THE ENGINE is whatever newest llama.cpp build is on the machine (``DLNE_LLAMA_BIN`` names a folder holding
``llama-server``; otherwise the folders in ``LLAMA_CANDIDATES`` are tried in order, newest build first). The build
string is read from ``llama-server --version`` and reported by the runner.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path
from typing import Dict, Optional, Tuple

MODEL = {
    "file": "qwen2.5-coder-0.5b-instruct-q4_k_m.gguf",
    "repo": "Qwen/Qwen2.5-Coder-0.5B-Instruct-GGUF",
    "revision": "ebb2015119c907b064c512bf053e945850b5875f",
    "size": 491400064,
    "sha256": "1d9614638d18024d0fbb36575a15f1302a3adf044df10345688ec4f6e1c4ff32",
}
MODEL["url"] = (f"https://huggingface.co/{MODEL['repo']}/resolve/{MODEL['revision']}/{MODEL['file']}")

# Newest build first. All of these are llama.cpp b11838 today (0.7.0-dev, commit 4231b23dc).
LLAMA_CANDIDATES = (
    "~/.cache/dlne-stage/worker/bin",
    "~/.cache/dlne-live-worker/app/bin",
)


class MissingResource(RuntimeError):
    """The model or the engine is not on this machine; the integration run cannot happen here."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


_verified: Dict[str, str] = {}


def find_model() -> Path:
    """The pinned model file, checked against its pinned size and SHA-256. Raises MissingResource."""
    folder = Path(os.environ.get("DLNE_TEST_DIR") or "~/.cache/dlne-test").expanduser()
    path = folder / MODEL["file"]
    if not path.is_file():
        raise MissingResource(f"{path} is missing (pinned: {MODEL['repo']}@{MODEL['revision'][:10]}, "
                              f"{MODEL['size']} bytes, sha256 {MODEL['sha256']})")
    if path.stat().st_size != MODEL["size"]:
        raise MissingResource(f"{path} is {path.stat().st_size} bytes, not the pinned {MODEL['size']}")
    key = str(path)
    if _verified.get(key) != MODEL["sha256"]:
        found = _sha256(path)
        if found != MODEL["sha256"]:
            raise MissingResource(f"{path} has sha256 {found}, not the pinned {MODEL['sha256']}; not using it")
        _verified[key] = found
    return path


def llama_version(bin_dir: Path) -> str:
    """``llama-server --version`` of a build, e.g. ``version: 0.7.0-dev (build 11838, commit 4231b23dc)``."""
    env = dict(os.environ, LD_LIBRARY_PATH=str(bin_dir), CUDA_VISIBLE_DEVICES="", GGML_VK_VISIBLE_DEVICES="",
               VK_ICD_FILENAMES="/nonexistent")
    done = subprocess.run([str(bin_dir / "llama-server"), "--version"], capture_output=True, text=True, timeout=30,
                          env=env)
    return " ".join((done.stdout + done.stderr).split()).strip()


def _build_number(version: str) -> int:
    match = re.search(r"build (\d+)", version)
    return int(match.group(1)) if match else -1


def find_llama() -> Tuple[Path, str]:
    """(folder holding llama-server, its version string): ``DLNE_LLAMA_BIN`` or the newest build among the candidates."""
    wanted = os.environ.get("DLNE_LLAMA_BIN")
    folders = [Path(wanted).expanduser()] if wanted else [Path(c).expanduser() for c in LLAMA_CANDIDATES]
    found = []
    for folder in folders:
        if (folder / "llama-server").is_file():
            try:
                found.append((_build_number(llama_version(folder)), folder))
            except (OSError, subprocess.SubprocessError):
                continue
    if not found:
        raise MissingResource("no llama-server found (set DLNE_LLAMA_BIN to a folder that holds one)")
    found.sort(key=lambda item: item[0], reverse=True)
    best = found[0][1]
    return best, llama_version(best)
