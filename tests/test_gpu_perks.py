"""
GPU perks follow use. A contributor lending a GPU is first in line with no usage window while that GPU is
actually serving, or for GPU_STANDBY_GRACE_MINUTES (default 30) after it joined or last had a job. After
that its owner is a CPU contributor like any other (the CPU rolling window), and is never refused for it.
"""

import asyncio
import inspect
import json
import os

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from engine import config as engine_config
from engine import coordinator
from engine.priority import UsageWindow
from engine.standby import StandbyBook
from engine.version import VERSION
from tests.test_coding_endpoint import Clock, env, fakes  # noqa: F401

REMOTE = ("198.51.100.40", 40000)
MINUTE = 60.0


def gpu_hw(vram=24.0, **extra):
    return {"ram_gb": 32.0, "cpu_cores": 8, "cpu_threads": 16, "is_yielding": False,
            "gpu": {"has_gpu": True, "vram_total_gb": vram, "name": "RTX"}, **extra}


@pytest.fixture
def perks(env, monkeypatch, tmp_path):
    """The real tier middleware in front of a route that reports the tier it was handed, a registry the
    tests fill, and a clock they move. The standing-by book keeps its file in the test's own folder (and is
    not loaded, so it writes nothing, until a test loads it)."""
    env.limits.update({"enforce_contrib_clause": True, "starter_quota_inferences": 5, "cpu_coding_limit": 2,
                       "cpu_coding_window_hours": 5.0, "gpu_standby_grace_minutes": 30.0, "cpu_min_ram_gb": 8.0})
    monkeypatch.setattr(coordinator, "cpu_coding_window", UsageWindow())
    standby = Clock()
    standby.t = 1_700_000_000.0
    standby_file = str(tmp_path / "install" / "config" / "gpu_standby.json")
    monkeypatch.setattr(coordinator, "gpu_standby", StandbyBook(lambda: standby_file, standby))
    env.standby_file = standby_file

    probe = FastAPI()
    probe.add_middleware(coordinator.NonContribBlockerMiddleware)

    @probe.post("/v1/chat/completions")
    async def chat(request: Request):
        return {"tier": getattr(request.state, "dlne_tier", None)}

    @probe.post("/v1/completions")
    async def complete(request: Request):
        return {"tier": getattr(request.state, "dlne_tier", None)}

    def heartbeat(worker_id):
        """What the coordinator's heartbeat handler does after it records a GPU node."""
        coordinator._gpu_note_job_state(env.registry.workers[worker_id])

    def add(worker_id, hardware, ip=None, port=50053):
        ip = ip or f"10.5.0.{len(env.registry.workers) + 2}"
        env.registry.update_worker(worker_id, ip, port, f"tok-{worker_id}", hardware)
        heartbeat(worker_id)
        return f"{ip}:{port}"

    def member(fake, worker_id, **kwargs):
        env.add(fake, worker_id, **kwargs)
        heartbeat(worker_id)
        return worker_id

    def tier(worker_id, path="/v1/chat/completions"):
        async def ask():
            async with AsyncClient(transport=ASGITransport(app=probe, client=REMOTE), base_url="http://t",
                                   headers={"Authorization": f"Bearer tok-{worker_id}"}) as c:
                return await c.post(path)
        return asyncio.run(ask())

    env.add_worker, env.member, env.tier, env.standby = add, member, tier, standby
    return env


def tier_of(response):
    assert response.status_code == 200, response.text
    return response.json()["tier"]


def test_a_newly_registered_gpu_has_the_gpu_tier_at_once(perks):
    perks.add_worker("g1", gpu_hw())
    assert tier_of(perks.tier("g1")) == "gpu"


def test_with_no_job_it_keeps_the_perks_for_the_grace_and_then_is_a_cpu_contributor_without_being_refused(perks):
    perks.add_worker("g1", gpu_hw())
    assert tier_of(perks.tier("g1")) == "gpu"
    perks.standby.t += 29 * MINUTE
    assert tier_of(perks.tier("g1")) == "gpu", "29 minutes in"
    perks.standby.t += 2 * MINUTE
    first = perks.tier("g1")
    assert tier_of(first) == "cpu", "31 minutes in, nothing ever ran on it"
    assert first.headers["x-dlne-limit-remaining"] == "1", "the CPU window now applies"
    assert tier_of(perks.tier("g1")) == "cpu"
    third = perks.tier("g1")
    assert third.status_code == 429 and "retry-after" in third.headers, "its window is used up: 429, not a refusal"
    assert perks.tier("g1", "/v1/completions").status_code == 200, "autocomplete is never counted or refused"


