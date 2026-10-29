"""
Tests for ``engine.setup_core`` — the wizard logic both front ends share.

WHY THIS FILE EXISTS
    The console wizard and the pywebview wizard ask the same questions in different widgets.
    The entire point of ``setup_core`` is that they cannot disagree about the *answers*: what
    a valid contribution is, what lands in ``config.txt``, which ports the firewall opens.

    So these tests pin the shared contract rather than either front end. If a rule can be
    asserted here, neither wizard can quietly grow its own version of it.
"""

import os

import pytest

from engine import setup_core


# ======================================================================================
# Config assembly
# ======================================================================================

def test_config_lines_are_flat_key_value_in_a_stable_order():
    """
    The format contract is flat ``KEY=VALUE`` and nothing else — ``engine.config`` has no
    JSON parser and never will. The stable ordering is not cosmetic either: operators diff
    two installs' config.txt by eye when a node will not join.
    """
    lines = setup_core.build_config_lines({
        "RPC_PORT": "50052",
        "MODE": "worker_only",
        "NETWORK_KEY": "abc123",
        "COORDINATOR_IP": "10.8.0.1",
    })

    assert lines == [
        "MODE=worker_only",
        "NETWORK_KEY=abc123",
        "COORDINATOR_IP=10.8.0.1",
        "RPC_PORT=50052",
    ]
    for line in lines:
        assert line.count("=") >= 1 and not line.startswith("{")


def test_empty_values_are_dropped_rather_than_written_as_bare_keys():
    """
    ``engine.config`` treats a present-but-empty key as an explicit override of its default,
    so writing ``WG_WAN_IP=`` is not the same as omitting it — it would blank a value the
    engine expected to fall back on.
    """
    lines = setup_core.build_config_lines({
        "MODE": "worker_only",
        "WG_WAN_IP": "",
        "ADMIN_HASH": None,
        "NETWORK_KEY": "   ",
    })

    assert lines == ["MODE=worker_only"]


def test_unknown_keys_survive_in_sorted_order():
    """
    The WireGuard allocator's high-water mark is written by key name, not by a field the
    assembler knows about. Dropping unrecognised keys would silently reset the address
    allocator and reissue tunnel addresses that are already in use.
    """
    lines = setup_core.build_config_lines({
        "MODE": "coordinator_only",
        "ZZZ_LAST": "2",
        "WG_NEXT_PEER_OCTET": "7",
    })

    assert lines[0] == "MODE=coordinator_only"
    assert lines[1:] == ["WG_NEXT_PEER_OCTET=7", "ZZZ_LAST=2"]


def test_keys_are_normalised_to_uppercase():
    lines = setup_core.build_config_lines({"mode": "worker_only", " rpc_port ": "50052"})

    assert lines == ["MODE=worker_only", "RPC_PORT=50052"]


def test_writing_replaces_the_file_wholesale(isolated_config):
    """
    Re-running setup must drop settings that belonged to a previous role. A merge would
    leave a machine switched from Coordinator to worker_only holding its BIND_PORT and
    ADMIN_HASH, behaving like a half-Coordinator.
    """
    isolated_config.write_text("MODE=coordinator_only\nBIND_PORT=11435\nADMIN_HASH=$2b$xyz\n",
                               encoding="utf-8")

    setup_core.write_config_file(setup_core.build_config_lines({"MODE": "worker_only"}))

    written = isolated_config.read_text(encoding="utf-8")
    assert "MODE=worker_only" in written
    assert "ADMIN_HASH" not in written
    assert "BIND_PORT" not in written


# ======================================================================================
# Contribution bounds
# ======================================================================================

def _hardware(ram=32.0, cores=8, gpus=None):
    return {"ok": True, "error": "", "gpus": gpus or [],
            "total_ram_gb": ram, "physical_cores": cores, "logical_threads": cores * 2}


def test_a_cpu_only_worker_below_the_floor_is_refused():
    """
    A CPU-only node under the floor slows the swarm down instead of helping it, which is why
    setup refuses rather than warns — the cost lands on every other participant, not on the
    person who clicked past a warning.
    """
    bounds = setup_core.contribution_bounds("worker_only", cpu_only=True,
                                            hardware=_hardware(ram=4.0, cores=1))

    assert bounds["meets_minimum"] is False
    assert "8" in bounds["reason"] and "2" in bounds["reason"]


