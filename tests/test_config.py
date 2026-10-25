"""
Tests for ``engine.config`` — Contract B (bugs #12 and #14).

``engine/config.py`` is now the single source of truth for the flat ``KEY=VALUE`` file that
the setup wizard, the invite files and ``builder/build_all_releases.ps1`` all generate and
consume. Three things had to be true for that to be safe, and each is asserted here:

* ``CONFIG_FILE`` must exist as an importable symbol. ``setup_cli.py`` imported it, got an
  ``ImportError`` that a bare ``except Exception: pass`` swallowed, and then silently wrote
  a ``configparser`` ``[Limits]`` section that ``get_worker_limits()`` cannot read (bug #12).
* The parser must skip ``#`` comment lines. ``main.py``'s deleted local ``load_config()``
  did not, so a commented-out setting became a key whose name started with ``#``.
* Writes must be atomic and lossless. This file holds the cluster's identity; a crash
  mid-write must not be able to truncate it, and a rewrite must not drop an operator's
  comments or any key this module has never heard of.

Every test runs against a ``tmp_path`` config tree (see the ``isolated_config`` fixture in
conftest.py) so the developer's real ``config/config.txt`` is never read or written.
"""

import os
import threading

import pytest

import engine.config as engine_config


def write_config(path, text):
    """Writes config text and returns the path, so tests read as arrange -> act -> assert."""
    path.write_text(text, encoding="utf-8")
    return path


# ======================================================================================
# The importable symbol setup_cli.py needs (bug #12)
# ======================================================================================

def test_config_file_symbol_is_importable_and_absolute():
    """
    Imported unpatched on purpose: this asserts what ``setup_cli.py`` actually gets at
    import time. An absolute path is required because DLNE is launched from a desktop
    shortcut, a service manager and a console, which have three different working
    directories.
    """
    from engine.config import CONFIG_DIR, CONFIG_FILE

    assert os.path.isabs(CONFIG_FILE)
    assert os.path.basename(CONFIG_FILE) == "config.txt"
    assert os.path.dirname(CONFIG_FILE) == CONFIG_DIR
    assert os.path.basename(CONFIG_DIR) == "config"


def test_public_surface_is_present():
    """The five names Contract B froze; other modules import them by name."""
    for symbol in ("CONFIG_FILE", "get_base_dir", "get_config",
                   "get_worker_limits", "set_config_values"):
        assert hasattr(engine_config, symbol), f"engine.config lost {symbol}"


# ======================================================================================
# Parsing
# ======================================================================================

def test_comment_lines_are_skipped(isolated_config):
    """
    A commented-out setting must not become a key.

    The old ``main.py`` parser split every line on ``=`` without checking for ``#``, so
    ``#NETWORK_KEY=old`` produced the key ``#NETWORK_KEY`` — and worse, an inline comment
    block could shadow the real value depending on ordering.
    """
    write_config(isolated_config, "\n".join([
        "# DLNE configuration",
        "#NETWORK_KEY=commented-out",
        "   # indented comment",
        "NETWORK_KEY=live-value",
        "",
        "ADMIN_HASH=hash-value",
    ]) + "\n")

    config = engine_config.get_config()

    assert config == {"NETWORK_KEY": "live-value", "ADMIN_HASH": "hash-value"}
    assert not any(key.startswith("#") for key in config)


def test_keys_are_uppercased_and_whitespace_is_stripped(isolated_config):
    """
    Operators hand-edit this file, so ``key = value`` with padding has to resolve. The old
    parser stripped only the whole line and produced a key with a trailing space, which then
    never matched any lookup.
    """
    write_config(isolated_config, "  network_key  =  spaced-value  \nMax_Ram_Gb=12\n")

    config = engine_config.get_config()

    assert config["NETWORK_KEY"] == "spaced-value"
    assert config["MAX_RAM_GB"] == "12"


