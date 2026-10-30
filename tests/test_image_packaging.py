"""
Shipping image generation (2.6.1): the pinned stable-diffusion.cpp packaged by scripts/package_sd.py, signed into the
manifest (sd_build, server_sd_sha256, server_sd_size) by scripts/release.py, uploaded and unpacked into <root>/bin-sd/ by
scripts/release_apply.py; the three model pins and the verified downloader; the config keys. Workers never see any of it.
"""

import gzip
import hashlib
import importlib.util
import io
import json
import os
import stat
import tarfile
import zipfile

import pytest

from engine import config as engine_config
from engine import image_member, release
from tests.test_release_rpc_bin import (  # noqa: F401 - fixtures and helpers shared with the llama packaging tests
    BUILD, BUILT_AT, FakeRemote, Recorder, _deploy, _pem, apply_mod, assets_dir, cli, host, make_repo, packaged, pem,
    run_apply, signed_release,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SD_BUILD = "master-1017-be48649"


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(REPO_ROOT, "scripts", filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


package_sd = _load("package_sd_t", "package_sd.py")
fetch = _load("fetch_image_models_t", "fetch_image_models.py")


# --- scripts/package_sd.py -------------------------------------------------------------------------------------------

MEMBERS = {
    "build/bin/sd-server": b"sd-server binary",
    "build/bin/sd-cli": b"sd-cli binary",
    "build/bin/libggml.so": b"ggml",
    "build/bin/libggml-base.so": b"base",
    "build/bin/libggml-vulkan.so": b"vulkan",
    "build/bin/libstable-diffusion.so": b"sd lib",
    "build/bin/libwebp.so.7": b"webp",
    "build/README.md": b"read me",
    "build/bin/LICENSE": b"licence",
}


def make_zip(path, members=None):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in (members or MEMBERS).items():
            z.writestr(name, data)
    return str(path)


def pack(tmp_path, name="out", members=None, zip_name="asset.zip"):
    zip_path = make_zip(tmp_path / zip_name, members)
    out = str(tmp_path / name)
    return out, package_sd.package(zip_path, out, SD_BUILD, hashlib.sha256(open(zip_path, "rb").read()).hexdigest())


def test_the_pins_are_the_ones_measured_on_thewall():
    assert package_sd.SD_BUILD == SD_BUILD
    assert package_sd.ASSET == "sd-master-be48649-bin-Linux-Ubuntu-24.04-x86_64-vulkan.zip"
    assert package_sd.ASSET_SHA256 == "794c913d077aaf72025e772b2acfb6a30f49156e92d072461ae15ce958d95c14"
    assert package_sd.UPSTREAM == "leejet/stable-diffusion.cpp"


def test_the_package_holds_sd_server_and_its_libraries_flat_and_not_sd_cli(tmp_path):
    out, info = pack(tmp_path)
    assert sorted(os.listdir(out)) == ["build.json", "server-sd.tar.gz"]
    with tarfile.open(os.path.join(out, "server-sd.tar.gz")) as tar:
        names = [m.name for m in tar.getmembers()]
        modes = {m.name: m.mode for m in tar.getmembers()}
        stamps = {(m.mtime, m.uid, m.gid) for m in tar.getmembers()}
        assert tar.extractfile("sd-server").read() == b"sd-server binary"
    assert names == sorted(["sd-server", "libggml.so", "libggml-base.so", "libggml-vulkan.so", "libstable-diffusion.so",
                            "libwebp.so.7", "LICENSE"])
    assert modes["sd-server"] == 0o755 and modes["libggml.so"] == 0o755 and modes["LICENSE"] == 0o644
    assert stamps == {(0, 0, 0)}
    assert json.load(open(os.path.join(out, "build.json"))) == info
    assert info["sd_build"] == SD_BUILD and info["server_sd"] == "server-sd.tar.gz"


def test_packaging_twice_gives_the_same_bytes(tmp_path):
    one, _ = pack(tmp_path, "one")
    two, _ = pack(tmp_path, "two", zip_name="again.zip")
    assert (open(os.path.join(one, "server-sd.tar.gz"), "rb").read() == open(os.path.join(two, "server-sd.tar.gz"), "rb").read())


def test_a_download_that_is_not_the_pinned_asset_is_refused_and_nothing_is_written(tmp_path):
    zip_path = make_zip(tmp_path / "asset.zip")
    out = tmp_path / "out"
    with pytest.raises(package_sd.PackageError, match="not the pinned asset"):
        package_sd.package(zip_path, str(out), SD_BUILD)                    # the real pin is not this file's hash
    with pytest.raises(package_sd.PackageError, match="not the pinned asset"):
        package_sd.package(str(tmp_path / "missing.zip"), str(out), SD_BUILD)
    assert not out.exists()


@pytest.mark.parametrize("members, words", [
    ({"build/bin/libggml.so": b"x"}, "no sd-server"),
    ({"build/bin/sd-server": b"x"}, "none of the libraries"),
    ({"a/sd-server": b"x", "b/sd-server": b"y", "a/libggml.so": b"z"}, "twice"),
])
def test_an_asset_that_is_not_the_engine_is_refused(tmp_path, members, words):
    zip_path = make_zip(tmp_path / "asset.zip", members)
    with pytest.raises(package_sd.PackageError, match=words):
        package_sd.package(zip_path, str(tmp_path / "out"), SD_BUILD, hashlib.sha256(open(zip_path, "rb").read()).hexdigest())


# --- the manifest ---------------------------------------------------------------------------------------------------

def make_release(tmp_path, pem, with_sd=True, with_llama=False, version="2.1.0"):
    archive = str(tmp_path / f"src-{version}.zip")
    release.build_source_archive(make_repo(tmp_path, version), archive)
    sd_dir, _info = pack(tmp_path, "sd-assets")
    sd = os.path.join(sd_dir, "server-sd.tar.gz")
    manifest = release.make_manifest(version, archive, "abc1234", BUILT_AT, "n",
                                     sd_build=SD_BUILD if with_sd else None, server_sd=sd if with_sd else None)
    return archive, sd, release.sign_manifest(manifest, pem)


def test_the_manifest_signs_the_sd_build_and_its_archive(tmp_path, pem):
    _archive, sd, m = make_release(tmp_path, pem)
    assert m["sd_build"] == SD_BUILD and m["server_sd_sha256"] == release.sha256_file(sd) and m["server_sd_size"] == os.path.getsize(sd)
    assert release.verify_manifest(m, release.public_key_hex(pem)) == (True, "ok")
    assert release.verify_server_sd(m, sd) == (True, "ok")
    assert "llama_build" not in m                                      # the image engine asks nothing of the llama fields


def test_a_release_without_it_is_unchanged(tmp_path, pem):
    _archive, _sd, m = make_release(tmp_path, pem, with_sd=False)
    assert not any(key in m for key in release.SD_FIELDS)
    assert release.verify_manifest(m, release.public_key_hex(pem)) == (True, "ok")
    assert release.verify_server_sd(m, "x")[0] is False


@pytest.mark.parametrize("field, value", [("sd_build", "other"), ("server_sd_sha256", "1" * 64), ("server_sd_size", 3)])
def test_changing_an_sd_field_breaks_the_signature(tmp_path, pem, field, value):
    _a, _sd, m = make_release(tmp_path, pem)
    ok, reason = release.verify_manifest(dict(m, **{field: value}), release.public_key_hex(pem))
    assert not ok and "signature" in reason


@pytest.mark.parametrize("patch, pattern", [
    ({"sd_build": "bad build!"}, "sd_build"), ({"sd_build": ""}, "sd_build"), ({"sd_build": 5}, "sd_build"),
    ({"server_sd_sha256": "XYZ"}, "server_sd_sha256"), ({"server_sd_size": 0}, "server_sd_size"),
    ({"server_sd_size": True}, "server_sd_size"), ({"server_sd_size": "9"}, "server_sd_size"),
])
def test_malformed_sd_fields_are_rejected_by_shape(tmp_path, pem, patch, pattern):
    _a, _sd, m = make_release(tmp_path, pem)
    ok, reason = release.verify_manifest(dict(m, **patch), release.public_key_hex(pem))
    assert not ok and pattern in reason


def test_half_of_the_sd_fields_is_rejected(tmp_path, pem):
    _a, _sd, m = make_release(tmp_path, pem)
    for missing in release.SD_FIELDS:
        lone = {k: v for k, v in m.items() if k != missing}
        assert release.verify_manifest(lone, release.public_key_hex(pem))[0] is False


def test_the_sd_archive_needs_its_build_and_the_build_its_archive(tmp_path):
    sd_dir, _ = pack(tmp_path)
    sd = os.path.join(sd_dir, "server-sd.tar.gz")
    with pytest.raises(release.ReleaseError, match="sd build"):
        release.make_manifest("2.1.0", sd, "a", BUILT_AT, server_sd=sd)
    with pytest.raises(release.ReleaseError, match="sd build"):
        release.make_manifest("2.1.0", sd, "a", BUILT_AT, sd_build=SD_BUILD)
    with pytest.raises(release.ReleaseError, match="sd build"):
        release.make_manifest("2.1.0", sd, "a", BUILT_AT, sd_build="bad one", server_sd=sd)


def test_a_substituted_sd_archive_is_caught(tmp_path, pem):
    _a, sd, m = make_release(tmp_path, pem)
    open(sd, "ab").write(b"x")
    ok, reason = release.verify_server_sd(m, sd)
    assert not ok and "size" in reason


# --- scripts/release.py ------------------------------------------------------------------------------------------------

def test_the_sd_package_is_optional_unless_named(tmp_path):
    assert cli.load_sd_assets(str(tmp_path / "absent")) is None
    with pytest.raises(cli.CliError, match="package_sd"):
        cli.load_sd_assets(str(tmp_path / "absent"), required=True)
    d, _ = pack(tmp_path)
    got = cli.load_sd_assets(d)
    assert got == {"sd_build": SD_BUILD, "server_sd": os.path.join(d, "server-sd.tar.gz")}
    os.remove(os.path.join(d, "server-sd.tar.gz"))
    with pytest.raises(cli.CliError, match="incomplete"):
        cli.load_sd_assets(d)


def test_the_cli_signs_sd_when_it_is_packaged_and_builds_as_before_when_it_is_not(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "DEFAULT_SD_ASSETS", str(tmp_path / "no-sd-yet"))
    bins = cli._args_binaries(cli.build_parser().parse_args(["build", "--binaries", packaged(tmp_path)]))
    assert bins["llama_build"] == BUILD and "sd_build" not in bins

    d, _ = pack(tmp_path)
    monkeypatch.setattr(cli, "DEFAULT_SD_ASSETS", d)
    bins = cli._args_binaries(cli.build_parser().parse_args(["build", "--binaries", packaged(tmp_path, "pkg2")]))
    assert bins["sd_build"] == SD_BUILD and bins["llama_build"] == BUILD


def test_no_binaries_leaves_the_image_engine_out_too_and_a_named_missing_folder_is_an_error(tmp_path, monkeypatch):
    d, _ = pack(tmp_path)
    monkeypatch.setattr(cli, "DEFAULT_SD_ASSETS", d)
    assert cli._args_binaries(cli.build_parser().parse_args(["build", "--no-binaries"])) is None
    with pytest.raises(cli.CliError, match="package_sd"):
        cli._args_binaries(cli.build_parser().parse_args(
            ["build", "--binaries", packaged(tmp_path), "--sd-assets", str(tmp_path / "nope")]))
    named = cli._args_binaries(cli.build_parser().parse_args(["build", "--binaries", packaged(tmp_path, "p3"), "--sd-assets", d]))
    assert named["sd_build"] == SD_BUILD


def test_build_signs_and_verifies_the_sd_archive(tmp_path, monkeypatch):
    key = _pem()
    monkeypatch.setattr(cli, "check_tree", lambda repo, allow_dirty: "abc1234")
    d, _ = pack(tmp_path)
    bins = {**cli.load_binaries(packaged(tmp_path)), **cli.load_sd_assets(d)}
    built = cli.build_release(make_repo(tmp_path, "2.1.0"), str(tmp_path / "out"), key, "n",
                              expected_pubkey=release.public_key_hex(key), binaries=bins)
    m = json.load(open(built["manifest_path"]))
    assert release.verify_manifest(m, release.public_key_hex(key)) == (True, "ok")
    assert m["sd_build"] == SD_BUILD and release.verify_server_sd(m, bins["server_sd"]) == (True, "ok")
    assert m["llama_build"] == BUILD


def test_build_without_sd_has_no_sd_fields(tmp_path, monkeypatch):
    key = _pem()
    monkeypatch.setattr(cli, "check_tree", lambda repo, allow_dirty: "abc1234")
    built = cli.build_release(make_repo(tmp_path, "2.1.0"), str(tmp_path / "out"), key, "n",
                              expected_pubkey=release.public_key_hex(key), binaries=cli.load_binaries(packaged(tmp_path)))
    assert not any(k in built["manifest"] for k in release.SD_FIELDS)


def test_deploy_uploads_and_verifies_the_sd_archive(tmp_path, monkeypatch):
    remote = FakeRemote(tmp_path)
    d, _ = pack(tmp_path)
    _deploy(tmp_path, monkeypatch, remote, {**cli.load_binaries(packaged(tmp_path)), **cli.load_sd_assets(d)})
    names = {u.rsplit("/", 1)[1] for u in remote.uploads}
    assert "server-sd.tar.gz" in names and "server-llama.tar.gz" in names
    checked = {c.split()[1].rsplit("/", 1)[1] for c in remote.commands if c.startswith("sha256sum")}
    assert "server-sd.tar.gz" in checked
    assert "--assets /box/.releases/incoming" in next(c for c in remote.commands if "release_apply.py apply" in c)


def test_a_corrupted_sd_upload_never_reaches_the_installer(tmp_path, monkeypatch):
    remote = FakeRemote(tmp_path, corrupt=("server-sd.tar.gz",))
    d, _ = pack(tmp_path)
    with pytest.raises(cli.CliError, match="server-sd.tar.gz arrived"):
        _deploy(tmp_path, monkeypatch, remote, {**cli.load_binaries(packaged(tmp_path)), **cli.load_sd_assets(d)})
    assert not any("release_apply.py apply" in c for c in remote.commands)


def test_deploy_without_sd_uploads_none(tmp_path, monkeypatch):
    remote = FakeRemote(tmp_path)
    _deploy(tmp_path, monkeypatch, remote, cli.load_binaries(packaged(tmp_path)))
    assert not any(u.endswith("server-sd.tar.gz") for u in remote.uploads)


# --- scripts/release_apply.py ----------------------------------------------------------------------------------------

def sd_release(tmp_path, pem, host, version="2.1.0", marker="v1"):
    """A release signing the llama.cpp binaries and an sd archive made with ``marker``; returns what apply needs."""
    archive, zips, srv, _m = signed_release(tmp_path, pem, version=version, marker=marker)
    members = dict(MEMBERS)
    members["build/bin/sd-server"] = f"sd-server {marker}".encode()
    zip_path = make_zip(tmp_path / f"asset-{marker}.zip", members)
    out = str(tmp_path / f"sd-{marker}")
    package_sd.package(zip_path, out, SD_BUILD, hashlib.sha256(open(zip_path, "rb").read()).hexdigest())
    sd = os.path.join(out, "server-sd.tar.gz")
    manifest = release.sign_manifest(release.make_manifest(
        version, archive, "abc1234", BUILT_AT, "n", rpc_zips=zips, llama_build=BUILD, rpc_proto="8.0.1", server_llama=srv,
        sd_build=SD_BUILD, server_sd=sd), pem)
    incoming = assets_dir(tmp_path, zips, srv)
    with open(os.path.join(incoming, "server-sd.tar.gz"), "wb") as handle:
        handle.write(open(sd, "rb").read())
    return archive, manifest, incoming, sd


def test_the_installer_unpacks_the_sd_engine_into_bin_sd_beside_llama_and_not_into_it(tmp_path, pem, host):
    archive, m, incoming, _sd = sd_release(tmp_path, pem, host)
    run_apply(host, archive, m, assets_dir=incoming)
    sd_dir = os.path.join(host, "bin-sd")
    assert sorted(os.listdir(sd_dir)) == sorted([".installed-from", "sd-server", "libggml.so", "libggml-base.so", "libggml-vulkan.so",
                                                 "libstable-diffusion.so", "libwebp.so.7", "LICENSE"])
    assert open(os.path.join(sd_dir, "sd-server")).read() == "sd-server v1"
    assert os.stat(os.path.join(sd_dir, "sd-server")).st_mode & stat.S_IXUSR
    assert open(os.path.join(sd_dir, ".installed-from")).read().strip() == m["server_sd_sha256"]
    assert not any(name.startswith("sd") or "stable" in name for name in os.listdir(os.path.join(host, "llama", BUILD, f"llama-{BUILD}")))
    assert "sd-server" not in os.listdir(os.path.join(host, "bin"))          # llama.cpp's folder is not touched
    assert not [n for n in os.listdir(host) if ".partial" in n or ".old-" in n]


def test_the_installer_is_what_image_member_looks_for(tmp_path, pem, host):
    archive, m, incoming, _sd = sd_release(tmp_path, pem, host)
    run_apply(host, archive, m, assets_dir=incoming)
    member = image_member.ImageMember(limits=lambda: {}, base_dir=lambda: host)
    assert member.binary() == os.path.join(host, "bin-sd", "sd-server") and os.access(member.binary(), os.X_OK)


def test_a_release_that_signs_the_same_engine_again_needs_no_new_upload(tmp_path, pem, host):
    archive, m, incoming, _sd = sd_release(tmp_path, pem, host)
    run_apply(host, archive, m, assets_dir=incoming)
    os.remove(os.path.join(incoming, "server-sd.tar.gz"))
    marker = os.stat(os.path.join(host, "bin-sd", "sd-server")).st_ino
    archive2, zips, srv, _m = signed_release(tmp_path, pem, version="2.1.1", name="bins2")
    m2 = release.sign_manifest(release.make_manifest(
        "2.1.1", archive2, "abc1234", BUILT_AT, "n", rpc_zips=zips, llama_build=BUILD, rpc_proto="8.0.1", server_llama=srv,
        sd_build=SD_BUILD, server_sd=_sd), pem)
    run_apply(host, archive2, m2, assets_dir=assets_dir(tmp_path, zips, srv))
    assert open(os.path.join(host, "bin-sd", "sd-server")).read() == "sd-server v1"
    assert os.stat(os.path.join(host, "bin-sd", "sd-server")).st_ino == marker          # not unpacked again


def test_a_new_engine_replaces_the_old_one_whole(tmp_path, pem, host):
    archive, m, incoming, _ = sd_release(tmp_path, pem, host, marker="v1")
    run_apply(host, archive, m, assets_dir=incoming)
    open(os.path.join(host, "bin-sd", "stale.so"), "w").write("left over")
    archive2, m2, incoming2, _ = sd_release(tmp_path, pem, host, version="2.1.1", marker="v2")
    run_apply(host, archive2, m2, assets_dir=incoming2)
    assert open(os.path.join(host, "bin-sd", "sd-server")).read() == "sd-server v2"
    assert "stale.so" not in os.listdir(os.path.join(host, "bin-sd"))


def test_a_signed_engine_that_was_not_uploaded_and_is_not_installed_stops_the_install_before_it_starts(tmp_path, pem, host):
    archive, m, incoming, _ = sd_release(tmp_path, pem, host)
    os.remove(os.path.join(incoming, "server-sd.tar.gz"))
    before = apply_mod.installed_version(host)
    with pytest.raises(apply_mod.ApplyError, match="image engine"):
        run_apply(host, archive, m, assets_dir=incoming)
    assert apply_mod.installed_version(host) == before and not os.path.exists(os.path.join(host, "bin-sd"))


def test_an_sd_archive_the_manifest_does_not_sign_is_refused(tmp_path, pem, host):
    archive, zips, srv, m = signed_release(tmp_path, pem)
    incoming = assets_dir(tmp_path, zips, srv)
    pack_dir, _ = pack(tmp_path)
    open(os.path.join(incoming, "server-sd.tar.gz"), "wb").write(open(os.path.join(pack_dir, "server-sd.tar.gz"), "rb").read())
    with pytest.raises(apply_mod.ApplyError, match="does not sign it"):
        run_apply(host, archive, m, assets_dir=incoming)


def test_an_sd_archive_that_is_not_the_signed_one_is_refused(tmp_path, pem, host):
    archive, m, incoming, _ = sd_release(tmp_path, pem, host)
    with open(os.path.join(incoming, "server-sd.tar.gz"), "ab") as handle:
        handle.write(b"tampered")
    with pytest.raises(apply_mod.ApplyError, match="image engine archive"):
        run_apply(host, archive, m, assets_dir=incoming)
    assert not os.path.exists(os.path.join(host, "bin-sd"))


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
    ([(member("sd-server"), b"x"), (member("../escape.so"), b"x")], "parent directory"),
    ([(member("sd-server"), b"x"), (member("sub/lib.so"), b"x")], "a folder"),
    ([(member("sd-server"), b"x"), (member("/abs.so"), b"x")], "absolute"),
    ([(member("sd-server"), b"x"), (member("link.so", 0, tarfile.SYMTYPE, "/etc/passwd"), None)], "symlink"),
    ([(member(".hidden"), b"x"), (member("sd-server"), b"x")], "hidden"),
    ([(member("libggml.so"), b"x")], "no sd-server"),
])
def test_an_unsafe_sd_archive_is_refused_and_the_installed_engine_is_left_alone(tmp_path, label, entries):
    root = tmp_path / "root"
    (root / "bin-sd").mkdir(parents=True)
    (root / "bin-sd" / "sd-server").write_text("installed")
    (root / "bin-sd" / ".installed-from").write_text("old\n")
    bad = evil_tarball(tmp_path / "evil.tar.gz", entries)
    with pytest.raises(apply_mod.ApplyError):
        apply_mod.install_server_sd(str(root), bad, "f" * 64)
    assert (root / "bin-sd" / "sd-server").read_text() == "installed"
    assert not (tmp_path / "escape.so").exists()
    assert sorted(os.listdir(root)) == ["bin-sd"]                       # no staging folder left behind


