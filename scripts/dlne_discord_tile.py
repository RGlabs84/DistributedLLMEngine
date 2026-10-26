#!/usr/bin/env python3
"""
DLNE Swarm Live Discord Telemetry Tile Daemon.
Runs 24/7 on TheWall coordinator and maintains a real-time updating
embed in the #dlne Discord channel via BarrkBOT.
"""

import os
import sys
import time
import json
import subprocess
import urllib.request
import urllib.error
from typing import Dict, Any, Optional

CHANNEL_ID = "1518133249799487620"  # #dlne channel
STATE_FILE = os.path.expanduser("~/DLNEv2/config/discord_live_tile.json")
COORDINATOR_URL = "http://127.0.0.1:11435"
BARRKBOT_DIR = os.path.expanduser("~/BarrkBOT")
POLL_INTERVAL_SECONDS = 30

_ext_version_cache = {"version": "1.0.26", "ts": 0.0}

def get_vscode_extension_version() -> str:
    now = time.time()
    if (now - _ext_version_cache["ts"] < 300) and _ext_version_cache["version"]:
        return _ext_version_cache["version"]

    try:
        from pathlib import Path
        for candidate in [
            Path(__file__).resolve().parent.parent / "vscode-extension" / "package.json",
            Path.home() / "DLNEv2" / "vscode-extension" / "package.json",
            Path.home() / "WubarrkCODING" / "WindowsDEV" / "DistributedLLMEngine" / "vscode-extension" / "package.json"
        ]:
            if candidate.exists():
                with open(candidate, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if data.get("version"):
                        _ext_version_cache["version"] = data["version"]
                        _ext_version_cache["ts"] = now
                        return data["version"]
    except Exception:
        pass

    try:
        req = urllib.request.Request(
            "https://open-vsx.org/api/dlne/dlne-code-assistant",
            headers={"User-Agent": "DLNE-Tile/1.0", "Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("version"):
                    _ext_version_cache["version"] = data["version"]
                    _ext_version_cache["ts"] = now
                    return data["version"]
    except Exception:
        pass

    return _ext_version_cache["version"] or "1.0.26"


def get_cluster_status() -> Optional[Dict[str, Any]]:
    """Query Coordinator cluster status endpoint locally on loopback."""
    url = f"{COORDINATOR_URL}/v1/cluster/status"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        pass
    return None


def run_barrkbot_cmd(cmd_args: list) -> Dict[str, Any]:
    """Execute a BarrkBOT CLI command with node and its environment."""
    node_cmd = [
        "node",
        "--env-file=/home/wubarrk/BarrkBOT/.env",
        "/home/wubarrk/BarrkBOT/src/cli/index.js",
        *cmd_args
    ]
    try:
        res = subprocess.run(
            node_cmd,
            cwd=BARRKBOT_DIR,
            capture_output=True,
            text=True,
            timeout=20
        )
        # Parse JSON block from stdout
        start_idx = res.stdout.find("{")
        end_idx = res.stdout.rfind("}")
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            try:
                return json.loads(res.stdout[start_idx:end_idx + 1])
            except Exception as e:
                print(f"[Tile] JSON parse error: {e}", file=sys.stderr)
        if res.returncode != 0:
            print(f"[Tile] BarrkBOT error (code {res.returncode}): {res.stderr[:200]}", file=sys.stderr)
    except Exception as exc:
        print(f"[Tile] Subprocess execution failed: {exc}", file=sys.stderr)
    return {"ok": False}


def load_state() -> Dict[str, Any]:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_state(state: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def build_tile_fields(status: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Construct embed title, color, and fields according to live cluster metrics."""
    if not status:
        return {
            "title": "⚡ DLNE Swarm Cluster Telemetry — Offline",
            "description": "🔴 *The DLNE Coordinator service is currently unreachable or restarting on TheWall.*",
            "color": "#ED4245",
            "fields": [
                ("Cluster State", "🔴 Offline / Restarting", True),
                ("Host", "TheWall (Dual BC-160)", True),
                ("Notice", "Coordinator process is not responding on 127.0.0.1:11435.", False)
            ]
        }

    is_swapping = status.get("is_swapping_models", False)
    loading = status.get("model_loading") or {}
    is_loading = loading.get("is_loading", False)
    workers_cnt = status.get("active_workers_count", 0)
    endpoints = status.get("active_endpoints") or []
    vram = status.get("total_vram_gb", 0.0)
    sysram = status.get("total_sysram_gb", 0.0)
    mem_total = status.get("total_memory_gb", vram + sysram)
    model = status.get("global_active_model") or "None"
    version = status.get("server_version", "1.1.10")
    leases = status.get("active_leases_count", 0)

    # Determine status & theme color
    if is_loading:
        pct = loading.get("progress_pct", 0)
        state_str = f"⏳ Syncing Shards ({pct:.0f}%)"
        color = "#FFA500"  # Orange
    elif is_swapping:
        state_str = "🔄 Swapping Active Model"
        color = "#9B59B6"  # Purple
    elif workers_cnt > 0:
        state_str = "🟢 Online (Distributed Active)"
        color = "#00F0FF"  # Cyan
    else:
        state_str = "🟡 Standby (Waiting for Workers)"
        color = "#FEE75C"  # Yellow

    # Format nodes
    rig_lines = []
    has_local = any("127.0.0.1" in ep for ep in endpoints)
    if has_local or workers_cnt > 0:
        rig_lines.append("• **TheWall** (Primary): Dual AMD BC-160 Navi 12 (16 GB HBM2) [Vulkan]")
    
    remote_nodes = [ep for ep in endpoints if "127.0.0.1" not in ep]
    if remote_nodes:
        for ep in remote_nodes:
            rig_lines.append(f"• **Remote Worker** (`{ep}`): Active GPU Compute Node")
    elif workers_cnt <= 1:
        rig_lines.append("• *Connect your worker node to contribute GPU VRAM!*")

    # Format loading row or sync state
    if is_loading:
        speed = loading.get("speed_mb_s", 0)
        eta = loading.get("eta_seconds", 0)
        sync_val = f"Downloading/distributing weights: {pct:.1f}% @ {speed:.1f} MB/s (ETA: {eta}s)"
    else:
        sync_val = f"Ready in Distributed VRAM (100% • {vram:.1f} GB resident)"

    ext_ver = get_vscode_extension_version()

    fields = [
        ("🟢 Swarm Health", state_str, True),
        ("Active Frontier Model",f"`{model}`", True),
        ("⚡ Speculative Draft Engine", "0.5B Q8_0 on Vulkan0 (Drafts 5 tok/step)", True),
        ("📊 Total Compute Pool", f"**{vram:.1f} GB VRAM** ({mem_total:.1f} GB Total)", True),
        ("🖥️ Compute Workers in Pool", f"**{workers_cnt} Node(s)** Active", True),
        ("🔒 Active Inference Leases", f"{leases} In-Flight ({'Locked' if leases > 0 else 'Idle / Unlocked'})", True),
        ("🌐 Connected Swarm Rigs", "\n".join(rig_lines), False),
        ("⏳ Model Sync Status", sync_val, False),
        ("🚀 Distributed Transport", "Bi-directional WebSocket RPC Reverse Tunneling (TCP_NODELAY)", False),
        ("📦 Official Software", f"Engine `v{version}` • VS Code Extension `v{ext_ver}` ([Open VSX](https://open-vsx.org/extension/dlne/dlne-code-assistant))", False),
    ]

    return {
        "title": "⚡ DLNE Swarm Cluster Telemetry — Live Monitor",
        "description": "*Live distributed compute metrics for the official DLNE Public Swarm.*",
        "color": color,
        "fields": fields
    }


def update_tile_once() -> None:
    state = load_state()
    message_id = state.get("message_id")
    status = get_cluster_status()
    tile_data = build_tile_fields(status)

    cmd_args = [
        "--title", tile_data["title"],
        "--description", tile_data["description"],
        "--color", tile_data["color"],
        "--footer", "DLNE Swarm Monitor • Give compute to receive compute • Auto-updates 24/7",
        "--timestamp"
    ]
    for name, val, is_inline in tile_data["fields"]:
        clean_val = val.replace("|", "/")
        cmd_args.extend(["--field", f"{name}|{clean_val}|{'true' if is_inline else 'false'}"])

    if message_id:
        # Edit existing message
        edit_args = ["edit", CHANNEL_ID, message_id, *cmd_args]
        res = run_barrkbot_cmd(edit_args)
        if res.get("ok"):
            return
        print(f"[Tile] Failed to edit message {message_id}, will re-create: {res}", file=sys.stderr)
        state["message_id"] = None

    # Send new message and save messageId
    send_args = ["send", CHANNEL_ID, *cmd_args]
    res = run_barrkbot_cmd(send_args)
    if res.get("ok") and res.get("data", {}).get("messageId"):
        new_msg_id = res["data"]["messageId"]
        state["message_id"] = new_msg_id
        save_state(state)
        print(f"[Tile] Created live tile in #{CHANNEL_ID} -> message ID {new_msg_id}")


def main():
    print("[Tile] Starting DLNE Swarm Discord Live Tile daemon on TheWall...")
    while True:
        try:
            update_tile_once()
        except Exception as exc:
            print(f"[Tile] Exception during update: {exc}", file=sys.stderr)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
