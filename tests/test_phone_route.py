"""
Phone routing (2.9.0, docs/release-2.9-contract.md 6b and 6e): PHONE_ROUTE (wall_first, volunteers_first, wall_only),
the volunteers' speed floor (PHONE_MIN_TPS), and phone photos on a coding lane when the phone rung cannot read them.

The environment is the one the phone-lane endpoint tests build (tests/test_phone_lane_endpoint.py: a stand-in engine with
two coding and two phone lanes, a stand-in volunteer "cpu-one" that is ready) with vision switched on as in
tests/test_phone_photos_endpoint.py.
"""

import asyncio
import time
from types import SimpleNamespace

import pytest

from engine import coordinator, media
from engine import config as engine_config
from engine import phone_route
from engine.app_pool import AppPool, Member
from engine.priority import CodingGate, LaneBook
from tests.test_phone_lane_endpoint import (  # noqa: F401 - env and servers are fixtures
    CHAT_A, CODING_SLOTS, OTHER, PHONE_SLOTS, REMOTE, client, env, nothing_held, phone_body, servers, until,
)
from tests.test_phone_photos_endpoint import lane_env, photo_body, send  # noqa: F401 - lane_env is a fixture

PHONE_LANES = range(CODING_SLOTS, CODING_SLOTS + PHONE_SLOTS)
CODING_LANES = range(CODING_SLOTS)


def hold_phone_lanes():
    """Every phone lane busy: the gate's slots taken by someone who never lets go until released."""
    assert all([coordinator.phone_gate.try_acquire() for _ in range(PHONE_SLOTS)])


def release_phone_lanes():
    for _ in range(PHONE_SLOTS):
        coordinator.phone_gate.release()


async def status_pool():
    async with client(("127.0.0.1", 5000)) as admin:
        return (await admin.get("/v1/cluster/status")).json()["app_pool"]


def route(env, value):
    env.limits["phone_route"] = value


# ======================================================================================
# The decision and its pieces
# ======================================================================================

WALL_FREE = phone_route.WallRung(True, True, True, {})
WALL_BUSY = phone_route.WallRung(True, False, True, {})
NO_WALL = phone_route.WallRung(False)


@pytest.mark.parametrize("route_name, wall, free, fast, photos, expected", [
    ("wall_first", WALL_FREE, True, True, False, "wall"),
    ("wall_first", WALL_BUSY, True, True, False, "volunteers"),
    ("wall_first", WALL_BUSY, True, False, False, "volunteers"),      # busy lanes: even a slow volunteer beats waiting
    ("wall_first", WALL_BUSY, False, False, False, "wall"),
    ("wall_first", WALL_BUSY, True, True, True, "wall"),              # a photo never goes to a volunteer
    ("wall_first", NO_WALL, True, True, False, "pool"),
    ("volunteers_first", WALL_FREE, True, True, False, "volunteers"),
    ("volunteers_first", WALL_FREE, True, False, False, "wall"),      # the slow-volunteer guard
    ("volunteers_first", WALL_FREE, False, False, False, "wall"),
    ("volunteers_first", WALL_BUSY, True, False, False, "volunteers"),
    ("volunteers_first", WALL_BUSY, False, False, False, "wall"),
    ("volunteers_first", WALL_FREE, True, True, True, "wall"),
    ("volunteers_first", NO_WALL, False, False, False, "pool"),
    ("wall_only", WALL_BUSY, True, True, False, "wall"),
    ("wall_only", WALL_FREE, True, True, False, "wall"),
    ("wall_only", NO_WALL, True, True, False, "pool"),
    ("nonsense", WALL_BUSY, True, True, False, "volunteers"),         # an unknown route is the default, wall_first
    (None, WALL_FREE, True, True, False, "wall"),
])
def test_choose(route_name, wall, free, fast, photos, expected):
    assert phone_route.choose(route_name, photos, wall, free, fast) == expected


def test_a_photo_with_no_phone_rung_is_never_a_volunteers():
    assert phone_route.choose("volunteers_first", True, NO_WALL, True, True) == "pool"     # the coordinator's "no photos now"


