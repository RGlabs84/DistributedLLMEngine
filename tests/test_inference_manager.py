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

    monkeypatch.setattr(inference_manager, "get_base_dir", lambda: str(dlne_root))
    monkeypatch.setattr(inference_manager, "get_worker_limits", make_worker_limits)

    # Pin what a token of KV cache costs. Real sizing reads it out of the model's GGUF
    # header, and the models here are empty stub files, so every test would otherwise be
    # priced at the pessimistic fallback — which means changing that fallback silently
    # re-tunes every pool-arithmetic assertion in this file. The tests below are about
    # admission and double-counting, not about what a particular architecture costs, so
    # they get one fixed price and state it.
    monkeypatch.setattr(inference_manager.gguf, "kv_bytes_per_token_for_model",
                        lambda path: TEST_KV_BYTES_PER_TOKEN)

    env = EngineEnv(None, models_dir, bound_ports, spawned)

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
    # The cache is stored q8_0 below 131,072 tokens of context, which halves its cost
    # (quartered with q4_0 above that); this model's context is far below that line.
    assert entry["safe_context"] < 131072
    expected_reserved = weights_gb + entry["safe_context"] * (TEST_KV_GB_PER_TOKEN / 2.0)
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
    ``safe_context`` is ``max(MIN_CONTEXT_TOKENS, min(json_max_context, hardware_max))``.

    A generous pool must not push the context past what the model actually supports (that
    reserves memory nobody can use and locks other models out), and it must never drop below
    the absolute floor, under which an instruct model cannot fit a system prompt plus a reply.
    """
    manager = engine_env.manager
    engine_env.add_model("model-a")
    monkeypatch.setattr(manager, "estimate_model_vram_gb", lambda path: 1.0)
    # Declare 8192 per slot explicitly; the built-in default is 32,768 now.
    (dlne_root / "models" / "models.json").write_text(
        '[{"id": "model-a", "max_context": 8192}]', encoding="utf-8"
    )
    slots = 2   # start_model's default queue_channels: llama-server splits -c across them
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
    assert manager.running_models["model-a.gguf"]["dynamic_slots"] == 6
    assert "-np" in engine_env.spawned[0].cmd
    assert engine_env.spawned[0].cmd[engine_env.spawned[0].cmd.index("-np") + 1] == "6"


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
