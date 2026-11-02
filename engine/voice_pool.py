"""
Voice clips (2.8.0): the WAV check, and who is waiting for the voice engine in what order.

A clip is a short WAV the coordinator turns into text with whisper-server (engine/voice_member.py). Clips run one at a
time, on one queue that goes by tier as the picture queue and the coding engine's do (engine/priority.py): a waiting clip
of a host, an API-key holder or a GPU contributor runs before a waiting trial caller's, and clips of one tier run in the
order they came. A running clip is never pre-empted.

The card is shared with the picture engine. If a picture is being made and the media card is short of the memory
whisper-server needs, the clip waits for the picture to be done (up to ``PICTURE_WAIT_SECONDS``); it never stops one.

The audio is held in memory only: in the queued job until the clip has run, then dropped. Nothing is written to disk and
nothing of the audio or the text is logged; a log line says only how long the clip was, its language and how long it took.
Each network may send ``VOICE_PER_HOUR`` clips an hour. All of this runs on the coordinator's one event loop.
"""

import asyncio
import secrets
import struct
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, Optional, Tuple

from engine.config import get_worker_limits
from engine.image_pool import Rejected, _Hourly
from engine.logger import log_coordinator, log_error
from engine.priority import LOWEST_PRIORITY
from engine.voice_member import Cancelled, MemberError, VoiceMember

SAMPLE_RATE = 16000
BYTES_PER_SECOND = SAMPLE_RATE * 2                # mono, 16 bit
MAX_BYTES = 2 * 1024 * 1024                       # the whole request's audio, header included
MIN_SECONDS = 0.1
PICTURE_WAIT_SECONDS = 180.0                      # a picture takes 20 to 30 s; this is for a queue of them
PICTURE_POLL_SECONDS = 0.5
HOUSEKEEPING_SECONDS = 30.0

NOT_WAV = "That isn't a WAV file. Send a WAV, 16-bit PCM, mono, 16 kHz."


