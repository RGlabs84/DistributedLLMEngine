"""
The pictures page a coordinator serves at ``/pictures``: whether DLNE makes pictures right now, and why not.

Pictures can be paused (``IMAGE_GEN_ENABLED=paused``, 2026-10-30): the media card that draws them is lent to the coding
model instead. While they are, every way of asking for one ends at the same sentence (``PICTURES_PAUSED_NOTE``): the
picture endpoints answer it with ``"page": "/pictures"``, the phone and web chat's model is told it with this page's
full address, and the status carries it as ``images.note``. This page is where that address leads.

LIVE, NOT TYPED. The page holds no state of its own. It reads ``/v1/cluster/status`` (same origin) when it opens, every
60 seconds while it is in view and whenever it is shown again, and says one of four things: pictures are paused (with
the status's own note), pictures are on (with a link to the chat), pictures are not available right now (off, or on but
not ready), or DLNE is restarting (the status could not be read). Nothing version- or count-like is written into the
HTML: the note itself comes from the status, so the page and the chat can never word it differently.

SAFETY. One self-contained document (inline CSS and JS, nothing from another site), served with the phone chat's own
headers (engine/chat_page.py: the same Content-Security-Policy, ``X-Frame-Options: DENY``, ``nosniff``, no referrer).
Text from the status is only ever put on the page with ``textContent``. Its one image is the DLNE logo the chat serves.

Wired in with one line: ``app.include_router(pictures_router)``.
"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from engine.chat_page import CSP, THEME_DARK, THEME_LIGHT
from engine.icons import icon_url
from engine.landing import PUBLIC_URL

# Where the page is, and the one sentence every paused picture request ends at.
PICTURES_PATH = "/pictures"
PICTURES_PAUSED_NOTE = ("Pictures are paused for now: DLNE's graphics card is busy running the smarter coding model. "
                        "They will be back.")


def paused_note_with_link() -> str:
    """The note with this page's full public address after it, for words a person reads away from this server (the chat
    model's tool result, the phone's image_refused event): "... They will be back. More: https://<host>/pictures"."""
    return f"{PICTURES_PAUSED_NOTE} More: {PUBLIC_URL.rstrip('/')}{PICTURES_PATH}"


# The phone chat's headers, exactly (tests/test_pictures_page.py compares them).
_HTML_HEADERS = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Content-Security-Policy": CSP,
                 "X-Frame-Options": "DENY", "Cache-Control": "no-cache"}

_HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>DLNE pictures</title>
<meta name="description" content="Whether DLNE is making pictures right now.">
<meta name="color-scheme" content="dark light">
<meta name="theme-color" content="{{THEME_DARK}}" media="(prefers-color-scheme: dark)">
<meta name="theme-color" content="{{THEME_LIGHT}}" media="(prefers-color-scheme: light)">
<link rel="icon" type="image/png" sizes="32x32" href="{{ICON_FAVICON_32}}">
<link rel="icon" type="image/png" sizes="48x48" href="{{ICON_FAVICON_48}}">
<style>
:root {
  color-scheme: dark;
  --bg: #0b1214; --surface: #111c1f; --surface-2: #172529; --border: #22353a;
  --text: #e4eef0; --muted: #93a7ac; --faint: #6f858a;
  --accent: #00f0ff; --on-accent: #00262a; --link: #6cf5ff;
  --ok: #3ee08f; --down: #f5b942; --focus: #00f0ff;
}
@media (prefers-color-scheme: light) {
  :root {
    color-scheme: light;
    --bg: #f4f8f9; --surface: #ffffff; --surface-2: #e8f0f2; --border: #d2dfe2;
    --text: #0f2023; --muted: #4c5f64; --faint: #687b80;
    --accent: #00838d; --on-accent: #ffffff; --link: #006770;
    --ok: #13894f; --down: #a86200; --focus: #00838d;
  }
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
html, body { margin: 0; min-height: 100%; background: var(--bg); color: var(--text); }
body {
  font: 16px/1.55 system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  -webkit-text-size-adjust: 100%; text-size-adjust: 100%;
  padding: calc(env(safe-area-inset-top) + 32px) max(16px, env(safe-area-inset-right)) 32px max(16px, env(safe-area-inset-left));
}
:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
main { max-width: 520px; margin: 0 auto; text-align: center; }
.mark { display: block; width: 72px; height: 72px; margin: 0 auto 16px; border-radius: 22%; }
.card { padding: 24px 20px; border: 1px solid var(--border); border-radius: 18px; background: var(--surface); }
h1 { margin: 0 0 10px; font-size: 1.35rem; line-height: 1.3; font-weight: 650; display: flex; align-items: center; justify-content: center; gap: 10px; }
.dot { flex: none; width: 10px; height: 10px; border-radius: 50%; background: var(--faint); }
.dot.up { background: var(--ok); }
.dot.down { background: var(--down); }
p { margin: 0 0 12px; color: var(--muted); }
.note { color: var(--text); }
.button {
  display: inline-flex; align-items: center; justify-content: center; min-height: 48px; margin-top: 6px; padding: 0 22px;
  border-radius: 999px; background: var(--accent); color: var(--on-accent); font-weight: 600; text-decoration: none;
}
.checked { margin: 16px 0 0; font-size: .8rem; color: var(--faint); }
a { color: var(--link); text-underline-offset: 2px; }
</style>
</head>
<body>
<main>
  <img class="mark" src="{{ICON_192}}" width="72" height="72" alt="DLNE">
  <div class="card" id="state" aria-live="polite">
    <h1><span class="dot" id="dot" aria-hidden="true"></span><span id="title">Checking pictures…</span></h1>
    <p class="note" id="note" hidden></p>
    <p id="more" hidden></p>
    <a class="button" id="chat" href="/chat">Open DLNE Chat</a>
  </div>
  <p class="checked" id="checked" hidden></p>
</main>
<script>
(function () {
  'use strict';
  const STATUS = '/v1/cluster/status';
  const EVERY = 60 * 1000;
  const $ = (id) => document.getElementById(id);
  const ui = { dot: $('dot'), title: $('title'), note: $('note'), more: $('more'), checked: $('checked') };

  // What the status says about pictures: 'paused', 'on', 'off' (switched off, or on but not ready), or 'restarting' when
  // there was no status to read. A missing key means false, as everywhere else that reads the status.
  function pictureState(data) {
    if (!data || typeof data !== 'object') return 'restarting';
    const images = data.images && typeof data.images === 'object' ? data.images : null;
    if (images && images.paused === true) return 'paused';
    const caps = data.capabilities && typeof data.capabilities === 'object' ? data.capabilities : {};
    return caps.image_generation === true ? 'on' : 'off';
  }

  const WORDS = {
    paused: { title: 'Pictures are paused', more: 'You can still chat with DLNE as usual.', dot: 'down' },
    on: { title: 'Pictures are on', more: 'Ask DLNE Chat for one, for example "draw a fox in the snow".', dot: 'up' },
    off: { title: "Pictures aren't available right now", more: 'You can still chat with DLNE as usual.', dot: '' },
    restarting: { title: 'DLNE is restarting', more: 'This page checks again every minute.', dot: 'down' },
  };

  function show(state, data) {
    const words = WORDS[state];
    ui.title.textContent = words.title;
    ui.dot.className = 'dot' + (words.dot ? ' ' + words.dot : '');
    // The note is the server's own sentence, never one typed here: the chat says the very same words.
    const note = state === 'paused' && typeof data.images.note === 'string' ? data.images.note : '';
    ui.note.textContent = note;
    ui.note.hidden = !note;
    ui.more.textContent = words.more;
    ui.more.hidden = false;
    ui.checked.textContent = 'Checked at ' + new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    ui.checked.hidden = false;
  }

  async function refresh() {
    let data = null;
    try {
      const resp = await fetch(STATUS, { cache: 'no-store', headers: { Accept: 'application/json' } });
      if (resp.ok) data = await resp.json();
    } catch (e) {
      data = null;      // offline, or the coordinator is restarting
    }
    show(pictureState(data), data);
  }

  refresh();
  setInterval(() => { if (document.visibilityState === 'visible') refresh(); }, EVERY);
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') refresh(); });
})();
</script>
</body>
</html>
"""

PICTURES_HTML = (_HTML_TEMPLATE
                 .replace("{{THEME_DARK}}", THEME_DARK)
                 .replace("{{THEME_LIGHT}}", THEME_LIGHT)
                 .replace("{{ICON_192}}", icon_url("icon-192.png"))
                 .replace("{{ICON_FAVICON_32}}", icon_url("favicon-32.png"))
                 .replace("{{ICON_FAVICON_48}}", icon_url("favicon-48.png")))

pictures_router = APIRouter()


@pictures_router.api_route(PICTURES_PATH, methods=["GET", "HEAD"], include_in_schema=False)
async def pictures_page() -> HTMLResponse:
    """The page. Public by design, like the chat: it says only what the public status already says."""
    return HTMLResponse(PICTURES_HTML, headers=_HTML_HEADERS)
