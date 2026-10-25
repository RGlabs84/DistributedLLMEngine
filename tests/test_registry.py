"""
Tests for ``engine.registry.WorkerRegistry`` — Contract C section 4a (bugs #6, #18, #30).

The registry's four total-getters are the *only* source of the memory and CPU numbers that
``InferenceManager`` divides up to decide whether a model may start and how large its KV
cache may be. Two defects made those numbers lie:

* only ``get_total_vram_gb`` skipped yielding nodes, so a machine handed back to its owner
  for Gaming Mode still contributed its RAM and cores to the budget (bug #18);
* none of the getters pruned first, so a node that had already timed out kept inflating the
  pool until some other code path happened to prune.

``get_active_rpc_endpoints`` had a third problem: it returned endpoints in dict-iteration
order, so an unchanged set of workers looked like a topology change whenever one of them
reconnected, which forced a needless full model restart (bug #6).
"""

import time

import pytest
from rich.text import Text

import engine.registry as registry_module
from engine.registry import WorkerRegistry, safe_log_text


def gpu_profile(vram_gb=8.0, ram_gb=32.0, cores=8, threads=16, yielding=False):
    """Builds the hardware profile shape a GPU worker actually sends in its heartbeat."""
    return {
        "gpu": {"has_gpu": True, "vram_total_gb": vram_gb, "name": "Test GPU", "load_percent": 5.0},
        "ram_gb": ram_gb,
        "cpu_cores": cores,
        "cpu_threads": threads,
        "is_yielding": yielding,
    }


def cpu_profile(ram_gb=16.0, cores=4, threads=8, yielding=False):
    """Hardware profile for a CPU-only node: no ``gpu`` sub-dictionary contribution."""
    return {
        "gpu": {"has_gpu": False, "vram_total_gb": 0.0},
        "ram_gb": ram_gb,
        "cpu_cores": cores,
        "cpu_threads": threads,
        "is_yielding": yielding,
    }


@pytest.fixture
def registry():
    """A fresh registry per test — the class holds all of its state on the instance."""
    return WorkerRegistry()


def age_worker(registry, worker_id, seconds):
    """
    Backdates a worker's last heartbeat.

    Rewriting ``last_heartbeat`` is how a timeout is simulated without sleeping for the full
    (20 s) window; ``update_worker`` always stamps ``time.time()``.
    """
    registry.workers[worker_id]["last_heartbeat"] = time.time() - seconds


# ======================================================================================
# Timeout window
# ======================================================================================

def test_timeout_tolerates_three_missed_heartbeats(registry):
    """
    Workers beat every 5 s. The old 15 s timeout tolerated only two missed beats, so a
    single network hiccup evicted a healthy node and triggered a model restart (bug #30).
    """
    assert registry.timeout >= 20


def test_worker_inside_the_timeout_window_survives_pruning(registry):
    """A node that missed two beats is still alive and must keep contributing."""
    registry.update_worker("alive", "10.0.0.2", 50052, "tok-a", gpu_profile())
    age_worker(registry, "alive", 12.0)

    registry.prune_dead_workers()

    assert "alive" in registry.workers


# ======================================================================================
# Pruning inside every getter (bug #18)
# ======================================================================================

@pytest.mark.parametrize(
    "getter, expected",
    [
        ("get_total_vram_gb", 0.0),
        ("get_total_sysram_gb", 0.0),
        ("get_total_cpu_cores", 0),
        ("get_total_cpu_threads", 0),
    ],
)
def test_every_total_prunes_dead_workers_first(registry, getter, expected):
    """
    A node that timed out seconds ago must not still be counted.

    Each getter is exercised on its own from a fresh registry, because a getter that only
    appears to work because a *different* getter pruned first is exactly the bug.
    """
    registry.update_worker("ghost", "10.0.0.9", 50052, "tok-ghost", gpu_profile())
    age_worker(registry, "ghost", 120.0)

    assert getattr(registry, getter)() == expected
    assert "ghost" not in registry.workers


