"""
The llama.cpp binaries a release ships: one ggml-rpc-server zip per worker platform and the
build the coordinator itself runs. They are executables, so the signed release names every one
(size, SHA-256, the upstream build and its RPC protocol) and every party checks them: the
release library, the packager, the operator CLI, the box-side installer (which also switches
the coordinator's own build, and puts it back on rollback), the coordinator (which routes only
to workers speaking its protocol), and the worker that downloads them.
"""

import hashlib
import importlib.util
import io
import json
import os
import tarfile
import zipfile

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import ASGITransport, AsyncClient

from engine import coordinator, downloader, release
from engine.registry import WorkerRegistry

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILT_AT = "2026-10-25T12:00:00Z"
BUILD, PROTO = "b11838", "8.0.1"


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(REPO_ROOT, "scripts", filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


apply_mod = _load("release_apply_bins", "release_apply.py")
cli = _load("release_cli_bins", "release.py")
packager = _load("package_llama_bins", "package_llama.py")


def _pem():
    return Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


def make_rpc_zip(path, members=None, marker="v1"):
    members = members if members is not None else {
        "bin/ggml-rpc-server": f"rpc {marker}".encode(),
        "bin/libggml-base.so.0": b"lib",
        "bin/libggml-vulkan.so": b"vk",
    }
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return str(path)


def make_server_tarball(path, build=BUILD, marker="new", extra=None):
    """An upstream-shaped build: llama-<build>/ with llama-server, ggml-rpc-server and a symlinked lib."""
    with tarfile.open(path, "w:gz") as tar:
        def add(name, data):
            info = tarfile.TarInfo(f"llama-{build}/{name}")
            info.size = len(data)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(data))
        add("llama-server", f"server {marker}".encode())
        add("ggml-rpc-server", f"rpc {marker}".encode())
        add("libggml.so.0.27.0", b"real lib")
        link = tarfile.TarInfo(f"llama-{build}/libggml.so.0")
        link.type = tarfile.SYMTYPE
        link.linkname = "libggml.so.0.27.0"
        tar.addfile(link)
        for info, data in (extra or []):
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    return str(path)


def make_repo(base, version):
    root = base / f"repo-{version}"
    (root / "engine").mkdir(parents=True, exist_ok=True)
    (root / "engine" / "version.py").write_text(f'VERSION = "{version}"\n')
    (root / "dlne_server.py").write_text(f"print('{version}')\n")
    (root / "requirements.txt").write_text("fastapi\n")
    return str(root)


def binaries(tmp_path, marker="v1", platforms=release.RPC_PLATFORMS, server=True, name="bins"):
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    zips = {p: make_rpc_zip(d / release.rpc_zip_name(p), marker=f"{p}-{marker}") for p in platforms}
    srv = make_server_tarball(d / f"llama-{BUILD}-bin-ubuntu-vulkan-x64.tar.gz", marker=marker) if server else None
    return zips, srv


def signed_release(tmp_path, pem, version="2.1.0", marker="v1", with_binaries=True, name="bins"):
    archive = str(tmp_path / f"src-{version}.zip")
    release.build_source_archive(make_repo(tmp_path, version), archive)
    zips, srv = binaries(tmp_path, marker, name=name) if with_binaries else ({}, None)
    manifest = release.make_manifest(version, archive, "abc1234", BUILT_AT, "n",
                                     rpc_zips=zips or None, llama_build=BUILD if with_binaries else None,
                                     rpc_proto=PROTO if with_binaries else None, server_llama=srv)
    return archive, zips, srv, release.sign_manifest(manifest, pem)


@pytest.fixture
def pem():
    return _pem()


# --- the release library ------------------------------------------------------------------

def test_the_manifest_signs_every_binary_with_its_build_and_protocol(tmp_path, pem):
    _a, zips, srv, m = signed_release(tmp_path, pem)
    assert (m["llama_build"], m["rpc_proto"]) == (BUILD, PROTO)
    for platform, path in zips.items():
        assert m[release.rpc_field(platform, "sha256")] == release.sha256_file(path)
        assert release.verify_rpc_bin(m, platform, path) == (True, "ok")
    assert release.verify_server_llama(m, srv) == (True, "ok")
    assert release.verify_manifest(m, release.public_key_hex(pem)) == (True, "ok")


def test_a_manifest_without_binaries_is_still_valid(tmp_path, pem):
    _a, _z, _s, m = signed_release(tmp_path, pem, with_binaries=False)
    assert release.verify_manifest(m, release.public_key_hex(pem)) == (True, "ok")
    assert release.verify_rpc_bin(m, "linux-x64", "x")[0] is False


@pytest.mark.parametrize("field,value", [
    ("rpc_linux_x64_sha256", "1" * 64), ("rpc_win_x64_size", 7), ("server_llama_sha256", "2" * 64),
    ("llama_build", "b1"), ("rpc_proto", "9.0.0"),
])
def test_changing_any_binary_field_breaks_the_signature(tmp_path, pem, field, value):
    _a, _z, _s, m = signed_release(tmp_path, pem)
    ok, reason = release.verify_manifest(dict(m, **{field: value}), release.public_key_hex(pem))
    assert not ok and "signature" in reason


