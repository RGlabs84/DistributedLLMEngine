"""
DLNE worker authentication primitives (Contract A).

WHY THIS MODULE EXISTS
----------------------
Before this file, ``POST /worker/heartbeat`` on the Coordinator accepted ``worker_id``,
``token``, ``port`` and ``hardware`` straight from the request body with no verification
at all. Three concrete attacks followed from that:

1. **Free membership.** The "Non-Contrib Clause" gate only checks that the caller's
   token appears in the worker registry. Anything that could reach ``:11435`` could
   register a token of its own choosing and then use the cluster for free.
2. **RPC hijack.** The ``ip:port`` a heartbeat reports is handed to ``llama-server``
   as ``--rpc <endpoint>``. An unauthenticated attacker could therefore point the
   cluster's inference backend at a machine they control, seeing every prompt and
   returning any completion they liked.
3. **Resource poisoning.** The reported hardware profile drives VRAM/KV-cache sizing.
   A node claiming 10^9 GB of VRAM makes the Coordinator admit models that cannot
   possibly fit, producing an out-of-memory crash loop for the whole cluster.

The countermeasures implemented here are, in order:

* an HMAC-SHA256 signature over a *canonical* heartbeat string, keyed by the
  pre-shared ``NETWORK_KEY`` (defeats 1 and 2 - you cannot join without the key);
* a timestamp freshness window plus a nonce cache (defeats replay of a captured
  heartbeat, which would otherwise let an attacker resurrect a dead worker's
  identity or re-assert a stale ``ip:port``);
* plausibility bounds on the hardware profile (defeats 3, and also stops a *legitimate*
  but buggy worker from poisoning the memory budget).

DESIGN CONSTRAINTS
------------------
* **Standard library only, and no DLNE imports.** This module is imported by both
  ``engine/coordinator.py`` and ``engine/worker.py``. Keeping it dependency-free
  guarantees there can be no import cycle through ``engine.config``/``engine.logger``,
  and it makes the module trivially unit-testable with no GPU, no network and no
  config file on disk.
* **The network key is never logged, never echoed and never returned.** No function
  here puts key material into a return value, an exception message or a rejection
  reason - rejection reasons travel back to the caller inside an HTTP 401 body.
* **Every secret comparison uses :func:`hmac.compare_digest`.** A naive ``==`` on a
  signature leaks, through timing, how many leading bytes were correct, which turns
  forging a signature from a 2^256 problem into a 64-step one.
* **The canonical string format is frozen, and it lives in exactly one function.** The
  Worker builds it, the Coordinator rebuilds it, and both must produce byte-identical
  input to the MAC. Both sides call :func:`_canonical_message`; nothing anywhere else in
  the codebase is allowed to assemble that string by hand, because a one-character
  divergence turns into "every heartbeat in the fleet returns 401" with a symptom
  (``invalid signature``) that points operators at their NETWORK_KEY instead of at the
  code.

WIRE FORMAT VERSION 2
---------------------
v1 signed only ``worker_id|port|token|timestamp|nonce``. That left two attacker-usable
fields *outside* the MAC even though the module claimed nothing "can be altered in
flight":

* ``hardware`` - an on-path attacker could rewrite the reported VRAM/RAM on a plain-HTTP
  heartbeat without touching the signature, poisoning the cluster's memory pool exactly
  as if they had joined the cluster themselves.
* ``version`` - rewriting it to junk makes the Coordinator answer 426, and the victim
  worker's 426 handler is a *self-terminate*. One forged byte took a node offline.

v2 therefore covers both. ``hardware`` is folded in as a SHA-256 digest rather than
inline, so the canonical string stays a fixed, bounded length no matter how large a
hardware profile grows. This is a breaking wire change: every node in a cluster must run
the same DLNE release, which is already required by the ``version`` check itself.

WIRE FORMAT VERSION 3
---------------------
The coordinator-last update rollout needs two more facts from every heartbeat, and both
of them change what the Coordinator will *do*, so both go inside the MAC:

* ``role`` - ``"worker"``, ``"redundancy"`` or ``"hybrid"``. The rollout machinery waits
  for every *redundancy* node to confirm a staged copy of the new release before the
  primary takes itself down to swap. An unsigned role would let an on-path attacker
  demote every redundancy node to ``"worker"`` in flight, emptying the wait-set so the
  primary reboots with no staged standby - defeating the entire reason the orchestration
  exists.
* ``staged_version`` - the release version this node has downloaded, SHA-256-verified
  and extracted, ``""`` when nothing is staged. Unsigned, an attacker could fabricate
  "everything is staged" confirmations and race the primary into the same unprotected
  reboot.

Both fields ride the canonical string directly (they are short, bounded strings — no
digesting needed). Like v1→v2 this is a breaking change, and like v1→v2 that is
acceptable by construction: the version check already requires the whole network to move
in lockstep.
"""

import base64
import hashlib
import hmac
import json
import math
import threading
import time
from collections import OrderedDict
from typing import Any, Optional, Tuple

__all__ = [
    "sign_heartbeat",
    "verify_heartbeat",
    "hardware_digest",
    "sign_enrollment",
    "verify_enrollment",
    "NonceCache",
    "HARDWARE_BOUNDS",
    "validate_hardware_profile",
]


# ---------------------------------------------------------------------------
# Canonical heartbeat message
# ---------------------------------------------------------------------------

# Maximum accepted nonce length, in characters. The Worker sends
# ``secrets.token_hex(16)`` (32 characters), so this is ~8x headroom. The cap exists
# because every accepted nonce is retained in the NonceCache for its full TTL: without
# a length limit, an attacker holding the network key (or an ALLOW_UNSIGNED_WORKERS
# deployment) could push megabyte-sized nonces and inflate coordinator memory.
_MAX_NONCE_LENGTH = 256

