"""
LAN coordinator discovery: the signed UDP beacon a coordinator broadcasts and the check a node
makes before believing one.

Moved here, unchanged in behaviour and wire format, from the old browser portal
(engine/client_portal.py) when that was retired: the coordinator sends the beacon
(coordinator.udp_broadcaster) and the join flow (engine.setup_core) receives it. A node that
believes a forged beacon would send its work to a stranger, hence the signature and the
freshness window below.
"""

import hashlib
import hmac
import time
from typing import Optional

from engine.logger import log_warning

# UDP port the Coordinator broadcasts its presence on (see coordinator.udp_broadcaster).
DISCOVERY_PORT: int = 11435

# --- Discovery beacon wire format (audit item 3.6) ------------------------------------
#
# The beacon and the worker heartbeat are both HMAC'd with the same NETWORK_KEY. Before
# this change the beacon signed a bare "DLNE_COORDINATOR:..." string, so the two protocols
# shared a key with no domain separation between them: any construction that ever let one
# protocol's signed bytes be reinterpreted as the other's would cross straight over. The
# beacon now carries an explicit protocol tag and version, and — this is the part that
# matters — that tag is INSIDE the signed message, not merely prepended to the packet.
#
# Full wire format, fields separated by '|':
#
#     DLNE-DISC|v1|DLNE_COORDINATOR:<api_port>:<unix_time>[|<hex hmac-sha256>]
#
# The signature covers the first three fields joined back together with '|', i.e. exactly
# "DLNE-DISC|v1|DLNE_COORDINATOR:<api_port>:<unix_time>". The signature field is present
# only when the sender has a NETWORK_KEY; a receiver that has one requires it.
DISCOVERY_PROTOCOL_TAG: str = "DLNE-DISC"
DISCOVERY_PROTOCOL_VERSION: str = "v1"
DISCOVERY_FIELD_SEPARATOR: str = "|"
# The inner payload keeps its historical shape so the port/timestamp parsing, and every
# log line an operator may have grepped for, stay recognisable.
DISCOVERY_PAYLOAD_PREFIX: str = "DLNE_COORDINATOR:"

# Port the Coordinator's HTTP API actually binds (coordinator.start_coordinator_server default).
DEFAULT_COORDINATOR_PORT: int = 11435

# A signed beacon older (or newer) than this is treated as a replay and dropped. The
# Coordinator re-broadcasts every 2 s, so 10 s tolerates a few lost packets and a small
# amount of clock drift while still making captured beacons useless very quickly.
DISCOVERY_MAX_SKEW_SECONDS: float = 10.0

# One-shot latch for a warning that would otherwise repeat on every UDP packet.
_warned_unauthenticated_discovery: bool = False


def _warn_unauthenticated_discovery_once() -> None:
    """Warns exactly once that LAN discovery is running without a shared secret."""
    global _warned_unauthenticated_discovery
    if _warned_unauthenticated_discovery:
        return
    _warned_unauthenticated_discovery = True
    log_warning(
        "NETWORK_KEY is not set: LAN Coordinator discovery is UNAUTHENTICATED. Any host "
        "on this network can broadcast a beacon and redirect this node to a machine of its "
        "choosing. Set NETWORK_KEY on every node to enable signed discovery."
    )


def build_discovery_beacon(api_port: int, network_key: str,
                           timestamp: Optional[float] = None) -> str:
    """
    Builds one discovery beacon in the domain-separated v1 format described above.

    This is the single canonical producer of the format. The Coordinator's
    ``udp_broadcaster`` is the only sender in the product; it either calls this function or
    reproduces its output byte for byte, and :func:`parse_discovery_beacon` is the matching
    consumer. Keeping both halves in one file is what stops the two from drifting.

    Args:
        api_port: The TCP port the Coordinator's HTTP API is bound to.
        network_key: Shared cluster secret. Empty means an unsigned beacon.
        timestamp: Override for the beacon's clock reading. Only tests pass this; live
            senders leave it None so the receiver's freshness window is meaningful.

    Returns:
        The complete beacon string, ready to be UTF-8 encoded onto the wire.
    """
    if timestamp is None:
        timestamp = time.time()

    signed_message = DISCOVERY_FIELD_SEPARATOR.join((
        DISCOVERY_PROTOCOL_TAG,
        DISCOVERY_PROTOCOL_VERSION,
        f"{DISCOVERY_PAYLOAD_PREFIX}{api_port}:{timestamp}",
    ))

    if not network_key:
        return signed_message

    signature = hmac.new(
        network_key.encode(), signed_message.encode(), hashlib.sha256
    ).hexdigest()
    return f"{signed_message}{DISCOVERY_FIELD_SEPARATOR}{signature}"


def parse_discovery_beacon(message: str, network_key: str) -> Optional[int]:
    """
    Validates one UDP beacon and returns the Coordinator's API port, or None to drop it.

    Wire format (see the constants at the top of this module)::

        DLNE-DISC|v1|DLNE_COORDINATOR:<api_port>:<unix_time>[|<hex hmac-sha256>]

    The HMAC covers the first three fields joined with ``|``, so the protocol tag and
    version are authenticated rather than decorative: a signature produced for any other
    DLNE protocol under the same NETWORK_KEY cannot validate here.

    Args:
        message: The decoded UDP payload exactly as received.
        network_key: This node's shared secret; empty disables signature checking.

    Returns:
        The Coordinator's API port, or None when the packet must be ignored.
    """
    parts = message.split(DISCOVERY_FIELD_SEPARATOR)

    # Tag, version and payload are all mandatory; the signature is the optional fourth.
    if len(parts) < 3:
        return None
    if parts[0] != DISCOVERY_PROTOCOL_TAG or parts[1] != DISCOVERY_PROTOCOL_VERSION:
        # Unrelated broadcast traffic, or a node from a different DLNE generation. Either
        # way this receiver cannot interpret it and must not guess.
        return None

    payload = parts[2]
    if not payload.startswith(DISCOVERY_PAYLOAD_PREFIX):
        return None

    payload_parts = payload.split(":")
    if len(payload_parts) < 2:
        return None

    try:
        port = int(payload_parts[1])
    except ValueError:
        return None
    if not 1 <= port <= 65535:
        return None

    if not network_key:
        # Nothing to verify against. The packet is accepted so that key-less LANs keep
        # working, but the operator is told exactly what that costs them.
        _warn_unauthenticated_discovery_once()
        return port

    if len(parts) < 4:
        return None  # Signature required but absent.

    signed_message = DISCOVERY_FIELD_SEPARATOR.join(parts[:3])
    expected = hmac.new(
        network_key.encode(), signed_message.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, parts[3]):
        return None  # Spoofed or signed with a different NETWORK_KEY.

    # The timestamp is mandatory, not optional. Treating it as optional meant an attacker
    # could replay one captured signed beacon forever (or strip the field outright) and
    # keep pointing a node at a dead - or hostile - address.
    if len(payload_parts) < 3:
        return None
    try:
        timestamp = float(payload_parts[2])
    except ValueError:
        return None
    if abs(time.time() - timestamp) > DISCOVERY_MAX_SKEW_SECONDS:
        return None

    return port
