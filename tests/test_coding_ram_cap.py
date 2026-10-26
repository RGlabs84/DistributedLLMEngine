"""
The RAM the coding job may use (CODING_MAX_RAM_GB), apart from MAX_RAM_GB.

The extension always passes ``--max-ram <dlne.worker.maxRamGb || 4>``, a small default meant for the GPU's other job
(an RPC node for the shared model). Read as the coding job's cap it refused every 8 or 12 GB GPU, whose coding model
needs 10 to 14 GB of RAM counted. So the extension also passes ``--coding-max-ram`` (-1, no cap, unless the user set
dlne.worker.maxRamGb), dlne_server.py writes CODING_MAX_RAM_GB, and only the coding job reads it. An older extension
does not write the key, and then the coding job falls back to MAX_RAM_GB as it always did. The RPC node and the phone
pool keep reporting through MAX_RAM_GB, unchanged.
"""

import glob
import os
import re
import sys
from types import SimpleNamespace

import pytest

from engine import coding_ladder, hardware, worker
from engine import coding_member_select as select
from engine import config as engine_config

KEY = "a-network-key-for-tests"
GPU8 = {"gpu": {"vram_total_gb": 8.0, "name": "NVIDIA GeForce RTX 4070 Laptop GPU", "has_gpu": True}, "ram_gb": 4.0}


@pytest.fixture
def node(monkeypatch, tmp_path):
    import engine.setup_core as setup_core
    monkeypatch.setattr(setup_core, "get_or_create_worker_id", lambda: "worker-under-test")
    monkeypatch.setattr(worker, "get_hardware_profile", lambda: dict(GPU8))
    monkeypatch.setattr(worker, "get_base_dir", lambda: str(tmp_path))
    n = worker.WorkerNode("http://coordinator:11435")
    n.hardware_profile = dict(GPU8)
    yield n
    n._stopped = True


def _limits(monkeypatch, **limits):
    monkeypatch.setattr(worker, "get_worker_limits", lambda: {"network_key": KEY, **limits})


def _memory(monkeypatch, total_gib, available_gib):
    import psutil
    monkeypatch.setattr(psutil, "virtual_memory", lambda: SimpleNamespace(
        total=int(total_gib * 2 ** 30), available=int(available_gib * 2 ** 30)))


def _offered(info):
    """The tiers the real chooser offers a machine described by ``_machine_info()``."""
    extra = {k: v for k, v in info.items() if k not in ("vram_gb", "ram_gb", "limited")}
    return [t["id"] for t in select.candidate_tiers(info["vram_gb"], info["ram_gb"], coding_ladder.load_coding_tiers(),
                                                    {}, **extra)]


# ======================================================================================
# What the coding job is offered
# ======================================================================================

def test_the_extensions_default_leaves_the_coding_job_uncapped_and_an_8_gb_gpu_is_offered_a_tier(node, monkeypatch):
    _memory(monkeypatch, 32, 16)
    _limits(monkeypatch, max_ram_gb=4.0, coding_max_ram_gb=-1.0)
    info = node._machine_info()
    assert info == {"vram_gb": 8.0, "ram_gb": 16.0, "unified": False}, "just the RAM that is free right now"
    assert _offered(info) == ["qwen36-a3b"]


def test_an_explicit_coding_cap_is_honoured_as_before(node, monkeypatch):
    _memory(monkeypatch, 32, 16)
    _limits(monkeypatch, max_ram_gb=4.0, coding_max_ram_gb=4.0)
    info = node._machine_info()
    assert info == {"vram_gb": 8.0, "ram_gb": 4.0, "unified": False, "limited": True}
    assert _offered(info) == [], "refused, as it is today"


def test_a_higher_explicit_cap_than_max_ram_lets_the_coding_job_use_it(node, monkeypatch):
    _memory(monkeypatch, 32, 20)
    _limits(monkeypatch, max_ram_gb=4.0, coding_max_ram_gb=16.0)
    info = node._machine_info()
    assert info["ram_gb"] == 16.0 and info.get("limited") is True and _offered(info) == ["qwen36-a3b"]