class BadAudio(Exception):
    """The upload is not a clip the engine takes; ``detail`` says why in plain words (400 voice_bad_audio)."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def parse_wav(data: bytes, max_seconds: int) -> Tuple[bytes, float]:
    """
    Check ``data`` is a RIFF/WAVE file of 16-bit PCM, mono, 16 kHz, no longer than ``max_seconds`` and 2 MB, by reading
    its header (no decoder, no ffmpeg). Returns ``(the clip, its length in seconds)``; the clip is ``data`` itself.
    Raises :class:`BadAudio`.
    """
    if len(data) > MAX_BYTES:
        raise BadAudio("That clip is bigger than 2 MB. Record a shorter one.")
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise BadAudio(NOT_WAV)
    fmt: Optional[Tuple[int, int, int, int]] = None
    data_bytes: Optional[int] = None
    pos = 12
    while pos + 8 <= len(data):
        kind, size = data[pos:pos + 4], struct.unpack_from("<I", data, pos + 4)[0]
        body = pos + 8
        if kind == b"fmt ":
            if size < 16 or body + 16 > len(data):
                raise BadAudio(NOT_WAV)
            tag, channels, rate, _byte_rate, _align, bits = struct.unpack_from("<HHIIHH", data, body)
            fmt = (tag, channels, rate, bits)
        elif kind == b"data":
            data_bytes = min(size, len(data) - body)          # a header that claims more than came is cut to what came
            break
        pos = body + size + (size & 1)                        # chunks are padded to an even size
    if fmt is None or data_bytes is None:
        raise BadAudio(NOT_WAV)
    tag, channels, rate, bits = fmt
    if tag != 1 or bits != 16:
        raise BadAudio("The WAV must be 16-bit PCM.")
    if channels != 1:
        raise BadAudio("The WAV must be mono (one channel), not stereo.")
    if rate != SAMPLE_RATE:
        raise BadAudio(f"The WAV must be 16 kHz (this one is {rate / 1000:g} kHz).")
    seconds = (data_bytes // 2) * 2 / BYTES_PER_SECOND
    if seconds < MIN_SECONDS:
        raise BadAudio("That clip is empty or too short.")
    if seconds > max_seconds:
        raise BadAudio(f"That clip is longer than {max_seconds} seconds. Record a shorter one.")
    return data, seconds


@dataclass
class Clip:
    id: str
    owner: str
    wav: Optional[bytes]                 # dropped as soon as the clip has run, or it ends
    seconds: float
    language: Optional[str]
    priority: int
    future: "asyncio.Future[Tuple[str, Optional[str]]]"
    created: float
    stamp: Optional[float] = None        # its place in the owner's hourly count
    cancelled: bool = False
    began: Optional[float] = None
    waiting_for_card: bool = field(default=False)


class VoiceService:
    """The queue, the worker and the hourly counts. One per coordinator."""

    def __init__(self, member: VoiceMember, limits: Callable[[], Dict[str, Any]] = get_worker_limits,
                 clock: Callable[[], float] = time.monotonic, picture_busy: Callable[[], bool] = lambda: False,
                 picture_wait: float = PICTURE_WAIT_SECONDS, picture_poll: float = PICTURE_POLL_SECONDS,
                 housekeeping: float = HOUSEKEEPING_SECONDS) -> None:
        self.member, self._limits, self._clock = member, limits, clock
        self._picture_busy, self._picture_wait, self._picture_poll = picture_busy, picture_wait, picture_poll
        self._housekeeping = housekeeping
        self._pending: Deque[Clip] = deque()
        self.active: Optional[Clip] = None
        self._hourly = _Hourly()
        self._wake: Optional[asyncio.Event] = None
        self._worker: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ------------------------------------------------------------------ status
    @property
    def idle(self) -> bool:
        return self.active is None and not self._pending

    def status_block(self) -> Dict[str, Any]:
        """The ``voice`` block of /v1/cluster/status (counts only; nothing names a network)."""
        return {"queue": len(self._pending), "busy": self.active is not None,
                "max_seconds": int(self._limits().get("voice_max_seconds", 60)), "languages": "auto"}

    # ------------------------------------------------------------------ the front door
    def submit(self, owner: str, wav: bytes, seconds: float, language: Optional[str], priority: int = LOWEST_PRIORITY) -> Clip:
        """
        Queue a checked clip. Raises :class:`Rejected` (``voice_busy`` when VOICE_QUEUE_MAX clips are waiting, ``voice_quota``
        when the network has used its hour). Await ``clip.future`` for ``(text, language)``; a MemberError comes out of it.
        """
        limits = self._limits()
        now = self._clock()
        self._hourly.forget_idle(now)
        if len(self._pending) >= int(limits.get("voice_queue_max", 8)):
            raise Rejected("voice_busy", "Lots of people are using voice right now. Please try again in a moment.", 10)
        per_hour = int(limits.get("voice_per_hour", 60))
        allowed, value = self._hourly.spend(owner, per_hour, now)
        if not allowed:
            raise Rejected("voice_quota", "Voice is used up for this hour. Please try again a little later.",
                           max(1, int(value) + 1))
        self._ensure_worker()
        clip = Clip(id=secrets.token_urlsafe(8), owner=owner, wav=wav, seconds=seconds, language=language,
                    priority=int(priority), future=self._loop.create_future(), created=now, stamp=value)
        index = len(self._pending)
        while index > 0 and self._pending[index - 1].priority > clip.priority:
            index -= 1
        self._pending.insert(index, clip)
        self._wake.set()
        return clip

    def cancel(self, clip: Clip) -> None:
        """The caller went away. A queued clip is dropped and its hour given back; a running one finishes and is thrown away."""
        if clip.cancelled:
            return
        clip.cancelled = True
        if clip in self._pending:
            self._pending.remove(clip)
            if clip.stamp is not None:
                self._hourly.refund(clip.owner, clip.stamp)
            clip.wav = None
        if not clip.future.done():
            clip.future.cancel()

    # ------------------------------------------------------------------ the worker
    def _ensure_worker(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:                 # a new event loop (a restarted server, a test): nothing of the old one is live
            self._loop, self._wake, self._worker = loop, asyncio.Event(), None
        if self._worker is None or self._worker.done():
            self._worker = loop.create_task(self._run(), name="voice-worker")

    async def shutdown(self) -> None:
        """Stop the worker and whisper-server (the coordinator's shutdown)."""
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()
        self.member.stop_now()

    async def _next(self) -> Clip:
        while not self._pending:
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), self._housekeeping)
            except asyncio.TimeoutError:
                try:
                    await self.member.maybe_unload()
                except Exception as exc:      # noqa: BLE001 - housekeeping must not end the worker
                    log_error(f"Voice engine idle check failed ({type(exc).__name__}).")
        return self._pending.popleft()

    async def _run(self) -> None:
        while True:
            clip = await self._next()
            if clip.cancelled:
                continue
            self.active = clip
            try:
                await self._process(clip)
            except asyncio.CancelledError:
                self._fail(clip, MemberError("engine_restarting", "DLNE is restarting, try again in a moment."), give_back=True)
                raise
            except Exception as exc:          # noqa: BLE001 - one clip's bug must not take the worker with it
                log_error(f"Voice clip crashed: {type(exc).__name__}.")
                self._fail(clip, MemberError("voice_failed", "Something went wrong turning that into text. Please try again."), give_back=True)
            finally:
                clip.wav = None
                self.active = None

    def _fail(self, clip: Clip, error: MemberError, give_back: bool) -> None:
        if give_back and clip.stamp is not None:
            self._hourly.refund(clip.owner, clip.stamp)
        if not clip.future.done():
            clip.future.set_exception(error)

    async def _wait_for_card(self, clip: Clip) -> None:
        """If the card is short of memory and a picture is being made, wait for the picture; never stop it."""
        if self.member.unavailable_reason() is not None:
            return                                    # the member will say so
        clip.waiting_for_card = True
        try:
            deadline = self._clock() + self._picture_wait
            while not clip.cancelled and self._clock() < deadline:
                enough, _free = await self.member.card_has_room()
                if enough or not self._picture_busy():
                    return
                await asyncio.sleep(self._picture_poll)
        finally:
            clip.waiting_for_card = False

    async def _process(self, clip: Clip) -> None:
        clip.began = self._clock()
        await self._wait_for_card(clip)
        if clip.cancelled:
            return
        wav = clip.wav or b""
        try:
            text, language = await self.member.transcribe(wav, clip.language, cancelled=lambda: clip.cancelled)
        except Cancelled:
            return
        except MemberError as exc:
            return self._fail(clip, exc, give_back=True)
        finally:
            clip.wav = None
        log_coordinator(f"Voice clip of {clip.seconds:.1f} s, language {language or 'unknown'}, took {self._clock() - clip.began:.1f} s.")
        if not clip.future.done():
            clip.future.set_result((text, language))


