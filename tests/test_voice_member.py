"""
The voice engine's lifecycle (engine/voice_member.py) against a fake whisper-server on a real loopback socket: the command it
is started with, its environment and directory, lazy start, idle unload, the media card's memory, a timeout, a crash, the
three-strikes cool-down, what is sent to it and what comes back, and that no audio is ever written to disk.
"""

import asyncio
import os
import stat
import subprocess
import tempfile
import time

import pytest

from engine import voice_member as module
from engine.voice_member import Cancelled, MemberError, VoiceMember, language_code
from tests.voice_fakes import FakeWhisperServer, Spawner, make_wav


@pytest.fixture(scope="module")
def whisper():
    server = FakeWhisperServer().start()
    yield server
    server.stop()


class Env:
    pass


@pytest.fixture
def env(tmp_path, monkeypatch, whisper):
    whisper.reset()
    e = Env()
    e.base = tmp_path
    (tmp_path / "bin-whisper").mkdir()
    binary = tmp_path / "bin-whisper" / "whisper-server"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    (tmp_path / "models" / "voice").mkdir(parents=True)
    (tmp_path / "models" / "voice" / "m.bin").write_bytes(b"x" * 10)
    e.limits = {"voice_enabled": True, "media_gpu": "0000:0c:00.0", "voice_model": "m.bin", "voice_threads": 2,
                "voice_cpu_mask": "", "voice_idle_unload_minutes": 20, "voice_min_free_vram_gb": 1.2}
    e.card = {"vk_index": 2, "free_gb": 6.5, "total_gb": 8.0}
    e.offset = [0.0]
    e.whisper = whisper
    e.spawner = Spawner()
    e.cwd_listings = []
    original = e.spawner.__call__

    def spawn(command, **kwargs):
        e.cwd_listings.append(sorted(os.listdir(kwargs["cwd"])))
        return original(command, **kwargs)

    e.member = VoiceMember(limits=lambda: e.limits, base_dir=lambda: str(tmp_path), spawn=spawn,
                           media_spec=lambda limits: limits.get("media_gpu", ""), media_card=lambda spec: e.card,
                           clock=lambda: time.monotonic() + e.offset[0], poll_seconds=0.01, ready_timeout=3.0,
                           clip_timeout=3.0, free_port=lambda: whisper.port)
    return e


async def hear(e, language=None, **kwargs):
    return await e.member.transcribe(make_wav(1.0), language, **kwargs)


# --- starting ----------------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_nothing_starts_until_the_first_clip_and_then_it_is_started_with_the_documented_flags(env):
    assert env.spawner.starts == [] and env.member.loaded is False

    await hear(env)

    (start,) = env.spawner.starts
    command = start["command"]
    assert command == [str(env.base / "bin-whisper" / "whisper-server"), "--host", "127.0.0.1", "--port", str(env.whisper.port),
                       "-m", str(env.base / "models" / "voice" / "m.bin"), "-t", "2", "-l", "auto", "-nlp"]
    assert "--convert" not in command, "no ffmpeg, and an upload is never written to a temporary file"
    assert command.count("127.0.0.1") == 1 and "0.0.0.0" not in command


@pytest.mark.anyio
async def test_it_sees_only_the_media_card_loads_only_its_own_libraries_and_runs_in_an_empty_private_directory(env):
    await hear(env)
    (start,) = env.spawner.starts
    assert start["env"]["GGML_VK_VISIBLE_DEVICES"] == "2"
    assert start["env"]["LD_LIBRARY_PATH"] == str(env.base / "bin-whisper")
    assert env.cwd_listings == [[]]
    assert os.path.basename(start["cwd"]).startswith("dlne-whisper-") and not str(start["cwd"]).startswith(str(env.base))
    assert start["stdout"] == subprocess.PIPE and start["stderr"] == subprocess.STDOUT


@pytest.mark.anyio
async def test_the_working_directory_is_removed_when_it_stops(env):
    await hear(env)
    cwd = env.spawner.starts[0]["cwd"]
    assert os.path.isdir(cwd)
    await env.member.stop()
    assert not os.path.exists(cwd) and env.spawner.processes[0].terminated and env.member.loaded is False


@pytest.mark.anyio
async def test_it_is_started_once_and_reused(env):
    await hear(env)
    await hear(env)
    assert len(env.spawner.starts) == 1 and len(env.whisper.forms) == 2


@pytest.mark.anyio
async def test_a_cpu_mask_pins_it_with_taskset(env, monkeypatch):
    env.limits["voice_cpu_mask"] = "0x30"
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/taskset")
    await hear(env)
    assert env.spawner.starts[0]["command"][:3] == ["taskset", "0x30", str(env.base / "bin-whisper" / "whisper-server")]


