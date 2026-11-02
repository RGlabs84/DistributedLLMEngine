"""
The phones' own engine (engine/phone_engine.py): the pins, the config keys, where the layers go (the home budget never
grows, photos stay home), the command line, and the lifecycle against a fake llama-server on a real loopback socket.
Every wait is bounded; no real server is started.
"""

import asyncio
import os
import re
import stat
import time

import pytest

from engine import config as cfg
from engine import phone_engine as pe
from engine.image_member import MemberError
from engine.phone_engine import PhoneEngine, phone_rungs, plan_layout, resolve_budget_mb, valid_endpoint, volunteer_need_gb
from tests.phone_fakes import FakeLlamaServer, Spawner


@pytest.fixture(scope="module")
def llama():
    server = FakeLlamaServer().start()
    yield server
    server.stop()


def limits_of(**over):
    base = dict(cfg.PHONE_ENGINE_DEFAULTS)
    base.update(phone_engine="own", media_gpu="0000:0c:00.0")
    base.update(over)
    return base


def gpu(vid, gb, endpoint=None):
    return {"id": vid, "kind": "gpu", "vram_gb": gb, "endpoint": endpoint or f"10.0.0.{len(vid)}:{50000 + len(vid)}"}


def cpu(vid, gb, endpoint=None):
    return {"id": vid, "kind": "cpu", "ram_lent_gb": gb, "endpoint": endpoint or f"10.0.1.{len(vid)}:{51000 + len(vid)}"}


def plan(rung_id, volunteers, **over):
    limits = limits_of(**over)
    rung = next(r for r in phone_rungs() if r["id"] == rung_id)
    return plan_layout(rung, volunteers, budget_mb=resolve_budget_mb(limits), lanes=limits["phone_lanes"],
                       context=limits["phone_context"], vision=limits["phone_vision"])


# ------------------------------------------------------------------ pins and config
def test_every_rung_is_pinned_by_revision_size_and_sha256():
    rungs = phone_rungs()
    assert [r["id"] for r in rungs] == ["qwen35-4b", "qwen35-9b", "qwen36-a3b"]
    for rung in rungs:
        assert len(rung["files"]) == 2
        for entry in rung["files"]:
            assert re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
            assert isinstance(entry["size"], int) and entry["size"] > 0
            assert re.fullmatch(r"https://huggingface\.co/unsloth/[A-Za-z0-9.\-]+-GGUF/resolve/[0-9a-f]{40}/[A-Za-z0-9._\-]+", entry["url"])
        assert rung["file"] == rung["files"][0]["file"] and rung["mmproj"] == rung["files"][1]["file"]
    four = rungs[0]["files"]
    assert (four[0]["size"], four[0]["sha256"][:8]) == (2740937888, "00fe7986")
    assert (four[1]["size"], four[1]["sha256"][:8]) == (672423616, "cd88edcf")


def test_the_qwen36_rung_is_the_same_file_the_coding_ladder_pins():
    import json
    tiers = json.load(open(os.path.join(os.path.dirname(__file__), "..", "scripts", "coding_models.json")))["tiers"]
    tier = next(t for t in tiers if t["id"] == "qwen36-a3b-iq4xs")
    entry = phone_rungs()[2]["files"][0]
    assert (entry["file"], entry["url"], entry["size"], entry["sha256"]) == (tier["file"], tier["url"], tier["size"], tier["sha256"])


def test_defaults_change_nothing_and_the_keys_parse_with_ranges():
    assert cfg.PHONE_ENGINE_DEFAULTS["phone_engine"] == "shared"
    out = dict(cfg.PHONE_ENGINE_DEFAULTS)
    cfg.apply_phone_engine_config({"PHONE_ENGINE": "OWN", "PHONE_MODEL": "qwen35-9b", "PHONE_LANES": "3", "PHONE_CONTEXT": "999999",
                                   "PHONE_VISION": "false", "PHONE_SHARD_MAX_RTT_MS": "150", "PHONE_MIN_TPS": "nan",
                                   "PHONE_UP_STABLE_SECONDS": "10", "PHONE_DEVICE": " cpu "}, out)
    assert out["phone_engine"] == "own" and out["phone_model"] == "qwen35-9b" and out["phone_lanes"] == 3
    assert out["phone_context"] == 8192            # out of range: default kept
    assert out["phone_vision"] is False and out["phone_shard_max_rtt_ms"] == 150.0 and out["phone_device"] == "cpu"
    assert out["phone_min_tps"] == 6.0 and out["phone_up_stable_seconds"] == 600
    bad = dict(cfg.PHONE_ENGINE_DEFAULTS)
    cfg.apply_phone_engine_config({"PHONE_ENGINE": "both", "PHONE_MODEL": "gpt"}, bad)
    assert bad["phone_engine"] == "shared" and bad["phone_model"] == "qwen35-4b"


