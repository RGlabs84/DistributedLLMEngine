"""
The worker's CPU pool wiring (engine/worker.py), the heartbeat's pool/app fields
(engine/authn.validate_hardware_profile), the APP_* config keys (engine/config.py) and the
verified-manifest fetch members read their tiers from (engine/downloader.py).

A worker with no usable GPU serves the CPU pool instead of running ggml-rpc-server, except the
coordinator host's own hybrid worker (the coordinator runs that host's member itself). It says
so inside the SIGNED hardware profile, and the coordinator's heartbeat answer names its tier.
"""

import io
import json
import os

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from engine import authn, downloader, hardware, release, worker
from engine import config as engine_config
from engine.app_member import APP_PORT, AppMember

KEY = "a-network-key-for-tests"
CPU_HW = {
    "gpu": {"has_gpu": False, "vram_total_gb": 0},
    "ram_gb": 12.0, "cpu_cores": 6, "cpu_threads": 12,
    "cpu_arch": "x86_64", "cpu_features": ["avx2", "fma", "f16c"],
    "mem_bandwidth_gbs": 48.0, "is_yielding": False,
}


class RecordingMember:
    def __init__(self, status=None):
        self.assigned = []
        self.stopped = False
        self._status = status or {"model": "tiny-a", "ready": True, "slots": 2}

    def status(self):
        return dict(self._status)

    def assign(self, tier_id):
        self.assigned.append(tier_id)

    def stop(self):
        self.stopped = True


