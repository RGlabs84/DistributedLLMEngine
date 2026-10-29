# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for DLNE Donate. Windows: one windowed DLNE-Donate.exe. Linux: a one-folder build (DLNE-Donate/).
#
#   DLNE_DONATE_ONEDIR=1   build a folder on Windows too
#   DLNE_DONATE_ICON=<file.ico>   the Windows icon (packaging/make_icon.py writes it)
#
# What goes in:
# * the app (desktop/dlne_donate), Tk, pystray;
# * every library the DLNE engine imports (the engine itself is NOT in the build: the app downloads it, verifies its
#   signature and runs that copy; see engine_imports.py);
# * the snapshot of engine files, as plain DATA under bundled_engine/engine/ (engine_snapshot.py: release.py, hardware.py,
#   coding_member_select.py, config.py, and a quiet stand-in for the engine's logger, which hardware.py imports), the
#   snapshot the Settings window and the trust anchor use. They are not importable from the build's archive, so they
#   can never shadow the verified engine in the worker process;
# * the DLNE icon.
import os
import sys

from PyInstaller.utils.hooks import collect_submodules

SPEC_DIR = os.path.abspath(SPECPATH)
DESKTOP = os.path.dirname(SPEC_DIR)
REPO = os.path.dirname(DESKTOP)
sys.path.insert(0, SPEC_DIR)
from engine_imports import engine_imports  # noqa: E402
from engine_snapshot import snapshot_files  # noqa: E402

standard, third_party = engine_imports(REPO)
hidden = list(standard) + list(third_party)
# Libraries that import parts of themselves by name at run time, which PyInstaller cannot see.
for package in ("uvicorn", "starlette", "fastapi", "anyio", "httpx", "httpcore", "h11", "websockets", "rich", "pydantic",
                "pydantic_core", "cryptography", "psutil", "GPUtil", "colorama", "bcrypt", "pystray", "PIL"):
    hidden += collect_submodules(package)
# GPUtil imports distutils, which Python 3.12 no longer ships; setuptools provides it.
hidden += ["distutils", "setuptools._distutils", "_distutils_hack"]
if sys.platform == "win32":
    hidden += ["pystray._win32", "winreg"]
else:
    hidden += ["pystray._xorg", "Xlib", "Xlib.display", "Xlib.ext", "Xlib.ext.xinerama", "Xlib.ext.shape"]

datas = [(source, os.path.join("bundled_engine", "engine")) for source, _name in snapshot_files(REPO, SPEC_DIR)]
datas.append((os.path.join(REPO, "android", "play", "icon-512.png"), "assets"))

a = Analysis(
    [os.path.join(SPEC_DIR, "launcher.py")],
    pathex=[DESKTOP],
    binaries=[],
    datas=datas,
    hiddenimports=sorted(set(hidden)),
    hookspath=[],
    runtime_hooks=[],
    excludes=["engine", "pytest", "IPython", "matplotlib", "numpy", "scipy", "pandas", "tkinter.test", "test"],
    noarchive=False,
)
pyz = PYZ(a.pure)

icon = os.environ.get("DLNE_DONATE_ICON") or None
onedir = sys.platform != "win32" or os.environ.get("DLNE_DONATE_ONEDIR") == "1"

if onedir:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="DLNE-Donate", console=False, icon=icon, upx=False)
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="DLNE-Donate")
else:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="DLNE-Donate", console=False, icon=icon, upx=False,
              runtime_tmpdir=None)
