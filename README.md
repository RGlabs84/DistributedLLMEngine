# DLNE: free AI for your editor

DLNE is a coding assistant for VS Code (and VSCodium, Cursor, Theia) powered by a swarm of
shared GPUs. There is no subscription and no token fee. The rule is simple: give a little
compute, get a lot of AI. New users get 200 free chats to try it; after that, sharing an
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

Settings live in `config/config.txt` (`KEY=VALUE`). Three groups matter for most operators:

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
- The public coordinator is reached over HTTPS (`https://73-215-216-195.sslip.io`, Caddy in front
  of the coordinator). Run your own for anything sensitive.
- The agent asks before it edits files, runs a command, or reads outside your workspace.
- Report security problems privately to the maintainer rather than in a public issue.

## License

MIT, see [LICENSE.md](LICENSE.md).
