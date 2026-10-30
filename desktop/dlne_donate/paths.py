"""Where this app keeps its files. One folder per user, nothing that needs administrator rights."""

import os
import sys

# Set by tests and by the smoke scripts: everything the app writes goes under this folder instead.
HOME_ENV = "DLNE_DONATE_HOME"


def app_data_dir() -> str:
    """``%LOCALAPPDATA%\\DLNE Donate`` on Windows, ``~/.local/share/dlne-donate`` elsewhere (XDG_DATA_HOME honoured)."""
    override = os.environ.get(HOME_ENV)
    if override:
        return os.path.abspath(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        return os.path.join(base, "DLNE Donate")
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "dlne-donate")


def engine_dir(home: str = "") -> str:
    """The folder the verified engine release is unpacked into (also holds config/, models/, bin/)."""
    return os.path.join(home or app_data_dir(), "engine")


def settings_path(home: str = "") -> str:
    return os.path.join(home or app_data_dir(), "settings.json")


def state_path(home: str = "") -> str:
    """What the app is doing right now, as one small JSON file (read by the smoke tests and by a second launch)."""
    return os.path.join(home or app_data_dir(), "state.json")


def lock_path(home: str = "") -> str:
    return os.path.join(home or app_data_dir(), "instance.lock")


def show_request_path(home: str = "") -> str:
    """A second launch touches this file; the running app notices it and opens Settings (no socket needed)."""
    return os.path.join(home or app_data_dir(), "show-settings.request")


def log_path(home: str = "") -> str:
    return os.path.join(home or app_data_dir(), "donate.log")


# Tests name the presence file here; otherwise it is the user's real ~/.dlne/editor.json.
EDITOR_FILE_ENV = "DLNE_DONATE_EDITOR_FILE"


def editor_presence_path() -> str:
    """
    The file the DLNE extension keeps while VS Code runs (``{"pid", "updated", "version"}``). It lives in the user's home, not in
    this app's folder, because the extension writes it: ``~/.dlne/editor.json`` on every system (``%USERPROFILE%`` on Windows).
    """
    override = os.environ.get(EDITOR_FILE_ENV)
    if override:
        return os.path.abspath(override)
    return os.path.join(os.path.expanduser("~"), ".dlne", "editor.json")


def ensure_private_dir(path: str) -> str:
    """Create ``path`` (and parents); on POSIX the folder is for this user only."""
    os.makedirs(path, exist_ok=True)
    if os.name == "posix":
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    return path