def test_without_the_key_the_coding_job_follows_max_ram_gb_as_it_always_did(node, monkeypatch):
    """An older extension writes MAX_RAM_GB only."""
    _memory(monkeypatch, 32, 16)
    _limits(monkeypatch, max_ram_gb=4.0)
    info = node._machine_info()
    assert info == {"vram_gb": 8.0, "ram_gb": 4.0, "unified": False, "limited": True} and _offered(info) == []
    _limits(monkeypatch, max_ram_gb=4.0, coding_max_ram_gb=None)
    assert node._machine_info()["ram_gb"] == 4.0


def test_with_neither_key_nothing_is_capped(node, monkeypatch):
    _memory(monkeypatch, 32, 16)
    _limits(monkeypatch)
    assert node._machine_info() == {"vram_gb": 8.0, "ram_gb": 16.0, "unified": False}
    _limits(monkeypatch, max_ram_gb=-1.0, coding_max_ram_gb=None)
    assert node._machine_info()["ram_gb"] == 16.0


@pytest.mark.parametrize("junk", ["x", True, [], {}])
def test_a_coding_cap_that_is_not_a_number_counts_as_absent(node, monkeypatch, junk):
    _memory(monkeypatch, 32, 16)
    _limits(monkeypatch, max_ram_gb=6.0, coding_max_ram_gb=junk)
    assert node._machine_info()["ram_gb"] == 6.0


@pytest.mark.parametrize("coding, legacy, expected", [
    (-1.0, 4.0, None), (-5, 4.0, None),          # any negative number is "no cap", and does not fall back
    (0.0, -1.0, 0.0), (12.0, 4.0, 12.0), (3, -1.0, 3.0),
    (None, 4.0, 4.0), (None, -1.0, None), (None, 0.0, 0.0),
])
def test_the_cap_the_coding_job_reads(coding, legacy, expected):
    assert worker._coding_ram_cap_gib({"coding_max_ram_gb": coding, "max_ram_gb": legacy}) == expected


def test_the_prompt_cache_shrinks_only_when_a_coding_cap_binds(node, monkeypatch):
    from engine.coding_member import LIMITED_PROMPT_CACHE_MIB
    _memory(monkeypatch, 32, 20)
    _limits(monkeypatch, max_ram_gb=4.0, coding_max_ram_gb=-1.0)
    assert "cache_ram_mib" not in node._coding_resources(), "MAX_RAM_GB is not the coding job's cap any more"
    _limits(monkeypatch, max_ram_gb=4.0, coding_max_ram_gb=16.0)
    assert node._coding_resources()["cache_ram_mib"] == LIMITED_PROMPT_CACHE_MIB
    _limits(monkeypatch, max_ram_gb=4.0, coding_max_ram_gb=64.0)
    assert "cache_ram_mib" not in node._coding_resources(), "a cap above the installed RAM binds nothing"
    _limits(monkeypatch, max_ram_gb=4.0)
    assert node._coding_resources()["cache_ram_mib"] == LIMITED_PROMPT_CACHE_MIB, "an older extension: as before"


# ======================================================================================
# What does not change: the RPC node and the phone pool
# ======================================================================================

def test_the_ram_a_node_reports_to_the_coordinator_still_follows_max_ram_gb(monkeypatch):
    import psutil
    monkeypatch.setattr(psutil, "virtual_memory", lambda: SimpleNamespace(total=32 * 2 ** 30, available=16 * 2 ** 30))
    for limits in ({"max_ram_gb": 4.0, "coding_max_ram_gb": -1.0}, {"max_ram_gb": 4.0, "coding_max_ram_gb": 16.0},
                   {"max_ram_gb": 4.0}):
        monkeypatch.setattr(hardware, "get_worker_limits", lambda limits=limits: limits)
        assert hardware.get_system_ram() == 4.0
    monkeypatch.setattr(hardware, "get_worker_limits", lambda: {"max_ram_gb": -1.0, "coding_max_ram_gb": 2.0})
    assert hardware.get_system_ram() == 32.0, "a coding cap never lowers what an RPC node or the phone pool reports"


