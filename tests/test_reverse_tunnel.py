"""
The reverse RPC tunnel, end to end over real sockets: a coordinator endpoint served by
uvicorn, the worker's real tunnel client, and an echo server standing in for ggml-rpc-server.

Before 2.1.4 /ws/rpc_tunnel/{worker_id} admitted anyone. Connecting under a worker's id closed
that worker's tunnel, and a hundred connections used up every relay port. Now only the worker
the registry holds the token for may open its tunnel, keyed by its registered address, and one
source address may hold at most MAX_TUNNELS_PER_SOURCE of them.
"""

import socket
import threading
import time

import pytest
import uvicorn
from fastapi import FastAPI, WebSocket
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect as ws_connect

from engine import coordinator, reverse_tunnel


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(predicate, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def echo_server():
    """A TCP echo server: the stand-in for a worker's ggml-rpc-server."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    stop = threading.Event()

    def serve():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                continue

            def echo(c):
                with c:
                    while True:
                        data = c.recv(65536)
                        if not data:
                            return
                        c.sendall(data)

            threading.Thread(target=echo, args=(conn,), daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()
    yield srv.getsockname()[1]
    stop.set()
    srv.close()


@pytest.fixture
def coord(monkeypatch):
    """The real tunnel endpoint on a real port, with a registry of known workers."""
    workers = {}
    monkeypatch.setattr(coordinator.registry, "workers", workers)
    monkeypatch.setattr(coordinator, "get_worker_limits", lambda: {"trust_proxy_headers": False})
    bridge = reverse_tunnel.ReverseRpcBridgeManager()
    monkeypatch.setattr(reverse_tunnel, "reverse_bridge", bridge)

    app = FastAPI()
    app.add_api_websocket_route("/ws/rpc_tunnel/{worker_id}", coordinator.rpc_tunnel_endpoint)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    assert _wait(lambda: server.started), "test coordinator did not start"

    def register(worker_id, token, ip="198.51.100.20", rpc_port=50052):
        workers[worker_id] = {"worker_id": worker_id, "ip": ip, "port": rpc_port, "token": token,
                              "last_seen": time.time(), "hardware": {}}

    yield {"url": f"http://127.0.0.1:{port}", "ws": f"ws://127.0.0.1:{port}", "bridge": bridge,
           "register": register}
    server.should_exit = True
    thread.join(timeout=5)


def _round_trip(relay_port: int, payload: bytes = b"tensor bytes") -> bytes:
    with socket.create_connection(("127.0.0.1", relay_port), timeout=5) as s:
        s.sendall(payload)
        got = b""
        s.settimeout(5)
        while len(got) < len(payload):
            chunk = s.recv(65536)
            if not chunk:
                break
            got += chunk
        return got


def _open(coord, worker_id, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else None
    return ws_connect(f"{coord['ws']}/ws/rpc_tunnel/{worker_id}?port=50052",
                      additional_headers=headers, open_timeout=5)


def test_an_authenticated_worker_tunnel_carries_rpc_traffic(coord, echo_server):
    coord["register"]("w1", "tok-w1")
    stop = threading.Event()
    reverse_tunnel.start_worker_reverse_tunnel(coord["url"], "w1", echo_server, stop,
                                               token_provider=lambda: "tok-w1")
    try:
        # Keyed by the worker's REGISTERED address, which is what the engine is told to dial.
        assert _wait(lambda: coord["bridge"].get_relay_port("198.51.100.20", 50052) is not None)
        relay = coord["bridge"].get_relay_port("198.51.100.20", 50052)
        assert _round_trip(relay) == b"tensor bytes"
    finally:
        stop.set()


def test_a_tunnel_without_a_token_is_refused(coord):
    coord["register"]("w1", "tok-w1")
    with pytest.raises(InvalidStatus) as refused:
        _open(coord, "w1")
    assert refused.value.response.status_code == 403
    assert coord["bridge"].get_transfer_telemetry()["active_tunnels"] == 0


def test_another_workers_token_does_not_open_this_tunnel(coord):
    coord["register"]("w1", "tok-w1")
    coord["register"]("w2", "tok-w2", ip="198.51.100.21")
    with pytest.raises(InvalidStatus):
        _open(coord, "w1", token="tok-w2")
    with pytest.raises(InvalidStatus):
        _open(coord, "w1", token="made-up")


def test_an_impostor_cannot_knock_a_worker_offline(coord, echo_server):
    """The attack: connect under a live worker's id. It used to close the real tunnel."""
    coord["register"]("w1", "tok-w1")
    stop = threading.Event()
    reverse_tunnel.start_worker_reverse_tunnel(coord["url"], "w1", echo_server, stop,
                                               token_provider=lambda: "tok-w1")
    try:
        assert _wait(lambda: coord["bridge"].get_relay_port("198.51.100.20", 50052) is not None)
        relay = coord["bridge"].get_relay_port("198.51.100.20", 50052)
        for token in (None, "guess"):
            with pytest.raises(InvalidStatus):
                _open(coord, "w1", token=token)
        assert coord["bridge"].get_relay_port("198.51.100.20", 50052) == relay
        assert _round_trip(relay, b"still here") == b"still here"
    finally:
        stop.set()


def test_the_caller_cannot_choose_the_address_its_tunnel_answers_for(coord):
    """?port= used to pick the key; a worker could claim any port on its address."""
    coord["register"]("w1", "tok-w1", ip="198.51.100.20", rpc_port=50052)
    with _open(coord, "w1", token="tok-w1"):
        assert _wait(lambda: coord["bridge"].get_relay_port("198.51.100.20", 50052) is not None)
        assert coord["bridge"].get_relay_port("127.0.0.1", 50052) is None


def test_one_address_cannot_take_every_relay_port(coord):
    cap = reverse_tunnel.MAX_TUNNELS_PER_SOURCE
    for i in range(cap + 1):
        coord["register"](f"w{i}", f"tok-{i}", ip=f"198.51.100.{30 + i}")
    held = []
    try:
        for i in range(cap):
            held.append(_open(coord, f"w{i}", token=f"tok-{i}"))
        assert _wait(lambda: coord["bridge"].get_transfer_telemetry()["active_tunnels"] == cap)
        with pytest.raises(InvalidStatus):
            _open(coord, f"w{cap}", token=f"tok-{cap}")
    finally:
        for conn in held:
            conn.close()


def test_a_worker_reconnecting_replaces_its_own_tunnel_within_the_cap(coord):
    """Its own old session does not count against it, or a reconnect at the cap would fail."""
    cap = reverse_tunnel.MAX_TUNNELS_PER_SOURCE
    for i in range(cap):
        coord["register"](f"w{i}", f"tok-{i}", ip=f"198.51.100.{30 + i}")
    held = [_open(coord, f"w{i}", token=f"tok-{i}") for i in range(cap)]
    try:
        assert _wait(lambda: coord["bridge"].get_transfer_telemetry()["active_tunnels"] == cap)
        held.append(_open(coord, "w0", token="tok-0"))
        assert _wait(lambda: coord["bridge"].get_transfer_telemetry()["active_tunnels"] == cap)
    finally:
        for conn in held:
            conn.close()


# --- PING/PONG: how far a worker reached only through its tunnel is (2.4.9) ------------------------------

def test_the_coordinator_times_a_worker_through_its_tunnel(coord, echo_server):
    coord["register"]("w1", "tok-w1")
    stop = threading.Event()
    reverse_tunnel.start_worker_reverse_tunnel(coord["url"], "w1", echo_server, stop,
                                               token_provider=lambda: "tok-w1")
    try:
        assert _wait(lambda: coord["bridge"].get_relay_port("198.51.100.20", 50052) is not None)
        rtt = coord["bridge"].measure_rtt_ms("198.51.100.20", 50052)
        assert rtt is not None and 0.0 <= rtt < 1000.0
        # Pings share the tunnel with RPC traffic and leave it working.
        assert _round_trip(coord["bridge"].get_relay_port("198.51.100.20", 50052)) == b"tensor bytes"
    finally:
        stop.set()


def test_a_worker_that_does_not_answer_pings_is_not_measured(coord):
    """A worker from before 2.4.9 ignores the PING frame: one unanswered ping, not three, and no figure."""
    coord["register"]("w1", "tok-w1")
    with _open(coord, "w1", token="tok-w1"):
        assert _wait(lambda: coord["bridge"].get_relay_port("198.51.100.20", 50052) is not None)
        started = time.monotonic()
        assert coord["bridge"].measure_rtt_ms("198.51.100.20", 50052, timeout=0.3) is None
        assert time.monotonic() - started < 2.0


def test_no_tunnel_means_no_measurement(coord):
    assert coord["bridge"].measure_rtt_ms("198.51.100.20", 50052) is None


def test_a_tunnel_busy_uploading_weights_is_not_timed(coord, echo_server, monkeypatch):
    """A ping queued behind megabytes of weights would make a LAN machine look far."""
    coord["register"]("w1", "tok-w1")
    stop = threading.Event()
    reverse_tunnel.start_worker_reverse_tunnel(coord["url"], "w1", echo_server, stop,
                                               token_provider=lambda: "tok-w1")
    try:
        assert _wait(lambda: coord["bridge"].get_relay_port("198.51.100.20", 50052) is not None)
        monkeypatch.setattr(reverse_tunnel._WorkerTunnelSession, "get_speed_mb_s", lambda self: 40.0)
        assert coord["bridge"].measure_rtt_ms("198.51.100.20", 50052) is None
    finally:
        stop.set()


def test_a_household_worker_is_dialled_on_its_lan_address(monkeypatch, echo_server):
    from engine import link_paths
    routes = link_paths.LanRoutes()
    monkeypatch.setattr(link_paths, "lan_routes", routes)
    bridge = reverse_tunnel.ReverseRpcBridgeManager()
    public = f"198.51.100.20:{echo_server}"                      # the household's public address: unreachable
    routes.offer("198.51.100.20", echo_server, ["127.0.0.1"])     # 127.0.0.1 standing in for 192.168.x.y
    assert bridge.resolve_endpoint(public, probe_timeout=0.2) == public, "offered but never confirmed"
    routes.confirm("198.51.100.20", echo_server, "127.0.0.1")
    assert bridge.resolve_endpoint(public, probe_timeout=0.2) == f"127.0.0.1:{echo_server}"


# --- the tunnel over the coordinator's LAN address (2.5.2) -------------------------------------------------
# The owner's laptop: its nftables dropped the coordinator's dial to :50052, and its tunnel through the public
# address (router hairpin, TLS) ran at ~24 MB/s against ~290 MB/s on the LAN, too slow to load a shard in time.

def _relay_works(coord) -> bool:
    relay = coord["bridge"].get_relay_port("198.51.100.20", 50052)
    try:
        return relay is not None and _round_trip(relay) == b"tensor bytes"
    except OSError:
        return False


@pytest.fixture
def lan_coord(coord):
    """A second door to the same coordinator (its LAN address), counting the tunnels that come through it."""
    hits = []

    async def endpoint(websocket: WebSocket, worker_id: str):
        hits.append(worker_id)
        await coordinator.rpc_tunnel_endpoint(websocket, worker_id)

    app = FastAPI()
    app.add_api_websocket_route("/ws/rpc_tunnel/{worker_id}", endpoint)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    assert _wait(lambda: server.started)
    yield {"ws": f"ws://127.0.0.1:{port}", "hits": hits}
    server.should_exit = True
    thread.join(timeout=5)


def test_a_household_worker_tunnels_to_the_coordinators_lan_address(coord, lan_coord, echo_server):
    coord["register"]("w1", "tok-w1")
    stop = threading.Event()
    dead_public = f"http://127.0.0.1:{_free_port()}"
    reverse_tunnel.start_worker_reverse_tunnel(dead_public, "w1", echo_server, stop, token_provider=lambda: "tok-w1",
                                               lan_url_provider=lambda: lan_coord["ws"])
    try:
        assert _wait(lambda: _relay_works(coord))
        assert lan_coord["hits"] == ["w1"]
    finally:
        stop.set()


def test_a_lan_address_that_does_not_answer_leaves_the_tunnel_on_the_public_one(coord, echo_server):
    coord["register"]("w1", "tok-w1")
    stop = threading.Event()
    dead_lan = f"ws://127.0.0.1:{_free_port()}"
    reverse_tunnel.start_worker_reverse_tunnel(coord["url"], "w1", echo_server, stop, token_provider=lambda: "tok-w1",
                                               lan_url_provider=lambda: dead_lan)
    try:
        assert _wait(lambda: _relay_works(coord))
    finally:
        stop.set()


def test_an_idle_tunnel_moves_to_the_lan_but_never_cuts_a_running_stream(coord, lan_coord, echo_server, monkeypatch):
    monkeypatch.setattr(reverse_tunnel, "MOVE_CHECK_SECONDS", 0.1)
    coord["register"]("w1", "tok-w1")
    lan = {"url": None}                        # the first answers carry no LAN address yet
    stop = threading.Event()
    reverse_tunnel.start_worker_reverse_tunnel(coord["url"], "w1", echo_server, stop, token_provider=lambda: "tok-w1",
                                               lan_url_provider=lambda: lan["url"])
    try:
        assert _wait(lambda: coord["bridge"].get_relay_port("198.51.100.20", 50052) is not None)
        relay = coord["bridge"].get_relay_port("198.51.100.20", 50052)
        with socket.create_connection(("127.0.0.1", relay), timeout=5) as running:   # weights on their way
            running.sendall(b"x")
            assert running.recv(1) == b"x"
            lan["url"] = lan_coord["ws"]
            time.sleep(0.6)
            assert lan_coord["hits"] == [], "a stream was running: the tunnel stays where it is"
        assert _wait(lambda: lan_coord["hits"] == ["w1"])
        assert _wait(lambda: _relay_works(coord))
    finally:
        stop.set()


@pytest.mark.parametrize("version, key", [("17.3", "additional_headers"), ("14.0", "additional_headers"),
                                          ("13.1", "extra_headers"), ("12.0", "extra_headers")])
def test_the_token_header_uses_the_installed_websockets_spelling(version, key):
    assert reverse_tunnel._auth_header_kwargs(version, "abc") == {key: {"Authorization": "Bearer abc"}}
    assert reverse_tunnel._auth_header_kwargs(version, "") == {}
