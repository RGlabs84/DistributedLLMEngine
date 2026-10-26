#!/usr/bin/env python3
"""
Load and soak test of the coding path: coordinator, router and queue, with many fake agents and many fake members.
No real model, no real network: every address is on loopback.

    python scripts/coding_load.py run <profile> [--out DIR] [--agents N] [--duration S] [--scale X] [--members N] ...
    python scripts/coding_load.py report DIR          # analyse a finished (or interrupted) run again
    python scripts/coding_load.py profiles            # list the profiles

Laptop use goes through the CPU lane (memory is capped, one heavy job at a time):

    HEAVY_LOCK=$HOME/valheim-testbed/heavy-cpu.lock ~/valheim-testbed/heavy.sh 6G coding-load \\
        timeout 1500 ~/.venvs/dlne/bin/python scripts/coding_load.py run members

WHAT RUNS
    * the coordinator, as its own process, from this checkout, with a temporary base directory (a folder holding a symlink to
      this checkout's ``engine/`` package, so the coordinator's ``get_base_dir()`` is the temporary folder and nothing reaches
      the real install), ``CODING_MEMBERS_ENABLED`` as the profile says, and its engine manager replaced by a stand-in that
      points at a fake llama-server (the one thing that is not the product's own code);
    * one process of fakes (scripts/coding_load_fakes.py): TheWall's engine with 2 slots, the coding members (signed
      heartbeats, the real reverse tunnel, sealed answers), contributors that only heartbeat;
    * one or two processes of agents (scripts/coding_load_clients.py) that behave like the extension.

WHAT IT MEASURES (``report.md`` / ``report.json`` in the run folder)
    throughput; time to first token and queue wait per tier (p50/p95/p99); sticky hit rate; re-routing and whether any sealed
    envelope was ever sent twice or reached the wrong member; place-in-line accuracy; Stop/cancel freeing a slot;
    the coordinator's CPU, memory, open files, threads and event-loop lag over time, and the sizes of the router's tables.

``scale`` makes every fake machine that many times faster and every agent think that many times less, so a 3 minute
run covers a long one. Request counts and event counts are not compressed in the tokens (a streamed event carries several),
so the coordinator's per-request work is real; its per-token work is lower than at real speed.
"""

import argparse
import asyncio
import copy
import json
import os
import random
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from coding_load_fakes import (EventLog, FakeContributor, FakeLocalEngine, FakeMember, LagProbe, identity_for,  # noqa: E402
                               loopback, percentile, proc_stats, token_for)
from coding_load_clients import LoadClient, read_jsonl  # noqa: E402

# ======================================================================================
# Profiles
# ======================================================================================

BASE_SPEC: Dict[str, Any] = {
    "name": "base",
    "scale": 6.0,
    "duration_s": 150,
    "grace_s": 90,
    "sample_s": 2.0,
    "client_procs": 1,
    "members_expected": False,           # the run needs the router (engine/coding_router.py)
    "local": {"slots": 2, "decode_tps": 40.0, "prefill_tps": 1500.0, "boot_s": 15.0, "cache_sessions": 8, "inject": {},
              "safe_context": 262144},
    "members": {"count": 0, "slots": 2, "decode_tps": 30.0, "prefill_tps": 120.0, "cache_sessions": 8, "trusted": [],
                "slow": {}, "inject": {}, "heartbeat_s": 5.0, "rotate_every_s": None, "stagger_s": 0.15},
    "churn": None,
    "agents": {"count": 20, "tiers": {"key": 0.2, "cpu": 0.3, "trial": 0.5},
               "privacy": {"open": 0.4, "any": 0.3, "trusted": 0.3},
               "ptok0": 3000, "ptok_step": 1200, "ptok_max": 40000, "otok": [60, 600], "calls_per_task": 8,
               "tool_gap_s": 2.0, "user_gap_s": 30.0, "cancel_prob": 0.0, "cancel_after_s": [1.0, 20.0],
               "ramp_s": 10.0, "api_key_count": 4, "cpu_contributors": 4},
    "config": {},
    "starvation_bound_s": 300.0,         # a trial user waits at most this long (real-speed equivalent) at the profile's load
}


def _merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


_MEMBER_AGENTS = {"count": 100, "tiers": {"key": 0.05, "gpu": 0.10, "cpu": 0.15, "trial": 0.70}, "ramp_s": 20.0, "cancel_prob": 0.03}

PROFILES: Dict[str, Dict[str, Any]] = {
    # Normal CI: a few seconds, two members. Checks that the whole rig works, not how fast it is.
    "smoke": {"scale": 40.0, "duration_s": 7, "grace_s": 25, "sample_s": 1.0,
              "local": {"boot_s": 1.0},
              "members": {"count": 2, "trusted": [0], "stagger_s": 0.05},
              "agents": {"count": 6, "tiers": {"key": 0.34, "cpu": 0.33, "trial": 0.33}, "ptok0": 1500, "ptok_step": 600,
                         "ramp_s": 1.0, "calls_per_task": 3, "tool_gap_s": 1.0, "user_gap_s": 4.0, "otok": [30, 90],
                         "api_key_count": 2, "cpu_contributors": 2, "cancel_prob": 0.1, "cancel_after_s": [0.5, 3.0],
                         "generic_fraction": 0.2}},
    # Today: no members; 20 agents on the 2 slots of TheWall's own engine.
    "today": {"scale": 6.0, "duration_s": 150, "agents": {"count": 20, "cancel_prob": 0.06, "generic_fraction": 0.15}},
    # Today, saturated by the higher tiers, to see whether a trial user is ever served.
    "starve": {"scale": 6.0, "duration_s": 150,
               "agents": {"count": 20, "tiers": {"key": 0.4, "cpu": 0.3, "trial": 0.3}, "tool_gap_s": 0.2, "user_gap_s": 1.0,
                          "calls_per_task": 30}},
    "members": {"members_expected": True, "scale": 6.0, "duration_s": 180, "members": {"count": 10, "trusted": [0, 1, 2]},
                "agents": _MEMBER_AGENTS},
    "members200": {"members_expected": True, "scale": 6.0, "duration_s": 180, "client_procs": 2,
                   "members": {"count": 10, "trusted": [0, 1, 2]}, "agents": {**_MEMBER_AGENTS, "count": 200, "ramp_s": 40.0}},
    "churn": {"members_expected": True, "scale": 6.0, "duration_s": 240,
              "members": {"count": 10, "trusted": [0, 1, 2], "inject": {"die_mid_rate": 0.10}},
              "churn": {"period_s": 4.0, "min_up": 4, "die_fraction": 0.4, "aside_fraction": 0.25},
              "agents": {**_MEMBER_AGENTS, "count": 60, "generic_fraction": 0.15}},
    # The extension's agents with callers that are not the extension (no X-DLNE-Session) mixed in: those must never see a
    # queue frame (a generic OpenAI client would read it as a broken answer) and still wait their turn.
    "mixed": {"members_expected": True, "scale": 6.0, "duration_s": 150, "members": {"count": 10, "trusted": [0, 1, 2]},
              "agents": {**_MEMBER_AGENTS, "count": 80, "generic_fraction": 0.3}},
    "slow": {"members_expected": True, "scale": 6.0, "duration_s": 180,
             "members": {"count": 10, "trusted": [0, 1, 2], "slow": {"0": {"decode_tps": 15.0, "prefill_tps": 80.0}}},
             "agents": {**_MEMBER_AGENTS, "count": 60}},
    # Many requests per second: short answers, short prompts, fast machines, many agents with no patience.
    "ramp": {"members_expected": True, "scale": 30.0, "duration_s": 300, "client_procs": 2, "sample_s": 2.0,
             "members": {"count": 10, "trusted": [0, 1, 2], "heartbeat_s": 5.0},
             "agents": {**_MEMBER_AGENTS, "count": 300, "ramp_s": 240.0, "tool_gap_s": 0.5, "user_gap_s": 3.0,
                        "ptok0": 1200, "ptok_step": 300, "otok": [20, 120], "calls_per_task": 12, "cancel_prob": 0.0}},
    # 30 to 60 minutes at moderate load, key rotation and light churn on, to watch memory, files, lag and the tables.
    "soak": {"members_expected": True, "scale": 1.0, "duration_s": 1800, "grace_s": 120, "sample_s": 10.0,
             "local": {"boot_s": 20.0},
             "members": {"count": 6, "trusted": [0, 1], "rotate_every_s": 300.0, "inject": {"die_mid_rate": 0.02}},
             "churn": {"period_s": 90.0, "min_up": 4, "die_fraction": 0.3},
             "agents": {**_MEMBER_AGENTS, "count": 40, "ramp_s": 60.0, "tool_gap_s": 4.0, "user_gap_s": 60.0,
                        "cancel_prob": 0.02}},
}


