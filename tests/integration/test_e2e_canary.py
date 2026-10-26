"""
Canaries and flags (docs/coding-members.md section 8). World: S is a snooping volunteer (it reads what it is sent and
uses a decoy credential if it finds one); N is honest. Only a member that served traffic recently is sent a canary.
"""

import base64
import json
import secrets
from typing import Any, Dict

import pytest

from tests.integration import checks
from tests.integration.helpers import build
from tests.integration.world import World, wait_until

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def canary_world(resources, run_root):
    world = build(run_root, resources, "canary", [{"name": "s", "snoop": "use"}, {"name": "n"}], start_later=("n",),
                  ready=1, canary_per_day=3)
    state: Dict[str, Any] = {"world": world}
    try:
        settings = {"privacy": "anyMember"}
        real = world.ask("Say hello in one short sentence.", session="canary-real-session", settings=settings)
        assert real["ev"] == "done", real
        state["real_session"] = "canary-real-session"
        # A route to S, and a request sealed for it, kept for after S is flagged (an in-flight request of a member that
        # is cut off). S is the only member ready, so the route is S's.
        state["pre_offer"] = world.route("pre-flag-session", "any")
        assert state["pre_offer"].get("route"), state["pre_offer"]
        state["pre_envelope"] = world.seal_for(state["pre_offer"])
        world.start_member("n")
        world.wait_members_ready(2)
        state["s_requests_before"] = len(world.members["s"].completions())
        state["n_requests_before"] = len(world.members["n"].completions())
        (world.coord_install / "canary_go").write_text("go")           # the canary scheduler may send now
        wait_until(lambda: len(world.members["s"].completions()) > state["s_requests_before"], 90,
                   "a canary to reach the member that served traffic")
        (world.coord_install / "canary_go").unlink()
        wait_until(lambda: world.canaries().get("flagged"), 60, "the snooping member to be flagged")
        s_member = world.members["s"]
        wait_until(lambda: b"data: [DONE]" in bytes(checks.chat_posts(s_member.connections())[-1]["conn"].s2c), 420,
                   "the canary's answer to run to its end")
        state["canary_view"] = world.canaries()
        yield state
    except BaseException:
        print(world.diagnostics())
        raise
    finally:
        world.stop()


def test_a_canary_reaches_the_member_sealed_and_looks_like_any_other_request(canary_world):
    w: World = canary_world["world"]
    s, n = w.members["s"], w.members["n"]
    assert len(n.completions()) == canary_world["n_requests_before"], "only a member that served traffic is sent a canary"
    posts = checks.chat_posts(s.connections())
    real, canary = posts[0], posts[canary_world["s_requests_before"]]
    mine = checks.envelope(canary["body"])
    theirs = checks.envelope(real["body"])
    assert mine is not None, "a canary must be sealed exactly like a real request"
    assert set(mine) == set(theirs) == {"v", "epk", "nonce", "ct"} and mine["v"] == theirs["v"] == 1
    assert canary["line"] == real["line"]
    assert set(canary["headers"]) == set(real["headers"]), (canary["headers"], real["headers"])
    assert canary["headers"]["content-type"] == real["headers"]["content-type"]
    for label, inner in (("canary", mine), ("real request", theirs)):
        raw = base64.b64decode(inner["ct"])
        assert (len(raw) - 16) % 256 == 0, f"the {label} is not padded to a multiple of 256 bytes before sealing"
    # the answer has the same shape too: sealed frames, a sealed end, then the plain [DONE]
    answer_lines = checks.sse_data_lines(canary["answer_body"])
    assert answer_lines, (canary["answer_status"], canary["answer_headers"], bytes(canary["conn"].s2c)[:400])
    frames = [json.loads(x) for x in answer_lines[:-1]]
    assert answer_lines[-1] == "[DONE]"
    assert frames and all(list(f) == ["dlne_e2e"] for f in frames)
    assert "rs" in frames[0]["dlne_e2e"]
    # the member did read a decoy in it (that is what the snooper was looking for) and the coordinator saw it come back
    # (SR-01 to SR-03) a canary is link-only: no `dlne_ck_` key in any prompt, where a key would be it says [REDACTED]
    snooped = s.snooped()
    assert snooped and snooped[-1]["links"], snooped
    assert not snooped[-1]["keys"], f"a canary carried a decoy key: {snooped[-1]}"
    assert snooped[-1]["redacted"] >= 1, f"no [REDACTED] where a key would be: {snooped[-1]}"
    view = canary_world["canary_view"]
    assert view["flagged"] and view["flagged"][0]["channels"] == ["url"], view      # caught by the link, never by a key


def test_a_flagged_member_stops_getting_traffic(canary_world):
    w: World = canary_world["world"]
    s, n = w.members["s"], w.members["n"]
    view = w.canaries()
    assert [f["trust_id"] for f in view["flagged"]] == [s.trust_id], view
    assert view["sent"]["total"] >= 1 and view["sent"]["members"] == 1, view
    s_before, n_before = len(s.completions()), len(n.completions())
    sessions = [canary_world["real_session"]] + [secrets.token_hex(8) for _ in range(5)]
    for session in sessions:
        result = w.ask("Say hello in one short sentence.", session=session, settings={"privacy": "anyMember"})
        assert result["ev"] == "done", result
    assert len(s.completions()) == s_before, "a flagged member was sent work (its old session included)"
    assert len(n.completions()) == n_before + len(sessions)
    for _ in range(2):          # N has two slots, and every route lookup holds one for 30 s
        assert w.route(secrets.token_hex(4), "any").get("route")

    # The flagged member is cut off by routing, while it is still running and still heartbeating (it is not "unassigned"):
    # 1. a request that was sealed for it before the flag, with the route it was given, is refused and goes nowhere
    s_requests = len(s.completions())
    refused = w.post_sealed(canary_world["pre_offer"], canary_world["pre_envelope"], "pre-flag-session")
    assert refused.status_code == 503, (refused.status_code, refused.text[:300])
    assert refused.json()["error"]["code"] == "dlne_reroute", refused.text[:300]
    assert len(s.completions()) == s_requests, "a request sealed for a flagged member reached it"
    assert s.proc.alive() and w.status()["coding_members"]["members"] >= 2, "S should still be a running, listed worker"
    # 2. with the honest member gone too, no route is offered at all and the work falls to the coordinator's own engine in plain
    n.kill()
    wait_until(lambda: w.coding_status().get("ready", 1) == 0, 60, "no member to be offered any more")
    assert w.route(secrets.token_hex(4), "any") == {"route": None}
    s_requests = len(s.completions())
    posts_before = len(checks.chat_posts(w.client_tap.snapshot()))
    result = w.ask("Say hello in one short sentence.", settings={"privacy": "anyMember"})
    assert result["ev"] == "done" and result["text"].strip(), result
    [post] = checks.chat_posts(w.client_tap.snapshot())[posts_before:]
    assert checks.envelope(post["body"]) is None, "with no member offered the editor sends the plain request"
    assert post["answer_headers"].get("x-dlne-served") != "member"
    assert len(s.completions()) == s_requests, "the flagged member was sent work"
