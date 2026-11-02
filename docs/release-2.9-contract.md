# DLNE 2.9.0: the pool counts system RAM, new models on the ladder, automatic switching back on (the contract)

**Owner, 2026-11-02:** "Turn automatic switching back on. Make CPUs and system count towards the pool. ... There's no
reason that when that laptop joins for coding and donates GPU and CPU that it shouldn't get folded into a bigger model.
... we want a model or two in the bracket range that would be achievable with 32 gigs, 36 gigs, 28 gigs etc. Prefer
coding specific but still need to be all around for phone and web use." Picks, after a look beyond Qwen: **Gemma 4
26B-A4B, Nemotron 3 Nano, Qwen3-Coder-Next (the ~45 GB build), gpt-oss-120b on standby.**

Release rule (owner, same day): this is the next version. It is built and tested in full and released once, with its
update log; nothing here goes live piecemeal.

## Why automatic switching is off, and why it cannot simply be turned on

`engine/auto_swap.py` and `engine/coding_ladder.py` count **near GPU memory only**: TheWall's cards and volunteers'
GPUs within 80 ms. Each rung has one number, `min_vram_gb`. TheWall's served model (Qwen3.6 35B-A3B IQ4_XS) runs on
16 GB of GPU memory plus about 8 GB of system RAM (`--n-cpu-moe 16`: some layers' experts in RAM), but its rung says
24 GB of VRAM. With `AUTO_SWAP=true` today the controller would see a 16 GB pool, decide the model no longer fits, and
step **down** to the 15 GB rung within two minutes, restarting the engine under its users.

## 1. The pool: GPU memory and system memory, counted apart

Mixture-of-experts (MoE) models use a few billion parameters per token, so most of their weight (the experts) can sit
in system RAM and be read by the CPU at usable speed. Dense models cannot.

- **Each near machine reports two numbers:** GPU memory it lends (as today) and **system RAM it lends** for experts.
  - TheWall: RAM the DLNE service may use (`MemoryMax`, 18 GB today) minus what the engine needs besides experts.
  - A volunteer: a share it chooses, defaulting to half its free RAM, never below 4 GB left for itself, and only
    while it is donating (VS Code's DLNE worker and DLNE Donate both get the setting).
- **Only close machines lend RAM.** A token's activations cross the network once per expert layer, so a volunteer's
  RAM counts only within `POOL_RAM_MAX_RTT_MS` (default 5 ms: the same LAN or a fast WireGuard hop), stricter than the
  80 ms GPU rule. Farther volunteers lend GPU memory only, as today.
- **Status:** `/v1/cluster/status` gains `pool_memory: {gpu_gb, ram_gb, total_gb, machines}`. The status page's map
  shows both numbers.

## 2. Rungs say what they need in both kinds of memory

A rung gains `min_total_gb` (GPU + lent RAM) and `min_gpu_gb` (what must be on GPUs: attention, shared weights, the
KV cache for its context). It fits when both hold. It also says how to lay itself out: which experts go to RAM
(`--n-cpu-moe N` or `-ot` rules), picked by the coordinator from the pool it actually has, and checked against
`parse_engine_args` before any restart. Dense rungs set `min_total_gb == min_gpu_gb`.

The ladder is ordered by **measured quality** (section 4), not size: "up" means better, so auto switching never moves
to a bigger model that measured worse.

## 3. The models: Qwen family only, for now (owner, 2 Nov)

Measured on TheWall on 2 Nov (`~/DLNEv2/models/bench/results/`, TheWall alone: two BC-160s + RAM, 32k, thinking off).
Writes and reads are in tok/s; reads are at 17.7k tokens; coding is out of 13, phone out of 4.

| Ladder | Rung | File (on TheWall) | Writes | Reads | Coding | Phone | Vision |
|---|---|---|---|---|---|---|---|
| coding | Qwen3.6 35B-A3B IQ4_XS (today) | 17.7 GB | 37.8 | 378 | 13 | 4 | yes |
| coding | Qwen3.6 35B-A3B Q4_K_M | 22.1 GB | 31.4 | 295 | 12 | 4 | yes |
| coding | Qwen3.6 35B-A3B Q6_K | 29.3 GB | 28.1 | 224 | 13 | 4 | yes |
| coding | Qwen3-Coder-Next 80B-A3B UD-IQ4_XS | 38.4 GB | 16.4 | 170 | 13 | 4 | no |
| coding | Qwen3-Coder-Next 80B-A3B Q4_K_S | 45.5 GB | not measured (needs a pooled laptop) | | | | no |
| phone | Qwen3.5 4B + photo reader (RX 580) | 2.7 + 0.7 GB | 27.6 | photos 2.2 s / 13 s | 10 | 4 | yes |
| phone | Qwen3.5 9B + photo reader (RX 580) | 5.7 + 0.9 GB | 20.5 | photos 3 s / 25 s | 13 | 4 | yes |

**Notes on the table:**
- **The bigger rungs** (Q6_K, Coder-Next) are slower on TheWall alone because their extra weight sits in RAM. They are the rungs a pool with volunteers' GPUs and RAM moves up to.
- **The ladder's order** comes from the harder benchmark round (section 4) before release; TheWall-only stays on IQ4_XS.
- **Measured but off the ladder:** Gemma 4 26B-A4B, Nemotron 3 Nano 30B-A3B and gpt-oss-120b (owner: "we stick with the qwen fam for now"). Their files stay on TheWall's disk.
- **Pins:** every rung is pinned by Hugging Face revision, size and SHA-256 (`scripts/` pins, as the ladder does today).

## 4. Measured before enabled

Each new rung is measured on TheWall, the same way the 9B and the RX 580 were, before it may be enabled:

- **Coding:** the DLNE coding suite (tasks v2), pass rate and tool-call correctness (the raw `<tool_call>` text of
  2.8.4 counts as a failure).
- **Phone and web:** a fixed set of chat questions (facts, lookups, a photo where it has vision, a picture ask).
- **Speed:** decode and prompt reading at 4k and 32k deep, on TheWall alone and with the Portabox laptop lending GPU
  and RAM. It must reach `AUTO_SWAP_MIN_DECODE_TPS` (10 tok/s) wherever its rung could be chosen.

The results go into the rung (a `measured` block) and decide the ladder's order. A rung that does not beat the rung
below it is left disabled, with the numbers saying why.

## 5. Photos when the served model has no vision

While the coding model cannot read pictures (Qwen3-Coder-Next), a message with a photo, from the VS Code agent or a
phone, goes to TheWall's phone lane: the phone ladder's photo rung (6c) answers it (owner, 2 Nov: "just have it use the
phone lane"). No separate helper model. With no phone rung that reads photos, the coding agent is told plainly that
pictures are not available right now, as today when vision is off.

## 6. Automatic switching back on

Only after 1-5: `AUTO_SWAP=true` on TheWall, in all three named layouts (normal, takeover one card, takeover two
cards). The takeover layouts keep the switcher on hold, as leases already do (2.7.1). Every existing safeguard stays:

- 10 minutes stable before moving up;
- 30 minutes cooldown between switches;
- the speed check before anyone is let in, and putting the old model back if it fails;
- never cutting a request (drain, or call the switch off);
- the flap breaker (3 rebuilds an hour).

**New:** a move up that depends on a volunteer's RAM also needs that volunteer to have stayed for 30 minutes, and the
move down when they leave is immediate (`engine_short`, as for GPUs).

## 6b. Phone and web chat: TheWall first with volunteer overflow (default), or volunteers first

**Owner, 2026-11-02:** "the phone system probably going back to cpu donations" ... "yes mark the routing change for 2.9".

Today (`/v1/app/chat/completions`, engine/app_pool.py): with phone lanes set (`ENGINE_PHONE_SLOTS`), every phone goes to
an engine lane whenever the engine is up; volunteers' CPUs answer only while it is down or swapping. With no lanes,
volunteers answer all text and photos are refused. Neither is what the owner wants.

- **`PHONE_ROUTE=wall_first` (the 2.9.0 default; owner, 2 Nov: "gpu first, if busy then volunteer cpus"):**
  1. A phone goes to TheWall's phone rung (6c) first: the fastest answer.
  2. When all of TheWall's phone lanes are busy, a text message goes to a ready volunteer CPU instead of waiting: the
     overflow. Photos never go to a volunteer; they wait for TheWall's lane.
  3. If neither can take it, the same 429 a busy phone gets today.
- **`PHONE_ROUTE=volunteers_first` (owner's earlier idea, kept as a setting, for when TheWall's GPUs are wanted for coding):**
  1. A text message goes to a ready volunteer CPU first, the best phone tier that machine can run.
  2. Photos never go to a volunteer: a message with a photo is answered on TheWall's phone lane.
  3. If no volunteer is ready, or all are busy, it goes to TheWall's phone lane: the backup.
  4. If neither can take it, the same 429.
- **`PHONE_ROUTE=wall_only`** keeps today's behaviour (volunteers answer only while TheWall's engine is down).
- **TheWall keeps at least one phone lane** (`ENGINE_PHONE_SLOTS >= 1`) for photos and backup; with 0, the coordinator
  warns at start that photos are refused and nothing backs the volunteers up.
- **Same model where it can be:** volunteers with 16 GB or more run Qwen3.6 35B-A3B, the model TheWall runs, so a phone
  barely notices which machine answered; smaller machines run the Qwen3 4B tier as now.
- **Looking things up, pictures, voice:** lookups (weather, search) run on the coordinator whoever writes the answer, as
  now; pictures and voice stay on TheWall's RX 580.
- **Speed guard:** a volunteer whose measured answer speed is below `PHONE_MIN_TPS` (default 6 tok/s) is skipped while
  TheWall's lane is free, so a slow volunteer never makes a phone wait longer than TheWall would.
- **Status and planner:** `/v1/cluster/status` `app_pool.serving` says `volunteers` or `engine` per the last answer, and
  `app_pool.route` says the mode; the status page's phone node and the planner (its "Phone & web chat answered by"
  choice) show it.
- **Privacy, unchanged:** the app's notice already says a lent computer may answer; photos stay on DLNE's own server.
- **Tests:** a text goes to a ready volunteer; a photo goes to the lane; no volunteer, then the lane; busy volunteers,
  then the lane; neither, then the 429; the slow-volunteer guard; `engine_first` keeps today's order.

## 6c. A phone ladder, like the coding ladder

**Owner, 2026-11-02:** "what about a phone tiers model system just like the coding engine?" Phones get their own rungs
and their own switcher, separate from coding's.

- **TheWall's phone rungs** (`PHONE_RUNGS`, ordered by measured speed and what they can do; all measured on TheWall
  on 1-2 Nov):

  | Rung | Where | Writes | Photos |
  |---|---|---|---|
  | Qwen3.5 9B + photo reader | a free BC-160 | 61.9 tok/s | yes |
  | Share the coding engine (Qwen3.6) | the coding engine's phone lanes | 33.4 | yes |
  | Qwen3.5 4B + photo reader | RX 580 | 27.6 (photos 2.2 s / 13 s) | yes |
  | Qwen3.5 9B + photo reader | RX 580 | 20.5 (photos 3 s / 25 s) | yes |
  | Qwen3.5 9B | TheWall's CPU | 10.5 | slow |

