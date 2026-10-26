"""
Tests for scripts/release.py, the maintainer's release tool: the git checks, building and
signing, and the deploy sequence including every reason it refuses to ship.
"""

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from engine import release

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "release.py")
_spec = importlib.util.spec_from_file_location("release_cli", SCRIPT)
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _pem():
    return Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


@pytest.fixture
def key():
    pem = _pem()
    return pem, release.public_key_hex(pem)


def git(repo, *args):
    subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "engine").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "engine" / "version.py").write_text('VERSION = "2.0.0"\n')
    (root / "engine" / "alpha.py").write_text("A = 1\n")
    (root / "dlne_server.py").write_text("print('s')\n")
    (root / "requirements.txt").write_text("fastapi\n")
    (root / "tests" / "t.py").write_text("x = 1\n")
    git(str(root), "init", "-q", "-b", "main")
    git(str(root), "add", "-A")
    git(str(root), "commit", "-q", "-m", "feat: the first release")
    return str(root)


def build(repo, tmp_path, key, **kw):
    return cli.build_release(repo, str(tmp_path / "out"), key[0], kw.pop("notes", "notes"),
                             expected_pubkey=key[1], **kw)


# --- the working tree ---------------------------------------------------------------------

def test_a_clean_tree_returns_the_commit(repo):
    commit = cli.check_tree(repo, allow_dirty=False)
    assert len(commit) == 7 and commit == subprocess.run(
        ["git", "-C", repo, "rev-parse", "--short=7", "HEAD"], capture_output=True, text=True).stdout.strip()


def test_uncommitted_changes_to_shipped_files_are_refused_and_named(repo):
    open(os.path.join(repo, "engine", "alpha.py"), "w").write("A = 2\n")
    with pytest.raises(cli.CliError, match=r"engine/alpha.py"):
        cli.check_tree(repo, allow_dirty=False)


def test_a_new_untracked_shipped_file_is_refused_too(repo):
    open(os.path.join(repo, "engine", "gamma.py"), "w").write("G = 1\n")
    with pytest.raises(cli.CliError, match="gamma.py"):
        cli.check_tree(repo, allow_dirty=False)


def test_changes_to_files_a_release_does_not_ship_do_not_matter(repo):
    open(os.path.join(repo, "tests", "t.py"), "w").write("x = 2\n")
    open(os.path.join(repo, "scratch.txt"), "w").write("junk")
    assert cli.check_tree(repo, allow_dirty=False)


def test_allow_dirty_marks_the_commit_so_it_cannot_be_mistaken_for_a_clean_build(repo):
    open(os.path.join(repo, "engine", "alpha.py"), "w").write("A = 2\n")
    assert cli.check_tree(repo, allow_dirty=True).endswith("-dirty")


def test_default_notes_are_the_last_commit_subject_in_ascii(repo):
    assert cli.default_notes(repo) == "feat: the first release"
    open(os.path.join(repo, "engine", "alpha.py"), "w").write("A = 3\n")
    git(repo, "commit", "-q", "-am", "fix: café — naïve")
    notes = cli.default_notes(repo)
    assert notes.isascii() and notes.startswith("fix: caf")


def test_default_notes_are_capped(repo):
    git(repo, "commit", "-q", "--allow-empty", "-m", "x" * 500)
    assert len(cli.default_notes(repo)) == 200


# --- building -----------------------------------------------------------------------------

def test_build_produces_a_signed_manifest_for_the_archive(repo, tmp_path, key):
    built = build(repo, tmp_path, key)
    assert built["version"] == "2.0.0"
    manifest = json.load(open(built["manifest_path"]))
    assert release.verify_manifest(manifest, key[1]) == (True, "ok")
    assert release.verify_archive(manifest, built["archive"]) == (True, "ok")
    assert manifest["git_commit"] == built["commit"] and manifest["notes"] == "notes"
    assert manifest["built_at"].endswith("Z")


def test_building_the_same_commit_twice_gives_the_same_archive(repo, tmp_path, key):
    a = build(repo, tmp_path, key)["manifest"]["sha256"]
    b = build(repo, tmp_path, key)["manifest"]["sha256"]
    assert a == b


