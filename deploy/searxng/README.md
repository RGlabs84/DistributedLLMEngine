# SearXNG for the phone chat's web search

`web_search` (engine/search.py) asks this SearXNG first, then Brave (only with a key, and only
`SEARCH_DAILY_LIMIT` times a day), then Wikipedia. Without SearXNG everything still works; it
just leans on the fallbacks. It runs on TheWall as a systemd **user** service in a virtualenv,
listening on 127.0.0.1:8888 only, which is DLNE's default `SEARXNG_URL`, so there is nothing to
set in DLNE's config.

## Install (no Docker)

As your own user on TheWall. The packages are the ones SearXNG's install guide lists, without
uWSGI, which this setup does not use.

```sh
# Debian/Ubuntu
sudo apt-get install -y python3-dev python3-babel python3-venv git build-essential \
    libxslt-dev zlib1g-dev libffi-dev libssl-dev
# Arch
sudo pacman -S --needed python python-pip python-lxml python-babel git base-devel libxml2

git clone https://github.com/searxng/searxng ~/searxng/src
python3 -m venv ~/searxng/venv
~/searxng/venv/bin/pip install -U pip setuptools wheel pyyaml msgspec typing-extensions pybind11
cd ~/searxng/src && ~/searxng/venv/bin/pip install --use-pep517 --no-build-isolation -e .

# Settings, from the DLNE checkout, with a random secret_key in place of the placeholder
# (SearXNG will not start until it is replaced).
mkdir -p ~/.config/searxng
cp deploy/searxng/settings.yml ~/.config/searxng/settings.yml
sed -i "s/ultrasecretkey/$(openssl rand -hex 32)/" ~/.config/searxng/settings.yml
chmod 600 ~/.config/searxng/settings.yml
```

Save this as `~/.config/systemd/user/searxng.service`:

```ini
[Unit]
Description=SearXNG for DLNE (loopback only)

[Service]
Environment=SEARXNG_SETTINGS_PATH=%h/.config/searxng/settings.yml
WorkingDirectory=%h/searxng/src
ExecStart=%h/searxng/venv/bin/python -m searx.webapp
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

```sh
systemctl --user daemon-reload
systemctl --user enable --now searxng
# Keep user services running when nobody is logged in to TheWall.
loginctl enable-linger "$USER"
```

`python -m searx.webapp` is SearXNG's own way to run it outside Docker. With one caller on
loopback it is enough; Granian (`pip install granian` in the same venv) is the faster server
SearXNG's container uses, if it is ever needed.

## Check it

```sh
systemctl --user status searxng
journalctl --user -u searxng -n 50
ss -ltn | grep 8888          # must show 127.0.0.1:8888, never 0.0.0.0 or [::]
curl -s 'http://127.0.0.1:8888/search?q=weather+radar&format=json' | python3 -m json.tool | head -40
```

A healthy answer has a non-empty `"results"` list. `"unresponsive_engines"` names the engines
that failed or timed out on that query. An HTTP 403 means `json` is missing from
`search.formats`. On DLNE's side, the coordinator's status shows `web_search_order` starting
with `searxng`, and `service_calls_today.searxng` rising as the phone chat searches.

To update: `cd ~/searxng/src && git pull && ~/searxng/venv/bin/pip install --use-pep517
--no-build-isolation -e . && systemctl --user restart searxng`.

## If the engines start refusing

Every engine sees TheWall's household address. If `unresponsive_engines` keeps reporting
CAPTCHAs or access denied, first drop the engine that refuses from `keep_only`. If they all do,
SearXNG can send its requests through Tor:

```sh
sudo apt-get install -y tor        # or: sudo pacman -S tor
sudo systemctl enable --now tor    # SOCKS proxy on 127.0.0.1:9050
```

Then uncomment the `proxies`, `using_tor_proxy` and `extra_proxy_timeout` lines under
`outgoing:` in `~/.config/searxng/settings.yml` and restart the service. Tor adds a second or
two to every request, so also raise `request_timeout` and `max_request_timeout`. The coordinator
gives SearXNG 3 seconds (`SEARXNG_TIMEOUT_SECONDS` in engine/search.py); past that, lookups
fall through to Brave or Wikipedia, so raise it to match if Tor becomes the normal path.