def test_a_gpu_standing_by_is_never_refused_like_a_cpu_node_that_is_too_small(perks):
    """cpu_node_shortfalls would turn a node away for its CPU numbers; a node with a GPU is judged on the GPU."""
    perks.add_worker("g1", gpu_hw(cpu_cores=1, ram_gb=2.0))
    perks.standby.t += 3 * 60 * MINUTE
    response = perks.tier("g1")
    assert response.status_code == 200 and response.json()["tier"] == "cpu"


def test_getting_a_job_restores_the_perks_and_resets_the_clock(perks):
    endpoint = perks.add_worker("g1", gpu_hw())
    assert tier_of(perks.tier("g1")) == "gpu"
    perks.standby.t += 45 * MINUTE
    assert tier_of(perks.tier("g1")) == "cpu"
    perks.local.endpoints = [endpoint]                       # the engine places it
    assert tier_of(perks.tier("g1")) == "gpu"
    perks.standby.t += 3 * 60 * MINUTE
    assert tier_of(perks.tier("g1")) == "gpu", "for as long as it is used"
    perks.local.endpoints = []                               # the job ends; the clock starts again from here
    assert tier_of(perks.tier("g1")) == "gpu"
    perks.standby.t += 29 * MINUTE
    assert tier_of(perks.tier("g1")) == "gpu"
    perks.standby.t += 2 * MINUTE
    assert tier_of(perks.tier("g1")) == "cpu"


def test_a_node_of_the_running_engine_stays_a_gpu_contributor(perks):
    endpoint = perks.add_worker("g1", gpu_hw())
    perks.add_worker("g2", gpu_hw(8.0))
    perks.local.endpoints = [endpoint]
    perks.standby.t += 10 * 60 * MINUTE
    assert tier_of(perks.tier("g1")) == "gpu"
    assert tier_of(perks.tier("g2")) == "cpu", "a GPU in the same swarm with no job does not"


def test_a_ready_coding_member_stays_a_gpu_contributor_and_a_member_with_no_job_does_not(perks):
    perks.member(perks.fakes["a"], "ca")                    # verified and ready: it serves
    perks.member(perks.fakes["b"], "cb", ready=False)       # verified, still setting up
    perks.standby.t += 31 * MINUTE
    assert tier_of(perks.tier("ca")) == "gpu"
    assert tier_of(perks.tier("cb")) == "cpu"
    perks.member(perks.fakes["b"], "cb")                    # its model is up: it has a job
    assert tier_of(perks.tier("cb")) == "gpu"


def test_a_new_coding_member_has_the_grace_while_it_sets_up(perks):
    perks.member(perks.fakes["b"], "cb", ready=False)
    assert tier_of(perks.tier("cb")) == "gpu"
    perks.standby.t += 29 * MINUTE
    assert tier_of(perks.tier("cb")) == "gpu"


def test_a_claim_to_be_a_coding_member_that_does_not_verify_earns_no_lasting_perks(perks):
    perks.member(perks.fakes["c"], "cc", identity=None)     # claims pool "coding" with a block nobody signed
    perks.standby.t += 31 * MINUTE
    assert tier_of(perks.tier("cc")) == "cpu"


def test_a_coding_member_has_no_job_while_members_are_off(perks):
    perks.member(perks.fakes["a"], "ca")
    perks.standby.t += 3 * 60 * MINUTE
    assert tier_of(perks.tier("ca")) == "gpu", "serving"
    perks.limits["coding_members_enabled"] = False
    assert tier_of(perks.tier("ca")) == "gpu", "its grace starts when the job goes away"
    perks.standby.t += 31 * MINUTE
    assert tier_of(perks.tier("ca")) == "cpu"


