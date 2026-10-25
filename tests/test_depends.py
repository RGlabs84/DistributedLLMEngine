"""
Tests for ``engine.depends`` — the runtime dependency preflight.

WHAT IS BEING PROTECTED
    Two things, and neither is "does the install work" (that needs a clean VM):

    1. **Consent.** This module downloads and executes third-party installers. Nothing may
       run without an explicit yes, and the dependencies that must never be auto-installed
       (GPU drivers) must stay uninstallable no matter what a caller passes.
    2. **Applicability.** The wrong dependency offered is worse than none: asking a LAN-only
       worker to install a VPN, or a CUDA node to install a Vulkan loader it does not use,
       is the noise that makes people skip the page that mattered.

    The probes themselves are mocked throughout. A test that asserted this machine has
    WebView2 would pass on the developer's laptop and fail in CI for reasons unrelated to
    the code.
"""

import pytest

from engine import depends


@pytest.fixture(autouse=True)
def mirror(monkeypatch):
    """
    Drive ``DEPENDENCY_MIRROR_URL`` from a stub of the config reader, never of
    :func:`depends.mirror_base_url` itself.

    Stubbing the function under test is how a config knob ends up "tested" while wired to
    nothing — which is exactly the state ``dependency_mirror_url`` was in before this module
    existed. Every test therefore runs the real reader; this fixture only decides what the
    config says. Default is "no mirror configured", i.e. the vendor URLs.

    Returns a setter so a test can opt into a mirror::

        def test_something(mirror):
            mirror("http://10.0.0.5/deps")
    """
    def _configure(value=""):
        monkeypatch.setattr(depends, "get_worker_limits",
                            lambda: {"dependency_mirror_url": value})

    _configure("")
    return _configure


@pytest.fixture
def real_mirror_reader(mirror):
    """Alias kept for readability at the mirror-specific tests' call sites."""
    return mirror


# ======================================================================================
# Applicability matrix
# ======================================================================================

def _ids(ctx):
    return {entry["id"] for entry in depends.DEPENDENCIES if entry["applies"](ctx)}


def test_windows_vulkan_worker_is_offered_the_vulkan_runtime_not_a_driver_check():
    ctx = {"system": "Windows", "variant": "windows-vulkan", "needs_gui": False, "needs_wan": False}

    applicable = _ids(ctx)

    assert "vulkan_runtime" in applicable
    assert "vcredist" in applicable
    assert "nvidia_driver" not in applicable
    assert "wireguard" not in applicable


def test_a_cuda_build_checks_the_driver_floor_and_skips_vulkan():
    """The CUDA binaries do not load a Vulkan ICD, so offering one is pure noise."""
    ctx = {"system": "Windows", "variant": "windows-cuda-13", "needs_gui": False, "needs_wan": False}

    applicable = _ids(ctx)

    assert "nvidia_driver" in applicable
    assert "vulkan_runtime" not in applicable


def test_linux_gets_the_loader_package_not_the_windows_runtime():
    ctx = {"system": "Linux", "variant": "linux-vulkan", "needs_gui": False, "needs_wan": False}

    applicable = _ids(ctx)

    assert applicable == {"vulkan_loader"}


def test_the_web_view_host_is_only_checked_when_a_gui_is_wanted():
    """A headless setup run must not be told to install a browser engine it will not open."""
    windows = {"system": "Windows", "variant": "windows-vulkan", "needs_gui": False, "needs_wan": False}
    linux = {"system": "Linux", "variant": "linux-vulkan", "needs_gui": False, "needs_wan": False}

    assert "webview2" not in _ids(windows)
    assert "webkit2gtk" not in _ids(linux)

    windows["needs_gui"] = True
    linux["needs_gui"] = True
    assert "webview2" in _ids(windows)
    assert "webkit2gtk" in _ids(linux)


def test_wireguard_is_only_offered_to_nodes_that_route_over_the_internet():
    for system in ("Windows", "Linux", "Darwin"):
        ctx = {"system": system, "variant": "", "needs_gui": False, "needs_wan": True}
        assert "wireguard" in _ids(ctx)
        ctx["needs_wan"] = False
        assert "wireguard" not in _ids(ctx)


# ======================================================================================
# CUDA driver floors
# ======================================================================================

def test_each_cuda_variant_maps_to_its_own_floor():
    assert depends.cuda_floor_for_variant("windows-cuda-11.8") == depends.CUDA_DRIVER_FLOORS["11.8"]
    assert depends.cuda_floor_for_variant("windows-cuda-13") == depends.CUDA_DRIVER_FLOORS["13"]
    assert depends.cuda_floor_for_variant("windows-vulkan") is None
    assert depends.cuda_floor_for_variant("") is None


