"""
The GPU coding pool's model ladder: scripts/coding_models.json, its validation and signing
(engine/release.py) and how the coordinator reads it (engine/coding_ladder.py).

The VS Code extension verifies the same signed manifest in TypeScript and throws on any value
that is not a string or an integer, so the coding fields are checked for that here and, when
node and the TypeScript compiler are available, byte for byte against engineRelease.ts.
"""

import base64
import copy
import importlib.util
import json
import os
import shutil
import subprocess

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from engine import coding_ladder, release

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILT_AT = "2026-10-25T12:00:00Z"
REV = "a483e9e6cbd595906af30beda3187c2663a1118c"
LIVE_FILE = "Qwen3.6-35B-A3B-UD-IQ3_S.gguf"

with open(os.path.join(REPO_ROOT, "scripts", "coding_models.json"), "r", encoding="utf-8") as _handle:
    SHIPPED = json.load(_handle)["tiers"]
with open(os.path.join(REPO_ROOT, "scripts", "app_models.json"), "r", encoding="utf-8") as _handle:
    APP_TIERS = {t["id"]: t for t in json.load(_handle)["tiers"]}


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


def _url(file, folder="", rev=REV, host="huggingface.co"):
    return f"https://{host}/acme/Tiny-GGUF/resolve/{rev}/{folder}{file}"


def tier(tid="tiny-8", file="Tiny-8B-Q4_K_M.gguf", min_vram_gb=8, **changes):
    t = {"id": tid, "name": "Tiny 8B", "min_vram_gb": min_vram_gb, "context": 32768, "enabled": True,
         "file": file, "url": _url(file), "sha256": "ab" * 32, "size": 5_000_000_000}
    t.update(changes)
    return t


def split_tier(tid="huge-100", stem="Huge-100B-Q2_K", count=3, min_vram_gb=100, **changes):
    files = []
    for k in range(1, count + 1):
        name = f"{stem}-{k:05d}-of-{count:05d}.gguf"
        files.append({"file": name, "url": _url(name, folder="Q2_K/"), "sha256": f"{k:02x}" * 32, "size": 1000 * k})
    t = {"id": tid, "name": "Huge 100B", "min_vram_gb": min_vram_gb, "context": 65536, "enabled": False,
         "blocked": "placeholder", "files": files}
    t.update(changes)
    return t


def manifest(archive, coding):
    return release.make_manifest("2.4.0", archive, "abc1234", BUILT_AT, "ladder", coding_models=coding)


# ======================================================================================
# The shipped ladder
# ======================================================================================

def test_the_shipped_ladder_is_valid_and_smallest_first():
    tiers = release.check_coding_tiers(SHIPPED)
    vram = [t["min_vram_gb"] for t in tiers]
    assert vram == sorted(vram)
    assert len({t["id"] for t in tiers}) == len(tiers)
    assert tiers[0]["file"] == LIVE_FILE, "the bottom rung is the model live on the public swarm"


def test_the_shipped_ladder_is_the_approved_one():
    tiers = {t["id"]: t for t in release.check_coding_tiers(SHIPPED)}
    # 16 GB (live) and the owner-approved 24/32/48 GB tiers are on.
    approved = ["qwen36-a3b", "qwen36-a3b-iq4xs", "qwen36-a3b-q4", "qwen36-a3b-q6"]
    assert [tiers[i]["min_vram_gb"] for i in approved] == [15, 24, 29, 39]
    assert all(tiers[i]["enabled"] and tiers[i]["blocked"] is None for i in approved)
    # 2.5.0: two slots of 40,960 tokens each, TheWall's own per-request context (docs/coding-members.md section 4).
    assert all((tiers[i]["context"], tiers[i]["server_slots"]) == (81920, 2) for i in approved)
    assert all(tiers[i][knob] is None for i in approved for knob in release._CODING_SERVER_KNOBS)
    # Qwen3-Coder-Next sits above them, pinned but off (see the test below). Everything else bigger is a placeholder:
    # off, and saying so.
    rest = [t for i, t in tiers.items() if i not in approved and i != CODER_NEXT["id"]]
    assert len(rest) == 4
    assert all(not t["enabled"] and t["blocked"].startswith("placeholder") for t in rest)
    assert all(len(t["files"]) > 1 for t in rest), "the Flash-Next tiers are split models"


