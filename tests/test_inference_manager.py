"""
Tests for ``engine.inference_manager.InferenceManager`` — Contract C section 4b
(bugs #5, #6, #7, #13, #20, #31).

Nothing here starts a real ``llama-server``: ``subprocess.Popen`` is replaced by
:class:`FakeProcess`, the llama.cpp binary lookup is stubbed, and the port-bind probe is
redirected at a simulated port space that :class:`FakeProcess` occupies and releases exactly
as a real child process would. That makes the suite runnable with no GPU, no network and no
binaries unpacked, while still exercising the real allocation, admission and teardown logic.

THE HEADLINE TEST is :func:`test_port_allocator_never_reissues_a_live_port`, which reproduces
bug #5 verbatim: model A takes 8081, model B takes 8082, A is stopped, and the next model
must not be handed 8082 while B is still listening on it. The old
``base_llama_port + len(self.running_models)`` arithmetic did exactly that, and the
coordinator then proxied that model's traffic straight into B's engine.
"""

import atexit
import os
import struct
import subprocess
import threading
import time

import pytest

import engine.inference_manager as inference_manager
from engine.inference_manager import InferenceManager

# What a token of KV cache costs throughout this file, in bytes. 64 KB — cheap, round, and
# deliberately decoupled from anything production ships, so that re-pricing a model
# architecture cannot quietly invert an admit/refuse assertion here. See the engine_env
# fixture for why it is pinned rather than measured.
TEST_KV_BYTES_PER_TOKEN = 64 * 1024
TEST_KV_GB_PER_TOKEN = TEST_KV_BYTES_PER_TOKEN / (1024 ** 3)


# ======================================================================================
# Test doubles
# ======================================================================================

def _port_from_command(cmd):
    """Extracts the ``--port`` value from a llama-server argv, as a real bind would see it."""
    argv = [str(part) for part in cmd]
    return int(argv[argv.index("--port") + 1])


class FakeProcess:
    """
    Stand-in for ``subprocess.Popen`` that models the one behaviour the manager depends on:
    a live child *holds its TCP port* until it actually exits.

    The port space is shared with the patched ``_is_port_bindable`` probe, so an allocator
    that hands out a port belonging to a running model is caught by the probe as well as by
    the bookkeeping — which is what a real machine would do.
    """

    def __init__(self, cmd, port_space, terminate_hangs=False, survives_kill=False,
                 wait_gate=None):
        self.cmd = [str(part) for part in cmd]
        self.port = _port_from_command(cmd)
        self.returncode = None
        self.terminate_calls = 0
        self.kill_calls = 0

        self._port_space = port_space
        # When True the process ignores terminate(), which is what forces the manager down
        # the kill() escalation path (bug #31).
        self._terminate_hangs = terminate_hangs
        # When True it ignores kill() as well — an engine wedged in an uninterruptible
        # driver call. It is still holding its VRAM and its port, which is what the zombie
        # quarantine exists for (finding 2.5).
        self._survives_kill = survives_kill
        # A real Popen.wait() blocks for the whole timeout before raising. When this gate is
        # armed, wait() parks in it instead, which lets a test hold a teardown inside its
        # slow phase and prove the state lock is not being held there (finding R3-10).
        self._wait_gate = wait_gate

        port_space.add(self.port)

    def poll(self):
        """None while running, the exit code once it has exited — same contract as Popen."""
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1
        if self._terminate_hangs:
            return
        self._exit(0)

    def kill(self):
        self.kill_calls += 1
        if self._survives_kill:
            return
        self._exit(-9)

    def wait(self, timeout=None):
        """Raises TimeoutExpired while the process is still up, exactly as Popen does."""
        if self.returncode is None:
            # Real waits burn the whole timeout before giving up; the gate stands in for
            # that so the cost is controlled by the test rather than by a sleep.
            if self._wait_gate is not None:
                self._wait_gate.pass_through()
            raise subprocess.TimeoutExpired(cmd=self.cmd, timeout=timeout)
        return self.returncode

    def _exit(self, code):
        self.returncode = code
        self._port_space.discard(self.port)


class Gate:
    """
    A rendezvous that holds ``start_model`` threads at a chosen point in its slow phase.

    Concurrency regressions that rely on ``time.sleep`` and hope pass on the developer's
    laptop and stop testing anything on CI. Instead the fixture calls :meth:`pass_through`
    from inside the patched engine-download and the patched ``Popen``; a test arms the gate,
    waits until a known number of threads have provably arrived (:meth:`wait_for_arrivals`),
    performs whatever racing operation it wants to prove is safe, and only then releases
    them. Every race in this file is therefore deterministic, not timing-dependent.
    """

    def __init__(self):
        self._condition = threading.Condition()
        self._proceed = threading.Event()
        self.arrivals = 0
        self.armed = False

    def arm(self):
        """Enables the gate. Un-armed gates are a no-op so unrelated tests are unaffected."""
        self.armed = True
        return self

    def pass_through(self, timeout=15.0):
        """Called from the code under test: announce arrival, then block until released."""
        if not self.armed:
            return
        with self._condition:
            self.arrivals += 1
            self._condition.notify_all()
        self._proceed.wait(timeout=timeout)

    def wait_for_arrivals(self, count, timeout=15.0):
        """Blocks until ``count`` threads are provably parked inside the gate."""
        with self._condition:
            arrived = self._condition.wait_for(lambda: self.arrivals >= count, timeout=timeout)
        assert arrived, f"only {self.arrivals} of {count} threads reached the gate"

    def release(self):
        """Lets every parked thread — and every later arrival — continue."""
        self._proceed.set()


def wait_until(predicate, timeout=15.0, message="condition never became true"):
    """Polls ``predicate`` until it holds. Used only for states no Event can signal."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.002)
    raise AssertionError(message)


def join_all(threads, timeout=20.0):
    """
    Joins every thread and fails loudly if any is still alive.

    This is the deadlock detector for the whole concurrency section: a lock-ordering mistake
    between ``_state_lock`` and ``_port_lock``, or a thread parked on another thread's event
    while holding the state lock, shows up here as a thread that never finishes rather than
    as a hung test run.
    """
    for thread in threads:
        thread.join(timeout=timeout)
    alive = [thread.name for thread in threads if thread.is_alive()]
    assert not alive, f"threads did not finish (deadlock?): {alive}"


class EngineEnv:
    """Bundle of the manager under test plus the simulated machine it runs on."""

    def __init__(self, manager, models_dir, bound_ports, spawned):
        self.manager = manager
        self.models_dir = models_dir
        self.bound_ports = bound_ports
        self.spawned = spawned
        # Flipped by a test before start_model() to make the next child ignore terminate().
        self.terminate_hangs = False
        # ... and this one to make it ignore kill() too.
        self.survives_kill = False
        # Holds a start after admission but before Popen (the window findings 2.1/2.2 are
        # about), and after Popen but before the entry is published (finding 2.3).
        self.download_gate = Gate()
        self.popen_gate = Gate()
        # Holds a teardown inside process.wait(), which is where the state lock used to be
        # pinned for up to four seconds per engine (finding R3-10).
        self.wait_gate = Gate()

    def add_model(self, tag, size_bytes=1024):
        """
        Drops a placeholder ``.gguf`` into the models root so ``resolve_model_path`` finds it.

        The file is deliberately tiny; tests that care about memory patch
        ``estimate_model_vram_gb`` instead of writing multi-gigabyte fixtures.
        """
        path = self.models_dir / f"{tag}.gguf"
        path.write_bytes(b"\0" * size_bytes)
        return path


@pytest.fixture
def engine_env(isolated_config, dlne_root, make_worker_limits, monkeypatch):
    """
    An ``InferenceManager`` wired to a temporary install root and a simulated port space.

    ``get_base_dir`` and ``get_worker_limits`` are patched in ``engine.inference_manager``'s
    own namespace rather than in ``engine.config``: the module did ``from engine.config
    import get_base_dir, get_worker_limits`` at import time, which copied the function
    objects into its globals, so patching the source module alone would have no effect.
    """
    models_dir = dlne_root / "models"
    bound_ports = set()
    spawned = []

    # A test sets ``engine_env.limits["engine_slots"] = 2`` (or any other worker limit) to
    # play the operator; the default is what production ships, ENGINE_SLOTS and
    # ENGINE_CONTEXT automatic.
    limits_overrides = {}
    monkeypatch.setattr(inference_manager, "get_base_dir", lambda: str(dlne_root))
    monkeypatch.setattr(inference_manager, "get_worker_limits",
                        lambda: make_worker_limits(**limits_overrides))

    # Pin what a token of KV cache costs. Real sizing reads it out of the model's GGUF
    # header, and the models here are empty stub files, so every test would otherwise be
    # priced at the pessimistic fallback — which means changing that fallback silently
    # re-tunes every pool-arithmetic assertion in this file. The tests below are about
    # admission and double-counting, not about what a particular architecture costs, so
    # they get one fixed price and state it.
    monkeypatch.setattr(inference_manager.gguf, "kv_bytes_per_token_for_model",
                        lambda path: TEST_KV_BYTES_PER_TOKEN)

    env = EngineEnv(None, models_dir, bound_ports, spawned)
    env.limits = limits_overrides

    # No llama.cpp release is unpacked on a test machine, and downloading one would need the
    # network. Point at a file that exists so the "Binary Missing" branch is not taken.
    #
    # This is also the slowest thing a real start does (it can fetch hundreds of megabytes),
    # so it is where the download gate lives: a test that arms it can hold a thread inside
    # start_model's lock-free phase for as long as it likes.
    fake_binary = dlne_root / "llama-server-stub"
    fake_binary.write_bytes(b"")

    def fake_download(role):
        env.download_gate.pass_through()
        return str(fake_binary)

    monkeypatch.setattr(inference_manager, "download_and_extract_engine", fake_download)

    def fake_popen(cmd, **kwargs):
        # The process (and therefore its hold on the port) exists *before* the gate, exactly
        # as a real spawn does: the child is running while the parent is still returning.
        process = FakeProcess(
            cmd,
            bound_ports,
            terminate_hangs=env.terminate_hangs,
            survives_kill=env.survives_kill,
            wait_gate=env.wait_gate,
        )
        process.env = kwargs.get("env")
        spawned.append(process)
        env.popen_gate.pass_through()
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    # The real probe binds a socket on the host, which would make every assertion depend on
    # whatever else happens to be listening on the developer's machine. The simulated space
    # is authoritative instead, and FakeProcess keeps it honest.
    monkeypatch.setattr(
        InferenceManager,
        "_is_port_bindable",
        staticmethod(lambda port: port not in bound_ports),
    )

    manager = InferenceManager()
    env.manager = manager

    yield env

    # Release both gates unconditionally: a test that failed an assertion before its own
    # release() would otherwise leave a worker thread parked for the full gate timeout.
    env.download_gate.release()
    env.popen_gate.release()
    env.wait_gate.release()

    # The constructor registers stop_all_models with atexit. Left in place, every manager
    # built by this suite would run its teardown against deleted tmp directories during
    # interpreter shutdown and spray log lines over the test report.
    atexit.unregister(manager.stop_all_models)
    manager.running_models.clear()


# Plenty of headroom so the port and endpoint tests are never perturbed by admission control.
BIG_VRAM = 200.0
BIG_SYSRAM = 200.0


# ======================================================================================
# Port allocation (bug #5) — the regression this whole file exists for
# ======================================================================================

def test_port_allocator_never_reissues_a_live_port(engine_env):
    """
    BUG #5 REGRESSION, exactly as reported.

    A -> 8081, B -> 8082, stop A, start C. The old
    ``base_llama_port + len(self.running_models)`` arithmetic recomputed 8082 for C because
    the dict had shrunk back to one entry, so C's llama-server collided with the live B:
    either C died on startup, or the coordinator proxied C's traffic into B's engine and
    served answers from the wrong model.
    """
    manager = engine_env.manager
    for tag in ("model-a", "model-b", "model-c"):
        engine_env.add_model(tag)

    ok_a, port_a, msg_a = manager.start_model("model-a", [], BIG_VRAM, BIG_SYSRAM)
    assert ok_a is True, msg_a
    assert port_a == 8081

    ok_b, port_b, msg_b = manager.start_model("model-b", [], BIG_VRAM, BIG_SYSRAM)
    assert ok_b is True, msg_b
    assert port_b == 8082

    assert manager.stop_model("model-a") is True
    assert 8081 not in engine_env.bound_ports          # A really let go of its port
    assert 8082 in engine_env.bound_ports              # B is still listening

    ok_c, port_c, msg_c = manager.start_model("model-c", [], BIG_VRAM, BIG_SYSRAM)
    assert ok_c is True, msg_c

    # The assertion the bug is about.
    assert port_c != port_b, "allocator handed model C the port live model B is bound to"
    # And the freed port is reused rather than the range drifting upwards forever.
    assert port_c == 8081

    assert manager.running_models["model-b.gguf"]["port"] == 8082
    assert manager.running_models["model-b.gguf"]["process"].poll() is None
    assert manager.running_models["model-c.gguf"]["port"] == 8081


def test_engine_is_spawned_with_the_models_own_chat_template(engine_env):
    """
    Without ``--jinja``, llama.cpp matches the GGUF's chat template against its own list of
    known ones and silently falls back to ChatML for anything unrecognised. A tool-trained
    model served through another family's delimiters emits its function-call format as
    ordinary chat text - which is how ``{"name": "good", "parameters": {}}`` reached a
    user's chat window. See tests/test_context_summary.py for the downstream half.
    """
    manager = engine_env.manager
    engine_env.add_model("model-a")

    ok, _port, msg = manager.start_model("model-a", [], BIG_VRAM, BIG_SYSRAM)
    assert ok is True, msg

    assert "--jinja" in engine_env.spawned[0].cmd


def test_engine_is_spawned_with_the_models_file_name_as_its_alias(engine_env):
    """
    llama-server reports the path it loaded the model from in every answer ("/home/<user>/..."), and the
    answers reach other people. --alias makes it report the file's name instead (engine/model_alias.py).
    """
    manager = engine_env.manager
    engine_env.add_model("model-a")

    ok, _port, msg = manager.start_model("model-a", [], BIG_VRAM, BIG_SYSRAM)
    assert ok is True, msg

    cmd = engine_env.spawned[0].cmd
    assert cmd[cmd.index("-m") + 1] == str(engine_env.models_dir / "model-a.gguf"), "the path is still what is loaded"
    assert cmd[cmd.index("--alias") + 1] == "model-a.gguf"


def _after(cmd, flag):
    return cmd[cmd.index(flag) + 1]


def test_a_draft_companion_is_actually_used_for_speculation(engine_env, make_worker_limits, monkeypatch):
    """
    llama.cpp defaults --spec-type to none. Before 2.1.1 the companion was passed with
    --model-draft and nothing else, so production loaded a 0.5B draft into VRAM and never
    drafted a token with it.
    """
    monkeypatch.setattr(inference_manager, "get_worker_limits",
                        lambda: make_worker_limits(spec_type="draft-simple"))
    manager = engine_env.manager
    engine_env.add_model("model-a")
    engine_env.add_model("model-a_dflash")

    ok, _port, msg = manager.start_model("model-a", [], BIG_VRAM, BIG_SYSRAM)
    assert ok is True, msg

    cmd = engine_env.spawned[0].cmd
    assert _after(cmd, "--model-draft") == str(engine_env.models_dir / "model-a_dflash.gguf")
    assert _after(cmd, "--spec-type") == "draft-simple"
    assert _after(cmd, "--spec-draft-n-max") == "16"
    assert _after(cmd, "--spec-draft-n-min") == "0"
    assert _after(cmd, "--spec-draft-p-min") == "0.75"


def test_no_speculation_flags_without_a_companion(engine_env):
    manager = engine_env.manager
    engine_env.add_model("model-a")

    ok, _port, msg = manager.start_model("model-a", [], BIG_VRAM, BIG_SYSRAM)
    assert ok is True, msg

    cmd = engine_env.spawned[0].cmd
    assert "--model-draft" not in cmd
    assert "--spec-type" not in cmd


def test_speculation_switched_off_does_not_load_the_draft(engine_env, make_worker_limits, monkeypatch):
    """A draft loaded under --spec-type none is VRAM spent on nothing."""
    monkeypatch.setattr(inference_manager, "get_worker_limits",
                        lambda: make_worker_limits(spec_type="none"))
    manager = engine_env.manager
    engine_env.add_model("model-a")
    engine_env.add_model("model-a_dflash")

    ok, _port, msg = manager.start_model("model-a", [], BIG_VRAM, BIG_SYSRAM)
    assert ok is True, msg

    cmd = engine_env.spawned[0].cmd
    assert "--model-draft" not in cmd
    assert "--spec-type" not in cmd


def test_operator_spec_settings_reach_the_engine(engine_env, make_worker_limits, monkeypatch):
    monkeypatch.setattr(inference_manager, "get_worker_limits", lambda: make_worker_limits(
        spec_type="draft-simple,ngram-mod", spec_draft_n_max=8, spec_draft_n_min=2, spec_draft_p_min=0.5))
    manager = engine_env.manager
    engine_env.add_model("model-a")
    engine_env.add_model("model-a_dflash")

    ok, _port, msg = manager.start_model("model-a", [], BIG_VRAM, BIG_SYSRAM)
    assert ok is True, msg

    cmd = engine_env.spawned[0].cmd
    assert _after(cmd, "--spec-type") == "draft-simple,ngram-mod"
    assert _after(cmd, "--spec-draft-n-max") == "8"
    assert _after(cmd, "--spec-draft-n-min") == "2"
    assert _after(cmd, "--spec-draft-p-min") == "0.5"


def test_speculative_args_keeps_ngram_types_without_a_draft():
    """ngram speculation needs no second model, so a missing companion only drops draft-*."""
    args = inference_manager.speculative_args({"spec_type": "draft-simple,ngram-mod"}, None)
    assert args == ["--spec-type", "ngram-mod"]

    assert inference_manager.speculative_args({"spec_type": "draft-simple"}, None) == []
    assert inference_manager.speculative_args({"spec_type": "none"}, "/m/a_dflash.gguf") == []


def test_speculative_args_falls_back_to_the_defaults():
    """The test fixture and older callers pass limits without the spec keys."""
    assert inference_manager.speculative_args({}, "/m/a_dflash.gguf") == []

    args = inference_manager.speculative_args({"spec_type": "draft-simple"}, "/m/a_dflash.gguf")
    assert args == [
        "--model-draft", "/m/a_dflash.gguf", "-ngld", "99",
        "--spec-draft-n-max", "16", "--spec-draft-n-min", "0", "--spec-draft-p-min", "0.75",
        "--spec-type", "draft-simple",
    ]


def test_allocator_skips_ports_held_by_unrelated_processes(engine_env):
    """
    Our own bookkeeping is not enough. A previous DLNE run that was killed without cleanup,
    or any unrelated program, can be sitting on a port in the range; the bind probe is what
    catches that, and skipping it would spawn an engine that dies seconds later.
    """
    manager = engine_env.manager
    engine_env.add_model("model-a")

    engine_env.bound_ports.update({8081, 8082})   # something else owns the first two

    ok, port, msg = manager.start_model("model-a", [], BIG_VRAM, BIG_SYSRAM)

    assert ok is True, msg
    assert port == 8083


def test_ports_are_released_when_every_model_stops(engine_env):
    """After a full teardown the allocator must be back to a clean slate."""
    manager = engine_env.manager
    for tag in ("model-a", "model-b"):
        engine_env.add_model(tag)
        manager.start_model(tag, [], BIG_VRAM, BIG_SYSRAM)

    manager.stop_all_models()

    assert manager.running_models == {}
    assert manager._allocated_ports == set()
    assert engine_env.bound_ports == set()

    engine_env.add_model("model-c")
    assert manager.start_model("model-c", [], BIG_VRAM, BIG_SYSRAM)[1] == 8081


def test_failed_spawn_returns_the_port_immediately(engine_env, monkeypatch):
    """
    The Popen failure branch must release the port it reserved. Without that, a run of failed
    spawns (a corrupt binary, a bad CUDA install) burns straight through the whole range and
    the node stops being able to start anything at all.
    """
    manager = engine_env.manager
    engine_env.add_model("model-a")

    def exploding_popen(cmd, **kwargs):
        raise OSError("llama-server binary is not executable")

    monkeypatch.setattr(subprocess, "Popen", exploding_popen)

    ok, port, message = manager.start_model("model-a", [], BIG_VRAM, BIG_SYSRAM)

    assert ok is False
    assert port == 0
    assert message
    assert manager._allocated_ports == set()
    assert manager.running_models == {}


def test_allocator_reports_exhaustion_instead_of_returning_a_taken_port(engine_env):
    """
    When every candidate port is occupied the manager must refuse to start rather than
    return a number somebody else owns.
    """
    manager = engine_env.manager
    engine_env.add_model("model-a")

    engine_env.bound_ports.update(
        range(manager.base_llama_port,
              manager.base_llama_port + inference_manager.PORT_SCAN_RANGE)
    )

    ok, port, message = manager.start_model("model-a", [], BIG_VRAM, BIG_SYSRAM)

    assert ok is False
    assert port == 0
    assert message == "No free inference port available"


def test_allocator_reclaims_a_port_deleted_straight_out_of_running_models(engine_env):
    """
    ``coordinator.py`` reaps a crashed engine by deleting the entry from ``running_models``
    directly. The reconciliation pass has to notice, otherwise that port stays marked as
    taken for the lifetime of the process and the allocator drifts steadily upwards.

    The engine here is genuinely dead — ``_exit`` both sets the exit code and lets go of the
    port, which is what a crashed process really does. The distinction matters: the
    companion test below covers the entry vanishing while the process is still ALIVE, and
    the allocator has to behave in opposite ways in the two cases.
    """
    manager = engine_env.manager
    for tag in ("model-a", "model-b"):
        engine_env.add_model(tag)

    manager.start_model("model-a", [], BIG_VRAM, BIG_SYSRAM)
    manager.running_models["model-a.gguf"]["process"]._exit(1)   # the engine crashed
    del manager.running_models["model-a.gguf"]

    assert manager.start_model("model-b", [], BIG_VRAM, BIG_SYSRAM)[1] == 8081


def test_allocator_never_reissues_a_port_whose_engine_is_still_alive(engine_env):
    """
    FINDING 2.4 REGRESSION.

    ``_reconcile_allocated_ports`` used to decide membership purely from ``running_models``,
    and the bind probe cannot see an engine that has been spawned but has not ``bind()``ed
    yet — which is the normal state for the tens of seconds a multi-GB model spends loading
    its weights. So an entry disappearing during that window (a reap racing a start, an
    external caller deleting it) made the allocator hand the port to a second engine that
    then died on startup, or left the coordinator proxying one model's traffic into
    another's.

    Process liveness now overrides the bookkeeping entirely.
    """
    manager = engine_env.manager
    for tag in ("model-a", "model-b"):
        engine_env.add_model(tag)

    manager.start_model("model-a", [], BIG_VRAM, BIG_SYSRAM)
    live_process = manager.running_models["model-a.gguf"]["process"]

    # Simulate an engine that is up but has not bound its socket yet, and whose dictionary
    # entry has been removed behind the manager's back.
    engine_env.bound_ports.discard(8081)
    del manager.running_models["model-a.gguf"]
    assert live_process.poll() is None, "the fixture must leave the process running"

    port_b = manager.start_model("model-b", [], BIG_VRAM, BIG_SYSRAM)[1]

    assert port_b != 8081, "allocator reissued a port whose engine process is still alive"
    assert port_b == 8082
    # Once that engine really exits, its port becomes available again.
    live_process._exit(0)
    engine_env.add_model("model-c")
    assert manager.start_model("model-c", [], BIG_VRAM, BIG_SYSRAM)[1] == 8081


# ======================================================================================
# Per-model endpoints (bug #6)
# ======================================================================================

def test_starting_a_second_model_does_not_disturb_the_first(engine_env):
    """
    Endpoints are stored per model.

    They used to live in one process-wide attribute that whichever model started last
    overwrote, so starting B made A's recorded topology look stale and the next request for A
    tore down a perfectly healthy engine — a multi-second stall and a dropped stream for
    every client on A.
    """
    manager = engine_env.manager
    engine_env.add_model("model-a")
    engine_env.add_model("model-b")

    ok_a, port_a, _ = manager.start_model("model-a", ["10.0.0.2:50052"], BIG_VRAM, BIG_SYSRAM)
    assert ok_a is True
    process_a = manager.running_models["model-a.gguf"]["process"]

    manager.start_model(
        "model-b", ["10.0.0.2:50052", "10.0.0.9:50052"], BIG_VRAM, BIG_SYSRAM
    )

    ok_again, port_again, message = manager.start_model(
        "model-a", ["10.0.0.2:50052"], BIG_VRAM, BIG_SYSRAM
    )

    assert (ok_again, port_again, message) == (True, port_a, "Already running")
    assert manager.running_models["model-a.gguf"]["process"] is process_a
    assert process_a.terminate_calls == 0
    assert manager.running_models["model-a.gguf"]["endpoints"] == ["10.0.0.2:50052"]


def test_a_genuine_topology_change_does_restart_that_model(engine_env):
    """The flip side: when *this* model's own worker set really changes, it must be rebuilt."""
    manager = engine_env.manager
    engine_env.add_model("model-a")

    manager.start_model("model-a", ["10.0.0.2:50052"], BIG_VRAM, BIG_SYSRAM)
    first_process = manager.running_models["model-a.gguf"]["process"]

    ok, _port, message = manager.start_model(
        "model-a", ["10.0.0.2:50052", "10.0.0.3:50052"], BIG_VRAM, BIG_SYSRAM
    )

    assert ok is True
    assert message == "Started"
    assert first_process.terminate_calls == 1
    assert manager.running_models["model-a.gguf"]["process"] is not first_process
    assert manager.running_models["model-a.gguf"]["endpoints"] == [
        "10.0.0.2:50052", "10.0.0.3:50052"
    ]


