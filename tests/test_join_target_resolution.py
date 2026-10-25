"""
Tests for finding the Coordinator a join file names, when its address cannot be dialled.

THE PROBLEM
-----------
One join file is handed to every tester, so it carries the Coordinator's *public* address.
A node behind the same router as the Coordinator cannot use that address: reaching your own
public IP from inside the network needs NAT hairpinning, which many routers refuse. The node
is one hop away and cannot dial it, and what the operator sees is "enrollment failed".

The fallback listens for the Coordinator's LAN discovery beacon instead. That beacon is a
redirection primitive - it tells a node where to send everything - so the rules it is
accepted under are the whole security story, and they are what these tests hold down:

  * signed with THIS join file's key, or ignored;
  * no key in the join file means no fallback at all;
  * only when the named address could not be OPENED - a Coordinator that answered and then
    said no has made a decision, and the node does not go shopping for a better answer;
  * a beacon that fails validation does not end the search, so one stray packet on a busy
    LAN cannot deny a node its Coordinator.
"""

import socket

import pytest

import engine.setup_core as setup_core
from engine.discovery import DISCOVERY_PORT, build_discovery_beacon


NETWORK_KEY = "b8e1f0c9a7d24e3f8a6b5c4d3e2f1a0b9c8d7e6f5a4b3c2d1e0f9a8b7c6d5e4f"
OTHER_KEY = "0" * 64

PUBLIC_JOIN = {
    "coordinator_host": "203.0.113.9",
    "coordinator_port": "11435",
    "network_key": NETWORK_KEY,
}


@pytest.fixture
def unreachable(monkeypatch):
    """The Coordinator's named address refuses every connection."""
    monkeypatch.setattr(setup_core, "coordinator_is_reachable", lambda *a, **k: False)


@pytest.fixture
def reachable(monkeypatch):
    monkeypatch.setattr(setup_core, "coordinator_is_reachable", lambda *a, **k: True)


def _beacon_from(monkeypatch, ip, port=11435, key=NETWORK_KEY):
    """Make discovery answer with one beacon signed by ``key``."""
    def fake(timeout=2.0, network_key=None):
        from engine.discovery import parse_discovery_beacon

        message = build_discovery_beacon(port, key)
        got = parse_discovery_beacon(message, network_key or "")
        return (ip, str(got)) if got is not None else (None, None)

    monkeypatch.setattr(setup_core, "discover_coordinator", fake)


# --------------------------------------------------------------------------------------
# The route that is taken
# --------------------------------------------------------------------------------------

def test_a_reachable_address_is_used_as_written(reachable):
    result = setup_core.resolve_join_target(PUBLIC_JOIN)

    assert result["ok"] is True
    assert result["via"] == "direct"
    assert (result["host"], result["port"]) == ("203.0.113.9", "11435")


def test_an_unreachable_address_falls_back_to_the_signed_beacon(unreachable, monkeypatch):
    """The case a node behind the Coordinator's own router actually hits."""
    _beacon_from(monkeypatch, "192.168.40.23")

    result = setup_core.resolve_join_target(PUBLIC_JOIN)

    assert result["ok"] is True
    assert result["via"] == "lan"
    assert result["host"] == "192.168.40.23"


def test_the_caller_can_tell_the_two_routes_apart(unreachable, monkeypatch):
    """
    Not cosmetic. A node that found its Coordinator on the LAN must talk to it there
    rather than build a tunnel to a public address this network cannot reach - which is
    the very thing that just failed.
    """
    _beacon_from(monkeypatch, "192.168.40.23")

    assert setup_core.resolve_join_target(PUBLIC_JOIN)["via"] == "lan"


# --------------------------------------------------------------------------------------
# What the fallback refuses
# --------------------------------------------------------------------------------------

def test_a_beacon_signed_with_another_key_is_not_followed(unreachable, monkeypatch):
    """
    The attack this rules out: a host on the same network broadcasting its own beacon to
    become the Coordinator, and with it the destination of every prompt typed into the
    WebUI. Only a holder of this cluster's secret can produce a beacon this accepts.
    """
    _beacon_from(monkeypatch, "192.168.40.99", key=OTHER_KEY)

    result = setup_core.resolve_join_target(PUBLIC_JOIN)

    assert result["ok"] is False
    assert result["host"] == ""


def test_an_unsigned_beacon_is_not_followed(unreachable, monkeypatch):
    """An unsigned beacon is an open invitation to nominate yourself."""
    _beacon_from(monkeypatch, "192.168.40.99", key="")

    assert setup_core.resolve_join_target(PUBLIC_JOIN)["ok"] is False


def test_a_join_file_with_no_key_never_falls_back(unreachable, monkeypatch):
    """
    With no key there is nothing to verify a beacon against, so the fallback would have to
    accept an unsigned one. It does not run at all. parse_join_file already refuses these;
    this refuses them a second time rather than trusting that it always will.
    """
    called = []
    monkeypatch.setattr(setup_core, "discover_coordinator",
                        lambda *a, **k: called.append(1) or (None, None))

    result = setup_core.resolve_join_target({**PUBLIC_JOIN, "network_key": ""})

    assert result["ok"] is False
    assert called == [], "discovery must not even be attempted without a key to verify with"


