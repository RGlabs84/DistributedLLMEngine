#!/usr/bin/env python3
"""
DLNE Server Unit — Headless Server & Daemon CLI.

Serves as the main distributed coordinator, OpenAI/FIM API gateway, and API key manager.
Runs seamlessly as a background systemd service on Linux, a Windows Service, or a headless console.

Usage:
    dlne_server.py start [--host 0.0.0.0] [--port 11435] [--hybrid]
    dlne_server.py apikey create [--name vscode-laptop]
    dlne_server.py apikey list
    dlne_server.py apikey revoke <key_id>
    dlne_server.py status
    dlne_server.py install-systemd [--user]
"""

import sys
import os
import argparse
import threading
import httpx
import json

from engine.config import get_config, get_base_dir, set_config_values
from engine.logger import log_coordinator, log_system, log_error
from engine.coordinator import start_coordinator_server
from engine.api_keys import api_key_manager, hash_admin_key, mint_admin_key
from engine.version import VERSION


def cmd_start(args):
    """Start the DLNE Server Unit."""
    host = args.host or get_config().get("BIND_IP", "0.0.0.0")
    port = args.port or int(get_config().get("BIND_PORT", "11435"))
    hybrid = args.hybrid

    log_system(f"Starting DLNE Server Unit v{VERSION} on {host}:{port}")

    if hybrid:
        import time
        import socket
        from engine.worker import WorkerNode
        rpc_ip = args.rpc_ip or get_config().get("RPC_IP", "0.0.0.0")
        rpc_port = args.rpc_port or int(get_config().get("RPC_PORT", "50052"))
        coord_url = f"http://127.0.0.1:{port}"

        def _launch_hybrid_worker():
            # Wait for coordinator HTTP listener to bind before starting worker heartbeats
            for _ in range(30):
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                        break
                except OSError:
                    time.sleep(0.5)

            log_system(f"Launching local hybrid worker compute node on port {rpc_port}...")
            worker = WorkerNode(
                coordinator_url=coord_url,
                rpc_ip=rpc_ip,
                rpc_port=rpc_port,
                verbose_heartbeats=False,
                role="hybrid",
            )
            worker.start()

        t = threading.Thread(target=_launch_hybrid_worker, daemon=True)
        t.start()

    start_coordinator_server(host=host, port=port)


def cmd_apikey(args):
    """Handle API Key operations."""
    action = args.api_action
    if action == "create":
        name = args.name or "vscode-client"
        raw_key, meta = api_key_manager.generate_key(name=name)
        print("\n" + "=" * 60)
        print(f"🔑 DLNE Bearer API Key Created: {name}")
        print("=" * 60)
        print(f"\nAPI Key: {raw_key}\n")
        print("⚠️  Save this key securely! It will not be displayed again.")
        print("Use in VS Code / IDE settings under 'dlne.apiKey'.")
        print("=" * 60 + "\n")
    elif action == "list":
        keys = api_key_manager.list_keys()
        print("\n" + "=" * 70)
        print(f"{'ID':<18} {'NAME':<20} {'PREFIX':<18} {'STATUS':<10}")
        print("-" * 70)
        for k in keys:
            status = "REVOKED" if k.get("revoked") else "ACTIVE"
            print(f"{k['id']:<18} {k['name']:<20} {k['prefix']:<18} {status:<10}")
        print("=" * 70 + "\n")
    elif action == "revoke":
        key_id = args.key_id
        if not key_id:
            print("Error: Specify key_id to revoke: dlne_server.py apikey revoke <key_id>")
            sys.exit(1)
        if api_key_manager.revoke_key(key_id):
            print(f"✅ Successfully revoked API key ID: {key_id}")
        else:
            print(f"❌ Key ID '{key_id}' not found or already revoked.")
            sys.exit(1)