def test_endpoints_are_host_and_port_only():
    assert valid_endpoint("10.0.0.5:50052") and valid_endpoint("peer.example.com:1")
    for bad in ("10.0.0.5", "a b:5", "x:99999", "x:0", "x:5,y:6", "--rpc:5", "", None, "host:port", "a;b:5"):
        assert not valid_endpoint(bad)


# ------------------------------------------------------------------ where the layers go
def test_the_base_rung_runs_on_the_home_card_alone_and_the_auto_budget_is_what_it_uses():
    p = plan("qwen35-4b", [])
    assert p["ok"] and p["home_layers"] == 32 and p["volunteer_layers"] == {} and p["need_mb"] == 0
    assert resolve_budget_mb(limits_of()) == pytest.approx(p["home_mb"])


@pytest.mark.parametrize("rung_id", ["qwen35-4b", "qwen35-9b", "qwen36-a3b"])
@pytest.mark.parametrize("volunteers", [[], [gpu("a", 8)], [gpu("a", 24)], [cpu("c", 32)], [gpu("a", 6), cpu("c", 12)],
                                        [gpu("a", 24), gpu("bb", 24), cpu("c", 64)]])
@pytest.mark.parametrize("vision", [True, False])
def test_the_home_card_stays_within_its_budget_at_every_rung(rung_id, volunteers, vision):
    limits = limits_of(phone_vision=vision)
    budget = resolve_budget_mb(limits)
    p = plan(rung_id, volunteers, phone_vision=vision, phone_home_budget_mb=budget)
    assert p["home_mb"] <= budget + 1e-6
    if p["ok"]:
        assert p["home_layers"] + sum(p["volunteer_layers"].values()) == next(r for r in phone_rungs() if r["id"] == rung_id)["layers"]


def test_the_budget_never_grows_with_the_volunteers():
    few, many = plan("qwen35-9b", [gpu("a", 24)]), plan("qwen35-9b", [gpu("a", 24), gpu("bb", 24)])
    assert few["ok"] and many["ok"] and few["home_layers"] == many["home_layers"] and few["home_mb"] == many["home_mb"]
    assert plan("qwen36-a3b", [gpu("a", 48)])["home_layers"] < 40


def test_a_rung_without_enough_volunteers_does_not_fit_and_says_what_is_missing():
    assert not plan("qwen35-9b", [])["ok"]
    assert not plan("qwen36-a3b", [gpu("a", 4)])["ok"]
    assert plan("qwen35-9b", [gpu("a", 4)])["ok"]
    assert volunteer_need_gb(phone_rungs()[0], limits_of()) == 0
    assert 0 < volunteer_need_gb(phone_rungs()[1], limits_of()) < volunteer_need_gb(phone_rungs()[2], limits_of())


def test_gpus_are_filled_before_lent_ram_and_bad_endpoints_are_skipped():
    p = plan("qwen35-9b", [cpu("c", 32), gpu("g", 24), {"id": "x", "kind": "gpu", "vram_gb": 99, "endpoint": "bad endpoint"}])
    assert set(p["volunteer_layers"]) == {"g"}


def test_a_home_budget_too_small_for_the_photo_reader_is_refused_not_moved_to_a_volunteer():
    p = plan("qwen35-4b", [gpu("a", 24)], phone_home_budget_mb=500)
    assert not p["ok"] and "photo reader" in p["reason"]
    assert plan("qwen35-4b", [gpu("a", 24)], phone_home_budget_mb=500, phone_vision=False)["ok"]


# ------------------------------------------------------------------ the engine
class Env:
    pass


