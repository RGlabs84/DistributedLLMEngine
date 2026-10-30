"""
DLNE Donate updates itself (docs/release-2.7-contract.md section 4a): the manifest, the version rule, the notice, the download and its
checks, the safe swap on each platform, and the "Update automatically" setting. Nothing here touches the network (a fake site, and for
the real downloader a throwaway server on 127.0.0.1) or the real app folder: every path is a temporary one.
"""

import hashlib
import json
import os
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from dlne_donate import __version__, paths, tiers, updater, worker
from dlne_donate.tiers import Hardware
from dlne_donate.updater import Build, UpdateError, UpdateManager

HW = Hardware(gpu_name="RTX 4070", vram_gb=8, gpu_count=1, total_ram_gb=32, physical_cores=8, logical_threads=16)
APP = tiers.app_tiers_from_status(None)
OLD, NEW = b"old app " * 200_000, b"new app " * 200_000        # 1.6 MB each: over the manifest's 1 MB floor


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def manifest(version="9.9.9", linux=NEW, windows=NEW, **extra):
    data = {"versionName": version, "notes": "Faster start.", "published": "2026-11-02"}
    if linux is not None:
        data["linux"] = {"url": f"https://{updater.HOST}/downloads/DLNE-Donate-{version}-x86_64.AppImage", "sha256": sha(linux), "size": len(linux)}
    if windows is not None:
        data["windows"] = {"url": f"https://{updater.HOST}/downloads/DLNE-Donate-{version}.exe", "sha256": sha(windows), "size": len(windows)}
    data.update(extra)
    return data


class FakeSite:
    """The website: a manifest, the files it names, and a record of what was asked. ``corrupt`` changes what the download carries."""

    def __init__(self, data=None, files=None):
        self.data = data if data is not None else manifest()
        self.files = files if files is not None else {"linux": NEW, "windows": NEW}
        self.requests = []
        self.fail = None                   # an UpdateError raised by get_json
        self.corrupt = None                # "short" | "long" | "flip" | "slow"
        self.cancel_after = None

    def get_json(self, url, timeout=15.0):
        self.requests.append(url)
        if self.fail:
            raise self.fail
        return json.loads(json.dumps(self.data))

    def download(self, url, destination, expected_size, progress=None, is_cancelled=None, timeout=60.0):
        self.requests.append(url)
        data = self.files["windows" if url.endswith(".exe") else "linux"]
        if self.corrupt == "short":
            data = data[:-10]
        elif self.corrupt == "flip":
            data = data[:-1] + bytes([data[-1] ^ 1])
        elif self.corrupt == "long":
            data = data + b"extra"
        written = 0
        digest = hashlib.sha256()
        with open(destination, "wb") as out:
            for at in range(0, len(data), 400_000):
                if is_cancelled and is_cancelled():
                    raise UpdateError(updater.CANCELLED, "cancelled")
                block = data[at:at + 400_000]
                written += len(block)
                if written > expected_size:
                    raise UpdateError(updater.SIZE, f"more than the published {expected_size} bytes")
                digest.update(block)
                out.write(block)
                if progress:
                    progress(written, expected_size)
        return written, digest.hexdigest()


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


class Spawned:
    def __init__(self):
        self.calls = []

    def __call__(self, argv):
        self.calls.append(list(argv))


def synchronous(call):
    call()


def make(tmp_path, platform="linux", site=None, auto=False, current="1.1.0", with_app=True, **kwargs):
    """A manager for a copy of the app that sits at tmp_path/'apps' (the running AppImage, or the Windows exe)."""
    apps = tmp_path / "apps"
    apps.mkdir(exist_ok=True)
    app = apps / ("DLNE-Donate.exe" if platform == "windows" else "DLNE-Donate-1.1.0-x86_64.AppImage")
    app.write_bytes(OLD)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    site = site or FakeSite()
    clock, spawned, logged, changes = Clock(), Spawned(), [], []
    manager = UpdateManager(str(home), current=current, auto=lambda: auto, http=site, clock=clock, platform=platform,
                            app_path=(lambda: str(app)) if with_app else (lambda: None), spawn=spawned, log=logged.append,
                            on_change=lambda: changes.append(1), run_async=synchronous, **kwargs)
    manager.site, manager.app, manager.clock, manager.spawned, manager.logged, manager.changes = site, app, clock, spawned, logged, changes
    return manager


# ---- the manifest (the same rules as the extension's parseTrayManifest) -------------------------------------------------

def test_a_good_manifest_is_read_whole():
    release, why = updater.parse_manifest(manifest("1.2.0"))
    assert why == "" and release.version == "1.2.0" and release.notes == "Faster start." and release.published == "2026-11-02"
    linux, windows = release.builds["linux"], release.builds["windows"]
    assert linux.file == "DLNE-Donate-1.2.0-x86_64.AppImage" and windows.file == "DLNE-Donate-1.2.0.exe"
    assert linux.sha256 == sha(NEW) and linux.size == len(NEW)
    only, _ = updater.parse_manifest(manifest(windows=None))
    assert list(only.builds) == ["linux"], "one platform may be left out"
    extra, _ = updater.parse_manifest(manifest(surprise={"a": 1}))
    assert extra is not None, "unknown fields are ignored"


def bad(**changes):
    data = manifest()
    for key, value in changes.items():
        if "." in key:
            outer, inner = key.split(".")
            data[outer] = {**data[outer], inner: value}
        else:
            data[key] = value
    return data


