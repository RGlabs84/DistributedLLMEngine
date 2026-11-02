"""
Tests for takeover leases (2.7.1, engine/takeover.py, docs/takeover-contract.md): the request, the planner (each kind of
card), the overlay on the limits, the lease file and its staleness, the CPU-mask surgery, and the controller (grant,
idle gate, one lease at a time, heartbeat, ttl, release and restore, a failed grant). The HTTP face is
tests/test_takeover_api.py.

Nothing touches the machine: the controller gets a ``Host`` of stand-ins (tests/takeover_fakes.py), the unit's memory
limits are a fake, and the config and the lease file live in a temporary installation root.
"""

import asyncio
import json
import os

import pytest

import engine.config as engine_config
from engine import takeover
from engine.config import get_plain_worker_limits, get_worker_limits
from tests.takeover_fakes import DEFAULT_CONFIG, MEDIA, POOL_A, POOL_B, box, engine_facts      # noqa: F401  (box is a fixture)

GIB = 1024 ** 3


def configure(box, text):
    box.config_path.write_text(text)
    engine_config._cached_stamp = None          # the stat cache: a rewrite of the same size must still be read


def plan(box, lease_active=False, memory=True, **body):
    body.setdefault("gpus", [])
    body.setdefault("holder", "DLNE-Sec on portoBOX")
    return takeover.make_plan(takeover.parse_request(body), limits=get_plain_worker_limits(), gpus=box.gpus,
                              facts=box.facts, memory_now=box.mem.read() if memory else None,
                              system_ram_gb=box.system_ram_gb, lease_active=lease_active)


def lease_file(box, state="active", overlay=None, heartbeat_age=0.0, ttl=14400, since_age=0.0, memory=None):
    now = box.clock()
    lease = {"version": 1, "lease_id": "tk_" + "ab" * 16, "state": state, "holder": "DLNE-Sec on portoBOX", "reason": "",
             "granted": {"gpus": [POOL_A], "cpu_cores": [], "ram_gb": 0.0}, "keep": {"coding_lanes": 1, "coding_context": 32768},
             "overlay": overlay or {}, "cards": [], "engine_restart": False, "memory": memory or {"applied": False},
             "pool_before": {"vram_gb": 16.0, "sysram_gb": 32.0}, "dlne_now": {},
             "since": now - since_age, "expires_at": now - since_age + ttl, "last_heartbeat": now - heartbeat_age}
    takeover.save_lease(lease)
    return lease


# ======================================================================================
# The request
# ======================================================================================

def test_a_request_with_defaults_asks_one_lane_of_32k_for_four_hours():
    req = takeover.parse_request({"gpus": ["0000:03:00.0"], "holder": "DLNE-Sec on portoBOX", "reason": "security scan session"})
    assert (req.coding_lanes, req.coding_context, req.ttl_seconds) == (1, 32768, 14400)
    assert (req.cpu_cores, req.ram_gb, req.holder, req.reason) == ([], 0.0, "DLNE-Sec on portoBOX", "security scan session")


def test_a_request_is_read_as_the_contract_writes_it():
    body = {"gpus": ["0000:03:00.0", "0000:0C:00.0", "0000:03:00.0"], "cpu_cores": [5, 4, 4], "ram_gb": 6,
            "keep": {"coding_lanes": 2, "coding_context": 65536}, "ttl_seconds": 600, "holder": "h", "reason": "r"}
    req = takeover.parse_request(body)
    assert req.gpus == ["0000:03:00.0", "0000:0c:00.0"], "lower-cased, each once, in the order written"
    assert (req.cpu_cores, req.ram_gb, req.coding_lanes, req.coding_context, req.ttl_seconds) == ([4, 5], 6.0, 2, 65536, 600)


@pytest.mark.parametrize("body", [
    [], "x", {}, {"gpus": "0000:03:00.0"}, {"gpus": [3]}, {"gpus": ["0000:03:00.0"], "cpu_cores": "4"},
    {"cpu_cores": [-1]}, {"cpu_cores": [1.5]}, {"cpu_cores": [True]}, {"ram_gb": -1}, {"ram_gb": "8"}, {"ram_gb": float("nan")},
    {"gpus": ["a:b"], "keep": []}, {"gpus": ["a:b"], "keep": {"coding_lanes": 0}}, {"gpus": ["a:b"], "keep": {"coding_lanes": 99}},
    {"gpus": ["a:b"], "keep": {"coding_context": 100}}, {"gpus": ["a:b"], "ttl_seconds": 5}, {"gpus": ["a:b"], "ttl_seconds": 10 ** 7},
    {"gpus": ["a:b"], "ttl_seconds": True}])
def test_a_request_that_is_not_the_contracts_shape_is_a_400_with_a_code(body):
    with pytest.raises(takeover.TakeoverError) as caught:
        takeover.parse_request(body)
    assert (caught.value.status, caught.value.code) == (400, "takeover_bad_request") and caught.value.detail


def test_the_holder_is_one_short_clean_line():
    req = takeover.parse_request({"gpus": ["0000:03:00.0"], "holder": "evil\n\x1b[31mname " + "x" * 200})
    assert "\n" not in req.holder and "\x1b" not in req.holder and len(req.holder) <= 80


# ======================================================================================
# The planner: each kind of card
# ======================================================================================