# Qwen3-Coder-Next UD-IQ4_XS as pinned by the 2026-10-25 research (unsloth/Qwen3-Coder-Next-GGUF at commit ce09c67b...;
# size and SHA-256 are the Hugging Face LFS values at that commit). Off until the DLNE coding suite has measured it.
CODER_NEXT = {
    "id": "qwen3-coder-next-iq4xs",
    "min_vram_gb": 49,
    "context": 65536,
    "file": "Qwen3-Coder-Next-UD-IQ4_XS.gguf",
    "url": "https://huggingface.co/unsloth/Qwen3-Coder-Next-GGUF/resolve/ce09c67b53bc8739eef83fe67b2f5d293c270632/Qwen3-Coder-Next-UD-IQ4_XS.gguf",
    "sha256": "abf56d7fe8a0a99c15d220c13de4aa57b69cfba6ef4c2a007b56e34d7b40cd11",
    "size": 38429272064,
}


def test_qwen3_coder_next_is_pinned_but_disabled():
    tiers = release.check_coding_tiers(SHIPPED)
    ids = [t["id"] for t in tiers]
    tier = tiers[ids.index(CODER_NEXT["id"])]
    assert {k: tier[k] for k in CODER_NEXT} == CODER_NEXT
    assert tier["files"] == [{k: CODER_NEXT[k] for k in ("file", "url", "sha256", "size")}]
    # on the 48 GB rung: after the Q6_K tier, before the Flash-Next placeholders
    assert ids.index("qwen36-a3b-q6") + 1 == ids.index(CODER_NEXT["id"]) == ids.index("qwen38-flash-next-q2") - 1
    assert not tier["enabled"]
    assert tier["blocked"] and "not measured" in tier["blocked"]
    # a 49 GB pool is not sent to it while it is off, and still is once someone enables it
    assert coding_ladder.tier_for_vram(tiers, 60)["id"] == "qwen36-a3b-q6"
    on = [dict(t, enabled=True, blocked=None) if t["id"] == CODER_NEXT["id"] else t for t in tiers]
    assert coding_ladder.tier_for_vram(on, 48.9)["id"] == "qwen36-a3b-q6"
    assert coding_ladder.tier_for_vram(on, 49)["id"] == CODER_NEXT["id"]


def test_the_live_tier_is_the_same_file_as_the_phone_pools():
    live = release.check_coding_tiers(SHIPPED)[0]
    app = APP_TIERS["qwen36-a3b"]
    assert {k: live[k] for k in ("file", "url", "sha256", "size")} == {k: app[k] for k in ("file", "url", "sha256", "size")}
    q4 = {t["id"]: t for t in release.check_coding_tiers(SHIPPED)}["qwen36-a3b-q4"]
    assert q4["sha256"] == APP_TIERS["qwen36-a3b-q4"]["sha256"]


def test_the_built_in_tier_is_the_shipped_first_tier():
    shipped_first = {k: v for k, v in SHIPPED[0].items() if not k.startswith("_")}
    assert coding_ladder.BUILTIN_TIERS == [shipped_first]


def test_the_stale_models_are_gone_from_the_ladder():
    tiers = release.check_coding_tiers(SHIPPED)
    names = " ".join([t["name"] for t in tiers] + [p["file"] for t in tiers for p in t["files"]]).lower()
    for stale in ("qwen2.5", "llama", "deepseek", "14b", "72b", "70b"):
        assert stale not in names


# ======================================================================================
# One tier
# ======================================================================================

def test_a_single_file_tier_is_normalised():
    t = release.coding_tier(tier())
    assert t["files"] == [{"file": "Tiny-8B-Q4_K_M.gguf", "url": _url("Tiny-8B-Q4_K_M.gguf"),
                           "sha256": "ab" * 32, "size": 5_000_000_000}]
    assert (t["file"], t["size"], t["enabled"], t["blocked"]) == ("Tiny-8B-Q4_K_M.gguf", 5_000_000_000, True, None)
    assert set(t) == {"id", "name", "file", "url", "sha256", "size", "min_vram_gb", "context", "enabled", "blocked", "files",
                      "server_slots", "server_ctx_checkpoints", "server_ubatch", "server_spec_type"}
    assert (t["server_slots"], t["server_ctx_checkpoints"], t["server_ubatch"], t["server_spec_type"]) == (2, None, None, None)


