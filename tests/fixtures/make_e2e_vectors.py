"""
Writes tests/fixtures/e2e_vectors.json: the known-answer vectors for end-to-end encryption
(docs/coding-members.md section 6). Run it once; the JSON is committed and both test suites read it
(tests/test_e2e.py in Python, vscode-extension/test/privacy-e2e.test.js in TypeScript).

    python3 tests/fixtures/make_e2e_vectors.py

This script deliberately does NOT import engine/e2e.py or the TypeScript module. It follows the
algorithm text of section 6 literally with raw primitives (X25519 and ChaCha20-Poly1305 from
`cryptography`, HKDF written out from RFC 5869 with hmac), so the vectors are a separate reading of
the spec and not the implementations agreeing with themselves. Every key, salt and nonce is derived
from a label, so running it again writes the same file byte for byte. Changing the file means changing
the wire format: do not do it casually.

What the file holds
    requests          request envelopes (unpadded and padded to 256) and the keys they derive
    stream_keys       stream_key = HKDF(resp_key, salt=rs, info="dlne-e2e-v1-resp") known answers
    responses         whole responses as sealed objects: rs on the first object, padding 0 and 256,
                      the sealed end marker, a non-streamed answer, an end-only stream
    low_level         single payloads sealed under a bare key (seal_payload / open_payload)
    request_rejects   envelopes every implementation must refuse, with the error code
    response_rejects  bare-key payloads that must be refused, with the error code
    stream_rejects    sequences of sealed objects that must fail at a given index, with the code
    stream_truncated  sequences that open fine but are not complete: close() must say "truncated"
"""

import base64
import hashlib
import hmac
import json
import os
from typing import Any, Dict, List, Optional

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "e2e_vectors.json")

AAD_REQ = b"dlne-req"
AAD_RESP = b"dlne-resp"
INFO = b"dlne-e2e-v1"
INFO_STREAM = b"dlne-e2e-v1-resp"
END = b"[DONE]"


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def label(name: str, length: int = 32) -> bytes:
    return hashlib.sha256(b"dlne-e2e-vectors/" + name.encode()).digest()[:length]