def test_a_card_dlne_does_not_use_is_handed_over_and_nothing_restarts(box):
    result = plan(box, gpus=[POOL_A])

    assert result.allowed and result.problems == []
    assert result.cards == [{"pci": POOL_A, "vulkan_index": 0, "name": "AMD Radeon Pro V520 (BC-160)", "vram_gb": 8.0,
                             "free_vram_gb": 7.98, "action": "handover"}]
    assert result.keep_result["engine_restart"] is False and result.keep_result["engine_on"] == [POOL_B]
    assert result.overlay == {} and result.pause_pictures is False


def test_the_media_card_pauses_pictures_and_moves_the_projector_to_the_cpu(box):
    result = plan(box, gpus=[MEDIA])

    assert result.allowed
    assert [(c["pci"], c["action"]) for c in result.cards] == [(MEDIA, "pictures-pause+vision-cpu")]
    assert result.keep_result["pictures"] == "paused" and result.keep_result["vision"] == "cpu"
    assert result.keep_result["engine_restart"] is True and result.keep_result["engine_on"] == [POOL_B]
    assert result.overlay == {"vision_cpu": True, "pictures_paused": True} and result.pause_pictures is True


def test_the_contracts_own_example_request_gives_its_cards_in_the_requests_order(box):
    result = plan(box, gpus=[POOL_A, MEDIA]).to_dict()

    assert result["allowed"] is True and result["problems"] == [] and result["cpu_cores"] == [] and result["ram_gb"] == 0.0
    assert result["cards"] == [
        {"pci": "0000:03:00.0", "vulkan_index": 0, "name": "AMD Radeon Pro V520 (BC-160)", "vram_gb": 8.0,
         "free_vram_gb": 7.98, "action": "handover"},
        {"pci": "0000:0c:00.0", "vulkan_index": 2, "name": "AMD Radeon RX 580", "vram_gb": 8.0,
         "free_vram_gb": 7.1, "action": "pictures-pause+vision-cpu"}]
    assert set(result["keep_result"]) == {"engine_on", "coding_lanes", "coding_context", "pictures", "vision", "engine_restart"}
    assert [c["pci"] for c in plan(box, gpus=[MEDIA, POOL_A]).cards] == [MEDIA, POOL_A]


def test_a_cards_free_vram_is_read_live_from_sysfs_when_it_is_there(box, tmp_path):
    folder = tmp_path / "pci" / POOL_A
    folder.mkdir(parents=True)
    (folder / "mem_info_vram_total").write_text(str(8 * GIB))
    (folder / "mem_info_vram_used").write_text(str(2 * GIB))

    assert plan(box, gpus=[POOL_A]).cards[0]["free_vram_gb"] == 6.0


def test_pictures_that_are_already_paused_stay_paused_and_need_no_pause(box):
    configure(box, DEFAULT_CONFIG.replace("IMAGE_GEN_ENABLED=true", "IMAGE_GEN_ENABLED=paused"))
    result = plan(box, gpus=[MEDIA])

    assert result.keep_result["pictures"] == "paused" and result.pause_pictures is False
    assert "pictures_paused" not in result.overlay


def test_pictures_that_are_off_stay_off_and_with_no_projector_nothing_restarts(box):
    configure(box, f"ALLOWED_GPU_INDICES={POOL_B}\nMEDIA_GPU={MEDIA}\n")
    result = plan(box, gpus=[MEDIA])

    assert result.keep_result["pictures"] == "off" and result.keep_result["vision"] == "off"
    assert result.keep_result["engine_restart"] is False and result.overlay == {}
    assert result.cards[0]["action"] == "pictures-pause+vision-cpu", "the contract's name for the media card"


def test_an_engine_card_with_another_left_moves_the_engine_to_it(box):
    configure(box, DEFAULT_CONFIG.replace(f"ALLOWED_GPU_INDICES={POOL_B}", f"ALLOWED_GPU_INDICES={POOL_A},{POOL_B}"))
    result = plan(box, gpus=[POOL_A], keep={"coding_lanes": 1, "coding_context": 32768})

    assert result.allowed and result.cards[0]["action"] == "engine-moves"
    assert result.overlay == {"allowed_gpu_indices": POOL_B, "engine_slots": 1, "engine_context": 0,
                              "drop_placement_args": True}
    assert result.keep_result["engine_on"] == [POOL_B] and result.keep_result["coding_lanes"] == 1
    assert result.keep_result["coding_context"] > 32768, "the floor is a floor: the card holds more"
    assert result.keep_result["engine_restart"] is True and result.removed_vram_gb == 8.0


def test_the_last_engine_card_sends_the_engine_to_the_cpu_with_the_floor(box):
    result = plan(box, gpus=[POOL_B], keep={"coding_lanes": 2, "coding_context": 16384})

    assert result.allowed and [c["action"] for c in result.cards] == ["engine-moves"]
    assert result.overlay["allowed_gpu_indices"] == "none"
    assert (result.overlay["engine_slots"], result.overlay["engine_context"]) == (2, 32768)       # lanes x floor, the total -c
    assert result.overlay["vision_cpu"] is True, "no card is left for the projector either"
    assert result.keep_result == {"engine_on": [], "coding_lanes": 2, "coding_context": 16384, "pictures": "on",
                                  "vision": "cpu", "engine_restart": True}


def test_with_no_allowed_list_the_engine_runs_on_every_discrete_card_but_the_media_card(box):
    configure(box, f"MEDIA_GPU={MEDIA}\n")
    result = plan(box, gpus=[POOL_A])

    assert [c["action"] for c in result.cards] == ["engine-moves"] and result.keep_result["engine_on"] == [POOL_B]
    assert plan(box, gpus=["0000:15:00.0"]).cards[0]["action"] == "handover", "an integrated GPU is not the engine's"


