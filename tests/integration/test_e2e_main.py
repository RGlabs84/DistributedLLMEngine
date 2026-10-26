"""
The coding-members system working as one (docs/coding-members.md), with real processes on loopback.

Every piece has unit tests; these scenarios are the ones where the pieces meet: the compiled extension's client
(sealing, route lookup, reading sealed answers, retry, place in line) talking to the real coordinator (router,
forwarding, queue, canaries, privacy filter) which reaches real coding members (the worker program, the member proxy,
llama-server) through the real reverse tunnel. The model is a 0.5B coder on the CPU, so what is asserted is how the
system behaves, never how well the model answers.

This module's world: a trusted member A, an unlisted member B, and a member C whose identity signature is damaged.
Run with tests/integration/run_integration.sh (skipped otherwise; see conftest.py).
"""

import json
import secrets
from typing import Dict

import pytest

from tests.integration import checks
from tests.integration.helpers import (FORBIDDEN_AT_MEMBER, assert_member_traffic_sealed, assert_nothing_readable, build,
                                       hits, open_mode_kwargs,
                                       post_count, posts_since, prompt_with, route_lookups, served_by)
from tests.integration.world import Holder, wait_until

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def main(resources, run_root):
    world = build(run_root, resources, "main", [{"name": "a"}, {"name": "b"}, {"name": "c", "tamper": "sig"}],
                  trusted=["a"], ready=2)
    yield world
    world.stop()
    assert world.real_home_untouched(), "a process of the run wrote into the real ~/.dlne"


def test_sealed_request_is_answered_by_a_member_and_nothing_readable_crosses_the_wire(main):
    w = main
    marker = "zebra-" + secrets.token_hex(6)
    before, tap_before, posts_before = hits(w), len(w.client_tap.snapshot()), post_count(w)
    result = w.ask(prompt_with(marker))
    assert result["ev"] == "done", result
    answer = result["text"]
    assert len(answer.strip()) > 10, "the member sent back an empty answer"
    assert result["content"] == answer

    # served by the trusted member (the editor's default privacy mode is `trusted`), and by nobody else
    assert served_by(before, hits(w)) == ["a"]
    [post] = posts_since(w, posts_before)

    # the editor-to-coordinator leg: a sealed envelope and a route, not a prompt
    inner = checks.envelope(post["body"])
    assert inner is not None, post["body"][:200]
    assert set(inner) == {"v", "epk", "nonce", "ct"} and inner["v"] == 1
    assert post["headers"].get("x-dlne-route"), post["headers"]
    assert post["headers"].get("x-dlne-privacy") == "trusted"
    assert post["headers"].get("x-dlne-session") == result["session"]
    assert b'"messages"' not in post["body"] and b'"model"' not in post["body"]
    assert post["answer_status"].startswith("HTTP/1.1 200")
    assert post["answer_headers"].get("x-dlne-served") == "member"
    lines = checks.sse_data_lines(post["answer_body"])
    assert lines[-1] == "[DONE]" and len(lines) > 4
    for line in lines[:-1]:
        obj = json.loads(line)
        assert list(obj) == ["dlne_e2e"] and "ct" in obj["dlne_e2e"], line[:120]
    assert b'"choices"' not in post["answer_body"] and b'"delta"' not in post["answer_body"]

    # the tunnel leg: the same bytes, relayed untouched, and nothing about who asked
    [relayed] = [p for p in checks.chat_posts(w.members["a"].connections()) if p["body"] == post["body"]]
    assert relayed["answer_body"] == post["answer_body"], "the coordinator changed the member's sealed answer"
    leaked = [h for h in FORBIDDEN_AT_MEMBER if h in relayed["headers"]]
    assert not leaked, f"the member was sent {leaked}: {relayed['headers']}"
    assert set(relayed["headers"]) <= {"host", "accept", "accept-encoding", "connection", "user-agent", "content-type",
                                       "content-length"}, relayed["headers"]

    # nothing readable anywhere TheWall (or a wire in between) could look
    needles = [marker, prompt_with(marker)] + checks.windows(answer)
    assert_nothing_readable(w, needles, since=tap_before)
    assert w.real_home_untouched()


def test_with_coding_members_off_today_s_local_path_is_unchanged(main):
    w = main
    original = "true"
    try:
        w.set_coordinator_config(CODING_MEMBERS_ENABLED="false")
        status = w.coding_status()
        assert status["enabled"] is False
        assert w.route("x", "any") == {"route": None}
        before, posts_before, lookups_before = hits(w), post_count(w), route_lookups(w)
        marker = "lynx-" + secrets.token_hex(5)
        result = w.ask(prompt_with(marker))
        assert result["ev"] == "done" and result["text"].strip(), result
        assert hits(w) == before, "no member is used while coding members are off"
        assert route_lookups(w) == lookups_before, "the editor must not ask for a route from a coordinator that has none on"
        [post] = posts_since(w, posts_before)
        assert checks.envelope(post["body"]) is None
        body = json.loads(post["body"])
        assert body["messages"][-1]["content"] == prompt_with(marker), "today's request, unchanged"
        assert "x-dlne-privacy" not in post["headers"] and "x-dlne-route" not in post["headers"]
        assert post["headers"].get("x-dlne-session")
        assert post["answer_headers"].get("x-dlne-served") is None
        assert b'"choices"' in post["answer_body"]
    finally:
        w.set_coordinator_config(CODING_MEMBERS_ENABLED=original)
        wait_until(lambda: w.coding_status().get("ready", 0) >= 2, 60, "the members to be offered again")


