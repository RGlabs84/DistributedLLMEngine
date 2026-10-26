# DLNE Coding Assistant

[![Open VSX Version](https://img.shields.io/open-vsx/v/dlne/dlne-code-assistant?color=blue&label=Open%20VSX)](https://open-vsx.org/extension/dlne/dlne-code-assistant)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**A free AI coding agent and autocomplete for VS Code, running on GPUs that its users share.**

No account, no API key, no subscription. Install it, open the DLNE sidebar, and ask it to write or fix code. It reads the files in your workspace, and asks before it changes a file, runs a command, or looks outside your workspace.

> **Upgrading to 1.3.0?** `dlne.serverUrl`, `dlne.apiKey`, `dlne.autoApproveReads`, `dlne.autoUpdate` and the `dlne.worker.*` settings are now read only from your user settings. If you set one of them in a workspace's `.vscode/settings.json`, move it to your user settings (`Preferences: Open User Settings`), or DLNE uses the default. This stops a cloned repository from sending your code to another server. [More below](#settings).

- **Agent chat**: in the DLNE sidebar, or as `@dlne` in VS Code's Chat view.
- **Ghost-text autocomplete**: suggestions inline as you type.
- **Fix problems**: `DLNE: Fix Current File Problems` sends the file's errors and warnings to the agent.
- **Secrets stay in your editor**: API keys, tokens, private keys and passwords in common formats are replaced with `[REDACTED]` before a request leaves VS Code, and files like `.env` and `*.pem` are never sent. Private repositories are answered only by trusted machines unless you opt in. [Details below](#privacy-what-to-know).
- **On your phone**: [DLNE Chat](https://dlne.duckdns.org/chat) is the same swarm in a phone browser (on an iPhone: Safari, Share, Add to Home Screen), and there is an Android app (the APK is on the [DLNE website](https://live.ravenirongames.com/#dlne); it is not on Google Play yet). In VS Code, use the **Use DLNE on your phone** link in the sidebar, or the command `DLNE: Use DLNE on Your Phone`.

---

## How it stays free

DLNE runs on a **swarm**: a coordinator plus the GPUs of the people who use it.

1. **Try it**: **100 free chats**, with nothing to set up. Autocomplete does not use them up, and the status bar shows how many are left. They're counted once per internet connection on the server, so reinstalling or clearing settings doesn't reset them.
2. **Keep using it**: lend some of your computer to the swarm with one click (**Start Contributing** in the sidebar).

| You lend | Coding agent | When the swarm is busy |
|---|---|---|
| **A GPU** | Unlimited | First in line |
| **A CPU** | 300 chats in any 5 hours (each frees up 5 hours after you used it) | Second in line |
| Nothing | The 100 free chats | After contributors |

A CPU contributor's chats show how many are left, and at the limit the agent says exactly when the next one frees up.

You choose how much to lend (`dlne.worker.maxVramGb`, `dlne.worker.maxRamGb`, `dlne.worker.allowedGpus`), and you can stop at any time.

> **Contributing works on Windows and Linux (x64 and ARM64) and on macOS (Apple Silicon and Intel).** GPUs are used through Vulkan on Windows and Linux and Metal on Apple Silicon. Intel Macs contribute their CPU (llama.cpp has no GPU support for them). The ARM64 and Intel Mac builds are the newest and the least tested.

### 8 GB of GPU memory or more? Be a coding member

A **coding member** runs a whole coding model on your GPU and answers coding requests on its own, so one request touches one machine and ordinary home internet is enough. **Each member adds about two more agents** working at the same time, so the swarm can run more agents as members join.

There is no separate switch to find. Click **Start Contributing** and choose **Lend my GPU**: when the coordinator has coding members turned on and your GPU is not on its own fast network, it gives your GPU this job instead of a share of the big model. The sidebar card, the status bar ("Coding member") and a one-time notice tell you which job you got. If you would rather not answer other people's requests, choose **Lend my CPU only**, or turn off **`dlne.worker.allowCodingMember`** (the extension writes `CODING_MEMBER_ALLOWED=false` into the engine's `config/config.txt` for you). The coding job uses the memory that is free when it starts (it steps aside when you game); set **`dlne.worker.maxRamGb`** to limit it. (That setting is also the limit of the shared-model job, which needs little, and its default of 4 GB does not apply to the coding job; the extension passes the coding limit as `--coding-max-ram`, `-1` meaning none.)

**Lend both.** The third button, **Lend both** (`dlne.worker.device` = `"both"`), lends the GPU and the CPU at the same time: the GPU does the job the swarm gives it, and the CPU also powers the DLNE phone app with the RAM you choose (you are asked on the first start; `dlne.worker.alsoCpuRamGb` holds the amount, at least 8 GB and never more than half your RAM; `dlne.worker.maxRamGb` stays the limit of the GPU's jobs). Your processor's threads are shared between the two jobs (`MAX_CPU_THREADS` is the total), the phone-app model loads before the coding model does, and when you game both step aside. The sidebar card, the status bar (`[Coding member 8GB + Phone app]`) and `@dlne /contribute` show both jobs while both run. On a machine without a usable GPU "both" is the same as "cpu".

- **You need** a GPU with **8 GB of memory or more**, and enough free system memory for the rest of the model: about **22 GB of GPU memory plus free system memory together** for the first version (for example an 8 GB GPU with 14 GB of RAM free). DLNE runs the best version of the model your machine can hold whole, using the GPU first and system memory for the rest.
- **Your machine is timed** when it joins, on reading a prompt and writing an answer. A machine under the speed floor is not sent coding work.
- **The more memory you have, the better the version you run.** About 22 GB runs the first version of Qwen3.6-35B-A3B (IQ3_S); higher-quality versions of the same model need roughly 26 GB (IQ4_XS), 30 GB (Q4_K_M) and 37 GB (Q6_K), and the larger ones also need a stronger GPU to be fast enough. Open, MIT-licensed heavyweight models are planned from 192 GB and are not available yet.
- **Your limits count.** The amounts you set with `dlne.worker.maxVramGb` and `dlne.worker.maxRamGb` are the most the member may use.
- **The model server runs at low priority**, so your computer stays usable, and the member steps aside while your own load (a game, a render) needs the machine.
- **Off until the coordinator turns it on.** Coding members are switched on by the coordinator's operator. Until then, coding requests are answered as before.

What a member can and cannot see of the requests it answers is under [Privacy](#privacy-what-to-know), and in full in [docs/privacy.md](https://github.com/RGlabs84/DistributedLLMEngine/blob/main/docs/privacy.md).

### No GPU? Your computer powers the DLNE phone app

A contributor without a usable GPU does not join the coding pipeline. Its worker joins the **CPU pool**, which answers the DLNE phone app (the [chat page](https://dlne.duckdns.org/chat) and the Android app). Each member runs a whole model on its CPU, and the server picks the model from how much RAM you lend:

| RAM you lend | Model your computer runs |
|---|---|
| 8 GB | Qwen3 4B, the fast overflow model |
| 16 GB | Qwen3.6 35B-A3B, the main phone model |
| 24 GB | Qwen3.6 35B-A3B at full quality, once 2 volunteers can host it |

In return you get the coding agent **2nd in line** (GPU contributors are 1st), with **300 chats in any 5 hours**. The model server runs at low priority on half your cores (2 to 8 threads), so your computer stays usable, and while you game the phone model drops to the lowest priority and runs lighter until you're done.

When you start, DLNE offers an amount to suit your machine, and never more than half your RAM:

| Your RAM | Offered by default | Also offered |
|---|---|---|
| 48 GB or more | 24 GB | 16 GB, 8 GB |
| 32 to 47 GB | 16 GB | 8 GB |
| 24 to 31 GB | 12 GB (runs the 8 GB model, with room to spare) | 8 GB |
| 16 to 23 GB | Half your RAM, 8 to 11 GB | 8 GB (when the default is higher) |
| Under 16 GB | Nothing: half your RAM is under the 8 GB minimum | |

To lend a different amount, set `dlne.worker.maxRamGb` yourself.

**An integrated GPU does not count as a GPU contributor**, except Intel Arc 140V / 140T (Lunar Lake, Arrow Lake) and AMD Strix Halo (Radeon 8060S / 8050S / 8040S, i.e. Ryzen AI Max). A machine whose only GPU is any other integrated GPU contributes its CPU instead, and DLNE tells you so.

A CPU-only machine needs, if it's fast enough not to slow everyone else down:

- at least **8 GB of RAM** lent to DLNE (`dlne.worker.maxRamGb`; you're asked how much when you start),
- **4 physical cores**,
- on Intel/AMD, **AVX2 and FMA** (Intel from 2013, any AMD Ryzen),
- **30 GB/s of memory bandwidth**, measured in a second or two when you join. Good dual-channel DDR4 just makes it; DDR5 and LPDDR5 pass easily.

A CPU's speed here is set by how fast it can read memory, and one slow machine would hold up every answer it takes part in, so a machine below any of these is told exactly what it's missing instead of joining. To lend only your CPU even though you have a GPU, set `dlne.worker.device` to `cpu`.

---

## Getting started

1. Install **DLNE Coding Assistant** from the Extensions view.
2. Open the **DLNE Agent** view in the Activity Bar, and start chatting. The status bar shows the swarm's state and your remaining free chats.
3. When you want to contribute, click **Start Contributing**. The first time, DLNE offers to install its compute engine:
   - It needs **Python 3.10 or newer**. The engine goes into `%LOCALAPPDATA%\DLNE\runtime` (Windows) or `~/.dlne/runtime`, with its own virtual environment.
   - **The engine runs on your machine, so it is only installed if it carries the maintainer's signature and matches it byte for byte.** Anything unsigned, altered, or older than what you have is refused, and your current engine is kept. The GPU engine (llama.cpp's `ggml-rpc-server`, an official upstream build pinned per release) is checked the same way, and updated with it.
   - Each step logs to the **DLNE Compute Worker** output channel. If one fails (no Python, `pip` offline), you are told which, with a **Show Log** button.

The agent's slash commands: `/code`, `/fix`, `/status`, `/model`, `/contribute`, `/clear`.

---

## Privacy: what to know

This section is about the coding agent in VS Code. The full version, with what each party can and cannot see, is [docs/privacy.md](https://github.com/RGlabs84/DistributedLLMEngine/blob/main/docs/privacy.md).

- **Your prompts, and the code the agent reads, go to the coordinator.** The public coordinator is run by the DLNE maintainer, so treat it like any hosted AI service. When coding members are on, a request may instead be answered by a volunteer's machine (below).
- **The connection to the public swarm is encrypted (HTTPS)**, so nobody between you and the coordinator can read it. For anything sensitive, run your own coordinator (below).
- When the model is split across several contributors' GPUs, those machines process the model's intermediate numbers for your request, not your text.

**Who can answer your requests: `dlne.privacy`**

**A coding member has to read your request to run the model, so it sees the code it is sent.** This setting decides which requests can go to which members.

DLNE looks at your workspace's git remote. If it is a **public repository** (GitHub, GitLab or Codeberg), any coding member can answer. **Anything else**, and anything DLNE cannot check, is answered only by the coordinator's own engine or by machines the coordinator's operator has listed as trusted. The operator lists a machine by its identity key, not by a name it claims, so a member cannot claim to be trusted.

**A public repository counts as public even for changes you have not pushed.** If you are working on something unpublished in a clone of a public repository, set `dlne.privacy` to `trustedOnly`. A repository's own settings can only narrow `dlne.privacy`, never widen it (only your own user settings can), and DLNE does not run in a workspace you have not trusted. While coding members are off (the default), the extension makes no repository lookup, and nothing about your repository leaves your machine beyond the request itself.

| `dlne.privacy` | Who may answer |
|---|---|
| `auto` (default) | Public repositories: any member. Everything else: trusted machines only. |
| `trustedOnly` | Trusted machines only, even for a public repository. |
| `anyMember` | Any member, for every workspace. Choose this only if you are happy for any volunteer's machine to see that work. |

**Secrets are removed in your editor.** Before a request leaves VS Code, API keys, tokens, private keys and passwords in common formats are replaced with `[REDACTED]`. These files are never sent: files your `.gitignore` ignores, anything in a `.dlneignore` file in your workspace, `.env*`, `*.pem`, `*.key`, `id_rsa*`, `.npmrc`, `.pypirc` and cloud credential files. This applies to every request, including those to the coordinator. It catches common formats, not every secret, so keep real secrets out of prompts.

**Requests and answers are encrypted on the way to and from a coding member.** Your editor encrypts the request and decrypts the reply. The coordinator relays bytes it cannot open, and the tunnel and the internet between it and the member carry nothing readable. That protects the trip, not the machine that answers: the member itself has to read the request. The coordinator is the operator's own machine and is trusted: it hands out the member's public key. A request answered by the coordinator's own engine goes over HTTPS instead and is not sealed.

**Members are not told who you are.** The coordinator passes on only the request: no IP address, no API key, no account. What the request itself contains (file paths, names in code or comments) can still say who you are.

**No-trace members and canaries.** Members run their model server with settings that keep prompts out of logs and off disk. Now and then the coordinator sends a member a normal-looking request holding a made-up link, built to look like any real request (where a file would hold a key it shows `[REDACTED]`, as yours do). If the link is ever opened, that member is flagged and gets no more work.

> **The honest limit: a coding member sees the code it is sent.** What protects you from members is the privacy mode (private repositories go only to trusted machines unless you opt in), the secrets removed in your editor, the no-trace settings and anonymity, and the canaries that are there to catch a member that snoops. A volunteer who changes DLNE's software could still read or keep what it receives, and none of this can promise otherwise. For code you cannot afford to expose, keep `dlne.privacy` on `auto` or `trustedOnly` (`trustedOnly` for unpublished work in a clone of a public repository), or run your own coordinator.

**If you contribute:**

- As a GPU contributor, your machine runs DLNE's worker and `ggml-rpc-server`, and accepts connections from the coordinator on port 50052.
- As a coding member, your machine decrypts and answers coding requests, so it sees their text (after redaction), but not who sent them. It never logs request or reply text. Its model server listens on your machine only, and the coordinator reaches it through a tunnel your worker opens.
- As a CPU contributor, your machine answers phone-app requests, so it sees the text of those questions and answers (the phone app tells its users this) and any search results for them. Its model server listens on your machine only, and the coordinator reaches it through a tunnel your worker opens. The phone chat is a separate path: none of the above (redaction, encryption, trusted routing) is promised for it.

---

## Which model?

The coding agent runs on GPU contributors' machines: one model at a time, chosen by the coordinator from the GPU memory available. Today that is **Qwen3.6-35B-A3B** (a UD-IQ3_S quant) on two 8 GB AMD GPUs, with a 65,536-token context: 38.5 tokens/s for one user and about 27 each for two, measured on the public swarm. Bigger models unlock automatically as more GPUs join. `/status` shows what is running now, and the sidebar shows it under the chat box. There is nothing to pick: VS Code's Chat model picker lists a single entry, **DLNE Swarm (auto)**. When coding members are on, each member runs its own copy of the best version of the model its machine can hold whole (see [Be a coding member](#8-gb-of-gpu-memory-or-more-be-a-coding-member)). The phone app runs a different, smaller model on CPU contributors' machines (see above).

---

## Your own private coordinator

Everything is open source, so you can run the whole thing yourself:

```bash
git clone https://github.com/RGlabs84/DistributedLLMEngine.git
cd DistributedLLMEngine
pip install -r requirements.txt
python dlne_server.py start --host 0.0.0.0 --port 11435 --hybrid   # coordinator (+ a local worker)
python dlne_server.py apikey create --name "my-laptop"              # optional: prints a dlne_sk_... key
python dlne_server.py worker --coordinator 192.168.1.50:11435 --network-key "your_cluster_secret"
```

Then in VS Code settings:

```json
{
  "dlne.serverUrl": "http://192.168.1.50:11435",
  "dlne.apiKey": "dlne_sk_your_key_here",
  "dlne.worker.networkKey": "your_cluster_secret",
  "dlne.worker.allowUnsignedEngine": true
}
```

`allowUnsignedEngine` is needed only because your coordinator does not publish releases signed with the maintainer's key. Never turn it on for the public swarm.

---

## Settings

The settings that decide where your code or keys go (`dlne.serverUrl`, `dlne.apiKey`, `dlne.autoApproveReads`, `dlne.autoUpdate`, and every `dlne.worker.*` setting) are read only from your own user settings: a workspace's or folder's `.vscode/settings.json` cannot change them, so a cloned repository cannot point DLNE at another server or turn on auto-approval. A folder can only narrow `dlne.privacy`, never widen it.

| Setting | Default | What it does |
|---|---|---|
| `dlne.serverUrl` | `https://dlne.duckdns.org` | The coordinator: the public swarm (over HTTPS), or your own. |
| `dlne.apiKey` | `""` | Optional API key. Leave blank to use the free trial or your contribution. |
| `dlne.privacy` | `auto` | Who may answer your coding requests: `auto` (any coding member for a public repository, trusted machines only for everything else), `trustedOnly` (use this for unpublished work in a clone of a public repository), or `anyMember` (any member, for every workspace). A repository's own settings can only narrow it, never widen it. See [Privacy](#privacy-what-to-know). |
| `dlne.worker.networkKey` | public swarm key | The swarm's shared worker key. Change it only for a private coordinator. |
| `dlne.worker.device` | `auto` | What to contribute: `auto` (a GPU if there is one that counts, else the CPU), `gpu`, or `cpu` (power the phone app and keep the GPU for yourself). |
| `dlne.worker.allowCodingMember` | `true` | Let the swarm give your GPU the coding-member job (answering other people's coding requests). Turn it off and your GPU only joins the shared pool. |
| `dlne.worker.allowedGpus` | `""` | GPU indices to lend (e.g. `0,1`). Blank lends all detected GPUs. |
| `dlne.worker.maxVramGb` | `-1` | Most VRAM to lend, in GB (`-1`: detect). |
| `dlne.worker.maxRamGb` | `4.0` | Most system RAM the worker may use, in GB. A CPU-only machine needs at least 8; the more it lends, the bigger the phone-app model it runs (16 GB: Qwen3.6). You're offered an amount when you start. |
| `dlne.worker.autoStart` | `false` | Resume contributing when VS Code opens (set for you when you start contributing). |
| `dlne.worker.pythonPath` | `python3` | Python used for the engine. |
| `dlne.enableInlineCompletion` | `true` | Ghost-text autocomplete. |
| `dlne.inlineCompletionDelay` | `150` | Delay after typing before asking for a completion, in ms. |
| `dlne.chatModel` / `dlne.fimModel` | `""` | Leave blank: the swarm serves the model it has active. Name a model file only for a private coordinator you run. |
| `dlne.autoApproveReads` | `true` | Let the agent read, list and search inside your workspace without asking. Edits (which you can allow for a whole session), shell commands (always one at a time), and anything outside the workspace always ask. |
| `dlne.autoUpdate` | `true` | Keep this extension and the engine on the latest signed release, and restart the worker when the coordinator upgrades. |
| `dlne.worker.allowUnsignedEngine` | `false` | Install the engine from a coordinator that publishes no signed release. Only for a private coordinator you run. A signed release that fails verification is never installed, whatever this says. |

---

## Support DLNE

Two ways to keep it free:

- **Donate compute at home**: click **Start Contributing** in the DLNE sidebar (see [How it stays free](#how-it-stays-free)).
- **Buy a coffee**: [buymeacoffee.com/wubarrk](https://buymeacoffee.com/wubarrk) helps pay for the coordinator's power and hardware.

---

## Links

- **Website**: [live.ravenirongames.com/#dlne](https://live.ravenirongames.com/#dlne)
- **Discord**: [discord.gg/RNxvxWn8cw](https://discord.gg/RNxvxWn8cw)
- **Source**: [RGlabs84/DistributedLLMEngine](https://github.com/RGlabs84/DistributedLLMEngine)
- **Issues**: [GitHub Issues](https://github.com/RGlabs84/DistributedLLMEngine/issues)
- **License**: MIT
