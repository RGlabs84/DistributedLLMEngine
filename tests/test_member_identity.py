"""
A coding member's identity key and the signature on its heartbeat block
(engine/member_identity.py, docs/coding-members.md sections 1 and 2).

Trust is decided by a key the member owns, never by a worker id, so these tests are about what a
hostile member could try: replaying an old block, changing a field after signing, claiming another
member's key, and sending junk to the coordinator's check (which must answer None and never raise).
"""

import base64
import hashlib
import json
import os
import subprocess
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from engine import member_identity as mi

BLOCK = {"model": "Qwén3.6-A3B.gguf", "tier": "qwen36-a3b", "ready": True, "slots": 2, "busy": 1,
         "decode_tps": 32.5, "prefill_tps": 121.2, "pubkey": base64.b64encode(b"k" * 32).decode(), "no_trace": True}
NOW = 1_793_000_000.0


@pytest.fixture
def identity(tmp_path):
    return mi.load_or_create_identity(str(tmp_path / "config" / "coding_identity.key"))


def signed(identity, now=NOW, **changes):
    return mi.sign_block({**BLOCK, **changes}, identity[0], now=now)


# ======================================================================================
# The trust id
# ======================================================================================

def test_the_trust_id_is_the_first_32_hex_characters_of_sha256_of_the_raw_public_key():
    raw = bytes(range(32))
    expected = hashlib.sha256(raw).hexdigest()[:32]
    assert mi.trust_id_of_pub(raw) == expected == mi.trust_id(base64.b64encode(raw).decode())
    assert len(expected) == 32


@pytest.mark.parametrize("bad", ["", "not base64!!", base64.b64encode(b"short").decode(),
                                 base64.b64encode(b"x" * 33).decode(), None, 5])
def test_a_trust_id_needs_exactly_32_bytes_of_base64(bad):
    with pytest.raises(ValueError):
        mi.trust_id(bad)


# ======================================================================================
# The key file
# ======================================================================================

def test_the_key_is_made_on_first_use_with_owner_only_permissions(tmp_path):
    path = tmp_path / "config" / "coding_identity.key"
    priv, pub = mi.load_or_create_identity(str(path))
    assert path.is_file() and len(priv) == 32
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
    derived = Ed25519PrivateKey.from_private_bytes(priv).public_key().public_bytes_raw()
    assert base64.b64decode(pub) == derived, "the returned public key belongs to the private key"


def test_the_same_key_comes_back_on_every_start(tmp_path):
    path = str(tmp_path / "id.key")
    first = mi.load_or_create_identity(path)
    assert mi.load_or_create_identity(path) == first == mi.load_or_create_identity(path)


def test_two_installs_have_different_identities(tmp_path):
    assert mi.load_or_create_identity(str(tmp_path / "a.key"))[1] != mi.load_or_create_identity(str(tmp_path / "b.key"))[1]


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_a_key_file_that_is_too_open_is_tightened(tmp_path):
    path = tmp_path / "id.key"
    mi.load_or_create_identity(str(path))
    path.chmod(0o644)
    mi.load_or_create_identity(str(path))
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("content", ["", "garbage\n", base64.b64encode(b"x" * 31).decode(),
                                     base64.b64encode(b"x" * 33).decode(), "\xe9\xe9"])
def test_a_damaged_key_file_is_an_error_and_is_left_alone(tmp_path, content):
    path = tmp_path / "id.key"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(mi.IdentityError) as caught:
        mi.load_or_create_identity(str(path))
    assert path.read_text(encoding="utf-8") == content, "never replaced: a new key would change the trust id"
    assert content.strip() not in str(caught.value) or not content.strip(), "the file's content is not quoted"


def test_a_key_path_that_cannot_be_read_or_made_is_an_error(tmp_path):
    (tmp_path / "dir.key").mkdir()
    with pytest.raises(mi.IdentityError):
        mi.load_or_create_identity(str(tmp_path / "dir.key"))
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(mi.IdentityError):
        mi.load_or_create_identity(str(blocker / "config" / "id.key"))


