"""config.txt editing (the live path for slider changes) and the worker's output markers."""

import os

from dlne_donate import configfile, markers


# ---- config.txt -------------------------------------------------------------------------------------

def test_set_config_value_edits_in_place_and_keeps_everything_else():
    text = "# a comment\nCOORDINATOR_URL=x\n\nmax_vram_gb = 4\nOTHER=1\n"
    out = configfile.set_config_value(text, "MAX_VRAM_GB", "8")
    assert out == "# a comment\nCOORDINATOR_URL=x\n\nmax_vram_gb=8\nOTHER=1\n"


def test_set_config_value_drops_a_stale_duplicate_and_appends_a_missing_key():
    assert configfile.set_config_value("A=1\nMAX_RAM_GB=1\nMAX_RAM_GB=2\n", "max_ram_gb", "9") == "A=1\nMAX_RAM_GB=9\n"
    assert configfile.set_config_value("A=1\n", "NEW", "2") == "A=1\nNEW=2\n"
    assert configfile.set_config_value("", "NEW", "2") == "NEW=2\n"


def test_set_config_value_keeps_crlf_and_removes_a_byte_order_mark():
    out = configfile.set_config_value("﻿A=1\r\nB=2\r\n", "B", "3")
    assert out == "A=1\r\nB=3\r\n"


def test_update_config_writes_atomically_and_reports_change(tmp_path):
    path = tmp_path / "config" / "config.txt"
    assert configfile.update_config(str(path), {"MAX_VRAM_GB": "8", "NETWORK_KEY": "abc"})
    assert configfile.read_config(str(path)) == {"MAX_VRAM_GB": "8", "NETWORK_KEY": "abc"}
    assert not configfile.update_config(str(path), {"MAX_VRAM_GB": "8"})                 # nothing changed
    assert configfile.update_config(str(path), {"MAX_VRAM_GB": "6"})
    assert configfile.read_config(str(path))["MAX_VRAM_GB"] == "6"
    assert [p for p in os.listdir(path.parent) if p.endswith(".tmp")] == []
    if os.name == "posix":
        assert (path.stat().st_mode & 0o077) == 0       # the file is the owner's alone


def test_the_engines_own_parser_reads_what_we_write(tmp_path):
    """Round trip through engine.config's parser (the one the worker re-reads the file with)."""
    from dlne_donate import bundled
    cfg = bundled.import_snapshot("config")
    path = tmp_path / "config.txt"
    configfile.update_config(str(path), {"MAX_VRAM_GB": "7.5", "ALLOWED_GPU_INDICES": "none"})
    parsed = cfg._parse_config_lines(path.read_text().splitlines())
    assert parsed["MAX_VRAM_GB"] == "7.5" and parsed["ALLOWED_GPU_INDICES"] == "none"


def test_format_gb():
    assert configfile.format_gb(16.0) == "16" and configfile.format_gb(12.5) == "12.5"


# ---- markers ----------------------------------------------------------------------------------------

def test_role_with_detail():
    line = 'noise\nDLNE_ROLE: coding {"model": "Qwen3.6-35B-A3B-UD-IQ3_S.gguf", "tier": "qwen36-a3b", "slots": 2, "trust_id": "' + "a" * 32 + '"}\n'
    role = markers.parse_role(line)
    assert role.role == "coding" and role.slots == 2 and role.trust_id == "a" * 32 and role.tier == "qwen36-a3b"
    assert markers.role_title(role) == "Coding member · Qwen3.6-35B-A3B-UD-IQ3_S"


def test_role_alone_last_one_wins_and_junk_is_ignored():
    assert markers.parse_role("DLNE_ROLE: rpc\r\nDLNE_ROLE: app\r\n").role == "app"
    assert markers.parse_role("DLNE_ROLE: nonsense\n") is None
    assert markers.parse_role('DLNE_ROLE: coding {not json}\n').role == "coding"
    bad = markers.parse_role('DLNE_ROLE: coding {"slots": 999, "trust_id": "xyz", "model": 5}\n')
    assert bad.role == "coding" and bad.slots == 0 and bad.trust_id == "" and bad.model == ""


def test_also_refusal_and_clock():
    assert markers.parse_also("DLNE_ALSO: app\n") is True
    assert markers.parse_also("DLNE_ALSO: app\nDLNE_ALSO: none\n") is False
    assert markers.parse_also("nothing") is None
    assert markers.parse_refusal("x\nDLNE_REFUSAL: below the minimum\n") == "below the minimum"
    assert len(markers.parse_refusal("DLNE_REFUSAL: " + "x" * 1000)) == 400
    assert markers.parse_clock('DLNE_CLOCK: {"offset_seconds": -18000}\n') == -18000
    assert markers.parse_clock("DLNE_CLOCK: junk\n") is None
    assert "5.0 hours ahead" in markers.clock_notice(-18000) and "behind" in markers.clock_notice(120)


def test_a_line_split_across_two_chunks_is_read_when_it_is_whole():
    reader = markers.OutputReader()
    assert reader.feed("starting\nDLNE_RO") == {}
    changed = reader.feed('LE: coding {"slots": 2}\r\nmore')
    assert changed["role"].role == "coding" and reader.role.slots == 2
    assert reader.feed("LE: x\n") == {}                  # the unfinished "more" + "LE: x" was not a marker


