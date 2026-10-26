"""
The standing-by book (engine/standby.py): when each GPU worker began standing by, kept in a small JSON file so a
coordinator restart does not hand every idle GPU a fresh grace period.

The clock and the file are injected; nothing here touches the installation's own files.
"""

import json
import os
import threading

import pytest

from engine.standby import FILE_VERSION, FORGET_AFTER_SECONDS, MAX_ID_CHARS, StandbyBook

HOUR = 3600.0
DAY = 24 * HOUR


class Clock:
    def __init__(self, now=1_000_000.0):
        self.t = now

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "config" / "gpu_standby.json")


def make(clock, path, **options):
    return StandbyBook(lambda: path, clock, **options)


def written(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


# ======================================================================================
# The clock
# ======================================================================================

def test_a_worker_with_no_job_starts_a_clock_at_first_sight_and_keeps_it(clock, path):
    book = make(clock, path)
    assert book.note("w1", False) == clock.t
    started = clock.t
    clock.t += 600
    assert book.note("w1", False) == started, "later sightings do not restart it"
    assert book.since("w1") == started and book.tracked() == {"w1": started}


def test_a_job_clears_the_clock_and_the_next_idle_sighting_starts_a_new_one(clock, path):
    book = make(clock, path)
    book.note("w1", False)
    clock.t += 600
    assert book.note("w1", True) is None and book.since("w1") is None and len(book) == 0
    clock.t += 600
    assert book.note("w1", False) == clock.t


def test_a_worker_with_a_job_never_gets_an_entry(clock, path):
    book = make(clock, path)
    assert book.note("busy", True) is None and len(book) == 0


def test_a_worker_not_seen_for_a_day_starts_again_like_a_new_one(clock, path):
    book = make(clock, path)
    book.note("w1", False)
    clock.t += FORGET_AFTER_SECONDS - 1
    assert book.note("w1", False) == 1_000_000.0, "seen within the day: the clock goes on"
    clock.t += FORGET_AFTER_SECONDS
    assert book.note("w1", False) == clock.t, "gone for a day: no history"


def test_a_clock_set_back_does_not_leave_a_start_in_the_future(clock, path):
    book = make(clock, path)
    book.note("w1", False)
    clock.t -= 5 * HOUR
    assert book.note("w1", False) == clock.t


def test_the_table_is_bounded_and_the_worker_seen_longest_ago_goes_first(clock, path):
    book = make(clock, path, max_tracked=3)
    for name in ("a", "b", "c"):
        book.note(name, False)
        clock.t += 10
    book.note("a", False)                       # a is seen again: b is now the stalest
    clock.t += 10
    book.note("d", False)
    assert set(book.tracked()) == {"a", "c", "d"}
    for i in range(50):
        clock.t += 1
        book.note(f"n{i}", False)
        assert len(book) <= 3


def test_entries_not_seen_for_a_day_are_dropped_before_a_live_one_when_full(clock, path):
    book = make(clock, path, max_tracked=2)
    book.note("old", False)
    clock.t += 2 * DAY
    book.note("recent", False)
    clock.t += 10
    book.note("new", False)
    assert set(book.tracked()) == {"recent", "new"}


def test_many_threads_may_note_at_once(clock, path):
    book = make(clock, path)
    errors = []

    def work(prefix):
        try:
            for i in range(200):
                book.note(f"{prefix}-{i}", i % 5 == 0)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(f"t{n}",)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors and len(book) == 8 * 160


# ======================================================================================
# The file
# ======================================================================================

def test_a_book_that_was_never_loaded_writes_nothing(clock, path):
    book = make(clock, path)
    book.note("w1", False)
    assert book.armed is False and book.flush() is False and book.save() is False
    assert not os.path.exists(path) and not os.path.exists(os.path.dirname(path))


def test_what_is_saved_is_what_a_new_book_loads(clock, path):
    book = make(clock, path)
    assert book.load() == 0 and book.armed is True, "no file: no history, and writing is allowed from now on"
    book.note("idle", False)
    started = clock.t
    clock.t += 20 * 60
    book.note("idle", False)
    book.note("busy", True)
    assert book.flush() is True
    document = written(path)
    assert document["version"] == FILE_VERSION and document["saved_at"] == clock.t
    assert document["workers"] == {"idle": {"since": started, "seen": clock.t}}, "only a worker with no job has an entry"

    clock.t += 5 * 60                                    # the coordinator restarts
    reborn = make(clock, path)
    assert reborn.load() == 1
    assert reborn.since("idle") == started, "the grace goes on from where it was"
    assert reborn.note("idle", False) == started


def test_a_restart_during_the_grace_does_not_give_a_fresh_one(clock, path):
    grace = 30 * 60
    book = make(clock, path)
    book.load()
    book.note("idle", False)
    clock.t += 25 * 60
    book.flush()
    clock.t += 10                                        # down for ten seconds
    reborn = make(clock, path)
    reborn.load()
    clock.t += 6 * 60
    assert clock.t - reborn.note("idle", False) >= grace, "31 minutes since it began standing by"


def test_the_file_is_written_at_most_once_every_thirty_seconds(clock, path):
    book = make(clock, path)
    book.load()
    writes = []
    real = book.save
    book.save = lambda: writes.append(clock.t) or real()
    book.note("w1", False)
    assert len(writes) == 1, "the first change is written at once"
    for i in range(20):
        clock.t += 1
        book.note(f"x{i}", False)
    assert len(writes) == 1, "20 more changes in 20 seconds"
    clock.t += 15
    book.note("late", False)
    assert len(writes) == 2, "the next one after 30 seconds"
    assert "late" in written(path)["workers"] and "x19" in written(path)["workers"]


def test_nothing_changed_means_nothing_written_until_the_refresh(clock, path):
    book = make(clock, path)
    book.load()
    book.note("w1", False)
    first = os.stat(path).st_mtime_ns
    for _ in range(10):
        clock.t += 31
        book.note("w1", False)
    assert os.stat(path).st_mtime_ns == first, "310 seconds of the same facts"
    clock.t += 300
    book.note("w1", False)
    assert written(path)["workers"]["w1"]["seen"] == clock.t, "after ten minutes it is refreshed so seen stays true"


def test_flush_writes_whatever_the_schedule_says(clock, path):
    book = make(clock, path)
    book.load()
    book.note("w1", False)
    clock.t += 1
    book.note("w2", False)
    assert "w2" not in written(path)["workers"], "throttled"
    assert book.flush() is True and "w2" in written(path)["workers"]


def test_entries_not_seen_for_a_day_are_not_loaded_and_not_saved(clock, path):
    book = make(clock, path)
    book.load()
    book.note("gone", False)
    book.note("here", False)
    book.flush()
    clock.t += 30 * HOUR
    book.note("here", False)
    book.flush()
    assert set(written(path)["workers"]) == {"here"}
    document = written(path)
    document["workers"]["ancient"] = {"since": clock.t - 3 * DAY, "seen": clock.t - 2 * DAY}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle)
    reborn = make(clock, path)
    assert reborn.load() == 1 and set(reborn.tracked()) == {"here"}


