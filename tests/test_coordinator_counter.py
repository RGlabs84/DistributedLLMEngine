"""
Tests for the in-flight inference counter in ``engine.coordinator`` — Contract D, bug #3,
plus the round-2 hardening of the request path around it (items 1.1, 1.2, 1.5, 1.6, 1.7).

``active_inference_requests`` is what the viral-traffic detector reads to decide whether to
tear the engine down and reboot it with more parallel slots. The increment at the top of
``/v1/chat/completions`` was not paired on the malformed-body path or on the
``raise HTTPException`` rejections, so every bad request permanently inflated the number.
Once it passed 12 the coordinator restarted the engine on every subsequent completion, and
it never came back down.

The endpoint is exercised by calling the coroutine directly with a stub request object rather
than through a TestClient. That is deliberate:

* it keeps the assertions on the counter, not on the middleware and dependency stack;
* it needs neither a listening socket nor the real ``httpx``, which is what lets the suite
  run with no network;
* ``StreamingResponse`` hands the counter's release to a generator, and driving that
  generator by hand is the only way to assert that the slot is still held *while* the stream
  is outstanding and released exactly once when it finishes.

Every scenario asserts the counter returns to the value it started at — not merely to zero —
so a leak cannot hide behind a baseline that happens to be empty.
"""

import asyncio
import gc
import json
import secrets
import time
import types

import pytest
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

import engine.coordinator as coordinator
from engine.authn import NonceCache, sign_heartbeat
from engine.priority import UsageWindow
from engine.version import VERSION


# ======================================================================================
# Test doubles
# ======================================================================================

class FakeClient:
    """The ``request.client`` tuple-ish object, which only ever has ``.host`` read."""

    def __init__(self, host):
        self.host = host


class FakeRequest:
    """
    Minimal stand-in for ``starlette.requests.Request``.

    ``chat_completions`` touches exactly two things on it: ``await request.json()`` and
    ``request.headers.get("Last-Event-ID")``. Passing an exception instance as the body makes
    ``json()`` raise, which is how the "body is not valid JSON" path is reached.

    ``client_host`` exists for the ``get_client_ip`` tests, which read ``request.client.host``
    and ``request.headers``.

    ``state.dlne_tier`` is what the contribution middleware would have set: "host", so the
    model these tests name is the one that runs (public callers get the live model instead,
    tests/test_model_choice.py).
    """

    def __init__(self, body=None, headers=None, client_host="203.0.113.7"):
        self._body = body
        self.headers = headers or {}
        self.client = FakeClient(client_host) if client_host is not None else None
        self.state = types.SimpleNamespace(dlne_tier="host")

    async def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeStreamResponse:
    """An httpx streamed response that yields a couple of SSE frames and then stops."""

    def __init__(self, chunks, status_code=200):
        self.status_code = status_code
        self._chunks = chunks

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    async def aread(self):
        return b"".join(self._chunks)


class FakeStreamContext:
    """The ``async with client.stream(...)`` context manager."""

    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc_info):
        return False


class FakeAsyncClient:
    """
    Replaces ``httpx.AsyncClient`` so no socket is ever opened.

    ``status_code`` is a class attribute so a test can make the upstream engine return an
    error without rebuilding the whole double.
    """

    status_code = 200
    is_closed = False
    chunks = [
        b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n',
        b"data: [DONE]\n\n",
    ]

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    def stream(self, method, url, **kwargs):
        return FakeStreamContext(FakeStreamResponse(self.chunks, self.status_code))


class FakeEngineProcess:
    """
    Stands in for the ``subprocess.Popen`` handle stored in a ``running_models`` entry.

    The chat handler only ever calls ``poll()`` on it (and reads ``returncode`` when that
    comes back non-None), so a live engine is a ``poll()`` that returns None.
    """

    def __init__(self, returncode=None):
        self.returncode = returncode

    def poll(self):
        return self.returncode


def running_engine_entry(port=8081, safe_context=8192, dynamic_slots=4, alive=True):
    """
    A ``running_models`` entry shaped exactly like the one ``InferenceManager`` publishes.

    ``chat_completions`` now takes a single coherent snapshot of this through
    ``manager.asnapshot_model`` (finding R3-9) and drives the readiness loop, the crash
    detection and the context sizing from it, so the tests have to supply a realistic entry
    rather than relying on an empty table making the handler skip the block.
    """
    return {
        "process": FakeEngineProcess(None if alive else 1),
        "port": port,
        "log_file": None,
        "safe_context": safe_context,
        "dynamic_slots": dynamic_slots,
        "endpoints": [],
        "reserved_gb": 2.0,
    }


def fake_httpx_module():
    """
    A drop-in for the ``httpx`` name inside ``engine.coordinator``.

    Patching the module reference in the coordinator's own namespace (rather than attributes
    on the real ``httpx``) keeps the replacement scoped to the code under test — nothing else
    in the interpreter sees a mutated httpx.
    """
    return types.SimpleNamespace(
        AsyncClient=FakeAsyncClient,
        Timeout=lambda *args, **kwargs: None,
        Limits=lambda *args, **kwargs: None,
    )


# ======================================================================================
# Fixture
# ======================================================================================

@pytest.fixture
def counter_env(monkeypatch):
    """
    Isolates the coordinator's module-level state for one test.

    Everything set here is restored by monkeypatch afterwards, so tests cannot leak state
    into each other through the shared ``engine.coordinator`` module object.
    """
    # Each test drives its own event loop through asyncio.run(); a fresh lock guarantees one
    # bound to an already-closed loop can never be inherited by the next test.
    monkeypatch.setattr(coordinator, "_inference_counter_lock", asyncio.Lock())

    monkeypatch.setattr(coordinator, "active_inference_requests", 0)
    monkeypatch.setattr(coordinator, "is_swapping_models", False)
    monkeypatch.setattr(coordinator, "is_recalculating_queue", False)
    monkeypatch.setattr(coordinator, "httpx", fake_httpx_module())
    # The pooled engine client is per event loop; never inherit another test's fake.
    monkeypatch.setattr(coordinator, "_engine_http", None)

    # No real llama-server exists, so the table holds one *simulated* engine for the model
    # every test in this file asks for. It has to be present: since finding R3-9 the handler
    # treats "astart_model said yes but the entry is gone" as a concurrent teardown and
    # answers with the retry nudge instead of proxying to a port with no listener, so an
    # empty table no longer means "skip the readiness block".
    monkeypatch.setattr(coordinator.manager, "running_models",
                        {"phi3-mini.gguf": running_engine_entry()})
    monkeypatch.setattr(coordinator.registry, "workers", {})

    # ...and the readiness probe is answered without opening a socket. Patched as a named
    # coordinator function rather than by monkeypatching asyncio.open_connection globally.
    async def instantly_ready(port):
        return True

    monkeypatch.setattr(coordinator, "_probe_engine_port", instantly_ready)

    async def no_vram_progress(port, model_name):
        """Replaces the SSE progress reader, which would otherwise dial 127.0.0.1."""
        return None

    monkeypatch.setattr(coordinator, "stream_vram_progress_async", no_vram_progress)

    return monkeypatch


def set_start_result(monkeypatch, result):
    """Makes ``manager.astart_model`` return a fixed ``(success, port, message)`` tuple."""
    async def fake_astart_model(*args, **kwargs):
        return result

    monkeypatch.setattr(coordinator.manager, "astart_model", fake_astart_model)


async def drain(response):
    """Consumes a StreamingResponse's body generator, returning the chunks it produced."""
    return [chunk async for chunk in response.body_iterator]


# ======================================================================================
# The counter helper itself
# ======================================================================================

def test_counter_is_clamped_at_zero(counter_env):
    """
    A stray double-decrement must not drive the counter negative.

    A negative value permanently disabled the viral-traffic queue recalculation, because the
    threshold comparison could never be reached again.
    """
    async def scenario():
        assert await coordinator._change_inference_count(-1) == 0
        assert await coordinator._change_inference_count(-5) == 0
        assert await coordinator._change_inference_count(1) == 1
        return coordinator.active_inference_requests

    assert asyncio.run(scenario()) == 1


def test_counter_updates_are_serialised(counter_env):
    """
    The increment is a read-modify-write. These are coroutines on one event loop, but they
    interleave at every ``await``, so the update is taken under a lock — 200 concurrent
    adjustments must net out exactly.
    """
    async def scenario():
        await asyncio.gather(*[coordinator._change_inference_count(1) for _ in range(100)])
        assert coordinator.active_inference_requests == 100
        await asyncio.gather(*[coordinator._change_inference_count(-1) for _ in range(100)])
        return coordinator.active_inference_requests

    assert asyncio.run(scenario()) == 0


# ======================================================================================
# BUG #3 REGRESSION — rejected requests must return the slot
# ======================================================================================

def test_a_body_with_no_model_field_uses_the_active_model_and_returns_the_slot(counter_env, monkeypatch):
    """
    A body with no ``model`` field used to be rejected with a 400 (BUG #3: that ``raise`` skipped
    the decrement, so a client looping on it ratcheted the counter past the recalculation
    threshold). It is no longer an error: the extension leaves ``dlne.chatModel`` empty on
    purpose so the request follows whatever model the cluster is serving. The request must be
    started against that model, and the slot must still come back.
    """
    monkeypatch.setattr(coordinator, "global_active_model", "phi3-mini")
    started = []

    async def recording_start(model_path, *args, **kwargs):
        started.append(model_path)
        return (True, 8081, "Started")

    monkeypatch.setattr(coordinator.manager, "astart_model", recording_start)
    coordinator.active_inference_requests = 3      # a deliberately non-zero baseline
    baseline = coordinator.active_inference_requests

    async def scenario():
        response = await coordinator.chat_completions(
            FakeRequest({"messages": [{"role": "user", "content": "hi"}]})
        )
        assert isinstance(response, StreamingResponse)
        await drain(response)

    asyncio.run(scenario())

    assert started == ["phi3-mini.gguf"]
    assert coordinator.active_inference_requests == baseline


