"""
DLNE setup logic, with every question and every ``print`` removed.

WHY THIS MODULE EXISTS
----------------------
``setup_cli.py`` grew into a 900-line function that mixed three unrelated things: rich
console prompts, the decisions those prompts feed, and the side effects those decisions
cause (writing config, generating WireGuard invites, elevating for Windows security). That
was survivable while the console wizard was the only front end. It stopped being survivable
the moment a GUI wizard had to ask the same questions — the alternative to this module is
two wizards that disagree about what a valid answer is, and they would drift on the first
change to either.

So the *logic* lives here and knows nothing about how it is being asked:

* hardware detection and the contribution floors a CPU-only node has to clear;
* invite discovery and parsing;
* Coordinator discovery over the UDP beacon (and the split-brain check that reuses it);
* the flat ``KEY=VALUE`` config assembler — one function that turns an answer dict into
  ``config.txt`` contents, so the console and GUI wizards cannot produce different files
  from the same answers;
* the Windows security step (Defender exclusion, WDAC supplemental policy, **firewall
  rules**) as a script *builder* plus a separate runner;
* the auto-start registration.

Every function returns data — status dicts, lists of lines, tuples — and raises nothing at
its callers. The two wizards are then thin: one renders with ``rich``, the other renders in
a web view, and both call the same functions in the same order.

CONFIG FORMAT
-------------
Unchanged and deliberately so: flat ``KEY=VALUE`` lines read by ``engine.config``. JSON is
not introduced here (see the contract in ``engine/config.py``); ``build_config_lines`` exists
to make the flat format the *only* thing either wizard can emit.
"""

import os
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

# Imported as a module, not as `from engine.config import CONFIG_FILE, get_base_dir`.
# ``CONFIG_FILE`` is a module-level constant derived from ``get_base_dir()`` at import time,
# so binding it into this module's globals would freeze the path this wizard writes to at
# whatever it was when engine.config was first imported. Reaching through the module means
# an install root that is re-pointed — by a test fixture, or by a portable launch — is
# followed rather than silently ignored.
from engine import config as engine_config
from engine.config import get_base_dir, get_config

# The Coordinator's discovery beacon has exactly one parser in the codebase and it lives in
# the module that has to trust it most. Both wizards and the redundancy node's promotion
# loop reuse it rather than keeping a second, quietly divergent copy.
from engine.discovery import DISCOVERY_PORT, parse_discovery_beacon

# The tunnel MTU has exactly one source of truth, in the module that writes every
# WireGuard config. The elevated activation script applies that same number to the live
# adapter, so a node repaired by hand and a node set up fresh end up identical.
from engine import wireguard

# --------------------------------------------------------------------------------------
# Roles
# --------------------------------------------------------------------------------------
MODES = ("coordinator_only", "coordinator_hybrid", "worker_only", "redundancy_worker")

# Roles that run a WorkerNode and therefore contribute hardware.
COMPUTE_MODES = ("worker_only", "coordinator_hybrid", "redundancy_worker")

# Roles that host the network rather than join it.
COORDINATOR_MODES = ("coordinator_only", "coordinator_hybrid")

# Contribution floors for a node that brings no GPU. A CPU-only node below these numbers
# slows the whole swarm down instead of helping it, which is why setup refuses rather than
# warns. Hybrid roles need more because the Coordinator's own overhead comes out of the same
# machine.
CPU_ONLY_MINIMUMS = {
    "worker": {"ram_gb": 8.0, "physical_cores": 2},
    "hybrid": {"ram_gb": 12.0, "physical_cores": 4},
}

DEFAULT_BIND_PORT = "11435"
DEFAULT_RPC_PORT = "50052"

# The security marker is versioned: the original marker meant "Defender exclusion and WDAC
# were done", and installs carrying it never got firewall rules because nothing created any.
# Bumping the name is what makes an existing install re-run the step and pick them up.
SECURITY_MARKER_NAME = ".winsec_configured_v2"
LEGACY_SECURITY_MARKER_NAME = ".winsec_configured"

AUTOSTART_ENTRY_NAME = "DLNE-Node"
WINDOWS_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


# --------------------------------------------------------------------------------------
# Config plumbing
# --------------------------------------------------------------------------------------

def read_existing_config() -> Dict[str, str]:
    """
    The current on-disk configuration as ``UPPERCASE_KEY -> str``.

    Used to pre-fill wizard defaults so a re-run does not lose ports, IPs and — most
    importantly — the Network Key every already-issued invite was signed with. It reads
    through ``engine.config`` rather than parsing a file, because ``config.txt`` lives under
    the install root and not the current working directory.
    """
    # force_reload because the wizard may run right after another process rewrote the file.
    return get_config(force_reload=True)


# The order config.txt is written in. Purely cosmetic to a parser that splits on '=', but a
# stable order makes two installs diffable by hand, which is how operators debug a swarm.
CONFIG_KEY_ORDER = (
    "MODE",
    "NETWORK_KEY",
    "ADMIN_HASH",
    "BIND_IP",
    "BIND_PORT",
    "WG_WAN_IP",
    "COORDINATOR_IP",
    "COORDINATOR_PORT",
    "RPC_IP",
    "RPC_PORT",
    "ALLOWED_GPU_INDICES",
    "MAX_VRAM_GB",
    "MAX_RAM_GB",
    "CODING_MAX_RAM_GB",
    "ALSO_CPU_RAM_GB",
    "MAX_CPU_THREADS",
    "DEPENDENCY_MIRROR_URL",
)


def build_config_lines(answers: Dict[str, Any]) -> List[str]:
    """
    Turn a wizard's collected answers into the exact contents of ``config.txt``.

    This is the single assembler both wizards call. Keys listed in :data:`CONFIG_KEY_ORDER`
    come first in that order; anything else the caller supplies (the WireGuard allocator's
    high-water mark, say) is appended afterwards in sorted order so nothing silently goes
    missing. Empty values are dropped rather than written as ``KEY=``, because
    ``engine.config`` treats a present-but-empty key as an explicit override of its default.

    Args:
        answers: ``KEY -> value`` where KEY is the uppercase config name. Values are
            stringified; ``None`` and ``""`` are omitted.

    Returns:
        The lines to write, without trailing newlines.
    """
    lines: List[str] = []
    remaining = {
        str(key).strip().upper(): value
        for key, value in answers.items()
        if value is not None and str(value).strip() != ""
    }

    for key in CONFIG_KEY_ORDER:
        if key in remaining:
            lines.append(f"{key}={remaining.pop(key)}")
    for key in sorted(remaining):
        lines.append(f"{key}={remaining[key]}")
    return lines


def write_config_file(config_lines: List[str]) -> str:
    """
    Write freshly built ``KEY=VALUE`` lines to ``config/config.txt``.

    The file is replaced wholesale, deliberately: re-running setup must drop settings that
    belonged to a previous role, otherwise a machine switched from Coordinator to
    worker_only keeps its BIND_PORT and ADMIN_HASH and behaves like a half-Coordinator.
    Targeted, additive updates go through ``engine.config.set_config_values`` instead, which
    merges and writes atomically.

    Returns:
        The absolute path that was written.
    """
    config_path = engine_config.CONFIG_FILE
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(config_lines) + "\n")
    return config_path


# --------------------------------------------------------------------------------------
# Invites
# --------------------------------------------------------------------------------------

