"""
engine/coding_router.py on its own (no sockets): picking, sticky sessions, routes and their
expiry, the member queue, role assignment, scrubbing, and the pieces around it that the router
depends on (heartbeat validation in engine/authn.py, the CODING_* config keys, the registry's
coding-member views). The coordinator's endpoints are in tests/test_coding_endpoint.py.
"""

import asyncio
import base64
import json
import math
import time

import pytest

from engine import authn, canary, coding_member_select
from engine import config as engine_config
from engine import coding_router as cr
from engine.coding_router import CodingMember, CodingRouter, assign_role, normalize_privacy, normalize_trust_ids
from engine.registry import WorkerRegistry, is_coding_member
from engine.e2e import generate_keypair
from tests.coding_router_fakes import make_identity, sign_block

MODEL = "Qwen3.6-35B-A3B-UD-IQ3_S.gguf"
TRUSTED = "a" * 32
OTHER = "b" * 32


class Clock:
    t = 5000.0

    def __call__(self):
        return self.t


def member(worker_id, *, slots=2, busy=0, prefill=100.0, decode=30.0, trust=OTHER, ready=True,
           base="http://127.0.0.1:9", floors=True, pubkey="KEY", model=MODEL):
    return CodingMember(worker_id=worker_id, model=model, tier="qwen36-a3b", ready=ready, slots=slots, busy=busy,
                        decode_tps=decode, prefill_tps=prefill, pubkey=pubkey, no_trace=True, base_url=base,
                        floors_met=floors, trust_id=trust)


@pytest.fixture(autouse=True)
def canary_book(tmp_path):
    """The canary book (flags that stop a member getting traffic) in a private file, never the install's."""
    previous = canary.set_book(canary.CanaryBook(path=str(tmp_path / "canaries.json")))
    yield canary.book()
    canary.set_book(previous)


@pytest.fixture
def router():
    wall = Clock()
    wall.t = 2_000_000_000.0           # epoch seconds, for last_served_at
    return CodingRouter(clock=Clock(), wall_clock=wall)


def pick(router, members, session=None, privacy="any", **kwargs):
    return router.pick(members, session, privacy, **kwargs)


# ======================================================================================
# pick: least busy, ties, exclusions
# ======================================================================================

def test_the_least_busy_share_of_slots_wins(router):
    a, b, c = member("a", slots=2, busy=1), member("b", slots=2, busy=0), member("c", slots=4, busy=2)
    assert pick(router, [a, b, c]).worker_id == "b"
    assert pick(router, [a, c]).worker_id == "a"                    # equal shares: a tie, broken below


def test_a_tie_goes_to_the_faster_reader_then_the_faster_writer_then_the_key(router):
    assert pick(router, [member("a", prefill=100), member("b", prefill=200)]).worker_id == "b"
    assert pick(router, [member("a", prefill=100, decode=20), member("b", prefill=100, decode=40)]).worker_id == "b"
    assert pick(router, [member("b"), member("a")]).worker_id == "a"


def test_in_flight_requests_and_held_routes_count_as_load_whichever_is_higher(router):
    a, b = member("a", prefill=200), member("b", prefill=100)
    router._reserve(a)
    assert pick(router, [a, b]).worker_id == "b"
    assert router.load(member("a", busy=1)) == 1 and router.load(member("a", busy=1)) == router.load(a)
    router.issue_route(b, None, "any")
    assert router.load(b) == 1                       # a held route is load on its member...
    assert router.load(a) == 1


def test_a_full_member_is_never_picked_and_none_means_none(router):
    assert pick(router, [member("a", slots=2, busy=2)]) is None
    assert pick(router, [member("a", slots=1, busy=5)]) is None
    assert pick(router, []) is None


def test_reserve_leaves_slots_for_agents(router):
    a = member("a", slots=2, busy=1)
    assert pick(router, [a]).worker_id == "a"
    assert pick(router, [a], reserve=1) is None
    assert pick(router, [member("a", slots=3, busy=1)], reserve=1).worker_id == "a"


def test_exclude_by_key_or_worker_id(router):
    a, b = member("a", prefill=200), member("b", prefill=100)
    assert pick(router, [a, b], exclude=[a.key]).worker_id == "b"
    assert pick(router, [a, b], exclude=["a"]).worker_id == "b"
    assert pick(router, [a, b], exclude=["a", "b"]) is None


def test_a_member_that_cannot_be_routed_is_never_picked(router):
    assert pick(router, [member("a", ready=False)]) is None
    assert pick(router, [member("a", base=None)]) is None
    assert pick(router, [member("a", floors=False)]) is None
    assert pick(router, [member("a", trust=None)]) is None, "a block that did not verify"
    router.block(TRUSTED)
    assert pick(router, [member("a", trust=TRUSTED)]) is None, "blocked by trust id"
    assert pick(router, [member("a", trust=TRUSTED), member("b", trust=OTHER)]).worker_id == "b"
    router.unblock(TRUSTED.upper())
    assert pick(router, [member("a", trust=TRUSTED)]).worker_id == "a"


def test_a_member_flagged_by_a_canary_is_never_picked(router, canary_book):
    """Flags are kept by trust id: a worker id is the member's to choose, its identity key is not."""
    a, b = member("a", prefill=200, trust=TRUSTED), member("b", prefill=100, trust=OTHER)
    canary_book.presented_key(canary_book.issue(TRUSTED).key, "198.51.100.5")
    assert router.is_blocked(a) and not router.is_blocked(b)
    assert pick(router, [a, b]).worker_id == "b"
    assert pick(router, [a]) is None
    assert pick(router, [member("a", trust=OTHER)]).worker_id == "a", "a flag on a worker id means nothing"
    canary_book.unflag(TRUSTED)
    assert pick(router, [a, b]).worker_id == "a"


