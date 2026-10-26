# Change Log

All notable changes to the **DLNE Coding Assistant** extension will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.3.1] - 2026-10-26

Pairs with DLNE coordinator **2.4.0** or later. The GPU perks wording below is coordinator 2.4.2's, where a GPU keeps its perks while the swarm uses it.

### Fixed
- **Start Contributing never starts a `dlne-worker` systemd service.** When a user service by that name existed, Start Contributing started it instead of the worker you chose, so "lend my CPU, 16 GB" ran whatever the service was set up as (an 8 GB GPU worker, on the machine where it was found). The extension now always starts its own worker, with your VS Code settings.
- **A worker started outside VS Code is described as one.** A worker the extension did not start (a service, a terminal, another editor) was shown as "8.0 GB via VULKAN (system service)", a fixed string, or described from this editor's settings. The sidebar, status bar, tooltip, quick pick and `@dlne /contribute` now say that a worker started outside VS Code is running with its own settings. Start Contributing says so instead of starting a second worker beside it. Stop Contributing stops it if it is the `dlne-worker` service; one started from a terminal or another editor is stopped there.
- **Stop Contributing stops only this install's engine.** An engine left behind by a killed worker was cleared with `killall -9 ggml-rpc-server` (Windows: `taskkill /F /IM ggml-rpc-server.exe`), which killed every `ggml-rpc-server` on the machine, other installs' and other programs' included. It now stops only the one at this install's own path.

### Changed
- **The GPU perks say when they apply.** The lend dialog, `@dlne /contribute` and the README now say what coordinator 2.4.2 does: a lent GPU gets unlimited coding, first in line, while the swarm uses it, and for its first 30 minutes while the swarm finds it a job. A GPU still without a job after that counts as a CPU contributor until it gets one.

---

## [1.3.0] - 2026-10-26

Pairs with DLNE coordinator **2.4.0** or later (coding members, docs/coding-members.md). With an older coordinator, or one with coding members off, requests go exactly as they did in 1.2.5.

