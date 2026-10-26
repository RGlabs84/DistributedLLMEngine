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
        "/v1/app/chat/completions", "/v1/app/report", "/v1/cluster/status"}


def test_the_privacy_notice_is_shown_once_and_repeated_under_the_input():
    page = chat_page.CHAT_HTML
    assert chat_page.PRIVACY_NOTICE == ("Answers are written by computers people lend to DLNE. "
                                        "Don't send passwords, private details or anything secret.")
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
    for line in (chat_page.PRIVACY_NOTICE, chat_page.LOOKUP_PRIVACY):
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
_NODE_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');

class Text {
  constructor(data) { this.nodeType = 3; this.data = String(data); this.parentNode = null; }
  get textContent() { return this.data; }
}
class Element {
  constructor(tag) {
    this.nodeType = 1; this.tagName = String(tag).toUpperCase(); this.childNodes = [];
    this.className = ''; this.attrs = {}; this.dataset = {}; this.style = {}; this.parentNode = null;
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
  addEventListener() {}
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  all() { return this.childNodes.filter((n) => n.nodeType === 1).flatMap((n) => [n, ...n.all()]); }
}
globalThis.document = { createElement: (tag) => new Element(tag), createTextNode: (data) => new Text(data) };

const html = fs.readFileSync(process.argv[2], 'utf8');
const core = /<script id="dlne-core">([\s\S]*?)<\/script>/.exec(html)[1];
vm.runInThisContext(core + '\n;globalThis.DLNEChat = DLNEChat;');
const C = globalThis.DLNEChat;
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
