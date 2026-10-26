"""
A worker whose computer clock is wrong still joins (2.4.6, engine/swarm_clock.py).

Signed timestamps are Unix epoch seconds, the same in every time zone, and the coordinator refuses anything more
than 30 s from its own clock. A volunteer whose clock was off (no time sync; a dual-boot PC whose hardware clock
Windows keeps in local time and Linux reads as UTC, which is off by the UTC offset; a wrong time zone) was refused
at the first heartbeat and stopped, told that the network key was wrong. Now the worker learns the coordinator's
time from its answers and signs with it; the coordinator's 30 s window is unchanged.
"""

import json
import time
from email.utils import formatdate

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from engine import authn, swarm_clock as sc, worker
from engine.swarm_clock import SwarmClock, describe, looks_like_time_zone, server_time_from
from engine.version import VERSION

HOUR = 3600.0
KEY = "k" * 64


@pytest.fixture(autouse=True)
def fresh_clock():
    sc.swarm_clock.reset()
    yield
    sc.swarm_clock.reset()


# ----------------------------------------------------------------------------------- what the owner is told

@pytest.mark.parametrize("offset, words", [
    (5 * HOUR, "5 h behind"),                 # New York in winter, a dual-boot PC
    (-8 * HOUR, "8 h ahead of"),              # Taipei
    (5.5 * HOUR, "5 h 30 min behind"),        # India
    (5.75 * HOUR, "5 h 45 min behind"),       # Nepal
    (-(1 * HOUR + 20), "1 h ahead of"),       # a zone, with 20 s of drift on top
])
def test_whole_time_zone_offsets_are_named_as_a_time_zone(offset, words):
    text = describe(offset)
    assert words in text
    assert "time zone" in text and "Windows keeps the hardware clock in local time" in text
    assert "DLNE corrects for it" in text


@pytest.mark.parametrize("offset, words", [(192, "3 min 12 s behind"), (-45, "45 s ahead of"),
                                           (HOUR + 7 * 60, "1 h 7 min behind")])
def test_drift_is_named_as_drift(offset, words):
    text = describe(offset)
    assert words in text and "automatic time sync" in text and "time zone" not in text


def test_inside_the_coordinators_window_nothing_is_said():
    assert describe(29) == describe(-29.9) == describe(float("nan")) == ""
    assert not looks_like_time_zone(20 * 60) and looks_like_time_zone(30 * 60) and looks_like_time_zone(-14 * HOUR)


# ----------------------------------------------------------------------------------- reading the coordinator's time

def test_the_exact_header_wins_and_any_case_is_read():
    now = time.time()
    assert server_time_from(httpx.Headers({"X-DLNE-Time": f"{now:.3f}", "Date": formatdate(0, usegmt=True)})) == \
        pytest.approx(now, abs=0.001)
    assert server_time_from({"x-dlne-time": f"{now:.3f}"}) == pytest.approx(now, abs=0.001)


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="time.tzset is POSIX only")
@pytest.mark.parametrize("zone", ["UTC", "Asia/Taipei", "America/New_York", "Asia/Kathmandu", "Pacific/Chatham"])
def test_the_date_header_is_read_as_gmt_whatever_this_machines_time_zone(zone, monkeypatch):
    # HTTP's Date is always GMT. Read as local time it would be off by the zone's offset.
    moment = 1_792_980_000.0
    header = formatdate(moment, usegmt=True)
    monkeypatch.setenv("TZ", zone)
    time.tzset()
    try:
        assert server_time_from({"Date": header}) == pytest.approx(moment + 0.5, abs=0.001)
        # A Date written with an explicit offset means the same moment.
        assert server_time_from({"Date": formatdate(moment, localtime=True)}) == pytest.approx(moment + 0.5, abs=0.001)
    finally:
        monkeypatch.undo()
        time.tzset()


@pytest.mark.parametrize("headers", [{}, {"X-DLNE-Time": "nope"}, {"X-DLNE-Time": "0"}, {"X-DLNE-Time": "nan"},
                                     {"Date": "yesterday"}, {"X-DLNE-Time": "1e12"}, None, "junk"])