def test_endpoint_order_and_duplicates_do_not_trigger_a_restart(engine_env):
    """
    The same worker set arriving in a different order, or with a duplicate, is not a change.
    Both sides are normalised to a sorted set before comparison.
    """
    manager = engine_env.manager
    engine_env.add_model("model-a")

    manager.start_model(
        "model-a", ["10.0.0.9:50052", "10.0.0.2:50052"], BIG_VRAM, BIG_SYSRAM
    )
    process = manager.running_models["model-a.gguf"]["process"]

    ok, _port, message = manager.start_model(
        "model-a",
        ["10.0.0.2:50052", "10.0.0.9:50052", "10.0.0.2:50052"],
        BIG_VRAM, BIG_SYSRAM,
    )

    assert (ok, message) == (True, "Already running")
    assert process.terminate_calls == 0
    assert manager.running_models["model-a.gguf"]["endpoints"] == [
        "10.0.0.2:50052", "10.0.0.9:50052"
    ]


def test_a_dead_engine_is_rebuilt_even_with_unchanged_endpoints(engine_env):
    """An identical topology is no reason to keep serving from a process that has exited."""
    manager = engine_env.manager
    engine_env.add_model("model-a")

    manager.start_model("model-a", ["10.0.0.2:50052"], BIG_VRAM, BIG_SYSRAM)
    dead = manager.running_models["model-a.gguf"]["process"]
    dead._exit(1)   # the engine crashed on its own

    ok, _port, message = manager.start_model(
        "model-a", ["10.0.0.2:50052"], BIG_VRAM, BIG_SYSRAM
    )

    assert ok is True
    assert message == "Started"
    assert manager.running_models["model-a.gguf"]["process"] is not dead


def test_current_endpoints_is_the_sorted_union_and_still_a_list(engine_env):
    """
    ``coordinator.py`` serialises this into the telemetry JSON and compares
    ``set(manager.current_endpoints)`` against the live registry, so it has to stay a plain
    ``list`` — a set would break ``json.dumps`` and the shipped WebUI reads that field.
    """
    manager = engine_env.manager
    engine_env.add_model("model-a")
    engine_env.add_model("model-b")

    assert manager.current_endpoints == []

    manager.start_model("model-a", ["10.0.0.9:50052"], BIG_VRAM, BIG_SYSRAM)
    manager.start_model(
        "model-b", ["10.0.0.2:50052", "10.0.0.9:50052"], BIG_VRAM, BIG_SYSRAM
    )

    assert isinstance(manager.current_endpoints, list)
    assert manager.current_endpoints == ["10.0.0.2:50052", "10.0.0.9:50052"]

    manager.stop_model("model-b")
    assert manager.current_endpoints == ["10.0.0.9:50052"]


# ======================================================================================
# Unified memory accounting (bug #7)
# ======================================================================================

def test_admission_and_context_sizing_share_one_budget(engine_env, monkeypatch):
    """
    BUG #7 REGRESSION.

    Admission used to count model *weights* only, while context sizing then handed the model
    every byte that was not weights. The second model was therefore admitted into memory the
    first one's KV cache had already taken, and the cluster OOMed under load.

    With a 9.0 GB usable pool and two 4.2 GB models: the weights-only sum is 8.4 GB, which
    "leaves" 0.6 GB and admits the second model. Counting the KV cache the first model
    actually reserved leaves 0.1 GB, which correctly refuses it.
    """
    manager = engine_env.manager
    engine_env.add_model("big-a")
    engine_env.add_model("big-b")

    weights_gb = 4.2
    # Patched rather than staged as a real 4 GB file on disk.
    monkeypatch.setattr(manager, "estimate_model_vram_gb", lambda path: weights_gb)

    total_vram, total_sysram = 10.0, 0.0
    budget = (total_vram + total_sysram) * inference_manager.POOL_SAFETY_FRACTION

    ok_a, _port_a, msg_a = manager.start_model("big-a", [], total_vram, total_sysram)
    assert ok_a is True, msg_a

    entry = manager.running_models["big-a.gguf"]
    # The cache is stored q8_0 below 131,072 tokens of context a slot, which costs 34/64 of
    # f16 (q4_0, 18/64, from there); this model's context a slot is far below that line.
    assert entry["safe_context"] // entry["dynamic_slots"] < 131072
    q8_gb_per_token = inference_manager.gguf.scale_kv_bytes(TEST_KV_BYTES_PER_TOKEN, "q8_0") / (1024 ** 3)
    expected_reserved = weights_gb + entry["safe_context"] * q8_gb_per_token
    assert entry["reserved_gb"] == pytest.approx(expected_reserved)
    # The KV cache is genuinely charged to the pool, not silently free.
    assert entry["reserved_gb"] > weights_gb

    ok_b, port_b, msg_b = manager.start_model("big-b", [], total_vram, total_sysram)

    assert ok_b is False
    # Frozen string: coordinator.py matches on it exactly to emit the graceful "switch to a
    # running model" nudge instead of a 500.
    assert msg_b == "Network Maxed Out"
    assert port_b == 0
    assert "big-b.gguf" not in manager.running_models
    # No port was burned on the rejected model.
    assert manager._allocated_ports == {entry["port"]}

    # The arithmetic the rejection rests on, spelled out.
    assert budget - entry["reserved_gb"] - weights_gb < inference_manager.MIN_KV_GB


def test_reserved_memory_never_exceeds_the_safety_capped_pool(engine_env, monkeypatch):
    """
    Whatever combination of models is admitted, the sum of what they reserved must stay
    inside the pool minus the safety fraction. Workers report *total* memory, not free
    memory, so the last 10% is the only headroom the OS and the RPC servers get.
    """
    manager = engine_env.manager
    monkeypatch.setattr(manager, "estimate_model_vram_gb", lambda path: 1.0)

    total_vram, total_sysram = 12.0, 4.0
    budget = (total_vram + total_sysram) * inference_manager.POOL_SAFETY_FRACTION

    admitted = 0
    for index in range(12):
        tag = f"model-{index}"
        engine_env.add_model(tag)
        if manager.start_model(tag, [], total_vram, total_sysram)[0]:
            admitted += 1

    assert admitted > 0, "the very first model should always fit a 14.4GB pool"
    committed = sum(d["reserved_gb"] for d in manager.running_models.values())
    assert committed <= budget + 1e-9


def test_context_respects_both_the_hardware_ceiling_and_the_model_declaration(engine_env, monkeypatch, dlne_root):
    """
    A slot's context is never more than the model declares, and the total never drops below
    the absolute floor, under which an instruct model cannot fit a system prompt plus a reply.

    A generous pool must not push the context past what the model actually supports (that
    reserves memory nobody can use and locks other models out).
    """
    manager = engine_env.manager
    engine_env.add_model("model-a")
    monkeypatch.setattr(manager, "estimate_model_vram_gb", lambda path: 1.0)
    # Declare 8192 per slot explicitly; the built-in default is 32,768 now.
    (dlne_root / "models" / "models.json").write_text(
        '[{"id": "model-a", "max_context": 8192}]', encoding="utf-8"
    )
    # The operator pins two slots here (llama-server splits -c across them); the automatic
    # slot count has its own tests below.
    engine_env.limits["engine_slots"] = 2
    slots = 2
    declared_total = 8192 * slots

    # Huge pool: the model's own declaration (per slot, times slots) is the binding
    # constraint, not the hardware.
    ok, _port, msg = manager.start_model("model-a", [], 1000.0, 1000.0)
    assert ok is True, msg
    assert manager.running_models["model-a.gguf"]["safe_context"] == declared_total

    manager.stop_all_models()

    # Tight pool: the hardware ceiling bites, but the floor still holds.
    ok, _port, msg = manager.start_model("model-a", [], 1.7, 0.0)
    assert ok is True, msg
    context = manager.running_models["model-a.gguf"]["safe_context"]
    assert context >= inference_manager.MIN_CONTEXT_TOKENS
    assert context <= declared_total


def test_a_model_that_cannot_fit_at_all_is_refused(engine_env, monkeypatch):
    """A pool smaller than the weights alone must never admit anything."""
    manager = engine_env.manager
    engine_env.add_model("huge")
    monkeypatch.setattr(manager, "estimate_model_vram_gb", lambda path: 400.0)

    ok, port, message = manager.start_model("huge", [], 8.0, 8.0)

    assert (ok, port, message) == (False, 0, "Network Maxed Out")
    assert manager.running_models == {}


def test_unknown_model_size_is_estimated_pessimistically(engine_env):
    """
    A model that has not finished downloading has no size on disk. Returning 0 would let the
    coordinator promise capacity the cluster does not have, so the pessimistic constant is
    used instead.
    """
    manager = engine_env.manager

    assert manager.estimate_model_vram_gb("not-downloaded-yet") == \
        inference_manager.UNKNOWN_MODEL_ESTIMATE_GB


def test_estimate_scales_a_real_file_by_the_overhead_factor(engine_env):
    """A GGUF's on-disk size is not its resident footprint; the documented factor applies."""
    manager = engine_env.manager
    size_bytes = 4 * 1024 * 1024        # 4 MiB is enough to check the arithmetic
    engine_env.add_model("small", size_bytes=size_bytes)

    expected = (size_bytes / (1024 ** 3)) * inference_manager.VRAM_OVERHEAD_FACTOR
    assert manager.estimate_model_vram_gb("small") == pytest.approx(expected)


# ======================================================================================
# models.json id sanitisation (bug #20)
# ======================================================================================

def test_max_context_is_found_despite_the_colon_in_the_library_id(engine_env, dlne_root):
    """
    BUG #20.

    ``models.json`` is written with raw ids (``phi3:mini``) while the id the engine is asked
    for has already been sanitised (``phi3-mini``), so a straight equality test never matched
    and *every* model silently fell back to 8192 tokens no matter what it declared.
    """
    manager = engine_env.manager
    engine_env.add_model("phi3-mini")
    (dlne_root / "models" / "models.json").write_text(
        '[{"id": "phi3:mini", "max_context": 4096}]', encoding="utf-8"
    )

    assert manager._lookup_json_max_context("phi3-mini") == 4096
    assert manager._lookup_json_max_context("phi3:mini") == 4096


def test_max_context_falls_back_to_the_default_on_a_corrupt_library(engine_env, dlne_root):
    """A half-written library file must not stop inference; the 32,768 default is the fallback."""
    manager = engine_env.manager
    (dlne_root / "models" / "models.json").write_text('[{"id": "phi3:mi', encoding="utf-8")

    assert manager._lookup_json_max_context("phi3-mini") == 32768


