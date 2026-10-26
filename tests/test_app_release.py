"""
The CPU pool's model tiers in the signed release (engine/release.py).

Tiers are signed as flat text and integer fields: "app_tiers" lists the ids smallest first and
each id has app_<id>_{name,file,url,sha256,size,min_ram_gb}. Every verifier in the field must
canonicalise them identically - including the VS Code extension's TypeScript
(vscode-extension/src/worker/engineRelease.ts), which is checked here byte for byte.
"""

import copy
import importlib.util
import json
import os
import shutil
import subprocess

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from engine import release

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILT_AT = "2026-10-25T12:00:00Z"
FIELDS = ("name", "file", "url", "sha256", "size", "min_ram_gb")
REV = "ae44f08e1392f39c0e474af10c3ff8355c8b6688"

with open(os.path.join(REPO_ROOT, "scripts", "app_models.json"), "r", encoding="utf-8") as _handle:
    SHIPPED = json.load(_handle)["tiers"]


def _pem(key):
    return key.private_bytes(encoding=serialization.Encoding.PEM, format=serialization.PrivateFormat.PKCS8,
                             encryption_algorithm=serialization.NoEncryption())


@pytest.fixture
def keypair():
    pem = _pem(Ed25519PrivateKey.generate())
    return pem, release.public_key_hex(pem)


@pytest.fixture
def archive(tmp_path):
    path = tmp_path / "dlne-source.zip"
    path.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    return str(path)


def tier(tid="tiny-1b", file="Tiny-1B-Q4_K_M.gguf", **changes):
    t = {"id": tid, "name": "Tiny 1B", "file": file,
         "url": f"https://huggingface.co/acme/Tiny-1B-GGUF/resolve/{REV}/{file}",
         "sha256": "ab" * 32, "size": 812345678, "min_ram_gb": 4}
    t.update(changes)
    return t


def manifest(archive, tiers):
    return release.make_manifest("2.3.0", archive, "abc1234", BUILT_AT, "CPU pool", app_models=tiers)


# ======================================================================================
# Shape
# ======================================================================================

def test_app_field_names():
    assert release.app_field("qwen3-4b", "sha256") == "app_qwen3_4b_sha256"
    assert release.app_field("qwen3-30b-a3b", "min_ram_gb") == "app_qwen3_30b_a3b_min_ram_gb"


def test_the_shipped_model_list_is_valid_and_smallest_first(archive):
    assert [release._check_app_model(t) for t in SHIPPED] == [None] * len(SHIPPED)
    ram = [t["min_ram_gb"] for t in SHIPPED]
    assert ram == sorted(ram) and len(set(t["id"] for t in SHIPPED)) == len(SHIPPED)
    manifest(archive, SHIPPED)


def test_make_manifest_flattens_tiers_into_text_and_integers(archive):
    m = manifest(archive, SHIPPED)
    assert m["app_tiers"] == ",".join(t["id"] for t in SHIPPED)
    assert m["app_tiers"].startswith("qwen3-4b,"), "the smallest tier first"
    for t in SHIPPED:
        for what in FIELDS:
            assert m[release.app_field(t["id"], what)] == t[what]
    for key, value in m.items():
        assert isinstance(value, (str, int)) and not isinstance(value, bool), key
        assert not isinstance(value, float), key


def test_no_app_models_means_no_app_fields(archive, keypair):
    for empty in (None, []):
        m = release.make_manifest("2.3.0", archive, "abc1234", BUILT_AT, app_models=empty)
        assert not any(k.startswith("app_") for k in m)
        assert release.app_tiers(m) == []
        assert release.verify_manifest(release.sign_manifest(m, keypair[0]), keypair[1]) == (True, "ok")


def test_sign_verify_and_read_back(archive, keypair):
    signed = release.sign_manifest(manifest(archive, SHIPPED), keypair[0])
    assert release.verify_manifest(signed, keypair[1]) == (True, "ok")
    assert release.app_tiers(signed) == [dict(t) for t in SHIPPED]
    # Through JSON and back, as a node receives it from /v1/release.
    wire = json.loads(json.dumps(signed))
    assert release.verify_manifest(wire, keypair[1]) == (True, "ok")


def _tamper_cases():
    a, b = SHIPPED[0]["id"], SHIPPED[1]["id"]
    other_sha = "cd" * 32
    return [
        ("name", lambda m: m.__setitem__(release.app_field(a, "name"), "Qwen3 4B Evil")),
        ("file+url", lambda m: (m.__setitem__(release.app_field(a, "file"), "Evil.gguf"),
                                m.__setitem__(release.app_field(a, "url"),
                                              f"https://huggingface.co/evil/x/resolve/{REV}/Evil.gguf"))),
        ("url", lambda m: m.__setitem__(release.app_field(a, "url"),
                                        m[release.app_field(a, "url")].replace("bartowski", "mallory"))),
        ("sha256", lambda m: m.__setitem__(release.app_field(a, "sha256"), other_sha)),
        ("size", lambda m: m.__setitem__(release.app_field(a, "size"), m[release.app_field(a, "size")] + 1)),
        ("min_ram_gb", lambda m: m.__setitem__(release.app_field(b, "min_ram_gb"), 8)),
        ("order", lambda m: m.__setitem__("app_tiers", f"{b},{a}")),
        ("drop a tier", lambda m: m.__setitem__("app_tiers", a)),
        ("drop the list", lambda m: m.pop("app_tiers")),
        ("drop a field", lambda m: m.pop(release.app_field(b, "name"))),
        ("add a field", lambda m: m.__setitem__(release.app_field(a, "mirror"), "https://evil.example/")),
    ]