def test_a_media_card_that_is_the_only_allowed_card_is_the_engines(box):
    # TheWall since 2026-11-01: ALLOWED_GPU_INDICES and MEDIA_GPU both the RX 580; the engine runs on it (as
    # inference_manager._engine_devices decides). 2.7.1 left it out and called it a media card: a lease for it paused
    # pictures but left DLNE's engine on the card it handed over (found in the first live plan, 2026-11-02).
    configure(box, DEFAULT_CONFIG.replace(f"ALLOWED_GPU_INDICES={POOL_B}", f"ALLOWED_GPU_INDICES={MEDIA}"))

    bcs = plan(box, gpus=[POOL_A, POOL_B])
    assert [c["action"] for c in bcs.cards] == ["handover", "handover"]
    assert bcs.keep_result["engine_on"] == [MEDIA] and bcs.keep_result["engine_restart"] is False

    rx = plan(box, gpus=[MEDIA])
    assert rx.allowed and [c["action"] for c in rx.cards] == ["engine-moves"], "the engine leaves the card it is on"
    assert rx.overlay["allowed_gpu_indices"] == "none" and rx.overlay["vision_cpu"] is True
    assert rx.keep_result["engine_on"] == [] and rx.keep_result["engine_restart"] is True
    assert rx.keep_result["pictures"] == "paused" and rx.keep_result["vision"] == "cpu"


def test_a_floor_the_cards_left_cannot_hold_is_too_much(box):
    configure(box, DEFAULT_CONFIG.replace(f"ALLOWED_GPU_INDICES={POOL_B}", f"ALLOWED_GPU_INDICES={POOL_A},{POOL_B}"))
    result = plan(box, gpus=[POOL_A], keep={"coding_lanes": 1, "coding_context": 262144})

    assert result.allowed is False and result.problems[0]["code"] == "takeover_too_much"
    assert "262144" in result.problems[0]["detail"] and result.first_error().status == 400


def test_a_model_the_cpu_and_its_ram_cannot_hold_is_too_much(box):
    box.facts = engine_facts(unplaced_weights_gb=40.0)
    result = plan(box, gpus=[POOL_B])

    assert result.allowed is False and result.problems[0]["code"] == "takeover_too_much"
    assert "RAM" in result.problems[0]["detail"]


def test_a_card_that_is_not_here_is_unknown_and_is_not_guessed_from_an_index(box):
    for spec in ("0000:ff:00.0", "1", "nonsense"):
        result = plan(box, gpus=[spec])
        assert result.allowed is False and result.cards == []
        assert result.problems[0]["code"] == "takeover_unknown_gpu" and result.first_error().status == 400
    assert plan(box, gpus=["03:00.0"]).cards[0]["pci"] == POOL_A, "an address without its domain names the same card"


def test_a_lease_already_there_is_a_409_problem(box):
    result = plan(box, gpus=[POOL_A], lease_active=True)
    assert result.allowed is False and (result.problems[0]["code"], result.first_error().status) == ("takeover_active", 409)


def test_with_no_engine_running_nothing_restarts_and_the_floor_is_not_checked(box):
    box.facts = None
    result = plan(box, gpus=[POOL_B])

    assert result.allowed and result.keep_result["engine_restart"] is False
    assert result.overlay["allowed_gpu_indices"] == "none", "the next start builds the engine from the overlay"


# ======================================================================================
# CPU cores and RAM
# ======================================================================================

def test_cores_take_their_smt_siblings_from_the_kernels_topology(tmp_path, monkeypatch):
    monkeypatch.setattr(takeover, "CPU_SYSFS", str(tmp_path))
    for cpu, siblings in ((4, "4,8"), (5, "5-5,9"), (8, "4,8"), (9, "5,9")):
        folder = tmp_path / f"cpu{cpu}" / "topology"
        folder.mkdir(parents=True)
        (folder / "thread_siblings_list").write_text(siblings + "\n")

    assert takeover.cpus_to_free([4, 5], 12) == [4, 5, 8, 9], "not the n+6 rule: the kernel says so"


def test_without_a_topology_core_n_has_cpu_n_plus_6_as_its_sibling(tmp_path, monkeypatch):
    monkeypatch.setattr(takeover, "CPU_SYSFS", str(tmp_path / "nothing"))

    assert takeover.cpus_to_free([4, 5], 12) == [4, 5, 10, 11]
    assert takeover.cpus_to_free([10], 12) == [4, 10], "a sibling named as the core brings its twin"
    assert takeover.cpus_to_free([3], 6) == [3], "a machine with no CPU n+6 has no sibling to take"


def test_cores_come_out_of_the_engines_mask_and_a_thread_count_follows(box):
    args = ["--threads", "4", "--cpu-mask", "0xc3", "-ts", "1,1"]

    assert takeover.args_without_cpus(args, [0, 6], 12) == ["--threads", "2", "--cpu-mask", "0x82", "-ts", "1,1"]
    assert takeover.args_without_cpus(args, [4, 5, 10, 11], 12) == args, "cores the engine never had change nothing"
    assert takeover.args_without_cpus(["-C", "ff", "-Cb", "0xff"], [0], 12) == ["-C", "0xfe", "-Cb", "0xfe"]


def test_an_engine_with_no_mask_gets_one_for_every_other_cpu(box):
    assert takeover.args_without_cpus([], [4, 5, 10, 11], 12) == ["--cpu-mask", "0x3cf"]
    assert takeover.mask_without("", [4, 5, 10, 11], 12) == "0x3cf"
    assert takeover.mask_without("0xfff", [0], 12) == "0xffe"