def test_a_start_in_the_future_is_taken_as_now(clock, path):
    os.makedirs(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"version": 1, "workers": {"w": {"since": clock.t + 9 * HOUR, "seen": clock.t + 9 * HOUR}}}, handle)
    book = make(clock, path)
    assert book.load() == 1 and book.since("w") == clock.t


def test_a_file_with_too_many_workers_keeps_the_most_recently_seen(clock, path):
    os.makedirs(os.path.dirname(path))
    workers = {f"w{i}": {"since": clock.t - 100 - i, "seen": clock.t - i} for i in range(20)}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"version": 1, "workers": workers}, handle)
    book = make(clock, path, max_tracked=5)
    assert book.load() == 5 and set(book.tracked()) == {f"w{i}" for i in range(5)}


GOOD = {"since": 999_000.0, "seen": 999_900.0}


@pytest.mark.parametrize("content", [
    b"", b"not json at all", b"\x00\xff\xfe garbage", b"{", b'{"version": 1, "workers": {"a": {"since":',
    b"[]", b"null", b"42", b'"text"', b'{"version": 2, "workers": {}}', b'{"workers": {}}', b'{"version": 1}',
    b'{"version": 1, "workers": []}', b'{"version": 1, "workers": "x"}', b'{"version": true, "workers": {}}',
])
def test_a_file_that_is_not_a_book_means_no_history(clock, path, content):
    os.makedirs(os.path.dirname(path))
    with open(path, "wb") as handle:
        handle.write(content)
    book = make(clock, path)
    assert book.load() == 0 and len(book) == 0 and book.armed is True
    book.note("w1", False)
    assert book.flush() is True
    assert list(written(path)["workers"]) == ["w1"], "and the next write puts a valid file in its place"


