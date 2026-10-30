"""
Image generation through the coordinator (2.6.1): the phone endpoints (/v1/app/images*), the coding-side ones (/v1/images*),
the status, the free trial, and the coordinator's own check of a prompt or a picture (_image_judge).

The image engine and the model are stand-ins (tests/test_image_pool.py); the real service, the real routes and the real
middleware are used. A network address is a test client's peer, as in tests/test_starter_quota_http.py.
"""

import asyncio
import base64
import threading
from types import SimpleNamespace

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from engine import coordinator, image_policy
from engine.contributor_networks import ContributorBook
from engine.image_pool import ImageService, JudgeUnavailable
from engine.priority import CodingGate, LaneBook
from tests.app_pool_fakes import free_port, wait_for
from tests.image_fakes import sd_png
from tests.test_image_pool import NO, YES, FakeMember, Judge
from tests.test_media_chat import counter_env, engine  # noqa: F401 - fixtures (the stub engine and its status)

REMOTE = ("203.0.113.9", 40000)
OTHER = ("198.51.100.20", 40001)
LOCAL = ("127.0.0.1", 5000)
MODEL = "m.gguf"
JSON = {"Content-Type": "application/json"}


def client(peer=REMOTE, headers=None):
    return AsyncClient(transport=ASGITransport(app=coordinator.app, client=peer), base_url="http://test", headers=headers or {})


@pytest.fixture
async def images(monkeypatch, tmp_path):
    """Image generation on, a stand-in engine and model, a trial of 5 chats, a limit of 3 pictures an hour."""
    limits = {"image_gen_enabled": True, "image_per_hour": 3, "image_queue_max": 2, "image_result_ttl_seconds": 600,
              "starter_quota_inferences": 5, "enforce_contrib_clause": True}
    real = coordinator.get_worker_limits
    monkeypatch.setattr(coordinator, "get_worker_limits", lambda: {**real(), **limits})
    member, judge = FakeMember(), Judge()
    service = ImageService(member, judge, limits=lambda: coordinator.get_worker_limits(), housekeeping=0.05)
    monkeypatch.setattr(coordinator, "image_service", service)

    async def vision(_names):
        return True

    monkeypatch.setattr(coordinator, "_engine_vision", vision)
    monkeypatch.setattr(coordinator.manager, "get_active_model_names", lambda: [MODEL])
    monkeypatch.setattr(coordinator, "is_swapping_models", False)
    monkeypatch.setattr(coordinator, "is_recalculating_queue", False)
    monkeypatch.setattr(coordinator, "_image_reason_seen", None)
    quota = coordinator.StarterQuotaManager(str(tmp_path / "starter_quota.json"), default_quota=5)
    monkeypatch.setattr(coordinator, "starter_quota_mgr", quota)
    monkeypatch.setattr(coordinator.registry, "workers", {})
    # no network is a contributor's unless a test says so (a heartbeat in another test file must not leak in)
    monkeypatch.setattr(coordinator, "contributor_networks", ContributorBook())
    yield SimpleNamespace(limits=limits, member=member, judge=judge, service=service, quota=quota)
    await service.shutdown()


async def post(c, path="/v1/app/images", body=None):
    return await c.post(path, json={"prompt": "a red fox"} if body is None else body)


async def finished(c, path, job_id, timeout=5.0):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        reply = await c.get(f"{path}/{job_id}")
        if reply.json()["status"] in ("completed", "refused", "failed", "cancelled", "expired"):
            return reply.json()
        await asyncio.sleep(0.01)
    raise AssertionError("the job did not finish")


# --- with no new keys nothing changes --------------------------------------------------------------------------

ROUTES = [("post", "/v1/app/images"), ("get", "/v1/app/images/abc"), ("post", "/v1/app/images/abc/cancel"),
          ("post", "/v1/images"), ("get", "/v1/images/abc"), ("post", "/v1/images/abc/cancel")]


