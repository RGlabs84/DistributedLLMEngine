"""
Tests for ``engine.hardware``'s Linux GPU detection.

A two-card AMD compute rig reported "no GPU". Both halves of the failure are asserted here,
because either one alone was enough to lose the hardware:

* Enumeration went through ``vulkaninfo`` and ``clinfo``, which come from vulkan-tools and
  an OpenCL runtime respectively. A freshly imaged compute box has neither, so every probe
  fell through and the machine was reported as CPU-only.
* Vendor classification scanned ``lspci`` for lines reading "VGA compatible controller" or
  "3D controller". A card with no display outputs is neither — it is a "Display
  controller" — so the rig was typed ``cpu`` and never loaded a GPU backend.

WHY SYSFS IS FAKED IN MEMORY RATHER THAN ON DISK
------------------------------------------------
The real tree cannot be reproduced as directories on the machine these tests run on: a PCI
address contains colons, which Windows forbids in a filename, and the ``device`` and
``driver`` entries are symlinks, which need a privilege Windows does not grant by default.
Faking the four primitives the probe reads through keeps the tests running on every
developer machine and on Linux CI alike, and the layout below is the real one:
``/sys/class/drm`` holds a bare ``cardN`` per device plus per-connector and render-node
entries that are not devices, and each card's ``device`` symlink points into ``/sys/devices``.
"""

import os

import pytest

import engine.hardware as hardware


AMD_VENDOR = "0x1002"
NVIDIA_VENDOR = "0x10de"

DRM_ROOT = "/sys/class/drm"


def install_fake_drm(monkeypatch, cards, first_card=0):
    """
    Points the probe at an in-memory ``/sys/class/drm``.

    Args:
        cards: one ``(pci_address, vendor_id, device_id, driver, vram_bytes)`` per card.
            ``vram_bytes=None`` omits ``mem_info_vram_total``, which is what a driver that
            does not publish a VRAM size looks like. ``vendor_id=None`` omits ``vendor``,
            which is what a virtual DRM provider looks like.
        first_card: DRM minor of the first card. Not always 0 — simpledrm claims minor 0
            during early boot on plenty of machines, leaving the real GPUs at card1 and up.
    """
    attributes = {}
    link_targets = {}
    entries = []

    for offset, (address, vendor, device, driver, vram) in enumerate(cards):
        index = first_card + offset
        device_dir = os.path.join(DRM_ROOT, f"card{index}", "device")
        if vendor is not None:
            attributes[os.path.join(device_dir, "vendor")] = vendor
        if device is not None:
            attributes[os.path.join(device_dir, "device")] = device
        if vram is not None:
            attributes[os.path.join(device_dir, "mem_info_vram_total")] = str(vram)

        link_targets[device_dir] = f"/sys/devices/pci0000:00/{address}"
        link_targets[os.path.join(device_dir, "driver")] = f"/sys/bus/pci/drivers/{driver}"

        # Noise the real tree always carries and the probe must ignore.
        entries += [f"card{index}", f"card{index}-DP-1", f"renderD{128 + index}"]

    monkeypatch.setattr(hardware, "_DRM_ROOT", DRM_ROOT)
    monkeypatch.setattr(hardware.os.path, "isdir", lambda path: path == DRM_ROOT)
    monkeypatch.setattr(hardware.os, "listdir", lambda path: list(entries))
    monkeypatch.setattr(hardware, "_read_sysfs", lambda path: attributes.get(path, ""))
    monkeypatch.setattr(hardware.os.path, "realpath", lambda path: link_targets.get(path, path))


def block_all_subprocesses(monkeypatch):
    """Neither vulkaninfo, clinfo, lspci nor nvidia-smi is installed."""
    def not_installed(args, **kwargs):
        raise FileNotFoundError(args[0] if args else "probe")

    monkeypatch.setattr(hardware.subprocess, "run", not_installed)