@pytest.mark.parametrize("patch,pattern", [
    ({"rpc_macos_arm64_sha256": "XYZ"}, "rpc_macos_arm64_sha256"),
    ({"rpc_linux_x64_size": 0}, "rpc_linux_x64_size"),
    ({"server_llama_size": True}, "server_llama_size"),
    ({"llama_build": "latest"}, "llama_build"),
    ({"rpc_proto": "8"}, "rpc_proto"),
])
def test_malformed_binary_fields_are_rejected_by_shape(tmp_path, pem, patch, pattern):
    _a, _z, _s, m = signed_release(tmp_path, pem)
    ok, reason = release.verify_manifest(dict(m, **patch), release.public_key_hex(pem))
    assert not ok and pattern in reason


def test_binaries_without_a_build_or_protocol_cannot_be_released(tmp_path):
    zips, _ = binaries(tmp_path, server=False)
    with pytest.raises(release.ReleaseError, match="build"):
        release.make_manifest("2.1.0", zips["linux-x64"], "a", BUILT_AT, rpc_zips=zips, rpc_proto=PROTO)
    with pytest.raises(release.ReleaseError, match="protocol"):
        release.make_manifest("2.1.0", zips["linux-x64"], "a", BUILT_AT, rpc_zips=zips, llama_build=BUILD)
    with pytest.raises(release.ReleaseError, match="Unknown RPC platform"):
        release.make_manifest("2.1.0", zips["linux-x64"], "a", BUILT_AT, rpc_zips={"amiga": zips["linux-x64"]},
                              llama_build=BUILD, rpc_proto=PROTO)


def test_half_a_binary_pair_is_rejected(tmp_path, pem):
    _a, _z, _s, m = signed_release(tmp_path, pem)
    lone = {k: v for k, v in m.items() if k != "rpc_win_x64_size"}
    assert release.verify_manifest(lone, release.public_key_hex(pem))[0] is False


def test_a_substituted_zip_is_caught(tmp_path, pem):
    _a, zips, _s, m = signed_release(tmp_path, pem)
    path = zips["linux-x64"]
    data = bytearray(open(path, "rb").read())
    data[-1] ^= 1
    open(path, "wb").write(bytes(data))
    assert release.verify_rpc_bin(m, "linux-x64", path) == (False, "the RPC archive hash does not match the manifest")
    assert release.verify_rpc_bin(m, "win-x64", zips["linux-x64"])[0] is False   # wrong platform's zip


@pytest.mark.parametrize("name,ok", [
    ("bin/ggml-rpc-server", True), ("bin/ggml-rpc-server.exe", True), ("bin/libggml-base.so.0.27.0", True),
    ("bin/ggml-vulkan.dll", True), ("bin/libggml-metal.0.dylib", True), ("bin/libomp.dll", True), ("bin/LICENSE", True),
    ("bin/evil.exe", False), ("ggml-rpc-server", False), ("bin/sub/ggml.dll", False), ("../bin/ggml.dll", False),
    ("bin/ggml.dll.lnk", False),
    # llama-server and exactly its own libraries ship too (the CPU pool, 2.3.0) ...
    ("bin/llama-server", True), ("bin/llama-server.exe", True), ("bin/libllama.so.0.7.0", True),
    ("bin/libllama-common.so.0", True), ("bin/libllama-server-impl.so", True), ("bin/mtmd.dll", True),
    ("bin/libmtmd.0.dylib", True), ("bin/llama-common.dll", True),
    # ... but no other llama.cpp tool.
    ("bin/llama-cli", False), ("bin/libllama-cli-impl.so", False), ("bin/libllama-bench-impl.so", False),
    ("bin/llama.exe", False), ("bin/llama-server.sh", False),
])
def test_what_an_rpc_zip_may_contain(name, ok):
    assert (release.rpc_members([name]) == []) is ok


def test_an_rpc_zip_needs_the_server(tmp_path):
    path = make_rpc_zip(tmp_path / "x.zip", {"bin/libggml.so": b"x"})
    with pytest.raises(release.ReleaseError, match="no bin/ggml-rpc-server"):
        release.check_rpc_bin(path)
    bad = tmp_path / "junk.zip"
    bad.write_bytes(b"nope")
    with pytest.raises(release.ReleaseError, match="not a readable zip"):
        release.check_rpc_bin(str(bad))


@pytest.mark.parametrize("system,machine,expected", [
    ("Windows", "AMD64", "win-x64"), ("Linux", "x86_64", "linux-x64"), ("Darwin", "arm64", "macos-arm64"),
    # Added with CPU-only contribution in 2.1.3.
    ("Darwin", "x86_64", "macos-x64"), ("Linux", "aarch64", "linux-arm64"), ("Windows", "ARM64", "win-arm64"),
    ("FreeBSD", "amd64", None), ("Linux", "riscv64", None), ("Linux", "s390x", None),
])
def test_host_platforms(system, machine, expected):
    assert release.host_rpc_platform(system, machine) == expected


