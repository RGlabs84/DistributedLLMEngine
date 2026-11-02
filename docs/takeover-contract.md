# DLNE 2.7.1: takeover leases (the coordinator side of "Duka Takeover")

**Status: agreed with the DLNE-Sec session 2026-11-02 (shapes, timings, floor; 4a/4b added at its request); the owner
confirms before any deploy.** Nothing here reaches TheWall until the owner
confirms a deploy. The DLNE-Sec side (2.7.1-sec, "takeover mode") is built by the DLNE-Sec session against this file.

## Why

The owner wants DLNE-Sec (Duka's app) to be able to borrow TheWall's hardware for a working session, without taking
DLNE down: DLNE keeps answering (more slowly, or with fewer features) and gets everything back when the session ends.
`/v1/cluster/swap` is the wrong tool: it changes the model for everyone, has no scope and no way back.

## The rules

1. **Repurpose, never stop.** DLNE always keeps at least one coding lane and its phone lanes answering. Whatever a
   lease takes, DLNE moves or pauses; it never stops serving.
2. **Reversible.** Every change is undone when the lease ends: released, expired, its heartbeat lost, or the
   coordinator restarted with the lease gone stale. A crash mid-lease restores on the next start.
3. **LAN only, operator only.** The operator key (`Authorization: Bearer <admin key>`, the same key as `/v1/admin/*`),
   from a LAN or WireGuard peer, never through the HTTPS proxy, whatever `ADMIN_ALLOW_REMOTE` says.
4. **One lease at a time.** A second grant while one is active is refused (409).
5. **Idle-gated.** Changes that restart the engine wait until it is idle (two checks 5 s apart, at most 10 minutes);
   a grant that cannot get an idle moment is refused, nothing changed.

## What a lease can take

A lease names hardware by **PCI address** (`0000:03:00.0`), and optionally CPU cores and RAM:

| Asked for | What DLNE does |
|---|---|
| A GPU DLNE does not use | Nothing: it is handed over as it is (today: BC-160 #1). |
| The media card (`MEDIA_GPU`, the RX 580) | Pictures pause (as `IMAGE_GEN_ENABLED=paused`, with the same plain answer to everyone who asks); the vision projector moves to the CPU (slower picture reading, still on). |
| A card the coding engine runs on | The engine moves to the engine cards the lease leaves; if it leaves none, the engine runs on the CPU with the lease's context floor (see `keep`). |
| `cpu_cores` (whole cores, e.g. `[4, 5]`) | DLNE's engine and media are pinned off those cores (their SMT siblings too). |
| `ram_gb` | DLNE's own limit is lowered by that much (`MemoryMax`), never below what the kept engine needs. |

`keep` sets DLNE's floor while the lease runs: `{"coding_lanes": 1, "coding_context": 32768}` (defaults: 1 lane,
32,768 tokens; phone lanes always stay).

## API (all under `/v1/admin/takeover`)

- `POST /v1/admin/takeover/plan` with the request below: **dry run**. Answers what DLNE would do (the table above,
  for this request) and whether it is allowed, and changes nothing. DLNE-Sec shows this to the operator before asking.
- `POST /v1/admin/takeover` with the same body: **grant**. Waits for an idle moment, applies, and answers when DLNE
  runs in its reduced form (or refuses, changing nothing).
- `PUT /v1/admin/takeover/{lease_id}`: **heartbeat**, every 15 s. Answers the lease. Two missed (no heartbeat for
  45 s) end the lease.
- `DELETE /v1/admin/takeover/{lease_id}`: **release**. Answers at once; DLNE is restored in the background (idle-gated)
  and `GET` shows `restoring` until it is done.
- `GET /v1/admin/takeover`: the current lease or `{"active": false}`.

Request:

```json
{
  "gpus": ["0000:03:00.0", "0000:0c:00.0"],
  "cpu_cores": [],
  "ram_gb": 0,
  "keep": {"coding_lanes": 1, "coding_context": 32768},
  "ttl_seconds": 14400,
  "holder": "DLNE-Sec on portoBOX",
  "reason": "security scan session"
}
```

Lease (the answer to grant, heartbeat and GET):

```json
{
  "active": true,
  "lease_id": "tk_8f3a…",
  "state": "granting | active | restoring",
  "holder": "DLNE-Sec on portoBOX",
  "granted": {"gpus": ["0000:03:00.0", "0000:0c:00.0"], "cpu_cores": [], "ram_gb": 0},
  "dlne_now": {"engine_on": ["0000:09:00.0"], "coding_lanes": 1, "coding_context": 74752, "pictures": "paused", "vision": "cpu"},
  "since": "2026-11-02T03:30:00Z",
  "expires_at": "2026-11-02T07:30:00Z",
  "heartbeat_every_seconds": 15,
  "cards": [
    {"pci": "0000:03:00.0", "vulkan_index": 0, "name": "AMD Radeon Pro V520 (BC-160)", "vram_gb": 8.0, "free_vram_gb": 7.98, "action": "handover"},
    {"pci": "0000:0c:00.0", "vulkan_index": 2, "name": "AMD Radeon RX 580", "vram_gb": 8.0, "free_vram_gb": 7.1, "action": "pictures-pause+vision-cpu"}
  ]
}
```

`cards` (DLNE-Sec, 2026-11-02): one entry per granted card, in the request's order. `vulkan_index` is the card's
index in TheWall's full Vulkan device list (what `GGML_VK_VISIBLE_DEVICES` takes), `free_vram_gb` is read live when
the answer is made. `action` is one of `handover` (DLNE did not use it), `pictures-pause+vision-cpu` (the media card),
`engine-moves` (an engine card; the engine moved to `dlne_now.engine_on`, or to the CPU when that list is empty).

The dry run (`POST /v1/admin/takeover/plan`) answers:

```json
{
  "allowed": true,
  "problems": [],
  "cards": [ ...the same entries as above... ],
  "keep_result": {"engine_on": ["0000:09:00.0"], "coding_lanes": 1, "coding_context": 74752, "pictures": "paused", "vision": "cpu", "engine_restart": true},
  "cpu_cores": [], "ram_gb": 0
}
```

`problems` holds the refusal codes (below) with plain-words details when `allowed` is false; `engine_restart` says
whether granting restarts DLNE's engine (an idle-gated restart) or touches nothing running.

Refusals use the coordinator's usual `{"detail": …, "code": …}` with these codes: `takeover_forbidden` (403: no key,
wrong key, not a LAN peer, came through the proxy), `takeover_active` (409), `takeover_busy` (503: never idle),
`takeover_unknown_gpu` (400), `takeover_too_much` (400: the floor in `keep` cannot fit what is left).