def test_values_containing_equals_are_preserved(isolated_config):
    """
    Base64 and URL values contain ``=``. Only the first separator delimits the key, so the
    value must survive intact — a naive ``split("=")`` would truncate a WireGuard key.
    """
    write_config(isolated_config, "NETWORK_KEY=abc==\nUPDATE_MANIFEST_URL=https://x/y?a=b&c=d\n")

    config = engine_config.get_config()

    assert config["NETWORK_KEY"] == "abc=="
    assert config["UPDATE_MANIFEST_URL"] == "https://x/y?a=b&c=d"


def test_missing_config_file_yields_an_empty_snapshot(isolated_config):
    """First run: no file yet. Callers get defaults instead of an exception at request time."""
    assert not isolated_config.exists()
    assert engine_config.get_config() == {}


def test_lines_without_a_separator_are_ignored(isolated_config):
    """Stray text (a half-finished edit) must not create a garbage key or raise."""
    write_config(isolated_config, "this line has no separator\nNETWORK_KEY=ok\n=orphan-value\n")

    assert engine_config.get_config() == {"NETWORK_KEY": "ok"}


# ======================================================================================
# Caching
# ======================================================================================

def test_cache_invalidates_when_only_the_mtime_changes(isolated_config):
    """
    ``get_worker_limits()`` is called by per-request middleware, so the snapshot is cached on
    ``(st_mtime_ns, st_size)``. Both halves of that key must be honoured: here the two
    versions are deliberately the same byte count, so a size-only check would serve stale
    config forever after the setup wizard edited it in place.
    """
    write_config(isolated_config, "ADMIN_HASH=one\n")
    assert engine_config.get_config()["ADMIN_HASH"] == "one"

    write_config(isolated_config, "ADMIN_HASH=two\n")
    stat_result = os.stat(isolated_config)
    # Filesystem timestamp granularity is coarse enough that two writes in the same
    # millisecond can share an mtime; bump it explicitly so the test asserts the cache logic
    # rather than the host clock's resolution.
    os.utime(isolated_config, ns=(stat_result.st_atime_ns,
                                  stat_result.st_mtime_ns + 1_000_000_000))

    assert engine_config.get_config()["ADMIN_HASH"] == "two"


def test_cache_invalidates_when_the_file_appears(isolated_config):
    """
    Reading before the file exists must not poison the cache. The wizard writes config.txt
    while the engine is already running, and the very next read has to see it.
    """
    assert engine_config.get_config() == {}

    write_config(isolated_config, "NETWORK_KEY=created-later\n")

    assert engine_config.get_config()["NETWORK_KEY"] == "created-later"


def test_get_config_returns_a_copy(isolated_config):
    """
    Callers routinely keep the returned dict around and mutate it. Handing out the cached
    object itself would let one caller silently rewrite the configuration of every other.
    """
    write_config(isolated_config, "NETWORK_KEY=original\n")

    first = engine_config.get_config()
    first["NETWORK_KEY"] = "tampered"
    first["INJECTED"] = "nope"

    second = engine_config.get_config()
    assert second["NETWORK_KEY"] == "original"
    assert "INJECTED" not in second


def test_force_reload_rereads_the_file(isolated_config):
    """The explicit escape hatch for "the wizard just rewrote this behind our back"."""
    write_config(isolated_config, "NETWORK_KEY=first\n")
    assert engine_config.get_config()["NETWORK_KEY"] == "first"

    write_config(isolated_config, "NETWORK_KEY=second\n")
    assert engine_config.get_config(force_reload=True)["NETWORK_KEY"] == "second"


# ======================================================================================
# get_worker_limits
# ======================================================================================

