"""
Image jobs (engine/image_pool.py): the queue, the pipeline's endings, who may see what, the hourly cap, expiry, estimates.
The image engine and the coordinator's model are stand-ins here; the engine's lifecycle is tests/test_image_member.py.
"""

import asyncio
import base64
import os
import re

import pytest

from engine import image_policy, media
from engine.image_member import Cancelled, MemberError
from engine.image_pool import ENGINE_RESTARTING, ImageService, JudgeUnavailable, Rejected, SEED_ETA_SECONDS
from engine.priority import tier_priority
from tests.image_fakes import sd_png

YES = '{"allowed": true, "category": "none"}'
NO = '{"allowed": false, "category": "violence"}'
A, B = "203.0.113.9", "198.51.100.20"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeMember:
    """The image engine as the service uses it."""

    def __init__(self):
        self.calls = []
        self.running = 0
        self.most_running = 0
        self.hold = False
        self.release = None
        self.error = None
        self.reason = None
        self.png = sd_png(8, 8, "the secret prompt")
        self.unloads = 0
        self.stopped = False
        self.entered = None

    def unavailable_reason(self):
        return self.reason

    async def generate(self, prompt, width, height, seed, cancelled=lambda: False, on_generating=None):
        self.calls.append({"prompt": prompt, "width": width, "height": height, "seed": seed})
        self.running += 1
        self.most_running = max(self.most_running, self.running)
        try:
            if self.entered is not None:
                self.entered.set()
            while self.hold and not self.release.is_set():
                if cancelled():
                    raise Cancelled()
                await asyncio.sleep(0.005)
            if self.error is not None:
                raise self.error
            return self.png
        finally:
            self.running -= 1

    async def maybe_unload(self):
        self.unloads += 1
        return False

    def stop_now(self):
        self.stopped = True


class Judge:
    def __init__(self):
        self.calls = []
        self.text_answer = YES
        self.image_answer = YES
        self.error = None

    async def __call__(self, kind, messages):
        self.calls.append((kind, messages))
        if self.error is not None:
            raise self.error
        return self.image_answer if media.has_images(messages) else self.text_answer


class Env:
    pass


@pytest.fixture
def env():
    e = Env()
    e.limits = {"image_queue_max": 3, "image_per_hour": 4, "image_result_ttl_seconds": 600}
    e.member, e.judge, e.clock = FakeMember(), Judge(), Clock()
    e.service = ImageService(e.member, e.judge, limits=lambda: e.limits, clock=e.clock, housekeeping=0.05)
    e.refunds = []
    return e


def submit(e, owner=A, kind="app", prompt="a red fox", size="512x512", refund=True, **kwargs):
    return e.service.submit(owner, kind, prompt, size, (lambda: e.refunds.append(prompt)) if refund else None, **kwargs)


async def until(predicate, timeout=5.0):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("timed out waiting")


async def done(e, job):
    await until(lambda: job.status in ("completed", "refused", "failed", "cancelled", "expired"))
    return e.service.view(job)


# --- the whole pipeline ---------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_a_job_runs_the_checks_draws_strips_and_completes(env):
    job = submit(env, prompt="the secret prompt", size="768x512")
    assert job.status == "queued"
    view = await done(env, job)

    assert view["status"] == "completed" and "error" not in view
    image = view["image"]
    assert (image["width"], image["height"], image["seed"]) == (768, 512, job.seed)
    png = base64.b64decode(image["b64_png"])
    assert png.startswith(b"\x89PNG") and b"secret" not in png and b"parameters" not in png     # metadata stripped
    assert env.member.calls == [{"prompt": "the secret prompt", "width": 768, "height": 512, "seed": job.seed}]
    assert job.prompt is None                                       # not kept once the picture is drawn


@pytest.mark.anyio
async def test_the_model_is_asked_about_the_prompt_and_then_about_the_stripped_picture(env):
    job = submit(env, prompt="the secret prompt", kind="ide")
    await done(env, job)
    (kind1, text_check), (kind2, image_check) = env.judge.calls
    assert kind1 == kind2 == "ide"
    assert "the secret prompt" in text_check[1]["content"] and not media.has_images(text_check)
    url = image_check[1]["content"][1]["image_url"]["url"]
    sent = base64.b64decode(url.split(",", 1)[1])
    assert b"secret" not in sent and b"parameters" not in sent        # the model sees the cleaned picture, not sd's file