def test_protocol_majors():
    assert release.proto_major("8.0.1") == 8
    assert release.proto_major("5.0.0") == 5
    for bad in ("8", "v8.0.1", None, 8, "8.0.x"):
        assert release.proto_major(bad) is None


# --- the packager -------------------------------------------------------------------------

def test_packaging_resolves_symlinks_and_keeps_only_the_two_servers_and_their_libraries(tmp_path):
    tarball = make_server_tarball(tmp_path / "u.tar.gz")
    members = dict(packager._read_members(tarball))
    assert members["libggml.so.0"] == b"real lib"          # the symlink, as a copy of its target
    assert "llama-server" in members                        # the CPU pool's server (2.3.0)
    assert members["ggml-rpc-server"] == b"rpc new"
    assert not any(name.startswith(("llama-cli", "libllama-cli")) for name in members)


def test_packaged_zips_are_deterministic_and_executable(tmp_path):
    members = [("ggml-rpc-server", b"exe"), ("libggml.so", b"lib"), ("llama-server", b"exe2")]
    a, b = str(tmp_path / "a.zip"), str(tmp_path / "b.zip")
    packager.write_rpc_zip(members, a)
    packager.write_rpc_zip(members, b)
    assert open(a, "rb").read() == open(b, "rb").read()
    with zipfile.ZipFile(a) as z:
        modes = {i.filename: (i.external_attr >> 16) & 0o777 for i in z.infolist()}
    assert modes == {"bin/ggml-rpc-server": 0o755, "bin/libggml.so": 0o644, "bin/llama-server": 0o755}


def test_an_upstream_archive_without_the_rpc_server_is_refused(tmp_path):
    path = tmp_path / "w.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("llama-server.exe", b"x")
    with pytest.raises(packager.PackageError, match="no ggml-rpc-server"):
        packager._read_members(str(path))


# --- the box-side installer ---------------------------------------------------------------

@pytest.fixture
def host(tmp_path):
    """A coordinator at 2.0.0 whose bin/ is a link to an older llama.cpp build somewhere else."""
    root = tmp_path / "box"
    (root / "engine").mkdir(parents=True)
    (root / "engine" / "version.py").write_text('VERSION = "2.0.0"\n')
    (root / "dlne_server.py").write_text("old\n")
    (root / "requirements.txt").write_text("fastapi\n")
    old_build = tmp_path / "shared" / "llama-b10451"
    old_build.mkdir(parents=True)
    (old_build / "llama-server").write_text("server old")
    os.symlink(str(old_build), str(root / "bin"))
    return str(root)


class Recorder:
    def __init__(self, healthy=True):
        self.restarts, self.healthy, self.bins_at_health = 0, healthy, []

    def restart(self):
        self.restarts += 1

    def health(self, _version):
        return (self.healthy, "fine" if self.healthy else "not healthy")


def run_apply(host, archive, manifest, rec=None, **kw):
    rec = rec or Recorder()
    return apply_mod.apply_release(host, archive, manifest, rec.restart, rec.health, log=lambda _m: None, **kw)


def assets_dir(tmp_path, zips, srv):
    """Lay the binaries out the way scripts/release.py uploads them."""
    d = tmp_path / "incoming"
    d.mkdir(exist_ok=True)
    for platform, path in zips.items():
        (d / release.rpc_zip_name(platform)).write_bytes(open(path, "rb").read())
    if srv:
        (d / apply_mod.SERVER_LLAMA_UPLOAD).write_bytes(open(srv, "rb").read())
    return str(d)


def server_binary(host):
    return open(os.path.join(host, "bin", "llama-server")).read()


def test_the_installer_installs_the_zips_and_switches_the_server_build(tmp_path, pem, host):
    archive, zips, srv, m = signed_release(tmp_path, pem)
    run_apply(host, archive, m, assets_dir=assets_dir(tmp_path, zips, srv))
    for platform, path in zips.items():
        assert open(os.path.join(host, release.rpc_zip_name(platform)), "rb").read() == open(path, "rb").read()
    assert server_binary(host) == "server v1"
    assert os.readlink(os.path.join(host, "bin")) == os.path.join(host, "llama", BUILD, f"llama-{BUILD}")
    assert os.path.islink(os.path.join(host, "llama", BUILD, f"llama-{BUILD}", "libggml.so.0"))


def test_the_old_build_directory_is_never_touched(tmp_path, pem, host):
    archive, zips, srv, m = signed_release(tmp_path, pem)
    run_apply(host, archive, m, assets_dir=assets_dir(tmp_path, zips, srv))
    assert open(tmp_path / "shared" / "llama-b10451" / "llama-server").read() == "server old"


