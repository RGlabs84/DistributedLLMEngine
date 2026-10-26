"""
Integrated GPUs do not make a GPU contributor (2.3.0), except Intel Arc 140V/140T, AMD Strix
Halo and Apple Silicon: engine/hardware.counts_as_gpu and the filter in get_gpu_info.
"""

import pytest

from engine import hardware
from engine.hardware import _vulkan_device_types, counts_as_gpu

# vulkaninfo --summary on TheWall, the public coordinator: a Ryzen 7600X's iGPU and two
# discrete Navi12 cards whose name looks generic.
THEWALL_SUMMARY = """
Devices:
========
GPU0:
	apiVersion         = 1.4.318
	vendorID           = 0x1002
	deviceID           = 0x164e
	deviceType         = PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU
	deviceName         = AMD Ryzen 5 7600X 6-Core Processor (RADV RAPHAEL_MENDOCINO)
GPU1:
	deviceType         = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU
	deviceName         = AMD Radeon Graphics (RADV NAVI12)
GPU2:
	deviceType         = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU
	deviceName         = AMD Radeon Graphics (RADV NAVI12)
GPU3:
	deviceType         = PHYSICAL_DEVICE_TYPE_CPU
	deviceName         = llvmpipe (LLVM 19.1.7, 256 bits)
"""


def test_vulkan_device_types_follow_the_device_list_and_skip_software():
    assert _vulkan_device_types(THEWALL_SUMMARY) == [True, False, False]


@pytest.mark.parametrize("name, integrated, counts", [
    # Vulkan's own verdict beats the name (TheWall's discrete cards have a generic one).
    ("AMD Radeon Graphics (RADV NAVI12)", False, True),
    ("AMD Ryzen 5 7600X 6-Core Processor (RADV RAPHAEL_MENDOCINO)", True, False),
    # The allowed integrated GPUs.
    ("Intel(R) Arc(TM) 140V GPU (16GB)", True, True),
    ("Intel(R) Arc(TM) 140T GPU", True, True),
    ("AMD Radeon 8060S Graphics", True, True),
    ("AMD Radeon Graphics (RADV GFX1151)", True, True),
    ("Apple M2 Pro", None, True),
])
def test_vulkans_answer_decides_unless_the_gpu_is_on_the_allow_list(name, integrated, counts):
    assert counts_as_gpu(name, integrated) is counts


@pytest.mark.parametrize("name, counts", [
    ("AMD Radeon Graphics (RADV NAVI12)", True),
    ("AMD Ryzen 5 7600X 6-Core Processor (RADV RAPHAEL_MENDOCINO)", False),
    ("NVIDIA GeForce RTX 4070 Laptop GPU", True),
    ("AMD Radeon RX 7900 XTX", True),
    ("Intel(R) Arc(TM) A770 Graphics", True),
    ("Intel(R) Arc(TM) B580 Graphics (BMG G21)", True),
    ("Intel(R) UHD Graphics 770", False),
    ("Intel(R) Graphics (RPL-S)", False),
    ("Intel(R) Arc(TM) Graphics", False),               # Meteor Lake's iGPU
    ("AMD Radeon(TM) Graphics", False),                 # a generic APU, as Windows names it
    ("AMD Radeon 780M Graphics", False),
    ("AMD Radeon 890M Graphics (RADV GFX1150)", False),  # Strix Point, not Strix Halo
    ("Radeon Vega 8", False),
    ("Qualcomm(R) Adreno(TM) X1-85 GPU", False),
    ("Some New Accelerator 9000", True),                # unknown: never turned away by a guess
])
def test_without_vulkans_answer_the_name_decides(name, counts):
    assert counts_as_gpu(name) is counts


def _gpus(*entries):
    return [{"id": i, "name": n, "vram_total_gb": v, "vram_free_gb": v, "load_percent": 0.0, **extra}
            for i, (n, v, extra) in enumerate(entries)]


def test_a_machine_with_only_an_integrated_gpu_is_cpu_only(monkeypatch):
    monkeypatch.setattr(hardware, "get_all_gpus", lambda: _gpus(("AMD Radeon 780M Graphics", 2.0, {"integrated": True})))
    monkeypatch.setattr(hardware, "get_worker_limits", lambda: {"allowed_gpu_indices": "-1", "max_vram_gb": "-1"})
    assert hardware.get_gpu_info()["has_gpu"] is False


def test_the_integrated_gpu_is_left_out_of_a_machine_with_real_cards(monkeypatch):
    monkeypatch.setattr(hardware, "get_all_gpus", lambda: _gpus(
        ("AMD Ryzen 5 7600X 6-Core Processor (RADV RAPHAEL_MENDOCINO)", 2.0, {"integrated": True}),
        ("AMD Radeon Graphics (RADV NAVI12)", 8.0, {"integrated": False}),
        ("AMD Radeon Graphics (RADV NAVI12)", 8.0, {"integrated": False})))
    monkeypatch.setattr(hardware, "get_worker_limits", lambda: {"allowed_gpu_indices": "-1", "max_vram_gb": "-1"})
    info = hardware.get_gpu_info()
    assert info["has_gpu"] is True and info["vram_total_gb"] == 16.0
    assert "RAPHAEL" not in info["name"]


def test_strix_halo_counts(monkeypatch):
    monkeypatch.setattr(hardware, "get_all_gpus", lambda: _gpus(("AMD Radeon 8060S Graphics", 64.0, {"integrated": True})))
    monkeypatch.setattr(hardware, "get_worker_limits", lambda: {"allowed_gpu_indices": "-1", "max_vram_gb": "-1"})
    assert hardware.get_gpu_info()["has_gpu"] is True