def find_invite_files() -> List[str]:
    """
    Every ``.conf`` that could be a DLNE invite, newest-looking first.

    Searches the install root plus ``config/`` and ``wg_configs/`` — resolved against the
    install directory rather than the working directory, because a node launched from a
    shortcut or a service has a working directory somewhere else entirely, and the old
    glob-relative-to-cwd search silently found nothing in exactly that case.
    """
    import glob

    base = get_base_dir()
    patterns = (
        os.path.join(base, "*.conf"),
        os.path.join(base, "config", "*.conf"),
        os.path.join(base, "wg_configs", "*.conf"),
    )
    found: List[str] = []
    for pattern in patterns:
        found.extend(sorted(glob.glob(pattern)))
    return found


def pick_join_invite(candidates: Optional[List[str]] = None) -> Optional[str]:
    """The invite a node should auto-join with, or None when no candidate looks like one."""
    for path in (candidates if candidates is not None else find_invite_files()):
        name = os.path.basename(path).lower()
        if "redundancy" in name or "worker" in name or "invite" in name:
            return path
    return None


def parse_invite(invite_path: str) -> Dict[str, Any]:
    """
    Read the DLNE metadata a WireGuard invite carries on its comment lines.

    The invite is a valid ``wg-quick`` config; DLNE's own fields ride along as ``#``
    comments so WireGuard ignores them. Values are split on the FIRST ``=`` only, because a
    base64 hash legitimately contains one and the old split truncated it.

    Returns:
        ``{"ok", "path", "network_key", "admin_hash", "error"}``. ``ok`` False means the file
        could not be read at all — a readable invite with no metadata is still ``ok`` with
        empty fields, which is a different problem the caller has to report differently.
    """
    result = {"ok": False, "path": invite_path, "network_key": "", "admin_hash": "", "error": ""}
    try:
        with open(invite_path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith("# DLNE_NETWORK_KEY="):
                    result["network_key"] = line.split("=", 1)[1].strip()
                elif line.startswith("# DLNE_ADMIN_HASH="):
                    result["admin_hash"] = line.split("=", 1)[1].strip()
    except OSError as exc:
        result["error"] = str(exc)
        return result

    result["ok"] = True
    return result


# --------------------------------------------------------------------------------------
# Join files and self-enrollment
# --------------------------------------------------------------------------------------
#
# A join file is what an operator hands to fifty testers at once. It is deliberately NOT a
# WireGuard config: it carries no private key and no address, because those are the two
# things that cannot be shared. A node reads the hub's location and the cluster secret out
# of it, generates its own keypair locally, and asks the Coordinator to assign it an
# address (see POST /worker/enroll).
#
# Flat KEY=VALUE, matching config.txt and the invite comment fields, rather than a .conf
# that wg-quick would try and fail to bring up.

JOIN_FILE_SUFFIX = ".dlnejoin"
JOIN_FILE_VERSION = "1"


def get_or_create_worker_id() -> str:
    """
    This node's stable identifier, minted once and kept in ``config/.dlne_id``.

    Extracted from ``WorkerNode.__init__``, which used to own the only copy. Enrollment
    happens during setup, long before a WorkerNode exists, and it must present the same
    identity the node will later heartbeat under — two implementations would have drifted
    the first time either changed.

    A failure to persist is not fatal: the node still gets a working identifier for this
    run, it just will not be the same one next time.
    """
    import uuid

    id_path = os.path.join(get_base_dir(), "config", ".dlne_id")
    try:
        with open(id_path, "r", encoding="utf-8") as handle:
            existing = handle.read().strip()
            if existing:
                return existing
    except OSError:
        pass

    worker_id = str(uuid.uuid4())
    try:
        os.makedirs(os.path.dirname(id_path), exist_ok=True)
        with open(id_path, "w", encoding="utf-8") as handle:
            handle.write(worker_id)
    except OSError:
        pass
    return worker_id


def find_join_files() -> List[str]:
    """
    Every join file the operator plausibly left lying about, in a stable order.

    Beside the binary and under ``config/`` first: that is where the wizard writes one and
    where an unattended deployment would drop it. Then the places a person actually
    downloads to. An installer run from Downloads leaves the join file next to the
    *installer*, not next to the executable it installed — searching only the install root
    means every tester has to be talked through finding %LOCALAPPDATA%, which is a support
    burden with no security benefit: it is the user's own file on the user's own machine.

    Deduplicated by real path, so a file reachable two ways is offered once.
    """
    import glob

    base = get_base_dir()
    home = os.path.expanduser("~")
    candidates = [base, os.path.join(base, "config")]
    try:
        candidates.append(os.getcwd())
    except OSError:
        pass  # cwd can be deleted out from under a running process
    candidates.extend([os.path.join(home, "Downloads"), os.path.join(home, "Desktop")])

    found: List[str] = []
    seen = set()
    for directory in candidates:
        try:
            matches = sorted(glob.glob(os.path.join(directory, f"*{JOIN_FILE_SUFFIX}")))
        except OSError:
            continue
        for path in matches:
            key = os.path.normcase(os.path.realpath(path))
            if key in seen:
                continue
            seen.add(key)
            found.append(path)
    return found


def parse_join_file(path: str) -> Dict[str, Any]:
    """
    Read a join file into its fields.

    Returns:
        ``{"ok", "path", "coordinator_host", "coordinator_port", "network_key",
        "wg_endpoint", "wg_pubkey", "error"}``. ``ok`` is False when the file cannot be
        read or is missing a field without which joining is impossible.
    """
    result: Dict[str, Any] = {
        "ok": False, "path": path, "coordinator_host": "", "coordinator_port": "11435",
        "network_key": "", "wg_endpoint": "", "wg_pubkey": "", "error": "",
    }
    fields: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                fields[key.strip().upper()] = value.strip()
    except OSError as exc:
        result["error"] = str(exc)
        return result

    result["coordinator_host"] = fields.get("COORDINATOR_HOST", "")
    result["coordinator_port"] = fields.get("COORDINATOR_PORT", "11435")
    result["network_key"] = fields.get("NETWORK_KEY", "")
    result["wg_endpoint"] = fields.get("WG_ENDPOINT", "")
    result["wg_pubkey"] = fields.get("WG_COORDINATOR_PUBKEY", "")

    if not result["coordinator_host"]:
        result["error"] = "The join file does not say which Coordinator to contact."
        return result
    if not result["network_key"]:
        # Without it the node cannot sign anything: not the enrollment request, not its
        # heartbeats. Failing here is far clearer than a 401 twenty seconds later.
        result["error"] = "The join file carries no NETWORK_KEY, so this node cannot authenticate."
        return result

    result["ok"] = True
    return result


# Roles that join a network someone else hosts, and therefore need its key to say anything.
JOINING_MODES = ("worker_only", "redundancy_worker")


def adopt_join_file_credentials() -> Dict[str, Any]:
    """
    Give a keyless node the network key from the join file sitting beside it.

    This exists for one specific dead end. Setup runs only when ``config.txt`` is *absent*,
    so a node configured without a key — by a wizard that did not read the join file, or by
    hand — never looks at that file again. It boots unsigned, the Coordinator refuses its
    heartbeat with a 401, it shuts down, and the next launch does exactly the same. Nothing
    in that loop can end it, and the file that would end it is right there in the folder.

    The key only. The Coordinator address in ``config.txt`` is left alone unless there is
    none worth keeping, because a working LAN address that someone chose deliberately must
    not be traded for the join file's public one — that address routes through a tunnel this
    function has no business bringing up.

    Adopting is not joining a network on the operator's behalf: they put the file there, and
    the node it names is the node they were told to join.

    Returns:
        ``{"adopted", "path", "network", "error"}``. ``adopted`` False with an empty
        ``error`` is the ordinary case of nothing to do.
    """
    from engine.config import set_config_values

    result: Dict[str, Any] = {"adopted": False, "path": "", "network": "", "error": ""}

    current = get_config()
    if str(current.get("NETWORK_KEY", "") or "").strip():
        return result

    for path in find_join_files():
        join = parse_join_file(path)
        if not join["ok"]:
            # Reported rather than skipped in silence: this is the file the operator was
            # told to drop in, and "nothing happened" is indistinguishable from "not found".
            result["error"] = f"{os.path.basename(path)} could not be used: {join['error']}"
            continue

        values = {"NETWORK_KEY": join["network_key"]}
        coordinator = str(current.get("COORDINATOR_IP", "") or "").strip()
        if not coordinator or coordinator == "127.0.0.1":
            values["COORDINATOR_IP"] = join["coordinator_host"]
            values["COORDINATOR_PORT"] = join["coordinator_port"]
            coordinator = join["coordinator_host"]

        if not set_config_values(values):
            result["error"] = f"The network key from {os.path.basename(path)} could not be saved."
            return result

        result["adopted"] = True
        result["path"] = path
        result["network"] = coordinator
        result["error"] = ""
        return result

    return result


def write_join_file(path: str, coordinator_host: str, network_key: str,
                    wg_endpoint: str = "", wg_pubkey: str = "",
                    coordinator_port: str = "11435") -> str:
    """
    Write the join file an operator distributes. Contains no private key, by design.

    Returns:
        The absolute path written.
    """
    lines = [
        "# DLNE join file. Give this to every node that should join the network.",
        "# It contains no private key: each node generates its own and enrolls itself.",
        f"DLNE_JOIN_VERSION={JOIN_FILE_VERSION}",
        f"COORDINATOR_HOST={coordinator_host}",
        f"COORDINATOR_PORT={coordinator_port}",
        f"NETWORK_KEY={network_key}",
    ]
    if wg_endpoint:
        lines.append(f"WG_ENDPOINT={wg_endpoint}")
    if wg_pubkey:
        lines.append(f"WG_COORDINATOR_PUBKEY={wg_pubkey}")

    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")

    # It still holds the cluster secret, so it is not world-readable even though it is
    # meant to be handed around.
    if sys.platform != "win32":
        os.chmod(path, 0o600)
    return path


def coordinator_is_reachable(host: str, port: str, timeout: float = 4.0) -> bool:
    """
    Whether a TCP connection to this Coordinator can be opened at all.

    Deliberately just a connect: it separates "nothing is listening there" from every other
    outcome without sending a request, so nothing is enrolled, registered or logged on the
    far side by asking. What the Coordinator then says about the request is the caller's
    problem and must not be confused with not being able to ask.
    """
    try:
        port_number = int(str(port).strip() or DEFAULT_BIND_PORT)
    except ValueError:
        return False

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port_number))
        return True
    except Exception:
        return False
    finally:
        sock.close()


