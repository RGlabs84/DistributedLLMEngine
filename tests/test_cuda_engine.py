"""
The CUDA engine (2.7): llama.cpp's official CUDA 13.4 build of the engine's own tag, shipped beside the Vulkan one.

Everything the Vulkan engine had, the CUDA engine has too, and none of the Vulkan side may change: the packager builds
cuda-bin-<platform>.zip from upstream's two archives, the signed manifest names each by size and SHA-256 (cuda_*),
the coordinator serves them, the installer keeps them out of its backups, the downloader fetches, verifies and
unpacks one into a folder of its own (bin-cuda) so both flavours can sit on a disk during a switch, and the
coordinator's own engine follows ENGINE_FLAVOR and reports the flavour it really runs (engine_flavour).
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

from engine import config as engine_config
from engine import coordinator, downloader, inference_manager, release

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILT_AT = "2026-10-30T12:00:00Z"
BUILD, PROTO = "b11838", "8.0.1"


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(REPO_ROOT, "scripts", filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


apply_mod = _load("release_apply_cuda", "release_apply.py")
cli = _load("release_cli_cuda", "release.py")
packager = _load("package_llama_cuda", "package_llama.py")


def _pem():
    return Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


@pytest.fixture
def pem():
    return _pem()


@pytest.fixture(autouse=True)
def _fresh_flavour():
    """The coordinator decides its flavour once per process; each test starts undecided."""
    downloader._coordinator_flavour = ""
    yield
    downloader._coordinator_flavour = ""


# --- builders -----------------------------------------------------------------------------

def make_cuda_zip(path, marker="v1", platform="linux-x64", members=None):
    windows = platform.startswith("win")
    exe = ".exe" if windows else ""
    members = members if members is not None else {
        f"bin/ggml-rpc-server{exe}": f"rpc {marker}".encode(),
        f"bin/llama-server{exe}": f"server {marker}".encode(),
        "bin/ggml-cuda.dll" if windows else "bin/libggml-cuda.so": b"cuda backend",
        "bin/cudart64_13.dll" if windows else "bin/libcudart.so.13": b"cudart",
        "bin/ggml-base.dll" if windows else "bin/libggml-base.so.0": b"lib",
    }
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return str(path)


def make_rpc_zip(path, marker="v1"):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("bin/ggml-rpc-server", f"rpc {marker}".encode())
        archive.writestr("bin/libggml-vulkan.so", b"vk")
    return str(path)


def make_repo(base, version):
    root = base / f"repo-{version}"
    (root / "engine").mkdir(parents=True, exist_ok=True)
    (root / "engine" / "version.py").write_text(f'VERSION = "{version}"\n')
    (root / "dlne_server.py").write_text(f"print('{version}')\n")
    (root / "requirements.txt").write_text("fastapi\n")
    return str(root)


def make_server_tarball(path, build=BUILD):
    with tarfile.open(path, "w:gz") as tar:
        info = tarfile.TarInfo(f"llama-{build}/llama-server")
        info.size, info.mode = 6, 0o755
        tar.addfile(info, io.BytesIO(b"server"))
    return str(path)


def release_parts(tmp_path, with_cuda=True, marker="v1", name="bins"):
    """(archive, rpc zips, server tarball, cuda zips) for a 2.7.0-shaped release."""
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    archive = str(tmp_path / f"src-{name}.zip")
    release.build_source_archive(make_repo(tmp_path, "2.7.0"), archive)
    rpc = {p: make_rpc_zip(d / release.rpc_zip_name(p), f"{p}-{marker}") for p in release.RPC_PLATFORMS}
    cuda = ({p: make_cuda_zip(d / release.cuda_zip_name(p), f"{p}-{marker}", p) for p in release.CUDA_PLATFORMS}
            if with_cuda else None)
    server = make_server_tarball(d / f"llama-{BUILD}-bin-ubuntu-vulkan-x64.tar.gz")
    return archive, rpc, server, cuda


def signed(tmp_path, pem, with_cuda=True, marker="v1", name="bins"):
    archive, rpc, server, cuda = release_parts(tmp_path, with_cuda, marker, name)
    manifest = release.make_manifest("2.7.0", archive, "abc1234", BUILT_AT, "n", rpc_zips=rpc, llama_build=BUILD,
                                     rpc_proto=PROTO, server_llama=server, cuda_zips=cuda)
    return archive, rpc, server, cuda, release.sign_manifest(manifest, pem)


# --- the release library ------------------------------------------------------------------

def test_the_manifest_signs_each_cuda_zip_with_its_size_and_hash(tmp_path, pem):
    _a, _r, _s, cuda, m = signed(tmp_path, pem)
    assert m["cuda_version"] == "13.4"
    for platform, path in cuda.items():
        assert m[release.cuda_field(platform, "sha256")] == release.sha256_file(path)
        assert m[release.cuda_field(platform, "size")] == os.path.getsize(path)
        assert release.verify_cuda_bin(m, platform, path) == (True, "ok")
    assert release.verify_manifest(m, release.public_key_hex(pem)) == (True, "ok")
    assert set(release.CUDA_PLATFORMS) == {"win-x64", "linux-x64"}


def test_the_field_names_are_the_ones_the_other_parts_code_to():
    assert release.cuda_field("linux-x64", "sha256") == "cuda_linux_x64_sha256"
    assert release.cuda_field("win-x64", "size") == "cuda_win_x64_size"
    assert release.cuda_zip_name("win-x64") == "cuda-bin-win-x64.zip"


def test_cuda_changes_no_vulkan_field(tmp_path, pem):
    """Old workers must keep working: every field a release without CUDA signs is signed identically with it."""
    archive, rpc, server, cuda = release_parts(tmp_path)
    plain = release.make_manifest("2.7.0", archive, "abc1234", BUILT_AT, "n", rpc_zips=rpc, llama_build=BUILD,
                                  rpc_proto=PROTO, server_llama=server)
    both = release.make_manifest("2.7.0", archive, "abc1234", BUILT_AT, "n", rpc_zips=rpc, llama_build=BUILD,
                                 rpc_proto=PROTO, server_llama=server, cuda_zips=cuda)
    assert {k: v for k, v in both.items() if not k.startswith("cuda_")} == plain
    assert not any(k.startswith("cuda_") for k in plain)
    assert set(both) - set(plain) == {"cuda_version", "cuda_linux_x64_sha256", "cuda_linux_x64_size",
                                      "cuda_win_x64_sha256", "cuda_win_x64_size"}


def test_a_release_without_cuda_is_still_valid_and_has_no_cuda_asset(tmp_path, pem):
    _a, _r, _s, _c, m = signed(tmp_path, pem, with_cuda=False)
    assert release.verify_manifest(m, release.public_key_hex(pem)) == (True, "ok")
    assert release.verify_cuda_bin(m, "linux-x64", "x")[0] is False


@pytest.mark.parametrize("field,value", [("cuda_linux_x64_sha256", "1" * 64), ("cuda_win_x64_size", 7)])
def test_changing_any_cuda_field_breaks_the_signature(tmp_path, pem, field, value):
    _a, _r, _s, _c, m = signed(tmp_path, pem)
    ok, reason = release.verify_manifest(dict(m, **{field: value}), release.public_key_hex(pem))
    assert not ok and "signature" in reason


def test_only_the_cuda_version_the_engine_is_built_with_is_accepted(tmp_path, pem):
    _a, _r, _s, _c, m = signed(tmp_path, pem)
    ok, reason = release.verify_manifest(dict(m, cuda_version="12.8"), release.public_key_hex(pem))
    assert not ok and "cuda_version" in reason


@pytest.mark.parametrize("patch,pattern", [
    ({"cuda_linux_x64_sha256": "XYZ"}, "cuda_linux_x64_sha256"),
    ({"cuda_win_x64_size": 0}, "cuda_win_x64_size"),
    ({"cuda_win_x64_size": True}, "cuda_win_x64_size"),
    ({"cuda_version": "latest"}, "cuda_version"),
    ({"cuda_version": 13}, "cuda_version"),
])
def test_malformed_cuda_fields_are_rejected_by_shape(tmp_path, pem, patch, pattern):
    _a, _r, _s, _c, m = signed(tmp_path, pem)
    ok, reason = release.verify_manifest(dict(m, **patch), release.public_key_hex(pem))
    assert not ok and pattern in reason


def test_half_a_cuda_pair_or_a_version_alone_is_rejected(tmp_path, pem):
    _a, _r, _s, _c, m = signed(tmp_path, pem)
    lone = {k: v for k, v in m.items() if k != "cuda_win_x64_size"}
    assert release.verify_manifest(lone, release.public_key_hex(pem))[0] is False
    bare = {k: v for k, v in m.items() if not k.startswith("cuda_") or k == "cuda_version"}
    assert "no CUDA build" in release.verify_manifest(bare, release.public_key_hex(pem))[1]


def test_cuda_cannot_be_released_without_its_llama_build(tmp_path):
    archive, _rpc, _server, cuda = release_parts(tmp_path)
    with pytest.raises(release.ReleaseError, match="build"):
        release.make_manifest("2.7.0", archive, "a", BUILT_AT, cuda_zips=cuda, rpc_proto=PROTO)
    with pytest.raises(release.ReleaseError, match="Unknown CUDA platform"):
        release.make_manifest("2.7.0", archive, "a", BUILT_AT, cuda_zips={"macos-arm64": cuda["linux-x64"]},
                              llama_build=BUILD, rpc_proto=PROTO)


def test_a_substituted_cuda_zip_is_caught(tmp_path, pem):
    _a, _r, _s, cuda, m = signed(tmp_path, pem)
    path = cuda["linux-x64"]
    data = bytearray(open(path, "rb").read())
    data[-1] ^= 1
    open(path, "wb").write(bytes(data))
    assert release.verify_cuda_bin(m, "linux-x64", path) == (False, "the CUDA archive hash does not match the manifest")
    assert release.verify_cuda_bin(m, "win-x64", cuda["linux-x64"])[0] is False     # the other platform's zip
    open(path, "wb").write(b"short")
    assert release.verify_cuda_bin(m, "linux-x64", path) == (False, "the CUDA archive size does not match the manifest")


@pytest.mark.parametrize("name,ok", [
    ("bin/ggml-rpc-server", True), ("bin/llama-server.exe", True), ("bin/libggml-cuda.so", True),
    ("bin/ggml-cuda.dll", True), ("bin/libcudart.so.13", True), ("bin/libcublas.so.13", True),
    ("bin/libcublasLt.so.13", True), ("bin/cudart64_13.dll", True), ("bin/cublas64_13.dll", True),
    ("bin/cublasLt64_13.dll", True), ("bin/vcruntime140.dll", True), ("bin/libomp.dll", True),
    # only the CUDA 13 runtime, only flat, and none of upstream's other tools
    ("bin/libcudart.so.12", False), ("bin/cudart64_12.dll", False), ("bin/libcudart.so.13.lnk", False),
    ("bin/nvrtc64_130_0.dll", False), ("bin/sub/libcudart.so.13", False), ("libcudart.so.13", False),
    ("bin/llama-cli", False), ("bin/llama-bench.exe", False), ("bin/evil.exe", False),
])
def test_what_a_cuda_zip_may_contain(name, ok):
    assert (release.cuda_members([name]) == []) is ok


def test_a_cuda_zip_needs_the_servers_the_backend_and_the_runtime(tmp_path):
    full = {"bin/ggml-rpc-server": b"1", "bin/llama-server": b"2", "bin/libggml-cuda.so": b"3", "bin/libcudart.so.13": b"4"}
    release.check_cuda_bin(make_cuda_zip(tmp_path / "ok.zip", members=full))
    for gone, message in (("bin/ggml-rpc-server", "no bin/ggml-rpc-server"), ("bin/llama-server", "no bin/llama-server"),
                          ("bin/libggml-cuda.so", "no CUDA backend"), ("bin/libcudart.so.13", "no CUDA runtime")):
        partial = {k: v for k, v in full.items() if k != gone}
        with pytest.raises(release.ReleaseError, match=message):
            release.check_cuda_bin(make_cuda_zip(tmp_path / "p.zip", members=partial))
    with pytest.raises(release.ReleaseError, match="never ships"):
        release.check_cuda_bin(make_cuda_zip(tmp_path / "x.zip", members={**full, "bin/extra.sh": b"x"}))
    bad = tmp_path / "junk.zip"
    bad.write_bytes(b"nope")
    with pytest.raises(release.ReleaseError, match="not a readable zip"):
        release.check_cuda_bin(str(bad))


@pytest.mark.parametrize("system,machine,expected", [
    ("Windows", "AMD64", "win-x64"), ("Linux", "x86_64", "linux-x64"),
    # no CUDA engine for these (the owner's decision: Windows on ARM and macOS are out of scope)
    ("Windows", "ARM64", None), ("Linux", "aarch64", None), ("Darwin", "arm64", None), ("Darwin", "x86_64", None),
    ("FreeBSD", "amd64", None),
])
def test_host_cuda_platforms(system, machine, expected):
    assert release.host_cuda_platform(system, machine) == expected


def test_the_driver_minimum_is_written_once_with_its_source():
    assert release.CUDA_MIN_DRIVER == 580
    assert release.CUDA_VERSION == "13.4"
    assert release.CUDA_MIN_COMPUTE_CAPABILITY == (7, 5)
    source = open(os.path.join(REPO_ROOT, "engine", "release.py"), encoding="utf-8").read()
    assert "docs.nvidia.com/cuda/archive/13.4.1/cuda-toolkit-release-notes" in source   # the cited release notes


# --- the packager -------------------------------------------------------------------------

def _tar(path, files, top, links=()):
    with tarfile.open(path, "w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size, info.mode = len(data), 0o755
            tar.addfile(info, io.BytesIO(data))
        for name, target in links:
            link = tarfile.TarInfo(f"{top}/{name}")
            link.type, link.linkname = tarfile.SYMTYPE, target
            tar.addfile(link)
    return str(path)


def _zip(path, files):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return str(path)


def upstream_sources(tmp_path, build="b1", cuda=True):
    """Fake upstream archives under the real names: every Vulkan source, and the four CUDA ones."""
    paths = {}
    for pattern in packager.SOURCES.values():
        name = pattern.format(b=build)
        if name.endswith(".zip"):
            paths[name] = _zip(tmp_path / name, {"ggml-rpc-server.exe": b"rpc", "ggml.dll": b"lib"})
        else:
            paths[name] = _tar(tmp_path / name, {"ggml-rpc-server": b"rpc"}, "llama")
    server = packager.SERVER_SOURCE.format(b=build)
    paths.setdefault(server, str(tmp_path / server))
    if cuda:
        lin_engine, lin_runtime = (n.format(b=build) for n in packager.CUDA_SOURCES["linux-x64"])
        win_engine, win_runtime = (n.format(b=build) for n in packager.CUDA_SOURCES["win-x64"])
        paths[lin_engine] = _tar(tmp_path / lin_engine, {
            "llama-server": b"lin server", "ggml-rpc-server": b"lin rpc", "libggml-cuda.so": b"lin cuda",
            "libggml.so.0.27.0": b"real lib", "llama-cli": b"junk", "llama-bench": b"junk"}, "llama", links=[("libggml.so.0", "libggml.so.0.27.0")])
        paths[lin_runtime] = _tar(tmp_path / lin_runtime, {"libcudart.so.13": b"cudart", "libcublas.so.13": b"cublas",
                                                           "libcublasLt.so.13": b"cublaslt"}, "cudart")
        paths[win_engine] = _zip(tmp_path / win_engine, {
            "llama-server.exe": b"win server", "ggml-rpc-server.exe": b"win rpc", "ggml-cuda.dll": b"win cuda",
            "ggml.dll": b"lib", "llama-cli.exe": b"junk", "LICENSE-LLVM-OpenMP": b"license"})
        paths[win_runtime] = _zip(tmp_path / win_runtime, {"cudart64_13.dll": b"cudart", "cublas64_13.dll": b"cublas",
                                                            "cublasLt64_13.dll": b"cublaslt"})
    return paths


@pytest.fixture
def no_vcredist(monkeypatch):
    monkeypatch.setattr(packager, "check_vcredist", lambda path: path)
    monkeypatch.setattr(packager, "vcruntime_members",
                        lambda path, platform: [(dll, f"{platform}:{dll}".encode()) for dll in packager.VCRUNTIME_PLATFORMS[platform][2]])


def test_the_upstream_archives_are_the_official_cuda_13_4_builds_of_the_engines_tag():
    assert packager.CUDA_SOURCES["linux-x64"] == ("llama-{b}-bin-ubuntu-cuda-13.4-x64.tar.gz",
                                                  "cudart-llama-{b}-bin-ubuntu-cuda-13.4-x64.tar.gz")
    assert packager.CUDA_SOURCES["win-x64"] == ("llama-{b}-bin-win-cuda-13.4-x64.zip", "cudart-llama-bin-win-cuda-13.4-x64.zip")
    names = packager.upstream_names("b11838")
    assert "cudart-llama-bin-win-cuda-13.4-x64.zip" in names and "llama-b11838-bin-ubuntu-cuda-13.4-x64.tar.gz" in names
    assert not any("cuda" in n for n in packager.upstream_names("b11838", cuda=False))


def test_packaging_builds_one_cuda_zip_per_platform_with_the_runtime_beside_the_programs(tmp_path, no_vcredist):
    out = tmp_path / "out"
    info = packager.package("b1", upstream_sources(tmp_path), str(out), rpc_proto=PROTO, vcredist="fake.exe")
    assert info["cuda"] == {"version": "13.4", "zips": {"linux-x64": "cuda-bin-linux-x64.zip", "win-x64": "cuda-bin-win-x64.zip"}}
    with zipfile.ZipFile(out / "cuda-bin-linux-x64.zip") as z:
        names = set(z.namelist())
        assert {"bin/llama-server", "bin/ggml-rpc-server", "bin/libggml-cuda.so", "bin/libcudart.so.13", "bin/libcublas.so.13",
                "bin/libcublasLt.so.13"} <= names
        assert z.read("bin/libggml.so.0") == b"real lib"                                   # the symlink, as a copy
        assert not names & {"bin/llama-cli", "bin/llama-bench"}
        modes = {i.filename: (i.external_attr >> 16) & 0o777 for i in z.infolist()}
        assert modes["bin/llama-server"] == modes["bin/ggml-rpc-server"] == 0o755 and modes["bin/libcudart.so.13"] == 0o644
    with zipfile.ZipFile(out / "cuda-bin-win-x64.zip") as z:
        names = set(z.namelist())
        assert {"bin/llama-server.exe", "bin/ggml-rpc-server.exe", "bin/ggml-cuda.dll", "bin/cudart64_13.dll",
                "bin/cublas64_13.dll", "bin/cublasLt64_13.dll"} <= names
        assert {"bin/vcruntime140.dll", "bin/vcruntime140_1.dll", "bin/msvcp140.dll"} <= names      # a clean Windows has none
        assert "bin/llama-cli.exe" not in names and "bin/LICENSE-LLVM-OpenMP" not in names
    assert json.load(open(out / "build.json"))["cuda"]["zips"]["win-x64"] == "cuda-bin-win-x64.zip"


def test_the_vulkan_assets_are_the_same_with_and_without_cuda(tmp_path, no_vcredist):
    sources = upstream_sources(tmp_path)
    with_cuda, without = tmp_path / "with", tmp_path / "without"
    info_with = packager.package("b1", sources, str(with_cuda), rpc_proto=PROTO, vcredist="f")
    info_without = packager.package("b1", {k: v for k, v in sources.items() if "cuda" not in k}, str(without),
                                    rpc_proto=PROTO, vcredist="f")
    for platform in release.RPC_PLATFORMS:
        name = release.rpc_zip_name(platform)
        assert (with_cuda / name).read_bytes() == (without / name).read_bytes()
    assert {k: v for k, v in info_with.items() if k != "cuda"} == info_without
    assert "cuda" not in json.load(open(without / "build.json"))
    assert not any(n.startswith("cuda-bin-") for n in os.listdir(without))


def test_the_cuda_zips_are_the_same_bytes_every_time(tmp_path, no_vcredist):
    sources = upstream_sources(tmp_path)
    packager.package("b1", sources, str(tmp_path / "a"), rpc_proto=PROTO, vcredist="f")
    packager.package("b1", sources, str(tmp_path / "b"), rpc_proto=PROTO, vcredist="f")
    for platform in release.CUDA_PLATFORMS:
        name = release.cuda_zip_name(platform)
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()


def test_a_windows_cuda_zip_without_the_runtime_installer_is_refused(tmp_path):
    sources = upstream_sources(tmp_path)
    engine_name, runtime_name = (n.format(b="b1") for n in packager.CUDA_SOURCES["win-x64"])
    with pytest.raises(packager.PackageError, match="CUDA zip needs the Visual C\\+\\+ runtime"):
        packager.cuda_members_for("win-x64", sources[engine_name], sources[runtime_name])


def test_a_cuda_archive_without_the_programs_is_refused(tmp_path, no_vcredist):
    sources = upstream_sources(tmp_path)
    name = packager.CUDA_SOURCES["linux-x64"][0].format(b="b1")
    sources[name] = _tar(tmp_path / "bare.tar.gz", {"libggml-cuda.so": b"x"}, "llama")
    with pytest.raises(packager.PackageError, match="no ggml-rpc-server"):
        packager.package("b1", sources, str(tmp_path / "out"), rpc_proto=PROTO, vcredist="f")


class FakeUpstream:
    """gh api / gh release download, as far as fetch() uses them."""

    def __init__(self, build, content):
        self.content = content          # name -> bytes served
        self.digests = {n: "sha256:" + hashlib.sha256(b).hexdigest() for n, b in content.items()}

    def run(self, command, capture_output=True, text=True):
        class Done:
            returncode, stdout, stderr = 0, "", ""
        done = Done()
        if command[:2] == ["gh", "release"]:
            name, dest = command[command.index("-p") + 1], command[command.index("-D") + 1]
            with open(os.path.join(dest, name), "wb") as handle:
                handle.write(self.content[name])
        return done


def test_every_upstream_download_is_checked_against_githubs_digest(tmp_path, monkeypatch):
    names = packager.upstream_names("b1")
    served = {n: n.encode() for n in names}
    upstream = FakeUpstream("b1", served)
    monkeypatch.setattr(packager, "upstream_digests", lambda build: upstream.digests)
    monkeypatch.setattr(packager.subprocess, "run", upstream.run)
    os.makedirs(tmp_path / "ok")
    assert set(packager.fetch("b1", str(tmp_path / "ok"), cuda=True)) == set(names)
    # a download whose bytes are not the published ones (a poisoned mirror, a truncated file) is refused ...
    bad_name = packager.CUDA_SOURCES["win-x64"][1].format(b="b1")
    upstream.content[bad_name] = b"tampered"
    os.makedirs(tmp_path / "bad")
    with pytest.raises(packager.PackageError, match="does not match the digest GitHub publishes"):
        packager.fetch("b1", str(tmp_path / "bad"), cuda=True)
    # ... and so is an asset GitHub publishes no digest for
    upstream.content[bad_name] = served[bad_name]
    upstream.digests = {k: v for k, v in upstream.digests.items() if k != bad_name}
    os.makedirs(tmp_path / "nodigest")
    with pytest.raises(packager.PackageError, match="publishes no SHA-256 digest"):
        packager.fetch("b1", str(tmp_path / "nodigest"), cuda=True)
    # the same bad archive is simply not asked for when CUDA is left out
    os.makedirs(tmp_path / "nocuda")
    assert bad_name not in packager.fetch("b1", str(tmp_path / "nocuda"), cuda=False)


def test_archives_already_on_disk_are_held_to_the_same_digests(tmp_path, monkeypatch):
    names = packager.upstream_names("b1")
    upstream = FakeUpstream("b1", {n: n.encode() for n in names})
    monkeypatch.setattr(packager, "upstream_digests", lambda build: upstream.digests)
    for n in names:
        (tmp_path / n).write_bytes(n.encode())
    assert set(packager._verify_existing("b1", str(tmp_path), True)) == set(names)
    (tmp_path / packager.CUDA_SOURCES["linux-x64"][0].format(b="b1")).write_bytes(b"other")
    with pytest.raises(packager.PackageError, match="does not match GitHub's digest"):
        packager._verify_existing("b1", str(tmp_path), True)
    assert set(packager._verify_existing("b1", str(tmp_path), False)) == set(packager.upstream_names("b1", cuda=False))


# --- the operator CLI ---------------------------------------------------------------------

def packaged(tmp_path, with_cuda=True, name="pkg"):
    d = tmp_path / name
    d.mkdir()
    for platform in release.RPC_PLATFORMS:
        make_rpc_zip(d / release.rpc_zip_name(platform), platform)
    make_server_tarball(d / "server.tar.gz")
    info = {"llama_build": BUILD, "rpc_proto": PROTO, "server_llama": "server.tar.gz"}
    if with_cuda:
        for platform in release.CUDA_PLATFORMS:
            make_cuda_zip(d / release.cuda_zip_name(platform), platform, platform)
        info["cuda"] = {"version": "13.4", "zips": {p: release.cuda_zip_name(p) for p in release.CUDA_PLATFORMS}}
    (d / "build.json").write_text(json.dumps(info))
    return str(d)


def test_the_cli_loads_and_signs_the_cuda_zips(tmp_path, monkeypatch):
    pem = _pem()
    monkeypatch.setattr(cli, "check_tree", lambda repo, allow_dirty: "abc1234")
    binaries = cli.load_binaries(packaged(tmp_path))
    assert set(binaries["cuda_zips"]) == set(release.CUDA_PLATFORMS)
    built = cli.build_release(make_repo(tmp_path, "2.7.0"), str(tmp_path / "out"), pem, "n",
                              expected_pubkey=release.public_key_hex(pem), binaries=binaries)
    m = json.load(open(built["manifest_path"]))
    assert release.verify_manifest(m, release.public_key_hex(pem)) == (True, "ok")
    assert m["cuda_version"] == "13.4" and all(release.cuda_field(p, "sha256") in m for p in release.CUDA_PLATFORMS)


def test_a_package_without_cuda_builds_the_release_it_always_did(tmp_path, monkeypatch):
    pem = _pem()
    monkeypatch.setattr(cli, "check_tree", lambda repo, allow_dirty: "abc1234")
    binaries = cli.load_binaries(packaged(tmp_path, with_cuda=False))
    assert "cuda_zips" not in binaries
    built = cli.build_release(make_repo(tmp_path, "2.7.0"), str(tmp_path / "out"), pem, "n",
                              expected_pubkey=release.public_key_hex(pem), binaries=binaries)
    assert not any(k.startswith("cuda_") for k in built["manifest"])


def test_a_package_that_names_a_cuda_zip_it_lacks_is_refused(tmp_path):
    d = packaged(tmp_path)
    os.remove(os.path.join(d, release.cuda_zip_name("win-x64")))
    with pytest.raises(cli.CliError, match="cuda-bin-win-x64.zip"):
        cli.load_binaries(d)


class FakeRemote:
    """The coordinator host: records uploads, answers sha256sum for what it holds (and fails for what it does not)."""

    def __init__(self, tmp_path, holds=None):
        self.host, self.root = "fakehost", "/box"
        self.dir = tmp_path / "remote"
        self.dir.mkdir()
        self.commands, self.uploads = [], []
        for remote_path, data in (holds or {}).items():
            self._local(remote_path).write_bytes(data)

    def _local(self, remote_path):
        return self.dir / remote_path.replace("/", "__")

    def run(self, command, stream=False):
        self.commands.append(command)
        if command.startswith("grep -m1 '^VERSION'"):
            return (0, 'VERSION = "2.6.6"')
        if command.startswith("sha256sum"):
            target = command.split()[1]
            if not self._local(target).exists():
                return (1, f"sha256sum: {target}: No such file or directory")
            return (0, f"{hashlib.sha256(self._local(target).read_bytes()).hexdigest()}  {target}")
        return (0, "")

    def put(self, local, remote_path):
        self.uploads.append(remote_path)
        self._local(remote_path).write_bytes(open(local, "rb").read())


def _deploy(tmp_path, monkeypatch, remote, binaries):
    pem = _pem()
    monkeypatch.setattr(cli, "check_tree", lambda repo, allow_dirty: "abc1234")
    cli.deploy(make_repo(tmp_path, "2.7.0"), remote, pem, "n", expected_pubkey=release.public_key_hex(pem),
               out_dir=str(tmp_path / "out"), log=lambda _m: None, binaries=binaries)


def test_deploy_uploads_each_cuda_zip_and_verifies_it(tmp_path, monkeypatch):
    remote = FakeRemote(tmp_path)
    _deploy(tmp_path, monkeypatch, remote, cli.load_binaries(packaged(tmp_path)))
    names = {u.rsplit("/", 1)[1] for u in remote.uploads}
    assert {release.cuda_zip_name(p) for p in release.CUDA_PLATFORMS} <= names
    checked = {c.split()[1].rsplit("/", 1)[1] for c in remote.commands if c.startswith("sha256sum /box/.releases/incoming")}
    assert {release.cuda_zip_name(p) for p in release.CUDA_PLATFORMS} <= checked


def test_deploy_does_not_send_a_cuda_zip_the_host_already_holds(tmp_path, monkeypatch):
    pkg = packaged(tmp_path)
    held = {f"/box/{release.cuda_zip_name('linux-x64')}": open(os.path.join(pkg, release.cuda_zip_name("linux-x64")), "rb").read()}
    remote = FakeRemote(tmp_path, holds=held)
    _deploy(tmp_path, monkeypatch, remote, cli.load_binaries(pkg))
    names = {u.rsplit("/", 1)[1] for u in remote.uploads}
    assert release.cuda_zip_name("linux-x64") not in names and release.cuda_zip_name("win-x64") in names
    # a file the host holds that is NOT the signed one is sent
    (tmp_path / "second").mkdir()
    remote2 = FakeRemote(tmp_path / "second", holds={f"/box/{release.cuda_zip_name('linux-x64')}": b"an older engine"})
    _deploy(tmp_path / "second", monkeypatch, remote2, cli.load_binaries(pkg))
    assert release.cuda_zip_name("linux-x64") in {u.rsplit("/", 1)[1] for u in remote2.uploads}


# --- the box-side installer ---------------------------------------------------------------

@pytest.fixture
def host(tmp_path):
    root = tmp_path / "box"
    (root / "engine").mkdir(parents=True)
    (root / "engine" / "version.py").write_text('VERSION = "2.6.6"\n')
    (root / "dlne_server.py").write_text("old\n")
    (root / "requirements.txt").write_text("fastapi\n")
    old_build = tmp_path / "shared" / "llama-b10451"
    old_build.mkdir(parents=True)
    (old_build / "llama-server").write_text("server old")
    os.symlink(str(old_build), str(root / "bin"))
    return str(root)


class Recorder:
    def __init__(self):
        self.restarts = 0

    def restart(self):
        self.restarts += 1

    def health(self, _version):
        return (True, "fine")


def incoming(tmp_path, rpc, server, cuda=None, name="incoming"):
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    for platform, path in rpc.items():
        (d / release.rpc_zip_name(platform)).write_bytes(open(path, "rb").read())
    (d / apply_mod.SERVER_LLAMA_UPLOAD).write_bytes(open(server, "rb").read())
    for platform, path in (cuda or {}).items():
        (d / release.cuda_zip_name(platform)).write_bytes(open(path, "rb").read())
    return str(d)


def run_apply(host, archive, manifest, assets):
    rec = Recorder()
    return apply_mod.apply_release(host, archive, manifest, rec.restart, rec.health, log=lambda _m: None, assets_dir=assets)


def test_the_installer_installs_the_cuda_zips_beside_the_code(tmp_path, pem, host):
    archive, rpc, server, cuda, m = signed(tmp_path, pem)
    run_apply(host, archive, m, incoming(tmp_path, rpc, server, cuda))
    for platform, path in cuda.items():
        assert open(os.path.join(host, release.cuda_zip_name(platform)), "rb").read() == open(path, "rb").read()


def test_the_installer_reuses_the_installed_cuda_zip_when_none_is_uploaded(tmp_path, pem, host):
    archive, rpc, server, cuda, m = signed(tmp_path, pem)
    for platform, path in cuda.items():                     # a previous release left them here
        open(os.path.join(host, release.cuda_zip_name(platform)), "wb").write(open(path, "rb").read())
    run_apply(host, archive, m, incoming(tmp_path, rpc, server))      # nothing CUDA uploaded
    assert os.path.exists(os.path.join(host, release.cuda_zip_name("linux-x64")))


def test_a_cuda_zip_that_is_neither_uploaded_nor_installed_is_refused_before_anything_changes(tmp_path, pem, host):
    archive, rpc, server, _cuda, m = signed(tmp_path, pem)
    with pytest.raises(apply_mod.ApplyError, match="CUDA archive"):
        run_apply(host, archive, m, incoming(tmp_path, rpc, server))
    assert open(os.path.join(host, "engine", "version.py")).read() == 'VERSION = "2.6.6"\n'


def test_a_tampered_cuda_upload_is_refused(tmp_path, pem, host):
    archive, rpc, server, cuda, m = signed(tmp_path, pem)
    assets = incoming(tmp_path, rpc, server, cuda)
    open(os.path.join(assets, release.cuda_zip_name("win-x64")), "wb").write(b"not the signed engine")
    with pytest.raises(apply_mod.ApplyError, match="win-x64 CUDA archive"):
        run_apply(host, archive, m, assets)


def test_backups_leave_the_big_cuda_zips_out_and_a_release_without_cuda_removes_them(tmp_path, pem, host):
    archive, rpc, server, cuda, m = signed(tmp_path, pem)
    run_apply(host, archive, m, incoming(tmp_path, rpc, server, cuda))
    backup = apply_mod.make_backup(host, "check")
    with tarfile.open(os.path.join(backup, apply_mod.BACKUP_TAR)) as tar:
        assert not any(n.startswith("cuda-bin-") for n in tar.getnames())
    # the next release signs no CUDA engine: the zips (600 MB each) are removed rather than left to rot
    archive2, rpc2, server2, _c, m2 = signed(tmp_path, pem, with_cuda=False, name="bins2")
    run_apply(host, archive2, m2, incoming(tmp_path, rpc2, server2, name="incoming2"))
    assert not any(n.startswith("cuda-bin-") for n in os.listdir(host))


def test_health_streams_and_checks_every_served_cuda_zip(tmp_path, pem, monkeypatch):
    archive, rpc, _server, cuda, m = signed(tmp_path, pem)
    served = {
        "/v1/cluster/status": json.dumps({"server_version": "2.7.0"}).encode(),
        "/v1/release": json.dumps(m).encode(),
        "/v1/download/source": open(archive, "rb").read(),
    }
    for platform, path in rpc.items():
        served[f"/v1/download/rpc-bin/{platform}"] = open(path, "rb").read()
    for platform, path in cuda.items():
        served[f"/v1/download/cuda-bin/{platform}"] = open(path, "rb").read()
    monkeypatch.setattr(apply_mod, "_http_get", lambda url, timeout=5.0: served[url.split("http://box", 1)[1]])
    monkeypatch.setattr(apply_mod.urllib.request, "urlopen", lambda url, timeout=5.0: io.BytesIO(served[url.split("http://box", 1)[1]]))
    health = apply_mod.http_health("http://box", lambda _v: m, timeout=0.2, poll=0.01, sleep=lambda _s: None)
    assert health("2.7.0") == (True, "up, serving the installed release")
    served["/v1/download/cuda-bin/win-x64"] = b"other"
    ok, detail = health("2.7.0")
    assert not ok and "win-x64" in detail and "CUDA" in detail


# --- the coordinator serves them ----------------------------------------------------------

def _client():
    return AsyncClient(transport=ASGITransport(app=coordinator.app, client=("203.0.113.9", 4000)), base_url="http://test")


@pytest.mark.anyio
async def test_the_coordinator_serves_each_cuda_platform_and_nothing_else(tmp_path, monkeypatch):
    monkeypatch.setattr(coordinator, "get_base_dir", lambda: str(tmp_path))
    zips = {p: make_cuda_zip(tmp_path / release.cuda_zip_name(p), p, p) for p in release.CUDA_PLATFORMS}
    (tmp_path / "release.json").write_text("{}")
    async with _client() as c:
        for platform, path in zips.items():
            resp = await c.get(f"/v1/download/cuda-bin/{platform}")
            assert resp.status_code == 200 and resp.content == open(path, "rb").read()
        assert (await c.get("/v1/download/cuda-bin/macos-arm64")).status_code == 404       # none shipped for it
        assert (await c.get("/v1/download/cuda-bin/..%2Frelease.json")).status_code == 404
        os.remove(zips["linux-x64"])
        assert (await c.get("/v1/download/cuda-bin/linux-x64")).status_code == 404


@pytest.mark.anyio
async def test_the_status_reports_the_coordinators_own_engine_flavour(monkeypatch):
    monkeypatch.setattr(engine_config, "get_config", lambda force_reload=False: {})
    async with _client() as c:
        assert (await c.get("/v1/cluster/status")).json()["engine_flavour"] == "vulkan"      # the default
        downloader.set_coordinator_engine_flavour("cuda")
        body = (await c.get("/v1/cluster/status")).json()
    assert body["engine_flavour"] == "cuda" and body["server_version"] == coordinator.VERSION   # the version stays plain


# --- the downloader -----------------------------------------------------------------------

class FakeHttp:
    def __init__(self, routes):
        self.routes, self.requested = routes, []

    def __call__(self, url, timeout=None):
        self.requested.append(url)
        if url not in self.routes:
            raise OSError(f"404 {url}")
        return io.BytesIO(self.routes[url])


def _worker_setup(tmp_path, monkeypatch, platform="linux-x64", serve=None, marker="v1", with_cuda=True):
    pem = _pem()
    monkeypatch.setattr(release, "RELEASE_PUBKEY", release.public_key_hex(pem))
    archive, rpc, _server, cuda, m = signed(tmp_path, pem, with_cuda=with_cuda, marker=marker, name=f"bins-{marker}")
    base = tmp_path / "runtime"
    base.mkdir(exist_ok=True)
    routes = {"http://c/v1/release": json.dumps(m).encode(),
              f"http://c/v1/download/rpc-bin/{platform}": open(rpc[platform], "rb").read()}
    if with_cuda:
        routes[f"http://c/v1/download/cuda-bin/{platform}"] = serve if serve is not None else open(cuda[platform], "rb").read()
    return str(base), FakeHttp(routes), m, cuda


def test_the_worker_installs_the_signed_cuda_engine_into_its_own_folder(tmp_path, monkeypatch):
    base, http, m, _cuda = _worker_setup(tmp_path, monkeypatch)
    assert downloader.sync_rpc_bin("http://c", base, "linux-x64", urlopen=http, flavour="cuda") is True
    folder = os.path.join(base, "bin-cuda")
    assert sorted(os.listdir(folder)) == ["ggml-rpc-server", "libcudart.so.13", "libggml-base.so.0", "libggml-cuda.so", "llama-server"]
    assert open(os.path.join(folder, "llama-server")).read() == "server linux-x64-v1"
    assert os.stat(os.path.join(folder, "llama-server")).st_mode & 0o111
    assert os.stat(os.path.join(folder, "ggml-rpc-server")).st_mode & 0o111
    assert json.load(open(os.path.join(base, downloader.CUDA_RECORD))) == {
        "sha256": m["cuda_linux_x64_sha256"], "llama_build": BUILD, "rpc_proto": PROTO, "cuda_version": "13.4"}
    assert not os.path.exists(os.path.join(base, "bin")) and not os.path.exists(os.path.join(base, downloader.RPC_RECORD))
    assert sorted(os.listdir(base)) == [downloader.CUDA_RECORD, "bin-cuda"]       # no download leftover, no staging folder


def test_both_flavours_coexist_and_neither_install_touches_the_other(tmp_path, monkeypatch):
    base, http, _m, _c = _worker_setup(tmp_path, monkeypatch)
    downloader.sync_rpc_bin("http://c", base, "linux-x64", urlopen=http)                          # vulkan, as today
    vulkan_before = open(os.path.join(base, "bin", "ggml-rpc-server")).read()
    downloader.sync_rpc_bin("http://c", base, "linux-x64", urlopen=http, flavour="cuda")
    assert open(os.path.join(base, "bin", "ggml-rpc-server")).read() == vulkan_before == "rpc linux-x64-v1"
    assert os.path.exists(os.path.join(base, "bin-cuda", "libggml-cuda.so"))
    assert not os.path.exists(os.path.join(base, "bin", "libggml-cuda.so"))
    assert os.path.exists(os.path.join(base, downloader.RPC_RECORD)) and os.path.exists(os.path.join(base, downloader.CUDA_RECORD))
    # switching: the flavour no longer run is removed, the other stays
    assert downloader.remove_engine_flavour(base, "vulkan") is True
    assert not os.path.exists(os.path.join(base, "bin")) and not os.path.exists(os.path.join(base, downloader.RPC_RECORD))
    assert os.path.exists(os.path.join(base, "bin-cuda", "llama-server"))
    assert downloader.remove_engine_flavour(base, "vulkan") is False


def test_removing_a_flavour_never_follows_a_symlinked_bin(tmp_path):
    target = tmp_path / "release-build"
    target.mkdir()
    (target / "llama-server").write_text("the release's own")
    base = tmp_path / "box"
    base.mkdir()
    os.symlink(str(target), str(base / "bin"))
    assert downloader.remove_engine_flavour(str(base), "vulkan") is False
    assert (target / "llama-server").exists()


def test_an_installed_cuda_engine_is_not_downloaded_again(tmp_path, monkeypatch):
    base, http, _m, _c = _worker_setup(tmp_path, monkeypatch)
    downloader.sync_rpc_bin("http://c", base, "linux-x64", urlopen=http, flavour="cuda")
    assert downloader.sync_rpc_bin("http://c", base, "linux-x64", urlopen=http, flavour="cuda") is False
    assert http.requested.count("http://c/v1/download/cuda-bin/linux-x64") == 1


def test_a_new_release_replaces_the_installed_cuda_engine_whole(tmp_path, monkeypatch):
    base, http, _m, _c = _worker_setup(tmp_path, monkeypatch)
    downloader.sync_rpc_bin("http://c", base, "linux-x64", urlopen=http, flavour="cuda")
    open(os.path.join(base, "bin-cuda", "leftover-of-v1"), "w").write("x")
    _b, http2, _m2, _c2 = _worker_setup(tmp_path, monkeypatch, marker="v2")
    assert downloader.sync_rpc_bin("http://c", base, "linux-x64", urlopen=http2, flavour="cuda") is True
    assert open(os.path.join(base, "bin-cuda", "llama-server")).read() == "server linux-x64-v2"
    assert not os.path.exists(os.path.join(base, "bin-cuda", "leftover-of-v1"))


def test_the_worker_refuses_a_cuda_zip_that_is_not_the_signed_one_and_leaves_nothing(tmp_path, monkeypatch):
    evil = io.BytesIO()
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("bin/ggml-rpc-server", b"something else")
    base, http, _m, _c = _worker_setup(tmp_path, monkeypatch, serve=evil.getvalue())
    with pytest.raises(release.ReleaseError, match="does not match"):
        downloader.sync_rpc_bin("http://c", base, "linux-x64", urlopen=http, flavour="cuda")
    assert os.listdir(base) == []


def test_a_signed_cuda_zip_with_unexpected_contents_is_still_refused(tmp_path, monkeypatch):
    pem = _pem()
    monkeypatch.setattr(release, "RELEASE_PUBKEY", release.public_key_hex(pem))
    archive, rpc, server, cuda = release_parts(tmp_path)
    odd = make_cuda_zip(tmp_path / "odd.zip", members={"bin/ggml-rpc-server": b"x", "bin/extra.sh": b"x"})
    m = release.make_manifest("2.7.0", archive, "a", BUILT_AT)
    m.update(llama_build=BUILD, rpc_proto=PROTO, cuda_version="13.4", cuda_linux_x64_sha256=release.sha256_file(odd),
             cuda_linux_x64_size=os.path.getsize(odd))
    base = tmp_path / "rt"
    base.mkdir()
    http = FakeHttp({"http://c/v1/release": json.dumps(release.sign_manifest(m, pem)).encode(),
                     "http://c/v1/download/cuda-bin/linux-x64": open(odd, "rb").read()})
    with pytest.raises(release.ReleaseError, match="never ships"):
        downloader.sync_rpc_bin("http://c", str(base), "linux-x64", urlopen=http, flavour="cuda")
    assert os.listdir(base) == []


def test_a_release_without_cuda_is_refused_by_name_and_the_vulkan_engine_is_untouched(tmp_path, monkeypatch):
    base, http, _m, _c = _worker_setup(tmp_path, monkeypatch, with_cuda=False)
    with pytest.raises(release.ReleaseError, match="no CUDA engine for linux-x64"):
        downloader.sync_rpc_bin("http://c", base, "linux-x64", urlopen=http, flavour="cuda")
    assert downloader.sync_rpc_bin("http://c", base, "linux-x64", urlopen=http) is True       # vulkan: exactly as before
    assert os.path.exists(os.path.join(base, "bin", "ggml-rpc-server"))


def test_the_cuda_download_waits_for_enough_disk(tmp_path, monkeypatch):
    base, http, _m, _c = _worker_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(downloader.shutil, "disk_usage", lambda path: type("U", (), {"free": 1024})())
    with pytest.raises(release.ReleaseError, match="not enough free disk space"):
        downloader.sync_rpc_bin("http://c", base, "linux-x64", urlopen=http, flavour="cuda")
    assert "http://c/v1/download/cuda-bin/linux-x64" not in http.requested


def test_the_coordinators_own_host_installs_cuda_from_the_zip_beside_the_code(tmp_path, monkeypatch):
    pem = _pem()
    monkeypatch.setattr(release, "RELEASE_PUBKEY", release.public_key_hex(pem))
    _a, _r, _s, cuda, m = signed(tmp_path, pem)
    base = tmp_path / "box"
    base.mkdir()
    (base / "release.json").write_text(json.dumps(m))
    with pytest.raises(release.ReleaseError, match="CUDA archive"):                           # signed, but not here yet
        downloader.sync_local_cuda_bin(str(base), "linux-x64")
    (base / "cuda-bin-linux-x64.zip").write_bytes(open(cuda["linux-x64"], "rb").read())
    assert downloader.sync_local_cuda_bin(str(base), "linux-x64") is True
    assert (base / "bin-cuda" / "llama-server").read_text() == "server linux-x64-v1"
    assert downloader.sync_local_cuda_bin(str(base), "linux-x64") is False
    (base / "release.json").write_text(json.dumps({k: v for k, v in m.items() if not k.startswith("cuda_")}))
    with pytest.raises(release.ReleaseError, match="failed verification"):                    # edited manifest: signature gone
        downloader.sync_local_cuda_bin(str(base), "linux-x64")


@pytest.mark.parametrize("value,expected", [("cuda", "cuda"), (" CUDA ", "cuda"), ("vulkan", "vulkan"), ("", "vulkan"),
                                            (None, "vulkan"), ("rocm", "vulkan"), (3, "vulkan")])
def test_any_flavour_but_cuda_is_vulkan(value, expected):
    assert downloader.normalize_flavour(value) == expected


def _engine_env(tmp_path, monkeypatch, allowed_gpu="-1", platform="linux-x64"):
    monkeypatch.setattr("engine.config.get_base_dir", lambda: str(tmp_path))
    monkeypatch.setattr("engine.config.get_worker_limits", lambda: {"allowed_gpu_indices": allowed_gpu})
    monkeypatch.setattr(release, "host_cuda_platform", lambda *a, **k: platform)
    monkeypatch.setattr(downloader, "test_binary_compatibility", lambda path, **k: True)
    calls = []
    monkeypatch.setattr(downloader, "sync_rpc_bin", lambda *a, **k: calls.append(("rpc", a, k)))
    monkeypatch.setattr(downloader, "sync_local_cuda_bin", lambda *a, **k: calls.append(("local", a, k)))
    return calls


def _fake_engine(tmp_path, folder, name="ggml-rpc-server"):
    (tmp_path / folder).mkdir(exist_ok=True)
    (tmp_path / folder / name).write_text("x")
    return str(tmp_path / folder / name)


def test_a_worker_asking_for_cuda_gets_the_cuda_programs_and_the_coordinator_is_asked_for_them(tmp_path, monkeypatch):
    calls = _engine_env(tmp_path, monkeypatch)
    _fake_engine(tmp_path, "bin")                                   # the Vulkan one is there too and must not be returned
    cuda_path = _fake_engine(tmp_path, "bin-cuda")
    assert downloader.download_and_extract_engine("worker", coordinator_url="http://c/", flavour="cuda") == cuda_path
    assert calls == [("rpc", ("http://c", str(tmp_path), "linux-x64"), {"flavour": "cuda"})]


def test_the_coordinators_own_host_uses_the_local_zip(tmp_path, monkeypatch):
    calls = _engine_env(tmp_path, monkeypatch)
    cuda_path = _fake_engine(tmp_path, "bin-cuda", "llama-server")
    assert downloader.download_and_extract_engine("coordinator", flavour="cuda") == cuda_path
    assert calls == [("local", (str(tmp_path), "linux-x64"), {})]


def test_cuda_never_answers_with_the_vulkan_engine(tmp_path, monkeypatch):
    _engine_env(tmp_path, monkeypatch)
    _fake_engine(tmp_path, "bin")                                   # only Vulkan installed
    assert downloader.download_and_extract_engine("worker", coordinator_url="http://c", flavour="cuda") is None
    cuda_path = _fake_engine(tmp_path, "bin-cuda")
    monkeypatch.setattr(downloader, "test_binary_compatibility", lambda path, **k: False)       # will not start
    assert downloader.download_and_extract_engine("worker", coordinator_url="http://c", flavour="cuda") is None
    assert os.path.exists(cuda_path)


def test_cuda_is_skipped_on_a_cpu_only_worker_and_on_a_platform_without_it(tmp_path, monkeypatch):
    calls = _engine_env(tmp_path, monkeypatch, allowed_gpu="none")
    _fake_engine(tmp_path, "bin-cuda")
    assert downloader.download_and_extract_engine("worker", coordinator_url="http://c", flavour="cuda") is None
    calls = _engine_env(tmp_path, monkeypatch, platform=None)
    assert downloader.download_and_extract_engine("worker", coordinator_url="http://c", flavour="cuda") is None
    assert calls == []


def test_vulkan_is_the_default_and_unknown_flavours_are_vulkan(tmp_path, monkeypatch):
    calls = _engine_env(tmp_path, monkeypatch)
    vulkan = _fake_engine(tmp_path, "bin")
    _fake_engine(tmp_path, "bin-cuda")
    for flavour in ({}, {"flavour": "vulkan"}, {"flavour": "rocm"}, {"flavour": None}):
        assert downloader.download_and_extract_engine("worker", coordinator_url="http://c", **flavour) == vulkan
    assert len(calls) == 4 and all(kind == "rpc" and k == {} for kind, _a, k in calls)   # the Vulkan sync, called as it always was


# --- ENGINE_FLAVOR ------------------------------------------------------------------------

def _limits(monkeypatch, values):
    monkeypatch.setattr(engine_config, "get_config", lambda force_reload=False: dict(values))
    return engine_config.get_worker_limits()


def test_engine_flavor_is_vulkan_by_default(monkeypatch):
    assert _limits(monkeypatch, {})["engine_flavor"] == "vulkan"
    assert engine_config.ENGINE_DEFAULTS["engine_flavor"] == "vulkan"


@pytest.mark.parametrize("raw,expected", [
    ("auto", "auto"), ("vulkan", "vulkan"), ("cuda", "cuda"), (" CUDA ", "cuda"), ("Auto", "auto"),
    # a typo must not start a different engine
    ("cuda13", "vulkan"), ("rocm", "vulkan"), ("", "vulkan"), ("true", "vulkan"),
])
def test_engine_flavor_is_one_of_the_known_flavours(monkeypatch, raw, expected):
    assert _limits(monkeypatch, {"ENGINE_FLAVOR": raw})["engine_flavor"] == expected


def _capability(monkeypatch, **answer):
    from engine import hardware
    monkeypatch.setattr(hardware, "cuda_capability", lambda: answer, raising=False)
    monkeypatch.setattr(release, "host_cuda_platform", lambda *a, **k: "linux-x64")


def test_vulkan_never_asks_the_hardware(monkeypatch):
    from engine import hardware
    monkeypatch.setattr(hardware, "cuda_capability", lambda: pytest.fail("vulkan must not detect"), raising=False)
    assert downloader.resolve_engine_flavour("vulkan") == "vulkan"
    assert downloader.resolve_engine_flavour("rocm") == "vulkan"
    assert downloader.resolve_engine_flavour(None) == "vulkan"


def test_auto_is_cuda_exactly_where_a_worker_would_be_allowed_it(monkeypatch):
    _capability(monkeypatch, capable=True, reason="", gpu="NVIDIA GeForce RTX 4070 Laptop GPU", compute_cap="8.9", driver="615.78")
    assert downloader.resolve_engine_flavour("auto") == "cuda"
    _capability(monkeypatch, capable=False, reason="no NVIDIA GPU", gpu="", compute_cap="", driver="")
    assert downloader.resolve_engine_flavour("auto") == "vulkan"


def test_auto_stays_on_vulkan_when_the_detection_is_missing_or_fails(monkeypatch):
    from engine import hardware
    monkeypatch.setattr(release, "host_cuda_platform", lambda *a, **k: "linux-x64")
    monkeypatch.delattr(hardware, "cuda_capability", raising=False)                         # before the worker part is merged
    assert downloader.resolve_engine_flavour("auto") == "vulkan"
    def explode():
        raise OSError("nvidia-smi")
    monkeypatch.setattr(hardware, "cuda_capability", explode, raising=False)
    assert downloader.resolve_engine_flavour("auto") == "vulkan"
    monkeypatch.setattr(hardware, "cuda_capability", lambda: "nonsense", raising=False)
    assert downloader.resolve_engine_flavour("auto") == "vulkan"


def test_auto_is_vulkan_on_a_platform_with_no_cuda_engine(monkeypatch):
    _capability(monkeypatch, capable=True)
    monkeypatch.setattr(release, "host_cuda_platform", lambda *a, **k: None)
    assert downloader.resolve_engine_flavour("auto") == "vulkan"


def test_an_explicit_cuda_is_refused_on_a_host_that_plainly_cannot_and_trusted_when_nothing_can_be_checked(monkeypatch):
    _capability(monkeypatch, capable=False, reason="this GPU is older than Turing (compute capability 6.1)")
    assert downloader.resolve_engine_flavour("cuda") == "vulkan"
    from engine import hardware
    monkeypatch.delattr(hardware, "cuda_capability", raising=False)
    assert downloader.resolve_engine_flavour("cuda") == "cuda"
    _capability(monkeypatch, capable=True)
    assert downloader.resolve_engine_flavour("cuda") == "cuda"


def test_the_coordinators_flavour_is_decided_once_and_a_fallback_sticks(monkeypatch):
    _capability(monkeypatch, capable=True)
    monkeypatch.setattr(engine_config, "get_worker_limits", lambda: {"engine_flavor": "auto"})
    assert downloader.coordinator_engine_flavour() == "cuda"
    from engine import hardware
    monkeypatch.setattr(hardware, "cuda_capability", lambda: pytest.fail("asked again"), raising=False)
    assert downloader.coordinator_engine_flavour() == "cuda"
    downloader.set_coordinator_engine_flavour("vulkan")
    assert downloader.coordinator_engine_flavour() == "vulkan"


# --- the coordinator's engine binary ------------------------------------------------------

def test_the_vulkan_flavour_selects_the_binary_exactly_as_it_always_did(monkeypatch):
    calls = []
    monkeypatch.setattr(inference_manager, "coordinator_engine_flavour", lambda: "vulkan")
    monkeypatch.setattr(inference_manager, "download_and_extract_engine", lambda *a, **k: calls.append((a, k)) or "/bin/llama-server")
    assert inference_manager.select_coordinator_engine() == "/bin/llama-server"
    assert calls == [(("coordinator",), {})]


def test_the_cuda_flavour_selects_the_cuda_binary(monkeypatch):
    calls = []
    monkeypatch.setattr(inference_manager, "coordinator_engine_flavour", lambda: "cuda")
    monkeypatch.setattr(inference_manager, "download_and_extract_engine", lambda *a, **k: calls.append((a, k)) or "/bin-cuda/llama-server")
    assert inference_manager.select_coordinator_engine() == "/bin-cuda/llama-server"
    assert calls == [(("coordinator",), {"flavour": "cuda"})]


def test_a_cuda_engine_that_cannot_be_had_runs_on_vulkan_and_the_status_says_so(monkeypatch):
    calls = []

    def fake(role, coordinator_url=None, flavour="vulkan"):
        calls.append(flavour)
        return None if flavour == "cuda" else "/bin/llama-server"

    downloader.set_coordinator_engine_flavour("cuda")
    monkeypatch.setattr(inference_manager, "download_and_extract_engine", fake)
    assert inference_manager.select_coordinator_engine() == "/bin/llama-server"
    assert calls == ["cuda", "vulkan"]
    assert downloader.coordinator_engine_flavour() == "vulkan"


def test_media_gpu_looks_at_the_folder_of_the_flavour_that_runs(tmp_path, monkeypatch):
    """MEDIA_GPU names Vulkan devices; a CUDA engine (its bin-cuda holds ggml-cuda) must not take it."""
    monkeypatch.setattr(inference_manager, "get_base_dir", lambda: str(tmp_path))
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "libggml-vulkan.so").write_text("")
    (tmp_path / "bin-cuda").mkdir()
    (tmp_path / "bin-cuda" / "libggml-cuda.so").write_text("")
    downloader.set_coordinator_engine_flavour("vulkan")
    assert inference_manager.engine_build_is_vulkan() is True
    downloader.set_coordinator_engine_flavour("cuda")
    assert inference_manager.engine_build_is_vulkan() is False
