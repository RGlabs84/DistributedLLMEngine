"""
Coding chats keep their lane (2.7.0). Before, every idle coding chat took lane 0, so two conversations taking
turns (the owner's two editors on TheWall, 2026-10-30) each found the other's prompt in the lane's cache and the
engine read a 90k-token conversation again from the start, minutes every time. Now a chat goes back to the lane
its conversation used last, and a chat with no lane of its own takes the one a chat used longest ago.
"""

from types import SimpleNamespace

import pytest

from engine import coordinator
from engine.priority import LaneAffinity, LaneBook

SESSION_A = "3f2b9c1e-6a47-4d52-9a0e-1b7c5d8e2f41"
SESSION_B = "9d8c7b6a-5f4e-4d3c-8b2a-1f0e9d8c7b6a"
SNAPSHOT = {"phone_slots": 2, "dynamic_slots": 4}


@pytest.fixture
def lanes(monkeypatch):
    book, affinity = LaneBook(), LaneAffinity()
    monkeypatch.setattr(coordinator, "lane_book", book)
    monkeypatch.setattr(coordinator, "lane_affinity", affinity)
    return book


def _request(session=None):
    return SimpleNamespace(headers={"x-dlne-session": session} if session else {})


def _chat(session, snapshot=SNAPSHOT):
    body = {"id_slot": 3}
    lane = coordinator._pin_lane(body, snapshot, key=coordinator._session_lane_key(_request(session)))
    assert body["id_slot"] == lane
    return lane


def test_two_conversations_taking_turns_keep_a_lane_each(lanes):
    seen = []
    for session in [SESSION_A, SESSION_B] * 4:
        lane = _chat(session)
        seen.append(lane)
        lanes.give(lane)            # each answer ends before the other editor asks

    assert seen[0::2] == [seen[0]] * 4, "conversation A always comes back to its lane"
    assert seen[1::2] == [seen[1]] * 4, "conversation B always comes back to its lane"
    assert seen[0] != seen[1]


def test_a_busy_lane_sends_its_conversation_to_another_and_it_stays_there_next_time(lanes):
    a = _chat(SESSION_A)                       # A is still answering
    b = _chat(SESSION_B)
    assert b != a
    lanes.give(a)
    lanes.give(b)

    assert _chat(SESSION_B) == b


def test_a_new_conversation_takes_the_lane_used_longest_ago_not_lane_0(lanes):
    for session in (SESSION_A, SESSION_B):
        lanes.give(_chat(session))             # A on 0, B on 1

    third = _chat("0b1c2d3e-4f50-4617-8a9b-0c1d2e3f4a5b")

    assert third not in (0, 1), "lanes 2 and 3 hold nobody's prompt"


def test_without_a_session_header_a_chat_still_spreads_and_nothing_is_remembered(lanes):
    first = _chat(None)
    lanes.give(first)
    second = _chat(None)

    assert first != second
    assert coordinator.lane_affinity._lanes == {}


def test_the_lane_is_remembered_under_a_tag_never_the_session_id(lanes):
    lanes.give(_chat(SESSION_A))

    keys = list(coordinator.lane_affinity._lanes)
    assert len(keys) == 1 and keys[0].startswith("code:")
    assert SESSION_A not in keys[0]


def test_a_malformed_session_header_counts_as_none():
    assert coordinator._session_lane_key(_request("not-a-uuid")) is None
    assert coordinator._session_lane_key(None) is None


def test_autocomplete_is_unchanged_and_does_not_move_a_chat_off_its_lane(lanes):
    a = _chat(SESSION_A)
    lanes.give(a)
    short = coordinator._pin_lane({}, SNAPSHOT, short=True)
    lanes.give(short, short=True)

    assert _chat(SESSION_A) == a


def test_without_phone_lanes_the_body_is_untouched(lanes):
    body = {"id_slot": 1}
    assert coordinator._pin_lane(body, {"phone_slots": 0, "dynamic_slots": 2}, key="code:x") is None
    assert body == {"id_slot": 1}


def test_the_book_without_spread_still_takes_the_lowest_lane():
    book = LaneBook()
    lane = book.take(0, 4)
    book.give(lane)

    assert book.take(0, 4) == 0, "the phone path and the benchmark keep their old order"