# ======================================================================================
# "Owner only" on Windows, where chmod does nothing
# ======================================================================================
#
# These run on any machine: the module asks `_is_windows()` and calls the Windows tools through its
# own `subprocess` name, both of which a test replaces. What they cannot show is that icacls.exe
# accepts the arguments; that needs a real Windows PC (the cross-platform report lists the steps).

SID = "S-1-5-21-1004336348-1177238915-682003330-1001"
CREATE_NO_WINDOW = 0x08000000


class FakeWindowsTools:
    """whoami.exe and icacls.exe as the module calls them: records every call, and how big the key
    file was at that moment (the access list must be set before the key is written)."""

    def __init__(self, whoami_out=None, whoami_rc=0, icacls_rc=0, missing=()):
        self.whoami_out = whoami_out if whoami_out is not None else f'"DESKTOP-9\\Jos\u00e9 \u00d1and\u00fa","{SID}"\r\n'
        self.whoami_rc, self.icacls_rc, self.missing = whoami_rc, icacls_rc, set(missing)
        self.calls = []

    def run(self, argv, **kwargs):
        tool = os.path.basename(argv[0]).lower()
        size = os.path.getsize(argv[1]) if tool == "icacls.exe" and os.path.exists(argv[1]) else None
        self.calls.append({"argv": list(argv), "tool": tool, "kwargs": kwargs, "size": size})
        if tool in self.missing:
            raise FileNotFoundError(tool)
        if tool == "whoami.exe":
            return SimpleNamespace(returncode=self.whoami_rc, stdout=self.whoami_out, stderr="")
        return SimpleNamespace(returncode=self.icacls_rc, stdout=b"", stderr=b"")

    def tools(self):
        return [call["tool"] for call in self.calls]


@pytest.fixture
def windows(monkeypatch):
    """Pretend to be Windows with the given fake tools; returns a factory for them."""
    warnings = []
    import engine.logger as logger
    monkeypatch.setattr(logger, "log_warning", warnings.append)
    monkeypatch.setenv("SystemRoot", "C:\\Windows")

    def install(**kwargs):
        fake = FakeWindowsTools(**kwargs)
        monkeypatch.setattr(mi, "_is_windows", lambda: True)
        monkeypatch.setattr(mi, "subprocess", SimpleNamespace(run=fake.run, SubprocessError=subprocess.SubprocessError))
        fake.warnings = warnings
        return fake

    return install


def test_on_windows_the_owner_gets_full_control_and_inherited_entries_go_before_the_key_is_written(tmp_path, windows):
    fake = windows()
    path = tmp_path / "config" / "coding_identity.key"
    priv, pub = mi.load_or_create_identity(str(path))

    assert fake.tools() == ["whoami.exe", "icacls.exe", "icacls.exe"]
    whoami, grant, strip = fake.calls
    assert whoami["argv"][0].startswith("C:\\Windows") and whoami["argv"][0].endswith("whoami.exe"), "System32, by absolute path"
    assert grant["argv"][0].startswith("C:\\Windows") and grant["argv"][0].endswith("icacls.exe")
    # Granted first, inheritance removed second: the owner is never left without an entry.
    assert grant["argv"][1:] == [str(path), "/grant:r", f"*{SID}:(F)"]
    assert strip["argv"][1:] == [str(path), "/inheritance:r"]
    assert grant["size"] == 0 and strip["size"] == 0, "locked down while the file is still empty"
    assert all(call["kwargs"]["creationflags"] == CREATE_NO_WINDOW for call in fake.calls), "no console window"
    assert path.stat().st_size > 0 and len(priv) == 32, "and the key is written afterwards"
    assert not fake.warnings


