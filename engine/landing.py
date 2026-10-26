"""
The landing page a DLNE coordinator serves at ``/``: where to get the extension, how to chat in the
browser, and whether the swarm is up right now.

DLNE is a VS Code extension. The coordinator used to serve a browser chat room at ``/``; that is
gone, and anyone who finds the address in a browser should be pointed at the extension instead.
Phones cannot run it, so they are pointed at the phone chat (engine/chat_page.py, at ``/chat``):
on a narrow screen (a CSS media query, no user-agent sniffing) "Open DLNE Chat" becomes the big
primary button at the top and "Get the extension" the secondary one; on a wide screen it is the
other way round.
The page is a single self-contained document (no external scripts, fonts or images) so it loads
instantly, works behind any firewall, and cannot be broken by a CDN. Its one image is the DLNE
logo and its favicon, the same PNGs the phone chat serves (engine/icons.py, at ``/chat/<name>``).

The outside links live here as constants; the phone chat imports them (engine/chat_page.py).
"""

from engine.icons import icon_url

EXTENSION_URL = "https://open-vsx.org/extension/dlne/dlne-code-assistant"
DISCORD_INVITE_URL = "https://discord.gg/RNxvxWn8cw"
WEBSITE_URL = "https://live.ravenirongames.com/#dlne"
# The public swarm's address, for link previews (LinkedIn, Discord, X need absolute URLs).
PUBLIC_URL = "https://dlne.duckdns.org"
# One preview card for both pages: the official logo.
OG_IMAGE_URL = PUBLIC_URL + icon_url("icon-512.png")

