"""
Tests for ``engine.authn`` — the heartbeat signing, replay guard and hardware clamp that
Contract A introduced (bugs #1 and #2).

These are the cluster's front door. Before this module existed, anything that could reach
``:11435`` could register a token of its choosing, inject an arbitrary ``ip:port`` into the
``--rpc`` argument of llama-server, and report whatever VRAM it liked. Every test below
asserts the *fixed* behaviour: a rejection is the expected outcome and a silent acceptance
is the failure.

The module deliberately has no DLNE imports and no external dependencies, so nothing here
needs a config file, a temp directory or a network.
"""

import hashlib
import hmac
import json
import time

import pytest

from engine import authn


# Fixed inputs shared by the signing tests, so a failure points at the behaviour under test
# rather than at differing arguments.
KEY = "correct-horse-battery-staple"
WORKER_ID = "node-a1b2c3d4"
PORT = 50052
TOKEN = "tok-deadbeef"
NONCE = "0123456789abcdef0123456789abcdef"
VERSION = "1.4.2"
HARDWARE = {
    "gpu": {"has_gpu": True, "vram_total_gb": 24.0, "name": "Test GPU", "load_percent": 5.0},
    "ram_gb": 64.0,
    "cpu_cores": 16,
    "cpu_threads": 32,
    "is_yielding": False,
}


# ======================================================================================
# sign_heartbeat / verify_heartbeat
# ======================================================================================

def test_sign_and_verify_round_trip():
    """The happy path: a freshly signed heartbeat verifies against the same key."""
    timestamp = time.time()
    signature = authn.sign_heartbeat(
        KEY, WORKER_ID, PORT, TOKEN, timestamp, NONCE, VERSION, HARDWARE
    )

    assert signature  # a configured key must actually produce a signature
    assert authn.verify_heartbeat(
        KEY, WORKER_ID, PORT, TOKEN, timestamp, NONCE, signature, VERSION, HARDWARE
    ) == (True, "ok")


