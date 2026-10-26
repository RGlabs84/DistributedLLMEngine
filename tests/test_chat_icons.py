"""
One DLNE icon everywhere.

The owner's rule: the only DLNE mark is the cyan-and-pink brain with "DLNE" under it
(vscode-extension/media/dlne_logo.png); anything else is gone for good. On the server that means:

* every icon the phone chat serves is a PNG cut from that logo by scripts/make_dlne_icons.py,
  and engine/static/icons.json records the SHA-256 of the logo and of each icon, so a swapped,
  hand-edited or stale icon (or a changed logo whose icons were not regenerated) fails here;
* engine/chat_page.py has no drawing code and its page has no inline logo (only the few button
  glyphs, pinned below);
* no other image, and no brain emoji standing in for the logo, lives in engine/ or scripts/.

The extension's side of the rule is vscode-extension/test/media-icons.test.js.
"""

import ast
import hashlib
import json
import os
import re
import struct
from html.parser import HTMLParser

import pytest
from httpx import ASGITransport, AsyncClient

from engine import chat_page, coordinator, icons, landing

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGO = os.path.join(REPO_ROOT, "vscode-extension", "media", "dlne_logo.png")
RECORD_PATH = os.path.join(icons.STATIC_DIR, "icons.json")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
REMOTE = ("203.0.113.79", 40000)   # TEST-NET-3: a stranger, not the coordinator host

# The maskable icon is the logo at 80% on its own background (scripts/make_dlne_icons.py).
MASKABLE_SCALE = 0.8
# Where the brain and the "DLNE" text sit in the 512 px logo (x0, y0, x1, y1), measured from the
# logo's bright regions. The tile's decorative rim reaches the corners and is allowed to be cropped.
BRAIN_BOX = (92, 92, 420, 376)
TEXT_BOX = (169, 395, 342, 434)