_LANDING_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DLNE - free AI for your editor</title>
<meta name="description" content="DLNE is a free AI coding assistant for VS Code, powered by a swarm of shared GPUs.">
<meta property="og:type" content="website">
<meta property="og:site_name" content="DLNE">
<meta property="og:title" content="DLNE - free AI for your editor and your phone">
<meta property="og:description" content="A free AI coding assistant for VS Code and a free phone chat, powered by computers people share. No subscription.">
<meta property="og:url" content="{{PUBLIC_URL}}/">
<meta property="og:image" content="{{OG_IMAGE_URL}}">
<meta name="twitter:card" content="summary">
<link rel="icon" type="image/png" sizes="32x32" href="{{ICON_FAVICON_32}}">
<link rel="icon" type="image/png" sizes="48x48" href="{{ICON_FAVICON_48}}">
<style>
:root {
  --bg: #f6f7f9; --card: #ffffff; --text: #14181f; --muted: #5a6472;
  --accent: #2563eb; --accent-text: #ffffff; --border: #dde1e7; --ok: #15803d; --warn: #b45309;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0e1116; --card: #161b22; --text: #e8ecf1; --muted: #9aa5b4;
    --accent: #5b9dff; --accent-text: #0b1220; --border: #2a313c; --ok: #4ade80; --warn: #fbbf24;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 16px/1.55 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
main { max-width: 640px; margin: 0 auto; padding: 48px 16px 64px; }
.logo { display: block; width: 72px; height: 72px; margin: 0 0 20px; border-radius: 22%; }
h1 { font-size: 2rem; line-height: 1.2; margin: 0 0 8px; }
.lead { color: var(--muted); font-size: 1.1rem; margin: 0 0 28px; }
.card {
  background: var(--card); border: 1px solid var(--border); border-radius: 12px;
  padding: 20px; margin: 0 0 16px;
}
.card h2 { font-size: 1rem; margin: 0 0 12px; }
ol { margin: 0; padding-left: 1.2rem; }
li { margin: 0 0 10px; }
li:last-child { margin-bottom: 0; }
.actions { display: flex; flex-wrap: wrap; gap: 12px; margin: 0 0 12px; }
.button {
  display: inline-block; text-align: center; text-decoration: none; font-weight: 600;
  padding: 12px 20px; border-radius: 8px; border: 2px solid var(--accent);
  background: var(--accent); color: var(--accent-text);
}
/* Wide screens: the extension is the primary button and the browser chat the quiet one. */
.button.chat { background: transparent; color: var(--accent); }
.button:focus-visible, a:focus-visible { outline: 3px solid var(--accent); outline-offset: 2px; }
a { color: var(--accent); }
.status { display: flex; gap: 10px; align-items: center; }
.dot { width: 10px; height: 10px; border-radius: 50%; background: var(--muted); flex: none; }
.dot.up { background: var(--ok); }
.dot.down { background: var(--warn); }
.muted { color: var(--muted); font-size: 0.9rem; }
footer { margin-top: 28px; }
footer p { margin: 0 0 4px; }
footer a { display: inline-block; padding: 6px 0; }
/* Phones (no user-agent sniffing, just the screen's width): the chat is what a phone can use, so it
   becomes the big primary button at the top and the extension the secondary one. */
@media (max-width: 600px) {
  main { padding: 28px 16px 48px; }
  .logo { width: 56px; height: 56px; margin-bottom: 14px; }
  h1 { font-size: 1.75rem; }
  .lead { font-size: 1rem; margin-bottom: 20px; }
  .actions { flex-direction: column; }
  .button { padding: 14px 20px; font-size: 1.05rem; }
  .button.chat { order: -1; background: var(--accent); color: var(--accent-text); padding: 18px 20px; font-size: 1.25rem; }
  .button.ext { background: transparent; color: var(--accent); }
}
</style>
</head>
<body>
<main>
  <img class="logo" src="{{ICON_192}}" width="72" height="72" alt="DLNE">
  <h1>Free AI for your editor</h1>
  <p class="lead">DLNE is a coding assistant for VS Code, powered by a swarm of shared GPUs. No subscription, no token fees: give a little compute, get a lot of AI.</p>

  <div class="actions">
    <a class="button ext" href="{{EXTENSION_URL}}" rel="noopener">Get the extension</a>
    <a class="button chat" href="/chat">Open DLNE Chat</a>
  </div>
  <p class="muted">No install needed: the chat is free in any browser.</p>

  <div class="card">
    <h2>How it works</h2>
    <ol>
      <li>Install <strong>DLNE Code Assistant</strong> from the Extensions view in VS Code, VSCodium or Cursor.</li>
      <li>Open the DLNE sidebar, or type <code>@dlne</code> in the chat panel. Your first 100 chats are free, no sign-up.</li>
      <li>To keep going, share your idle GPU with the swarm in one click. That is the whole rule: give compute, receive compute.</li>
    </ol>
  </div>

  <div class="card" aria-live="polite">
    <h2>Swarm right now</h2>
    <div class="status">
      <span class="dot" id="dot"></span>
      <span id="status-text">Checking...</span>
    </div>
    <p class="muted" id="status-detail"></p>
  </div>

  <footer class="muted">
    <p><a href="{{WEBSITE_URL}}" rel="noopener">DLNE website</a> &middot; <a href="{{DISCORD_INVITE_URL}}" rel="noopener">Join the Discord</a></p>
    <p id="version"></p>
  </footer>
</main>
<script>
(function () {
  var dot = document.getElementById('dot');
  var text = document.getElementById('status-text');
  var detail = document.getElementById('status-detail');
  var version = document.getElementById('version');
  fetch('/v1/cluster/status', { cache: 'no-store' })
    .then(function (r) { if (!r.ok) { throw new Error('status ' + r.status); } return r.json(); })
    .then(function (s) {
      var workers = s.active_workers_count || 0;
      var up = s.status === 'Active' && workers > 0;
      dot.className = 'dot ' + (up ? 'up' : 'down');
      text.textContent = up ? 'Online' : (s.status || 'Waiting for nodes');
      detail.textContent = workers + (workers === 1 ? ' node' : ' nodes') + ' sharing ' +
        (s.total_vram_gb || 0) + ' GB of GPU memory' +
        (s.global_active_model ? ' - serving ' + s.global_active_model.replace(/\\.gguf$/, '') : '');
      if (s.server_version) { version.textContent = 'Coordinator v' + s.server_version; }
    })
    .catch(function () {
      dot.className = 'dot down';
      text.textContent = 'Status unavailable';
    });
})();
</script>
</body>
</html>
"""

LANDING_HTML = (_LANDING_TEMPLATE
                .replace("{{PUBLIC_URL}}", PUBLIC_URL)
                .replace("{{OG_IMAGE_URL}}", OG_IMAGE_URL)
                .replace("{{EXTENSION_URL}}", EXTENSION_URL)
                .replace("{{WEBSITE_URL}}", WEBSITE_URL)
                .replace("{{DISCORD_INVITE_URL}}", DISCORD_INVITE_URL)
                .replace("{{ICON_192}}", icon_url("icon-192.png"))
                .replace("{{ICON_FAVICON_32}}", icon_url("favicon-32.png"))
                .replace("{{ICON_FAVICON_48}}", icon_url("favicon-48.png")))
