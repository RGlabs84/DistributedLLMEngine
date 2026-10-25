"""
Tests for ``engine.wireguard`` invite generation — audit item 3.4.

THE BUG THIS FILE EXISTS TO PREVENT
    ``generate_worker_invite`` hardcoded ``Address = 10.8.0.2/24``, and the setup wizard
    produced the redundancy node's invite with ``shutil.copy`` of the worker's. Every node
    in the cluster therefore received the *same* WireGuard private key and the *same*
    tunnel address, and none of them were ever registered as peers on the Coordinator's
    own interface. WireGuard identifies a peer by its public key and routes to it by its
    allowed IP, so that arrangement can only ever have one node connected at a time — the
    WAN multi-node deployment did not work at all.

    Nothing about that failure is visible from a single-node test: one worker connects
    perfectly. It only shows up as "the second node silently never joins", which is
    exactly the kind of defect that stays in a product for a year. Hence these tests.

The ``wg`` binary is replaced with a deterministic fake, so the suite still runs on a
machine with no WireGuard installation (a hard requirement of the test suite).
"""

import os

import pytest

import engine.config as engine_config
import engine.wireguard as wireguard
from engine.wireguard import WireGuardOrchestrator, WireGuardUnavailableError


COORDINATOR_PUBKEY = "COORD" + "0" * 38 + "="


@pytest.fixture
def wg(isolated_config, dlne_root, monkeypatch):
    """
    A :class:`WireGuardOrchestrator` writing into the throwaway install root.

    ``engine.wireguard`` did ``from engine.config import get_base_dir`` at import time, so
    the function object is bound in *its* module globals; patching ``engine.config`` alone
    would leave the orchestrator writing next to the developer's real installation. Both
    namespaces are therefore patched (the same reasoning as conftest.py's module docstring).
    """
    monkeypatch.setattr(wireguard, "get_base_dir", lambda: str(dlne_root))

    generated = []

    def fake_keypair(self):
        """Deterministic stand-in for ``wg genkey`` / ``wg pubkey``."""
        index = len(generated) + 1
        pair = (f"PRIV{index:040d}=", f"PUB{index:041d}=")
        generated.append(pair)
        return pair

    monkeypatch.setattr(WireGuardOrchestrator, "generate_keypair", fake_keypair)

    def fake_wg_cmd(self, *args, stdin_text=None):
        """
        Stands in for the ``wg`` binary, which the suite must run without.

        Only the two invocations the orchestrator actually makes outside key generation:
        deriving a public key from a private one (a pure function, and the fake keypairs
        are built so PRIV<n> pairs with PUB<n>), and listing live interfaces — of which a
        test machine has none, so the answer is empty rather than an error.
        """
        if args[:1] == ("pubkey",):
            private = (stdin_text or "").strip()
            for priv, pub in generated:
                if priv == private:
                    return pub
            return None
        if args[:2] == ("show", "interfaces"):
            return ""
        return None

    monkeypatch.setattr(WireGuardOrchestrator, "_run_wg_cmd", fake_wg_cmd)

    orchestrator = WireGuardOrchestrator()
    # Every keypair handed out, in order, so a test can assert exactly which public key
    # ended up registered on the Coordinator rather than inferring it from the filenames.
    orchestrator.generated_keypairs = generated
    return orchestrator


def read_field(conf_path: str, field: str) -> str:
    """Returns the first value of ``field`` in a WireGuard config, or '' if absent."""
    with open(conf_path, "r", encoding="utf-8") as handle:
        for line in handle:
            name, separator, value = line.partition("=")
            if separator and name.strip().lower() == field.lower():
                return value.strip()
    return ""


def peer_sections(conf_text: str) -> list:
    """Returns ``[(public_key, allowed_ips), ...]`` for every [Peer] in a config."""
    peers = []
    current = {}
    for line in conf_text.splitlines():
        stripped = line.strip()
        if stripped == "[Peer]":
            current = {}
            peers.append(current)
            continue
        name, separator, value = stripped.partition("=")
        if separator and current is not None and peers:
            peers[-1][name.strip()] = value.strip()
    return [(peer.get("PublicKey", ""), peer.get("AllowedIPs", "")) for peer in peers]