def test_on_windows_an_existing_key_file_is_locked_down_again_at_every_start(tmp_path, windows):
    path = str(tmp_path / "id.key")
    first = mi.load_or_create_identity(path)          # made on a non-Windows host: no tools involved
    fake = windows()
    assert mi.load_or_create_identity(path) == first
    assert fake.tools() == ["whoami.exe", "icacls.exe", "icacls.exe"]
    assert fake.calls[1]["size"] > 0


@pytest.mark.parametrize("fake_options", [
    {"icacls_rc": 5},                       # access denied (a folder the owner does not control)
    {"missing": {"icacls.exe"}},            # no such tool
    {"missing": {"whoami.exe"}},
    {"whoami_rc": 1},
    {"whoami_out": "INFO: no account information\r\n"},
])
def test_when_the_access_list_cannot_be_set_the_key_is_still_made_and_the_owner_is_told(tmp_path, windows, fake_options):
    fake = windows(**fake_options)
    path = tmp_path / "id.key"
    priv, pub = mi.load_or_create_identity(str(path))
    assert len(priv) == 32 and path.read_text(encoding="ascii").strip(), "the member still gets its identity"
    assert len(fake.warnings) == 1 and "identity key" in fake.warnings[0]
    assert str(tmp_path) not in fake.warnings[0], "no paths in the warning"
    assert mi.load_or_create_identity(str(path)) == (priv, pub)


def test_a_failing_access_list_stops_after_the_first_step(tmp_path, windows):
    fake = windows(icacls_rc=5)
    mi.load_or_create_identity(str(tmp_path / "id.key"))
    assert fake.tools() == ["whoami.exe", "icacls.exe"], "the inheritance is not stripped when the grant failed"


def test_the_account_is_found_by_its_sid_whatever_its_name_looks_like(windows):
    windows(whoami_out='"\u30c7\u30b9\u30af\u30c8\u30c3\u30d7\\\u592a\u90ce","S-1-5-21-1-2-3-1000"\r\n')
    assert mi._own_sid() == "S-1-5-21-1-2-3-1000"
    windows(whoami_out="garbled \ufffd\ufffd output")
    assert mi._own_sid() is None


