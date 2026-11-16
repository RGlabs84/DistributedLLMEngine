"""
The "device" tier (engine/device_licence.py): a NoClipOS install's signed LicenceKit licence, sent as
``Authorization: Bearer alic1.<base64url>``, checked offline, counted per install, and never quietly demoted to a trial
caller when it is wrong. The licences here are signed in-test with throwaway keys; nothing reads the real config. The
format document's own test vector is checked in tests/test_device_licence_vectors.py, which stays off public GitHub
(it is a signed licence, and fakes are not published).
"""

import base64
import hashlib
import struct
import time
from argparse import Namespace

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from engine import coordinator, device_licence
from engine.config import get_plain_worker_limits
from engine.device_licence import (BAD_SIGNATURE, LICENCE_PREFIX, MALFORMED, MAX_BYTES, NOT_ACCEPTED, REVOKED, VALID,
                                   WRONG_PRODUCT, DeviceLicences, decode_token, parse_licence, verify_licence)
from engine.priority import UsageWindow, tier_priority

REMOTE = ("198.51.100.40", 40000)
LOCAL = ("127.0.0.1", 1)
INSTALL_A = "aa" * 16
INSTALL_B = "bb" * 16


# ---- helpers --------------------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def signer():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def other_signer():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def pem_of(private_key) -> bytes:
    return private_key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)


def make_licence(private_key, install_hex=INSTALL_A, products=("noclipos",), issued_at=1794009600) -> bytes:
    """A format 1 licence, signed as LicenceKit's service signs it."""
    body = b"ALIC" + b"\x01" + bytes.fromhex(install_hex) + struct.pack(">q", issued_at) + bytes([len(products)])
    for product in products:
        raw = product.encode("ascii")
        body += bytes([len(raw)]) + raw + hashlib.sha256(b"alic-key-v1\n" + raw + b"\nKEY").digest()
    signature = private_key.sign(body, padding.PKCS1v15(), hashes.SHA256())
    return body + struct.pack(">H", len(signature)) + signature


def token_of(licence: bytes) -> str:
    return LICENCE_PREFIX + base64.urlsafe_b64encode(licence).rstrip(b"=").decode("ascii")


# ---- the format ---------------------------------------------------------------------------------------------------

def test_a_licence_signed_in_the_test_is_valid_for_its_product(signer):
    verdict = verify_licence(make_licence(signer), signer.public_key(), "noclipos")
    assert verdict.valid and verdict.install_id == INSTALL_A
    many = make_licence(signer, products=("a", "noclipos", "z.1-x"))
    assert verify_licence(many, signer.public_key(), "noclipos").valid


def test_wrong_product_wrong_key_and_tampering(signer, other_signer):
    key = signer.public_key()
    assert verify_licence(make_licence(signer, products=("tanks-anyway",)), key, "noclipos").reason == WRONG_PRODUCT
    assert verify_licence(make_licence(other_signer), key, "noclipos").reason == BAD_SIGNATURE
    licence = bytearray(make_licence(signer))
    licence[5] ^= 0x01                              # the install id: signed, so it no longer verifies
    assert verify_licence(bytes(licence), key, "noclipos").reason == BAD_SIGNATURE