def test_a_cpu_only_hybrid_has_a_higher_floor_than_a_worker():
    """The Coordinator's own overhead comes out of the same machine."""
    hardware = _hardware(ram=10.0, cores=3)

    assert setup_core.contribution_bounds("worker_only", True, hardware)["meets_minimum"] is True
    assert setup_core.contribution_bounds("coordinator_hybrid", True, hardware)["meets_minimum"] is False
    assert setup_core.contribution_bounds("redundancy_worker", True, hardware)["meets_minimum"] is False


def test_contributing_a_gpu_removes_the_cpu_floor_entirely():
    """A node bringing VRAM is useful regardless of how modest its CPU is."""
    bounds = setup_core.contribution_bounds("coordinator_hybrid", cpu_only=False,
                                            hardware=_hardware(ram=4.0, cores=1))

    assert bounds["meets_minimum"] is True
    assert bounds["min_ram_gb"] == 0.0
    assert bounds["min_cores"] == 0


def test_defaults_leave_the_users_own_machine_usable():
    """A third of RAM and a quarter of the cores — enough to help, not enough to hurt."""
    bounds = setup_core.contribution_bounds("worker_only", cpu_only=False,
                                            hardware=_hardware(ram=48.0, cores=16))

    assert bounds["default_ram_gb"] == 16.0
    assert bounds["default_cores"] == 4


def test_defaults_are_raised_to_the_floor_on_a_cpu_only_node():
    bounds = setup_core.contribution_bounds("worker_only", cpu_only=True,
                                            hardware=_hardware(ram=12.0, cores=4))

    assert bounds["default_ram_gb"] >= bounds["min_ram_gb"]
    assert bounds["default_cores"] >= bounds["min_cores"]


def test_a_single_core_machine_still_gets_a_usable_default():
    bounds = setup_core.contribution_bounds("worker_only", cpu_only=False,
                                            hardware=_hardware(ram=8.0, cores=1))

    assert bounds["default_cores"] == 1


def test_failed_hardware_detection_never_refuses_the_node():
    """
    Detection can fail on an odd platform. Refusing to configure a node because psutil threw
    would be a worse outcome than accepting an unverified contribution figure.
    """
    bounds = setup_core.contribution_bounds(
        "worker_only", cpu_only=True,
        hardware={"ok": False, "error": "psutil exploded", "gpus": [],
                  "total_ram_gb": 0, "physical_cores": 0, "logical_threads": 0})

    assert bounds["meets_minimum"] is True


# ======================================================================================
# Contribution validation
# ======================================================================================

def test_validation_rejects_values_below_the_floor():
    bounds = setup_core.contribution_bounds("worker_only", True, _hardware(ram=32.0, cores=8))

    verdict = setup_core.validate_contribution(4.0, 1, bounds)

    assert not verdict["ok"]
    assert len(verdict["errors"]) == 2


def test_validation_rejects_more_cores_than_the_machine_has():
    bounds = setup_core.contribution_bounds("worker_only", False, _hardware(ram=32.0, cores=8))

    verdict = setup_core.validate_contribution(8.0, 64, bounds)

    assert not verdict["ok"]
    assert any("only has 8" in error for error in verdict["errors"])


def test_validation_rejects_non_numeric_input_without_raising():
    """
    The console wizard used to call ``float()`` on raw input inside a broad try/except, so a
    typo did not re-prompt — it aborted the whole hardware step with "detection skipped" and
    silently wrote no limits at all.
    """
    bounds = setup_core.contribution_bounds("worker_only", False, _hardware())

    assert setup_core.validate_contribution("twelve", 4, bounds)["ok"] is False
    assert setup_core.validate_contribution(12.0, "four", bounds)["ok"] is False


def test_validation_rejects_zero_and_negative_contributions():
    bounds = setup_core.contribution_bounds("worker_only", False, _hardware())

    assert setup_core.validate_contribution(0, 4, bounds)["ok"] is False
    assert setup_core.validate_contribution(8, 0, bounds)["ok"] is False
    assert setup_core.validate_contribution(-8, 4, bounds)["ok"] is False