# ======================================================================================
# Path containment (bug #13)
# ======================================================================================

@pytest.mark.parametrize(
    "tag",
    [
        # The ``model`` field of an OpenAI chat request reaches resolve_model_path and the
        # result becomes `llama-server -m <path>`. Every one of these used to be accepted if
        # the file happened to exist or the string merely ended in .gguf.
        "../../../etc/passwd",
        "..",
        "../models/phi3-mini",
        "sub/dir/model",
        "sub\\dir\\model",
        "/etc/shadow",
        "\\\\attacker-host\\share\\evil.gguf",
        "C:\\Windows\\System32\\config\\SAM",
        "C:model",
        "c:model.gguf",
        "~/secret",
        "~\\secret",
        "model\x00.gguf",
        "model\nname",
        "",
    ],
)
def test_resolve_model_path_rejects_path_shaped_tags(engine_env, tag):
    """A tag is a bare file name and nothing else; anything path-shaped is refused outright."""
    assert engine_env.manager.resolve_model_path(tag) == ""


@pytest.mark.parametrize("tag", [None, 42, ["phi3-mini"], {"id": "phi3-mini"}])
def test_resolve_model_path_rejects_non_strings(engine_env, tag):
    """The request body is JSON, so ``model`` can arrive as any type at all."""
    assert engine_env.manager.resolve_model_path(tag) == ""


def test_resolve_model_path_finds_a_model_in_the_configured_root(engine_env):
    """The positive case, including the Ollama-style ``:`` -> ``-`` flattening."""
    manager = engine_env.manager
    path = engine_env.add_model("phi3-mini")
    expected = os.path.realpath(str(path))

    assert manager.resolve_model_path("phi3-mini") == expected
    assert manager.resolve_model_path("phi3:mini") == expected
    assert manager.resolve_model_path("phi3-mini.gguf") == expected


def test_resolve_model_path_cannot_reach_a_file_outside_every_root(engine_env, tmp_path):
    """
    Containment is enforced on the resolved path, not on the spelling of the tag: a file that
    exists, is readable and ends in ``.gguf`` is still refused when it lives outside the
    trusted roots.
    """
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "borrowed.gguf").write_bytes(b"\0")

    assert engine_env.manager.resolve_model_path("borrowed") == ""


def test_resolve_model_path_returns_empty_for_a_model_that_does_not_exist(engine_env):
    """No file, no path — the caller must not be handed a name to pass to ``-m``."""
    assert engine_env.manager.resolve_model_path("never-downloaded") == ""


def test_start_model_refuses_a_tag_it_cannot_contain(engine_env):
    """
    End to end: an uncontained tag never reaches ``Popen``, so no subprocess is spawned with
    an attacker-chosen ``-m`` argument.
    """
    manager = engine_env.manager

    ok, port, message = manager.start_model(
        "../../../etc/passwd", [], BIG_VRAM, BIG_SYSRAM
    )

    assert ok is False
    assert port == 0
    assert "Could not find absolute blob path" in message
    assert engine_env.spawned == []
    assert manager._allocated_ports == set()


# ======================================================================================
# Teardown (bug #31)
# ======================================================================================

def test_stop_all_models_kills_a_process_that_ignores_terminate(engine_env):
    """
    BUG #31 REGRESSION.

    ``stop_all_models`` used to call ``terminate()``, log the timeout, and then ``.clear()``
    the dictionary — orphaning a live llama-server that still held both its VRAM and its TCP
    port, with no handle left anywhere in the process to stop it.
    """
    manager = engine_env.manager
    engine_env.add_model("stubborn")
    engine_env.terminate_hangs = True

    manager.start_model("stubborn", [], BIG_VRAM, BIG_SYSRAM)
    process = manager.running_models["stubborn.gguf"]["process"]
    log_file = manager.running_models["stubborn.gguf"]["log_file"]

    manager.stop_all_models()

    assert process.terminate_calls == 1
    assert process.kill_calls == 1, "terminate timeout was not escalated to kill"
    assert process.poll() is not None
    assert engine_env.bound_ports == set()
    assert manager.running_models == {}
    assert manager._allocated_ports == set()
    assert log_file.closed, "the child's log file descriptor was leaked"


def test_stop_model_closes_the_log_file_and_frees_the_port(engine_env):
    """
    ``stop_model`` exists so callers have a teardown path that releases everything; deleting
    straight out of ``running_models`` leaks the descriptor and the port.
    """
    manager = engine_env.manager
    engine_env.add_model("model-a")
    manager.start_model("model-a", [], BIG_VRAM, BIG_SYSRAM)
    log_file = manager.running_models["model-a.gguf"]["log_file"]

    assert manager.stop_model("model-a") is True

    assert log_file.closed
    assert manager._allocated_ports == set()
    assert manager.running_models == {}


def test_stop_model_reports_a_model_that_was_not_running(engine_env):
    """A no-op teardown must be safe and honest about having done nothing."""
    assert engine_env.manager.stop_model("never-started") is False


def test_stop_all_models_is_safe_to_call_twice(engine_env):
    """``atexit`` can fire after an explicit shutdown has already run."""
    manager = engine_env.manager
    engine_env.add_model("model-a")
    manager.start_model("model-a", [], BIG_VRAM, BIG_SYSRAM)

    manager.stop_all_models()
    manager.stop_all_models()

    assert manager.running_models == {}
    assert manager._allocated_ports == set()


# ======================================================================================
# Concurrency — findings 2.1, 2.2, 2.3
#
# ``astart_model`` dispatches ``start_model`` onto a worker thread and ``coordinator.py``
# calls it once per chat request with no mutual exclusion, so overlapping starts are the
# normal case. Every test below drives the race deterministically with :class:`Gate`
# instead of relying on timing, and every one of them joins its threads through
# :func:`join_all`, which doubles as the deadlock detector for the ``_state_lock`` /
# ``_port_lock`` ordering.
# ======================================================================================

def test_concurrent_starts_of_the_same_model_spawn_exactly_one_engine(engine_env):
    """
    FINDING 2.1 REGRESSION.

    ``if model_path in self.running_models`` and ``self.running_models[model_path] = {...}``
    were an unsynchronised check-then-act separated by a stat, a JSON read, an engine
    download, up to 256 socket binds and a ``Popen``. Two simultaneous POSTs to
    ``/v1/chat/completions`` for a model that is not loaded yet both missed the check, both
    spawned an engine, and the second assignment destroyed the only reference to the first's
    ``Popen`` *and* its open log file. That engine then ran until the machine was rebooted,
    holding its VRAM and its port, with nothing left anywhere in the process able to stop it.

    The race is made deterministic: the winner is held inside the (lock-free) spawn phase
    until all three losers are provably parked on its completion event.
    """
    manager = engine_env.manager
    engine_env.add_model("model-a")
    engine_env.download_gate.arm()

    results = {}

    def start(name):
        results[name] = manager.start_model("model-a", [], BIG_VRAM, BIG_SYSRAM)

    winner = threading.Thread(target=start, args=("winner",), name="winner")
    winner.start()
    # The winner now holds the reservation and is inside the slow phase.
    engine_env.download_gate.wait_for_arrivals(1)

    losers = [
        threading.Thread(target=start, args=(f"loser-{index}",), name=f"loser-{index}")
        for index in range(3)
    ]
    for thread in losers:
        thread.start()

    # Deterministic proof that all three losers hit the in-flight start rather than racing
    # past it: they have each parked on the winner's completion event at least once.
    wait_until(
        lambda: manager._starting.get("model-a.gguf", {}).get("handoffs", 0) >= 3,
        message="the losing threads never saw the in-flight start",
    )
    # While the start is in flight nothing is published yet...
    assert manager.running_models == {}
    # ...but its memory and its port are already booked.
    assert manager._starting["model-a.gguf"]["reserved_gb"] > 0
    assert manager._allocated_ports == {manager._starting["model-a.gguf"]["port"]}

    engine_env.download_gate.release()
    join_all([winner] + losers)

    # THE ASSERTION THE BUG IS ABOUT: one model, one process, one log file, one port.
    assert len(engine_env.spawned) == 1, "a concurrent start orphaned a second engine"
    assert list(manager.running_models) == ["model-a.gguf"]
    assert engine_env.bound_ports == {manager.running_models["model-a.gguf"]["port"]}
    assert manager.running_models["model-a.gguf"]["process"] is engine_env.spawned[0]

    port = manager.running_models["model-a.gguf"]["port"]
    assert results["winner"] == (True, port, "Started")
    for index in range(3):
        # The losers did not fail and did not start anything; they were handed the winner's
        # engine, which is what the caller wanted in the first place.
        assert results[f"loser-{index}"] == (True, port, "Already running")

    # No reservation and no waiter survives a completed start.
    assert manager._starting == {}


def test_concurrent_admissions_cannot_over_commit_the_pool(engine_env, monkeypatch):
    """
    FINDING 2.2 REGRESSION.

    The admission budget was read at the top of ``start_model`` but the model's
    ``reserved_gb`` was only written at the very bottom, after the engine had spawned. Every
    thread inside that window therefore sized itself against a pool that looked emptier than
    it was. The auditor's worked example: 24 GB VRAM + 8 GB RAM gives a 28.8 GB budget, one
    model reserves 20.54 GB, and two concurrent admits commit 41.08 GB on a 32 GB machine.

    Here four 4 GB models race for a 10.8 GB budget that fits exactly two. The two that are
    admitted are held in the spawn phase — nothing published, ``running_models`` still empty
    — while the other two make their decision, so the *only* thing that can refuse them is
    the reservation published by the in-flight starts.
    """
    manager = engine_env.manager
    monkeypatch.setattr(manager, "estimate_model_vram_gb", lambda path: 4.0)
    # Two slots pinned: the automatic count would give the first model four and leave room
    # for no second one, which is a different (and tested elsewhere) property.
    engine_env.limits["engine_slots"] = 2
    engine_env.download_gate.arm()

    total_vram, total_sysram = 12.0, 0.0
    budget = (total_vram + total_sysram) * inference_manager.POOL_SAFETY_FRACTION

    tags = [f"racer-{index}" for index in range(4)]
    for tag in tags:
        engine_env.add_model(tag)

    results = {}

    def start(tag):
        results[tag] = manager.start_model(tag, [], total_vram, total_sysram)

    threads = [threading.Thread(target=start, args=(tag,), name=tag) for tag in tags]
    for thread in threads:
        thread.start()

    # Two threads get in and park in the spawn phase; the other two are refused outright and
    # never reach the gate at all.
    engine_env.download_gate.wait_for_arrivals(2)
    wait_until(
        lambda: len(results) == 2,
        message="the losing threads never returned a decision",
    )

    # The decisive assertion: both refusals were decided while NOTHING was published, so
    # they can only have come from the in-flight reservations.
    assert manager.running_models == {}
    assert len(manager._starting) == 2
    refusals = [result for result in results.values() if not result[0]]
    assert len(refusals) == 2
    for ok, port, message in refusals:
        assert (ok, port, message) == (False, 0, "Network Maxed Out")

    engine_env.download_gate.release()
    join_all(threads)

    admitted = [tag for tag, result in results.items() if result[0]]
    assert len(admitted) == 2, f"expected exactly two admissions, got {admitted}"
    assert len(engine_env.spawned) == 2

    committed = sum(data["reserved_gb"] for data in manager.running_models.values())
    assert committed <= budget + 1e-9, f"over-committed: {committed:.2f}GB of a {budget:.2f}GB pool"

    # And the ports are distinct, which the same lock is responsible for.
    ports = sorted(data["port"] for data in manager.running_models.values())
    assert ports == [8081, 8082]


def test_stop_all_models_racing_a_start_leaves_nothing_behind(engine_env):
    """
    FINDING 2.3 REGRESSION, the hard half.

    ``stop_all_models`` snapshotted ``running_models``, terminated the snapshot, and then
    unconditionally ``.clear()``ed the dict. Anything published after the snapshot was
    dropped without terminate, without kill and without closing its log file — a live
    llama-server holding VRAM and a port that nothing could ever stop again.

    The race is forced at the worst possible instant: the engine process already exists (it
    is parked inside ``Popen``) when the teardown runs, so the start thread is guaranteed to
    reach the publication step *after* ``stop_all_models`` has finished walking the dict.
    """
    manager = engine_env.manager
    engine_env.add_model("model-a")
    engine_env.popen_gate.arm()

    result = {}

    def start():
        result["value"] = manager.start_model("model-a", [], BIG_VRAM, BIG_SYSRAM)

    starter = threading.Thread(target=start, name="starter")
    starter.start()

    # The child process is alive and holding its port, but the entry has not been published.
    engine_env.popen_gate.wait_for_arrivals(1)
    assert engine_env.spawned and engine_env.spawned[0].poll() is None
    assert manager.running_models == {}

    # This must not block behind the in-flight start (the state lock is not held across the
    # spawn) and must not deadlock against it either.
    stop_finished = threading.Event()

    def stop():
        manager.stop_all_models()
        stop_finished.set()

    stopper = threading.Thread(target=stop, name="stopper")
    stopper.start()
    assert stop_finished.wait(timeout=10.0), "stop_all_models blocked behind a spawning engine"

    engine_env.popen_gate.release()
    join_all([starter, stopper])

    ok, port, message = result["value"]
    assert ok is False
    assert port == 0
    assert message == inference_manager.START_CANCELLED_MESSAGE

    # Nothing published, nothing left running, nothing left holding a port.
    assert manager.running_models == {}
    assert manager._starting == {}
    assert manager._allocated_ports == set()
    assert engine_env.bound_ports == set()
    assert manager._zombies == []

    orphan = engine_env.spawned[0]
    assert orphan.poll() is not None, "the engine spawned during shutdown was orphaned"
    assert orphan.terminate_calls == 1


def test_a_start_cancelled_before_spawning_never_creates_a_process(engine_env):
    """
    The cheaper half of the same guarantee: when the teardown wins the race outright there
    is no point spawning an engine at all, so the pre-Popen cancellation check short-circuits
    and no process is ever created.
    """
    manager = engine_env.manager
    engine_env.add_model("model-a")
    engine_env.download_gate.arm()

    result = {}

    def start():
        result["value"] = manager.start_model("model-a", [], BIG_VRAM, BIG_SYSRAM)

    starter = threading.Thread(target=start, name="starter")
    starter.start()
    engine_env.download_gate.wait_for_arrivals(1)

    manager.stop_all_models()

    engine_env.download_gate.release()
    join_all([starter])

    assert result["value"] == (False, 0, inference_manager.START_CANCELLED_MESSAGE)
    assert engine_env.spawned == []
    assert manager.running_models == {}
    assert manager._starting == {}
    assert manager._allocated_ports == set()


def test_stop_model_cancels_an_in_flight_start_of_that_model(engine_env):
    """
    Unloading a model the instant after requesting it must not leave an engine running that
    nothing is tracking — the single-model version of the teardown race.
    """
    manager = engine_env.manager
    engine_env.add_model("model-a")
    engine_env.add_model("model-b")
    engine_env.download_gate.arm()

    result = {}

    def start():
        result["value"] = manager.start_model("model-a", [], BIG_VRAM, BIG_SYSRAM)

    starter = threading.Thread(target=start, name="starter")
    starter.start()
    engine_env.download_gate.wait_for_arrivals(1)

    assert manager.stop_model("model-a") is True, "an in-flight start counts as running"
    # An unrelated model is untouched by that cancellation.
    assert manager.stop_model("model-b") is False

    engine_env.download_gate.release()
    join_all([starter])

    assert result["value"] == (False, 0, inference_manager.START_CANCELLED_MESSAGE)
    assert manager.running_models == {}
    assert manager._allocated_ports == set()
    assert engine_env.bound_ports == set()


def test_hammering_start_and_stop_from_many_threads_never_deadlocks(engine_env):
    """
    THE DEADLOCK TEST, run for real rather than argued.

    ``_state_lock`` (outer) and ``_port_lock`` (leaf) are taken together on every start,
    every teardown and every port operation; ``atexit`` re-enters ``stop_all_models``
    synchronously while a ``to_thread`` start may be in flight; and the prune/reconcile
    paths call into each other. This drives all of those simultaneously — starts, stops,
    single-model unloads, admission queries and telemetry reads — and fails if any thread is
    still alive at the join, which is what a lock-ordering inversion or a wait-while-holding
    would produce.
    """
    manager = engine_env.manager
    tags = [f"model-{index}" for index in range(4)]
    for tag in tags:
        engine_env.add_model(tag)

    errors = []
    rounds = 25

    def starter(tag):
        try:
            for _ in range(rounds):
                manager.start_model(tag, [], BIG_VRAM, BIG_SYSRAM)
                manager.stop_model(tag)
        except Exception as exc:                      # pragma: no cover - failure detail
            errors.append(f"starter {tag}: {exc!r}")

    def stopper():
        try:
            for _ in range(rounds):
                manager.stop_all_models()
        except Exception as exc:                      # pragma: no cover - failure detail
            errors.append(f"stopper: {exc!r}")

    def reader():
        try:
            for _ in range(rounds * 4):
                manager.current_endpoints            # noqa: B018 - property with logic
                manager.get_active_model_names()
                manager.can_admit("model-0", BIG_VRAM, BIG_SYSRAM)
        except Exception as exc:                      # pragma: no cover - failure detail
            errors.append(f"reader: {exc!r}")

    threads = [threading.Thread(target=starter, args=(tag,), name=f"start-{tag}") for tag in tags]
    threads.append(threading.Thread(target=stopper, name="stop-all"))
    threads.append(threading.Thread(target=reader, name="reader"))

    for thread in threads:
        thread.start()
    join_all(threads, timeout=60.0)

    assert errors == []

    # Whatever survived the hammering, the invariants still hold.
    manager.stop_all_models()
    assert manager.running_models == {}
    assert manager._starting == {}
    assert manager._allocated_ports == set()
    assert engine_env.bound_ports == set()


