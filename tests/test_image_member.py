"""
The image engine's lifecycle (engine/image_member.py) against a fake sd-server on a real loopback socket: the command it is
started with, its environment and directory, lazy start, idle unload, the media card's memory, a timeout, a crash, a
cancel, and the three-strikes cool-down.
"""

import asyncio
import json
import os
import stat
import sys
import textwrap
import time

import pytest

from engine import image_member as member_module
from engine import media
from engine.image_member import Cancelled, ImageMember, MemberError
from tests.image_fakes import FakeSdServer, Spawner

SMALL_MODELS = {"diffusion": ("d.gguf", 11), "vae": ("v.safetensors", 7), "llm": ("l.gguf", 5)}


@pytest.fixture(scope="module")
def sd():
    server = FakeSdServer().start()
    yield server
    server.stop()


class Env:
    pass


@pytest.fixture
def env(tmp_path, monkeypatch, sd):
    sd.reset()
    monkeypatch.setattr(member_module, "MODEL_FILES", SMALL_MODELS)
    e = Env()
    e.base = tmp_path
    (tmp_path / "bin-sd").mkdir()
    binary = tmp_path / "bin-sd" / "sd-server"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    models = tmp_path / "models" / "image"
    models.mkdir(parents=True)
    for name, size in SMALL_MODELS.values():
        (models / name).write_bytes(b"x" * size)
    e.limits = {"image_gen_enabled": True, "media_gpu": "0000:0c:00.0", "image_model_dir": "", "image_threads": 4,
                "image_cpu_mask": "", "image_idle_unload_minutes": 20, "image_job_timeout_seconds": 300}
    e.card = {"vk_index": 3, "free_gb": 6.5, "total_gb": 8.0}
    e.offset = [0.0]
    e.sd = sd
    e.spawner = Spawner()
    e.cwd_listings = []
    original = e.spawner.__call__

    def spawn(command, **kwargs):
        e.cwd_listings.append(sorted(os.listdir(kwargs["cwd"])))
        return original(command, **kwargs)

    e.member = ImageMember(limits=lambda: e.limits, base_dir=lambda: str(tmp_path), spawn=spawn,
                           media_spec=lambda limits: limits.get("media_gpu", ""), media_card=lambda spec: e.card,
                           clock=lambda: time.monotonic() + e.offset[0], poll_seconds=0.01, ready_timeout=3.0,
                           cancel_wait=0.4, free_port=lambda: sd.port)
    return e


async def draw(e, prompt="a red fox", width=768, height=768, seed=42, **kwargs):
    return await e.member.generate(prompt, width, height, seed, **kwargs)


# --- starting ----------------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_nothing_starts_until_the_first_job_and_then_it_is_started_as_the_contract_says(env):
    assert env.spawner.starts == [] and env.member.loaded is False

    png = await draw(env)

    (start,) = env.spawner.starts
    command = start["command"]
    assert command[0] == str(env.base / "bin-sd" / "sd-server")
    assert command[command.index("--listen-ip") + 1] == "127.0.0.1"
    assert command[command.index("--listen-port") + 1] == str(env.sd.port)
    models = env.base / "models" / "image"
    assert command[command.index("--diffusion-model") + 1] == str(models / "d.gguf")
    assert command[command.index("--vae") + 1] == str(models / "v.safetensors")
    assert command[command.index("--llm") + 1] == str(models / "l.gguf")
    assert command[command.index("--backend") + 1] == "te=cpu"
    assert command[command.index("-t") + 1] == "4"
    assert command[command.index("--cfg-scale") + 1] == "1.0" and command[command.index("--steps") + 1] == "8"
    assert "--eager-load" in command
    assert command.count("127.0.0.1") == 1 and "0.0.0.0" not in command         # bound to loopback and nothing else
    assert png.startswith(b"\x89PNG")


@pytest.mark.anyio
async def test_it_sees_only_the_media_card_loads_only_its_own_libraries_and_runs_in_an_empty_private_directory(env):
    await draw(env)
    (start,) = env.spawner.starts
    assert start["env"]["GGML_VK_VISIBLE_DEVICES"] == "3"
    assert start["env"]["LD_LIBRARY_PATH"] == str(env.base / "bin-sd")          # nothing of llama.cpp's appended
    assert env.cwd_listings == [[]]
    assert os.path.basename(start["cwd"]).startswith("dlne-sd-") and not str(start["cwd"]).startswith(str(env.base))
    import subprocess
    assert start["stdout"] == subprocess.PIPE and start["stderr"] == subprocess.STDOUT      # read only while it starts