@pytest.fixture
def no_lspci(monkeypatch):
    """lspci absent — the normal state of a minimal server image."""
    monkeypatch.setattr(hardware, "_lspci_device_names", lambda: {})


def test_two_amd_cards_are_found_without_any_userspace_tooling(monkeypatch, no_lspci):
    """The regression: two 32 GB compute cards, nothing installed to ask about them."""
    thirty_two_gb = 32 * 1024 ** 3
    install_fake_drm(monkeypatch, [
        ("0000:03:00.0", AMD_VENDOR, "0x7362", "amdgpu", thirty_two_gb),
        ("0000:04:00.0", AMD_VENDOR, "0x7362", "amdgpu", thirty_two_gb),
    ])

    cards = hardware._probe_linux_drm_cards()

    assert [card["id"] for card in cards] == [0, 1]
    assert [card["vram_total_gb"] for card in cards] == [32.0, 32.0]
    assert all(card["vendor_id"] == AMD_VENDOR for card in cards)
    assert all(card["driver"] == "amdgpu" for card in cards)


def test_virtual_drm_providers_are_not_counted_as_gpus(monkeypatch, no_lspci):
    """simpledrm and vkms have no PCI vendor attribute, and must not become a GPU."""
    install_fake_drm(monkeypatch, [("virtual", None, None, "simpledrm", None)])

    assert hardware._probe_linux_drm_cards() == []


def test_a_card_that_hides_its_vram_is_still_enumerated(monkeypatch, no_lspci):
    """Unknown VRAM is reported as 0.0, not as a missing card."""
    install_fake_drm(monkeypatch, [("0000:01:00.0", NVIDIA_VENDOR, "0x1c03", "nouveau", None)])

    cards = hardware._probe_linux_drm_cards()

    assert len(cards) == 1
    assert cards[0]["vram_total_gb"] == 0.0
    assert NVIDIA_VENDOR in cards[0]["name"]


def test_lspci_supplies_the_readable_name_when_it_is_installed(monkeypatch):
    """The PCI ID is the fallback, not the preference."""
    install_fake_drm(monkeypatch, [("0000:03:00.0", AMD_VENDOR, "0x7362", "amdgpu", 32 * 1024 ** 3)])
    # lspci prints the address without the PCI domain; the probe reads it with one.
    monkeypatch.setattr(hardware, "_lspci_device_names",
                        lambda: {"03:00.0": "Advanced Micro Devices, Inc. [AMD/ATI] Device 7362"})

    assert hardware._probe_linux_drm_cards()[0]["name"].startswith("Advanced Micro Devices")


def test_a_display_controller_class_card_is_typed_vulkan(monkeypatch, no_lspci):
    """
    Vendor classification must not depend on the card driving a monitor.

    GPUtil and the Windows/Darwin branches are bypassed by forcing the platform to Linux and
    stubbing GPUtil out, leaving exactly the path a headless AMD rig takes.
    """
    install_fake_drm(monkeypatch, [("0000:03:00.0", AMD_VENDOR, "0x7362", "amdgpu", 32 * 1024 ** 3)])
    monkeypatch.setattr(hardware.platform, "system", lambda: "Linux")
    monkeypatch.setattr(hardware.GPUtil, "getGPUs", lambda: [])
    block_all_subprocesses(monkeypatch)

    assert hardware._detect_gpu_type() == "vulkan"


def test_a_machine_with_no_pci_gpu_is_still_cpu(monkeypatch, no_lspci):
    """The new probe must not turn every machine into a GPU machine."""
    install_fake_drm(monkeypatch, [])
    monkeypatch.setattr(hardware.platform, "system", lambda: "Linux")
    monkeypatch.setattr(hardware.GPUtil, "getGPUs", lambda: [])
    block_all_subprocesses(monkeypatch)

    assert hardware._detect_gpu_type() == "cpu"