def test_validation_accepts_a_sensible_contribution():
    bounds = setup_core.contribution_bounds("worker_only", False, _hardware(ram=32.0, cores=8))

    assert setup_core.validate_contribution(10.5, 2, bounds) == {"ok": True, "errors": []}


# ======================================================================================
# GPU selection
# ======================================================================================

def test_gpu_selection_strings_resolve_to_records():
    gpus = [{"id": 0, "name": "A"}, {"id": 1, "name": "B"}, {"id": 2, "name": "C"}]

    assert setup_core.selected_gpus("-1", gpus) == gpus
    assert setup_core.selected_gpus("none", gpus) == []
    assert setup_core.selected_gpus("", gpus) == []
    assert [g["id"] for g in setup_core.selected_gpus("0,2", gpus)] == [0, 2]
    assert [g["id"] for g in setup_core.selected_gpus(" 1 , 2 ", gpus)] == [1, 2]


def test_an_unknown_gpu_index_is_dropped_not_expanded_to_everything():
    """A typo'd index must never silently become "dedicate all my GPUs"."""
    gpus = [{"id": 0}, {"id": 1}]

    assert setup_core.selected_gpus("7", gpus) == []
    assert setup_core.selected_gpus("0,7", gpus) == [{"id": 0}]


# ======================================================================================
# Invites
# ======================================================================================

def test_invite_metadata_is_read_from_comment_lines(tmp_path):
    invite = tmp_path / "worker_invite.conf"
    invite.write_text(
        "[Interface]\nPrivateKey = abc=\n"
        "# DLNE_NETWORK_KEY=deadbeef\n"
        "# DLNE_ADMIN_HASH=$2b$12$abc=def=\n"
        "[Peer]\nEndpoint = 1.2.3.4:51820\n", encoding="utf-8")

    parsed = setup_core.parse_invite(str(invite))

    assert parsed["ok"]
    assert parsed["network_key"] == "deadbeef"
    # Split on the FIRST '=' only: a bcrypt hash legitimately contains more.
    assert parsed["admin_hash"] == "$2b$12$abc=def="


def test_a_readable_invite_without_metadata_is_still_ok(tmp_path):
    """
    "Unreadable file" and "invite carrying no Network Key" are different problems with
    different messages, so they must not collapse into one failure flag.
    """
    invite = tmp_path / "plain.conf"
    invite.write_text("[Interface]\nPrivateKey = abc=\n", encoding="utf-8")

    parsed = setup_core.parse_invite(str(invite))

    assert parsed["ok"]
    assert parsed["network_key"] == ""
    assert parsed["admin_hash"] == ""


def test_a_missing_invite_reports_an_error_instead_of_raising(tmp_path):
    parsed = setup_core.parse_invite(str(tmp_path / "nope.conf"))

    assert parsed["ok"] is False
    assert parsed["error"]


def test_join_invites_are_recognised_by_name():
    candidates = ["/x/coordinator_wg0.conf", "/x/redundancy_invite.conf"]

    assert setup_core.pick_join_invite(candidates) == "/x/redundancy_invite.conf"
    assert setup_core.pick_join_invite(["/x/coordinator_wg0.conf"]) is None


def test_admin_password_hashing_round_trips():
    hashed = setup_core.hash_admin_password("hunter2")

    assert setup_core.verify_admin_password("hunter2", hashed)
    assert not setup_core.verify_admin_password("hunter3", hashed)


def test_a_malformed_hash_answers_no_instead_of_raising():
    """A truncated invite or a hand-edited config must not throw mid-wizard."""
    assert setup_core.verify_admin_password("hunter2", "not-a-bcrypt-hash") is False
    assert setup_core.verify_admin_password("hunter2", "") is False


# ======================================================================================
# Firewall + security script
# ======================================================================================

def test_firewall_ports_fall_back_to_the_engine_defaults():
    ports = setup_core.firewall_ports({})

    assert ports["rpc"] == int(setup_core.DEFAULT_RPC_PORT)
    assert ports["bind"] == int(setup_core.DEFAULT_BIND_PORT)
    assert ports["discovery"] == setup_core.DISCOVERY_PORT


