# Coding members: how they work

Coding members let a volunteer's GPU run a whole coding model and answer coding requests on its own, the
way phone-pool (CPU) members already work. One request touches one machine, so home internet is fine, and
each member adds about two agent slots. This document is the specification: the names, formats and
behaviour the coordinator, the worker and the extension share.

Everything here ships **off by default** (`CODING_MEMBERS_ENABLED=false`) until the owner turns it on.

## 1. Roles

- **Coding member**: a worker whose signed heartbeat says `"pool": "coding"`. It runs one llama-server
  with a whole coding model, behind a small **member proxy** that listens on the worker's RPC port and is
  reached by the coordinator through the existing reverse tunnel (`reverse_bridge.get_relay_port(ip,
  port)`, exactly as phone-pool members are; see `AppPool.members` and `_app_members()`).
- **Local engine**: TheWall's own coding engine (today's path). Always trusted. The fallback when no
  member fits.
- **Trusted member**: a member whose **trust id** is listed in the coordinator's `TRUSTED_MEMBERS`
  config (comma-separated trust ids). Trust is set by the coordinator, never claimed by a member.
  Worker ids cannot carry trust: heartbeats are signed with the network key every extension ships, so
  anyone can claim any worker id. A trust id is instead derived from the member's own identity key
  (section 2): `trust_id = first 32 hex characters of sha256(raw 32-byte Ed25519 identity public key)`.
  The operator lists the trust ids of their own machines; a member shows its trust id in its log and UI.
