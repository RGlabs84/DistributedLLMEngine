"""
Maintenance mode (2.8.5). Owner, 2026-11-02: "We need a temporary unavailable page that shows universally and vs code
and in the app and the web chat ... in dlne's website layout", "Make it scripting on and off".

MAINTENANCE=true closes the doors that start new work for people (429 with the message: what today's app and VS Code
agent turn into the server's own words; voice 503 voice_paused, which every voice client shows as said), the status
carries a "maintenance" block only while it is on, and everything else stays open.
"""
import argparse

import pytest
from httpx import ASGITransport, AsyncClient

import engine.config as engine_config
from engine import coordinator
from engine import chat_page

REMOTE = ("203.0.113.9", 40000)


def client(peer=REMOTE):
    return AsyncClient(transport=ASGITransport(app=coordinator.app, client=peer), base_url="http://test")


@pytest.fixture
def maint(monkeypatch):
    state = {"maintenance": True, "maintenance_message": "", "maintenance_until": ""}
    real = coordinator.get_worker_limits
    monkeypatch.setattr(coordinator, "get_worker_limits", lambda: {**real(), **state})
    monkeypatch.setattr(coordinator, "_maintenance_since", None)
    return state


@pytest.mark.anyio
@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/completions", "/v1/app/chat/completions",
                                  "/v1/images", "/v1/app/images", "/v1/models/request"])
async def test_new_work_is_answered_with_the_message_as_a_429(maint, path):
    async with client() as c:
        reply = await c.post(path, json={"messages": [{"role": "user", "content": "hi"}]})
    assert reply.status_code == 429 and reply.headers["retry-after"] == "120"
    body = reply.json()
    assert body["code"] == "maintenance" and body["detail"] == coordinator.MAINTENANCE_DEFAULT
    assert body["maintenance"]["message"] == coordinator.MAINTENANCE_DEFAULT


@pytest.mark.anyio
async def test_a_voice_clip_gets_voice_paused_with_the_message_so_no_client_says_used_up(maint):
    maint["maintenance_message"] = "Re-laying the GPUs."
    async with client() as c:
        reply = await c.post("/v1/audio/transcriptions", files={"file": ("clip.wav", b"RIFF", "audio/wav")})
    assert reply.status_code == 503 and reply.json()["code"] == "voice_paused"
    assert reply.json()["detail"] == "Re-laying the GPUs."


@pytest.mark.anyio
async def test_the_coding_route_is_closed_too(maint):
    async with client() as c:
        reply = await c.get("/v1/coding/route")
    assert reply.status_code == 429 and reply.json()["code"] == "maintenance"


@pytest.mark.anyio
async def test_the_status_says_maintenance_with_the_owners_words_and_when(maint):
    maint.update(maintenance_message="Benchmarking new models on TheWall.", maintenance_until="about 1 hour")
    async with client() as c:
        reply = await c.get("/v1/cluster/status")
    assert reply.status_code == 200
    m = reply.json()["maintenance"]
    assert m["active"] is True and m["message"] == "Benchmarking new models on TheWall." and m["until"] == "about 1 hour"
    assert m["since"].endswith("Z")


@pytest.mark.anyio
async def test_off_the_status_is_unchanged_and_nothing_is_answered_with_maintenance(maint):
    maint["maintenance"] = False
    async with client() as c:
        status = await c.get("/v1/cluster/status")
        chat = await c.post("/v1/app/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
    assert "maintenance" not in status.json()
    assert chat.status_code != 429 or chat.json().get("code") != "maintenance"


def test_what_stays_open():
    closes = coordinator.maintenance_closes
    assert closes("POST", "/v1/chat/completions") and closes("POST", "/v1/app/images")
    assert not closes("GET", "/v1/app/images/abc"), "a picture already made can still be read"
    assert not closes("POST", "/v1/images/abc/cancel") and not closes("POST", "/v1/app/images/abc/cancel")
    for path in ("/v1/cluster/status", "/v1/release", "/worker/heartbeat", "/worker/enroll", "/v1/admin/stats",
                 "/v1/admin/takeover", "/v1/me", "/v1/models", "/v1/download/rpc-bin", "/v1/app/report", "/chat"):
        assert not closes("POST", path) and not closes("GET", path), path


def test_the_config_keys(isolated_config):
    def limits(text):
        isolated_config.write_text(text)
        engine_config._cached_stamp = None
        return engine_config.get_plain_worker_limits()
    assert limits("")["maintenance"] is False
    on = limits("MAINTENANCE=true\nMAINTENANCE_MESSAGE=  Re-laying   the GPUs. \nMAINTENANCE_UNTIL=about 1 hour\n")
    assert (on["maintenance"], on["maintenance_message"], on["maintenance_until"]) == (True, "Re-laying the GPUs.", "about 1 hour")
    assert limits("MAINTENANCE=yes\n")["maintenance"] is False, "only the literal true closes the doors"
    assert len(limits("MAINTENANCE_MESSAGE=" + "x" * 900 + "\n")["maintenance_message"]) == 400


def test_the_on_and_off_command_writes_config(monkeypatch):
    import dlne_server
    written = []
    monkeypatch.setattr(dlne_server, "set_config_values", lambda values: written.append(values) or True)
    dlne_server.cmd_maintenance(argparse.Namespace(maintenance_action="on", message=" Back  soon ", until="14:30"))
    dlne_server.cmd_maintenance(argparse.Namespace(maintenance_action="off", message=None, until=None))
    assert written == [{"MAINTENANCE": "true", "MAINTENANCE_MESSAGE": "Back soon", "MAINTENANCE_UNTIL": "14:30"},
                       {"MAINTENANCE": "false", "MAINTENANCE_MESSAGE": "", "MAINTENANCE_UNTIL": ""}]


def test_the_web_chat_draws_the_card_from_the_status():
    page = chat_page.CHAT_HTML
    assert 'id="maint"' in page and 'role="alertdialog"' in page and "DLNE is being upgraded" in page
    assert "showMaintenance(data && data.maintenance);" in page
    assert "setInterval(() => refreshStatus(), 15000)" in page and "clearInterval(maintTimer)" in page
    assert "ui.maintMsg.textContent" in page, "the owner's words go in as text, never as HTML"