def test_dead_worker_is_dropped_from_the_rpc_endpoint_list(registry):
    """A timed-out node's ``ip:port`` must not be handed to ``llama-server --rpc``."""
    registry.update_worker("ghost", "10.0.0.9", 50052, "tok-ghost", gpu_profile())
    age_worker(registry, "ghost", 120.0)

    assert registry.get_active_rpc_endpoints() == []


def test_live_workers_are_summed_across_every_total(registry):
    """The positive case: two healthy nodes contribute all four resources."""
    registry.update_worker("a", "10.0.0.2", 50052, "tok-a",
                           gpu_profile(vram_gb=8.0, ram_gb=32.0, cores=8, threads=16))
    registry.update_worker("b", "10.0.0.3", 50052, "tok-b",
                           gpu_profile(vram_gb=12.0, ram_gb=64.0, cores=16, threads=32))

    assert registry.get_total_vram_gb() == pytest.approx(20.0)
    assert registry.get_total_sysram_gb() == pytest.approx(96.0)
    assert registry.get_total_cpu_cores() == 24
    assert registry.get_total_cpu_threads() == 48


# ======================================================================================
# Yielding nodes excluded everywhere (bug #18)
# ======================================================================================

def test_yielding_worker_is_excluded_from_every_total(registry):
    """
    A yielding node has handed its machine back to its owner (Gaming Mode).

    Counting its RAM or cores would size a KV cache against memory that is not ours to
    spend, and the resulting llama-server would be killed by the OS mid-inference. Only VRAM
    used to be skipped; all four totals must skip it now.
    """
    registry.update_worker("busy", "10.0.0.2", 50052, "tok-busy",
                           gpu_profile(vram_gb=8.0, ram_gb=32.0, cores=8, threads=16))
    registry.update_worker("gaming", "10.0.0.3", 50052, "tok-gaming",
                           gpu_profile(vram_gb=24.0, ram_gb=128.0, cores=32, threads=64,
                                       yielding=True))

    assert registry.get_total_vram_gb() == pytest.approx(8.0)
    assert registry.get_total_sysram_gb() == pytest.approx(32.0)
    assert registry.get_total_cpu_cores() == 8
    assert registry.get_total_cpu_threads() == 16


def test_yielding_cpu_only_worker_is_excluded_from_ram_and_cpu(registry):
    """
    A CPU-only node contributes no VRAM, so the yield rule for it is *only* observable in
    the RAM and CPU totals — precisely the three getters that used to ignore it.
    """
    registry.update_worker("cpu-node", "10.0.0.4", 50052, "tok-cpu",
                           cpu_profile(ram_gb=16.0, cores=4, threads=8, yielding=True))

    assert registry.get_total_sysram_gb() == pytest.approx(0.0)
    assert registry.get_total_cpu_cores() == 0
    assert registry.get_total_cpu_threads() == 0


def test_worker_that_stops_yielding_is_counted_again(registry):
    """Yielding is a transient state, so the node has to come back into the pool cleanly."""
    registry.update_worker("node", "10.0.0.2", 50052, "tok",
                           gpu_profile(ram_gb=32.0, cores=8, threads=16, yielding=True))
    assert registry.get_total_sysram_gb() == pytest.approx(0.0)

    registry.update_worker("node", "10.0.0.2", 50052, "tok",
                           gpu_profile(ram_gb=32.0, cores=8, threads=16, yielding=False))
    assert registry.get_total_sysram_gb() == pytest.approx(32.0)
    assert registry.get_total_cpu_cores() == 8


def test_malformed_hardware_values_do_not_break_the_totals(registry):
    """
    Hardware profiles arrive over the network from other machines, so one malformed field
    must never raise out of a totals call and take capacity planning down for the whole
    cluster. The bad node contributes zero; the good node still contributes.
    """
    broken = gpu_profile()
    broken["ram_gb"] = "thirty-two"
    broken["cpu_cores"] = None

    registry.update_worker("broken", "10.0.0.5", 50052, "tok-broken", broken)
    registry.update_worker("good", "10.0.0.6", 50052, "tok-good",
                           gpu_profile(ram_gb=32.0, cores=8))

    assert registry.get_total_sysram_gb() == pytest.approx(32.0)
    assert registry.get_total_cpu_cores() == 8


