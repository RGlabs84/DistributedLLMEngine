# Change Log

All notable changes to the **DLNE Coding Assistant** extension will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.2.1] - 2026-10-25

Pairs with DLNE coordinator **2.1.3**.

### Added
- **Contribute your CPU.** New setting `dlne.worker.device`: `auto` (the GPU if there is one, otherwise the CPU), `gpu`, or `cpu` (lend only the CPU and keep the GPU for yourself). A CPU-only machine needs 8 GB of RAM lent, 4 physical cores, AVX2 and FMA on Intel/AMD, and 30 GB/s of memory bandwidth, which the engine measures in a second or two when you join.
- **More machines can contribute:** Windows and Linux on ARM64 (GPU through Vulkan, or the CPU), and Intel Macs (CPU only: llama.cpp's Intel Mac build has no GPU support), alongside Windows/Linux x64 and Apple Silicon. These builds are new and the least tested.

### Fixed (Intel Macs)
- An Intel Mac with an AMD GPU could be reported as a Metal GPU node, which the engine cannot use. Only Apple Silicon is treated as Metal now.

### Fixed
- A machine without a GPU was refused by the swarm with nothing on screen: the extension lent 4 GB of RAM by default, and a CPU-only machine needs 8. Starting now offers to lend 12 GB first, and if the swarm still refuses the machine, you are told exactly what it lacks (for example "memory bandwidth of at least 30 GB/s (measured 21.5 GB/s)").
- Choosing CPU-only (`allowedGpus: none`) made the engine look for a file no release ships, so it never started.

## [1.2.0] - 2026-10-25

Pairs with DLNE coordinator **2.1.2**.

### Added
- **Encrypted connection.** The public swarm is now reached over HTTPS (`https://73-215-216-195.sslip.io`), so prompts and code no longer cross the internet in plain text. If your settings still hold the old `http://73.215.216.195:11435` address, it is upgraded to HTTPS for you. A coordinator of your own is used exactly as you set it.
- **Linux and macOS (Apple Silicon) can contribute compute.** Until now only Windows could download the GPU engine, so contributing failed elsewhere. Each platform now gets an official llama.cpp `ggml-rpc-server` build (b11838), signed into the release and verified before it is installed.
- **Workers follow llama.cpp upgrades.** When a release names a new GPU engine build, it is installed on the next start. The coordinator only uses workers on its own llama.cpp RPC protocol, so an upgrade can never crash the cluster's engine.

### Security
- **The agent now asks before it acts.** Until now every tool call ran the moment the model asked for it: file writes, edits, any shell command, and reads anywhere on disk (your SSH keys included). The model's replies arrive over the network, and the agent reads files that may contain instructions, so: reads, listings and searches inside your workspace run without asking (`dlne.autoApproveReads`); edits ask (with "Allow edits for this session"); every shell command asks, showing the exact command; anything outside the workspace always asks. A refusal is reported to the model so it can adjust. `/clear` withdraws session approval.
- **The Windows GPU engine binaries are verified** against the signed release before they are installed, and are updated when the release names new ones. The Windows setup step no longer re-downloads the engine source without checking it.
- The engine and its Python are never taken from the open workspace, and the retired, unverified standalone `DLNE-Node` executables are no longer run.
- The agent's instructions no longer contain the maintainer's own folders or tell the model to go anywhere on your disk; it works in your workspace.

### Fixed
- Contributing to a coordinator with an `https://` address sent the worker to plain HTTP on port 80, where it was redirected and could not register. The worker now gets the full address.

### Faster
- **Answers come faster.** The coordinator now uses its small draft model to guess ahead (measured 45 vs 39 tokens per second on the public swarm).
- **Autocomplete**: smaller prompts (50 lines before the cursor, 10 after), the `dlne.inlineCompletionDelay` setting is actually used (it was forced to at least 450 ms), a new keystroke cancels a stale request instead of being ignored, completions inside a line stop at the line end, and connections are reused.
- **Agent**: the conversation is no longer rewritten on every turn, so the server reuses what it has already read instead of re-reading the whole conversation; and a chat turn no longer makes an extra status request first.

### Changed
- "Run Combined Suite Setup (Tray Daemon + Extension)" is now **Set Up Compute Engine**.
- The cluster menu says when it cannot reach the cluster instead of showing made-up numbers.
- The extension no longer changes VS Code's own `extensions.autoUpdate` settings.
- The README describes what DLNE actually does today, including what is sent where.

---

## [1.1.1] - 2026-10-25

Identical to 1.1.0. Open VSX accepted the 1.1.0 upload but never activated it, so it was re-released under a new version number. See 1.1.0 for the changes.

---

## [1.1.0] - 2026-10-25

Pairs with DLNE coordinator **2.0.0**, the first signed, reproducible release.

### Security
- **The engine is verified before it runs.** The extension installs a Python engine from the coordinator and runs it on your machine, so it now installs one only if its release manifest carries the maintainer's Ed25519 signature, the downloaded archive has exactly the signed size and SHA-256, and the release is not older than the engine you already have (so a genuine but outdated release cannot be replayed to downgrade you). A failed check keeps your existing engine, deletes the bad download and tells you why.
- **Removed the unverified fallbacks** (the coordinator's Windows bundle and a hard-coded Google Drive link) that a failed verified download could otherwise have routed around.
- New setting `dlne.worker.allowUnsignedEngine` (off by default) for a private coordinator that does not sign its releases. It never excuses a signature that is present but wrong.

### Fixed
- **Setup no longer lies.** Creating the virtual environment and `pip install` now run with live output in the "DLNE Compute Worker" log, realistic timeouts, a real error message and a **Show Log** button; success is announced only after the dependencies import. Python 3.10+ is actually enforced.
- **Machines without `unzip`** crashed while unpacking the engine with no message (GNU `tar` cannot read zips). The installer now falls back to Python's `zipfile`, then `tar -xf`, and fails cleanly if nothing works.
- **After a coordinator upgrade your worker comes back by itself.** A refused worker exits with a dedicated code; the extension re-fetches the verified engine and restarts it (at most twice in ten minutes, then it says plainly what is wrong).
- The engine is no longer re-downloaded on every worker start when nothing changed.

### Changed
- **Bigger free trial:** 200 free chats (was 50), autocomplete no longer spends them, the status bar shows `[Free trial: N chats left]`, and running out gives a friendly prompt to start contributing instead of a raw error.

---

## [1.0.32] - 2026-10-25

### Fixed
- **Honest first-run setup**: the compute engine installer no longer reports success when it failed. Virtual-environment creation and `pip install` now stream their output to the "DLNE Compute Worker" log, use realistic timeouts (2 min / 10 min), stop with the actual error and a **Show Log** button, and verify the install by importing the dependencies. Python 3.10+ is now actually enforced, and a missing `python3-venv` gets a specific hint.

### Changed
- **Bigger free trial**: new users get 200 free chat requests (was 50). Ghost-text autocomplete no longer spends the trial. The status bar shows `[Free trial: N chats left]`, and running out shows a friendly prompt to start contributing compute.

---

## [1.0.22] - 2026-10-24

### Fixed
- **Graceful Fallback on Model Swap OOM / Startup Crashes**: If an oversized model (e.g. 32B) fails to allocate in the current cluster VRAM pool and returns 500/503 during startup, the client automatically synchronizes with the active cluster model (e.g. 14B) and retries seamlessly instead of failing the user prompt.
- **Auto-Track Active Cluster Model**: Set `dlne.chatModel` default to `""` so extensions automatically track the active cluster model without requesting an incompatible model swap.

---

## [1.0.21] - 2026-10-24

### Fixed
- **Prioritize Active Worker Token Over Stale Settings API Key**: The extension now prioritizes the live local worker token whenever the user is actively contributing compute, and automatically retries with the worker token if a configured static `dlne.apiKey` is rejected with HTTP 403.

---

## [1.0.20] - 2026-10-24

### Fixed
- **Persistent Worker Across Window Reloads**: Removed `stopWorker()` from extension `dispose()`. Reloading the window, switching workspaces, or updating the extension no longer terminates background worker compute processes or systemd user services.
- **Auto-Start & Auto-Resume Worker (`dlne.worker.autoStart: true`)**: Automatically resumes local worker compute contribution on VS Code activation so users never have to manually click `/contribute` or "re-establish worker" after restarts.
- **Self-Healing Non-Contrib 403 Flow**: When an inference request encounters a 403, the client now checks worker state, automatically starts the local worker in the background, reports progress in the chat stream, and seamlessly retries instead of immediately failing with a "DLNE Compute Required" error.
- **Worker Token Preservation Across Restarts**: Removed `atexit` token wiping in `engine/worker.py` so brief worker restarts or reload cycles retain the last valid token on disk during the coordinator's 90-second grace window.

---

## [1.0.19] - 2026-10-24

### Fixed
- **"Error connecting to DLNE cluster: read ECONNRESET"**: Chat requests now open a fresh socket instead of reusing a pooled keep-alive connection the coordinator had already closed, and transient network errors (`ECONNRESET`, `EPIPE`, `ECONNREFUSED`, `ETIMEDOUT`) are retried up to 4 times with backoff.

---

## [1.0.18] - 2026-10-24

### Fixed
- **False "DLNE Compute Required" 403 While Contributing**: Fixed a race where the worker wrote its rotated ephemeral token to disk before the Coordinator had registered it, causing chat requests to be rejected for a few seconds after each rotation or worker restart.
  - Worker now writes the new token to disk only after the Coordinator accepts the heartbeat carrying it.
  - Coordinator keeps the previous token valid for a 90 s grace window after rotation.
  - Extension transparently re-reads the token and retries (up to 5x / 15 s) on a 403 while a local worker token exists.

---

## [1.0.17] - 2026-10-24

### Added
- **Built-in Open VSX Self-Updater & Reload Prompt**: On startup and every 30 minutes, the extension directly checks `https://open-vsx.org/api/dlne/dlne-code-assistant` for newer releases, automatically downloads and installs the latest `.vsix` when `dlne.autoUpdate` is enabled (`true` by default), and displays a 1-click **"Reload Window"** prompt (even if originally installed via a local `.vsix`).

---

## [1.0.16] - 2026-10-24

### Fixed
- **Automatic Windows `ggml-rpc-server.exe` Binary Provisioning**: Added automatic download and extraction of the pre-compiled Vulkan & CPU `ggml-rpc-server.exe` binaries (`/v1/download/rpc-bin`, ~15 MB) into `%LOCALAPPDATA%\DLNE\runtime\bin\` whenever missing, resolving `FATAL: Could not locate a compatible bundled binary for 'ggml-rpc-server'` on fresh Windows installations.
- **Discrete GPU Detection & Isolation on Hybrid Systems (e.g., Ryzen iGPU + NVIDIA RTX 5070)**:
  - Prioritized `nvidia-smi` discrete GPU detection and `PHYSICAL_DEVICE_TYPE_DISCRETE_GPU` in `vulkaninfo` so hybrid laptops/desktops accurately display and contribute their discrete GPU (e.g., RTX 5070 12 GB) instead of the integrated GPU (e.g., Radeon 880M 0.5 GB).
  - Automatically sets `GGML_VK_VISIBLE_DEVICES` to the discrete Vulkan device index when both integrated and discrete GPUs are present so `ggml-rpc-server` never binds the 0.5 GB iGPU.
- **Auto-Update Enabled by Default (`dlne.autoUpdate: true`)**: Automatically keeps both the VS Code extension (`extensions.autoUpdate`) and the local DLNE compute runtime synced with the latest coordinator release on startup.
- **Redacted Network Key in Output Logs**: Masked `--network-key` in the extension output channel logs.

---

## [1.0.15] - 2026-10-24

### Fixed
- **Resolved 404 Error on Engine Download**: Replaced private GitHub repository clone/download URLs with direct coordinator-served distribution endpoints (`/v1/download/source` and `/v1/download/windows`).
- **HTTP / HTTPS Protocol Compatibility**: Fixed protocol dispatch in extension file downloader so `http://` coordinator endpoints stream reliably without throwing HTTPS-only protocol errors.
- **Standalone `DLNE-Node.exe` Support**: Added native execution of standalone pre-compiled Windows executable bundles without requiring Python, Git, or pip environments.
- **Automatic Multi-CDN Fallback**: If coordinator is busy, engine downloads smoothly fall back to Google Drive distribution mirrors.

---

## [1.0.14] - 2026-10-24

### Added
- **Zero-Friction In-Extension Auto-Bootstrap**:
  - Automatically installs and configures the DLNE compute engine runtime upon 1-click user permission.
  - Automatic fallback download over HTTPS when `git` is not installed on the system.
  - Auto-detection of system Python (Windows `py -3`, `python`, `winget` integration, Linux `python3`).
  - Isolated virtual environment creation (`.venv`) with automated lightweight dependency installation.
  - Default runtime installed cleanly into `%LOCALAPPDATA%\DLNE\runtime` (Windows) or `~/.dlne/runtime` (Linux/macOS).
- **Public Swarm Discovery**:
  - Cluster status (`/v1/cluster/status`), tier recommendations (`/v1/cluster/tiers`), and model catalogues (`/v1/models`, `/v1/models.json`) are now fully public with no authentication required.
  - All users installing the extension immediately see live swarm health, available pooled VRAM, and active models in the status bar without encountering HTTP 403 blocks.
- **Ultra-Long Context Support**:
  - Full support for `Qwen2.5-14B-Instruct-1M` with up to 256,000+ token context windows deployed on the public swarm.
- **Pre-Configured Public Swarm Network Key**:
  - Extension now defaults `dlne.worker.networkKey` out of the box to the public cluster secret, allowing seamless instant connection.

### Changed
- **Suite Setup Flow**: Replaced the terminal command script execution with a native in-extension setup modal and notification progress bar.
- **Documentation**: Completely overhauled `README.md` with detailed walkthroughs for both Public Zero-Config Swarm usage and Private Self-Hosted clusters, hardware requirements, and FAQ.
- **Repository Metadata**: Corrected repository URLs in extension manifest to `https://github.com/RGlabs84/DistributedLLMEngine`.

### Fixed
- Fixed HTTP 403 "NON-CONTRIB BAN" middleware interception that prevented new users from reading cluster status.
- Fixed Windows terminal failure when executing non-existent `python3` command.
- Fixed 404 clone failure caused by outdated git repository reference.
- Fixed Activity Bar icon rasterization by adopting clean vector SVG (`media/brain.svg`).

---

## [1.0.13] - 2026-10-24

### Added
- Native vector SVG brain icon (`media/brain.svg`) for crisp rendering in the VS Code Activity Bar across all DPI scalings.
- Cluster model loading telemetry in the status bar displaying live transfer speeds (MB/s), RPC sync progress, and estimated arrival times (ETA).

### Changed
- Polished status bar tooltips to display detailed node breakdown and active lease status.

---

## [1.0.12] - 2026-10-23

### Added
- Initial public release on Open VSX Marketplace.
- Fill-In-The-Middle (FIM) inline ghost-text completion provider with sub-150ms latency.
- Dedicated Frontier Reasoning Agent sidebar webview with deep `<think>` chain-of-thought support.
- Native VS Code Chat Participant (`@dlne`) integration in the primary chat panel (`Ctrl+Alt+I`).
- Quick-pick cluster model selector and hot-swap controls.

---

## [1.0.10] - 2026-10-23

### Added
- Initial pre-release of the DLNE extension client.
- Integration with Distributed LLM Engine Coordinator API.
- Support for autonomous file editing (`write_file`, `edit_file_diff`) and compiler diagnostic self-healing (`dlne.fixDiagnostics`).