# ----------------------------------------------------------------------------------------------------------------------
# multipart/form-data, read by hand: the server has no multipart library (and needs none for one file and two words).
# ----------------------------------------------------------------------------------------------------------------------

MAX_FORM_BYTES = MAX_BYTES + 64 * 1024            # the audio and room for the other fields and the framing
MAX_FIELDS = 16


def _boundary(content_type: str) -> Optional[bytes]:
    if not content_type.lower().startswith("multipart/form-data"):
        return None
    for piece in content_type.split(";")[1:]:
        key, _, value = piece.strip().partition("=")
        if key.strip().lower() == "boundary":
            value = value.strip().strip('"')
            return value.encode("latin-1", "replace") if value else None
    return None


def parse_form(content_type: str, body: bytes) -> Dict[str, bytes]:
    """
    The fields of a ``multipart/form-data`` body as ``{name: raw bytes}`` (the file is the field ``file``). Raises
    :class:`BadAudio` for anything that is not one. At most ``MAX_FIELDS`` parts are read.
    """
    boundary = _boundary(content_type)
    if boundary is None:
        raise BadAudio("Send the clip as multipart/form-data with the WAV in a field named file.")
    delimiter = b"--" + boundary
    fields: Dict[str, bytes] = {}
    chunks = body.split(delimiter)
    for chunk in chunks[1:1 + MAX_FIELDS + 1]:
        if chunk.startswith(b"--"):
            break                                        # the closing delimiter
        head, sep, value = chunk.partition(b"\r\n\r\n")
        if not sep:
            continue
        name = None
        for line in head.decode("latin-1").split("\r\n"):
            if line.lower().startswith("content-disposition"):
                for piece in line.split(";")[1:]:
                    key, _, val = piece.strip().partition("=")
                    if key.strip().lower() == "name":
                        name = val.strip().strip('"')
        if name is None:
            continue
        fields.setdefault(name, value[:-2] if value.endswith(b"\r\n") else value)
    return fields
