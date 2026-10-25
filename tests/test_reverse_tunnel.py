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
from fastapi import FastAPI
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


@pytest.mark.parametrize("version, key", [("17.3", "additional_headers"), ("14.0", "additional_headers"),
                                          ("13.1", "extra_headers"), ("12.0", "extra_headers")])
def test_the_token_header_uses_the_installed_websockets_spelling(version, key):
    assert reverse_tunnel._auth_header_kwargs(version, "abc") == {key: {"Authorization": "Bearer abc"}}
    assert reverse_tunnel._auth_header_kwargs(version, "") == {}