@pytest.mark.anyio
async def test_statuses_go_queued_checking_generating_reviewing_completed(env):
    seen = []
    env.member.hold, env.member.release = True, asyncio.Event()
    job = submit(env)
    original = env.judge.__call__

    async def watching(kind, messages):
        seen.append(job.status)
        return await original(kind, messages)

    env.service._judge = watching
    await until(lambda: job.status == "generating")
    seen.append(job.status)
    env.member.release.set()
    await done(env, job)
    assert seen == ["checking", "generating", "reviewing"]


@pytest.mark.anyio
async def test_the_seed_is_random_and_ids_are_128_bit_url_safe_and_all_different(env):
    env.limits["image_per_hour"] = 60
    jobs = []
    for _ in range(3):
        jobs.append(submit(env))
        await done(env, jobs[-1])
    for job in jobs:
        assert re.fullmatch(r"[A-Za-z0-9_-]{22}", job.id)            # 16 random bytes
        assert 0 <= job.seed < 2 ** 31
    assert len({j.id for j in jobs}) == 3 and len({j.seed for j in jobs}) == 3


# --- one at a time, in order -------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_jobs_run_one_at_a_time_in_the_order_they_came(env):
    env.member.hold, env.member.release = True, asyncio.Event()
    jobs = [submit(env, owner=f"10.0.0.{i}", prompt=f"p{i}") for i in range(3)]
    await until(lambda: len(env.member.calls) == 1)
    await asyncio.sleep(0.05)
    assert len(env.member.calls) == 1 and [j.status for j in jobs] == ["generating", "queued", "queued"]
    env.member.release.set()
    for job in jobs:
        await done(env, job)
    assert [c["prompt"] for c in env.member.calls] == ["p0", "p1", "p2"] and env.member.most_running == 1


@pytest.mark.anyio
async def test_a_waiting_key_holders_job_runs_before_a_trials_and_the_running_one_is_not_preempted(env):
    env.member.hold, env.member.release = True, asyncio.Event()
    env.limits["image_queue_max"] = 6
    env.limits["image_per_hour"] = 60
    running = submit(env, owner="10.0.0.1", prompt="running", priority=tier_priority("trial"))
    await until(lambda: running.status == "generating")
    trial_early = submit(env, owner="10.0.0.2", prompt="trial early", priority=tier_priority("trial"))
    cpu = submit(env, owner="10.0.0.3", prompt="cpu", priority=tier_priority("cpu"))
    key = submit(env, owner="10.0.0.4", prompt="key", priority=tier_priority("key"))
    host = submit(env, owner="10.0.0.5", prompt="host", priority=tier_priority("host"))
    gpu = submit(env, owner="10.0.0.6", prompt="gpu", priority=tier_priority("gpu"))
    assert running.status == "generating"                    # nobody pre-empts a running job
    # places: key, host and gpu share the first tier and keep their arrival order, then cpu, then the trial
    assert [env.service.view(j)["position"] for j in (key, host, gpu, cpu, trial_early)] == [2, 3, 4, 5, 6]
    env.member.release.set()
    for job in (running, key, host, gpu, cpu, trial_early):
        await done(env, job)
    assert [c["prompt"] for c in env.member.calls] == ["running", "key", "host", "gpu", "cpu", "trial early"]


@pytest.mark.anyio
async def test_jobs_of_one_tier_keep_their_arrival_order_and_a_job_without_a_tier_is_last(env):
    env.member.hold, env.member.release = True, asyncio.Event()
    env.limits["image_queue_max"] = 6
    env.limits["image_per_hour"] = 60
    running = submit(env, owner="10.0.0.1", prompt="running")
    await until(lambda: running.status == "generating")
    plain = submit(env, owner="10.0.0.2", prompt="plain")
    keys = [submit(env, owner=f"10.0.1.{i}", prompt=f"key{i}", priority=tier_priority("key")) for i in range(2)]
    plain_late = submit(env, owner="10.0.0.3", prompt="plain late")
    env.member.release.set()
    for job in (running, plain, *keys, plain_late):
        await done(env, job)
    assert [c["prompt"] for c in env.member.calls] == ["running", "key0", "key1", "plain", "plain late"]


@pytest.mark.anyio
async def test_a_queued_job_knows_its_place_and_roughly_how_long(env):
    env.member.hold, env.member.release = True, asyncio.Event()
    first, second, third = (submit(env, owner=f"10.0.0.{i}") for i in range(3))
    await until(lambda: first.status == "generating")
    assert env.service.view(second)["position"] == 2 and env.service.view(third)["position"] == 3
    assert "position" not in env.service.view(first)
    # the second waits out the first (18 s measured), then its own 18 s; the third waits for both
    assert env.service.view(second)["eta_seconds"] > env.service.view(first)["eta_seconds"] - 1
    assert env.service.view(third)["eta_seconds"] > env.service.view(second)["eta_seconds"]
    env.member.release.set()


