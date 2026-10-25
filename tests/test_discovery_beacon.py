"""
Tests for the LAN discovery beacon — audit item 3.6.

WHY THIS MATTERS
    The portal is what decides where the browser's traffic goes, so whoever can convince
    it of a Coordinator address receives every prompt the user types. The beacon is signed
    with ``NETWORK_KEY`` — the *same* key that signs worker heartbeats — and it used to
    sign a bare ``DLNE_COORDINATOR:...`` string with no protocol tag in it. Two protocols
    sharing one key with no domain separation is a standing invitation for a cross-protocol
    signature reuse, and it is free to fix. The tag and version are now part of the signed
    message, not merely prepended to the packet.

    The freshness window is asserted here too, because it is the only thing stopping a
    captured beacon from being replayed forever.

``engine.discovery`` owns the one canonical implementation of both halves of the format;
``coordinator.udp_broadcaster`` (the sender) and ``setup_core.discover_coordinator`` (the join
flow's and the redundancy node's receiver) speak the same v1 wire format.
"""

import hashlib
import hmac
import time


import pytest

from engine.discovery import (
    DISCOVERY_MAX_SKEW_SECONDS,
    DISCOVERY_PROTOCOL_TAG,
    DISCOVERY_PROTOCOL_VERSION,
    build_discovery_beacon,
    parse_discovery_beacon,
)


NETWORK_KEY = "shared-cluster-secret"
API_PORT = 11435


# ======================================================================================
# The wire format itself
# ======================================================================================

def test_beacon_is_domain_separated_and_versioned():
    """
    The exact shape other DLNE components have to reproduce::

        DLNE-DISC|v1|DLNE_COORDINATOR:<api_port>:<unix_time>|<hex hmac-sha256>
    """
    beacon = build_discovery_beacon(API_PORT, NETWORK_KEY, timestamp=1700000000.0)
    fields = beacon.split("|")

    assert len(fields) == 4
    assert fields[0] == DISCOVERY_PROTOCOL_TAG == "DLNE-DISC"
    assert fields[1] == DISCOVERY_PROTOCOL_VERSION == "v1"
    assert fields[2] == f"DLNE_COORDINATOR:{API_PORT}:1700000000.0"

    # The signature covers the tag and version, not just the payload — that is the whole
    # point of the change, and it is what a hand-written sender gets wrong.
    signed_message = "|".join(fields[:3])
    expected = hmac.new(NETWORK_KEY.encode(), signed_message.encode(),
                        hashlib.sha256).hexdigest()
    assert fields[3] == expected


def test_unsigned_beacon_has_no_signature_field():
    """A Coordinator with no NETWORK_KEY still broadcasts, just without a signature."""
    beacon = build_discovery_beacon(API_PORT, "", timestamp=1700000000.0)

    assert beacon.split("|") == ["DLNE-DISC", "v1", f"DLNE_COORDINATOR:{API_PORT}:1700000000.0"]


# ======================================================================================
# Round-trip and rejection
# ======================================================================================

def test_signed_round_trip():
    """The everyday path: a correctly signed, fresh beacon yields the API port."""
    beacon = build_discovery_beacon(API_PORT, NETWORK_KEY)

    assert parse_discovery_beacon(beacon, NETWORK_KEY) == API_PORT


def test_a_beacon_signed_with_a_different_key_is_dropped():
    """Someone else's cluster, or an outright spoof."""
    beacon = build_discovery_beacon(API_PORT, "some-other-cluster-key")

    assert parse_discovery_beacon(beacon, NETWORK_KEY) is None


def test_an_unsigned_beacon_is_dropped_when_a_key_is_configured():
    """Stripping the signature must not downgrade a configured node to trusting anyone."""
    beacon = build_discovery_beacon(API_PORT, "")

    assert parse_discovery_beacon(beacon, NETWORK_KEY) is None


def test_tampering_with_the_port_breaks_the_signature():
    """The port is the field an attacker actually wants to rewrite."""
    beacon = build_discovery_beacon(API_PORT, NETWORK_KEY)
    tampered = beacon.replace(f":{API_PORT}:", ":9999:")

    assert tampered != beacon
    assert parse_discovery_beacon(tampered, NETWORK_KEY) is None