def test_a_null_gpu_section_does_not_raise_anywhere(registry):
    """
    ITEM 1.1 REGRESSION: ``{"gpu": null}`` is a legitimate CPU-only profile.

    ``hardware.get("gpu", {})`` reads as if it defaults, and does not: a ``dict.get``
    default only applies when the key is *absent*, so a present-but-null ``gpu`` returns
    ``None`` and the chained ``.get`` on it raises ``AttributeError``. The profile comes
    from another machine over the network, so that turned one CPU-only (or merely
    malformed) heartbeat into an exception inside the totals — which every capacity
    decision in the cluster reads.
    """
    profile = {"gpu": None, "ram_gb": 32.0, "cpu_cores": 8, "cpu_threads": 16,
               "is_yielding": False}

    registry.update_worker("cpu-only", "10.0.0.7", 50052, "tok-cpu", profile)

    assert registry.get_total_vram_gb() == pytest.approx(0.0)
    assert registry.get_total_sysram_gb() == pytest.approx(32.0)
    assert registry.get_total_cpu_cores() == 8
    assert registry.get_total_cpu_threads() == 16
    # No GPU section at all means nothing to yield, so the node still serves RPC.
    assert registry.get_active_rpc_endpoints() == ["10.0.0.7:50052"]


@pytest.mark.parametrize("gpu_value", [None, "nvidia", [1, 2, 3], 42])
def test_a_wrongly_typed_gpu_section_is_treated_as_absent(registry, gpu_value):
    """
    The registry never crashes on a malformed ``gpu`` field, whatever shape it arrives in.

    ``validate_hardware_profile`` rejects most of these at the heartbeat boundary, but the
    registry is also written by other paths and must not depend on that check having run.
    """
    profile = {"gpu": gpu_value, "ram_gb": 16.0, "cpu_cores": 4, "cpu_threads": 8}

    registry.update_worker("odd", "10.0.0.8", 50052, "tok-odd", profile)

    assert registry.get_total_vram_gb() == pytest.approx(0.0)
    assert registry.get_total_sysram_gb() == pytest.approx(16.0)
    assert registry.get_active_rpc_endpoints() == ["10.0.0.8:50052"]


# ======================================================================================
# get_active_rpc_endpoints (bug #6)
# ======================================================================================

def test_rpc_endpoints_are_sorted_and_deduplicated(registry):
    """
    The returned list is compared as an ordered value against the endpoint set a model was
    launched with. Unstable ordering looked like a topology change and forced a full model
    restart; duplicates additionally made llama.cpp shard layers onto the same node twice.
    """
    registry.update_worker("w9", "10.0.0.9", 50052, "tok-9", gpu_profile())
    registry.update_worker("w2", "10.0.0.2", 50052, "tok-2", gpu_profile())
    registry.update_worker("w5", "10.0.0.5", 50052, "tok-5", gpu_profile())
    # A second worker_id reporting the same ip:port — e.g. a node that restarted with a new
    # id before its old entry timed out.
    registry.update_worker("w5-restarted", "10.0.0.5", 50052, "tok-5b", gpu_profile())

    endpoints = registry.get_active_rpc_endpoints()

    assert endpoints == ["10.0.0.2:50052", "10.0.0.5:50052", "10.0.0.9:50052"]
    assert len(endpoints) == len(set(endpoints))
    assert isinstance(endpoints, list)


def test_rpc_endpoint_order_is_stable_across_calls(registry):
    """
    Re-registering a worker rewrites its dict entry and moves it to the end of the insertion
    order. The endpoint list must not move with it.
    """
    for index in (7, 3, 5):
        registry.update_worker(f"w{index}", f"10.0.0.{index}", 50052, f"tok-{index}",
                               gpu_profile())

    first_call = registry.get_active_rpc_endpoints()

    registry.update_worker("w3", "10.0.0.3", 50052, "tok-3", gpu_profile())
    assert registry.get_active_rpc_endpoints() == first_call


