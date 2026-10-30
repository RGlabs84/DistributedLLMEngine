# DLNE 2.7.0 / extension 1.6.0 / DLNE Donate 1.1.0 — the contract

**Approved by the owner, 2026-10-30**, with three changes folded in: unlimited turns ON by default, "Not now" asks
again after 3 days, and 2.6.6 (FLUX.2 klein pictures, never released on its own) ships inside 2.7.0. The builders
build to this file; a change to it is the owner's call.

## Why

- **CUDA** (measured on portoBOX's RTX 4070 Laptop, 2026-10-30, Qwen3.6 35B-A3B IQ4_XS with 28 layers' experts in
  RAM, llama.cpp b11838 official builds, warm, 3 rounds): Vulkan 27.9 tok/s writing / 322 tok/s reading 4k; CUDA 13.4
  38.6 / 600; CUDA 12.8 39.0 / 599. CUDA is +40% writing and +85% reading; 12.8 and 13.4 are equal on a modern card.
  The owner's decision: **CUDA 13 only**. Older cards (Maxwell, Pascal, Volta) stay on Vulkan; they lack the VRAM for
  this model anyway.
- **ROCm** was measured on TheWall's BC-160s the same day (TheRock gfx101X): +12% writing, +30% with two at once, but
  half the prompt-reading speed. The owner's decision: **TheWall stays on Vulkan.** ROCm is not part of 2.7.
- **The DLNE Agent panel** (the sidebar chat, "the brain panel") gets the controls people reach for, and two
  annoyances are fixed.
- **API-key holders**: unlimited agent turns (their choice), and first place in every queue.

## 1. Engine flavours: `vulkan` and `cuda`

- **One version, two flavours.** The version stays one number (2.7.0) everywhere a version is compared: the heartbeat's
  "same version" rule, the release manifest, the update check. The flavour is build metadata, shown as `2.7.0+cuda` /
  `2.7.0+vulkan` in the status, the tooltips, the panel and the tray, and never compared. Vulkan and CUDA machines join
  the same swarm: both are llama.cpp b11838, so the RPC protocol is the same and a CUDA laptop can shard into TheWall's
  Vulkan engine.
- **The CUDA build is llama.cpp's official CUDA 13.4 build of the engine's own version** (Linux x64:
  `llama-<tag>-bin-ubuntu-cuda-13.4-x64.tar.gz` + `cudart-…-13.4-x64.tar.gz`; Windows x64: `llama-<tag>-bin-win-cuda-13.4-x64.zip`
  + `cudart-llama-bin-win-cuda-13.4-x64.zip`). `scripts/package_llama.py` packages them like the Vulkan ones (server,
  rpc-server, the CUDA runtime libraries next to them), and the release manifest signs them per platform and flavour,
  with size and SHA-256, as it does today. About 600 MB a platform against 35 MB for Vulkan.
- **Who may use CUDA:** an NVIDIA GPU of compute capability 7.5 or newer (Turing: RTX 20 / GTX 16 and later) with a
  driver new enough for CUDA 13: **driver 580 or later, on Linux and on Windows** (`engine.release.CUDA_MIN_DRIVER`).
  Source: NVIDIA's CUDA Toolkit 13.4 release notes, "CUDA Driver": Table 3, minor version compatibility, "13.x | >= 580 |
  N/A" (existing CUDA 13.x applications run on drivers 580 and later, no upper bound). The notes give no separate Linux and
  Windows number for 13.4 (the per-OS table stops at 12.9), and say CUDA 13.4's *new* features need R615 or later, which
  a stock llama.cpp build does not use; so 580 is the whole rule. The worker reads both from `nvidia-smi` (name, `compute_cap`,
  `driver_version`). Anything else is Vulkan, as today.
- **Consent before any download.** On a machine that may use CUDA and has not chosen yet, the extension asks once:
  "Your NVIDIA GPU can run DLNE about 30-50% faster with the CUDA engine. It is a one-time download of about 600 MB.
  Use CUDA?" — **Use CUDA** / **Not now** (asked again after 3 days) / **Keep Vulkan** (never asked again). Nothing is
  downloaded before **Use CUDA**. The DLNE Donate tray asks the same in its own window.
