"""
Voice through the coordinator (2.8.0): POST /v1/audio/transcriptions with the real route, middleware and service, the
status (capabilities.voice and the voice block), the config keys, and the takeover pause. The voice engine is a stand-in
(tests/test_voice_pool.py's), and the card is a fake; a few tests run the real VoiceMember against a fake whisper-server.

A network address is a test client's peer, as in tests/test_image_endpoints.py.
"""

import asyncio
import os
import stat
import tempfile
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

import engine.config as engine_config
from engine import coordinator, takeover
from engine.contributor_networks import ContributorBook
from engine.voice_member import MemberError, VoiceMember
from engine.voice_pool import VoiceService
from tests.takeover_fakes import DEFAULT_CONFIG, MEDIA, POOL_A, box     # noqa: F401  (box is a fixture)
from tests.test_media_chat import counter_env, engine    # noqa: F401 - fixtures (the stub engine and its status)
from tests.test_voice_pool import FakeMember
from tests.voice_fakes import FakeWhisperServer, make_wav, multipart

REMOTE = ("203.0.113.9", 40000)
OTHER = ("198.51.100.20", 40001)
LOCAL = ("127.0.0.1", 5000)
PATH = "/v1/audio/transcriptions"
MODEL = "m.gguf"


def client(peer=REMOTE, headers=None):
    return AsyncClient(transport=ASGITransport(app=coordinator.app, client=peer), base_url="http://test", headers=headers or {})


@pytest.fixture
async def voice(monkeypatch, tmp_path):
    """Voice on, a stand-in engine, a trial of 5 chats, a limit of 3 clips an hour and a waiting list of 2."""
    limits = {"voice_enabled": True, "voice_paused": False, "voice_per_hour": 3, "voice_queue_max": 2,
              "voice_max_seconds": 60, "starter_quota_inferences": 5, "enforce_contrib_clause": True,
              "image_gen_enabled": False}
    real = coordinator.get_worker_limits
    monkeypatch.setattr(coordinator, "get_worker_limits", lambda: {**real(), **limits})
    member = FakeMember()
    service = VoiceService(member, limits=lambda: coordinator.get_worker_limits(), housekeeping=0.05,
                           picture_busy=lambda: False, picture_wait=0.3, picture_poll=0.01)
    monkeypatch.setattr(coordinator, "voice_service", service)
    monkeypatch.setattr(coordinator, "_voice_reason_seen", None)
    quota = coordinator.StarterQuotaManager(str(tmp_path / "starter_quota.json"), default_quota=5)
    monkeypatch.setattr(coordinator, "starter_quota_mgr", quota)
    monkeypatch.setattr(coordinator.registry, "workers", {})
    monkeypatch.setattr(coordinator, "contributor_networks", ContributorBook())
    yield SimpleNamespace(limits=limits, member=member, service=service, quota=quota)
    await service.shutdown()


async def say(c, fields=None, content_type=None, body=None, **kwargs):
    if body is None:
        content_type, body = multipart(fields if fields is not None else {"file": make_wav(1.0), "model": "whisper-1"})
    return await c.post(PATH, content=body, headers={"Content-Type": content_type}, **kwargs)


# --- with the key off nothing changes ---------------------------------------------------------------------

@pytest.mark.anyio
async def test_the_defaults_are_off_and_the_contracts_numbers():
    limits = coordinator.get_worker_limits()
    assert (limits["voice_enabled"], limits["voice_paused"], limits["voice_model"], limits["voice_threads"],
            limits["voice_cpu_mask"], limits["voice_max_seconds"], limits["voice_per_hour"], limits["voice_queue_max"],
            limits["voice_idle_unload_minutes"]) == (False, False, "ggml-large-v3-turbo-q8_0.bin", 2, "", 60, 60, 8, 20)
    assert isinstance(limits["voice_min_free_vram_gb"], float)