def resolve_join_target(join: Dict[str, Any], connect_timeout: float = 4.0,
                        discovery_timeout: float = 8.0) -> Dict[str, Any]:
    """
    Work out where the Coordinator named by a join file can actually be reached.

    One join file is handed to every tester, so it carries the Coordinator's *public*
    address. A node sitting behind the same router as the Coordinator cannot use that
    address: reaching your own public IP from inside the network requires NAT hairpinning,
    which many routers simply refuse. The node is one hop from the Coordinator and cannot
    dial it, which is a miserable thing to hand somebody as "enrollment failed".

    So when the named address cannot be opened, this listens for the Coordinator's LAN
    discovery beacon *signed with the join file's own network key*.

    What makes that safe, rather than a way to be pushed onto somebody else's Coordinator:

    - The signature is required. ``parse_discovery_beacon`` rejects an unsigned beacon
      outright once a key is supplied, and a beacon signed with any other key fails the
      HMAC. Only a node that already holds this cluster's secret can produce one, and
      holding that secret is the entire definition of belonging to this network.
    - A join file with no key never reaches the fallback at all. ``parse_join_file``
      refuses those already; this refuses them a second time rather than trusting that.
    - The fallback runs only when the named address could not be *opened*. A Coordinator
      that answered and then rejected the request is a decision, and it stands — the node
      does not go looking for a second opinion from something else on the LAN.
    - The beacon carries a timestamp inside the signed material, so a captured one cannot
      be replayed later to steer a node somewhere else.

    Returns:
        ``{"ok", "via", "host", "port", "error"}``. ``via`` is ``"direct"`` when the join
        file's own address answered and ``"lan"`` when the beacon did — the caller needs
        that distinction, because a node that found its Coordinator on the LAN wants to
        talk to it over the LAN rather than through a tunnel to the public address.
    """
    result: Dict[str, Any] = {"ok": False, "via": "", "host": "", "port": "", "error": ""}

    host = str(join.get("coordinator_host") or "").strip()
    port = str(join.get("coordinator_port") or DEFAULT_BIND_PORT).strip()
    if not host:
        result["error"] = "The join file does not say which Coordinator to contact."
        return result

    if coordinator_is_reachable(host, port, timeout=connect_timeout):
        result.update(ok=True, via="direct", host=host, port=port)
        return result

    network_key = str(join.get("network_key") or "").strip()
    if not network_key:
        # Without a key there is nothing to authenticate a beacon with, and an unsigned
        # beacon is an invitation for any host on the network to nominate itself.
        result["error"] = (f"The Coordinator at {host}:{port} could not be reached, and this "
                           "join file carries no network key to verify a local one with.")
        return result

    found_host, found_port = discover_coordinator(timeout=discovery_timeout,
                                                  network_key=network_key)
    if found_host:
        result.update(ok=True, via="lan", host=found_host, port=found_port or port)
        return result

    result["error"] = (
        f"The Coordinator at {host}:{port} could not be reached, and no Coordinator on this "
        "network answered with a beacon signed by this network's key. If this machine is "
        "behind the same router as the Coordinator, use the Coordinator's local address; "
        "otherwise check that the Coordinator is running and its port is reachable."
    )
    return result