def test_sysfs_cards_are_the_fallback_when_no_probe_tool_exists(monkeypatch, no_lspci):
    """End to end through _probe_all_gpus: nvidia-smi, vulkaninfo and clinfo all absent."""
    install_fake_drm(monkeypatch, [
        ("0000:03:00.0", AMD_VENDOR, "0x7362", "amdgpu", 32 * 1024 ** 3),
        ("0000:04:00.0", AMD_VENDOR, "0x7362", "amdgpu", 32 * 1024 ** 3),
    ])
    monkeypatch.setattr(hardware.platform, "system", lambda: "Linux")
    block_all_subprocesses(monkeypatch)

    gpus, values_are_live = hardware._probe_all_gpus()

    assert values_are_live is False
    assert len(gpus) == 2
    # The sysfs-only keys must not leak into the heartbeat; the PCI address stays, it is how ALLOWED_GPU_INDICES and MEDIA_GPU name a card.
    # The wire shape is fixed; the sysfs-only keys must not leak into the heartbeat.
    assert set(gpus[0]) == {"id", "name", "vram_total_gb", "vram_free_gb", "load_percent", "address"}


def test_a_software_rasteriser_is_not_reported_as_a_gpu(monkeypatch, no_lspci):
    """
    llvmpipe answering vulkaninfo is the dangerous false positive.

    The node would advertise acceleration to the coordinator, be handed layers on that
    basis, and then run them on the CPU.
    """
    install_fake_drm(monkeypatch, [])
    monkeypatch.setattr(hardware.platform, "system", lambda: "Linux")

    class _Result:
        returncode = 0
        stdout = "\tdeviceName         = llvmpipe (LLVM 15.0.7, 256 bits)\n"

    def fake_run(args, **kwargs):
        if args[0] == "vulkaninfo":
            return _Result()
        raise FileNotFoundError(args[0])

    monkeypatch.setattr(hardware.subprocess, "run", fake_run)

    gpus, _ = hardware._probe_all_gpus()

    assert gpus == []


def test_each_card_is_sized_from_its_own_vram(monkeypatch, no_lspci):
    """
    A mixed rig used to take every card's size from whichever one the kernel numbered
    card0, because the VRAM read was hardcoded to that path and the result applied to all.
    """
    install_fake_drm(monkeypatch, [
        ("0000:03:00.0", AMD_VENDOR, "0x7362", "amdgpu", 8 * 1024 ** 3),
        ("0000:04:00.0", AMD_VENDOR, "0x73bf", "amdgpu", 16 * 1024 ** 3),
    ])
    monkeypatch.setattr(hardware.platform, "system", lambda: "Linux")

    class _Result:
        returncode = 0
        stdout = ("\tdeviceName         = AMD Radeon Graphics (RADV NAVI12)\n"
                  "\tdeviceName         = AMD Radeon Graphics (RADV NAVI21)\n")

    def fake_run(args, **kwargs):
        if args[0] == "vulkaninfo":
            return _Result()
        raise FileNotFoundError(args[0])

    monkeypatch.setattr(hardware.subprocess, "run", fake_run)

    gpus, _ = hardware._probe_all_gpus()

    assert [gpu["vram_total_gb"] for gpu in gpus] == [8.0, 16.0]