def test_counter_returns_to_baseline_when_the_body_is_not_json(counter_env):
    """
    The other half of the leak: ``await request.json()`` raising landed outside the release
    path too, so a truncated upload leaked a slot just as reliably.
    """
    coordinator.active_inference_requests = 2
    baseline = coordinator.active_inference_requests

    async def scenario():
        with pytest.raises(HTTPException) as excinfo:
            await coordinator.chat_completions(FakeRequest(ValueError("truncated body")))
        assert excinfo.value.status_code == 400

    asyncio.run(scenario())

    assert coordinator.active_inference_requests == baseline


def test_repeated_malformed_requests_do_not_accumulate(counter_env):
    """
    The failure mode as a user would hit it: a client retrying a bad request in a loop. Any
    per-request leak, however small, shows up here as a counter that never comes back down.
    """
    baseline = coordinator.active_inference_requests

    async def scenario():
        for _ in range(25):
            with pytest.raises(HTTPException):
                await coordinator.chat_completions(FakeRequest(ValueError("truncated body")))

    asyncio.run(scenario())

    assert coordinator.active_inference_requests == baseline


def test_concurrent_malformed_requests_do_not_accumulate(counter_env):
    """Ten rejections in flight at once still net out to the baseline."""
    baseline = coordinator.active_inference_requests

    async def one_rejected_request():
        with pytest.raises(HTTPException):
            await coordinator.chat_completions(FakeRequest(ValueError("truncated body")))

    async def scenario():
        # gather() must be built inside the running loop, not passed into asyncio.run().
        await asyncio.gather(*[one_rejected_request() for _ in range(10)])

    asyncio.run(scenario())

    assert coordinator.active_inference_requests == baseline


def test_counter_returns_to_baseline_when_the_engine_fails_to_start(counter_env, monkeypatch):
    """The 500 raised after a failed engine start is another path out from under the increment."""
    set_start_result(monkeypatch, (False, 0, "Binary Missing"))
    baseline = coordinator.active_inference_requests

    async def scenario():
        with pytest.raises(HTTPException) as excinfo:
            await coordinator.chat_completions(
                FakeRequest({"model": "phi3-mini", "messages": [{"role": "user", "content": "hi"}]})
            )
        assert excinfo.value.status_code == 500

    asyncio.run(scenario())

    assert coordinator.active_inference_requests == baseline


def test_counter_returns_to_baseline_after_the_maxed_out_nudge(counter_env, monkeypatch):
    """
    "Network Maxed Out" is answered with a short StreamingResponse rather than an error, so
    its release happens inside a generator's ``finally``. That generator is the only owner of
    the slot, and it has to give it back once the client has read the nudge.
    """
    set_start_result(monkeypatch, (False, 0, "Network Maxed Out"))
    # Pinned (2.6.2 review): the swarm's own model not fitting is an engine between restarts, not "maxed out", so
    # which model is the swarm's must not be left to whatever an earlier test file set it to.
    monkeypatch.setattr(coordinator, "global_active_model", "some-other-model.gguf")
    baseline = coordinator.active_inference_requests

    async def scenario():
        response = await coordinator.chat_completions(
            FakeRequest({"model": "phi3-mini", "messages": [{"role": "user", "content": "hi"}]})
        )
        assert isinstance(response, StreamingResponse)
        # Ownership has been handed to the generator, so the slot is still held here.
        assert coordinator.active_inference_requests == baseline + 1

        chunks = await drain(response)
        assert b"maxed out" in b"".join(chunks) and b"dlne_error" not in b"".join(chunks)

    asyncio.run(scenario())

    assert coordinator.active_inference_requests == baseline


def test_the_swarms_own_model_not_fitting_asks_for_the_step_again_and_gives_the_trial_chat_back(counter_env,
                                                                                                monkeypatch):
    """2026-10-28 10:11: with the laptop's shard gone the swarm's own IQ4_XS could not start, and "maxed out ... switch
    your chat" ended a 330-step task. It is the engine between restarts: "engine_unavailable", and the free-trial chat
    the request spent comes back (the agent sends the step again)."""
    set_start_result(monkeypatch, (False, 0, "Network Maxed Out"))
    monkeypatch.setattr(coordinator, "global_active_model", "phi3-mini.gguf")
    refunds = []
    monkeypatch.setattr(coordinator.starter_quota_mgr, "refund", lambda client_id, count=1: refunds.append(client_id))
    request = FakeRequest({"model": "phi3-mini", "messages": [{"role": "user", "content": "hi"}]})
    request.state.starter_quota_remaining = 41
    request.url = types.SimpleNamespace(path="/v1/chat/completions")

    async def scenario():
        response = await coordinator.chat_completions(request)
        body = b"".join(await drain(response))
        assert b"reorganising" in body and b'"dlne_error": "engine_unavailable"' in body
        assert b"switch your chat" not in body

    asyncio.run(scenario())
    assert refunds == [coordinator.trial_identity("203.0.113.7")], "given back once"


def test_a_cpu_contributor_gets_its_window_chat_back_and_no_trial_chat_is_touched(counter_env, monkeypatch):
    """2.6.2 review: a CPU contributor's step that met an engine restart spent one of its window's chats (50 per 5 h by
    default) each time the agent sent it again, and none came back."""
    set_start_result(monkeypatch, (False, 0, "Network Maxed Out"))
    monkeypatch.setattr(coordinator, "global_active_model", "phi3-mini.gguf")
    window = UsageWindow()
    monkeypatch.setattr(coordinator, "cpu_coding_window", window)
    trial_refunds = []
    monkeypatch.setattr(coordinator.starter_quota_mgr, "refund", lambda client_id, count=1: trial_refunds.append(client_id))
    for _ in range(3):
        window.check_and_record("worker-cpu", 50, 5 * 3600)       # the middleware spent this request's chat, and two before
    request = FakeRequest({"model": "phi3-mini", "messages": [{"role": "user", "content": "hi"}]})
    request.state.dlne_tier = "cpu"
    request.state.cpu_window_key = "worker-cpu"
    request.url = types.SimpleNamespace(path="/v1/chat/completions")

    async def scenario():
        response = await coordinator.chat_completions(request)
        body = b"".join(await drain(response))
        assert b'"dlne_error": "engine_unavailable"' in body

    asyncio.run(scenario())
    assert window.peek("worker-cpu", 50, 5 * 3600)[0] == 48, "this request's one chat is back, the two before are not"
    assert trial_refunds == []


@pytest.mark.parametrize("status, tagged", [(504, True), (500, True), (503, True), (400, False), (403, False)])
def test_a_queued_chat_whose_engine_fails_or_is_loading_is_told_to_send_the_step_again(monkeypatch, status, tagged):
    """2.6.2 review: a queued request that reached its slot and then met the engine failing or still loading (a
    5xx) is marked "engine_unavailable" as a plain string, the form the extension's agent retries; it used to be
    "[System Alert] ..." text alone, which ended the agent's task. A refusal (4xx) is left as it was."""
    async def nothing(*args, **kwargs):
        return None

    async def granted(self, deadline):
        self.winner = "engine"
        return
        yield

    async def failing(request, slot_held=False):
        raise HTTPException(status_code=status, detail="Inference engine timed out while loading the model.")

    monkeypatch.setattr(coordinator, "_change_inference_count", nothing)
    monkeypatch.setattr(coordinator._QueuedChat, "_wait", granted)
    monkeypatch.setattr(coordinator, "_chat_completions_impl", failing)
    queued = coordinator._QueuedChat(FakeRequest({"messages": [{"role": "user", "content": "hi"}]}), {}, {})

    async def run():
        return b"".join([chunk async for chunk in queued._run()])

    body = asyncio.run(run())
    assert b"timed out while loading" in body
    assert (b'"dlne_error": "engine_unavailable"' in body) is tagged


# ======================================================================================
# The normal streamed request
# ======================================================================================

def test_counter_returns_to_baseline_after_a_normal_streamed_request(counter_env, monkeypatch):
    """
    The success path, which is where the counter is legitimately held for a long time.

    The slot must stay taken for as long as the stream is open — that is what the
    viral-traffic detector is measuring — and be released exactly once when the generator is
    exhausted, not when the handler returns.
    """
    set_start_result(monkeypatch, (True, 8081, "Started"))
    coordinator.active_inference_requests = 1
    baseline = coordinator.active_inference_requests

    async def scenario():
        response = await coordinator.chat_completions(
            FakeRequest({"model": "phi3-mini",
                         "messages": [{"role": "user", "content": "hello"}]})
        )
        assert isinstance(response, StreamingResponse)
        assert coordinator.active_inference_requests == baseline + 1

        chunks = await drain(response)
        assert b"[DONE]" in b"".join(chunks)
        assert coordinator.active_inference_requests == baseline

        # Let the vram-progress background task retire so it does not outlive the loop.
        await asyncio.sleep(0)

    asyncio.run(scenario())

    assert coordinator.active_inference_requests == baseline


def test_counter_returns_to_baseline_when_the_engine_drops_mid_stream(counter_env, monkeypatch):
    """
    A worker disconnecting mid-generation is the single most common real-world exit from this
    endpoint. The generator catches it, emits the fallback message and still releases.
    """
    set_start_result(monkeypatch, (True, 8081, "Started"))
    baseline = coordinator.active_inference_requests

    class ExplodingClient(FakeAsyncClient):
        def stream(self, method, url, **kwargs):
            raise ConnectionResetError("worker node vanished")

    monkeypatch.setattr(
        coordinator, "httpx",
        types.SimpleNamespace(AsyncClient=ExplodingClient,
                              Timeout=lambda *args, **kwargs: None,
                              Limits=lambda *args, **kwargs: None),
    )

    async def scenario():
        response = await coordinator.chat_completions(
            FakeRequest({"model": "phi3-mini",
                         "messages": [{"role": "user", "content": "hello"}]})
        )
        chunks = await drain(response)
        assert b"Node disconnected" in b"".join(chunks)
        await asyncio.sleep(0)

    asyncio.run(scenario())

    assert coordinator.active_inference_requests == baseline


