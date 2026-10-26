"""
End-to-end encryption between the VS Code extension and a coding member (docs/coding-members.md, §6).

SECURITY NOTE
    What this protects.  The coordinator, the relay tunnel and everything on the network between the
    editor and the member carry only ciphertext of the user's code and of the model's answer. A changed,
    repeated, reordered or dropped message is detected (ChaCha20-Poly1305 is authenticated, the response
    counter must arrive 0, 1, 2, ... with no gaps), and so is a stream cut short: a stream only counts as
    complete after a sealed end marker. Requests and response chunks are padded to 256-byte blocks, so
    the length of a sealed message does not show the length of a token.

    What it does NOT protect.
      * The member's own memory. A GPU has to see plaintext to run the model, and consumer cards have no
        confidential computing: whoever owns the member machine can read every prompt that reaches it.
        That is why privacy mode "trusted" exists (sensitive workspaces only go to members the operator
        trusts). End-to-end encryption does not replace that trust; it takes the coordinator and the
        network out of it.
      * Whose key it is. The member's public key reaches the client through the coordinator, so a
        coordinator that swaps in its own key reads everything. The operator's machine is trusted not to
        do that; nothing in this module can check it.
      * Timing and the number of chunks (one per token) are still visible. Block padding hides sizes only.
      * Past traffic if a member's private key is stolen: only the client key is ephemeral. `KeyRing`
        replaces the member key every 6 hours and forgets the old one 15 minutes later.
      * This host's memory. Python cannot reliably wipe keys; nothing here tries to.

    Replays.  A replayed request envelope must not make a member seal a second answer under the same
    key and nonces (the XOR of the two would leak). Two things stop it: every response is sealed with a
    stream key that mixes in a fresh random salt `rs` chosen by the member, and `open_request` refuses a
    client key (epk) it has already seen, with a bounded in-memory guard (lost on restart; the salt
    still holds then).

Algorithm x25519-hkdf-sha256-chacha20poly1305, version 1 (docs/coding-members.md section 6):
    eph        = fresh X25519 key pair made by the client for this one request
    shared     = X25519(eph_priv, member_pub)               (all-zero result refused)
    okm        = HKDF-SHA256(ikm=shared, salt=eph_pub || member_pub, info=b"dlne-e2e-v1", length=64)
    req_key    = okm[0:32]            resp_key = okm[32:64]
    request    = {"dlne_e2e": {"v": 1, "epk": b64(eph_pub), "nonce": b64(12 random bytes),
                               "ct": b64(ChaCha20-Poly1305(req_key, nonce, json_bytes, aad=b"dlne-req"))}}
    rs         = 16 random bytes chosen by the member for each response, sent in its first sealed object
    stream_key = HKDF-SHA256(ikm=resp_key, salt=rs, info=b"dlne-e2e-v1-resp", length=32)
    response   = per sealed object: {"n": counter, "ct": b64(ChaCha20-Poly1305(stream_key,
                 nonce=counter as 12-byte big-endian, payload, aad=b"dlne-resp"))}; the object with
                 n = 0 also carries "rs": b64(rs)
    end        = for a stream, one more sealed object whose plaintext is the bytes [DONE], then the plain
                 `data: [DONE]` line; a non-streamed answer is a single object (n = 0) and has no end
    padding    = request json and response payloads get trailing spaces up to a multiple of 256 bytes
                 before sealing (JSON ignores them; the end marker is compared with them trimmed)
    "ct" is ciphertext followed by the 16-byte tag. Every base64 string is the canonical standard
    alphabet with padding; anything else is refused.

Use the helpers, never count nonces by hand: `KeyRing` (member keys and opening requests),
`ResponseSealer` (member side of a response), `ResponseOpener` (client side), `seal_request` (client).
`seal_payload` / `open_payload` are the low-level pair: they seal with exactly the key you hand them (the
stream key, see `derive_stream_key`) and know nothing about `rs` or the end marker.

Points the contract text left open, resolved as follows (do not change silently):
    * `seal_request(...)` returns the whole wire body `{"dlne_e2e": {...}}`; `open_request` takes that
      (it also accepts the bare inner dict).
    * A sealed object is the inner dict `{"n", "ct"}` (the first also has "rs"); the caller writes
      `data: ` + json.dumps({"dlne_e2e": sealed}). `ResponseOpener.open` accepts either form.
    * Counters are capped at 2**32 - 1 and must arrive exactly in order (a missing chunk is a dropped
      token). Bodies and payloads are capped at 4 MiB.

Errors are `E2EError` with a short `code` and a fixed message. They never contain request, response or
key material, and not even the names of unexpected fields.
"""

