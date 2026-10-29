"""
Keep-alives on the coding chat's stream (2.6.2).

While the engine reads a long prompt (tens of thousands of tokens at a few hundred a second) it says nothing for a
minute or more, and a client that ends a stream after 120 s of silence (the extension's watchdog) takes the wait for a
dead engine and sends its step again. So every STREAM_KEEPALIVE_SECONDS without a byte from the engine, a streamed
coding answer sends its client an SSE comment line, ``: keep-alive``. These tests drive the chat handler with a fake
engine that pauses (the interval is shortened to a few hundredths of a second) and check that comments are sent while
the engine is silent and not once it streams, that the answer arrives whole and unchanged, that a comment never lands
inside an event, that a request that did not ask for a stream gets none, and that a client that leaves mid-wait gives
its slot back and stops the engine read. The phone path and the members' sealed streams do not go through this code.
"""

import asyncio
import gc
import types
from collections import deque

import httpx
import pytest
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

import engine.coordinator as coordinator
from engine.priority import CodingGate, coding_gate

KEEPALIVE = b": keep-alive\n\n"
FRAME_1 = b'data: {"choices":[{"delta":{"content":"hel"}}]}\n\n'
FRAME_2 = b'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n'
DONE = b"data: [DONE]\n\n"
ANSWER = [FRAME_1, FRAME_2, DONE]
STREAM_BODY = {"model": "phi3-mini", "stream": True, "messages": [{"role": "user", "content": "hello"}]}


# ======================================================================================
# Test doubles
# ======================================================================================

class FakeRequest:
    """A stand-in for ``starlette.requests.Request``: a JSON body, no headers, a host-tier caller."""

    def __init__(self, body):
        self._body = body
        self.headers = {}
        self.client = types.SimpleNamespace(host="203.0.113.7")
        self.state = types.SimpleNamespace(dlne_tier="host")

    async def json(self):
        return self._body


class FakeEngineProcess:
    returncode = None

    def poll(self):
        return None


class EngineLog:
    """What happened to the fake engine's side of the stream."""

    def __init__(self):
        self.cancelled = False       # the engine read was cancelled under the proxy
        self.finished = False        # the engine's byte stream ended or was torn down
        self.closed = False          # the ``async with client.stream(...)`` block was left
        self.opened = 0


class ScriptedResponse:
    """An httpx streamed response whose body follows a script: a number pauses that many seconds, bytes are a chunk,
    an exception is raised."""

    def __init__(self, script, log):
        self.status_code = 200
        self._script = script
        self._log = log

    async def aiter_bytes(self):
        try:
            for step in self._script:
                if isinstance(step, BaseException):
                    raise step
                if isinstance(step, (int, float)):
                    await asyncio.sleep(step)
                else:
                    yield step
        except asyncio.CancelledError:
            self._log.cancelled = True
            raise
        finally:
            self._log.finished = True

    async def aread(self):
        return b""


class ScriptedContext:
    def __init__(self, response, log):
        self._response = response
        self._log = log

    async def __aenter__(self):
        self._log.opened += 1
        return self._response

    async def __aexit__(self, *exc_info):
        self._log.closed = True
        return False


def scripted_httpx(script, log):
    """A drop-in for the ``httpx`` name inside engine.coordinator whose engine follows ``script``."""

    class Client:
        is_closed = False

        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        def stream(self, method, url, **kwargs):
            return ScriptedContext(ScriptedResponse(script, log), log)

    return types.SimpleNamespace(AsyncClient=Client, Timeout=lambda *a, **k: None, Limits=lambda *a, **k: None)


@pytest.fixture
def env(monkeypatch, isolated_config):
    """The coordinator's chat path with its engine faked, and the keep-alive interval shortened to 50 ms. The config
    is a throwaway one: what the chat path reads from it (coding members on or off, say) is never a developer's own."""
    monkeypatch.setattr(coordinator, "_inference_counter_lock", asyncio.Lock())
    monkeypatch.setattr(coordinator, "active_inference_requests", 0)
    monkeypatch.setattr(coordinator, "is_swapping_models", False)
    monkeypatch.setattr(coordinator, "is_recalculating_queue", False)
    monkeypatch.setattr(coordinator, "_engine_http", None)
    # The handler sets the swarm's model to the one it just served; patched here so that it is put back afterwards
    # (a test file that runs later must not find this one's model there).
    monkeypatch.setattr(coordinator, "global_active_model", "phi3-mini.gguf")
    monkeypatch.setattr(coordinator, "STREAM_KEEPALIVE_SECONDS", 0.05)
    monkeypatch.setattr(coordinator.manager, "running_models", {"phi3-mini.gguf": {
        "process": FakeEngineProcess(), "port": 8081, "log_file": None, "safe_context": 8192, "dynamic_slots": 4,
        "endpoints": [], "reserved_gb": 2.0}})
    monkeypatch.setattr(coordinator.registry, "workers", {})

    async def started(*args, **kwargs):
        return (True, 8081, "Started")

    async def ready(port):
        return True

    async def no_progress(port, model_name):
        return None

    monkeypatch.setattr(coordinator.manager, "astart_model", started)
    monkeypatch.setattr(coordinator, "_probe_engine_port", ready)
    monkeypatch.setattr(coordinator, "stream_vram_progress_async", no_progress)
    return monkeypatch


