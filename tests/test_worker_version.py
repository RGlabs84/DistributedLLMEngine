"""
What a worker does when the coordinator runs a different engine version.

The old self-updater (consent prompts, staged downloads, a swap-and-relaunch script) is gone.
The coordinator is the version authority, and the launcher (the VS Code extension) brings a
worker's engine in line. So the worker's job is small and these tests pin it exactly:

* behind the coordinator -> end with the dedicated exit code that means "restart me after the
  engine has been updated";
* ahead of the coordinator -> keep waiting (it is probably mid-restart), never downgrade;
* credentials rejected -> still fatal, with the ordinary failure code.
"""

import httpx
import pytest

from engine import worker
from engine.version import VERSION

PROFILE = {
    "is_yielding": False, "ram_gb": 32.0, "cpu_cores": 8, "cpu_threads": 16,
    "gpu": {"vram_total_gb": 8.0, "name": "Test GPU", "api": "vulkan"},
}


@pytest.fixture
def node(monkeypatch):
    monkeypatch.setattr(worker, "get_hardware_profile", lambda: dict(PROFILE))
    monkeypatch.setattr(worker, "_write_token_to_disk", lambda token: None)   # never touch the real install
    monkeypatch.setattr(worker, "AHEAD_OF_COORDINATOR_RETRY_SECONDS", 0.01)
    monkeypatch.setattr(worker, "HEARTBEAT_INTERVAL_SECONDS", 0.01)
    n = worker.WorkerNode("http://coordinator:11435")
    n.is_running = True
    return n


def _respond(node, monkeypatch, replies):
    """Make each heartbeat answer with the next scripted (status, headers, json) reply."""
    posts = []

    def fake_post(url, json=None, timeout=None):
        posts.append(url)
        index = min(len(posts), len(replies)) - 1
        status, headers, body = replies[index]
        request = httpx.Request("POST", url)
        response = httpx.Response(status, headers=headers, json=body, request=request)
        if len(posts) >= len(replies) + 2:
            node.request_shutdown(0, "test finished")       # safety stop: never loop forever
        return response

    monkeypatch.setattr(worker.httpx, "post", fake_post)
    return posts


def test_behind_the_coordinator_the_worker_exits_asking_to_be_restarted(node, monkeypatch):
    posts = _respond(node, monkeypatch, [(426, {"X-DLNE-Required-Version": "99.0.0"}, {"detail": "out of date"})])
    node.heartbeat_loop()
    assert node.exit_code == worker.VERSION_MISMATCH_EXIT_CODE == 75
    assert len(posts) == 1, "a refused build must not keep hammering the coordinator"


def test_the_mismatch_exit_code_is_distinct_from_an_ordinary_failure():
    assert worker.VERSION_MISMATCH_EXIT_CODE != worker.FATAL_EXIT_CODE
    assert worker.VERSION_MISMATCH_EXIT_CODE not in (0, 1)


def test_an_unreadable_requirement_still_ends_with_the_restart_code(node, monkeypatch):
    posts = _respond(node, monkeypatch, [(426, {}, {"detail": "out of date"})])
    node.heartbeat_loop()
    assert node.exit_code == 75 and len(posts) == 1


def test_a_garbage_requirement_is_treated_as_behind_not_ahead(node, monkeypatch):
    posts = _respond(node, monkeypatch, [(426, {"X-DLNE-Required-Version": "not-a-version"}, None)])
    node.heartbeat_loop()
    assert node.exit_code == 75 and len(posts) == 1


def test_ahead_of_the_coordinator_it_waits_instead_of_exiting_or_downgrading(node, monkeypatch):
    older = "0.0.1"
    assert worker.is_older(older, VERSION)
    posts = _respond(node, monkeypatch, [
        (426, {"X-DLNE-Required-Version": older}, None),
        (426, {"X-DLNE-Required-Version": older}, None),
        (200, {}, {"status": "ok"}),                      # the coordinator caught up
    ])
    node.heartbeat_loop()
    assert len(posts) >= 3, "it must keep trying while the coordinator is older"
    assert node.exit_code == 0, "being ahead is not a failure and must not end the node"


def test_the_same_version_is_not_a_mismatch(node, monkeypatch):
    posts = _respond(node, monkeypatch, [(200, {}, {"status": "ok"})])
    node.heartbeat_loop()
    assert node.exit_code == 0 and len(posts) >= 1


def test_rejected_credentials_are_still_fatal_with_the_ordinary_code(node, monkeypatch):
    posts = _respond(node, monkeypatch, [(401, {}, {"detail": "bad signature"})])
    node.heartbeat_loop()
    assert node.exit_code == worker.FATAL_EXIT_CODE and len(posts) == 1


def test_an_old_style_rollout_directive_in_a_reply_is_ignored(node, monkeypatch):
    """A coordinator that still sends update_directive must not make a worker prompt or stage anything."""
    posts = _respond(node, monkeypatch, [
        (200, {}, {"status": "ok", "update_directive": {"version": "9.9.9", "notes": "x"}}),
    ])
    node.heartbeat_loop()
    assert node.exit_code == 0 and len(posts) >= 1
    assert not hasattr(node, "_surface_update_offer")


def test_the_self_updater_is_really_gone_from_the_worker():
    for name in ("release_updater", "_surface_update_offer", "_consent_watcher", "_apply_and_exit",
                 "_proactive_update_check", "UPDATE_PROACTIVE_CHECK_SECONDS"):
        assert not hasattr(worker, name) and not hasattr(worker.WorkerNode, name), name


def test_the_heartbeat_wire_format_is_unchanged(node, isolated_config, dlne_root, monkeypatch):
    """role and staged_version stay in the signed payload so older/newer ends still agree on the MAC."""
    # The signature needs a NETWORK_KEY. The test brings its own, in a throwaway root: reading the real
    # config/config.txt made this fail in every checkout that has none (a fresh worktree).
    monkeypatch.setattr(worker, "get_base_dir", lambda: str(dlne_root))
    isolated_config.write_text("NETWORK_KEY=throwaway-test-key\n", encoding="utf-8")
    payload = node._build_heartbeat_payload()
    assert payload["version"] == VERSION
    assert payload["role"] == "worker" and payload["staged_version"] == ""
    assert payload["signature"] and payload["nonce"] and payload["timestamp"]