def test_worker_limits_defaults_cover_every_key(isolated_config):
    """
    ``network_key`` and ``admin_hash`` previously had no default at all, which is bug #14's
    root cause: callers reached for a key that was simply absent unless the operator had set
    it, and the ``.get(..., "")`` band-aids hid the difference between "unset" and "empty".
    """
    limits = engine_config.get_worker_limits()

    assert limits["network_key"] == ""
    assert limits["admin_hash"] == ""
    assert limits["dependency_mirror_url"] == ""
    assert "update_manifest_url" not in limits and "discord_webhook_url" not in limits   # retired with the self-updater
    assert limits["allow_unsigned_workers"] is False
    assert limits["trust_proxy_headers"] is False
    assert limits["enforce_contrib_clause"] is True
    assert limits["enforce_model_whitelist"] is False
    assert limits["max_cpu_threads"] == -1
    assert limits["redundancy_priority_index"] == 1
    assert os.path.isabs(limits["custom_gguf_dir"])
    # Added in the same round as the right-most X-Forwarded-For read. The reader lives in
    # coordinator.py and the default belongs here; when they were written by different
    # agents the default was missed, which quietly sent a cached, typed lookup back to the
    # raw config file on every request that carried the header.
    assert limits["trusted_proxy_hops"] == 1


def test_worker_limits_covers_every_key_read_by_the_engine():
    """
    Cross-module invariant: no module may read a key this view does not define.

    ``get_worker_limits()`` is the single typed configuration surface, and every consumer
    reaches for keys by name. A key read by one module but never defaulted here yields
    ``None`` (or falls back to an unexpected file read), and because every call site uses
    ``.get`` the failure is silent — a security flag reads as off, a limit as unlimited.
    Rather than restate the key list by hand, this walks the engine's own source so a key
    added to a consumer without a default here fails immediately.
    """
    import ast

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # Only DLNE's own runtime modules. Vendored trees, the frozen backup and the build
    # scripts are explicitly out of scope for the remediation.
    skip_dirs = {"tests", "builder", "webui", "WebLander", "dist", "build", "discord",
                 "__pycache__", "DLNE Backup - No Modifications Allowed"}

    class _KeyCollector(ast.NodeVisitor):
        """Collects literal keys read from a ``get_worker_limits()`` mapping."""

        def __init__(self):
            self.keys = set()
            self.bound_names = set()      # locals assigned from get_worker_limits()

        @staticmethod
        def _is_limits_call(node):
            return (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "get_worker_limits")

        def visit_Assign(self, node):
            if self._is_limits_call(node.value):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self.bound_names.add(target.id)
            self.generic_visit(node)

        def visit_Call(self, node):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "get":
                base = func.value
                if (self._is_limits_call(base)
                        or (isinstance(base, ast.Name) and base.id in self.bound_names)):
                    if node.args and isinstance(node.args[0], ast.Constant):
                        if isinstance(node.args[0].value, str):
                            self.keys.add(node.args[0].value)
            self.generic_visit(node)

        def visit_Subscript(self, node):
            base = node.value
            if isinstance(base, ast.Name) and base.id in self.bound_names:
                if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                    self.keys.add(node.slice.value)
            self.generic_visit(node)

    defaults = set(engine_config.get_worker_limits())
    undefaulted = {}

    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            path = os.path.join(dirpath, filename)
            # config.py defines the mapping; it is not a consumer of it.
            if os.path.abspath(path) == os.path.abspath(engine_config.__file__):
                continue
            with open(path, "r", encoding="utf-8") as handle:
                source = handle.read()
            collector = _KeyCollector()
            collector.visit(ast.parse(source))
            for key in collector.keys - defaults:
                undefaulted.setdefault(key, []).append(os.path.relpath(path, repo_root))

    assert not undefaulted, (
        "keys read from get_worker_limits() with no default in engine/config.py: "
        + "; ".join(f"{key} (read by {', '.join(files)})"
                    for key, files in sorted(undefaulted.items()))
    )


