"""A benchmark run on an engine with phone lanes (2.6) is pinned to a coding lane, never a phone's."""

import pytest

from engine.priority import LaneBook
from tests.app_pool_fakes import FakeLlamaServer


@pytest.fixture(scope="module")
def engine():
    server = FakeLlamaServer("gpu").start()
    yield server
    server.stop()


@pytest.mark.anyio
async def test_a_benchmark_is_pinned_to_a_coding_lane_and_gives_it_back(engine, monkeypatch):
    from engine import benchmark

    book = LaneBook()
    monkeypatch.setattr(benchmark, "lane_book", book)
    monkeypatch.setattr(benchmark, "append_record", lambda record: True)
    snapshot = {"port": engine.port, "dynamic_slots": 2, "phone_slots": 2}

    class Manager:
        async def asnapshot_model(self, model):
            return dict(snapshot)

    engine.reset()
    for _ in range(3):
        await benchmark.run_inference_benchmark(Manager(), "m.gguf", max_tokens=8)
    lanes_off = dict(snapshot, phone_slots=0)
    snapshot.update(lanes_off)
    await benchmark.run_inference_benchmark(Manager(), "m.gguf", max_tokens=8)

    assert [p.get("id_slot") for p in engine.payloads] == [0, 0, 0, None]
    assert book._uses.get(0, 0) == 0 and all(lane in (0, 1) for lane in book._uses)
    engine.reset()
