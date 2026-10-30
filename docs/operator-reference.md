# Operator reference

The detailed settings, privacy design and release notes for people who run a coordinator. For the setup walk-through, start with [Run your own coordinator](../README.md#run-your-own-coordinator).

## Coding members: lend a GPU, add agents

A **coding member** is a volunteer's GPU that runs a whole coding model on its own and answers coding
requests by itself, the way the phone pool's machines already do for chat. One request touches one
machine, so ordinary home internet is enough, and **each member adds about two more agents** working at
the same time.

- **Who qualifies:** a GPU with 8 GB of memory or more that is not on the coordinator's own fast network
  (a GPU that is, and is docked, still joins the shared big model, as before: on mains power and on a wired
  link, or on Wi-Fi whose link passes the coordinator's test, 2.6.4; a laptop on battery or off its dock is a
  member like a far GPU), and about 22 GB of GPU memory plus free
  system memory together for the first version (for example an 8 GB GPU with 14 GB of RAM free). The model
  is held whole: GPU memory first, the rest in system RAM. When a machine joins, it is timed on reading a
  prompt and writing an answer; one under the speed floors is not sent work.
- **Which model:** the smartest version of Qwen3.6-35B-A3B that the machine can hold whole and run fast
  enough. About 22 GB of GPU plus free system memory runs the first version (IQ3_S). Higher-quality
  versions of the same model need more, roughly 26 GB (IQ4_XS), 30 GB (Q4_K_M) and 37 GB (Q6_K), and the
  larger ones also need a stronger GPU to be fast enough. Open, MIT-licensed heavyweight models are planned
  from 192 GB; they are not available yet.
- **Your machine stays yours.** A volunteer can refuse the job (turn off `dlne.worker.allowCodingMember` in
  the extension, which writes `CODING_MEMBER_ALLOWED=false` into the engine's `config.txt`, or choose "Lend my
  CPU only"), the lending limits
  (`dlne.worker.maxVramGb`, `dlne.worker.maxRamGb`) are honoured, and a member steps aside while the
  owner's own load (a game, a render) needs the machine.
- **Off by default.** A coordinator uses members only when its operator sets `CODING_MEMBERS_ENABLED=true`;
  until then every coding request is answered by the coordinator's own engine, as before.
- **The coordinator's status** (`/v1/cluster/status`) reports `coding_members` with counts only: how many
  members have joined and are ready, how many agent slots they offer, and how many are in use.

### Privacy, in short

**A coding member has to read a request to run the model, so it sees the code it is sent.** What protects
you from members:

- **Public repositories** may be answered by any member, including changes you have not pushed yet (for
  unpublished work in a clone of a public repository, set `dlne.privacy` to `trustedOnly`). **Everything
  else** is answered only by the coordinator's own engine or by machines the operator has listed as
  trusted, unless you opt in (`dlne.privacy`). While coding members are off, the extension makes no
  repository lookup. (VS Code's own agent on DLNE Swarm never gets the public-repository widening: its
  requests go only to the coordinator's own engine or trusted machines unless `dlne.privacy` is `anyMember`.)
- **Secrets are removed in the editor** before a request leaves it, and DLNE's own agent (`@dlne`, the sidebar
  chat) never reads files such as `.env`, `*.pem`, `.gitignore`d files or anything in `.dlneignore`.
  VS Code's own agent on DLNE Swarm is the exception: its tools read the files, so DLNE refuses the excluded
  ones it can see by path and redacts secrets, but VS Code's own tools may still put file text into a request.
- A member is **not told who asked**: no IP address, no API key, no account.
- Members run with **no-trace** settings (prompts kept out of logs and off disk), and the coordinator
  sends **canary** decoys to catch a member that snoops.
- Requests and answers are **encrypted on the way** to and from a member (X25519, HKDF-SHA256,
  ChaCha20-Poly1305): the coordinator relays bytes it cannot open, and the tunnel and the internet between
  it and the member carry nothing readable. That protects the trip, not the machine that answers.
  Requests answered by the coordinator's own engine go over HTTPS instead.
- **The coordinator is the operator's own machine and is trusted**: it hands out each member's public key.
- A volunteer who changes DLNE's software could still read or keep what a member receives. None of this
  can promise otherwise.

The details, what members can and cannot see, and what none of this can promise are in
[`docs/privacy.md`](privacy.md). The formats and routes are in
[`docs/coding-members.md`](coding-members.md).

## All coordinator settings

The first start, the key and the model are in [Run your own coordinator](../README.md#run-your-own-coordinator).
This is the long list.

```bash
python dlne_server.py start --port 11435
python dlne_server.py adminkey create   # prints the operator key once; only its hash is stored
```

Everything under `/v1/admin/*` needs that operator key as `Authorization: Bearer <key>`, and by
default only accepts it from the coordinator's own machine (`ADMIN_ALLOW_REMOTE=true` to change
that). BarrkBOT's `/dlne` command uses it.

Settings live in `config/config.txt` (`KEY=VALUE`). Eight groups matter for most operators:

- **Coding members (off by default)**: volunteers' GPUs that each run a whole coding model and answer
  coding requests on their own (see [Coding members](#coding-members-lend-a-gpu-add-agents) and
  [`docs/coding-members.md`](coding-members.md)). `CODING_MEMBERS_ENABLED=false` keeps today's
  behaviour; set it to `true` to route to members. `TRUSTED_MEMBERS` is a comma-separated list of trust
  ids, each derived from a member's own identity key (a member shows its trust id in its log and UI): only
  those machines (and the coordinator's own engine) answer private workspaces. A worker id carries no
  trust, and a member can never claim it. `CODING_MEMBER_MIN_DECODE_TPS` (15) and
  `CODING_MEMBER_MIN_PREFILL_TPS` (80) are the speed floors from the join test, and
  `CODING_CANARY_PER_DAY` (4 per member) sets how many decoy requests each member gets.

- **The CPU pool (the phone app)**: workers with no usable GPU serve the phone app instead of
  the coding pipeline, each running a whole model on its CPU (`engine/app_pool.py`,
  `engine/app_member.py`). The tiers are signed into each release from
  `scripts/app_models.json`: Qwen3 4B (members lending 8 GB), Qwen3.6 35B-A3B (16 GB) and
  Qwen3.6 at full quality (24 GB). The coordinator's own machine serves the biggest tier its
  allocation holds; a bigger tier unlocks when `APP_TIER_MIN_MEMBERS` (2) volunteers can host
  it. A volunteer's member runs at low priority on half its cores, 2 to 8 threads
  (`APP_MEMBER_THREADS` to override). Phone users are throttled per network: one answer at a
  time and `APP_RATE_PER_MINUTE` (6) requests a minute. The coordinator's own machine joins
  with `APP_LOCAL_ENABLED=true`, `APP_LOCAL_THREADS`, `APP_LOCAL_RAM_GB` and
  `APP_LOCAL_CPU_MASK` (hex, one thread pinned per logical CPU; the public swarm uses cores
  2-5 of a Ryzen 5 7600X: `0xf3c` with 8 threads and 16 GB). The model files go in the models
  folder. The phone page is `/chat` (installable from Chrome, or on an iPhone from Safari:
  Share, Add to Home Screen); the Android app is in `android/` (the APK is on the
  [website](https://dlne.rossgilsonlabs.dev); it is not on Google Play yet). Reported
  answers (`POST /v1/app/report`) are kept in `config/app_reports.jsonl` and read with
  `GET /v1/admin/app_reports`.

- **Lookups (the phone model's search)**: the model is told today's date and may look things
  up when it is unsure (`engine/search.py`): weather (Open-Meteo), Wikipedia (with Wikidata),
  news (GDELT) and web search. All are free and run by the coordinator, never by pool
  members. Web search asks a local SearXNG first (`SEARXNG_URL`; see `deploy/searxng/`), then
  Brave Search only if `BRAVE_SEARCH_API_KEY` is set and within `SEARCH_DAILY_LIMIT` (33 a
  day fits Brave's free monthly credit), then Wikipedia. Answers are cached
  (`SEARCH_CACHE_MINUTES`, 30), each network gets `SEARCH_PER_NETWORK_PER_HOUR` (30) lookups,
  and `SEARCH_ENABLED=false` turns lookups off.

- **The engine's slots, phone lanes and media card (2.6)**: `ENGINE_CONTEXT` fixes the engine's total context and
  `ENGINE_SLOTS` its coding request slots (0 for both: the engine sizes itself from the model and the free VRAM).
  `ENGINE_PHONE_SLOTS` (0 to 8, default 0 = off) adds slots reserved for the phone chat, each
  `ENGINE_PHONE_CONTEXT` tokens (4096 to 65536, default 8192). They come on top of the coding slots, so coding is
  not shrunk to make room: `ENGINE_SLOTS=2 ENGINE_PHONE_SLOTS=2 ENGINE_CONTEXT=109226` starts
  `-np 4 -c 125952 --kv-unified --kv-unified-per-slot 54784` and coding still advertises 54,613 tokens per request.
  `ENGINE_KV_TYPE` (2.6.5: `auto`, `q8_0`, `q4_0` or `f16`; default `auto`) is the context memory's type. `auto` is
  q8_0, and q4_0 once a slot reaches 131,072 tokens; any other value is used whatever the slot size. TheWall runs
  `ENGINE_KV_TYPE=q8_0` with its 2 x 131,072 lanes, because the owner's rule is that q8_0 is never lowered.
  A phone chat then answers on the GPU on a lane of its own and never takes a coding slot (coding never takes a
  phone lane); with every lane busy for 25 seconds it gets a 429 with `Retry-After: 5`, and with the engine down
  or swapping the CPU pool answers (and when no CPU machine is ready either, the phone reads "DLNE is restarting,
  try again in a moment"). A request keeps its lane at most about a minute of lookups: no lookup round starts after 60
  seconds. `phone_lanes` in the status shows `slots`, `in_use` and `waiting`, plus `available` (phones are answered
  on the lanes now) and `starting` (lanes are configured but the engine is down, loading or swapping; `in_use` is
  then 0); it stays while the engine restarts. While the lanes answer, `app_pool` counts the engine as a serving
  machine: `members_ready` is at least 1, `active_model` and `active_tier` name the engine's model, every CPU tier is
  `unlocked` and `serving` is `engine` (`cpu` when the CPU pool answers). `requests_in_flight` counts the phone model
  requests running now on any machine (`engine_requests_in_flight` is the lanes' share) and `median_answer_seconds`
  the whole phone answers finished in the last 15 minutes.
  `MEDIA_GPU` (a PCI address) keeps one card out of the model's memory; `ENGINE_MMPROJ` (a projector `.gguf`)
  lets the coding chat read images, on the media card when there is one; `ENGINE_IMAGE_MAX_TOKENS` (default 1024)
  caps what one image costs in context. Those three, the phone lanes and the image budget below change nothing
  until set: with none of them the engine starts with the command 2.5.4 started it with. Four things are on
  whatever is set, and are deliberate: a message part that is not `text` or `image_url` (a `video_url`,
  `input_audio`, ...) is a 400 `unsupported_content_type` on every role (llama-server would fetch the URLs in
  them), and so is a `/v1/completions` prompt that is an object (only a string, token ids, or a batch of those are
  taken); a client's own `id_slot` is dropped on `/v1/chat/completions` and `/v1/completions`, lanes or not; the
  coding members apply the same two rules to what they open (`engine/coding_member.py`) and refuse images, which
  they cannot read; and `/v1/cluster/status` carries `capabilities` and `policy.image_input`.
  Loading a projector turns off cache reuse (`--cache-reuse`) and context shift for every request, coding
  included. `ALLOWED_GPU_INDICES` takes PCI addresses as well as indices (`0,0a:00.0`). An address is known only
  where the probe reads it: on Linux, from sysfs, for the cards the Vulkan probe lists (AMD, Intel); with
  nvidia-smi, clinfo, Windows or macOS use the id. A card named twice is one device, and every entry left out (no
  such card, or the media card) is said once in the log. On a llama.cpp build that is not Vulkan `MEDIA_GPU` is
  ignored, in the engine command and in the pool this machine advertises alike.
  `MEDIA_GPU_SHARED=true` (default `false`; only the word `true` turns it on) lends the media card to the model as
  well (2026-10-30, "Smarter Wall"): the card holds layers like the others and its memory counts in the pool this
  machine advertises (what the coordinator, the "Needs X GB Total" check and automatic switching size the model
  for), while the projector stays on it. It keeps its place, last: `--device` lists the engine's cards and then the
  media card, so `-ts` in `ENGINE_EXTRA_ARGS` takes one more number, the last one, for it. On TheWall
  (`MEDIA_GPU=0000:0c:00.0`, `ENGINE_EXTRA_ARGS=-ts 15,15,10 ...`) that is `--device Vulkan0,Vulkan1,Vulkan2
  -ts 15,15,10 --mmproj-device Vulkan2` and a pool of 24 GB. Picture making needs the card to itself, so it never
  starts while the card is shared (the log says "the media card is shared with the coding model
  (MEDIA_GPU_SHARED)"). An engine running across volunteers' GPUs drops the card as it always has (and the `-ts`
  with the rest of the card placement), so the model then runs on the other cards and the volunteers'; the pool
  this machine advertises still counts the card. Without `MEDIA_GPU`, or on a build that is not Vulkan, the flag
  changes nothing.
  Details in the [changelog](../CHANGELOG.md#260---2026-10-28) and [`docs/privacy.md`](privacy.md).

  Images, on the coding chat (`POST /v1/chat/completions`) and, from 2.6.2, the phone chat (Photos, below): up to 4
  per request, PNG or JPEG, 4 MiB and
  4096 pixels a side each, sent inline as `data:image/png;base64,...` or `data:image/jpeg;base64,...` (spelled
  exactly so: lowercase, no spaces, no other type; a link is never fetched). The validator accepts what the
  engine's decoder (llama.cpp's stb_image) decodes and nothing else: a PNG's data is inflated here, with a bound
  taken from its own header, so a small file that expands to gigabytes is refused (`image_too_large`); JPEG is
  baseline or progressive Huffman, 8-bit; arithmetic-coded, lossless and 12-bit JPEG are refused. WebP is not
  accepted: stb_image cannot read it, and llama.cpp would hand it (and any file stb_image refuses) to an `ffmpeg`
  found on `PATH`, so the engine is started with `--video-ffmpeg-dir` pointing at an empty private directory
  (`<install>/no-ffmpeg`) and that path cannot start anything. `IMAGE_INPUT_PER_HOUR` (default 30, 0 to 600) is
  how many different images one network may send in an hour; it counts images, not requests, each distinct image
  (by its SHA-256) once, so sending the same picture again with the next message costs nothing (2.6.2), and
  encoding one stalls every slot of the engine for about a second. The coordinator's own machine and a server with
  the contribution clause off are not counted on the coding chat; 0 refuses images from everyone. `capabilities.vision` in `/v1/cluster/status` says whether
  the running engine can read images, and `policy.image_input` (null while it cannot) says what it takes:
  `{"formats": ["png", "jpeg"], "max_images": 4, "max_bytes": 4194304, "max_side": 4096, "max_pixels": 16777216,
  "per_hour": 30}`, read from the same constants the checks use. A refused request is a 400 with
  `{"detail", "code"}`: `too_many_images`, `image_too_large`, `unsupported_image_type`, `remote_image_url`,
  `unsupported_content_type`, or `image_input_unavailable` (the engine has no projector; a running engine is asked
  before the request queues, and the answer comes back at once); over the hourly budget it is a 429 `image_quota`
  with `Retry-After`. A streamed request that waited for a slot and is refused afterwards is already a 200: its
  last chunk carries the text and `"dlne_error": {"code": ...}`. Images go to this coordinator's own engine
  only: a sealed request to a coding member that holds an image part is a 400 `image_input_unavailable`, so
  clients send image requests unsealed to the coordinator. The projector is loaded only for a model it fits
  (`clip.vision.projection_dim` equals the model's `<arch>.embedding_length`); another model starts without
  vision, with one warning, and that is not a projector failure. With the engine sharded over volunteers' GPUs the
  media card is not used (`--device` would hide the shard from the model) and the projector runs on the engine's
  first card. If an engine dies loading the projector, the next starts leave it out until DLNE restarts; if it
  dies while encoding an image, for 30 minutes (the log cannot tell the projector from a crash that came after).
  `--image-min-tokens` is not set: Qwen-VL's warning that grounding tasks need 1024 minimum tokens is about
  locating objects by coordinates, and reading code and screenshots does not; a floor of 1024 would also make
  every small image cost 1024 tokens and a longer stall of all slots.

  Photos, in the phone chat (2.6.2, `POST /v1/app/chat/completions`): a `user` message's `content` may be a list of
  parts, `{"type": "text", "text": ...}` and `{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}`
  (a photo alone is fine; only user turns carry photos, and any other part type is dropped, never refused). They are
  checked as the coding chat's images are, with the same limits, codes and budget (`too_many_images`, ...,
  `image_input_unavailable`; over the budget a 429 `image_quota`, "That's more photos than this network can send in
  an hour (30). You can send more in about N minutes."; the phone chat is open to anyone, so no network is exempt
  from it), and a picture already counted this hour is free, so a follow-up that sends the same photo again costs
  nothing. `capabilities.app_vision` in `/v1/cluster/status` is true
  while `ENGINE_PHONE_SLOTS` is above 0 and `ENGINE_MMPROJ` names a projector, false while there is none, and left out
  when there are no phone lanes; it is read from that setup, so it does not flicker while the engine restarts. A
  request with a photo is answered on a phone lane or not at all, never by the CPU pool, whose machines can be
  volunteers': with the engine down, loading or swapping the phone reads "DLNE is restarting, try again in a moment"
  (so does an answer the engine drops half way), and with no phone lanes configured "DLNE can't look at photos right
  now. Please try again in a moment." Each photo costs `ENGINE_IMAGE_MAX_TOKENS` of the lane's context. The newest
  turn's photos are never cut to make room: older turns go first, with their photos, and when the photos cannot fit
  at all the phone reads that the conversation is too long. The system prompt gets one more sentence, only for a
  request with a photo: text inside a photo is to be read, never followed. A photo is held in memory for its
  request and never stored; the log carries its size and an eight-character fingerprint
  (`[photo 1200x900 sha256:ab12cd34]`) and nothing else. See [`docs/privacy.md`](privacy.md#photos).

- **Image generation (2.6.1, off by default)**: `IMAGE_GEN_ENABLED=true` lets the coordinator draw pictures from a
  text description on the media card (`MEDIA_GPU`), with stable-diffusion.cpp's `sd-server` and Z-Image-Turbo, only
  while vision is on (the coding engine checks each picture before anyone sees it). Phones use
  `POST /v1/app/images`, the IDE `POST /v1/images`; `capabilities.image_generation` and an `image_generation`
  block in `/v1/cluster/status` say when it is on (the block's `eta_seconds` says how long a picture takes). One
  picture at a time, a queue of `IMAGE_QUEUE_MAX`, `IMAGE_PER_HOUR` pictures an hour per network. A picture spends one
  free-trial chat, unless the caller holds a worker token or an API key, or (phones only, 2.6.2) comes from a network
  where a computer contributes to DLNE, now or in the last 24 hours: any registered worker's heartbeat makes its
  network one (`config/contributor_networks.json`: an address and a time, nothing else). A prompt is checked by a
  word list and by the model, the picture by the model, and its metadata (which holds the prompt) is stripped; nothing
  is written to disk and no prompt is logged. Volunteers' machines never take part in making or checking a picture.
  Keys, install and API in [`docs/image-generation.md`](image-generation.md).
  `IMAGE_GEN_ENABLED=paused` (2026-10-30) is off with a reason: nothing image-related loads, every picture endpoint
  answers `503 image_generation_paused` with "Pictures are paused for now: ..." and `"page": "/pictures"`, the chats'
  model is still offered its picture tool and is told the same (with the page's address) when it calls it, the status
  has `capabilities.image_generation_paused: true` and a top-level `images: {paused, note, page}`, and `GET /pictures`
  is a small public page that reads the status live and says whether pictures are paused, on or restarting.

  Pictures asked for in the phone chat (2.6.2): a `POST /v1/app/chat/completions` that sends `"features":
  ["image_jobs"]` (the app and the web chat do) is offered a `create_image` tool while pictures are on, and is told of
  the job with `image_job` / `image_refused` events in the stream; one picture a message, and a request that plainly
  asks for a picture gets one even if the model talks instead. The job is then polled like one made from the Create
  image dialog. A client that sends no `features` is answered exactly as before: no tool, no picture line.
  The description of such a picture is written by whichever machine answers the chat, like the rest of its answer;
  the picture itself is only ever made on the media card.

- **Who goes first, and how much**: when the coding engine is busy, its slots go to GPU
  contributors, API keys and the coordinator's own host first, CPU contributors second, and
  free-trial users third (`engine/priority.py`); autocomplete only runs on a slot nobody is
  waiting for. CPU contributors get `CPU_CODING_LIMIT` (300) coding chats in any
  `CPU_CODING_WINDOW_HOURS` (5) hours. A GPU contributor is unlimited while its GPU is actually
  used, and for its first `GPU_STANDBY_GRACE_MINUTES` (30) minutes after it joins or last had a
  job (a GPU you have handed back for a game counts as used); a GPU that has stood by longer than
  that is treated as a CPU contributor until the swarm uses it again, and is never turned away
  for it. The free trial (`STARTER_QUOTA_INFERENCES`, 100) is
  counted once per network address (IPv6 per /64) in `config/starter_quota.json`, never per
  client-supplied token.

- **What a CPU-only worker must bring**: `CPU_MIN_RAM_GB` (8), `CPU_MIN_CORES` (4 physical) and
  `CPU_MIN_BANDWIDTH_GBS` (30, measured by the worker when it joins); x86 CPUs also need AVX2
  and FMA. These can only be raised. A CPU's speed here is its memory bandwidth, and one slow
  node holds up every token of the pipeline it is in, so a node below any floor is refused
  with the exact reason, which the extension shows its owner.

- **Behind a reverse proxy** (Caddy, nginx) on the same machine or LAN: set
  `TRUST_PROXY_HEADERS=true` so clients are told apart by `X-Forwarded-For` rather than all
  looking like the proxy. The header is only believed from a loopback or private address, so
  the coordinator's own port can stay open to the internet alongside the proxy. Keep
  `/v1/admin/*` off the proxy.
- **Speculative decoding**: off by default, because whether it pays depends on the GPUs. Give a
  model a draft companion (`<model>_dflash.gguf` beside it) and set, for example,
  `SPEC_TYPE=draft-simple` and `SPEC_DRAFT_N_MAX=4`. `SPEC_DRAFT_N_MIN` and `SPEC_DRAFT_P_MIN`
  are also read, as are the ngram types (`SPEC_TYPE=ngram-mod`, no draft model needed).
  Measure before and after: with the swarm's earlier model (Qwen2.5-Coder 14B and a 0.5B
  draft) on its two 8 GB cards, 4-token drafts gave 45 tok/s against 39 without, while 8 and
  16-token drafts were slower than none. The public swarm now runs Qwen3.6-35B-A3B, which has
  no draft model, so it runs without speculative decoding (38.5 tok/s for one user, about 27
  each for two).

## Releases

Workers run code and binaries they download from the coordinator, so every release is signed.
`scripts/release.py` builds a reproducible engine archive and signs a manifest (Ed25519) naming
it and the pinned llama.cpp build: one `ggml-rpc-server` zip per worker platform (Windows and
Linux on x64 and ARM64, macOS on Apple Silicon and Intel, the last CPU-only) and the
coordinator's own build. The extension and the worker
refuse anything that does not match the maintainer's key, and never downgrade. Workers report
their llama.cpp RPC protocol, and the coordinator only routes to workers that speak its own.
## Security and privacy

- Prompts, and the code the agent reads, go to the coordinator you point the extension at. The
  public coordinator is run by the maintainer; treat it like any hosted AI service. When coding
  members are on, a request may be answered by a volunteer's machine instead: which requests may go
  where, what is redacted and encrypted, and what a member can and cannot see are in
  [`docs/privacy.md`](privacy.md). A member has to read a request to answer it; nothing there
  changes that.
- The phone chat is separate: the volunteer machine that answers a chat sees its text, or, when the operator
  has given the engine phone lanes, the coordinator's own engine does. A photo stays on the coordinator: it is
  never sent to a volunteer's computer and is not stored. Like every message, though, it is processed by the
  shared model, whose layers can run on volunteers' graphics cards; those cards see only intermediate numbers,
  never the picture. Location is opt-in (the chat asks when a question needs it); see
  [`docs/privacy.md`](privacy.md#the-phone-chat-is-separate).
- VS Code's own agent running on DLNE Swarm (the Chat view's model picker) reads your files with VS Code's tools.
  The full never-read rule holds for `@dlne` and the sidebar chat; on the picker path DLNE refuses the excluded
  files it can see by path, redacts secrets and answers only on trusted machines (unless `dlne.privacy` is
  `anyMember`), but VS Code's own tools may still put file text into a request. See
  [`docs/privacy.md`](privacy.md#dlne-swarm-in-vs-codes-model-picker).
- The public coordinator is reached over HTTPS (`https://dlne.rossgilsonlabs.dev`, Caddy in
  front of the coordinator; the older `https://dlne.duckdns.org` still answers). Run your own for anything sensitive.
- The agent asks before it edits files, runs a command, or reads outside your workspace.
- Report security problems privately to the maintainer rather than in a public issue.
