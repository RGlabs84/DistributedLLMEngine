"""
Shared fixtures for the DLNE Donate tests. Run them from the repository root:

    python -m pytest desktop/tests -q -p no:cacheprovider

Nothing here touches the network, the real app-data folder or a display: every test gets its own app-data folder (the
app honours DLNE_DONATE_HOME), and the installer and the worker are driven through fakes or throwaway child processes.
"""

import io
import json
import os
import sys
import zipfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pytest

DESKTOP = Path(__file__).resolve().parent.parent
REPO = DESKTOP.parent
for path in (str(DESKTOP), str(REPO)):
    if path not in sys.path:
        sys.path.insert(0, path)

from dlne_donate import bundled  # noqa: E402

release = bundled.load_release()


@pytest.fixture(autouse=True)
def app_home(tmp_path, monkeypatch):
    """The app-data folder of the test: nothing is ever written to the real one."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("DLNE_DONATE_HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    return home


# --------------------------------------------------------------------------------------------------
# A signed release, built with a key made for the test (the real private key is never anywhere near)
# --------------------------------------------------------------------------------------------------

def make_keypair() -> Tuple[bytes, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return pem, release.public_key_hex(pem)


def small_source_zip(version: str = "9.9.9", extra: Optional[Dict[str, bytes]] = None) -> bytes:
    """A source archive with the shape of a release (and a worker.py that only prints), not the real engine."""
    files: Dict[str, bytes] = {
        "dlne_server.py": b"print('fake dlne_server')\n",
        "requirements.txt": b"httpx\n",
        "engine/__init__.py": b"",
        "engine/version.py": f'VERSION = "{version}"\n'.encode(),
        "engine/worker.py": b"X = 1\n",
        "engine/static/icon.png": b"\x89PNG fake",
    }
    files.update(extra or {})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            archive.writestr(name, data)
    return buffer.getvalue()


def rpc_zip(extra: Optional[Dict[str, bytes]] = None) -> bytes:
    files = {"bin/ggml-rpc-server": b"#!/bin/sh\nexit 0\n", "bin/llama-server": b"#!/bin/sh\nexit 0\n", "bin/libggml-base.so": b"lib"}
    files.update(extra or {})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            archive.writestr(name, data)
    return buffer.getvalue()


class Release:
    """One signed release held in memory, and the fake coordinator that serves it."""

    def __init__(self, tmp: Path, version: str = "9.9.9", platform: str = "linux-x64", source: Optional[bytes] = None,
                 rpc: Optional[bytes] = None, sign: bool = True, key: Optional[Tuple[bytes, str]] = None):
        self.pem, self.public_key = key or make_keypair()
        self.platform = platform
        self.source = source if source is not None else small_source_zip(version)
        self.rpc = rpc if rpc is not None else rpc_zip()
        src_path, rpc_path = tmp / "dlne-source.zip", tmp / "rpc.zip"
        src_path.write_bytes(self.source)
        rpc_path.write_bytes(self.rpc)
        unsigned = {
            "schema": 1, "version": version, "file": "dlne-source.zip", "sha256": release.sha256_file(str(src_path)),
            "size": len(self.source), "git_commit": "abc1234", "built_at": "2026-10-26T00:00:00Z", "notes": "test",
            "llama_build": "b11838", "rpc_proto": "8.0.1",
            release.rpc_field(platform, "sha256"): release.sha256_file(str(rpc_path)),
            release.rpc_field(platform, "size"): len(self.rpc),
        }
        self.manifest = release.sign_manifest(unsigned, self.pem) if sign else {**unsigned, "signature": "00" * 64}
        self.requests = []

    def http(self) -> "FakeHttp":
        return FakeHttp(self)


class FakeHttp:
    """The two methods installer.Http has, answering from a Release, and remembering what was asked."""

    def __init__(self, served: Release):
        self.served = served
        self.requests = []

    def get_json(self, url: str, timeout: float = 15.0):
        self.requests.append(url)
        if url.endswith("/v1/release"):
            return 200, json.loads(json.dumps(self.served.manifest))
        return 404, None

    def download(self, url: str, destination: str, expected_size: int, progress=None, timeout: float = 60.0) -> None:
        self.requests.append(url)
        if url.endswith("/v1/download/source"):
            data = self.served.source
        elif "/v1/download/rpc-bin/" in url:
            data = self.served.rpc
        else:
            raise AssertionError(url)
        with open(destination, "wb") as handle:
            handle.write(data)
        if progress:
            progress(len(data), len(data))

    def downloads(self):
        return [r for r in self.requests if "/v1/download/" in r]


@pytest.fixture
def served(tmp_path) -> Release:
    return Release(tmp_path)