def test_rolling_back_does_not_touch_the_image_engine(tmp_path, pem, host):
    archive, m, incoming, _ = sd_release(tmp_path, pem, host)
    run_apply(host, archive, m, assets_dir=incoming)
    rec = Recorder()
    apply_mod.rollback_release(host, None, rec.restart, rec.health, log=lambda _m: None)
    assert open(os.path.join(host, "bin-sd", "sd-server")).read() == "sd-server v1"      # old code ignores it


# --- workers never fetch or need it ------------------------------------------------------------------------------------

def test_workers_and_the_downloaders_know_nothing_of_the_image_engine():
    for name in ("worker.py", "downloader.py", "setup_core.py", "app_member.py", "registry.py"):
        source = open(os.path.join(REPO_ROOT, "engine", name), encoding="utf-8").read()
        for word in ("bin-sd", "server-sd", "server_sd", "sd_build", "sd-server", "image_member"):
            assert word not in source, (name, word)


def test_the_coordinator_serves_no_download_of_it():
    from engine import coordinator
    paths = [getattr(route, "path", "") for route in coordinator.app.routes]
    assert not [p for p in paths if "download" in p and ("sd" in p.split("/")[-1] or "image" in p)]
    assert "rpc-bin" in "".join(paths)                                   # the routes it does serve are still there


