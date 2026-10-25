"""
The coordinator's side of releases: GET /v1/release (public) and the release summary the
operator sees in /v1/admin/stats.
"""

import json
import os

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import ASGITransport, AsyncClient

from engine import api_keys, coordinator, release

REMOTE = ("203.0.113.88", 40000)
LOOPBACK = ("127.0.0.1", 40000)
KEY = "dlne_ak_release_endpoint_tests"


def _pem():
    return Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


@pytest.fixture
def served(tmp_path, monkeypatch):
    """A base dir with a signed release in it, signed by a throwaway key that the coordinator trusts."""
    pem = _pem()
    monkeypatch.setattr(release, "RELEASE_PUBKEY", release.public_key_hex(pem))
    monkeypatch.setattr(coordinator, "get_base_dir", lambda: str(tmp_path))

    archive = tmp_path / "dlne-source.zip"
    repo = tmp_path / "repo"
    (repo / "engine").mkdir(parents=True)
    (repo / "engine" / "version.py").write_text(f'VERSION = "{coordinator.VERSION}"\n')
    (repo / "dlne_server.py").write_text("x = 1\n")
    (repo / "requirements.txt").write_text("fastapi\n")
    release.build_source_archive(str(repo), str(archive))
    manifest = release.sign_manifest(
        release.make_manifest(coordinator.VERSION, str(archive), "abc1234", "2026-10-25T12:00:00Z", "n"), pem)
    (tmp_path / "release.json").write_text(json.dumps(manifest))
    return manifest


def _client(ip=REMOTE, key=None):
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return AsyncClient(transport=ASGITransport(app=coordinator.app, client=ip), base_url="http://test", headers=headers)


@pytest.mark.anyio
async def test_release_is_public_and_returns_exactly_the_published_manifest(served):
    async with _client() as c:
        resp = await c.get("/v1/release")
    assert resp.status_code == 200
    assert resp.json() == served
    ok, _ = release.verify_manifest(resp.json())
    assert ok


@pytest.mark.anyio
async def test_release_is_404_when_nothing_has_been_published(tmp_path, monkeypatch):
    monkeypatch.setattr(coordinator, "get_base_dir", lambda: str(tmp_path))
    async with _client() as c:
        resp = await c.get("/v1/release")
    assert resp.status_code == 404
    assert "no signed release manifest" in resp.json()["detail"]


@pytest.mark.anyio
async def test_a_corrupt_manifest_file_is_a_404_not_a_500(tmp_path, monkeypatch):
    monkeypatch.setattr(coordinator, "get_base_dir", lambda: str(tmp_path))
    (tmp_path / "release.json").write_text("{not json")
    async with _client() as c:
        assert (await c.get("/v1/release")).status_code == 404


@pytest.fixture
def operator(monkeypatch):
    real = coordinator.get_worker_limits
    monkeypatch.setattr(coordinator, "get_worker_limits",
                        lambda: {**real(), "admin_key_hash": api_keys.hash_admin_key(KEY)})


@pytest.mark.anyio
async def test_stats_reports_a_valid_published_release(served, operator):
    async with _client(LOOPBACK, KEY) as c:
        data = (await c.get("/v1/admin/stats")).json()["release"]
    assert data["published"] is True
    assert data["version"] == coordinator.VERSION == served["version"]
    assert data["sha256"] == served["sha256"]
    assert data["signature_valid"] is True and data["matches_running_code"] is True


@pytest.mark.anyio
async def test_stats_flags_a_release_that_does_not_verify(served, operator, tmp_path):
    tampered = dict(served, sha256="0" * 64)
    (tmp_path / "release.json").write_text(json.dumps(tampered))
    async with _client(LOOPBACK, KEY) as c:
        data = (await c.get("/v1/admin/stats")).json()["release"]
    assert data["published"] is True and data["signature_valid"] is False and data["detail"]


@pytest.mark.anyio
async def test_stats_flags_a_release_for_a_different_version_than_the_running_code(served, operator, tmp_path, monkeypatch):
    monkeypatch.setattr(coordinator, "VERSION", "99.0.0")
    async with _client(LOOPBACK, KEY) as c:
        data = (await c.get("/v1/admin/stats")).json()["release"]
    assert data["matches_running_code"] is False


@pytest.mark.anyio
async def test_stats_says_so_when_nothing_is_published(tmp_path, monkeypatch, operator):
    monkeypatch.setattr(coordinator, "get_base_dir", lambda: str(tmp_path))
    async with _client(LOOPBACK, KEY) as c:
        assert (await c.get("/v1/admin/stats")).json()["release"] == {"published": False}