def test_malformed_licences(signer):
    key = signer.public_key()
    good = make_licence(signer)

    def reason(data):
        return verify_licence(data, key, "noclipos").reason

    assert reason(b"") == MALFORMED
    assert reason(good[:-1]) == MALFORMED                              # truncated
    assert reason(good[:20]) == MALFORMED
    assert reason(good + b"\x00") == MALFORMED                         # bytes left over
    assert reason(b"ALIX" + good[4:]) == MALFORMED                     # magic
    assert reason(good[:4] + b"\x02" + good[5:]) == MALFORMED          # format 2
    assert reason(good + b"\x00" * MAX_BYTES) == MALFORMED             # oversized
    assert reason(make_licence(signer, products=("b", "a"))) == MALFORMED     # not ascending
    assert reason(make_licence(signer, products=("a", "a"))) == MALFORMED     # repeated
    assert reason(make_licence(signer, products=("Upper",))) == MALFORMED     # not a product id
    assert reason(make_licence(signer, products=("-lead",))) == MALFORMED
    assert reason(make_licence(signer, products=("x" * 65,))) == MALFORMED
    assert reason(make_licence(signer, products=tuple(f"p{i:02d}" for i in range(17)))) == MALFORMED
    no_products = good[:29] + b"\x00" + good[30:]
    assert reason(no_products) == MALFORMED
    # a signature that is not the key's modulus length (even if it were otherwise right) is malformed
    signed_part = good[:good.index(b"\x01\x00", 29)]
    assert reason(signed_part + b"\x00\x04abcd") == MALFORMED


def test_max_bytes_is_the_documented_formula():
    assert MAX_BYTES == 4 + 1 + 16 + 8 + 1 + 16 * (1 + 64 + 32) + 2 + 512


def test_the_token_form():
    licence = b"ALIC" + bytes(range(60))
    assert decode_token(token_of(licence)) == licence
    assert decode_token("alic1.") is None
    assert decode_token("alic1.abc=") is None                      # no padding, no stray characters
    assert decode_token("alic1.ab+/") is None                      # standard alphabet is not base64url
    assert decode_token("alic1.a") is None                         # a length no base64 has
    assert decode_token("alic1." + "A" * 5000) is None             # too long to be a licence
    assert decode_token("dlne_sk_" + "0" * 48) is None
    assert decode_token(None) is None


def test_the_licence_public_key_must_be_a_spki_pem_rsa_key(tmp_path, signer):
    good = tmp_path / "pub.pem"
    good.write_bytes(pem_of(signer))
    assert device_licence.load_public_key(str(good)).key_size == 2048
    pkcs1 = tmp_path / "pkcs1.pem"
    pkcs1.write_bytes(signer.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.PKCS1))
    for bad in (pkcs1, tmp_path / "missing.pem"):
        with pytest.raises(ValueError):
            device_licence.load_public_key(str(bad))


# ---- the checker: key, product, revocation, cache ----------------------------------------------------------------------

@pytest.fixture
def files(tmp_path, signer):
    pub = tmp_path / "noclip_pub.pem"
    pub.write_bytes(pem_of(signer))
    return {"pub": pub, "revoked": tmp_path / "revoked.txt"}


def limits_for(files, **extra):
    return {"noclip_licence_public_key": str(files["pub"]), "noclip_licence_product": "noclipos",
            "noclip_revoked_installs": str(files["revoked"]), **extra}


def test_checker_accepts_a_valid_licence_and_refuses_the_rest(files, signer, other_signer):
    checker, limits = DeviceLicences(), limits_for(files)
    assert checker.check(token_of(make_licence(signer)), limits).valid
    assert checker.check(token_of(make_licence(other_signer)), limits).reason == BAD_SIGNATURE
    assert checker.check(token_of(make_licence(signer, products=("tanks-anyway",))), limits).reason == WRONG_PRODUCT
    assert checker.check("alic1.not-a-licence", limits).reason == MALFORMED
    assert checker.check(token_of(make_licence(signer))[:-8], limits).reason == MALFORMED
    # the product is configurable
    assert checker.check(token_of(make_licence(signer, products=("tanks-anyway",))), {**limits, "noclip_licence_product": "tanks-anyway"}).valid


def test_without_a_public_key_every_licence_is_refused_with_one_log_line(files, signer, monkeypatch):
    lines = []
    monkeypatch.setattr(device_licence, "log_warning", lines.append)
    checker = DeviceLicences()
    for _ in range(5):
        assert checker.check(token_of(make_licence(signer)), limits_for(files, noclip_licence_public_key="")).reason == NOT_ACCEPTED
    assert len(lines) == 1 and "NOCLIP_LICENCE_PUBLIC_KEY" in lines[0]
    missing = limits_for(files, noclip_licence_public_key=str(files["pub"]) + ".gone")
    assert checker.check(token_of(make_licence(signer)), missing).reason == NOT_ACCEPTED