@pytest.mark.anyio
async def test_forward_to_refuses_a_member_that_is_not_routable_free_or_answering(router):
    class Never:        # a client that must not be used
        def build_request(self, *args, **kwargs):
            raise AssertionError("nothing should be sent")

    a = member("a", slots=1, trust=TRUSTED)
    with pytest.raises(LookupError):
        await router.forward_to(Never(), lambda: [a], OTHER, b"{}")
    with pytest.raises(LookupError):
        await router.forward_to(Never(), lambda: [a], "a", b"{}")            # a worker id is not a trust id
    with pytest.raises(LookupError):                                          # the same worker name, another identity
        await router.forward_to(Never(), lambda: [a], member("a", slots=1, trust=OTHER), b"{}")
    with pytest.raises(LookupError):                                          # the same identity, another worker name
        await router.forward_to(Never(), lambda: [a], member("z", slots=1, trust=TRUSTED), b"{}")
    with pytest.raises(LookupError):
        await router.forward_to(Never(), lambda: [member("a", trust=None)], TRUSTED, b"{}")
    router.block(TRUSTED)
    with pytest.raises(LookupError):
        await router.forward_to(Never(), lambda: [a], TRUSTED, b"{}")
    router.unblock(TRUSTED)
    router._reserve(a)
    with pytest.raises(RuntimeError, match="free slot"):
        await router.forward_to(Never(), lambda: [a], TRUSTED.upper(), b"{}")


@pytest.mark.anyio
@pytest.mark.parametrize("parsed", [None, {}, [], "text", {"messages": [{"role": "user", "content": "hi"}]},
                                    {"prompt": "def f("}, {"dlne_e2e": "not an object"}, {"dlne_e2e": None},
                                    {"messages": [], "dlne_e2e": 1}])
@pytest.mark.parametrize("route_id", [None, "a-route", ""])
async def test_serve_leaves_every_plain_body_to_the_local_engine(router, parsed, route_id):
    class Never:        # a client that must not be used
        def build_request(self, *args, **kwargs):
            raise AssertionError("a plain body was sent to a member")

    members = [member("a", trust=TRUSTED)]
    for path in ("/v1/chat/completions", "/v1/completions"):
        assert await router.serve(client=Never(), members_fn=lambda: members, path=path, raw_body=b"{}",
                                  parsed=parsed, privacy="any", route_id=route_id) is None
    assert router.in_flight() == 0


def test_blocked_members_are_left_out_of_the_member_list(router):
    registry = WorkerRegistry()
    trust = registered(registry, "w1")
    assert [m.worker_id for m in router.members(registry, lambda ip, port: 7000)] == ["w1"]
    router.block(trust)
    assert router.members(registry, lambda ip, port: 7000) == []
    router.unblock(trust)
    assert [m.worker_id for m in router.members(registry, lambda ip, port: 7000)] == ["w1"]


def test_a_member_is_active_once_a_routed_request_has_finished_on_it(router):
    a = member("a", trust=TRUSTED)
    plain = router._reserve(a)
    plain.release()
    assert router._last_served == {}, "a request that never answered does not count"
    served = router._reserve(a)
    served.served = True
    router._wall.t += 5
    served.release(), served.release()
    assert router._last_served == {TRUSTED: router._wall.t}, "epoch seconds, the clock engine/canary.py reads"
    registry = WorkerRegistry()
    trust = registered(registry, "w1")
    (before,) = router.members(registry, lambda ip, port: 7000)
    assert before.last_served_at is None
    lease = router._reserve(before)
    lease.served = True
    lease.release()
    (after,) = router.members(registry, lambda ip, port: 7000)
    assert after.last_served_at == router._wall()
    assert trust in router._last_served


def test_the_model_filter_compares_file_names(router):
    a = member("a", model="/models/Qwen3.6-35B-A3B-UD-IQ3_S.gguf")
    b = member("b", model="Other.gguf")
    assert pick(router, [a, b], model="qwen3.6-35b-a3b-ud-iq3_s").worker_id == "a"
    assert pick(router, [a, b], model="Other.gguf").worker_id == "b"
    assert pick(router, [a, b], model="Missing.gguf") is None
    assert pick(router, [a, b], model=None) is not None


def test_a_route_needs_a_member_with_a_key(router):
    assert pick(router, [member("a", pubkey=None)], need_pubkey=True) is None
    assert pick(router, [member("a", pubkey=None)]).worker_id == "a"


# ======================================================================================
# Privacy
# ======================================================================================

def test_privacy_modes_and_trust_ids(router):
    trusted, stranger = member("a", trust=TRUSTED), member("b", trust=OTHER)
    both = [stranger, trusted]
    for mode in ("trusted", None, "", "Open", " open", "open,any", "ANY", 7, ["open"], "nonsense"):
        assert pick(router, both, privacy=mode, trusted_ids={TRUSTED}).worker_id == "a", mode
        assert pick(router, [stranger], privacy=mode, trusted_ids={TRUSTED}) is None, mode
    for mode in ("open", "any"):
        assert pick(router, [stranger], privacy=mode).worker_id == "b", "any member may see public code"
        assert pick(router, both, privacy=mode, trusted_ids={TRUSTED}) is not None


def test_nothing_is_trusted_by_default(router):
    assert pick(router, [member("a", trust=TRUSTED)], privacy="trusted") is None
    router.trusted_ids = {TRUSTED}
    assert pick(router, [member("a", trust=TRUSTED)], privacy="trusted").worker_id == "a"


def test_trust_ids_are_whole_strings_of_32_hex_in_either_case():
    assert normalize_trust_ids([TRUSTED.upper(), f" {OTHER} ", "abc", "a" * 31, "a" * 33, "g" * 32, 5, None]) \
        == frozenset({TRUSTED, OTHER})
    assert normalize_trust_ids(None) == frozenset() == normalize_trust_ids(())
    assert normalize_privacy("open") == "open" and normalize_privacy("Open") == "trusted"


def test_a_worker_id_is_never_a_trust_id(router):
    claimed = member("a" * 32, trust=OTHER)              # a worker id that looks like a trust id
    assert pick(router, [claimed], privacy="trusted", trusted_ids={"a" * 32}) is None


# ======================================================================================
# Sticky sessions
# ======================================================================================

def test_a_session_keeps_its_member_for_30_minutes_after_the_last_request(router):
    a, b = member("a", prefill=200), member("b", prefill=100)
    assert pick(router, [a, b], "s1").worker_id == "a"
    busier = member("a", prefill=200, busy=1)
    assert pick(router, [busier, b], "s1").worker_id == "a", "sticky beats least busy"
    assert pick(router, [busier, b], "s2").worker_id == "b"
    router._clock.t += 30 * 60 - 1
    assert pick(router, [busier, b], "s1").worker_id == "a", "each use renews it"
    router._clock.t += 30 * 60 + 1
    assert pick(router, [busier, b], "s1").worker_id == "b", "a session quiet for 30 minutes is placed afresh"


