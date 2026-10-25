"""
Tests for tunnel self-enrollment — the path that makes one join file serve every node.

WHAT THIS REPLACES
------------------
A tunnel address could previously only be handed out by an operator running the setup
wizard, which minted a complete WireGuard invite per node, private key included. Fifty
testers meant fifty files, and the obvious shortcut of copying one invite is broken by
construction: WireGuard identifies a peer by public key, so N nodes sharing a keypair are
one peer and the address flaps to whichever handshook last.

Enrollment inverts it. The node generates its own keypair, keeps the private half, and
presents only the public half signed with the cluster's NETWORK_KEY — the secret that
already proves membership. The join file therefore contains no private key and can be
handed to anyone.

WHAT IS ASSERTED HERE
---------------------
The signing primitive (domain separation, tamper detection, replay), the allocator's
idempotency (the property that stops a nightly-rebooting node from exhausting the /24),
and the join file's parsing and refusal cases.

The ``wg`` binary is not assumed to exist on the machine running these tests, so key
generation is stubbed wherever it is reached. The keys below are syntactically real
Curve25519 base64 so the Coordinator's shape check is genuinely exercised.
"""

import os
import time

import pytest

import engine.authn as authn
import engine.setup_core as setup_core
import engine.wireguard as wireguard


NETWORK_KEY = "b8e1f0c9a7d24e3f8a6b5c4d3e2f1a0b9c8d7e6f5a4b3c2d1e0f9a8b7c6d5e4f"
PUBKEY_A = "gOpcFhvKSI8J4CYUMRZNMYzhkmvJPovr6fdDhihq9Uc="
PUBKEY_B = "TEd0aGlzSXNBbm90aGVyVmFsaWRLZXlTaGFwZUFBQT0="


# --------------------------------------------------------------------------------------
# The signing primitive
# --------------------------------------------------------------------------------------

def test_a_valid_enrollment_verifies():
    now = time.time()
    signature = authn.sign_enrollment(NETWORK_KEY, "node-1", PUBKEY_A, now, "nonce-1")

    assert authn.verify_enrollment(
        NETWORK_KEY, "node-1", PUBKEY_A, now, "nonce-1", signature) == (True, "ok")


def test_the_public_key_is_inside_the_mac():
    """
    The whole request is that key. An on-path attacker who could swap it would have the
    Coordinator register THEIR key against a legitimate node's enrollment, taking the
    tunnel address and every packet routed to it.
    """
    now = time.time()
    signature = authn.sign_enrollment(NETWORK_KEY, "node-1", PUBKEY_A, now, "nonce-1")

    ok, reason = authn.verify_enrollment(
        NETWORK_KEY, "node-1", PUBKEY_B, now, "nonce-1", signature)

    assert (ok, reason) == (False, "invalid signature")


def test_a_heartbeat_signature_cannot_be_replayed_as_an_enrollment():
    """
    Domain separation. Both protocols are MACed with the same NETWORK_KEY, so without a
    distinct prefix a captured heartbeat would be a valid enrollment for whatever its
    fields happened to line up with.
    """
    now = time.time()
    heartbeat_signature = authn.sign_heartbeat(
        NETWORK_KEY, "node-1", 50052, PUBKEY_A, now, "nonce-1")

    ok, _ = authn.verify_enrollment(
        NETWORK_KEY, "node-1", PUBKEY_A, now, "nonce-1", heartbeat_signature)

    assert ok is False


def test_an_enrollment_signature_cannot_be_replayed_as_a_heartbeat():
    """The separation has to hold in both directions to be worth anything."""
    now = time.time()
    enrollment_signature = authn.sign_enrollment(
        NETWORK_KEY, "node-1", PUBKEY_A, now, "nonce-1")

    ok, _ = authn.verify_heartbeat(
        NETWORK_KEY, "node-1", 50052, PUBKEY_A, now, "nonce-1", enrollment_signature)

    assert ok is False


def test_a_stale_request_is_refused():
    old = time.time() - 3600
    signature = authn.sign_enrollment(NETWORK_KEY, "node-1", PUBKEY_A, old, "nonce-1")

    assert authn.verify_enrollment(
        NETWORK_KEY, "node-1", PUBKEY_A, old, "nonce-1", signature) == (False, "stale timestamp")


def test_a_different_key_does_not_verify():
    now = time.time()
    signature = authn.sign_enrollment("some-other-network", "node-1", PUBKEY_A, now, "n")

    ok, reason = authn.verify_enrollment(
        NETWORK_KEY, "node-1", PUBKEY_A, now, "n", signature)

    assert (ok, reason) == (False, "invalid signature")