def issue(wg, filename, peer_name="node"):
    """Generates one invite with the arguments the setup wizard uses."""
    return wg.generate_worker_invite(
        coordinator_external_ip="203.0.113.9",
        coordinator_pubkey=COORDINATOR_PUBKEY,
        network_key="shared-secret",
        filename=filename,
        peer_name=peer_name,
    )


# ======================================================================================
# Distinct identities per invite
# ======================================================================================

def test_each_invite_gets_its_own_key_and_address(wg):
    """The core of item 3.4: no two invites may share a private key or an address."""
    wg.setup_coordinator_server()

    paths = [
        issue(wg, "worker_invite.conf", "worker"),
        issue(wg, "redundancy_invite.conf", "redundancy node"),
        issue(wg, "worker_invite_2.conf", "worker 2"),
    ]

    private_keys = [read_field(path, "PrivateKey") for path in paths]
    addresses = [read_field(path, "Address") for path in paths]

    assert all(private_keys), "an invite was written without a private key"
    assert len(set(private_keys)) == 3, f"private keys are not distinct: {private_keys}"
    # Sequential from .2, because the Coordinator itself owns .1.
    assert addresses == ["10.8.0.2/24", "10.8.0.3/24", "10.8.0.4/24"]


def test_every_invite_is_registered_as_a_coordinator_peer(wg):
    """
    The half that was missing entirely: WireGuard drops handshakes from public keys the
    interface has never been told about, so an unregistered invite can never connect no
    matter how correct the invite file itself is.

    ``AllowedIPs`` must be a /32. A /24 here would make the Coordinator route the whole
    tunnel subnet to whichever peer happened to be listed last.
    """
    wg.setup_coordinator_server()

    issue(wg, "worker_invite.conf", "worker")
    issue(wg, "redundancy_invite.conf", "redundancy node")

    with open(wg.coordinator_conf_path, "r", encoding="utf-8") as handle:
        coordinator_text = handle.read()
    peers = peer_sections(coordinator_text)

    # Keypair 0 belongs to the Coordinator's own interface; 1 and 2 are the two invites.
    # The invite keeps the private half, the Coordinator is told the public half.
    _, worker_pubkey = wg.generated_keypairs[1]
    _, redundancy_pubkey = wg.generated_keypairs[2]

    assert peers == [(worker_pubkey, "10.8.0.2/32"),
                     (redundancy_pubkey, "10.8.0.3/32")]

    # A private key must never leak into the Coordinator's peer list.
    for private_key, _ in wg.generated_keypairs[1:]:
        assert private_key not in coordinator_text


def test_invites_still_route_the_whole_subnet_back_to_the_coordinator(wg):
    """
    The peer entry *inside the invite* describes the Coordinator, so it keeps the /24 —
    that is what installs the route to every other node in the tunnel on the worker.
    """
    wg.setup_coordinator_server()
    path = issue(wg, "worker_invite.conf", "worker")

    assert read_field(path, "AllowedIPs") == "10.8.0.0/24"
    assert read_field(path, "PublicKey") == COORDINATOR_PUBKEY
    assert read_field(path, "Endpoint") == "203.0.113.9:51820"


def test_network_key_is_still_carried_as_a_comment(wg):
    """
    The shared secret rides along as a ``#`` comment so the file stays a valid WireGuard
    config that ``wg-quick`` accepts untouched, while setup_cli's auto-join can read it.
    Item 3.4 rewrote this function, so the behaviour is pinned here.
    """
    wg.setup_coordinator_server()
    path = issue(wg, "worker_invite.conf", "worker")

    with open(path, "r", encoding="utf-8") as handle:
        assert "# DLNE_NETWORK_KEY=shared-secret" in handle.read()


# ======================================================================================
# The address allocator
# ======================================================================================