def enroll_with_coordinator(join: Dict[str, Any], worker_id: str,
                            timeout: float = 15.0) -> Dict[str, Any]:
    """
    Generate this node's own tunnel keypair and get it an address from the Coordinator.

    The node keeps the private key; only the public half is transmitted, signed with the
    network key so the Coordinator knows the request came from a cluster member and an
    on-path attacker cannot substitute their own key for it.

    Re-running this is safe and cheap. An existing ``dlne_tunnel.conf`` is reused rather
    than replaced, so the node presents the same public key it enrolled with the first time
    and the Coordinator hands back the same address instead of consuming another.

    Args:
        join: A parsed join file (see :func:`parse_join_file`).
        worker_id: This node's stable identifier, as used in heartbeats.
        timeout: Seconds to allow for the HTTP round trip.

    Returns:
        ``{"ok", "conf_path", "tunnel_address", "error", "reason"}``. ``reason`` is
        ``"wireguard_missing"`` when the only thing standing in the way is the WireGuard
        tools — a caller with a way to install them can offer that and retry, rather than
        telling the operator to go away and run setup again.
    """
    import secrets

    import httpx

    from engine.authn import sign_enrollment
    from engine.swarm_clock import swarm_clock
    from engine.wireguard import WireGuardOrchestrator, WireGuardUnavailableError

    result: Dict[str, Any] = {"ok": False, "conf_path": "", "tunnel_address": "",
                              "error": "", "reason": ""}

    try:
        orchestrator = WireGuardOrchestrator()
        existing = _existing_tunnel_identity(orchestrator)
        if existing:
            private_key, public_key = existing
        else:
            private_key, public_key = orchestrator.generate_keypair()
            if not private_key or not public_key:
                result["reason"] = "wireguard_missing"
                result["error"] = ("WireGuard is not installed, so this node cannot create a "
                                   "tunnel key. Install WireGuard and try again.")
                return result
    except WireGuardUnavailableError as exc:
        result["reason"] = "wireguard_missing"
        result["error"] = str(exc)
        return result

    host = join["coordinator_host"]
    url = host if host.startswith("http") else f"http://{host}:{join['coordinator_port']}"
    # Signed with the coordinator's time as this machine knows it (engine/swarm_clock.py). A refusal for a stale
    # timestamp carries the coordinator's time, so the enrollment is signed and sent once more with it: a computer
    # whose clock is wrong, by minutes or by a time zone's hours, still enrolls.
    for attempt in range(2):
        timestamp = swarm_clock.now()
        nonce = secrets.token_hex(16)
        payload = {
            "worker_id": worker_id,
            "public_key": public_key,
            "timestamp": timestamp,
            "nonce": nonce,
            "signature": sign_enrollment(join["network_key"], worker_id, public_key,
                                         timestamp, nonce),
        }
        try:
            sent_at = swarm_clock.local()
            response = httpx.post(f"{url.rstrip('/')}/worker/enroll", json=payload, timeout=timeout)
            swarm_clock.learn_from(response, sent_at, swarm_clock.local())
        except httpx.HTTPError as exc:
            result["error"] = f"Could not reach the Coordinator at {url}: {exc}"
            return result
        if attempt == 0 and response.status_code == 401 and "stale timestamp" in response.text:
            continue
        break

    if response.status_code != 200:
        # The Coordinator's rejection reasons are written to be shown to an operator, so
        # pass them through rather than inventing a summary.
        detail = ""
        try:
            detail = str(response.json().get("detail", ""))
        except Exception:
            detail = response.text.strip()[:200]
        result["error"] = f"Enrollment refused ({response.status_code}): {detail}"
        return result

    try:
        body = response.json()
        address = str(body["tunnel_address"])
        octet = int(address.rsplit(".", 1)[1])
        coordinator_pubkey = str(body["coordinator_pubkey"])
        coordinator_endpoint = str(body["coordinator_endpoint"])
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        result["error"] = f"The Coordinator's enrollment reply could not be understood: {exc}"
        return result

    # The hub's own key wins over whatever the join file claimed: the file may have been
    # written before the Coordinator's interface was last regenerated, and a stale peer key
    # produces a tunnel that looks configured and never handshakes.
    result["conf_path"] = orchestrator.write_member_conf(
        private_key, octet, coordinator_pubkey or join["wg_pubkey"],
        coordinator_endpoint or join["wg_endpoint"],
        network_key=join["network_key"],
    )
    result["tunnel_address"] = address
    result["ok"] = True
    return result