@pytest.mark.parametrize("data", [
    [], "x", None,
    bad(versionName="v1.2"), bad(versionName="1"), bad(versionName="1.2.3.4.5"), bad(versionName=12),
    bad(notes=5), bad(notes="x" * 2001), bad(published="2026-02-30"), bad(published="tomorrow"), bad(published=20261102),
    bad(**{"linux.url": "http://dlne.rossgilsonlabs.dev/downloads/a.AppImage"}),
    bad(**{"linux.url": "https://evil.example/downloads/a.AppImage"}),
    bad(**{"linux.url": "https://dlne.rossgilsonlabs.dev.evil.example/downloads/a.AppImage"}),
    bad(**{"linux.url": "https://dlne.rossgilsonlabs.dev:8443/downloads/a.AppImage"}),
    bad(**{"linux.url": "https://user@dlne.rossgilsonlabs.dev/downloads/a.AppImage"}),
    bad(**{"linux.url": "https://dlne.rossgilsonlabs.dev/downloads/a.AppImage?x=1"}),
    bad(**{"linux.url": "https://dlne.rossgilsonlabs.dev/downloads/../a.AppImage"}),
    bad(**{"linux.url": "https://dlne.rossgilsonlabs.dev/other/a.AppImage"}),
    bad(**{"linux.url": "https://dlne.rossgilsonlabs.dev/downloads/a.exe"}),
    bad(**{"windows.url": "https://dlne.rossgilsonlabs.dev/downloads/a.AppImage"}),
    bad(**{"linux.url": "https://dlne.rossgilsonlabs.dev/downloads/a%2eAppImage"}),
    bad(**{"linux.sha256": "abc"}), bad(**{"linux.sha256": "z" * 64}), bad(**{"linux.sha256": 5}),
    bad(**{"linux.size": 999_999}), bad(**{"linux.size": 501 * 1024 * 1024}), bad(**{"linux.size": True}), bad(**{"linux.size": "5"}),
    bad(**{"linux.size": 1_600_000.5}), bad(linux="nope"),
    {"versionName": "1.2.0"},
])
def test_a_manifest_that_is_not_whole_and_official_is_refused_entirely(data):
    release, why = updater.parse_manifest(data)
    assert release is None and why


def test_the_notes_become_one_clean_line():
    release, _ = updater.parse_manifest(manifest(notes="line one\nline\ttwo\u2028three\x07" + "y" * 400))
    assert "\n" not in release.notes and "\x07" not in release.notes and "\u2028" not in release.notes and len(release.notes) == 300
    assert release.notes.startswith("line one line two three")
    quiet, _ = updater.parse_manifest({k: v for k, v in manifest().items() if k != "notes"})
    assert quiet.notes == ""


def test_the_sha256_is_kept_in_lower_case():
    data = manifest()
    data["linux"]["sha256"] = data["linux"]["sha256"].upper()
    release, _ = updater.parse_manifest(data)
    assert release.builds["linux"].sha256 == sha(NEW)


# ---- newer, same, older ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("candidate,current,newer", [
    ("1.2.0", "1.1.0", True), ("1.1.1", "1.1.0", True), ("2.0", "1.9.9", True), ("1.10.0", "1.9.0", True),
    ("1.1.0", "1.1.0", False), ("1.1", "1.1.0", False), ("1.1.0.0", "1.1", False),
    ("1.0.9", "1.1.0", False), ("0.9", "1.1.0", False), ("1.1.0", "1.1.0+cuda", False),
])
def test_only_a_newer_version_counts(candidate, current, newer):
    assert updater.is_newer(candidate, current) is newer


# ---- checking: start, every 12 hours, newer / same / older ---------------------------------------------------------------

def test_a_newer_version_is_announced_with_the_contracts_words(tmp_path):
    manager = make(tmp_path, site=FakeSite(manifest("1.2.0")))
    assert manager.view().visible is False and manager.release is None
    found = manager.check()
    assert found.version == "1.2.0"
    view = manager.view()
    assert view.text == "DLNE Donate 1.2.0 is available" and view.version == "1.2.0" and view.can_update_now
    assert (updater.UPDATE_NOW, updater.LATER) == ("Update now", "Later")
    assert manager.site.requests == [updater.MANIFEST_URL]
    assert updater.MANIFEST_URL == "https://dlne.rossgilsonlabs.dev/downloads/desktop.json"


def test_the_same_or_an_older_version_is_not_announced(tmp_path):
    for published in ("1.1.0", "1.0.5"):
        manager = make(tmp_path, site=FakeSite(manifest(published)))
        assert manager.check() is None and not manager.view().visible


def test_a_build_for_another_platform_only_is_not_an_update(tmp_path):
    assert make(tmp_path, platform="linux", site=FakeSite(manifest("1.2.0", linux=None))).check() is None
    assert make(tmp_path, platform="windows", site=FakeSite(manifest("1.2.0", windows=None))).check() is None


def test_it_checks_at_start_and_then_every_twelve_hours(tmp_path):
    manager = make(tmp_path, site=FakeSite(manifest("1.2.0")))
    manager.tick()
    assert len(manager.site.requests) == 1, "at start"
    start = manager.clock.t
    for hours in (1, 6, 11.9):
        manager.clock.t = start + hours * 3600
        manager.tick()
    assert len(manager.site.requests) == 1, "not before twelve hours"
    manager.clock.t = start + 12 * 3600
    manager.tick()
    assert len(manager.site.requests) == 2
    manager.tick()
    assert len(manager.site.requests) == 2, "once per period"
    manager.clock.t += 12 * 3600
    manager.tick()
    assert len(manager.site.requests) == 3


