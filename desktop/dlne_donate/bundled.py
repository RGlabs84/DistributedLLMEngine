"""
The few engine files this app is BUILT with, and its icon.

Two different copies of ``engine/`` exist while the app runs, and they must never be confused:

* the one downloaded from the coordinator, verified and unpacked under the app-data folder. That is the
  engine the worker process runs, and nothing else is ever run from there;
* this one, a snapshot of four modules taken when the app was built (``engine/release.py`` for the trust anchor
  and the manifest checks, ``hardware.py`` to see the machine the way the worker will, ``coding_member_select.py``
  to say what a setting powers, and ``config.py`` that those two read). The tray process uses them to draw the
  Settings window. They are plain data files in the build, never imported by the worker process. The list is
  packaging/engine_snapshot.py; it also holds a quiet stand-in for ``engine/logger.py``, which ``hardware.py``
  imports (the real one would open a log file next to the executable), and a test imports the snapshot on its own.

From a source checkout the snapshot is the repository itself.
"""

import importlib
import importlib.util
import os
import sys
from types import ModuleType
from typing import Optional

_release_module: Optional[ModuleType] = None


def _frozen_root() -> Optional[str]:
    return getattr(sys, "_MEIPASS", None) if getattr(sys, "frozen", False) else None


def bundled_engine_root() -> str:
    """The folder that holds the snapshot's ``engine/`` package."""
    frozen = _frozen_root()
    if frozen:
        return os.path.join(frozen, "bundled_engine")
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def asset_path(name: str) -> str:
    """A file shipped with the app: the DLNE icon. From a checkout it is the repository's own copy."""
    files = {"icon.png": ("android", "play", "icon-512.png")}[name]
    frozen = _frozen_root()
    if frozen:
        return os.path.join(frozen, "assets", files[-1])
    return os.path.join(bundled_engine_root(), *files)


def load_release() -> ModuleType:
    """
    ``engine/release.py`` of the snapshot, loaded by file path under a private name, so it cannot collide with the
    ``engine`` package the worker process loads from the verified download. Its only imports are the standard
    library (and ``cryptography``, lazily, when a signature is checked).
    """
    global _release_module
    if _release_module is None:
        path = os.path.join(bundled_engine_root(), "engine", "release.py")
        spec = importlib.util.spec_from_file_location("dlne_donate_release", path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules["dlne_donate_release"] = module   # dataclasses/typing look the module up by name
        spec.loader.exec_module(module)
        _release_module = module
    return _release_module


def import_snapshot(name: str) -> ModuleType:
    """
    ``engine.<name>`` of the snapshot (``hardware`` or ``coding_member_select``), for the tray process only. Never call
    this in the worker process: there ``engine`` must be the verified download.
    """
    root = bundled_engine_root()
    if root not in sys.path:
        sys.path.insert(0, root)
    return importlib.import_module(f"engine.{name}")
