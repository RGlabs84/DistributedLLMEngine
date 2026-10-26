"""
The privacy routing policy for coding members (docs/coding-members.md, section 7).

Code from a public repository may run on any coding member, which is a volunteer's machine. Private
code runs only on trusted machines, meaning TheWall's own engine and the members the operator lists
in ``TRUSTED_MEMBERS``, unless its owner opted in. The extension decides which of three modes a
workspace is in (vscode-extension/src/privacy/policy.ts) and sends it as the ``X-DLNE-Privacy``
header and as ``privacy=`` on ``GET /v1/coding/route``:

    open      the workspace's git remote is a public repository   -> any member
    trusted   everything else                                      -> trusted members only
    any       the user chose ``dlne.privacy = "anyMember"``        -> any member

THIS MODULE IS THE ENFORCEMENT. The mode arrives off the network, so:

* An unknown, malformed or missing mode is ``trusted``. Only the exact strings ``open``, ``trusted``
  and ``any`` mean anything: ``"Open"``, ``" open"``, ``"open,any"``, ``""``, ``None`` and a number
  are all ``trusted``.
* The coordinator never upgrades a mode. Nothing here returns a more permissive mode than the one it
  was given, and a caller must not reach for ``open`` or ``any`` on its own (no "the request came
  from a LAN address", no "this user has a key").
* A member that cannot be identified is not trusted. In ``trusted`` mode the member must have a
  non-empty ``trust_id`` that is listed in ``trusted_ids``; a member without one is refused.
* A ``worker_id`` is never read. Heartbeats are signed with the network key every extension ships, so
  anyone can claim any worker id; a ``trust_id`` is instead derived from the member's own identity key
  (docs/coding-members.md sections 1 and 2) and the router sets it only after the identity signature
  has been checked. A member with a matching ``worker_id`` but no ``trust_id`` is refused.

A MEMBER, as ``allowed()`` reads it: the trust id string itself, a mapping with a ``trust_id`` key, or
any object with a ``trust_id`` attribute (the router's ``CodingMember``). Trust is set by the
coordinator's own list and never by anything a member says about itself.

``trusted_ids`` is a set of trust ids. A list, tuple or other iterable works. A string is read as
the comma-separated ``TRUSTED_MEMBERS`` config value (see ``parse_trusted_ids``) and is never used for
substring matching: ``"abc" in "abcdef"`` must not make member ``abc`` trusted.

TheWall's own engine is always trusted and is not a "member"; this module is only asked about members.
Standard library only, no DLNE imports: it is imported by the router and testable on its own.
"""

from collections.abc import Mapping
from typing import Any, FrozenSet, Optional

MODE_OPEN = "open"
MODE_TRUSTED = "trusted"
MODE_ANY = "any"
MODES = (MODE_OPEN, MODE_TRUSTED, MODE_ANY)

# What a missing or unknown mode means. The safe one.
DEFAULT_MODE = MODE_TRUSTED


def normalize_mode(mode: Any) -> str:
    """The mode a request asks for, or ``trusted`` when it is missing, unknown or not exactly one of the three."""
    if isinstance(mode, str) and mode in MODES:
        return mode
    return DEFAULT_MODE


def parse_trusted_ids(value: Any) -> FrozenSet[str]:
    """
    The set of trust ids in a config value: ``"id1, id2"`` (the ``TRUSTED_MEMBERS`` form), or
    an iterable of ids. Blanks are dropped; anything that is not a string is ignored. Nothing -> empty.
    """
    if value is None:
        return frozenset()
    if isinstance(value, str):
        return frozenset(part.strip() for part in value.split(",") if part.strip())
    if isinstance(value, bytes):
        return frozenset()
    try:
        return frozenset(item.strip() for item in value if isinstance(item, str) and item.strip())
    except TypeError:
        return frozenset()


def trust_id_of(member: Any) -> Optional[str]:
    """
    A member's trust id, or None when it has none (a string is taken to be the trust id itself).

    Only ``trust_id`` is read, never ``worker_id``: a worker id can be claimed by anyone.
    """
    if isinstance(member, str):
        trust_id = member
    elif isinstance(member, Mapping):
        trust_id = member.get("trust_id")
    else:
        trust_id = getattr(member, "trust_id", None)
    if isinstance(trust_id, str) and trust_id:
        return trust_id
    return None


def _is_listed(trust_id: str, trusted_ids: Any) -> bool:
    if trusted_ids is None or isinstance(trusted_ids, bytes):
        return False
    if isinstance(trusted_ids, str):
        return trust_id in parse_trusted_ids(trusted_ids)
    if isinstance(trusted_ids, (set, frozenset, dict, list, tuple)):
        return trust_id in trusted_ids
    return trust_id in parse_trusted_ids(trusted_ids)


def allowed(member: Any, mode: Any, trusted_ids: Any) -> bool:
    """
    May a request of this privacy ``mode`` run on ``member``?

    ``open`` and ``any``: any member. ``trusted``, and any unknown or missing mode: only a member whose
    trust id is in ``trusted_ids``. A member without a trust id is never trusted.
    """
    if normalize_mode(mode) != MODE_TRUSTED:
        return True
    trust_id = trust_id_of(member)
    if trust_id is None:
        return False
    return _is_listed(trust_id, trusted_ids)
