"""
A deploy restarts the coordinator. A member that is already serving must not be told it has no job.
"""

import time

import pytest

from tests.integration.helpers import build

pytestmark = pytest.mark.integration


def test_a_coordinator_restart_does_not_make_serving_members_unload_their_model(resources, run_root):
    """Releases restart the coordinator. A member that is already serving must keep its model loaded and be offered
    again as soon as its heartbeat and tunnel are back; it must not be told, for the minute and a half the coordinator
    waits to measure workers it has not seen yet, that it has no job."""
    world = build(run_root, resources, "restart", [{"name": "m"}], role_grace=None)       # the real 90 s window
    try:
        member = world.members["m"]
        pids = member.llama_pids()
        assert pids, "the member should be serving"
        world.restart_coordinator(role_grace=None)
        started = time.monotonic()
        samples = []
        offered_again = None
        while time.monotonic() - started < 150:
            ready = world.coding_status().get("ready", 0)
            samples.append((round(time.monotonic() - started), ready, member.llama_pids()))
            if ready >= 1 and offered_again is None:
                offered_again = time.monotonic() - started
            time.sleep(3)
            if offered_again is not None and time.monotonic() - started > offered_again + 20:
                break
        assert offered_again is not None, f"the member was never offered again: {samples}"
        # (a heartbeat every 5 s, the tunnel redialled every 3 s: a few beats, not the 90 s a machine nobody knows waits)
        assert offered_again < 25, f"the member took {offered_again:.0f} s to be offered again: {samples}"
        assert member.llama_pids() == pids, f"the member unloaded and reloaded its model: {samples}"
    finally:
        world.stop()