import base64
import hmac
import json
import os
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Optional, Tuple

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

ALGORITHM = "x25519-hkdf-sha256-chacha20poly1305"
VERSION = 1
WRAPPER_KEY = "dlne_e2e"

HKDF_INFO = b"dlne-e2e-v1"
HKDF_INFO_STREAM = b"dlne-e2e-v1-resp"
AAD_REQUEST = b"dlne-req"
AAD_RESPONSE = b"dlne-resp"
END_MARKER = b"[DONE]"

KEY_LEN = 32
NONCE_LEN = 12
TAG_LEN = 16
RS_LEN = 16

MAX_BODY_BYTES = 4 * 1024 * 1024
MAX_COUNTER = 2 ** 32 - 1
PAD_BLOCK = 256                           # default padding of requests and response chunks
ROTATE_EVERY_SECONDS = 6 * 60 * 60        # KeyRing: new member key this often
KEEP_PREVIOUS_SECONDS = 15 * 60           # KeyRing: how long a retired key still opens requests
_ZERO_SHARED = bytes(KEY_LEN)
_REQUEST_FIELDS = frozenset(("v", "epk", "nonce", "ct"))


class E2EError(ValueError):
    """Any refusal. `code` is one of: format, version, base64, length, size, key, auth, json,
    counter, sequence, closed, truncated, replay. The message is fixed text and never echoes input."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> "E2EError":
    return E2EError(code, message)


# --------------------------------------------------------------------------------------------------
# Encoding helpers
# --------------------------------------------------------------------------------------------------

def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(value: Any, field: str, *, exact: Optional[int] = None, minimum: int = 0,
          maximum: Optional[int] = None, too_big: str = "length") -> bytes:
    """Strict base64: a str in the standard alphabet with padding, canonical (re-encoding gives back
    exactly the same text), decoding to a length that fits the field. The text length is checked
    before decoding, so a huge string is refused without being decoded."""
    if not isinstance(value, str):
        raise _fail("base64", f"{field} must be a base64 string")
    limit = exact if exact is not None else maximum
    if limit is not None and len(value) > 4 * ((limit + 2) // 3):
        raise _fail(too_big, f"{field} is too large")
    raw = None
    try:
        raw = base64.b64decode(value, validate=True)
    except Exception:
        pass  # raised below, outside the handler, so no chained library error rides along
    if raw is None:
        raise _fail("base64", f"{field} is not valid base64")
    if not hmac.compare_digest(_b64e(raw).encode("ascii"), value.encode("ascii")):
        raise _fail("base64", f"{field} is not canonical base64")
    if exact is not None and len(raw) != exact:
        raise _fail("length", f"{field} has the wrong length")
    if maximum is not None and len(raw) > maximum:
        raise _fail(too_big, f"{field} is too large")
    if len(raw) < minimum:
        raise _fail("length", f"{field} has the wrong length")
    return raw


def _check_key(key: Any, name: str) -> bytes:
    if not isinstance(key, (bytes, bytearray)) or len(key) != KEY_LEN:
        raise _fail("key", f"{name} must be {KEY_LEN} bytes")
    return bytes(key)


def _json_bytes(body: Any) -> bytes:
    if not isinstance(body, dict):
        raise _fail("json", "request body must be a JSON object")
    data = None
    try:
        data = json.dumps(body, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        pass  # raised below, outside the handler: a chained error could carry the offending text
    if data is None:
        raise _fail("json", "request body is not serializable")
    return data


def _pad(data: bytes, pad_block: int) -> bytes:
    """Append spaces up to a multiple of `pad_block` (JSON ignores trailing whitespace)."""
    if isinstance(pad_block, bool) or not isinstance(pad_block, int) or pad_block < 0 or pad_block > MAX_BODY_BYTES:
        raise _fail("length", "pad_block must be 0 or a positive size")
    if pad_block <= 1:
        return data
    missing = -len(data) % pad_block
    return data + b" " * min(missing, max(0, MAX_BODY_BYTES - len(data)))


def _decrypt(key: bytes, nonce: bytes, aad: bytes, ct: bytes, failure: str) -> bytes:
    plaintext = None
    try:
        plaintext = ChaCha20Poly1305(key).decrypt(nonce, ct, aad)
    except InvalidTag:
        pass
    if plaintext is None:
        raise _fail("auth", failure)
    return plaintext


def _check_plaintext(data: Any, what: str) -> bytes:
    if not isinstance(data, (bytes, bytearray)):
        raise _fail("format", f"{what} must be bytes")
    if len(data) == 0:
        raise _fail("length", f"{what} must not be empty")
    if len(data) > MAX_BODY_BYTES:
        raise _fail("size", f"{what} is too large")
    return bytes(data)


# --------------------------------------------------------------------------------------------------
# Key agreement
# --------------------------------------------------------------------------------------------------

def _derive(priv: X25519PrivateKey, peer_pub_raw: bytes, eph_pub: bytes, member_pub: bytes) -> Tuple[bytes, bytes]:
    shared = None
    try:
        shared = priv.exchange(X25519PublicKey.from_public_bytes(peer_pub_raw))
    except Exception:
        pass  # raised below, outside the handler
    # A low-order point makes the shared secret all zero (some libraries refuse it themselves), which an
    # attacker can compute. Refuse it.
    if shared is None or hmac.compare_digest(shared, _ZERO_SHARED):
        raise _fail("key", "key agreement failed")
    okm = HKDF(algorithm=hashes.SHA256(), length=2 * KEY_LEN, salt=eph_pub + member_pub,
               info=HKDF_INFO).derive(shared)
    return okm[:KEY_LEN], okm[KEY_LEN:]


def _raw_public(priv: X25519PrivateKey) -> bytes:
    return priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def generate_keypair() -> Tuple[bytes, str]:
    """A member's long-lived key pair: (raw 32-byte private key, base64 raw 32-byte public key)."""
    priv = X25519PrivateKey.generate()
    raw = priv.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                             serialization.NoEncryption())
    return raw, _b64e(_raw_public(priv))


