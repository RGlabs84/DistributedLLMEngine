"""
engine/throughput.py: the sliding window of tokens per second per pool, and the counters that feed it
without reading anything but whether a frame carries generated text.
"""

import json
import threading

import pytest

from engine.throughput import (SealedFrameCounter, StreamTokenCounter, Throughput, completion_tokens)


class Clock:
    t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def window():
    clock = Clock()
    return Throughput(clock=clock), clock


def frame(text, **extra):
    chunk = {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}], **extra}
    return f"data: {json.dumps(chunk)}\n\n".encode()


# ======================================================================================
# The window
# ======================================================================================

def test_nothing_recorded_is_zero(window):
    w, _ = window
    assert w.rate("coding", 60) == 0.0 and w.total("coding", 3600) == 0 and w.total("never-heard-of", 10) == 0


def test_the_rate_is_tokens_over_the_last_minute_per_second(window):
    w, clock = window
    for second in range(60):
        w.record("coding", 30)           # 30 tokens in each of 60 seconds
        if second < 59:
            clock.t += 1
    assert w.rate("coding", 60) == pytest.approx(30.0)
    assert w.total("coding", 60) == 1800 and w.total("coding", 3600) == 1800
    assert w.rate("coding", 10) == pytest.approx(30.0)


def test_pools_are_separate(window):
    w, _ = window
    w.record("coding", 5)
    w.record("phone", 2)
    assert (w.total("coding"), w.total("phone")) == (5, 2)


def test_tokens_in_one_second_share_a_bucket_and_n_is_summed(window):
    w, _ = window
    for n in (1, 1, 3):
        w.record("coding", n)
    assert w.total("coding", 1) == 5 and len(w._buckets["coding"]) == 1


def test_a_second_ago_counts_in_the_minute_but_not_in_the_current_second(window):
    w, clock = window
    w.record("coding", 7)
    clock.t += 1
    assert w.total("coding", 1) == 0 and w.total("coding", 2) == 7


def test_the_last_minute_forgets_and_the_hour_keeps(window):
    w, clock = window
    w.record("coding", 600)
    clock.t += 59
    assert w.total("coding", 60) == 600
    clock.t += 1
    assert w.total("coding", 60) == 0 and w.rate("coding", 60) == 0.0
    assert w.total("coding", 3600) == 600
    clock.t += 3600
    assert w.total("coding", 3600) == 0


def test_old_buckets_are_dropped_so_memory_is_bounded(window):
    w, clock = window
    for _ in range(10_000):
        w.record("coding", 1)
        clock.t += 1
    assert len(w._buckets["coding"]) <= 3601
    assert w.total("coding", 3600) <= 3600


def test_records_that_are_not_tokens_are_ignored(window):
    w, _ = window
    for n in (0, -4):
        w.record("coding", n)
    assert w.total("coding") == 0


def test_a_clock_that_steps_back_does_not_break_the_window(window):
    w, clock = window
    w.record("coding", 2)
    clock.t -= 30
    w.record("coding", 3)
    assert w.total("coding", 60) == 5


def test_an_explicit_time_is_honoured(window):
    w, clock = window
    w.record("coding", 4, now=clock.t - 120)
    w.record("coding", 6)
    assert w.total("coding", 60) == 6 and w.total("coding", 3600) == 10


def test_the_windows_asked_for_are_clamped(window):
    w, _ = window
    w.record("coding", 9)
    assert w.total("coding", 10 ** 9) == 9
    assert w.rate("coding", 0) == 9.0 and w.rate("coding", -5) == 9.0


def test_many_threads_recording_lose_nothing():
    clock = Clock()
    w = Throughput(clock=clock)
    per_thread, threads = 5_000, 8

    def work():
        for _ in range(per_thread):
            w.record("coding", 1)

    pool = [threading.Thread(target=work) for _ in range(threads)]
    for thread in pool:
        thread.start()
    readers = [threading.Thread(target=lambda: [w.rate("coding", 60) for _ in range(300)]) for _ in range(2)]
    for thread in readers:
        thread.start()
    for thread in pool + readers:
        thread.join()
    assert w.total("coding", 3600) == per_thread * threads


# ======================================================================================
# Counting a stream
# ======================================================================================

def test_each_frame_with_generated_text_is_one_token():
    w = Throughput(clock=Clock())
    counter = StreamTokenCounter("coding", sink=w)
    assert counter.feed(frame("hel") + frame("lo") + frame(" world")) == 3
    assert counter.feed(b"data: [DONE]\n\n") == 0
    assert w.total("coding") == 3


def test_a_frame_cut_in_two_by_the_network_counts_once_when_it_is_whole():
    w = Throughput(clock=Clock())
    counter = StreamTokenCounter("coding", sink=w)
    whole = frame("token")
    assert counter.feed(whole[:20]) == 0 and counter.feed(whole[20:]) == 1
    assert w.total("coding") == 1


