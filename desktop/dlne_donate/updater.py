"""
DLNE Donate updates itself (docs/release-2.7-contract.md section 4a).

The app looks for a newer DLNE Donate at start and every 12 hours, in the same place the extension's "Optional add-on" button
reads it (vscode-extension/src/worker/trayApp.ts): the website's download manifest

    https://dlne.rossgilsonlabs.dev/downloads/desktop.json
    {"versionName": "1.1.0", "notes": "...", "published": "2026-10-30",
     "windows": {"url": "https://dlne.rossgilsonlabs.dev/downloads/DLNE-Donate-1.1.0.exe", "sha256": "<64 hex>", "size": 34348456},
     "linux":   {"url": "https://dlne.rossgilsonlabs.dev/downloads/DLNE-Donate-1.1.0-x86_64.AppImage", "sha256": "<64 hex>", "size": 57526776}}

A newer version is announced ("DLNE Donate 1.1.0 is available", Update now / Later) and nothing is downloaded before the owner says
Update now (or has switched "Update automatically" on, which is off by default). The download is hashed as it arrives and kept only
when it has exactly the published size and SHA-256; any other file is deleted and the running version stays.

    AppImage   the new file is written beside the running one, checked, swapped in under the same name (the old one stays beside it
               as ``.previous`` until the next start) and started again once this app has exited
    Windows    the new exe waits in the app-data folder; a small step (the new exe itself, run with ``--finish-update``) waits for this
               app to exit, copies itself over the old exe, and starts it again

Everything that touches the world (the network, the clock, the processes) is handed in, so the tests drive it with fakes.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import __version__

HOST = "dlne.rossgilsonlabs.dev"
MANIFEST_URL = f"https://{HOST}/downloads/desktop.json"
PAGE_URL = f"https://{HOST}/#dlne-tray"          # the DLNE page's tray app tab, for a build that cannot replace itself

CHECK_EVERY_S = 12 * 3600
RETRY_AFTER_FAILURE_S = 3600                     # offline at start: look again in an hour, not in twelve

MAX_MANIFEST_BYTES = 16 * 1024
MIN_BUILD_BYTES = 1_000_000                      # the builds are 30 to 60 MB; under 1 MB is not the app
MAX_BUILD_BYTES = 500 * 1024 * 1024
MAX_URL_CHARS = 300
MAX_RAW_NOTES = 2000
MAX_NOTES_CHARS = 300

WAIT_FOR_EXIT_S = 60.0                           # the update step waits this long for the old app to exit
SWAP_TRIES_S = 30.0                              # ... and this long for a virus scanner to let go of the file it just wrote

OFF_ENV = "DLNE_DONATE_NO_UPDATE_CHECK"          # set by the tests (and the smoke scripts): the app then never looks for an update

AVAILABLE_TEXT = "DLNE Donate {version} is available"
UPDATE_NOW, LATER = "Update now", "Later"

# What went wrong, in a word (the extension's TrayProblem), so it can be said plainly.
NETWORK, HTTP, REDIRECTED, TOO_LARGE, REFUSED, SIZE, HASH, SAVE, CANCELLED = (
    "network", "http", "redirected", "too-large", "refused", "size", "hash", "save", "cancelled")


class UpdateError(Exception):
    def __init__(self, problem: str, message: str):
        super().__init__(message)
        self.problem = problem


# --------------------------------------------------------------------------------------------------
# The manifest (trayApp.ts parseTrayManifest, rule for rule)
# --------------------------------------------------------------------------------------------------

_VERSION_NAME = re.compile(r"^\d{1,4}(?:\.\d{1,4}){1,3}$")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_ISO_DAY = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_FILE_NAME = {
    "windows": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.exe$"),
    "linux": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.AppImage$"),
}
_URL_REFUSED = re.compile(r"[\s\x00-\x1f\x7f\\%@?#]")
PLATFORMS = ("windows", "linux")


@dataclass(frozen=True)
class Build:
    url: str
    file: str
    sha256: str
    size: int


@dataclass(frozen=True)
class Release:
    version: str
    notes: str = ""
    published: Optional[str] = None
    builds: Dict[str, Build] = field(default_factory=dict)


def official_download(raw: Any, platform: str, host: str = HOST, scheme: str = "https") -> Optional[Tuple[str, str]]:
    """``(url, file)`` when ``raw`` is an official download for ``platform``: https, the DLNE host, no port, no user info, query or fragment, ``/downloads/<name>``."""
    if not isinstance(raw, str) or len(raw) > MAX_URL_CHARS or _URL_REFUSED.search(raw):
        return None
    try:
        url = urllib.parse.urlsplit(raw)
        port = url.port
    except ValueError:
        return None
    if url.scheme != scheme or (url.hostname or "") != host or port is not None or url.username or url.password or url.query or url.fragment:
        return None
    match = re.match(r"^/downloads/([^/]+)$", url.path)
    if not match or not _FILE_NAME[platform].match(match.group(1)):
        return None
    return raw, match.group(1)


def _is_day(text: str) -> bool:
    match = _ISO_DAY.match(text)
    if not match:
        return False
    import datetime
    try:
        datetime.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return False
    return True


def _parse_build(raw: Any, platform: str, host: str, scheme: str) -> Any:
    if not isinstance(raw, dict):
        return f"{platform} is not an object"
    download = official_download(raw.get("url"), platform, host, scheme)
    if not download:
        return f"{platform} url is not an official DLNE download"
    sha = raw.get("sha256")
    if not isinstance(sha, str) or not _SHA256.match(sha):
        return f"{platform} sha256"
    size = raw.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or not MIN_BUILD_BYTES <= size <= MAX_BUILD_BYTES:
        return f"{platform} size"
    return Build(url=download[0], file=download[1], sha256=sha.lower(), size=size)


def parse_manifest(raw: Any, host: str = HOST, scheme: str = "https") -> Tuple[Optional[Release], str]:
    """
    ``(release, "")`` for a manifest that is whole and official, ``(None, why)`` otherwise: anything malformed, and any download that
    is not an official one, refuses all of it. Unknown extra fields are ignored. ``host``/``scheme`` are the tests' to change.
    """
    if not isinstance(raw, dict):
        return None, "not a JSON object"
    version = raw.get("versionName")
    if not isinstance(version, str) or not _VERSION_NAME.match(version):
        return None, "versionName"
    notes = ""
    if raw.get("notes") is not None:
        text = raw["notes"]
        if not isinstance(text, str) or len(text) > MAX_RAW_NOTES:
            return None, "notes"
        notes = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", text)
        notes = re.sub(r"\s+", " ", notes).strip()[:MAX_NOTES_CHARS]
    published = None
    if raw.get("published") is not None:
        if not isinstance(raw["published"], str) or not _is_day(raw["published"]):
            return None, "published"
        published = raw["published"]
    builds: Dict[str, Build] = {}
    for platform in PLATFORMS:
        if raw.get(platform) is None:
            continue
        build = _parse_build(raw[platform], platform, host, scheme)
        if isinstance(build, str):
            return None, build
        builds[platform] = build
    if not builds:
        return None, "no build for Windows or Linux"
    return Release(version=version, notes=notes, published=published, builds=builds), ""


def version_tuple(text: str) -> Tuple[int, ...]:
    """``1.10.0`` as (1, 10, 0), padded to four numbers so ``1.1`` and ``1.1.0`` are the same."""
    parts = [int(p) for p in re.findall(r"\d+", text.split("+")[0])][:4]
    return tuple(parts + [0] * (4 - len(parts)))


def is_newer(candidate: str, current: str = __version__) -> bool:
    return version_tuple(candidate) > version_tuple(current)


def platform_key(platform: Optional[str] = None, machine: Optional[str] = None) -> Optional[str]:
    """``windows`` or ``linux`` for the x86-64 computers DLNE Donate is built for; None for anything else (no download exists for it)."""
    import platform as platform_module
    system = platform or sys.platform
    arch = (machine or platform_module.machine()).lower()
    if arch not in ("x86_64", "amd64"):
        return None
    if system == "win32":
        return "windows"
    return "linux" if system.startswith("linux") else None


def running_app(platform: Optional[str] = None, frozen: Optional[bool] = None, env: Optional[Dict[str, str]] = None,
                executable: Optional[str] = None) -> Optional[str]:
    """
    The file that is this running app, when it can replace itself: the AppImage (``$APPIMAGE``), or the built Windows exe. None for
    a run from the sources and for the Linux folder build, which cannot swap itself (the window then points to the download page).
    """
    system = platform or sys.platform
    if system == "win32":
        is_frozen = getattr(sys, "frozen", False) if frozen is None else frozen
        exe = executable or sys.executable
        return exe if is_frozen and exe else None
    appimage = (os.environ if env is None else env).get("APPIMAGE")
    return appimage if appimage and os.path.isfile(appimage) else None


# --------------------------------------------------------------------------------------------------
# The network: https to the DLNE host only, never following a redirect
# --------------------------------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: Any, msg: Any, headers: Any, newurl: Any) -> None:
        raise UpdateError(REDIRECTED, f"the server answered {code} (a redirect)")


def _official_url(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
        return parts.scheme == "https" and parts.hostname == HOST and parts.port is None
    except ValueError:
        return False


class Http:
    """
    The two requests the updater makes: the manifest and the build. Both go to the DLNE host over https, with the certificates
    checked (the same roots the installer uses), and a redirect is refused, not followed, so a file can never come from somewhere else.
    ``url_ok`` is for the tests' local server.
    """

    user_agent = f"DLNE-Donate/{__version__}"

    def __init__(self, url_ok: Callable[[str], bool] = _official_url):
        self._url_ok = url_ok

    def _open(self, url: str, accept: str, timeout: float) -> Any:
        if not self._url_ok(url):
            raise UpdateError(REFUSED, f"only https://{HOST} is used")
        from . import installer
        handlers: List[Any] = [_NoRedirect(), urllib.request.HTTPSHandler(context=installer.tls_context())]
        opener = urllib.request.build_opener(*handlers)
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent, "Accept": accept})
        try:
            return opener.open(request, timeout=timeout)
        except UpdateError:
            raise
        except urllib.error.HTTPError as error:
            raise UpdateError(HTTP, f"the server answered HTTP {error.code}") from error
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise UpdateError(NETWORK, str(getattr(error, "reason", error))) from error

    def get_json(self, url: str, timeout: float = 15.0) -> Any:
        with self._open(url, "application/json", timeout) as response:
            body = response.read(MAX_MANIFEST_BYTES + 1)
        if len(body) > MAX_MANIFEST_BYTES:
            raise UpdateError(TOO_LARGE, "the download list is too large")
        try:
            return json.loads(body.decode("utf-8"))
        except ValueError as error:
            raise UpdateError(REFUSED, "the download list is not JSON") from error

    def download(self, url: str, destination: str, expected_size: int, progress: Optional[Callable[[int, int], None]] = None,
                 is_cancelled: Optional[Callable[[], bool]] = None, timeout: float = 60.0) -> Tuple[int, str]:
        """Saves ``url`` to ``destination``, counting and hashing as it arrives. ``(bytes written, sha256)``. Never writes more than ``expected_size``."""
        digest = hashlib.sha256()
        written = 0
        try:
            with self._open(url, "*/*", timeout) as response, open(destination, "wb") as out:
                announced = response.headers.get("Content-Length")
                if announced is not None and announced.isdigit() and int(announced) != expected_size:
                    raise UpdateError(SIZE, f"the server announced {announced} bytes, not {expected_size}")
                while True:
                    if is_cancelled and is_cancelled():
                        raise UpdateError(CANCELLED, "cancelled")
                    block = response.read(1 << 20)
                    if not block:
                        break
                    written += len(block)
                    if written > expected_size:
                        raise UpdateError(SIZE, f"more than the published {expected_size} bytes")
                    digest.update(block)
                    out.write(block)
                    if progress:
                        progress(written, expected_size)
        except UpdateError:
            raise
        except OSError as error:
            raise UpdateError(NETWORK, str(error)) from error
        return written, digest.hexdigest()


# --------------------------------------------------------------------------------------------------
# Checking a file, and swapping it in
# --------------------------------------------------------------------------------------------------

def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def matches_build(path: str, build: Build) -> bool:
    """Whether the file at ``path`` is exactly ``build``: its size, then its SHA-256. False for anything missing or unreadable."""
    try:
        return os.path.isfile(path) and os.path.getsize(path) == build.size and sha256_file(path) == build.sha256
    except OSError:
        return False


def remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def swap(staged: str, target: str) -> None:
    """
    Puts ``staged`` where ``target`` is, keeping the old file beside it as ``<target>.previous``. Both are on the same file system
    (``staged`` was written beside ``target``). If the second step fails the old file is put back, so a failed swap changes nothing.
    """
    previous = target + ".previous"
    had_target = os.path.exists(target)
    if had_target:
        os.replace(target, previous)
    try:
        os.replace(staged, target)
    except OSError:
        if had_target:
            os.replace(previous, target)
        raise


def leftovers(app: Optional[str], home: str) -> List[str]:
    """What an earlier update may have left: the file written beside the app, the old app, and the folder of staged downloads."""
    found: List[str] = []
    if app:
        found += [app + ".new", app + ".previous"]
    staging = staging_dir(home)
    if os.path.isdir(staging):
        found += [os.path.join(staging, name) for name in os.listdir(staging)]
    return found


def staging_dir(home: str) -> str:
    return os.path.join(home, "update")


def restart_environment(env: Dict[str, str]) -> Dict[str, str]:
    """
    The environment the new copy starts with: this process's, minus what only made sense inside it. A PyInstaller app points
    ``_MEIPASS2`` and ``LD_LIBRARY_PATH`` at its own unpacked files, and an AppImage sets ``APPDIR`` and friends; the new copy sets its own.
    """
    clean = dict(env)
    for key in ("_MEIPASS2", "APPDIR", "APPIMAGE", "ARGV0", "OWD"):
        clean.pop(key, None)
    original = clean.pop("LD_LIBRARY_PATH_ORIG", None)
    if original is not None:
        clean["LD_LIBRARY_PATH"] = original
    elif "LD_LIBRARY_PATH" in clean and getattr(sys, "frozen", False):
        clean.pop("LD_LIBRARY_PATH")
    clean["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return clean


# The restart on Linux: wait until this app has really gone (its lock is released when it exits), then run the new one.
WAIT_THEN_RUN = 'pid=$1; app=$2; shift 2; while kill -0 "$pid" 2>/dev/null; do sleep 0.5; done; exec "$app" "$@"'


def default_spawn(argv: Sequence[str]) -> Any:
    """Starts ``argv`` on its own, detached, with nothing connected to this app."""
    flags: Dict[str, Any] = {}
    if sys.platform == "win32":
        flags["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000      # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    else:
        flags["start_new_session"] = True
    return subprocess.Popen(list(argv), env=restart_environment(dict(os.environ)), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, close_fds=True, **flags)


def finish_update(wait_pid: int, staged: str, target: str, restart_args: Sequence[str],
                  alive: Callable[[int], bool], sleep: Callable[[float], None] = time.sleep,
                  clock: Callable[[], float] = time.monotonic, spawn: Callable[[Sequence[str]], Any] = default_spawn,
                  copy: Callable[[str, str], Any] = shutil.copyfile) -> int:
    """
    The Windows update step (``DLNE-Donate.exe --finish-update <pid> <target> <args...>``, run from the new exe in the app-data folder):
    wait for the old app to exit, copy this file over it, start it again. Exit codes: 0 swapped and restarted, 3 the old app did not
    exit in time (nothing changed), 4 the swap failed (the old app is put back and started again), 5 the app could not be started.
    After a failed swap the old app is started again, so the owner's tray comes back.
    """
    deadline = clock() + WAIT_FOR_EXIT_S
    while alive(wait_pid):
        if clock() > deadline:
            return 3
        sleep(0.5)
    fresh = target + ".new"
    outcome = 4
    deadline = clock() + SWAP_TRIES_S
    while True:
        try:
            copy(staged, fresh)
            swap(fresh, target)
            outcome = 0
            break
        except OSError:
            remove_quietly(fresh)
            if clock() > deadline:
                break
            sleep(0.5)
    try:
        spawn([target] + list(restart_args))
    except OSError:
        return 5
    return outcome


# --------------------------------------------------------------------------------------------------
# The manager the app talks to
# --------------------------------------------------------------------------------------------------

def run_in_thread(call: Callable[[], Any]) -> None:
    threading.Thread(target=call, name="update-check", daemon=True).start()


def problem_text(error: UpdateError, step: str = "download") -> str:
    """Why a step stopped, in plain words. In every case nothing was kept and the running version stays."""
    if step == "check":
        return {REFUSED: "The update list was refused, so it was not used.", REDIRECTED: "The update list was redirected, so it was not used.",
                TOO_LARGE: "The update list is too large to be real, so it was not used."}.get(
                    error.problem, f"Could not check for a newer DLNE Donate ({error}).")
    return {
        CANCELLED: "The update was cancelled. Nothing was kept.",
        SIZE: "The download was not the published size, so it was deleted. This version keeps running.",
        HASH: "The download did not match its published SHA-256, so it was deleted. This version keeps running.",
        REDIRECTED: "The download was redirected, so it was refused. Nothing was kept.",
        REFUSED: f"The download was refused ({error}). Nothing was kept.",
        SAVE: f"The update could not be put in place ({error}). This version keeps running.",
    }.get(error.problem, f"The download failed ({error}). Nothing was kept.")


@dataclass(frozen=True)
class View:
    """What the window and the tray show about updates."""
    version: str = ""               # the newer version, "" when there is none (or the owner said Later)
    text: str = ""                  # "DLNE Donate 1.2.0 is available"
    busy: str = ""                  # "", "checking", "downloading" or "installing"
    progress: float = 0.0           # 0..1 while downloading
    note: str = ""                  # the last thing that went wrong
    can_update_now: bool = False    # false for a build that cannot replace itself: the window offers the download page
    notes: str = ""                 # the release's one-line notes

    @property
    def visible(self) -> bool:
        return bool(self.version or self.note)

    @property
    def status_line(self) -> str:
        if self.busy == "downloading":
            return f"Downloading DLNE Donate {self.version}... {int(self.progress * 100)}%"
        if self.busy == "installing":
            return f"Installing DLNE Donate {self.version}..."
        return self.text


class UpdateManager:
    def __init__(self, home: str, current: str = __version__, auto: Callable[[], bool] = lambda: False, http: Any = None,
                 clock: Callable[[], float] = time.time, platform: Any = "auto", app_path: Optional[Callable[[], Optional[str]]] = None,
                 spawn: Callable[[Sequence[str]], Any] = default_spawn, log: Callable[[str], None] = lambda _m: None,
                 on_change: Callable[[], None] = lambda: None, run_async: Callable[[Callable[[], Any]], None] = run_in_thread,
                 enabled: bool = True, manifest_url: str = MANIFEST_URL, host: str = HOST, scheme: str = "https"):
        self.home = home
        self.current = current
        self._auto = auto
        self._http = http
        self._clock = clock
        self._platform: Optional[str] = platform_key() if platform == "auto" else platform      # "windows", "linux" or None
        self._app_path = app_path or running_app
        self._spawn = spawn
        self._log = log
        self._on_change = on_change
        self._run_async = run_async
        self.enabled = enabled
        self._manifest_url = manifest_url
        self._host, self._scheme = host, scheme
        self._lock = threading.RLock()
        self.release: Optional[Release] = None      # a newer release, when one is published for this computer
        self.later_for = ""                         # the version the owner said "Later" to
        self.busy = ""
        self.progress = 0.0
        self.note = ""
        self.ready: Optional[str] = None            # the verified download, kept until it is installed
        self.next_check = 0.0
        self.notified = ""                          # the version a notification was already shown for
        self.auto_blocked = ""                      # the version whose automatic download failed (not tried again until the next check)
        self._cancel = False

    # ---- what is known -----------------------------------------------------------------------------

    @property
    def build(self) -> Optional[Build]:
        release = self.release
        return release.builds.get(self._platform or "") if release else None

    def can_replace_itself(self) -> bool:
        return bool(self._platform and self._app_path())

    def view(self) -> View:
        release = self.release
        shown = release is not None and (release.version != self.later_for or bool(self.busy))
        version = release.version if release and shown else ""
        return View(version=version, text=AVAILABLE_TEXT.format(version=version) if version else "", busy=self.busy,
                    progress=self.progress, note=self.note, can_update_now=bool(version) and self.can_replace_itself(),
                    notes=release.notes if release and shown else "")

    def to_notify(self) -> str:
        """The newer version a tray notification has not been shown for yet ("" when there is none); calling it marks it shown."""
        version = self.view().version
        if version and not self.busy and version != self.notified:
            self.notified = version
            return version
        return ""

    def later(self) -> None:
        """The owner said Later: the notice goes away until the next check finds a newer version still, or the app starts again."""
        with self._lock:
            if self.release is not None:
                self.later_for = self.release.version
            self.note = ""
        self._on_change()

    # ---- checking ----------------------------------------------------------------------------------

    def due(self) -> bool:
        return self.enabled and not self.busy and self._clock() >= self.next_check

    def tick(self) -> None:
        """Called often. Starts a check when one is due, and in automatic mode the download of a release that was found."""
        if self.due():
            self.next_check = self._clock() + CHECK_EVERY_S      # set now, so a slow check is not started twice
            self._run_async(self.check)
        elif (self.enabled and self.release is not None and not self.busy and not self.ready and self._auto()
              and self.can_replace_itself() and self.build is not None and self.auto_blocked != self.release.version):
            self._run_async(self._download_quietly)

    def check(self) -> Optional[Release]:
        """Reads the manifest. Never raises. The newer release, or None; a failed check keeps what was known and tries again in an hour."""
        with self._lock:
            if self.busy:
                return self.release
            self.busy = "checking"
        self._on_change()
        found: Optional[Release] = None
        try:
            http = self._http or Http()
            raw = http.get_json(self._manifest_url)
            release, why = parse_manifest(raw, self._host, self._scheme)
            if release is None:
                raise UpdateError(REFUSED, why)
            self.next_check = self._clock() + CHECK_EVERY_S
            self.auto_blocked = ""                    # a new check lets an automatic download that failed try again
            if is_newer(release.version, self.current) and self._platform in release.builds:
                found = release
            self._log(f"Checked for a newer DLNE Donate: {release.version} is published, this is {self.current}"
                      f"{'' if found else ' (nothing to update)'}.")
        except UpdateError as error:
            self.next_check = self._clock() + RETRY_AFTER_FAILURE_S
            self._log(f"Could not check for a newer DLNE Donate: {error}")
            found = self.release                      # keep what was known
        except Exception as error:  # noqa: BLE001 - a check never breaks the app
            self.next_check = self._clock() + RETRY_AFTER_FAILURE_S
            self._log(f"Could not check for a newer DLNE Donate: {error}")
            found = self.release
        finally:
            with self._lock:
                self.busy = ""
        with self._lock:
            if found is None or self.release is None or found.version != self.release.version:
                self.discard_download()                 # a download of another version than the one now published is of no use
                self.auto_blocked = ""
            self.release = found
        self._on_change()
        return found

    # ---- downloading -------------------------------------------------------------------------------

    def staged_path(self, build: Build) -> Optional[str]:
        app = self._app_path()
        if not app:
            return None
        if self._platform == "windows":
            return os.path.join(staging_dir(self.home), build.file)
        return app + ".new"                            # beside the running AppImage: the swap is then one rename

    def discard_download(self) -> None:
        path, self.ready = self.ready, None
        if path:
            remove_quietly(path)

    def cancel(self) -> None:
        self._cancel = True

    def download(self) -> str:
        """
        Downloads the newer build next to where it will be installed and keeps it only when it has the published size and SHA-256.
        Returns the path. Raises :class:`UpdateError` (and has deleted the download) for anything else.
        """
        with self._lock:
            release, build = self.release, self.build
            if release is None or build is None:
                raise UpdateError(REFUSED, "there is no newer DLNE Donate to download")
            if self.busy == "downloading":
                raise UpdateError(REFUSED, "a download is already running")
            if self.ready and matches_build(self.ready, build):
                return self.ready
            target = self.staged_path(build)
            if target is None:
                raise UpdateError(SAVE, "this copy of the app cannot replace itself")
            self.discard_download()
            self.busy, self.progress, self.note, self._cancel = "downloading", 0.0, "", False
        self._on_change()
        try:
            directory = os.path.dirname(target)
            try:
                os.makedirs(directory, exist_ok=True)
            except OSError as error:
                raise UpdateError(SAVE, f"cannot write in {directory}: {error}") from error
            if not os.access(directory, os.W_OK):
                raise UpdateError(SAVE, f"cannot write in {directory}")
            remove_quietly(target)
            http = self._http or Http()
            self._log(f"Downloading DLNE Donate {release.version} ({build.size} bytes) to {target}")

            def progressed(done: int, total: int) -> None:
                self.progress = done / total if total else 0.0
                self._on_change()

            try:
                written, digest = http.download(build.url, target, build.size, progressed, lambda: self._cancel)
            except OSError as error:
                raise UpdateError(SAVE, str(error)) from error
            if written != build.size:
                raise UpdateError(SIZE, f"{written} bytes, not the published {build.size}")
            if digest != build.sha256:
                raise UpdateError(HASH, "the SHA-256 does not match the published one")
            if self._platform == "linux":
                os.chmod(target, 0o755)
            self.ready = target
            self._log(f"DLNE Donate {release.version} was downloaded and checked.")
            return target
        except UpdateError as error:
            remove_quietly(target)
            self.note = problem_text(error)
            self._log(f"The update was not kept: {error}")
            raise
        except Exception as error:  # noqa: BLE001
            remove_quietly(target)
            self.note = problem_text(UpdateError(NETWORK, str(error)))
            raise UpdateError(NETWORK, str(error)) from error
        finally:
            with self._lock:
                self.busy = ""
            self._on_change()

    def _download_quietly(self) -> None:
        try:
            self.download()
        except UpdateError:
            if self.release is not None:
                self.auto_blocked = self.release.version      # the note says why; the next check tries again

    # ---- installing --------------------------------------------------------------------------------

    def wants_auto_install(self, idle: bool) -> bool:
        """Automatic mode: a verified download is waiting and the worker is idle, so this is the moment."""
        return bool(self.enabled and self._auto() and self.ready and idle and not self.busy and self.can_replace_itself())

    def install(self, pid: Optional[int] = None, restart_args: Sequence[str] = ()) -> bool:
        """
        Puts the verified download in place and arranges for the new copy to start once this app has exited. Call it after the worker
        has stopped. True when the hand-over is arranged (the caller then quits); False, with the reason in ``note``, when nothing changed.
        The staged file is checked again first: it may have waited days.
        """
        pid = os.getpid() if pid is None else pid
        build, staged, app = self.build, self.ready, self._app_path()
        try:
            if build is None or not staged or not app:
                raise UpdateError(SAVE, "there is nothing downloaded to install")
            if not matches_build(staged, build):
                remove_quietly(staged)
                self.ready = None
                raise UpdateError(HASH, "the downloaded file no longer matches the published size and SHA-256")
            self.busy = "installing"
            self._on_change()
            if self._platform == "windows":
                # The new exe does the swap itself once this app has exited; nothing is replaced while this one runs.
                self._spawn([staged, "--finish-update", str(pid), app] + list(restart_args))
            else:
                try:
                    swap(staged, app)
                except OSError as error:
                    raise UpdateError(SAVE, str(error)) from error
                self._spawn(["sh", "-c", WAIT_THEN_RUN, "sh", str(pid), app] + list(restart_args))
            self.ready = None
            self._log(f"DLNE Donate {self.release.version if self.release else ''} is installed; it starts when this app has exited.")
            return True
        except (UpdateError, OSError) as error:
            self.busy = ""
            self.note = problem_text(error if isinstance(error, UpdateError) else UpdateError(SAVE, str(error)))
            self._log(f"The update was not installed: {error}")
            self._on_change()
            return False

    def cleanup(self) -> None:
        """At start: remove what an earlier update left behind (the old app, an unfinished download)."""
        for path in leftovers(self._app_path(), self.home):
            remove_quietly(path)
