"""
The coding model as websites and apps see it: ``coding_model`` and ``coding_tiers`` in
/v1/cluster/status, the ladder-based recommendation and /v1/cluster/tiers, and the speed
measured on real answers (engine/coding_speed.py) as they stream through /v1/chat/completions.
"""

import asyncio
import json
import os
import types

import pytest
from fastapi.responses import StreamingResponse
from httpx import ASGITransport, AsyncClient

import engine.coordinator as coordinator
from engine import coding_ladder, coding_speed, priority, release

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE_FILE = "Qwen3.6-35B-A3B-UD-IQ3_S.gguf"
LADDER = coding_ladder.load_tier_file(os.path.join(REPO_ROOT, "scripts", "coding_models.json"))

# The final chunk of a real streamed answer from TheWall's coding engine (llama.cpp b11838,
# 2026-10-25, max_tokens 16), byte for byte: the timings close the chunk with finish_reason.
LIVE_FINAL_CHUNK = (
    b'data: {"choices":[{"finish_reason":"length","index":0,"delta":{}}],"created":1792980760,'
    b'"id":"chatcmpl-wYPsjjKPylN8zL3iUuLJkaHyVr4IuJQt","model":"/home/wubarrk/barrkbot-llm/models/'
    b'Qwen3.6-35B-A3B-UD-IQ3_S.gguf","system_fingerprint":"b11838-4231b23dc","object":"chat.completion.chunk",'
    b'"timings":{"cache_n":0,"prompt_n":20,"prompt_ms":272.356,"prompt_per_token_ms":13.617799999999999,'
    b'"prompt_per_second":73.43330053312576,"predicted_n":16,"predicted_ms":396.896,'
    b'"predicted_per_token_ms":26.459733333333336,"predicted_per_second":37.79327582036604}}\n\n'
)


def content_chunk(text):
    body = {"choices": [{"finish_reason": None, "index": 0, "delta": {"content": text}}],
            "created": 1792980760, "id": "chatcmpl-x", "model": "/models/" + LIVE_FILE,
            "object": "chat.completion.chunk"}
    return b"data: " + json.dumps(body, separators=(",", ":")).encode() + b"\n\n"


def final_chunk(predicted_n, rate, finish="stop"):
    body = {"choices": [{"finish_reason": finish, "index": 0, "delta": {}}], "created": 1792980760,
            "id": "chatcmpl-x", "model": "/models/" + LIVE_FILE, "object": "chat.completion.chunk",
            "timings": {"cache_n": 0, "prompt_n": 812, "prompt_ms": 1500.0, "prompt_per_token_ms": 1.8,
                        "prompt_per_second": 541.3, "predicted_n": predicted_n,
                        "predicted_ms": predicted_n / rate * 1000.0, "predicted_per_token_ms": 1000.0 / rate,
                        "predicted_per_second": rate}}
    return b"data: " + json.dumps(body, separators=(",", ":")).encode() + b"\n\n"


def answer(predicted_n=200, rate=38.5, words=40):
    """A whole streamed answer, as llama-server sends it."""
    return [content_chunk(f"word{i} ") for i in range(words)] + [final_chunk(predicted_n, rate), b"data: [DONE]\n\n"]


# ======================================================================================
# Reading llama-server's timings
# ======================================================================================

def test_the_live_engines_final_chunk_is_read():
    assert coding_speed.parse_timings(LIVE_FINAL_CHUNK + b"data: [DONE]\n\n") == (16, 37.79327582036604)


def test_a_whole_answer_is_read_from_its_tail():
    tail = b"".join(answer(predicted_n=321, rate=41.25))[-coding_speed.TAIL_BYTES:]
    assert coding_speed.parse_timings(tail) == (321, 41.25)


def test_whitespace_in_the_json_is_tolerated():
    assert coding_speed.parse_timings(b'{"timings" : {"predicted_n": 80, "predicted_per_second": 30.5}}') == (80, 30.5)


def test_the_models_own_text_cannot_fake_a_measurement():
    # The model writing the literal text "timings":{...} arrives JSON-escaped.
    fake = content_chunk('"timings":{"predicted_n":999,"predicted_per_second":9999}')
    assert coding_speed.parse_timings(fake + b"data: [DONE]\n\n") is None
    real = fake + final_chunk(100, 35.0) + b"data: [DONE]\n\n"
    assert coding_speed.parse_timings(real) == (100, 35.0)


