"""
engine/link_paths.py: which way the coordinator reaches a volunteer's GPU (its household LAN address,
its own address, or its reverse tunnel) and how far away it is along that way.

The case that made this module (2026-10-26): the owner's laptop sits on the same 2.5 Gbps LAN as the
coordinator, joins through the public address, so heartbeats from the household's public IP, and that
address cannot be dialled back. Its GPU read as far and the pool never grew.
"""

import ipaddress

import pytest

from engine import link_paths

HOME_PUBLIC = "198.51.100.200"
LAN = ipaddress.IPv4Network("192.168.40.0/24")


def household(public=(HOME_PUBLIC,), networks=(LAN,), own=("192.168.40.23",)):
    h = link_paths.Household(resolve=lambda: set(public), interfaces=lambda: (list(networks), set(own)),
                             refresh_seconds=1e9)
    h.refresh_now()
    return h


# --- Household: who may offer a LAN address, and which addresses count ----------------------------------

def test_only_connections_from_the_households_public_address_are_household():
    h = household()
    assert h.is_household(HOME_PUBLIC)
    assert not h.is_household("203.0.113.9") and not h.is_household("")


def test_a_household_worker_may_offer_only_addresses_on_the_coordinators_own_subnets():
    h = household()
    offered = ["172.17.0.1",        # docker on the laptop
               "192.168.122.1",     # libvirt
               "10.13.13.2",        # WireGuard: a tunnel, not a LAN
               "192.168.40.50",     # the real LAN address
               "192.168.40.23",     # the coordinator itself
               "127.0.0.1", "169.254.3.4", "8.8.8.8", "not an address", 42]
    assert h.lan_candidates(HOME_PUBLIC, offered) == ["192.168.40.50"]


def test_the_coordinator_names_its_own_address_on_the_workers_subnet():
    h = household(networks=(LAN, ipaddress.IPv4Network("10.0.5.0/24")), own=("10.0.5.1", "192.168.40.23"))
    assert h.own_address_near(["192.168.40.50"]) == "192.168.40.23"
    assert h.own_address_near(["10.0.5.9", "192.168.40.50"]) == "10.0.5.1"
    assert h.own_address_near([]) is None


def test_a_worker_from_anywhere_else_cannot_point_the_coordinator_at_the_lan():
    h = household()
    assert h.lan_candidates("203.0.113.9", ["192.168.40.50"]) == []
    assert h.lan_candidates(HOME_PUBLIC, "192.168.40.50") == []          # not a list


def test_only_the_first_few_addresses_are_read():
    h = household()
    many = [f"10.9.9.{i}" for i in range(20)] + ["192.168.40.50"]
    assert h.lan_candidates(HOME_PUBLIC, many) == []


def test_a_failed_lookup_keeps_what_was_known():
    calls = {"n": 0}

    def resolve():
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError("DNS is down")
        return {HOME_PUBLIC}

    h = link_paths.Household(resolve=resolve, interfaces=lambda: ([LAN], set()), refresh_seconds=1e9)
    h.refresh_now()
    h.refresh_now()
    assert h.is_household(HOME_PUBLIC)


def test_a_coordinator_that_has_not_looked_yet_asks_nobody():
    h = link_paths.Household(resolve=lambda: {HOME_PUBLIC}, interfaces=lambda: ([LAN], set()),
                             refresh_seconds=1e9)
    h._refreshing = True                      # the background lookup has not finished
    assert not h.is_household(HOME_PUBLIC)


def test_a_vpn_interface_is_not_a_lan(monkeypatch):
    """TheWall: eno1 192.168.40.23/24 is the LAN; wg0 10.13.13.1/24 is a WireGuard tunnel (not DLNE's subnet)."""
    import psutil
    from types import SimpleNamespace

    def entry(address, netmask="255.255.255.0"):
        return SimpleNamespace(family=link_paths.socket.AF_INET, address=address, netmask=netmask)

    monkeypatch.setattr(psutil, "net_if_addrs", lambda: {
        "lo": [entry("127.0.0.1", "255.0.0.0")], "eno1": [entry("192.168.40.23")], "wg0": [entry("10.13.13.1")],
        "tailscale0": [entry("100.101.102.103", "255.255.255.255")], "virbr1": [entry("192.168.150.1")]})
    monkeypatch.setattr(psutil, "net_if_stats", lambda: {
        "lo": SimpleNamespace(flags="up,loopback,running"), "eno1": SimpleNamespace(flags="up,broadcast,running,multicast"),
        "wg0": SimpleNamespace(flags="up,pointopoint,running,noarp"), "tailscale0": SimpleNamespace(flags=""),
        "virbr1": SimpleNamespace()})
    networks, own = link_paths._interface_networks()
    assert [str(n) for n in networks] == ["192.168.40.0/24", "192.168.150.0/24"]
    assert own == {"192.168.40.23", "192.168.150.1"}


