"""
Shipping voice (2.8.0): the pinned whisper.cpp built and packaged by scripts/package_whisper.py, signed into the manifest
(whisper_build, server_whisper_sha256, server_whisper_size) by scripts/release.py, uploaded and unpacked into
<root>/bin-whisper/ by scripts/release_apply.py; the model pin and the verified downloader. Workers never see any of it,
and a node that does not know the fields ignores them.
"""

import gzip
import hashlib
import importlib.util
import io
import json
import os
import stat
import tarfile

import pytest

from engine import release
from tests.test_release_rpc_bin import (  # noqa: F401 - fixtures and helpers shared with the llama packaging tests
    BUILD, BUILT_AT, FakeRemote, Recorder, _deploy, _pem, apply_mod, assets_dir, cli, host, make_repo, packaged, pem,
    run_apply, signed_release,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WHISPER_BUILD = "v1.9.5"


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(REPO_ROOT, "scripts", filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


package_whisper = _load("package_whisper_t", "package_whisper.py")
fetch = _load("fetch_voice_models_t", "fetch_voice_models.py")


# --- scripts/package_whisper.py --------------------------------------------------------------------------------------

def make_build(path, marker="v1", with_license=True, server=None):
    os.makedirs(path, exist_ok=True)
    (path / "whisper-server").write_bytes(server if server is not None else b"\x7fELF whisper-server " + marker.encode())
    if with_license:
        (path / "LICENSE").write_bytes(b"MIT licence")
    return str(path)


def pack(tmp_path, name="out", marker="v1", build_name="build"):
    build = make_build(tmp_path / f"{build_name}-{marker}", marker)
    out = str(tmp_path / name)
    return out, package_whisper.package(build, out, WHISPER_BUILD)


def test_the_pins_name_a_tag_by_commit_and_the_upstream_is_ggml_org():
    assert package_whisper.WHISPER_BUILD == WHISPER_BUILD
    assert package_whisper.UPSTREAM_URL == "https://github.com/ggml-org/whisper.cpp"
    for commit in (package_whisper.WHISPER_COMMIT, package_whisper.VULKAN_HEADERS_COMMIT, package_whisper.SPIRV_HEADERS_COMMIT):
        assert len(commit) == 40 and set(commit) <= set("0123456789abcdef")
    assert package_whisper.WHISPER_COMMIT == "d1be6fde11ac6e0407606b4e42fe72d34add8037"


def test_the_cmake_line_is_a_static_vulkan_build_that_does_not_tune_for_the_builder():
    args = package_whisper.cmake_configure_args("/s", "/b", "/h", "/p")
    for wanted in ("-DGGML_VULKAN=ON", "-DBUILD_SHARED_LIBS=OFF", "-DGGML_NATIVE=OFF", "-DGGML_OPENMP=OFF",
                   "-DVulkan_INCLUDE_DIR=/h/include", "-DCMAKE_PREFIX_PATH=/p", "-DCMAKE_INSTALL_RPATH=$ORIGIN"):
        assert wanted in args
    assert any("-static-libstdc++" in a for a in args) and any("-isystem /p/include" in a for a in args)


def test_the_package_holds_whisper_server_and_the_licence_flat(tmp_path):
    out, info = pack(tmp_path)
    assert sorted(os.listdir(out)) == ["build.json", "server-whisper.tar.gz"]
    with tarfile.open(os.path.join(out, "server-whisper.tar.gz")) as tar:
        members = tar.getmembers()
        names = [m.name for m in members]
        modes = {m.name: m.mode for m in members}
        stamps = {(m.mtime, m.uid, m.gid) for m in members}
        assert tar.extractfile("whisper-server").read() == b"\x7fELF whisper-server v1"
    assert names == ["LICENSE", "whisper-server"]
    assert modes == {"whisper-server": 0o755, "LICENSE": 0o644} and stamps == {(0, 0, 0)}
    assert json.load(open(os.path.join(out, "build.json"))) == info
    assert info["whisper_build"] == WHISPER_BUILD and info["server_whisper"] == "server-whisper.tar.gz"
    assert info["source_commit"] == package_whisper.WHISPER_COMMIT
    assert info["binary_sha256"] == hashlib.sha256(b"\x7fELF whisper-server v1").hexdigest()


def test_packaging_twice_gives_the_same_bytes(tmp_path):
    one, _ = pack(tmp_path, "one")
    two, _ = pack(tmp_path, "two", build_name="again")
    assert open(os.path.join(one, "server-whisper.tar.gz"), "rb").read() == open(os.path.join(two, "server-whisper.tar.gz"), "rb").read()


def test_a_folder_that_is_not_a_build_is_refused_and_nothing_is_written(tmp_path):
    out = tmp_path / "out"
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(package_whisper.PackageError, match="no whisper-server"):
        package_whisper.package(str(empty), str(out))
    not_elf = make_build(tmp_path / "text", server=b"#!/bin/sh\n")
    with pytest.raises(package_whisper.PackageError, match="not a Linux executable"):
        package_whisper.package(not_elf, str(out))
    assert not out.exists()


def test_a_build_without_a_licence_still_packages_the_server(tmp_path):
    build = make_build(tmp_path / "b", with_license=False)
    info = package_whisper.package(build, str(tmp_path / "out"))
    assert info["files"] == ["whisper-server"]


# --- the manifest ---------------------------------------------------------------------------------------------------

def make_release(tmp_path, pem, with_whisper=True, version="2.8.0"):
    archive = str(tmp_path / f"src-{version}.zip")
    release.build_source_archive(make_repo(tmp_path, version), archive)
    out, _info = pack(tmp_path, "whisper-assets")
    archive_w = os.path.join(out, "server-whisper.tar.gz")
    manifest = release.make_manifest(version, archive, "abc1234", BUILT_AT, "n",
                                     whisper_build=WHISPER_BUILD if with_whisper else None,
                                     server_whisper=archive_w if with_whisper else None)
    return archive, archive_w, release.sign_manifest(manifest, pem)


def test_the_manifest_signs_the_whisper_build_and_its_archive(tmp_path, pem):
    _archive, w, m = make_release(tmp_path, pem)
    assert m["whisper_build"] == WHISPER_BUILD
    assert m["server_whisper_sha256"] == release.sha256_file(w) and m["server_whisper_size"] == os.path.getsize(w)
    assert release.verify_manifest(m, release.public_key_hex(pem)) == (True, "ok")
    assert release.verify_server_whisper(m, w) == (True, "ok")
    assert "sd_build" not in m and "llama_build" not in m              # the voice engine asks nothing of the other fields


def test_a_release_without_it_is_unchanged(tmp_path, pem):
    _archive, _w, m = make_release(tmp_path, pem, with_whisper=False)
    assert not any(key in m for key in release.WHISPER_FIELDS)
    assert release.verify_manifest(m, release.public_key_hex(pem)) == (True, "ok")
    assert release.verify_server_whisper(m, "x")[0] is False


def test_a_node_that_does_not_know_the_fields_still_verifies_the_release(tmp_path, pem):
    """Older nodes check the signature over every field; the shape checks for the new ones are additive only."""
    _archive, _w, m = make_release(tmp_path, pem)
    assert release.verify_manifest(m, release.public_key_hex(pem))[0] is True
    assert {"whisper_build", "server_whisper_sha256", "server_whisper_size"} <= set(m)
    assert m["schema"] == release.MANIFEST_SCHEMA                        # no schema bump: nothing for an old node to refuse


@pytest.mark.parametrize("field, value", [("whisper_build", "other"), ("server_whisper_sha256", "1" * 64), ("server_whisper_size", 3)])
def test_changing_a_whisper_field_breaks_the_signature(tmp_path, pem, field, value):
    _a, _w, m = make_release(tmp_path, pem)
    ok, reason = release.verify_manifest(dict(m, **{field: value}), release.public_key_hex(pem))
    assert not ok and "signature" in reason


@pytest.mark.parametrize("patch, pattern", [
    ({"whisper_build": "bad build!"}, "whisper_build"), ({"whisper_build": ""}, "whisper_build"), ({"whisper_build": 5}, "whisper_build"),
    ({"server_whisper_sha256": "XYZ"}, "server_whisper_sha256"), ({"server_whisper_size": 0}, "server_whisper_size"),
    ({"server_whisper_size": True}, "server_whisper_size"), ({"server_whisper_size": "9"}, "server_whisper_size"),
])
def test_malformed_whisper_fields_are_rejected_by_shape(tmp_path, pem, patch, pattern):
    _a, _w, m = make_release(tmp_path, pem)
    ok, reason = release.verify_manifest(dict(m, **patch), release.public_key_hex(pem))
    assert not ok and pattern in reason


def test_half_of_the_whisper_fields_is_rejected(tmp_path, pem):
    _a, _w, m = make_release(tmp_path, pem)
    for missing in release.WHISPER_FIELDS:
        lone = {k: v for k, v in m.items() if k != missing}
        assert release.verify_manifest(lone, release.public_key_hex(pem))[0] is False


def test_the_whisper_archive_needs_its_build_and_the_build_its_archive(tmp_path):
    out, _ = pack(tmp_path)
    w = os.path.join(out, "server-whisper.tar.gz")
    with pytest.raises(release.ReleaseError, match="whisper build"):
        release.make_manifest("2.8.0", w, "a", BUILT_AT, server_whisper=w)
    with pytest.raises(release.ReleaseError, match="whisper build"):
        release.make_manifest("2.8.0", w, "a", BUILT_AT, whisper_build=WHISPER_BUILD)
    with pytest.raises(release.ReleaseError, match="whisper build"):
        release.make_manifest("2.8.0", w, "a", BUILT_AT, whisper_build="bad one", server_whisper=w)


def test_a_substituted_whisper_archive_is_caught(tmp_path, pem):
    _a, w, m = make_release(tmp_path, pem)
    open(w, "ab").write(b"x")
    ok, reason = release.verify_server_whisper(m, w)
    assert not ok and "size" in reason


# --- scripts/release.py ------------------------------------------------------------------------------------------------

def test_the_whisper_package_is_optional_unless_named(tmp_path):
    assert cli.load_whisper_assets(str(tmp_path / "absent")) is None
    with pytest.raises(cli.CliError, match="package_whisper"):
        cli.load_whisper_assets(str(tmp_path / "absent"), required=True)
    d, _ = pack(tmp_path)
    assert cli.load_whisper_assets(d) == {"whisper_build": WHISPER_BUILD, "server_whisper": os.path.join(d, "server-whisper.tar.gz")}
    os.remove(os.path.join(d, "server-whisper.tar.gz"))
    with pytest.raises(cli.CliError, match="incomplete"):
        cli.load_whisper_assets(d)


def test_the_cli_signs_whisper_when_it_is_packaged_and_builds_as_before_when_it_is_not(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "DEFAULT_WHISPER_ASSETS", str(tmp_path / "no-whisper-yet"))
    monkeypatch.setattr(cli, "DEFAULT_SD_ASSETS", str(tmp_path / "no-sd-yet"))
    bins = cli._args_binaries(cli.build_parser().parse_args(["build", "--binaries", packaged(tmp_path)]))
    assert bins["llama_build"] == BUILD and "whisper_build" not in bins

    d, _ = pack(tmp_path)
    monkeypatch.setattr(cli, "DEFAULT_WHISPER_ASSETS", d)
    bins = cli._args_binaries(cli.build_parser().parse_args(["build", "--binaries", packaged(tmp_path, "pkg2")]))
    assert bins["whisper_build"] == WHISPER_BUILD and bins["llama_build"] == BUILD


def test_no_binaries_leaves_the_voice_engine_out_too_and_a_named_missing_folder_is_an_error(tmp_path, monkeypatch):
    d, _ = pack(tmp_path)
    monkeypatch.setattr(cli, "DEFAULT_WHISPER_ASSETS", d)
    assert cli._args_binaries(cli.build_parser().parse_args(["build", "--no-binaries"])) is None
    with pytest.raises(cli.CliError, match="package_whisper"):
        cli._args_binaries(cli.build_parser().parse_args(
            ["build", "--binaries", packaged(tmp_path), "--whisper-assets", str(tmp_path / "nope")]))
    named = cli._args_binaries(cli.build_parser().parse_args(["build", "--binaries", packaged(tmp_path, "p3"), "--whisper-assets", d]))
    assert named["whisper_build"] == WHISPER_BUILD


def test_build_signs_and_verifies_the_whisper_archive(tmp_path, monkeypatch):
    key = _pem()
    monkeypatch.setattr(cli, "check_tree", lambda repo, allow_dirty: "abc1234")
    d, _ = pack(tmp_path)
    bins = {**cli.load_binaries(packaged(tmp_path)), **cli.load_whisper_assets(d)}
    built = cli.build_release(make_repo(tmp_path, "2.8.0"), str(tmp_path / "out"), key, "n",
                              expected_pubkey=release.public_key_hex(key), binaries=bins)
    m = json.load(open(built["manifest_path"]))
    assert release.verify_manifest(m, release.public_key_hex(key)) == (True, "ok")
    assert m["whisper_build"] == WHISPER_BUILD and release.verify_server_whisper(m, bins["server_whisper"]) == (True, "ok")
    assert m["llama_build"] == BUILD


def test_build_without_whisper_has_no_whisper_fields(tmp_path, monkeypatch):
    key = _pem()
    monkeypatch.setattr(cli, "check_tree", lambda repo, allow_dirty: "abc1234")
    built = cli.build_release(make_repo(tmp_path, "2.8.0"), str(tmp_path / "out"), key, "n",
                              expected_pubkey=release.public_key_hex(key), binaries=cli.load_binaries(packaged(tmp_path)))
    assert not any(k in built["manifest"] for k in release.WHISPER_FIELDS)


def test_deploy_uploads_and_verifies_the_whisper_archive(tmp_path, monkeypatch):
    remote = FakeRemote(tmp_path)
    d, _ = pack(tmp_path)
    _deploy(tmp_path, monkeypatch, remote, {**cli.load_binaries(packaged(tmp_path)), **cli.load_whisper_assets(d)})
    names = {u.rsplit("/", 1)[1] for u in remote.uploads}
    assert "server-whisper.tar.gz" in names and "server-llama.tar.gz" in names
    checked = {c.split()[1].rsplit("/", 1)[1] for c in remote.commands if c.startswith("sha256sum")}
    assert "server-whisper.tar.gz" in checked


def test_a_corrupted_whisper_upload_never_reaches_the_installer(tmp_path, monkeypatch):
    remote = FakeRemote(tmp_path, corrupt=("server-whisper.tar.gz",))
    d, _ = pack(tmp_path)
    with pytest.raises(cli.CliError, match="server-whisper.tar.gz arrived"):
        _deploy(tmp_path, monkeypatch, remote, {**cli.load_binaries(packaged(tmp_path)), **cli.load_whisper_assets(d)})
    assert not any("release_apply.py apply" in c for c in remote.commands)


def test_deploy_without_whisper_uploads_none(tmp_path, monkeypatch):
    remote = FakeRemote(tmp_path)
    _deploy(tmp_path, monkeypatch, remote, cli.load_binaries(packaged(tmp_path)))
    assert not any(u.endswith("server-whisper.tar.gz") for u in remote.uploads)


# --- scripts/release_apply.py ----------------------------------------------------------------------------------------

def whisper_release(tmp_path, pem, host, version="2.8.0", marker="v1"):
    """A release signing the llama.cpp binaries and a whisper archive made with ``marker``."""
    archive, zips, srv, _m = signed_release(tmp_path, pem, version=version, marker=marker)
    out, _info = pack(tmp_path, f"w-{marker}", marker=marker)
    w = os.path.join(out, "server-whisper.tar.gz")
    manifest = release.sign_manifest(release.make_manifest(
        version, archive, "abc1234", BUILT_AT, "n", rpc_zips=zips, llama_build=BUILD, rpc_proto="8.0.1", server_llama=srv,
        whisper_build=WHISPER_BUILD, server_whisper=w), pem)
    incoming = assets_dir(tmp_path, zips, srv)
    with open(os.path.join(incoming, "server-whisper.tar.gz"), "wb") as handle:
        handle.write(open(w, "rb").read())
    return archive, manifest, incoming, w


def test_the_installer_unpacks_the_voice_engine_into_bin_whisper_and_nowhere_else(tmp_path, pem, host):
    archive, m, incoming, _w = whisper_release(tmp_path, pem, host)
    run_apply(host, archive, m, assets_dir=incoming)
    folder = os.path.join(host, "bin-whisper")
    assert sorted(os.listdir(folder)) == sorted([".installed-from", "whisper-server", "LICENSE"])
    assert open(os.path.join(folder, "whisper-server"), "rb").read() == b"\x7fELF whisper-server v1"
    assert os.stat(os.path.join(folder, "whisper-server")).st_mode & stat.S_IXUSR
    assert open(os.path.join(folder, ".installed-from")).read().strip() == m["server_whisper_sha256"]
    assert "whisper-server" not in os.listdir(os.path.join(host, "bin"))          # llama.cpp's folder is not touched
    assert not os.path.exists(os.path.join(host, "bin-sd"))
    assert not [n for n in os.listdir(host) if ".partial" in n or ".old-" in n]


def test_a_release_that_signs_the_same_engine_again_needs_no_new_upload(tmp_path, pem, host):
    archive, m, incoming, w = whisper_release(tmp_path, pem, host)
    run_apply(host, archive, m, assets_dir=incoming)
    inode = os.stat(os.path.join(host, "bin-whisper", "whisper-server")).st_ino
    archive2, zips, srv, _m = signed_release(tmp_path, pem, version="2.8.1", name="bins2")
    m2 = release.sign_manifest(release.make_manifest(
        "2.8.1", archive2, "abc1234", BUILT_AT, "n", rpc_zips=zips, llama_build=BUILD, rpc_proto="8.0.1", server_llama=srv,
        whisper_build=WHISPER_BUILD, server_whisper=w), pem)
    run_apply(host, archive2, m2, assets_dir=assets_dir(tmp_path, zips, srv))
    assert os.stat(os.path.join(host, "bin-whisper", "whisper-server")).st_ino == inode          # not unpacked again


def test_a_new_engine_replaces_the_old_one_whole(tmp_path, pem, host):
    archive, m, incoming, _ = whisper_release(tmp_path, pem, host, marker="v1")
    run_apply(host, archive, m, assets_dir=incoming)
    open(os.path.join(host, "bin-whisper", "stale.so"), "w").write("left over")
    archive2, m2, incoming2, _ = whisper_release(tmp_path, pem, host, version="2.8.1", marker="v2")
    run_apply(host, archive2, m2, assets_dir=incoming2)
    assert open(os.path.join(host, "bin-whisper", "whisper-server"), "rb").read() == b"\x7fELF whisper-server v2"
    assert "stale.so" not in os.listdir(os.path.join(host, "bin-whisper"))


def test_a_signed_engine_that_was_not_uploaded_and_is_not_installed_stops_the_install_before_it_starts(tmp_path, pem, host):
    archive, m, incoming, _ = whisper_release(tmp_path, pem, host)
    os.remove(os.path.join(incoming, "server-whisper.tar.gz"))
    before = apply_mod.installed_version(host)
    with pytest.raises(apply_mod.ApplyError, match="voice engine"):
        run_apply(host, archive, m, assets_dir=incoming)
    assert apply_mod.installed_version(host) == before and not os.path.exists(os.path.join(host, "bin-whisper"))


def test_a_whisper_archive_the_manifest_does_not_sign_is_refused(tmp_path, pem, host):
    archive, zips, srv, m = signed_release(tmp_path, pem)
    incoming = assets_dir(tmp_path, zips, srv)
    out, _ = pack(tmp_path)
    open(os.path.join(incoming, "server-whisper.tar.gz"), "wb").write(open(os.path.join(out, "server-whisper.tar.gz"), "rb").read())
    with pytest.raises(apply_mod.ApplyError, match="does not sign it"):
        run_apply(host, archive, m, assets_dir=incoming)


def test_a_whisper_archive_that_is_not_the_signed_one_is_refused(tmp_path, pem, host):
    archive, m, incoming, _ = whisper_release(tmp_path, pem, host)
    with open(os.path.join(incoming, "server-whisper.tar.gz"), "ab") as handle:
        handle.write(b"tampered")
    with pytest.raises(apply_mod.ApplyError, match="voice engine archive"):
        run_apply(host, archive, m, assets_dir=incoming)
    assert not os.path.exists(os.path.join(host, "bin-whisper"))


def evil_tarball(path, entries):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for info, data in entries:
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    with open(path, "wb") as handle:
        handle.write(gzip.compress(raw.getvalue()))
    return str(path)


def member(name, size=1, kind=tarfile.REGTYPE, link=""):
    info = tarfile.TarInfo(name)
    info.size, info.type, info.linkname = size, kind, link
    return info


@pytest.mark.parametrize("entries, label", [
    ([(member("whisper-server"), b"x"), (member("../escape.so"), b"x")], "parent directory"),
    ([(member("whisper-server"), b"x"), (member("sub/lib.so"), b"x")], "a folder"),
    ([(member("whisper-server"), b"x"), (member("/abs.so"), b"x")], "absolute"),
    ([(member("whisper-server"), b"x"), (member("link.so", 0, tarfile.SYMTYPE, "/etc/passwd"), None)], "symlink"),
    ([(member(".hidden"), b"x"), (member("whisper-server"), b"x")], "hidden"),
    ([(member("LICENSE"), b"x")], "no whisper-server"),
])
def test_an_unsafe_whisper_archive_is_refused_and_the_installed_engine_is_left_alone(tmp_path, label, entries):
    root = tmp_path / "root"
    (root / "bin-whisper").mkdir(parents=True)
    (root / "bin-whisper" / "whisper-server").write_text("installed")
    (root / "bin-whisper" / ".installed-from").write_text("old\n")
    bad = evil_tarball(tmp_path / "evil.tar.gz", entries)
    with pytest.raises(apply_mod.ApplyError):
        apply_mod.install_server_whisper(str(root), bad, "f" * 64)
    assert (root / "bin-whisper" / "whisper-server").read_text() == "installed"
    assert not (tmp_path / "escape.so").exists()
    assert sorted(os.listdir(root)) == ["bin-whisper"]                       # no staging folder left behind


def test_rolling_back_does_not_touch_the_voice_engine(tmp_path, pem, host):
    archive, m, incoming, _ = whisper_release(tmp_path, pem, host)
    run_apply(host, archive, m, assets_dir=incoming)
    rec = Recorder()
    apply_mod.rollback_release(host, None, rec.restart, rec.health, log=lambda _m: None)
    assert open(os.path.join(host, "bin-whisper", "whisper-server"), "rb").read() == b"\x7fELF whisper-server v1"


def test_workers_and_the_downloaders_know_nothing_of_the_voice_engine():
    for name in ("worker.py", "downloader.py", "setup_core.py", "app_member.py", "registry.py"):
        source = open(os.path.join(REPO_ROOT, "engine", name), encoding="utf-8").read()
        for word in ("bin-whisper", "server-whisper", "server_whisper", "whisper_build", "whisper-server"):
            assert word not in source, (name, word)


# --- the model pin and the downloader -------------------------------------------------------------------------------

def test_the_pin_is_the_turbo_q8_0_file_by_size_and_sha256_at_a_fixed_revision():
    pins = fetch.load_pins()
    assert [p["role"] for p in pins] == ["whisper"]
    pin = pins[0]
    assert pin["file"] == "ggml-large-v3-turbo-q8_0.bin"
    assert (pin["size"], pin["sha256"]) == (874188075, "317eb69c11673c9de1e1f0d459b253999804ec71ac4c23c17ecf5fbe24e259a1")
    assert pin["url"] == ("https://huggingface.co/ggerganov/whisper.cpp/resolve/"
                          "5359861c739e955e79d9a303bcbc70fb988958b1/ggml-large-v3-turbo-q8_0.bin")


def test_the_measurements_ride_in_the_pin_file_comment():
    comment = json.load(open(os.path.join(REPO_ROOT, "scripts", "voice_models.json"), encoding="utf-8"))["_comment"]
    assert "RX 580" in comment and "VOICE_MIN_FREE_VRAM_GB" in comment and "MEASUREMENTS_PLACEHOLDER" not in comment


DATA = bytes(range(256)) * 40            # 10,240 bytes


def pin_for(data=DATA, name="m.bin"):
    return {"role": "whisper", "file": name, "url": "https://example.test/m.bin", "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data)}


class Response:
    def __init__(self, data, status=200):
        self.status, self._io = status, io.BytesIO(data)

    def read(self, n=-1):
        return self._io.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Server:
    """urlopen: serves ``data``, honouring a Range header."""

    def __init__(self, data=DATA):
        self.data, self.requests = data, []

    def __call__(self, request, timeout=None):
        range_header = request.headers.get("Range")
        self.requests.append(range_header)
        if range_header:
            return Response(self.data[int(range_header.split("=")[1].split("-")[0]):], 206)
        return Response(self.data)


def test_a_good_download_is_verified_and_moved_into_place(tmp_path):
    path = fetch.fetch_one(pin_for(), str(tmp_path), Server(), log=lambda m: None)
    assert open(path, "rb").read() == DATA and os.listdir(tmp_path) == ["m.bin"]


def test_a_file_already_in_place_and_right_is_not_downloaded_again(tmp_path):
    (tmp_path / "m.bin").write_bytes(DATA)
    server = Server()
    fetch.fetch_one(pin_for(), str(tmp_path), server, log=lambda m: None)
    assert server.requests == []


def test_an_interrupted_download_continues_where_it_stopped(tmp_path):
    (tmp_path / "m.bin.part").write_bytes(DATA[:4000])
    server = Server()
    fetch.fetch_one(pin_for(), str(tmp_path), server, log=lambda m: None)
    assert server.requests == ["bytes=4000-"] and (tmp_path / "m.bin").read_bytes() == DATA


def test_a_download_with_the_wrong_hash_is_deleted_and_refused(tmp_path):
    with pytest.raises(fetch.FetchError, match="SHA-256"):
        fetch.fetch_one(pin_for(), str(tmp_path), Server(bytes(reversed(DATA))), log=lambda m: None)
    assert os.listdir(tmp_path) == []


def test_a_download_of_the_wrong_size_is_never_moved_into_place(tmp_path):
    with pytest.raises(fetch.FetchError, match="expected 10,240"):
        fetch.fetch_one(pin_for(), str(tmp_path), Server(DATA[:5000]), log=lambda m: None)
    assert not (tmp_path / "m.bin").exists() and (tmp_path / "m.bin.part").stat().st_size == 5000


def test_a_wrong_file_already_in_place_is_replaced_not_trusted(tmp_path):
    (tmp_path / "m.bin").write_bytes(b"not the model")
    fetch.fetch_one(pin_for(), str(tmp_path), Server(), log=lambda m: None)
    assert (tmp_path / "m.bin").read_bytes() == DATA


def test_the_script_defaults_to_models_voice_under_the_install(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch, "load_pins", lambda: [pin_for()])
    monkeypatch.setattr(fetch, "fetch_one", lambda pin, directory: fetch._img.fetch_one(pin, directory, Server(), log=lambda m: None))
    import engine.config as cfg
    monkeypatch.setattr(cfg, "get_base_dir", lambda: str(tmp_path))
    assert fetch.main([]) == 0
    assert (tmp_path / "models" / "voice" / "m.bin").read_bytes() == DATA


def test_the_script_reports_a_bad_download_and_exits_nonzero(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(fetch, "load_pins", lambda: [pin_for()])
    monkeypatch.setattr(fetch, "fetch_one", lambda pin, directory: fetch._img.fetch_one(
        pin, directory, Server(bytes(reversed(DATA))), log=lambda m: None))
    assert fetch.main(["--dir", str(tmp_path)]) == 1
    assert "SHA-256" in capsys.readouterr().err
