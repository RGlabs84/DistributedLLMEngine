"""
Tests for the 1.1.0 WAN-sharding work.

Every case here corresponds to a failure that was diagnosed on the live cluster rather than
imagined, and several of them assert the *absence* of something — a message that must not be
produced, a flag that must not be passed — because that is what the bugs actually were.
"""

import os
import types

import httpx
import pytest

from engine import benchmark, wireguard
from engine import coordinator
from engine import inference_manager
from engine.config import get_worker_limits, set_config_values
from engine.registry import WorkerRegistry


# --------------------------------------------------------------------------------------
# Phase A — the tunnel MTU
# --------------------------------------------------------------------------------------

@pytest.fixture
def sandbox(isolated_config, dlne_root, monkeypatch):
    """
    A throwaway install root that *every* module under test actually writes into.

    ``dlne_root`` on its own only makes a directory — it redirects nothing. Each of these
    modules did ``from engine.config import get_base_dir`` at import time, so the function
    object is bound in its own globals and patching ``engine.config`` alone leaves them
    writing next to the developer's real installation. This was not hypothetical: an
    earlier draft of this file generated WireGuard configs straight into the source tree.
    """
    for module in (wireguard, benchmark, inference_manager):
        monkeypatch.setattr(module, "get_base_dir", lambda: str(dlne_root))
    (dlne_root / "wg_configs").mkdir(exist_ok=True)
    return dlne_root


@pytest.fixture
def wg_sandbox(sandbox, monkeypatch):
    """A WireGuardOrchestrator with deterministic keys, writing only into the sandbox."""
    pairs = []

    def fake_keypair(self):
        index = len(pairs) + 1
        pair = (f"PRIV{index:040d}=", f"PUB{index:041d}=")
        pairs.append(pair)
        return pair

    def fake_wg_cmd(self, *args, stdin_text=None):
        # The suite must run without the wg binary present.
        if args[:1] == ("pubkey",):
            private = (stdin_text or "").strip()
            return next((pub for priv, pub in pairs if priv == private), None)
        if args[:2] == ("show", "interfaces"):
            return ""
        return None

    monkeypatch.setattr(wireguard.WireGuardOrchestrator, "generate_keypair", fake_keypair)
    monkeypatch.setattr(wireguard.WireGuardOrchestrator, "_run_wg_cmd", fake_wg_cmd)
    return wireguard.WireGuardOrchestrator()