def test_the_server_settings_are_read_and_none_means_unset():
    t = release.coding_tier(tier(context=32768, server_slots=4, server_ctx_checkpoints=8, server_ubatch=256,
                                 server_spec_type="ngram-mod"))
    assert (t["server_slots"], t["server_ctx_checkpoints"], t["server_ubatch"], t["server_spec_type"]) == (4, 8, 256, "ngram-mod")
    assert release.coding_tier(tier(server_spec_type="none"))["server_spec_type"] is None, '"none" is no setting'
    assert release.coding_tier(tier(server_ctx_checkpoints=0))["server_ctx_checkpoints"] == 0, "0 switches checkpoints off"
    nulls = release.coding_tier(tier(server_slots=None, server_ctx_checkpoints=None, server_ubatch=None, server_spec_type=None))
    assert (nulls["server_slots"], nulls["server_ctx_checkpoints"], nulls["server_ubatch"], nulls["server_spec_type"]) == (2, None, None, None)
    assert release.coding_tier(tier(context=65536, server_slots=64))["server_slots"] == 64


def test_a_split_tier_names_part_one_and_totals_the_size():
    t = release.coding_tier(split_tier())
    assert t["file"] == "Huge-100B-Q2_K-00001-of-00003.gguf"
    assert t["url"].endswith("/Q2_K/Huge-100B-Q2_K-00001-of-00003.gguf")
    assert t["sha256"] == "01" * 32
    assert t["size"] == 1000 + 2000 + 3000
    assert [p["file"] for p in t["files"]] == [f"Huge-100B-Q2_K-0000{k}-of-00003.gguf" for k in (1, 2, 3)]


def test_comment_keys_are_ignored():
    assert release.coding_tier(tier(_note="why this tier"))["id"] == "tiny-8"


def _parts(**change):
    t = split_tier()
    t["files"] = copy.deepcopy(t["files"])
    for key, value in change.items():
        index, field = key.split("__")
        t["files"][int(index)][field] = value
    return t


BAD_TIERS = {
    "id uppercase": tier(tid="Tiny-8"),
    "id with a comma": tier(tid="a,b"),
    "unknown field": tier(enable=True),
    "name non-ASCII": tier(name="Tiny \u2014 8B"),
    "name empty": tier(name=""),
    "name too long": tier(name="x" * 81),
    "min_vram float": tier(min_vram_gb=8.0),
    "min_vram bool": tier(min_vram_gb=True),
    "min_vram zero": tier(min_vram_gb=0),
    "context float": tier(context=32768.0),
    "context too small": tier(context=512),
    "context too big": tier(context=(1 << 21) + 1),
    "enabled as text": tier(enabled="true"),
    "enabled as 1": tier(enabled=1),
    "server_slots zero": tier(server_slots=0),
    "server_slots over 64": tier(server_slots=65),
    "server_slots bool": tier(server_slots=True),
    "server_slots float": tier(server_slots=2.0),
    "server_slots text": tier(server_slots="2"),
    "context not a multiple of server_slots": tier(context=32768, server_slots=3),
    "checkpoints negative": tier(server_ctx_checkpoints=-1),
    "checkpoints over 64": tier(server_ctx_checkpoints=65),
    "checkpoints bool": tier(server_ctx_checkpoints=False),
    "ubatch zero": tier(server_ubatch=0),
    "ubatch over 8192": tier(server_ubatch=8193),
    "ubatch float": tier(server_ubatch=512.0),
    "spec type with a draft model": tier(server_spec_type="draft-simple"),
    "spec type ngram-cache": tier(server_spec_type="ngram-cache"),
    "spec type not text": tier(server_spec_type=1),
    "spec type a list": tier(server_spec_type=["ngram-mod"]),
    "enabled but blocked": tier(blocked="licence"),
    "blocked empty": tier(enabled=False, blocked=""),
    "blocked non-ASCII": tier(enabled=False, blocked="licen\u00e7e"),
    "file and files": dict(split_tier(), file="x.gguf"),
    "files empty": split_tier(files=[]),
    "too many parts": split_tier(count=17),
    "url on another host": tier(url=_url("Tiny-8B-Q4_K_M.gguf", host="evil.example")),
    "url unpinned": tier(url=_url("Tiny-8B-Q4_K_M.gguf", rev="main")),
    "url names another file": tier(url=_url("Other.gguf")),
    "url climbs a folder": tier(url=_url("Tiny-8B-Q4_K_M.gguf", folder="../")),
    "url two folders deep": tier(url=_url("Tiny-8B-Q4_K_M.gguf", folder="a/b/")),
    "url with a query": tier(url=_url("Tiny-8B-Q4_K_M.gguf") + "?download=true"),
    "file in a folder": tier(file="sub/Tiny.gguf", url=_url("sub/Tiny.gguf")),
    "file not gguf": tier(file="Tiny.bin", url=_url("Tiny.bin")),
    "sha uppercase": tier(sha256="AB" * 32),
    "size bool": tier(size=True),
    "size float": tier(size=5e9),
    "size over 1 TiB": tier(size=(1 << 40) + 1),
    "part out of order": _parts(**{"0__file": "Huge-100B-Q2_K-00002-of-00003.gguf",
                                   "0__url": _url("Huge-100B-Q2_K-00002-of-00003.gguf", folder="Q2_K/")}),
    "part count wrong": _parts(**{"2__file": "Huge-100B-Q2_K-00003-of-00004.gguf",
                                  "2__url": _url("Huge-100B-Q2_K-00003-of-00004.gguf", folder="Q2_K/")}),
    "parts of two models": _parts(**{"1__file": "Other-100B-Q2_K-00002-of-00003.gguf",
                                     "1__url": _url("Other-100B-Q2_K-00002-of-00003.gguf", folder="Q2_K/")}),
    "part with a bad hash": _parts(**{"1__sha256": "zz" * 32}),
    "part with an extra field": _parts(**{"1__mirror": "https://evil.example/"}),
}