- **A rung of its own** is a phone engine the coordinator starts on that card or the CPU (`PHONE_MODEL`,
  `PHONE_DEVICE`, `PHONE_LANES`, `PHONE_CONTEXT`, `PHONE_VISION`), separate from the coding engine, so coding never
  waits for phones. Sharing the coding engine stays a rung (today's behaviour).
- **The phone switcher** moves TheWall's phone rung up when room appears (pictures paused, a card freed by a takeover
  ending, coding needing less) and down when coding or pictures need it back, with the coding switcher's safeguards:
  - stable for a while before moving up;
  - a cooldown between switches;
  - a speed check before phones are let in;
  - never mid-answer (drain);
  - the flap breaker.

  Coding always wins a contest for a card.
- **The volunteers' rungs** stay as today (engine/app_pool.py): each volunteer runs the best phone tier its RAM allows
  (Qwen3 4B at 8 GB, Qwen3.6 IQ4_XS at 16 GB, Qwen3.6 full quality at 24 GB).
- **Choosing per phone:** the router (6b) asks TheWall's rung, then the volunteers, by the route; within the
  volunteers, the fastest measured one that is free.
- **Planner:** the phone card's settings and its "Phone ladder" table show the rungs; the status page's phone node
  shows the rung in use.

## 6c+. The phone model grows with the volunteers, exactly as the coding model does

