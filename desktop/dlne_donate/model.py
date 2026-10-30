"""The app's state in one place (settings, the machine, what the swarm can use, the worker), shared by the window and the tray."""

import json
import os
import threading
import time
from typing import Any, Callable, List, Optional

from . import (COORDINATOR_URL, __version__, PUBLIC_NETWORK_KEY, autostart, bundled, engine_flavour, netstatus, paths, settings as settings_store,
               tiers, updater, vscode_first)
from .worker import STOPPING, Status, WorkerController


class Model:
    def __init__(self, home: str = "", hardware: Optional[tiers.Hardware] = None, base_url: str = COORDINATOR_URL,
                 select: Any = None, controller_factory: Optional[Callable[..., WorkerController]] = None,
                 fetch_info: bool = True, editor_present: Optional[Callable[[], bool]] = None,
                 other_worker: Optional[Callable[[], bool]] = None, now: Callable[[], float] = time.time,
                 clock: Callable[[], float] = time.monotonic, updates: Optional[updater.UpdateManager] = None):
        self.home = home or paths.app_data_dir()
        paths.ensure_private_dir(self.home)
        if hardware is None:
            from . import hardware as probe
            hardware = probe.detect()
        self.hardware = hardware
        self.base_url = base_url
        self.swarm = netstatus.SwarmInfo()
        if select is None:
            try:
                select = bundled.import_snapshot("coding_member_select")
            except Exception:  # noqa: BLE001 - the window then says less about speed
                select = None
        self.select = select
        self._lock = threading.RLock()
        self.settings = settings_store.load(self.hardware, self.swarm.app_tiers, os.path.join(self.home, "settings.json"))
        self.listeners: List[Callable[[], None]] = []
        self._log_lock = threading.Lock()
        factory = controller_factory or WorkerController
        self.controller: WorkerController = factory(
            self.home, lambda: self.settings, lambda: self.hardware, base_url=base_url, network_key=PUBLIC_NETWORK_KEY,
            on_change=self._status_changed, log=self._log)
        self._now = now
        # VS Code first (vscode_first.py): the check reads ~/.dlne/editor.json unless a test hands in its own.
        self.vscode_first = vscode_first.VsCodeFirst(
            editor_present or (lambda: vscode_first.read_presence(paths.editor_presence_path(), time.time())),
            is_running=lambda: self.controller.status().running, is_stopping=lambda: self.controller.status().state == STOPPING,
            stop=self.stop, start=self._resume_donating, on_change=self._paused_changed,
            # A test that names its own editor file gets no scan of the real computer for workers either.
            other_worker=other_worker or (vscode_first.other_worker_running if editor_present is None else (lambda: False)),
            clock=clock)
        # DLNE Donate updates itself (updater.py). Like the swarm fetch below it is off for a model built with fetch_info=False.
        self.updates = updates or updater.UpdateManager(self.home, auto=lambda: self.settings.auto_update, clock=now, log=self._log,
                                                        on_change=self._notify,
                                                        enabled=fetch_info and not os.environ.get(updater.OFF_ENV))
        self._fetching = False
        self._fetched_at = float("-inf")
        if fetch_info:
            self._fetching = True
            netstatus.fetch_in_background(self._swarm_loaded, base_url)

    # ---- notifications -----------------------------------------------------------------------------

    def _notify(self) -> None:
        for listener in list(self.listeners):
            try:
                listener()
            except Exception:  # noqa: BLE001
                pass

    def _status_changed(self, status: Status) -> None:
        self._write_state(status)
        self._notify()

    def refresh_swarm(self, max_age_s: float = 600.0) -> None:
        """Read the swarm's tiers again when the last reading is older than ``max_age_s`` (the window calls this when it opens)."""
        if self._fetching or time.monotonic() - self._fetched_at < max_age_s:
            return
        self._fetching = True
        netstatus.fetch_in_background(self._swarm_loaded, self.base_url)

    def _swarm_loaded(self, info: netstatus.SwarmInfo) -> None:
        self._fetching = False
        if not info.reachable:
            return                                   # keep what we had; an offline refresh must not forget the tiers
        self._fetched_at = time.monotonic()
        with self._lock:
            self.swarm = info
            self.settings = tiers.clamp_settings(self.settings, self.hardware, info.app_tiers)
        self._notify()

    # ---- settings ----------------------------------------------------------------------------------

    def update(self, **changes: Any) -> tiers.Settings:
        """Change some settings: clamped to what this machine can honour, saved, and applied to a running worker."""
        with self._lock:
            from dataclasses import replace
            new = tiers.clamp_settings(replace(self.settings, **changes), self.hardware, self.swarm.app_tiers)
            if new == self.settings:
                return new
            self.settings = new
        settings_store.save(new, os.path.join(self.home, "settings.json"))
        self.controller.apply_settings()
        self._notify()
        return new

    def describe(self) -> tiers.Description:
        with self._lock:
            return tiers.describe(self.settings, self.hardware, self.swarm.coding_tiers, self.swarm.app_tiers, self.select,
                                  self.swarm.app_sizes)

    # ---- the engine flavour and the CUDA question (engine_flavour.py) ------------------------------

    def cuda_question_due(self) -> bool:
        """Whether the window should ask the CUDA question now (once; again after "Not now" and three days)."""
        s = self.settings
        return bool(s.gpu_enabled and not self.paused
                    and engine_flavour.question_due(s.engine, s.cuda_consent, s.cuda_later_ms, self.status().cuda, self._now()))

    def answer_cuda(self, answer: str, choosing_cuda: bool = False) -> tiers.Settings:
        """Record the answer to the CUDA question. Use CUDA makes a running worker restart on the CUDA engine."""
        return self.update(**engine_flavour.answer_changes(answer, self._now(), choosing_cuda, self.settings.engine))

    def choose_engine(self, value: str, ask: Callable[[], str]) -> tiers.Settings:
        """
        The owner picked an engine in the Settings window. Choosing CUDA before agreeing to the download asks the question
        first (``ask`` shows it and returns the answer): only Use CUDA turns it on, the other answers leave the setting as it was.
        """
        if value == "cuda" and self.settings.cuda_consent != "yes":
            return self.answer_cuda(ask(), choosing_cuda=True)
        return self.update(engine=engine_flavour.clean_engine(value))

    # ---- VS Code first ---------------------------------------------------------------------------

    @property
    def paused(self) -> bool:
        return self.vscode_first.paused

    def check_editor(self, force: bool = False) -> None:
        """Look for DLNE in VS Code (every ten seconds at most): pause the worker while it is open, resume when it is gone."""
        self.vscode_first.tick(force)

    def _paused_changed(self, reason: str) -> None:
        self._log({vscode_first.EDITOR: "DLNE in VS Code is using this computer: the worker is paused.",
                   vscode_first.WORKER: "VS Code is closed but its DLNE worker still runs: this app waits for it to end.",
                   }.get(reason, "This computer is free again."))
        self.controller.set_paused(bool(reason), reason)

    def _resume_donating(self) -> bool:
        """The editor is gone and the owner was donating: do it again. True once there is nothing left to try."""
        return self.donate() or not self.describe().can_donate

    # ---- donating ----------------------------------------------------------------------------------

    def donate(self) -> bool:
        if self.paused:
            self.vscode_first.want_donating()      # started by itself (resume at launch) while VS Code is open: later
            return False
        if not self.describe().can_donate:
            return False
        settings_store.save(self.settings, os.path.join(self.home, "settings.json"))
        return self.controller.start()

    def stop(self, wait: bool = False) -> None:
        self.controller.stop(wait=wait)

    def toggle(self) -> None:
        if self.controller.status().running:
            self.stop()
        else:
            self.donate()

    def status(self) -> Status:
        return self.controller.status()

    def autostart_enabled(self) -> bool:
        return autostart.is_enabled()

    def set_autostart(self, enabled: bool) -> dict:
        return autostart.set_enabled(enabled)

    def quit(self) -> None:
        self.controller.shutdown()

    # ---- the log and the state file ----------------------------------------------------------------

    def _log(self, message: str) -> None:
        """donate.log: this app's own words and the worker's output. It stays on this computer; the app sends nothing anywhere."""
        path = paths.log_path(self.home)
        line = time.strftime("%Y-%m-%d %H:%M:%S ") + message.rstrip() + "\n"
        with self._log_lock:
            try:
                if os.path.exists(path) and os.path.getsize(path) > 1_000_000:
                    os.replace(path, path + ".1")
                with open(path, "a", encoding="utf-8", errors="replace") as handle:
                    handle.write(line)
            except OSError:
                pass

    def _write_state(self, status: Status) -> None:
        data = {
            # "version" is this app's own (the extension tells a 1.1.0+ app, which pauses for VS Code, from an older one by it).
            # While paused for VS Code the state is "paused" once the worker has gone; until then it is still "stopping" (the
            # extension waits for a worker that is leaving), and "paused": true says why in both.
            "version": __version__, "state": "paused" if status.paused and not status.running else status.state, "message": status.message, "role": status.role.role if status.role else "",
            "model": status.role.model if status.role else "", "also_cpu": status.also_cpu, "error": status.error,
            "engine_version": status.engine_version, "devices": status.devices, "pid": os.getpid(),
            "verification_failed": status.verification_failed, "clock_notice": status.clock_notice,
            "refusal": status.refusal, "activity": status.activity,
            "paused": status.paused, "engine_flavour": status.engine.flavour if status.engine else "",
        }
        path = paths.state_path(self.home)
        scratch = f"{path}.{os.getpid()}.tmp"
        try:
            with open(scratch, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            os.replace(scratch, path)
        except OSError:
            pass