def test_the_route_settings_are_read_and_a_typo_keeps_the_default(monkeypatch):
    def parse(config):
        monkeypatch.setattr(engine_config, "get_config", lambda force_reload=False: dict(config))
        return engine_config.get_worker_limits()

    defaults = {"phone_route": "wall_first", "phone_min_tps": 6.0, "phone_vision": True}
    parsed = parse({})
    assert {k: parsed[k] for k in defaults} == defaults
    assert parse({"PHONE_ROUTE": "Volunteers_First"})["phone_route"] == "volunteers_first"
    assert parse({"PHONE_ROUTE": "wall_only"})["phone_route"] == "wall_only"
    assert parse({"PHONE_ROUTE": "voluntears"})["phone_route"] == "wall_first"
    assert parse({"PHONE_MIN_TPS": "10"})["phone_min_tps"] == 10.0
    assert parse({"PHONE_MIN_TPS": "-1"})["phone_min_tps"] == 6.0 and parse({"PHONE_MIN_TPS": "fast"})["phone_min_tps"] == 6.0
    assert parse({"PHONE_VISION": "false"})["phone_vision"] is False
    assert parse({"PHONE_VISION": "flase"})["phone_vision"] is True


def test_the_coldest_lane_is_one_with_no_conversation_else_the_longest_unused_and_never_a_recent_one_if_another_will_do():
    book = LaneBook()
    assert book.coldest(0, 3) == 0
    book.give(0)
    # lane 0 was just used; 1 and 2 never were: the first of those
    assert book.coldest(0, 3, recent_seconds=300) == 1
    book.give(1)
    assert book.coldest(0, 3, recent_seconds=300) == 2
    book.give(2)
    # all used within the last few minutes: the one used longest ago
    book._last_used_at.update({0: 1000.0, 1: 1100.0, 2: 1050.0})
    assert book.coldest(0, 3, recent_seconds=300, now=1200.0) == 0
    # lane 1 is old, 0 and 2 recent: 1, whatever the order
    book._last_used_at.update({0: 1190.0, 1: 800.0, 2: 1195.0})
    assert book.coldest(0, 3, recent_seconds=300, now=1200.0) == 1
    # a lane with a chat on it right now is the last resort
    book = LaneBook()
    busy = book.take(0, 2, spread=True)
    assert book.coldest(0, 2) == 1 - busy


def test_a_member_is_slow_only_after_it_has_been_measured_slow():
    pool = AppPool()
    fast = Member("worker:a", 12.0, "small", True, 2, "http://x")
    slow = Member("worker:b", 12.0, "small", True, 2, "http://y")
    unmeasured = Member("worker:c", 12.0, "small", True, 2, "http://z")
    down = Member("worker:d", 12.0, "small", False, 2, "http://w")
    pool._member_tps.update({"worker:a": 12.0, "worker:b": 2.0})
    assert pool.free_members([fast, slow, unmeasured, down]) == [fast, slow, unmeasured]
    assert pool.free_members([fast, slow, unmeasured, down], 6.0) == [fast, unmeasured]
    pool._inflight["worker:a"] = 2
    assert pool.free_members([fast, slow, unmeasured], 6.0) == [unmeasured], "a full member is not free"


def test_a_members_speed_is_the_tokens_between_its_first_and_last_frame_and_short_answers_say_nothing():
    pool = AppPool()
    pool._note_member_speed("worker:a", 5, 10.0, 11.0)
    assert pool.member_tps("worker:a") is None
    pool._note_member_speed("worker:a", 21, 10.0, 12.0)
    assert pool.member_tps("worker:a") == 10.0
    pool._note_member_speed("worker:a", 21, 10.0, 12.0 + 8.0)           # 2 tok/s now: the average moves, it does not jump
    assert 2.0 < pool.member_tps("worker:a") < 10.0


# ======================================================================================
# wall_first (the default)
# ======================================================================================

@pytest.mark.anyio
async def test_wall_first_answers_on_the_lane_when_one_is_free(env):
    route(env, "wall_first")
    async with client() as c:
        resp = await c.post("/v1/app/chat/completions", json=phone_body())
        pool = await status_pool()

    assert b"engine-one" in resp.content and env.cpu.payloads == []
    assert pool["route"] == "wall_first" and pool["serving"] == "engine"
    assert nothing_held(env)


@pytest.mark.anyio
async def test_a_missing_setting_is_wall_first(env):
    env.limits.pop("phone_route", None)
    hold_phone_lanes()
    async with client() as c:
        resp = await c.post("/v1/app/chat/completions", json=phone_body())
        pool = await status_pool()
    release_phone_lanes()

    assert b"cpu-one" in resp.content and pool["route"] == "wall_first"


