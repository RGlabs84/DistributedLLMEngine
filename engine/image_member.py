"""
The image engine (2.6.1): stable-diffusion.cpp's ``sd-server`` as a child of the coordinator, on the media card only.

    start     lazily, on the first job (about 24 s cold); never while the media card has less than 5.6 GiB free
    run       one job at a time through the native /sdcpp/v1 API: submit, poll, cancel. The request body is built
              HERE from the prompt, a size and a seed; nothing a client sent is ever passed on
    unload    after IMAGE_IDLE_UNLOAD_MINUTES with no job (0: never)
    recover   killed and started again after a job timeout or a crash; three failures in a row make image
              generation unavailable for 30 minutes

sd-server listens on 127.0.0.1 only, on a free port. It sees only the media card (GGML_VK_VISIBLE_DEVICES), loads its
own libraries from its own folder (LD_LIBRARY_PATH; llama.cpp's libggml must never be next to them) and runs in an empty
private directory. Its output is kept only while it starts (it prints nothing of anyone's before its first job), so
that a start that fails can say why; once it is ready the output is read and thrown away, because from the first job on it
prints the prompt, and a prompt is never kept or logged. After readiness a crash is logged as its exit code or signal only.
Images are never written to disk; the PNG comes back in the job's JSON and is handed to the caller.

Nothing here touches the coding engine, so an engine restart is never waiting on this module.
"""

import asyncio
import base64
import binascii
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import unicodedata
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

import httpx

from engine import media
from engine.config import get_base_dir, get_worker_limits
from engine.logger import log_coordinator, log_error, log_warning

SD_DIR_NAME = "bin-sd"                    # <install>/bin-sd/, where scripts/release_apply.py unpacks server-sd.tar.gz
SD_BINARY = "sd-server"

# The three model files (scripts/image_models.json pins them with their sha256; scripts/fetch_image_models.py
# downloads and verifies them). Here only the names and exact sizes: a file of any other size is not the pinned one.
# The text encoder is the CPU pool's qwen3-4b (scripts/app_models.json).
MODEL_FILES: Dict[str, Tuple[str, int]] = {
    "diffusion": ("z_image_turbo-Q4_K.gguf", 3_864_250_304),
    "vae": ("ae.safetensors", 335_304_388),
    "llm": ("Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf", 2_497_280_736),
}
DEFAULT_MODEL_SUBDIR = os.path.join("models", "image")

MIN_FREE_VRAM_GB = 5.6                    # what loading the three models takes on the card (measured: 4.85 GiB idle, 6.45 peak)
FAILURES_BEFORE_COOLDOWN = 3
COOLDOWN_SECONDS = 30 * 60
READY_TIMEOUT_SECONDS = 180.0             # cold start is about 24 s; this is for a slow disk
CANCEL_WAIT_SECONDS = 30.0                # sd stops at its next step; past this the process is replaced
POLL_FAILURES_ALLOWED = 3                 # job polls that fail in a row (process alive) before the job is given up
STEPS, TXT_CFG = 8, 1.0                   # Z-Image-Turbo's settings (measured on TheWall)
# Why nothing is drawn while the coding model has layers on the media card (for the log, like the other reasons).
MEDIA_CARD_SHARED_REASON = "the media card is shared with the coding model (MEDIA_GPU_SHARED)"

# What a job's failure says to a person. The code is stable for clients; the sentence is theirs to show.
UNAVAILABLE = "image_generation_unavailable"
FAILED = "generation_failed"
TIMEOUT = "generation_timeout"


STARTUP_RING_LINES = 200          # what is kept of sd-server's output while it starts
STARTUP_LOG_LINES = 40            # how many of those a failed start logs
OUTPUT_LINE_CHARS = 300           # a longer line is cut; a read never takes more than 4 times this at once


def exit_text(code: Optional[int]) -> str:
    """How a child ended, for the log: its exit code, or the signal that killed it (a negative code). Nothing it printed."""
    if code is not None and code < 0:
        try:
            return f"signal {-code} ({signal.Signals(-code).name})"
        except ValueError:
            return f"signal {-code}"
    return f"exit code {code}"