def test_revocation_is_read_live_from_the_file(files, signer):
    checker, limits = DeviceLicences(), limits_for(files)
    token = token_of(make_licence(signer))
    assert checker.check(token, limits).valid                       # no revoked file at all
    files["revoked"].write_text(f"# the owner's note\n{INSTALL_B}\n")
    assert checker.check(token, limits).valid                       # another install
    files["revoked"].write_text(f"{INSTALL_B}\n{INSTALL_A.upper()}  # shared the key\n")
    verdict = checker.check(token, limits)
    assert verdict.reason == REVOKED and verdict.install_id == INSTALL_A
    files["revoked"].write_text("\n")                               # un-revoked
    assert checker.check(token, limits).valid


def test_the_cache_is_bounded_and_keyed_by_the_licence(files, signer, monkeypatch):
    monkeypatch.setattr(device_licence, "CACHE_SIZE", 4)
    checker, limits = DeviceLicences(), limits_for(files)
    for index in range(10):
        checker.check(token_of(make_licence(signer, install_hex=f"{index:032x}")), limits)
    assert len(checker._cache) == 4
    calls = []
    real = device_licence.verify_licence
    monkeypatch.setattr(device_licence, "verify_licence", lambda *a: calls.append(1) or real(*a))
    token = token_of(make_licence(signer, install_hex=f"{9:032x}"))
    assert checker.check(token, limits).valid and checker.check(token, limits).valid
    assert calls == []                                              # the newest was still cached


def test_changing_the_key_file_drops_what_was_cached(files, signer, other_signer):
    checker, limits = DeviceLicences(), limits_for(files)
    token = token_of(make_licence(signer))
    assert checker.check(token, limits).valid
    files["pub"].write_bytes(pem_of(other_signer))
    assert checker.check(token, limits).reason == BAD_SIGNATURE


def test_the_licence_is_never_logged(files, signer, monkeypatch):
    lines = []
    monkeypatch.setattr(device_licence, "log_warning", lines.append)
    token = token_of(make_licence(signer))
    files["revoked"].write_text(INSTALL_A + "\n")
    DeviceLicences().check(token, limits_for(files))
    DeviceLicences().check(token, limits_for(files, noclip_licence_public_key=""))
    assert lines and all(token not in line and token[len(LICENCE_PREFIX):] not in line for line in lines)
    assert INSTALL_A in lines[0]                                    # an install id may be


# ---- config -----------------------------------------------------------------------------------------------------------

def test_config_defaults_and_overrides():
    limits = get_plain_worker_limits({})
    assert (limits["device_quota_chats"], limits["device_quota_window_hours"]) == (300, 5.0)
    assert limits["noclip_licence_product"] == "noclipos" and limits["noclip_licence_public_key"] == ""
    limits = get_plain_worker_limits({"DEVICE_QUOTA_CHATS": "25", "DEVICE_QUOTA_WINDOW_HOURS": "2.5",
                                      "NOCLIP_LICENCE_PUBLIC_KEY": " /etc/dlne/noclip.pem ",
                                      "NOCLIP_LICENCE_PRODUCT": "noclipos.beta", "NOCLIP_REVOKED_INSTALLS": "/x/r.txt"})
    assert (limits["device_quota_chats"], limits["device_quota_window_hours"]) == (25, 2.5)
    assert limits["noclip_licence_public_key"] == "/etc/dlne/noclip.pem"
    assert limits["noclip_licence_product"] == "noclipos.beta" and limits["noclip_revoked_installs"] == "/x/r.txt"
    for bad in ({"DEVICE_QUOTA_CHATS": "0"}, {"DEVICE_QUOTA_CHATS": "lots"}, {"DEVICE_QUOTA_WINDOW_HOURS": "nan"},
                {"DEVICE_QUOTA_WINDOW_HOURS": "0"}):
        limits = get_plain_worker_limits(bad)
        assert (limits["device_quota_chats"], limits["device_quota_window_hours"]) == (300, 5.0), bad


