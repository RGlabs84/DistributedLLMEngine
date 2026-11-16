"""
The "device" tier: a NoClipOS install proves itself with a signed LicenceKit licence, checked here offline.

THE CREDENTIAL
    ``Authorization: Bearer alic1.<base64url, no padding, of the raw licence bytes>``. The prefix marks it as a licence
    and not a ``dlne_sk_`` operator key. Nothing is looked up and nothing is asked of any server: the signature is
    checked against the public key in the PEM file ``NOCLIP_LICENCE_PUBLIC_KEY`` names (unset: every licence is
    refused, with one log line saying why), and the licence must cover the product ``NOCLIP_LICENCE_PRODUCT``
    (default ``noclipos``).

LICENCE FORMAT 1 ("ALIC"), all integers big-endian (Scorched android-test docs/design/trial-unlock.md section 3,
reference implementation LicenceKit/Licence.cs; the test vector in tests/test_device_licence.py is the document's)::

    magic "ALIC" (4) | format 1 (1) | installId (16) | issuedAt int64 unix seconds (8) | productCount 1..16 (1)
    then productCount of: productLength 1..64 (1) | product ASCII (n) | keyHash (32)
    then signatureLength (2) = the key's modulus length in bytes | signature RSASSA-PKCS1-v1_5 / SHA-256 over every byte
    before signatureLength

    A product is [a-z0-9][a-z0-9.-]*; products are strictly ascending by byte value, no repeats; no bytes follow the
    signature. issuedAt is informational (licences do not expire). keyHash is never looked at: it is the buyer's
    download key's hash, of no use here.

WHAT HAPPENS TO THE ANSWER
    A valid licence is the caller tier ``device``, and its chats are counted per installId (hex) in a rolling window
    (NonContribBlockerMiddleware._device_window). A licence that is malformed, wrongly signed, for another product, or
    of a revoked install is a 401 with a short reason, never a trial caller: a broken install should be seen.

REVOCATION
    ``NOCLIP_REVOKED_INSTALLS`` names a text file of install ids (32 hex, one a line; ``#`` starts a comment), re-read when
    its modification time or size changes, so ``dlne_server.py device revoke <installId>`` takes effect without a restart.

Results are cached by the SHA-256 of the licence bytes (bounded, least recently used out first); revocation is looked up
after the cache, so it is live. The licence itself is never logged; an install id may be.
"""

import base64
import hashlib
import os
import re
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from engine.logger import log_warning

LICENCE_PREFIX = "alic1."
MAGIC = b"ALIC"
FORMAT = 1
INSTALL_ID_BYTES = 16
KEY_HASH_BYTES = 32
MAX_PRODUCTS = 16
MAX_PRODUCT_LENGTH = 64
MAX_SIGNATURE_BYTES = 512                                  # a 4096-bit key
MAX_BYTES = 4 + 1 + 16 + 8 + 1 + 16 * (1 + 64 + 32) + 2 + 512
MAX_TOKEN_CHARS = (MAX_BYTES * 4 + 2) // 3                 # the most base64url characters MAX_BYTES can take
CACHE_SIZE = 2048
MIN_KEY_BITS, MAX_KEY_BITS = 2048, 4096
DEFAULT_PRODUCT = "noclipos"
DEFAULT_REVOKED_FILE = os.path.join("config", "noclip_revoked_installs.txt")

PRODUCT_RE = re.compile(r"[a-z0-9][a-z0-9.\-]{0,63}")
BASE64URL_RE = re.compile(r"[A-Za-z0-9_\-]+")
INSTALL_HEX_RE = re.compile(r"[0-9a-f]{32}")

# reason codes, and what the caller is told for each (short, and nothing that helps forge a licence)
VALID = "valid"
MALFORMED = "malformed"
BAD_SIGNATURE = "bad_signature"
WRONG_PRODUCT = "wrong_product"
REVOKED = "revoked"
NOT_ACCEPTED = "not_accepted"
DETAILS = {
    MALFORMED: "The NoClipOS licence is malformed.",
    BAD_SIGNATURE: "The NoClipOS licence's signature is not valid.",
    WRONG_PRODUCT: "The NoClipOS licence does not cover NoClipOS.",
    REVOKED: "This NoClipOS install's licence has been revoked.",
    NOT_ACCEPTED: "This coordinator does not accept NoClipOS licences.",
}


@dataclass(frozen=True)
class Verdict:
    reason: str                       # VALID or one of the codes above
    install_id: str = ""              # 32 lowercase hex, set whenever the licence parsed

    @property
    def valid(self) -> bool:
        return self.reason == VALID

    @property
    def detail(self) -> str:
        return DETAILS.get(self.reason, "")


