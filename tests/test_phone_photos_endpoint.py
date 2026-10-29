"""
Photos in the phone chat (2.6.2) through the coordinator: POST /v1/app/chat/completions with `image_url` parts and
GET /v1/cluster/status `capabilities.app_vision`. The pieces (what a turn becomes, fitting a lane) are in
tests/test_phone_photos.py.

The environment is the one tests/test_phone_lane_endpoint.py builds (a stand-in engine with two phone lanes, a stand-in
CPU-pool member that records every body it is sent, a stub manager) with vision switched on: the manager says a projector
is set up and the engine says it can read images. "Never reached a volunteer" means the CPU stand-in recorded nothing.
"""

import asyncio
import base64
import hashlib
import itertools
import json
import re
import zlib
from types import SimpleNamespace

import pytest

from engine import app_pool as app_pool_module
from engine import chat_privacy, coordinator, media
from engine import logger as dlne_logger
from engine.priority import DistinctWindow
from tests.media_samples import png as make_png
from tests.media_samples import png_with_stream
from tests.test_phone_lane_endpoint import (  # noqa: F401 - env and servers are fixtures
    CHAT_A, CHAT_B, CODING_SLOTS, OTHER, PHONE_SLOTS, REMOTE, client, env, nothing_held, phone_body, servers, system_of,
    until,
)

PHOTO_NOTICE = app_pool_module.PHOTO_UNAVAILABLE_NOTICE
RESTARTING = app_pool_module.RESTARTING_NOTICE
_sizes = itertools.count(1)


def b64(raw):
    return base64.b64encode(raw).decode()


def png_url(width=None, height=48):
    return "data:image/png;base64," + b64(make_png(width or 8 + next(_sizes), height))


def photo(url=None):
    """An image part. Each call without a url makes a different picture (the budget counts distinct ones)."""
    return {"type": "image_url", "image_url": {"url": url or png_url()}}


def words(text):
    return {"type": "text", "text": text}


def photo_body(text="what is this?", photos=None, chat_id=CHAT_A, **extra):
    parts = ([words(text)] if text else []) + (photos if photos is not None else [photo()])
    body = {"messages": [{"role": "user", "content": parts}], **extra}
    if chat_id:
        body["chat_id"] = chat_id
    return body


async def send(c, body):
    return await c.post("/v1/app/chat/completions", json=body)


@pytest.fixture
def lane_env(env, monkeypatch):
    """The phone lane environment with vision on. ``env.vision`` is what the engine says it can read (None: it does
    not answer), and every test starts with a budget nobody has spent."""
    env.manager.vision_configured = lambda: True
    env.vision = True

    async def snapshot_vision(snapshot):
        return env.vision

    monkeypatch.setattr(coordinator, "_snapshot_vision", snapshot_vision)
    monkeypatch.setattr(coordinator, "VISION_ASK_RETRY_SECONDS", 0.0)
    monkeypatch.setattr(coordinator, "image_input_window", DistinctWindow())
    real_cpu_pool = coordinator._answer_on_cpu_pool

    def cpu_pool(body, *args, **kwargs):
        assert not app_pool_module.has_photos(body), "a request with photos was given to the CPU pool"
        return real_cpu_pool(body, *args, **kwargs)

    monkeypatch.setattr(coordinator, "_answer_on_cpu_pool", cpu_pool)
    return env


@pytest.fixture
def logged(monkeypatch):
    """Every line any log helper hands to the loggers, as it is handed over (before the console renders it)."""
    lines = []

    class Recorder:
        def info(self, message):
            lines.append(message)

        warning = error = info

    for name in ("sys_logger", "worker_logger", "coord_logger"):
        monkeypatch.setattr(dlne_logger, name, Recorder())
    return lines


# ======================================================================================
# A photo reaches the engine's lane
# ======================================================================================

@pytest.mark.anyio
async def test_a_photo_reaches_the_engines_phone_lane_as_an_image_part_and_no_cpu_member_is_asked(lane_env):
    shot = photo()
    async with client() as c:
        resp = await send(c, photo_body("what is this?", [shot]))

    assert resp.status_code == 200 and b"engine-one" in resp.content
    assert lane_env.cpu.payloads == [], "a volunteer's machine is never asked"
    (sent,) = lane_env.engine.chats
    assert sent["messages"][1] == {"role": "user", "content": [words("what is this?"), shot]}
    assert sent["id_slot"] in range(CODING_SLOTS, CODING_SLOTS + PHONE_SLOTS) and sent["n_cache_reuse"] == 0
    assert system_of(sent).endswith(app_pool_module.PHOTO_INSTRUCTIONS + " " + chat_privacy.salt_line(CHAT_A))
    assert lane_env.engine.tokenized >= 1 and nothing_held(lane_env)