def test_yielding_to_the_owner_is_not_idling(perks):
    endpoint = perks.add_worker("g1", gpu_hw())
    perks.standby.t += 29 * MINUTE
    assert tier_of(perks.tier("g1")) == "gpu"
    perks.add_worker("g1", gpu_hw(is_yielding=True), ip=endpoint.split(":")[0])        # the owner starts a game
    perks.standby.t += 2 * 60 * MINUTE
    assert tier_of(perks.tier("g1")) == "gpu", "two hours of gaming cost it nothing"
    perks.add_worker("g1", gpu_hw(), ip=endpoint.split(":")[0])                         # the game ends
    assert tier_of(perks.tier("g1")) == "gpu"
    perks.standby.t += 29 * MINUTE
    assert tier_of(perks.tier("g1")) == "gpu", "the grace starts over when it comes back"
    perks.standby.t += 2 * MINUTE
    assert tier_of(perks.tier("g1")) == "cpu"


def test_a_cpu_contributor_is_unchanged(perks):
    perks.add_worker("c1", {"ram_gb": 16.0, "cpu_cores": 8, "cpu_threads": 16, "gpu": None, "cpu_arch": "x86_64",
                            "cpu_features": ["avx2", "fma"], "mem_bandwidth_gbs": 48.0, "is_yielding": False})
    assert tier_of(perks.tier("c1")) == "cpu"


def test_the_grace_is_configurable_and_zero_means_none(perks):
    perks.add_worker("g1", gpu_hw())
    perks.limits["gpu_standby_grace_minutes"] = 5.0
    perks.standby.t += 4 * MINUTE
    assert tier_of(perks.tier("g1")) == "gpu"
    perks.standby.t += 2 * MINUTE
    assert tier_of(perks.tier("g1")) == "cpu"
    perks.add_worker("g2", gpu_hw(8.0))
    perks.limits["gpu_standby_grace_minutes"] = 0.0
    assert tier_of(perks.tier("g2")) == "cpu"


def test_the_heartbeat_keeps_the_clock_so_it_starts_when_the_swarm_first_sees_the_gpu(perks, monkeypatch):
    from tests.test_coordinator_counter import FakeRequest
    monkeypatch.setattr(coordinator, "_gpu_has_job", lambda worker: worker["ip"] == "10.9.0.2")
    perks.limits.update({"network_key": "", "allow_unsigned_workers": True})

    def beat(worker_id, ip):
        payload = coordinator.HeartbeatPayload(worker_id=worker_id, version=VERSION, port=50053,
                                               token=f"tok-{worker_id}", hardware=gpu_hw())
        return asyncio.run(coordinator.worker_heartbeat(FakeRequest(client_host=ip), payload))

    beat("idle", "10.9.0.1")
    beat("busy", "10.9.0.2")
    assert set(coordinator.gpu_standby.tracked()) == {"idle"}, "only the one with no job has a clock"
    started = coordinator.gpu_standby.since("idle")
    perks.standby.t += 10 * MINUTE
    beat("idle", "10.9.0.1")
    assert coordinator.gpu_standby.since("idle") == started, "a later heartbeat does not restart it"


def test_the_tracking_table_is_bounded(perks):
    coordinator.gpu_standby.max_tracked = 5
    for i in range(12):
        perks.standby.t += 1
        coordinator._gpu_note_job_state({"worker_id": f"w{i}", "ip": "10.7.0.1", "port": i, "hardware": gpu_hw()})
    assert len(coordinator.gpu_standby) <= 5


def test_a_node_with_no_id_is_never_judged(perks):
    assert coordinator._gpu_note_job_state({"hardware": gpu_hw()}) is None
    assert coordinator._gpu_keeps_perks({"hardware": gpu_hw()}, perks.limits) is True


def test_the_grace_setting_is_read_from_config(monkeypatch):
    def limits_for(raw):
        monkeypatch.setattr(engine_config, "get_config", lambda force_reload=False: raw)
        return engine_config.get_worker_limits()["gpu_standby_grace_minutes"]

    assert limits_for({}) == 30.0
    assert limits_for({"GPU_STANDBY_GRACE_MINUTES": "45"}) == 45.0
    assert limits_for({"GPU_STANDBY_GRACE_MINUTES": "0"}) == 0.0
    for bad in ("-1", "soon", "nan", "99999", ""):
        assert limits_for({"GPU_STANDBY_GRACE_MINUTES": bad}) == 30.0, bad


# ======================================================================================
# A coordinator restart keeps the grace (engine/standby.py)
# ======================================================================================