def test_a_mask_with_no_cpu_left_is_refused(box):
    with pytest.raises(ValueError):
        takeover.args_without_cpus(["--cpu-mask", "0xc3"], [0, 1, 6, 7], 12)
    with pytest.raises(ValueError):
        takeover.mask_without("", list(range(12)), 12)


def test_a_request_for_cores_takes_them_and_their_siblings_out_of_the_engine_and_the_media_pin(box):
    result = plan(box, cpu_cores=[4, 5])

    assert result.allowed and result.overlay == {"remove_cpus": [4, 5, 10, 11]}
    assert result.keep_result["engine_restart"] is True and result.cpu_cores == [4, 5]
    limits = takeover.overlay_limits(get_plain_worker_limits(), result.overlay, ncpu=12)
    assert limits["image_cpu_mask"] == "0x3cf" and limits["engine_extra_args"][:4] == ["--threads", "4", "--cpu-mask", "0xc3"]


def test_cores_that_do_not_exist_or_are_all_the_engine_has_are_too_much(box):
    for cores, word in (([12], "no core 12"), ([0, 1], "every core")):
        result = plan(box, cpu_cores=cores)
        assert result.allowed is False and result.problems[0]["code"] == "takeover_too_much"
        assert word in result.problems[0]["detail"]


def test_ram_lowers_the_units_limit_by_that_much_and_keeps_the_old_values(box):
    box.mem.values = {"MemoryMax": "infinity", "MemoryHigh": "infinity"}
    result = plan(box, ram_gb=8)

    assert result.allowed and result.keep_result["engine_restart"] is False
    assert result.memory == {"before": {"MemoryMax": "infinity", "MemoryHigh": "infinity"},
                             "after": {"MemoryMax": str(24 * GIB), "MemoryHigh": str(int(24 * GIB * 0.92))}}


def test_ram_is_lowered_from_the_limit_the_unit_already_has(box):
    box.mem.values = {"MemoryMax": str(20 * GIB), "MemoryHigh": str(10 * GIB)}
    result = plan(box, ram_gb=4)

    assert result.memory["after"] == {"MemoryMax": str(16 * GIB), "MemoryHigh": str(10 * GIB)}, "never raised above what it was"


def test_ram_never_goes_below_what_the_kept_engine_needs(box):
    result = plan(box, ram_gb=31)       # 1 GB would be left; the engine's host side alone needs 3

    assert result.allowed is False and result.problems[0]["code"] == "takeover_too_much" and result.memory is None
    cpu = plan(box, gpus=[POOL_B], ram_gb=24)      # the model (5.8 GB) and its cache on the CPU, in 8 GB: no
    assert cpu.allowed is False and cpu.problems[0]["code"] == "takeover_too_much"


def test_ram_cannot_be_taken_where_there_is_no_unit_to_lower(box):
    result = plan(box, ram_gb=4, memory=False)

    assert result.allowed is False and result.problems[0]["code"] == "takeover_too_much"
    assert "memory limit" in result.problems[0]["detail"]


# ======================================================================================
# The overlay, the lease file and staleness
# ======================================================================================

FULL_OVERLAY = {"allowed_gpu_indices": "none", "engine_slots": 1, "engine_context": 32768, "drop_placement_args": True,
                "remove_cpus": [4, 5, 10, 11], "vision_cpu": True, "pictures_paused": True}


def test_the_overlay_changes_the_limits_it_names_and_nothing_else(box):
    plain = get_plain_worker_limits()
    limits = takeover.overlay_limits(plain, FULL_OVERLAY, ncpu=12)

    assert (limits["allowed_gpu_indices"], limits["engine_slots"], limits["engine_context"]) == ("none", 1, 32768)
    assert limits["engine_extra_args"] == ["--threads", "4", "--cpu-mask", "0xc3"], "-ts is a card placement: dropped"
    assert limits["engine_mmproj_cpu"] is True
    assert (limits["image_gen_enabled"], limits["image_gen_paused"]) == (False, True)
    assert limits["image_cpu_mask"] == "0x3cf"
    assert plain["allowed_gpu_indices"] == POOL_B and plain["engine_mmproj_cpu"] is False, "the input is not touched"
    changed = {k for k in limits if limits[k] != plain[k]}
    assert changed == {"allowed_gpu_indices", "engine_slots", "engine_context", "engine_extra_args", "engine_mmproj_cpu",
                       "image_gen_enabled", "image_gen_paused", "image_cpu_mask"}


def test_get_worker_limits_applies_a_live_lease_on_top_of_config_txt_and_never_writes_it(box):
    before = box.config_path.read_bytes()
    assert get_worker_limits()["allowed_gpu_indices"] == POOL_B

    lease_file(box, overlay=FULL_OVERLAY)
    limits = get_worker_limits()

    assert limits["allowed_gpu_indices"] == "none" and limits["engine_mmproj_cpu"] is True
    assert get_plain_worker_limits()["allowed_gpu_indices"] == POOL_B
    assert box.config_path.read_bytes() == before, "config.txt is exactly as it was"
    takeover.delete_lease()
    assert get_worker_limits()["allowed_gpu_indices"] == POOL_B


@pytest.mark.parametrize("state, heartbeat_age, ttl, applies", [
    ("active", 0, 14400, True), ("active", 44, 14400, True), ("active", 46, 14400, False),
    ("active", 0, 30, False), ("granting", 0, 14400, True), ("granting", 600, 14400, True),
    ("granting", 1300, 14400, False), ("restoring", 0, 14400, False)])
