"""
Engine flavours on the worker's side (DLNE 2.7.0, docs/release-2.7-contract.md sections 1 and 7).

* engine/hardware.py ``cuda_capability()``: what ``nvidia-smi`` says, turned into "may this machine run the CUDA engine, and
  if not, why" -- an RTX 40 laptop, an old driver, an old GPU, no NVIDIA GPU, several GPUs, Windows, an nvidia-smi too old
  to know compute_cap.
* engine/engine_flavour.py: the CUDA engine's start-up check and fall-back, the DLNE_CUDA / DLNE_ENGINE lines, and that a
  machine keeps only the flavour it runs.
* engine/worker.py: the RPC server and the coding member start the right engine, say so, fall back; the heartbeat carries
  ``engine_flavour``; the coordinator counts it.

No GPU, no network, no model: nvidia-smi, the downloader and the engine processes are fakes. What these cannot show is
a real CUDA engine on a real driver; that was run once by hand on the RTX 4070 (see the 2.7.0 report).
"""

import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from engine import authn, coding_member as cm, engine_flavour as ef, hardware, registry, worker
from engine.coding_member import CodingMember
from tests.coding_member_fakes import (
    FakeDownloader, free_port, make_tier, recorded_starts, wait_for, write_fake_coding_binary,
)

LAPTOP = "NVIDIA GeForce RTX 4070 Laptop GPU, 8.9, 615.78.08"
MIN_DRIVER = hardware.CUDA13_MIN_DRIVER          # builder A may correct it; the tests follow the constant


def smi(monkeypatch, outputs, system="Linux", found=True, existing=()):
    """Fake nvidia-smi. ``outputs`` is a list of (returncode, stdout) answers, one per call; a string is a bare stdout.
    Returns the list of field lists asked for."""
    asked = []
    answers = list(outputs)
    monkeypatch.setattr(hardware, "_cuda_capability_cache", None)
    monkeypatch.setattr(hardware.platform, "system", lambda: system)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/nvidia-smi" if found else None)
    monkeypatch.setattr(hardware.os.path, "exists", lambda path: path in existing)

    def run(command, **kwargs):
        asked.append(command[1])
        answer = answers.pop(0)
        code, out = answer if isinstance(answer, tuple) else (0, answer)
        return SimpleNamespace(returncode=code, stdout=out, stderr="")

    monkeypatch.setattr(hardware.subprocess, "run", run)
    return asked


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.setattr(hardware, "_cuda_capability_cache", None)


# ======================================================================================
# cuda_capability(): nvidia-smi in, a verdict in plain words out
# ======================================================================================

def test_the_laptops_rtx_4070_on_driver_615_may_use_cuda(monkeypatch):
    asked = smi(monkeypatch, [LAPTOP + "\n"])
    assert hardware.cuda_capability() == {
        "capable": True, "reason": "", "gpu": "NVIDIA GeForce RTX 4070 Laptop GPU", "compute_cap": "8.9",
        "driver": "615.78.08"}
    assert asked == ["--query-gpu=name,compute_cap,driver_version"]


def test_an_old_driver_is_not_enough_and_says_so(monkeypatch):
    smi(monkeypatch, ["NVIDIA GeForce RTX 3060, 8.6, 572.16\n"])
    answer = hardware.cuda_capability()
    assert answer["capable"] is False and answer["driver"] == "572.16" and answer["compute_cap"] == "8.6"
    assert answer["reason"] == f"driver 572.16 is older than CUDA 13 needs ({MIN_DRIVER} or later)"


def test_the_driver_minimum_itself_and_anything_above_it_is_enough(monkeypatch):
    smi(monkeypatch, [f"NVIDIA GeForce RTX 3060, 8.6, {MIN_DRIVER}\n"])
    assert hardware.cuda_capability()["capable"] is True
    smi(monkeypatch, ["NVIDIA GeForce RTX 3060, 8.6, 579.99\n"])
    assert hardware.cuda_capability()["capable"] is False, "579.99 is older than 580"
    smi(monkeypatch, ["NVIDIA GeForce RTX 5090, 12.0, 1000.5\n"])
    assert hardware.cuda_capability()["capable"] is True, "versions are numbers, not text (1000 > 580)"


def test_a_gpu_older_than_turing_is_not_capable_whatever_its_driver(monkeypatch):
    smi(monkeypatch, ["NVIDIA GeForce GTX 1080, 6.1, 615.78\n"])
    answer = hardware.cuda_capability()
    assert answer["capable"] is False
    assert answer["reason"] == "this GPU is older than Turing (compute capability 6.1)"
    smi(monkeypatch, ["NVIDIA GeForce GTX 1650, 7.5, 615.78\n"])
    assert hardware.cuda_capability()["capable"] is True, "7.5 is Turing itself"
    smi(monkeypatch, ["NVIDIA Tesla V100, 7.0, 615.78\n"])
    assert hardware.cuda_capability()["capable"] is False, "Volta stays on Vulkan"