@pytest.mark.anyio
async def test_a_photo_alone_is_a_complete_request_and_four_are_fine(lane_env):
    shots = [photo() for _ in range(4)]
    async with client() as c:
        alone = await send(c, photo_body(None, [shots[0]]))
        four = await send(c, photo_body("which is biggest?", shots))

    assert alone.status_code == 200 and four.status_code == 200
    first, second = lane_env.engine.chats
    assert first["messages"][1]["content"] == [shots[0]]
    assert second["messages"][1]["content"] == [words("which is biggest?")] + shots


@pytest.mark.anyio
async def test_the_photo_stays_in_the_chat_after_the_question_about_it_and_the_later_turns_stay_text(lane_env):
    shot = photo()
    body = {"chat_id": CHAT_A, "messages": [
        {"role": "user", "content": [words("what is this?"), shot]}, {"role": "assistant", "content": "a red car"},
        {"role": "user", "content": "what colour is it?"}]}
    async with client() as c:
        resp = await send(c, body)

    assert resp.status_code == 200
    (sent,) = lane_env.engine.chats
    assert sent["messages"][1:] == body["messages"]
    assert app_pool_module.PHOTO_INSTRUCTIONS in system_of(sent)


@pytest.mark.anyio
async def test_what_a_photo_costs_the_lane_is_the_engines_image_tokens_setting(lane_env):
    shots = [photo() for _ in range(3)]
    async with client() as c:
        fits = await send(c, photo_body("what are these?", shots))               # 3 x 1,024 of an 8,192 lane
        lane_env.limits["engine_image_max_tokens"] = 3000                          # 9,000: a photo costs more
        too_long = await send(c, photo_body("and these?", shots))

    assert fits.status_code == 200 and b"engine-one" in fits.content
    assert too_long.status_code == 200 and app_pool_module.TOO_LONG_NOTICE.encode() in too_long.content
    assert len(lane_env.engine.chats) == 1, "the request whose photos cannot fit never reached the engine"
    assert lane_env.cpu.payloads == [] and nothing_held(lane_env)


@pytest.mark.anyio
async def test_a_part_that_is_not_text_or_an_image_is_dropped_not_refused_and_never_reaches_the_engine(lane_env):
    shot = photo()
    body = photo_body("look", [{"type": "video_url", "video_url": {"url": "http://169.254.169.254/x.mp4"}}, shot,
                               {"type": "input_audio", "input_audio": {"data": "AAAA", "format": "wav"}}])
    async with client() as c:
        resp = await send(c, body)

    assert resp.status_code == 200
    assert lane_env.engine.chats[0]["messages"][1] == {"role": "user", "content": [words("look"), shot]}
    assert "169.254" not in str(lane_env.engine.chats[0])


@pytest.mark.anyio
async def test_an_image_in_a_turn_the_model_never_sees_is_not_checked_and_makes_no_photo_request(lane_env):
    body = {"chat_id": CHAT_A, "messages": [
        {"role": "assistant", "content": [photo("https://example.com/cat.png")]}, {"role": "user", "content": "hello"}]}
    async with client() as c:
        resp = await send(c, body)

    assert resp.status_code == 200 and b"engine-one" in resp.content
    (sent,) = lane_env.engine.chats
    assert sent["messages"][1:] == [{"role": "user", "content": "hello"}] and "photo" not in system_of(sent)


@pytest.mark.anyio
async def test_a_request_without_photos_is_what_2_6_1_sent_the_engine(lane_env):
    async with client() as c:
        resp = await send(c, phone_body("hello"))

    assert resp.status_code == 200
    (sent,) = lane_env.engine.chats
    assert set(sent) - {"tools"} == {"temperature", "top_p", "top_k", "presence_penalty", "chat_template_kwargs",
                                     "stream", "max_tokens", "messages", "id_slot", "n_cache_reuse"}
    assert sent["messages"][1:] == [{"role": "user", "content": "hello"}] and "photo" not in system_of(sent)
    assert system_of(sent).endswith(chat_privacy.salt_line(CHAT_A))


