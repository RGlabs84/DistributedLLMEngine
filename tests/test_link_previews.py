"""
Launch-night polish (2.3.6): links to / and /chat shared on LinkedIn, Discord or X show the DLNE
card, uptime monitors that send HEAD see the pages up, and the coordinator no longer publishes
a map of its routes.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from engine import coordinator
from engine.chat_page import CHAT_HTML
from engine.landing import LANDING_HTML, OG_IMAGE_URL


def test_both_pages_carry_a_preview_card_with_the_official_logo():
    assert OG_IMAGE_URL.startswith("https://dlne.rossgilsonlabs.dev/chat/icon-512.png")
    for page, url in ((LANDING_HTML, "https://dlne.rossgilsonlabs.dev/"), (CHAT_HTML, "https://dlne.rossgilsonlabs.dev/chat")):
        assert f'<meta property="og:image" content="{OG_IMAGE_URL}">' in page
        assert f'<meta property="og:url" content="{url}">' in page
        assert 'property="og:title"' in page and 'name="twitter:card"' in page
        assert "{{" not in page


async def _request(method, path):
    transport = ASGITransport(app=coordinator.app, client=("203.0.113.9", 5000))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path)


@pytest.mark.anyio
@pytest.mark.parametrize("path", ["/", "/chat"])
async def test_pages_answer_head(path):
    assert (await _request("HEAD", path)).status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
async def test_no_route_map_is_published(path):
    assert (await _request("GET", path)).status_code == 404