@pytest.mark.parametrize(
    "raw, expected",
    [("3", 3), ("1", 1), ("16", 16),
     # Anything that is not a positive integer keeps the safe default.
     ("0", 1), ("-2", 1), ("two", 1), ("", 1), ("2.5", 1)],
)
def test_trusted_proxy_hops(isolated_config, raw, expected):
    """
    ``TRUSTED_PROXY_HOPS`` selects which ``X-Forwarded-For`` entry was written by our
    outermost trusted proxy. Proxies append, so the trustworthy entries are at the RIGHT
    end and the index counts back from there. A zero, a negative or a typo would index from
    the wrong end of the chain — which is precisely the attacker-controlled end — so
    anything that is not a positive integer must fall back to one hop.

    Parametrised rather than looped: ``get_config`` caches on the config file's
    ``(mtime_ns, size)``, so two same-length writes inside a single test can land in the
    same clock tick and the second read silently returns the first value. A fresh
    ``isolated_config`` per case removes that flake entirely.
    """
    write_config(isolated_config, f"TRUSTED_PROXY_HOPS={raw}\n")

    assert engine_config.get_worker_limits()["trusted_proxy_hops"] == expected


def test_worker_limits_reads_values_from_the_file(isolated_config):
    """The typed view has to reflect the file, with the historical types preserved."""
    write_config(isolated_config, "\n".join([
        "NETWORK_KEY=shared-secret",
        "ADMIN_HASH=$2b$12$fake",
        "MAX_RAM_GB=24.5",
        "MAX_CPU_THREADS=12",
        "MAX_VRAM_GB=8,8",
        "REDUNDANCY_PRIORITY_INDEX=3",
        "CUSTOM_GGUF_DIR=D:/models",
        "DEPENDENCY_MIRROR_URL=http://10.0.0.5/deps",
    ]) + "\n")

    limits = engine_config.get_worker_limits()

    assert limits["network_key"] == "shared-secret"
    assert limits["admin_hash"] == "$2b$12$fake"
    assert limits["max_ram_gb"] == pytest.approx(24.5)
    assert limits["max_cpu_threads"] == 12
    # Stays a string: multi-GPU splits are comma-separated.
    assert limits["max_vram_gb"] == "8,8"
    assert limits["redundancy_priority_index"] == 3
    assert limits["custom_gguf_dir"] == "D:/models"
    # Read by exactly one consumer (engine.depends). A typo in the reader would otherwise only
    # surface as "the mirror does nothing", which looks like a configuration mistake.
    assert limits["dependency_mirror_url"] == "http://10.0.0.5/deps"


@pytest.mark.parametrize(
    "raw, expected",
    [("true", True), ("True", True), ("TRUE", True), ("  true  ", True),
     ("false", False), ("yes", False), ("1", False), ("", False), ("tru", False)],
)
def test_security_flags_fail_closed(isolated_config, raw, expected):
    """
    ``ALLOW_UNSIGNED_WORKERS`` and ``TRUST_PROXY_HEADERS`` disable authentication checks, so
    anything other than the literal ``true`` — including a typo like ``yes`` — must leave
    them off. Failing open on a typo would silently unauthenticate a whole cluster.
    """
    write_config(isolated_config,
                 f"ALLOW_UNSIGNED_WORKERS={raw}\nTRUST_PROXY_HEADERS={raw}\n")

    limits = engine_config.get_worker_limits()

    assert limits["allow_unsigned_workers"] is expected
    assert limits["trust_proxy_headers"] is expected


def test_malformed_numeric_values_keep_their_default(isolated_config):
    """A typo in a number must not raise inside a request handler; the default is kept."""
    write_config(isolated_config, "MAX_RAM_GB=lots\nMAX_CPU_THREADS=all-of-them\n")

    limits = engine_config.get_worker_limits()

    assert limits["max_ram_gb"] == -1.0
    assert limits["max_cpu_threads"] == -1


# ======================================================================================
# set_config_values
# ======================================================================================