### Added
- **Lend both.** `dlne.worker.device` gains `"both"`, and the lend dialog a third button, **Lend both**: the GPU does its job for the swarm and the CPU also powers the DLNE phone app, at the same time. The CPU half lends the RAM you choose in the usual offer (its own setting, `dlne.worker.alsoCpuRamGb`), the two jobs split the CPU cores between them, and both step aside when you game. A machine with no usable GPU treats "both" as "cpu"; one too small to lend the CPU as well lends the GPU alone and says so. The sidebar, status bar and `@dlne /contribute` show both jobs while both run. Needs coordinator 2.4.0.
- **`dlne.worker.allowCodingMember`** (default on, application scope): turn it off and the swarm never gives this GPU the coding-member job; the GPU only joins the shared pool. The extension writes it as `CODING_MEMBER_ALLOWED=true|false` into the engine's `config.txt` before the worker starts and whenever you change the setting, so a running worker follows without a restart.
- **Windows and macOS fixes for lending a GPU.** No console windows flash any more: the worker's `python.exe` and every probe (`tasklist` every 5 s, `nvidia-smi`, `vulkaninfo`, `taskkill`) start hidden. Unpacking the engine works for a user name with an apostrophe (O'Brien) or square brackets, and a Python found under a folder with a space is quoted. The Windows ARM engine is found as `ggml-rpc-server.exe` (it was re-downloaded on every start). A `DLNE_ROLE` or `DLNE_REFUSAL` line that arrives in two pieces, or with Windows line endings, is read once it is whole. The worker's output is UTF-8 whatever the console's code page is. Stopping on macOS and Linux gives the worker eight seconds to stop its coding model server (it was cut off after 0.8 s), and never kills a worker that was started in the meantime.
- **Requests flow through coding members.** When the coordinator reports `coding_members.enabled`, each chat, agent and autocomplete request asks `GET /v1/coding/route`; if a member is offered, the request body is sealed end-to-end for that member (X25519 + ChaCha20-Poly1305) and sent with `X-DLNE-Route`, and the answer is read back frame by frame. The coordinator and the network only ever carry ciphertext. An answer counts as complete only at its sealed end: a stream that is cut, a chunk that fails its integrity check, or a member that answers in the clear is reported as an error (or retried on another member before anything is shown), never passed off as a finished answer. An envelope is sent once: every retry asks for a route again and seals again. After two members fail, the coordinator's own engine answers in plain.
- **Sessions.** `X-DLNE-Session` carries a random id per agent conversation (a new one when you clear it) and one per editor session for autocomplete, so the coordinator can send a conversation back to the member that already read it.
- **Privacy mode on every coding request.** With members on, `X-DLNE-Privacy: open|trusted|any` (and `privacy=` on the route lookup) says how widely your code may travel, from the `dlne.privacy` setting and whether the repository is public. With members off, nothing is looked up or sent about your repository, and the privacy item stays out of the status bar.
- **Secrets never leave your editor.** Before anything is sent to the coordinator or to a coding member, API keys, tokens, JWTs, private keys, passwords in config files and code, passwords in connection strings, and DLNE's own `dlne_` keys (and `NETWORK_KEY` / `ADMIN_HASH` in `config.txt`) are replaced with `[REDACTED]`. It covers the files the agent reads, what a shell command prints, the prompt you type, earlier tool-call arguments, the messages VS Code's model picker builds, and autocomplete prompts; the agent keeps its real history. Ordinary code is left alone: hashes in lockfiles, UUIDs, base64 test data, `process.env.X`, `${PASSWORD}` and type names all come through untouched. A 1 MB file takes under 100 ms; an autocomplete prompt about 0.2 ms.
- **Private files are not read at all.** `.env*` (templates such as `.env.example` excepted), `*.pem`, `*.key`, `*.p12`, `id_rsa*`, `.npmrc`, `.pypirc`, `.netrc`, cloud, Docker and Kubernetes credentials, anything under `.git/` or `.ssh/`, files ignored by your `.gitignore` files, and files matched by a new **`.dlneignore`** (same syntax; a `!pattern` line lets the model see a git-ignored file). The agent is told "this file is excluded for privacy" instead of failing silently, autocomplete does not run in these files, and search and diagnostics skip them. If the redactor or the exclusion check fails, the text is withheld or the file counts as excluded.
- The agent refuses to write `[REDACTED]` over a file that holds secrets it could not see.
- **A busy swarm tells you where you stand.** When every coding slot is taken, the sidebar shows a banner ("All 2 coding slots are busy. You are #3 in line."), the status bar shows "#3 in line", and the `@dlne` chat shows a progress line, with how many are waiting and that lending a GPU skips the line. The coordinator can report the place in the stream or in a 503 (`dlne_queue`, with a ticket that keeps your place); a coordinator that says nothing is read through `coding_queue` in its status, and the place is shown as an estimate. A request that stays busy for 15 minutes ends with a message, and Stop works during the wait.
- **Lend your GPU, with a clear choice and explanation.** The first time you click to contribute a GPU, a dialog explains that the swarm chooses the job (a share of the one big model, or a coding member that answers other people's coding requests), lists the privacy facts for a coding member (anonymous, end-to-end encrypted in transit, the machine keeps no trace, private-repository code goes only to trusted machines, and honestly that the GPU has to read what it runs), and offers Lend my GPU or Lend my CPU only. It never appears when a worker resumes by itself.
- **See the job your machine got.** The sidebar card, the status bar tag ("Coding member"), the tooltip and `@dlne /contribute` show whether the coordinator made your GPU part of the pool or a coding member, and what that means. A coding member sees its trust id (to give the operator if the machine is yours and you want private repositories routed to it), with a Copy link, and is told once, in a toast, when its GPU becomes a coding member. The worker reports the job on a `DLNE_ROLE:` line (docs/coding-members.md section 10).
- `@dlne /status` shows the coding slots in use and waiting, and the coding members when there are any.

### Changed
- **Settings that decide where your code or keys go are read only from your own user settings.** `dlne.serverUrl`, `dlne.apiKey`, `dlne.autoApproveReads`, `dlne.autoUpdate` and every `dlne.worker.*` setting are now application-scoped (`dlne.worker.pythonPath` is machine-scoped): a value in a workspace's or folder's `.vscode/settings.json` is ignored, so a cloned repository cannot point DLNE at another server, hand it a key or turn on auto-approval. **If you had set `dlne.serverUrl` (or another of these) in a workspace's settings, that override no longer applies; move it to your user settings.** `dlne.privacy` can be narrowed by a folder (`auto` to `trustedOnly`) but only your user setting can widen it.
- **Faster ghost text.** The prompt's first line now moves once per 8 lines typed instead of once per line, so the engine's cache keeps matching; with coordinator 2.4.0 a warm-up request goes ahead of each completion (measured on the public swarm: median 1.57 s to 1.0 s per completion).
- **The sidebar shows text as text.** Everything the model, a file or a command produced is put on the page as plain text, and the page runs under a content-security-policy that allows only its own script (by nonce), its own styles and images, and nothing else.
- **No autocomplete in files outside every open folder** (a file you opened from elsewhere is not sent to the swarm for completion).
- **A GPU's coding job is not held to `dlne.worker.maxRamGb`'s default of 4 GB.** That default suits the GPU's other job (a share of the shared model); a whole coding model needs more, so the coding job uses the memory that is free when it starts unless you set `dlne.worker.maxRamGb` yourself. The lend dialog says so.
- The extension looks for its engine, Python and config only where it or you installed them, and never reads the network key from the open workspace.
- The waiting messages are true: "Evaluating prompt across distributed Vulkan GPU cluster" and "Distributing activation tensors across 3 GPUs (24 GB VRAM pool)" were fixed strings; the wait now says either where you are in line or that the swarm is working on the first words (with the seconds waited).
- The agent is told that secrets appear as `[REDACTED]` and that some files cannot be read.

---

## [1.2.5] - 2026-10-26

Pairs with DLNE coordinator **2.3.0** or later. Server 2.3.5 adds the model's name to the status, which the sidebar shows; earlier servers show the model's file name.

### Changed
- **One model entry, "DLNE Swarm (auto)".** VS Code's Chat model picker listed eight models (Qwen 2.5 Coder 14B and 32B, Qwen3 Coder 30B, DeepSeek-R1 32B and 70B, Llama 3.3 70B, Qwen 2.5 72B) that the swarm does not run, with the 14B as the default, and choosing one wrote it into `dlne.chatModel`. There is now a single entry, and picking it changes nothing: the coordinator serves whichever model its ladder has active (today Qwen3.6-35B-A3B on the GPU pool).
- **The sidebar's model dropdown is a read-only line** showing "DLNE Swarm (auto)" and the live model's name, taken from the status the status bar already polls. The dropdown offered the same eight models and asked the coordinator to swap its model to the one you picked.
- **Requests no longer name a model**, unless you set `dlne.chatModel` or `dlne.fimModel` yourself (for a private coordinator). A request without a model is served by the coordinator's active model; naming one, even the active one from a status answer a few seconds old, can make the coordinator swap models. The Qwen2.5-Coder 14B file name that was the fallback when the status could not be read is gone.
- A `dlne.chatModel` or `dlne.fimModel` still holding one of those eight names, saved by the old picker, is read as empty.
- **"Frontier Coding Agent / Assistant" is now "DLNE Code Assistant"** in the sidebar title and welcome line, the chat participant's name, the update notice and the Loading message.
- `/model` says what is running now instead of listing tiers (it said the 14B was active, "with speculative decoding"), and shows "not reported" or "?" rather than made-up figures when the server cannot be reached.

### Removed
- The `selectModel` sidebar action and the client's `swapModel` call to `/v1/cluster/swap`.

---

## [1.2.4] - 2026-10-25

Pairs with DLNE coordinator **2.3.0** or later.

### Changed
- **One DLNE icon everywhere.** The only DLNE mark is now the cyan-and-pink brain with "DLNE" under it (`media/dlne_logo.png`), which the Marketplace listing and the chat participant already used. The line-art "swarm" icons are gone (`dlne_icon.svg`, `brain.svg`, `brain_logo.svg`, and the duplicate `brain.png` and `dlne_sidebar.png`), and so is the brain emoji, which had stood in for the logo in the sidebar, the status bar, the chat participant and notifications.
- **The Activity Bar icon is that same brain**, traced from the logo as a single-colour silhouette (`media/dlne_activitybar.svg`), because VS Code draws Activity Bar icons in one colour taken from your theme.
- The sidebar shows the logo image beside "Compute Contribution" and the agent's welcome line. The status bar says "DLNE" in plain text, with a glyph only for contributing or locked.
- **The public swarm's address is `https://dlne.duckdns.org`.** A name that follows the server's changing home connection, in place of an address with the IP in it. Settings that still hold either earlier public address (`http://73.215.216.195:11435` or `https://73-215-216-195.sslip.io`) are read as the new one; both keep working.

---

## [1.2.3] - 2026-10-25

Pairs with DLNE coordinator **2.3.0**.

### Changed
- **A computer without a GPU now powers the DLNE phone app.** Since coordinator 2.3.0 a CPU contributor no longer joins the coding pipeline: its worker joins the CPU pool, which answers the phone app (the chat page at `/chat`, and the Android app). Each member runs a whole model on its CPU, and the server picks it from the RAM you lend: 8 GB runs Qwen3 4B (the fast overflow model), 16 GB runs Qwen3.6 35B-A3B (the main phone model), 24 GB runs it at full quality once 2 volunteers can host it. The modal, notifications, status bar, sidebar and chat commands all say so, instead of describing a GPU.
- **You are offered the RAM that suits your machine**, not a flat 12 GB: 24 GB from 48 GB of RAM, 16 GB from 32 GB, 12 GB from 16 GB, otherwise the 8 GB minimum, never more than half your RAM. The modal explains what each amount runs, and offers the other ladder steps your machine can afford. A machine under 16 GB (half its RAM is under the 8 GB minimum) is told so, instead of being asked to lend more than it can spare. `dlne.worker.maxRamGb` still sets any amount by hand. The RAM figure allows for the OS reporting a little under what the machine was sold with (a 16 GB laptop reads about 15.5), so common machines land on the right step.
- **What a CPU contributor gets:** the coding agent 2nd in line (GPU contributors are 1st and unlimited), with **300 chats in any 5 hours** (was 100). The free trial is **100 chats** (was 200), still counted once per internet connection. Both limits are enforced by the server.
- **The model server runs at low priority on half your cores** (2 to 8 threads), so your computer stays usable. The modal and README say so.
- **An integrated GPU does not count as a GPU contributor**, except Intel Arc 140V / 140T (Lunar Lake, Arrow Lake) and AMD Strix Halo (Radeon 8060S / 8050S / 8040S, i.e. Ryzen AI Max). A machine whose only GPU is any other integrated GPU (Intel UHD or Iris, Arc Graphics on Meteor Lake, Radeon 780M, Vega) contributes its CPU, is told why, and its worker is told to leave the integrated GPU alone. The same rule is enforced by the worker and the server.

### Added
- **Use DLNE on your phone.** A link at the bottom of the sidebar, an item in the cluster menu, and the command `DLNE: Use DLNE on Your Phone` open the phone chat page on your coordinator.

### Fixed
- A machine without a GPU was shown as a GPU contributor in the sidebar ("CPU GPU: CPU Compute Node", "Inference unlocked!"), and the engine installer said contributing would "unlock unlimited frontier coding models". Neither is true of a CPU contributor.
- The RAM offer could be more than the machine has: a flat 12 GB was offered to an 8 GB laptop.

---

## [1.2.2] - 2026-10-25

Pairs with DLNE coordinator **2.2.0**.

### Changed
- **Contributors go first.** When the swarm is busy, GPU contributors' requests are served first, CPU contributors' second, and free-trial users' after them.
- **CPU contributors get 100 coding chats in any 5 hours.** At the limit, the agent says exactly when the next one frees up. GPU contributors stay unlimited.
- **The 200 free chats are counted once per internet connection**, on the server. Reinstalling, clearing settings or switching accounts no longer resets them.

### Fixed
- A refused chat showed the server's raw response. It now shows the server's own sentence (for example "The next one frees up in 42 minutes"), and only a spent free trial gets the "start contributing" guide.

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