@pytest.mark.anyio
@pytest.mark.parametrize("method, path", ROUTES)
async def test_with_image_generation_off_every_endpoint_says_503_unavailable(tmp_path, monkeypatch, method, path):
    quota = coordinator.StarterQuotaManager(str(tmp_path / "q.json"), default_quota=5)
    monkeypatch.setattr(coordinator, "starter_quota_mgr", quota)
    assert coordinator.get_worker_limits()["image_gen_enabled"] is False            # the default
    async with client() as c:
        reply = await getattr(c, method)(path, json={"prompt": "a red fox"}) if method == "post" else await c.get(path)
    assert reply.status_code == 503
    assert reply.json() == {"detail": coordinator.IMAGE_UNAVAILABLE, "code": "image_generation_unavailable"}
    assert quota.summary(5)["chats_used"] == 0                                      # not even a free chat was spent


@pytest.mark.anyio
async def test_with_image_generation_off_the_status_is_what_2_6_0_sent(engine, monkeypatch):
    async def snapshot(_name):
        return dict(engine.entry)

    monkeypatch.setattr(coordinator.manager, "asnapshot_model", snapshot)
    monkeypatch.setattr(coordinator.manager, "get_active_model_names", lambda: [MODEL])
    async with client(LOCAL) as c:
        status = (await c.get("/v1/cluster/status")).json()
    assert status["capabilities"] == {"vision": True} and "image_generation" not in status


def test_the_limits_default_to_off_and_the_contracts_numbers():
    limits = coordinator.get_worker_limits()
    assert (limits["image_gen_enabled"], limits["image_model_dir"], limits["image_threads"], limits["image_cpu_mask"],
            limits["image_per_hour"], limits["image_queue_max"], limits["image_job_timeout_seconds"],
            limits["image_idle_unload_minutes"], limits["image_result_ttl_seconds"]) == (False, "", 4, "", 6, 4, 300, 20, 1800)


# --- the status ---------------------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_the_status_offers_images_with_the_contracts_block(engine, images, monkeypatch):
    async def snapshot(_name):
        return dict(engine.entry)

    monkeypatch.setattr(coordinator.manager, "asnapshot_model", snapshot)
    images.limits["image_per_hour"] = 6
    async with client(LOCAL) as c:
        status = (await c.get("/v1/cluster/status")).json()
    assert status["capabilities"] == {"vision": True, "image_generation": True}
    assert status["image_generation"] == {"queue": 0, "busy": False, "eta_seconds": {"768x768": 80, "1024x1024": 153},
                                          "sizes": ["768x768", "1024x1024", "768x1024", "1024x768"],
                                          "default_size": "768x768", "per_hour": 6}


@pytest.mark.anyio
@pytest.mark.parametrize("why", ["no vision", "not installed", "restarting"])
async def test_the_status_says_false_and_has_no_block_when_images_cannot_be_made(engine, images, monkeypatch, why):
    async def snapshot(_name):
        return dict(engine.entry)

    monkeypatch.setattr(coordinator.manager, "asnapshot_model", snapshot)
    if why == "no vision":
        async def no_vision(_names):
            return False

        monkeypatch.setattr(coordinator, "_engine_vision", no_vision)
    elif why == "not installed":
        images.member.reason = "the sd build is not installed"
    else:
        monkeypatch.setattr(coordinator, "is_swapping_models", True)
    async with client(LOCAL) as c:
        status = (await c.get("/v1/cluster/status")).json()
    assert status["capabilities"]["image_generation"] is False and "image_generation" not in status


# --- paused (IMAGE_GEN_ENABLED=paused, 2026-10-30) ----------------------------------------------------------------------

PAUSED_NOTE = ("Pictures are paused for now: DLNE's graphics card is busy running the smarter coding model. "
               "They will be back.")
PAUSED_BODY = {"detail": PAUSED_NOTE, "code": "image_generation_paused", "page": "/pictures"}


@pytest.fixture
def paused(images, monkeypatch):
    """The images environment as config.txt's IMAGE_GEN_ENABLED=paused leaves it: off, and paused. The stand-in image
    engine is never even asked whether it could run."""
    images.limits.update(image_gen_enabled=False, image_gen_paused=True)
    asked = []

    def unavailable_reason():
        asked.append(1)
        return None

    monkeypatch.setattr(images.member, "unavailable_reason", unavailable_reason)
    images.asked = asked
    return images