def test_a_sticky_member_that_is_full_gone_or_not_allowed_is_not_used(router):
    a, b = member("a", prefill=200, trust=TRUSTED), member("b", prefill=100)
    assert pick(router, [a, b], "s", privacy="any").worker_id == "a"
    assert pick(router, [member("a", prefill=200, busy=2, trust=TRUSTED), b], "s").worker_id == "b"
    assert pick(router, [b], "s").worker_id == "b"
    assert pick(router, [a, b], "s2", privacy="any").worker_id == "a"
    assert pick(router, [a, b], "s2", privacy="trusted", trusted_ids={OTHER}).worker_id == "b"


# ======================================================================================
# pick: the requester's own machine (2.5.0)
# ======================================================================================

def test_the_requesters_own_member_comes_before_the_least_busy_while_it_is_fast_enough(router):
    mine, other = member("mine", busy=1, decode=25.0, trust=TRUSTED), member("other", decode=40.0, prefill=300.0)
    assert pick(router, [mine, other]).worker_id == "other", "nothing said: the least busy"
    assert pick(router, [mine, other], own=TRUSTED).worker_id == "mine", "no engine speed measured yet: own"
    assert pick(router, [mine, other], own=TRUSTED, engine_tps=25.0).worker_id == "mine", "as fast as the engine: own"
    assert pick(router, [mine, other], own=TRUSTED, engine_tps=25.1).worker_id == "other", "slower than the engine: not own"
    assert pick(router, [mine, other], own=OTHER).worker_id == "other", "own names a member that is not here"
    assert pick(router, [member("mine", busy=2, trust=TRUSTED), other], own=TRUSTED).worker_id == "other", "own is full"
    assert pick(router, [mine, other], own=TRUSTED, privacy="trusted", trusted_ids={OTHER}).worker_id == "other", \
        "own may not have this code"
    assert pick(router, [mine], own=TRUSTED, engine_tps=99.0).worker_id == "mine", "too slow, but the only one there is"
    assert pick(router, [mine, other], own=TRUSTED.upper()).worker_id == "other", "a trust id is compared as given"


def test_the_sessions_sticky_member_still_comes_before_the_requesters_own(router):
    mine, other = member("mine", trust=TRUSTED), member("other", prefill=300.0)
    assert pick(router, [mine, other], "s").worker_id == "other"
    assert pick(router, [mine, other], "s", own=TRUSTED).worker_id == "other", "the session's prompt is cached there"
    assert pick(router, [mine, other], "s2", own=TRUSTED).worker_id == "mine"
    assert pick(router, [mine, other], "s2").worker_id == "mine", "and own is sticky in its turn"


def test_commit_false_leaves_no_memory(router):
    a, b = member("a", prefill=200), member("b", prefill=100)
    assert pick(router, [a, b], "s", commit=False).worker_id == "a"
    assert router._sessions == {}
    assert pick(router, [member("a", prefill=200, busy=1), b], "s").worker_id == "b"


def test_a_session_id_is_not_kept_as_given_and_odd_ids_are_ignored(router):
    pick(router, [member("a")], "secret-session-id")
    assert all("secret-session-id" not in key for key in router._sessions)
    pick(router, [member("a")], "x" * (cr.SESSION_ID_MAX_CHARS + 1))
    pick(router, [member("a")], "   ")
    pick(router, [member("a")], None)
    pick(router, [member("a")], 12345)
    assert len(router._sessions) == 1


def test_the_session_table_is_bounded(router, monkeypatch):
    monkeypatch.setattr(cr, "MAX_SESSIONS", 5)
    for i in range(20):
        pick(router, [member("a")], f"s{i}")
    assert len(router._sessions) == 5


def test_a_member_that_failed_is_left_out_for_30_seconds(router):
    a, b = member("a", prefill=200), member("b", prefill=100)
    router.mark_failed(a)
    assert pick(router, [a, b]).worker_id == "b"
    router._clock.t += cr.FAILURE_COOLDOWN_SECONDS + 1
    assert pick(router, [a, b]).worker_id == "a"


# ======================================================================================
# Routes
# ======================================================================================

def test_a_route_id_is_opaque_unique_and_good_for_ten_minutes(router):
    a = member("worker-with-a-name")
    ids = {router.issue_route(a, "s", "any") for _ in range(50)}
    assert len(ids) == 50 and all(len(i) >= 20 and "worker" not in i for i in ids)
    route_id = next(iter(ids))
    assert router.resolve_route(route_id).key == a.key
    router._clock.t += cr.ROUTE_TTL_SECONDS - 1
    assert router.resolve_route(route_id) is not None
    router._clock.t += 1
    assert router.resolve_route(route_id) is None, "ten minutes, to the second"
    assert router.resolve_route(route_id) is None


@pytest.mark.parametrize("bad", [None, "", "nope", 5, b"x", ["a"], {"a": 1}])
def test_unknown_route_ids_resolve_to_nothing(router, bad):
    router.issue_route(member("a"), None, "any")
    assert router.resolve_route(bad) is None


def test_a_route_remembers_the_mode_it_was_issued_under(router):
    assert router.resolve_route(router.issue_route(member("a"), None, "open")).mode == "open"
    assert router.resolve_route(router.issue_route(member("a"), None, "Open")).mode == "trusted"
    assert router.resolve_route(router.issue_route(member("a"), None, None)).mode == "trusted"


def test_the_route_table_is_bounded_and_drops_the_oldest(router, monkeypatch):
    monkeypatch.setattr(cr, "MAX_ROUTES", 4)
    ids = [router.issue_route(member("a"), None, "any") for _ in range(7)]
    assert [router.resolve_route(i) is not None for i in ids] == [False, False, False, True, True, True, True]
    assert router._hold_counts == {"worker:a": 4}