def test_priority_is_the_cpu_tiers():
    assert tier_priority("device") == tier_priority("cpu") == 1


# ---- over HTTP: the real middleware, /v1/me and the tiers around it ------------------------------------------------------

CPU_NODE = {"worker_id": "cpu-node", "hardware": {
    "gpu": {"has_gpu": False, "vram_total_gb": 0}, "ram_gb": 12, "cpu_cores": 6, "cpu_arch": "x86_64",
    "cpu_features": ["avx2", "fma"], "mem_bandwidth_gbs": 48.0}}


class Env:
    pass


@pytest.fixture
def env(tmp_path, monkeypatch, files):
    quota = coordinator.StarterQuotaManager(str(tmp_path / "q.json"), default_quota=3)
    monkeypatch.setattr(coordinator, "starter_quota_mgr", quota)
    monkeypatch.setattr(coordinator, "cpu_coding_window", UsageWindow())
    monkeypatch.setattr(coordinator, "device_coding_window", UsageWindow())
    monkeypatch.setattr(coordinator, "device_licences", DeviceLicences())
    real = coordinator.get_worker_limits
    limits = {"enforce_contrib_clause": True, "starter_quota_inferences": 3, "device_quota_chats": 2,
              "device_quota_window_hours": 5.0, **limits_for(files)}
    monkeypatch.setattr(coordinator, "get_worker_limits", lambda: {**real(), **limits})
    monkeypatch.setattr(coordinator.registry, "find_by_token", lambda t: CPU_NODE if t == "cpu-token" else None)
    monkeypatch.setattr(coordinator.api_key_manager, "verify_key", lambda t: t == "dlne_sk_good")

    test_app = FastAPI()
    test_app.add_middleware(coordinator.NonContribBlockerMiddleware)
    test_app.add_api_route("/v1/me", coordinator.get_me, methods=["GET"])

    @test_app.post("/v1/chat/completions")
    async def chat(request: Request):
        return {"tier": getattr(request.state, "dlne_tier", None), "may_choose_model": coordinator._may_choose_model(request)}

    @test_app.post("/v1/completions")
    async def complete(request: Request):
        return {"tier": getattr(request.state, "dlne_tier", None)}

    e = Env()
    e.app, e.quota, e.limits = test_app, quota, limits
    return e


def caller(env, token=None, ip=REMOTE):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return AsyncClient(transport=ASGITransport(app=env.app, client=ip), base_url="http://t", headers=headers)


@pytest.mark.anyio
async def test_me_says_device_for_a_valid_licence(env, signer):
    async with caller(env, token_of(make_licence(signer))) as c:
        reply = await c.get("/v1/me")
    assert reply.status_code == 200 and reply.json() == {"tier": "device"}
    assert reply.headers["cache-control"] == "no-store"
    assert env.quota._usage == {}                                    # costs nothing


@pytest.mark.anyio
async def test_other_tiers_are_what_they_were(env):
    for token, ip, tier in (("dlne_sk_good", REMOTE, "key"), ("cpu-token", REMOTE, "cpu"), (None, REMOTE, "trial"),
                            ("not-a-key", REMOTE, "trial"), (None, LOCAL, "host")):
        async with caller(env, token, ip) as c:
            reply = await c.get("/v1/me")
        assert reply.status_code == 200 and reply.json() == {"tier": tier}, (token, tier)
    async with caller(env, "dlne_sk_good") as c:
        for _ in range(10):                                          # a key is never counted
            reply = await c.post("/v1/chat/completions")
            assert reply.status_code == 200 and reply.json()["tier"] == "key" and reply.json()["may_choose_model"] is True
            assert "x-dlne-quota-remaining" not in reply.headers