@pytest.mark.anyio
async def test_the_working_directory_is_removed_when_it_stops(env):
    await draw(env)
    cwd = env.spawner.starts[0]["cwd"]
    assert os.path.isdir(cwd)
    await env.member.stop()
    assert not os.path.exists(cwd) and env.spawner.processes[0].terminated and env.member.loaded is False


@pytest.mark.anyio
async def test_it_is_started_once_and_reused(env):
    await draw(env)
    await draw(env, "a blue fox")
    assert len(env.spawner.starts) == 1 and len(env.sd.bodies) == 2


@pytest.mark.anyio
async def test_a_cpu_mask_pins_it_with_taskset(env, monkeypatch):
    env.limits["image_cpu_mask"] = "0xf0"
    monkeypatch.setattr(member_module.shutil, "which", lambda name: "/usr/bin/taskset")
    await draw(env)
    assert env.spawner.starts[0]["command"][:3] == ["taskset", "0xf0", str(env.base / "bin-sd" / "sd-server")]


@pytest.mark.anyio
async def test_a_cpu_mask_without_taskset_starts_it_unpinned(env, monkeypatch):
    env.limits["image_cpu_mask"] = "0xf0"
    monkeypatch.setattr(member_module.shutil, "which", lambda name: None)
    await draw(env)
    assert env.spawner.starts[0]["command"][0] == str(env.base / "bin-sd" / "sd-server")


# --- the request body ---------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_the_body_sd_is_sent_is_built_here_and_holds_nothing_else(env):
    await draw(env, "a red fox", 1024, 768, 77)
    (body,) = env.sd.bodies
    assert body == {"prompt": "a red fox", "width": 1024, "height": 768, "seed": 77,
                    "sample_params": {"sample_steps": 8, "guidance": {"txt_cfg": 1.0}},
                    "output_format": "png", "embed_image_metadata": False}


def test_the_body_builder_takes_nothing_a_client_could_add(env):
    import inspect
    assert list(inspect.signature(env.member._request_body).parameters) == ["prompt", "width", "height", "seed"]


@pytest.mark.anyio
async def test_the_png_comes_back_as_sd_wrote_it_for_the_caller_to_strip(env):
    png = await draw(env, "my secret prompt")
    assert b"my secret prompt" in png                       # the member does not strip: media.strip_png_metadata does
    assert b"secret" not in media.strip_png_metadata(png)


# --- availability and the media card -----------------------------------------------------------------------------

def test_a_complete_install_is_available(env):
    assert env.member.unavailable_reason() is None


@pytest.mark.parametrize("change, words", [
    (lambda e: e.limits.update(image_gen_enabled=False), "IMAGE_GEN_ENABLED"),
    (lambda e: e.limits.update(media_gpu=""), "MEDIA_GPU"),
    (lambda e: (e.base / "bin-sd" / "sd-server").unlink(), "not installed"),
    (lambda e: (e.base / "bin-sd" / "sd-server").chmod(0o644), "not installed"),
    (lambda e: (e.base / "models" / "image" / "v.safetensors").unlink(), "vae model file is missing"),
    (lambda e: (e.base / "models" / "image" / "d.gguf").write_bytes(b"short"), "diffusion model file has the wrong size"),
])
def test_anything_missing_makes_it_unavailable_and_says_what(env, change, words):
    change(env)
    assert words in env.member.unavailable_reason()


def test_a_media_card_shared_with_the_coding_model_makes_no_pictures(env):
    env.limits["media_gpu_shared"] = True
    assert env.member.unavailable_reason() == "the media card is shared with the coding model (MEDIA_GPU_SHARED)"

    env.limits["media_gpu_shared"] = False
    assert env.member.unavailable_reason() is None


def test_switched_off_says_so_before_the_card_is_looked_at(env):
    env.limits.update(media_gpu_shared=True, image_gen_enabled=False)
    assert "IMAGE_GEN_ENABLED" in env.member.unavailable_reason()