class StartupOutput:
    """
    sd-server's stdout and stderr, read on a thread of their own for as long as the process lives.

    While it starts the last ``STARTUP_RING_LINES`` lines are kept, cleaned of control characters and cut to a sane length,
    so a start that fails can log what sd-server said. :meth:`stop_keeping` (called once it is ready) empties the ring and
    keeps nothing from then on, for good; the thread goes on reading only so that the pipe never fills and stops sd-server.
    The thread ends when the pipe does, that is when the process (and anything it started) has ended.
    """

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._lock = threading.Lock()
        self._lines: Deque[str] = deque(maxlen=STARTUP_RING_LINES)
        self._keeping = True
        self.thread = threading.Thread(target=self._read, name="sd-output", daemon=True)
        self.thread.start()

    @staticmethod
    def _clean(raw: bytes) -> str:
        text = raw.decode("utf-8", "replace").rstrip("\r\n")
        text = "".join(ch if ch == "\t" or not unicodedata.category(ch).startswith("C") else " " for ch in text)
        return text[:OUTPUT_LINE_CHARS]

    def _read(self) -> None:
        try:
            for raw in iter(lambda: self._stream.readline(OUTPUT_LINE_CHARS * 4), b""):
                with self._lock:
                    if self._keeping:
                        self._lines.append(self._clean(raw))
        except (OSError, ValueError):          # the pipe was closed under it
            pass
        finally:
            try:
                self._stream.close()
            except Exception:                  # noqa: BLE001 - nothing left to do for it
                pass

    def stop_keeping(self) -> None:
        with self._lock:
            self._keeping = False
            self._lines.clear()

    def tail(self, count: int = STARTUP_LOG_LINES, wait_for_end: float = 0.0) -> List[str]:
        """The last ``count`` kept lines. ``wait_for_end``: seconds to let the reader reach the end of a pipe that has closed."""
        if wait_for_end:
            self.thread.join(wait_for_end)
        with self._lock:
            return list(self._lines)[-count:]

    def join(self, timeout: float = 2.0) -> None:
        self.thread.join(timeout)


