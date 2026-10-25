"""
WireGuard orchestration for DLNE's optional WAN transport.

This module only ever *generates* configuration: it creates the Coordinator's server
interface and the worker invite that a remote node imports into its WireGuard client.
Bringing the tunnel up is handled by ``setup_cli.auto_install_wireguard``.

Bug #22 — why this file no longer has a "fallback" key path:
    ``generate_keypair`` used to return the literal strings ``FALLBACK_PRIV_KEY`` /
    ``FALLBACK_PUB_KEY`` whenever the ``wg`` command line tool was missing, and the
    callers wrote those strings straight into real ``.conf`` files. The result was a
    file that looks completely valid, imports cleanly into the WireGuard client, and
    then silently never completes a handshake — a dead tunnel that is extremely hard to
    diagnose from the operator's side. Failing loudly at generation time is the only
    honest option, so key generation now returns ``(None, None)`` and the two config
    writers raise :class:`WireGuardUnavailableError` *before* touching the filesystem.

Audit item 3.4 — why every invite now gets its own identity:
    ``generate_worker_invite`` used to hardcode ``Address = 10.8.0.2/24``, and the setup
    wizard produced the redundancy invite by *copying* the worker invite byte for byte.
    Every node in the cluster therefore shared one private key and one tunnel address,
    and it never registered any of them as a peer on the Coordinator's own interface.
    WireGuard identifies a peer by its public key and routes to it by its allowed IP, so
    that arrangement can only ever have one node connected at a time — the WAN
    multi-node story did not work at all. Invites now get a freshly generated keypair and
    the next free address in the ``10.8.0.0/24`` tunnel subnet, the allocator's high-water
    mark is persisted in ``config/config.txt`` as ``WG_NEXT_PEER_OCTET``, and each peer's
    own public key is appended to ``coordinator_wg0.conf`` as a ``[Peer]`` section.
"""

import os
import re
import shutil
import subprocess
import sys
import threading
from typing import Optional, Set, Tuple

from engine.config import get_base_dir, get_config, get_worker_limits, set_config_values
from engine.logger import log_system, log_error, log_warning


class WireGuardUnavailableError(RuntimeError):
    """
    Raised when a genuine WireGuard keypair cannot be produced on this machine.

    Callers are expected to catch this and tell the operator what to install, then fall
    back to LAN / their own VPN. It must never be swallowed silently: an unhandled
    WireGuard failure previously turned into a config file full of placeholder keys.
    """


# The Windows installer normally puts wg.exe on PATH, but a machine that has had the
# installer run under a different account (or a portable extraction) will not have it.
# Probing the standard install locations avoids a false "WireGuard is missing" verdict.
_WINDOWS_WG_CANDIDATES: Tuple[str, ...] = (
    os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "WireGuard", "wg.exe"),
    os.path.join(os.environ.get("ProgramW6432", r"C:\Program Files"), "WireGuard", "wg.exe"),
    os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "WireGuard", "wg.exe"),
)

# --------------------------------------------------------------------------------------
# Tunnel address plan
# --------------------------------------------------------------------------------------
#
# DLNE runs one flat /24 for the whole cluster. The Coordinator is always .1, and every
# invited node gets exactly one address from .2 upwards. These are module constants rather
# than literals so the invite writer, the peer registrar and the allocator can never drift
# apart from each other.
WG_SUBNET_PREFIX: str = "10.8.0"
WG_SUBNET_CIDR: str = f"{WG_SUBNET_PREFIX}.0/24"
WG_COORDINATOR_OCTET: int = 1
WG_FIRST_PEER_OCTET: int = 2
# .255 is the broadcast address of the /24, so .254 is the last usable host.
WG_LAST_PEER_OCTET: int = 254

# The Coordinator's interface. wg-quick names the interface after the file, so the two are
# the same string by construction and are written once here rather than in both places.
WG_INTERFACE_NAME: str = "coordinator_wg0"
WG_COORDINATOR_CONF_NAME: str = f"{WG_INTERFACE_NAME}.conf"

# The allocator's high-water mark, persisted in config/config.txt so that invites issued
# across separate runs of the setup wizard never collide. Flat KEY=VALUE like every other
# DLNE setting.
WG_NEXT_PEER_OCTET_KEY: str = "WG_NEXT_PEER_OCTET"

# Serialises allocation inside this process. The persisted counter is the cross-process
# story; this lock stops two threads of the same process (the wizard generating the worker
# and redundancy invites, an operator-triggered re-issue) from reading the same value.
_ALLOCATION_LOCK = threading.Lock()

# Matches the address a registered peer was given, e.g. "AllowedIPs = 10.8.0.7/32".
# Reading the addresses back out of coordinator_wg0.conf is what makes the allocator
# self-healing: if config.txt is restored from an older backup, or was hand-edited, the
# counter is still floored above every address that is demonstrably already in use.
_PEER_ADDRESS_PATTERN = re.compile(
    r"^\s*AllowedIPs\s*=\s*" + re.escape(WG_SUBNET_PREFIX) + r"\.(\d{1,3})\s*/",
    re.IGNORECASE | re.MULTILINE,
)

# Matches the MTU line in an [Interface] stanza, for reading a tunnel's configured value
# back and for repairing a conf written before DLNE set one at all.
_MTU_PATTERN = re.compile(r"^[ \t]*MTU[ \t]*=[ \t]*(\d+)[ \t]*$", re.IGNORECASE | re.MULTILINE)

# Where the MTU line is inserted when repairing a conf that predates it: immediately after
# the Address line, which every [Interface] DLNE has ever written carries.
_ADDRESS_LINE_PATTERN = re.compile(
    r"^([ \t]*Address[ \t]*=[ \t]*\S+[ \t]*)$", re.IGNORECASE | re.MULTILINE
)


