"""
Voice clips (engine/voice_pool.py): the WAV check, the multipart reader, the queue (one at a time, key holders first), the
hourly cap, the waiting list's limit, the wait for a picture, and that the audio is dropped once it has run. The voice engine
is a stand-in here; its lifecycle is tests/test_voice_member.py.
"""

import asyncio
import time

import pytest

from engine.image_pool import Rejected
from engine.priority import tier_priority
from engine.voice_member import Cancelled, MemberError
from engine.voice_pool import BadAudio, VoiceService, parse_form, parse_wav
from tests.voice_fakes import make_wav, multipart

A, B = "203.0.113.9", "198.51.100.20"


# --- the WAV check ----------------------------------------------------------------------------------------

def test_a_good_clip_passes_and_its_length_comes_from_the_header():
    wav = make_wav(2.5)
    clip, seconds = parse_wav(wav, 60)
    assert clip is wav and seconds == 2.5


def test_a_header_with_other_chunks_before_the_data_is_read():
    assert parse_wav(make_wav(1.0, extra_chunk=True), 60)[1] == 1.0


def test_a_header_that_claims_more_data_than_came_is_cut_to_what_came():
    wav = make_wav(1.0)[:-16000]            # half a second gone
    assert parse_wav(wav, 60)[1] == 0.5


@pytest.mark.parametrize("wav, words", [
    (make_wav(1.0, channels=2), "mono"),
    (make_wav(1.0, rate=44100), "16 kHz"),
    (make_wav(1.0, rate=8000), "16 kHz"),
    (make_wav(1.0, bits=8), "16-bit"),
    (make_wav(1.0, bits=32), "16-bit"),
    (make_wav(1.0, tag=3), "16-bit PCM"),
    (make_wav(61.0), "longer than 60 seconds"),
    (make_wav(0.0), "empty"),
    (b"ID3\x04\x00\x00" + b"\x00" * 100, "WAV"),
    (b"", "WAV"),
    (b"RIFF", "WAV"),
    (b"RIFF\x00\x00\x00\x00WAVE", "WAV"),
    (b"RIFF\x00\x00\x00\x00WAVEfmt \x10\x00\x00\x00", "WAV"),
])
def test_anything_else_is_refused_with_a_plain_reason(wav, words):
    with pytest.raises(BadAudio) as caught:
        parse_wav(wav, 60)
    assert words in caught.value.detail


def test_more_than_two_megabytes_is_refused_even_when_the_seconds_allow_it():
    with pytest.raises(BadAudio) as caught:
        parse_wav(make_wav(1.0, pad=2 * 1024 * 1024), 600)
    assert "2 MB" in caught.value.detail


def test_the_longest_clip_is_the_setting():
    assert parse_wav(make_wav(10.0), 10)[1] == 10.0
    with pytest.raises(BadAudio):
        parse_wav(make_wav(10.5), 10)


def test_sixty_seconds_fit_in_two_megabytes():
    assert len(make_wav(60.0)) < 2 * 1024 * 1024 and parse_wav(make_wav(60.0), 60)[1] == 60.0


# --- the multipart reader ---------------------------------------------------------------------------------

def test_a_form_gives_back_its_fields_and_the_file_untouched():
    wav = make_wav(0.5)
    content_type, body = multipart({"file": wav, "model": "whisper-1", "language": "en"})
    fields = parse_form(content_type, body)
    assert fields == {"file": wav, "model": b"whisper-1", "language": b"en"}


def test_binary_data_that_looks_like_framing_survives():
    data = b"\r\n\r\n--XBOUNDARY\r\nContent-Disposition: form-data; name=\"x\"\r\n\r\n" + bytes(range(256))
    content_type, body = multipart({"file": data})
    assert parse_form(content_type, body)["file"] == data


@pytest.mark.parametrize("content_type", ["application/json", "", "multipart/form-data", "multipart/form-data; charset=x"])
def test_anything_that_is_not_a_multipart_form_is_refused(content_type):
    with pytest.raises(BadAudio):
        parse_form(content_type, b"{}")


def test_a_quoted_boundary_works_and_a_junk_body_gives_no_fields():
    assert parse_form('multipart/form-data; boundary="abc"', b"nothing here") == {}
    content_type, body = multipart({"file": b"x"}, boundary="abc")
    assert parse_form(content_type.replace("abc", '"abc"'), body) == {"file": b"x"}