# --- the model pins and the downloader -------------------------------------------------------------------------------

def test_the_three_pins_are_the_contracts_and_the_runtime_checks_the_same_names_and_sizes():
    pins = {p["role"]: p for p in fetch.load_pins()}
    assert set(pins) == {"diffusion", "vae", "llm"}
    # FLUX.2 [klein] 4B since 2.6.6.
    assert pins["diffusion"]["file"] == "flux-2-klein-4b-Q4_0.gguf"
    assert pins["diffusion"]["url"] == ("https://huggingface.co/leejet/FLUX.2-klein-4B-GGUF/resolve/"
                                        "3b1f5a9dc3abb32238b053aeb3d823c30afdacbd/flux-2-klein-4b-Q4_0.gguf")
    assert (pins["diffusion"]["size"], pins["diffusion"]["sha256"]) == (
        2460378560, "d1023499ef3f2f82ff7c50e6778495195c1b6cc34835741778868428111f9ff4")
    # The full-encoder VAE, saved under a name of its own (sd-server refuses the small decoder alone).
    assert pins["vae"]["file"] == "flux2-full-encoder-small-decoder.safetensors"
    assert pins["vae"]["url"] == ("https://huggingface.co/black-forest-labs/FLUX.2-small-decoder/resolve/"
                                  "a3efc24f613ef42d9428af62fdbd6f5fd8856c4a/full_encoder_small_decoder.safetensors")
    assert (pins["vae"]["size"], pins["vae"]["sha256"]) == (
        249519092, "ea4273f02d1fafbf8e1d1c2cf6018ed8748652eb0bf34f2dd91171f16f15ab62")
    for role, pin in pins.items():
        assert image_member.MODEL_FILES[role] == (pin["file"], pin["size"])
        assert len(pin["sha256"]) == 64 and "/resolve/" in pin["url"] and len(pin["url"].split("/resolve/")[1].split("/")[0]) == 40