def test_routes_hold_their_members_slot_until_used_or_thirty_seconds(router):
    a = member("a", slots=1)
    route_id = router.issue_route(a, None, "any")
    assert router.free_slots(a) == 0 and pick(router, [a]) is None
    router._clock.t += cr.ROUTE_HOLD_SECONDS
    assert router.free_slots(a) == 1, "the hold is given back; the route itself lives on"
    assert router.resolve_route(route_id) is not None
    again = router.issue_route(a, None, "any")
    lease = router._try_specific(a, 0, again)        # using the route turns its hold into a real slot
    assert lease is not None and router.free_slots(a) == 0
    lease.release()
    assert router.free_slots(a) == 1 and router._hold_counts == {}


def test_a_used_route_does_not_hold_twice(router):
    a = member("a", slots=2)
    route_id = router.issue_route(a, None, "any")
    first = router._try_specific(a, 0, route_id)
    second = router._try_specific(a, 0, route_id)
    assert first is not None and second is not None and router.in_flight() == 2
    assert router._try_specific(a, 0, route_id) is None
    first.release(), second.release()
    assert router.in_flight() == 0


# ======================================================================================
# The queue
# ======================================================================================

def want(members, privacy="any", **kwargs):
    return cr._Want(lambda: members, None, privacy, frozenset(), **kwargs)


@pytest.mark.anyio
async def test_a_slot_that_frees_up_goes_to_the_best_tier_then_the_earliest(router):
    only = [member("a", slots=1)]
    held = await router.place(want(only), 0, 0)
    assert held is not None
    trial = asyncio.create_task(router.place(want(only), 2, 5))
    await asyncio.sleep(0.05)
    cpu = asyncio.create_task(router.place(want(only), 1, 5))
    await asyncio.sleep(0.05)
    gpu_late = asyncio.create_task(router.place(want(only), 0, 5))
    await asyncio.sleep(0.05)
    assert router.queue_stats() == {"waiting": 3, "in_flight": 1}
    waiting = {"gpu": gpu_late, "cpu": cpu, "trial": trial}
    order = []
    for expected in ("gpu", "cpu", "trial"):
        held.release()
        done, _pending = await asyncio.wait(list(waiting.values()), timeout=1, return_when=asyncio.FIRST_COMPLETED)
        assert done == {waiting[expected]}, expected
        held = waiting.pop(expected).result()
        order.append(expected)
    held.release()
    assert order == ["gpu", "cpu", "trial"] and router.in_flight() == 0


@pytest.mark.anyio
async def test_the_same_tier_is_served_in_arrival_order(router):
    only = [member("a", slots=1)]
    held = await router.place(want(only), 1, 0)
    tasks = []
    for _ in range(3):
        tasks.append(asyncio.create_task(router.place(want(only), 1, 5)))
        await asyncio.sleep(0.03)
    pending = list(tasks)
    for task in tasks:
        held.release()
        done, _ = await asyncio.wait(pending, timeout=1, return_when=asyncio.FIRST_COMPLETED)
        assert done == {task}
        pending.remove(task)
        held = task.result()
    held.release()
    assert router.in_flight() == 0


@pytest.mark.anyio
async def test_a_waiter_that_cannot_use_the_freed_member_does_not_block_one_that_can(router):
    trusted, stranger = member("t", slots=1, trust=TRUSTED), member("s", slots=1, trust=OTHER)
    members = [trusted, stranger]

    def private():
        return cr._Want(lambda: members, None, "trusted", frozenset({TRUSTED}))

    held_trusted = await router.place(private(), 0, 0)
    held_stranger = await router.place(want(members, "any"), 0, 0)
    assert held_trusted.member.worker_id == "t" and held_stranger.member.worker_id == "s"
    waits_for_trusted = asyncio.create_task(router.place(private(), 0, 5))
    await asyncio.sleep(0.05)
    waits_for_any = asyncio.create_task(router.place(want(members, "any"), 2, 5))
    await asyncio.sleep(0.05)
    held_stranger.release()                                # frees the stranger: only the second waiter can use it
    lease = await asyncio.wait_for(waits_for_any, 1)
    assert lease.member.worker_id == "s" and not waits_for_trusted.done()
    held_trusted.release()
    assert (await asyncio.wait_for(waits_for_trusted, 1)).member.worker_id == "t"


@pytest.mark.anyio
async def test_waiting_ends_with_none_when_the_time_is_up(router):
    only = [member("a", slots=1)]
    held = await router.place(want(only), 0, 0)
    started = time.monotonic()
    assert await router.place(want(only), 0, 0.3) is None
    assert 0.25 <= time.monotonic() - started < 2
    assert router.queue_stats() == {"waiting": 0, "in_flight": 1}
    held.release()


@pytest.mark.anyio
async def test_nothing_waits_when_no_member_could_ever_serve(router):
    started = time.monotonic()
    assert await router.place(want([]), 0, 5) is None
    assert await router.place(want([member("a", trust=OTHER)], "trusted"), 0, 5) is None
    assert time.monotonic() - started < 0.5


@pytest.mark.anyio
async def test_a_waiter_gives_up_when_its_members_disappear(router):
    members = [member("a", slots=1)]
    held = await router.place(want(members), 0, 0)
    task = asyncio.create_task(router.place(cr._Want(lambda: members, None, "any", frozenset()), 0, 30))
    await asyncio.sleep(0.1)
    members.clear()
    started = time.monotonic()
    assert await asyncio.wait_for(task, 5) is None
    assert time.monotonic() - started < 3
    held.release()


@pytest.mark.anyio
async def test_a_cancelled_waiter_leaves_no_trace_and_takes_no_slot(router):
    only = [member("a", slots=1)]
    held = await router.place(want(only), 0, 0)
    task = asyncio.create_task(router.place(want(only), 0, 30))
    await asyncio.sleep(0.05)
    assert router.queue_stats()["waiting"] == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert router.queue_stats() == {"waiting": 0, "in_flight": 1}
    held.release()
    assert router.in_flight() == 0 and router.free_slots(only[0]) == 1


