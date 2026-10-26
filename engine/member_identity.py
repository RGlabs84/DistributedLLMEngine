"""
A coding member's identity (docs/coding-members.md sections 1 and 2): who may be trusted with
private code is decided by a key the member owns, not by a worker id anyone can claim.

THE MEMBER SIDE (engine/coding_member.py)
    * ``load_or_create_identity(path)`` makes an Ed25519 key once, stores it readable by its owner
      only (0600) and reuses it across restarts. The private key is never sent anywhere; a key file
      that exists but cannot be read is an error, never silently replaced (a new key would change
      the trust id the operator has listed).
    * ``sign_block(block, priv)`` completes the heartbeat's ``coding`` block with ``identity`` (the
      public key), ``signed_at`` and ``sig``: an Ed25519 signature over the block as canonical JSON.

THE COORDINATOR SIDE (engine/coding_router.py)
    * ``verify_block(block)`` returns the member's **trust id** when the signature verifies and
      ``signed_at`` is within ``max_skew`` seconds of the coordinator's clock, else None. It never
      raises. Only a block that passes may be given a trust id.
    * ``trust_id(identity_b64)`` / ``trust_id_of_pub(raw)``: the first 32 hex characters of
      sha256(raw 32-byte public key). The operator lists these in TRUSTED_MEMBERS.

Because the end-to-end ``pubkey`` is inside the signed block, a machine that copies a trusted
member's block still cannot open requests sealed to that member.

"READABLE BY ITS OWNER ONLY" ON EACH PLATFORM
    * macOS and Linux: mode 0600.
    * Windows: the mode bits do nothing there (only the read-only flag), so the file's access list
      is set instead: the owner's account gets full control and the folder's inherited entries
      are removed (``icacls``, called by absolute path with no console window). The list is
      applied to the still-empty file, before the key is written into it. An administrator can
      still take ownership of the file, as root can read a 0600 file; no software prevents that.
      When ``icacls`` or ``whoami`` is missing or fails, the key file keeps the permissions of
      its folder (under ``%LOCALAPPDATA%`` that is the owner, SYSTEM and Administrators only;
      under ``C:\\Program Files`` it is readable by every local user) and a warning says so.
"""

import base64
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from typing import Any, Dict, Optional, Tuple

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

DEFAULT_MAX_SKEW_SECONDS = 300.0
KEY_BYTES = 32
SIGNATURE_BYTES = 64


class IdentityError(Exception):
    """The identity key could not be loaded or made. The message is fixed text."""


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(value: Any, length: int) -> bytes:
    if not isinstance(value, str):
        raise ValueError("not base64 text")
    raw = base64.b64decode(value, validate=True)
    if len(raw) != length:
        raise ValueError("wrong length")
    return raw


def _public_raw(private: Ed25519PrivateKey) -> bytes:
    return private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _private_raw(private: Ed25519PrivateKey) -> bytes:
    return private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                 serialization.NoEncryption())


# ---------------------------------------------------------------------------------------------
# Owner-only files
# ---------------------------------------------------------------------------------------------

_SID = re.compile(r"S-1-[0-9]+(?:-[0-9]+)+")
_CREATE_NO_WINDOW = 0x08000000      # subprocess.CREATE_NO_WINDOW, which only exists on Windows
_TOOL_TIMEOUT_SECONDS = 15


def _is_windows() -> bool:
    return sys.platform == "win32"


def _windows_tool(name: str) -> str:
    """A tool in System32 by absolute path, so a lookalike earlier on PATH or in the working
    directory is never run in its place."""
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    return os.path.join(root, "System32", name)


