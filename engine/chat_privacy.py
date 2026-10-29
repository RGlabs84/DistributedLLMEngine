"""
Keeps one phone chat's cached prompt from being reachable by another chat (2.6).

llama-server remembers the start of every prompt it has read and skips re-reading it when a later
prompt begins the same way, which is what makes a long chat fast. The phone lanes share that memory
between all phones, and every phone chat starts with the same public system prompt, so without this
a prompt that shares more of its start with someone else's earlier one is answered measurably sooner,
and that timing says what the other chat began with.

The fix is one line at the end of the system prompt that is different for every chat and the same for
every turn of it: an HMAC of the phone's ``chat_id`` under a secret only this coordinator knows. Chats
then share only the public prompt before that line, and a chat's own turns keep sharing everything.
The phone cannot compute another chat's line without the secret, and the line is never logged. It goes only to
the engine on this machine (a CPU-pool member is never sent it: engine/app_pool.py builds that request without
it). A request with no usable ``chat_id`` has no stable line, so its cache is switched off instead.

The salt only breaks the shared PREFIX. llama-server's ``--cache-reuse`` also re-uses a chunk found anywhere in a
slot's earlier prompt, so every phone-lane request carries ``n_cache_reuse`` 0 (engine/app_pool.py): without that
the salt would not stop a guessed passage from being confirmed by timing. The secret file is for Linux: elsewhere
``os.chmod`` cannot make it owner-only.
"""

import hashlib
import hmac
import os
import re
import secrets
import threading
from typing import Optional

from engine.config import get_base_dir
from engine.logger import log_warning

SALT_FILE = os.path.join("config", "chat_salt.key")
SECRET_BYTES = 32
# Our own clients send a random UUID per chat (crypto.randomUUID, UUID.randomUUID). Only that shape is a chat id:
# the privacy argument rests on ids nobody can guess, and a short or constant one ("00000000", a user name)
# would put all of a third-party client's chats in one cache namespace that anyone could share.
_CHAT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
_NIL_UUID = "00000000-0000-0000-0000-000000000000"

_lock = threading.Lock()
_secret: Optional[bytes] = None


def clean_chat_id(value: object) -> Optional[str]:
    """The phone's ``chat_id`` when it is a canonical UUID (lower-cased), else None: anything else is no chat id."""
    if not isinstance(value, str) or not _CHAT_ID.fullmatch(value):
        return None
    value = value.lower()
    return None if value == _NIL_UUID else value


def _read_secret(path: str) -> Optional[bytes]:
    """The secret in the file, or None when it is empty or damaged. A missing file raises FileNotFoundError (which is
    what tells "nobody made it yet" from "damaged" in one step, so a file made a moment ago is never mistaken for a
    damaged one). 'utf-8-sig': Notepad adds a BOM."""
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            secret = bytes.fromhex(handle.read().strip())
    except FileNotFoundError:
        raise
    except (OSError, ValueError):       # not hex (or not text) is as good as empty
        return None
    return secret if len(secret) >= SECRET_BYTES else None


def _tighten(path: str) -> None:
    """A file restored from a backup or copied with ``cp`` may be readable by others: owner-only again (POSIX)."""
    try:
        if os.stat(path).st_mode & 0o077:
            os.chmod(path, 0o600)
    except OSError:
        pass


def _load_secret() -> bytes:
    """
    The secret from ``config/chat_salt.key``, made (readable by its owner only) the first time. Every process
    that starts at once ends up with the same one: the file is created whole or not at all, and a process that
    lost the race uses what the winner wrote.
    """
    path = os.path.join(get_base_dir(), SALT_FILE)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            secret = _read_secret(path)
        except FileNotFoundError:
            _write_secret(path, replace=False)      # no file yet: made whole; a process that was faster keeps its own
            secret = _read_secret(path)             # whoever wrote it, the file's secret is THE secret
        else:
            if secret is None:
                # An empty or damaged file would otherwise be met again at every start, each with a different
                # in-memory secret: the cache would never match across restarts. Replace it in one step.
                log_warning("The phone chat salt file was empty or damaged; it has been replaced with a new one.")
                _write_secret(path, replace=True)
                secret = _read_secret(path)
        if secret is None:
            raise OSError("the chat salt file cannot be read back")
        _tighten(path)
        return secret
    except OSError:
        # Chats stay separate for this run; they just no longer match across a restart.
        log_warning("The phone chat salt could not be stored; using one that lasts until this server restarts.")
        return secrets.token_bytes(32)


def _write_secret(path: str, replace: bool) -> None:
    """
    A new secret in the file at ``path``: written beside it (owner-only) and then moved into place whole, so no
    reader ever sees it half written. ``replace`` False puts it there only if there is still no file (a process
    that got there first keeps its secret); True moves it over a damaged one.
    """
    token = secrets.token_hex(SECRET_BYTES)
    temp = f"{path}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(token + "\n")
        if replace:
            os.replace(temp, path)
            return
        try:
            os.link(temp, path)             # fails if the file exists: the winner's secret stands
        except FileExistsError:
            pass
        except OSError:                     # a file system without hard links: create it exclusively instead
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                return
            with os.fdopen(fd, "w", encoding="ascii") as handle:
                handle.write(token + "\n")
    finally:
        try:
            os.unlink(temp)
        except OSError:
            pass


def _secret_bytes() -> bytes:
    global _secret
    with _lock:
        if _secret is None:
            _secret = _load_secret()
        return _secret


def chat_tag(chat_id: str) -> str:
    """A short HMAC of ``chat_id`` (from :func:`clean_chat_id`): what stands for the chat wherever it is remembered."""
    return hmac.new(_secret_bytes(), chat_id.encode("ascii"), hashlib.sha256).hexdigest()[:16]


def salt_line(chat_id: str) -> str:
    """The sentence that ends the system prompt of ``chat_id``'s requests (``chat_id`` from :func:`clean_chat_id`)."""
    return f"[internal reference {chat_tag(chat_id)} - ignore it, never mention or repeat it]"