def test_reader_reports_each_change_once():
    reader = markers.OutputReader()
    assert "role" in reader.feed("DLNE_ROLE: rpc\n")
    assert reader.feed("DLNE_ROLE: rpc\n") == {}
    assert reader.feed("DLNE_ALSO: app\n") == {"also": True}
    assert "heartbeat" in reader.feed("[worker] Heartbeat OK\n")
    assert "refusal" in reader.feed("DLNE_REFUSAL: no\n")


def test_reader_notices_a_missing_visual_cpp_runtime_once():
    reader = markers.OutputReader()
    assert reader.feed("DLNE_NEEDS: vcredist\n") == {"vcredist": True}
    assert reader.feed("DLNE_NEEDS: vcredist\n") == {}


REAL_LOG = """\
[10/26/26 19:33:39] INFO     Downloading Qwen3 4B Instruct 2507 for the CPU pool (2.5 GB, once)...
[10/26/26 19:33:41] INFO       Qwen3 4B Instruct 2507: 10%
                    INFO       Qwen3 4B Instruct 2507: 40%
[10/26/26 19:33:52] INFO     Qwen3 4B Instruct 2507 downloaded and verified.
[10/26/26 19:33:56] INFO     Serving Qwen3 4B Instruct 2507 for the phone app.
"""


def test_the_workers_download_and_serving_lines_become_an_activity():
    lines = REAL_LOG.splitlines()
    assert markers.parse_activity(lines[0]) == markers.Activity("Downloading Qwen3 4B Instruct 2507 (2.5 GB, once)", 0.0)
    assert markers.parse_activity(lines[1]).fraction == 0.1
    assert markers.parse_activity(lines[2]) == markers.Activity("Downloading Qwen3 4B Instruct 2507: 40%", 0.4)
    assert "downloaded and checked" in markers.parse_activity(lines[3]).text
    assert markers.parse_activity(lines[4]) == markers.Activity("Serving Qwen3 4B Instruct 2507", None)
    assert markers.parse_activity(REAL_LOG).text == "Serving Qwen3 4B Instruct 2507"        # the last one wins
    assert markers.parse_activity("[x] INFO     Heartbeat OK | Status: Active") is None
    assert markers.parse_activity("INFO     progress: 250%") is None


def test_the_coding_member_wording_is_read_too():
    assert markers.parse_activity("INFO     Serving Qwen3.6 35B-A3B to the coding pool.").text == "Serving Qwen3.6 35B-A3B"
    assert markers.parse_activity("INFO     Downloading Qwen3.6 35B-A3B for the coding pool (13.7 GB, once)...").fraction == 0.0


def test_take_complete_lines_caps_a_line_that_never_ends():
    complete, rest = markers.take_complete_lines("", "x" * 200000)
    assert complete == "" and len(rest) == 65536


def test_dock_and_yield_lines_and_the_members_figures():
    dock = markers.parse_dock('DLNE_DOCK: {"link": "wired", "power": "ac", "docked": true}\n')
    assert dock == markers.Dock(link="wired", power="ac", docked=True)
    assert markers.dock_line(dock).startswith("Docked (wired and plugged in)")
    undocked = markers.parse_dock('DLNE_DOCK: {"link": "wifi", "power": "battery", "docked": false}\n')
    assert markers.dock_line(undocked) == ("Not docked (on Wi-Fi and battery): your GPU runs a whole model by itself "
                                           "until it is docked again.")
    assert markers.parse_dock('DLNE_DOCK: {"power": "ac"}\n') == markers.Dock(power="ac")
    assert markers.dock_line(markers.Dock(power="ac")) == "", "nothing claimed while a fact is unknown"
    assert markers.parse_dock("DLNE_DOCK: junk\n") is None
    y = markers.parse_yield('DLNE_YIELD: {"yielding": true, "why": "gpu"}\n')
    assert y == markers.Yield(yielding=True, why="gpu")
    assert markers.yield_line(y) == "Stepping aside for your own GPU use: the swarm gets the GPU back once you are done."
    assert markers.yield_line(markers.parse_yield('DLNE_YIELD: {"yielding": false, "why": ""}\n')) == ""
    assert markers.parse_yield('DLNE_YIELD: {"yielding": "yes"}\n') is None
    role = markers.parse_role('DLNE_ROLE: coding {"slots": 1, "context": 40960, "decode_tps": 24.26, "prefill_tps": 310}\n')
    assert (role.context, role.decode_tps, role.prefill_tps) == (40960, 24.3, 310.0)
    assert markers.role_meaning(role).endswith("about 1 at a time (40,960 tokens each, 24.3 tok/s).")
    reader = markers.OutputReader()
    assert "dock" in reader.feed('DLNE_DOCK: {"docked": true}\n') and reader.feed('DLNE_DOCK: {"docked": true}\n') == {}
    assert reader.feed('DLNE_YIELD: {"yielding": true, "why": "cpu"}\n')["yield"].why == "cpu"
