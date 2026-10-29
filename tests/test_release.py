"""
Tests for engine.release: reproducible archives, the signed manifest, and the checks a node
makes before trusting what it downloaded.
"""

import datetime
import hashlib
import json
import os
import re
import time
import zipfile

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from engine import release

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILT_AT = "2026-10-25T12:00:00Z"


def _pem(key: Ed25519PrivateKey) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


@pytest.fixture
def keypair():
    key = Ed25519PrivateKey.generate()
    return _pem(key), release.public_key_hex(_pem(key))


@pytest.fixture
def tree(tmp_path):
    """A miniature repo: what ships, and plenty that must not."""
    root = tmp_path / "repo"
    (root / "engine" / "__pycache__").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "engine" / "version.py").write_text('# comment\nVERSION = "2.0.0"\n')
    (root / "engine" / "alpha.py").write_text("ALPHA = 1\n")
    (root / "engine" / "beta.py").write_text("BETA = 2\n")
    (root / "engine" / "__pycache__" / "alpha.cpython-314.pyc").write_bytes(b"\x00bytecode")
    (root / "engine" / "notes.txt").write_text("not python")
    (root / "engine" / "sub").mkdir()
    (root / "engine" / "sub" / "nested.py").write_text("NESTED = 1\n")
    (root / "dlne_server.py").write_text("print('server')\n")
    (root / "requirements.txt").write_text("fastapi\n")
    (root / "tests" / "test_x.py").write_text("def test_x(): pass\n")
    (root / ".env").write_text("SECRET=1\n")
    (root / "config.txt").write_text("NETWORK_KEY=abc\n")
    return str(root)


def _build(tree, tmp_path, name="a.zip"):
    out = str(tmp_path / name)
    info = release.build_source_archive(tree, out)
    return out, info


# --- versions -----------------------------------------------------------------------------

def test_read_and_write_version_touch_only_the_version_line(tree):
    assert release.read_version(tree) == "2.0.0"
    release.write_version(tree, "2.1.0")
    assert release.read_version(tree) == "2.1.0"
    text = open(os.path.join(tree, "engine", "version.py")).read()
    assert text == '# comment\nVERSION = "2.1.0"\n'


def test_write_version_refuses_nonsense(tree):
    for bad in ("2.1", "v2.1.0", "2.1.0-beta", "", "two"):
        with pytest.raises(release.ReleaseError):
            release.write_version(tree, bad)
    assert release.read_version(tree) == "2.0.0"


def test_a_version_file_without_a_version_line_is_an_error(tree):
    open(os.path.join(tree, "engine", "version.py"), "w").write("nothing here\n")
    with pytest.raises(release.ReleaseError):
        release.read_version(tree)
    with pytest.raises(release.ReleaseError):
        release.write_version(tree, "2.0.0")


@pytest.mark.parametrize("current,part,expected", [
    ("1.1.12", "major", "2.0.0"), ("1.1.12", "minor", "1.2.0"), ("1.1.12", "patch", "1.1.13"),
    ("1.1.12", "2.0.0", "2.0.0"), ("0.9.9", "patch", "0.9.10"), ("1.9.9", "minor", "1.10.0"),
])
def test_bump_version(current, part, expected):
    assert release.bump_version(current, part) == expected


def test_bump_version_rejects_unknown_parts():
    for bad in ("huge", "", "1.2", "MAJOR"):
        with pytest.raises(release.ReleaseError):
            release.bump_version("1.0.0", bad)


def test_is_older_is_numeric_and_never_guesses():
    assert release.is_older("1.1.0", "1.1.12")
    assert release.is_older("1.9.0", "1.10.0")
    assert not release.is_older("1.10.0", "1.9.0")
    assert not release.is_older("2.0.0", "2.0.0")
    assert not release.is_older("banana", "1.0.0") and not release.is_older("1.0.0", "x")


# --- the archive --------------------------------------------------------------------------