def test_rolling_back_puts_the_previous_server_build_back(tmp_path, pem, host):
    old_target = os.readlink(os.path.join(host, "bin"))
    archive, zips, srv, m = signed_release(tmp_path, pem)
    run_apply(host, archive, m, assets_dir=assets_dir(tmp_path, zips, srv))
    rec = Recorder()
    apply_mod.rollback_release(host, None, rec.restart, rec.health, log=lambda _m: None)
    assert os.readlink(os.path.join(host, "bin")) == old_target
    assert server_binary(host) == "server old"
    for platform in release.RPC_PLATFORMS:      # exactly the 2.0.0 state, which served none
        assert not os.path.exists(os.path.join(host, release.rpc_zip_name(platform)))


def test_a_backup_made_by_the_old_installer_keeps_the_zips_and_the_build(tmp_path, pem, host):
    """Backups from before binaries were managed record neither the zips nor bin/: leave both."""
    archive, zips, srv, m = signed_release(tmp_path, pem)
    result = run_apply(host, archive, m, assets_dir=assets_dir(tmp_path, zips, srv))
    meta_path = os.path.join(host, ".releases", result["backup"], "backup.json")
    meta = json.load(open(meta_path))
    del meta["owns"], meta["bin_target"]
    json.dump(meta, open(meta_path, "w"))
    apply_mod.restore_backup(host, os.path.dirname(meta_path))
    assert apply_mod.installed_version(host) == "2.0.0"
    assert server_binary(host) == "server v1"
    for platform in release.RPC_PLATFORMS:
        assert os.path.exists(os.path.join(host, release.rpc_zip_name(platform)))


def test_an_unhealthy_install_restores_the_previous_build(tmp_path, pem, host):
    old_target = os.readlink(os.path.join(host, "bin"))
    archive, zips, srv, m = signed_release(tmp_path, pem)
    with pytest.raises(apply_mod.ApplyError, match="Rolled back"):
        run_apply(host, archive, m, Recorder(healthy=False), assets_dir=assets_dir(tmp_path, zips, srv))
    assert os.readlink(os.path.join(host, "bin")) == old_target
    assert apply_mod.installed_version(host) == "2.0.0"


def test_a_tampered_zip_is_refused_before_anything_changes(tmp_path, pem, host):
    archive, zips, srv, m = signed_release(tmp_path, pem)
    d = assets_dir(tmp_path, zips, srv)
    path = os.path.join(d, release.rpc_zip_name("macos-arm64"))
    data = bytearray(open(path, "rb").read())
    data[-1] ^= 1
    open(path, "wb").write(bytes(data))
    rec = Recorder()
    with pytest.raises(apply_mod.ApplyError, match="macos-arm64 RPC archive hash"):
        run_apply(host, archive, m, rec, assets_dir=d)
    assert rec.restarts == 0 and server_binary(host) == "server old"
    assert not os.path.exists(os.path.join(host, "llama"))


def test_a_tampered_server_build_is_refused(tmp_path, pem, host):
    archive, zips, srv, m = signed_release(tmp_path, pem)
    d = assets_dir(tmp_path, zips, srv)
    make_server_tarball(os.path.join(d, apply_mod.SERVER_LLAMA_UPLOAD), marker="evil")
    with pytest.raises(apply_mod.ApplyError, match="server llama.cpp build"):
        run_apply(host, archive, m, assets_dir=d)
    assert server_binary(host) == "server old"


@pytest.mark.parametrize("bad", ["traversal", "absolute_link", "device"])
def test_a_server_build_with_unsafe_entries_is_refused(tmp_path, pem, host, bad):
    if bad == "traversal":
        info = tarfile.TarInfo("llama-b11838/../../evil")
        info.size = 1
        extra = [(info, b"x")]
    elif bad == "absolute_link":
        info = tarfile.TarInfo("llama-b11838/escape")
        info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
        extra = [(info, None)]
    else:
        info = tarfile.TarInfo("llama-b11838/dev")
        info.type = tarfile.CHRTYPE
        extra = [(info, None)]
    archive = str(tmp_path / "src.zip")
    release.build_source_archive(make_repo(tmp_path, "2.1.0"), archive)
    zips, _ = binaries(tmp_path, server=False)
    srv = make_server_tarball(tmp_path / "unsafe.tar.gz", extra=extra)
    m = release.sign_manifest(release.make_manifest("2.1.0", archive, "a", BUILT_AT, rpc_zips=zips, llama_build=BUILD,
                                                    rpc_proto=PROTO, server_llama=srv), pem)
    with pytest.raises(apply_mod.ApplyError, match="unsafe"):
        run_apply(host, archive, m, assets_dir=assets_dir(tmp_path, zips, srv))
    assert server_binary(host) == "server old"
    assert not os.path.exists(tmp_path / "evil")


def test_an_installed_build_need_not_be_uploaded_again(tmp_path, pem, host):
    archive, zips, srv, m = signed_release(tmp_path, pem)
    run_apply(host, archive, m, assets_dir=assets_dir(tmp_path, zips, srv))
    archive2 = str(tmp_path / "src2.zip")
    release.build_source_archive(make_repo(tmp_path, "2.1.1"), archive2)
    m2 = dict(m)
    m2.update(version="2.1.1", sha256=release.sha256_file(archive2), size=os.path.getsize(archive2))
    m2 = release.sign_manifest({k: v for k, v in m2.items() if k != "signature"}, pem)
    run_apply(host, archive2, m2)                            # no assets: zips and build already there
    assert apply_mod.installed_version(host) == "2.1.1" and server_binary(host) == "server v1"


