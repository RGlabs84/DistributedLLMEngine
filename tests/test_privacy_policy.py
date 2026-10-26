"""
The privacy routing policy (engine/privacy_policy.py, docs/coding-members.md section 7).

The rule under test: ``open`` and ``any`` may use any coding member; ``trusted`` only a member the
operator listed; anything else, including a missing mode, means ``trusted``; and nothing here ever
makes a mode more permissive than the one it was handed.
"""

import itertools
from types import SimpleNamespace

import pytest

from engine import privacy_policy
from engine.privacy_policy import (
    DEFAULT_MODE,
    MODES,
    allowed,
    normalize_mode,
    parse_trusted_ids,
    trust_id_of,
)

# Trust ids are the first 32 hex characters of sha256 of a member's identity public key (section 1).
THEWALL_MEMBER = "1f3a9c5e7b2d4086a1c3e5f708192a3b"
VOLUNTEER = "9e8d7c6b5a49382716f5e4d3c2b1a0f9"
TRUSTED = {THEWALL_MEMBER}


class Member:
    """Shaped like the router's CodingMember: an object with a trust_id (and a claimed worker_id)."""

    def __init__(self, trust_id, worker_id="worker-1"):
        self.trust_id = trust_id
        self.worker_id = worker_id


def _forms(trust_id):
    """The shapes a member can take when it reaches allowed()."""
    return [trust_id, {"trust_id": trust_id}, Member(trust_id), SimpleNamespace(trust_id=trust_id)]


# ---- the matrix ----------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["open", "any"])
@pytest.mark.parametrize("trust_id", [THEWALL_MEMBER, VOLUNTEER])
def test_open_and_any_use_any_member(mode, trust_id):
    for member in _forms(trust_id):
        assert allowed(member, mode, TRUSTED) is True


def test_open_and_any_do_not_need_a_trusted_list_at_all():
    for mode in ("open", "any"):
        assert allowed(Member(VOLUNTEER), mode, set()) is True
        assert allowed(Member(VOLUNTEER), mode, None) is True


def test_trusted_uses_only_listed_members():
    for member in _forms(THEWALL_MEMBER):
        assert allowed(member, "trusted", TRUSTED) is True
    for member in _forms(VOLUNTEER):
        assert allowed(member, "trusted", TRUSTED) is False


def test_trusted_with_nobody_listed_uses_nobody():
    for trusted_ids in (set(), frozenset(), [], (), None, ""):
        assert allowed(Member(THEWALL_MEMBER), "trusted", trusted_ids) is False


def test_the_full_matrix():
    cases = {
        # (mode, member listed?) -> allowed
        ("open", True): True, ("open", False): True,
        ("any", True): True, ("any", False): True,
        ("trusted", True): True, ("trusted", False): False,
    }
    for (mode, listed), expected in cases.items():
        member = Member(THEWALL_MEMBER if listed else VOLUNTEER)
        assert allowed(member, mode, TRUSTED) is expected, (mode, listed)


# ---- an unknown or missing mode is trusted --------------------------------------------------

UNKNOWN_MODES = [
    None, "", " ", "Open", "OPEN", " open", "open ", "any ", "Any", "ANY", "open,any", "open;any",
    "trusted,open", "all", "public", "everyone", "admin", "*", "true", "1", "0", 0, 1, True, False,
    [], ["open"], {}, {"mode": "open"}, b"open", b"any", object(),
]


@pytest.mark.parametrize("mode", UNKNOWN_MODES, ids=repr)
def test_an_unknown_or_missing_mode_means_trusted(mode):
    assert normalize_mode(mode) == "trusted"
    assert allowed(Member(VOLUNTEER), mode, TRUSTED) is False
    assert allowed(Member(THEWALL_MEMBER), mode, TRUSTED) is True


def test_a_missing_header_means_trusted():
    assert DEFAULT_MODE == "trusted"
    assert allowed(Member(VOLUNTEER), None, TRUSTED) is False


def test_only_the_three_exact_modes_are_known():
    assert MODES == ("open", "trusted", "any")
    for mode in MODES:
        assert normalize_mode(mode) == mode


def test_nothing_is_ever_upgraded():
    # normalize_mode returns the mode it was given or "trusted": never a more permissive one.
    rank = {"trusted": 0, "open": 1, "any": 1}
    for mode in UNKNOWN_MODES + list(MODES):
        result = normalize_mode(mode)
        assert result in MODES
        if isinstance(mode, str) and mode in MODES:
            assert result == mode
        else:
            assert rank[result] == 0, mode


# ---- identifying a member -------------------------------------------------------------------

@pytest.mark.parametrize("member", [
    None, "", {}, {"trust_id": ""}, {"trust_id": None}, {"trust_id": 7}, {"id": VOLUNTEER},
    Member(""), Member(None), Member(5), object(), SimpleNamespace(), 42, [], [THEWALL_MEMBER],
], ids=repr)
def test_a_member_that_cannot_be_identified_is_not_trusted(member):
    assert trust_id_of(member) is None
    assert allowed(member, "trusted", TRUSTED | {"", None}) is False


def test_a_member_that_cannot_be_identified_still_passes_open_and_any():
    # The modes that allow every member allow it too: identity only matters for trusted.
    assert allowed(Member(None), "open", TRUSTED) is True
    assert allowed({}, "any", TRUSTED) is True