def test_no_usable_time_teaches_nothing(headers):
    clock = SwarmClock()
    assert server_time_from(headers) is None
    assert clock.learn_from(headers, 100.0, 100.1) is False
    assert clock.offset == 0.0


def test_the_offset_is_measured_from_the_middle_of_the_round_trip():
    clock = SwarmClock(clock=lambda: 1_792_980_000.0)
    assert clock.learn(1_792_980_000.0 + 5 * HOUR, sent_at=1_792_979_999.0, received_at=1_792_980_001.0)
    assert clock.offset == pytest.approx(5 * HOUR)
    assert clock.now() == pytest.approx(1_792_980_000.0 + 5 * HOUR)
    assert clock.learn(1_792_990_000.0, sent_at=100.0, received_at=111.0) is False, "an answer that took 11 s says too little"


def test_the_owner_is_told_once_per_change():
    clock = SwarmClock(clock=lambda: 1_792_980_000.0)
    clock.learn(1_792_980_000.0 + 5 * HOUR, 1_792_980_000.0, 1_792_980_000.0)
    assert "5 h behind" in clock.report()
    clock.learn(1_792_980_000.0 + 5 * HOUR + 3, 1_792_980_000.0, 1_792_980_000.0)
    assert clock.report() == "", "3 s more is not news"
    clock.learn(1_792_980_000.0 + 1, 1_792_980_000.0, 1_792_980_000.0)       # the owner fixed the clock
    assert clock.report() == ""
    clock.learn(1_792_980_000.0 - 8 * HOUR, 1_792_980_000.0, 1_792_980_000.0)
    assert "8 h ahead of" in clock.report()


# ----------------------------------------------------------------------------------- end to end with the real signatures

def _signed_heartbeat(timestamp):
    nonce = "a" * 32
    signature = authn.sign_heartbeat(KEY, "Worker-ABCD", 50052, "t" * 64, timestamp, nonce, VERSION, {"ram_gb": 8.0},
                                     "worker", "")
    return authn.verify_heartbeat(KEY, "Worker-ABCD", 50052, "t" * 64, timestamp, nonce, signature,
                                  VERSION, {"ram_gb": 8.0}, "worker", "")


@pytest.mark.parametrize("wrong_by", [5 * HOUR, -8 * HOUR, 5.75 * HOUR, 95.0, -400.0])
def test_a_clock_that_is_off_is_refused_until_it_learns_the_coordinators_time(wrong_by):
    # This computer's clock reads wrong_by seconds behind (or ahead of) the coordinator's, which verifies with the real time.
    clock = SwarmClock(clock=lambda: time.time() - wrong_by)
    assert _signed_heartbeat(clock.now()) == (False, "stale timestamp")
    sent = clock.local()
    assert clock.learn_from(httpx.Headers({"X-DLNE-Time": f"{time.time():.3f}"}), sent, clock.local())
    assert _signed_heartbeat(clock.now()) == (True, "ok")


def test_the_window_itself_is_unchanged():
    assert _signed_heartbeat(time.time() - 31) == (False, "stale timestamp")
    assert _signed_heartbeat(time.time() + 31) == (False, "stale timestamp")


# ----------------------------------------------------------------------------------- the worker

PROFILE = {"is_yielding": False, "ram_gb": 32.0, "cpu_cores": 8, "cpu_threads": 16,
           "gpu": {"vram_total_gb": 8.0, "name": "Test GPU", "api": "vulkan"}}


@pytest.fixture
def node(monkeypatch):
    monkeypatch.setattr(worker, "get_hardware_profile", lambda: dict(PROFILE))
    monkeypatch.setattr(worker, "_write_token_to_disk", lambda token: None)
    monkeypatch.setattr(worker, "HEARTBEAT_INTERVAL_SECONDS", 0.01)
    n = worker.WorkerNode("http://coordinator:11435")
    n.is_running = True
    return n