- **The choice is a setting:** extension `dlne.worker.engine` = `auto` (default: Vulkan until consent, then CUDA where
  allowed) | `vulkan` | `cuda`; the tray keeps the same three values in its settings; a coordinator host has
  `ENGINE_FLAVOR=auto|vulkan|cuda` in config.txt (default `vulkan`; TheWall stays `vulkan`).
- **Falling back.** If the CUDA engine fails its start-up check (the binary will not start, no CUDA device, an
  out-of-date driver), the worker starts the Vulkan engine instead, says so in its log and in the panel ("CUDA could
  not start: …; running on Vulkan"), and does not retry CUDA until the engine setting changes or the version changes.
- **Downloads stay per flavour:** a machine keeps only the flavour it runs (switching removes the other after the new
  one starts), so a laptop never holds 600 MB it does not use.

## 2. The DLNE Agent panel (extension 1.6.0)

- **Speed vs Quality:** a two-way switch near the top of the panel, writing the existing `dlne.ownGpu` setting
  (`faster` = Speed, `smarter` = Quality). Shown only while this computer is a coding member (that is when it does
  anything); it stays in Settings too, and the two always show the same.
- **Graphics API:** **Auto / Vulkan / CUDA** in the panel, writing `dlne.worker.engine` (section 1). CUDA is shown
  greyed, with the reason, on a machine that may not use it. Choosing CUDA where it has not been downloaded asks the
  consent question first. The current flavour is shown next to it ("running on CUDA").
- **Terminal cards:** clicking the command line expands the card to its full output (and back); the card has
  **Copy command** and **Copy output** buttons. The collapsed card stays 140 px as today.
- **Copy from the chat:** every message has a **Copy** button (copies the message as plain text; for the agent's
  replies the markdown source). Selecting text and Ctrl+C / Cmd+C must work in the panel; this is checked in a real
  VS Code window before release, and if VS Code's own webview context menu is missing, the panel offers Copy on
  right-click over a selection.

## 3. API-key holders

- **Unlimited agent turns, toggle-able.** A new setting `dlne.agent.unlimitedTurns` (default **on**; also a switch in
  the panel, shown only to key holders). When on, and the coordinator confirms this caller is a key holder, the agent
  has no turn limit (today's hard maximum is 500). The loop check, Stop and the step counter still work. The
  coordinator confirms it with a new `GET /v1/me` → `{"tier": "key" | "host" | "gpu" | "cpu" | "trial" | "open"}`
  (the tier it already works out per request, `request.state.dlne_tier`; nothing secret is returned). With an old
  coordinator, or any tier but `key` / `host`, the setting has no effect and the panel says why.
- **First place in every queue.** Key holders already go first on the coordinator's own engine (`engine/priority.py`:
  `host`, `key`, `gpu` first; `open`, `cpu` second; `trial` third). 2.7 makes the same order hold in the picture queue
  and in the coding-member queue (waiting for a member slot), with tests. The panel shows key holders "Priority: first
  in line".

## 4. DLNE Donate 1.1.0

- The CUDA question, the engine setting (Auto / Vulkan / CUDA) and the "running on …" line in the tray's settings
  window; the flavour in the tray menu's version line.
- One tray build for both flavours: it downloads its engine at run time from the same manifest, like the extension.

## 4a. DLNE Donate updates itself (owner, 2026-10-30)

- The tray checks for a newer DLNE Donate at start and every 12 h, from the same place the extension's "Optional
  add-on" button already reads it (the site's Donate download manifest; read how trayAddon.ts finds it).
- **Notifier:** a newer version shows a tray notification and a line in its window: "DLNE Donate <new> is available"
  with **Update now** and **Later**.
- **Update now:** downloads the new build, checks its size and SHA-256 against the manifest, replaces the running
  app the safe way for each platform (AppImage: write beside, swap, restart; Windows: download, then hand over to a
  small updater step that swaps the exe after the tray exits and starts it again), stopping the worker gracefully
  first. A failed check deletes the download and keeps the old version.
- **Automatic:** a setting "Update automatically" (default **off**); when on, an available update installs at the
  next moment the worker is idle, with the same checks.

## 4b. VS Code first: the tray pauses while DLNE runs in VS Code (owner, 2026-10-30)

- Only one worker runs on a computer. Until now the extension stepped back when the tray was donating; from 2.7 it is
  the other way round: **while VS Code with DLNE is open, the tray pauses its worker**, and the extension's own worker
  (with the user's VS Code settings: engine flavour, Speed vs Quality, auto-start) is the one that runs.
- **Presence file:** the extension writes `~/.dlne/editor.json` (`{"pid": <extension host pid>, "updated": <epoch s>,
  "version": "1.6.0"}`, mode 0600) when it activates and every 30 s after, and deletes it when it deactivates. Several
  VS Code windows each refresh it; it counts as present while it is less than 90 s old and its pid is alive.
- **The tray** checks it every 10 s. Present: it stops its worker the way its own Stop does (the coordinator is told it
  is leaving; a running answer finishes), shows "Paused: DLNE in VS Code is using this computer" in its window and its
  tray tooltip, and does not start the worker while it stays present. Gone (stale, removed, or the pid dead): it resumes
  on its own if it was donating before the pause.
- **The extension** does not start its worker while the tray's worker is still leaving: it waits until the tray's
  worker is gone (up to the drain time the tray uses), then starts its own as its settings say (auto-start or the
  button). If the user starts nothing in VS Code, the computer simply donates nothing while VS Code is open: the panel
  says "DLNE Donate is paused while VS Code is open; Start here to donate".
- A tray from before 1.1.0 knows nothing of this: the extension keeps today's behaviour for it (it sees the tray's
  worker and steps back).

## 4c. Skills: the agent learns procedures (owner, 2026-10-30: "can this model learn skills like you do?", then "fold everything into 2.7")

Like Claude Code's skills: a skill is a folder with a `SKILL.md` (a name, a one-line description, step-by-step
instructions). Only the names and descriptions sit in the prompt; the agent loads a skill's instructions when a task
fits it, and writes new ones for procedures it worked out. Facts (`remember`) stay short truths; skills are procedures
("how to release this project", "how its tests run"). Nothing is trained: the model reads and writes files.