@pytest.mark.parametrize("what, tamper", _tamper_cases(), ids=[c[0] for c in _tamper_cases()])
def test_tampering_with_any_app_field_fails_verification(archive, keypair, what, tamper):
    signed = release.sign_manifest(manifest(archive, SHIPPED), keypair[0])
    tampered = copy.deepcopy(signed)
    tamper(tampered)
    ok, reason = release.verify_manifest(tampered, keypair[1])
    assert not ok and reason


# ======================================================================================
# Malformed tiers
# ======================================================================================

def _url(file="Tiny-1B-Q4_K_M.gguf", rev=REV, host="huggingface.co", scheme="https"):
    return f"{scheme}://{host}/acme/Tiny-1B-GGUF/resolve/{rev}/{file}"


BAD_TIERS = {
    "url on another host": tier(url=_url(host="evil.example")),
    "url on a look-alike host": tier(url=_url(host="huggingface.co.evil.example")),
    "plain http": tier(url=_url(scheme="http")),
    "unpinned revision": tier(url=_url(rev="main")),
    "short revision": tier(url=_url(rev=REV[:-1])),
    "uppercase revision": tier(url=_url(rev=REV.upper())),
    "url names another file": tier(url=_url(file="Other-Q4_K_M.gguf")),
    "url with a query": tier(url=_url() + "?download=true"),
    "url in a subfolder": tier(url=f"https://huggingface.co/acme/Tiny-1B-GGUF/resolve/{REV}/sub/Tiny-1B-Q4_K_M.gguf"),
    "url missing": tier(url=None),
    "sha uppercase": tier(sha256="AB" * 32),
    "sha short": tier(sha256="ab" * 31 + "a"),
    "sha not hex": tier(sha256="zz" * 32),
    "sha missing": tier(sha256=None),
    "size zero": tier(size=0),
    "size negative": tier(size=-1),
    "size bool": tier(size=True),
    "size float": tier(size=812345678.0),
    "size text": tier(size="812345678"),
    "size over 1 TiB": tier(size=(1 << 40) + 1),
    "min_ram zero": tier(min_ram_gb=0),
    "min_ram over 512": tier(min_ram_gb=513),
    "min_ram float": tier(min_ram_gb=4.0),
    "min_ram bool": tier(min_ram_gb=True),
    "name non-ASCII": tier(name="Tiny 1B — Instruct"),
    "name empty": tier(name=""),
    "name with a tab": tier(name="Tiny\t1B"),
    "name missing": tier(name=None),
    "id uppercase": tier(tid="Tiny-1b"),
    "id underscore": tier(tid="tiny_1b"),
    "id leading hyphen": tier(tid="-tiny"),
    "id empty": tier(tid=""),
    "id too long": tier(tid="t" * 42),
    "id with a comma": tier(tid="a,b"),
    "id not text": tier(tid=7),
    "file escapes the folder": tier(file="../Tiny-1B-Q4_K_M.gguf"),
    "file in a folder": tier(file="sub/Tiny-1B-Q4_K_M.gguf"),
    "file not gguf": tier(file="Tiny-1B.bin"),
    "file executable": tier(file="Tiny-1B.gguf.exe"),
}


@pytest.mark.parametrize("label", list(BAD_TIERS))
def test_make_manifest_refuses_a_malformed_tier(archive, label):
    with pytest.raises(release.ReleaseError):
        manifest(archive, [tier(tid="good-1", file="Good-1.gguf"), BAD_TIERS[label]])
    assert release._check_app_model(BAD_TIERS[label]) is not None


def test_make_manifest_refuses_duplicate_ids(archive):
    with pytest.raises(release.ReleaseError, match="unique"):
        manifest(archive, [tier(), tier(file="Other.gguf")])


def _signed_by_hand(archive, keypair, mutate):
    """A correctly signed manifest whose tiers bypassed make_manifest's checks."""
    m = manifest(archive, [tier(), tier(tid="big-8b", file="Big-8B.gguf", min_ram_gb=16)])
    mutate(m)
    return release.sign_manifest(m, keypair[0])