def test_the_note_is_the_contracts_sentence():
    assert coordinator.PICTURES_PAUSED_NOTE == PAUSED_NOTE and coordinator.PICTURES_PATH == "/pictures"


@pytest.mark.anyio
@pytest.mark.parametrize("method, path", ROUTES)
async def test_while_paused_every_endpoint_says_503_paused_with_the_page(paused, method, path):
    async with client() as c:
        reply = await getattr(c, method)(path, json={"prompt": "a red fox"}) if method == "post" else await c.get(path)
    assert reply.status_code == 503 and reply.json() == PAUSED_BODY
    assert reply.headers["cache-control"] == "no-store"
    assert paused.quota.summary(5)["chats_used"] == 0                               # not even a free chat was spent
    assert paused.member.calls == [] and paused.service.status_block()["queue"] == 0 and not paused.service._jobs
    assert paused.asked == [], "nothing image-related is even asked about"


@pytest.mark.anyio
async def test_while_paused_a_job_made_before_the_pause_is_not_served_either(images):
    async with client() as c:
        made = (await post(c)).json()["id"]
        await finished(c, "/v1/app/images", made)
        images.limits.update(image_gen_enabled=False, image_gen_paused=True)
        reply = await c.get(f"/v1/app/images/{made}")
    assert reply.status_code == 503 and reply.json() == PAUSED_BODY


@pytest.mark.anyio
async def test_while_paused_the_status_says_so_and_offers_nothing(engine, paused, monkeypatch):
    async def snapshot(_name):
        return dict(engine.entry)

    monkeypatch.setattr(coordinator.manager, "asnapshot_model", snapshot)
    async with client(LOCAL) as c:
        status = (await c.get("/v1/cluster/status")).json()
    assert status["capabilities"] == {"vision": True, "image_generation": False, "image_generation_paused": True}
    assert status["images"] == {"paused": True, "note": PAUSED_NOTE, "page": "/pictures"}
    assert "image_generation" not in status                                         # no block of numbers while paused
    assert paused.asked == []


@pytest.mark.anyio
async def test_on_or_off_the_status_never_mentions_a_pause(engine, images, monkeypatch):
    async def snapshot(_name):
        return dict(engine.entry)

    monkeypatch.setattr(coordinator.manager, "asnapshot_model", snapshot)
    async with client(LOCAL) as c:
        on = (await c.get("/v1/cluster/status")).json()
        images.limits["image_gen_enabled"] = False
        off = (await c.get("/v1/cluster/status")).json()
    assert on["capabilities"] == {"vision": True, "image_generation": True} and "images" not in on
    assert off["capabilities"] == {"vision": True} and "images" not in off and "image_generation" not in off


@pytest.mark.anyio
async def test_while_paused_the_canaries_look_like_a_server_without_pictures(paused):
    assert await coordinator._image_offered() is False


# --- the phone endpoints ---------------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_a_phone_makes_a_picture_end_to_end(images):
    async with client() as c:
        created = await post(c, body={"prompt": "a red fox <lora:x:1> in the snow", "size": "1024x768",
                                      "chat_id": "3f2b9c1e-6a47-4d52-9a0e-1b7c5d8e2f41"})
        assert created.status_code == 202
        body = created.json()
        assert set(body) == {"id", "status", "position", "eta_seconds"} and body["status"] == "queued"
        assert body["position"] == 1 and body["eta_seconds"] >= 100
        assert created.headers["cache-control"] == "no-store"

        done = await finished(c, "/v1/app/images", body["id"])

    assert done["status"] == "completed" and set(done) == {"id", "status", "elapsed_seconds", "image"}
    image = done["image"]
    assert (image["width"], image["height"]) == (1024, 768) and isinstance(image["seed"], int)
    png = base64.b64decode(image["b64_png"])
    assert png.startswith(b"\x89PNG") and b"parameters" not in png and b"secret" not in png
    assert images.member.calls[0]["prompt"] == "a red fox in the snow"           # cleaned: the tag block is gone


@pytest.mark.anyio
async def test_the_default_size_is_768(images):
    async with client() as c:
        created = await post(c)
        await finished(c, "/v1/app/images", created.json()["id"])
    assert (images.member.calls[0]["width"], images.member.calls[0]["height"]) == (768, 768)