def test_yielding_gpu_worker_is_dropped_from_the_endpoint_list(registry):
    """A GPU node in Gaming Mode must not receive sharded layers."""
    registry.update_worker("active", "10.0.0.2", 50052, "tok-a", gpu_profile())
    registry.update_worker("gaming", "10.0.0.3", 50052, "tok-g", gpu_profile(yielding=True))

    assert registry.get_active_rpc_endpoints() == ["10.0.0.2:50052"]


def test_yielding_cpu_worker_still_serves_rpc(registry):
    """
    Only *GPU* workers are removed when yielding. A CPU-only node's yield flag reflects
    local CPU load, and dropping it from the RPC set would needlessly reshard the model —
    this preserves the existing behaviour rather than tightening it.
    """
    registry.update_worker("cpu-node", "10.0.0.4", 50052, "tok-cpu", cpu_profile(yielding=True))

    assert registry.get_active_rpc_endpoints() == ["10.0.0.4:50052"]


# ======================================================================================
# Locking
# ======================================================================================

def test_getters_are_reentrant_with_pruning(registry):
    """
    Every public getter takes the registry lock and then calls ``prune_dead_workers``, which
    takes it again on the same thread. That only works because the lock is an ``RLock``; a
    plain ``Lock`` would deadlock here, so this test is the guard against someone
    "simplifying" it back.
    """
    registry.update_worker("node", "10.0.0.2", 50052, "tok", gpu_profile())

    # Would hang forever rather than fail if the lock were not re-entrant.
    assert registry.get_total_vram_gb() == pytest.approx(8.0)
    assert registry.get_active_rpc_endpoints() == ["10.0.0.2:50052"]


# ======================================================================================
# FINDING R3-1 — a hostile hardware profile must not raise out of update_worker
# ======================================================================================
#
# Every field below arrives over the network from another machine, and under
# ALLOW_UNSIGNED_WORKERS that machine is not authenticated. Two ways the ACK log line used to
# turn one of them into an unhandled exception — i.e. a remote HTTP 500 on POST
# /worker/heartbeat:
#
#   * `{load_pct:.1f}` on a non-number raised `ValueError: Unknown format code 'f' for object
#     of type 'str'`;
#   * a GPU `name` of `"[/dim]"` interpolated into a rich-markup string raised
#     `rich.errors.MarkupError` out of RichHandler.emit (logging's own error handling does not
#     absorb it).
#
# And it raised on the SUCCESS path, above `self.workers[worker_id] = {...}`, so the worker
# was never recorded at all: `last_log_time` stayed 0, the ten-second ACK throttle never
# engaged, and every retry from that node produced a fresh 500 forever.


@pytest.fixture
def captured_logs(monkeypatch):
    """
    Collects the messages ``update_worker``/``prune_dead_workers`` would emit.

    The functions are patched in ``engine.registry``'s own namespace because the module does
    ``from engine.logger import log_coordinator, ...``, which binds the function objects into
    its globals at import time.

    Capturing matters as well as not-raising: the production code catches exceptions around
    its log emission (a console message must never fail a registration), so a genuinely
    broken message would otherwise be swallowed and the test would pass against a regression.
    ``assert_markup_is_renderable`` closes that hole by parsing the captured text exactly the
    way ``RichHandler`` does.
    """
    messages = []

    def collect(msg):
        messages.append(msg)

    monkeypatch.setattr(registry_module, "log_coordinator", collect)
    monkeypatch.setattr(registry_module, "log_warning", collect)
    monkeypatch.setattr(registry_module, "log_error", collect)
    return messages


def assert_markup_is_renderable(messages):
    """
    Every captured message must survive rich's markup parser.

    ``engine.logger`` builds its ``RichHandler`` with ``markup=True``, so this is precisely
    what happens to each message inside ``emit``. ``Text.from_markup`` raises ``MarkupError``
    for a closing tag that matches nothing — the exact failure the audit reproduced with a GPU
    name of ``"[/"``.
    """
    for message in messages:
        Text.from_markup(message)


def rendered(message):
    """The text an operator actually sees, after rich has consumed the markup."""
    return Text.from_markup(message).plain


