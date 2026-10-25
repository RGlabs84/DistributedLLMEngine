"""
The coordinator's only web page: a signpost to the extension plus a live view of the swarm.

DLNE is a VS Code extension, not a website. These tests pin that the old browser chat room is
gone, the replacement is public, self-contained and well-formed, and nothing in it can be
mistaken for (or reached as) the old WebUI.
"""

from html.parser import HTMLParser

import pytest
from httpx import ASGITransport, AsyncClient

from engine import coordinator, landing

REMOTE = ("203.0.113.77", 40000)   # TEST-NET-3: a stranger, not the coordinator host


def _client(ip=REMOTE):
    return AsyncClient(transport=ASGITransport(app=coordinator.app, client=ip), base_url="http://test")


class _Collector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self.scripts, self.styles, self.tags = [], [], [], []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        attrs = dict(attrs)
        if tag == "a":
            self.links.append(attrs)
        if tag == "script":
            self.scripts.append(attrs)
        if tag == "link":
            self.styles.append(attrs)


def _parsed():
    collector = _Collector()
    collector.feed(landing.LANDING_HTML)
    return collector


@pytest.mark.anyio
async def test_a_stranger_gets_the_landing_page_with_no_credentials():
    async with _client() as c:
        resp = await c.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.text == landing.LANDING_HTML


@pytest.mark.anyio
async def test_the_old_chat_room_is_gone():
    async with _client() as c:
        for path in ("/webui", "/webui/", "/webui/index.html", "/webui/app.js"):
            assert (await c.get(path)).status_code == 404, path
    assert not hasattr(coordinator, "webui_path")


@pytest.mark.anyio
async def test_the_page_is_not_behind_the_contribution_wall():
    """A visitor with no worker and a spent trial must still see where to get the extension."""
    coordinator.starter_quota_mgr._usage["203.0.113.77"] = 10 ** 6
    try:
        async with _client() as c:
            assert (await c.get("/")).status_code == 200
    finally:
        coordinator.starter_quota_mgr._usage.pop("203.0.113.77", None)


def test_the_page_points_at_the_extension_and_the_discord():
    links = [a.get("href") for a in _parsed().links]
    assert landing.EXTENSION_URL in links
    assert landing.DISCORD_INVITE_URL in links
    assert landing.EXTENSION_URL.startswith("https://open-vsx.org/extension/")


def test_every_link_is_https_and_opens_safely():
    for a in _parsed().links:
        assert a["href"].startswith("https://"), a
        assert "noopener" in a.get("rel", ""), a


def test_the_page_is_self_contained():
    """No CDN, no external font, no tracker: it must load behind any firewall."""
    page = _parsed()
    assert all("src" not in s for s in page.scripts), "an external script is referenced"
    assert page.styles == [], "an external stylesheet is referenced"
    assert "<img" not in landing.LANDING_HTML
    assert "@import" not in landing.LANDING_HTML


def test_the_page_works_on_a_phone_and_in_the_dark():
    assert 'name="viewport"' in landing.LANDING_HTML
    assert "prefers-color-scheme: dark" in landing.LANDING_HTML
    assert "<html lang=" in landing.LANDING_HTML


def test_the_live_status_uses_the_public_endpoint_and_degrades_gracefully():
    assert "fetch('/v1/cluster/status'" in landing.LANDING_HTML
    assert ".catch(" in landing.LANDING_HTML
    assert "Status unavailable" in landing.LANDING_HTML


def test_the_page_states_the_free_trial_the_coordinator_actually_gives():
    assert "200" in landing.LANDING_HTML
    assert coordinator.StarterQuotaManager.__init__.__defaults__[-1] == 200


def test_the_page_is_small():
    assert len(landing.LANDING_HTML.encode()) < 8 * 1024


def test_the_page_never_mentions_the_retired_chat_room():
    text = landing.LANDING_HTML.lower()
    assert "webui" not in text and "chat room" not in text