@pytest.mark.anyio
async def test_a_cpu_mask_without_taskset_starts_it_unpinned(env, monkeypatch):
    env.limits["voice_cpu_mask"] = "0x30"
    monkeypatch.setattr(module.shutil, "which", lambda name: None)
    await hear(env)
    assert env.spawner.starts[0]["command"][0] == str(env.base / "bin-whisper" / "whisper-server")


@pytest.mark.anyio
async def test_the_thread_count_comes_from_the_limits(env):
    env.limits["voice_threads"] = 5
    await hear(env)
    command = env.spawner.starts[0]["command"]
    assert command[command.index("-t") + 1] == "5"


# --- what is sent and what comes back ----------------------------------------------------------------------

@pytest.mark.anyio
async def test_the_form_is_built_here_and_the_text_and_language_come_back_cleaned(env):
    text, language = await hear(env)
    (form,) = env.whisper.forms
    assert set(form) == {"file", "response_format", "temperature", "language"}
    assert form["response_format"] == b"verbose_json" and form["language"] == b"auto"
    assert form["file"][:4] == b"RIFF"
    assert (text, language) == ("Hello there.", "en")


@pytest.mark.anyio
async def test_a_requested_language_is_sent_and_reported(env):
    text, language = await hear(env, "de")
    assert env.whisper.forms[0]["language"] == b"de" and language == "de"


def test_language_names_become_codes():
    assert language_code("english") == "en" and language_code("German") == "de" and language_code("cantonese") == "yue"
    assert language_code("klingon") == "klingon" and language_code("") is None and language_code(None) is None


# --- availability and the media card -----------------------------------------------------------------------

def test_a_complete_install_is_available(env):
    assert env.member.unavailable_reason() is None


@pytest.mark.parametrize("change, words", [
    (lambda e: e.limits.update(voice_enabled=False), "VOICE_ENABLED"),
    (lambda e: e.limits.update(media_gpu=""), "MEDIA_GPU"),
    (lambda e: (e.base / "bin-whisper" / "whisper-server").unlink(), "not installed"),
    (lambda e: (e.base / "bin-whisper" / "whisper-server").chmod(0o644), "not installed"),
    (lambda e: (e.base / "models" / "voice" / "m.bin").unlink(), "model is missing"),
    (lambda e: e.limits.update(voice_model="other.bin"), "model is missing"),
])
def test_anything_missing_makes_it_unavailable_and_says_what(env, change, words):
    change(env)
    assert words in env.member.unavailable_reason()


def test_a_model_name_cannot_leave_the_models_folder(env):
    env.limits["voice_model"] = "../../etc/passwd"
    assert env.member.model_path() == str(env.base / "models" / "voice" / "passwd")


@pytest.mark.anyio
async def test_whisper_never_starts_on_a_shared_card(env):
    env.limits["media_gpu_shared"] = True
    assert env.member.unavailable_reason() == module.MEDIA_CARD_SHARED_REASON
    with pytest.raises(MemberError) as caught:
        await hear(env)
    assert caught.value.code == "voice_unavailable"
    assert env.spawner.starts == [] and env.member.failures == 0


@pytest.mark.anyio
@pytest.mark.parametrize("free", [1.19, 0.2, None])
async def test_it_does_not_start_when_the_media_card_lacks_the_configured_memory(env, free):
    env.card = {"vk_index": 2, "free_gb": free}
    with pytest.raises(MemberError) as caught:
        await hear(env)
    assert caught.value.code == "voice_unavailable" and "memory" in caught.value.detail
    assert env.spawner.starts == [] and env.member.failures == 0
    assert (await env.member.card_has_room())[0] is False


@pytest.mark.anyio
async def test_it_starts_when_the_card_has_exactly_enough_and_the_figure_is_a_setting(env):
    env.card = {"vk_index": 0, "free_gb": 0.9}
    env.limits["voice_min_free_vram_gb"] = 0.9
    await hear(env)
    assert env.spawner.starts[0]["env"]["GGML_VK_VISIBLE_DEVICES"] == "0"


@pytest.mark.anyio
async def test_a_loaded_engine_is_not_asked_about_the_card_again(env):
    await hear(env)
    env.card = {"vk_index": 2, "free_gb": 0.1}
    assert (await env.member.card_has_room()) == (True, None)
    await hear(env)
    assert len(env.spawner.starts) == 1


@pytest.mark.anyio
async def test_a_card_that_is_gone_is_unavailable(env):
    env.card = None
    with pytest.raises(MemberError) as caught:
        await hear(env)
    assert caught.value.code == "voice_unavailable"


