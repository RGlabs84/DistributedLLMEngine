"""
The coordinator's side of coding members (docs/coding-members.md, section 5).

A coding member is a volunteer's machine that runs one whole coding model on its own and
answers coding requests itself, so the swarm serves as many agents at once as it has member
slots instead of two. This module decides which member answers a request and carries the
request there and the answer back. Everything it does is off unless CODING_MEMBERS_ENABLED.

WHO THE MEMBERS ARE. Workers whose signed heartbeat says ``"pool": "coding"`` (the ``coding``
block, validated in engine/authn.py), reached through the reverse tunnel's relay port the way
CPU pool members are (engine/app_pool.py). A member is *routable* when its block's identity
signature verifies (engine/member_identity.verify_block; a block that does not is dropped and
its member never routed to), it says it is ready, its tunnel is up, its join-test speeds meet
the floors (engine/coding_member_select.py) and it has not been blocked (a canary caught it,
engine/canary.py) or failed a request a moment ago.

TRUST. Worker ids prove nothing: anyone with the network key can claim any. A member is trusted
only by its ``trust_id`` (32 hex characters of sha256 of its Ed25519 identity key), set from a
verified block alone and listed by the operator in TRUSTED_MEMBERS; engine/privacy_policy.allowed
is the only place trust is judged. If two workers present one identity (a replayed copy of a
signed block) only the block with the newest ``signed_at`` is believed, and neither if they tie.

WHICH MEMBER. ``pick`` (for GET /v1/coding/route): members the privacy mode allows (engine/privacy_policy.py), then the
member that served this session in the last 30 minutes if it still qualifies and has a free
slot (its model has the session's prompt cached, and a laptop reads prompts at a quarter of
the speed of the coordinator's own GPUs), then the requester's own machine (``own``, its trust
id; 2.5.0) if it is among them and writes at least as fast as the coordinator's engine would
for them right now, else the least busy, ties to the faster reader.
Load is what the member reported, or what this coordinator has in flight there, whichever is
higher, plus the routes issued but not yet used (a burst of route lookups is spread out
instead of all landing on the same member).

SEALED ONLY. A member is only ever sent a sealed body (``{"dlne_e2e": ...}``, section 6) selected
by ``X-DLNE-Route``. A plain body (an old client, a failed sealing, a client that chose not to
seal) is never sent to a member, whatever else it carries: the coordinator's own engine, which is
always trusted, answers it. So what a volunteer's machine is sent has always been encrypted on
the way, without exception. Because a sealed body cannot wait in a queue for a free slot on the
member it is bound to, a full member answers with the re-route code and the client asks again
(see FAILURE); waiting for a slot happens at the local engine's own gate (coordinator._QueuedChat).

ANONYMITY. Only the request body goes to a member: no client address, no API key, no user
identity, no coordinator header (engine/no_trace.forward_headers). A member learns nothing about
who asked. The sealed body is passed through byte for byte; the coordinator cannot read it and
never falls back to the local engine for it, which could not either.

FAILURE. A sealed request is used once and bound to its member's key (contract section 6): it
is never tried on another member. If its member is gone, full, failing or the route is older
than 10 minutes, the answer is HTTP 503 ``{"error": {"code": "dlne_reroute", "reason": ...}}``
and the client asks /v1/coding/route again; these answers are unsealed and carry nothing from
the request, the member or the network. The failing member is left out of routing for half a
minute. A member that dies mid-answer ends the answer with an event the client reads as a cut
stream (no sealed end).

KEY ROTATION. Members rotate their key every 6 hours and keep the old one 15 minutes
(contract section 6), so /v1/coding/route hands out the public key from the member's latest
heartbeat, and a route lives 10 minutes: a route issued just before a rotation still opens. A
route remembers the key it was issued with; if the member's key has changed and the route is
older than the 15 minutes, a sealed request on it is refused with the re-route code.

WHAT A MEMBER SERVES. Routing goes by what a member's signed heartbeat says it is serving (its
model and tier), never by what the coordinator asked of it: a member may step down to a smaller
tier after a failed join test or to fit its spare memory.

State lives on the event loop (sessions, routes, in-flight counts): none of it is thread-safe,
and none of it needs to be.
"""

import asyncio
import hashlib
import itertools
import json
import os
import secrets
import statistics
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Deque, Dict, Iterable, List, Optional, Tuple

import httpx

from engine import canary, coding_member_select, e2e, member_identity, no_trace, privacy_policy
from engine.logger import log_warning
from engine.registry import safe_log_text
from engine.throughput import SealedFrameCounter

E2E_ALG = "x25519-hkdf-sha256-chacha20poly1305"

SESSION_TTL_SECONDS = 30 * 60      # the member that served a session keeps it this long
ROUTE_TTL_SECONDS = 10 * 60        # an opaque route id is good for this long
ROUTE_HOLD_SECONDS = 30.0          # a new route counts as load on its member until used or this old
FAILURE_COOLDOWN_SECONDS = 30.0    # a member that failed a request is left out for this long
MAX_SESSIONS = 20000
MAX_ROUTES = 20000
MAX_FIRST_SEEN = 4096
MAX_VERIFIED = 4096                # signed blocks whose signature check is remembered (see CodingRouter._trust_of)
BAD_BLOCK_REMEMBERED_SECONDS = 5.0
MAX_ATTEMPTS = 3                   # members tried for one request before the local engine takes it
SESSION_ID_MAX_CHARS = 128
IDENTITY_MAX_SKEW_SECONDS = 300    # a coding block signed longer ago (or further ahead) than this is dropped
COMPLETION_RESERVE_SLOTS = 1       # autocomplete leaves this many free slots for agents
ERROR_BODY_LIMIT = 64 * 1024
CANARY_ANSWER_LIMIT = 8 * 1024 * 1024
MEMBER_ANSWER_LIMIT = 8 * 1024 * 1024        # a whole (not streamed) answer a member may send: more is a runaway (SR-10)
# The whole exchange with a member, from sending the request to the last byte of its answer. A long
# agent prompt read at laptop speed plus a long answer is a few minutes; this is the backstop for a
# member that holds a slot and a connection open and dribbles bytes (SR-10).
MEMBER_EXCHANGE_DEADLINE_SECONDS = 30 * 60.0
KEY_OVERLAP_SECONDS = float(e2e.KEEP_PREVIOUS_SECONDS)    # how long a member keeps a retired end-to-end key

