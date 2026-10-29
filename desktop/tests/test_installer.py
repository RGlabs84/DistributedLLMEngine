"""Nothing unverified runs: the signed-release checks, in every way a release can be wrong."""

import io
import json
import os
import zipfile

import pytest

from conftest import Release, release, rpc_zip, small_source_zip
from dlne_donate import installer


def folder(parent, name):
    path = parent / name
    path.mkdir()
    return path


def make_installer(tmp_path, served, http=None, platform="linux-x64", **kwargs):
    root = tmp_path / "engine"
    http = http or served.http()
    return root, http, installer.EngineInstaller(str(root), "https://example.test", http=http, public_key=served.public_key,
                                                  platform=platform, **kwargs)


def tree(root):
    found = []
    for folder, _dirs, files in os.walk(root):
        for name in files:
            found.append(os.path.relpath(os.path.join(folder, name), root).replace(os.sep, "/"))
    return sorted(found)


def test_a_signed_release_installs_and_is_marked_verified(tmp_path, served):
    root, http, inst = make_installer(tmp_path, served)
    engine = inst.ensure()
    assert installer.is_verified(engine)
    assert engine.version == "9.9.9"
    assert (root / "dlne_server.py").read_bytes() == b"print('fake dlne_server')\n"
    assert (root / "engine" / "worker.py").exists()
    assert (root / "bin" / "ggml-rpc-server").exists()
    assert os.access(root / "bin" / "ggml-rpc-server", os.X_OK) or os.name == "nt"
    assert not [p for p in tree(root) if p.endswith((".zip", ".new", ".tmp"))]    # no scratch left behind
    assert any(u.endswith("/v1/release") for u in http.requests)


def test_second_run_downloads_nothing(tmp_path, served):
    _root, http, inst = make_installer(tmp_path, served)
    inst.ensure()
    first = len(http.downloads())
    inst.ensure()
    assert len(http.downloads()) == first


def test_a_look_alike_is_not_verified(tmp_path, served):
    _root, _http, inst = make_installer(tmp_path, served)
    real = inst.ensure()
    fake = installer.VerifiedEngine(root=real.root, version=real.version, manifest=real.manifest, rpc_platform=None)
    assert not installer.is_verified(fake)
    assert not installer.is_verified(object())


def test_a_manifest_signed_by_someone_else_installs_nothing(tmp_path, served):
    other = Release(folder(tmp_path, "other"))   # a different key
    root, http, inst = make_installer(tmp_path, served)
    inst.public_key = other.public_key           # the app trusts the other key; the served manifest is signed by `served`
    with pytest.raises(installer.VerificationError, match="signature does not verify"):
        inst.ensure()
    assert not root.exists() or tree(root) == []
    assert http.downloads() == []                # not even downloaded


def test_an_unsigned_manifest_installs_nothing(tmp_path):
    served = Release(tmp_path, sign=False)
    root, http, inst = make_installer(tmp_path, served)
    with pytest.raises(installer.VerificationError):
        inst.ensure()
    assert http.downloads() == []
    assert not root.exists() or tree(root) == []


def test_a_manifest_that_was_edited_after_signing_is_refused(tmp_path, served):
    served.manifest["version"] = "9.9.8"
    root, http, inst = make_installer(tmp_path, served)
    with pytest.raises(installer.VerificationError):
        inst.ensure()
    assert http.downloads() == []


def test_the_real_release_key_is_the_default_trust_anchor(tmp_path, served):
    """Without a test key, the app trusts exactly engine/release.py's RELEASE_PUBKEY, so a test-signed release is refused."""
    inst = installer.EngineInstaller(str(tmp_path / "e"), "https://example.test", http=served.http(), platform="linux-x64")
    with pytest.raises(installer.VerificationError):
        inst.ensure()


def test_an_archive_that_is_not_the_signed_one_is_not_installed(tmp_path, served):
    root, http, inst = make_installer(tmp_path, served)
    served.source = served.source + b"x"          # the manifest still names the original bytes
    with pytest.raises(installer.VerificationError, match="size does not match|hash does not match"):
        inst.ensure()
    assert not (root / "dlne_server.py").exists()