def test_a_driver_below_the_floor_is_action_required_never_installable(monkeypatch):
    """
    ITEM: DLNE must never push a GPU driver.

    An unattended driver install can black-screen a machine, and the user has a free
    alternative — the Vulkan build, which has no floor. So the check reports and recommends
    rather than offering a button.
    """
    monkeypatch.setattr(depends, "_nvidia_driver_version", lambda: 470.5)

    outcome = depends.check_nvidia_driver("windows-cuda-13")

    assert outcome["status"] == depends.STATUS_ACTION_REQUIRED
    assert "Vulkan" in outcome["detail"]

    entry = next(item for item in depends.DEPENDENCIES if item["id"] == "nvidia_driver")
    assert entry["install"] is None


def test_a_driver_above_the_floor_passes(monkeypatch):
    monkeypatch.setattr(depends, "_nvidia_driver_version", lambda: 581.0)

    assert depends.check_nvidia_driver("windows-cuda-13")["status"] == depends.STATUS_OK


def test_a_missing_nvidia_driver_on_a_cuda_build_is_action_required(monkeypatch):
    monkeypatch.setattr(depends, "_nvidia_driver_version", lambda: None)

    outcome = depends.check_nvidia_driver("windows-cuda-11.8")

    assert outcome["status"] == depends.STATUS_ACTION_REQUIRED


def test_a_non_cuda_build_never_fails_the_driver_check(monkeypatch):
    monkeypatch.setattr(depends, "_nvidia_driver_version", lambda: None)

    assert depends.check_nvidia_driver("windows-vulkan")["status"] == depends.STATUS_OK


# ======================================================================================
# DEPENDENCY_MIRROR_URL
# ======================================================================================

def test_no_mirror_means_the_vendor_url_verbatim():
    assert depends.resolve_download_url(depends.WEBVIEW2_BOOTSTRAP_URL,
                                        depends.WEBVIEW2_BOOTSTRAP_FILENAME) == depends.WEBVIEW2_BOOTSTRAP_URL


def test_a_mirror_rewrites_by_filename_not_by_vendor_path(real_mirror_reader):
    """
    Microsoft's URL is an opaque redirector (``fwlink/p/?LinkId=2124703``) with no filename
    in it at all, so the mirror layout has to be driven by the declared filename. That is
    also what makes the mirror documentable: host these five names under one base URL.
    """
    real_mirror_reader("http://10.0.0.5/deps")

    assert depends.resolve_download_url(depends.WEBVIEW2_BOOTSTRAP_URL,
                                        depends.WEBVIEW2_BOOTSTRAP_FILENAME) == \
        "http://10.0.0.5/deps/MicrosoftEdgeWebview2Setup.exe"
    assert depends.resolve_download_url(depends.VULKAN_RUNTIME_URL,
                                        depends.VULKAN_RUNTIME_FILENAME) == \
        "http://10.0.0.5/deps/vulkan-runtime.exe"


def test_a_trailing_slash_in_the_configured_mirror_does_not_double_up(real_mirror_reader):
    """
    Operators type trailing slashes. Both the config reader and the URL builder normalise,
    because either one could be the layer a future caller reaches first.
    """
    real_mirror_reader("http://10.0.0.5/deps/")

    assert depends.resolve_download_url(depends.VULKAN_RUNTIME_URL, depends.VULKAN_RUNTIME_FILENAME) == \
        "http://10.0.0.5/deps/vulkan-runtime.exe"


def test_an_unreadable_config_does_not_break_the_probe(monkeypatch, real_mirror_reader):
    """A broken config.txt must not stop a wizard from telling the user what is missing."""
    def _explode():
        raise OSError("config is on fire")

    real_mirror_reader("")
    monkeypatch.setattr(depends, "get_worker_limits", _explode)

    assert depends.mirror_base_url() == ""


def test_a_configured_mirror_is_read_through_the_worker_limits(real_mirror_reader):
    """
    ``dependency_mirror_url`` existed, was typed, was tested — and was read by nothing at
    all until this module. This pins that it is genuinely wired to the config key.
    """
    real_mirror_reader("http://mirror.lan/dlne")

    assert depends.mirror_base_url() == "http://mirror.lan/dlne"


# ======================================================================================
# Consent
# ======================================================================================

def test_install_refuses_without_consent(monkeypatch):
    """
    ``consent`` defaults to False and is a required act, not a formality: the caller passing
    True is asserting a human read the dependency's ``why`` text and clicked or typed yes.
    """
    called = []
    monkeypatch.setitem(_entry("vcredist"), "install", lambda consent: called.append(consent))

    outcome = depends.install("vcredist", {"system": "Windows"}, consent=False)

    assert outcome["status"] == depends.STATUS_ACTION_REQUIRED
    assert not called