def _own_sid() -> Optional[str]:
    """The security identifier of the Windows account running this process, or None. Taken from
    ``whoami /user`` because the account's name can hold characters a command line mangles and a
    SID is plain ASCII."""
    try:
        done = subprocess.run([_windows_tool("whoami.exe"), "/user", "/fo", "csv", "/nh"], capture_output=True,
                              text=True, errors="replace", timeout=_TOOL_TIMEOUT_SECONDS,
                              creationflags=_CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    found = _SID.findall(done.stdout or "") if done.returncode == 0 else []
    return found[-1] if found else None


def _restrict_to_owner(path: str) -> bool:
    """
    Make ``path`` readable and writable by its owner only; True when that is done. Never raises.
    POSIX: mode 0600. Windows: the owner's account is granted full control first, and only then
    are the inherited entries removed, so the file is never without an entry for its owner.
    """
    if not _is_windows():
        try:
            os.chmod(path, 0o600)
            return True
        except OSError:
            return False
    sid = _own_sid()
    if sid is None:
        return False
    icacls = _windows_tool("icacls.exe")
    for arguments in (["/grant:r", f"*{sid}:(F)"], ["/inheritance:r"]):
        try:
            done = subprocess.run([icacls, path] + arguments, capture_output=True,
                                  timeout=_TOOL_TIMEOUT_SECONDS, creationflags=_CREATE_NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            return False
        if done.returncode != 0:
            return False
    return True


def _warn_not_private() -> None:
    """Windows only: the key file could not be locked down to its owner (fixed text, no paths)."""
    try:
        from engine.logger import log_warning
        log_warning("The coding member's identity key file could not be restricted to your Windows account; "
                    "it keeps the permissions of its folder. Move the DLNE install under your user profile "
                    "(%LOCALAPPDATA%) if other people use this PC.")
    except Exception:  # noqa: BLE001 - a warning must never stop the member
        pass


def trust_id_of_pub(raw_public: bytes) -> str:
    """The trust id of a raw 32-byte Ed25519 public key: 32 hex characters."""
    return hashlib.sha256(raw_public).hexdigest()[:32]


def trust_id(identity_pub_b64: str) -> str:
    """The trust id of a base64 identity public key. Raises ValueError for anything but 32 bytes of base64."""
    return trust_id_of_pub(_b64d(identity_pub_b64, KEY_BYTES))


def load_or_create_identity(path: str) -> Tuple[bytes, str]:
    """
    ``(raw 32-byte private key, base64 raw public key)`` from ``path``, making the file first if it
    does not exist. The file holds the private key as one line of base64 and is created readable
    by its owner only; a file that is more open than that is tightened. Raises IdentityError.
    """
    try:
        with open(path, "r", encoding="ascii") as handle:
            text = handle.read().strip()
    except FileNotFoundError:
        return _create(path)
    except (OSError, UnicodeDecodeError):
        raise IdentityError("the identity key file cannot be read") from None
    try:
        raw = _b64d(text, KEY_BYTES)
        private = Ed25519PrivateKey.from_private_bytes(raw)
    except Exception:  # noqa: BLE001 - never quote the file's content
        raise IdentityError("the identity key file is damaged; delete it to make a new identity "
                            "(the trust id changes)") from None
    if not _restrict_to_owner(path) and _is_windows():
        _warn_not_private()
    return raw, _b64e(_public_raw(private))


def _create(path: str) -> Tuple[bytes, str]:
    private = Ed25519PrivateKey.generate()
    raw = _private_raw(private)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        # O_EXCL: if another process made the file a moment ago, use theirs instead of replacing it.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return load_or_create_identity(path)
    except OSError:
        raise IdentityError("the identity key file cannot be created") from None
    # The file exists and is still empty: it is made private before the key goes into it.
    if not _restrict_to_owner(path) and _is_windows():
        _warn_not_private()
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(_b64e(raw) + "\n")
    except OSError:
        raise IdentityError("the identity key file cannot be written") from None
    return raw, _b64e(_public_raw(private))


def _canonical(block: Dict[str, Any]) -> bytes:
    """The bytes that are signed: the block without ``sig`` as canonical JSON."""
    unsigned = {key: value for key, value in block.items() if key != "sig"}
    return json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sign_block(block: Dict[str, Any], identity_priv: bytes, now: Optional[float] = None) -> Dict[str, Any]:
    """
    A copy of ``block`` (the coding block without identity, signed_at and sig) with those three
    added. ``identity`` and ``signed_at`` are signed too, so neither can be swapped in flight.
    """
    private = Ed25519PrivateKey.from_private_bytes(identity_priv)
    signed = {key: value for key, value in block.items() if key not in ("identity", "signed_at", "sig")}
    signed["identity"] = _b64e(_public_raw(private))
    signed["signed_at"] = int(time.time() if now is None else now)
    signed["sig"] = _b64e(private.sign(_canonical(signed)))
    return signed


def verify_block(block: Any, max_skew: float = DEFAULT_MAX_SKEW_SECONDS, now: Optional[float] = None) -> Optional[str]:
    """
    The member's trust id when ``block``'s signature verifies against its own ``identity`` key and
    ``signed_at`` is within ``max_skew`` seconds of ``now`` (default: this machine's clock); None
    for anything else. Never raises.
    """
    try:
        if not isinstance(block, dict):
            return None
        signed_at = block.get("signed_at")
        if isinstance(signed_at, bool) or not isinstance(signed_at, (int, float)) or not math.isfinite(signed_at):
            return None
        if abs((time.time() if now is None else now) - signed_at) > max_skew:
            return None
        public = _b64d(block.get("identity"), KEY_BYTES)
        signature = _b64d(block.get("sig"), SIGNATURE_BYTES)
        Ed25519PublicKey.from_public_bytes(public).verify(signature, _canonical(block))
        return trust_id_of_pub(public)
    except Exception:  # noqa: BLE001 - anything wrong at all means "not verified"
        return None
