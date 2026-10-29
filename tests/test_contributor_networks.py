"""
The contributing-networks book (engine/contributor_networks.py) and the rule it feeds (2.6.2): a phone's picture costs
a free-trial chat unless a computer is contributing from its network, now or in the last 24 hours.

The book's clock and file are injected, so nothing in the first half touches the installation's own files. The second
half is the coordinator: the heartbeat that feeds the book, the charge rule on both phone picture routes, the IDE side
that is left alone, and a restart that keeps what was learned.
"""

import inspect
import json
import os
import threading
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from engine import coordinator
from engine.contributor_networks import (FILE_VERSION, FORGET_AFTER_SECONDS, MAX_ID_CHARS, MAX_TRACKED,
                                         ContributorBook, _is_network)
from engine.version import VERSION
from tests.test_coordinator_counter import FakeRequest
from tests.test_image_endpoints import LOCAL, OTHER, REMOTE, client, finished, images, post  # noqa: F401 - fixtures

HOUR = 3600.0
DAY = 24 * HOUR
NET = "203.0.113.9"
OTHER_NET = "198.51.100.20"


class Clock:
    def __init__(self, now=1_700_000_000.0):
        self.t = now

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "config" / "contributor_networks.json")


def make(clock, path, **options):
    return ContributorBook(lambda: path, clock, **options)


