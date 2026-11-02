"""2.9.0, lent RAM: ram_lent_gb in the heartbeat, LEND_RAM_GB, and the CPU ggml-rpc-server that serves it."""

import pytest

from engine import hardware, worker

GPU_HW = {"gpu": {"has_gpu": True, "vram_total_gb": 8.0}, "is_yielding": False, "ram_gb": 4.0}


@pytest.mark.parametrize("setting,available,expected", [
    (8, 32, 8.0),          # the default, plenty free
    (8, 10, 6.0),          # free RAM less 4 GB is the ceiling
    (8, 4, 0.0),           # nothing above the 4 GB kept
    (8, 2, 0.0),           # never below 0
    (0, 32, 0.0),          # 0 turns it off
    (-3, 32, 0.0),
    (12.55, 64, 12.6),
    ("lots", 32, 0.0),
    (float("nan"), 32, 0.0),
])
def test_ram_lent_gb(setting, available, expected):
    assert hardware.ram_lent_gb(setting, available) == expected


def test_get_ram_lent_gb_reads_the_setting_and_free_ram(monkeypatch):
    class Mem:
        available = 20 * 1024 ** 3
    monkeypatch.setattr(hardware.psutil, "virtual_memory", lambda: Mem())
    monkeypatch.setattr(hardware, "get_worker_limits", lambda: {"lend_ram_gb": 8.0})
    assert hardware.get_ram_lent_gb() == 8.0
    monkeypatch.setattr(hardware, "get_worker_limits", lambda: {"lend_ram_gb": 30.0})
    assert hardware.get_ram_lent_gb() == 16.0
    monkeypatch.setattr(hardware, "get_worker_limits", lambda: {})
    assert hardware.get_ram_lent_gb() == 8.0, "no setting: the default 8"


def test_lend_ram_gb_in_config(monkeypatch):
    from engine import config
    for text, expected in (({}, 8.0), ({"LEND_RAM_GB": "0"}, 0.0), ({"LEND_RAM_GB": "12"}, 12.0),
                           ({"LEND_RAM_GB": "-2"}, 0.0), ({"LEND_RAM_GB": "x"}, 8.0), ({"LEND_RAM_GB": "nan"}, 8.0)):
        monkeypatch.setattr(config, "get_config", lambda t=text: dict(t))
        assert config.get_worker_limits()["lend_ram_gb"] == expected


class FakeProcess:
    def __init__(self, args=None, **kwargs):
        self.args = args
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.terminate()


@pytest.fixture
def node(monkeypatch, tmp_path):
    import engine.setup_core as setup_core
    monkeypatch.setattr(setup_core, "get_or_create_worker_id", lambda: "worker-under-test")
    monkeypatch.setattr(worker, "get_hardware_profile", lambda: dict(GPU_HW))
    monkeypatch.setattr(worker, "_write_token_to_disk", lambda token: None)
    monkeypatch.setattr(worker, "get_worker_limits", lambda: {"network_key": "k" * 64})
    monkeypatch.setattr(worker, "get_base_dir", lambda: str(tmp_path))
    started = []

    def popen(args, **kwargs):
        process = FakeProcess(args)
        started.append(process)
        return process

    monkeypatch.setattr(worker.subprocess, "Popen", popen)
    n = worker.WorkerNode("http://coordinator:11435", rpc_port=50052)
    n.is_running = True
    n.started = started
    n.rpc_process = FakeProcess()
    n._rpc_binary = "/engine/ggml-rpc-server"
    monkeypatch.setattr(worker, "get_ram_lent_gb", lambda: n.lent)
    monkeypatch.setattr(worker.WorkerNode, "_free_port_after", staticmethod(lambda port: port + 1))
    n.lent = 8.0
    yield n
    n._stopped = True


def test_cpu_server_runs_while_donating_and_is_its_own_endpoint(node):
    node._sync_cpu_rpc()
    assert len(node.started) == 1
    args = node.started[0].args
    assert args[0] == "/engine/ggml-rpc-server" and "--device" in args and args[args.index("--device") + 1] == "CPU"
    assert args[args.index("--port") + 1] == "50053"
    hw = node._build_heartbeat_payload()["hardware"]
    assert hw["ram_lent_gb"] == 8.0
    assert hw["endpoints"] == [{"type": "gpu", "port": 50052}, {"type": "cpu", "port": 50053, "ram_gb": 8.0}]


def test_no_cpu_server_when_nothing_is_lent(node):
    node.lent = 0.0
    node._sync_cpu_rpc()
    assert node.started == []
    hw = node._build_heartbeat_payload()["hardware"]
    assert hw["ram_lent_gb"] == 0.0 and hw["endpoints"] == [{"type": "gpu", "port": 50052}]


def test_setting_changes_start_and_stop_the_cpu_server(node):
    node._sync_cpu_rpc()
    first = node.started[0]
    node._sync_cpu_rpc()
    assert len(node.started) == 1, "already running: not started twice"
    node.lent = 0.0
    node._sync_cpu_rpc()
    assert first.terminated and node.cpu_rpc_process is None
    node.lent = 6.0
    node._sync_cpu_rpc()
    assert len(node.started) == 2
    assert node._build_heartbeat_payload()["hardware"]["ram_lent_gb"] == 6.0


def test_a_dead_cpu_server_is_started_again(node):
    node._sync_cpu_rpc()
    node.started[0].returncode = 1
    node._sync_cpu_rpc()
    assert len(node.started) == 2


def test_no_gpu_server_means_no_ram_lent(node):
    node.rpc_process = None
    node._sync_cpu_rpc()
    assert node.started == []
    assert node._build_heartbeat_payload()["hardware"]["ram_lent_gb"] == 0.0


def test_a_cpu_only_node_is_one_cpu_endpoint(node):
    node._rpc_cpu_only = True
    node._sync_cpu_rpc()
    assert node.started == []
    hw = node._build_heartbeat_payload()["hardware"]
    assert hw["ram_lent_gb"] == 8.0 and hw["endpoints"] == [{"type": "cpu", "port": 50052, "ram_gb": 8.0}]


def test_stopping_the_gpu_server_stops_the_cpu_one(node):
    node._sync_cpu_rpc()
    cpu = node.started[0]
    node._stop_rpc_server()
    assert cpu.terminated and node.cpu_rpc_port == 0