def test_a_signed_build_that_is_neither_uploaded_nor_installed_is_refused(tmp_path, pem, host):
    archive, zips, srv, m = signed_release(tmp_path, pem)
    d = assets_dir(tmp_path, zips, None)
    with pytest.raises(apply_mod.ApplyError, match="not uploaded and is not installed"):
        run_apply(host, archive, m, assets_dir=d)


def test_binaries_the_manifest_does_not_sign_are_refused(tmp_path, pem, host):
    archive, _z, _s, m = signed_release(tmp_path, pem, with_binaries=False)
    zips, srv = binaries(tmp_path, name="unsigned")
    with pytest.raises(apply_mod.ApplyError, match="does not sign it"):
        run_apply(host, archive, m, assets_dir=assets_dir(tmp_path, {}, srv))


def test_a_real_bin_directory_is_moved_aside_never_deleted(tmp_path, pem):
    root = tmp_path / "box2"
    (root / "engine").mkdir(parents=True)
    (root / "engine" / "version.py").write_text('VERSION = "2.0.0"\n')
    (root / "bin").mkdir()
    (root / "bin" / "llama-server").write_text("hand-installed")
    archive, zips, srv, m = signed_release(tmp_path, pem)
    run_apply(str(root), archive, m, assets_dir=assets_dir(tmp_path, zips, srv))
    aside = [d for d in os.listdir(root / "llama") if d.startswith("previous-bin-")]
    assert len(aside) == 1 and (root / "llama" / aside[0] / "llama-server").read_text() == "hand-installed"


def test_health_checks_every_served_zip(tmp_path, pem, monkeypatch):
    archive, zips, _srv, m = signed_release(tmp_path, pem)
    served = {
        "/v1/cluster/status": json.dumps({"server_version": "2.1.0"}).encode(),
        "/v1/release": json.dumps(m).encode(),
        "/v1/download/source": open(archive, "rb").read(),
    }
    for platform, path in zips.items():
        served[f"/v1/download/rpc-bin/{platform}"] = open(path, "rb").read()
    monkeypatch.setattr(apply_mod, "_http_get", lambda url, timeout=5.0: served[url.split("http://box", 1)[1]])
    health = apply_mod.http_health("http://box", lambda _v: m, timeout=0.2, poll=0.01, sleep=lambda _s: None)
    assert health("2.1.0") == (True, "up, serving the installed release")
    served["/v1/download/rpc-bin/linux-x64"] = b"other"
    ok, detail = health("2.1.0")
    assert not ok and "linux-x64" in detail


def test_the_command_line_passes_the_assets_through(tmp_path, pem, host, monkeypatch):
    archive, zips, srv, m = signed_release(tmp_path, pem)
    manifest_path = tmp_path / "release.json"
    manifest_path.write_text(json.dumps(m))
    rec = Recorder()
    monkeypatch.setattr(apply_mod, "systemd_restart", lambda unit, user=True: rec.restart)
    monkeypatch.setattr(apply_mod, "http_health", lambda url, mf, timeout=0: rec.health)
    assert apply_mod.main(["apply", "--root", host, "--archive", archive, "--manifest", str(manifest_path),
                           "--assets", assets_dir(tmp_path, zips, srv)]) == 0
    assert server_binary(host) == "server v1"


# --- the operator CLI ---------------------------------------------------------------------

def packaged(tmp_path, name="pkg"):
    d = tmp_path / name
    d.mkdir()
    for platform in release.RPC_PLATFORMS:
        make_rpc_zip(d / release.rpc_zip_name(platform), marker=platform)
    make_server_tarball(d / "server.tar.gz")
    (d / "build.json").write_text(json.dumps({"llama_build": BUILD, "rpc_proto": PROTO, "server_llama": "server.tar.gz"}))
    return str(d)


def test_build_signs_the_packaged_binaries(tmp_path, monkeypatch):
    pem = _pem()
    monkeypatch.setattr(cli, "check_tree", lambda repo, allow_dirty: "abc1234")
    built = cli.build_release(make_repo(tmp_path, "2.1.0"), str(tmp_path / "out"), pem, "n",
                              expected_pubkey=release.public_key_hex(pem), binaries=cli.load_binaries(packaged(tmp_path)))
    m = json.load(open(built["manifest_path"]))
    assert release.verify_manifest(m, release.public_key_hex(pem)) == (True, "ok")
    assert m["llama_build"] == BUILD and all(release.rpc_field(p, "sha256") in m for p in release.RPC_PLATFORMS)


def test_an_incomplete_package_is_refused_and_named(tmp_path):
    d = packaged(tmp_path)
    os.remove(os.path.join(d, release.rpc_zip_name("macos-arm64")))
    with pytest.raises(cli.CliError, match="rpc-bin-macos-arm64.zip"):
        cli.load_binaries(d)
    with pytest.raises(cli.CliError, match="package_llama"):
        cli.load_binaries(str(tmp_path / "nothing"))


