"""
The landing page a DLNE coordinator serves at ``/``: where to get the extension, and whether the
swarm is up right now. (Phones are pointed at the chat page, engine/chat_page.py, at ``/chat``.)

DLNE is a VS Code extension. The coordinator used to serve a browser chat room at ``/``; that is
gone, and anyone who finds the address in a browser should be pointed at the extension instead.
The page is a single self-contained document (no external scripts, fonts or images) so it loads
instantly, works behind any firewall, and cannot be broken by a CDN. Its one image is the DLNE
logo and its favicon, the same PNGs the phone chat serves (engine/icons.py, at ``/chat/<name>``).
"""

from engine.icons import icon_url

EXTENSION_URL = "https://open-vsx.org/extension/dlne/dlne-code-assistant"
DISCORD_INVITE_URL = "https://discord.gg/RNxvxWn8cw"

_LANDING_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DLNE - free AI for your editor</title>
<meta name="description" content="DLNE is a free AI coding assistant for VS Code, powered by a swarm of shared GPUs.">
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
.button {
  display: inline-block; background: var(--accent); color: var(--accent-text);
  text-decoration: none; font-weight: 600; padding: 12px 20px; border-radius: 8px;
}
.button:focus-visible, a:focus-visible { outline: 3px solid var(--accent); outline-offset: 2px; }
a { color: var(--accent); }
.status { display: flex; gap: 10px; align-items: center; }
.dot { width: 10px; height: 10px; border-radius: 50%; background: var(--muted); flex: none; }
.dot.up { background: var(--ok); }
.dot.down { background: var(--warn); }
.muted { color: var(--muted); font-size: 0.9rem; }
footer { margin-top: 28px; }
</style>
</head>
<body>
<main>
  <img class="logo" src="{{ICON_192}}" width="72" height="72" alt="DLNE">
  <h1>Free AI for your editor</h1>
  <p class="lead">DLNE is a coding assistant for VS Code, powered by a swarm of shared GPUs. No subscription, no token fees: give a little compute, get a lot of AI.</p>

  <p><a class="button" href="https://open-vsx.org/extension/dlne/dlne-code-assistant" rel="noopener">Get the extension</a></p>
  <p>On your phone? <a href="/chat">Open DLNE Chat</a>, free in the browser.</p>

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
    <a href="https://discord.gg/RNxvxWn8cw" rel="noopener">Join the Discord</a>
    <span id="version"></span>
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
      if (s.server_version) { version.textContent = ' - coordinator v' + s.server_version; }
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
                .replace("{{ICON_192}}", icon_url("icon-192.png"))
                .replace("{{ICON_FAVICON_32}}", icon_url("favicon-32.png"))
                .replace("{{ICON_FAVICON_48}}", icon_url("favicon-48.png")))