@pytest.mark.anyio
async def test_sd_server_never_starts_on_a_shared_card(env):
    env.limits["media_gpu_shared"] = True
    with pytest.raises(MemberError) as caught:
        await draw(env)
    assert caught.value.code == "image_generation_unavailable"
    assert env.spawner.starts == [] and env.member.failures == 0


def test_the_model_directory_can_be_moved(env, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    env.limits["image_model_dir"] = str(other)
    assert "diffusion model file is missing" in env.member.unavailable_reason()
    for name, size in SMALL_MODELS.values():
        (other / name).write_bytes(b"x" * size)
    assert env.member.unavailable_reason() is None


def test_the_real_pins_are_checked_by_exact_size(tmp_path, monkeypatch):
    monkeypatch.undo()
    assert member_module.MODEL_FILES["diffusion"] == ("z_image_turbo-Q4_K.gguf", 3_864_250_304)
    assert member_module.MODEL_FILES["vae"] == ("ae.safetensors", 335_304_388)
    assert member_module.MODEL_FILES["llm"][1] == 2_497_280_736


@pytest.mark.anyio
@pytest.mark.parametrize("free", [5.59, 0.5, None])
async def test_it_does_not_start_when_the_media_card_lacks_5_6_gib(env, free):
    env.card = {"vk_index": 3, "free_gb": free}
    with pytest.raises(MemberError) as caught:
        await draw(env)
    assert caught.value.code == "image_generation_unavailable" and "memory" in caught.value.detail
    assert env.spawner.starts == [] and env.member.failures == 0         # the card's state is not a failure of the engine


@pytest.mark.anyio
async def test_it_starts_when_the_card_has_exactly_enough(env):
    env.card = {"vk_index": 0, "free_gb": 5.6}
    await draw(env)
    assert env.spawner.starts[0]["env"]["GGML_VK_VISIBLE_DEVICES"] == "0"


@pytest.mark.anyio
async def test_a_loaded_engine_is_not_asked_about_the_card_again(env):
    await draw(env)
    env.card = {"vk_index": 3, "free_gb": 0.1}            # sd itself now holds the memory
    await draw(env)
    assert len(env.spawner.starts) == 1


@pytest.mark.anyio
async def test_a_card_that_is_gone_is_unavailable(env):
    env.card = None
    with pytest.raises(MemberError) as caught:
        await draw(env)
    assert caught.value.code == "image_generation_unavailable"


# --- failures, timeouts, crashes ---------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_a_binary_that_cannot_be_started_is_a_failure_and_leaves_nothing_behind(env):
    env.spawner.fail = True
    with pytest.raises(MemberError) as caught:
        await draw(env)
    assert caught.value.code == "generation_failed" and env.member.failures == 1 and env.member.loaded is False


@pytest.mark.anyio
async def test_an_engine_that_never_answers_is_stopped(env):
    env.sd.ready = False
    env.member._ready_timeout = 0.2
    with pytest.raises(MemberError) as caught:
        await draw(env)
    assert caught.value.code == "generation_failed" and env.spawner.processes[0].terminated and env.member.failures == 1


@pytest.mark.anyio
async def test_a_job_that_runs_past_the_limit_is_cancelled_and_the_process_replaced(env):
    env.sd.mode = "hold"
    env.limits["image_job_timeout_seconds"] = 0.3
    with pytest.raises(MemberError) as caught:
        await draw(env)
    assert caught.value.code == "generation_timeout" and env.sd.cancels
    assert env.spawner.processes[0].terminated and env.member.failures == 1

    env.sd.mode = "ok"
    await draw(env)
    assert len(env.spawner.starts) == 2 and env.member.failures == 0         # started afresh; the success cleared the count


@pytest.mark.anyio
async def test_a_crash_while_drawing_is_a_failure_and_the_next_job_starts_it_again(env):
    env.sd.mode = "hold"
    task = asyncio.create_task(draw(env))
    for _ in range(200):
        if env.sd.bodies:
            break
        await asyncio.sleep(0.01)
    env.spawner.processes[0].die(-11)
    with pytest.raises(MemberError) as caught:
        await task
    assert caught.value.code == "generation_failed" and env.member.failures == 1

    env.sd.mode = "ok"
    await draw(env)
    assert len(env.spawner.starts) == 2


@pytest.mark.anyio
async def test_a_process_that_died_between_jobs_is_replaced_without_a_failure(env):
    await draw(env)
    env.spawner.processes[0].die(1)
    await draw(env)
    assert len(env.spawner.starts) == 2 and env.member.failures == 0


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["fail", "badpng"])
async def test_a_failed_job_or_a_reply_that_is_not_a_picture_is_a_failure(env, mode):
    env.sd.mode = mode
    with pytest.raises(MemberError) as caught:
        await draw(env)
    assert caught.value.code == "generation_failed" and env.member.failures == 1
    assert "memory" not in caught.value.detail                    # sd's own error text is never passed on