def test_safe_log_text_neutralises_markup_control_characters_and_length():
    """The jobs of the sanitiser, asserted directly rather than through a log line."""
    # A closing tag that matches nothing is the shape that raises MarkupError.
    assert "[" not in safe_log_text("[/dim]")
    assert "]" not in safe_log_text("[/dim]")
    Text.from_markup(f"[dim]{safe_log_text('[/dim]')}[/dim]")

    # THE CASE ``rich.markup.escape`` DOES NOT COVER, which is why this helper does not use
    # it: an opener with no closer passes an escaper untouched, and the surrounding template
    # then supplies the ``]``. Only removing the bracket from the fragment fixes that.
    Text.from_markup(f"[dim]prefix {safe_log_text('[/')} | suffix][/dim]")

    # Newlines and carriage returns forge extra log lines; they become spaces.
    assert "\n" not in safe_log_text("line one\nline two")
    assert "\r" not in safe_log_text("line one\r\nline two")

    # Long values are truncated, and truncation happens before escaping so it can never
    # split a backslash escape in half.
    assert len(safe_log_text("A" * 5000)) <= 96

    # A value whose __str__ explodes must not take the caller down with it.
    class Hostile:
        def __str__(self):
            raise RuntimeError("nope")

    assert safe_log_text(Hostile()) == "<unprintable>"


@pytest.mark.parametrize("load_percent", ["x", None, [1], {"a": 1}, "1.0"])
def test_a_non_numeric_gpu_load_does_not_raise(registry, captured_logs, load_percent):
    """
    FINDING R3-1 REGRESSION: ``{load_pct:.1f}`` against attacker-supplied text.

    ``load_percent`` appears in neither ``authn.HARDWARE_BOUNDS`` nor ``authn._BOOLEAN_FIELDS``,
    so ``validate_hardware_profile`` returns ``(True, "ok")`` and the coordinator passes the
    value straight through to this method. Measured pre-fix: HTTP 500 for each of these.
    """
    hardware = {"gpu": {"has_gpu": False, "load_percent": load_percent},
                "ram_gb": 16.0, "cpu_cores": 4}

    registry.update_worker("w", "1.2.3.4", 50052, "tok", hardware)

    assert "w" in registry.workers, "the worker must be recorded, not lost to a log failure"
    assert_markup_is_renderable(captured_logs)


def test_a_markup_injecting_gpu_name_does_not_raise(registry, captured_logs):
    """
    FINDING R3-1 REGRESSION: rich markup in a GPU name.

    ``"[/"`` raised ``MarkupError: closing tag '[/ | V.0.0GB | ...' doesn't match any open
    tag``, and the styled variant let a remote node repaint the operator's console.
    """
    for hostile_name in ("[/", "[/dim][bold red]ADMIN[/", "[bold red]owned"):
        registry.update_worker("gpu-node", "1.2.3.4", 50052, "tok", {
            "gpu": {"has_gpu": True, "vram_total_gb": 8.0, "name": hostile_name,
                    "load_percent": 1.0},
            "ram_gb": 16.0, "cpu_cores": 4,
        })
        # Force the next call past the ten-second ACK throttle so each name is really logged.
        registry.workers["gpu-node"]["last_log_time"] = 0

    assert "gpu-node" in registry.workers
    assert_markup_is_renderable(captured_logs)
    assert any("ADMIN" in m for m in captured_logs), "the name should still be shown, just inert"


def test_a_markup_injecting_worker_id_does_not_raise(registry, captured_logs):
    """
    Slicing an attacker-controlled id does not make it safe.

    ``short_id`` is ``worker_id.split('-')[0][:4].upper()``. A node calling itself ``"[/d]-x"``
    yields the four characters ``"[/D]"``, which rich parses as a closing tag matching nothing.
    """
    registry.update_worker("[/d]-x", "1.2.3.4", 50052, "tok", cpu_profile())

    assert "[/d]-x" in registry.workers
    assert_markup_is_renderable(captured_logs)


def test_a_markup_injecting_ip_does_not_raise(registry, captured_logs):
    """
    The address is worker-influenced too.

    Under ``TRUST_PROXY_HEADERS`` the coordinator's ``get_client_ip`` returns a token taken
    from a request header, and it is this value that is stored and logged.
    """
    registry.update_worker("node", "[/dim]10.0.0.2", 50052, "tok", cpu_profile())
    age_worker(registry, "node", 999.0)
    registry.prune_dead_workers()

    assert registry.workers == {}
    assert_markup_is_renderable(captured_logs)