def test_stop_all_models_is_reentrant_from_the_thread_that_already_holds_the_lock(engine_env):
    """
    ``atexit`` fires ``stop_all_models`` synchronously, potentially on a thread that is
    already inside the manager. Both locks are ``RLock``s precisely so that re-entry is a
    no-op rather than a self-deadlock; a plain ``Lock`` would hang the interpreter on exit
    with no diagnostic at all.
    """
    manager = engine_env.manager
    engine_env.add_model("model-a")
    manager.start_model("model-a", [], BIG_VRAM, BIG_SYSRAM)

    with manager._state_lock:
        with manager._port_lock:
            manager.stop_all_models()          # would deadlock on a non-reentrant lock

    assert manager.running_models == {}
    assert manager._allocated_ports == set()


# ======================================================================================
# Admission gate exposed to the coordinator — finding 2.6
# ======================================================================================

def test_can_admit_agrees_with_start_model(engine_env, monkeypatch):
    """
    FINDING 2.6 / 1.9.

    ``coordinator.py`` had a second, weaker auto-deploy gate: weights only, against the raw
    uncorrected pool, with no safety fraction, no already-committed term and no KV term — a
    different number from the one that then decided whether the start actually succeeded, so
    it green-lit deployments that were then refused. ``can_admit`` is the authoritative
    answer, and this test pins it to ``start_model``'s own verdict rather than to a
    re-implementation of the arithmetic.
    """
    manager = engine_env.manager
    monkeypatch.setattr(manager, "estimate_model_vram_gb", lambda path: 4.2)
    for tag in ("big-a", "big-b"):
        engine_env.add_model(tag)

    total_vram, total_sysram = 10.0, 0.0

    assert manager.can_admit("big-a", total_vram, total_sysram) == (True, "ok")
    assert manager.start_model("big-a", [], total_vram, total_sysram)[0] is True

    # The first model's weights AND the KV cache it reserved now count against the pool.
    admitted, reason = manager.can_admit("big-b", total_vram, total_sysram)
    assert (admitted, reason) == (False, "Network Maxed Out")
    # ...and start_model returns exactly the same verdict, which is the whole point.
    assert manager.start_model("big-b", [], total_vram, total_sysram) == (
        False, 0, "Network Maxed Out"
    )

    # Freeing the incumbent frees the verdict too.
    manager.stop_model("big-a")
    assert manager.can_admit("big-b", total_vram, total_sysram) == (True, "ok")


def test_can_admit_reports_a_running_or_starting_model_as_admissible(engine_env):
    """
    A model that is already loaded takes no new memory, so the gate must not refuse a
    request for it just because the pool is otherwise full. The same holds while its start
    is still in flight — its reservation is already booked.
    """
    manager = engine_env.manager
    engine_env.add_model("model-a")
    # model-b exists on disk too, so the refusal below can only be about memory. Before the
    # R3-7 fix can_admit never resolved the path at all and a model that was not downloaded
    # yet was reported as admissible; that case is now covered by its own test rather than
    # accidentally riding along in this one.
    engine_env.add_model("model-b")
    engine_env.download_gate.arm()

    starter = threading.Thread(
        target=manager.start_model,
        args=("model-a", [], BIG_VRAM, BIG_SYSRAM),
        name="starter",
    )
    starter.start()
    engine_env.download_gate.wait_for_arrivals(1)

    # Zero pool, yet still admissible: nothing new would be allocated.
    assert manager.can_admit("model-a", 0.0, 0.0) == (True, "ok")

    engine_env.download_gate.release()
    join_all([starter])

    assert manager.can_admit("model-a", 0.0, 0.0) == (True, "ok")
    # An unrelated model on a zero-sized pool is still refused.
    assert manager.can_admit("model-b", 0.0, 0.0) == (
        False, inference_manager.NETWORK_MAXED_OUT_MESSAGE
    )


def test_can_admit_counts_a_zombie_engine(engine_env, monkeypatch):
    """An engine that survived SIGKILL is still holding its memory, so it still counts."""
    manager = engine_env.manager
    monkeypatch.setattr(manager, "estimate_model_vram_gb", lambda path: 4.2)
    for tag in ("big-a", "big-b"):
        engine_env.add_model(tag)

    engine_env.terminate_hangs = True
    engine_env.survives_kill = True
    manager.start_model("big-a", [], 10.0, 0.0)
    manager.stop_model("big-a")

    assert len(manager._zombies) == 1
    assert manager.can_admit("big-b", 10.0, 0.0) == (False, "Network Maxed Out")


# ======================================================================================
# Unkillable engines — finding 2.5
# ======================================================================================

def test_an_engine_that_survives_sigkill_keeps_its_port_and_its_memory(engine_env, monkeypatch):
    """
    FINDING 2.5 REGRESSION.

    ``_terminate_entry`` logged "survived SIGKILL; its port and memory stay reserved" and
    then unconditionally released the port and dropped the entry — so ``committed``
    under-reported memory a live process still owned, and the allocator could hand that
    process's port to the next engine. The log line and the behaviour now agree: the process
    is quarantined, keeps counting, and keeps its port off the allocator until it dies.
    """
    manager = engine_env.manager
    monkeypatch.setattr(manager, "estimate_model_vram_gb", lambda path: 1.0)
    for tag in ("stubborn", "next-one", "later-one"):
        engine_env.add_model(tag)

    engine_env.terminate_hangs = True
    engine_env.survives_kill = True
    manager.start_model("stubborn", [], 40.0, 0.0)
    undead = manager.running_models["stubborn.gguf"]["process"]
    reserved = manager.running_models["stubborn.gguf"]["reserved_gb"]

    engine_env.terminate_hangs = False
    engine_env.survives_kill = False

    manager.stop_all_models()

    # Escalation happened, and failed.
    assert undead.terminate_calls == 1
    assert undead.kill_calls == 1
    assert undead.poll() is None, "the fixture must leave this process alive"

    # It is out of running_models but it has NOT been forgotten.
    assert manager.running_models == {}
    assert len(manager._zombies) == 1
    assert manager._zombies[0]["reserved_gb"] == pytest.approx(reserved)
    assert manager._committed_gb_locked() == pytest.approx(reserved)

    # Its port is still off the allocator, so the next engine goes elsewhere...
    assert 8081 in manager._allocated_ports
    assert manager.start_model("next-one", [], 40.0, 0.0)[1] == 8082

    # ...until the OS finally reaps it, at which point everything comes back.
    undead._exit(-9)
    assert manager.start_model("later-one", [], 40.0, 0.0)[1] == 8081
    assert manager._zombies == []


# ======================================================================================
# Async wrappers (bug #10)
# ======================================================================================

def test_async_wrappers_forward_to_the_blocking_implementations(engine_env):
    """
    ``start_model`` blocks for up to ~4 s on terminate/wait and ``Popen`` touches the disk.
    Called straight from the coordinator's event loop they stall every other request,
    WebSocket broadcast and heartbeat, so the coordinator uses these wrappers instead. The
    synchronous methods stay because ``atexit`` cannot await anything.
    """
    import asyncio

    manager = engine_env.manager
    engine_env.add_model("model-a")

    async def scenario():
        started = await manager.astart_model("model-a", [], BIG_VRAM, BIG_SYSRAM)
        assert started[0] is True
        assert started[1] == 8081
        assert started[2] == "Started"

        await manager.astop_all_models()
        return manager.running_models

    assert asyncio.run(scenario()) == {}
    assert manager._allocated_ports == set()


def test_astart_model_forwards_keyword_arguments(engine_env):
    """``queue_channels`` is passed by keyword from the coordinator; it must survive the hop."""
    import asyncio

    manager = engine_env.manager
    engine_env.add_model("model-a")

    async def scenario():
        return await manager.astart_model(
            "model-a", [], BIG_VRAM, BIG_SYSRAM, queue_channels=6
        )

    ok, _port, _message = asyncio.run(scenario())

    assert ok is True
    # queue_channels is a floor on the slot count, not a cap: this pool holds more than 6.
    slots = manager.running_models["model-a.gguf"]["dynamic_slots"]
    assert slots >= 6
    assert "-np" in engine_env.spawned[0].cmd
    assert engine_env.spawned[0].cmd[engine_env.spawned[0].cmd.index("-np") + 1] == str(slots)


def test_queue_channels_is_honoured_when_the_pool_holds_fewer_slots(engine_env, monkeypatch):
    """
    After a traffic spike the coordinator restarts the engine with 6 lanes. A pool that
    holds only one slot at the floor context still gets 6: the slots are kept and the
    context gives way, as before the engine chose its own slot count.
    """
    manager = engine_env.manager
    engine_env.add_model("model-a")
    monkeypatch.setattr(manager, "estimate_model_vram_gb", lambda path: 8.0)

    ok, _port, message = manager.start_model("model-a", [], 12.0, 0.0, queue_channels=6)

    assert ok is True, message
    entry = manager.running_models["model-a.gguf"]
    assert entry["dynamic_slots"] == 6
    assert entry["safe_context"] // 6 < inference_manager.SLOT_CONTEXT_FLOOR
    assert entry["safe_context"] // 6 >= inference_manager.MIN_CONTEXT_TOKENS


def test_every_locking_method_has_an_async_wrapper(engine_env):
    """
    The rule the coordinator codes against: anything that takes ``_state_lock`` is reachable
    from a coroutine without ``asyncio.to_thread`` at the call site. Round 2 shipped only two
    wrappers, so the chat handler called ``reap_model`` synchronously on the event loop and
    inherited the full teardown wait (finding R3-6).

    Each wrapper is exercised for real rather than merely checked for existence, so a wrapper
    that forwards the wrong arguments fails here.
    """
    import asyncio

    manager = engine_env.manager
    for tag in ("model-a", "model-b"):
        engine_env.add_model(tag)

    for name in (
        "astart_model", "astop_all_models", "astop_model", "areap_model",
        "acan_admit", "asnapshot_model", "aestimate_model_vram_gb",
        "aresolve_model_path", "aget_available_models",
    ):
        assert asyncio.iscoroutinefunction(getattr(manager, name)), f"{name} is not async"

    async def scenario():
        assert await manager.acan_admit("model-a", BIG_VRAM, BIG_SYSRAM) == (True, "ok")
        # The optional topology argument survives the hop as well.
        assert await manager.acan_admit(
            "model-a", BIG_VRAM, BIG_SYSRAM, ["10.0.0.2:50052"]
        ) == (True, "ok")

        ok, port, _msg = await manager.astart_model("model-a", [], BIG_VRAM, BIG_SYSRAM)
        assert ok is True

        snapshot = await manager.asnapshot_model("model-a")
        assert snapshot["port"] == port
        assert await manager.asnapshot_model("model-b") is None

        assert await manager.aestimate_model_vram_gb("model-a") > 0.0
        assert (await manager.aresolve_model_path("model-a")).endswith("model-a.gguf")
        listing = await manager.aget_available_models(BIG_VRAM, BIG_SYSRAM)
        assert {entry["id"] for entry in listing["data"]} >= {"model-a", "model-b"}

        # reap_model drops a model whose engine has died...
        manager.running_models["model-a.gguf"]["process"]._exit(1)
        assert await manager.areap_model("model-a") is True
        assert await manager.areap_model("model-a") is False

        # ...and stop_model is the deliberate-unload path.
        ok, _port, _msg = await manager.astart_model("model-b", [], BIG_VRAM, BIG_SYSRAM)
        assert ok is True
        assert await manager.astop_model("model-b") is True
        assert await manager.astop_model("model-b") is False

        await manager.astop_all_models()

    asyncio.run(scenario())

    assert manager.running_models == {}
    assert manager._allocated_ports == set()


# ======================================================================================
# ROUND 3 — can_admit and start_model must agree (finding R3-7)
#
# The two used to be separate implementations that were only *supposed* to reach the same
# verdict, and they diverged in three measured ways. The coordinator's auto-deploy gate hit
# divergence 1 on every 2-second tick, and the code there hid it by suppressing the log line
# rather than fixing the disagreement. There is now one function, ``_plan_start_locked``,
# which can_admit runs in dry-run mode and start_model runs in commit mode.
# ======================================================================================

def _verdicts(manager, tag, vram, sysram, endpoints=None):
    """
    Asks both gates the same question and returns their two answers side by side.

    ``can_admit`` is called FIRST and ``start_model`` second, which is the real order of
    events: the coordinator gates on the prediction and then acts on it.
    """
    predicted_ok, predicted_reason = manager.can_admit(tag, vram, sysram, endpoints)
    actual_ok, _port, actual_message = manager.start_model(
        tag, endpoints or [], vram, sysram
    )
    return (predicted_ok, predicted_reason), (actual_ok, actual_message)


def test_can_admit_refuses_a_model_that_is_not_downloaded_yet(engine_env):
    """
    R3-7 DIVERGENCE 1 REGRESSION — the one that fired every two seconds in production.

    ``can_admit`` never called ``resolve_model_path``. ``estimate_model_vram_gb`` returns the
    pessimistic UNKNOWN_MODEL_ESTIMATE_GB for a tag it cannot resolve, so on any reasonable
    pool the admission arithmetic passed and the gate said yes — and then ``start_model``
    resolved the path, found nothing, and failed. The coordinator announced "Auto-Deploying"
    and silently failed, over and over, with ``if "absolute blob path" not in msg`` in the
    error branch to keep the log quiet.
    """
    manager = engine_env.manager

    predicted, actual = _verdicts(manager, "definitely-not-a-real-model", 24.0, 8.0)

    # Tags are canonicalised to their .gguf filename before the message is built.
    expected = inference_manager.model_not_on_disk_message("definitely-not-a-real-model.gguf")
    assert predicted == (False, expected)
    assert actual == (False, expected)

    # And the caller can recognise it without copying a substring out of this module.
    assert inference_manager.is_model_not_on_disk(predicted[1]) is True
    assert inference_manager.is_model_not_on_disk(
        inference_manager.NETWORK_MAXED_OUT_MESSAGE
    ) is False

    # Nothing was reserved on the way to that answer.
    assert manager._allocated_ports == set()
    assert manager._starting == {}
    assert engine_env.spawned == []


def test_can_admit_refuses_when_every_port_is_taken(engine_env):
    """
    R3-7 DIVERGENCE 2 REGRESSION.

    The port allocator was consulted only by ``start_model``. With the whole scan range held
    by other processes, ``can_admit`` returned (True, 'ok') and ``start_model`` then returned
    'No free inference port available'. Both now run the same allocator scan; only the
    claiming differs.
    """
    manager = engine_env.manager
    engine_env.add_model("model-a")

    engine_env.bound_ports.update(
        range(manager.base_llama_port,
              manager.base_llama_port + inference_manager.PORT_SCAN_RANGE)
    )

    predicted, actual = _verdicts(manager, "model-a", BIG_VRAM, BIG_SYSRAM)

    assert predicted == (False, inference_manager.NO_FREE_PORT_MESSAGE)
    assert actual == (False, inference_manager.NO_FREE_PORT_MESSAGE)

    # A dry run must not claim the port it finds, either — freeing one port and asking again
    # has to produce a fresh yes rather than a stale reservation.
    engine_env.bound_ports.discard(8081)
    assert manager.can_admit("model-a", BIG_VRAM, BIG_SYSRAM) == (True, "ok")
    assert manager.can_admit("model-a", BIG_VRAM, BIG_SYSRAM) == (True, "ok")
    assert manager._allocated_ports == set(), "a dry run reserved a port"


def test_a_topology_restart_that_cannot_fit_is_refused_without_killing_the_incumbent(engine_env, monkeypatch):
    """
    R3-7 DIVERGENCE 3 REGRESSION.

    ``can_admit`` reported anything in ``running_models`` as admissible regardless of the
    caller's RPC topology. ``start_model`` with a *changed* topology tore the incumbent down
    first — releasing its reservation — and only then re-ran admission, which could now fail
    against memory another model had taken. can_admit said yes; the user ended up with no
    model at all.

    Both halves are fixed here: the prediction accounts for the restart, and the restart is
    admitted BEFORE anything is destroyed, so a refusal leaves the running engine serving.
    """
    manager = engine_env.manager
    monkeypatch.setattr(manager, "estimate_model_vram_gb", lambda path: 4.0)
    engine_env.add_model("model-a")

    assert manager.start_model("model-a", ["10.0.0.2:50052"], 12.0, 0.0)[0] is True
    incumbent = manager.running_models["model-a.gguf"]["process"]

    # Same topology, tiny pool: it is already running, so nothing new is needed and both
    # gates say yes.
    assert manager.can_admit("model-a", 4.0, 0.0, ["10.0.0.2:50052"]) == (True, "ok")
    # Omitting the topology means "whatever it is using", which is the same answer.
    assert manager.can_admit("model-a", 4.0, 0.0) == (True, "ok")

    # A DIFFERENT topology on that same tiny pool means a rebuild, and a rebuild does not fit.
    predicted, actual = _verdicts(
        manager, "model-a", 4.0, 0.0, endpoints=["10.0.0.9:50052"]
    )
    assert predicted == (False, inference_manager.NETWORK_MAXED_OUT_MESSAGE)
    assert actual == (False, inference_manager.NETWORK_MAXED_OUT_MESSAGE)

    # THE ASSERTION THE FINDING IS ABOUT: the engine that was already serving is untouched.
    assert manager.running_models["model-a.gguf"]["process"] is incumbent
    assert incumbent.poll() is None
    assert incumbent.terminate_calls == 0
    assert manager.running_models["model-a.gguf"]["endpoints"] == ["10.0.0.2:50052"]

    # On a pool that does fit the rebuild, the restart goes ahead as normal.
    ok, _port, message = manager.start_model("model-a", ["10.0.0.9:50052"], 12.0, 0.0)
    assert (ok, message) == (True, inference_manager.STARTED_MESSAGE)
    assert incumbent.terminate_calls == 1
    assert manager.running_models["model-a.gguf"]["endpoints"] == ["10.0.0.9:50052"]