@pytest.mark.anyio
async def test_three_failures_in_a_row_switch_it_off_for_thirty_minutes(env):
    env.sd.mode = "fail"
    for _ in range(3):
        with pytest.raises(MemberError):
            await draw(env)
    assert "paused" in env.member.unavailable_reason()
    starts = len(env.spawner.starts)
    with pytest.raises(MemberError) as caught:
        await draw(env)
    assert caught.value.code == "image_generation_unavailable" and len(env.spawner.starts) == starts

    env.offset[0] = 29 * 60
    assert "paused" in env.member.unavailable_reason()
    env.offset[0] = 31 * 60
    assert env.member.unavailable_reason() is None
    env.sd.mode = "ok"
    await draw(env)
    assert env.member.failures == 0


@pytest.mark.anyio
async def test_a_success_between_failures_resets_the_count(env):
    env.sd.mode = "fail"
    for _ in range(2):
        with pytest.raises(MemberError):
            await draw(env)
    env.sd.mode = "ok"
    await draw(env)
    env.sd.mode = "fail"
    for _ in range(2):
        with pytest.raises(MemberError):
            await draw(env)
    assert env.member.unavailable_reason() is None


# --- cancelling -------------------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_a_cancelled_job_is_cancelled_on_sd_and_costs_no_failure(env):
    env.sd.mode = "hold"
    flag = {"stop": False}
    task = asyncio.create_task(draw(env, cancelled=lambda: flag["stop"]))
    for _ in range(200):
        if env.sd.bodies:
            break
        await asyncio.sleep(0.01)
    flag["stop"] = True
    with pytest.raises(Cancelled):
        await task
    assert env.sd.cancels == ["job_0001"] and env.member.failures == 0
    assert env.member.loaded                                 # sd stopped at its checkpoint; no restart needed


@pytest.mark.anyio
async def test_a_job_that_will_not_stop_after_a_cancel_gets_its_process_replaced(env):
    env.sd.mode = "hold"
    env.sd.ignore_cancel = True
    flag = {"stop": False}
    task = asyncio.create_task(draw(env, cancelled=lambda: flag["stop"]))
    for _ in range(200):
        if env.sd.bodies:
            break
        await asyncio.sleep(0.01)
    flag["stop"] = True
    with pytest.raises(Cancelled):
        await task
    assert env.sd.cancels and env.spawner.processes[0].terminated and env.member.failures == 0


# --- unloading ---------------------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_it_unloads_after_the_idle_minutes_and_not_before(env):
    env.limits["image_idle_unload_minutes"] = 20
    await draw(env)
    cwd = env.spawner.starts[0]["cwd"]
    env.offset[0] = 19 * 60
    assert await env.member.maybe_unload() is False and env.member.loaded
    env.offset[0] = 20 * 60 + 1
    assert await env.member.maybe_unload() is True
    assert env.member.loaded is False and env.spawner.processes[0].terminated and not os.path.exists(cwd)
    await draw(env)                                            # the next job loads it again
    assert len(env.spawner.starts) == 2


@pytest.mark.anyio
async def test_zero_minutes_means_it_is_never_unloaded(env):
    env.limits["image_idle_unload_minutes"] = 0
    await draw(env)
    env.offset[0] = 10 * 24 * 3600
    assert await env.member.maybe_unload() is False and env.member.loaded