def test_an_entry_that_makes_no_sense_is_skipped_and_the_good_ones_stay(clock, path):
    os.makedirs(os.path.dirname(path))
    workers = {
        "good": GOOD,
        "no-seen": {"since": 5.0},
        "no-since": {"seen": 5.0},
        "string": {"since": "1", "seen": "2"},
        "bool": {"since": True, "seen": True},
        "negative": {"since": -1, "seen": -1},
        "nan": {"since": float("nan"), "seen": float("nan")},
        "inf": {"since": float("inf"), "seen": float("inf")},
        "list": [1, 2],
        "null": None,
        "x" * (MAX_ID_CHARS + 1): GOOD,
        "": GOOD,
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"version": 1, "workers": workers}, handle)         # json.dump writes NaN and Infinity
    book = make(clock, path)
    assert book.load() == 1 and book.tracked() == {"good": GOOD["since"]}


def test_a_path_that_cannot_be_read_is_no_history(clock, tmp_path):
    folder = tmp_path / "folder"
    folder.mkdir()
    book = StandbyBook(lambda: str(folder), clock)
    assert book.load() == 0 and book.armed is True


def test_a_path_function_that_fails_is_no_history_and_no_write(clock):
    def broken():
        raise RuntimeError("no install directory")

    book = StandbyBook(broken, clock)
    assert book.load() == 0 and book.armed is True
    assert book.note("w1", False) == clock.t and book.flush() is False
    assert book.since("w1") == clock.t, "the clock still works in memory"


def test_a_failed_write_never_raises_leaves_the_old_file_and_is_tried_again(clock, path, monkeypatch):
    book = make(clock, path)
    book.load()
    book.note("w1", False)
    before = written(path)

    def refuse(source, target):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", refuse)
    clock.t += 31
    assert book.note("w2", False) == clock.t, "the heartbeat's answer is not affected"
    monkeypatch.undo()
    assert written(path) == before, "the old file is intact"
    assert [name for name in os.listdir(os.path.dirname(path)) if name.endswith(".tmp")] == [], "no temp file left behind"
    clock.t += 31
    book.note("w3", False)
    assert {"w1", "w2", "w3"} <= set(written(path)["workers"]), "tried again at the next change"


def test_the_file_is_replaced_whole_never_written_in_place(clock, path, monkeypatch):
    book = make(clock, path)
    book.load()
    book.note("w1", False)
    seen = []
    real = os.replace

    def spy(source, target):
        with open(source, encoding="utf-8") as handle:
            seen.append((os.path.dirname(source) == os.path.dirname(target), json.load(handle)))
        return real(source, target)

    monkeypatch.setattr(os, "replace", spy)
    clock.t += 31
    book.note("w2", False)
    assert seen and seen[0][0] is True and set(seen[0][1]["workers"]) == {"w1", "w2"}


def test_loading_does_not_overwrite_what_this_process_has_already_seen(clock, path):
    os.makedirs(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"version": 1, "workers": {"w1": {"since": clock.t - 3000, "seen": clock.t - 5}}}, handle)
    book = make(clock, path)
    started = book.note("w1", False)
    assert started == clock.t
    book.load()
    assert book.since("w1") == started
