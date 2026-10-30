"""
How the coordinator reaches each volunteer's GPU, and how far away it is along that way (2.4.9).

THE PATHS, IN THE ORDER THEY ARE TRIED
    Each GPU is measured along the path the engine will really use, the same order as
    engine/reverse_tunnel.resolve_endpoint:

      lan     a machine in this coordinator's own household, at a private address it reported
              and this coordinator checked (see "Safety");
      direct  the address its heartbeats come from, when a TCP connection reaches it;
      tunnel  its own outbound reverse tunnel, timed with PING/PONG frames: a machine behind
              NAT, anywhere on the internet.

    A TCP connect is one round trip (SYN out, SYN-ACK back) and so is a ping through the
    tunnel; the best of three samples is the figure (engine/benchmark.measure_rtt_ms explains
    why the minimum). engine/auto_swap.py decides what the figure means.

WHY A LAN PATH
    Every install joins through the public address. A machine in the same house as the
    coordinator therefore heartbeats from the household's public IP (the router hairpins the
    connection), and dialling that address back fails: nothing is forwarded to the worker. Until
    this module, such a GPU read as unreachable, so far, even on a 2.5 Gbps LAN, and the pool
    never grew.

    Now the heartbeat answer to a worker whose connections come from this household's public
    address carries ``"send_lan": true``, and that worker lists its private IPv4 addresses
    (``lan_ipv4``) in its next signed hardware profile. No other worker is asked, so no other
    volunteer ever tells this coordinator about its private network.

SAFETY
    The coordinator dials an address a worker named only when all of these hold:
      * the worker's connections come from this household's public address, so only someone on
        this network can name one;
      * the address is a private IPv4 address inside the subnet of one of this machine's own
        interfaces: never loopback, never this machine itself, never a VPN or other
        point-to-point interface's subnet (WireGuard included);
      * a TCP connection to it, on the worker's registered RPC port, succeeds.

LIMITATION
    Workers are told apart by their registered ``ip:port``, here as in the tunnel manager. Two
    machines in one household that both serve RPC on the default port share that key; the
    second one to report wins.
"""

import ipaddress
import socket
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urlparse

# The household's public address is looked up again this often (a home IP can change; the
# DuckDNS timer on the coordinator keeps the name pointing at it).
HOUSEHOLD_REFRESH_SECONDS = 300.0
# A worker lists at most this many addresses; any more are ignored, not an error.
MAX_LAN_ADDRESSES = 8
# Connects and pings per measurement, and how long one may take.
PROBE_SAMPLES = 3
CONNECT_TIMEOUT_SECONDS = 1.0
TUNNEL_PING_TIMEOUT_SECONDS = 2.0

# Older names that still point at the coordinator, checked alongside PUBLIC_URL's.
EXTRA_PUBLIC_HOSTNAMES = ("dlne.duckdns.org",)


def _public_hostnames() -> List[str]:
    from engine.landing import PUBLIC_URL
    names = [urlparse(PUBLIC_URL).hostname or "", *EXTRA_PUBLIC_HOSTNAMES]
    return [name for name in names if name]


def _resolve_public_ips() -> Set[str]:
    """The public IPv4 addresses the coordinator's own names resolve to (blocking: DNS)."""
    found: Set[str] = set()
    for name in _public_hostnames():
        try:
            for info in socket.getaddrinfo(name, None, socket.AF_INET, socket.SOCK_STREAM):
                address = ipaddress.ip_address(info[4][0])
                if address.is_global:
                    found.add(str(address))
        except (OSError, ValueError):
            continue
    return found


def _is_lan_address(address: ipaddress.IPv4Address) -> bool:
    """Private and on a LAN: not loopback or link-local, and not the WireGuard subnet (a tunnel, not a LAN)."""
    from engine.benchmark import classify_endpoint
    return (address.is_private and not address.is_loopback and not address.is_link_local
            and classify_endpoint(str(address)) == "lan")


# Interface names that are tunnels whatever their addresses look like (psutil before 5.9.3 has no flags).
_TUNNEL_NAME_PREFIXES = ("wg", "tun", "tap", "tailscale", "zt", "utun", "ppp")


def _is_tunnel(name: str, stats: Any) -> bool:
    """
    A VPN or other point-to-point link: the far end of it is not on this LAN even when its subnet is private.
    TheWall's own WireGuard (10.13.13.0/24) is not DLNE's tunnel subnet, so the subnet alone cannot tell.
    """
    flags = str(getattr(stats, "flags", "") or "").lower().split(",")
    return "pointopoint" in flags or name.lower().startswith(_TUNNEL_NAME_PREFIXES)