@pytest.mark.anyio
async def test_a_busy_engine_is_never_unloaded(env):
    env.limits["image_idle_unload_minutes"] = 1
    env.limits["image_job_timeout_seconds"] = 10 ** 7        # the clock is moved an hour below: the job is not late
    env.sd.mode = "hold"
    task = asyncio.create_task(draw(env))
    for _ in range(200):
        if env.sd.bodies:
            break
        await asyncio.sleep(0.01)
    env.offset[0] = 3600
    assert await env.member.maybe_unload() is False
    env.sd.gate.set()
    await task


def test_stop_now_ends_the_process_without_an_event_loop(env):
    async def start():
        await draw(env)

    asyncio.run(start())
    env.member.stop_now()
    assert env.spawner.processes[0].terminated and env.member.loaded is False


# --- a real child process ---------------------------------------------------------------------------------------

FAKE_BINARY = textwrap.dedent('''\
    #!__PYTHON__
    import http.server, json, os, sys
    args = sys.argv[1:]
    with open(os.environ["FAKE_SD_RECORD"], "w") as handle:
        json.dump({"argv": args, "cwd": os.getcwd(), "vk": os.environ.get("GGML_VK_VISIBLE_DEVICES"),
                   "ld": os.environ.get("LD_LIBRARY_PATH"), "listing": os.listdir(".")}, handle)
    port = int(args[args.index("--listen-port") + 1])
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"{}")
        def log_message(self, *a): pass
    http.server.HTTPServer((args[args.index("--listen-ip") + 1], port), Handler).serve_forever()
    ''')


@pytest.mark.anyio
async def test_a_real_child_is_started_with_the_right_argv_environment_and_directory_and_is_stopped(env, tmp_path, monkeypatch):
    binary = env.base / "bin-sd" / "sd-server"
    binary.write_text(FAKE_BINARY.replace("__PYTHON__", sys.executable))
    binary.chmod(0o755)
    record = tmp_path / "record.json"
    monkeypatch.setenv("FAKE_SD_RECORD", str(record))
    real = ImageMember(limits=lambda: env.limits, base_dir=lambda: str(env.base),
                       media_spec=lambda limits: "x", media_card=lambda spec: env.card,
                       poll_seconds=0.05, ready_timeout=15.0)
    await real._ensure_started(env.limits)
    try:
        assert real.loaded
        seen = json.loads(record.read_text())
        assert seen["argv"][seen["argv"].index("--listen-ip") + 1] == "127.0.0.1"
        assert seen["vk"] == "3" and seen["ld"] == str(env.base / "bin-sd") and seen["listing"] == []
        assert os.path.basename(seen["cwd"]).startswith("dlne-sd-")
        cwd, process = seen["cwd"], real.process
    finally:
        await real.stop()
    assert process.poll() is not None and not os.path.exists(cwd)


# --- the media card as hardware.py maps it -----------------------------------------------------------------------

def test_the_media_card_is_found_by_pci_address_with_its_global_vulkan_index_and_live_free_memory(monkeypatch):
    from engine import hardware
    gpus = [{"id": 0, "name": "big", "address": "0000:01:00.0", "vk_index": 0, "vram_total_gb": 24.0, "vram_free_gb": 24.0},
            {"id": 1, "name": "RX 580", "address": "0000:0c:00.0", "vk_index": 3, "vram_total_gb": 8.0, "vram_free_gb": 8.0}]
    monkeypatch.setattr(hardware, "get_all_gpus", lambda: [dict(g) for g in gpus])
    sysfs = {"/sys/bus/pci/devices/0000:0c:00.0/mem_info_vram_total": str(8 * 1024 ** 3),
             "/sys/bus/pci/devices/0000:0c:00.0/mem_info_vram_used": str(3 * 1024 ** 3)}
    monkeypatch.setattr(hardware, "_read_sysfs", lambda path: sysfs.get(path, ""))

    card = hardware.media_card("0c:00.0")                     # the address without its domain works too
    assert card == {"vk_index": 3, "total_gb": 8.0, "free_gb": 5.0}
    assert hardware.media_card("1")["vk_index"] == 3          # a DLNE GPU id
    assert hardware.media_card("0000:99:00.0") is None and hardware.media_card("") is None

    sysfs.clear()                                             # a card that publishes no counters: no live figure, not the static size
    assert hardware.media_card("0000:0c:00.0")["free_gb"] is None