@pytest.mark.anyio
@pytest.mark.parametrize("body, code", [
    ({}, "prompt_empty"),
    ({"prompt": "   \x00 "}, "prompt_empty"),
    ({"prompt": "<lora:only:1>"}, "prompt_empty"),
    ({"prompt": 5}, "prompt_empty"),
    ({"prompt": "x" * 1001}, "prompt_too_long"),
    ({"prompt": "x" * 5000}, "prompt_too_long"),
    ({"prompt": "a fox", "size": "512x512"}, "bad_size"),
    ({"prompt": "a fox", "size": 768}, "bad_size"),
    ({"prompt": "a fox", "size": "1920x1080"}, "bad_size"),
])
async def test_bad_requests_get_a_400_with_a_code_and_cost_nothing(images, body, code):
    async with client() as c:
        reply = await c.post("/v1/app/images", json=body)
    assert reply.status_code == 400 and reply.json()["code"] == code and reply.json()["detail"]
    assert images.quota.summary(5)["chats_used"] == 0 and images.service.status_block()["queue"] == 0


@pytest.mark.anyio
async def test_a_body_that_is_not_json_is_a_400(images):
    async with client() as c:
        reply = await c.post("/v1/app/images", content="{", headers=JSON)
        assert reply.status_code == 400 and reply.json()["code"] == "bad_request"
        assert (await c.post("/v1/app/images", json=["a fox"])).status_code == 400


@pytest.mark.anyio
async def test_exactly_one_thousand_characters_is_fine(images):
    async with client() as c:
        assert (await post(c, body={"prompt": "a" * 1000})).status_code == 202


@pytest.mark.anyio
async def test_the_term_list_refuses_at_once_with_its_category_and_spends_nothing(images):
    async with client() as c:
        reply = await post(c, body={"prompt": "a nude woman"})
    assert reply.status_code == 400
    assert reply.json() == {"detail": image_policy.MESSAGES["sexual"], "code": "prompt_refused", "category": "sexual"}
    assert images.member.calls == [] and images.judge.calls == [] and images.quota.summary(5)["chats_used"] == 0


@pytest.mark.anyio
async def test_the_models_refusal_is_a_refused_job_with_the_same_code_and_category(images):
    images.judge.text_answer = NO
    async with client() as c:
        created = await post(c)
        done = await finished(c, "/v1/app/images", created.json()["id"])
    assert done["status"] == "refused"
    assert done["error"]["code"] == "prompt_refused" and done["error"]["category"] == "violence"


@pytest.mark.anyio
async def test_another_network_gets_404_for_a_job_it_did_not_make(images):
    async with client() as c, client(OTHER) as other:
        job_id = (await post(c)).json()["id"]
        await finished(c, "/v1/app/images", job_id)
        for reply in (await other.get(f"/v1/app/images/{job_id}"), await other.post(f"/v1/app/images/{job_id}/cancel"),
                      await c.get(f"/v1/images/{job_id}"), await c.post(f"/v1/images/{job_id}/cancel"),
                      await c.get("/v1/app/images/not-a-job")):
            assert reply.status_code == 404 and reply.json()["code"] == "image_not_found"
        assert (await c.get(f"/v1/app/images/{job_id}")).status_code == 200
        assert (await other.get(f"/v1/app/images/{job_id}")).status_code == 404            # and still not after the owner read it


@pytest.mark.anyio
async def test_one_ipv6_block_is_one_network(images):
    mine, same_block, other_block = ("2001:db8:1:2::1", 1), ("2001:db8:1:2:ffff::9", 1), ("2001:db8:1:3::1", 1)
    async with client(mine) as a, client(same_block) as b, client(other_block) as c:
        job_id = (await post(a)).json()["id"]
        assert (await b.get(f"/v1/app/images/{job_id}")).status_code == 200
        assert (await c.get(f"/v1/app/images/{job_id}")).status_code == 404


