"""
The end-to-end run of the coding-members system (docs/coding-members.md): real processes on loopback.

Nothing in this folder runs in the normal suite. Every test here is skipped unless DLNE_INTEGRATION=1, so
``pytest`` on a laptop, in CI and on TheWall's copy stays exactly as it was. To run it, use
``tests/integration/run_integration.sh`` (it puts the run in the CPU lane of the machine's heavy-job lock, with a
memory cap and a timeout, and prints the llama.cpp build and the RAM before and after).

What the run needs: the pinned test model (tests/integration/models.py) in ~/.cache/dlne-test, a llama.cpp build
(newest found in ~/.cache), the node binary and a compiled extension (``npm run compile`` in vscode-extension).
"""

import os
import shutil
import time
from pathlib import Path

import pytest

ENABLED = os.environ.get("DLNE_INTEGRATION") == "1"
HERE = Path(__file__).resolve().parent


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: a real-process end-to-end test (needs DLNE_INTEGRATION=1)")


def pytest_collection_modifyitems(config, items):
    if ENABLED:
        return
    skip = pytest.mark.skip(reason="end-to-end run: set DLNE_INTEGRATION=1 (see tests/integration/run_integration.sh)")
    for item in items:
        try:
            inside = HERE in Path(str(getattr(item, "path", None) or item.fspath)).resolve().parents
        except OSError:
            inside = False
        if inside:
            item.add_marker(skip)


if ENABLED:
    # Only the integration run needs these; the normal suite never imports them.
    from tests.integration import world as world_module

    @pytest.fixture(scope="session")
    def resources():
        try:
            return world_module.discover()
        except Exception as exc:  # noqa: BLE001 - a machine without the model or an engine cannot run this
            pytest.skip(f"integration resources are missing: {exc}")

    @pytest.fixture(scope="session")
    def run_root():
        base = Path(os.environ.get("DLNE_IT_DIR") or "~/.cache/dlne-it").expanduser()
        base.mkdir(parents=True, exist_ok=True)
        for old in sorted(base.glob("run-*"))[:-3]:             # keep the last three runs for a look at the logs
            shutil.rmtree(old, ignore_errors=True)
        root = base / time.strftime("run-%Y%m%d-%H%M%S")
        root.mkdir()
        return root

    @pytest.fixture(scope="session", autouse=True)
    def _announce(resources):
        print(f"\n[integration] llama.cpp: {resources.llama_version} ({resources.llama_dir}); "
              f"model: {resources.model.name}; MemAvailable {world_module.mem_available_mb()} MB", flush=True)
        yield
        print(f"\n[integration] MemAvailable at the end: {world_module.mem_available_mb()} MB", flush=True)