# ======================================================================================
# What is refused, with the codes the coding chat uses
# ======================================================================================

NO_DATA = {"type": "image_url", "image_url": {"url": ""}}
BAD_PHOTOS = [
    ("five photos", lambda: [photo() for _ in range(5)], media.TOO_MANY_IMAGES),
    ("a link", lambda: [photo("https://example.com/cat.png")], media.REMOTE_IMAGE_URL),
    ("a file path", lambda: [photo("file:///etc/passwd")], media.REMOTE_IMAGE_URL),
    ("webp", lambda: [photo("data:image/webp;base64," + b64(b"RIFF\0\0\0\0WEBPVP8 "))], media.UNSUPPORTED_IMAGE_TYPE),
    ("a gif that says png", lambda: [photo("data:image/png;base64," + b64(b"GIF89a" + bytes(40)))],
     media.UNSUPPORTED_IMAGE_TYPE),
    ("bytes that are not base64", lambda: [photo("data:image/png;base64,!!!not base64!!!")],
     media.UNSUPPORTED_IMAGE_TYPE),
    ("a part with no data", lambda: [NO_DATA], media.UNSUPPORTED_IMAGE_TYPE),
    ("a part with no image_url", lambda: [{"type": "image_url"}], media.UNSUPPORTED_IMAGE_TYPE),
    ("too many pixels across", lambda: [photo(png_url(5000, 4))], media.IMAGE_TOO_LARGE),
    ("a one pixel png that inflates to 20 MB",
     lambda: [photo("data:image/png;base64," + b64(png_with_stream(1, 1, zlib.compress(bytes(20_000_000), 9))))],
     media.IMAGE_TOO_LARGE),
]


@pytest.mark.anyio
@pytest.mark.parametrize("make, code", [(make, code) for _name, make, code in BAD_PHOTOS],
                         ids=[name for name, _make, _code in BAD_PHOTOS])
async def test_a_photo_the_coding_chat_would_refuse_gets_its_400_and_code_and_costs_nothing(lane_env, make, code):
    lane_env.limits["image_input_per_hour"] = 1
    async with client() as c:
        refused = await send(c, photo_body("what is this?", make()))
        accepted = await send(c, photo_body("and this?", [photo()]))          # the one photo of the budget is still there

    assert refused.status_code == 400
    assert refused.json() == {"detail": refused.json()["detail"], "code": code} and refused.json()["detail"]
    assert accepted.status_code == 200 and len(lane_env.engine.chats) == 1
    assert lane_env.cpu.payloads == [] and nothing_held(lane_env)


@pytest.mark.anyio
async def test_a_request_that_is_not_a_photo_request_is_never_a_media_error(lane_env):
    async with client() as c:
        resp = await send(c, {"chat_id": CHAT_A, "messages": [
            {"role": "user", "content": [words("hello"), {"type": "video_url", "video_url": {"url": "x"}}]}]})

    assert resp.status_code == 200 and lane_env.engine.chats[0]["messages"][1] == {"role": "user", "content": "hello"}


@pytest.mark.anyio
async def test_an_engine_that_cannot_read_images_says_so_at_once_and_the_budget_is_not_spent(lane_env):
    lane_env.limits["image_input_per_hour"] = 1
    lane_env.vision = False
    async with client() as c:
        for _ in range(3):
            refused = await send(c, photo_body())
        lane_env.vision = True
        accepted = await send(c, photo_body())

    assert refused.status_code == 400 and refused.json() == {
        "detail": "This server's coding engine cannot read images.", "code": media.IMAGE_INPUT_UNAVAILABLE}
    assert accepted.status_code == 200 and len(lane_env.engine.chats) == 1 and nothing_held(lane_env)


@pytest.mark.anyio
async def test_an_engine_that_does_not_answer_for_its_vision_is_asked_again_and_then_told_it_cannot_read_images(
        lane_env, monkeypatch):
    asked = []

    async def silent(snapshot):
        asked.append(1)
        return None

    monkeypatch.setattr(coordinator, "_snapshot_vision", silent)
    async with client() as c:
        resp = await send(c, photo_body())

    assert resp.status_code == 400 and resp.json()["code"] == media.IMAGE_INPUT_UNAVAILABLE
    assert len(asked) == 1 + coordinator.VISION_ASK_ATTEMPTS, "once in the early check, then the attempts of the late one"
    assert lane_env.engine.chats == [] and nothing_held(lane_env)


