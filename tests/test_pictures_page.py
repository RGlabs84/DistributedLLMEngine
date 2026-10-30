"""
The pictures page at /pictures (engine/pictures_page.py, 2026-10-30): where "Pictures are paused for now" points.

These pin that it is served like the phone chat (the same strict headers, public, nothing from other sites), that it
types nothing live into its HTML (the owner's rule: every status reads live), and, under node with a stand-in DOM and
fetch, what it says for each thing the status can say.
"""

import json
import re
import shutil
import subprocess

import pytest
from httpx import ASGITransport, AsyncClient

from engine import chat_page, coordinator, pictures_page
from engine.landing import PUBLIC_URL

REMOTE = ("203.0.113.78", 40000)   # TEST-NET-3: a stranger, not the coordinator host
NOTE = ("Pictures are paused for now: DLNE's graphics card is busy running the smarter coding model. "
        "They will be back.")


def _client(ip=REMOTE):
    return AsyncClient(transport=ASGITransport(app=coordinator.app, client=ip), base_url="http://test")


# ---------------------------------------------------------------- the words and the address


def test_the_note_and_its_link_are_the_contracts():
    assert pictures_page.PICTURES_PAUSED_NOTE == NOTE and pictures_page.PICTURES_PATH == "/pictures"
    assert pictures_page.paused_note_with_link() == NOTE + " More: " + PUBLIC_URL + "/pictures"
    assert pictures_page.paused_note_with_link().startswith(NOTE + " More: https://")
    assert coordinator.PICTURES_PAUSED_NOTE is pictures_page.PICTURES_PAUSED_NOTE        # one constant, reused


# ---------------------------------------------------------------- serving


@pytest.mark.anyio
async def test_the_page_is_public_and_served_with_the_chats_strict_headers(monkeypatch):
    real = coordinator.get_worker_limits
    monkeypatch.setattr(coordinator, "get_worker_limits", lambda: {**real(), "enforce_contrib_clause": True})
    async with _client() as c:
        page = await c.get("/pictures")
        head = await c.head("/pictures")
        chat = await c.get("/chat")
    assert page.status_code == 200 and head.status_code == 200
    assert page.headers["content-type"].startswith("text/html") and page.text == pictures_page.PICTURES_HTML
    for name in ("content-security-policy", "x-frame-options", "x-content-type-options", "referrer-policy",
                 "cache-control"):
        assert page.headers[name] == chat.headers[name], name
    assert page.headers["content-security-policy"] == chat_page.CSP and "connect-src 'self'" in chat_page.CSP


def test_the_page_loads_nothing_from_other_sites_and_links_only_to_itself():
    page = pictures_page.PICTURES_HTML
    assert re.findall(r"https?://", page) == []
    for value in re.findall(r'(?:src|href)="([^"]*)"', page):
        assert value.startswith("/") and not value.startswith("//"), value
    assert 'href="/chat"' in page
    assert len(re.findall(r"<script[^>]*>", page)) == 1 and "<script src" not in page and "@import" not in page


def test_nothing_live_is_typed_into_the_page():
    page = pictures_page.PICTURES_HTML
    assert coordinator.VERSION not in page
    assert NOTE not in page and "graphics card" not in page          # the note is the status's own, read live
    assert "const STATUS = '/v1/cluster/status';" in page and "const EVERY = 60 * 1000;" in page
    assert "fetch(STATUS, { cache: 'no-store'" in page
    assert "setInterval(() => { if (document.visibilityState === 'visible') refresh(); }, EVERY);" in page
    # Text from the status only ever goes in as text.
    assert "innerHTML" not in page and "insertAdjacentHTML" not in page and "document.write" not in page


# ---------------------------------------------------------------- what it says, under node

