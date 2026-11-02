"""Trial events, stats and the burst warning (2.9.0 section 6d, engine/trial_watch.py)."""

import json
import os

from engine import coordinator
from engine.trial_watch import TrialWatch


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def make(tmp_path, clock=None):
    logs, warns = [], []
    watch = TrialWatch(lambda: str(tmp_path), logs.append, warns.append, clock or Clock())
    return watch, logs, warns


def test_each_event_is_logged_once_with_a_masked_network(tmp_path):
    watch, logs, _ = make(tmp_path)
    net = "203.0.113.77"
    for used in range(1, 11):
        watch.record(net, used - 1, used, 10)
    for used in range(1, 11):          # a refund and a second pass logs nothing new
        watch.record(net, used - 1, used, 10)
    kinds = [l.split(":")[0] for l in logs]
    assert kinds == ["Trial start", "Trial half", "Trial used up"]
    assert not any(net in l for l in logs) and not any("203.0.113" in l for l in logs)
    tags = {l.rsplit(" ", 1)[1] for l in logs}
    assert len(tags) == 1 and watch.mask(net).rstrip(".") in tags.pop()


def test_the_mask_is_keyed_stable_and_the_key_is_kept_in_config_and_never_logged(tmp_path):
    watch, logs, _ = make(tmp_path)
    tag = watch.mask("198.51.100.4")
    assert tag == watch.mask("198.51.100.4") and tag != watch.mask("198.51.100.5") and "198.51" not in tag
    key_file = tmp_path / "trial_key"
    assert key_file.exists() and oct(key_file.stat().st_mode & 0o777) == "0o600"
    again, _, _ = make(tmp_path)
    assert again.mask("198.51.100.4") == tag, "the same key is read back"
    watch.record("198.51.100.4", 0, 1, 10)
    assert key_file.read_text().strip() not in " ".join(logs)
    assert "198.51.100.4" not in (tmp_path / "trial_events.json").read_text()


def test_the_stats_count_starts_used_up_and_the_busiest_hour(tmp_path):
    clock = Clock()
    watch, _, _ = make(tmp_path, clock)
    for i in range(3):
        watch.record(f"10.0.0.{i}", 0, 1, 4)
    watch.record("10.0.0.0", 1, 4, 4)           # crosses half and uses up in one step
    clock.t += 3 * 3600
    watch.record("10.0.1.1", 0, 1, 4)
    clock.t += 3601
    stats = watch.stats()
    assert stats["started_week"] == 4 and stats["used_up_week"] == 1
    assert stats["busiest_hour"]["fresh"] == 3
    assert stats["fresh_last_hour"] == 0 and stats["burst"] is False
    assert stats["burst_limit_per_hour"] == 10


def test_burst_warns_once_past_the_limit_and_not_below(tmp_path, monkeypatch):
    monkeypatch.setattr("engine.trial_watch.burst_limit", lambda: 3)
    watch, _, warns = make(tmp_path)
    for i in range(3):
        watch.record(f"10.0.0.{i}", 0, 1, 100)
    assert warns == [] and watch.stats()["burst"] is False
    watch.record("10.0.0.9", 0, 1, 100)
    watch.record("10.0.0.10", 0, 1, 100)
    assert len(warns) == 1 and "4 fresh networks" in warns[0]
    assert watch.stats()["burst"] is True


def test_events_survive_a_restart_without_being_logged_again(tmp_path):
    watch, logs, _ = make(tmp_path)
    watch.record("10.1.1.1", 0, 1, 10)
    again, logs2, _ = make(tmp_path)
    again.record("10.1.1.1", 0, 1, 10)
    assert len(logs) == 1 and logs2 == []
    assert again.stats()["started_week"] == 1


def test_the_quota_manager_feeds_the_watch_next_to_its_own_record(tmp_path):
    mgr = coordinator.StarterQuotaManager(str(tmp_path / "starter_quota.json"), default_quota=2)
    mgr.check_and_consume("203.0.113.5", limit=2)
    mgr.check_and_consume("203.0.113.5", limit=2)
    mgr.check_and_consume("203.0.113.5", limit=2)     # refused: nothing more to log
    events = json.loads((tmp_path / "trial_events.json").read_text())
    assert [e["kind"] for e in events] == ["start", "half", "used_up"]
    assert "203.0.113.5" not in json.dumps(events)
    assert mgr.watch.stats()["used_up_week"] == 1