@pytest.mark.parametrize("description,kwargs,expected", [
    ("no key configured", {"network_key": ""}, "no network key configured"),
    ("no signature", {"signature": ""}, "missing signature"),
    ("no nonce", {"nonce": ""}, "missing nonce"),
    ("no public key", {"public_key": ""}, "missing public key"),
    ("nonce far too long", {"nonce": "x" * 500}, "nonce too long"),
    ("timestamp is not a number", {"timestamp": "yesterday"}, "invalid timestamp"),
    ("timestamp is NaN", {"timestamp": float("nan")}, "invalid timestamp"),
])
def test_malformed_requests_are_refused_with_a_specific_reason(description, kwargs, expected):
    """Fail closed, and say which check failed — the reasons reach the operator's log."""
    fields = {
        "network_key": NETWORK_KEY, "worker_id": "node-1", "public_key": PUBKEY_A,
        "timestamp": time.time(), "nonce": "nonce-1", "signature": "a" * 64,
    }
    fields.update(kwargs)

    ok, reason = authn.verify_enrollment(**fields)

    assert (ok, reason) == (False, expected)


def test_an_absent_key_produces_no_signature_rather_than_a_forgeable_one():
    """HMAC over an empty key is a signature every attacker can also compute."""
    assert authn.sign_enrollment("", "node-1", PUBKEY_A, time.time(), "n") == ""


# --------------------------------------------------------------------------------------
# Address allocation
# --------------------------------------------------------------------------------------

@pytest.fixture
def hub(dlne_root, isolated_config, monkeypatch):
    """A WireGuardOrchestrator with a Coordinator interface and no real `wg` binary."""
    monkeypatch.setattr(wireguard, "get_base_dir", lambda: str(dlne_root))
    orchestrator = wireguard.WireGuardOrchestrator()
    with open(orchestrator.coordinator_conf_path, "w", encoding="utf-8") as handle:
        handle.write("[Interface]\nPrivateKey = kOhAmVQBn0kSuPeRsEcReTpRiVaTeKeYaAAA=\n"
                     "ListenPort = 51820\nAddress = 10.8.0.1/24\n")
    return orchestrator


def test_a_new_key_is_given_the_first_free_address(hub):
    assert hub.enroll_peer(PUBKEY_A, "first worker") == 2


def test_re_enrolling_the_same_key_returns_the_same_address(hub):
    """
    The property that stops the subnet leaking.

    A node re-enrolls every time it restarts or setup is re-run. Allocating afresh each
    time would walk a nightly-rebooting node up the /24 and exhaust it inside a year.
    """
    first = hub.enroll_peer(PUBKEY_A, "worker")
    second = hub.enroll_peer(PUBKEY_A, "worker")
    third = hub.enroll_peer(PUBKEY_A, "worker")

    assert first == second == third == 2
    # And exactly one peer block was written, not three.
    assert hub.coordinator_conf_path
    with open(hub.coordinator_conf_path, encoding="utf-8") as handle:
        assert handle.read().count(PUBKEY_A) == 1


def test_distinct_keys_get_distinct_addresses(hub):
    """Two nodes must never share an address, which is the whole reason invites were per-node."""
    assert hub.enroll_peer(PUBKEY_A, "worker a") == 2
    assert hub.enroll_peer(PUBKEY_B, "worker b") == 3


def test_an_address_already_in_the_interface_file_is_never_reissued(hub):
    """
    A hand-added peer, or a config.txt restored from an older backup, must not cause a
    collision — the allocator reads the addresses actually in use, not just its counter.
    """
    with open(hub.coordinator_conf_path, "a", encoding="utf-8") as handle:
        handle.write("\n[Peer]\n# added by hand\nPublicKey = "
                     "aGFuZE1hZGVQZWVyS2V5VGhhdExvb2tzUmVhbEE9\nAllowedIPs = 10.8.0.2/32\n")

    assert hub.enroll_peer(PUBKEY_A, "worker") == 3


def test_a_full_subnet_is_an_explicit_error_not_a_wraparound(hub, monkeypatch):
    """Wrapping around to .2 would silently corrupt a working cluster."""
    monkeypatch.setattr(hub, "_octets_in_use",
                        lambda: set(range(1, wireguard.WG_LAST_PEER_OCTET + 1)))

    with pytest.raises(wireguard.WireGuardUnavailableError) as excinfo:
        hub.enroll_peer(PUBKEY_A, "worker")

    assert "full" in str(excinfo.value).lower()