@pytest.mark.parametrize("label, mutate", [
    ("empty list", lambda m: m.__setitem__("app_tiers", "")),
    ("list not text", lambda m: m.__setitem__("app_tiers", ["tiny-1b"])),
    ("empty entry", lambda m: m.__setitem__("app_tiers", "tiny-1b,,big-8b")),
    ("trailing comma", lambda m: m.__setitem__("app_tiers", "tiny-1b,big-8b,")),
    ("unknown id", lambda m: m.__setitem__("app_tiers", "tiny-1b,ghost")),
    ("missing field", lambda m: m.pop("app_big_8b_sha256")),
    ("float size", lambda m: m.__setitem__("app_big_8b_size", 1.5)),
    ("bad host", lambda m: m.__setitem__("app_big_8b_url", "https://evil.example/a/b/resolve/" + REV + "/Big-8B.gguf")),
])
def test_verify_refuses_a_signed_manifest_with_malformed_tiers(archive, keypair, label, mutate):
    ok, reason = release.verify_manifest(_signed_by_hand(archive, keypair, mutate), keypair[1])
    assert not ok and ("app_tiers" in reason or "CPU pool" in reason)


def test_verify_refuses_duplicate_tier_ids(archive, keypair):
    signed = _signed_by_hand(archive, keypair, lambda m: m.__setitem__("app_tiers", "tiny-1b,tiny-1b"))
    assert release.verify_manifest(signed, keypair[1])[0] is False


@pytest.mark.parametrize("field", ["id", "file+url", "sha256"])
def test_a_trailing_newline_is_refused(archive, field):
    if field == "id":
        bad = tier(tid="tiny-1b\n")
    elif field == "file+url":
        bad = tier(file="Tiny-1B-Q4_K_M.gguf\n")
    else:
        bad = tier(sha256="ab" * 32 + "\n")
    with pytest.raises(release.ReleaseError):
        manifest(archive, [bad])


def test_tiers_out_of_order_are_refused(archive):
    with pytest.raises(release.ReleaseError):
        manifest(archive, [tier(tid="big-8b", file="Big-8B.gguf", min_ram_gb=16), tier()])


# ======================================================================================
# scripts/release.py reads the model list
# ======================================================================================

def _cli():
    spec = importlib.util.spec_from_file_location("release_cli_app", os.path.join(REPO_ROOT, "scripts", "release.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_release_script_reads_the_model_list(tmp_path):
    cli = _cli()
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    assert cli.load_app_models(str(repo)) is None
    target = repo / "scripts" / "app_models.json"
    target.write_text("{not json")
    with pytest.raises(cli.CliError):
        cli.load_app_models(str(repo))
    target.write_text(json.dumps({"tiers": []}))
    with pytest.raises(cli.CliError):
        cli.load_app_models(str(repo))
    target.write_text(json.dumps(["tiers"]))
    with pytest.raises(cli.CliError):
        cli.load_app_models(str(repo))
    assert cli.load_app_models(REPO_ROOT) == SHIPPED


# ======================================================================================
# The extension canonicalises and verifies the same bytes
# ======================================================================================

def _engine_release_js(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    ext = os.path.join(REPO_ROOT, "vscode-extension")
    source = os.path.join(ext, "src", "worker", "engineRelease.ts")
    tsc = os.path.join(ext, "node_modules", ".bin", "tsc")
    if os.path.isfile(tsc):
        out = tmp_path / "js"
        subprocess.run([tsc, "--outDir", str(out), "--module", "commonjs", "--target", "ES2022", "--skipLibCheck",
                        "--esModuleInterop", source], cwd=ext, capture_output=True, timeout=300)
        if (out / "engineRelease.js").is_file():
            return node, str(out / "engineRelease.js")
    built = os.path.join(ext, "dist", "worker", "engineRelease.js")
    if os.path.isfile(built) and os.path.getmtime(built) >= os.path.getmtime(source):
        return node, built
    pytest.skip("the extension is not built: cd vscode-extension && npx tsc -p ./")


_NODE_SCRIPT = """
const release = require(process.argv[1]);
const manifest = JSON.parse(require('fs').readFileSync(process.argv[2], 'utf8'));
let canonical = null, error = null;
try { canonical = release.canonicalBytes(manifest).toString('base64'); } catch (e) { error = String(e.message); }
const verdict = release.verifyManifest(manifest, process.argv[3]);
process.stdout.write(JSON.stringify({canonical, error, ok: verdict.ok, reason: verdict.reason || null}));
"""


def _node_view(tmp_path, manifest_dict, pubkey):
    node, js = _engine_release_js(tmp_path)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest_dict))
    done = subprocess.run([node, "-e", _NODE_SCRIPT, js, str(path), pubkey], capture_output=True, text=True,
                          timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_the_extension_canonicalises_app_fields_byte_for_byte(archive, keypair, tmp_path):
    import base64
    signed = release.sign_manifest(manifest(archive, [tier()] + SHIPPED), keypair[0])
    view = _node_view(tmp_path, signed, keypair[1])
    assert view["error"] is None
    assert base64.b64decode(view["canonical"]) == release.canonical_bytes(signed)
    assert view["ok"] is True, view["reason"]


def test_the_extension_rejects_a_tampered_app_field(archive, keypair, tmp_path):
    signed = release.sign_manifest(manifest(archive, SHIPPED), keypair[0])
    signed[release.app_field(SHIPPED[0]["id"], "sha256")] = "cd" * 32
    view = _node_view(tmp_path, signed, keypair[1])
    assert view["ok"] is False and "signature" in view["reason"]