@pytest.mark.anyio
async def test_with_voice_off_the_endpoint_says_503_unavailable_and_spends_nothing(tmp_path, monkeypatch):
    quota = coordinator.StarterQuotaManager(str(tmp_path / "q.json"), default_quota=5)
    monkeypatch.setattr(coordinator, "starter_quota_mgr", quota)
    assert coordinator.get_worker_limits()["voice_enabled"] is False
    async with client() as c:
        reply = await say(c)
    assert reply.status_code == 503
    assert reply.json() == {"detail": coordinator.VOICE_UNAVAILABLE, "code": "voice_unavailable"}
    assert quota.summary(5)["chats_used"] == 0


@pytest.mark.anyio
async def test_with_voice_off_the_status_is_what_2_7_sent(engine, monkeypatch):
    async def snapshot(_name):
        return dict(engine.entry)

    monkeypatch.setattr(coordinator.manager, "asnapshot_model", snapshot)
    monkeypatch.setattr(coordinator.manager, "get_active_model_names", lambda: [MODEL])
    async with client(LOCAL) as c:
        status = (await c.get("/v1/cluster/status")).json()
    assert "voice" not in status and "voice" not in status["capabilities"]


# --- a clip ----------------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_a_clip_is_turned_into_text_in_the_contracts_shape(voice):
    async with client() as c:
        reply = await say(c, {"file": make_wav(2.5), "model": "anything", "language": "en"})
    assert reply.status_code == 200 and reply.headers["cache-control"] == "no-store"
    assert reply.json() == {"text": "hello", "language": "en", "duration_seconds": 2.5}
    assert voice.member.calls[0]["language"] == "en"


@pytest.mark.anyio
async def test_no_language_means_detect_and_auto_is_the_same(voice):
    async with client() as c:
        first = await say(c)
        second = await say(c, {"file": make_wav(1.0), "language": "AUTO"})
    assert [call["language"] for call in voice.member.calls] == [None, None]
    assert first.json()["language"] == "en" == second.json()["language"]


@pytest.mark.anyio
async def test_the_text_format_answers_plain_text(voice):
    async with client() as c:
        reply = await say(c, {"file": make_wav(1.0), "response_format": "text"})
    assert reply.status_code == 200 and reply.text == "hello" and reply.headers["content-type"].startswith("text/plain")


@pytest.mark.anyio
async def test_a_clip_spends_no_free_trial_chat(voice):
    async with client() as c:
        assert (await say(c)).status_code == 200
    assert voice.quota.summary(5)["chats_used"] == 0


@pytest.mark.anyio
@pytest.mark.parametrize("fields, words", [
    ({"file": make_wav(1.0, channels=2)}, "mono"),
    ({"file": make_wav(1.0, rate=44100)}, "16 kHz"),
    ({"file": make_wav(61.0)}, "longer than 60 seconds"),
    ({"file": b"ID3 not a wav at all"}, "WAV"),
    ({"model": "whisper-1"}, "missing"),
    ({"file": make_wav(1.0), "language": "english-please"}, "ISO"),
    ({"file": make_wav(1.0), "response_format": "srt"}, "json or text"),
])
async def test_audio_the_engine_does_not_take_is_400_with_a_plain_reason_and_is_never_queued(voice, fields, words):
    async with client() as c:
        reply = await say(c, fields)
    assert reply.status_code == 400 and reply.json()["code"] == "voice_bad_audio" and words in reply.json()["detail"]
    assert voice.member.calls == [] and voice.service.status_block()["queue"] == 0


@pytest.mark.anyio
async def test_a_body_that_is_not_multipart_is_400(voice):
    async with client() as c:
        reply = await c.post(PATH, json={"file": "x"})
    assert reply.status_code == 400 and reply.json()["code"] == "voice_bad_audio"


@pytest.mark.anyio
async def test_more_than_two_megabytes_is_400_and_the_longest_clip_is_the_setting(voice):
    async with client() as c:
        big = await say(c, {"file": make_wav(1.0, pad=2 * 1024 * 1024)})
        voice.limits["voice_max_seconds"] = 5
        long = await say(c, {"file": make_wav(6.0)})
        fine = await say(c, {"file": make_wav(5.0)})
    assert big.status_code == 400 and "2 MB" in big.json()["detail"]
    assert long.status_code == 400 and "5 seconds" in long.json()["detail"]
    assert fine.status_code == 200