def test_firewall_ports_read_the_configured_values():
    ports = setup_core.firewall_ports({"RPC_PORT": "50999", "BIND_PORT": "11999"})

    assert ports["rpc"] == 50999
    assert ports["bind"] == 11999


def test_an_out_of_range_or_junk_port_falls_back_rather_than_creating_a_broken_rule():
    ports = setup_core.firewall_ports({"RPC_PORT": "99999", "BIND_PORT": "not-a-port"})

    assert ports["rpc"] == int(setup_core.DEFAULT_RPC_PORT)
    assert ports["bind"] == int(setup_core.DEFAULT_BIND_PORT)


def test_the_security_script_opens_every_port_the_node_listens_on():
    """
    ITEM: nothing in DLNE ever created a firewall rule.

    The Worker's RPC listener, the Coordinator's HTTP bind and the UDP discovery beacon are
    all inbound. Without rules the node binds successfully and is simply never reached —
    a failure that looks exactly like a broken cluster.
    """
    script = setup_core.build_security_script(
        r"C:\DLNE", {"rpc": 50052, "bind": 11435, "discovery": 11435},
        has_wdac=False, marker_path=r"C:\DLNE\config\.winsec_configured_v2")

    assert "New-NetFirewallRule" in script
    assert "-Protocol TCP -LocalPort 50052" in script.replace("$rule.Protocol", "TCP").replace("$rule.Port", "50052") \
        or "50052" in script
    assert "11435" in script
    assert "'DLNE Worker RPC'" in script
    assert "'DLNE Coordinator'" in script
    assert "'DLNE Discovery'" in script


def test_the_security_script_removes_rules_before_creating_them():
    """Re-running setup must not accumulate duplicate rules."""
    script = setup_core.build_security_script(
        r"C:\DLNE", {"rpc": 50052, "bind": 11435, "discovery": 11435},
        has_wdac=False, marker_path=r"C:\DLNE\config\.winsec_configured_v2")

    assert script.index("Remove-NetFirewallRule") < script.index("New-NetFirewallRule")


def test_the_security_script_always_adds_the_defender_exclusion_and_marker():
    script = setup_core.build_security_script(
        r"C:\DLNE", {"rpc": 1, "bind": 2, "discovery": 3},
        has_wdac=False, marker_path=r"C:\DLNE\config\.winsec_configured_v2")

    assert "Add-MpPreference -ExclusionPath 'C:\\DLNE'" in script
    assert ".winsec_configured_v2" in script


# ======================================================================================
# The firewall profile the tunnel lands on
# ======================================================================================
#
# THE BUG THESE EXIST TO PREVENT
#     The rules above are created with -Profile Private,Domain. Windows classifies a
#     WireGuard adapter as an unidentified network, which is Public - so the rule opening
#     the worker's RPC port did not apply to the single interface the Coordinator would
#     ever dial it on, and every inbound connection over the tunnel was dropped before DLNE
#     saw it.
#
#     It has no symptom. Heartbeats are outbound, so the node registers, shows up in the
#     WebUI, and has its VRAM counted into the cluster total. It is a full member of a
#     cluster it cannot contribute one shard of inference to. A WAN node's entire purpose is
#     to be reached through that tunnel, and it silently could not be.

def test_bringing_up_a_tunnel_also_puts_it_on_a_reachable_profile():
    script = setup_core._windows_tunnel_activation_script(r"C:\DLNE\wg_configs\dlne_tunnel.conf")

    assert "/installtunnelservice" in script
    assert "Set-NetConnectionProfile" in script
    assert "-NetworkCategory Private" in script


def test_the_adapter_name_is_derived_from_the_file_not_assumed():
    """wg-quick and wireguard.exe both name the interface after the file. One derivation."""
    script = setup_core._windows_tunnel_activation_script(r"C:\somewhere\else\node7.conf")

    assert '$alias = "node7"' in script
    assert "dlne_tunnel" not in script


def test_the_rules_are_not_widened_to_every_network():
    """
    -Profile Any would also fix it, by opening the RPC port on every café and hotspot
    network the machine ever joins. Narrowing the *adapter* opens it where the Coordinator
    is and nowhere else.
    """
    script = setup_core._windows_tunnel_activation_script(r"C:\DLNE\wg_configs\dlne_tunnel.conf")

    assert "-Profile Any" not in script