def _interface_networks() -> Tuple[List[ipaddress.IPv4Network], Set[str]]:
    """This machine's private IPv4 subnets, and its own addresses on them, tunnels left out."""
    networks: List[ipaddress.IPv4Network] = []
    own: Set[str] = set()
    try:
        import psutil
        interfaces = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
    except Exception:  # noqa: BLE001 - no psutil, no LAN path
        return networks, own
    for name, addresses in interfaces.items():
        if _is_tunnel(name, stats.get(name)):
            continue
        for entry in addresses:
            if entry.family != socket.AF_INET or not entry.netmask:
                continue
            try:
                interface = ipaddress.IPv4Interface(f"{entry.address}/{entry.netmask}")
            except ValueError:
                continue
            if _is_lan_address(interface.ip):
                networks.append(interface.network)
                own.add(str(interface.ip))
    return networks, own


class Household:
    """
    This coordinator's public addresses and its own LAN subnets. Lookups never block: a stale
    answer starts a refresh on a background thread and the cached one is used meanwhile (empty
    until the first lookup finishes, so a coordinator that has just started asks nobody for
    a minute).
    """

    def __init__(self, *, resolve: Callable[[], Set[str]] = _resolve_public_ips,
                 interfaces: Callable[[], Tuple[List[ipaddress.IPv4Network], Set[str]]] = _interface_networks,
                 clock: Callable[[], float] = time.monotonic,
                 refresh_seconds: float = HOUSEHOLD_REFRESH_SECONDS) -> None:
        self._resolve = resolve
        self._interfaces = interfaces
        self._clock = clock
        self._refresh_seconds = refresh_seconds
        self._lock = threading.Lock()
        self._public: Set[str] = set()
        self._networks: List[ipaddress.IPv4Network] = []
        self._own: Set[str] = set()
        self._checked = float("-inf")
        self._refreshing = False

    def refresh_now(self) -> None:
        """Look everything up again (blocking)."""
        try:
            public = set(self._resolve())
        except Exception:  # noqa: BLE001 - a failed lookup keeps what we had
            public = None
        try:
            networks, own = self._interfaces()
        except Exception:  # noqa: BLE001
            networks, own = None, None
        with self._lock:
            if public is not None:
                self._public = public
            if networks is not None:
                self._networks, self._own = list(networks), set(own or ())
            self._checked = self._clock()
            self._refreshing = False

    def _maybe_refresh(self) -> None:
        with self._lock:
            if self._refreshing or self._clock() - self._checked < self._refresh_seconds:
                return
            self._refreshing = True
        threading.Thread(target=self.refresh_now, name="dlne-household", daemon=True).start()

    def is_household(self, ip: str) -> bool:
        """Whether connections from ``ip`` come from this household (its public address)."""
        self._maybe_refresh()
        with self._lock:
            return bool(ip) and ip in self._public

    def lan_candidates(self, observed_ip: str, reported: Any) -> List[str]:
        """
        The addresses from a worker's ``lan_ipv4`` this coordinator may dial: none unless the
        worker is in this household, and only private addresses on one of this machine's own
        subnets (see the module docstring).
        """
        if not self.is_household(observed_ip) or not isinstance(reported, list):
            return []
        with self._lock:
            networks, own = list(self._networks), set(self._own)
        allowed: List[str] = []
        for item in reported[:MAX_LAN_ADDRESSES]:
            try:
                address = ipaddress.IPv4Address(item)
            except (ValueError, TypeError):
                continue
            text = str(address)
            if text in own or text in allowed or not _is_lan_address(address):
                continue
            if any(address in network for network in networks):
                allowed.append(text)
        return allowed

    def own_address_near(self, lan_ips: List[str]) -> Optional[str]:
        """This machine's own address on the subnet of the first of ``lan_ips`` (from lan_candidates) it shares one with."""
        with self._lock:
            networks, own = list(self._networks), sorted(self._own)
        for item in lan_ips:
            address = ipaddress.IPv4Address(item)
            for network in networks:
                if address in network:
                    mine = [a for a in own if ipaddress.IPv4Address(a) in network]
                    if mine:
                        return mine[0]
        return None


