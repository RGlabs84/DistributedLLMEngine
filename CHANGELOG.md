# DLNE server changelog

Changes to the DLNE coordinator and worker engine (`engine/`, `dlne_server.py`), released as one signed
version that every node runs. The VS Code extension keeps its own log in
[vscode-extension/CHANGELOG.md](vscode-extension/CHANGELOG.md); the older entries that used to be here were
the extension's and are there.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