class MemberError(Exception):
    """A job could not be drawn. ``code`` is for clients, ``detail`` a sentence for a person."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


class Cancelled(Exception):
    """The job was cancelled while it was running."""


def model_dir(limits: Dict[str, Any], base_dir: str) -> str:
    return limits.get("image_model_dir") or os.path.join(base_dir, DEFAULT_MODEL_SUBDIR)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _default_media_spec(limits: Dict[str, Any]) -> str:
    from engine.inference_manager import media_gpu_spec        # imported late: it pulls in the whole engine manager
    return media_gpu_spec(limits)


def _default_media_card(spec: str) -> Optional[Dict[str, Any]]:
    from engine.hardware import media_card
    return media_card(spec)


class ImageMember:
    """
    sd-server and everything about running it. All the knobs are read from the limits dict at the moment they
    are used, so a change to config.txt applies to the next start. The collaborators are parameters so tests can
    stand in for the process, the card and the clock.
    """

    def __init__(self, limits: Callable[[], Dict[str, Any]] = get_worker_limits,
                 base_dir: Callable[[], str] = get_base_dir,
                 spawn: Callable[..., Any] = subprocess.Popen,
                 media_spec: Callable[[Dict[str, Any]], str] = _default_media_spec,
                 media_card: Callable[[str], Optional[Dict[str, Any]]] = _default_media_card,
                 clock: Callable[[], float] = time.monotonic,
                 poll_seconds: float = 1.0,
                 ready_timeout: float = READY_TIMEOUT_SECONDS,
                 cancel_wait: float = CANCEL_WAIT_SECONDS,
                 free_port: Callable[[], int] = _free_port) -> None:
        self._limits, self._base_dir, self._spawn = limits, base_dir, spawn
        self._media_spec, self._media_card = media_spec, media_card
        self._clock, self._poll, self._ready_timeout, self._cancel_wait = clock, poll_seconds, ready_timeout, cancel_wait
        self._free_port = free_port
        self.process: Optional[Any] = None
        self._output: Optional[StartupOutput] = None
        self.port: Optional[int] = None
        self._workdir: Optional[str] = None
        self._lock: Optional[asyncio.Lock] = None
        self._lock_loop: Optional[asyncio.AbstractEventLoop] = None
        self._last_used = clock()
        self.failures = 0
        self.unavailable_until: Optional[float] = None

    # ------------------------------------------------------------------ what is installed
    def sd_dir(self) -> str:
        return os.path.join(self._base_dir(), SD_DIR_NAME)

    def binary(self) -> str:
        return os.path.join(self.sd_dir(), SD_BINARY)

    def model_paths(self, limits: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
        folder = model_dir(limits if limits is not None else self._limits(), self._base_dir())
        return {role: os.path.join(folder, name) for role, (name, _size) in MODEL_FILES.items()}

    def unavailable_reason(self) -> Optional[str]:
        """
        Why images cannot be made on this machine right now (for the operator: a log line, never a client), or None.
        Checks the settings and the files only: the media card's free memory is looked at when sd-server starts, and
        vision (which the result check needs) is the coordinator's to check.
        """
        limits = self._limits()
        if not limits.get("image_gen_enabled", False):
            return "IMAGE_GEN_ENABLED is not true"
        now = self._clock()
        if self.unavailable_until is not None:
            if now < self.unavailable_until:
                return f"paused for {int(self.unavailable_until - now) // 60 + 1} more minute(s) after {FAILURES_BEFORE_COOLDOWN} failures in a row"
            self.unavailable_until, self.failures = None, 0
        if not self._media_spec(limits):
            return "MEDIA_GPU is not set (or the engine is not a Vulkan build)"
        if limits.get("media_gpu_shared", False):
            # The coding model has layers on the card; sd-server needs it to itself (5.6 GiB free, and its time).
            return MEDIA_CARD_SHARED_REASON
        binary = self.binary()
        if not (os.path.isfile(binary) and os.access(binary, os.X_OK)):
            return f"the sd build is not installed ({binary})"
        for role, path in self.model_paths(limits).items():
            try:
                size = os.path.getsize(path)
            except OSError:
                return f"the {role} model file is missing ({path})"
            if size != MODEL_FILES[role][1]:
                return f"the {role} model file has the wrong size ({path})"
        return None

    @property
    def loaded(self) -> bool:
        return self.process is not None and self.process.poll() is None

    # ------------------------------------------------------------------ one job
    def _job_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock, self._lock_loop = asyncio.Lock(), loop
        return self._lock

    async def generate(self, prompt: str, width: int, height: int, seed: int,
                       cancelled: Callable[[], bool] = lambda: False,
                       on_generating: Optional[Callable[[], None]] = None) -> bytes:
        """
        Draw one picture and return the PNG exactly as sd wrote it (metadata still in it: the caller strips it).
        ``on_generating`` is called once the model is loaded and the job is submitted. Raises :class:`MemberError`
        or :class:`Cancelled`.
        """
        async with self._job_lock():
            limits = self._limits()
            try:
                await self._ensure_started(limits)
                if on_generating:
                    on_generating()
                png = await self._run_job(prompt, width, height, seed, cancelled, float(limits.get("image_job_timeout_seconds", 300)))
            except Cancelled:
                raise
            except MemberError as exc:
                if exc.code != UNAVAILABLE:
                    self._strike()
                raise
            else:
                self.failures = 0
                return png
            finally:
                self._last_used = self._clock()

    def _strike(self) -> None:
        self.failures += 1
        if self.failures >= FAILURES_BEFORE_COOLDOWN:
            self.unavailable_until = self._clock() + COOLDOWN_SECONDS
            log_error(f"Image generation failed {self.failures} times in a row; it is off for {COOLDOWN_SECONDS // 60} minutes.")

    def _client(self) -> httpx.AsyncClient:
        # trust_env off: a proxy in the environment must never be given a request for the loopback sd-server.
        return httpx.AsyncClient(base_url=f"http://127.0.0.1:{self.port}", timeout=10.0, trust_env=False)

    async def _ensure_started(self, limits: Dict[str, Any]) -> None:
        if self.loaded:
            return
        if self.process is not None:                 # it died on its own: clear it away before starting again
            log_warning(f"The image engine had stopped ({exit_text(self.process.poll())}); starting it again.")
            await self.stop()
        reason = self.unavailable_reason()
        if reason:
            log_warning(f"The image engine was not started: {reason}.")
            raise MemberError(UNAVAILABLE, "Image generation isn't available right now.")
        spec = self._media_spec(limits)
        card = await asyncio.to_thread(self._media_card, spec)
        if card is None:
            raise MemberError(UNAVAILABLE, "Image generation isn't available right now.")
        free = card.get("free_gb")
        if free is None or free < MIN_FREE_VRAM_GB:
            log_warning(f"The media card has {'unknown' if free is None else f'{free:.1f} GiB'} free; the image engine needs {MIN_FREE_VRAM_GB} GiB.")
            raise MemberError(UNAVAILABLE, "The graphics card doesn't have enough free memory for images right now. Try again in a few minutes.")
        paths = self.model_paths(limits)
        self.port = self._free_port()
        command = [self.binary(), "--listen-ip", "127.0.0.1", "--listen-port", str(self.port),
                   "--diffusion-model", paths["diffusion"], "--vae", paths["vae"], "--llm", paths["llm"],
                   "--backend", "te=cpu", "-t", str(int(limits.get("image_threads", 4))),
                   "--cfg-scale", str(TXT_CFG), "--steps", str(STEPS), "--eager-load"]
        mask = limits.get("image_cpu_mask") or ""
        if mask:
            if shutil.which("taskset"):
                command = ["taskset", mask] + command
            else:
                log_warning("IMAGE_CPU_MASK is set but taskset is not installed; the image engine is not pinned.")
        # Only the media card, and only its own libraries (never llama.cpp's libggml, which has the same names).
        env = dict(os.environ, GGML_VK_VISIBLE_DEVICES=str(card["vk_index"]), LD_LIBRARY_PATH=self.sd_dir())
        self._workdir = tempfile.mkdtemp(prefix="dlne-sd-")
        began = self._clock()
        try:
            self.process = self._spawn(command, env=env, cwd=self._workdir, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as exc:
            await self.stop()
            log_error(f"The image engine could not be started ({type(exc).__name__}).")
            raise MemberError(FAILED, "The image engine could not be started.") from exc
        stream = getattr(self.process, "stdout", None)
        self._output = StartupOutput(stream) if stream is not None else None
        log_coordinator(f"Image engine starting on 127.0.0.1:{self.port} (media card Vulkan device {card['vk_index']}).")
        if not await self._wait_ready():
            await self._log_failed_start()
            await self.stop()
            raise MemberError(FAILED, "The image engine did not start. Try again in a moment.")
        if self._output is not None:
            self._output.stop_keeping()          # from here on nothing it prints is kept: the next thing it prints may be a prompt
        log_coordinator(f"Image engine ready in {self._clock() - began:.0f} s.")

    async def _log_failed_start(self) -> None:
        """Say why a start failed: how it ended, and the last of what it printed before it was ever ready."""
        process = self.process
        code = process.poll() if process is not None else None
        how = f"it ended with {exit_text(code)}" if code is not None else "it never answered"
        log_error(f"The image engine did not become ready ({how}).")
        if self._output is not None:
            lines = await asyncio.to_thread(self._output.tail, STARTUP_LOG_LINES, 1.0 if code is not None else 0.0)
            for line in lines:
                log_error(f"sd-server: {line}")

    async def _wait_ready(self) -> bool:
        deadline = self._clock() + self._ready_timeout
        async with self._client() as client:
            while self._clock() < deadline:
                if self.process is None or self.process.poll() is not None:
                    return False
                try:
                    if (await client.get("/sdcpp/v1/capabilities")).status_code == 200:
                        return True
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(self._poll)
        return False

    def _request_body(self, prompt: str, width: int, height: int, seed: int) -> Dict[str, Any]:
        """The whole of what sd-server is sent. Built from these four values and constants, never from a client's body."""
        return {"prompt": prompt, "width": int(width), "height": int(height), "seed": int(seed),
                "sample_params": {"sample_steps": STEPS, "guidance": {"txt_cfg": TXT_CFG}},
                "output_format": "png",
                # Belt and braces: the PNG's metadata is stripped before it leaves (media.strip_png_metadata) either way.
                "embed_image_metadata": False}

    async def _run_job(self, prompt: str, width: int, height: int, seed: int, cancelled: Callable[[], bool],
                       timeout: float) -> bytes:
        async with self._client() as client:
            try:
                submitted = await client.post("/sdcpp/v1/img_gen", json=self._request_body(prompt, width, height, seed))
                job_id = submitted.json().get("id") if submitted.status_code == 202 else None
            except (httpx.HTTPError, ValueError, AttributeError):
                job_id = None
            if not isinstance(job_id, str) or not job_id or "/" in job_id:
                await self._abandon()
                raise MemberError(FAILED, "The image engine would not take the job. Try again in a moment.")
            deadline = self._clock() + timeout
            blips = 0
            while True:
                if self.process is None or self.process.poll() is not None:
                    # Only how it ended: whatever it printed since it was ready may hold the prompt.
                    log_error(f"The image engine stopped while drawing ({exit_text(self.process.poll() if self.process else None)}).")
                    await self._abandon()
                    raise MemberError(FAILED, "The image engine stopped while drawing. Try again in a moment.")
                if cancelled():
                    await self._cancel(client, job_id)
                    raise Cancelled()
                if self._clock() > deadline:
                    await self._cancel(client, job_id, wait=False)
                    await self._abandon()
                    raise MemberError(TIMEOUT, "The picture took too long to draw and was stopped. Try a simpler description or a smaller size.")
                try:
                    reply = await client.get(f"/sdcpp/v1/jobs/{job_id}")
                    job = reply.json() if reply.status_code == 200 else None
                except (httpx.HTTPError, ValueError):
                    job = None
                if not isinstance(job, dict):
                    blips += 1
                    if blips >= POLL_FAILURES_ALLOWED:
                        await self._abandon()
                        raise MemberError(FAILED, "The image engine stopped answering. Try again in a moment.")
                    await asyncio.sleep(self._poll)
                    continue
                blips = 0
                status = job.get("status")
                if status == "completed":
                    return self._png_of(job)
                if status in ("failed", "cancelled"):
                    await self._abandon()
                    raise MemberError(FAILED, "The picture could not be drawn. Try again in a moment.")
                await asyncio.sleep(self._poll)

    @staticmethod
    def _png_of(job: Dict[str, Any]) -> bytes:
        try:
            encoded = job["result"]["images"][0]["b64_json"]
            png = base64.b64decode(encoded, validate=True)
        except (KeyError, IndexError, TypeError, binascii.Error, ValueError):
            png = b""
        if media.png_size(png) is None:
            raise MemberError(FAILED, "The image engine returned something that is not a picture.")
        return png

    async def _cancel(self, client: httpx.AsyncClient, job_id: str, wait: bool = True) -> None:
        """Ask sd to stop the job and, with ``wait``, wait for it to; if it will not, the process is replaced."""
        try:
            await client.post(f"/sdcpp/v1/jobs/{job_id}/cancel")
        except httpx.HTTPError:
            pass
        if not wait:
            return
        deadline = self._clock() + self._cancel_wait
        while self._clock() < deadline and self.loaded:
            try:
                reply = await client.get(f"/sdcpp/v1/jobs/{job_id}")
                if reply.status_code != 200 or reply.json().get("status") in ("completed", "failed", "cancelled"):
                    return
            except (httpx.HTTPError, ValueError, AttributeError):
                pass
            await asyncio.sleep(self._poll)
        await self.stop()           # still running past its checkpoint: the next job starts a fresh one

    async def _abandon(self) -> None:
        """A job went wrong: whatever sd is doing is stopped for good; the next job starts it afresh."""
        await self.stop()

    # ------------------------------------------------------------------ stopping
    async def stop(self) -> None:
        """Stop sd-server (terminate, then kill after 5 s) and remove its working directory. Safe to repeat."""
        process, workdir, self.process, self.port, self._workdir = self.process, self._workdir, None, None, None
        output, self._output = self._output, None
        if process is not None and process.poll() is None:
            await asyncio.to_thread(self._end, process)
        if output is not None:
            await asyncio.to_thread(output.join)         # the pipe closed with the process: the reader is done
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)

    @staticmethod
    def _end(process: Any) -> None:
        try:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        except Exception:      # noqa: BLE001 - already gone, or not ours to wait for
            pass

    def stop_now(self) -> None:
        """For the coordinator's shutdown: the same as :meth:`stop`, without an event loop."""
        process, workdir, self.process, self.port, self._workdir = self.process, self._workdir, None, None, None
        output, self._output = self._output, None
        if process is not None and process.poll() is None:
            self._end(process)
        if output is not None:
            output.join()
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)

    async def maybe_unload(self) -> bool:
        """Stop sd-server when it has had no job for IMAGE_IDLE_UNLOAD_MINUTES (0: never). True when it was stopped."""
        minutes = int(self._limits().get("image_idle_unload_minutes", 20))
        if minutes <= 0 or not self.loaded or self._job_lock().locked():
            return False
        if self._clock() - self._last_used < minutes * 60:
            return False
        await self.stop()
        log_coordinator(f"Image engine unloaded after {minutes} minutes idle.")
        return True