def test_vulkan_device_order_is_not_assumed_to_match_the_kernel(monkeypatch, no_lspci):
    """
    Measured on the real rig: RADV listed the integrated GPU FIRST, while sysfs numbered
    the two discrete cards first and the integrated one last.

    Pairing the two lists by position therefore hands each device some other device's
    VRAM — the integrated GPU would have been advertised with a discrete card's 8 GB. When
    the counts agree, sysfs is the authority for both size and order.
    """
    install_fake_drm(monkeypatch, [
        ("0000:03:00.0", AMD_VENDOR, "0x7360", "amdgpu", 8573157376),   # BC-160
        ("0000:09:00.0", AMD_VENDOR, "0x7360", "amdgpu", 8573157376),   # BC-160
        ("0000:10:00.0", AMD_VENDOR, "0x164e", "amdgpu", 536870912),    # Raphael iGPU
    ])
    monkeypatch.setattr(hardware.platform, "system", lambda: "Linux")

    class _Result:
        returncode = 0
        # RADV's real ordering: integrated first.
        stdout = ("\tdeviceName         = AMD Ryzen 5 7600X 6-Core Processor (RADV RAPHAEL_MENDOCINO)\n"
                  "\tdeviceName         = AMD Radeon Graphics (RADV NAVI12)\n"
                  "\tdeviceName         = AMD Radeon Graphics (RADV NAVI12)\n")

    def fake_run(args, **kwargs):
        if args[0] == "vulkaninfo":
            return _Result()
        raise FileNotFoundError(args[0])

    monkeypatch.setattr(hardware.subprocess, "run", fake_run)

    gpus, _ = hardware._probe_all_gpus()

    assert [gpu["vram_total_gb"] for gpu in gpus] == [8.0, 8.0, 0.5]
    assert sum(gpu["vram_total_gb"] for gpu in gpus) == 16.5


def test_a_machine_with_no_card0_still_reports_its_vram(monkeypatch, no_lspci):
    """
    The direct cause of the out-of-memory kill on the test rig.

    simpledrm claimed DRM minor 0 during boot, so the amdgpu cards are card1..card3 and the
    hardcoded /sys/class/drm/card0 path did not exist at all. os.path.exists said no, the
    size silently stayed 0.0, and every GPU was advertised as having no VRAM while still
    reporting has_gpu=True — so the coordinator sized a workload against something other
    than VRAM and the engine was OOM-killed.
    """
    install_fake_drm(monkeypatch, [
        ("0000:03:00.0", AMD_VENDOR, "0x7360", "amdgpu", 8573157376),
        ("0000:09:00.0", AMD_VENDOR, "0x7360", "amdgpu", 8573157376),
        ("0000:10:00.0", AMD_VENDOR, "0x164e", "amdgpu", 536870912),
    ], first_card=1)

    cards = hardware._probe_linux_drm_cards()

    assert [card["vram_total_gb"] for card in cards] == [8.0, 8.0, 0.5]


def test_rejected_compute_cards_do_not_inflate_the_survivor(monkeypatch, no_lspci):
    """
    The real rig this was written for: two 8 GB compute cards the kernel bound and RADV
    refused (VK_ERROR_INCOMPATIBLE_DRIVER), leaving only the 0.5 GB integrated GPU to
    answer vulkaninfo.

    The counts disagree, so there is no way to tell which sysfs card the surviving Vulkan
    device is. Advertising 8 GB here would have the coordinator size a layer split against
    VRAM that does not exist on the device actually doing the work.
    """
    install_fake_drm(monkeypatch, [
        ("0000:03:00.0", AMD_VENDOR, "0x7360", "amdgpu", 8573157376),
        ("0000:09:00.0", AMD_VENDOR, "0x7360", "amdgpu", 8573157376),
        ("0000:10:00.0", AMD_VENDOR, "0x164e", "amdgpu", 536870912),
    ])
    monkeypatch.setattr(hardware.platform, "system", lambda: "Linux")

    class _Result:
        returncode = 0
        stdout = "\tdeviceName         = AMD Radeon Graphics (RADV RAPHAEL_MENDOCINO)\n"

    def fake_run(args, **kwargs):
        if args[0] == "vulkaninfo":
            return _Result()
        raise FileNotFoundError(args[0])

    monkeypatch.setattr(hardware.subprocess, "run", fake_run)

    gpus, _ = hardware._probe_all_gpus()

    assert len(gpus) == 1
    assert gpus[0]["vram_total_gb"] == 0.5


# --- The media card (2.6): cards named by PCI address, and one the engine never loads ---------