@pytest.fixture
def node(monkeypatch, tmp_path):
    import engine.setup_core as setup_core
    monkeypatch.setattr(setup_core, "get_or_create_worker_id", lambda: "worker-under-test")
    monkeypatch.setattr(worker, "get_hardware_profile", lambda: dict(CPU_HW))
    monkeypatch.setattr(worker, "_write_token_to_disk", lambda token: None)
    monkeypatch.setattr(worker, "HEARTBEAT_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(worker, "get_worker_limits", lambda: {"network_key": KEY})
    monkeypatch.setattr(worker, "get_base_dir", lambda: str(tmp_path))
    n = worker.WorkerNode("http://coordinator:11435")
    n.is_running = True
    yield n
    n._stopped = True     # keep the atexit hook quiet


# ======================================================================================
# Who serves the CPU pool
# ======================================================================================

@pytest.mark.parametrize("role, hw, serves", [
    ("worker", {"gpu": {"has_gpu": True, "vram_total_gb": 8.0}}, False),
    ("worker", {"gpu": {"has_gpu": False, "vram_total_gb": 0}}, True),
    ("worker", {"gpu": None}, True),
    ("worker", {}, True),
    ("worker", "not a profile", True),
    ("redundancy", {"gpu": None}, True),
    ("redundancy", {"gpu": {"has_gpu": True}}, False),
    ("hybrid", {"gpu": None}, False),
    ("hybrid", {"gpu": {"has_gpu": True}}, False),
])
def test_serves_cpu_pool(node, role, hw, serves):
    node.role = role
    node.hardware_profile = hw
    assert node._serves_cpu_pool() is serves


@pytest.mark.parametrize("gpu, expected", [(None, ["app"]), ({"has_gpu": True, "vram_total_gb": 8.0}, ["rpc"])])
def test_start_takes_the_matching_path(node, monkeypatch, gpu, expected):
    chosen = []
    monkeypatch.setattr(node, "start_app_member", lambda: chosen.append("app") or False)
    monkeypatch.setattr(node, "start_rpc_server", lambda: chosen.append("rpc") or False)
    node.hardware_profile = {**CPU_HW, "gpu": gpu}
    with pytest.raises(SystemExit):
        node.start()          # both stubs fail, so the node shuts down (on the main thread: exits)
    assert chosen == expected


def test_start_app_member_wires_the_member(node, monkeypatch, tmp_path):
    engines, fetched = [], []
    monkeypatch.setattr(worker, "download_and_extract_engine",
                        lambda role, coordinator_url=None: engines.append((role, coordinator_url)) or "x")
    manifest = {"app_tiers": "tiny-a", **{release.app_field("tiny-a", k): v for k, v in
                                          {"name": "Tiny", "file": "t.gguf", "url": "u", "sha256": "s",
                                           "size": 5, "min_ram_gb": 8}.items()}}
    monkeypatch.setattr(downloader, "fetch_verified_manifest", lambda url: fetched.append(url) or manifest)
    monkeypatch.setattr(hardware, "get_cpu_cores", lambda: 6)
    node.coordinator_url = "http://coordinator:11435/"
    assert node.start_app_member() is True
    assert engines == [("worker", "http://coordinator:11435/")]
    assert node.rpc_port == APP_PORT, "the heartbeat (and so the tunnel) names the pool server's port"
    member = node.app_member
    assert isinstance(member, AppMember)
    assert (member.base_dir, member.threads, member.port) == (str(tmp_path), 3, APP_PORT), \
        "half the physical cores: the owner keeps the rest of their machine"
    assert [t["id"] for t in member.tiers_loader()] == ["tiny-a"]
    assert member.tiers_loader()[0]["min_ram_gb"] == 8
    assert fetched == ["http://coordinator:11435"], "fetched once, then cached"


def test_stopping_the_node_stops_the_member(node):
    node.app_member = RecordingMember()
    node.stop()
    assert node.app_member.stopped


# ======================================================================================
# The heartbeat
# ======================================================================================

def _verify(payload, hardware=None):
    return authn.verify_heartbeat(KEY, payload["worker_id"], payload["port"], payload["token"],
                                  payload["timestamp"], payload["nonce"], payload["signature"], payload["version"],
                                  payload["hardware"] if hardware is None else hardware,
                                  role=payload["role"], staged_version=payload["staged_version"])


def test_the_heartbeat_says_pool_member_inside_the_signature(node):
    node.app_member = RecordingMember({"model": "tiny-a", "ready": True, "slots": 2})
    node.rpc_port = APP_PORT
    node.access_token = "tok"
    payload = node._build_heartbeat_payload()
    assert payload["port"] == APP_PORT
    assert payload["hardware"]["pool"] == "app"
    assert payload["hardware"]["app"] == {"model": "tiny-a", "ready": True, "slots": 2}
    assert authn.validate_hardware_profile(payload["hardware"]) == (True, "ok")
    assert _verify(payload)[0] is True
    # Rewriting the claim in flight breaks the signature.
    forged_ready = {**payload["hardware"], "app": {**payload["hardware"]["app"], "ready": False}}
    assert _verify(payload, forged_ready)[0] is False
    not_a_member = {k: v for k, v in payload["hardware"].items() if k not in ("pool", "app")}
    assert _verify(payload, not_a_member)[0] is False
    assert "pool" not in node.hardware_profile, "the probed profile itself is not modified"


def test_a_real_member_status_passes_the_coordinators_checks(node, tmp_path):
    node.app_member = AppMember(str(tmp_path), lambda: [], threads=2)
    payload = node._build_heartbeat_payload()
    assert payload["hardware"]["app"] == {"model": None, "ready": False, "slots": 2, "threads": 2}
    assert authn.validate_hardware_profile(payload["hardware"]) == (True, "ok")
    assert _verify(payload)[0] is True


def test_an_rpc_node_heartbeat_has_no_pool_fields(node):
    payload = node._build_heartbeat_payload()
    assert "pool" not in payload["hardware"] and "app" not in payload["hardware"]
    assert _verify(payload)[0] is True


def _run(node, monkeypatch, replies):
    """Run the real heartbeat loop for len(replies) beats, answering each with replies[i]."""
    sent = []

    def fake_post(url, json=None, timeout=None):
        sent.append(json)
        reply = replies[len(sent) - 1]
        if len(sent) >= len(replies):
            node.request_shutdown(0, "test finished")
        request = httpx.Request("POST", url)
        if isinstance(reply, bytes):
            return httpx.Response(200, content=reply, request=request)
        return httpx.Response(200, json=reply, request=request)

    monkeypatch.setattr(worker.httpx, "post", fake_post)
    node.heartbeat_loop()
    return sent


def test_the_coordinators_answer_reaches_assign(node, monkeypatch):
    node.app_member = RecordingMember()
    sent = _run(node, monkeypatch, [{"status": "ok", "app_model": "tiny-a"},
                                    {"status": "ok", "app_model": "tiny-b"},
                                    {"status": "ok"}])
    assert node.app_member.assigned == ["tiny-a", "tiny-b", None]
    assert all(beat["hardware"]["pool"] == "app" for beat in sent)


def test_an_answer_that_is_not_an_object_assigns_nothing(node, monkeypatch):
    node.app_member = RecordingMember()
    _run(node, monkeypatch, [[1, 2], "ok", {"status": "ok", "app_model": "tiny-a"}])
    assert node.app_member.assigned == ["tiny-a"]


def test_an_rpc_node_ignores_app_model(node, monkeypatch):
    _run(node, monkeypatch, [{"status": "ok", "app_model": "tiny-a"}])
    assert node.app_member is None


# ======================================================================================
# authn: the pool/app fields of a hardware profile
# ======================================================================================

@pytest.mark.parametrize("extra, ok", [
    ({"pool": "app"}, True),
    ({"pool": None}, True),
    ({"pool": "rpc"}, False),
    ({"pool": ""}, False),
    ({"pool": 1}, False),
    ({"app": {}}, True),
    ({"app": {"model": "qwen3-4b", "ready": True, "slots": 2}}, True),
    ({"app": {"model": None, "ready": False, "slots": 1}}, True),
    ({"app": {"model": "m" * 64, "slots": 64}}, True),
    ({"app": {"model": "m" * 65}}, False),
    ({"app": {"model": ""}}, False),
    ({"app": {"model": 7}}, False),
    ({"app": {"ready": "true"}}, False),
    ({"app": {"ready": 1}}, False),
    ({"app": {"ready": None}}, False),
    ({"app": {"slots": 0}}, False),
    ({"app": {"slots": 65}}, False),
    ({"app": {"slots": True}}, False),
    ({"app": {"slots": 2.0}}, False),
    ({"app": {"slots": "2"}}, False),
    ({"app": {"model": "m", "extra": 1}}, False),
    ({"app": "qwen3-4b"}, False),
    ({"app": ["model"]}, False),
    ({"app": None}, True),
])
def test_validate_hardware_profile_pool_fields(extra, ok):
    valid, reason = authn.validate_hardware_profile({**CPU_HW, **extra})
    assert valid is ok, reason


# ======================================================================================
# config: APP_* keys
# ======================================================================================

def _limits(monkeypatch, raw):
    monkeypatch.setattr(engine_config, "get_config", lambda force_reload=False: raw)
    return engine_config.get_worker_limits()


def test_app_defaults(monkeypatch):
    limits = _limits(monkeypatch, {})
    assert {k: limits[k] for k in limits if k.startswith("app_")} == {
        "app_tier_min_members": 2, "app_rate_per_minute": 6, "app_local_enabled": False,
        "app_local_cpu_mask": "", "app_local_threads": 4, "app_local_ram_gb": 8.0, "app_local_port": 8099,
        "app_member_threads": 0,
    }


@pytest.mark.parametrize("cores, threads", [(4, 2), (6, 3), (8, 4), (12, 6), (24, 8), (64, 8), (1, 2)])
def test_a_volunteer_member_uses_half_its_cores_two_to_eight(cores, threads):
    from engine.app_member import default_threads
    assert default_threads(cores) == threads


@pytest.mark.parametrize("key, raw, field, expected", [
    ("APP_TIER_MIN_MEMBERS", "3", "app_tier_min_members", 3),
    ("APP_TIER_MIN_MEMBERS", "0", "app_tier_min_members", 2),
    ("APP_TIER_MIN_MEMBERS", "two", "app_tier_min_members", 2),
    ("APP_RATE_PER_MINUTE", "30", "app_rate_per_minute", 30),
    ("APP_RATE_PER_MINUTE", "601", "app_rate_per_minute", 6),
    ("APP_LOCAL_THREADS", "8", "app_local_threads", 8),
    ("APP_LOCAL_THREADS", "1.5", "app_local_threads", 4),
    ("APP_LOCAL_THREADS", "257", "app_local_threads", 4),
    ("APP_LOCAL_RAM_GB", "12.5", "app_local_ram_gb", 12.5),
    ("APP_LOCAL_RAM_GB", "0.5", "app_local_ram_gb", 8.0),
    ("APP_LOCAL_RAM_GB", "nan", "app_local_ram_gb", 8.0),
    ("APP_LOCAL_RAM_GB", "inf", "app_local_ram_gb", 8.0),
    ("APP_LOCAL_PORT", "9000", "app_local_port", 9000),
    ("APP_LOCAL_PORT", "80", "app_local_port", 8099),
    ("APP_LOCAL_PORT", "70000", "app_local_port", 8099),
    ("APP_LOCAL_ENABLED", "True", "app_local_enabled", True),
    ("APP_LOCAL_ENABLED", "yes", "app_local_enabled", False),
    ("APP_LOCAL_CPU_MASK", " 0xFF00 ", "app_local_cpu_mask", "0xff00"),
    ("APP_LOCAL_CPU_MASK", "ff00", "app_local_cpu_mask", ""),
    ("APP_LOCAL_CPU_MASK", "0x", "app_local_cpu_mask", ""),
    ("APP_LOCAL_CPU_MASK", "0xfg", "app_local_cpu_mask", ""),
    ("APP_LOCAL_CPU_MASK", "0x1;rm -rf /", "app_local_cpu_mask", ""),
])
def test_app_config_keys(monkeypatch, key, raw, field, expected):
    assert _limits(monkeypatch, {key: raw})[field] == expected


# ======================================================================================
# downloader.fetch_verified_manifest: where a member's tier list comes from
# ======================================================================================

def _pem(key):
    return key.private_bytes(encoding=serialization.Encoding.PEM, format=serialization.PrivateFormat.PKCS8,
                             encryption_algorithm=serialization.NoEncryption())


def test_members_only_read_tiers_from_a_verified_release(monkeypatch, tmp_path):
    pem = _pem(Ed25519PrivateKey.generate())
    monkeypatch.setattr(release, "RELEASE_PUBKEY", release.public_key_hex(pem))
    archive = tmp_path / "dlne-source.zip"
    archive.write_bytes(b"zip bytes")
    models_json = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(release.__file__))),
                               "scripts", "app_models.json")
    with open(models_json, "r", encoding="utf-8") as handle:
        tiers = json.load(handle)["tiers"]
    signed = release.sign_manifest(release.make_manifest("2.3.0", str(archive), "abc1234", "2026-10-25T00:00:00Z",
                                                         app_models=tiers), pem)
    asked = []

    def urlopen(url, timeout=None):
        asked.append(url)
        return io.BytesIO(json.dumps(served).encode())

    served = signed
    assert downloader.fetch_verified_manifest("http://coordinator:11435", urlopen) == signed
    assert asked == ["http://coordinator:11435/v1/release"]
    assert [t["id"] for t in release.app_tiers(signed)] == [t["id"] for t in tiers]

    served = dict(signed, **{release.app_field(tiers[0]["id"], "url"): tiers[0]["url"].replace("bartowski", "mallory")})
    with pytest.raises(release.ReleaseError):
        downloader.fetch_verified_manifest("http://coordinator:11435", urlopen)
