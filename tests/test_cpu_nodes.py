"""
CPU-only contributors: what a node reports (instruction sets, memory bandwidth), the floors
the Coordinator holds it to, and how a refusal reaches the person whose machine it is.

Generating a token reads every weight a node holds, so a CPU node's speed is its memory
bandwidth, and one slow node holds up every token of the pipeline it sits in. Until 2.1.3 a
CPU node only had to claim 8 GB and 2 cores, and the extension lent 4 GB by default, so a
GPU-less contributor was refused with nothing on screen to say why.
"""

import asyncio

import httpx
import pytest
from fastapi import HTTPException

from engine import authn, coordinator, downloader, hardware, worker
from engine import config as engine_config
from engine.version import VERSION

QUALIFYING = {
    "gpu": {"has_gpu": False, "vram_total_gb": 0},
    "ram_gb": 12.0, "cpu_cores": 6, "cpu_threads": 12,
    "cpu_arch": "x86_64", "cpu_features": ["avx2", "fma", "f16c"],
    "mem_bandwidth_gbs": 48.0, "is_yielding": False,
}
FLOORS = dict(engine_config.CPU_NODE_DEFAULTS)


def node(**changes):
    hw = {**QUALIFYING, **changes}
    return {k: v for k, v in hw.items() if v is not ...}


# ======================================================================================
# What a node reports
# ======================================================================================

CPUINFO = """processor\t: 0
vendor_id\t: GenuineIntel
flags\t\t: fpu sse sse2 ssse3 fma sse4_1 sse4_2 avx f16c avx2 bmi2 avx512f avx_vnni amx_tile amx_int8
"""


def test_linux_flags_are_read_in_llama_cpp_names():
    assert hardware._linux_cpu_features(CPUINFO) == ["avx2", "fma", "f16c", "avx512f", "avx_vnni", "amx"]


def test_an_old_cpu_reports_none_of_the_fast_paths():
    old = "flags\t\t: fpu sse sse2 ssse3 sse4_1 sse4_2 avx\n"
    assert hardware._linux_cpu_features(old) == []


def test_windows_infers_fma_and_f16c_from_avx2():
    """Windows has no FMA query; every x86 CPU with AVX2 has FMA3 and F16C."""
    with_avx2 = hardware._windows_cpu_features(lambda pf: pf == hardware._WIN_PF_AVX2)
    assert with_avx2 == ["avx2", "fma", "f16c"]
    assert hardware._windows_cpu_features(lambda pf: False) == []
    both = hardware._windows_cpu_features(lambda pf: pf in (hardware._WIN_PF_AVX2, hardware._WIN_PF_AVX512F))
    assert both == ["avx2", "fma", "f16c", "avx512f"]


def test_arm_reports_no_x86_features(monkeypatch):
    monkeypatch.setattr(hardware, "_cached_cpu_features", None)
    monkeypatch.setattr(hardware, "get_cpu_arch", lambda: "arm64")
    assert hardware.get_cpu_features() == []


@pytest.mark.parametrize("machine, arch", [("x86_64", "x86_64"), ("AMD64", "x86_64"),
                                           ("aarch64", "arm64"), ("arm64", "arm64")])
def test_cpu_arch_is_normalised(monkeypatch, machine, arch):
    monkeypatch.setattr(hardware.platform, "machine", lambda: machine)
    assert hardware.get_cpu_arch() == arch