def written(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


# ======================================================================================
# What counts as a network
# ======================================================================================

@pytest.mark.parametrize("value", ["203.0.113.9", "10.8.0.2", "2001:db8:1:2::/64", "fd00::/64", "2001:db8:ffff:ffff::/64"])
def test_what_trial_identity_makes_is_a_network(value):
    assert _is_network(value)


@pytest.mark.parametrize("value", [
    "", "unknown", "localhost", "not an address", "999.1.1.1", "203.0.113.9/24", "203.0.113.09", "203.0.113", " 203.0.113.9",
    "2001:db8:1:2::1", "2001:db8:1:2::1/64", "2001:db8:1:2::/48", "2001:0db8:1:2::/64", "2001:DB8:1:2::/64", "::ffff:203.0.113.9",
    "x" * (MAX_ID_CHARS + 1), None, 5, 2.5, True, ["203.0.113.9"], {"a": 1},
])
def test_anything_else_is_not(value):
    assert _is_network(value) is False


def test_what_trial_identity_makes_is_always_a_network():
    for address in ("203.0.113.9", "2001:db8:1:2:aaaa:bbbb:cccc:dddd", "::ffff:198.51.100.7", "2001:DB8::1"):
        assert _is_network(coordinator.trial_identity(address)), address


# ======================================================================================
# The book
# ======================================================================================

def test_a_network_is_a_contributor_from_its_heartbeat_for_a_day(clock, path):
    book = make(clock, path)
    assert book.is_contributor(NET) is False and len(book) == 0
    book.note(NET)
    assert book.is_contributor(NET) is True, "now"
    clock.t += 23 * HOUR
    assert book.is_contributor(NET) is True, "23 hours after its last heartbeat"
    clock.t += 1 * HOUR - 1
    assert book.is_contributor(NET) is True, "a second short of a day"
    clock.t += 1
    assert book.is_contributor(NET) is False, "a day"
    clock.t += 1 * HOUR
    assert book.is_contributor(NET) is False, "25 hours"


def test_the_day_is_counted_from_the_latest_heartbeat(clock, path):
    book = make(clock, path)
    book.note(NET)
    clock.t += 20 * HOUR
    book.note(NET)
    clock.t += 20 * HOUR
    assert book.is_contributor(NET) is True, "40 hours after the first heartbeat, 20 after the last"
    assert book.seen(NET) == clock.t - 20 * HOUR


def test_one_network_is_not_another(clock, path):
    book = make(clock, path)
    book.note(NET)
    assert book.is_contributor(OTHER_NET) is False and book.tracked() == {NET: clock.t}


@pytest.mark.parametrize("value", ["", "unknown", "localhost", "999.1.1.1", "2001:db8:1:2::1", None, 5, ["203.0.113.9"]])
def test_a_value_that_is_no_network_never_becomes_a_contributor(clock, path, value):
    book = make(clock, path)
    book.note(value)
    assert len(book) == 0 and book.is_contributor(value) is False


def test_a_back_of_the_clock_is_taken_as_now(clock, path):
    book = make(clock, path)
    book.note(NET)
    clock.t -= 5 * HOUR
    assert book.is_contributor(NET) is True, "the machine's clock was set back: the heartbeat is not in the future"
    clock.t += 5 * HOUR + 23 * HOUR
    assert book.is_contributor(NET) is True and book.seen(NET) < clock.t


def test_the_book_holds_ten_thousand_networks_by_default():
    assert MAX_TRACKED == 10_000 and ContributorBook().max_tracked == 10_000


def test_the_table_is_bounded_and_the_network_seen_longest_ago_goes_first(clock, path):
    book = make(clock, path, max_tracked=3)
    for name in ("10.0.0.1", "10.0.0.2", "10.0.0.3"):
        book.note(name)
        clock.t += 10
    book.note("10.0.0.1")                       # seen again: .2 is now the stalest
    clock.t += 10
    book.note("10.0.0.4")
    assert set(book.tracked()) == {"10.0.0.1", "10.0.0.3", "10.0.0.4"}
    for i in range(50):
        clock.t += 1
        book.note(f"10.1.0.{i}")
        assert len(book) <= 3


def test_networks_not_seen_for_a_day_are_dropped_before_a_live_one_when_full(clock, path):
    book = make(clock, path, max_tracked=2)
    book.note("10.0.0.1")
    clock.t += 2 * DAY
    book.note("10.0.0.2")
    clock.t += 10
    book.note("10.0.0.3")
    assert set(book.tracked()) == {"10.0.0.2", "10.0.0.3"}


def test_many_threads_may_note_at_once(clock, path):
    book = make(clock, path)
    errors = []

    def work(block):
        try:
            for i in range(200):
                book.note(f"10.{block}.0.{i}")
                book.is_contributor(f"10.{block}.0.{i}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors and len(book) == 8 * 200


# ======================================================================================
# The file
# ======================================================================================

def test_a_book_that_was_never_loaded_writes_nothing(clock, path):
    book = make(clock, path)
    book.note(NET)
    assert book.armed is False and book.flush() is False and book.save() is False
    assert not os.path.exists(path) and not os.path.exists(os.path.dirname(path))
    assert book.is_contributor(NET) is True, "it still remembers, in memory"


def test_what_is_saved_is_what_a_new_book_loads(clock, path):
    book = make(clock, path)
    assert book.load() == 0 and book.armed is True, "no file: no history, and writing is allowed from now on"
    book.note(NET)
    seen = clock.t
    clock.t += 20 * 60
    book.note("2001:db8:1:2::/64")
    assert book.flush() is True
    assert written(path) == {"version": FILE_VERSION, "saved_at": clock.t,
                             "networks": {NET: seen, "2001:db8:1:2::/64": clock.t}}, "addresses and times, nothing else"

    clock.t += 5 * 60                                        # the coordinator restarts
    reborn = make(clock, path)
    assert reborn.load() == 2
    assert reborn.is_contributor(NET) is True and reborn.seen(NET) == seen, "its day goes on from its last heartbeat"
    assert reborn.is_contributor("2001:db8:1:2::/64") is True


def test_a_restart_keeps_23_hours_and_forgets_25(clock, path):
    book = make(clock, path)
    book.load()
    book.note(NET)
    clock.t += 3 * HOUR
    book.note(OTHER_NET)
    book.flush()
    clock.t += 21 * HOUR                                     # 24 h since NET, 21 h since OTHER_NET
    reborn = make(clock, path)
    assert reborn.load() == 1, "the one a day old is not read"
    assert reborn.is_contributor(NET) is False and reborn.is_contributor(OTHER_NET) is True


def test_the_file_is_written_at_most_once_every_thirty_seconds(clock, path):
    book = make(clock, path)
    book.load()
    writes = []
    real = book.save
    book.save = lambda: writes.append(clock.t) or real()
    book.note("10.0.0.1")
    assert len(writes) == 1, "the first change is written at once"
    for i in range(20):
        clock.t += 1
        book.note(f"10.1.0.{i}")
    assert len(writes) == 1, "20 more networks in 20 seconds"
    clock.t += 15
    book.note("10.2.0.1")
    assert len(writes) == 2, "the next one after 30 seconds"
    assert "10.2.0.1" in written(path)["networks"] and "10.1.0.19" in written(path)["networks"]


def test_nothing_new_means_nothing_written_until_the_refresh(clock, path):
    book = make(clock, path)
    book.load()
    book.note(NET)
    first = os.stat(path).st_mtime_ns
    for _ in range(10):
        clock.t += 31
        book.note(NET)                                       # a worker's heartbeat, every few seconds, for ever
    assert os.stat(path).st_mtime_ns == first, "310 seconds of the same facts"
    clock.t += 300
    book.note(NET)
    assert written(path)["networks"][NET] == clock.t, "after ten minutes it is refreshed so the time stays true"


def test_a_network_back_after_a_day_is_written_at_once(clock, path):
    book = make(clock, path)
    book.load()
    book.note(NET)
    clock.t += 2 * DAY
    book.note(NET)
    assert written(path)["networks"][NET] == clock.t, "the file held a time more than a day old: wrong now"


def test_flush_writes_whatever_the_schedule_says(clock, path):
    book = make(clock, path)
    book.load()
    book.note("10.0.0.1")
    clock.t += 1
    book.note("10.0.0.2")
    assert "10.0.0.2" not in written(path)["networks"], "throttled"
    assert book.flush() is True and "10.0.0.2" in written(path)["networks"]


def test_networks_not_seen_for_a_day_are_not_loaded_and_not_saved(clock, path):
    book = make(clock, path)
    book.load()
    book.note("10.0.0.1")
    clock.t += 30 * HOUR
    book.note("10.0.0.2")
    book.flush()
    assert set(written(path)["networks"]) == {"10.0.0.2"}
    document = written(path)
    document["networks"]["10.0.0.9"] = clock.t - 2 * DAY
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle)
    reborn = make(clock, path)
    assert reborn.load() == 1 and set(reborn.tracked()) == {"10.0.0.2"}


def test_a_time_in_the_future_is_taken_as_now(clock, path):
    os.makedirs(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"version": 1, "networks": {NET: clock.t + 9 * HOUR}}, handle)
    book = make(clock, path)
    assert book.load() == 1 and book.seen(NET) == clock.t
    clock.t += 23 * HOUR
    assert book.is_contributor(NET) is True
    clock.t += 2 * HOUR
    assert book.is_contributor(NET) is False, "a day from now, not from the future"