@pytest.mark.parametrize("tail", [
    b"",
    b"data: [DONE]\n\n",
    b'"timings":{"predicted_n":100,"predicted_per_second":30.0',          # cut off
    b'"timings":["predicted_n"]',
    b'"timings":{"predicted_n":100}',
    b'"timings":{"predicted_per_second":30.0}',
    b'"timings":{"predicted_n":"100","predicted_per_second":30.0}',
    b'"timings":{"predicted_n":true,"predicted_per_second":30.0}',
    b'"timings":{"predicted_n":100,"predicted_per_second":"fast"}',
    b'"timings":{"predicted_n":100,"predicted_per_second":-3.0}',
    b'"timings":{"predicted_n":100,"predicted_per_second":0}',
    b'"timings":{"predicted_n":100,"predicted_per_second":NaN}',
    b'"timings":{"predicted_n":100,"predicted_per_second":Infinity}',
    b'"timings":{"predicted_n":100,"predicted_per_second":1e9}',
    b'"timings":{"predicted_n":-1,"predicted_per_second":30.0}',
    b'"timings":{not json}',
    None,
])
def test_anything_else_is_no_measurement(tail):
    assert coding_speed.parse_timings(tail) is None


# ======================================================================================
# The meter: threshold, median, window, persistence
# ======================================================================================

@pytest.fixture
def meter(tmp_path):
    return coding_speed.SpeedMeter(str(tmp_path / "config" / coding_speed.SPEED_FILE))


def test_short_answers_do_not_count(meter):
    assert coding_speed.MIN_PREDICTED_TOKENS == 64
    assert meter.record(LIVE_FILE, 63, 99.0) is False
    assert meter.speed(LIVE_FILE) == (None, None)
    assert meter.record(LIVE_FILE, 64, 38.0, at="2026-10-26T01:00:00Z") is True
    assert meter.speed(LIVE_FILE) == (38.0, "2026-10-26T01:00:00Z")


def test_observe_applies_the_threshold_to_the_streams_own_count(meter):
    assert meter.observe(LIVE_FILE, LIVE_FINAL_CHUNK) is False      # 16 tokens
    assert meter.observe(LIVE_FILE, b"".join(answer(predicted_n=64, rate=33.0))) is True
    assert meter.speed(LIVE_FILE)[0] == 33.0


def test_the_median_of_the_last_twenty(meter):
    for rate in (10.0, 50.0, 30.0):
        meter.record(LIVE_FILE, 100, rate)
    assert meter.speed(LIVE_FILE)[0] == 30.0           # median, not mean (30.0 either way here)
    meter.record(LIVE_FILE, 100, 31.0)
    assert meter.speed(LIVE_FILE)[0] == 30.5           # even count: middle pair
    for rate in range(1, 26):                          # 25 more: only the last 20 stay
        meter.record(LIVE_FILE, 100, float(rate))
    assert meter.samples(LIVE_FILE) == [float(r) for r in range(6, 26)]
    assert meter.speed(LIVE_FILE)[0] == 15.5


def test_one_slow_outlier_does_not_move_the_median(meter):
    for _ in range(9):
        meter.record(LIVE_FILE, 200, 38.0)
    meter.record(LIVE_FILE, 200, 2.0)
    assert meter.speed(LIVE_FILE)[0] == 38.0


def test_speed_is_per_model_file(meter):
    meter.record("Other-Model-Q4_K_M.gguf", 100, 90.0)
    assert meter.speed(LIVE_FILE) == (None, None)
    assert meter.speed(None) == (None, None)
    assert meter.speed("") == (None, None)


def test_the_measurement_survives_a_restart(meter, tmp_path):
    for rate in (37.0, 38.5, 40.0):
        meter.record(LIVE_FILE, 300, rate, at="2026-10-26T01:02:03Z")
    meter.save()
    path = tmp_path / "config" / coding_speed.SPEED_FILE
    saved = json.loads(path.read_text())
    assert saved["models"][LIVE_FILE] == {"tokens_per_second": 38.5, "measured_at": "2026-10-26T01:02:03Z",
                                          "samples": [37.0, 38.5, 40.0]}
    assert [p.name for p in path.parent.iterdir()] == [coding_speed.SPEED_FILE], "no temporary file left behind"

    restarted = coding_speed.SpeedMeter(str(path))
    assert restarted.speed(LIVE_FILE) == (38.5, "2026-10-26T01:02:03Z")
    restarted.record(LIVE_FILE, 300, 41.0)             # and the window carries on
    assert restarted.samples(LIVE_FILE) == [37.0, 38.5, 40.0, 41.0]