# --------------------------------------------------------------------------------------------------
# Replay guard
# --------------------------------------------------------------------------------------------------

class ReplayGuard:
    """Remembers the client keys (epk) of recent requests so one is never opened twice. Bounded: the
    oldest entries fall out first. Thread-safe."""

    def __init__(self, max_entries: int = 65536):
        self._max = max(1, int(max_entries))
        self._seen: "OrderedDict[bytes, None]" = OrderedDict()
        self._lock = threading.Lock()

    def reserve(self, epk: bytes) -> bool:
        with self._lock:
            if epk in self._seen:
                return False
            self._seen[epk] = None
            while len(self._seen) > self._max:
                self._seen.popitem(last=False)
            return True

    def release(self, epk: bytes) -> None:
        """Forget a reservation whose request then failed to open (so a forged copy of a real
        request's epk cannot burn it)."""
        with self._lock:
            self._seen.pop(epk, None)

    def clear(self) -> None:
        with self._lock:
            self._seen.clear()


DEFAULT_REPLAY_GUARD = ReplayGuard()

_USE_DEFAULT = object()


# --------------------------------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------------------------------

def is_envelope(obj: Any) -> bool:
    """True when a parsed request body is an end-to-end envelope (it carries the dlne_e2e key)."""
    return isinstance(obj, dict) and WRAPPER_KEY in obj


def seal_request(body: Dict[str, Any], member_pub_b64: str, *, pad_block: int = PAD_BLOCK) -> Tuple[Dict[str, Any], bytes]:
    """Client side: encrypt a JSON request for a member. Returns (wire body, resp_key). The JSON is
    padded with trailing spaces to a multiple of `pad_block` (256 by default; 0 turns it off)."""
    member_pub = _b64d(member_pub_b64, "member key", exact=KEY_LEN)
    plaintext = _check_plaintext(_pad(_json_bytes(body), pad_block), "request body")
    eph = X25519PrivateKey.generate()
    eph_pub = _raw_public(eph)
    req_key, resp_key = _derive(eph, member_pub, eph_pub, member_pub)
    nonce = os.urandom(NONCE_LEN)
    ct = ChaCha20Poly1305(req_key).encrypt(nonce, plaintext, AAD_REQUEST)
    envelope = {WRAPPER_KEY: {"v": VERSION, "epk": _b64e(eph_pub), "nonce": _b64e(nonce), "ct": _b64e(ct)}}
    return envelope, resp_key