def test_a_file_with_too_many_networks_keeps_the_most_recently_seen(clock, path):
    os.makedirs(os.path.dirname(path))
    networks = {f"10.0.0.{i}": clock.t - i for i in range(20)}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"version": 1, "networks": networks}, handle)
    book = make(clock, path, max_tracked=5)
    assert book.load() == 5 and set(book.tracked()) == {f"10.0.0.{i}" for i in range(5)}


@pytest.mark.parametrize("content", [
    b"", b"not json at all", b"\x00\xff\xfe garbage", b"{", b'{"version": 1, "networks": {"10.0.0.1":',
    b"[]", b"null", b"42", b'"text"', b'{"version": 2, "networks": {}}', b'{"networks": {}}', b'{"version": 1}',
    b'{"version": 1, "networks": []}', b'{"version": 1, "networks": "x"}', b'{"version": true, "networks": {}}',
])
def test_a_file_that_is_not_a_book_means_no_history(clock, path, content):
    os.makedirs(os.path.dirname(path))
    with open(path, "wb") as handle:
        handle.write(content)
    book = make(clock, path)
    assert book.load() == 0 and len(book) == 0 and book.armed is True
    book.note(NET)
    assert book.flush() is True
    assert list(written(path)["networks"]) == [NET], "and the next write puts a valid file in its place"