@dataclass(frozen=True)
class ParsedLicence:
    install_id: str
    issued_at: int
    products: Tuple[str, ...]
    signed: bytes
    signature: bytes


def parse_licence(data: bytes) -> ParsedLicence:
    """The fields of a format 1 licence. Raises ValueError on anything malformed (including bytes left over). The
    signature is NOT checked here, so nothing read may be trusted until :func:`verify_licence` says so."""
    if not isinstance(data, (bytes, bytearray)) or len(data) > MAX_BYTES:
        raise ValueError("size")
    data = bytes(data)
    at = 0

    def take(count: int) -> bytes:
        nonlocal at
        if count < 0 or at + count > len(data):
            raise ValueError("cut short")
        part = data[at:at + count]
        at += count
        return part

    if take(4) != MAGIC:
        raise ValueError("magic")
    if take(1)[0] != FORMAT:
        raise ValueError("format")
    install = take(INSTALL_ID_BYTES)
    issued_at = int.from_bytes(take(8), "big", signed=True)
    count = take(1)[0]
    if not 1 <= count <= MAX_PRODUCTS:
        raise ValueError("product count")
    products: List[str] = []
    for _ in range(count):
        length = take(1)[0]
        if not 1 <= length <= MAX_PRODUCT_LENGTH:
            raise ValueError("product length")
        raw = take(length)
        if any(b >= 0x80 for b in raw):
            raise ValueError("product not ASCII")
        product = raw.decode("ascii")
        if not PRODUCT_RE.fullmatch(product):
            raise ValueError("product id")
        if products and products[-1].encode("ascii") >= raw:
            raise ValueError("products out of order")
        products.append(product)
        take(KEY_HASH_BYTES)
    signed_length = at
    signature_length = int.from_bytes(take(2), "big")
    if signature_length == 0:
        raise ValueError("no signature")
    signature = take(signature_length)
    if at != len(data):
        raise ValueError("bytes left over")
    return ParsedLicence(install.hex(), issued_at, tuple(products), data[:signed_length], signature)


def verify_licence(data: bytes, public_key: rsa.RSAPublicKey, product: str) -> Verdict:
    """Parse, check the signature, then the product, in LicenceKit's order. Revocation is not here (see DeviceLicences)."""
    try:
        parsed = parse_licence(data)
    except ValueError:
        return Verdict(MALFORMED)
    if len(parsed.signature) != (public_key.key_size + 7) // 8:
        return Verdict(MALFORMED, parsed.install_id)
    try:
        public_key.verify(parsed.signature, parsed.signed, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        return Verdict(BAD_SIGNATURE, parsed.install_id)
    if product not in parsed.products:
        return Verdict(WRONG_PRODUCT, parsed.install_id)
    return Verdict(VALID, parsed.install_id)


def decode_token(token: str) -> Optional[bytes]:
    """The licence bytes inside ``alic1.<base64url, no padding>``, or None when it is not that, or too long to be one."""
    if not isinstance(token, str) or not token.startswith(LICENCE_PREFIX):
        return None
    body = token[len(LICENCE_PREFIX):]
    if not body or len(body) > MAX_TOKEN_CHARS or len(body) % 4 == 1 or not BASE64URL_RE.fullmatch(body):
        return None
    try:
        return base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except ValueError:
        return None


def load_public_key(path: str) -> rsa.RSAPublicKey:
    """The RSA public key in a PEM ``PUBLIC KEY`` file (X.509 SubjectPublicKeyInfo), 2048 to 4096 bits. Raises ValueError."""
    try:
        with open(path, "rb") as handle:
            pem = handle.read(16384)
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {type(exc).__name__}") from None
    if not pem.lstrip().startswith(b"-----BEGIN PUBLIC KEY-----"):
        raise ValueError("not a PEM PUBLIC KEY (SubjectPublicKeyInfo)")
    try:
        key = serialization.load_pem_public_key(pem)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"unreadable key ({type(exc).__name__})") from None
    if not isinstance(key, rsa.RSAPublicKey) or not MIN_KEY_BITS <= key.key_size <= MAX_KEY_BITS:
        raise ValueError(f"not an RSA key of {MIN_KEY_BITS} to {MAX_KEY_BITS} bits")
    return key


def parse_revoked(text: str) -> Set[str]:
    """The install ids in a revoked-installs file: 32 hex per line, ``#`` comments and anything else ignored."""
    found: Set[str] = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip().lower()
        if INSTALL_HEX_RE.fullmatch(line):
            found.add(line)
    return found


