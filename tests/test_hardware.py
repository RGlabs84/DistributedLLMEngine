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
    assert sum(gpu["vram_total_gb"] for gpu in gpus) == 64.0
    # The wire shape is fixed; the sysfs-only keys must not leak into the heartbeat.
    assert set(gpus[0]) == {"id", "name", "vram_total_gb", "vram_free_gb", "load_percent"}


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