def test_repeated_topology_restarts_do_not_leak_ports(engine_env):
    """
    A restart now allocates the replacement's port BEFORE the incumbent has finished dying,
    because the incumbent's ``wait()`` deliberately happens outside the lock and it keeps its
    port until it is really gone. That is correct — reissuing a live engine's port is bug #5
    — but it means the new engine lands one slot higher for the duration of the changeover.

    The old port must come straight back afterwards. The coordinator's monitor loop restarts
    the active model every time the worker set changes, so a port that drifted upwards by one
    on every restart would walk out of the scan range on a flapping network.
    """
    manager = engine_env.manager
    engine_env.add_model("model-a")

    ports = []
    for index in range(6):
        ok, port, _message = manager.start_model(
            "model-a", [f"10.0.0.{index}:50052"], BIG_VRAM, BIG_SYSRAM
        )
        assert ok is True
        ports.append(port)

    # It ping-pongs between the two lowest ports rather than climbing.
    assert set(ports) <= {8081, 8082}, f"ports drifted upwards across restarts: {ports}"
    # Exactly one engine is left alive, holding exactly one port.
    assert len(manager._allocated_ports) == 1
    assert manager._terminating == []
    assert manager._zombies == []
    assert len(engine_env.bound_ports) == 1


@pytest.mark.parametrize(
    "scenario",
    [
        "fits",
        "pool_too_small",
        "not_downloaded",
        "already_running",
        "ports_exhausted",
    ],
)
def test_can_admit_predicts_start_model_across_every_outcome(engine_env, monkeypatch, scenario):
    """
    The property, stated directly: for any state of the manager, ``can_admit``'s verdict is
    ``start_model``'s verdict. This is the test that would catch a fourth divergence being
    introduced, rather than the three that are known.
    """
    manager = engine_env.manager
    monkeypatch.setattr(manager, "estimate_model_vram_gb", lambda path: 4.0)
    engine_env.add_model("subject")

    vram, sysram = 12.0, 0.0
    expected_success = True

    if scenario == "pool_too_small":
        vram = 3.0
        expected_success = False
    elif scenario == "not_downloaded":
        os.remove(str(engine_env.models_dir / "subject.gguf"))
        expected_success = False
    elif scenario == "already_running":
        assert manager.start_model("subject", [], vram, sysram)[0] is True
    elif scenario == "ports_exhausted":
        engine_env.bound_ports.update(
            range(manager.base_llama_port,
                  manager.base_llama_port + inference_manager.PORT_SCAN_RANGE)
        )
        expected_success = False

    predicted, actual = _verdicts(manager, "subject", vram, sysram)

    assert predicted[0] is expected_success
    assert actual[0] is expected_success
    assert predicted[0] is actual[0], (
        f"can_admit said {predicted} but start_model said {actual}"
    )
    if not expected_success:
        # A refusal must give the same reason, not merely the same yes/no.
        assert predicted[1] == actual[1]


# ======================================================================================
# ROUND 3 — no double counting between publication and reservation retirement (R3-8)
# ======================================================================================

def test_a_published_model_is_never_counted_twice(engine_env, monkeypatch):
    """
    FINDING R3-8 REGRESSION.

    ``start_model`` published ``running_models[model]`` in one critical section and only
    removed the matching ``_starting`` reservation in a *later* one. Between the two the
    model was counted twice by ``_committed_gb_locked`` — and the window was not
    instantaneous, it contained the "Engine is running and ready for inference." disk write.
    A concurrent admission therefore saw 41.00 GB committed against a 28.80 GB budget when
    the true figure was 20.50 GB, and refused a model that fitted with 8.30 GB to spare.

    The probe samples at exactly that log line, which is the widest point of the old window.
    """
    manager = engine_env.manager
    monkeypatch.setattr(manager, "estimate_model_vram_gb", lambda path: 4.0)
    engine_env.limits["engine_slots"] = 2   # the automatic count would fill the pool
    for tag in ("first", "second"):
        engine_env.add_model(tag)

    vram, sysram = 12.0, 0.0
    samples = {}

    real_log = inference_manager.log_coordinator

    def sampling_log(message):
        if "ready for inference" in message:
            # Inside the old double-counting window, on the very thread that created it.
            samples["committed"] = manager._committed_gb_locked()
            samples["starting"] = dict(manager._starting)
            samples["second_admissible"] = manager.can_admit("second", vram, sysram)
        return real_log(message)

    monkeypatch.setattr(inference_manager, "log_coordinator", sampling_log)

    assert manager.start_model("first", [], vram, sysram)[0] is True
    reserved = manager.running_models["first.gguf"]["reserved_gb"]

    assert samples, "the probe never ran — the log line it samples on has moved"
    # Counted exactly once: not 2 x reserved.
    assert samples["committed"] == pytest.approx(reserved)
    assert samples["starting"] == {}, "the reservation outlived the publication"
    # And the consequence the user would have seen: a model that fits is admitted.
    assert samples["second_admissible"] == (True, "ok")
    # The window is closed for good, not merely narrowed.
    assert manager._committed_gb_locked() == pytest.approx(reserved)


# ======================================================================================
# ROUND 3 — the state lock is not held across process.wait() (finding R3-10)
#
# The invariant documented in __init__ said "the state lock is NEVER held across a genuinely
# slow operation" while _terminate_entry_locked ran two 2-second process.wait() calls inside
# the critical section, and stop_all_models ran them for every engine back to back. Measured
# before the fix: 12.03 s of continuously-held _state_lock for three wedged engines, with a
# concurrent can_admit blocked 11.76 s — on the coordinator's event loop.
#
# These tests do not measure elapsed time, which would be flaky. They park a teardown INSIDE
# process.wait() with a gate and then require unrelated work to complete while it is parked.
# ======================================================================================

def test_stop_all_models_does_not_hold_the_state_lock_across_the_waits(engine_env, monkeypatch):
    """
    FINDING R3-10 REGRESSION, the headline case.

    Two engines that ignore terminate() are torn down while an unrelated ``can_admit`` and an
    unrelated ``start_model`` must still be served. Before the fix both would have queued
    behind up to 4 s per engine of ``process.wait()``; the assertion is not "it was fast" but
    "it finished at all while the teardown was demonstrably still inside its wait".
    """
    manager = engine_env.manager
    monkeypatch.setattr(manager, "estimate_model_vram_gb", lambda path: 1.0)
    for tag in ("stubborn-a", "stubborn-b", "newcomer"):
        engine_env.add_model(tag)

    engine_env.terminate_hangs = True
    for tag in ("stubborn-a.gguf", "stubborn-b.gguf"):
        assert manager.start_model(tag, [], 40.0, 0.0)[0] is True
    engine_env.terminate_hangs = False

    engine_env.wait_gate.arm()

    stopper = threading.Thread(target=manager.stop_all_models, name="stopper")
    stopper.start()

    # Deterministic proof that a teardown is parked inside process.wait() right now.
    engine_env.wait_gate.wait_for_arrivals(1)

    served = threading.Event()
    outcome = {}

    def unrelated_work():
        outcome["admit"] = manager.can_admit("newcomer", 40.0, 0.0)
        outcome["start"] = manager.start_model("newcomer", [], 40.0, 0.0)
        served.set()

    worker = threading.Thread(target=unrelated_work, name="unrelated")
    worker.start()

    # THE ASSERTION: this completes while the teardown is still inside wait(). With the lock
    # held across the wait it could not, and the test fails here instead of hanging.
    assert served.wait(timeout=10.0), (
        "_state_lock is being held across process.wait() — an unrelated admission and start "
        "could not be served while a teardown was parked in its wait"
    )
    assert outcome["admit"] == (True, "ok")
    assert outcome["start"][0] is True

    # The engines being torn down are still fully accounted for while they die: their memory
    # counts and their ports are still off the allocator. That is what makes dropping the
    # lock safe rather than merely fast.
    assert len(manager._terminating) >= 1
    assert manager._committed_gb_locked() > outcome["start"][1] * 0.0  # sanity: non-zero
    for handle in list(manager._terminating):
        assert handle["port"] in manager._allocated_ports

    engine_env.wait_gate.release()
    join_all([stopper, worker])

    # Everything settles exactly as it did before the split.
    assert "stubborn-a.gguf" not in manager.running_models
    assert "stubborn-b.gguf" not in manager.running_models
    assert manager._terminating == []
    assert manager._zombies == []
    for tag in ("stubborn-a.gguf", "stubborn-b.gguf"):
        assert 8081 <= manager.running_models.get(tag, {}).get("port", 8081)

    manager.stop_all_models()
    assert manager.running_models == {}
    assert manager._allocated_ports == set()
    assert manager._terminating == []


def test_reap_model_does_not_hold_the_state_lock_across_the_wait(engine_env):
    """
    The same guarantee for the single-model path the chat handler uses (finding R3-6): the
    coordinator reaps a crashed engine from a request handler, and that must not serialise
    the whole process behind a stubborn ``wait()``.
    """
    manager = engine_env.manager
    for tag in ("crashed", "other"):
        engine_env.add_model(tag)

    engine_env.terminate_hangs = True
    assert manager.start_model("crashed", [], BIG_VRAM, BIG_SYSRAM)[0] is True
    engine_env.terminate_hangs = False

    engine_env.wait_gate.arm()

    reaped = {}

    def reap():
        reaped["value"] = manager.reap_model("crashed")

    reaper = threading.Thread(target=reap, name="reaper")
    reaper.start()
    engine_env.wait_gate.wait_for_arrivals(1)

    # The entry is already invisible to callers even though the process is still dying...
    assert "crashed.gguf" not in manager.running_models
    # ...and an unrelated model can still be started while that happens.
    served = threading.Event()

    def start_other():
        manager.start_model("other", [], BIG_VRAM, BIG_SYSRAM)
        served.set()

    worker = threading.Thread(target=start_other, name="other")
    worker.start()
    assert served.wait(timeout=10.0), "reap_model held _state_lock across process.wait()"

    engine_env.wait_gate.release()
    join_all([reaper, worker])

    assert reaped["value"] is True
    assert manager._terminating == []
    assert "other.gguf" in manager.running_models


def test_memory_and_port_stay_booked_for_an_engine_that_is_being_torn_down(engine_env, monkeypatch):
    """
    The safety property that makes the lock split legal. An engine in ``_terminating`` has
    left ``running_models``, but for the seconds it takes to die it is still holding its
    memory and its TCP port, so both must keep counting. Dropping either one would trade the
    lock-hold bug for an over-commit bug or a reissued port.
    """
    manager = engine_env.manager
    monkeypatch.setattr(manager, "estimate_model_vram_gb", lambda path: 4.0)
    for tag in ("dying", "hopeful"):
        engine_env.add_model(tag)

    # A 9.0 GB pool is a 8.1 GB budget. "dying" reserves 4.5 GB of it (4.0 GB of weights plus
    # a 0.5 GB KV cache), which leaves 3.6 GB — not enough for another 4.0 GB model until the
    # dying engine has genuinely let go.
    engine_env.terminate_hangs = True
    assert manager.start_model("dying", [], 9.0, 0.0)[0] is True
    engine_env.terminate_hangs = False
    reserved = manager.running_models["dying.gguf"]["reserved_gb"]
    doomed_port = manager.running_models["dying.gguf"]["port"]

    engine_env.wait_gate.arm()
    stopper = threading.Thread(target=manager.stop_model, args=("dying",), name="stopper")
    stopper.start()
    engine_env.wait_gate.wait_for_arrivals(1)

    # Gone from running_models, still charged to the pool.
    assert "dying.gguf" not in manager.running_models
    assert manager._committed_gb_locked() == pytest.approx(reserved)
    # A model that only fits if the dying engine's memory is already free must be refused,
    # because that memory is NOT free yet.
    assert manager.can_admit("hopeful", 9.0, 0.0) == (
        False, inference_manager.NETWORK_MAXED_OUT_MESSAGE
    )
    # And its port is still off the allocator.
    assert doomed_port in manager._allocated_ports
    assert manager.start_model("hopeful", [], 40.0, 0.0)[1] != doomed_port

    engine_env.wait_gate.release()
    join_all([stopper])

    # Once it has really gone, everything comes back.
    assert manager._terminating == []
    assert manager._committed_gb_locked() == pytest.approx(
        manager.running_models["hopeful.gguf"]["reserved_gb"]
    )
    assert doomed_port not in manager._allocated_ports


# ======================================================================================
# ROUND 3 — the cancellation contract (finding R3-11)
# ======================================================================================

def test_a_cancelled_start_is_advertised_as_retryable(engine_env):
    """
    FINDING R3-11 REGRESSION — the contract half, which is this module's responsibility.

    A start cancelled by a concurrent teardown returns START_CANCELLED_MESSAGE. The chat
    handler did not recognise it and fell through to ``HTTPException(500)``, so a routine
    topology restart — which the monitor loop performs whenever the worker set changes —
    turned an in-flight user request into a server error. Nothing is actually wrong in that
    situation: the correct answer is to try again.

    The sentinel is a module constant and the classification is a module function, so the
    coordinator matches on the symbol rather than on a copied string.
    """
    manager = engine_env.manager
    engine_env.add_model("model-a")
    engine_env.download_gate.arm()

    result = {}

    def start():
        result["value"] = manager.start_model("model-a", [], BIG_VRAM, BIG_SYSRAM)

    starter = threading.Thread(target=start, name="starter")
    starter.start()
    engine_env.download_gate.wait_for_arrivals(1)

    manager.stop_all_models()

    engine_env.download_gate.release()
    join_all([starter])

    ok, port, message = result["value"]
    assert (ok, port) == (False, 0)
    assert message is inference_manager.START_CANCELLED_MESSAGE

    # THE CONTRACT: this is a "try again", not a failure to report to the user.
    assert inference_manager.is_retryable_start_failure(message) is True
    assert message in inference_manager.RETRYABLE_START_MESSAGES

    # ...and the genuinely terminal outcomes are NOT retryable, so a caller that loops on
    # is_retryable_start_failure cannot spin forever on a full pool or a missing model.
    for terminal in (
        inference_manager.NETWORK_MAXED_OUT_MESSAGE,
        inference_manager.NO_FREE_PORT_MESSAGE,
        inference_manager.model_not_on_disk_message("model-a"),
        inference_manager.STARTED_MESSAGE,
        inference_manager.ALREADY_RUNNING_MESSAGE,
        "Binary Missing",
    ):
        assert inference_manager.is_retryable_start_failure(terminal) is False

    # A handoff timeout is the other side of the same coin: nothing is wrong, ask again.
    assert inference_manager.is_retryable_start_failure(
        inference_manager.START_HANDOFF_TIMEOUT_MESSAGE
    ) is True
    # Non-strings (an exception object reaching the classifier) must not explode.
    assert inference_manager.is_retryable_start_failure(None) is False
    assert inference_manager.is_model_not_on_disk(None) is False


def test_start_model_returns_the_documented_sentinels(engine_env, monkeypatch):
    """
    The message half of ``start_model``'s return value is a control signal, so the exact
    strings are pinned here. Changing one without changing the coordinator would silently
    turn a graceful nudge into an HTTP 500.
    """
    manager = engine_env.manager
    engine_env.add_model("model-a")

    assert manager.start_model("model-a", [], BIG_VRAM, BIG_SYSRAM)[2] == \
        inference_manager.STARTED_MESSAGE
    assert manager.start_model("model-a", [], BIG_VRAM, BIG_SYSRAM)[2] == \
        inference_manager.ALREADY_RUNNING_MESSAGE

    monkeypatch.setattr(manager, "estimate_model_vram_gb", lambda path: 400.0)
    engine_env.add_model("model-b")
    assert manager.start_model("model-b", [], 8.0, 8.0)[2] == \
        inference_manager.NETWORK_MAXED_OUT_MESSAGE

    assert manager.start_model("model-c", [], BIG_VRAM, BIG_SYSRAM)[2] == \
        inference_manager.model_not_on_disk_message("model-c.gguf")

    # The literal values, spelled out, because other modules and operators' log greps
    # depend on them.
    assert inference_manager.STARTED_MESSAGE == "Started"
    assert inference_manager.ALREADY_RUNNING_MESSAGE == "Already running"
    assert inference_manager.NETWORK_MAXED_OUT_MESSAGE == "Network Maxed Out"
    assert inference_manager.NO_FREE_PORT_MESSAGE == "No free inference port available"
    assert inference_manager.START_CANCELLED_MESSAGE == "Start cancelled during shutdown"


# ======================================================================================
# ROUND 3 — consistent reads for the coordinator
# ======================================================================================

def test_snapshot_model_hands_back_a_coherent_private_copy(engine_env):
    """
    ``coordinator.py`` read ``manager.running_models.get(model_path, {})`` straight off the
    event loop and then made four separate decisions from the result. A teardown landing
    mid-block turned "the entry exists" into "the entry is gone", and the coordinator went on
    to proxy to a dead port and to size the prompt from default values instead of the
    engine's real ones. A snapshot is one coherent view taken under the lock.
    """
    manager = engine_env.manager
    engine_env.add_model("model-a")
    engine_env.limits["engine_slots"] = 3
    manager.start_model("model-a", ["10.0.0.2:50052"], BIG_VRAM, BIG_SYSRAM, queue_channels=3)

    snapshot = manager.snapshot_model("model-a")

    assert snapshot["port"] == 8081
    assert snapshot["dynamic_slots"] == 3
    assert snapshot["safe_context"] == manager.running_models["model-a.gguf"]["safe_context"]
    assert snapshot["endpoints"] == ["10.0.0.2:50052"]
    # The live Popen is shared on purpose — polling it is the point — but the dict is ours.
    assert snapshot["process"] is manager.running_models["model-a.gguf"]["process"]

    snapshot["port"] = 9999
    snapshot["endpoints"].append("10.0.0.3:50052")
    assert manager.running_models["model-a.gguf"]["port"] == 8081
    assert manager.running_models["model-a.gguf"]["endpoints"] == ["10.0.0.2:50052"]

    # A model that is not running is None, never an empty dict that reads as "no process,
    # no context, carry on".
    assert manager.snapshot_model("model-b") is None
    manager.stop_model("model-a")
    assert manager.snapshot_model("model-a") is None


# ======================================================================================
# Slots and context from the model's real cost (2.4)
#
# The numbers are Qwen3.6-35B-A3B UD-IQ3_S, read from the pinned file: 10 attention blocks
# of 40, 20 KiB of KV a token at f16 (10,880 B at q8_0), 62.8 MiB of recurrent state a slot,
# 13,676,723,168 bytes of which 417 MB is the token embedding and 11.57 GB routed experts.
# ======================================================================================