def test_set_config_values_preserves_comments_unknown_keys_and_order(isolated_config):
    """
    The wizard rewrites a couple of keys in a file operators annotate by hand and that other
    tools (invite generation, the release build script) also write to. Anything this module
    does not know about has to survive byte-for-byte, and known keys are edited in place
    rather than being moved to the end.
    """
    write_config(isolated_config, "\n".join([
        "# DLNE Coordinator configuration",
        "# Do not share this file.",
        "",
        "SOME_FUTURE_KEY=keep-me",
        "NETWORK_KEY=old-secret",
        "# ENFORCE_CONTRIB_CLAUSE=true",
        "MAX_RAM_GB=8",
    ]) + "\n")

    assert engine_config.set_config_values({"NETWORK_KEY": "new-secret",
                                            "TRUST_PROXY_HEADERS": "true"}) is True

    assert isolated_config.read_text(encoding="utf-8").splitlines() == [
        "# DLNE Coordinator configuration",
        "# Do not share this file.",
        "",
        "SOME_FUTURE_KEY=keep-me",
        "NETWORK_KEY=new-secret",
        "# ENFORCE_CONTRIB_CLAUSE=true",
        "MAX_RAM_GB=8",
        "TRUST_PROXY_HEADERS=true",
    ]


def test_set_config_values_creates_the_directory_and_file(isolated_config, dlne_root):
    """First run: the wizard writes before anything else has created ``config/``."""
    import shutil
    shutil.rmtree(dlne_root / "config")
    assert not (dlne_root / "config").exists()

    assert engine_config.set_config_values({"NETWORK_KEY": "brand-new"}) is True

    assert isolated_config.exists()
    assert engine_config.get_config()["NETWORK_KEY"] == "brand-new"


def test_set_config_values_keeps_the_format_flat(isolated_config):
    """
    Invite files, the setup wizard and ``builder/build_all_releases.ps1`` all parse this file
    as flat ``KEY=VALUE`` lines. An INI section header or JSON would break all three, which
    is exactly how bug #12 manifested.
    """
    engine_config.set_config_values({"NETWORK_KEY": "k", "MAX_RAM_GB": 16})

    for line in isolated_config.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        assert not line.strip().startswith("[")
        assert "=" in line


def test_set_config_values_normalises_keys_and_stringifies_values(isolated_config):
    """Callers pass ints and bools freely, and match keys case-insensitively."""
    write_config(isolated_config, "network_key=old\n")

    assert engine_config.set_config_values({"network_key": "new",
                                            "MAX_CPU_THREADS": 8,
                                            "trust_proxy_headers": True}) is True

    config = engine_config.get_config()
    assert config["NETWORK_KEY"] == "new"
    assert config["MAX_CPU_THREADS"] == "8"
    assert config["TRUST_PROXY_HEADERS"] == "True"


def test_set_config_values_drops_stale_duplicate_keys(isolated_config):
    """
    The parser is last-write-wins, so a duplicate further down the file would silently
    override the value we were just asked to set. Removing it is required, not cosmetic.
    """
    write_config(isolated_config, "NETWORK_KEY=first\nMAX_RAM_GB=8\nNETWORK_KEY=second\n")

    assert engine_config.set_config_values({"NETWORK_KEY": "final"}) is True

    text = isolated_config.read_text(encoding="utf-8")
    assert text.count("NETWORK_KEY=") == 1
    assert engine_config.get_config()["NETWORK_KEY"] == "final"


def test_set_config_values_invalidates_the_cache(isolated_config):
    """
    A rewrite that lands inside the same filesystem timestamp tick and happens to produce
    the same byte count would otherwise be served from the stale cache — so the writer drops
    the cache explicitly rather than trusting the stat comparison.
    """
    write_config(isolated_config, "ADMIN_HASH=aaa\n")
    assert engine_config.get_config()["ADMIN_HASH"] == "aaa"

    engine_config.set_config_values({"ADMIN_HASH": "bbb"})

    assert engine_config.get_config()["ADMIN_HASH"] == "bbb"