@pytest.mark.anyio
async def test_the_member_queue_says_where_a_waiter_stands(router):
    only = [member("a", slots=1)]
    held = await router.place(want(only), 0, 0)
    tickets = [{}, {}, {}]
    tasks = []
    for ticket, priority in zip(tickets, (2, 2, 0)):
        tasks.append(asyncio.create_task(router.place(want(only), priority, 5, ticket)))
        await asyncio.sleep(0.03)
    assert [router.position(t) for t in tickets] == [2, 3, 1]
    assert router.position({}) is None and router.position(None) is None
    held.release()
    lease = await asyncio.wait_for(tasks[2], 1)
    assert router.position(tickets[2]) is None, "no longer waiting"
    assert [router.position(t) for t in tickets[:2]] == [1, 2]
    lease.release()
    for task in tasks[:2]:
        (await asyncio.wait_for(task, 1)).release()
    assert router.capacity(only) == {"slots": 1, "in_use": 0}


@pytest.mark.anyio
async def test_the_engine_gate_says_where_a_waiter_stands_and_never_lets_a_newcomer_jump_the_queue():
    from engine.priority import CodingGate
    gate = CodingGate(slots=1)
    assert gate.try_acquire() and not gate.try_acquire()
    tickets = [{}, {}, {}]
    tasks = []
    for ticket, priority in zip(tickets, (2, 1, 0)):
        tasks.append(asyncio.create_task(gate.acquire(priority, ticket=ticket)))
        await asyncio.sleep(0.03)
    assert [gate.position(t) for t in tickets] == [3, 2, 1]
    assert gate.position({}) is None and gate.position(None) is None
    gate.release()                                    # goes to the best waiter, not to a new arrival
    await asyncio.wait_for(tasks[2], 1)
    assert not gate.try_acquire()
    assert [gate.position(t) for t in tickets] == [2, 1, None]
    assert gate.stats()["waiting"] == 2
    tasks[0].cancel()
    await asyncio.gather(tasks[0], return_exceptions=True)
    assert gate.position(tickets[0]) is None and gate.position(tickets[1]) == 1


def test_a_lease_releases_once(router):
    a = member("a", slots=2)
    lease = router._reserve(a)
    other = router._reserve(a)
    lease.release(), lease.release(), lease.release()
    assert router.in_flight() == 1
    other.release()
    assert router.in_flight() == 0


# ======================================================================================
# Members from the registry
# ======================================================================================

def registered(registry, worker_id, *, ip="10.0.0.5", port=50053, identity=None, signed_at=None, **block):
    key, public, trust = identity or make_identity()
    fields = {"model": MODEL, "tier": "qwen36-a3b", "ready": True, "slots": 2, "busy": 0, "decode_tps": 30.0,
              "prefill_tps": 100.0, "no_trace": True, "identity": public, **block}
    registry.update_worker(worker_id, ip, port, f"tok-{worker_id}",
                           {"pool": "coding", "ram_gb": 32, "gpu": {"has_gpu": True, "vram_total_gb": 8},
                            "coding": sign_block(fields, key, signed_at)})
    return trust


def test_members_are_read_from_signed_blocks_and_carry_their_trust_id(router):
    registry = WorkerRegistry()
    trust = registered(registry, "w1", pubkey=generate_keypair()[1], busy=1)
    (found,) = router.members(registry, lambda ip, port: 7001 if (ip, port) == ("10.0.0.5", 50053) else None)
    assert (found.worker_id, found.key, found.trust_id) == ("w1", "worker:w1", trust)
    assert (found.slots, found.busy, found.decode_tps, found.prefill_tps) == (2, 1, 30.0, 100.0)
    assert found.base_url == "http://127.0.0.1:7001" and found.model == MODEL and found.no_trace


@pytest.mark.parametrize("block, relay, listed", [
    ({"ready": False}, 7001, False),
    ({}, None, False),
    ({"decode_tps": 14.9}, 7001, False),
    ({"prefill_tps": 79.9}, 7001, False),
    ({"decode_tps": 15.0, "prefill_tps": 80.0}, 7001, True),
    ({"decode_tps": 0.0, "prefill_tps": 0.0}, 7001, False),
])
def test_ready_tunnel_and_floors_decide_who_is_routable(router, block, relay, listed):
    registry = WorkerRegistry()
    registered(registry, "w1", **block)
    assert bool(router.members(registry, lambda ip, port: relay)) is listed
    assert len(router.all_members(registry, lambda ip, port: relay)) == 1


def test_the_floors_come_from_the_limits_passed_in(router):
    registry = WorkerRegistry()
    registered(registry, "w1", decode_tps=20.0, prefill_tps=90.0)
    relay = lambda ip, port: 7001
    assert router.members(registry, relay, {"coding_member_min_decode_tps": 25.0}) == []
    assert router.members(registry, relay, {"coding_member_min_prefill_tps": 95.0}) == []
    assert len(router.members(registry, relay, {"coding_member_min_decode_tps": 20.0})) == 1


def test_yielding_and_phone_pool_workers_are_not_coding_members(router):
    registry = WorkerRegistry()
    registered(registry, "w1")
    registry.update_worker("phone", "10.0.0.6", 50053, "t", {"pool": "app", "ram_gb": 12, "gpu": None,
                                                             "app": {"model": "m", "ready": True, "slots": 2}})
    registry.update_worker("gpu", "10.0.0.7", 50053, "t", {"ram_gb": 32, "gpu": {"has_gpu": True, "vram_total_gb": 24}})
    assert [r["worker_id"] for r in registry.coding_members()] == ["w1"]
    hardware = registry.workers["w1"]["hardware"]
    registry.workers["w1"]["hardware"] = {**hardware, "is_yielding": True}
    assert registry.coding_members() == []


def test_a_coding_member_has_no_rpc_server(router):
    registry = WorkerRegistry()
    registered(registry, "w1")
    hardware = registry.workers["w1"]["hardware"]
    assert is_coding_member(hardware) and not registry.rpc_compatible(hardware)
    assert not is_coding_member({"pool": "app"}) and not is_coding_member(None) and not is_coding_member("coding")
    assert registry.get_active_rpc_endpoints() == [] and registry.get_total_vram_gb() == 0.0
    assert registry.get_total_sysram_gb() == 0.0 and registry.get_total_cpu_cores() == 0