def test_frames_that_generate_nothing_do_not_count():
    w = Throughput(clock=Clock())
    counter = StreamTokenCounter("coding", sink=w)
    first = {"choices": [{"index": 0, "delta": {"role": "assistant", "content": None}, "finish_reason": None}]}
    empty = {"choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": None}]}
    last = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": {"completion_tokens": 40},
            "timings": {"predicted_per_second": 30.0}}
    usage_only = {"choices": [], "usage": {"completion_tokens": 40}}
    data = b"".join(f"data: {json.dumps(c)}\n\n".encode() for c in (first, empty, last, usage_only))
    data += b": a comment\n\n" + b"event: ping\n\n" + b"data: [DONE]\n\n"
    assert counter.feed(data) == 0 and counter.finish() == 0
    assert w.total("coding") == 0, "the closing frame's usage is not counted on top of the frames"


def test_reasoning_and_tool_call_frames_are_tokens_too():
    w = Throughput(clock=Clock())
    counter = StreamTokenCounter("coding", sink=w)
    reasoning = {"choices": [{"delta": {"reasoning_content": "think"}}]}
    tool = {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{\"pa"}}]}}]}
    data = b"".join(f"data: {json.dumps(c)}\n\n".encode() for c in (reasoning, tool))
    assert counter.feed(data) == 2


def test_text_that_looks_like_a_frame_is_still_one_token():
    """The model writing the word content or a quoted key inside its answer is one frame, however it is shaped."""
    w = Throughput(clock=Clock())
    counter = StreamTokenCounter("coding", sink=w)
    assert counter.feed(frame('"content": "x"')) == 1


def test_the_unfinished_last_line_counts_at_the_end():
    w = Throughput(clock=Clock())
    counter = StreamTokenCounter("coding", sink=w)
    whole = frame("tail").rstrip(b"\n")
    assert counter.feed(whole) == 0 and counter.finish() == 1 and counter.finish() == 0


def test_a_whole_answer_counts_its_reported_completion_tokens_when_it_is_complete():
    w = Throughput(clock=Clock())
    answer = json.dumps({"choices": [{"message": {"role": "assistant", "content": "hello there"}}],
                         "usage": {"completion_tokens": 37, "prompt_tokens": 5000, "total_tokens": 5037},
                         "timings": {"predicted_per_second": 31.5, "prompt_per_second": 400.0, "x": "y" * 300}}).encode()
    counter = StreamTokenCounter("coding", streamed=False, sink=w)
    for i in range(0, len(answer), 50):
        assert counter.feed(answer[i:i + 50]) == 0
    assert w.total("coding") == 0, "nothing until the answer is complete"
    assert counter.finish() == 37 and w.total("coding") == 37
    assert completion_tokens(answer) == 37 and completion_tokens(b"{}") == 0 and completion_tokens(b"") == 0


def test_a_streamed_counter_ignores_usage_so_nothing_is_counted_twice():
    w = Throughput(clock=Clock())
    counter = StreamTokenCounter("coding", sink=w)
    counter.feed(frame("a") + frame("b") + frame("", usage={"completion_tokens": 2}))
    counter.finish()
    assert w.total("coding") == 2


def test_the_counter_keeps_nothing_but_the_unfinished_line():
    counter = StreamTokenCounter("coding", sink=Throughput(clock=Clock()))
    for _ in range(1000):
        counter.feed(frame("secret prompt echo " * 10))
    assert counter._pending == b""
    counter.feed(b"data: " + b"x" * (StreamTokenCounter.MAX_LINE + 10))
    assert counter._pending == b""


# ======================================================================================
# Counting a sealed stream
# ======================================================================================

def sealed(n):
    return f'data: {json.dumps({"dlne_e2e": {"n": n, "ct": "AAAA"}})}\n\n'.encode()


def test_a_sealed_stream_counts_every_frame_but_the_sealed_end():
    w = Throughput(clock=Clock())
    counter = SealedFrameCounter(sink=w)
    stream = b"".join(sealed(n) for n in range(6)) + b"data: [DONE]\n\n"        # frame 5 is the sealed end marker
    assert counter.feed(stream) == 5 and w.total("coding") == 5


def test_a_sealed_frame_is_counted_as_the_next_one_arrives():
    w = Throughput(clock=Clock())
    counter = SealedFrameCounter(sink=w)
    assert counter.feed(sealed(0)) == 0
    assert counter.feed(sealed(1)) == 1
    assert counter.feed(sealed(2)[:15]) == 0 and counter.feed(sealed(2)[15:]) == 1
    assert w.total("coding") == 2


def test_nothing_but_sealed_frames_count():
    w = Throughput(clock=Clock())
    counter = SealedFrameCounter(sink=w)
    assert counter.feed(b'data: {"dlne": {"event": "member_dropped"}}\n\ndata: [DONE]\n\n: ping\n\n') == 0
    assert counter.feed(frame("plain text")) == 0
    assert w.total("coding") == 0