def test_signature_matches_the_frozen_canonical_format():
    """
    Pins the exact bytes that go into the MAC — wire format v3.

    The Worker builds this string and the Coordinator rebuilds it independently. If either
    side ever changes the separator, the version tag or the field order, every heartbeat in
    the fleet stops verifying — and the failure looks like "wrong NETWORK_KEY", which sends
    operators hunting in the wrong place. Recomputing the HMAC here from the literal format
    means such a change fails loudly in CI instead.

    v2 added ``version`` and a digest of ``hardware`` to the signed set. Under v1 both
    travelled unsigned over plain HTTP: an on-path attacker could inflate the reported VRAM
    to poison the cluster's memory budget, or corrupt ``version`` to make the Coordinator
    answer 426 — which the Worker's own handler turns into a self-terminate.

    v3 added ``role`` and ``staged_version``, which the coordinator-last rollout depends on:
    the primary waits for every *redundancy* node to report the new build staged before it
    reboots itself. Unsigned, either field breaks that gate — flipping a role empties the
    wait set, and forging a staged_version claims a download that never happened.
    """
    timestamp = 1_700_000_000.5
    hardware_hash = hashlib.sha256(
        json.dumps(HARDWARE, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    expected_message = (
        f"DLNE-HB|v3|{WORKER_ID}|{PORT}|{TOKEN}|{timestamp:.3f}|{NONCE}"
        f"|{VERSION}|{hardware_hash}|redundancy|1.0.1"
    )
    expected_signature = hmac.new(
        KEY.encode("utf-8"), expected_message.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    assert authn.sign_heartbeat(
        KEY, WORKER_ID, PORT, TOKEN, timestamp, NONCE, VERSION, HARDWARE,
        role="redundancy", staged_version="1.0.1"
    ) == expected_signature


def test_role_and_staged_version_default_to_the_plain_worker_case():
    """
    A caller that ignores the v3 fields still signs the v3 string, with the defaults in it.

    Both ends default identically (``role="worker"``, ``staged_version=""``), so the older
    call shape keeps verifying — but the *format* is v3 for everyone, because a signer that
    silently emitted v2 while the verifier expected v3 would reject the whole fleet.
    """
    timestamp = 1_700_000_000.5
    hardware_hash = hashlib.sha256(
        json.dumps(HARDWARE, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    expected_message = (
        f"DLNE-HB|v3|{WORKER_ID}|{PORT}|{TOKEN}|{timestamp:.3f}|{NONCE}"
        f"|{VERSION}|{hardware_hash}|worker|"
    )
    expected_signature = hmac.new(
        KEY.encode("utf-8"), expected_message.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    assert authn.sign_heartbeat(
        KEY, WORKER_ID, PORT, TOKEN, timestamp, NONCE, VERSION, HARDWARE
    ) == expected_signature


def test_a_tampered_role_fails_verification():
    """
    ITEM: the rollout gate is only as strong as the signature over ``role``.

    The primary Coordinator reboots once every *redundancy* node reports the new build
    staged. If ``role`` could be edited in flight, an on-path attacker (or a buggy proxy)
    could relabel every backup as a plain worker, emptying the wait set and letting the
    primary reboot with nothing staged anywhere — exactly the unattended-outage the
    coordinator-last ordering exists to prevent.
    """
    # A live timestamp, because verify_heartbeat also enforces the freshness window and a
    # frozen one would fail this test for the wrong reason.
    timestamp = time.time()
    signature = authn.sign_heartbeat(
        KEY, WORKER_ID, PORT, TOKEN, timestamp, NONCE, VERSION, HARDWARE,
        role="redundancy", staged_version="1.0.1"
    )

    ok, _ = authn.verify_heartbeat(
        KEY, WORKER_ID, PORT, TOKEN, timestamp, NONCE, signature, VERSION, HARDWARE,
        role="redundancy", staged_version="1.0.1"
    )
    assert ok

    downgraded, reason = authn.verify_heartbeat(
        KEY, WORKER_ID, PORT, TOKEN, timestamp, NONCE, signature, VERSION, HARDWARE,
        role="worker", staged_version="1.0.1"
    )
    assert not downgraded
    assert reason

    forged, reason = authn.verify_heartbeat(
        KEY, WORKER_ID, PORT, TOKEN, timestamp, NONCE, signature, VERSION, HARDWARE,
        role="redundancy", staged_version="9.9.9"
    )
    assert not forged
    assert reason


def test_signer_and_verifier_canonicalise_identically_after_a_json_round_trip():
    """
    ITEM 1.3 GUARD: the Worker's bytes and the Coordinator's bytes must match exactly.

    This is the failure mode that would take the whole fleet offline at once. The Worker
    signs a live Python dictionary; the Coordinator verifies a copy of that dictionary that
    has been through ``json.dumps`` on one machine and ``json.loads`` on another, arriving
    with a different key order and with every value rebuilt by a JSON decoder. Both sides
    reach the canonical string through the single function in ``engine.authn``, so the two
    must agree byte-for-byte — and if they ever stop, every heartbeat returns 401 with
    "invalid signature", which reads as a NETWORK_KEY problem and is not one.

    ``sort_keys`` is what makes the key-order difference irrelevant, and the shared
    ``:.3f``/``int()`` coercions handle the numeric round-trip. The nested reordering below
    is deliberate: a plain equality of two identically-ordered dicts would not exercise it.
    """
    timestamp = time.time()

    # What the Worker signs: a locally built profile, in its natural key order.
    worker_side = {
        "gpu": {"has_gpu": True, "vram_total_gb": 24.0, "name": "Test GPU"},
        "ram_gb": 64.0,
        "cpu_cores": 16,
        "is_yielding": False,
    }
    signature = authn.sign_heartbeat(
        KEY, WORKER_ID, PORT, TOKEN, timestamp, NONCE, VERSION, worker_side
    )

    # What the Coordinator sees: the same content after a real serialise/deserialise, then
    # re-inserted in a different order to model a decoder that does not preserve ordering.
    decoded = json.loads(json.dumps(worker_side))
    coordinator_side = {
        "is_yielding": decoded["is_yielding"],
        "cpu_cores": decoded["cpu_cores"],
        "ram_gb": decoded["ram_gb"],
        "gpu": {
            "name": decoded["gpu"]["name"],
            "vram_total_gb": decoded["gpu"]["vram_total_gb"],
            "has_gpu": decoded["gpu"]["has_gpu"],
        },
    }
    assert coordinator_side != worker_side or list(coordinator_side) != list(worker_side)

    # Byte-for-byte agreement, asserted on the canonical inputs and not merely on the verdict.
    assert authn.hardware_digest(coordinator_side) == authn.hardware_digest(worker_side)
    assert authn.sign_heartbeat(
        KEY, WORKER_ID, PORT, TOKEN, timestamp, NONCE, VERSION, coordinator_side
    ) == signature
    assert authn.verify_heartbeat(
        KEY, WORKER_ID, PORT, TOKEN, timestamp, NONCE, signature, VERSION, coordinator_side
    ) == (True, "ok")


def test_hardware_digest_is_stable_and_content_sensitive():
    """
    The digest must depend on content only — not on key order — and must change when any
    reported number does, since that is what binds the memory pool to the MAC.
    """
    assert authn.hardware_digest({"a": 1, "b": 2}) == authn.hardware_digest({"b": 2, "a": 1})
    assert authn.hardware_digest({"ram_gb": 64.0}) != authn.hardware_digest({"ram_gb": 64.5})
    # Absent and empty are distinct claims and must not collide.
    assert authn.hardware_digest(None) != authn.hardware_digest({})
    # Always a SHA-256 hex digest, whatever it was handed.
    for value in (None, {}, HARDWARE, {"weird": {1, 2, 3}}):
        digest = authn.hardware_digest(value)
        assert len(digest) == 64
        assert set(digest) <= set("0123456789abcdef")


def test_tampering_with_the_hardware_profile_is_rejected():
    """
    THE RESOURCE-POISONING ATTACK, now covered by the MAC.

    Heartbeats are plain HTTP and the module's threat model explicitly includes an on-path
    attacker. Under v1 they could rewrite the reported VRAM without touching the signature,
    and the Coordinator would size its KV cache and admit models against memory that does
    not exist.
    """
    timestamp = time.time()
    signature = authn.sign_heartbeat(
        KEY, WORKER_ID, PORT, TOKEN, timestamp, NONCE, VERSION, HARDWARE
    )

    inflated = json.loads(json.dumps(HARDWARE))
    inflated["gpu"]["vram_total_gb"] = 480.0

    ok, reason = authn.verify_heartbeat(
        KEY, WORKER_ID, PORT, TOKEN, timestamp, NONCE, signature, VERSION, inflated
    )
    assert ok is False
    assert reason == "invalid signature"


def test_tampering_with_the_version_is_rejected():
    """
    THE REMOTE KILL SWITCH, now covered by the MAC.

    A version mismatch is answered with HTTP 426, and the Worker's 426 handler shuts the
    node down. Under v1 that made an unsigned ``version`` field a one-byte way for an on-path
    attacker to take any node in the cluster offline.
    """
    timestamp = time.time()
    signature = authn.sign_heartbeat(
        KEY, WORKER_ID, PORT, TOKEN, timestamp, NONCE, VERSION, HARDWARE
    )

    ok, reason = authn.verify_heartbeat(
        KEY, WORKER_ID, PORT, TOKEN, timestamp, NONCE, signature, "0.0.0-junk", HARDWARE
    )
    assert ok is False
    assert reason == "invalid signature"


def test_timestamp_is_rendered_at_fixed_precision():
    """
    Two timestamps that differ below millisecond resolution must sign identically.

    The timestamp crosses the wire as a JSON number. ``str(float)`` is not guaranteed to
    round-trip byte-identically through every encoder/decoder pair, and one differing digit
    changes the whole MAC. The ``:.3f`` rendering in the canonical string is what removes
    that entire class of "signature mismatch on one OS only" bug.
    """
    first = authn.sign_heartbeat(KEY, WORKER_ID, PORT, TOKEN, 1_700_000_000.00011, NONCE)
    second = authn.sign_heartbeat(KEY, WORKER_ID, PORT, TOKEN, 1_700_000_000.00019, NONCE)
    assert first == second


def test_port_as_string_and_int_sign_identically():
    """
    A JSON ``50052`` and a JSON ``"50052"`` must produce the same MAC.

    Both sides canonicalise through the same coercion, so a worker that serialises its port
    as a string is not locked out for a reason no log line would explain.
    """
    timestamp = time.time()
    assert authn.sign_heartbeat(KEY, WORKER_ID, PORT, TOKEN, timestamp, NONCE) == \
        authn.sign_heartbeat(KEY, WORKER_ID, str(PORT), TOKEN, timestamp, NONCE)


def test_empty_key_produces_no_signature():
    """
    An unset key must yield ``""``, never ``HMAC(key=b"")``.

    A MAC keyed on the empty string looks perfectly valid but can be computed by anyone, so
    it would be strictly worse than an obviously absent signature: the Coordinator would
    accept it while believing the cluster was authenticated.
    """
    assert authn.sign_heartbeat("", WORKER_ID, PORT, TOKEN, time.time(), NONCE) == ""


def test_verify_fails_closed_when_the_verifier_has_no_key():
    """With no key configured there is nothing to verify against, so nothing is accepted."""
    ok, reason = authn.verify_heartbeat(
        "", WORKER_ID, PORT, TOKEN, time.time(), NONCE, "a" * 64
    )
    assert ok is False
    assert reason == "no network key configured"


def test_wrong_key_is_rejected():
    """A heartbeat signed with a different cluster secret must not verify."""
    timestamp = time.time()
    signature = authn.sign_heartbeat(
        "some-other-cluster", WORKER_ID, PORT, TOKEN, timestamp, NONCE
    )

    ok, reason = authn.verify_heartbeat(
        KEY, WORKER_ID, PORT, TOKEN, timestamp, NONCE, signature
    )
    assert ok is False
    assert reason == "invalid signature"


@pytest.mark.parametrize(
    "field, tampered_value",
    [
        ("worker_id", "node-attacker"),
        # The RPC hijack: port and worker identity end up in `llama-server --rpc <ip:port>`.
        ("port", 4444),
        # The free-membership attack: token is what the Non-Contrib gate matches on.
        ("token", "tok-attacker"),
        ("nonce", "ffffffffffffffffffffffffffffffff"),
    ],
)
def test_tampering_with_any_signed_field_is_rejected(field, tampered_value):
    """
    Every field inside the canonical string is covered by the MAC.

    Each case signs a legitimate heartbeat, swaps exactly one field, and asserts the
    signature no longer verifies — which is what stops an on-path attacker from rewriting a
    captured heartbeat's endpoint or token.
    """
    timestamp = time.time()
    fields = {
        "worker_id": WORKER_ID,
        "port": PORT,
        "token": TOKEN,
        "nonce": NONCE,
    }
    signature = authn.sign_heartbeat(KEY, timestamp=timestamp, **fields)

    fields[field] = tampered_value
    ok, reason = authn.verify_heartbeat(
        KEY, timestamp=timestamp, signature=signature, **fields
    )
    assert ok is False
    assert reason == "invalid signature"


def test_tampering_with_the_timestamp_is_rejected():
    """
    The timestamp is signed too, so it cannot be nudged to extend a captured heartbeat's
    life. Shifting it by 5 s keeps it inside the skew window, which isolates the signature
    check from the freshness check.
    """
    timestamp = time.time()
    signature = authn.sign_heartbeat(KEY, WORKER_ID, PORT, TOKEN, timestamp, NONCE)

    ok, reason = authn.verify_heartbeat(
        KEY, WORKER_ID, PORT, TOKEN, timestamp + 5.0, NONCE, signature
    )
    assert ok is False
    assert reason == "invalid signature"


@pytest.mark.parametrize("offset", [-3600.0, -31.0, 31.0, 3600.0])
def test_stale_or_future_timestamps_are_rejected(offset):
    """
    Freshness bounds how long a captured heartbeat stays useful, in both directions.

    A far-future timestamp is as dangerous as an old one: it would let an attacker mint a
    credential now that stays valid for an hour. The default window is +/-30 s.
    """
    timestamp = time.time() + offset
    signature = authn.sign_heartbeat(KEY, WORKER_ID, PORT, TOKEN, timestamp, NONCE)

    ok, reason = authn.verify_heartbeat(
        KEY, WORKER_ID, PORT, TOKEN, timestamp, NONCE, signature
    )
    assert ok is False
    assert reason == "stale timestamp"


def test_timestamp_inside_the_skew_window_is_accepted():
    """Ordinary NTP drift between hobbyist machines must not evict a healthy node."""
    timestamp = time.time() - 20.0
    signature = authn.sign_heartbeat(KEY, WORKER_ID, PORT, TOKEN, timestamp, NONCE)

    assert authn.verify_heartbeat(
        KEY, WORKER_ID, PORT, TOKEN, timestamp, NONCE, signature
    ) == (True, "ok")


@pytest.mark.parametrize("bad_timestamp", [float("nan"), float("inf"), "not-a-number", None])
def test_non_numeric_and_non_finite_timestamps_are_rejected(bad_timestamp):
    """
    NaN makes every comparison false, so without an explicit guard a NaN timestamp would
    sail straight through ``abs(now - ts) > skew``. Infinity and garbage are rejected for
    the same reason: they must never reach the arithmetic.
    """
    ok, reason = authn.verify_heartbeat(
        KEY, WORKER_ID, PORT, TOKEN, bad_timestamp, NONCE, "a" * 64
    )
    assert ok is False
    assert reason == "invalid timestamp"


@pytest.mark.parametrize(
    "signature, expected_reason",
    [
        ("", "missing signature"),
        (None, "missing signature"),
        ("deadbeef", "invalid signature"),          # right alphabet, wrong length
        ("z" * 64, "invalid signature"),            # right length, not a real digest
        ("é" * 64, "invalid signature"),       # non-ASCII cannot be a hex digest
    ],
)
def test_malformed_signatures_are_rejected(signature, expected_reason):
    """Structurally impossible signatures are refused before any comparison is attempted."""
    ok, reason = authn.verify_heartbeat(
        KEY, WORKER_ID, PORT, TOKEN, time.time(), NONCE, signature
    )
    assert ok is False
    assert reason == expected_reason


@pytest.mark.parametrize(
    "nonce, expected_reason",
    [
        ("", "missing nonce"),
        (None, "missing nonce"),
        ("a" * 1000, "nonce too long"),
    ],
)
def test_malformed_nonces_are_rejected(nonce, expected_reason):
    """
    A nonce is retained for its whole TTL, so an unbounded one is a memory-exhaustion lever.
    It is refused before the cache ever sees it.
    """
    ok, reason = authn.verify_heartbeat(
        KEY, WORKER_ID, PORT, TOKEN, time.time(), nonce, "a" * 64
    )
    assert ok is False
    assert reason == expected_reason


def test_rejection_reasons_never_leak_the_key():
    """
    Rejection reasons are surfaced to the caller in the HTTP 401 body, so they must not
    contain key material. This walks every failure mode the verifier has.
    """
    timestamp = time.time()
    good_signature = authn.sign_heartbeat(KEY, WORKER_ID, PORT, TOKEN, timestamp, NONCE)

    rejections = [
        authn.verify_heartbeat("", WORKER_ID, PORT, TOKEN, timestamp, NONCE, good_signature),
        authn.verify_heartbeat(KEY, WORKER_ID, PORT, TOKEN, timestamp, NONCE, ""),
        authn.verify_heartbeat(KEY, WORKER_ID, PORT, TOKEN, timestamp, "", good_signature),
        authn.verify_heartbeat(KEY, WORKER_ID, PORT, TOKEN, 0.0, NONCE, good_signature),
        authn.verify_heartbeat(KEY, "other", PORT, TOKEN, timestamp, NONCE, good_signature),
    ]

    for ok, reason in rejections:
        assert ok is False
        assert KEY not in reason
        assert good_signature not in reason


# ======================================================================================
# NonceCache
# ======================================================================================

def test_nonce_replay_is_detected():
    """
    The core replay case: the identical nonce presented twice.

    A signature stays valid for its whole skew window, so freshness alone does not stop an
    attacker rebroadcasting captured bytes to keep a removed node "alive" in the registry.
    """
    cache = authn.NonceCache()

    assert cache.seen(NONCE) is False   # first sighting: accept and record
    assert cache.seen(NONCE) is True    # replay: reject


def test_distinct_nonces_are_all_accepted():
    """A busy cluster's genuine heartbeats must never collide with each other."""
    cache = authn.NonceCache()
    assert [cache.seen(f"nonce-{i}") for i in range(50)] == [False] * 50
    assert len(cache) == 50


def test_nonce_cache_evicts_on_ttl():
    """
    Entries expire, so memory stays proportional to the heartbeat rate rather than uptime.

    Once a nonce is older than the TTL its heartbeat can no longer be fresh, so forgetting
    it is safe — and the cache must actually forget it, otherwise it grows without bound.
    """
    cache = authn.NonceCache(ttl_seconds=0.05)

    assert cache.seen("short-lived") is False
    assert cache.seen("short-lived") is True

    time.sleep(0.2)

    assert len(cache) == 0
    assert cache.seen("short-lived") is False


def test_nonce_cache_enforces_its_hard_entry_cap():
    """
    TTL alone is not a bound: a flood can insert far more nonces inside one TTL than the
    host has memory for. The cap converts that memory-exhaustion DoS into, at worst, early
    forgetting of the *oldest* entries — which are the closest to expiring anyway.
    """
    cache = authn.NonceCache(ttl_seconds=60.0, max_entries=3)

    for i in range(4):
        assert cache.seen(f"n{i}") is False

    assert len(cache) == 3
    # n0 was the oldest and has been surrendered, so it looks new again...
    assert cache.seen("n0") is False
    # ...while the three most recent are still remembered and still reject replays.
    assert cache.seen("n3") is True


@pytest.mark.parametrize("bad_nonce", ["", None, 12345, b"bytes"])
def test_nonce_cache_fails_closed_on_garbage(bad_nonce):
    """
    Unusable input reports "already seen" so the Coordinator rejects it. Returning False
    would wave malformed heartbeats straight past the replay guard.
    """
    cache = authn.NonceCache()
    assert cache.seen(bad_nonce) is True


@pytest.mark.parametrize("ttl, max_entries", [(0, 10), (-1.0, 10), (60.0, 0), (60.0, -5)])
def test_nonce_cache_refuses_a_configuration_that_would_disable_it(ttl, max_entries):
    """
    A zero TTL or a zero cap retains nothing, so every nonce looks new and the replay guard
    silently stops guarding. A security control that fails open without complaining is worse
    than no control, so construction is refused outright.
    """
    with pytest.raises(ValueError):
        authn.NonceCache(ttl_seconds=ttl, max_entries=max_entries)


# ======================================================================================
# validate_hardware_profile
# ======================================================================================

@pytest.mark.parametrize(
    "profile",
    [
        {},                                                   # nothing reported at all
        {"ram_gb": 32.0},                                     # partial top-level profile
        {"cpu_cores": 8, "cpu_threads": 16},
        {"gpu": {"has_gpu": False}},                          # CPU-only node
        {"gpu": None},                                        # explicit null
        {"ram_gb": 0, "cpu_cores": 0},                        # zero is plausible, not absurd
        {"gpu": {"vram_total_gb": 24.0}, "ram_gb": 64.0,
         "cpu_cores": 16, "cpu_threads": 32},                 # a full, ordinary profile
        {"ram_gb": 32.0, "some_future_field": "anything"},     # unknown keys are ignored
    ],
)
def test_plausible_profiles_are_accepted(profile):
    """
    Absent is not invalid.

    Older workers and CPU-only nodes legitimately omit fields. Rejecting them would break
    rolling upgrades, which in practice means operators disable the check entirely — so the
    validator has to tolerate a partial profile to stay switched on.
    """
    assert authn.validate_hardware_profile(profile) == (True, "ok")


@pytest.mark.parametrize(
    "profile",
    [
        # The resource-poisoning attack: an inflated pool makes the Coordinator admit a
        # model that cannot fit and OOM the whole cluster.
        {"gpu": {"vram_total_gb": 1e9}},
        {"gpu": {"vram_total_gb": 2048.001}},
        {"ram_gb": 8193.0},
        {"cpu_cores": 5000},
        {"cpu_threads": 99999},
        # Negative values propagate as a negative budget deep inside the sizing maths.
        {"ram_gb": -1.0},
        {"gpu": {"vram_total_gb": -0.5}},
        {"cpu_cores": -8},
        # Non-numeric values raise TypeError far from their origin.
        {"ram_gb": "lots"},
        {"cpu_cores": None},
        {"cpu_threads": [1, 2]},
        # bool is an int subclass, so `True` would otherwise register as a 1-core node.
        {"cpu_cores": True},
        {"ram_gb": False},
        # Non-finite values survive every ordinary comparison.
        {"ram_gb": float("nan")},
        {"gpu": {"vram_total_gb": float("inf")}},
        # Wrong container shapes: the Coordinator does hw.get("gpu", {}).get(...), which
        # raises AttributeError (HTTP 500) on these unless they are caught here (HTTP 400).
        {"gpu": "nvidia"},
        {"gpu": [1, 2, 3]},
    ],
)
def test_implausible_profiles_are_rejected(profile):
    """Every rejection returns an operator-readable reason, never a bare False."""
    ok, reason = authn.validate_hardware_profile(profile)
    assert ok is False
    assert reason and reason != "ok"


def test_non_dict_profile_is_rejected():
    """The whole ``hardware`` object is attacker-controlled, including its type."""
    for value in (None, "hardware", 42, ["gpu"]):
        ok, reason = authn.validate_hardware_profile(value)
        assert ok is False
        assert reason == "hardware profile must be an object"


def test_bounds_are_inclusive_at_the_maximum():
    """
    The documented maxima are accepted, not rejected off-by-one. These are sanity clamps far
    above any real machine, so a legitimate node that sits exactly on the line must pass.
    """
    for key, (_minimum, maximum) in authn.HARDWARE_BOUNDS.items():
        assert authn.validate_hardware_profile({key: maximum}) == (True, "ok")


def test_bounds_are_tight_enough_to_be_a_control():
    """
    ITEM 1.4: the clamp has to actually cap what one lying node can add to the pool.

    The previous bounds allowed 2048 GB of VRAM and 8192 GB of RAM *per node* — roughly
    9 TB from a single machine, which is more than the largest model in the library needs.
    Every model was therefore admitted and the cluster OOMed: the exact attack the check
    exists to stop. These assertions pin the maxima so nobody loosens them back without
    reading this comment.
    """
    assert authn.HARDWARE_BOUNDS["vram_total_gb"] == (0.0, 512.0)
    assert authn.HARDWARE_BOUNDS["ram_gb"] == (0.0, 2048.0)
    assert authn.HARDWARE_BOUNDS["cpu_cores"] == (0, 256)
    assert authn.HARDWARE_BOUNDS["cpu_threads"] == (0, 512)


@pytest.mark.parametrize(
    "profile",
    [
        # Each of these was accepted under the old bounds and is the whole attack: one node
        # claiming enough memory to make any model look admissible.
        {"gpu": {"vram_total_gb": 2000.0}},
        {"ram_gb": 8000.0},
        {"cpu_cores": 1000},
        {"cpu_threads": 2000},
    ],
)
def test_values_the_old_loose_bounds_allowed_are_now_refused(profile):
    """A hostile node can no longer inflate the pool into "everything fits" territory."""
    ok, _reason = authn.validate_hardware_profile(profile)
    assert ok is False


@pytest.mark.parametrize(
    "profile",
    [
        # Comfortably beyond any consumer rig, and still accepted: the clamp must not start
        # rejecting real hardware.
        {"gpu": {"vram_total_gb": 320.0}},     # four H100 80GB
        {"ram_gb": 1024.0},                     # a large server
        {"cpu_cores": 128, "cpu_threads": 256},
    ],
)
def test_generous_but_real_hardware_still_passes(profile):
    assert authn.validate_hardware_profile(profile) == (True, "ok")


# --- 2.4.9: the coding job's RAM and a household node's LAN addresses -------------------------------------

def test_the_coding_ram_is_bounded_like_the_ram():
    assert authn.validate_hardware_profile({"coding_ram_gb": 30.0}) == (True, "ok")
    for bad in (-1.0, 4096.0, "lots", True):
        assert authn.validate_hardware_profile({"coding_ram_gb": bad})[0] is False


@pytest.mark.parametrize("lan, ok", [
    (["192.168.40.50"], True),
    (["192.168.40.50", "172.17.0.1", "10.13.13.2"], True),
    ([], True),
    ("192.168.40.50", False),                                   # not a list
    (["192.168.40.50/24"], False),
    (["fe80::1"], False),                                       # dotted quads only
    (["192.168.40.50", 7], False),
    (["x" * 300], False),
    ([f"10.0.0.{i}" for i in range(9)], False),                 # at most eight
])
def test_lan_addresses_are_a_short_list_of_ipv4_addresses(lan, ok):
    assert authn.validate_hardware_profile({"lan_ipv4": lan})[0] is ok


@pytest.mark.parametrize(
    "profile, expected_fragment",
    [
        # is_yielding removes a node from EVERY total. The string "false" is truthy in
        # Python, so this used to silently delete the node's whole contribution from the
        # budget with nothing logged and nothing raised.
        ({"is_yielding": "false"}, "is_yielding"),
        ({"is_yielding": 0}, "is_yielding"),
        ({"is_yielding": None}, "is_yielding"),
        # has_gpu gates the VRAM sum, and it is a plain attacker-set flag.
        ({"gpu": {"has_gpu": "true", "vram_total_gb": 24.0}}, "gpu.has_gpu"),
        ({"gpu": {"has_gpu": 1}}, "gpu.has_gpu"),
        ({"has_gpu": "yes"}, "has_gpu"),
    ],
)
def test_flag_fields_must_be_real_booleans(profile, expected_fragment):
    """
    ITEM 1.4: ``has_gpu`` and ``is_yielding`` decide whether a node is counted at all, so a
    wrong type there is a silent mis-accounting rather than a visible error. Requiring a
    genuine JSON boolean turns it into an honest HTTP 400.
    """
    ok, reason = authn.validate_hardware_profile(profile)
    assert ok is False
    assert expected_fragment in reason


@pytest.mark.parametrize(
    "profile",
    [
        {"is_yielding": True},
        {"is_yielding": False},
        {"gpu": {"has_gpu": True, "vram_total_gb": 24.0}},
        {"gpu": {"has_gpu": False, "vram_total_gb": 0.0}, "is_yielding": False},
    ],
)
def test_genuine_booleans_are_accepted(profile):
    """The positive case, so the new check cannot be satisfied by rejecting everything."""
    assert authn.validate_hardware_profile(profile) == (True, "ok")


def test_nested_vram_is_validated_and_named_in_the_reason():
    """
    ``hardware.get_hardware_profile()`` nests VRAM under ``gpu``, so the nested form is the
    one real workers send. The reason has to say *which* claim was refused, otherwise the
    operator cannot tell a bad probe from a bad node.
    """
    ok, reason = authn.validate_hardware_profile({"gpu": {"vram_total_gb": 999999.0}})
    assert ok is False
    assert "gpu.vram_total_gb" in reason