def test_allocator_high_water_mark_is_persisted(wg):
    """
    Invites are routinely issued across separate runs of the setup wizard. Without a
    persisted counter the second run would start again at .2 and collide with the nodes
    the first run already deployed.
    """
    wg.setup_coordinator_server()
    issue(wg, "worker_invite.conf", "worker")

    persisted = engine_config.get_config(force_reload=True)[wireguard.WG_NEXT_PEER_OCTET_KEY]
    assert persisted == "3"


def test_allocator_never_reissues_an_address_that_is_already_registered(wg):
    """
    Self-healing. config.txt can legitimately be behind the interface file — restored from
    an older backup, or hand-edited. Addresses that are demonstrably live in
    coordinator_wg0.conf must still be treated as taken.
    """
    wg.setup_coordinator_server()
    issue(wg, "a.conf", "a")
    issue(wg, "b.conf", "b")

    # Simulate the restored-from-backup case: the counter is rolled back to the start.
    engine_config.set_config_values({wireguard.WG_NEXT_PEER_OCTET_KEY: "2"})

    path = issue(wg, "c.conf", "c")
    assert read_field(path, "Address") == "10.8.0.4/24"


def test_allocator_recovers_from_a_corrupt_counter(wg):
    """A hand-mangled counter must not raise inside the setup wizard."""
    wg.setup_coordinator_server()
    engine_config.set_config_values({wireguard.WG_NEXT_PEER_OCTET_KEY: "not-a-number"})

    path = issue(wg, "worker_invite.conf", "worker")
    assert read_field(path, "Address") == "10.8.0.2/24"


def test_a_full_subnet_is_an_explicit_error(wg):
    """
    253 nodes is far beyond anything DLNE targets, but wrapping silently back to .2 would
    corrupt a working cluster, so exhaustion has to be loud.
    """
    wg.setup_coordinator_server()
    engine_config.set_config_values(
        {wireguard.WG_NEXT_PEER_OCTET_KEY: str(wireguard.WG_LAST_PEER_OCTET)}
    )

    last = issue(wg, "last.conf", "last")
    assert read_field(last, "Address") == f"10.8.0.{wireguard.WG_LAST_PEER_OCTET}/24"

    with pytest.raises(WireGuardUnavailableError) as excinfo:
        issue(wg, "one_too_many.conf", "overflow")
    assert "full" in str(excinfo.value).lower()


def test_rerunning_setup_keeps_the_coordinator_identity(wg):
    """
    THE FAILURE WITH NO SYMPTOM.

    This used to mint a new keypair on every call and rewrite the interface file whole.
    Re-running setup on a live Coordinator therefore rotated its tunnel identity behind its
    own back — and nothing restarts the interface as part of setup, so the running tunnel
    kept the old key while the file held the new one.

    From then on ``/worker/enroll`` handed every joining node the key out of the *file*,
    the interface answered with the one in the *kernel*, and every tunnel built against
    that mismatch was pinned to a key the server does not hold. Both ends look correctly
    configured. No handshake ever completes. Nothing is logged, because from each end
    nothing went wrong.
    """
    first = wg.setup_coordinator_server()

    second = wg.setup_coordinator_server()

    assert second == first
    assert wg.coordinator_public_key() == first


def test_rerunning_setup_keeps_the_peers_and_their_addresses(wg):
    """
    The other half of the same wholesale rewrite: every registered ``[Peer]`` was dropped
    and the allocator reset, so the next node issued was handed 10.8.0.2 — an address two
    already-enrolled peers were still using.
    """
    wg.setup_coordinator_server()
    issue(wg, "a.conf", "a")
    issue(wg, "b.conf", "b")

    wg.setup_coordinator_server()

    with open(wg.coordinator_conf_path, encoding="utf-8") as handle:
        assert handle.read().count("[Peer]") == 2

    path = issue(wg, "fresh.conf", "fresh")
    assert read_field(path, "Address") == "10.8.0.4/24"


def test_rotation_is_available_but_never_the_default(wg):
    """
    An operator who means it — after a key compromise — can still replace the identity.
    Every invite and every enrolled peer is invalidated, which is why it has to be asked
    for rather than happening because setup was opened twice.
    """
    first = wg.setup_coordinator_server()

    rotated = wg.setup_coordinator_server(rotate_identity=True)

    assert rotated != first
    assert wg.coordinator_public_key() == rotated
    # A new generation starts its addresses again from the bottom of the subnet.
    assert read_field(issue(wg, "fresh.conf", "fresh"), "Address") == "10.8.0.2/24"