def test_a_stale_beacon_is_dropped():
    """
    Without this a single captured beacon could be replayed forever, pinning the portal to
    an address that is dead — or hostile.
    """
    stale = build_discovery_beacon(
        API_PORT, NETWORK_KEY, timestamp=time.time() - (DISCOVERY_MAX_SKEW_SECONDS + 5)
    )

    assert parse_discovery_beacon(stale, NETWORK_KEY) is None


def test_a_beacon_from_the_future_is_dropped():
    """The window is symmetric: a far-future timestamp is just as suspect."""
    ahead = build_discovery_beacon(
        API_PORT, NETWORK_KEY, timestamp=time.time() + (DISCOVERY_MAX_SKEW_SECONDS + 5)
    )

    assert parse_discovery_beacon(ahead, NETWORK_KEY) is None


# ======================================================================================
# Domain separation — the actual finding
# ======================================================================================

def test_the_legacy_untagged_format_no_longer_validates():
    """
    The pre-3.6 beacon — a bare payload signed with the same key and no protocol tag —
    must be rejected outright. This is the lockstep wire-format break; accepting it would
    leave the cross-protocol hole wide open while pretending it was closed.
    """
    payload = f"DLNE_COORDINATOR:{API_PORT}:{time.time()}"
    legacy = payload + "|" + hmac.new(NETWORK_KEY.encode(), payload.encode(),
                                      hashlib.sha256).hexdigest()

    assert parse_discovery_beacon(legacy, NETWORK_KEY) is None
    # Rejected even by a key-less receiver, because the tag is missing entirely.
    assert parse_discovery_beacon(legacy, "") is None


def test_a_heartbeat_signature_is_not_accepted_as_a_beacon():
    """
    Heartbeats are HMAC'd with the same NETWORK_KEY over a ``DLNE-HB|...`` string. The two
    protocols must not be able to be confused for one another in either direction.
    """
    heartbeat = "DLNE-HB|v2|worker-1|50052|token|1700000000.000|nonce|1.0|abcd"

    assert parse_discovery_beacon(heartbeat, NETWORK_KEY) is None
    assert parse_discovery_beacon(heartbeat, "") is None


@pytest.mark.parametrize("bad_tag", ["DLNE-DISK", "dlne-disc", "", "DLNE-DISC-EXTRA"])
def test_an_unknown_protocol_tag_is_dropped(bad_tag):
    """Unrelated broadcast traffic shares this UDP port on busy networks."""
    payload = f"DLNE_COORDINATOR:{API_PORT}:{time.time()}"
    message = f"{bad_tag}|v1|{payload}"

    assert parse_discovery_beacon(message, "") is None


def test_an_unknown_protocol_version_is_dropped():
    """A node from a different DLNE generation must be ignored, not guessed at."""
    payload = f"DLNE_COORDINATOR:{API_PORT}:{time.time()}"

    assert parse_discovery_beacon(f"DLNE-DISC|v2|{payload}", "") is None


# ======================================================================================
# Malformed input
# ======================================================================================

@pytest.mark.parametrize("message", [
    "",
    "DLNE-DISC",
    "DLNE-DISC|v1",
    "DLNE-DISC|v1|SOMETHING_ELSE:11435:1700000000",
    "DLNE-DISC|v1|DLNE_COORDINATOR:",
    "DLNE-DISC|v1|DLNE_COORDINATOR:not-a-port:1700000000",
    "DLNE-DISC|v1|DLNE_COORDINATOR:0:1700000000",
    "DLNE-DISC|v1|DLNE_COORDINATOR:70000:1700000000",
])
def test_malformed_beacons_are_dropped_without_raising(message):
    """
    The parser runs on raw UDP from anyone on the network, inside a loop whose death
    silently ends Coordinator discovery for the rest of the session — so it must never
    raise, only return None.
    """
    assert parse_discovery_beacon(message, "") is None
    assert parse_discovery_beacon(message, NETWORK_KEY) is None