def test_a_coordinator_that_answers_is_never_second_guessed(reachable, monkeypatch):
    """
    Reachability is the only trigger. A Coordinator that accepted the connection and then
    rejected the request has made a decision - looking for a different Coordinator on the
    LAN at that point would route around a refusal, which is the opposite of fail-safe.
    """
    called = []
    monkeypatch.setattr(setup_core, "discover_coordinator",
                        lambda *a, **k: called.append(1) or ("192.168.40.23", "11435"))

    result = setup_core.resolve_join_target(PUBLIC_JOIN)

    assert result["via"] == "direct"
    assert called == []


def test_no_beacon_at_all_is_a_clear_failure(unreachable, monkeypatch):
    monkeypatch.setattr(setup_core, "discover_coordinator", lambda *a, **k: (None, None))

    result = setup_core.resolve_join_target(PUBLIC_JOIN)

    assert result["ok"] is False
    assert "203.0.113.9" in result["error"]
    assert result["error"]


def test_a_join_file_with_no_host_is_refused(monkeypatch):
    called = []
    monkeypatch.setattr(setup_core, "coordinator_is_reachable",
                        lambda *a, **k: called.append(1) or True)

    result = setup_core.resolve_join_target({**PUBLIC_JOIN, "coordinator_host": ""})

    assert result["ok"] is False
    assert called == []


# --------------------------------------------------------------------------------------
# Reachability probing
# --------------------------------------------------------------------------------------

def test_a_listening_socket_is_reachable():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    try:
        _, port = server.getsockname()
        assert setup_core.coordinator_is_reachable("127.0.0.1", str(port), timeout=3.0) is True
    finally:
        server.close()


def test_a_closed_port_is_not_reachable():
    # Bound then closed, so the port is known to have nothing behind it.
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    _, port = probe.getsockname()
    probe.close()

    assert setup_core.coordinator_is_reachable("127.0.0.1", str(port), timeout=2.0) is False


def test_a_nonsense_port_is_not_reachable():
    assert setup_core.coordinator_is_reachable("127.0.0.1", "not-a-port") is False


def test_probing_sends_nothing():
    """
    A connect and nothing more. Anything sent here would be a request the Coordinator has
    to interpret, on a path whose only question is whether it can be spoken to at all.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(setup_core.coordinator_is_reachable).strip())
    function = tree.body[0]
    # The docstring explains the rule in prose; scanning it would be scanning the comment,
    # not the code. Drop it and read what the function actually does.
    body = ast.unparse(ast.Module(body=function.body[1:], type_ignores=[]))

    for forbidden in ("send", "sendall", "httpx", "request"):
        assert forbidden not in body, f"the probe must not {forbidden} anything"


# --------------------------------------------------------------------------------------
# Discovery robustness
# --------------------------------------------------------------------------------------

def test_one_junk_packet_does_not_end_the_search(monkeypatch):
    """
    This port carries other broadcast traffic. Discovery used to return on the first packet
    that failed validation, so an unrelated datagram arriving first made a Coordinator that
    was broadcasting perfectly well look absent - and on a busy LAN that is not rare.
    """
    packets = [
        (b"\xff\xfe not even utf-8", ("192.168.40.5", 1)),
        (b"DLNE-DISC|v1|garbage", ("192.168.40.6", 1)),
        (build_discovery_beacon(11435, NETWORK_KEY).encode(), ("192.168.40.23", 1)),
    ]

    class FakeSocket:
        def setsockopt(self, *a): pass
        def bind(self, *a): pass
        def settimeout(self, *a): pass
        def close(self): pass
        def recvfrom(self, _n):
            if not packets:
                raise socket.timeout()
            return packets.pop(0)

    monkeypatch.setattr(socket, "socket", lambda *a, **k: FakeSocket())

    ip, port = setup_core.discover_coordinator(timeout=5.0, network_key=NETWORK_KEY)

    assert (ip, port) == ("192.168.40.23", "11435")


def test_discovery_still_gives_up_when_only_junk_arrives(monkeypatch):
    """Waiting longer must not mean waiting forever."""
    class FakeSocket:
        def setsockopt(self, *a): pass
        def bind(self, *a): pass
        def settimeout(self, *a): pass
        def close(self): pass
        def recvfrom(self, _n):
            return b"DLNE-DISC|v1|garbage", ("192.168.40.6", 1)

    monkeypatch.setattr(socket, "socket", lambda *a, **k: FakeSocket())

    assert setup_core.discover_coordinator(timeout=0.3, network_key=NETWORK_KEY) == (None, None)


def test_the_discovery_port_is_unchanged():
    """The fallback listens where the Coordinator broadcasts, not somewhere new."""
    assert DISCOVERY_PORT == 11435


# The text assertions over the retired graphical and console wizards lived here. Both wizards
# are gone with the desktop app; the join-target resolution above is what remains.