# --- the queue --------------------------------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeMember:
    """The voice engine as the service uses it."""

    def __init__(self):
        self.calls = []
        self.running = 0
        self.most_running = 0
        self.hold = False
        self.release = None
        self.error = None
        self.reason = None
        self.room = True
        self.room_asked = 0
        self.stopped = False
        self.unloads = 0

    def unavailable_reason(self):
        return self.reason

    async def card_has_room(self):
        self.room_asked += 1
        return self.room, None

    async def transcribe(self, wav, language=None, cancelled=lambda: False):
        self.calls.append({"wav": wav, "language": language})
        self.running += 1
        self.most_running = max(self.most_running, self.running)
        try:
            while self.hold and not self.release.is_set():
                if cancelled():
                    raise Cancelled()
                await asyncio.sleep(0.005)
            if self.error is not None:
                raise self.error
            return "hello", language or "en"
        finally:
            self.running -= 1

    async def maybe_unload(self):
        self.unloads += 1
        return False

    def stop_now(self):
        self.stopped = True


class Env:
    pass


@pytest.fixture
def env():
    e = Env()
    e.limits = {"voice_queue_max": 3, "voice_per_hour": 4, "voice_max_seconds": 60}
    e.member, e.clock = FakeMember(), Clock()
    e.picture = [False]
    e.service = VoiceService(e.member, limits=lambda: e.limits, clock=e.clock, picture_busy=lambda: e.picture[0],
                             picture_wait=0.5, picture_poll=0.01, housekeeping=0.05)
    return e


def submit(e, owner=A, language=None, **kwargs):
    return e.service.submit(owner, make_wav(0.5), 0.5, language, **kwargs)


async def until(predicate, timeout=5.0):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("timed out waiting")


@pytest.mark.anyio
async def test_a_clip_is_run_and_answered_and_the_audio_is_dropped(env):
    clip = submit(env, language="de")
    assert await clip.future == ("hello", "de")
    assert env.member.calls[0]["language"] == "de" and env.member.calls[0]["wav"][:4] == b"RIFF"
    assert clip.wav is None, "the audio is not kept once it has run"
    await until(lambda: env.service.idle)


@pytest.mark.anyio
async def test_clips_run_one_at_a_time_in_the_order_they_came(env):
    env.member.hold, env.member.release = True, asyncio.Event()
    clips = [submit(env, owner=f"10.0.0.{i}", language=f"l{i}") for i in range(3)]
    await until(lambda: len(env.member.calls) == 1)
    await asyncio.sleep(0.05)
    assert len(env.member.calls) == 1 and env.service.status_block()["queue"] == 2 and env.service.status_block()["busy"]
    env.member.release.set()
    for clip in clips:
        await clip.future
    assert [c["language"] for c in env.member.calls] == ["l0", "l1", "l2"] and env.member.most_running == 1


@pytest.mark.anyio
async def test_a_waiting_key_holders_clip_runs_before_a_trials_and_the_running_one_is_not_preempted(env):
    env.member.hold, env.member.release = True, asyncio.Event()
    running = submit(env, owner="10.0.0.1", language="running", priority=tier_priority("trial"))
    await until(lambda: len(env.member.calls) == 1)
    trial = submit(env, owner="10.0.0.2", language="trial", priority=tier_priority("trial"))
    key = submit(env, owner="10.0.0.3", language="key", priority=tier_priority("key"))
    gpu = submit(env, owner="10.0.0.4", language="gpu", priority=tier_priority("gpu"))
    env.member.release.set()
    for clip in (running, trial, key, gpu):
        await clip.future
    order = [c["language"] for c in env.member.calls]
    assert order[0] == "running" and order[-1] == "trial" and set(order[1:3]) == {"key", "gpu"}


@pytest.mark.anyio
async def test_clips_of_one_tier_keep_their_order(env):
    env.member.hold, env.member.release = True, asyncio.Event()
    first = submit(env, owner="10.0.0.1", language="first", priority=tier_priority("key"))
    await until(lambda: len(env.member.calls) == 1)
    clips = [submit(env, owner=f"10.0.1.{i}", language=f"k{i}", priority=tier_priority("key")) for i in range(2)]
    env.member.release.set()
    for clip in [first, *clips]:
        await clip.future
    assert [c["language"] for c in env.member.calls] == ["first", "k0", "k1"]


# --- limits -----------------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_the_waiting_list_is_limited_and_says_busy(env):
    env.member.hold, env.member.release = True, asyncio.Event()
    env.limits["voice_per_hour"] = 60
    submit(env, owner="10.0.0.0")
    await until(lambda: len(env.member.calls) == 1)
    for i in range(3):
        submit(env, owner=f"10.0.0.{i + 1}")
    with pytest.raises(Rejected) as caught:
        submit(env, owner="10.0.0.9")
    assert caught.value.code == "voice_busy" and caught.value.retry_after >= 1
    env.member.release.set()