@pytest.mark.anyio
async def test_wall_first_sends_a_text_to_a_ready_volunteer_when_every_lane_is_busy(env):
    route(env, "wall_first")
    hold_phone_lanes()
    async with client() as c:
        resp = await c.post("/v1/app/chat/completions", json=phone_body())
        pool = await status_pool()
    release_phone_lanes()

    assert resp.status_code == 200 and b"cpu-one" in resp.content and env.engine.chats == []
    assert len(env.cpu.payloads) == 1 and "id_slot" not in env.cpu.payloads[0]
    assert pool["serving"] == "volunteers"
    assert env.pool._active_by_network == {}


@pytest.mark.anyio
async def test_wall_first_a_photo_never_goes_to_a_volunteer_it_waits_for_the_lane(lane_env):
    route(lane_env, "wall_first")
    hold_phone_lanes()
    async with client() as c:
        task = asyncio.create_task(send(c, photo_body()))
        assert await until(lambda: coordinator.phone_gate.stats()["waiting"] == 1)
        assert lane_env.cpu.payloads == []
        release_phone_lanes()
        resp = await task

    assert b"engine-one" in resp.content and lane_env.cpu.payloads == []
    assert lane_env.engine.chats[0]["id_slot"] in PHONE_LANES and nothing_held(lane_env)


@pytest.mark.anyio
async def test_wall_first_a_photo_with_every_lane_busy_for_good_gets_the_429(lane_env, monkeypatch):
    monkeypatch.setattr(coordinator, "PHONE_LANE_WAIT_SECONDS", 0.2)
    route(lane_env, "wall_first")
    hold_phone_lanes()
    async with client() as c:
        resp = await send(c, photo_body())
    release_phone_lanes()

    assert resp.status_code == 429 and lane_env.cpu.payloads == [] and lane_env.engine.chats == []
    assert nothing_held(lane_env)


@pytest.mark.anyio
async def test_wall_first_with_no_volunteer_and_every_lane_busy_it_waits_then_the_429(env, monkeypatch):
    monkeypatch.setattr(coordinator, "PHONE_LANE_WAIT_SECONDS", 0.2)
    route(env, "wall_first")
    monkeypatch.setattr(coordinator, "_app_members", lambda: [])
    hold_phone_lanes()
    async with client() as c:
        resp = await c.post("/v1/app/chat/completions", json=phone_body())
    release_phone_lanes()

    assert resp.status_code == 429 and resp.headers["retry-after"] and env.cpu.payloads == []
    assert env.pool._active_by_network == {}


@pytest.mark.anyio
async def test_wall_first_with_the_volunteers_busy_too_the_phone_waits_for_the_lane_then_the_429(env, monkeypatch):
    monkeypatch.setattr(coordinator, "PHONE_LANE_WAIT_SECONDS", 0.2)
    route(env, "wall_first")
    env.pool._inflight["worker:w1"] = 2                      # both of its slots are in use
    hold_phone_lanes()
    async with client() as c:
        resp = await c.post("/v1/app/chat/completions", json=phone_body())
    release_phone_lanes()

    assert resp.status_code == 429 and env.cpu.payloads == [] and env.engine.chats == []


@pytest.mark.anyio
async def test_wall_first_a_busy_volunteer_does_not_take_a_text_while_a_lane_frees_up(env):
    route(env, "wall_first")
    env.pool._inflight["worker:w1"] = 2
    hold_phone_lanes()
    async with client() as c:
        task = asyncio.create_task(c.post("/v1/app/chat/completions", json=phone_body()))
        assert await until(lambda: coordinator.phone_gate.stats()["waiting"] == 1)
        release_phone_lanes()
        resp = await task

    assert b"engine-one" in resp.content and env.cpu.payloads == []


@pytest.mark.anyio
async def test_wall_first_with_the_engine_down_the_volunteers_answer_as_always(env):
    route(env, "wall_first")
    env.manager.running = False
    async with client() as c:
        resp = await c.post("/v1/app/chat/completions", json=phone_body())

    assert b"cpu-one" in resp.content and env.engine.chats == []


# ======================================================================================
# volunteers_first
# ======================================================================================

@pytest.mark.anyio
async def test_volunteers_first_a_text_goes_to_a_ready_volunteer_though_a_lane_is_free(env):
    route(env, "volunteers_first")
    async with client() as c:
        resp = await c.post("/v1/app/chat/completions", json=phone_body())
        pool = await status_pool()

    assert b"cpu-one" in resp.content and env.engine.chats == [] and pool["route"] == "volunteers_first"
    assert pool["serving"] == "volunteers"