@pytest.mark.parametrize("label", list(BAD_TIERS))
def test_a_malformed_tier_is_refused(label, archive):
    with pytest.raises(release.ReleaseError):
        release.coding_tier(BAD_TIERS[label])
    with pytest.raises(release.ReleaseError, match="Coding model list"):
        manifest(archive, [tier(tid="good-1", file="Good-1.gguf", min_vram_gb=1), BAD_TIERS[label]])


@pytest.mark.parametrize("label, tiers", [
    ("empty", []),
    ("duplicate ids", [tier(), tier(file="Other.gguf", url=_url("Other.gguf"))]),
    ("duplicate files", [tier(), tier(tid="tiny-8b")]),
    ("out of order", [tier(tid="big", file="Big.gguf", url=_url("Big.gguf"), min_vram_gb=24), tier()]),
    ("not a list", {"tiers": []}),
])
def test_a_malformed_list_is_refused(label, tiers):
    with pytest.raises(release.ReleaseError):
        release.check_coding_tiers(tiers)


# ======================================================================================
# The signed manifest
# ======================================================================================

def test_make_manifest_flattens_the_ladder_into_text_and_integers_only(archive):
    m = manifest(archive, SHIPPED)
    assert m["coding_tiers"] == ",".join(t["id"] for t in SHIPPED)
    assert m[release.coding_field("qwen36-a3b", "enabled")] == 1
    assert m[release.coding_field("qwen38-flash-next-q2", "enabled")] == 0
    assert m[release.coding_field("qwen38-flash-next-q2", "parts")] == 3
    assert m[release.coding_field("qwen36-a3b", "part1_file")] == LIVE_FILE
    assert m[release.coding_field("qwen36-a3b", "server_slots")] == 2
    assert m[release.coding_field("qwen38-flash-next-q2", "server_slots")] == 2, "the default is signed too"
    assert not any(key.endswith(release._CODING_SERVER_KNOBS) for key in m), "an unset knob is left out, never null"
    for key, value in m.items():
        # The extension's verifier throws on anything else, rejecting the whole release.
        assert isinstance(value, (str, int)) and not isinstance(value, (bool, float)), key


KNOBS = dict(context=32768, server_slots=4, server_ctx_checkpoints=8, server_ubatch=256, server_spec_type="ngram-mod")