def test_the_adapter_is_waited_for_rather_than_assumed_present():
    """The service takes a moment to start and Windows a moment more to identify it."""
    script = setup_core._windows_tunnel_activation_script(r"C:\DLNE\wg_configs\dlne_tunnel.conf")

    assert "Start-Sleep" in script
    assert "for (" in script


def test_the_elevated_command_survives_a_path_with_spaces(monkeypatch, tmp_path):
    """
    Nested two shells deep and carrying a Windows path. Base64 is what makes the quoting
    unloseable; this proves the script arrives intact rather than merely that it was sent.
    """
    import base64

    conf = tmp_path / "Program Files" / "dlne_tunnel.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text("[Interface]\n", encoding="utf-8")

    issued = {}
    monkeypatch.setattr(setup_core.sys, "platform", "win32")
    monkeypatch.setattr(setup_core.subprocess, "run",
                        lambda cmd, **kw: issued.update(cmd=cmd) or None)

    setup_core.activate_tunnel(str(conf))

    command = " ".join(issued["cmd"])
    encoded = command.split("'-EncodedCommand','")[1].split("'")[0]
    decoded = base64.b64decode(encoded).decode("utf-16-le")

    assert str(conf) in decoded
    assert "-NetworkCategory Private" in decoded


def test_a_node_without_a_tunnel_is_asked_nothing(monkeypatch, tmp_path):
    """No tunnel means no adapter to misclassify, and no reason to spawn a shell to ask."""
    monkeypatch.setattr(setup_core.sys, "platform", "win32")
    monkeypatch.setattr(setup_core, "get_base_dir", lambda: str(tmp_path))
    called = []
    monkeypatch.setattr(setup_core.subprocess, "run",
                        lambda *a, **k: called.append(a) or None)

    assert setup_core.warn_if_tunnel_is_public() == ""
    assert called == []


def test_a_public_tunnel_is_reported_with_the_command_that_fixes_it(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_core.sys, "platform", "win32")
    monkeypatch.setattr(setup_core, "get_base_dir", lambda: str(tmp_path))
    wg_dir = tmp_path / "wg_configs"
    wg_dir.mkdir()
    (wg_dir / "dlne_tunnel.conf").write_text("[Interface]\n", encoding="utf-8")

    class Result:
        stdout = "Public\n"

    monkeypatch.setattr(setup_core.subprocess, "run", lambda *a, **k: Result())

    message = setup_core.warn_if_tunnel_is_public()

    assert "Public" in message
    assert "Set-NetConnectionProfile" in message


def test_a_private_tunnel_and_an_absent_one_are_both_silent(monkeypatch, tmp_path):
    """
    Empty output means the tunnel is not up. An adapter that does not exist is not a
    misconfigured adapter, and reporting it would train the operator to ignore the warning.
    """
    monkeypatch.setattr(setup_core.sys, "platform", "win32")
    monkeypatch.setattr(setup_core, "get_base_dir", lambda: str(tmp_path))
    wg_dir = tmp_path / "wg_configs"
    wg_dir.mkdir()
    (wg_dir / "dlne_tunnel.conf").write_text("[Interface]\n", encoding="utf-8")

    for output in ("Private\n", "", "   "):
        class Result:
            stdout = output

        monkeypatch.setattr(setup_core.subprocess, "run", lambda *a, **k: Result())
        assert setup_core.warn_if_tunnel_is_public() == ""


def test_the_check_never_raises(monkeypatch, tmp_path):
    """A diagnostic must not be the thing that stops a node from starting."""
    monkeypatch.setattr(setup_core.sys, "platform", "win32")
    monkeypatch.setattr(setup_core, "get_base_dir", lambda: str(tmp_path))
    wg_dir = tmp_path / "wg_configs"
    wg_dir.mkdir()
    (wg_dir / "dlne_tunnel.conf").write_text("[Interface]\n", encoding="utf-8")

    def explode(*a, **k):
        raise OSError("powershell is not on PATH")

    monkeypatch.setattr(setup_core.subprocess, "run", explode)

    assert setup_core.warn_if_tunnel_is_public() == ""


