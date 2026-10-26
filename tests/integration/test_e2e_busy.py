"""
Every slot busy (docs/coding-members.md section 10, "The busy answer"): the client must show where it stands and then
get its answer. World: one member (two slots) and the coordinator's own engine (two slots), all four taken.
"""

import secrets

import pytest

from tests.integration.helpers import build
from tests.integration.world import wait_until

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def busy(resources, run_root):
    world = build(run_root, resources, "busy", [{"name": "m"}])
    yield world
    world.stop()


def test_when_every_slot_is_busy_the_client_shows_its_place_in_line_and_the_answer_comes_when_one_frees(busy):
    w = busy
    members = w.hold(2, sealed=True, privacy="any")            # both of the member's slots
    locals_ = w.hold(2, sealed=False, privacy="any")           # and both of the coordinator's own engine
    try:
        for holder in members + locals_:
            assert holder.error is None, holder.error
        wait_until(lambda: w.coding_status().get("busy", 0) >= 2, 60, "the member's slots to be taken")
        wait_until(lambda: (w.status() or {}).get("coding_queue", {}).get("in_use", 0) >= 2, 60,
                   "the coordinator's own slots to be taken")

        run = w.client("queued", [{"op": "status", "id": "s"},
                                  {"op": "chat", "id": "c", "text": "Say hello in one short sentence.",
                                   "sessionId": secrets.token_hex(8)}],
                       settings={"privacy": "anyMember"}, wait_timing={"firstMs": 800, "everyMs": 1000})
        run.wait_for(lambda events: any(e["ev"] == "queue" and e.get("queue") for e in events), 90,
                     "the client to be told where it stands")
        shown = [e["queue"] for e in run.of("queue") if e.get("queue")]
        assert shown[0].get("position", 0) >= 1 or shown[0].get("waiting", 0) >= 1, shown[0]
        assert not run.of("delta"), "no answer can have started while every slot is taken"
        assert not run.of("done", "c")

        # a slot frees (a local one): the request is answered
        locals_[0].release()
        run.wait(120)
        result = run.result("c")
        assert result["ev"] == "done" and result["content"].strip(), result
        assert run.of("queue")[-1].get("queue") is None, "the place in line is taken down once the answer starts"
    finally:
        for holder in members + locals_:
            holder.release()
        wait_until(lambda: w.coding_status().get("busy", 1) == 0, 90, "the member to free its slots")