@pytest.fixture
def env(tmp_path, llama):
    llama.reset()
    e = Env()
    e.base, e.llama = tmp_path, llama
    binary = tmp_path / "llama-server"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    models = tmp_path / "models" / "phone"
    models.mkdir(parents=True)
    for rung in phone_rungs():
        for entry in rung["files"]:
            with open(models / entry["file"], "wb") as handle:
                handle.truncate(entry["size"])         # sparse: the right size, no disk used
    e.limits = limits_of()
    e.card = {"vk_index": 2, "free_gb": 6.5, "total_gb": 8.0}
    e.spawner = Spawner()
    e.cwd_listings = []
    original = e.spawner.__call__

    def spawn(command, **kwargs):
        e.cwd_listings.append(sorted(os.listdir(kwargs["cwd"])))
        return original(command, **kwargs)

    e.engine = PhoneEngine(limits=lambda: e.limits, base_dir=lambda: str(tmp_path), spawn=spawn,
                           media_spec=lambda limits: limits.get("media_gpu", ""), card_of=lambda spec: e.card,
                           binary=lambda: str(binary), poll_seconds=0.01, ready_timeout=3.0, free_port=lambda: llama.port)
    e.rung = lambda rid: next(r for r in phone_rungs() if r["id"] == rid)
    return e


@pytest.mark.anyio
async def test_shared_mode_starts_nothing_and_reports_the_coding_engine(env):
    env.limits = limits_of(phone_engine="shared", engine_phone_slots=2)
    assert env.engine.unavailable_reason() == "PHONE_ENGINE is not own"
    with pytest.raises(MemberError):
        await env.engine.start(env.rung("qwen35-4b"), [])
    assert env.spawner.starts == []
    assert env.engine.status() == {"mode": "shared", "rung": "coding-engine", "device": "coding", "lanes": 2,
                                   "layers": {"home": None, "volunteers": 0}, "vision": None}


@pytest.mark.anyio
async def test_the_base_command_env_and_directory(env):
    await env.engine.start(env.rung("qwen35-4b"), [])
    start = env.spawner.starts[0]
    command = start["command"]
    assert command[0] == str(env.base / "llama-server")
    assert command[command.index("-m") + 1].endswith("Qwen3.5-4B-Q4_K_M.gguf")
    assert command[command.index("--host") + 1] == "127.0.0.1" and "--no-webui" in command
    assert command[command.index("-np") + 1] == "2" and command[command.index("-c") + 1] == str(2 * 8192)
    assert command[command.index("--device") + 1] == "Vulkan0" and "--rpc" not in command and "-ts" not in command
    assert command[command.index("--mmproj") + 1].endswith("Qwen3.5-4B-mmproj-F16.gguf")
    assert command[command.index("--mmproj-device") + 1] == "Vulkan0"
    assert start["env"]["GGML_VK_VISIBLE_DEVICES"] == "2"
    assert env.cwd_listings[0] == []                    # an empty private directory
    assert os.path.basename(start["cwd"]) == "run"
    workdir = os.path.dirname(start["cwd"])
    assert os.path.isdir(workdir)
    assert env.engine.status() == {"mode": "own", "rung": "qwen35-4b", "device": "0000:0c:00.0", "lanes": 2,
                                   "layers": {"home": 32, "volunteers": 0}, "vision": True}
    await env.engine.stop()
    assert not os.path.exists(workdir) and env.spawner.processes[0].terminated
    assert env.engine.status()["rung"] is None


@pytest.mark.anyio
async def test_photos_stay_home_the_reader_is_on_the_home_device_and_never_in_the_rpc_list(env):
    volunteers = [gpu("a", 24, "10.1.1.1:50052"), cpu("c", 32, "10.2.2.2:50053")]
    await env.engine.start(env.rung("qwen35-9b"), volunteers)
    command = env.spawner.starts[0]["command"]
    assert command[command.index("--mmproj-device") + 1] == "Vulkan0"
    assert command[command.index("--rpc") + 1] == "10.1.1.1:50052"          # the cpu one is not needed: the GPU holds it
    devices = command[command.index("--device") + 1].split(",")
    assert devices[0] == "Vulkan0" and devices[1:] == ["RPC0"]
    split = [int(n) for n in command[command.index("-ts") + 1].split(",")]
    assert split[0] == env.engine.layout["home_layers"] and sum(split) == 32
    assert env.engine.layout["home_mb"] <= resolve_budget_mb(env.limits) + 1e-6
    st = env.engine.status()
    assert st["layers"] == {"home": split[0], "volunteers": 32 - split[0]} and st["rung"] == "qwen35-9b"
    await env.engine.stop()


