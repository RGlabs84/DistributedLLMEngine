"""
Images in coding chat requests (2.6), through the coordinator: list content in the context trimming, the 400s
(`detail` as every client already reads it, plus a machine-readable `code`), the vision flag in
/v1/cluster/status, and the unchanged path for plain text.

The handler is driven the way tests/test_coordinator_counter.py drives it (a stub request, a stub engine client), so
what the engine would have been sent is read straight from the recorded payload.
"""

import asyncio
import base64
import itertools
import json
import types

import pytest
from httpx import ASGITransport, AsyncClient

import engine.coordinator as coordinator
from engine import media
from engine.priority import DistinctWindow
from tests.media_samples import png as make_png
from tests.test_coordinator_counter import (  # noqa: F401 - counter_env is a fixture
    FakeAsyncClient, FakeRequest, FakeStreamContext, FakeStreamResponse, counter_env, drain, running_engine_entry,
    set_start_result,
)

MODEL = "phi3-mini.gguf"


def png(width=64, height=48):
    return make_png(width, height)


def data_url(raw=None):
    return "data:image/png;base64," + base64.b64encode(raw or png()).decode()


def image_part(url=None):
    return {"type": "image_url", "image_url": {"url": url or data_url()}}


_new_image = itertools.count(1)


def fresh_image_part():
    """An image no other call made. The hourly budget counts DISTINCT images (2.6.2: the same photo sent again is free),
    so the tests that spend it send pictures of a different size each time."""
    return image_part(data_url(png(64 + next(_new_image), 48)))


def asking(*messages):
    return FakeRequest({"model": MODEL, "stream": True, "messages": list(messages)})


class Props:
    """What GET /props answered."""

    def __init__(self, status_code=200, vision=True):
        self.status_code = status_code
        self._vision = vision

    def json(self):
        return {"modalities": {"vision": self._vision, "audio": False}}


class RecordingClient(FakeAsyncClient):
    """The stub engine client: remembers every payload it was sent and every /props it was asked."""

    sent = []
    props_asked = []
    props = Props()

    async def get(self, url, **kwargs):
        self.props_asked.append(url)
        if isinstance(self.props, Exception):
            raise self.props
        return self.props

    def stream(self, method, url, **kwargs):
        self.sent.append(kwargs.get("json"))
        return super().stream(method, url, **kwargs)


@pytest.fixture
def engine(counter_env, monkeypatch):
    monkeypatch.setattr(coordinator, "VISION_ASK_RETRY_SECONDS", 0.0)
    monkeypatch.setattr(coordinator, "image_input_window", DistinctWindow())
    """A running engine; ``engine.entry`` is what the manager says about it, ``engine.sent`` what it was sent."""
    RecordingClient.sent, RecordingClient.props_asked, RecordingClient.props = [], [], Props()
    monkeypatch.setattr(coordinator.httpx, "AsyncClient", RecordingClient)
    monkeypatch.setattr(coordinator, "_engine_vision_seen", (None, False))
    entry = running_engine_entry(port=8081, safe_context=65536, dynamic_slots=2)
    entry.update(mmproj_path="/models/mmproj-F16.gguf", started_at=1.0)
    monkeypatch.setattr(coordinator.manager, "running_models", {MODEL: entry})
    starts = []

    async def start(model_path, *args, **kwargs):
        starts.append(model_path)
        return (True, 8081, "Started")

    monkeypatch.setattr(coordinator.manager, "astart_model", start)
    return types.SimpleNamespace(entry=entry, sent=RecordingClient.sent, starts=starts, client=RecordingClient)


def run(request):
    async def scenario():
        response = await coordinator.chat_completions(request)
        await drain(response)

    asyncio.run(scenario())


# --- the request reaches the engine whole ---------------------------------------------------------------

def test_an_image_message_reaches_the_engine_with_its_image_and_its_text(engine):
    message = {"role": "user", "content": [{"type": "text", "text": "what is this?"}, image_part()]}

    run(asking({"role": "system", "content": "be brief"}, message))

    (sent,) = engine.sent
    assert sent["messages"][-1] == message
    assert sent["messages"][0] == {"role": "system", "content": "be brief"}


