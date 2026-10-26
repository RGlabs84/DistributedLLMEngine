"""
Tests for scripts/release_apply.py: installing a release on the coordinator host.

The installer is the one piece of the release pipeline that runs against a live service, so
every way it can fail is exercised here against real temp directories: a bad archive must
leave the install byte-for-byte untouched, and a release that does not come up healthy must
be undone.
"""

import hashlib
import http.server
import importlib.util
import json
import os
import threading
import zipfile

import pytest

from engine import release

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "release_apply.py")
_spec = importlib.util.spec_from_file_location("release_apply", SCRIPT)
apply_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(apply_mod)

BUILT_AT = "2026-10-25T12:00:00Z"


# --- fixtures -----------------------------------------------------------------------------

PNG = b"\x89PNG\r\n\x1a\n"


def make_repo(base, name, version, extra_engine=None, static=None):
    root = base / name
    (root / "engine").mkdir(parents=True)
    (root / "engine" / "version.py").write_text(f'VERSION = "{version}"\n')
    (root / "engine" / "alpha.py").write_text(f"ALPHA = '{version}'\n")
    for fname, body in (extra_engine or {}).items():
        (root / "engine" / fname).write_text(body)
    if static:
        (root / "engine" / "static").mkdir()
        for fname, body in static.items():
            (root / "engine" / "static" / fname).write_bytes(body)
    (root / "dlne_server.py").write_text(f"print('server {version}')\n")
    (root / "requirements.txt").write_text("fastapi\n")
    return str(root)


def build(base, repo, version, name="new.zip"):
    archive = str(base / name)
    release.build_source_archive(repo, archive)
    manifest = release.make_manifest(version, archive, "abc1234", BUILT_AT, "test")
    return archive, manifest


def tree_state(root):
    """Hash of every file under root (path -> sha256), to prove a directory did not change."""
    state = {}
    for dirpath, _dirs, files in os.walk(root):
        if os.sep + ".releases" in dirpath or dirpath.endswith(".releases"):
            continue
        for name in files:
            path = os.path.join(dirpath, name)
            state[os.path.relpath(path, root)] = hashlib.sha256(open(path, "rb").read()).hexdigest()
    return state