def test_saving_twice_replaces_the_file(meter, tmp_path):
    meter.record(LIVE_FILE, 100, 30.0)
    meter.save()
    meter.record(LIVE_FILE, 100, 40.0)
    meter.save()
    saved = json.loads((tmp_path / "config" / coding_speed.SPEED_FILE).read_text())
    assert saved["models"][LIVE_FILE]["tokens_per_second"] == 35.0


@pytest.mark.parametrize("content", [
    "{not json",
    "[]",
    json.dumps({"models": []}),
    json.dumps({"models": {LIVE_FILE: {"samples": ["x", -1, None], "measured_at": "2026-10-26T01:02:03Z"}}}),
    json.dumps({"models": {LIVE_FILE: {"samples": [30.0]}}}),                       # no time
    json.dumps({"models": {LIVE_FILE: "fast"}}),
])
def test_a_damaged_speed_file_means_no_measurement_yet(tmp_path, content):
    path = tmp_path / coding_speed.SPEED_FILE
    path.write_text(content)
    meter = coding_speed.SpeedMeter(str(path))
    assert meter.speed(LIVE_FILE) == (None, None)
    assert meter.record(LIVE_FILE, 100, 30.0) is True
    meter.save()
    assert json.loads(path.read_text())["models"][LIVE_FILE]["samples"] == [30.0]


def test_a_saved_median_without_samples_is_kept(tmp_path):
    path = tmp_path / coding_speed.SPEED_FILE
    path.write_text(json.dumps({"models": {LIVE_FILE: {"tokens_per_second": 36.6, "measured_at": "2026-10-26T00:00:00Z"}}}))
    assert coding_speed.SpeedMeter(str(path)).speed(LIVE_FILE) == (36.6, "2026-10-26T00:00:00Z")


def test_the_oldest_models_are_forgotten_first(meter):
    for n in range(coding_speed.MAX_MODELS + 3):
        meter.record(f"Model-{n}.gguf", 100, 10.0 + n)
    meter.record("Model-0.gguf", 100, 1.0)             # gone already: comes back as new
    kept = [f"Model-{n}.gguf" for n in range(coding_speed.MAX_MODELS + 3)]
    assert meter.speed("Model-1.gguf") == (None, None)
    assert meter.speed("Model-0.gguf")[0] == 1.0
    assert meter.speed(kept[-1])[0] == 10.0 + coding_speed.MAX_MODELS + 2


def test_the_default_file_lives_in_the_install_config(isolated_config, dlne_root):
    meter = coding_speed.SpeedMeter()
    assert meter.path == str(dlne_root / "config" / coding_speed.SPEED_FILE)
    meter.record(LIVE_FILE, 100, 30.0)
    meter.save()
    assert (dlne_root / "config" / coding_speed.SPEED_FILE).is_file()


# ======================================================================================
# The chat proxy measures real answers
# ======================================================================================

class FakeRequest:
    def __init__(self, body):
        self._body = body
        self.headers = {}
        self.client = types.SimpleNamespace(host="203.0.113.7")
        # As the contribution middleware would set it: "host" may name the model (2.3.6).
        self.state = types.SimpleNamespace(dlne_tier="host")

    async def json(self):
        return self._body


class FakeStreamResponse:
    def __init__(self, chunks):
        self.status_code = 200
        self._chunks = chunks

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


class FakeStreamContext:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc_info):
        return False


class FakeAsyncClient:
    chunks = []
    is_closed = False

    def __init__(self, *args, **kwargs):
        pass

    def stream(self, method, url, **kwargs):
        return FakeStreamContext(FakeStreamResponse(list(self.chunks)))


class FakeProcess:
    returncode = None

    def poll(self):
        return None


def engine_entry(safe_context=65536, port=8081):
    return {"process": FakeProcess(), "port": port, "log_file": None, "safe_context": safe_context,
            "dynamic_slots": 2, "endpoints": [], "reserved_gb": 14.0}