def test_an_unreadable_existing_identity_is_never_replaced(wg, monkeypatch):
    """
    Fail closed. If the tools cannot derive the public key of an interface that exists,
    that is a transient inability to *read* an identity — treating it as an absent one and
    generating a replacement would disconnect every peer over a temporary fault.
    """
    wg.setup_coordinator_server()
    before = open(wg.coordinator_conf_path, encoding="utf-8").read()
    monkeypatch.setattr(WireGuardOrchestrator, "_run_wg_cmd",
                        lambda self, *a, **k: None)

    with pytest.raises(WireGuardUnavailableError) as excinfo:
        wg.setup_coordinator_server()

    assert "refusing to replace" in str(excinfo.value).lower()
    assert open(wg.coordinator_conf_path, encoding="utf-8").read() == before


# ======================================================================================
# The running interface versus the configured one
# ======================================================================================

def test_a_diverged_interface_is_reported(wg, monkeypatch):
    """The whole point of the check: say the thing that nothing else would say."""
    wg.setup_coordinator_server()
    monkeypatch.setattr(WireGuardOrchestrator, "live_interface_public_key",
                        lambda self: "SOMEONEELSE" + "0" * 32 + "=")

    message = wg.warn_if_interface_diverged()

    assert message is not None
    assert "handshake" in message.lower()


def test_an_agreeing_interface_is_silent(wg, monkeypatch):
    configured = wg.setup_coordinator_server()
    monkeypatch.setattr(WireGuardOrchestrator, "live_interface_public_key",
                        lambda self: configured)

    assert wg.warn_if_interface_diverged() is None


def test_an_interface_that_is_not_running_is_not_a_divergence(wg):
    """
    Fails open, and must. A Coordinator serving only its own LAN runs no tunnel at all,
    and "not up" is not evidence of a mismatch — nor is "wg would not answer me".
    """
    wg.setup_coordinator_server()

    assert wg.warn_if_interface_diverged() is None


def test_a_coordinator_with_no_interface_asks_wireguard_nothing(wg, monkeypatch):
    """A node that runs no tunnel must not log about tunnels on every start."""
    calls = []
    monkeypatch.setattr(WireGuardOrchestrator, "_run_wg_cmd",
                        lambda self, *a, **k: calls.append(a) or None)

    assert wg.warn_if_interface_diverged() is None
    assert calls == []


# ======================================================================================
# Failure handling
# ======================================================================================

def test_a_failed_keypair_writes_nothing_and_consumes_no_address(wg, monkeypatch):
    """
    Bug #22's rule still holds after the item 3.4 rewrite: no ``.conf`` may be produced
    when key generation fails, because such a file imports cleanly and never connects.
    The address must not be burned either, or a machine without the WireGuard tools would
    walk through the whole subnet one failed attempt at a time.
    """
    wg.setup_coordinator_server()
    monkeypatch.setattr(WireGuardOrchestrator, "generate_keypair", lambda self: (None, None))

    with pytest.raises(WireGuardUnavailableError):
        issue(wg, "doomed.conf", "doomed")

    assert not os.path.exists(os.path.join(wg.config_dir, "doomed.conf"))
    assert engine_config.get_config(force_reload=True)[
        wireguard.WG_NEXT_PEER_OCTET_KEY] == str(wireguard.WG_FIRST_PEER_OCTET)


def test_a_missing_coordinator_public_key_is_rejected(wg):
    """An invite pointing at a peer with no public key can never complete a handshake."""
    with pytest.raises(WireGuardUnavailableError):
        wg.generate_worker_invite(coordinator_external_ip="203.0.113.9",
                                  coordinator_pubkey="")