def engine(monkeypatch, script):
    log = EngineLog()
    monkeypatch.setattr(coordinator, "httpx", scripted_httpx(script, log))
    return log


async def ask(body=None):
    response = await coordinator.chat_completions(FakeRequest(dict(STREAM_BODY) if body is None else body))
    assert isinstance(response, StreamingResponse)
    return response


async def drain(response):
    return [chunk async for chunk in response.body_iterator]


def comments(chunks):
    return [chunk for chunk in chunks if chunk == KEEPALIVE]


def answer_only(chunks):
    return [chunk for chunk in chunks if chunk != KEEPALIVE]


def tasks_now():
    """The tasks running now: take this at the start of a test, and hand it to :func:`left_running` at the end."""
    return set(asyncio.all_tasks())


async def left_running(before):
    """Give the vram-progress task and any cancelled read their turn, then list the tasks the test started that are
    still running (the test runner's own tasks are in ``before``)."""
    await asyncio.sleep(0.05)
    return [task for task in asyncio.all_tasks() if task not in before and not task.done()]


# ASGI plumbing, as tests/test_coordinator_counter.py does it (spec_version 2.3 is uvicorn's: Starlette's task-group path).
ASGI_SCOPE = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "method": "POST",
              "path": "/v1/chat/completions", "headers": []}


# ======================================================================================
# The comment line itself
# ======================================================================================

def test_the_comment_is_a_valid_sse_comment_every_15_seconds():
    assert coordinator.STREAM_KEEPALIVE_SECONDS == 15.0
    assert coordinator.STREAM_KEEPALIVE_LINE == KEEPALIVE
    assert KEEPALIVE.startswith(b":") and KEEPALIVE.endswith(b"\n\n")       # a comment, then the blank line


# ======================================================================================
# The handler
# ======================================================================================

@pytest.mark.anyio
async def test_a_silent_engine_is_kept_alive_and_the_answer_then_arrives_whole_and_unchanged(env):
    before = tasks_now()
    engine(env, [0.3, *ANSWER])
    chunks = await drain(await ask())
    assert 2 <= len(comments(chunks)) <= 7, "about one comment every 50 ms of the 300 ms of silence"
    assert answer_only(chunks) == ANSWER                                    # byte for byte, in order
    assert chunks[:len(comments(chunks))] == comments(chunks)               # every comment came before the answer
    assert b"".join(answer_only(chunks)) == b"".join(ANSWER)
    assert coordinator.active_inference_requests == 0
    assert (await left_running(before)) == []                               # no engine read left running


@pytest.mark.anyio
async def test_no_comment_is_sent_once_the_engine_streams(env, monkeypatch):
    monkeypatch.setattr(coordinator, "STREAM_KEEPALIVE_SECONDS", 0.25)
    engine(env, [0.6, FRAME_1, 0.02, FRAME_2, 0.02, DONE])                 # silent, then a steady stream
    chunks = await drain(await ask())
    assert answer_only(chunks) == ANSWER
    first_answer = chunks.index(FRAME_1)
    assert len(comments(chunks)) >= 1 and all(c == KEEPALIVE for c in chunks[:first_answer])
    assert KEEPALIVE not in chunks[first_answer:]                           # none between or after the frames


@pytest.mark.anyio
async def test_an_engine_that_never_pauses_gets_no_comment_at_all(env, monkeypatch):
    monkeypatch.setattr(coordinator, "STREAM_KEEPALIVE_SECONDS", 5.0)
    engine(env, [*ANSWER])
    assert await drain(await ask()) == ANSWER


@pytest.mark.anyio
async def test_a_pause_after_a_whole_event_is_kept_alive_between_events(env):
    engine(env, [FRAME_1, 0.3, FRAME_2, DONE])
    chunks = await drain(await ask())
    assert answer_only(chunks) == ANSWER
    assert chunks[0] == FRAME_1 and chunks[-2:] == [FRAME_2, DONE]
    assert len(comments(chunks)) >= 2 and all(c == KEEPALIVE for c in chunks[1:-2])