def test_the_server_knobs_are_signed_as_integers_and_text_only_when_set(archive):
    m = manifest(archive, [tier(**KNOBS), tier(tid="plain-8", file="Plain.gguf", url=_url("Plain.gguf"),
                                               server_spec_type="none")])
    f = release.coding_field
    assert [m[f("tiny-8", k)] for k in ("server_slots", "server_ctx_checkpoints", "server_ubatch", "server_spec_type")] == \
        [4, 8, 256, "ngram-mod"]
    assert m[f("plain-8", "server_slots")] == 2
    assert not any(k.startswith(f("plain-8", "server_")) and k != f("plain-8", "server_slots") for k in m), \
        '"none" and the unset knobs are left out'
    for key, value in m.items():
        assert isinstance(value, (str, int)) and not isinstance(value, (bool, float)), key


def test_sign_verify_and_read_back(archive, keypair):
    signed = release.sign_manifest(manifest(archive, SHIPPED), keypair[0])
    assert release.verify_manifest(signed, keypair[1]) == (True, "ok")
    assert release.coding_tiers(signed) == release.check_coding_tiers(SHIPPED)
    wire = json.loads(json.dumps(signed))
    assert release.verify_manifest(wire, keypair[1]) == (True, "ok")
    assert release.coding_tiers(wire) == release.check_coding_tiers(SHIPPED)


def test_the_server_knobs_survive_the_round_trip(archive, keypair):
    ladder = [tier(**KNOBS), split_tier()]
    signed = release.sign_manifest(manifest(archive, ladder), keypair[0])
    assert release.verify_manifest(signed, keypair[1]) == (True, "ok")
    back = release.coding_tiers(json.loads(json.dumps(signed)))
    assert back == release.check_coding_tiers(ladder)
    assert (back[0]["server_slots"], back[0]["server_ctx_checkpoints"], back[0]["server_ubatch"], back[0]["server_spec_type"]) == \
        (4, 8, 256, "ngram-mod")
    assert (back[1]["server_slots"], back[1]["server_ctx_checkpoints"], back[1]["server_ubatch"], back[1]["server_spec_type"]) == \
        (2, None, None, None)


def test_a_release_from_before_the_server_fields_reads_as_two_slots(archive, keypair):
    """A 2.4.9 manifest signs no server_slots: its tiers are read exactly as the members of that release ran them."""
    m = manifest(archive, [tier()])
    del m[release.coding_field("tiny-8", "server_slots")]
    signed = release.sign_manifest(m, keypair[0])
    assert release.verify_manifest(signed, keypair[1]) == (True, "ok")
    (t,) = release.coding_tiers(signed)
    assert (t["server_slots"], t["server_ctx_checkpoints"], t["server_ubatch"], t["server_spec_type"]) == (2, None, None, None)


def test_the_ladder_sits_beside_the_cpu_pools_tiers(archive, keypair):
    m = release.make_manifest("2.4.0", archive, "abc1234", BUILT_AT, "both",
                              app_models=list(APP_TIERS.values()), coding_models=SHIPPED)
    signed = release.sign_manifest(m, keypair[0])
    assert release.verify_manifest(signed, keypair[1]) == (True, "ok")
    assert [t["id"] for t in release.app_tiers(signed)] == list(APP_TIERS)
    assert [t["id"] for t in release.coding_tiers(signed)] == [t["id"] for t in SHIPPED]


def test_no_ladder_means_no_coding_fields(archive, keypair):
    for empty in (None, []):
        m = release.make_manifest("2.4.0", archive, "abc1234", BUILT_AT, coding_models=empty)
        assert not any(k.startswith("coding_") for k in m)
        assert release.coding_tiers(m) == []
        assert release.verify_manifest(release.sign_manifest(m, keypair[0]), keypair[1]) == (True, "ok")


def _tamper_cases():
    live, split = "qwen36-a3b", "qwen38-flash-next-q2"
    f = release.coding_field
    return [
        ("name", lambda m: m.__setitem__(f(live, "name"), "Evil 1T")),
        ("enable a placeholder", lambda m: m.__setitem__(f(split, "enabled"), 1)),
        ("min_vram", lambda m: m.__setitem__(f(live, "min_vram_gb"), 1)),
        ("url", lambda m: m.__setitem__(f(live, "part1_url"), m[f(live, "part1_url")].replace("unsloth", "mallory"))),
        ("sha256", lambda m: m.__setitem__(f(live, "part1_sha256"), "cd" * 32)),
        ("size", lambda m: m.__setitem__(f(split, "part2_size"), m[f(split, "part2_size")] + 1)),
        ("order", lambda m: m.__setitem__("coding_tiers", ",".join(reversed(m["coding_tiers"].split(","))))),
        ("drop a tier", lambda m: m.__setitem__("coding_tiers", live)),
        ("drop the list", lambda m: m.pop("coding_tiers")),
        ("add a field", lambda m: m.__setitem__(f(live, "mirror"), "https://evil.example/")),
    ]