HARNESS = r"""
const fs = require('fs');
const html = fs.readFileSync(process.argv[2], 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const cases = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));

async function run(c) {
  const els = {};
  const byId = (id) => (els[id] = els[id] || { id, textContent: '', hidden: true, className: '' });
  const asked = [];
  const timers = [];
  const ctx = {
    document: { getElementById: byId, visibilityState: 'visible', addEventListener: () => {} },
    setInterval: (fn, ms) => { timers.push(ms); },
    fetch: async (url, opts) => {
      asked.push([url, opts && opts.cache]);
      if (c.throws) throw new TypeError('Failed to fetch');
      return { ok: c.status === 200, status: c.status, json: async () => c.body };
    },
  };
  new Function('document', 'setInterval', 'fetch', script)(ctx.document, ctx.setInterval, ctx.fetch);
  await new Promise((r) => setTimeout(r, 20));
  return {
    title: byId('title').textContent, note: byId('note').hidden ? null : byId('note').textContent,
    more: byId('more').textContent, dot: byId('dot').className, checked: byId('checked').hidden ? null : byId('checked').textContent,
    asked, timers,
  };
}

(async () => {
  const out = [];
  for (const c of cases) out.push(await run(c));
  process.stdout.write(JSON.stringify(out));
})();
"""

PAUSED_STATUS = {"capabilities": {"vision": True, "image_generation": False, "image_generation_paused": True},
                 "images": {"paused": True, "note": NOTE, "page": "/pictures"}}
CASES = [
    ("paused", {"status": 200, "body": PAUSED_STATUS}),
    ("on", {"status": 200, "body": {"capabilities": {"vision": True, "image_generation": True}}}),
    ("off", {"status": 200, "body": {"capabilities": {"vision": True}}}),
    ("on but not ready", {"status": 200, "body": {"capabilities": {"image_generation": False}}}),
    ("not an explicit true", {"status": 200, "body": {"images": {"paused": "true"}, "capabilities": {"image_generation": 1}}}),
    ("a 502 from the proxy", {"status": 502, "body": None}),
    ("no network", {"throws": True}),
    ("paused with no note", {"status": 200, "body": {"images": {"paused": True, "note": 5}}}),
]


@pytest.fixture(scope="module")
def said(tmp_path_factory):
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    folder = tmp_path_factory.mktemp("pictures")
    (folder / "harness.js").write_text(HARNESS, encoding="utf-8")
    (folder / "page.html").write_text(pictures_page.PICTURES_HTML, encoding="utf-8")
    (folder / "cases.json").write_text(json.dumps([case for _name, case in CASES]), encoding="utf-8")
    result = subprocess.run(["node", str(folder / "harness.js"), str(folder / "page.html"), str(folder / "cases.json")],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return dict(zip([name for name, _case in CASES], json.loads(result.stdout)))


def test_paused_shows_the_servers_own_note(said):
    view = said["paused"]
    assert view["title"] == "Pictures are paused" and view["note"] == NOTE
    assert view["more"] == "You can still chat with DLNE as usual." and view["dot"] == "dot down"


def test_on_points_at_the_chat(said):
    view = said["on"]
    assert view["title"] == "Pictures are on" and view["note"] is None and view["dot"] == "dot up"
    assert "DLNE Chat" in view["more"]


@pytest.mark.parametrize("name", ["off", "on but not ready", "not an explicit true"])
def test_anything_else_the_status_says_is_not_available(said, name):
    view = said[name]
    assert view["title"] == "Pictures aren't available right now" and view["note"] is None and view["dot"] == "dot"


@pytest.mark.parametrize("name", ["a 502 from the proxy", "no network"])
def test_no_status_means_dlne_is_restarting(said, name):
    view = said[name]
    assert view["title"] == "DLNE is restarting" and view["note"] is None
    assert view["more"] == "This page checks again every minute."


def test_a_note_that_is_not_text_is_left_out(said):
    assert said["paused with no note"]["title"] == "Pictures are paused" and said["paused with no note"]["note"] is None


def test_it_reads_the_status_on_load_and_every_minute(said):
    for view in said.values():
        assert view["asked"] == [["/v1/cluster/status", "no-store"]] and view["timers"] == [60000]
        assert view["checked"] is not None and view["checked"].startswith("Checked at ")
