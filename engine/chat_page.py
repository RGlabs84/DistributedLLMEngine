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

PRIVACY. A volunteer's machine sees the text of the requests it answers, so the page says so on
first run and, in fewer words, under the input; the menu adds that lookups are made by the
server, not the phone, and that reported answers are kept for review. Chat history lives in the browser's
localStorage on the phone, nowhere else.

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

PRIVACY_NOTICE = ("Answers are written by computers people lend to DLNE. "
                  "Don't send passwords, private details or anything secret.")
LOOKUP_PRIVACY = ("Lookups (weather, Wikipedia, news, web search) are made by the DLNE server, not your phone; "
                  "reported answers are kept on the server for review.")
REPORT_PRIVACY = "Reported answers are kept on the DLNE server for review."

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
<meta name="description" content="Free AI chat, answered by computers people lend to DLNE.">
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
.sources { margin-top: 14px; }
.sources-title { margin: 0 0 6px; font-size: .74rem; letter-spacing: .07em; text-transform: uppercase; color: var(--faint); }
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
  #menu, #report { width: 100%; margin: auto 0 0; max-height: 90%; border-width: 1px 0 0; border-radius: 20px 20px 0 0; }
}
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
      <p class="lead">Free, and answered by computers people lend to DLNE, so an answer can take a moment.</p>
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
    <div class="field">
      <textarea id="input" rows="1" maxlength="8000" placeholder="Message DLNE" aria-label="Message"></textarea>
      <button type="submit" class="round" id="send" aria-label="Send" disabled>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 19V5M5.5 11.5L12 5l6.5 6.5"/></svg>
      </button>
      <button type="button" class="round stop" id="stop" aria-label="Stop the answer" hidden>
        <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="7" y="7" width="10" height="10" rx="2" fill="currentColor"/></svg>
      </button>
    </div>
    <p class="hint">Volunteers' computers write the answers. Don't share secrets.</p>
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
    <p class="muted small" id="about">Answers come from computers people lend to DLNE. They can be slow, and they can be wrong.</p>
    <h3>Your chats</h3>
    <div class="recent" id="recent"></div>
    <button type="button" class="smallbtn" id="clear-all">Delete all chats</button>
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
    </div>
  </div>
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

  function sourceList(sources) {
    const box = el('div', 'sources');
    box.appendChild(el('p', 'sources-title', 'Sources'));
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
    return false;
  }

  // A saved message, cleaned on its way back from the phone's storage (null: drop it).
  function cleanMessage(m) {
    if (!m || (m.role !== 'user' && m.role !== 'assistant') || typeof m.content !== 'string') return null;
    const kept = { role: m.role, content: m.content };
    if (typeof m.status === 'string') kept.status = m.status;
    if (typeof m.note === 'string') kept.note = m.note;
    if (m.reported === true) kept.reported = true;
    if (kept.role === 'assistant') {
      const sources = cleanSources(m.sources);
      if (sources.length) kept.sources = sources;
      const notes = cleanNotes(m.lookupNotes);
      if (notes.length) kept.lookupNotes = notes;
      if (typeof m.searchNote === 'string' && m.searchNote) kept.searchNote = str(m.searchNote, 500);
      if (!kept.content && !kept.note) return null;   // an answer that never started
    }
    return kept;
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
  function reportPayload(messages, answer, reason, note) {
    if (REASONS.indexOf(reason) === -1 || !answer) return null;
    let question = '';
    for (let i = messages.indexOf(answer) - 1; i >= 0; i--) {
      if (messages[i].role === 'user') { question = messages[i].content; break; }
    }
    return {
      question: String(question).slice(0, 4000),
      answer: String(answer.content).slice(0, 8000),
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

  return {
    str, el, clear, button, safeUrl, link, markdown, copyText, cleanSources, sourceList, applyData, streamReader,
    lookupLine, cleanMessage, reportPayload, reportResult,
  };
})();
</script>
<script>
(() => {
  'use strict';
  const {
    el, clear, button, markdown, copyText, sourceList, applyData, streamReader, cleanMessage, reportPayload, reportResult,
  } = DLNEChat;
  const API = '/v1/app/chat/completions';
  const REPORT = '/v1/app/report';
  const STATUS = '/v1/cluster/status';
  const STORE = 'dlne.chat.v1';
  const SEEN = 'dlne.chat.noticeSeen';
  const SYSTEM = 'You are the assistant in DLNE Chat, a free phone app. Be helpful, clear and reasonably brief. ' +
    'Use Markdown lists, bold and code blocks only when they help.';
  const CONTEXT_CHARS = 24000;   // about 6000 tokens of history, inside one member slot's context
  const MAX_SAVED = 20;

  const $ = (id) => document.getElementById(id);
  const ui = {
    app: $('app'), log: $('log'), thread: $('thread'), welcome: $('welcome'), form: $('composer'), input: $('input'),
    send: $('send'), stop: $('stop'), bar: $('bar'), offline: $('offline'), dot: $('dot'), serving: $('serving'),
    sep: $('sep'), model: $('model'), about: $('about'), menu: $('menu'), recent: $('recent'), firstRun: $('first-run'),
    install: $('install'), clearAll: $('clear-all'), report: $('report'), reportForm: $('report-form'),
    reportReasons: $('report-reasons'), reportNote: $('report-note'), reportStatus: $('report-status'),
    reportSend: $('report-send'),
  };
  const coarse = !!(window.matchMedia && window.matchMedia('(pointer: coarse)').matches);

  // ---------------------------------------------------------------- storage (may be unavailable)
  const store = {
    get(key) { try { return window.localStorage.getItem(key); } catch (e) { return null; } },
    set(key, value) { try { window.localStorage.setItem(key, value); return null; } catch (e) { return e || new Error('storage'); } },
  };
  const fresh = () => ({ id: Date.now().toString(36) + Math.random().toString(36).slice(2, 8), updated: Date.now(), messages: [] });
  let current = fresh();
  let saved = [];
  let draft = '';

  function cleanConversation(c) {
    if (!c || typeof c !== 'object' || !Array.isArray(c.messages)) return null;
    const messages = c.messages.map(cleanMessage).filter(Boolean);
    return { id: String(c.id || fresh().id), updated: Number(c.updated) || Date.now(), messages };
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

  // An answer's row: [searching status] [body] [sources, search note] [notes and actions].
  function view(msg) {
    const row = el('div', 'msg ' + msg.role);
    const v = { row, msg, body: el('div', 'body'), status: null, extras: null, query: null, extrasKey: null };
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
    }
    paint(v, live !== null && live.msg === msg);
    return v;
  }

  function paint(v, streaming) {
    const m = v.msg;
    if (m.role === 'user') { v.body.textContent = m.content; return; }
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
      if (line === null) v.body.appendChild(typing(m.sources ? 'Reading the sources…' : 'Waiting for a volunteer’s computer…'));
    } else {
      markdown(v.body, m.content, m.sources);
      v.body.hidden = !m.content;
    }
    const key = JSON.stringify([m.sources || [], m.lookupNotes || [], m.searchNote || '']);
    if (key !== v.extrasKey) {   // sources are replaced as lookups finish
      v.extrasKey = key;
      clear(v.extras);
      if (m.sources) v.extras.appendChild(sourceList(m.sources));
      (m.lookupNotes || []).forEach((note) => v.extras.appendChild(el('p', 'search-note', note)));
      if (m.searchNote) v.extras.appendChild(el('p', 'search-note', m.searchNote));
      v.extras.hidden = !v.extras.firstChild;
    }
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
  function context(messages) {
    const picked = [];
    let budget = CONTEXT_CHARS;
    for (let i = messages.length - 1; i >= 0; i--) {
      const text = (messages[i].content || '').trim();
      if (!text) continue;
      if (picked.length && text.length > budget) break;
      budget -= text.length;
      picked.unshift({ role: messages[i].role, content: text });
    }
    while (picked.length > 1 && picked[0].role !== 'user') picked.shift();
    return [{ role: 'system', content: SYSTEM }].concat(picked);
  }

  function submit() {
    const text = ui.input.value.trim();
    if (!text || busy || Date.now() < cooldownUntil) return;
    current.messages.push({ role: 'user', content: text });
    ui.input.value = '';
    draft = '';
    autosize();
    ask();
  }

  function retry() {
    if (busy || Date.now() < cooldownUntil) return;
    const msgs = current.messages;
    if (msgs.length && msgs[msgs.length - 1].role === 'assistant') msgs.pop();
    if (msgs.length && msgs[msgs.length - 1].role === 'user') ask();
    else renderAll();
  }

  function retryAfter(resp, detail) {
    let seconds = parseInt(resp.headers.get('Retry-After') || '', 10);
    if (isNaN(seconds)) seconds = 5;
    const said = /(\d+)\s*seconds?/i.exec(detail || '');   // the detail may name a longer wait
    if (said) seconds = Math.max(seconds, parseInt(said[1], 10));
    return Math.min(Math.max(seconds, 1), 600);
  }

  async function detailOf(resp) {
    try {
      const data = await resp.json();
      return data && typeof data.detail === 'string' ? data.detail : '';
    } catch (e) { return ''; }
  }

  async function ask() {
    const conv = current;
    const payload = { messages: context(conv.messages) };
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
      const resp = await fetch(API, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
        body: JSON.stringify(payload),
        signal: ctrl.signal,
        cache: 'no-store',
      });
      if (resp.status === 429) {
        throttled = true;
        const detail = await detailOf(resp);
        throttle(conv, reply, detail || 'Too many messages right now.', retryAfter(resp, detail));
      } else if (!resp.ok) {
        reply.status = 'error';
        reply.note = (await detailOf(resp)) || ('DLNE could not answer (HTTP ' + resp.status + '). Please try again.');
      } else {
        await readStream(resp, reply);
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
      if (result === 'changed') schedule();
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

  function throttle(conv, reply, detail, seconds) {
    const msgs = conv.messages;
    if (msgs[msgs.length - 1] === reply) msgs.pop();
    const asked = msgs[msgs.length - 1];
    if (asked && asked.role === 'user') {
      msgs.pop();   // not sent: give the words back to the input
      if (!ui.input.value.trim()) { ui.input.value = asked.content; draft = asked.content; }
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
    ui.send.disabled = busy || cooling || !ui.input.value.trim() || navigator.onLine === false;
  }

  function autosize() {
    ui.input.style.height = 'auto';
    ui.input.style.height = ui.input.scrollHeight + 'px';
  }

  // ---------------------------------------------------------------- chats: new, saved, deleted
  function park() {
    if (controller) controller.abort();
    if (current.messages.some((m) => m.role === 'user')) {
      saved = [current].concat(saved.filter((c) => c.id !== current.id)).slice(0, MAX_SAVED);
    }
  }

  function newChat() {
    park();
    current = fresh();
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
    save();
    renderAll();
  }

  function titleOf(c) {
    const first = c.messages.find((m) => m.role === 'user');
    return first ? first.content.replace(/\s+/g, ' ').slice(0, 80) : 'Empty chat';
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

  // ---------------------------------------------------------------- the pool's status
  let lastModel = '';
  async function refreshStatus() {
    if (navigator.onLine === false) { setStatus('off', "You're offline", lastModel); return; }
    try {
      const resp = await fetch(STATUS, { cache: 'no-store' });
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      const data = await resp.json();
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
    ui.about.textContent = (model ? 'Answers come from ' + model + ', running on computers people lend to DLNE.'
      : 'Answers come from computers people lend to DLNE.') + ' They can be slow, and they can be wrong.';
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
    clearTimeout(draftTimer);
    draftTimer = setTimeout(() => { draft = ui.input.value; save(); }, 400);
  });
  ui.stop.addEventListener('click', () => { if (controller) controller.abort(); });
  $('new-chat').addEventListener('click', newChat);
  $('open-menu').addEventListener('click', () => { renderSaved(); openDialog(ui.menu); });
  $('close-menu').addEventListener('click', () => closeDialog(ui.menu));
  ui.menu.addEventListener('click', (e) => { if (e.target === ui.menu) closeDialog(ui.menu); });
  ui.clearAll.addEventListener('click', () => {
    if (!window.confirm('Delete all your chats on this device?')) return;
    if (controller) controller.abort();
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
    ui.input.focus();
  }));
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
    if (document.visibilityState === 'visible') refreshStatus();
    else save();
  });
  window.addEventListener('pagehide', save);
  setInterval(() => { if (document.visibilityState === 'visible') refreshStatus(); }, 30000);
  if (vv) {
    vv.addEventListener('resize', fitViewport);
    vv.addEventListener('scroll', fitViewport);
    fitViewport();
  }

  load();
  ui.input.value = draft;
  autosize();
  renderAll();
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
    "description": "Free AI chat, answered by computers people lend to DLNE.",
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
