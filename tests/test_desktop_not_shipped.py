"""
The DLNE Donate tray app lives in desktop/. It is a program people download and run, not part of the engine the
coordinator serves at /v1/download/source, so no release may carry it (and a worker must never download a tray app,
its icons or its PyInstaller specs).
"""

import hashlib
import os
import zipfile

from engine import release

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _tree(root, with_desktop):
    files = {
        "dlne_server.py": "# server\n",
        "requirements.txt": "httpx\n",
        "engine/__init__.py": "",
        "engine/version.py": 'VERSION = "1.2.3"\n',
        "engine/worker.py": "X = 1\n",
    }
    if with_desktop:
        files.update({
            "desktop/dlne_donate/__init__.py": "# app\n",
            "desktop/dlne_donate/app.py": "# app\n",
            "desktop/packaging/dlne-donate.spec": "# spec\n",
            "desktop/README.md": "# app\n",
            "desktop/requirements.txt": "pystray\n",
            "desktop/tests/test_x.py": "def test_x(): pass\n",
        })
    for name, text in files.items():
        path = os.path.join(root, *name.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
    return str(root)


def test_a_tree_with_desktop_ships_exactly_what_it_ships_without_it(tmp_path):
    with_desktop = _tree(tmp_path / "with", True)
    without = _tree(tmp_path / "without", False)
    assert release.shipped_files(with_desktop) == release.shipped_files(without)
    assert not any(name.startswith("desktop/") for name in release.shipped_files(with_desktop))


def test_the_archive_is_byte_for_byte_the_same_with_or_without_desktop(tmp_path):
    a = release.build_source_archive(_tree(tmp_path / "with", True), str(tmp_path / "a.zip"))
    b = release.build_source_archive(_tree(tmp_path / "without", False), str(tmp_path / "b.zip"))
    assert a["sha256"] == b["sha256"] and a["files"] == b["files"]
    with zipfile.ZipFile(tmp_path / "a.zip") as archive:
        assert not [n for n in archive.namelist() if "desktop" in n or n.endswith((".spec", ".exe"))]


def test_nothing_in_the_allowlist_names_desktop():
    for entry in (*release.SHIPPED_FILES, *release.SHIPPED_PACKAGES, *release.SHIPPED_ASSET_DIRS):
        assert not entry.startswith("desktop")


def test_the_real_repository_release_has_no_desktop_files(tmp_path):
    files = release.shipped_files(REPO_ROOT)
    assert not [f for f in files if f.startswith("desktop/") or "dlne_donate" in f]
    info = release.build_source_archive(REPO_ROOT, str(tmp_path / "real.zip"))
    with zipfile.ZipFile(tmp_path / "real.zip") as archive:
        assert not [n for n in archive.namelist() if n.startswith("desktop") or "dlne_donate" in n]
    assert hashlib.sha256((tmp_path / "real.zip").read_bytes()).hexdigest() == info["sha256"]