@pytest.mark.anyio
async def test_an_oversize_body_is_refused_from_its_length_before_it_is_read(voice):
    content_type, body = multipart({"file": make_wav(1.0, pad=3 * 1024 * 1024)})
    async with client() as c:
        reply = await c.post(PATH, content=body, headers={"Content-Type": content_type})
    assert reply.status_code == 400 and reply.json()["code"] == "voice_bad_audio"


# --- quota, queue, unavailable, paused ----------------------------------------------------------------------

@pytest.mark.anyio
async def test_a_network_has_so_many_clips_an_hour_then_429_voice_quota(voice):
    async with client() as c, client(OTHER) as other:
        for _ in range(3):
            assert (await say(c)).status_code == 200
        reply = await say(c)
        assert reply.status_code == 429 and reply.json()["code"] == "voice_quota"
        assert int(reply.headers["retry-after"]) >= 1 and "hour" in reply.json()["detail"]
        assert (await say(other)).status_code == 200
    assert voice.member.calls.__len__() == 4


@pytest.mark.anyio
async def test_a_full_waiting_list_is_503_voice_busy(voice):
    voice.member.hold, voice.member.release = True, asyncio.Event()
    voice.limits["voice_per_hour"] = 60
    peers = [(f"203.0.113.{i}", 40000) for i in range(20, 26)]
    async with client(peers[0]) as c0, client(peers[1]) as c1, client(peers[2]) as c2, client(peers[3]) as c3:
        first = asyncio.create_task(say(c0))
        for _ in range(200):
            if voice.member.calls:
                break
            await asyncio.sleep(0.01)
        waiting = [asyncio.create_task(say(c1)), asyncio.create_task(say(c2))]
        for _ in range(200):
            if voice.service.status_block()["queue"] == 2:
                break
            await asyncio.sleep(0.01)
        refused = await say(c3)
        voice.member.release.set()
        results = [await first, *[await t for t in waiting]]
    assert refused.status_code == 503 and refused.json()["code"] == "voice_busy" and "Retry-After" in refused.headers
    assert all(r.status_code == 200 for r in results)


@pytest.mark.anyio
async def test_a_member_that_cannot_serve_is_503_unavailable(voice):
    voice.member.reason = "the voice model is missing (/x)"
    async with client() as c:
        reply = await say(c)
    assert reply.status_code == 503 and reply.json() == {"detail": coordinator.VOICE_UNAVAILABLE, "code": "voice_unavailable"}
    assert "/x" not in reply.text, "the reason is for the log, not the client"


@pytest.mark.anyio
async def test_a_card_that_turns_out_short_is_503_unavailable_with_the_engines_sentence(voice):
    voice.member.error = MemberError("voice_unavailable", "The graphics card doesn't have enough free memory for voice right now.")
    async with client() as c:
        reply = await say(c)
    assert reply.status_code == 503 and reply.json()["code"] == "voice_unavailable" and "memory" in reply.json()["detail"]


@pytest.mark.anyio
async def test_an_engine_failure_is_a_5xx_with_its_code_and_the_hour_is_given_back(voice):
    voice.member.error = MemberError("voice_failed", "The voice engine stopped. Try again in a moment.")
    async with client() as c:
        for _ in range(5):
            assert (await say(c)).status_code == 502
        voice.member.error = None
        assert (await say(c)).status_code == 200


@pytest.mark.anyio
async def test_a_request_never_waits_for_ever_on_a_hung_engine(voice, monkeypatch):
    monkeypatch.setattr(coordinator, "VOICE_REQUEST_TIMEOUT_SECONDS", 0.3)
    voice.member.hold, voice.member.release = True, asyncio.Event()
    try:
        async with client() as c:
            reply = await asyncio.wait_for(say(c), 10)
    finally:
        voice.member.release.set()
    assert reply.status_code == 503 and reply.json()["code"] == "voice_busy"