## How it is built (coordinator 2.7.1)

- **An overlay, not an edit.** A lease never writes `config.txt`. Its changes live in `config/takeover.json` (the
  lease, its expiry and the overlay of limits), and `get_worker_limits()` applies that overlay on top of `config.txt`
  while the file exists. Release deletes the file and restarts the engine on the plain config. A coordinator that
  starts with a stale lease file (expired, or no heartbeat for 45 s) deletes it first.
- **The switch** goes through `_swap_model_core(hold_first=True, abort_if_busy=True, ready_timeout=…,
  rollback_model=…)`, the path automatic switching already trusts: the same model, restarted on the overlaid limits,
  and rolled back to the plain config if it does not come up.
- **Vision on the CPU** is llama.cpp's `--no-mmproj-offload` (the projector stays loaded, on the CPU); pictures pause
  through the same switch as `IMAGE_GEN_ENABLED=paused`.
- **CPU cores** are taken out of the engine's and the media's CPU masks (`--cpu-mask` in `ENGINE_EXTRA_ARGS`,
  `IMAGE_CPU_MASK`), with their SMT siblings (core n's sibling on TheWall is CPU n+6).
- **RAM** is `systemctl --user set-property --runtime dlne.service MemoryMax=… MemoryHigh=…` (runtime only, so a
  reboot cannot leave it lowered); the values before are kept in the lease file and set back on release.
- **Visible:** `/v1/cluster/status` gains `takeover: {"active", "state", "holder", "since", "expires_at"}` (no lease
  id, no address). The admin stats show it too.
- **Tests:** plan answers for each kind of card; LAN/proxy/key refusals; one lease at a time; idle gate refusal;
  heartbeat loss and ttl end it; release restores; a stale file at start is removed; the overlay never touches
  `config.txt`; status fields.

## Out of scope for 2.7.1

Leases from outside the LAN; several leases at once; taking a lane or the model itself (the lease takes hardware;
DLNE decides how to keep serving); DLNE-Sec's own scheduling of what it runs on what it borrowed.