@pytest.mark.anyio
async def test_a_full_queue_says_busy_and_when_to_come_back(env):
    env.member.hold, env.member.release = True, asyncio.Event()
    env.limits["image_per_hour"] = 60
    first = submit(env, owner="10.0.0.1")
    await until(lambda: first.status == "generating")
    for i in range(3):                               # the queue holds three (IMAGE_QUEUE_MAX) besides the running job
        submit(env, owner=f"10.0.1.{i}")
    with pytest.raises(Rejected) as caught:
        submit(env, owner="10.0.2.1")
    assert caught.value.code == "image_busy" and 5 <= caught.value.retry_after <= 120
    env.member.release.set()


# --- ownership -------------------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_only_the_network_that_made_a_job_and_the_endpoint_family_can_see_it(env):
    job = submit(env, owner=A, kind="app")
    assert env.service.get(A, "app", job.id) is job
    assert env.service.get(B, "app", job.id) is None
    assert env.service.get(A, "ide", job.id) is None
    assert env.service.get(A, "app", "no-such-id") is None
    await done(env, job)


# --- the hourly cap ----------------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_each_network_has_its_own_hourly_cap(env):
    env.limits["image_queue_max"] = 16
    for _ in range(4):
        submit(env, owner=A)
    with pytest.raises(Rejected) as caught:
        submit(env, owner=A)
    assert caught.value.code == "image_quota" and "4 pictures an hour" in caught.value.detail
    assert 0 < caught.value.retry_after <= 3601
    submit(env, owner=B)                                              # another network is not affected


@pytest.mark.anyio
async def test_the_hour_is_a_sliding_window(env):
    env.limits.update(image_queue_max=16)
    for _ in range(4):
        job = submit(env, owner=A)
        await done(env, job)
    with pytest.raises(Rejected):
        submit(env, owner=A)
    env.clock.t += 3601
    submit(env, owner=A)


@pytest.mark.anyio
async def test_a_job_that_never_got_going_gives_its_hour_back_and_a_refused_one_does_not(env):
    env.limits.update(image_per_hour=1)
    env.member.hold, env.member.release = True, asyncio.Event()
    blocker = submit(env, owner="10.0.0.1")
    await until(lambda: blocker.status == "generating")
    waiting = submit(env, owner=A)
    env.service.cancel(waiting)                                       # cancelled while it queued
    submit(env, owner=A)                                              # the hour was given back
    env.member.release.set()

    env.limits.update(image_per_hour=1)
    env.judge.text_answer = NO
    refused = submit(env, owner=B)
    assert (await done(env, refused))["status"] == "refused"
    with pytest.raises(Rejected):
        submit(env, owner=B)                                          # a refusal is not free: no probing the check


# --- cancelling -------------------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_cancelling_a_queued_job_removes_it_and_refunds_once(env):
    env.member.hold, env.member.release = True, asyncio.Event()
    first = submit(env, owner="10.0.0.1")
    await until(lambda: first.status == "generating")
    queued = submit(env, owner=A, prompt="queued one")
    env.service.cancel(queued)
    env.service.cancel(queued)
    assert env.service.view(queued)["status"] == "cancelled" and env.refunds == ["queued one"]
    env.member.release.set()
    await done(env, first)
    assert [c["prompt"] for c in env.member.calls] == ["a red fox"]       # the cancelled one was never drawn


@pytest.mark.anyio
async def test_cancelling_a_job_being_drawn_stops_it_and_the_next_one_goes_ahead(env):
    env.member.hold, env.member.release = True, asyncio.Event()
    first = submit(env, owner="10.0.0.1", prompt="first")
    await until(lambda: first.status == "generating")
    second = submit(env, owner="10.0.0.2", prompt="second")
    env.service.cancel(first)
    assert env.service.view(first)["status"] == "cancelled"
    env.member.hold = False
    view = await done(env, second)
    assert view["status"] == "completed" and env.refunds == ["first"]
    assert env.service.view(first)["status"] == "cancelled" and "image" not in env.service.view(first)