def test_counter_returns_to_baseline_when_the_engine_returns_an_error_status(counter_env, monkeypatch):
    """An upstream non-200 is reported to the client as text, and the slot still comes back."""
    set_start_result(monkeypatch, (True, 8081, "Started"))
    baseline = coordinator.active_inference_requests

    class FailingClient(FakeAsyncClient):
        # 500, not 503. 503 is the engine saying "not ready yet", which is now answered with
        # a retry nudge rather than an alert - this test is about a genuine failure.
        status_code = 500
        chunks = [b'{"error": {"message": "context shift failed"}}']

    monkeypatch.setattr(
        coordinator, "httpx",
        types.SimpleNamespace(AsyncClient=FailingClient,
                              Timeout=lambda *args, **kwargs: None,
                              Limits=lambda *args, **kwargs: None),
    )

    async def scenario():
        response = await coordinator.chat_completions(
            FakeRequest({"model": "phi3-mini",
                         "messages": [{"role": "user", "content": "hello"}]})
        )
        chunks = await drain(response)
        assert b"System Alert" in b"".join(chunks)
        await asyncio.sleep(0)

    asyncio.run(scenario())

    assert coordinator.active_inference_requests == baseline


def test_a_loading_engine_is_a_retry_nudge_and_not_an_alert(counter_env, monkeypatch):
    """
    THE BUG: llama.cpp answers 503 for the entire time a model is being read off disk, and
    every non-200 was funnelled into one branch prefixed "[System Alert] Error from internal
    engine". Choosing a model is the most ordinary thing a user does, and for the minutes it
    then takes to load - longer when the weights sit on a network share - every message they
    sent was answered with the same words a crashed engine produces, and nothing suggested
    that waiting was the remedy.
    """
    set_start_result(monkeypatch, (True, 8081, "Started"))
    baseline = coordinator.active_inference_requests

    class LoadingClient(FakeAsyncClient):
        status_code = 503
        chunks = [b'{"error": {"message": "Loading model"}}']

    monkeypatch.setattr(
        coordinator, "httpx",
        types.SimpleNamespace(AsyncClient=LoadingClient,
                              Timeout=lambda *args, **kwargs: None,
                              Limits=lambda *args, **kwargs: None),
    )

    async def scenario():
        response = await coordinator.chat_completions(
            FakeRequest({"model": "phi3-mini",
                         "messages": [{"role": "user", "content": "hello"}]})
        )
        body = b"".join(await drain(response))
        assert b"System Alert" not in body
        assert b"still loading" in body
        await asyncio.sleep(0)

    asyncio.run(scenario())

    # The slot must come back on this path too: a user retrying every few seconds during a
    # long load would otherwise exhaust the pool before the model ever finished.
    assert coordinator.active_inference_requests == baseline


def test_the_slot_is_released_exactly_once_per_request(counter_env, monkeypatch):
    """
    Both the handler's ``finally`` and the generator's ``finally`` can run for the same
    request. The release is idempotent, so a completed stream must decrement once — never
    twice, which would under-count in-flight work and, before the clamp, drive the counter
    negative.
    """
    set_start_result(monkeypatch, (True, 8081, "Started"))
    coordinator.active_inference_requests = 5
    baseline = coordinator.active_inference_requests

    async def scenario():
        response = await coordinator.chat_completions(
            FakeRequest({"model": "phi3-mini",
                         "messages": [{"role": "user", "content": "hello"}]})
        )
        await drain(response)
        # Draining an exhausted generator again must not decrement a second time.
        assert await drain(response) == []
        await asyncio.sleep(0)

    asyncio.run(scenario())

    assert coordinator.active_inference_requests == baseline


# ======================================================================================
# ITEM 1.5 REGRESSION — a client that disconnects mid-stream must not leak its slot
# ======================================================================================
#
# The counter's release lives in a generator's ``finally``, and a generator's ``finally``
# only runs when the generator is resumed or closed. When the client disconnects, Starlette
# cancels the task consuming the generator; if that cancellation lands while the generator is
# parked on a bare ``yield`` — which every pair of adjacent yields with no ``await`` between
# them invites — nothing ever resumes it and the slot is gone for the life of the process.
#
# These tests drive the returned ``StreamingResponse`` as a real ASGI app, with a ``send``
# that parks forever on the first body chunk (a client that stopped reading, so the socket
# buffer is full) and a ``receive`` that then reports ``http.disconnect``. That is exactly
# what uvicorn does on a dropped connection. Measured against the pre-fix shape this leaked
# 8 of 8 slots and never recovered; the fix attaches the release to the response itself via
# ``BackgroundTask``, which Starlette awaits after its task group absorbs the scoped cancel.

# spec_version < 2.4 selects Starlette's task-group path, which is what uvicorn's HTTP
# protocol advertises ("2.3" in both h11_impl.py and httptools_impl.py).
ASGI_SCOPE = {
    "type": "http",
    "asgi": {"version": "3.0", "spec_version": "2.3"},
    "method": "POST",
    "path": "/v1/chat/completions",
    "headers": [],
}


async def abandon_mid_stream(response):
    """
    Run one response as an ASGI app and disconnect the client on its first body chunk.

    NOTE FOR ANYONE EDITING THE CALLERS: they deliberately keep every response object alive
    until after the assertion. Dropping the last reference lets CPython's async-generator
    finaliser close the abandoned generator, which runs its ``finally`` and hides the leak —
    so a test that discards the response would pass against the *broken* code. Under uvicorn
    the cancelled streaming task, its traceback and the connection's own bookkeeping all keep
    the response reachable, so "still referenced" is the production case, not a contrivance.
    """
    first_chunk_sent = asyncio.Event()
    disconnect_now = asyncio.Event()
    never = asyncio.Event()

    async def send(message):
        if message["type"] == "http.response.body":
            first_chunk_sent.set()
            await never.wait()          # the parked socket write

    async def receive():
        await disconnect_now.wait()
        return {"type": "http.disconnect"}

    task = asyncio.create_task(response(ASGI_SCOPE, receive, send))
    await first_chunk_sent.wait()
    disconnect_now.set()
    await task


def test_streaming_response_carries_a_background_release(counter_env, monkeypatch):
    """
    The mechanism itself, asserted directly.

    Everything below depends on the release being attached to the *response* rather than only
    to the generator, so if someone removes ``background=`` the failure should name that
    rather than showing up as a mysterious counter drift.
    """
    set_start_result(monkeypatch, (True, 8081, "Started"))

    async def scenario():
        response = await coordinator.chat_completions(
            FakeRequest({"model": "phi3-mini", "messages": [{"role": "user", "content": "hi"}]})
        )
        assert response.background is not None, "StreamingResponse has no BackgroundTask"
        await drain(response)
        await asyncio.sleep(0)

    asyncio.run(scenario())


def test_slot_is_returned_when_the_client_aborts_the_proxied_stream(counter_env, monkeypatch):
    """
    ITEM 1.5, the reported case: eight clients that vanish mid-generation.

    Pre-fix this left the counter eight above its baseline permanently, which drove it past
    the viral-traffic threshold of 12 and made the coordinator tear down and reboot the engine
    on every subsequent completion.
    """
    set_start_result(monkeypatch, (True, 8081, "Started"))
    coordinator.active_inference_requests = 0

    async def scenario():
        # Held for the whole scenario on purpose — see abandon_mid_stream's docstring.
        abandoned = []
        for _ in range(8):
            response = await coordinator.chat_completions(
                FakeRequest({"model": "phi3-mini",
                             "messages": [{"role": "user", "content": "hi"}]})
            )
            abandoned.append(response)
            await abandon_mid_stream(response)
        gc.collect()
        await asyncio.sleep(0)
        assert coordinator.active_inference_requests == 0
        abandoned.clear()

    asyncio.run(scenario())

    assert coordinator.active_inference_requests == 0


def test_slot_is_returned_when_the_client_aborts_the_maxed_out_nudge(counter_env, monkeypatch):
    """
    The same abort against ``reject_generator`` — the branch the audit quoted, whose two
    adjacent yields with no await between them are the shape that makes the leak likely.
    """
    set_start_result(monkeypatch, (False, 0, "Network Maxed Out"))
    coordinator.active_inference_requests = 0

    async def scenario():
        abandoned = []
        for _ in range(8):
            response = await coordinator.chat_completions(
                FakeRequest({"model": "phi3-mini",
                             "messages": [{"role": "user", "content": "hi"}]})
            )
            abandoned.append(response)
            await abandon_mid_stream(response)
        gc.collect()
        await asyncio.sleep(0)
        assert coordinator.active_inference_requests == 0
        abandoned.clear()

    asyncio.run(scenario())

    assert coordinator.active_inference_requests == 0


def test_an_aborted_stream_is_not_double_released(counter_env, monkeypatch):
    """
    Both the generator's ``finally`` and the response's ``BackgroundTask`` may run for the
    same request. Exactly one decrement must happen, or the counter under-reports genuine
    in-flight work — and before the clamp it went negative and disabled the recalculation
    permanently.
    """
    set_start_result(monkeypatch, (True, 8081, "Started"))
    coordinator.active_inference_requests = 5
    baseline = coordinator.active_inference_requests

    async def scenario():
        response = await coordinator.chat_completions(
            FakeRequest({"model": "phi3-mini", "messages": [{"role": "user", "content": "hi"}]})
        )
        await abandon_mid_stream(response)
        assert coordinator.active_inference_requests == baseline
        # Drive the background task a second time by hand: it must be a no-op.
        await response.background()
        await asyncio.sleep(0)

    asyncio.run(scenario())

    assert coordinator.active_inference_requests == baseline