# --- failures, timeouts, crashes ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_a_binary_that_cannot_be_started_is_a_failure_and_leaves_nothing_behind(env):
    env.spawner.fail = True
    with pytest.raises(MemberError) as caught:
        await hear(env)
    assert caught.value.code == "voice_failed" and env.member.failures == 1 and env.member.loaded is False


@pytest.mark.anyio
async def test_an_engine_that_never_answers_is_stopped(env):
    env.whisper.ready = False
    env.member._ready_timeout = 0.2
    with pytest.raises(MemberError) as caught:
        await hear(env)
    assert caught.value.code == "voice_failed" and env.spawner.processes[0].terminated and env.member.failures == 1


@pytest.mark.anyio
async def test_a_clip_that_runs_past_the_limit_is_stopped_and_the_process_replaced(env):
    env.whisper.mode = "hold"
    env.member._clip_timeout = 0.3
    with pytest.raises(MemberError) as caught:
        await hear(env)
    assert caught.value.code == "voice_timeout" and env.spawner.processes[0].terminated and env.member.failures == 1

    env.whisper.gate.set()
    env.whisper.mode = "ok"
    env.member._clip_timeout = 3.0
    await hear(env)
    assert len(env.spawner.starts) == 2 and env.member.failures == 0


@pytest.mark.anyio
async def test_a_crash_during_a_clip_is_a_failure_and_the_next_clip_starts_it_again(env):
    env.whisper.mode = "hold"
    task = asyncio.create_task(hear(env))
    for _ in range(200):
        if env.whisper.forms:
            break
        await asyncio.sleep(0.01)
    env.spawner.processes[0].die(-11)
    env.whisper.gate.set()
    with pytest.raises(MemberError) as caught:
        await task
    assert caught.value.code == "voice_failed" and env.member.failures == 1

    env.whisper.mode = "ok"
    await hear(env)
    assert len(env.spawner.starts) == 2


@pytest.mark.anyio
async def test_a_process_that_died_between_clips_is_replaced_without_a_failure(env):
    await hear(env)
    env.spawner.processes[0].die(1)
    await hear(env)
    assert len(env.spawner.starts) == 2 and env.member.failures == 0


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["fail", "garbage"])
async def test_an_error_or_a_reply_that_is_not_text_is_a_failure_and_its_words_are_not_passed_on(env, mode):
    env.whisper.mode = mode
    with pytest.raises(MemberError) as caught:
        await hear(env)
    assert caught.value.code == "voice_failed" and env.member.failures == 1
    assert "failed to process" not in caught.value.detail


@pytest.mark.anyio
async def test_three_failures_in_a_row_switch_voice_off_for_thirty_minutes(env):
    env.whisper.mode = "fail"
    for _ in range(3):
        with pytest.raises(MemberError):
            await hear(env)
    assert "3 failures in a row" in env.member.unavailable_reason()
    starts = len(env.spawner.starts)
    with pytest.raises(MemberError) as caught:
        await hear(env)
    assert caught.value.code == "voice_unavailable" and len(env.spawner.starts) == starts

    env.offset[0] = 31 * 60
    env.whisper.mode = "ok"
    assert env.member.unavailable_reason() is None
    assert (await hear(env))[0] == "Hello there." and env.member.failures == 0


@pytest.mark.anyio
async def test_a_clip_cancelled_before_it_starts_starts_nothing(env):
    with pytest.raises(Cancelled):
        await hear(env, cancelled=lambda: True)
    assert env.spawner.starts == []


# --- idle unload -------------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_it_is_unloaded_after_the_idle_minutes_and_not_before(env):
    await hear(env)
    env.offset[0] = 19 * 60
    assert await env.member.maybe_unload() is False and env.member.loaded
    env.offset[0] = 21 * 60
    assert await env.member.maybe_unload() is True and env.member.loaded is False


@pytest.mark.anyio
async def test_zero_minutes_means_never(env):
    env.limits["voice_idle_unload_minutes"] = 0
    await hear(env)
    env.offset[0] = 10 ** 6
    assert await env.member.maybe_unload() is False and env.member.loaded


# --- the audio is never written ----------------------------------------------------------------------------

@pytest.mark.anyio
async def test_no_audio_lands_in_the_working_directory_or_the_temp_directory(env):
    before = set(os.listdir(tempfile.gettempdir()))
    await hear(env)
    await hear(env, "de")
    cwd = env.spawner.starts[0]["cwd"]
    assert os.listdir(cwd) == []
    new = {n for n in set(os.listdir(tempfile.gettempdir())) - before if n.startswith("dlne") or ".wav" in n}
    assert new <= {os.path.basename(cwd)}, f"unexpected temporary files: {new - {os.path.basename(cwd)}}"