@pytest.mark.parametrize("what, tamper", _tamper_cases(), ids=[c[0] for c in _tamper_cases()])
def test_tampering_with_any_coding_field_fails_verification(archive, keypair, what, tamper):
    signed = release.sign_manifest(manifest(archive, SHIPPED), keypair[0])
    tampered = copy.deepcopy(signed)
    tamper(tampered)
    ok, reason = release.verify_manifest(tampered, keypair[1])
    assert not ok and reason


def _signed_by_hand(archive, keypair, mutate):
    """A correctly signed manifest whose coding fields bypassed make_manifest's checks."""
    m = manifest(archive, [tier(), split_tier()])
    mutate(m)
    return release.sign_manifest(m, keypair[0])


@pytest.mark.parametrize("label, mutate", [
    ("empty list", lambda m: m.__setitem__("coding_tiers", "")),
    ("unknown id", lambda m: m.__setitem__("coding_tiers", "tiny-8,ghost")),
    ("enabled as a bool", lambda m: m.__setitem__(release.coding_field("tiny-8", "enabled"), True)),
    ("enabled as 2", lambda m: m.__setitem__(release.coding_field("tiny-8", "enabled"), 2)),
    ("enabled as a float", lambda m: m.__setitem__(release.coding_field("tiny-8", "enabled"), 1.0)),
    ("no parts", lambda m: m.__setitem__(release.coding_field("huge-100", "parts"), 0)),
    ("more parts than listed", lambda m: m.__setitem__(release.coding_field("huge-100", "parts"), 4)),
    ("missing part field", lambda m: m.pop(release.coding_field("huge-100", "part2_sha256"))),
    ("blocked not text", lambda m: m.__setitem__(release.coding_field("huge-100", "blocked"), 0)),
    ("bad host", lambda m: m.__setitem__(release.coding_field("tiny-8", "part1_url"),
                                         _url("Tiny-8B-Q4_K_M.gguf", host="evil.example"))),
    ("server_slots as 0", lambda m: m.__setitem__(release.coding_field("tiny-8", "server_slots"), 0)),
    ("server_slots as text", lambda m: m.__setitem__(release.coding_field("tiny-8", "server_slots"), "2")),
    ("server_slots not dividing the context", lambda m: m.__setitem__(release.coding_field("tiny-8", "server_slots"), 3)),
    ("checkpoints over the limit", lambda m: m.__setitem__(release.coding_field("tiny-8", "server_ctx_checkpoints"), 65)),
    ("ubatch as a bool", lambda m: m.__setitem__(release.coding_field("tiny-8", "server_ubatch"), True)),
    ("spec type with a draft model", lambda m: m.__setitem__(release.coding_field("tiny-8", "server_spec_type"), "draft-simple")),
    ("spec type not text", lambda m: m.__setitem__(release.coding_field("tiny-8", "server_spec_type"), 1)),
])
def test_verify_refuses_a_signed_manifest_with_a_malformed_ladder(archive, keypair, label, mutate):
    ok, reason = release.verify_manifest(_signed_by_hand(archive, keypair, mutate), keypair[1])
    assert not ok and "coding" in reason


# ======================================================================================
# Where the coordinator reads the ladder from
# ======================================================================================

def _write_manifest(base, archive, coding):
    m = release.make_manifest("2.4.0", archive, "abc1234", BUILT_AT, coding_models=coding)
    (base / release.MANIFEST_NAME).write_text(json.dumps(m))


def _write_file(base, tiers):
    (base / "scripts").mkdir(exist_ok=True)
    (base / "scripts" / "coding_models.json").write_text(json.dumps({"tiers": tiers}))


def test_the_signed_release_wins(tmp_path, archive):
    _write_manifest(tmp_path, archive, [tier(tid="from-release")])
    _write_file(tmp_path, [tier(tid="from-file")])
    assert [t["id"] for t in coding_ladder.load_coding_tiers(str(tmp_path))] == ["from-release"]


