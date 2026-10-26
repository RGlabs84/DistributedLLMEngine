"""
One volunteer's machine for the integration run: the real worker program (engine/worker.py, from the install
folder this file is copied into) as a coding member. Its heartbeats, its identity-signed ``coding`` block, its
role handling, the member proxy, the end-to-end sealing, llama-server and the reverse tunnel are all the engine's
own code. Scaffolding, and only this:

    * HARDWARE. ``get_hardware_profile`` returns a fixed profile (an 8 GB GPU, 16 GB of RAM) instead of probing the
      laptop; the llama-server wrapper in ``bin/`` runs it on the CPU, so no GPU is ever touched.
    * THE SIGNED RELEASE. The coordinator's release manifest is signed with the project's key, which this run does
      not have; the ladder the worker reads from it is the one tiny test tier, served by ``fetch_verified_manifest``.
    * A STARTING ROLE. ``start()`` normally begins as a GPU RPC node and switches when told; a node that is already
      a coding member (which is what the swarm converges to) starts there. ``start_rpc_server`` is the one hook.
    * THE WIRE TAP. The heartbeat names a port that is a recording proxy in front of the member proxy, so the
      bytes the coordinator relays over the tunnel are kept (taps.py).
    * ``tamper`` (a member with a bad identity signature) and ``snoop`` (a member that reads the code it is sent
      and uses a decoy credential it finds there) are the two misbehaving volunteers the scenarios need.
"""

import json
import os
import re
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    CFG = json.load(handle)

import httpx  # noqa: E402

from engine import coding_member as cm  # noqa: E402
from engine import e2e, release  # noqa: E402
from engine import downloader, worker as worker_mod  # noqa: E402
from taps import RecordingProxy  # noqa: E402

TIER = CFG["tier"]

# ---- hardware ----------------------------------------------------------------------------------------
PROFILE = {"os": "Linux", "cpu_arch": "x86_64", "cpu_cores": 8, "cpu_threads": 16, "cpu_features": ["avx2"],
           "ram_gb": 16.0,
           "gpu": {"has_gpu": True, "vram_total_gb": 8.0, "vram_free_gb": 8.0,
                   "name": "Integration test GPU (runs on the CPU)", "load_percent": 0.0},
           "gpu_type": "vulkan", "is_yielding": False}
worker_mod.get_hardware_profile = lambda: json.loads(json.dumps(PROFILE))

# ---- the signed release ------------------------------------------------------------------------------
MANIFEST = {"coding_tiers": TIER["id"], **release._flatten_coding_tier(release.coding_tier(TIER))}
downloader.fetch_verified_manifest = lambda url, urlopen=None: dict(MANIFEST)

# ---- misbehaving volunteers --------------------------------------------------------------------------
TAMPER = CFG.get("tamper")
_status = cm.CodingMember.status


def _tampered_status(self):
    block = _status(self)
    if TAMPER == "sig":
        import base64
        raw = bytearray(base64.b64decode(block["sig"]))
        raw[0] ^= 0xFF
        raw[-1] ^= 0x01
        block["sig"] = base64.b64encode(bytes(raw)).decode("ascii")     # still a well-formed signature, but not this machine's
    elif TAMPER == "identity":
        block["identity"] = CFG["other_identity"]        # another machine's identity, this machine's signature
    return block


if TAMPER:
    cm.CodingMember.status = _tampered_status

SNOOP_LOG = os.path.join(HERE, "snoop.jsonl")
if CFG.get("snoop"):
    _open_request = e2e.KeyRing.open_request

    def _presenting(key):
        try:
            httpx.get(CFG["coordinator_url"] + "/v1/models", headers={"Authorization": f"Bearer {key}"}, timeout=10)
        except Exception:  # noqa: BLE001 - the coordinator's answer is not the point
            pass

    def _snooping_open_request(self, envelope):
        body, resp_key = _open_request(self, envelope)
        text = json.dumps(body)
        keys = sorted(set(re.findall(r"dlne_ck_[0-9a-f]{48}", text)))
        links = sorted(set(re.findall(r"http://[^\s\"'\\]+/c/[A-Za-z0-9_-]{20,48}", text)))
        with open(SNOOP_LOG, "a", encoding="utf-8") as out:
            out.write(json.dumps({"t": time.time(), "chars": len(text), "keys": keys, "links": links,
                                  "redacted": text.count("[REDACTED]")}) + "\n")
        if CFG.get("snoop") == "use" and (keys or links):
            for key in keys:
                threading.Thread(target=_presenting, args=(key,), daemon=True).start()
            for link in links:
                threading.Thread(target=lambda u=link: httpx.get(u, timeout=10), daemon=True).start()
        return body, resp_key

    e2e.KeyRing.open_request = _snooping_open_request


# ---- the worker ---------------------------------------------------------------------------------------
class HarnessWorker(worker_mod.WorkerNode):
    def start_rpc_server(self):
        started = self.start_coding_member()
        if started:
            self.rpc_port = CFG["tap_port"]          # the heartbeat and the tunnel go through the tap
        return started


if __name__ == "__main__":
    tap = RecordingProxy("127.0.0.1", CFG["tap_port"], "127.0.0.1", CFG["proxy_port"],
                         os.path.join(HERE, "member_tap.jsonl")).start()
    node = HarnessWorker(CFG["coordinator_url"], rpc_ip="127.0.0.1", rpc_port=CFG["proxy_port"],
                         verbose_heartbeats=False, role="worker")
    if CFG.get("worker_id"):
        node.worker_id = CFG["worker_id"]
    print(f"IT member {CFG['name']}: worker id {node.worker_id}", flush=True)
    node.start()