def pub_of(priv_raw: bytes) -> bytes:
    return X25519PrivateKey.from_private_bytes(priv_raw).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def hkdf_sha256(ikm: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    okm, block, counter = b"", b"", 1
    while len(okm) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        okm += block
        counter += 1
    return okm[:length]


def keys_for(eph_priv: bytes, member_pub: bytes):
    eph_pub = pub_of(eph_priv)
    shared = X25519PrivateKey.from_private_bytes(eph_priv).exchange(X25519PublicKey.from_public_bytes(member_pub))
    okm = hkdf_sha256(shared, eph_pub + member_pub, INFO, 64)
    return eph_pub, shared, okm[:32], okm[32:]


def stream_key_of(resp_key: bytes, rs: bytes) -> bytes:
    return hkdf_sha256(resp_key, rs, INFO_STREAM, 32)


def envelope_of(eph_pub: bytes, nonce: bytes, ct: bytes, v: Any = 1) -> Dict[str, Any]:
    return {"dlne_e2e": {"v": v, "epk": b64(eph_pub), "nonce": b64(nonce), "ct": b64(ct)}}


def seal(key: bytes, nonce: bytes, plaintext: bytes, aad: bytes) -> bytes:
    return ChaCha20Poly1305(key).encrypt(nonce, plaintext, aad)


def padded(data: bytes, block: int) -> bytes:
    return data + b" " * (-len(data) % block) if block > 1 else data


MEMBER_PRIV = label("member-1")
MEMBER_PUB = pub_of(MEMBER_PRIV)
OTHER_PRIV = label("member-2")
OTHER_PUB = pub_of(OTHER_PRIV)


def request_vector(name: str, plaintext: bytes, tag: str, pad_block: int) -> Dict[str, Any]:
    eph_priv = label("eph-" + tag)
    nonce = label("nonce-" + tag, 12)
    eph_pub, shared, req_key, resp_key = keys_for(eph_priv, MEMBER_PUB)
    return {
        "name": name,
        "member_priv_hex": MEMBER_PRIV.hex(),
        "member_pub_b64": b64(MEMBER_PUB),
        "eph_priv_hex": eph_priv.hex(),
        "shared_hex": shared.hex(),
        "req_key_hex": req_key.hex(),
        "resp_key_hex": resp_key.hex(),
        "pad_block": pad_block,
        "plaintext_utf8": plaintext.decode("utf-8"),
        "envelope": envelope_of(eph_pub, nonce, seal(req_key, nonce, plaintext, AAD_REQ)),
    }


def sealed_object(key: bytes, n: int, payload: bytes, pad_block: int, rs: Optional[bytes],
                  aad: bytes = AAD_RESP) -> Dict[str, Any]:
    """One response object as it goes on the wire (the inner dict). `rs` only on the first object."""
    obj: Dict[str, Any] = {"n": n}
    if rs is not None:
        obj["rs"] = b64(rs)
    obj["ct"] = b64(seal(key, n.to_bytes(12, "big"), padded(payload, pad_block), aad))
    return obj


def response_vector(name: str, resp_key: bytes, rs: bytes, payloads: List[str], pad_block: int,
                    streamed: bool) -> Dict[str, Any]:
    key = stream_key_of(resp_key, rs)
    items = [(p.encode("utf-8"), "chunk") for p in payloads]
    if streamed:
        items.append((END, "end"))
    objects = []
    for n, (payload, kind) in enumerate(items):
        data = padded(payload, pad_block)
        objects.append({
            "kind": kind,
            "payload_utf8": payload.decode("utf-8"),
            "plaintext_utf8": data.decode("utf-8"),
            "nonce_hex": n.to_bytes(12, "big").hex(),
            "sealed": sealed_object(key, n, payload, pad_block, rs if n == 0 else None),
        })
    return {"name": name, "resp_key_hex": resp_key.hex(), "rs_b64": b64(rs), "stream_key_hex": key.hex(),
            "pad_block": pad_block, "streamed": streamed, "objects": objects}


def flip(b: bytes, index: int) -> bytes:
    out = bytearray(b)
    out[index] ^= 0x01
    return bytes(out)


def main() -> None:
    chat = json.dumps({"model": "coding", "stream": True, "messages": [
        {"role": "user", "content": "def add(a, b):\n    return a + b\n# why is this slow?"}]},
        separators=(",", ":")).encode()
    unicode_body = json.dumps({"messages": [{"role": "user", "content": "naïve café — 日本語 — 🚀 \"quoted\" \\ back"}]},
                              separators=(",", ":"), ensure_ascii=False).encode()

    requests: List[Dict[str, Any]] = [
        request_vector("chat", chat, "chat", 0),
        request_vector("unicode", unicode_body, "unicode", 0),
        request_vector("padded-to-256", padded(chat, 256), "padded", 256),
    ]

    # The response key of the first request drives the response vectors.
    resp_key = bytes.fromhex(requests[0]["resp_key_hex"])
    rs = label("rs-1", 16)
    other_rs = label("rs-2", 16)
    stream = [
        '{"id":"chatcmpl-1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}',
        '{"id":"chatcmpl-1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"The loop"},"finish_reason":null}]}',
        '{"id":"chatcmpl-1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":" is fine \\u2713 ✓"},"finish_reason":null}]}',
        '{"id":"chatcmpl-1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
    ]
    whole = '{"choices":[{"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}]}'

    stream_keys = [
        {"resp_key_hex": resp_key.hex(), "rs_b64": b64(rs), "stream_key_hex": stream_key_of(resp_key, rs).hex()},
        {"resp_key_hex": resp_key.hex(), "rs_b64": b64(other_rs), "stream_key_hex": stream_key_of(resp_key, other_rs).hex()},
    ]
    assert stream_keys[0]["stream_key_hex"] != stream_keys[1]["stream_key_hex"] != resp_key.hex()

    responses = [
        response_vector("stream-pad0", resp_key, rs, stream, 0, True),
        response_vector("stream-pad256", resp_key, rs, stream, 256, True),
        response_vector("non-streamed-pad256", resp_key, other_rs, [whole], 256, False),
        response_vector("non-streamed-pad0", resp_key, other_rs, [whole], 0, False),
        response_vector("end-only", resp_key, other_rs, [], 256, True),
    ]

    sk = stream_key_of(resp_key, rs)
    low_level = []
    for name, n, payload, pad_block in (("counter zero", 0, '{"edge":"zero"}', 0),
                                        ("counter largest", 2 ** 32 - 1, '{"edge":"counter ends in ffffffff"}', 0),
                                        ("counter largest padded", 2 ** 32 - 1, '{"edge":"padded"}', 64)):
        data = padded(payload.encode(), pad_block)
        low_level.append({"name": name, "key_hex": sk.hex(), "n": n, "payload_utf8": payload, "pad_block": pad_block,
                          "plaintext_utf8": data.decode(), "nonce_hex": n.to_bytes(12, "big").hex(),
                          "ct": b64(seal(sk, n.to_bytes(12, "big"), data, AAD_RESP))})

    # ---- rejected requests ------------------------------------------------------------------
    eph_priv = label("eph-reject")
    nonce = label("nonce-reject", 12)
    eph_pub, shared, req_key, resp_key_r = keys_for(eph_priv, MEMBER_PUB)
    good_ct = seal(req_key, nonce, chat, AAD_REQ)
    good = envelope_of(eph_pub, nonce, good_ct)

    def variant(**changes: Any) -> Dict[str, Any]:
        inner = dict(good["dlne_e2e"])
        for k, val in changes.items():
            if val is None:
                inner.pop(k, None)
            else:
                inner[k] = val
        return {"dlne_e2e": inner}

    # A second spelling of the same bytes: a changed last character that only touches the unused bits.
    epk_text = b64(eph_pub)
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    last = epk_text[-2]
    noncanonical_last = alphabet[(alphabet.index(last) & ~3) | ((alphabet.index(last) + 1) & 3)]
    if noncanonical_last == last:
        noncanonical_last = alphabet[alphabet.index(last) ^ 1]
    noncanonical_epk = epk_text[:-2] + noncanonical_last + epk_text[-1]
    assert noncanonical_epk != epk_text and base64.b64decode(noncanonical_epk) == eph_pub

    def with_ct(plaintext: bytes, aad: bytes = AAD_REQ, key: bytes = req_key) -> Dict[str, Any]:
        return variant(ct=b64(seal(key, nonce, plaintext, aad)))

    other_eph_pub = pub_of(label("eph-other"))
    low_order = [bytes(32), bytes.fromhex("e0eb7a7c3b41b8ae1656e3faf19fc46ada098deb9c32b1fd866205165f49b800")]

    request_rejects: List[Dict[str, Any]] = [
        {"name": "version 2", "code": "version", "envelope": variant(v=2)},
        {"name": "version as text", "code": "version", "envelope": variant(v="1")},
        {"name": "version true", "code": "version", "envelope": variant(v=True)},
        {"name": "version missing", "code": "version", "envelope": variant(v=None)},
        {"name": "ct missing", "code": "format", "envelope": variant(ct=None)},
        {"name": "extra field", "code": "format", "envelope": variant(extra="x")},
        {"name": "extra top-level field", "code": "format", "envelope": {**good, "model": "coding"}},
        {"name": "inner is text", "code": "format", "envelope": {"dlne_e2e": "nope"}},
        {"name": "epk not base64", "code": "base64", "envelope": variant(epk="!" * 44)},
        {"name": "epk url-safe alphabet", "code": "base64", "envelope": variant(epk=epk_text.replace("+", "-").replace("/", "_") if ("+" in epk_text or "/" in epk_text) else epk_text[:-1] + "_")},
        {"name": "epk without padding", "code": "base64", "envelope": variant(epk=epk_text.rstrip("="))},
        {"name": "epk non-canonical bits", "code": "base64", "envelope": variant(epk=noncanonical_epk)},
        {"name": "ct with whitespace", "code": "base64", "envelope": variant(ct=b64(good_ct)[:8] + " " + b64(good_ct)[8:])},
        {"name": "epk as number", "code": "base64", "envelope": variant(epk=5)},
        {"name": "epk 31 bytes", "code": "length", "envelope": variant(epk=b64(eph_pub[:31]))},
        {"name": "epk 33 bytes", "code": "length", "envelope": variant(epk=b64(eph_pub + b"\x00"))},
        {"name": "nonce 11 bytes", "code": "length", "envelope": variant(nonce=b64(nonce[:11]))},
        {"name": "nonce 13 bytes", "code": "length", "envelope": variant(nonce=b64(nonce + b"\x00"))},
        {"name": "ct only a tag", "code": "length", "envelope": variant(ct=b64(good_ct[:16]))},
        {"name": "ct 5 bytes", "code": "length", "envelope": variant(ct=b64(good_ct[:5]))},
        {"name": "ct flipped first byte", "code": "auth", "envelope": variant(ct=b64(flip(good_ct, 0)))},
        {"name": "ct flipped tag", "code": "auth", "envelope": variant(ct=b64(flip(good_ct, len(good_ct) - 1)))},
        {"name": "ct truncated", "code": "auth", "envelope": variant(ct=b64(good_ct[:-1]))},
        {"name": "nonce flipped", "code": "auth", "envelope": variant(nonce=b64(flip(nonce, 0)))},
        {"name": "epk replaced by another key", "code": "auth", "envelope": variant(epk=b64(other_eph_pub))},
        {"name": "aad is the response label", "code": "auth", "envelope": with_ct(chat, aad=AAD_RESP)},
        {"name": "aad is empty", "code": "auth", "envelope": with_ct(chat, aad=b"")},
        {"name": "sealed under the response key", "code": "auth", "envelope": with_ct(chat, key=resp_key_r)},
        {"name": "low-order epk (all zero)", "code": "key", "envelope": variant(epk=b64(low_order[0]))},
        {"name": "low-order epk (order 8)", "code": "key", "envelope": variant(epk=b64(low_order[1]))},
        {"name": "body is not JSON", "code": "json", "envelope": with_ct(b"hello, not json")},
        {"name": "body is a JSON list", "code": "json", "envelope": with_ct(b'[1,2,3]')},
        {"name": "body is invalid UTF-8", "code": "json", "envelope": with_ct(b'{"a":"\xff\xfe"}')},
    ]
    for item in request_rejects:
        item["member_priv_hex"] = MEMBER_PRIV.hex()
    # An envelope that is fine but sealed for someone else.
    other_eph = label("eph-for-other")
    o_pub, _, o_req, _ = keys_for(other_eph, OTHER_PUB)
    request_rejects.append({
        "name": "sealed for a different member", "code": "auth", "member_priv_hex": MEMBER_PRIV.hex(),
        "envelope": envelope_of(o_pub, nonce, seal(o_req, nonce, chat, AAD_REQ))})

    # ---- rejected bare-key payloads ----------------------------------------------------------
    payload = stream[1]
    n1 = (1).to_bytes(12, "big")
    good_resp = seal(sk, n1, payload.encode(), AAD_RESP)
    wrong_key = label("some-other-stream-key")
    response_rejects: List[Dict[str, Any]] = [
        {"name": "ct flipped", "code": "auth", "n": 1, "ct": b64(flip(good_resp, 3))},
        {"name": "tag flipped", "code": "auth", "n": 1, "ct": b64(flip(good_resp, len(good_resp) - 1))},
        {"name": "truncated", "code": "auth", "n": 1, "ct": b64(good_resp[:-1])},
        {"name": "counter changed (sealed for 1, shown as 2)", "code": "auth", "n": 2, "ct": b64(good_resp)},
        {"name": "aad is the request label", "code": "auth", "n": 1, "ct": b64(seal(sk, n1, payload.encode(), AAD_REQ))},
        {"name": "aad is empty", "code": "auth", "n": 1, "ct": b64(seal(sk, n1, payload.encode(), b""))},
        {"name": "wrong key", "code": "auth", "n": 1, "ct": b64(seal(wrong_key, n1, payload.encode(), AAD_RESP))},
        {"name": "sealed under resp_key, not the stream key", "code": "auth", "n": 1, "ct": b64(seal(resp_key, n1, payload.encode(), AAD_RESP))},
        {"name": "not base64", "code": "base64", "n": 1, "ct": "not base64 at all"},
        {"name": "no padding", "code": "base64", "n": 1, "ct": b64(good_resp).rstrip("=") if b64(good_resp).endswith("=") else b64(good_resp) + "A"},
        {"name": "ct is a number", "code": "base64", "n": 1, "ct": 7},
        {"name": "ct only a tag", "code": "length", "n": 1, "ct": b64(good_resp[:16])},
        {"name": "ct 3 bytes", "code": "length", "n": 1, "ct": b64(good_resp[:3])},
        {"name": "counter negative", "code": "counter", "n": -1, "ct": b64(good_resp)},
        {"name": "counter too large", "code": "counter", "n": 2 ** 32, "ct": b64(good_resp)},
        {"name": "counter fractional", "code": "counter", "n": 1.5, "ct": b64(good_resp)},
        {"name": "counter as text", "code": "counter", "n": "1", "ct": b64(good_resp)},
        {"name": "counter null", "code": "counter", "n": None, "ct": b64(good_resp)},
    ]
    for item in response_rejects:
        item["key_hex"] = sk.hex()

    # ---- rejected and cut-short streams ---------------------------------------------------------
    def obj(n: int, text: str = "", pad_block: int = 256, with_rs: Optional[bytes] = None, key: bytes = sk,
            aad: bytes = AAD_RESP, final: bool = False) -> Dict[str, Any]:
        data = END if final else (text or stream[min(n, 3)]).encode()
        return sealed_object(key, n, data, pad_block, with_rs if with_rs is not None else (rs if n == 0 else None), aad)

    def tampered(o: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(o)
        out["ct"] = b64(flip(base64.b64decode(o["ct"]), 4))
        return out

    def with_fields(o: Dict[str, Any], **changes: Any) -> Dict[str, Any]:
        out = dict(o)
        for k, val in changes.items():
            if val is None:
                out.pop(k, None)
            else:
                out[k] = val
        return out

    def case(name: str, code: str, objects: List[Dict[str, Any]], fail_at: int, streamed: bool = True) -> Dict[str, Any]:
        return {"name": name, "code": code, "resp_key_hex": resp_key.hex(), "streamed": streamed,
                "objects": objects, "fail_at": fail_at}

    o0, o1, o2, o3 = obj(0), obj(1), obj(2), obj(3)
    end4 = obj(4, final=True)
    flipped_rs = flip(rs, 0)
    stream_rejects: List[Dict[str, Any]] = [
        case("first object has no rs", "format", [with_fields(o0, rs=None)], 0),
        case("rs 15 bytes", "length", [with_fields(o0, rs=b64(rs[:15]))], 0),
        case("rs 17 bytes", "length", [with_fields(o0, rs=b64(rs + b"\x00"))], 0),
        case("rs not base64", "base64", [with_fields(o0, rs="!" * 24)], 0),
        case("rs without padding", "base64", [with_fields(o0, rs=b64(rs).rstrip("="))], 0),
        case("rs is a number", "base64", [with_fields(o0, rs=5)], 0),
        case("rs on a later object", "format", [o0, with_fields(o1, rs=b64(rs))], 1),
        case("extra field on the first object", "format", [with_fields(o0, extra="x")], 0),
        case("extra field on a later object", "format", [o0, with_fields(o1, extra="x")], 1),
        case("object is not a dict", "format", [o0, "text"], 1),
        case("rs bit flipped", "auth", [with_fields(o0, rs=b64(flipped_rs))], 0),
        case("sealed under resp_key, not the stream key", "auth", [obj(0, key=resp_key)], 0),
        case("first chunk tampered", "auth", [tampered(o0)], 0),
        case("third chunk tampered", "auth", [o0, o1, tampered(o2)], 2),
        case("end marker tampered", "auth", [o0, o1, o2, o3, tampered(end4)], 4),
        case("aad is the request label", "auth", [obj(0, aad=AAD_REQ)], 0),
        case("chunk sealed for another counter", "auth", [o0, with_fields(o1, ct=obj(5)["ct"])], 1),
        case("ct not base64", "base64", [with_fields(o0, ct="????")], 0),
        case("ct only a tag", "length", [with_fields(o0, ct=b64(base64.b64decode(o0["ct"])[:16]))], 0),
        case("first counter is 1", "sequence", [with_fields(o1, rs=b64(rs))], 0),
        case("counter repeated", "sequence", [o0, with_fields(o0, rs=None)], 1),
        case("counter skipped", "sequence", [o0, o2], 1),
        case("counters reordered", "sequence", [o0, o2, o1], 1),
        case("counter as text", "sequence", [o0, with_fields(o1, n="1")], 1),
        case("counter as true", "sequence", [o0, with_fields(o1, n=True)], 1),
        case("data after the sealed end", "sequence", [o0, o1, o2, o3, end4, obj(5)], 5),
        case("second object of a non-streamed answer", "sequence", [o0, o1], 1, streamed=False),
    ]

    stream_truncated: List[Dict[str, Any]] = [
        {"name": "no sealed end (plain [DONE] only)", "resp_key_hex": resp_key.hex(), "streamed": True,
         "objects": [o0, o1, o2, o3]},
        {"name": "only the first chunk", "resp_key_hex": resp_key.hex(), "streamed": True, "objects": [o0]},
        {"name": "nothing at all", "resp_key_hex": resp_key.hex(), "streamed": True, "objects": []},
        {"name": "a non-streamed answer read as a stream", "resp_key_hex": resp_key.hex(), "streamed": True,
         "objects": [response_vector("x", resp_key, rs, [whole], 256, False)["objects"][0]["sealed"]]},
    ]

    doc = {
        "about": "Known-answer vectors for docs/coding-members.md section 6. Generated by "
                 "tests/fixtures/make_e2e_vectors.py from the algorithm text, not from either implementation.",
        "algorithm": "x25519-hkdf-sha256-chacha20poly1305",
        "version": 1,
        "requests": requests,
        "stream_keys": stream_keys,
        "responses": responses,
        "low_level": low_level,
        "request_rejects": request_rejects,
        "response_rejects": response_rejects,
        "stream_rejects": stream_rejects,
        "stream_truncated": stream_truncated,
    }
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"wrote {OUT}: {len(requests)} requests, {len(responses)} responses, {len(low_level)} low-level, "
          f"{len(request_rejects)} request rejects, {len(response_rejects)} payload rejects, "
          f"{len(stream_rejects)} stream rejects, {len(stream_truncated)} truncated")


if __name__ == "__main__":
    main()