from engine import gguf as gguf_module
from engine.config import parse_engine_args

GIB = 1024 ** 3
QWEN_KV_F16 = 20_480
QWEN_STATE = 65_863_680


def _plan(**overrides):
    values = dict(kv_f16_bytes_per_token=QWEN_KV_F16, state_bytes_per_slot=QWEN_STATE, budget_gb=1.0,
                  native_context=262_144, requested_slots=2)
    values.update(overrides)
    return inference_manager.plan_slots_and_context(**values)


def test_the_floor_slot_costs_what_the_hand_arithmetic_says():
    """32,768 tokens at q8_0 plus one slot's recurrent state: 0.393 GiB, not the 1.7 GB of 2.3.7."""
    cost = (QWEN_STATE + 32768 * gguf_module.scale_kv_bytes(QWEN_KV_F16, "q8_0")) / GIB

    assert cost == pytest.approx(0.3934, abs=1e-3)


def test_slots_come_first_then_context():
    """Owner's rule: more slots first, then more context per slot, never a slot under 32,768."""
    previous_slots = 0
    for budget in (1.5, 1.86, 2.0, 2.4, 3.0, 4.0):
        plan = _plan(budget_gb=budget)
        assert plan.mode == "auto"
        assert plan.slots >= previous_slots, "more memory must never mean fewer slots"
        assert plan.context_per_slot >= inference_manager.SLOT_CONTEXT_FLOOR
        assert plan.kv_gb <= budget + 1e-9
        previous_slots = plan.slots

    assert _plan(budget_gb=1.5).slots == 3
    assert _plan(budget_gb=1.86).slots == 4
    assert _plan(budget_gb=2.4).slots == 6
    # Slots stop at AUTO_SLOTS_MAX; only then does the surplus become context.
    assert _plan(budget_gb=4.0).slots == 8
    assert _plan(budget_gb=8.0).context_per_slot > _plan(budget_gb=4.0).context_per_slot
    # With room to spare in the slots that exist, they grow before another slot is added.
    assert _plan(budget_gb=1.0).slots == 2 and _plan(budget_gb=1.0).context_per_slot > 32768


def test_the_automatic_choice_stops_at_eight_slots_and_never_goes_q4_0():
    plan = _plan(budget_gb=100.0)

    assert plan.slots == inference_manager.AUTO_SLOTS_MAX == 8
    assert plan.context_per_slot == inference_manager.AUTO_SLOT_CONTEXT_CAP
    assert plan.cache_type == "q8_0"


def test_the_model_native_context_limits_a_slot():
    plan = _plan(budget_gb=100.0, native_context=8192)

    assert plan.context_per_slot == 8192
    assert plan.slots == 8


def test_the_callers_slots_are_a_floor_not_a_cap():
    """2 normally, 6 after a traffic spike: the context gives way, not the slots."""
    tight = _plan(budget_gb=0.3, requested_slots=6)
    assert tight.slots == 6
    assert tight.context_per_slot >= inference_manager.MIN_CONTEXT_TOKENS
    assert tight.context_per_slot < inference_manager.SLOT_CONTEXT_FLOOR
    # and where the budget holds more, the budget wins
    assert _plan(budget_gb=2.4, requested_slots=2).slots == 6


def test_a_dense_model_with_no_recurrent_state_is_planned_the_same_way():
    """Qwen2.5-Coder-14B: 192 KiB a token at f16, no state. One slot at the floor costs 3.2 GiB."""
    plan = _plan(kv_f16_bytes_per_token=196_608, state_bytes_per_slot=0, budget_gb=5.0)

    assert plan.slots == 2                      # the floor of 1 fits once; the caller's 2 stands
    assert plan.context_per_slot == 25_600      # (5 GiB / 2) / 102 KiB, in steps of 1,024
    assert plan.kv_gb <= 5.0


def test_the_operators_context_alone_gives_floor_sized_slots():
    """TheWall's 65,536 is 2 x 32,768 today; that is what it still is."""
    for budget in (0.1, 1.86, 100.0):    # the budget does not matter: the operator fixed it
        plan = _plan(fixed_context=65_536, budget_gb=budget)
        assert (plan.slots, plan.context_per_slot, plan.total_context) == (2, 32_768, 65_536)
        assert plan.cache_type == "q8_0" and plan.mode == "context-fixed"

    # After a spike the coordinator asks for 6 lanes on the same 65,536
    spike = _plan(fixed_context=65_536, requested_slots=6)
    assert (spike.slots, spike.total_context) == (6, 65_536)


def test_the_operators_slots_alone_get_the_context_the_budget_holds():
    plan = _plan(fixed_slots=6, budget_gb=2.4)

    assert plan.slots == 6 and plan.mode == "slots-fixed"
    assert plan.context_per_slot == 32_768
    assert _plan(fixed_slots=6, budget_gb=1.0).context_per_slot < 32_768   # the operator chose 6; the context shrinks


def test_both_fixed_is_used_as_written():
    plan = _plan(fixed_slots=6, fixed_context=196_608)

    assert (plan.slots, plan.total_context, plan.context_per_slot, plan.mode) == (6, 196_608, 32_768, "fixed")


def test_q4_0_starts_at_131072_tokens_a_slot_not_on_the_total():
    """Eight users at 32K are not a 256K context: the cache stays q8_0."""
    assert _plan(fixed_context=262_144).cache_type == "q8_0"                 # 8 x 32,768
    assert _plan(fixed_context=262_144).slots == 8
    assert _plan(fixed_context=262_144, fixed_slots=2).cache_type == "q4_0"   # 2 x 131,072
    assert _plan(fixed_context=262_144, fixed_slots=2).context_per_slot == 131_072
    assert _plan(fixed_context=131_072, fixed_slots=2).cache_type == "q8_0"   # 2 x 65,536


# --- TheWall, with its real configuration -----------------------------------------------

THEWALL_EXTRA_ARGS = ("--n-cpu-moe 5 -ts 23,17 --threads 4 --threads-batch 4 --cpu-mask 0xc3 --cpu-strict 1 "
                      "--chat-template-kwargs '{\"enable_thinking\":false}'")
# /v1/cluster/status on 2026-10-26: total_vram_gb 16.0 (two 8 GB BC-160), total_sysram_gb 12.0
THEWALL_VRAM_GB, THEWALL_SYSRAM_GB = 16.0, 12.0
# /sys/class/drm mem_info_vram_* with the engine running 2 x 32,768: both cards 8,573,157,376
# bytes, used 7,048,101,888 and 6,058,741,760. The engine's own cache is 805 MiB of that.
THEWALL_FREE_BYTES = 2 * 8_573_157_376 - 7_048_101_888 - 6_058_741_760
THEWALL_ENGINE_CACHE_BYTES = 805 * 1024 * 1024

QWEN36_IQ3S = gguf_module.ModelShape(
    arch="qwen35moe",
    file_bytes=13_676_723_168,
    kv_layers=10,
    kv_f16_bytes_per_token=QWEN_KV_F16,
    state_bytes_per_sequence=QWEN_STATE,
    embedding_bytes=417_177_600,
    embedding_tied=False,
    expert_bytes_by_layer=tuple(
        (block, 314_572_800 if block in (34, 38, 39) else 287_309_824) for block in range(40)),
    has_tensor_table=True,
)
# What 2.3.7 computed for it on TheWall, by the formulas of that release: 1.2 x the file, and
# a cache priced on all 40 blocks at half of f16 for 65,536 tokens.
OLD_THEWALL_WEIGHTS_GB = 13_676_723_168 / GIB * 1.2
OLD_THEWALL_RESERVED_GB = OLD_THEWALL_WEIGHTS_GB + 65_536 * (4 * QWEN_KV_F16 / 2) / GIB


@pytest.fixture
def thewall(engine_env, monkeypatch):
    """The coordinator as TheWall runs it: Qwen3.6 IQ3_S, ENGINE_CONTEXT 65536, the real extra args."""
    monkeypatch.setattr(inference_manager.gguf, "model_shape", lambda path: QWEN36_IQ3S)
    monkeypatch.setattr(inference_manager.gguf, "kv_bytes_per_token_for_model",
                        lambda path: QWEN36_IQ3S.kv_f16_bytes_per_token)
    engine_env.limits.update(engine_context=65_536, shard_policy="auto",
                             engine_extra_args=parse_engine_args(THEWALL_EXTRA_ARGS))
    engine_env.add_model("Qwen3.6-35B-A3B-UD-IQ3_S")
    engine_env.tag = "Qwen3.6-35B-A3B-UD-IQ3_S"
    return engine_env


def _flag(cmd, name):
    return cmd[cmd.index(name) + 1]


def _thewall_start(env, **kwargs):
    ok, _port, message = env.manager.start_model(
        env.tag, ["127.0.0.1:50052"], THEWALL_VRAM_GB, THEWALL_SYSRAM_GB, **kwargs)
    assert ok is True, message
    return env.spawned[-1].cmd, next(iter(env.manager.running_models.values()))


def test_thewall_still_gets_two_slots_of_32768(thewall):
    """The proof that today's behaviour does not get worse: the same -c, -np and cache."""
    cmd, entry = _thewall_start(thewall)

    assert (_flag(cmd, "-c"), _flag(cmd, "-np")) == ("65536", "2")
    assert (_flag(cmd, "-ctk"), _flag(cmd, "-ctv")) == ("q8_0", "q8_0")
    assert (entry["safe_context"], entry["dynamic_slots"]) == (65_536, 2)
    # and everything the operator wrote in ENGINE_EXTRA_ARGS reaches the engine untouched
    assert cmd[cmd.index("--n-cpu-moe"):cmd.index("--n-cpu-moe") + 4] == ["--n-cpu-moe", "5", "-ts", "23,17"]
    assert _flag(cmd, "--chat-template-kwargs") == '{"enable_thinking":false}'
    assert _flag(cmd, "--cpu-mask") == "0xc3"


def test_thewall_is_admitted_from_real_bytes_and_reserves_less_than_before(thewall):
    manager = thewall.manager
    estimate = manager.estimate_model_vram_gb(thewall.tag)

    # 11.01 GiB of weights on the cards (file - token embedding - 5 expert blocks in RAM)
    # plus the runtime's buffers; 2.3.7 said 15.28 and sent it through the RAM spillover.
    assert estimate == pytest.approx(11_822_996_448 / GIB + inference_manager.RUNTIME_OVERHEAD_GB)
    assert estimate < OLD_THEWALL_WEIGHTS_GB
    assert estimate + inference_manager.MIN_KV_GB <= THEWALL_VRAM_GB * inference_manager.POOL_SAFETY_FRACTION

    assert manager.can_admit(thewall.tag, THEWALL_VRAM_GB, THEWALL_SYSRAM_GB) == (True, "ok")
    _cmd, entry = _thewall_start(thewall)
    # 0.79 GiB of cache for 2 x 32,768 plus two recurrent states, not 2.5 GiB
    assert entry["reserved_gb"] == pytest.approx(estimate + (65_536 * 10_880 + 2 * QWEN_STATE) / GIB)
    assert entry["reserved_gb"] < OLD_THEWALL_RESERVED_GB


def test_thewall_with_a_lan_volunteer_still_runs_on_its_own_cards(thewall):
    """
    The two halves of this change belong together. The shard planner now asks for
    required / 0.9 of raw VRAM; fed the old 15.28 GiB estimate that is 17.0 GiB against
    TheWall's 16, and a LAN volunteer would be pulled in (a restart across the network, for
    a model that fits). Fed the real 11.76 GiB it is 13.1 and TheWall stays on its own cards.
    """
    from engine.registry import WorkerRegistry

    registry = WorkerRegistry()
    for index, (ip, vram) in enumerate((("127.0.0.1", 16.0), ("192.168.1.50", 8.0))):
        registry.update_worker(worker_id=f"w{index}", ip=ip, port=50052, token=f"t{index}",
                               hardware_profile={"gpu": {"has_gpu": True, "vram_total_gb": vram}})

    estimate = thewall.manager.estimate_model_vram_gb(thewall.tag)

    assert registry.plan_endpoints(estimate) == ["127.0.0.1:50052"]
    assert registry.plan_endpoints(OLD_THEWALL_WEIGHTS_GB) == ["127.0.0.1:50052", "192.168.1.50:50052"]


def test_thewall_after_a_traffic_spike_keeps_its_six_lanes(thewall):
    cmd, entry = _thewall_start(thewall, queue_channels=6)

    assert (_flag(cmd, "-c"), _flag(cmd, "-np")) == ("65536", "6")
    assert entry["dynamic_slots"] == 6


def test_thewall_with_the_context_left_automatic_gets_more_slots(thewall):
    """ENGINE_CONTEXT removed: four slots of at least 32,768, inside what the cards really have."""
    thewall.limits["engine_context"] = 0

    cmd, entry = _thewall_start(thewall)

    slots = int(_flag(cmd, "-np"))
    assert slots == 4
    assert int(_flag(cmd, "-c")) // slots >= inference_manager.SLOT_CONTEXT_FLOOR
    assert (_flag(cmd, "-ctk"), _flag(cmd, "-ctv")) == ("q8_0", "q8_0")
    # The measured headroom: the cards had 4.04 GB free while the engine held 805 MiB of cache.
    cache_bytes = (entry["reserved_gb"] - thewall.manager.estimate_model_vram_gb(thewall.tag)) * GIB
    assert cache_bytes < THEWALL_FREE_BYTES + THEWALL_ENGINE_CACHE_BYTES


def test_thewall_with_six_operator_slots_is_used_as_written_and_fits_the_measured_headroom(thewall, monkeypatch):
    """The proposal for TheWall: ENGINE_SLOTS=6 with ENGINE_CONTEXT=196608 (6 x 32,768)."""
    warnings = []
    monkeypatch.setattr(inference_manager, "log_warning", warnings.append)
    thewall.limits.update(engine_slots=6, engine_context=196_608)

    cmd, entry = _thewall_start(thewall)

    assert (_flag(cmd, "-c"), _flag(cmd, "-np")) == ("196608", "6")
    assert warnings == [], "the proposal must not need the pool's margin"
    cache_bytes = (entry["reserved_gb"] - thewall.manager.estimate_model_vram_gb(thewall.tag)) * GIB
    assert cache_bytes < THEWALL_FREE_BYTES + THEWALL_ENGINE_CACHE_BYTES


def test_an_operator_setting_beyond_the_pool_is_used_but_warned_about(thewall, monkeypatch):
    warnings = []
    monkeypatch.setattr(inference_manager, "log_warning", warnings.append)
    thewall.limits.update(engine_slots=8, engine_context=1_048_576)

    cmd, _entry = _thewall_start(thewall)

    assert _flag(cmd, "-c") == "1048576" and _flag(cmd, "-np") == "8"
    assert len(warnings) == 1 and "ENGINE_SLOTS" in warnings[0]


def test_a_cache_slot_of_131072_tokens_is_the_only_way_to_q4_0(thewall):
    thewall.limits.update(engine_slots=2, engine_context=262_144)
    cmd, _entry = _thewall_start(thewall)
    assert (_flag(cmd, "-ctk"), _flag(cmd, "-ctv")) == ("q4_0", "q4_0")

    thewall.manager.stop_all_models()
    # 262,144 shared by eight slots used to flip the cache to q4_0 on the total alone
    thewall.limits.update(engine_slots=0, engine_context=262_144)
    cmd, _entry = _thewall_start(thewall)
    assert _flag(cmd, "-np") == "8" and _flag(cmd, "-ctk") == "q8_0"


def test_volunteers_gpus_give_the_experts_back_to_the_cards(thewall):
    """
    --n-cpu-moe is dropped when the engine runs across volunteers' GPUs
    (engine_args_without_placement), so those experts are on the cards again and the model
    is admitted as the heavier thing it then is.
    """
    manager = thewall.manager
    with manager._state_lock:
        local = manager._evaluate_admission_locked(thewall.tag, 16.0, 12.0, remote=False)
        remote = manager._evaluate_admission_locked(thewall.tag, 16.0, 12.0, remote=True)

    returned = 5 * 287_309_824 / GIB
    assert remote[2] - local[2] == pytest.approx(returned)


def test_the_host_part_of_a_model_must_fit_the_machines_ram(thewall):
    """1.7 GiB of embedding and expert blocks live in RAM whatever the cards hold."""
    manager = thewall.manager

    assert manager.can_admit(thewall.tag, 16.0, 1.0) == (False, "Network Maxed Out")
    # a pool that reports no RAM at all is not read as "no RAM", as admission has always done
    assert manager.can_admit(thewall.tag, 16.0, 0.0) == (True, "ok")


def test_a_model_spilling_out_of_the_cards_is_charged_its_host_part_too(thewall):
    """11 GB of cards cannot hold 11.8 GiB of weights: the spillover branch charges everything."""
    thewall.limits["engine_context"] = 0
    _cmd, entry = _thewall_start_pool(thewall, vram=11.0, sysram=12.0)

    host_gb = 1_853_726_720 / GIB
    assert entry["reserved_gb"] > 11_822_996_448 / GIB + inference_manager.RUNTIME_OVERHEAD_GB + host_gb


def _thewall_start_pool(env, vram, sysram):
    ok, _port, message = env.manager.start_model(env.tag, [], vram, sysram)
    assert ok is True, message
    return env.spawned[-1].cmd, next(iter(env.manager.running_models.values()))


def test_a_model_whose_header_cannot_be_read_is_sized_the_old_way(engine_env, monkeypatch):
    """Nothing to read means 1.2 x the file, as it always was; the slots still follow the budget."""
    monkeypatch.setattr(inference_manager.gguf, "model_shape", lambda path: None)
    engine_env.add_model("unreadable", size_bytes=4 * 1024 * 1024)

    expected = (4 * 1024 * 1024 / GIB) * inference_manager.VRAM_OVERHEAD_FACTOR
    assert engine_env.manager.estimate_model_vram_gb("unreadable") == pytest.approx(expected)
    ok, _port, message = engine_env.manager.start_model("unreadable", [], 16.0, 8.0)
    assert ok is True, message
    assert 2 <= engine_env.manager.running_models["unreadable.gguf"]["dynamic_slots"] <= 8