**Owner, 2026-11-02:** "The coding model steps up when GPUs connect ... it gets sharded across the other GPUs, allowing us
to run bigger models. The phone model should act the exact same way. Therefore, it should not kick voice out because the
expansion should only take place when there is enough volunteers or volunteer resources for the phone model to actually
upgrade without affecting the RX580." (This replaces an earlier draft of this section, "the 9B runs on the volunteers
instead", which missed that point.)

- **TheWall's phone engine is shardable**, like the coding engine: its base is a fixed budget on its home card (the
  RX 580 today: `PHONE_HOME_BUDGET_MB`, what the base rung uses, about 3 GB for Qwen3.5 4B), plus the GPUs and the lent
  RAM (section 1) of near volunteers, joined over RPC as the coding engine joins its volunteers.
- **The home budget never grows.** A bigger rung puts on the RX 580 only what fits in that budget and the rest of its
  layers on the volunteers. So voice, the vision projector and pictures keep their room whatever the phones do.
- **Phone rungs** (6c), smallest first: Qwen3.5 4B (the base, on the RX 580 alone), then Qwen3.5 9B, then Qwen3.6
  35B-A3B (its experts in the volunteers' lent RAM, like the coding engine's `--n-cpu-moe`). Each rung states what it
  needs from the volunteers beyond the home budget, in GPU memory and in RAM (section 2).