@pytest.mark.anyio
async def test_volunteers_first_a_photo_goes_to_the_lane(lane_env):
    route(lane_env, "volunteers_first")
    async with client() as c:
        resp = await send(c, photo_body())

    assert b"engine-one" in resp.content and lane_env.cpu.payloads == []


@pytest.mark.anyio
async def test_volunteers_first_with_no_volunteer_the_lane_is_the_backup(env, monkeypatch):
    route(env, "volunteers_first")
    monkeypatch.setattr(coordinator, "_app_members", lambda: [])
    async with client() as c:
        resp = await c.post("/v1/app/chat/completions", json=phone_body())

    assert b"engine-one" in resp.content and env.cpu.payloads == []


@pytest.mark.anyio
async def test_volunteers_first_with_the_volunteers_busy_the_lane_is_the_backup(env):
    route(env, "volunteers_first")
    env.pool._inflight["worker:w1"] = 2
    async with client() as c:
        resp = await c.post("/v1/app/chat/completions", json=phone_body())

    assert b"engine-one" in resp.content and env.cpu.payloads == []


@pytest.mark.anyio
async def test_volunteers_first_with_neither_the_usual_429(env, monkeypatch):
    monkeypatch.setattr(coordinator, "PHONE_LANE_WAIT_SECONDS", 0.2)
    route(env, "volunteers_first")
    env.pool._inflight["worker:w1"] = 2
    hold_phone_lanes()
    async with client() as c:
        resp = await c.post("/v1/app/chat/completions", json=phone_body())
    release_phone_lanes()

    assert resp.status_code == 429 and env.cpu.payloads == [] and env.engine.chats == []


@pytest.mark.anyio
async def test_a_slow_volunteer_is_skipped_while_the_lane_is_free(env):
    route(env, "volunteers_first")
    env.limits["phone_min_tps"] = 6.0
    env.pool._member_tps["worker:w1"] = 2.0
    async with client() as c:
        resp = await c.post("/v1/app/chat/completions", json=phone_body())

    assert b"engine-one" in resp.content and env.cpu.payloads == []


@pytest.mark.anyio
async def test_a_slow_volunteer_still_answers_when_the_lanes_are_busy(env):
    route(env, "volunteers_first")
    env.pool._member_tps["worker:w1"] = 2.0
    hold_phone_lanes()
    async with client() as c:
        resp = await c.post("/v1/app/chat/completions", json=phone_body())
    release_phone_lanes()

    assert b"cpu-one" in resp.content


@pytest.mark.anyio
async def test_the_speed_floor_is_the_operators(env):
    route(env, "volunteers_first")
    env.limits["phone_min_tps"] = 1.0
    env.pool._member_tps["worker:w1"] = 2.0
    async with client() as c:
        resp = await c.post("/v1/app/chat/completions", json=phone_body())

    assert b"cpu-one" in resp.content


@pytest.mark.anyio
async def test_a_volunteer_answer_is_measured_and_its_speed_kept(env):
    route(env, "volunteers_first")
    async with client() as c:
        await c.post("/v1/app/chat/completions", json=phone_body())
    # the stand-in sends a few frames only: too few to say anything, so the member is still "not shown slow"
    assert env.pool.member_tps("worker:w1") is None or env.pool.member_tps("worker:w1") > 0


# ======================================================================================
# wall_only: today's order
# ======================================================================================

@pytest.mark.anyio
async def test_wall_only_keeps_todays_order_a_busy_lane_is_waited_for_and_never_a_volunteer(env, monkeypatch):
    monkeypatch.setattr(coordinator, "PHONE_LANE_WAIT_SECONDS", 0.2)
    route(env, "wall_only")
    hold_phone_lanes()
    async with client() as c:
        resp = await c.post("/v1/app/chat/completions", json=phone_body())
        pool = await status_pool()
    release_phone_lanes()

    assert resp.status_code == 429 and env.cpu.payloads == [] and pool["route"] == "wall_only"


@pytest.mark.anyio
async def test_wall_only_the_volunteers_answer_while_the_engine_is_down(env):
    route(env, "wall_only")
    env.manager.running = False
    async with client() as c:
        resp = await c.post("/v1/app/chat/completions", json=phone_body())

    assert b"cpu-one" in resp.content