def thewall_cards():
    """TheWall as measured 2026-10-27: DLNE's ids follow the kernel's PCI order, Vulkan's order puts the iGPU first."""
    def card(card_id, address, name, vram_gb, vk_index, integrated=False):
        return {"id": card_id, "name": name, "address": address, "vram_total_gb": vram_gb, "vram_free_gb": vram_gb,
                "load_percent": 0.0, "vk_index": vk_index, "integrated": integrated}

    return [
        card(0, "0000:03:00.0", "AMD Radeon Graphics (RADV NAVI12)", 8.0, 1),
        card(1, "0000:09:00.0", "AMD Radeon Graphics (RADV NAVI12)", 8.0, 2),
        card(2, "0000:0c:00.0", "AMD Radeon RX 580 Series (RADV POLARIS10)", 8.0, 3),
        card(3, "0000:15:00.0", "AMD Radeon Graphics (RADV RAPHAEL_MENDOCINO)", 0.5, 0, integrated=True),
    ]


@pytest.fixture
def thewall_gpus(monkeypatch):
    monkeypatch.setattr(hardware, "get_all_gpus", lambda: thewall_cards())
    return thewall_cards()


@pytest.mark.parametrize("spec, expected", [
    ("1", 1), (" 2 ", 2), ("0000:03:00.0", 0), ("03:00.0", 0), ("0000:0C:00.0", 2), ("0c:00.0", 2),
    ("9", None), ("0000:ff:00.0", None), ("00.0", None), ("", None), ("gpu", None),
])
def test_a_card_is_named_by_id_or_by_pci_address_with_or_without_the_domain(thewall_gpus, spec, expected):
    found = hardware.find_gpu(spec, thewall_gpus)

    assert (found["id"] if found else None) == expected


def test_select_gpus_keeps_the_order_written_each_card_once_and_skips_unknown_entries(thewall_gpus):
    chosen = hardware.select_gpus("1, 0000:03:00.0,0,7,03:00.0", thewall_gpus)

    assert [g["id"] for g in chosen] == [1, 0]


def test_without_a_media_card_the_engine_sees_what_was_allowed(thewall_gpus):
    assert hardware.engine_gpu_devices("0,1") == ("0,1", "1,2", "")
    assert hardware.engine_gpu_devices("0000:03:00.0,0000:09:00.0") == ("0,1", "1,2", "")
    assert hardware.engine_gpu_devices("-1") == ("", "", "")        # no limit, no media card: left to Vulkan


def test_the_media_card_comes_last_so_the_engine_cards_keep_their_names(thewall_gpus):
    cuda, vulkan, media_device = hardware.engine_gpu_devices("0,1", "0000:0c:00.0")

    assert (cuda, vulkan, media_device) == ("0,1", "1,2,3", "Vulkan2")


def test_a_media_card_in_the_allowed_list_is_still_only_the_media_card(thewall_gpus):
    assert hardware.engine_gpu_devices("0,1,2", "0c:00.0") == ("0,1", "1,2,3", "Vulkan2")
    assert hardware.engine_gpu_devices("0,0000:0c:00.0,1", "2") == ("0,1", "1,2,3", "Vulkan2")


def test_no_limit_with_a_media_card_writes_out_every_discrete_card_but_that_one(thewall_gpus):
    # Vulkan's own default would take the RX 580 too; the iGPU is not a GPU contributor
    assert hardware.engine_gpu_devices("-1", "0000:0c:00.0") == ("0,1", "1,2,3", "Vulkan2")


def test_a_shared_media_card_is_listed_the_same_way_last(thewall_gpus):
    # MEDIA_GPU_SHARED: the same list, so -ts 15,15,10 lines up with BC-160 #1, BC-160 #2, RX 580
    assert hardware.engine_gpu_devices("-1", "0000:0c:00.0", shared=True) == ("0,1", "1,2,3", "Vulkan2")
    assert hardware.engine_gpu_devices("0,1", "0000:0c:00.0", shared=True) == ("0,1", "1,2,3", "Vulkan2")
    assert hardware.engine_gpu_devices("0,0000:0c:00.0,1", "2", shared=True) == ("0,1", "1,2,3", "Vulkan2")


