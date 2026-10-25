# DLNE Coding Assistant

[![Open VSX Version](https://img.shields.io/open-vsx/v/dlne/dlne-code-assistant?color=blue&label=Open%20VSX)](https://open-vsx.org/extension/dlne/dlne-code-assistant)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**A free AI coding agent and autocomplete for VS Code, running on GPUs that its users share.**

No account, no API key, no subscription. Install it, open the DLNE sidebar, and ask it to write or fix code. It reads the files in your workspace, and asks before it changes a file, runs a command, or looks outside your workspace.

- **Agent chat**: in the DLNE sidebar, or as `@dlne` in VS Code's Chat view.
- **Ghost-text autocomplete**: suggestions inline as you type.
- **Fix problems**: `DLNE: Fix Current File Problems` sends the file's errors and warnings to the agent.

---

## How it stays free

DLNE runs on a **swarm**: a coordinator plus the GPUs of the people who use it.

1. **Try it**: your first **200 chats are free**, with nothing to set up. Autocomplete does not use them up. The status bar shows how many are left.
2. **Keep using it**: lend some of your GPU (or CPU) to the swarm with one click (**Start Contributing** in the sidebar). While you contribute, chat and autocomplete are unlimited.

You choose how much to lend (`dlne.worker.maxVramGb`, `dlne.worker.maxRamGb`, `dlne.worker.allowedGpus`), and you can stop at any time.

> **Contributing works on Windows and Linux (x64 and ARM64) and on macOS (Apple Silicon and Intel).** GPUs are used through Vulkan on Windows and Linux and Metal on Apple Silicon. Intel Macs contribute their CPU (llama.cpp has no GPU support for them). The ARM64 and Intel Mac builds are the newest and the least tested.

**No GPU? You can contribute your CPU**, if it's fast enough not to slow everyone else down. A CPU-only machine needs:

- at least **8 GB of RAM** lent to DLNE (`dlne.worker.maxRamGb`; you're offered 12 GB when you start),
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
- As a contributor, your machine runs DLNE's worker and `ggml-rpc-server`, and accepts connections from the coordinator on port 50052.

---

## Which model?

The swarm serves one model at a time, chosen by the coordinator from the GPU memory available. Today that is usually **Qwen 2.5 Coder 14B**. Larger models (32B and up) become possible as more GPUs join. `/status` shows what is running now.

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
| `dlne.serverUrl` | `https://73-215-216-195.sslip.io` | The coordinator: the public swarm (over HTTPS), or your own. |
| `dlne.apiKey` | `""` | Optional API key. Leave blank to use the free trial or your contribution. |
| `dlne.worker.networkKey` | public swarm key | The swarm's shared worker key. Change it only for a private coordinator. |
| `dlne.worker.device` | `auto` | What to contribute: `auto` (GPU if there is one, else CPU), `gpu`, or `cpu`. |
| `dlne.worker.allowedGpus` | `""` | GPU indices to lend (e.g. `0,1`). Blank lends all detected GPUs. |
| `dlne.worker.maxVramGb` | `-1` | Most VRAM to lend, in GB (`-1`: detect). |
| `dlne.worker.maxRamGb` | `4.0` | Most system RAM the worker may use, in GB. A CPU-only machine needs at least 8. |
| `dlne.worker.autoStart` | `false` | Resume contributing when VS Code opens (set for you when you start contributing). |
| `dlne.worker.pythonPath` | `python3` | Python used for the engine. |
| `dlne.enableInlineCompletion` | `true` | Ghost-text autocomplete. |
| `dlne.inlineCompletionDelay` | `150` | Delay after typing before asking for a completion, in ms. |
| `dlne.chatModel` / `dlne.fimModel` | `""` | Ask for a specific model (blank: whatever the swarm runs). |
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

- **Source**: [RGlabs84/DistributedLLMEngine](https://github.com/RGlabs84/DistributedLLMEngine)
- **Issues**: [GitHub Issues](https://github.com/RGlabs84/DistributedLLMEngine/issues)
- **License**: MIT