@pytest.mark.anyio
async def test_a_comment_never_lands_inside_an_event(env):
    first_half, second_half = FRAME_1[:20], FRAME_1[20:]
    engine(env, [first_half, 0.3, second_half, FRAME_2, DONE])             # the engine stalls in the middle of an event
    chunks = await drain(await ask())
    assert chunks == [first_half, second_half, FRAME_2, DONE]              # nothing was put between the halves
    assert b"".join(chunks) == b"".join(ANSWER)


@pytest.mark.anyio
async def test_a_stalled_event_split_across_crlf_is_not_interrupted_either(env):
    crlf = b'data: {"a":1}\r\n\r\n'
    half = b'data: {"a":2}\r\n'
    engine(env, [crlf, 0.15, half, 0.3, b"\r\n", DONE])
    chunks = await drain(await ask())
    assert chunks.index(half) == chunks.index(crlf) + 1 + len(comments(chunks[:chunks.index(half)]))
    assert chunks[chunks.index(half) + 1] == b"\r\n"                       # nothing between "half" and its end
    assert answer_only(chunks) == [crlf, half, b"\r\n", DONE]


@pytest.mark.anyio
async def test_a_request_that_did_not_ask_for_a_stream_gets_no_comment(env):
    body = b'{"choices":[{"message":{"role":"assistant","content":"hi"}}]}'
    for stream in (False, None, "true", 1):
        request_body = {"model": "phi3-mini", "messages": [{"role": "user", "content": "hello"}]}
        if stream is not None:
            request_body["stream"] = stream
        engine(env, [0.2, body])
        assert await drain(await ask(request_body)) == [body], f"stream={stream!r}"     # one JSON body, nothing else


@pytest.mark.anyio
async def test_an_engine_that_fails_after_a_silence_is_reported_as_before_and_the_slot_comes_back(env):
    before = tasks_now()
    engine(env, [0.2, httpx.ReadError("the engine's socket was reset")])
    chunks = await drain(await ask())
    assert len(comments(chunks)) >= 2
    text = b"".join(answer_only(chunks)).decode()
    assert "Node disconnected mid-response" in text and '"dlne_error": "engine_dropped"' in text
    assert text.rstrip().endswith("[DONE]")
    assert coordinator.active_inference_requests == 0 and coding_gate.stats()["in_use"] == 0
    assert (await left_running(before)) == []


@pytest.mark.anyio
async def test_a_non_200_engine_answer_is_unchanged(env, monkeypatch):
    class Refusing(ScriptedResponse):
        async def aread(self):
            return b'{"error":{"message":"nope"}}'

    log = EngineLog()
    base = scripted_httpx([], log)

    class Client(base.AsyncClient):
        def stream(self, method, url, **kwargs):
            refusing = Refusing([], log)
            refusing.status_code = 500
            return ScriptedContext(refusing, log)

    monkeypatch.setattr(coordinator, "httpx", types.SimpleNamespace(
        AsyncClient=Client, Timeout=lambda *a, **k: None, Limits=lambda *a, **k: None))
    text = b"".join(await drain(await ask())).decode()
    assert "Error from internal engine: nope" in text and KEEPALIVE.decode() not in text


# --- a client that leaves ---------------------------------------------------------------------------------------

async def leave_on_first_chunk(response):
    """Run the response as an ASGI app whose client stops reading on its first body chunk (the socket's buffer is
    full), then disconnects: what uvicorn sees from a dropped connection. The response must stay referenced by the
    caller until the assertions (see tests/test_coordinator_counter.py's abandon_mid_stream for why)."""
    first_chunk_sent = asyncio.Event()
    disconnect_now = asyncio.Event()
    never = asyncio.Event()
    sent = []

    async def send(message):
        if message["type"] == "http.response.body":
            sent.append(message["body"])
            first_chunk_sent.set()
            await never.wait()

    async def receive():
        await disconnect_now.wait()
        return {"type": "http.disconnect"}

    task = asyncio.create_task(response(ASGI_SCOPE, receive, send))
    await first_chunk_sent.wait()
    disconnect_now.set()
    await task
    return sent


async def leave_while_the_proxy_waits(response, after_seconds):
    """The client reads everything it is sent and disconnects ``after_seconds`` in, while the proxy is waiting on the
    engine."""
    sent = []

    async def send(message):
        if message["type"] == "http.response.body":
            sent.append(message["body"])

    async def receive():
        await asyncio.sleep(after_seconds)
        return {"type": "http.disconnect"}

    await response(ASGI_SCOPE, receive, send)
    return sent


