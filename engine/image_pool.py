"""
Image jobs (2.6.1): who asked for what, in what order, and what became of it.

A job is created by an endpoint (engine/coordinator.py), waits in one FIFO queue, and is then taken by one worker that
runs it, alone, through the whole pipeline. Each step can end it, and each ending is a status the client reads:

    queued -> checking -> generating -> reviewing -> completed
                 |            |             |
              refused      failed        refused       (cancelled can interrupt any of them)

    checking    the prompt is read by the coordinator's own model (engine/image_policy.py)
    generating  sd-server draws it (engine/image_member.py)
    reviewing   the picture's metadata is stripped and the coordinator's vision model looks at it

A finished job is kept ``IMAGE_RESULT_TTL_SECONDS`` and is then ``expired``; an expired one is forgotten after the same
time again. The PNG is held in memory only, and so is the prompt, until the picture is drawn. Nothing is written to disk.

A job belongs to the network that created it (a random 128-bit id, and ``owner``): anyone else asking for it is told it
does not exist. Each network may start ``IMAGE_PER_HOUR`` jobs an hour.

All of this runs on the coordinator's one event loop, so there are no locks: every method that changes state is either
synchronous or changes it before its first await.
"""

import asyncio
import base64
import secrets
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional, Tuple

from engine import image_policy, media
from engine.config import get_worker_limits
from engine.image_member import Cancelled, ImageMember, MemberError, UNAVAILABLE
from engine.logger import log_coordinator, log_error, log_warning

QUEUED, CHECKING, GENERATING, REVIEWING = "queued", "checking", "generating", "reviewing"
COMPLETED, REFUSED, FAILED, CANCELLED, EXPIRED = "completed", "refused", "failed", "cancelled", "expired"
TERMINAL = frozenset({COMPLETED, REFUSED, FAILED, CANCELLED, EXPIRED})

# The seconds a picture takes on the RX 580 with the model kept loaded (measured on TheWall, 2026-10-28), until real
# jobs give a better figure. The two sizes in between are placed by their pixel count.
SEED_ETA_SECONDS = {"768x768": 80, "1024x1024": 153, "768x1024": 111, "1024x768": 111}
MEDIAN_OF = 9                   # the last this many real jobs of a size
HOUSEKEEPING_SECONDS = 30.0     # how often an idle worker looks at expiry and the idle unload

ENGINE_RESTARTING = "DLNE is restarting, try again in a moment."


class JudgeUnavailable(Exception):
    """The coordinator's model could not be asked (restarting, swapping, no vision, no slot in time)."""

    def __init__(self, detail: str = ENGINE_RESTARTING, code: str = "engine_restarting"):
        super().__init__(detail)
        self.detail, self.code = detail, code


# (kind, chat messages) -> the model's answer text. ``kind`` is "app" or "ide": which engine slot the check takes.
Judge = Callable[[str, List[Dict[str, Any]]], Awaitable[str]]


class Rejected(Exception):
    """A job is not accepted: 429 with ``code`` (image_quota or image_busy) and how many seconds to wait."""

    def __init__(self, code: str, detail: str, retry_after: int):
        super().__init__(detail)
        self.code, self.detail, self.retry_after = code, detail, retry_after


@dataclass
class Job:
    id: str
    owner: str
    kind: str                       # "app" (the phone endpoints) or "ide" (the coding-side ones)
    size: str
    width: int
    height: int
    seed: int
    prompt: Optional[str]           # dropped as soon as the picture is drawn, or the job ends
    created: float
    status: str = QUEUED
    began: Optional[float] = None   # when the worker took it
    finished: Optional[float] = None
    png: Optional[bytes] = None
    error: Optional[Dict[str, str]] = None
    cancel_requested: bool = False
    refund: Optional[Callable[[], None]] = None      # gives back what the job cost the caller beyond the hourly cap
    stamp: Optional[float] = None                    # its place in the owner's hourly count
    tag: str = ""                   # what a log may say about the prompt