def test_a_member_with_a_garbled_block_is_listed_but_never_routable(router):
    registry = WorkerRegistry()
    registry.update_worker("w1", "10.0.0.5", 50053, "t", {"pool": "coding", "ram_gb": 32, "coding": "nonsense"})
    registry.update_worker("w2", "10.0.0.6", 50053, "t", {"pool": "coding", "ram_gb": 32})
    assert len(router.all_members(registry, lambda ip, port: 7001)) == 2
    assert router.members(registry, lambda ip, port: 7001) == []


def test_status_counts_only(router):
    registry = WorkerRegistry()
    registered(registry, "w1", ip="10.0.0.1", slots=2, busy=1)
    registered(registry, "w2", ip="10.0.0.2", slots=4)
    registered(registry, "w3", ip="10.0.0.3", ready=False)
    registered(registry, "w4", ip="10.0.0.4", decode_tps=1.0)
    relay = lambda ip, port: 7001
    router._reserve(router.members(registry, relay)[1])
    assert router.status(registry, relay, {}, True) == {
        "enabled": True, "members": 4, "ready": 2, "slots": 6, "busy": 2,
        "decode_tps_median": 30.0, "decode_tps_min": 30.0, "prefill_tps_median": 100.0, "prefill_tps_min": 100.0}
    assert router.status(registry, relay, {}, False)["enabled"] is False


def test_the_status_speeds_are_the_ready_pools_median_and_minimum(router):
    registry = WorkerRegistry()
    registered(registry, "w1", ip="10.0.0.1", decode_tps=25.25, prefill_tps=90.0)
    registered(registry, "w2", ip="10.0.0.2", decode_tps=40.0, prefill_tps=300.0)
    registered(registry, "w3", ip="10.0.0.3", decode_tps=33.0, prefill_tps=121.0)
    registered(registry, "w4", ip="10.0.0.4", decode_tps=999.0, prefill_tps=9999.0, ready=False)    # not ready: not counted
    status = router.status(registry, lambda ip, port: 7001, {}, True)
    assert (status["decode_tps_median"], status["decode_tps_min"]) == (33.0, 25.2)
    assert (status["prefill_tps_median"], status["prefill_tps_min"]) == (121.0, 90.0)
    none = router.status(WorkerRegistry(), lambda ip, port: 7001, {}, True)
    assert [none[k] for k in ("decode_tps_median", "decode_tps_min", "prefill_tps_median", "prefill_tps_min")] == [None] * 4
    text = json.dumps(status)
    assert "w1" not in text and "10.0.0" not in text, "aggregates only"


# ======================================================================================
# What comes back from a member
# ======================================================================================

@pytest.mark.anyio
async def test_a_sealed_stream_is_joined_without_a_byte_changed():
    pieces = [b'data: {"dlne_e2e": {"n": 0, "ct": "AAAA"}}\n', b'\ndata: [DONE]\n\n']

    async def rest():
        for piece in pieces[1:]:
            yield piece

    far = asyncio.get_running_loop().time() + 60
    assert b"".join([c async for c in cr._joined(pieces[0], rest(), far)]) == b"".join(pieces)


@pytest.mark.anyio
async def test_a_stream_that_outlives_its_deadline_is_cut_even_if_the_member_is_silent():
    async def silent():
        await asyncio.sleep(30)
        yield b"never"

    started = time.monotonic()
    soon = asyncio.get_running_loop().time() + 0.3
    pieces = []
    with pytest.raises(asyncio.TimeoutError):
        async for piece in cr._joined(b"first", silent(), soon):
            pieces.append(piece)
    assert pieces == [b"first"] and 0.25 <= time.monotonic() - started < 3


@pytest.mark.anyio
async def test_a_stream_already_past_its_deadline_gives_what_it_has_and_stops():
    async def endless():
        while True:
            yield b"more"

    pieces = []
    with pytest.raises(asyncio.TimeoutError):
        async for piece in cr._joined(b"first", endless(), asyncio.get_running_loop().time() - 1):
            pieces.append(piece)
    assert pieces == [b"first"]


def test_the_unsealed_answers_say_nothing_about_the_request_the_member_or_the_network():
    for reason in ("expired", "gone", "busy", "failed", "privacy"):
        served = cr._reroute(reason)
        payload = json.loads(served.body)
        assert served.status == 503 and set(payload) == {"error"}
        assert payload["error"] == {"code": "dlne_reroute", "message": cr._REROUTE_MESSAGES[reason], "reason": reason}
        assert served.retry_after == (3 if reason == "busy" else None)
    assert cr._dropped_notice() == b'data: {"dlne": {"event": "member_dropped"}}\n\ndata: [DONE]\n\n'


# ======================================================================================
# Roles
# ======================================================================================

TIERS = [{"id": "qwen36-a3b", "name": "Qwen3.6", "file": MODEL, "min_vram_gb": 15, "enabled": True}]


def gpu(vram, ram=32.0, **extra):
    return {"gpu": {"has_gpu": True, "vram_total_gb": vram}, "ram_gb": ram, **extra}


class Calls(list):
    """choose_tier's (vram, ram) calls; ``.slots`` holds choose_slots' (tier id, vram, ram) calls."""
    slots: list


@pytest.fixture
def chooser(monkeypatch):
    calls = Calls()
    calls.slots = []

    def choose(vram, ram, tiers):
        calls.append((vram, ram))
        return tiers[0] if vram >= 8 and vram + ram >= 24 else None

    def slots(tier, vram, ram):
        calls.slots.append((tier["id"], vram, ram))
        return 2 if vram >= 12 else 1

    monkeypatch.setattr(coding_member_select, "choose_tier", choose)
    monkeypatch.setattr(coding_member_select, "choose_slots", slots)
    return calls