def test_the_cli_requires_binaries_unless_told_otherwise(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "DEFAULT_BINARIES", str(tmp_path / "absent"))
    with pytest.raises(cli.CliError):
        cli._args_binaries(cli.build_parser().parse_args(["build"]))
    assert cli._args_binaries(cli.build_parser().parse_args(["build", "--no-binaries"])) is None
    got = cli._args_binaries(cli.build_parser().parse_args(["deploy", "--binaries", packaged(tmp_path)]))
    assert got["llama_build"] == BUILD


class FakeRemote:
    def __init__(self, tmp_path, corrupt=()):
        self.host, self.root = "fakehost", "/box"
        self.dir = tmp_path / "remote"
        self.dir.mkdir()
        self.corrupt, self.commands, self.uploads = corrupt, [], []

    def _local(self, remote_path):
        return self.dir / remote_path.replace("/", "__")

    def run(self, command, stream=False):
        self.commands.append(command)
        if command.startswith("grep -m1 '^VERSION'"):
            return (0, 'VERSION = "2.0.0"')
        if command.startswith("sha256sum"):
            target = command.split()[1]
            digest = hashlib.sha256(self._local(target).read_bytes()).hexdigest()
            return (0, f"{'0' * 64 if any(target.endswith(c) for c in self.corrupt) else digest}  {target}")
        return (0, "")

    def put(self, local, remote_path):
        self.uploads.append(remote_path)
        self._local(remote_path).write_bytes(open(local, "rb").read())


def _deploy(tmp_path, monkeypatch, remote, bins):
    pem = _pem()
    monkeypatch.setattr(cli, "check_tree", lambda repo, allow_dirty: "abc1234")
    return cli.deploy(make_repo(tmp_path, "2.1.0"), remote, pem, "n", expected_pubkey=release.public_key_hex(pem),
                      out_dir=str(tmp_path / "out"), log=lambda _m: None, binaries=bins)


def test_deploy_uploads_and_verifies_every_binary(tmp_path, monkeypatch):
    remote = FakeRemote(tmp_path)
    _deploy(tmp_path, monkeypatch, remote, cli.load_binaries(packaged(tmp_path)))
    names = {u.rsplit("/", 1)[1] for u in remote.uploads}
    assert {release.rpc_zip_name(p) for p in release.RPC_PLATFORMS} | {"server-llama.tar.gz"} <= names
    checked = {c.split()[1].rsplit("/", 1)[1] for c in remote.commands if c.startswith("sha256sum")}
    assert names - {"release.json", "release_apply.py"} <= checked
    apply_cmd = next(c for c in remote.commands if "release_apply.py apply" in c)
    assert "--assets /box/.releases/incoming" in apply_cmd


@pytest.mark.parametrize("corrupt", ["rpc-bin-linux-x64.zip", "server-llama.tar.gz"])
def test_a_corrupted_binary_upload_never_reaches_the_installer(tmp_path, monkeypatch, corrupt):
    remote = FakeRemote(tmp_path, corrupt=(corrupt,))
    with pytest.raises(cli.CliError, match=f"{corrupt} arrived"):
        _deploy(tmp_path, monkeypatch, remote, cli.load_binaries(packaged(tmp_path)))
    assert not any("release_apply.py apply" in c for c in remote.commands)


def test_without_binaries_deploy_passes_none(tmp_path, monkeypatch):
    remote = FakeRemote(tmp_path)
    _deploy(tmp_path, monkeypatch, remote, None)
    assert "--assets" not in next(c for c in remote.commands if "release_apply.py apply" in c)


# --- the coordinator ----------------------------------------------------------------------

def _client():
    return AsyncClient(transport=ASGITransport(app=coordinator.app, client=("203.0.113.9", 4000)), base_url="http://test")


@pytest.mark.anyio
async def test_the_coordinator_serves_each_platform_and_nothing_unsigned(tmp_path, monkeypatch):
    monkeypatch.setattr(coordinator, "get_base_dir", lambda: str(tmp_path))
    zips = {p: make_rpc_zip(tmp_path / release.rpc_zip_name(p), marker=p) for p in release.RPC_PLATFORMS}
    (tmp_path / "DLNE-Windows-Vulkan.zip").write_bytes(b"old unverified bundle")
    async with _client() as c:
        for platform, path in zips.items():
            resp = await c.get(f"/v1/download/rpc-bin/{platform}")
            assert resp.status_code == 200 and resp.content == open(path, "rb").read()
        legacy = await c.get("/v1/download/rpc-bin")
        unknown = await c.get("/v1/download/rpc-bin/..%2Frelease.json")
        other = await c.get("/v1/download/rpc-bin/amiga")
        bundle = await c.get("/v1/download/windows")
    assert legacy.content == open(zips["win-x64"], "rb").read()
    assert unknown.status_code == 404 and other.status_code == 404 and bundle.status_code == 404


