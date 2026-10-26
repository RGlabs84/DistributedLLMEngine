"""
Where tokens are counted (engine/throughput.py): the coordinator's own coding engine (chat and
autocomplete, streamed or not), and the phone pool. Coding members and canaries are counted (and not)
in tests/test_coding_endpoint.py, next to the members they involve.

A token is counted when its frame arrives. Nothing of a request or an answer is read beyond whether a
frame carries text.
"""

import asyncio
import json
import types

import httpx
import pytest

from engine import coordinator
from engine.app_pool import AppPool, Member
from engine.throughput import POOL_CODING, POOL_PHONE, throughput
from tests.app_pool_fakes import DONE, FakeLlamaServer, sse
from tests.test_coding_status import FakeAsyncClient, FakeRequest, answer, content_chunk, final_chunk, proxy, run_chat  # noqa: F401


@pytest.fixture(autouse=True)
def empty_window():
    throughput.reset()
    yield
    throughput.reset()


# ======================================================================================
# The coordinator's own coding engine
# ======================================================================================

def test_a_streamed_chat_answer_counts_one_token_per_frame_of_text(proxy, monkeypatch):
    chunks = answer(predicted_n=40, rate=38.5, words=40)          # 40 frames of text, a closing frame, [DONE]
    received = run_chat(monkeypatch, chunks)
    assert b"".join(received) == b"".join(chunks), "counting changes nothing the client receives"
    assert throughput.total(POOL_CODING, 3600) == 40
    assert throughput.total(POOL_PHONE, 3600) == 0


def test_a_chat_answer_that_was_not_streamed_counts_its_reported_completion_tokens(proxy, monkeypatch):
    whole = json.dumps({"choices": [{"message": {"role": "assistant", "content": "hello there"}}],
                        "usage": {"completion_tokens": 123, "prompt_tokens": 800, "total_tokens": 923},
                        "timings": {"predicted_per_second": 33.0}}).encode()
    run_chat(monkeypatch, [whole], body={"messages": [{"role": "user", "content": "hi"}], "stream": False})
    assert throughput.total(POOL_CODING, 3600) == 123


def test_a_streamed_answers_closing_usage_is_not_counted_on_top(proxy, monkeypatch):
    closing = dict(json.loads(final_chunk(5, 30.0)[len(b"data: "):]), usage={"completion_tokens": 5})
    chunks = [content_chunk(f"w{i}") for i in range(5)] + [b"data: " + json.dumps(closing).encode() + b"\n\n", DONE]
    run_chat(monkeypatch, chunks)
    assert throughput.total(POOL_CODING, 3600) == 5


def test_a_chat_that_never_reached_the_engine_counts_nothing(proxy, monkeypatch):
    run_chat(monkeypatch, [])
    assert throughput.total(POOL_CODING, 3600) == 0


class FakeCompletionClient(FakeAsyncClient):
    """The engine's /v1/completions: a stream of frames, or a whole JSON answer for post()."""

    whole = b"{}"

    async def post(self, url, **kwargs):
        return types.SimpleNamespace(status_code=200, content=self.whole)


@pytest.fixture
def completions(proxy, monkeypatch):
    monkeypatch.setattr(coordinator, "httpx", types.SimpleNamespace(
        AsyncClient=FakeCompletionClient, Timeout=lambda *a, **k: None, Limits=lambda *a, **k: None))
    monkeypatch.setattr(coordinator, "_engine_http", None)

    async def started(model_path, *args, **kwargs):
        return (True, 8081, "Already running")

    async def estimate(_model):
        return 1.0

    monkeypatch.setattr(coordinator.manager, "astart_model", started)
    monkeypatch.setattr(coordinator.manager, "aestimate_model_vram_gb", estimate)

    def run(body, chunks=(), whole=b"{}"):
        monkeypatch.setattr(FakeCompletionClient, "chunks", list(chunks))
        monkeypatch.setattr(FakeCompletionClient, "whole", whole)

        async def scenario():
            response = await coordinator.completions(FakeRequest(body))
            if hasattr(response, "body_iterator"):
                return [chunk async for chunk in response.body_iterator]
            return [response.body]

        return asyncio.run(scenario())

    return run


def fim_frame(text):
    return b"data: " + json.dumps({"index": 0, "content": text, "stop": False}).encode() + b"\n\n"


def test_a_streamed_autocomplete_counts_its_frames(completions):
    out = completions({"prompt": "def f(", "model": "m.gguf", "stream": True},
                      [fim_frame("a"), fim_frame("b"), fim_frame("c"), fim_frame(""), DONE])
    assert len(out) == 5
    assert throughput.total(POOL_CODING, 3600) == 3


def test_an_autocomplete_that_was_not_streamed_counts_its_reported_tokens(completions):
    whole = json.dumps({"choices": [{"text": "x"}], "usage": {"completion_tokens": 17}}).encode()
    completions({"prompt": "def f(", "model": "m.gguf"}, whole=whole)
    assert throughput.total(POOL_CODING, 3600) == 17


def test_a_warm_up_that_generates_nothing_counts_nothing(completions):
    """The extension warms the engine's cache with max_tokens 0: frames with no text, usage of 0."""
    completions({"prompt": "def f(", "model": "m.gguf", "stream": True, "max_tokens": 0},
                [fim_frame(""), b'data: {"index":0,"content":"","stop":true}\n\n', DONE])
    completions({"prompt": "def f(", "model": "m.gguf", "max_tokens": 0},
                whole=json.dumps({"choices": [{"text": ""}], "usage": {"completion_tokens": 0}}).encode())
    assert throughput.total(POOL_CODING, 3600) == 0


# ======================================================================================
# The phone pool
# ======================================================================================

@pytest.fixture(scope="module")
def fake_member():
    server = FakeLlamaServer("p").start()
    yield server
    server.stop()


def phone_stream(server, chunks):
    server.reset()
    server.chunks = list(chunks)
    pool = AppPool()
    pool.load_tiers([{"id": "small", "name": "Small", "file": "s.gguf", "min_ram_gb": 8}])
    member = Member("worker:p", 12.0, "small", True, 2, server.url)

    async def run():
        async with httpx.AsyncClient() as client:
            return b"".join([c async for c in pool.stream(
                {"messages": [{"role": "user", "content": "hello"}]}, [member], "small", client)])

    return asyncio.run(run())


def test_the_phone_pool_counts_a_token_per_frame_of_text(fake_member):
    out = phone_stream(fake_member, [sse("one "), sse("two "), sse("three"), DONE])
    assert b"three" in out
    assert throughput.total(POOL_PHONE, 3600) == 3
    assert throughput.total(POOL_CODING, 3600) == 0


def test_the_phone_pool_ignores_frames_with_no_text(fake_member):
    empty = b'data: {"choices":[{"index":0,"delta":{"content":""},"finish_reason":null}]}\n\n'
    role = b'data: {"choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}\n\n'
    phone_stream(fake_member, [role, empty, sse("a"), empty, DONE])
    assert throughput.total(POOL_PHONE, 3600) == 1