def test_no_nvidia_smi_means_no_nvidia_gpu(monkeypatch):
    asked = smi(monkeypatch, [], found=False)
    assert hardware.cuda_capability() == {"capable": False, "reason": "no NVIDIA GPU", "gpu": "", "compute_cap": "",
                                          "driver": ""}
    assert asked == [], "nothing is run when there is no nvidia-smi"


def test_nvidia_smi_that_fails_or_lists_nothing_means_no_nvidia_gpu(monkeypatch):
    smi(monkeypatch, [(9, ""), (9, "")])
    assert hardware.cuda_capability()["reason"] == "no NVIDIA GPU"
    smi(monkeypatch, ["\n"])
    assert hardware.cuda_capability()["reason"] == "no NVIDIA GPU"


def test_nvidia_smi_that_hangs_or_cannot_be_run_is_no_gpu(monkeypatch):
    smi(monkeypatch, [])

    def hang(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 8)

    monkeypatch.setattr(hardware.subprocess, "run", hang)
    assert hardware.cuda_capability()["reason"] == "no NVIDIA GPU"
    monkeypatch.setattr(hardware, "_cuda_capability_cache", None)
    monkeypatch.setattr(hardware.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no exec")))
    assert hardware.cuda_capability()["reason"] == "no NVIDIA GPU"


def test_the_probe_has_a_short_timeout(monkeypatch):
    seen = {}
    smi(monkeypatch, [])

    def run(command, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(returncode=0, stdout=LAPTOP, stderr="")

    monkeypatch.setattr(hardware.subprocess, "run", run)
    hardware.cuda_capability()
    assert 0 < seen["timeout"] <= 10


def test_with_several_gpus_the_newest_architecture_is_judged(monkeypatch):
    smi(monkeypatch, ["NVIDIA GeForce GTX 1060, 6.1, 615.78\nNVIDIA GeForce RTX 4070, 8.9, 615.78\n"])
    answer = hardware.cuda_capability()
    assert answer["capable"] is True and answer["gpu"] == "NVIDIA GeForce RTX 4070"
    smi(monkeypatch, ["NVIDIA GeForce RTX 2060, 7.5, 572.16\nNVIDIA GeForce GTX 1060, 6.1, 572.16\n"])
    answer = hardware.cuda_capability()
    assert answer["gpu"] == "NVIDIA GeForce RTX 2060" and "driver 572.16" in answer["reason"]


def test_a_gpu_name_with_a_comma_does_not_confuse_the_parser(monkeypatch):
    smi(monkeypatch, ["NVIDIA RTX A2000, Laptop GPU, 8.6, 615.78\n"])
    answer = hardware.cuda_capability()
    assert answer["gpu"] == "NVIDIA RTX A2000, Laptop GPU" and answer["compute_cap"] == "8.6" and answer["capable"]


def test_windows_finds_nvidia_smi_in_system32_and_reads_crlf_output(monkeypatch):
    system32 = r"C:\Windows\System32\nvidia-smi.exe"
    seen = []
    smi(monkeypatch, [], system="Windows", found=False, existing=(system32,))

    def run(command, **kwargs):
        seen.append((command[0], kwargs.get("creationflags")))
        return SimpleNamespace(returncode=0, stdout="NVIDIA GeForce RTX 4060, 8.9, 572.16\r\n", stderr="")

    monkeypatch.setattr(hardware.subprocess, "run", run)
    monkeypatch.setattr(hardware.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    answer = hardware.cuda_capability()
    assert seen == [(system32, 0x08000000)], "found by its Windows path, with no console window"
    assert answer["driver"] == "572.16" and answer["capable"] is False


def test_windows_without_nvidia_smi_anywhere_is_no_gpu(monkeypatch):
    smi(monkeypatch, [], system="Windows", found=False, existing=())
    assert hardware.cuda_capability()["reason"] == "no NVIDIA GPU"


def test_a_driver_too_old_to_know_compute_cap_still_names_itself(monkeypatch):
    asked = smi(monkeypatch, [(2, ""), "NVIDIA GeForce GTX 970, 390.157\n"])
    answer = hardware.cuda_capability()
    assert asked == ["--query-gpu=name,compute_cap,driver_version", "--query-gpu=name,driver_version"]
    assert answer["capable"] is False and answer["driver"] == "390.157" and answer["compute_cap"] == ""
    assert "compute capability could not be read" in answer["reason"]


def test_the_answer_is_read_once_per_process(monkeypatch):
    asked = smi(monkeypatch, [LAPTOP, LAPTOP])
    first = hardware.cuda_capability()
    first["capable"] = False                      # a caller changing its copy must not change the cache
    assert hardware.cuda_capability()["capable"] is True and len(asked) == 1
    hardware.cuda_capability(refresh=True)
    assert len(asked) == 2


def test_a_probe_that_breaks_is_not_capable_and_never_raises(monkeypatch):
    smi(monkeypatch, [])
    monkeypatch.setattr(hardware, "_read_nvidia_gpus", lambda: (_ for _ in ()).throw(ValueError("boom")))
    answer = hardware.cuda_capability()
    assert answer["capable"] is False and "ValueError" in answer["reason"]


# ======================================================================================
# The engine flavour: what DLNE_ENGINE_FLAVOR asks for, and what the machine is given
# ======================================================================================

CAPABLE = {"capable": True, "reason": "", "gpu": "NVIDIA GeForce RTX 4070 Laptop GPU", "compute_cap": "8.9",
           "driver": "615.78"}
NOT_CAPABLE = {"capable": False, "reason": "no NVIDIA GPU", "gpu": "", "compute_cap": "", "driver": ""}


def lines(capsys, marker):
    return [json.loads(l[len(marker):]) for l in capsys.readouterr().out.splitlines() if l.startswith(marker)]


class Engines:
    """Fake engine folders below an install: ``fetch(flavour)`` makes the folder and returns its ggml-rpc-server."""

    def __init__(self, tmp_path, cuda_ok=True, cuda_devices="Available devices:\n  CUDA0: NVIDIA GeForce RTX 4070 (7888 MiB)\n",
                 check_code=0):
        self.base = str(tmp_path)
        self.fetches = []
        self.checks = []
        self.cuda_ok, self.cuda_devices, self.check_code = cuda_ok, cuda_devices, check_code

    def folder(self, flavour):
        return os.path.join(self.base, ef.FLAVOUR_FOLDERS[flavour])

    def fetch(self, flavour):
        self.fetches.append(flavour)
        if flavour == "cuda" and not self.cuda_ok:
            return None
        os.makedirs(self.folder(flavour), exist_ok=True)
        for name in ("ggml-rpc-server", "llama-server"):
            path = os.path.join(self.folder(flavour), name)
            if not os.path.exists(path):          # a test may have put its own stand-in llama-server there
                with open(path, "w") as handle:
                    handle.write(flavour)
        return os.path.join(self.folder(flavour), "ggml-rpc-server")

    def run(self, command, **kwargs):
        self.checks.append(command)
        return SimpleNamespace(returncode=self.check_code, stdout=self.cuda_devices, stderr="")


def flavour_for(engines, asked="cuda", capability=CAPABLE, hybrid=False, fetch=None):
    return ef.EngineFlavour(engines.base, asked, fetch or engines.fetch, hybrid=hybrid,
                            capability=lambda: dict(capability), run=engines.run)


def test_only_cuda_is_cuda_anything_else_is_vulkan():
    assert ef.requested_flavour({"DLNE_ENGINE_FLAVOR": "cuda"}) == "cuda"
    assert ef.requested_flavour({"DLNE_ENGINE_FLAVOR": " CUDA "}) == "cuda"
    for other in ("vulkan", "", "auto", "rocm", "cuda13", None):
        assert ef.requested_flavour({"DLNE_ENGINE_FLAVOR": other} if other is not None else {}) == "vulkan"


def test_a_capable_machine_that_asked_for_cuda_gets_the_cuda_engine(tmp_path):
    engines = Engines(tmp_path)
    flavour = flavour_for(engines)
    path = flavour.acquire()
    assert path == os.path.join(engines.folder("cuda"), "ggml-rpc-server") and engines.fetches == ["cuda"]
    assert flavour.running == "cuda" and flavour.failure == ""
    assert flavour.bin_dir() == engines.folder("cuda")
    assert engines.checks == [[os.path.join(engines.folder("cuda"), "llama-server"), "--list-devices"]]


@pytest.mark.parametrize("asked,capability", [("vulkan", CAPABLE), ("cuda", NOT_CAPABLE)])
def test_vulkan_is_given_quietly_when_cuda_was_not_asked_for_or_is_not_possible(tmp_path, capsys, asked, capability):
    engines = Engines(tmp_path)
    flavour = flavour_for(engines, asked, capability)
    assert flavour.acquire() == os.path.join(engines.folder("vulkan"), "ggml-rpc-server")
    flavour.confirm()
    assert engines.fetches == ["vulkan"] and engines.checks == [] and flavour.failure == ""
    assert lines(capsys, ef.ENGINE_MARKER) == [{"flavour": "vulkan", "fallback": ""}]


def test_a_hybrid_worker_is_always_vulkan_and_never_deletes_its_hosts_folder(tmp_path, capsys):
    engines = Engines(tmp_path)
    os.makedirs(engines.folder("cuda"))
    flavour = flavour_for(engines, "cuda", hybrid=True)
    flavour.acquire()
    flavour.confirm()
    assert engines.fetches == ["vulkan"] and os.path.isdir(engines.folder("cuda"))


def test_the_capability_line_is_printed_once_per_change(tmp_path, capsys):
    engines = Engines(tmp_path)
    state = dict(CAPABLE)
    flavour = ef.EngineFlavour(engines.base, "cuda", engines.fetch, capability=lambda: dict(state), run=engines.run)
    flavour.announce_capability()
    flavour.announce_capability()
    state.update(capable=False, reason="driver 572.16 is older than CUDA 13 needs (580 or later)", driver="572.16")
    flavour.announce_capability()
    got = lines(capsys, ef.CUDA_MARKER)
    assert got == [CAPABLE, state]
    assert set(got[0]) == {"capable", "reason", "gpu", "compute_cap", "driver"}


def test_the_exact_marker_lines(tmp_path, capsys):
    engines = Engines(tmp_path)
    flavour = flavour_for(engines)
    flavour.announce_capability()
    flavour.acquire()
    flavour.confirm()
    out = capsys.readouterr().out.splitlines()
    assert out == [
        'DLNE_CUDA: {"capable":true,"reason":"","gpu":"NVIDIA GeForce RTX 4070 Laptop GPU","compute_cap":"8.9","driver":"615.78"}',
        'DLNE_ENGINE: {"flavour":"cuda","fallback":""}',
    ]


def test_a_cuda_engine_that_sees_no_cuda_device_falls_back_and_says_why(tmp_path, capsys, monkeypatch):
    warnings = []
    monkeypatch.setattr(ef, "log_warning", warnings.append)
    engines = Engines(tmp_path, cuda_devices="Available devices:\n  Vulkan0: AMD (8 GiB)\n")
    flavour = flavour_for(engines)
    assert flavour.acquire() == os.path.join(engines.folder("vulkan"), "ggml-rpc-server")
    flavour.confirm()
    assert flavour.running == "vulkan" and flavour.failure == "the CUDA engine found no CUDA device"
    assert warnings == ["CUDA could not start: the CUDA engine found no CUDA device; running on Vulkan."]
    assert lines(capsys, ef.ENGINE_MARKER) == [{"flavour": "vulkan", "fallback": "the CUDA engine found no CUDA device"}]


def test_a_cuda_engine_that_will_not_start_falls_back_with_its_exit_code(tmp_path):
    engines = Engines(tmp_path, cuda_devices="error while loading shared libraries: libcudart.so.13\n", check_code=127)
    flavour = flavour_for(engines)
    flavour.acquire()
    assert flavour.running == "vulkan"
    assert "exit code 127" in flavour.failure and "libcudart.so.13" in flavour.failure


def test_a_check_that_cannot_run_or_hangs_falls_back(tmp_path):
    for error, words in ((OSError(8, "Exec format error"), "Exec format error"),
                         (subprocess.TimeoutExpired("x", 60), "did not answer")):
        engines = Engines(tmp_path)
        engines.run = lambda command, error=error, **kw: (_ for _ in ()).throw(error)
        flavour = flavour_for(engines)
        flavour.acquire()
        assert flavour.running == "vulkan" and words in flavour.failure


def test_a_cuda_engine_that_cannot_be_had_falls_back(tmp_path):
    engines = Engines(tmp_path, cuda_ok=False)
    flavour = flavour_for(engines)
    assert flavour.acquire() == os.path.join(engines.folder("vulkan"), "ggml-rpc-server")
    assert flavour.running == "vulkan" and "could not be downloaded" in flavour.failure


def test_a_downloader_from_before_the_flavours_falls_back_to_vulkan(tmp_path):
    engines = Engines(tmp_path)

    def old_downloader(flavour):
        if flavour == "cuda":
            raise TypeError("download_and_extract_engine() got an unexpected keyword argument 'flavour'")
        return engines.fetch(flavour)

    flavour = flavour_for(engines, fetch=old_downloader)
    assert flavour.acquire().endswith("ggml-rpc-server") and flavour.running == "vulkan" and flavour.failure


def test_cuda_is_not_tried_again_in_this_process_after_a_failure(tmp_path):
    engines = Engines(tmp_path, cuda_devices="nothing\n")
    flavour = flavour_for(engines)
    flavour.acquire()
    flavour.acquire()
    flavour.fall_back("again")
    assert engines.fetches == ["cuda", "vulkan", "vulkan"] and flavour.failure == "the CUDA engine found no CUDA device"


def test_the_device_check_is_done_once_per_engine_folder(tmp_path):
    engines = Engines(tmp_path)
    flavour = flavour_for(engines)
    flavour.acquire()
    flavour.acquire()
    assert len(engines.checks) == 1


# ---- a machine keeps only the flavour it runs -----------------------------------------------------------

def test_the_other_flavours_folder_goes_only_after_the_new_engine_runs(tmp_path):
    engines = Engines(tmp_path)
    engines.fetch("vulkan")                         # an install that has been running Vulkan
    flavour = flavour_for(engines)
    flavour.acquire()
    assert os.path.isdir(engines.folder("vulkan")), "still there while the CUDA engine has not run"
    flavour.confirm()
    assert not os.path.exists(engines.folder("vulkan")) and os.path.isdir(engines.folder("cuda"))


def test_a_fall_back_removes_the_cuda_files_it_could_not_use(tmp_path):
    engines = Engines(tmp_path, cuda_devices="no\n")
    flavour = flavour_for(engines)
    flavour.acquire()
    assert os.path.isdir(engines.folder("cuda"))
    flavour.confirm()
    assert not os.path.exists(engines.folder("cuda")) and os.path.isdir(engines.folder("vulkan"))


def test_switching_back_to_vulkan_removes_the_cuda_folder(tmp_path):
    engines = Engines(tmp_path)
    engines.fetch("cuda")
    flavour = flavour_for(engines, "vulkan")
    flavour.acquire()
    flavour.confirm()
    assert not os.path.exists(engines.folder("cuda")) and os.path.isdir(engines.folder("vulkan"))


def test_cleanup_stays_inside_the_install_and_never_touches_the_folder_in_use(tmp_path):
    engines = Engines(tmp_path)
    flavour = flavour_for(engines, "vulkan")
    flavour.acquire()
    keep = tmp_path / "unrelated.txt"
    keep.write_text("x")
    flavour.path = os.path.join(engines.folder("cuda"), "ggml-rpc-server")      # cuda "in use" while vulkan confirmed
    os.makedirs(engines.folder("cuda"))
    flavour.confirmed = "vulkan"
    flavour.running = "vulkan"
    flavour.confirm()                              # other == cuda == the folder in use: kept
    assert os.path.isdir(engines.folder("cuda")) and keep.exists()


def test_the_engine_line_is_printed_again_when_the_flavour_changes(tmp_path, capsys):
    engines = Engines(tmp_path)
    flavour = flavour_for(engines)
    flavour.acquire()
    flavour.confirm()
    flavour.confirm()                               # unchanged: silent
    flavour.fall_back("the CUDA RPC server did not start")
    flavour.confirm()
    assert lines(capsys, ef.ENGINE_MARKER) == [{"flavour": "cuda", "fallback": ""},
                                               {"flavour": "vulkan", "fallback": "the CUDA RPC server did not start"}]


def test_the_downloaders_own_folder_layout_wins_when_it_has_one(tmp_path, monkeypatch):
    from engine import downloader
    monkeypatch.setattr(downloader, "engine_dir", lambda base, flavour: os.path.join(base, "engines", flavour),
                        raising=False)
    assert ef.flavour_folder(str(tmp_path), "cuda") == os.path.join(str(tmp_path), "engines", "cuda")


def test_the_downloaders_own_removal_is_used_when_it_has_one(tmp_path, monkeypatch):
    from engine import downloader
    removed = []
    monkeypatch.setattr(downloader, "remove_engine_flavour", lambda base, flavour: removed.append(flavour), raising=False)
    engines = Engines(tmp_path)
    engines.fetch("vulkan")
    flavour = flavour_for(engines)
    flavour.acquire()
    flavour.confirm()
    assert removed == ["vulkan"], "the downloader removes it (this fake only records the call)"


# ======================================================================================
# engine/worker.py: the RPC server, the markers, the heartbeat
# ======================================================================================

KEY = "a-network-key-for-tests"
GPU_HW = {"gpu": {"has_gpu": True, "vram_total_gb": 8.0}, "ram_gb": 30.0, "cpu_cores": 8, "cpu_threads": 16,
          "is_yielding": False}


@pytest.fixture
def node_factory(monkeypatch, tmp_path):
    import engine.setup_core as setup_core
    monkeypatch.setattr(setup_core, "get_or_create_worker_id", lambda: "worker-under-test")
    monkeypatch.setattr(worker, "get_hardware_profile", lambda: dict(GPU_HW))
    monkeypatch.setattr(worker, "_write_token_to_disk", lambda token: None)
    monkeypatch.setattr(worker, "get_worker_limits", lambda: {"network_key": KEY})
    monkeypatch.setattr(worker, "get_base_dir", lambda: str(tmp_path))
    monkeypatch.setattr(worker, "_rpc_server_supports_cache", lambda binary: False)
    monkeypatch.setattr(hardware, "cuda_capability", lambda refresh=False: dict(CAPABLE))
    # start_rpc_server asks vulkaninfo which cards to use unless this is set (and Popen is faked in these tests).
    monkeypatch.setenv("GGML_VK_VISIBLE_DEVICES", "0")
    made = []

    def make(flavour="cuda"):
        if flavour is None:
            monkeypatch.delenv("DLNE_ENGINE_FLAVOR", raising=False)
        else:
            monkeypatch.setenv("DLNE_ENGINE_FLAVOR", flavour)
        node = worker.WorkerNode("http://coordinator:11435")
        node.is_running = True
        node._stopped_for_test = True
        made.append(node)
        return node

    yield make
    for node in made:
        node._stopped = True            # keep the atexit hook quiet
        if getattr(node, "rpc_log_file", None):
            node.rpc_log_file.close()


class RpcFakes:
    """The downloader and Popen for start_rpc_server: each start is recorded; ``banners`` says, per start, what the
    RPC server writes to its log (the first line announces it)."""

    def __init__(self, monkeypatch, engines, banners):
        self.engines, self.banners, self.popens, self.stopped = engines, list(banners), [], []
        monkeypatch.setattr(worker, "download_and_extract_engine", self.download)
        monkeypatch.setattr(worker.subprocess, "Popen", self.popen)
        monkeypatch.setattr(worker, "read_rpc_banner", self.read_banner)

    def download(self, role, coordinator_url=None, flavour="vulkan"):
        return self.engines.fetch(flavour)

    def popen(self, args, **kwargs):
        proc = SimpleNamespace(args=args, returncode=None, poll=lambda: None, terminate=lambda: self.stopped.append(args),
                               wait=lambda timeout=None: 0, kill=lambda: None)
        self.popens.append(proc)
        return proc

    def read_banner(self, log_path, offset, process=None, timeout=10.0, poll=0.1):
        return self.banners.pop(0)


def test_the_rpc_server_runs_on_cuda_where_it_may(node_factory, monkeypatch, tmp_path, capsys):
    engines = Engines(tmp_path)
    engines.fetch("vulkan")
    fakes = RpcFakes(monkeypatch, engines, ["8.0.1"])
    node = node_factory("cuda")
    monkeypatch.setattr(ef.EngineFlavour, "capability", lambda self: dict(CAPABLE))
    monkeypatch.setattr(node.engine_flavour, "_run", engines.run)
    assert node.start_rpc_server() is True
    assert [p.args[0] for p in fakes.popens] == [os.path.join(engines.folder("cuda"), "ggml-rpc-server")]
    assert lines(capsys, ef.ENGINE_MARKER) == [{"flavour": "cuda", "fallback": ""}]
    assert not os.path.exists(engines.folder("vulkan")), "the machine keeps only the flavour it runs"
    assert node.rpc_proto == "8.0.1"


def test_a_cuda_rpc_server_that_never_announces_itself_is_replaced_by_the_vulkan_one(node_factory, monkeypatch, tmp_path,
                                                                                    capsys):
    engines = Engines(tmp_path)
    fakes = RpcFakes(monkeypatch, engines, ["", "8.0.1"])
    node = node_factory("cuda")
    monkeypatch.setattr(ef.EngineFlavour, "capability", lambda self: dict(CAPABLE))
    monkeypatch.setattr(node.engine_flavour, "_run", engines.run)
    assert node.start_rpc_server() is True
    assert [p.args[0] for p in fakes.popens] == [os.path.join(engines.folder("cuda"), "ggml-rpc-server"),
                                                 os.path.join(engines.folder("vulkan"), "ggml-rpc-server")]
    assert len(fakes.stopped) == 1, "the CUDA server was stopped before Vulkan started"
    assert lines(capsys, ef.ENGINE_MARKER) == [{"flavour": "vulkan", "fallback": "the CUDA RPC server did not start"}]
    assert node.rpc_proto == "8.0.1"
    assert not os.path.exists(engines.folder("cuda"))


def test_a_cuda_rpc_server_that_cannot_be_launched_is_replaced_too(node_factory, monkeypatch, tmp_path, capsys):
    engines = Engines(tmp_path)
    calls = []
    RpcFakes(monkeypatch, engines, ["8.0.1"])

    def popen(args, **kwargs):
        calls.append(args[0])
        if len(calls) == 1:
            raise OSError(8, "Exec format error")
        return SimpleNamespace(returncode=None, poll=lambda: None, terminate=lambda: None, wait=lambda timeout=None: 0)

    monkeypatch.setattr(worker.subprocess, "Popen", popen)
    node = node_factory("cuda")
    monkeypatch.setattr(ef.EngineFlavour, "capability", lambda self: dict(CAPABLE))
    monkeypatch.setattr(node.engine_flavour, "_run", engines.run)
    assert node.start_rpc_server() is True
    assert [os.path.basename(os.path.dirname(c)) for c in calls] == ["bin-cuda", "bin"]
    assert "would not start" in lines(capsys, ef.ENGINE_MARKER)[0]["fallback"]


def test_a_vulkan_machine_starts_vulkan_and_says_nothing_about_cuda(node_factory, monkeypatch, tmp_path, capsys):
    engines = Engines(tmp_path)
    fakes = RpcFakes(monkeypatch, engines, ["8.0.1"])
    node = node_factory(None)                      # no DLNE_ENGINE_FLAVOR at all
    assert node.start_rpc_server() is True
    assert [p.args[0] for p in fakes.popens] == [os.path.join(engines.folder("vulkan"), "ggml-rpc-server")]
    assert lines(capsys, ef.ENGINE_MARKER) == [{"flavour": "vulkan", "fallback": ""}]
    assert engines.checks == [], "no CUDA check on a Vulkan machine"


def test_a_machine_that_may_not_use_cuda_gets_vulkan_quietly(node_factory, monkeypatch, tmp_path, capsys):
    engines = Engines(tmp_path)
    RpcFakes(monkeypatch, engines, ["8.0.1"])
    node = node_factory("cuda")
    monkeypatch.setattr(ef.EngineFlavour, "capability", lambda self: dict(NOT_CAPABLE))
    logged = []
    monkeypatch.setattr(ef, "log_warning", logged.append)
    assert node.start_rpc_server() is True
    assert engines.fetches == ["vulkan"] and logged == []
    assert lines(capsys, ef.ENGINE_MARKER) == [{"flavour": "vulkan", "fallback": ""}]


def test_a_node_that_will_not_use_a_gpu_never_asks_for_cuda(node_factory, monkeypatch):
    monkeypatch.setattr(worker, "get_worker_limits", lambda: {"network_key": KEY, "allowed_gpu_indices": "none"})
    assert node_factory("cuda").engine_flavour.requested == "vulkan"
    monkeypatch.setattr(worker, "get_worker_limits", lambda: {"network_key": KEY})
    monkeypatch.setattr(worker, "get_hardware_profile", lambda: {**GPU_HW, "gpu": {"has_gpu": False}})
    assert node_factory("cuda").engine_flavour.requested == "vulkan"


def test_the_node_fetches_cuda_with_the_flavour_argument_and_vulkan_without_it(node_factory, monkeypatch):
    calls = []
    monkeypatch.setattr(worker, "download_and_extract_engine", lambda *a, **k: calls.append((a, k)) or "x")
    node = node_factory("cuda")
    node._fetch_engine("vulkan")
    node._fetch_engine("cuda")
    assert calls == [(("worker",), {"coordinator_url": "http://coordinator:11435"}),
                     (("worker",), {"coordinator_url": "http://coordinator:11435", "flavour": "cuda"})]


def test_start_prints_the_cuda_line_before_any_engine_is_fetched(node_factory, monkeypatch, tmp_path, capsys):
    engines = Engines(tmp_path)
    order = []
    node = node_factory("cuda")
    monkeypatch.setattr(ef.EngineFlavour, "capability", lambda self: dict(CAPABLE))
    monkeypatch.setattr(node, "start_rpc_server", lambda: order.append(("engine", capsys.readouterr().out)) or False)
    monkeypatch.setattr(node, "request_shutdown", lambda *a, **k: node._shutdown_event.set())
    monkeypatch.setattr(node, "_stop_on_termination_signal", lambda: None)
    monkeypatch.setattr(node, "stop", lambda: None)
    monkeypatch.setattr(worker.sys, "exit", lambda code=0: None)
    node.start()
    (kind, printed), = order
    assert printed.startswith(ef.CUDA_MARKER) and json.loads(printed[len(ef.CUDA_MARKER):])["capable"] is True


def test_the_heartbeat_profile_carries_the_engine_flavour(node_factory, monkeypatch, tmp_path):
    engines = Engines(tmp_path)
    RpcFakes(monkeypatch, engines, ["8.0.1"])
    node = node_factory("cuda")
    monkeypatch.setattr(ef.EngineFlavour, "capability", lambda self: dict(CAPABLE))
    monkeypatch.setattr(node.engine_flavour, "_run", engines.run)
    assert node._build_heartbeat_payload()["hardware"]["engine_flavour"] == "vulkan", "until a CUDA engine has run"
    node.start_rpc_server()
    payload = node._build_heartbeat_payload()
    assert payload["hardware"]["engine_flavour"] == "cuda"
    assert authn.validate_hardware_profile(payload["hardware"]) == (True, "") or authn.validate_hardware_profile(
        payload["hardware"])[0] is True
    node.rpc_log_file.close()


# ---- the coordinator's side: accepted, stored, counted ----------------------------------------------------

def test_only_the_two_flavour_names_are_accepted_in_a_profile():
    base = {**GPU_HW, "cpu_arch": "x86_64", "cpu_features": ["avx2"]}
    assert authn.validate_hardware_profile(base)[0] is True, "the plain profile is fine"
    assert authn.validate_hardware_profile({**base, "engine_flavour": "cuda"})[0] is True
    assert authn.validate_hardware_profile({**base, "engine_flavour": "vulkan"})[0] is True
    assert authn.validate_hardware_profile({**base, "engine_flavour": "rocm"})[0] is False
    assert authn.validate_hardware_profile({**base, "engine_flavour": 1})[0] is False


def test_the_registry_counts_workers_per_engine_flavour():
    reg = registry.WorkerRegistry()
    for worker_id, extra in (("a", {"engine_flavour": "cuda"}), ("b", {"engine_flavour": "vulkan"}), ("c", {}),
                             ("d", {"engine_flavour": "cuda"}), ("e", {"engine_flavour": "cuda", "companion_of": "d"})):
        reg.workers[worker_id] = {"hardware": {**GPU_HW, **extra}, "ip": "1.2.3.4", "port": 1, "last_seen": 0}
    assert reg.engine_flavour_counts() == {"vulkan": 2, "cuda": 2}, "a worker that says nothing runs Vulkan; a companion is not counted"


def test_the_gpu_inventory_names_the_flavour_of_each_node():
    reg = registry.WorkerRegistry()
    reg.prune_dead_workers = lambda: None
    for worker_id, flavour in (("a", "cuda"), ("b", None)):
        profile = {**GPU_HW, "gpu": {**GPU_HW["gpu"], "count": 1}}
        if flavour:
            profile["engine_flavour"] = flavour
        reg.workers[worker_id] = {"hardware": profile, "ip": "1.2.3.4", "port": 1, "last_seen": 0}
    rows = {row["worker_id"]: row["engine_flavour"] for row in reg.gpu_inventory()}
    assert rows == {"a": "cuda", "b": "vulkan"}


# ======================================================================================
# The coding member: a CUDA server that dies while starting is started again on Vulkan
# ======================================================================================

DATA = b"GGUF coding model " * 64


def build_member(tmp_path, cuda_crashes, devices="  CUDA0: NVIDIA (7888 MiB)\n"):
    base = tmp_path / "dlne"
    record = str(tmp_path / "starts.jsonl")
    engines = Engines(base)
    engines.cuda_devices = devices
    # The two engine folders hold stand-in llama-servers: the CUDA one crashes (or works), the Vulkan one works.
    engines.fetch("vulkan"), engines.fetch("cuda")
    write_fake_coding_binary(os.path.join(engines.folder("vulkan"), "llama-server"), record)
    write_fake_coding_binary(os.path.join(engines.folder("cuda"), "llama-server"), record + ".cuda", crash=cuda_crashes)
    flavour = flavour_for(engines)
    tier = make_tier("coder-a", [DATA])
    member = CodingMember(str(base), lambda: [tier], port=free_port(),
                          download=FakeDownloader({p["file"]: DATA for p in tier["files"]}),
                          measure=lambda url, client: (30.0, 100.0), poll_seconds=0.05, ready_timeout=20.0,
                          engine=flavour)
    assert member.start()
    return SimpleNamespace(member=member, flavour=flavour, engines=engines, tier=tier, record=record)


def settled(member, timeout=30.0):
    assert wait_for(lambda: not member._busy, timeout), "the member never finished converging"


def test_a_coding_member_runs_its_model_on_the_cuda_engine_when_that_works(tmp_path, capsys):
    env = build_member(tmp_path, cuda_crashes=False)
    try:
        env.flavour.acquire()
        env.member.assign(env.tier)
        settled(env.member)
        assert env.member.status()["ready"] is True
        assert recorded_starts(env.record + ".cuda") and not recorded_starts(env.record)
        assert lines(capsys, ef.ENGINE_MARKER) == [{"flavour": "cuda", "fallback": ""}]
        assert not os.path.exists(env.engines.folder("vulkan"))
    finally:
        env.member.stop()


def test_a_cuda_model_server_that_dies_while_starting_is_started_again_on_vulkan(tmp_path, capsys):
    env = build_member(tmp_path, cuda_crashes=True)
    try:
        env.flavour.acquire()
        assert env.flavour.running == "cuda"
        env.member.assign(env.tier)
        settled(env.member)
        assert env.member.status()["ready"] is True, "the model is served, on Vulkan"
        assert len(recorded_starts(env.record + ".cuda")) == 1 and len(recorded_starts(env.record)) == 1
        assert lines(capsys, ef.ENGINE_MARKER) == [
            {"flavour": "vulkan", "fallback": "the CUDA model server stopped while starting"}]
        assert env.flavour.failure and env.flavour.running == "vulkan"
        assert env.member._backoff == {}, "a CUDA failure is not counted against the model"
        assert not os.path.exists(env.engines.folder("cuda"))
    finally:
        env.member.stop()


def test_a_coding_member_keeps_the_flavour_its_heartbeat_named():
    from engine import coding_router
    records = [{"worker_id": "a", "ip": "1.2.3.4", "port": 1, "hardware": {"engine_flavour": "cuda"}},
               {"worker_id": "b", "ip": "1.2.3.5", "port": 1, "hardware": {}}]
    router = coding_router.CodingRouter()
    found = router.all_members(SimpleNamespace(coding_members=lambda: records), lambda ip, port: None, {})
    assert {m.worker_id: m.engine_flavour for m in found} == {"a": "cuda", "b": "vulkan"}
