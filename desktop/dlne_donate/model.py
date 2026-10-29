"""The app's state in one place (settings, the machine, what the swarm can use, the worker), shared by the window and the tray."""

import json
import os
import threading
import time
from typing import Any, Callable, List, Optional

from . import COORDINATOR_URL, PUBLIC_NETWORK_KEY, autostart, bundled, netstatus, paths, settings as settings_store, tiers
from .worker import Status, WorkerController


class Model:
    def __init__(self, home: str = "", hardware: Optional[tiers.Hardware] = None, base_url: str = COORDINATOR_URL,
                 select: Any = None, controller_factory: Optional[Callable[..., WorkerController]] = None,
                 fetch_info: bool = True):
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

    # ---- donating ----------------------------------------------------------------------------------

    def donate(self) -> bool:
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
            "state": status.state, "message": status.message, "role": status.role.role if status.role else "",
            "model": status.role.model if status.role else "", "also_cpu": status.also_cpu, "error": status.error,
            "engine_version": status.engine_version, "devices": status.devices, "pid": os.getpid(),
            "verification_failed": status.verification_failed, "clock_notice": status.clock_notice,
            "refusal": status.refusal, "activity": status.activity,
        }
        path = paths.state_path(self.home)
        scratch = f"{path}.{os.getpid()}.tmp"
        try:
            with open(scratch, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            os.replace(scratch, path)
        except OSError:
            pass