def test_an_intel_mac_gpu_is_never_reported_as_metal(monkeypatch):
    """llama.cpp's Intel Mac build has no Metal backend; reporting its AMD GPU would count
    VRAM the engine cannot use."""
    profiler = "Chipset Model: AMD Radeon Pro 5500M\n  Vendor: AMD\n  Metal Support: Metal 3\nApple display"

    class Result:
        stdout = profiler

    monkeypatch.setattr(hardware.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(hardware.GPUtil, "getGPUs", lambda: [])     # no NVIDIA on a Mac
    monkeypatch.setattr(hardware.subprocess, "run", lambda *a, **k: Result())
    monkeypatch.setattr(hardware, "_cached_gpu_type", None, raising=False)

    monkeypatch.setattr(hardware, "get_cpu_arch", lambda: "x86_64")
    assert hardware.get_gpu_type() == "cpu"

    monkeypatch.setattr(hardware, "_cached_gpu_type", None, raising=False)
    monkeypatch.setattr(hardware, "get_cpu_arch", lambda: "arm64")
    assert hardware.get_gpu_type() == "metal"


def test_bandwidth_is_really_measured():
    """A real (small) run: separate processes, timed only once every buffer is touched."""
    gbs = hardware.measure_memory_bandwidth_gbs(processes=2, chunk_bytes=8 * 1024 * 1024, passes=2)
    assert isinstance(gbs, float) and gbs > 0.5


def test_a_failed_bandwidth_probe_is_reported_as_unknown(monkeypatch):
    monkeypatch.setattr(hardware, "_bandwidth_measured", False)
    monkeypatch.setattr(hardware, "_cached_bandwidth", None)

    def boom(*a, **k):
        raise RuntimeError("no memory")

    monkeypatch.setattr(hardware, "measure_memory_bandwidth_gbs", boom)
    assert hardware.get_memory_bandwidth_gbs() is None


def test_bandwidth_is_measured_once(monkeypatch):
    calls = []
    monkeypatch.setattr(hardware, "_bandwidth_measured", False)
    monkeypatch.setattr(hardware, "measure_memory_bandwidth_gbs", lambda: calls.append(1) or 40.0)
    assert hardware.get_memory_bandwidth_gbs() == 40.0
    assert hardware.get_memory_bandwidth_gbs() == 40.0
    assert calls == [1]


def _profile(monkeypatch, has_gpu, bandwidth=33.3):
    gpu = {"has_gpu": has_gpu, "vram_total_gb": 8.0 if has_gpu else 0, "vram_free_gb": 0,
           "name": None, "load_percent": 0.0}
    monkeypatch.setattr(hardware, "get_gpu_info", lambda: gpu)
    monkeypatch.setattr(hardware, "get_gpu_type", lambda: "vulkan" if has_gpu else "cpu")
    monkeypatch.setattr(hardware, "is_gaming_or_heavy_load", lambda info: False)
    monkeypatch.setattr(hardware, "get_dock_state", lambda: {"link": "unknown", "power": "unknown"})
    monkeypatch.setattr(hardware, "get_cpu_features", lambda: ["avx2", "fma"])
    measured = []
    monkeypatch.setattr(hardware, "get_memory_bandwidth_gbs", lambda: measured.append(1) or bandwidth)
    return hardware.get_hardware_profile(), measured


def test_a_cpu_node_reports_its_bandwidth_and_instructions(monkeypatch):
    profile, measured = _profile(monkeypatch, has_gpu=False)
    assert profile["mem_bandwidth_gbs"] == 33.3
    assert profile["cpu_features"] == ["avx2", "fma"]
    assert profile["cpu_arch"] in ("x86_64", "arm64") or profile["cpu_arch"]
    assert measured == [1]


def test_a_gpu_node_never_pays_for_the_bandwidth_probe(monkeypatch):
    profile, measured = _profile(monkeypatch, has_gpu=True)
    assert "mem_bandwidth_gbs" not in profile and measured == []


def test_an_unmeasured_bandwidth_is_left_out_rather_than_sent_as_null(monkeypatch):
    profile, _ = _profile(monkeypatch, has_gpu=False, bandwidth=None)
    assert "mem_bandwidth_gbs" not in profile
    assert authn.validate_hardware_profile(profile)[0]


def test_the_new_fields_pass_validation():
    assert authn.validate_hardware_profile(QUALIFYING) == (True, "ok")


@pytest.mark.parametrize("bad", [
    {"cpu_features": "avx2"},
    {"cpu_features": ["avx2", 7]},
    {"cpu_features": ["x" * 33]},
    {"cpu_features": ["avx2"] * 33},
    {"cpu_arch": ["x86_64"]},
    {"cpu_arch": "x" * 33},
    {"mem_bandwidth_gbs": 5000.0},
    {"mem_bandwidth_gbs": -1},
    {"mem_bandwidth_gbs": "fast"},
    {"mem_bandwidth_gbs": True},
])
def test_malformed_cpu_claims_are_rejected(bad):
    ok, _reason = authn.validate_hardware_profile({**QUALIFYING, **bad})
    assert not ok


# ======================================================================================
# The floors
# ======================================================================================

def test_the_floors_are_the_agreed_ones():
    assert FLOORS == {"cpu_min_ram_gb": 8.0, "cpu_min_cores": 4, "cpu_min_bandwidth_gbs": 30.0}


def test_a_qualifying_cpu_node_has_no_shortfalls():
    assert coordinator.cpu_node_shortfalls(QUALIFYING, FLOORS) == []


def test_a_gpu_node_is_not_judged_on_cpu_floors():
    gpu_node = {"gpu": {"has_gpu": True, "vram_total_gb": 8.0}, "ram_gb": 2.0, "cpu_cores": 1}
    assert coordinator.cpu_node_shortfalls(gpu_node, FLOORS) == []


@pytest.mark.parametrize("changes, phrase", [
    ({"ram_gb": 4.0}, "at least 8 GB of RAM lent to DLNE (this machine lends 4 GB)"),
    ({"cpu_cores": 2}, "4 physical CPU cores (this machine has 2)"),
    ({"cpu_features": ["avx2"]}, "missing: FMA"),
    ({"cpu_features": []}, "missing: AVX2, FMA"),
    ({"cpu_features": ...}, "missing: AVX2, FMA"),
    ({"mem_bandwidth_gbs": 21.5}, "memory bandwidth of at least 30 GB/s (measured 21.5 GB/s)"),
    ({"mem_bandwidth_gbs": ...}, "none was reported"),
])
def test_each_shortfall_is_named(changes, phrase):
    missing = coordinator.cpu_node_shortfalls(node(**changes), FLOORS)
    assert len(missing) == 1 and phrase in missing[0], missing


def test_exactly_at_the_floor_qualifies():
    at_floor = node(ram_gb=8.0, cpu_cores=4, mem_bandwidth_gbs=30.0)
    assert coordinator.cpu_node_shortfalls(at_floor, FLOORS) == []


def test_every_shortfall_is_listed_at_once():
    poor = node(ram_gb=4.0, cpu_cores=2, cpu_features=["avx"], mem_bandwidth_gbs=12.0)
    assert len(coordinator.cpu_node_shortfalls(poor, FLOORS)) == 4


def test_arm_is_judged_on_bandwidth_not_x86_instructions():
    arm = node(cpu_arch="arm64", cpu_features=[], mem_bandwidth_gbs=100.0)
    assert coordinator.cpu_node_shortfalls(arm, FLOORS) == []


def test_operator_raised_floors_apply():
    stricter = {**FLOORS, "cpu_min_bandwidth_gbs": 60.0}
    assert "measured 48 GB/s" in coordinator.cpu_node_shortfalls(QUALIFYING, stricter)[0]


@pytest.mark.parametrize("raw, expected", [
    ({"CPU_MIN_RAM_GB": "12", "CPU_MIN_CORES": "8", "CPU_MIN_BANDWIDTH_GBS": "45"},
     {"cpu_min_ram_gb": 12.0, "cpu_min_cores": 8, "cpu_min_bandwidth_gbs": 45.0}),
    # Floors may only be raised: lowering one is how a slow machine drags the swarm down.
    ({"CPU_MIN_RAM_GB": "4", "CPU_MIN_CORES": "2", "CPU_MIN_BANDWIDTH_GBS": "10"}, FLOORS),
    ({"CPU_MIN_CORES": "lots", "CPU_MIN_BANDWIDTH_GBS": ""}, FLOORS),
])
def test_floors_from_config(monkeypatch, raw, expected):
    monkeypatch.setattr(engine_config, "get_config", lambda force_reload=False: raw)
    limits = engine_config.get_worker_limits()
    assert {k: limits[k] for k in FLOORS} == expected


# ======================================================================================
# The refusal, end to end
# ======================================================================================

def _heartbeat(monkeypatch, hardware_profile):
    from tests.test_coordinator_counter import FakeRequest
    monkeypatch.setattr(coordinator, "get_worker_limits", lambda: {
        "network_key": "", "allow_unsigned_workers": True, "trust_proxy_headers": False, **FLOORS})
    monkeypatch.setattr(coordinator.registry, "workers", {})
    payload = coordinator.HeartbeatPayload(worker_id="cpu-node", version=VERSION, port=50052,
                                           token="tok", hardware=hardware_profile)
    return asyncio.run(coordinator.worker_heartbeat(FakeRequest(client_host="10.0.0.5"), payload))


def test_a_qualifying_cpu_node_is_admitted(monkeypatch):
    assert _heartbeat(monkeypatch, dict(QUALIFYING)) == {"status": "ok"}


def test_a_slow_cpu_node_is_refused_with_the_reason_and_the_marker_header(monkeypatch):
    with pytest.raises(HTTPException) as refused:
        _heartbeat(monkeypatch, node(mem_bandwidth_gbs=18.0))
    assert refused.value.status_code == 403
    assert refused.value.headers == {"X-DLNE-Refusal": "cpu-minimum"}
    assert "measured 18 GB/s" in refused.value.detail
    assert refused.value.detail.startswith("This machine has no usable GPU")


@pytest.fixture
def worker_node(monkeypatch):
    monkeypatch.setattr(worker, "get_hardware_profile", lambda: dict(QUALIFYING))
    monkeypatch.setattr(worker, "_write_token_to_disk", lambda token: None)
    monkeypatch.setattr(worker, "HEARTBEAT_INTERVAL_SECONDS", 0.01)
    n = worker.WorkerNode("http://coordinator:11435")
    n.is_running = True
    return n


def _reply(n, monkeypatch, status, headers, body):
    posts = []

    def fake_post(url, json=None, timeout=None):
        posts.append(url)
        if len(posts) > 3:
            n.request_shutdown(0, "test finished")
        return httpx.Response(status, headers=headers, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(worker.httpx, "post", fake_post)
    return posts


def test_the_worker_stops_and_says_why_when_refused(worker_node, monkeypatch, capsys):
    reason = "This machine has no usable GPU, and a CPU-only node needs 4 physical CPU cores (this machine has 2)."
    posts = _reply(worker_node, monkeypatch, 403, {"X-DLNE-Refusal": "cpu-minimum"}, {"detail": reason})
    worker_node.heartbeat_loop()
    assert worker_node.exit_code == worker.CPU_BELOW_MINIMUM_EXIT_CODE == 78
    assert len(posts) == 1, "the hardware will not change; retrying only spams the coordinator"
    assert f"{worker.REFUSAL_MARKER}{reason}" in capsys.readouterr().out


def test_another_403_is_not_mistaken_for_the_cpu_refusal(worker_node, monkeypatch):
    posts = _reply(worker_node, monkeypatch, 403, {}, {"detail": "something else"})
    worker_node.heartbeat_loop()
    assert worker_node.exit_code != 78 and len(posts) > 1


def test_cpu_only_mode_finds_the_pinned_engine(monkeypatch, tmp_path):
    """allowedGpus 'none' used to look only for a ggml-rpc-server-cpu that no release ships."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = ".exe" if downloader.platform.system() == "Windows" else ""
    (bin_dir / f"ggml-rpc-server{exe}").write_bytes(b"")
    monkeypatch.setattr(downloader, "test_binary_compatibility", lambda path: True)
    monkeypatch.setattr(engine_config, "get_worker_limits", lambda: {"allowed_gpu_indices": "none"})
    import engine.config as cfg
    monkeypatch.setattr(cfg, "get_base_dir", lambda: str(tmp_path))
    found = downloader.download_and_extract_engine("worker")
    assert found == str(bin_dir / f"ggml-rpc-server{exe}")
