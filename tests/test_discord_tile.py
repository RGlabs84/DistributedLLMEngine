"""
scripts/dlne_discord_tile.py: the live swarm tile BarrkBOT keeps in a public Discord channel.

Until 2.4.9 it listed every remote worker as "Remote Worker (`ip:port`)", a volunteer's home address
in a public channel. It now shows counts and how far away the GPUs are, never an address.
"""

import importlib.util
import json
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "dlne_discord_tile.py"
_spec = importlib.util.spec_from_file_location("dlne_discord_tile", _PATH)
tile = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tile)


@pytest.fixture(autouse=True)
def fixed_extension_version(monkeypatch):
    monkeypatch.setattr(tile, "get_vscode_extension_version", lambda: "1.4.0")


def status(**extra):
    base = {"status": "ready", "active_workers_count": 3, "total_vram_gb": 32.0, "total_sysram_gb": 40.0,
            "global_active_model": "Qwen3.6-35B-A3B-UD-IQ3_S.gguf", "server_version": "2.4.9",
            "active_leases_count": 0, "model_loading": {"is_loading": False}}
    return {**base, **extra}


def rigs(fields):
    return dict((name, value) for name, value, _inline in fields["fields"])["🌐 Connected Swarm Rigs"]


def test_volunteer_gpus_are_counted_and_graded_never_addressed():
    fields = tile.build_tile_fields(status(gpu_pool={"reach": {"host": 2, "best": 1, "good": 0, "usable": 1,
                                                               "too_far": 0, "unmeasured": 0}}))
    assert "TheWall" in rigs(fields)
    assert "**2 volunteer GPU(s)**: 1 best link, 1 usable link" in rigs(fields)


def test_an_old_status_that_still_lists_addresses_is_not_echoed():
    leaky = status(active_endpoints=["127.0.0.1:50052", "73.215.216.195:50052", "203.0.113.9:50052"])
    fields = tile.build_tile_fields(leaky)
    text = json.dumps(fields)
    assert "73.215.216.195" not in text and "203.0.113.9" not in text and "50052" not in text
    assert "**2 volunteer GPU(s)**" in rigs(fields)          # from the count: every worker but TheWall's own


def test_nobody_lending_yet_invites_people_to():
    fields = tile.build_tile_fields(status(active_workers_count=1, gpu_pool={"reach": {"host": 2}}))
    assert "Connect your worker node" in rigs(fields)


@pytest.mark.parametrize("reach", [{"best": "lots"}, {"best": True}, {"best": -3}, "not a dict"])
def test_a_strange_reach_never_breaks_the_tile(reach):
    tile.build_tile_fields(status(gpu_pool={"reach": reach}))
