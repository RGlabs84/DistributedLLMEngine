"""
Starts the real coordinator (engine/coordinator.py, from the install folder this file is copied into) for the
integration run, with four pieces of test scaffolding around it. Everything the coordinator does with requests,
members, tunnels, queues, canaries and its own engine is its own code; the scaffolding only sets the stage.

    1. NOTHING LEAVES THIS MACHINE. The LAN beacon (UDP broadcast) is switched off, and the canary decoy links
       point at this coordinator instead of the public address.
    2. LOOPBACK VOLUNTEERS COUNT AS FAR AWAY. On one machine every worker is "local" and near, and a near GPU is
       told to join the sharded engine, never to be a coding member. Members listed in ``far_ports`` classify as
       WAN and their link probe measures 120 ms, which is what a volunteer across the internet looks like.
    3. A FAST CANARY CLOCK. The scheduler ticks every half second, sends when the file ``canary_go`` exists in the
       install folder (and never otherwise), and treats ``canary_min_gap`` as the gap between two canaries.
    4. CAPACITY. TheWall's own hybrid worker is what tells the coordinator it has memory to run its engine in;
       a thread here registers one such worker (CPU-only, plenty of RAM) so the coordinator starts its own
       llama-server with the real engine code (InferenceManager) on the CPU.
"""

import asyncio
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    CFG = json.load(handle)

from engine import benchmark, canary  # noqa: E402

# ---- 2. far volunteers -------------------------------------------------------------------------------
FAR_PORTS = {int(p) for p in CFG.get("far_ports", [])}
_classify = benchmark.classify_endpoint


def _classify_endpoint(endpoint):
    _, _, port = str(endpoint).rpartition(":")
    if port.isdigit() and int(port) in FAR_PORTS:
        return "wan"
    return _classify(endpoint)


benchmark.classify_endpoint = _classify_endpoint

# ---- 3. the canary clock -----------------------------------------------------------------------------
canary.PUBLIC_URL = f"http://{CFG['host']}:{CFG['port']}"           # decoy links lead here, not to the internet
canary.MIN_GAP_SECONDS = float(CFG.get("canary_min_gap", 3600.0))
GO_FILE = os.path.join(HERE, "canary_go")


class _GoRng:
    """random() is 0.0 (always send) while the go-file exists, else 1.0 (never)."""

    def random(self):
        return 0.0 if os.path.exists(GO_FILE) else 1.0

    def __getattr__(self, name):
        import random
        return getattr(random.Random(), name)


_Scheduler = canary.CanaryScheduler


class _FastScheduler(_Scheduler):
    def __init__(self, members, forward, **kw):
        kw["interval"] = float(CFG.get("canary_interval", 0.5))
        kw["rng"] = _GoRng()
        super().__init__(members, forward, **kw)


canary.CanaryScheduler = _FastScheduler

from engine import coordinator  # noqa: E402  (after the patches above: it reads them when it starts)

# ---- 1. nothing leaves the machine -------------------------------------------------------------------
coordinator.udp_broadcaster = lambda port: None

# ---- 2 (cont). the link probe says a far member is far ----------------------------------------------
_PROBE_RTT = 120.0
_probe = coordinator._probe_rpc_rtt


def _probe_rpc_rtt(endpoint):
    _, _, port = str(endpoint).rpartition(":")
    if port.isdigit() and int(port) in FAR_PORTS:
        return _PROBE_RTT
    return _probe(endpoint)


coordinator._probe_rpc_rtt = _probe_rpc_rtt
coordinator.link_probe._measure = _probe_rpc_rtt     # the probe was built at import, with the original

# How long a worker nobody has measured is given before the coordinator calls it far and assigns it a coding job
# (90 s in production). Shortened so a run does not spend two minutes per member; a scenario that is about that
# window leaves it alone.
if "role_grace" in CFG:
    from engine import coding_router
    coding_router.ROLE_GRACE_SECONDS = float(CFG["role_grace"])


# ---- 4. capacity -------------------------------------------------------------------------------------
def _capacity_worker():
    """Registered like a heartbeat would (the registry is the coordinator's own); refreshed every 2 s."""
    hardware = {"os": "Linux", "cpu_arch": "x86_64", "cpu_cores": 8, "cpu_threads": 16, "cpu_features": ["avx2"],
                "ram_gb": 16.0, "gpu": {"has_gpu": False, "vram_total_gb": 0, "vram_free_gb": 0, "name": None,
                                        "load_percent": 0.0},
                "gpu_type": "cpu", "is_yielding": False, "mem_bandwidth_gbs": 60.0}
    while True:
        try:
            coordinator.registry.update_worker("it-capacity", "127.0.0.1", int(CFG.get("capacity_port", 59999)),
                                               "it-capacity-token", hardware, role="hybrid")
        except Exception as exc:  # noqa: BLE001
            print(f"capacity worker: {exc}", flush=True)
        time.sleep(2.0)


if CFG.get("capacity", True):
    threading.Thread(target=_capacity_worker, name="it-capacity", daemon=True).start()

if __name__ == "__main__":
    coordinator.start_coordinator_server(host=CFG["host"], port=int(CFG["port"]))