def test_a_key_that_nodes_do_not_trust_is_refused_before_anything_is_built(repo, tmp_path, key):
    with pytest.raises(cli.CliError, match="does not match the public key"):
        cli.build_release(repo, str(tmp_path / "out"), _pem(), "n", expected_pubkey=key[1])
    assert not os.path.exists(tmp_path / "out")


def test_building_with_a_dirty_tree_is_refused(repo, tmp_path, key):
    open(os.path.join(repo, "dlne_server.py"), "w").write("print('changed')\n")
    with pytest.raises(cli.CliError, match="uncommitted"):
        build(repo, tmp_path, key)


def test_notes_that_are_not_ascii_are_refused_with_a_clear_message(repo, tmp_path, key):
    with pytest.raises(cli.CliError, match="printable ASCII"):
        build(repo, tmp_path, key, notes="café")


def test_a_broken_signer_is_caught_before_anything_is_published(repo, tmp_path, key, monkeypatch):
    """If signing ever yields a signature that does not verify, the build must stop, not ship it."""
    real_sign = release.sign_manifest
    monkeypatch.setattr(release, "sign_manifest", lambda m, pem: dict(real_sign(m, pem), signature="00" * 64))
    with pytest.raises(cli.CliError, match="does not verify"):
        build(repo, tmp_path, key)
    assert not os.path.exists(tmp_path / "out" / "release.json")


def test_a_missing_key_file_says_where_it_looked(tmp_path):
    with pytest.raises(cli.CliError, match="No release key"):
        cli.load_key(str(tmp_path / "nope.pem"))


@pytest.mark.skipif(not os.path.isdir(os.path.join(REPO_ROOT, ".git")),
                    reason="needs a git checkout (the TheWall test copy has none)")
def test_the_real_repository_builds_a_valid_release(tmp_path, key):
    built = cli.build_release(REPO_ROOT, str(tmp_path / "out"), key[0], "smoke", allow_dirty=True, expected_pubkey=key[1])
    assert built["version"] == release.read_version(REPO_ROOT)
    assert release.verify_manifest(built["manifest"], key[1]) == (True, "ok")
    import zipfile
    names = zipfile.ZipFile(built["archive"]).namelist()
    assert "engine/release.py" in names and "engine/landing.py" in names


# --- the coordinator host (faked) ---------------------------------------------------------

class FakeRemote:
    """Stands in for ssh/scp. `box` is a local directory playing the coordinator host."""

    def __init__(self, tmp_path, version="1.0.0", corrupt_upload=False, apply_code=0):
        self.host, self.root = "fakehost", "/home/box/DLNEv2"
        self.box = tmp_path / "box"
        self.box.mkdir(exist_ok=True)
        self.version, self.corrupt, self.apply_code = version, corrupt_upload, apply_code
        self.commands, self.uploads = [], []

    def _local(self, remote_path):
        return self.box / remote_path.replace(self.root, "").lstrip("/").replace("/", "__")

    def run(self, command, stream=False):
        self.commands.append(command)
        if command.startswith("grep -m1 '^VERSION'"):
            return (0, f'VERSION = "{self.version}"') if self.version else (1, "")
        if command.startswith("mkdir -p"):
            return (0, "")
        if command.startswith("sha256sum"):
            path = self._local(command.split()[1])
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            return (0, f"{'0' * 64 if self.corrupt else digest}  {command.split()[1]}")
        if "release_apply.py apply" in command:
            return (self.apply_code, "")
        return (0, "")

    def put(self, local, remote_path):
        self.uploads.append(remote_path)
        shutil.copy(local, self._local(remote_path))


def run_deploy(repo, tmp_path, key, remote, **kw):
    return cli.deploy(repo, remote, key[0], "notes", expected_pubkey=key[1],
                      out_dir=str(tmp_path / "out"), log=lambda _m: None, **kw)


def test_a_deploy_uploads_verifies_and_installs(repo, tmp_path, key):
    remote = FakeRemote(tmp_path)
    result = run_deploy(repo, tmp_path, key, remote)
    assert result["version"] == "2.0.0" and result["previous"] == "1.0.0"
    assert [u.rsplit("/", 1)[1] for u in remote.uploads] == ["dlne-source.zip", "release.json", "release_apply.py"]
    assert all("/.releases/incoming/" in u for u in remote.uploads)
    apply_cmd = next(c for c in remote.commands if "release_apply.py apply" in c)
    assert "--pubkey " + key[1] in apply_cmd and "--archive" in apply_cmd and "--manifest" in apply_cmd
    assert remote.commands.index(next(c for c in remote.commands if c.startswith("sha256sum"))) < remote.commands.index(apply_cmd)