def test_only_the_allowlist_ships(tree):
    assert release.shipped_files(tree) == [
        "dlne_server.py", "engine/alpha.py", "engine/beta.py", "engine/version.py", "requirements.txt",
    ]


def test_secrets_tests_bytecode_and_subpackages_never_ship(tree, tmp_path):
    out, info = _build(tree, tmp_path)
    with zipfile.ZipFile(out) as archive:
        names = archive.namelist()
    for forbidden in (".env", "config.txt", "tests/test_x.py", "engine/notes.txt", "engine/sub/nested.py"):
        assert forbidden not in names
    assert not any("__pycache__" in n or n.endswith(".pyc") for n in names)
    assert names == info["files"]


def _add_static(tree):
    """engine/static as the repository has it (the icons), plus what must never ship from it."""
    static = os.path.join(tree, "engine", "static")
    os.makedirs(os.path.join(static, "deeper"))
    for name, data in (("icon-192.png", b"\x89PNG-a"), ("favicon-32.png", b"\x89PNG-b"),
                       ("icons.json", b"{}"), ("notes.txt", b"scratch"), ("logo.svg", b"<svg/>"),
                       (os.path.join("deeper", "nested.png"), b"\x89PNG-c")):
        with open(os.path.join(static, name), "wb") as handle:
            handle.write(data)


def test_the_icons_in_engine_static_ship_and_nothing_else_in_it_does(tree, tmp_path):
    """The coordinator will not start without its icons, so the archive must carry them (and only them)."""
    _add_static(tree)
    assert release.shipped_files(tree) == [
        "dlne_server.py", "engine/alpha.py", "engine/beta.py", "engine/static/favicon-32.png",
        "engine/static/icon-192.png", "engine/version.py", "requirements.txt",
    ]
    out, info = _build(tree, tmp_path)
    with zipfile.ZipFile(out) as archive:
        assert archive.namelist() == info["files"]
        assert archive.read("engine/static/icon-192.png") == b"\x89PNG-a"
        assert archive.getinfo("engine/static/icon-192.png").date_time == archive.getinfo("engine/version.py").date_time


def test_a_tree_without_engine_static_still_builds(tree):
    assert not any(f.startswith("engine/static/") for f in release.shipped_files(tree))


def test_building_twice_gives_identical_bytes(tree, tmp_path):
    a, info_a = _build(tree, tmp_path, "a.zip")
    time.sleep(1.1)
    for root, _dirs, files in os.walk(tree):                 # touch every file: only mtimes change
        for name in files:
            os.utime(os.path.join(root, name), None)
    b, info_b = _build(tree, tmp_path, "b.zip")
    assert open(a, "rb").read() == open(b, "rb").read()
    assert info_a["sha256"] == info_b["sha256"] == hashlib.sha256(open(a, "rb").read()).hexdigest()


def test_the_archive_depends_on_content(tree, tmp_path):
    a, info_a = _build(tree, tmp_path, "a.zip")
    open(os.path.join(tree, "engine", "alpha.py"), "w").write("ALPHA = 99\n")
    _b, info_b = _build(tree, tmp_path, "b.zip")
    assert info_a["sha256"] != info_b["sha256"]


def test_entries_share_one_past_date_and_fixed_permissions(tree, tmp_path):
    out, _ = _build(tree, tmp_path)
    with zipfile.ZipFile(out) as archive:
        dates = {info.date_time for info in archive.infolist()}
        assert len(dates) == 1
        assert datetime.datetime(1980, 1, 1) < datetime.datetime(*dates.pop()) < datetime.datetime.now()
        for info in archive.infolist():
            assert (info.external_attr >> 16) == 0o100644
            assert info.compress_type == zipfile.ZIP_DEFLATED


