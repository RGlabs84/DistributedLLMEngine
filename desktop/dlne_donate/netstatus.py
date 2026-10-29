"""
What the swarm can use right now, for the sliders' live line: the signed release's tiers (with sizes, for the speed
estimate) and the cluster status (which tiers are switched on). Both are optional: with neither, the window uses the
built-in ladder and says less. The release is verified before a word of it is used.
"""

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import COORDINATOR_URL, bundled
from .installer import FetchError, Http
from .tiers import app_tiers_from_status


@dataclass
class SwarmInfo:
    coding_tiers: List[Dict[str, Any]] = field(default_factory=list)        # validated, with file sizes
    app_tiers: List[Dict[str, Any]] = field(default_factory=lambda: app_tiers_from_status(None))
    app_sizes: Dict[str, int] = field(default_factory=dict)
    server_version: str = ""
    members: int = 0
    reachable: bool = False
    verified_release: bool = False


def fetch_swarm_info(base_url: str = COORDINATOR_URL, http: Any = None, public_key: Optional[str] = None) -> SwarmInfo:
    """Never raises."""
    http = http or Http()
    release = bundled.load_release()
    info = SwarmInfo()
    base = base_url.rstrip("/")
    status: Optional[Dict[str, Any]] = None
    try:
        code, body = http.get_json(f"{base}/v1/cluster/status")
        if code == 200 and isinstance(body, dict):
            status = body
            info.reachable = True
            info.server_version = str(body.get("server_version") or "")
            info.app_tiers = app_tiers_from_status(body)
            workers = body.get("active_workers_count")
            info.members = workers if isinstance(workers, int) else 0
    except FetchError:
        pass
    try:
        code, manifest = http.get_json(f"{base}/v1/release")
        if code == 200 and isinstance(manifest, dict):
            ok, _reason = release.verify_manifest(manifest, public_key)
            if ok:
                info.reachable = True
                info.verified_release = True
                info.server_version = info.server_version or str(manifest.get("version") or "")
                info.coding_tiers = [t for t in release.coding_tiers(manifest) if t.get("enabled")]
                info.app_sizes = {t["id"]: int(t["size"]) for t in release.app_tiers(manifest)
                                  if isinstance(t.get("size"), int)}
                if status is None:
                    from_manifest = release.app_tiers(manifest)
                    if from_manifest:
                        info.app_tiers = [{"id": t["id"], "name": t["name"], "min_ram_gb": float(t["min_ram_gb"]), "unlocked": True}
                                          for t in from_manifest]
    except (FetchError, Exception):  # noqa: BLE001 - the window works without it
        pass
    return info


def fetch_in_background(callback: Any, base_url: str = COORDINATOR_URL, http: Any = None) -> threading.Thread:
    """Fetch on a thread and call ``callback(SwarmInfo)`` from it."""
    def run() -> None:
        callback(fetch_swarm_info(base_url, http))
    thread = threading.Thread(target=run, name="swarm-info", daemon=True)
    thread.start()
    return thread