def test_a_failed_check_changes_nothing_and_is_tried_again_in_an_hour(tmp_path):
    manager = make(tmp_path, site=FakeSite(manifest("1.2.0")))
    manager.check()
    manager.site.fail = UpdateError(updater.NETWORK, "offline")
    manager.clock.t += 12 * 3600
    manager.tick()
    assert manager.release.version == "1.2.0", "what was known stays"
    assert any("Could not check" in line for line in manager.logged)
    assert manager.next_check == manager.clock.t + updater.RETRY_AFTER_FAILURE_S
    manager.site.fail = None
    manager.clock.t += 3600
    manager.tick()
    assert len(manager.site.requests) == 3


def test_a_refused_manifest_is_a_failed_check(tmp_path):
    manager = make(tmp_path, site=FakeSite(bad(**{"linux.url": "https://evil.example/downloads/a.AppImage"})))
    assert manager.check() is None and manager.release is None and not manager.view().visible


def test_a_disabled_manager_never_looks(tmp_path):
    manager = make(tmp_path, enabled=False)
    manager.tick()
    assert manager.site.requests == [] and not manager.due()


def test_later_hides_the_notice_until_a_still_newer_version(tmp_path):
    manager = make(tmp_path, site=FakeSite(manifest("1.2.0")))
    manager.check()
    assert manager.view().visible
    manager.later()
    assert not manager.view().visible and not manager.view().text
    manager.check()
    assert not manager.view().visible, "the same version stays dismissed"
    manager.site.data = manifest("1.3.0")
    manager.check()
    assert manager.view().version == "1.3.0", "a newer one is announced again"


def test_the_tray_is_notified_once_per_version(tmp_path):
    manager = make(tmp_path, site=FakeSite(manifest("1.2.0")))
    assert manager.to_notify() == ""
    manager.check()
    assert manager.to_notify() == "1.2.0" and manager.to_notify() == ""
    manager.site.data = manifest("1.3.0")
    manager.check()
    assert manager.to_notify() == "1.3.0"


# ---- downloading and the checks ----------------------------------------------------------------------------------------

def test_the_linux_download_is_written_beside_the_appimage_and_checked(tmp_path):
    manager = make(tmp_path, site=FakeSite(manifest("1.2.0")))
    manager.check()
    path = manager.download()
    assert path == str(manager.app) + ".new" and os.path.dirname(path) == str(manager.app.parent), "beside the running app"
    assert open(path, "rb").read() == NEW and os.access(path, os.X_OK)
    assert manager.ready == path and manager.busy == "" and manager.note == ""
    assert manager.site.requests[-1].endswith("DLNE-Donate-1.2.0-x86_64.AppImage")
    assert open(manager.app, "rb").read() == OLD, "the running app is untouched until the swap"


def test_the_windows_download_waits_in_the_app_data_folder(tmp_path):
    manager = make(tmp_path, platform="windows", site=FakeSite(manifest("1.2.0")))
    manager.check()
    path = manager.download()
    assert path == os.path.join(str(tmp_path / "home"), "update", "DLNE-Donate-1.2.0.exe")
    assert open(path, "rb").read() == NEW and open(manager.app, "rb").read() == OLD


def test_the_progress_is_reported_while_it_downloads(tmp_path):
    manager = make(tmp_path, site=FakeSite(manifest("1.2.0")))
    manager.check()
    seen = []
    manager._on_change = lambda: seen.append((manager.busy, round(manager.progress, 2)))
    manager.download()
    assert seen[0] == ("downloading", 0.0) and any(0 < p < 1 for _b, p in seen) and seen[-1][0] == ""
    assert max(p for _b, p in seen) == 1.0


@pytest.mark.parametrize("corrupt,problem", [("flip", updater.HASH), ("short", updater.SIZE), ("long", updater.SIZE)])
def test_a_download_that_is_not_the_published_file_is_deleted_and_the_old_version_stays(tmp_path, corrupt, problem):
    manager = make(tmp_path, site=FakeSite(manifest("1.2.0")))
    manager.check()
    manager.site.corrupt = corrupt
    with pytest.raises(UpdateError) as caught:
        manager.download()
    assert caught.value.problem == problem
    assert not os.path.exists(str(manager.app) + ".new"), "the bad file is gone"
    assert manager.ready is None and manager.busy == "" and ("deleted" in manager.note or "Nothing was kept" in manager.note)
    assert open(manager.app, "rb").read() == OLD and manager.spawned.calls == []
    assert manager.view().version == "1.2.0", "the notice stays: the owner can try again"


def test_a_bad_download_on_windows_leaves_nothing_in_the_staging_folder(tmp_path):
    manager = make(tmp_path, platform="windows", site=FakeSite(manifest("1.2.0")))
    manager.check()
    manager.site.corrupt = "flip"
    with pytest.raises(UpdateError):
        manager.download()
    staging = tmp_path / "home" / "update"
    assert not staging.exists() or list(staging.iterdir()) == []