@pytest.mark.anyio
async def test_with_no_engine_and_no_projector_set_up_a_photo_is_refused_rather_than_told_to_wait(lane_env):
    lane_env.manager.running = False
    lane_env.manager.vision_configured = lambda: False
    async with client() as c:
        resp = await send(c, photo_body())

    assert resp.status_code == 400 and resp.json()["code"] == media.IMAGE_INPUT_UNAVAILABLE
    assert lane_env.cpu.payloads == [] and nothing_held(lane_env)


# ======================================================================================
# The hourly budget counts each distinct photo once
# ======================================================================================

@pytest.mark.anyio
async def test_the_budget_counts_a_resent_photo_once(lane_env):
    lane_env.limits["image_input_per_hour"] = 2
    a, b, c_new = photo(), photo(), photo()
    first = photo_body("what is this?", [a])
    follow_up = {"chat_id": CHAT_A, "messages": [first["messages"][0], {"role": "assistant", "content": "a car"},
                                                 {"role": "user", "content": "what colour is it?"}]}
    async with client() as c:
        assert (await send(c, first)).status_code == 200                           # a: one of two
        for _ in range(3):
            assert (await send(c, follow_up)).status_code == 200                    # the same photo again: free
        assert (await send(c, photo_body("and this one?", [b, b, b]))).status_code == 200      # b x3: two of two
        refused = await send(c, photo_body("and this?", [c_new]))                  # a third photo: over the budget
        again = await send(c, follow_up)                                           # a is still free
        async with client(OTHER) as elsewhere:
            other = await send(elsewhere, photo_body("and this?", [c_new]))        # another network has its own

    assert refused.status_code == 429
    detail = refused.json()["detail"]
    assert re.fullmatch(r"That's more photos than this network can send in an hour \(2\)\. "
                        r"You can send more in about \d+ minutes\.", detail), detail
    assert refused.json()["code"] == media.IMAGE_QUOTA and 1 <= int(refused.headers["retry-after"]) <= 3600
    assert again.status_code == 200 and other.status_code == 200
    assert len(lane_env.engine.chats) == 1 + 3 + 1 + 1 + 1, "the refused request never reached the engine"
    assert nothing_held(lane_env)


@pytest.mark.anyio
async def test_a_refused_photo_gives_the_phone_its_lane_and_its_one_answer_at_a_time_back(lane_env):
    lane_env.limits["image_input_per_hour"] = 1
    async with client() as c:
        await send(c, photo_body())
        refused = await send(c, photo_body())
        text = await send(c, phone_body("hello"))

    assert refused.status_code == 429 and text.status_code == 200 and b"engine-one" in text.content
    assert nothing_held(lane_env)


@pytest.mark.anyio
async def test_zero_photos_an_hour_refuses_every_photo_and_no_text(lane_env):
    lane_env.limits["image_input_per_hour"] = 0
    async with client() as c:
        refused = await send(c, photo_body())
        text = await send(c, phone_body("hello"))

    assert refused.status_code == 429
    assert refused.json() == {"detail": "This server does not take images.", "code": media.IMAGE_QUOTA}
    assert text.status_code == 200 and nothing_held(lane_env)


@pytest.mark.anyio
async def test_the_coding_chat_and_the_phone_chat_draw_on_one_budget_per_network(lane_env):
    """One network, one budget. A picture the coding chat has counted still counts once in a phone chat, whose lane has
    never read it; it is free there after that."""
    lane_env.limits["image_input_per_hour"] = 2
    shot = photo()
    request = SimpleNamespace(state=SimpleNamespace(dlne_tier="key"), client=SimpleNamespace(host=REMOTE[0]),
                              headers={})
    coordinator._spend_image_budget(request, media.validate_images([{"role": "user", "content": [shot]}]))
    async with client() as c:
        same = await send(c, photo_body("what is this?", [shot]))              # two of two
        again = await send(c, photo_body("and now?", [shot]))                  # the same chat: free
        different = await send(c, photo_body("and this?", [photo()]))

    assert same.status_code == 200 and again.status_code == 200 and different.status_code == 429