def test_an_entry_that_makes_no_sense_is_skipped_and_the_good_ones_stay(clock, path):
    os.makedirs(os.path.dirname(path))
    good = clock.t - 100
    networks = {
        NET: good,
        "not a network": good,
        "unknown": good,
        "": good,
        "10.0.0.2": "yesterday",
        "10.0.0.3": True,
        "10.0.0.4": -1,
        "10.0.0.5": float("nan"),
        "10.0.0.6": float("inf"),
        "10.0.0.7": [1, 2],
        "10.0.0.8": None,
        "10.0.0.9": {"seen": good},
        "x" * (MAX_ID_CHARS + 1): good,
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"version": 1, "networks": networks}, handle)         # json.dump writes NaN and Infinity
    book = make(clock, path)
    assert book.load() == 1 and book.tracked() == {NET: good}


def test_a_path_that_cannot_be_read_is_no_history(clock, tmp_path):
    folder = tmp_path / "folder"
    folder.mkdir()
    book = ContributorBook(lambda: str(folder), clock)
    assert book.load() == 0 and book.armed is True


def test_a_path_function_that_fails_is_no_history_and_no_write(clock):
    def broken():
        raise RuntimeError("no install directory")

    book = ContributorBook(broken, clock)
    assert book.load() == 0 and book.armed is True
    book.note(NET)
    assert book.flush() is False
    assert book.is_contributor(NET) is True, "it still remembers, in memory"


def test_a_failed_write_never_raises_leaves_the_old_file_and_is_tried_again(clock, path, monkeypatch):
    book = make(clock, path)
    book.load()
    book.note("10.0.0.1")
    before = written(path)

    def refuse(source, target):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", refuse)
    clock.t += 31
    book.note("10.0.0.2")                                    # the heartbeat is not affected
    monkeypatch.undo()
    assert written(path) == before, "the old file is intact"
    assert [name for name in os.listdir(os.path.dirname(path)) if name.endswith(".tmp")] == [], "no temp file left behind"
    clock.t += 31
    book.note("10.0.0.3")
    assert {"10.0.0.1", "10.0.0.2", "10.0.0.3"} <= set(written(path)["networks"]), "tried again at the next change"


def test_the_file_is_replaced_whole_never_written_in_place(clock, path, monkeypatch):
    book = make(clock, path)
    book.load()
    book.note("10.0.0.1")
    seen = []
    real = os.replace

    def spy(source, target):
        with open(source, encoding="utf-8") as handle:
            seen.append((os.path.dirname(source) == os.path.dirname(target), json.load(handle)))
        return real(source, target)

    monkeypatch.setattr(os, "replace", spy)
    clock.t += 31
    book.note("10.0.0.2")
    assert seen and seen[0][0] is True and set(seen[0][1]["networks"]) == {"10.0.0.1", "10.0.0.2"}


def test_loading_does_not_overwrite_what_this_process_has_already_seen(clock, path):
    os.makedirs(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"version": 1, "networks": {NET: clock.t - 5 * HOUR}}, handle)
    book = make(clock, path)
    book.note(NET)
    book.load()
    assert book.seen(NET) == clock.t


# ======================================================================================
# The coordinator: the heartbeat feeds the book
# ======================================================================================

def gpu_hw():
    return {"ram_gb": 32.0, "cpu_cores": 8, "cpu_threads": 16, "is_yielding": False,
            "gpu": {"has_gpu": True, "vram_total_gb": 24.0, "name": "RTX"}}


def cpu_hw(**changes):
    return {"ram_gb": 16.0, "cpu_cores": 8, "cpu_threads": 16, "gpu": None, "cpu_arch": "x86_64",
            "cpu_features": ["avx2", "fma"], "mem_bandwidth_gbs": 48.0, "is_yielding": False, **changes}


async def beat(worker_id, ip, hardware=None, version=VERSION):
    payload = coordinator.HeartbeatPayload(worker_id=worker_id, version=version, port=50053, token=f"tok-{worker_id}",
                                           hardware=hardware or gpu_hw())
    return await coordinator.worker_heartbeat(FakeRequest(client_host=ip), payload)


@pytest.fixture
def contributors(images, monkeypatch, tmp_path):
    """The coordinator with a book of its own (clock and file in the test's folder), heartbeats accepted without a key,
    and everything `images` sets up: a stand-in picture engine, a trial of 5 chats, 3 pictures an hour."""
    clock = Clock()
    file = str(tmp_path / "install" / "config" / "contributor_networks.json")
    book = ContributorBook(lambda: file, clock)
    monkeypatch.setattr(coordinator, "contributor_networks", book)
    images.limits.update({"network_key": "", "allow_unsigned_workers": True, "image_per_hour": 60, "image_queue_max": 16})
    return SimpleNamespace(images=images, clock=clock, book=book, file=file, quota=images.quota)


