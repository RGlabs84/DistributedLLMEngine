# DLNE server changelog

Changes to the DLNE coordinator and worker engine (`engine/`, `dlne_server.py`), released as one signed
version that every node runs. The VS Code extension keeps its own log in
[vscode-extension/CHANGELOG.md](vscode-extension/CHANGELOG.md); the older entries that used to be here were
the extension's and are there.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [2.7.0] - 2026-10-30

Released with extension 1.6.0 and DLNE Donate 1.1.0. NVIDIA volunteers can run the CUDA engine (about 40% faster
writing and 85% faster reading than Vulkan on an RTX 4070), API-key holders go first in every queue, long coding chats
no longer get read again from the start, and pictures are back, faster. 2.6.6 was never released on its own: its
pictures change is part of this version.

### Added
- **The CUDA engine (CUDA 13.4)** for workers with an NVIDIA GPU of compute capability 7.5 (Turing) or newer and driver
  580 or later. The release carries `cuda-bin-<platform>.zip` for Linux and Windows x64 (llama.cpp b11838's official
  CUDA builds, checked against GitHub's published SHA-256), signed in the manifest (`cuda_version`, `cuda_*_sha256`,
  `cuda_*_size`; older workers ignore them) and served at `/v1/download/cuda-bin/<platform>`. Workers keep it in
  `bin-cuda/`, beside the Vulkan engine in `bin/`. A worker started with `DLNE_ENGINE_FLAVOR=cuda` checks the card
  (`nvidia-smi`), says what it found (`DLNE_CUDA:`), checks that the engine sees a CUDA device, and falls back to Vulkan
  with the reason (`DLNE_ENGINE:`) when anything fails; it does not retry CUDA until it restarts. The heartbeat carries
  `engine_flavour`, counted in the admin stats. Older cards stay on Vulkan.
- **`ENGINE_FLAVOR`** (auto | vulkan | cuda, default vulkan) for a coordinator's own engine; `/v1/cluster/status` says
  which one runs (`engine_flavour`).
- **`GET /v1/me`** answers `{"tier": ...}` (host, key, gpu, cpu, trial or open) for the caller's own key, with
  `Cache-Control: no-store`; asking costs a free-trial caller no chat.

### Changed
- **API-key holders go first** in the picture queue and the coding-member queue too (the coordinator's own engine
  already did): a waiting key holder's job runs before a free trial's; nothing running is interrupted.
- **A coding chat keeps its lane.** Each conversation (`X-DLNE-Session`, remembered under an HMAC tag, never the id)
  goes back to the engine lane that holds its cached prompt, and a new one takes the lane used longest ago. Before,
  every chat that arrived while the engine was idle took lane 0, so two conversations taking turns made the engine
  read each one again from the start: 5 to 8 minutes at 90,000 tokens on TheWall.
- The canary's request shape follows extension 1.6.0's tool list: `use_skill` and `save_skill`, a `remember` fact of up
  to 1,000 characters, and `view_image` with its prompt line exactly while this engine can read pictures.