def test_a_release_whose_version_file_keeps_its_size_still_gets_a_new_date(tree, tmp_path):
    """unzip keeps the archive's dates and Python trusts a .pyc while the source's mtime and size match:
    2.4.9's version.py and 2.5.0's were both 616 bytes, so one fixed date kept workers on 2.4.9's bytecode."""
    a, _ = _build(tree, tmp_path, "a.zip")
    release.write_version(tree, "2.0.1")
    b, _ = _build(tree, tmp_path, "b.zip")
    with zipfile.ZipFile(a) as old, zipfile.ZipFile(b) as new:
        assert old.getinfo("engine/version.py").file_size == new.getinfo("engine/version.py").file_size
        assert old.getinfo("engine/version.py").date_time != new.getinfo("engine/version.py").date_time


def test_the_archive_round_trips_file_contents(tree, tmp_path):
    out, _ = _build(tree, tmp_path)
    with zipfile.ZipFile(out) as archive:
        assert archive.read("engine/alpha.py") == b"ALPHA = 1\n"
        assert archive.testzip() is None


def test_a_missing_required_file_is_an_error_not_a_short_release(tree, tmp_path):
    os.remove(os.path.join(tree, "requirements.txt"))
    with pytest.raises(release.ReleaseError, match="requirements.txt"):
        release.build_source_archive(tree, str(tmp_path / "x.zip"))


def test_archive_version_reads_the_code_inside(tree, tmp_path):
    out, _ = _build(tree, tmp_path)
    assert release.archive_version(out) == "2.0.0"


def test_archive_version_of_a_non_archive_is_an_error(tmp_path):
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip")
    with pytest.raises(release.ReleaseError):
        release.archive_version(str(bad))


# --- the manifest -------------------------------------------------------------------------

def _signed(tree, tmp_path, keypair, notes="first signed release"):
    out, _ = _build(tree, tmp_path)
    manifest = release.make_manifest("2.0.0", out, "abc1234", BUILT_AT, notes)
    return out, release.sign_manifest(manifest, keypair[0])


def test_a_signed_manifest_verifies(tree, tmp_path, keypair):
    _out, signed = _signed(tree, tmp_path, keypair)
    assert release.verify_manifest(signed, keypair[1]) == (True, "ok")


def test_the_manifest_describes_the_archive(tree, tmp_path, keypair):
    out, signed = _signed(tree, tmp_path, keypair)
    assert signed["schema"] == 1 and signed["file"] == "dlne-source.zip"
    assert signed["sha256"] == release.sha256_file(out) and signed["size"] == os.path.getsize(out)
    assert signed["version"] == "2.0.0" and signed["git_commit"] == "abc1234"


@pytest.mark.parametrize("field,value", [
    ("version", "2.0.1"), ("sha256", "0" * 64), ("size", 1), ("file", "evil.zip"),
    ("notes", "tampered"), ("git_commit", "deadbee"), ("built_at", "2030-01-01T00:00:00Z"), ("schema", 2),
])
def test_changing_any_field_breaks_the_signature(tree, tmp_path, keypair, field, value):
    _out, signed = _signed(tree, tmp_path, keypair)
    signed[field] = value
    ok, reason = release.verify_manifest(signed, keypair[1])
    assert not ok and reason


def test_an_extra_field_breaks_the_signature(tree, tmp_path, keypair):
    _out, signed = _signed(tree, tmp_path, keypair)
    signed["extra"] = "smuggled"
    assert release.verify_manifest(signed, keypair[1])[0] is False


def test_a_signature_from_another_key_is_rejected(tree, tmp_path, keypair):
    _out, signed = _signed(tree, tmp_path, keypair)
    other = release.public_key_hex(_pem(Ed25519PrivateKey.generate()))
    assert release.verify_manifest(signed, other) == (False, "the release signature does not verify")


def test_an_unsigned_manifest_is_rejected(tree, tmp_path, keypair):
    _out, signed = _signed(tree, tmp_path, keypair)
    del signed["signature"]
    assert release.verify_manifest(signed, keypair[1]) == (False, "the release manifest is not signed")
    signed["signature"] = ""
    assert release.verify_manifest(signed, keypair[1])[0] is False