def _coordinator(node, monkeypatch, replies, ahead_by):
    """Answers heartbeats with scripted (status, detail) replies from a coordinator whose clock is ahead_by s ahead."""
    sent = []

    def fake_post(url, json=None, timeout=None):
        sent.append(json)
        status, detail = replies[min(len(sent), len(replies)) - 1]
        headers = {"X-DLNE-Time": f"{time.time() + ahead_by:.3f}"}
        response = httpx.Response(status, headers=headers, json={"detail": detail} if detail else {},
                                  request=httpx.Request("POST", url))
        if len(sent) >= len(replies) + 1:
            node.request_shutdown(0, "test finished")
        return response

    monkeypatch.setattr(worker.httpx, "post", fake_post)
    return sent


def test_a_stale_refusal_is_corrected_and_sent_again_at_once_not_fatal(node, monkeypatch, capsys):
    sent = _coordinator(node, monkeypatch, [(401, "Heartbeat rejected: stale timestamp"), (200, None)], ahead_by=5 * HOUR)
    started = time.time()
    node.heartbeat_loop()
    assert node.exit_code == 0, "the worker kept going"
    assert len(sent) >= 2
    assert sent[0]["timestamp"] == pytest.approx(started, abs=5), "the first beat used this computer's own clock"
    assert sent[1]["timestamp"] == pytest.approx(started + 5 * HOUR, abs=5), "the next one, the coordinator's"
    out = capsys.readouterr().out
    marker = [line for line in out.splitlines() if line.startswith(worker.CLOCK_MARKER)]
    assert marker and json.loads(marker[0][len(worker.CLOCK_MARKER):])["offset_seconds"] == pytest.approx(5 * HOUR, abs=5)


def test_a_clock_that_keeps_jumping_ends_with_a_clock_message_not_a_key_message(node, monkeypatch):
    said = []
    monkeypatch.setattr(worker, "log_error", lambda text: said.append(text))
    monkeypatch.setattr(worker, "log_system", lambda text: said.append(text))
    sent = _coordinator(node, monkeypatch, [(401, "Heartbeat rejected: stale timestamp")] * 10, ahead_by=0)
    node.heartbeat_loop()
    assert node.exit_code == worker.FATAL_EXIT_CODE
    assert len(sent) == worker.MAX_STALE_RETRIES + 1
    assert any("clock" in text for text in said)
    assert not any("NETWORK_KEY" in text for text in said), "not blamed on the network key"


def test_other_credential_refusals_are_still_fatal_at_once(node, monkeypatch):
    sent = _coordinator(node, monkeypatch, [(401, "Heartbeat rejected: bad signature")], ahead_by=0)
    node.heartbeat_loop()
    assert node.exit_code == worker.FATAL_EXIT_CODE and len(sent) == 1


def test_the_coding_members_identity_block_is_signed_with_the_coordinators_time(tmp_path):
    from engine import member_identity
    from engine.coding_member import CodingMember
    from tests.coding_member_fakes import free_port

    class Ring:
        public_b64 = "cHVi"

        def maybe_rotate(self):
            return False

    sc.swarm_clock.learn(time.time() + 8 * HOUR, time.time(), time.time())
    identity = member_identity.load_or_create_identity(str(tmp_path / "id.key"))
    member = CodingMember(str(tmp_path), lambda: [], port=free_port(), keyring=Ring(), identity=identity)
    block = member.status()
    assert block["signed_at"] == pytest.approx(time.time() + 8 * HOUR, abs=5)
    assert member_identity.verify_block(block, now=time.time() + 8 * HOUR) is not None


# ----------------------------------------------------------------------------------- the coordinator

@pytest.mark.anyio
async def test_every_answer_to_a_worker_carries_the_coordinators_time():
    import engine.coordinator as coordinator

    async with AsyncClient(transport=ASGITransport(app=coordinator.app), base_url="http://test") as client:
        refused = await client.post("/worker/heartbeat", json={"worker_id": "x"})
        other = await client.get("/v1/release")
    assert refused.status_code >= 400
    assert float(refused.headers["X-DLNE-Time"]) == pytest.approx(time.time(), abs=5), "a refusal carries it too"
    assert "X-DLNE-Time" not in other.headers, "only answers to workers"