def tunnel_mtu() -> int:
    """
    The MTU DLNE writes into every WireGuard ``[Interface]`` it generates.

    Read through the typed config view so an operator on a path that needs something
    lower than the default can set ``WG_MTU`` without editing generated files by hand —
    and so every writer, the repair path and the startup diagnostic all agree on one
    number. See ``config.get_worker_limits`` for why the default is 1380.
    """
    return int(get_worker_limits().get("wg_mtu", 1380))


def configured_tunnel_mtu(conf_path: str) -> Optional[int]:
    """
    The MTU a WireGuard config declares, or None when it declares none or cannot be read.

    None is "the file says nothing", never "the file says zero" — the caller's whole
    question is whether an ``MTU`` line is present, so an unreadable file and a file
    without the line must answer the same way and neither may be mistaken for a value.
    """
    try:
        with open(conf_path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None

    match = _MTU_PATTERN.search(text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def repair_missing_mtu(conf_path: str) -> bool:
    """
    Add the configured ``MTU`` to a tunnel config DLNE wrote before it set one.

    Every config generated before 1.1.0 has an ``[Interface]`` with no ``MTU`` line, and
    those files are only written once — at enrollment. Without this, the fix for a tunnel
    that black-holes large packets would reach new nodes and no existing one.

    Only ever *adds* a line, and only when none is present: an operator who tuned the MTU
    by hand has made a deliberate decision about their own path, and this must not
    silently overwrite it. Returns False for that case exactly as it does for a file that
    could not be read, because in both the file is left as it was found.

    Args:
        conf_path: A WireGuard ``.conf`` DLNE owns.

    Returns:
        True only when a line was genuinely added.
    """
    try:
        with open(conf_path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return False

    if _MTU_PATTERN.search(text):
        return False  # Already set, by us or by the operator. Theirs wins.

    # Anchored to Address rather than to the [Interface] header: it is the one line every
    # [Interface] DLNE has ever written carries, and inserting after it keeps the stanza
    # in the order a human reading the file expects.
    patched, count = _ADDRESS_LINE_PATTERN.subn(
        lambda m: f"{m.group(1)}\nMTU = {tunnel_mtu()}", text, count=1
    )
    if count != 1:
        return False

    try:
        with open(conf_path, "w", encoding="utf-8") as f:
            f.write(patched)
    except OSError as exc:
        log_warning(f"Could not add an MTU to {conf_path}: {exc}")
        return False

    return True


def warn_if_tunnel_mtu_unset(conf_path: str) -> Optional[str]:
    """
    Say so when a tunnel config carries no MTU, and name what that costs.

    The other failure with no symptom. WireGuard encapsulation adds ~80 bytes; when the
    result exceeds the path MTU and the ICMP "fragmentation needed" reply is filtered —
    the norm on residential links — the tunnel passes small packets and silently drops
    large ones. Handshakes complete, heartbeats arrive, ``/health`` answers, the node
    appears healthy in every view an operator has, and inference returns a blank
    response. Diagnosed live on 2026-08-10; setting an MTU on both ends fixed it outright.

    Read-only and silent on every uncertain case: no file, an unreadable file, or a file
    that already declares one.

    Returns:
        A message naming the file and the fix, or None when there is nothing to say.
    """
    try:
        if not os.path.isfile(conf_path):
            return None
        if configured_tunnel_mtu(conf_path) is not None:
            return None

        message = (
            f"{conf_path} sets no MTU. WireGuard's encapsulation overhead can then push "
            "packets past what the path actually carries, and because the ICMP reply that "
            "would say so is usually filtered, the tunnel passes small packets and drops "
            "large ones without a word — enrollment and heartbeats work while inference "
            f"returns nothing. DLNE writes MTU = {tunnel_mtu()} into the configs it "
            "generates; re-running setup, or adding that line to the [Interface] section "
            "and restarting the tunnel, applies it here."
        )
        log_warning(message)
        return message
    except Exception:
        # A diagnostic must never be the reason a node fails to start.
        return None


# Shown to the operator whenever key generation fails, so the message is identical no
# matter which entry point hit the problem.
_INSTALL_HINT = (
    "Install the WireGuard tools and re-run setup: "
    "Windows -> https://www.wireguard.com/install/ , "
    "Debian/Ubuntu -> 'sudo apt install wireguard-tools' , "
    "Arch -> 'sudo pacman -S wireguard-tools' , "
    "macOS -> 'brew install wireguard-tools'."
)


# Substrings WireGuard's tools use when the kernel refuses the query. Matched on the text
# because the exit code does not distinguish "you may not ask" from "there is nothing to
# ask about", and those two need opposite responses from us.
_PERMISSION_MARKERS: Tuple[str, ...] = (
    "operation not permitted",
    "permission denied",
    "access is denied",
    "access denied",
)

# Whether this process has already explained the privilege situation. Reading an interface
# and modifying one both need CAP_NET_ADMIN, and a Coordinator that lacks it asks on every
# start and again on every enrollment. The answer is a standing fact about how the node was
# launched, so saying it once is informative and saying it every time buries everything
# else in the log — which is how the original failure went unnoticed for weeks.
_privilege_notice_lock = threading.Lock()
_privilege_notice_given = False


def _looks_like_permission_denial(stderr: str) -> bool:
    lowered = stderr.lower()
    return any(marker in lowered for marker in _PERMISSION_MARKERS)


def _log_privilege_notice_once() -> None:
    """Explain what unprivileged operation actually costs, once per process."""
    global _privilege_notice_given

    with _privilege_notice_lock:
        if _privilege_notice_given:
            return
        _privilege_notice_given = True

    if sys.platform == "win32":
        remedy = "Run DLNE as Administrator."
    else:
        remedy = (
            "Run the node with sufficient privilege (sudo), or grant it CAP_NET_ADMIN."
        )
    log_warning(
        f"This node is not permitted to read or modify the {WG_INTERFACE_NAME} interface. "
        "Two things follow, and neither is visible from the outside: a peer that enrolls "
        "cannot be loaded into the running interface, so it stays in the config file and "
        "cannot complete a handshake until the interface is restarted; and the check that "
        "compares the running interface against that file cannot run, so a mismatch will "
        f"not be reported. {remedy} Reported once per run."
    )


def _hidden_process_kwargs() -> dict:
    """
    Extra ``subprocess`` keyword arguments that suppress the console window on Windows.

    DLNE ships as a windowed executable; without this flag every ``wg`` invocation
    flashes a black console box in the user's face during setup.
    """
    if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


class WireGuardOrchestrator:
    def __init__(self):
        self.config_dir = os.path.join(get_base_dir(), "wg_configs")
        # exist_ok covers the race where two setup paths construct the orchestrator at
        # nearly the same moment (invite auto-join plus a manual run).
        os.makedirs(self.config_dir, exist_ok=True)

    # ----------------------------------------------------------------------------------
    # Paths
    # ----------------------------------------------------------------------------------

    @property
    def coordinator_conf_path(self) -> str:
        """Absolute path of the Coordinator's own WireGuard interface configuration."""
        return os.path.join(self.config_dir, WG_COORDINATOR_CONF_NAME)

    @property
    def restart_interface_command(self) -> str:
        """
        The command an operator must run to reload the Coordinator's interface from its file.

        Printed by two different diagnostics, both of which used to say
        ``wg-quick down coordinator_wg0``. That command cannot work: wg-quick's bare-name
        form resolves against ``/etc/wireguard/``, and DLNE keeps this file in its own
        ``wg_configs/`` directory, so the operator is told to run something that fails with
        "does not exist" while the interface it names is plainly running. The full path is
        the form wg-quick accepts for a file it does not own. Windows has no wg-quick at
        all and drives tunnels as services instead, so it gets its own phrasing rather than
        advice it cannot follow.
        """
        conf = self.coordinator_conf_path
        if sys.platform == "win32":
            return (
                f'wireguard /uninstalltunnelservice {WG_INTERFACE_NAME}  (then)  '
                f'wireguard /installtunnelservice "{conf}"'
            )
        return f"sudo wg-quick down '{conf}' && sudo wg-quick up '{conf}'"

    def _find_wg_executable(self) -> Optional[str]:
        """Returns the absolute path of the ``wg`` binary, or None when it is not installed."""
        found = shutil.which("wg")
        if found:
            return found

        if sys.platform == "win32":
            for candidate in _WINDOWS_WG_CANDIDATES:
                if os.path.isfile(candidate):
                    return candidate

        return None

    def _run_wg_cmd(self, *args, stdin_text: Optional[str] = None) -> Optional[str]:
        """
        Runs ``wg <args>`` and returns its trimmed stdout, or None on any failure.

        Every failure mode is logged rather than swallowed — a silent None here is what
        produced the placeholder keys of bug #22.
        """
        executable = self._find_wg_executable()
        if executable is None:
            log_error(f"WireGuard tools are not installed ('wg' not found on PATH). {_INSTALL_HINT}")
            return None

        try:
            result = subprocess.run(
                [executable, *args],
                input=stdin_text,
                capture_output=True,
                text=True,
                check=True,
                # wg genkey/pubkey are pure computation; anything slower than this is a
                # hung process and must not stall the interactive setup wizard forever.
                timeout=15,
                **_hidden_process_kwargs(),
            )
            return result.stdout.strip()
        except FileNotFoundError:
            # The binary vanished between the lookup and the call (uninstall mid-setup).
            log_error(f"WireGuard tools disappeared while running 'wg {' '.join(args)}'. {_INSTALL_HINT}")
            return None
        except subprocess.TimeoutExpired:
            log_error(f"WireGuard command 'wg {' '.join(args)}' timed out after 15 seconds.")
            return None
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            if _looks_like_permission_denial(stderr):
                # Not a fault to report on every occurrence. The raw text ("Unable to
                # access interface: Operation not permitted") describes the symptom of a
                # deliberate launch decision, and repeating it per command says nothing
                # the one notice does not say better.
                _log_privilege_notice_once()
                return None
            log_error(f"WireGuard command 'wg {' '.join(args)}' failed: {stderr or f'exit code {exc.returncode}'}")
            return None
        except OSError as exc:
            log_error(f"WireGuard command 'wg {' '.join(args)}' could not be started: {exc}")
            return None

    def generate_keypair(self) -> Tuple[Optional[str], Optional[str]]:
        """
        Generates a WireGuard private/public keypair using the ``wg`` tool.

        Returns:
            ``(private_key, public_key)`` as base64 strings on success, or
            ``(None, None)`` when WireGuard is not installed or the tool failed. The
            caller MUST check for None: writing a placeholder into a ``.conf`` produces
            a tunnel that looks correct and never connects (bug #22).
        """
        privkey = self._run_wg_cmd("genkey")
        if not privkey:
            log_error("Unable to generate a WireGuard private key — no keypair was produced.")
            return None, None

        # 'wg pubkey' derives the public half from the private key on stdin. The trailing
        # newline matches what a shell pipe would send, which some builds expect.
        pubkey = self._run_wg_cmd("pubkey", stdin_text=privkey + "\n")
        if not pubkey:
            log_error("Unable to derive the WireGuard public key — no keypair was produced.")
            return None, None

        return privkey, pubkey

    # ----------------------------------------------------------------------------------
    # Tunnel address allocation (audit item 3.4)
    # ----------------------------------------------------------------------------------

    def _octets_in_use(self) -> Set[int]:
        """
        Returns the host octets already handed out, read back from the Coordinator config.

        The persisted counter in ``config.txt`` is the primary source of truth, but it can
        legitimately be behind: config.txt is restored from a backup, or an operator adds
        a peer by hand. Every peer that is actually registered on the Coordinator's
        interface carries its address in an ``AllowedIPs`` line, so parsing them back is a
        cheap way to guarantee the allocator never re-issues a live address.
        """
        used: Set[int] = {WG_COORDINATOR_OCTET}

        try:
            with open(self.coordinator_conf_path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except FileNotFoundError:
            # No Coordinator interface on this machine (a worker running the wizard, or a
            # hub whose config lives elsewhere). The persisted counter is all we have.
            return used
        except OSError as exc:
            log_warning(
                f"Could not read {self.coordinator_conf_path} while allocating a tunnel "
                f"address: {exc}. Falling back to the counter in config.txt alone."
            )
            return used

        for match in _PEER_ADDRESS_PATTERN.finditer(text):
            octet = int(match.group(1))
            if 0 <= octet <= 255:
                used.add(octet)

        return used

    def _allocate_peer_octet(self) -> int:
        """
        Reserves and returns the next free host octet in the tunnel subnet.

        The reservation is persisted immediately (``WG_NEXT_PEER_OCTET`` in
        ``config/config.txt``) rather than after the invite is written, so a crash between
        the two hands out a *gap* rather than a duplicate — and a duplicate address is the
        exact failure this whole mechanism exists to prevent.

        Raises:
            WireGuardUnavailableError: when the /24 is exhausted. 253 nodes is far beyond
                anything DLNE targets, but silently wrapping around to .2 would corrupt a
                working cluster, so it is an explicit error.
        """
        with _ALLOCATION_LOCK:
            used = self._octets_in_use()

            # The counter is a floor, not the answer: it may be stale (restored config) or
            # ahead of the interface file (invites issued for a hub configured elsewhere).
            try:
                counter = int(get_config().get(WG_NEXT_PEER_OCTET_KEY, WG_FIRST_PEER_OCTET))
            except ValueError:
                log_warning(
                    f"{WG_NEXT_PEER_OCTET_KEY} in config.txt is not a number; restarting "
                    f"tunnel address allocation from {WG_SUBNET_PREFIX}.{WG_FIRST_PEER_OCTET}."
                )
                counter = WG_FIRST_PEER_OCTET

            candidate = max(counter, WG_FIRST_PEER_OCTET)
            while candidate in used and candidate <= WG_LAST_PEER_OCTET:
                candidate += 1

            if candidate > WG_LAST_PEER_OCTET:
                raise WireGuardUnavailableError(
                    f"The DLNE tunnel subnet {WG_SUBNET_CIDR} is full: every address from "
                    f"{WG_SUBNET_PREFIX}.{WG_FIRST_PEER_OCTET} to "
                    f"{WG_SUBNET_PREFIX}.{WG_LAST_PEER_OCTET} is already assigned. "
                    "Remove retired peers from coordinator_wg0.conf and reset "
                    f"{WG_NEXT_PEER_OCTET_KEY} in config/config.txt."
                )

            # Persisted before the caller writes anything, so the address is spoken for
            # even if invite generation fails immediately afterwards.
            if not set_config_values({WG_NEXT_PEER_OCTET_KEY: str(candidate + 1)}):
                raise WireGuardUnavailableError(
                    "Could not persist the WireGuard address allocation to config.txt. "
                    "Refusing to issue an invite, because a second invite generated after "
                    "this one would be handed the same tunnel address and neither node "
                    "would be able to connect."
                )

            return candidate

    def _apply_peer_to_live_interface(self, peer_pubkey: str, octet: int,
                                      peer_name: str) -> None:
        """
        Load one peer into the *running* interface, not just the file it was written to.

        WireGuard reads its configuration once, when the interface is brought up. A
        ``[Peer]`` appended to the ``.conf`` of a live interface is invisible to the kernel,
        which then drops that node's handshakes as coming from a key it has never heard of.
        Enrollment answers 200, the worker writes a perfectly correct tunnel config, both
        ends look right, and nothing ever connects — with no restart of the Coordinator's
        tunnel, which nothing in this product performs, it never will.

        ``wg set peer`` is an upsert and is safe to repeat, so re-enrollment runs it too:
        that is what repairs a peer registered into the file during an earlier version.

        Silent when the interface is not up — then the file *is* the whole story, and it
        will be read at the next bring-up.
        """
        interfaces = self._run_wg_cmd("show", "interfaces")
        if interfaces is None or WG_INTERFACE_NAME not in interfaces.split():
            return

        # wg set prints nothing on success, so "" is success and None is failure. Do not
        # collapse these with `or`.
        applied = self._run_wg_cmd(
            "set", WG_INTERFACE_NAME, "peer", peer_pubkey,
            "allowed-ips", f"{WG_SUBNET_PREFIX}.{octet}/32",
        )
        if applied is None:
            log_error(
                f"Peer '{peer_name}' was recorded in {self.coordinator_conf_path} but could "
                f"not be loaded into the running {WG_INTERFACE_NAME} interface. That node "
                "will not complete a handshake until the interface is restarted: "
                f"{self.restart_interface_command}. Adding peers to a live interface needs "
                "the same privileges as bringing it up."
            )
            return

        log_system(
            f"Peer '{peer_name}' loaded into the running {WG_INTERFACE_NAME} interface."
        )

    def _register_peer(self, peer_pubkey: str, octet: int, peer_name: str) -> None:
        """
        Appends a ``[Peer]`` section for one invited node to ``coordinator_wg0.conf``.

        This is the half that was missing entirely before: WireGuard identifies a peer by
        its public key and will drop handshakes from any key it does not know, so an
        invite whose key was never registered here can never connect no matter how
        correct the invite itself is.

        ``AllowedIPs`` is a /32 — the single address this peer owns. A /24 here would make
        the Coordinator route the whole tunnel subnet to whichever peer was listed last.
        """
        peer_block = (
            f"\n[Peer]\n"
            f"# DLNE {peer_name}\n"
            f"PublicKey = {peer_pubkey}\n"
            f"AllowedIPs = {WG_SUBNET_PREFIX}.{octet}/32\n"
        )

        try:
            with open(self.coordinator_conf_path, "a", encoding="utf-8") as f:
                f.write(peer_block)
        except FileNotFoundError:
            # The Coordinator's interface lives on another machine (this node is only
            # generating invites for a hub it does not host). Print the exact block so the
            # operator can paste it, instead of shipping an invite that silently dies.
            log_error(
                f"No Coordinator interface at {self.coordinator_conf_path}, so the new "
                f"'{peer_name}' peer could not be registered automatically. Add this block "
                f"to the Coordinator's WireGuard configuration or the node will never "
                f"complete a handshake:{peer_block}"
            )
            return
        except OSError as exc:
            log_error(
                f"Could not register the '{peer_name}' peer in "
                f"{self.coordinator_conf_path}: {exc}. Add this block manually or the node "
                f"will never complete a handshake:{peer_block}"
            )
            return

        log_system(
            f"Registered WireGuard peer '{peer_name}' at {WG_SUBNET_PREFIX}.{octet} "
            f"in {os.path.basename(self.coordinator_conf_path)}."
        )
        self._apply_peer_to_live_interface(peer_pubkey, octet, peer_name)

    def coordinator_private_key(self) -> Optional[str]:
        """
        The private key out of this hub's own ``[Interface]`` block, or None.

        The single reader of that value. It is what proves an interface already exists and
        must not be replaced, and what the public half is derived from — two questions that
        were answered by two copies of this loop and would have drifted apart eventually.

        Returns:
            The base64 private key, or None when there is no Coordinator interface here or
            its ``[Interface]`` block carries no key.
        """
        try:
            with open(self.coordinator_conf_path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            return None

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if line.lower().startswith("privatekey"):
                _, _, value = line.partition("=")
                return value.strip() or None
            if line.lower().startswith("[peer]"):
                # Past the [Interface] block without finding a key; the peers below carry
                # public keys belonging to other nodes, and returning one of those would be
                # actively harmful.
                break

        return None

    def coordinator_public_key(self) -> Optional[str]:
        """
        This hub's own WireGuard public key, derived from its interface configuration.

        :meth:`setup_coordinator_server` returns the public key once, at creation time, and
        nothing persists it — which was fine while every invite was minted in that same
        wizard run. Enrollment happens later, in a different process, so the key has to be
        recoverable. It is: the public half is a pure function of the private half, and the
        private half is in ``coordinator_wg0.conf``.

        Returns:
            The base64 public key, or None when this machine hosts no Coordinator
            interface or the WireGuard tools are unavailable to derive it.
        """
        private_key = self.coordinator_private_key()
        if not private_key:
            return None
        return self._run_wg_cmd("pubkey", stdin_text=private_key + "\n") or None

    def live_interface_public_key(self) -> Optional[str]:
        """
        The public key the *running* ``coordinator_wg0`` interface is actually using.

        Read from the kernel rather than from the file, which is the entire point: the two
        can disagree, and when they do, nothing else notices.

        Returns:
            The base64 public key, or None when the interface is not up, ``wg`` is not
            installed, or the caller lacks the privilege to ask. None means *unknown* and
            never *mismatched* — no caller may treat it as evidence of anything.
        """
        # Asked for the interface list first, because ``wg show <name>`` on an interface
        # that is not up exits non-zero, and _run_wg_cmd logs every non-zero exit. A node
        # that simply has no tunnel would print an error on every start for a question it
        # was never obliged to answer. Listing exits cleanly and just returns nothing.
        interfaces = self._run_wg_cmd("show", "interfaces")
        if interfaces is None or WG_INTERFACE_NAME not in interfaces.split():
            return None

        return self._run_wg_cmd("show", WG_INTERFACE_NAME, "public-key") or None

    def configured_peer_keys(self) -> Set[str]:
        """
        The public keys of every ``[Peer]`` block in the Coordinator's config file.

        Only keys below a ``[Peer]`` header count. The ``[Interface]`` block has no
        ``PublicKey`` line of its own today, but reading one as a peer would report this
        hub as a peer of itself, and that is the kind of thing a file edited by hand
        eventually produces.
        """
        keys: Set[str] = set()
        try:
            with open(self.coordinator_conf_path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            return keys

        in_peer = False
        for raw_line in text.splitlines():
            line = raw_line.strip()
            lowered = line.lower()
            if lowered.startswith("["):
                in_peer = lowered.startswith("[peer]")
            elif in_peer and lowered.startswith("publickey"):
                _, _, value = line.partition("=")
                # Base64 keys end in '=' padding, which partition() ate along with the
                # separator. Re-attach it or every key comes back subtly wrong.
                value = value.strip()
                if value and not value.endswith("="):
                    value += "="
                if value:
                    keys.add(value)

        return keys

    def live_peer_keys(self) -> Optional[Set[str]]:
        """
        The public keys the *running* interface will actually accept a handshake from.

        Returns:
            The set of peer keys, or None when the interface is not up or the question
            could not be asked. None means *unknown*, never *empty*.
        """
        interfaces = self._run_wg_cmd("show", "interfaces")
        if interfaces is None or WG_INTERFACE_NAME not in interfaces.split():
            return None

        listed = self._run_wg_cmd("show", WG_INTERFACE_NAME, "peers")
        if listed is None:
            return None
        return {line.strip() for line in listed.splitlines() if line.strip()}

    def warn_if_interface_diverged(self) -> Optional[str]:
        """
        Say so, loudly, when the running interface is not the one in the config file.

        This is the failure that has no symptom. ``/worker/enroll`` hands out the key
        derived from ``coordinator_wg0.conf``; the interface answers with whatever it was
        started with. If those differ — because the file was rewritten while the tunnel was
        up, and nothing restarts it — then every worker builds a tunnel pinned to a key the
        server does not hold. Each side looks correctly configured. No handshake ever
        completes. Nothing is logged, because from each end nothing went wrong.

        Fails open in every uncertain case: an interface that is not running, tools that
        are absent, a key that cannot be read. Only a definite disagreement is reported.

        Returns:
            A message describing the divergence, or None when the two agree or the question
            could not be answered.
        """
        try:
            # Nothing configured here means nothing to disagree with, and asking WireGuard
            # anything at all on a Coordinator that runs no tunnel is pure noise.
            if not self.coordinator_private_key():
                return None

            live = self.live_interface_public_key()
            if not live:
                return None  # Not up, or not askable. Neither is a divergence.

            problems = []

            configured = self.coordinator_public_key()
            if configured and configured != live:
                problems.append(
                    f"The running {WG_INTERFACE_NAME} interface is using a different key "
                    f"than {self.coordinator_conf_path} (running {live}, configured "
                    f"{configured}). Workers are handed the configured key when they "
                    "enroll, so every tunnel they build is pinned to a key this machine is "
                    "not answering with, and no handshake can complete."
                )

            # The identity is only half of it. A peer written to the file while the
            # interface was up is equally invisible to the kernel, and equally silent:
            # WireGuard drops handshakes from keys it was never told about, without
            # logging anything, because from its side nothing arrived that it recognised.
            live_peers = self.live_peer_keys()
            if live_peers is not None:
                missing = sorted(self.configured_peer_keys() - live_peers)
                if missing:
                    problems.append(
                        f"{len(missing)} enrolled peer(s) exist in "
                        f"{self.coordinator_conf_path} but not on the running "
                        f"{WG_INTERFACE_NAME} interface ({', '.join(missing)}). Those nodes "
                        "cannot complete a handshake however correct their own "
                        "configuration is."
                    )

            if not problems:
                return None

            message = " ".join(problems) + (
                " Restart the interface to load the file: "
                f"{self.restart_interface_command}"
            )
            log_warning(message)
            return message
        except Exception:
            # Diagnostics must never be the thing that stops a Coordinator from starting.
            return None

    def find_peer_octet(self, peer_pubkey: str) -> Optional[int]:
        """
        The tunnel octet already registered to a public key, or None if it is unknown.

        This is what makes enrollment idempotent. A node that re-enrolls — because it
        restarted, because setup was re-run, because a heartbeat 401'd and it retried —
        presents the same public key it generated the first time, and must be handed back
        the same address rather than burning a new one. Without this a node that reboots
        daily walks up the /24 and exhausts it in eight months.

        Parsed rather than tracked in config.txt because ``coordinator_wg0.conf`` is the
        file WireGuard actually obeys: an operator who deletes a peer block by hand has
        genuinely retired that node, and the allocator should agree with them.

        Args:
            peer_pubkey: Base64 public key, exactly as ``wg pubkey`` emits it.

        Returns:
            The host octet, or None when the key is not registered (or the interface file
            does not exist on this machine).
        """
        if not peer_pubkey:
            return None

        try:
            with open(self.coordinator_conf_path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            return None

        # Walk the peer blocks in order, remembering the most recent PublicKey, and answer
        # with the AllowedIPs that follows it. Line-oriented rather than one regex because
        # the two fields can legitimately appear in either order within a block.
        current_key: Optional[str] = None
        pending_octet: Optional[int] = None
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if line.lower().startswith("[peer]"):
                current_key, pending_octet = None, None
                continue
            if line.lower().startswith("publickey"):
                _, _, value = line.partition("=")
                current_key = value.strip()
                if current_key == peer_pubkey and pending_octet is not None:
                    return pending_octet
                continue
            match = _PEER_ADDRESS_PATTERN.match(raw_line)
            if match:
                octet = int(match.group(1))
                if current_key == peer_pubkey:
                    return octet
                pending_octet = octet

        return None

    def enroll_peer(self, peer_pubkey: str, peer_name: str = "worker") -> int:
        """
        Give a self-generated public key a tunnel address, allocating one only if needed.

        The Coordinator half of self-enrollment. The node generated its own keypair and
        kept the private half; all this sees is the public one, which is exactly the point
        — a join file that carries no private key can be handed to any number of nodes.

        Args:
            peer_pubkey: The node's WireGuard public key.
            peer_name: Human label recorded beside the peer in ``coordinator_wg0.conf``.

        Returns:
            The host octet assigned to this key, whether freshly allocated or already held.

        Raises:
            WireGuardUnavailableError: when the tunnel subnet is exhausted, or the
                allocation could not be persisted. Both come from
                :meth:`_allocate_peer_octet`, which refuses to hand out an address it
                cannot record.
        """
        if not peer_pubkey:
            raise WireGuardUnavailableError("Cannot enroll a peer with no public key.")

        # Re-enrollment is the common case once a cluster has been running a while, so it
        # is checked first and costs one file read.
        existing = self.find_peer_octet(peer_pubkey)
        if existing is not None:
            log_system(
                f"WireGuard peer '{peer_name}' re-enrolled at "
                f"{WG_SUBNET_PREFIX}.{existing} (already registered)."
            )
            # Re-applied, not assumed. Being in the file is not the same as being in the
            # running interface, and a peer written there by an earlier version never
            # reached the kernel at all. This is the retry that repairs it, and re-enrolling
            # is exactly when a node that cannot connect comes back to ask again.
            self._apply_peer_to_live_interface(peer_pubkey, existing, peer_name)
            return existing

        octet = self._allocate_peer_octet()
        self._register_peer(peer_pubkey, octet, peer_name)
        return octet

    def write_member_conf(self, private_key: str, octet: int, coordinator_pubkey: str,
                          coordinator_endpoint: str, network_key: str = "",
                          filename: str = "dlne_tunnel.conf") -> str:
        """
        Write this node's own tunnel configuration, from a keypair it generated itself.

        The worker half of self-enrollment, and deliberately the same file shape
        :meth:`generate_worker_invite` produces — so ``wg-quick`` and
        ``setup_core.activate_tunnel`` treat an enrolled node and a hand-invited one
        identically, and neither path becomes the untested one.

        Args:
            private_key: This node's WireGuard private key. Generated locally by
                :meth:`generate_keypair` and never transmitted anywhere.
            octet: The host octet the Coordinator assigned during enrollment.
            coordinator_pubkey: The hub's public key, from the join file.
            coordinator_endpoint: ``host:port`` the hub's WireGuard listens on.

        Returns:
            Absolute path of the written configuration.
        """
        conf_path = os.path.join(self.config_dir, os.path.basename(filename))
        with open(conf_path, "w", encoding="utf-8") as f:
            f.write("[Interface]\n")
            f.write(f"PrivateKey = {private_key}\n")
            # The /24 mask is what installs the route to the rest of the tunnel; a /32
            # here would leave this node able to reach the Coordinator and nothing else.
            f.write(f"Address = {WG_SUBNET_PREFIX}.{octet}/24\n")
            f.write(f"MTU = {tunnel_mtu()}\n\n")

            f.write("[Peer]\n")
            f.write(f"PublicKey = {coordinator_pubkey}\n")
            f.write(f"Endpoint = {coordinator_endpoint}\n")
            f.write(f"AllowedIPs = {WG_SUBNET_CIDR}\n")
            f.write("PersistentKeepalive = 25\n")

            # Written in the same comment form a minted invite uses, so an enrolled node's
            # tunnel file is byte-shape-identical to an invited one and every consumer -
            # setup_core.parse_invite, the wizards' auto-join, the vault backup - handles
            # both without a second code path.
            if network_key:
                f.write(f"\n# DLNE_NETWORK_KEY={network_key}\n")

        if sys.platform != "win32":
            os.chmod(conf_path, 0o600)

        return conf_path

    def setup_coordinator_server(self, listen_port: int = 51820,
                                 rotate_identity: bool = False) -> str:
        """
        Ensures this Coordinator has a WireGuard interface, and reports its public key.

        AN EXISTING INTERFACE IS KEPT, AND THAT IS THE WHOLE POINT
            This used to mint a fresh keypair on every call and rewrite the file wholesale.
            Re-running setup on a live Coordinator therefore rotated its tunnel identity
            behind its own back and dropped every registered ``[Peer]``, while the running
            interface — which nobody restarts as part of setup — carried on with the old
            key. From then on the two disagreed, and the disagreement was invisible:

              * ``coordinator_public_key`` reads the *file*, so ``/worker/enroll`` handed
                every joining node the new key;
              * the *interface* answered with the old one;
              * so each worker built a tunnel pinned to a key the server does not have,
                and no handshake could ever complete. A correct-looking config, a peer that
                never connects, and nothing in any log that says why.

            It also silently invalidated every invite already in circulation, and reset the
            address allocator underneath peers that still held addresses.

        Args:
            listen_port: UDP port the Coordinator's WireGuard interface listens on. Only
                consulted when an interface is actually created.
            rotate_identity: Deliberately replace an existing interface with a new keypair.
                This invalidates every invite and every enrolled peer, so it is never the
                default — it exists for an operator who means it, after a key compromise.

        Returns:
            The Coordinator's public key, which every worker invite must reference.

        Raises:
            WireGuardUnavailableError: if a real keypair could not be generated, or if an
                interface exists whose public key cannot be derived. Nothing is written to
                disk in either case: no half-valid config is left behind, and — more
                importantly — an identity that is merely unreadable right now is never
                mistaken for one that is absent and replaced.
        """
        existing_private = self.coordinator_private_key()
        if existing_private and not rotate_identity:
            existing_public = self._run_wg_cmd("pubkey", stdin_text=existing_private + "\n")
            if not existing_public:
                # The tools are missing or broken. Generating a replacement here would
                # rotate the identity of a working Coordinator because of a transient
                # failure to read it, which is the exact defect this branch exists to stop.
                raise WireGuardUnavailableError(
                    f"A Coordinator interface already exists at {self.coordinator_conf_path} "
                    "but its public key could not be derived. Refusing to replace it: doing "
                    "so would invalidate every invite and disconnect every enrolled peer. "
                    + _INSTALL_HINT
                )
            log_system(
                f"Reusing the existing Coordinator WireGuard identity at "
                f"{self.coordinator_conf_path}. Its peers and every invite already issued "
                "remain valid."
            )
            return existing_public

        privkey, pubkey = self.generate_keypair()
        if not privkey or not pubkey:
            raise WireGuardUnavailableError(
                "Cannot create the Coordinator WireGuard interface: key generation failed. "
                + _INSTALL_HINT
            )

        conf_path = self.coordinator_conf_path
        with open(conf_path, "w", encoding="utf-8") as f:
            f.write("[Interface]\n")
            f.write(f"PrivateKey = {privkey}\n")
            f.write(f"ListenPort = {listen_port}\n")
            f.write(f"Address = {WG_SUBNET_PREFIX}.{WG_COORDINATOR_OCTET}/24\n")
            f.write(f"MTU = {tunnel_mtu()}\n")

        # The file holds a private key. POSIX gets 0600; Windows inherits the directory
        # ACL, which os.chmod cannot meaningfully tighten.
        if sys.platform != "win32":
            os.chmod(conf_path, 0o600)

        # Best effort: a failure here only costs address density (the allocator would keep
        # counting up from a stale value), never correctness, so it must not abort setup.
        if not set_config_values({WG_NEXT_PEER_OCTET_KEY: str(WG_FIRST_PEER_OCTET)}):
            log_warning(
                f"Could not reset {WG_NEXT_PEER_OCTET_KEY} in config.txt. New invites will "
                "still receive unique addresses, just not starting from "
                f"{WG_SUBNET_PREFIX}.{WG_FIRST_PEER_OCTET}."
            )

        log_system(f"WireGuard Server generated. Load '{conf_path}' into your WireGuard client.")
        return pubkey

    def generate_worker_invite(
        self,
        coordinator_external_ip: str,
        coordinator_pubkey: str,
        port: int = 51820,
        network_key: str = "",
        filename: str = "worker_invite.conf",
        peer_name: str = "worker",
    ) -> str:
        """
        Generates an invite ``.conf`` that one remote node imports to join the tunnel.

        Every call produces a **distinct** identity: a freshly generated keypair and the
        next free address in :data:`WG_SUBNET_CIDR`, with the new peer's public key
        registered on the Coordinator's own interface. Issuing two nodes the same key and
        the same address — which is what copying an invite file does — means only one of
        them can ever be connected (audit item 3.4).

        Args:
            coordinator_external_ip: Public IP/hostname workers dial to reach the hub.
            coordinator_pubkey: Public key returned by :meth:`setup_coordinator_server`.
            port: Coordinator's WireGuard listen port.
            network_key: Shared cluster secret. When provided it is appended as a
                ``# DLNE_NETWORK_KEY=`` comment line — a comment so the file remains a
                valid WireGuard config that ``wg-quick`` accepts untouched, while
                ``setup_cli``'s auto-join path can still read it out and persist it as
                ``NETWORK_KEY`` in ``config/config.txt``. Without it the worker cannot
                sign its heartbeats and the Coordinator rejects it with HTTP 401.
            filename: Name of the invite inside ``wg_configs/``. Callers that issue more
                than one invite (worker plus redundancy node) pass a distinct name so the
                second does not overwrite the first.
            peer_name: Human label recorded as a comment beside the peer's entry in
                ``coordinator_wg0.conf``, so an operator reading that file later can tell
                which node an address belongs to.

        Returns:
            Absolute path of the generated invite.

        Raises:
            WireGuardUnavailableError: if a real keypair could not be generated, if the
                Coordinator's public key is missing — an invite pointing at a peer with no
                valid public key can never complete a handshake — or if the tunnel subnet
                has no free address left.
        """
        if not coordinator_pubkey:
            raise WireGuardUnavailableError(
                "Cannot create a worker invite: the Coordinator's WireGuard public key is missing."
            )

        privkey, pubkey = self.generate_keypair()
        if not privkey or not pubkey:
            raise WireGuardUnavailableError(
                "Cannot create a worker invite: key generation failed. " + _INSTALL_HINT
            )

        # Allocated only once a real keypair exists, so a machine without the WireGuard
        # tools does not burn an address on every failed attempt.
        octet = self._allocate_peer_octet()
        peer_address = f"{WG_SUBNET_PREFIX}.{octet}"

        # Basename only: a caller-supplied name must never be able to escape wg_configs/.
        conf_path = os.path.join(self.config_dir, os.path.basename(filename))
        with open(conf_path, "w", encoding="utf-8") as f:
            f.write("[Interface]\n")
            f.write(f"PrivateKey = {privkey}\n")
            # This node's own address inside the /24 the Coordinator serves at .1. The /24
            # mask is what installs the route to the rest of the tunnel on this node.
            f.write(f"Address = {peer_address}/24\n")
            f.write(f"MTU = {tunnel_mtu()}\n\n")

            f.write("[Peer]\n")
            f.write(f"PublicKey = {coordinator_pubkey}\n")
            f.write(f"Endpoint = {coordinator_external_ip}:{port}\n")
            f.write(f"AllowedIPs = {WG_SUBNET_CIDR}\n")
            f.write("PersistentKeepalive = 25\n")

            if network_key:
                f.write(f"\n# DLNE_NETWORK_KEY={network_key}\n")

        if sys.platform != "win32":
            os.chmod(conf_path, 0o600)

        # The Coordinator drops handshakes from public keys it has never been told about,
        # so this is not bookkeeping — it is the other half of making the invite work.
        self._register_peer(pubkey, octet, peer_name)

        log_system(f"Worker Invite generated at '{conf_path}' (tunnel address {peer_address}).")
        return conf_path
