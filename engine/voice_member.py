"""
The voice engine (2.8.0): whisper.cpp's ``whisper-server`` as a child of the coordinator, on the media card only.
Speech to text; docs/voice.md and docs/voice-contract.md.

    start     lazily, on the first clip; never while the media card has less than VOICE_MIN_FREE_VRAM_GB free
    run       one clip at a time: ``POST /inference`` (multipart: file, response_format, language), the answer is JSON
    unload    after VOICE_IDLE_UNLOAD_MINUTES with no clip (0: never)
    recover   killed and started again after a timeout or a crash; three failures in a row make voice unavailable
              for 30 minutes

It is modelled on engine/image_member.py and shares its helpers. whisper-server listens on 127.0.0.1 only, on a free
port. It sees only the media card (GGML_VK_VISIBLE_DEVICES), loads its own libraries from its own folder
(LD_LIBRARY_PATH) and runs in an empty private directory. It is started without ``--convert`` (so it never needs ffmpeg and
never writes an upload to disk) and with ``-np`` (so it prints no transcript); its output is kept only while it starts, as
the picture engine's is. The audio is a bytes object in this process and in the request to the loopback port: never a file.
The text is returned to the caller and kept nowhere.

Nothing here touches the coding engine, so an engine restart is never waiting on this module.
"""

import asyncio
import os
import shutil
import subprocess
import tempfile
import time
from typing import Any, Callable, Dict, Optional, Tuple

import httpx

from engine.config import get_base_dir, get_worker_limits
from engine.image_member import (Cancelled, MemberError, StartupOutput, STARTUP_LOG_LINES, _default_media_card,
                                 _default_media_spec, _free_port, exit_text)
from engine.logger import log_coordinator, log_error, log_warning

__all__ = ["VoiceMember", "MemberError", "Cancelled", "UNAVAILABLE", "FAILED", "TIMEOUT", "language_code"]

WHISPER_DIR_NAME = "bin-whisper"          # <install>/bin-whisper/, where scripts/release_apply.py unpacks server-whisper.tar.gz
WHISPER_BINARY = "whisper-server"
DEFAULT_MODEL_SUBDIR = os.path.join("models", "voice")

FAILURES_BEFORE_COOLDOWN = 3
COOLDOWN_SECONDS = 30 * 60
READY_TIMEOUT_SECONDS = 120.0             # the model is loaded before the port opens; this is for a slow disk
CLIP_TIMEOUT_SECONDS = 120.0              # a 60 s clip takes a few seconds on the RX 580; this is for a stuck process
MEDIA_CARD_SHARED_REASON = "the media card is shared with the coding model (MEDIA_GPU_SHARED)"

UNAVAILABLE = "voice_unavailable"
FAILED = "voice_failed"
TIMEOUT = "voice_timeout"

# whisper's own names for the languages it knows, as ``verbose_json`` reports the detected one, to their ISO 639-1 codes
# (Cantonese, "yue", has no two-letter code). A name not in the table is passed on lower-cased.
_LANGUAGE_CODES = {
    "english": "en", "chinese": "zh", "german": "de", "spanish": "es", "russian": "ru", "korean": "ko", "french": "fr",
    "japanese": "ja", "portuguese": "pt", "turkish": "tr", "polish": "pl", "catalan": "ca", "dutch": "nl", "arabic": "ar",
    "swedish": "sv", "italian": "it", "indonesian": "id", "hindi": "hi", "finnish": "fi", "vietnamese": "vi",
    "hebrew": "he", "ukrainian": "uk", "greek": "el", "malay": "ms", "czech": "cs", "romanian": "ro", "danish": "da",
    "hungarian": "hu", "tamil": "ta", "norwegian": "no", "thai": "th", "urdu": "ur", "croatian": "hr", "bulgarian": "bg",
    "lithuanian": "lt", "latin": "la", "maori": "mi", "malayalam": "ml", "welsh": "cy", "slovak": "sk", "telugu": "te",
    "persian": "fa", "latvian": "lv", "bengali": "bn", "serbian": "sr", "azerbaijani": "az", "slovenian": "sl",
    "kannada": "kn", "estonian": "et", "macedonian": "mk", "breton": "br", "basque": "eu", "icelandic": "is",
    "armenian": "hy", "nepali": "ne", "mongolian": "mn", "bosnian": "bs", "kazakh": "kk", "albanian": "sq",
    "swahili": "sw", "galician": "gl", "marathi": "mr", "punjabi": "pa", "sinhala": "si", "khmer": "km", "shona": "sn",
    "yoruba": "yo", "somali": "so", "afrikaans": "af", "occitan": "oc", "georgian": "ka", "belarusian": "be",
    "tajik": "tg", "sindhi": "sd", "gujarati": "gu", "amharic": "am", "yiddish": "yi", "lao": "lo", "uzbek": "uz",
    "faroese": "fo", "haitian creole": "ht", "pashto": "ps", "turkmen": "tk", "nynorsk": "nn", "maltese": "mt",
    "sanskrit": "sa", "luxembourgish": "lb", "myanmar": "my", "tibetan": "bo", "tagalog": "tl", "malagasy": "mg",
    "assamese": "as", "tatar": "tt", "hawaiian": "haw", "lingala": "ln", "hausa": "ha", "bashkir": "ba", "javanese": "jw",
    "sundanese": "su", "cantonese": "yue",
}