@pytest.mark.anyio
async def test_a_heartbeat_from_a_gpu_makes_its_network_a_contributor(contributors):
    assert (await beat("g1", NET))["status"] == "ok"
    assert contributors.book.tracked() == {NET: contributors.clock.t}


@pytest.mark.anyio
async def test_a_heartbeat_from_a_cpu_worker_does_too(contributors):
    await beat("c1", NET, cpu_hw())
    assert contributors.book.is_contributor(NET) is True


@pytest.mark.anyio
async def test_every_heartbeat_refreshes_the_time(contributors):
    await beat("g1", NET)
    contributors.clock.t += 40 * HOUR
    assert contributors.book.is_contributor(NET) is False
    await beat("g1", NET)
    assert contributors.book.is_contributor(NET) is True


@pytest.mark.anyio
async def test_the_key_is_the_free_trials_own_identity(contributors):
    await beat("g1", "2001:db8:1:2:aaaa:bbbb:cccc:dddd")
    await beat("g2", "::ffff:198.51.100.7")
    assert set(contributors.book.tracked()) == {"2001:db8:1:2::/64", "198.51.100.7"}


@pytest.mark.anyio
async def test_a_heartbeat_from_this_machine_itself_is_not_recorded(contributors):
    await beat("g1", "127.0.0.1")
    assert len(contributors.book) == 0, "its own clients are never charged: it says nothing about anyone else"


@pytest.mark.anyio
async def test_a_heartbeat_from_one_of_this_machines_own_addresses_is_not_recorded_either(contributors, monkeypatch):
    monkeypatch.setattr(coordinator, "is_local_ip", lambda ip: ip == "192.168.7.7")
    await beat("g1", "192.168.7.7")
    assert len(contributors.book) == 0
    await beat("g2", "192.168.7.8")
    assert set(contributors.book.tracked()) == {"192.168.7.8"}


@pytest.mark.anyio
@pytest.mark.parametrize("why", ["old version", "implausible hardware", "below the CPU floors"])
async def test_a_heartbeat_the_coordinator_refuses_makes_no_contributor(contributors, why):
    with pytest.raises(HTTPException):
        if why == "old version":
            await beat("g1", NET, version="0.0.1")
        elif why == "implausible hardware":
            await beat("g1", NET, {"ram_gb": -5, "gpu": None})
        else:
            await beat("c1", NET, cpu_hw(ram_gb=2.0, cpu_cores=1, mem_bandwidth_gbs=5.0))
    assert len(contributors.book) == 0


@pytest.mark.anyio
async def test_an_unsigned_heartbeat_is_refused_when_the_coordinator_has_a_key(contributors):
    contributors.images.limits.update({"network_key": "swarm-secret", "allow_unsigned_workers": False})
    with pytest.raises(HTTPException):
        await beat("g1", NET)
    assert len(contributors.book) == 0


@pytest.mark.anyio
async def test_the_heartbeat_answer_is_not_changed(contributors):
    assert await beat("g1", NET) == {"status": "ok"}


# ======================================================================================
# The charge rule: POST /v1/app/images (the dialog; the chat tool has the same rule, tests/test_phone_pictures.py)
# ======================================================================================

NEW_WORDING = ("Your free trial is used up. Pictures stay free on any network where a computer contributes to DLNE "
               "(now or in the last day).")


@pytest.mark.anyio
async def test_a_network_with_a_live_worker_is_never_charged(contributors):
    await beat("g1", NET)
    async with client(REMOTE) as c:
        for _ in range(3):
            assert (await post(c)).status_code == 202
    assert contributors.quota.summary(5)["chats_used"] == 0 and contributors.quota.summary(5)["clients"] == 0