@pytest.mark.anyio
async def test_a_client_that_leaves_while_the_proxy_waits_gives_back_its_slot_and_stops_the_engine_read(env):
    before = tasks_now()
    log = engine(env, [30.0, *ANSWER])                                       # the engine is reading a long prompt
    response = await ask()
    assert coordinator.active_inference_requests == 1                       # the request holds its slot
    sent = await leave_while_the_proxy_waits(response, 0.3)
    assert KEEPALIVE in sent and FRAME_1 not in sent and DONE not in sent    # kept alive until the client left
    assert (await left_running(before)) == []                                # the read of the engine was cancelled
    assert log.cancelled is True and log.closed is True
    assert coordinator.active_inference_requests == 0
    assert coding_gate.stats()["in_use"] == 0


@pytest.mark.anyio
async def test_a_client_that_leaves_on_a_keep_alive_gives_back_its_slot(env):
    log = engine(env, [30.0, *ANSWER])
    response = await ask()
    assert coordinator.active_inference_requests == 1
    sent = await leave_on_first_chunk(response)
    assert sent == [KEEPALIVE]                                               # the first thing it was sent
    # The generator is parked on its yield and never resumed, so only the response's own background task can have
    # released the slot, as for any other chunk (tests/test_coordinator_counter.py, ITEM 1.5).
    assert coordinator.active_inference_requests == 0
    assert coding_gate.stats()["in_use"] == 0
    # The response is still referenced here; once it is dropped Python closes the abandoned generator, which ends
    # the engine read it left in flight.
    del response
    gc.collect()
    await asyncio.sleep(0.1)
    assert log.cancelled is True and log.closed is True


@pytest.mark.anyio
async def test_a_slot_is_released_once_whichever_way_the_client_leaves(env):
    engine(env, [30.0, *ANSWER])
    response = await ask()
    await leave_while_the_proxy_waits(response, 0.2)
    assert coordinator.active_inference_requests == 0
    await response.background()                                              # the background task is a no-op now
    assert coordinator.active_inference_requests == 0 and coding_gate.stats()["in_use"] == 0


@pytest.mark.anyio
async def test_many_clients_that_leave_mid_wait_leak_nothing(env):
    before = tasks_now()
    engine(env, [30.0, *ANSWER])
    responses = []
    for _ in range(6):
        response = await ask()
        responses.append(response)
        await leave_while_the_proxy_waits(response, 0.12)
    assert coordinator.active_inference_requests == 0 and coding_gate.stats()["in_use"] == 0
    assert (await left_running(before)) == []


def queued_gate(monkeypatch):
    """The engine has one request slot, and it is taken: a streamed request from the extension has to wait for it, and
    is sent queue frames (as tests/test_coding_endpoint.py sets it up). Returns the gate."""
    gate = CodingGate(slots=1)
    monkeypatch.setattr(coordinator, "coding_gate", gate)
    monkeypatch.setattr(coordinator, "QUEUE_FRAME_SECONDS", 0.1)
    monkeypatch.setattr(coordinator, "QUEUE_POLL_SECONDS", 0.03)
    monkeypatch.setattr(coordinator, "_coding_hold_seconds", deque(maxlen=40))
    assert gate.try_acquire()
    return gate


def extension_request():
    request = FakeRequest(dict(STREAM_BODY))
    request.headers = {"x-dlne-session": "keep-alive-test"}                  # the extension: it is sent queue frames
    return request


async def until(predicate, seconds=5.0):
    deadline = asyncio.get_running_loop().time() + seconds
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "gave up waiting"
        await asyncio.sleep(0.01)


@pytest.mark.anyio
async def test_a_request_that_waited_for_a_slot_is_kept_alive_through_the_engines_silence_too(env, monkeypatch):
    """The extension's path when every slot is busy (docs/coding-members.md section 10): queue frames first, then the
    usual handler's stream, which forwards the comments."""
    gate = queued_gate(monkeypatch)
    engine(env, [0.3, *ANSWER])
    response = await coordinator.chat_completions(extension_request())
    consumer = asyncio.create_task(drain(response))
    await until(lambda: gate.stats()["waiting"] == 1)
    await asyncio.sleep(0.25)                                                # a few queue frames' worth
    gate.release()
    chunks = await asyncio.wait_for(consumer, 10)
    text = b"".join(chunks)
    assert b'"dlne_queue"' in text and len(comments(chunks)) >= 2
    assert text.rindex(b'"dlne_queue"') < text.index(KEEPALIVE) < text.index(FRAME_1), "frames, comments, answer"
    assert text.replace(KEEPALIVE, b"").endswith(b"".join(ANSWER))
    assert gate.stats()["in_use"] == 0 and gate.stats()["waiting"] == 0
    assert coordinator.active_inference_requests == 0