@pytest.mark.anyio
async def test_the_hourly_cap_is_per_network_and_says_when_to_come_back(images):
    images.limits["image_queue_max"] = 16
    async with client() as c, client(OTHER) as other:
        for _ in range(3):
            assert (await post(c)).status_code == 202
        reply = await post(c)
        assert reply.status_code == 429 and reply.json()["code"] == "image_quota"
        assert 0 < int(reply.headers["retry-after"]) <= 3601 and "3 pictures an hour" in reply.json()["detail"]
        assert (await post(other)).status_code == 202


@pytest.mark.anyio
async def test_a_full_queue_is_429_busy_with_retry_after(images):
    images.member.hold, images.member.release = True, asyncio.Event()
    images.limits.update(image_per_hour=60, starter_quota_inferences=0, enforce_contrib_clause=False)
    async with client() as c:
        first = (await post(c)).json()["id"]
        for _ in range(100):
            if (await c.get(f"/v1/app/images/{first}")).json()["status"] == "generating":
                break
            await asyncio.sleep(0.01)
        for _ in range(2):
            assert (await post(c)).status_code == 202
        reply = await post(c)
        images.member.release.set()
    assert reply.status_code == 429 and reply.json()["code"] == "image_busy" and int(reply.headers["retry-after"]) >= 5


@pytest.mark.anyio
async def test_cancel_returns_the_jobs_status(images):
    images.member.hold, images.member.release = True, asyncio.Event()
    async with client() as c:
        job_id = (await post(c)).json()["id"]
        for _ in range(100):
            if (await c.get(f"/v1/app/images/{job_id}")).json()["status"] == "generating":
                break
            await asyncio.sleep(0.01)
        reply = await c.post(f"/v1/app/images/{job_id}/cancel")
        assert reply.status_code == 200 and reply.json()["status"] == "cancelled"
        assert (await c.get(f"/v1/app/images/{job_id}")).json()["status"] == "cancelled"
        assert (await c.post(f"/v1/app/images/{job_id}/cancel")).json()["status"] == "cancelled"        # repeated: safe


# --- unavailable while on ------------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_creating_a_job_says_503_when_the_machine_cannot_make_images_right_now(images, monkeypatch):
    images.member.reason = "the sd build is not installed"
    async with client() as c:
        reply = await post(c)
        assert reply.status_code == 503 and reply.json()["code"] == "image_generation_unavailable"
        assert reply.json()["detail"] == coordinator.IMAGE_UNAVAILABLE
        assert images.quota.summary(5)["chats_used"] == 0


@pytest.mark.anyio
async def test_a_phone_during_an_engine_restart_reads_the_restarting_sentence(images, monkeypatch):
    monkeypatch.setattr(coordinator, "is_swapping_models", True)
    async with client() as c:
        reply = await post(c)
    assert reply.status_code == 503 and reply.json() == {"detail": "DLNE is restarting, try again in a moment.",
                                                       "code": "image_generation_unavailable"}


@pytest.mark.anyio
async def test_without_vision_images_are_unavailable(images, monkeypatch):
    async def no_vision(_names):
        return False

    monkeypatch.setattr(coordinator, "_engine_vision", no_vision)
    async with client() as c:
        assert (await post(c)).status_code == 503


@pytest.mark.anyio
async def test_a_job_already_made_can_still_be_read_while_images_are_unavailable(images, monkeypatch):
    async with client() as c:
        job_id = (await post(c)).json()["id"]
        await finished(c, "/v1/app/images", job_id)
        monkeypatch.setattr(coordinator, "is_swapping_models", True)
        assert (await c.get(f"/v1/app/images/{job_id}")).json()["status"] == "completed"


# --- the free trial: one chat per picture ----------------------------------------------------------------------------

@pytest.mark.anyio
async def test_a_picture_costs_a_phone_one_trial_chat(images):
    async with client() as c:
        job_id = (await post(c)).json()["id"]
        await finished(c, "/v1/app/images", job_id)
    assert images.quota.summary(5)["chats_used"] == 1


@pytest.mark.anyio
async def test_a_used_up_trial_is_429_quota(images):
    images.limits["starter_quota_inferences"] = 1
    async with client() as c:
        assert (await post(c)).status_code == 202
        reply = await post(c)
    assert reply.status_code == 429 and reply.json()["code"] == "image_quota" and "free trial" in reply.json()["detail"]