def test_set_config_values_leaves_no_temp_file_behind(isolated_config, dlne_root):
    """The staging file is renamed onto the target, not left as litter next to it."""
    engine_config.set_config_values({"NETWORK_KEY": "k"})

    assert not (dlne_root / "config" / "config.txt.tmp").exists()
    assert sorted(p.name for p in (dlne_root / "config").iterdir()) == ["config.txt"]


def test_set_config_values_is_atomic_when_the_rename_fails(isolated_config, monkeypatch):
    """
    The whole point of write-to-tmp-then-``os.replace``: a failure between the two must leave
    the original config completely intact. This file holds the cluster's identity, and a
    truncated one bricks every node that reads it.
    """
    original = "# keep me\nNETWORK_KEY=original\nMAX_RAM_GB=8\n"
    write_config(isolated_config, original)

    def exploding_replace(src, dst):
        raise OSError("simulated crash between write and rename")

    monkeypatch.setattr(os, "replace", exploding_replace)

    assert engine_config.set_config_values({"NETWORK_KEY": "new"}) is False

    assert isolated_config.read_text(encoding="utf-8") == original
    assert not (isolated_config.parent / "config.txt.tmp").exists()


def test_set_config_values_with_nothing_to_do_is_a_no_op(isolated_config):
    """An empty update must not create or rewrite the file."""
    assert engine_config.set_config_values({}) is True
    assert not isolated_config.exists()


# ======================================================================================
# Concurrency and Windows rename behaviour (audit items 3.1 and 3.2)
# ======================================================================================