def test_a_stale_or_ended_lease_is_ignored_by_the_limits(box, state, heartbeat_age, ttl, applies):
    lease_file(box, state=state, overlay=FULL_OVERLAY, heartbeat_age=heartbeat_age, ttl=ttl, since_age=31 if ttl == 30 else 0)

    assert (get_worker_limits()["allowed_gpu_indices"] == "none") is applies


def test_the_limits_notice_a_lease_going_stale_without_a_rewrite_of_the_file(box):
    lease_file(box, overlay=FULL_OVERLAY)
    assert get_worker_limits()["engine_mmproj_cpu"] is True
    box.clock.t += 46
    assert get_worker_limits()["engine_mmproj_cpu"] is False


def test_a_damaged_lease_file_applies_nothing(box):
    open(takeover.lease_path(), "w").write("{not json")
    assert takeover.load_lease() is None and get_worker_limits()["allowed_gpu_indices"] == POOL_B


def test_the_lease_file_is_written_whole_or_not_at_all(box):
    lease_file(box)
    folder = os.path.dirname(takeover.lease_path())

    assert takeover.load_lease()["lease_id"] == "tk_" + "ab" * 16
    assert sorted(os.listdir(folder)) == ["config.txt", "takeover.json"], "no staging file is left behind"
    assert os.path.dirname(takeover.lease_path()) == os.path.join(engine_config.get_base_dir(), "config")


def test_a_start_removes_a_stale_lease_and_puts_the_ram_limits_back(box):
    box.mem.values = {"MemoryMax": str(24 * GIB), "MemoryHigh": str(22 * GIB)}
    lease_file(box, heartbeat_age=46, memory={"applied": True, "before": {"MemoryMax": "infinity", "MemoryHigh": "infinity"}})

    note = takeover.purge_on_start()

    assert "stale" in note and takeover.load_lease() is None and not os.path.exists(takeover.lease_path())
    assert box.mem.values == {"MemoryMax": "infinity", "MemoryHigh": "infinity"}


@pytest.mark.parametrize("state, age", [("active", 0), ("granting", 5), ("restoring", 0)])
def test_a_start_keeps_only_a_live_active_lease(box, state, age):
    lease_file(box, state=state, heartbeat_age=age)
    note = takeover.purge_on_start()

    if state == "active":
        assert note is None and takeover.load_lease() is not None
    else:
        assert note and takeover.load_lease() is None, "a grant or a restore of a process that is gone is not carried on"


def test_a_start_with_an_expired_lease_removes_it_and_with_no_lease_does_nothing(box):
    assert takeover.purge_on_start() is None
    lease_file(box, ttl=60, since_age=120)
    assert takeover.purge_on_start() and takeover.load_lease() is None


def test_a_start_removes_a_lease_file_that_cannot_be_read(box):
    open(takeover.lease_path(), "w").write("[]")
    assert takeover.purge_on_start() and not os.path.exists(takeover.lease_path())


def test_the_status_block_names_the_lease_and_not_its_id_or_anything_of_the_machine(box):
    assert takeover.status_block() == {"active": False, "state": None, "holder": None, "since": None, "expires_at": None}
    lease_file(box)
    block = takeover.status_block()

    assert set(block) == {"active", "state", "holder", "since", "expires_at"}
    assert (block["active"], block["state"], block["holder"]) == (True, "active", "DLNE-Sec on portoBOX")
    assert block["since"] == takeover.iso(box.clock()) and block["expires_at"] == takeover.iso(box.clock() + 14400)
    assert "tk_" not in json.dumps(block) and POOL_A not in json.dumps(block)
    box.clock.t += 46
    assert takeover.status_block()["state"] == "restoring", "an active lease gone stale reads as restoring"


def test_who_may_ask_is_the_socket_peer_on_a_lan_range_and_nothing_a_proxy_adds():
    for peer in ("127.0.0.1", "192.168.1.5", "10.8.0.7", "172.16.4.2", "172.31.255.1", "::1", "fd00::1", "::ffff:192.168.1.5"):
        assert takeover.peer_is_lan(peer), peer
    for peer in ("8.8.8.8", "203.0.113.50", "172.32.0.1", "100.64.0.1", "unknown", "", None, "2001:db8::1"):
        assert not takeover.peer_is_lan(peer), peer
    for headers in ({"x-forwarded-for": "1.2.3.4"}, {"X-Forwarded-Host": "x"}, {"Forwarded": "for=1.2.3.4"}, {"Via": "1.1 caddy"},
                    {"X-Real-IP": "1.2.3.4"}, {"x-forwarded-proto": "https"}):
        assert takeover.came_through_a_proxy(headers), headers
    assert not takeover.came_through_a_proxy({"Authorization": "Bearer x", "Content-Type": "application/json"})


# ======================================================================================
# The controller
# ======================================================================================