def test_the_same_version_is_never_deployed_twice(repo, tmp_path, key):
    remote = FakeRemote(tmp_path, version="2.0.0")
    with pytest.raises(cli.CliError, match="already deployed"):
        run_deploy(repo, tmp_path, key, remote)
    assert remote.uploads == [] and not any("apply" in c for c in remote.commands)


def test_an_older_version_is_refused_unless_explicitly_allowed(repo, tmp_path, key):
    remote = FakeRemote(tmp_path, version="2.5.0")
    with pytest.raises(cli.CliError, match="OLDER"):
        run_deploy(repo, tmp_path, key, remote)
    assert remote.uploads == []
    assert run_deploy(repo, tmp_path, key, FakeRemote(tmp_path, version="2.5.0"), allow_downgrade=True)["version"] == "2.0.0"


def test_a_corrupted_upload_never_reaches_the_installer(repo, tmp_path, key):
    remote = FakeRemote(tmp_path, corrupt_upload=True)
    with pytest.raises(cli.CliError, match="dlne-source.zip arrived on the coordinator host different"):
        run_deploy(repo, tmp_path, key, remote)
    assert not any("release_apply.py apply" in c for c in remote.commands)


def test_a_failed_install_is_reported_as_a_failure(repo, tmp_path, key):
    with pytest.raises(cli.CliError, match="rolled back"):
        run_deploy(repo, tmp_path, key, FakeRemote(tmp_path, apply_code=1))


def test_a_host_with_nothing_installed_can_be_deployed_to(repo, tmp_path, key):
    assert run_deploy(repo, tmp_path, key, FakeRemote(tmp_path, version=None))["previous"] == "none"


def test_a_dirty_tree_stops_a_deploy_before_it_touches_the_host(repo, tmp_path, key):
    open(os.path.join(repo, "engine", "alpha.py"), "w").write("A = 9\n")
    remote = FakeRemote(tmp_path)
    with pytest.raises(cli.CliError, match="uncommitted"):
        run_deploy(repo, tmp_path, key, remote)
    assert remote.commands == [] and remote.uploads == []


# --- checking from the outside ------------------------------------------------------------

def test_the_public_check_confirms_the_served_release(repo, tmp_path, key):
    manifest = build(repo, tmp_path, key)["manifest"]
    out = cli.verify_public("http://x", manifest, key[1], fetch=lambda u: json.dumps(manifest).encode())
    assert "serves the new signed release" in out


def test_the_public_check_catches_a_different_release_a_bad_signature_and_an_outage(repo, tmp_path, key):
    manifest = build(repo, tmp_path, key)["manifest"]
    other = dict(manifest, sha256="1" * 64)
    assert "does not verify" in cli.verify_public("http://x", manifest, key[1], fetch=lambda u: json.dumps(other).encode())
    stale = release.sign_manifest(dict(manifest, sha256="2" * 64), key[0])
    assert "not the one just installed" in cli.verify_public("http://x", manifest, key[1], fetch=lambda u: json.dumps(stale).encode())

    def down(_u):
        raise OSError("refused")

    assert "could not fetch" in cli.verify_public("http://x", manifest, key[1], fetch=down)


def test_remote_version_parses_the_hosts_version_file(tmp_path):
    assert cli.remote_version(FakeRemote(tmp_path, version="3.1.4")) == "3.1.4"
    assert cli.remote_version(FakeRemote(tmp_path, version=None)) == "none"


def test_the_command_line_parses_every_command():
    parser = cli.build_parser()
    for argv in (["status"], ["bump", "minor"], ["build"], ["build", "--notes", "x", "--allow-dirty"],
                 ["deploy", "--allow-downgrade"], ["backups"], ["rollback", "--backup", "1.0.0-20261025"]):
        assert parser.parse_args(argv).func
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_errors_exit_nonzero_without_a_traceback(tmp_path, capsys):
    assert cli.main(["--key", str(tmp_path / "missing.pem"), "build"]) == 1
    assert "ERROR: No release key" in capsys.readouterr().err
