"""
The snapshot of engine files the frozen app is built with (packaging/engine_snapshot.py) must stand on its own.

The Settings window imports hardware.py and coding_member_select.py from it, and a failed import there is swallowed:
the window then shows a computer with no GPU. That is what a build made from the 2.6.2 engine did, because
engine/hardware.py began to import engine.logger, which the snapshot did not hold. These tests lay the snapshot out the way
the build does and import it in a clean interpreter.
"""

import ast
import importlib.util
import os
import shutil
import subprocess
import sys

from conftest import DESKTOP, REPO

PACKAGING = os.path.join(str(DESKTOP), "packaging")
if PACKAGING not in sys.path:
    sys.path.insert(0, PACKAGING)

import engine_snapshot  # noqa: E402


def assemble(tmp_path):
    """The snapshot as the build lays it out: <root>/engine/<files>."""
    root = tmp_path / "snapshot"
    (root / "engine").mkdir(parents=True)
    for source, name in engine_snapshot.snapshot_files(str(REPO), PACKAGING):
        shutil.copy(source, root / "engine" / name)
    return root


def test_the_snapshot_imports_by_itself_and_writes_no_log(tmp_path):
    root = assemble(tmp_path)
    program = (
        "import sys\n"
        f"sys.path.insert(0, {str(root)!r})\n"
        "import engine.config, engine.hardware, engine.release, engine.coding_member_select, engine.logger\n"
        "for name in ('_detect_physical_cores', 'get_cpu_features', 'get_all_gpus', 'counts_as_gpu'):\n"
        "    assert callable(getattr(engine.hardware, name)), name\n"
        "assert engine.hardware.counts_as_gpu('NVIDIA GeForce RTX 4070 Laptop GPU') is True\n"
        "print('LOGGER', engine.logger.__file__)\n"
    )
    result = subprocess.run([sys.executable, "-I", "-c", program], capture_output=True, text=True, timeout=120, cwd=str(tmp_path))
    assert result.returncode == 0, result.stderr
    assert f"LOGGER {root / 'engine' / 'logger.py'}" in result.stdout, "the snapshot must carry the quiet logger, not the engine's"
    written = [p for p in tmp_path.rglob("*") if p.name in ("logs", "dlne_node.log")]
    assert not written, f"importing the snapshot wrote a log: {written}"


def test_the_quiet_logger_has_every_log_function_of_the_real_one():
    tree = ast.parse((REPO / "engine" / "logger.py").read_text(encoding="utf-8"))
    names = [node.name for node in tree.body if isinstance(node, ast.FunctionDef) and node.name.startswith("log_")]
    assert names, "engine/logger.py has no log_* functions any more: this test needs a look"
    path = os.path.join(PACKAGING, "snapshot_engine", "logger.py")
    spec = importlib.util.spec_from_file_location("dlne_donate_quiet_logger", path)
    quiet = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(quiet)
    for name in names:
        assert getattr(quiet, name)("any text") is None, name
    assert quiet.log_something_the_engine_adds_later("any text") is None
    assert not hasattr(quiet, "setup_logger"), "only log_* names are silent; anything else is a real import error"