@pytest.mark.anyio
async def test_a_handover_grant_changes_nothing_that_is_running(box):
    ctrl = box.controller()
    lease = await ctrl.grant({"gpus": [POOL_A], "holder": "DLNE-Sec on portoBOX", "reason": "security scan session"})

    assert lease["active"] is True and lease["state"] == "active" and lease["lease_id"].startswith("tk_")
    assert lease["holder"] == "DLNE-Sec on portoBOX" and lease["heartbeat_every_seconds"] == 15
    assert lease["granted"] == {"gpus": [POOL_A], "cpu_cores": [], "ram_gb": 0}
    assert lease["since"] == takeover.iso(box.clock()) and lease["expires_at"] == takeover.iso(box.clock() + 14400)
    assert [c["action"] for c in lease["cards"]] == ["handover"] and lease["cards"][0]["pci"] == POOL_A
    assert set(lease["dlne_now"]) == {"engine_on", "coding_lanes", "coding_context", "pictures", "vision"}
    assert (box.restarts, box.idle_calls, box.pictures_stopped, box.mem.sets) == ([], 0, 0, []), "no idle wait, nothing touched"
    assert takeover.load_lease()["state"] == "active" and ctrl.get()["lease_id"] == lease["lease_id"]


@pytest.mark.anyio
async def test_a_grant_that_restarts_the_engine_waits_for_an_idle_moment_and_restarts_it_once_on_the_overlay(box):
    start = box.clock()
    box.idle_script = [False, True, True]       # busy, then two idle checks 5 s apart
    ctrl = box.controller()

    lease = await ctrl.grant({"gpus": [POOL_A, MEDIA], "holder": "h"})

    assert box.idle_calls == 3 and box.clock() - start == 10
    assert len(box.restarts) == 1
    restart = box.restarts[0]
    assert restart["engine_mmproj_cpu"] is True and restart["image_gen_paused"] is True, "the overlay was in force"
    assert (restart["reduced"], restart["patient"], restart["expected_pool"]) == (True, False, (16.0, 32.0))
    assert box.pictures_stopped == 1
    assert lease["dlne_now"] == {"engine_on": [POOL_B], "coding_lanes": 1, "coding_context": 90112, "pictures": "paused",
                                 "vision": "cpu"}
    assert [c["action"] for c in lease["cards"]] == ["handover", "pictures-pause+vision-cpu"]


@pytest.mark.anyio
async def test_the_idle_gate_is_two_checks_five_seconds_apart(box):
    start = box.clock()
    box.idle_script = [True, False, True, True]      # idle, then busy at the second look: it starts over
    await box.controller().grant({"gpus": [MEDIA]})

    assert box.idle_calls == 4 and box.clock() - start == 10 and len(box.restarts) == 1


@pytest.mark.anyio
async def test_a_grant_that_never_gets_an_idle_moment_is_a_503_and_nothing_changed(box):
    start = box.clock()
    box.idle_default = False
    ctrl = box.controller()

    with pytest.raises(takeover.TakeoverError) as caught:
        await ctrl.grant({"gpus": [POOL_B], "ram_gb": 2})

    assert (caught.value.status, caught.value.code) == (503, "takeover_busy") and "nothing was changed" in caught.value.detail
    assert 600 <= box.clock() - start <= 610, "ten minutes, no more"
    assert takeover.load_lease() is None and box.restarts == [] and box.mem.sets == [] and box.pictures_stopped == 0
    assert get_worker_limits()["allowed_gpu_indices"] == POOL_B
    assert ctrl.get() == {"active": False}


@pytest.mark.anyio
async def test_only_one_lease_at_a_time(box):
    ctrl = box.controller()
    first = await ctrl.grant({"gpus": [POOL_A]})

    with pytest.raises(takeover.TakeoverError) as caught:
        await ctrl.grant({"gpus": [MEDIA]})
    assert (caught.value.status, caught.value.code) == (409, "takeover_active")
    assert takeover.load_lease()["lease_id"] == first["lease_id"] and box.restarts == []
    assert (await ctrl.plan({"gpus": [MEDIA]}))["problems"][0]["code"] == "takeover_active"


@pytest.mark.anyio
async def test_two_grants_at_once_give_one_lease_and_one_409(box):
    box.idle_script = [False, True, True]
    ctrl = box.controller()

    results = await asyncio.gather(ctrl.grant({"gpus": [MEDIA]}), ctrl.grant({"gpus": [POOL_A]}), return_exceptions=True)

    leases = [r for r in results if isinstance(r, dict)]
    refused = [r for r in results if isinstance(r, takeover.TakeoverError)]
    assert len(leases) == 1 and len(refused) == 1 and refused[0].code == "takeover_active"


@pytest.mark.anyio
async def test_a_refused_request_is_refused_before_anything_waits_or_changes(box):
    ctrl = box.controller()
    with pytest.raises(takeover.TakeoverError) as unknown:
        await ctrl.grant({"gpus": ["0000:ff:00.0"]})
    with pytest.raises(takeover.TakeoverError) as too_much:
        await ctrl.grant({"gpus": [POOL_B], "keep": {"coding_lanes": 8, "coding_context": 1048576}})

    assert (unknown.value.status, unknown.value.code) == (400, "takeover_unknown_gpu")
    assert (too_much.value.status, too_much.value.code) == (400, "takeover_too_much")
    assert box.idle_calls == 0 and box.restarts == [] and takeover.load_lease() is None and ctrl._granting is False


@pytest.mark.anyio
async def test_a_failed_restart_is_rolled_back_to_the_plain_config(box):
    box.results = [{"outcome": "failed", "success": False, "detail": "the engine exited while loading"}]
    ctrl = box.controller()

    with pytest.raises(takeover.TakeoverError) as caught:
        await ctrl.grant({"gpus": [POOL_B], "ram_gb": 2})

    assert (caught.value.status, caught.value.code) == (503, "takeover_failed")
    assert len(box.restarts) == 2
    assert box.restarts[0]["allowed_gpu_indices"] == "none", "the first try ran on the overlay"
    assert box.restarts[1]["allowed_gpu_indices"] == POOL_B and box.restarts[1]["engine_slots"] == 0, "the second on the plain config"
    assert (box.restarts[1]["patient"], box.restarts[1]["reduced"], box.restarts[1]["expected_pool"]) == (True, False, (16.0, 32.0))
    assert takeover.load_lease() is None and ctrl._granting is False
    assert box.mem.values == {"MemoryMax": "infinity", "MemoryHigh": "infinity"} and len(box.mem.sets) == 2