def test_a_failed_decrement_leaves_the_slot_reclaimable(counter_env, monkeypatch):
    """
    ITEM 1.6: the released flag must not be stranded by a failing decrement.

    Marking the slot released *before* awaiting the decrement, and leaving it marked when the
    await raised, converted a retryable leak into a permanent one — the slot was recorded as
    returned while the counter still held it, and no later caller would try again. The first
    call here fails, the second (the BackgroundTask) succeeds.
    """
    set_start_result(monkeypatch, (True, 8081, "Started"))
    coordinator.active_inference_requests = 0

    real_change = coordinator._change_inference_count
    calls = {"count": 0}

    async def flaky_change(delta):
        # Fail only the first decrement; increments and later decrements behave normally.
        if delta < 0:
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("transient counter failure")
        return await real_change(delta)

    async def scenario():
        response = await coordinator.chat_completions(
            FakeRequest({"model": "phi3-mini", "messages": [{"role": "user", "content": "hi"}]})
        )
        assert coordinator.active_inference_requests == 1

        monkeypatch.setattr(coordinator, "_change_inference_count", flaky_change)

        # The generator's finally is the first releaser and it blows up...
        with pytest.raises(RuntimeError):
            await drain(response)
        assert coordinator.active_inference_requests == 1, "slot should still be held"

        # ...and the response's BackgroundTask is still able to reclaim the slot.
        await response.background()
        assert coordinator.active_inference_requests == 0

    asyncio.run(scenario())

    assert coordinator.active_inference_requests == 0


# ======================================================================================
# ITEM 1.7 — one VRAM-progress reader per (model, port), not one per request
# ======================================================================================

def test_vram_progress_readers_are_not_spawned_per_request(counter_env, monkeypatch):
    """
    ``astart_model`` returns success for "Already running" too, so a busy model used to get a
    fresh SSE reader — and a fresh open connection to llama-server — on every chat request.
    Since background tasks are now held by strong reference, neither the task set nor the
    connections ever shrank.
    """
    monkeypatch.setattr(coordinator, "_vram_progress_streams", {})
    started = []

    async def counting_progress(port, model_name):
        started.append((model_name, port))
        # Stay alive for the duration of the test so the entry counts as "live".
        await asyncio.sleep(3600)

    monkeypatch.setattr(coordinator, "stream_vram_progress_async", counting_progress)

    async def scenario():
        for _ in range(10):
            coordinator.spawn_vram_progress_task(8081, "phi3-mini")
        # A different engine legitimately gets its own reader.
        coordinator.spawn_vram_progress_task(8082, "llama3.1-8b")
        await asyncio.sleep(0)

        assert started == [("phi3-mini", 8081), ("llama3.1-8b", 8082)]

        # Do not leave the sleeping tasks behind for the next test's event loop.
        for task in list(coordinator._vram_progress_streams.values()):
            task.cancel()
        await asyncio.gather(*coordinator._vram_progress_streams.values(),
                             return_exceptions=True)

    asyncio.run(scenario())


# ======================================================================================
# ITEM 1.1 — a present-but-null "gpu" section must not become an HTTP 500
# ======================================================================================

def test_heartbeat_accepts_a_null_gpu_section(monkeypatch):
    """
    ITEM 1.1 REGRESSION: ``POST /worker/heartbeat`` with ``{"gpu": null}``.

    ``hw.get("gpu", {}).get("vram_total_gb", 0)`` looks safe and is not: a ``dict.get``
    default only applies when the key is *absent*, so a present JSON ``null`` comes back as
    ``None`` and the chained ``.get`` raises ``AttributeError``. Step 3 of the handler
    (``validate_hardware_profile``) deliberately *permits* ``gpu: null`` for CPU-only nodes,
    so step 4 crashed on input that had just been declared valid — an unauthenticated remote
    500 and log flood whenever ``ALLOW_UNSIGNED_WORKERS`` is on.
    """
    monkeypatch.setattr(coordinator, "get_worker_limits", lambda: {
        "network_key": "",
        "allow_unsigned_workers": True,
        "trust_proxy_headers": False,
    })
    monkeypatch.setattr(coordinator.registry, "workers", {})

    payload = coordinator.HeartbeatPayload(
        worker_id="cpu-node",
        version=VERSION,
        port=50052,
        token="tok",
        hardware={"gpu": None, "ram_gb": 32.0, "cpu_cores": 8, "cpu_threads": 16,
                  "cpu_arch": "x86_64", "cpu_features": ["avx2", "fma"], "mem_bandwidth_gbs": 45.0,
                  "is_yielding": False},
    )

    result = asyncio.run(coordinator.worker_heartbeat(FakeRequest(client_host="10.0.0.5"),
                                                      payload))

    assert result == {"status": "ok"}
    assert "cpu-node" in coordinator.registry.workers


def test_a_worker_signed_heartbeat_verifies_end_to_end(monkeypatch):
    """
    ITEM 1.3 END-TO-END GUARD: what the Worker signs is what the Coordinator verifies.

    ``tests/test_authn.py`` proves the two canonicalisations agree in isolation. This proves
    they still agree after the body has crossed the real boundary: serialised by the Worker,
    parsed by FastAPI's ``HeartbeatPayload`` model (pydantic, not ``json.loads``), and handed
    to ``worker_heartbeat``. If a JSON round-trip or a pydantic coercion ever perturbed a
    value inside ``hardware``, the digest would change and *every* heartbeat in the fleet
    would come back 401 "invalid signature" — which reads as a NETWORK_KEY problem and is
    not one. Signing v2 without this test would be signing on trust.
    """
    network_key = "cluster-secret-for-this-test"
    monkeypatch.setattr(coordinator, "get_worker_limits", lambda: {
        "network_key": network_key,
        "allow_unsigned_workers": False,
        "trust_proxy_headers": False,
    })
    monkeypatch.setattr(coordinator.registry, "workers", {})
    monkeypatch.setattr(coordinator, "_nonce_cache", NonceCache())

    # --- Worker side: build and sign exactly as engine/worker.py does ------------------
    hardware = {
        "gpu": {"has_gpu": True, "vram_total_gb": 24.0, "name": "Test GPU",
                "load_percent": 5.25},
        "ram_gb": 64.0,
        "cpu_cores": 16,
        "cpu_threads": 32,
        "is_yielding": False,
    }
    timestamp = time.time()
    nonce = secrets.token_hex(16)
    body = {
        "worker_id": "signed-node",
        "version": VERSION,
        "port": 50052,
        "token": "tok-signed",
        "hardware": hardware,
        "timestamp": timestamp,
        "nonce": nonce,
        "signature": sign_heartbeat(network_key, "signed-node", 50052, "tok-signed",
                                    timestamp, nonce, VERSION, hardware),
    }

    # --- The wire: serialise, then parse through the real request model ---------------
    payload = coordinator.HeartbeatPayload.model_validate_json(json.dumps(body))

    # --- Coordinator side --------------------------------------------------------------
    result = asyncio.run(
        coordinator.worker_heartbeat(FakeRequest(client_host="10.0.0.11"), payload)
    )
    assert result == {"status": "ok"}
    assert "signed-node" in coordinator.registry.workers


def test_a_heartbeat_with_a_rewritten_hardware_profile_is_rejected(monkeypatch):
    """
    ITEM 1.3, the attack: heartbeats are plain HTTP and the threat model includes an on-path
    attacker. Under v1 they could inflate the reported VRAM without touching the signature
    and poison the memory pool the whole cluster sizes itself against.
    """
    network_key = "cluster-secret-for-this-test"
    monkeypatch.setattr(coordinator, "get_worker_limits", lambda: {
        "network_key": network_key,
        "allow_unsigned_workers": False,
        "trust_proxy_headers": False,
    })
    monkeypatch.setattr(coordinator.registry, "workers", {})
    monkeypatch.setattr(coordinator, "_nonce_cache", NonceCache())

    hardware = {"gpu": {"has_gpu": True, "vram_total_gb": 8.0}, "ram_gb": 32.0,
                "cpu_cores": 8}
    timestamp = time.time()
    nonce = secrets.token_hex(16)
    signature = sign_heartbeat(network_key, "victim", 50052, "tok", timestamp, nonce,
                               VERSION, hardware)

    # The attacker rewrites the profile in flight and leaves the MAC alone.
    tampered = json.loads(json.dumps(hardware))
    tampered["gpu"]["vram_total_gb"] = 480.0

    payload = coordinator.HeartbeatPayload(
        worker_id="victim", version=VERSION, port=50052, token="tok",
        hardware=tampered, timestamp=timestamp, nonce=nonce, signature=signature,
    )

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(coordinator.worker_heartbeat(FakeRequest(client_host="10.0.0.12"),
                                                 payload))

    assert excinfo.value.status_code == 401
    assert "invalid signature" in excinfo.value.detail
    assert coordinator.registry.workers == {}


def test_a_heartbeat_with_a_rewritten_version_is_rejected(monkeypatch):
    """
    ITEM 1.3, the other attack: a version mismatch is answered 426, and the Worker's own 426
    handler shuts the node down. An unsigned ``version`` was a one-byte remote kill switch.

    The rewritten value here is still the *correct* version, so the 426 path is not taken and
    the failure can only come from the signature — which is the point.
    """
    network_key = "cluster-secret-for-this-test"
    monkeypatch.setattr(coordinator, "get_worker_limits", lambda: {
        "network_key": network_key,
        "allow_unsigned_workers": False,
        "trust_proxy_headers": False,
    })
    monkeypatch.setattr(coordinator.registry, "workers", {})
    monkeypatch.setattr(coordinator, "_nonce_cache", NonceCache())

    hardware = {"gpu": None, "ram_gb": 32.0, "cpu_cores": 8}
    timestamp = time.time()
    nonce = secrets.token_hex(16)
    # Signed while claiming a different build; replayed with the accepted one.
    signature = sign_heartbeat(network_key, "victim", 50052, "tok", timestamp, nonce,
                               "9.9.9-other", hardware)

    payload = coordinator.HeartbeatPayload(
        worker_id="victim", version=VERSION, port=50052, token="tok",
        hardware=hardware, timestamp=timestamp, nonce=nonce, signature=signature,
    )

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(coordinator.worker_heartbeat(FakeRequest(client_host="10.0.0.13"),
                                                 payload))

    assert excinfo.value.status_code == 401