def test_a_card_without_a_vulkan_index_uses_its_gpu_id_as_the_engines_own_mapping_does(monkeypatch):
    from engine import hardware
    monkeypatch.setattr(hardware, "get_all_gpus", lambda: [{"id": 2, "name": "nv", "vram_total_gb": 12.0, "vram_free_gb": 9.5}])
    monkeypatch.setattr(hardware, "_read_sysfs", lambda path: "")
    assert hardware.media_card("2") == {"vk_index": 2, "total_gb": 12.0, "free_gb": 9.5}      # the nvidia-smi path is live


# --- sd-server's output: kept while it starts, never after ----------------------------------------------------------

import io
import threading

OUTPUT_BINARY = textwrap.dedent('''\
    #!__PYTHON__
    import http.server, os, sys, threading, time
    args = sys.argv[1:]
    mode = os.environ["FAKE_SD_MODE"]
    port = int(args[args.index("--listen-port") + 1])
    print("sd-server startup banner", flush=True)
    print("ggml_vulkan: Found 1 Vulkan devices", file=sys.stderr, flush=True)
    if mode == "fail_start":
        print("ERROR: failed to load model /models/image/d.gguf: out of device memory", file=sys.stderr, flush=True)
        sys.exit(3)
    state = {"ready": False}
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            state["ready"] = True
            self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"{}")
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = b'{"id": "job1"}'
            self.send_response(202); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        def log_message(self, *a): pass
    def chatter():
        while not state["ready"]:
            time.sleep(0.005)
        time.sleep(0.05)                      # after readiness: what sd-server prints from its first job on
        if mode == "flood":
            for _ in range(600):
                sys.stdout.write("x" * 4000 + "\\n"); sys.stdout.flush()
            sys.stdout.write("y" * 20000); sys.stdout.flush()       # a line with no end
        while True:
            print("prompt: a TOPSECRET picture of my neighbour", flush=True)
            time.sleep(0.01)
    threading.Thread(target=chatter, daemon=True).start()
    http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
    ''')


@pytest.fixture
def logged(monkeypatch):
    """Everything image_member logs, as (level, text)."""
    lines = []
    for level in ("log_error", "log_warning", "log_coordinator"):
        monkeypatch.setattr(member_module, level, lambda text, level=level: lines.append((level, text)))
    return lines


def real_member(env, monkeypatch, mode, ready_timeout=15.0):
    binary = env.base / "bin-sd" / "sd-server"
    binary.write_text(OUTPUT_BINARY.replace("__PYTHON__", sys.executable))
    binary.chmod(0o755)
    monkeypatch.setenv("FAKE_SD_MODE", mode)
    return ImageMember(limits=lambda: env.limits, base_dir=lambda: str(env.base), media_spec=lambda limits: "x",
                       media_card=lambda spec: env.card, poll_seconds=0.02, ready_timeout=ready_timeout)


def output_threads():
    return [t for t in threading.enumerate() if t.name == "sd-output" and t.is_alive()]


@pytest.mark.anyio
async def test_a_start_that_fails_logs_what_sd_server_said_and_how_it_ended(env, monkeypatch, logged):
    member = real_member(env, monkeypatch, "fail_start")
    with pytest.raises(MemberError) as caught:
        await member._ensure_started(env.limits)
    assert caught.value.code == "generation_failed"
    text = "\n".join(line for _level, line in logged)
    assert "sd-server: ERROR: failed to load model /models/image/d.gguf: out of device memory" in text
    assert "sd-server: sd-server startup banner" in text and "ggml_vulkan: Found 1 Vulkan devices" in text
    assert "exit code 3" in text
    assert all(level == "log_error" for level, line in logged if line.startswith("sd-server:"))
    assert member.loaded is False and not output_threads()


@pytest.mark.anyio
async def test_an_engine_that_never_answers_logs_its_startup_lines_too(env, monkeypatch, logged):
    member = real_member(env, monkeypatch, "ok", ready_timeout=0.05)
    member._ready_timeout = 0.0                     # never gets the chance to answer
    with pytest.raises(MemberError):
        await member._ensure_started(env.limits)
    assert any("never answered" in line for _level, line in logged)
    assert not output_threads()