class LanRoutes:
    """
    The LAN addresses household workers offered, and the one that answered: keyed by the
    worker's registered ``(ip, port)``, the key engine/reverse_tunnel.py uses too.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._offers: Dict[Tuple[str, int], List[str]] = {}
        self._routes: Dict[Tuple[str, int], str] = {}

    def offer(self, ip: str, port: int, candidates: List[str]) -> None:
        """What a heartbeat offered, already filtered by Household.lan_candidates. Empty withdraws the route."""
        key = (ip, int(port))
        with self._lock:
            if candidates:
                self._offers[key] = list(candidates)
                if self._routes.get(key) not in candidates:
                    self._routes.pop(key, None)
            else:
                self._offers.pop(key, None)
                self._routes.pop(key, None)

    def offers(self, ip: str, port: int) -> List[str]:
        """The offered addresses, the one that answered last first."""
        key = (ip, int(port))
        with self._lock:
            offered = list(self._offers.get(key, ()))
            route = self._routes.get(key)
        if route in offered:
            offered.remove(route)
            offered.insert(0, route)
        return offered

    def confirm(self, ip: str, port: int, lan_ip: str) -> None:
        key = (ip, int(port))
        with self._lock:
            if lan_ip in self._offers.get(key, ()):
                self._routes[key] = lan_ip

    def drop(self, ip: str, port: int) -> None:
        with self._lock:
            self._routes.pop((ip, int(port)), None)

    def get(self, ip: str, port: int) -> Optional[str]:
        """The verified LAN address for this worker, or None."""
        with self._lock:
            return self._routes.get((ip, int(port)))

    def keep_only(self, live: Iterable[Tuple[str, int]]) -> None:
        """Forget workers that are gone."""
        alive = {(ip, int(port)) for ip, port in live}
        with self._lock:
            for key in set(self._offers) - alive:
                del self._offers[key]
            for key in set(self._routes) - alive:
                del self._routes[key]


def _connect_rtt(host: str, port: int) -> Optional[float]:
    from engine.benchmark import measure_rtt_ms
    try:
        result = measure_rtt_ms(host, int(port), samples=PROBE_SAMPLES, timeout=CONNECT_TIMEOUT_SECONDS)
    except (ValueError, OSError):
        return None
    return result.get("rtt_min_ms") if result.get("ok") else None


def _tunnel_rtt(host: str, port: int) -> Optional[float]:
    from engine.reverse_tunnel import reverse_bridge
    return reverse_bridge.measure_rtt_ms(host, int(port), samples=PROBE_SAMPLES,
                                         timeout=TUNNEL_PING_TIMEOUT_SECONDS)


def measure(endpoint: str, *, routes: Optional[LanRoutes] = None,
            connect: Callable[[str, int], Optional[float]] = _connect_rtt,
            tunnel: Callable[[str, int], Optional[float]] = _tunnel_rtt) -> Optional[Tuple[float, str]]:
    """
    ``(rtt_ms, path)`` for the worker registered at ``endpoint`` ("ip:port"), path being "lan",
    "direct" or "tunnel"; None when no path answered. Blocking: run it on a thread.
    """
    routes = routes if routes is not None else lan_routes
    host, _, port_text = endpoint.rpartition(":")
    try:
        port = int(port_text)
    except ValueError:
        return None
    for lan_ip in routes.offers(host, port):
        rtt = connect(lan_ip, port)
        if rtt is not None:
            routes.confirm(host, port, lan_ip)
            return float(rtt), "lan"
    routes.drop(host, port)
    rtt = connect(host, port)
    if rtt is not None:
        return float(rtt), "direct"
    rtt = tunnel(host, port)
    if rtt is not None:
        return float(rtt), "tunnel"
    return None


def _connect_series(host: str, port: int, samples: int) -> Optional[List[Optional[float]]]:
    from engine.benchmark import measure_rtt_ms
    try:
        result = measure_rtt_ms(host, int(port), samples=samples, timeout=CONNECT_TIMEOUT_SECONDS)
    except (ValueError, OSError):
        return None
    return result.get("rtt_series_ms") if result.get("ok") else None


def _tunnel_series(host: str, port: int, samples: int) -> Optional[List[Optional[float]]]:
    from engine.reverse_tunnel import reverse_bridge
    return reverse_bridge.ping_series(host, int(port), samples, timeout=TUNNEL_PING_TIMEOUT_SECONDS)


def measure_series(endpoint: str, samples: int, *, routes: Optional[LanRoutes] = None,
                   connect: Callable[[str, int, int], Optional[List[Optional[float]]]] = _connect_series,
                   tunnel: Callable[[str, int, int], Optional[List[Optional[float]]]] = _tunnel_series,
                   ) -> Optional[Tuple[List[Optional[float]], str]]:
    """
    :func:`measure` with every sample kept, for the Wi-Fi test (engine/dock_policy.py, 2.6.4):
    ``(samples_ms, path)``, None for a sample that went unanswered; None when no path answered at
    all. The same paths in the same order, so the series times the link the engine would use.
    Blocking: run it on a thread.
    """
    routes = routes if routes is not None else lan_routes
    host, _, port_text = endpoint.rpartition(":")
    try:
        port = int(port_text)
    except ValueError:
        return None
    for lan_ip in routes.offers(host, port):
        series = connect(lan_ip, port, samples)
        if series:
            routes.confirm(host, port, lan_ip)
            return list(series), "lan"
    routes.drop(host, port)
    series = connect(host, port, samples)
    if series:
        return list(series), "direct"
    series = tunnel(host, port, samples)
    if series:
        return list(series), "tunnel"
    return None


household = Household()
lan_routes = LanRoutes()
