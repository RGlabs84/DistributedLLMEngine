"""
Shared fixtures for the DLNE test-suite.

The suite has to run on a laptop with no GPU, no network access and no llama.cpp binaries
unpacked, and — just as importantly — it runs on the same checkout people develop against,
so it must never read or write the developer's real ``config/config.txt``, real ``models/``
folder or real llama-server processes. Everything that would otherwise reach outside the
process is redirected by the fixtures below.

WHY THE PATCHING IS DONE IN TWO PLACES
    ``engine.config`` computes ``CONFIG_DIR``/``CONFIG_FILE`` once at import time from
    ``get_base_dir()``, so re-pointing ``get_base_dir`` alone would leave those constants
    aimed at the real installation. Both the function and the derived constants are
    therefore patched.

    Separately, the consumer modules do ``from engine.config import get_base_dir,
    get_worker_limits``. That binds the *function objects* into each consumer's own module
    globals at import time, so patching ``engine.config.get_base_dir`` does not change what
    ``engine.inference_manager`` calls. Consumers are patched in their own namespace by the
    fixtures that need them (see ``tests/test_inference_manager.py``).
"""

import os
import sys
from pathlib import Path

import pytest

# `pythonpath = .` in pytest.ini covers the normal `pytest` / `python -m pytest` invocation
# from the repository root. This guard additionally keeps `pytest tests/test_authn.py`
# working when an IDE runner launches it from some unrelated working directory, because
# `engine` is a top-level package one level above this file.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Importing engine.coordinator has an import-time side effect: it resolves the WebUI folder
# with os.path.abspath("webui") and then os.makedirs() it, so importing the module from any
# other working directory creates an empty "webui" directory there. Pinning the process CWD
# to the repository root at collection time — before any test module is imported — keeps the
# suite from scattering directories around when it is launched from an IDE runner. (The
# underlying CWD-relative path in coordinator.py is reported separately; this guard only
# stops the *test suite* from tripping over it.)
os.chdir(_REPO_ROOT)

import engine.config as engine_config  # noqa: E402  (must follow the sys.path guard)


@pytest.fixture
def dlne_root(tmp_path):
    """
    A throwaway DLNE installation root with the directory layout the engine expects.

    Returns a ``pathlib.Path``. Tests that need the ``models`` or ``logs`` sub-directories
    build their paths from this so nothing is ever created next to the real installation.
    """
    root = tmp_path / "dlne"
    (root / "config").mkdir(parents=True)
    (root / "models").mkdir()
    (root / "logs").mkdir()
    return root