def test_a_plain_text_request_is_sent_as_it_was_and_the_engine_is_not_asked_about_vision(engine):
    messages = [{"role": "user", "content": "one"}, {"role": "assistant", "content": "two"},
                {"role": "user", "content": "three"}]

    run(asking(*[dict(m) for m in messages]))

    assert engine.sent[0]["messages"] == messages
    assert engine.client.props_asked == []


def test_the_engine_is_asked_once_per_start_not_per_request(engine):
    for _ in range(3):
        run(asking({"role": "user", "content": [image_part()]}))

    assert len(engine.client.props_asked) == 1 and engine.client.props_asked[0].endswith(":8081/props")
    assert len(engine.sent) == 3

    engine.entry["started_at"] = 2.0                 # the engine restarted
    run(asking({"role": "user", "content": [image_part()]}))
    assert len(engine.client.props_asked) == 2


# --- the 400s ------------------------------------------------------------------------------------------

def refused(request):
    with pytest.raises(coordinator.ImageRefused) as excinfo:
        run(request)
    return excinfo.value


def test_an_image_to_an_engine_without_a_projector_is_refused_and_nothing_is_held(engine):
    engine.entry["mmproj_path"] = ""
    coordinator.active_inference_requests = 0

    error = refused(asking({"role": "user", "content": [image_part()]}))

    assert (error.status_code, error.code) == (400, media.IMAGE_INPUT_UNAVAILABLE)
    assert engine.client.props_asked == [] and engine.sent == []
    assert coordinator.active_inference_requests == 0


@pytest.mark.parametrize("props", [Props(vision=False), Props(status_code=503), RuntimeError("down")])
def test_an_engine_that_does_not_say_it_sees_is_taken_at_its_word(engine, props):
    engine.client.props = props

    assert refused(asking({"role": "user", "content": [image_part()]})).code == media.IMAGE_INPUT_UNAVAILABLE


def test_a_refusal_while_the_engine_loads_is_not_remembered(engine):
    engine.client.props = Props(status_code=503)
    assert refused(asking({"role": "user", "content": [image_part()]})).code == media.IMAGE_INPUT_UNAVAILABLE

    engine.client.props = Props()
    run(asking({"role": "user", "content": [image_part()]}))

    assert len(engine.sent) == 1


@pytest.mark.parametrize("url, code", [
    ("https://example.com/a.png", media.REMOTE_IMAGE_URL),
    ("file:///etc/passwd", media.REMOTE_IMAGE_URL),
    ("data:image/gif;base64," + base64.b64encode(b"GIF89a" + bytes(20)).decode(), media.UNSUPPORTED_IMAGE_TYPE),
    (data_url(png(5000, 10)), media.IMAGE_TOO_LARGE),
])
def test_a_bad_image_is_refused_before_the_engine_is_touched(engine, url, code):
    coordinator.active_inference_requests = 0

    error = refused(asking({"role": "user", "content": [image_part(url)]}))

    assert error.code == code
    assert engine.starts == [] and engine.sent == [] and engine.client.props_asked == []
    assert coordinator.active_inference_requests == 0


def test_too_many_images_are_refused(engine):
    parts = [image_part() for _ in range(media.MAX_IMAGES + 1)]

    assert refused(asking({"role": "user", "content": parts})).code == media.TOO_MANY_IMAGES


@pytest.mark.parametrize("role", ["user", "system", "assistant"])
@pytest.mark.parametrize("part", [
    {"type": "video_url", "video_url": {"url": "http://192.168.1.1/clip.mp4"}},
    {"type": "input_video", "input_video": {"url": "http://192.168.1.1/clip.mp4"}},
    {"type": "input_audio", "input_audio": {"data": "AAAA", "format": "wav"}},
    {"text": "no type"},
])
def test_a_part_llama_server_would_fetch_is_refused_before_anything_starts(engine, role, part):
    """llama-server downloads http(s) URLs in video and audio parts itself: they never reach it, vision or not."""
    for mmproj in ("/models/mmproj-F16.gguf", ""):
        engine.entry["mmproj_path"] = mmproj
        error = refused(asking({"role": "user", "content": "hi"}, {"role": role, "content": [part]}))

        assert error.code == media.UNSUPPORTED_CONTENT_TYPE
        assert engine.starts == [] and engine.sent == [] and engine.client.props_asked == []