def _existing_tunnel_identity(orchestrator) -> Optional[Tuple[str, str]]:
    """
    This node's already-enrolled keypair, read back from its own tunnel config.

    Returns ``(private_key, public_key)``, or None when there is no usable existing
    identity — in which case the caller generates a fresh one.
    """
    conf_path = os.path.join(orchestrator.config_dir, "dlne_tunnel.conf")
    try:
        with open(conf_path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                stripped = line.strip()
                if stripped.lower().startswith("privatekey"):
                    private_key = stripped.partition("=")[2].strip()
                    if not private_key:
                        return None
                    public_key = orchestrator._run_wg_cmd("pubkey", stdin_text=private_key + "\n")
                    return (private_key, public_key) if public_key else None
                if stripped.lower().startswith("[peer]"):
                    break
    except OSError:
        return None
    return None


def hash_admin_password(password: str) -> str:
    """bcrypt hash for storage in ``ADMIN_HASH``."""
    import bcrypt

    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_admin_password(password: str, admin_hash: str) -> bool:
    """
    Check a password against a stored bcrypt hash.

    Wrapped rather than called inline because a malformed hash (a truncated invite, a hand
    edited config) makes bcrypt raise, and every caller here wants "no" rather than a
    traceback in the middle of a wizard.
    """
    if not admin_hash:
        return False
    try:
        import bcrypt

        return bcrypt.checkpw(password.encode(), admin_hash.encode())
    except Exception:
        return False


# --------------------------------------------------------------------------------------
# Coordinator discovery
# --------------------------------------------------------------------------------------

def discover_coordinator(timeout: float = 2.0,
                         network_key: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """
    Listen for the Coordinator's UDP discovery beacon on the local network.

    Validation is delegated to ``engine.discovery.parse_discovery_beacon``, the one
    canonical implementation of the beacon format — signature, protocol domain separation
    and replay window included.

    Args:
        timeout: Seconds to wait for a beacon before giving up.
        network_key: Shared cluster secret. When set, unsigned or wrongly signed beacons are
            rejected. None/empty accepts any well-formed beacon.

    Returns:
        ``(coordinator_ip, coordinator_port)`` as strings, or ``(None, None)`` on timeout or
        on any packet that fails validation. The port is a string because callers feed it
        straight into prompt defaults and config lines.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    try:
        sock.bind(('', DISCOVERY_PORT))
    except Exception:
        return None, None

    # Keep listening until the deadline instead of giving up on the first packet that fails
    # validation. This port carries other broadcast traffic on a busy LAN, and one unrelated
    # datagram arriving before the Coordinator's beacon used to end the search — discovery
    # then "timed out" against a Coordinator that was broadcasting perfectly well. Rejected
    # packets are dropped, never acted on, so waiting longer costs nothing but time.
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, None

            sock.settimeout(remaining)
            try:
                data, addr = sock.recvfrom(1024)
            except socket.timeout:
                return None, None
            except Exception:
                return None, None

            try:
                port = parse_discovery_beacon(data.decode(), network_key or "")
            except UnicodeDecodeError:
                continue  # Unrelated broadcast traffic shares this port on busy LANs.
            except Exception:
                continue

            if port is not None:
                return addr[0], str(port)
            # Malformed, unsigned when a key is required, spoofed, or stale. Keep waiting.
    finally:
        sock.close()


def check_split_brain(timeout: float = 3.0) -> Dict[str, Any]:
    """
    Whether another Primary Coordinator is already broadcasting on this LAN.

    Two primaries on one network means two registries, two election timers and two sets of
    workers that each think they are the whole swarm — the failure is silent and the recovery
    is manual, so setup refuses to configure the second one.

    Returns:
        ``{"conflict": bool, "ip": str, "port": str}``.
    """
    ip, port = discover_coordinator(timeout=timeout)
    return {"conflict": bool(ip), "ip": ip or "", "port": port or ""}


# --------------------------------------------------------------------------------------
# Hardware detection and contribution bounds
# --------------------------------------------------------------------------------------

def detect_hardware() -> Dict[str, Any]:
    """
    What this machine has to offer, in the shape both wizards render.

    Returns:
        ``{"ok", "error", "gpus", "total_ram_gb", "physical_cores", "logical_threads"}``.
        ``gpus`` entries carry ``id``, ``name`` and ``vram_total_gb``. ``ok`` False means
        detection failed outright; the wizard then skips the contribution page rather than
        writing limits it guessed at.
    """
    profile: Dict[str, Any] = {
        "ok": False,
        "error": "",
        "gpus": [],
        "total_ram_gb": 0.0,
        "physical_cores": 0,
        "logical_threads": 0,
    }
    try:
        import psutil

        from engine.hardware import get_all_gpus

        profile["gpus"] = get_all_gpus() or []
        profile["total_ram_gb"] = round(psutil.virtual_memory().total / (1024 ** 3), 1)
        logical = psutil.cpu_count(logical=True) or 1
        physical = psutil.cpu_count(logical=False) or logical
        profile["logical_threads"] = logical
        profile["physical_cores"] = physical
        profile["ok"] = True
    except Exception as exc:
        profile["error"] = str(exc)
    return profile


def contribution_bounds(mode: str, cpu_only: bool,
                        hardware: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    The floors, defaults and verdict for this node's hardware contribution.

    Both wizards need the same three answers — may this node join at all, what should the
    sliders start at, and what is the lowest the user may drag them to — and computing them
    in two places is how a GUI ends up accepting a node the console would have rejected.

    Args:
        mode: The role being configured.
        cpu_only: True when the user dedicated no GPU. The floors only apply then, because a
            node contributing VRAM is useful regardless of its CPU.
        hardware: A :func:`detect_hardware` result; detected on demand when omitted.

    Returns:
        ``{"meets_minimum", "reason", "min_ram_gb", "min_cores", "default_ram_gb",
        "default_cores", "total_ram_gb", "physical_cores", "logical_threads"}``.
    """
    hardware = hardware or detect_hardware()
    total_ram = float(hardware.get("total_ram_gb") or 0.0)
    physical = int(hardware.get("physical_cores") or 0)

    is_hybrid = mode in ("coordinator_hybrid", "redundancy_worker")
    floors = CPU_ONLY_MINIMUMS["hybrid" if is_hybrid else "worker"]
    min_ram = floors["ram_gb"] if cpu_only else 0.0
    min_cores = floors["physical_cores"] if cpu_only else 0

    # A third of RAM is the long-standing default: enough for emergency spillover without
    # pushing the user's own workload into swap.
    default_ram = round(total_ram / 3, 1) if total_ram else min_ram
    if cpu_only and default_ram < CPU_ONLY_MINIMUMS["worker"]["ram_gb"]:
        default_ram = CPU_ONLY_MINIMUMS["worker"]["ram_gb"]

    # Quarter of the physical cores, never zero. Physical only: hyperthreading hurts LLM
    # inference latency through cache thrashing, so logical threads are never mapped.
    default_cores = max(1, physical // 4) if physical else min_cores
    if cpu_only and default_cores < CPU_ONLY_MINIMUMS["worker"]["physical_cores"]:
        default_cores = CPU_ONLY_MINIMUMS["worker"]["physical_cores"]

    meets = True
    reason = ""
    if cpu_only and hardware.get("ok"):
        if total_ram < min_ram or physical < min_cores:
            meets = False
            role_text = ("CPU-only Hybrid nodes must have at least "
                         f"{min_ram:g}GB of System RAM and {min_cores} Physical Cores to leave "
                         "room for the Network Hub overhead."
                         if is_hybrid else
                         f"CPU-only nodes must have at least {min_ram:g}GB of System RAM and "
                         f"{min_cores} Physical Cores.")
            reason = (f"{role_text} Detected: {total_ram:g}GB RAM, {physical} Cores. "
                      "Contributing a GPU removes this requirement.")

    return {
        "meets_minimum": meets,
        "reason": reason,
        "min_ram_gb": min_ram,
        "min_cores": min_cores,
        "default_ram_gb": default_ram,
        "default_cores": default_cores,
        "total_ram_gb": total_ram,
        "physical_cores": physical,
        "logical_threads": int(hardware.get("logical_threads") or 0),
    }


def validate_contribution(ram_gb: float, cores: int, bounds: Dict[str, Any]) -> Dict[str, Any]:
    """Check a user's chosen contribution against :func:`contribution_bounds`."""
    problems: List[str] = []
    try:
        ram_value = float(ram_gb)
    except (TypeError, ValueError):
        return {"ok": False, "errors": ["RAM must be a number."]}
    try:
        core_value = int(cores)
    except (TypeError, ValueError):
        return {"ok": False, "errors": ["Core count must be a whole number."]}

    if ram_value < bounds["min_ram_gb"]:
        problems.append(f"CPU-only nodes must allocate at least {bounds['min_ram_gb']:g} GB of RAM.")
    if core_value < bounds["min_cores"]:
        problems.append(f"CPU-only nodes must allocate at least {bounds['min_cores']} Physical Cores.")
    if ram_value <= 0:
        problems.append("RAM allocation must be greater than zero.")
    if core_value <= 0:
        problems.append("At least one Physical Core must be allocated.")
    if bounds["physical_cores"] and core_value > bounds["physical_cores"]:
        problems.append(f"This machine only has {bounds['physical_cores']} Physical Cores.")

    return {"ok": not problems, "errors": problems}


def selected_gpus(gpu_choice: str, gpus: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Resolve a GPU selection string (``-1``, ``none``, or ``0,2``) to GPU records.

    ``-1`` means all, ``none`` means CPU-only, and unknown ids are dropped rather than
    invented — a typo'd index must not silently become "all GPUs".
    """
    choice = (gpu_choice or "").strip().lower()
    if choice in ("none", ""):
        return []
    if choice == "-1":
        return list(gpus)

    chosen = set()
    for token in choice.split(","):
        token = token.strip()
        if token.lstrip("-").isdigit():
            chosen.add(int(token))
    return [gpu for gpu in gpus if gpu.get("id") in chosen]


# --------------------------------------------------------------------------------------
# Windows security configuration
# --------------------------------------------------------------------------------------

def security_status(install_dir: Optional[str] = None) -> Dict[str, Any]:
    """
    Probe what the Windows security step would have to do, without doing any of it.

    Returns:
        ``{"applicable", "already_configured", "has_wdac", "sac_state", "install_dir",
        "marker_path", "legacy_marker"}``. ``already_configured`` is only True for the
        *current* marker version — an install carrying the legacy marker reports
        ``legacy_marker`` True and is expected to re-run, which is how existing installs pick
        up the firewall rules that never existed when they were first configured.
    """
    install_dir = os.path.abspath(install_dir or get_base_dir())
    marker_path = os.path.join(install_dir, "config", SECURITY_MARKER_NAME)
    legacy_path = os.path.join(install_dir, "config", LEGACY_SECURITY_MARKER_NAME)

    status = {
        "applicable": sys.platform == "win32",
        "already_configured": os.path.exists(marker_path),
        "legacy_marker": os.path.exists(legacy_path),
        "has_wdac": False,
        "sac_state": "Unknown",
        "install_dir": install_dir,
        "marker_path": marker_path,
    }
    if not status["applicable"] or status["already_configured"]:
        return status

    try:
        result = subprocess.run(
            ["powershell", "-Command",
             "(Get-ChildItem '$env:windir\\System32\\CodeIntegrity\\CIPolicies\\Active\\*.cip' "
             "-ErrorAction SilentlyContinue).Count"],
            capture_output=True, text=True, timeout=10
        )
        count = result.stdout.strip()
        status["has_wdac"] = count.isdigit() and int(count) > 0
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["powershell", "-Command", "(Get-MpComputerStatus).SmartAppControlState"],
            capture_output=True, text=True, timeout=10
        )
        status["sac_state"] = result.stdout.strip() or "Unknown"
    except Exception:
        pass

    return status


def firewall_ports(config: Optional[Dict[str, str]] = None) -> Dict[str, int]:
    """
    The ports DLNE needs opened inbound, read from config with the engine's own defaults.

    Nothing in DLNE has ever created a firewall rule: the Worker's RPC listener, the
    Coordinator's HTTP bind and the UDP discovery beacon all relied on the user clicking
    through Windows' prompt — or, for a service launch with no interactive desktop, on being
    silently dropped. That is what this feeds.
    """
    config = config if config is not None else read_existing_config()

    def _port(key: str, fallback: str) -> int:
        try:
            value = int(str(config.get(key, fallback)).strip())
        except (TypeError, ValueError):
            return int(fallback)
        return value if 1 <= value <= 65535 else int(fallback)

    return {
        "rpc": _port("RPC_PORT", DEFAULT_RPC_PORT),
        "bind": _port("BIND_PORT", DEFAULT_BIND_PORT),
        "discovery": int(DISCOVERY_PORT),
    }


def build_security_script(install_dir: str, ports: Dict[str, int], has_wdac: bool,
                          marker_path: str) -> str:
    """
    Build the single elevated PowerShell script the security step runs.

    Everything that needs administrator rights is batched into one script so the user sees
    exactly one UAC dialog: the Defender exclusion, the optional WDAC supplemental policy,
    the firewall rules, and finally the marker file (written from the elevated session, which
    is also how the caller detects that the user did not cancel the prompt).

    The firewall rules are idempotent by construction — each is removed by display name
    before being created — so re-running setup cannot accumulate duplicates.
    """
    lines = [
        "# DLNE Automated Windows Security Configuration",
        "# This script is auto-generated and executed with admin privileges.",
        "$ErrorActionPreference = 'Continue'",
        "",
        "# --- Defender Exclusion ---",
        "# The C++ engine binaries are reassembled at runtime from obfuscated chunks, which",
        "# is behaviour heuristic AV engines flag on sight.",
        f"Add-MpPreference -ExclusionPath '{install_dir}' -ErrorAction SilentlyContinue",
        "",
    ]

    if has_wdac:
        lines.extend([
            "# --- WDAC Supplemental Whitelist Policy ---",
            "try {",
            "    $rules = @()",
            f"    $rules += New-CIPolicyRule -FilePathRule '{install_dir}\\*'",
            f"    $xmlPath = '{install_dir}\\config\\.dlne_wdac_policy.xml'",
            f"    $cipPath = '{install_dir}\\config\\.dlne_wdac_policy.cip'",
            "    New-CIPolicy -FilePath $xmlPath -Rules $rules -UserPEs",
            "",
            "    # Attempt to link as supplemental to known WDAC base policies",
            "    $basePolicies = @(",
            "        '{0283AC0F-FFF1-49AE-ADA1-8A933130CAD6}',",
            "        '{1283AC0F-FFF1-49AE-ADA1-8A933130CAD6}'",
            "    )",
            "    foreach ($bp in $basePolicies) {",
            "        try {",
            "            Set-CIPolicyIdInfo -FilePath $xmlPath -PolicyName 'DLNE Engine Whitelist' -SupplementsBasePolicyID $bp -ErrorAction Stop",
            "            break",
            "        } catch { continue }",
            "    }",
            "",
            "    # Remove Audit Mode flag so the Allow rule actively takes effect",
            "    Set-RuleOption -FilePath $xmlPath -Option 3 -Delete -ErrorAction SilentlyContinue",
            "",
            "    # Convert to binary and deploy",
            "    ConvertFrom-CIPolicy $xmlPath $cipPath | Out-Null",
            "    CiTool.exe --update-policy $cipPath",
            "",
            "    # Clean up policy source files (the .cip is cached by Windows)",
            "    Remove-Item $xmlPath -Force -ErrorAction SilentlyContinue",
            "    Remove-Item $cipPath -Force -ErrorAction SilentlyContinue",
            "} catch {",
            "    # Non-fatal — WDAC policy creation is best-effort",
            "}",
            "",
        ])

    lines.extend([
        "# --- Windows Firewall ---",
        "# Without these the node binds successfully and is simply never reached: the",
        "# Coordinator's HTTP port, the Worker's RPC port and the UDP discovery beacon are",
        "# all inbound. Rules are scoped to Private+Domain profiles; a node on a public",
        "# network is expected to arrive over the WireGuard tunnel instead.",
        "$dlneRules = @(",
        f"    @{{ Name = 'DLNE Worker RPC'; Port = {ports['rpc']}; Protocol = 'TCP' }},",
        f"    @{{ Name = 'DLNE Coordinator'; Port = {ports['bind']}; Protocol = 'TCP' }},",
        f"    @{{ Name = 'DLNE Discovery'; Port = {ports['discovery']}; Protocol = 'UDP' }}",
        ")",
        "foreach ($rule in $dlneRules) {",
        "    try {",
        "        Remove-NetFirewallRule -DisplayName $rule.Name -ErrorAction SilentlyContinue",
        "        New-NetFirewallRule -DisplayName $rule.Name -Direction Inbound -Action Allow `",
        "            -Protocol $rule.Protocol -LocalPort $rule.Port -Profile Private,Domain `",
        "            -Description 'Distributed LLM Network Engine' | Out-Null",
        "    } catch {",
        "        # A locked-down group policy can forbid rule creation; the node still runs",
        "        # on LAN-less setups, so this never aborts the rest of the script.",
        "    }",
        "}",
        "",
        "# --- Write marker to skip this on future runs ---",
        f"New-Item -ItemType Directory -Force -Path (Split-Path '{marker_path}') | Out-Null",
        f"Set-Content -Path '{marker_path}' -Value 'configured'",
    ])

    return "\n".join(lines)


def apply_security_configuration(install_dir: Optional[str] = None,
                                 status: Optional[Dict[str, Any]] = None,
                                 ports: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    """
    Run the elevated security script and report what happened.

    Success is decided by the marker file existing afterwards, not by the exit code:
    ``Start-Process -Verb RunAs`` returns 0 whether the user accepted or dismissed the UAC
    dialog, so the marker — written from inside the elevated session — is the only honest
    signal that the work actually ran.

    Returns:
        ``{"ok", "skipped", "detail", "script_path", "sac_state"}``. ``script_path`` is only
        populated on failure, so a caller can tell the user what to run by hand.
    """
    if sys.platform != "win32":
        return {"ok": True, "skipped": True, "detail": "Not applicable on this platform.",
                "script_path": "", "sac_state": "Unknown"}

    status = status or security_status(install_dir)
    if status["already_configured"]:
        return {"ok": True, "skipped": True, "detail": "Already configured.",
                "script_path": "", "sac_state": status["sac_state"]}

    install_dir = status["install_dir"]
    marker_path = status["marker_path"]
    ports = ports or firewall_ports()

    os.makedirs(os.path.dirname(marker_path), exist_ok=True)
    script_text = build_security_script(install_dir, ports, status["has_wdac"], marker_path)
    script_path = os.path.join(tempfile.gettempdir(), "dlne_winsec_setup.ps1")
    try:
        with open(script_path, "w", encoding="utf-8") as handle:
            handle.write(script_text)
    except OSError as exc:
        return {"ok": False, "skipped": False, "detail": f"Could not write the script: {exc}",
                "script_path": "", "sac_state": status["sac_state"]}

    try:
        subprocess.run(
            ["powershell", "-Command",
             f"Start-Process powershell -ArgumentList '-ExecutionPolicy Bypass -File \"{script_path}\"' "
             "-Verb RunAs -Wait"],
            capture_output=True, text=True, timeout=180
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "skipped": False,
                "detail": "The Administrator prompt timed out.",
                "script_path": script_path, "sac_state": status["sac_state"]}
    except Exception as exc:
        return {"ok": False, "skipped": False,
                "detail": f"Could not launch the Administrator prompt: {exc}",
                "script_path": script_path, "sac_state": status["sac_state"]}

    if os.path.exists(marker_path):
        try:
            os.remove(script_path)
        except OSError:
            pass
        detail = "Defender exclusion and firewall rules configured."
        if status["has_wdac"]:
            detail = "Defender exclusion, WDAC whitelist and firewall rules configured."
        return {"ok": True, "skipped": False, "detail": detail, "script_path": "",
                "sac_state": status["sac_state"]}

    # Left on disk on purpose so the surfaced instructions actually work.
    return {"ok": False, "skipped": False,
            "detail": "The Administrator prompt was cancelled or failed.",
            "script_path": script_path, "sac_state": status["sac_state"]}


# --------------------------------------------------------------------------------------
# Auto-start
# --------------------------------------------------------------------------------------

def launch_command(tray: bool = True) -> str:
    """
    The command line that starts this installation.

    A frozen build is its own executable; a source checkout has to be launched through the
    interpreter that is running right now, since that is the one with DLNE's dependencies.
    """
    if getattr(sys, "frozen", False):
        command = f'"{sys.executable}"'
    else:
        main_path = os.path.join(get_base_dir(), "main.py")
        command = f'"{sys.executable}" "{main_path}"'
    if tray:
        command += " --tray"
    return command


def _linux_autostart_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "autostart", "dlne-node.desktop")


def get_autostart() -> bool:
    """Whether DLNE is currently registered to start with the user's session."""
    if sys.platform == "win32":
        try:
            import winreg  # type: ignore

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WINDOWS_RUN_KEY) as key:
                winreg.QueryValueEx(key, AUTOSTART_ENTRY_NAME)
            return True
        except Exception:
            return False
    if sys.platform.startswith("linux"):
        return os.path.exists(_linux_autostart_path())
    return False


def set_autostart(enabled: bool, tray: bool = True) -> Dict[str, Any]:
    """
    Register or unregister DLNE for session start.

    Deliberately per-user (``HKCU\\...\\Run`` on Windows, an XDG autostart entry on Linux):
    a machine-wide entry would need elevation on every toggle and would start the node for
    other accounts on a shared machine, which nobody asked for.

    Returns:
        ``{"ok", "enabled", "detail"}``.
    """
    if sys.platform == "win32":
        try:
            import winreg  # type: ignore

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WINDOWS_RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as key:
                if enabled:
                    winreg.SetValueEx(key, AUTOSTART_ENTRY_NAME, 0, winreg.REG_SZ,
                                      launch_command(tray=tray))
                else:
                    try:
                        winreg.DeleteValue(key, AUTOSTART_ENTRY_NAME)
                    except FileNotFoundError:
                        pass
            return {"ok": True, "enabled": bool(enabled), "detail": "Auto-start updated."}
        except Exception as exc:
            return {"ok": False, "enabled": get_autostart(),
                    "detail": f"Could not update the Run key: {exc}"}

    if sys.platform.startswith("linux"):
        path = _linux_autostart_path()
        try:
            if enabled:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                entry = (
                    "[Desktop Entry]\n"
                    "Type=Application\n"
                    "Name=DLNE Node\n"
                    "Comment=Distributed LLM Network Engine\n"
                    f"Exec={launch_command(tray=tray)}\n"
                    "Terminal=false\n"
                    "X-GNOME-Autostart-enabled=true\n"
                )
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(entry)
            elif os.path.exists(path):
                os.remove(path)
            return {"ok": True, "enabled": bool(enabled), "detail": "Auto-start updated."}
        except OSError as exc:
            return {"ok": False, "enabled": get_autostart(),
                    "detail": f"Could not write the autostart entry: {exc}"}

    return {"ok": False, "enabled": False,
            "detail": "Auto-start is not supported on this platform."}


# --------------------------------------------------------------------------------------
# WireGuard
# --------------------------------------------------------------------------------------

def wireguard_installed() -> bool:
    """Whether WireGuard is present — delegated to the dependency registry."""
    from engine import depends

    return depends.check_wireguard()["status"] == depends.STATUS_OK


def install_wireguard(consent: bool = False) -> Dict[str, Any]:
    """Install WireGuard through the dependency registry (which owns the vendor URLs)."""
    from engine import depends

    return depends.install("wireguard", consent=consent)


def _windows_tunnel_activation_script(conf_path: str) -> str:
    """
    The elevated PowerShell that brings the tunnel up *and* makes it usable.

    THE SECOND HALF IS NOT OPTIONAL, AND ITS ABSENCE HAS NO SYMPTOM
        DLNE's firewall rules are created with ``-Profile Private,Domain``. Windows
        classifies a WireGuard adapter as an unidentified network, which puts it in
        ``Public`` — so the rule that opens the worker's RPC port does not apply to the one
        interface the Coordinator will ever dial it on. Every inbound connection over the
        tunnel is dropped before DLNE sees it.

        Nothing about that looks broken. Heartbeats are outbound, so the node registers,
        appears in the WebUI, and has its VRAM counted into the cluster total. It simply
        never receives a single shard of inference, because the Coordinator cannot open a
        connection to it. The node looks like a full member of a cluster it cannot
        contribute to.

        Setting the *adapter* to Private is deliberately narrower than widening the rules to
        ``-Profile Any``: it opens the RPC port on the tunnel, where the Coordinator is, and
        leaves it closed on whatever café or hotspot network the machine is also attached
        to. A node's tunnel is a point-to-point link to one Coordinator; that is a private
        network by any definition, and the firewall rules already assume so.

    THE THIRD HALF, FOR THE SAME REASON
        The adapter also gets its MTU set here. A conf written before 1.1.0 carries no
        ``MTU`` line, and a Windows tunnel brought up without one takes the default —
        which on a path with any encapsulation is too large. The oversized packet is
        dropped and the ICMP notice that would explain it is filtered, so the tunnel
        carries handshakes, heartbeats and ``/health`` perfectly while inference returns a
        blank response. Setting it on the live adapter is what repairs nodes whose config
        file predates the fix, without regenerating their invite.

    Neither setting survives the adapter: reactivating the tunnel by hand builds a fresh
    one, Public and default-MTU again. That is why both are also *reported* at startup —
    see :func:`warn_if_tunnel_is_public` and :func:`wireguard.warn_if_tunnel_mtu_unset`.

    The adapter does not exist until the service starts and takes a moment to be
    identified, so both are applied in a short retry loop rather than once, immediately.
    """
    # wg-quick and wireguard.exe both name the interface after the file, so the alias is
    # the basename with its extension removed - the same derivation, not a second source.
    alias = os.path.splitext(os.path.basename(conf_path.replace("\\", "/")))[0]
    mtu = wireguard.tunnel_mtu()
    return "\n".join([
        f'& wireguard /installtunnelservice "{conf_path}"',
        f'$alias = "{alias}"',
        'for ($i = 0; $i -lt 40; $i++) {',
        # Not $profile: that is a PowerShell automatic variable holding the profile script
        # path, and assigning to it inside an elevated shell is asking for trouble.
        '    $found = Get-NetConnectionProfile -InterfaceAlias $alias -ErrorAction SilentlyContinue',
        '    if ($found) {',
        '        Set-NetConnectionProfile -InterfaceAlias $alias -NetworkCategory Private '
        '-ErrorAction SilentlyContinue',
        # Both address families: a tunnel that carries IPv6 at an oversized MTU black-holes
        # exactly as thoroughly as one that carries IPv4 at one.
        f'        Set-NetIPInterface -InterfaceAlias $alias -AddressFamily IPv4 '
        f'-NlMtuBytes {mtu} -ErrorAction SilentlyContinue',
        f'        Set-NetIPInterface -InterfaceAlias $alias -AddressFamily IPv6 '
        f'-NlMtuBytes {mtu} -ErrorAction SilentlyContinue',
        '        break',
        '    }',
        '    Start-Sleep -Milliseconds 500',
        '}',
    ])


def warn_if_tunnel_is_public(conf_name: str = "dlne_tunnel.conf") -> str:
    """
    Say so when this node's tunnel is on a firewall profile that shuts the Coordinator out.

    :func:`activate_tunnel` sets the adapter to Private when it brings the tunnel up, but
    that lasts exactly as long as the adapter does: deactivating and reactivating the
    tunnel in the WireGuard app creates a fresh one, classified Public again, and DLNE
    cannot set it back without elevation it does not have at run time.

    So this only reports. It is worth reporting because the failure it describes has no
    other symptom — the node keeps heartbeating, keeps appearing in the WebUI, keeps having
    its VRAM counted into the cluster total, and silently never receives any work.

    Read-only, Windows-only, and skipped entirely on a node with no tunnel. Returns the
    warning text, or ``""`` when there is nothing to say or the question could not be
    answered — never an exception, because a diagnostic must not stop a node from starting.
    """
    if sys.platform != "win32":
        return ""

    conf_path = os.path.join(get_base_dir(), "wg_configs", conf_name)
    if not os.path.exists(conf_path):
        return ""  # No tunnel on this node, so nothing to be on the wrong profile.

    alias = os.path.splitext(conf_name)[0]
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-NetConnectionProfile -InterfaceAlias '{alias}' "
             "-ErrorAction SilentlyContinue).NetworkCategory"],
            capture_output=True, text=True, timeout=20,
        )
    except Exception:
        return ""

    category = (result.stdout or "").strip()
    if category.lower() != "public":
        # Includes the empty case: the tunnel is not up, and an absent adapter is not a
        # misconfigured one.
        return ""

    return (
        f"This node's WireGuard adapter ('{alias}') is on the Public firewall profile. "
        "DLNE's inbound rules are scoped to Private and Domain, so the Coordinator cannot "
        "open a connection to this node's RPC port over the tunnel — it will register, "
        "appear online, have its memory counted into the cluster, and never be given any "
        "work. Fix it from an elevated PowerShell with: Set-NetConnectionProfile "
        f"-InterfaceAlias '{alias}' -NetworkCategory Private"
    )


def activate_tunnel(conf_path: str) -> Dict[str, Any]:
    """
    Bring up a WireGuard tunnel from a ``.conf`` file.

    Split out from installation because the two fail for entirely different reasons and a
    wizard has to say which one happened: "WireGuard isn't installed" is an offer, while
    "the tunnel refused to start" is a routing problem the user has to look at.

    Returns:
        ``{"ok", "detail"}``.
    """
    import base64

    conf_path = os.path.abspath(conf_path)
    if not os.path.exists(conf_path):
        return {"ok": False, "detail": f"Tunnel configuration not found: {conf_path}"}

    try:
        if sys.platform == "win32":
            # -EncodedCommand rather than a quoted -Command string: this script is nested
            # two levels deep (a PowerShell that elevates a PowerShell) and carries a path
            # that routinely contains spaces. Base64 has no characters either shell has an
            # opinion about, so there is nothing left to quote wrongly.
            script = _windows_tunnel_activation_script(conf_path)
            encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Start-Process powershell -Verb RunAs -ArgumentList "
                 f"'-NoProfile','-WindowStyle','Hidden','-EncodedCommand','{encoded}'"],
                check=True, timeout=120
            )
        else:
            subprocess.run(["sudo", "wg-quick", "up", conf_path], check=True, timeout=120)
    except subprocess.TimeoutExpired:
        return {"ok": False, "detail": "The tunnel did not come up in time."}
    except Exception as exc:
        return {"ok": False, "detail": f"Tunnel activation failed: {exc}"}

    return {"ok": True, "detail": "Secure tunnel connected."}