@pytest.mark.parametrize("hardware, near, role, tiered", [
    ({"gpu": None, "ram_gb": 16}, True, "app", False),
    ({"gpu": {"has_gpu": False, "vram_total_gb": 0}, "ram_gb": 16}, False, "app", False),
    ({"ram_gb": 16}, None, "app", False),
    ({"pool": "app", **gpu(24)}, False, "app", False),
    (gpu(24), True, "rpc", False),
    (gpu(4), True, "rpc", False),
    (gpu(8), False, "coding", True),
    (gpu(24), False, "coding", True),
    (gpu(24, pool="coding"), False, "coding", True),
    (gpu(8, coding_ok=True), False, "coding", True),
    (gpu(8, coding_ok=False), False, None, False),
    (gpu(24, coding_ok="yes"), False, None, False),
    (gpu(24, coding_ok=None), False, None, False),
    (gpu(24, coding_ok=False), True, "rpc", False),
    (gpu(7.9), False, None, False),
    (gpu(8, ram=8), False, None, False),
    (gpu(24), None, None, False),
    ("nonsense", True, "app", False),
    (None, False, "app", False),
])
def test_assign_role(chooser, hardware, near, role, tiered):
    got_role, tier, slots = assign_role(hardware, near, TIERS)
    assert got_role == role and (tier is not None) is tiered
    if tiered:
        assert tier is TIERS[0] and slots == (2 if hardware["gpu"]["vram_total_gb"] >= 12 else 1)
    else:
        assert slots is None, "no tier, no slot count"


def test_a_gpu_that_is_not_near_is_only_sized_with_its_own_memory(chooser):
    assign_role(gpu(12.0, ram=20.0), False, TIERS)
    assign_role(gpu(24.0), True, TIERS)
    assert chooser == [(12.0, 20.0)] and chooser.slots == [("qwen36-a3b", 12.0, 20.0)]


def test_the_coding_model_is_sized_on_the_ram_the_coding_job_may_use(chooser):
    """ram_gb is the RPC node's lend (MAX_RAM_GB, 4 GB by default); coding_ram_gb is the coding job's (2.4.9)."""
    assert assign_role(gpu(8.0, ram=4.0), False, TIERS) == (None, None, None)
    role, tier, slots = assign_role({**gpu(8.0, ram=4.0), "coding_ram_gb": 30.0}, False, TIERS)
    assert role == "coding" and tier is TIERS[0] and slots == 1
    assert chooser[-1] == (8.0, 30.0) and chooser.slots[-1] == ("qwen36-a3b", 8.0, 30.0), "the slots are sized on the same memory"


def test_a_tier_the_slot_estimate_cannot_read_still_gets_one_slot(monkeypatch):
    """TIERS here has no context or size; choose_slots never raises and answers 1, the join test's figure."""
    monkeypatch.setattr(coding_member_select, "choose_tier", lambda vram, ram, tiers: tiers[0])
    assert assign_role(gpu(24), False, TIERS) == ("coding", TIERS[0], 1)


@pytest.mark.parametrize("rtt, role", [(None, "coding"), (0.0, "coding"), (119.9, "coding"), (120.0, "coding"),
                                       (120.1, None), (900.0, None)])
def test_a_far_gpu_gets_a_model_of_its_own_only_within_the_member_limit(chooser, rtt, role):
    assert assign_role(gpu(24), False, TIERS, rtt_ms=rtt, max_member_rtt_ms=120.0)[0] == role


def test_without_a_member_limit_any_far_gpu_may_be_a_member(chooser):
    assert assign_role(gpu(24), False, TIERS, rtt_ms=900.0)[0] == "coding"


def test_first_seen_is_kept_and_bounded(router, monkeypatch):
    first = router.first_seen("w")
    time.sleep(0.01)
    assert router.first_seen("w") == first
    monkeypatch.setattr(cr, "MAX_FIRST_SEEN", 3)
    for i in range(10):
        router.first_seen(f"x{i}")
    assert len(router._first_seen) == 3


# ======================================================================================
# Heartbeat validation (engine/authn.py)
# ======================================================================================

REMOVE = object()


def b64(raw):
    return base64.b64encode(raw).decode()


# The same 32 bytes with other bits in the unused tail of the last character: decodes to the same bytes
# but is not what b64encode would write, so it is refused.
_PUBKEY = b64(bytes(range(32)))
NON_CANONICAL = _PUBKEY[:-2] + chr(ord(_PUBKEY[-2]) + 1) + "="


def coding_hw(**overrides):
    block = {"model": MODEL, "tier": "qwen36-a3b", "ready": True, "slots": 2, "busy": 0, "decode_tps": 32.5,
             "prefill_tps": 121.2, "pubkey": _PUBKEY, "no_trace": True,
             "identity": b64(bytes(range(1, 33))), "signed_at": 1793000000, "sig": b64(bytes(range(64)))}
    block.update(overrides)
    for key in [k for k, v in block.items() if v is REMOVE]:
        del block[key]
    return {"pool": "coding", "ram_gb": 32.0, "gpu": {"has_gpu": True, "vram_total_gb": 8.0}, "coding": block}


def valid(hw):
    return authn.validate_hardware_profile(hw)[0]


def test_a_complete_coding_block_is_accepted():
    assert authn.validate_hardware_profile(coding_hw()) == (True, "ok")
    assert valid({"pool": "coding", "ram_gb": 32.0})
    assert valid(coding_hw(model=None, tier=None, pubkey=None, identity=None, sig=None, signed_at=None))
    assert valid({"pool": "coding", "coding": {}})
    for key in ("model", "tier", "ready", "slots", "busy", "decode_tps", "prefill_tps", "pubkey", "no_trace",
                "identity", "signed_at", "sig"):
        assert valid(coding_hw(**{key: REMOVE})), f"{key} may be absent"