@pytest.mark.anyio
async def test_a_switch_that_was_busy_touched_nothing_so_nothing_is_restarted_back(box):
    box.results = [{"outcome": "busy", "success": False, "detail": "requests were still running after 15 s"}]

    with pytest.raises(takeover.TakeoverError) as caught:
        await box.controller().grant({"gpus": [POOL_B], "ram_gb": 2})

    assert (caught.value.status, caught.value.code) == (503, "takeover_busy")
    assert len(box.restarts) == 1 and takeover.load_lease() is None
    assert box.mem.values == {"MemoryMax": "infinity", "MemoryHigh": "infinity"}


@pytest.mark.anyio
async def test_systemd_refusing_the_memory_limit_ends_the_grant_with_nothing_changed(box):
    box.mem.accepts = False

    with pytest.raises(takeover.TakeoverError) as caught:
        await box.controller().grant({"gpus": [POOL_A], "ram_gb": 4})

    assert caught.value.code == "takeover_failed" and takeover.load_lease() is None and box.restarts == []


@pytest.mark.anyio
async def test_ram_is_lowered_for_the_lease_and_set_back_when_it_ends(box):
    ctrl = box.controller()
    lease = await ctrl.grant({"gpus": [POOL_A], "ram_gb": 8})

    assert lease["granted"]["ram_gb"] == 8
    assert box.mem.values == {"MemoryMax": str(24 * GIB), "MemoryHigh": str(int(24 * GIB * 0.92))}
    assert takeover.load_lease()["memory"]["before"] == {"MemoryMax": "infinity", "MemoryHigh": "infinity"}
    assert box.restarts == [], "RAM alone restarts nothing"

    await ctrl.release(lease["lease_id"])
    await ctrl._restore_task
    assert box.mem.values == {"MemoryMax": "infinity", "MemoryHigh": "infinity"} and takeover.load_lease() is None
    assert box.restarts == []


@pytest.mark.anyio
async def test_the_overlay_never_touches_config_txt_through_a_whole_lease(box):
    before = box.config_path.read_bytes()
    ctrl = box.controller()
    lease = await ctrl.grant({"gpus": [POOL_B, MEDIA], "cpu_cores": [4, 5], "ram_gb": 2})
    assert box.config_path.read_bytes() == before and get_worker_limits()["allowed_gpu_indices"] == "none"

    await ctrl.release(lease["lease_id"])
    await ctrl._restore_task
    assert box.config_path.read_bytes() == before and get_worker_limits()["allowed_gpu_indices"] == POOL_B


@pytest.mark.anyio
async def test_a_heartbeat_keeps_the_lease_and_answers_it(box):
    ctrl = box.controller()
    lease = await ctrl.grant({"gpus": [POOL_A]})
    box.clock.t += 40

    answer = await ctrl.heartbeat(lease["lease_id"])

    assert answer["lease_id"] == lease["lease_id"] and answer["state"] == "active" and answer["active"] is True
    assert takeover.load_lease()["last_heartbeat"] == box.clock()
    box.clock.t += 40
    await ctrl.tick()
    assert takeover.load_lease()["state"] == "active", "40 s since the last heartbeat is not 45"


@pytest.mark.anyio
async def test_a_heartbeat_for_another_lease_is_a_404(box):
    ctrl = box.controller()
    await ctrl.grant({"gpus": [POOL_A]})
    for call in (ctrl.heartbeat, ctrl.release):
        with pytest.raises(takeover.TakeoverError) as caught:
            await call("tk_nonsense")
        assert (caught.value.status, caught.value.code) == (404, "takeover_not_found")
    assert takeover.load_lease()["state"] == "active"


@pytest.mark.anyio
async def test_a_lost_heartbeat_ends_the_lease_and_restores_dlne(box):
    ctrl = box.controller()
    await ctrl.grant({"gpus": [POOL_B], "ram_gb": 2})
    assert len(box.restarts) == 1
    box.clock.t += 46

    assert get_worker_limits()["allowed_gpu_indices"] == POOL_B, "the limits stop applying a stale lease at once"
    await ctrl.tick()
    assert takeover.load_lease()["state"] == "restoring" and ctrl.get()["state"] == "restoring"
    await ctrl._restore_task

    assert ctrl.get() == {"active": False} and takeover.load_lease() is None
    assert len(box.restarts) == 2 and box.restarts[1]["allowed_gpu_indices"] == POOL_B
    assert box.restarts[1]["engine_slots"] == 0 and box.restarts[1]["patient"] is True and box.restarts[1]["reduced"] is False
    assert box.restarts[1]["expected_pool"] == (16.0, 32.0), "the pool from before the lease, not the registry's lagging figure"
    assert box.mem.values == {"MemoryMax": "infinity", "MemoryHigh": "infinity"}