# ======================================================================================
# A queued request whose handler is busy
# ======================================================================================
#
# Once its slot is granted, a queued request (the extension's, behind queue frames) goes to the usual handler, and the
# handler can be minutes before it has a stream to send: a swap to wait out, the engine to start and load, the summary of
# a long history. The 200 and the queue frames are all the client has had, so the handler runs in a task and the client
# is sent a comment every STREAM_KEEPALIVE_SECONDS it has not returned. Here the handler's slow stage is the engine
# memory estimate (it is awaited before the engine is started); nothing else about the handler is faked.

def busy_handler(monkeypatch, seconds):
    """Makes the handler's first await last ``seconds``. Returns what the tests ask about it."""
    log = types.SimpleNamespace(started=asyncio.Event(), calls=0, cancelled=False)

    async def estimate(model_path):
        log.calls += 1
        log.started.set()
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            log.cancelled = True
            raise
        return 2.0

    monkeypatch.setattr(coordinator.manager, "aestimate_model_vram_gb", estimate)
    return log


@pytest.mark.anyio
async def test_a_queued_request_is_kept_alive_while_its_handler_is_busy(env, monkeypatch):
    before = tasks_now()
    gate = queued_gate(monkeypatch)
    busy = busy_handler(monkeypatch, 0.35)
    engine(env, [*ANSWER])                                                   # the engine itself never pauses
    response = await coordinator.chat_completions(extension_request())
    consumer = asyncio.create_task(drain(response))
    await until(lambda: gate.stats()["waiting"] == 1)
    await asyncio.sleep(0.25)                                                # a few queue frames' worth
    gate.release()                                                           # the slot is granted: the handler starts
    chunks = await asyncio.wait_for(consumer, 10)
    text = b"".join(chunks)
    assert busy.calls == 1 and busy.cancelled is False                       # it was waited for, not cancelled
    assert len(comments(chunks)) >= 2, "a comment every 50 ms of the 350 ms the handler took"
    assert text.rindex(b'"dlne_queue"') < text.index(KEEPALIVE) < text.index(FRAME_1), "frames, comments, answer"
    assert text.replace(KEEPALIVE, b"").endswith(b"".join(ANSWER))
    assert gate.stats()["in_use"] == 0 and gate.stats()["waiting"] == 0
    assert coordinator.active_inference_requests == 0
    assert (await left_running(before)) == []


@pytest.mark.anyio
async def test_a_queued_request_whose_handler_is_quick_is_sent_no_comment(env, monkeypatch):
    gate = queued_gate(monkeypatch)
    monkeypatch.setattr(coordinator, "STREAM_KEEPALIVE_SECONDS", 5.0)
    engine(env, [*ANSWER])
    response = await coordinator.chat_completions(extension_request())
    consumer = asyncio.create_task(drain(response))
    await until(lambda: gate.stats()["waiting"] == 1)
    gate.release()
    chunks = await asyncio.wait_for(consumer, 10)
    assert comments(chunks) == [] and b"".join(chunks).endswith(b"".join(ANSWER))


@pytest.mark.anyio
async def test_the_handler_is_never_cancelled_to_measure_the_silence(env, monkeypatch):
    """Whatever the handler is, a comment is a reason to wait again, not to stop it."""
    gate = queued_gate(monkeypatch)
    handled = types.SimpleNamespace(cancelled=False, finished=False)

    async def handler(request, slot_held=False):
        try:
            await asyncio.sleep(0.4)
        except asyncio.CancelledError:
            handled.cancelled = True
            raise
        handled.finished = True
        return StreamingResponse(iter([b"data: [DONE]\n\n"]), media_type="text/event-stream")

    monkeypatch.setattr(coordinator, "_chat_completions_impl", handler)
    response = await coordinator.chat_completions(extension_request())
    consumer = asyncio.create_task(drain(response))
    await until(lambda: gate.stats()["waiting"] == 1)
    gate.release()
    chunks = await asyncio.wait_for(consumer, 10)
    assert handled.finished is True and handled.cancelled is False
    assert len(comments(chunks)) >= 3 and chunks[-1] == b"data: [DONE]\n\n"