# A member reads a long agent prompt at laptop speed before its first word, so the read budget is
# the local engine's (coordinator.ENGINE_READ_TIMEOUT_SECONDS); only the connect fails fast.
CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 900.0

# Role assignment (the heartbeat answer).
MIN_CODING_VRAM_GB = 8.0
ROLE_GRACE_SECONDS = 90.0          # a GPU nobody has measured yet is not called far for this long

PRIVACY_MODES = ("open", "trusted", "any")
STRICT_MODE = "trusted"


# ======================================================================================
# Members
# ======================================================================================

def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value) if value == value and abs(value) != float("inf") else 0.0


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _signed_at(block: Dict[str, Any]) -> int:
    value = block.get("signed_at")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


@dataclass(frozen=True)
class CodingMember:
    """One coding member as its last heartbeat described it."""

    worker_id: str
    model: str
    tier: str
    ready: bool
    slots: int
    busy: int                  # in flight, as the member reported it
    decode_tps: float
    prefill_tps: float
    pubkey: Optional[str]      # base64 X25519, for end-to-end encryption (section 6)
    no_trace: bool
    base_url: Optional[str]    # through the reverse tunnel; None while the tunnel is down
    floors_met: bool
    # The member's trust id (32 lower-case hex characters, from its own identity key), set only
    # when its block's signature verified (engine/member_identity.verify_block). None: nothing
    # about this member can be believed, so it is not routed to. This, never the worker id (which
    # anyone can claim), is what privacy_policy.allowed() reads.
    trust_id: Optional[str] = None
    # When (epoch seconds, time.time()) a routed request last finished on this member; None if none
    # has since this coordinator started. Canaries do not count: they would keep a member "active"
    # forever. engine/canary.py sends them only to members active in the last 2 h.
    last_served_at: Optional[float] = None

    @property
    def key(self) -> str:
        return f"worker:{self.worker_id}"

    @property
    def routable(self) -> bool:
        return (self.trust_id is not None and self.ready and self.base_url is not None and self.floors_met
                and self.slots >= 1)


def normalize_privacy(mode: Any) -> str:
    """A privacy mode as the client declared it; anything missing, unknown or not exactly one of
    ``open``, ``trusted`` and ``any`` is ``trusted`` (engine/privacy_policy.normalize_mode)."""
    return privacy_policy.normalize_mode(mode)


def normalize_trust_ids(ids: Iterable[Any]) -> frozenset:
    """Trust ids as the router compares them: lower-case, whole-string, 32 hex characters (never a substring)."""
    out = set()
    for item in ids or ():
        if isinstance(item, str):
            item = item.strip().lower()
            if len(item) == 32 and all(c in "0123456789abcdef" for c in item):
                out.add(item)
    return frozenset(out)


def _session_key(session_id: Any) -> Optional[str]:
    """A session id as the key it is remembered under (hashed: ids are the client's, not ours to keep)."""
    if not isinstance(session_id, str):
        return None
    session_id = session_id.strip()
    if not session_id or len(session_id) > SESSION_ID_MAX_CHARS:
        return None
    return hashlib.sha256(session_id.encode("utf-8", "replace")).hexdigest()[:32]


def _model_key(name: Any) -> str:
    base = os.path.basename(str(name or "").strip().replace("\\", "/")).replace(":", "-").lower()
    if not base:
        return ""
    return base if base.endswith(".gguf") else base + ".gguf"


# ======================================================================================
# What a request wants, what it gets
# ======================================================================================

@dataclass
class _Want:
    """What one request needs from the members, re-evaluated each time capacity changes."""

    members_fn: Callable[[], List[CodingMember]]
    session_id: Optional[str]          # already hashed (_session_key)
    privacy: str
    trusted: frozenset
    exclude: frozenset = frozenset()
    reserve: int = 0
    model: Optional[str] = None
    need_pubkey: bool = False
    own: Optional[str] = None          # the requester's own member's trust id (2.5.0)
    engine_tps: Optional[float] = None # what the coordinator's engine would write for them now; None: unmeasured


class MemberLease:
    """One slot on one member, held until ``release()`` (idempotent). ``served`` is set once the member
    has really answered a request of a user's; releasing then records the member as active."""

    def __init__(self, router: "CodingRouter", member: CodingMember) -> None:
        self._router = router
        self.member = member
        self.served = False
        self._released = False

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._router._release(self.member, self.served)


@dataclass
class _Waiter:
    priority: int
    seq: int
    want: _Want
    future: "asyncio.Future[MemberLease]"


@dataclass
class _Route:
    key: str
    mode: str
    expires: float
    held: bool
    session: Optional[str]
    pubkey: Optional[str] = None      # the member's end-to-end key when the route was issued
    issued: float = 0.0               # the router's monotonic clock then


@dataclass
class Served:
    """A request answered by a member: send ``body`` (a whole answer) or relay ``stream``."""

    status: int
    media_type: str
    body: Optional[bytes] = None
    stream: Optional[AsyncIterator[bytes]] = None
    retry_after: Optional[int] = None
    _cleanup: Optional[Callable[[], Awaitable[None]]] = None

    async def release(self) -> None:
        """Close the member's connection and give its slot back (idempotent). The stream's own end
        does this too; it is also the response's background task, so a client that goes away before
        the first byte cannot leak either."""
        cleanup, self._cleanup = self._cleanup, None
        if cleanup is not None:
            await cleanup()


REROUTE_CODE = "dlne_reroute"
ROUTE_REQUIRED_CODE = "dlne_route_required"