- **The phone switcher** moves up when the near volunteers' resources have held that need for `PHONE_UP_STABLE_SECONDS`
  (10 min). It uses the coding switcher's rules:
  - the volunteer's RAM counts only after it has stayed 30 minutes;
  - a speed check before phones are let in (`PHONE_MIN_TPS`), or it puts the base back;
  - never mid-answer;
  - a cooldown;
  - the flap breaker.

  It steps down to the rung that still fits as soon as a volunteer leaves, back to the 4B on the RX 580 alone with none.
  Coding wins any contest for a volunteer's resources.
- **Phones allow farther volunteers** (owner, 2 Nov: "its phone, loosen the latency to 200ms"): a volunteer within
  `PHONE_SHARD_MAX_RTT_MS` (default 200 ms) may carry phone layers, against the coding pool's 5 ms for lent RAM. A split
  model still sends each token across the network wherever its layers change machine, so a far volunteer makes every
  token wait about one round trip per hop; the speed check (`PHONE_MIN_TPS`) decides whether that rung is used.
- **Photos:** the phone engine's photo reader stays on its home card (or rule 6e sends photos to a coding lane). Photos
  never go to a volunteer: on a sharded phone model, the photo is read at home and only the model's layers run on the
  volunteers.
- **Status and planner:** the status says the phone rung and where its layers are; the planner's phone card shows its
  home budget on the RX 580 and the volunteers' share, like the coding model's layers on its cards.
- **Tests:**
  - the switcher moves up only when the volunteers' share holds;
  - the home card's use stays within its budget at every rung;
  - the step down at once when a volunteer leaves;
  - the speed check;
  - photos stay home.

## 6d. Seeing whether the coding trial is being worked around

**Owner, 2026-11-02:** "I wonder if someone has figured out how to get around the trial" ... "yes add those to the 2.9
plan". On 2 Nov the record held 27 networks and 998 trial chats (5 used up, 4 past half, 18 light), with no sign of
abuse, but it keeps no times, so a string of fresh networks burnt one after another could not be seen.

