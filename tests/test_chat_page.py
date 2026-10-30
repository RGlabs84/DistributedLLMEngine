"""
DLNE Chat, the phone page at /chat (engine/chat_page.py).

These pin what makes the page safe to put in front of strangers (strict headers, nothing loaded
from other sites, model text never parsed as HTML, the privacy notice) and what makes it
installable on a phone (a valid manifest whose icons are real PNGs, the Apple tags, a service
worker that caches the app shell and never an API answer).
"""

import json
import re
import shutil
import struct
import subprocess
from html.parser import HTMLParser

import pytest
from httpx import ASGITransport, AsyncClient

from engine import chat_page, coordinator
from engine.landing import OG_IMAGE_URL, PUBLIC_URL

REMOTE = ("203.0.113.78", 40000)   # TEST-NET-3: a stranger, not the coordinator host
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
SUPPORT_LINKS = {chat_page.COFFEE_URL, chat_page.EXTENSION_URL}
COMMUNITY_LINKS = {chat_page.WEBSITE_URL, chat_page.DISCORD_INVITE_URL}
OUTSIDE_LINKS = SUPPORT_LINKS | COMMUNITY_LINKS   # every address the page may link to, and nothing else
ICON_PATHS = [f"/chat/{name}" for name in chat_page.ICONS]


def _client(ip=REMOTE):
    return AsyncClient(transport=ASGITransport(app=coordinator.app, client=ip), base_url="http://test")


class _Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    handle_startendtag = handle_starttag


def _tags(tag=None):
    parser = _Tags()
    parser.feed(chat_page.CHAT_HTML)
    return [attrs for name, attrs in parser.tags if tag is None or name == tag]


def _png_header(data):
    """(width, height, bit depth, colour type) from a PNG's IHDR; colour type 2 is RGB with no alpha."""
    assert data.startswith(PNG_SIGNATURE)
    return struct.unpack(">IIBB", data[16:26])


# ---------------------------------------------------------------- serving


@pytest.mark.anyio
async def test_the_page_is_served_with_strict_headers():
    async with _client() as c:
        resp = await c.get("/chat")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.text == chat_page.CHAT_HTML
    csp = resp.headers["content-security-policy"]
    assert csp == chat_page.CSP
    for directive in ("default-src 'self'", "connect-src 'self'", "script-src 'self' 'unsafe-inline'",
                      "img-src 'self' data:", "manifest-src 'self'", "worker-src 'self'",
                      "base-uri 'none'", "frame-ancestors 'none'"):
        assert directive in csp
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert "no-cache" in resp.headers["cache-control"]


@pytest.mark.anyio
async def test_every_chat_route_answers_with_the_right_type():
    expected = {"/chat": "text/html", "/chat/manifest.webmanifest": "application/manifest+json",
                "/chat/sw.js": "text/javascript", **{path: "image/png" for path in ICON_PATHS}}
    async with _client() as c:
        for path, kind in expected.items():
            resp = await c.get(path)
            assert resp.status_code == 200, path
            assert resp.headers["content-type"].startswith(kind), (path, resp.headers["content-type"])
            assert resp.headers["x-content-type-options"] == "nosniff", path


@pytest.mark.anyio
async def test_the_page_is_not_behind_the_contribution_wall():
    """A phone with a spent coding trial must still get the chat (the CPU pool has its own throttle)."""
    coordinator.starter_quota_mgr._usage["203.0.113.78"] = 10 ** 6
    try:
        async with _client() as c:
            for path in ["/chat", "/chat/manifest.webmanifest", "/chat/sw.js"] + ICON_PATHS:
                assert (await c.get(path)).status_code == 200, path
    finally:
        coordinator.starter_quota_mgr._usage.pop("203.0.113.78", None)


@pytest.mark.anyio
async def test_only_the_named_files_are_served():
    async with _client() as c:
        for path in ("/chat/icon-64.png", "/chat/../engine/config.py", "/chat/index.html"):
            assert (await c.get(path)).status_code == 404, path


@pytest.mark.anyio
async def test_the_landing_page_links_to_the_chat():
    async with _client() as c:
        resp = await c.get("/")
    assert 'href="/chat"' in resp.text


# ---------------------------------------------------------------- installing