# Why a sealed request cannot be answered. Fixed words, so an answer that travels unsealed says
# nothing about the request, the member or the client.
_REROUTE_MESSAGES = {
    "expired": "This route has expired. Ask for a new route and send the request again.",
    "gone": "The member for this route is no longer available. Ask for a new route and send the request again.",
    "busy": "The member for this route has no free slot. Ask for a new route and send the request again.",
    "failed": "The member for this route did not answer. Ask for a new route and send the request again.",
    "privacy": "The member for this route is not allowed by this request's privacy mode. Ask for a new route.",
}


def _error(status: int, code: str, message: str, reason: Optional[str] = None,
           retry_after: Optional[int] = None) -> Served:
    error: Dict[str, Any] = {"code": code, "message": message}
    if reason:
        error["reason"] = reason
    return Served(status, "application/json", body=json.dumps({"error": error}).encode(), retry_after=retry_after)


def _reroute(reason: str) -> Served:
    """
    The one answer to a sealed request that cannot be carried out: HTTP 503 with
    ``{"error": {"code": "dlne_reroute", "reason": ...}}`` (reason: expired, gone, busy, failed or
    privacy). A sealed body is bound to one member's key and is used once, so it is never tried on
    another member; the client asks /v1/coding/route again and seals again. Nothing in it comes
    from the request, the member or the network.
    """
    return _error(503, REROUTE_CODE, _REROUTE_MESSAGES[reason], reason, retry_after=3 if reason == "busy" else None)


@dataclass
class _Opened:
    """The first thing a member did with a request."""

    ok: bool
    status: int = 0
    media_type: str = "application/json"
    body: Optional[bytes] = None
    first: bytes = b""
    rest: Optional[AsyncIterator[bytes]] = None
    response: Any = None
    deadline: float = 0.0       # loop time by which the whole exchange (a streamed answer included) must be over


# ======================================================================================
# The router
# ======================================================================================