@pytest.mark.anyio
async def test_a_finished_job_cannot_be_cancelled_into_something_else(env):
    job = submit(env)
    await done(env, job)
    env.service.cancel(job)
    assert job.status == "completed"


# --- every way a job can end -----------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_a_prompt_the_model_refuses_is_refused_with_its_category_and_never_drawn(env):
    env.judge.text_answer = NO
    job = submit(env)
    view = await done(env, job)
    assert view["status"] == "refused" and "image" not in view
    assert view["error"] == {"detail": image_policy.MESSAGES["violence"], "code": "prompt_refused", "category": "violence"}
    assert env.member.calls == [] and env.refunds == []


@pytest.mark.anyio
@pytest.mark.parametrize("answer", ["", "sure!", "{broken", '{"allowed": "yes"}', '{"allowed": true, "category": "sexual"}'])
async def test_an_unreadable_or_contradictory_verdict_refuses_the_prompt(env, answer):
    env.judge.text_answer = answer
    view = await done(env, submit(env))
    assert view["status"] == "refused" and env.member.calls == []


@pytest.mark.anyio
async def test_a_picture_the_model_refuses_is_dropped(env):
    env.judge.image_answer = NO
    job = submit(env)
    view = await done(env, job)
    assert view["status"] == "refused" and "image" not in view and job.png is None
    assert view["error"]["code"] == "image_refused" and view["error"]["detail"] == image_policy.IMAGE_REFUSED_MESSAGE


@pytest.mark.anyio
async def test_an_unreadable_verdict_on_the_picture_drops_it_too(env):
    env.judge.image_answer = "I like it"
    view = await done(env, submit(env))
    assert view["status"] == "refused" and "image" not in view


@pytest.mark.anyio
async def test_the_model_being_unavailable_fails_the_job_kindly_and_refunds(env):
    env.judge.error = JudgeUnavailable()
    job = submit(env)
    view = await done(env, job)
    assert view["status"] == "failed" and view["error"] == {"detail": ENGINE_RESTARTING, "code": "engine_restarting"}
    assert env.refunds == ["a red fox"]
    env.limits["image_per_hour"] = 4
    for _ in range(3):
        submit(env)                                                   # the hour was given back: three more fit with this one's
    with pytest.raises(Rejected):
        submit(env)


@pytest.mark.anyio
async def test_the_model_going_away_after_the_picture_fails_the_job_and_throws_the_picture_away(env):
    answers = iter([None, JudgeUnavailable("Back in a moment.")])
    original = env.judge.__call__

    async def flaky(kind, messages):
        failure = next(answers)
        if failure:
            raise failure
        return await original(kind, messages)

    env.service._judge = flaky
    job = submit(env)
    view = await done(env, job)
    assert view["status"] == "failed" and view["error"]["detail"] == "Back in a moment." and job.png is None


@pytest.mark.anyio
async def test_an_image_engine_failure_fails_the_job_with_its_code_and_refunds(env):
    env.member.error = MemberError("generation_timeout", "Too slow.")
    job = submit(env)
    view = await done(env, job)
    assert view["status"] == "failed" and view["error"] == {"detail": "Too slow.", "code": "generation_timeout"}
    assert env.refunds == ["a red fox"]


@pytest.mark.anyio
async def test_a_picture_that_is_not_a_png_fails_the_job(env):
    env.member.png = b"definitely not a png"
    view = await done(env, submit(env))
    assert view["status"] == "failed" and view["error"]["code"] == "generation_failed"


@pytest.mark.anyio
async def test_a_bug_in_one_job_does_not_end_the_worker(env):
    env.member.error = RuntimeError("boom")
    first = submit(env, owner="10.0.0.1")
    assert (await done(env, first))["status"] == "failed"
    env.member.error = None
    assert (await done(env, submit(env, owner="10.0.0.2")))["status"] == "completed"


# --- time -----------------------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_a_finished_job_is_kept_for_the_ttl_then_expired_then_forgotten(env):
    job = submit(env)
    await done(env, job)
    env.clock.t += 599
    assert env.service.get(A, "app", job.id) is job and env.service.view(job)["status"] == "completed"
    env.clock.t += 2
    got = env.service.get(A, "app", job.id)
    view = env.service.view(job)
    assert got is job and view["status"] == "expired" and set(view) == {"id", "status", "elapsed_seconds"}
    assert job.png is None
    env.clock.t += 600
    assert env.service.get(A, "app", job.id) is None