@pytest.mark.anyio
async def test_the_manifest_makes_the_page_installable():
    async with _client() as c:
        resp = await c.get("/chat/manifest.webmanifest")
        manifest = json.loads(resp.text)
        assert manifest["name"] == "DLNE Chat"
        assert manifest["short_name"] == "DLNE"
        assert manifest["start_url"] == "/chat"
        assert manifest["start_url"].startswith(manifest["scope"])
        assert manifest["display"] == "standalone"
        for colour in ("theme_color", "background_color"):
            assert re.fullmatch(r"#[0-9a-fA-F]{6}", manifest[colour]), colour

        icons = manifest["icons"]
        offered = {(icon["sizes"], icon.get("purpose", "any")) for icon in icons}
        assert {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")} <= offered
        for icon in icons:
            assert icon["type"] == "image/png"
            got = await c.get(icon["src"])
            assert got.status_code == 200, icon["src"]
            assert got.headers["content-type"] == "image/png"
            assert got.content.startswith(PNG_SIGNATURE), icon["src"]
            width, height = struct.unpack(">II", got.content[16:24])
            assert f"{width}x{height}" == icon["sizes"], icon["src"]


@pytest.mark.anyio
async def test_icons_that_a_platform_crops_are_opaque():
    """iOS shows transparent pixels as black, and a maskable icon must fill its square."""
    async with _client() as c:
        for path, size in (("/chat/apple-touch-icon.png", 180), ("/chat/icon-maskable-512.png", 512)):
            resp = await c.get(path)
            width, height, depth, colour = _png_header(resp.content)
            assert (width, height, depth) == (size, size, 8), path
            assert colour == 2, f"{path} must be RGB with no alpha channel"


@pytest.mark.anyio
async def test_an_icon_is_cached_for_a_year_only_under_its_current_version():
    """A phone that cached an older icon must not keep it: only the versioned URL is long-lived."""
    async with _client() as c:
        for name in chat_page.ICONS:
            versioned = await c.get(chat_page.icon_url(name))
            assert versioned.headers["cache-control"] == "public, max-age=31536000, immutable", name
            for path in (f"/chat/{name}", f"/chat/{name}?v=old", f"/chat/{name}?v="):
                other = await c.get(path)
                assert other.headers["cache-control"] == "no-cache", path
                assert other.content == versioned.content, path


def test_the_page_has_the_tags_phones_need_to_install_it():
    metas = {m.get("name"): m.get("content") for m in _tags("meta") if m.get("name")}
    assert "viewport-fit=cover" in metas["viewport"] and "width=device-width" in metas["viewport"]
    assert metas["apple-mobile-web-app-capable"] == "yes"
    assert metas["apple-mobile-web-app-title"] == "DLNE"
    assert "theme-color" in metas
    links = {link.get("rel"): link.get("href") for link in _tags("link")}
    assert links["manifest"] == "/chat/manifest.webmanifest"
    assert links["apple-touch-icon"] == chat_page.icon_url("apple-touch-icon.png")
    favicons = {link["sizes"]: link["href"] for link in _tags("link") if link.get("rel") == "icon"}
    assert favicons == {"32x32": chat_page.icon_url("favicon-32.png"), "48x48": chat_page.icon_url("favicon-48.png")}
    page = chat_page.CHAT_HTML
    assert "100dvh" in page
    assert "env(safe-area-inset-top)" in page and "env(safe-area-inset-bottom)" in page
    assert "prefers-color-scheme: light" in page


@pytest.mark.anyio
async def test_the_service_worker_caches_only_the_app_shell():
    async with _client() as c:
        resp = await c.get("/chat/sw.js")
        worker = resp.text
        assert resp.headers["service-worker-allowed"] == "/chat"
        assert "no-cache" in resp.headers["cache-control"]
        assert "/v1" not in worker
        shell = json.loads(re.search(r"const SHELL = (\[.*?\]);", worker).group(1))
        assert shell == chat_page.SHELL_PATHS
        for path in shell:
            assert path == "/chat" or path.startswith("/chat/"), path
            assert (await c.get(path)).status_code == 200, path
    # It answers only for shell paths, and stores nothing but the page itself.
    assert "!SHELL.includes(key)) return;" in worker
    assert "const key = url.pathname === '/chat' ? '/chat' : url.pathname + url.search;" in worker
    assert worker.count(".put(") == 1 and "cache.put('/chat', copy)" in worker
    assert chat_page.SHELL_VERSION in worker
    assert "register('/chat/sw.js', { scope: '/chat' })" in chat_page.CHAT_HTML


# ---------------------------------------------------------------- safety


def test_the_page_loads_nothing_from_other_sites():
    page = chat_page.CHAT_HTML
    assert all("src" not in script for script in _tags("script")), "an external script is referenced"
    assert not [link for link in _tags("link") if "stylesheet" in (link.get("rel") or "")]
    assert "@import" not in page
    for attrs in _tags():
        for key in ("src", "href"):
            value = attrs.get(key)
            if value is None:
                continue
            assert value in OUTSIDE_LINKS or value.startswith("#") or (
                value.startswith("/") and not value.startswith("//")), value
    # The link-preview card names the page's own public address (meta content, never loaded by
    # the page itself).
    preview = {f"{PUBLIC_URL}/chat", OG_IMAGE_URL}
    assert set(re.findall(r"https?://[^\s\"'<>)]+", page)) == OUTSIDE_LINKS | preview


def test_the_support_links_are_there_and_open_safely():
    anchors = [a for a in _tags("a") if a.get("href") in OUTSIDE_LINKS]
    assert {a["href"] for a in anchors} == OUTSIDE_LINKS
    for a in anchors:
        assert a.get("target") == "_blank", a
        assert a.get("rel") == "noopener noreferrer", a
    assert "Support DLNE: buy a coffee" in chat_page.CHAT_HTML
    assert "Donate compute at home" in chat_page.CHAT_HTML
    assert "Add to Home Screen" in chat_page.CHAT_HTML and "Install app" in chat_page.CHAT_HTML


def test_the_website_and_the_discord_are_linked_from_the_menu_and_the_welcome_screen():
    assert chat_page.WEBSITE_URL == "https://dlne.rossgilsonlabs.dev"
    assert chat_page.DISCORD_INVITE_URL == "https://discord.gg/RNxvxWn8cw"
    page = chat_page.CHAT_HTML
    # The menu: a "More from DLNE" section of two link rows, above "Support DLNE".
    menu = page.split('<dialog id="menu"', 1)[1].split("</dialog>", 1)[0]
    assert menu.index("<h3>More from DLNE</h3>") < menu.index("<h3>Support DLNE</h3>")
    section = menu.split("<h3>More from DLNE</h3>", 1)[1].split("<h3>Support DLNE</h3>", 1)[0]
    rows = re.findall(r'<a class="linkrow" href="([^"]+)"[^>]*>([^<]+)</a>', section)
    assert rows == [(chat_page.WEBSITE_URL, "DLNE website"), (chat_page.DISCORD_INVITE_URL, "Discord")]
    # The welcome screen's support line: short, and every item a link that opens in a new tab.
    welcome = page.split('<section class="welcome"', 1)[1].split("</section>", 1)[0]
    support = re.search(r'<p class="support">(.*?)</p>', welcome, re.S).group(1)
    items = re.findall(r'<a href="([^"]+)" target="_blank" rel="noopener noreferrer">([^<]+)</a>', support)
    assert items == [(chat_page.WEBSITE_URL, "Website"), (chat_page.DISCORD_INVITE_URL, "Discord"),
                     (chat_page.COFFEE_URL, "Buy a coffee"), (chat_page.EXTENSION_URL, "Donate compute")]
    assert re.sub(r"<[^>]+>", "", support).replace("&middot;", "·") == "Website · Discord · Buy a coffee · Donate compute"
    # A link never breaks in two on a narrow phone, and the line balances when it has to wrap.
    assert re.search(r"\.support a \{[^}]*white-space: nowrap", page) and "text-wrap: balance" in page


def test_the_links_need_nothing_from_the_content_security_policy():
    """Plain navigation is not a fetch: the policy still names no other site."""
    assert chat_page.CSP == ("default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                             "connect-src 'self'; img-src 'self' data:; manifest-src 'self'; worker-src 'self'; "
                             "base-uri 'none'; frame-ancestors 'none'")
    assert "http" not in chat_page.CSP and "navigate-to" not in chat_page.CSP and "form-action" not in chat_page.CSP


def test_model_and_user_text_are_never_parsed_as_html():
    page = chat_page.CHAT_HTML
    assert "innerHTML" not in page
    for sink in ("outerHTML", "insertAdjacentHTML", "document.write", "DOMParser",
                 "createContextualFragment", "eval(", "new Function"):
        assert sink not in page, sink
    assert "textContent" in page
    # Links are made only for http(s), and open without handing over the page.
    assert "url.protocol === 'https:' || url.protocol === 'http:'" in page
    assert "a.rel = 'noopener noreferrer nofollow'" in page


def test_the_page_talks_only_to_the_phone_endpoints():
    """Never the coding engine: that would spend the visitor's coding trial."""
    page = chat_page.CHAT_HTML
    assert set(re.findall(r"/v1/[\w/.-]*", page)) == {
        "/v1/app/chat/completions", "/v1/app/report", "/v1/app/images", "/v1/cluster/status"}


def test_the_privacy_notice_is_shown_once_and_repeated_under_the_input():
    page = chat_page.CHAT_HTML
    assert chat_page.PRIVACY_NOTICE == ("Answers are written by DLNE's own server. If it is restarting or down, a volunteer's "
                                        "computer may answer instead. Don't send passwords, private details or anything secret.")
    assert chat_page.PRIVACY_NOTICE in page
    assert "dlne.chat.noticeSeen" in page
    assert re.search(r'<p class="hint">[^<]*secret[^<]*</p>', page)


def test_storage_access_is_guarded():
    """localStorage throws in private modes and when disabled; the page must still work."""
    page = chat_page.CHAT_HTML
    assert page.count("localStorage") == 2
    assert "try { return window.localStorage.getItem(key); } catch (e) { return null; }" in page
    assert "try { window.localStorage.setItem(key, value); return null; } catch (e)" in page


def test_the_privacy_lines_cover_lookups_and_reports():
    menu = chat_page.CHAT_HTML.split('<dialog id="menu"', 1)[1].split("</dialog>", 1)[0]
    assert chat_page.LOOKUP_PRIVACY == (
        "Lookups (weather, Wikipedia, news, web search) are made by the DLNE server, not your phone; "
        "reported answers are kept on the server for review.")
    assert chat_page.REPORT_PRIVACY == "Reported answers are kept on the DLNE server for review."
    for line in (chat_page.PRIVACY_NOTICE, chat_page.LOOKUP_PRIVACY, chat_page.PHOTO_PRIVACY, chat_page.PICTURE_PRIVACY,
                 chat_page.LOCATION_PRIVACY):
        assert line in menu
    assert "Brave" not in chat_page.CHAT_HTML


def test_the_report_sheet_offers_the_four_reasons_and_a_short_note():
    sheet = chat_page.CHAT_HTML.split('<dialog id="report"', 1)[1].split("</dialog>", 1)[0]
    radios = re.findall(r'<input type="radio" name="reason" value="(\w+)">\s*([^<]+?)\s*</label>', sheet)
    assert radios == [("harmful", "Harmful or dangerous"), ("wrong", "Wrong"),
                      ("offensive", "Offensive"), ("other", "Something else")]
    assert re.search(r'<textarea id="report-note"[^>]*maxlength="500"', sheet)
    assert chat_page.REPORT_PRIVACY in sheet
    page = chat_page.CHAT_HTML
    assert "const REPORT = '/v1/app/report';" in page
    assert "Reporting isn't available on this server." in page and "Thanks, we'll review it." in page


def test_search_text_reaches_the_page_only_as_text():
    page = chat_page.CHAT_HTML
    assert "v.status.appendChild(el('span', '', line));" in page
    assert "main.appendChild(el('span', 'source-title', s.title));" in page
    assert "el('p', 'search-note', note)" in page


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_scripts_parse(tmp_path):
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", chat_page.CHAT_HTML, re.S)
    assert len(scripts) == 2
    sources = [(f"page{i}.js", s) for i, s in enumerate(scripts)] + [("sw.js", chat_page.SERVICE_WORKER_JS)]
    for name, source in sources:
        path = tmp_path / name
        path.write_text(source, encoding="utf-8")
        result = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------- the page's logic, run under node

# A DOM just big enough for the page's renderer: elements, text nodes, textContent. It has no
# HTML parser at all, so anything that tried to parse markup would show up as missing elements.
_NODE_PRELUDE = r"""
const fs = require('fs');
const vm = require('vm');

class Text {
  constructor(data) { this.nodeType = 3; this.data = String(data); this.parentNode = null; }
  get textContent() { return this.data; }
}
class Element {
  constructor(tag) {
    this.nodeType = 1; this.tagName = String(tag).toUpperCase(); this.childNodes = [];
    this.className = ''; this.attrs = {}; this.dataset = {}; this.style = {}; this.parentNode = null; this.listeners = {};
  }
  appendChild(node) {
    if (node.parentNode) node.parentNode.removeChild(node);
    node.parentNode = this; this.childNodes.push(node); return node;
  }
  removeChild(node) { this.childNodes.splice(this.childNodes.indexOf(node), 1); node.parentNode = null; return node; }
  get firstChild() { return this.childNodes[0] || null; }
  get lastElementChild() { const kids = this.childNodes.filter((n) => n.nodeType === 1); return kids[kids.length - 1] || null; }
  get textContent() { return this.childNodes.map((n) => n.textContent).join(''); }
  set textContent(value) { this.childNodes = []; if (String(value) !== '') this.appendChild(new Text(value)); }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  click() { (this.listeners.click || []).forEach((fn) => fn({ target: this })); }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  all() { return this.childNodes.filter((n) => n.nodeType === 1).flatMap((n) => [n, ...n.all()]); }
}
globalThis.document = { createElement: (tag) => new Element(tag), createTextNode: (data) => new Text(data) };

const html = fs.readFileSync(process.argv[2], 'utf8');
const core = /<script id="dlne-core">([\s\S]*?)<\/script>/.exec(html)[1];
vm.runInThisContext(core + '\n;globalThis.DLNEChat = DLNEChat;');
const C = globalThis.DLNEChat;
"""
_NODE_HARNESS = _NODE_PRELUDE + r"""
const out = {};

// An answer that made two lookups, read whole and split at every awkward place (inside lines,
// inside UTF-8). Each "sources" event carries the whole list so far.
const recap = { n: 1, title: 'Game recap', url: 'https://example.com/recap', host: 'example.com', age: '2 days ago' };
const events = [
  { dlne: { event: 'searching', tool: 'web_search', query: 'super bowl <b>winner</b> 2026' } },
  { dlne: { event: 'sources', sources: [recap] } },
  { dlne: { event: 'searching', tool: 'news', query: 'super bowl LX result' } },
  { dlne: { event: 'search_note', note: 'News came from a fallback source.' } },
  { dlne: { event: 'search_note', note: 'News came from a fallback source.' } },
  { dlne: { event: 'sources', sources: [recap,
    { n: 2, title: '<img src=x onerror=alert(1)>Evil', url: 'javascript:alert(1)', host: 'evil.test', age: '' },
    { n: 3, title: 'Plain http', url: 'http://www.example.org/a', host: 'spoofed.test', age: '1 week ago' },
  ] } },
  { dlne: { event: 'mystery', text: 'ignored' } },
  { choices: [{ index: 0, delta: { content: 'Team A won [1]. ' }, finish_reason: null }] },
  { choices: [{ index: 0, delta: { content: 'Odd [2]; old [3]; nope [4]; both [1, 3]; café ☕ 🧠' }, finish_reason: null }] },
  { choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] },
];
const wire = events.map((e) => 'data: ' + JSON.stringify(e) + '\r\n\r\n').join('') + 'data: [DONE]\n\n';
const bytes = new TextEncoder().encode(wire);
function run(size) {
  const reply = { role: 'assistant', content: '' };
  const seen = [];
  let done = false;
  const reader = C.streamReader((data) => {
    const result = C.applyData(reply, data);
    if (typeof reply.searching === 'string' && seen[seen.length - 1] !== reply.searching) seen.push(reply.searching);
    if (result === 'done') { done = true; return false; }
    return true;
  });
  for (let i = 0; i < bytes.length; i += size) reader.push(bytes.slice(i, i + size));
  reader.end();
  return { reply, seen, done };
}
out.splits = [1, 2, 3, 5, 7, 64, bytes.length].map(run);

const reply = out.splits[0].reply;
const body = new Element('div');
C.markdown(body, reply.content, reply.sources);
out.cites = body.all().filter((n) => n.tagName === 'SUP').map((s) => {
  const a = s.all().find((n) => n.tagName === 'A');
  return { text: s.textContent, href: a ? a.href : null, rel: a ? a.rel : null, target: a ? a.target : null };
});
out.bodyText = body.textContent;
out.bodyTags = [...new Set(body.all().map((n) => n.tagName))].sort();
const bare = new Element('div');
C.markdown(bare, 'No sources here [1].', undefined);
out.bareText = bare.textContent;
out.bareTags = bare.all().map((n) => n.tagName);

const list = C.sourceList(reply.sources);
out.sourceLinks = list.all().filter((n) => n.tagName === 'A')
  .map((a) => ({ href: a.href, rel: a.rel, target: a.target, text: a.textContent }));
out.sourcePlain = list.all().filter((n) => n.className === 'source plain').map((n) => n.textContent);
out.sourceTags = [...new Set(list.all().map((n) => n.tagName))].sort();
out.sourceRoot = list.tagName;
out.sourceOpen = !!list.open;
out.sourceSummary = list.all().find((n) => n.tagName === 'SUMMARY').textContent;
out.sourceReopened = !!C.sourceList(reply.sources, true).open;
const ten = Array.from({ length: 10 }, (_, i) => ({ n: i + 1, title: 't' + i, url: 'https://site' + (i % 6) + '.test/' + i }));
out.tenSummary = C.sourceList(C.cleanSources(ten)).all().find((n) => n.tagName === 'SUMMARY').textContent;
out.oneSummary = C.sourceList(C.cleanSources([ten[0]])).all().find((n) => n.tagName === 'SUMMARY').textContent;

const dated = { role: 'assistant', content: '' };
C.applyData(dated, JSON.stringify({ dlne: { event: 'searching', query: 'x' } }));
C.applyData(dated, JSON.stringify({ dlne: { event: 'search_unavailable', reason: 'The search budget for today is used up.' } }));
out.dated = dated;
const blank = { role: 'assistant', content: '' };
C.applyData(blank, JSON.stringify({ dlne: { event: 'search_unavailable' } }));
out.blankNote = blank.searchNote;
out.junk = C.cleanSources([null, 'x', { n: 'a', title: 't'.repeat(400), url: 5 }, { n: 1, title: 'dup' },
                           { n: 2, title: '', url: 'https://e.com/' }]);
out.notArray = C.cleanSources('nope');
const tail = { role: 'assistant', content: '' };
C.applyData(tail, JSON.stringify({ choices: [{ delta: { content: '[2]' }, finish_reason: 'stop' }] }));
out.tail = tail;
const notice = { role: 'assistant', content: '' };
C.applyData(notice, JSON.stringify({ choices: [{ delta: { content: '\n\n[No machine in the CPU pool is free right now.]' }, finish_reason: 'stop' }] }));
out.notice = notice;

out.lines = [['weather', 'Philadelphia'], ['wikipedia', 'Ada Lovelace'], ['news', 'storm'], ['web_search', 'x'],
             ['calculator', '2+2'], [undefined, 'old'], ['weather', '']].map(([tool, q]) => C.lookupLine(tool, q));
const four = { role: 'assistant', content: '' };
['weather', 'wikipedia', 'news', 'web_search'].forEach((tool, i) =>
  C.applyData(four, JSON.stringify({ dlne: { event: 'searching', tool, query: 'q' + i } })));
out.latest = four.searching;
const swap = { role: 'assistant', content: '' };
C.applyData(swap, JSON.stringify({ dlne: { event: 'sources', sources: [recap, { n: 2, title: 'b', url: 'https://b.test/' }] } }));
C.applyData(swap, JSON.stringify({ dlne: { event: 'sources', sources: [{ n: 5, title: 'only', url: 'https://c.test/' }] } }));
out.replaced = swap.sources.map((s) => s.n);
C.applyData(swap, JSON.stringify({ dlne: { event: 'sources', sources: [] } }));
out.emptied = 'sources' in swap;

const saved = JSON.parse(JSON.stringify(Object.assign({}, reply, {
  searching: 'stale', reported: true, searchNote: 'dated', evil: '<script>', lookupNotes: ['n1', 7, '', 'n2'] })));
out.restored = C.cleanMessage(saved);
out.dropped = [C.cleanMessage({ role: 'assistant', content: '' }), C.cleanMessage({ role: 'system', content: 'x' }),
               C.cleanMessage(null)];

const msgs = [{ role: 'user', content: 'q1' }, { role: 'assistant', content: 'a1' },
              { role: 'user', content: 'Q'.repeat(5000) }, { role: 'assistant', content: 'A'.repeat(9000) }];
out.report = C.reportPayload(msgs, msgs[3], 'wrong', '  ' + 'n'.repeat(600) + '  ');
out.report1 = C.reportPayload(msgs, msgs[1], 'harmful', '');
out.badReason = C.reportPayload(msgs, msgs[1], 'spam', '');
out.results = [200, 404, 405, 429, 500].map((s) => C.reportResult(s));

// Server 2.3.5 takes an unsure answer back: {"dlne":{"event":"retract"}}. Everything shown so far
// (text, sources, notes) goes, whatever else the message holds stays, and paint() redraws the rest.
const shown = { role: 'assistant', content: 'I am not sure, but it was probably', sources: [recap],
                lookupNotes: ['A note.'], searchNote: 'Dated.', reported: true };
out.retractResult = C.applyData(shown, JSON.stringify({ dlne: { event: 'retract', text: 'extra fields are ignored' } }));
out.retracted = shown;
const untouched = { role: 'assistant', content: '' };
out.retractFirst = [C.applyData(untouched, JSON.stringify({ dlne: { event: 'retract' } })), untouched];

// The whole thing as it arrives: memory text, the retract, then the lookup and a fresh answer,
// split at every awkward place. A page that shows a reply must never show the memory text again.
const takeBack = [
  { choices: [{ index: 0, delta: { content: "I don't have real-time information, " }, finish_reason: null }] },
  { choices: [{ index: 0, delta: { content: 'but my guess is Team B.' }, finish_reason: null }] },
  { dlne: { event: 'retract' } },
  { dlne: { event: 'searching', tool: 'web_search', query: 'who won the game' } },
  { dlne: { event: 'sources', sources: [recap] } },
  { choices: [{ index: 0, delta: { content: 'Team A won [1].' }, finish_reason: null }] },
  { choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] },
];
const takeBackBytes = new TextEncoder().encode(
  takeBack.map((e) => 'data: ' + JSON.stringify(e) + '\n\n').join('') + 'data: [DONE]\n\n');
function runTakeBack(size) {
  const reply = { role: 'assistant', content: '' };
  const states = [];
  const reader = C.streamReader((data) => {
    const result = C.applyData(reply, data);
    if (result === 'changed') states.push(JSON.parse(JSON.stringify(reply)));
    return result !== 'done';
  });
  for (let i = 0; i < takeBackBytes.length; i += size) reader.push(takeBackBytes.slice(i, i + size));
  reader.end();
  return { reply, states };
}
out.takeBack = [1, 3, 7, 64, takeBackBytes.length].map(runTakeBack);

// A chat id is a UUID v4 whatever the random bytes are; a position leaves with at most five decimals.
out.ids = [new Uint8Array(16), new Uint8Array(16).fill(255), Uint8Array.from({ length: 16 }, (_, i) => i * 17)].map(C.uuid4);
out.idChecks = [out.ids[2], 'not-a-uuid', 'abc12def', out.ids[2].slice(0, 14) + '1' + out.ids[2].slice(15), 42, null].map(C.isChatId);
out.places = [
  { latitude: 40.123456, longitude: -74.564949 }, { latitude: -33.8688, longitude: 151.2093 }, { latitude: 0.000004, longitude: -0.000004 },
  { latitude: 91, longitude: 0 }, { latitude: 10, longitude: 181 }, { latitude: NaN, longitude: 5 }, { latitude: null, longitude: null }, null,
].map(C.place);
process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def core_run(tmp_path_factory):
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    folder = tmp_path_factory.mktemp("chat-core")
    (folder / "page.html").write_text(chat_page.CHAT_HTML, encoding="utf-8")
    (folder / "harness.js").write_text(_NODE_HARNESS, encoding="utf-8")
    result = subprocess.run(["node", str(folder / "harness.js"), str(folder / "page.html")],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_search_events_survive_any_split_of_the_stream(core_run):
    first = core_run["splits"][0]
    for run in core_run["splits"]:
        assert run == first
    assert first["done"] is True
    assert first["seen"] == ["Searching the web for “super bowl <b>winner</b> 2026”…",
                             "Checking the news for “super bowl LX result”…"]
    reply = first["reply"]
    assert "searching" not in reply   # the answer started: the lookup line is gone
    assert reply["lookupNotes"] == ["News came from a fallback source."]   # once, though sent twice
    assert reply["content"] == "Team A won [1]. Odd [2]; old [3]; nope [4]; both [1, 3]; café ☕ \U0001f9e0"
    assert [s["n"] for s in reply["sources"]] == [1, 2, 3]
    assert reply["sources"][1]["title"] == "<img src=x onerror=alert(1)>Evil"   # kept as text


def test_in_range_markers_become_citation_links_and_others_stay_text(core_run):
    cites = core_run["cites"]
    assert [c["text"] for c in cites] == ["1", "2", "3", "1", "3"]
    assert cites[0]["href"] == "https://example.com/recap"
    assert cites[1]["href"] is None             # source 2's javascript: URL is never a link
    assert cites[2]["href"] == "http://www.example.org/a"
    for cite in cites:
        if cite["href"]:
            assert cite["target"] == "_blank"
            assert "noopener" in cite["rel"] and "noreferrer" in cite["rel"]
    assert "nope [4]" in core_run["bodyText"]   # out of range: plain text
    assert core_run["bareText"] == "No sources here [1]." and "SUP" not in core_run["bareTags"]
    assert set(core_run["bodyTags"]) <= {"P", "SUP", "A"}


def test_the_source_list_links_only_http_and_shows_titles_as_text(core_run):
    links = core_run["sourceLinks"]
    assert [link["href"] for link in links] == ["https://example.com/recap", "http://www.example.org/a"]
    for link in links:
        assert link["target"] == "_blank" and "noopener" in link["rel"] and "noreferrer" in link["rel"]
    assert "Game recap" in links[0]["text"] and "example.com" in links[0]["text"] and "2 days ago" in links[0]["text"]
    # The host shown is the link's real host, not whatever the event claimed.
    assert "example.org" in links[1]["text"] and "spoofed.test" not in links[1]["text"]
    assert core_run["sourcePlain"] == ["2<img src=x onerror=alert(1)>Evilevil.test"]
    assert set(core_run["sourceTags"]) <= {"SUMMARY", "OL", "LI", "A", "DIV", "SPAN"}


def test_the_sources_fold_into_one_line_that_opens_when_tapped(core_run):
    # Ten sources used to stack up under the answer and push it out of view (owner, 26 Oct 2026).
    assert core_run["sourceRoot"] == "DETAILS"
    assert core_run["sourceOpen"] is False, "closed until the reader opens it"
    assert core_run["sourceReopened"] is True, "a list the reader opened stays open when it is replaced"
    # The line counts the sources and names the first sites (the link's real host, never the claimed one).
    assert core_run["sourceSummary"] == "›3 sources· example.com, evil.test, example.org"
    assert core_run["tenSummary"] == "›10 sources· site0.test, site1.test, site2.test +3"
    assert core_run["oneSummary"] == "›1 source· site0.test"


def test_the_lookup_line_is_worded_by_tool_and_shows_the_latest(core_run):
    assert core_run["lines"] == [
        "Checking the weather for “Philadelphia”…",
        "Looking up “Ada Lovelace” on Wikipedia…",
        "Checking the news for “storm”…",
        "Searching the web for “x”…",
        "Looking up “2+2”…",            # a tool the page does not know
        "Searching the web for “old”…",  # an older coordinator sends no tool
        "Checking the weather…",
    ]
    assert core_run["latest"] == "Searching the web for “q3”…"


def test_each_sources_event_replaces_the_list(core_run):
    assert core_run["replaced"] == [5]
    assert core_run["emptied"] is False


def test_saved_answers_keep_their_sources_and_notes(core_run):
    restored = core_run["restored"]
    assert [s["n"] for s in restored["sources"]] == [1, 2, 3]
    assert restored["sources"][1]["url"] == "javascript:alert(1)"   # kept as data, never linked
    assert restored["lookupNotes"] == ["n1", "n2"]
    assert restored["searchNote"] == "dated" and restored["reported"] is True
    assert "searching" not in restored and "evil" not in restored
    assert core_run["dropped"] == [None, None, None]


def test_search_unavailable_and_junk_events_degrade_safely(core_run):
    assert core_run["dated"] == {"role": "assistant", "content": "",
                                 "searchNote": "The search budget for today is used up."}
    assert core_run["blankNote"]
    assert core_run["junk"] == [
        {"n": 1, "title": "t" * 300, "url": "", "host": "", "age": ""},
        {"n": 2, "title": "https://e.com/", "url": "https://e.com/", "host": "", "age": ""},
    ]
    assert core_run["notArray"] == []
    # A last chunk that is only a citation is the model's text, not a coordinator notice.
    assert core_run["tail"] == {"role": "assistant", "content": "[2]"}
    assert core_run["notice"]["status"] == "notice" and core_run["notice"]["content"] == ""


def test_a_report_carries_the_question_it_answered_cut_to_size(core_run):
    report = core_run["report"]
    assert report["reason"] == "wrong"
    assert report["question"] == "Q" * 4000 and report["answer"] == "A" * 8000
    assert report["note"] == "n" * 500
    assert core_run["report1"] == {"question": "q1", "answer": "a1", "reason": "harmful", "note": ""}
    assert core_run["badReason"] is None
    texts = [r["text"] for r in core_run["results"]]
    assert core_run["results"][0]["ok"] is True and texts[0] == "Thanks, we'll review it."
    assert texts[1] == texts[2] == "Reporting isn't available on this server."
    assert not any(r["ok"] for r in core_run["results"][1:])


def test_a_retract_event_empties_the_answer_and_drops_its_sources_and_notes(core_run):
    assert core_run["retractResult"] == "changed"   # the page repaints
    # Text, sources, lookup notes and the dated-search note are gone; the rest of the message stays.
    assert core_run["retracted"] == {"role": "assistant", "content": "", "reported": True}
    # Nothing to take back yet (the event came first): still a clean, empty answer.
    assert core_run["retractFirst"] == ["changed", {"role": "assistant", "content": ""}]


def test_after_a_retract_the_lookup_and_a_fresh_answer_replace_the_memory_text(core_run):
    first = core_run["takeBack"][0]
    for run in core_run["takeBack"]:
        assert run == first   # the same whatever the network pieces
    memory = ["I don't have real-time information, ", "I don't have real-time information, but my guess is Team B."]
    contents = [state["content"] for state in first["states"]]
    assert contents == memory + ["", "", "", "Team A won [1]."]
    retracted, searching, sourced = first["states"][2:5]
    assert retracted == {"role": "assistant", "content": ""}
    assert searching["searching"] == "Searching the web for “who won the game”…" and searching["content"] == ""
    assert [s["n"] for s in sourced["sources"]] == [1] and "searching" not in sourced
    # The answer that is kept is the fresh one alone, with the lookup's sources.
    reply = first["reply"]
    assert reply["content"] == "Team A won [1]." and [s["n"] for s in reply["sources"]] == [1]
    assert "Team B" not in json.dumps(first["states"][2:]) and "real-time" not in json.dumps(first["states"][2:])


# ---------------------------------------------------------------- chat id and location

UUID4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def test_a_chat_id_is_a_uuid_v4_whatever_the_random_bytes(core_run):
    assert all(UUID4.match(i) for i in core_run["ids"])
    assert core_run["ids"][0] == "00000000-0000-4000-8000-000000000000"
    assert core_run["idChecks"] == [True, False, False, False, False, False]


def test_the_chat_id_is_made_per_chat_kept_when_valid_and_sent_with_every_request():
    page = chat_page.CHAT_HTML
    assert "chatId: newChatId()" in page                     # a new chat starts with one
    assert "if (isChatId(c.chatId)) kept.chatId = c.chatId" in page   # a stored one survives a reload
    assert "if (!conv.chatId) conv.chatId = newChatId();" in page      # an older chat gets one when next sent
    # 2.6.2: every request says this page shows the picture events (image_job, image_refused), so the server may offer its tool
    assert "const payload = { chat_id: conv.chatId, features: ['image_jobs'], messages: context(conv.messages) };" in page


def test_a_position_keeps_five_decimals_and_junk_is_dropped(core_run):
    assert core_run["places"] == [
        {"lat": 40.12346, "lon": -74.56495}, {"lat": -33.8688, "lon": 151.2093}, {"lat": 0, "lon": 0},
        None, None, None, None, None]


def test_location_is_off_by_default_and_only_sent_while_on_with_a_fresh_fix():
    page = chat_page.CHAT_HTML
    checkbox = next(t for t in _tags("input") if t.get("id") == "share-location")
    assert checkbox["type"] == "checkbox" and "checked" not in checkbox   # off until the reader turns it on
    assert "where.enable(store.get(LOCATION) === '1');" in page          # remembered only through the guarded store
    # The send path asks the locator, which hands back only a fresh fix; without one the answer says so. Stop ends the wait.
    assert "const here = await Promise.race([where.forSend(prompted ? PROMPT_WAIT : undefined), whenAborted(ctrl.signal)]);" in page
    assert "if (here) payload.location = here; else reply.locationNote = NO_LOCATION_NOTE;" in page
    # Only a local question wakes the GPS or carries a place; nothing asks the phone on load or on becoming visible.
    assert "if (where.on && asksLocal(conv.messages)) {" in page
    # The browser is asked only by the reader's own tap: the menu's switch, or "Use my location" on the card.
    assert page.count("where.touch()") == 2
    assert "setSharing(ui.shareLocation.checked); where.touch(); });" in page
    yes = page.split("ui.askYes.addEventListener('click', () => {", 1)[1].split("  });", 1)[0]
    assert "setSharing(true);" in yes and "where.touch();" in yes and "submit('yes');" in yes
    assert "payload.location = fix" not in page
    assert "enableHighAccuracy: true, maximumAge: FIX_REFRESH, timeout: 10000" in page
    assert "const FIX_MAX_AGE = 2 * 60 * 1000;" in page
    assert page.count("getCurrentPosition") == 1
    # The coordinates themselves are never saved: the only key written for location is the on/off flag.
    assert "store.set(LOCATION, on ? '1' : '0')" in page
    assert "JSON.stringify({ current, saved, draft })" in page
    for words in ("precise location", "Open-Meteo", "never gets your coordinates", "logged or stored"):
        assert words in page
    assert "approximate" not in page.split('id="share-location"', 1)[1].split("</p>", 1)[0].lower()
    # What the menu says matches what the page does: only a fresh fix, and the answer says when there was none.
    menu_text = page.split('id="share-location"', 1)[1].split("</p>", 1)[0]
    assert "only with a local question" in menu_text and "under two minutes old" in menu_text
    assert "Other questions never ask your phone for it" in menu_text and "the answer says so" in menu_text


@pytest.mark.anyio
async def test_nothing_in_the_headers_blocks_geolocation():
    """The CSP does not govern geolocation; a Permissions-Policy that listed it as empty would."""
    async with _client() as c:
        resp = await c.get("/chat")
    policy = resp.headers.get("permissions-policy", "")
    assert not re.search(r"geolocation\s*=\s*\(\s*\)", policy)
    assert "geolocation" not in resp.headers["content-security-policy"]


# ---------------------------------------------------------------- the location fix (behaviour, under node)

_LOCATION_HARNESS = _NODE_PRELUDE + r"""
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const coords = (lat, lon) => ({ latitude: lat, longitude: lon });
let now = 1700000000000;
const clock = () => now;

function rig(options) {
  options = options || {};
  const geo = options.none ? undefined : {
    calls: [],
    getCurrentPosition(ok, err, opts) {
      this.calls.push({ ok, err, opts });
      if (options.sync) ok({ coords: coords(1, 2), timestamp: now });
    },
  };
  const reports = [];
  const where = C.locator(geo, clock, (kind) => reports.push(kind), 30);
  return { geo, reports, where };
}
const answer = (r, n, position) => r.geo.calls[n].ok(position);
const refuse = (r, n, code) => r.geo.calls[n].err({ code });

(async () => {
  const out = {};

  // Off by default: nothing is asked, nothing is sent.
  let r = rig();
  r.where.touch();
  out.off = { sent: await r.where.forSend(), calls: r.geo.calls.length };

  // A fresh fix goes with the message; a second send does not ask again.
  r = rig();
  r.where.enable(true);
  r.where.touch();
  answer(r, 0, { coords: coords(40.123456, -74.5), timestamp: now });
  const first = await r.where.forSend();
  now += 30000;
  const second = await r.where.forSend();
  out.fresh = { first, second, calls: r.geo.calls.length, reports: r.reports.slice(), opts: r.geo.calls[0].opts };

  // Past a minute it is renewed in the background, and the old one (still under 3 min) goes meanwhile.
  now += 40000;   // 70 s old
  const during = await r.where.forSend();
  out.renewing = { during, calls: r.geo.calls.length };
  answer(r, 1, { coords: coords(41, -75), timestamp: now });
  out.renewed = await r.where.forSend();

  // A fix over two minutes old is never sent: the send waits for a renewal (a moment at most) and goes without.
  r = rig();
  r.where.enable(true);
  r.where.touch();
  answer(r, 0, { coords: coords(10, 10), timestamp: now });
  now += 4 * 60000;
  const t0 = Date.now();
  const stale = await r.where.forSend();
  out.stale = { sent: stale, calls: r.geo.calls.length, waitedMs: Date.now() - t0 };

  // Age comes from position.timestamp, not from when the callback ran: the browser handed back an old position.
  r = rig();
  r.where.enable(true);
  r.where.touch();
  answer(r, 0, { coords: coords(10, 10), timestamp: now - 130000 });   // 2 min 10 s old on arrival
  out.cachedOld = { sent: await r.where.forSend(), reports: r.reports.slice() };
  r = rig();
  r.where.enable(true);
  r.where.touch();
  answer(r, 0, { coords: coords(10, 10), timestamp: now - 100000 });   // under 2 min
  out.cachedOk = await r.where.forSend();
  // A timestamp that cannot be right (another unit, a clock far off) counts from the arrival.
  r = rig();
  r.where.enable(true);
  r.where.touch();
  answer(r, 0, { coords: coords(10, 10), timestamp: 1700000000 });
  const odd = await r.where.forSend();
  out.oddStamp = odd;

  // A failed refresh drops the old fix; the next send neither waits nor asks again for 30 s, then it retries.
  r = rig();
  r.where.enable(true);
  r.where.touch();
  answer(r, 0, { coords: coords(10, 10), timestamp: now });
  now += 70000;
  const stillHeld = await r.where.forSend();     // asks again
  refuse(r, 1, 2);
  const t1 = Date.now();
  const afterFail = await r.where.forSend();
  out.failed = { stillHeld, afterFail, quick: Date.now() - t1 < 25, calls: r.geo.calls.length, reports: r.reports.slice() };
  now += 31000;
  const retry = r.where.forSend();
  answer(r, 2, { coords: coords(12, 13), timestamp: now });
  out.retried = { sent: await retry, calls: r.geo.calls.length, reports: r.reports.slice() };

  // A send waits for a fix that is on its way and uses it.
  r = rig();
  r.where.enable(true);
  const waiting = r.where.forSend();
  await sleep(5);
  answer(r, 0, { coords: coords(5, 6), timestamp: now });
  out.waited = await waiting;

  // Refusing the permission switches sharing off for good; a late answer after switching off is ignored.
  r = rig();
  r.where.enable(true);
  r.where.touch();
  refuse(r, 0, 1);
  out.denied = { on: r.where.on, reports: r.reports, sent: await r.where.forSend() };
  r = rig();
  r.where.enable(true);
  r.where.touch();
  r.where.enable(false);
  answer(r, 0, { coords: coords(5, 6), timestamp: now });
  out.late = { sent: await r.where.forSend(), fix: r.where.fix, reports: r.reports };
  r.where.enable(true);
  r.where.touch();       // the old request is forgotten: a new one is made
  out.reasked = r.geo.calls.length;

  // No geolocation in this browser; one that answers at once; one that throws.
  r = rig({ none: true });
  r.where.enable(true);
  r.where.touch();
  out.unsupported = { on: r.where.on, reports: r.reports };
  r = rig({ sync: true });
  r.where.enable(true);
  out.sync = await r.where.forSend();
  const throwing = C.locator({ getCurrentPosition() { throw new Error('insecure origin'); } }, clock, (k) => (out.threw = k), 30);
  throwing.enable(true);
  out.threwSent = await throwing.forSend();

  const U = (content) => ({ role: 'user', content });
  const A = (content) => ({ role: 'assistant', content });
  out.local = [
    'pizza near me', 'Is there a pharmacy NEARBY?', 'anything near here', 'cafes around here', 'a park close to me', 'what is around me',
    "What's the weather?", 'forecast for tomorrow', 'Will it be raining later', 'is it snowing', 'what is my location', 'where I am right now',
    "my city's mayor", 'in my area', 'where I live', 'my current location please',
    'a picture of the park near me', 'draw what the weather is like where I am',
  ].map(C.isLocalQuestion);
  out.notLocal = [
    'Write a poem about autumn', 'Explain recursion', 'How far is the moon?', 'Translate hello into French', 'nearly done',
    'I live in a sunny place', 'what is the terrain', '', null, 5,
    'hey, can I get a picture of a fox in the snow?', 'draw me a rainy street', 'make an image of snow falling on a cabin',
  ].map(C.isLocalQuestion);
  out.asks = [
    C.asksLocal([U('weather near me'), A('Sunny.'), U('and tomorrow?')]),
    C.asksLocal([U('weather near me'), A('Sunny.'), U('thanks'), A('Welcome.'), U('tell me a joke')]),
    C.asksLocal([U('hello')]), C.asksLocal([]),
    C.asksLocal([A('The weather is nice.'), U('ok')]),   // only the person's own words count
  ];
  out.text = C.LOCATION_TEXT;
  out.note = C.NO_LOCATION_NOTE;
  process.stdout.write(JSON.stringify(out));
})();
"""


@pytest.fixture(scope="module")
def location_run(tmp_path_factory):
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    folder = tmp_path_factory.mktemp("chat-location")
    (folder / "page.html").write_text(chat_page.CHAT_HTML, encoding="utf-8")
    (folder / "harness.js").write_text(_LOCATION_HARNESS, encoding="utf-8")
    result = subprocess.run(["node", str(folder / "harness.js"), str(folder / "page.html")],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_location_stays_off_until_turned_on(location_run):
    assert location_run["off"] == {"sent": None, "calls": 0}


def test_a_fresh_fix_goes_with_the_message_and_is_not_asked_for_twice(location_run):
    fresh = location_run["fresh"]
    assert fresh["first"] == {"lat": 40.12346, "lon": -74.5} and fresh["second"] == fresh["first"]
    assert fresh["calls"] == 1 and fresh["reports"] == [""]
    # High accuracy, and the browser may not hand back a position older than a minute.
    assert fresh["opts"] == {"enableHighAccuracy": True, "maximumAge": 60000, "timeout": 10000}


def test_a_minute_old_fix_is_renewed_in_the_background_while_it_is_still_good(location_run):
    assert location_run["renewing"] == {"during": {"lat": 40.12346, "lon": -74.5}, "calls": 2}
    assert location_run["renewed"] == {"lat": 41, "lon": -75}


def test_a_fix_over_two_minutes_old_is_never_sent(location_run):
    stale = location_run["stale"]
    assert stale["sent"] is None
    assert stale["calls"] == 2            # a renewal was asked for
    assert stale["waitedMs"] < 1000       # and the send waited only the short cap, not for the browser's 10 s


def test_the_fixs_age_is_the_positions_own_timestamp_not_the_callbacks(location_run):
    assert location_run["cachedOld"] == {"sent": None, "reports": ["unavailable"]}
    assert location_run["cachedOk"] == {"lat": 10, "lon": 10}
    assert location_run["oddStamp"] == {"lat": 10, "lon": 10}   # an impossible stamp counts from arrival


def test_a_failed_refresh_drops_the_old_fix_and_the_page_stops_claiming_otherwise(location_run):
    failed = location_run["failed"]
    assert failed["stillHeld"] == {"lat": 10, "lon": 10}   # the renewal was pending, the fix was 70 s old
    assert failed["afterFail"] is None                     # the failure dropped it
    assert failed["quick"] is True and failed["calls"] == 2   # no wait, no new ask inside 30 s
    assert failed["reports"] == ["", "unavailable"]
    assert location_run["retried"] == {"sent": {"lat": 12, "lon": 13}, "calls": 3, "reports": ["", "unavailable", ""]}
    assert "Messages go without it until it works" in location_run["text"]["unavailable"]


def test_a_send_waits_briefly_for_a_fix_that_is_on_its_way(location_run):
    assert location_run["waited"] == {"lat": 5, "lon": 6}


def test_a_refused_permission_switches_sharing_off_and_a_late_answer_is_ignored(location_run):
    assert location_run["denied"] == {"on": False, "reports": ["denied"], "sent": None}
    assert location_run["late"] == {"sent": None, "fix": None, "reports": []}
    assert location_run["reasked"] == 2


def test_a_browser_without_geolocation_or_one_that_throws_falls_back_cleanly(location_run):
    assert location_run["unsupported"] == {"on": False, "reports": ["unsupported"]}
    assert location_run["sync"] == {"lat": 1, "lon": 2}
    assert location_run["threw"] == "unavailable" and location_run["threwSent"] is None


def test_only_local_questions_carry_a_location_the_same_list_as_the_android_app(location_run):
    assert location_run["local"] == [True] * 18
    # The last three are picture requests whose weather words are what to draw (2.6.2: "a fox in the snow" asked first).
    assert location_run["notLocal"] == [False] * 13
    # The latest two user messages count, so a follow-up still gets the position; an older one does not.
    assert location_run["asks"] == [True, False, False, False, False]


def test_a_message_that_went_without_a_fix_says_so_under_its_answer():
    page = chat_page.CHAT_HTML
    assert "Your location wasn't ready, so this local question went without it" in page
    assert "m.locationNote" in page and "if (m.locationNote) v.extras.appendChild(el('p', 'search-note', m.locationNote));" in page
    # It is saved with the answer (and cleaned on the way back), but no coordinates are.
    assert "kept.locationNote = str(m.locationNote, 300)" in page


def test_the_page_says_who_answers_without_promising_it_is_always_the_server():
    page = chat_page.CHAT_HTML
    assert "people lend" not in page and "Volunteers' computers write" not in page and "volunteer’s computer…" not in page
    about = re.search(r'<p class="muted small" id="about">(.*?)</p>', page).group(1)
    assert about == ("Answers come from DLNE's own server; a volunteer's computer answers only if it is restarting or down. "
                     "They can be slow, and they can be wrong.")
    assert "'Answers are written by ' + model + ' on DLNE\\'s own server.'" in page   # the line after the status loads
    assert "They can be slow, and they can be wrong." in page.split("function setStatus", 1)[1]
    assert re.search(r'<p class="hint">[^<]*server[^<]*volunteer[^<]*secrets[^<]*</p>', page)
    assert "answered by DLNE's own server" in page and "people lend" not in chat_page.MANIFEST_JSON
    # The status line still reads these two keys and nothing else of the pool.
    block = page.split("async function refreshStatus()", 1)[1].split("function setStatus", 1)[0]
    assert set(re.findall(r"pool\.(\w+)", block)) == {"members_ready", "active_model"}


# ---------------------------------------------------------------- images, under node

_IMAGE_HARNESS = _NODE_PRELUDE + r"""
const out = {};
const now = 1700000000000;
const png = process.argv[3];
const text = (node) => node.textContent;
const find = (root, cls) => root.all().filter((n) => n.className === cls || n.className.split(' ').indexOf(cls) !== -1);

out.eta = [C.imageEta({ '768x768': 80, '1024x1024': 153 }, '1024x1024'), C.imageEta({ '768x768': 80, '1024x1024': 153 }, '768x1024'),
           C.imageEta(null, '1024x768'), C.imageEta(undefined, 'weird')];
out.about = [80, 153, 61, 3].map(C.aboutSeconds);
out.seconds = [0, 59, 119, 120, 125].map(C.seconds);

const msg = { role: 'assistant', kind: 'image', content: C.imagePlaceholder('a <b>red</b>   fox'), prompt: 'a <b>red</b> fox', size: '768x768',
              state: 'queued', imageId: 'AbCdEf12_-xyz789', startedAt: now };
out.placeholder = msg.content;
const status = (state, extra) => { const m = Object.assign({}, msg, { state }, extra || {}); return C.imageStatus(m); };
out.lines = {
  queued: status('queued'), checking: status('checking'), generating: status('generating', { eta: 80 }),
  generatingBig: status('generating', { size: '1024x1024', eta: 153 }), reviewing: status('reviewing'), completed: status('completed'),
  cancelled: status('cancelled'), expired: status('expired'), failed: status('failed'),
  refused: status('refused', { note: 'I can’t make that picture, but I’d be glad to help with something else.' }),
};
out.clock = [
  C.imageClock(Object.assign({}, msg, { state: 'generating', eta: 80 }), now + 23000),
  C.imageClock(Object.assign({}, msg, { state: 'generating', eta: 80 }), now + 100000),
  C.imageClock(Object.assign({}, msg, { state: 'completed' }), now + 5000),
];

// A poll's answer applied: states, elapsed, eta, the error detail, and a finished job loses its id.
const card = Object.assign({}, msg);
const steps = [
  { status: 'queued', position: 1, eta_seconds: 90 }, { status: 'generating', elapsed_seconds: 12, eta_seconds: 80 },
  { status: 'mystery' }, { status: 'refused', error: { detail: 'That is not something DLNE will draw.', code: 'prompt_refused' } },
];
out.applied = steps.map((job) => { const ok = C.imageApply(card, job, now); return [ok, card.state, card.startedAt, card.eta, card.imageId || null, card.note || null]; });
const okCard = Object.assign({}, msg, { cancelling: true });
C.imageApply(okCard, { status: 'completed', elapsed_seconds: 77 }, now);
out.finished = [okCard.state, okCard.imageId || null, okCard.cancelling || null, okCard.note || null];

// A finished picture: only a real PNG in base64 is shown, through a data: URL.
const good = { b64_png: png, width: 768, height: 768, seed: 4242 };
out.picture = C.imagePicture(good);
out.pictureSpaced = !!C.imagePicture({ b64_png: png.slice(0, 20) + '\n' + png.slice(20), width: 1, height: 1 });
out.pictureBad = [null, {}, { b64_png: 5 }, { b64_png: 'PGh0bWw+' + 'A'.repeat(100) }, { b64_png: png.slice(0, 50) + '<' + png.slice(51) },
                  { b64_png: png + '!' }, { b64_png: 'x'.repeat(30e6) }].map((i) => C.imagePicture(i));
const odd = C.imagePicture({ b64_png: png, width: 99999, height: 'x', seed: -3 });
out.oddDims = [odd.width, odd.height, odd.seed];

// The card: running, finished with the picture, finished after a reload, refused and failed.
function card_(m, picture) {
  const calls = [];
  const v = C.imageCard(m, { picture: () => picture || null, now: () => now + 23000, cancel: () => calls.push('cancel'), retry: () => calls.push('retry') });
  return { v, calls };
}
const buttons = (v) => v.row.all().filter((n) => n.tagName === 'BUTTON' && !n.hidden).map(text);
let c = card_(Object.assign({}, msg, { state: 'generating', eta: 80 }));
out.running = {
  status: text(find(c.v.row, 'imgcard-status')[0]), clock: text(find(c.v.row, 'imgcard-clock')[0]), buttons: buttons(c.v),
  pulseHidden: find(c.v.row, 'pulse')[0].hidden, imgHidden: find(c.v.row, 'imgcard-pic')[0].hidden,
  prompt: text(find(c.v.row, 'imgcard-prompt')[0]), tags: [...new Set(c.v.row.all().map((n) => n.tagName))].sort(),
  group: c.v.card.attrs.role, label: c.v.card.attrs['aria-label'], clockHidden: find(c.v.row, 'imgcard-clock')[0].attrs['aria-hidden'],
  statusRole: find(c.v.row, 'imgcard-status')[0].attrs.role,
};
const live = Object.assign({}, msg, { state: 'generating', eta: 80 });
c = card_(live);
live.state = 'reviewing';
c.v.update();
out.updatedInPlace = text(find(c.v.row, 'imgcard-status')[0]);
live.cancelling = true;
c.v.update();
out.cancelling = [text(c.v.cancel), c.v.cancel.disabled];
live.state = 'completed';
delete live.cancelling;
c.v.update();
out.noPictureYet = text(find(c.v.row, 'imgcard-status')[0]);

const done = Object.assign({}, msg, { state: 'completed' });
c = card_(done, C.imagePicture(good));
const pic = find(c.v.row, 'imgcard-pic')[0];
const save = c.v.row.all().find((n) => n.tagName === 'A');
out.finishedCard = {
  status: text(find(c.v.row, 'imgcard-status')[0]), src: pic.src.slice(0, 30), alt: pic.alt, hidden: pic.hidden, width: pic.attrs.width,
  save: save.download, saveSame: save.href === pic.src, saveHidden: save.hidden, keep: text(find(c.v.row, 'imgcard-keep')[0]),
  keepHidden: find(c.v.row, 'imgcard-keep')[0].hidden, buttons: buttons(c.v), clockHidden: find(c.v.row, 'imgcard-clock')[0].hidden,
};
c = card_(done);
out.reloaded = { status: text(find(c.v.row, 'imgcard-status')[0]), picHidden: find(c.v.row, 'imgcard-pic')[0].hidden,
                 saveHidden: c.v.row.all().find((n) => n.tagName === 'A').hidden, keep: text(find(c.v.row, 'imgcard-keep')[0]) };
c = card_(Object.assign({}, msg, { state: 'refused', note: 'I can’t make that picture.' }));
out.refusedCard = { status: text(find(c.v.row, 'imgcard-status')[0]), cls: find(c.v.row, 'imgcard-status')[0].className, buttons: buttons(c.v) };
c = card_(Object.assign({}, msg, { state: 'failed', note: '<img src=x onerror=alert(1)> broke' }));
out.failedCard = { status: text(find(c.v.row, 'imgcard-status')[0]), imgs: c.v.row.all().filter((n) => n.tagName === 'IMG').length,
                   buttons: buttons(c.v), cls: find(c.v.row, 'imgcard-status')[0].className };
c = card_(Object.assign({}, msg, { state: 'cancelled' }));
out.cancelledCard = { status: text(find(c.v.row, 'imgcard-status')[0]), cls: find(c.v.row, 'imgcard-status')[0].className };

// A saved card keeps the prompt, size and state: never bytes, never a transient flag.
const saved = JSON.parse(JSON.stringify(Object.assign({}, msg, { cancelling: true, picture: good, b64_png: png, size: '640x480', evil: '<script>' })));
out.savedActive = C.cleanMessage(saved);
out.savedDone = C.cleanMessage(Object.assign({}, saved, { state: 'completed', imageId: 'AbCdEf12_-xyz789', size: '1024x768' }));
out.savedNoId = C.cleanMessage(Object.assign({}, saved, { imageId: '../etc/passwd' }));
out.savedJunk = [C.cleanMessage({ role: 'assistant', kind: 'image', content: 'x', prompt: '' }),
                 C.cleanMessage({ role: 'assistant', kind: 'image', content: 'x', prompt: 'a cat', state: 'bogus' }),
                 C.cleanMessage({ role: 'user', kind: 'image', content: 'x', prompt: 'a cat' })];

// A normal answer keeps the note that it went without a location, and the engine's restart notice shows as a notice.
out.locationNote = C.cleanMessage({ role: 'assistant', content: 'hi', locationNote: C.NO_LOCATION_NOTE + ' '.repeat(3) });
const restart = { role: 'assistant', content: '' };
out.restart = [C.applyData(restart, JSON.stringify({ choices: [{ index: 0, delta: { content: '\n\n[DLNE is restarting, try again in a moment.]' }, finish_reason: 'stop' }] })), restart];
const restartEvent = { role: 'assistant', content: '' };
out.restartError = [C.applyData(restartEvent, JSON.stringify({ error: { message: 'DLNE is restarting, try again in a moment.' } })), restartEvent];
const half = { role: 'assistant', content: 'The answer so far' };
C.applyData(half, JSON.stringify({ choices: [{ index: 0, delta: { content: '\n\n[DLNE is restarting, try again in a moment.]' }, finish_reason: 'stop' }] }));
out.restartMidAnswer = half;
out.restartSaved = C.cleanMessage({ role: 'assistant', content: '', status: 'notice', note: 'DLNE is restarting, try again in a moment.' });
process.stdout.write(JSON.stringify(out));
"""


def _tiny_png_b64():
    import base64
    import zlib
    raw = b"".join(b"\x00" + bytes([x, 0, 255 - x]) * 16 for x in range(16))

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 16, 16, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    return base64.b64encode(png).decode()


@pytest.fixture(scope="module")
def image_run(tmp_path_factory):
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    folder = tmp_path_factory.mktemp("chat-image")
    (folder / "page.html").write_text(chat_page.CHAT_HTML, encoding="utf-8")
    (folder / "harness.js").write_text(_IMAGE_HARNESS, encoding="utf-8")
    b64 = _tiny_png_b64()
    result = subprocess.run(["node", str(folder / "harness.js"), str(folder / "page.html"), b64],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    data["b64"] = b64
    return data


def test_the_estimate_is_the_servers_own_figure_or_the_squares_scaled_by_area(image_run):
    assert image_run["eta"][0] == 153
    assert image_run["eta"][1] == pytest.approx(80 * 768 * 1024 / (768 * 768))
    assert image_run["eta"][2] == pytest.approx(80 * 1024 * 768 / (768 * 768)) and image_run["eta"][3] == 80
    assert image_run["about"] == ["80 s", "2 min 35 s", "60 s", "5 s"]
    assert image_run["seconds"] == ["0 s", "59 s", "119 s", "2 min", "2 min 5 s"]


def test_the_conversation_keeps_a_text_placeholder_with_the_prompt(image_run):
    assert image_run["placeholder"] == "[Image: a <b>red</b> fox]"


def test_the_card_words_each_state_and_shows_elapsed_time_against_the_estimate(image_run):
    lines = image_run["lines"]
    assert lines["queued"] == "Waiting in line…" and lines["checking"] == "Checking your request…"
    assert lines["generating"] == "Creating your image… about 80 s"
    assert lines["generatingBig"] == "Creating your image… about 2 min 35 s"
    assert lines["reviewing"] == "Checking the picture…" and lines["completed"] == "Your image is ready."
    assert lines["cancelled"] == "Cancelled." and lines["expired"] == "This image is no longer available."
    assert lines["failed"] == "The image couldn't be made. Please try again."
    assert lines["refused"] == "I can’t make that picture, but I’d be glad to help with something else."   # the server's words
    assert image_run["clock"] == ["23 s of about 80 s", "100 s, taking longer than expected", ""]


def test_a_poll_answer_moves_the_card_and_a_finished_job_drops_its_id(image_run):
    applied = image_run["applied"]
    assert applied[0][:2] == [True, "queued"] and applied[0][3] == 90 and applied[0][4] == "AbCdEf12_-xyz789"
    assert applied[1][:2] == [True, "generating"] and applied[1][2] == 1700000000000 - 12000   # elapsed realigns the clock
    assert applied[2][0] is False and applied[2][1] == "generating"                            # an unknown status changes nothing
    assert applied[3][:2] == [True, "refused"] and applied[3][4] is None
    assert applied[3][5] == "That is not something DLNE will draw."
    assert image_run["finished"] == ["completed", None, None, None]


def test_only_a_real_png_is_shown_and_only_through_a_data_url(image_run):
    picture = image_run["picture"]
    assert picture["src"] == "data:image/png;base64," + image_run["b64"]
    assert (picture["width"], picture["height"], picture["seed"]) == (768, 768, 4242)
    assert image_run["pictureSpaced"] is True                 # line breaks in the base64 are tolerated
    assert image_run["pictureBad"] == [None] * 7              # no image, not a PNG, bad characters, trailing junk, oversized
    assert image_run["oddDims"] == [0, 0, None]                # absurd sizes and seeds are left out, not trusted


def test_the_running_card_has_a_status_a_clock_that_is_not_read_out_and_cancel(image_run):
    running = image_run["running"]
    assert running["status"] == "Creating your image… about 80 s" and running["clock"] == "23 s of about 80 s"
    assert running["buttons"] == ["Cancel"] and running["pulseHidden"] is False and running["imgHidden"] is True
    assert running["prompt"] == "a <b>red</b> fox"                                   # the prompt is text
    assert "IMG" in running["tags"] and "B" not in running["tags"] and "SCRIPT" not in running["tags"]
    assert running["group"] == "group" and running["label"] == "Image: a <b>red</b> fox"
    assert running["statusRole"] == "status" and running["clockHidden"] == "true"     # one announcement per change, not per second
    assert image_run["updatedInPlace"] == "Checking the picture…"
    assert image_run["cancelling"] == ["Cancelling…", True]
    assert "can't be shown again after the page is reloaded" in image_run["noPictureYet"]


def test_the_finished_card_shows_the_picture_with_alt_text_a_save_link_and_says_it_is_not_kept(image_run):
    done = image_run["finishedCard"]
    assert done["status"] == "Your image is ready."
    assert done["src"] == "data:image/png;base64,iVBORw0K" and done["hidden"] is False and done["width"] == "768"
    assert done["alt"] == "Generated image: a <b>red</b> fox"
    assert done["save"] == "dlne-image-4242.png" and done["saveSame"] is True and done["saveHidden"] is False
    assert done["keep"] == "This picture isn't saved with the chat. Save it to keep it." and done["keepHidden"] is False
    assert done["buttons"] == [] and done["clockHidden"] is True
    gone = image_run["reloaded"]                                # after a reload the bytes are gone
    assert gone["picHidden"] is True and gone["saveHidden"] is True and "reloaded" in gone["status"]
    assert gone["keep"] == done["keep"]


def test_a_refusal_or_error_shows_the_servers_words_as_text_and_offers_another_go(image_run):
    assert image_run["refusedCard"] == {"status": "I can’t make that picture.", "cls": "imgcard-status error",
                                        "buttons": ["Change description"]}
    failed = image_run["failedCard"]
    assert failed["status"] == "<img src=x onerror=alert(1)> broke" and failed["buttons"] == ["Try again"]
    assert failed["cls"] == "imgcard-status error" and failed["imgs"] == 1      # only the (hidden) picture slot
    assert image_run["cancelledCard"] == {"status": "Cancelled.", "cls": "imgcard-status"}


def test_a_saved_image_card_keeps_text_only(image_run):
    active = image_run["savedActive"]
    assert active == {"role": "assistant", "kind": "image", "content": "[Image: a <b>red</b> fox]", "prompt": "a <b>red</b> fox",
                      "size": "768x768", "state": "queued", "imageId": "AbCdEf12_-xyz789", "startedAt": 1700000000000}
    done = image_run["savedDone"]
    assert done["state"] == "completed" and done["size"] == "1024x768"
    for kept in (active, done):
        assert not {"picture", "b64_png", "cancelling", "evil"} & set(kept)
    # A job without a usable id cannot be followed after a reload, so it ends as failed, with the reason.
    no_id = image_run["savedNoId"]
    assert no_id["state"] == "failed" and "imageId" not in no_id and "closed before" in no_id["note"]
    assert image_run["savedJunk"][0] is None and image_run["savedJunk"][1]["state"] == "failed"
    assert image_run["savedJunk"][2] is not None and image_run["savedJunk"][2].get("kind") is None   # a user message is never a card


def test_the_engine_restart_notice_shows_cleanly_as_a_notice(image_run):
    assert image_run["restart"][0] == "changed"
    assert image_run["restart"][1] == {"role": "assistant", "content": "", "status": "notice",
                                       "note": "DLNE is restarting, try again in a moment."}
    assert image_run["restartError"][1]["status"] == "error"
    assert image_run["restartError"][1]["note"] == "DLNE is restarting, try again in a moment."
    mid = image_run["restartMidAnswer"]
    assert mid["content"] == "The answer so far" and mid["note"] == "DLNE is restarting, try again in a moment."
    assert image_run["restartSaved"]["note"] == "DLNE is restarting, try again in a moment."   # kept though it has no text
    assert image_run["locationNote"]["locationNote"].startswith("Your location wasn't ready")


# ---------------------------------------------------------------- images: what the page is wired to do


def test_create_image_is_hidden_until_the_status_says_the_server_makes_images():
    page = chat_page.CHAT_HTML
    item = next(t for t in _tags("button") if t.get("id") == "make-image")
    assert item["type"] == "button" and item.get("aria-haspopup") == "dialog"   # still the sheet's opener, now in the "+" menu
    assert "data.capabilities.image_generation === true" in page               # only an explicit true shows it
    assert "ui.makeImage.hidden = !canPictures;" in page
    assert "const canPictures = !!imageSupport;" in page
    assert "readCapabilities(data);" in page and "readImageSupport(data);" in page
    assert not [t for t in _tags() if t.get("id") == "tools"]                   # the 2.6.1 strip above the box is gone
    assert "ui.tools" not in page


def test_the_image_sheet_takes_a_prompt_and_one_of_the_four_sizes():
    sheet = chat_page.CHAT_HTML.split('<dialog id="image"', 1)[1].split("</dialog>", 1)[0]
    assert re.findall(r'<option value="(\d+x\d+)"', sheet) == ["768x768", "1024x1024", "768x1024", "1024x768"]
    assert '<option value="768x768">' in sheet and "selected" not in sheet            # the first, 768x768, is the default
    assert re.search(r'<textarea id="image-prompt"[^>]*maxlength="1000"', sheet)
    assert 'for="image-prompt"' in sheet and 'for="image-size"' in sheet               # both fields are labelled
    assert 'id="image-status" role="alert"' in sheet                                   # errors are announced
    assert "isn't saved with your chat" in sheet                                       # the bytes are not kept: said up front too
    assert "IMAGE_SIZES = ['768x768', '1024x1024', '768x1024', '1024x768']" in chat_page.CHAT_HTML


def test_a_job_is_started_on_the_phone_endpoint_with_the_chat_id_and_never_sends_the_prompt_to_the_chat():
    page = chat_page.CHAT_HTML
    assert "const IMAGES = '/v1/app/images';" in page
    assert "body: JSON.stringify({ prompt, size, chat_id: conv.chatId })" in page
    # The server's own words are shown for a refusal, a full queue or a quota.
    assert "if (resp.ok) { try { job = await resp.json(); } catch (e) { job = null; } } else detail = await detailOf(resp);" in page
    assert "showImageStatus(imageFailure(resp, detail), 'error');" in page
    # The chat model sees only the text placeholder: the card is a message whose content is that placeholder.
    assert "content: imagePlaceholder(prompt)" in page
    history = page.split("function context(messages)", 1)[1].split("function submit(answered)", 1)[0]
    assert "wireMessages(messages" in history and "b64_png" not in history


def test_polling_is_two_seconds_in_view_and_five_in_the_background_and_resumes_after_a_reload():
    page = chat_page.CHAT_HTML
    assert "const wait = soon ? 0 : (document.visibilityState === 'visible' ? 2000 : 5000);" in page
    assert "if (document.visibilityState === 'visible') { refreshStatus(); pollAll(); }" in page
    # ... for a card made from the sheet and for the job a reply carries (2.6.2)
    assert "const job = jobOf(m); if (job && imageActive(job)) watch(job, true);" in page
    # A job that finished first still has its picture fetched, after Cancel.
    assert "if (job && job.status !== 'completed') imageApply(msg, job, Date.now());" in page
    assert "'/cancel', { method: 'POST'" in page


def test_the_picture_is_held_in_memory_only_and_shown_through_the_policys_data_images():
    page = chat_page.CHAT_HTML
    assert "const pictures = new WeakMap();" in page
    assert "JSON.stringify({ current, saved, draft })" in page and "pictures" not in page.split("function save()", 1)[1].split("function", 1)[0]
    assert "img-src 'self' data:" in chat_page.CSP and "blob:" not in chat_page.CSP   # no new source was needed
    assert "createObjectURL" not in page
    assert "prefers-reduced-motion" in page                                          # the status dot and the "+" turning honour it


def test_a_server_status_with_extra_pool_hints_does_not_change_the_status_line_code():
    """Another agent may add lanes/engine hints to app_pool; the line reads members_ready and active_model only."""
    page = chat_page.CHAT_HTML
    block = page.split("async function refreshStatus()", 1)[1].split("function setStatus", 1)[0]
    assert set(re.findall(r"pool\.(\w+)", block)) == {"members_ready", "active_model"}


def test_the_header_keeps_its_text_and_follows_the_live_model():
    """2.6.2 leaves the header alone: "<model> · N machines serving", the model being app_pool.active_model (the engine's
    tier while it answers phones), refreshed every 30 s, when the page is shown again and after each answer."""
    page = chat_page.CHAT_HTML
    assert "lastModel = typeof pool.active_model === 'string' ? pool.active_model : '';" in page
    assert "ready + (ready === 1 ? ' machine serving' : ' machines serving')" in page
    assert "setInterval(() => { if (document.visibilityState === 'visible') refreshStatus(); }, 30000);" in page
    assert "if (document.visibilityState === 'visible') { refreshStatus(); pollAll(); }" in page
    assert "        refreshStatus();\n      }\n    }\n  }\n\n  async function readStream" in page   # after each answer


# ---------------------------------------------------------------- 2.6.2: the "+" menu, photos, pictures from the chat, the location card


def test_the_plus_sits_at_the_left_of_the_box_and_opens_photos_camera_and_create_image():
    page = chat_page.CHAT_HTML
    field = page.split('<div class="field" id="field">', 1)[1].split('<textarea id="input"', 1)[0]   # what comes before the box
    plus = next(t for t in _tags("button") if t.get("id") == "plus")
    assert plus["type"] == "button" and "hidden" in plus                                   # hidden until the server can do something
    assert plus["aria-expanded"] == "false" and plus["aria-controls"] == "plus-menu"
    assert 'id="plus"' in field and 'id="plus-menu"' in field
    menu = next(t for t in _tags("div") if t.get("id") == "plus-menu")
    assert "hidden" in menu and menu["class"] == "popover"
    items = re.findall(r'<button type="button" class="popitem" id="([\w-]+)"[^>]*>([^<]+)</button>', field)
    assert items == [("add-photos", "Photos"), ("add-camera", "Camera"), ("make-image", "Create image")]
    camera = next(t for t in _tags("button") if t.get("id") == "add-camera")
    assert "hidden" in camera                                                              # the script shows it on touch screens only
    # It is a disclosure, not an ARIA menu (which would owe the reader arrow keys): a button that says it is open.
    assert "role=\"menu\"" not in page and "menuitem" not in page
    assert "ui.plus.setAttribute('aria-expanded', 'true');" in page and "ui.plus.setAttribute('aria-expanded', 'false');" in page
    # It closes on a choice, on Escape and on a tap anywhere else.
    assert "if (e.key === 'Escape' && plusOpen) { closePlus(); ui.plus.focus(); }" in page
    assert "if (plusOpen && !ui.plus.contains(e.target) && !ui.plusMenu.contains(e.target)) closePlus();" in page
    assert "ui.makeImage.addEventListener('click', () => { closePlus(); openImage(null); });" in page


def test_photos_come_from_two_hidden_file_inputs_png_or_jpeg_and_the_camera():
    photos = next(t for t in _tags("input") if t.get("id") == "pick-photos")
    camera = next(t for t in _tags("input") if t.get("id") == "pick-camera")
    assert photos["type"] == "file" and photos["accept"] == "image/png,image/jpeg" and "multiple" in photos and "hidden" in photos
    assert camera["type"] == "file" and camera["accept"] == "image/*" and camera["capture"] == "environment"
    assert "hidden" in camera and "multiple" not in camera and "capture" not in photos
    page = chat_page.CHAT_HTML
    assert "ui.pickPhotos.addEventListener('change', () => addPhotos(ui.pickPhotos.files));" in page
    assert "ui.pickCamera.addEventListener('change', () => addPhotos(ui.pickCamera.files));" in page
    # Choosing the same file again must still fire a change, and the list is copied before it is cleared.
    assert "const files = Array.from(list || []);" in page and "ui.pickPhotos.value = '';" in page


def test_the_plus_shows_only_what_the_server_can_do_and_camera_only_on_touch_screens():
    page = chat_page.CHAT_HTML
    assert "visionSupport = !!(data && data.capabilities && data.capabilities.app_vision === true);" in page   # missing is false
    assert "ui.plus.hidden = !canPhotos && !canPictures && !paused;" in page            # none of them: no "+"
    assert "ui.addPhotos.hidden = !canPhotos;" in page
    assert "ui.addCamera.hidden = !(canPhotos && coarse);" in page
    assert "const coarse = !!(window.matchMedia && window.matchMedia('(pointer: coarse)').matches);" in page
    assert "ui.field.classList.toggle('has-plus', !ui.plus.hidden);" in page            # the box gives the "+" its room
    assert "if (ui.plus.hidden) closePlus();" in page                                   # an open menu does not outlive its items


def test_while_pictures_are_paused_a_greyed_item_in_the_plus_links_to_the_pictures_page():
    """IMAGE_GEN_ENABLED=paused (2026-10-30): the status says images.paused, and where Create image would be the "+" holds
    "Pictures paused", a plain link to /pictures in a new tab. It is shown only while the server does not make pictures."""
    page = chat_page.CHAT_HTML
    field = page.split('<div class="field" id="field">', 1)[1].split('<textarea id="input"', 1)[0]
    item = next(t for t in _tags("a") if t.get("id") == "pictures-paused")
    assert item["href"] == "/pictures" and item["target"] == "_blank" and item["rel"] == "noopener noreferrer"
    assert "hidden" in item and item["class"] == "popitem popitem-off"
    assert re.search(r'id="make-image"[^>]*>Create image</button>\s*<a [^>]*id="pictures-paused"[^>]*>Pictures paused ', field)
    assert "picturesPaused = !!(data && data.images && data.images.paused === true);" in page   # only an explicit true
    assert "const paused = picturesPaused && !canPictures;" in page
    assert "ui.picturesPaused.hidden = !paused;" in page
    assert ": canPictures ? 'Create an image' : 'Pictures are paused');" in page
    assert "ui.picturesPaused.addEventListener('click', () => closePlus());" in page
    assert "querySelectorAll('button, a')" in page                       # a keyboard reader can land on the link too
    # The note itself is never typed into the chat: the page that explains it reads it live from the status.
    assert "graphics card is busy" not in page


def test_a_photo_is_made_ready_in_the_page_with_its_orientation_scaled_and_as_a_jpeg():
    page = chat_page.CHAT_HTML
    assert "createImageBitmap(file, { imageOrientation: 'from-image' })" in page
    assert "const img = await loadImage(await readAsDataUrl(file));" in page            # the way round, from a data: URL
    for line in ("const MAX_PHOTOS = 4;", "const PHOTO_MAX_PIXELS = 1200000;", "const PHOTO_MAX_SIDE = 2048;",
                 "const PHOTO_MAX_BYTES = 4 * 1024 * 1024;", "const PHOTO_QUALITY = 0.85;", "const PHOTO_QUALITY_LOW = 0.7;"):
        assert line in page
    assert "canvas.toDataURL('image/jpeg', quality)" in page
    assert "ctx.fillStyle = '#fff';" in page                                            # a PNG's see-through pixels would turn black
    assert "canvas.width = canvas.height = 0;" in page and "decoded.close();" in page    # a phone counts every canvas it holds
    assert "createObjectURL" not in page and "blob:" not in chat_page.CSP
    # Nothing is decoded before the file's own header says it is a photo of a size the page can take (and a photo being
    # looked at is already counted, so two choices made close together cannot pass four between them).
    assert "const PHOTO_FILE_MAX_BYTES = 64 * 1024 * 1024;" in page and "const PHOTO_FILE_MAX_PIXELS = 100 * 1000 * 1000;" in page
    adding = page.split("async function addPhotos(list) {", 1)[1].split("// A photo of the reader's, full screen", 1)[0]
    assert adding.index("preparing++;") < adding.index("await photoRefusal(file)") < adding.index("await preparePhoto(file)")
    assert "if (refusal) trayNote = refusal; else tray.push(await preparePhoto(file));" in adding
    assert "MAX_PHOTO_FILE" not in page                                                 # the one cap lives with the rest of the rules
    # At most four, and a note says so; a photo in preparation holds the send back.
    assert "const trayFull = () => tray.length + preparing >= MAX_PHOTOS;" in page
    assert "(!text && !tray.length) || preparing || busy || Date.now() < cooldownUntil" in page
    assert "(!ui.input.value.trim() && !tray.length) || preparing > 0" in page         # send is enabled with photos and no text


def test_the_tray_shows_thumbnails_with_remove_buttons_and_says_where_photos_go():
    page = chat_page.CHAT_HTML
    tray = next(t for t in _tags("div") if t.get("id") == "tray")
    assert "hidden" in tray
    assert next(t for t in _tags("ul") if t.get("id") == "tray-thumbs")["aria-label"] == "Photos to send"
    assert next(t for t in _tags("p") if t.get("id") == "tray-note")["role"] == "status"      # a failed photo is announced
    hint = re.search(r'<p class="tray-note" id="tray-hint">([^<]+)</p>', page).group(1)
    assert hint == chat_page.PHOTO_HINT == "Photos stay on DLNE's own server and are never sent to a volunteer's computer."
    assert "ui.trayHint.hidden = !tray.length && !preparing;" in page                        # shown while photos are attached
    assert "tray.forEach((photo, i) => ui.trayThumbs.appendChild(trayItem(photo, i, () => removePhoto(i))));" in page


def test_a_photo_viewer_opens_on_a_tap_and_its_image_is_made_at_run_time():
    page = chat_page.CHAT_HTML
    viewer = page.split('<dialog id="photo"', 1)[1].split("</dialog>", 1)[0]
    assert 'id="photo-box"' in viewer and 'id="close-photo"' in viewer and "<img" not in viewer   # no picture is embedded
    assert "const tap = button('thumb', undefined, () => open(photo));" in page
    assert "ui.photo.addEventListener('click', () => closeDialog(ui.photo));" in page


def test_photos_stay_in_memory_and_a_saved_chat_keeps_only_how_many_there_were():
    page = chat_page.CHAT_HTML
    assert "const photoData = new WeakMap();" in page
    saving = page.split("function save()", 1)[1].split("// ------", 1)[0]
    assert "photoData" not in saving and "tray" not in saving and "JSON.stringify({ current, saved, draft })" in saving
    assert "msg.photos = tray.length;" in page and "photoData.set(msg, tray);" in page
    assert "const PHOTO_GONE = 'Photo not kept after reload';" in page
    assert "photoStrip(count, photoData.get(m) || [], showPhoto)" in page
    assert "kept.photos = m.photos" in page                                            # cleaned on the way back
    assert page.count("localStorage") == 2                                             # no new place was added to keep anything


def test_a_refusal_about_photos_keeps_the_turn_says_why_pauses_nothing_and_never_resends_those_photos():
    page = chat_page.CHAT_HTML
    ask = page.split("async function ask(prompted) {", 1)[1].split("async function readStream", 1)[0]
    # The turn whose photos the request carries is the one a refusal is about.
    assert "? conv.messages.slice().reverse().find((m) => photoCount(m) > 0) : null;" in ask
    # The hour's limit of photos (429 image_quota) is not the rate limit: no countdown, no locked box.
    assert "if (resp.status === 429 && problem.code !== 'image_quota') {" in ask
    refusal = ask.split("} else if (photoTurn && resp.status >= 400 && resp.status < 500) {", 1)[1].split("} else {", 1)[0]
    assert "refusedPhotos.add(photoTurn);" in refusal
    assert "reply.status = 'error';" in refusal and "reply.note = problem.detail ||" in refusal   # the server's own words, under the turn
    assert "throttle" not in refusal and "cooldownUntil" not in refusal and "showBar" not in refusal   # nothing is paused
    # Later requests send "[photo]" for them (an empty list of held photos), and Try again is the reader's choice to resend.
    assert "const held = (m) => (refusedPhotos.has(m) ? [] : photoData.get(m) || []);" in page
    assert "const refusedPhotos = new WeakSet();" in page
    assert "refusedPhotos.delete(msgs[msgs.length - 1]);" in page.split("function retry()", 1)[1].split("function retryAfter", 1)[0]
    # A plain rate limit is as it was: the turn goes back to the box (photos too) and the box waits.
    throttle = page.split("function throttle(conv, reply, detail, seconds) {", 1)[1].split("function showBar", 1)[0]
    assert "if (photos && !tray.length && !preparing) { tray = photos.slice(); renderTray(); }" in throttle
    assert "cooldownUntil = Date.now() + seconds * 1000;" in throttle and "if (!(seconds > 0))" not in page
    assert "showBar('');" in page.split("function submit(answered)", 1)[1].split("function retry()", 1)[0]


def test_a_reply_that_made_a_picture_polls_it_keeps_it_per_job_and_drops_it_with_the_answer():
    page = chat_page.CHAT_HTML
    assert "if (reply.job && imageActive(reply.job) && !jobTimers.has(reply.job)) watch(reply.job, false);" in page
    assert "const jobOf = (m) => (m.kind === 'image' ? m : m.job || null);" in page
    # Every table is keyed by the job object, so any number of pictures can be coming at once.
    for table in ("const cardViews = new WeakMap();", "const pictures = new WeakMap();", "const jobTimers = new Map();",
                  "const polling = new Set();", "const firstMiss = new Map();"):
        assert table in page
    assert "cardViews.set(job, card);" in page and "v.pictureBox.appendChild(card.card);" in page
    assert "if (dropped.job) forgetJob(dropped.job);" in page                           # Try again throws the picture away too
    assert "refreshReported(target.msg);" in page and "report: (btn) => openReport(v.msg, btn)" in page
    assert "if (m.pictureNote) v.extras.appendChild(el('p', 'search-note', m.pictureNote));" in page   # a quiet note
    # Share is offered only where the browser takes a PNG file (Web Share level 2); elsewhere a picture has Save alone.
    assert "navigator.share({ files: [file]" in page and "if (!navigator.canShare({ files: [file] })) return;" in page
    assert "typeof navigator.canShare === 'function'" in page and "navigator.canShare({ files: [new File(['x'], 'dlne-image.png'" in page
    assert "share: canShare ? () => sharePicture(job) : undefined," in page and "share: canShare ? () => sharePicture(msg) : undefined," in page


def test_the_privacy_text_says_where_photos_and_pictures_go():
    page = chat_page.CHAT_HTML
    assert chat_page.PHOTO_PRIVACY == (
        "Photos you attach are sent to DLNE's own server for the answer, never to a volunteer's computer, and are not stored "
        "there. Like every message, a photo is processed by the shared model, whose layers can run on volunteers' graphics "
        "cards; those cards see only intermediate numbers, never the picture. This page keeps them in memory only, so they "
        "are gone when you reload it.")
    assert chat_page.PICTURE_PRIVACY == (
        "Pictures you ask for are made on DLNE's own server, never on a volunteer's computer. The description and the picture "
        "are checked before you see it, and it isn't saved with your chat: save it to keep it.")
    menu = page.split('<dialog id="menu"', 1)[1].split("</dialog>", 1)[0]
    privacy = menu.split('<div class="privacy">', 1)[1]
    assert re.findall(r"<p>([^<]+)</p>", privacy) == [
        chat_page.PRIVACY_NOTICE, chat_page.LOOKUP_PRIVACY, chat_page.PHOTO_PRIVACY, chat_page.PICTURE_PRIVACY, chat_page.LOCATION_PRIVACY]
    # The picture sheet tells the truth about what it costs: the free trial, except on a contributor's network.
    sheet = page.split('<dialog id="image"', 1)[1].split("</dialog>", 1)[0]
    assert "counts as one of your free chats, unless a computer on your network is contributing to DLNE" in sheet
    # The first-run notice and the line under the box are as they were.
    assert chat_page.PRIVACY_NOTICE in page and re.search(r'<p class="hint">[^<]*secret[^<]*</p>', page)


def test_the_location_card_asks_in_the_chat_with_two_answers():
    page = chat_page.CHAT_HTML
    card = next(t for t in _tags("div") if t.get("id") == "ask-location")
    assert "hidden" in card and card["role"] == "group" and card["aria-labelledby"] == "ask-location-title"
    assert re.search(r'<p class="askcard-title" id="ask-location-title">Use your location for this\?</p>', page)
    assert ('<p class="askcard-text">DLNE uses it only to look up the weather and places near you, and never keeps it.</p>'
            in page)
    # It says that "Use my location" is not a one-off: sharing stays on until the reader turns it off in the menu.
    note = re.search(r'<p class="askcard-note">([^<]+)</p>', page).group(1)
    assert note == 'Choosing "Use my location" keeps sharing on until you turn it off in the menu.'
    card_html = page.split('id="ask-location"', 1)[1].split("</form>", 1)[0]
    assert card_html.index("askcard-text") < card_html.index("askcard-note") < card_html.index("askcard-actions")
    buttons = re.findall(r'<button type="button" class="[\w ]+" id="(ask-location-\w+)">([^<]+)</button>', page)
    assert buttons == [("ask-location-yes", "Use my location"), ("ask-location-no", "Not now")]
    form = page.split('<form class="composer"', 1)[1]
    assert form.index('id="ask-location"') < form.index('id="tray"') < form.index('<div class="field"')   # above the box


def test_the_location_card_holds_a_local_question_and_remembers_not_now_with_the_chat():
    page = chat_page.CHAT_HTML
    assert ("if (!answered && locationCardWanted(text, where.on, where.available, current.declinedLocation, current.messages)) {\n"
            "      showAsk(true);\n      return;\n    }") in page
    no = page.split("ui.askNo.addEventListener('click', () => {", 1)[1].split("  });", 1)[0]
    assert "current.declinedLocation = true;" in no and "save();" in no and "submit('no');" in no
    assert "if (c.declinedLocation === true) kept.declinedLocation = true;" in page    # saved with the chat, cleaned on the way back
    # A browser that says no counts as "Not now" for the chat too: there is nothing more the card could do.
    assert "if (kind === 'denied' && !current.declinedLocation) {" in page
    assert "const PROMPT_WAIT = 60 * 1000;" in page and "ask(answered === 'yes');" in page
    assert "async function ask(prompted) {" in page
    # Stop works while the send waits for the browser's own prompt.
    assert "whenAborted(ctrl.signal)" in page
    # Another chat is not asked about this one's question; the menu's switch is as it was.
    assert page.count("showAsk(false);") >= 4
    assert 'id="share-location"' in page and "setSharing(ui.shareLocation.checked); where.touch(); });" in page
    menu_text = page.split('id="share-location"', 1)[1].split("</p>", 1)[0]
    assert "the chat asks you first" in menu_text and "only with a local question" in menu_text
    assert "choosing Use my location there turns this on, and it stays on until you turn it off here" in menu_text


def test_the_location_card_goes_away_when_the_held_words_are_deleted_or_stop_being_a_local_question():
    page = chat_page.CHAT_HTML
    # It is asked again as the words change (typing, or a chip replacing them) and the card hides when nothing is held.
    refresh = page.split("function refreshAsk() {", 1)[1].split("  }\n", 1)[0]
    assert "!ui.askLocation.hidden && !locationCardWanted(ui.input.value, where.on, where.available, current.declinedLocation, current.messages)" in refresh
    assert "showAsk(false);" in refresh
    typing = page.split("ui.input.addEventListener('input', () => {", 1)[1].split("});", 1)[0]
    assert "refreshAsk();" in typing
    chips = page.split("document.querySelectorAll('[data-prompt]')", 1)[1].split("}));", 1)[0]
    assert "refreshAsk();" in chips
    # And if an answer somehow arrives with no words left, nothing changes: sharing is not turned on with nothing to send.
    for answer in ("askYes", "askNo"):
        body = page.split(f"ui.{answer}.addEventListener('click', () => {{", 1)[1].split("  });", 1)[0]
        assert body.index("showAsk(false);") < body.index("if (!ui.input.value.trim()) return;")
    yes = page.split("ui.askYes.addEventListener('click', () => {", 1)[1].split("  });", 1)[0]
    assert yes.index("if (!ui.input.value.trim()) return;") < yes.index("setSharing(true);")


# ---------------------------------------------------------------- 2.6.2 under node


_PHOTO_HARNESS = _NODE_PRELUDE + r"""
const out = {};
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const png = process.argv[3];
const text = (node) => node.textContent;
const JPEG = (tag) => 'data:image/jpeg;base64,' + tag.padEnd(8, 'A');
const photo = (tag) => ({ src: JPEG(tag), thumb: JPEG('t' + tag), width: 10, height: 10 });

(async () => {
  // ---- the sizes: never enlarged, at most 1.2 million pixels and 2,048 a side, in the photo's own shape
  out.sizes = [[4000, 3000], [1000, 800], [1200, 1000], [4000, 300], [300, 4000], [1, 5000], [2048, 2048], [8000, 6000], [1201, 1000]]
    .map(([w, h]) => C.photoSize(w, h));
  let bad = 0;
  let seed = 12345;
  const rnd = () => { seed = (seed * 1103515245 + 12345) & 0x7fffffff; return seed / 0x7fffffff; };
  for (let i = 0; i < 3000; i++) {
    const w = 1 + Math.floor(rnd() * 12000);
    const h = 1 + Math.floor(rnd() * 12000);
    const s = C.photoSize(w, h);
    const t = C.thumbSize(w, h);
    const shaped = s.width < 60 || s.height < 60 || Math.abs(s.width / s.height - w / h) / (w / h) < 0.02;
    if (s.width * s.height > 1200000 || Math.max(s.width, s.height) > 2048 || s.width > w || s.height > h || s.width < 1 || s.height < 1 || !shaped) bad++;
    if (t.width * t.height > 57600 || Math.max(t.width, t.height) > 240 || t.width > w || t.height > h || t.width < 1 || t.height < 1) bad++;
  }
  out.badSizes = bad;
  out.oddSizes = [[0, 0], [NaN, 10], [-5, 10], ['x', 'y'], [Infinity, 3]].map(([w, h]) => C.photoSize(w, h));
  out.bytes = ['data:image/jpeg;base64,QUJD', 'data:image/jpeg;base64,QUI=', 'data:image/jpeg;base64,QQ==', '', null].map(C.dataUrlBytes);

  // ---- the encoding loop, with a stand-in for the canvas
  const url = JPEG('x');
  function rig(bytesFor, kind) {
    const calls = [];
    const draw = (w, h, q) => {
      calls.push([w, h, q]);
      const made = { url: kind === 'png' ? 'data:image/png;base64,AAAAAAAA' : url, bytes: bytesFor(w, h, q) };
      return kind === 'async' ? Promise.resolve(made) : made;
    };
    return { calls, draw };
  }
  let r = rig(() => 5000);
  let done = await C.fitPhoto(4000, 3000, r.draw);
  out.fitSmall = { calls: r.calls, size: [done.width, done.height], bytes: done.bytes, src: done.src === url, thumb: done.thumb === url };
  r = rig(() => 4 * 1024 * 1024);
  done = await C.fitPhoto(4000, 3000, r.draw);
  out.fitExactly4MiB = r.calls.length;                                    // 4 MiB fits: one try and the thumbnail
  r = rig(() => 4 * 1024 * 1024 + 1);
  await C.fitPhoto(100, 100, r.draw).catch(() => null);
  out.fitOver = r.calls[1];                                               // one byte over: quality 70 next
  r = rig((w, h, q) => (q > 0.8 ? 5e6 : 1e6));
  done = await C.fitPhoto(4000, 3000, r.draw);
  out.fitQuality = { calls: r.calls, size: [done.width, done.height] };
  r = rig((w, h) => (w * h > 400000 ? 5e6 : 1e6));
  done = await C.fitPhoto(4000, 3000, r.draw);
  out.fitHalved = { calls: r.calls, size: [done.width, done.height] };
  r = rig((w, h) => (w * h > 100000 ? 5e6 : 1e6));      // still over after one halving: each side halves again, a quarter of the pixels each time
  done = await C.fitPhoto(4000, 3000, r.draw);
  out.fitQuartered = { calls: r.calls, size: [done.width, done.height] };
  r = rig(() => 5e6);
  done = await C.fitPhoto(4000, 3000, r.draw);
  out.fitNever = { n: r.calls.length, size: [done.width, done.height] };
  r = rig(() => 3e6, 'async');
  done = await C.fitPhoto(640, 480, r.draw);
  out.fitAsync = { calls: r.calls, size: [done.width, done.height] };
  r = rig(() => 5000, 'png');
  try { await C.fitPhoto(100, 100, r.draw); out.fitPng = 'resolved'; } catch (e) { out.fitPng = String(e.message); }

  // ---- what a request carries
  const U = (content, photos) => (photos ? { role: 'user', content, photos } : { role: 'user', content });
  const A = (content, job) => (job ? { role: 'assistant', content, job } : { role: 'assistant', content });
  const held = new Map();
  const hold = (m, n, tag) => { held.set(m, Array.from({ length: n }, (_, i) => photo(tag + i))); return m; };
  const wire = (msgs, budget) => C.wireMessages(msgs, (m) => held.get(m) || [], budget === undefined ? 24000 : budget);
  const shape = (list) => list.map((m) => [m.role, typeof m.content === 'string' ? m.content
    : m.content.map((p) => (p.type === 'text' ? 'T:' + p.text : 'I:' + p.image_url.url.slice(23, 25)))]);

  // text alone is exactly what 2.6.1 sent (a reference copy of its context function)
  function oldContext(messages, budget) {
    const picked = [];
    let left = budget;
    for (let i = messages.length - 1; i >= 0; i--) {
      const t = (messages[i].content || '').trim();
      if (!t) continue;
      if (picked.length && t.length > left) break;
      left -= t.length;
      picked.unshift({ role: messages[i].role, content: t });
    }
    while (picked.length > 1 && picked[0].role !== 'user') picked.shift();
    return picked;
  }
  const histories = [
    [U('hi')], [U('hi'), A('hello'), U('how are you')], [U('a'), A('b'), U(' '), A(''), U('c  ')], [A('stray'), U('x'), A('y')],
    [U('x'.repeat(50)), A('y'.repeat(50)), U('z'.repeat(50))], [U('old '.repeat(30)), A('mid'), U('new'), A('')],
    [U('draw'), { role: 'assistant', kind: 'image', content: '[Image: a cat]' }, U('make it blue')],
  ];
  out.sameAsBefore = histories.flatMap((h) => [24000, 120, 60, 5].map((b) => JSON.stringify(wire(h, b)) === JSON.stringify(oldContext(h, b))));

  const t1 = hold(U('what is this?', 2), 2, 'a');
  out.newestPhotoTurn = shape(wire([U('hi'), A('hello'), t1, A('A cat.'), U('and the left one?')]));
  const o1 = hold(U('first', 2), 2, 'b');
  const o2 = hold(U('second', 1), 1, 'c');
  out.twoPhotoTurns = shape(wire([o1, A('one'), o2, A('two'), U('now?')]));
  out.photosAlone = shape(wire([hold(U('', 2), 2, 'd')]));
  out.lostPhotos = shape(wire([U('look', 2), A('?')]));
  out.lostPhotoAlone = shape(wire([U('', 1)]));
  out.olderPhotosAlone = shape(wire([hold(U('', 3), 3, 'i'), A('three'), hold(U('', 1), 1, 'j')]));
  // a newest photo turn the server refused is held no more (the page hands over an empty list for it): "[photo]", and the older
  // turn's photos stay "[photo]" too, since only the newest turn that has photos ever sends them
  const refusedTurn = U('look', 2);
  out.refusedTurn = shape(wire([hold(U('before', 1), 1, 'k'), A('ok'), refusedTurn, A(''), U('and now?')]));
  out.capped = shape(wire([hold(U('six', 6), 6, 'e')]));
  out.partial = shape(wire([hold(U('p', 3), 2, 'f')]));
  out.budget = shape(wire([U('old'), A('mid'), hold(U('x'.repeat(50), 1), 1, 'g')], 10));
  out.freePictures = shape(wire([U('q'.repeat(10)), A('a'.repeat(10)), hold(U('', 4), 4, 'h')], 20)).map((m) => m[0]);
  out.firstIsUser = shape(wire([A('stray'), U('x'), A('y')]));
  const job = { prompt: 'a red fox', size: '768x768', state: 'completed' };
  out.pictureText = shape(wire([U('draw a fox'), A('Here you go.', job), U('bluer')]));
  out.pictureOnly = shape(wire([U('draw a fox'), A('', job), U('bluer')]));
  out.refusedAddsNothing = shape(wire([U('draw x'), Object.assign(A('No can do.'), { pictureNote: 'refused words' }), U('ok')]));
  out.turnText = [C.turnText(U('hi'), 0), C.turnText(U('hi', 2), 0), C.turnText(U('hi', 2), 2), C.turnText(U('', 1), 0),
                  C.turnText(A('t', job), 0), C.turnText(A(' ', job), 0), C.turnText(A('t'), 0)];
  out.pictureStates = {};
  ['queued', 'checking', 'generating', 'reviewing', 'completed', 'refused', 'failed', 'cancelled', 'expired'].forEach((state) => {
    out.pictureStates[state] = C.turnText(A('t', Object.assign({}, job, { state })), 0).indexOf('[Picture created') !== -1;
  });
  const answer = A('Here.', job);
  out.report = C.reportPayload([U('', 2), answer], answer, 'wrong', '');

  // ---- the two events, alone and inside a stream
  const ev = (event) => JSON.stringify({ dlne: event });
  let reply = { role: 'assistant', content: '' };
  const got = C.applyData(reply, ev({ event: 'image_job', id: 'job001-abcdefgh', prompt: '  a <b>red</b>   fox  ', size: '768x1024', position: 1, eta_seconds: 90 }));
  const stamp = reply.job.startedAt;
  delete reply.job.startedAt;
  out.job = { got, job: reply.job, startedNow: Math.abs(stamp - Date.now()) < 5000 };
  out.secondJob = [C.applyData(reply, ev({ event: 'image_job', id: 'other-job-12345', prompt: 'x', size: '768x768' })), reply.job.imageId];
  out.badIds = [{ id: '../etc/passwd' }, { id: 'short' }, { id: 5 }, {}, { id: 'a'.repeat(200) }].map((e) => {
    const rr = { role: 'assistant', content: '' };
    return [C.applyData(rr, ev(Object.assign({ event: 'image_job', prompt: 'p', size: '768x768' }, e))), 'job' in rr];
  });
  const odd = { role: 'assistant', content: '' };
  C.applyData(odd, ev({ event: 'image_job', id: 'job002-abcdefgh', prompt: '', size: '5x5', eta_seconds: 'soon' }));
  delete odd.job.startedAt;
  out.oddJob = odd.job;
  out.etas = [0, -4, 4000, 6.5].map((eta) => {
    const rr = { role: 'assistant', content: '' };
    C.applyData(rr, ev({ event: 'image_job', id: 'job002-abcdefgh', prompt: 'p', size: '768x768', eta_seconds: eta }));
    return rr.job.eta === undefined ? null : rr.job.eta;
  });
  const refused = { role: 'assistant', content: '' };
  out.refused = [C.applyData(refused, ev({ event: 'image_refused', code: 'prompt_refused', detail: '  That is not   something DLNE will draw.  ' })), refused];
  const blank = { role: 'assistant', content: '' };
  C.applyData(blank, ev({ event: 'image_refused', code: 'x' }));
  out.refusedBlank = blank.pictureNote;
  const markup = { role: 'assistant', content: '' };
  C.applyData(markup, ev({ event: 'image_refused', detail: '<img src=x onerror=alert(1)>' }));
  out.refusedMarkup = markup.pictureNote;

  const streamEvents = [
    { dlne: { event: 'image_job', id: 'job003-abcdefgh', prompt: 'a fox', size: '768x768', position: null, eta_seconds: null } },
    { choices: [{ index: 0, delta: { content: 'Sure! ' }, finish_reason: null }] },
    { dlne: { event: 'retract' } },
    { choices: [{ index: 0, delta: { content: 'On its way.' }, finish_reason: null }] },
    { choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] },
  ];
  const wireBytes = new TextEncoder().encode(streamEvents.map((e) => 'data: ' + JSON.stringify(e) + '\n\n').join('') + 'data: [DONE]\n\n');
  out.streams = [1, 3, 7, 64, wireBytes.length].map((size) => {
    const rr = { role: 'assistant', content: '' };
    const reader = C.streamReader((data) => C.applyData(rr, data) !== 'done');
    for (let i = 0; i < wireBytes.length; i += size) reader.push(wireBytes.slice(i, i + size));
    reader.end();
    delete rr.job.startedAt;
    return rr;
  });

  // ---- saved chats keep a count of photos and a job's words, never bytes
  out.cleanUser = [C.cleanMessage({ role: 'user', content: 'hi', photos: 2, src: 'x', thumb: 'y', extra: 1 }), C.cleanMessage({ role: 'user', content: '', photos: 1 })];
  out.badCounts = [0, 5, '2', 1.5, -1, null, true].map((p) => 'photos' in C.cleanMessage({ role: 'user', content: 'x', photos: p }));
  out.cleanJob = C.cleanMessage({ role: 'assistant', content: 'Sure', pictureNote: 'note',
    job: { prompt: 'a fox', size: '1024x768', state: 'generating', imageId: 'job003-abcdefgh', startedAt: 1700000000000, eta: 80, cancelling: true, b64_png: 'AAAA' } });
  out.cleanNoId = C.cleanMessage({ role: 'assistant', content: 'Sure', job: { prompt: 'a fox', state: 'queued' } }).job;
  out.cleanNoPrompt = C.cleanMessage({ role: 'assistant', content: 'Sure', job: { prompt: '', state: 'queued', imageId: 'job003-abcdefgh' } });
  out.cleanOnlyJob = C.cleanMessage({ role: 'assistant', content: '', job: { prompt: 'a fox', state: 'completed' } });
  out.cleanOnlyNote = C.cleanMessage({ role: 'assistant', content: '', pictureNote: 'refused' });
  out.cleanNothing = C.cleanMessage({ role: 'assistant', content: '' });
  out.cleanUserJob = C.cleanMessage({ role: 'user', content: 'hi', job: { prompt: 'x', state: 'queued' } });
  out.cleanCard = C.cleanMessage({ role: 'assistant', kind: 'image', prompt: 'a cat', state: 'completed', content: 'x' });

  // ---- thumbnails: nodes and text only
  const p0 = photo('p0');
  const p2 = photo('p2');
  const opened = [];
  const strip = C.photoStrip(3, [p0, undefined, p2], (p) => opened.push(p.src.slice(23, 25)));
  const kids = strip.childNodes;
  kids[0].click();
  kids[2].click();
  out.strip = { cls: strip.className, kids: kids.map((k) => [k.tagName, k.className, k.textContent]), opened,
                imgs: strip.all().filter((n) => n.tagName === 'IMG').map((i) => [i.src.slice(23, 26), i.alt]),
                labels: kids.filter((k) => k.attrs['aria-label']).map((k) => k.attrs['aria-label']) };
  out.strip1 = C.photoStrip(1, [p0], () => {}).className;
  out.gone = C.photoStrip(2, [], () => {}).childNodes.map((k) => [k.tagName, k.className, k.textContent]);
  const removed = [];
  const item = C.trayItem(p0, 1, () => removed.push('removed'));
  item.all().find((n) => n.tagName === 'BUTTON').click();
  const parts = item.all();
  out.item = { tag: item.tagName, cls: item.className, kids: item.childNodes.map((k) => [k.tagName, k.className]), alt: parts[0].alt,
               src: parts[0].src.slice(23, 26), label: parts[1].attrs['aria-label'], text: parts[1].textContent, removed };

  // ---- the location card
  const local = [U('weather near me')];
  out.ask = [C.shouldAskLocation(false, true, false, local), C.shouldAskLocation(true, true, false, local), C.shouldAskLocation(false, false, false, local),
             C.shouldAskLocation(false, true, true, local), C.shouldAskLocation(false, true, false, [U('write a poem')]),
             C.shouldAskLocation(false, true, false, [U('weather near me'), A('Sunny.'), U('and tomorrow?')]),
             C.shouldAskLocation(false, true, false, [U('weather near me'), A('Sunny.'), U('thanks'), A('Welcome.'), U('a joke')])];
  out.available = [C.locator({ getCurrentPosition() {} }, () => 0, () => {}, 30).available, C.locator(undefined, () => 0, () => {}, 30).available];
  const clockNow = 1700000000000;
  const lateGeo = { callbacks: [], getCurrentPosition(ok) { this.callbacks.push(ok); } };
  const late = C.locator(lateGeo, () => clockNow, () => {}, 30);
  late.enable(true);
  late.touch();
  const usual = await late.forSend();             // waits its 30 ms and goes without
  const patient = late.forSend(5000);             // the send that just turned sharing on waits for the reader
  await sleep(150);
  lateGeo.callbacks[0]({ coords: { latitude: 1, longitude: 2 }, timestamp: clockNow });
  out.patient = { usual, patient: await patient, asked: lateGeo.callbacks.length };

  // ---- an image card inside a reply
  const now = 1700000000000;
  const pic = C.imagePicture({ b64_png: png, width: 192, height: 192, seed: 7 });
  const calls = [];
  const ctx = (extra) => Object.assign({ picture: () => pic, now: () => now, cancel: () => calls.push('cancel'), retry: () => calls.push('retry') }, extra || {});
  const visible = (v) => v.row.all().filter((n) => (n.tagName === 'BUTTON' || n.tagName === 'A') && !n.hidden).map(text);
  const finished = { prompt: 'a fox', size: '768x768', state: 'completed' };
  let card = C.imageCard(finished, ctx(), true);
  out.embedded = { same: card.row === card.card, cls: card.card.className, plain: visible(card) };
  let reported = false;
  card = C.imageCard(finished, ctx({ share: () => calls.push('share'), report: (b) => calls.push('report:' + b.textContent), reported: () => reported }), true);
  out.actions = visible(card);
  const buttons = () => card.row.all().filter((n) => n.tagName === 'BUTTON');
  buttons().find((b) => b.textContent === 'Share').click();
  buttons().find((b) => b.textContent === 'Report').click();
  reported = true;
  card.update();
  const again = buttons().find((b) => /Report/.test(b.textContent));
  out.afterReport = { calls: calls.slice(), text: again.textContent, disabled: again.disabled, popup: again.attrs['aria-haspopup'] };
  const running = C.imageCard({ prompt: 'a fox', size: '768x768', state: 'generating', startedAt: now }, ctx({ picture: () => null, share: () => {}, report: () => {} }), true);
  out.running = visible(running);
  const gone = C.imageCard(finished, ctx({ picture: () => null, share: () => {}, report: () => {} }), true);
  out.afterReload = visible(gone);
  const alone = C.imageCard(finished, ctx(), false);
  out.standalone = [alone.row !== alone.card, alone.row.className];
  const bytes = C.pictureBytes({ src: 'data:image/png;base64,' + png });
  out.pictureBytes = [bytes.length, Array.from(bytes.slice(0, 8)), Buffer.from(png, 'base64').length, bytes instanceof Uint8Array];
  process.stdout.write(JSON.stringify(out));
})();
"""


@pytest.fixture(scope="module")
def photo_run(tmp_path_factory):
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    folder = tmp_path_factory.mktemp("chat-photo")
    (folder / "page.html").write_text(chat_page.CHAT_HTML, encoding="utf-8")
    (folder / "harness.js").write_text(_PHOTO_HARNESS, encoding="utf-8")
    result = subprocess.run(["node", str(folder / "harness.js"), str(folder / "page.html"), _tiny_png_b64()],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_a_photo_is_scaled_to_the_contracts_limits_and_never_enlarged(photo_run):
    assert photo_run["sizes"] == [
        {"width": 1264, "height": 948},       # 12 MP: area is the limit, 4:3 kept
        {"width": 1000, "height": 800},       # small enough: untouched
        {"width": 1200, "height": 1000},      # exactly 1.2 MP: untouched
        {"width": 2048, "height": 153},       # wide: the side is the limit
        {"width": 153, "height": 2048},
        {"width": 1, "height": 2048},         # a sliver keeps at least a pixel
        {"width": 1095, "height": 1095},      # 2048 square is 4.2 MP: area is the limit again
        {"width": 1264, "height": 948},
        {"width": 1200, "height": 999},
    ]
    assert photo_run["badSizes"] == 0         # 3,000 random sizes: both limits kept, never grown, shape kept
    # A side that is not a size (zero, negative, NaN, text, infinity) counts as a single pixel, never as NaN.
    assert photo_run["oddSizes"] == [{"width": 1, "height": 1}, {"width": 1, "height": 10}, {"width": 1, "height": 10},
                                     {"width": 1, "height": 1}, {"width": 1, "height": 3}]
    assert photo_run["bytes"] == [3, 2, 1, 0, 0]


def test_a_photo_is_tried_at_85_then_70_then_half_the_size_until_it_fits_in_4_mib(photo_run):
    small = photo_run["fitSmall"]
    assert small["calls"][0] == [1264, 948, 0.85] and len(small["calls"]) == 2          # the photo, then its thumbnail
    thumb = small["calls"][1]
    assert thumb[2] == 0.8 and max(thumb[:2]) <= 240 and thumb[0] * thumb[1] <= 240 * 240
    assert small["size"] == [1264, 948] and small["bytes"] == 5000 and small["src"] and small["thumb"]
    assert photo_run["fitExactly4MiB"] == 2                                            # exactly 4 MiB fits
    assert photo_run["fitOver"] == [100, 100, 0.7]                                     # one byte over: quality 70 on the same size
    assert photo_run["fitQuality"]["calls"][:2] == [[1264, 948, 0.85], [1264, 948, 0.7]]
    assert photo_run["fitQuality"]["size"] == [1264, 948] and len(photo_run["fitQuality"]["calls"]) == 3
    assert photo_run["fitHalved"]["calls"][:3] == [[1264, 948, 0.85], [1264, 948, 0.7], [632, 474, 0.7]]
    assert photo_run["fitHalved"]["size"] == [632, 474]
    # "Halve the size" is each side (a quarter of the pixels): 1264x948, 632x474, 316x237, always at quality 70 after the first two tries.
    assert photo_run["fitQuartered"]["calls"][:4] == [[1264, 948, 0.85], [1264, 948, 0.7], [632, 474, 0.7], [316, 237, 0.7]]
    assert photo_run["fitQuartered"]["size"] == [316, 237]
    assert photo_run["fitNever"]["size"] == [1, 1] and photo_run["fitNever"]["n"] < 30  # it ends, even for an impossible encoder
    assert photo_run["fitAsync"] == {"calls": [[640, 480, 0.85], [240, 180, 0.8]], "size": [640, 480]}
    assert "did not make a JPEG" in photo_run["fitPng"]                                 # a browser that wrote a PNG is refused


def test_text_alone_is_sent_exactly_as_before(photo_run):
    assert photo_run["sameAsBefore"] == [True] * 28


def test_only_the_newest_turn_with_photos_sends_them_and_every_other_photo_is_the_text_photo(photo_run):
    assert photo_run["newestPhotoTurn"] == [
        ["user", "hi"], ["assistant", "hello"], ["user", ["T:what is this?", "I:a0", "I:a1"]],
        ["assistant", "A cat."], ["user", "and the left one?"]]                        # a text follow-up still carries them
    assert photo_run["twoPhotoTurns"] == [
        ["user", "first\n\n[photo] [photo]"], ["assistant", "one"], ["user", ["T:second", "I:c0"]],
        ["assistant", "two"], ["user", "now?"]]
    assert photo_run["photosAlone"] == [["user", ["I:d0", "I:d1"]]]                    # no text part for photos alone
    assert photo_run["lostPhotos"] == [["user", "look\n\n[photo] [photo]"], ["assistant", "?"]]   # after a reload
    assert photo_run["lostPhotoAlone"] == [["user", "[photo]"]]
    assert photo_run["olderPhotosAlone"] == [["user", "[photo] [photo] [photo]"], ["assistant", "three"], ["user", ["I:j0"]]]
    assert photo_run["refusedTurn"] == [["user", "before\n\n[photo]"], ["assistant", "ok"],
                                        ["user", "look\n\n[photo] [photo]"], ["user", "and now?"]]   # nothing is sent again
    assert photo_run["capped"] == [["user", ["T:six", "I:e0", "I:e1", "I:e2", "I:e3"]]]          # never more than four
    assert photo_run["partial"] == [["user", ["T:p\n\n[photo]", "I:f0", "I:f1"]]]      # one of three is not held: it is text
    assert photo_run["budget"] == [["user", ["T:" + "x" * 50, "I:g0"]]]                # the newest turn goes however long it is
    assert photo_run["freePictures"] == ["user", "assistant", "user"]                  # pictures cost no characters here


def test_a_reply_that_made_a_picture_goes_back_with_its_description_and_a_refused_one_adds_nothing(photo_run):
    assert photo_run["pictureText"] == [
        ["user", "draw a fox"], ["assistant", "Here you go.\n\n[Picture created: a red fox]"], ["user", "bluer"]]
    assert photo_run["pictureOnly"][1] == ["assistant", "[Picture created: a red fox]"]
    assert photo_run["refusedAddsNothing"][1] == ["assistant", "No can do."]
    assert photo_run["firstIsUser"] == [["user", "x"], ["assistant", "y"]]
    assert photo_run["turnText"] == ["hi", "hi\n\n[photo] [photo]", "hi", "[photo]", "t\n\n[Picture created: a red fox]",
                                     "[Picture created: a red fox]", "t"]
    # A picture that is being made or was made is "created"; one that failed, was cancelled, refused or expired is not.
    states = photo_run["pictureStates"]
    assert [s for s, line in states.items() if line] == ["queued", "checking", "generating", "reviewing", "completed"]
    assert [s for s, line in states.items() if not line] == ["refused", "failed", "cancelled", "expired"]
    # A report reads a turn as the model did: photos as [photo], a picture as its description.
    assert photo_run["report"] == {"question": "[photo] [photo]", "answer": "Here.\n\n[Picture created: a red fox]",
                                   "reason": "wrong", "note": ""}


def test_an_image_job_event_gives_the_reply_a_job_card_and_a_second_one_is_ignored(photo_run):
    job = photo_run["job"]
    assert job["got"] == "changed" and job["startedNow"] is True
    assert job["job"] == {"prompt": "a <b>red</b> fox", "size": "768x1024", "state": "queued", "imageId": "job001-abcdefgh", "eta": 90}
    assert photo_run["secondJob"] == ["skip", "job001-abcdefgh"]                       # one picture per answer
    assert photo_run["badIds"] == [["skip", False]] * 5                                # no usable id, no job: never polled
    assert photo_run["oddJob"] == {"prompt": "A picture", "size": "768x768", "state": "queued", "imageId": "job002-abcdefgh"}
    assert photo_run["etas"] == [None, None, None, 6.5]                                # an estimate that is not one is left out


def test_an_image_refused_event_is_a_quiet_note_in_the_servers_words(photo_run):
    assert photo_run["refused"] == ["changed", {"role": "assistant", "content": "",
                                                "pictureNote": "That is not something DLNE will draw."}]
    assert photo_run["refusedBlank"] == "The picture couldn't be made."
    assert photo_run["refusedMarkup"] == "<img src=x onerror=alert(1)>"                # kept as text; shown through textContent


def test_the_picture_events_survive_any_split_of_the_stream_and_a_retract_keeps_the_job(photo_run):
    first = photo_run["streams"][0]
    assert all(run == first for run in photo_run["streams"])
    assert first["content"] == "On its way."                                          # the retract emptied "Sure! " only
    assert first["job"] == {"prompt": "a fox", "size": "768x768", "state": "queued", "imageId": "job003-abcdefgh"}


def test_a_saved_chat_keeps_how_many_photos_and_a_pictures_words_never_bytes(photo_run):
    assert photo_run["cleanUser"] == [{"role": "user", "content": "hi", "photos": 2}, {"role": "user", "content": "", "photos": 1}]
    assert photo_run["badCounts"] == [False] * 7                                       # 0, 5, "2", 1.5, -1, null, true
    assert photo_run["cleanJob"] == {
        "role": "assistant", "content": "Sure", "pictureNote": "note",
        "job": {"prompt": "a fox", "size": "1024x768", "state": "generating", "imageId": "job003-abcdefgh",
                "startedAt": 1700000000000, "eta": 80}}
    no_id = photo_run["cleanNoId"]                                                     # it cannot be followed after a reload
    assert no_id["state"] == "failed" and "imageId" not in no_id and "closed before" in no_id["note"]
    assert photo_run["cleanNoPrompt"] == {"role": "assistant", "content": "Sure"}
    assert photo_run["cleanOnlyJob"]["job"]["state"] == "completed"                    # a reply of only a picture is a reply
    assert photo_run["cleanOnlyNote"]["pictureNote"] == "refused"
    assert photo_run["cleanNothing"] is None
    assert "job" not in photo_run["cleanUserJob"]                                      # the reader's own turn never carries one
    assert photo_run["cleanCard"]["kind"] == "image" and photo_run["cleanCard"]["content"] == "[Image: a cat]"


def test_thumbnails_are_built_from_nodes_with_the_not_kept_box_for_photos_that_were_not_held(photo_run):
    strip = photo_run["strip"]
    assert strip["cls"] == "thumbs n3"
    assert strip["kids"] == [["BUTTON", "thumb", ""], ["DIV", "thumb gone", "Photo not kept after reload"], ["BUTTON", "thumb", ""]]
    assert strip["opened"] == ["p0", "p2"]                                             # a tap hands over that photo
    assert strip["imgs"] == [["tp0", "Your photo 1 of 3"], ["tp2", "Your photo 3 of 3"]]
    assert strip["labels"] == ["Open photo 1 of 3", "Open photo 3 of 3"]
    assert photo_run["strip1"] == "thumbs n1"                                         # a lone photo is shown larger
    assert photo_run["gone"] == [["DIV", "thumb gone", "Photo not kept after reload"]] * 2
    item = photo_run["item"]
    assert item["tag"] == "LI" and item["cls"] == "tray-item" and item["kids"] == [["IMG", ""], ["BUTTON", "tray-remove"]]
    assert item["alt"] == "Photo 2" and item["src"] == "tp0" and item["label"] == "Remove photo 2" and item["text"] == "×"
    assert item["removed"] == ["removed"]


def test_the_card_asks_only_for_a_local_question_with_sharing_off_in_a_chat_not_yet_declined(photo_run):
    assert photo_run["ask"] == [True, False, False, False, False, True, False]
    assert photo_run["available"] == [True, False]                                    # a browser with no geolocation is never asked about it
    # The send that just turned sharing on waits as long as the reader needs; the usual send gives up after its short wait.
    assert photo_run["patient"] == {"usual": None, "patient": {"lat": 1, "lon": 2}, "asked": 1}


def test_a_picture_in_a_reply_has_save_share_and_report_and_only_with_a_picture(photo_run):
    assert photo_run["embedded"] == {"same": True, "cls": "imgcard", "plain": ["Save"]}   # no row of its own; no extras unasked
    assert photo_run["actions"] == ["Save", "Share", "Report"]
    assert photo_run["afterReport"] == {"calls": ["share", "report:Report"], "text": "Reported", "disabled": True, "popup": "dialog"}
    assert photo_run["running"] == ["Cancel"]                                          # nothing to share or report yet
    assert photo_run["afterReload"] == []                                              # after a reload: the picture is gone, and so are its actions
    assert photo_run["standalone"] == [True, "msg assistant"]                          # the sheet's card is as it was
    size, signature, expected, is_bytes = photo_run["pictureBytes"]
    assert size == expected and signature == [137, 80, 78, 71, 13, 10, 26, 10] and is_bytes is True


# A file is judged by its header before anything is decoded. The fakes below are only that: a PNG that is its signature and
# its IHDR chunk, a JPEG that is segments up to its frame header. No decoder could open them; the page must never try.
_HEADER_HARNESS = _NODE_PRELUDE + r"""
const out = {};
const refuse = (f) => C.photoRefusal(f);
const concat = (parts) => {
  const all = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  parts.forEach((p) => { all.set(p, at); at += p.length; });
  return all;
};
function pngHeader(width, height) {
  const b = new Uint8Array(33);
  const v = new DataView(b.buffer);
  b.set([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A], 0);
  v.setUint32(8, 13);
  b.set([0x49, 0x48, 0x44, 0x52], 12);
  v.setUint32(16, width);
  v.setUint32(20, height);
  b[24] = 8; b[25] = 2;
  return b;
}
function segment(marker, size) {
  const b = new Uint8Array(4 + size);
  b[0] = 0xFF; b[1] = marker;
  new DataView(b.buffer).setUint16(2, size + 2);
  return b;
}
function jpegHeader(width, height, options) {
  options = options || {};
  const frame = segment(options.sof || 0xC0, 15);        // precision, height, width, three components
  const v = new DataView(frame.buffer);
  frame[4] = 8; v.setUint16(5, height); v.setUint16(7, width); frame[9] = 3;
  return concat([Uint8Array.of(0xFF, 0xD8)].concat(options.before || [], [frame]));
}
const file = (bytes, type) => new Blob([bytes], { type });
const spy = (blob) => {
  const reads = [];
  return { type: blob.type, size: blob.size, reads, slice(a, b) { reads.push([a, b]); return blob.slice(a, b); } };
};
const text = (s) => new TextEncoder().encode(s);