def test_install_runs_and_re_probes_when_consent_is_given(monkeypatch):
    """
    A silent installer can exit 0 having installed nothing usable (wrong architecture,
    blocked by policy). The wizard must show what is true *now*, so the check is re-run
    rather than the exit code trusted.
    """
    probes = iter([
        {"status": depends.STATUS_OK, "detail": "installed"},
    ])
    monkeypatch.setitem(_entry("vcredist"), "install",
                        lambda consent: {"status": depends.STATUS_OK, "detail": "Installed."})
    monkeypatch.setitem(_entry("vcredist"), "check", lambda ctx: next(probes))

    outcome = depends.install("vcredist", {"system": "Windows"}, consent=True)

    assert outcome["status"] == depends.STATUS_OK


def test_an_installer_that_succeeds_but_changes_nothing_is_reported_honestly(monkeypatch):
    monkeypatch.setitem(_entry("vcredist"), "install",
                        lambda consent: {"status": depends.STATUS_OK, "detail": "Installed."})
    monkeypatch.setitem(_entry("vcredist"), "check",
                        lambda ctx: {"status": depends.STATUS_MISSING, "detail": "still absent"})

    outcome = depends.install("vcredist", {"system": "Windows"}, consent=True)

    assert outcome["status"] != depends.STATUS_OK
    assert "still not detected" in outcome["detail"]


def test_an_unknown_dependency_id_is_rejected():
    outcome = depends.install("definitely-not-a-dependency", {}, consent=True)

    assert outcome["status"] == depends.STATUS_UNKNOWN


def test_console_flow_declines_without_a_terminal(monkeypatch):
    """
    A service or a piped launch has nobody to answer the prompt. It must return False
    immediately rather than block forever on stdin that will never produce a line.
    """
    monkeypatch.setitem(_entry("vcredist"), "check",
                        lambda ctx: {"status": depends.STATUS_MISSING, "detail": "absent"})

    class _NoTty:
        def isatty(self):
            return False

    monkeypatch.setattr(depends.sys, "stdin", _NoTty())

    assert depends.console_install_flow("vcredist", {"system": "Windows"}) is False


def test_console_flow_short_circuits_when_the_dependency_is_already_present(monkeypatch):
    monkeypatch.setitem(_entry("vcredist"), "check",
                        lambda ctx: {"status": depends.STATUS_OK, "detail": "present"})

    assert depends.console_install_flow("vcredist", {"system": "Windows"}) is True


def test_console_flow_installs_on_yes_and_skips_on_no(monkeypatch):
    attempts = []
    monkeypatch.setitem(_entry("vcredist"), "check",
                        lambda ctx: {"status": depends.STATUS_MISSING, "detail": "absent"})
    monkeypatch.setattr(depends, "install",
                        lambda dep, ctx=None, consent=False: attempts.append((dep, consent))
                        or {"status": depends.STATUS_OK, "detail": ""})

    class _Tty:
        def isatty(self):
            return True

    monkeypatch.setattr(depends.sys, "stdin", _Tty())

    assert depends.console_install_flow("vcredist", {"system": "Windows"}, prompt=lambda _: "n") is False
    assert not attempts

    assert depends.console_install_flow("vcredist", {"system": "Windows"}, prompt=lambda _: "") is True
    assert attempts == [("vcredist", True)]


# ======================================================================================
# Package managers
# ======================================================================================

def test_pamac_is_preferred_over_pacman(monkeypatch):
    """
    A Manjaro box has both, and pamac is the one that resolves that distribution's repo
    state the way its users expect. Order in the table is the behaviour, so it is pinned.
    """
    monkeypatch.setattr(depends.shutil, "which",
                        lambda name: f"/usr/bin/{name}" if name in ("pamac", "pacman") else None)

    assert depends.detect_package_manager()["id"] == "pamac"


def test_no_supported_package_manager_is_action_required_not_a_crash(monkeypatch):
    monkeypatch.setattr(depends.shutil, "which", lambda name: None)

    outcome = depends._install_packages({"apt": "libvulkan1"})

    assert outcome["status"] == depends.STATUS_ACTION_REQUIRED
    assert "package manager" in outcome["detail"]


def test_a_distribution_with_no_known_package_name_is_not_guessed_at(monkeypatch):
    monkeypatch.setattr(depends.shutil, "which",
                        lambda name: "/usr/bin/dnf" if name == "dnf" else None)

    outcome = depends._install_packages({"apt": "libvulkan1"})

    assert outcome["status"] == depends.STATUS_ACTION_REQUIRED