def test_heartbeat_still_refuses_an_under_contributing_cpu_node(monkeypatch):
    """
    The null-gpu fix must not accidentally disable the contribution baseline: a CPU-only node
    with less than 8 GB of RAM or fewer than 2 cores is still a 403.
    """
    monkeypatch.setattr(coordinator, "get_worker_limits", lambda: {
        "network_key": "",
        "allow_unsigned_workers": True,
        "trust_proxy_headers": False,
    })
    monkeypatch.setattr(coordinator.registry, "workers", {})

    payload = coordinator.HeartbeatPayload(
        worker_id="tiny-node",
        version=VERSION,
        port=50052,
        token="tok",
        hardware={"gpu": None, "ram_gb": 2.0, "cpu_cores": 1},
    )

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(coordinator.worker_heartbeat(FakeRequest(client_host="10.0.0.6"), payload))

    assert excinfo.value.status_code == 403


# ======================================================================================
# ITEM 1.2 — X-Forwarded-For must be read from the RIGHT-hand end
# ======================================================================================

def trusting_limits(**overrides):
    """Config view with the proxy opt-in switched on."""
    limits = {"trust_proxy_headers": True, "network_key": "", "allow_unsigned_workers": True}
    limits.update(overrides)
    return limits


def test_forwarded_for_is_ignored_unless_the_operator_opted_in(monkeypatch):
    """``TRUST_PROXY_HEADERS`` defaults to false, and the header must then be inert."""
    monkeypatch.setattr(coordinator, "get_worker_limits", lambda: {"trust_proxy_headers": False})

    request = FakeRequest(headers={"X-Forwarded-For": "127.0.0.1"}, client_host="198.51.100.9")
    assert coordinator.get_client_ip(request) == "198.51.100.9"


def test_forwarded_for_takes_the_rightmost_entry(monkeypatch):
    """
    ITEM 1.2 REGRESSION, the exact attack.

    With the canonical nginx directive ``proxy_set_header X-Forwarded-For
    $proxy_add_x_forwarded_for``, a proxy *appends*. An attacker who sends
    ``X-Forwarded-For: 127.0.0.1`` therefore produces ``127.0.0.1, <their real ip>``, and
    reading the LEFT-most entry — which this function used to do, contradicting its own
    comment — returned the attacker's forgery. That yielded ``is_local_ip() -> True``, a
    complete Non-Contrib bypass, plus arbitrary RPC-address injection through
    ``registry.update_worker(..., client_ip, ...)``.
    """
    monkeypatch.setattr(coordinator, "get_worker_limits", trusting_limits)
    monkeypatch.setattr(coordinator, "get_config", lambda: {})

    request = FakeRequest(headers={"X-Forwarded-For": "127.0.0.1, 198.51.100.9"},
                          client_host="10.0.0.1")
    assert coordinator.get_client_ip(request) == "198.51.100.9"


def test_a_direct_internet_caller_cannot_choose_its_address(monkeypatch):
    """
    The coordinator stays open on its own port beside the HTTPS proxy. A caller reaching it
    straight from the internet must be identified by its socket, whatever it forwards.
    """
    monkeypatch.setattr(coordinator, "get_worker_limits", trusting_limits)
    monkeypatch.setattr(coordinator, "get_config", lambda: {})

    request = FakeRequest(headers={"X-Forwarded-For": "100.64.1.2"}, client_host="1.1.1.1")
    assert coordinator.get_client_ip(request) == "1.1.1.1"


@pytest.mark.parametrize("proxy", ["127.0.0.1", "::1", "192.168.40.23", "10.13.13.1"])
def test_a_proxy_on_this_host_or_the_lan_is_believed(monkeypatch, proxy):
    monkeypatch.setattr(coordinator, "get_worker_limits", trusting_limits)
    monkeypatch.setattr(coordinator, "get_config", lambda: {})

    request = FakeRequest(headers={"X-Forwarded-For": "1.1.1.1"}, client_host=proxy)
    assert coordinator.get_client_ip(request) == "1.1.1.1"


def test_forwarded_for_honours_the_configured_hop_count(monkeypatch):
    """
    Two trusted proxies means the address we want is the second from the right; everything
    to its left was written by something we do not control.
    """
    monkeypatch.setattr(coordinator, "get_worker_limits", trusting_limits)
    monkeypatch.setattr(coordinator, "get_config", lambda: {"TRUSTED_PROXY_HOPS": "2"})

    request = FakeRequest(
        headers={"X-Forwarded-For": "127.0.0.1, 198.51.100.9, 192.0.2.50"},
        client_host="10.0.0.1",
    )
    assert coordinator.get_client_ip(request) == "198.51.100.9"


@pytest.mark.parametrize("configured", ["0", "-3", "not-a-number", ""])
def test_a_broken_hop_count_falls_back_to_one_hop(monkeypatch, configured):
    """A typo in config.txt must not break client identification for every request."""
    monkeypatch.setattr(coordinator, "get_worker_limits", trusting_limits)
    monkeypatch.setattr(coordinator, "get_config",
                        lambda: {"TRUSTED_PROXY_HOPS": configured})

    request = FakeRequest(headers={"X-Forwarded-For": "127.0.0.1, 198.51.100.9"},
                          client_host="10.0.0.1")
    assert coordinator.get_client_ip(request) == "198.51.100.9"


def test_a_shorter_chain_than_configured_fails_closed(monkeypatch):
    """
    FINDING R3-3 REGRESSION: an anomalous chain must be discarded, not partially believed.

    The first fix clamped — ``index = max(0, len(parts) - hops)`` — and returned
    ``parts[0]``. With the default single hop and a one-entry header that is the *left-most*
    entry, i.e. the fully client-controlled value that reading from the right exists to stop
    trusting: it failed OPEN, and re-opened the identical localhost bypass.

    A chain shorter than the configured hop count means the request never traversed the
    proxies we were told about — DLNE binds 0.0.0.0, so anyone who can reach port 11435
    directly rather than via nginx produces exactly this shape. The header is therefore
    ignored entirely and the real transport peer is used.
    """
    monkeypatch.setattr(coordinator, "get_worker_limits", trusting_limits)
    monkeypatch.setattr(coordinator, "get_config", lambda: {"TRUSTED_PROXY_HOPS": "5"})

    request = FakeRequest(headers={"X-Forwarded-For": "198.51.100.9"}, client_host="10.0.0.1")
    assert coordinator.get_client_ip(request) == "10.0.0.1"


def test_a_forged_short_chain_cannot_claim_to_be_localhost(monkeypatch):
    """
    FINDING R3-3, the attack itself, and the consequence it buys.

    Two trusted proxies are configured, so a genuinely proxied request always carries at
    least two entries. A caller who reaches port 11435 *directly* — which DLNE's default
    ``0.0.0.0`` bind allows — supplies a one-entry header of their own choosing. The clamping
    version returned ``parts[0]``, i.e. the forgery, so ``X-Forwarded-For: 127.0.0.1`` made
    ``is_local_ip()`` true: a complete Non-Contrib bypass, plus arbitrary RPC-address
    injection through ``registry.update_worker(..., client_ip, ...)``.

    A one-entry chain against ONE configured hop is the normal shape nginx produces for a
    client that sent no header of its own, so the length rule alone cannot reject it — that
    case is covered by :func:`test_a_header_can_never_promote_a_remote_caller_to_local`.
    """
    monkeypatch.setattr(coordinator, "get_worker_limits", trusting_limits)
    monkeypatch.setattr(coordinator, "get_config", lambda: {"TRUSTED_PROXY_HOPS": "2"})

    request = FakeRequest(headers={"X-Forwarded-For": "127.0.0.1"}, client_host="203.0.113.7")
    assert coordinator.get_client_ip(request) == "203.0.113.7"
    assert not coordinator.is_local_ip(coordinator.get_client_ip(request))


@pytest.mark.parametrize("claimed", ["127.0.0.1", "0.0.0.0", "localhost", "::1"])
def test_a_header_can_never_promote_a_remote_caller_to_local(monkeypatch, claimed):
    """
    FINDING R3-3, THE RESIDUAL — the finding's own measured case, on the DEFAULT hop count.

    The first round-3 fix only discarded chains *shorter* than TRUSTED_PROXY_HOPS. With the
    default of one hop a single-entry header is not short, so the exact scenario the finding
    measured — ``get_client_ip(X-Forwarded-For: '127.0.0.1')`` from a peer of ``9.9.9.9``
    returning ``'127.0.0.1'`` — still returned the forgery, and ``is_local_ip()`` still said
    True. That is the full Non-Contrib bypass plus RPC-address injection the finding is about.

    A local address is a privilege, not just an identity, so the header may re-attribute a
    request to a different *remote* address but may never promote a remote caller to a local
    one. Note the hop count here is the default 1, not the 2 the test above configures.
    """
    monkeypatch.setattr(coordinator, "get_worker_limits", trusting_limits)
    monkeypatch.setattr(coordinator, "get_config", lambda: {})

    request = FakeRequest(headers={"X-Forwarded-For": claimed}, client_host="9.9.9.9")
    assert coordinator.get_client_ip(request) == "9.9.9.9"
    assert not coordinator.is_local_ip(coordinator.get_client_ip(request))


def test_a_genuinely_local_peer_still_reads_as_local(monkeypatch):
    """
    The other direction of the same rule: the guard must not cost a real deployment anything.

    A reverse proxy running on this host reaches the coordinator over loopback, so its socket
    peer is already local and a forwarded ``127.0.0.1`` is consistent rather than a forgery.
    And a proxy forwarding a genuine remote client reports a non-local address, which the
    guard does not touch at all.
    """
    monkeypatch.setattr(coordinator, "get_worker_limits", trusting_limits)
    monkeypatch.setattr(coordinator, "get_config", lambda: {})

    same_host = FakeRequest(headers={"X-Forwarded-For": "127.0.0.1"}, client_host="127.0.0.1")
    assert coordinator.get_client_ip(same_host) == "127.0.0.1"
    assert coordinator.is_local_ip(coordinator.get_client_ip(same_host))

    real_client = FakeRequest(headers={"X-Forwarded-For": "203.0.113.7"}, client_host="127.0.0.1")
    assert coordinator.get_client_ip(real_client) == "203.0.113.7"


# ======================================================================================
# FINDING R3-4 — is_local_ip must never reach a DNS resolver
# ======================================================================================

