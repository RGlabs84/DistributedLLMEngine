"""
The coordinator's only web page: a signpost to the extension plus a live view of the swarm.

DLNE is a VS Code extension, not a website. These tests pin that the old browser chat room is
gone, the replacement is public, self-contained and well-formed, and nothing in it can be
mistaken for (or reached as) the old WebUI.
"""

import re
from html.parser import HTMLParser

import pytest
from httpx import ASGITransport, AsyncClient

from engine import coordinator, icons, landing

REMOTE = ("203.0.113.77", 40000)   # TEST-NET-3: a stranger, not the coordinator host


def _client(ip=REMOTE):
    return AsyncClient(transport=ASGITransport(app=coordinator.app, client=ip), base_url="http://test")


class _Collector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self.scripts, self.styles, self.images, self.tags = [], [], [], [], []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        attrs = dict(attrs)
        if tag == "a":
            self.links.append(attrs)
        if tag == "img":
            self.images.append(attrs)
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


def test_the_footer_links_the_website_next_to_the_discord():
    assert landing.WEBSITE_URL == "https://dlne.rossgilsonlabs.dev"
    assert landing.DISCORD_INVITE_URL == "https://discord.gg/RNxvxWn8cw"
    footer = re.search(r"<footer.*?</footer>", landing.LANDING_HTML, re.S).group(0)
    anchors = re.findall(r'<a href="([^"]+)" rel="([^"]+)">([^<]+)</a>', footer)
    assert anchors == [(landing.WEBSITE_URL, "noopener", "DLNE website"),
                       (landing.DISCORD_INVITE_URL, "noopener", "Join the Discord")]


def test_on_a_phone_the_chat_is_the_big_primary_button_and_the_extension_the_secondary():
    """Chosen by the screen's width alone (no user-agent sniffing); wide screens keep the extension first."""
    page = landing.LANDING_HTML
    ext = re.search(r'<a class="button ext" href="([^"]+)"', page)
    chat = re.search(r'<a class="button chat" href="([^"]+)"', page)
    assert ext.group(1) == landing.EXTENSION_URL and chat.group(1) == "/chat"
    # Wide: the extension is the filled button, the chat the outlined one.
    wide = page.split("@media (max-width: 600px)", 1)[0]
    assert re.search(r"\.button \{[^}]*background: var\(--accent\)", wide)
    assert re.search(r"\.button\.chat \{[^}]*background: transparent", wide)
    # Narrow: the chat comes first, filled and bigger; the extension is outlined.
    narrow = re.search(r"@media \(max-width: 600px\) \{(.*?)\n\}\n", page, re.S).group(1)
    chat_rule = re.search(r"\.button\.chat \{([^}]*)\}", narrow).group(1)
    assert "order: -1" in chat_rule and "background: var(--accent)" in chat_rule
    assert float(re.search(r"font-size: ([\d.]+)rem", chat_rule).group(1)) > 1.05   # bigger than the other button
    assert "background: transparent" in re.search(r"\.button\.ext \{([^}]*)\}", narrow).group(1)
    assert "flex-direction: column" in narrow
    script = re.search(r"<script>(.*?)</script>", page, re.S).group(1)
    for sniffing in ("navigator", "userAgent", "matchMedia"):
        assert sniffing not in script


def test_every_link_is_https_and_opens_safely():
    """Off-site links are https and open safely; the one on-site link is the phone chat."""
    for a in _parsed().links:
        if a["href"] == "/chat":
            continue
        assert a["href"].startswith("https://"), a
        assert "noopener" in a.get("rel", ""), a


def test_the_page_points_phones_at_the_chat():
    assert "/chat" in [a.get("href") for a in _parsed().links]


def test_the_page_is_self_contained():
    """No CDN, no external font, no tracker: it must load behind any firewall."""
    page = _parsed()
    assert all("src" not in s for s in page.scripts), "an external script is referenced"
    assert [s for s in page.styles if "stylesheet" in (s.get("rel") or "")] == [], "an external stylesheet is referenced"
    assert all(s["href"].startswith("/chat/") for s in page.styles), "a link points off this server"
    assert "@import" not in landing.LANDING_HTML


def test_the_page_shows_the_dlne_logo_and_its_favicon_from_the_chat_routes():
    """The one DLNE icon: the logo PNGs the chat router serves, linked with their cache-busting version."""
    page = _parsed()
    assert [img["src"] for img in page.images] == [icons.icon_url("icon-192.png")]
    assert page.images[0]["alt"] == "DLNE"
    favicons = {s["sizes"]: s["href"] for s in page.styles if s.get("rel") == "icon"}
    assert favicons == {"32x32": icons.icon_url("favicon-32.png"), "48x48": icons.icon_url("favicon-48.png")}
    assert "<svg" not in landing.LANDING_HTML and "data:" not in landing.LANDING_HTML


@pytest.mark.anyio
async def test_the_landing_logo_and_favicons_are_served():
    page = _parsed()
    async with _client() as c:
        for url in [img["src"] for img in page.images] + [link["href"] for link in page.styles]:
            resp = await c.get(url)
            assert resp.status_code == 200, url
            assert resp.headers["content-type"] == "image/png", url
            assert resp.content == icons.icon_png(url.split("?")[0].rsplit("/", 1)[1]), url


def test_the_page_works_on_a_phone_and_in_the_dark():
    assert 'name="viewport"' in landing.LANDING_HTML
    assert "prefers-color-scheme: dark" in landing.LANDING_HTML
    assert "<html lang=" in landing.LANDING_HTML


def test_the_live_status_uses_the_public_endpoint_and_degrades_gracefully():
    assert "fetch('/v1/cluster/status'" in landing.LANDING_HTML
    assert ".catch(" in landing.LANDING_HTML
    assert "Status unavailable" in landing.LANDING_HTML


def test_the_page_states_the_free_trial_the_coordinator_actually_gives():
    assert "100" in landing.LANDING_HTML
    assert coordinator.StarterQuotaManager.__init__.__defaults__[-1] == 100


def test_the_page_is_small():
    assert len(landing.LANDING_HTML.encode()) < 8 * 1024


def test_the_page_never_mentions_the_retired_chat_room():
    text = landing.LANDING_HTML.lower()
    assert "webui" not in text and "chat room" not in text