@pytest.mark.anyio
async def test_while_paused_the_answer_is_503_paused_and_nothing_is_asked_or_queued(voice):
    voice.limits.update(voice_enabled=False, voice_paused=True)
    asked = []
    voice.member.unavailable_reason = lambda: asked.append(1)
    async with client() as c:
        reply = await say(c)
    assert reply.status_code == 503 and reply.json() == {"detail": coordinator.VOICE_PAUSED, "code": "voice_paused"}
    assert voice.member.calls == [] and asked == [] and voice.quota.summary(5)["chats_used"] == 0


# --- who may use it ----------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_a_caller_whose_free_trial_is_used_up_can_still_use_voice_and_spends_nothing(voice):
    # Owner, 2.8.3: the voice button in the Android app answered "DLNE Compute Required". Voice follows the chat its mic
    # sits in: the phone app's and the web chat's chat is free for anyone, so voice is too.
    for _ in range(5):
        voice.quota.check_and_consume("203.0.113.9", limit=5)
    voice.limits["voice_per_hour"] = 60
    async with client() as c:
        reply = await say(c)
    assert reply.status_code == 200 and "Compute Required" not in reply.text
    assert len(voice.member.calls) == 1 and voice.quota.summary(5)["chats_used"] == 5


@pytest.mark.anyio
async def test_the_host_a_key_holder_a_contributor_and_anyone_else_may_all_use_voice(voice, monkeypatch):
    monkeypatch.setattr(coordinator.api_key_manager, "verify_key", lambda token: token == "good-key")
    for _ in range(5):
        voice.quota.check_and_consume("192.0.2.77", limit=5)
        voice.quota.check_and_consume("203.0.113.9", limit=5)
    voice.limits["voice_per_hour"] = 60
    async with client(LOCAL) as local, client(("192.0.2.77", 40002), {"Authorization": "Bearer good-key"}) as keyed, \
            client(OTHER) as anyone:
        assert (await say(local)).status_code == 200
        assert (await say(keyed)).status_code == 200
        assert (await say(anyone)).status_code == 200


@pytest.mark.anyio
async def test_a_key_holders_clip_goes_ahead_of_a_trials_in_the_queue(voice, monkeypatch):
    monkeypatch.setattr(coordinator.api_key_manager, "verify_key", lambda token: token == "good-key")
    voice.member.hold, voice.member.release = True, asyncio.Event()
    voice.limits["voice_per_hour"] = 60
    async with client(("203.0.113.30", 1)) as t0, client(("203.0.113.31", 1)) as t1, \
            client(("192.0.2.77", 2), {"Authorization": "Bearer good-key"}) as keyed:
        running = asyncio.create_task(say(t0, {"file": make_wav(1.0), "language": "aa"}))
        for _ in range(200):
            if voice.member.calls:
                break
            await asyncio.sleep(0.01)
        trial = asyncio.create_task(say(t1, {"file": make_wav(1.0), "language": "tr"}))
        await asyncio.sleep(0.05)
        key = asyncio.create_task(say(keyed, {"file": make_wav(1.0), "language": "ke"}))
        await asyncio.sleep(0.05)
        voice.member.release.set()
        await asyncio.gather(running, trial, key)
    assert [call["language"] for call in voice.member.calls] == ["aa", "ke", "tr"]


# --- status ----------------------------------------------------------------------------------------------

async def status_of(engine, monkeypatch):
    async def snapshot(_name):
        return dict(engine.entry)

    monkeypatch.setattr(coordinator.manager, "asnapshot_model", snapshot)
    monkeypatch.setattr(coordinator.manager, "get_active_model_names", lambda: [MODEL])
    async with client(LOCAL) as c:
        return (await c.get("/v1/cluster/status")).json()