def _sha256(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def _record():
    with open(RECORD_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _png_header(data):
    """(width, height, bit depth, colour type); colour type 2 is RGB with no alpha channel."""
    assert data.startswith(PNG_SIGNATURE)
    return struct.unpack(">IIBB", data[16:26])


def _client():
    return AsyncClient(transport=ASGITransport(app=coordinator.app, client=REMOTE), base_url="http://test")


# ---------------------------------------------------------------- the files and their record


def test_the_record_names_the_logo_and_every_icon_the_chat_can_serve():
    record = _record()
    assert record["source"] == "vscode-extension/media/dlne_logo.png"
    assert set(record["icons"]) == set(icons.ICONS) == set(chat_page.ICONS)
    for name, (size, purpose) in icons.ICONS.items():
        assert (record["icons"][name]["size"], record["icons"][name]["purpose"]) == (size, purpose), name


def test_every_icon_is_exactly_the_recorded_png_and_nothing_else_is_in_the_folder():
    record = _record()
    for name, info in record["icons"].items():
        path = os.path.join(icons.STATIC_DIR, name)
        assert _sha256(path) == info["sha256"], f"{name} is not the icon scripts/make_dlne_icons.py recorded"
        with open(path, "rb") as handle:
            width, height, depth, colour = _png_header(handle.read())
        assert (width, height, depth) == (info["size"], info["size"], 8), name
        assert colour == 2, f"{name} must be opaque RGB: iOS shows transparent pixels as black"
    assert sorted(os.listdir(icons.STATIC_DIR)) == sorted(list(record["icons"]) + ["icons.json"])


def test_the_record_is_for_the_logo_that_is_in_the_repository():
    if not os.path.isfile(LOGO):
        pytest.skip("vscode-extension/media/dlne_logo.png is not on this machine")
    assert _sha256(LOGO) == _record()["source_sha256"], (
        "dlne_logo.png changed since the icons were cut: run scripts/make_dlne_icons.py and commit the result")
    with open(LOGO, "rb") as handle:
        assert _png_header(handle.read())[:2] == (512, 512)


def test_the_icons_are_the_logo_resized_and_still_show_the_brain():
    pil = pytest.importorskip("PIL.Image")
    if not os.path.isfile(LOGO):
        pytest.skip("vscode-extension/media/dlne_logo.png is not on this machine")
    from PIL import ImageChops, ImageStat

    logo = pil.open(LOGO).convert("RGB")

    def difference(a, b):
        return sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3

    for name, (size, purpose) in icons.ICONS.items():
        icon = pil.open(os.path.join(icons.STATIC_DIR, name)).convert("RGB")
        assert icon.size == (size, size), name
        if purpose != "maskable":
            assert difference(icon, logo.resize((size, size), pil.LANCZOS)) < 1.0, f"{name} is not the logo resized"
        # Cyan and pink glow are the logo's colours; a blank tile or another mark would not have both.
        raw = icon.tobytes()
        pixels = [(raw[i], raw[i + 1], raw[i + 2]) for i in range(0, len(raw), 3)]
        cyan = sum(1 for r, g, b in pixels if r < 120 and g > 180 and b > 190)
        pink = sum(1 for r, g, b in pixels if r > 190 and g < 150 and b > 120)
        assert cyan > len(pixels) * 0.004 and pink > len(pixels) * 0.003, (name, cyan, pink)

    maskable = pil.open(os.path.join(icons.STATIC_DIR, "icon-maskable-512.png")).convert("RGB")
    inner = round(512 * MASKABLE_SCALE)
    offset = (512 - inner) // 2
    middle = (offset + 40, offset + 40, offset + inner - 40, offset + inner - 40)
    scaled = logo.resize((inner, inner), pil.LANCZOS)
    assert difference(maskable.crop(middle), scaled.crop((40, 40, inner - 40, inner - 40))) < 1.0
    corner = maskable.getpixel((0, 0))
    assert all(abs(corner[i] - logo.getpixel((256, 1))[i]) < 14 for i in range(3)), "maskable background is not the logo's own"


def test_the_maskable_icon_keeps_the_brain_and_the_text_inside_androids_safe_circle():
    """Android keeps a circle of 80% of the icon's width. Every corner of the brain and the text must be in it."""
    centre, radius = 256.0, 0.4 * 512
    for box in (BRAIN_BOX, TEXT_BOX):
        x0, y0, x1, y1 = box
        for x, y in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
            fx = (x - centre) * MASKABLE_SCALE
            fy = (y - centre) * MASKABLE_SCALE
            assert (fx * fx + fy * fy) ** 0.5 <= radius, (box, (x, y))


# ---------------------------------------------------------------- what the chat serves


@pytest.mark.anyio
async def test_every_icon_the_chat_serves_is_a_recorded_png_cut_from_the_logo():
    record = _record()["icons"]
    async with _client() as c:
        for name in icons.ICONS:
            for path in (f"/chat/{name}", icons.icon_url(name)):
                resp = await c.get(path)
                assert resp.status_code == 200, path
                assert resp.headers["content-type"] == "image/png", path
                assert hashlib.sha256(resp.content).hexdigest() == record[name]["sha256"], path
        manifest = json.loads((await c.get("/chat/manifest.webmanifest")).text)
        for icon in manifest["icons"]:
            name = icon["src"].split("?")[0].rsplit("/", 1)[1]
            assert name in record and icon["src"] == icons.icon_url(name), icon
            assert hashlib.sha256((await c.get(icon["src"])).content).hexdigest() == record[name]["sha256"]


class _Tree(HTMLParser):
    """Every element of a page with the chain of its ancestors."""

    VOID = {"meta", "link", "img", "br", "input", "hr", "path", "rect", "circle", "line", "use"}

    def __init__(self):
        super().__init__()
        self.stack, self.elements = [], []

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs), [(t, a) for t, a in self.stack]))
        if tag not in self.VOID:
            self.stack.append((tag, dict(attrs)))

    def handle_startendtag(self, tag, attrs):
        self.elements.append((tag, dict(attrs), [(t, a) for t, a in self.stack]))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break


def _tree(html):
    parser = _Tree()
    parser.feed(html)
    return parser.elements


def test_the_pages_marks_are_the_logo_image_in_the_header_the_welcome_and_the_first_run_sheet():
    marks = [(attrs, parents) for tag, attrs, parents in _tree(chat_page.CHAT_HTML)
             if tag == "img" and "mark" in (attrs.get("class") or "").split()]
    assert len(marks) == 3
    for attrs, _parents in marks:
        assert attrs["src"] == icons.icon_url("icon-192.png")
        assert attrs["alt"] == "" and attrs["width"] == attrs["height"]
    places = [{(t, a.get("id") or a.get("class")) for t, a in parents} for _attrs, parents in marks]
    assert ("header", "top") in places[0]
    assert ("section", "welcome") in places[1]
    assert ("dialog", "first-run") in places[2]
    assert [tag for tag, _a, _p in _tree(chat_page.CHAT_HTML) if tag in ("img",)] == ["img"] * 3, "no other image"


def test_the_page_keeps_its_csp_and_loads_its_icons_only_from_itself():
    assert chat_page.CSP.count("img-src 'self' data:") == 1
    for tag, attrs, _parents in _tree(chat_page.CHAT_HTML):
        if tag == "img" or (tag == "link" and attrs.get("rel") in ("icon", "apple-touch-icon")):
            url = attrs.get("src") or attrs.get("href")
            assert url.startswith("/chat/") and not url.startswith("//"), url
    assert "data:image" not in chat_page.CHAT_HTML and "data:image" not in landing.LANDING_HTML