def test_the_invite_filename_cannot_escape_the_config_directory(wg):
    """
    ``filename`` became a parameter in this change, so it is now attacker-adjacent input
    if an invite name is ever taken from config. Only the basename is honoured.
    """
    wg.setup_coordinator_server()

    path = issue(wg, os.path.join("..", "..", "escaped.conf"), "sneaky")

    assert os.path.dirname(os.path.abspath(path)) == os.path.abspath(wg.config_dir)
    assert os.path.basename(path) == "escaped.conf"


# ======================================================================================
# Peers must reach the running interface, not only the file
# ======================================================================================
#
# WireGuard reads its configuration once, at bring-up. A [Peer] appended to the .conf of a
# live interface is invisible to the kernel, which drops that node's handshakes as coming
# from an unknown key. Enrollment answers 200, the worker writes a correct tunnel config,
# both ends look right, and nothing connects - and nothing in this product ever restarts a
# Coordinator's tunnel, so it never would.

@pytest.fixture
def live_wg(wg, monkeypatch):
    """A ``wg`` whose coordinator interface is up, recording every command issued."""
    issued = []
    real = WireGuardOrchestrator._run_wg_cmd

    def recording(self, *args, stdin_text=None):
        issued.append(args)
        if args[:2] == ("show", "interfaces"):
            return wireguard.WG_INTERFACE_NAME
        if args[:1] == ("set",):
            return ""  # wg set prints nothing on success
        return real(self, *args, stdin_text=stdin_text)

    monkeypatch.setattr(WireGuardOrchestrator, "_run_wg_cmd", recording)
    wg.issued_commands = issued
    return wg


def test_an_enrolled_peer_is_loaded_into_the_running_interface(live_wg):
    live_wg.setup_coordinator_server()
    peer_key = "PEER" + "0" * 40 + "="

    octet = live_wg.enroll_peer(peer_key, "worker")

    assert ("set", wireguard.WG_INTERFACE_NAME, "peer", peer_key,
            "allowed-ips", f"10.8.0.{octet}/32") in live_wg.issued_commands


def test_re_enrolling_reapplies_the_peer(live_wg):
    """
    The repair path. A peer written to the file by an earlier version never reached the
    kernel, and re-enrolling is exactly when a node that cannot connect comes back to ask.
    """
    live_wg.setup_coordinator_server()
    peer_key = "PEER" + "0" * 40 + "="
    live_wg.enroll_peer(peer_key, "worker")
    live_wg.issued_commands.clear()

    live_wg.enroll_peer(peer_key, "worker")

    assert any(args[:1] == ("set",) for args in live_wg.issued_commands)


def test_nothing_is_applied_when_the_interface_is_not_up(wg):
    """
    Then the file is the whole story and will be read at the next bring-up. The base wg
    fixture reports no live interfaces, so no 'set' may be attempted.
    """
    wg.setup_coordinator_server()
    issued = []
    original = WireGuardOrchestrator._run_wg_cmd
    WireGuardOrchestrator._run_wg_cmd = lambda self, *a, **k: (
        issued.append(a) or original(self, *a, **k))
    try:
        wg.enroll_peer("PEER" + "0" * 40 + "=", "worker")
    finally:
        WireGuardOrchestrator._run_wg_cmd = original

    assert not any(args[:1] == ("set",) for args in issued)


def test_the_peer_is_still_written_to_the_file(live_wg):
    """Applying live does not replace persisting: the interface restarts eventually."""
    live_wg.setup_coordinator_server()
    peer_key = "PEER" + "0" * 40 + "="

    live_wg.enroll_peer(peer_key, "worker")

    with open(live_wg.coordinator_conf_path, encoding="utf-8") as handle:
        assert peer_key in handle.read()


# ======================================================================================
# Peers the file knows about and the kernel does not
# ======================================================================================
#
# Found by reading a real Coordinator that had been broken for weeks. Its interface and its
# file each listed two peers at 10.8.0.2 and 10.8.0.3 - four different keys. The identity
# check above would have caught the interface key, and said nothing at all about the peers,
# which is the half that decides whether a given node can connect.

def _agreeing_identity(wg, monkeypatch):
    """Pin the interface key to the configured one, isolating the peer comparison."""
    configured = wg.setup_coordinator_server()
    monkeypatch.setattr(WireGuardOrchestrator, "live_interface_public_key",
                        lambda self: configured)


