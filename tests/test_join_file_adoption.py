"""
Tests for the join file being *used* — by the boot path.

THE FAILURE THESE COVER
-----------------------
A tester installed 1.0.6, and the installer copied their ``network_join.dlnejoin`` beside
the binary exactly as designed. The graphical wizard then ignored it completely: it
implemented only the older WireGuard ``.conf`` invite path and never read ``join_files``
out of its own bootstrap payload, so the tester was asked for a Coordinator address they
had been given a file to avoid typing. The config that came out carried no NETWORK_KEY.

The node then booted unsigned, the Coordinator refused its heartbeat with a 401, it shut
down — and every relaunch did the same, because setup only runs when ``config.txt`` is
*absent*. Nothing in that loop could end it, and the file that would end it was sitting in
the same folder the whole time.

So there are two things to hold down: a keyless joining node adopts the key beside it, and
the wizard's page actually asks for the join file it is handed.
"""

import os

import engine.setup_core as setup_core
from engine.config import get_config


NETWORK_KEY = "b8e1f0c9a7d24e3f8a6b5c4d3e2f1a0b9c8d7e6f5a4b3c2d1e0f9a8b7c6d5e4f"
PUBKEY = "gOpcFhvKSI8J4CYUMRZNMYzhkmvJPovr6fdDhihq9Uc="


def _join_file(root, coordinator="203.0.113.9", key=NETWORK_KEY):
    path = str(root / f"network_join{setup_core.JOIN_FILE_SUFFIX}")
    setup_core.write_join_file(path, coordinator_host=coordinator, network_key=key,
                               wg_endpoint=f"{coordinator}:51820", wg_pubkey=PUBKEY)
    return path


def _isolate(monkeypatch, root, tmp_path):
    """Point every search path at the temporary root, including the ones off in $HOME."""
    monkeypatch.setattr(setup_core, "get_base_dir", lambda: str(root))
    monkeypatch.setattr(os.path, "expanduser", lambda _: str(tmp_path / "nowhere"))
    monkeypatch.chdir(tmp_path)


# --------------------------------------------------------------------------------------
# Boot-time adoption
# --------------------------------------------------------------------------------------

def test_a_keyless_node_adopts_the_key_from_the_join_file_beside_it(
        dlne_root, isolated_config, tmp_path, monkeypatch):
    """The rescue. Without this the node is in a 401 loop no restart can break."""
    _isolate(monkeypatch, dlne_root, tmp_path)
    isolated_config.write_text("MODE=worker_only\nCOORDINATOR_IP=192.168.40.23\n",
                               encoding="utf-8")
    _join_file(dlne_root)

    outcome = setup_core.adopt_join_file_credentials()

    assert outcome["adopted"] is True
    assert get_config(force_reload=True)["NETWORK_KEY"] == NETWORK_KEY


def test_adoption_keeps_a_coordinator_address_someone_chose(
        dlne_root, isolated_config, tmp_path, monkeypatch):
    """
    The join file names the Coordinator's public address, which routes through a tunnel
    this function does not bring up. A LAN address already in the config is reachable
    *now*, so overwriting it would trade a working node for a broken one.
    """
    _isolate(monkeypatch, dlne_root, tmp_path)
    isolated_config.write_text("MODE=worker_only\nCOORDINATOR_IP=192.168.40.23\n",
                               encoding="utf-8")
    _join_file(dlne_root, coordinator="203.0.113.9")

    setup_core.adopt_join_file_credentials()

    assert get_config(force_reload=True)["COORDINATOR_IP"] == "192.168.40.23"


def test_adoption_supplies_the_address_when_the_config_has_none_worth_keeping(
        dlne_root, isolated_config, tmp_path, monkeypatch):
    """127.0.0.1 is the default, not a decision — a worker pointed at itself finds nothing."""
    _isolate(monkeypatch, dlne_root, tmp_path)
    isolated_config.write_text("MODE=worker_only\nCOORDINATOR_IP=127.0.0.1\n", encoding="utf-8")
    _join_file(dlne_root, coordinator="203.0.113.9")

    outcome = setup_core.adopt_join_file_credentials()

    config = get_config(force_reload=True)
    assert config["COORDINATOR_IP"] == "203.0.113.9"
    assert outcome["network"] == "203.0.113.9"