def make_spec(profile: str, overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if profile not in PROFILES:
        raise SystemExit(f"unknown profile {profile!r}; one of: {', '.join(PROFILES)}")
    spec = _merge(BASE_SPEC, PROFILES[profile])
    spec["name"] = profile
    if overrides:
        spec = _merge(spec, overrides)
    return spec


# ======================================================================================
# Helpers
# ======================================================================================

def free_port(host: str = "127.0.0.1") -> int:
    with socket.socket() as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


def available_cpus() -> List[int]:
    try:
        return sorted(os.sched_getaffinity(0))
    except AttributeError:
        return list(range(os.cpu_count() or 1))


def cpu_plan(spec: Dict[str, Any]) -> Dict[str, List[int]]:
    """Pin each part to its own cores, the highest ones the machine has (at most 8 in all)."""
    cpus = available_cpus()
    if len(cpus) < 6:
        return {}
    top = cpus[-8:] if len(cpus) >= 8 else cpus
    procs = int(spec.get("client_procs", 1))
    return {"coordinator": top[0:2], "fakes": top[2:4], "clients": top[4:4 + max(1, procs)] or top[4:], "parent": top[-1:]}


def spawn(name: str, argv: List[str], env: Dict[str, str], cwd: str, log_path: str,
          cpus: Optional[List[int]]) -> subprocess.Popen:
    log = open(log_path, "ab")

    def pin() -> None:
        if cpus:
            try:
                os.sched_setaffinity(0, set(cpus))
            except (AttributeError, OSError):
                pass
        os.setsid()

    return subprocess.Popen(argv, env=env, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, preexec_fn=pin)


def stop_proc(proc: Optional[subprocess.Popen], timeout: float = 20.0) -> Optional[int]:
    if proc is None:
        return None
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            proc.wait(timeout=10)
    return proc.returncode


def write_json(path: str, obj: Any) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(obj, handle, indent=1)


# ======================================================================================
# Role: coordinator (a real coordinator process from this checkout, with a stand-in for its engine manager)
# ======================================================================================

def coordinator_main(args: argparse.Namespace) -> None:
    import uvicorn
    spec = json.load(open(args.spec, encoding="utf-8"))
    runtime = spec["_runtime"]

    from engine import coordinator as co
    from engine.priority import coding_gate
    import httpx

    engine_port, ctl_port = runtime["engine_port"], runtime["ctl_port"]
    local = spec["local"]
    state = {"running": True, "slots": int(local["slots"]), "boots": 0}

    class AliveProcess:
        returncode = None

        def poll(self):
            return None

    async def ctl(**command: Any) -> None:
        async with httpx.AsyncClient(timeout=600.0) as client:
            await client.post(f"http://127.0.0.1:{ctl_port}/ctl", json=command)

    async def astart_model(model_path, endpoints, vram, ram, queue_channels=2):
        if state["running"]:
            return True, engine_port, "Already running"
        await ctl(cmd="start", slots=queue_channels, boot_s=float(local.get("boot_s", 15.0)))
        state.update(running=True, slots=int(queue_channels))
        state["boots"] += 1
        return True, engine_port, "Started"

    async def astop_all_models():
        state["running"] = False
        await ctl(cmd="stop")

    async def asnapshot_model(model_path):
        if not state["running"]:
            return None
        return {"process": AliveProcess(), "safe_context": int(local.get("safe_context", 262144)),
                "dynamic_slots": state["slots"], "port": engine_port}

    async def aestimate(model_path):
        return 0.0

    async def acan_admit(*a, **k):
        return True, "ok"

    async def areap_model(model_path):
        return True

    manager = co.manager
    manager.astart_model = astart_model
    manager.astop_all_models = astop_all_models
    manager.asnapshot_model = asnapshot_model
    manager.aestimate_model_vram_gb = aestimate
    manager.acan_admit = acan_admit
    manager.areap_model = areap_model
    manager.get_active_model_names = lambda: ["fake-coder"]
    co.global_active_model = "fake-coder"
    co.has_pulled_seed = True

    probe = LagProbe()
    history: List[Dict[str, Any]] = []

    @co.app.get("/__load/metrics")
    async def load_metrics():
        import gc
        now_wall, now_mono = time.time(), time.monotonic()
        out: Dict[str, Any] = {"t": now_wall, "lag": probe.take(), "lag_worst": probe.worst, "proc": proc_stats(),
                               "gate": coding_gate.stats(), "gate_heap": len(getattr(coding_gate, "_waiters", [])),
                               "tasks": len(asyncio.all_tasks()), "inflight_counter": co.active_inference_requests,
                               "leases": co.lease_manager.active_count(), "workers": len(co.registry.workers),
                               "gc": list(gc.get_count()), "engine_boots": state["boots"],
                               "recalculating": bool(co.is_recalculating_queue), "swapping": bool(co.is_swapping_models)}
        router = getattr(sys.modules.get("engine.coding_router"), "coding_router", None)
        if router is not None:
            sessions = list(getattr(router, "_sessions", {}).values())
            routes = list(getattr(router, "_routes", {}).values())
            out["router"] = {
                "sessions": len(sessions), "sessions_stale": sum(1 for s in sessions if s[1] <= router._clock()),
                "routes": len(routes), "routes_expired": sum(1 for r in routes if r.expires <= router._clock()),
                "routes_held": sum(1 for r in routes if r.held), "holds": len(getattr(router, "_holds", [])),
                "hold_counts": sum(getattr(router, "_hold_counts", {}).values()),
                "cooldown": len(getattr(router, "_cooldown", {})), "waiters": len(getattr(router, "_waiters", [])),
                "waiters_live": sum(1 for w in getattr(router, "_waiters", []) if not w.future.done()),
                "inflight": sum(getattr(router, "_inflight", {}).values()),
                "last_served": len(getattr(router, "_last_served", {})), "first_seen": len(getattr(router, "_first_seen", {}))}
        return out

    @co.app.on_event("startup")
    async def start_probe():
        probe.start()

    keys: List[str] = []
    for i in range(int(spec["agents"].get("api_key_count", 0))):
        token, _meta = co.api_key_manager.generate_key(f"load-{i}")
        keys.append(token)
    write_json(os.path.join(args.out, "keys.json"), keys)

    config = uvicorn.Config(co.app, host="127.0.0.1", port=int(runtime["port"]), log_level="error", timeout_keep_alive=75)
    uvicorn.Server(config).run()


# ======================================================================================
# Role: fakes (TheWall's engine, the members, the contributors)
# ======================================================================================

def fakes_main(args: argparse.Namespace) -> None:
    spec = json.load(open(args.spec, encoding="utf-8"))
    asyncio.run(_fakes(spec, args.out))


async def _fakes(spec: Dict[str, Any], out: str) -> None:
    runtime = spec["_runtime"]
    scale = float(spec["scale"])
    url = f"http://127.0.0.1:{runtime['port']}"
    key = runtime["network_key"]
    log = EventLog(os.path.join(out, "fakes-events.jsonl"))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    local = FakeLocalEngine(runtime["engine_port"], runtime["ctl_port"], spec["local"], log, scale)
    await local.start()
    await local.start_control()

    mspec = spec["members"]
    count = int(mspec["count"])

    def member_spec(i: int) -> Dict[str, Any]:
        merged = {k: v for k, v in mspec.items() if k not in ("slow", "trusted", "count", "stagger_s")}
        merged.update(mspec.get("slow", {}).get(str(i), {}))
        return merged

    def make_member(i: int) -> FakeMember:
        return FakeMember(i, member_spec(i), url, key, log, scale, runtime["member_ports"][i])

    members: List[FakeMember] = [make_member(i) for i in range(count)]
    contributors = [FakeContributor(i, url, key) for i in range(int(spec["agents"].get("cpu_contributors", 0)))]
    for contributor in contributors:
        contributor.start()
    lag = LagProbe()
    lag.start()

    for member in members:
        await member.start()
        await asyncio.sleep(float(mspec.get("stagger_s", 0.15)))

    tasks = []
    if spec.get("churn") and count:
        tasks.append(asyncio.create_task(_churn(members, make_member, spec["churn"], log)))

    async def write_stats() -> None:
        with open(os.path.join(out, "fakes-stats.jsonl"), "a", buffering=1) as handle:
            while True:
                await asyncio.sleep(max(1.0, float(spec.get("sample_s", 2.0))))
                row = {"t": time.time(), "lag": lag.take(), "proc": proc_stats(), "local": local.stats(),
                       "members": [m.stats() for m in members if m.up],
                       "members_up": sum(1 for m in members if m.up)}
                handle.write(json.dumps(row, separators=(",", ":")) + "\n")
                log.flush()

    tasks.append(asyncio.create_task(write_stats()))
    await stop.wait()
    for task in tasks:
        task.cancel()
    final = {"t": time.time(), "local": local.stats(), "members": [m.stats() for m in members],
             "heartbeat_answers": {m.name: m.stats()["answers"] for m in members}}
    write_json(os.path.join(out, "fakes-final.json"), final)
    for member in members:
        await member.stop()
    log.close()


async def _churn(members: List[FakeMember], make_member, cfg: Dict[str, Any], log: EventLog) -> None:
    """Members joining and leaving: every few seconds one leaves (or dies, with its streams reset) or one comes back."""
    rng = random.Random(7)
    period, min_up = float(cfg["period_s"]), int(cfg["min_up"])
    while True:
        await asyncio.sleep(period * rng.uniform(0.5, 1.5))
        up = [i for i, m in enumerate(members) if m.up]
        down = [i for i, m in enumerate(members) if not m.up]
        if down and (len(up) <= min_up or rng.random() < 0.5):
            i = rng.choice(down)
            members[i] = make_member(i)
            await members[i].start()
        elif len(up) > min_up:
            i = rng.choice(up)
            if rng.random() < float(cfg.get("aside_fraction", 0.0)):
                # The owner starts a game: ready false and no slots for a while, then back.
                members[i].aside = True
                log.add("member", op="aside", who=members[i].name)
                asyncio.get_running_loop().call_later(period * rng.uniform(1.5, 3.0), _resume, members[i], log)
                continue
            await members[i].stop(hard=rng.random() < float(cfg.get("die_fraction", 0.4)))


def _resume(member: FakeMember, log: EventLog) -> None:
    if member.up:
        member.aside = False
        log.add("member", op="resume", who=member.name)


# ======================================================================================
# Role: clients (the agents)
# ======================================================================================

def clients_main(args: argparse.Namespace) -> None:
    spec = json.load(open(args.spec, encoding="utf-8"))
    runtime = spec["_runtime"]
    spec["_shards"] = int(spec.get("client_procs", 1))
    spec["agents"]["api_keys"] = json.load(open(os.path.join(args.out, "keys.json"), encoding="utf-8"))
    spec["agents"]["gpu_members"] = int(spec["members"]["count"])
    client = LoadClient(f"http://127.0.0.1:{runtime['port']}", spec, os.path.join(args.out, f"clients-{args.shard}.jsonl"),
                        args.t0, args.shard, seed=1000 + args.shard)
    asyncio.run(client.run())


# ======================================================================================
# The run
# ======================================================================================

def http_get(url: str, timeout: float = 5.0) -> Tuple[int, Any, float]:
    import urllib.request
    start = time.time()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as reply:
            body = reply.read()
            return reply.status, json.loads(body), (time.time() - start) * 1000
    except Exception:  # noqa: BLE001
        return 0, None, (time.time() - start) * 1000


def prepare_base(base: str, spec: Dict[str, Any], repo: str, trusted_ids: List[str], members_on: bool) -> str:
    for sub in ("config", "logs", "models"):
        os.makedirs(os.path.join(base, sub), exist_ok=True)
    link = os.path.join(base, "engine")
    if os.path.lexists(link):
        os.unlink(link)
    os.symlink(os.path.join(repo, "engine"), link)
    network_key = secrets.token_hex(24)
    config = {
        "NETWORK_KEY": network_key, "TRUST_PROXY_HEADERS": "true", "TRUSTED_PROXY_HOPS": "1",
        "STARTER_QUOTA_INFERENCES": "10000000", "CPU_CODING_LIMIT": "10000000", "AUTO_SWAP": "false",
        "CODING_MEMBERS_ENABLED": "true" if members_on else "false",
        "TRUSTED_MEMBERS": ",".join(trusted_ids),
    }
    config.update({k: str(v) for k, v in (spec.get("config") or {}).items()})
    with open(os.path.join(base, "config", "config.txt"), "w", encoding="utf-8") as handle:
        handle.write("\n".join(f"{k}={v}" for k, v in config.items()) + "\n")
    return network_key


def router_available(repo: str) -> bool:
    return os.path.exists(os.path.join(repo, "engine", "coding_router.py"))


def run_profile(spec: Dict[str, Any], out: str, repo: str = REPO, python: Optional[str] = None,
                log=print) -> Dict[str, Any]:
    python = python or sys.executable
    if repo not in sys.path:
        sys.path.insert(0, repo)        # the parent reads the trust ids of the members it will start from the code under test
    out = os.path.abspath(out)
    os.makedirs(out, exist_ok=True)
    base = os.path.join(out, "base")
    members_on = router_available(repo) and (spec["members"]["count"] > 0 or bool(spec.get("members_expected")))
    if spec.get("members_expected") and not router_available(repo):
        raise SystemExit("this profile needs engine/coding_router.py (the router has not landed in this checkout)")
    if not members_on:
        # Today's path: no router in this checkout (or none wanted), so there is nothing for a member to join.
        spec["members"]["count"] = 0
        spec["members"]["trusted"] = []
        spec["agents"]["tiers"] = {k: v for k, v in spec["agents"]["tiers"].items() if k != "gpu"}
    count = int(spec["members"]["count"])
    trusted = [identity_for(f"m{i:02d}")[2] for i in spec["members"].get("trusted", []) if i < count]
    network_key = prepare_base(base, spec, repo, trusted, members_on)
    port = free_port()
    spec["_runtime"] = {"port": port, "engine_port": free_port(), "ctl_port": free_port(), "network_key": network_key,
                        "member_ports": [free_port() for _ in range(max(count, 1))], "members_on": members_on,
                        "trusted_ids": trusted}
    spec_path = os.path.join(out, "spec.json")
    write_json(spec_path, spec)
    plan = cpu_plan(spec)
    env = dict(os.environ, PYTHONPATH=base, PYTHONPYCACHEPREFIX=os.path.join(out, "pyc"), PYTHONUNBUFFERED="1",
               PYTHONDONTWRITEBYTECODE="")
    script = os.path.join(HERE, "coding_load.py")
    url = f"http://127.0.0.1:{port}"
    procs: Dict[str, subprocess.Popen] = {}
    result: Dict[str, Any] = {"out": out, "notes": []}
    samples = open(os.path.join(out, "samples.jsonl"), "a", buffering=1)
    try:
        log(f"[load] starting the coordinator on {url} (base {base})")
        procs["coordinator"] = spawn("coordinator", [python, script, "_coordinator", "--spec", spec_path, "--out", out],
                                     env, base, os.path.join(out, "coordinator.log"), plan.get("coordinator"))
        if not wait_for(lambda: http_get(f"{url}/__load/metrics")[0] == 200 and os.path.exists(os.path.join(out, "keys.json")),
                        90, procs["coordinator"]):
            raise RuntimeError("the coordinator did not come up; see coordinator.log")
        log("[load] starting the fakes")
        procs["fakes"] = spawn("fakes", [python, script, "_fakes", "--spec", spec_path, "--out", out], env, base,
                               os.path.join(out, "fakes.log"), plan.get("fakes"))
        if members_on and count:
            ready = wait_for(lambda: ((http_get(f"{url}/v1/cluster/status")[1] or {}).get("coding_members") or {}).get("ready", 0)
                             >= count, 90, procs["fakes"])
            status = http_get(f"{url}/v1/cluster/status")[1] or {}
            result["members_at_start"] = status.get("coding_members")
            log(f"[load] members: {status.get('coding_members')} (all ready: {ready})")
            if not ready:
                result["notes"].append("not every member was ready when the agents started")
        else:
            time.sleep(1.0)
        t0 = time.time() + 2.0
        shards = int(spec.get("client_procs", 1))
        cpus = plan.get("clients") or []
        for shard in range(shards):
            procs[f"clients{shard}"] = spawn(
                f"clients{shard}", [python, script, "_clients", "--spec", spec_path, "--out", out, "--shard", str(shard),
                                    "--t0", repr(t0)], env, base, os.path.join(out, f"clients-{shard}.log"),
                [cpus[shard % len(cpus)]] if cpus else None)
        log(f"[load] {spec['agents']['count']} agents for {spec['duration_s']} s (scale x{spec['scale']:g}), "
            f"{shards} client process(es)")
        deadline = t0 + float(spec["duration_s"]) + float(spec.get("grace_s", 60)) + 30
        last = 0.0
        prev_proc: Dict[str, Dict[str, Any]] = {}
        while time.time() < deadline:
            time.sleep(max(0.2, min(1.0, float(spec["sample_s"]))))
            if time.time() - last >= float(spec["sample_s"]):
                last = time.time()
                row = sample(url, procs)
                samples.write(json.dumps(row, separators=(",", ":")) + "\n")
            if all(procs[f"clients{s}"].poll() is not None for s in range(shards)):
                break
            if procs["coordinator"].poll() is not None:
                result["notes"].append("THE COORDINATOR EXITED during the run")
                break
        result["clients_finished"] = all(procs[f"clients{s}"].poll() is not None for s in range(shards))
        log("[load] agents done; waiting for the queue to drain")
        drained = False
        for _ in range(60):
            code, metrics, _ms = http_get(f"{url}/__load/metrics")
            if code == 200 and metrics["gate"]["in_use"] == 0 and metrics["gate"]["waiting"] == 0 \
                    and metrics.get("inflight_counter", 0) == 0:
                drained = True
                break
            time.sleep(1.0)
        code, metrics, _ms = http_get(f"{url}/__load/metrics")
        result["drained"] = drained
        write_json(os.path.join(out, "final.json"), {"metrics": metrics, "status": (http_get(f"{url}/v1/cluster/status")[1])})
        for shard in range(shards):
            stop_proc(procs[f"clients{shard}"], 30)
    finally:
        samples.close()
        stop_proc(procs.get("fakes"), 30)
        result["coordinator_exit"] = stop_proc(procs.get("coordinator"), 30)
    report = analyze(out)
    result["report"] = report
    write_json(os.path.join(out, "report.json"), report)
    with open(os.path.join(out, "report.md"), "w", encoding="utf-8") as handle:
        handle.write(format_report(report))
    return result


def wait_for(check, timeout: float, proc: Optional[subprocess.Popen] = None) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if check():
                return True
        except Exception:  # noqa: BLE001
            pass
        if proc is not None and proc.poll() is not None:
            return False
        time.sleep(0.25)
    return False


def sample(url: str, procs: Dict[str, subprocess.Popen]) -> Dict[str, Any]:
    t = time.time()
    code, metrics, metrics_ms = http_get(f"{url}/__load/metrics")
    _c, status, status_ms = http_get(f"{url}/v1/cluster/status")
    row: Dict[str, Any] = {"t": t, "metrics": metrics, "metrics_ms": round(metrics_ms, 1), "status_ms": round(status_ms, 1),
                           "coding_queue": (status or {}).get("coding_queue"),
                           "coding_members": (status or {}).get("coding_members"),
                           "procs": {name: proc_stats(p.pid) for name, p in procs.items() if p.poll() is None}}
    return row


# ======================================================================================
# Analysis
# ======================================================================================

def _dist(values: List[float]) -> Dict[str, float]:
    return {"n": len(values), "p50": round(percentile(values, 50), 3), "p95": round(percentile(values, 95), 3),
            "p99": round(percentile(values, 99), 3), "max": round(max(values), 3) if values else 0.0,
            "mean": round(sum(values) / len(values), 3) if values else 0.0}


def _slope_per_hour(points: List[Tuple[float, float]]) -> float:
    """Least-squares slope of (seconds, value) points, per hour."""
    n = len(points)
    if n < 3:
        return 0.0
    mx = sum(p[0] for p in points) / n
    my = sum(p[1] for p in points) / n
    den = sum((p[0] - mx) ** 2 for p in points)
    return (sum((p[0] - mx) * (p[1] - my) for p in points) / den) * 3600.0 if den else 0.0


def analyze(out: str) -> Dict[str, Any]:
    spec = json.load(open(os.path.join(out, "spec.json"), encoding="utf-8"))
    scale = float(spec["scale"])
    rows: List[Dict[str, Any]] = []
    for name in sorted(os.listdir(out)):
        if name.startswith("clients-") and name.endswith(".jsonl"):
            rows += read_jsonl(os.path.join(out, name))
    calls = [r for r in rows if r.get("k") == "call"]
    client_samples = [r for r in rows if r.get("k") in ("client_sample", "client_end")]
    events = read_jsonl(os.path.join(out, "fakes-events.jsonl"))
    samples = read_jsonl(os.path.join(out, "samples.jsonl"))
    report: Dict[str, Any] = {"profile": spec["name"], "scale": scale, "agents": spec["agents"]["count"],
                              "members": spec["members"]["count"], "duration_s": spec["duration_s"]}

    t_first_call = min((c["t_call"] for c in calls), default=0.0)
    t_last_end = max((c.get("t_end", 0.0) for c in calls), default=0.0)
    span = max(1e-6, t_last_end - t_first_call)
    ok = [c for c in calls if c.get("outcome") == "ok"]
    outcomes: Dict[str, int] = {}
    for c in calls:
        outcomes[c.get("outcome", "?")] = outcomes.get(c.get("outcome", "?"), 0) + 1
    report["calls"] = {"total": len(calls), "outcomes": outcomes}
    report["throughput"] = {"span_s": round(span, 1), "calls_per_s": round(len(ok) / span, 3),
                            "calls_per_min": round(len(ok) / span * 60, 1),
                            "out_tokens_per_s": round(sum(c.get("otok", 0) for c in ok) / span, 1),
                            "out_tokens_per_s_real_equivalent": round(sum(c.get("otok", 0) for c in ok) / span / scale, 1)}

    # ---- latency per tier
    tiers: Dict[str, Any] = {}
    for tier in sorted({c["tier"] for c in calls}):
        mine = [c for c in calls if c["tier"] == tier]
        done = [c for c in mine if c.get("outcome") == "ok"]
        ttft = [c["t_first"] - c["t_call"] for c in done if "t_first" in c]
        wait = [c["recv"] - c["t_call"] for c in done if c.get("recv")]
        tiers[tier] = {"calls": len(mine), "ok": len(done),
                       "not_ok": {k: v for k, v in _count(c.get("outcome", "?") for c in mine).items() if k != "ok"},
                       "ttft_s": _dist(ttft), "queue_wait_s": _dist(wait),
                       "ttft_real_equiv_s": _dist([v * scale for v in ttft]),
                       "queue_wait_real_equiv_s": _dist([v * scale for v in wait])}
    report["tiers"] = tiers
    trial_waits = [c["recv"] - c["t_call"] for c in ok if c["tier"] == "trial" and c.get("recv")]
    trial_total = [c for c in calls if c["tier"] == "trial"]
    bound = float(spec.get("starvation_bound_s", 300.0))
    worst_real = max(trial_waits, default=0.0) * scale
    never = [c for c in trial_total if c.get("outcome") in ("gave_up", "failed", "exception")]
    report["starvation"] = {"bound_real_equiv_s": bound, "trial_calls": len(trial_total),
                            "worst_wait_real_equiv_s": round(worst_real, 1), "trial_never_served": len(never),
                            "within_bound": worst_real <= bound and not never}

    # ---- callers that are not the extension (no X-DLNE-Session): they must never see a queue frame
    generic = [c for c in calls if c.get("generic")]
    report["generic_clients"] = {
        "calls": len(generic), "ok": sum(1 for c in generic if c.get("outcome") == "ok"),
        "dlne_queue_frames_seen": sum(c.get("generic_frames", 0) for c in generic),
        "frames_seen_by_extension_agents": sum(len(c.get("frames", [])) for c in calls if not c.get("generic")),
        "queue_wait_s": _dist([c["recv"] - c["t_call"] for c in generic if c.get("outcome") == "ok" and c.get("recv")]),
        "served_by": _count(c.get("served_by") or "?" for c in generic if c.get("outcome") == "ok")}

    # ---- who served
    served: Dict[str, int] = {}
    for c in ok:
        served[c.get("served_by") or "?"] = served.get(c.get("served_by") or "?", 0) + 1
    report["served_by"] = dict(sorted(served.items()))
    member_serves = sum(v for k, v in served.items() if k.startswith("m"))
    report["member_share"] = round(member_serves / max(1, len(ok)), 3)

    # ---- sticky
    report["sticky"] = _sticky(calls, events)

    # ---- re-routing, resent envelopes, wrong members
    report["reroute"] = _reroute(calls, events)

    # ---- cancel
    report["cancel"] = _cancel(calls, events)

    # ---- place in line
    report["place_in_line"] = _place_accuracy(calls)

    # ---- coordinator over time
    report["coordinator"] = _timeseries(samples, spec)
    report["clients_side"] = _client_side(client_samples)
    report["fakes_side"] = _fakes_side(out)
    report["member_utilization"] = _member_utilization(out, samples)

    # ---- the end state
    final_path = os.path.join(out, "final.json")
    if os.path.exists(final_path):
        final = json.load(open(final_path, encoding="utf-8"))
        report["final"] = final.get("metrics")
        report["final_status"] = {k: (final.get("status") or {}).get(k) for k in ("coding_queue", "coding_members")}
    report["log_errors"] = _log_errors(os.path.join(out, "coordinator.log"))
    report["degradation"] = _degradation(calls, samples, spec)
    return report


def _count(items) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for item in items:
        out[item] = out.get(item, 0) + 1
    return out


def _sticky(calls: List[Dict[str, Any]], events: List[Dict[str, Any]]) -> Dict[str, Any]:
    gone: List[Tuple[float, str]] = sorted((e["t"], e["who"]) for e in events if e["k"] == "member" and e["op"] in ("leave", "die"))
    by_session: Dict[str, List[Dict[str, Any]]] = {}
    for c in calls:
        if c.get("outcome") == "ok" and c.get("served_by"):
            by_session.setdefault(c["sid"], []).append(c)
    pairs = hits = pairs_clean = hits_clean = cache_hits = member_later = 0
    for seq in by_session.values():
        seq.sort(key=lambda c: c["call"])
        for prev, cur in zip(seq, seq[1:]):
            if not prev["served_by"].startswith("m"):
                continue
            pairs += 1
            hit = cur["served_by"] == prev["served_by"]
            hits += hit
            left = any(prev["t_end"] <= t <= cur["t_call"] and who == prev["served_by"] for t, who in gone)
            if not left:
                pairs_clean += 1
                hits_clean += hit
            if cur["served_by"].startswith("m"):
                member_later += 1
                if (cur.get("cached") or 0) > 0:
                    cache_hits += 1
    return {"pairs": pairs, "hit_rate": round(hits / pairs, 3) if pairs else None,
            "pairs_member_stayed": pairs_clean, "hit_rate_member_stayed": round(hits_clean / pairs_clean, 3) if pairs_clean else None,
            "prompt_cache_hit_rate": round(cache_hits / member_later, 3) if member_later else None}


def _reroute(calls: List[Dict[str, Any]], events: List[Dict[str, Any]]) -> Dict[str, Any]:
    failed_first = recovered = 0
    attempt_kinds = _count(a.get("kind", "?") for c in calls for a in c["attempts"])
    for c in calls:
        routed = [a for a in c["attempts"] if a.get("route")]
        if any(a.get("kind") in ("member_failure", "member_answer_error", "loading", "socket") for a in routed):
            failed_first += 1
            recovered += c.get("outcome") == "ok"
    epks = _count(e["epk"] for e in events if e["k"] == "req" and e.get("epk"))
    resent = sum(1 for n in epks.values() if n > 1)
    member_of: Dict[str, set] = {}
    for e in events:
        if e["k"] == "req" and e.get("epk"):
            member_of.setdefault(e["epk"], set()).add(e["who"])
    to_two_members = sum(1 for m in member_of.values() if len(m) > 1)
    unopened = [e for e in events if e["k"] == "req" and e.get("sealed") and e.get("opened") is False]
    codes = _count(e.get("code", "?") for e in unopened)
    wrong_rid = sum(1 for c in calls if c.get("wrong_rid"))
    sealed_to_member = sum(1 for e in events if e["k"] == "req" and e.get("sealed"))
    plain_to_member = sum(1 for e in events if e["k"] == "req" and e["who"].startswith("m") and not e.get("sealed"))
    rej = _count(e.get("why", "?") for e in events if e["k"] == "rej")
    return {"calls_with_a_routed_attempt_that_failed": failed_first, "of_which_recovered": recovered,
            "recovery_rate": round(recovered / failed_first, 3) if failed_first else None,
            "attempt_kinds": attempt_kinds, "sealed_requests_seen_by_members": sealed_to_member,
            "plain_requests_seen_by_members": plain_to_member,
            "envelopes_sent_twice": resent, "envelopes_seen_by_two_members": to_two_members,
            "envelopes_not_opened_by_code": codes, "wrong_member_deliveries_auth_failures": codes.get("auth", 0),
            "answers_for_another_request": wrong_rid, "member_rejections": rej}


def _cancel(calls: List[Dict[str, Any]], events: List[Dict[str, Any]]) -> Dict[str, Any]:
    cancelled = [c for c in calls if c.get("t_cancel")]
    gone: Dict[str, float] = {}
    for e in events:
        if e["k"] == "gone" and e.get("rid"):
            gone.setdefault(e["rid"], e["t"])
    started: Dict[str, float] = {}
    for e in events:
        if e["k"] == "req" and e.get("rid"):
            started.setdefault(e["rid"], e["t"])
    delays = []
    reached = 0
    for c in cancelled:
        if c["rid"] in started and started[c["rid"]] < c["t_cancel"]:
            reached += 1
            if c["rid"] in gone and gone[c["rid"]] >= c["t_cancel"] - 0.05:
                delays.append(gone[c["rid"]] - c["t_cancel"])
    unfinished = [e for e in events if e["k"] == "req" and e.get("rid")]
    fin = {e["rid"] for e in events if e["k"] == "fin" and e.get("rid")}
    started_not_finished = len({e["rid"] for e in unfinished} - fin)
    return {"cancelled_calls": len(cancelled), "cancelled_after_reaching_an_engine": reached,
            "upstream_close_seen_after_cancel_s": _dist(delays), "engine_requests_never_finished": started_not_finished}


def _place_accuracy(calls: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Against the truth in the 'today' profile: the frames' ``position`` versus the real place at that instant
    (calls that arrived earlier with a better tier and were not yet served)."""
    prio = {"key": 0, "gpu": 0, "cpu": 1, "trial": 2}
    members_run = any((c.get("served_by") or "").startswith("m") for c in calls)
    arrivals = []
    for c in calls:
        grant = c.get("recv") or c.get("t_cancel") or c.get("t_end")
        if grant is None:
            continue
        arrivals.append((c["t_call"], grant, prio.get(c["tier"], 2), c["rid"]))
    exact = within1 = total = 0
    errors: List[float] = []
    eta_err: List[float] = []
    stream = poll = 0
    for c in calls:
        if not c.get("recv"):
            continue
        me = (prio.get(c["tier"], 2), c["t_call"])
        for sample_ in c["frames"]:
            t, pos = sample_[0], sample_[1]
            eta = sample_[5]
            if sample_[6] == "stream":
                stream += 1
            if pos is None:
                continue
            truth = 1 + sum(1 for (arr, grant, pr, rid) in arrivals
                            if rid != c["rid"] and arr <= t < grant and (pr, arr) < me)
            if not members_run:
                total += 1
                exact += truth == pos
                within1 += abs(truth - pos) <= 1
                errors.append(abs(truth - pos))
            if eta:
                eta_err.append((eta - (c["recv"] - t) * 1.0) / max(0.5, (c["recv"] - t)))
        for sample_ in c["polls"]:
            poll += 1
    out = {"frames_in_stream": stream, "status_polls": poll, "scored_in_this_run": not members_run}
    if total:
        out.update(frames_scored=total, exact=round(exact / total, 3), within_one=round(within1 / total, 3),
                   mean_abs_error=round(sum(errors) / total, 2), max_abs_error=max(errors))
    if eta_err:
        out["eta_relative_error_median"] = round(percentile(eta_err, 50), 2)
    return out


def _timeseries(samples: List[Dict[str, Any]], spec: Dict[str, Any]) -> Dict[str, Any]:
    rows = [s for s in samples if s.get("metrics")]
    if len(rows) < 2:
        return {"samples": len(rows)}
    t0 = rows[0]["t"]
    cpu_points, rss_points, fd_points, lag_p99, lag_max, status_ms, thread_points, task_points = [], [], [], [], [], [], [], []
    series = []
    prev = None
    for s in rows:
        p = s["procs"].get("coordinator") or s["metrics"].get("proc") or {}
        if not p:
            continue
        cpu_pct = None
        if prev is not None and s["t"] > prev["t"]:
            cpu_pct = (p["cpu_s"] - prev["cpu_s"]) / (s["t"] - prev["t"]) * 100.0
        prev = {"t": s["t"], **p}
        lag = s["metrics"].get("lag") or {}
        point = {"t": round(s["t"] - t0, 1), "cpu_pct": round(cpu_pct, 1) if cpu_pct is not None else None,
                 "rss_mb": round(p["rss_kb"] / 1024, 1), "fds": p["fds"], "threads": p["threads"],
                 "lag_p99_ms": lag.get("p99"), "lag_max_ms": lag.get("max"), "status_ms": s.get("status_ms"),
                 "tasks": s["metrics"].get("tasks"), "gate_in_use": s["metrics"]["gate"]["in_use"],
                 "gate_waiting": s["metrics"]["gate"]["waiting"], "gate_heap": s["metrics"].get("gate_heap"),
                 "router": s["metrics"].get("router")}
        series.append(point)
        if cpu_pct is not None:
            cpu_points.append(cpu_pct)
        rss_points.append((s["t"] - t0, p["rss_kb"] / 1024))
        fd_points.append((s["t"] - t0, p["fds"]))
        thread_points.append(p["threads"])
        task_points.append(s["metrics"].get("tasks") or 0)
        if lag.get("n"):
            lag_p99.append(lag["p99"])
            lag_max.append(lag["max"])
        if s.get("status_ms") is not None:
            status_ms.append(s["status_ms"])
    third = max(1, len(rss_points) // 3)
    late = rss_points[third:]
    out = {"samples": len(series), "cpu_pct": _dist(cpu_points), "rss_mb_start": rss_points[0][1], "rss_mb_end": rss_points[-1][1],
           "rss_mb_max": max(p[1] for p in rss_points), "rss_slope_mb_per_hour_after_first_third": round(_slope_per_hour(late), 1),
           "fds_start": fd_points[0][1], "fds_end": fd_points[-1][1], "fds_max": max(p[1] for p in fd_points),
           "fds_slope_per_hour_after_first_third": round(_slope_per_hour(fd_points[third:]), 1),
           "threads_max": max(thread_points), "tasks_max": max(task_points),
           "loop_lag_p99_ms": _dist(lag_p99), "loop_lag_max_ms": max(lag_max, default=0.0),
           "status_endpoint_ms": _dist(status_ms), "series": series[:: max(1, len(series) // 120)]}
    last_router = next((p["router"] for p in reversed(series) if p.get("router")), None)
    out["router_tables_at_end"] = last_router
    return out


def _member_utilization(out: str, samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    """How busy the members' slots really were, in total and while requests were waiting for a member slot. Slots that sit
    empty while someone waits are lost throughput (a freed slot the router does not hand on)."""
    stats = [r for r in read_jsonl(os.path.join(out, "fakes-stats.jsonl")) if r.get("members")]
    if not stats:
        return {}

    def busy_fraction(row: Dict[str, Any]) -> float:
        slots = sum(2 for _m in row["members"])
        return sum(m["inflight"] for m in row["members"]) / slots if slots else 0.0

    overall = [busy_fraction(r) for r in stats]
    waiting_rows = []
    for s in samples:
        router = (s.get("metrics") or {}).get("router") or {}
        if router.get("waiters_live", 0) > 0:
            nearest = min(stats, key=lambda r: abs(r["t"] - s["t"]))
            if abs(nearest["t"] - s["t"]) < 3.0:
                waiting_rows.append(busy_fraction(nearest))
    return {"slot_busy_fraction_mean": round(sum(overall) / len(overall), 3),
            "samples_with_member_waiters": len(waiting_rows),
            "slot_busy_fraction_while_requests_wait": round(sum(waiting_rows) / len(waiting_rows), 3) if waiting_rows else None}


def _client_side(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    lags = [r["lag"] for r in rows if r.get("lag") and r["lag"].get("n")]
    return {"loop_lag_p99_ms_max": max((x["p99"] for x in lags), default=0.0), "loop_lag_max_ms": max((x["max"] for x in lags), default=0.0),
            "note": "if this is large the agents' own process was the bottleneck, not the coordinator"}


def _fakes_side(out: str) -> Dict[str, Any]:
    rows = read_jsonl(os.path.join(out, "fakes-stats.jsonl"))
    lags = [r["lag"] for r in rows if r.get("lag") and r["lag"].get("n")]
    final_path = os.path.join(out, "fakes-final.json")
    final = json.load(open(final_path, encoding="utf-8")) if os.path.exists(final_path) else {}
    answers: Dict[str, int] = {}
    for member_answers in (final.get("heartbeat_answers") or {}).values():
        for key, n in member_answers.items():
            answers[key] = answers.get(key, 0) + n
    return {"loop_lag_p99_ms_max": max((x["p99"] for x in lags), default=0.0),
            "local_engine": final.get("local"), "heartbeat_answers_seen": answers}


def _log_errors(path: str) -> Dict[str, Any]:
    counts: Dict[str, int] = {}
    first: Dict[str, str] = {}
    try:
        with open(path, "r", errors="replace") as handle:
            for line in handle:
                for word in ("Traceback", "CRITICAL", "Error", "Exception"):
                    if word in line:
                        key = line.strip()[:110]
                        counts[key] = counts.get(key, 0) + 1
                        first.setdefault(key, key)
                        break
    except OSError:
        return {}
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:8]
    return {"distinct": len(counts), "top": top}


def _degradation(calls: List[Dict[str, Any]], samples: List[Dict[str, Any]], spec: Dict[str, Any]) -> Dict[str, Any]:
    """Bins of 20 s: calls started per second against the coordinator's lag and the route and status latency."""
    if not calls:
        return {}
    t0 = min(c["t_call"] for c in calls)
    width = 20.0
    bins: Dict[int, Dict[str, Any]] = {}
    for c in calls:
        b = int((c["t_call"] - t0) // width)
        entry = bins.setdefault(b, {"started": 0, "route_ms": [], "ttft": []})
        entry["started"] += 1
        entry["route_ms"] += c.get("route_ms", [])
        if c.get("t_first"):
            entry["ttft"].append(c["t_first"] - c["t_call"])
    lag_by_bin: Dict[int, List[float]] = {}
    status_by_bin: Dict[int, List[float]] = {}
    cpu_by_bin: Dict[int, List[float]] = {}
    prev = None
    for s in samples:
        if not s.get("metrics"):
            continue
        b = int((s["t"] - t0) // width)
        lag = (s["metrics"].get("lag") or {})
        if lag.get("n"):
            lag_by_bin.setdefault(b, []).append(lag["p99"])
        if s.get("status_ms") is not None:
            status_by_bin.setdefault(b, []).append(s["status_ms"])
        p = s["procs"].get("coordinator")
        if p and prev and s["t"] > prev["t"]:
            cpu_by_bin.setdefault(b, []).append((p["cpu_s"] - prev["cpu_s"]) / (s["t"] - prev["t"]) * 100)
        if p:
            prev = {"t": s["t"], **p}
    table = []
    knee = None
    for b in sorted(bins):
        e = bins[b]
        row = {"t_s": int(b * width), "calls_started_per_s": round(e["started"] / width, 2),
               "route_p95_ms": round(percentile(e["route_ms"], 95), 1) if e["route_ms"] else None,
               "ttft_p95_s": round(percentile(e["ttft"], 95), 2) if e["ttft"] else None,
               "lag_p99_ms": round(max(lag_by_bin.get(b, [0.0])), 1), "status_ms_p95": round(percentile(status_by_bin.get(b, []), 95), 1),
               "cpu_pct": round(sum(cpu_by_bin.get(b, [0])) / max(1, len(cpu_by_bin.get(b, [0]))), 1)}
        table.append(row)
        if knee is None and (row["lag_p99_ms"] > 100.0 or (row["route_p95_ms"] or 0) > 500.0 or row["status_ms_p95"] > 1000.0):
            knee = row
    return {"bins": table, "first_bin_degraded": knee,
            "definition": "degraded = event-loop lag p99 over 100 ms, or route lookup p95 over 500 ms, or status p95 over 1 s"}


def format_report(r: Dict[str, Any]) -> str:
    lines = [f"# Coding load run: {r['profile']}", "",
             f"agents {r['agents']}, members {r['members']}, scale x{r['scale']:g}, {r['duration_s']} s of load", ""]
    c = r["calls"]
    lines += [f"calls {c['total']}: {c['outcomes']}", "", "## Throughput", "",
              f"{r['throughput']['calls_per_s']} calls/s ({r['throughput']['calls_per_min']}/min), "
              f"{r['throughput']['out_tokens_per_s']} output tokens/s (x{r['scale']:g} slower at real speed: "
              f"{r['throughput']['out_tokens_per_s_real_equivalent']} tok/s)", "",
              f"member share of served calls: {r['member_share']}; served by {r['served_by']}", "",
              "## Latency per tier (seconds, wall clock at this scale | real-speed equivalent = x scale)", "",
              "| tier | calls | ok | TTFT p50 | p95 | p99 | queue p50 | p95 | p99 | max | real-eq TTFT p95 | real-eq queue p95 |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for tier, t in r["tiers"].items():
        lines.append(f"| {tier} | {t['calls']} | {t['ok']} | {t['ttft_s']['p50']} | {t['ttft_s']['p95']} | {t['ttft_s']['p99']} | "
                     f"{t['queue_wait_s']['p50']} | {t['queue_wait_s']['p95']} | {t['queue_wait_s']['p99']} | {t['queue_wait_s']['max']} | "
                     f"{t['ttft_real_equiv_s']['p95']} | {t['queue_wait_real_equiv_s']['p95']} |")
    s = r["starvation"]
    lines += ["", f"Starvation: trial calls {s['trial_calls']}, worst wait {s['worst_wait_real_equiv_s']} s real-equivalent "
              f"(bound {s['bound_real_equiv_s']} s), never served {s['trial_never_served']}, within bound: {s['within_bound']}", "",
              f"Callers without X-DLNE-Session: {r.get('generic_clients')}", "",
              "## Sticky routing", "", f"{r['sticky']}", "", "## Re-routing and sealed envelopes", "", f"{r['reroute']}", "",
              "## Stop / cancel", "", f"{r['cancel']}", "", "## Place in line", "", f"{r['place_in_line']}", ""]
    co = r["coordinator"]
    if co.get("samples", 0) > 1:
        lines += ["## Coordinator", "", f"CPU % {co['cpu_pct']}", f"RSS MB {co['rss_mb_start']} -> {co['rss_mb_end']} (max {co['rss_mb_max']}, "
                  f"slope after the first third {co['rss_slope_mb_per_hour_after_first_third']} MB/h)",
                  f"open files {co['fds_start']} -> {co['fds_end']} (max {co['fds_max']}, slope {co['fds_slope_per_hour_after_first_third']}/h); "
                  f"threads max {co['threads_max']}; asyncio tasks max {co['tasks_max']}",
                  f"event-loop lag p99 per sample {co['loop_lag_p99_ms']}, worst single {co['loop_lag_max_ms']} ms",
                  f"/v1/cluster/status latency ms {co['status_endpoint_ms']}", f"router tables at the end {co.get('router_tables_at_end')}", ""]
    lines += ["## Member slots", "", f"{r.get('member_utilization')}", ""]
    lines += ["## Agents' and fakes' own loops (if large, the rig was the limit)", "", f"{r['clients_side']}", f"{r['fakes_side']}", "",
              "## Degradation", "", f"{r['degradation'].get('definition')}", f"first degraded bin: {r['degradation'].get('first_bin_degraded')}", ""]
    for row in r["degradation"].get("bins", []):
        lines.append(f"- {row}")
    lines += ["", "## End state", "", f"{r.get('final_status')}", f"coordinator log problems: {r.get('log_errors')}"]
    return "\n".join(lines) + "\n"


# ======================================================================================
# Command line
# ======================================================================================

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="run a profile")
    run.add_argument("profile")
    run.add_argument("--out")
    run.add_argument("--repo", default=REPO, help="the checkout whose coordinator is tested")
    run.add_argument("--agents", type=int)
    run.add_argument("--members", type=int)
    run.add_argument("--duration", type=int)
    run.add_argument("--scale", type=float)
    run.add_argument("--set", action="append", default=[], metavar="KEY=JSON",
                     help="override a spec value, e.g. --set agents.tool_gap_s=1.0 --set local.slots=2")
    report = sub.add_parser("report", help="analyse a run folder again")
    report.add_argument("dir")
    sub.add_parser("profiles")
    for role in ("_coordinator", "_fakes", "_clients"):
        p = sub.add_parser(role)
        p.add_argument("--spec", required=True)
        p.add_argument("--out", required=True)
        p.add_argument("--shard", type=int, default=0)
        p.add_argument("--t0", type=float, default=0.0)
    args = parser.parse_args(argv)
    if args.cmd == "_coordinator":
        coordinator_main(args)
    elif args.cmd == "_fakes":
        fakes_main(args)
    elif args.cmd == "_clients":
        clients_main(args)
    elif args.cmd == "profiles":
        for name, over in PROFILES.items():
            print(f"{name}: {json.dumps(over)[:300]}")
    elif args.cmd == "report":
        rep = analyze(args.dir)
        write_json(os.path.join(args.dir, "report.json"), rep)
        text = format_report(rep)
        with open(os.path.join(args.dir, "report.md"), "w", encoding="utf-8") as handle:
            handle.write(text)
        print(text)
    elif args.cmd == "run":
        overrides: Dict[str, Any] = {}
        if args.agents:
            overrides.setdefault("agents", {})["count"] = args.agents
        if args.members is not None:
            overrides.setdefault("members", {})["count"] = args.members
        if args.duration:
            overrides["duration_s"] = args.duration
        if args.scale:
            overrides["scale"] = args.scale
        for item in args.set:
            key, _, value = item.partition("=")
            node = overrides
            parts = key.split(".")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            try:
                node[parts[-1]] = json.loads(value)
            except ValueError:
                node[parts[-1]] = value
        spec = make_spec(args.profile, overrides)
        out = args.out or os.path.join(os.environ.get("TMPDIR", "/tmp"), f"dlne-load-{args.profile}-{int(time.time())}")
        result = run_profile(spec, out, repo=args.repo)
        print(format_report(result["report"]))
        print(f"[load] results in {out}; notes: {result['notes']}; drained: {result.get('drained')}; "
              f"coordinator exit: {result.get('coordinator_exit')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