def test_a_media_card_that_is_not_there_changes_nothing(thewall_gpus):
    assert hardware.engine_gpu_devices("0,1", "0000:ff:00.0") == ("0,1", "1,2", "")
    assert hardware.engine_gpu_devices("-1", "0000:ff:00.0") == ("", "", "")


@pytest.fixture
def warnings_logged(monkeypatch):
    seen = []
    monkeypatch.setattr(hardware, "log_warning", seen.append)
    monkeypatch.setattr(hardware, "_dropped_warned", set())
    return seen


def test_a_card_named_twice_is_one_device_by_index_by_address_or_both(thewall_gpus, warnings_logged):
    assert hardware.engine_gpu_devices("0,0000:03:00.0,1", "") == ("0,1", "1,2", "")
    assert hardware.engine_gpu_devices("0000:03:00.0,0000:03:00.0", "") == ("0", "1", "")
    assert hardware.engine_gpu_devices("0,0,0", "") == ("0", "1", "")
    assert hardware.engine_gpu_devices("0,0000:0c:00.0", "0000:0c:00.0") == ("0", "1,3", "Vulkan1")
    assert any("already listed" in line for line in warnings_logged)


def test_a_card_the_probe_does_not_list_is_deduplicated_too(monkeypatch, warnings_logged):
    monkeypatch.setattr(hardware, "get_all_gpus", lambda: [])

    assert hardware.engine_gpu_devices("0,1,0,1", "") == ("0,1", "0,1", "")


def test_every_entry_that_is_left_out_is_said_once_in_the_log(thewall_gpus, warnings_logged):
    for _ in range(3):
        assert hardware.engine_gpu_devices("0,typo,0000:ff:00.0,1,0000:0c:00.0", "0000:0c:00.0") == ("0,1", "1,2,3", "Vulkan2")

    text = "\n".join(warnings_logged)
    assert text.count("typo") == 1 and text.count("0000:ff:00.0") == 1 and text.count("media card") == 1
    assert "names no card here" in text and "sysfs" in text and "use the id" in text
    assert len(warnings_logged) == 3


def test_nothing_is_said_when_nothing_is_left_out(thewall_gpus, warnings_logged):
    hardware.engine_gpu_devices("0,1", "0000:0c:00.0")
    hardware.engine_gpu_devices("-1", "0000:0c:00.0")

    assert warnings_logged == []


def test_a_shared_media_card_named_in_the_allowed_list_is_no_mistake(thewall_gpus, warnings_logged):
    assert hardware.engine_gpu_devices("0,1,0000:0c:00.0", "0000:0c:00.0", shared=True) == ("0,1", "1,2,3", "Vulkan2")

    assert warnings_logged == []


def test_an_entry_the_probe_does_not_list_is_passed_through_as_written(monkeypatch):
    monkeypatch.setattr(hardware, "get_all_gpus", lambda: [])

    assert hardware.engine_gpu_devices("0,1") == ("0,1", "0,1", "")
    assert hardware.engine_gpu_devices("0000:03:00.0") == ("", "", "")      # an address cannot be passed through


def test_a_probe_that_fails_does_not_stop_the_engine_starting(monkeypatch):
    def broken():
        raise OSError("no drm")

    monkeypatch.setattr(hardware, "get_all_gpus", broken)

    assert hardware.engine_gpu_devices("0,1", "0000:0c:00.0") == ("0,1", "0,1", "")


@pytest.fixture
def vulkan_install(tmp_path, monkeypatch):
    """MEDIA_GPU applies on a Vulkan build; engine_build_is_vulkan reads <base>/bin, so a test that sets it points
    that at a directory of its own instead of whatever the checkout or install holds."""
    from engine import inference_manager
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "libggml-vulkan.so").write_bytes(b"")
    monkeypatch.setattr(inference_manager, "get_base_dir", lambda: str(tmp_path))
    return tmp_path