@pytest.mark.anyio
async def test_the_ttl_ends_a_lease_that_is_still_heartbeating(box):
    ctrl = box.controller()
    lease = await ctrl.grant({"gpus": [POOL_A], "ttl_seconds": 60})
    for _ in range(3):
        box.clock.t += 15
        await ctrl.heartbeat(lease["lease_id"])
    await ctrl.tick()
    assert takeover.load_lease()["state"] == "active"

    box.clock.t += 16               # 61 s in, the last heartbeat 16 s ago
    await ctrl.tick()
    assert ctrl.get()["state"] == "restoring"
    await ctrl._restore_task
    assert ctrl.get() == {"active": False}


@pytest.mark.anyio
async def test_a_heartbeat_that_comes_too_late_finds_the_lease_ended(box):
    ctrl = box.controller()
    lease = await ctrl.grant({"gpus": [POOL_A]})
    box.clock.t += 46

    with pytest.raises(takeover.TakeoverError) as caught:
        await ctrl.heartbeat(lease["lease_id"])
    assert (caught.value.status, caught.value.code) == (404, "takeover_not_found")
    assert ctrl.get()["state"] == "restoring"
    await ctrl._restore_task
    assert takeover.load_lease() is None


@pytest.mark.anyio
async def test_release_answers_at_once_and_restores_in_the_background(box):
    ctrl = box.controller()
    lease = await ctrl.grant({"gpus": [POOL_B]})
    assert len(box.restarts) == 1

    answer = await ctrl.release(lease["lease_id"])

    assert answer["state"] == "restoring" and answer["lease_id"] == lease["lease_id"]
    assert ctrl.get()["state"] == "restoring" and len(box.restarts) == 1, "answered before the restore ran"
    again = await ctrl.release(lease["lease_id"])
    assert again["state"] == "restoring"
    await ctrl._restore_task
    assert ctrl.get() == {"active": False} and len(box.restarts) == 2, "one restore, however often it was asked for"
    assert box.restarts[1]["allowed_gpu_indices"] == POOL_B
    assert takeover.status_block()["active"] is False


@pytest.mark.anyio
async def test_the_restore_waits_for_an_idle_moment_like_the_grant(box):
    ctrl = box.controller()
    lease = await ctrl.grant({"gpus": [POOL_B]})
    box.idle_script = [False, False, True, True]
    released_at = box.clock()
    await ctrl.release(lease["lease_id"])

    await ctrl._restore_task

    assert box.clock() - released_at == 15 and len(box.restarts) == 2


@pytest.mark.anyio
async def test_a_restore_that_never_gets_an_idle_moment_goes_ahead_after_ten_minutes(box):
    ctrl = box.controller()
    lease = await ctrl.grant({"gpus": [POOL_B]})
    box.idle_default = False
    released_at = box.clock()
    await ctrl.release(lease["lease_id"])

    await ctrl._restore_task

    assert 600 <= box.clock() - released_at <= 610 and len(box.restarts) == 2 and takeover.load_lease() is None


@pytest.mark.anyio
async def test_a_restore_waits_out_a_switch_that_is_running(box):
    ctrl = box.controller()
    lease = await ctrl.grant({"gpus": [POOL_B]})
    box.results = [{"outcome": "busy", "success": False, "detail": "another model switch is in progress"},
                   {"outcome": "busy", "success": False, "detail": "another model switch is in progress"}]
    await ctrl.release(lease["lease_id"])

    await ctrl._restore_task

    assert len(box.restarts) == 4 and takeover.load_lease() is None, "grant, two busy answers, then the restore"


@pytest.mark.anyio
async def test_releasing_a_handover_lease_restarts_nothing(box):
    ctrl = box.controller()
    lease = await ctrl.grant({"gpus": [POOL_A]})
    await ctrl.release(lease["lease_id"])
    await ctrl._restore_task

    assert box.restarts == [] and box.idle_calls == 0 and takeover.load_lease() is None


@pytest.mark.anyio
async def test_a_watcher_tick_with_nothing_to_do_does_nothing(box):
    ctrl = box.controller()
    await ctrl.tick()
    await ctrl.grant({"gpus": [POOL_A]})
    await ctrl.tick()
    assert takeover.load_lease()["state"] == "active" and ctrl._restore_task is None


@pytest.mark.anyio
async def test_the_plan_changes_nothing(box):
    ctrl = box.controller()
    answer = await ctrl.plan({"gpus": [POOL_B, MEDIA], "cpu_cores": [4], "ram_gb": 2})

    assert set(answer) == {"allowed", "problems", "cards", "keep_result", "cpu_cores", "ram_gb"} and answer["allowed"] is True
    assert [c["action"] for c in answer["cards"]] == ["engine-moves", "pictures-pause+vision-cpu"]
    assert (box.restarts, box.idle_calls, box.mem.sets, box.pictures_stopped) == ([], 0, [], 0)
    assert takeover.load_lease() is None and not os.path.exists(takeover.lease_path())
    with pytest.raises(takeover.TakeoverError):
        await ctrl.plan({"gpus": "nope"})


@pytest.mark.anyio
async def test_pausing_pictures_alone_waits_for_an_idle_moment_but_restarts_nothing(box):
    configure(box, f"ALLOWED_GPU_INDICES={POOL_B}\nMEDIA_GPU={MEDIA}\nIMAGE_GEN_ENABLED=true\n")      # pictures on, no projector
    box.idle_script = [False, True, True]

    lease = await box.controller().grant({"gpus": [MEDIA]})

    assert box.restarts == [] and box.pictures_stopped == 1 and box.idle_calls == 3
    assert lease["dlne_now"]["pictures"] == "paused" and lease["dlne_now"]["vision"] == "off"
    assert get_worker_limits()["image_gen_paused"] is True and get_worker_limits()["image_gen_enabled"] is False