@pytest.mark.anyio
async def test_no_vision_means_no_reader_anywhere(env):
    env.limits = limits_of(phone_vision=False)
    await env.engine.start(env.rung("qwen35-4b"), [])
    assert "--mmproj" not in env.spawner.starts[0]["command"]
    assert env.engine.status()["vision"] is False and not env.engine.can_read_photos()
    await env.engine.stop()


@pytest.mark.anyio
async def test_cpu_device_runs_on_the_host_with_no_card_and_no_volunteers(env):
    env.limits = limits_of(phone_device="cpu")
    env.card = None
    await env.engine.start(env.rung("qwen35-4b"), [gpu("a", 24)])
    command = env.spawner.starts[0]["command"]
    assert command[command.index("--device") + 1] == "none" and command[command.index("-ngl") + 1] == "0"
    assert "--rpc" not in command and "--mmproj-device" not in command
    await env.engine.stop()


@pytest.mark.anyio
async def test_it_does_not_start_without_room_files_or_a_card(env):
    env.card = {"vk_index": 2, "free_gb": 1.0, "total_gb": 8.0}
    with pytest.raises(MemberError):
        await env.engine.start(env.rung("qwen35-4b"), [])
    env.card = None
    with pytest.raises(MemberError):
        await env.engine.start(env.rung("qwen35-4b"), [])
    env.card = {"vk_index": 2, "free_gb": 6.5, "total_gb": 8.0}
    os.truncate(env.base / "models" / "phone" / "Qwen3.5-4B-Q4_K_M.gguf", 10)
    assert "missing" in env.engine.unavailable_reason(env.rung("qwen35-4b"))
    with pytest.raises(MemberError):
        await env.engine.start(env.rung("qwen35-4b"), [])
    assert env.spawner.starts == []


@pytest.mark.anyio
async def test_a_layout_that_does_not_fit_is_refused_before_anything_starts(env):
    with pytest.raises(MemberError):
        await env.engine.start(env.rung("qwen36-a3b"), [gpu("a", 2)])
    assert env.spawner.starts == []


@pytest.mark.anyio
async def test_a_server_that_never_gets_ready_is_stopped_after_the_timeout_and_three_failures_pause_it(env):
    env.llama.loading = True
    env.engine._ready_override = 0.3
    for _ in range(3):
        began = time.monotonic()
        with pytest.raises(MemberError):
            await env.engine.start(env.rung("qwen35-4b"), [])
        assert time.monotonic() - began < 5
    assert all(p.terminated for p in env.spawner.processes) and env.engine.process is None
    assert "paused" in env.engine.unavailable_reason(env.rung("qwen35-4b"))
    assert len(env.spawner.starts) == 3
    with pytest.raises(MemberError):
        await env.engine.start(env.rung("qwen35-4b"), [])
    assert len(env.spawner.starts) == 3


@pytest.mark.anyio
async def test_a_crash_while_starting_fails_fast(env):
    original = env.spawner.__call__

    def dying(command, **kwargs):
        process = original(command, **kwargs)
        process.die(1)
        return process
    env.engine._spawn = dying
    began = time.monotonic()
    with pytest.raises(MemberError):
        await env.engine.start(env.rung("qwen35-4b"), [])
    assert time.monotonic() - began < 2


@pytest.mark.anyio
async def test_phones_are_not_let_in_until_the_engine_is_opened_and_lanes_are_counted(env):
    await env.engine.start(env.rung("qwen35-4b"), [])
    assert env.engine.lanes_free() == 0 and not env.engine.acquire() and env.engine.base_url is None
    env.engine.open_for_phones()
    assert env.engine.lanes_free() == 2 and env.engine.can_read_photos()
    async with env.engine.lane() as url:
        assert url == f"http://127.0.0.1:{env.llama.port}" and env.engine.lanes_free() == 1
        async with env.engine.lane() as second:
            assert second is not None and env.engine.lanes_free() == 0
            async with env.engine.lane() as third:
                assert third is None
    assert env.engine.lanes_free() == 2 and env.engine.in_flight == 0
    env.spawner.processes[0].die(1)
    assert env.engine.lanes_free() == 0 and env.engine.process_died()
    await env.engine.stop()


