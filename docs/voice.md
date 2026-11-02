# Voice (2.8.0)

Speech to text, nothing else: a client records a short clip, the coordinator turns it into text, the text goes back. DLNE does
not speak answers. The contract is [`voice-contract.md`](voice-contract.md); the keys are in
[`operator-reference.md`](operator-reference.md).

## How it runs

- `engine/voice_member.py` runs whisper.cpp's `whisper-server` as a child of the coordinator, on the media card only
  (`MEDIA_GPU`; never a card the coding model shares; `GGML_VK_VISIBLE_DEVICES` is the card's Vulkan index, `LD_LIBRARY_PATH`
  its own folder). It starts on the first clip, waits to be ready, unloads after `VOICE_IDLE_UNLOAD_MINUTES`, and three
  failures in a row switch voice off for 30 minutes.
- The command: `[taskset VOICE_CPU_MASK] bin-whisper/whisper-server --host 127.0.0.1 --port <free port> -m models/voice/<VOICE_MODEL> -t <VOICE_THREADS> -l auto -np`.
  No `--convert`, so it needs no ffmpeg and never writes an upload to a temporary file; `-np` keeps it from printing transcripts.
- A clip is `POST /inference` (multipart: `file`, `response_format=verbose_json`, `language`, `temperature=0.0`) on the
  loopback port. `verbose_json` is used because plain `json` does not say which language was detected.
- `engine/voice_pool.py` is the queue: one clip at a time, API-key holders, hosts and GPU contributors before the free trial,
  `VOICE_QUEUE_MAX` waiting, `VOICE_PER_HOUR` clips a network an hour. It shares the media card with pictures: if a
  picture is being drawn and the card has less than `VOICE_MIN_FREE_VRAM_GB` free, the clip waits for the picture (up to
  three minutes); it never stops one.

## The endpoint

`POST /v1/audio/transcriptions`, multipart: `file` (WAV, 16-bit PCM, mono, 16 kHz, at most `VOICE_MAX_SECONDS` and 2 MB; the
header is read here, nothing decodes it), `language` (optional ISO code, default detect), `response_format` (`json` or
`text`), `model` (ignored). Answer: `{"text": "...", "language": "en", "duration_seconds": 4.2}`.

| Status | `code` | When |
| --- | --- | --- |
| 400 | `voice_bad_audio` | not a WAV of that shape, too long, over 2 MB, no `file`, a bad `language` or `response_format` |
| 403 | | a free-trial caller whose trial is used up (the trial is looked at, never spent) |
| 429 | `voice_quota` | `VOICE_PER_HOUR` reached for the network (`Retry-After`) |
| 503 | `voice_busy` | `VOICE_QUEUE_MAX` clips already waiting (`Retry-After`) |
| 503 | `voice_unavailable` | off, no binary or model, no media card, the card short of memory, cooling down |
| 503 | `voice_paused` | `VOICE_ENABLED=paused` or a takeover lease holds the media card |

## Privacy

The audio is a bytes object in the request and in the queued clip, dropped as soon as the clip has run. It is never written to
disk (whisper-server's folder stays empty and `--convert` is off) and never logged; the text is returned and not stored. The log
line is "Voice clip of N s, language L, took T s".

## Status and takeover

`/v1/cluster/status` has `capabilities.voice` (true only when enabled and the files are present) and
`voice: {queue, busy, max_seconds, languages: "auto"}`; while paused `capabilities.voice` is false and the block carries
`"paused": true`. A takeover lease that takes the media card pauses voice as it pauses pictures, and `dlne_now.voice`
says `paused`.

## Not done here

The binary, the model pin and the release manifest fields (`scripts/package_whisper.py`, `scripts/voice_models.json`) are
the packaging builder's; the web chat's mic, the Android mic and the status page's line are the clients'.
