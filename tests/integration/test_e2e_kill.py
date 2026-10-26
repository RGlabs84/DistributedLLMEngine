"""
Members that go away (docs/coding-members.md sections 6 and 10): a member killed mid-stream, and a member whose model
server died. World: three ordinary members; the client's privacy mode is `anyMember` so any of them may serve.
"""

import secrets
from typing import Dict

import pytest

from tests.integration import checks
from tests.integration.helpers import assert_member_traffic_sealed, build, hits, post_count, posts_since, served_by
from tests.integration.world import wait_until

pytestmark = pytest.mark.integration

COUNTING = "Count from 1 to 400, one number per line, and write nothing else."


@pytest.fixture(scope="module")
def kill(resources, run_root):
    world = build(run_root, resources, "kill", [{"name": "k1"}, {"name": "k2"}, {"name": "k3"}])
    yield world
    world.stop()


def test_a_member_killed_mid_stream_is_reported_as_cut_and_the_next_request_reroutes_with_a_fresh_seal(kill):
    w = kill
    settings = {"privacy": "anyMember"}
    session = secrets.token_hex(8)
    before, posts_before = hits(w), post_count(w)
    run = w.client("cut", [{"op": "status", "id": "s"},
                           {"op": "chat", "id": "c", "text": COUNTING, "sessionId": session}], settings=settings)
    run.wait_for(lambda events: sum(1 for e in events if e["ev"] == "delta") >= 5, 120, "the answer to start")
    [victim] = served_by(before, hits(w))
    w.members[victim].kill()                                   # the volunteer's machine goes away mid-answer
    run.wait(90)
    result = run.result("c")

    # reported, not hidden and not quietly redone: part of the answer was on screen, so it is an error that says so
    assert result["ev"] == "error", result
    assert result["name"] == "MemberAnswerError" and result["partial"] is True, result
    assert "cut" in result["message"] or "stopped" in result["message"], result["message"]
    assert result["shown"].strip(), "what had arrived before the cut stays on the user's screen"
    [first] = posts_since(w, posts_before)                     # the request was not sent a second time
    first_epk = checks.envelope(first["body"])["epk"]

    # the very next request asks for a route again, seals again, and is answered by a member that is still there
    others = {n for n in w.members if n != victim}
    wait_until(lambda: w.coding_status().get("ready", 0) <= len(w.members) - 1, 60, "the coordinator to notice")
    before, posts_before = hits(w), post_count(w)
    again = w.ask(COUNTING.replace("400", "20"), session=session, settings=settings)
    assert again["ev"] == "done" and again["text"].strip(), again
    [served] = served_by(before, hits(w))
    assert served in others and served != victim
    [second] = posts_since(w, posts_before)
    assert checks.envelope(second["body"])["epk"] != first_epk, "a sealed request must never be sent again"
    assert second["headers"]["x-dlne-route"] != first["headers"]["x-dlne-route"]


def test_a_member_whose_engine_died_is_skipped_in_the_same_call_and_the_answer_still_arrives(kill):
    w = kill
    alive = [n for n, m in w.members.items() if m.proc.alive()]
    assert len(alive) >= 2
    target = alive[0]
    original = w.coordinator_config()["TRUSTED_MEMBERS"]
    try:
        w.set_trusted(target)                                  # only this member may be offered for private work
        wait_until(lambda: w.route(secrets.token_hex(4), "trusted").get("route"), 30, "the member to be offered")
        w.members[target].kill_engine_only()                   # the proxy stays up; its model server is gone
        before, posts_before = hits(w), post_count(w)
        result = w.ask("Say hello in one short sentence.")
        assert result["ev"] == "done" and result["text"].strip(), result
        posts = posts_since(w, posts_before)
        sealed = [p for p in posts if checks.envelope(p["body"])]
        assert sealed, "the first attempt should have been sealed to the member"
        epks = [checks.envelope(p["body"])["epk"] for p in sealed]
        assert len(epks) == len(set(epks)), "a retry must be sealed afresh"
        assert any("did not answer" in (e.get("line") or "") for e in result["run"].of("progress")), \
            "the user should be told the member did not answer: " + str([e.get("line") for e in result["run"].of("progress")])
    finally:
        w.set_coordinator_config(TRUSTED_MEMBERS=original)


def test_everything_the_kill_world_members_were_sent_or_sent_back_was_sealed(kill):
    assert assert_member_traffic_sealed(kill) >= 1


def test_every_envelope_in_the_kill_world_was_sent_once(kill):
    seen: Dict[str, int] = {}
    for post in checks.chat_posts(kill.client_tap.snapshot()):
        inner = checks.envelope(post["body"])
        if inner:
            seen[inner["epk"]] = seen.get(inner["epk"], 0) + 1
    assert seen and max(seen.values()) == 1, seen