@pytest.mark.anyio
async def test_a_handler_that_fails_after_the_comments_is_reported_in_band_as_before(env, monkeypatch):
    """A refusal after the 200 has gone out is a notice in the stream, marked so the extension's agent sends the step
    again (the 500 here is the engine failing to start)."""
    before = tasks_now()
    gate = queued_gate(monkeypatch)
    busy_handler(monkeypatch, 0.25)

    async def refused(*args, **kwargs):
        return (False, 0, "Binary Missing")

    monkeypatch.setattr(coordinator.manager, "astart_model", refused)
    response = await coordinator.chat_completions(extension_request())
    consumer = asyncio.create_task(drain(response))
    await until(lambda: gate.stats()["waiting"] == 1)
    gate.release()
    chunks = await asyncio.wait_for(consumer, 10)
    text = b"".join(chunks).decode()
    assert len(comments(chunks)) >= 2
    assert text.index(KEEPALIVE.decode()) < text.index("[System Alert] Failed to initialize inference engine.")
    assert '"dlne_error": "engine_unavailable"' in text and text.rstrip().endswith("[DONE]")
    assert gate.stats()["in_use"] == 0 and coordinator.active_inference_requests == 0
    assert (await left_running(before)) == []


async def leave_on_the_first_keep_alive(response):
    """Run the response as an ASGI app whose client stops reading at its first comment (the socket's buffer is full),
    then disconnects. The response must stay referenced by the caller until its assertions (see
    tests/test_coordinator_counter.py's abandon_mid_stream)."""
    got_one = asyncio.Event()
    disconnect_now = asyncio.Event()
    never = asyncio.Event()
    sent = []

    async def send(message):
        if message["type"] == "http.response.body":
            sent.append(message["body"])
            if message["body"] == KEEPALIVE:
                got_one.set()
                await never.wait()

    async def receive():
        await disconnect_now.wait()
        return {"type": "http.disconnect"}

    task = asyncio.create_task(response(ASGI_SCOPE, receive, send))
    await got_one.wait()
    disconnect_now.set()
    await task
    return sent


@pytest.mark.anyio
async def test_a_client_that_leaves_while_the_handler_is_busy_cancels_it_and_everything_comes_back(env, monkeypatch):
    before = tasks_now()
    leases = coordinator.lease_manager.active_count()
    gate = queued_gate(monkeypatch)
    busy = busy_handler(monkeypatch, 30.0)                                   # the handler would be at it for good
    started = []

    async def never_started(*args, **kwargs):
        started.append(args)
        return (True, 8081, "Started")

    monkeypatch.setattr(coordinator.manager, "astart_model", never_started)
    response = await coordinator.chat_completions(extension_request())
    leaving = asyncio.create_task(leave_while_the_proxy_waits(response, 0.5))
    await until(lambda: gate.stats()["waiting"] == 1)
    gate.release()                                                           # the slot is granted: the handler starts
    await until(busy.started.is_set)
    sent = await asyncio.wait_for(leaving, 10)
    assert KEEPALIVE in sent and b"data: [DONE]" not in b"".join(sent)       # kept alive until the client left
    assert (await left_running(before)) == []                                # the handler's task is over
    assert busy.cancelled is True and started == []                          # cancelled where it was; no engine started
    assert gate.stats()["in_use"] == 0 and gate.stats()["waiting"] == 0      # the slot, the lease and the count
    assert coordinator.lease_manager.active_count() == leases
    assert coordinator.active_inference_requests == 0


@pytest.mark.anyio
async def test_a_client_that_leaves_on_a_comment_gives_everything_back_once_the_stream_is_closed(env, monkeypatch):
    before = tasks_now()
    leases = coordinator.lease_manager.active_count()
    gate = queued_gate(monkeypatch)
    busy = busy_handler(monkeypatch, 30.0)
    response = await coordinator.chat_completions(extension_request())
    leaving = asyncio.create_task(leave_on_the_first_keep_alive(response))
    await until(lambda: gate.stats()["waiting"] == 1)
    gate.release()
    sent = await asyncio.wait_for(leaving, 10)
    assert sent[-1] == KEEPALIVE
    # The generator is parked on its yield and never resumed: nothing has told the handler yet. Python closes an
    # abandoned generator when it is dropped, and that cancels the handler, which gives the slot back as it always did.
    assert gate.stats()["in_use"] == 1
    del response
    gc.collect()
    await asyncio.sleep(0.2)
    assert busy.cancelled is True
    assert (await left_running(before)) == []
    assert gate.stats()["in_use"] == 0 and coordinator.active_inference_requests == 0
    assert coordinator.lease_manager.active_count() == leases