class _Hourly:
    """Jobs started per network in the last hour."""

    def __init__(self) -> None:
        self._uses: Dict[str, Deque[float]] = {}

    def spend(self, owner: str, limit: int, now: float) -> Tuple[bool, float]:
        """``(allowed, stamp or seconds to wait)``: one use is recorded when allowed."""
        uses = self._uses.setdefault(owner, deque())
        while uses and uses[0] <= now - 3600:
            uses.popleft()
        if len(uses) >= limit:
            return False, uses[0] + 3600 - now
        uses.append(now)
        return True, now

    def refund(self, owner: str, stamp: float) -> None:
        uses = self._uses.get(owner)
        if uses and stamp in uses:
            uses.remove(stamp)

    def forget_idle(self, now: float) -> None:
        for owner in [o for o, u in self._uses.items() if not u or u[-1] <= now - 3600]:
            del self._uses[owner]


class ImageService:
    """The queue, the worker and the job store. One per coordinator."""

    def __init__(self, member: ImageMember, judge: Judge, limits: Callable[[], Dict[str, Any]] = get_worker_limits,
                 clock: Callable[[], float] = time.monotonic, housekeeping: float = HOUSEKEEPING_SECONDS) -> None:
        self.member, self._judge, self._limits, self._clock = member, judge, limits, clock
        self._housekeeping = housekeeping
        self._jobs: "OrderedDict[str, Job]" = OrderedDict()
        self._pending: Deque[Job] = deque()
        self.active: Optional[Job] = None
        self._hourly = _Hourly()
        self._samples: Dict[str, Deque[float]] = {size: deque(maxlen=MEDIAN_OF) for size in image_policy.SIZES}
        self._wake: Optional[asyncio.Event] = None
        self._worker: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ------------------------------------------------------------------ estimates
    def eta(self, size: str) -> int:
        """Seconds a picture of this size takes: the median of the last real jobs, the measured figure until there are any."""
        samples = sorted(self._samples.get(size) or ())
        if not samples:
            return SEED_ETA_SECONDS.get(size, SEED_ETA_SECONDS[image_policy.DEFAULT_SIZE])
        middle = len(samples) // 2
        return round(samples[middle] if len(samples) % 2 else (samples[middle - 1] + samples[middle]) / 2)

    def _remaining_of_active(self) -> int:
        if self.active is None or self.active.began is None:
            return 0
        return max(5, self.eta(self.active.size) - int(self._clock() - self.active.began))

    def _wait_ahead(self, job: Job) -> int:
        """Seconds until ``job`` starts, as far as can be told: the running job's remainder and every job queued before it."""
        wait = self._remaining_of_active()
        for other in self._pending:
            if other is job:
                break
            wait += self.eta(other.size)
        return wait

    def status_block(self) -> Dict[str, Any]:
        """The ``image_generation`` block of /v1/cluster/status (counts and estimates only; nothing names a network)."""
        return {"queue": len(self._pending), "busy": self.active is not None,
                "eta_seconds": {"768x768": self.eta("768x768"), "1024x1024": self.eta("1024x1024")},
                "sizes": list(image_policy.SIZES), "default_size": image_policy.DEFAULT_SIZE,
                "per_hour": int(self._limits().get("image_per_hour", 6))}

    # ------------------------------------------------------------------ the front door
    def submit(self, owner: str, kind: str, prompt: str, size: str,
               refund: Optional[Callable[[], None]] = None) -> Job:
        """
        Queue a job for a cleaned, term-checked prompt. Raises :class:`Rejected` when the queue is full or the network has
        used its hour. ``refund`` is called once if the job ends without a picture through no fault of the prompt.
        """
        limits = self._limits()
        now = self._clock()
        self._sweep(now)
        if len(self._pending) >= int(limits.get("image_queue_max", 4)):
            raise Rejected("image_busy", "Lots of people are making pictures right now. Please try again in a moment.",
                           max(5, min(120, self._remaining_of_active())))
        allowed, value = self._hourly.spend(owner, int(limits.get("image_per_hour", 6)), now)
        if not allowed:
            raise Rejected("image_quota", f"You've reached the limit of {int(limits.get('image_per_hour', 6))} pictures an hour. "
                           "Please try again a little later.", max(1, int(value) + 1))
        width, height = image_policy.SIZES[size]
        job = Job(id=secrets.token_urlsafe(16), owner=owner, kind=kind, size=size, width=width, height=height,
                  seed=secrets.randbelow(2 ** 31), prompt=prompt, created=now, refund=refund, stamp=value,
                  tag=image_policy.prompt_tag(prompt))
        self._jobs[job.id] = job
        self._pending.append(job)
        self._ensure_worker()
        self._wake.set()
        log_coordinator(f"Image job queued: {job.tag} {size}, {kind}.")
        return job

    def get(self, owner: str, kind: str, job_id: str) -> Optional[Job]:
        """The job, only for the network and the endpoint family that created it."""
        self._sweep(self._clock())
        job = self._jobs.get(job_id)
        return job if job is not None and job.owner == owner and job.kind == kind else None

    def cancel(self, job: Job) -> Job:
        """Stop a job. A queued one is gone at once; a running one stops at the next step. A finished one is left as it is."""
        if job.status in TERMINAL:
            return job
        job.cancel_requested = True
        if job.status == QUEUED:
            try:
                self._pending.remove(job)
            except ValueError:
                pass
        self._finish(job, CANCELLED, refund=True, hourly_back=job.status in (QUEUED, CHECKING))
        return job

    def view(self, job: Job) -> Dict[str, Any]:
        """The job as the GET endpoints answer it."""
        now = self._clock()
        end = job.finished if job.finished is not None else now
        answer: Dict[str, Any] = {"id": job.id, "status": job.status, "elapsed_seconds": max(0, int(end - job.created))}
        if job.status == QUEUED:
            answer["position"] = 1 + (1 if self.active is not None else 0) + self._index_of(job)
        if job.status in (QUEUED, CHECKING, GENERATING, REVIEWING):
            # The whole time from submission to the picture: what it has waited, plus what is still to come.
            waited = (job.began if job.began is not None else now) - job.created
            expected = waited + (self._wait_ahead(job) if job.status == QUEUED else 0) + self.eta(job.size)
            answer["eta_seconds"] = max(int(expected), answer["elapsed_seconds"] + 5)
        if job.status == COMPLETED and job.png is not None:
            answer["image"] = {"b64_png": base64.b64encode(job.png).decode("ascii"), "width": job.width,
                               "height": job.height, "seed": job.seed}
        if job.error:
            answer["error"] = dict(job.error)
        return answer

    def _index_of(self, job: Job) -> int:
        for index, other in enumerate(self._pending):
            if other is job:
                return index
        return 0

    def eta_of(self, job: Job) -> int:
        return self.view(job).get("eta_seconds", self.eta(job.size))

    def position_of(self, job: Job) -> int:
        return self.view(job).get("position", 1)

    # ------------------------------------------------------------------ job endings
    def _finish(self, job: Job, status: str, error: Optional[Dict[str, str]] = None, refund: bool = False,
                hourly_back: bool = False) -> None:
        """The one place a job ends. A job that already ended (cancelled while a step was running) stays as it was."""
        if job.status in TERMINAL:
            return
        job.status, job.error, job.finished = status, error, self._clock()
        job.prompt = None
        if status != COMPLETED:
            job.png = None
        if refund and job.refund is not None:
            callback, job.refund = job.refund, None
            try:
                callback()
            except Exception as exc:      # noqa: BLE001 - a refund that fails must not break the job's ending
                log_warning(f"Image job refund failed ({type(exc).__name__}).")
        job.refund = None
        if hourly_back and job.stamp is not None:
            self._hourly.refund(job.owner, job.stamp)
        log_coordinator(f"Image job {status}: {job.tag} {job.size}"
                        + (f", {error['code']}" if error else "") + f", {job.finished - job.created:.0f} s.")

    def _sweep(self, now: float) -> None:
        """Finished jobs become ``expired`` after the TTL (their picture and words dropped), and are forgotten after it again."""
        ttl = int(self._limits().get("image_result_ttl_seconds", 1800))
        for job_id, job in list(self._jobs.items()):
            if job.finished is None:
                continue
            age = now - job.finished
            if job.status != EXPIRED and age >= ttl:
                job.status, job.png, job.error, job.prompt = EXPIRED, None, None, None
            if age >= 2 * ttl:
                del self._jobs[job_id]
        self._hourly.forget_idle(now)

    # ------------------------------------------------------------------ the worker
    def _ensure_worker(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:                 # a new event loop (a restarted server, a test): nothing of the old one is live
            self._loop, self._wake, self._worker = loop, asyncio.Event(), None
        if self._worker is None or self._worker.done():
            self._worker = loop.create_task(self._run(), name="image-worker")

    async def shutdown(self) -> None:
        """Stop the worker and sd-server (the coordinator's shutdown)."""
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()
        self.member.stop_now()

    async def _next(self) -> Optional[Job]:
        while not self._pending:
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), self._housekeeping)
            except asyncio.TimeoutError:
                self._sweep(self._clock())
                try:
                    await self.member.maybe_unload()
                except Exception as exc:      # noqa: BLE001 - housekeeping must not end the worker
                    log_warning(f"Image engine idle check failed ({type(exc).__name__}).")
        return self._pending.popleft()

    async def _run(self) -> None:
        while True:
            job = await self._next()
            if job.status != QUEUED:           # cancelled while it waited
                continue
            self.active = job
            try:
                await self._process(job)
            except asyncio.CancelledError:
                self._finish(job, FAILED, {"detail": ENGINE_RESTARTING, "code": "engine_restarting"})
                raise
            except Exception as exc:          # noqa: BLE001 - one job's bug must not take the worker with it
                log_error(f"Image job crashed: {type(exc).__name__}.")
                self._finish(job, FAILED, {"detail": "Something went wrong making the picture. Please try again.",
                                           "code": "generation_failed"}, refund=True, hourly_back=True)
            finally:
                job.prompt = None
                self.active = None

    def _go(self, job: Job, status: str) -> bool:
        """Move a running job to its next step; False when it was cancelled (or ended) meanwhile."""
        if job.cancel_requested or job.status in TERMINAL:
            return False
        job.status = status
        return True

    async def _process(self, job: Job) -> None:
        job.began = self._clock()
        if not self._go(job, CHECKING):
            return
        # 1. The prompt, read by the coordinator's own model.
        try:
            verdict = image_policy.parse_verdict(await self._judge(job.kind, image_policy.text_check_messages(job.prompt or "")))
        except JudgeUnavailable as exc:
            return self._finish(job, FAILED, {"detail": exc.detail, "code": exc.code}, refund=True, hourly_back=True)
        if job.cancel_requested:
            return
        if not verdict.allowed:
            return self._finish(job, REFUSED, {"detail": image_policy.refusal_message(verdict.category),
                                               "code": "prompt_refused", "category": verdict.category})
        # 2. The picture.
        if not self._go(job, GENERATING):
            return
        try:
            png = await self.member.generate(job.prompt or "", job.width, job.height, job.seed,
                                             cancelled=lambda: job.cancel_requested)
        except Cancelled:
            return self._finish(job, CANCELLED, refund=True)
        except MemberError as exc:
            return self._finish(job, FAILED, {"detail": exc.detail, "code": exc.code}, refund=True,
                                hourly_back=exc.code in (UNAVAILABLE,))
        job.prompt = None                      # drawn: the words are not needed again
        if job.cancel_requested:
            return
        # 3. Its metadata off, and the coordinator's vision model's look at it.
        if not self._go(job, REVIEWING):
            return
        try:
            clean = media.strip_png_metadata(png)
        except media.ImageError:
            return self._finish(job, FAILED, {"detail": "The picture could not be prepared. Please try again.",
                                              "code": "generation_failed"}, refund=True)
        try:
            verdict = image_policy.parse_verdict(await self._judge(job.kind, image_policy.image_check_messages(clean)))
        except JudgeUnavailable as exc:
            return self._finish(job, FAILED, {"detail": exc.detail, "code": exc.code}, refund=True)
        self._samples[job.size].append(self._clock() - job.began)
        if job.cancel_requested:
            return
        if not verdict.allowed:
            log_coordinator(f"Image discarded by the result check: {job.tag} {verdict.category}.")
            return self._finish(job, REFUSED, {"detail": image_policy.IMAGE_REFUSED_MESSAGE, "code": "image_refused",
                                               "category": verdict.category})
        job.png = clean
        self._finish(job, COMPLETED)