def test_a_matching_worker_id_without_a_trust_id_is_refused():
    # Worker ids can be claimed by anyone (heartbeats are signed with the shared network key), so they
    # carry no trust: a member whose worker_id is a listed value, but that has no trust_id, is refused.
    listed = {THEWALL_MEMBER}
    for member in (
        {"worker_id": THEWALL_MEMBER},
        {"worker_id": THEWALL_MEMBER, "trust_id": None},
        {"worker_id": THEWALL_MEMBER, "trust_id": ""},
        SimpleNamespace(worker_id=THEWALL_MEMBER),
        Member(None, worker_id=THEWALL_MEMBER),
        Member("", worker_id=THEWALL_MEMBER),
    ):
        assert trust_id_of(member) is None
        assert allowed(member, "trusted", listed) is False, repr(member)
        assert allowed(member, "trusted", ",".join(listed)) is False
        assert allowed(member, "nonsense", listed) is False
        assert allowed(member, None, listed) is False


def test_the_trust_id_decides_not_the_worker_id():
    listed = {THEWALL_MEMBER}
    # A listed trust id is trusted whatever worker id the member claims.
    assert allowed(Member(THEWALL_MEMBER, worker_id="anything"), "trusted", listed) is True
    assert allowed({"trust_id": THEWALL_MEMBER, "worker_id": VOLUNTEER}, "trusted", listed) is True
    # An unlisted trust id is refused even when the claimed worker id is a listed value.
    assert allowed(Member(VOLUNTEER, worker_id=THEWALL_MEMBER), "trusted", listed) is False
    assert allowed({"trust_id": VOLUNTEER, "worker_id": THEWALL_MEMBER}, "trusted", listed) is False


def test_what_a_member_says_about_itself_does_not_make_it_trusted():
    claims = {"trust_id": VOLUNTEER, "trusted": True, "pool": "coding", "privacy": "any", "role": "admin"}
    assert allowed(claims, "trusted", TRUSTED) is False
    assert allowed(SimpleNamespace(trust_id=VOLUNTEER, trusted=True), "trusted", TRUSTED) is False


def test_trust_ids_match_exactly():
    lookalikes = (
        THEWALL_MEMBER.upper(), THEWALL_MEMBER + " ", " " + THEWALL_MEMBER, THEWALL_MEMBER[:-1],
        THEWALL_MEMBER + "0", THEWALL_MEMBER + "\n", THEWALL_MEMBER + "\x00",
    )
    for lookalike in lookalikes:
        assert allowed(Member(lookalike), "trusted", TRUSTED) is False, repr(lookalike)


# ---- the shapes of trusted_ids --------------------------------------------------------------

@pytest.mark.parametrize("trusted_ids", [
    {THEWALL_MEMBER}, frozenset({THEWALL_MEMBER}), [THEWALL_MEMBER], (THEWALL_MEMBER,),
    {THEWALL_MEMBER: "note"}, iter([THEWALL_MEMBER]), (i for i in [THEWALL_MEMBER]),
    THEWALL_MEMBER, f"other-1, {THEWALL_MEMBER} ,other-2", f"{THEWALL_MEMBER},",
], ids=repr)
def test_trusted_ids_may_be_any_collection_or_the_config_string(trusted_ids):
    assert allowed(Member(THEWALL_MEMBER), "trusted", trusted_ids) is True
    # A fresh stand-in for the iterators, which are used up by the first call.
    if not hasattr(trusted_ids, "__next__"):
        assert allowed(Member(VOLUNTEER), "trusted", trusted_ids) is False


def test_a_config_string_is_never_matched_by_substring():
    assert allowed(Member("abc"), "trusted", "abcdef") is False
    assert allowed(Member("def"), "trusted", "abcdef") is False
    assert allowed(Member("abc"), "trusted", "abcdef,abc") is True
    assert allowed(Member("abc"), "trusted", b"abc") is False


def test_odd_trusted_ids_trust_nobody():
    for trusted_ids in (42, 3.5, True, object(), THEWALL_MEMBER.encode(), {THEWALL_MEMBER: 1}.items()):
        assert allowed(Member(THEWALL_MEMBER), "trusted", trusted_ids) is False, repr(trusted_ids)


def test_parse_trusted_ids():
    assert parse_trusted_ids("a,b") == frozenset({"a", "b"})
    assert parse_trusted_ids(" a , b ,, ,c ") == frozenset({"a", "b", "c"})
    assert parse_trusted_ids("") == frozenset()
    assert parse_trusted_ids(None) == frozenset()
    assert parse_trusted_ids(["a", " b ", "", None, 3]) == frozenset({"a", "b"})
    assert parse_trusted_ids(42) == frozenset()
    assert parse_trusted_ids(b"a,b") == frozenset()


def test_the_trusted_list_is_not_changed():
    trusted = {THEWALL_MEMBER}
    before = set(trusted)
    allowed(Member(VOLUNTEER), "trusted", trusted)
    allowed(Member(VOLUNTEER), "open", trusted)
    assert trusted == before


def test_every_pairing_of_mode_and_member_is_a_plain_bool():
    members = [Member(THEWALL_MEMBER), Member(VOLUNTEER), Member(None), None, {}]
    modes = list(MODES) + [None, "x", 3]
    for member, mode in itertools.product(members, modes):
        assert allowed(member, mode, TRUSTED) in (True, False)
        assert isinstance(allowed(member, mode, TRUSTED), bool)


def test_the_module_imports_nothing_from_dlne():
    # It is imported by the router; keeping it free of engine imports means no cycle and no config read.
    import inspect
    source = inspect.getsource(privacy_policy)
    assert "from engine" not in source and "import engine" not in source