def test_cpu_moe_layers_reads_the_operators_flag():
    assert inference_manager.cpu_moe_layers(parse_engine_args(THEWALL_EXTRA_ARGS)) == 5
    assert inference_manager.cpu_moe_layers(["-ncmoe", "12"]) == 12
    assert inference_manager.cpu_moe_layers(["--n-cpu-moe", "lots"]) == 0
    assert inference_manager.cpu_moe_layers(["--n-cpu-moe"]) == 0
    assert inference_manager.cpu_moe_layers(None) == 0
    assert inference_manager.cpu_moe_layers(["-ot", "exps=CPU"]) == 0     # patterns are not interpreted


# --- The media card and the vision projector (2.6) ----------------------------------------------------

MMPROJ_BYTES = 899_283_680          # Qwen3.6's mmproj-F16.gguf
THEWALL_CARDS = [
    {"id": 0, "name": "AMD Radeon Graphics (RADV NAVI12)", "address": "0000:03:00.0", "vk_index": 1, "integrated": False},
    {"id": 1, "name": "AMD Radeon Graphics (RADV NAVI12)", "address": "0000:09:00.0", "vk_index": 2, "integrated": False},
    {"id": 2, "name": "AMD Radeon RX 580 Series (RADV POLARIS10)", "address": "0000:0c:00.0", "vk_index": 3,
     "integrated": False},
    {"id": 3, "name": "AMD Radeon Graphics (RADV RAPHAEL_MENDOCINO)", "address": "0000:15:00.0", "vk_index": 0,
     "integrated": True},
]
RX580 = "0000:0c:00.0"
VK = "GGML_VK_VISIBLE_DEVICES"
CUDA = "CUDA_VISIBLE_DEVICES"
# What 2.5.4 starts on TheWall with these settings, from a run of that release; the model path and port are swapped in.
THEWALL_2_5_4_COMMAND = [
    "-m", "<MODEL>", "--alias", "Qwen3.6-35B-A3B-UD-IQ3_S.gguf", "--host", "127.0.0.1", "--port", "<PORT>",
    "-c", "65536", "-np", "2", "--threads", "4", "--threads-batch", "4", "-ngl", "99", "-b", "2048", "-ub", "512",
    "-fa", "on", "-ctk", "q8_0", "-ctv", "q8_0", "--cache-reuse", "256", "--jinja",
    "--n-cpu-moe", "5", "-ts", "23,17", "--threads", "4", "--threads-batch", "4", "--cpu-mask", "0xc3",
    "--cpu-strict", "1", "--chat-template-kwargs", '{"enable_thinking":false}',
]


@pytest.fixture
def cards(thewall, monkeypatch):
    """TheWall's four cards for the hardware lookups, and a projector file of the real size."""
    import engine.hardware as hardware

    monkeypatch.setattr(hardware, "get_all_gpus", lambda: [dict(c) for c in THEWALL_CARDS])
    projector = str(thewall.models_dir.parent / "mmproj-F16.gguf")
    with open(projector, "wb") as handle:
        handle.truncate(MMPROJ_BYTES)
    thewall.projector = projector
    thewall.logged, thewall.errors = [], []
    monkeypatch.setattr(inference_manager, "log_warning", thewall.logged.append)
    monkeypatch.setattr(inference_manager, "log_error", thewall.errors.append)
    return thewall


def _start(env, endpoints=("127.0.0.1:50052",), **limits):
    env.limits.update(limits)
    ok, _port, message = env.manager.start_model(env.tag, list(endpoints), THEWALL_VRAM_GB, THEWALL_SYSRAM_GB)
    assert ok is True, message
    process = env.spawned[-1]
    return process.cmd, next(iter(env.manager.running_models.values())), process.env


def _normalised(cmd, entry):
    return [("<MODEL>" if a == entry["resolved_path"] else "<PORT>" if a == str(entry["port"]) else a) for a in cmd[1:]]


@pytest.mark.parametrize("allowed, cuda, vulkan", [
    ("-1", os.environ.get(CUDA), os.environ.get(VK)), ("0,1", "0,1", "1,2"), ("none", "", "")])
def test_with_media_and_vision_unset_the_engine_starts_exactly_as_before(cards, allowed, cuda, vulkan):
    cmd, entry, env = _start(cards, allowed_gpu_indices=allowed)

    assert _normalised(cmd, entry) == THEWALL_2_5_4_COMMAND
    assert (env.get(CUDA), env.get(VK)) == (cuda, vulkan)
    assert entry["mmproj_path"] == ""
    assert cards.logged == []


def test_the_target_config_gives_the_engine_two_cards_and_the_projector_the_third(cards):
    cmd, entry, env = _start(cards, allowed_gpu_indices="0,1", media_gpu=RX580, engine_mmproj=cards.projector)

    assert (env[CUDA], env[VK]) == ("0,1", "1,2,3")
    assert _flag(cmd, "--device") == "Vulkan0,Vulkan1"
    assert (_flag(cmd, "--mmproj"), _flag(cmd, "--image-max-tokens"), _flag(cmd, "--mmproj-device")) == (
        cards.projector, "1024", "Vulkan2")
    assert _flag(cmd, "-ts") == "23,17" and _flag(cmd, "--cache-reuse") == "256"      # -ts keeps its meaning
    assert entry["mmproj_path"] == cards.projector
    assert cards.logged == []


def test_the_media_card_alone_changes_the_devices_and_nothing_else(cards):
    cmd, _entry, env = _start(cards, allowed_gpu_indices="0,1", media_gpu=RX580)

    assert (env[CUDA], env[VK], _flag(cmd, "--device")) == ("0,1", "1,2,3", "Vulkan0,Vulkan1")
    assert "--mmproj" not in cmd and "--mmproj-device" not in cmd


def test_the_projector_alone_follows_the_first_engine_card(cards):
    cmd, _entry, env = _start(cards, allowed_gpu_indices="0,1", engine_mmproj=cards.projector,
                              engine_image_max_tokens=768)

    assert (env[CUDA], env[VK]) == ("0,1", "1,2")
    assert "--device" not in cmd and "--mmproj-device" not in cmd
    assert (_flag(cmd, "--mmproj"), _flag(cmd, "--image-max-tokens")) == (cards.projector, "768")


def test_a_home_directory_path_for_the_projector_is_expanded(cards, monkeypatch):
    monkeypatch.setenv("HOME", os.path.dirname(cards.projector))

    cmd, _entry, _env = _start(cards, engine_mmproj="~/mmproj-F16.gguf")

    assert _flag(cmd, "--mmproj") == cards.projector


def test_a_projector_file_that_is_not_there_starts_the_engine_without_vision_and_says_so(cards):
    cmd, entry, _env = _start(cards, engine_mmproj="/nowhere/mmproj.gguf")

    assert "--mmproj" not in cmd and entry["mmproj_path"] == ""
    assert any("ENGINE_MMPROJ names no file" in line for line in cards.logged)


def test_a_media_card_that_is_not_there_is_warned_about_and_left_out(cards):
    cmd, _entry, env = _start(cards, allowed_gpu_indices="0,1", media_gpu="0000:ff:00.0", engine_mmproj=cards.projector)

    assert (env[CUDA], env[VK]) == ("0,1", "1,2")
    assert "--device" not in cmd and "--mmproj-device" not in cmd and "--mmproj" in cmd
    assert any("MEDIA_GPU is not used" in line for line in cards.logged)


def test_a_media_card_that_is_the_only_allowed_card_is_the_engines_after_all(cards):
    cmd, _entry, env = _start(cards, allowed_gpu_indices="2", media_gpu=RX580)

    assert (env[CUDA], env[VK]) == ("2", "3")
    assert "--device" not in cmd
    assert any("MEDIA_GPU is not used" in line for line in cards.logged)


def test_cards_named_by_pci_address_are_resolved_for_the_engine(cards):
    _cmd, _entry, env = _start(cards, allowed_gpu_indices="0000:09:00.0,0000:03:00.0")

    assert (env[CUDA], env[VK]) == ("1,0", "2,1")


def test_an_allowed_list_that_names_no_card_here_hides_the_gpus_and_says_so(cards):
    _cmd, _entry, env = _start(cards, allowed_gpu_indices="0000:ff:00.0")

    assert (env[CUDA], env[VK]) == ("", "")
    assert any("names no card here" in line for line in cards.logged)


def test_no_limit_with_a_media_card_lists_the_engines_cards_so_vulkan_does_not_take_it(cards):
    cmd, _entry, env = _start(cards, allowed_gpu_indices="-1", media_gpu=RX580)

    assert (env[CUDA], env[VK], _flag(cmd, "--device")) == ("0,1", "1,2,3", "Vulkan0,Vulkan1")


SHARDED = ("127.0.0.1:50052", "192.168.1.50:50052")


def test_an_engine_sharded_over_volunteers_drops_the_media_card_and_passes_no_device(cards):
    cmd, entry, env = _start(cards, endpoints=SHARDED, allowed_gpu_indices="0,1", media_gpu=RX580,
                             engine_mmproj=cards.projector)

    assert (env[CUDA], env[VK]) == ("0,1", "1,2")                 # the RX 580 is not even visible
    assert "--device" not in cmd and "--mmproj-device" not in cmd
    assert _flag(cmd, "--mmproj") == cards.projector              # vision stays, on the first engine card
    assert "--rpc" in cmd and "-ts" not in cmd                    # the operator's card split is dropped too
    assert any("MEDIA_GPU is not used" in line and "volunteers" in line for line in cards.logged)


@pytest.fixture
def cuda_build(cards, dlne_root, monkeypatch):
    """The install's bin/ holds a CUDA build of llama.cpp: a CUDA backend library and no Vulkan one."""
    (dlne_root / "bin").mkdir(exist_ok=True)
    (dlne_root / "bin" / "libggml-cuda.so").write_bytes(b"")
    monkeypatch.setattr(inference_manager, "_media_gpu_warned", False)
    return cards


def test_on_a_cuda_build_the_media_card_is_ignored_with_one_warning(cuda_build):
    cards = cuda_build
    cmd, entry, env = _start(cards, allowed_gpu_indices="0,1", media_gpu=RX580, engine_mmproj=cards.projector)
    _start(cards, allowed_gpu_indices="0,1", media_gpu=RX580, engine_mmproj=cards.projector)

    assert (env[CUDA], env[VK]) == ("0,1", "1,2")                 # the RX 580 is not hidden or named
    assert "--device" not in cmd and "--mmproj-device" not in cmd  # no Vulkan name reaches a CUDA build
    assert _flag(cmd, "--mmproj") == cards.projector               # vision stays, on the first engine card
    assert [line for line in cards.logged if "MEDIA_GPU" in line] == [
        line for line in cards.logged if "not a Vulkan build" in line]
    assert len([line for line in cards.logged if "not a Vulkan build" in line]) == 1


def test_a_vulkan_build_keeps_the_media_card(cuda_build, dlne_root):
    (dlne_root / "bin" / "libggml-vulkan.so").write_bytes(b"")     # both backends: Vulkan is there
    cmd, _entry, _env = _start(cuda_build, allowed_gpu_indices="0,1", media_gpu=RX580, engine_mmproj=cuda_build.projector)

    assert _flag(cmd, "--device") == "Vulkan0,Vulkan1" and _flag(cmd, "--mmproj-device") == "Vulkan2"
    assert cuda_build.logged == []


def _reserved(env, **limits):
    _cmd, entry, _env = _start(env, **limits)
    return entry["reserved_gb"]


def test_the_sizer_charges_the_projector_only_when_it_is_not_on_the_media_card(cards):
    projector_gb = MMPROJ_BYTES / GIB + inference_manager.PROJECTOR_WORKING_GB

    def reserved(**limits):
        cards.manager.stop_all_models()
        return _reserved(cards, allowed_gpu_indices="0,1", **limits)

    base = reserved()
    assert reserved(engine_mmproj=cards.projector) == pytest.approx(base + projector_gb)
    assert reserved(media_gpu=RX580, engine_mmproj=cards.projector) == pytest.approx(base)
    assert reserved(media_gpu="0000:ff:00.0", engine_mmproj=cards.projector) == pytest.approx(base + projector_gb)
    assert reserved(media_gpu=RX580) == pytest.approx(base)
    assert reserved(engine_mmproj="/nowhere/mmproj.gguf") == pytest.approx(base)


def test_the_sizer_charges_the_pool_for_a_projector_on_a_sharded_engine(cards):
    """A sharded engine has no media card, so the projector sits in the pool, on this host or a volunteer's."""
    projector_gb = MMPROJ_BYTES / GIB + inference_manager.PROJECTOR_WORKING_GB
    cards.limits.update(allowed_gpu_indices="0,1", media_gpu=RX580, engine_mmproj=cards.projector)

    assert cards.manager._projector_charge_gb(remote=False) == 0.0
    assert cards.manager._projector_charge_gb(remote=True) == pytest.approx(projector_gb)

    cards.limits.update(engine_mmproj="")
    assert cards.manager._projector_charge_gb(remote=True) == 0.0


def test_admission_reads_the_same_charge_as_the_start(cards):
    """A dry run asks the same question: a projector that does not fit the pool is refused before any start."""
    cards.limits.update(allowed_gpu_indices="0,1", engine_mmproj=cards.projector)
    assert cards.manager.can_admit(cards.tag, THEWALL_VRAM_GB, THEWALL_SYSRAM_GB) == (True, "ok")

    # the pool 1.3 GiB short of the model plus the projector, but long enough for the model alone
    estimate = cards.manager.estimate_model_vram_gb(cards.tag)
    pool = (estimate + inference_manager.MIN_KV_GB + 0.2) / inference_manager.POOL_SAFETY_FRACTION
    assert cards.manager.can_admit(cards.tag, pool, 0.0) == (False, inference_manager.NETWORK_MAXED_OUT_MESSAGE)
    cards.limits.update(engine_mmproj="", media_gpu="")
    assert cards.manager.can_admit(cards.tag, pool, 0.0) == (True, "ok")
    cards.limits.update(engine_mmproj=cards.projector, media_gpu=RX580)
    assert cards.manager.can_admit(cards.tag, pool, 0.0) == (True, "ok")


def test_the_hardware_is_looked_at_before_the_state_lock_is_taken(cards, monkeypatch):
    """The first look can run vulkaninfo; the sizer, which holds the lock, must find the answer cached."""
    import engine.hardware as hardware

    order = []
    manager = cards.manager
    real_lock = manager._state_lock

    class Spy:
        def __enter__(self):
            order.append("lock")
            return real_lock.__enter__()

        def __exit__(self, *exc):
            return real_lock.__exit__(*exc)

    monkeypatch.setattr(manager, "_state_lock", Spy())
    monkeypatch.setattr(hardware, "get_all_gpus", lambda: order.append("hardware") or [dict(c) for c in THEWALL_CARDS])
    cards.limits.update(allowed_gpu_indices="0,1", media_gpu=RX580, engine_mmproj=cards.projector)

    manager.can_admit(cards.tag, THEWALL_VRAM_GB, THEWALL_SYSRAM_GB)

    assert order[0] == "hardware"


# --- A projector that kills the engine: vision off, coding on ------------------------------------------

def _die(cards, text):
    """The engine ends with ``text`` as its log since the start (the log is not in the fake process's hands)."""
    entry = next(iter(cards.manager.running_models.values()))
    with open(entry["log_path"], "a", encoding="utf-8") as handle:
        handle.write(text)
    cards.spawned[-1].returncode = 1
    return entry


LOADED = "srv  load_model: loaded multimodal model\nmain: server is listening on http://127.0.0.1:8081\n"


@pytest.mark.parametrize("log, failed", [
    ("llm_load_tensors: offloaded 41/41 layers\nfailed to load multimodal model\n", True),      # died loading
    ("llm_load_tensors: offloaded 41/41 layers\nclip_ctx: CLIP using Vulkan2 backend\n", True),     # began loading it
    ("llama_model_load: error loading model: out of memory\n", False),     # the weights, before the projector
    ("main: couldn't bind HTTP server socket, hostname: 127.0.0.1, port: 8081\n", False),
    ("clip_model_loader: model name: Qwen3.6\nllama_model_load: failed to allocate buffer\n", False),   # only sized it
    ("CLIP using Vulkan2 backend\nloaded multimodal model\n", False),     # loaded; died before it listened, not on it
    (LOADED + "encoding image slice...\n", True),                                              # died encoding
    (LOADED + "encoding image slice...\nimage slice encoded in 900 ms\nencoding image slice...\n", True),
    (LOADED + "encoding image slice...\nimage slice encoded in 900 ms\n", False),             # an unrelated death
    (LOADED, False),
])
def test_an_engine_that_died_on_the_projector_is_restarted_without_it(cards, log, failed):
    _start(cards, allowed_gpu_indices="0,1", media_gpu=RX580, engine_mmproj=cards.projector)
    _die(cards, log)

    cmd, entry, _env = _start(cards, allowed_gpu_indices="0,1", media_gpu=RX580, engine_mmproj=cards.projector)

    assert ("--mmproj" not in cmd) is failed
    assert (entry["mmproj_path"] == "") is failed
    assert len([e for e in cards.errors if "vision projector" in e]) == (1 if failed else 0)
    # the media card stays out of the model's way either way
    assert _flag(cmd, "--device") == "Vulkan0,Vulkan1"
    assert ("--mmproj-device" in cmd) is (not failed)


def test_the_coordinators_reap_after_a_crash_during_startup_turns_vision_off_too(cards):
    _start(cards, allowed_gpu_indices="0,1", media_gpu=RX580, engine_mmproj=cards.projector)
    _die(cards, "CLIP using Vulkan2 backend\nggml_vulkan: device lost\n")

    assert cards.manager.reap_model(cards.tag) is True
    assert cards.manager._mmproj_failed == cards.projector

    cmd, _entry, _env = _start(cards, allowed_gpu_indices="0,1", media_gpu=RX580, engine_mmproj=cards.projector)
    assert "--mmproj" not in cmd