@pytest.mark.anyio
async def test_the_speed_is_timed_by_the_server_itself(env):
    await env.engine.start(env.rung("qwen35-4b"), [])
    env.llama.tps = 23.5
    assert await env.engine.measure_tps() == pytest.approx(23.5)
    assert env.llama.bodies[0]["n_predict"] == 48 and env.llama.bodies[0]["cache_prompt"] is False
    await env.engine.stop()
    assert await env.engine.measure_tps() is None


@pytest.mark.anyio
async def test_a_switch_up_passes_the_speed_check_and_lets_phones_in(env):
    await env.engine.switch(env.rung("qwen35-4b"), [])
    env.llama.tps = 12.0
    result = await env.engine.switch(env.rung("qwen35-9b"), [gpu("a", 24)], check_speed=True, abort_if_busy=True)
    assert result.status == "swapped" and env.engine.rung["id"] == "qwen35-9b" and env.engine.lanes_free() == 2
    await env.engine.stop()


@pytest.mark.anyio
async def test_a_switch_too_slow_puts_the_base_back_alone_and_keeps_phones_off_the_slow_one(env):
    await env.engine.switch(env.rung("qwen35-4b"), [])
    env.llama.tps = 2.0
    result = await env.engine.switch(env.rung("qwen35-9b"), [gpu("a", 24)], check_speed=True)
    assert result.status == "failed" and "under the floor" in result.detail
    assert env.engine.rung["id"] == "qwen35-4b" and env.engine.volunteer_ids == [] and env.engine.lanes_free() == 2
    names = [os.path.basename(s["command"][s["command"].index("-m") + 1]) for s in env.spawner.starts]
    assert names == ["Qwen3.5-4B-Q4_K_M.gguf", "Qwen3.5-9B-Q4_K_M.gguf", "Qwen3.5-4B-Q4_K_M.gguf"]
    await env.engine.stop()


@pytest.mark.anyio
async def test_a_switch_that_cannot_start_puts_the_base_back(env):
    await env.engine.switch(env.rung("qwen35-4b"), [])
    result = await env.engine.switch(env.rung("qwen36-a3b"), [gpu("a", 1)], check_speed=True)
    assert result.status == "failed" and env.engine.rung["id"] == "qwen35-4b" and env.engine.lanes_free() == 2
    await env.engine.stop()


@pytest.mark.anyio
async def test_a_switch_never_cuts_an_answer_when_asked_not_to(env):
    await env.engine.switch(env.rung("qwen35-4b"), [])
    assert env.engine.acquire()
    began = time.monotonic()
    result = await env.engine.switch(env.rung("qwen35-9b"), [gpu("a", 24)], drain_seconds=0.2, abort_if_busy=True)
    assert result.status == "busy" and time.monotonic() - began < 3
    assert env.engine.rung["id"] == "qwen35-4b" and len(env.spawner.starts) == 1
    assert env.engine.lanes_free() == 1                    # still open for phones: nothing was touched
    env.engine.release()
    await env.engine.stop()


@pytest.mark.anyio
async def test_a_step_down_waits_a_bounded_time_then_goes_ahead_and_new_requests_wait_meanwhile(env):
    await env.engine.switch(env.rung("qwen35-9b"), [gpu("a", 24)])
    assert env.engine.acquire()
    task = asyncio.ensure_future(env.engine.switch(env.rung("qwen35-4b"), [], drain_seconds=0.3))
    await asyncio.sleep(0.05)
    assert env.engine.lanes_free() == 0 and not env.engine.acquire()      # draining: no new lane
    result = await asyncio.wait_for(task, 10)
    assert result.status == "swapped" and env.engine.rung["id"] == "qwen35-4b"
    await env.engine.stop()


@pytest.mark.anyio
async def test_stop_now_ends_the_child_without_a_loop(env):
    await env.engine.start(env.rung("qwen35-4b"), [])
    process = env.spawner.processes[0]
    env.engine.stop_now()
    assert process.terminated and env.engine.process is None
