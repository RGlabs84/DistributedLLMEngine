"""
The few engine files the frozen app is built with, defined once for the PyInstaller spec and for the tests.

The Settings window draws what this computer can lend and what the signed release offers with a SNAPSHOT of engine
files (``bundled_engine/engine/`` inside the build; see dlne_donate/bundled.py), because the app has no engine of its
own until the owner presses Donate. The snapshot must be self-contained: ``engine/hardware.py`` imported only
``engine.config`` when the app was first built, and imports ``engine.logger`` as well since 2.6.2's engine; with the
logger missing, the import failed quietly and the window said "no GPU the swarm can use" on a computer that has one.

    python engine_snapshot.py <repository root>     prints "<source file> -> engine/<name>" for every file
"""

import os
import sys
from typing import List, Tuple

# Copied from the repository's engine/ as they are.
ENGINE_FILES = ("__init__.py", "config.py", "hardware.py", "coding_member_select.py", "release.py")

# Not the engine's file: a quiet stand-in kept in packaging/snapshot_engine/, taking the engine file's place in the snapshot.
# engine/logger.py prints with Rich and opens a log file next to the executable, neither of which the tray process wants.
STAND_INS = ("logger.py",)


def snapshot_files(repo: str, packaging_dir: str) -> List[Tuple[str, str]]:
    """``(source file, file name inside the snapshot's engine/ folder)`` for every file of the snapshot."""
    files = [(os.path.join(repo, "engine", name), name) for name in ENGINE_FILES]
    files += [(os.path.join(packaging_dir, "snapshot_engine", name), name) for name in STAND_INS]
    return files


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    for source, name in snapshot_files(sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(here)), here):
        print(f"{source} -> engine/{name}")