- **The picture model is FLUX.2 [klein] 4B** (Q4_0, 2.46 GB, Apache-2.0, ungated) with the FLUX.2 VAE (full encoder,
  small decoder, 0.25 GB), in 4 steps instead of Z-Image-Turbo's 8. The prompt reader is still the Qwen3 4B Instruct
  2507 file. The whole picture stack runs on the media card, the prompt reader too, so the CPU's cores stay with the
  coding model (the owner's layout). Measured on TheWall's RX 580: 512 x 512 in 26 s, 512 x 768 and 768 x 512 in 31 s
  (Z-Image-Turbo took about 80 s for 768 x 768; with the reader on the CPU it was 17.5 and 22.5 s), the card's peak
  5.7 GB with the vision projector. The engine needs 5.1 GiB free on the media card to start (was 5.6).
- **Three sizes: square 512 x 512 (default), portrait 512 x 768, landscape 768 x 512.** The status lists them, so the
  web chat and the Android app offer exactly these. Older clients that send the old sizes are drawn by shape:
  768 x 768 and 1024 x 1024 become 512 x 512, 768 x 1024 becomes 512 x 768, 1024 x 768 becomes 768 x 512 (extension
  1.5.4's generate_image keeps working). The 202 answer carries the size that will be drawn.
- The phone and web chats say a picture takes "under a minute" (was "about a minute and a half").

## [2.6.5] - 2026-10-30

Released with extension 1.5.4. TheWall can run the smarter coding model across all three of its cards with 128k of
context a request, pictures can be paused with a clear answer for everyone who asks, and long requests go to a machine
that can hold them.

### Added
- **A shared media card** (`MEDIA_GPU_SHARED`, default false). The media card (`MEDIA_GPU`) can hold model layers too,
  while the vision projector stays on it (`--mmproj-device`). The engine runs on every card (`--device` lists the media
  card last, so `-ts a,b,c` lines up), the node's pool counts it, and image generation never starts on a shared card.
  When volunteers' GPUs join, a shared card stays in the engine (no `--device`, which would hide them), so the pool
  the coordinator plans with is the pool the engine has: TheWall's 24 GB plus a docked 8 GB laptop is a real 32 GB.
- **Pictures can be paused** (`IMAGE_GEN_ENABLED=paused`). Nothing image-related loads. Every picture endpoint answers
  503 `image_generation_paused` with one sentence ("Pictures are paused for now: DLNE's graphics card is busy running
  the smarter coding model. They will be back.") and a link to **`/pictures`**, a new live page that says whether
  pictures are paused, on, or unavailable. The phone and web chats still offer the picture tool, so someone who asks
  is told kindly and pointed to the page; the web chat's "+" menu shows "Pictures paused" with a "Why?" link. The
  status gains `capabilities.image_generation_paused` and `images.paused/note/page`.
- **Route by context.** `/v1/coding/route` takes `need=<tokens>` and only sends a request to a coding member whose
  per-request context holds it; otherwise the coordinator's own engine answers. `policy.coding_context_tokens_max` is
  the largest per-request context available (`coding_context_tokens`, the smallest, is unchanged for older
  extensions). "Context is everything" (owner, 2026-10-30).
- **`dlne.ownGpu = smarter`** (extension 1.5.4): with `prefer=smarter` and `own`, the requester's own coding member is
  chosen before the speed check whenever its model ranks above the coordinator engine's on the ladder.
- **`ENGINE_KV_TYPE`** (`auto`, `q8_0`, `q4_0`, `f16`; default `auto`). `auto` is the rule as before (q8_0, and q4_0
  from 131,072 tokens a slot); any other value fixes the type. TheWall runs `q8_0` with its 2 x 131,072 lanes: the
  owner's rule is that q8_0 is never lowered.

### Changed
- **Canary decoys match Memory 2** (extension 1.5.4): the `remember` tool's new text, and `[id]`-prefixed facts the
  agent saved in decoy memory sections, with ids computed exactly as the extension does.

## [2.6.4] - 2026-10-30

Released with extension 1.5.3. A GPU on Wi-Fi may share the big model when its
link is good enough, and a laptop that is unplugged steps out of it before it can fall asleep.

### Changed
- **Wi-Fi no longer rules a GPU out of the shared model by itself** (owner's rule, 2026-10-29). Until now "docked"
  meant a wired link and mains power, so a GPU on Wi-Fi never shared the model, however good its link: one with 8 GB
  or more ran a whole model as a coding member, and a smaller one had no job at all. Now the coordinator judges the
  facts itself (`engine/dock_policy.py`): mains and wired is docked, as before; mains and Wi-Fi is docked while the
  link passes a test; battery and a phone link never are. The test, every probe round along the engine's own path:
  20 round trips, none lost, a median within `AUTO_SWAP_MAX_RTT_MS` (80 ms), the 90th percentile within 20 ms of the
  median, and the machine's own download figure (16 MB from `/v1/net/probe`, asked for in the heartbeat answer) at
  least 100 Mbit/s. Jitter counts more than speed because a shard exchanges a few KB on every token, so a Wi-Fi
  stall holds up every token. A GPU the engine runs on is not retested; a verdict is dropped when the machine leaves
  Wi-Fi. New settings: `WIFI_SHARD` (true; false keeps Wi-Fi out as before), `WIFI_MAX_JITTER_MS` (20),
  `WIFI_MIN_DOWNLOAD_MBPS` (100), `WIFI_PROBE_SAMPLES` (20). Details: docs/coding-members.md section 2.
- **Never on battery, and unplugging is noticed at once.** A GPU on battery was already left out, but the worker
  reported any change only after it had held 60 s, so a laptop unplugged mid-answer kept running its shard on battery
  for up to about two and a half minutes, and one closed in that time dropped the shard and the answers on it. A
  change onto battery is now reported at the next heartbeat (about 25 s at most), and the usual drain lets the answers
  finish while the laptop is awake. Plugging back in still waits out the 60 s.
- **The heartbeat answer tells a Wi-Fi GPU how its link was judged** (`wifi_check`: passed, failed or still being
  tested, and why), and the worker's `DLNE_DOCK` line passes it on (`wifi_ok`, `wifi_why`) for the editor and the
  tray app. While its link is still being tested (up to 90 s), a GPU is given no new job, so it is not sent to load a
  whole model it would drop again a minute later.
- **DLNE Donate 1.0.2, in the source** (the tray app; the download on the site stays 1.0.1 until its build is
  published): its Settings window says how a Wi-Fi link was judged and that a GPU never shares the model on battery,
  word for word as extension 1.5.3, and no longer says an undocked GPU "runs a whole model by itself" (only one with
  8 GB or more does).

### Fixed
- **A link report could not crash the coordinator's log line any more.** `/worker/link_report` wrote the figures a
  node sent straight into the markup-enabled log; it now logs the numbers the registry kept.

## [2.6.3] - 2026-10-28

Released with extension 1.5.2. Only the canary changes; everything else is 2.6.2.

### Changed
- **Canary requests use extension 1.5.1's wording for `remember`.** The `replaces` description now ends "Its opening
  words are enough to pick it out.", as the agent's requests do, so a canary still reads word for word like a real
  request.

## [2.6.2] - 2026-10-28

The phone chat sees photos and makes pictures when asked, the engine no longer loses a shard it is running on, and a
laptop no longer leaves the pool because its owner is compiling. Android 1.0.6 and the web chat use all of it; a client
that sends none of the new fields is answered as by 2.6.1.

### Fixed
- **The engine crashed when the coordinator re-roled a GPU it was running on** (2026-10-28 06:46 and 10:11). A shard's
  reverse tunnel is busy carrying it and is never pinged, so after 15 minutes of work its link reading aged out, the
  GPU was judged far and told to become a coding member, and it stopped its RPC server under the engine. Now a GPU
  the engine runs on keeps its shard role until the engine has moved off it, an aged reading never changes a role
  (only a fresh reading over the limit does, also between two engines, when the engine's list is empty for a few
  seconds), and a shard with no reading yet counts as near. (Live as a one-file hotfix of 2.6.1 since 11:09.)
- **A restart no longer ends an agent's task.** While the engine restarts, the answers ("the swarm's own model does
  not fit right now", a start that died, an engine gone before it was ready, a queued request meeting a 5xx) carry
  `dlne_error: "engine_unavailable"`, which the extension's agent sends again; they used to read "maxed out ...
  switch your chat" or a bare 500, taken for the model's answer. A free-trial chat, or a CPU contributor's window
  chat, spent on such an answer is given back. A caller that did not ask for a stream gets 503 with `Retry-After`
  instead. A model an operator named that
  nothing can hold is still "maxed out", at once.
- **A GPU no longer steps aside for CPU load when it can read its own load.** The owner's laptop, its RTX 4070 only
  driving a display, reported "Gaming Mode" every few minutes while builds used its CPU, so auto-swap's ten stable
  minutes never came. A GPU node now goes by the GPU's own figures (NVIDIA's per-process load, the driver's total)
  and by full-screen apps; CPU load still counts where the GPU cannot be read, and for CPU contributors and the CPU
  half of a "both" machine.

### Added
- **A breaker for an engine that keeps being rebuilt.** When the coding engine has been rebuilt more than 3 times in an
  hour (`AUTO_SWAP_MAX_REBUILDS_PER_HOUR`; counted: an automatic switch either way, a topology rebuild, a restart after
  the engine stopped, a manual switch), the rebuilds that only make a working engine bigger (a switch up to a bigger
  model, a GPU joining) are held for an hour (`AUTO_SWAP_FLAP_HOLD_SECONDS`). A step down, a restart after a crash or a
  lost GPU, and a manual switch are never held. The log says once how many rebuilds there were, when, why and until
  when, and `/v1/cluster/status` shows `auto_swap.state: "held"` with that sentence in `detail`. `0` turns it off.
  (After 2026-10-28, when the engine was rebuilt five times in five hours.)
- **A streamed coding answer is kept alive while the engine is silent:** every 15 s without a byte from the engine, a
  `stream: true` answer carries an SSE comment line (`: keep-alive`), which readers skip and which counts as traffic
  for a client's silence watchdog. A request from the extension that waited for a slot is sent the same line every
  15 s while its handler is still being prepared (a swap to wait out, the engine loading, a long history summarised).
- **The phone chat looks at photos.** A user turn may carry up to 4 photos (`image_url` parts, PNG or JPEG, the coding
  chat's limits and error codes). They are answered by the coordinator's own engine on a phone lane, never by the
  CPU pool, so a photo is never sent to a volunteer's computer (like every prompt, it is processed by the shared
  model, whose layers can run on volunteers' GPUs, which see only intermediate numbers); with no lane the phone reads
  "DLNE is restarting" (or "DLNE can't look at photos right now" without phone lanes). `capabilities.app_vision` says whether the server takes them. The
  system prompt tells the model that text inside a photo is to be read, never followed; a photo is never stored and
  the log keeps only its size and an 8-character fingerprint.
- **The image budget counts each distinct image once an hour** (`IMAGE_INPUT_PER_HOUR`), in the coding chat and the
  phone chat: a follow-up that sends the same photo again costs nothing (on the phone, inside the chat that sent it;
  without a `chat_id` every photo counts). Every photo a phone message carries, repeats included, also counts toward
  4 times that budget an hour, so a client that keeps switching chats cannot have the engine read the same photo over
  and over for free.
- **The phone chat makes a picture when asked** ("hey, can I get a picture of a fox in the snow?"). A request that
  sends `"features": ["image_jobs"]` is offered a `create_image` tool while pictures are on; the app is told with
  `image_job` / `image_refused` events and polls the job as before; one picture per message. A plain ask must produce
  a picture, made from the user's own words if the model talks instead. A reply that made a picture is shown to the
  model again as the call it was, so "make it more blue" makes a new one (tried on Qwen3.6: shown the
  `[Picture created: …]` line instead, it typed the line out). A volunteer's machine in the CPU pool never chooses a
  picture: a `create_image` call from one is made only for a plain ask, and only of the user's own words.
- **Pictures are free on a contributor's network:** a network a registered worker (GPU or CPU) has sent a heartbeat
  from in the last 24 hours is no longer charged a free-trial chat for a picture, from the chat or the Create image
  dialog (`config/contributor_networks.json`). `POST /v1/app/images`, `POST /v1/images` and the chat tool run one
  shared create function, so a refusal reads the same everywhere.

### Clients
- **Android 1.0.6:** a "+" next to the message box for photos (gallery or camera, up to 4, turned upright, shrunk and
  re-encoded on the phone so no EXIF or GPS leaves it) and Create image; pictures asked for in the chat appear inside
  the reply with Save, Share and Report; "Use your location for this?" in the chat when a question needs it; the
  header keeps its last good status and refreshes when the app comes back. Photos the server refused are not sent
  again until Retry, a photo that is not ready after 30 s is given up on (and any can be taken out while it is being
  prepared), and leftover camera shots and images no chat refers to are cleared at start.
- **Web chat:** the same "+" (Photos, Camera on touch screens, Create image), pictures inside replies, and the
  location question in the chat. A photo whose header says it is over 100 megapixels, or that is not a PNG or JPEG,
  is refused with a plain line before it is decoded. On both, the location card says that Use my location keeps
  sharing on until it is turned off, and goes away if the question in the box is deleted; a picture request whose
  words mention the weather ("a fox in the snow") no longer asks for the location.
- **DLNE Donate 1.0.1** (the tray app, Windows and Linux): its own changes since 1.0.0 (the engine button says when an
  update is published; docked and stepping-aside are shown) and a fix found while building it: since the engine's
  hardware check imports the engine's logger, the app's frozen engine snapshot carries a quiet stand-in, without which
  the app said "no GPU" on cards it can use. Apps on 1.0.0 run the 2.6.2 engine as they are.
- **Extension 1.5.0** (its own log): DLNE Swarm in VS Code's model picker, one `@dlne` conversation per Chat thread, the
  sidebar chat fixed.

### Also
- `Callable` was used in `engine/coordinator.py` annotations without being imported (hidden on Python 3.14).
- The phone endpoints are never read past a cap: `/v1/app/chat/completions` refuses a body over 26 MiB (four photos at
  the media limits, in base64, and the words) with 413, from its `Content-Length` or as soon as it runs over; the
  report and picture endpoints take 1 MiB.
- Only the coordinator sends a phone its events: a CPU-pool member's `dlne` field is dropped.
- The `[Picture created: …]` line is found from the end of a reply in one pass (a regex took quadratic time on a long
  run of spaces, on the event loop).

## [2.6.1] - 2026-10-28

Every finding of the review of 2.6.0 fixed, and pictures made on the media card. With the new keys unset a coordinator
answers as 2.6.0 did, except for the security fixes below, which are always on.

### Security
- **An image "zip bomb" is refused.** A PNG whose data inflates far past what its header allows (a 3 MB file made
  the engine allocate 2 GiB) is now inflated under a bound taken from its own header, with chunk counts and side
  data capped; only the PNG and JPEG variants the engine's own decoder reads are accepted (no arithmetic-coded,
  lossless or 12-bit JPEG).
- **No image ever reaches ffmpeg.** With vision on the engine starts with `--video-ffmpeg-dir` pointing at an
  empty private folder. WebP is therefore no longer accepted, and a data URL must be exactly
  `data:image/png;base64,` or `data:image/jpeg;base64,`.
- **Images have a budget:** `IMAGE_INPUT_PER_HOUR` (default 30, 0 to 600; 0 refuses images), counted per network,
  answered with 429 `image_quota` and `Retry-After`. An image encode briefly holds every engine slot, so the budget
  keeps phones from waiting behind a flood of pictures.
- **Volunteers' machines refuse images and unknown message parts** in sealed requests too (400
  `image_input_unavailable` / `unsupported_content_type`); images only ever go to the coordinator's own engine.
- **Chats cannot learn from each other through the cache:** every phone-lane request sends `n_cache_reuse: 0`, and
  an engine with phone lanes starts with `--no-slots`.
- **A client-sent `id_slot` is always dropped** on the coding endpoints, lanes on or off.
- `config/chat_salt.key`: an existing file is made owner-only, and two processes starting together agree on one
  secret. A `chat_id` must be a canonical UUID; anything else counts as no chat id.
- A `location` or `max_tokens` with an enormous number is ignored instead of answering 500.

### Fixed
- **"No machines online" while phones were being answered.** With phone lanes on and the CPU copy off, the status
  said 0 machines and "Qwen3 4B". `app_pool` now counts the engine's lanes as a serving machine and names the
  engine's model; new fields `app_pool.serving`, `app_pool.engine_requests_in_flight`, `phone_lanes.available`
  and `phone_lanes.starting`, and `phone_lanes` stays through engine restarts.
- **Phones during a restart read "DLNE is restarting, try again in a moment"**, not "No machine in the CPU pool is
  free right now".
- **Phone lanes:** a chat no longer waits behind another while autocomplete holds the idle lane; a chat goes back
  to the lane it last used when that one is free (its cache is there); coding re-checks the engine after its queue
  wait; no lookup round starts once a request has held its lane 60 s (the 429 after 25 s waiting stays).
- **The CPU fallback is the CPU pool's own request** (no salt line, the 8,000-character history) and gives the GPU
  lane back while the CPU answers. Engine swaps wait for phone answers on the lanes.
- **Location:**
  - a malformed weather call is dropped, never turned into "weather here";
  - "here" or "near me" with no location is not searched or geocoded, and the model is told to ask for a town;
  - no town within 50 km means no local search;
  - the town is not added to questions that are not local or that already name a place;
  - a report never stores the derived town.
- **Vision:**
  - an image is refused with its real 400 and `code` before it waits in line, not after (a request refused
    after queueing carries `dlne_error.code`);
  - a slow `/props` no longer reads as "no vision";
  - the projector loads only for the model it fits, and a projector death while encoding costs vision for
    30 minutes, not until a restart.
- `/v1/completions` batch prompts (`[[ints]]`, `[[strings]]`) work again.
- Duplicate `ALLOWED_GPU_INDICES` entries are one device and dropped entries are logged; `MEDIA_GPU` is ignored
  consistently on non-Vulkan builds; the projector's working-memory estimate is the measured 0.15 GiB.
- `requests_in_flight` and `median_answer_seconds` say what they count (phone requests on any machine, lanes
  included).

### Added
- **Pictures (off by default, `IMAGE_GEN_ENABLED`).** stable-diffusion.cpp's `sd-server` with Z-Image-Turbo on the
  media card only, and only while the coding engine has vision. `POST /v1/app/images` for phones and
  `POST /v1/images` for the IDE, each with `GET …/{id}` and `…/{id}/cancel`; sizes 768x768 (default), 1024x1024,
  768x1024 and 1024x768 (about 80 s and 150 s on TheWall's RX 580). `capabilities.image_generation` and an
  `image_generation` status block (`queue`, `busy`, `eta_seconds`, `sizes`, `default_size`, `per_hour`).
  - Every prompt is checked (a word list and the coordinator's own model) and every finished picture is looked at
    by the vision model; both checks fail closed. No sexual content, real people, gore, hate symbols, self-harm or
    anything involving minors.
  - Pictures stay in memory until fetched and expire after 10 minutes; nothing is written to disk and no prompt is
    logged (its length and a short hash only). The prompt sd writes into the PNG is stripped.
  - Keys: `IMAGE_MODEL_DIR`, `IMAGE_THREADS`, `IMAGE_CPU_MASK`, `IMAGE_PER_HOUR` (default 6 per network),
    `IMAGE_QUEUE_MAX`, `IMAGE_JOB_TIMEOUT_SECONDS`, `IMAGE_IDLE_UNLOAD_MINUTES`, `IMAGE_RESULT_TTL_SECONDS`.
  - `scripts/package_sd.py` and `scripts/fetch_image_models.py`; the signed manifest carries `sd_build`,
    `server_sd_sha256` and `server_sd_size`, and the coordinator installs the build into `bin-sd/`. Workers never
    fetch it. See [docs/image-generation.md](docs/image-generation.md).
- `/v1/cluster/status` `policy.image_input`: the image limits (formats, count, bytes, side, pixels, hourly budget),
  null while the engine cannot read images.
- Canary decoys carry the extension's `generate_image` tool while pictures are on, so they stay indistinguishable.

### Clients
- The web chat sends the location only with local questions and only while the fix is under 2 minutes old; a
  failed refresh drops the old fix; it can create pictures; and it says who answers (DLNE's own server, with a
  volunteer's computer only as the backup).

## [2.6.0] - 2026-10-28

The phone chat gets its own lanes on the GPU engine and can answer "near me", and the coding chat can read pictures.
Everything below is off by default: a coordinator that sets none of the new keys starts its engine with exactly the
arguments of 2.5.4.

### For people who use DLNE
- **The phone chat answers on the coordinator's GPU, without waiting behind coding.** With phone lanes on, a phone
  chat runs on an engine slot reserved for phones. Coding and autocomplete never take one, and a phone never takes a
  coding slot. If every lane is busy for 25 seconds the phone is told to try again in 5 (a 429). While the engine is
  down or swapping, the CPU pool answers as before.
- **Chats cannot learn from each other.** Each chat's prompt cache is private to it. Without a chat id the phone chat
  keeps no cache at all.
- **Pictures (coding chat, when the operator turns vision on).** `GET /v1/cluster/status` says
  `capabilities.vision`. PNG, JPEG or WebP, up to 4 per request, 4 MiB and 4096 pixels a side each, sent inline.
  A refused image is a 400 with `{"detail", "code"}`: `too_many_images`, `image_too_large`,
  `unsupported_image_type`, `remote_image_url` or `image_input_unavailable`.
- **"Near me" and local weather (opt-in).** The web chat and the Android app may send the phone's precise
  location, only when the user turned it on (off by default), with the chat id. The server uses it for local weather
  and for "near me" searches and for nothing else. The coordinates are never put in the model's prompt, never
  logged or kept, and never sent to a search engine; a local lookup's results name the nearby town, which the model
  then reads. See [docs/privacy.md](docs/privacy.md#the-phone-chat-is-separate).

### For operators
All keys go in `config/config.txt`; a value outside its range keeps the default.
- **Phone lanes.** `ENGINE_PHONE_SLOTS` (0 to 8, default 0 = off) and `ENGINE_PHONE_CONTEXT` (4096 to 65536, default
  8192 tokens per lane). The lanes are extra to `ENGINE_SLOTS` and, with `ENGINE_CONTEXT` set, on top of it. On
  TheWall, `ENGINE_SLOTS=2 ENGINE_PHONE_SLOTS=2 ENGINE_CONTEXT=109226` starts the engine with `-np 4 -c 125952
  --kv-unified --kv-unified-per-slot 54784`, and coding still advertises 54,613 tokens per request. The status
  shows `phone_lanes` ({slots, in_use, waiting}) while the running engine has lanes. Takes effect on the next engine
  start; an engine started before the keys were set keeps answering phones from the CPU pool until then.
- **Media card and vision.** `MEDIA_GPU` (a PCI address, such as `0000:0a:00.0`; the card is then never used for the model), `ENGINE_MMPROJ` (path to the model's
  projector `.gguf`) and `ENGINE_IMAGE_MAX_TOKENS` (1 to 8192, default 1024). Vision is on when `ENGINE_MMPROJ` names
  a file. **Loading a projector turns off prompt-prefix reuse (`--cache-reuse`) and context shift for every request
  the engine answers, coding included**; turn vision on only where that trade is wanted. If an engine dies loading
  the projector or encoding an image, the next start leaves the projector out and coding goes on without vision until
  DLNE restarts. With the engine sharded over volunteers' GPUs the media card is not used and the projector runs on
  the engine's own first card.
- **`ALLOWED_GPU_INDICES` takes PCI addresses** as well as indices, mixed freely (`0,0000:0a:00.0`; read on Linux); a PCI address is also taken without its domain (`0a:00.0`).
- **Chat salt.** `config/chat_salt.key` is made on first use, readable by its owner only. Back it up or leave it:
  if it is lost a new one is made and chats simply stop sharing a cache with their earlier turns once.
- **Place list.** `engine/places_data.py` (GeoNames, CC BY 4.0), built by `scripts/build_places.py`. Nothing to
  configure.

### Compatibility
- DLNE Donate 1.0.0 and the extension 1.4.x workers update normally. The place list ships as a Python module
  (`engine/places_data.py`) because installed volunteers accept only `engine/*.py` and icons from a release.
- Workers never import the place list; only a coordinator that is sent a location does.
- A client that sends no `chat_id`, no `location` or no images is answered exactly as on 2.5.4.

### Credits
- Place names come from [GeoNames](https://www.geonames.org), licensed
  [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

## [2.5.4] - 2026-10-28

No more "Node disconnected mid-response" for a connection the engine dropped before answering.

### Fixed
- **A request the engine dropped before answering is sent again.** A kept-alive connection to the coordinator's own
  engine, closed by the engine just as it was reused, failed the request with "Server disconnected without sending a
  response", and the user got "[System Alert: Node disconnected mid-response.]" although no node had gone and nothing
  had reached the engine (2026-10-27, at the end of an agent's task). The request is now opened once more on a new
  connection (`_open_engine_stream`), for chat, summaries and autocomplete alike. A drop after the answer began is
  still reported.
- **Engine failures say what a client may do.** The message frame carries `dlne_error`: `engine_dropped` and
  `engine_unavailable` (worth sending again a moment later, which extension 1.4.5's agent does instead of ending the
  task), `engine_timeout` and `engine_failed`. Clients that do not read it see the same message as before.

## [2.5.3] - 2026-10-27

A worker survives the editor that started it, and a lost shard no longer leaves the engine spilling.

### Fixed
- **A window reload no longer stops (or hangs) the worker.** The extension keeps the worker running across a VS Code
  window reload, by design, but the worker's stdout is a pipe to the window that started it. After a reload the
  next marker line it printed failed with a broken pipe, and the console logger (Rich) answers that with
  `SystemExit`, which ended the heartbeat loop. Output now goes nowhere once its reader is gone
  (`engine/logger.py`), and the node runs on and is picked up by the new window.
- **A heartbeat loop that ends never waits on itself.** Its last act asked for a normal stop, and on a node
  carrying a shard that stop is deferred until the heartbeat loop sees the coordinator stop draining it: the loop
  that had just ended. The node then hung for good with its RPC server gone (2026-10-27, minutes until it was
  killed). That stop is now immediate.
- **A lost shard steps the model down at once.** Losing an endpoint rebuilds the engine immediately, so a model
  that only fitted with that GPU ran spilling on what was left for `AUTO_SWAP_DOWN_STABLE_SECONDS` (2 min) before
  the controller switched down; the wait could no longer save anything. When the engine already runs on what is
  left, the down-switch is immediate (`engine_short`); the wait still applies while the engine holds the GPU.

## [2.5.2] - 2026-10-27

A docked laptop behind its own firewall shards at LAN speed.

### Fixed
- **The reverse tunnel goes over the LAN.** A GPU in the coordinator's household is now also told the
  coordinator's own LAN address (`lan_tunnel`, `ws://<address>:<port>` on the subnet the worker reported), and its
  reverse tunnel connects there instead of the public address. The worker connects out, so a host firewall that
  drops the coordinator's dial to the RPC port (Arch's default nftables, ufw, Windows on a "Public" network) no
  longer matters, and the router's hairpin and TLS are out of the way: the owner's laptop measured ~24 MB/s
  through the public address against ~290 MB/s on the LAN, which is why its first IQ4_XS shard did not load
  within the 180 s deadline. A tunnel moves only while no stream runs on it, so weights in flight are never cut;
  if the LAN address does not take the tunnel the public one is used for `LAN_RETRY_SECONDS` (600), and if the
  public one does not either the coordinator is away and the LAN is tried first again. The worker accepts only a
  plain `ws://` URL to a private IPv4 address, since the tunnel carries its token.

## [2.5.1] - 2026-10-27

Unsticks the workers that updated to 2.5.0 through the VS Code extension but kept running 2.4.9.

### Fixed
- **Each release dates its files.** Every release archive gave all its files the same date, 1980-01-01. The
  extension unpacks with `unzip`, which keeps that date, and Python reuses a cached `.pyc` while its source's
  date and size are unchanged; `engine/version.py` is 616 bytes in both 2.4.9 and 2.5.0, so a worker the
  extension updated kept running 2.4.9's bytecode, was refused by the 2.5.0 coordinator and stopped after two
  restarts. The one date every entry shares now comes from the archive's contents (the same tree still builds the
  same bytes), so each release invalidates the previous one's bytecode. A stuck worker comes right on its next
  start; if the extension had given up, start contributing again.

## [2.5.0] - 2026-10-27

The release for machines that are already on: a docked laptop shards into the shared engine, an undocked one is a
coding member, a GPU that steps aside is drained rather than killed, yielding no longer flaps or yields to DLNE
itself, and coding members get their settings from the signed release. Worker and coordinator must match
exactly, as always: every worker updates itself through its launcher on the first heartbeat after the deploy.

### Added
- **Docked.** A GPU node reports whether it is on a wired link and on mains power (`link`, `power`, `docked` in
  the signed profile; `engine/hardware.get_dock_state`, sysfs and `/proc/net/route` on Linux,
  `GetSystemPowerStatus` and the default route's adapter on Windows, `pmset` and `networksetup` on macOS; a
  dock's USB Ethernet counts as wired, a VM never claims a link, a fact that cannot be read is left out, a change
  is reported only once it has held 60 s). A near GPU shards only while docked; an undocked one is left out of the
  pool automatic switching sizes the model for and is never given a shard. The worker prints `DLNE_DOCK` once per
  change for the editor and the tray app. On Windows the RPC port is also opened to the local subnet on a network
  left "Public", so a coordinator on the same LAN can dial a docked laptop.
- **Graceful leaving.** A registered GPU that is stepping aside (yielding, undocked, or saying `leaving`) is
  left out of new plans at once, but the engine running on it is rebuilt only after the answers in flight finish,
  `SHARD_DRAIN_SECONDS` (60) at most; a node whose heartbeats stop still restarts the engine immediately. The
  heartbeat answer tells an RPC node the engine runs on `shard: true`, and `draining: true` while that rebuild is
  pending; a normal stop of such a node sends `leaving` and keeps its RPC server up until the key is gone or
  `LEAVE_GRACE_SECONDS` (90) pass, and a yield that outlasts the drain restarts the RPC server so the owner gets
  the VRAM back. A GPU planned but not yet run on keeps its owner's perks; a gain waits behind running answers for
  `TOPOLOGY_REJOIN_MAX_SECONDS` (600) at most.
- **Signed server settings for coding members.** The release manifest carries, per tier, `server_slots`,
  `server_ctx_checkpoints`, `server_ubatch` and `server_spec_type`; a member applies them from its signed copy
  when it starts llama-server. The coordinator chooses each member's slot count from its hardware
  (`coding_slots` in the heartbeat answer; with the shipped floors every card under 48 GB runs one slot,
  a 48 GB card or 24 GB+ of unified memory two), and the context per
  request is the tier's invariant: `context ÷ server_slots`, 40,960 tokens on the Qwen3.6 tiers (81,920 over 2),
  so no member can shrink anyone's `coding_context_tokens`. `/v1/cluster/tiers` shows `server_slots` and
  `context_per_request`; `DLNE_ROLE` carries the member's context and join-test speeds.
- **Speed in the status.** `coding_members` reports the pool's median and minimum decode and prefill speeds
  (aggregates only; nothing names a machine).
- **The requester's own member first.** A route lookup may carry the requester's own trust id (`own`); the
  coordinator prefers that member when it is eligible for the privacy mode and at least as fast as the shared
  engine's current per-slot rate.
- `DLNE_YIELD`: the worker says once per change whether it is stepping aside for its owner and why.

### Changed
- **A coding member is not a member for good.** The link probe now measures coding members too; a member that
  is near and docked is told "rpc" and switches back into the shared engine. The role grace runs from the
  current registration, so a node that left and came back is not "far" on its first beat, and a node reached
  over a confirmed LAN address ranks as LAN whatever address it registered from.
- **Yielding v2.** DLNE's own work is this process and everything it started (the name tags stay as a fallback
  for an engine somebody else runs), so the worker never yields to its own probes, downloads or bandwidth test;
  the busiest card decides and amdgpu's `gpu_busy_percent` is read on Linux; the limits are `YIELD_GPU_PERCENT`
  and `YIELD_CPU_PERCENT` (60); one sampler thread shared by the whole process takes a reading every 3 s, a
  full-screen app yields at once, other load has to last two readings and the owner has to be quiet for ten
  before the node comes back. A yield counts as a job for the owner's perks for `YIELD_PERKS_MAX_HOURS` (6),
  not for ever, and a coding member drains for 30 s instead of 10 before it gives the GPU back.
- **Canary decoys ask for 10,240 tokens**, the extension's answer size since 1.4.2 (16,384 before).

---

## [2.4.9] - 2026-10-27

### Security
- **The public status no longer publishes addresses.** `/v1/cluster/status`, which anyone can read, listed every
  worker's `ip:port` (`active_endpoints`: a volunteer's home address) and the IP address of every caller with a
  request in flight (`in_flight_leases[].client_id`). `active_endpoints` is gone (`active_workers_count` stays),
  and an in-flight lease now shows only its `task_type`, `elapsed_seconds` and `remaining_seconds`
  (`lease.public_summary`). Nothing read either field.
- **The Discord swarm tile no longer posts workers' addresses.** It listed each remote worker as
  "Remote Worker (`ip:port`)" in a public channel; it now says how many volunteer GPUs there are and how far
  away (`gpu_pool.reach`).

### Fixed
- **GPUs on the coordinator's own LAN, and volunteers behind NAT anywhere, can now join the shared model.** A
  GPU counted as near only if the coordinator could open a TCP connection to the address its heartbeats came
  from, within 15 ms. A machine in the coordinator's own house joins through the public address, so it
  heartbeats from the household's public IP, which cannot be dialled back; a volunteer behind NAT has no
  reachable address at all. Both read as far forever, the bigger tiers never unlocked, and the swarm never
  grew. Now (`engine/link_paths.py`):
  - A GPU is timed along the path the engine will use: its household LAN address, its own address, or its
    reverse tunnel (new `PING`/`PONG` frames, types 4 and 5).
  - A GPU worker whose connections come from the coordinator's own public address is asked for its LAN
    addresses (`"send_lan": true` in the heartbeat answer, `lan_ipv4` in its next signed profile). Only
    addresses on the coordinator's own non-tunnel subnets are kept, and one is dialled only after a TCP
    connection to it succeeds. `reverse_tunnel.resolve_endpoint` tries that address first.
- **"Unlocked" meant nothing.** `coding_tiers[].unlocked`, `recommended_tier` and `/v1/cluster/tiers` counted
  every GPU, far ones included, so the site promised a bigger model that automatic switching would never
  move to. They now count the near GPUs only, the same pool auto-swap uses. `total_vram_gb` is unchanged.
- **A far GPU's coding model was sized on the wrong RAM.** The coordinator chose the tier from `ram_gb`, which
  is what the RPC node lends (`MAX_RAM_GB`, 4 GB by default), so an 8 GB card fitted no tier and sat idle. The
  heartbeat now carries `coding_ram_gb` (the machine's RAM under `CODING_MAX_RAM_GB`, uncapped by default),
  and the tier is chosen on that.
- **A worker's own link report no longer makes it near.** `/worker/link_report` is the worker's story; whether
  a GPU may share the model is decided only by the coordinator's own measurement (`registry.link_probes`). The
  shard planner orders volunteers by that measurement first.

### Changed
- **New distance limits (grades: 40 ms and under best, 80 good, 120 usable, more too far).**
  `AUTO_SWAP_MAX_RTT_MS` defaults to 80 (was 15): a GPU within it shares the coding model. A farther GPU may
  still run a whole model of its own, up to `CODING_MEMBER_MAX_RTT_MS` (new, default 120); past that it gets
  no job. A GPU nobody has measured may still be a coding member, as before.
- `/v1/cluster/status`: `gpu_pool.reach` counts the GPUs by grade (`host`, `best`, `good`, `usable`,
  `too_far`, `unmeasured`), and `policy` has `shared_max_rtt_ms` and `coding_member_max_rtt_ms`.
- **An automatic move up must be fast enough to keep.** Once the bigger model has loaded, and before any
  request is let in, the coordinator times a short generation on it (an 8-token warm-up, then 64 tokens,
  timed by llama.cpp and filed in `logs/benchmarks.jsonl`). Under `AUTO_SWAP_MIN_DECODE_TPS` (new, default
  10 tokens a second; 0 turns the check off), or not finished within 90 s, the previous model is put back
  and the tier is held off like any failed switch (6 h, doubling to 48 h). A move down is not checked: it
  happens because the bigger model no longer fits.

### Added
- **The status says how much context a coding request can use.** The `policy` block of `/v1/cluster/status` has `coding_context_tokens`: the smallest context per request among the coordinator's own engine (its context over its slots) and every ready coding member (the tier's context over the member's slots; 65,536 over 2 by default). It is 32,768 when nothing better is known. The extension (1.4.0) sizes its working memory from it, so a long task is trimmed to fit whichever machine will answer.

### Changed
- **Privacy decoys follow the agent's new request.** The extension's agent (1.4.0) has a new system prompt, a `remember` tool and, when a project has notes, a `<dlne_context>` message. Canaries now carry the same prompt and the same eight tools, and about two in five carry a decoy `<dlne_context>` with plausible notes for their made-up project, in the exact format the extension writes. A decoy context never holds anything the extension's redactor would change, and canaries never carry a working-memory block. `vscode-extension/test/canary-parity.test.js` compares both sides against `docs/agent-memory.md`.

---

## [2.4.8] - 2026-10-26

### Added
- **The status states the rules the websites tell visitors.** `/v1/cluster/status` has a `policy` block, read from the limits the coordinator enforces: `trial_chats`, `cpu_chats` and `cpu_window_hours` (what a CPU contributor gets back), `cpu_min_ram_gb`, `cpu_min_cores`, `cpu_min_bandwidth_gbs`, and `coding_member_min_vram_gb`. The pages showed these figures typed in; now they can read them live.

### Changed
- **Privacy decoys ask for as many tokens as the agent does.** Extension 1.3.5 lets an agent answer run to 16,384 tokens (it was 4,096). Canary decoys must look exactly like a real agent request, so they ask for 16,384 too, and a test in the extension holds the two figures together.

---

## [2.4.7] - 2026-10-26

### Changed
- **DLNE's address is now `https://dlne.rossgilsonlabs.dev`.** The same name serves the DLNE website at `/` and passes the coordinator's own paths (`/chat`, `/c/`, `/v1/`, `/worker/`, `/ws/`) straight to it. `/v1/admin` stays closed from outside, as before.
  - The landing page and DLNE Chat link to it as the website, and give it to link previews.
  - Canary decoys are written with it.
  - `scripts/release.py` checks a deploy through it.
  - `https://dlne.duckdns.org` still answers, and so does every earlier address. Workers and phones already pointed at them keep working; extension 1.3.4 and the Android app 1.0.4 move to the new name on their own.

---

## [2.4.6] - 2026-10-26

### Fixed
- **A volunteer whose computer clock is wrong can join.** Signed messages carry Unix epoch seconds, which are the same in every time zone, and the coordinator refuses anything more than 30 s from its own clock. A computer whose clock was off was refused at its first heartbeat and stopped, and was told its network key was wrong. That happens with no automatic time sync, on a dual-boot PC whose hardware clock Windows keeps in local time while Linux reads it as UTC (off by the UTC offset, several whole hours), and with a wrong time zone. Now:
  - The worker keeps the coordinator's time (`engine/swarm_clock.py`) and signs its heartbeats, its tunnel enrollment and a coding member's identity block with it.
  - The coordinator puts its time on every answer to a worker (`X-DLNE-Time`, Unix seconds). The worker learns it from every answer, and from the standard `Date` header, read as GMT, as a fallback.
  - A refusal for a "stale timestamp" is corrected and sent again at once. Only a clock that keeps jumping ends the worker, and then with a message about the clock.
- **The owner is told, once, how far off the clock is.** For example "5 h behind the swarm's", and when it is a whole number of hours, that a time zone or a dual-boot hardware clock is the likely cause and how to fix it. The worker also prints `DLNE_CLOCK: {"offset_seconds": N}` for the editor.
- The coordinator's 30 s window is unchanged. Its log now says how far a refused worker's clock was from its own.

---

## [2.4.5] - 2026-10-26

### Changed
- **DLNE Chat folds an answer's sources into one line.** A looked-up answer listed every source under it, ten at a time, and pushed the answer out of view. The list is now one line, "10 sources · patriots.com, nytimes.com, espn.com +3", that opens when tapped (and stays open while the answer streams on). The numbers in the answer still link straight to their sources.

---

## [2.4.4] - 2026-10-26

### Fixed
- **The coding slots are right from the start.** After a restart, `coding_queue.slots` in `/v1/cluster/status` said 2 (the default) until the first coding request, while the engine had 5, and the website's swarm tile showed 2. The count now follows the running engine as soon as the status sees it, and autocomplete (which runs only while a slot is free) uses the real count too.

---

## [2.4.3] - 2026-10-26

### Changed
- **A coordinator restart no longer starts the GPU grace again.** The moment each idle GPU worker began standing by
  (the clock behind `GPU_STANDBY_GRACE_MINUTES`) is kept in `config/gpu_standby.json`: written atomically, at most
  every 30 s while it changes and at a clean shutdown; read once at start. Only workers with no job have an entry,
  and one not seen for 24 hours is forgotten. A missing, old or damaged file means no history (never an error).

### Fixed
- **No file path in an answer.** llama-server reports the path it loaded the model from in every answer
  (`"model": "/home/<user>/.../X.gguf"`), and those answers reach other people: a phone through the pool, a
  developer through a coding member. Every llama-server DLNE starts now gets `--alias <the model file's name>`
  (`Qwen3.6-35B-A3B-UD-IQ3_S.gguf`), so answers and `/v1/models` carry the name and no directory. That covers the
  coordinator's coding engine, the coordinator host's phone member, volunteers' phone members (light mode too) and
  coding members. Nothing in DLNE reads the `model` of an answer (the extension and the phone apps name the model
  from the pool's status), so nothing that matches on a model name changes.

---

## [2.4.2] - 2026-10-26

### Added
- **Live pool numbers in `/v1/cluster/status`** (counts only): `gpu_pool` {`gpus`, `vram_gb`, `gpus_standing_by`,
  `vram_standing_by_gb`, `tokens_per_second`, `tokens_last_hour`, `measured_tokens_per_second`} and `cpu_pool`
  {`machines`, `machines_standing_by`, `cores`, `ram_gb`, `tokens_per_second`, `tokens_last_hour`}. A GPU or CPU is
  *serving* when it has a job (the coordinator's engine runs on it, a ready coding member, a ready phone-pool
  member) and *standing by* when it is registered without one. Tokens are counted as they arrive, nothing of the
  content is kept. The worker reports `gpu.count` and `app.threads` for these counts.
- **Light mode for a volunteer's CPU-only phone-pool member.** While the owner games the member no longer stops: at
  once, with no restart, its model server drops to the lowest priority and to half the CPUs it may use, and the
  member reports one slot; after 10 s for the answers in flight it is restarted lighter (half the threads, one
  slot, half the context, no prompt cache); 60 s after the load ends it is restarted at full strength. At most one
  switch per 60 s, so a flickering load cannot cause a restart loop. It stays in the phone app's routing meanwhile:
  the coordinator keeps a member whose owner is gaming while its status says it is ready. The "both" machine's
  phone-pool half still steps aside completely, the coordinator host's own member is unchanged, and a member whose
  owner never games is not touched. Details: [docs/coding-members.md](docs/coding-members.md).

### Changed
- **GPU-tier perks follow use.** Unlimited coding and first in line while the contributor's GPU is serving, or for
  its first `GPU_STANDBY_GRACE_MINUTES` (default 30) standing by; after that it is a CPU-tier contributor (300 chats
  per 5 hours) until it gets a job. A coordinator restart starts the grace again.

### Fixed
- The VRAM the coordinator sizes its engine from (`total_vram_gb`) no longer counts phone-pool members, coding
  members or "both" companions, which are not part of the shared model's pool.

---

## [2.4.1] - 2026-10-26

### Fixed
- **"Both": the phone-pool half steps aside while the owner games, as extension 1.3.0 says.** In 2.4.0 only the
  GPU half did. Now the companion reports `ready: false` at once (the coordinator sends it nothing new), gives the
  answers in flight 10 s, stops its model to give the CPU back, and starts it again 60 s after the load ends. A
  machine that lends only its CPU is unchanged.

---

## [2.4.0] - 2026-10-26

Coding members are off until the operator sets `CODING_MEMBERS_ENABLED=true`. With it off, every endpoint
answers as in 2.3.7, apart from the additions named below. Details: [docs/coding-members.md](docs/coding-members.md)
and [docs/privacy.md](docs/privacy.md).

### Added
- **Coding members.** A volunteer's GPU with 8 GB or more that is not on the coordinator's fast network can run a
  whole coding model behind a small member proxy, reached through the existing reverse tunnel, and answer coding
  requests on its own: two agent slots each. It runs the smartest Qwen3.6-35B-A3B tier it can hold whole and run
  fast enough, and a join test checks the speed floors (`CODING_MEMBER_MIN_DECODE_TPS` 15,
  `CODING_MEMBER_MIN_PREFILL_TPS` 80). A machine with no GPU is never given the job.
- **The volunteer stays in control.** `CODING_MEMBER_ALLOWED=false` refuses the job, `MAX_VRAM_GB` and
  `MAX_CPU_THREADS` cap it, and the member steps aside while the owner's own load (a game) needs the machine.
  The coding job's RAM has its own cap, `CODING_MAX_RAM_GB` (-1: none, the job counts only the RAM that is free;
  extension 1.3.0 sets it only from a `dlne.worker.maxRamGb` the volunteer chose). Without the key it follows
  `MAX_RAM_GB` as before.
- **Routing.** `GET /v1/coding/route` hands the extension a route id (10 minutes) and the member's public key.
  Privacy filter first, then the member that served the same session in the last 30 minutes, else the least busy.
  Only sealed requests ever reach a member; a plain request is answered by the coordinator's own engine. A
  sealed request that cannot be served answers `503 dlne_reroute` and the extension asks again. Member-served
  answers carry `X-DLNE-Served: member`.
- **End-to-end encryption** between the extension and a member (X25519, HKDF-SHA256, ChaCha20-Poly1305): padded
  to 256 bytes, complete only after a sealed end marker, each request usable once, member keys rotated every
  6 hours. The coordinator relays bytes it cannot open.
- **Privacy modes** `open`, `trusted` and `any`, sent by the extension; the coordinator never widens one, and a
  request without one counts as `trusted`. Trusted machines are listed by trust id in `TRUSTED_MEMBERS`, derived
  from each member's own Ed25519 identity key, which also signs its coding heartbeat.
- **No-trace members and canaries.** Members run their model server with logging off and never log bodies.
  Decoy requests carrying a made-up `/c/<token>` link flag a member that opens it (`CODING_CANARY_PER_DAY`, 4 per
  untrusted member); the operator sees flags at `GET /v1/admin/canaries`.
- **Place in line.** A streamed request from the DLNE extension (`X-DLNE-Session`) that has to wait gets
  `dlne_queue` frames with its place every 3 s. Every other caller waits silently, as before.
- **"Both": a GPU machine's CPU can serve the phone pool at the same time.** With `ALSO_CPU_RAM_GB` set
  (`dlne_server.py worker --also-cpu-ram <gb>`, written by extension 1.3.0's "Lend both"), the worker runs a
  companion node, `<worker id>-cpu`, on a thread of the same process: an ordinary phone-pool member with its own
  heartbeat and tunnel and `companion_of` in its signed profile. The GPU node keeps its own job; every node still
  has one. The two split the CPU threads (`MAX_CPU_THREADS` is the total), and a coding model loads after the
  phone-pool model has. The coordinator counts the machine once; the phone pool counts the companion.
- `/v1/cluster/status` gains `coding_members: {enabled, members, ready, slots, busy}` (counts only).
- Verified, resumable downloads (`engine/downloader.py`): every part's size and SHA-256 checked.
- The signed coding ladder gains one row, disabled until measured: Qwen3-Coder-Next UD-IQ4_XS on a 49 GB rung.

### Changed (not behind the members flag)
- **Faster ghost text.** Before each fill-in-the-middle completion the coordinator sends its engine the text
  before the cursor as a request of its own (no tokens generated), so the real request re-reads only what follows
  the cursor. Median 1.57 s to 1.0 s per completion on TheWall, same quality. `--cache-ram` is now allowed in
  `ENGINE_EXTRA_ARGS`.
- **More coding slots from the same GPUs.** The engine now chooses its own slot count and per-slot context from the model's real KV cost and the free VRAM: slots first (as many as hold 32,768 tokens each, at most 8), then more context per slot. New `ENGINE_SLOTS` (0 = automatic) sits next to `ENGINE_CONTEXT`; with `ENGINE_CONTEXT` set and `ENGINE_SLOTS` at 0 the slots are the context divided into 32,768 per slot, so TheWall's 65,536 is still 2 x 32,768.
- **Correct KV sizing for hybrid models.** `engine/gguf.py` counts only the attention blocks (Qwen3.6-35B-A3B: 10 of 40, 20 KiB a token at f16, not 80), prices the recurrent state each slot carries (62.8 MiB), and prices q8_0/q4_0 caches exactly (34/32 and 18/32 bytes per element). Checked against llama.cpp's own memory breakdown (805 MiB at `-c 65536 -np 2`).
- **Admission from real bytes.** A model whose header can be read is admitted from the bytes that sit on the GPUs (file minus the token embedding and any `--n-cpu-moe` expert blocks) plus 0.75 GiB of runtime buffers, instead of 1.2 x the file. Qwen3.6 IQ3_S on TheWall: 11.8 GiB, not 15.3. Its RAM part must fit the host's RAM.
- **q4_0 cache is chosen per slot** (from 131,072 tokens a slot), not on the total context.
- **Shard planner** (`registry.plan_endpoints`) asks for `required / POOL_SAFETY_FRACTION` of raw VRAM, so it no longer plans a pool that admission would then refuse.
- **Windows and gaming-mode fixes in the worker.** A 16 GB Radeon on Windows reports 16 GB, not 4, and the
  "owner is gaming" signals keep working while the machine serves (full-screen detection on Windows, per-process
  GPU use on NVIDIA).

### Security
- Free-trial callers' route lookups hold no member slot and are limited to 6 per 30 s per client (429 with
  `Retry-After`).
- A member's whole non-streamed answer is capped at 8 MiB and a whole exchange at 30 minutes, a stalled stream
  included.
- The member proxy answers 421 to any request with an `Origin` header or a non-loopback `Host`, so a web page in
  the owner's browser cannot use it.
- A worker that already serves as a coding member keeps its role across a coordinator restart.