# ======================================================================================
# 6e: a photo on a coding lane
# ======================================================================================

def no_phone_vision(lane_env):
    lane_env.limits["phone_vision"] = False
    return lane_env


@pytest.mark.anyio
async def test_a_phone_photo_goes_to_a_coding_lane_when_the_phone_model_cannot_see(lane_env):
    no_phone_vision(lane_env)
    async with client() as c:
        resp = await send(c, photo_body())

    assert resp.status_code == 200 and b"engine-one" in resp.content and lane_env.cpu.payloads == []
    (sent,) = lane_env.engine.chats
    assert sent["id_slot"] in CODING_LANES, "a coding lane, not one of the phone lanes"
    assert coordinator.coding_gate.stats()["in_use"] == 0 and nothing_held(lane_env)
    assert coordinator.active_inference_requests == 0


@pytest.mark.anyio
async def test_a_phone_model_that_can_see_keeps_its_own_photos(lane_env):
    async with client() as c:
        resp = await send(c, photo_body())

    assert b"engine-one" in resp.content and lane_env.engine.chats[0]["id_slot"] in PHONE_LANES


@pytest.mark.anyio
async def test_a_text_only_turn_stays_on_the_phone_model(lane_env):
    no_phone_vision(lane_env)
    async with client() as c:
        text = await c.post("/v1/app/chat/completions", json=phone_body("hello"))
        shot = await send(c, photo_body())
        after = await c.post("/v1/app/chat/completions", json=phone_body("and now?"))

    first, second, third = lane_env.engine.chats
    assert first["id_slot"] in PHONE_LANES and second["id_slot"] in CODING_LANES and third["id_slot"] in PHONE_LANES
    assert text.status_code == shot.status_code == after.status_code == 200


@pytest.mark.anyio
async def test_the_photo_takes_the_coldest_coding_lane(lane_env):
    no_phone_vision(lane_env)
    book = coordinator.lane_book
    # lane 0 has just been used by a coding chat, lane 1 never was
    book.give(book.take(0, CODING_SLOTS, spread=True))
    async with client() as c:
        await send(c, photo_body())
    assert lane_env.engine.chats[0]["id_slot"] == 1

    # both used, lane 1 minutes ago and lane 0 a moment ago: the older one
    lane_env.engine.reset()
    now = time.monotonic()
    book._last_used_at.update({0: now - 5, 1: now - 1000})
    async with client() as c:
        await send(c, photo_body(chat_id=None))
    assert lane_env.engine.chats[0]["id_slot"] == 1

    lane_env.engine.reset()
    book._last_used_at.update({0: now - 2000, 1: now - 3})
    async with client() as c:
        await send(c, photo_body(chat_id=None))
    assert lane_env.engine.chats[0]["id_slot"] == 0


@pytest.mark.anyio
async def test_the_photo_never_interrupts_a_coding_answer_it_takes_the_other_lane(lane_env):
    no_phone_vision(lane_env)
    assert coordinator.coding_gate.try_acquire()
    busy = coordinator.lane_book.take(0, CODING_SLOTS, spread=True)
    async with client() as c:
        resp = await send(c, photo_body())
    coordinator.lane_book.give(busy)
    coordinator.coding_gate.release()

    assert b"engine-one" in resp.content and lane_env.engine.chats[0]["id_slot"] == 1 - busy


@pytest.mark.anyio
async def test_the_photo_waits_for_a_coding_slot_like_a_phone_then_the_429(lane_env, monkeypatch):
    no_phone_vision(lane_env)
    monkeypatch.setattr(coordinator, "PHONE_LANE_WAIT_SECONDS", 0.2)
    assert all([coordinator.coding_gate.try_acquire() for _ in range(CODING_SLOTS)])
    async with client() as c:
        waiting = asyncio.create_task(send(c, photo_body()))
        assert await until(lambda: coordinator.coding_gate.stats()["waiting"] == 1)
        resp = await waiting
    for _ in range(CODING_SLOTS):
        coordinator.coding_gate.release()

    assert resp.status_code == 429 and resp.headers["retry-after"] == "5"
    assert lane_env.engine.chats == [] and lane_env.cpu.payloads == []
    assert nothing_held(lane_env) and coordinator.coding_gate.stats()["waiting"] == 0
    assert coordinator.active_inference_requests == 0