@pytest.mark.anyio
async def test_a_device_chat_is_counted_per_install_with_the_quota_headers(env, signer):
    a, b = token_of(make_licence(signer, INSTALL_A)), token_of(make_licence(signer, INSTALL_B))
    async with caller(env, a) as ca, caller(env, b) as cb:
        first = await ca.post("/v1/chat/completions")
        assert first.status_code == 200 and first.json() == {"tier": "device", "may_choose_model": False}
        assert first.headers["x-dlne-quota-remaining"] == "1" and first.headers["x-dlne-limit-remaining"] == "1"
        assert int(first.headers["x-dlne-limit-reset"]) > time.time()
        assert (await ca.post("/v1/chat/completions")).headers["x-dlne-quota-remaining"] == "0"
        spent = await ca.post("/v1/chat/completions")                # the third: A's window is spent
        assert spent.status_code == 429
        assert spent.headers["x-dlne-quota-remaining"] == "0" and int(spent.headers["retry-after"]) >= 1
        assert "window" in spent.json()["detail"]
        other = await cb.post("/v1/chat/completions")                # B has its own
        assert other.status_code == 200 and other.headers["x-dlne-quota-remaining"] == "1"
    assert env.quota._usage == {}                                    # no trial chat was touched


@pytest.mark.anyio
async def test_the_same_install_on_two_networks_shares_one_window(env, signer):
    token = token_of(make_licence(signer, INSTALL_A))
    async with caller(env, token, ("198.51.100.1", 1)) as c1, caller(env, token, ("203.0.113.8", 1)) as c2:
        assert (await c1.post("/v1/chat/completions")).status_code == 200
        assert (await c2.post("/v1/chat/completions")).status_code == 200
        assert (await c1.post("/v1/chat/completions")).status_code == 429


@pytest.mark.anyio
async def test_autocomplete_is_never_counted(env, signer):
    async with caller(env, token_of(make_licence(signer))) as c:
        for _ in range(10):
            reply = await c.post("/v1/completions")
            assert reply.status_code == 200 and reply.json() == {"tier": "device"}
        assert (await c.post("/v1/chat/completions")).headers["x-dlne-quota-remaining"] == "1"


@pytest.mark.anyio
async def test_a_chat_given_back_is_not_counted(env, signer):
    request = type("R", (), {})()
    request.state = type("S", (), {"device_window_key": INSTALL_A})()
    request.url = type("U", (), {"path": "/v1/chat/completions"})()
    window = coordinator.device_coding_window
    window.check_and_record(INSTALL_A, 2, 5 * 3600)
    coordinator._refund_chat(request)
    coordinator._refund_chat(request)                                # once only
    assert window.peek(INSTALL_A, 2, 5 * 3600)[0] == 2


@pytest.mark.anyio
@pytest.mark.parametrize("which", ["wrong_signature", "wrong_product", "malformed", "truncated", "oversized", "revoked"])
async def test_a_licence_that_does_not_check_out_is_401_never_a_trial(env, signer, other_signer, files, which):
    licences = {
        "wrong_signature": token_of(make_licence(other_signer)),
        "wrong_product": token_of(make_licence(signer, products=("tanks-anyway",))),
        "malformed": LICENCE_PREFIX + "!!!!",
        "truncated": token_of(make_licence(signer))[:-40],
        "oversized": LICENCE_PREFIX + "A" * 6000,
        "revoked": token_of(make_licence(signer, INSTALL_B)),
    }
    files["revoked"].write_text(INSTALL_B + "\n")
    async with caller(env, licences[which]) as c:
        for route in ("/v1/chat/completions", "/v1/completions"):
            reply = await c.post(route)
            assert reply.status_code == 401, (route, reply.text)
            assert reply.json()["detail"] and len(reply.json()["detail"]) < 100
            assert licences[which] not in reply.text
        reply = await c.get("/v1/me")
        assert reply.status_code == 401 and reply.json()["detail"]
    assert env.quota._usage == {}                                    # nothing was charged to the network's trial