def test_the_coordinator_requires_the_protocol_its_release_names(tmp_path, pem, monkeypatch):
    _a, _z, _s, m = signed_release(tmp_path, pem)
    monkeypatch.setattr(release, "RELEASE_PUBKEY", release.public_key_hex(pem))
    monkeypatch.setattr(coordinator, "get_base_dir", lambda: str(tmp_path))
    monkeypatch.setattr(coordinator.registry, "required_rpc_major", None)
    (tmp_path / "release.json").write_text(json.dumps(m))
    coordinator._apply_rpc_requirement()
    assert coordinator.registry.required_rpc_major == 8
    (tmp_path / "release.json").write_text(json.dumps(dict(m, rpc_proto="9.0.0")))   # forged
    coordinator._apply_rpc_requirement()
    assert coordinator.registry.required_rpc_major is None


# --- the registry: never route to a worker that would abort the engine ---------------------

def _worker(reg, wid, ip, proto, vram=8.0):
    hardware = {"gpu": {"has_gpu": True, "vram_total_gb": vram}, "ram_gb": 16}
    if proto is not None:
        hardware["rpc_proto"] = proto
    reg.update_worker(wid, ip, 50052, f"tok-{wid}", hardware)


def test_mismatched_and_unknown_protocols_are_left_out_of_everything():
    reg = WorkerRegistry()
    _worker(reg, "new", "10.0.0.2", "8.0.1")
    _worker(reg, "old", "10.0.0.3", "5.0.0")
    _worker(reg, "unknown", "10.0.0.4", None)
    assert len(reg.get_active_rpc_endpoints()) == 3            # no requirement: as before
    reg.required_rpc_major = 8
    assert reg.get_active_rpc_endpoints() == ["10.0.0.2:50052"]
    assert reg.plan_endpoints(1000.0, "always") == ["10.0.0.2:50052"]
    assert reg.get_total_vram_gb() == 8.0
    assert reg.get_total_sysram_gb() == 16


def test_a_minor_difference_is_compatible():
    reg = WorkerRegistry()
    reg.required_rpc_major = 8
    assert reg.rpc_compatible({"rpc_proto": "8.2.0"})
    assert not reg.rpc_compatible({"rpc_proto": "8"})
    assert not reg.rpc_compatible("not a dict")


# --- the worker ---------------------------------------------------------------------------

def test_the_worker_reads_the_protocol_from_the_rpc_server_banner(tmp_path):
    from engine.worker import read_rpc_banner
    log = tmp_path / "rpc_server.log"
    log.write_text("Starting RPC server v5.0.0\n")                       # an earlier run
    offset = log.stat().st_size
    with open(log, "a") as handle:
        handle.write("load_backend: loaded Vulkan backend\nStarting RPC server v8.0.1\n  endpoint : 0.0.0.0:50052\n")
    assert read_rpc_banner(str(log), offset, timeout=0.5) == "8.0.1"
    assert read_rpc_banner(str(log), log.stat().st_size, timeout=0.2) == ""


def test_the_protocol_travels_inside_the_signed_hardware_profile(monkeypatch):
    from engine import authn, worker as worker_mod
    node = worker_mod.WorkerNode.__new__(worker_mod.WorkerNode)
    node.worker_id, node.rpc_port, node.access_token, node.role = "w1", 50052, "tok", "worker"
    node.hardware_profile, node._staged_version, node.rpc_proto = {"ram_gb": 16}, "", "8.0.1"
    monkeypatch.setattr(node.__class__, "_get_network_key", lambda self: "k" * 64, raising=False)
    payload = node._build_heartbeat_payload()
    assert payload["hardware"]["rpc_proto"] == "8.0.1"
    assert "rpc_proto" not in node.hardware_profile                     # the profile itself untouched
    expected = authn.sign_heartbeat("k" * 64, "w1", 50052, "tok", payload["timestamp"], payload["nonce"],
                                    payload["version"], payload["hardware"], "worker", "")
    assert payload["signature"] == expected
    tampered = authn.sign_heartbeat("k" * 64, "w1", 50052, "tok", payload["timestamp"], payload["nonce"],
                                    payload["version"], {**payload["hardware"], "rpc_proto": "5.0.0"}, "worker", "")
    assert tampered != payload["signature"]


# --- the Python worker's download ---------------------------------------------------------

class FakeHttp:
    def __init__(self, routes):
        self.routes, self.requested = routes, []

    def __call__(self, url, timeout=None):
        self.requested.append(url)
        if url not in self.routes:
            raise OSError(f"404 {url}")
        return io.BytesIO(self.routes[url])


def _worker_setup(tmp_path, monkeypatch, platform="linux-x64", serve=None, marker="v1"):
    pem = _pem()
    monkeypatch.setattr(release, "RELEASE_PUBKEY", release.public_key_hex(pem))
    archive, zips, _srv, m = signed_release(tmp_path, pem, marker=marker, name=f"bins-{marker}")
    base = tmp_path / "runtime"
    base.mkdir(exist_ok=True)
    http = FakeHttp({
        "http://c/v1/release": json.dumps(m).encode(),
        f"http://c/v1/download/rpc-bin/{platform}": serve if serve is not None else open(zips[platform], "rb").read(),
    })
    return str(base), http, m