def test_the_public_address_comes_from_the_coordinators_own_names(monkeypatch):
    seen = []

    def getaddrinfo(name, *args):
        seen.append(name)
        return [(None, None, None, "", ("73.215.216.195", 0)), (None, None, None, "", ("192.168.40.23", 0))]

    monkeypatch.setattr(link_paths.socket, "getaddrinfo", getaddrinfo)
    assert link_paths._resolve_public_ips() == {"73.215.216.195"}     # a private answer is not a public address
    assert "dlne.rossgilsonlabs.dev" in seen and "dlne.duckdns.org" in seen


# --- LanRoutes -------------------------------------------------------------------------------------------

def test_a_route_is_only_ever_one_of_the_offered_addresses():
    routes = link_paths.LanRoutes()
    routes.confirm(HOME_PUBLIC, 50052, "192.168.40.50")
    assert routes.get(HOME_PUBLIC, 50052) is None, "never offered"
    routes.offer(HOME_PUBLIC, 50052, ["192.168.40.50", "192.168.40.51"])
    routes.confirm(HOME_PUBLIC, 50052, "192.168.40.51")
    assert routes.get(HOME_PUBLIC, 50052) == "192.168.40.51"
    assert routes.offers(HOME_PUBLIC, 50052) == ["192.168.40.51", "192.168.40.50"]   # the one that answered first
    routes.offer(HOME_PUBLIC, 50052, ["192.168.40.50"])                     # the laptop moved
    assert routes.get(HOME_PUBLIC, 50052) is None
    routes.offer(HOME_PUBLIC, 50052, [])
    assert routes.offers(HOME_PUBLIC, 50052) == []


def test_routes_of_workers_that_left_are_forgotten():
    routes = link_paths.LanRoutes()
    routes.offer(HOME_PUBLIC, 50052, ["192.168.40.50"])
    routes.confirm(HOME_PUBLIC, 50052, "192.168.40.50")
    routes.keep_only([("203.0.113.9", 50052)])
    assert routes.get(HOME_PUBLIC, 50052) is None and routes.offers(HOME_PUBLIC, 50052) == []


# --- measure: the path the engine will use, timed ---------------------------------------------------------

def test_a_household_gpu_is_measured_on_the_lan():
    routes = link_paths.LanRoutes()
    routes.offer(HOME_PUBLIC, 50052, ["192.168.40.50"])
    dialled = []

    def connect(host, port):
        dialled.append(host)
        return 0.4 if host == "192.168.40.50" else None

    assert link_paths.measure(f"{HOME_PUBLIC}:50052", routes=routes, connect=connect,
                              tunnel=lambda h, p: 1.5) == (0.4, "lan")
    assert routes.get(HOME_PUBLIC, 50052) == "192.168.40.50"
    assert dialled == ["192.168.40.50"]


def test_a_lan_address_that_stops_answering_is_dropped_and_the_next_path_is_used():
    routes = link_paths.LanRoutes()
    routes.offer(HOME_PUBLIC, 50052, ["192.168.40.50"])
    routes.confirm(HOME_PUBLIC, 50052, "192.168.40.50")
    assert link_paths.measure(f"{HOME_PUBLIC}:50052", routes=routes, connect=lambda h, p: None,
                              tunnel=lambda h, p: 1.5) == (1.5, "tunnel")
    assert routes.get(HOME_PUBLIC, 50052) is None


def test_a_reachable_volunteer_is_measured_directly():
    assert link_paths.measure("203.0.113.9:50052", routes=link_paths.LanRoutes(),
                              connect=lambda h, p: 23.0, tunnel=lambda h, p: 99.0) == (23.0, "direct")


def test_a_volunteer_behind_nat_anywhere_is_measured_through_its_tunnel():
    """Before 2.4.9 such a GPU could never be measured, so it was far whatever its link."""
    assert link_paths.measure("203.0.113.9:50052", routes=link_paths.LanRoutes(),
                              connect=lambda h, p: None, tunnel=lambda h, p: 61.0) == (61.0, "tunnel")


@pytest.mark.parametrize("endpoint", ["203.0.113.9:50052", "203.0.113.9:port"])
def test_nothing_answering_means_no_measurement(endpoint):
    assert link_paths.measure(endpoint, routes=link_paths.LanRoutes(),
                              connect=lambda h, p: None, tunnel=lambda h, p: None) is None