- **Where:** `<workspace root>/.dlne/skills/<name>/SKILL.md` (project) and `~/.dlne/skills/<name>/SKILL.md` (the user's,
  every project). `<name>` is `[a-z0-9][a-z0-9-]{0,47}`. The same privacy rules as the memory files (`.dlneignore`,
  the credential rules, no link out of the workspace or home folder; git-ignored is fine), and secrets are redacted on
  load and refused on save. The setting `dlne.agent.memory` covers skills too: `off` loads and saves none; `ask` asks
  before every save.
- **The file:** `---` / `name: <name>` / `description: <one line, at most 200 characters>` / `by: dlne` (only on a skill
  the agent wrote) / `---`, then the instructions in Markdown, at most 12,000 characters. A file that does not parse is
  skipped (the user can see it in the folder).
- **In the prompt:** a `## Skills` section at the end of the `<dlne_context>` block, read with it (once per
  conversation, contract section 6a): `- <name> (project|user): <description>`, the project's first, at most 50 lines and
  6,000 characters, then `… and <n> more (use_skill works for any)`. A project skill hides a user skill of the same name.
- **Two new tools** (the fixed tool list changes, so `engine/canary.py`, `tests/test_canary.py` and
  `scripts/cache_probe.py` change in the same commit, word for word):
  - `use_skill {name}` → the skill's instructions (with its folder path, so files beside SKILL.md can be read with
    `read_file`); read-only, never asks.
  - `save_skill {name, description, instructions, scope: "project"|"user"}` → writes the skill. Project skills are saved
    at once and shown with Undo, like project facts; user skills ask first, like user facts. Saving over an existing name
    is allowed only for a skill the agent wrote (`by: dlne`); the user's own skills are theirs. Empty `instructions`
    removes the agent's skill of that name. At most 100 skills per scope.
- **System prompt:** one added paragraph: use a listed skill (`use_skill`) when a task matches its description; after
  working out a procedure that is likely to be needed again, save it with `save_skill`, short and step by step.
- **Looking back at the end of a task** (docs/agent-memory.md 7.7) may call `save_skill` as well as `remember`, under
  the same streak rules.