def test_a_hostile_heartbeat_does_not_disable_the_ack_throttle(registry, captured_logs):
    """
    FINDING R3-1, the self-sustaining part.

    The old failure happened *before* the record was stored, so the node was never registered,
    ``last_log_time`` was 0 on the next attempt, and the ten-second throttle could never
    engage — every single repeat took the same failing branch. Registration now happens first,
    so a hostile profile is recorded exactly like any other and its ACK is throttled normally.
    """
    hardware = {"gpu": {"has_gpu": True, "vram_total_gb": 8.0, "name": "[/", "load_percent": "x"},
                "ram_gb": 16.0, "cpu_cores": 4}

    registry.update_worker("hostile", "1.2.3.4", 50052, "tok", hardware)
    assert registry.workers["hostile"]["last_log_time"] > 0

    logged_after_first = len(captured_logs)
    for _ in range(5):
        registry.update_worker("hostile", "1.2.3.4", 50052, "tok", hardware)

    assert len(captured_logs) == logged_after_first, "the ACK throttle must engage"
    assert_markup_is_renderable(captured_logs)


def test_a_lower_case_gpu_name_cannot_swallow_the_whole_ack_line(registry, captured_logs):
    """
    The other half of R3-1 in this line, found while auditing it: sanitising the *values* is
    not enough when the bracket is ours.

    rich's tag pattern is ``\\[[a-z#/@][^[]*?]``, so a GPU name starting with a lower-case
    letter turns the ACK line's own ``[name | V.8.0GB | ... ]`` group into something rich
    reads as a style tag. It does not raise — it silently *swallows the group*, and the
    operator's console shows an ACK with the GPU, VRAM, RAM, core count, load and yielding
    state all missing. A node can hide its own hardware report just by lower-casing its name.
    Measured on the pre-fix template: ``Text.from_markup(...).plain`` ended at the IP address.
    """
    registry.update_worker("node", "1.2.3.4", 50052, "tok", {
        "gpu": {"has_gpu": True, "vram_total_gb": 8.0, "load_percent": 1.0,
                "name": "nvidia geforce rtx"},
        "ram_gb": 16.0, "cpu_cores": 4,
    })

    ack = [m for m in captured_logs if "Heartbeat ACK" in m]
    assert ack, "no ACK line was produced"
    visible = rendered(ack[0])
    assert "nvidia geforce rtx" in visible
    assert "V.8.0GB" in visible
    assert "Yielding: False" in visible


def test_a_non_dict_hardware_profile_is_normalised(registry, captured_logs):
    """
    A profile that is not a dictionary must not poison the totals.

    Every getter does ``hardware.get(...)``, so storing a list or a string would make the
    *next* ``get_total_vram_gb()`` raise ``AttributeError`` from a completely unrelated call
    site. It is normalised to ``{}`` on the way in instead.
    """
    registry.update_worker("weird", "1.2.3.4", 50052, "tok", ["not", "a", "dict"])

    assert registry.workers["weird"]["hardware"] == {}
    assert registry.get_total_vram_gb() == 0.0
    assert registry.get_total_sysram_gb() == 0.0
    assert registry.get_total_cpu_cores() == 0
    assert_markup_is_renderable(captured_logs)


def test_a_string_vram_value_is_coerced_rather_than_interpolated_raw(registry, captured_logs):
    """
    Numbers are coerced once, on the way in, so nothing downstream is ever handed text.

    This is the same class of defect as ``load_percent`` — it simply did not happen to hit a
    format spec — and coercing here also keeps the stored-vs-logged values consistent.
    """
    registry.update_worker("w", "1.2.3.4", 50052, "tok", {
        "gpu": {"has_gpu": True, "vram_total_gb": "not-a-number"},
        "ram_gb": "also-not", "cpu_cores": "nope",
    })

    assert "w" in registry.workers
    assert registry.get_total_vram_gb() == 0.0
    assert_markup_is_renderable(captured_logs)