def test_a_peer_the_running_interface_has_never_heard_of_is_reported(wg, monkeypatch):
    _agreeing_identity(wg, monkeypatch)
    orphan = "ORPHAN" + "0" * 38 + "="
    monkeypatch.setattr(WireGuardOrchestrator, "configured_peer_keys",
                        lambda self: {orphan})
    monkeypatch.setattr(WireGuardOrchestrator, "live_peer_keys", lambda self: set())

    message = wg.warn_if_interface_diverged()

    assert message is not None
    assert orphan in message
    assert "handshake" in message.lower()


def test_matching_peer_sets_are_silent(wg, monkeypatch):
    _agreeing_identity(wg, monkeypatch)
    peer = "PEER" + "0" * 40 + "="
    monkeypatch.setattr(WireGuardOrchestrator, "configured_peer_keys", lambda self: {peer})
    monkeypatch.setattr(WireGuardOrchestrator, "live_peer_keys", lambda self: {peer})

    assert wg.warn_if_interface_diverged() is None


def test_a_kernel_peer_missing_from_the_file_is_not_reported(wg, monkeypatch):
    """
    One direction only. A peer the kernel holds and the file does not will simply vanish
    at the next restart; it blocks nobody, and reporting it would bury the direction that
    does. Only configured-but-not-running keeps a node off the network.
    """
    _agreeing_identity(wg, monkeypatch)
    monkeypatch.setattr(WireGuardOrchestrator, "configured_peer_keys", lambda self: set())
    monkeypatch.setattr(WireGuardOrchestrator, "live_peer_keys",
                        lambda self: {"STALE" + "0" * 39 + "="})

    assert wg.warn_if_interface_diverged() is None


def test_peers_that_cannot_be_listed_are_not_a_divergence(wg, monkeypatch):
    """None means unknown. Fails open, like every other arm of this check."""
    _agreeing_identity(wg, monkeypatch)
    monkeypatch.setattr(WireGuardOrchestrator, "configured_peer_keys",
                        lambda self: {"PEER" + "0" * 40 + "="})
    monkeypatch.setattr(WireGuardOrchestrator, "live_peer_keys", lambda self: None)

    assert wg.warn_if_interface_diverged() is None


def test_configured_peer_keys_reads_the_real_file(wg):
    """Against the shape enroll_peer actually writes, not a hand-built string."""
    wg.setup_coordinator_server()
    first, second = "AAAA" + "0" * 40 + "=", "BBBB" + "0" * 40 + "="
    wg.enroll_peer(first, "worker")
    wg.enroll_peer(second, "redundancy node")

    assert wg.configured_peer_keys() == {first, second}


def test_the_interface_block_is_never_read_as_a_peer(wg):
    """
    A hand-edited file can carry a PublicKey in [Interface]. Reading it here would report
    the hub as a peer of itself and then, since the kernel never lists it, as a permanent
    divergence that no restart can clear.
    """
    wg.setup_coordinator_server()
    with open(wg.coordinator_conf_path, encoding="utf-8") as handle:
        body = handle.read()
    with open(wg.coordinator_conf_path, "w", encoding="utf-8") as handle:
        handle.write(body.replace("[Interface]", "[Interface]\nPublicKey = SELF" + "0" * 40 + "="))

    assert "SELF" not in "".join(wg.configured_peer_keys())


# ======================================================================================
# The command the operator is told to run
# ======================================================================================

def test_the_restart_command_names_the_config_file_by_path(wg):
    """
    Both diagnostics used to print 'wg-quick down coordinator_wg0'. wg-quick's bare-name
    form resolves against /etc/wireguard/, and DLNE keeps this file in wg_configs/ - so the
    operator ran it, got 'does not exist', and was looking at the interface it named.
    """
    command = wg.restart_interface_command

    assert wg.coordinator_conf_path in command
    assert f"down {wireguard.WG_INTERFACE_NAME}" not in command


