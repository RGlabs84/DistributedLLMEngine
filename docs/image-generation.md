# Image generation (since server 2.6.1)

The coordinator can draw a picture from a text description. It is **off by default**, runs **only on the coordinator's
own media card** (`MEDIA_GPU`) and never on a volunteer's machine or a coding member. This page is for the operator who
turns it on and for the people writing clients (the web chat, the Android app, the VS Code extension).

With none of the settings below present the coordinator behaves exactly as 2.6.0: the status does not mention images, and
every image endpoint answers `503 image_generation_unavailable`.

It can also be **paused** (`IMAGE_GEN_ENABLED=paused`, 2026-10-30), for when the media card is lent to the coding model:
nothing image-related is loaded, but every request for a picture is told so in plain words with a link to a page that
explains it. See [Paused](#paused-image_gen_enabledpaused).

## What it is made of

| Part | What |
|---|---|
| The image engine | stable-diffusion.cpp `master-1017-be48649`, `sd-server`, with Z-Image-Turbo (Q4_K) and its VAE; the text encoder is the Qwen3-4B Instruct 2507 the CPU pool already pins (`scripts/app_models.json`, tier `qwen3-4b`), run on the CPU. All three model files are ungated (Apache-2.0). |
| Where it runs | The media card only (`GGML_VK_VISIBLE_DEVICES` is set to that card's global Vulkan index, found from the PCI address in `MEDIA_GPU`). `sd-server` listens on `127.0.0.1` on a free port, and runs in an empty private directory. |
| The checks | The coordinator's own engine reads the description, and later the finished picture (so vision, `capabilities.vision`, must be on). |
| Measured on TheWall (RX 580) | 768x768: 78 to 80 s. 1024x1024: 153 s. Cold start (loading the models): 24 s. Peak 6.45 GiB of the card with the vision projector also on it; 4.85 GiB idle with `sd-server` loaded. 512x512 is faster but draws stray made-up text, so it is not offered. |

## Turning it on (operator)

1. **Release.** Package the engine once per upstream build and ship it with the release:
   `scripts/package_sd.py` (downloads the pinned asset, checks its SHA-256, writes `release_dist/assets/sd/server-sd.tar.gz`
   and `build.json`), then `scripts/release.py deploy`. A release signs `sd_build`, `server_sd_sha256` and `server_sd_size`
   into the manifest when `release_dist/assets/sd/` holds a build (or `--sd-assets DIR`); without it everything else is as
   before, and `--no-binaries` leaves it out too. `release_apply.py` unpacks it into `<install>/bin-sd/`, a folder of its
   own: its libraries have llama.cpp's names and must never be mixed with them. Workers never download it.
2. **Models.** On the coordinator host: `python scripts/fetch_image_models.py` (into `IMAGE_MODEL_DIR`, default
   `models/image` under the install; `--dir` overrides). It resumes an interrupted download and moves a file into place
   only when its size and SHA-256 match the pin in `scripts/image_models.json`; a wrong file is deleted and refused.
   About 6.7 GB in all.
3. **Config** (`config/config.txt`), with `MEDIA_GPU` and `ENGINE_MMPROJ` already set (the media card carries the vision
   projector; the engine must be a Vulkan build):

   ```
   IMAGE_GEN_ENABLED=true
   ```

   Everything else has a default. The change applies without a restart of the coordinator; restart DLNE only when
   `MEDIA_GPU` or `ENGINE_MMPROJ` are new.
4. **Check.** `GET /v1/cluster/status` should show `"capabilities": {"vision": true, "image_generation": true}` and an
   `image_generation` block. If `image_generation` is `false`, the coordinator log says why once per change ("Image
   generation is not available: ..."): the media card is not named, the sd build or a model file is missing or the wrong
   size, vision is off, the engine is restarting, or `sd-server` failed three times in a row and is paused.

Requirements, all of them: `IMAGE_GEN_ENABLED=true`; `MEDIA_GPU` set on a Vulkan engine build, and the card not
shared with the coding model (`MEDIA_GPU_SHARED` unset or `false`); the engine reads images
(vision on); `bin-sd/sd-server` installed; the three model files present with the right sizes. Any one missing means
unavailable.

### Settings

| Key | Default | Range | Meaning |
|---|---|---|---|
| `IMAGE_GEN_ENABLED` | `false` | `true`/`false`/`paused` | The master switch. `paused` is off, but says so to everyone who asks ([below](#paused-image_gen_enabledpaused)). Any other word is `false`. |
| `IMAGE_MODEL_DIR` | `models/image` | a path | Where the three model files are. |
| `IMAGE_THREADS` | 4 | 1 to 16 | CPU threads for the text encoder (`-t`). |
| `IMAGE_CPU_MASK` | unset | hex, e.g. `0xf0` | Pins `sd-server` to those CPUs with `taskset` (needs `taskset`; unpinned with a warning without it). |
| `IMAGE_PER_HOUR` | 6 | 1 to 60 | Pictures one network may start in any hour. |
| `IMAGE_QUEUE_MAX` | 4 | 1 to 16 | Jobs that may wait; more get `429 image_busy`. |
| `IMAGE_JOB_TIMEOUT_SECONDS` | 300 | 60 to 900 | A job running longer is stopped and `sd-server` replaced. |
| `IMAGE_IDLE_UNLOAD_MINUTES` | 20 | 0 to 1440 | `sd-server` is stopped after this long with no job (the next one reloads it, 24 s). 0: never. |
| `IMAGE_RESULT_TTL_SECONDS` | 1800 | 60 to 3600 | A finished job is kept this long, then `expired` (30 min since 2.6.2: the Android app checks only while it is on screen). |

A value outside its range keeps the default.

### What `sd-server` does and does not get

- Started lazily on the first job; only if the media card has at least 5.6 GiB free (read live from the card).
- Command: `sd-server --listen-ip 127.0.0.1 --listen-port <free port> --diffusion-model ... --vae ... --llm ... --backend te=cpu -t <IMAGE_THREADS> --cfg-scale 1.0 --steps 8 --eager-load`.
- Environment: `GGML_VK_VISIBLE_DEVICES=<the media card>`, `LD_LIBRARY_PATH=<install>/bin-sd`. Working directory: an empty private directory, removed when it stops.
- Its output (stdout and stderr) is kept only while it starts: the last 200 lines, in memory. If it exits or never answers during startup, the coordinator log gets how it ended (exit code or signal) and its last 40 lines, each as `sd-server: <line>`. The moment it is ready the kept lines are dropped and nothing it prints afterwards is ever kept or logged, because from the first job on it prints the prompt; the output is still read and thrown away so its pipe never fills. A crash after readiness logs only the exit code or signal.
- The request body is built by the coordinator from the cleaned prompt, the size and a random seed, with fixed steps and guidance; no field a client sends is passed on.
- A job timeout, a crash or a failed job kills it; the next job starts it again. **Three failures in a row** make image generation unavailable for 30 minutes. A cancelled job, or a media card without enough memory, is not a failure.
- It is a child of the coordinator: stopping DLNE stops it, and the coding engine's restart path never waits on it.

## What a picture goes through

1. **Clean** the prompt: control and invisible characters dropped, every `<...>` block (`<lora:...>`, `<sd_cpp_extra_args>...</...>`) removed, whitespace collapsed. 1 to 1000 characters remain, or the request is a 400.
2. **Term list** (`engine/image_policy.py`): obvious cases are refused with a 400 `prompt_refused` before anything is queued, and cost nothing (no trial chat, no hour).
3. **Model check** of the prompt, by the coordinator's own engine, as a strict JSON verdict `{"allowed": bool, "category": str}`. Anything that is not plainly `allowed: true` is a refusal (fails closed). The job ends `refused`.
4. **Draw** on `sd-server`: one job at a time, FIFO, a random seed.
5. **Strip metadata**: only `IHDR`, `PLTE`, `tRNS`, `IDAT`, `IEND`, `sRGB`, `gAMA`, `cHRM`, `pHYs` chunks are kept. stable-diffusion.cpp writes the prompt into a `parameters` text chunk; it, EXIF, time stamps and all other text are removed.
6. **Model check** of the stripped picture by the coordinator's vision model, same verdict. Refused: the picture is dropped, the job ends `refused`.
7. **Hold** the PNG in memory only, for `IMAGE_RESULT_TTL_SECONDS` after the job finishes (fetching it does not remove it earlier). Then the job is `expired` and the picture is dropped.

Refused categories: sexual content or nudity (any); minors in any sexual, violent or exploitative context; real identifiable people; graphic violence or gore; hate symbols or extremist propaganda; self-harm; instructions for making weapons or drugs. A refusal is a short, kind sentence in `detail`, never a lecture and never the prompt repeated.

**Which engine slot the checks take.** The checks always run on the coordinator's own engine. A phone job takes one of the engine's phone lanes while it has them (`ENGINE_PHONE_SLOTS`); otherwise, and for every coding-side job, a coding slot at the lowest priority (behind every person's chat). If no slot frees up within two minutes, or the engine is restarting, the job ends `failed` with "DLNE is restarting, try again in a moment." (or a "busy" sentence), and the trial chat is given back (the hour too, when nothing had been drawn yet).

**What is logged.** A prompt's length and an 8-hex hash (`[prompt 31 chars #ab12cd34]`), the size, timings, and the verdict category. Never a prompt, never picture bytes. Nothing is written to disk.

## The phone endpoints (anyone)

The free trial applies: one picture costs one trial chat (the same `STARTER_QUOTA_INFERENCES` count coding chats spend, by network address, IPv6 per /64), unless the caller is the coordinator host, holds an API key or a worker token, the contribution rule is off, or (2.6.2) the picture is made from a network where a computer is contributing ([below](#pictures-are-free-on-a-contributors-network-262)). The chat is given back if the job ends `failed` or `cancelled`, or was turned away by a cap; a `refused` picture keeps it spent. `chat_id` is accepted and ignored.

### Pictures are free on a contributor's network (2.6.2)

A network (the trial's own identity: an IPv4 address, an IPv6 /64) from which **any registered worker, GPU or CPU, in any role, has sent a heartbeat now or in the last 24 hours** is never charged a trial chat for a picture, from the chat tool or the dialog alike. A contributor's own phone on their home Wi-Fi shares the laptop's network, so it spends nothing while the laptop lends compute, nor for a day after it stops. (The network is the trial's own identity, so a phone and a laptop that reach the coordinator over different address families, one over IPv4 and one over IPv6, are two networks.) Everything else about a picture still applies: the hourly cap (`IMAGE_PER_HOUR`), the queue, the term list and both checks.

- **What feeds it.** `POST /worker/heartbeat`, after the coordinator has accepted the heartbeat (signature, version, plausible hardware, the CPU floors) and registered the worker. A refused heartbeat counts for nothing. A heartbeat from this machine itself (a local address) is not recorded: its own clients are never charged anyway, and it says nothing about anyone else.
- **What is kept.** `config/contributor_networks.json`, written like `config/gpu_standby.json` (`engine/contributor_networks.py`): network address and the time of its last heartbeat, nothing else (no worker id, no hardware). Written aside and renamed into place, at most every 30 s when a network was added or dropped and every 10 minutes to keep the times fresh, and at a clean shutdown; read at start-up. Entries not seen for 24 hours are dropped, at most 10,000 are kept (the one seen longest ago goes first), and a missing, damaged or hand-edited file means no history. After a restart a network's day can run out up to 10 minutes early, never late.
- **When the trial is used up** and the network is nobody's contributor, the refusal is `429 image_quota` with `Retry-After: 3600` and the detail "Your free trial is used up. Pictures stay free on any network where a computer contributes to DLNE (now or in the last day)."
- **Unchanged:** the IDE side (`POST /v1/images`, [below](#the-coding-side-endpoints-ide)) goes through the chat gate as before and still spends a trial chat on a contributor's network; polling and cancelling never cost anything.

### `POST /v1/app/images`

```json
{"prompt": "a red fox in the snow", "size": "768x768", "chat_id": "<uuid, optional>"}
```

`size` is one of `768x768` (default), `1024x1024`, `768x1024`, `1024x768`.

| Status | Body |
|---|---|
| `202` | `{"id", "status": "queued", "position", "eta_seconds"}` |
| `400` | `{"detail", "code"}`: `bad_request`, `prompt_empty`, `prompt_too_long`, `bad_size`, `prompt_refused` (+ `"category"`) |
| `429` | `{"detail", "code"}` with `Retry-After`: `image_quota` (the hourly cap, or the free trial is used up and the network is no contributor's), `image_busy` (the queue is full) |
| `503` | `{"detail", "code": "image_generation_unavailable"}`. While the engine restarts the `detail` is "DLNE is restarting, try again in a moment." While pictures are paused: `{"detail", "code": "image_generation_paused", "page": "/pictures"}` ([Paused](#paused-image_gen_enabledpaused)). |

`position` is 1 for a job that will start next, counting the running job (an idle server gives 1 and starts at once). `eta_seconds` is the estimate for the whole wait from now: the running job's remainder, the jobs queued before it, and its own median time. Estimates are the median of the last nine real jobs of that size, seeded with the figures above.

### `GET /v1/app/images/{id}`

```json
{"id": "...", "status": "generating", "elapsed_seconds": 41, "eta_seconds": 81}
{"id": "...", "status": "completed", "elapsed_seconds": 86,
 "image": {"b64_png": "...", "width": 768, "height": 768, "seed": 123456789}}
{"id": "...", "status": "refused", "elapsed_seconds": 6,
 "error": {"detail": "I can't make ...", "code": "prompt_refused", "category": "violence"}}
```

`status` is one of `queued`, `checking` (the prompt is being read), `generating`, `reviewing` (the picture is being checked), `completed`, `refused`, `failed`, `cancelled`, `expired`. A queued job also has `position`; a running one has `eta_seconds`, the estimate for the whole job from submission, never less than elapsed plus 5. Errors: `error.code` is `prompt_refused` or `image_refused` (with `category`), or for `failed`: `engine_restarting`, `engine_busy`, `generation_failed`, `generation_timeout`, `image_generation_unavailable`.

Ids are random 128-bit URL-safe strings. **Only the network that created a job can read or cancel it**; anyone else (and the other endpoint family) gets `404 {"code": "image_not_found"}`. Jobs are held in memory, so a coordinator restart forgets them (404).

### `POST /v1/app/images/{id}/cancel`

Answers the job as `GET` does. A queued job is cancelled at once; a running one stops at its next step (its status reads `cancelled` immediately). Repeating it is safe; a finished job is returned unchanged.

## Pictures from the phone chat (2.6.2)

A phone user can just ask the chat ("hey, can I get a picture of a fox in the snow?"). The phone model is offered a `create_image` tool; the coordinator makes the job, and the phone shows it exactly as it shows one made from the dialog.

### Who is offered the tool

`POST /v1/app/chat/completions` takes an optional `"features"`: a list of strings. `"image_jobs"` means: this client shows the `image_job` and `image_refused` events below. Unknown values, and any other type (a string, an object, a list of lists), are ignored, never an error. Android 1.0.6 and the 2.6.2 web chat send `["image_jobs"]`.

The tool and its line in the system prompt are offered only when the request's features include `image_jobs` **and** pictures can be made right now (the test behind `capabilities.image_generation`: switched on, installed, vision on, the engine up and not restarting), or are paused (then every call is refused with the pause's words, see [Paused](#paused-image_gen_enabledpaused)). It is asked again when the model calls the tool, because that can come a minute later. A request without the feature is answered exactly as in 2.6.1: no tool, no picture line, and image generation is not even asked about. Lookups and pictures can be offered together.

### The tool

`create_image(prompt, shape?)`, defined in `engine/app_pool.py` (`IMAGE_TOOL`):

| Argument | |
|---|---|
| `prompt` | Required text: the picture described in English (subject, setting, style, colours, lighting, composition), under 1,000 characters, never a real named person. It goes through exactly what a dialog's prompt does: cleaned, measured, term list, charge, hourly cap, queue. |
| `shape` | `square` (768x768), `portrait` (768x1024) or `landscape` (1024x768). Missing or unknown is square. The model never chooses 1024x1024, which takes twice as long. |

When the tool is offered the system prompt ends with: " You can create pictures with the create_image tool: when the user asks for a picture of something, call it with a detailed description instead of describing a picture in words. The picture appears below your reply about a minute and a half later, so reply in one short sentence. You cannot change or edit a photo the user sent; offer to create a new picture from a description instead."

**One picture per answer.** A second `create_image` call in the same answer, in the same round or a later one, is told "Only one picture per message." and makes no job. A first call that was refused still counts as the answer's one picture.

**A plain ask is required.** Like a question about now is required to look something up, a newest user message that plainly asks for a picture (`IMAGE_ASK` in `engine/app_pool.py`) has its first pass sent with `tool_choice: "required"`, held like a required lookup (nothing reaches the phone, at most 384 tokens: room for a whole description). If the model calls `create_image`, its own description is used. If it talks instead, its text is dropped and the server makes the call itself with the user's own words (the first 1,000 characters, square). `IMAGE_ASK` matches only a sentence that *begins* as a request: "draw me a fox", "can you make a poster of ...", "can I get a picture of ...", "I'd like a logo for ...", "show me pictures of ...". It does not match questions ("how do I draw a cat?", "who painted this?"), requests in code or words ("make an svg logo", "paint me a picture in words"), figures of speech ("draw a conclusion", "sketch a plan") or things that only mention a picture ("a picture book"). When the model had to be overruled, the log says so (once per such answer, without any of the words), which is how to tell whether the prompt needs work. It is a safety net, never a gate: whatever it misses, the model may still call the tool itself, and a false match costs a picture (a trial chat and one of the hour's few), which is why it is conservative. When a question is both a plain ask and about now, the picture comes first.

### What the phone is sent

Events in the answer's stream, before the model's reply text when the call was required (when the model chose to call it, after whatever it had already written). They carry no `choices`, so an OpenAI client skips them; there is no `searching` event for a picture.

```
data: {"dlne": {"event": "image_job", "id": "...", "prompt": "A red fox in fresh snow ...", "size": "1024x768", "position": 1, "eta_seconds": 111}}
data: {"dlne": {"event": "image_refused", "code": "prompt_refused", "detail": "I can't make images with nudity or sexual content. ..."}}
```

| `image_job` | |
|---|---|
| `id` | The job's id. Poll `GET /v1/app/images/{id}` as for any phone job. |
| `prompt` | The cleaned description the job runs (a client keeps it as `[Picture created: <prompt>]` in its history). |
| `size` | `768x768`, `768x1024` or `1024x768`. |
| `position`, `eta_seconds` | As in the `202` answer: `position` counts the running job, `eta_seconds` is the whole wait from now. |

`image_refused` carries the `code` and `detail` that `POST /v1/app/images` would have answered, and nothing else (no category, no retry time): `prompt_refused` (the term list), `prompt_empty`, `prompt_too_long`, `image_quota` (the hour's cap, or the trial used up on a network that is nobody's contributor), `image_busy`, `image_generation_unavailable` (the engine restarted since the tool was offered), `image_generation_paused` (pictures are paused; its `detail` ends with " More: " and the `/pictures` page's full address), and `generation_failed` for an unexpected error while queueing, the one code the endpoint has no answer for. A refusal costs nothing. A prompt the checks refuse later is not an `image_refused` event: the job is made and then ends `refused`, as from the dialog.

**What the model is told** (the tool result): "The picture is being made and will appear below your reply in about N seconds. Tell the user in one short sentence. Do not describe it as if you could see it, and add no link or placeholder." (N is the event's `eta_seconds`); or "The picture was not made: <detail> Tell the user briefly and kindly, in your own words."; or "Only one picture per message."

### The job

Owner: the network the chat request came from. Kind: `app`. So only that network can poll or cancel it, and its checks take a phone lane while the engine has them. It then runs the pipeline above unchanged. As on the dialog endpoint the chat's `chat_id` is not kept with it. Both routes call the same function (`_create_image_job` in `engine/coordinator.py`), so a refusal can never read differently on one of them.

### Which machine writes the description

The tool call runs on the coordinator whichever machine answers the chat: the engine's phone lanes, or a CPU-pool machine when the engine cannot (which can be a volunteer's). The machine that answers writes the description and reads the result sentence above, as it reads the rest of the chat. The picture is only ever made and checked on the coordinator's media card, never sent to a volunteer. (The checks run on the coordinator's own engine, which, like every request to it, can use volunteers' graphics cards for some of the model's layers; those cards see only intermediate numbers.)

## The coding-side endpoints (IDE)

`POST /v1/images`, `GET /v1/images/{id}`, `POST /v1/images/{id}/cancel`: the same bodies and answers, without `chat_id`. Creating a job passes the same gate as `POST /v1/chat/completions` (`check_contrib_clause`: the coordinator host, an API key, a worker, or a free-trial chat; the same 403 when the trial is used up), so a trial caller spends one chat per picture. Polling and cancelling need only the id and the network that made the job: polling must not spend the trial. The extension's `generate_image(prompt, path?, size?)` agent tool uses these.

## The status

`GET /v1/cluster/status`:

```json
{
  "capabilities": {"vision": true, "image_generation": true},
  "image_generation": {
    "queue": 0, "busy": false,
    "eta_seconds": {"768x768": 80, "1024x1024": 153},
    "sizes": ["768x768", "1024x1024", "768x1024", "1024x768"],
    "default_size": "768x768",
    "per_hour": 6
  }
}
```

- With `IMAGE_GEN_ENABLED` off (the default) neither appears.
- Enabled but unavailable right now: `capabilities.image_generation` is `false` and there is no block.
- Paused: `capabilities` has `"image_generation": false, "image_generation_paused": true`, there is no
  `image_generation` block, and a top-level `images` says why (the same sentence the endpoints answer):

  ```json
  "images": {"paused": true, "note": "Pictures are paused for now: DLNE's graphics card is busy running the smarter coding model. They will be back.", "page": "/pictures"}
  ```

  `images` appears only while paused; with pictures on or off the status is as above.
- `queue` is the jobs waiting, `busy` whether one is running, `eta_seconds` the current median per size, `per_hour` the configured `IMAGE_PER_HOUR`. Pages should read these, never type them.

## Paused (`IMAGE_GEN_ENABLED=paused`)

Since 2026-10-30, for when the media card is busy with something else: on TheWall the RX 580 runs a third of the smarter
coding model (`MEDIA_GPU_SHARED=true`), and a picture needs the card to itself. Paused is off as far as the machine goes
(nothing loads or starts `sd-server`, no model file is looked at, the canary decoys are those of a server without
pictures) but it is never silent. Everything ends at one sentence, `PICTURES_PAUSED_NOTE` in `engine/pictures_page.py`:

> Pictures are paused for now: DLNE's graphics card is busy running the smarter coding model. They will be back.

| Who asks | What they get |
|---|---|
| Any of the six endpoints (`/v1/images`, `/v1/images/{id}`, `/v1/images/{id}/cancel` and the three `/v1/app/...`) | `503 {"detail": <the sentence>, "code": "image_generation_paused", "page": "/pictures"}`, `Cache-Control: no-store`. Nothing is queued or charged, and a job made before the pause is not served either. |
| The phone app or the web chat, asking in words | The model is still offered `create_image` (so a plain ask is still required to call it, and the model never has to guess). Every call is refused: the stream gets `{"event": "image_refused", "code": "image_generation_paused", "detail": "<the sentence> More: https://dlne.rossgilsonlabs.dev/pictures"}`, and the model is told "The picture was not made: <that detail> Tell the user briefly and kindly, in your own words." Nothing is queued or charged. |
| The web chat's "+" | A greyed "Pictures paused" item where Create image would be, a link to `/pictures` (in a new tab). The Android app is unchanged: it shows no picture button, as with pictures off, and the chat's answer says why. |
| The status | As in [The status](#the-status) above. |

The address after "More:" is the coordinator's public one (`PUBLIC_URL` in `engine/landing.py`). The public name passes
only some paths to the coordinator (Caddy on TheWall), so `/pictures` must be one of them.

### The page, `GET /pictures`

Public, like `/chat`, and served with the chat's headers (the same Content-Security-Policy, `X-Frame-Options: DENY`,
`nosniff`, no referrer). It reads `/v1/cluster/status` when it opens, every minute while it is in view and whenever it is
shown again, and says one of four things: **Pictures are paused** (with the status's own `images.note`), **Pictures are
on** (with a link to the chat), **Pictures aren't available right now** (off, or on but not ready), or **DLNE is
restarting** (no status). Nothing on it is typed in: the note comes from the status, so the page and the chat can never
word it differently. Tests: `tests/test_pictures_page.py`.

## For client authors

- Show "Create image" only while `capabilities.image_generation === true` (missing means false). While `images.paused`
  is `true`, a client may show a greyed "Pictures paused" in its place that opens `images.page` (the web chat does);
  one that does not know the key simply shows no picture button, as with pictures off.
- Poll every 2 s while visible, 5 s in the background. Show elapsed time against `eta_seconds`, not a progress bar (there are no step previews). A `GET` is cheap until `completed`, which carries the whole PNG (about 1 MB at 768x768): stop polling then.
- Show the server's `detail` for every refusal and error. Show `Retry-After` for a 429.
- A picture made from the Create image dialog is a card in the conversation whose saved text is `[Image: <prompt>]`; never send the picture's bytes to the chat model. (The Android app leaves those cards out of the history it sends; the web chat sends the placeholder text. A reply that the chat itself made a picture for is different: its history text is `[Picture created: <prompt>]`, see below.)
- Save with the picture's own bytes; they carry no metadata.
- **Asking the chat for a picture (2.6.2).** Send `"features": ["image_jobs"]` with each chat request, and only if the client shows what comes back: the server offers the model its picture tool to clients that say so. On `image_job` show a job card under the streaming reply (stage, elapsed against the estimate, Cancel) and poll the `id` as above; several can run at once, one per reply, so key them by id. On `image_refused` show its `detail` as a quiet notice under the reply. In the history send a reply that made a picture as its text followed by `\n\n[Picture created: <prompt>]` (a reply with no text is just the line), so "make it bluer" can reuse the description; a refused, failed or cancelled one adds nothing. While the tool is on offer, the server turns that last line back into the `create_image` call it was (and a short "The picture was made and shown to the user." result) before the model reads the history, because a model shown the bare line tended to type it out instead of calling the tool; so the line must be the very end of the reply's text. Without the tool the line stays as text.
- A phone on a contributor's network never meets the free-trial wall for a picture; one that does gets the `image_quota` words above, which say how to avoid it.

## For the maintainer

- `canary` decoys: while the coordinator offers images, a decoy request to a coding member carries the same ninth tool and prompt line the extension adds (`generate_image`), byte for byte (`engine/canary.py`, `tests/test_image_canary.py`, which reads `vscode-extension/src/agent/imageTool.ts`). With images off the decoys are unchanged. The prompt itself never goes to a coding member.
- Tests: `tests/test_image_policy.py`, `test_image_member.py`, `test_image_pool.py`, `test_image_endpoints.py`, `test_image_canary.py`, `test_image_packaging.py`, and for the pause and its page `test_pictures_page.py`; for the phone chat's tool `test_phone_pictures.py` (the stream, `IMAGE_ASK`, a fake engine on a phone lane that calls the tool, the 2.6.1 pin) and for the contributor rule `test_contributor_networks.py`. The real `sd-server` is not needed; a fake speaks its API.
- The phone chat's picture tool is not the extension's: the canary decoys copy `generate_image` (the coding agent's tool), and nothing in `engine/canary.py` knows `create_image`, which only ever goes to the model that answers a phone.
- Moving to a newer stable-diffusion.cpp: change the three pins in `scripts/package_sd.py`, run those tests, measure on TheWall, update the figures above and `SEED_ETA_SECONDS` in `engine/image_pool.py`.