@pytest.mark.anyio
async def test_the_host_an_api_key_and_the_contribution_rule_off_are_not_charged(images, monkeypatch):
    monkeypatch.setattr(coordinator.api_key_manager, "verify_key", lambda token: token == "good-key")
    async with client(LOCAL) as local, client(headers={"Authorization": "Bearer good-key"}) as keyed:
        assert (await post(local)).status_code == 202
        assert (await post(keyed)).status_code == 202
    images.limits["enforce_contrib_clause"] = False
    async with client(OTHER) as anyone:
        assert (await post(anyone)).status_code == 202
    assert images.quota.summary(5)["chats_used"] == 0


@pytest.mark.anyio
async def test_a_job_that_fails_through_no_fault_of_the_prompt_gives_the_chat_back(images):
    images.judge.error = JudgeUnavailable()
    async with client() as c:
        job_id = (await post(c)).json()["id"]
        done = await finished(c, "/v1/app/images", job_id)
    assert done["status"] == "failed" and images.quota.summary(5)["chats_used"] == 0


@pytest.mark.anyio
async def test_a_refused_picture_keeps_its_chat_spent(images):
    images.judge.image_answer = NO
    async with client() as c:
        await finished(c, "/v1/app/images", (await post(c)).json()["id"])
    assert images.quota.summary(5)["chats_used"] == 1


@pytest.mark.anyio
async def test_a_job_turned_away_by_the_hourly_cap_or_a_full_queue_is_not_charged(images):
    images.limits["image_per_hour"] = 1
    async with client() as c:
        await post(c)
        assert (await post(c)).status_code == 429
    assert images.quota.summary(5)["chats_used"] == 1


# --- the coding side --------------------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_the_ide_makes_a_picture_through_the_same_gate_as_chat(images):
    async with client() as c:
        created = await post(c, "/v1/images", {"prompt": "an icon of a leaf", "chat_id": "ignored"})
        assert created.status_code == 202 and set(created.json()) == {"id", "status", "position", "eta_seconds"}
        done = await finished(c, "/v1/images", created.json()["id"])
    assert done["status"] == "completed" and done["image"]["width"] == 768
    assert images.judge.calls[0][0] == "ide"                     # its checks take a coding slot, not a phone lane
    assert images.quota.summary(5)["chats_used"] == 1            # create passed check_contrib_clause: one trial chat


@pytest.mark.anyio
async def test_polling_and_cancelling_do_not_spend_the_trial(images):
    images.limits["starter_quota_inferences"] = 1
    async with client() as c:
        job_id = (await post(c, "/v1/images")).json()["id"]
        for _ in range(20):
            assert (await c.get(f"/v1/images/{job_id}")).status_code == 200
        await c.post(f"/v1/images/{job_id}/cancel")
    assert images.quota.summary(1)["chats_used"] == 1


@pytest.mark.anyio
async def test_the_ide_wall_is_the_chat_wall(images):
    images.limits["starter_quota_inferences"] = 1
    async with client() as c:
        assert (await post(c, "/v1/images")).status_code == 202
        reply = await post(c, "/v1/images")
    assert reply.status_code == 403 and "starter quota" in reply.json()["detail"].lower()


@pytest.mark.anyio
async def test_the_host_and_keys_are_not_charged_on_the_ide_side_either(images, monkeypatch):
    monkeypatch.setattr(coordinator.api_key_manager, "verify_key", lambda token: token == "good-key")
    async with client(LOCAL) as local, client(headers={"Authorization": "Bearer good-key"}) as keyed:
        assert (await post(local, "/v1/images")).status_code == 202
        assert (await post(keyed, "/v1/images")).status_code == 202
    assert images.quota.summary(5)["chats_used"] == 0


@pytest.mark.anyio
async def test_the_ide_gives_the_chat_back_when_the_cap_turns_the_job_away(images):
    images.limits["image_per_hour"] = 1
    async with client() as c:
        await post(c, "/v1/images")
        assert (await post(c, "/v1/images")).status_code == 429
    assert images.quota.summary(5)["chats_used"] == 1


