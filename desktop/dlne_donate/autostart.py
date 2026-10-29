"""
"Start with my computer": per user, no administrator rights. ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run`` on
Windows, an XDG autostart entry on Linux (the same two places engine/setup_core.py ``set_autostart`` uses, under this
app's own name). The app starts in the tray and does nothing until the owner clicks Donate, unless they chose to resume.
"""

import os
import shlex
import sys
from typing import Dict, List, Optional

from . import APP_NAME

ENTRY_NAME = "DLNE-Donate"
WINDOWS_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def launch_command(extra: Optional[List[str]] = None) -> List[str]:
    """The argv that starts this app: the program itself when frozen, else the interpreter running it."""
    args = list(extra or [])
    appimage = os.environ.get("APPIMAGE")
    if appimage and os.path.isfile(appimage):
        return [appimage] + args          # inside an AppImage, sys.executable is a path in a temporary mount
    if getattr(sys, "frozen", False):
        return [sys.executable] + args
    return [sys.executable, "-m", "dlne_donate"] + args


def _windows_command(argv: List[str]) -> str:
    return " ".join(f'"{part}"' if (" " in part or not part) else part for part in argv)


def linux_autostart_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "autostart", "dlne-donate.desktop")


def desktop_entry(argv: List[str], working_dir: str = "") -> str:
    exec_line = " ".join(shlex.quote(part) for part in argv)
    lines = [
        "[Desktop Entry]", "Type=Application", f"Name={APP_NAME}", "Comment=Lend this computer's GPU or CPU to the DLNE swarm",
        f"Exec={exec_line}", "Terminal=false", "X-GNOME-Autostart-enabled=true",
    ]
    if working_dir:
        lines.insert(4, f"Path={working_dir}")
    return "\n".join(lines) + "\n"


def is_enabled() -> bool:
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WINDOWS_RUN_KEY) as key:
                winreg.QueryValueEx(key, ENTRY_NAME)
            return True
        except OSError:
            return False
    return os.path.exists(linux_autostart_path())


def set_enabled(enabled: bool, argv: Optional[List[str]] = None) -> Dict[str, object]:
    """``{"ok", "enabled", "detail"}``. Never raises."""
    argv = argv or launch_command(["--autostart"])
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, WINDOWS_RUN_KEY) as key:
                if enabled:
                    winreg.SetValueEx(key, ENTRY_NAME, 0, winreg.REG_SZ, _windows_command(argv))
                else:
                    try:
                        winreg.DeleteValue(key, ENTRY_NAME)
                    except FileNotFoundError:
                        pass
            return {"ok": True, "enabled": bool(enabled), "detail": "Start with my computer updated."}
        except OSError as error:
            return {"ok": False, "enabled": is_enabled(), "detail": f"Could not update the Run key: {error}"}
    path = linux_autostart_path()
    try:
        if enabled:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(desktop_entry(argv))
        elif os.path.exists(path):
            os.remove(path)
        return {"ok": True, "enabled": bool(enabled), "detail": "Start with my computer updated."}
    except OSError as error:
        return {"ok": False, "enabled": is_enabled(), "detail": f"Could not write the autostart entry: {error}"}
