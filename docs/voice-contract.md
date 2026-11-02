# DLNE 2.8.0: voice (speech to text), the contract

**Owner, 2026-11-02:** "add the voice, this is like my 5th time putting it on the planner and not getting it". The planner
has put voice on TheWall's RX 580 since 30 Oct: whisper.cpp large-v3-turbo next to the vision projector and pictures. This
file is what every builder builds to. Nothing is deployed or published without the owner's OK.

## What it is

Speak instead of type. A mic button records a short clip; TheWall turns it into text; the text lands in the message box,
where the person can read and fix it before sending. Speech to text only: DLNE does not speak answers back (out of scope).

## The server (coordinator 2.8.0)

- **Engine:** whisper.cpp's `whisper-server`, a Vulkan build, on the media card (`MEDIA_GPU`, TheWall's RX 580), started
  lazily on the first clip and unloaded after `VOICE_IDLE_UNLOAD_MINUTES` (default 20) without one, like the picture engine
  (`engine/image_member.py`: same lifecycle, same media-card checks, same "never on a shared card" rule).
- **Model:** `ggml-large-v3-turbo-q8_0.bin` (whisper.cpp's own conversion, about 0.87 GB), pinned by size and SHA-256 in
  `scripts/voice_models.json` and fetched by `scripts/fetch_voice_models.py` into `models/voice/` (as image models are).
- **Memory:** needs `VOICE_MIN_FREE_VRAM_GB` free on the media card to start (set from a measurement on the RX 580; the
  planner's budget was about 0.9 GB; measured 1.07 GB peak on the RX 580, default 1.4). With the vision projector (about 1 GB) and a picture at its peak (about 4.7 GB) it
  still fits the 8 GB card; if a picture is being drawn and the card is short, the clip waits for it (it never evicts it).
- **Packaging:** the whisper.cpp build ships like the picture engine: `server-whisper.tar.gz` built by
  `scripts/package_whisper.py` from a pinned upstream release (verified against its published SHA-256) or, if upstream
  publishes no Linux Vulkan build, a pinned source tag built reproducibly; signed into the release manifest
  (`whisper_build`, `server_whisper_sha256`, `server_whisper_size`; older nodes ignore them); unpacked to `bin-whisper/`.
- **Endpoint:** `POST /v1/audio/transcriptions`, OpenAI-compatible multipart: `file` (required), `model` (ignored, any
  value), `language` (optional ISO code; default: detect), `response_format` (`json` default, or `text`). Answer
  `{"text": "...", "language": "en", "duration_seconds": 4.2}`.
  - **Audio in:** WAV only, PCM 16-bit, mono, 16 kHz (clients convert; the server needs no ffmpeg: TheWall runs without
    it on purpose). At most `VOICE_MAX_SECONDS` (default 60) and 2 MB. Anything else: 400 `voice_bad_audio` with a plain reason.
  - **Who:** the same authentication and tiers as chat (`request.state.dlne_tier`); key holders first in the voice queue
    (as in the picture queue). A clip does not use a free-trial chat. At most `VOICE_PER_HOUR` clips a network an hour
    (default 60; 429 `voice_quota`), `VOICE_QUEUE_MAX` waiting (default 8; 503 `voice_busy`).
  - **Privacy:** the audio is held in memory only, never written to disk, never logged, never kept after the answer; the
    text is returned and not stored. Logs say only "a clip of N s, language L, took T s".
  - **Off or unavailable:** `VOICE_ENABLED=false` (default false; TheWall sets true) or a missing model/binary: 503
    `voice_unavailable` with a plain sentence. `VOICE_ENABLED=paused` answers 503 `voice_paused`. The engine failing on
    a clip answers 502 `voice_failed` (the clip is not counted against the hour). A clip waits at most
    600 s (`VOICE_REQUEST_TIMEOUT_SECONDS` in engine/coordinator.py, not a config key) for its turn and answer, then 503 `voice_busy` with `Retry-After: 30`.
- **Status:** `/v1/cluster/status` gains `capabilities.voice` (true only when enabled and the files are present) and
  `voice: {"queue", "busy", "max_seconds", "languages": "auto"}`. Clients show the mic only while `capabilities.voice` is true.
- **Takeover (2.7.x leases):** a lease that takes the media card pauses voice exactly as it pauses pictures (`voice: paused`
  in `dlne_now`).
- **Config keys** (engine/config.py, documented in docs/operator-reference.md): `VOICE_ENABLED`, `VOICE_MODEL` (file name in
  models/voice), `VOICE_THREADS` (default 2), `VOICE_CPU_MASK`, `VOICE_MAX_SECONDS`, `VOICE_PER_HOUR`, `VOICE_QUEUE_MAX`,
  `VOICE_IDLE_UNLOAD_MINUTES`, `VOICE_MIN_FREE_VRAM_GB`.
- **The canary:** if any client request shape changes (none should: a voice clip is its own endpoint), canary parity stays.

## The clients

Every client: a mic button beside the message box, shown only while the status says `capabilities.voice`. Tap to start,
tap again to stop (a visible timer and a stop at `max_seconds`); the clip is converted to WAV 16 kHz mono on the device,
sent, and the returned text is inserted at the cursor (never sent automatically). The first use asks for the microphone the
platform's way and says once that the clip goes to the DLNE server to be turned into text and is not kept. Errors in plain
words (`voice_quota`: "Voice is used up for this hour"; `voice_paused`/`voice_unavailable`: the server's sentence).

- **Web chat** (`engine/chat_page.py`, the `/chat` page the coordinator serves, phone and desktop browsers):
  `getUserMedia` + `AudioContext` to record and resample to 16 kHz mono PCM, WAV-encoded in JS; no external libraries.
- **Android app** (`android/`, "DLNE Chat"): `AudioRecord` at 16 kHz mono PCM, WAV header in Kotlin, `RECORD_AUDIO`
  permission asked at first use; version 1.0.7 (versionCode 8). Built and tested, not published.
- **VS Code extension (owner, 2026-11-02: "voice in coding as well"), extension 1.7.0:** VS Code webviews cannot use the
  microphone, so the extension HOST records with the platform's own recorder and the panel only shows the button and state:
  Linux `pw-record`, else `parecord`, else `arecord` (asked for 16 kHz mono S16 WAV directly); Windows a PowerShell step
  using the built-in winmm MCI recorder (`mciSendString`, waveaudio, 16-bit 16 kHz mono, saved to a temp WAV that is deleted
  right after sending); macOS `sox`/`rec` or `ffmpeg` if present. No recorder found: the button says what to install. The
  WAV goes through the extension's own client (same headers) to `/v1/audio/transcriptions`; the text goes into the DLNE
  Agent panel's message box at the cursor. Shown only while `capabilities.voice` and a recorder exists; a setting
  `dlne.voice.enabled` (default true) hides it. A keybinding (e.g. Ctrl+Alt+V) toggles recording. The temp file never
  outlives the request.
- **DLNE Status page** (`/status/` on the DLNE site): a "Voice" line (on / paused / off, queue).

## Conversation mode (2.8.2, owner 2026-11-02: "Both, switchable", "The device's own voice")

A setting chooses what the mic does. **Dictate** (the default, everything above): the text lands in the box and the person
sends it. **Conversation**: the message sends itself and the answer is read aloud by the device's own text-to-speech.
Nothing changes on the server: no new endpoint, no audio leaves the server, TheWall makes no speech.

- **The setting:** "Voice mode: Dictate / Conversation". Web chat: in the menu, remembered in localStorage
  (`dlne.voiceMode`). Android: Settings screen (SettingsStore). VS Code: `dlne.voice.mode` (`"dictate"` default,
  `"conversation"`) plus a command "DLNE: Toggle Voice Conversation Mode". It is only offered where the mic is.
- **Sending:** in Conversation, when a clip comes back as text, the text is put in the box at the cursor exactly as in
  Dictate and then the whole box is sent, as if the person pressed Send (so a half-typed draft goes with it). Empty text
  ("nothing heard") sends nothing. Every rule that blocks Send (busy, offline, no privacy OK) blocks it here too, and the
  text then stays in the box.
- **What is read aloud:** only the answer to a message that was sent by voice in Conversation mode (typed messages are
  never read). It starts as soon as the first sentence of the answer is complete and goes on sentence by sentence while
  the answer streams. When the server retracts what it wrote so far (it does that before looking something up), speech stops at once and the fresh answer that follows is read from its start; the retracted text is never read again. Pictures: "I made a picture. It's in the chat."
- **Speech text** (one pure function per client, `speakable(markdown) -> string`, tested with the vectors below):
  fenced code blocks become "There's code in the chat." (once per block); tables become "There's a table in the chat.";
  inline code keeps its text; links keep their text, bare URLs are dropped; heading, list, quote and emphasis markers
  and citation marks like [1] or [2][3] are removed; whitespace collapses. Sentences split after . ! ? (and the
  newline between paragraphs or list items), never inside a number like 3.5 or an abbreviation like "e.g.".
- **Stop and interrupt:** while it speaks, the mic button shows a stop-speaking state (a speaker with a stop square;
  label "Stop speaking"). Tapping it stops speech. Starting a recording, sending anything, opening another conversation,
  leaving the screen, turning Conversation off or a new answer starting all stop speech at once (a retract stops it too, and reading starts over with the fresh answer). Speech never queues
  across answers.
- **Voice and language:** the device's default voice for the language the server detected for the clip
  (`language` of the transcription), else the device default.
- **No speech available** (no voices installed, the platform's engine fails): the answer stays text only and the person
  is told once, in plain words, how to add a voice (Android: Settings > Accessibility > Text-to-speech; web: the
  browser/OS voices; VS Code: below).
- **Web chat:** `speechSynthesis` + `SpeechSynthesisUtterance`, one utterance per sentence (Chrome cuts long
  utterances). iPhone/iPad Safari only lets a page speak after a tap: the mic tap that starts the recording also
  "unlocks" speech (speak an empty utterance, or `speechSynthesis.resume()`, inside that tap's handler).
- **Android:** `android.speech.tts.TextToSpeech`, created when Conversation is first used and shut down with the
  screen; `QUEUE_ADD` per sentence; audio attributes `USAGE_ASSISTANT` with transient audio focus while speaking; nothing on the main
  thread that blocks.
- **VS Code:** spoken by the extension HOST with the platform's speech, never with text on a command line that a shell
  reads: Windows a PowerShell step with `System.Speech.Synthesis.SpeechSynthesizer` reading the text from stdin; macOS
  `say -f -` (stdin); Linux `espeak-ng --stdin`, else `espeak --stdin`, else `spd-say` (text as one argv entry, spawned
  without a shell). None found: the setting says what to install. In the DLNE Agent panel the agent's reply prose is
  read (not tool calls, file edits, terminal output or diffs), at most 1,500 characters a turn, then "The rest is in
  the chat."

**Shared test vectors** (each client's `speakable` and sentence split must give these):

| Markdown | Spoken sentences |
|---|---|
| `**Paris** is the capital [1].` | `Paris is the capital.` |
| `Run this:\n\n```bash\nls -la\n```\n\nDone.` | `Run this:` / `There's code in the chat.` / `Done.` |
| `See [the docs](https://x.test/a) or https://y.test/b.` | `See the docs or.` (a bare URL is dropped) |
| `- one\n- two` | `one` / `two` |
| `It costs 3.5 dollars, e.g. today. Really!` | `It costs 3.5 dollars, e.g. today.` / `Really!` |
| `\| a \| b \|\n\|---\|---\|\n\| 1 \| 2 \|` | `There's a table in the chat.` |
| `Use \`git status\` first.` | `Use git status first.` |

## Out of scope

Speech from TheWall (a server voice); a hands-free loop that listens again by itself; live streaming transcription;
translation.

## Release

2.8.0 (server), Android 1.0.7, extension 1.7.0, the site's status line. Each released only with the owner's OK; public GitHub only when named.
Conversation mode: 2.8.2 (the web chat it serves), Android 1.1.0 (versionCode 11), extension 1.8.0.