# Length of a SHA-256 hex digest. Used only for a cheap shape check before the
# constant-time comparison; the digest length is public information, so rejecting on
# it leaks nothing an attacker does not already know.
_HEX_DIGEST_LENGTH = 64


def hardware_digest(hardware: Any) -> str:
    """
    Reduce a reported hardware profile to one stable hex digest for signing.

    WHY A DIGEST AND NOT THE PROFILE ITSELF
        The profile is an open-ended nested dictionary that grows whenever
        ``engine/hardware.py`` learns to probe something new. Splicing it into the
        canonical string verbatim would make that string unbounded (a memory-exhaustion
        lever on the verifier) and would make the signature sensitive to key ordering.
        A digest is fixed-width and order-independent, and it still binds every byte of
        the profile to the MAC: change one reported GB and the digest changes, so the
        signature no longer verifies.

    WHY THESE json.dumps ARGUMENTS
        * ``sort_keys=True`` - Python dictionaries preserve insertion order, and the
          Worker's profile and the Coordinator's JSON-decoded copy of it need not share
          one. Sorting makes the encoding depend on content only.
        * ``separators=(",", ":")`` - removes the whitespace ``json.dumps`` inserts by
          default, so the two sides cannot differ over a space.
        * ``default=str`` - a profile that somehow contains a non-JSON value (a ``set``,
          a ``datetime``) must still produce *a* digest rather than raising out of the
          signing path and killing the heartbeat loop.

    Args:
        hardware: The ``hardware`` object exactly as it will be / was sent on the wire.
            ``None`` and ``{}`` are both legitimate (a node that reports nothing).

    Returns:
        A lowercase SHA-256 hex digest of the canonical JSON encoding.
    """
    try:
        encoded = json.dumps(hardware, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        # Circular references and other pathological structures land here. Falling back to
        # repr() keeps the function total: an unusual profile produces an unusual-but-stable
        # digest instead of an exception on the Worker's heartbeat thread. The Coordinator
        # cannot reproduce such a structure from JSON anyway, so the heartbeat will simply
        # fail to verify - which is the correct, fail-closed outcome.
        encoded = repr(hardware)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _canonical_message(worker_id: str, port: int, token: str, timestamp: float,
                       nonce: str, version: str, hardware: Any,
                       role: str = "worker", staged_version: str = "") -> str:
    """
    Build the exact string that both sides of the heartbeat MAC over.

    This is the single source of truth for the canonical form. The Worker reaches it
    through :func:`sign_heartbeat`, and the Coordinator reaches it through
    :func:`verify_heartbeat`, which itself calls :func:`sign_heartbeat` - so there is
    literally one code path and the two sides cannot drift apart.

    The format is::

        DLNE-HB|v3|{worker_id}|{port}|{token}|{timestamp:.3f}|{nonce}|{version}|{hw_digest}|{role}|{staged_version}

    WHY the explicit coercions and the ``:.3f`` timestamp:

    * ``timestamp`` crosses the wire as a JSON number and comes back as a Python
      ``float``. ``str(float)`` is *not* guaranteed to round-trip identically once a
      value has been through a JSON encoder/decoder on a different platform, and a
      single differing digit changes the MAC completely. Fixing the rendering at three
      decimal places (millisecond resolution, which is far finer than the 30 s skew
      window needs) removes that entire class of "signature mismatch on one OS only"
      bug.
    * ``port`` is coerced with ``int()`` so that a JSON value of ``11436`` and a string
      ``"11436"`` produce the same bytes. Both the signer and the verifier go through
      this one function, so any coercion applied here is applied identically on both
      sides - which is the only property that actually matters.
    * The ``DLNE-HB|v2`` prefix is domain separation plus a version tag. The same
      ``NETWORK_KEY`` also signs LAN discovery beacons (``DLNE-DISC|v1|...``), so
      tagging the message with its purpose means a signature captured from one protocol
      can never be replayed as a valid message in the other. The version number is what
      let this format grow from v1 to v2 without any ambiguity about which fields a
      given MAC covers.
    * ``version`` is the DLNE build string. It is signed because the Coordinator answers
      a mismatch with HTTP 426 and the Worker's 426 handler shuts the node down - so an
      unsigned ``version`` was a one-byte remote kill switch on plain HTTP.
    * ``hardware`` enters as :func:`hardware_digest` rather than inline, keeping the
      canonical string bounded while still binding every reported GB to the MAC.
    * ``role`` and ``staged_version`` joined in v3 for the update rollout - see the
      module docstring. They are placed *after* the digest so every v2 field keeps its
      position; a v2 signature can still never verify against a v3 string (the tag
      differs), but keeping stable field positions makes the format diffable by humans.

    Raises:
        TypeError / ValueError: if a field cannot be coerced to its canonical type.
            Callers that handle untrusted input (:func:`verify_heartbeat`) catch this
            and turn it into a rejection; the Worker signs its own well-formed values,
            so for the signing path a hard failure is the correct, loud behaviour.
    """
    return (
        f"DLNE-HB|v3|{str(worker_id)}|{int(port)}|{str(token)}|"
        f"{float(timestamp):.3f}|{str(nonce)}|{str(version)}|{hardware_digest(hardware)}|"
        f"{str(role)}|{str(staged_version)}"
    )


def sign_heartbeat(network_key: str, worker_id: str, port: int, token: str,
                   timestamp: float, nonce: str, version: str = "",
                   hardware: Any = None, role: str = "worker",
                   staged_version: str = "") -> str:
    """
    Produce the HMAC-SHA256 signature for one heartbeat.

    Called by the Worker immediately before it POSTs to ``/worker/heartbeat``, and by
    :func:`verify_heartbeat` on the Coordinator to recompute the expected value. Routing
    both through this one function is what guarantees the two sides canonicalise
    identically.

    Args:
        network_key: The pre-shared cluster secret (``NETWORK_KEY`` in config.txt).
            Never logged, never returned.
        worker_id: Stable identifier this node registers under.
        port: The RPC port the node is offering to the cluster.
        token: The node's access token, which the Non-Contrib gate matches on.
        timestamp: Unix epoch seconds, as produced by ``time.time()``.
        nonce: A fresh random value per heartbeat (``secrets.token_hex(16)``).
        version: The node's DLNE build string (``engine.version.VERSION``). Defaults to
            ``""`` only so the parameter can be omitted in tests; production callers on
            both sides always pass it.
        hardware: The hardware profile dictionary the heartbeat carries. Signed as a
            digest - see :func:`hardware_digest`.
        role: The node's cluster role (``worker``/``redundancy``/``hybrid``). Signed as
            of wire v3 because the rollout machinery's wait-set is built from it.
        staged_version: The release version this node holds verified on disk, ``""``
            when none. Signed as of wire v3 because it authorises the primary's reboot.

    Returns:
        The signature as a lowercase hex digest, or ``""`` when ``network_key`` is
        empty. An empty key means the operator has not configured cluster
        authentication yet; the Worker still sends the heartbeat with an empty
        signature so that ``ALLOW_UNSIGNED_WORKERS=true`` migrations keep working, and
        the Coordinator decides whether to accept it.
    """
    # An unset key must not be silently turned into HMAC(key=b"") - that produces a
    # perfectly valid-looking signature that *any* attacker can also compute, which is
    # strictly worse than an obviously absent one. Return empty and let the
    # Coordinator's ALLOW_UNSIGNED_WORKERS policy make the call.
    if not network_key:
        return ""

    message = _canonical_message(worker_id, port, token, timestamp, nonce, version,
                                 hardware, role, staged_version)
    return hmac.new(
        network_key.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_heartbeat(network_key: str, worker_id: str, port: int, token: str,
                     timestamp: float, nonce: str, signature: str,
                     version: str = "", hardware: Any = None,
                     role: str = "worker", staged_version: str = "",
                     max_skew_seconds: float = 30.0) -> Tuple[bool, str]:
    """
    Verify a heartbeat's authenticity and freshness.

    Called by the Coordinator on every ``/worker/heartbeat`` request, before any of the
    request's data is trusted or stored.

    Checks, in order:

    1. **A key is configured.** If the Coordinator has no key we cannot verify anything,
       so we fail closed. (The Coordinator only reaches this function when a key *is*
       configured; this branch exists so the function is safe in isolation.)
    2. **Structural sanity** of nonce and signature - cheap, and it keeps obviously
       malformed input out of the hashing path and out of the NonceCache.
    3. **Timestamp freshness.** Rejecting stale packets before hashing means a captured
       heartbeat that is replayed hours later costs the Coordinator a float comparison
       rather than a SHA-256. The check involves no secret, so its ordering leaks
       nothing.
    4. **The signature itself**, compared in constant time.

    Note that freshness alone does not stop replay: an attacker who captures a heartbeat
    can rebroadcast it within the skew window. That window is closed by
    :class:`NonceCache`, which the Coordinator consults immediately after this function
    returns ``True``. The two mechanisms are complementary - the timestamp bounds how
    long the nonce cache must remember anything, and the nonce cache covers the window
    the timestamp leaves open.

    Args:
        network_key: The Coordinator's copy of the pre-shared secret.
        worker_id, port, token, timestamp, nonce: The claimed heartbeat fields, exactly
            as received. They are re-canonicalised here; any tampering with any one of
            them changes the message and therefore invalidates the signature.
        signature: The hex digest the caller supplied.
        version: The claimed DLNE build string. Covered by the MAC as of wire format v2,
            so an on-path attacker can no longer rewrite it to force the victim worker
            down the self-terminating HTTP 426 path.
        hardware: The claimed hardware profile. Covered by the MAC (as a digest) as of
            wire format v2, so an on-path attacker can no longer inflate a node's
            reported VRAM/RAM to poison the cluster's memory budget.
        role: The claimed cluster role. Covered by the MAC as of wire format v3, so the
            rollout wait-set cannot be emptied by rewriting roles in flight.
        staged_version: The claimed staged release. Covered by the MAC as of wire
            format v3, so "everything is staged" cannot be forged to race the primary
            into an unprotected reboot.
        max_skew_seconds: Half-width of the accepted clock window. 30 s tolerates
            ordinary NTP drift between hobbyist machines while keeping the replay
            window short.

    Returns:
        ``(True, "ok")`` when the heartbeat is authentic and fresh, otherwise
        ``(False, reason)``. The reason is a short, non-sensitive phrase intended to be
        surfaced to the operator in the HTTP 401 body and in the Coordinator log; it
        never contains key material.
    """
    # 1. Fail closed on a missing key rather than verifying against b"".
    if not network_key:
        return (False, "no network key configured")

    # 2. Structural sanity. A missing signature is the common misconfiguration
    #    (worker has no NETWORK_KEY), so it gets its own distinct reason to make the
    #    operator's fix obvious.
    if not isinstance(signature, str) or not signature:
        return (False, "missing signature")
    if not isinstance(nonce, str) or not nonce:
        return (False, "missing nonce")
    if len(nonce) > _MAX_NONCE_LENGTH:
        return (False, "nonce too long")

    # 3. Freshness. Reject non-finite or non-numeric timestamps explicitly: NaN makes
    #    every comparison below false, so without this guard a NaN timestamp would
    #    sail straight through the skew check.
    try:
        claimed_time = float(timestamp)
    except (TypeError, ValueError):
        return (False, "invalid timestamp")
    if not math.isfinite(claimed_time):
        return (False, "invalid timestamp")
    if abs(time.time() - claimed_time) > max_skew_seconds:
        return (False, "stale timestamp")

    # 4. Recompute the MAC over the canonical form of what we actually received.
    try:
        expected = sign_heartbeat(
            network_key, worker_id, port, token, timestamp, nonce, version, hardware,
            role, staged_version
        )
    except (TypeError, ValueError):
        # A field that cannot be canonicalised (e.g. port="eight thousand") is
        # malformed input, not an authentication failure - but it is still a rejection.
        return (False, "malformed heartbeat fields")

    # compare_digest raises TypeError on non-ASCII str arguments, so encode first and
    # treat anything that is not ASCII as an outright mismatch: a real hex digest is
    # ASCII by construction.
    try:
        candidate_bytes = signature.encode("ascii")
    except UnicodeEncodeError:
        return (False, "invalid signature")

    if len(candidate_bytes) != _HEX_DIGEST_LENGTH:
        return (False, "invalid signature")

    if not hmac.compare_digest(expected.encode("ascii"), candidate_bytes):
        return (False, "invalid signature")

    return (True, "ok")


# ---------------------------------------------------------------------------
# Tunnel enrollment
# ---------------------------------------------------------------------------
#
# WHY THIS EXISTS
#     Every worker used to need its own hand-minted invite carrying a WireGuard private
#     key, because only the Coordinator could allocate a tunnel address. Fifty testers
#     meant fifty files, and the obvious shortcut - copying one invite to everybody - does
#     not work: WireGuard identifies a peer by its public key, so N nodes presenting the
#     same keypair are one peer fighting over one address.
#
#     Enrollment inverts it. The node generates its own keypair, keeps the private half,
#     and asks to be assigned an address by presenting only the PUBLIC half. What it must
#     prove is membership, and the network key already is that proof - the same secret that
#     signs every heartbeat. So one join file, containing no private key at all, serves any
#     number of nodes.
#
# WHY IT NEEDS ITS OWN DOMAIN TAG
#     A captured heartbeat signature must never be replayable as an enrollment request, and
#     vice versa. "DLNE-ENROL|v1" separates this protocol from "DLNE-HB|v3" and the LAN
#     discovery beacon's "DLNE-DISC|v1" under the same key, exactly as those two are
#     separated from each other.
#
# WHAT IS SIGNED
#     The public key is signed because it is the entire request: an on-path attacker who
#     could rewrite it would have the Coordinator register THEIR key against the victim's
#     enrollment, handing them the tunnel address and the traffic routed to it.

def _canonical_enrollment(worker_id: str, public_key: str, timestamp: float,
                          nonce: str) -> str:
    """
    Build the exact string both sides of the enrollment MAC over.

    The format is::

        DLNE-ENROL|v1|{worker_id}|{public_key}|{timestamp:.3f}|{nonce}

    The ``:.3f`` timestamp rendering and the explicit ``str()`` coercions exist for the
    same reason they do in :func:`_canonical_message`: the values cross the wire as JSON
    and must re-render byte-identically on the far side.
    """
    return (
        f"DLNE-ENROL|v1|{str(worker_id)}|{str(public_key)}|"
        f"{float(timestamp):.3f}|{str(nonce)}"
    )


def sign_enrollment(network_key: str, worker_id: str, public_key: str,
                    timestamp: float, nonce: str) -> str:
    """
    Produce the HMAC-SHA256 signature for one tunnel enrollment request.

    Args:
        network_key: The pre-shared cluster secret, as carried by the join file. Never
            logged, never returned.
        worker_id: Stable identifier this node registers under.
        public_key: The node's WireGuard **public** key, base64 as ``wg pubkey`` emits it.
            The private half never leaves the node and is never an argument here.
        timestamp: Unix epoch seconds.
        nonce: A fresh random value per request.

    Returns:
        Lowercase hex digest, or ``""`` when ``network_key`` is empty — for the same
        reason :func:`sign_heartbeat` does it: HMAC over an empty key is a signature any
        attacker can also compute, which is worse than an obviously absent one.
    """
    if not network_key:
        return ""

    message = _canonical_enrollment(worker_id, public_key, timestamp, nonce)
    return hmac.new(
        network_key.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_enrollment(network_key: str, worker_id: str, public_key: str,
                      timestamp: float, nonce: str, signature: str,
                      max_skew_seconds: float = 30.0) -> Tuple[bool, str]:
    """
    Verify an enrollment request's authenticity and freshness.

    Mirrors :func:`verify_heartbeat` check for check, and for the same reasons: fail
    closed on a missing key, reject malformed input before it reaches the hashing path or
    the nonce cache, check freshness before spending a SHA-256, and compare the MAC in
    constant time.

    As with heartbeats, freshness alone does not stop replay — the caller must consult a
    :class:`NonceCache` immediately after this returns ``True``. Replay matters more here
    than it does for a heartbeat: a captured enrollment resent within the window would
    re-register a public key the attacker has now seen, so the nonce check is not optional.

    Args:
        network_key: The Coordinator's copy of the pre-shared secret.
        worker_id, public_key, timestamp, nonce: The claimed fields, exactly as received.
        signature: The hex digest the caller supplied.
        max_skew_seconds: Half-width of the accepted clock window.

    Returns:
        ``(True, "ok")`` or ``(False, reason)``. The reason never contains key material.
    """
    if not network_key:
        return (False, "no network key configured")

    if not isinstance(signature, str) or not signature:
        return (False, "missing signature")
    if not isinstance(nonce, str) or not nonce:
        return (False, "missing nonce")
    if len(nonce) > _MAX_NONCE_LENGTH:
        return (False, "nonce too long")
    if not isinstance(public_key, str) or not public_key:
        return (False, "missing public key")

    try:
        claimed_time = float(timestamp)
    except (TypeError, ValueError):
        return (False, "invalid timestamp")
    if not math.isfinite(claimed_time):
        return (False, "invalid timestamp")
    if abs(time.time() - claimed_time) > max_skew_seconds:
        return (False, "stale timestamp")

    try:
        expected = sign_enrollment(network_key, worker_id, public_key, timestamp, nonce)
    except (TypeError, ValueError):
        return (False, "malformed enrollment fields")

    try:
        candidate_bytes = signature.encode("ascii")
    except UnicodeEncodeError:
        return (False, "invalid signature")

    if len(candidate_bytes) != _HEX_DIGEST_LENGTH:
        return (False, "invalid signature")

    if not hmac.compare_digest(expected.encode("ascii"), candidate_bytes):
        return (False, "invalid signature")

    return (True, "ok")


# ---------------------------------------------------------------------------
# Replay guard
# ---------------------------------------------------------------------------

class NonceCache:
    """
    Bounded, thread-safe set of recently used heartbeat nonces.

    THREAT ADDRESSED
        A valid signed heartbeat is a bearer credential for its skew window. Anyone who
        can observe one on the wire (an unencrypted LAN, a compromised switch, a
        mis-configured WireGuard peer) can resend the identical bytes and the signature
        will still verify. That is enough to keep a removed node "alive" in the
        registry, or to re-point an RPC endpoint the operator just changed. Remembering
        each nonce until its heartbeat can no longer be fresh closes that window
        completely.

    WHY IT IS BOUNDED TWICE
        * **TTL** keeps the memory proportional to the heartbeat rate rather than to
          uptime. Entries only need to outlive the freshness window, so a TTL a few
          times larger than ``max_skew_seconds`` is sufficient.
        * **A hard entry cap** exists because TTL alone is not a bound: a flood of
          heartbeats can insert far more nonces within one TTL than the host has memory
          for. The cap converts an unbounded memory-exhaustion DoS into, at worst,
          early forgetting of the oldest nonces. Eviction is oldest-first, which is
          exactly the right order - the oldest entries are the closest to expiring
          anyway and protect the least.

    KNOWN LIMITATION - THE GUARD IS PROCESS-LOCAL
        This cache lives in the Coordinator process and nowhere else. It is therefore
        empty again after:

        * a Coordinator restart (crash, upgrade, or an ordinary stop/start), and
        * a redundancy node being promoted to Coordinator, which starts a fresh process
          that has never seen any of the primary's nonces.

        For roughly ``max_skew_seconds`` after either event (30 s by default) a heartbeat
        captured just beforehand can be replayed successfully, because the signature is
        still fresh and the new process has no record of the nonce. The consequences are
        bounded - an attacker can keep one already-departed worker looking alive, or
        re-assert an ``ip:port`` the operator has just changed, for one skew window - and
        closing it properly would require shared state (a database or a file) between
        processes, which this project deliberately does not have. It is accepted, and
        documented here so that it is a known limitation rather than a later surprise.
        Anyone tightening this should shrink ``max_skew_seconds`` first; that is the
        single knob that bounds the exposure.

    WHY IT IS LOCKED
        The Coordinator is served by uvicorn. Even though async handlers share one event
        loop, FastAPI runs synchronous dependencies and ``run_in_threadpool`` work on
        worker threads, and the codebase also drives background threads (registry
        pruning, redundancy sync). A plain ``dict`` mutated from several threads can
        drop or duplicate entries during a resize, and a duplicate-checking cache that
        silently drops entries is a replay guard that silently stops guarding. One
        ``threading.Lock`` around every mutation makes the check-and-record atomic,
        which also removes the check-then-insert race that would otherwise let two
        simultaneous replays of the same nonce both be accepted.
    """

    def __init__(self, ttl_seconds: float = 120.0, max_entries: int = 10000) -> None:
        """
        Args:
            ttl_seconds: How long a nonce is remembered. Must be > 0. Should comfortably
                exceed the verifier's ``max_skew_seconds`` (default 30 s), otherwise a
                nonce could be forgotten while its heartbeat is still considered fresh -
                which would reopen the replay window.
            max_entries: Hard ceiling on retained nonces. Must be > 0.

        Raises:
            ValueError: on a non-positive TTL or cap. Both would silently disable the
                replay guard (nothing is ever retained, so every nonce looks new), and a
                security control that fails open without complaining is worse than no
                control at all - so we refuse to construct one.
        """
        if ttl_seconds <= 0:
            raise ValueError("NonceCache ttl_seconds must be greater than 0")
        if max_entries <= 0:
            raise ValueError("NonceCache max_entries must be greater than 0")

        self.ttl_seconds = float(ttl_seconds)
        self.max_entries = int(max_entries)

        # Ordered by insertion. Because every entry gets the same TTL, insertion order
        # *is* expiry order, so both TTL eviction and cap eviction are cheap pops from
        # the front. Value is the monotonic deadline at which the entry expires.
        self._entries: "OrderedDict[str, float]" = OrderedDict()
        self._lock = threading.Lock()

    def _purge_expired(self, now: float) -> None:
        """
        Drop every entry whose deadline has passed. Caller must hold ``self._lock``.

        Uses the shared expiry ordering described above: as soon as the oldest entry is
        still live, every later entry is too, so we can stop. This keeps the amortised
        cost of :meth:`seen` at O(1) instead of scanning the whole cache each call.
        """
        while self._entries:
            oldest_nonce, deadline = next(iter(self._entries.items()))
            if deadline > now:
                break
            self._entries.pop(oldest_nonce, None)

    def seen(self, nonce: str) -> bool:
        """
        Atomically test-and-record a nonce.

        Args:
            nonce: The value from the heartbeat. Must be a non-empty string; anything
                else is treated as already-seen so that malformed input fails closed
                rather than bypassing the guard.

        Returns:
            ``True`` if this nonce has been presented before (the caller must reject the
            heartbeat as a replay). ``False`` if it is new, in which case it has now
            been recorded and any subsequent presentation within the TTL returns
            ``True``.
        """
        # Fail closed on garbage: an empty or non-string nonce cannot be tracked
        # meaningfully, and reporting it as "already seen" makes the Coordinator reject
        # it instead of waving it through.
        if not isinstance(nonce, str) or not nonce:
            return True

        # time.monotonic(), not time.time(): the cache must not be confused by an NTP
        # step or a manual clock change. Wall-clock time is only used for the *skew*
        # check, where it is genuinely required because the two machines must agree.
        now = time.monotonic()

        with self._lock:
            self._purge_expired(now)

            if nonce in self._entries:
                return True

            self._entries[nonce] = now + self.ttl_seconds

            # Enforce the hard cap after insertion so a brand-new nonce is never
            # rejected merely because the cache was full; the oldest (closest to
            # expiry) entries are surrendered instead.
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

            return False

    def clear(self) -> None:
        """Forget every recorded nonce. Used by tests and by a clean coordinator restart."""
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        """
        Number of nonces currently being remembered.

        Expired entries are purged first so the count reflects entries that would
        actually cause a rejection, not accumulated garbage awaiting collection.
        """
        with self._lock:
            self._purge_expired(time.monotonic())
            return len(self._entries)


# ---------------------------------------------------------------------------
# Hardware plausibility clamping
# ---------------------------------------------------------------------------

# Inclusive (minimum, maximum) for each numeric field a worker may report, PER NODE.
#
# These are the numbers that stop a hostile node from poisoning the cluster's memory pool,
# so they have to be tight enough to actually be a control. The previous values (2048 GB
# VRAM, 8192 GB RAM per node) were not: a single node could claim ~9 TB, which is more than
# the largest model in the library needs, so every model was admitted and the cluster OOMed -
# the exact attack the check exists to stop. A bound that admits the attack is decoration.
#
# The current values are still far above any machine that would plausibly run DLNE (a
# 512 GB VRAM node is eight H100s; 2 TB of RAM is a large server), so they cost a legitimate
# operator nothing while capping what one lying node can add to the pool. If real hardware
# ever exceeds them the fix is a one-line constant change here - a much cheaper problem than
# unbounded trust in a remote number.
HARDWARE_BOUNDS = {
    "vram_total_gb": (0.0, 512.0),    # ~6x an H100 80GB / 8 consumer GPUs in one chassis
    "ram_gb": (0.0, 2048.0),          # 2 TB, i.e. a well-specified server
    "cpu_cores": (0, 256),            # physical cores
    "cpu_threads": (0, 512),          # logical processors
    # Measured by CPU-only nodes (engine/hardware.measure_memory_bandwidth_gbs). A 12-channel
    # DDR5 server reaches ~460 GB/s; anything near the cap is a lie, not a machine.
    "mem_bandwidth_gbs": (0.0, 2048.0),
}

# cpu_features is a list of instruction-set names (engine/hardware.CPU_FEATURE_NAMES); a
# node gets to say which it has, not to send arbitrary data along with it.
_MAX_CPU_FEATURES = 32
_MAX_NAME_LENGTH = 32

# Fields a worker reports that MUST be genuine JSON booleans, and where they may appear.
# These are not numbers, so HARDWARE_BOUNDS cannot express them, but they are every bit as
# load-bearing:
#
# * ``has_gpu`` gates the VRAM sum in ``registry.get_total_vram_gb`` - a node that sets it
#   while reporting VRAM adds that VRAM to the pool.
# * ``is_yielding`` removes a node from *every* total. A JSON string ``"false"`` is truthy
#   in Python, so a worker (or an attacker) sending the string form silently deletes that
#   node's whole contribution from the budget - and the resulting under-count is invisible,
#   because nothing errors.
#
# Requiring a real bool means both of those become an honest HTTP 400 instead of a silent
# mis-accounting. ``(container_key, field)`` - ``None`` means the top level of the profile.
_BOOLEAN_FIELDS = (
    (None, "is_yielding"),
    ("gpu", "has_gpu"),
    (None, "has_gpu"),      # tolerated at the top level too; older probes put it there
    (None, "coding_ok"),    # the volunteer allows the coding-member job (CODING_MEMBER_ALLOWED)
)


def _check_bounded_value(label: str, value: Any, bounds: Tuple[float, float]) -> Optional[str]:
    """
    Validate one reported hardware number.

    Args:
        label: Human-readable field path used in the rejection reason (e.g.
            ``"gpu.vram_total_gb"``), so the operator can see exactly which claim was
            refused.
        value: The value as received from the heartbeat body.
        bounds: ``(minimum, maximum)``, inclusive.

    Returns:
        ``None`` when the value is acceptable, otherwise a short rejection reason.
    """
    minimum, maximum = bounds

    # ``bool`` is a subclass of ``int`` in Python, so ``isinstance(True, int)`` is True
    # and ``True`` would sail through the range check as the number 1. A worker
    # reporting ``"cpu_cores": true`` is malformed, and letting it register as a
    # 1-core node hides the fault - so booleans are rejected explicitly, before the
    # numeric test.
    if isinstance(value, bool):
        return f"{label} must be a number"

    if not isinstance(value, (int, float)):
        return f"{label} must be a number"

    # NaN and +/-inf survive every ordinary comparison (NaN makes them all False, inf
    # propagates through the sizing arithmetic), so they are excluded before the range
    # test rather than relying on it.
    if not math.isfinite(value):
        return f"{label} must be a finite number"

    # Called out separately from the range test purely for the operator-facing message:
    # "negative" points at a broken probe, "out of range" points at a bogus claim.
    if value < 0:
        return f"{label} must not be negative"

    if not (minimum <= value <= maximum):
        return f"{label} value {value} is outside the plausible range {minimum}-{maximum}"

    return None


def validate_hardware_profile(hw: dict) -> Tuple[bool, str]:
    """
    Reject implausible hardware claims before they reach the registry.

    THREAT ADDRESSED
        The hardware profile is attacker-controlled data that becomes cluster policy:
        ``registry`` sums it into the VRAM/RAM pool, and ``InferenceManager`` divides
        that pool to pick a context size and to decide whether another model may be
        admitted. Inflated values cause over-admission and an out-of-memory crash loop;
        negative or non-numeric values propagate as ``TypeError`` or as a negative
        budget deep inside the sizing maths, far from their origin. Validating once, at
        the boundary, keeps every downstream consumer working with real numbers.

    TOLERANCE OF MISSING KEYS
        Absent is not invalid. Older workers and CPU-only nodes legitimately omit
        fields, and rejecting them would break rolling upgrades - which in practice
        means operators disable the check entirely. Only fields that are actually
        *present* are validated. Downstream code already defaults missing fields to 0
        via ``.get(key, 0)``.

    WHERE THE FIELDS LIVE
        ``engine/hardware.get_hardware_profile()`` returns ``ram_gb``, ``cpu_cores`` and
        ``cpu_threads`` at the top level but nests VRAM inside a ``gpu`` sub-dictionary
        (``hw["gpu"]["vram_total_gb"]``). Both locations are therefore searched for
        every bounded key, so the check works regardless of which shape a given worker
        version sends.

    NON-NUMERIC FIELDS
        ``is_yielding`` and ``gpu.has_gpu`` are validated too, as strict booleans. They are
        not sizes, but they decide whether a node's resources are counted *at all*, and
        Python's truthiness rules make a wrong type here silent rather than loud - see
        :data:`_BOOLEAN_FIELDS`.

    Args:
        hw: The ``hardware`` object from the heartbeat body.

    Returns:
        ``(True, "ok")`` when every present field is plausible, otherwise
        ``(False, reason)``. The Coordinator turns the reason into an HTTP 400 body.
    """
    if not isinstance(hw, dict):
        return (False, "hardware profile must be an object")

    gpu = hw.get("gpu")
    # The Coordinator does ``hw.get("gpu", {}).get("vram_total_gb", 0)``. If ``gpu``
    # arrives as a string or a list that call raises AttributeError and returns a 500,
    # turning a malformed heartbeat into a server error. Catching the wrong type here
    # converts it into an honest 400. ``None``/absent is fine - CPU-only nodes.
    if gpu is not None and not isinstance(gpu, dict):
        return (False, "hardware.gpu must be an object")

    for key, bounds in HARDWARE_BOUNDS.items():
        # Top-level form, e.g. {"ram_gb": 32.0}.
        if key in hw:
            problem = _check_bounded_value(key, hw[key], bounds)
            if problem:
                return (False, problem)

        # Nested form, e.g. {"gpu": {"vram_total_gb": 24.0}}. Labelled with its path so
        # the rejection reason is unambiguous when both forms are present.
        if isinstance(gpu, dict) and key in gpu:
            problem = _check_bounded_value(f"gpu.{key}", gpu[key], bounds)
            if problem:
                return (False, problem)

    # The two flags are only meaningful as real booleans - see _BOOLEAN_FIELDS for why a
    # string "false" here is worse than an obviously wrong value.
    for container_key, field in _BOOLEAN_FIELDS:
        if container_key is None:
            container, label = hw, field
        else:
            container, label = gpu, f"{container_key}.{field}"

        if isinstance(container, dict) and field in container:
            if not isinstance(container[field], bool):
                return (False, f"{label} must be true or false")

    # Instruction sets and architecture decide whether a CPU-only node is admitted, so they
    # must be what they claim to be: a short list of short names, and a short name.
    features = hw.get("cpu_features")
    if features is not None:
        if (not isinstance(features, list) or len(features) > _MAX_CPU_FEATURES
                or not all(isinstance(f, str) and 0 < len(f) <= _MAX_NAME_LENGTH for f in features)):
            return (False, "cpu_features must be a list of instruction-set names")
    arch = hw.get("cpu_arch")
    if arch is not None and not (isinstance(arch, str) and len(arch) <= _MAX_NAME_LENGTH):
        return (False, "cpu_arch must be a short name")

    # A CPU pool member (2.3.0) says so, and reports what it serves. A coding member (see
    # docs/coding-members.md) says "coding" and reports its model and speeds in a "coding" block.
    # A worker is one or the other, never both.
    pool = hw.get("pool")
    if pool is not None and pool not in ("app", "coding"):
        return (False, "pool must be \"app\" or \"coding\" when present")
    # The CPU half of a machine that also has a GPU node (engine/companion.py) names that node. It is a CPU pool
    # member, nothing else, and the id is the one ``<id>-cpu`` the companion registers under.
    companion_of = hw.get("companion_of")
    if companion_of is not None:
        if not (isinstance(companion_of, str) and 0 < len(companion_of) <= _MAX_NAME_LENGTH * 2):
            return (False, "companion_of must be a short worker id")
        if pool != "app":
            return (False, "only a CPU pool member can be the companion of another node")
    app = hw.get("app")
    if app is not None:
        if pool == "coding":
            return (False, "a coding member has no app section")
        if not isinstance(app, dict) or set(app) - {"model", "ready", "slots", "threads"}:
            return (False, "app must be an object with model, ready, slots and threads")
        model = app.get("model")
        if model is not None and not (isinstance(model, str) and 0 < len(model) <= _MAX_NAME_LENGTH * 2):
            return (False, "app.model must be a short name")
        if "ready" in app and not isinstance(app["ready"], bool):
            return (False, "app.ready must be true or false")
        slots = app.get("slots")
        if slots is not None and (isinstance(slots, bool) or not isinstance(slots, int) or not 1 <= slots <= 64):
            return (False, "app.slots must be a whole number from 1 to 64")
        threads = app.get("threads")      # the threads the member really runs its model with
        if threads is not None and (isinstance(threads, bool) or not isinstance(threads, int)
                                    or not 1 <= threads <= 256):
            return (False, "app.threads must be a whole number from 1 to 256")

    # How many GPUs the node's memory total is made of (the website's count of GPUs lent).
    if isinstance(gpu, dict) and "count" in gpu:
        count = gpu["count"]
        if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 64:
            return (False, "gpu.count must be a whole number from 0 to 64")

    coding = hw.get("coding")
    if coding is not None:
        problem = _check_coding_block(coding) if pool == "coding" else "coding is only valid for pool \"coding\""
        if problem:
            return (False, problem)

    return (True, "ok")


# What a coding member may say about itself (docs/coding-members.md section 2). Strings are
# short, numbers finite and in range, and anything not listed is refused, like the app block.
_CODING_KEYS = frozenset({"model", "tier", "ready", "slots", "busy", "decode_tps", "prefill_tps", "pubkey", "no_trace",
                          "identity", "signed_at", "sig"})
_MAX_CODING_STRING = 128
_MAX_CODING_SLOTS = 64
_MAX_CODING_BUSY = 1024
_MAX_CODING_TPS = 100000.0
_MAX_SIGNED_AT = 4102444800          # 2100-01-01: a unix time in seconds, not a millisecond count
_ED25519_KEY_BYTES = 32
_ED25519_SIGNATURE_BYTES = 64


def _check_coding_block(coding: Any) -> Optional[str]:
    """Why a heartbeat's ``coding`` block is refused, or None when it is acceptable."""
    if not isinstance(coding, dict) or set(coding) - _CODING_KEYS:
        return ("coding must be an object with only model, tier, ready, slots, busy, decode_tps, prefill_tps, "
                "pubkey, no_trace, identity, signed_at and sig")
    for key in ("model", "tier"):
        value = coding.get(key)
        if value is not None and not (isinstance(value, str) and 0 < len(value) <= _MAX_CODING_STRING
                                      and value.isprintable()):
            return f"coding.{key} must be a short name"
    for key in ("ready", "no_trace"):
        if key in coding and not isinstance(coding[key], bool):
            return f"coding.{key} must be true or false"
    slots = coding.get("slots")
    if slots is not None and (isinstance(slots, bool) or not isinstance(slots, int)
                              or not 0 <= slots <= _MAX_CODING_SLOTS):
        return f"coding.slots must be a whole number from 0 to {_MAX_CODING_SLOTS}"
    # 0 slots is a member stepping aside (its owner is gaming): only while it says it is not ready.
    if slots == 0 and coding.get("ready") is not False:
        return "coding.slots may be 0 only while coding.ready is false"
    busy = coding.get("busy")
    if busy is not None and (isinstance(busy, bool) or not isinstance(busy, int)
                             or not 0 <= busy <= _MAX_CODING_BUSY):
        return f"coding.busy must be a whole number from 0 to {_MAX_CODING_BUSY}"
    for key in ("decode_tps", "prefill_tps"):
        if key in coding:
            problem = _check_bounded_value(f"coding.{key}", coding[key], (0.0, _MAX_CODING_TPS))
            if problem:
                return problem
    pubkey = coding.get("pubkey")
    if pubkey is not None and not _is_canonical_b64(pubkey, 32):
        return "coding.pubkey must be a base64 X25519 public key (32 bytes)"
    identity = coding.get("identity")
    if identity is not None and not _is_canonical_b64(identity, _ED25519_KEY_BYTES):
        return "coding.identity must be a base64 Ed25519 public key (32 bytes)"
    sig = coding.get("sig")
    if sig is not None and not _is_canonical_b64(sig, _ED25519_SIGNATURE_BYTES):
        return "coding.sig must be a base64 Ed25519 signature (64 bytes)"
    signed_at = coding.get("signed_at")
    if signed_at is not None and (isinstance(signed_at, bool) or not isinstance(signed_at, int)
                                  or not 0 <= signed_at <= _MAX_SIGNED_AT):
        return "coding.signed_at must be a whole number of seconds"
    return None


def _is_canonical_b64(value: Any, size: int) -> bool:
    """Standard base64, padded and canonical (what ``base64.b64encode`` returns), of exactly ``size`` bytes."""
    if not isinstance(value, str) or len(value) != 4 * ((size + 2) // 3):
        return False
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        return False
    return len(raw) == size and base64.b64encode(raw).decode() == value