def test_a_cancelled_download_keeps_nothing(tmp_path):
    manager = make(tmp_path, site=FakeSite(manifest("1.2.0")))
    manager.check()
    original = manager._http.download

    def cancelling(*args, **kwargs):
        manager.cancel()                              # the owner pressed cancel just as the download began
        return original(*args, **kwargs)

    manager._http.download = cancelling
    with pytest.raises(UpdateError) as caught:
        manager.download()
    assert caught.value.problem == updater.CANCELLED and not os.path.exists(str(manager.app) + ".new")


def test_a_folder_that_cannot_be_written_stops_the_update_before_downloading(tmp_path):
    manager = make(tmp_path, site=FakeSite(manifest("1.2.0")))
    manager.check()
    os.chmod(manager.app.parent, 0o500)
    try:
        if os.access(manager.app.parent, os.W_OK):
            pytest.skip("running as a user that can write anywhere")
        with pytest.raises(UpdateError) as caught:
            manager.download()
    finally:
        os.chmod(manager.app.parent, 0o700)
    assert caught.value.problem == updater.SAVE
    assert not any(url.endswith(".AppImage") for url in manager.site.requests), "nothing was downloaded"


def test_a_verified_download_is_not_fetched_twice(tmp_path):
    manager = make(tmp_path, site=FakeSite(manifest("1.2.0")))
    manager.check()
    first = manager.download()
    assert manager.download() == first
    assert sum(1 for url in manager.site.requests if url.endswith(".AppImage")) == 1


def test_a_build_that_cannot_replace_itself_is_not_downloaded(tmp_path):
    manager = make(tmp_path, with_app=False, site=FakeSite(manifest("1.2.0")))     # from the sources, or the Linux folder build
    manager.check()
    assert manager.view().version == "1.2.0" and manager.view().can_update_now is False
    with pytest.raises(UpdateError):
        manager.download()


# ---- installing: AppImage ----------------------------------------------------------------------------------------------

def test_the_appimage_is_swapped_under_the_same_name_and_started_again_after_this_app_exits(tmp_path):
    manager = make(tmp_path, site=FakeSite(manifest("1.2.0")))
    manager.check()
    manager.download()
    assert manager.install(pid=4321, restart_args=["--settings", "--donate"]) is True
    assert open(manager.app, "rb").read() == NEW and os.access(manager.app, os.X_OK)
    assert open(str(manager.app) + ".previous", "rb").read() == OLD, "the old version waits beside it"
    assert not os.path.exists(str(manager.app) + ".new")
    assert manager.spawned.calls == [["sh", "-c", updater.WAIT_THEN_RUN, "sh", "4321", str(manager.app), "--settings", "--donate"]]
    assert manager.ready is None


def test_the_restart_script_waits_for_the_old_process_then_runs_the_new_one(tmp_path):
    import subprocess
    import sys
    if sys.platform == "win32":
        pytest.skip("the AppImage restart is a POSIX shell script")
    out = tmp_path / "ran.txt"
    app = tmp_path / "new-app.sh"
    app.write_text(f'#!/bin/sh\necho "$@" > {out}\n')
    app.chmod(0o755)
    old = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1.5)"])
    started = subprocess.Popen(["sh", "-c", updater.WAIT_THEN_RUN, "sh", str(old.pid), str(app), "--autostart", "--donate"])
    old.wait()
    assert started.wait(timeout=10) == 0
    assert out.read_text().strip() == "--autostart --donate"


def test_an_install_re_checks_the_file_that_waited(tmp_path):
    manager = make(tmp_path, site=FakeSite(manifest("1.2.0")))
    manager.check()
    staged = manager.download()
    with open(staged, "r+b") as handle:             # damaged while it waited
        handle.seek(5)
        handle.write(b"X")
    assert manager.install(pid=1) is False
    assert not os.path.exists(staged) and open(manager.app, "rb").read() == OLD and manager.spawned.calls == []
    assert "no longer matches" in manager.note or "deleted" in manager.note


def test_a_swap_that_fails_puts_the_old_app_back(tmp_path, monkeypatch):
    manager = make(tmp_path, site=FakeSite(manifest("1.2.0")))
    manager.check()
    manager.download()
    real_replace = os.replace
    calls = []

    def replace_failing_the_second(src, dst):
        calls.append((src, dst))
        if len(calls) == 2:
            raise PermissionError("denied")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace_failing_the_second)
    assert manager.install(pid=1) is False
    monkeypatch.setattr(os, "replace", real_replace)
    assert open(manager.app, "rb").read() == OLD, "the old app is back where it was"
    assert not os.path.exists(str(manager.app) + ".previous")
    assert manager.spawned.calls == [] and manager.busy == "" and "could not be put in place" in manager.note


def test_install_without_a_download_does_nothing(tmp_path):
    manager = make(tmp_path, site=FakeSite(manifest("1.2.0")))
    manager.check()
    assert manager.install(pid=1) is False and manager.spawned.calls == [] and open(manager.app, "rb").read() == OLD


def test_swap_keeps_the_old_file_and_restores_it_on_failure(tmp_path):
    target, staged = tmp_path / "app", tmp_path / "app.new"
    target.write_bytes(b"old")
    staged.write_bytes(b"new")
    updater.swap(str(staged), str(target))
    assert target.read_bytes() == b"new" and (tmp_path / "app.previous").read_bytes() == b"old"
    gone = tmp_path / "missing.new"
    with pytest.raises(OSError):
        updater.swap(str(gone), str(target))
    assert target.read_bytes() == b"new", "a swap that cannot finish changes nothing"