def test_a_release_without_a_ladder_falls_back_to_the_file(tmp_path, archive):
    _write_manifest(tmp_path, archive, None)
    _write_file(tmp_path, [tier(tid="from-file")])
    assert [t["id"] for t in coding_ladder.load_coding_tiers(str(tmp_path))] == ["from-file"]


def test_a_malformed_release_ladder_falls_back_to_the_file(tmp_path, archive):
    m = release.make_manifest("2.4.0", archive, "abc1234", BUILT_AT, coding_models=[tier(tid="from-release")])
    m[release.coding_field("from-release", "enabled")] = True    # a bool: invalid
    (tmp_path / release.MANIFEST_NAME).write_text(json.dumps(m))
    _write_file(tmp_path, [tier(tid="from-file")])
    assert [t["id"] for t in coding_ladder.load_coding_tiers(str(tmp_path))] == ["from-file"]


@pytest.mark.parametrize("content", [None, "{not json", json.dumps({"tiers": []}), json.dumps([1]),
                                     json.dumps({"tiers": [tier(enabled="yes")]})])
def test_nothing_usable_means_the_built_in_tier(tmp_path, content):
    if content is not None:
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "coding_models.json").write_text(content)
    tiers = coding_ladder.load_coding_tiers(str(tmp_path))
    assert [t["file"] for t in tiers] == [LIVE_FILE]
    assert tiers[0]["enabled"] is True


def test_the_source_checkout_reads_the_shipped_file():
    assert coding_ladder.load_coding_tiers(REPO_ROOT) == release.check_coding_tiers(SHIPPED)


def test_load_coding_tiers_defaults_to_the_install_directory(isolated_config, dlne_root):
    _write_file(dlne_root, [tier(tid="installed")])
    assert [t["id"] for t in coding_ladder.load_coding_tiers()] == ["installed"]


def test_callers_get_their_own_copies(tmp_path):
    first = coding_ladder.load_coding_tiers(str(tmp_path))
    first[0]["name"] = "changed"
    assert coding_ladder.load_coding_tiers(str(tmp_path))[0]["name"] == "Qwen3.6 35B-A3B"
    assert coding_ladder.BUILTIN_TIERS[0]["name"] == "Qwen3.6 35B-A3B"


# ======================================================================================
# Lookups
# ======================================================================================

LADDER = release.check_coding_tiers(SHIPPED)


@pytest.mark.parametrize("vram, enabled_only, expected", [
    (0, True, None),
    (14.9, True, None),
    (15, True, "qwen36-a3b"),
    (16.0, True, "qwen36-a3b"),          # TheWall today: 2 x 8 GB
    (23.99, True, "qwen36-a3b"),
    (24, True, "qwen36-a3b-iq4xs"),
    (30, True, "qwen36-a3b-q4"),
    (48, True, "qwen36-a3b-q6"),
    (500, True, "qwen36-a3b-q6"),        # the placeholders above are off
    (49, True, "qwen36-a3b-q6"),         # Qwen3-Coder-Next is pinned at 49 but disabled
    (49, False, "qwen3-coder-next-iq4xs"),
    (64.9, False, "qwen3-coder-next-iq4xs"),
    (500, False, "qwen38-flash-next-q5"),
    (70, False, "qwen38-flash-next-q2"),
    (None, True, None),
    ("lots", True, None),
    (float("nan"), True, None),
])
def test_tier_for_vram(vram, enabled_only, expected):
    found = coding_ladder.tier_for_vram(LADDER, vram, enabled_only=enabled_only)
    assert (found["id"] if found else None) == expected


@pytest.mark.parametrize("file, expected", [
    (LIVE_FILE, "qwen36-a3b"),
    ("/home/wubarrk/barrkbot-llm/models/" + LIVE_FILE, "qwen36-a3b"),
    ("qwen3.6-35b-a3b-ud-iq3_s", "qwen36-a3b"),
    ("Qwen3.6-35B-A3B-UD-Q6_K.gguf", "qwen36-a3b-q6"),
    ("Qwen3.8-Flash-Next-UD-IQ4_XS-00003-of-00003.gguf", "qwen38-flash-next-iq4xs"),
    ("qwen2.5-coder-14b-instruct-q4_k_m.gguf", None),
    ("", None),
    (None, None),
])
def test_tier_for_file(file, expected):
    found = coding_ladder.tier_for_file(LADDER, file)
    assert (found["id"] if found else None) == expected