@pytest.mark.anyio
async def test_a_contributors_network_is_free_even_when_its_trial_was_already_used_up(contributors):
    for _ in range(5):
        contributors.quota.check_and_consume(NET, limit=5)         # the owner's IDE chats spent them all
    async with client(REMOTE) as c:
        assert (await post(c)).status_code == 429, "before anyone contributes: the wall"
        await beat("g1", NET)
        created = await post(c)
        assert created.status_code == 202
        done = await finished(c, "/v1/app/images", created.json()["id"])
    assert done["status"] == "completed"
    assert contributors.quota.summary(5)["chats_used"] == 5, "nothing was spent, nothing was given back"


@pytest.mark.anyio
async def test_23_hours_after_the_last_heartbeat_is_free_and_25_is_charged(contributors):
    await beat("g1", NET)
    contributors.clock.t += 23 * HOUR
    async with client(REMOTE) as c:
        assert (await post(c)).status_code == 202
        assert contributors.quota.summary(5)["chats_used"] == 0
        contributors.clock.t += 2 * HOUR
        assert (await post(c)).status_code == 202
    assert contributors.quota.summary(5)["chats_used"] == 1, "25 hours: a free-trial chat, as in 2.6.1"


@pytest.mark.anyio
async def test_another_network_is_charged_as_before(contributors):
    await beat("g1", NET)
    async with client(OTHER) as c:
        assert (await post(c)).status_code == 202
    assert contributors.quota.peek(OTHER[0], limit=5) == (True, 1, 4)
    assert contributors.quota.peek(NET, limit=5) == (True, 0, 5)


@pytest.mark.anyio
async def test_a_used_up_trial_says_the_new_words_and_when_to_come_back(contributors):
    contributors.images.limits["starter_quota_inferences"] = 1
    async with client(OTHER) as c:
        assert (await post(c)).status_code == 202
        reply = await post(c)
    assert reply.status_code == 429 and reply.json() == {"detail": NEW_WORDING, "code": "image_quota"}
    assert reply.headers["retry-after"] == "3600" and reply.headers["cache-control"] == "no-store"
    assert "Contributing compute" not in reply.text


@pytest.mark.anyio
async def test_one_ipv6_block_is_one_contributor_network(contributors):
    await beat("g1", "2001:db8:1:2::1")
    async with client(("2001:db8:1:2:ffff::9", 1)) as same_block, client(("2001:db8:1:3::1", 1)) as other_block:
        assert (await post(same_block)).status_code == 202
        assert contributors.quota.summary(5)["chats_used"] == 0
        assert (await post(other_block)).status_code == 202
    assert contributors.quota.summary(5)["chats_used"] == 1


@pytest.mark.anyio
async def test_local_clients_stay_uncharged_and_stay_out_of_the_book(contributors):
    async with client(LOCAL) as c:
        assert (await post(c)).status_code == 202
    assert contributors.quota.summary(5)["chats_used"] == 0 and len(contributors.book) == 0


@pytest.mark.anyio
async def test_a_heartbeat_from_this_machine_does_not_free_every_client_of_that_address(contributors):
    await beat("g1", "127.0.0.1")
    async with client(OTHER) as c:
        assert (await post(c)).status_code == 202
    assert contributors.quota.summary(5)["chats_used"] == 1


@pytest.mark.anyio
async def test_the_contribution_rule_off_charges_nobody_as_before(contributors):
    contributors.images.limits["enforce_contrib_clause"] = False
    async with client(OTHER) as c:
        assert (await post(c)).status_code == 202
    assert contributors.quota.summary(5)["chats_used"] == 0


@pytest.mark.anyio
async def test_the_hourly_cap_still_applies_to_a_contributor(contributors):
    contributors.images.limits["image_per_hour"] = 2
    await beat("g1", NET)
    async with client(REMOTE) as c:
        assert (await post(c)).status_code == 202 and (await post(c)).status_code == 202
        capped = await post(c)
    assert capped.status_code == 429 and capped.json()["code"] == "image_quota" and "2 pictures an hour" in capped.json()["detail"]


@pytest.mark.anyio
async def test_the_ide_side_is_unchanged_a_contributors_network_still_spends_the_trial(contributors):
    await beat("g1", NET)
    async with client(REMOTE) as c:
        assert (await post(c, "/v1/images")).status_code == 202
        assert contributors.quota.summary(5)["chats_used"] == 1
        contributors.images.limits["starter_quota_inferences"] = 1
        wall = await post(c, "/v1/images")
    assert wall.status_code == 403 and "starter quota" in wall.json()["detail"].lower()


