"""The tray icon and its menu: the official DLNE icon with a status dot, and the five menu entries."""

import threading
import webbrowser
from typing import Any, Callable, Optional

from . import WEBSITE_URL, bundled
from . import worker as worker_mod

BADGE = {
    worker_mod.IDLE: (138, 143, 152),
    worker_mod.PREPARING: (224, 161, 0),
    worker_mod.STARTING: (224, 161, 0),
    worker_mod.STOPPING: (224, 161, 0),
    worker_mod.DONATING: (46, 158, 79),
    worker_mod.ERROR: (209, 52, 56),
}

_base_cache = {}


def make_icon(state: str, size: int = 64) -> Any:
    """The DLNE icon at ``size`` pixels with a coloured dot in the corner for the state (dimmed while idle)."""
    from PIL import Image, ImageDraw, ImageEnhance

    key = size
    if key not in _base_cache:
        _base_cache[key] = Image.open(bundled.asset_path("icon.png")).convert("RGBA").resize((size, size), Image.LANCZOS)
    image = _base_cache[key].copy()
    if state == worker_mod.IDLE:
        image = ImageEnhance.Brightness(image).enhance(0.7)
    draw = ImageDraw.Draw(image)
    radius = max(5, size // 5)
    cx, cy = size - radius - 1, size - radius - 1
    draw.ellipse((cx - radius - 2, cy - radius - 2, cx + radius + 2, cy + radius + 2), fill=(20, 22, 28, 255))
    draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=BADGE.get(state, BADGE[worker_mod.IDLE]) + (255,))
    return image


class Tray:
    """The pystray icon. Everything it does is handed to ``actions``; it never touches the worker or the window itself."""

    def __init__(self, status: Callable[[], worker_mod.Status], autostart_enabled: Callable[[], bool], actions: dict,
                 donate_enabled: Callable[[], bool] = lambda: True):
        self._status = status
        self._autostart_enabled = autostart_enabled
        self._actions = actions
        self._donate_enabled = donate_enabled
        self.icon: Optional[Any] = None
        self._last_state = ""
        self._last_title = ""
        self._last_menu_key: Any = None
        self._label_running = False       # whether the first entry last read "Stop donating" (the click does what its label said)

    def _toggle_label(self, _item: Any = None) -> str:
        self._label_running = self._status().running
        return "Stop donating" if self._label_running else "Donate"

    def _toggle_enabled(self, _item: Any = None) -> bool:
        """A computer that can lend nothing has nothing to start; "Stop donating" is always available."""
        return self._status().running or self._donate_enabled()

    def _toggle_clicked(self, _icon: Any = None, _item: Any = None) -> None:
        self._actions["toggle"](self._label_running)

    def start(self) -> bool:
        """Show the icon (on a thread of its own). False when this computer has no system tray to show it in."""
        try:
            import pystray
            menu = pystray.Menu(
                pystray.MenuItem(self._toggle_label, self._toggle_clicked, enabled=self._toggle_enabled),
                pystray.MenuItem("Settings…", self._call("settings"), default=True),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Start with my computer", self._call("autostart"), checked=lambda _i: self._autostart_enabled()),
                pystray.MenuItem("Open the DLNE website", lambda _i, _it=None: webbrowser.open(WEBSITE_URL)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", self._call("quit")),
            )
            status = self._status()
            self.icon = pystray.Icon("dlne-donate", make_icon(status.state), status.tooltip, menu)
            self.icon.run_detached()
            return True
        except Exception:  # noqa: BLE001 - no tray: the caller shows the window instead
            self.icon = None
            return False

    def _call(self, name: str) -> Callable[..., None]:
        def run(_icon: Any = None, _item: Any = None) -> None:
            self._actions[name]()
        return run

    def update(self) -> None:
        """The state or tooltip changed: redraw the icon and its hover text."""
        icon = self.icon
        if icon is None:
            return
        status = self._status()
        try:
            if status.state != self._last_state:
                icon.icon = make_icon(status.state)
                self._last_state = status.state
            if status.tooltip != self._last_title:
                icon.title = status.tooltip
                self._last_title = status.tooltip
            # The menu is rebuilt only when something it shows changed, not every second: a menu that is open while it is
            # rebuilt would have a label that no longer matches what a click is taken to mean.
            key = (status.running, self._autostart_enabled(), self._toggle_enabled())
            if key != self._last_menu_key:
                self._last_menu_key = key
                icon.update_menu()
        except Exception:  # noqa: BLE001 - a broken tray never stops donating
            pass

    def visible(self) -> bool:
        """
        Whether the icon is on screen. On Linux the legacy system-tray backend docks into a tray manager; a desktop without
        one (GNOME without its AppIndicator extension, a bare window manager) leaves the icon nowhere, which is when the app
        shows its window instead.
        """
        icon = self.icon
        if icon is None:
            return False
        if type(icon).__module__.endswith("_xorg"):
            return bool(getattr(icon, "_systray_manager", None))
        return True

    def stop(self) -> None:
        """Remove the icon. pystray's stop can wait for a tray that never came, so it runs on a thread and is not waited for long."""
        icon, self.icon = self.icon, None
        if icon is not None:
            thread = threading.Thread(target=lambda: _quietly(icon.stop), name="tray-stop", daemon=True)
            thread.start()
            thread.join(2.0)


def _quietly(call: Callable[[], Any]) -> None:
    try:
        call()
    except Exception:  # noqa: BLE001
        pass