@pytest.mark.anyio
async def test_a_network_has_so_many_clips_an_hour_and_the_hour_rolls(env):
    for _ in range(4):
        await submit(env).future
    with pytest.raises(Rejected) as caught:
        submit(env)
    assert caught.value.code == "voice_quota" and 1 <= caught.value.retry_after <= 3601
    await submit(env, owner=B).future                      # another network is not affected
    env.clock.t += 3601
    await submit(env).future


@pytest.mark.anyio
async def test_a_clip_that_could_not_be_run_gives_its_hour_back(env):
    env.member.error = MemberError("voice_failed", "no")
    for _ in range(6):
        with pytest.raises(MemberError):
            await submit(env).future
    env.member.error = None
    await submit(env).future


@pytest.mark.anyio
async def test_a_cancelled_queued_clip_is_dropped_unrun_and_its_hour_is_given_back(env):
    env.member.hold, env.member.release = True, asyncio.Event()
    running = submit(env, owner=B)
    await until(lambda: len(env.member.calls) == 1)
    waiting = submit(env)
    env.service.cancel(waiting)
    assert waiting.wav is None and env.service.status_block()["queue"] == 0
    env.member.release.set()
    await running.future
    assert len(env.member.calls) == 1
    for _ in range(4):
        await submit(env).future                           # all four of the hour are still there


# --- the picture --------------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_a_clip_waits_for_a_picture_when_the_card_is_short_and_never_stops_it(env):
    env.service._clock = time.monotonic
    env.member.room, env.picture[0] = False, True
    clip = submit(env)
    await asyncio.sleep(0.1)
    assert env.member.calls == [] and not clip.future.done() and env.member.room_asked > 1
    env.picture[0] = False
    assert await clip.future == ("hello", "en")
    assert not hasattr(env.member, "evicted")


@pytest.mark.anyio
async def test_a_clip_does_not_wait_when_the_card_has_room_even_with_a_picture(env):
    env.picture[0] = True
    assert await submit(env).future == ("hello", "en")


@pytest.mark.anyio
async def test_a_short_card_with_no_picture_goes_straight_to_the_engine_which_answers_unavailable(env):
    env.member.room = False
    env.member.error = MemberError("voice_unavailable", "The graphics card doesn't have enough free memory for voice right now.")
    with pytest.raises(MemberError) as caught:
        await submit(env).future
    assert caught.value.code == "voice_unavailable" and len(env.member.calls) == 1


@pytest.mark.anyio
async def test_a_clip_that_waited_too_long_for_a_picture_is_given_to_the_engine_anyway(env):
    env.service._clock = time.monotonic              # the wait is measured in real time (the fixture's clock stands still)
    env.member.room, env.picture[0] = False, True
    env.member.error = MemberError("voice_unavailable", "short of memory")
    with pytest.raises(MemberError):
        await submit(env).future                           # picture_wait is 0.5 s here
    assert len(env.member.calls) == 1


@pytest.mark.anyio
async def test_a_clip_that_cannot_be_served_does_not_wait_for_pictures(env):
    env.member.reason = "the voice model is missing"
    env.member.room, env.picture[0] = False, True
    env.member.error = MemberError("voice_unavailable", "x")
    with pytest.raises(MemberError):
        await asyncio.wait_for(submit(env).future, 0.3)
    assert env.member.room_asked == 0


# --- status, housekeeping and shutdown --------------------------------------------------------------------

@pytest.mark.anyio
async def test_the_status_block_is_the_contracts_and_names_nobody(env):
    assert env.service.status_block() == {"queue": 0, "busy": False, "max_seconds": 60, "languages": "auto"}
    env.limits["voice_max_seconds"] = 30
    assert env.service.status_block()["max_seconds"] == 30


@pytest.mark.anyio
async def test_an_idle_worker_asks_the_engine_to_unload_and_shutdown_stops_it(env):
    submit(env)
    await until(lambda: env.member.unloads >= 1)
    await env.service.shutdown()
    assert env.member.stopped


@pytest.mark.anyio
async def test_an_engine_that_blows_up_does_not_end_the_worker(env):
    env.member.error = RuntimeError("boom")
    with pytest.raises(MemberError) as caught:
        await submit(env).future
    assert caught.value.code == "voice_failed"
    env.member.error = None
    assert await submit(env, owner=B).future == ("hello", "en")