def test_the_divergence_warning_prints_a_runnable_command(wg, monkeypatch):
    wg.setup_coordinator_server()
    monkeypatch.setattr(WireGuardOrchestrator, "live_interface_public_key",
                        lambda self: "SOMEONEELSE" + "0" * 32 + "=")

    assert wg.coordinator_conf_path in wg.warn_if_interface_diverged()


# ======================================================================================
# A node that is not allowed to ask
# ======================================================================================
#
# Reading a WireGuard interface and modifying one both need CAP_NET_ADMIN. A Coordinator
# started as an ordinary user can list interfaces and nothing more, so it logged
# "Unable to access interface: Operation not permitted" on every start and again on every
# enrollment - a raw command failure repeated forever, describing not a fault but a
# deliberate decision about how the node was launched.

import subprocess  # noqa: E402  - grouped with the tests that need it


@pytest.fixture
def refused(monkeypatch):
    """A ``wg`` that is installed and refuses every interface query."""
    monkeypatch.setattr(wireguard, "_privilege_notice_given", False)
    monkeypatch.setattr(WireGuardOrchestrator, "_find_wg_executable", lambda self: "wg")

    def refuse(cmd, **kwargs):
        raise subprocess.CalledProcessError(
            1, cmd, output="",
            stderr="Unable to access interface: Operation not permitted")

    monkeypatch.setattr(wireguard.subprocess, "run", refuse)


@pytest.fixture
def logs(monkeypatch):
    captured = {"warning": [], "error": []}
    monkeypatch.setattr(wireguard, "log_warning", captured["warning"].append)
    monkeypatch.setattr(wireguard, "log_error", captured["error"].append)
    return captured


def test_a_refused_query_is_explained_rather_than_dumped(refused, logs, dlne_root, monkeypatch):
    monkeypatch.setattr(wireguard, "get_base_dir", lambda: str(dlne_root))

    assert WireGuardOrchestrator()._run_wg_cmd("show", "coordinator_wg0", "public-key") is None
    assert logs["error"] == [], "a launch decision is not a command fault"
    assert len(logs["warning"]) == 1
    assert "not permitted" in logs["warning"][0]


def test_the_notice_says_what_it_costs_and_how_to_fix_it(refused, logs, dlne_root, monkeypatch):
    """
    The point of saying anything. Both consequences are invisible from outside the process:
    enrolled peers never reach the kernel, and the check that would have caught it is the
    thing that cannot run.
    """
    monkeypatch.setattr(wireguard, "get_base_dir", lambda: str(dlne_root))
    WireGuardOrchestrator()._run_wg_cmd("show", "coordinator_wg0", "public-key")

    notice = logs["warning"][0].lower()
    assert "handshake" in notice
    assert "sudo" in notice or "administrator" in notice


def test_the_notice_is_given_once_per_run(refused, logs, dlne_root, monkeypatch):
    """
    It is asked on every start and on every enrollment, and the answer never changes
    between them. Repeating it buries the lines that do change - which is how the original
    failure survived weeks of logs nobody could read.
    """
    monkeypatch.setattr(wireguard, "get_base_dir", lambda: str(dlne_root))
    orchestrator = WireGuardOrchestrator()

    for _ in range(5):
        orchestrator._run_wg_cmd("show", "coordinator_wg0", "public-key")
        orchestrator._run_wg_cmd("set", "coordinator_wg0", "peer", "X", "allowed-ips", "10.8.0.9/32")

    assert len(logs["warning"]) == 1


def test_an_ordinary_failure_is_still_reported_in_full(logs, dlne_root, monkeypatch):
    """The quiet path is for permission denial specifically, not for failures generally."""
    monkeypatch.setattr(wireguard, "get_base_dir", lambda: str(dlne_root))
    monkeypatch.setattr(WireGuardOrchestrator, "_find_wg_executable", lambda self: "wg")

    def fail(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd, output="", stderr="Key is not the correct length")

    monkeypatch.setattr(wireguard.subprocess, "run", fail)

    assert WireGuardOrchestrator()._run_wg_cmd("pubkey", stdin_text="x") is None
    assert len(logs["error"]) == 1
    assert "not the correct length" in logs["error"][0]