def test_only_the_file_that_failed_is_left_out(cards):
    _start(cards, allowed_gpu_indices="0,1", engine_mmproj=cards.projector)
    _die(cards, "failed to load multimodal model\n")
    cards.manager.reap_model(cards.tag)
    other = os.path.join(os.path.dirname(cards.projector), "other-mmproj.gguf")
    with open(other, "wb") as handle:
        handle.write(b"x")

    cmd, _entry, _env = _start(cards, allowed_gpu_indices="0,1", engine_mmproj=other)

    assert _flag(cmd, "--mmproj") == other


def test_an_engine_still_running_is_never_judged_by_its_log(cards):
    _start(cards, allowed_gpu_indices="0,1", engine_mmproj=cards.projector)
    entry = next(iter(cards.manager.running_models.values()))
    with open(entry["log_path"], "a", encoding="utf-8") as handle:
        handle.write("encoding image slice...\n")

    cards.manager._check_projector_failure(entry)

    assert cards.manager._mmproj_failed == ""


def test_a_line_from_an_earlier_engine_on_the_same_port_is_not_this_engines(cards):
    log_dir = cards.models_dir.parent / "logs"
    log_dir.mkdir(exist_ok=True)
    for port in range(8081, 8091):                          # whichever port the start is given
        (log_dir / f"llama_server_{port}.log").write_text("encoding image slice...\n", encoding="utf-8")

    _start(cards, allowed_gpu_indices="0,1", engine_mmproj=cards.projector)
    entry = _die(cards, LOADED)
    cards.manager.reap_model(cards.tag)

    assert entry["log_offset"] > 0 and cards.manager._mmproj_failed == ""


# --- 2.6.1: no ffmpeg path, a projector only for the model it fits, a measured buffer -------------------

def _gguf_file(path, entries):
    from tests.test_gguf import build_gguf

    with open(path, "wb") as handle:
        handle.write(build_gguf(entries))
    return str(path)


def _model_header(cards, embedding, arch="qwen35moe"):
    from tests.test_gguf import T_STRING, T_UINT32, _string

    resolved = cards.manager.resolve_model_path(cards.tag)
    _gguf_file(resolved, [("general.architecture", T_STRING, _string(arch)),
                          (f"{arch}.embedding_length", T_UINT32, struct.pack("<I", embedding))])


def _projector_header(cards, projection_dim):
    from tests.test_gguf import T_STRING, T_UINT32, _string

    return _gguf_file(cards.projector, [("general.architecture", T_STRING, _string("clip")),
                                        ("clip.vision.projection_dim", T_UINT32, struct.pack("<I", projection_dim))])


def test_the_measured_projector_buffer_is_what_the_sizer_adds():
    # TheWall's RX 580, 2.6.0: 0.96 GiB in all for the 0.84 GiB file
    assert inference_manager.PROJECTOR_WORKING_GB == 0.15
    assert MMPROJ_BYTES / GIB + inference_manager.PROJECTOR_WORKING_GB == pytest.approx(0.99, abs=0.01)


def test_with_vision_on_the_engine_is_told_to_look_for_ffmpeg_in_an_empty_private_directory(cards, dlne_root):
    cmd, _entry, _env = _start(cards, allowed_gpu_indices="0,1", media_gpu=RX580, engine_mmproj=cards.projector)

    directory = _flag(cmd, "--video-ffmpeg-dir")
    assert directory == str(dlne_root / "no-ffmpeg") and os.listdir(directory) == []
    assert oct(os.stat(directory).st_mode & 0o777) == "0o700"


def test_without_vision_the_engine_is_given_no_ffmpeg_flag(cards):
    cmd, _entry, _env = _start(cards, allowed_gpu_indices="0,1", media_gpu=RX580)

    assert "--video-ffmpeg-dir" not in cmd


def test_if_the_empty_directory_cannot_be_made_or_is_not_empty_the_engine_starts_without_vision(cards, dlne_root):
    (dlne_root / "no-ffmpeg").mkdir()
    (dlne_root / "no-ffmpeg" / "ffmpeg").write_bytes(b"#!/bin/sh\n")

    cmd, entry, _env = _start(cards, allowed_gpu_indices="0,1", engine_mmproj=cards.projector)

    assert "--mmproj" not in cmd and "--video-ffmpeg-dir" not in cmd and entry["mmproj_path"] == ""
    assert any("private empty directory" in e for e in cards.errors)


def test_the_manager_says_whether_a_start_now_would_have_vision(cards):
    manager = cards.manager
    cards.limits.update(engine_mmproj="")
    assert manager.vision_configured() is False
    cards.limits.update(engine_mmproj="/nowhere/mmproj.gguf")
    assert manager.vision_configured() is False
    cards.limits.update(engine_mmproj=cards.projector)
    assert manager.vision_configured() is True
    manager._mmproj_failed = cards.projector                      # an engine died on it
    assert manager.vision_configured() is False


def test_a_projector_for_this_model_is_loaded(cards):
    _model_header(cards, 2048)
    _projector_header(cards, 2048)

    cmd, entry, _env = _start(cards, allowed_gpu_indices="0,1", engine_mmproj=cards.projector)

    assert _flag(cmd, "--mmproj") == cards.projector and entry["mmproj_path"] == cards.projector
    assert cards.logged == []


def test_a_projector_for_another_model_starts_that_model_without_vision_and_says_so_once(cards):
    _model_header(cards, 4096)
    _projector_header(cards, 2048)

    cmd, entry, _env = _start(cards, allowed_gpu_indices="0,1", engine_mmproj=cards.projector)
    cards.manager.stop_all_models()
    _start(cards, allowed_gpu_indices="0,1", engine_mmproj=cards.projector)

    assert "--mmproj" not in cmd and "--video-ffmpeg-dir" not in cmd and entry["mmproj_path"] == ""
    assert len([line for line in cards.logged if "is not for" in line]) == 1
    assert "width 2048" in cards.logged[0] and "width 4096" in cards.logged[0]


def test_a_mismatch_is_not_a_projector_failure_and_the_right_model_keeps_its_vision(cards):
    _model_header(cards, 4096)
    _projector_header(cards, 2048)
    _start(cards, allowed_gpu_indices="0,1", engine_mmproj=cards.projector)
    _die(cards, "llama_model_load: error loading model\n")
    cards.manager.reap_model(cards.tag)

    assert cards.manager._mmproj_failed == "" and cards.errors == []

    _model_header(cards, 2048)
    cmd, _entry, _env = _start(cards, allowed_gpu_indices="0,1", engine_mmproj=cards.projector)
    assert _flag(cmd, "--mmproj") == cards.projector


def test_the_projector_is_judged_again_when_the_files_change(cards):
    _model_header(cards, 4096)
    _projector_header(cards, 2048)
    cmd, _entry, _env = _start(cards, allowed_gpu_indices="0,1", engine_mmproj=cards.projector)
    assert "--mmproj" not in cmd
    cards.manager.stop_all_models()

    _projector_header(cards, 4096)
    os.utime(cards.projector, ns=(1, 2 * 10 ** 18))
    cmd, _entry, _env = _start(cards, allowed_gpu_indices="0,1", engine_mmproj=cards.projector)

    assert _flag(cmd, "--mmproj") == cards.projector


@pytest.mark.parametrize("model_header, projector_header", [
    (None, 2048), (2048, None), (None, None),                 # a header that cannot be read gives no answer: tried as before
])
def test_headers_that_cannot_be_read_leave_the_projector_to_the_failure_fallback(cards, model_header, projector_header):
    if model_header:
        _model_header(cards, model_header)
    if projector_header:
        _projector_header(cards, projector_header)

    cmd, _entry, _env = _start(cards, allowed_gpu_indices="0,1", engine_mmproj=cards.projector)

    assert _flag(cmd, "--mmproj") == cards.projector


def test_the_sizer_charges_nothing_for_a_projector_that_is_not_for_the_model(cards):
    _model_header(cards, 4096)
    _projector_header(cards, 2048)
    cards.limits.update(allowed_gpu_indices="0,1", engine_mmproj=cards.projector)
    resolved = cards.manager.resolve_model_path(cards.tag)

    assert cards.manager._projector_charge_gb(remote=False, model_path=resolved) == 0.0
    _model_header(cards, 2048)
    assert cards.manager._projector_charge_gb(remote=False, model_path=resolved) == pytest.approx(
        os.path.getsize(cards.projector) / GIB + inference_manager.PROJECTOR_WORKING_GB)


def test_an_engine_that_died_encoding_gets_its_vision_back_after_the_pause_but_one_that_died_loading_does_not(cards, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(inference_manager.time, "monotonic", lambda: clock[0])
    _start(cards, allowed_gpu_indices="0,1", engine_mmproj=cards.projector)
    _die(cards, LOADED + "encoding image slice...\n")
    cards.manager.reap_model(cards.tag)

    cmd, _entry, _env = _start(cards, allowed_gpu_indices="0,1", engine_mmproj=cards.projector)
    assert "--mmproj" not in cmd
    clock[0] += inference_manager.PROJECTOR_RETRY_SECONDS - 1
    cards.manager.stop_all_models()
    cmd, _entry, _env = _start(cards, allowed_gpu_indices="0,1", engine_mmproj=cards.projector)
    assert "--mmproj" not in cmd

    clock[0] += 2
    cards.manager.stop_all_models()
    cmd, _entry, _env = _start(cards, allowed_gpu_indices="0,1", engine_mmproj=cards.projector)
    assert _flag(cmd, "--mmproj") == cards.projector
    assert any("tried again" in line for line in cards.logged)

    _die(cards, "failed to load multimodal model\n")
    cards.manager.reap_model(cards.tag)
    clock[0] += 10 * inference_manager.PROJECTOR_RETRY_SECONDS
    cmd, _entry, _env = _start(cards, allowed_gpu_indices="0,1", engine_mmproj=cards.projector)
    assert "--mmproj" not in cmd


def test_the_log_is_read_in_bounded_chunks_from_the_engines_own_offset(tmp_path, monkeypatch):
    log = tmp_path / "llama_server_8081.log"
    old = b"encoding image slice...\n" * 10                       # an earlier engine's lines
    mine = (b"x" * 700_000) + b"CLIP using Vulkan2 backend\n" + b"y" * 900_000 + b"encoding image slice...\n"
    log.write_bytes(old + mine)
    reads = []
    real_open = open

    class Spy:
        def __init__(self, handle):
            self._handle = handle

        def read(self, size=-1):
            reads.append(size)
            return self._handle.read(size)

        def __getattr__(self, name):
            return getattr(self._handle, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._handle.close()

    monkeypatch.setattr("builtins.open", lambda *a, **k: Spy(real_open(*a, **k)))

    signs = inference_manager._projector_log_signs(str(log), len(old))

    assert signs["clip"] is True and signs["loaded"] is False and signs["listening"] is False
    assert signs["encoding"] == len(old) + len(mine) - len(b"encoding image slice...\n") and signs["encoded"] == -1
    assert reads and all(size == inference_manager._LOG_CHUNK for size in reads)       # never the whole file at once
    assert len(reads) >= 7


def test_a_marker_cut_in_two_by_a_chunk_boundary_is_still_found(tmp_path, monkeypatch):
    monkeypatch.setattr(inference_manager, "_LOG_CHUNK", 100)
    log = tmp_path / "log"
    for split in (1, 7, 15, 19):
        text = b"a" * (100 - split) + b"failed to load multimodal model" + b"b" * 50
        log.write_bytes(text)

        assert inference_manager._projector_log_signs(str(log), 0)["load_failed"] is True


def test_the_last_encoding_lines_are_ordered_across_chunks(tmp_path, monkeypatch):
    monkeypatch.setattr(inference_manager, "_LOG_CHUNK", 64)
    log = tmp_path / "log"
    log.write_bytes(b"encoding image slice...\n" + b"z" * 300 + b"image slice encoded in 1 ms\n" + b"z" * 200)
    signs = inference_manager._projector_log_signs(str(log), 0)
    assert signs["encoding"] < signs["encoded"]

    log.write_bytes(b"image slice encoded in 1 ms\n" + b"z" * 300 + b"encoding image slice...\n")
    signs = inference_manager._projector_log_signs(str(log), 0)
    assert signs["encoding"] > signs["encoded"]


def test_a_log_that_cannot_be_read_says_nothing(tmp_path):
    assert inference_manager._projector_log_signs(str(tmp_path / "missing"), 0) is None


# ======================================================================================
# Phone lanes (2.6)
# ======================================================================================

def test_without_phone_lanes_the_plan_is_what_it_was():
    plain = _plan(fixed_slots=2, fixed_context=65_536)
    zero = _plan(fixed_slots=2, fixed_context=65_536, phone_slots=0, phone_context=8192)

    assert zero == plain
    assert (plain.phone_slots, plain.phone_context, plain.pool_context) == (0, 0, plain.total_context)


def test_phone_lanes_are_extra_to_the_coding_slots_and_padded_to_256():
    # The coordinator advertises 109,226 / 2 = 54,613 a request; llama-server rounds each slot up to 54,784.
    plan = _plan(fixed_slots=2, fixed_context=109_226, phone_slots=2, phone_context=8192)

    assert (plan.slots, plan.context_per_slot, plan.total_context) == (2, 54_613, 109_226)
    assert (plan.phone_slots, plan.phone_context) == (2, 8192)
    assert plan.pool_context == 2 * 54_784 + 2 * 8192 == 125_952
    assert inference_manager.pad_context(54_613) == 54_784 and inference_manager.pad_context(8192) == 8192


def test_the_pool_is_paid_for_with_the_phone_lanes_in_it():
    token = gguf_module.scale_kv_bytes(QWEN_KV_F16, "q8_0")
    plain = _plan(fixed_slots=2, fixed_context=65_536)
    lanes = _plan(fixed_slots=2, fixed_context=65_536, phone_slots=2, phone_context=8192)

    assert lanes.kv_gb - plain.kv_gb == pytest.approx(2 * (QWEN_STATE + 8192 * token) / GIB)


def test_automatic_sizing_spends_the_phone_lanes_share_first():
    """The coding slots get what the budget has left once the phone lanes are paid for, never more than before."""
    token = gguf_module.scale_kv_bytes(QWEN_KV_F16, "q8_0")
    plain = _plan(budget_gb=3.0, fixed_slots=2)
    lanes = _plan(budget_gb=3.0, fixed_slots=2, phone_slots=2, phone_context=8192)

    assert lanes.context_per_slot < plain.context_per_slot
    assert lanes.kv_gb <= 3.0 + 1e-9
    assert lanes.slots == 2 and lanes.total_context == lanes.slots * lanes.context_per_slot
    assert lanes.pool_context == 2 * inference_manager.pad_context(lanes.context_per_slot) + 2 * 8192
    assert lanes.kv_gb == pytest.approx(
        (4 * QWEN_STATE + lanes.pool_context * token) / GIB)


def test_the_cache_type_follows_the_coding_slot_only():
    plan = _plan(fixed_slots=2, fixed_context=65_536, phone_slots=8, phone_context=65_536)
    assert plan.cache_type == "q8_0"


def test_a_phone_lane_context_is_padded_when_it_is_odd():
    assert _plan(fixed_slots=1, fixed_context=32_768, phone_slots=1, phone_context=4100).phone_context == 4352


def test_a_phone_lane_never_holds_more_than_the_coding_slot_does():
    """--kv-unified-per-slot caps every lane at the coding slot's size: the context recorded is the one that holds."""
    small = _plan(fixed_slots=4, fixed_context=16_384, phone_slots=2, phone_context=8192)

    assert small.context_per_slot == 4096 and small.phone_context == 4096
    assert small.pool_context == 4 * 4096 + 2 * 4096
    # an odd slot is capped at its padded size, which is what the engine pads it to
    odd = _plan(fixed_slots=3, fixed_context=3 * 5000, phone_slots=1, phone_context=65_536)
    assert odd.phone_context == 5120
    # roomy slots leave the operator's figure alone
    assert _plan(fixed_slots=2, fixed_context=65_536, phone_slots=2, phone_context=8192).phone_context == 8192


def test_a_small_coding_slot_starts_the_engine_with_the_clamped_phone_context(thewall):
    thewall.limits.update(engine_slots=4, engine_context=16_384, engine_phone_slots=2, engine_phone_context=8192)

    cmd, entry = _thewall_start(thewall)

    assert entry["phone_context"] == 4096
    assert _flag(cmd, "--kv-unified-per-slot") == "4096" and _flag(cmd, "-c") == str(4 * 4096 + 2 * 4096)


def test_thewall_without_phone_lanes_starts_with_no_lane_flags_and_the_same_numbers(thewall):
    cmd, entry = _thewall_start(thewall)

    assert "--kv-unified" not in cmd and "--kv-unified-per-slot" not in cmd and "--no-slots" not in cmd
    assert (_flag(cmd, "-c"), _flag(cmd, "-np")) == ("65536", "2")
    assert (entry["phone_slots"], entry["phone_context"]) == (0, 0)


def test_thewall_with_phone_lanes_adds_slots_a_pool_and_the_unified_cache(thewall):
    """ENGINE_SLOTS=2 ENGINE_PHONE_SLOTS=2, ENGINE_CONTEXT = 2 x 54,613 coding."""
    thewall.limits.update(engine_slots=2, engine_context=109_226, engine_phone_slots=2, engine_phone_context=8192)

    cmd, entry = _thewall_start(thewall)

    assert (_flag(cmd, "-np"), _flag(cmd, "-c")) == ("4", "125952")
    assert "--kv-unified" in cmd and _flag(cmd, "--kv-unified-per-slot") == "54784"
    assert "--no-slots" in cmd, "phone conversations sit in the lanes: /slots is closed"
    # what the coordinator sizes coding prompts against is the coding share alone
    assert (entry["safe_context"], entry["dynamic_slots"]) == (109_226, 2)
    assert (entry["phone_slots"], entry["phone_context"]) == (2, 8192)
    assert entry["safe_context"] // entry["dynamic_slots"] == 54_613
    # and everything the operator wrote in ENGINE_EXTRA_ARGS still reaches the engine
    assert _flag(cmd, "--n-cpu-moe") == "5"


def test_phone_lane_args_are_empty_without_lanes():
    assert inference_manager.phone_lane_args(0, 54_613) == []
    assert inference_manager.phone_lane_args(2, 54_613) == ["--kv-unified", "--kv-unified-per-slot", "54784", "--no-slots"]