@pytest.mark.anyio
async def test_a_resent_photo_is_free_only_in_the_chat_that_sent_it_and_without_a_chat_it_counts_every_time(lane_env):
    """2.6.2 review: a repeat was free from any chat, or from none, while a lane that never read it encoded it again."""
    lane_env.limits["image_input_per_hour"] = 3
    shot = photo()
    async with client() as c:
        first = await send(c, photo_body("what is this?", [shot]))                         # one of three
        same_chat = await send(c, photo_body("and now?", [shot]))                          # free
        other_chat = await send(c, photo_body("what is this?", [shot], chat_id=CHAT_B))    # two of three
        no_chat = await send(c, photo_body("what is this?", [shot], chat_id=None))         # three of three
        no_chat_again = await send(c, photo_body("what is this?", [shot], chat_id=None))   # not free: refused
        still_free = await send(c, photo_body("and now?", [shot], chat_id=CHAT_B))

    assert [r.status_code for r in (first, same_chat, other_chat, no_chat, no_chat_again, still_free)] == [
        200, 200, 200, 200, 429, 200]
    assert nothing_held(lane_env)


@pytest.mark.anyio
async def test_every_photo_a_message_carries_counts_toward_four_times_the_budget(lane_env):
    """A client that keeps switching chats evicts the lanes' caches, so each of its messages is encoded again: repeats
    are free of the budget of distinct photos, not of this bound."""
    lane_env.limits["image_input_per_hour"] = 1
    shot = photo()
    async with client() as c:
        sent = [await send(c, photo_body("and now?", [shot])) for _ in range(4)]          # four parts of four
        over = await send(c, photo_body("and now?", [shot]))
        text = await send(c, phone_body("hello"))
        async with client(OTHER) as elsewhere:
            other = await send(elsewhere, photo_body("what is this?", [shot]))

    assert [r.status_code for r in sent] == [200] * 4
    assert over.status_code == 429 and over.json()["code"] == media.IMAGE_QUOTA
    assert re.fullmatch(r"Your messages have carried as many photos this hour as this network can send\. Start a new "
                        r"chat to go on without them, or wait about \d+ minutes\.", over.json()["detail"])
    assert 1 <= int(over.headers["retry-after"]) <= 3600
    assert text.status_code == 200 and other.status_code == 200 and nothing_held(lane_env)
    assert len(lane_env.engine.chats) == 4 + 1 + 1, "the refused message never reached the engine"


@pytest.mark.anyio
async def test_a_photo_the_budget_refuses_gives_back_the_parts_it_was_counted(lane_env):
    lane_env.limits["image_input_per_hour"] = 1
    shot = photo()
    async with client() as c:
        first = await send(c, photo_body("what is this?", [shot]))                 # one part of four
        refused = [await send(c, photo_body("and this?", [photo(), photo()])) for _ in range(3)]
        resent = [await send(c, photo_body("and now?", [shot])) for _ in range(3)]  # parts two to four

    assert first.status_code == 200 and [r.status_code for r in refused] == [429] * 3
    assert all("than this network can send in an hour (1)" in r.json()["detail"] for r in refused)
    assert [r.status_code for r in resent] == [200] * 3


@pytest.mark.anyio
@pytest.mark.parametrize("chunked", [False, True], ids=["content-length", "chunked"])
async def test_a_body_over_the_cap_is_refused_with_413_before_it_is_read_whole(lane_env, monkeypatch, chunked):
    """2.6.2 review: the whole body was read and parsed before anything else, with nothing to bound it."""
    monkeypatch.setattr(coordinator, "APP_CHAT_MAX_BYTES", 4096)
    raw = json.dumps(photo_body("x" * 8000, [])).encode()

    async def pieces():
        for start in range(0, len(raw), 1024):
            yield raw[start:start + 1024]

    async with client() as c:
        if chunked:
            refused = await c.post("/v1/app/chat/completions", content=pieces(),
                                   headers={"content-type": "application/json"})
        else:
            refused = await c.post("/v1/app/chat/completions", content=raw,
                                   headers={"content-type": "application/json"})
        fits = await send(c, phone_body("hello"))

    assert refused.status_code == 413
    assert refused.json()["detail"] == "That message is too large to send. Try fewer or smaller photos."
    assert fits.status_code == 200 and lane_env.engine.chats and nothing_held(lane_env)
    assert len(lane_env.engine.chats) == 1, "the refused body never reached an engine"


# ======================================================================================
# With no lane to look at it, a photo is told so and never goes to a volunteer
# ======================================================================================

def cpu_saw_no_photo(env):
    return all(isinstance(m["content"], str) for p in env.cpu.payloads for m in p["messages"]) and not any(
        "base64" in str(p) for p in env.cpu.payloads)