def test_off_windows_no_tool_is_ever_run_and_the_mode_is_0600(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(mi, "subprocess", SimpleNamespace(run=lambda *a, **k: ran.append(a), SubprocessError=Exception))
    monkeypatch.setattr(mi, "_is_windows", lambda: False)
    path = tmp_path / "id.key"
    mi.load_or_create_identity(str(path))
    mi.load_or_create_identity(str(path))
    assert ran == []
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600


# ======================================================================================
# Signing and checking
# ======================================================================================

def test_a_signed_block_verifies_and_names_its_trust_id(identity):
    block = signed(identity)
    assert set(block) == set(BLOCK) | {"identity", "signed_at", "sig"}
    assert block["identity"] == identity[1] and block["signed_at"] == int(NOW)
    assert mi.verify_block(block, now=NOW) == mi.trust_id(identity[1])


def test_signing_leaves_the_callers_block_alone(identity):
    original = dict(BLOCK)
    mi.sign_block(original, identity[0])
    assert original == BLOCK


def test_the_signature_is_the_contracts_recipe(identity):
    """Canonical JSON of the block without sig: sorted keys, no spaces, UTF-8 as is (not \\u escapes)."""
    block = signed(identity)
    unsigned = {k: v for k, v in block.items() if k != "sig"}
    message = json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    assert "Qwén" in message.decode() and b"\\u" not in message
    Ed25519PublicKey.from_public_bytes(base64.b64decode(block["identity"])).verify(base64.b64decode(block["sig"]), message)


def test_a_block_that_went_through_json_still_verifies(identity):
    """The coordinator receives parsed JSON, not the member's dict."""
    block = json.loads(json.dumps(signed(identity)))
    assert mi.verify_block(block, now=NOW) == mi.trust_id(identity[1])


def test_values_the_caller_pre_filled_for_the_signed_fields_are_replaced(identity):
    block = mi.sign_block({**BLOCK, "identity": "x", "signed_at": 1, "sig": "y"}, identity[0], now=NOW)
    assert block["identity"] == identity[1] and block["signed_at"] == int(NOW)
    assert mi.verify_block(block, now=NOW)


@pytest.mark.parametrize("field, value", [
    ("model", "Other.gguf"), ("tier", "x"), ("ready", False), ("slots", 64), ("busy", 0), ("decode_tps", 99.9),
    ("prefill_tps", 1.0), ("pubkey", base64.b64encode(b"j" * 32).decode()), ("no_trace", False),
    ("signed_at", int(NOW) + 1),
])
def test_changing_any_field_after_signing_breaks_the_signature(identity, field, value):
    assert mi.verify_block({**signed(identity), field: value}, now=NOW) is None


def test_an_extra_or_a_removed_field_breaks_the_signature(identity):
    block = signed(identity)
    assert mi.verify_block({**block, "trust_id": "0" * 32}, now=NOW) is None
    assert mi.verify_block({k: v for k, v in block.items() if k != "busy"}, now=NOW) is None


def test_someone_elses_key_cannot_vouch_for_a_block(identity, tmp_path):
    other = mi.load_or_create_identity(str(tmp_path / "other.key"))
    block = signed(identity)
    assert mi.verify_block({**block, "identity": other[1]}, now=NOW) is None, "the trust id would be theirs"
    forged = mi.sign_block(BLOCK, other[0], now=NOW)
    assert mi.verify_block({**forged, "identity": identity[1]}, now=NOW) is None


def test_a_signature_from_another_block_does_not_carry_over(identity):
    a, b = signed(identity), signed(identity, busy=0)
    assert mi.verify_block({**b, "sig": a["sig"]}, now=NOW) is None


@pytest.mark.parametrize("offset, ok", [(0, True), (299, True), (-299, True), (300, True), (301, False), (-301, False),
                                        (86400, False), (-86400, False)])
def test_a_block_must_be_fresh_both_ways(identity, offset, ok):
    block = signed(identity)
    assert (mi.verify_block(block, now=NOW + offset) is not None) is ok


def test_the_allowed_skew_is_a_parameter(identity):
    block = signed(identity)
    assert mi.verify_block(block, max_skew=10, now=NOW + 11) is None
    assert mi.verify_block(block, max_skew=10, now=NOW + 10)
    assert mi.verify_block(block, now=NOW + 11), "300 seconds by default"


def test_verification_uses_the_clock_when_no_time_is_given(identity):
    assert mi.verify_block(mi.sign_block(BLOCK, identity[0])) is not None
    assert mi.verify_block(mi.sign_block(BLOCK, identity[0], now=1.0)) is None


@pytest.mark.parametrize("junk", [
    None, 5, "block", [], [BLOCK], {}, {"identity": 1}, {**BLOCK},
])
def test_anything_that_is_not_a_signed_block_gives_none(junk):
    assert mi.verify_block(junk, now=NOW) is None


@pytest.mark.parametrize("field, value", [
    ("signed_at", True), ("signed_at", "1793000000"), ("signed_at", None), ("signed_at", float("nan")),
    ("signed_at", float("inf")), ("signed_at", [1]),
    ("identity", None), ("identity", 7), ("identity", ""), ("identity", "!!!"), ("identity", base64.b64encode(b"x" * 31).decode()),
    ("sig", None), ("sig", 7), ("sig", ""), ("sig", "!!!"), ("sig", base64.b64encode(b"x" * 63).decode()),
    ("sig", base64.b64encode(b"x" * 65).decode()), ("sig", base64.b64encode(b"x" * 64).decode()),
])
def test_junk_in_the_signed_fields_gives_none_and_never_raises(identity, field, value):
    assert mi.verify_block({**signed(identity), field: value}, now=NOW) is None


def test_a_block_that_cannot_be_serialised_gives_none(identity):
    block = signed(identity)
    block["extra"] = object()
    assert mi.verify_block(block, now=NOW) is None