def test_an_unknown_key_has_no_address(hub):
    assert hub.find_peer_octet(PUBKEY_B) is None
    assert hub.find_peer_octet("") is None


def test_the_hubs_own_public_key_is_derived_from_its_interface(hub, monkeypatch):
    """
    setup_coordinator_server returns the public key once and nothing persists it, but
    enrollment happens later in a different process and has to answer with it.
    """
    monkeypatch.setattr(hub, "_run_wg_cmd",
                        lambda *args, **kwargs: "DERIVEDPUBLICKEYAAAAAAAAAAAAAAAAAAAAAAAAAAA=")

    assert hub.coordinator_public_key() == "DERIVEDPUBLICKEYAAAAAAAAAAAAAAAAAAAAAAAAAAA="


def test_a_peers_public_key_is_never_mistaken_for_the_hubs(hub, monkeypatch):
    """
    Returning a peer's key here would tell every joining node to encrypt to another
    worker, producing a tunnel that never handshakes.
    """
    hub.enroll_peer(PUBKEY_A, "worker")
    # An interface file whose [Interface] block has lost its PrivateKey.
    with open(hub.coordinator_conf_path, encoding="utf-8") as handle:
        text = handle.read()
    stripped = "\n".join(line for line in text.splitlines()
                         if not line.strip().lower().startswith("privatekey"))
    with open(hub.coordinator_conf_path, "w", encoding="utf-8") as handle:
        handle.write(stripped)

    monkeypatch.setattr(hub, "_run_wg_cmd", lambda *a, **k: "SHOULDNEVERBEREACHED=")

    assert hub.coordinator_public_key() is None


def test_the_member_conf_matches_the_shape_of_a_minted_invite(hub):
    """
    An enrolled node and a hand-invited one must produce the same file, so that wg-quick,
    the auto-join path and the vault backup all have exactly one shape to handle.
    """
    path = hub.write_member_conf(
        "cHJpVmF0ZUtleUZvclRlc3RpbmdQdXJwb3Nlc09ubHk=", 7,
        PUBKEY_A, "203.0.113.9:51820", network_key=NETWORK_KEY)

    with open(path, encoding="utf-8") as handle:
        text = handle.read()

    assert "Address = 10.8.0.7/24" in text
    assert f"PublicKey = {PUBKEY_A}" in text
    assert "Endpoint = 203.0.113.9:51820" in text
    assert f"AllowedIPs = {wireguard.WG_SUBNET_CIDR}" in text
    # The network key rides as a comment so the file stays a valid wg-quick config, and
    # setup_core.parse_invite reads it back out of an enrolled conf exactly as it does an
    # invited one.
    assert setup_core.parse_invite(path)["network_key"] == NETWORK_KEY


# --------------------------------------------------------------------------------------
# The join file
# --------------------------------------------------------------------------------------

def test_a_join_file_round_trips(dlne_root, monkeypatch):
    monkeypatch.setattr(setup_core, "get_base_dir", lambda: str(dlne_root))
    path = str(dlne_root / f"network_join{setup_core.JOIN_FILE_SUFFIX}")

    setup_core.write_join_file(path, coordinator_host="203.0.113.9",
                               network_key=NETWORK_KEY,
                               wg_endpoint="203.0.113.9:51820", wg_pubkey=PUBKEY_A)
    parsed = setup_core.parse_join_file(path)

    assert parsed["ok"] is True
    assert parsed["coordinator_host"] == "203.0.113.9"
    assert parsed["network_key"] == NETWORK_KEY
    assert parsed["wg_pubkey"] == PUBKEY_A


def test_a_join_file_contains_no_private_key(dlne_root):
    """The entire point. This file is handed to strangers."""
    path = str(dlne_root / f"network_join{setup_core.JOIN_FILE_SUFFIX}")

    setup_core.write_join_file(path, coordinator_host="203.0.113.9",
                               network_key=NETWORK_KEY,
                               wg_endpoint="203.0.113.9:51820", wg_pubkey=PUBKEY_A)

    with open(path, encoding="utf-8") as handle:
        text = handle.read()

    assert "PrivateKey" not in text
    assert "[Interface]" not in text


