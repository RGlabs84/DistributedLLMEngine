# DLNE: free AI for coding and for your phone

DLNE is a free AI assistant that runs on graphics cards (GPUs) people lend to a shared swarm. It helps
you write code in VS Code, and it chats on your phone. There is no subscription and no per-message fee.

The rule is simple: give a little compute, get a lot of AI. Everyone gets a number of free chats to try
it. After that, lending an idle GPU or a fast CPU to the swarm keeps it free. You can also run a coordinator
of your own and keep everything on your own machines (see [Run your own coordinator](#run-your-own-coordinator)).

Live status, the current model and the free-chat allowance are on the website, not in this file:
[dlne.rossgilsonlabs.dev](https://dlne.rossgilsonlabs.dev).

## Use the public swarm in your editor

1. **Install the extension.** Search the Extensions view for **DLNE Code Assistant**.
   - VS Code: the [VS Code Marketplace](https://marketplace.visualstudio.com/items?itemName=dlne.dlne-code-assistant).
   - VSCodium, Cursor, Theia and other Open VSX editors: [Open VSX](https://open-vsx.org/extension/dlne/dlne-code-assistant).
   - Offline or locked down: the `.vsix` is on the [latest release](https://github.com/RGlabs84/DistributedLLMEngine/releases/latest)
     (Extensions view, the `...` menu, **Install from VSIX...**).
2. **Chat.** There is no account and no sign-up. Pick whichever you like:
   - Type `@dlne` in VS Code's Chat view, followed by what you want.
   - Or pick **DLNE Swarm** in the Chat view's model picker. VS Code's own Ask, Edit and Agent modes then run
     on the swarm. This also lets `@dlne` work for someone who is not signed in to GitHub.
   - Or open the **DLNE Agent** sidebar (Activity Bar). It is DLNE's own chat, for editors with no Chat view
     and for anyone who prefers it.
3. **The free trial.** A new user gets free chats to try DLNE, counted per network address, with no sign-up.
   The status bar shows what is left. When it runs out, contribute compute (below) or run your own coordinator.

[What each way of chatting is, and when to use it](vscode-extension/README.md#chatting-with-dlne).

**Privacy, honestly.** To answer you, a machine has to read your question and the code sent with it. DLNE
removes secrets in the editor first. It never reads files such as `.env` or `*.pem`, anything `.gitignore`
ignores, or anything in `.dlneignore`, when you use `@dlne` or the sidebar. (VS Code's own agent on DLNE Swarm is
the exception: its tools read the files, so DLNE refuses the excluded ones it can see by path and redacts
secrets, but VS Code's own tools may still put file text into a request.) Private repositories are answered only
by machines the operator trusts, unless you choose otherwise with `dlne.privacy`. Requests to a volunteer's
machine are encrypted on the way. A photo in the phone chat stays on DLNE's own server and is never sent to a
volunteer's computer. Like every message, it is processed by the shared model, whose layers can run on
volunteers' graphics cards, and those cards see only intermediate numbers, never the picture. Read the whole
thing, including what none of this can promise, in [`docs/privacy.md`](docs/privacy.md).

## The phone app

You don't need an editor to use DLNE.

- **In a browser:** open [DLNE Chat](https://dlne.rossgilsonlabs.dev/chat). You can install it as an app:
  in Chrome use the install option in the menu, on an iPhone use Safari, Share, Add to Home Screen. In VS Code,
  the command **DLNE: Use DLNE on Your Phone** opens the chat page of the coordinator your extension uses.
- **Android app:** the APK is on the website (it is not on Google Play). Download it from
  [dlne.rossgilsonlabs.dev](https://dlne.rossgilsonlabs.dev).
- **Photos:** add one with the **+** next to the message box and ask about it. It shows only where the server
  offers it.
- **Pictures:** ask for one ("can I get a picture of a fox in the snow?"). It shows only where the server
  offers it.
- **Location:** the chat asks before it uses where you are, and only when a question needs it (the weather,
  for example). If you say no, it asks nothing more of your location.

The phone chat is a separate path from the editor: the redaction and encryption above are not promised for it.
See [`docs/privacy.md`](docs/privacy.md#the-phone-chat-is-separate).

## Contribute compute

Lending compute is how the swarm stays free. You can lend a GPU, a CPU, or both.

| You have | You become | Roughly |
|---|---|---|
| A GPU with 8 GB of memory or more | A **coding member**: your GPU runs a whole coding model and answers other people's coding requests, adding agents for everyone | Needs the GPU plus free system memory for the model (the extension tells you if yours is enough) |
| A smaller or shared GPU | Part of the **GPU pool**: one big model run together with other volunteers | Any GPU the swarm counts |
| A fast CPU with spare memory | A **phone-app member**: your CPU answers phone chats while the main engine is down, and the memory you lend picks the model | 4 physical cores, AVX2, and 8 GB or more lent |

**Two ways to do it:**

- **DLNE Donate** is a small tray app for Windows and Linux, for people who don't use VS Code. Download it from the
  website. It does the same job as the extension's worker. See [`desktop/README.md`](desktop/README.md).
- **The extension's Donate compute panel** (in the DLNE sidebar), or the command **DLNE: Toggle Compute
  Contribution**. [Details and settings](vscode-extension/README.md).

**What it costs you.** Your machine steps aside when you need it: while you game or render, the swarm gives it
back. You can cap the memory you lend (`dlne.worker.maxVramGb`, `dlne.worker.maxRamGb`), refuse the coding-member
job (`dlne.worker.allowCodingMember`), or lend your CPU only. Nothing runs until you switch it on. A volunteer
who answers a request can read it: see the privacy note above and in [`docs/privacy.md`](docs/privacy.md).

**What you get.** Your own chats go first when the engine is busy: contributors have priority over the free
trial. Pictures made on your network are free of trial charges. More machines also mean bigger models for
everyone.

## Run your own coordinator

Everything is open source. A coordinator is the server the extension and the phone app talk to. Run your own to
keep code on machines you control, to use DLNE without the free-trial limit, or to share one big GPU box with a
team.

**You need:** Linux, Windows or macOS with Python 3.10 or newer, and a GPU if you want the model on a GPU
(a CPU-only box runs the phone chat, not the coding agent).

### 1. Install

```bash
git clone https://github.com/RGlabs84/DistributedLLMEngine.git
cd DistributedLLMEngine
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
mkdir -p config bin models
```

### 2. Get the inference programs

The coordinator does not download `llama-server` for itself. It looks in `bin/` and answers "Binary Missing" if it
is not there. Download a [llama.cpp release](https://github.com/ggml-org/llama.cpp/releases) built for your
hardware (the Vulkan build works on AMD, Intel and NVIDIA) and put `llama-server` and `ggml-rpc-server`, with the
libraries that come beside them, into `bin/`. The build the public swarm uses is named in `llama_build` at
`/v1/release` on the website.

### 3. Set the network key

Create `config/config.txt` (one `KEY=VALUE` per line; it is not in git):

```
NETWORK_KEY=pick-a-long-random-secret
```

Make the secret long and random, for example the output of `python -c "import secrets; print(secrets.token_hex(32))"`.
Workers prove they belong to your swarm by signing their messages with this key, and a coordinator with no
`NETWORK_KEY` refuses all workers. Never use the public swarm's key. Keep the file private.

Other keys you will probably want (the full list is in the [operator reference](docs/operator-reference.md)):

| Key | What it does |
|---|---|
| `DEFAULT_MODEL` | The model to start on. Left out, a fresh coordinator starts on the first model of the signed ladder. |
| `ENGINE_SLOTS`, `ENGINE_CONTEXT` | Coding request slots and total context. `0` (the default) lets the engine size itself from the model and the free GPU memory. |
| `ENGINE_PHONE_SLOTS` | `0` to `8`, default `0` (off). Lanes reserved for the phone chat. The phone chat needs these, or CPU volunteers, or `APP_LOCAL_ENABLED=true` on this machine. |
| `ENGINE_MMPROJ` | Path to a vision projector `.gguf`: lets the engine read photos and images. |
| `IMAGE_GEN_ENABLED` | `true` lets phones and the IDE ask for pictures (off by default; needs a second GPU, see [`docs/image-generation.md`](docs/image-generation.md)). |
| `ENFORCE_CONTRIB_CLAUSE` | See "Who may use it" below. |
| `TRUST_PROXY_HEADERS` | `true` when a reverse proxy sits in front. |
| `CUSTOM_GGUF_DIR` | Where model files live (default: `models/` in the install folder). |
| `BIND_IP`, `BIND_PORT` | Address and port to listen on (default `0.0.0.0` and `11435`). |

### 4. Start it

```bash
python dlne_server.py start --port 11435
```

`start` runs the coordinator and, always, a worker on the same machine (a "hybrid"), so the box's own GPU
joins your swarm. Open `http://<this-machine>:11435/` for the status page. `python dlne_server.py status` asks a
running coordinator how it is doing.

### 5. Put a model on it

The coordinator can fetch its own model: with auto-swap on (the default) it downloads the right model from the
signed ladder (`scripts/coding_models.json`) into `models/` and switches to bigger ones as more GPUs join. It checks free disk first and
verifies each file. To pick the model yourself, set `DEFAULT_MODEL`, or put a `.gguf` file in `models/` and
set `AUTO_SWAP=false`. (`download_dlne_models.sh` in the repo is an old script that fetches about 1.3 TB.
Don't use it.)

### 6. Who may use it

- **Your own machine** (the one running the coordinator) is always let in.
- **Everyone else** (your laptop on the LAN, a teammate) needs an API key. Make one on the coordinator:

  ```bash
  python dlne_server.py apikey create --name "my-laptop"    # prints a dlne_sk_... key once
  python dlne_server.py apikey list
  python dlne_server.py apikey revoke <key_id>
  ```

  "This machine" means an address of the coordinator's own computer, not "the local network". A laptop on the same
  Wi-Fi is not exempt.
- On a coordinator that is private anyway (a home network, a VPN), `ENFORCE_CONTRIB_CLAUSE=false` lets everyone
  in without keys and turns off the free-trial limit. Use it only when you trust everyone who can reach the port.
- Everything under `/v1/admin/*` needs the operator key: `python dlne_server.py adminkey create`. It is accepted
  only from the coordinator's own machine unless you set `ADMIN_ALLOW_REMOTE=true`.

### 7. Keep it running (systemd)

`python dlne_server.py install-systemd` writes a *system* unit to `daemon/systemd/dlne.service` (copy it to
`/etc/systemd/system/`). For a service that runs as you, with no root, make a *user* unit at
`~/.config/systemd/user/dlne.service`:

```ini
[Unit]
Description=DLNE coordinator
After=network.target

[Service]
WorkingDirectory=/home/you/DistributedLLMEngine
ExecStart=/home/you/DistributedLLMEngine/.venv/bin/python dlne_server.py start --host 0.0.0.0 --port 11435
Restart=on-failure
RestartSec=5s
LimitNOFILE=65536

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now dlne.service
loginctl enable-linger "$USER"        # keep it running when you are logged out
journalctl --user -u dlne.service -f  # the log
```

### 8. HTTPS and ports

Editors and phones should reach the coordinator over HTTPS. With [Caddy](https://caddyserver.com) on the same
machine, a `Caddyfile` is enough (it gets the certificate itself):

```
dlne.example.com {
    reverse_proxy 127.0.0.1:11435
}
```

Then set `TRUST_PROXY_HEADERS=true` in `config/config.txt` so each client is told apart by its address, and keep
`/v1/admin/*` off the proxy. Workers use WebSockets through the same address, which Caddy passes on by default.

| Port | What | Open it? |
|---|---|---|
| 11435 (TCP) | The coordinator: editors, phones and workers. | Yes, or only 443 if everything goes through the proxy. |
| 80 and 443 | Caddy (certificate and HTTPS). | Yes, if you use it. |
| 50052 | The coordinator machine's own RPC worker (`RPC_PORT`). | No. Keep it closed to the outside. Remote workers dial out to the coordinator and need no inbound port. |

### 9. Add workers

Other machines join with the command line from a checkout of this repository:

```bash
python dlne_server.py worker --coordinator https://dlne.example.com --network-key "pick-a-long-random-secret"
```

Without a proxy, use `host:11435` (the worker adds `http://`). The worker also needs a `ggml-rpc-server` of its own in its `bin/`.
Useful options are `--allowed-gpus`, `--max-vram` and `--max-ram`.

**A signed-release catch.** The public swarm's workers fetch their engine from the coordinator, and accept it
only if it is signed with the maintainer's release key, which is built into the extension. A private coordinator
cannot sign for that key. So a worker started from a checkout (as above) runs the code you checked out, and the
VS Code extension needs `dlne.worker.allowUnsignedEngine` turned on to install an engine from your coordinator.
The tray app (DLNE Donate) always talks to the public swarm and cannot join a private one. To publish signed
releases of your own, you would fork the project and replace the key.

## Point your extension at your own coordinator

Open VS Code settings (JSON) and set:

```json
{
  "dlne.serverUrl": "https://dlne.example.com",
  "dlne.apiKey": "dlne_sk_your_key_here",
  "dlne.worker.networkKey": "pick-a-long-random-secret",
  "dlne.worker.allowUnsignedEngine": true,
  "dlne.privacy": "trustedOnly"
}
```

| Setting | Why |
|---|---|
| `dlne.serverUrl` | The address of your coordinator, with `http://` or `https://` and no trailing path. Leave it empty and you are on the public swarm. |
| `dlne.apiKey` | The `dlne_sk_...` key from `apikey create`. Needed unless you set `ENFORCE_CONTRIB_CLAUSE=false` on the coordinator. |
| `dlne.worker.networkKey` | Only if this machine also lends compute to your coordinator. It must equal your `NETWORK_KEY`. Its default is the public swarm's key. |
| `dlne.worker.allowUnsignedEngine` | Only if this machine runs the worker through the extension (see the signed-release catch above). Never turn it on for the public swarm. |
| `dlne.privacy` | `auto` (default), `trustedOnly` or `anyMember`. Only matters when your coordinator has coding members. `trustedOnly` keeps all requests on your own engine and machines you listed in `TRUSTED_MEMBERS`. |

Reload the window after changing them.

### Check that it worked

- **Status bar.** It reads the swarm state from your coordinator. If it cannot reach it, it shows
  **DLNE: Offline**, and its tooltip names the address it tried ("Could not connect to DLNE Cluster at ...").
- **`@dlne /status`** in the Chat view, and the **DLNE: Open Swarm Dashboard** command, show the model and the
  machines of the coordinator you set.
- **`DLNE: Use DLNE on Your Phone`** opens `<your serverUrl>/chat`. If that is your own address, the extension
  is pointed at you.
- **On the coordinator:** watch its log (`journalctl --user -u dlne.service -f`) while you chat. Your request
  appears there. `curl https://dlne.example.com/v1/cluster/status` shows what it is running.

### Does anything go to the public swarm?

**No, not your chats and code.** The extension has no fallback. If your coordinator is down, it shows
**Offline** and the chat fails with an error. It never quietly switches to the public swarm. The only way to
land on the public swarm is an empty `dlne.serverUrl`, or one of three retired public addresses, which are read
as the public one.

Things to know so it stays that way:

- Keep `dlne.serverUrl` set in your **User** settings, not only in a workspace, or a different workspace can
  point somewhere else.
- A key or token is sent to whatever address you set, including a plain `http://` one. Use HTTPS beyond your own
  network.
- A worker already running keeps the coordinator it started with. After you change `dlne.serverUrl`, stop the
  worker and start it again.
- `dlne.worker.networkKey` defaults to the public key. If this machine lends compute, set it, or it signs for
  the wrong swarm.
- Other, smaller calls that are not about your chats: the extension checks for updates on Open VSX
  (`dlne.autoUpdate`; turn it off to stop that), and, when your coordinator has coding members and
  `dlne.privacy` is `auto`, asks GitHub, GitLab or Codeberg whether a repository is public.
- The tray app, DLNE Donate, only ever uses the public swarm.

More on this in [the extension's README](vscode-extension/README.md#your-own-private-coordinator).

## Help, questions and support

- Questions and news: the [Discord](https://discord.gg/RNxvxWn8cw).
- [Buy a coffee](https://buymeacoffee.com/wubarrk) toward the power and hardware of the public coordinator.
- Report security problems privately to the maintainer, not in a public issue.
- Everything about where data goes: [`docs/privacy.md`](docs/privacy.md).

## For developers

| Path | What it is |
|---|---|
| `vscode-extension/` | The extension: chat agent, autocomplete, one-click compute sharing |
| `engine/` | The coordinator and worker: scheduling, worker registry, free-trial quota, admin API |
| `dlne_server.py` | Command line: `start`, `worker`, `apikey`, `adminkey`, `status`, `install-systemd` |
| `android/`, `desktop/` | The Android app and the DLNE Donate tray app |
| `docs/` | [`privacy.md`](docs/privacy.md), [`operator-reference.md`](docs/operator-reference.md) (every coordinator setting), [`coding-members.md`](docs/coding-members.md), [`image-generation.md`](docs/image-generation.md), [`agent-memory.md`](docs/agent-memory.md) |
| `tests/` | The Python test suite |
| `scripts/` | Release tooling and model lists |

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m pytest                        # engine and coordinator

cd vscode-extension
npm install
npm test                                # extension unit tests (no VS Code needed)
```

**Releases.** Workers run code and binaries they download from the coordinator, so every release is signed:
`scripts/release.py` builds a reproducible engine archive and signs a manifest (Ed25519) with the maintainer's
key. The extension and the worker refuse anything that does not match it, and never downgrade. Details are in the
[operator reference](docs/operator-reference.md#releases). The history is in the [changelog](CHANGELOG.md).

## License

MIT, see [LICENSE.md](LICENSE.md).