def test_the_worker_installs_its_platforms_signed_build(tmp_path, monkeypatch):
    base, http, m = _worker_setup(tmp_path, monkeypatch)
    assert downloader.sync_rpc_bin("http://c", base, "linux-x64", urlopen=http) is True
    exe = os.path.join(base, "bin", "ggml-rpc-server")
    assert open(exe).read() == "rpc linux-x64-v1"
    assert os.stat(exe).st_mode & 0o111
    record = json.load(open(os.path.join(base, downloader.RPC_RECORD)))
    assert record == {"sha256": m["rpc_linux_x64_sha256"], "llama_build": BUILD, "rpc_proto": PROTO}
    assert not os.path.exists(os.path.join(base, "rpc-bin.zip.download"))


def test_an_installed_build_is_not_downloaded_again(tmp_path, monkeypatch):
    base, http, _m = _worker_setup(tmp_path, monkeypatch)
    downloader.sync_rpc_bin("http://c", base, "linux-x64", urlopen=http)
    assert downloader.sync_rpc_bin("http://c", base, "linux-x64", urlopen=http) is False
    assert http.requested.count("http://c/v1/download/rpc-bin/linux-x64") == 1


def test_a_new_release_replaces_the_installed_build(tmp_path, monkeypatch):
    base, http, _m = _worker_setup(tmp_path, monkeypatch)
    downloader.sync_rpc_bin("http://c", base, "linux-x64", urlopen=http)
    base2, http2, _m2 = _worker_setup(tmp_path, monkeypatch, marker="v2")
    assert downloader.sync_rpc_bin("http://c", base2, "linux-x64", urlopen=http2) is True
    assert open(os.path.join(base2, "bin", "ggml-rpc-server")).read() == "rpc linux-x64-v2"


def test_the_worker_refuses_a_tampered_build_and_leaves_nothing(tmp_path, monkeypatch):
    evil = io.BytesIO()
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("bin/ggml-rpc-server", b"something else")
    base, http, _m = _worker_setup(tmp_path, monkeypatch, serve=evil.getvalue())
    with pytest.raises(release.ReleaseError, match="does not match"):
        downloader.sync_rpc_bin("http://c", base, "linux-x64", urlopen=http)
    assert os.listdir(base) == []


def test_the_worker_refuses_a_release_without_its_platform(tmp_path, monkeypatch):
    base, http, _m = _worker_setup(tmp_path, monkeypatch)
    with pytest.raises(release.ReleaseError, match="no RPC build for amiga"):
        downloader.sync_rpc_bin("http://c", base, "amiga", urlopen=http)


def test_the_worker_refuses_a_manifest_that_does_not_verify(tmp_path, monkeypatch):
    base, http, _m = _worker_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(release, "RELEASE_PUBKEY", release.public_key_hex(_pem()))
    with pytest.raises(release.ReleaseError, match="failed verification"):
        downloader.sync_rpc_bin("http://c", base, "linux-x64", urlopen=http)
    assert http.requested == ["http://c/v1/release"]


def test_a_signed_zip_with_unexpected_contents_is_refused(tmp_path, monkeypatch):
    """Defence in depth: even a correctly signed zip must hold only the expected files."""
    pem = _pem()
    monkeypatch.setattr(release, "RELEASE_PUBKEY", release.public_key_hex(pem))
    archive = str(tmp_path / "src.zip")
    release.build_source_archive(make_repo(tmp_path, "2.1.0"), archive)
    odd = make_rpc_zip(tmp_path / "odd.zip", {"bin/ggml-rpc-server": b"MZ", "bin/extra.sh": b"x"})
    m = release.make_manifest("2.1.0", archive, "a", BUILT_AT)
    m.update(llama_build=BUILD, rpc_proto=PROTO, rpc_linux_x64_sha256=release.sha256_file(odd),
             rpc_linux_x64_size=os.path.getsize(odd))
    base = tmp_path / "rt"
    base.mkdir()
    http = FakeHttp({"http://c/v1/release": json.dumps(release.sign_manifest(m, pem)).encode(),
                     "http://c/v1/download/rpc-bin/linux-x64": open(odd, "rb").read()})
    with pytest.raises(release.ReleaseError, match="never ships"):
        downloader.sync_rpc_bin("http://c", str(base), "linux-x64", urlopen=http)
    assert os.listdir(base) == []


def test_a_hybrid_worker_on_the_coordinator_never_syncs(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(downloader, "sync_rpc_bin", lambda *a, **k: calls.append(a))
    monkeypatch.setattr("engine.config.get_base_dir", lambda: str(tmp_path))
    downloader.download_and_extract_engine("worker", coordinator_url=None)
    assert calls == []
    downloader.download_and_extract_engine("worker", coordinator_url="http://c/")
    assert calls and calls[0][0] == "http://c"
