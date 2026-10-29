"""
The Windows RPC zips carry the Visual C++ runtime beside the programs (scripts/package_llama.py),
because a clean Windows has none and the programs exit 0xC0000135 without it.
"""

import importlib.util
import os
import zipfile

import pytest

from engine import release

_SPEC = importlib.util.spec_from_file_location(
    "package_llama_vcrt", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "package_llama.py"))
packager = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(packager)


def _pe(machine):
    header = bytearray(0x80)
    header[:2] = b"MZ"
    header[0x3C:0x40] = (0x40).to_bytes(4, "little")
    header[0x40:0x44] = b"PE\0\0"
    header[0x44:0x46] = machine.to_bytes(2, "little")
    return bytes(header)


def test_pe_machine_reads_the_architecture():
    assert packager._pe_machine(_pe(0x8664)) == 0x8664
    assert packager._pe_machine(_pe(0xAA64)) == 0xAA64
    assert packager._pe_machine(b"not a program") == -1


def test_only_the_pinned_installer_is_accepted(tmp_path):
    other = tmp_path / "VC_redist.x64.exe"
    other.write_bytes(b"something else")
    with pytest.raises(packager.PackageError, match="not the pinned Visual C"):
        packager.check_vcredist(str(other))
    with pytest.raises(packager.PackageError):
        packager.check_vcredist(str(tmp_path / "missing.exe"))


def test_the_pin_is_a_content_addressed_microsoft_download():
    assert packager.VCREDIST_URL.startswith("https://download.visualstudio.microsoft.com/")
    assert packager.VCREDIST_SHA256.upper() in packager.VCREDIST_URL
    assert len(packager.VCREDIST_SHA256) == 64


def _sources(tmp_path, build="b1"):
    names = {pattern.format(b=build) for pattern in packager.SOURCES.values()}
    paths = {}
    for name in names:
        path = tmp_path / name
        if name.endswith(".zip"):
            with zipfile.ZipFile(path, "w") as z:
                z.writestr("ggml-rpc-server.exe" if "win" in name else "x", b"rpc")
                z.writestr("ggml.dll", b"lib")
        else:
            import tarfile, io
            with tarfile.open(path, "w:gz") as t:
                data = b"rpc"
                info = tarfile.TarInfo("ggml-rpc-server")
                info.size = len(data)
                t.addfile(info, io.BytesIO(data))
        paths[name] = str(path)
    server = packager.SERVER_SOURCE.format(b=build)
    paths.setdefault(server, str(tmp_path / server))
    return paths


def test_windows_zips_carry_the_runtime_and_the_others_do_not(tmp_path, monkeypatch):
    monkeypatch.setattr(packager, "check_vcredist", lambda path: path)
    monkeypatch.setattr(packager, "vcruntime_members",
                        lambda path, platform: [(dll, f"{platform}:{dll}".encode()) for dll in packager.VCRUNTIME_PLATFORMS[platform][2]])
    out = tmp_path / "out"
    packager.package("b1", _sources(tmp_path), str(out), rpc_proto="8.0.1", vcredist="fake.exe")
    for platform, needed in (("win-x64", {"vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll"}),
                             ("win-arm64", {"vcruntime140.dll", "msvcp140.dll"})):
        with zipfile.ZipFile(out / release.rpc_zip_name(platform)) as z:
            names = z.namelist()
            assert {f"bin/{n}" for n in needed} <= set(names)
            assert z.read("bin/msvcp140.dll") == f"{platform}:msvcp140.dll".encode()
    for platform in ("linux-x64", "macos-arm64", "linux-arm64", "macos-x64"):
        with zipfile.ZipFile(out / release.rpc_zip_name(platform)) as z:
            assert not any("140" in name for name in z.namelist())


def test_a_windows_build_without_the_runtime_is_refused(tmp_path):
    with pytest.raises(packager.PackageError, match="needs the Visual C\\+\\+ runtime"):
        packager.package("b1", _sources(tmp_path), str(tmp_path / "out"), rpc_proto="8.0.1")


def test_the_zip_is_the_same_bytes_every_time(tmp_path, monkeypatch):
    monkeypatch.setattr(packager, "check_vcredist", lambda path: path)
    monkeypatch.setattr(packager, "vcruntime_members", lambda path, platform: [(d, b"x" * 10) for d in packager.VCRUNTIME_DLLS])
    sources = _sources(tmp_path)
    packager.package("b1", sources, str(tmp_path / "a"), rpc_proto="8.0.1", vcredist="f")
    packager.package("b1", sources, str(tmp_path / "b"), rpc_proto="8.0.1", vcredist="f")
    name = release.rpc_zip_name("win-x64")
    assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()