def test_a_join_file_without_a_network_key_is_refused(tmp_path):
    """
    Without it the node can sign neither its enrollment nor its heartbeats. Failing at
    parse time is far clearer than a 401 twenty seconds after launch.
    """
    path = tmp_path / f"broken{setup_core.JOIN_FILE_SUFFIX}"
    path.write_text("COORDINATOR_HOST=203.0.113.9\n", encoding="utf-8")

    parsed = setup_core.parse_join_file(str(path))

    assert parsed["ok"] is False
    assert "NETWORK_KEY" in parsed["error"]


def test_a_join_file_without_a_coordinator_is_refused(tmp_path):
    path = tmp_path / f"broken{setup_core.JOIN_FILE_SUFFIX}"
    path.write_text(f"NETWORK_KEY={NETWORK_KEY}\n", encoding="utf-8")

    parsed = setup_core.parse_join_file(str(path))

    assert parsed["ok"] is False
    assert "Coordinator" in parsed["error"]


def test_an_unreadable_join_file_reports_rather_than_raises(tmp_path):
    parsed = setup_core.parse_join_file(str(tmp_path / "absent.dlnejoin"))

    assert parsed["ok"] is False
    assert parsed["error"]


def test_join_files_are_found_beside_the_binary_and_in_config(dlne_root, tmp_path, monkeypatch):
    monkeypatch.setattr(setup_core, "get_base_dir", lambda: str(dlne_root))
    # Downloads, Desktop and the working directory are searched too, so they have to be
    # pinned somewhere empty. Otherwise this test reads whatever the developer happens to
    # have in their own Downloads folder and stops being a test.
    monkeypatch.setattr(os.path, "expanduser", lambda _: str(tmp_path / "nowhere"))
    monkeypatch.chdir(tmp_path)
    (dlne_root / f"a{setup_core.JOIN_FILE_SUFFIX}").write_text("x", encoding="utf-8")
    (dlne_root / "config" / f"b{setup_core.JOIN_FILE_SUFFIX}").write_text("x", encoding="utf-8")
    (dlne_root / "unrelated.conf").write_text("x", encoding="utf-8")

    found = [p.replace("\\", "/") for p in setup_core.find_join_files()]

    assert len(found) == 2
    assert any(p.endswith(f"a{setup_core.JOIN_FILE_SUFFIX}") for p in found)
    assert any(p.endswith(f"config/b{setup_core.JOIN_FILE_SUFFIX}") for p in found)


def test_a_join_file_left_in_downloads_is_found(dlne_root, tmp_path, monkeypatch):
    """
    The realistic case. A tester downloads the installer and the join file together, runs
    the installer, and the software lands in %LOCALAPPDATA% while the join file stays in
    Downloads. Searching only the install root means talking every one of them through
    finding a hidden directory.
    """
    monkeypatch.setattr(setup_core, "get_base_dir", lambda: str(dlne_root))
    home = tmp_path / "home"
    (home / "Downloads").mkdir(parents=True)
    monkeypatch.setattr(os.path, "expanduser", lambda _: str(home))
    monkeypatch.chdir(tmp_path)
    (home / "Downloads" / f"network_join{setup_core.JOIN_FILE_SUFFIX}").write_text(
        "x", encoding="utf-8"
    )

    found = setup_core.find_join_files()

    assert len(found) == 1
    assert found[0].endswith(f"network_join{setup_core.JOIN_FILE_SUFFIX}")


def test_the_same_join_file_reachable_two_ways_is_offered_once(dlne_root, tmp_path, monkeypatch):
    """Install root doubling as the working directory must not show the file twice."""
    monkeypatch.setattr(setup_core, "get_base_dir", lambda: str(dlne_root))
    monkeypatch.setattr(os.path, "expanduser", lambda _: str(tmp_path / "nowhere"))
    monkeypatch.chdir(dlne_root)
    (dlne_root / f"network_join{setup_core.JOIN_FILE_SUFFIX}").write_text("x", encoding="utf-8")

    assert len(setup_core.find_join_files()) == 1


# --------------------------------------------------------------------------------------
# The endpoint
# --------------------------------------------------------------------------------------