- **The panel** shows a saved skill like a saved fact: `📘 Skill saved for this project: <name>` with Undo.
- Docs: a new section in docs/agent-memory.md; the README's agent section names skills.

## 4d. The agent sees pictures (owner, 2026-10-30: "our chat agent says it can't read pictures but the phone users can?")

The coordinator has read pictures in coding chats since 2.6.1 (OpenAI `image_url` parts with `data:` URIs, checked by
`engine/media.py`, TheWall's vision projector on the RX 580, `IMAGE_INPUT_PER_HOUR` per network, coding members
refuse them with `IMAGE_INPUT_UNAVAILABLE`). Only the extension never sends one. It does now:
- **The panel:** paste a picture (Ctrl/Cmd+V), drop one, or an attach button; PNG, JPEG, WebP or GIF (first frame), at
  most 4 a message and 4 MiB each, shown as thumbnails with ✕ before sending and in the message after. Sent as
  `image_url` parts in the user's message, the same shape the phone uses. Saved chats keep a placeholder
  (`[picture: <name>]`), not the bytes.
- **A tool, `view_image {path}`:** the agent looks at a picture file in the workspace (a screenshot, a diagram, an icon
  it made). The same privacy rules as `read_file`; the picture goes into the conversation as an `image_url` part in the
  user's side of the next request (tool results stay text), with the tool result `Showing <path> (<w>x<h>)`. The fixed
  tool list changes, so the canary and the cache probe change with it, word for word.
- **Only where a machine can see:** the agent offers pictures (the paste/attach controls and `view_image`) while the
  coordinator's status says `capabilities.vision`; a request that carries a picture goes to the coordinator's own engine,
  never sealed to a coding member. The Chat view's DLNE model says `imageInput: true` on the same condition.
- **Context:** a picture costs up to about 1,000 tokens. Pictures older than the last 3 user turns are replaced by
  `[picture: <name>, shown earlier]` when a request is built, and a compaction drops them all, so they never pile up.
- **Plain answers:** a refusal from the coordinator (budget spent, no projector, too big) is shown in plain words and the
  run goes on without the picture.
- Docs: docs/agent-memory.md sections 1 and 3; docs/image-generation.md says reading and making pictures are both TheWall's.

## 5. Out of scope

- ROCm, a CUDA 12 flavour, Windows on ARM CUDA.
- Picture making on CUDA machines (pictures stay TheWall's, on its RX 580).

## 6. Order of work, once approved

1. 2.6.6 (FLUX.2 klein pictures, branch `release/2.6.6`) is folded into 2.7.0: its changelog entry becomes part of
   2.7.0's, and TheWall gets klein with the 2.7.0 deploy (`IMAGE_GEN_ENABLED=true` then).
2. 2.7.0 / 1.6.0 / Donate 1.1.0 built in parallel parts (engine flavours + manifest; worker detection + fallback;
   extension panel + consent; key-holder turns + queues + `/v1/me`; tray), each with tests, then released with the
   owner's OK for each (stores, TheWall, the site's Donate download, public GitHub).

3. Also folded in (owner, 2026-10-30, "fold everything into 2.7"): sections 4c (skills), 4d (the agent sees pictures) and 6a (already built
   on `release/2.7.0`: e1994ab, dafcad6).

## 6a. Already built after approval: no more whole-conversation re-reads, a bigger memory

TheWall's log on 2026-10-30 showed 87k–92k-token conversations read again from the start, 5–8 minutes each, with the
owner's two editors open.
- **Coding lanes:** a coding chat goes back to the lane its conversation used last (`X-DLNE-Session`, remembered under
  its HMAC tag, as phone chats already were); one with no lane of its own takes the lane a chat used longest ago, not
  always lane 0.
- **The top of the prompt** (system prompt, tool list, `<dlne_context>`) is read once per conversation, and again at a
  compaction, a Clear or a load. A saved fact or skill no longer changes the prompt's first tokens mid-conversation.
- **Compaction** keeps 20% of the budget word for word (was 35%).
- **Memory:** each file (AGENTS.md, project, user) holds and loads up to 32,000 characters on every machine (owner:
  "32000 char saving for memory across the board"); the three together load at most 0.75 characters per token of
  context (24,576 at 32k, 48,096 at 64,128, all 96,000 from 128k). One fact may be 1,000 characters (was 500).