def language_code(name: Any) -> Optional[str]:
    """whisper-server's detected-language name ("english") as a code ("en"); a code is passed through; None when empty."""
    if not isinstance(name, str) or not name.strip():
        return None
    word = name.strip().lower()
    return _LANGUAGE_CODES.get(word, word)


def model_dir(base_dir: str) -> str:
    return os.path.join(base_dir, DEFAULT_MODEL_SUBDIR)


class VoiceMember:
    """
    whisper-server and everything about running it. All the knobs are read from the limits dict at the moment they
    are used, so a change to config.txt applies to the next start. The collaborators are parameters so tests can
    stand in for the process, the card and the clock.
    """

    def __init__(self, limits: Callable[[], Dict[str, Any]] = get_worker_limits,
                 base_dir: Callable[[], str] = get_base_dir,
                 spawn: Callable[..., Any] = subprocess.Popen,
                 media_spec: Callable[[Dict[str, Any]], str] = _default_media_spec,
                 media_card: Callable[[str], Optional[Dict[str, Any]]] = _default_media_card,
                 clock: Callable[[], float] = time.monotonic,
                 poll_seconds: float = 0.5,
                 ready_timeout: float = READY_TIMEOUT_SECONDS,
                 clip_timeout: float = CLIP_TIMEOUT_SECONDS,
                 free_port: Callable[[], int] = _free_port) -> None:
        self._limits, self._base_dir, self._spawn = limits, base_dir, spawn
        self._media_spec, self._media_card = media_spec, media_card
        self._clock, self._poll, self._ready_timeout, self._clip_timeout = clock, poll_seconds, ready_timeout, clip_timeout
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
    def whisper_dir(self) -> str:
        return os.path.join(self._base_dir(), WHISPER_DIR_NAME)

    def binary(self) -> str:
        return os.path.join(self.whisper_dir(), WHISPER_BINARY)

    def model_path(self, limits: Optional[Dict[str, Any]] = None) -> str:
        name = str((limits if limits is not None else self._limits()).get("voice_model") or "")
        return os.path.join(model_dir(self._base_dir()), os.path.basename(name))

    def unavailable_reason(self) -> Optional[str]:
        """
        Why voice cannot run on this machine right now (for the operator: a log line, never a client), or None.
        Checks the settings and the files only; the card's free memory is looked at when whisper-server starts.
        """
        limits = self._limits()
        if not limits.get("voice_enabled", False):
            return "VOICE_ENABLED is not true"
        now = self._clock()
        if self.unavailable_until is not None:
            if now < self.unavailable_until:
                return f"paused for {int(self.unavailable_until - now) // 60 + 1} more minute(s) after {FAILURES_BEFORE_COOLDOWN} failures in a row"
            self.unavailable_until, self.failures = None, 0
        if not self._media_spec(limits):
            return "MEDIA_GPU is not set (or the engine is not a Vulkan build)"
        if limits.get("media_gpu_shared", False):
            return MEDIA_CARD_SHARED_REASON
        binary = self.binary()
        if not (os.path.isfile(binary) and os.access(binary, os.X_OK)):
            return f"the whisper build is not installed ({binary})"
        model = self.model_path(limits)
        if not os.path.isfile(model):
            return f"the voice model is missing ({model})"
        return None

    @property
    def loaded(self) -> bool:
        return self.process is not None and self.process.poll() is None

    async def card_has_room(self) -> Tuple[bool, Optional[float]]:
        """``(enough, free GiB)`` on the media card for starting whisper-server; (True, None) when it is already running."""
        if self.loaded:
            return True, None
        limits = self._limits()
        card = await asyncio.to_thread(self._media_card, self._media_spec(limits))
        if card is None:
            return False, None
        free = card.get("free_gb")
        return (free is not None and free >= float(limits.get("voice_min_free_vram_gb", 1.2))), free

    # ------------------------------------------------------------------ one clip
    def _clip_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock, self._lock_loop = asyncio.Lock(), loop
        return self._lock

    async def transcribe(self, wav: bytes, language: Optional[str] = None,
                         cancelled: Callable[[], bool] = lambda: False) -> Tuple[str, Optional[str]]:
        """
        Turn one validated WAV clip into ``(text, language code)``. ``language`` is an ISO code, or None to detect.
        Raises :class:`MemberError` (or :class:`Cancelled` if ``cancelled()`` turns true before it starts).
        """
        async with self._clip_lock():
            limits = self._limits()
            try:
                if cancelled():
                    raise Cancelled()
                await self._ensure_started(limits)
                text, detected = await self._run_clip(wav, language)
            except Cancelled:
                raise
            except MemberError as exc:
                if exc.code != UNAVAILABLE:
                    self._strike()
                raise
            else:
                self.failures = 0
                return text, detected or language
            finally:
                self._last_used = self._clock()

    def _strike(self) -> None:
        self.failures += 1
        if self.failures >= FAILURES_BEFORE_COOLDOWN:
            self.unavailable_until = self._clock() + COOLDOWN_SECONDS
            log_error(f"Voice failed {self.failures} times in a row; it is off for {COOLDOWN_SECONDS // 60} minutes.")

    def _client(self, timeout: float = 10.0) -> httpx.AsyncClient:
        # trust_env off: a proxy in the environment must never be given a request for the loopback whisper-server.
        return httpx.AsyncClient(base_url=f"http://127.0.0.1:{self.port}", timeout=timeout, trust_env=False)

    def command(self, card: Dict[str, Any], limits: Dict[str, Any], port: int) -> list:
        """The whole command line. whisper.cpp's examples/server flags: ``-m`` model, ``-t`` threads, ``--host``, ``--port``,
        ``-l auto`` (detect unless a request names the language), ``-nlp`` (no language-probability pass: ``language`` still
        names the detected one). No ``--convert``: the clip is read from memory, never written to a temp file, and nothing
        transcribed is printed (print_realtime stays off). v1.9.5 has no ``-np``: an unknown flag prints usage and exits."""
        command = [self.binary(), "--host", "127.0.0.1", "--port", str(port), "-m", self.model_path(limits),
                   "-t", str(int(limits.get("voice_threads", 2))), "-l", "auto", "-nlp"]
        mask = limits.get("voice_cpu_mask") or ""
        if mask:
            if shutil.which("taskset"):
                command = ["taskset", mask] + command
            else:
                log_warning("VOICE_CPU_MASK is set but taskset is not installed; the voice engine is not pinned.")
        return command

    async def _ensure_started(self, limits: Dict[str, Any]) -> None:
        if self.loaded:
            return
        if self.process is not None:                 # it died on its own: clear it away before starting again
            log_warning(f"The voice engine had stopped ({exit_text(self.process.poll())}); starting it again.")
            await self.stop()
        reason = self.unavailable_reason()
        if reason:
            log_warning(f"The voice engine was not started: {reason}.")
            raise MemberError(UNAVAILABLE, "Voice isn't available right now.")
        spec = self._media_spec(limits)
        card = await asyncio.to_thread(self._media_card, spec)
        if card is None:
            raise MemberError(UNAVAILABLE, "Voice isn't available right now.")
        free, need = card.get("free_gb"), float(limits.get("voice_min_free_vram_gb", 1.2))
        if free is None or free < need:
            log_warning(f"The media card has {'unknown' if free is None else f'{free:.1f} GiB'} free; the voice engine needs {need:g} GiB.")
            raise MemberError(UNAVAILABLE, "The graphics card doesn't have enough free memory for voice right now. Try again in a few minutes.")
        self.port = self._free_port()
        command = self.command(card, limits, self.port)
        # Only the media card, and only its own libraries (never llama.cpp's libggml, which has the same names).
        env = dict(os.environ, GGML_VK_VISIBLE_DEVICES=str(card["vk_index"]), LD_LIBRARY_PATH=self.whisper_dir())
        self._workdir = tempfile.mkdtemp(prefix="dlne-whisper-")
        began = self._clock()
        try:
            self.process = self._spawn(command, env=env, cwd=self._workdir, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as exc:
            await self.stop()
            log_error(f"The voice engine could not be started ({type(exc).__name__}).")
            raise MemberError(FAILED, "The voice engine could not be started.") from exc
        stream = getattr(self.process, "stdout", None)
        self._output = StartupOutput(stream) if stream is not None else None
        log_coordinator(f"Voice engine starting on 127.0.0.1:{self.port} (media card Vulkan device {card['vk_index']}).")
        if not await self._wait_ready():
            await self._log_failed_start()
            await self.stop()
            raise MemberError(FAILED, "The voice engine did not start. Try again in a moment.")
        if self._output is not None:
            self._output.stop_keeping()          # from here on nothing it prints is kept
        log_coordinator(f"Voice engine ready in {self._clock() - began:.0f} s.")

    async def _log_failed_start(self) -> None:
        process = self.process
        code = process.poll() if process is not None else None
        how = f"it ended with {exit_text(code)}" if code is not None else "it never answered"
        log_error(f"The voice engine did not become ready ({how}).")
        if self._output is not None:
            lines = await asyncio.to_thread(self._output.tail, STARTUP_LOG_LINES, 1.0 if code is not None else 0.0)
            for line in lines:
                log_error(f"whisper-server: {line}")

    async def _wait_ready(self) -> bool:
        """whisper-server opens its port only once the model is loaded; ``GET /`` then answers its test page with 200."""
        deadline = self._clock() + self._ready_timeout
        async with self._client() as client:
            while self._clock() < deadline:
                if self.process is None or self.process.poll() is not None:
                    return False
                try:
                    if (await client.get("/")).status_code == 200:
                        return True
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(self._poll)
        return False

    async def _run_clip(self, wav: bytes, language: Optional[str]) -> Tuple[str, Optional[str]]:
        """One ``POST /inference``. The form is built here from the WAV and two constants; a client's fields never reach it."""
        data = {"response_format": "verbose_json", "temperature": "0.0", "language": language or "auto"}
        try:
            async with self._client(timeout=self._clip_timeout) as client:
                reply = await client.post("/inference", data=data, files={"file": ("clip.wav", wav, "audio/wav")})
            body = reply.json() if reply.status_code == 200 else None
        except httpx.TimeoutException:
            log_error("The voice engine took too long on a clip and was stopped.")
            await self._abandon()
            raise MemberError(TIMEOUT, "That took too long to turn into text and was stopped. Try a shorter clip.")
        except (httpx.HTTPError, ValueError):
            body = None
        if self.process is None or self.process.poll() is not None:
            log_error(f"The voice engine stopped on a clip ({exit_text(self.process.poll() if self.process else None)}).")
            await self._abandon()
            raise MemberError(FAILED, "The voice engine stopped. Try again in a moment.")
        if not isinstance(body, dict) or not isinstance(body.get("text"), str) or body.get("error"):
            await self._abandon()
            raise MemberError(FAILED, "The voice engine could not read that clip. Try again in a moment.")
        return " ".join(body["text"].split()), language_code(body.get("language"))

    async def _abandon(self) -> None:
        """A clip went wrong: whatever whisper-server is doing is stopped for good; the next clip starts it afresh."""
        await self.stop()

    # ------------------------------------------------------------------ stopping
    async def stop(self) -> None:
        """Stop whisper-server (terminate, then kill after 5 s) and remove its working directory. Safe to repeat."""
        process, workdir, self.process, self.port, self._workdir = self.process, self._workdir, None, None, None
        output, self._output = self._output, None
        if process is not None and process.poll() is None:
            await asyncio.to_thread(self._end, process)
        if output is not None:
            await asyncio.to_thread(output.join)
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
        """Stop whisper-server when it has had no clip for VOICE_IDLE_UNLOAD_MINUTES (0: never). True when it was stopped."""
        minutes = int(self._limits().get("voice_idle_unload_minutes", 20))
        if minutes <= 0 or not self.loaded or self._clip_lock().locked():
            return False
        if self._clock() - self._last_used < minutes * 60:
            return False
        await self.stop()
        log_coordinator(f"Voice engine unloaded after {minutes} minutes idle.")
        return True