@pytest.mark.parametrize("not_an_address", [
    "this-name-does-not-exist-dlne-audit.invalid",
    "a" * 200 + ".example.com",
    "evil.example.com",
    "",
    "1.2.3.4.5",
    "not an ip at all",
])
def test_is_local_ip_never_resolves_a_hostname(monkeypatch, not_an_address):
    """
    FINDING R3-4 REGRESSION.

    ``socket.bind((host, 0))`` accepts a hostname and performs a blocking ``getaddrinfo`` when
    it gets one. The argument comes from ``get_client_ip``, which under ``TRUST_PROXY_HEADERS``
    returns an arbitrary token from a request header, and both call sites that matter — the
    Non-Contrib middleware's ``dispatch`` and the WebSocket handler — are coroutines. So a
    hostname whose resolver *drops* rather than refuses parked the whole event loop for the
    full DNS timeout, once per request. (An NXDOMAIN was measured at ~20 ms, which already
    proves the round trip happens.)

    The guard asserted here is structural, not statistical: any name resolution at all is a
    failure, so ``socket.getaddrinfo`` is booby-trapped rather than timed.
    """
    def exploding_getaddrinfo(*args, **kwargs):
        raise AssertionError("is_local_ip performed a DNS lookup on attacker-supplied text")

    monkeypatch.setattr(coordinator.socket, "getaddrinfo", exploding_getaddrinfo)

    assert coordinator.is_local_ip(not_an_address) is False


def test_is_local_ip_still_recognises_loopback_and_rejects_a_remote_literal(monkeypatch):
    """The behaviour the guard protects must survive it: real literals are still classified."""
    def exploding_getaddrinfo(*args, **kwargs):
        raise AssertionError("a valid IP literal must not need resolving either")

    monkeypatch.setattr(coordinator.socket, "getaddrinfo", exploding_getaddrinfo)

    assert coordinator.is_local_ip("127.0.0.1") is True
    assert coordinator.is_local_ip("localhost") is True
    assert coordinator.is_local_ip("::1") is True
    # A routable address that is not on this machine cannot be bound to.
    assert coordinator.is_local_ip("203.0.113.7") is False


# ======================================================================================
# FINDING R3-2 — the 426 kill switch must sit BEHIND the signature check
# ======================================================================================

def signed_limits(network_key):
    """Config view with a network key configured and unsigned workers refused."""
    return lambda: {
        "network_key": network_key,
        "allow_unsigned_workers": False,
        "trust_proxy_headers": False,
    }


def test_a_version_rewritten_in_flight_is_401_not_426(monkeypatch):
    """
    FINDING R3-2 REGRESSION, the exact measured attack.

    ``engine/worker.py`` answers HTTP 426 with
    ``request_shutdown(FATAL_EXIT_CODE, "Coordinator rejected this build as out of date")`` —
    the node stops. Running the version comparison at step 1, above the signature check, left
    that kill switch reachable by anyone on the path: heartbeats are plain HTTP, and rewriting
    the single ``version`` field took a node offline. Wire format v2 folded ``version`` into
    the MAC for exactly this reason, but a MAC verified *after* the field has been acted on
    protects nothing.

    Here the worker signs a genuine heartbeat over the true VERSION and an attacker rewrites
    only the on-the-wire value. The pre-fix answer was 426; it must now be 401.
    """
    network_key = "cluster-secret-for-this-test"
    monkeypatch.setattr(coordinator, "get_worker_limits", signed_limits(network_key))
    monkeypatch.setattr(coordinator.registry, "workers", {})
    monkeypatch.setattr(coordinator, "_nonce_cache", NonceCache())

    hardware = {"gpu": None, "ram_gb": 32.0, "cpu_cores": 8}
    timestamp = time.time()
    nonce = secrets.token_hex(16)
    # A completely legitimate worker on the current build.
    signature = sign_heartbeat(network_key, "victim", 50052, "tok", timestamp, nonce,
                               VERSION, hardware)

    # ...whose version field is flipped to junk by an on-path attacker.
    payload = coordinator.HeartbeatPayload(
        worker_id="victim", version="junk", port=50052, token="tok",
        hardware=hardware, timestamp=timestamp, nonce=nonce, signature=signature,
    )

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(coordinator.worker_heartbeat(FakeRequest(client_host="10.0.0.14"), payload))

    assert excinfo.value.status_code == 401, "the 426 kill switch is still forgeable"
    assert coordinator.registry.workers == {}


def test_a_genuinely_outdated_worker_still_gets_426(monkeypatch):
    """
    The upgrade path must survive the reordering.

    A real old build signs its heartbeat over its own true version, so the MAC verifies and
    execution reaches the version comparison — which still answers 426 and still drives the
    worker to upgrade. That is the whole reason the check was kept rather than removed.
    """
    network_key = "cluster-secret-for-this-test"
    monkeypatch.setattr(coordinator, "get_worker_limits", signed_limits(network_key))
    monkeypatch.setattr(coordinator.registry, "workers", {})
    monkeypatch.setattr(coordinator, "_nonce_cache", NonceCache())

    hardware = {"gpu": None, "ram_gb": 32.0, "cpu_cores": 8}
    timestamp = time.time()
    nonce = secrets.token_hex(16)
    old_version = "0.0.1-ancient"
    signature = sign_heartbeat(network_key, "oldnode", 50052, "tok", timestamp, nonce,
                               old_version, hardware)

    payload = coordinator.HeartbeatPayload(
        worker_id="oldnode", version=old_version, port=50052, token="tok",
        hardware=hardware, timestamp=timestamp, nonce=nonce, signature=signature,
    )

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(coordinator.worker_heartbeat(FakeRequest(client_host="10.0.0.15"), payload))

    assert excinfo.value.status_code == 426
    assert VERSION in excinfo.value.detail


def test_a_markup_injecting_worker_id_does_not_break_the_heartbeat(monkeypatch):
    """
    FINDING R3-1, the coordinator half.

    ``worker_id`` and ``version`` are unauthenticated attacker-controlled strings that this
    handler interpolates into ``log_error``/``log_warning`` — and ``engine.logger`` renders
    through rich with markup enabled, where a stray closing tag raises ``MarkupError`` out of
    the handler. The rejection must stay a clean 426/403/401, never a 500.
    """
    monkeypatch.setattr(coordinator, "get_worker_limits", lambda: {
        "network_key": "",
        "allow_unsigned_workers": True,
        "trust_proxy_headers": False,
    })
    monkeypatch.setattr(coordinator.registry, "workers", {})
    monkeypatch.setattr(coordinator, "_unsigned_worker_warnings", set())

    payload = coordinator.HeartbeatPayload(
        worker_id="[/dim][bold red]ADMIN[/",
        version="[/",
        port=50052,
        token="tok",
        hardware={"gpu": None, "ram_gb": 32.0, "cpu_cores": 8},
    )

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(coordinator.worker_heartbeat(FakeRequest(client_host="10.0.0.16"), payload))

    assert excinfo.value.status_code == 426


# ======================================================================================
# FINDINGS R3-6, R3-9, R3-11 — the chat handler's dealings with the InferenceManager
# ======================================================================================

def set_snapshot(monkeypatch, entry):
    """Makes ``manager.asnapshot_model`` return a fixed entry (or None)."""
    async def fake_asnapshot_model(model_path):
        return entry

    monkeypatch.setattr(coordinator.manager, "asnapshot_model", fake_asnapshot_model)


def test_a_vanished_engine_is_answered_with_a_nudge_not_a_dead_proxy(counter_env, monkeypatch):
    """
    FINDING R3-9 REGRESSION.

    ``proc = manager.running_models.get(model_path, {}).get("process")`` was an unsynchronised
    read from the event loop, and BOTH the 120-second readiness loop and the
    ``if not engine_ready: raise 504`` guard were nested inside ``if proc:``. So when a
    concurrent ``astop_all_models``/``reap_model`` had drained the entry in the gap, ``proc``
    came back None, the whole block was skipped *without raising*, and the request was proxied
    to a port with no listener. The "Already running" return path is the easiest way in,
    because it does no spawning at all.

    The entry being gone is now an explicit case: it means a teardown won the race, which is
    the same class of event as a retryable start failure and gets the same graceful answer.
    """
    set_start_result(monkeypatch, (True, 8081, "Already running"))
    set_snapshot(monkeypatch, None)

    proxied = []

    class RecordingClient(FakeAsyncClient):
        def stream(self, method, url, **kwargs):
            proxied.append(url)
            return FakeStreamContext(FakeStreamResponse(self.chunks))

    monkeypatch.setattr(coordinator, "httpx",
                        types.SimpleNamespace(RecordingClient=RecordingClient,
                                              AsyncClient=RecordingClient,
                                              Timeout=lambda *a, **k: None,
                                              Limits=lambda *a, **k: None))
    baseline = coordinator.active_inference_requests

    async def scenario():
        response = await coordinator.chat_completions(
            FakeRequest({"model": "phi3-mini", "messages": [{"role": "user", "content": "hi"}]})
        )
        assert isinstance(response, StreamingResponse)
        chunks = b"".join(await drain(response))
        assert b"reorganising" in chunks, "the user should be told to retry, not silently 502"

    asyncio.run(scenario())

    assert proxied == [], "nothing may be proxied to an engine that no longer exists"
    assert coordinator.active_inference_requests == baseline