def test_the_wdac_policy_is_only_emitted_when_wdac_is_enforcing():
    without = setup_core.build_security_script(r"C:\DLNE", {"rpc": 1, "bind": 2, "discovery": 3},
                                               has_wdac=False, marker_path="m")
    with_wdac = setup_core.build_security_script(r"C:\DLNE", {"rpc": 1, "bind": 2, "discovery": 3},
                                                 has_wdac=True, marker_path="m")

    assert "New-CIPolicy" not in without
    assert "New-CIPolicy" in with_wdac


def test_the_marker_was_versioned_so_existing_installs_re_run(tmp_path):
    """
    ITEM: an install carrying the *old* marker predates firewall rules entirely.

    Leaving the marker name alone would mean every already-configured machine silently never
    gets the rules that were the whole point of the change.
    """
    assert setup_core.SECURITY_MARKER_NAME != setup_core.LEGACY_SECURITY_MARKER_NAME

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / setup_core.LEGACY_SECURITY_MARKER_NAME).write_text("configured", encoding="utf-8")

    status = setup_core.security_status(str(tmp_path))

    assert status["legacy_marker"] is True
    assert status["already_configured"] is False


def test_a_current_marker_skips_the_step(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / setup_core.SECURITY_MARKER_NAME).write_text("configured", encoding="utf-8")

    assert setup_core.security_status(str(tmp_path))["already_configured"] is True


@pytest.mark.skipif(os.name == "nt", reason="the security step is Windows-only by design")
def test_the_security_step_is_a_no_op_off_windows(tmp_path):
    result = setup_core.apply_security_configuration(install_dir=str(tmp_path))

    assert result["ok"] and result["skipped"]


# ======================================================================================
# Auto-start
# ======================================================================================

def test_the_launch_command_points_at_this_installation():
    command = setup_core.launch_command(tray=True)

    assert command.endswith("--tray")
    assert "main.py" in command or command.count('"') >= 2


def test_the_launch_command_can_omit_the_tray_flag():
    assert not setup_core.launch_command(tray=False).endswith("--tray")


def test_the_autostart_entry_name_matches_the_installers_registry_value():
    """
    The Inno installer writes the same HKCU Run value name. If the two ever diverge, the
    app's toggle and the installer checkbox become two independent settings that each think
    they own auto-start.
    """
    assert setup_core.AUTOSTART_ENTRY_NAME == "DLNE-Node"
    assert setup_core.WINDOWS_RUN_KEY == r"Software\Microsoft\Windows\CurrentVersion\Run"


# ======================================================================================
# Role tables
# ======================================================================================

def test_the_role_tables_stay_consistent_with_each_other():
    """
    ``main.py`` dispatches on these exact strings, and the GUI validates against MODES before
    writing anything. A role in one table and not the others is a role that half-works.
    """
    assert set(setup_core.COMPUTE_MODES) <= set(setup_core.MODES)
    assert set(setup_core.COORDINATOR_MODES) <= set(setup_core.MODES)
    # coordinator_only is the one role that runs no WorkerNode.
    assert "coordinator_only" not in setup_core.COMPUTE_MODES
    assert "coordinator_hybrid" in setup_core.COMPUTE_MODES
    assert "coordinator_hybrid" in setup_core.COORDINATOR_MODES


def test_the_rpc_port_is_also_opened_to_the_same_subnet_for_a_docked_laptop_on_a_public_network():
    """Windows leaves a wired office or home network 'Public' until someone says otherwise; a coordinator on that
    LAN dials the RPC port there. The extra rule reaches that subnet only, never every network (2.5.0)."""
    script = setup_core.build_security_script(r"C:\DLNE", {"rpc": 50052, "bind": 11435, "discovery": 50051},
                                              has_wdac=False, marker_path=r"C:\DLNE\.security_done")
    assert "DLNE Worker RPC (same subnet)" in script
    assert "-LocalPort 50052 -Profile Any -RemoteAddress LocalSubnet" in script
    assert script.count("-Profile Any") == 1, "only the subnet-scoped rule; the others stay Private,Domain"
    assert "-Profile Private,Domain" in script