def test_a_signed_beacon_without_a_timestamp_is_dropped():
    """
    The timestamp is mandatory. It was previously validated only ``if len(parts) >= 3``,
    so stripping the field skipped the replay check entirely.
    """
    signed_message = f"DLNE-DISC|v1|DLNE_COORDINATOR:{API_PORT}"
    signature = hmac.new(NETWORK_KEY.encode(), signed_message.encode(),
                         hashlib.sha256).hexdigest()

    assert parse_discovery_beacon(f"{signed_message}|{signature}", NETWORK_KEY) is None


# ======================================================================================
# Sender and receiver, end to end
#
# Everything above tests engine.discovery's producer against engine.discovery's consumer. That
# is a self-consistency check: it would still pass if the *actual* sender in the product —
# engine.coordinator.udp_broadcaster, a different module owned by a different person —
# emitted something else entirely. These tests drive the real broadcaster and feed what it
# puts on the wire to the real listener, which is the only thing that proves LAN discovery
# works. The failure mode they guard against is silent: beacons keep being sent, every
# receiver discards them, and nothing is logged anywhere.
# ======================================================================================

class _StopBroadcastLoop(Exception):
    """Sentinel used to escape udp_broadcaster's infinite loop after one cycle."""


def _run_one_broadcast(monkeypatch, network_key):
    """
    Runs exactly one iteration of the real ``udp_broadcaster`` and returns ``(payload, addr)``.

    The broadcaster is an endless loop around a real UDP socket, so both are replaced: a
    fake socket records what is handed to ``sendto``, and a fake ``time.sleep`` aborts the
    loop once a datagram has been emitted. Nothing about the message construction itself is
    stubbed — that is the part under test.
    """
    from engine import coordinator

    sent = []

    class FakeSocket:
        def __init__(self, *args, **kwargs):
            pass

        def setsockopt(self, *args):
            pass

        def sendto(self, data, addr):
            sent.append((data, addr))

        def close(self):
            pass

    monkeypatch.setattr(coordinator.socket, "socket", FakeSocket)
    monkeypatch.setattr(coordinator, "get_worker_limits",
                        lambda: {"network_key": network_key})

    # sleep() is called once before the loop and once at the end of each iteration, and
    # both calls sit outside the loop's try/except — so raising on the second one escapes
    # cleanly with exactly one datagram sent.
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise _StopBroadcastLoop

    monkeypatch.setattr(coordinator.time, "sleep", fake_sleep)

    with pytest.raises(_StopBroadcastLoop):
        coordinator.udp_broadcaster(API_PORT)

    assert len(sent) == 1, "the broadcaster did not emit exactly one datagram"
    data, addr = sent[0]
    return data.decode(), addr


def test_broadcaster_output_is_accepted_by_the_listener(monkeypatch):
    """
    The load-bearing cross-module check: what the Coordinator actually transmits must
    validate under the portal's parser, with the same key.
    """
    from engine.discovery import DISCOVERY_PORT

    message, addr = _run_one_broadcast(monkeypatch, NETWORK_KEY)

    # Right format, right destination — a beacon sent to a port nobody binds is as dead as
    # one nobody can parse.
    assert addr == ("<broadcast>", DISCOVERY_PORT)
    assert parse_discovery_beacon(message, NETWORK_KEY) == API_PORT

    # ...and it is genuinely signed, not merely well-shaped: a receiver holding a different
    # key must reject it.
    assert parse_discovery_beacon(message, "some-other-key") is None


def test_unsigned_broadcaster_output_is_accepted_by_a_keyless_listener(monkeypatch):
    """A cluster with no NETWORK_KEY still has to be able to find its Coordinator."""
    message, _addr = _run_one_broadcast(monkeypatch, "")

    assert message.count("|") == 2, "an unsigned beacon must carry no signature field"
    assert parse_discovery_beacon(message, "") == API_PORT
    # A receiver that *does* hold a key must not accept an unsigned beacon.
    assert parse_discovery_beacon(message, NETWORK_KEY) is None


def test_the_join_receiver_shares_the_one_parser():
    """
    ``setup_core.discover_coordinator`` is another speaker of this protocol (the join flow
    and the redundancy node's promotion-abort path). It must not carry its own copy of the
    validation logic — it previously did, and the copy had already drifted.
    """
    from engine import setup_core

    assert setup_core.parse_discovery_beacon is parse_discovery_beacon