class CodingRouter:
    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time) -> None:
        self._clock = clock                     # monotonic: TTLs, cool-downs
        self._wall = wall_clock                 # epoch seconds: CodingMember.last_served_at (engine/canary.py reads it)
        self.trusted_ids: set = set()           # TRUSTED_MEMBERS, set by the coordinator before use
        self.blocked: set = set()               # trust ids that stop receiving traffic (besides canary flags)
        self._last_served: Dict[str, float] = {}   # trust id -> when a routed request last finished there
        self._inflight: Dict[str, int] = {}
        self._sessions: "OrderedDict[str, Tuple[str, float]]" = OrderedDict()
        self._routes: "OrderedDict[str, _Route]" = OrderedDict()
        self._holds: Deque[Tuple[float, str]] = deque()
        self._hold_counts: Dict[str, int] = {}
        self._cooldown: Dict[str, float] = {}
        self._waiters: List[_Waiter] = []
        self._seq = itertools.count()
        self._first_seen: "OrderedDict[str, float]" = OrderedDict()
        # canonical JSON of a signed block -> (its trust id or None, wall-clock time until the answer holds)
        self._verified: "OrderedDict[str, Tuple[Optional[str], float]]" = OrderedDict()
        self._contested_logged: set = set()

    # ------------------------------------------------------------------ members
    def _trust_of(self, block: Dict[str, Any]) -> Optional[str]:
        """
        ``member_identity.verify_block``, remembered. A heartbeat's block is signed once and read many times: every
        request, every route lookup, every waiting request's one-second look and every status call builds the member
        list, and an Ed25519 check is about 120 microseconds a member (measured: 100 waiters on 10 members made one
        freed slot block the event loop for 123 ms). The same bytes verify the same way until their ``signed_at``
        leaves the skew window, so a good answer is kept until then; a bad one for a few seconds (it may be a clock
        that is about to be right). The key is the whole block, signature included: a changed field is a new key.
        """
        try:
            key = json.dumps(block, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        except (TypeError, ValueError):
            return None
        now = time.time()
        hit = self._verified.get(key)
        if hit is not None and now <= hit[1]:
            return hit[0]
        trust_id = member_identity.verify_block(block, max_skew=IDENTITY_MAX_SKEW_SECONDS)
        signed_at = block.get("signed_at")
        if trust_id and isinstance(signed_at, (int, float)) and not isinstance(signed_at, bool):
            until = signed_at + IDENTITY_MAX_SKEW_SECONDS
        else:
            until = now + BAD_BLOCK_REMEMBERED_SECONDS
        self._verified[key] = (trust_id, until)
        self._verified.move_to_end(key)
        while len(self._verified) > MAX_VERIFIED:
            self._verified.popitem(last=False)
        return trust_id

    def all_members(self, registry, relay_lookup: Callable[[str, int], Optional[int]],
                    limits: Optional[Dict[str, Any]] = None) -> List[CodingMember]:
        """
        Every live coding member, ready or not (for the counts in /v1/cluster/status).

        A block is believed only if its identity signature verifies and is fresh
        (engine/member_identity.verify_block). One that does not is dropped: its member is listed,
        for the count, with nothing it claimed and no trust id, and is never routed to. When two
        workers present the same identity (a signed block copied and replayed under another worker
        id), only the one with the newest ``signed_at`` is believed, because only the identity's
        owner can sign a newer one; a tie is believed for neither.
        """
        limits = limits or {}
        parsed = []
        for record in registry.coding_members():
            hardware = record.get("hardware") if isinstance(record.get("hardware"), dict) else {}
            block = hardware.get("coding") if isinstance(hardware.get("coding"), dict) else {}
            trust_id = None
            if block:
                try:
                    trust_id = self._trust_of(block)
                except Exception:  # noqa: BLE001 - a bad block is a dropped block, never an error
                    trust_id = None
            if isinstance(trust_id, str):
                trust_id = trust_id.strip().lower()
            parsed.append((record, block, trust_id if trust_id else None))

        stamps: Dict[str, List[int]] = {}            # trust id -> the signed_at of every block presenting it
        for _record, block, trust_id in parsed:
            if trust_id:
                stamps.setdefault(trust_id, []).append(_signed_at(block))
        contested = {t for t, seen in stamps.items() if len(seen) > 1}
        for trust_id in contested - self._contested_logged:
            log_warning(f"Two coding members present the same identity ({trust_id[:8]}...): only the newest "
                        "signed block is believed, and neither if they tie.")
        self._contested_logged = contested

        found: List[CodingMember] = []
        for record, block, trust_id in parsed:
            if trust_id and len(stamps[trust_id]) > 1:
                top = max(stamps[trust_id])
                if _signed_at(block) != top or stamps[trust_id].count(top) != 1:
                    trust_id = None
            if not trust_id:
                block = {}
            try:
                relay = relay_lookup(str(record.get("ip")), int(record.get("port", 0) or 0))
            except (TypeError, ValueError):
                relay = None
            decode, prefill = _number(block.get("decode_tps")), _number(block.get("prefill_tps"))
            slots = block.get("slots")
            pubkey = block.get("pubkey")
            found.append(CodingMember(
                worker_id=str(record.get("worker_id")),
                model=_text(block.get("model")),
                tier=_text(block.get("tier")),
                ready=block.get("ready") is True,
                slots=slots if isinstance(slots, int) and not isinstance(slots, bool) and slots >= 1 else 1,
                busy=int(_number(block.get("busy"))),
                decode_tps=decode,
                prefill_tps=prefill,
                pubkey=pubkey if isinstance(pubkey, str) and pubkey else None,
                no_trace=block.get("no_trace") is True,
                base_url=f"http://127.0.0.1:{relay}" if relay else None,
                floors_met=bool(coding_member_select.meets_floor(decode, prefill, limits)),
                trust_id=trust_id,
                last_served_at=self._last_served.get(trust_id) if trust_id else None,
            ))
        return found

    def members(self, registry, relay_lookup: Callable[[str, int], Optional[int]],
                limits: Optional[Dict[str, Any]] = None) -> List[CodingMember]:
        """The members that can be sent work now: verified, ready, tunnel up, floors met, not blocked."""
        return [m for m in self.all_members(registry, relay_lookup, limits)
                if m.routable and not self.is_blocked(m)]

    def block(self, trust_id: str) -> None:
        """Stop sending the member with this trust id work, on top of the canary book's flags."""
        self.blocked.add(str(trust_id).lower())

    def unblock(self, trust_id: str) -> None:
        self.blocked.discard(str(trust_id).lower())

    def is_blocked(self, member: CodingMember) -> bool:
        """Blocked here, or flagged by a canary (a decoy key or link it was sent was used, engine/canary.py).
        Both are by trust id, the one name a member cannot choose; a member with none is not routable anyway."""
        return member.trust_id is None or member.trust_id in self.blocked or canary.is_flagged(member)

    # ------------------------------------------------------------------ load
    def load(self, member: CodingMember) -> int:
        """Requests the member is busy with: what it said or what is in flight here, plus routes held for it."""
        return max(member.busy, self._inflight.get(member.key, 0)) + self._held(member.key)

    def free_slots(self, member: CodingMember) -> int:
        return member.slots - self.load(member)

    def _held(self, key: str) -> int:
        self._expire_holds()
        return self._hold_counts.get(key, 0)

    def _expire_holds(self) -> None:
        now = self._clock()
        while self._holds and self._holds[0][0] <= now:
            _until, route_id = self._holds.popleft()
            self._drop_hold(route_id)

    def _drop_hold(self, route_id: str) -> None:
        route = self._routes.get(route_id)
        if route is not None and route.held:
            route.held = False
            left = self._hold_counts.get(route.key, 0) - 1
            if left > 0:
                self._hold_counts[route.key] = left
            else:
                self._hold_counts.pop(route.key, None)

    def _reserve(self, member: CodingMember) -> MemberLease:
        self._inflight[member.key] = self._inflight.get(member.key, 0) + 1
        return MemberLease(self, member)

    def _release(self, member: CodingMember, served: bool = False) -> None:
        left = self._inflight.get(member.key, 1) - 1
        if left > 0:
            self._inflight[member.key] = left
        else:
            self._inflight.pop(member.key, None)
        if served and member.trust_id:
            self._last_served.pop(member.trust_id, None)
            self._last_served[member.trust_id] = self._wall()
            while len(self._last_served) > MAX_FIRST_SEEN:
                self._last_served.pop(next(iter(self._last_served)))
        self._wake(member)

    def in_flight(self) -> int:
        return sum(self._inflight.values())

    # ------------------------------------------------------------------ picking
    def _eligible(self, want: _Want, ignore_load: bool = False) -> List[CodingMember]:
        now = self._clock()
        for key in [k for k, until in self._cooldown.items() if until <= now]:
            del self._cooldown[key]
        wanted = _model_key(want.model) if want.model else ""
        out = []
        for m in want.members_fn():
            if (not m.routable or self.is_blocked(m) or m.key in want.exclude
                    or m.worker_id in want.exclude or m.key in self._cooldown):
                continue
            if want.need_pubkey and not m.pubkey:
                continue
            if wanted and _model_key(m.model) != wanted:
                continue
            if not privacy_policy.allowed(m, want.privacy, set(want.trusted)):
                continue
            if ignore_load or self.free_slots(m) > want.reserve:
                out.append(m)
        return out

    def pick(self, members: List[CodingMember], session_id: Optional[str], privacy: Optional[str],
             exclude: Iterable[str] = (), *, trusted_ids: Optional[Iterable[str]] = None, reserve: int = 0,
             model: Optional[str] = None, need_pubkey: bool = False,
             commit: bool = True, own: Optional[str] = None,
             engine_tps: Optional[float] = None) -> Optional[CodingMember]:
        """
        The member for a request: privacy first, then the session's sticky member if it still
        qualifies and has a free slot, then the requester's own machine (``own``, a trust id; 2.5.0)
        if it qualifies and its ``decode_tps`` is at least ``engine_tps``, what the coordinator's own
        engine would write for them right now (None, no measurement yet, lets it through), else the
        least busy (load as a share of its slots; ties to the faster prompt reader, then the faster
        writer). None when no member qualifies. ``reserve`` free slots are left alone (autocomplete
        leaves one for agents); ``commit`` remembers the choice as the session's sticky member.
        """
        want = _Want(lambda: members, _session_key(session_id), normalize_privacy(privacy),
                     normalize_trust_ids(self.trusted_ids if trusted_ids is None else trusted_ids),
                     frozenset(exclude), reserve, model, need_pubkey, own, engine_tps)
        chosen = self._choose(want, self._eligible(want))
        if chosen is not None and commit:
            self._remember(want.session_id, chosen)
        return chosen

    def _choose(self, want: _Want, candidates: List[CodingMember]) -> Optional[CodingMember]:
        if not candidates:
            return None
        sticky = self._sticky(want.session_id)
        if sticky is not None:
            for member in candidates:
                if member.key == sticky:
                    return member
        if want.own:
            for member in candidates:
                if member.trust_id == want.own and (want.engine_tps is None or member.decode_tps >= want.engine_tps):
                    return member
        return min(candidates, key=lambda m: (self.load(m) / m.slots, -m.prefill_tps, -m.decode_tps, m.key))

    # ------------------------------------------------------------------ sessions
    def _sticky(self, session: Optional[str]) -> Optional[str]:
        if session is None:
            return None
        entry = self._sessions.get(session)
        if entry is None:
            return None
        if entry[1] <= self._clock():
            del self._sessions[session]
            return None
        return entry[0]

    def _remember(self, session: Optional[str], member: CodingMember) -> None:
        if session is None:
            return
        now = self._clock()
        self._sessions[session] = (member.key, now + SESSION_TTL_SECONDS)
        self._sessions.move_to_end(session)
        while self._sessions and (len(self._sessions) > MAX_SESSIONS
                                  or next(iter(self._sessions.values()))[1] <= now):
            self._sessions.popitem(last=False)

    def _forget(self, session: Optional[str], key: str) -> None:
        entry = self._sessions.get(session) if session else None
        if entry is not None and entry[0] == key:
            del self._sessions[session]

    def mark_failed(self, member: CodingMember) -> None:
        """A request to this member failed before it answered: leave it out for a while."""
        self._cooldown[member.key] = self._clock() + FAILURE_COOLDOWN_SECONDS

    # ------------------------------------------------------------------ routes
    def issue_route(self, member: CodingMember, session_id: Optional[str], privacy: Optional[str],
                    hold: bool = True) -> str:
        """An opaque id for ``member`` (10 minutes). With ``hold`` it counts as load on the member for the
        first 30 seconds, so a burst of lookups is spread out; the coordinator gives no hold to a free-trial
        caller (anyone can ask for routes, and holds would let a stranger keep members' slots from agents)."""
        now = self._clock()
        self._prune_routes(now)
        route_id = secrets.token_urlsafe(18)
        self._routes[route_id] = _Route(member.key, normalize_privacy(privacy), now + ROUTE_TTL_SECONDS,
                                        hold, _session_key(session_id), member.pubkey, now)
        if hold:
            self._hold_counts[member.key] = self._hold_counts.get(member.key, 0) + 1
            self._holds.append((now + ROUTE_HOLD_SECONDS, route_id))
        while len(self._routes) > MAX_ROUTES:
            old_id, _old = next(iter(self._routes.items()))
            self._drop_hold(old_id)
            del self._routes[old_id]
        return route_id

    def resolve_route(self, route_id: Any) -> Optional[_Route]:
        """The route an id stands for, or None when it is unknown or older than 10 minutes."""
        if not isinstance(route_id, str):
            return None
        route = self._routes.get(route_id)
        if route is None:
            return None
        if route.expires <= self._clock():
            self._drop_hold(route_id)
            del self._routes[route_id]
            return None
        return route

    def _prune_routes(self, now: float) -> None:
        while self._routes:
            route_id, route = next(iter(self._routes.items()))
            if route.expires > now:
                break
            self._drop_hold(route_id)
            del self._routes[route_id]

    # ------------------------------------------------------------------ placing (with the queue)
    def _try(self, want: _Want) -> Optional[MemberLease]:
        member = self._choose(want, self._eligible(want))
        if member is None:
            return None
        self._remember(want.session_id, member)
        return self._reserve(member)

    def _try_specific(self, member: CodingMember, reserve: int, route_id: Optional[str] = None) -> Optional[MemberLease]:
        """A slot on exactly this member (a route's), counting the route's own hold as already taken."""
        if route_id is not None:
            self._drop_hold(route_id)
        if member.key in self._cooldown and self._cooldown[member.key] > self._clock():
            return None
        if self.free_slots(member) <= reserve:
            return None
        return self._reserve(member)

    def _wake(self, freed: Optional[CodingMember] = None) -> None:
        """A slot was freed: hand it to the best-placed waiter that can use it. Within a tier, a waiter whose session
        this member served last goes first: its prompt is still cached here, and reading it again on another member
        costs minutes at a laptop's prompt speed (measured: 21% sticky hits with all slots busy, because a waiter
        took whichever member freed up first)."""
        def order(w: "_Waiter"):
            here = freed is not None and w.want.session_id is not None and self._sticky(w.want.session_id) == freed.key
            return (w.priority, 0 if here else 1, w.seq)

        waiters = sorted((w for w in self._waiters if not w.future.done()), key=order)
        if not waiters:
            return
        # One look at the members, not one per waiter: with nothing free no waiter can be served, and finding that
        # out used to cost every waiter its own member-list build (100 waiters on 10 members: 123 ms of event loop).
        members = waiters[0].want.members_fn()
        for waiter in waiters:
            if waiter.future.done():
                continue
            if not any(self.free_slots(m) > 0 for m in members):
                return
            lease = self._try(waiter.want)
            if lease is not None:
                waiter.future.set_result(lease)
                self._waiters.remove(waiter)

    async def place(self, want: _Want, priority: int, wait_seconds: float,
                    ticket: Optional[dict] = None) -> Optional[MemberLease]:
        """
        A slot on a member for ``want``. With ``wait_seconds`` the request waits, in tier order,
        for one to free up when members that could serve it exist but are all full; None when
        none qualifies or the wait ran out. A ``ticket`` (an empty dict the caller keeps) is given
        the request's place in the queue while it waits, for :meth:`position`.
        """
        lease = self._try(want)
        if lease is not None or wait_seconds <= 0 or not self._eligible(want, ignore_load=True):
            return lease
        loop = asyncio.get_running_loop()
        waiter = _Waiter(int(priority), next(self._seq), want, loop.create_future())
        if ticket is not None:
            ticket["waiter"] = waiter
        self._waiters.append(waiter)
        deadline = loop.time() + wait_seconds
        handed = False
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return None
                try:
                    lease = await asyncio.wait_for(asyncio.shield(waiter.future), min(remaining, 1.0))
                    handed = True
                    return lease
                except asyncio.TimeoutError:
                    if not self._eligible(want, ignore_load=True):
                        return None          # the members it could use have gone
                    # A member's own report of being busy can lag behind our release: look again,
                    # but only the best-placed waiter may, so the queue order holds.
                    if self._is_head(waiter):
                        lease = self._try(want)
                        if lease is not None:
                            handed = True
                            return lease
        finally:
            if waiter in self._waiters:
                self._waiters.remove(waiter)
            if not handed:
                if waiter.future.done() and not waiter.future.cancelled() and waiter.future.exception() is None:
                    waiter.future.result().release()
                else:
                    waiter.future.cancel()

    @staticmethod
    def _effective_mode(route: _Route, privacy: Optional[str]) -> str:
        """The mode a route's member is judged by: a request can tighten the mode its route was
        issued under (header present and strict), never relax it."""
        request_mode = normalize_privacy(privacy) if privacy else None
        return STRICT_MODE if STRICT_MODE in (route.mode, request_mode) else route.mode

    def _is_head(self, waiter: _Waiter) -> bool:
        live = [w for w in self._waiters if not w.future.done()]
        return bool(live) and min(live, key=lambda w: (w.priority, w.seq)) is waiter

    def queue_stats(self) -> Dict[str, int]:
        return {"waiting": sum(1 for w in self._waiters if not w.future.done()), "in_flight": self.in_flight()}

    def position(self, ticket: Optional[dict]) -> Optional[int]:
        """The place of the request waiting under ``ticket`` in the member queue (1 = next, in the order
        slots are handed out), or None when it is not waiting there."""
        waiter = (ticket or {}).get("waiter")
        if waiter is None or waiter.future.done():
            return None
        return 1 + sum(1 for w in self._waiters
                       if not w.future.done() and (w.priority, w.seq) < (waiter.priority, waiter.seq))

    def capacity(self, members: List[CodingMember]) -> Dict[str, int]:
        """``{"slots", "in_use"}`` over the given members, counts only."""
        return {"slots": sum(m.slots for m in members),
                "in_use": sum(min(m.slots, max(m.busy, self._inflight.get(m.key, 0))) for m in members)}

    # ------------------------------------------------------------------ the request
    async def serve(self, *, client, members_fn: Callable[[], List[CodingMember]], path: str,
                    raw_body: bytes, parsed: Any, privacy: Optional[str], route_id: Optional[str],
                    reserve: int = 0, trusted_ids: Optional[Iterable[str]] = None) -> Optional[Served]:
        """
        Answer one SEALED request from the member its route names; None leaves the request to the
        local engine.

        Only a sealed body (``{"dlne_e2e": ...}``) ever goes to a member, so that what a volunteer's
        machine is sent has always been encrypted on the way. A plain body (an old client, a failed
        sealing, a client that chose not to seal) returns None here whatever else it carries: the
        coordinator's own engine, which is always trusted, answers it. A sealed body needs
        ``route_id``, is forwarded as it came, and is used once, bound to that member: whatever goes
        wrong, the answer is the re-route code (503, see :func:`_reroute`), never another member.
        ``trusted_ids`` are the operator's trust ids (TRUSTED_MEMBERS); None uses ``self.trusted_ids``.
        """
        if not (isinstance(parsed, dict) and isinstance(parsed.get("dlne_e2e"), dict)):
            return None
        if not route_id:
            return _error(400, ROUTE_REQUIRED_CODE,
                          "An end-to-end request needs the X-DLNE-Route header from /v1/coding/route.")
        trusted = normalize_trust_ids(self.trusted_ids if trusted_ids is None else trusted_ids)
        route = self.resolve_route(route_id)
        if route is None:
            return _reroute("expired")
        member = next((m for m in members_fn() if m.key == route.key and m.routable
                       and not self.is_blocked(m)), None)
        if member is None:
            return _reroute("gone")
        if not privacy_policy.allowed(member, self._effective_mode(route, privacy), set(trusted)):
            return _reroute("privacy")
        if member.pubkey != route.pubkey and self._clock() - route.issued >= KEY_OVERLAP_SECONDS:
            # The member rotated its key, and the one this route was sealed to is no longer
            # kept (members keep a retired key 15 minutes): the body could not be opened.
            return _reroute("expired")
        lease = self._try_specific(member, reserve, route_id)
        if lease is None:
            return _reroute("busy")
        return await self._answer(client, lease, path, raw_body, route.session)

    async def _answer(self, client, lease: MemberLease, path: str, raw_body: bytes, session: Optional[str],
                      *, count_activity: bool = True) -> Served:
        """Send a sealed request to the leased member and turn what it does into a Served (a re-route
        answer when it does not answer). ``count_activity`` is False for canaries, which must not make
        a member look busy serving users."""
        member = lease.member
        try:
            opened = await self._open(client, member, path, raw_body)
        except BaseException:           # the client went away while the member was thinking
            lease.release()
            raise
        if not opened.ok:
            lease.release()
            self.mark_failed(member)
            self._forget(session, member.key)
            # What the member said is not passed on (it could carry request content or the member's
            # own details); the client is only told to route again.
            return _reroute("busy" if opened.status in (429, 503) else "failed")
        self._cooldown.pop(member.key, None)
        lease.served = count_activity
        if opened.rest is None:                          # a whole answer
            lease.release()
            return Served(200, opened.media_type, body=opened.body)

        async def finish() -> None:
            try:
                await opened.response.aclose()
            except Exception:  # noqa: BLE001 - already closed or gone
                pass
            lease.release()

        return Served(200, "text/event-stream", stream=self._relay(opened, lease, finish), _cleanup=finish)

    # ------------------------------------------------------------------ talking to a member
    async def _open(self, client, member: CodingMember, path: str, payload: bytes) -> _Opened:
        """POST ``payload`` to the member and read as far as its first bytes (or its whole answer), all
        within the exchange's overall deadline."""
        deadline = asyncio.get_running_loop().time() + MEMBER_EXCHANGE_DEADLINE_SECONDS
        try:
            opened = await asyncio.wait_for(self._open_once(client, member, path, payload),
                                            MEMBER_EXCHANGE_DEADLINE_SECONDS)
        except asyncio.TimeoutError:
            log_warning(f"Coding member {safe_log_text(member.key, max_chars=80)} did not answer in time.")
            return _Opened(False, 504)
        opened.deadline = deadline
        return opened

    async def _open_once(self, client, member: CodingMember, path: str, payload: bytes) -> _Opened:
        headers = no_trace.forward_headers({"Content-Type": "application/json"})
        timeout = httpx.Timeout(READ_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS)
        response = None
        try:
            request = client.build_request("POST", f"{member.base_url}{path}", content=payload, headers=headers,
                                           timeout=timeout)
            response = await client.send(request, stream=True)
            media_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
            name = safe_log_text(member.key, max_chars=80)
            if response.status_code != 200:
                data = bytearray()
                async for piece in response.aiter_bytes():
                    data += piece
                    if len(data) >= ERROR_BODY_LIMIT:
                        break
                await response.aclose()
                log_warning(f"Coding member {name} answered HTTP {response.status_code}.")
                return _Opened(False, response.status_code, "application/json", body=bytes(data[:ERROR_BODY_LIMIT]))
            if media_type == "text/event-stream":
                rest = response.aiter_bytes()
                try:
                    first = await rest.__anext__()
                except StopAsyncIteration:
                    first = b""
                if not first:
                    await response.aclose()
                    log_warning(f"Coding member {name} closed without answering.")
                    return _Opened(False, 502)
                return _Opened(True, 200, "text/event-stream", first=first, rest=rest, response=response)
            data = bytearray()
            async for piece in response.aiter_bytes():
                data += piece
                if len(data) > MEMBER_ANSWER_LIMIT:
                    await response.aclose()
                    log_warning(f"Coding member {name} sent an answer over {MEMBER_ANSWER_LIMIT // (1024 * 1024)} MiB.")
                    return _Opened(False, 502)
            await response.aclose()
            return _Opened(True, 200, "application/json", body=bytes(data))
        except Exception as exc:  # noqa: BLE001 - any failure before an answer is handled the same way
            log_warning(f"Coding member {safe_log_text(member.key, max_chars=80)} failed before answering "
                        f"({type(exc).__name__}).")
            await _close_quietly(response)
            return _Opened(False, 0)
        except BaseException:           # cancelled: do not leave the member's connection open
            await _close_quietly(response)
            raise

    async def _relay(self, opened: _Opened, lease: MemberLease,
                     finish: Callable[[], Awaitable[None]]) -> AsyncIterator[bytes]:
        member = lease.member
        # Sealed frames are counted as tokens for the website's live figure (engine/throughput.py), but
        # not a canary's (it is not a user's work, and lease.served is False for it).
        counter = SealedFrameCounter() if lease.served else None
        try:
            async for piece in _joined(opened.first, opened.rest, opened.deadline):
                if counter is not None:
                    counter.feed(piece)
                yield piece
        except Exception as exc:  # noqa: BLE001 - the member died or the tunnel dropped mid-answer
            log_warning(f"Coding member {safe_log_text(member.key, max_chars=80)} dropped mid-answer "
                        f"({type(exc).__name__}).")
            self.mark_failed(member)
            lease.served = False
            yield _dropped_notice()
        finally:
            await finish()

    async def forward_to(self, client, members_fn: Callable[[], List[CodingMember]], member: Any, body: bytes,
                         headers: Optional[Dict[str, str]] = None) -> Any:
        """
        Hand a sealed request to a member along the path routed traffic takes (the lease, the headers
        engine/no_trace allows, the same relay); this is what the canary scheduler
        (engine/canary.CanaryScheduler) is given as its ``forward``. ``member`` is the CodingMember the
        scheduler was handed (or a trust id): it is found again in the current list by its trust id and,
        when given the object, its worker key too, so a second worker claiming the same name cannot
        receive it. Returns the member's answer, an async iterator of its bytes (or the bytes of a
        whole answer), and raises when the member is not routable now, has no free slot, or does not
        answer with a 200. ``headers`` is accepted for the scheduler's signature: a member only ever
        receives what no_trace.forward_headers allows, and that is what is sent. It does not count as
        the member serving anyone (``last_served_at``).
        """
        if isinstance(member, CodingMember):
            wanted_trust, wanted_key = member.trust_id, member.key
        else:
            wanted_trust, wanted_key = str(member).lower(), None
        member = next((m for m in members_fn() if m.trust_id == wanted_trust and m.trust_id is not None
                       and (wanted_key is None or m.key == wanted_key) and m.routable
                       and not self.is_blocked(m)), None)
        if member is None:
            raise LookupError("not a routable coding member")
        lease = self._try_specific(member, 0)
        if lease is None:
            raise RuntimeError("the member has no free slot")
        served = await self._answer(client, lease, "/v1/chat/completions", body, None, count_activity=False)
        if served.status != 200:
            raise RuntimeError(f"the member did not answer (HTTP {served.status})")
        if served.stream is None:
            return served.body
        # Read the whole answer here, so the slot and the connection are given back whatever the
        # caller does with it (a canary's answer is small; the cap is a runaway member's).
        answer = bytearray()
        try:
            async for piece in served.stream:
                answer += piece
                if len(answer) > CANARY_ANSWER_LIMIT:
                    raise RuntimeError("the member's answer is too long")
        finally:
            await served.release()
        return bytes(answer)

    # ------------------------------------------------------------------ status
    def status(self, registry, relay_lookup, limits: Optional[Dict[str, Any]], enabled: bool) -> Dict[str, Any]:
        """Counts and pool-wide speeds only, for /v1/cluster/status: nothing here names a member."""
        everyone = self.all_members(registry, relay_lookup, limits)
        ready = [m for m in everyone if m.routable and not self.is_blocked(m)]
        decode, prefill = [m.decode_tps for m in ready], [m.prefill_tps for m in ready]
        return {
            "enabled": bool(enabled),
            "members": len(everyone),
            "ready": len(ready),
            "slots": sum(m.slots for m in ready),
            "busy": sum(min(m.slots, max(m.busy, self._inflight.get(m.key, 0))) for m in ready),
            # The ready members' join-test speeds as a pool (2.5.0), one decimal; null while none is ready.
            "decode_tps_median": round(statistics.median(decode), 1) if ready else None,
            "decode_tps_min": round(min(decode), 1) if ready else None,
            "prefill_tps_median": round(statistics.median(prefill), 1) if ready else None,
            "prefill_tps_min": round(min(prefill), 1) if ready else None,
        }

    # ------------------------------------------------------------------ roles
    def first_seen(self, worker_id: str) -> float:
        """When this coordinator first heard from ``worker_id`` (epoch seconds; bounded memory)."""
        now = time.time()
        if worker_id not in self._first_seen:
            self._first_seen[worker_id] = now
            while len(self._first_seen) > MAX_FIRST_SEEN:
                self._first_seen.popitem(last=False)
        return self._first_seen[worker_id]


# ======================================================================================
# The request a member is sent, and what comes back
# ======================================================================================

async def _close_quietly(response: Any) -> None:
    if response is not None:
        try:
            await response.aclose()
        except Exception:  # noqa: BLE001 - already closed or gone
            pass


async def _joined(first: bytes, rest: AsyncIterator[bytes], deadline: float) -> AsyncIterator[bytes]:
    """The member's answer as it comes, ending (TimeoutError) at ``deadline`` (loop time) even if the member
    is still sending, or has stopped."""
    yield first
    loop = asyncio.get_running_loop()
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise asyncio.TimeoutError("the exchange outlived its deadline")
        try:
            piece = await asyncio.wait_for(rest.__anext__(), remaining)
        except StopAsyncIteration:
            return
        yield piece


def _dropped_notice() -> bytes:
    """What ends a sealed answer cut short by its member: an event the client can read (a sealed stream
    cannot carry text it would show) and the plain [DONE]. With no sealed end before it, the client
    treats the stream as cut."""
    return b'data: {"dlne": {"event": "member_dropped"}}\n\ndata: [DONE]\n\n'


# ======================================================================================
# Roles (the heartbeat answer)
# ======================================================================================

def assign_role(hardware: Any, near: Optional[bool], tiers: List[Dict[str, Any]], rtt_ms: Optional[float] = None,
                max_member_rtt_ms: Optional[float] = None
                ) -> Tuple[Optional[str], Optional[Dict[str, Any]], Optional[int]]:
    """
    What a worker should be, as ``(role, coding_tier, coding_slots)``:

    * no GPU (or already a CPU pool member): ``"app"``, today's behaviour;
    * a GPU close to the coordinator (``near``) that is docked, or does not say (2.5.0, ``docked``):
      ``"rpc"``, part of the sharded engine; one that is off its dock is judged like a far GPU. The
      coordinator passes ``docked`` as it judges it (engine/dock_policy.py, 2.6.4: on battery never,
      on Wi-Fi while its link passes the test), not as the worker reported it;
    * a GPU that is yielding to its owner right now: ``None``, no new job (it keeps the one it has);
    * a far GPU with at least 8 GB: ``"coding"``, with the biggest tier it can hold whole
      (engine/coding_member_select.choose_tier) and how many requests at once to open it for
      (choose_slots: up to the tier's server_slots, as many as it is estimated to run fast
      enough; 2.5.0), unless its measured round trip ``rtt_ms`` is over ``max_member_rtt_ms``
      (CODING_MEMBER_MAX_RTT_MS). One nobody has measured may still;
    * anything else, a far GPU too small, too short of memory for a tier, or too far away, or one
      nobody has measured yet (``near`` None): ``None``, no assignment, so it does what it did before.

    ``coding_slots`` is None whenever there is no tier. The tier is sized on ``coding_ram_gb``, the
    RAM the worker's coding job may use (2.4.9), and on ``ram_gb`` only for a worker that does not
    say: ``ram_gb`` is what its RPC node lends, capped by MAX_RAM_GB (4 GB by default), which fits
    no coding model.
    """
    hardware = hardware if isinstance(hardware, dict) else {}
    gpu = hardware.get("gpu") if isinstance(hardware.get("gpu"), dict) else {}
    vram = _number(gpu.get("vram_total_gb"))
    if hardware.get("pool") == "app" or gpu.get("has_gpu") is False or vram <= 0:
        return "app", None, None
    if near is None:
        return None, None, None
    if hardware.get("is_yielding") is True:
        return None, None, None      # stepping aside for its owner (2.5.0): no new job now; what it has, it keeps
    if near and hardware.get("docked") is not False:
        return "rpc", None, None     # a LAN GPU that is docked (or cannot say) shares the big model
    # Near but off its dock (battery, or Wi-Fi without a passing test, 2.6.4): a shard that may leave any minute would take every
    # user's answer with it, so it is treated like a far GPU below: a whole model of its own, or nothing.
    if vram < MIN_CODING_VRAM_GB or hardware.get("coding_ok", True) is not True:
        return None, None, None      # too small, or the worker says it cannot run a coding member (coding_ok)
    if rtt_ms is not None and max_member_rtt_ms is not None and _number(rtt_ms) > max_member_rtt_ms:
        return None, None, None      # too far even for a whole model of its own
    ram = _number(hardware.get("coding_ram_gb", hardware.get("ram_gb")))
    tier = coding_member_select.choose_tier(vram, ram, tiers)
    if not tier:
        return None, None, None
    return "coding", tier, coding_member_select.choose_slots(tier, vram, ram)


coding_router = CodingRouter()
