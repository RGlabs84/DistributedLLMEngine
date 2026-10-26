# DLNE Coding Assistant

[![Open VSX Version](https://img.shields.io/open-vsx/v/dlne/dlne-code-assistant?color=blue&label=Open%20VSX)](https://open-vsx.org/extension/dlne/dlne-code-assistant)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**A free AI coding agent and autocomplete for VS Code, running on GPUs that its users share.**

No account, no API key, no subscription. Install it, open the DLNE sidebar, and ask it to write or fix code. It reads the files in your workspace, and asks before it changes a file, runs a command, or looks outside your workspace.

- **Agent chat**: in the DLNE sidebar, or as `@dlne` in VS Code's Chat view.
- **Ghost-text autocomplete**: suggestions inline as you type.
- **Fix problems**: `DLNE: Fix Current File Problems` sends the file's errors and warnings to the agent.
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

### No GPU? Your computer powers the DLNE phone app

A contributor without a usable GPU does not join the coding pipeline. Its worker joins the **CPU pool**, which answers the DLNE phone app (the [chat page](https://dlne.duckdns.org/chat) and the Android app). Each member runs a whole model on its CPU, and the server picks the model from how much RAM you lend:

| RAM you lend | Model your computer runs |
|---|---|
| 8 GB | Qwen3 4B, the fast overflow model |
| 16 GB | Qwen3.6 35B-A3B, the main phone model |
| 24 GB | Qwen3.6 35B-A3B at full quality, once 2 volunteers can host it |

In return you get the coding agent **2nd in line** (GPU contributors are 1st), with **300 chats in any 5 hours**. The model server runs at low priority on half your cores (2 to 8 threads), so your computer stays usable.

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

- **Your prompts, and the code the agent reads, go to the coordinator.** The public coordinator is run by the DLNE maintainer, so treat it like any hosted AI service. Do not send secrets.
- **The connection to the public swarm is encrypted (HTTPS)**, so nobody between you and the coordinator can read it. For anything sensitive, run your own coordinator (below).
- When the model is split across several contributors' GPUs, those machines process the model's intermediate numbers for your request, not your text.
- As a GPU contributor, your machine runs DLNE's worker and `ggml-rpc-server`, and accepts connections from the coordinator on port 50052.
- As a CPU contributor, your machine answers phone-app requests, so it sees the text of those questions and answers (the phone app tells its users this) and any search results for them. Its model server listens on your machine only, and the coordinator reaches it through a tunnel your worker opens.

---

## Which model?

The coding agent runs on GPU contributors' machines: one model at a time, chosen by the coordinator from the GPU memory available. Today that is **Qwen3.6-35B-A3B** (a UD-IQ3_S quant) on two 8 GB AMD GPUs, with a 65,536-token context: 38.5 tokens/s for one user and about 27 each for two, measured on the public swarm. Bigger models unlock automatically as more GPUs join. `/status` shows what is running now, and the sidebar shows it under the chat box. There is nothing to pick: VS Code's Chat model picker lists a single entry, **DLNE Swarm (auto)**. The phone app runs a different, smaller model on CPU contributors' machines (see above).

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

| Setting | Default | What it does |
|---|---|---|
| `dlne.serverUrl` | `https://dlne.duckdns.org` | The coordinator: the public swarm (over HTTPS), or your own. |
| `dlne.apiKey` | `""` | Optional API key. Leave blank to use the free trial or your contribution. |
| `dlne.worker.networkKey` | public swarm key | The swarm's shared worker key. Change it only for a private coordinator. |
| `dlne.worker.device` | `auto` | What to contribute: `auto` (a GPU if there is one that counts, else the CPU), `gpu`, or `cpu` (power the phone app and keep the GPU for yourself). |
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