def test_a_refused_streamed_request_is_refused_at_once_not_after_the_queue(engine, monkeypatch):
    """The extension's streamed chats are queued for a slot; a refusal must not wait there, nor lose its code."""
    class NoGate:
        def __getattr__(self, name):
            raise AssertionError(f"the coding gate was used ({name}) for a request that is refused")

    monkeypatch.setattr(coordinator, "coding_gate", NoGate())
    request = asking({"role": "user", "content": [image_part("https://example.com/a.png")]})
    request.headers = {"x-dlne-session": "abc"}

    error = refused(request)

    assert error.code == media.REMOTE_IMAGE_URL


def test_the_images_are_decoded_once_however_the_request_gets_to_the_handler(engine, monkeypatch):
    calls = []
    real = media.validate_images
    monkeypatch.setattr(media, "validate_images", lambda messages: calls.append(1) or real(messages))

    run(asking({"role": "user", "content": [image_part()]}))

    assert calls == [1]


@pytest.mark.anyio
async def test_the_400_for_a_video_part_carries_its_code(engine):
    body = {"model": MODEL, "messages": [{"role": "user", "content": [
        {"type": "video_url", "video_url": {"url": "http://192.168.1.1/clip.mp4"}}]}]}
    async with _client() as client:
        for headers in ({}, {"X-DLNE-Session": "abc"}):
            response = await client.post("/v1/chat/completions", json={**body, "stream": True}, headers=headers)

            assert response.status_code == 400
            assert response.json()["code"] == "unsupported_content_type" and "192.168" not in response.text


def _client():
    transport = ASGITransport(app=coordinator.app, client=("127.0.0.1", 5000))
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.anyio
async def test_the_400_body_is_detail_like_every_other_error_plus_a_code(engine):
    body = {"model": MODEL, "messages": [{"role": "user", "content": [image_part("https://example.com/a.png")]}]}
    async with _client() as client:
        response = await client.post("/v1/chat/completions", json=body)

    assert response.status_code == 400
    assert response.json() == {"detail": "Images must be sent inline as data: URLs; links are not fetched.",
                               "code": "remote_image_url"}
    assert "example.com" not in response.text


@pytest.mark.anyio
async def test_other_400s_keep_their_shape(engine):
    async with _client() as client:
        response = await client.post("/v1/chat/completions", content="{", headers={"Content-Type": "application/json"})

    assert response.status_code == 400 and response.json() == {"detail": "Request body is not valid JSON."}


# --- trimming a conversation that holds images -----------------------------------------------------------

def test_an_image_costs_context_and_an_older_turn_that_no_longer_fits_is_summarised_not_sent(engine, monkeypatch):
    real = coordinator.get_worker_limits
    monkeypatch.setattr(coordinator, "get_worker_limits", lambda: {**real(), "engine_image_max_tokens": 8000})
    engine.entry.update(safe_context=8192, dynamic_slots=1)
    old = {"role": "user", "content": [{"type": "text", "text": "this diagram"}, image_part()]}
    newest = {"role": "user", "content": "and now?"}

    run(asking(old, {"role": "assistant", "content": "it shows a cycle"}, newest))

    summary_request, final_request = engine.sent
    prompt = summary_request["messages"][0]["content"]
    assert "USER: this diagram [an image]" in prompt and "data:image" not in prompt
    assert media.image_count(final_request["messages"][0]["content"]) == 0
    assert all(not media.has_images([m]) for m in final_request["messages"])