def test_same_size_different_bytes_is_refused(tmp_path, served):
    root, http, inst = make_installer(tmp_path, served)
    tampered = bytearray(served.source)
    tampered[len(tampered) // 2] ^= 0xFF
    served.source = bytes(tampered)
    with pytest.raises(installer.VerificationError, match="hash does not match"):
        inst.ensure()
    assert not (root / "dlne_server.py").exists()


@pytest.mark.parametrize("bad", ["../evil.py", "/etc/evil.py", "engine/../../evil.py", "engine\\evil.py", "bin/anything", "engine/evil.exe",
                                 "engine/sub/deep.py", "config/config.txt"])
def test_an_archive_with_a_file_a_release_never_holds_is_refused(tmp_path, bad):
    served = Release(tmp_path, source=small_source_zip(extra={bad: b"boom"}))
    root, _http, inst = make_installer(tmp_path, served)
    with pytest.raises(installer.VerificationError):
        inst.ensure()
    assert not (root / "dlne_server.py").exists()
    assert not (tmp_path / "evil.py").exists()


def test_an_archive_without_the_worker_is_refused(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("dlne_server.py", "x")
        archive.writestr("engine/version.py", 'VERSION = "9.9.9"\n')
    served = Release(tmp_path, source=buffer.getvalue())
    _root, _http, inst = make_installer(tmp_path, served)
    with pytest.raises(installer.VerificationError, match="no engine/worker.py"):
        inst.ensure()


def test_an_older_release_is_never_installed_over_a_newer_one(tmp_path, served):
    root, _http, inst = make_installer(tmp_path, served)
    inst.ensure()
    older = Release(folder(tmp_path, "older"), version="9.9.1", key=(served.pem, served.public_key))
    _r, _h, inst2 = make_installer(tmp_path, older)
    with pytest.raises(installer.VerificationError, match="older than the 9.9.9 already installed"):
        inst2.ensure()
    assert (root / "engine" / "version.py").read_text().strip() == 'VERSION = "9.9.9"'


def test_a_newer_release_replaces_the_files_and_drops_the_ones_it_removed(tmp_path, served):
    root, _http, inst = make_installer(tmp_path, served)
    inst.ensure()
    assert (root / "engine" / "worker.py").exists()
    newer_files = {"engine/extra.py": b"Y = 2\n"}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in {"dlne_server.py": b"print('v2')\n", "requirements.txt": b"httpx\n", "engine/__init__.py": b"",
                           "engine/version.py": b'VERSION = "9.10.0"\n', "engine/worker.py": b"X = 2\n", **newer_files}.items():
            archive.writestr(name, data)
    newer = Release(folder(tmp_path, "newer"), version="9.10.0", source=buffer.getvalue(), key=(served.pem, served.public_key))
    _r, _h, inst2 = make_installer(tmp_path, newer)
    engine = inst2.ensure()
    assert engine.version == "9.10.0"
    assert (root / "dlne_server.py").read_bytes() == b"print('v2')\n"
    assert (root / "engine" / "extra.py").exists()
    assert not (root / "engine" / "static" / "icon.png").exists()       # the old release's file that the new one dropped


def test_a_file_changed_on_disk_after_install_is_caught_before_every_run(tmp_path, served):
    root, http, inst = make_installer(tmp_path, served)
    inst.ensure()
    (root / "engine" / "worker.py").write_text("import os; os.system('evil')\n")
    with pytest.raises(installer.VerificationError, match="does not match the signed release"):
        inst.check_on_disk(served.manifest, "linux-x64")
    # ensure() repairs it from the signed archive instead of trusting what is on disk
    engine = inst.ensure()
    assert (root / "engine" / "worker.py").read_bytes() == b"X = 1\n"
    assert installer.is_verified(engine)


def test_the_rpc_programs_must_be_the_signed_ones(tmp_path, served):
    root, _http, inst = make_installer(tmp_path, served)
    served.rpc = served.rpc + b"x"
    with pytest.raises(installer.VerificationError, match="does not match|hash"):
        inst.ensure()
    assert not (root / "bin").exists()


@pytest.mark.parametrize("bad", ["../escape", "bin/../../escape", "evil.sh", "bin/sub/x", "bin/evil.sh"])
def test_the_rpc_zip_may_hold_only_the_servers_and_their_libraries(tmp_path, bad):
    served = Release(tmp_path, rpc=rpc_zip(extra={bad: b"boom"}))
    root, _http, inst = make_installer(tmp_path, served)
    with pytest.raises(installer.VerificationError):
        inst.ensure()
    assert not (root / "bin").exists()
    assert not (tmp_path / "escape").exists()


def test_a_release_that_does_not_sign_this_platforms_programs_installs_none(tmp_path, served):
    root, _http, inst = make_installer(tmp_path, served, platform="win-x64")
    with pytest.raises(installer.VerificationError, match="does not sign the engine programs for win-x64"):
        inst.ensure()


def test_an_unreachable_coordinator_is_a_fetch_error_not_a_verification_error(tmp_path, served):
    class Down:
        def get_json(self, url, timeout=15.0):
            raise installer.FetchError("no route")

    _root, _http, inst = make_installer(tmp_path, served, http=Down())
    with pytest.raises(installer.FetchError):
        inst.ensure()


def test_a_coordinator_without_a_release_endpoint_is_refused(tmp_path, served):
    class NoRelease:
        def get_json(self, url, timeout=15.0):
            return 404, None

    _root, _http, inst = make_installer(tmp_path, served, http=NoRelease())
    with pytest.raises(installer.VerificationError, match="does not publish a signed release"):
        inst.ensure()


def test_download_larger_than_the_signed_size_is_cut_off(tmp_path):
    """The real Http.download never writes more than the signed size."""
    import http.server
    import threading

    payload = b"a" * 5000

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        target = tmp_path / "out.bin"
        with pytest.raises(installer.FetchError, match="larger than the signed"):
            installer.Http().download(f"http://127.0.0.1:{server.server_port}/x", str(target), 1000)
    finally:
        server.shutdown()


def test_missing_modules_names_what_the_app_does_not_carry(tmp_path):
    engine = tmp_path / "e"
    (engine / "engine").mkdir(parents=True)
    (engine / "dlne_server.py").write_text("import httpx\nimport os\nfrom engine import x\n")
    (engine / "engine" / "x.py").write_text("import something_new\ntry:\n    import gi\nexcept ImportError:\n    pass\n")
    assert installer.missing_modules(str(engine), available=lambda name: name == "httpx") == ["something_new"]
    assert installer.missing_modules(str(engine), available=lambda name: True) == []


def test_the_real_engine_needs_only_libraries_the_app_pins():
    """Drift guard: every third-party module the repository's engine imports is in desktop/requirements.txt."""
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    wanted = installer.missing_modules(repo, available=lambda name: False)
    pinned = set()
    with open(os.path.join(repo, "desktop", "requirements.txt"), encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line and not line.startswith("#"):
                pinned.add(line.split("==")[0].strip().lower().replace("-", "_"))
    aliases = {"gputil": "gputil", "pil": "pillow"}
    missing = [m for m in wanted if aliases.get(m.lower(), m.lower()) not in pinned]
    assert not missing, f"the engine imports {missing}, which desktop/requirements.txt does not pin"
