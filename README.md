# DLNE: free AI for your editor

DLNE is a coding assistant for VS Code (and VSCodium, Cursor, Theia) powered by a swarm of
shared GPUs. There is no subscription and no token fee. The rule is simple: give a little
compute, get a lot of AI. New users get 100 free chats to try it; after that, sharing an
idle GPU with the swarm keeps it free.

**Use it:** install **DLNE Code Assistant** from the Extensions view
([Open VSX](https://open-vsx.org/extension/dlne/dlne-code-assistant)), open the DLNE sidebar or
type `@dlne` in the chat panel.

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
| `tests/` | Python test suite (`pytest`) |
| `scripts/` | Release tooling (`release.py`, `release_apply.py`, `package_llama.py`), `publish_public.py` (the scanned snapshot that updates this public repository) and the Discord status tile |

The coordinator's `/` is a small public page that points at the extension and shows live
swarm status. The OpenAI-compatible API lives under `/v1/*`.

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

Settings live in `config/config.txt` (`KEY=VALUE`). Six groups matter for most operators:

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
  folder. The phone page is `/chat` (installable from Safari or Chrome); the Android app is in
  `android/`. Reported answers (`POST /v1/app/report`) are kept in
  `config/app_reports.jsonl` and read with `GET /v1/admin/app_reports`.

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
  Measure before and after: on the public swarm's two 8 GB cards, 4-token drafts gave 45 tok/s
  against 39 without, while 8 and 16-token drafts were slower than none.

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
  public coordinator is run by the maintainer; treat it like any hosted AI service.
- The public coordinator is reached over HTTPS (`https://dlne.duckdns.org`, Caddy in front
  of the coordinator). Run your own for anything sensitive.
- The agent asks before it edits files, runs a command, or reads outside your workspace.
- Report security problems privately to the maintainer rather than in a public issue.

## Support

Contribute a GPU (or a fast CPU) from the extension's sidebar, or
[buy a coffee](https://buymeacoffee.com/wubarrk) toward the coordinator's power and hardware.

## License

MIT, see [LICENSE.md](LICENSE.md).