def test_a_newest_message_too_long_for_its_slot_is_cut_in_its_text_and_keeps_its_image(engine, monkeypatch):
    real = coordinator.get_worker_limits
    monkeypatch.setattr(coordinator, "get_worker_limits", lambda: {**real(), "engine_image_max_tokens": 1000})
    engine.entry.update(safe_context=4096, dynamic_slots=1)
    long_text = "x" * 60000
    newest = {"role": "user", "content": [{"type": "text", "text": long_text}, image_part()]}

    run(asking(newest))

    (sent,) = engine.sent
    text_part, image = sent["messages"][-1]["content"]
    assert text_part["text"].startswith("...[Text omitted for memory]...\n") and image == image_part()
    assert len(text_part["text"]) < 4096 * 3 - 1000 * 3 + 100          # the image's share of the slot is kept free


def test_plain_text_is_trimmed_exactly_as_it_was(engine):
    engine.entry.update(safe_context=4096, dynamic_slots=1)
    newest = {"role": "user", "content": "y" * 60000}

    run(asking(newest))

    (sent,) = engine.sent
    assert sent["messages"][-1]["content"] == "...[Text omitted for memory]...\n" + "y" * ((4096 - 512) * 3)


# --- the vision flag in the status ------------------------------------------------------------------------

@pytest.mark.anyio
async def test_the_status_says_whether_the_engine_reads_images(engine, monkeypatch):
    async def snapshot(_name):
        return dict(engine.entry)

    monkeypatch.setattr(coordinator.manager, "asnapshot_model", snapshot)
    monkeypatch.setattr(coordinator.manager, "get_active_model_names", lambda: [MODEL])
    async with _client() as client:
        assert (await client.get("/v1/cluster/status")).json()["capabilities"] == {"vision": True}

        engine.entry["mmproj_path"] = ""
        assert (await client.get("/v1/cluster/status")).json()["capabilities"] == {"vision": False}


@pytest.mark.anyio
async def test_the_status_is_false_without_an_engine_or_while_it_loads(engine, monkeypatch):
    assert await coordinator._engine_vision([]) is False

    async def snapshot(_name):
        return dict(engine.entry)

    monkeypatch.setattr(coordinator.manager, "asnapshot_model", snapshot)
    engine.client.props = Props(status_code=503)
    assert await coordinator._engine_vision([MODEL]) is False

    async def broken(_name):
        raise RuntimeError("manager")

    monkeypatch.setattr(coordinator.manager, "asnapshot_model", broken)
    assert await coordinator._engine_vision([MODEL]) is False


@pytest.mark.anyio
async def test_with_vision_off_the_status_asks_the_engine_nothing(engine, monkeypatch):
    engine.entry["mmproj_path"] = ""

    async def snapshot(_name):
        return dict(engine.entry)

    monkeypatch.setattr(coordinator.manager, "asnapshot_model", snapshot)

    assert await coordinator._engine_vision([MODEL]) is False
    assert engine.client.props_asked == []


# --- 2.6.1: the image budget, the early check, a busy engine, the queued path, the published limits ------------

def as_key_holder(request, host="198.51.100.9"):
    """A request from outside the coordinator's own machine, whose images are counted."""
    request.state.dlne_tier = "key"
    request.client = types.SimpleNamespace(host=host)
    return request


def four_images():
    return {"role": "user", "content": [fresh_image_part() for _ in range(4)]}


def test_images_are_counted_not_requests_and_the_next_over_the_budget_is_a_429_with_retry_after(engine):
    for _ in range(7):                                   # 28 of 30 images
        run(as_key_holder(asking(four_images())))
    engine.sent.clear()

    error = refused(as_key_holder(asking(four_images())))             # 32 would be over

    assert (error.status_code, error.code) == (429, media.IMAGE_QUOTA)
    assert 1 <= int(error.headers["Retry-After"]) <= 3600 and engine.sent == []
    run(as_key_holder(asking({"role": "user", "content": [fresh_image_part(), fresh_image_part()]})))   # the last two fit
    assert len(engine.sent) == 1
    assert refused(as_key_holder(asking({"role": "user", "content": [fresh_image_part()]}))).code == media.IMAGE_QUOTA