- **Log trial events** (engine/logger.py, the coordinator's log): a network starting its trial, crossing half, and
  using it up, each with the time and a **masked** network (a keyed hash: the same network always gets the same tag, the
  address itself is never written). One line per event, never per request.
- **Admin stats** (`GET /v1/admin/stats`, operator key): trials started today / this week, used up today / this week,
  fresh networks in the last hour, and the largest number of fresh networks seen in any hour.
- **A burst flag:** more than `TRIAL_BURST_PER_HOUR` fresh networks in an hour (default 10) logs one warning line and
  shows in the stats, so a person rotating addresses stands out. Nothing is blocked automatically.
- **Privacy:** the hash key lives in config/ next to the trial record and never leaves TheWall; the masked tags are
  only in the coordinator's log and the operator's stats. The free phone/web chat and voice keep having no trial.
- **If abuse ever shows up** (a later version, owner's call): tie the coding trial to a key the extension creates and
  keeps, as well as the network, so a new address alone no longer gives a fresh 100.
- **Tests:** the events are logged once each with a masked tag (never the address); the stats count them; the burst
  flag trips past the limit and not below it.

## 6e. Phone photos go to a coding lane when the phone model cannot see

**Owner, 2026-11-02:** "plan the rule change so phone photos goto a coding lane". It lets a layout keep one photo reader,
the coding model's own projector (Qwen3.6, the stronger reader), and run the phones' model without one, which saves its
reader's memory (0.64 GB for Qwen3.5 4B) on the RX 580.

- **When:** the phone rung answering text cannot read photos (`PHONE_VISION=false`, or a text-only phone model), and the
  coding engine can (its projector is loaded). Otherwise nothing changes: a phone model that reads photos answers its own,
  and section 5 covers the opposite case (the coding model cannot see: photos go to the phone lane).
- **What goes there:** only a turn that carries a photo. The phone's history is sent with it, at the phone's own context
  (`ENGINE_PHONE_CONTEXT`, 8k), never the coding lane's 64k. The answer streams back to the phone in the same format, and
  the next text-only turn goes back to the phone model.
- **Which coding lane:** the coordinator asks the lane book (engine/priority.py `LaneAffinity`) for the coding lane
  whose cached conversation matters least: one with no conversation, else the one used longest ago. A coding
  conversation in its last few minutes is never chosen while another lane will do. The engine's prompt cache in RAM
  (`--cache-ram`, 6 GB on TheWall) keeps the evicted conversation's start, so its owner's next turn is read faster than
  from nothing.
- **Waiting and priority:** the photo waits for a coding lane like a phone waits for its lane (`PHONE_LANE_WAIT_SECONDS`),
  then gets the same 429 as a busy phone. It never interrupts a coding answer, and it queues behind key holders' coding
  requests (priority as the phone's tier).
- **Limits, privacy:** the hourly photo budget per network (`IMAGE_INPUT_PER_HOUR`) counts it as today. The photo never
  goes to a volunteer, as always.
- **Status:** `app_pool.photos` says where phone photos are read: `phone_model`, `coding_lane` or `none`.
- **Planner:** with the phone card's "Reads photos" unticked and the coding projector loaded, the notes say phone photos
  go to a coding lane instead of being refused.
- **Tests:**
  - a phone photo goes to a coding lane when the phone model cannot see;
  - the lane chosen is the coldest;
  - a text-only turn stays on the phone model;
  - the history is capped at the phone's context;
  - the wait and the 429;
  - a phone model that can see keeps its own photos;
  - with no projector anywhere, photos are refused as today.

## 7. Tests and release

Unit tests for the two-number pool, rung fitting and ordering, the RAM RTT rule, layout picking, photo routing, the
phone routing (6b) and the phone ladder and its switcher (6c), the trial events, stats and burst flag (6d), phone photos on a coding lane (6e);
the auto-swap tests extended with fake RAM pools. The full Python suite on TheWall's CI, the extension and Android
suites, and a live check with the laptop joining and leaving, recorded on the status page. Released once, as 2.9.0,
with Android 1.1.1 and anything else saved for it; each part is named in the update log.

## The owner's decisions (2026-11-02)

1. **A volunteer lends 8 GB of RAM by default** ("8gb"), never more than it has free minus 4 GB for itself; the
   volunteer can change it in VS Code's DLNE worker settings and in DLNE Donate.
2. **Qwen family only, for now** ("nah we stick with the qwen fam for now"): Gemma 4 and Nemotron 3 Nano (measured 2 Nov)
   and gpt-oss-120b (on TheWall's disk, standby) stay **off the ladder**. The coding ladder is Qwen3.6 35B-A3B (IQ4_XS,
   Q4_K_M, Q6_K) and Qwen3-Coder-Next (IQ4_XS, Q4_K_S); the phone ladder is Qwen3.5 4B / 9B with their photo readers and
   Qwen3.6.
3. **No separate vision helper** (owner: "just have it use the phone lane if we ever get that far in swarm"): while a
   coding model without vision (Qwen3-Coder-Next) is served, a photo, from the coding agent or a phone, is answered on
   TheWall's phone lane, by the phone ladder's photo rung (6c: Qwen3.5 4B or 9B with its photo reader, or Qwen3.6 when
   phones share the coding engine). Nothing extra is loaded for it. Section 5 shrinks to this rule.