def test_trusted_work_goes_only_to_listed_trust_ids_and_open_work_to_any_member(main):
    w = main
    held = []
    try:
        # privacy `trusted` (the editor's default): the listed member, again and again, and never B
        for _ in range(3):
            before = hits(w)
            result = w.ask("Say hello in one short sentence.")
            assert result["ev"] == "done", result
            assert served_by(before, hits(w)) == ["a"]

        # A has a free slot but is the busier of the two: a request that is only for trusted machines still goes to A
        held += w.hold(1, sealed=True, privacy="trusted")
        before = hits(w)
        result = w.ask("Say hello in one short sentence.")
        assert result["ev"] == "done", result
        assert served_by(before, hits(w)) == ["a"], "B is not listed in TRUSTED_MEMBERS and must get no trusted work"

        # A is now full. Work that is only for trusted machines does not spill onto B...
        held += w.hold(1, sealed=True, privacy="trusted")
        wait_until(lambda: w.coding_status().get("busy", 0) >= 2, 30, "both of A's slots to be taken")
        before, posts_before = hits(w), post_count(w)
        result = w.ask("Say hello in one short sentence.")
        assert result["ev"] == "done", result
        assert "b" not in served_by(before, hits(w)), "private work reached a machine that is not listed as trusted"
        [post] = posts_since(w, posts_before)
        assert post["answer_headers"].get("x-dlne-served") != "member"           # the coordinator's own engine answered
        assert post["headers"].get("x-dlne-privacy") == "trusted"

        # ...but work on a public repository (`open`) may go to any member: B has the free slots
        before, posts_before = hits(w), post_count(w)
        result = w.ask("Say hello in one short sentence.", **open_mode_kwargs(w))
        assert result["ev"] == "done", result
        assert served_by(before, hits(w)) == ["b"], "open work should reach the free, unlisted member"
        [post] = posts_since(w, posts_before)
        assert post["headers"].get("x-dlne-privacy") == "open"
    finally:
        for holder in held:
            holder.release()
        wait_until(lambda: w.coding_status().get("busy", 1) == 0, 90, "the members to free their slots")


def test_autocomplete_is_answered_by_a_member_as_one_sealed_object(main):
    w = main
    before, tap_before, posts_before = hits(w), len(w.client_tap.snapshot()), post_count(w)
    marker = "quokka_" + secrets.token_hex(5)
    run = w.client("fim", [{"op": "status", "id": "s"},
                           {"op": "complete", "id": "c", "prompt": f"def {marker}(a, b):\n    return ", "maxTokens": 16,
                            "sessionId": secrets.token_hex(8)}])
    run.wait(120)
    done = run.result("c")
    assert done["ev"] == "done" and done["text"].strip(), done
    assert served_by(before, hits(w)) == ["a"]
    [post] = posts_since(w, posts_before)
    assert post["line"].startswith("POST /v1/completions")
    assert checks.envelope(post["body"]) is not None
    inner = json.loads(post["answer_body"])
    assert list(inner) == ["dlne_e2e"] and inner["dlne_e2e"].get("n") == 0, "a whole answer is one sealed object, n = 0"
    assert_nothing_readable(w, [marker, f"def {marker}"] + checks.windows(done["text"], 8, 2), since=tap_before)


def test_a_sticky_session_returns_to_the_member_that_read_its_prompt(main):
    w = main
    session_a, session_b = secrets.token_hex(8), secrets.token_hex(8)
    settings = {"privacy": "anyMember"}               # either member may serve; the router chooses

    first_before = hits(w)
    first = w.ask("Say hello in one short sentence.", session=session_a, settings=settings)
    assert first["ev"] == "done", first
    [x] = served_by(first_before, hits(w))
    y = "b" if x == "a" else "a"

    # keep one of x's two slots taken, by the same session (it must go to x by stickiness)
    held_before = hits(w)
    holder = Holder(w, sealed=True, privacy="any")
    holder.session = session_a
    holder.start()
    try:
        assert holder.started.wait(60) and holder.error is None, holder.error
        wait_until(lambda: w.coding_status().get("busy", 0) >= 1, 30, "a slot to be taken")
        assert served_by(held_before, hits(w)) == [x], "the same session did not return to the member that served it"

        # a session the router has never seen goes to the member with the free slots...
        other_before = hits(w)
        other = w.ask("Say goodbye in one short sentence.", session=session_b, settings=settings)
        assert other["ev"] == "done", other
        assert served_by(other_before, hits(w)) == [y], "a new session should take the less busy member"

        # ...while the first session still goes to x, though y is the less busy one
        again_before = hits(w)
        again = w.ask("Say hello once more.", session=session_a, settings=settings)
        assert again["ev"] == "done", again
        assert served_by(again_before, hits(w)) == [x], "a sticky session must stay with its member while it has a free slot"
    finally:
        holder.release()
        wait_until(lambda: w.coding_status().get("busy", 1) == 0, 60, "the member to free its slot")