# ---- installing: Windows -----------------------------------------------------------------------------------------------

def test_windows_hands_over_to_the_new_exe_and_replaces_nothing_while_it_runs(tmp_path):
    manager = make(tmp_path, platform="windows", site=FakeSite(manifest("1.2.0")))
    manager.check()
    staged = manager.download()
    assert manager.install(pid=777, restart_args=["--autostart"]) is True
    assert manager.spawned.calls == [[staged, "--finish-update", "777", str(manager.app), "--autostart"]]
    assert open(manager.app, "rb").read() == OLD, "the running exe is only replaced after it has exited"


class Steps:
    """The fakes of the Windows update step: a process that exits after some looks, a clock that moves with every sleep."""

    def __init__(self, exits_after=3):
        self.looks = 0
        self.exits_after = exits_after
        self.now = 0.0
        self.spawned = []

    def alive(self, pid):
        self.looks += 1
        return self.looks <= self.exits_after

    def sleep(self, seconds):
        self.now += seconds

    def clock(self):
        return self.now

    def spawn(self, argv):
        self.spawned.append(list(argv))


def test_the_update_step_waits_for_the_old_app_swaps_and_starts_the_new_one(tmp_path):
    app, staged = tmp_path / "DLNE-Donate.exe", tmp_path / "update" / "DLNE-Donate-1.2.0.exe"
    staged.parent.mkdir()
    app.write_bytes(b"old exe")
    staged.write_bytes(b"new exe")
    steps = Steps(exits_after=3)
    code = updater.finish_update(55, str(staged), str(app), ["--autostart", "--donate"], steps.alive, steps.sleep, steps.clock, steps.spawn)
    assert code == 0 and steps.looks == 4, "looked until the process was gone"
    assert app.read_bytes() == b"new exe" and (tmp_path / "DLNE-Donate.exe.previous").read_bytes() == b"old exe"
    assert steps.spawned == [[str(app), "--autostart", "--donate"]]
    assert staged.read_bytes() == b"new exe", "the step is the staged exe itself: it is not deleted from under itself"


def test_the_update_step_gives_up_when_the_old_app_will_not_exit(tmp_path):
    app, staged = tmp_path / "DLNE-Donate.exe", tmp_path / "new.exe"
    app.write_bytes(b"old exe")
    staged.write_bytes(b"new exe")
    steps = Steps(exits_after=10 ** 9)
    assert updater.finish_update(55, str(staged), str(app), [], steps.alive, steps.sleep, steps.clock, steps.spawn) == 3
    assert app.read_bytes() == b"old exe" and steps.spawned == [], "nothing changed, and the running app is left alone"
    assert steps.now >= updater.WAIT_FOR_EXIT_S


def test_a_swap_that_keeps_failing_starts_the_old_app_again(tmp_path):
    app, staged = tmp_path / "DLNE-Donate.exe", tmp_path / "new.exe"
    app.write_bytes(b"old exe")
    staged.write_bytes(b"new exe")
    steps = Steps(exits_after=0)

    def locked(src, dst):
        raise PermissionError("a virus scanner has it")

    code = updater.finish_update(55, str(staged), str(app), ["--autostart"], steps.alive, steps.sleep, steps.clock, steps.spawn, copy=locked)
    assert code == 4 and app.read_bytes() == b"old exe"
    assert steps.spawned == [[str(app), "--autostart"]], "the owner's tray comes back, on the old version"
    assert steps.now >= updater.SWAP_TRIES_S, "it tried for a while first"


def test_a_swap_that_works_on_the_second_try_is_a_success(tmp_path):
    app, staged = tmp_path / "DLNE-Donate.exe", tmp_path / "new.exe"
    app.write_bytes(b"old exe")
    staged.write_bytes(b"new exe")
    steps = Steps(exits_after=0)
    tries = []

    def copy(src, dst):
        tries.append(1)
        if len(tries) == 1:
            raise PermissionError("busy")
        open(dst, "wb").write(open(src, "rb").read())

    assert updater.finish_update(55, str(staged), str(app), [], steps.alive, steps.sleep, steps.clock, steps.spawn, copy=copy) == 0
    assert app.read_bytes() == b"new exe" and len(tries) == 2


def test_the_update_step_says_when_the_app_could_not_be_started(tmp_path):
    app, staged = tmp_path / "a.exe", tmp_path / "n.exe"
    app.write_bytes(b"o")
    staged.write_bytes(b"n")
    steps = Steps(exits_after=0)

    def refuse(argv):
        raise OSError("no")

    assert updater.finish_update(1, str(staged), str(app), [], steps.alive, steps.sleep, steps.clock, refuse) == 5


def test_finish_update_is_a_command_of_the_app():
    from dlne_donate.__main__ import main
    assert main(["--finish-update"]) == 2
    assert main(["--finish-update", "abc", "x"]) == 2


# ---- the environment of the restart --------------------------------------------------------------------------------------