- **"Both"** (a volunteer's choice, `dlne.worker.device = "both"`): a machine with a usable GPU whose CPU
  also serves the phone pool while the GPU does whatever job the coordinator gives it (an RPC node or a
  coding member). Every node still has exactly one job: the CPU is a second node of the same process, the
  **companion** (`engine/companion.py`), registered as `<worker id>-cpu` with `"companion_of": "<worker id>"`
  in its signed hardware profile, `"pool": "app"`, `gpu: null`, `ram_gb` = the RAM it lends
  (`ALSO_CPU_RAM_GB`, written by `dlne_server.py worker --also-cpu-ram`; absent or 0 means off), its own port
  (50053) and tunnel. A coordinator that has never heard of companions sees two ordinary nodes. A coordinator
  that has counts the machine once: `registry.count()` and the node totals skip hardware with `companion_of`
  (VRAM and RAM totals already skip pool members), and the app pool's member count includes the companion.
  Sharing the machine: `MAX_CPU_THREADS` (or the physical cores) is the total, the phone pool gets what it
  would run on alone but at most half of it and the coding model the rest; the coding model loads only after
  the pool model has loaded (or given up), so the two never read the free RAM at the same moment; when the
  owner games (`is_yielding`) both halves step aside: the GPU half as any GPU node does, and the phone-pool half
  (since 2.4.1) reports `ready: false` at once, gives the answers in flight 10 s, stops its model, and starts it
  again 60 s after the load ends. A machine that lends only its CPU is unchanged. `MAX_RAM_GB`, `CODING_MAX_RAM_GB`
  and everything the GPU node reports are those of a GPU node alone. The worker prints `DLNE_ALSO: app` (and
  `DLNE_ALSO: none` when the CPU half stops) next to `DLNE_ROLE`, which stays the GPU's job. Without a usable
  GPU, with `MAX_CPU_THREADS` under 2, or on the coordinator's own hybrid worker, "both" does nothing
  (a machine without a GPU is the phone pool's member already).
- **Light mode** (a volunteer's CPU-only phone-pool member, since 2.4.2): when the owner games (`is_yielding`) the
  member does not stop, it runs lighter, driven by the same probe through `AppMember.set_light`. At once, with no
  restart, its llama-server drops to the lowest priority (niceness 19; `IDLE_PRIORITY_CLASS` on Windows) and to half
  the CPUs it may use (at least one; no affinity on macOS), and the member reports `slots: 1`. After a 10 s drain
  for the answers in flight it is restarted in light mode: half the threads (at least one), `-np 1` instead of 2,
  half the context, `--cache-ram 0`, lowest priority from the start. When the load has been over for 60 s it is
  restarted at full strength (a member that was only lowered has its priority and CPUs put back in place; where the
  system cannot raise a priority again, as on Linux, it is restarted instead). There is at most one switch per 60 s,
  so a load that flickers cannot cause a restart loop. While light the member stays `ready: true` and its profile
  still says `is_yielding: true`; the coordinator keeps a yielding pool member in the routing while its own status
  says it is ready (`registry.app_members`), one slot at a time, and leaves out one that stepped aside (not ready).
  `app.threads` reports the threads it really runs on (half while light). The companion of a "both"
  machine keeps the full step-aside above, the coordinator host's own member (`LocalAppServer`) never lightens, and a
  member whose owner never games is not touched.

## 2. Member heartbeat (inside the signed hardware profile)

```
"pool": "coding",
"coding": {
  "model": "Qwen3.6-35B-A3B-UD-IQ3_S.gguf",   // file being served
  "tier": "qwen36-a3b",                        // coding ladder tier id (engine/coding_ladder.py)
  "ready": true,                               // llama-server answers /health
  "slots": 2,                                  // parallel requests it accepts
  "busy": 0,                                   // requests in flight right now
  "decode_tps": 32.5,                          // join test: generation speed, one user
  "prefill_tps": 121.2,                        // join test: prompt reading speed
  "pubkey": "<base64 raw 32-byte X25519 public key>",   // for end-to-end encryption (section 6)
  "no_trace": true,                            // the member runs with the no-trace settings (section 8)
  "identity": "<base64 raw 32-byte Ed25519 public key>", // long-term identity key, kept on the member's disk
  "signed_at": 1793000000,                     // unix seconds when the block was signed
  "sig": "<base64 Ed25519 signature>"          // by the identity key, over the canonical block (below)
}
```

Validation on the coordinator: strings ≤ 128 chars, numbers finite and in sane ranges,
unknown keys refused, like the `app` block. A member below the speed floors is not routed to.

**Identity signature.** The signed bytes are the coding block
without `sig`, as canonical JSON (`json.dumps(block, sort_keys=True, separators=(",", ":"),
ensure_ascii=False).encode()`). The member makes its identity key once, stores it in its config folder
readable by the owner only (0600), signs every heartbeat's block, and never sends the private key
anywhere. The coordinator drops a coding block whose signature does not verify or whose `signed_at` is
more than 300 s from its clock, and only then computes the member's `trust_id` (section 1). Because
`pubkey` is inside the signed block, a machine that copies a trusted member's block still cannot open
requests sealed to that member. The router's `CodingMember` carries `trust_id` (set only after the check)
and `privacy_policy.allowed()` reads `trust_id`, never `worker_id`.

## 3. Choosing a member's model, and the join test

`engine/coding_member_select.py`. The rule: run the **smartest** enabled ladder tier this one machine can hold whole **and
is estimated to run fast enough**; step down only when the estimate, or later the join test, says the
floors would be missed. Ladder order is quality order (smallest and least smart first).
- `choose_tier(vram_gb: float, ram_gb: float, tiers: list, limits=None, **machine) -> Optional[dict]` — the
  ladder's own dict, or None (not a coding member). Units are GiB, as `hardware.py` reports them; pass the
  memory the machine can really spare (total RAM, less what the owner's other programs hold if known): the
  RAM-resident part of the model is mmap'd, and a laptop that has to evict it between tokens measured 24
  tok/s decode and 141 tok/s prefill instead of 29 and 340 with the same model. A tier
  **fits** when GPU memory + (system RAM − a 6 GB OS reserve) holds its file, the KV cache for its
  `context` and ~2 GB of overhead, and at least 8 GB (7.5 reported) of GPU memory exists; llama.cpp's
  `--fit on` puts what does not fit on the card into RAM. Among the tiers that fit, a **speed estimate**
  for this machine (decode: bytes read per token against GPU and RAM bandwidth and the share of the
  weights on the GPU; prefill: GPU compute, plus CPU compute for the experts left in RAM, or the PCIe copy
  for a dense model's RAM-resident layers; dense versus MoE from the active parameters) must clear the
  floors by `CODING_MEMBER_ESTIMATE_MARGIN` (default 1.2). Calibrated on the owner's laptop: IQ3_S 32.5
  decode / 121 prefill measured, 32.0 / 119.9 estimated; dense 14B prefill 460 measured, 436 estimated.
  Optional `**machine`: `unified=True` for Apple Silicon and Strix Halo (one memory pool: `ram_gb` is the
  pool, `vram_gb` what the GPU may use, the two are never added), `gpu_count`, and `gpu_bw_gbps`,
  `gpu_gflops`, `ram_bw_gbps` when known (otherwise conservative figures per card size; the hardware
  probe's `mem_bandwidth_gbs` is a measured `ram_bw_gbps`).
- `candidate_tiers(vram_gb, ram_gb, tiers, limits=None, **machine) -> list[dict]` — the tiers to try, best
  first (`choose_tier` is its first element). **Step-down protocol for the member runtime:** download and
  run the first, join-test it (`measure`, `meets_floor`); on a miss take the next (`next_tier_down(failed,
  vram_gb, ram_gb, tiers, ...)`), at most `MAX_STEP_DOWNS` = 2 times, then give up. If no tier is
  estimated to clear the margin but the smallest is within 75% of the floors, that one is the only
  candidate and the join test decides. `assess_tiers(...)` returns every tier's estimate (fit, verdict,
  GPU share, estimated tok/s) for logs and status.
- `meets_floor(decode_tps: float, prefill_tps: float, limits: dict = None) -> bool` — floors from
  `engine.config.get_worker_limits()` (None reads it): `CODING_MEMBER_MIN_DECODE_TPS` (15) and
  `CODING_MEMBER_MIN_PREFILL_TPS` (80), as `limits["coding_member_min_decode_tps"]` /
  `["coding_member_min_prefill_tps"]`. Other settings: `CODING_MEMBER_RAM_RESERVE_GB` (6),
  `CODING_MEMBER_ESTIMATE_MARGIN` (1.2). A missing, NaN or non-positive speed is a miss.
- `measure(base_url: str, client) -> (decode_tps, prefill_tps)` — the join test against the member's own
  llama-server (`client` is an `httpx.Client`): a tiny warm-up request, then one `/v1/chat/completions`
  request with a ~2,000-token prompt and `max_tokens` 128 (thinking off, no prompt cache, EOS ignored),
  both speeds read from llama.cpp's `timings` of that request. At most 60 s in all (a member exactly at the
  floors needs about 34 s). Any failure (down, HTTP error, timeout, no or implausible timings, a prompt
  answered from the cache) returns `(0.0, 0.0)`, which `meets_floor` refuses. `run_join_test(...)` returns
  the same with the reason.
- The ladder has no parameter counts or KV sizes (the signed manifest carries a fixed set of fields), so the
  module knows the shipped model families by name (`FAMILIES`). A new family needs a row there, or the
  tier may carry `params_b`, `active_params_b`, `kv_gb_per_64k`, `recurrent_gb`, `cpu_resident_gb`, which
  override the table (they must first be allowed by `engine/release.py` and signed); a model in neither is
  read from its name ("30B-A3B" is a MoE with 3B active, "27B" is dense) with a deliberately large KV.

## 4. Member runtime

`engine/coding_member.py` (worker side; modelled on `engine/app_member.py`):
- downloads and verifies the chosen tier's files (sha256 + size; split models: every part, using the
  downloader's API, section 9), starts llama-server on loopback with `--fit on`, the coding context, and
  the no-trace arguments (section 8); restarts with backoff; low priority; stops cleanly.
- starts it with `--alias <the model file's name>` (`engine/model_alias.py`, the same for the coordinator's
  engine and the phone-pool members), because llama-server otherwise puts the path it loaded the model from
  (`/home/<user>/...`) in the `model` field of every answer, which the requester opens. Nothing reads that
  field: clients name the model from the status.
- runs the **member proxy** on the worker's RPC port: `POST /v1/chat/completions` and
  `POST /v1/completions`, plain JSON or an end-to-end envelope (section 6), streamed to loopback
  llama-server and back; `GET /health`. The proxy never logs request or response bodies. (The
  coordinator only ever sends it envelopes, section 5.) It answers 421 to any request with an `Origin`
  header or a non-loopback `Host`, so a web page in the owner's browser cannot use it (SR-08).
- `status()` returns the `coding` block of section 2. `engine/worker.py` adds it to the heartbeat the
  same way it adds the phone-pool `app` block.
- A worker is either a GPU RPC worker, a phone-pool member or a coding member, never two at once.
- **RAM.** The coding job has its own RAM cap, `CODING_MAX_RAM_GB` in `config.txt` (`-1`: none, so it counts
  only the RAM that is free when it starts). `MAX_RAM_GB` stays the limit of the GPU's other job (the RPC node)
  and of the phone pool, and a worker whose config lacks `CODING_MAX_RAM_GB` (an older extension) still reads
  `MAX_RAM_GB` for the coding job as well. The extension writes `-1` unless the user set `dlne.worker.maxRamGb`.

## 5. Routing

`engine/coding_router.py` (coordinator side):
- `CodingRouter.members(registry, relay_lookup) -> List[CodingMember]` (ready, tunnel up, floors met).
- `pick(members, session_id, privacy, exclude) -> Optional[CodingMember]`: privacy filter first
  (section 7), then **sticky**: the member that served this session in the last 30 minutes if it is
  still eligible and not full; else the least busy (ties: higher `prefill_tps`, then `decode_tps`).
- Forwarding: only the body goes to the member. No client IP, no API key, no user identity, no
  coordinator headers: a member learns nothing about who asked (anonymity).
- Wiring in `engine/coordinator.py` (`chat_completions`, `completions`): when
  `CODING_MEMBERS_ENABLED` and the request is **sealed** (section 6) with an `X-DLNE-Route`, forward it
  to that route's member; every plain request takes today's local engine path, unchanged. Queue order
  (GPU contributors, CPU contributors, trial) applies at the local engine's gate. The public model rule
  stays: callers cannot choose the model.
- `GET /v1/coding/route?session=<id>&privacy=<mode>` → `{"route": "<opaque id>", "pubkey": "<b64>",
  "alg": "x25519-hkdf-sha256-chacha20poly1305"}` for a member, or `{"route": null}` for the local engine.
  The route id is opaque and short-lived (10 min); it is what the client sends back as `X-DLNE-Route`.
- Status: `"coding_members": {"enabled": bool, "members": n, "ready": n, "slots": n, "busy": n}` in
  `/v1/cluster/status` (counts only).

As built:
- **Sealed requests are never retried by the coordinator.** A missing, expired, gone, busy, failing or
  key-rotated route answers `503 {"error": {"code": "dlne_reroute", "reason":
  "expired|gone|busy|failed|privacy", "message": ...}}`, and `busy` adds `Retry-After: 3`.
  - A sealed body without `X-DLNE-Route` answers `400 dlne_route_required`.
  - A route id remembers the pubkey it was issued with.
  - The client caps its re-routes, then uses the local engine in plain.
  - A failing member is left out of new routes for 30 s.
- **Plain requests never go to a member** (security follow-up, a9d5d93): whatever headers a plain body
  carries, the coordinator's own engine answers it, so what a volunteer's machine is sent has always
  been encrypted on the way. There is no member queue (`CODING_MEMBER_QUEUE_SECONDS` is gone and
  ignored if still in a config); a request that has to wait waits at the local engine's gate.
- **Abuse limits:** free-trial callers' route lookups hold no member slots and are limited to 6 per
  30 s per client (429 with `Retry-After`; SR-09). A member's whole non-streamed answer is capped at
  8 MiB, and the whole exchange at 30 minutes, a stalled stream included (SR-10).
- **Restart grace:** a worker whose heartbeat already says `pool: coding` keeps its role after a
  coordinator restart; only an unknown machine waits out the 90 s probe grace.
- A member-served 200 carries `X-DLNE-Served: member`.
- **Roles:** a node is given `role: "coding"` only when it is a far GPU with at least 8 GB, has
  `coding_ok: true` in its hardware profile, and has a tier from `choose_tier`.
  - A member stepping aside (its owner is gaming) reports `ready: false, slots: 0` and is not routable.
  - A member may serve a smaller tier than the one assigned; routing follows what it reports.
- **Duplicate identities:** only the newest `signed_at` is believed, and neither on a tie.
- **Pool figures for the website** (`/v1/cluster/status`, counts only; nothing names a machine or a user):
  `"gpu_pool": {"gpus", "vram_gb", "gpus_standing_by", "vram_standing_by_gb", "tokens_per_second",
  "tokens_last_hour", "measured_tokens_per_second"}` and `"cpu_pool": {"machines", "machines_standing_by",
  "cores", "ram_gb", "tokens_per_second", "tokens_last_hour"}`.
  - A GPU is **serving** when it has a job: a node of the running engine's `--rpc` topology (this host's
    own GPUs are among them while its engine runs on them) or a ready coding member's. It is **standing
    by** when it is awake with none (or a verified member that is not ready). A node handed back to its
    owner (yielding) is in neither. GPUs per node are the profile's `gpu.count` (1 for an older worker);
    `vram_*` are the VRAM of exactly those GPUs. `total_vram_gb` keeps meaning the sharded admission pool
    and counts no member, phone-pool node or companion.
  - `cpu_pool.machines` are phone-pool members that are ready (the coordinator's own included),
    `machines_standing_by` those registered but not ready; `cores` are the threads they run the phone
    model on (`app.threads`; `default_threads` for an older worker) and `ram_gb` the RAM they lend.
  - `tokens_per_second` is tokens generated per second over the last 60 s, `tokens_last_hour` over 60
    min (`engine/throughput.py`, one-second buckets). Tokens are counted as they arrive: each streamed
    SSE frame carrying generated text is one token, a whole answer counts its `usage.completion_tokens`,
    a sealed member stream counts one per sealed frame (the sealed end marker and `[DONE]` are not
    tokens), and canaries and warm-ups (`max_tokens` 0) count nothing. A sealed whole answer cannot be
    read, so it counts nothing. `measured_tokens_per_second` is `coding_model.tokens_per_second`.
- **Owner perk:** an owner whose worker holds a GPU is tier `gpu` (first in line, no usage window) while
  that GPU is serving. A coding member is such a worker, so its owner needs no new link, and a member
  that is ready keeps the perk for as long as it is ready. A GPU that is registered but has no job
  (not in the running engine's topology, not a ready member) keeps the perk for
  `GPU_STANDBY_GRACE_MINUTES` (config.txt, default 30; 0 means none) from when the coordinator first saw
  it without one; after that it is tier `cpu` with the CPU rolling window, and is never refused for it
  (`cpu_node_shortfalls` is not applied to a GPU node). A job resets the clock: the next heartbeat or
  request that finds the GPU serving clears it. A member that yields to its owner's game counts as
  serving. The clock (one start time per worker id, `engine/standby.py`) survives a coordinator restart: it
  is kept in `config/gpu_standby.json`, written atomically at most every 30 s while it changes and at a clean
  shutdown, read once at start. Only workers with no job have an entry, and one not seen for 24 hours is
  forgotten (it starts again with a fresh grace when it returns). A missing, old or damaged file means no
  history and never stops the coordinator.

## 6. End-to-end encryption

Algorithm `x25519-hkdf-sha256-chacha20poly1305`, version 1:
- Client makes an ephemeral X25519 key pair; `shared = X25519(eph_priv, member_pub)`;
  `okm = HKDF-SHA256(ikm=shared, salt=eph_pub || member_pub, info="dlne-e2e-v1", length=64)`;
  `req_key = okm[0:32]`, `resp_key = okm[32:64]`.
- Request body sent instead of the JSON: `{"dlne_e2e": {"v": 1, "epk": "<b64 eph_pub>",
  "nonce": "<b64 12 random bytes>", "ct": "<b64 ChaCha20-Poly1305(req_key, nonce, json_bytes, aad=b"dlne-req")>"}}`.
  Header `X-DLNE-Route: <route id>` selects the member.
- Response: each upstream SSE `data:` line's payload (the bytes after `data: `) becomes
  `data: {"dlne_e2e": {"n": <counter from 0>, "ct": "<b64 ChaCha20-Poly1305(resp_key, nonce=counter as
  12-byte big-endian, payload, aad=b"dlne-resp")>"}}`; `data: [DONE]` stays plain. A non-streamed answer
  is one sealed payload with `n = 0`.
- Python: `engine/e2e.py` — `generate_keypair() -> (priv_bytes, pub_b64)`, `open_request(envelope: dict,
  priv: bytes) -> (body: dict, resp_key: bytes)`, `seal_payload(resp_key, n: int, payload: bytes) -> dict`.
  Uses `cryptography` (already in requirements.txt).
- TypeScript: `vscode-extension/src/privacy/e2e.ts` — `sealRequest(body: object, memberPubB64: string)
  -> {envelope, respKey}`, `openPayload(respKey, n, ctB64) -> Buffer`. Node `crypto` only.
- The local engine path (no member) stays plain JSON over HTTPS to the coordinator.

**Wire details:** `seal_request`/`sealRequest().envelope` return the whole
body `{"dlne_e2e": {...}}`; `open_request` accepts that or the bare inner object. `seal_payload` returns
only the inner `{"n", "ct"}`; the caller writes `data: ` + `{"dlne_e2e": <that>}`. Base64 is the standard
alphabet, padded and canonical; anything else is refused, so the heartbeat `pubkey` is published exactly
as `generate_keypair` returns it.

**Version 1 also includes:**
1. *Per-response salt.* A replayed envelope must not reuse the response key and nonces. The member's
   first sealed response object also carries `"rs": "<b64 16 random bytes>"`; every response chunk is
   sealed with `stream_key = HKDF-SHA256(ikm=resp_key, salt=rs, info="dlne-e2e-v1-resp", length=32)`
   instead of `resp_key`. Members still refuse an `epk` they have already seen (replay guard).
2. *Sealed end.* After the last token the member sends one more sealed chunk whose plaintext is the
   bytes `[DONE]`, then the plain `data: [DONE]`. The client treats a stream as complete only after the
   sealed end; a plain `[DONE]` or a closed connection without it is a cut stream and an error.
   A non-streamed answer is one sealed object (`n = 0`, with `rs`) and needs no end marker.
3. *Padding on.* Requests and response chunks are padded with trailing spaces to a multiple of 256
   bytes before sealing, so token lengths do not show. Decrypted bytes are parsed as JSON (trailing
   spaces are allowed) or, for the end marker, trimmed.
4. *Key rotation.* A member makes a fresh key pair when it starts and every 6 hours, advertises the
   current public key in its heartbeat, and keeps the previous private key for 15 minutes so routes
   issued just before a rotation still open.
5. *Use the helpers.* Integrators never count nonces by hand:
   - Python: `KeyRing()` (`.public_b64`, `.rotate()`, `.maybe_rotate()`,
     `.open_request(envelope) -> (body, resp_key)`, current key then previous, replay guard on);
     `ResponseSealer(resp_key)` (`.seal(payload: bytes) -> dict`, the first one carrying `rs`;
     `.finish() -> dict`, the sealed end); `ResponseOpener(resp_key)` (`.open(sealed: dict) ->
     Optional[bytes]`, `None` for the sealed end; `.finished`).
   - TypeScript: `sealRequest(body, memberPubB64)` pads by default; `new ResponseOpener(respKey)`
     (`.open(sealed) -> Buffer | null`, `null` for the sealed end; `.finished`); `ResponseSealer` and
     `openRequest` mirror Python for tests and fake members.
6. *Single use.* An envelope is sealed to one member's key and refused if sent twice. If a member fails
   or is busy, the client asks `/v1/coding/route` again and seals again; the coordinator never retries a
   sealed body on another member.
7. *In the clear.* Error and busy answers (non-200) are not sealed; they carry no request content.
8. *Canaries are sealed* exactly like real requests (same padding, same path), so a member cannot tell
   them apart (section 8).

**What end-to-end encryption does and does not do.** The member has to read the request to run the
model, so it sees the code it is sent; that is what privacy modes (section 7), redaction (section 11),
no-trace and canaries (section 8) are for. Encryption keeps the request and the answer unreadable on the
way: on TheWall (it relays bytes it cannot open, and nothing about them reaches its logs or disk) and on
the tunnel and the internet between TheWall and the member. The member's public key comes from the
coordinator, so the coordinator (the operator's own machine) is trusted not to swap it; user-facing
text must say exactly this and must not claim the member cannot see the request.

## 7. Privacy policy

- Extension computes a mode per workspace: `open` (the workspace's git remote is a public repository),
  `trusted` (default for everything else), `any` (the user opted in with `dlne.privacy = "anyMember"`).
  Setting `dlne.privacy`: `"auto"` (default: open for public repos, else trusted) | `"trustedOnly"` |
  `"anyMember"`. Sent as header `X-DLNE-Privacy: open|trusted|any` and as `privacy=` on `/v1/coding/route`.
- `vscode-extension/src/privacy/policy.ts` — `getPrivacyMode(workspaceFolder) -> Promise<'open'|'trusted'|'any'>`
  (public-repo check: remote URL host github.com/gitlab.com/codeberg.org and an unauthenticated API
  lookup cached per remote, "public" 1 h and "not public" 24 h; any failure → `trusted`). It never throws. The folder is a
  `WorkspaceFolder`, a Uri or a path; without one, the workspace's only folder (several folders → `trusted`).
  - `anyMember` → `any` and `trustedOnly` → `trusted`, with no lookup. A value that is not one of the three
    → `trusted`. `auto` (or unset) → the public-repo check. The setting has `scope: resource`, but only the
    USER's value can widen it: `effectivePrivacySetting(config)` takes the user's value (default `auto`) and a
    workspace or folder value (`config.inspect()`) can only narrow it (`trustedOnly < auto < anyMember`; an
    unknown value → `trusted`). A cloned repo's `.vscode/settings.json` cannot widen it, trusted or not.
    Review SR-05: every other setting that decides where code or keys go (`dlne.serverUrl`, `dlne.apiKey`,
    `dlne.autoApproveReads`, `dlne.autoUpdate`, `dlne.worker.*`) has `scope: application` (`pythonPath`:
    `machine`), so a folder's settings are not read for them.
  - The remote is read from the folder's own `.git/config` (a linked checkout's or submodule's `.git` file is followed;
    a parent folder's repository is not adopted). Exactly one `remote.*.url`; a `pushurl` that names another
    repository, an `include`, or anything the reader does not follow → `trusted`.
  - Recognised: `https`/`http`/`git`/`ssh` URLs and `git@host:owner/repo` on exactly github.com, gitlab.com
    and codeberg.org; `owner/repo` (GitLab: subgroups allowed). Embedded credentials are dropped, never kept.
    Enterprise hosts, look-alikes, odd ports and anything else → `trusted`.
  - The lookup is one `GET` with no credentials, cookies or DLNE headers: `api.github.com/repos/{o}/{r}`
    (`private: false`), `gitlab.com/api/v4/projects/{path}` (`visibility: "public"`),
    `codeberg.org/api/v1/repos/{o}/{r}` (`private: false`). The answer must name this repository. 404 means
    not public; 403/429, a redirect, a server error, a timeout (3 s) or an odd body means unknown → `trusted`.
  - Cache, in memory, per remote (case-insensitive): "public" 1 h, "not public" (private, or a 404) 24 h, a
    failure 5 min; requests that arrive together share one lookup.
  - `package.json` declares `capabilities.untrustedWorkspaces` with `supported: false`: DLNE does not run in
    Restricted Mode at all (see the design decisions below).
  - `createPrivacyStatus()` is a small status bar item (`Public` / `Trusted` / `Any machine`, click opens the
    setting; the `Public` tooltip says unpushed changes count as public and points to `trustedOnly`).
    `extension.ts` registers it, and shows it, and does the lookup, only when `coding_members.enabled`.
- `engine/privacy_policy.py` — `allowed(member, mode: str, trusted_ids: set) -> bool`: `open`/`any` → any
  member; `trusted` → only members whose trust id is in `trusted_ids`; unknown mode → `trusted`.
  - Only the exact strings `open`, `trusted` and `any` are modes (`"Open"`, `" open"`, `""`, `None` → `trusted`).
  - `member` is read for its **trust id** (section 1; set by the router only after the identity signature
    checks out): the id string itself, a mapping with `trust_id`, or an object with a `trust_id` attribute.
    No trust id → not trusted. A `worker_id` is never read (worker ids can be claimed by anyone).
  - `trusted_ids` may be any collection of trust ids, or the comma-separated `TRUSTED_MEMBERS` string (never
    matched by substring). Helpers: `normalize_mode(mode) -> str`, `parse_trusted_ids(value) -> frozenset`,
    `trust_id_of(member) -> Optional[str]`.
- The coordinator never upgrades a mode; a missing header means `trusted`.
- Design decisions:
  - The public-repo cache keeps a "public" answer 1 h (a repo made private stops being `open` quickly) and a
    "not public" answer 24 h (safe either way).
  - DLNE declares `capabilities.untrustedWorkspaces.supported: false`, so it waits until the user trusts the
    workspace; and after it, a folder's settings still cannot widen `dlne.privacy` (above). (`"limited"`
    with `restrictedConfigurations` was tried and dropped: it would let the agent, which reads files and runs
    tools and never checks `workspace.isTrusted`, run inside untrusted repos.)
  - The extension skips the public-repo lookup (and hides the status item) unless the coordinator's status
    says `coding_members.enabled`; with members off nothing about the repo leaves the machine.
  - `open` covers the working copy, including unpushed changes in a clone of a public repo. User-facing
    text says so and points to `trustedOnly` for unpublished work.

## 8. No-trace members, anonymity and canaries

- `engine/no_trace.py` — `LLAMA_NO_TRACE_ARGS: list` (llama-server arguments that keep prompts out of
  logs and off disk: low verbosity, no slot save path, no prompt logging) and
  `forward_headers(headers) -> dict` (the only headers a member may receive: content type and length).
- `engine/canary.py` — the coordinator occasionally (config `CODING_CANARY_PER_DAY`, default 4 per
  member) sends a member a normal-looking coding request that embeds a unique decoy bound to that member:
  a canary URL `https://dlne.rossgilsonlabs.dev/c/<token>`. If the URL is ever fetched, the member that saw it is
  flagged and stops receiving traffic; the operator sees it in an admin endpoint. Decoys are never sent to
  trusted members' real users and never to the local engine.
  **Review SR-01 to SR-03: a canary is a request the extension could have sent.** Its system prompt, tools
  and fields are those of `vscode-extension/src/agent/agentLoop.ts`, `tools.ts` and `client/dlneClient.ts`
  (`tests/test_security_review.py` compares them with the source), and it has no decoy key in any prompt: the
  extension's redactor replaces every DLNE-format key and every `*_API_KEY = "..."` value with `[REDACTED]`
  before sealing, so a live `dlne_ck_` key could only have come from the coordinator. Where a canary file
  would hold a key it shows `[REDACTED]`. Each decoy still has a `dlne_ck_` key and presenting it still
  flags its member (a tripwire for a member that makes one up from the source), but it is in no prompt.

**As built:**
- `LLAMA_NO_TRACE_ARGS = ["--log-disable", "--log-verbosity", "0", "--no-slots", "--no-webui"]`, all four
  checked against llama.cpp b11838's `--help`. `--log-disable` pauses the logger (nothing is printed, at
  any level; at debug level the server logs whole prompts and every generated token, and even at error
  level a chat-parse failure quotes model output). `--no-slots` closes `/slots`, whose JSON carries each
  slot's full prompt and generated text. No `--slot-save-path`, `--log-file`, `--log-prompts-dir` or
  `--lookup-cache-dynamic` is ever passed. The prompt cache stays on (memory only; sticky routing needs it).
  Also exported: `no_trace_violations(args) -> list[str]` (what is wrong with a full llama-server argument
  list; use it to refuse to start and before claiming `"no_trace": true`) and `no_trace_env(env) -> dict`
  (the child process's environment without any `LLAMA_ARG_*` variable, which could switch logging or slot
  saving back on). A member's own software cannot hide memory from its owner: that is what canaries are for.
- `forward_headers(headers)` returns a dict with lower-case keys `content-type` and `content-length` only
  (values validated; accepts a dict, Starlette/httpx `Headers` or a list of pairs).
- Identity: decoys, flags, the store, the admin view and `is_flagged` are keyed on the member's **trust id**
  (sections 1 and 2), never on its worker id, which anyone can claim (renaming would dodge a flag; claiming
  another member's id would frame it). The worker id is kept only as a display label. A member without a
  verified trust id is never sent a canary. A flagged machine that makes a new identity key starts clean;
  that is inherent to open membership.
- Canary sealing and timing: a canary is sealed with `e2e.seal_request(body, member.pubkey)` (library
  default padding, compact JSON), handed to the router's forwarding callable with `forward_headers`, and its
  answer is read to the end with `ResponseOpener` (`.open(sealed)` per object, `.close()` at the plain `[DONE]`
  or the end of the data; a truncated answer is recorded as `answered: false`) and discarded. Bodies are
  agent-shaped (the extension's system prompt and tools, sometimes 1 to 3 earlier turns, a config-style file
  with the decoy link and `[REDACTED]` where a key would be, `max_tokens` 4096) and vary in size. Members with no valid `pubkey` are never sent one
  (plain JSON would be a giveaway).
- When: only to a member that served routed traffic in the last 2 hours (`last_served_at`, epoch seconds), so
  a canary never arrives at an hour when nothing else does. `CODING_CANARY_PER_DAY` (default 4, 0 = off) is a
  ceiling for any rolling 24 hours, not a quota; while a member is in use the coin is weighted as if it were
  used about 12 hours a day. Never two within 15 minutes. Without `last_served_at` on the members no canary is
  ever sent (it logs a warning once).
- Router contract for canaries: `CanaryScheduler(members, forward)` where `members()` returns the current
  coding members (objects or dicts with `trust_id`, `pubkey`, `last_served_at`, and optionally `worker_id`,
  `ready`, `busy`, `slots`, `local`), and `await forward(member, body: bytes, headers: dict)` (`member` is the
  object `members()` returned) sends the sealed bytes exactly as routed traffic is sent and returns the answer
  (bytes/text/iterable/async iterable of event-stream chunks; raise on a non-200). The router asks
  `engine.canary.is_flagged(member_or_trust_id)` and gives a flagged member no traffic. Start it with
  `asyncio.create_task(scheduler.run())`.
- Detection: `/c/{token}` (any method; answers 404 exactly like an unknown path) and any request that
  carries a `dlne_ck_` key (Authorization bearer, `x-api-key`, `api-key`, `?token=`, any path, websockets
  too) flag the member; the key is then treated as any other invalid key. State: `config/canaries.json`
  (digests only). Operator: `GET /v1/admin/canaries`, `DELETE /v1/admin/canaries/{trust_id}` (clear a flag).
  Trusted members are the trust ids in `TRUSTED_MEMBERS`.

## 9. Downloads

`engine/downloader.py` — `download_verified(files: list[{file, url, size, sha256}], dest_dir, progress=None,
**options) -> bool`: all parts, resumable, each part's size and sha256 checked, atomic rename, nothing left
half-written. Used by coding members and by auto-swap for split models. It blocks: run it on a thread.

- **Result.** `True` only when every part is in `dest_dir` and verified (parts that already were are
  skipped; a part is verified once and remembered in the `<file>.dlne-sha256` marker the CPU pool's
  `sha256_ok` also reads and writes). It never raises for a failed download. `fetch_verified(...)` takes the
  same arguments and returns a `DownloadResult(ok, reason, lasting, cancelled, failed_file, bytes_fetched,
  paths)`: `lasting` is True when trying again unchanged cannot help (bad manifest, no disk space, a hash that
  does not match, HTTP 404), False for network trouble, so a caller knows whether to back off or give up.
- **Manifest is untrusted.** A file name is a bare name (letters, digits and `._+=,@~()-`, at most 200
  characters, no folders, no `..`, not ending in `.part`, `.lock`, `.dlne-sha256`, `.tmp`), a URL is `https://`
  with a host and no password (redirects too: never to plain http), a size a positive integer, a hash 64 hex
  digits. One bad entry refuses the whole call before any network or disk use. All parts go in the one
  `dest_dir` (llama.cpp finds part 2 beside part 1).
- **Disk.** Fetched into `<file>.part` with an HTTP Range request from where the last attempt stopped, hashed
  while it streams (no second pass), renamed to `<file>` only at the signed size and hash. The real name
  never holds a partial or wrong file. A failure a later call can continue (network, cancelled, disk full)
  keeps the `.part` as the resume point; known-bad data (wrong hash or size) is deleted; `keep_partial=False`
  deletes it on every failure. Free space is checked up front for what is still to download plus a margin
  (`free_margin`, default 1 GiB or 2%).
- **Retries.** After a failure that made no net progress: `retries` (default 5) more tries, backoff 2 s
  doubling to 60 s with jitter, a server's `Retry-After` honoured up to 5 min, `timeout` 60 s of silence.
  4xx other than 408/425/429 fail at once.
- **Two callers.** One lock file per part (`<file>.lock`, an OS file lock the kernel drops if the holder
  dies). A second caller for the same part waits, then finds it verified and skips it.
- **Stopping.** Pass `cancel=threading.Event()`; setting it stops within one 1 MiB block and keeps the `.part`.
- **Progress.** `progress(update: dict)`, from the downloading thread, about twice a second plus on every
  phase change; an exception in it is ignored. Keys: `phase` (`checking`, `waiting`, `downloading`,
  `retrying`, `done`, `failed`), `file`, `index`, `count`, `file_done`, `file_size`, `done`, `total` (bytes),
  `speed` (bytes/s), and `error` / `retry_in` on `retrying`, `error` on `failed`.
- **Auto-swap.** A split tier is downloadable when every part is pinned (`file_is_pinned` and an https URL);
  the coordinator's `_download_coding_tier` hands several-file tiers to `download_verified` (single-file tiers
  keep the multi-stream path). The status line shows `Downloading <tier> (x of y GB, n files)`.

## 10. Extension integration

- `X-DLNE-Session: <random uuid>` per agent conversation (and one per editor session for autocomplete),
  sent on every chat, agent and autocomplete request. It is random, derived from nothing, and is the
  coordinator's key for sticky routing (section 5).
- Before each request: privacy mode (section 7), redaction (section 11), route lookup and sealing
  (sections 5 and 6) when the coordinator offers a member; plain request otherwise.
- Adapter `vscode-extension/src/privacy/index.ts` is the only place the client imports the
  privacy modules from.

**The pipeline** (`vscode-extension/src/client/pipeline.ts`, used by `streamChat` and `getCompletion`):
1. *Members on or off.* The client reads `coding_members.enabled` from `/v1/cluster/status` (the status bar
   polls it; a chat turn reuses a recent answer, autocomplete uses the last one polled). Not `true`, absent
   (an older coordinator), or unknown: the **plain path, exactly as before**: no privacy header, no
   public-repository lookup, no route lookup, nothing about the workspace leaves the machine. (Redaction and
   `X-DLNE-Session` still apply.)
2. *Redaction* (section 11) is applied to a copy of every outgoing body: message text, the arguments of
   earlier tool calls, and completion prompts. The agent keeps its real history; the same text always
   redacts to the same text, so the engine's prefix cache still matches. If the redactor throws, the text is
   replaced by a "withheld" notice, never sent. Files that `isExcluded` are not read for the model
   (`read_file` is refused before the user is asked, `search_codebase` skips them) and get no autocomplete.
3. *Privacy mode* (section 7): `X-DLNE-Privacy` on every request while members are on, and `privacy=` on the
   route lookup. A lookup that is slow (1.5 s), throws, or returns anything else reads as `trusted`.
4. *Route* (section 5): `GET /v1/coding/route?session=&privacy=` with the usual Authorization. Chat asks
   before every request; autocomplete reuses an answer for 20 s. 404/405/501 mean "older coordinator": not
   asked again for 10 minutes. Any other failure, `{route: null}`, or an offer the client cannot use (route id
   not `[A-Za-z0-9._~:-]{1,256}`, key not 32 bytes of base64, `alg` other than ours) is the plain path.
5. *Seal* (section 6): the redacted body is sealed with `sealRequest`; the request carries
   `X-DLNE-Route: <route id>` and the envelope as its body. **An envelope is sent once.** Every attempt
   (a retry after a socket error, a busy answer, a failed member) asks for a route again and seals again.
6. *Answer*: each `data:` line's `dlne_e2e` object goes to `new ResponseOpener(respKey).open(sealed)`; the
   answer is complete only at the sealed end (`opener.finished`). A chunk that fails to open ends the
   stream. A plain `[DONE]` or a closed connection before the sealed end is a **cut stream**: the user sees an
   error, the half answer is not recorded as a turn, and it is not retried once part of it was shown. An
   answer that arrives in the clear for a sealed request is refused. A non-streamed (autocomplete) answer is
   one sealed object (`n = 0`).
7. *Member failure*: HTTP 404/410/421/502/504 from a routed request, an answer that fails the checks above
   before anything was shown, or a socket error on autocomplete: ask for a route again and seal again, at most
   twice, then the coordinator's own engine in plain (always trusted). Error and busy answers are not
   sealed (section 6, item 7).

**The busy answer.** When every coding slot is taken a request waits in `engine/priority.py`'s queue. Today
the coordinator holds the connection and says nothing (the gate is awaited before the handler writes), so a
client could only guess. The client understands one object, `dlne_queue`, which the coordinator may send
through either of two channels, and works out an estimate itself from the third when it gets neither:

```
"dlne_queue": {
  "position": 3,        // 1 = next to get a slot            (optional)
  "waiting": 5,         // requests waiting, this one included (optional)
  "slots": 2,           // the coding engine's slots          (optional)
  "in_use": 2,          // slots taken                        (optional)
  "eta_seconds": 40,    // best guess, omit if unknown        (optional)
  "ticket": "opaque"    // [A-Za-z0-9._~-]{1,200}, 503 form only (optional)
}
```

1. **In the stream (recommended).** The coordinator answers `200 text/event-stream` at once and, while the
   request waits for its slot, sends `data: {"dlne_queue": {...}}` frames (when the numbers change, and at
   least every 5 s), then the model's chunks. No ticket is needed: the request holds its place by holding
   its connection. **As built:** frames go only to streamed requests that carry `X-DLNE-Session` (the
   DLNE extension), every 3 s. Every other caller waits silently as in 2.3.7, because a generic OpenAI
   client reads a chunk without `choices` as a broken answer, and an early 200 would turn engine errors
   into model text.
2. **As a refusal.** `503` (or `429`) with `Retry-After: <seconds>` and a JSON body
   `{"detail": "...", "dlne_queue": {...}}`. The client shows the place, waits `Retry-After` (1 to 15 s),
   and asks again with `X-DLNE-Ticket: <ticket>` so the coordinator can keep its place. A `503` or `429`
   **without** `dlne_queue` is unchanged (`503` = the model is loading; `429` = a CPU contributor's usage
   window). Gives up after 15 minutes with a message.
3. **Silence (what exists today).** After 2 s with no reply the client reads `coding_queue`
   (`{slots, in_use, waiting, waiting_by_priority}`, served by `/v1/cluster/status` already) every 4 s; when
   `in_use >= slots` and `waiting > 0` it shows an estimated place (those waiting at a better priority, plus
   those already waiting at its own), marked "about". Its own priority is a guess (key or GPU contributor 0,
   CPU contributor 1, trial 2). If the swarm is not busy it says it is waiting for the first words, with the
   seconds elapsed.

Shown as: a banner above the sidebar's input ("All 2 coding slots are busy. You are #3 in line." with the
count waiting and, for a non-contributor, that lending a GPU skips the line), the status bar item
("#3 in line"), and a progress line in the `@dlne` chat. Never a silent wait.

**Lending a GPU.** `dlne.worker.device` (auto / gpu / cpu) is unchanged. The first time a GPU owner clicks to
contribute (a click or a command; never when a worker resumes by itself or restarts after a version change)
a modal explains that the swarm chooses the job, names the two jobs, lists the privacy facts for a coding
member, says what the owner gets (first in line), and offers **Lend my GPU** or **Lend my CPU only** (sets
`dlne.worker.device` to `cpu`); closing it starts nothing. It is not shown again once answered.

**Which job the machine got.** The coordinator assigns it; the worker tells the extension on its own output,
the way it reports `DLNE_REFUSAL`:

```
DLNE_ROLE: rpc
DLNE_ROLE: app
DLNE_ROLE: coding {"model": "Qwen3.6-35B-A3B-UD-IQ3_S.gguf", "tier": "qwen36-a3b", "slots": 2, "trust_id": "<32 hex>"}
```

(`rpc` = a share of the one big model run on many GPUs; `coding` = a whole coding model answering other
people's requests; `app` = the phone app's CPU pool.) The JSON part is optional and each field is checked
(`trust_id` is the 32 lower-case hex characters of section 1). The last line printed wins, and a new one is
printed when the job changes. An engine that never prints one is a GPU pool member, as it always was.
`engine/worker.py` prints this line when the coordinator assigns the job, with `trust_id` once the identity
key exists. The sidebar card, the status bar tag ("Coding member"), the
tooltip and `@dlne /contribute` show the job, a coding member's privacy facts (they run other people's
requests; anonymous; end-to-end encrypted in transit; the machine keeps no trace; private-repository code
goes only to trusted machines; decoy-credential tests exist; and, honestly, the GPU has to read what it
runs), and the trust id with "Give this to the operator if this is your own machine and you want private repos
routed to it" and a Copy link. The first time a GPU becomes a coding member a toast says so, once per install.

## 11. Secrets never leave the editor

`vscode-extension/src/privacy/redact.ts` — `redactSecrets(text) -> {text, count}` (API keys, tokens,
private keys, passwords in common formats; replaced by `[REDACTED]`) and `isExcluded(uri) -> boolean`
(.gitignore'd files, `.dlneignore`, `.env*`, `*.pem`, `*.key`, `id_rsa*`, `.npmrc`, `.pypirc`, cloud
credential files). Applied to every outgoing request: agent tool reads, chat context and autocomplete
prompts, to TheWall too.

As built:
- `redactSecrets(text, fileName?)` takes an optional file name. It only decides whether unquoted
  `name = value` pairs count as credentials; shaped tokens and quoted values are redacted anywhere.
- The exclusion rules are in `src/privacy/exclude.ts`, re-exported by `redact.ts`. Templates such as
  `.env.example` and `.env.sample` are not excluded, but they still get pattern redaction.
- Everything outside `src/privacy/` imports these through the adapter `src/privacy/index.ts`. A test
  enforces this.
- Exclusion checks fail closed: `isExcludedFile()` in the adapter treats a check that throws as excluded,
  and so does the agent's `privacyRefusal`.
- Known limits: shell output (for example `cat .env` that the user approves) is only pattern-redacted,
  not excluded. A name and value on different lines (Terraform `variable` blocks) is not caught. VS
  Code chat attachments arrive without a path, so they get pattern redaction only.