# --------------------------------------------------------------------------------------
# Public IP detection
# --------------------------------------------------------------------------------------

def detect_public_ip(timeout: float = 5.0) -> str:
    """
    This machine's public IPv4 as seen from the internet, or ``""``.

    Used to pre-fill the endpoint baked into WireGuard invites. IPv4 specifically: the
    invite's ``Endpoint`` has to be reachable by workers that may have no IPv6 path at all.
    """
    import urllib.request

    try:
        with urllib.request.urlopen("https://api4.ipify.org", timeout=timeout) as response:
            return response.read().decode("utf8").strip()
    except Exception:
        return ""


# --------------------------------------------------------------------------------------
# Dependency preflight passthrough
# --------------------------------------------------------------------------------------

def dependency_report(mode: str = "", needs_gui: bool = False,
                      needs_wan: bool = False) -> Dict[str, Any]:
    """
    The System Check page's data: every dependency that applies, plus a rollup.

    Kept here rather than having each wizard import ``engine.depends`` directly so both
    surfaces build the context the same way — in particular ``needs_wan``, which decides
    whether WireGuard is even asked about.
    """
    from engine import depends

    context = depends.build_context(mode=mode, needs_gui=needs_gui, needs_wan=needs_wan)
    report = depends.preflight(context)
    return {"context": context, "items": report, "summary": depends.summarize(report)}


def install_dependency(dependency_id: str, context: Optional[Dict[str, Any]] = None,
                       consent: bool = False) -> Dict[str, Any]:
    """Install one dependency after the user consented. See ``engine.depends.install``."""
    from engine import depends

    return depends.install(dependency_id, context, consent=consent)
