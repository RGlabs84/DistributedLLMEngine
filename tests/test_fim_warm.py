"""
Ghost text on the hybrid coding model: the coordinator sends the text before the cursor to the engine as a request of
its own (nothing generated) ahead of the completion, so the engine's state is parked at the cursor and the completion
reads only what follows it. Measured on TheWall: about 500 tokens re-read per keystroke without it, about 110 with it.
"""

import http.server
import json
import threading

import pytest
from httpx import ASGITransport, AsyncClient

from engine import coordinator
from engine.lease import lease_manager


def test_only_a_fill_in_the_middle_prompt_gets_a_warm_up():
    prompt = "<|fim_prefix|>def f(x):\n    return <|fim_suffix|>\n\nprint(f(1))<|fim_middle|>"
    warm = coordinator.fim_warm_body({"prompt": prompt, "max_tokens": 48, "stop": ["\n"]})
    assert warm == {"prompt": "<|fim_prefix|>def f(x):\n    return ", "max_tokens": 0, "temperature": 0.0, "stream": False}
    for plain in ("def f(", "<|fim_prefix|>no suffix marker", "x<|fim_prefix|>a<|fim_suffix|>b", None, 7, ["<|fim_prefix|>a<|fim_suffix|>"]):
        assert coordinator.fim_warm_body({"prompt": plain}) is None
    assert coordinator.fim_warm_body({}) is None


class FakeEngine(http.server.BaseHTTPRequestHandler):
    seen = []
    drop_warm = False

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeEngine.seen.append(body)
        if FakeEngine.drop_warm and body.get("max_tokens") == 0:
            self.close_connection = True
            self.connection.shutdown(2)
            return
        data = json.dumps({"choices": [{"text": "pass", "index": 0}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def engine(monkeypatch):
    FakeEngine.seen = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeEngine)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    async def fake_start(model_path, *args, **kwargs):
        return (True, server.server_address[1], "")

    async def fake_estimate(_model):
        return 1.0

    async def ready(_port):
        return True

    monkeypatch.setattr(coordinator.manager, "astart_model", fake_start)
    monkeypatch.setattr(coordinator.manager, "aestimate_model_vram_gb", fake_estimate)
    monkeypatch.setattr(coordinator, "_probe_engine_port", ready)
    monkeypatch.setattr(coordinator, "is_swapping_models", False)
    monkeypatch.setattr(coordinator, "is_recalculating_queue", False)
    with lease_manager._lock:
        lease_manager._leases.clear()
    yield FakeEngine
    server.shutdown()
    with lease_manager._lock:
        lease_manager._leases.clear()


async def _complete(prompt):
    transport = ASGITransport(app=coordinator.app, client=("127.0.0.1", 5000))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/v1/completions", json={"prompt": prompt, "model": "m.gguf", "max_tokens": 48, "stop": ["\n"]})


@pytest.mark.anyio
async def test_the_engine_sees_the_prefix_alone_and_then_the_unchanged_request(engine):
    prompt = "<|fim_prefix|>x = 1\ny = <|fim_suffix|>\nprint(y)<|fim_middle|>"
    resp = await _complete(prompt)
    assert resp.status_code == 200 and resp.json()["choices"][0]["text"] == "pass"
    warm, real = engine.seen
    assert warm["prompt"] == "<|fim_prefix|>x = 1\ny = " and warm["max_tokens"] == 0
    assert real["prompt"] == prompt and real["max_tokens"] == 48 and real["stop"] == ["\n"]


@pytest.mark.anyio
async def test_an_ordinary_completion_is_sent_once(engine):
    resp = await _complete("def f(")
    assert resp.status_code == 200
    assert [b["prompt"] for b in engine.seen] == ["def f("]


@pytest.mark.anyio
async def test_a_warm_up_that_fails_does_not_fail_the_completion(engine):
    engine.drop_warm = True            # the engine hangs up on the warm-up request
    try:
        resp = await _complete("<|fim_prefix|>a<|fim_suffix|>b<|fim_middle|>")
    finally:
        engine.drop_warm = False
    assert resp.status_code == 200 and resp.json()["choices"][0]["text"] == "pass"
    assert [b["max_tokens"] for b in engine.seen] == [0, 48]