# The page's own button glyphs: plain controls, not marks. Anything else drawn inline is a new logo.
BUTTON_GLYPH_PATHS = {
    "M12 5v14M5 12h14",                       # new chat
    "M4 7h16M4 12h16M4 17h16",                # menu
    "M6 6l12 12M18 6L6 18",                   # close (menu and report sheets)
    "M12 19V5M5.5 11.5L12 5l6.5 6.5",         # send
}
STOP_SQUARE = {"x": "7", "y": "7", "width": "10", "height": "10", "rx": "2", "fill": "currentColor"}


def test_the_page_has_no_inline_logo_only_its_button_glyphs():
    elements = _tree(chat_page.CHAT_HTML)
    assert not [e for e in elements if e[0] in ("symbol", "use", "image", "text", "g", "defs", "circle", "ellipse")]
    assert "<symbol" not in chat_page.CHAT_HTML and "dlne-mark" not in chat_page.CHAT_HTML
    svgs = [e for e in elements if e[0] == "svg"]
    assert len(svgs) == 6
    for _tag, attrs, parents in svgs:
        assert "class" not in attrs and attrs.get("aria-hidden") == "true", attrs
        assert any(t == "button" for t, _a in parents), "an inline <svg> outside a button is a drawn mark"
        assert attrs["viewbox"] == "0 0 24 24"   # HTMLParser lower-cases attribute names
    shapes = [(t, a) for t, a, parents in elements if t in ("path", "rect") and any(p[0] == "svg" for p in parents)]
    assert len(shapes) == 6
    for tag, attrs in shapes:
        if tag == "path":
            assert attrs["d"] in BUTTON_GLYPH_PATHS, f"a new drawn shape: {attrs['d']}"
        else:
            assert attrs == STOP_SQUARE


DRAWING_IMPORTS = {"zlib", "struct", "math", "array", "PIL", "numpy", "cairo", "cairosvg", "matplotlib"}
DRAWING_NAMES = {"render_icon", "_png", "_bezier", "_mark_primitives", "_MARK_SYMBOL", "_MARK_CENTRE",
                 "_MARK_REACH", "_HEMISPHERE", "_BG_CENTRE", "_BG_EDGE", "_CYAN", "lru_cache"}


@pytest.mark.parametrize("module", ["chat_page.py", "landing.py", "icons.py"])
def test_the_servers_icon_modules_have_no_drawing_code(module):
    path = os.path.join(REPO_ROOT, "engine", module)
    with open(path, "r", encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source)
    imported, defined = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
            defined |= {alias.name for alias in node.names}
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            defined.add(node.id)
    assert not imported & DRAWING_IMPORTS, f"engine/{module} imports drawing tools: {imported & DRAWING_IMPORTS}"
    assert not defined & DRAWING_NAMES, f"engine/{module} defines drawing code: {defined & DRAWING_NAMES}"
    for old in ("<symbol", "dlne-mark", "M11 3.5C9.2", "dlne_icon.svg", "brain.svg"):
        assert old not in source, f"engine/{module} still mentions {old!r}"


def test_the_landing_page_draws_nothing():
    elements = _tree(landing.LANDING_HTML)
    assert not [e for e in elements if e[0] in ("svg", "symbol", "use", "path", "canvas")]


# ---------------------------------------------------------------- no other mark anywhere on the server side


def _files(top, skip=("__pycache__",)):
    for dirpath, dirnames, filenames in os.walk(os.path.join(REPO_ROOT, top)):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for name in filenames:
            yield os.path.join(dirpath, name)


def test_engine_holds_code_and_the_one_icon_folder_and_scripts_hold_no_images():
    for path in _files("engine"):
        relative = os.path.relpath(path, os.path.join(REPO_ROOT, "engine")).replace(os.sep, "/")
        assert relative.endswith(".py") or relative.startswith("static/"), f"engine/{relative} is not code or an icon"
    for path in _files("scripts"):
        assert not path.lower().endswith((".png", ".svg", ".ico", ".jpg", ".jpeg", ".gif", ".webp", ".icns")), path


def test_no_brain_emoji_stands_in_for_the_logo_in_the_servers_code_or_the_extensions_source():
    """The brain emoji was a second DLNE mark (sidebar, status bar, notifications, the Discord tile)."""
    brain = "\U0001F9E0"
    roots = [("engine", (".py",)), ("scripts", (".py",)), ("vscode-extension/src", (".ts",))]
    for top, extensions in roots:
        for path in _files(top):
            if path.endswith(extensions):
                with open(path, "r", encoding="utf-8") as handle:
                    assert brain not in handle.read(), f"{os.path.relpath(path, REPO_ROOT)} uses the brain emoji as a mark"
    assert brain not in chat_page.CHAT_HTML and brain not in landing.LANDING_HTML
    assert re.search("&#(x1f9e0|129504);", chat_page.CHAT_HTML + landing.LANDING_HTML, re.I) is None