def test_concurrent_writers_do_not_lose_updates(isolated_config):
    """
    Audit item 3.1. Every writer used the SAME ``config.txt.tmp`` path and performed an
    unsynchronised read-modify-write, so two overlapping calls interleaved their bytes in
    one shared staging file and ``os.replace`` published whichever half-merged result
    finished last. This is not hypothetical: the setup wizard, the WireGuard peer-address
    allocator and the coordinator all write config, and the wizard issues two invites back
    to back.

    Each thread here sets its own key. Losing even one of them means the merge dropped an
    update; a parse failure means the staging file was shared.
    """
    write_config(isolated_config, "# header comment\nPRESERVED_KEY=keep-me\n")

    writer_count = 12
    results: list[bool] = []
    results_lock = threading.Lock()
    start = threading.Barrier(writer_count)

    def writer(index: int) -> None:
        # The barrier maximises the overlap: without it the threads would mostly run one
        # after another and the test could pass on a broken implementation.
        start.wait()
        ok = engine_config.set_config_values({f"THREAD_KEY_{index}": f"value-{index}"})
        with results_lock:
            results.append(ok)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(writer_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive(), "a config writer deadlocked"

    assert all(results), "at least one concurrent write reported failure"

    config = engine_config.get_config(force_reload=True)
    for index in range(writer_count):
        assert config.get(f"THREAD_KEY_{index}") == f"value-{index}", (
            f"THREAD_KEY_{index} was lost by a concurrent write"
        )
    # Content the writers never touched must still be there, and the comment proves the
    # file was rewritten by the real merge path rather than clobbered wholesale.
    assert config["PRESERVED_KEY"] == "keep-me"
    assert isolated_config.read_text(encoding="utf-8").startswith("# header comment")


def test_concurrent_writers_leave_no_staging_files_behind(isolated_config, dlne_root):
    """
    The staging file is now uniquely named per call, so this also asserts that the unique
    names are all cleaned up — an implementation that leaked them would slowly fill the
    operator's ``config/`` directory with debris.
    """
    threads = [
        threading.Thread(target=engine_config.set_config_values,
                         args=({f"KEY_{i}": str(i)},))
        for i in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(p.name for p in (dlne_root / "config").iterdir()) == ["config.txt"]


def test_replace_is_retried_when_the_file_is_briefly_locked(isolated_config, monkeypatch):
    """
    Audit item 3.2. On Windows ``os.replace`` fails with WinError 5 while any other handle
    to the destination is open — an antivirus scan, an editor, a backup agent. The old
    code took that single failure as final, returned False and left the configuration
    silently unchanged. Those handles live for milliseconds, so a short retry turns the
    failure into a completed write.
    """
    write_config(isolated_config, "NETWORK_KEY=original\n")

    real_replace = os.replace
    attempts = {"count": 0}

    def flaky_replace(src, dst):
        attempts["count"] += 1
        if attempts["count"] < 3:
            # Exactly what Windows raises while another handle is open.
            raise PermissionError(5, "Access is denied")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky_replace)

    assert engine_config.set_config_values({"NETWORK_KEY": "new"}) is True
    assert attempts["count"] == 3
    assert engine_config.get_config(force_reload=True)["NETWORK_KEY"] == "new"


def test_replace_gives_up_after_the_configured_attempts(isolated_config, monkeypatch):
    """
    The retry is bounded: a genuinely permanent failure (read-only volume, a hostile ACL)
    must still report False rather than blocking startup forever, and it must not have
    touched the original file.
    """
    original = "# keep me\nNETWORK_KEY=original\n"
    write_config(isolated_config, original)

    attempts = {"count": 0}

    def always_failing_replace(src, dst):
        attempts["count"] += 1
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(os, "replace", always_failing_replace)
    # Keep the test fast: the production backoff is deliberate, the exact duration is not
    # what is under test here.
    monkeypatch.setattr(engine_config, "_REPLACE_BACKOFF_SECONDS", 0.0)

    assert engine_config.set_config_values({"NETWORK_KEY": "new"}) is False
    assert attempts["count"] == engine_config._REPLACE_ATTEMPTS
    assert isolated_config.read_text(encoding="utf-8") == original
    assert sorted(p.name for p in isolated_config.parent.iterdir()) == ["config.txt"]


# --------------------------------------------------------------------------------------
# get_bundle_dir(): shipped assets vs. writable installation state
# --------------------------------------------------------------------------------------

def test_bundle_dir_matches_base_dir_when_not_frozen():
    """Running from source, the two roots are the same tree — assets live beside the code."""
    assert engine_config.get_bundle_dir() == engine_config.get_base_dir()


def test_bundle_dir_follows_meipass_when_frozen(monkeypatch, tmp_path):
    """
    Frozen, the two roots MUST diverge.

    PyInstaller unpacks ``--add-data`` payloads into ``sys._MEIPASS`` while the executable's
    own directory holds ``config/``, ``models/`` and ``logs/``. Reading bundled assets from
    the installation root is what made the graphical wizard announce "the wizard's web assets
    are missing from this build" on every release and drop the user to the console.
    """
    install_root = tmp_path / "install"
    payload_root = tmp_path / "payload"
    install_root.mkdir()
    payload_root.mkdir()

    monkeypatch.setattr(engine_config.sys, "frozen", True, raising=False)
    monkeypatch.setattr(engine_config.sys, "executable",
                        str(install_root / "DLNE-Node"), raising=False)
    monkeypatch.setattr(engine_config.sys, "_MEIPASS", str(payload_root), raising=False)

    assert engine_config.get_base_dir() == str(install_root)
    assert engine_config.get_bundle_dir() == str(payload_root)


# --------------------------------------------------------------------------------------
# WireGuard hub settings, read by the tunnel enrollment endpoint
# --------------------------------------------------------------------------------------

def test_wireguard_hub_settings_have_usable_defaults(isolated_config):
    """
    Absent means "answer with the address the request came from", which is correct on a LAN.
    The port defaults to WireGuard's own 51820 rather than 0, so a hub that never set it
    still tells joining nodes somewhere real to dial.
    """
    write_config(isolated_config, "MODE=coordinator_only\n")

    limits = engine_config.get_worker_limits()

    assert limits["wg_wan_ip"] == ""
    assert limits["wg_listen_port"] == 51820


def test_wireguard_hub_settings_are_read_and_typed(isolated_config):
    write_config(isolated_config, "WG_WAN_IP=203.0.113.9\nWG_LISTEN_PORT=51821\n")

    limits = engine_config.get_worker_limits()

    assert limits["wg_wan_ip"] == "203.0.113.9"
    assert limits["wg_listen_port"] == 51821
    assert isinstance(limits["wg_listen_port"], int)


def test_a_malformed_listen_port_keeps_the_default(isolated_config):
    """A typo must not become port 0, which would send every joining node nowhere."""
    write_config(isolated_config, "WG_LISTEN_PORT=fifty-one-eight-twenty\n")

    assert engine_config.get_worker_limits()["wg_listen_port"] == 51820


def test_speculation_is_off_until_the_operator_measures_it(monkeypatch):
    """Drafting was measured slower on the production host; it is a per-host choice."""
    monkeypatch.setattr(engine_config, "get_config", lambda force_reload=False: {})
    limits = engine_config.get_worker_limits()

    assert limits["spec_type"] == "none"
    assert limits["spec_draft_n_max"] == 16
    assert limits["spec_draft_n_min"] == 0
    assert limits["spec_draft_p_min"] == 0.75


def test_speculation_settings_are_read_from_config(monkeypatch):
    monkeypatch.setattr(engine_config, "get_config", lambda force_reload=False: {
        "SPEC_TYPE": " Draft-Simple, ngram-mod ",
        "SPEC_DRAFT_N_MAX": "8",
        "SPEC_DRAFT_N_MIN": "2",
        "SPEC_DRAFT_P_MIN": "0.6",
    })
    limits = engine_config.get_worker_limits()

    assert limits["spec_type"] == "draft-simple,ngram-mod"
    assert limits["spec_draft_n_max"] == 8
    assert limits["spec_draft_n_min"] == 2
    assert limits["spec_draft_p_min"] == 0.6


def test_spec_type_none_wins_over_anything_listed_with_it(monkeypatch):
    monkeypatch.setattr(engine_config, "get_config",
                        lambda force_reload=False: {"SPEC_TYPE": "ngram-mod,none"})
    assert engine_config.get_worker_limits()["spec_type"] == "none"


@pytest.mark.parametrize("settings", [
    {"SPEC_TYPE": "draft-simpel"},          # llama-server would refuse to start
    {"SPEC_TYPE": "draft-simple,bogus"},    # one bad entry spoils the list
    {"SPEC_TYPE": " , "},
    {"SPEC_DRAFT_N_MAX": "0"},
    {"SPEC_DRAFT_N_MAX": "65"},
    {"SPEC_DRAFT_N_MAX": "lots"},
    {"SPEC_DRAFT_N_MIN": "-1"},
    {"SPEC_DRAFT_N_MIN": "17"},             # above the (default) maximum
    {"SPEC_DRAFT_P_MIN": "1.5"},
    {"SPEC_DRAFT_P_MIN": "-0.1"},
    {"SPEC_DRAFT_P_MIN": "high"},
])
def test_a_bad_spec_setting_keeps_the_default(monkeypatch, settings):
    """A typo in config.txt must not stop the engine from starting."""
    monkeypatch.setattr(engine_config, "get_config", lambda force_reload=False: settings)
    limits = engine_config.get_worker_limits()

    for key, value in engine_config.SPEC_DEFAULTS.items():
        assert limits[key] == value


def test_draft_device_defaults_to_auto_and_honours_an_override(monkeypatch):
    """Empty means 'pick per machine' at the point of use; DRAFT_DEVICE pins it explicitly."""
    monkeypatch.setattr(engine_config, "get_config", lambda force_reload=False: {})
    assert engine_config.get_worker_limits()["draft_device"] == ""

    monkeypatch.setattr(engine_config, "get_config", lambda force_reload=False: {"DRAFT_DEVICE": " CUDA1 "})
    assert engine_config.get_worker_limits()["draft_device"] == "CUDA1"