def restart(perks, monkeypatch):
    """What a restart is: a clean shutdown saves the clocks, a new process starts with a new book and loads them."""
    asyncio.run(coordinator.shutdown_event())
    book = StandbyBook(lambda: perks.standby_file, perks.standby)
    monkeypatch.setattr(coordinator, "gpu_standby", book)
    return coordinator._load_gpu_standby()


def test_a_restart_in_the_middle_of_the_grace_does_not_start_it_again(perks, monkeypatch):
    coordinator._load_gpu_standby()
    perks.add_worker("g1", gpu_hw())
    perks.standby.t += 20 * MINUTE
    assert tier_of(perks.tier("g1")) == "gpu"
    perks.standby.t += 1 * MINUTE
    assert restart(perks, monkeypatch) == 1
    perks.standby.t += 5 * MINUTE                       # 26 minutes since it began standing by
    assert tier_of(perks.tier("g1")) == "gpu"
    perks.standby.t += 5 * MINUTE                       # 31: a fresh 30 would still have 25 to go
    assert tier_of(perks.tier("g1")) == "cpu"


def test_without_the_file_a_restart_would_start_the_grace_again(perks, monkeypatch):
    """The control: what the same steps do when nothing was saved."""
    perks.add_worker("g1", gpu_hw())
    perks.standby.t += 25 * MINUTE
    monkeypatch.setattr(coordinator, "gpu_standby", StandbyBook(lambda: perks.standby_file, perks.standby))
    perks.standby.t += 10 * MINUTE
    assert tier_of(perks.tier("g1")) == "gpu"


def test_a_gpu_with_a_job_when_the_coordinator_stopped_has_no_clock_when_it_returns(perks, monkeypatch):
    coordinator._load_gpu_standby()
    endpoint = perks.add_worker("g1", gpu_hw())
    perks.standby.t += 40 * MINUTE
    perks.local.endpoints = [endpoint]
    assert tier_of(perks.tier("g1")) == "gpu"
    assert restart(perks, monkeypatch) == 0, "a worker with a job has no entry"
    perks.local.endpoints = []                          # the engine did not come back with it
    assert tier_of(perks.tier("g1")) == "gpu", "a fresh grace, from the first sight"


def test_a_worker_not_seen_for_a_day_comes_back_to_a_fresh_grace(perks, monkeypatch):
    coordinator._load_gpu_standby()
    perks.add_worker("g1", gpu_hw())
    perks.standby.t += 2 * MINUTE
    asyncio.run(coordinator.shutdown_event())
    perks.standby.t += 25 * 60 * MINUTE
    assert restart(perks, monkeypatch) == 0
    assert tier_of(perks.tier("g1")) == "gpu"


@pytest.mark.parametrize("content", [b"", b"{", b"\xff\xfe\x00 not json", b"[]", b'{"version": 1, "workers": 5}'])
def test_a_damaged_file_means_no_history_and_never_stops_the_coordinator(perks, content):
    os.makedirs(os.path.dirname(perks.standby_file))
    with open(perks.standby_file, "wb") as handle:
        handle.write(content)
    assert coordinator._load_gpu_standby() == 0
    perks.add_worker("g1", gpu_hw())
    assert tier_of(perks.tier("g1")) == "gpu"
    asyncio.run(coordinator.shutdown_event())
    with open(perks.standby_file, encoding="utf-8") as handle:
        assert list(json.load(handle)["workers"]) == ["g1"], "and the file is whole again"


def test_nothing_is_written_until_the_coordinator_has_started(perks):
    perks.add_worker("g1", gpu_hw())
    perks.standby.t += 31 * MINUTE
    assert tier_of(perks.tier("g1")) == "cpu"
    asyncio.run(coordinator.shutdown_event())
    assert not os.path.exists(os.path.dirname(perks.standby_file)), "a book that was never loaded writes nothing"


def test_the_coordinator_loads_the_clocks_at_start_and_saves_them_at_shutdown():
    assert "_load_gpu_standby()" in inspect.getsource(coordinator.startup_event)
    assert "gpu_standby.flush()" in inspect.getsource(coordinator.shutdown_event)


def test_the_real_book_keeps_its_file_in_the_installation_config_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(coordinator, "get_base_dir", lambda: str(tmp_path))
    assert coordinator.gpu_standby.path_fn() == os.path.join(str(tmp_path), "config", "gpu_standby.json")