@pytest.mark.anyio
async def test_the_status_offers_voice_with_the_contracts_block(engine, voice, monkeypatch):
    status = await status_of(engine, monkeypatch)
    assert status["capabilities"]["voice"] is True
    assert status["voice"] == {"queue": 0, "busy": False, "max_seconds": 60, "languages": "auto"}


@pytest.mark.anyio
async def test_the_status_says_false_and_has_no_block_when_voice_cannot_run(engine, voice, monkeypatch):
    voice.member.reason = "the voice model is missing"
    status = await status_of(engine, monkeypatch)
    assert status["capabilities"]["voice"] is False and "voice" not in status


@pytest.mark.anyio
async def test_while_paused_the_status_says_false_and_paused(engine, voice, monkeypatch):
    voice.limits.update(voice_enabled=False, voice_paused=True)
    status = await status_of(engine, monkeypatch)
    assert status["capabilities"]["voice"] is False
    assert status["voice"]["paused"] is True and status["voice"]["languages"] == "auto"


@pytest.mark.anyio
async def test_the_status_counts_the_queue_without_naming_anybody(engine, voice, monkeypatch):
    voice.member.hold, voice.member.release = True, asyncio.Event()
    voice.limits["voice_per_hour"] = 60
    async with client() as c:
        task = asyncio.create_task(say(c))
        for _ in range(200):
            if voice.member.calls:
                break
            await asyncio.sleep(0.01)
        status = await status_of(engine, monkeypatch)
        voice.member.release.set()
        await task
    assert status["voice"]["busy"] is True and "203.0.113.9" not in str(status["voice"])


# --- the real engine behind the real route, and the audio is never written --------------------------------------

@pytest.fixture
async def real(monkeypatch, tmp_path, voice):
    whisper = FakeWhisperServer().start()
    binary = tmp_path / "bin-whisper" / "whisper-server"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\n")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    (tmp_path / "models" / "voice").mkdir(parents=True)
    (tmp_path / "models" / "voice" / "m.bin").write_bytes(b"x")
    voice.limits.update(media_gpu="0000:0c:00.0", voice_model="m.bin", voice_threads=2, voice_cpu_mask="",
                        voice_idle_unload_minutes=20, voice_min_free_vram_gb=1.2)
    from tests.voice_fakes import Spawner
    spawner = Spawner()
    member = VoiceMember(limits=lambda: coordinator.get_worker_limits(), base_dir=lambda: str(tmp_path), spawn=spawner,
                         media_spec=lambda limits: limits.get("media_gpu", ""),
                         media_card=lambda spec: {"vk_index": 2, "free_gb": 6.0, "total_gb": 8.0},
                         poll_seconds=0.01, ready_timeout=3.0, clip_timeout=3.0, free_port=lambda: whisper.port)
    service = VoiceService(member, limits=lambda: coordinator.get_worker_limits(), housekeeping=0.05)
    monkeypatch.setattr(coordinator, "voice_service", service)
    yield SimpleNamespace(whisper=whisper, spawner=spawner, member=member, service=service)
    await service.shutdown()
    whisper.stop()


@pytest.mark.anyio
async def test_a_clip_goes_through_the_real_member_to_the_fake_whisper_and_no_audio_touches_the_disk(real):
    before = set(os.listdir(tempfile.gettempdir()))
    async with client() as c:
        reply = await say(c, {"file": make_wav(1.5), "language": "de"})
    assert reply.status_code == 200
    assert reply.json() == {"text": "Hello there.", "language": "de", "duration_seconds": 1.5}
    (form,) = real.whisper.forms
    assert form["file"][:4] == b"RIFF" and form["language"] == b"de"
    cwd = real.spawner.starts[0]["cwd"]
    assert os.listdir(cwd) == []
    new = {n for n in set(os.listdir(tempfile.gettempdir())) - before if n.startswith("dlne") or ".wav" in n}
    assert new <= {os.path.basename(cwd)}