def test_the_same_image_sent_again_costs_nothing_and_each_new_one_costs_one(engine):
    """2.6.2: the budget counts each DISTINCT image (SHA-256) once per hour, so a follow-up that resends a picture is free."""
    for _ in range(7):
        run(as_key_holder(asking(four_images())))                                       # 28 distinct images
    photo = image_part()
    follow_up = [{"role": "user", "content": [{"type": "text", "text": "what is it?"}, photo]},
                 {"role": "assistant", "content": "a car"}, {"role": "user", "content": "what colour?"}]
    run(as_key_holder(asking(*follow_up)))                                              # one new image: 29
    for _ in range(5):
        run(as_key_holder(asking(*follow_up)))                                          # the same one again and again
    run(as_key_holder(asking({"role": "user", "content": [photo, photo, photo]})))      # and copies of it in one request
    engine.sent.clear()

    run(as_key_holder(asking({"role": "user", "content": [fresh_image_part()]})))        # the 30th
    assert refused(as_key_holder(asking({"role": "user", "content": [fresh_image_part()]}))).code == media.IMAGE_QUOTA
    run(as_key_holder(asking(*follow_up)))                                              # still free over the budget
    assert len(engine.sent) == 2


def test_the_budget_is_per_network_and_text_costs_nothing(engine):
    for _ in range(7):
        run(as_key_holder(asking(four_images())))

    run(as_key_holder(asking(four_images()), host="198.51.100.77"))                       # another network
    run(as_key_holder(asking({"role": "user", "content": "just words"})))                 # no images
    assert refused(as_key_holder(asking(four_images()))).code == media.IMAGE_QUOTA        # the first one is still spent


def test_a_whole_ipv6_block_is_one_network(engine):
    for _ in range(7):
        run(as_key_holder(asking(four_images()), host="2001:db8:1:2::1"))

    assert refused(as_key_holder(asking(four_images()), host="2001:db8:1:2:ffff::9")).code == media.IMAGE_QUOTA


def test_a_refused_request_spends_no_budget(engine):
    bad = {"role": "user", "content": [image_part(), image_part("https://example.com/a.png")]}
    for _ in range(40):
        assert refused(as_key_holder(asking(bad))).code == media.REMOTE_IMAGE_URL
    engine.entry["mmproj_path"] = ""
    for _ in range(40):
        assert refused(as_key_holder(asking(four_images()))).code == media.IMAGE_INPUT_UNAVAILABLE
    engine.entry["mmproj_path"] = "/models/mmproj-F16.gguf"

    run(as_key_holder(asking(four_images())))                                            # the budget is untouched


def test_the_host_is_not_counted_but_zero_refuses_everyone(engine, monkeypatch):
    for _ in range(12):
        run(asking(four_images()))                                                        # the host: 48 images

    real = coordinator.get_worker_limits
    monkeypatch.setattr(coordinator, "get_worker_limits", lambda: {**real(), "image_input_per_hour": 0})
    for request in (asking(four_images()), as_key_holder(asking(four_images()))):
        error = refused(request)
        assert (error.status_code, error.code) == (429, media.IMAGE_QUOTA)


@pytest.mark.anyio
async def test_the_429_body_is_detail_and_code_with_retry_after(engine, monkeypatch):
    real = coordinator.get_worker_limits
    monkeypatch.setattr(coordinator, "get_worker_limits", lambda: {**real(), "image_input_per_hour": 0})
    monkeypatch.setattr(coordinator.manager, "asnapshot_model", lambda name: _snapshot(engine))
    monkeypatch.setattr(coordinator.manager, "get_active_model_names", lambda: [MODEL])
    body = {"model": MODEL, "messages": [{"role": "user", "content": [image_part()]}]}

    async with _client() as client:
        response = await client.post("/v1/chat/completions", json=body)

    assert response.status_code == 429
    assert response.json() == {"detail": "This server does not take images.", "code": "image_quota"}


