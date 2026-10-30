"""The tray app: wires the tray icon, the Settings window and the worker together, on one Tk thread."""

import atexit
import os
import queue
import signal
import sys
import threading
import time
import webbrowser
from typing import Any, Optional

from . import APP_NAME, instance, paths, updater
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
        self._asking_cuda = False
        self._downloading_update = False
        self._updating = False              # an update is installing: the worker is stopping, then the swap, then this app exits
        self._update_started = 0.0
        self._update_args: list = []
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
            "update": lambda: self._events.put("update-now"),
        }, donate_enabled=lambda: not self.model.describe().machine_blocker and not self.model.paused,
            update_label=self._update_label)
        has_tray = False if self.window_only else self.tray.start()
        window_only = self.window_only or not has_tray
        self.window = ui.SettingsWindow(self.root, self.model, on_quit=lambda: self._events.put("quit"), window_only=window_only,
                                        on_update=lambda: self._events.put("update-now"))
        if window_only:
            self.window.show()
        elif self.show_settings or not self.autostart:
            # A launch by the owner (a double-click) shows the window: the tray icon alone can sit in the hidden-icons
            # flyout, and nothing visible would happen. Only the system's own start at sign-in stays quiet.
            self.window.show()
        s = self.model.settings
        # Resuming by itself never lends a GPU the owner has not yet been told about (the first click shows that notice).
        resume = s.resume_donating and (s.lend_notice_seen or not (s.gpu_enabled and self.model.hardware.has_gpu))
        if self.model.updates.enabled:
            self.model.updates.cleanup()             # what an earlier update left behind (the old app, an unfinished download)
        self.model.check_editor(force=True)          # VS Code first: with DLNE open in it, donating waits until it is closed
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
                elif event == "update-now":
                    self._update_now()
                elif event == "update-downloaded":
                    self._update_downloaded()
                elif event == "quit":
                    self._begin_quit()
        except queue.Empty:
            pass
        if instance.take_show_request(self.home):
            self.window.show(near_pointer=False)
        self.model.check_editor()
        if self._poll_updates():
            return                           # an update was installed and the app has quit
        if not self._asking_cuda and not self._quitting and self.model.cuda_question_due():
            self._asking_cuda = True
            self.root.after(0, self._ask_cuda)
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

    # ---- DLNE Donate updates itself (updater.py) ---------------------------------------------------

    def _update_label(self) -> str:
        view = self.model.updates.view()
        return f"Update DLNE Donate to {view.version}" if view.version and not view.busy else ""

    def _poll_updates(self) -> bool:
        """Looks after updates once per poll. True when an update was installed and the app has just quit."""
        updates = self.model.updates
        if self._quitting:
            return False
        if self._updating:
            return self._continue_update()
        updates.tick()
        version = updates.to_notify()
        if version and self.tray is not None:
            self.tray.notify(updater.AVAILABLE_TEXT.format(version=version))
        status = self.model.status()
        idle = not status.running                    # the worker is not donating (stopped, paused for VS Code, or failed)
        if updates.wants_auto_install(idle):
            self._update_now(automatic=True)
        return False

    def _update_now(self, automatic: bool = False) -> None:
        """Update now (the window's button, the tray's entry, or the automatic setting at an idle moment)."""
        updates = self.model.updates
        if self._quitting or self._updating or self._downloading_update or updates.release is None:
            return
        if not updates.can_replace_itself():
            webbrowser.open(updater.PAGE_URL)        # a build that cannot swap itself: the DLNE page has the download
            return
        self._update_args = ["--autostart"] if automatic else ["--settings"]
        if updates.ready:
            self._events.put("update-downloaded")
            return
        self._downloading_update = True
        threading.Thread(target=self._download_update, name="update-download", daemon=True).start()

    def _download_update(self) -> None:
        try:
            self.model.updates.download()
        except updater.UpdateError:
            pass                                     # the window shows what the manager noted
        finally:
            self._downloading_update = False
            self._events.put("update-downloaded")

    def _update_downloaded(self) -> None:
        """The verified download is ready: stop the worker the way Stop does, then swap (see _continue_update)."""
        updates = self.model.updates
        if not updates.ready or self._quitting or self._updating:
            return
        # Donating comes back with the new copy when it was donating (or was about to, after a VS Code pause).
        if self.model.status().running or self.model.vscode_first.resume_wanted:
            self._update_args = self._update_args + ["--donate"]
        self._updating = True
        self._update_started = time.monotonic()
        self.model.stop()

    def _continue_update(self) -> bool:
        """Called every poll while an update waits for the worker to leave; then the swap, and this app exits (True)."""
        if self.model.status().running and time.monotonic() - self._update_started <= QUIT_WAIT_S:
            return False
        if self.model.updates.install(os.getpid(), self._update_args):
            self._quitting = True
            self._finish_quit()
            return True
        # Nothing changed: the old version keeps running, and donates again if it was donating.
        self._updating = False
        if "--donate" in self._update_args:
            self.model.donate()
        return False

    def _ask_cuda(self) -> None:
        """The worker said this computer can run CUDA and the owner has not answered: ask, in the Settings window, once."""
        try:
            self.window.show()
            self.model.answer_cuda(self.window.ask_cuda())
        finally:
            self._asking_cuda = False

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
        self._updating = False               # quitting wins over an update that was waiting for the worker
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