# ======================================================================================
# A restart keeps what was learned
# ======================================================================================

def restart(contributors, monkeypatch):
    """A clean shutdown saves the book; a new process starts with a new book and loads it."""
    contributors.book.flush()
    book = ContributorBook(lambda: contributors.file, contributors.clock)
    monkeypatch.setattr(coordinator, "contributor_networks", book)
    contributors.book = book
    return coordinator._load_contributor_networks()


@pytest.mark.anyio
async def test_a_restart_keeps_a_contributors_network_free(contributors, monkeypatch):
    coordinator._load_contributor_networks()
    await beat("g1", NET)
    contributors.clock.t += 20 * HOUR
    assert restart(contributors, monkeypatch) == 1
    contributors.clock.t += 3 * HOUR                          # 23 hours since the worker was last heard
    async with client(REMOTE) as c:
        assert (await post(c)).status_code == 202
        assert contributors.quota.summary(5)["chats_used"] == 0
        contributors.clock.t += 2 * HOUR                      # 25
        assert (await post(c)).status_code == 202
    assert contributors.quota.summary(5)["chats_used"] == 1


@pytest.mark.anyio
async def test_without_the_file_a_restart_would_forget_the_network(contributors, monkeypatch):
    """The control: what the same steps do when nothing was saved."""
    await beat("g1", NET)
    monkeypatch.setattr(coordinator, "contributor_networks", ContributorBook(lambda: contributors.file, contributors.clock))
    async with client(REMOTE) as c:
        assert (await post(c)).status_code == 202
    assert contributors.quota.summary(5)["chats_used"] == 1


@pytest.mark.anyio
async def test_the_real_shutdown_saves_the_book_and_the_real_startup_reads_it(contributors, monkeypatch):
    coordinator._load_contributor_networks()
    await beat("g1", NET)
    contributors.clock.t += 10
    await beat("g2", OTHER_NET)                                # throttled: not in the file yet
    assert OTHER_NET not in written(contributors.file)["networks"]
    await coordinator.shutdown_event()
    assert set(written(contributors.file)["networks"]) == {NET, OTHER_NET}
    reborn = ContributorBook(lambda: contributors.file, contributors.clock)
    monkeypatch.setattr(coordinator, "contributor_networks", reborn)
    assert coordinator._load_contributor_networks() == 2


@pytest.mark.parametrize("content", [b"", b"{", b"\xff\xfe\x00 not json", b"[]", b'{"version": 1, "networks": 5}'])
@pytest.mark.anyio
async def test_a_damaged_file_means_no_history_and_never_stops_the_coordinator(contributors, content):
    os.makedirs(os.path.dirname(contributors.file))
    with open(contributors.file, "wb") as handle:
        handle.write(content)
    assert coordinator._load_contributor_networks() == 0
    await beat("g1", NET)
    await coordinator.shutdown_event()
    assert list(written(contributors.file)["networks"]) == [NET], "and the file is whole again"


@pytest.mark.anyio
async def test_nothing_is_written_until_the_coordinator_has_started(contributors):
    await beat("g1", NET)
    contributors.clock.t += 31
    await beat("g1", NET)
    await coordinator.shutdown_event()
    assert not os.path.exists(os.path.dirname(contributors.file)), "a book that was never loaded writes nothing"


def test_the_coordinator_loads_the_book_at_start_and_saves_it_at_shutdown():
    assert "_load_contributor_networks()" in inspect.getsource(coordinator.startup_event)
    assert "contributor_networks.flush()" in inspect.getsource(coordinator.shutdown_event)


def test_the_real_book_keeps_its_file_in_the_installation_config_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(coordinator, "get_base_dir", lambda: str(tmp_path))
    assert coordinator.contributor_networks.path_fn() == os.path.join(str(tmp_path), "config", "contributor_networks.json")


def test_the_real_book_is_the_one_the_standard_day_and_size_describe():
    assert coordinator.contributor_networks.forget_after == FORGET_AFTER_SECONDS == DAY
    assert coordinator.contributor_networks.max_tracked == 10_000
    assert coordinator.contributor_networks.save_every == 30.0