@pytest.mark.parametrize("file, expected", [
    (LIVE_FILE, "Qwen3.6 35B-A3B"),
    ("Qwen3.6-35B-A3B-UD-Q6_K.gguf", "Qwen3.6 35B-A3B (Q6_K)"),
    ("Qwen3.8-Flash-Next-UD-Q2_K_XL-00001-of-00003.gguf", "Qwen3.8 Flash-Next (Q2_K_XL)"),
])
def test_display_name_uses_the_ladder(file, expected):
    assert coding_ladder.display_name(file, LADDER) == expected


@pytest.mark.parametrize("file, expected", [
    (LIVE_FILE, "Qwen3.6 35B-A3B"),
    ("Qwen3-Coder-30B-A3B-Instruct-UD-Q3_K_XL.gguf", "Qwen3 Coder 30B-A3B Instruct"),
    ("qwen2.5-coder-14b-instruct-q4_k_m.gguf", "qwen2.5 coder 14b instruct"),
    ("Meta-Llama-3.1-405B-Instruct.Q4_K_M.gguf", "Meta Llama 3.1 405B Instruct"),
    ("Qwen3.8-Flash-Next-UD-Q2_K_XL-00002-of-00003.gguf", "Qwen3.8 Flash Next"),
    ("gpt-oss-20b-MXFP4.gguf", "gpt oss 20b"),
    ("model-f16.gguf", "model"),
    ("phi3-mini", "phi3 mini"),
    ("C:\\models\\Tiny-1B-IQ4_XS.gguf", "Tiny 1B"),
    ("", ""),
    (None, ""),
])
def test_display_name_derived_from_the_file_name(file, expected):
    assert coding_ladder.display_name(file, []) == expected
    if file:
        assert coding_ladder.display_name(file) == expected


def test_default_tier_and_label():
    assert coding_ladder.default_tier(LADDER)["file"] == LIVE_FILE
    assert coding_ladder.tier_label(LADDER[0]) == "Qwen3.6 35B-A3B (15 GB+ VRAM)"
    off = [dict(t, enabled=False) for t in LADDER[:2]]
    assert coding_ladder.default_tier(off)["id"] == "qwen36-a3b"
    assert coding_ladder.default_tier([]) is None


# ======================================================================================
# scripts/release.py signs the ladder in
# ======================================================================================

def _cli():
    spec = importlib.util.spec_from_file_location("release_cli_ladder", os.path.join(REPO_ROOT, "scripts", "release.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_release_script_reads_the_ladder(tmp_path):
    cli = _cli()
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    assert cli.load_coding_models(str(repo)) is None
    target = repo / "scripts" / "coding_models.json"
    for bad in ("{not json", json.dumps({"tiers": []}), json.dumps(["tiers"])):
        target.write_text(bad)
        with pytest.raises(cli.CliError):
            cli.load_coding_models(str(repo))
    assert cli.load_coding_models(REPO_ROOT) == SHIPPED


# ======================================================================================
# The extension canonicalises and verifies the same bytes
# ======================================================================================

def _engine_release_js(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    ext = os.path.join(REPO_ROOT, "vscode-extension")
    source = os.path.join(ext, "src", "worker", "engineRelease.ts")
    tsc = os.environ.get("DLNE_TSC") or os.path.join(ext, "node_modules", ".bin", "tsc")
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
    done = subprocess.run([node, "-e", _NODE_SCRIPT, js, str(path), pubkey], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_the_extension_accepts_a_release_carrying_the_ladder(archive, keypair, tmp_path):
    m = release.make_manifest("2.4.0", archive, "abc1234", BUILT_AT, "both",
                              app_models=list(APP_TIERS.values()), coding_models=SHIPPED)
    signed = release.sign_manifest(m, keypair[0])
    view = _node_view(tmp_path, signed, keypair[1])
    assert view["error"] is None
    assert base64.b64decode(view["canonical"]) == release.canonical_bytes(signed)
    assert view["ok"] is True, view["reason"]


def test_the_extension_would_reject_a_boolean_which_is_why_none_is_signed(archive, keypair, tmp_path):
    m = manifest(archive, SHIPPED)
    m[release.coding_field("qwen36-a3b", "enabled")] = True
    view = _node_view(tmp_path, release.sign_manifest(m, keypair[0]), keypair[1])
    assert view["ok"] is False