@pytest.mark.parametrize("junk", [None, [], "text", 7, {}, {"schema": 1}, {"signature": "aa"}])
def test_garbage_is_a_rejection_never_an_exception(junk, keypair):
    ok, reason = release.verify_manifest(junk, keypair[1])
    assert ok is False and reason


def test_a_malformed_signature_or_key_is_a_rejection(tree, tmp_path, keypair):
    _out, signed = _signed(tree, tmp_path, keypair)
    signed["signature"] = "zz-not-hex"
    assert release.verify_manifest(signed, keypair[1])[0] is False
    _out2, good = _signed(tree, tmp_path, keypair)
    assert release.verify_manifest(good, "not-a-key")[0] is False


def test_signing_does_not_depend_on_key_order(tree, tmp_path, keypair):
    _out, signed = _signed(tree, tmp_path, keypair)
    shuffled = dict(reversed(list(signed.items())))
    assert release.verify_manifest(shuffled, keypair[1]) == (True, "ok")


def test_canonical_bytes_exclude_the_signature_and_are_compact(tree, tmp_path, keypair):
    _out, signed = _signed(tree, tmp_path, keypair)
    raw = release.canonical_bytes(signed)
    assert b"signature" not in raw and b" " not in raw.replace(b"first signed release", b"")
    assert raw == release.canonical_bytes({k: v for k, v in signed.items() if k != "signature"})


def test_non_ascii_text_cannot_be_signed(tree, tmp_path):
    out, _ = _build(tree, tmp_path)
    for bad in ("naïve", "emoji \U0001f680", "tab\there", "new\nline"):
        with pytest.raises(release.ReleaseError, match="printable ASCII"):
            release.make_manifest("2.0.0", out, "abc1234", BUILT_AT, bad)


def test_a_manifest_that_is_not_printable_ascii_is_rejected_on_verify(tree, tmp_path, keypair):
    _out, signed = _signed(tree, tmp_path, keypair)
    signed["notes"] = "café"
    assert release.verify_manifest(signed, keypair[1])[0] is False


def test_a_non_ed25519_key_cannot_sign(tree, tmp_path):
    from cryptography.hazmat.primitives.asymmetric import rsa

    rsa_pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    out, _ = _build(tree, tmp_path)
    manifest = release.make_manifest("2.0.0", out, "abc1234", BUILT_AT)
    with pytest.raises(release.ReleaseError, match="Ed25519"):
        release.sign_manifest(manifest, rsa_pem)


# --- archive vs manifest ------------------------------------------------------------------

def test_the_genuine_archive_matches_its_manifest(tree, tmp_path, keypair):
    out, signed = _signed(tree, tmp_path, keypair)
    assert release.verify_archive(signed, out) == (True, "ok")


def test_a_modified_archive_is_caught(tree, tmp_path, keypair):
    out, signed = _signed(tree, tmp_path, keypair)
    with open(out, "ab") as handle:
        handle.write(b"x")
    ok, reason = release.verify_archive(signed, out)
    assert not ok and "size" in reason


def test_a_same_size_substitute_is_caught_by_the_hash(tree, tmp_path, keypair):
    out, signed = _signed(tree, tmp_path, keypair)
    data = bytearray(open(out, "rb").read())
    data[-30] ^= 0xFF
    open(out, "wb").write(bytes(data))
    ok, reason = release.verify_archive(signed, out)
    assert not ok and "hash" in reason


def test_a_manifest_claiming_a_different_version_than_the_code_is_caught(tree, tmp_path, keypair):
    out, _ = _build(tree, tmp_path)
    lying = release.make_manifest("9.9.9", out, "abc1234", BUILT_AT)
    ok, reason = release.verify_archive(lying, out)
    assert not ok and "9.9.9" in reason and "2.0.0" in reason


def test_a_missing_archive_is_a_rejection(tmp_path, keypair):
    ok, reason = release.verify_archive({"size": 1, "sha256": "0" * 64, "version": "1.0.0"}, str(tmp_path / "gone.zip"))
    assert not ok and reason