def cmd_adminkey(args):
    """Mint or remove the operator key that opens /v1/admin/* (used by BarrkBOT)."""
    if args.admin_action == "create":
        key = mint_admin_key()
        if not set_config_values({"ADMIN_KEY_HASH": hash_admin_key(key)}):
            print("Error: could not write ADMIN_KEY_HASH to config.txt", file=sys.stderr)
            sys.exit(1)
        if args.quiet:
            # Scripting form: the key and nothing else, so a caller can capture it straight
            # into another service's environment without it ever being shown on a terminal.
            print(key)
            return
        print("\n" + "=" * 60)
        print("🔐 DLNE Operator Key Created")
        print("=" * 60)
        print(f"\nAdmin Key: {key}\n")
        print("⚠️  Save this key now. Only its hash is stored; it cannot be shown again.")
        print("Creating a new key replaces (revokes) the previous one.")
        print("=" * 60 + "\n")
    elif args.admin_action == "revoke":
        if not set_config_values({"ADMIN_KEY_HASH": ""}):
            print("Error: could not write ADMIN_KEY_HASH to config.txt", file=sys.stderr)
            sys.exit(1)
        print("✅ Operator key revoked. /v1/admin/* now accepts only the legacy password session.")


def cmd_status(args):
    """Query live DLNE Server Unit status."""
    host = args.host or "127.0.0.1"
    port = args.port or int(get_config().get("BIND_PORT", "11435"))
    url = f"http://{host}:{port}/v1/cluster/status"

    try:
        resp = httpx.get(url, timeout=5.0)
        if resp.status_code == 200:
            data = resp.json()
            print("\n" + "=" * 60)
            print(f"🌐 DLNE Server Cluster Status (v{data.get('server_version', VERSION)})")
            print("=" * 60)
            print(f"Cluster State:      {data.get('status')}")
            print(f"Active Model:       {data.get('global_active_model')}")
            print(f"Total VRAM:         {data.get('total_vram_gb')} GB")
            print(f"Total SysRAM:       {data.get('total_sysram_gb')} GB")
            print(f"Total Compute Pool: {data.get('total_memory_gb')} GB")
            print(f"Active Workers:     {data.get('active_workers_count')}")
            print(f"In-Flight Leases:   {data.get('active_leases_count')} (locked: {data.get('is_locked_for_edits')})")
            print("=" * 60 + "\n")
        else:
            print(f"Server returned status {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"Could not connect to DLNE Server at {url}: {e}")
        sys.exit(1)


def cmd_worker(args):
    """Start a headless DLNE Worker Node to contribute compute to a Coordinator."""
    coordinator_url = args.coordinator or get_config().get("COORDINATOR_IP", "127.0.0.1:11435")
    if not coordinator_url.startswith("http"):
        coordinator_url = f"http://{coordinator_url}"
    rpc_ip = args.rpc_ip or get_config().get("RPC_IP", "0.0.0.0")
    rpc_port = args.rpc_port or int(get_config().get("RPC_PORT", "50052"))

    updates = {}
    if args.network_key:
        updates["NETWORK_KEY"] = args.network_key
    if args.allowed_gpus:
        updates["ALLOWED_GPU_INDICES"] = args.allowed_gpus
    if args.max_vram:
        updates["MAX_VRAM_GB"] = str(args.max_vram)
    if args.max_ram:
        updates["MAX_RAM_GB"] = str(args.max_ram)
    if updates:
        set_config_values(updates)

    from engine.worker import WorkerNode
    log_system(f"Starting DLNE Worker Node v{VERSION} connecting to {coordinator_url}...")
    worker = WorkerNode(
        coordinator_url=coordinator_url,
        rpc_ip=rpc_ip,
        rpc_port=rpc_port,
        verbose_heartbeats=True,
        role="worker",
    )
    worker.start()


def cmd_install_systemd(args):
    """Generate systemd service unit file."""
    python_path = sys.executable
    script_path = os.path.abspath(__file__)
    work_dir = os.path.dirname(script_path)
    user = os.environ.get("USER", "root")

    service_content = f"""[Unit]
Description=DLNE Distributed LLM Engine Server Unit
After=network.target

[Service]
Type=simple
User={user}
WorkingDirectory={work_dir}
ExecStart={python_path} {script_path} start --host 0.0.0.0 --port 11435 --hybrid
Restart=on-failure
RestartSec=5s
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
"""
    os.makedirs(os.path.join(work_dir, "daemon", "systemd"), exist_ok=True)
    out_file = os.path.join(work_dir, "daemon", "systemd", "dlne.service")
    with open(out_file, "w", encoding="utf-8") as f:
        f.write(service_content)

    print("\n" + "=" * 60)
    print(f"Generated systemd unit file at: {out_file}")
    print("=" * 60)
    print("To install as a system service, run:")
    print(f"  sudo cp {out_file} /etc/systemd/system/dlne.service")
    print("  sudo systemctl daemon-reload")
    print("  sudo systemctl enable --now dlne")
    print("  sudo systemctl status dlne")
    print("=" * 60 + "\n")


def main():
    parser = argparse.ArgumentParser(description="DLNE Server Unit")
    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # start
    p_start = subparsers.add_parser("start", help="Start the DLNE Server")
    p_start.add_argument("--host", default=None, help="Host/IP to bind to")
    p_start.add_argument("--port", type=int, default=None, help="Port to bind to")
    p_start.add_argument("--hybrid", action="store_true", default=True, help="Run local hybrid worker node")
    p_start.add_argument("--rpc-ip", default=None, help="RPC IP for hybrid worker")
    p_start.add_argument("--rpc-port", type=int, default=None, help="RPC Port for hybrid worker")

    # apikey
    p_key = subparsers.add_parser("apikey", help="Manage Bearer API keys")
    p_key.add_argument("api_action", choices=["create", "list", "revoke"], help="Action")
    p_key.add_argument("--name", default="vscode-client", help="Name/description of the key")
    p_key.add_argument("key_id", nargs="?", default=None, help="Key ID to revoke")

    # adminkey
    p_admin = subparsers.add_parser("adminkey", help="Manage the operator key for /v1/admin/*")
    p_admin.add_argument("admin_action", choices=["create", "revoke"], help="Action")
    p_admin.add_argument("--quiet", action="store_true", help="Print only the new key (for scripts)")

    # status
    p_status = subparsers.add_parser("status", help="Query DLNE Server status")
    p_status.add_argument("--host", default="127.0.0.1", help="Coordinator host")
    p_status.add_argument("--port", type=int, default=None, help="Coordinator port")

    # worker
    p_worker = subparsers.add_parser("worker", help="Start a worker node to contribute GPU/RAM to cluster")
    p_worker.add_argument("--coordinator", "-c", required=True, help="Coordinator address (e.g. 192.168.1.100:11435 or mybox.domain.com:11435)")
    p_worker.add_argument("--network-key", "-k", default=None, help="Cluster network authentication key")
    p_worker.add_argument("--allowed-gpus", default=None, help="Comma-separated GPU indices to contribute")
    p_worker.add_argument("--max-vram", type=float, default=None, help="Max VRAM in GB to contribute")
    p_worker.add_argument("--max-ram", type=float, default=None, help="Max System RAM overhead in GB")
    p_worker.add_argument("--rpc-ip", default=None, help="Local RPC IP to bind")
    p_worker.add_argument("--rpc-port", type=int, default=None, help="Local RPC port to bind")

    # install-systemd
    p_sys = subparsers.add_parser("install-systemd", help="Generate systemd service unit file")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "start":
        cmd_start(args)
    elif args.command == "worker":
        cmd_worker(args)
    elif args.command == "apikey":
        cmd_apikey(args)
    elif args.command == "adminkey":
        cmd_adminkey(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "install-systemd":
        cmd_install_systemd(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log_system("Shutting down DLNE Server.")
        sys.exit(0)