def _limits(monkeypatch, **overrides):
    limits = {"allowed_gpu_indices": "-1", "max_vram_gb": "-1", **overrides}
    monkeypatch.setattr(hardware, "get_worker_limits", lambda: limits)


def test_the_pool_a_node_advertises_leaves_the_media_card_out(thewall_gpus, monkeypatch, vulkan_install):
    _limits(monkeypatch, allowed_gpu_indices="-1", media_gpu="0000:0c:00.0")
    info = hardware.get_gpu_info()

    assert info["count"] == 2 and info["vram_total_gb"] == 16.0
    assert "RX 580" not in info["name"]


def test_a_media_card_inside_an_explicit_list_is_left_out_too(thewall_gpus, monkeypatch, vulkan_install):
    _limits(monkeypatch, allowed_gpu_indices="0,1,0000:0c:00.0", media_gpu="2")

    assert hardware.get_gpu_info()["vram_total_gb"] == 16.0


def test_on_a_cuda_build_the_pool_a_node_advertises_keeps_the_card_the_engine_uses(thewall_gpus, monkeypatch, tmp_path):
    from engine import inference_manager
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "libggml-cuda.so").write_bytes(b"")
    monkeypatch.setattr(inference_manager, "get_base_dir", lambda: str(tmp_path))
    monkeypatch.setattr(inference_manager, "_media_gpu_warned", True)       # said once already: no log line here
    _limits(monkeypatch, allowed_gpu_indices="-1", media_gpu="0000:0c:00.0")

    info = hardware.get_gpu_info()

    assert info["count"] == 3 and info["vram_total_gb"] == 24.0 and "RX 580" in info["name"]


def test_a_shared_media_card_counts_in_the_pool_a_node_advertises(thewall_gpus, monkeypatch, vulkan_install):
    """MEDIA_GPU_SHARED: the RX 580 holds layers too, so TheWall advertises 24 GB (the coordinator's pool)."""
    _limits(monkeypatch, allowed_gpu_indices="-1", media_gpu="0000:0c:00.0", media_gpu_shared=True)
    info = hardware.get_gpu_info()

    assert info["count"] == 3 and info["vram_total_gb"] == 24.0 and "RX 580" in info["name"]

    # the engine is given the card whether ALLOWED_GPU_INDICES names it or not, so the pool counts it either way
    _limits(monkeypatch, allowed_gpu_indices="0,1", media_gpu="0000:0c:00.0", media_gpu_shared=True)
    assert hardware.get_gpu_info()["vram_total_gb"] == 24.0
    _limits(monkeypatch, allowed_gpu_indices="0,1,2", media_gpu="2", media_gpu_shared=True)
    assert hardware.get_gpu_info()["count"] == 3                      # counted once


def test_a_media_card_that_is_not_shared_stays_out_of_the_pool(thewall_gpus, monkeypatch, vulkan_install):
    _limits(monkeypatch, allowed_gpu_indices="-1", media_gpu="0000:0c:00.0", media_gpu_shared=False)
    assert hardware.get_gpu_info()["vram_total_gb"] == 16.0

    _limits(monkeypatch, allowed_gpu_indices="0,1", media_gpu_shared=True)       # shared, but no media card named
    assert hardware.get_gpu_info()["vram_total_gb"] == 16.0


def test_the_pool_is_what_it_was_without_a_media_card(thewall_gpus, monkeypatch):
    _limits(monkeypatch, allowed_gpu_indices="0,1")
    assert hardware.get_gpu_info()["vram_total_gb"] == 16.0

    _limits(monkeypatch, allowed_gpu_indices="-1")
    assert hardware.get_gpu_info()["vram_total_gb"] == 24.0       # the RX 580 counts when nothing says otherwise


def test_the_pool_can_be_limited_by_pci_address(thewall_gpus, monkeypatch):
    _limits(monkeypatch, allowed_gpu_indices="0000:03:00.0")

    assert hardware.get_gpu_info()["vram_total_gb"] == 8.0