@pytest.mark.anyio
async def test_the_hourly_429_has_a_retry_after_header(engine, monkeypatch):
    monkeypatch.setattr(coordinator, "image_input_window", DistinctWindow())
    real = coordinator.get_worker_limits
    monkeypatch.setattr(coordinator, "get_worker_limits", lambda: {**real(), "image_input_per_hour": 1})
    monkeypatch.setattr(coordinator.manager, "asnapshot_model", lambda name: _snapshot(engine))
    monkeypatch.setattr(coordinator.manager, "get_active_model_names", lambda: [MODEL])
    bodies = [{"model": MODEL, "messages": [{"role": "user", "content": [fresh_image_part()]}]} for _ in range(2)]
    headers = {"X-Forwarded-For": "198.51.100.5"}
    transport = ASGITransport(app=coordinator.app, client=("198.51.100.5", 5000))
    monkeypatch.setattr(coordinator, "is_local_ip", lambda ip: False)
    monkeypatch.setattr(coordinator, "check_contrib_clause", lambda request: None)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post("/v1/chat/completions", json=bodies[0], headers=headers)
        second = await client.post("/v1/chat/completions", json=bodies[1], headers=headers)

    assert second.status_code == 429 and second.json()["code"] == "image_quota"
    assert 1 <= int(second.headers["Retry-After"]) <= 3600 and first.status_code != 429


async def _snapshot(engine):
    return dict(engine.entry)


def test_an_engine_that_is_busy_for_a_moment_is_asked_again_before_it_is_told_it_cannot_see(engine, monkeypatch):
    answers = [Props(status_code=503), RuntimeError("busy"), Props()]
    asked = []

    async def get(self, url, **kwargs):
        asked.append(url)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(engine.client, "get", get)

    run(asking({"role": "user", "content": [image_part()]}))

    assert len(asked) == 3 and len(engine.sent) == 1


def test_a_failed_ask_after_an_answer_keeps_the_answer_for_that_start(engine):
    run(asking({"role": "user", "content": [image_part()]}))
    engine.client.props = Props(status_code=503)

    run(asking({"role": "user", "content": [image_part()]}))
    run(asking({"role": "user", "content": [image_part()]}))

    assert len(engine.sent) == 3 and len(engine.client.props_asked) == 1


@pytest.mark.anyio
async def test_the_status_keeps_saying_yes_through_a_failed_ask_in_the_same_start(engine, monkeypatch):
    monkeypatch.setattr(coordinator.manager, "asnapshot_model", lambda name: _snapshot(engine))
    assert await coordinator._engine_vision([MODEL]) is True
    engine.client.props = Props(status_code=503)

    assert await coordinator._engine_vision([MODEL]) is True

    engine.entry["started_at"] = 2.0                       # a new start: nothing is known about it yet
    assert await coordinator._engine_vision([MODEL]) is False


def test_an_image_to_a_running_engine_without_vision_is_refused_before_the_queue_and_the_engine_start(engine, monkeypatch):
    engine.client.props = Props(vision=False)

    class NoGate:
        def __getattr__(self, name):
            raise AssertionError(f"the coding gate was used ({name})")

    monkeypatch.setattr(coordinator, "coding_gate", NoGate())
    request = asking({"role": "user", "content": [image_part()]})
    request.headers = {"x-dlne-session": "abc"}

    error = refused(request)

    assert error.code == media.IMAGE_INPUT_UNAVAILABLE and engine.starts == [] and engine.sent == []


def test_an_image_while_no_engine_runs_starts_nothing_when_no_engine_could_read_it(engine, monkeypatch):
    monkeypatch.setattr(coordinator.manager, "running_models", {})
    monkeypatch.setattr(coordinator.manager, "vision_configured", lambda: False)

    error = refused(asking({"role": "user", "content": [image_part()]}))

    assert error.code == media.IMAGE_INPUT_UNAVAILABLE and engine.starts == [] and engine.sent == []