def test_with_no_member_that_fits_the_coordinators_own_engine_answers(main):
    w = main
    original = w.coordinator_config(trusted=[w.members["a"].trust_id])["TRUSTED_MEMBERS"]
    try:
        w.set_coordinator_config(TRUSTED_MEMBERS="")                    # nobody is trusted: private work has no member
        before, posts_before = hits(w), post_count(w)
        marker = "ibis-" + secrets.token_hex(5)
        result = w.ask(prompt_with(marker))
        assert result["ev"] == "done" and result["text"].strip(), result
        assert hits(w) == before
        [post] = posts_since(w, posts_before)
        assert post["answer_headers"].get("x-dlne-served") != "member"
        assert checks.envelope(post["body"]) is None, "with no member offered, the editor sends the plain request"
        body = json.loads(post["body"])
        assert body["messages"][-1]["content"] == prompt_with(marker)
        assert b'"choices"' in post["answer_body"]
    finally:
        w.set_coordinator_config(TRUSTED_MEMBERS=original)


def test_no_envelope_was_ever_sent_twice(main):
    """An envelope is sealed to one member and used once (contract section 6, item 6): over everything this world
    carried, no ephemeral key appears in two requests, on the editor's wire or in front of any member."""
    seen: Dict[str, int] = {}
    for post in checks.chat_posts(main.client_tap.snapshot()):
        inner = checks.envelope(post["body"])
        if inner:
            seen[inner["epk"]] = seen.get(inner["epk"], 0) + 1
    assert seen and max(seen.values()) == 1, f"an envelope was sent more than once: {seen}"
    for member in main.members.values():
        epks = [checks.envelope(p["body"])["epk"] for p in checks.chat_posts(member.connections())
                if checks.envelope(p["body"])]
        assert len(epks) == len(set(epks)), f"member {member.name} was sent the same envelope twice"


def test_everything_a_member_was_sent_or_sent_back_was_sealed(main):
    """Over the whole module: only envelopes ever reached a member, and no member-served answer was readable on the wire."""
    assert assert_member_traffic_sealed(main) >= 4


def test_a_member_turns_away_requests_that_do_not_come_from_its_coordinator(main):
    """SR-08: a page in the owner's browser (an Origin header, or a name that resolves to 127.0.0.1) cannot use the proxy.
    The coordinator's own requests, which carry Host 127.0.0.1:<relay port>, pass through the tunnel (scenario 1)."""
    import httpx
    a = main.members["a"]
    base = f"http://127.0.0.1:{a.proxy_port}"
    body = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 1}
    seen_before = len(a.connections())
    for headers in ({"Origin": "http://evil.example"}, {"Host": "evil.example"}, {"Host": "evil.example:80"}):
        refused = httpx.post(base + "/v1/chat/completions", json=body, headers=headers, trust_env=False, timeout=10)
        assert refused.status_code == 421, (headers, refused.status_code, refused.text[:200])
    assert httpx.get(base + "/health", headers={"Origin": "http://evil.example"}, trust_env=False, timeout=10).status_code == 421
    assert httpx.get(base + "/health", trust_env=False, timeout=10).status_code == 200
    assert len(a.connections()) == seen_before


# (Last in this module on purpose: every route lookup holds a slot on its member for 30 s, and the scenarios above
# need free slots.)
def test_a_member_with_a_damaged_identity_signature_is_never_routed_to(main):
    w = main
    c = w.members["c"]
    assert c.proc.alive(), "member C should be running (it serves, but its heartbeat cannot be believed)"
    before = hits(w)
    for _ in range(4):
        result = w.ask("Say hello in one short sentence.", settings={"privacy": "anyMember"})
        assert result["ev"] == "done", result
    assert hits(w)["c"] == before["c"] == 0, "a member whose identity signature does not verify was sent work"
    status = w.coding_status()
    assert status["ready"] == 2, status          # A and B are usable; C is not counted as ready
    assert status["slots"] == 4, status
    # (each route lookup holds a slot for 30 s, so only a few are made)
    for _ in range(3):
        assert w.route(secrets.token_hex(6), "any").get("route"), "members A and B are free; a route should be offered"
    assert hits(w)["c"] == 0 and not c.connections(), "the wire in front of C carried a request"