@pytest.mark.anyio
async def test_nothing_of_the_audio_or_the_text_is_logged(real, monkeypatch):
    lines = []
    for name in ("log_coordinator", "log_warning", "log_error"):
        for module in ("engine.voice_pool", "engine.voice_member", "engine.coordinator"):
            import importlib
            mod = importlib.import_module(module)
            if hasattr(mod, name):
                monkeypatch.setattr(mod, name, lambda text, *a, **k: lines.append(str(text)))
    real.whisper.text = " my secret sentence "
    async with client() as c:
        reply = await say(c)
    assert reply.json()["text"] == "my secret sentence"
    assert not any("secret" in line or "RIFF" in line or "clip.wav" in line for line in lines)
    assert any(line.startswith("Voice clip of 1.0 s, language en") for line in lines)


# --- the config keys ---------------------------------------------------------------------------------------

def limits_from(isolated_config, text):
    isolated_config.write_text(text)
    engine_config._cached_stamp = None
    return engine_config.get_plain_worker_limits()


def test_voice_enabled_takes_true_false_and_paused(isolated_config):
    on = limits_from(isolated_config, "VOICE_ENABLED=true\n")
    assert (on["voice_enabled"], on["voice_paused"]) == (True, False)
    paused = limits_from(isolated_config, "VOICE_ENABLED=Paused\n")
    assert (paused["voice_enabled"], paused["voice_paused"]) == (False, True)
    for word in ("false", "yes", "1", ""):
        off = limits_from(isolated_config, f"VOICE_ENABLED={word}\n")
        assert (off["voice_enabled"], off["voice_paused"]) == (False, False)


def test_every_number_keeps_its_default_outside_its_range_and_takes_a_good_value(isolated_config):
    text = ("VOICE_THREADS=99\nVOICE_MAX_SECONDS=4\nVOICE_PER_HOUR=0\nVOICE_QUEUE_MAX=abc\nVOICE_IDLE_UNLOAD_MINUTES=-1\n"
            "VOICE_MIN_FREE_VRAM_GB=nan\nVOICE_CPU_MASK=zz\nVOICE_MODEL=../evil.bin\n")
    bad = limits_from(isolated_config, text)
    assert (bad["voice_threads"], bad["voice_max_seconds"], bad["voice_per_hour"], bad["voice_queue_max"],
            bad["voice_idle_unload_minutes"], bad["voice_cpu_mask"], bad["voice_model"]) == (
        2, 60, 60, 8, 20, "", "ggml-large-v3-turbo-q8_0.bin")
    assert bad["voice_min_free_vram_gb"] == 1.4

    good = limits_from(isolated_config, "VOICE_THREADS=4\nVOICE_MAX_SECONDS=30\nVOICE_PER_HOUR=10\nVOICE_QUEUE_MAX=3\n"
                                        "VOICE_IDLE_UNLOAD_MINUTES=0\nVOICE_MIN_FREE_VRAM_GB=0.9\nVOICE_CPU_MASK=f0\n"
                                        "VOICE_MODEL=ggml-small.bin\n")
    assert (good["voice_threads"], good["voice_max_seconds"], good["voice_per_hour"], good["voice_queue_max"],
            good["voice_idle_unload_minutes"], good["voice_min_free_vram_gb"], good["voice_cpu_mask"],
            good["voice_model"]) == (4, 30, 10, 3, 0, 0.9, "0xf0", "ggml-small.bin")


# --- the takeover ------------------------------------------------------------------------------------------

def configure(box, text):
    box.config_path.write_text(text)
    engine_config._cached_stamp = None


def plan(box, **body):
    body.setdefault("gpus", [])
    body.setdefault("holder", "DLNE-Sec on portoBOX")
    return takeover.make_plan(takeover.parse_request(body), limits=engine_config.get_plain_worker_limits(), gpus=box.gpus,
                              facts=box.facts, memory_now=box.mem.read(), system_ram_gb=box.system_ram_gb, lease_active=False)