- **TheWall with the deploy** (the owner's planner layout, 2026-10-30): IQ4_XS `-ts 26,14`, `--n-cpu-moe 16`, 4 coding
  lanes × 64,128 at q8_0, 2 phone lanes × 8,192, `--cache-ram 8192`, RX 580 for the vision projector, klein pictures
  and its text encoder, 4 whole CPU cores. Voice (whisper) is not built and not part of 2.7.

## 7. Interfaces between the parts (exact; every builder codes to these)

- **Worker environment:** the extension and the tray start the worker with `DLNE_ENGINE_FLAVOR=vulkan|cuda` (already
  resolved: `auto` is turned into one of the two by whoever starts the worker, from the setting and the consent).
  Missing or anything else means `vulkan`.
- **Worker marker lines** (printed once at start, and again on change, like `DLNE_ROLE:` / `DLNE_DOCK:`):
  - `DLNE_CUDA: {"capable": true, "reason": "", "gpu": "NVIDIA GeForce RTX 4070 Laptop GPU", "compute_cap": "8.9", "driver": "615.78"}`
    — printed before any engine download; `capable` false with a plain-words `reason` ("no NVIDIA GPU", "this GPU is
    older than Turing (compute capability 6.1)", "driver 572.16 is older than CUDA 13 needs (580 or later)").
  - `DLNE_ENGINE: {"flavour": "cuda", "fallback": ""}` once the engine runs; `{"flavour": "vulkan", "fallback": "<why CUDA could not start>"}` after a fall-back.
- **Heartbeat / status:** the worker's profile gains `engine_flavour` (`vulkan` | `cuda`); `/v1/cluster/status` gains
  `engine_flavour` for the coordinator's own engine. Version strings everywhere stay plain `2.7.0`; the `+cuda` form is
  display only.
- **Downloader:** `engine.downloader.download_and_extract_engine(role, coordinator_url=None, flavour="vulkan")`; the
  release manifest carries the CUDA assets per platform as the packaging part defines them (the worker part only calls
  this function).
- **Coordinator:** `ENGINE_FLAVOR=auto|vulkan|cuda` in config.txt (default `vulkan`; `auto` = cuda when capable, the
  same rules as a worker); parsed in `engine/config.py`.
- **Extension, engine state** (`vscode-extension/src/worker/engineFlavour.ts`, a stub on the base branch; the
  extension-worker part fills it in, the panel part only calls it):
  `getEngineState(): EngineState` with `{ setting: 'auto'|'vulkan'|'cuda', running: 'vulkan'|'cuda'|null, capable: boolean|null, reason: string, gpu: string, fallback: string, consent: 'yes'|'never'|'later'|'unasked' }`,
  `setEngineSetting(value): Promise<void>` (asks consent first when it turns on CUDA), `onEngineStateChanged(listener): vscode.Disposable`.
- **Extension, key tier** (`vscode-extension/src/agent/keyTier.ts`, a stub on the base branch; the key-holder part
  fills it in, the panel part only calls it): `getKeyTier(): Promise<'key'|'host'|'gpu'|'cpu'|'trial'|'open'|null>`
  (null: unknown or an old coordinator), `unlimitedTurnsActive(): Promise<boolean>`, `onKeyTierChanged(listener): vscode.Disposable`.
- **Settings** (package.json; scope `application`): `dlne.worker.engine` (enum auto|vulkan|cuda, default auto) — the
  extension-worker part adds it; `dlne.agent.unlimitedTurns` (boolean, default true) — the key-holder part adds it.
  `dlne.ownGpu` exists.
- **Presence file:** `~/.dlne/editor.json` as section 4b says (the extension-worker part writes it; the tray part reads it).
- **Consent storage:** extension globalState `dlne.cudaConsent` = `'yes'` | `'never'` | `{ "later": <epoch ms when to ask again> }`;
  the tray keeps `engine` and `cuda_consent` in its settings file with the same meanings.
- **`GET /v1/me`** → `{"tier": "key"}` (one of host, key, gpu, cpu, trial, open), `Cache-Control: no-store`, the same
  authentication as every other request; nothing else in the answer.