(async () => {
  const app1 = segment(0xE1, 60000);     // an EXIF block's size: skipped by its length, never read
  const icc = segment(0xE2, 3000);
  const dqt = segment(0xDB, 65);
  const dht = segment(0xC4, 30);         // a Huffman table is no frame header, whatever its marker looks like

  out.png = {
    ok: await refuse(file(pngHeader(4000, 3000), 'image/png')),
    exactly: await refuse(file(pngHeader(10000, 10000), 'image/png')),
    over: await refuse(file(pngHeader(10000, 10001), 'image/png')),
    huge: await refuse(file(pngHeader(20000, 20000), 'image/png')),
    most: await refuse(file(pngHeader(4294967295, 4294967295), 'image/png')),
    noType: await refuse(file(pngHeader(100, 100), '')),
    zero: await refuse(file(pngHeader(0, 100), 'image/png')),
  };
  out.jpeg = {
    ok: await refuse(file(jpegHeader(4000, 3000, { before: [app1, icc, dqt] }), 'image/jpeg')),
    exactly: await refuse(file(jpegHeader(10000, 10000, { before: [app1] }), 'image/jpeg')),
    over: await refuse(file(jpegHeader(10000, 10001, { before: [app1] }), 'image/jpeg')),
    huge: await refuse(file(jpegHeader(65535, 65535, { before: [app1, icc, dqt] }), 'image/jpeg')),
    progressive: await refuse(file(jpegHeader(30000, 20000, { sof: 0xC2, before: [dqt] }), 'image/jpeg')),
    arithmetic: await refuse(file(jpegHeader(30000, 20000, { sof: 0xC9 }), 'image/jpeg')),
    afterTables: await refuse(file(jpegHeader(30000, 20000, { before: [dht, dqt] }), 'image/jpeg')),
    fillBytes: await refuse(file(jpegHeader(30000, 20000, { before: [Uint8Array.of(0xFF, 0xFF), dqt] }), 'image/jpeg')),
    smallAfterTables: await refuse(file(jpegHeader(640, 480, { before: [dht, dqt] }), 'image/jpeg')),
  };

  // Only a few bytes of a file are read: its first 24 and four of each segment, never the 60 KB EXIF block or the rest
  const big = spy(file(jpegHeader(30000, 20000, { before: [app1, icc, dqt] }), 'image/jpeg'));
  const bigRefusal = await refuse(big);
  out.reads = { refusal: bigRefusal, bytes: big.reads.reduce((n, r) => n + (r[1] - r[0]), 0), calls: big.reads.length, size: big.size };
  const png = spy(file(pngHeader(30000, 20000), 'image/png'));
  await refuse(png);
  out.pngReads = png.reads;

  // Anything that is not a PNG or a JPEG with a usable header is not opened
  const notIhdr = pngHeader(10, 10);
  notIhdr.set(text('IDAT'), 12);
  out.notPhotos = {
    textClaimingJpeg: await refuse(file(text('not a photo at all\n'.repeat(40)), 'image/jpeg')),
    gif: await refuse(file(text('GIF89a\x10\x00\x10\x00'), 'image/gif')),
    webp: await refuse(file(text('RIFF\x00\x00\x00\x00WEBPVP8 '), 'image/webp')),
    empty: await refuse(file(new Uint8Array(0), 'image/png')),
    truncatedPng: await refuse(file(pngHeader(10, 10).slice(0, 20), 'image/png')),
    notIhdr: await refuse(file(notIhdr, 'image/png')),                             // a plausible size, but the first chunk is not IHDR
    soiOnly: await refuse(file(Uint8Array.of(0xFF, 0xD8), 'image/jpeg')),
    scanBeforeFrame: await refuse(file(concat([Uint8Array.of(0xFF, 0xD8), dqt, segment(0xDA, 10)]), 'image/jpeg')),
    endBeforeFrame: await refuse(file(concat([Uint8Array.of(0xFF, 0xD8, 0xFF, 0xD9), dqt, dqt]), 'image/jpeg')),
    badLength: await refuse(file(Uint8Array.of(0xFF, 0xD8, 0xFF, 0xE1, 0x00, 0x01, 0xFF, 0xC0, 0, 0, 0, 0, 0, 0, 0, 0), 'image/jpeg')),
    cutOff: await refuse(file(concat([Uint8Array.of(0xFF, 0xD8), app1.slice(0, 5000)]), 'image/jpeg')),
    notAnImage: await refuse(file(pngHeader(10, 10), 'text/plain')),
    pdf: await refuse(file(text('%PDF-1.7'), 'application/pdf')),
  };

  // The byte cap stays: 64 MiB goes on to its header, one byte more is refused without reading any of it
  const never = { type: 'image/jpeg', size: 64 * 1024 * 1024 + 1, slice() { throw new Error('it was read'); } };
  out.overBytes = await refuse(never).catch((e) => 'it threw: ' + e.message);
  const exactly = { type: 'image/png', size: 64 * 1024 * 1024, slice: (a, b) => new Blob([pngHeader(100, 100)]).slice(a, b) };
  out.exactlyBytes = await refuse(exactly);

  // The header as read, whatever the page then does with it
  const read = (bytes) => { const f = file(bytes, ''); return C.imageHeader((a, b) => C.readBytes(f, a, b), f.size); };
  out.headers = [await read(pngHeader(4000, 3000)), await read(jpegHeader(640, 480, { sof: 0xC2, before: [app1, dqt] })), await read(text('hello'))];

  // A browser without Blob.arrayBuffer reads through a FileReader
  globalThis.FileReader = class {
    readAsArrayBuffer(part) { this.result = part.bytes.buffer.slice(part.bytes.byteOffset, part.bytes.byteOffset + part.bytes.length); setTimeout(() => this.onload(), 0); }
  };
  const old = { slice: (a, b) => ({ bytes: Uint8Array.of(1, 2, 3, 4, 5, 6).slice(a, b) }) };
  out.fileReader = Array.from(await C.readBytes(old, 1, 4));

  // The location card follows the words held in the box
  const U = (content) => ({ role: 'user', content });
  const A = (content) => ({ role: 'assistant', content });
  const wanted = C.locationCardWanted;
  out.card = [
    wanted("what's the weather near me", false, true, false, []),
    wanted('and tomorrow?', false, true, false, [U('weather near me'), A('Sunny.')]),
    wanted('', false, true, false, [U('weather near me'), A('Sunny.')]),
    wanted('   \n ', false, true, false, [U('weather near me'), A('Sunny.')]),
    wanted('write me a poem', false, true, false, []),
    wanted('weather near me', true, true, false, []),
    wanted('weather near me', false, false, false, []),
    wanted('weather near me', false, true, true, []),
    wanted(null, false, true, false, [U('weather near me')]),
    wanted(undefined, false, true, false, []),
  ];
  process.stdout.write(JSON.stringify(out));
})();
"""


@pytest.fixture(scope="module")
def header_run(tmp_path_factory):
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    folder = tmp_path_factory.mktemp("chat-header")
    (folder / "page.html").write_text(chat_page.CHAT_HTML, encoding="utf-8")
    (folder / "harness.js").write_text(_HEADER_HARNESS, encoding="utf-8")
    result = subprocess.run(["node", str(folder / "harness.js"), str(folder / "page.html")],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


NOT_A_PHOTO = "That file isn't a photo this page can open."
TOO_MANY_PIXELS = "That photo is too big to open here (over 100 megapixels)."
TOO_MANY_BYTES = "That photo is too big to open here (over 64 MB)."


def test_a_png_or_jpeg_over_100_megapixels_is_refused_from_its_header_alone(header_run):
    assert header_run["png"] == {
        "ok": "", "exactly": "", "over": TOO_MANY_PIXELS, "huge": TOO_MANY_PIXELS, "most": TOO_MANY_PIXELS,
        "noType": "",                                                       # a browser that gives no type is judged by the header
        "zero": NOT_A_PHOTO}                                                # a header with no size says nothing usable
    assert header_run["jpeg"] == {
        "ok": "", "exactly": "", "over": TOO_MANY_PIXELS, "huge": TOO_MANY_PIXELS,
        "progressive": TOO_MANY_PIXELS, "arithmetic": TOO_MANY_PIXELS,      # every frame marker, not only baseline
        "afterTables": TOO_MANY_PIXELS,                                     # a Huffman table (C4) is not mistaken for the frame
        "fillBytes": TOO_MANY_PIXELS, "smallAfterTables": ""}
    assert header_run["headers"] == [{"format": "png", "width": 4000, "height": 3000},
                                     {"format": "jpeg", "width": 640, "height": 480}, None]


def test_the_header_is_read_without_reading_the_file(header_run):
    reads = header_run["reads"]
    assert reads["refusal"] == TOO_MANY_PIXELS and reads["size"] > 63000       # a file with a 60 KB EXIF block in it
    assert reads["bytes"] <= 60 and reads["calls"] <= 8                         # 24 bytes, then four of each segment, then the frame's size
    assert header_run["pngReads"] == [[0, 24]]                                  # a PNG says it all in its first 24 bytes


def test_a_file_that_is_neither_a_png_nor_a_jpeg_with_a_usable_header_is_not_opened(header_run):
    assert set(header_run["notPhotos"].values()) == {NOT_A_PHOTO}
    assert len(header_run["notPhotos"]) == 13                                   # text, GIF, WebP, empty, cut off, no IHDR, no frame, ...


def test_the_64_mib_cap_stays_and_is_checked_before_any_byte_is_read(header_run):
    assert header_run["overBytes"] == TOO_MANY_BYTES                            # the fake throws if a byte is read
    assert header_run["exactlyBytes"] == ""                                     # exactly 64 MiB is allowed; its header is read next


def test_a_browser_without_blob_array_buffer_reads_the_header_through_a_file_reader(header_run):
    assert header_run["fileReader"] == [2, 3, 4]


def test_the_location_card_stays_only_while_the_held_words_are_a_local_question(header_run):
    # a local question; a follow-up to one; deleted words (even after a weather question); words edited into something else;
    # sharing already on; no geolocation in the browser; "Not now" already said in this chat; no words at all
    assert header_run["card"] == [True, True, False, False, False, False, False, False, False, False]