def test_an_image_while_no_engine_runs_starts_one_when_it_would_have_a_projector(engine, monkeypatch):
    monkeypatch.setattr(coordinator.manager, "running_models", {})
    monkeypatch.setattr(coordinator.manager, "vision_configured", lambda: True)

    asyncio.run(coordinator._refuse_images_early(asking()))             # nothing to ask yet, and nothing wrong


@pytest.mark.anyio
async def test_a_queued_request_refused_after_its_wait_carries_the_code_in_its_stream(engine, monkeypatch):
    async def refuse(request, slot_held=False):
        raise coordinator.ImageRefused(media.IMAGE_INPUT_UNAVAILABLE, "This server's coding engine cannot read images.")

    class Gate:
        def stats(self):
            return {"slots": 1, "in_use": 0, "waiting": 0}

        def position(self, ticket):
            return 1

        async def acquire(self, priority, slots=None, ticket=None):
            return None

        def release(self):
            pass

    monkeypatch.setattr(coordinator, "_chat_completions_impl", refuse)
    monkeypatch.setattr(coordinator, "coding_gate", Gate())
    queued = coordinator._QueuedChat(asking(), {}, {})

    frames = [frame async for frame in queued.stream()]
    text = b"".join(frames).decode()

    assert "[System Alert] This server's coding engine cannot read images." in text
    assert '"dlne_error": {"code": "image_input_unavailable"}' in text


def test_a_notice_without_a_code_is_what_it_was():
    plain = coordinator._sse_notice_bytes("hello").decode()

    assert "dlne_error" not in plain and plain.endswith("data: [DONE]\n\n")


@pytest.mark.anyio
async def test_the_status_publishes_the_image_limits_from_the_constants_the_validator_uses(engine, monkeypatch):
    monkeypatch.setattr(coordinator.manager, "asnapshot_model", lambda name: _snapshot(engine))
    monkeypatch.setattr(coordinator.manager, "get_active_model_names", lambda: [MODEL])
    real = coordinator.get_worker_limits
    monkeypatch.setattr(coordinator, "get_worker_limits", lambda: {**real(), "image_input_per_hour": 45})
    async with _client() as client:
        policy = (await client.get("/v1/cluster/status")).json()["policy"]

    assert policy["image_input"] == {
        "formats": ["png", "jpeg"], "max_images": media.MAX_IMAGES, "max_bytes": media.MAX_IMAGE_BYTES,
        "max_side": media.MAX_SIDE, "max_pixels": media.MAX_PIXELS, "per_hour": 45}
    assert policy["image_input"]["max_images"] == 4 and policy["image_input"]["max_bytes"] == 4 * 1024 * 1024
    assert policy["image_input"]["max_side"] == 4096 and policy["image_input"]["max_pixels"] == 4096 * 4096

    engine.entry["mmproj_path"] = ""
    async with _client() as client:
        assert (await client.get("/v1/cluster/status")).json()["policy"]["image_input"] is None


def test_the_published_formats_are_the_ones_the_validator_returns():
    from tests.media_samples import jpeg
    seen = {info.format for info in media.validate_images(
        [{"role": "user", "content": [image_part(), image_part("data:image/jpeg;base64," + base64.b64encode(jpeg(8, 8)).decode())]}])}

    assert seen == set(media.public_limits(30)["formats"])


# --- a client never chooses the engine slot ---------------------------------------------------------------

def test_a_clients_id_slot_never_reaches_the_engine_with_or_without_phone_lanes(engine):
    for phone_slots in (0, 2):
        engine.entry["phone_slots"] = phone_slots
        engine.sent.clear()
        run(asking({"role": "user", "content": "hi"}))
        run(FakeRequest({"model": MODEL, "stream": True, "id_slot": 7, "messages": [{"role": "user", "content": "hi"}]}))

        assert all(sent.get("id_slot") != 7 for sent in engine.sent)
        if phone_slots == 0:
            assert all("id_slot" not in sent for sent in engine.sent)       # the body reaches the engine as it always did
