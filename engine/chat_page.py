"""
DLNE Chat: the phone chat page a coordinator serves at ``/chat``.

One self-contained page (inline CSS and JS, no external scripts, fonts or CDNs) over the CPU
pool's public endpoint, ``POST /v1/app/chat/completions`` (engine/app_pool.py). It installs as
an app: ``/chat/manifest.webmanifest`` and the icons make iPhone "Add to Home Screen" and
Android Chrome "Install app" work, and ``/chat/sw.js`` caches the app shell (never an API
answer) so the installed app opens offline and says so.

ICONS. The page has one mark: the official DLNE logo (the cyan-and-pink brain with "DLNE" under
it), served as the PNG files in ``engine/static/`` (engine/icons.py). Nothing here draws a mark,
and a logo drawn inline as SVG would be a second one, so tests/test_chat_icons.py fails if the
module gains drawing code or the page gains any inline graphic beyond its few button glyphs.

LOOKUPS. The coordinator can look things up for the model (weather, Wikipedia, news, web
search). The answer stream carries its events as ``data:`` lines holding a ``{"dlne": {...}}``
object instead of ``choices``: ``searching`` (a tool and a query, up to four per answer; the page
shows the latest, worded by tool), ``sources`` (the whole list so far, so it replaces the last
one), ``search_note`` (a muted line under the sources) and ``search_unavailable``. In-range
``[n]`` markers in the answer become links to source n. Sources are saved with the chat.
``retract`` (sent by server 2.3.5) means the model began answering from memory while unsure and the
server takes that text back to look the question up instead: the page empties the answer and drops
its sources and notes, then the usual ``searching`` and ``sources`` events and a fresh answer follow.
An older page ignores the event and shows both texts.

REPORTS. Each answer has a "Report" action that posts the question, the answer, a reason and an
optional note to ``POST /v1/app/report``.

SAFETY. Model, user and search text are only ever put on the page through ``textContent`` and
``createElement``: the page's small Markdown renderer builds DOM nodes and never parses HTML,
and links (in answers, citations and source lists) are made only for http(s) URLs. The pure parts
live in a separate ``<script id="dlne-core">`` so tests can run them under node. The page is
served with a strict Content-Security-Policy (``connect-src 'self'`` keeps it talking to this
coordinator only).

LINKS. The page links out to the DLNE website, the Discord, the coffee page and the extension (the
constants below and in engine/landing.py), in the welcome screen's support line and in the menu. They
are plain ``<a href>`` links that open in a new tab with ``rel="noopener noreferrer"``: navigation
needs nothing from the CSP, which stays as strict as it was.

CHAT ID AND LOCATION. Every chat carries a random UUID v4 (``chat_id``, made when the chat starts,
or on the next send for a chat saved before it existed), sent with each request so the server can
keep a private prompt cache per chat. "Share location" in the menu is off until the
reader turns it on, which is when the browser asks; the position is the browser's precise one (high
accuracy), at most five decimals (about 1 m), held in memory only. It is judged by the age the browser
reports (``position.timestamp``): a message carries ``location: {lat, lon}`` only while the setting is on
and the fix is under three minutes old, a fix is asked for again once it is a minute old, and a failed
refresh drops the old fix. A send waits up to three seconds for a fix that is on its way; if there is
still none, the message goes without one and the answer says so (a note under it). The logic is
``DLNEChat.locator`` so tests can run it under node. The server uses the position for lookups and
never stores or logs it. The setting itself is remembered in localStorage; the browser remembers its own
permission answer (an iOS home-screen app may ask again each session). Geolocation is not governed by
the CSP, and no Permissions-Policy is sent, so the default (same origin) applies.

IMAGES (server 2.6.1). "Create image" is an item of the composer's "+" menu (below), offered only while
``/v1/cluster/status`` says ``capabilities.image_generation`` is true. A sheet takes a description and a size,
``POST /v1/app/images`` starts the job, and a card in the conversation polls ``GET /v1/app/images/{id}`` (every 2 s
while the page is visible, 5 s in the background), shows the elapsed time against the estimate, offers Cancel, and
then shows the picture with a Save link. The picture arrives as base64 and is shown through a ``data:`` URL (the CSP
allows ``img-src data:``; nothing else). Its bytes live in memory only: the saved chat keeps the text
placeholder ``[Image: <prompt>]``, which is also all the chat model ever sees, and a card for a job still
running when the page reloads picks its polling up again from the saved job id.

THE "+" MENU AND PHOTOS (server 2.6.2). A "+" at the left of the message box opens a small popover: Photos (a file
picker for PNG and JPEG, several at once), Camera (the same picker with ``capture``, offered on touch devices only)
and Create image. Photos and Camera appear only while ``capabilities.app_vision`` is true, Create image only while
``capabilities.image_generation`` is; with neither the "+" is hidden. A chosen photo is made ready in the page,
before anything is sent: decoded with its EXIF orientation applied (``createImageBitmap`` with
``imageOrientation: 'from-image'``, or an ``<img>`` where the browser lacks the option; first its own header is read, a
PNG's first 24 bytes or a JPEG's segments walked to its frame header, a few bytes of each, and a file over 64 MiB, a PNG
or JPEG of more than 100 megapixels, or one that is neither, is refused with a plain line before anything is decoded),
scaled so it has at most
1,200,000 pixels and no side over 2,048, and written again as a JPEG on a canvas (quality 85; over 4 MiB, quality 70,
then each side halved, again and again, until it fits), which leaves every trace of EXIF and GPS behind. It shows as a thumbnail above the
message box with a remove button, at most four per message, and a message may be photos alone. On the wire a user
turn's ``content`` is then a list of ``text`` and ``image_url`` parts. Only the newest user turn that has photos sends
them; every other photo goes as the text ``[photo]``, one each, side by side after a blank line that follows its turn's
text (the Android app writes it the same way). Photos live in memory only (a WeakMap, and
the CSP is untouched: they are ``data:`` URLs): a saved chat keeps only how many a turn had, and after a reload that
turn says "Photo not kept after reload" and sends ``[photo]``. When the server refuses a turn's photos (a 4xx that
names them: this network's hour of photos is used up, 429 ``image_quota``, or a photo it cannot read), the turn stays
with the server's words under it, nothing is paused (messages without photos still go through), and those photos
are ``[photo]`` in every later request; Try again on that turn is the reader's choice to send them again. A rate limit
(a 429 with no ``code``) is different: the turn goes back to the box and the box waits. The pure parts (the sizes, the
encoding loop, the history rule) are in ``dlne-core`` so tests run them under node.

PICTURES FROM THE CHAT (server 2.6.2). Every request carries ``features: ["image_jobs"]``: this page shows the two
events that make that safe to send. ``image_job`` (an id, the prompt, a size, a position and an estimate) gives the
reply a job card under its text, polled exactly like a card made from the sheet (2 s in view, 5 s behind it) and keyed by
its own job, so several replies can have pictures coming at once; the finished picture shows inside that reply with
Save, Share (where the browser can share a file) and Report. ``image_refused`` (a code and the server's own words) shows
the words as a quiet note under the reply. A reply that made a picture goes back to the model as its text followed by
``[Picture created: <prompt>]``, so "make it bluer" can reuse the description; one that was refused, failed or was
cancelled adds nothing, since no picture was created.

LOCATION, ASKED IN THE CHAT (server 2.6.2). Sending a local question while sharing is off, in a chat where the reader
has not said "Not now", holds the send and shows a card above the message box: "Use your location for this?" with "Use
my location" and "Not now". The first turns sharing on and asks the browser (its own prompt; the send waits up to a
minute for the answer), and the card says that it keeps sharing on until the reader turns it off in the menu; the second
sends without a place and is remembered with the chat (``declinedLocation``), as is a refusal from the browser. The card
belongs to the words held in the box: it goes away if they are deleted or edited into something that is not a local
question (``locationCardWanted``). The menu's switch is unchanged.

WHO ANSWERS. Since 2.6.1 phone answers come from DLNE's own server (its GPUs run the phone model in lanes
reserved for phones); a volunteer's computer is the backup, answering only while that engine is restarting or
down. The copy says so plainly and never promises it is always the server. The status line still reads
``app_pool.members_ready`` and ``active_model`` and nothing else.

PRIVACY. A volunteer's machine may see the text of a request it answers (as the backup), so the page says so on
first run and, in fewer words, under the input; the menu adds that lookups are made by the
server, not the phone, and that reported answers are kept for review. A photo or a picture is never sent to a volunteer's
machine (the server answers them on its own engine only; that engine's model is shared, and its layers can run on
volunteers' graphics cards, which see only intermediate numbers, never the picture), which the menu says
(``PHOTO_PRIVACY``, ``PICTURE_PRIVACY``) and a line under the thumbnails repeats while photos are attached
(``PHOTO_HINT``). Chat history lives in the browser's localStorage on the phone, nowhere else.

Wired in with one line: ``app.include_router(chat_router)``.
"""

import hashlib
import json
from typing import List

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, Response

from engine.icons import ICON_VERSION, ICONS, icon_png, icon_url
from engine.landing import DISCORD_INVITE_URL, EXTENSION_URL, OG_IMAGE_URL, PUBLIC_URL, WEBSITE_URL

COFFEE_URL = "https://buymeacoffee.com/wubarrk"

PRIVACY_NOTICE = ("Answers are written by DLNE's own server. If it is restarting or down, a volunteer's computer "
                  "may answer instead. Don't send passwords, private details or anything secret.")
LOOKUP_PRIVACY = ("Lookups (weather, Wikipedia, news, web search) are made by the DLNE server, not your phone; "
                  "reported answers are kept on the server for review.")
REPORT_PRIVACY = "Reported answers are kept on the DLNE server for review."
LOCATION_PRIVACY = ("Location is off unless you turn it on. If you do, your device's precise location is sent with a local "
                    "question (weather, nearby places and the like) to DLNE's own server and is used only for lookups that need it. For local weather, the "
                    "coordinates, rounded to about 100 m, go to the weather service (Open-Meteo). For local searches, "
                    "only a nearby place name is added to the search, which DLNE's search sends to web search engines. "
                    "The AI model that answers never gets your coordinates; when it looks something up near you, the "
                    "results it reads name the nearby town. Your location is never logged or stored.")
# Photos and pictures (server 2.6.2). A photo is sent only to DLNE's own engine, never to the volunteers' computers that
# answer as the backup for text; this page holds it in memory, so a reload ends it. A picture is made on DLNE's own
# server alone, checked before it is shown, and not kept with the chat. What is true only of the pixels is said as such:
# the engine's model is shared and its layers can run on volunteers' graphics cards, which see intermediate numbers.
PHOTO_PRIVACY = ("Photos you attach are sent to DLNE's own server for the answer, never to a volunteer's computer, and "
                 "are not stored there. Like every message, a photo is processed by the shared model, whose layers can "
                 "run on volunteers' graphics cards; those cards see only intermediate numbers, never the picture. "
                 "This page keeps them in memory only, so they are gone when you reload it.")
PICTURE_PRIVACY = ("Pictures you ask for are made on DLNE's own server, never on a volunteer's computer. The description "
                   "and the picture are checked before you see it, and it isn't saved with your chat: save it to keep it.")
PHOTO_HINT = "Photos stay on DLNE's own server and are never sent to a volunteer's computer."

CSP = ("default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
       "connect-src 'self'; img-src 'self' data:; manifest-src 'self'; worker-src 'self'; "
       "base-uri 'none'; frame-ancestors 'none'")

THEME_DARK = "#0b1214"
THEME_LIGHT = "#f4f8f9"

_COMMON_HEADERS = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}
_HTML_HEADERS = {**_COMMON_HEADERS, "Content-Security-Policy": CSP, "X-Frame-Options": "DENY",
                 "Cache-Control": "no-cache"}
# An icon URL that carries the current version never changes its bytes, so it is kept for a year.
# Any other URL for the same file (an old or hand-typed one) is revalidated every time, so a stale
# copy of an icon can never linger on a phone.
_ICON_HEADERS = {**_COMMON_HEADERS, "Cache-Control": "public, max-age=31536000, immutable"}
_ICON_UNVERSIONED_HEADERS = {**_COMMON_HEADERS, "Cache-Control": "no-cache"}


# ======================================================================================
# The page
# ======================================================================================

_HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, interactive-widget=resizes-content">
<title>DLNE Chat</title>
<meta name="description" content="Free AI chat, answered by DLNE's own server.">
<meta property="og:type" content="website">
<meta property="og:site_name" content="DLNE">
<meta property="og:title" content="DLNE Chat - free AI chat on your phone">
<meta property="og:description" content="Ask anything, free and without signing up. It looks things up when it isn't sure and shows its sources.">
<meta property="og:url" content="{{PUBLIC_URL}}/chat">
<meta property="og:image" content="{{OG_IMAGE_URL}}">
<meta name="twitter:card" content="summary">
<meta name="color-scheme" content="dark light">
<meta name="theme-color" content="{{THEME_DARK}}" media="(prefers-color-scheme: dark)">
<meta name="theme-color" content="{{THEME_LIGHT}}" media="(prefers-color-scheme: light)">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="DLNE">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="format-detection" content="telephone=no">
<link rel="manifest" href="/chat/manifest.webmanifest">
<link rel="icon" type="image/png" sizes="32x32" href="{{ICON_FAVICON_32}}">
<link rel="icon" type="image/png" sizes="48x48" href="{{ICON_FAVICON_48}}">
<link rel="apple-touch-icon" sizes="180x180" href="{{ICON_APPLE_TOUCH}}">
<style>
:root {
  color-scheme: dark;
  --bg: #0b1214; --surface: #111c1f; --surface-2: #172529; --border: #22353a;
  --text: #e4eef0; --muted: #93a7ac; --faint: #6f858a;
  --mark: #00f0ff; --accent: #00f0ff; --on-accent: #00262a; --link: #6cf5ff;
  --user: rgba(0, 240, 255, 0.08); --user-border: rgba(0, 240, 255, 0.24);
  --code-bg: #071012; --warn-bg: #2a2410; --warn-text: #fbd987; --err: #ff9b9b;
  --ok: #3ee08f; --down: #f5b942; --focus: #00f0ff;
}
@media (prefers-color-scheme: light) {
  :root {
    color-scheme: light;
    --bg: #f4f8f9; --surface: #ffffff; --surface-2: #e8f0f2; --border: #d2dfe2;
    --text: #0f2023; --muted: #4c5f64; --faint: #687b80;
    --mark: #00a8b4; --accent: #00838d; --on-accent: #ffffff; --link: #006770;
    --user: #e0f5f7; --user-border: #b6e3e8;
    --code-bg: #eef3f4; --warn-bg: #fff3d1; --warn-text: #5f4100; --err: #b42318;
    --ok: #13894f; --down: #a86200; --focus: #00838d;
  }
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
html, body { margin: 0; height: 100%; overflow: hidden; overscroll-behavior: none; background: var(--bg); color: var(--text); }
body {
  font: 16px/1.55 system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  -webkit-text-size-adjust: 100%; text-size-adjust: 100%; -webkit-tap-highlight-color: transparent;
}
button { font: inherit; color: inherit; }
:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
.sbar { display: none; }
@media (prefers-color-scheme: light) {
  /* iOS draws the status bar's text in white over an installed app: keep a dark strip under it. */
  .sbar { display: block; position: fixed; top: 0; left: 0; right: 0; height: env(safe-area-inset-top); background: {{THEME_DARK}}; z-index: 5; }
}
.app { position: fixed; left: 0; right: 0; top: 0; height: 100vh; display: flex; flex-direction: column; }
@supports (height: 100dvh) { .app { height: 100dvh; } }
.app.vv { top: var(--app-top, 0px); height: var(--app-h, 100dvh); }

/* Header */
.top {
  flex: none; display: flex; align-items: center; gap: 2px;
  padding: calc(env(safe-area-inset-top) + 6px) max(6px, env(safe-area-inset-right)) 6px max(14px, env(safe-area-inset-left));
  border-bottom: 1px solid var(--border); background: var(--bg);
}
.brand { flex: 1; min-width: 0; display: flex; align-items: center; gap: 10px; }
/* The DLNE logo image. Its dark tile gets rounded corners so it sits cleanly on the light theme too. */
.mark { flex: none; display: block; width: 36px; height: 36px; border-radius: 22%; }
.titles { min-width: 0; }
.titles h1 { margin: 0; font-size: 1rem; line-height: 1.25; font-weight: 650; }
.status { margin: 0; display: flex; align-items: center; gap: 6px; font-size: .78rem; line-height: 1.3; color: var(--muted); min-width: 0; white-space: nowrap; }
.status .model { overflow: hidden; text-overflow: ellipsis; min-width: 0; }
.dot { flex: none; width: 8px; height: 8px; border-radius: 50%; background: var(--faint); }
.dot.up { background: var(--ok); }
.dot.down { background: var(--down); }
.iconbtn {
  flex: none; min-width: 44px; height: 44px; padding: 0 10px; display: inline-flex; align-items: center; justify-content: center; gap: 6px;
  border: 0; border-radius: 12px; background: transparent; color: var(--text); font-size: .9rem; cursor: pointer;
}
.iconbtn:hover { background: var(--surface-2); }
.iconbtn svg { width: 22px; height: 22px; flex: none; }
.iconbtn .label { display: none; }
@media (min-width: 520px) { .iconbtn .label { display: inline; } }

/* Banners */
.banner { flex: none; margin: 0; padding: 8px 16px; font-size: .88rem; line-height: 1.4; text-align: center; background: var(--warn-bg); color: var(--warn-text); }

/* Conversation */
.log {
  flex: 1; min-height: 0; overflow-y: auto; overflow-x: hidden; overscroll-behavior-y: contain; -webkit-overflow-scrolling: touch;
  padding: 18px max(16px, env(safe-area-inset-right)) 24px max(16px, env(safe-area-inset-left));
}
.thread, .welcome { max-width: 760px; margin: 0 auto; }
.msg { display: flex; flex-direction: column; min-width: 0; margin: 0 0 20px; }
.msg.user { align-items: flex-end; }
.msg.user .body {
  max-width: 86%; padding: 10px 14px; border-radius: 18px 18px 6px 18px;
  background: var(--user); border: 1px solid var(--user-border); white-space: pre-wrap; overflow-wrap: anywhere;
}
.msg.assistant .body { max-width: 100%; min-width: 0; overflow-wrap: anywhere; }
.body p { margin: 0 0 .75em; }
.body > :last-child { margin-bottom: 0; }
.body .h { margin: 1.1em 0 .4em; }
.body > .h:first-child { margin-top: 0; }
.body ul, .body ol { margin: 0 0 .75em; padding-left: 1.4em; }
.body li { margin: .25em 0; }
.body li.sub { margin-left: 1.2em; list-style-type: circle; }
.body hr { border: 0; border-top: 1px solid var(--border); margin: 1em 0; }
.body a { color: var(--link); text-underline-offset: 2px; }
.body code { font: .88em/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace; background: var(--surface-2); padding: .1em .35em; border-radius: 6px; }
.code { margin: 0 0 .85em; border: 1px solid var(--border); border-radius: 12px; overflow: hidden; background: var(--code-bg); }
.code-bar { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 2px 4px 2px 12px; font-size: .78rem; color: var(--muted); border-bottom: 1px solid var(--border); }
.code pre { margin: 0; padding: 12px; overflow-x: auto; }
.code pre code { display: block; background: none; padding: 0; font-size: .84rem; white-space: pre; overflow-wrap: normal; }
.typing { display: flex; align-items: center; gap: 10px; color: var(--muted); font-size: .9rem; min-height: 26px; }
.dots { display: inline-flex; gap: 5px; }
.dots i { width: 7px; height: 7px; border-radius: 50%; background: var(--mark); opacity: .25; animation: blink 1.2s infinite ease-in-out; }
.dots i:nth-child(2) { animation-delay: .2s; }
.dots i:nth-child(3) { animation-delay: .4s; }
@keyframes blink { 0%, 80%, 100% { opacity: .2; } 40% { opacity: 1; } }
@media (prefers-reduced-motion: reduce) { .dots i { animation: none; opacity: .7; } }
.after { display: flex; flex-direction: column; align-items: flex-start; gap: 8px; margin-top: 8px; }
.note { margin: 0; padding: 10px 12px; border-radius: 12px; background: var(--surface); border: 1px solid var(--border); color: var(--muted); font-size: .92rem; }
.note.error { color: var(--err); }
.tag { font-size: .8rem; color: var(--faint); }
.actions { display: flex; flex-wrap: wrap; gap: 6px; }
.smallbtn {
  min-height: 40px; padding: 0 14px; border-radius: 12px; border: 1px solid var(--border);
  background: transparent; color: var(--muted); font-size: .86rem; cursor: pointer;
}
.smallbtn:hover { color: var(--text); background: var(--surface-2); }
.code-bar .smallbtn { min-height: 34px; border: 0; }
.smallbtn:disabled { opacity: .6; cursor: default; }
.smallbtn:disabled:hover { color: var(--muted); background: transparent; }

/* Web search */
.searching { display: flex; align-items: center; gap: 10px; margin: 0 0 4px; min-height: 26px; color: var(--muted); font-size: .9rem; overflow-wrap: anywhere; }
.pulse { flex: none; position: relative; width: 8px; height: 8px; border-radius: 50%; background: var(--mark); }
.pulse::after {
  content: ""; position: absolute; inset: -5px; border-radius: 50%; border: 2px solid var(--mark);
  opacity: 0; animation: ripple 1.6s ease-out infinite;
}
@keyframes ripple { 0% { transform: scale(.4); opacity: .7; } 100% { transform: scale(1.25); opacity: 0; } }
@media (prefers-reduced-motion: reduce) { .pulse::after { animation: none; } }
.cite { font-size: .72em; line-height: 1; vertical-align: .5em; margin: 0 1px; }
.cite a {
  display: inline-block; min-width: 1.6em; padding: 0 .35em; border-radius: 6px; line-height: 1.45; text-align: center;
  background: var(--surface-2); color: var(--link); font-weight: 650; text-decoration: none;
}
.extras { min-width: 0; }
/* Sources fold into one line ("10 sources · patriots.com, espn.com…") and open when tapped. */
.sources { margin-top: 12px; }
.sources > summary {
  display: inline-flex; align-items: center; gap: 8px; max-width: 100%; min-height: 36px; padding: 4px 14px 4px 10px;
  border: 1px solid var(--border); border-radius: 999px; background: var(--surface); color: var(--muted);
  font-size: .82rem; cursor: pointer; list-style: none; user-select: none; box-sizing: border-box;
}
.sources > summary::-webkit-details-marker { display: none; }
.sources > summary:hover, .sources > summary:focus-visible { border-color: var(--user-border); color: var(--text); }
.sources-chevron { flex: none; display: inline-block; width: 1em; text-align: center; transition: transform .15s; }
.sources[open] .sources-chevron { transform: rotate(90deg); }
@media (prefers-reduced-motion: reduce) { .sources-chevron { transition: none; } }
.sources-count { flex: none; color: var(--text); font-weight: 600; }
.sources-hosts { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sources[open] > summary { margin-bottom: 8px; }
.source-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.source {
  display: flex; align-items: center; gap: 10px; min-height: 48px; min-width: 0; padding: 6px 12px;
  border: 1px solid var(--border); border-radius: 12px; background: var(--surface); color: var(--text); text-decoration: none;
}
a.source:hover { border-color: var(--user-border); }
.source-n {
  flex: none; min-width: 1.7em; height: 1.7em; padding: 0 .3em; border-radius: 6px; display: inline-flex; align-items: center; justify-content: center;
  background: var(--surface-2); color: var(--link); font-size: .76rem; font-weight: 650;
}
.source.plain .source-n { color: var(--faint); }
.source-main { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.source-title, .source-meta { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.source-title { font-size: .9rem; line-height: 1.35; }
.source-meta { font-size: .76rem; line-height: 1.35; color: var(--muted); }
.search-note { margin: 10px 0 0; font-size: .8rem; line-height: 1.4; color: var(--faint); }

/* Empty state */
.welcome { text-align: center; padding: 5vh 0 8px; }
.welcome .mark { width: 96px; height: 96px; margin: 0 auto; }
.welcome h2 { margin: 14px 0 6px; font-size: 1.4rem; line-height: 1.3; font-weight: 650; }
.welcome .lead { margin: 0 auto 20px; max-width: 30em; color: var(--muted); }
.chips { display: flex; flex-direction: column; gap: 8px; max-width: 440px; margin: 0 auto 24px; }
.chip {
  min-height: 48px; padding: 10px 16px; text-align: left; border-radius: 14px; border: 1px solid var(--border);
  background: var(--surface); color: var(--text); font-size: .95rem; cursor: pointer;
}
.chip:hover { border-color: var(--user-border); }
.support { margin: 0; font-size: .85rem; color: var(--faint); text-wrap: balance; }
/* A link never breaks in two, and the padding makes a 13 px link an easier tap without changing the line. */
.support a { color: var(--muted); text-underline-offset: 2px; white-space: nowrap; padding: 8px 0; }

/* Composer */
.composer {
  flex: none; background: var(--bg); border-top: 1px solid var(--border);
  padding: 8px max(12px, env(safe-area-inset-right)) max(8px, env(safe-area-inset-bottom)) max(12px, env(safe-area-inset-left));
}
.kb .composer { padding-bottom: 8px; }
.field {
  max-width: 760px; margin: 0 auto; display: flex; align-items: flex-end; gap: 6px;
  padding: 4px 4px 4px 16px; border-radius: 24px; background: var(--surface); border: 1px solid var(--border);
}
.field:focus-within { border-color: var(--user-border); }
.field textarea {
  flex: 1; min-width: 0; min-height: 40px; max-height: 38vh; margin: 0; padding: 8px 0; resize: none; overflow-y: auto;
  border: 0; outline: none; background: transparent; color: var(--text); font: inherit; font-size: 16px; line-height: 1.5;
}
.field textarea::placeholder { color: var(--faint); }
.field textarea:disabled { opacity: .6; }
.round {
  flex: none; width: 44px; height: 44px; border-radius: 50%; border: 0; display: inline-flex; align-items: center; justify-content: center;
  background: var(--accent); color: var(--on-accent); cursor: pointer;
}
.round svg { width: 22px; height: 22px; }
.round:disabled { opacity: .35; cursor: default; }
.round.stop { background: var(--text); color: var(--bg); }
.hint { max-width: 760px; margin: 6px auto 0; text-align: center; font-size: .75rem; line-height: 1.35; color: var(--faint); }

/* The "+" at the left of the box and its popover: Photos, Camera, Create image */
.field { position: relative; }
.field.has-plus { padding-left: 4px; }
.round.plus { background: var(--surface-2); color: var(--text); }
.round.plus:hover { background: var(--border); }
.plus svg { transition: transform .15s; }
.plus[aria-expanded="true"] svg { transform: rotate(45deg); }
@media (prefers-reduced-motion: reduce) { .plus svg { transition: none; } }
.popover {
  position: absolute; left: 0; bottom: calc(100% + 8px); z-index: 6; min-width: 220px; padding: 6px;
  display: flex; flex-direction: column; gap: 2px;
  border: 1px solid var(--border); border-radius: 16px; background: var(--surface); box-shadow: 0 12px 32px rgba(0, 0, 0, .35);
}
.popitem {
  min-height: 48px; padding: 0 14px; border: 0; border-radius: 12px; background: transparent; color: var(--text);
  font-size: 1rem; text-align: left; cursor: pointer;
}
.popitem:hover { background: var(--surface-2); }

/* Photos waiting to be sent, above the box; and the same photos in the reader's own bubble */
.tray { max-width: 760px; margin: 0 auto 8px; }
.tray-thumbs { display: flex; flex-wrap: wrap; gap: 12px; margin: 0; padding: 8px 8px 0 0; list-style: none; }
.tray-item, .tray-wait { position: relative; width: 64px; height: 64px; }
.tray-item img, .tray-wait { display: block; object-fit: cover; border-radius: 12px; border: 1px solid var(--border); background: var(--surface-2); }
.tray-item img { width: 64px; height: 64px; }
.tray-remove {
  position: absolute; top: -9px; right: -9px; width: 26px; height: 26px; padding: 0; display: inline-flex; align-items: center;
  justify-content: center; border: 1px solid var(--border); border-radius: 50%; background: var(--surface); color: var(--text);
  font-size: 18px; line-height: 1; cursor: pointer;
}
.tray-remove::after { content: ""; position: absolute; inset: -9px; }   /* a 44 px target around the small circle */
.tray-note { margin: 6px 0 0; font-size: .8rem; line-height: 1.4; color: var(--faint); }
.tray-note:empty { display: none; }
/* Two to a row, so the bubble hugs its photos; a lone photo is shown larger */
.thumbs { display: grid; grid-template-columns: repeat(2, 112px); gap: 6px; margin: 0 0 8px; }
.thumbs.n1 { grid-template-columns: 180px; }
.thumbs.n1 .thumb { width: 180px; height: 180px; }
.body > .thumbs:last-child { margin-bottom: 0; }
.thumb { width: 112px; height: 112px; padding: 0; overflow: hidden; border: 1px solid var(--user-border); border-radius: 14px; background: var(--surface-2); }
button.thumb { cursor: zoom-in; }
.thumb img { display: block; width: 100%; height: 100%; object-fit: cover; }
.thumb.gone {
  display: flex; align-items: center; justify-content: center; padding: 8px; border-style: dashed; text-align: center;
  font-size: .75rem; line-height: 1.3; color: var(--muted);
}

/* "Use your location for this?" above the box, while a local question waits to be sent */
.askcard { max-width: 760px; margin: 0 auto 8px; padding: 12px 14px; border: 1px solid var(--user-border); border-radius: 16px; background: var(--surface); }
.askcard-title { margin: 0 0 2px; font-weight: 650; }
.askcard-text { margin: 0 0 6px; font-size: .92rem; color: var(--muted); }
.askcard-note { margin: 0 0 10px; font-size: .84rem; line-height: 1.4; color: var(--muted); }
.askcard-actions { display: flex; flex-wrap: wrap; gap: 8px; }
.askcard-actions .primary { width: auto; min-height: 44px; margin: 0; padding: 0 18px; }
.askcard-actions .smallbtn { min-height: 44px; }

/* Image cards: the job (elapsed time, Cancel) and then the picture */
a.smallbtn { display: inline-flex; align-items: center; text-decoration: none; }
.imgcard {
  width: min(100%, 460px); padding: 12px 14px; border: 1px solid var(--border); border-radius: 16px; background: var(--surface);
}
.imgcard:focus { outline: none; }
.imgcard-title { margin: 0 0 2px; font-size: .78rem; letter-spacing: .07em; text-transform: uppercase; color: var(--muted); }
.imgcard-prompt { margin: 0 0 10px; overflow-wrap: anywhere; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
.imgcard-line { display: flex; align-items: flex-start; gap: 12px; }
.imgcard-line .pulse { margin-top: 7px; margin-left: 5px; }
.imgcard-main { min-width: 0; }
.imgcard-status { margin: 0; font-size: .92rem; overflow-wrap: anywhere; }
.imgcard-status.error { color: var(--err); }
.imgcard-clock { margin: 2px 0 0; font-size: .8rem; color: var(--muted); font-variant-numeric: tabular-nums; }
.imgcard-pic { display: block; width: 100%; height: auto; margin-top: 10px; border-radius: 12px; background: var(--surface-2); }
.imgcard-actions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
.imgcard-keep { margin: 8px 0 0; font-size: .8rem; line-height: 1.4; color: var(--faint); }
.picture { margin-top: 12px; }   /* the card of a picture the reply made, under its text and sources */
#image-prompt, #image-size {
  display: block; width: 100%; margin: 0 0 14px; padding: 10px 12px;
  border: 1px solid var(--border); border-radius: 12px; background: var(--surface-2); color: var(--text); font: inherit; font-size: 16px;
}
#image-prompt { min-height: 96px; resize: vertical; }

/* Dialogs */
dialog {
  width: min(520px, calc(100% - 24px)); max-width: none; max-height: calc(100% - 24px); padding: 0; overflow: auto;
  border: 1px solid var(--border); border-radius: 20px; background: var(--surface); color: var(--text);
  box-shadow: 0 24px 64px rgba(0, 0, 0, .45);
}
dialog::backdrop { background: rgba(2, 8, 10, .62); }
.sheet { padding: 20px 20px calc(20px + env(safe-area-inset-bottom)); }
.sheet h2 { margin: 0 0 8px; font-size: 1.25rem; line-height: 1.3; }
.sheet h3 { margin: 22px 0 10px; font-size: .78rem; letter-spacing: .07em; text-transform: uppercase; color: var(--muted); }
.sheet p { margin: 0 0 12px; }
.sheet .muted { color: var(--muted); }
.sheet .small { font-size: .88rem; }
.sheet-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin: -6px -10px 4px 0; }
.sheet-head h2 { margin: 0; }
.center { text-align: center; }
.center .mark { width: 72px; height: 72px; margin: 0 auto 8px; }
.primary {
  display: block; width: 100%; min-height: 50px; margin-top: 8px; border: 0; border-radius: 14px;
  background: var(--accent); color: var(--on-accent); font-weight: 650; cursor: pointer;
}
.primary.install { margin: 0 0 14px; }
.linkrow {
  display: flex; align-items: center; justify-content: space-between; gap: 12px; min-height: 50px; margin: 0 0 8px; padding: 0 16px;
  border: 1px solid var(--border); border-radius: 14px; background: var(--surface-2); color: var(--text); text-decoration: none; font-weight: 550;
}
.linkrow::after { content: "\2197"; color: var(--muted); font-weight: 400; }
.recent { display: flex; flex-direction: column; gap: 6px; margin-bottom: 8px; }
.recent-item {
  display: flex; align-items: center; justify-content: space-between; gap: 12px; width: 100%; min-height: 48px; padding: 8px 14px;
  border: 1px solid var(--border); border-radius: 12px; background: var(--surface-2); text-align: left; cursor: pointer;
}
.recent-title { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.recent-when { flex: none; font-size: .8rem; color: var(--muted); }
.steps { margin: 0 0 12px; padding-left: 1.2em; }
.steps li { margin: 0 0 8px; }
.privacy { padding: 12px 14px; border-radius: 12px; background: var(--surface-2); color: var(--muted); font-size: .9rem; }
.privacy p { margin: 0 0 8px; }
.privacy p:last-child { margin: 0; }
.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
.reasons { margin: 0 0 16px; padding: 0; border: 0; display: flex; flex-direction: column; gap: 6px; }
.reason {
  display: flex; align-items: center; gap: 12px; min-height: 48px; padding: 0 14px; cursor: pointer;
  border: 1px solid var(--border); border-radius: 12px; background: var(--surface-2);
}
.reason:has(input:checked) { border-color: var(--accent); }
.reason input { flex: none; width: 20px; height: 20px; margin: 0; accent-color: var(--accent); }
.reasons:disabled .reason { opacity: .6; cursor: default; }
.field-label { display: block; margin: 0 0 6px; font-size: .88rem; color: var(--muted); }
#report-note {
  display: block; width: 100%; min-height: 84px; margin: 0; padding: 10px 12px; resize: vertical;
  border: 1px solid var(--border); border-radius: 12px; background: var(--surface-2); color: var(--text); font: inherit; font-size: 16px;
}
.report-status { margin: 12px 0 0; font-size: .92rem; }
.report-status.ok { color: var(--ok); }
.report-status.error { color: var(--err); }
@media (max-width: 599px) {
  #menu, #report, #image { width: 100%; margin: auto 0 0; max-height: 90%; border-width: 1px 0 0; border-radius: 20px 20px 0 0; }
}
/* A photo of the reader's, full screen: a tap anywhere closes it */
#photo { width: 100%; max-width: none; height: 100%; max-height: none; margin: 0; border: 0; border-radius: 0; background: rgba(0, 0, 0, .92); box-shadow: none; overflow: hidden; }
#photo[open] { display: flex; align-items: center; justify-content: center; }
.photo-view { display: flex; max-width: 100%; max-height: 100%; }
.photo-view img { max-width: 100vw; max-height: 100vh; object-fit: contain; }
@supports (height: 100dvh) { .photo-view img { max-height: 100dvh; } }
.photo-close { position: absolute; top: max(12px, env(safe-area-inset-top)); right: 12px; background: rgba(0, 0, 0, .55); color: #fff; border-color: rgba(255, 255, 255, .4); }
.photo-close:hover { background: rgba(0, 0, 0, .75); color: #fff; }
</style>
</head>
<body>
<div class="sbar" aria-hidden="true"></div>
<div class="app" id="app">
  <header class="top">
    <div class="brand">
      <img class="mark" src="{{ICON_192}}" width="36" height="36" alt="">
      <div class="titles">
        <h1>DLNE Chat</h1>
        <p class="status"><span class="dot" id="dot"></span><span id="serving">Connecting&hellip;</span><span id="sep" hidden>&middot;</span><span class="model" id="model"></span></p>
      </div>
    </div>
    <button type="button" class="iconbtn" id="new-chat" aria-label="New chat" title="New chat">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg><span class="label">New chat</span>
    </button>
    <button type="button" class="iconbtn" id="open-menu" aria-label="Menu" aria-haspopup="dialog" title="Menu">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M4 7h16M4 12h16M4 17h16"/></svg>
    </button>
  </header>
  <p class="banner" id="offline" role="status" hidden>You're offline. Your saved chats are here; sending works again when you reconnect.</p>
  <main class="log" id="log" aria-busy="false">
    <section class="welcome" id="welcome">
      <img class="mark" src="{{ICON_192}}" width="96" height="96" alt="">
      <h2>Ask anything</h2>
      <p class="lead">Free, and answered by DLNE's own server, so an answer can take a moment.</p>
      <div class="chips">
        <button type="button" class="chip" data-prompt="Explain how vaccines teach the immune system, in simple terms.">Explain something simply</button>
        <button type="button" class="chip" data-prompt="Help me write a short, polite email asking my landlord to fix the heating.">Help me write an email</button>
        <button type="button" class="chip" data-prompt="Give me a quick vegetarian dinner idea with a shopping list.">Plan a quick dinner</button>
      </div>
      <p class="support"><a href="{{WEBSITE_URL}}" target="_blank" rel="noopener noreferrer">Website</a> &middot; <a href="{{DISCORD_INVITE_URL}}" target="_blank" rel="noopener noreferrer">Discord</a> &middot; <a href="{{COFFEE_URL}}" target="_blank" rel="noopener noreferrer">Buy a coffee</a> &middot; <a href="{{EXTENSION_URL}}" target="_blank" rel="noopener noreferrer">Donate compute</a></p>
    </section>
    <div class="thread" id="thread" role="log"></div>
  </main>
  <p class="banner" id="bar" role="alert" hidden></p>
  <form class="composer" id="composer" autocomplete="off">
    <div class="askcard" id="ask-location" role="group" aria-labelledby="ask-location-title" hidden>
      <p class="askcard-title" id="ask-location-title">Use your location for this?</p>
      <p class="askcard-text">DLNE uses it only to look up the weather and places near you, and never keeps it.</p>
      <p class="askcard-note">Choosing "Use my location" keeps sharing on until you turn it off in the menu.</p>
      <div class="askcard-actions">
        <button type="button" class="primary" id="ask-location-yes">Use my location</button>
        <button type="button" class="smallbtn" id="ask-location-no">Not now</button>
      </div>
    </div>
    <div class="tray" id="tray" hidden>
      <ul class="tray-thumbs" id="tray-thumbs" aria-label="Photos to send"></ul>
      <p class="tray-note" id="tray-note" role="status"></p>
      <p class="tray-note" id="tray-hint">{{PHOTO_HINT}}</p>
    </div>
    <div class="field" id="field">
      <button type="button" class="round plus" id="plus" aria-label="Add" aria-expanded="false" aria-controls="plus-menu" hidden>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>
      </button>
      <div class="popover" id="plus-menu" hidden>
        <button type="button" class="popitem" id="add-photos">Photos</button>
        <button type="button" class="popitem" id="add-camera" hidden>Camera</button>
        <button type="button" class="popitem" id="make-image" aria-haspopup="dialog">Create image</button>
      </div>
      <textarea id="input" rows="1" maxlength="8000" placeholder="Message DLNE" aria-label="Message"></textarea>
      <button type="submit" class="round" id="send" aria-label="Send" disabled>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 19V5M5.5 11.5L12 5l6.5 6.5"/></svg>
      </button>
      <button type="button" class="round stop" id="stop" aria-label="Stop the answer" hidden>
        <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="7" y="7" width="10" height="10" rx="2" fill="currentColor"/></svg>
      </button>
    </div>
    <p class="hint">DLNE's server writes the answers (a volunteer's computer, if it is down). Don't share secrets.</p>
    <input type="file" id="pick-photos" accept="image/png,image/jpeg" multiple hidden>
    <input type="file" id="pick-camera" accept="image/*" capture="environment" hidden>
  </form>
</div>

<dialog id="first-run" aria-labelledby="first-run-title">
  <div class="sheet center">
    <img class="mark" src="{{ICON_192}}" width="72" height="72" alt="">
    <h2 id="first-run-title">Before you start</h2>
    <p>{{PRIVACY_NOTICE}}</p>
    <p class="muted small">DLNE Chat is free. Your chat history stays on this device.</p>
    <button type="button" class="primary" id="first-run-ok">I understand</button>
  </div>
</dialog>

<dialog id="menu" aria-labelledby="menu-title">
  <div class="sheet">
    <div class="sheet-head">
      <h2 id="menu-title">DLNE Chat</h2>
      <button type="button" class="iconbtn" id="close-menu" aria-label="Close">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>
      </button>
    </div>
    <p class="muted small" id="about">Answers come from DLNE's own server; a volunteer's computer answers only if it is restarting or down. They can be slow, and they can be wrong.</p>
    <h3>Your chats</h3>
    <div class="recent" id="recent"></div>
    <button type="button" class="smallbtn" id="clear-all">Delete all chats</button>
    <h3>Location</h3>
    <div class="reasons"><label class="reason"><input type="checkbox" id="share-location"> Share location</label></div>
    <p class="muted small">Off unless you turn it on. When a local question needs it and it is off, the chat asks you first; choosing Use my location there turns this on, and it stays on until you turn it off here. Your device's precise location goes to DLNE's own server only with a local question (near me, nearby, weather, forecast, rain, snow, my location, my area and the like, or a follow-up to one), and only while it is fresh (under two minutes old). Other questions never ask your phone for it. If none is ready, the question goes without one and the answer says so. It is used only for lookups that need it: local weather (rounded to about 100 m, sent to Open-Meteo) and local searches (only a nearby place name). The AI model never gets your coordinates, only the town named in those lookups' results. Nothing is logged or stored.</p>
    <p class="report-status error" id="location-status" role="status" hidden></p>
    <h3>More from DLNE</h3>
    <a class="linkrow" href="{{WEBSITE_URL}}" target="_blank" rel="noopener noreferrer">DLNE website</a>
    <a class="linkrow" href="{{DISCORD_INVITE_URL}}" target="_blank" rel="noopener noreferrer">Discord</a>
    <h3>Support DLNE</h3>
    <a class="linkrow" href="{{COFFEE_URL}}" target="_blank" rel="noopener noreferrer">Support DLNE: buy a coffee</a>
    <a class="linkrow" href="{{EXTENSION_URL}}" target="_blank" rel="noopener noreferrer">Donate compute at home</a>
    <p class="muted small">Lend your computer while you're not using it, with the DLNE extension for VS Code. Every machine makes answers faster for everyone.</p>
    <h3>Install on your phone</h3>
    <button type="button" class="primary install" id="install" hidden>Install DLNE Chat</button>
    <ul class="steps">
      <li><strong>iPhone:</strong> open this page in Safari, tap Share, then Add to Home Screen.</li>
      <li><strong>Android:</strong> use the DLNE app, or open the Chrome menu (&#8942;) and tap Install app.</li>
    </ul>
    <div class="privacy">
      <p>{{PRIVACY_NOTICE}}</p>
      <p>{{LOOKUP_PRIVACY}}</p>
      <p>{{PHOTO_PRIVACY}}</p>
      <p>{{PICTURE_PRIVACY}}</p>
      <p>{{LOCATION_PRIVACY}}</p>
    </div>
  </div>
</dialog>

<dialog id="image" aria-labelledby="image-title">
  <form class="sheet" id="image-form" novalidate>
    <div class="sheet-head">
      <h2 id="image-title">Create an image</h2>
      <button type="button" class="iconbtn" id="close-image" aria-label="Close">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>
      </button>
    </div>
    <p class="muted small">Describe the picture you want. It takes a minute or two. It counts as one of your free chats, unless a computer on your network is contributing to DLNE.</p>
    <label class="field-label" for="image-prompt">Description</label>
    <textarea id="image-prompt" rows="4" maxlength="1000" aria-describedby="image-hint"></textarea>
    <label class="field-label" for="image-size">Size</label>
    <select id="image-size">
      <option value="768x768">Square, 768 x 768</option>
      <option value="1024x1024">Large square, 1024 x 1024</option>
      <option value="768x1024">Portrait, 768 x 1024</option>
      <option value="1024x768">Landscape, 1024 x 768</option>
    </select>
    <p class="muted small" id="image-hint">The picture is checked before you see it. It isn't saved with your chat: save it to keep it.</p>
    <p class="report-status error" id="image-status" role="alert" hidden></p>
    <button type="submit" class="primary" id="image-send">Create image</button>
  </form>
</dialog>

<dialog id="photo" aria-label="Your photo">
  <div class="photo-view" id="photo-box"></div>
  <button type="button" class="smallbtn photo-close" id="close-photo">Close</button>
</dialog>

<dialog id="report" aria-labelledby="report-title">
  <form class="sheet" id="report-form" novalidate>
    <div class="sheet-head">
      <h2 id="report-title">Report this answer</h2>
      <button type="button" class="iconbtn" id="close-report" aria-label="Close">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>
      </button>
    </div>
    <p class="muted small">Your question and this answer are sent with the report. {{REPORT_PRIVACY}}</p>
    <fieldset class="reasons" id="report-reasons">
      <legend class="sr-only">What is wrong with it?</legend>
      <label class="reason"><input type="radio" name="reason" value="harmful"> Harmful or dangerous</label>
      <label class="reason"><input type="radio" name="reason" value="wrong"> Wrong</label>
      <label class="reason"><input type="radio" name="reason" value="offensive"> Offensive</label>
      <label class="reason"><input type="radio" name="reason" value="other"> Something else</label>
    </fieldset>
    <label class="field-label" for="report-note">Anything to add? (optional)</label>
    <textarea id="report-note" rows="3" maxlength="500"></textarea>
    <p class="report-status" id="report-status" role="status" hidden></p>
    <button type="submit" class="primary" id="report-send" disabled>Send report</button>
  </form>
</dialog>

<script id="dlne-core">
// The page's pure parts: the stream reader, DLNE's in-band events, the Markdown renderer and the
// source list. They build DOM nodes and set textContent; nothing here ever parses HTML.
const DLNEChat = (() => {
  'use strict';
  const NO_SEARCH = "Web search isn't available right now, so this answer comes from the model's memory.";

  const str = (value, max) => (typeof value === 'string' ? value.replace(/\s+/g, ' ').trim().slice(0, max) : '');

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function button(cls, label, onClick) {
    const b = el('button', cls, label);
    b.type = 'button';
    b.addEventListener('click', onClick);
    return b;
  }

  // ---------------------------------------------------------------- links: http(s) only
  function safeUrl(href) {
    if (typeof href !== 'string') return null;
    try {
      const url = new URL(href);
      if (url.protocol === 'https:' || url.protocol === 'http:') return url.href;
    } catch (e) { /* not a URL */ }
    return null;
  }

  function link(href, label) {
    const url = safeUrl(href);
    if (!url) return document.createTextNode(label);
    const a = el('a', '', label);
    a.href = url;
    a.target = '_blank';
    a.rel = 'noopener noreferrer nofollow';
    return a;
  }

  // ---------------------------------------------------------------- web sources
  function cleanSources(list) {
    if (!Array.isArray(list)) return [];
    const out = [];
    const taken = {};
    list.slice(0, 20).forEach((s) => {
      if (!s || typeof s !== 'object') return;
      let n = Number(s.n);
      if (!Number.isInteger(n) || n < 1 || n > 99) n = out.length + 1;
      if (taken[n]) return;
      taken[n] = true;
      const url = str(s.url, 2048);
      out.push({ n, title: str(s.title, 300) || url || 'Source ' + n, url, host: str(s.host, 200), age: str(s.age, 60) });
    });
    return out;
  }

  function hostOf(source) {
    const url = safeUrl(source.url);
    if (url) {
      try { return new URL(url).hostname.replace(/^www\./, ''); } catch (e) { /* use the host given */ }
    }
    return source.host;
  }

  // The sources, folded into one line that opens when tapped: a long list pushed the answer out of view.
  // ``open``: show it opened (it was open before the list was replaced while the answer streamed).
  function sourceList(sources, open) {
    const box = el('details', 'sources');
    if (open) box.open = true;
    const summary = el('summary');
    const chevron = el('span', 'sources-chevron', '›');
    chevron.setAttribute('aria-hidden', 'true');
    summary.appendChild(chevron);
    summary.appendChild(el('span', 'sources-count', sources.length === 1 ? '1 source' : sources.length + ' sources'));
    const hosts = [...new Set(sources.map(hostOf).filter(Boolean))];
    if (hosts.length) {
      const more = hosts.length > 3 ? ' +' + (hosts.length - 3) : '';
      summary.appendChild(el('span', 'sources-hosts', '· ' + hosts.slice(0, 3).join(', ') + more));
    }
    box.appendChild(summary);
    const list = el('ol', 'source-list');
    sources.forEach((s) => {
      const url = safeUrl(s.url);
      const entry = el(url ? 'a' : 'div', url ? 'source' : 'source plain');
      if (url) {
        entry.href = url;
        entry.target = '_blank';
        entry.rel = 'noopener noreferrer nofollow';
      }
      entry.appendChild(el('span', 'source-n', String(s.n)));
      const main = el('span', 'source-main');
      main.appendChild(el('span', 'source-title', s.title));
      const meta = [hostOf(s), s.age].filter(Boolean).join(' · ');
      if (meta) main.appendChild(el('span', 'source-meta', meta));
      entry.appendChild(main);
      const item = el('li');
      item.appendChild(entry);
      list.appendChild(item);
    });
    box.appendChild(list);
    return box;
  }

  function cite(source) {
    const sup = el('sup', 'cite');
    const url = safeUrl(source.url);
    if (!url) { sup.textContent = String(source.n); return sup; }
    const a = el('a', '', String(source.n));
    a.href = url;
    a.target = '_blank';
    a.rel = 'noopener noreferrer nofollow';
    a.title = source.title;
    a.setAttribute('aria-label', 'Source ' + source.n + ': ' + source.title);
    sup.appendChild(a);
    return sup;
  }

  // ---------------------------------------------------------------- Markdown subset -> DOM
  const FENCE = /^ {0,3}(`{3,}|~{3,})\s*([\w#+.-]*)/;
  const INLINE = /`([^`\n]+)`|\*\*(\S(?:[^\n]*?\S)?)\*\*|(^|[^\w*])\*([^\s*](?:[^*\n]*?[^\s*])?)\*(?![\w*])|\[([^\]\n]+)\]\((https?:\/\/[^\s()]+)\)|\[(\d{1,2}(?:, ?\d{1,2})*)\](?!\()|(https?:\/\/[^\s<>"'`]+)/g;

  function isFenceEnd(line, marker) {
    const t = line.trim();
    return t.length >= marker.length && t.charAt(0) === marker.charAt(0) && /^(`+|~+)$/.test(t);
  }

  // ``sources``: the answer's web sources, so [n] markers can point at them.
  function markdown(target, text, sources) {
    clear(target);
    const lines = text.replace(/\r\n?/g, '\n').split('\n');
    let para = null;
    let list = null;
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      const fence = FENCE.exec(line);
      if (fence) {
        para = list = null;
        const code = [];
        let j = i + 1;
        while (j < lines.length && !isFenceEnd(lines[j], fence[1])) { code.push(lines[j]); j++; }
        target.appendChild(codeBlock(code.join('\n'), fence[2]));
        i = j;   // the closing fence, or past the end while the block is still arriving
        continue;
      }
      if (!line.trim()) { para = list = null; continue; }
      const heading = /^ {0,3}#{1,6}\s+(.*?)[\s#]*$/.exec(line);
      if (heading) {
        para = list = null;
        const h = el('p', 'h');
        const strong = el('strong');
        inline(strong, heading[1], sources);
        h.appendChild(strong);
        target.appendChild(h);
        continue;
      }
      if (/^ {0,3}([-*_])( *\1){2,} *$/.test(line)) { para = list = null; target.appendChild(el('hr')); continue; }
      const item = /^( *)(?:([-*+])|(\d{1,9})[.)])\s+(.*)$/.exec(line);
      if (item) {
        para = null;
        const tag = item[2] ? 'UL' : 'OL';
        if (!list || list.tagName !== tag) {
          list = el(tag.toLowerCase());
          if (item[3] && item[3] !== '1') list.start = parseInt(item[3], 10);
          target.appendChild(list);
        }
        const li = el('li', item[1].length >= 2 ? 'sub' : '');
        inline(li, item[4], sources);
        list.appendChild(li);
        continue;
      }
      if (list && /^\s{2,}\S/.test(line) && list.lastElementChild) {
        list.lastElementChild.appendChild(el('br'));
        inline(list.lastElementChild, line.trim(), sources);
        continue;
      }
      list = null;
      if (para) para.appendChild(el('br'));
      else { para = el('p'); target.appendChild(para); }
      inline(para, line, sources);
    }
  }

  function inline(parent, text, sources) {
    const re = new RegExp(INLINE.source, 'g');
    let last = 0;
    let m;
    while ((m = re.exec(text)) !== null) {
      if (m.index > last) parent.appendChild(document.createTextNode(text.slice(last, m.index)));
      if (m[1] !== undefined) {
        parent.appendChild(el('code', '', m[1]));
      } else if (m[2] !== undefined) {
        const strong = el('strong');
        inline(strong, m[2], sources);
        parent.appendChild(strong);
      } else if (m[4] !== undefined) {
        if (m[3]) parent.appendChild(document.createTextNode(m[3]));
        const em = el('em');
        inline(em, m[4], sources);
        parent.appendChild(em);
      } else if (m[5] !== undefined) {
        parent.appendChild(link(m[6], m[5]));
      } else if (m[7] !== undefined) {
        // [n] (or [n, m]) cites the answer's sources; a number outside the list stays text.
        const cited = m[7].split(',').map((n) => (sources || []).find((s) => s.n === parseInt(n, 10)));
        if (cited.every(Boolean)) cited.forEach((s) => parent.appendChild(cite(s)));
        else parent.appendChild(document.createTextNode(m[0]));
      } else {
        let url = m[8];
        const tail = /[.,;:!?)\]}*_]+$/.exec(url);
        if (tail) url = url.slice(0, -tail[0].length);
        parent.appendChild(link(url, url));
        if (tail) parent.appendChild(document.createTextNode(tail[0]));
      }
      last = re.lastIndex;
    }
    if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
  }

  function codeBlock(code, lang) {
    const box = el('div', 'code');
    const bar = el('div', 'code-bar');
    bar.appendChild(el('span', '', lang || 'code'));
    const copy = button('smallbtn', 'Copy', () => copyText(code, copy));
    bar.appendChild(copy);
    const pre = el('pre');
    pre.appendChild(el('code', '', code));
    box.appendChild(bar);
    box.appendChild(pre);
    return box;
  }

  function copyText(text, btn) {
    if (!btn.dataset.label) btn.dataset.label = btn.textContent;
    const done = () => {
      btn.textContent = 'Copied';
      setTimeout(() => { btn.textContent = btn.dataset.label; }, 1600);
    };
    const fallback = () => {
      const area = el('textarea');
      area.value = text;
      area.setAttribute('readonly', '');
      area.style.position = 'fixed';
      area.style.opacity = '0';
      document.body.appendChild(area);
      area.select();
      let ok = false;
      try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
      area.remove();
      if (ok) done();
    };
    if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done, fallback);
    else fallback();
  }

  // ---------------------------------------------------------------- the answer stream
  // The status line while the coordinator looks something up, worded by the lookup tool. The
  // query is only ever put on the page as text.
  function lookupLine(tool, query) {
    const q = query ? '“' + query + '”' : '';
    if (tool === 'weather') return q ? 'Checking the weather for ' + q + '…' : 'Checking the weather…';
    if (tool === 'wikipedia') return q ? 'Looking up ' + q + ' on Wikipedia…' : 'Looking it up on Wikipedia…';
    if (tool === 'news') return q ? 'Checking the news for ' + q + '…' : 'Checking the news…';
    if (tool === 'web_search' || tool === undefined || tool === null) {   // no tool: an older coordinator's web search
      return q ? 'Searching the web for ' + q + '…' : 'Searching the web…';
    }
    return q ? 'Looking up ' + q + '…' : 'Looking it up…';
  }

  function cleanNotes(list) {
    return Array.isArray(list) ? list.map((n) => str(n, 300)).filter(Boolean).slice(0, 4) : [];
  }

  // DLNE's own events ride in the stream as {"dlne": {...}} with no "choices".
  function applyDlne(reply, event) {
    if (!event || typeof event !== 'object') return false;
    if (event.event === 'searching') {
      reply.searching = lookupLine(event.tool, str(event.query, 300));   // several lookups: the latest shows
      return true;
    }
    if (event.event === 'sources') {
      // Each "sources" event carries the whole list so far: replace, never append.
      const sources = cleanSources(event.sources);
      if (sources.length) reply.sources = sources;
      else delete reply.sources;
      delete reply.searching;
      return true;
    }
    if (event.event === 'search_note') {
      const note = str(event.note, 300);
      const notes = reply.lookupNotes || [];
      if (!note || notes.indexOf(note) !== -1 || notes.length >= 4) return false;
      reply.lookupNotes = notes.concat(note);
      return true;
    }
    if (event.event === 'search_unavailable') {
      reply.searchNote = str(event.reason, 500) || NO_SEARCH;
      delete reply.searching;
      return true;
    }
    if (event.event === 'retract') {
      // Server 2.3.5: the model began answering from memory while unsure, so the server takes that
      // text back and looks the question up instead. Forget everything shown so far; the usual
      // "searching", "sources" and a fresh answer follow, and paint() redraws the whole answer.
      reply.content = '';
      delete reply.sources;
      delete reply.lookupNotes;
      delete reply.searchNote;
      return true;
    }
    if (event.event === 'image_job') {
      // Server 2.6.2: the model asked for a picture and the job was made. It becomes the reply's own job card, polled
      // like a card made from the sheet. One picture per answer: a second event (the server sends none) is ignored.
      if (reply.job) return false;
      const job = jobFromEvent(event, Date.now());
      if (!job) return false;
      reply.job = job;
      return true;
    }
    if (event.event === 'image_refused') {
      // The picture was not made (the term list, the free trial, the hourly cap, a full queue...). The server's own
      // words go under the reply as a quiet note; the model's reply says the same in its own words, and the history
      // gets nothing, since no picture was created.
      reply.pictureNote = str(event.detail, 500) || "The picture couldn't be made.";
      return true;
    }
    return false;
  }

  // A saved message, cleaned on its way back from the phone's storage (null: drop it).
  function cleanMessage(m) {
    if (!m || (m.role !== 'user' && m.role !== 'assistant') || typeof m.content !== 'string') return null;
    if (m.kind === 'image' && m.role === 'assistant') return cleanImageCard(m);
    const kept = { role: m.role, content: m.content };
    if (typeof m.status === 'string') kept.status = m.status;
    if (typeof m.note === 'string') kept.note = m.note;
    if (m.reported === true) kept.reported = true;
    // Only how many photos a turn had is kept: the photos themselves stay in memory, so after a reload the turn says so.
    if (kept.role === 'user' && Number.isInteger(m.photos) && m.photos >= 1 && m.photos <= MAX_PHOTOS) kept.photos = m.photos;
    if (kept.role === 'assistant') {
      const sources = cleanSources(m.sources);
      if (sources.length) kept.sources = sources;
      const notes = cleanNotes(m.lookupNotes);
      if (notes.length) kept.lookupNotes = notes;
      if (typeof m.searchNote === 'string' && m.searchNote) kept.searchNote = str(m.searchNote, 500);
      if (typeof m.locationNote === 'string' && m.locationNote) kept.locationNote = str(m.locationNote, 300);
      if (typeof m.pictureNote === 'string' && m.pictureNote) kept.pictureNote = str(m.pictureNote, 500);
      const job = m.job && typeof m.job === 'object' ? cleanJob(m.job) : null;
      if (job) kept.job = job;
      if (!kept.content && !kept.note && !kept.job && !kept.pictureNote) return null;   // an answer that never started
    }
    return kept;
  }

  // A picture job as it is saved, in a card or in a reply: its prompt, size and state; never picture bytes. A job that
  // was running keeps its id so the page can pick polling up again; one without an id cannot be, so it ends as failed.
  function cleanJob(m) {
    const prompt = str(m.prompt, 1000);
    if (!prompt) return null;
    const known = IMAGE_ACTIVE.concat(IMAGE_FINAL);
    const kept = {
      prompt,
      size: IMAGE_SIZES.indexOf(m.size) !== -1 ? m.size : IMAGE_SIZES[0],
      state: known.indexOf(m.state) !== -1 ? m.state : 'failed',
    };
    if (IMAGE_ACTIVE.indexOf(kept.state) !== -1) {
      if (isImageId(m.imageId)) kept.imageId = m.imageId;
      else { kept.state = 'failed'; kept.note = 'The page was closed before this image was started.'; }
    }
    if (typeof m.startedAt === 'number' && isFinite(m.startedAt)) kept.startedAt = m.startedAt;
    if (typeof m.eta === 'number' && m.eta > 0 && m.eta < 3600) kept.eta = m.eta;
    if (!kept.note && typeof m.note === 'string' && m.note) kept.note = str(m.note, 500);
    return kept;
  }

  // A saved image card (made from the sheet): a message that is its own job.
  function cleanImageCard(m) {
    const job = cleanJob(m);
    return job && Object.assign({ role: 'assistant', kind: 'image', content: imagePlaceholder(job.prompt) }, job);
  }

  // The job an ``image_job`` event describes, as a card-like object the polling code can follow (null: no usable id).
  function jobFromEvent(event, now) {
    if (!isImageId(event.id)) return null;
    const job = {
      prompt: str(event.prompt, 1000) || 'A picture',
      size: IMAGE_SIZES.indexOf(event.size) !== -1 ? event.size : IMAGE_SIZES[0],
      state: 'queued', imageId: event.id, startedAt: now,
    };
    if (typeof event.eta_seconds === 'number' && event.eta_seconds > 0 && event.eta_seconds < 3600) job.eta = event.eta_seconds;
    return job;
  }

  // One "data:" payload applied to ``reply``: 'done' (the answer is over), 'changed' or 'skip'.
  function applyData(reply, data) {
    if (data === '[DONE]') return 'done';
    let event;
    try { event = JSON.parse(data); } catch (e) { return 'skip'; }
    if (!event || typeof event !== 'object') return 'skip';
    if (event.dlne !== undefined) return applyDlne(reply, event.dlne) ? 'changed' : 'skip';
    if (event.error) {
      reply.status = 'error';
      reply.note = typeof event.error === 'string' ? str(event.error, 500)
        : (str(event.error.message, 500) || 'The answer failed. Please try again.');
      return 'done';
    }
    const choice = Array.isArray(event.choices) ? event.choices[0] : null;
    const piece = choice && choice.delta && typeof choice.delta.content === 'string' ? choice.delta.content : '';
    if (!piece) return 'skip';
    delete reply.searching;
    if (choice.finish_reason === 'stop' && /^\n\n\[[^\n]*[a-z][^\n]*\]\s*$/i.test(piece)) {
      // The coordinator's own notice (no machine free, a machine dropped out), not the model's words.
      reply.status = 'notice';
      reply.note = piece.trim().replace(/^\[|\]$/g, '');
      return 'changed';
    }
    reply.content += piece;
    return 'changed';
  }

  // Server-sent events arrive in arbitrary network pieces: a "data:" line can be split across
  // reads (even inside a UTF-8 character), so the unfinished tail waits for its newline.
  // ``onData`` returns false to stop reading.
  function streamReader(onData) {
    const decoder = new TextDecoder();
    let buffer = '';
    let open = true;
    const line = (raw) => {
      const text = raw.endsWith('\r') ? raw.slice(0, -1) : raw;
      if (open && text.startsWith('data:') && onData(text.slice(5).replace(/^ /, '')) === false) open = false;
    };
    const drain = () => {
      let nl;
      while (open && (nl = buffer.indexOf('\n')) !== -1) {
        const raw = buffer.slice(0, nl);
        buffer = buffer.slice(nl + 1);
        line(raw);
      }
    };
    return {
      push(bytes) {
        if (open) { buffer += decoder.decode(bytes, { stream: true }); drain(); }
        return open;
      },
      end() {
        if (open) { buffer += decoder.decode(); drain(); if (buffer) line(buffer); }
        buffer = '';
        open = false;
      },
    };
  }

  // ---------------------------------------------------------------- reporting an answer
  const REASONS = ['harmful', 'wrong', 'offensive', 'other'];

  // The report for ``answer`` (a message in ``messages``): the question it answered, the answer
  // and the reason, cut to what the server accepts. null for a reason the server does not know.
  // Both are written as the model saw them (turnText): a question of photos alone says "[photo]", and an answer that
  // made a picture ends with the picture's description, which is what there is to review (no picture is sent).
  function reportPayload(messages, answer, reason, note) {
    if (REASONS.indexOf(reason) === -1 || !answer) return null;
    let question = '';
    for (let i = messages.indexOf(answer) - 1; i >= 0; i--) {
      if (messages[i].role === 'user') { question = turnText(messages[i], 0); break; }
    }
    return {
      question: String(question).slice(0, 4000),
      answer: String(turnText(answer, 0)).slice(0, 8000),
      reason,
      note: String(note || '').trim().slice(0, 500),
    };
  }

  // What the page says after the server answers a report with ``status``.
  function reportResult(status) {
    if (status >= 200 && status < 300) return { ok: true, text: "Thanks, we'll review it." };
    if (status === 404 || status === 405) return { ok: false, text: "Reporting isn't available on this server." };
    if (status === 429) return { ok: false, text: 'Too many reports right now.' };
    return { ok: false, text: "Couldn't send the report (HTTP " + status + '). Please try again.' };
  }

  // A UUID v4 from 16 random bytes (the caller supplies them: crypto.getRandomValues).
  function uuid4(bytes) {
    const b = Array.from(bytes, (n) => n & 255);
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    const hex = b.map((n) => (n + 256).toString(16).slice(1)).join('');
    return [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20)].join('-');
  }

  const CHAT_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
  const isChatId = (value) => typeof value === 'string' && CHAT_ID.test(value);

  // A position (the browser's coords) with at most five decimals, about 1 m. The server uses it
  // for lookups only and keeps nothing. null for anything that is not a real place on Earth.
  function place(coords) {
    const lat = coords ? coords.latitude : NaN;
    const lon = coords ? coords.longitude : NaN;
    if (typeof lat !== 'number' || typeof lon !== 'number' || !(Math.abs(lat) <= 90) || !(Math.abs(lon) <= 180)) return null;
    return { lat: Math.round(lat * 1e5) / 1e5, lon: Math.round(lon * 1e5) / 1e5 };
  }

  // ---------------------------------------------------------------- the location fix
  // Opt-in and in memory only. A fix is judged by the age the browser reports for the position
  // (position.timestamp), not by when the callback ran: the browser may hand back a cached one.
  const FIX_MAX_AGE = 2 * 60 * 1000;   // a fix older than this is never sent (the Android app's limit too)
  const FIX_REFRESH = 60 * 1000;       // asked for again once it is this old; the browser may not hand back an older one
  const FIX_WAIT = 3000;               // a send waits this long at most for a fix that is on its way
  const FIX_RETRY = 30 * 1000;         // after a failure, a send does not wait for another try for this long
  const LOCATION_TEXT = {
    denied: 'Location was refused, so sharing is off. Allow it in your browser or phone settings to use it.',
    unsupported: "This browser can't share a location.",
    unavailable: "Couldn't find your location just now. Messages go without it until it works.",
  };
  const NO_LOCATION_NOTE = "Your location wasn't ready, so this local question went without it. The answer may not be local to you.";

  // Whether a question is about where the person is: the only kind that wakes the GPS or carries a position.
  // The first phrases are the server's own near-me match (_NEAR_ME in engine/search.py); the rest are what the
  // model answers with the weather tool at the person's place, and the standing "where I am" wording. The
  // Android app uses the same list (LocalQuestion.kt): keep the two alike. Weather words in a message about a picture
  // ("a picture of a fox in the snow") are what to draw, not a question about the reader's place; a place phrase still counts.
  const LOCAL_PLACE = new RegExp('\\b(?:near me|nearby|near here|around here|close to me|around me' +
    '|my location|my current location|current location|my area|my city|my town|where i am|where i live)\\b', 'i');
  const LOCAL_WEATHER = /\b(?:weather|forecast|rain(?:ing)?|snow(?:ing)?)\b/i;
  const ABOUT_A_PICTURE = /\b(?:pictures?|images?|drawings?|paintings?|illustrations?|sketch(?:es)?|wallpapers?|draw|paint)\b/i;
  const isLocalQuestion = (text) => typeof text === 'string'
    && (LOCAL_PLACE.test(text) || (LOCAL_WEATHER.test(text) && !ABOUT_A_PICTURE.test(text)));
  // The latest user message or the one before it, so "and tomorrow?" after a weather question still counts.
  function asksLocal(messages) {
    return messages.filter((m) => m.role === 'user').slice(-2).some((m) => isLocalQuestion(m.content));
  }

  // Whether sending ``messages`` should first ask the reader to use their location (the card above the box): sharing is
  // off, this browser can share a location at all, the chat has not been answered "Not now", and the question is a local one.
  function shouldAskLocation(sharing, available, declined, messages) {
    return !sharing && !!available && !declined && asksLocal(messages);
  }

  // Whether the words held in the box still need the location card: there are words (a message of photos alone asks about
  // no place), and shouldAskLocation says they are a local question. The words can be edited while the card is up, so this is
  // asked again as they change: the card goes away when they are deleted or stop being a local question.
  function locationCardWanted(text, sharing, available, declined, messages) {
    const words = typeof text === 'string' ? text.trim() : '';
    return !!words && shouldAskLocation(sharing, available, declined, messages.concat([{ role: 'user', content: words }]));
  }

  // When the position was taken, in ``now``'s clock. A time that cannot be right (a browser with another
  // unit, a clock far off) counts from its arrival instead.
  function fixTime(position, now) {
    const t = position ? position.timestamp : NaN;
    if (typeof t !== 'number' || !isFinite(t) || t < now - 864e5 || t > now + 6e4) return now;
    return Math.min(t, now);
  }

  // ``geo``: navigator.geolocation; ``clock``: Date.now; ``report(kind)``: 'denied', 'unsupported' and
  // 'unavailable' (a problem, with LOCATION_TEXT) or '' (it works); ``wait``: how long a send may wait.
  function locator(geo, clock, report, wait) {
    let on = false;
    let fix = null;
    let fixAt = 0;
    let pending = null;     // the request in flight (a promise that never rejects)
    let failedAt = -Infinity;
    let generation = 0;     // bumped when sharing is switched, so an old answer is ignored
    const usable = () => fix !== null && clock() - fixAt < FIX_MAX_AGE;
    const stale = () => fix === null || clock() - fixAt >= FIX_REFRESH;
    const mayAsk = () => on && stale() && clock() - failedAt >= FIX_RETRY;

    function fail(kind) {   // a failed refresh drops the old fix: messages must not go with a place the phone left
      fix = null;
      fixAt = 0;
      failedAt = clock();
      report(kind);
    }

    function start() {
      if (pending) return;
      if (!geo) { on = false; report('unsupported'); return; }
      let done;
      const mine = new Promise((resolve) => { done = resolve; });
      pending = mine;
      const epoch = generation;
      const settle = (apply) => {
        if (pending === mine) pending = null;
        if (epoch === generation && on) apply();
        done();
      };
      const got = (position) => settle(() => {
        const spot = place(position && position.coords);
        if (spot) { fix = spot; fixAt = fixTime(position, clock()); }
        if (!spot || !usable()) fail('unavailable'); else report('');
      });
      const refused = (failure) => settle(() => {
        if (failure && failure.code === 1) { on = false; fail('denied'); } else fail('unavailable');
      });
      try {
        geo.getCurrentPosition(got, refused, { enableHighAccuracy: true, maximumAge: FIX_REFRESH, timeout: 10000 });
      } catch (e) { refused(null); }
    }

    return {
      get on() { return on; },
      get fix() { return usable() ? fix : null; },
      get available() { return !!geo; },   // whether this browser can share a location at all
      enable(value) {
        value = !!value;
        if (value === on) return;
        on = value;
        generation++;
        pending = null;
        fix = null;
        fixAt = 0;
        failedAt = -Infinity;
      },
      // Renews the fix in the background (when the setting is turned on, which is when the browser asks).
      touch() { if (mayAsk()) start(); },
      // The fix a message goes with, or null. Renews a stale one first; waits (briefly) only for a fix
      // that is on its way and not yet usable. ``limit`` is a longer wait for one send: the one that just turned
      // sharing on, while the browser's own question is on screen and waiting for the reader.
      async forSend(limit) {
        if (!on) return null;
        if (mayAsk()) start();
        if (!usable() && pending) {
          let timer;
          const cap = limit !== undefined ? limit : (wait === undefined ? FIX_WAIT : wait);
          await Promise.race([pending, new Promise((resolve) => { timer = setTimeout(resolve, cap); })]);
          clearTimeout(timer);
        }
        return on && usable() ? fix : null;
      },
    };
  }

  // ---------------------------------------------------------------- photos (server 2.6.2)
  // What the reader attaches is made ready in the page before anything is sent: decoded with its orientation applied,
  // scaled down and written again as a JPEG, which leaves every trace of EXIF and GPS behind. The limits are the
  // contract's; the server's own (engine/media.py) are looser, so no photo is ever refused for its size.
  const MAX_PHOTOS = 4;                    // per message; only one turn sends its photos as pictures, so per request too
  const PHOTO_MAX_PIXELS = 1200000;
  const PHOTO_MAX_SIDE = 2048;
  const PHOTO_MAX_BYTES = 4 * 1024 * 1024;
  const PHOTO_QUALITY = 0.85;
  const PHOTO_QUALITY_LOW = 0.7;
  const THUMB_SIDE = 240;                  // the tray and the bubbles show this small copy; a tap opens the whole photo
  const PHOTO_MARK = '[photo]';
  const PHOTO_GONE = 'Photo not kept after reload';
  const JPEG_URL = /^data:image\/jpeg;base64,[A-Za-z0-9+/]+={0,2}$/;
  // What a chosen file may be before anything is decoded (decoding a huge picture can take a phone's tab down): at most
  // 64 MiB, and, for a PNG or JPEG (the only two this page measures), at most 100 megapixels by its own header.
  const PHOTO_FILE_MAX_BYTES = 64 * 1024 * 1024;
  const PHOTO_FILE_MAX_PIXELS = 100 * 1000 * 1000;
  const NOT_A_PHOTO = "That file isn't a photo this page can open.";
  const PHOTO_TOO_BIG = 'That photo is too big to open here (over ' + PHOTO_FILE_MAX_BYTES / (1024 * 1024) + ' MB).';
  const PHOTO_TOO_MANY_PIXELS = 'That photo is too big to open here (over ' + PHOTO_FILE_MAX_PIXELS / 1e6 + ' megapixels).';

  const photoCount = (m) => (m && m.role === 'user' && Number.isInteger(m.photos) && m.photos > 0 ? Math.min(m.photos, MAX_PHOTOS) : 0);

  // The size to draw a photo at: never enlarged, at most ``maxPixels`` pixels and ``maxSide`` on a side, in its own shape.
  function fitSize(width, height, maxPixels, maxSide) {
    const whole = (n) => (Number.isFinite(n) && n >= 1 ? Math.floor(n) : 1);
    const w0 = whole(width);
    const h0 = whole(height);
    const scale = Math.min(1, maxSide / Math.max(w0, h0), Math.sqrt(maxPixels / (w0 * h0)));
    let w = Math.max(1, Math.floor(w0 * scale));
    let h = Math.max(1, Math.floor(h0 * scale));
    // The last digit of a float must not let the area or a side run past its limit.
    while (w * h > maxPixels || Math.max(w, h) > maxSide) { if (w >= h) w--; else h--; }
    return { width: w, height: h };
  }
  const photoSize = (width, height) => fitSize(width, height, PHOTO_MAX_PIXELS, PHOTO_MAX_SIDE);
  const thumbSize = (width, height) => fitSize(width, height, THUMB_SIDE * THUMB_SIDE, THUMB_SIDE);

  // The bytes a data: URL's base64 stands for.
  function dataUrlBytes(url) {
    const b64 = typeof url === 'string' ? url.slice(url.indexOf(',') + 1) : '';
    const pad = b64.endsWith('==') ? 2 : b64.endsWith('=') ? 1 : 0;
    return Math.max(0, Math.floor(b64.length / 4) * 3 - pad);
  }

  // ``end - start`` bytes of a file from ``start`` (fewer at its end), reading nothing else of it.
  function readBytes(blob, start, end) {
    const part = blob.slice(start, end);
    if (typeof part.arrayBuffer === 'function') return part.arrayBuffer().then((buffer) => new Uint8Array(buffer));
    return new Promise((resolve, reject) => {   // Safari before 14 has no Blob.arrayBuffer
      const reader = new FileReader();
      reader.onload = () => resolve(new Uint8Array(reader.result));
      reader.onerror = () => reject(reader.error || new Error('The file could not be read.'));
      reader.readAsArrayBuffer(part);
    });
  }

  const PNG_SIGNATURE = [0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A];
  // A JPEG's frame header is one of SOF0 to SOF15: every marker from C0 to CF but DHT (C4), JPG (C8) and DAC (CC).
  const JPEG_FRAME = [0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF];
  const be16 = (b, i) => (b[i] << 8) | b[i + 1];
  const be32 = (b, i) => ((b[i] << 24) | (b[i + 1] << 16) | (b[i + 2] << 8) | b[i + 3]) >>> 0;

  // The size a PNG or a JPEG says it has, from its header alone: { format, width, height }, or null for anything else (or a
  // header that says nothing usable). ``read(start, end)`` answers those bytes of the file and ``length`` is its size. A PNG
  // says it in its first 24 bytes. A JPEG is walked segment by segment, each segment naming its own length, so only four bytes
  // of each are read, and a long EXIF block or colour profile is skipped, not read.
  async function imageHeader(read, length) {
    const first = await read(0, 24);
    if (first.length >= 24 && PNG_SIGNATURE.every((v, i) => first[i] === v) && be32(first, 12) === 0x49484452) {   // "IHDR"
      const width = be32(first, 16);
      const height = be32(first, 20);
      return width > 0 && height > 0 ? { format: 'png', width, height } : null;
    }
    if (first.length < 4 || first[0] !== 0xFF || first[1] !== 0xD8) return null;
    let at = 2;
    for (let steps = 0; steps < 512 && at + 4 <= length; steps++) {
      const head = await read(at, at + 4);
      if (head.length < 2 || head[0] !== 0xFF) return null;
      const marker = head[1];
      if (marker === 0xFF) { at += 1; continue; }                                    // a fill byte
      if (marker === 0x01 || (marker >= 0xD0 && marker <= 0xD9)) {                    // markers that have no length: TEM, RSTn, SOI, EOI
        if (marker === 0xD9) return null;
        at += 2;
        continue;
      }
      if (marker === 0xDA || head.length < 4) return null;                            // the picture data began with no frame header before it
      const size = be16(head, 2);
      if (size < 2) return null;
      if (JPEG_FRAME.indexOf(marker) !== -1) {
        const frame = await read(at + 5, at + 9);                                     // after the marker, the length and the precision: height, width
        const height = frame.length >= 4 ? be16(frame, 0) : 0;
        const width = frame.length >= 4 ? be16(frame, 2) : 0;
        return width > 0 && height > 0 ? { format: 'jpeg', width, height } : null;
      }
      at += 2 + size;
    }
    return null;
  }

  // Why a chosen file is not opened, in plain words, or '' when it may be. Before anything is decoded: not an image, over 64 MiB,
  // or a PNG or JPEG whose own header says it has more than 100 megapixels; and a file whose header is neither (this page can
  // measure only those two, and they are what its Photos item offers) is not opened either.
  async function photoRefusal(file) {
    if (file.type && file.type.indexOf('image/') !== 0) return NOT_A_PHOTO;
    if (file.size > PHOTO_FILE_MAX_BYTES) return PHOTO_TOO_BIG;
    const head = await imageHeader((start, end) => readBytes(file, start, end), file.size);
    if (!head) return NOT_A_PHOTO;
    return head.width * head.height > PHOTO_FILE_MAX_PIXELS ? PHOTO_TOO_MANY_PIXELS : '';
  }

  // A decoded picture of ``width`` x ``height`` made ready to send. ``draw(w, h, quality)`` paints it at that size and
  // answers { url, bytes } (the page's canvas; a stand-in in tests). The first try is the scaled size at quality 85; a
  // result over 4 MiB is tried again at quality 70, then at half the size, and again, until it fits. ``thumb`` is the
  // small copy the tray and the bubbles show.
  async function fitPhoto(width, height, draw) {
    let size = photoSize(width, height);
    let quality = PHOTO_QUALITY;
    let made = await draw(size.width, size.height, quality);
    while (made.bytes > PHOTO_MAX_BYTES && (size.width > 1 || size.height > 1)) {
      if (quality === PHOTO_QUALITY) quality = PHOTO_QUALITY_LOW;
      else size = { width: Math.max(1, Math.floor(size.width / 2)), height: Math.max(1, Math.floor(size.height / 2)) };
      made = await draw(size.width, size.height, quality);
    }
    const little = thumbSize(size.width, size.height);
    const thumb = await draw(little.width, little.height, 0.8);
    if (!JPEG_URL.test(made.url) || !JPEG_URL.test(thumb.url)) throw new Error('This browser did not make a JPEG.');
    return { src: made.url, thumb: thumb.url, width: size.width, height: size.height, bytes: made.bytes };
  }

  // A turn in words, which is what the model reads of it when its photos do not go as pictures, and what a report says.
  // Its text; then, after a blank line, for a user turn one "[photo]" for each photo not sent as a picture (``sent`` of
  // them are), side by side; for an assistant turn that made a picture, "[Picture created: <prompt>]", so "make it
  // bluer" has a description to go on. A picture that was refused, failed or was cancelled adds nothing: none was created.
  // (The Android app writes both lines the same way, so one model reads the same history from either.)
  function turnText(m, sent) {
    const text = (typeof m.content === 'string' ? m.content : '').trim();
    let line = '';
    if (m.role === 'assistant') {
      if (m.job && m.job.prompt && !imageFailed(m.job)) line = '[Picture created: ' + m.job.prompt + ']';
    } else {
      line = Array(Math.max(0, photoCount(m) - (sent || 0))).fill(PHOTO_MARK).join(' ');
    }
    return line ? (text ? text + '\n\n' : '') + line : text;
  }

  // The messages a request carries: the newest that fit ``budget`` characters of text (the newest always goes), starting
  // with a user turn. ``photosOf(m)`` gives the photos the page still holds for a user turn. Only the newest user turn
  // that has photos sends them, as ``image_url`` parts after its text (a turn may be photos alone); every other photo
  // is only the text "[photo]". Pictures cost no characters here: the server counts each one itself.
  function wireMessages(messages, photosOf, budget) {
    const picked = [];
    let left = budget;
    let looking = true;   // the turn whose photos may go as pictures has not been met yet
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i];
      const count = photoCount(m);
      const held = looking && count ? (photosOf(m) || []).slice(0, MAX_PHOTOS) : [];
      if (count) looking = false;
      const text = turnText(m, held.length);
      if (!text && !held.length) continue;
      if (picked.length && text.length > left) break;
      left -= text.length;
      const pictures = held.map((p) => ({ type: 'image_url', image_url: { url: p.src } }));
      picked.unshift({ role: m.role, content: held.length ? (text ? [{ type: 'text', text }] : []).concat(pictures) : text });
    }
    while (picked.length > 1 && picked[0].role !== 'user') picked.shift();
    return picked;
  }

  // One photo waiting above the box, with the button that takes it out again.
  function trayItem(photo, index, onRemove) {
    const item = el('li', 'tray-item');
    const img = el('img');
    img.src = photo.thumb;
    img.alt = 'Photo ' + (index + 1);
    const remove = button('tray-remove', '×', onRemove);
    remove.setAttribute('aria-label', 'Remove photo ' + (index + 1));
    item.appendChild(img);
    item.appendChild(remove);
    return item;
  }

  // The photos of a user turn, above its text: a thumbnail that opens the photo full screen for each one the page still
  // holds, and a plain box that says so for each one it does not (the page was reloaded since).
  function photoStrip(count, held, open) {
    const strip = el('div', 'thumbs n' + count);
    for (let i = 0; i < count; i++) {
      const photo = held[i];
      if (!photo) { strip.appendChild(el('div', 'thumb gone', PHOTO_GONE)); continue; }
      const tap = button('thumb', undefined, () => open(photo));
      const img = el('img');
      img.src = photo.thumb;
      img.alt = 'Your photo ' + (i + 1) + ' of ' + count;
      tap.setAttribute('aria-label', 'Open photo ' + (i + 1) + ' of ' + count);
      tap.appendChild(img);
      strip.appendChild(tap);
    }
    return strip;
  }

  // ---------------------------------------------------------------- images (server 2.6.1)
  // A card in the conversation: { role: 'assistant', kind: 'image', content: '[Image: <prompt>]', prompt, size,
  // state, imageId (while running), startedAt, eta, note }. The text placeholder is all the chat model ever
  // sees and all a saved chat keeps; the picture itself stays in memory (the page's WeakMap). Since 2.6.2 a reply
  // can carry the same thing as ``reply.job`` ({ prompt, size, state, imageId, startedAt, eta, note }, no role): the
  // picture the model asked for, shown under the reply's text.
  const IMAGE_SIZES = ['768x768', '1024x1024', '768x1024', '1024x768'];
  const IMAGE_ACTIVE = ['queued', 'checking', 'generating', 'reviewing'];
  const IMAGE_FINAL = ['completed', 'refused', 'failed', 'cancelled', 'expired'];
  const IMAGE_ID = /^[A-Za-z0-9_-]{8,128}$/;
  const PNG_BASE64 = /^[A-Za-z0-9+/]+={0,2}$/;
  const IMAGE_NOT_KEPT = "This picture isn't saved with the chat. Save it to keep it.";
  const imageActive = (msg) => IMAGE_ACTIVE.indexOf(msg.state) !== -1;
  const imageFailed = (msg) => IMAGE_FINAL.indexOf(msg.state) !== -1 && msg.state !== 'completed';   // ended without a picture
  const isImageId = (value) => typeof value === 'string' && IMAGE_ID.test(value);
  const imagePlaceholder = (prompt) => '[Image: ' + str(prompt, 1000) + ']';

  function seconds(n) {
    const s = Math.max(0, Math.round(Number(n) || 0));
    return s < 120 ? s + ' s' : Math.floor(s / 60) + ' min' + (s % 60 ? ' ' + (s % 60) + ' s' : '');
  }
  const aboutSeconds = (n) => seconds(Math.max(5, Math.round(n / 5) * 5));

  // The estimate for ``size`` in seconds: the server's own figure if it has one, else the square's
  // scaled by area (the server only measures the two squares).
  function imageEta(etas, size) {
    const known = etas && typeof etas === 'object' ? etas : {};
    if (Number(known[size]) > 0) return Number(known[size]);
    const base = Number(known['768x768']) > 0 ? Number(known['768x768']) : 80;
    const dims = String(size).split('x').map(Number);
    return dims.length === 2 && dims[0] > 0 && dims[1] > 0 ? base * dims[0] * dims[1] / (768 * 768) : base;
  }

  function imageStatus(msg) {
    switch (msg.state) {
      case 'queued': return 'Waiting in line…';
      case 'checking': return 'Checking your request…';
      case 'generating': return 'Creating your image… about ' + aboutSeconds(msg.eta || imageEta(null, msg.size));
      case 'reviewing': return 'Checking the picture…';
      case 'completed': return 'Your image is ready.';
      case 'cancelled': return msg.note || 'Cancelled.';
      case 'refused': return msg.note || "That can't be made into an image.";
      case 'expired': return msg.note || 'This image is no longer available.';
      default: return msg.note || "The image couldn't be made. Please try again.";
    }
  }

  // Elapsed time against the estimate; there is no progress bar because the server has no step count.
  function imageClock(msg, now) {
    if (!imageActive(msg)) return '';
    const elapsed = Math.max(0, Math.floor((now - (msg.startedAt || now)) / 1000));
    const eta = msg.eta || imageEta(null, msg.size);
    return elapsed > eta + 10 ? seconds(elapsed) + ', taking longer than expected' : seconds(elapsed) + ' of about ' + aboutSeconds(eta);
  }

  // A poll's answer applied to the card. false: not a status this page knows (the card stays as it was).
  function imageApply(msg, job, now) {
    if (!job || typeof job !== 'object' || IMAGE_ACTIVE.concat(IMAGE_FINAL).indexOf(job.status) === -1) return false;
    msg.state = job.status;
    if (typeof job.elapsed_seconds === 'number' && isFinite(job.elapsed_seconds) && job.elapsed_seconds >= 0) {
      msg.startedAt = now - job.elapsed_seconds * 1000;
    }
    if (typeof job.eta_seconds === 'number' && job.eta_seconds > 0 && job.eta_seconds < 3600) msg.eta = job.eta_seconds;
    if (!imageActive(msg)) {
      delete msg.imageId;
      delete msg.cancelling;
      const detail = job.error && typeof job.error === 'object' ? str(job.error.detail, 500) : '';
      if (detail) msg.note = detail; else delete msg.note;
    }
    return true;
  }

  // The finished picture from a job's ``image``, or null if it is not a PNG we can show. It is shown through a
  // data: URL, the one image source the page's CSP allows besides its own icons.
  function imagePicture(image) {
    if (!image || typeof image !== 'object' || typeof image.b64_png !== 'string') return null;
    const data = image.b64_png.replace(/\s+/g, '');
    if (data.length < 40 || data.length > 24e6 || data.indexOf('iVBORw0KGgo') !== 0 || !PNG_BASE64.test(data)) return null;
    const side = (n) => (Number.isInteger(n) && n >= 1 && n <= 4096 ? n : 0);
    return {
      src: 'data:image/png;base64,' + data,
      width: side(image.width),
      height: side(image.height),
      seed: Number.isSafeInteger(image.seed) && image.seed >= 0 ? image.seed : null,
    };
  }

  // The bytes of a finished picture (its data: URL), to hand it to the share sheet as a file.
  function pictureBytes(picture) {
    const raw = atob(picture.src.slice(picture.src.indexOf(',') + 1));
    return Uint8Array.from(raw, (c) => c.charCodeAt(0));
  }

  // One image card. ``ctx``: picture() (the finished one, if the page holds it), now(), cancel(), retry(), and
  // optionally share() (where the browser can share a file), report(button) and reported() (a picture a reply made
  // can be reported: they show only with a picture). ``embedded``: the card sits inside a reply, so it is not wrapped
  // in a message row of its own. update() brings the card up to date in place, so the status line (a live region)
  // is announced once per change and a focused button is not rebuilt under the reader; tick() only moves the clock.
  function imageCard(msg, ctx, embedded) {
    const row = embedded ? null : el('div', 'msg assistant');
    const card = el('div', 'imgcard');
    card.setAttribute('role', 'group');
    card.setAttribute('aria-label', 'Image: ' + str(msg.prompt, 80));
    card.tabIndex = -1;
    card.appendChild(el('p', 'imgcard-title', 'Image'));
    card.appendChild(el('p', 'imgcard-prompt', msg.prompt));
    const line = el('div', 'imgcard-line');
    const pulse = el('span', 'pulse');
    pulse.setAttribute('aria-hidden', 'true');
    const main = el('div', 'imgcard-main');
    const status = el('p', 'imgcard-status');
    status.setAttribute('role', 'status');
    const clock = el('p', 'imgcard-clock');
    clock.setAttribute('aria-hidden', 'true');   // a counter that changes every second is not read out
    main.appendChild(status);
    main.appendChild(clock);
    line.appendChild(pulse);
    line.appendChild(main);
    card.appendChild(line);
    const pic = el('img', 'imgcard-pic');
    pic.hidden = true;
    card.appendChild(pic);
    const actions = el('div', 'imgcard-actions');
    const cancel = button('smallbtn', 'Cancel', () => ctx.cancel());
    const save = el('a', 'smallbtn', 'Save');
    const share = button('smallbtn', 'Share', () => ctx.share());
    const report = button('smallbtn', 'Report', () => ctx.report(report));
    report.setAttribute('aria-haspopup', 'dialog');
    const retry = button('smallbtn', 'Try again', () => ctx.retry());
    actions.appendChild(cancel);
    actions.appendChild(save);
    actions.appendChild(share);
    actions.appendChild(report);
    actions.appendChild(retry);
    card.appendChild(actions);
    const keep = el('p', 'imgcard-keep');
    card.appendChild(keep);
    if (row) row.appendChild(card);
    let shown = null;

    function tick() { clock.textContent = imageClock(msg, ctx.now()); }

    function update() {
      const picture = msg.state === 'completed' ? ctx.picture() : null;
      const active = imageActive(msg);
      const problem = !active && msg.state !== 'completed' && msg.state !== 'cancelled';
      const text = msg.state === 'completed' && !picture
        ? "This picture was made, but it can't be shown again after the page is reloaded." : imageStatus(msg);
      if (status.textContent !== text) status.textContent = text;
      status.className = 'imgcard-status' + (problem ? ' error' : '');
      pulse.hidden = !active;
      clock.hidden = !active;
      const hadFocus = typeof document !== 'undefined' && document.activeElement === cancel;
      cancel.hidden = !active;
      cancel.disabled = !!msg.cancelling;
      cancel.textContent = msg.cancelling ? 'Cancelling…' : 'Cancel';
      retry.hidden = active || msg.state === 'completed';
      retry.textContent = msg.state === 'refused' ? 'Change description' : 'Try again';
      if (picture) {
        if (shown !== picture) {
          shown = picture;
          pic.src = picture.src;
          if (picture.width && picture.height) { pic.setAttribute('width', picture.width); pic.setAttribute('height', picture.height); }
          pic.alt = 'Generated image: ' + str(msg.prompt, 400);
          save.href = picture.src;
          save.download = 'dlne-image-' + (picture.seed !== null ? picture.seed : ctx.now()) + '.png';
          save.setAttribute('aria-label', 'Save the image');
        }
      }
      pic.hidden = !picture;
      save.hidden = !picture;
      share.hidden = !(picture && ctx.share);
      const reportable = !!(picture && ctx.report);
      const reported = reportable && !!(ctx.reported && ctx.reported());
      report.hidden = !reportable;
      report.textContent = reported ? 'Reported' : 'Report';
      report.disabled = reported;
      keep.textContent = IMAGE_NOT_KEPT;
      keep.hidden = msg.state !== 'completed';
      if (hadFocus && cancel.hidden) (picture ? save : retry.hidden ? card : retry).focus();
      tick();
    }

    update();
    return { row: row || card, card, update, tick, cancel };
  }

  return {
    str, el, clear, button, safeUrl, link, markdown, copyText, cleanSources, sourceList, applyData, streamReader,
    lookupLine, cleanMessage, reportPayload, reportResult, uuid4, isChatId, place,
    locator, LOCATION_TEXT, NO_LOCATION_NOTE, FIX_MAX_AGE, FIX_REFRESH, isLocalQuestion, asksLocal, shouldAskLocation,
    IMAGE_SIZES, imageActive, isImageId, imagePlaceholder, imageEta, imageStatus, imageClock, imageApply, imagePicture, imageCard,
    aboutSeconds, seconds, pictureBytes,
    MAX_PHOTOS, PHOTO_GONE, photoCount, photoSize, thumbSize, dataUrlBytes, fitPhoto, turnText, wireMessages, trayItem, photoStrip,
    PHOTO_FILE_MAX_BYTES, PHOTO_FILE_MAX_PIXELS, readBytes, imageHeader, photoRefusal, locationCardWanted,
  };
})();
</script>
<script>
(() => {
  'use strict';
  const {
    el, clear, button, markdown, copyText, sourceList, applyData, streamReader, cleanMessage, reportPayload, reportResult,
    str, uuid4, isChatId, locator, LOCATION_TEXT, NO_LOCATION_NOTE, asksLocal, locationCardWanted, IMAGE_SIZES, imageActive, isImageId,
    imagePlaceholder, imageEta, imageApply, imagePicture, imageCard, aboutSeconds, pictureBytes,
    MAX_PHOTOS, photoCount, dataUrlBytes, fitPhoto, wireMessages, trayItem, photoStrip, photoRefusal,
  } = DLNEChat;
  const API = '/v1/app/chat/completions';
  const REPORT = '/v1/app/report';
  const STATUS = '/v1/cluster/status';
  const IMAGES = '/v1/app/images';
  const STORE = 'dlne.chat.v1';
  const SEEN = 'dlne.chat.noticeSeen';
  const SYSTEM = 'You are the assistant in DLNE Chat, a free phone app. Be helpful, clear and reasonably brief. ' +
    'Use Markdown lists, bold and code blocks only when they help.';
  const CONTEXT_CHARS = 24000;   // about 6000 tokens of history, inside one member slot's context
  const MAX_SAVED = 20;
  const PROMPT_WAIT = 60 * 1000;           // the send that turns sharing on waits this long for the reader to answer the browser's prompt

  const $ = (id) => document.getElementById(id);
  const ui = {
    app: $('app'), log: $('log'), thread: $('thread'), welcome: $('welcome'), form: $('composer'), input: $('input'),
    send: $('send'), stop: $('stop'), bar: $('bar'), offline: $('offline'), dot: $('dot'), serving: $('serving'),
    sep: $('sep'), model: $('model'), about: $('about'), menu: $('menu'), recent: $('recent'), firstRun: $('first-run'),
    install: $('install'), clearAll: $('clear-all'), report: $('report'), reportForm: $('report-form'),
    reportReasons: $('report-reasons'), reportNote: $('report-note'), reportStatus: $('report-status'),
    reportSend: $('report-send'), shareLocation: $('share-location'), locationStatus: $('location-status'),
    field: $('field'), plus: $('plus'), plusMenu: $('plus-menu'), addPhotos: $('add-photos'), addCamera: $('add-camera'),
    makeImage: $('make-image'), pickPhotos: $('pick-photos'), pickCamera: $('pick-camera'),
    tray: $('tray'), trayThumbs: $('tray-thumbs'), trayNote: $('tray-note'), trayHint: $('tray-hint'),
    askLocation: $('ask-location'), askYes: $('ask-location-yes'), askNo: $('ask-location-no'),
    photo: $('photo'), photoBox: $('photo-box'),
    image: $('image'), imageForm: $('image-form'), imagePrompt: $('image-prompt'),
    imageSize: $('image-size'), imageStatus: $('image-status'), imageSend: $('image-send'),
  };
  const coarse = !!(window.matchMedia && window.matchMedia('(pointer: coarse)').matches);
  // The share sheet takes a file only in some browsers (Web Share level 2: ``canShare`` says so for a PNG file). Where it
  // does not, a picture has Save alone: a Share button that did nothing would be worse than none.
  const canShare = (() => {
    try {
      return typeof navigator.share === 'function' && typeof navigator.canShare === 'function' && typeof File === 'function'
        && navigator.canShare({ files: [new File(['x'], 'dlne-image.png', { type: 'image/png' })] });
    } catch (e) { return false; }
  })();

  // ---------------------------------------------------------------- storage (may be unavailable)
  const store = {
    get(key) { try { return window.localStorage.getItem(key); } catch (e) { return null; } },
    set(key, value) { try { window.localStorage.setItem(key, value); return null; } catch (e) { return e || new Error('storage'); } },
  };
  const newChatId = () => uuid4(crypto.getRandomValues(new Uint8Array(16)));
  const fresh = () => ({
    id: Date.now().toString(36) + Math.random().toString(36).slice(2, 8), chatId: newChatId(), updated: Date.now(), messages: [],
  });
  let current = fresh();
  let saved = [];
  let draft = '';

  function cleanConversation(c) {
    if (!c || typeof c !== 'object' || !Array.isArray(c.messages)) return null;
    const messages = c.messages.map(cleanMessage).filter(Boolean);
    const kept = { id: String(c.id || fresh().id), updated: Number(c.updated) || Date.now(), messages };
    if (isChatId(c.chatId)) kept.chatId = c.chatId;   // an older chat gets one when it is next sent
    if (c.declinedLocation === true) kept.declinedLocation = true;   // "Not now" to the location card: not asked again here
    return kept;
  }

  function load() {
    const raw = store.get(STORE);
    if (!raw) return;
    try {
      const data = JSON.parse(raw) || {};
      current = cleanConversation(data.current) || fresh();
      saved = (Array.isArray(data.saved) ? data.saved : []).map(cleanConversation)
        .filter((c) => c && c.messages.length).slice(0, MAX_SAVED);
      draft = typeof data.draft === 'string' ? data.draft : '';
    } catch (e) { /* a damaged store starts over */ }
  }

  function save() {
    for (let attempt = 0; attempt < 6; attempt++) {
      const failure = store.set(STORE, JSON.stringify({ current, saved, draft }));
      if (!failure) return;
      // Storage full: forget the oldest saved chats and try again. Storage off: keep chats in memory only.
      if (!saved.length || !/quota/i.test(String(failure.name || failure))) return;
      saved = saved.slice(0, Math.floor(saved.length / 2));
    }
  }

  // ---------------------------------------------------------------- the conversation on screen
  let busy = false;
  let controller = null;
  let live = null;          // the answer being written: { msg, view }
  let frame = 0;
  let stick = true;         // follow the answer while the reader is at the bottom
  let cooldownUntil = 0;
  let cooldownTimer = 0;

  function typing(text) {
    const box = el('div', 'typing');
    const dots = el('span', 'dots');
    dots.appendChild(el('i'));
    dots.appendChild(el('i'));
    dots.appendChild(el('i'));
    box.appendChild(dots);
    box.appendChild(el('span', '', text));
    return box;
  }

  const viewOf = new WeakMap();   // message -> its view, so a report made from the picture can update the answer's own button

  // An answer's row: [searching status] [body] [sources, search note] [the picture it made] [notes and actions].
  function view(msg) {
    if (msg.kind === 'image') return pictureView(msg);
    const row = el('div', 'msg ' + msg.role);
    const v = { row, msg, body: el('div', 'body'), status: null, extras: null, pictureBox: null, job: null, query: null, extrasKey: null };
    if (msg.role === 'assistant') {
      v.status = el('p', 'searching');
      v.status.setAttribute('role', 'status');
      v.status.hidden = true;
      row.appendChild(v.status);
    }
    row.appendChild(v.body);
    if (msg.role === 'assistant') {
      v.extras = el('div', 'extras');
      row.appendChild(v.extras);
      v.pictureBox = el('div', 'picture');
      v.pictureBox.hidden = true;
      row.appendChild(v.pictureBox);
    }
    viewOf.set(msg, v);
    paint(v, live !== null && live.msg === msg);
    return v;
  }

  // The reader's own turn: its photos as thumbnails above the text, or just the text.
  function paintUser(v) {
    const m = v.msg;
    const count = photoCount(m);
    if (!count) { v.body.textContent = m.content; return; }
    clear(v.body);
    v.body.appendChild(photoStrip(count, photoData.get(m) || [], showPhoto));
    if (m.content) v.body.appendChild(el('div', 'said', m.content));
  }

  // The picture this reply is making (an image_job event) sits under its text as a card of its own. It is polled by
  // the code below, not by repainting, so painting only has to put the card in once.
  function showJob(v) {
    const job = v.msg.job;
    if (!job || v.job === job) return;
    v.job = job;
    clear(v.pictureBox);
    const card = imageCard(job, {
      picture: () => pictures.get(job) || null, now: Date.now, cancel: () => cancelImage(job), retry: () => openImage(job),
      share: canShare ? () => sharePicture(job) : undefined,
      report: (btn) => openReport(v.msg, btn), reported: () => !!v.msg.reported,
    }, true);
    cardViews.set(job, card);
    v.pictureBox.appendChild(card.card);
    v.pictureBox.hidden = false;
  }

  function paint(v, streaming) {
    const m = v.msg;
    if (m.role === 'user') { paintUser(v); return; }
    // The lookup in progress (the latest of up to four), as text; rebuilt only when it changes so
    // the animation runs smoothly.
    const line = streaming && typeof m.searching === 'string' ? m.searching : null;
    if (line !== v.query) {
      v.query = line;
      clear(v.status);
      if (line !== null) {
        v.status.appendChild(el('span', 'pulse'));
        v.status.appendChild(el('span', '', line));
      }
      v.status.hidden = line === null;
    }
    if (streaming && !m.content) {
      clear(v.body);
      v.body.hidden = line !== null;
      if (line === null) v.body.appendChild(typing(m.sources ? 'Reading the sources…' : 'Waiting for an answer…'));
    } else {
      markdown(v.body, m.content, m.sources);
      v.body.hidden = !m.content;
    }
    const key = JSON.stringify([m.sources || [], m.lookupNotes || [], m.searchNote || '', m.locationNote || '', m.pictureNote || '']);
    if (key !== v.extrasKey) {   // sources are replaced as lookups finish
      v.extrasKey = key;
      const wasOpen = !!(v.sourcesBox && v.sourcesBox.open);   // a list the reader opened stays open
      clear(v.extras);
      v.sourcesBox = m.sources ? sourceList(m.sources, wasOpen) : null;
      if (v.sourcesBox) v.extras.appendChild(v.sourcesBox);
      (m.lookupNotes || []).forEach((note) => v.extras.appendChild(el('p', 'search-note', note)));
      if (m.searchNote) v.extras.appendChild(el('p', 'search-note', m.searchNote));
      if (m.locationNote) v.extras.appendChild(el('p', 'search-note', m.locationNote));
      if (m.pictureNote) v.extras.appendChild(el('p', 'search-note', m.pictureNote));   // the picture was not made: the server's words
      v.extras.hidden = !v.extras.firstChild;
    }
    showJob(v);
    if (!streaming) footer(v);
  }

  function footer(v) {
    const m = v.msg;
    const old = v.row.querySelector('.after');
    if (old) old.remove();
    const after = el('div', 'after');
    if (m.status === 'stopped') after.appendChild(el('span', 'tag', 'Stopped'));
    if (m.note) after.appendChild(el('p', m.status === 'error' ? 'note error' : 'note', m.note));
    const actions = el('div', 'actions');
    if (m.content.trim()) {
      const copy = button('smallbtn', 'Copy', () => copyText(m.content.trim(), copy));
      actions.appendChild(copy);
    }
    if (!busy && current.messages[current.messages.length - 1] === m) actions.appendChild(button('smallbtn', 'Try again', retry));
    if (m.content.trim()) {
      const report = button('smallbtn', m.reported ? 'Reported' : 'Report', () => openReport(m, report));
      report.disabled = !!m.reported;
      report.setAttribute('aria-haspopup', 'dialog');
      actions.appendChild(report);
    }
    if (actions.firstChild) after.appendChild(actions);
    if (after.firstChild) v.row.appendChild(after);
  }

  function unanswered() {
    const row = el('div', 'msg assistant');
    const after = el('div', 'after');
    after.appendChild(el('p', 'note', 'This message has no answer yet.'));
    const actions = el('div', 'actions');
    actions.appendChild(button('smallbtn', 'Send again', () => { if (!busy && Date.now() >= cooldownUntil) ask(); }));
    after.appendChild(actions);
    row.appendChild(after);
    return row;
  }

  function renderAll() {
    clear(ui.thread);
    if (live) live.view = null;
    current.messages.forEach((m) => {
      const v = view(m);
      if (live && live.msg === m) live.view = v;
      ui.thread.appendChild(v.row);
    });
    const last = current.messages[current.messages.length - 1];
    if (last && last.role === 'user' && !busy) ui.thread.appendChild(unanswered());
    ui.welcome.hidden = current.messages.length > 0;
    stick = true;
    scrollToEnd(true);
  }

  function scrollToEnd(force) { if (force || stick) ui.log.scrollTop = ui.log.scrollHeight; }
  ui.log.addEventListener('scroll', () => {
    stick = ui.log.scrollHeight - ui.log.scrollTop - ui.log.clientHeight < 80;
  }, { passive: true });

  function schedule() {
    if (frame) return;
    frame = requestAnimationFrame(() => {
      frame = 0;
      if (live && live.view) { paint(live.view, true); scrollToEnd(false); }
    });
  }

  // ---------------------------------------------------------------- asking
  // What the request carries (DLNEChat.wireMessages): the history that fits, with the photos the page still holds for the
  // newest turn that has any and a "[photo]" for every other one. Photos the server refused are not held back for another
  // try in later messages: they are "[photo]" too.
  function context(messages) {
    const held = (m) => (refusedPhotos.has(m) ? [] : photoData.get(m) || []);
    return [{ role: 'system', content: SYSTEM }].concat(wireMessages(messages, held, CONTEXT_CHARS));
  }

  // ``answered``: how the location card was answered ('yes' or 'no') when this is the send it was holding.
  function submit(answered) {
    const text = ui.input.value.trim();
    if ((!text && !tray.length) || preparing || busy || Date.now() < cooldownUntil) return;
    // A local question with sharing off asks first, in a card above the box: nothing is sent until it is answered.
    if (!answered && locationCardWanted(text, where.on, where.available, current.declinedLocation, current.messages)) {
      showAsk(true);
      return;
    }
    showAsk(false);
    showBar('');
    const msg = { role: 'user', content: text };
    if (tray.length) {
      msg.photos = tray.length;
      photoData.set(msg, tray);
      tray = [];
      renderTray();
    }
    current.messages.push(msg);
    ui.input.value = '';
    draft = '';
    autosize();
    ask(answered === 'yes');
  }

  function retry() {
    if (busy || Date.now() < cooldownUntil) return;
    const msgs = current.messages;
    if (msgs.length && msgs[msgs.length - 1].role === 'assistant') {
      const dropped = msgs.pop();
      if (dropped.job) forgetJob(dropped.job);   // the answer is thrown away, and so is the picture it was making
    }
    if (msgs.length && msgs[msgs.length - 1].role === 'user') {
      refusedPhotos.delete(msgs[msgs.length - 1]);   // Try again on a refused turn is the reader's own choice to send its photos again
      ask();
    } else renderAll();
  }

  function retryAfter(resp, detail) {
    let seconds = parseInt(resp.headers.get('Retry-After') || '', 10);
    if (isNaN(seconds)) seconds = 5;
    const said = /(\d+)\s*seconds?/i.exec(detail || '');   // the detail may name a longer wait
    if (said) seconds = Math.max(seconds, parseInt(said[1], 10));
    return Math.min(Math.max(seconds, 1), 600);
  }

  // What a refused request says: the server's own words and, for a refusal about photos, its ``code``.
  async function problemOf(resp) {
    try {
      const data = await resp.json();
      return {
        detail: data && typeof data.detail === 'string' ? data.detail : '',
        code: data && typeof data.code === 'string' ? data.code : '',
      };
    } catch (e) { return { detail: '', code: '' }; }
  }
  async function detailOf(resp) { return (await problemOf(resp)).detail; }

  // Resolves when ``signal`` is aborted, so a long wait can be raced against the Stop button.
  const whenAborted = (signal) => new Promise((resolve) => {
    if (signal.aborted) resolve(); else signal.addEventListener('abort', () => resolve(), { once: true });
  });

  // ``prompted``: this send just turned sharing on, so the browser's own question may still be on screen.
  async function ask(prompted) {
    const conv = current;
    if (!conv.chatId) conv.chatId = newChatId();
    // ``features``: this page shows the picture events (image_job, image_refused), so the server may offer the model its
    // picture tool. A page that does not say so gets none.
    const payload = { chat_id: conv.chatId, features: ['image_jobs'], messages: context(conv.messages) };
    // The turn whose photos this request carries as pictures (the newest that has any): a refusal about photos is about it.
    const photoTurn = payload.messages.some((m) => Array.isArray(m.content))
      ? conv.messages.slice().reverse().find((m) => photoCount(m) > 0) : null;
    const reply = { role: 'assistant', content: '' };
    conv.messages.push(reply);
    conv.updated = Date.now();
    live = { msg: reply, view: null };
    setBusy(true);
    renderAll();
    save();
    const ctrl = new AbortController();
    controller = ctrl;
    let throttled = false;
    try {
      if (where.on && asksLocal(conv.messages)) {
        // Only a local question wakes the GPS or carries a place, and only a fresh fix goes along (a stale one is
        // renewed first, for up to three seconds, or for a minute while the reader answers the browser's prompt, and
        // Stop ends the wait). Without one the message goes out anyway and the answer says so, instead of the page
        // claiming a place it doesn't have.
        const here = await Promise.race([where.forSend(prompted ? PROMPT_WAIT : undefined), whenAborted(ctrl.signal)]);
        if (ctrl.signal.aborted) { reply.status = 'stopped'; return; }
        if (here) payload.location = here; else reply.locationNote = NO_LOCATION_NOTE;
      }
      const resp = await fetch(API, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
        body: JSON.stringify(payload),
        signal: ctrl.signal,
        cache: 'no-store',
      });
      if (resp.ok) {
        await readStream(resp, reply);
      } else {
        const problem = await problemOf(resp);
        if (resp.status === 429 && problem.code !== 'image_quota') {
          throttled = true;
          throttle(conv, reply, problem.detail || 'Too many messages right now.', retryAfter(resp, problem.detail));
        } else if (photoTurn && resp.status >= 400 && resp.status < 500) {
          // The server will not take these photos (this network's hour of photos is used up, or one cannot be read). The turn
          // stays, with the server's words under it; its photos are marked refused, so no later message sends them again (they
          // go as "[photo]"), and nothing is paused: messages without photos still go through.
          refusedPhotos.add(photoTurn);
          reply.status = 'error';
          reply.note = problem.detail || ('DLNE could not take the photos (HTTP ' + resp.status + ').');
        } else {
          reply.status = 'error';
          reply.note = problem.detail || ('DLNE could not answer (HTTP ' + resp.status + '). Please try again.');
        }
      }
    } catch (err) {
      if (err && err.name === 'AbortError') {
        reply.status = 'stopped';
      } else {
        reply.status = 'error';
        reply.note = navigator.onLine === false
          ? "You're offline. Reconnect, then try again."
          : "Couldn't reach DLNE. Check your connection and try again.";
      }
    } finally {
      if (frame) { cancelAnimationFrame(frame); frame = 0; }
      const shown = live && live.msg === reply ? live.view : null;
      live = null;
      delete reply.searching;
      if (controller === ctrl) controller = null;
      setBusy(false);
      if (!throttled) {
        conv.updated = Date.now();
        save();
        if (shown && conv === current) { paint(shown, false); scrollToEnd(false); }
        refreshStatus();
      }
    }
  }

  async function readStream(resp, reply) {
    let finished = false;
    const parser = streamReader((data) => {
      const result = applyData(reply, data);
      if (result === 'done') { finished = true; return false; }
      if (result === 'changed') {
        // The picture the model asked for: its job is polled from the moment the event arrives, whatever the text does.
        if (reply.job && imageActive(reply.job) && !jobTimers.has(reply.job)) watch(reply.job, false);
        schedule();
      }
      return true;
    });
    const reader = resp.body.getReader();
    for (;;) {
      const { value, done } = await reader.read();
      if (done) { parser.end(); break; }
      if (!parser.push(value)) {
        reader.cancel().catch(() => {});
        break;
      }
    }
    if (!finished && reply.status !== 'error') {
      reply.status = 'error';
      reply.note = reply.content ? 'The connection dropped before the answer finished.' : 'No answer came back. Please try again.';
    }
  }

  // The server's rate limit (too many messages): the turn goes back (no answer, nothing sent) and its words and photos
  // return to the box, which stays shut for ``seconds``. (The hour's limit of photos is another matter: see ask().)
  function throttle(conv, reply, detail, seconds) {
    const msgs = conv.messages;
    if (msgs[msgs.length - 1] === reply) msgs.pop();
    const asked = msgs[msgs.length - 1];
    if (asked && asked.role === 'user') {
      msgs.pop();   // not sent: give the words (and photos) back to the input
      if (!ui.input.value.trim()) { ui.input.value = asked.content; draft = asked.content; }
      const photos = photoData.get(asked);
      if (photos && !tray.length && !preparing) { tray = photos.slice(); renderTray(); }
    }
    save();
    if (conv === current) renderAll();
    cooldownUntil = Date.now() + seconds * 1000;
    clearTimeout(cooldownTimer);
    const tick = () => {
      const left = Math.ceil((cooldownUntil - Date.now()) / 1000);
      if (left <= 0) {
        cooldownUntil = 0;
        showBar('');
        updateComposer();
        return;
      }
      showBar(detail + ' You can send again in ' + left + ' s.');
      cooldownTimer = setTimeout(tick, 500);
    };
    tick();
    updateComposer();
    autosize();
  }

  function showBar(text) {
    ui.bar.textContent = text;
    ui.bar.hidden = !text;
  }

  function setBusy(value) {
    const stopHadFocus = document.activeElement === ui.stop;
    busy = value;
    ui.log.setAttribute('aria-busy', value ? 'true' : 'false');
    updateComposer();
    if (stopHadFocus && !value && !coarse) ui.input.focus();
  }

  function updateComposer() {
    const cooling = Date.now() < cooldownUntil;
    ui.input.disabled = cooling;
    ui.send.hidden = busy;
    ui.stop.hidden = !busy;
    // Photos alone are a message too; one still being made ready is not in the message yet, so it holds the send back.
    ui.send.disabled = busy || cooling || (!ui.input.value.trim() && !tray.length) || preparing > 0 || navigator.onLine === false;
  }

  function autosize() {
    ui.input.style.height = 'auto';
    ui.input.style.height = ui.input.scrollHeight + 'px';
  }

  // ---------------------------------------------------------------- chats: new, saved, deleted
  function park() {
    if (controller) controller.abort();
    if (current.messages.some((m) => m.role === 'user' || m.kind === 'image')) {
      saved = [current].concat(saved.filter((c) => c.id !== current.id)).slice(0, MAX_SAVED);
    }
  }

  function newChat() {
    park();
    current = fresh();
    showAsk(false);   // a question waiting on the location card stays in the box, but the card belongs to the chat it was for
    save();
    renderAll();
    if (!coarse && !ui.input.disabled) ui.input.focus();
  }

  function openSaved(id) {
    const found = saved.find((c) => c.id === id);
    if (!found) return;
    park();
    saved = saved.filter((c) => c.id !== id);
    current = found;
    showAsk(false);
    save();
    renderAll();
  }

  function titleOf(c) {
    const first = c.messages.find((m) => m.role === 'user');
    const pictured = first ? null : c.messages.find((m) => m.kind === 'image');
    // A first turn of photos alone has no words to be named by.
    return first ? (first.content.replace(/\s+/g, ' ').slice(0, 80) || (photoCount(first) ? 'Photo' : ''))
      : pictured ? pictured.content.slice(0, 80) : 'Empty chat';
  }

  function renderSaved() {
    clear(ui.recent);
    if (!saved.length) ui.recent.appendChild(el('p', 'muted small', 'Earlier chats appear here when you start a new one. They stay on this device.'));
    saved.forEach((c) => {
      const b = button('recent-item', undefined, () => { closeDialog(ui.menu); openSaved(c.id); });
      b.appendChild(el('span', 'recent-title', titleOf(c)));
      let when = '';
      try { when = new Date(c.updated).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }); } catch (e) { when = ''; }
      b.appendChild(el('span', 'recent-when', when));
      ui.recent.appendChild(b);
    });
    ui.clearAll.hidden = !saved.length && !current.messages.length;
  }

  // ---------------------------------------------------------------- dialogs
  function openDialog(d) {
    if (typeof d.showModal === 'function') { if (!d.open) d.showModal(); }
    else d.setAttribute('open', '');
  }
  function closeDialog(d) {
    if (typeof d.close === 'function') d.close();
    else d.removeAttribute('open');
  }

  // ---------------------------------------------------------------- reporting an answer
  let reportFor = null;     // { msg, button }
  let reportDone = false;
  let reportBusy = false;
  let reportCooldown = 0;
  let reportTimer = 0;

  function chosenReason() {
    const picked = ui.reportForm.querySelector('input[name="reason"]:checked');
    return picked ? picked.value : '';
  }

  function updateReportSend() {
    if (reportDone) return;
    ui.reportSend.disabled = !chosenReason() || reportBusy || Date.now() < reportCooldown;
  }

  // An answer can be reported from its own button or from the picture it made: after either, both say "Reported".
  function refreshReported(msg) {
    const v = viewOf.get(msg);
    if (v && v.row.querySelector('.after')) footer(v);
    const card = msg.job ? cardViews.get(msg.job) : null;
    if (card) card.update();
  }

  function showReportStatus(text, kind) {
    ui.reportStatus.textContent = text;
    ui.reportStatus.className = 'report-status' + (kind ? ' ' + kind : '');
    ui.reportStatus.hidden = !text;
  }

  function openReport(msg, btn) {
    reportFor = { msg, button: btn };
    reportDone = false;
    ui.reportForm.reset();
    ui.reportReasons.disabled = false;
    ui.reportNote.disabled = false;
    ui.reportSend.textContent = 'Send report';
    showReportStatus('', '');
    updateReportSend();
    openDialog(ui.report);
  }

  async function sendReport() {
    const target = reportFor;
    const payload = target && reportPayload(current.messages, target.msg, chosenReason(), ui.reportNote.value);
    if (!payload || reportBusy) return;
    reportBusy = true;
    updateReportSend();
    showReportStatus('Sending…', '');
    try {
      const resp = await fetch(REPORT, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
        cache: 'no-store',
      });
      const result = reportResult(resp.status);
      if (result.ok) {
        reportDone = true;
        target.msg.reported = true;
        save();
        if (target.button) { target.button.textContent = 'Reported'; target.button.disabled = true; }
        refreshReported(target.msg);
        ui.reportReasons.disabled = true;
        ui.reportNote.disabled = true;
        ui.reportSend.textContent = 'Close';
        ui.reportSend.disabled = false;
        showReportStatus(result.text, 'ok');
      } else if (resp.status === 429) {
        const detail = await detailOf(resp);
        const wait = retryAfter(resp, detail);
        reportCooldown = Date.now() + wait * 1000;
        clearTimeout(reportTimer);
        reportTimer = setTimeout(updateReportSend, wait * 1000 + 50);
        showReportStatus((detail || result.text) + ' You can send again in ' + wait + ' s.', 'error');
      } else {
        showReportStatus(result.text, 'error');
      }
    } catch (e) {
      showReportStatus(navigator.onLine === false
        ? "You're offline. Reconnect, then try again."
        : "Couldn't reach DLNE. Check your connection and try again.", 'error');
    } finally {
      reportBusy = false;
      updateReportSend();
    }
  }

  // ---------------------------------------------------------------- images (only while the server offers them)
  // A picture job is followed whether it is a card of its own (made from the sheet) or the ``job`` of a reply (the model
  // asked for it): each is its own object, and every table below is keyed by that object, so any number can run at once.
  const cardViews = new WeakMap();   // job -> its card's view
  const pictures = new WeakMap();    // job -> the finished picture: memory only, never saved
  const jobTimers = new Map();       // running job -> its poll timer
  const polling = new Set();         // jobs with a poll in flight
  const firstMiss = new Map();       // job -> when its polls began failing
  const LOST_AFTER = 2 * 60 * 1000;  // give up following a job after this long without an answer
  let imageSupport = null;           // { etas } while capabilities.image_generation is true
  let imageBusy = false;
  const sizeLabels = {};
  Array.from(ui.imageSize.options).forEach((o) => { sizeLabels[o.value] = o.textContent; });
  const jobOf = (m) => (m.kind === 'image' ? m : m.job || null);   // the job a message is or carries

  // The status says what the server can do; the page offers it only while it can: photos while ``app_vision`` is true
  // (a missing key is false), pictures while ``image_generation`` is.
  function readCapabilities(data) {
    visionSupport = !!(data && data.capabilities && data.capabilities.app_vision === true);
    readImageSupport(data);
    updatePlus();
  }

  function readImageSupport(data) {
    const capable = !!(data && data.capabilities && data.capabilities.image_generation === true);
    const info = capable && data.image_generation && typeof data.image_generation === 'object' ? data.image_generation : {};
    const first = capable && !imageSupport;
    imageSupport = capable ? { etas: info.eta_seconds } : null;
    if (!capable) return;
    const offered = Array.isArray(info.sizes) ? IMAGE_SIZES.filter((s) => info.sizes.indexOf(s) !== -1) : IMAGE_SIZES;
    Array.from(ui.imageSize.options).forEach((o) => {
      o.hidden = o.disabled = offered.length > 0 && offered.indexOf(o.value) === -1;
      o.textContent = sizeLabels[o.value] + ', about ' + aboutSeconds(imageEta(info.eta_seconds, o.value));
    });
    if (first && offered.indexOf(info.default_size) !== -1) ui.imageSize.value = info.default_size;
  }

  function showImageStatus(text, kind) {
    ui.imageStatus.textContent = text;
    ui.imageStatus.className = 'report-status' + (kind ? ' ' + kind : '');
    ui.imageStatus.hidden = !text;
  }

  function openImage(again) {
    if (!imageSupport) return;
    if (again) { ui.imagePrompt.value = again.prompt; if (IMAGE_SIZES.indexOf(again.size) !== -1) ui.imageSize.value = again.size; }
    showImageStatus('', '');
    openDialog(ui.image);
    ui.imagePrompt.focus();
  }

  function imageFailure(resp, detail) {
    if (detail) return detail;
    if (resp.status === 429) return 'Too many pictures right now. Please try again later.';
    if (resp.status === 503) return "Picture making isn't available right now.";
    return "DLNE couldn't start the picture (HTTP " + resp.status + '). Please try again.';
  }

  async function submitImage() {
    if (imageBusy) return;
    const prompt = str(ui.imagePrompt.value, 1000);
    if (!prompt) { showImageStatus('Describe the picture first.', 'error'); ui.imagePrompt.focus(); return; }
    const size = IMAGE_SIZES.indexOf(ui.imageSize.value) !== -1 ? ui.imageSize.value : IMAGE_SIZES[0];
    const conv = current;
    if (!conv.chatId) conv.chatId = newChatId();
    imageBusy = true;
    ui.imageSend.disabled = true;
    showImageStatus('Sending…', '');
    try {
      const resp = await fetch(IMAGES, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt, size, chat_id: conv.chatId }),
        cache: 'no-store',
      });
      let job = null;
      let detail = '';
      if (resp.ok) { try { job = await resp.json(); } catch (e) { job = null; } } else detail = await detailOf(resp);
      if (resp.status === 202 && job && isImageId(job.id)) {
        const card = {
          role: 'assistant', kind: 'image', content: imagePlaceholder(prompt), prompt, size, state: 'queued',
          imageId: job.id, startedAt: Date.now(),
        };
        imageApply(card, job, Date.now());
        conv.messages.push(card);
        conv.updated = Date.now();
        ui.imagePrompt.value = '';
        closeDialog(ui.image);
        if (conv === current) renderAll();
        save();
        watch(card, false);
      } else {
        showImageStatus(imageFailure(resp, detail), 'error');
      }
    } catch (e) {
      showImageStatus(navigator.onLine === false
        ? "You're offline. Reconnect, then try again."
        : "Couldn't reach DLNE. Check your connection and try again.", 'error');
    } finally {
      imageBusy = false;
      ui.imageSend.disabled = false;
    }
  }

  function pictureView(msg) {
    const v = imageCard(msg, {
      picture: () => pictures.get(msg) || null, now: Date.now, cancel: () => cancelImage(msg), retry: () => openImage(msg),
      share: canShare ? () => sharePicture(msg) : undefined,
    });
    cardViews.set(msg, v);
    return { row: v.row, msg };
  }

  // Hands the finished picture to the browser's share sheet as a PNG file (only offered where it can take a file).
  async function sharePicture(job) {
    const picture = pictures.get(job);
    if (!picture || !canShare) return;
    try {
      const name = 'dlne-image-' + (picture.seed !== null ? picture.seed : Date.now()) + '.png';
      const file = new File([pictureBytes(picture)], name, { type: 'image/png' });
      if (!navigator.canShare({ files: [file] })) return;
      await navigator.share({ files: [file], title: 'A picture from DLNE Chat' });
    } catch (e) { /* the sheet was dismissed, or this browser would not take the file */ }
  }

  // Polls every 2 s while the page is shown and every 5 s behind it.
  function watch(msg, soon) {
    clearTimeout(jobTimers.get(msg));
    if (!imageActive(msg) || !msg.imageId) { jobTimers.delete(msg); firstMiss.delete(msg); return; }
    const wait = soon ? 0 : (document.visibilityState === 'visible' ? 2000 : 5000);
    jobTimers.set(msg, setTimeout(() => poll(msg), wait));
  }

  function pollAll() { Array.from(jobTimers.keys()).forEach((msg) => watch(msg, true)); }

  function lose(msg) {
    const since = firstMiss.get(msg) || Date.now();
    firstMiss.set(msg, since);
    if (Date.now() - since < LOST_AFTER) return;
    msg.state = 'failed';
    msg.note = "Lost touch with DLNE, so this picture can't be followed. Please try again.";
    delete msg.imageId;
  }

  async function poll(msg) {
    if (polling.has(msg) || !imageActive(msg) || !msg.imageId) return;
    polling.add(msg);
    const before = msg.state;
    try {
      const resp = await fetch(IMAGES + '/' + encodeURIComponent(msg.imageId), { cache: 'no-store' });
      if (resp.status === 404) {
        imageApply(msg, { status: 'expired' }, Date.now());   // not found: finished and gone, or another network's
      } else if (!resp.ok) {
        lose(msg);
      } else {
        let job = await resp.json();
        firstMiss.delete(msg);
        if (job && job.status === 'completed') {
          const picture = imagePicture(job.image);
          if (picture) pictures.set(msg, picture);
          else job = { status: 'failed', error: { detail: "The picture couldn't be shown. Please try again." } };
        }
        imageApply(msg, job, Date.now());
      }
    } catch (e) {
      lose(msg);
    } finally {
      polling.delete(msg);
    }
    jobMoved(msg, before);
  }

  function jobMoved(msg, before) {
    const v = cardViews.get(msg);
    if (v) v.update();
    if (msg.state !== before) save();
    watch(msg, false);
    if (msg.state !== before) scrollToEnd(false);   // a finished picture is tall: a reader at the bottom stays there
  }

  async function cancelImage(msg) {
    if (!msg.imageId || msg.cancelling) return;
    const before = msg.state;
    msg.cancelling = true;
    const v = cardViews.get(msg);
    if (v) v.update();
    try {
      const resp = await fetch(IMAGES + '/' + encodeURIComponent(msg.imageId) + '/cancel', { method: 'POST', cache: 'no-store' });
      const job = resp.ok ? await resp.json() : null;
      // A job that finished first still has a picture to fetch: the next poll takes it.
      if (job && job.status !== 'completed') imageApply(msg, job, Date.now());
    } catch (e) { /* the next poll shows where the job is */ }
    delete msg.cancelling;
    jobMoved(msg, before);
    if (imageActive(msg)) watch(msg, true);
  }

  // Deleting every chat also stops the pictures being made for them.
  function dropJobs() {
    jobTimers.forEach((timer, msg) => {
      clearTimeout(timer);
      if (msg.imageId) fetch(IMAGES + '/' + encodeURIComponent(msg.imageId) + '/cancel', { method: 'POST', cache: 'no-store' }).catch(() => {});
    });
    jobTimers.clear();
    firstMiss.clear();
  }

  // One job is given up on (its answer was thrown away to be written again): stop following it and stop it being made.
  function forgetJob(job) {
    clearTimeout(jobTimers.get(job));
    jobTimers.delete(job);
    firstMiss.delete(job);
    if (job.imageId && imageActive(job)) {
      fetch(IMAGES + '/' + encodeURIComponent(job.imageId) + '/cancel', { method: 'POST', cache: 'no-store' }).catch(() => {});
    }
  }

  // ---------------------------------------------------------------- the "+" menu and photos (only while the server reads them)
  const photoData = new WeakMap();   // user message -> its photos ({ src, thumb, width, height }): memory only, never saved
  const refusedPhotos = new WeakSet();   // user messages whose photos the server refused: later requests send "[photo]" for them
  let tray = [];                     // the photos chosen for the next message
  let preparing = 0;                 // photos still being made ready
  let trayNote = '';                 // what last went wrong with a photo, in words
  let visionSupport = false;         // capabilities.app_vision
  let plusOpen = false;

  // The "+" shows only what the server can do now: Photos and Camera while it reads photos (Camera on touch screens, where
  // ``capture`` opens the camera app; on a computer it would open the same file picker twice), Create image while it makes
  // pictures. With neither there is no "+".
  function updatePlus() {
    const canPhotos = visionSupport;
    const canPictures = !!imageSupport;
    ui.plus.hidden = !canPhotos && !canPictures;
    ui.field.classList.toggle('has-plus', !ui.plus.hidden);
    ui.addPhotos.hidden = !canPhotos;
    ui.addCamera.hidden = !(canPhotos && coarse);
    ui.makeImage.hidden = !canPictures;
    ui.plus.setAttribute('aria-label',
      canPhotos && canPictures ? 'Add a photo or create an image' : canPhotos ? 'Add a photo' : 'Create an image');
    if (ui.plus.hidden) closePlus();
  }

  function closePlus() {
    plusOpen = false;
    ui.plusMenu.hidden = true;
    ui.plus.setAttribute('aria-expanded', 'false');
  }

  function openPlus(byKeyboard) {
    plusOpen = true;
    ui.plusMenu.hidden = false;
    ui.plus.setAttribute('aria-expanded', 'true');
    if (byKeyboard) {   // a keyboard reader lands on the first item; a touch reader is left where they are
      const first = Array.from(ui.plusMenu.querySelectorAll('button')).find((b) => !b.hidden);
      if (first) first.focus();
    }
  }

  function renderTray() {
    clear(ui.trayThumbs);
    tray.forEach((photo, i) => ui.trayThumbs.appendChild(trayItem(photo, i, () => removePhoto(i))));
    for (let i = 0; i < preparing; i++) ui.trayThumbs.appendChild(el('li', 'tray-wait'));
    ui.trayNote.textContent = preparing ? 'Preparing your photo…' : trayNote;
    ui.tray.hidden = !tray.length && !preparing && !trayNote;
    ui.trayHint.hidden = !tray.length && !preparing;   // the line about where photos go shows while there are photos
    updateComposer();
  }

  function removePhoto(index) {
    tray.splice(index, 1);
    trayNote = '';
    showBar('');
    renderTray();
  }

  function readAsDataUrl(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result));
      reader.onerror = () => reject(reader.error || new Error('The file could not be read.'));
      reader.readAsDataURL(file);
    });
  }

  function loadImage(url) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = () => reject(new Error('The file is not a picture this browser can open.'));
      img.src = url;
    });
  }

  // The file as a picture with its EXIF orientation applied, so a phone photo taken sideways stands up. createImageBitmap
  // does it when told ``from-image``; a browser that does not know the option throws, and an <img> element (which every
  // current browser turns upright by itself) is the way round. It is loaded from a data: URL: the policy has no blob: source.
  async function decodePhoto(file) {
    if (typeof createImageBitmap === 'function') {
      try {
        const bitmap = await createImageBitmap(file, { imageOrientation: 'from-image' });
        return { source: bitmap, width: bitmap.width, height: bitmap.height, close() { if (bitmap.close) bitmap.close(); } };
      } catch (e) { /* no such option here, or the file did not decode: the element below tries too */ }
    }
    const img = await loadImage(await readAsDataUrl(file));
    return { source: img, width: img.naturalWidth, height: img.naturalHeight, close() {} };
  }

  // One chosen file made ready: scaled and written again as a JPEG by DLNEChat.fitPhoto, which asks this canvas to draw it.
  async function preparePhoto(file) {
    const decoded = await decodePhoto(file);
    const canvas = document.createElement('canvas');
    try {
      const draw = (width, height, quality) => {
        canvas.width = width;
        canvas.height = height;
        const ctx = canvas.getContext('2d');
        ctx.fillStyle = '#fff';   // a PNG's see-through pixels would come out black in a JPEG
        ctx.fillRect(0, 0, width, height);
        ctx.imageSmoothingQuality = 'high';
        ctx.drawImage(decoded.source, 0, 0, width, height);
        const url = canvas.toDataURL('image/jpeg', quality);
        return { url, bytes: dataUrlBytes(url) };
      };
      return await fitPhoto(decoded.width, decoded.height, draw);
    } finally {
      canvas.width = canvas.height = 0;   // a canvas keeps its memory until told otherwise, and a phone counts every one
      decoded.close();
    }
  }

  const trayFull = () => tray.length + preparing >= MAX_PHOTOS;
  const FULL_NOTE = 'Only ' + MAX_PHOTOS + ' photos fit in one message.';

  async function addPhotos(list) {
    const files = Array.from(list || []);   // copied first: clearing the input below empties the list it hands out
    ui.pickPhotos.value = '';               // so that choosing the same file again fires a change again
    ui.pickCamera.value = '';
    trayNote = '';
    for (const file of files) {
      if (trayFull()) { trayNote = FULL_NOTE; break; }
      preparing++;   // counted before the first await, so two choices made close together cannot together pass four
      renderTray();
      try {
        // Nothing is decoded until the file's own header says it is a photo of a size this page can take (a PNG or JPEG of
        // at most 100 megapixels, in at most 64 MiB): decoding a huge one could take a phone's tab down.
        const refusal = await photoRefusal(file);
        if (refusal) trayNote = refusal; else tray.push(await preparePhoto(file));
      } catch (e) {
        trayNote = "That file couldn't be opened as a photo.";
      } finally {
        preparing--;
        renderTray();
      }
    }
    renderTray();
  }

  // A photo of the reader's, full screen (a tap on it, or on Close, puts it away).
  function showPhoto(photo) {
    clear(ui.photoBox);
    const img = el('img');
    img.src = photo.src;
    img.alt = 'Your photo';
    ui.photoBox.appendChild(img);
    openDialog(ui.photo);
  }

  // ---------------------------------------------------------------- location (off unless chosen)
  const LOCATION = 'dlne.chat.location';

  function locationProblem(text) {
    ui.locationStatus.textContent = text;
    ui.locationStatus.hidden = !text;
  }

  // The fix itself (its age, its refresh, what a failure does) is DLNEChat.locator; this only shows what it reports.
  const where = locator(navigator.geolocation, Date.now, (kind) => {
    if (kind === 'denied' || kind === 'unsupported') setSharing(false);
    if (kind === 'denied' && !current.declinedLocation) {
      current.declinedLocation = true;   // the browser said no: the card has nothing more to offer in this chat
      save();
    }
    locationProblem(LOCATION_TEXT[kind] || '');
  });

  function setSharing(on) {
    where.enable(on);
    store.set(LOCATION, on ? '1' : '0');
    ui.shareLocation.checked = on;
    locationProblem('');
  }

  // The card above the box that holds a local question's send until the reader answers (see submit).
  function showAsk(visible) {
    if (ui.askLocation.hidden === !visible) return;
    ui.askLocation.hidden = !visible;
    if (visible) ui.askYes.focus();
  }

  // The card belongs to the words held in the box: when they are deleted, or edited into something that is not a local
  // question, there is nothing left to ask about, and "Use my location" would turn sharing on with nothing to send.
  function refreshAsk() {
    if (!ui.askLocation.hidden && !locationCardWanted(ui.input.value, where.on, where.available, current.declinedLocation, current.messages)) {
      showAsk(false);
    }
  }

  // ---------------------------------------------------------------- the pool's status
  let lastModel = '';
  async function refreshStatus() {
    if (navigator.onLine === false) { setStatus('off', "You're offline", lastModel); return; }
    try {
      const resp = await fetch(STATUS, { cache: 'no-store' });
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      const data = await resp.json();
      readCapabilities(data);
      const pool = data && data.app_pool;
      if (!pool || typeof pool !== 'object') { setStatus('down', 'Not available here yet', ''); return; }
      const ready = Math.max(0, Math.floor(Number(pool.members_ready) || 0));
      lastModel = typeof pool.active_model === 'string' ? pool.active_model : '';
      setStatus(ready ? 'up' : 'down',
        ready ? ready + (ready === 1 ? ' machine serving' : ' machines serving') : 'No machines online', lastModel);
    } catch (e) {
      setStatus('off', navigator.onLine === false ? "You're offline" : "Can't reach DLNE", lastModel);
    }
  }

  function setStatus(kind, serving, model) {
    ui.dot.className = 'dot ' + kind;
    ui.serving.textContent = serving;
    ui.model.textContent = model;
    ui.sep.hidden = !model;
    ui.about.textContent = (model ? 'Answers are written by ' + model + ' on DLNE\'s own server.' : 'Answers come from DLNE\'s own server.')
      + ' A volunteer\'s computer answers only if it is restarting or down. They can be slow, and they can be wrong.';
  }

  function connectivity() {
    ui.offline.hidden = navigator.onLine !== false;
    updateComposer();
    refreshStatus();
  }

  // ---------------------------------------------------------------- keep the input above the keyboard
  const vv = window.visualViewport;
  function fitViewport() {
    if (!vv) return;
    if (Math.abs(vv.scale - 1) > 0.01) { ui.app.classList.remove('vv'); return; }   // pinch-zoomed: leave the layout alone
    const root = document.documentElement.style;
    root.setProperty('--app-h', vv.height + 'px');
    root.setProperty('--app-top', vv.offsetTop + 'px');
    ui.app.classList.add('vv');
    document.body.classList.toggle('kb', window.innerHeight - vv.height > 120);
    scrollToEnd(false);
  }

  // ---------------------------------------------------------------- wiring
  ui.form.addEventListener('submit', (e) => { e.preventDefault(); submit(); });
  ui.input.addEventListener('keydown', (e) => {
    // A keyboard sends with Enter (Shift+Enter for a new line); on a phone, Enter is a new line.
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229 && !coarse) { e.preventDefault(); submit(); }
  });
  let draftTimer = 0;
  ui.input.addEventListener('input', () => {
    autosize();
    updateComposer();
    refreshAsk();
    clearTimeout(draftTimer);
    draftTimer = setTimeout(() => { draft = ui.input.value; save(); }, 400);
  });
  ui.shareLocation.addEventListener('change', () => { setSharing(ui.shareLocation.checked); where.touch(); });
  ui.stop.addEventListener('click', () => { if (controller) controller.abort(); });
  $('new-chat').addEventListener('click', newChat);
  $('open-menu').addEventListener('click', () => { renderSaved(); openDialog(ui.menu); });
  $('close-menu').addEventListener('click', () => closeDialog(ui.menu));
  ui.menu.addEventListener('click', (e) => { if (e.target === ui.menu) closeDialog(ui.menu); });
  ui.clearAll.addEventListener('click', () => {
    if (!window.confirm('Delete all your chats on this device?')) return;
    if (controller) controller.abort();
    dropJobs();
    saved = [];
    current = fresh();
    save();
    renderAll();
    renderSaved();
  });
  document.querySelectorAll('[data-prompt]').forEach((chip) => chip.addEventListener('click', () => {
    if (ui.input.disabled) return;
    ui.input.value = chip.getAttribute('data-prompt');
    autosize();
    updateComposer();
    refreshAsk();
    ui.input.focus();
  }));
  // The "+" menu: a popover that closes on a choice, on Escape, and on a tap anywhere else.
  ui.plus.addEventListener('click', (e) => { if (plusOpen) closePlus(); else openPlus(e.detail === 0); });
  ui.addPhotos.addEventListener('click', () => {
    closePlus();
    if (trayFull()) { trayNote = FULL_NOTE; renderTray(); } else ui.pickPhotos.click();
  });
  ui.addCamera.addEventListener('click', () => {
    closePlus();
    if (trayFull()) { trayNote = FULL_NOTE; renderTray(); } else ui.pickCamera.click();
  });
  ui.makeImage.addEventListener('click', () => { closePlus(); openImage(null); });
  document.addEventListener('click', (e) => {
    if (plusOpen && !ui.plus.contains(e.target) && !ui.plusMenu.contains(e.target)) closePlus();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && plusOpen) { closePlus(); ui.plus.focus(); }
  });
  ui.field.addEventListener('focusout', (e) => {
    if (plusOpen && e.relatedTarget && !ui.field.contains(e.relatedTarget)) closePlus();   // tabbed away
  });
  ui.pickPhotos.addEventListener('change', () => addPhotos(ui.pickPhotos.files));
  ui.pickCamera.addEventListener('change', () => addPhotos(ui.pickCamera.files));
  ui.photo.addEventListener('click', () => closeDialog(ui.photo));
  // The location card: "Use my location" turns sharing on (which is when the browser asks; it stays on until the reader turns
  // it off in the menu, which the card says) and sends the held message, which waits for the answer; "Not now" sends it
  // without a place and is remembered for this chat. With no words left in the box (the card is normally gone by then, see
  // refreshAsk) there is nothing to send, so neither answer changes anything.
  ui.askYes.addEventListener('click', () => {
    showAsk(false);
    if (!ui.input.value.trim()) return;
    setSharing(true);
    where.touch();
    submit('yes');
  });
  ui.askNo.addEventListener('click', () => {
    showAsk(false);
    if (!ui.input.value.trim()) return;
    current.declinedLocation = true;
    save();
    submit('no');
  });
  ui.imageForm.addEventListener('submit', (e) => { e.preventDefault(); submitImage(); });
  $('close-image').addEventListener('click', () => closeDialog(ui.image));
  ui.image.addEventListener('click', (e) => { if (e.target === ui.image) closeDialog(ui.image); });
  ui.reportForm.addEventListener('submit', (e) => {
    e.preventDefault();
    if (reportDone) closeDialog(ui.report);
    else sendReport();
  });
  ui.reportForm.addEventListener('change', updateReportSend);
  $('close-report').addEventListener('click', () => closeDialog(ui.report));
  ui.report.addEventListener('click', (e) => { if (e.target === ui.report) closeDialog(ui.report); });
  $('first-run-ok').addEventListener('click', () => { store.set(SEEN, '1'); closeDialog(ui.firstRun); });
  ui.firstRun.addEventListener('close', () => store.set(SEEN, '1'));

  let installPrompt = null;
  window.addEventListener('beforeinstallprompt', (e) => { e.preventDefault(); installPrompt = e; ui.install.hidden = false; });
  window.addEventListener('appinstalled', () => { installPrompt = null; ui.install.hidden = true; });
  ui.install.addEventListener('click', async () => {
    if (!installPrompt) return;
    installPrompt.prompt();
    try { await installPrompt.userChoice; } catch (e) { /* dismissed */ }
    installPrompt = null;
    ui.install.hidden = true;
  });

  window.addEventListener('online', connectivity);
  window.addEventListener('offline', connectivity);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') { refreshStatus(); pollAll(); }
    else save();
  });
  window.addEventListener('pagehide', save);
  setInterval(() => { if (document.visibilityState === 'visible') refreshStatus(); }, 30000);
  setInterval(() => jobTimers.forEach((_timer, msg) => { const v = cardViews.get(msg); if (v) v.tick(); }), 1000);
  if (vv) {
    vv.addEventListener('resize', fitViewport);
    vv.addEventListener('scroll', fitViewport);
    fitViewport();
  }

  load();
  where.enable(store.get(LOCATION) === '1');
  ui.shareLocation.checked = where.on;
  ui.input.value = draft;
  autosize();
  renderAll();
  // A picture still being made when the page was reloaded, in a card or in a reply: its saved job id picks the polling up again.
  saved.concat([current]).forEach((c) => c.messages.forEach((m) => { const job = jobOf(m); if (job && imageActive(job)) watch(job, true); }));
  connectivity();
  if (store.get(SEEN) !== '1') openDialog(ui.firstRun);
  if ('serviceWorker' in navigator && window.isSecureContext) {
    navigator.serviceWorker.register('/chat/sw.js', { scope: '/chat' }).catch(() => {});
  }
})();
</script>
</body>
</html>
"""

CHAT_HTML = (_HTML_TEMPLATE
             .replace("{{PUBLIC_URL}}", PUBLIC_URL)
             .replace("{{OG_IMAGE_URL}}", OG_IMAGE_URL)
             .replace("{{ICON_192}}", icon_url("icon-192.png"))
             .replace("{{ICON_FAVICON_32}}", icon_url("favicon-32.png"))
             .replace("{{ICON_FAVICON_48}}", icon_url("favicon-48.png"))
             .replace("{{ICON_APPLE_TOUCH}}", icon_url("apple-touch-icon.png"))
             .replace("{{PRIVACY_NOTICE}}", PRIVACY_NOTICE)
             .replace("{{LOOKUP_PRIVACY}}", LOOKUP_PRIVACY)
             .replace("{{LOCATION_PRIVACY}}", LOCATION_PRIVACY)
             .replace("{{PHOTO_PRIVACY}}", PHOTO_PRIVACY)
             .replace("{{PICTURE_PRIVACY}}", PICTURE_PRIVACY)
             .replace("{{PHOTO_HINT}}", PHOTO_HINT)
             .replace("{{REPORT_PRIVACY}}", REPORT_PRIVACY)
             .replace("{{COFFEE_URL}}", COFFEE_URL)
             .replace("{{EXTENSION_URL}}", EXTENSION_URL)
             .replace("{{WEBSITE_URL}}", WEBSITE_URL)
             .replace("{{DISCORD_INVITE_URL}}", DISCORD_INVITE_URL)
             .replace("{{THEME_DARK}}", THEME_DARK)
             .replace("{{THEME_LIGHT}}", THEME_LIGHT))


# ======================================================================================
# Install: the manifest and the service worker
# ======================================================================================

MANIFEST = {
    "id": "/chat",
    "name": "DLNE Chat",
    "short_name": "DLNE",
    "description": "Free AI chat, answered by DLNE's own server.",
    "lang": "en",
    "start_url": "/chat",
    "scope": "/chat",
    "display": "standalone",
    "background_color": THEME_DARK,
    "theme_color": THEME_DARK,
    "icons": [
        {"src": icon_url("icon-192.png"), "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": icon_url("icon-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {"src": icon_url("icon-maskable-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ],
}
MANIFEST_JSON = json.dumps(MANIFEST, indent=2)

# What the installed app needs to open with no network. Only these exact paths (an icon's with its
# ?v=) are ever cached. Just the one icon the page itself shows: the others are fetched once, by
# the browser or the phone, when the app is installed, and the 512 px pair would add ~600 KB.
SHELL_PATHS: List[str] = ["/chat", "/chat/manifest.webmanifest", icon_url("icon-192.png")]

_SW_TEMPLATE = r"""// DLNE Chat service worker: caches the app shell so the installed app opens offline.
// It answers only for the shell's own paths; every other request (the chat and status API
// included) goes straight to the network and is never stored.
const CACHE = 'dlne-chat-{{VERSION}}';
const SHELL = {{SHELL}};

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => cache.addAll(SHELL.map((path) => new Request(path, { cache: 'reload' }))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k.startsWith('dlne-chat-') && k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// The page itself: the network first (so an update arrives), the saved copy when offline or slow.
function page(request) {
  return new Promise((resolve) => {
    let settled = false;
    const settle = (response) => { if (!settled && response) { settled = true; resolve(response); } };
    const saved = () => caches.open(CACHE).then((cache) => cache.match('/chat'));
    const timer = setTimeout(() => saved().then(settle), 6000);
    fetch(request).then((response) => {
      clearTimeout(timer);
      if (response.ok && response.type === 'basic') {
        const copy = response.clone();
        caches.open(CACHE).then((cache) => cache.put('/chat', copy));
      }
      settle(response);
    }).catch(() => {
      clearTimeout(timer);
      saved().then((hit) => settle(hit || Response.error()));
    });
  });
}

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  // The page is matched by its path alone; the icon is matched with its ?v= (its version).
  const key = url.pathname === '/chat' ? '/chat' : url.pathname + url.search;
  if (url.origin !== self.location.origin || !SHELL.includes(key)) return;
  if (key === '/chat') {
    event.respondWith(page(request));
    return;
  }
  event.respondWith(
    caches.open(CACHE).then((cache) => cache.match(key)).then((hit) => hit || fetch(request))
  );
});
"""

SHELL_VERSION = hashlib.sha256((CHAT_HTML + MANIFEST_JSON).encode()).hexdigest()[:12]
SERVICE_WORKER_JS = (_SW_TEMPLATE.replace("{{VERSION}}", SHELL_VERSION)
                     .replace("{{SHELL}}", json.dumps(SHELL_PATHS)))


# ======================================================================================
# Routes
# ======================================================================================

chat_router = APIRouter()


@chat_router.api_route("/chat", methods=["GET", "HEAD"], include_in_schema=False)
async def chat_page() -> HTMLResponse:
    """The phone chat. Public, like the landing page: the endpoint it talks to is the throttle."""
    return HTMLResponse(CHAT_HTML, headers=_HTML_HEADERS)


@chat_router.get("/chat/manifest.webmanifest", include_in_schema=False)
async def chat_manifest() -> Response:
    return Response(MANIFEST_JSON, media_type="application/manifest+json",
                    headers={**_COMMON_HEADERS, "Cache-Control": "public, max-age=3600"})


@chat_router.get("/chat/sw.js", include_in_schema=False)
async def chat_service_worker() -> Response:
    # Service-Worker-Allowed lets the worker control /chat itself, not only paths under /chat/.
    return Response(SERVICE_WORKER_JS, media_type="text/javascript",
                    headers={**_COMMON_HEADERS, "Cache-Control": "no-cache", "Service-Worker-Allowed": "/chat"})


def _icon_endpoint(name: str):
    async def serve_icon(v: str = "") -> Response:
        # ?v= is only a cache key (see engine/icons.py): the bytes are the same whatever it says,
        # but only a URL with the current version may be cached for a year.
        headers = _ICON_HEADERS if v == ICON_VERSION else _ICON_UNVERSIONED_HEADERS
        return Response(icon_png(name), media_type="image/png", headers=headers)
    serve_icon.__name__ = "chat_icon_" + name.replace("-", "_").replace(".", "_")
    return serve_icon


for _name in ICONS:
    chat_router.add_api_route(f"/chat/{_name}", _icon_endpoint(_name), methods=["GET"], include_in_schema=False)