def open_request(envelope: Dict[str, Any], priv: bytes, *, replay_guard: Any = _USE_DEFAULT) -> Tuple[Dict[str, Any], bytes]:
    """Member side: decrypt a request. Returns (the JSON body as a dict, resp_key).

    `replay_guard` defaults to the module-wide guard; pass None to turn replay protection off (tests)
    or your own ReplayGuard. Raises E2EError on anything wrong; the message never echoes input."""
    guard = DEFAULT_REPLAY_GUARD if replay_guard is _USE_DEFAULT else replay_guard

    if not isinstance(envelope, dict):
        raise _fail("format", "not an end-to-end envelope")
    inner = envelope
    if WRAPPER_KEY in envelope:
        if set(envelope) != {WRAPPER_KEY}:
            raise _fail("format", "unexpected field in envelope")
        inner = envelope[WRAPPER_KEY]
        if not isinstance(inner, dict):
            raise _fail("format", "not an end-to-end envelope")
    version = inner.get("v")
    if type(version) is not int or version != VERSION:
        raise _fail("version", "unsupported envelope version")
    if set(inner) != _REQUEST_FIELDS:
        raise _fail("format", "envelope fields are wrong")

    epk = _b64d(inner["epk"], "epk", exact=KEY_LEN)
    nonce = _b64d(inner["nonce"], "nonce", exact=NONCE_LEN)
    ct = _b64d(inner["ct"], "ct", minimum=TAG_LEN + 1, maximum=MAX_BODY_BYTES + TAG_LEN, too_big="size")

    member_priv_raw = _check_key(priv, "member private key")
    member_priv = None
    try:
        member_priv = X25519PrivateKey.from_private_bytes(member_priv_raw)
    except Exception:
        pass  # raised below, outside the handler
    if member_priv is None:
        raise _fail("key", "bad member private key")
    member_pub = _raw_public(member_priv)
    req_key, resp_key = _derive(member_priv, epk, epk, member_pub)

    reserved = False
    if guard is not None:
        if not guard.reserve(epk):
            raise _fail("replay", "this request was already received")
        reserved = True
    try:
        plaintext = _decrypt(req_key, nonce, AAD_REQUEST, ct, "request authentication failed")
        body = None
        try:
            body = json.loads(plaintext.decode("utf-8"))
        except (ValueError, RecursionError):  # ValueError covers UnicodeDecodeError and JSONDecodeError (which keeps the text)
            pass  # raised below, outside the handler, so no chained error holds the plaintext
        if not isinstance(body, dict):
            raise _fail("json", "request body must be a JSON object")
    except BaseException:
        if reserved:
            guard.release(epk)
        raise
    return body, resp_key


# --------------------------------------------------------------------------------------------------
# Responses
# --------------------------------------------------------------------------------------------------

def _check_counter(n: Any) -> int:
    if type(n) is not int or n < 0 or n > MAX_COUNTER:
        raise _fail("counter", "bad response counter")
    return n


def derive_stream_key(resp_key: bytes, rs: bytes) -> bytes:
    """stream_key = HKDF-SHA256(ikm=resp_key, salt=rs, info=b"dlne-e2e-v1-resp", length=32): the key a
    response is actually sealed with. `rs` is 16 random bytes the member picks for each response."""
    key = _check_key(resp_key, "response key")
    if not isinstance(rs, (bytes, bytearray)) or len(rs) != RS_LEN:
        raise _fail("length", "rs has the wrong length")
    return HKDF(algorithm=hashes.SHA256(), length=KEY_LEN, salt=bytes(rs), info=HKDF_INFO_STREAM).derive(key)


def seal_payload(resp_key: bytes, n: int, payload: bytes, *, pad_block: int = 0) -> Dict[str, Any]:
    """Low level: seal one payload under exactly the key given (a stream key, see derive_stream_key) with
    nonce = n. Returns {"n": n, "ct": base64}; no `rs`, no end marker, padding only if `pad_block` is
    set. The caller owns the counter and must never reuse one for a key: use ResponseSealer."""
    key = _check_key(resp_key, "response key")
    counter = _check_counter(n)
    data = _check_plaintext(payload, "payload")
    data = _pad(data, pad_block)
    nonce = counter.to_bytes(NONCE_LEN, "big")
    return {"n": counter, "ct": _b64e(ChaCha20Poly1305(key).encrypt(nonce, data, AAD_RESPONSE))}