def test_a_node_that_already_has_a_key_is_left_alone(
        dlne_root, isolated_config, tmp_path, monkeypatch):
    """
    A node configured by hand against one network must not be silently moved to another
    because an old join file was never cleaned out of Downloads.
    """
    _isolate(monkeypatch, dlne_root, tmp_path)
    existing = "f" * 64
    isolated_config.write_text(f"MODE=worker_only\nNETWORK_KEY={existing}\n", encoding="utf-8")
    _join_file(dlne_root)

    outcome = setup_core.adopt_join_file_credentials()

    assert outcome["adopted"] is False
    assert get_config(force_reload=True)["NETWORK_KEY"] == existing


def test_no_join_file_is_not_an_error(dlne_root, isolated_config, tmp_path, monkeypatch):
    """The ordinary case for a node configured by hand. Nothing to say about it."""
    _isolate(monkeypatch, dlne_root, tmp_path)
    isolated_config.write_text("MODE=worker_only\n", encoding="utf-8")

    outcome = setup_core.adopt_join_file_credentials()

    assert outcome == {"adopted": False, "path": "", "network": "", "error": ""}


def test_an_unusable_join_file_is_reported_rather_than_ignored(
        dlne_root, isolated_config, tmp_path, monkeypatch):
    """
    Silence here is indistinguishable from "no file found", and this is the file the
    operator was told to drop in. They need to hear that it was found and rejected.
    """
    _isolate(monkeypatch, dlne_root, tmp_path)
    isolated_config.write_text("MODE=worker_only\n", encoding="utf-8")
    (dlne_root / f"broken{setup_core.JOIN_FILE_SUFFIX}").write_text(
        "COORDINATOR_HOST=203.0.113.9\n", encoding="utf-8")

    outcome = setup_core.adopt_join_file_credentials()

    assert outcome["adopted"] is False
    assert "broken" in outcome["error"]


def test_a_usable_join_file_wins_over_a_broken_one(
        dlne_root, isolated_config, tmp_path, monkeypatch):
    """One stale file in the folder must not cost the node a working one."""
    _isolate(monkeypatch, dlne_root, tmp_path)
    isolated_config.write_text("MODE=worker_only\n", encoding="utf-8")
    # "a" sorts first, so the broken file is reached first and has to be stepped over.
    (dlne_root / f"a_broken{setup_core.JOIN_FILE_SUFFIX}").write_text(
        "COORDINATOR_HOST=203.0.113.9\n", encoding="utf-8")
    _join_file(dlne_root)

    outcome = setup_core.adopt_join_file_credentials()

    assert outcome["adopted"] is True
    assert outcome["error"] == ""
    assert get_config(force_reload=True)["NETWORK_KEY"] == NETWORK_KEY


def test_adoption_survives_a_config_that_cannot_be_written(
        dlne_root, isolated_config, tmp_path, monkeypatch):
    """A read-only install directory is a bad launch, not a traceback."""
    _isolate(monkeypatch, dlne_root, tmp_path)
    isolated_config.write_text("MODE=worker_only\n", encoding="utf-8")
    _join_file(dlne_root)
    monkeypatch.setattr("engine.config.set_config_values", lambda *_a, **_k: False)

    outcome = setup_core.adopt_join_file_credentials()

    assert outcome["adopted"] is False
    assert outcome["error"]


def test_only_joining_roles_are_rescued():
    """
    A Coordinator hosts the network rather than joining it: its key is generated, not
    received, and adopting one from a stray file would re-point the whole cluster.
    """
    assert setup_core.JOINING_MODES == ("worker_only", "redundancy_worker")
    for mode in setup_core.COORDINATOR_MODES:
        assert mode not in setup_core.JOINING_MODES


# The tests that read the retired graphical and console wizards (and main.py's boot path) as
# text lived here. The wizards are gone with the desktop app; the extension writes the node's
# config itself. What remains above is the part that still matters: the join-file logic in
# engine.setup_core.