def test_a_lease_on_the_media_card_pauses_voice_like_pictures(box):
    configure(box, DEFAULT_CONFIG + "VOICE_ENABLED=true\n")
    result = plan(box, gpus=[MEDIA])
    assert result.pause_voice is True and result.pause_pictures is True
    assert result.keep_result["voice"] == "paused" and result.keep_result["pictures"] == "paused"
    assert result.overlay == {"vision_cpu": True, "pictures_paused": True, "voice_paused": True}


def test_a_lease_on_an_engine_card_leaves_voice_on(box):
    configure(box, DEFAULT_CONFIG + "VOICE_ENABLED=true\n")
    result = plan(box, gpus=[POOL_A])
    assert result.pause_voice is False and result.keep_result["voice"] == "on" and "voice_paused" not in result.overlay


def test_voice_that_is_already_paused_stays_paused_and_needs_no_pause(box):
    configure(box, DEFAULT_CONFIG + "VOICE_ENABLED=paused\n")
    result = plan(box, gpus=[MEDIA])
    assert result.keep_result["voice"] == "paused" and result.pause_voice is False and "voice_paused" not in result.overlay


def test_voice_that_is_off_is_not_mentioned_at_all(box):
    result = plan(box, gpus=[MEDIA])
    assert "voice" not in result.keep_result and result.pause_voice is False and "voice_paused" not in result.overlay


def test_the_overlay_turns_voice_off_and_paused():
    out = takeover.overlay_limits({"voice_enabled": True, "voice_paused": False}, {"voice_paused": True})
    assert (out["voice_enabled"], out["voice_paused"]) == (False, True)
    untouched = takeover.overlay_limits({"voice_enabled": True, "voice_paused": False}, {})
    assert (untouched["voice_enabled"], untouched["voice_paused"]) == (True, False)


@pytest.mark.anyio
async def test_a_grant_on_the_media_card_with_only_voice_on_lets_go_of_the_card_and_dlne_now_says_so(box):
    configure(box, DEFAULT_CONFIG.replace("IMAGE_GEN_ENABLED=true\n", "") + "VOICE_ENABLED=true\n")
    lease = await box.controller().grant({"gpus": [MEDIA], "holder": "DLNE-Sec on portoBOX"})
    assert lease["dlne_now"]["voice"] == "paused" and lease["dlne_now"]["pictures"] == "off"
    assert box.pictures_stopped == 1, "the host's media-card hook ran for voice alone"
    assert engine_config.get_worker_limits()["voice_paused"] is True and engine_config.get_worker_limits()["voice_enabled"] is False


@pytest.mark.anyio
async def test_a_grant_that_does_not_touch_the_media_card_leaves_voice_alone(box):
    configure(box, DEFAULT_CONFIG + "VOICE_ENABLED=true\n")
    lease = await box.controller().grant({"gpus": [POOL_A], "holder": "h"})
    assert lease["dlne_now"]["voice"] == "on" and box.pictures_stopped == 0
    assert engine_config.get_worker_limits()["voice_enabled"] is True


@pytest.mark.anyio
async def test_the_coordinators_media_card_hook_stops_both_engines_and_the_idle_test_counts_a_clip(monkeypatch):
    stopped = []

    class Member:
        def __init__(self, name):
            self.name = name

        async def stop(self):
            stopped.append(self.name)

    monkeypatch.setattr(coordinator, "image_service", SimpleNamespace(member=Member("sd"), active=None, _pending=[]))
    monkeypatch.setattr(coordinator, "voice_service", SimpleNamespace(member=Member("whisper"), idle=True))
    await coordinator._takeover_pause_pictures()
    assert stopped == ["sd", "whisper"]

    monkeypatch.setattr(coordinator, "_coding_reconfiguration_running", lambda: False)
    monkeypatch.setattr(coordinator, "_coding_queue_busy", lambda: False)
    assert coordinator._takeover_idle() is True
    monkeypatch.setattr(coordinator, "voice_service", SimpleNamespace(member=Member("whisper"), idle=False))
    assert coordinator._takeover_idle() is False