@pytest.mark.anyio
async def test_the_ide_endpoints_have_the_same_validation_and_ownership(images):
    async with client() as c, client(OTHER) as other:
        assert (await c.post("/v1/images", json={"prompt": "a nude"})).json()["code"] == "prompt_refused"
        assert (await c.post("/v1/images", json={"prompt": "a fox", "size": "1x1"})).json()["code"] == "bad_size"
        job_id = (await post(c, "/v1/images")).json()["id"]
        assert (await other.get(f"/v1/images/{job_id}")).status_code == 404
        assert (await other.post(f"/v1/images/{job_id}/cancel")).status_code == 404


# --- the coordinator's own check (_image_judge) ----------------------------------------------------------------------

class CheckEngine:
    """A stand-in llama-server: answers /v1/chat/completions with a verdict and records every body."""

    def __init__(self):
        self.port = free_port()
        self.bodies = []
        self.answer = YES
        self.status = 200
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        app = FastAPI()

        @app.post("/v1/chat/completions")
        async def chat(request: Request):
            self.bodies.append(await request.json())
            self.entered.set()
            await asyncio.to_thread(self.release.wait, 10)
            if self.status != 200:
                return JSONResponse({"error": "bad"}, status_code=self.status)
            return {"choices": [{"message": {"role": "assistant", "content": self.answer}}]}

        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="critical", lifespan="off"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        assert wait_for(lambda: self.server.started)

    def stop(self):
        self.release.set()
        self.server.should_exit = True
        self.thread.join(timeout=5)


@pytest.fixture
def judge_env(monkeypatch):
    fake = CheckEngine()
    state = SimpleNamespace(engine=fake, alive=True, phone_slots=0, vision=True)
    state.process = SimpleNamespace(poll=lambda: None if state.alive else 1)
    gate, phone, lanes = CodingGate(slots=2), CodingGate(slots=2), LaneBook()
    state.coding_gate, state.phone_gate, state.lanes = gate, phone, lanes
    monkeypatch.setattr(coordinator, "coding_gate", gate)
    monkeypatch.setattr(coordinator, "phone_gate", phone)
    monkeypatch.setattr(coordinator, "lane_book", lanes)
    monkeypatch.setattr(coordinator, "is_swapping_models", False)
    monkeypatch.setattr(coordinator, "is_recalculating_queue", False)
    monkeypatch.setattr(coordinator, "_engine_http", None)
    monkeypatch.setattr(coordinator.manager, "get_active_model_names", lambda: [MODEL] if state.engine else [])

    async def snapshot(_name):
        return {"process": state.process, "port": fake.port, "safe_context": 65536, "dynamic_slots": 2,
                "phone_slots": state.phone_slots, "phone_context": 8192, "mmproj_path": "/m.gguf", "started_at": 1.0}

    async def has_vision(_snapshot):
        return state.vision

    async def phone_engine():
        return (await snapshot(MODEL)) if state.phone_slots else None

    monkeypatch.setattr(coordinator.manager, "asnapshot_model", snapshot)
    monkeypatch.setattr(coordinator, "_snapshot_has_vision", has_vision)
    monkeypatch.setattr(coordinator, "_phone_engine", phone_engine)
    yield state
    fake.stop()


TEXT = image_policy.text_check_messages("a red fox")
PICTURE = image_policy.image_check_messages(sd_png())


@pytest.mark.anyio
async def test_a_check_asks_the_engine_for_a_deterministic_short_uncached_answer(judge_env):
    answer = await coordinator._image_judge("ide", TEXT)
    assert answer == YES
    (body,) = judge_env.engine.bodies
    assert body["messages"] == TEXT and body["stream"] is False and body["temperature"] == 0
    assert body["max_tokens"] == coordinator.IMAGE_JUDGE_MAX_TOKENS and body["cache_prompt"] is False
    assert "id_slot" not in body                                    # no phone lanes: an engine without them is addressed as always
    assert judge_env.coding_gate.stats()["in_use"] == 0             # the slot is given back