@pytest.fixture
def host(tmp_path):
    """A coordinator install at 1.0.0, with data the installer must never touch."""
    old = make_repo(tmp_path, "oldrepo", "1.0.0", {"tray.py": "TRAY = 1\n"})
    root = tmp_path / "box"
    root.mkdir()
    for rel in ("dlne_server.py", "requirements.txt", "engine/version.py", "engine/alpha.py", "engine/tray.py"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(open(os.path.join(old, rel), "rb").read())
    (root / "config").mkdir()
    (root / "config" / "config.txt").write_text("NETWORK_KEY=secret\n")
    (root / "models").mkdir()
    (root / "models" / "m.gguf").write_bytes(b"weights")
    (root / ".venv").mkdir()
    (root / ".venv" / "marker").write_text("venv")
    return str(root)


class Recorder:
    def __init__(self, healthy=True, restart_error=None, healthy_for=None):
        self.restarts = 0
        self.health_calls = []
        self.healthy = healthy
        self.restart_error = restart_error
        self.healthy_for = healthy_for     # None: governed by `healthy`; else the only version that is healthy

    def restart(self):
        self.restarts += 1
        if self.restart_error and self.restarts == 1:
            raise apply_mod.ApplyError(self.restart_error)

    def health(self, version):
        self.health_calls.append(version)
        ok = self.healthy if self.healthy_for is None else (version == self.healthy_for)
        return (ok, "fine" if ok else "not healthy")


def run_apply(host, archive, manifest, rec, **kwargs):
    return apply_mod.apply_release(host, archive, manifest, rec.restart, rec.health, log=lambda _m: None, **kwargs)


# --- happy path ---------------------------------------------------------------------------

def test_a_good_release_installs(tmp_path, host):
    new = make_repo(tmp_path, "newrepo", "2.0.0", {"landing.py": "L = 1\n"})
    archive, manifest = build(tmp_path, new, "2.0.0")
    rec = Recorder()
    result = run_apply(host, archive, manifest, rec)

    assert apply_mod.installed_version(host) == "2.0.0"
    assert open(os.path.join(host, "engine", "landing.py")).read() == "L = 1\n"
    assert open(os.path.join(host, "dlne_server.py")).read() == "print('server 2.0.0')\n"
    assert rec.restarts == 1 and rec.health_calls == ["2.0.0"]
    assert result["version"] == "2.0.0" and result["previous"] == "1.0.0"


def test_the_archive_and_manifest_are_kept_for_the_coordinator_to_serve(tmp_path, host):
    archive, manifest = build(tmp_path, make_repo(tmp_path, "n", "2.0.0"), "2.0.0")
    run_apply(host, archive, manifest, Recorder())
    assert open(os.path.join(host, "dlne-source.zip"), "rb").read() == open(archive, "rb").read()
    assert json.load(open(os.path.join(host, "release.json")))["sha256"] == manifest["sha256"]


def test_data_config_models_and_the_venv_are_never_touched(tmp_path, host):
    before = {k: v for k, v in tree_state(host).items() if k.split(os.sep)[0] in ("config", "models", ".venv")}
    archive, manifest = build(tmp_path, make_repo(tmp_path, "n", "2.0.0"), "2.0.0")
    run_apply(host, archive, manifest, Recorder())
    after = {k: v for k, v in tree_state(host).items() if k.split(os.sep)[0] in ("config", "models", ".venv")}
    assert before == after and len(before) == 3


def test_files_the_release_dropped_are_removed_but_kept_in_the_backup(tmp_path, host):
    archive, manifest = build(tmp_path, make_repo(tmp_path, "n", "2.0.0"), "2.0.0")
    result = run_apply(host, archive, manifest, Recorder())
    assert result["pruned"] == ["engine/tray.py"]
    assert not os.path.exists(os.path.join(host, "engine", "tray.py"))
    import tarfile
    with tarfile.open(os.path.join(host, ".releases", result["backup"], "files.tar")) as tar:
        assert "engine/tray.py" in tar.getnames()


def test_keep_stale_leaves_dropped_files(tmp_path, host):
    archive, manifest = build(tmp_path, make_repo(tmp_path, "n", "2.0.0"), "2.0.0")
    run_apply(host, archive, manifest, Recorder(), prune=False)
    assert os.path.exists(os.path.join(host, "engine", "tray.py"))


def test_the_backup_is_named_for_the_version_being_left(tmp_path, host):
    archive, manifest = build(tmp_path, make_repo(tmp_path, "n", "2.0.0"), "2.0.0")
    result = run_apply(host, archive, manifest, Recorder())
    assert result["backup"].startswith("1.0.0-")
    assert [b["version"] for b in apply_mod.list_backups(host)] == ["1.0.0"]


def test_a_fresh_host_with_nothing_installed_can_be_installed(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    archive, manifest = build(tmp_path, make_repo(tmp_path, "n", "2.0.0"), "2.0.0")
    result = run_apply(str(root), archive, manifest, Recorder())
    assert result["previous"] == "none" and apply_mod.installed_version(str(root)) == "2.0.0"


# --- a bad archive changes nothing --------------------------------------------------------

def _assert_untouched(host, before, rec):
    assert tree_state(host) == before
    assert rec.restarts == 0
    assert not os.path.exists(os.path.join(host, ".releases"))


def test_a_wrong_hash_is_refused_and_nothing_changes(tmp_path, host):
    archive, manifest = build(tmp_path, make_repo(tmp_path, "n", "2.0.0"), "2.0.0")
    manifest["sha256"] = "0" * 64
    before, rec = tree_state(host), Recorder()
    with pytest.raises(apply_mod.ApplyError, match="hash"):
        run_apply(host, archive, manifest, rec)
    _assert_untouched(host, before, rec)


def test_a_wrong_size_is_refused(tmp_path, host):
    archive, manifest = build(tmp_path, make_repo(tmp_path, "n", "2.0.0"), "2.0.0")
    manifest["size"] += 1
    before, rec = tree_state(host), Recorder()
    with pytest.raises(apply_mod.ApplyError, match="size"):
        run_apply(host, archive, manifest, rec)
    _assert_untouched(host, before, rec)


def test_a_manifest_that_lies_about_the_version_is_refused(tmp_path, host):
    archive, manifest = build(tmp_path, make_repo(tmp_path, "n", "2.0.0"), "2.0.0")
    manifest["version"] = "9.9.9"
    before, rec = tree_state(host), Recorder()
    with pytest.raises(apply_mod.ApplyError, match="9.9.9"):
        run_apply(host, archive, manifest, rec)
    _assert_untouched(host, before, rec)


def test_code_that_does_not_compile_is_refused_before_anything_is_replaced(tmp_path, host):
    repo = make_repo(tmp_path, "n", "2.0.0", {"broken.py": "def oops(:\n"})
    archive, manifest = build(tmp_path, repo, "2.0.0")
    before, rec = tree_state(host), Recorder()
    with pytest.raises(apply_mod.ApplyError, match="broken.py does not compile"):
        run_apply(host, archive, manifest, rec)
    _assert_untouched(host, before, rec)


def _hand_archive(tmp_path, entries):
    path = str(tmp_path / "hand.zip")
    with zipfile.ZipFile(path, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    manifest = {"version": "2.0.0", "sha256": hashlib.sha256(open(path, "rb").read()).hexdigest(),
                "size": os.path.getsize(path)}
    return path, manifest


@pytest.mark.parametrize("evil", ["../evil.py", "engine/../../evil.py", "/etc/evil.py", "engine\\evil.py",
                                  "C:/evil.py", "config/config.txt", "engine/sub/x.py", "models/m.gguf",
                                  "engine/data.txt", ".env",
                                  # Only engine/static/<name>.png ships there, nothing else and nothing deeper:
                                  "engine/static/notes.txt", "engine/static/icon.svg", "engine/static/sub/icon.png",
                                  "engine/static/../evil.png", "engine/static.png", "static/icon.png",
                                  # These pass the "engine/<name>.py" shape, so only the path guard stops them:
                                  "engine/..\\evil.py", "engine/c:evil.py", "engine/..\\..\\evil.py"])
def test_an_archive_cannot_write_outside_what_a_release_ships(tmp_path, host, evil):
    path, manifest = _hand_archive(tmp_path, {"engine/version.py": 'VERSION = "2.0.0"\n', evil: "x"})
    before, rec = tree_state(host), Recorder()
    with pytest.raises(apply_mod.ApplyError):
        run_apply(host, path, manifest, rec)
    _assert_untouched(host, before, rec)
    assert not os.path.exists(os.path.join(os.path.dirname(host), "evil.py"))


def test_a_corrupt_zip_is_refused(tmp_path, host):
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"this is not a zip")
    manifest = {"version": "2.0.0", "sha256": hashlib.sha256(b"this is not a zip").hexdigest(), "size": 17}
    before, rec = tree_state(host), Recorder()
    with pytest.raises(apply_mod.ApplyError, match="valid zip"):
        run_apply(host, str(bad), manifest, rec)
    _assert_untouched(host, before, rec)


def test_a_png_that_is_not_a_png_is_refused(tmp_path, host):
    path, manifest = _hand_archive(tmp_path, {"engine/version.py": 'VERSION = "2.0.0"\n',
                                              "engine/static/icon-192.png": b"<svg>not an image</svg>"})
    before, rec = tree_state(host), Recorder()
    with pytest.raises(apply_mod.ApplyError, match=r"engine/static/icon-192\.png is not a PNG image"):
        run_apply(host, path, manifest, rec)
    _assert_untouched(host, before, rec)


# --- the icons ship beside the code -------------------------------------------------------

def test_the_icons_install_into_engine_static_and_are_owned_by_the_release(tmp_path, host):
    icons = {"icon-192.png": PNG + b"192", "favicon-32.png": PNG + b"32"}
    archive, manifest = build(tmp_path, make_repo(tmp_path, "n", "2.0.0", static=icons), "2.0.0")
    run_apply(host, archive, manifest, Recorder())
    for name, data in icons.items():
        assert open(os.path.join(host, "engine", "static", name), "rb").read() == data
    assert [f for f in apply_mod.managed_files(host) if f.startswith("engine/static/")] == [
        "engine/static/favicon-32.png", "engine/static/icon-192.png"]


def test_a_stray_file_in_engine_static_is_not_a_release_file(tmp_path, host):
    os.makedirs(os.path.join(host, "engine", "static"))
    open(os.path.join(host, "engine", "static", "notes.txt"), "w").write("mine")
    archive, manifest = build(tmp_path, make_repo(tmp_path, "n", "2.0.0", static={"icon-192.png": PNG}), "2.0.0")
    run_apply(host, archive, manifest, Recorder())
    assert open(os.path.join(host, "engine", "static", "notes.txt")).read() == "mine"
    assert "engine/static/notes.txt" not in apply_mod.managed_files(host)


def test_icons_the_release_dropped_are_pruned_and_a_rollback_brings_them_back(tmp_path, host):
    first = make_repo(tmp_path, "first", "2.0.0", static={"icon-192.png": PNG + b"old", "favicon-32.png": PNG + b"f"})
    run_apply(host, *build(tmp_path, first, "2.0.0", "first.zip"), Recorder())
    second = make_repo(tmp_path, "second", "3.0.0", static={"icon-192.png": PNG + b"new"})
    result = run_apply(host, *build(tmp_path, second, "3.0.0", "second.zip"), Recorder())

    assert result["pruned"] == ["engine/static/favicon-32.png"]
    assert open(os.path.join(host, "engine", "static", "icon-192.png"), "rb").read() == PNG + b"new"
    assert not os.path.exists(os.path.join(host, "engine", "static", "favicon-32.png"))

    rec = Recorder()
    apply_mod.rollback_release(host, result["backup"], rec.restart, rec.health, log=lambda _m: None)
    assert open(os.path.join(host, "engine", "static", "icon-192.png"), "rb").read() == PNG + b"old"
    assert open(os.path.join(host, "engine", "static", "favicon-32.png"), "rb").read() == PNG + b"f"


def test_rolling_back_to_a_release_without_icons_removes_them(tmp_path, host):
    """The host starts at 1.0.0, which had no engine/static: going back to it must not keep the new icons."""
    archive, manifest = build(tmp_path, make_repo(tmp_path, "n", "2.0.0", static={"icon-192.png": PNG}), "2.0.0")
    run_apply(host, archive, manifest, Recorder())
    rec = Recorder()
    apply_mod.rollback_release(host, None, rec.restart, rec.health, log=lambda _m: None)
    assert not os.path.exists(os.path.join(host, "engine", "static", "icon-192.png"))
    assert apply_mod.installed_version(host) == "1.0.0"


def test_an_unhealthy_release_with_new_icons_is_rolled_back_byte_for_byte(tmp_path, host):
    os.makedirs(os.path.join(host, "engine", "static"))
    open(os.path.join(host, "engine", "static", "icon-192.png"), "wb").write(PNG + b"old")
    before = tree_state(host)
    new = make_repo(tmp_path, "n", "2.0.0", static={"icon-192.png": PNG + b"new", "icon-512.png": PNG + b"big"})
    archive, manifest = build(tmp_path, new, "2.0.0")
    with pytest.raises(apply_mod.ApplyError, match="Rolled back"):
        run_apply(host, archive, manifest, Recorder(healthy_for="1.0.0"))
    assert tree_state(host) == before


# --- a release that does not come up is undone --------------------------------------------

def test_an_unhealthy_release_is_rolled_back_exactly(tmp_path, host):
    before = tree_state(host)
    new = make_repo(tmp_path, "n", "2.0.0", {"landing.py": "L = 1\n"})
    archive, manifest = build(tmp_path, new, "2.0.0")
    rec = Recorder(healthy_for="1.0.0")          # 2.0.0 never comes up; 1.0.0 does
    with pytest.raises(apply_mod.ApplyError, match="Rolled back; the previous release is running again"):
        run_apply(host, archive, manifest, rec)

    assert tree_state(host) == before             # byte for byte, including the file that was only in 2.0.0
    assert not os.path.exists(os.path.join(host, "engine", "landing.py"))
    assert os.path.exists(os.path.join(host, "engine", "tray.py"))
    assert rec.restarts == 2 and rec.health_calls == ["2.0.0", "1.0.0"]


def test_a_failed_restart_is_rolled_back(tmp_path, host):
    before = tree_state(host)
    archive, manifest = build(tmp_path, make_repo(tmp_path, "n", "2.0.0"), "2.0.0")
    rec = Recorder(restart_error="systemd said no")
    with pytest.raises(apply_mod.ApplyError, match="systemd said no"):
        run_apply(host, archive, manifest, rec)
    assert tree_state(host) == before and rec.restarts == 2


def test_if_the_old_release_also_fails_to_return_it_says_so(tmp_path, host):
    archive, manifest = build(tmp_path, make_repo(tmp_path, "n", "2.0.0"), "2.0.0")
    rec = Recorder(healthy=False)
    with pytest.raises(apply_mod.ApplyError, match="did NOT come back"):
        run_apply(host, archive, manifest, rec)


def test_a_broken_backup_is_reported_with_where_it_is(tmp_path, host, monkeypatch):
    archive, manifest = build(tmp_path, make_repo(tmp_path, "n", "2.0.0"), "2.0.0")
    rec = Recorder(healthy=False)

    def boom(root, backup_dir):
        raise OSError("disk on fire")

    monkeypatch.setattr(apply_mod, "restore_backup", boom)
    with pytest.raises(apply_mod.ApplyError, match=r"rollback failed.*disk on fire.*\.releases"):
        run_apply(host, archive, manifest, rec)


# --- manual rollback ----------------------------------------------------------------------

def test_rollback_restores_a_backup_and_saves_the_current_state_first(tmp_path, host):
    archive, manifest = build(tmp_path, make_repo(tmp_path, "n", "2.0.0", {"landing.py": "L = 1\n"}), "2.0.0")
    run_apply(host, archive, manifest, Recorder())
    assert apply_mod.installed_version(host) == "2.0.0"

    rec = Recorder()
    result = apply_mod.rollback_release(host, None, rec.restart, rec.health, log=lambda _m: None)
    assert result["version"] == "1.0.0" and apply_mod.installed_version(host) == "1.0.0"
    assert not os.path.exists(os.path.join(host, "engine", "landing.py"))
    assert os.path.exists(os.path.join(host, "engine", "tray.py"))
    assert rec.restarts == 1 and rec.health_calls == ["1.0.0"]
    assert any("before-rollback" in b["name"] for b in apply_mod.list_backups(host)), "rollback must itself be undoable"


def test_rollback_can_name_a_specific_backup(tmp_path, host):
    archive, manifest = build(tmp_path, make_repo(tmp_path, "n", "2.0.0"), "2.0.0")
    first = run_apply(host, archive, manifest, Recorder())["backup"]
    rec = Recorder()
    out = apply_mod.rollback_release(host, first, rec.restart, rec.health, log=lambda _m: None)
    assert out["restored"] == first


def test_rollback_errors_are_clear(tmp_path, host):
    rec = Recorder()
    with pytest.raises(apply_mod.ApplyError, match="no backups"):
        apply_mod.rollback_release(host, None, rec.restart, rec.health, log=lambda _m: None)
    archive, manifest = build(tmp_path, make_repo(tmp_path, "n", "2.0.0"), "2.0.0")
    run_apply(host, archive, manifest, Recorder())
    with pytest.raises(apply_mod.ApplyError, match="No backup named 'nope'"):
        apply_mod.rollback_release(host, "nope", rec.restart, rec.health, log=lambda _m: None)
    assert rec.restarts == 0


def test_a_backup_with_an_unexpected_entry_is_not_restored(tmp_path, host):
    archive, manifest = build(tmp_path, make_repo(tmp_path, "n", "2.0.0"), "2.0.0")
    name = run_apply(host, archive, manifest, Recorder())["backup"]
    import tarfile, io
    path = os.path.join(host, ".releases", name, "files.tar")
    with tarfile.open(path, "a") as tar:
        info = tarfile.TarInfo("config/config.txt")
        info.size = 4
        tar.addfile(info, io.BytesIO(b"evil"))
    with pytest.raises(apply_mod.ApplyError, match="unexpected entry"):
        apply_mod.restore_backup(host, os.path.join(host, ".releases", name))
    assert open(os.path.join(host, "config", "config.txt")).read() == "NETWORK_KEY=secret\n"


# --- helpers ------------------------------------------------------------------------------

def test_managed_files_are_only_what_a_release_owns(host):
    assert apply_mod.managed_files(host) == ["dlne_server.py", "engine/alpha.py", "engine/tray.py",
                                             "engine/version.py", "requirements.txt"]


def test_installed_version_of_nothing_is_none(tmp_path):
    assert apply_mod.installed_version(str(tmp_path)) == "none"


def test_a_tampered_manifest_fails_the_signature_check(tmp_path, host):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    archive, manifest = build(tmp_path, make_repo(tmp_path, "n", "2.0.0"), "2.0.0")
    signed = release.sign_manifest(manifest, pem)
    pub = release.public_key_hex(pem)

    run_apply(host, archive, signed, Recorder(), pubkey_hex=pub)           # genuine: installs

    tampered = dict(signed, notes="changed after signing")
    other = make_repo(tmp_path, "m", "3.0.0")
    archive3, manifest3 = build(tmp_path, other, "3.0.0", "three.zip")
    signed3 = release.sign_manifest(manifest3, pem)
    tampered3 = dict(signed3, notes="changed after signing")
    before = tree_state(host)
    with pytest.raises(apply_mod.ApplyError, match="signature"):
        run_apply(host, archive3, tampered3, Recorder(), pubkey_hex=pub)
    assert tree_state(host) == before


# --- the real health check ----------------------------------------------------------------

class _Server:
    def __init__(self, version, release_json, archive_bytes):
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                routes = {
                    "/v1/cluster/status": json.dumps({"server_version": outer.version}).encode(),
                    "/v1/release": json.dumps(outer.release_json).encode(),
                    "/v1/download/source": outer.archive_bytes,
                }
                body = routes.get(self.path)
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

        self.version, self.release_json, self.archive_bytes = version, release_json, archive_bytes
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()


def _health(url, manifest, timeout=1.0):
    return apply_mod.http_health(url, lambda _v: manifest, timeout=timeout, poll=0.05, sleep=lambda _s: None)


def test_health_passes_when_version_manifest_and_archive_all_agree(tmp_path):
    archive, manifest = build(tmp_path, make_repo(tmp_path, "n", "2.0.0"), "2.0.0")
    server = _Server("2.0.0", manifest, open(archive, "rb").read())
    try:
        ok, detail = _health(server.url, manifest)("2.0.0")
    finally:
        server.close()
    assert ok, detail


def test_health_fails_on_the_wrong_version(tmp_path):
    archive, manifest = build(tmp_path, make_repo(tmp_path, "n", "2.0.0"), "2.0.0")
    server = _Server("1.0.0", manifest, open(archive, "rb").read())
    try:
        ok, detail = _health(server.url, manifest, timeout=0.3)("2.0.0")
    finally:
        server.close()
    assert not ok and "1.0.0" in detail and "2.0.0" in detail


def test_health_fails_when_the_served_manifest_is_a_different_release(tmp_path):
    archive, manifest = build(tmp_path, make_repo(tmp_path, "n", "2.0.0"), "2.0.0")
    server = _Server("2.0.0", dict(manifest, sha256="1" * 64), open(archive, "rb").read())
    try:
        ok, detail = _health(server.url, manifest)("2.0.0")
    finally:
        server.close()
    assert not ok and "different release manifest" in detail


def test_health_fails_when_the_served_archive_does_not_match(tmp_path):
    archive, manifest = build(tmp_path, make_repo(tmp_path, "n", "2.0.0"), "2.0.0")
    server = _Server("2.0.0", manifest, b"some other bytes")
    try:
        ok, detail = _health(server.url, manifest)("2.0.0")
    finally:
        server.close()
    assert not ok and "does not hash" in detail


def test_health_fails_when_nothing_is_listening():
    ok, detail = _health("http://127.0.0.1:1", {"sha256": "0" * 64}, timeout=0.2)("2.0.0")
    assert not ok and "timed out" in detail


# --- command line -------------------------------------------------------------------------

def test_the_command_line_installs_and_lists(tmp_path, host, monkeypatch, capsys):
    archive, manifest = build(tmp_path, make_repo(tmp_path, "n", "2.0.0"), "2.0.0")
    manifest_path = tmp_path / "release.json"
    manifest_path.write_text(json.dumps(manifest))
    rec = Recorder()
    monkeypatch.setattr(apply_mod, "systemd_restart", lambda unit, user=True: rec.restart)
    monkeypatch.setattr(apply_mod, "http_health", lambda url, mf, timeout=0: rec.health)

    assert apply_mod.main(["apply", "--root", host, "--archive", archive, "--manifest", str(manifest_path)]) == 0
    assert apply_mod.installed_version(host) == "2.0.0"
    assert apply_mod.main(["list", "--root", host]) == 0
    assert "version 1.0.0" in capsys.readouterr().out


def test_the_command_line_reports_failures_with_a_nonzero_exit(tmp_path, host, monkeypatch, capsys):
    archive, manifest = build(tmp_path, make_repo(tmp_path, "n", "2.0.0"), "2.0.0")
    manifest["sha256"] = "0" * 64
    manifest_path = tmp_path / "release.json"
    manifest_path.write_text(json.dumps(manifest))
    rec = Recorder()
    monkeypatch.setattr(apply_mod, "systemd_restart", lambda unit, user=True: rec.restart)
    monkeypatch.setattr(apply_mod, "http_health", lambda url, mf, timeout=0: rec.health)
    assert apply_mod.main(["apply", "--root", host, "--archive", archive, "--manifest", str(manifest_path)]) == 1
    assert "ERROR" in capsys.readouterr().err and rec.restarts == 0