@pytest.mark.anyio
async def test_a_handler_that_finishes_as_the_client_leaves_still_gives_its_slot_back(env, monkeypatch):
    """The narrow case: the handler returns its response after the client has gone and before the abandoned stream is
    closed. Nobody will read that response, so its own background task (what gives the slot back) is run."""
    gate = queued_gate(monkeypatch)
    released = []

    async def give_back():
        released.append(True)
        gate.release()

    async def handler(request, slot_held=False):
        await asyncio.sleep(0.3)
        return StreamingResponse(iter([b"data: [DONE]\n\n"]), media_type="text/event-stream",
                                 background=BackgroundTask(give_back))

    monkeypatch.setattr(coordinator, "_chat_completions_impl", handler)
    response = await coordinator.chat_completions(extension_request())
    leaving = asyncio.create_task(leave_on_the_first_keep_alive(response))
    await until(lambda: gate.stats()["waiting"] == 1)
    gate.release()                                                           # the slot is granted: the handler starts
    await asyncio.wait_for(leaving, 10)                                      # the client left on a comment, at ~50 ms
    await asyncio.sleep(0.5)                                                 # and the handler finished meanwhile
    assert released == [] and gate.stats()["in_use"] == 1                    # nothing has run its release yet
    del response
    gc.collect()
    await asyncio.sleep(0.2)
    assert released == [True]
    assert gate.stats()["in_use"] == 0


@pytest.mark.anyio
async def test_many_clients_that_leave_while_the_handler_is_busy_leak_nothing(env, monkeypatch):
    before = tasks_now()
    leases = coordinator.lease_manager.active_count()
    busy = busy_handler(monkeypatch, 30.0)
    responses = []
    for _ in range(4):
        gate = queued_gate(monkeypatch)
        response = await coordinator.chat_completions(extension_request())
        responses.append(response)
        leaving = asyncio.create_task(leave_while_the_proxy_waits(response, 0.4))
        await until(lambda: gate.stats()["waiting"] == 1)
        gate.release()
        await asyncio.wait_for(leaving, 10)
        await asyncio.sleep(0.1)
        assert gate.stats()["in_use"] == 0
    assert busy.calls == 4 and busy.cancelled is True
    assert coordinator.active_inference_requests == 0 and coordinator.lease_manager.active_count() == leases
    assert (await left_running(before)) == []


# ======================================================================================
# Against a real HTTP stream
# ======================================================================================
#
# The fakes above are not httpx. The keep-alive reads the engine from a task of its own, one task per chunk, and httpx
# reads its socket through anyio, so these run the real client against a local server that answers as llama-server
# does (200 and an event stream at once, then silence while it "reads the prompt", then the answer).

class SlowEngine:
    """A local HTTP/1.1 server on a free loopback port. It follows ``script`` (numbers pause, bytes are a chunk) and
    notes when the client hangs up on it (``gone``)."""

    def __init__(self, script):
        self.script = script
        self.gone = asyncio.Event()
        self.requests = 0
        self.port = 0
        self._server = None

    async def __aenter__(self):
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc_info):
        self._server.close()
        await self._server.wait_closed()
        return False

    async def _handle(self, reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            length = 0
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":", 1)[1])
            if length:
                await reader.readexactly(length)
            self.requests += 1
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n")
            await writer.drain()
            hangup = asyncio.ensure_future(reader.read())                    # b"" the moment the client closes
            try:
                for step in self.script:
                    if isinstance(step, (int, float)):
                        await asyncio.wait({hangup}, timeout=step)
                        if hangup.done():
                            self.gone.set()
                            return
                    else:
                        writer.write(b"%x\r\n" % len(step) + step + b"\r\n")
                        await writer.drain()
                writer.write(b"0\r\n\r\n")
                await writer.drain()
            finally:
                hangup.cancel()
        except (ConnectionError, asyncio.IncompleteReadError):
            self.gone.set()
        finally:
            writer.close()


def point_the_engine_at(env, port):
    """The chat path's engine is the local server on ``port``; the real httpx is used (nothing about it is faked)."""
    async def started(model, endpoints, vram, sysram, queue_channels=2):
        return (True, port, "Started")

    env.setattr(coordinator.manager, "astart_model", started)
    env.setattr(coordinator.manager, "running_models", {"phi3-mini.gguf": {
        "process": FakeEngineProcess(), "port": port, "log_file": None, "safe_context": 8192, "dynamic_slots": 4,
        "endpoints": [], "reserved_gb": 2.0}})


async def close_the_pool():
    """The pooled engine client belongs to this test's event loop: close it before the loop does."""
    pooled = coordinator._engine_http
    if pooled is not None and not pooled[1].is_closed:
        await pooled[1].aclose()


@pytest.mark.anyio
async def test_a_real_silent_engine_is_kept_alive_and_the_answer_arrives_whole(env):
    before = tasks_now()
    async with SlowEngine([0.35, FRAME_1, 0.01, FRAME_2, DONE]) as slow:
        point_the_engine_at(env, slow.port)
        chunks = await drain(await ask())
    await close_the_pool()
    assert len(comments(chunks)) >= 2
    assert b"".join(chunks).replace(KEEPALIVE, b"") == b"".join(ANSWER)     # whole and unchanged
    assert chunks[0] == KEEPALIVE                                            # kept alive from the first interval
    assert coordinator.active_inference_requests == 0
    assert (await left_running(before)) == []