def test_the_text_encoder_is_the_cpu_pools_qwen3_4b_pin_not_a_second_copy():
    with open(os.path.join(REPO_ROOT, "scripts", "app_models.json"), encoding="utf-8") as handle:
        tier = next(t for t in json.load(handle)["tiers"] if t["id"] == "qwen3-4b")
    llm = next(p for p in fetch.load_pins() if p["role"] == "llm")
    assert {k: llm[k] for k in ("file", "url", "sha256", "size")} == {k: tier[k] for k in ("file", "url", "sha256", "size")}
    with open(os.path.join(REPO_ROOT, "scripts", "image_models.json"), encoding="utf-8") as handle:
        entry = next(e for e in json.load(handle)["models"] if e["role"] == "llm")
    assert "sha256" not in entry and "url" not in entry and entry["from_app_models"] == "qwen3-4b"


DATA = bytes(range(256)) * 40            # 10,240 bytes


def pin_for(data=DATA, name="m.bin"):
    return {"role": "diffusion", "file": name, "url": "https://example.test/m.bin", "sha256": hashlib.sha256(data).hexdigest(),
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
    """urlopen: serves ``data``, honouring a Range header unless ``ranges`` is False."""

    def __init__(self, data=DATA, ranges=True):
        self.data, self.ranges, self.requests = data, ranges, []

    def __call__(self, request, timeout=None):
        range_header = request.headers.get("Range")
        self.requests.append(range_header)
        if range_header and self.ranges:
            start = int(range_header.split("=")[1].split("-")[0])
            return Response(self.data[start:], 206)
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


def test_a_server_that_ignores_the_range_starts_the_file_over_rather_than_appending(tmp_path):
    (tmp_path / "m.bin.part").write_bytes(DATA[:4000])
    fetch.fetch_one(pin_for(), str(tmp_path), Server(ranges=False), log=lambda m: None)
    assert (tmp_path / "m.bin").read_bytes() == DATA


def test_a_download_with_the_wrong_hash_is_deleted_and_refused(tmp_path):
    wrong = bytes(reversed(DATA))
    with pytest.raises(fetch.FetchError, match="SHA-256"):
        fetch.fetch_one(pin_for(), str(tmp_path), Server(wrong), log=lambda m: None)
    assert os.listdir(tmp_path) == []


def test_a_download_of_the_wrong_size_is_refused_and_never_moved_into_place(tmp_path):
    with pytest.raises(fetch.FetchError, match="expected 10,240"):
        fetch.fetch_one(pin_for(), str(tmp_path), Server(DATA[:5000]), log=lambda m: None)
    assert not (tmp_path / "m.bin").exists() and (tmp_path / "m.bin.part").stat().st_size == 5000   # kept: run again to continue
    with pytest.raises(fetch.FetchError, match="deleted"):
        fetch.fetch_one(pin_for(), str(tmp_path / "other"), Server(DATA + b"extra"), log=lambda m: None)
    assert os.listdir(tmp_path / "other") == []


def test_a_wrong_file_already_in_place_is_replaced_not_trusted(tmp_path):
    (tmp_path / "m.bin").write_bytes(b"not the model")
    fetch.fetch_one(pin_for(), str(tmp_path), Server(), log=lambda m: None)
    assert (tmp_path / "m.bin").read_bytes() == DATA


def test_a_dropped_connection_says_to_run_again(tmp_path):
    def broken(request, timeout=None):
        raise ConnectionResetError("reset")

    with pytest.raises(fetch.FetchError, match="run the script again"):
        fetch.fetch_one(pin_for(), str(tmp_path), broken, log=lambda m: None)


def test_the_script_defaults_to_image_model_dir(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(fetch, "load_pins", lambda: [pin_for()])
    monkeypatch.setattr(fetch.urllib.request, "urlopen", Server())
    import engine.config as cfg
    monkeypatch.setattr(cfg, "get_worker_limits", lambda: {"image_model_dir": str(tmp_path / "chosen")})
    assert fetch.main([]) == 0
    assert (tmp_path / "chosen" / "m.bin").read_bytes() == DATA


# --- the config keys ----------------------------------------------------------------------------------------------------

def limits_with(monkeypatch, values):
    monkeypatch.setattr(engine_config, "get_config", lambda force_reload=False: dict(values))
    return engine_config.get_worker_limits()


def test_the_defaults_are_the_contracts(monkeypatch):
    limits = limits_with(monkeypatch, {})
    assert {k: limits[k] for k in engine_config.IMAGE_DEFAULTS} == {
        "image_gen_enabled": False, "image_gen_paused": False, "image_model_dir": "", "image_threads": 4, "image_cpu_mask": "", "image_per_hour": 6,
        "image_queue_max": 4, "image_job_timeout_seconds": 300, "image_idle_unload_minutes": 20, "image_result_ttl_seconds": 1800}


@pytest.mark.parametrize("key, good, bad", [
    ("IMAGE_THREADS", ("1", "16", " 8 "), ("0", "17", "-1", "many", "2.5", "")),
    ("IMAGE_PER_HOUR", ("1", "60"), ("0", "61", "lots")),
    ("IMAGE_QUEUE_MAX", ("1", "16"), ("0", "17", "x")),
    ("IMAGE_JOB_TIMEOUT_SECONDS", ("60", "900"), ("59", "901", "fast")),
    ("IMAGE_IDLE_UNLOAD_MINUTES", ("0", "20", "1440"), ("-1", "1441", "never")),
    ("IMAGE_RESULT_TTL_SECONDS", ("60", "3600"), ("59", "3601", "x")),
])
def test_numbers_are_taken_inside_their_range_and_a_value_outside_it_keeps_the_default(monkeypatch, key, good, bad):
    name = key.lower()
    default = engine_config.IMAGE_DEFAULTS[name]
    for raw in good:
        assert limits_with(monkeypatch, {key: raw})[name] == int(raw)
    for raw in bad:
        assert limits_with(monkeypatch, {key: raw})[name] == default, (key, raw)


@pytest.mark.parametrize("raw, expected", [("true", True), ("TRUE", True), (" true ", True), ("false", False),
                                           ("yes", False), ("1", False), ("", False)])
def test_only_the_word_true_turns_it_on(monkeypatch, raw, expected):
    assert limits_with(monkeypatch, {"IMAGE_GEN_ENABLED": raw})["image_gen_enabled"] is expected
    assert limits_with(monkeypatch, {"IMAGE_GEN_ENABLED": raw})["image_gen_paused"] is False     # true/false: as before


@pytest.mark.parametrize("raw", ["paused", "PAUSED", " Paused "])
def test_paused_is_its_own_word_and_never_also_on(monkeypatch, raw):
    limits = limits_with(monkeypatch, {"IMAGE_GEN_ENABLED": raw})
    assert limits["image_gen_paused"] is True and limits["image_gen_enabled"] is False   # nothing that loads anything runs


@pytest.mark.parametrize("raw", ["pause", "paused!", "true paused", "", "stopped"])
def test_any_other_word_is_off_as_before(monkeypatch, raw):
    limits = limits_with(monkeypatch, {"IMAGE_GEN_ENABLED": raw})
    assert limits["image_gen_paused"] is False and limits["image_gen_enabled"] is False


def test_the_model_directory_is_taken_as_written_with_a_home_expanded(monkeypatch):
    assert limits_with(monkeypatch, {"IMAGE_MODEL_DIR": " /data/image "})["image_model_dir"] == "/data/image"
    assert limits_with(monkeypatch, {"IMAGE_MODEL_DIR": "~/img"})["image_model_dir"] == os.path.expanduser("~/img")
    assert limits_with(monkeypatch, {"IMAGE_MODEL_DIR": ""})["image_model_dir"] == ""


@pytest.mark.parametrize("raw, expected", [("0xf0", "0xf0"), ("F0", "0xf0"), (" 0XC3 ", "0xc3"), ("", ""), ("0x", ""),
                                           ("0xzz", ""), ("1,2,3", ""), ("not hex", "")])
def test_the_cpu_mask_is_hex_or_unset(monkeypatch, raw, expected):
    assert limits_with(monkeypatch, {"IMAGE_CPU_MASK": raw})["image_cpu_mask"] == expected