@pytest.fixture
def proxy(monkeypatch, tmp_path):
    """The chat proxy with a simulated engine serving the live model, and a private meter."""
    monkeypatch.setattr(coordinator, "_inference_counter_lock", asyncio.Lock())
    monkeypatch.setattr(coordinator, "active_inference_requests", 0)
    monkeypatch.setattr(coordinator, "is_swapping_models", False)
    monkeypatch.setattr(coordinator, "is_recalculating_queue", False)
    monkeypatch.setattr(coordinator, "httpx", types.SimpleNamespace(
        AsyncClient=FakeAsyncClient, Timeout=lambda *a, **k: None, Limits=lambda *a, **k: None))
    monkeypatch.setattr(coordinator, "_engine_http", None)
    monkeypatch.setattr(coordinator, "global_active_model", LIVE_FILE)
    monkeypatch.setattr(coordinator, "_coding_tiers", LADDER)
    monkeypatch.setattr(coordinator.manager, "running_models", {LIVE_FILE: engine_entry()})
    monkeypatch.setattr(coordinator.registry, "workers", {})

    async def started(model_path, *args, **kwargs):
        return (True, 8081, "Already running")

    async def ready(port):
        return True

    async def no_progress(port, model_name):
        return None

    monkeypatch.setattr(coordinator.manager, "astart_model", started)
    monkeypatch.setattr(coordinator, "_probe_engine_port", ready)
    monkeypatch.setattr(coordinator, "stream_vram_progress_async", no_progress)
    meter = coding_speed.SpeedMeter(str(tmp_path / "config" / coding_speed.SPEED_FILE))
    monkeypatch.setattr(coding_speed, "meter", meter)
    return meter


def run_chat(monkeypatch, chunks, body=None):
    """One chat request through the proxy; returns what the client received."""
    monkeypatch.setattr(FakeAsyncClient, "chunks", chunks)

    async def scenario():
        response = await coordinator.chat_completions(
            FakeRequest(body or {"messages": [{"role": "user", "content": "write a function"}], "stream": True}))
        assert isinstance(response, StreamingResponse)
        received = [chunk async for chunk in response.body_iterator]
        saves = [t for t in list(coordinator._background_tasks) if t.get_name() == "coding-speed-save"]
        await asyncio.gather(*saves)
        return received

    return asyncio.run(scenario())


def test_a_real_answer_is_measured_and_saved(proxy, monkeypatch, tmp_path):
    chunks = answer(predicted_n=250, rate=38.5)
    received = run_chat(monkeypatch, chunks)
    assert b"".join(received) == b"".join(chunks), "the client gets the engine's bytes untouched"
    assert proxy.samples(LIVE_FILE) == [38.5]
    saved = json.loads((tmp_path / "config" / coding_speed.SPEED_FILE).read_text())
    assert saved["models"][LIVE_FILE]["tokens_per_second"] == 38.5
    assert coordinator.active_inference_requests == 0


def test_a_short_answer_is_not_measured(proxy, monkeypatch, tmp_path):
    run_chat(monkeypatch, answer(predicted_n=40, rate=60.0))
    assert proxy.samples(LIVE_FILE) == []
    assert not (tmp_path / "config" / coding_speed.SPEED_FILE).exists()


def test_a_long_answer_is_measured_from_its_tail(proxy, monkeypatch):
    chunks = answer(predicted_n=4000, rate=27.0, words=3000)    # far more than the tail keeps
    assert sum(len(c) for c in chunks) > 10 * coding_speed.TAIL_BYTES
    run_chat(monkeypatch, chunks)
    assert proxy.samples(LIVE_FILE) == [27.0]


def test_the_timings_split_across_chunks_are_still_read(proxy, monkeypatch):
    whole = b"".join(answer(predicted_n=150, rate=36.0))
    run_chat(monkeypatch, [whole[i:i + 7] for i in range(0, len(whole), 7)])
    assert proxy.samples(LIVE_FILE) == [36.0]