@pytest.mark.anyio
async def test_nothing_it_prints_after_it_is_ready_is_kept_or_logged(env, monkeypatch, logged):
    member = real_member(env, monkeypatch, "ok")
    await member._ensure_started(env.limits)
    try:
        await asyncio.sleep(0.5)                    # it prints a "prompt" line every 10 ms from now on
        assert member._output._keeping is False and member._output.tail() == []
        assert member.process.poll() is None
        task = asyncio.create_task(member.generate("a TOPSECRET picture", 768, 768, 1))
        await asyncio.sleep(0.3)                    # submitted; sd-server keeps "generating" and chattering
        member.process.kill()                       # a crash after readiness
        with pytest.raises(MemberError):
            await task
    finally:
        await member.stop()
    text = "\n".join(line for _level, line in logged)
    assert "TOPSECRET" not in text and "neighbour" not in text and "startup banner" not in text
    assert "stopped while drawing (signal 9 (SIGKILL))" in text           # only how it ended
    assert not output_threads()


@pytest.mark.anyio
async def test_a_chatty_engine_never_blocks_on_a_full_pipe_and_a_line_without_an_end_is_bounded(env, monkeypatch, logged):
    member = real_member(env, monkeypatch, "flood")
    await member._ensure_started(env.limits)
    try:
        await asyncio.sleep(1.0)                    # 2.4 MB, far more than a pipe holds, then a 20 KB line with no end
        assert await member._wait_ready()           # still answering: its writes were never stuck
        assert member._output.tail() == []
    finally:
        await member.stop()
    assert "xxxx" not in "\n".join(line for _level, line in logged)
    assert not output_threads()


@pytest.mark.anyio
async def test_the_reader_ends_with_the_process_even_when_it_was_never_told_to_stop(env, monkeypatch, logged):
    member = real_member(env, monkeypatch, "ok")
    await member._ensure_started(env.limits)
    reader = member._output.thread
    assert reader.is_alive()
    member.process.kill()
    member.process.wait()
    reader.join(5)
    assert not reader.is_alive()
    await member.stop()


def test_the_ring_keeps_the_last_200_lines_cleaned_and_cut():
    raw = b"".join(f"line {i}\n".encode() for i in range(1000)) + b"bell\x07 and \x1b[31mred\x1b[0m\n" + b"z" * 5000 + b"\n"
    output = member_module.StartupOutput(io.BytesIO(raw))
    output.join()
    lines = output.tail(10_000)
    assert len(lines) == member_module.STARTUP_RING_LINES == 200
    assert lines[0].startswith("line 8") and "line 0" not in lines            # the oldest are gone
    assert any(line == "bell  and  [31mred [0m" for line in lines)         # control characters become spaces
    assert max(map(len, lines)) <= member_module.OUTPUT_LINE_CHARS
    assert len(output.tail()) == member_module.STARTUP_LOG_LINES == 40


def test_a_stopped_ring_keeps_nothing_more_for_good():
    output = member_module.StartupOutput(io.BytesIO(b"a\nb\n"))
    output.join()
    assert output.tail() == ["a", "b"]
    output.stop_keeping()
    assert output.tail() == []


def test_exit_text_says_the_code_or_the_signal():
    assert member_module.exit_text(3) == "exit code 3" and member_module.exit_text(0) == "exit code 0"
    assert member_module.exit_text(-11) == "signal 11 (SIGSEGV)" and member_module.exit_text(-9) == "signal 9 (SIGKILL)"
    assert member_module.exit_text(-250) == "signal 250" and member_module.exit_text(None) == "exit code None"


@pytest.mark.anyio
async def test_a_fake_process_crash_in_the_in_process_fake_logs_only_the_signal(env, logged):
    env.sd.mode = "hold"
    task = asyncio.create_task(draw(env, "TOPSECRET words"))
    for _ in range(200):
        if env.sd.bodies:
            break
        await asyncio.sleep(0.01)
    env.spawner.processes[0].die(-11)
    with pytest.raises(MemberError):
        await task
    text = "\n".join(line for _level, line in logged)
    assert "signal 11 (SIGSEGV)" in text and "TOPSECRET" not in text