@pytest.fixture
def isolated_config(dlne_root, monkeypatch):
    """
    Re-points ``engine.config`` at :func:`dlne_root` and clears its stat cache.

    Returns the ``pathlib.Path`` of the temporary ``config.txt`` (which does not exist yet —
    a missing config file is a state the parser must handle, and several tests exercise it).

    The cache reset is not optional. ``get_config`` caches on ``(st_mtime_ns, st_size)`` of
    whatever ``CONFIG_FILE`` pointed at when it last ran; without clearing it, a snapshot of
    the developer's real config could be served for the temporary file, and one test's
    writes would be visible to the next.
    """
    config_dir = dlne_root / "config"
    config_file = config_dir / "config.txt"

    monkeypatch.setattr(engine_config, "get_base_dir", lambda: str(dlne_root))
    monkeypatch.setattr(engine_config, "CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(engine_config, "CONFIG_FILE", str(config_file))
    monkeypatch.setattr(engine_config, "_CONFIG_TMP_FILE", str(config_file) + ".tmp")

    monkeypatch.setattr(engine_config, "_cached_config", {})
    monkeypatch.setattr(engine_config, "_cached_stamp", None)

    return config_file


@pytest.fixture
def make_worker_limits(dlne_root):
    """
    Factory returning a complete ``get_worker_limits()`` dict pointed at the temp root.

    A *complete* dict is returned rather than a couple of relevant keys, because the code
    under test indexes several of them and a partial stub would fail for a reason that has
    nothing to do with the behaviour being asserted. Pass keyword overrides for the values a
    given test actually cares about::

        limits = make_worker_limits(network_key="hunter2", allow_unsigned_workers=True)
    """
    def _make(**overrides):
        limits = {
            "max_vram_gb": "-1",
            "max_ram_gb": -1.0,
            # A positive value keeps start_model off the engine.hardware import path, which
            # would otherwise probe the real CPU of whatever machine runs the suite.
            "max_cpu_threads": 4,
            "allowed_gpu_indices": "-1",
            "enforce_model_whitelist": False,
            "enforce_contrib_clause": True,
            "redundancy_priority_index": 1,
            "custom_gguf_dir": os.path.join(str(dlne_root), "models"),
            "admin_hash": "",
            "network_key": "",
            "dependency_mirror_url": "",
            "allow_unsigned_workers": False,
            "trust_proxy_headers": False,
            # Present so the fixture stays a faithful mirror of get_worker_limits(). A
            # partial stub would send coordinator._trusted_proxy_hops down its
            # raw-config-file fallback, which reads the developer's real config.txt.
            "trusted_proxy_hops": 1,
        }
        limits.update(overrides)
        return limits

    return _make


@pytest.fixture(autouse=True)
def _no_yield_sampler_thread(monkeypatch):
    """
    ``engine.hardware`` samples the owner's load on a thread of its own once a profile is built (2.5.0). In the
    suite that thread would outlive the test that started it and run real subprocesses under later tests' fakes
    (a recorder standing in for ``subprocess.Popen`` then sees a call it never expected). The detector is tested
    by calling ``sample()`` directly; here its thread is never started.
    """
    import engine.hardware as hardware
    monkeypatch.setattr(hardware._yield_detector, "start", lambda: None)


@pytest.fixture(autouse=True)
def _fresh_flap_breaker():
    """
    The coordinator's flap breaker (engine/auto_swap.py, 2.6.2) counts the engine rebuilds of the last hour in this
    process and holds the optional ones once there are too many. Many tests rebuild an engine, and the whole suite
    runs inside one hour: without this the count would carry from one test to the next, and a test late in the run
    would meet a tripped breaker it did nothing to cause (a GPU that joins would be held, and its test fail).
    Reset only when the coordinator is already imported: this must not be what imports it.
    """
    module = sys.modules.get("engine.coordinator")
    if module is not None:
        module.flap_breaker.reset()
    yield


@pytest.fixture(autouse=True)
def _active_model_put_back():
    """
    The coordinator's ``global_active_model`` is set by a chat handler that started an engine and by heartbeats that
    report one, which many tests do. The reconfiguring nudge reads it (2.6.2), so a value one test left behind changed
    what a later test was told, in some orders only (tests/test_coding_endpoint.py after
    test_phone_lane_endpoint.py). Put back after every test; the test files import the coordinator while they are
    collected, so the value saved before the first test is the one it starts with.
    """
    module = sys.modules.get("engine.coordinator")
    saved = module.global_active_model if module is not None else None
    yield
    if module is not None:
        module.global_active_model = saved


@pytest.fixture(autouse=True)
def _fresh_photo_budgets(monkeypatch):
    """
    The coordinator's hourly photo budgets (``image_input_window``, ``phone_photo_window``) are kept per network for an
    hour in this process, and the tests' phones share a few addresses: without this, photos one test sent would count
    against the next. Only when the coordinator is already imported, as above.
    """
    module = sys.modules.get("engine.coordinator")
    if module is not None:
        from engine.priority import DistinctWindow, UsageWindow
        monkeypatch.setattr(module, "image_input_window", DistinctWindow())
        monkeypatch.setattr(module, "phone_photo_window", UsageWindow())