def test_context_sizing_uses_the_engines_real_values(counter_env, monkeypatch):
    """
    FINDING R3-9, the quieter half.

    The second unsynchronised read yielded ``{}`` whenever the entry had gone, so the handler
    silently fell back to ``safe_context=8192`` / ``dynamic_slots=4`` instead of the engine's
    real ``-np`` value. With a real 2 slots that made ``slot_context`` 2048 rather than 4096
    and truncated the user's prompt to half the budget it actually had. One coherent snapshot
    is now taken and used for the readiness check *and* the sizing.
    """
    set_start_result(monkeypatch, (True, 8081, "Started"))
    set_snapshot(monkeypatch, running_engine_entry(safe_context=8192, dynamic_slots=2))

    sent_bodies = []

    class RecordingClient(FakeAsyncClient):
        def stream(self, method, url, **kwargs):
            sent_bodies.append(kwargs.get("json"))
            return FakeStreamContext(FakeStreamResponse(self.chunks))

    monkeypatch.setattr(coordinator, "httpx",
                        types.SimpleNamespace(AsyncClient=RecordingClient,
                                              Timeout=lambda *a, **k: None,
                                              Limits=lambda *a, **k: None))

    # With slot_context = 8192 // 2 = 4096 the allowance is 3584 tokens (~10752 chars), so a
    # 9000-character message fits untouched. Under the 8192 // 4 = 2048 fallback the allowance
    # would be 1536 tokens (~4608 chars) and the text would be sliced.
    long_message = "x" * 9000

    async def scenario():
        response = await coordinator.chat_completions(
            FakeRequest({"model": "phi3-mini",
                         "messages": [{"role": "user", "content": long_message}]})
        )
        await drain(response)
        await asyncio.sleep(0)

    asyncio.run(scenario())

    assert sent_bodies, "the request should have been proxied"
    forwarded = sent_bodies[0]["messages"][-1]["content"]
    assert forwarded == long_message, "the prompt was truncated against the wrong slot budget"


def test_a_crashed_engine_is_reaped_through_the_async_wrapper(counter_env, monkeypatch):
    """
    FINDING R3-6 REGRESSION.

    ``manager.reap_model(model_path)`` was called with no ``await`` and no
    ``asyncio.to_thread`` from inside the async chat handler. It takes the manager's
    ``_state_lock``, so the entire uvicorn event loop froze for as long as that lock was
    contended — measured at 11.81 s behind one ``stop_all_models`` teardown, during which no
    request, WebSocket broadcast or worker heartbeat was served by the process.

    The synchronous entry point is booby-trapped here: reaching it from a coroutine is the
    defect, so the test fails loudly rather than merely being slow.
    """
    set_start_result(monkeypatch, (True, 8081, "Started"))
    set_snapshot(monkeypatch, running_engine_entry(alive=False))

    def forbidden_sync_reap(model_path):
        raise AssertionError("reap_model was called synchronously from the event loop")

    reaped = []

    async def recording_areap(model_path):
        reaped.append(model_path)
        return True

    monkeypatch.setattr(coordinator.manager, "reap_model", forbidden_sync_reap)
    monkeypatch.setattr(coordinator.manager, "areap_model", recording_areap)
    baseline = coordinator.active_inference_requests

    async def scenario():
        response = await coordinator.chat_completions(
            FakeRequest({"model": "phi3-mini", "stream": True,
                         "messages": [{"role": "user", "content": "hi"}]})
        )
        # 2.6.2: a start that dies is the coordinator's own rebuild after a GPU left (2026-10-28 10:11), so the
        # client is told to send the step again ("engine_unavailable"), not handed a 500 that ends an agent's task.
        assert isinstance(response, StreamingResponse)
        body = b"".join(await drain(response))
        assert b"reorganising" in body and b'"dlne_error": "engine_unavailable"' in body
        # A caller that did not ask for a stream (an OpenAI client) gets a 503 with Retry-After, which it retries.
        with pytest.raises(HTTPException) as excinfo:
            await coordinator.chat_completions(
                FakeRequest({"model": "phi3-mini", "messages": [{"role": "user", "content": "hi"}]}))
        assert excinfo.value.status_code == 503 and excinfo.value.headers == {"Retry-After": "5"}

    asyncio.run(scenario())

    assert reaped == ["phi3-mini.gguf", "phi3-mini.gguf"]
    assert coordinator.active_inference_requests == baseline


def test_the_event_loop_keeps_running_while_a_crashed_engine_is_reaped(counter_env, monkeypatch):
    """
    FINDING R3-6, measured rather than asserted structurally.

    ``areap_model`` is driven onto a worker thread, so a reap that takes a second must not
    stop the loop for a second. A concurrent ticker counts how many times it got to run: with
    the synchronous call it could only be 1 (the loop was frozen), and with the wrapper it is
    many.
    """
    set_start_result(monkeypatch, (True, 8081, "Started"))
    set_snapshot(monkeypatch, running_engine_entry(alive=False))

    reap_duration = 0.4

    def slow_blocking_reap(model_path):
        time.sleep(reap_duration)     # stands in for a contended _state_lock
        return True

    async def areap(model_path):
        return await asyncio.to_thread(slow_blocking_reap, model_path)

    monkeypatch.setattr(coordinator.manager, "areap_model", areap)

    ticks = {"count": 0}

    async def ticker():
        while True:
            ticks["count"] += 1
            await asyncio.sleep(0.01)

    async def scenario():
        beat = asyncio.create_task(ticker())
        try:
            response = await coordinator.chat_completions(
                FakeRequest({"model": "phi3-mini", "stream": True,
                             "messages": [{"role": "user", "content": "hi"}]})
            )
            await drain(response)
        finally:
            beat.cancel()
            await asyncio.gather(beat, return_exceptions=True)

    asyncio.run(scenario())

    # ~40 ticks are expected in 0.4 s; anything comfortably above 1 proves the loop kept
    # running. The bound is loose on purpose so a slow CI machine cannot make it flaky.
    assert ticks["count"] > 5, "the event loop was blocked for the duration of the reap"


def test_a_cancelled_start_is_retried_before_it_becomes_an_error(counter_env, monkeypatch):
    """
    FINDING R3-11 REGRESSION, the happy resolution.

    ``START_CANCELLED_MESSAGE`` is returned whenever a concurrent ``stop_all_models`` marked
    the reservation cancelled — which a routine topology restart does every time a worker
    joins or leaves. The handler only special-cased "Network Maxed Out", so everything else
    fell through to ``raise HTTPException(500)`` and a user mid-request got a server error for
    a completely normal cluster event.
    """
    from engine.inference_manager import START_CANCELLED_MESSAGE

    attempts = []

    async def flaky_astart_model(*args, **kwargs):
        attempts.append(args[1])          # the endpoint list this attempt was given
        if len(attempts) == 1:
            return (False, 0, START_CANCELLED_MESSAGE)
        return (True, 8081, "Started")

    monkeypatch.setattr(coordinator.manager, "astart_model", flaky_astart_model)
    baseline = coordinator.active_inference_requests

    async def scenario():
        response = await coordinator.chat_completions(
            FakeRequest({"model": "phi3-mini", "messages": [{"role": "user", "content": "hi"}]})
        )
        chunks = b"".join(await drain(response))
        assert b"[DONE]" in chunks, "the retry should have produced a real answer"
        await asyncio.sleep(0)

    asyncio.run(scenario())

    assert len(attempts) == 2, "a retryable failure must be retried exactly once"
    assert coordinator.active_inference_requests == baseline


def test_a_start_cancelled_twice_is_a_nudge_not_a_500(counter_env, monkeypatch):
    """
    FINDING R3-11, the fallback.

    Losing the race twice is still not the user's fault and still not a server fault, so it
    gets the same in-band SSE treatment the "Network Maxed Out" path already used rather than
    an HTTP 500 that a chat client renders as a lost message.
    """
    from engine.inference_manager import START_HANDOFF_TIMEOUT_MESSAGE

    set_start_result(monkeypatch, (False, 0, START_HANDOFF_TIMEOUT_MESSAGE))
    baseline = coordinator.active_inference_requests

    async def scenario():
        response = await coordinator.chat_completions(
            FakeRequest({"model": "phi3-mini", "messages": [{"role": "user", "content": "hi"}]})
        )
        assert isinstance(response, StreamingResponse)
        chunks = b"".join(await drain(response))
        assert b"reorganising" in chunks

    asyncio.run(scenario())

    assert coordinator.active_inference_requests == baseline


def test_a_model_that_is_not_downloaded_yet_is_a_nudge_not_a_500(counter_env, monkeypatch):
    """
    The third graceful case, and the one a first-run cluster hits.

    ``can_admit`` now resolves the model path exactly as ``start_model`` does, so the
    coordinator's auto-deploy gate no longer even attempts a model whose weights are missing.
    A chat request can still race the download, and "still downloading" is not a 500 either.
    """
    from engine.inference_manager import model_not_on_disk_message

    set_start_result(monkeypatch, (False, 0, model_not_on_disk_message("phi3-mini")))
    baseline = coordinator.active_inference_requests

    async def scenario():
        response = await coordinator.chat_completions(
            FakeRequest({"model": "phi3-mini", "messages": [{"role": "user", "content": "hi"}]})
        )
        chunks = b"".join(await drain(response))
        assert b"finished downloading" in chunks

    asyncio.run(scenario())

    assert coordinator.active_inference_requests == baseline


def test_a_genuine_engine_failure_is_still_a_500(counter_env, monkeypatch):
    """
    The graceful paths must not swallow real faults.

    A missing binary, a bad launch argument or an unexpected exception text is a terminal
    failure an operator has to see, and it keeps its HTTP 500.
    """
    set_start_result(monkeypatch, (False, 0, "Binary Missing"))

    async def scenario():
        with pytest.raises(HTTPException) as excinfo:
            await coordinator.chat_completions(
                FakeRequest({"model": "phi3-mini",
                             "messages": [{"role": "user", "content": "hi"}]})
            )
        assert excinfo.value.status_code == 500

    asyncio.run(scenario())


# ======================================================================================
# FINDINGS R3-5 and R3-12 — the monitor loop must never block
# ======================================================================================
#
# monitor_workers_async is the only thing that prunes dead workers and pushes cluster
# telemetry to the WebUI. It used to `await manager.astart_model(...)` in its body, and that
# call parks on another thread's start of the same model for up to
# START_HANDOFF_TIMEOUT_SECONDS = 300 s. For the whole stall the loop's own
# `await asyncio.sleep(2)` was never reached: no pruning, no broadcasts, every live worker
# eventually timed out of the registry and the WebUI went stale — while nothing was wrong.