def test_the_new_copy_does_not_inherit_what_only_made_sense_inside_the_old_one():
    env = {"HOME": "/h", "_MEIPASS2": "/tmp/_MEIold", "APPDIR": "/tmp/.mount_x", "APPIMAGE": "/old", "ARGV0": "x", "OWD": "/o",
           "LD_LIBRARY_PATH": "/tmp/_MEIold:/lib", "LD_LIBRARY_PATH_ORIG": "/usr/local/lib"}
    clean = updater.restart_environment(env)
    assert clean["HOME"] == "/h" and clean["LD_LIBRARY_PATH"] == "/usr/local/lib" and clean["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    for key in ("_MEIPASS2", "APPDIR", "APPIMAGE", "ARGV0", "OWD", "LD_LIBRARY_PATH_ORIG"):
        assert key not in clean
    assert env["APPDIR"] == "/tmp/.mount_x"


# ---- automatic -----------------------------------------------------------------------------------------------------------

def test_nothing_is_downloaded_unless_the_owner_asks_or_automatic_is_on(tmp_path):
    manager = make(tmp_path, site=FakeSite(manifest("1.2.0")), auto=False)
    manager.tick()
    manager.tick()
    assert not any(url.endswith(".AppImage") for url in manager.site.requests) and manager.ready is None
    assert manager.wants_auto_install(True) is False


def test_automatic_downloads_when_a_newer_version_is_found_and_installs_only_at_an_idle_moment(tmp_path):
    manager = make(tmp_path, site=FakeSite(manifest("1.2.0")), auto=True)
    manager.tick()                                   # the check
    assert manager.release.version == "1.2.0" and manager.ready is None
    manager.tick()                                   # the download
    assert manager.ready and os.path.exists(manager.ready)
    assert manager.wants_auto_install(idle=False) is False, "the worker is busy"
    assert manager.wants_auto_install(idle=True) is True
    assert manager.install(pid=9, restart_args=["--autostart"]) is True
    assert open(manager.app, "rb").read() == NEW


def test_automatic_off_again_stops_an_install_that_was_waiting(tmp_path):
    state = {"auto": True}
    manager = make(tmp_path, site=FakeSite(manifest("1.2.0")))
    manager._auto = lambda: state["auto"]
    manager.tick()
    manager.tick()
    assert manager.wants_auto_install(True)
    state["auto"] = False
    assert not manager.wants_auto_install(True)


def test_a_failed_automatic_download_is_not_tried_on_every_tick(tmp_path):
    manager = make(tmp_path, site=FakeSite(manifest("1.2.0")), auto=True)
    manager.tick()
    manager.site.corrupt = "flip"
    for _ in range(5):
        manager.tick()
    downloads = [url for url in manager.site.requests if url.endswith(".AppImage")]
    assert len(downloads) == 1 and "SHA-256" in manager.note
    manager.site.corrupt = None
    manager.clock.t += 12 * 3600
    manager.tick()                                   # the next check ...
    manager.tick()                                   # ... lets it try again
    assert manager.ready


def test_automatic_does_nothing_for_a_build_that_cannot_replace_itself(tmp_path):
    manager = make(tmp_path, with_app=False, site=FakeSite(manifest("1.2.0")), auto=True)
    manager.tick()
    manager.tick()
    assert manager.ready is None and not manager.wants_auto_install(True)


def test_the_automatic_setting_is_off_by_default_and_round_trips(app_home):
    from dlne_donate import settings as store
    assert tiers.default_settings(HW, APP).auto_update is False
    path = str(app_home / "settings.json")
    store.save(replace(tiers.default_settings(HW, APP), auto_update=True), path)
    assert json.load(open(path))["auto_update"] is True
    assert store.load(HW, APP, path).auto_update is True
    store.save(replace(tiers.default_settings(HW, APP), auto_update=False), path)
    assert store.load(HW, APP, path).auto_update is False
    for junk in ("yes", 1, None, [], {}):
        data = json.load(open(path))
        data["auto_update"] = junk
        json.dump(data, open(path, "w"))
        assert store.load(HW, APP, path).auto_update is False, junk
    data = json.load(open(path))
    del data["auto_update"]
    json.dump(data, open(path, "w"))
    assert store.load(HW, APP, path).auto_update is False, "an old settings file"


def test_the_model_owns_a_manager_that_follows_the_setting(app_home):
    from dlne_donate.model import Model
    model = Model(str(app_home), hardware=HW, fetch_info=False)
    assert model.updates.enabled is False and not model.updates.due(), "a model that fetches nothing looks for nothing"
    assert model.updates._auto() is False
    model.update(auto_update=True)
    assert model.updates._auto() is True
    assert json.load(open(paths.settings_path(str(app_home))))["auto_update"] is True


def test_the_real_app_never_looks_while_the_test_switch_is_set(app_home):
    from dlne_donate.model import Model
    assert os.environ[updater.OFF_ENV] == "1"
    assert Model(str(app_home), hardware=HW, fetch_info=True).updates.enabled is False


# ---- leftovers, platform, the running app --------------------------------------------------------------------------------

def test_leftovers_of_an_earlier_update_are_removed_at_start(tmp_path):
    manager = make(tmp_path)
    for name in (".new", ".previous"):
        open(str(manager.app) + name, "wb").write(b"x")
    staging = tmp_path / "home" / "update"
    staging.mkdir()
    (staging / "DLNE-Donate-1.2.0.exe").write_bytes(b"x")
    manager.cleanup()
    assert sorted(os.listdir(manager.app.parent)) == [manager.app.name] and os.listdir(staging) == []


def test_the_platform_and_the_running_app():
    assert updater.platform_key("win32", "AMD64") == "windows" and updater.platform_key("linux", "x86_64") == "linux"
    assert updater.platform_key("linux", "aarch64") is None and updater.platform_key("darwin", "x86_64") is None
    assert updater.platform_key("win32", "ARM64") is None
    assert updater.running_app("win32", True, {}, "C:\\Apps\\DLNE-Donate.exe") == "C:\\Apps\\DLNE-Donate.exe"
    assert updater.running_app("win32", False, {}, "C:\\Python\\python.exe") is None, "from the sources: no self-update"


def test_inside_an_appimage_the_running_app_is_the_appimage_file(tmp_path):
    image = tmp_path / "DLNE-Donate.AppImage"
    image.write_bytes(b"x")
    assert updater.running_app("linux", False, {"APPIMAGE": str(image)}) == str(image)
    assert updater.running_app("linux", True, {"APPIMAGE": str(tmp_path / "gone")}) is None
    assert updater.running_app("linux", True, {}) is None, "the Linux folder build cannot swap itself"


# ---- the real downloader, against a throwaway server on this computer -----------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    routes = {}

    def log_message(self, *args):
        pass

    def do_GET(self):
        route = self.routes.get(self.path)
        if route is None:
            self.send_response(404)
            self.end_headers()
            return
        code, headers, body = route
        self.send_response(code)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", headers.get("Content-Length", str(len(body))))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def server():
    Handler.routes = {}
    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def local_http():
    return updater.Http(url_ok=lambda url: url.startswith("http://127.0.0.1"))


def test_the_real_downloader_counts_and_hashes_what_arrives(server, tmp_path):
    Handler.routes["/file"] = (200, {}, NEW)
    out = tmp_path / "f"
    seen = []
    written, digest = local_http().download(server + "/file", str(out), len(NEW), lambda done, total: seen.append(done))
    assert written == len(NEW) and digest == sha(NEW) and out.read_bytes() == NEW and seen[-1] == len(NEW)


def test_the_real_downloader_stops_at_more_than_the_published_size(server, tmp_path):
    Handler.routes["/big"] = (200, {}, NEW + b"more")
    with pytest.raises(UpdateError) as caught:
        local_http().download(server + "/big", str(tmp_path / "f"), len(NEW))
    assert caught.value.problem == updater.SIZE


def test_the_real_downloader_refuses_a_server_that_announces_another_size(server, tmp_path):
    Handler.routes["/odd"] = (200, {"Content-Length": str(len(NEW))}, NEW)
    with pytest.raises(UpdateError) as caught:
        local_http().download(server + "/odd", str(tmp_path / "f"), len(NEW) + 5)
    assert caught.value.problem == updater.SIZE


def test_the_real_downloader_never_follows_a_redirect(server, tmp_path):
    Handler.routes["/moved"] = (302, {"Location": "https://evil.example/x"}, b"")
    with pytest.raises(UpdateError) as caught:
        local_http().download(server + "/moved", str(tmp_path / "f"), 10)
    assert caught.value.problem == updater.REDIRECTED
    with pytest.raises(UpdateError) as caught:
        local_http().get_json(server + "/moved")
    assert caught.value.problem == updater.REDIRECTED


def test_the_real_downloader_reports_http_errors_and_reads_json(server):
    with pytest.raises(UpdateError) as caught:
        local_http().get_json(server + "/nothing")
    assert caught.value.problem == updater.HTTP
    Handler.routes["/m.json"] = (200, {}, json.dumps(manifest("1.2.0")).encode())
    assert local_http().get_json(server + "/m.json")["versionName"] == "1.2.0"
    Handler.routes["/text"] = (200, {}, b"not json")
    with pytest.raises(UpdateError) as caught:
        local_http().get_json(server + "/text")
    assert caught.value.problem == updater.REFUSED
    Handler.routes["/huge"] = (200, {}, b" " * (updater.MAX_MANIFEST_BYTES + 10))
    with pytest.raises(UpdateError) as caught:
        local_http().get_json(server + "/huge")
    assert caught.value.problem == updater.TOO_LARGE


def test_the_real_downloader_only_talks_to_the_dlne_host_over_https(tmp_path):
    http = updater.Http()
    for url in ("http://dlne.rossgilsonlabs.dev/downloads/desktop.json", "https://evil.example/downloads/desktop.json",
                "https://dlne.rossgilsonlabs.dev:8443/downloads/desktop.json", "file:///etc/passwd", "ftp://dlne.rossgilsonlabs.dev/x"):
        with pytest.raises(UpdateError) as caught:
            http.get_json(url)
        assert caught.value.problem == updater.REFUSED, url
        with pytest.raises(UpdateError):
            http.download(url, str(tmp_path / "f"), 10)
    assert not (tmp_path / "f").exists()


def test_a_whole_update_through_the_real_downloader(server, tmp_path):
    """The manager, the real Http and a throwaway server: check, download, verify, swap."""
    data = manifest("1.2.0")
    Handler.routes["/desktop.json"] = (200, {}, json.dumps(data).encode())
    Handler.routes["/linux"] = (200, {}, NEW)
    http = local_http()
    real_get, real_download = http.get_json, http.download
    http.get_json = lambda url, timeout=15.0: real_get(server + "/desktop.json", timeout)
    http.download = lambda url, dest, size, progress=None, cancelled=None, timeout=60.0: real_download(server + "/linux", dest, size, progress, cancelled, timeout)
    manager = make(tmp_path, site=http)
    assert manager.check().version == "1.2.0"
    manager.download()
    assert manager.install(pid=1) is True and open(manager.app, "rb").read() == NEW


# ---- the app's part: stop the worker first, then swap, then quit ------------------------------------------------------------

class FakeUpdates:
    def __init__(self, ready=True, installs=True, replace_itself=True):
        self.ready = "/tmp/new" if ready else None
        self.release = updater.Release("1.2.0")
        self.installs = installs
        self.replace_itself = replace_itself
        self.install_calls = []
        self.enabled = True

    def can_replace_itself(self):
        return self.replace_itself

    def install(self, pid, args):
        self.install_calls.append((pid, list(args)))
        return self.installs


class FakeModel:
    def __init__(self, running, resume_wanted=False, **kwargs):
        self.running = running
        self.updates = FakeUpdates(**kwargs)
        self.vscode_first = type("V", (), {"resume_wanted": resume_wanted})()
        self.events = []

    def status(self):
        return worker.Status(state=worker.DONATING if self.running else worker.IDLE)

    def stop(self, wait=False):
        self.events.append("stop")

    def donate(self):
        self.events.append("donate")

    def quit(self):
        self.events.append("quit")


class FakeTray:
    def __init__(self):
        self.events = []

    def stop(self):
        self.events.append("tray-stop")


class FakeRoot:
    def __init__(self):
        self.destroyed = False

    def destroy(self):
        self.destroyed = True


def make_app(tmp_path, **kwargs):
    from dlne_donate.app import App
    app = App(home=str(tmp_path))
    app.model, app.tray, app.root = FakeModel(**kwargs), FakeTray(), FakeRoot()
    return app


def test_update_now_stops_the_worker_first_and_swaps_only_once_it_has_left(tmp_path):
    app = make_app(tmp_path, running=True)
    app._update_args = ["--settings"]
    app._update_downloaded()
    assert app.model.events == ["stop"] and app._updating and "--donate" in app._update_args, "it was donating: it comes back donating"
    assert app._continue_update() is False and app.model.updates.install_calls == [], "the worker is still leaving"
    app.model.running = False
    assert app._continue_update() is True
    assert app.model.updates.install_calls == [(os.getpid(), ["--settings", "--donate"])]
    assert app.model.events[-1] == "quit" and app.tray.events == ["tray-stop"] and app.root.destroyed and app._quitting


def test_an_idle_app_updates_without_donating_again(tmp_path):
    app = make_app(tmp_path, running=False)
    app._update_args = ["--autostart"]
    app._update_downloaded()
    assert app._continue_update() is True
    assert app.model.updates.install_calls == [(os.getpid(), ["--autostart"])]


def test_an_app_paused_for_vs_code_that_was_donating_comes_back_donating(tmp_path):
    app = make_app(tmp_path, running=False, resume_wanted=True)
    app._update_args = ["--autostart"]
    app._update_downloaded()
    assert app._continue_update() is True
    assert app.model.updates.install_calls[0][1] == ["--autostart", "--donate"]


def test_a_failed_install_keeps_the_old_version_and_donates_again(tmp_path):
    app = make_app(tmp_path, running=True, installs=False)
    app._update_args = ["--settings"]
    app._update_downloaded()
    app.model.running = False
    assert app._continue_update() is False
    assert not app._updating and not app._quitting and app.model.events == ["stop", "donate"]
    assert not app.root.destroyed and app.tray.events == []


def test_a_worker_that_will_not_leave_does_not_hold_the_update_forever(tmp_path, monkeypatch):
    from dlne_donate import app as app_module
    app = make_app(tmp_path, running=True)
    app._update_args = []
    app._update_downloaded()
    clock = {"t": app._update_started}
    monkeypatch.setattr(app_module.time, "monotonic", lambda: clock["t"])
    assert app._continue_update() is False
    clock["t"] += app_module.QUIT_WAIT_S + 1
    assert app._continue_update() is True


def test_quitting_wins_over_an_update_that_was_waiting(tmp_path):
    app = make_app(tmp_path, running=True)
    app._update_args = []
    app._update_downloaded()
    app._begin_quit()
    assert app._quitting and not app._updating
    app.model.running = False
    assert app._poll_updates() is False and app.model.updates.install_calls == []


def test_update_now_on_a_build_that_cannot_replace_itself_opens_the_download_page(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr("dlne_donate.app.webbrowser.open", opened.append)
    app = make_app(tmp_path, running=False, replace_itself=False)
    app._update_now()
    assert opened == [updater.PAGE_URL] and not app._updating and not app._downloading_update


def test_the_tray_entry_and_the_notification_follow_the_manager(tmp_path):
    from dlne_donate import trayicon

    class Notified(trayicon.Tray):
        pass

    tray = trayicon.Tray(lambda: worker.Status(), lambda: False, {})
    tray.notify("DLNE Donate 1.2.0 is available")       # no icon yet: nothing to show, nothing to crash
    assert tray._update_label() == ""
    shown = []
    tray.icon = type("I", (), {"notify": staticmethod(lambda message, title: shown.append((title, message)))})()
    tray.notify("DLNE Donate 1.2.0 is available")
    assert shown == [("DLNE Donate", "DLNE Donate 1.2.0 is available")]
    app = make_app(tmp_path, running=False)
    app.model.updates = make(tmp_path, site=FakeSite(manifest("1.2.0")))
    assert app._update_label() == ""
    app.model.updates.check()
    assert app._update_label() == "Update DLNE Donate to 1.2.0"