@pytest.mark.anyio
async def test_the_ttl_follows_the_setting(env):
    env.limits["image_result_ttl_seconds"] = 60
    job = submit(env)
    await done(env, job)
    env.clock.t += 61
    env.service.get(A, "app", job.id)
    assert job.status == "expired"


@pytest.mark.anyio
async def test_refused_and_failed_jobs_expire_like_the_rest(env):
    env.judge.text_answer = NO
    job = submit(env)
    await done(env, job)
    env.clock.t += 601
    env.service.get(A, "app", job.id)
    assert job.status == "expired" and job.error is None


@pytest.mark.anyio
async def test_elapsed_is_frozen_when_the_job_ends(env):
    job = submit(env)
    await done(env, job)
    first = env.service.view(job)["elapsed_seconds"]
    env.clock.t += 300
    assert env.service.view(job)["elapsed_seconds"] == first


@pytest.mark.anyio
async def test_an_idle_worker_gives_the_engine_its_idle_check(env):
    submit(env)
    await until(lambda: env.member.unloads >= 1)


# --- estimates and the status block -----------------------------------------------------------------------------

def test_estimates_start_at_the_measured_figures(env):
    # FLUX.2 [klein] 4B on the RX 580 with its prompt reader on the card too (2026-10-30): 26 s square, 31 s the others.
    assert SEED_ETA_SECONDS == {"512x512": 26, "512x768": 31, "768x512": 31}
    assert set(SEED_ETA_SECONDS) == set(image_policy.SIZES)
    assert env.service.eta("512x512") == 26 and env.service.eta("512x768") == env.service.eta("768x512") == 31
    assert env.service.eta("an unknown size") == 26                  # the default size's figure


def test_estimates_follow_the_median_of_real_jobs_per_size(env):
    for seconds in (20, 18, 100, 19, 17):
        env.service._samples["512x512"].append(seconds)
    assert env.service.eta("512x512") == 19                           # one slow outlier does not move it
    assert env.service.eta("768x512") == 31                           # other sizes keep their own
    for seconds in range(9):
        env.service._samples["512x512"].append(15)                    # only the last nine count
    assert env.service.eta("512x512") == 15


@pytest.mark.anyio
async def test_real_jobs_feed_the_estimate(env):
    env.member.hold, env.member.release = True, asyncio.Event()
    job = submit(env)
    await until(lambda: job.status == "generating")
    env.clock.t += 40
    env.member.release.set()
    await done(env, job)
    assert env.service.eta("512x512") == 40


@pytest.mark.anyio
async def test_the_status_block_has_exactly_the_contracts_fields(env):
    block = env.service.status_block()
    assert block == {"queue": 0, "busy": False, "eta_seconds": {"512x512": 26, "512x768": 31, "768x512": 31},
                     "sizes": ["512x512", "512x768", "768x512"], "default_size": "512x512", "per_hour": 4}
    env.limits["image_per_hour"] = 9
    assert env.service.status_block()["per_hour"] == 9                # read live, never typed
    env.member.hold, env.member.release = True, asyncio.Event()
    env.limits["image_per_hour"] = 60
    a = submit(env, owner="10.0.0.1")
    await until(lambda: a.status == "generating")
    submit(env, owner="10.0.0.2")
    assert env.service.status_block()["queue"] == 1 and env.service.status_block()["busy"] is True
    env.member.release.set()


# --- nothing is kept or written that should not be --------------------------------------------------------------

@pytest.mark.anyio
async def test_logs_carry_the_length_and_a_hash_but_never_the_prompt(env, caplog, capsys):
    caplog.set_level("DEBUG")
    prompt = "zebra-umbrella-xylophone secret words"
    job = submit(env, prompt=prompt)
    await done(env, job)
    text = caplog.text + capsys.readouterr().out
    assert "zebra" not in text and "xylophone" not in text
    assert image_policy.prompt_tag(prompt) in text


def test_no_image_module_writes_a_file():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("image_pool.py", "image_policy.py"):
        source = open(os.path.join(root, "engine", name), encoding="utf-8").read()
        for forbidden in ("open(", "write_bytes", ".write(", "tempfile", "os.makedirs"):
            assert forbidden not in source, (name, forbidden)
    member = open(os.path.join(root, "engine", "image_member.py"), encoding="utf-8").read()
    assert "open(" not in member and "write_bytes" not in member and member.count("mkdtemp") == 1   # only sd's empty cwd


@pytest.mark.anyio
async def test_shutdown_stops_the_worker_and_the_engine(env):
    submit(env)
    await env.service.shutdown()
    assert env.member.stopped is True