@pytest.fixture
def monitor_env(monkeypatch):
    """
    Isolates ``monitor_workers_async`` so one tick costs milliseconds instead of two seconds.

    Returns a dict of the recorders the tests assert on. Everything the loop touches is
    replaced: the registry getters, ``broadcast``, and every InferenceManager entry point.
    ``MONITOR_TICK_SECONDS`` is patched rather than ``asyncio.sleep`` so the real event loop
    semantics — including the interleaving these tests are entirely about — are untouched.
    """
    recorded = {"broadcasts": [], "prunes": 0, "starts": [], "estimates": 0}

    monkeypatch.setattr(coordinator, "MONITOR_TICK_SECONDS", 0.005)
    monkeypatch.setattr(coordinator, "_reconfiguration_task", None)
    monkeypatch.setattr(coordinator, "_background_tasks", set())

    monkeypatch.setattr(coordinator, "global_active_model", "phi3-mini")
    monkeypatch.setattr(coordinator, "cluster_status", "Waiting for Nodes")
    monkeypatch.setattr(coordinator, "is_swapping_models", False)
    monkeypatch.setattr(coordinator, "is_recalculating_queue", False)
    # Skip the seed-download branch: it is not what these tests are about.
    monkeypatch.setattr(coordinator, "has_pulled_seed", True)
    monkeypatch.setattr(coordinator, "is_downloading_seed", False)

    def counting_prune():
        recorded["prunes"] += 1

    monkeypatch.setattr(coordinator.registry, "prune_dead_workers", counting_prune)
    monkeypatch.setattr(coordinator.registry, "get_total_vram_gb", lambda: 24.0)
    monkeypatch.setattr(coordinator.registry, "get_total_sysram_gb", lambda: 32.0)
    monkeypatch.setattr(coordinator.registry, "get_total_cpu_cores", lambda: 8)
    monkeypatch.setattr(coordinator.registry, "get_total_cpu_threads", lambda: 16)
    monkeypatch.setattr(coordinator.registry, "count", lambda: 1)
    monkeypatch.setattr(coordinator.registry, "get_active_rpc_endpoints", lambda: ["10.0.0.2:50052"])
    # Since 1.1.0 the monitor asks the planner which endpoints a model of this size should
    # actually run on, rather than taking every registered one. The fixture answers with the
    # same single endpoint so these tests keep measuring what they were written to measure.
    monkeypatch.setattr(coordinator.registry, "plan_endpoints",
                        lambda required_gb, policy="auto": ["10.0.0.2:50052"])

    async def recording_broadcast(payload):
        recorded["broadcasts"].append(payload)

    monkeypatch.setattr(coordinator, "broadcast", recording_broadcast)

    async def aestimate(model_path):
        recorded["estimates"] += 1
        return 4.0

    monkeypatch.setattr(coordinator.manager, "aestimate_model_vram_gb", aestimate)

    def forbidden_sync_estimate(model_path):
        raise AssertionError("estimate_model_vram_gb was called on the event loop")

    monkeypatch.setattr(coordinator.manager, "estimate_model_vram_gb", forbidden_sync_estimate)

    def forbidden_sync_can_admit(*args, **kwargs):
        raise AssertionError("can_admit was called on the event loop")

    monkeypatch.setattr(coordinator.manager, "can_admit", forbidden_sync_can_admit)

    async def not_running(model_path):
        return None

    monkeypatch.setattr(coordinator.manager, "asnapshot_model", not_running)

    async def admissible(model_path, vram, sysram, endpoints=None):
        return (True, "ok")

    monkeypatch.setattr(coordinator.manager, "acan_admit", admissible)

    async def astop_all():
        return None

    monkeypatch.setattr(coordinator.manager, "astop_all_models", astop_all)

    async def no_vram_progress(port, model_name):
        return None

    monkeypatch.setattr(coordinator, "stream_vram_progress_async", no_vram_progress)
    monkeypatch.setattr(coordinator, "_vram_progress_streams", {})

    return recorded


async def run_monitor_for(seconds):
    """Run ``monitor_workers_async`` for a bounded window and cancel it cleanly."""
    task = asyncio.create_task(coordinator.monitor_workers_async())
    await asyncio.sleep(seconds)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def drain_background_tasks():
    """Cancel anything the loop spawned so it does not outlive the test's event loop."""
    pending = list(coordinator._background_tasks)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


def test_the_monitor_keeps_ticking_while_a_model_start_is_parked(monitor_env, monkeypatch):
    """
    FINDING R3-5 REGRESSION, the reported 300-second stall.

    ``astart_model`` here never returns — which is exactly what the handoff wait does when
    another thread is already starting the same model, bounded only by
    START_HANDOFF_TIMEOUT_SECONDS = 300 s. Pre-fix the loop body awaited it inline, so
    ``prune_dead_workers`` and both broadcasts stopped dead for the whole time and the counts
    below would be 1 and 0. They must now keep climbing.
    """
    never_returns = asyncio.Event()

    async def parked_astart_model(*args, **kwargs):
        await never_returns.wait()
        return (True, 8081, "Started")

    monkeypatch.setattr(coordinator.manager, "astart_model", parked_astart_model)

    async def scenario():
        await run_monitor_for(0.25)
        await drain_background_tasks()

    try:
        asyncio.run(scenario())
    finally:
        never_returns.set()

    assert monitor_env["prunes"] > 3, "the registry stopped being pruned during a slow start"
    assert len(monitor_env["broadcasts"]) > 6, "the WebUI stopped being updated during a slow start"


def test_only_one_reconfiguration_is_in_flight_at_a_time(monitor_env, monkeypatch):
    """
    A stalled deployment must not be re-spawned on every subsequent tick.

    The loop keeps running, so without the single-slot guard each of the ~50 ticks in this
    window would launch another cluster-wide start, all of them fighting over the same
    engines.
    """
    never_returns = asyncio.Event()
    starts = []

    async def parked_astart_model(*args, **kwargs):
        starts.append(args[0])
        await never_returns.wait()
        return (True, 8081, "Started")

    monkeypatch.setattr(coordinator.manager, "astart_model", parked_astart_model)

    async def scenario():
        await run_monitor_for(0.25)
        await drain_background_tasks()

    try:
        asyncio.run(scenario())
    finally:
        never_returns.set()

    assert starts == ["phi3-mini"], f"expected exactly one deployment, got {len(starts)}"


def test_the_monitor_estimates_model_memory_off_the_event_loop(monitor_env):
    """
    FINDING R3-12 REGRESSION.

    ``estimate_model_vram_gb`` stats the config file, realpaths every model search root and
    getsizes the weights. It was the one manager entry point round 2 left un-threaded, and it
    runs at 0.5 Hz forever — on a network-mounted models directory a single realpath stalls
    the whole coordinator. The synchronous version is booby-trapped by the fixture, so
    reaching it fails the test rather than merely being slow.
    """
    async def scenario():
        await run_monitor_for(0.1)
        await drain_background_tasks()

    asyncio.run(scenario())

    assert monitor_env["estimates"] > 0, "the loop never asked for the estimate at all"


def test_a_model_that_is_not_downloaded_yet_reads_as_awaiting_download(monitor_env, monkeypatch):
    """
    Agent-LOCK's hand-over item, and the death of the log-suppression hack.

    ``can_admit`` now resolves the model path exactly as ``start_model`` does, so a model
    whose weights are still downloading is refused by the gate rather than being announced as
    "Auto-Deploying" every two seconds and then failing inside ``start_model``. Rendered
    naively that refusal would read "Paused (Could not find absolute blob path for
    phi3-mini)"; the correct status — and what the old ``if "absolute blob path" not in msg``
    suppression was groping towards — is this one.
    """
    from engine.inference_manager import model_not_on_disk_message

    async def not_on_disk(model_path, vram, sysram, endpoints=None):
        return (False, model_not_on_disk_message(model_path))

    monkeypatch.setattr(coordinator.manager, "acan_admit", not_on_disk)

    def forbidden_start(*args, **kwargs):
        raise AssertionError("a model with no weights on disk must not be started")

    monkeypatch.setattr(coordinator.manager, "astart_model", forbidden_start)

    async def scenario():
        await run_monitor_for(0.05)
        await drain_background_tasks()

    asyncio.run(scenario())

    assert coordinator.cluster_status == "Paused (Awaiting Model Download)"


def test_a_memory_refusal_still_reads_as_a_memory_shortfall(monitor_env, monkeypatch):
    """The wording the WebUI expects for the ordinary "not enough hardware yet" case."""
    async def maxed_out(model_path, vram, sysram, endpoints=None):
        return (False, coordinator.NETWORK_MAXED_OUT_MESSAGE)

    monkeypatch.setattr(coordinator.manager, "acan_admit", maxed_out)

    async def scenario():
        await run_monitor_for(0.05)
        await drain_background_tasks()

    asyncio.run(scenario())

    assert coordinator.cluster_status.startswith("Paused (Needs ")


def test_the_monitor_passes_the_topology_to_the_admission_gate(monitor_env, monkeypatch):
    """
    A changed worker set forces a restart, and ``can_admit`` can only account for that
    restart's memory if it is told which endpoints the caller intends to use. Passing the
    topology is R3-7's third divergence answered at the call site as well as in the manager.
    """
    seen = []

    async def recording_can_admit(model_path, vram, sysram, endpoints=None):
        seen.append(endpoints)
        return (False, coordinator.NETWORK_MAXED_OUT_MESSAGE)

    monkeypatch.setattr(coordinator.manager, "acan_admit", recording_can_admit)

    async def scenario():
        await run_monitor_for(0.05)
        await drain_background_tasks()

    asyncio.run(scenario())

    assert seen, "the admission gate was never consulted"
    assert seen[0] == ["10.0.0.2:50052"]


def test_an_empty_forwarded_header_falls_back_to_the_socket_peer(monkeypatch):
    """A header that is present but contains nothing usable must not yield an empty string."""
    monkeypatch.setattr(coordinator, "get_worker_limits", trusting_limits)
    monkeypatch.setattr(coordinator, "get_config", lambda: {})

    request = FakeRequest(headers={"X-Forwarded-For": " , ,"}, client_host="10.0.0.1")
    assert coordinator.get_client_ip(request) == "10.0.0.1"
