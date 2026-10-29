# Privacy: what DLNE sends, to whom, and what they can see

This page is about the **coding agent in VS Code** (chat, agent and autocomplete). It says where your
code goes, what is removed first, how it is protected on the way, and what the machine that answers
can and cannot see. It also says plainly what none of this can promise.

The phone chat and the Android app are a separate path; see
[The phone chat](#the-phone-chat-is-separate).

DLNE has three ways to chat in the editor: `@dlne` in VS Code's Chat view, the chat in the DLNE sidebar, and DLNE
Swarm in VS Code's model picker. The first two are DLNE's own agent and keep every promise on this page, the full
never-read rule included. The model picker is VS Code's own agent with the swarm as its model: there DLNE refuses the
excluded files it can see by path, redacts secrets and answers only on trusted machines (unless you chose
`anyMember`), but VS Code's own tools may still put file text into a request, so the never-read rule is not promised
for it. See [DLNE Swarm in VS Code's model picker](#dlne-swarm-in-vs-codes-model-picker).

Coding members (volunteers' GPUs that each run a whole coding model) are **off by default** on a
coordinator (`CODING_MEMBERS_ENABLED=false`). Until an operator turns them on, every coding request is
answered by the coordinator's own engine, and the member parts of this page do not apply. The parts about
what is removed in the editor apply either way.

## The short version

**A coding member has to read your request to run the model, so it sees the code it is sent.** Nothing on
this page changes that. What follows is which requests go to members, and what protects you:

- **Public repositories** may be answered by any coding member. **Anything else** is answered only by
  the coordinator's own engine or by machines the coordinator's operator has marked as trusted, unless
  you opt in.
- **Secrets are removed in your editor**, before a request leaves it: API keys, tokens, private keys and
  passwords in common formats. DLNE's own agent (`@dlne` and the sidebar chat) also never reads files such as `.env`
  and `*.pem`; VS Code's own agent on DLNE Swarm is the exception (see
  [below](#dlne-swarm-in-vs-codes-model-picker)).
- A member **is not told who you are**: no IP address, no API key, no account.
- Members run with settings that keep prompts **out of logs and off disk**.
- The coordinator sometimes sends a member a **decoy** (a made-up link) to catch one that snoops.
- Requests and answers are **encrypted on the way** to and from a member. The coordinator (TheWall, for the
  public swarm) relays bytes it cannot open, and the tunnel and the internet between it and the member
  carry nothing readable. This protects the trip; it does not hide anything from the member that answers.
- The coordinator is the operator's own machine, and **you trust it**: it receives requests to its own
  engine in the clear, and it hands out each member's public key.

## Where a request can go

| Where | Who it is | Sees your text? | How it travels |
|---|---|---|---|
| The coordinator's own engine | The machine the coordinator runs on (TheWall, for the public swarm). Always trusted. The fallback when no member fits. | Yes. The coordinator is the one you point the extension at. | Plain JSON over HTTPS to the coordinator. |
| A trusted member | A machine the operator has listed by its identity key (`TRUSTED_MEMBERS`; see [Who counts as trusted](#who-counts-as-trusted)). Trust is set by the coordinator; a member cannot claim it. | Yes. It has to read the request to answer it. | Encrypted on the way. The coordinator relays bytes it cannot open. |
| Any coding member | Any volunteer's machine that has joined and passed the speed test. | Yes. It has to read the request to answer it. | Encrypted on the way. The coordinator relays bytes it cannot open. |

When the coordinator's own engine answers, it may use other contributors' GPUs to run the model. A
machine that only holds part of a model processes the model's intermediate numbers for your request, not
your text.

## Which requests may go to which member

The extension works out a **privacy mode** for each workspace and sends it with the request.

| Mode | When | Who may answer |
|---|---|---|
| `open` | The workspace's git remote is a **public repository**. | Any member. |
| `trusted` | Everything else. This is the default. | The coordinator's own engine, or a trusted member. |
| `any` | You set `dlne.privacy` to `anyMember`. | Any member. |

The setting `dlne.privacy` has three values:

- `auto` (the default): public repositories are `open`, everything else is `trusted`.
- `trustedOnly`: always `trusted`, even for a public repository.
- `anyMember`: always `any`. You are choosing to let any member see this workspace's requests.

**One exception: VS Code's own agent on DLNE Swarm** (the Chat view's model picker, see
[below](#dlne-swarm-in-vs-codes-model-picker)). Its requests are never `open`: `auto` and `trustedOnly` both give
`trusted` for it, even for a public repository, and only `anyMember` gives `any`.

How "public" is decided: the folder has exactly one git remote, its host is exactly github.com,
gitlab.com or codeberg.org, and one unauthenticated lookup of that repository (no credentials, no cookies,
no DLNE headers) says it is public. **If anything fails or is unclear, the workspace is treated as
private**: enterprise hosts, several remotes, a lookup that times out or is refused, anything odd. A
"public" answer is remembered for 1 hour, so a repository that is made private stops counting as public
quickly; a "not public" answer for 24 hours. The coordinator never upgrades a mode, and a request with no
mode counts as `trusted`.

**A public repository counts as public even for changes you have not pushed.** The mode follows the
repository your folder is a clone of, not whether the files in front of you are published. If you are working
on something unpublished in a clone of a public repository, set `dlne.privacy` to `trustedOnly`.

**A cloned repository cannot widen this behind your back.** DLNE does not run in a workspace you have not
trusted in VS Code. And once you have, a folder's or workspace's own `.vscode/settings.json` can only make
`dlne.privacy` narrower than your own setting (`trustedOnly`), never wider: an `anyMember` written by a
repository is ignored, and so is its `auto` over your `trustedOnly`. The settings that decide where your code
and your key go (`dlne.serverUrl`, `dlne.apiKey`, `dlne.autoApproveReads`, `dlne.autoUpdate` and all
`dlne.worker.*`) are read from your user settings only.

**While coding members are off (the default), there is no lookup.** The extension only checks a repository
once the coordinator's status says coding members are on. With them off, nothing about your repository
leaves your machine beyond the request itself.

## Who counts as trusted

A trusted machine is one the operator has listed by its **identity key**, not by a name the machine
claims. Every coding member makes a long-term Ed25519 identity key once and keeps it on its own disk. Its
heartbeat is signed with that key, and the coordinator drops a heartbeat whose signature does not check
out. The member's **trust id** is derived from the key (the first 32 hex characters of the SHA-256 of the
public key). The operator puts the trust ids of their own machines in `TRUSTED_MEMBERS`, and a member shows
its trust id in its log and UI.

This is deliberate. A worker id is just a label, and the key that signs heartbeats for the whole swarm
ships in every extension, so anyone can claim any worker id. A worker id is never read as trust.

## What is removed before anything leaves your editor

Two things happen in VS Code before the extension sends anything: secrets are replaced in the files the agent
reads, the chat context and the autocomplete prompts, and DLNE's own agent leaves some files unread. Both apply to
requests to the coordinator's own engine too, not only to members.

**Secrets are replaced.** API keys, tokens, private keys and passwords in common formats are replaced
with `[REDACTED]`. The model sees the placeholder, never the value.

**Some files are never read.** DLNE's own agent (`@dlne` and the sidebar chat) never reads these into a request:

- files your `.gitignore` ignores,
- anything matched by a `.dlneignore` file in your workspace (add paths there to keep more out),
- `.env` and `.env.*` files, `*.pem`, `*.key`, `id_rsa*`, `.npmrc`, `.pypirc`,
- cloud credential files.

VS Code's own agent on DLNE Swarm reads files with VS Code's tools, not DLNE's: what DLNE does there, and what it
cannot do, is in [DLNE Swarm in VS Code's model picker](#dlne-swarm-in-vs-codes-model-picker).

**What this does not catch.** Redaction recognises common formats. A secret in a shape it does not know,
a password written in a sentence, or a customer's data pasted into a prompt is sent as written. Treat the
filter as a safety net, and keep real secrets out of prompts.

**What you attach follows the same rules.** Files, `#file` references and the text you select, attached to an `@dlne`
message, are read by the agent's own file reading: a private file is not read (and you are told), secrets in what is
read are replaced, a very large file is attached in part or not at all (the agent reads the rest itself, under the
same rules), and anything that is not text, an image included, is not read.

## DLNE Swarm in VS Code's model picker

With VS Code 1.104 or later, the Chat view's model picker lists **DLNE Swarm**, and VS Code's own Ask, Edit and Agent
modes can run on it. This is a different path from `@dlne` and the sidebar chat: VS Code's agent decides what to
read and which tools to call, and DLNE is only the model. Most of this page holds on it, and the never-read rule holds
only in part.

**What is the same.** The request goes through the same pipeline as the agent's own: secrets in a known shape are
replaced with `[REDACTED]` before anything leaves the editor (the text VS Code builds is checked once as it arrives and
again as it is sent), and a request that goes to a coding member is sealed like any other. A workspace with several
folders open counts as private (`trusted`), as it does for `@dlne`. The API key, worker token or free trial, and the
place in line, work as for the agent; the wait is shown in the status bar and never in the answer.

**Where it may go is capped.** A request from this path is answered only by the coordinator's own engine or by a
machine the operator has marked as trusted, even for a public repository, unless you set `dlne.privacy` to
`anyMember`. A public repository does not widen it: the `open` mode does not apply on this path.

**What is different: the never-read rule is only partly kept.** On this path VS Code's own tools, not DLNE's, read
your files and hand DLNE the text. What DLNE does with it:

- Where DLNE can see the path of the file a tool result came from, and its rule excludes that file (files your
  `.gitignore` ignores, `.dlneignore`, `.env*`, `*.pem`, `*.key`, cloud credential files), the result is replaced with
  the same "excluded for privacy" notice DLNE's own agent gives, and nothing from the file leaves your editor.
- Secrets in a known shape are replaced with `[REDACTED]`, in everything.
- DLNE shows a one-time notice about this.

What it cannot do: VS Code's own tools may still put file text into a request that DLNE cannot tie to a file path (for
example the output of a command, or text VS Code attaches), and a secret in a shape the filter does not know is sent
as written. So the never-read rule is not promised for this path. `@dlne` and the sidebar chat read the files
themselves and keep the full rule, so use one of them in a workspace where that matters.

**Everything VS Code puts into its prompts goes with the request.** That is the files its tools read, and whatever
else VS Code adds as context, such as the instruction files it finds (`AGENTS.md` among them). DLNE does not add its
own memory files (`.dlne/memory.md`, `~/.dlne/memory.md`) to these requests: loading them is the agent's job, and
VS Code's agent is not DLNE's.

**What it does not do.** The entry does not take images: one in a request is refused before anything is sent. It
changes where the chat's answers come from, not what VS Code's other AI features use: while you are signed in to
GitHub, those can still call GitHub's models, as VS Code's own settings say. DLNE does not see or control that.

## The agent's memory travels with the request

The coding agent has persistent memory: the project's `AGENTS.md`, its `.dlne/memory.md`, and the user's
own `~/.dlne/memory.md`. Their text is part of every agent request, so it goes wherever the request goes:
to the coordinator's own engine, or to a volunteer's coding member, which sees it like the rest of the
prompt.

- **The same filters apply.** The text is passed through the same secret redaction as everything else. A file
  listed in `.dlneignore` is not loaded (and not written). `.gitignore` keeps `AGENTS.md` out, but not the two
  memory files: tool folders like `.dlne/` are often git-ignored, and memory must not silently stop.
- **Secrets are refused when saving.** The agent's `remember` tool will not save a fact that holds one.
  Redaction knows common formats only, so keep secrets out of these files.
- **You see what the agent saves.** By default (`dlne.agent.memory` = `auto`) the agent saves project facts by
  itself, and each one appears in the chat with an Undo. A fact about you, or one holding a link or a command
  that fetches, runs or escalates (`curl`, `sudo`, `| sh`…), is shown to you for approval before it is written.
  With `ask`, every fact asks first. The agent only updates or removes lines it wrote itself.
- **The files are yours.** They are plain Markdown; edit or delete any line. `.dlne/memory.md` is in your
  project, so whether to commit it is your choice; if you do, anyone who runs DLNE on the repository sends it
  too.
- **Off switch.** `dlne.agent.memory` set to `off` loads and saves nothing.

A task in progress is also saved between VS Code sessions, but only in the extension's private storage on
your computer. Saving it sends nothing; when you carry on, the conversation's requests go as any others do.

## Encryption on the way to the member

When a request is going to a member, the extension asks the coordinator for a route. The coordinator
answers with an opaque route id (valid for about 10 minutes) and the member's public key. The extension
then:

1. makes a fresh (ephemeral) X25519 key pair and agrees a shared secret with the member's public key,
2. derives two keys from it with HKDF-SHA256, one for the request and one for the reply,
3. encrypts the request body with ChaCha20-Poly1305 and sends it with `X-DLNE-Route: <route id>`.

The coordinator relays the sealed body to the member. The member decrypts it, runs the model, and sends
the reply back as sealed chunks, which the extension decrypts; a reply counts as complete only after a
sealed end marker, so a cut-off answer is an error and not a silently short one. The route id is opaque:
it is only a handle for picking a member. A sealed request works once and for one member: if that member
fails or is busy, the extension asks for a new route and seals again.

**What this protects:** the request and the answer are unreadable on the way. On the coordinator, which
relays bytes it cannot open, so their contents never reach its logs or its disk (it still sees that a
request happened, how big it was and which member took it). And on the tunnel and
the internet between the coordinator and the member. Requests and answers are padded to multiples of 256
bytes, so the exact length of a prompt or a token does not show. A member makes a fresh key pair when it
starts and every 6 hours.

**What it does not do:**

- **It does not hide anything from the member.** The member has to read the request to run the model, so
  it sees the code it is sent. That is what the privacy mode, the redaction, the no-trace settings and the
  canaries are for.
- **The coordinator is trusted.** It is the operator's own machine (for the public swarm, run by the
  maintainer), and it hands out the member's public key, so it is trusted not to swap it for one it
  controls. The extension has no independent way to check the key.
- **Rough size and timing** of a request can still be seen by whoever relays it.
- **Requests answered by the coordinator's own engine are not sealed.** They go over HTTPS to the
  coordinator, which can read them.
- **Images are never sent to a volunteer's machine.** A coding request that holds an image is answered by the
  coordinator's own engine only (the extension sends it unsealed, to the coordinator); a member that is sent
  one anyway, sealed, refuses it (400 `image_input_unavailable`) without reading it, because members have no
  vision projector and an image should not sit decrypted on a machine the operator does not run. Like the text of
  a request, though, an image is processed by the coordinator's shared model, whose layers can run on
  contributors' graphics cards (see [Where a request can go](#where-a-request-can-go)); those cards see only
  intermediate numbers, never the picture.

## What a member can and cannot see

A member **can** see:

- the full text of each request it answers: your prompt, and the file contents and chat context the agent
  put in it (after redaction), the agent's memory files (see
  [The agent's memory travels with the request](#the-agents-memory-travels-with-the-request)), and its own
  reply,
- how large the request is and when it arrived,
- the whole conversation as it goes. A conversation stays on the same member for up to 30 minutes when
  that member is still eligible and not full, so one member may see a conversation's requests in a row.

A member **cannot**:

- learn **who you are** from the connection. The coordinator forwards only the request body: no IP
  address, no API key, no account, no coordinator headers. The only headers a member receives are content
  type and length. What the request itself contains can still identify you: file paths, names in code or
  comments, a project's name.
- see anything redacted or excluded in your editor, because it never left. (On the model-picker path, text from an
  excluded file can still get into a request when DLNE cannot see which file it came from; those requests are capped
  to trusted machines unless you chose `anyMember`. See
  [DLNE Swarm in VS Code's model picker](#dlne-swarm-in-vs-codes-model-picker).)
- see other members' requests.

A member is a volunteer's computer. It can run DLNE's software as shipped, or something else.

## No-trace members

DLNE's member software starts its model server with settings that keep prompts out of the logs and off
disk (low verbosity, no slot save path, no prompt logging). The member proxy that receives your request
never logs request or reply bodies. The member's heartbeat says whether it runs with these settings.

That flag is the member's own report, and DLNE has no way to check it from the outside. A volunteer who
changes the software can turn the settings off.

## Canary tokens

Now and then (by default 4 times a day per member, `CODING_CANARY_PER_DAY`) the coordinator sends a member
a normal-looking coding request. It contains a decoy that belongs to that member alone: a made-up link on the
coordinator (`/c/<token>`), in a place where a real file could hold an address. If the link is ever fetched,
the member that was given it is **flagged and receives no more requests**, and the operator sees it in an
admin listing. A decoy request is sealed and padded exactly like a real one, and it is built to match the
requests the extension really sends: the same system prompt, tools and fields (tests compare them with the
extension's source), and where a file would hold a key it shows `[REDACTED]`, which is what a real request
shows. There is no decoy key: the extension replaces every key before a request leaves, so a live one would
have given the canary away. Decoys are never used on the coordinator's own engine.

**What this does:** it catches a member that takes what it sees and uses it.
**What it does not do:** it does not stop a member reading your request, and it says nothing about a
member that reads and does nothing. The format of the link is public, so a member that knows it and never
opens such a link is not caught. It is a tripwire for the careless, not a lock.

## The honest limit

**A coding member has to read your request to run the model, so it sees the code it is sent.** Encryption
keeps requests and answers unreadable on the way. It does not keep them from the machine that answers: that
machine decrypts your request to do its job, and for those seconds it holds your code in plain form.

What protects you from members is the rest of this page: private repositories go only to trusted machines
unless you opt in, secrets are removed in your editor before anything leaves it, members run with no-trace
settings and never learn who asked, and canary tokens are there to catch a member that snoops. A volunteer
who modifies DLNE's software could still read your request, keep it, or pass it on, and nothing here can
prevent that. These measures make it rarer, less damaging and more likely to be noticed. They do not make
it impossible.

So:

- For code you cannot afford to expose, **run your own coordinator** and your own workers (see the
  README), or keep `dlne.privacy` on `auto` or `trustedOnly` so that private work stays on trusted
  machines.
- Put anything that must stay out of reach in `.dlneignore`.
- Do not put secrets in prompts.

## The phone chat is separate

Everything above is about the coding agent in VS Code. The phone chat (the chat page and the Android app) is
answered by a volunteer's computer in the CPU pool, or, when the operator has given the coordinator's engine
phone lanes (`ENGINE_PHONE_SLOTS`), by the coordinator's own GPU engine: **the machine that answers a chat sees
its text**, and the DLNE server makes any web lookups for it. Each answer has a Report button; reports are kept
on the server for review (without the location or the town a local lookup used; see below). The routing,
end-to-end encryption, redaction and canaries on this page are about the coding agent and are not described or
promised for the phone chat. A photo is never sent to a volunteer's computer, though like every message to the
coordinator's own engine it is processed by the shared model, whose layers can run on volunteers' graphics cards: see
[Photos](#photos).

### Photos

The phone chat can send photos (2.6.2), when the server says it takes them (`capabilities.app_vision`: phone lanes
and a vision projector are set up). A photo goes inline with the message to the DLNE server and is looked at by
**the coordinator's own engine only**, on a phone lane. **It is never sent to a volunteer's computer**: a request
with a photo is not sent to the CPU pool, not even when the engine cannot answer. Then the phone is told "DLNE is
restarting, try again in a moment" (or "DLNE can't look at photos right now" when the server has no phone lanes), and
the person sends it again.

**What a volunteer's graphics card can see.** Your photo stays on DLNE's own server. Like every message, it is
processed by the shared model, whose layers can run on volunteers' graphics cards (the same way as for every coding
prompt: see [Where a request can go](#where-a-request-can-go)), and those cards see only intermediate numbers, never
the picture.

The server checks the file before anything else (PNG or JPEG, at most 4 a message, 4 MiB and 4096 pixels a side
each, inline data and never a link it would open), holds it in memory for that request, and **does not store it**.
Nothing is written to disk for it. The server's log holds a photo's size and an eight-character fingerprint of its
bytes (`[photo 1200x900 sha256:ab12cd34]`), never the photo; a report of an answer (`/v1/app/report`) takes text only.
To tell that a follow-up sends the same photo again (a picture already counted in the hour is not counted twice
against `IMAGE_INPUT_PER_HOUR`), the server keeps each network's photos' SHA-256 fingerprints for that hour, in memory
only and never in a file.

On the phone, before anything is sent, the app or the page turns each photo upright, shrinks it and saves it again as
a JPEG, which drops the camera details and the GPS position stored in the original. The Android app keeps the photos
in its private storage with the chat and deletes them with it; the web chat keeps them in memory only, so a reload
ends them. A photo goes again with each follow-up until a newer photo is sent, so the model can keep looking at it;
photos of older messages go only as the word "[photo]".

### Pictures (image generation, if the operator turned it on)

A picture is drawn only on the coordinator's own media card, never on a volunteer's machine or a coding member. The
description is read by the coordinator's own model and then by `sd-server`, a program on the coordinator that listens
on this machine only. The finished picture is looked at by the coordinator's model, has its metadata (stable-diffusion
writes the whole prompt into the PNG) removed, and is kept in memory for the operator's keep time after it is finished
(`IMAGE_RESULT_TTL_SECONDS`, 30 minutes unless changed), whether or not it was fetched, then dropped. Nothing
is written to disk, and the server's log holds a prompt's length and an 8-character hash, never its words. When a
picture is asked for in the phone chat (2.6.2) the description is written by the machine that answers the chat, as part
of its answer: the coordinator's own engine, or, when that cannot answer, a CPU-pool machine, which can be a volunteer's.
That machine also reads one sentence saying the picture is being made or was refused. It never receives the picture,
which is made and checked only on the coordinator. (Like every message, the description and the picture check are
processed by the coordinator's shared model, whose layers can run on volunteers' graphics cards; those cards see only
intermediate numbers.) Details in [`image-generation.md`](image-generation.md).

### Chats on the engine cannot read each other's cache

An engine remembers the start of every prompt it has read and skips it the next time, which is what makes a long
chat fast. On the phone lanes that memory is shared by all phones, and every phone chat starts with the same
public system prompt. Without more, a prompt that shared more of its start with someone else's earlier prompt
would be answered measurably sooner, and that timing would say what the other chat began with.

So each chat's system prompt ends with one line that is different for every chat and the same for every turn of
it: an HMAC of the chat's `chat_id` under a secret only this coordinator knows (`config/chat_salt.key`, made on
first use; on Linux it is readable by its owner only, and a looser file is tightened at the next start). Chats
share only the public prompt before that line, and a chat's own turns share everything. A phone cannot work out
another chat's line without the secret, and the line is never logged. It goes to the engine on the coordinator and
nowhere else: if the engine cannot answer and a CPU-pool machine takes over, that machine is sent the
conversation without the line.

The line only breaks the shared *start* of a prompt, so two more things back it up. Every phone-lane request says
`n_cache_reuse: 0`, because the engine's `--cache-reuse` would otherwise re-use a matching chunk found anywhere in a
lane's earlier prompt, which the line does not touch. And the engine is started with `--no-slots` when it has phone
lanes, which closes llama-server's `/slots` page: its JSON holds every slot's prompt and answer, and nothing in
DLNE reads it.

Only a canonical UUID (`xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`, as both our clients send) counts as a `chat_id`. A
short, constant or guessable id would put all of one client's chats in a cache anyone could share, so anything else
is treated as no `chat_id` at all. A request with no usable `chat_id` has no stable line, so its cache is switched
off instead (`cache_prompt: false`): slower, and private.

### Location (opt-in)

The web chat and the Android app can send the phone's **precise location** so local questions can be answered. It
is **off unless the user turns it on**: with the Share location switch in the settings, or by answering **Use my
location** when the chat asks "Use your location for this?" (a card above the message box, shown when a question
looks local and sharing is off; **Not now** sends the message without a location, and that chat is not asked again).
The phone's own permission prompt still applies. Even then a client sends it as `"location": {"lat", "lon"}` **only with a local question** (it looks for words such as "near me",
"nearby", "weather" or "where I live"; the list is `LocalQuestion` in the Android app and the same in the web
chat), and only with a fix **under two minutes old**. A message that is not about where the user is goes without it,
and the phone is not asked for a position for it. The server reads whatever location arrives the same way: one that
is not two finite numbers in range is ignored, never an error.

- The server **never puts it in the model's prompt** and **never logs or stores it**: located lookups skip the
  result cache, and it reaches no search engine. **The model never gets the coordinates.** When a local lookup runs,
  the results the model reads name the nearby town, and so does whichever machine answers: the coordinator's own GPU
  lanes first (that machine is TheWall), or, only when the lanes are off or the engine is down or restarting, a
  CPU-pool machine, which can be a volunteer's.
- **Open-Meteo** ([open-meteo.com](https://open-meteo.com), where the forecast comes from) gets the coordinates
  rounded to about 100 m, together with the server's own network address and never the phone's, because the
  coordinator makes the request. A forecast for a place the user names sends only that name.
- **Search engines** get a town name and nothing else. A local search (the model marks it `near_me`, or the question
  says "near me" and the like) has the phrase replaced by "near <Town, Region>". The name comes from an offline list
  on the server ([GeoNames](https://www.geonames.org), CC BY 4.0), so the coordinates never go to a map service. A
  question that already names a place ("restaurants in Rome") only loses the phrase, and one that is not local gets
  no town. If no listed town is within 50 km of the phone, no search is made at all, rather than letting the engines
  answer "near me" for the server's own address; the model is told the user's place is not known. The same goes for
  a phone that sent no location at all: a "near me" search is not made, and the model is told to ask for a town or to
  suggest turning on location sharing.
- **A report never stores the location or the town.** `/v1/app/report` keeps the question and the answer as the
  phone sent them, and an answer to a local question names the town. The server therefore remembers, in memory only
  and for six hours, which towns the located lookups of a network address used, and replaces them with "[place]" in
  that network's reports before anything is written. A town the user typed into their own question is their text and
  is kept as they wrote it if no located lookup of theirs used it.

### Image generation

When the operator turns on image generation (`IMAGE_GEN_ENABLED`, off by default; see [docs/image-generation.md](image-generation.md)),
the prompt and the image **stay on the coordinator host (TheWall)**. Nothing is
written to disk or to a log except the prompt's **length and a short hash** (the log never carries its words); the
image is held in memory for its time to live after it is finished (fetched or not), then dropped. The prompt that the generator writes
into the PNG's metadata is **stripped from the image file** before it leaves. (A picture asked for in the chat starts
as a description written by whichever machine answers that chat, which is the one exception: see
[Pictures](#pictures-image-generation-if-the-operator-turned-it-on) above.)

## What lending a GPU tells the coordinator

A worker's heartbeat carries its hardware profile: the GPU's name and memory, the RAM it lends and the RAM
its coding job may use, its CPU cores, and whether its owner is gaming. The coordinator also times how far
away the machine is (a TCP connect, or a ping through the worker's own tunnel).

Only a machine in the coordinator's **own household** (its connections come from the coordinator's own
public address) is asked for its private network addresses, so the coordinator can reach its GPU over the
home LAN instead of the internet. Nobody else is ever asked, and the coordinator dials only an address on
its own LAN.

The coordinator also remembers **which network addresses a worker's heartbeat came from, and when the last one came**,
for 24 hours (`config/contributor_networks.json`: an address and a time, no worker id and nothing about the machine), so
that phones on a network where a computer is contributing get pictures without spending the free trial. A heartbeat
from the coordinator's own machine is not recorded. The free trial itself is kept per network address in the same way
(`config/starter_quota.json`).

The public status page and the Discord swarm tile show counts only: how many GPUs and machines, how much
memory, how far away by grade. Neither shows a worker's address or the address of anyone using DLNE (until
server 2.4.9 the status page listed workers' addresses, and the address of each user with a request in
progress).

## For operators

| Setting | What it does |
|---|---|
| `CODING_MEMBERS_ENABLED` | Turns coding members on. Default `false`. |
| `AUTO_SWAP_MAX_RTT_MS` | How far away (round trip measured by the coordinator) a GPU may be to share the coding model. Default 80. |
| `CODING_MEMBER_MAX_RTT_MS` | How far away a GPU may be to run a coding model of its own. Default 120. |
| `TRUSTED_MEMBERS` | Comma-separated trust ids (derived from each member's own identity key) that count as trusted. Worker ids carry no trust, and a member can never claim it. |
| `CODING_CANARY_PER_DAY` | How many decoy requests each member gets a day. Default 4. |
| `CODING_MEMBER_MIN_DECODE_TPS`, `CODING_MEMBER_MIN_PREFILL_TPS` | Speed floors from the join test (defaults 15 and 80 tokens a second). A member under them is not sent work. |

The technical contract (formats, headers, routes) is in [`coding-members.md`](coding-members.md).

Report security problems privately to the maintainer rather than in a public issue.