def test_load_manifest_handles_absent_and_broken_files(tmp_path):
    assert release.load_manifest(str(tmp_path / "nope.json")) is None
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert release.load_manifest(str(broken)) is None
    listy = tmp_path / "list.json"
    listy.write_text("[1, 2]")
    assert release.load_manifest(str(listy)) is None
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"version": "1.0.0"}))
    assert release.load_manifest(str(good)) == {"version": "1.0.0"}


# --- the real repository ------------------------------------------------------------------

def test_the_real_release_contains_the_engine_and_nothing_private():
    files = release.shipped_files(REPO_ROOT)
    for expected in ("dlne_server.py", "requirements.txt", "engine/coordinator.py", "engine/worker.py",
                     "engine/release.py", "engine/landing.py", "engine/version.py"):
        assert expected in files
    assert not any(f.startswith(("tests/", "vscode-extension/", "scripts/")) for f in files)
    assert not any(f in files for f in ("config/config.txt", ".env"))


def test_the_real_archive_carries_the_real_version(tmp_path):
    out = str(tmp_path / "real.zip")
    info = release.build_source_archive(REPO_ROOT, out)
    assert release.archive_version(out) == release.read_version(REPO_ROOT)
    assert info["size"] < 3 * 1024 * 1024   # about 2.2 MB: ~0.7 MB of PNGs and ~0.9 MB of place list; every worker downloads it


def test_the_real_archive_ships_every_icon_the_chat_serves_byte_for_byte(tmp_path):
    """The icons are in engine/static, which an archive of only engine/*.py would silently leave out."""
    from engine import icons

    out = str(tmp_path / "real.zip")
    info = release.build_source_archive(REPO_ROOT, out)
    shipped = {f for f in info["files"] if f.startswith("engine/static/")}
    assert shipped == {f"engine/static/{name}" for name in icons.ICONS}, "icons.json is a record, it does not ship"
    with zipfile.ZipFile(out) as archive:
        for name in icons.ICONS:
            assert archive.read(f"engine/static/{name}") == icons.icon_png(name), name


# The member pattern of the DLNE Donate 1.0.0 tray installer (desktop/dlne_donate/installer.py at 235cdd2),
# copied here on purpose and NEVER to be updated to follow later changes: those installers are already on
# volunteers' machines and refuse a release holding any other file ("NOT installed"), and workers must run
# the coordinator's exact version, so one stray file would strand them on the old release for good.
_TRAY_1_0_0_MEMBER_RE = re.compile(
    r"^(dlne_server\.py|requirements\.txt|engine/[A-Za-z0-9_]+\.py|engine/static/[A-Za-z0-9_.-]+\.png)$")


def test_the_real_archive_holds_only_what_the_tray_installers_in_the_field_accept(tmp_path):
    """The place list (engine/places_data.py) is Python for this reason; a data file would strand Donate 1.0.0."""
    out = str(tmp_path / "real.zip")
    info = release.build_source_archive(REPO_ROOT, out)
    with zipfile.ZipFile(out) as archive:
        names = archive.namelist()
    assert "engine/places_data.py" in names
    assert not [n for n in names if not _TRAY_1_0_0_MEMBER_RE.match(n)]
    assert names == info["files"]


def test_the_baked_in_public_key_is_a_valid_ed25519_key():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    assert len(release.RELEASE_PUBKEY) == 64
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(release.RELEASE_PUBKEY))


def test_the_maintainers_key_matches_the_baked_in_public_key_when_it_is_present():
    path = os.path.expanduser("~/WubarrkCODING/DLNE_RELEASE_KEYS/dlne_release_key.pem")
    if not os.path.isfile(path):
        pytest.skip("the maintainer's release key is not on this machine")
    with open(path, "rb") as handle:
        assert release.public_key_hex(handle.read()) == release.RELEASE_PUBKEY