# ======================================================================================
# Report shape
# ======================================================================================

def test_a_probe_that_raises_reports_unknown_rather_than_missing(monkeypatch):
    """
    "Unknown" keeps the wizard usable and honest. "Missing" would offer an install for
    something that may well be present, and a failed install is where users give up.
    """
    def _explode(ctx):
        raise RuntimeError("probe exploded")

    monkeypatch.setitem(_entry("vcredist"), "check", _explode)

    report = depends.preflight({"system": "Windows", "variant": "windows-vulkan",
                                "needs_gui": False, "needs_wan": False, "mirror": ""})
    entry = next(item for item in report if item["id"] == "vcredist")

    assert entry["status"] == depends.STATUS_UNKNOWN
    assert entry["can_install"] is False


def test_the_summary_separates_installable_from_blocking():
    report = [
        {"id": "a", "status": depends.STATUS_OK},
        {"id": "b", "status": depends.STATUS_MISSING},
        {"id": "c", "status": depends.STATUS_ACTION_REQUIRED},
        {"id": "d", "status": depends.STATUS_UNKNOWN},
    ]

    summary = depends.summarize(report)

    assert summary == {"total": 4, "ok": 1, "missing": 1, "blocking": 1, "all_clear": False}


def test_every_registry_entry_carries_the_fields_both_wizards_render():
    """The console table and the GUI's System Check page read the same keys."""
    for entry in depends.DEPENDENCIES:
        assert entry["id"] and entry["label"] and entry["why"]
        assert callable(entry["applies"])
        assert callable(entry["check"])
        assert entry["install"] is None or callable(entry["install"])


def _entry(dependency_id):
    return next(item for item in depends.DEPENDENCIES if item["id"] == dependency_id)


# ======================================================================================
# Variant resolution (moved here from the retired self-updater, behaviour unchanged)
# ======================================================================================

def test_release_info_is_authoritative_over_hardware(tmp_path, monkeypatch):
    """
    A user who deliberately installed the Vulkan build on an NVIDIA card keeps Vulkan checks.
    Re-detecting hardware every time would silently switch them to CUDA.
    """
    import json

    (tmp_path / depends.RELEASE_INFO_FILE).write_text(
        json.dumps({"variant": "windows-vulkan", "version": "1.0.0"}), encoding="utf-8")
    monkeypatch.setattr("engine.hardware.get_gpu_type", lambda: "nvidia-12")

    assert depends.resolve_variant(str(tmp_path)) == "windows-vulkan"


def test_an_unknown_variant_falls_back_to_detection(tmp_path, monkeypatch):
    import json

    (tmp_path / depends.RELEASE_INFO_FILE).write_text(json.dumps({"variant": "windows-quantum"}), encoding="utf-8")
    monkeypatch.setattr(depends.platform, "system", lambda: "Linux")

    assert depends.resolve_variant(str(tmp_path)) == "linux-vulkan"


def test_a_missing_release_info_falls_back_to_detection(tmp_path, monkeypatch):
    monkeypatch.setattr(depends.platform, "system", lambda: "Darwin")

    assert depends.resolve_variant(str(tmp_path)) == "mac-applesilicon"


def test_a_corrupt_release_info_falls_back_to_detection(tmp_path, monkeypatch):
    (tmp_path / depends.RELEASE_INFO_FILE).write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(depends.platform, "system", lambda: "Linux")

    assert depends.resolve_variant(str(tmp_path)) == "linux-vulkan"


def test_every_resolvable_variant_is_a_known_one(tmp_path, monkeypatch):
    for system, gpu, expected in (
        ("Windows", "nvidia-12", "windows-cuda-13"),
        ("Windows", "nvidia-11", "windows-cuda-11.8"),
        ("Windows", "amd", "windows-vulkan"),
        ("Linux", "amd", "linux-vulkan"),
        ("Darwin", "apple", "mac-applesilicon"),
    ):
        monkeypatch.setattr(depends.platform, "system", lambda s=system: s)
        monkeypatch.setattr("engine.hardware.get_gpu_type", lambda g=gpu: g)
        resolved = depends.resolve_variant(str(tmp_path))
        assert resolved == expected
        assert resolved in depends.KNOWN_VARIANTS


def test_build_context_resolves_the_variant_itself(monkeypatch):
    monkeypatch.setattr(depends, "resolve_variant", lambda: "linux-vulkan")
    assert depends.build_context()["variant"] == "linux-vulkan"
    assert depends.build_context(variant="windows-vulkan")["variant"] == "windows-vulkan"