def revoked_file_path(limits: Dict[str, Any], base_dir: str) -> str:
    """Where the revoked installs are kept: ``NOCLIP_REVOKED_INSTALLS``, else config/noclip_revoked_installs.txt."""
    return str(limits.get("noclip_revoked_installs") or "") or os.path.join(base_dir, DEFAULT_REVOKED_FILE)


def read_revoked(path: str) -> Set[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return parse_revoked(handle.read(4 * 1024 * 1024))
    except OSError:
        return set()


def write_revoked(path: str, ids: Set[str]) -> None:
    """Atomically rewrite the file with ``ids`` (sorted, one a line)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp.{uuid.uuid4().hex}"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write("# NoClipOS installs whose licences are refused (dlne_server.py device revoke <installId>)\n")
            for install in sorted(ids):
                handle.write(install + "\n")
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


class DeviceLicences:
    """Checks ``alic1.`` credentials against the configured key, product and revoked file, with a bounded cache."""

    def __init__(self, now=time.time) -> None:
        self._lock = threading.Lock()
        self._now = now
        self._cache: "OrderedDict[bytes, Verdict]" = OrderedDict()
        self._context: Optional[Tuple[Any, ...]] = None      # (key path, its stamp, product) the cache was made under
        self._key: Optional[rsa.RSAPublicKey] = None
        self._revoked_stamp: Optional[Tuple[str, int, int]] = None
        self._revoked: Set[str] = set()
        self._last_warned: Dict[str, float] = {}

    def _warn(self, what: str, text: str) -> None:
        """One log line per ``what`` every ten minutes, so a refused flood does not become a log flood."""
        now = self._now()
        if now - self._last_warned.get(what, -1e9) >= 600.0:
            self._last_warned[what] = now
            log_warning(text)

    def _key_for(self, path: str) -> Optional[rsa.RSAPublicKey]:
        if not path:
            self._warn("nokey", "NoClipOS licences are refused: NOCLIP_LICENCE_PUBLIC_KEY is not set in config.txt.")
            return None
        try:
            st = os.stat(path)
            stamp: Any = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        context = (path, stamp)
        if self._context is None or self._context[:2] != context:
            self._cache.clear()
            self._key = None
            if stamp is not None:
                try:
                    self._key = load_public_key(path)
                except ValueError as exc:
                    self._warn("badkey", f"NoClipOS licences are refused: NOCLIP_LICENCE_PUBLIC_KEY is unusable ({exc}).")
            self._context = (path, stamp, None)
        if self._key is None:
            self._warn("badkey", "NoClipOS licences are refused: the file NOCLIP_LICENCE_PUBLIC_KEY names is missing or unusable.")
        return self._key

    def _revoked_installs(self, path: str) -> Set[str]:
        try:
            st = os.stat(path)
            stamp: Tuple[str, int, int] = (path, st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = (path, -1, -1)
        if stamp != self._revoked_stamp:
            self._revoked = read_revoked(path) if stamp[1] != -1 else set()
            self._revoked_stamp = stamp
        return self._revoked

    def check(self, token: str, limits: Dict[str, Any], base_dir: Optional[str] = None) -> Verdict:
        """The verdict on a bearer token that starts with ``alic1.``."""
        data = decode_token(token)
        if data is None:
            return Verdict(MALFORMED)
        if base_dir is None:
            from engine.config import get_base_dir
            base_dir = get_base_dir()
        product = str(limits.get("noclip_licence_product") or DEFAULT_PRODUCT)
        digest = hashlib.sha256(data).digest()
        with self._lock:
            key = self._key_for(str(limits.get("noclip_licence_public_key") or ""))
            if key is None:
                return Verdict(NOT_ACCEPTED)
            cache_key = digest + product.encode("utf-8")
            verdict = self._cache.get(cache_key)
            if verdict is None:
                verdict = verify_licence(data, key, product)
                self._cache[cache_key] = verdict
                while len(self._cache) > CACHE_SIZE:
                    self._cache.popitem(last=False)
            else:
                self._cache.move_to_end(cache_key)
            if verdict.valid and verdict.install_id in self._revoked_installs(revoked_file_path(limits, base_dir)):
                self._warn(f"revoked:{verdict.install_id}", f"A revoked NoClipOS install tried to connect: {verdict.install_id}.")
                return Verdict(REVOKED, verdict.install_id)
            return verdict


device_licences = DeviceLicences()