@pytest.mark.anyio
async def test_the_picture_check_sends_the_image_to_the_engine(judge_env):
    await coordinator._image_judge("ide", PICTURE)
    part = judge_env.engine.bodies[0]["messages"][1]["content"][1]
    assert part["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.anyio
async def test_a_check_takes_a_coding_slot_at_the_lowest_priority(judge_env):
    await judge_env.coding_gate.acquire(0)
    await judge_env.coding_gate.acquire(0)                          # both slots busy with coding chats
    task = asyncio.create_task(coordinator._image_judge("ide", TEXT))
    await asyncio.sleep(0.1)
    assert judge_env.coding_gate.stats()["waiting_by_priority"] == {"2": 1} and judge_env.engine.bodies == []
    judge_env.coding_gate.release()
    assert await task == YES
    judge_env.coding_gate.release()


@pytest.mark.anyio
async def test_with_phone_lanes_a_phone_check_takes_a_phone_lane_and_never_a_coding_slot(judge_env):
    judge_env.phone_slots = 2
    answer = await coordinator._image_judge("app", TEXT)
    assert answer == YES and judge_env.engine.bodies[0]["id_slot"] in (2, 3)    # after the engine's two coding lanes
    assert judge_env.phone_gate.stats()["in_use"] == 0 and judge_env.coding_gate.stats()["in_use"] == 0
    assert all(uses == 0 for uses in judge_env.lanes._uses.values())            # the lane is given back


@pytest.mark.anyio
async def test_with_phone_lanes_an_ide_check_is_pinned_to_a_coding_lane(judge_env):
    judge_env.phone_slots = 2
    await coordinator._image_judge("ide", TEXT)
    assert judge_env.engine.bodies[0]["id_slot"] in (0, 1) and judge_env.phone_gate.stats()["in_use"] == 0


@pytest.mark.anyio
async def test_without_phone_lanes_a_phone_check_follows_the_coding_path(judge_env):
    await coordinator._image_judge("app", TEXT)
    assert judge_env.engine.bodies and judge_env.phone_gate.stats()["in_use"] == 0


@pytest.mark.anyio
async def test_the_slots_are_given_back_when_the_engine_fails(judge_env):
    judge_env.engine.status = 500
    with pytest.raises(JudgeUnavailable):
        await coordinator._image_judge("ide", TEXT)
    judge_env.phone_slots = 2
    with pytest.raises(JudgeUnavailable):
        await coordinator._image_judge("app", TEXT)
    assert judge_env.coding_gate.stats()["in_use"] == 0 and judge_env.phone_gate.stats()["in_use"] == 0


@pytest.mark.anyio
@pytest.mark.parametrize("cause", ["swapping", "no engine", "dead process", "no vision for a picture", "port closed"])
async def test_an_engine_that_cannot_be_asked_is_unavailable_with_the_restarting_sentence(judge_env, monkeypatch, cause):
    messages = TEXT
    if cause == "swapping":
        monkeypatch.setattr(coordinator, "is_swapping_models", True)
    elif cause == "no engine":
        monkeypatch.setattr(coordinator.manager, "get_active_model_names", lambda: [])
    elif cause == "dead process":
        judge_env.alive = False
    elif cause == "no vision for a picture":
        judge_env.vision = False
        messages = PICTURE
    else:
        judge_env.engine.stop()
    with pytest.raises(JudgeUnavailable) as caught:
        await coordinator._image_judge("ide", messages)
    if cause != "no vision for a picture":
        assert caught.value.detail == "DLNE is restarting, try again in a moment."
    assert judge_env.coding_gate.stats()["in_use"] == 0


@pytest.mark.anyio
async def test_no_slot_in_time_is_unavailable_not_a_hang(judge_env, monkeypatch):
    monkeypatch.setattr(coordinator, "IMAGE_JUDGE_WAIT_SECONDS", 0.1)
    await judge_env.coding_gate.acquire(0)
    await judge_env.coding_gate.acquire(0)
    with pytest.raises(JudgeUnavailable) as caught:
        await coordinator._image_judge("ide", TEXT)
    assert "try again" in caught.value.detail and caught.value.code == "engine_busy"
    assert judge_env.coding_gate.stats()["waiting"] == 0


@pytest.mark.anyio
async def test_an_engine_answer_that_cannot_be_read_comes_back_empty_so_the_check_refuses(judge_env):
    judge_env.engine.answer = None
    assert await coordinator._image_judge("ide", TEXT) == ""
    assert image_policy.parse_verdict("").allowed is False