@pytest.mark.parametrize("overrides", [
    {"extra": 1}, {"worker_id": "x"}, {"trust_id": "a" * 32},
    {"model": ""}, {"model": "m" * 129}, {"model": 7}, {"model": "a\nb"}, {"tier": ["x"]}, {"tier": "t" * 129},
    {"ready": "yes"}, {"ready": 1}, {"ready": "true"}, {"no_trace": 0}, {"no_trace": "false"},
    {"slots": 0}, {"slots": 65}, {"slots": True}, {"slots": 2.0}, {"slots": "2"},
    {"busy": -1}, {"busy": 1025}, {"busy": True}, {"busy": 1.5},
    {"decode_tps": -0.1}, {"decode_tps": 100001}, {"decode_tps": math.nan}, {"decode_tps": math.inf},
    {"decode_tps": True}, {"decode_tps": "30"}, {"prefill_tps": -1}, {"prefill_tps": math.nan}, {"prefill_tps": [1]},
    {"pubkey": b64(bytes(31))}, {"pubkey": b64(bytes(33))}, {"pubkey": 5}, {"pubkey": ""},
    {"pubkey": _PUBKEY.replace("=", "")}, {"pubkey": _PUBKEY + "\n"}, {"pubkey": " " + _PUBKEY[:-1]},
    {"pubkey": b64(bytes([255] * 32)).replace("+", "-").replace("/", "_")},
    {"pubkey": base64.urlsafe_b64encode(bytes([251] * 32)).decode()},
    {"pubkey": _PUBKEY[:-2] + "=="}, {"pubkey": NON_CANONICAL},
    {"identity": NON_CANONICAL}, {"sig": _PUBKEY},
    {"identity": b64(bytes(31))}, {"identity": b64(bytes(64))}, {"identity": 5}, {"identity": ""},
    {"identity": b64(bytes(range(1, 33)))[:-1] + "!"},
    {"sig": b64(bytes(63))}, {"sig": b64(bytes(32))}, {"sig": 5}, {"sig": ""}, {"sig": b64(bytes(65))},
    {"signed_at": 1.5}, {"signed_at": True}, {"signed_at": "1793000000"}, {"signed_at": -1},
    {"signed_at": 10 ** 13}, {"signed_at": [1]},
])
def test_an_implausible_coding_block_is_refused(overrides):
    assert not valid(coding_hw(**overrides)), overrides


def test_a_member_stepping_aside_reports_no_slots_and_is_still_accepted():
    """While its owner games, a member says ready false with 0 slots (engine/coding_member.py); that
    heartbeat must not be refused, and 0 slots are only believed together with ready false."""
    assert valid(coding_hw(ready=False, slots=0))
    assert not valid(coding_hw(ready=True, slots=0))
    assert not valid(coding_hw(ready=REMOVE, slots=0))


def test_coding_ok_is_a_real_boolean():
    assert valid({**coding_hw(), "coding_ok": True}) and valid({**coding_hw(), "coding_ok": False})
    for nonsense in ("false", 0, 1, None, "yes"):
        assert not valid({**coding_hw(), "coding_ok": nonsense}), nonsense


def test_pool_and_blocks_must_agree():
    block = coding_hw()["coding"]
    assert not valid({"pool": "app", "coding": block})
    assert not valid({"coding": block})
    assert not valid({"pool": None, "coding": block})
    assert not valid({"pool": "coding", "coding": block, "app": {"model": "m", "ready": True, "slots": 1}})
    assert not valid({"pool": "coding", "coding": "nonsense"})
    assert not valid({"pool": "coding", "coding": ["model"]})
    assert not valid({"pool": "rpc"}) and not valid({"pool": ""}) and not valid({"pool": 1})
    assert valid({"pool": "app"}) and valid({"pool": None}) and valid({"pool": "coding"})


# ======================================================================================
# Config
# ======================================================================================

def limits_for(monkeypatch, raw):
    monkeypatch.setattr(engine_config, "get_config", lambda force_reload=False: raw)
    return engine_config.get_worker_limits()


def test_coding_member_defaults_are_off_and_unrestricted(monkeypatch):
    limits = limits_for(monkeypatch, {})
    assert limits["coding_members_enabled"] is False and limits["trusted_members"] == []
    assert limits["coding_member_min_decode_tps"] == 15.0 and limits["coding_member_min_prefill_tps"] == 80.0


@pytest.mark.parametrize("raw, enabled", [("true", True), ("TRUE", True), (" true ", True), ("false", False),
                                          ("yes", False), ("1", False), ("", False), ("tru", False)])
def test_coding_members_are_on_only_for_the_word_true(monkeypatch, raw, enabled):
    assert limits_for(monkeypatch, {"CODING_MEMBERS_ENABLED": raw})["coding_members_enabled"] is enabled


def test_trusted_members_are_trust_ids(monkeypatch):
    raw = f"{TRUSTED.upper()}, {OTHER} ,{TRUSTED}, worker-7, {'c' * 31}, {'d' * 33},,  ,{'e' * 16}{'E' * 16}"
    assert limits_for(monkeypatch, {"TRUSTED_MEMBERS": raw})["trusted_members"] == [TRUSTED, OTHER, "e" * 32]
    assert limits_for(monkeypatch, {"TRUSTED_MEMBERS": ""})["trusted_members"] == []
    assert limits_for(monkeypatch, {"TRUSTED_MEMBERS": "worker-7,nonsense"})["trusted_members"] == []


def test_the_removed_member_queue_setting_is_ignored(monkeypatch):
    """Plain requests never wait for a member (only the local engine serves them), so the setting that
    bounded that wait is gone; an old config line mentioning it changes nothing."""
    limits = limits_for(monkeypatch, {"CODING_MEMBER_QUEUE_SECONDS": "45"})
    assert "coding_member_queue_seconds" not in limits


def test_the_floors_in_config_reach_the_join_test_check(monkeypatch):
    limits = limits_for(monkeypatch, {"CODING_MEMBER_MIN_DECODE_TPS": "25"})
    assert not coding_member_select.meets_floor(20.0, 200.0, limits)
    assert coding_member_select.meets_floor(25.0, 80.0, limits)


# ---- 2.5.0: docked laptops shard; a yielding GPU is given no new job --------------------------------------------

@pytest.mark.parametrize("hardware, near, role, tiered", [
    (gpu(24, docked=True), True, "rpc", False),
    (gpu(24), True, "rpc", False),                              # cannot say: as before
    (gpu(24, docked=False), True, "coding", True),              # near but off its dock: a whole model of its own
    (gpu(8, ram=4, docked=False), True, None, False),           # ... or nothing, when it cannot hold one
    (gpu(24, docked=False), False, "coding", True),
    (gpu(24, is_yielding=True), True, None, False),             # stepping aside: no new job, it keeps the one it has
    (gpu(24, is_yielding=True), False, None, False),
    (gpu(24, is_yielding=False, docked=True), True, "rpc", False),
])
def test_docked_and_yielding_decide_a_near_gpus_job(chooser, hardware, near, role, tiered):
    got_role, tier, slots = assign_role(hardware, near, TIERS)
    assert got_role == role and (tier is not None) is tiered and (slots is not None) is tiered