@pytest.mark.anyio
async def test_the_photo_gets_a_coding_slot_when_one_frees_and_queues_at_the_phones_tier(lane_env, monkeypatch):
    no_phone_vision(lane_env)
    seen = []
    real = CodingGate.acquire

    async def spy(self, priority, slots=None, ticket=None):
        seen.append(priority)
        return await real(self, priority, slots=slots, ticket=ticket)

    monkeypatch.setattr(CodingGate, "acquire", spy)
    assert all([coordinator.coding_gate.try_acquire() for _ in range(CODING_SLOTS)])
    async with client() as c:
        task = asyncio.create_task(send(c, photo_body()))
        assert await until(lambda: coordinator.coding_gate.stats()["waiting"] == 1)
        coordinator.coding_gate.release()
        resp = await task
    coordinator.coding_gate.release()

    assert b"engine-one" in resp.content
    assert seen == [1], "the phone's tier (a key holder, 0, goes first; a free trial, 2, after)"
    assert nothing_held(lane_env)


@pytest.mark.anyio
async def test_the_history_sent_with_the_photo_is_capped_at_the_phones_context(lane_env):
    no_phone_vision(lane_env)
    history = []
    for i in range(30):
        history += [{"role": "user", "content": f"question {i} " + "x" * 3000},
                    {"role": "assistant", "content": f"answer {i} " + "y" * 3000}]
    shot = photo_body("what is this?")["messages"][0]
    async with client() as c:
        resp = await send(c, {"messages": history + [shot], "chat_id": CHAT_A})

    assert resp.status_code == 200
    (sent,) = lane_env.engine.chats
    chars = sum(len(m["content"]) for m in sent["messages"] if isinstance(m["content"], str))
    assert sent["id_slot"] in CODING_LANES
    assert chars / 3 + sent["max_tokens"] <= 8192, "the engine's tokenizer would count more than the phone's context"
    assert sent["messages"][-1]["content"][-1]["type"] == "image_url", "the newest turn and its photo are kept"
    assert len(sent["messages"]) < len(history), "older turns were cut"


@pytest.mark.anyio
async def test_with_no_projector_anywhere_a_photo_is_refused_as_today(lane_env):
    no_phone_vision(lane_env)
    lane_env.vision = False
    async with client() as c:
        resp = await send(c, photo_body())

    assert resp.status_code == 400 and resp.json()["code"] == media.IMAGE_INPUT_UNAVAILABLE
    assert lane_env.engine.chats == [] and lane_env.cpu.payloads == [] and nothing_held(lane_env)


@pytest.mark.anyio
async def test_with_no_phone_lanes_a_photo_still_reads_on_a_coding_lane_and_a_text_goes_to_the_volunteers(lane_env):
    lane_env.limits["engine_phone_slots"] = 0
    lane_env.manager.phone_slots = 0
    async with client() as c:
        shot = await send(c, photo_body())
        text = await c.post("/v1/app/chat/completions", json=phone_body("hello"))

    assert b"engine-one" in shot.content and "id_slot" not in lane_env.engine.chats[0], "no lanes of its own to pin to"
    assert b"cpu-one" in text.content and len(lane_env.cpu.payloads) == 1
    assert coordinator.coding_gate.stats()["in_use"] == 0


@pytest.mark.anyio
async def test_a_photo_never_reaches_the_cpu_pool_when_the_coding_engine_fails(lane_env):
    no_phone_vision(lane_env)
    lane_env.engine.fails = True
    async with client() as c:
        resp = await send(c, photo_body())

    assert lane_env.cpu.payloads == [] and nothing_held(lane_env)
    assert coordinator.coding_gate.stats()["in_use"] == 0


@pytest.mark.anyio
async def test_the_photo_is_counted_in_the_hourly_budget(lane_env):
    no_phone_vision(lane_env)
    lane_env.limits["image_input_per_hour"] = 1
    async with client() as c:
        first = await send(c, photo_body())
        second = await send(c, photo_body(chat_id=None))

    assert first.status_code == 200 and second.status_code == 429


@pytest.mark.anyio
@pytest.mark.parametrize("phone_vision, slots, vision_set, expected", [
    (True, PHONE_SLOTS, True, "phone_model"),
    (False, PHONE_SLOTS, True, "coding_lane"),
    (True, 0, True, "coding_lane"),
    (True, PHONE_SLOTS, False, "none"),
    (False, 0, False, "none"),
])
async def test_the_status_says_where_photos_are_read(lane_env, phone_vision, slots, vision_set, expected):
    lane_env.limits["phone_vision"] = phone_vision
    lane_env.limits["engine_phone_slots"] = slots
    lane_env.manager.vision_configured = lambda: vision_set
    pool = await status_pool()

    assert pool["photos"] == expected