@pytest.mark.anyio
async def test_no_public_key_configured_refuses_licences(env, signer):
    env.limits["noclip_licence_public_key"] = ""
    async with caller(env, token_of(make_licence(signer))) as c:
        reply = await c.post("/v1/chat/completions")
    assert reply.status_code == 401 and "does not accept" in reply.json()["detail"]


@pytest.mark.anyio
async def test_a_revocation_applies_to_the_next_request(env, signer, files):
    async with caller(env, token_of(make_licence(signer, INSTALL_A))) as c:
        assert (await c.post("/v1/chat/completions")).status_code == 200
        files["revoked"].write_text(INSTALL_A + "\n")
        assert (await c.post("/v1/chat/completions")).status_code == 401
        assert (await c.get("/v1/me")).status_code == 401


@pytest.mark.anyio
async def test_the_clause_off_and_the_host_ignore_licences(env, signer):
    bad = LICENCE_PREFIX + "!!!!"
    async with caller(env, bad, LOCAL) as c:                         # this machine is the host whatever it sends
        assert (await c.post("/v1/chat/completions")).json()["tier"] == "host"
        assert (await c.get("/v1/me")).json() == {"tier": "host"}
    env.limits["enforce_contrib_clause"] = False
    async with caller(env, bad) as c:
        assert (await c.post("/v1/chat/completions")).json()["tier"] == "open"
        assert (await c.get("/v1/me")).json() == {"tier": "open"}


def test_check_contrib_clause_refuses_a_bad_licence_and_lets_a_counted_device_through(env, signer, other_signer):
    def request(token, tier=None):
        r = Request({"type": "http", "method": "POST", "path": "/v1/models/request", "query_string": b"", "client": REMOTE,
                     "headers": [(b"authorization", f"Bearer {token}".encode())]})
        if tier:
            r.state.dlne_tier = tier
        return r

    good = token_of(make_licence(signer))
    coordinator.check_contrib_clause(request(good, "device"))
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as refused:
        coordinator.check_contrib_clause(request(token_of(make_licence(other_signer))))
    assert refused.value.status_code == 401


# ---- the operator's command ---------------------------------------------------------------------------------------------

def test_device_revoke_and_list_revoked_edit_the_file(files, signer, monkeypatch, capsys):
    import dlne_server
    monkeypatch.setattr(dlne_server, "get_worker_limits", lambda: limits_for(files))
    monkeypatch.setattr(dlne_server, "get_base_dir", lambda: str(files["revoked"].parent))
    dlne_server.cmd_device(Namespace(device_action="revoke", install_id=INSTALL_A.upper()))
    dlne_server.cmd_device(Namespace(device_action="revoke", install_id=INSTALL_A))      # already revoked: no second line
    dlne_server.cmd_device(Namespace(device_action="revoke", install_id=INSTALL_B))
    assert device_licence.read_revoked(str(files["revoked"])) == {INSTALL_A, INSTALL_B}
    assert files["revoked"].read_text().count(INSTALL_A) == 1
    capsys.readouterr()
    dlne_server.cmd_device(Namespace(device_action="list-revoked", install_id=None))
    listing = capsys.readouterr().out
    assert INSTALL_A in listing and INSTALL_B in listing and "(2)" in listing
    assert DeviceLicences().check(token_of(make_licence(signer, INSTALL_A)), limits_for(files)).reason == REVOKED
    for bad in ("", "xyz", "aa" * 15, "aa" * 17, "gg" * 16):
        with pytest.raises(SystemExit):
            dlne_server.cmd_device(Namespace(device_action="revoke", install_id=bad))
