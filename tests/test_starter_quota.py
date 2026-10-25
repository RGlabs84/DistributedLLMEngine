"""
Tests for the free starter trial in ``engine.coordinator``.

The trial exists so a newcomer can try the swarm before contributing compute. Ghost-text
autocomplete hits ``/v1/completions`` after every typing pause, so it must never be charged
against the trial, while chat requests must be.
"""

from types import SimpleNamespace

from engine import coordinator


def _request(path: str):
    return SimpleNamespace(url=SimpleNamespace(path=path), state=SimpleNamespace())


def _manager(tmp_path):
    # An absolute path keeps the usage file out of the real install (see dlne-test-isolation).
    return coordinator.StarterQuotaManager(str(tmp_path / "starter_quota.json"), default_quota=3)


def test_default_trial_is_200_chat_requests(tmp_path):
    assert coordinator.StarterQuotaManager(str(tmp_path / "q.json")).default_quota == 200


def test_peek_never_spends_quota(tmp_path):
    mgr = _manager(tmp_path)
    for _ in range(10):
        assert mgr.peek("ip", limit=3) == (True, 0, 3)
    assert mgr.check_and_consume("ip", limit=3) == (True, 1, 2)


def test_peek_reports_exhaustion(tmp_path):
    mgr = _manager(tmp_path)
    for _ in range(3):
        mgr.check_and_consume("ip", limit=3)
    assert mgr.peek("ip", limit=3) == (False, 3, 0)


def test_chat_spends_quota_and_autocomplete_does_not(tmp_path, monkeypatch):
    monkeypatch.setattr(coordinator, "starter_quota_mgr", _manager(tmp_path))

    for _ in range(50):
        allowed, remaining = coordinator.use_starter_quota(_request("/v1/completions"), "ip", 3)
        assert allowed and remaining == 3

    allowed, remaining = coordinator.use_starter_quota(_request("/v1/chat/completions"), "ip", 3)
    assert allowed and remaining == 2


def test_autocomplete_is_refused_once_chat_trial_is_spent(tmp_path, monkeypatch):
    monkeypatch.setattr(coordinator, "starter_quota_mgr", _manager(tmp_path))
    for _ in range(3):
        coordinator.use_starter_quota(_request("/v1/chat/completions"), "ip", 3)

    allowed, remaining = coordinator.use_starter_quota(_request("/v1/completions"), "ip", 3)
    assert not allowed and remaining == 0
    allowed, _ = coordinator.use_starter_quota(_request("/v1/chat/completions"), "ip", 3)
    assert not allowed


def test_a_failed_save_leaves_the_previous_file_intact(tmp_path, monkeypatch):
    """
    The save used to truncate the file in place; dying halfway left unparseable JSON, and
    the next start loaded that as "nobody has used anything", resetting every trial.
    """
    mgr = _manager(tmp_path)
    mgr.check_and_consume("ip", limit=3)
    path = tmp_path / "starter_quota.json"
    before = path.read_text()

    def dump_then_die(obj, f, **kwargs):
        f.write('{"ip": ')
        raise OSError("disk full")

    monkeypatch.setattr(coordinator.json, "dump", dump_then_die)
    mgr.check_and_consume("ip", limit=3)
    monkeypatch.undo()

    assert path.read_text() == before
    assert _manager(tmp_path).peek("ip", limit=3) == (True, 1, 2)


def test_remaining_count_is_stashed_for_the_header(tmp_path, monkeypatch):
    monkeypatch.setattr(coordinator, "starter_quota_mgr", _manager(tmp_path))
    req = _request("/v1/chat/completions")
    coordinator.use_starter_quota(req, "ip", 3)
    assert req.state.starter_quota_remaining == 2