@pytest.mark.anyio
async def test_a_real_engine_connection_is_closed_when_the_client_leaves_mid_wait(env):
    before = tasks_now()
    async with SlowEngine([30.0, *ANSWER]) as slow:
        point_the_engine_at(env, slow.port)
        response = await ask()
        sent = await leave_while_the_proxy_waits(response, 0.3)
        assert KEEPALIVE in sent and DONE not in sent
        await asyncio.wait_for(slow.gone.wait(), 3.0)                        # the engine saw its client hang up
    await close_the_pool()
    assert coordinator.active_inference_requests == 0 and coding_gate.stats()["in_use"] == 0
    assert (await left_running(before)) == []


@pytest.mark.anyio
async def test_a_real_engine_that_drops_the_connection_after_a_silence_is_reported_as_before(env):
    async def hang_up(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n")
        await writer.drain()
        await asyncio.sleep(0.25)
        writer.transport.abort()                                             # a reset, not a clean end

    server = await asyncio.start_server(hang_up, "127.0.0.1", 0)
    try:
        point_the_engine_at(env, server.sockets[0].getsockname()[1])
        chunks = await drain(await ask())
    finally:
        server.close()
        await server.wait_closed()
        await close_the_pool()
    text = b"".join(chunks).decode()
    assert text.startswith(KEEPALIVE.decode()) and "dlne_error" in text and "[DONE]" in text
    assert coordinator.active_inference_requests == 0


# ======================================================================================
# The helper that watches the silence
# ======================================================================================

class Source:
    """A byte stream that pauses, and notes whether anything cancelled it."""

    def __init__(self, script):
        self.script = script
        self.cancelled = False
        self.finished = False

    def __aiter__(self):
        return self._run()

    async def _run(self):
        try:
            for step in self.script:
                if isinstance(step, BaseException):
                    raise step
                if isinstance(step, (int, float)):
                    await asyncio.sleep(step)
                else:
                    yield step
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        finally:
            self.finished = True


async def collect(marks):
    return [item async for item in marks]


@pytest.mark.anyio
async def test_the_helper_marks_each_interval_of_silence_and_never_cancels_the_read():
    before = tasks_now()
    source = Source([0.32, b"a", b"b"])
    marks = coordinator._SilenceMarks(source, 0.05)
    items = await collect(marks)
    assert items[-2:] == [b"a", b"b"] and len(items) >= 4
    assert all(item is None for item in items[:-2])
    assert source.cancelled is False                                         # waited for again, never cancelled
    assert (await left_running(before)) == []


@pytest.mark.anyio
async def test_the_helper_is_transparent_to_a_stream_that_never_pauses():
    marks = coordinator._SilenceMarks(Source([b"a", b"b", b"c"]), 5.0)
    assert await collect(marks) == [b"a", b"b", b"c"]


@pytest.mark.anyio
async def test_close_cancels_a_read_in_flight_and_is_safe_to_repeat():
    before = tasks_now()
    source = Source([30.0, b"never"])
    marks = coordinator._SilenceMarks(source, 0.03)
    assert await marks.__anext__() is None                                   # silent for the interval; the read goes on
    assert source.cancelled is False
    marks.close()
    marks.close()
    assert (await left_running(before)) == []
    assert source.cancelled is True


@pytest.mark.anyio
async def test_close_with_no_read_in_flight_does_nothing():
    marks = coordinator._SilenceMarks(Source([b"a"]), 5.0)
    marks.close()
    assert await collect(coordinator._SilenceMarks(Source([b"a"]), 5.0)) == [b"a"]


@pytest.mark.anyio
async def test_a_failure_of_the_stream_reaches_the_caller_and_leaves_nothing_running():
    before = tasks_now()
    marks = coordinator._SilenceMarks(Source([0.08, RuntimeError("the engine reset the connection")]), 0.03)
    seen = []
    with pytest.raises(RuntimeError, match="reset the connection"):
        async for item in marks:
            seen.append(item)
    assert seen and all(item is None for item in seen)
    marks.close()
    assert (await left_running(before)) == []


@pytest.mark.anyio
async def test_cancelling_the_waiter_leaves_the_read_to_close():
    before = tasks_now()
    source = Source([30.0])
    marks = coordinator._SilenceMarks(source, 5.0)
    waiter = asyncio.create_task(marks.__anext__())
    await asyncio.sleep(0.05)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert source.cancelled is False                                         # the waiter's cancel is not the read's
    marks.close()
    assert (await left_running(before)) == []
    assert source.cancelled is True