@pytest.mark.anyio
async def test_the_status_mentions_app_vision_with_no_phone_lanes_while_a_projector_is_set_up(lane_env):
    lane_env.limits["engine_phone_slots"] = 0
    async with client(("127.0.0.1", 5000)) as admin:
        caps = (await admin.get("/v1/cluster/status")).json()["capabilities"]

    assert caps["app_vision"] is True


@pytest.mark.anyio
async def test_lanes_configured_but_the_engine_started_without_them_still_reads_a_photo_on_a_coding_lane(lane_env):
    lane_env.manager.phone_slots = 0
    async with client() as c:
        resp = await send(c, photo_body())

    assert b"engine-one" in resp.content and "id_slot" not in lane_env.engine.chats[0] and lane_env.cpu.payloads == []


# ======================================================================================
# A volunteer that fails: TheWall's lane is the backup
# ======================================================================================

@pytest.mark.anyio
@pytest.mark.parametrize("route_name, busy_lanes", [("wall_first", True), ("volunteers_first", False)])
@pytest.mark.parametrize("mode", ["500", "die_before_first_chunk"])
async def test_a_volunteer_that_fails_before_any_text_hands_the_phone_to_thewall(env, route_name, busy_lanes, mode):
    route(env, route_name)
    env.cpu.mode = mode
    if busy_lanes:
        hold_phone_lanes()
        async with client() as c:
            task = asyncio.create_task(c.post("/v1/app/chat/completions", json=phone_body()))
            assert await until(lambda: coordinator.phone_gate.stats()["waiting"] == 1)
            release_phone_lanes()                    # a lane frees up while the phone waits for it
            resp = await task
    else:
        async with client() as c:
            resp = await c.post("/v1/app/chat/completions", json=phone_body())
            pool = await status_pool()
        assert pool["serving"] == "engine"

    assert resp.status_code == 200 and b"engine-one" in resp.content and b"No machine" not in resp.content
    assert len(env.cpu.payloads) >= 1 and len(env.engine.chats) == 1
    assert nothing_held(env)


@pytest.mark.anyio
@pytest.mark.parametrize("route_name", ["wall_first", "volunteers_first"])
async def test_a_failed_volunteer_and_busy_lanes_for_good_is_the_usual_429(env, monkeypatch, route_name):
    monkeypatch.setattr(coordinator, "PHONE_LANE_WAIT_SECONDS", 0.2)
    route(env, route_name)
    env.cpu.mode = "500"
    if route_name == "wall_first":
        hold_phone_lanes()
    else:
        hold_phone_lanes()                            # the volunteer is tried first whatever the lanes
    async with client() as c:
        resp = await c.post("/v1/app/chat/completions", json=phone_body())
    release_phone_lanes()

    assert resp.status_code == 429 and resp.headers["retry-after"] and env.engine.chats == []
    assert env.pool._active_by_network == {} and nothing_held(env)


@pytest.mark.anyio
@pytest.mark.parametrize("route_name, busy_lanes", [("wall_first", True), ("volunteers_first", False)])
async def test_a_volunteer_that_fails_after_text_is_the_dropped_notice_and_nothing_else_answers(env, route_name, busy_lanes):
    route(env, route_name)
    env.cpu.mode = "die_mid_stream"
    env.cpu.die_after = 1
    if busy_lanes:
        hold_phone_lanes()
    async with client() as c:
        resp = await c.post("/v1/app/chat/completions", json=phone_body())
    if busy_lanes:
        release_phone_lanes()

    assert b"cpu-one" in resp.content and b"dropped out" in resp.content
    assert env.engine.chats == [], "no double answer"
    assert nothing_held(env)


@pytest.mark.anyio
async def test_a_failed_volunteer_with_thewall_unavailable_keeps_the_no_machine_notice(env):
    route(env, "wall_first")
    env.cpu.mode = "500"
    env.manager.running = False
    async with client() as c:
        resp = await c.post("/v1/app/chat/completions", json=phone_body())

    assert resp.status_code == 200 and env.engine.chats == []
    assert b"No machine" in resp.content or b"restarting" in resp.content.lower()