def open_payload(resp_key: bytes, n: int, ct_b64: str) -> bytes:
    """Low level: decrypt one payload under exactly the key given (a stream key). Does not check order,
    `rs` or the end marker; use ResponseOpener for a response."""
    key = _check_key(resp_key, "response key")
    counter = _check_counter(n)
    ct = _b64d(ct_b64, "ct", minimum=TAG_LEN + 1, maximum=MAX_BODY_BYTES + TAG_LEN, too_big="size")
    return _decrypt(key, counter.to_bytes(NONCE_LEN, "big"), AAD_RESPONSE, ct, "response authentication failed")


def _is_end_marker(data: bytes) -> bool:
    return data.rstrip(b" ") == END_MARKER


class ResponseSealer:
    """Member side of one response. Picks a fresh random salt `rs`, seals with the stream key derived from
    it, counts 0, 1, 2, ... so a nonce can never repeat, pads every payload to `pad_block` (256), and ends
    a stream with `finish()`.

        sealer = ResponseSealer(resp_key)
        sealed = sealer.seal(payload_bytes)       # dict; the first one also carries "rs"
        ...
        sealed = sealer.finish()                  # the sealed end marker; send the plain [DONE] after it

    Each dict goes out as `data: ` + json.dumps({"dlne_e2e": sealed}). A non-streamed answer is one
    `seal()` and no `finish()`. `rs` can be passed in for known-answer tests only."""

    def __init__(self, resp_key: bytes, *, pad_block: int = PAD_BLOCK, rs: Optional[bytes] = None):
        key = _check_key(resp_key, "response key")
        if rs is None:
            rs = os.urandom(RS_LEN)
        self._rs = bytes(rs) if isinstance(rs, (bytes, bytearray)) else b""
        self._key = derive_stream_key(key, self._rs)
        _pad(b"{}", pad_block)  # refuse a bad pad_block now, not on the first chunk
        self._pad_block = pad_block
        self._next = 0
        self._finished = False
        self._lock = threading.Lock()

    def _seal(self, payload: bytes, final: bool) -> Dict[str, Any]:
        with self._lock:
            if self._finished:
                raise _fail("sequence", "the response was already finished")
            if self._next > MAX_COUNTER:
                raise _fail("counter", "response counter exhausted")
            sealed = seal_payload(self._key, self._next, payload, pad_block=self._pad_block)
            if self._next == 0:
                sealed = {"n": 0, "rs": _b64e(self._rs), "ct": sealed["ct"]}
            self._next += 1
            self._finished = final
            return sealed

    def seal(self, payload: bytes) -> Dict[str, Any]:
        if isinstance(payload, (bytes, bytearray)) and _is_end_marker(bytes(payload)):
            raise _fail("format", "a payload may not look like the end marker; use finish()")
        return self._seal(payload, final=False)

    def finish(self) -> Dict[str, Any]:
        """The sealed end of a stream (plaintext `[DONE]`). Nothing can be sealed after it."""
        return self._seal(END_MARKER, final=True)