def test_the_rate_is_filed_under_the_model_that_answered(proxy, monkeypatch):
    other = "Qwen3.6-35B-A3B-UD-Q6_K.gguf"
    monkeypatch.setattr(coordinator.manager, "running_models", {other: engine_entry()})
    run_chat(monkeypatch, answer(predicted_n=150, rate=22.0),
             body={"model": other, "messages": [{"role": "user", "content": "x"}], "stream": True})
    assert proxy.samples(other) == [22.0]
    assert proxy.samples(LIVE_FILE) == []


def test_a_failing_measurement_never_breaks_the_answer(proxy, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(proxy, "observe", broken)
    chunks = answer()
    assert b"".join(run_chat(monkeypatch, chunks)) == b"".join(chunks)
    assert coordinator.active_inference_requests == 0


def test_a_request_naming_no_model_starts_the_ladders_first_tier(proxy, monkeypatch):
    monkeypatch.setattr(coordinator, "global_active_model", "")
    started = []

    async def recording(model_path, *args, **kwargs):
        started.append(model_path)
        return (True, 8081, "Started")

    monkeypatch.setattr(coordinator.manager, "astart_model", recording)
    run_chat(monkeypatch, answer())
    assert started == [LIVE_FILE]
    assert coordinator._default_coding_model() == LIVE_FILE


# ======================================================================================
# /v1/cluster/status and /v1/cluster/tiers
# ======================================================================================

@pytest.fixture
def cluster(monkeypatch, tmp_path):
    """TheWall today: a 16 GB pool serving Qwen3.6 35B-A3B IQ3_S with a 65536-token engine."""
    monkeypatch.setattr(coordinator, "_coding_tiers", LADDER)
    monkeypatch.setattr(coordinator, "global_active_model", LIVE_FILE)
    monkeypatch.setattr(coordinator.manager, "running_models", {LIVE_FILE: engine_entry(65536)})
    monkeypatch.setattr(coordinator.registry, "get_total_vram_gb", lambda: 16.0)
    monkeypatch.setattr(coordinator, "_near_vram_gb", lambda: 16.0)      # all of it near enough to share a model

    async def no_telemetry(model_path):
        return {}

    monkeypatch.setattr(coordinator.manager, "aget_loading_telemetry", no_telemetry)
    meter = coding_speed.SpeedMeter(str(tmp_path / coding_speed.SPEED_FILE))
    monkeypatch.setattr(coding_speed, "meter", meter)
    return types.SimpleNamespace(monkeypatch=monkeypatch, meter=meter, tmp_path=tmp_path)


async def get_json(path):
    async with AsyncClient(transport=ASGITransport(app=coordinator.app), base_url="http://test") as client:
        response = await client.get(path)
        assert response.status_code == 200
        return response.json()


@pytest.mark.anyio
async def test_status_reports_the_real_coding_model(cluster):
    for rate in (37.0, 38.5, 39.0):
        cluster.meter.record(LIVE_FILE, 400, rate, at="2026-10-26T01:30:00Z")
    data = await get_json("/v1/cluster/status")
    assert data["coding_model"] == {
        "file": LIVE_FILE,
        "name": "Qwen3.6 35B-A3B",
        "context": 65536,
        "tokens_per_second": 38.5,
        "speed_measured_at": "2026-10-26T01:30:00Z",
    }


@pytest.mark.anyio
async def test_status_lists_the_ladder_in_the_agreed_shape(cluster):
    data = await get_json("/v1/cluster/status")
    tiers = data["coding_tiers"]
    assert [t["id"] for t in tiers] == [t["id"] for t in LADDER]
    for row in tiers:
        assert set(row) == {"id", "name", "min_vram_gb", "enabled", "unlocked", "active"}
        assert isinstance(row["min_vram_gb"], int)
        assert all(isinstance(row[k], bool) for k in ("enabled", "unlocked", "active"))
    rest = len(tiers) - 1
    assert [t["unlocked"] for t in tiers] == [True] + [False] * rest      # 16 GB holds only the first
    assert [t["active"] for t in tiers] == [True] + [False] * rest
    assert [t["enabled"] for t in tiers] == [True] * 4 + [False] * (len(tiers) - 4)   # the four approved tiers, then pinned-but-off ones
    assert set(data["coding_model"]) == {"file", "name", "context", "tokens_per_second", "speed_measured_at"}


@pytest.mark.anyio
async def test_the_stale_14b_recommendation_is_gone(cluster):
    data = await get_json("/v1/cluster/status")
    assert data["recommended_tier"] == "Qwen3.6 35B-A3B (15 GB+ VRAM)"
    assert data["recommended_model"] == LIVE_FILE
    ours = json.dumps({k: data[k] for k in ("recommended_tier", "recommended_model", "coding_model", "coding_tiers")})
    assert "14B" not in ours and "qwen2.5" not in ours.lower()
    assert not hasattr(coordinator, "VRAM_TIERS")


@pytest.mark.anyio
@pytest.mark.parametrize("vram, file", [
    (8.0, LIVE_FILE),                                   # below every tier: the ladder's first
    (16.0, LIVE_FILE),
    (24.0, "Qwen3.6-35B-A3B-UD-IQ4_XS.gguf"),
    (32.0, "Qwen3.6-35B-A3B-UD-Q4_K_M.gguf"),
    (48.0, "Qwen3.6-35B-A3B-UD-Q6_K.gguf"),
    (256.0, "Qwen3.6-35B-A3B-UD-Q6_K.gguf"),            # the placeholders are off
])
async def test_the_recommendation_follows_the_pool(cluster, vram, file):
    cluster.monkeypatch.setattr(coordinator.registry, "get_total_vram_gb", lambda: vram)
    cluster.monkeypatch.setattr(coordinator, "_near_vram_gb", lambda: vram)
    data = await get_json("/v1/cluster/status")
    assert data["recommended_model"] == file
    tiers = await get_json("/v1/cluster/tiers")
    assert tiers["recommended_model"] == file


@pytest.mark.anyio
async def test_a_gpu_too_far_to_share_the_model_unlocks_nothing(cluster):
    """24 GB lent, but 8 of it too far away to share a model: the 24 GB tier is not unlocked, because
    automatic switching would never move to it (the site said "unlocked" and nothing happened)."""
    cluster.monkeypatch.setattr(coordinator.registry, "get_total_vram_gb", lambda: 24.0)
    data = await get_json("/v1/cluster/status")
    assert data["total_vram_gb"] == 24.0
    assert {t["id"]: t["unlocked"] for t in data["coding_tiers"]}["qwen36-a3b-iq4xs"] is False
    assert data["recommended_model"] == LIVE_FILE
    tiers = await get_json("/v1/cluster/tiers")
    assert {t["id"]: t["unlocked"] for t in tiers["tiers"]}["qwen36-a3b-iq4xs"] is False
    assert tiers["total_vram_gb"] == 24.0


@pytest.mark.anyio
async def test_no_engine_running_means_no_context_but_still_the_model(cluster):
    cluster.monkeypatch.setattr(coordinator.manager, "running_models", {})
    cluster.meter.record(LIVE_FILE, 400, 38.0, at="2026-10-26T01:30:00Z")
    data = await get_json("/v1/cluster/status")
    assert data["coding_model"]["file"] == LIVE_FILE           # what the next request starts
    assert data["coding_model"]["context"] is None             # never guessed
    assert data["coding_model"]["tokens_per_second"] == 38.0   # the saved measurement, not blanked


@pytest.mark.anyio
async def test_the_queue_shows_the_engines_slots_before_the_first_chat(cluster):
    # After a restart the gate kept its default of 2 until a chat asked for a slot, while TheWall's engine runs 5:
    # the status (and the website) said 2 coding slots.
    gate = priority.CodingGate()
    cluster.monkeypatch.setattr(coordinator, "coding_gate", gate)
    entry = engine_entry(163840)
    entry["dynamic_slots"] = 5
    cluster.monkeypatch.setattr(coordinator.manager, "running_models", {LIVE_FILE: entry})
    data = await get_json("/v1/cluster/status")
    assert data["coding_queue"]["slots"] == 5
    assert gate.stats()["slots"] == 5
    assert coordinator._fim_engine_slots() == 5          # autocomplete sees the real count too


@pytest.mark.anyio
@pytest.mark.parametrize("slots", [0, -1, True, "5", None, 2.5])
async def test_a_nonsense_slot_count_leaves_the_queue_as_it_was(cluster, slots):
    gate = priority.CodingGate(slots=3)
    cluster.monkeypatch.setattr(coordinator, "coding_gate", gate)
    entry = engine_entry(65536)
    entry["dynamic_slots"] = slots
    cluster.monkeypatch.setattr(coordinator.manager, "running_models", {LIVE_FILE: entry})
    assert (await get_json("/v1/cluster/status"))["coding_queue"]["slots"] == 3


@pytest.mark.anyio
async def test_with_no_engine_running_the_queue_keeps_its_count(cluster):
    gate = priority.CodingGate(slots=3)
    cluster.monkeypatch.setattr(coordinator, "coding_gate", gate)
    cluster.monkeypatch.setattr(coordinator.manager, "running_models", {})
    assert (await get_json("/v1/cluster/status"))["coding_queue"]["slots"] == 3


@pytest.mark.anyio
async def test_speed_is_only_reported_for_the_model_being_served(cluster):
    cluster.meter.record("Qwen3.6-35B-A3B-UD-Q6_K.gguf", 400, 21.0)
    data = await get_json("/v1/cluster/status")
    assert data["coding_model"]["tokens_per_second"] is None
    assert data["coding_model"]["speed_measured_at"] is None


@pytest.mark.anyio
async def test_a_model_off_the_ladder_gets_a_derived_name_and_no_active_tier(cluster):
    odd = "Qwen3-Coder-30B-A3B-Instruct-UD-Q3_K_XL.gguf"
    cluster.monkeypatch.setattr(coordinator.manager, "running_models", {odd: engine_entry(32768)})
    data = await get_json("/v1/cluster/status")
    assert data["coding_model"]["name"] == "Qwen3 Coder 30B-A3B Instruct"
    assert data["coding_model"]["context"] == 32768
    assert not any(t["active"] for t in data["coding_tiers"])


@pytest.mark.anyio
async def test_status_still_answers_if_the_coding_part_fails(cluster):
    async def broken(*args, **kwargs):
        raise RuntimeError("snapshot exploded")

    cluster.monkeypatch.setattr(coordinator.manager, "asnapshot_model", broken)
    data = await get_json("/v1/cluster/status")
    assert data["coding_model"] == {"file": None, "name": None, "context": None,
                                    "tokens_per_second": None, "speed_measured_at": None}
    assert data["coding_tiers"] == []
    assert data["recommended_model"] == LIVE_FILE


@pytest.mark.anyio
async def test_cluster_tiers_lists_the_ladder_with_the_old_field_names(cluster, tmp_path):
    present = tmp_path / LIVE_FILE
    present.write_bytes(b"GGUF")

    async def resolve(tag):
        return str(present) if tag == LIVE_FILE else ""

    cluster.monkeypatch.setattr(coordinator.manager, "aresolve_model_path", resolve)
    data = await get_json("/v1/cluster/tiers")
    assert data["recommended_tier"] == "Qwen3.6 35B-A3B (15 GB+ VRAM)"
    assert data["total_vram_gb"] == 16.0
    rows = data["tiers"]
    assert [r["id"] for r in rows] == [t["id"] for t in LADDER]
    for row in rows:
        assert {"min_vram_gb", "tier", "model", "alt_model", "unlocked", "cached_on_disk"} <= set(row)
    first, split = rows[0], {r["id"]: r for r in rows}["qwen38-flash-next-q2"]
    assert (first["model"], first["tier"], first["cached_on_disk"], first["parts"]) == \
        (LIVE_FILE, "Qwen3.6 35B-A3B (15 GB+ VRAM)", True, 1)
    assert (first["context"], first["server_slots"], first["context_per_request"]) == (81920, 2, 40960)
    assert (split["context"], split["server_slots"], split["context_per_request"]) == (65536, 2, 32768)
    assert split["parts"] == 3 and split["enabled"] is False and split["blocked"].startswith("placeholder")
    assert split["cached_on_disk"] is False
    assert "qwen2.5" not in json.dumps(data).lower()


def test_the_coordinator_reads_the_shipped_ladder_by_default(monkeypatch):
    monkeypatch.setattr(coordinator, "_coding_tiers", None)
    monkeypatch.setattr(coordinator, "get_base_dir", lambda: REPO_ROOT)
    with open(os.path.join(REPO_ROOT, "scripts", "coding_models.json"), encoding="utf-8") as handle:
        shipped = json.load(handle)["tiers"]
    assert coordinator._coding_ladder() == release.check_coding_tiers(shipped)
    assert coordinator._default_coding_model() == LIVE_FILE