def test_only_the_coding_job_reads_the_key():
    here = os.path.dirname(os.path.abspath(worker.__file__))
    readers = sorted(os.path.basename(path) for path in glob.glob(os.path.join(here, "*.py"))
                     if "coding_max_ram_gb" in open(path, encoding="utf-8").read())
    assert readers == ["config.py", "worker.py"], "the RPC, registry and phone-pool paths stay on max_ram_gb"
    source = open(os.path.join(here, "worker.py"), encoding="utf-8").read()
    assert len(re.findall(r"coding_max_ram_gb", source)) == 1, "only _coding_ram_cap_gib reads it"


# ======================================================================================
# config.txt and the command line
# ======================================================================================

@pytest.mark.parametrize("line, expected", [
    ("", None), ("CODING_MAX_RAM_GB=-1", -1.0), ("CODING_MAX_RAM_GB=16", 16.0), ("CODING_MAX_RAM_GB=12.5", 12.5),
    ("CODING_MAX_RAM_GB=0", 0.0), ("CODING_MAX_RAM_GB=junk", None), ("CODING_MAX_RAM_GB=", None),
    ("CODING_MAX_RAM_GB=nan", None), ("CODING_MAX_RAM_GB=inf", None),
])
def test_the_config_key_is_read_as_a_number_and_absent_means_absent(isolated_config, monkeypatch, line, expected):
    isolated_config.write_text("MAX_RAM_GB=4.0\n" + line + "\n", encoding="utf-8")
    monkeypatch.setattr(engine_config, "_cached_stamp", None)
    limits = engine_config.get_worker_limits()
    assert limits["coding_max_ram_gb"] == expected
    assert limits["max_ram_gb"] == 4.0, "MAX_RAM_GB is read as before"


def test_the_key_is_one_of_the_wizards_config_keys():
    from engine import setup_core
    order = setup_core.CONFIG_KEY_ORDER
    assert "CODING_MAX_RAM_GB" in order and order.index("CODING_MAX_RAM_GB") == order.index("MAX_RAM_GB") + 1


class _FakeWorker:
    def __init__(self, **kwargs):
        pass

    def start(self):
        pass


def _run_worker_command(monkeypatch, *flags):
    import dlne_server
    written = []
    monkeypatch.setattr(dlne_server, "set_config_values", lambda updates: written.append(dict(updates)))
    monkeypatch.setattr(worker, "WorkerNode", _FakeWorker)
    monkeypatch.setattr(sys, "argv", ["dlne_server.py", "worker", "--coordinator", "http://c:1", "--rpc-port", "50052", *flags])
    dlne_server.main()
    return written


@pytest.mark.parametrize("flags, expected", [
    ((), []),
    (("--max-ram", "4"), [{"MAX_RAM_GB": "4.0"}]),
    (("--max-ram", "4", "--coding-max-ram", "-1"), [{"MAX_RAM_GB": "4.0", "CODING_MAX_RAM_GB": "-1.0"}]),
    (("--max-ram", "4", "--coding-max-ram", "16"), [{"MAX_RAM_GB": "4.0", "CODING_MAX_RAM_GB": "16.0"}]),
    (("--coding-max-ram", "0"), [{"CODING_MAX_RAM_GB": "0.0"}]),
    (("--max-ram", "0"), []),                 # --max-ram keeps its old handling: zero is "not given"
])
def test_the_command_line_writes_the_key_and_minus_one_is_written(isolated_config, monkeypatch, flags, expected):
    assert _run_worker_command(monkeypatch, *flags) == expected