def _conf_text(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def test_every_generated_tunnel_config_sets_an_mtu(wg_sandbox, monkeypatch):
    """
    The bug in one assertion.

    A WireGuard interface with no MTU passes small packets and silently drops large ones
    when the path cannot carry them and the ICMP notice is filtered. Handshakes, heartbeats
    and /health are all small; an inference response is not. Every writer must set it.
    """
    monkeypatch.setattr(wireguard, "tunnel_mtu", lambda: 1380)
    orchestrator = wg_sandbox

    # setup_coordinator_server returns the hub's public key, not a path — the file it
    # writes is the orchestrator's own coordinator_conf_path.
    hub_pubkey = orchestrator.setup_coordinator_server(listen_port=51820)
    written = [
        orchestrator.coordinator_conf_path,
        orchestrator.generate_worker_invite("1.2.3.4", hub_pubkey, port=51820),
        orchestrator.write_member_conf("PRIV1", 4, hub_pubkey, "1.2.3.4:51820"),
    ]

    for path in written:
        text = _conf_text(path)
        assert "MTU = 1380" in text, f"{os.path.basename(path)} sets no MTU"
        # Inside [Interface], not after a [Peer] header where WireGuard would read it as a
        # peer setting and refuse the file outright.
        head = text.split("[Peer]")[0]
        assert "MTU = 1380" in head, f"{os.path.basename(path)} puts MTU in the wrong section"


def test_a_configured_mtu_overrides_the_default(sandbox):
    """An operator on a path that needs something lower sets WG_MTU rather than editing files."""
    assert set_config_values({"WG_MTU": "1280"})
    assert get_worker_limits()["wg_mtu"] == 1280


@pytest.mark.parametrize("bad", ["0", "9000", "1200", "banana", "-1"])
def test_an_impossible_mtu_keeps_the_default(sandbox, bad):
    """
    A typo here would break the tunnel it exists to fix, so out-of-range values are refused.

    Below 1280 is not a legal IPv6 link MTU; above 1500 cannot cross an ordinary Ethernet
    hop. Neither is a deployment anyone has.
    """
    assert set_config_values({"WG_MTU": bad})
    assert get_worker_limits()["wg_mtu"] == 1380


def test_a_config_written_before_1_1_0_is_repaired_in_place(tmp_path):
    """
    Existing nodes are the whole point: tunnel configs are written once, at enrollment.

    Without this the fix would reach every new node and no node that already exists.
    """
    conf = tmp_path / "dlne_tunnel.conf"
    conf.write_text(
        "[Interface]\nPrivateKey = abc=\nAddress = 10.8.0.4/24\n\n"
        "[Peer]\nPublicKey = def=\nEndpoint = 1.2.3.4:51820\n",
        encoding="utf-8",
    )

    assert wireguard.repair_missing_mtu(str(conf)) is True
    text = conf.read_text(encoding="utf-8")
    assert f"MTU = {wireguard.tunnel_mtu()}" in text
    # Immediately after Address, inside [Interface] — not swept into the [Peer] block.
    assert text.index("MTU =") < text.index("[Peer]")
    assert wireguard.configured_tunnel_mtu(str(conf)) == wireguard.tunnel_mtu()


def test_a_hand_tuned_mtu_is_never_overwritten(tmp_path):
    """An operator who chose a value for their own path has made a decision. It stands."""
    conf = tmp_path / "dlne_tunnel.conf"
    conf.write_text(
        "[Interface]\nPrivateKey = abc=\nAddress = 10.8.0.4/24\nMTU = 1300\n",
        encoding="utf-8",
    )

    assert wireguard.repair_missing_mtu(str(conf)) is False
    assert wireguard.configured_tunnel_mtu(str(conf)) == 1300
    assert wireguard.warn_if_tunnel_mtu_unset(str(conf)) is None


def test_the_mtu_warning_is_silent_when_there_is_nothing_to_say(tmp_path):
    """A diagnostic that fires on a node with no tunnel is noise, not a diagnostic."""
    assert wireguard.warn_if_tunnel_mtu_unset(str(tmp_path / "absent.conf")) is None


def test_the_mtu_warning_names_the_problem_when_there_is_one(tmp_path):
    conf = tmp_path / "dlne_tunnel.conf"
    conf.write_text("[Interface]\nPrivateKey = abc=\nAddress = 10.8.0.4/24\n", encoding="utf-8")

    message = wireguard.warn_if_tunnel_mtu_unset(str(conf))
    assert message and "MTU" in message


def test_the_elevated_activation_script_sets_both_category_and_mtu():
    """
    One elevation, both settings.

    The adapter only exists for the moment the tunnel is being brought up, and DLNE has no
    elevation at run time — so anything not applied here cannot be applied at all without
    asking the user for a UAC prompt they have no reason to expect.
    """
    from engine import setup_core

    script = setup_core._windows_tunnel_activation_script(r"C:\Program Files\DLNE\dlne_tunnel.conf")
    assert "Set-NetConnectionProfile" in script
    assert "-NetworkCategory Private" in script
    assert "Set-NetIPInterface" in script
    assert f"-NlMtuBytes {wireguard.tunnel_mtu()}" in script
    # Both address families: an oversized IPv6 MTU black-holes exactly as thoroughly.
    assert "IPv4" in script and "IPv6" in script
    # $profile is a PowerShell automatic variable and assigning to it inside an elevated
    # shell is its own bug; this was fixed once and must not come back.
    assert "$profile" not in script


# --------------------------------------------------------------------------------------
# Phase A2 — the 60-second walls, and the message that invented a cause
# --------------------------------------------------------------------------------------

def test_the_engine_timeout_is_split_and_generous_on_read():
    """
    A single scalar timeout cannot be right for both connect and read.

    Connect is to a loopback port and should fail in seconds. Read has to survive an engine
    still pushing weights to a remote backend, which is minutes and is not a fault.
    """
    assert coordinator.ENGINE_CONNECT_TIMEOUT_SECONDS <= 15
    assert coordinator.ENGINE_READ_TIMEOUT_SECONDS >= 600


def test_a_slow_engine_is_not_reported_as_missing_vram():
    """
    THE REGRESSION TEST THAT MATTERS MOST IN THIS FILE.

    Every exception out of the engine stream used to produce "[System Alert: Node
    disconnected. Insufficient VRAM. Model evicted.]" — three specific claims about a cause
    the code had never determined. On the failure that prompted this work it was none of
    them: no node had disconnected, VRAM was never short, nothing was evicted. It was a read
    timeout while the model loaded across a tunnel. That single string sent days of
    debugging at hardware that was fine.
    """
    log_line, message, is_error = coordinator._engine_failure_report(
        httpx.ReadTimeout("timed out"), "llama3.1-8b"
    )

    assert "VRAM" not in message
    assert "evicted" not in message.lower()
    assert "disconnected" not in message.lower()
    # And it must be recognisable as the transient state it is, not an alarm.
    assert is_error is False
    assert "timeout" in log_line.lower() or "did not respond" in log_line.lower()


def test_a_genuinely_dropped_connection_is_still_called_a_disconnection():
    """The old message was wrong about *why*, not about this case. Truth is still truth."""
    _, message, is_error = coordinator._engine_failure_report(
        ConnectionResetError("worker vanished"), "llama3.1-8b"
    )
    assert "disconnected" in message.lower()
    assert is_error is True


def test_an_unreachable_engine_reads_as_loading_not_as_a_crash():
    """A process that is up but has not bound its socket yet is loading, and says so."""
    _, message, is_error = coordinator._engine_failure_report(
        httpx.ConnectError("refused"), "phi3-mini"
    )
    assert message == coordinator.MODEL_LOADING_NUDGE
    assert is_error is False


def test_an_unrecognised_failure_names_its_type_rather_than_guessing():
    """The fallback must report what it knows and stop there."""
    _, message, is_error = coordinator._engine_failure_report(ValueError("odd"), "phi3-mini")
    assert "ValueError" in message
    assert "VRAM" not in message
    assert is_error is True


# --------------------------------------------------------------------------------------
# Phase A3 — the two-ended speed test
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("endpoint,expected", [
    ("127.0.0.1:50052", "local"),
    ("::1", "local"),
    ("192.168.40.23:50052", "lan"),
    ("10.8.0.4:50052", "wan"),      # tunnel: private numerically, remote in every way that matters
    ("1.1.1.1:11435", "wan"),
    ("not-an-address", "unknown"),
])
def test_endpoint_classification(endpoint, expected):
    assert benchmark.classify_endpoint(endpoint) == expected


def test_a_tunnel_address_is_wan_even_though_it_is_numerically_private():
    """
    Called out on its own because getting this wrong is subtle and expensive.

    10.8.0.0/24 is RFC1918, so a naive check calls it LAN — and the shard planner would
    then spend a WAN link as though it were free. The whole purpose of that subnet is to
    carry traffic between machines that are not on one network.
    """
    assert benchmark.classify_endpoint("10.8.0.4:50052") == "wan"
    assert benchmark.classify_endpoint("10.0.0.4:50052") == "lan"


def test_a_pipeline_summary_says_whether_it_crosses_the_internet():
    summary = benchmark.summarise_endpoints(["127.0.0.1:50052", "10.8.0.4:50052"])
    assert summary["local"] == 1
    assert summary["wan"] == 1
    assert summary["sharded"] is True
    assert summary["crosses_wan"] is True


@pytest.fixture
def empty_benchmarks(sandbox):
    """A results file with nothing in it, since these cases assert on its exact contents."""
    path = benchmark.benchmarks_path()
    if os.path.exists(path):
        os.remove(path)
    return path


def test_results_survive_the_session_that_produced_them(empty_benchmarks):
    """
    The operator cannot be connected remotely and watching the Coordinator at once, so a
    measurement taken while remote has to still be there afterwards. Append-only, one object
    per line: a whole-file rewrite is the shape that loses every earlier measurement.
    """
    assert benchmark.append_record({"kind": "link", "rtt_min_ms": 21.5})
    assert benchmark.append_record({"kind": "inference", "tokens_per_second": 42.0})

    records = benchmark.read_records()
    assert len(records) == 2
    assert records[-1]["tokens_per_second"] == 42.0


def test_one_torn_line_does_not_cost_every_other_measurement(empty_benchmarks):
    """A file appended to by several processes over months is where a torn line appears."""
    benchmark.append_record({"kind": "link", "rtt_min_ms": 21.5})
    with open(benchmark.benchmarks_path(), "a", encoding="utf-8") as handle:
        handle.write('{"kind": "link", "rtt_min\n')
    benchmark.append_record({"kind": "link", "rtt_min_ms": 22.5})

    records = benchmark.read_records()
    assert [r["rtt_min_ms"] for r in records] == [21.5, 22.5]


def test_a_benchmark_refuses_to_measure_a_model_it_would_have_to_start(sandbox):
    """
    Starting the model here would fold the load time into the generation rate and produce a
    number that means nothing. The load is worth measuring — separately.
    """
    import asyncio

    class NotRunning:
        async def asnapshot_model(self, model_path):
            return None

    result = asyncio.run(benchmark.run_inference_benchmark(NotRunning(), "phi3-mini"))
    assert result["ok"] is False
    assert "not running" in result["detail"].lower()


def test_the_probe_payload_is_capped():
    """`?bytes=` is otherwise an arbitrary allocation request aimed at the Coordinator."""
    assert benchmark.MAX_PROBE_BYTES <= 64 * 1024 * 1024
    assert benchmark.DEFAULT_PROBE_BYTES <= benchmark.MAX_PROBE_BYTES


# --------------------------------------------------------------------------------------
# Phase B / C — the shard planner and the settle window
# --------------------------------------------------------------------------------------

def _registry_with(*workers):
    registry = WorkerRegistry()
    for index, (ip, vram) in enumerate(workers):
        registry.update_worker(
            worker_id=f"w{index}", ip=ip, port=50052, token=f"t{index}",
            hardware_profile={"gpu": {"has_gpu": True, "vram_total_gb": vram}},
        )
    return registry


def test_a_model_that_fits_locally_is_never_sharded():
    """
    The whole of Phase C in one assertion, and the exact case every live test ran.

    Adding a remote backend to a model that already fits costs a multi-gigabyte transfer
    before the first token and a network round trip per token afterwards, and buys nothing:
    the model was never short of memory.
    """
    registry = _registry_with(("127.0.0.1", 20.0), ("10.8.0.4", 20.0))
    assert registry.plan_endpoints(required_gb=4.0) == ["127.0.0.1:50052"]


def test_a_model_that_does_not_fit_reaches_for_the_network():
    """And when it genuinely does not fit, that is what the remote node is for."""
    registry = _registry_with(("127.0.0.1", 8.0), ("10.8.0.4", 20.0))
    planned = registry.plan_endpoints(required_gb=24.0)
    assert planned == ["127.0.0.1:50052", "10.8.0.4:50052"]


def test_the_planner_covers_the_model_with_the_part_of_the_pool_admission_will_spend():
    """
    Admission only lets a model claim POOL_SAFETY_FRACTION of the pool's reported VRAM, so
    the planner must not stop adding GPUs at raw VRAM >= the model. A 14.5 GiB model on
    16 GiB of local cards "fits" raw, but 14.5 > 0.9 x 16 = 14.4: admission would refuse
    it, and the planner had not asked the LAN node for help.
    """
    from engine.inference_manager import POOL_SAFETY_FRACTION

    registry = _registry_with(("127.0.0.1", 16.0), ("192.168.1.50", 8.0))
    assert 14.3 < 16.0 * POOL_SAFETY_FRACTION < 14.5
    # 14.3 / 0.9 = 15.9 <= 16: the local cards cover it, nothing is borrowed
    assert registry.plan_endpoints(required_gb=14.3) == ["127.0.0.1:50052"]
    # 14.5 / 0.9 = 16.1 > 16: raw VRAM "fits" (the old test stopped here) but admission
    # would refuse it, so the LAN node is brought in
    assert registry.plan_endpoints(required_gb=14.5) == ["127.0.0.1:50052", "192.168.1.50:50052"]


@pytest.mark.parametrize("policy", ["auto", "always"])
def test_no_endpoints_at_all_plans_nothing_instead_of_raising(policy):
    """
    The router agent reported an IndexError (`candidates[0]`) when there is nothing to plan
    over. Every shape of "nothing" must give an empty plan: an empty registry, a pool of
    members that run their own models (CPU pool and coding members have no rpc-server), and
    a GPU handed back to its owner.
    """
    assert WorkerRegistry().plan_endpoints(required_gb=10.0, policy=policy) == []

    members = WorkerRegistry()
    members.update_worker(worker_id="app", ip="10.8.0.2", port=50052, token="t1",
                          hardware_profile={"pool": "app", "gpu": {"has_gpu": False}})
    members.update_worker(worker_id="coder", ip="10.8.0.3", port=50052, token="t2",
                          hardware_profile={"pool": "coding", "gpu": {"has_gpu": True, "vram_total_gb": 24.0}})
    assert members.plan_endpoints(required_gb=10.0, policy=policy) == []
    assert members.plan_endpoints(required_gb=0.0, policy=policy) == []

    yielding = WorkerRegistry()
    yielding.update_worker(worker_id="w", ip="192.168.1.50", port=50052, token="t3",
                           hardware_profile={"gpu": {"has_gpu": True, "vram_total_gb": 8.0}, "is_yielding": True})
    assert yielding.plan_endpoints(required_gb=4.0, policy=policy) == []


@pytest.mark.parametrize("required_gb", [0.0, -1.0, float("nan")])
def test_nothing_local_and_nothing_needed_still_runs_on_the_best_remote_node(required_gb):
    """
    The planner's last resort, the only line that reads ``candidates[0]``: with no local node and a "need" the
    first remote node does not satisfy (zero, negative or not a number), nothing is selected by the loop, and the
    fallback must pick the best candidate (LAN before WAN), never an empty plan and never an IndexError. An
    empty plan is for a pool with no eligible node at all (the test above).
    """
    registry = _registry_with(("10.8.0.4", 8.0), ("192.168.1.50", 24.0))
    assert registry.plan_endpoints(required_gb=required_gb) == ["192.168.1.50:50052"]


def test_a_coding_member_is_left_out_of_the_plan_but_the_pool_still_plans():
    """Members run whole models of their own; the GPUs that do shard are planned as before."""
    registry = WorkerRegistry()
    registry.update_worker(worker_id="coder", ip="127.0.0.1", port=50099, token="t0",
                           hardware_profile={"pool": "coding", "gpu": {"has_gpu": True, "vram_total_gb": 24.0}})
    registry.update_worker(worker_id="gpu", ip="192.168.1.50", port=50052, token="t1",
                           hardware_profile={"gpu": {"has_gpu": True, "vram_total_gb": 8.0}})

    assert registry.plan_endpoints(required_gb=4.0) == ["192.168.1.50:50052"]


def test_the_planner_stops_adding_gpus_exactly_at_the_safety_adjusted_need():
    """required_gb / POOL_SAFETY_FRACTION: 9 GiB needs 10 GiB of raw VRAM, no more, no less."""
    # 0.5 local + 9.0 LAN = 9.5 raw: enough for 9.0 by the old comparison, short of 10.0 by the new
    short = _registry_with(("127.0.0.1", 0.5), ("192.168.1.50", 9.0), ("10.8.0.4", 8.0))
    assert short.plan_endpoints(required_gb=9.0) == ["127.0.0.1:50052", "192.168.1.50:50052", "10.8.0.4:50052"]

    # 0.5 + 9.5 = 10.0 covers it exactly: the WAN node is not needed
    exact = _registry_with(("127.0.0.1", 0.5), ("192.168.1.50", 9.5), ("10.8.0.4", 8.0))
    assert exact.plan_endpoints(required_gb=9.0) == ["127.0.0.1:50052", "192.168.1.50:50052"]


def test_capacity_is_bought_cheapest_first():
    """LAN before WAN, whatever order the workers happened to register in."""
    registry = _registry_with(("10.8.0.4", 8.0), ("192.168.1.50", 8.0), ("127.0.0.1", 2.0))
    planned = registry.plan_endpoints(required_gb=9.0)
    assert planned[0] == "127.0.0.1:50052"
    assert planned[1] == "192.168.1.50:50052"
    assert "10.8.0.4:50052" not in planned


def test_local_endpoints_always_participate():
    """
    Free capacity is never left idle, and a coordinator_only box must still have somewhere
    to run when nothing else is needed.
    """
    registry = _registry_with(("127.0.0.1", 0.5))
    assert registry.plan_endpoints(required_gb=0.0) == ["127.0.0.1:50052"]


def test_the_always_policy_restores_the_old_behaviour():
    """Kept so the sharding path can be exercised on hardware where it would not engage."""
    registry = _registry_with(("127.0.0.1", 20.0), ("10.8.0.4", 20.0))
    planned = registry.plan_endpoints(required_gb=1.0, policy="always")
    assert set(planned) == {"127.0.0.1:50052", "10.8.0.4:50052"}


def test_planning_is_deterministic():
    """An unstable order reads as a topology change and triggers a needless full restart."""
    registry = _registry_with(("10.8.0.4", 8.0), ("192.168.1.50", 8.0), ("127.0.0.1", 8.0))
    first = registry.plan_endpoints(required_gb=20.0)
    assert all(registry.plan_endpoints(required_gb=20.0) == first for _ in range(5))


def test_a_measured_link_beats_an_unmeasured_one_within_a_class():
    """Assuming an unmeasured node is fast is how shards land on the slowest link there is."""
    registry = _registry_with(("192.168.1.50", 8.0), ("192.168.1.51", 8.0))
    registry.record_link_measurement("w1", {"rtt_min_ms": 1.0})
    planned = registry.plan_endpoints(required_gb=8.0)
    assert planned[0] == "192.168.1.51:50052"


def test_a_link_measurement_does_not_survive_the_node_that_reported_it():
    """A stale figure must not go on influencing placement for a machine that has left."""
    registry = _registry_with(("192.168.1.50", 8.0))
    registry.record_link_measurement("w0", {"rtt_min_ms": 5.0})
    assert registry.get_link_measurement("w0")

    registry.timeout = -1  # everything is overdue
    registry.prune_dead_workers()
    assert registry.get_link_measurement("w0") == {}


def test_a_worker_reported_measurement_cannot_reach_arithmetic_as_a_string():
    """Every number here was chosen by another machine. R3-1's lesson, applied again."""
    registry = _registry_with(("192.168.1.50", 8.0))
    registry.record_link_measurement("w0", {"rtt_min_ms": "fast", "download_mbps": None})
    stored = registry.get_link_measurement("w0")
    assert "rtt_min_ms" not in stored
    assert "download_mbps" not in stored


def test_an_unknown_worker_cannot_grow_the_measurement_table():
    registry = _registry_with(("192.168.1.50", 8.0))
    registry.record_link_measurement("never-registered", {"rtt_min_ms": 5.0})
    assert registry.get_link_measurement("never-registered") == {}


# --- settle window -------------------------------------------------------------------

def _set_live_endpoints(monkeypatch, endpoints):
    """
    Point ``manager.current_endpoints`` at a fixed list.

    Patched on the class, not the instance: it is a read-only property (deliberately — it
    hands out a copy so the monitor cannot mutate the manager's state), and a property has
    no instance-level setter to monkeypatch.
    """
    monkeypatch.setattr(
        inference_manager.InferenceManager, "current_endpoints",
        property(lambda self: list(endpoints)), raising=False,
    )


@pytest.fixture
def settle_env(monkeypatch):
    """A clean topology-debounce state for each case."""
    monkeypatch.setattr(coordinator, "_pending_topology", None)
    monkeypatch.setattr(coordinator, "_pending_topology_since", 0.0)
    _set_live_endpoints(monkeypatch, ["127.0.0.1:50052"])
    return monkeypatch


def test_an_unchanged_topology_rebuilds_nothing(settle_env):
    assert coordinator._topology_change_is_settled(["127.0.0.1:50052"]) is False


def test_losing_an_endpoint_acts_immediately(settle_env):
    """
    The engine already holds a backend that is gone, so it is broken now. Every second of
    debounce here is a second of outage for nothing.
    """
    _set_live_endpoints(settle_env, ["127.0.0.1:50052", "10.8.0.4:50052"])
    assert coordinator._topology_change_is_settled(["127.0.0.1:50052"]) is True


def test_gaining_an_endpoint_waits_for_the_cluster_to_settle(settle_env):
    """
    A rebuild re-sends every remote backend its share of the weights. Reacting to a node
    that is still deciding whether it is here costs the cluster a full redistribution, and
    the loop that decides this runs twice a second.
    """
    settle_env.setattr(coordinator, "get_worker_limits",
                       lambda: {"topology_settle_seconds": 30, "shard_policy": "auto"})
    assert coordinator._topology_change_is_settled(
        ["127.0.0.1:50052", "10.8.0.4:50052"]) is False


def test_a_settled_gain_eventually_rebuilds(settle_env):
    settle_env.setattr(coordinator, "get_worker_limits",
                       lambda: {"topology_settle_seconds": 30, "shard_policy": "auto"})
    target = ["127.0.0.1:50052", "10.8.0.4:50052"]

    assert coordinator._topology_change_is_settled(target) is False
    # Rewind the clock rather than sleeping: the behaviour under test is the comparison.
    coordinator._pending_topology_since -= 31
    assert coordinator._topology_change_is_settled(target) is True


def test_a_flapping_node_restarts_the_clock_rather_than_inheriting_it(settle_env):
    """A timer counting toward a topology that no longer exists is not a settled topology."""
    settle_env.setattr(coordinator, "get_worker_limits",
                       lambda: {"topology_settle_seconds": 30, "shard_policy": "auto"})

    coordinator._topology_change_is_settled(["127.0.0.1:50052", "10.8.0.4:50052"])
    coordinator._pending_topology_since -= 29
    # A different set arrives before the first one settled.
    assert coordinator._topology_change_is_settled(
        ["127.0.0.1:50052", "10.8.0.5:50052"]) is False
    coordinator._pending_topology_since -= 2
    # The original would have been due by now; this one is not, and must not inherit it.
    assert coordinator._topology_change_is_settled(
        ["127.0.0.1:50052", "10.8.0.5:50052"]) is False


def test_a_zero_settle_window_restores_the_old_immediate_behaviour(settle_env):
    settle_env.setattr(coordinator, "get_worker_limits",
                       lambda: {"topology_settle_seconds": 0, "shard_policy": "auto"})
    assert coordinator._topology_change_is_settled(
        ["127.0.0.1:50052", "10.8.0.4:50052"]) is True


# --------------------------------------------------------------------------------------
# Phase D — speculative decoding and the model catalogue
# --------------------------------------------------------------------------------------

def test_a_draft_companion_is_not_offered_as_a_model():
    """
    The live cluster listed llama3.1-8b and llama3.1-8b_dflash side by side.

    A draft is a deliberately small, deliberately inaccurate model whose only job is to
    propose tokens for a real one to check. Served on its own it is simply a bad model,
    presented to the user as a choice.
    """
    assert inference_manager._is_draft_companion("llama3.1-8b_dflash.gguf") is True
    assert inference_manager._is_draft_companion("llama3.1-8b.gguf") is False
    # Case-insensitively, because a filesystem is.
    assert inference_manager._is_draft_companion("Llama_DFlash.GGUF") is True


def test_the_catalogue_hides_draft_companions(sandbox, monkeypatch):
    models_dir = os.path.join(sandbox, "models")
    os.makedirs(models_dir, exist_ok=True)
    for name in ("phi3-mini.gguf", "phi3-mini_dflash.gguf"):
        with open(os.path.join(models_dir, name), "wb") as handle:
            handle.write(b"\0" * 1024)

    manager = inference_manager.InferenceManager()
    monkeypatch.setattr(manager, "_model_search_roots", lambda: [models_dir])

    ids = [entry["id"] for entry in manager.get_available_models(24.0, 32.0)["data"]]
    assert "phi3-mini" in ids
    assert not any("dflash" in i.lower() for i in ids)


@pytest.mark.parametrize("endpoint,remote", [
    ("127.0.0.1:50052", False),
    ("192.168.1.50:50052", True),
    ("10.8.0.4:50052", True),
])
def test_remote_endpoint_detection(endpoint, remote):
    assert inference_manager._endpoint_is_remote(endpoint) is remote


def test_the_rpc_cache_flag_is_probed_rather_than_assumed(monkeypatch):
    """
    Passing a flag an older build does not know makes it exit immediately — turning a
    performance improvement into a node that will not start, which is the one outcome not
    worth any amount of speed.
    """
    from engine import worker as worker_module

    monkeypatch.setattr(worker_module, "_RPC_CACHE_SUPPORT", {})

    def fake_run(cmd, **kwargs):
        return types.SimpleNamespace(stdout="  -c, --cache   enable local cache\n", stderr="")

    monkeypatch.setattr(worker_module.subprocess, "run", fake_run)
    assert worker_module._rpc_server_supports_cache("ggml-rpc-server") is True

    monkeypatch.setattr(worker_module, "_RPC_CACHE_SUPPORT", {})

    def old_build(cmd, **kwargs):
        return types.SimpleNamespace(stdout="  -H, --host\n  -p, --port\n", stderr="")

    monkeypatch.setattr(worker_module.subprocess, "run", old_build)
    assert worker_module._rpc_server_supports_cache("ggml-rpc-server") is False


def test_an_unanswerable_probe_assumes_no_cache(monkeypatch):
    """A node that cannot answer the question still has to start."""
    from engine import worker as worker_module

    monkeypatch.setattr(worker_module, "_RPC_CACHE_SUPPORT", {})

    def explode(cmd, **kwargs):
        raise OSError("binary is not executable here")

    monkeypatch.setattr(worker_module.subprocess, "run", explode)
    assert worker_module._rpc_server_supports_cache("ggml-rpc-server") is False
