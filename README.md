# DLNE: free AI for your editor

DLNE is a coding assistant for VS Code (and VSCodium, Cursor, Theia) powered by a swarm of
shared GPUs. There is no subscription and no token fee. The rule is simple: give a little
compute, get a lot of AI. New users get 100 free chats to try it; after that, sharing an
idle GPU with the swarm keeps it free.

**New in 2.4:** a GPU lent from home (8 GB or more) can run a whole coding model by itself and serve
two agents for other people, with requests encrypted end-to-end from the editor. Secrets are redacted
before anything leaves VS Code, and private repositories stay on machines the operator trusts.
[What that does and doesn't protect](docs/privacy.md).

**Use it:**
- **VS Code:** search the Extensions view for **DLNE Code Assistant**
  ([VS Code Marketplace](https://marketplace.visualstudio.com/items?itemName=dlne.dlne-code-assistant)).
- **VSCodium, Cursor, Theia** and other Open VSX editors: the same search finds it on
  [Open VSX](https://open-vsx.org/extension/dlne/dlne-code-assistant).
- Offline or locked down: the `.vsix` is on the [latest release](https://github.com/RGlabs84/DistributedLLMEngine/releases/latest)
  (Extensions view, the `...` menu, **Install from VSIX...**).
- Then open the DLNE sidebar, or type `@dlne` in the chat panel. No account needed.
- **No editor?** [DLNE Chat](https://dlne.duckdns.org/chat) runs in a phone browser.

## How it fits together

```
 VS Code extension ──HTTP──▶ coordinator ◀──RPC tunnels── worker nodes (volunteers' GPUs)
 (agent, chat, autocomplete)   (this repo)                 (this repo: engine/worker.py)
                                   ▲
                                   └── BarrkBOT (Discord): stats and operator controls
```

| Path | What it is |
|---|---|
| `vscode-extension/` | The product: chat/code agent, ghost-text autocomplete, one-click compute sharing |
| `engine/` | The coordinator and worker: model scheduling, worker registry, free-trial quota, admin API |
| `dlne_server.py` | Command line: `start` (coordinator), `worker`, `apikey`, `adminkey`, `status` |
| `docs/` | [`privacy.md`](docs/privacy.md) (what is sent where, and who can see it) and [`coding-members.md`](docs/coding-members.md) (the technical contract for coding members) |
| `tests/` | Python test suite (`pytest`) |
| `scripts/` | Release tooling (`release.py`, `release_apply.py`, `package_llama.py`), `publish_public.py` (the scanned snapshot that updates this public repository) and the Discord status tile |

The coordinator's `/` is a small public page that points at the extension and shows live
swarm status. The OpenAI-compatible API lives under `/v1/*`.

The public swarm's coding model is Qwen3.6-35B-A3B (a UD-IQ3_S quant) on two 8 GB AMD GPUs,
with a 65,536-token context; bigger models unlock automatically as more GPUs join.

## Coding members: lend a GPU, add agents

A **coding member** is a volunteer's GPU that runs a whole coding model on its own and answers coding
requests by itself, the way the phone pool's machines already do for chat. One request touches one
machine, so ordinary home internet is enough, and **each member adds about two more agents** working at
the same time.

- **Who qualifies:** a GPU with 8 GB of memory or more that is not on the coordinator's own fast network
  (a GPU that is still joins the shared big model, as before), and about 22 GB of GPU memory plus free
  system memory together for the first version (for example an 8 GB GPU with 14 GB of RAM free). The model
  is held whole: GPU memory first, the rest in system RAM. When a machine joins, it is timed on reading a
  prompt and writing an answer; one under the speed floors is not sent work.
- **Which model:** the smartest version of Qwen3.6-35B-A3B that the machine can hold whole and run fast
  enough. About 22 GB of GPU plus free system memory runs the first version (IQ3_S, live today). Higher-quality
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
  repository lookup.
- **Secrets are removed in the editor** before a request leaves it, and files such as `.env`, `*.pem`,
  `.gitignore`d files and anything in `.dlneignore` are never sent.
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
[`docs/privacy.md`](docs/privacy.md). The formats and routes are in
[`docs/coding-members.md`](docs/coding-members.md).

## Run the tests

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m pytest                        # engine and coordinator

cd vscode-extension
npm install
npm test                                # extension unit tests (no VS Code needed)
npm run test:e2e                        # real venv + pip install of the engine (network)
```

## Run a coordinator

```bash
python dlne_server.py start --port 11435
python dlne_server.py adminkey create   # prints the operator key once; only its hash is stored
```

Everything under `/v1/admin/*` needs that operator key as `Authorization: Bearer <key>`, and by
default only accepts it from the coordinator's own machine (`ADMIN_ALLOW_REMOTE=true` to change
that). BarrkBOT's `/dlne` command uses it.

Settings live in `config/config.txt` (`KEY=VALUE`). Seven groups matter for most operators:

- **Coding members (off by default)**: volunteers' GPUs that each run a whole coding model and answer
  coding requests on their own (see [Coding members](#coding-members-lend-a-gpu-add-agents) and
  [`docs/coding-members.md`](docs/coding-members.md)). `CODING_MEMBERS_ENABLED=false` keeps today's
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
  [website](https://live.ravenirongames.com/#dlne); it is not on Google Play yet). Reported
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

- **Who goes first, and how much**: when the coding engine is busy, its slots go to GPU
  contributors, API keys and the coordinator's own host first, CPU contributors second, and
  free-trial users third (`engine/priority.py`); autocomplete only runs on a slot nobody is
  waiting for. CPU contributors get `CPU_CODING_LIMIT` (300) coding chats in any
  `CPU_CODING_WINDOW_HOURS` (5) hours. The free trial (`STARTER_QUOTA_INFERENCES`, 100) is
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
  [`docs/privacy.md`](docs/privacy.md). A member has to read a request to answer it; nothing there
  changes that.
- The phone chat is separate: the volunteer machine that answers a chat sees its text.
- The public coordinator is reached over HTTPS (`https://dlne.duckdns.org`, Caddy in front
  of the coordinator). Run your own for anything sensitive.
- The agent asks before it edits files, runs a command, or reads outside your workspace.
- Report security problems privately to the maintainer rather than in a public issue.

## Support

Contribute a GPU (or a fast CPU) from the extension's sidebar, or
[buy a coffee](https://buymeacoffee.com/wubarrk) toward the coordinator's power and hardware.
Questions and news are in the [Discord](https://discord.gg/RNxvxWn8cw).

## License

MIT, see [LICENSE.md](LICENSE.md).
