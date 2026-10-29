"""The tray app: wires the tray icon, the Settings window and the worker together, on one Tk thread."""

import atexit
import os
import queue
import signal
import sys
import time
from typing import Any, Optional

from . import APP_NAME, instance, paths
from .model import Model

POLL_MS = 200
REFRESH_EVERY_S = 1.0
QUIT_WAIT_S = 20.0


class App:
    def __init__(self, show_settings: bool = False, donate_now: bool = False, window_only: bool = False,
                 home: str = "", model: Optional[Model] = None, autostart: bool = False):
        self.home = home or paths.app_data_dir()
        self.autostart = autostart          # started by the system at sign-in (--autostart): stay in the tray, show nothing
        self.show_settings = show_settings
        self.donate_now = donate_now
        self.window_only = window_only
        self._model = model
        self._events: "queue.SimpleQueue[str]" = queue.SimpleQueue()
        self._quitting = False
        self._quit_started = 0.0
        self._last_refresh = 0.0
        self.root: Any = None
        self.window: Any = None
        self.tray: Any = None

    # ---- run ---------------------------------------------------------------------------------------

    def run(self) -> int:
        lock = instance.InstanceLock(self.home)
        if not lock.acquire():
            instance.ask_running_instance_to_show(self.home)
            print(f"{APP_NAME} is already running; it was asked to open its settings.")
            return 0
        try:
            return self._run_locked()
        finally:
            lock.release()

    def _run_locked(self) -> int:
        import tkinter as tk

        from . import trayicon, ui

        if sys.platform == "win32":
            # Without this Windows scales the window up as a blurry bitmap on a high-DPI screen.
            try:
                import ctypes
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:  # noqa: BLE001
                try:
                    ctypes.windll.user32.SetProcessDPIAware()
                except Exception:  # noqa: BLE001
                    pass
        self.root = tk.Tk()
        self.root.withdraw()
        self.model = self._model or Model(self.home)
        self.model.listeners.append(lambda: self._events.put("refresh"))
        atexit.register(self._emergency_stop)
        for name in ("SIGTERM", "SIGINT", "SIGHUP"):
            sig = getattr(signal, name, None)
            if sig is not None:
                try:
                    signal.signal(sig, lambda *_a: self._events.put("quit"))
                except (ValueError, OSError):
                    pass

        self.tray = trayicon.Tray(self.model.status, self.model.autostart_enabled, {
            # The tray tells which label the owner clicked ("Stop donating" / "Donate"): the click does that, not what the
            # state happens to be when the event is handled.
            "toggle": lambda label_running: self._events.put("toggle-stop" if label_running else "toggle-start"),
            "settings": lambda: self._events.put("settings-pointer"),
            "autostart": lambda: self._events.put("autostart"),
            "quit": lambda: self._events.put("quit"),
        }, donate_enabled=lambda: not self.model.describe().machine_blocker)
        has_tray = False if self.window_only else self.tray.start()
        window_only = self.window_only or not has_tray
        self.window = ui.SettingsWindow(self.root, self.model, on_quit=lambda: self._events.put("quit"), window_only=window_only)
        if window_only:
            self.window.show()
        elif self.show_settings or not self.autostart:
            # A launch by the owner (a double-click) shows the window: the tray icon alone can sit in the hidden-icons
            # flyout, and nothing visible would happen. Only the system's own start at sign-in stays quiet.
            self.window.show()
        s = self.model.settings
        # Resuming by itself never lends a GPU the owner has not yet been told about (the first click shows that notice).
        resume = s.resume_donating and (s.lend_notice_seen or not (s.gpu_enabled and self.model.hardware.has_gpu))
        if self.donate_now or resume:
            self.model.donate()
        self.model._write_state(self.model.status())
        self.root.after(POLL_MS, self._poll)
        if has_tray:
            self.root.after(3000, self._check_tray)
        self.root.mainloop()
        return 0

    def _check_tray(self) -> None:
        """No system tray took the icon: the window is the app, so open it (closing it then only minimises it)."""
        if self.tray.visible() or self._quitting:
            return
        self.window.set_window_only(True)
        self.window.show()

    # ---- the one thread that touches Tk ------------------------------------------------------------

    def _poll(self) -> None:
        refresh = False
        try:
            while True:
                event = self._events.get_nowait()
                if event == "refresh":
                    refresh = True
                elif event in ("toggle-stop", "toggle-start"):
                    self._toggle(stop=event == "toggle-stop")
                elif event == "settings-pointer":
                    self.window.show(near_pointer=True)
                elif event == "settings":
                    self.window.show()
                elif event == "autostart":
                    self.model.set_autostart(not self.model.autostart_enabled())
                    refresh = True
                elif event == "quit":
                    self._begin_quit()
        except queue.Empty:
            pass
        if instance.take_show_request(self.home):
            self.window.show(near_pointer=False)
        now = time.monotonic()
        if refresh or now - self._last_refresh > REFRESH_EVERY_S:
            self._last_refresh = now
            self.tray.update()
            if self.window.visible():
                self.window.refresh()
        if self._quitting:
            if not self.model.status().running or now - self._quit_started > QUIT_WAIT_S:
                self._finish_quit()
                return
        self.root.after(POLL_MS, self._poll)

    def _toggle(self, stop: bool) -> None:
        """The tray's Donate / Stop donating entry. ``stop`` is what its label said when it was clicked."""
        status = self.model.status()
        if stop:
            if status.running:
                self.model.stop()
            return                           # it had already stopped by itself: a "Stop" click never starts anything
        if status.running:
            return                           # "Donate" was clicked just as donating began: nothing to do
        if self.model.describe().can_donate:
            if self.window._confirm_lending():
                self.model.donate()
        else:
            self.window.show(near_pointer=True)       # nothing to lend yet: the sliders are where to fix that

    def _begin_quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        self._quit_started = time.monotonic()
        self.model.stop()

    def _finish_quit(self) -> None:
        self.model.quit()                # waits for the worker, then kills anything of ours still running
        self.tray.stop()
        try:
            self.root.destroy()
        except Exception:  # noqa: BLE001
            pass

    def _emergency_stop(self) -> None:
        """The interpreter is exiting by some other road (an exception, a closed console): leave nothing running."""
        try:
            self.model.controller.shutdown()
        except Exception:  # noqa: BLE001
            pass
