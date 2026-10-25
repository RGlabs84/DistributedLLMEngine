# Change Log

All notable changes to the **DLNE Frontier Coding Assistant** extension will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