@pytest.fixture
def enroll_client(hub, dlne_root, make_worker_limits, monkeypatch):
    """
    A TestClient over the real Coordinator app, with a WireGuard hub on the temp root.

    Imported inside the fixture because constructing the FastAPI app is expensive, matching
    the pattern in test_update_flow.
    """
    from fastapi.testclient import TestClient

    from engine import coordinator as coordinator_module

    monkeypatch.setattr(coordinator_module, "get_worker_limits",
                        lambda: make_worker_limits(network_key=NETWORK_KEY))
    monkeypatch.setattr(wireguard, "get_base_dir", lambda: str(dlne_root))
    monkeypatch.setattr(wireguard.WireGuardOrchestrator, "_run_wg_cmd",
                        lambda self, *a, **k: "SGVyZUlzVGhlSHVic093blB1YmxpY0tleUFBQUE=")
    coordinator_module._nonce_cache.clear()

    return TestClient(coordinator_module.app)


def signed_enrollment(public_key=PUBKEY_A, worker_id="node-1", nonce="enrol-nonce-1",
                      network_key=NETWORK_KEY):
    now = time.time()
    return {
        "worker_id": worker_id,
        "public_key": public_key,
        "timestamp": now,
        "nonce": nonce,
        "signature": authn.sign_enrollment(network_key, worker_id, public_key, now, nonce),
    }


def test_a_signed_enrollment_is_assigned_an_address(enroll_client):
    response = enroll_client.post("/worker/enroll", json=signed_enrollment())

    assert response.status_code == 200
    body = response.json()
    assert body["tunnel_address"] == "10.8.0.2"
    assert body["subnet_cidr"] == wireguard.WG_SUBNET_CIDR
    assert body["coordinator_pubkey"] == "SGVyZUlzVGhlSHVic093blB1YmxpY0tleUFBQUE="
    assert body["coordinator_endpoint"].endswith(":51820")


def test_an_unsigned_enrollment_is_refused(enroll_client):
    payload = signed_enrollment()
    payload["signature"] = ""

    response = enroll_client.post("/worker/enroll", json=payload)

    assert response.status_code == 401


def test_a_replayed_enrollment_is_refused(enroll_client):
    """
    A captured request resent inside the skew window would re-register a public key the
    attacker has now seen. The nonce cache is not optional here.
    """
    payload = signed_enrollment()

    assert enroll_client.post("/worker/enroll", json=payload).status_code == 200
    assert enroll_client.post("/worker/enroll", json=payload).status_code == 401


def test_a_malformed_public_key_is_refused_before_it_is_written(enroll_client, hub):
    """
    Authenticated is not well-formed. This value is appended to a file WireGuard parses,
    so a key containing a newline could inject directives — an AllowedIPs claiming the
    whole subnet would route every node's traffic to the caller.
    """
    injected = "abc\nAllowedIPs = 10.8.0.0/24\n"
    response = enroll_client.post("/worker/enroll", json=signed_enrollment(public_key=injected))

    assert response.status_code == 400
    with open(hub.coordinator_conf_path, encoding="utf-8") as handle:
        assert "10.8.0.0/24" not in handle.read()


def test_enrollment_is_idempotent_over_the_wire(enroll_client):
    """Distinct nonces, same key: the second call must not consume a second address."""
    first = enroll_client.post("/worker/enroll", json=signed_enrollment(nonce="n-1"))
    second = enroll_client.post("/worker/enroll", json=signed_enrollment(nonce="n-2"))

    assert first.json()["tunnel_address"] == second.json()["tunnel_address"] == "10.8.0.2"


def test_a_coordinator_with_no_network_key_enrolls_nobody(enroll_client, make_worker_limits,
                                                          monkeypatch):
    """
    ALLOW_UNSIGNED_WORKERS exists to migrate pre-Contract-A workers. There are no
    pre-existing unsigned enrollments to migrate, so honouring it here would only mean
    handing tunnel addresses to anonymous callers.
    """
    from engine import coordinator as coordinator_module

    monkeypatch.setattr(coordinator_module, "get_worker_limits",
                        lambda: make_worker_limits(network_key="",
                                                   allow_unsigned_workers=True))

    response = enroll_client.post("/worker/enroll", json=signed_enrollment(network_key=""))

    assert response.status_code == 401


# --------------------------------------------------------------------------------------
# Node identity
# --------------------------------------------------------------------------------------

def test_the_worker_id_is_minted_once_and_reused(dlne_root, monkeypatch):
    """
    Enrollment runs during setup, before any WorkerNode exists, and must present the same
    identity the node later heartbeats under — so both go through this one function.
    """
    monkeypatch.setattr(setup_core, "get_base_dir", lambda: str(dlne_root))

    first = setup_core.get_or_create_worker_id()
    second = setup_core.get_or_create_worker_id()

    assert first == second
    assert (dlne_root / "config" / ".dlne_id").read_text(encoding="utf-8").strip() == first