@pytest.mark.anyio
@pytest.mark.parametrize("how", ["down", "not_restarted", "swapping", "dead", "silent"])
async def test_with_the_lanes_unable_to_answer_a_photo_is_told_dlne_is_restarting_while_a_text_goes_to_the_cpu_pool(
        lane_env, monkeypatch, how):
    if how == "down":
        lane_env.manager.running = False
    elif how == "not_restarted":
        lane_env.manager.phone_slots = 0
    elif how == "swapping":
        monkeypatch.setattr(coordinator, "is_swapping_models", True)
    elif how == "dead":
        lane_env.manager.alive = False
    else:
        async def probe(port):
            return False

        monkeypatch.setattr(coordinator, "_probe_engine_port", probe)
    async with client() as c:
        shot = await send(c, photo_body())
        text = await send(c, phone_body("hello"))                       # the control: nothing about text changed

    assert shot.status_code == 200 and RESTARTING.encode() in shot.content and b"cpu-one" not in shot.content
    assert shot.content.endswith(b"data: [DONE]\n\n") and PHOTO_NOTICE.encode() not in shot.content
    assert b"cpu-one" in text.content and len(lane_env.cpu.payloads) == 1, "only the text request reached a member"
    assert cpu_saw_no_photo(lane_env) and lane_env.engine.chats == []
    assert nothing_held(lane_env)


@pytest.mark.anyio
async def test_with_no_phone_lanes_at_all_a_photo_is_told_it_cannot_be_looked_at_and_a_text_still_goes_to_the_cpu_pool(
        lane_env):
    lane_env.limits["engine_phone_slots"] = 0
    lane_env.manager.phone_slots = 0
    async with client() as c:
        shot = await send(c, photo_body())
        text = await send(c, phone_body("hello"))
        status = (await c.get("/v1/cluster/status")).json()

    assert shot.status_code == 200 and PHOTO_NOTICE.encode() in shot.content and RESTARTING.encode() not in shot.content
    assert b"No machine" not in shot.content and shot.content.endswith(b"data: [DONE]\n\n")
    assert b"cpu-one" in text.content and cpu_saw_no_photo(lane_env) and len(lane_env.cpu.payloads) == 1
    assert "app_vision" not in status["capabilities"], "no lanes: the status is what 2.6.1 sent"
    assert nothing_held(lane_env)


@pytest.mark.anyio
async def test_an_engine_that_fails_before_answering_does_not_hand_the_photo_to_the_ready_cpu_member(lane_env):
    lane_env.engine.fails = True
    async with client() as c:
        shot = await send(c, photo_body())
        text = await send(c, phone_body("hello"))

    assert RESTARTING.encode() in shot.content and b"cpu-one" not in shot.content
    assert b"cpu-one" in text.content, "a text request in the same state is answered by the CPU pool as before"
    assert len(lane_env.cpu.payloads) == 1 and cpu_saw_no_photo(lane_env)
    assert nothing_held(lane_env)


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["restart", "port", "lanes", "swap"])
async def test_an_engine_that_changed_while_the_photo_waited_for_a_lane_is_not_used_nor_is_the_cpu_pool(lane_env, change):
    held = [coordinator.phone_gate.try_acquire() for _ in range(PHONE_SLOTS)]
    assert all(held)
    async with client() as c:
        task = asyncio.create_task(send(c, photo_body()))
        assert await until(lambda: coordinator.phone_gate.stats()["waiting"] == 1)
        if change == "restart":
            lane_env.manager.process = SimpleNamespace(poll=lambda: None)
        elif change == "port":
            lane_env.manager.engine = SimpleNamespace(port=lane_env.engine.port + 1)
        elif change == "lanes":
            lane_env.manager.phone_slots = PHONE_SLOTS + 1
        else:
            coordinator.is_swapping_models = True
        for _ in range(PHONE_SLOTS):
            coordinator.phone_gate.release()
        resp = await task
        coordinator.is_swapping_models = False

    assert resp.status_code == 200 and RESTARTING.encode() in resp.content
    assert lane_env.engine.chats == [] and lane_env.cpu.payloads == []
    assert nothing_held(lane_env)