class ResponseOpener:
    """Client side of one response. Insists on the first object carrying `rs`, on the counter 0, 1, 2, ...
    exactly in order, and on a sealed end. After any failure it refuses everything, so a bad chunk ends the
    stream instead of being skipped.

        opener = ResponseOpener(resp_key)             # streamed=False for a non-streamed answer
        data = opener.open(sealed)                    # bytes (JSON, maybe with trailing spaces), or None
        ...                                           # None is the sealed end; opener.finished is True
        opener.close()                                # call at the plain [DONE] or at EOF: raises
                                                      # E2EError "truncated" if the sealed end never came
    """

    def __init__(self, resp_key: bytes, *, streamed: bool = True):
        self._resp_key = _check_key(resp_key, "response key")
        self._streamed = bool(streamed)
        self._stream_key: Optional[bytes] = None
        self._next = 0
        self._finished = False
        self._dead = False

    @property
    def count(self) -> int:
        """How many objects were opened so far."""
        return self._next

    @property
    def finished(self) -> bool:
        """True once the sealed end arrived (or the single object of a non-streamed answer was opened)."""
        return self._finished

    def open(self, sealed: Dict[str, Any]) -> Optional[bytes]:
        """Open the next sealed object (the inner dict, or `{"dlne_e2e": inner}`). Returns the payload
        bytes, or None for the sealed end."""
        if self._dead:
            raise _fail("closed", "response stream already failed")
        try:
            return self._open(sealed)
        except BaseException:
            self._dead = True
            raise

    def _open(self, sealed: Any) -> Optional[bytes]:
        if self._finished:
            raise _fail("sequence", "data after the end of the response")
        if not isinstance(sealed, dict):
            raise _fail("format", "not a sealed response object")
        obj = sealed
        if WRAPPER_KEY in obj:
            obj = obj[WRAPPER_KEY]
            if len(sealed) != 1 or not isinstance(obj, dict):
                raise _fail("format", "not a sealed response object")
        first = self._next == 0
        if set(obj) != ({"n", "rs", "ct"} if first else {"n", "ct"}):
            raise _fail("format", "sealed object fields are wrong")
        n = obj["n"]
        if type(n) is not int or n != self._next:
            raise _fail("sequence", "response chunk is out of order, repeated or missing")
        if first:
            self._stream_key = derive_stream_key(self._resp_key, _b64d(obj["rs"], "rs", exact=RS_LEN))
        data = open_payload(self._stream_key, n, obj["ct"])
        self._next += 1
        if not self._streamed:
            self._finished = True
            return data
        if _is_end_marker(data):
            self._finished = True
            return None
        return data

    def close(self) -> None:
        """Call when the plain `data: [DONE]` arrives or the connection ends. Raises E2EError with code
        "truncated" unless the sealed end was seen (the answer may have been cut short), or "closed" if the
        stream had already failed."""
        if self._dead:
            raise _fail("closed", "response stream already failed")
        if not self._finished:
            raise _fail("truncated", "the response ended before its sealed end")


# --------------------------------------------------------------------------------------------------
# Member keys
# --------------------------------------------------------------------------------------------------

class KeyRing:
    """A member's keys. Makes a fresh key pair at start, again on every `rotate()` (the heartbeat loop
    calls `maybe_rotate()` and so replaces the key every 6 hours), advertises the current public key as
    `public_b64`, and keeps each retired private key for 15 minutes so that routes issued just before a
    rotation still open. `open_request` tries the current key, then the retired ones, with the replay
    guard on. Nothing is written to disk. `clock` is injectable so tests need no waiting."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic, rotate_every: float = ROTATE_EVERY_SECONDS,
                 keep_previous: float = KEEP_PREVIOUS_SECONDS, replay_guard: Optional[ReplayGuard] = None):
        self._clock = clock
        self._rotate_every = rotate_every
        self._keep_previous = keep_previous
        self._guard = replay_guard if replay_guard is not None else ReplayGuard()
        self._lock = threading.Lock()
        self._retired: List[Tuple[bytes, float]] = []
        self._priv, self._pub_b64 = generate_keypair()
        self._rotated_at = clock()

    @property
    def public_b64(self) -> str:
        """The current public key, exactly as it goes into the heartbeat."""
        with self._lock:
            return self._pub_b64

    def _prune(self, now: float) -> None:
        self._retired = [(key, at) for key, at in self._retired if now - at < self._keep_previous]

    def rotate(self, now: Optional[float] = None) -> str:
        """Retire the current key and make a new one. Returns the new public key."""
        t = self._clock() if now is None else now
        with self._lock:
            self._retired.append((self._priv, t))
            self._priv, self._pub_b64 = generate_keypair()
            self._rotated_at = t
            self._prune(t)
            return self._pub_b64

    def maybe_rotate(self, now: Optional[float] = None) -> bool:
        """Rotate if the current key is `rotate_every` seconds old. Returns True when it did."""
        t = self._clock() if now is None else now
        with self._lock:
            due = t - self._rotated_at >= self._rotate_every
        if due:
            self.rotate(t)
        return due

    def open_request(self, envelope: Dict[str, Any], now: Optional[float] = None) -> Tuple[Dict[str, Any], bytes]:
        """Like the module-level open_request, with the ring's keys and replay guard. A request sealed to a
        key that is no longer kept fails with code "auth"."""
        t = self._clock() if now is None else now
        with self._lock:
            self._prune(t)
            candidates = [self._priv] + [key for key, _ in reversed(self._retired)]
        first_failure: Optional[E2EError] = None
        for priv in candidates:
            try:
                return open_request(envelope, priv, replay_guard=self._guard)
            except E2EError as exc:
                if exc.code != "auth":  # anything but "not sealed to this key" ends it now
                    raise
                if first_failure is None:
                    first_failure = exc
        assert first_failure is not None
        raise first_failure