@pytest.mark.anyio
async def test_an_engine_that_stayed_the_same_while_the_photo_waited_answers_it(lane_env):
    held = [coordinator.phone_gate.try_acquire() for _ in range(PHONE_SLOTS)]
    async with client() as c:
        task = asyncio.create_task(send(c, photo_body()))
        assert await until(lambda: coordinator.phone_gate.stats()["waiting"] == 1)
        for _ in range(PHONE_SLOTS):
            coordinator.phone_gate.release()
        resp = await task

    assert resp.status_code == 200 and b"engine-one" in resp.content and lane_env.cpu.payloads == []
    assert nothing_held(lane_env) and all(held)


@pytest.mark.anyio
async def test_a_photo_is_not_answered_while_every_phone_lane_is_busy_for_good_the_phone_gets_the_usual_429(
        lane_env, monkeypatch):
    monkeypatch.setattr(coordinator, "PHONE_LANE_WAIT_SECONDS", 0.2)
    held = [coordinator.phone_gate.try_acquire() for _ in range(PHONE_SLOTS)]
    async with client() as c:
        resp = await send(c, photo_body())
    for _ in held:
        coordinator.phone_gate.release()

    assert resp.status_code == 429 and resp.headers["retry-after"] == "5" and lane_env.cpu.payloads == []
    assert nothing_held(lane_env)


# ======================================================================================
# capabilities.app_vision
# ======================================================================================

async def capabilities():
    async with client(("127.0.0.1", 5000)) as admin:
        return (await admin.get("/v1/cluster/status")).json()["capabilities"]


@pytest.mark.anyio
async def test_the_status_says_the_phone_chat_takes_photos_while_the_lanes_and_a_projector_are_set_up(lane_env):
    assert (await capabilities())["app_vision"] is True

    lane_env.manager.vision_configured = lambda: False
    assert (await capabilities())["app_vision"] is False


@pytest.mark.anyio
async def test_the_status_does_not_flicker_while_the_engine_restarts(lane_env, monkeypatch):
    lane_env.manager.running = False
    assert (await capabilities())["app_vision"] is True

    lane_env.manager.running = True
    monkeypatch.setattr(coordinator, "is_swapping_models", True)
    assert (await capabilities())["app_vision"] is True

    monkeypatch.setattr(coordinator, "is_swapping_models", False)
    lane_env.manager.phone_slots = 0                                   # started without lanes: lanes are still configured
    assert (await capabilities())["app_vision"] is True


@pytest.mark.anyio
async def test_the_status_leaves_app_vision_out_with_no_phone_lanes_and_still_answers_when_the_manager_cannot(lane_env):
    lane_env.limits["engine_phone_slots"] = 0
    caps = await capabilities()
    assert "app_vision" not in caps and set(caps) == {"vision"}

    lane_env.limits["engine_phone_slots"] = PHONE_SLOTS

    def broken():
        raise RuntimeError("manager")

    lane_env.manager.vision_configured = broken
    assert (await capabilities())["app_vision"] is False


# ======================================================================================
# Nothing of a photo is logged or kept
# ======================================================================================

@pytest.mark.anyio
async def test_nothing_of_a_photo_is_logged_only_its_size_and_a_short_fingerprint(lane_env, logged, caplog, capsys):
    caplog.set_level(0)
    raw = make_png(77, 33)
    url = "data:image/png;base64," + b64(raw)
    sha = hashlib.sha256(raw).hexdigest()
    async with client() as c:
        answered = await send(c, photo_body("what is this?", [photo(url)]))
        lane_env.engine.fails = True
        failed = await send(c, photo_body("and now?", [photo(url)]))
        lane_env.engine.fails = False
        bad = await send(c, photo_body("this?", [photo("data:image/png;base64," + b64(b"GIF89a" + bytes(40)))]))

    everything = "\n".join(logged) + caplog.text + capsys.readouterr().out
    assert answered.status_code == 200 and b"engine-one" in answered.content
    assert RESTARTING.encode() in failed.content and bad.status_code == 400
    assert f"[photo 77x33 sha256:{sha[:8]}]" in "\n".join(logged)
    assert sha[:9] not in everything, "a fingerprint is eight characters, not more"
    for needle in (url[40:140], b64(raw)[10:60], b64(b"GIF89a")):
        assert needle not in everything
        assert needle.encode() not in answered.content + failed.content + bad.content


@pytest.mark.anyio
async def test_a_refused_photo_is_not_logged_at_all(lane_env, logged):
    async with client() as c:
        await send(c, photo_body("this?", [photo("https://example.com/cat.png")]))

    assert not any("photo" in line for line in logged)
