# Privacy: what DLNE sends, to whom, and what they can see

This page is about the **coding agent in VS Code** (chat, agent and autocomplete). It says where your
code goes, what is removed first, how it is protected on the way, and what the machine that answers
can and cannot see. It also says plainly what none of this can promise.

The phone chat and the Android app are a separate path; see
[The phone chat](#the-phone-chat-is-separate).

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
  passwords in common formats, and files such as `.env` and `*.pem` are never sent.
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

Two things happen in VS Code to everything the extension sends: the files the agent reads, the chat
context and the autocomplete prompts. They apply to requests to the coordinator's own engine too, not only
to members.

**Secrets are replaced.** API keys, tokens, private keys and passwords in common formats are replaced
with `[REDACTED]`. The model sees the placeholder, never the value.

**Some files are never sent.** These are never read into a request:

- files your `.gitignore` ignores,
- anything matched by a `.dlneignore` file in your workspace (add paths there to keep more out),
- `.env` and `.env.*` files, `*.pem`, `*.key`, `id_rsa*`, `.npmrc`, `.pypirc`,
- cloud credential files.

**What this does not catch.** Redaction recognises common formats. A secret in a shape it does not know,
a password written in a sentence, or a customer's data pasted into a prompt is sent as written. Treat the
filter as a safety net, and keep real secrets out of prompts.

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

## What a member can and cannot see

A member **can** see:

- the full text of each request it answers: your prompt, and the file contents and chat context the agent
  put in it (after redaction), and its own reply,
- how large the request is and when it arrived,
- the whole conversation as it goes. A conversation stays on the same member for up to 30 minutes when
  that member is still eligible and not full, so one member may see a conversation's requests in a row.

A member **cannot**:

- learn **who you are** from the connection. The coordinator forwards only the request body: no IP
  address, no API key, no account, no coordinator headers. The only headers a member receives are content
  type and length. What the request itself contains can still identify you: file paths, names in code or
  comments, a project's name.
- see anything redacted or excluded in your editor, because it never left.
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
answered by a volunteer's computer in the CPU pool: **the volunteer machine that answers a chat sees its
text**, and the DLNE server makes any web lookups for it. Each answer has a Report button; reports are kept
on the server for review. The routing, end-to-end encryption, redaction and canaries on this page are about
the coding agent and are not described or promised for the phone chat.

## For operators

| Setting | What it does |
|---|---|
| `CODING_MEMBERS_ENABLED` | Turns coding members on. Default `false`. |
| `TRUSTED_MEMBERS` | Comma-separated trust ids (derived from each member's own identity key) that count as trusted. Worker ids carry no trust, and a member can never claim it. |
| `CODING_CANARY_PER_DAY` | How many decoy requests each member gets a day. Default 4. |
| `CODING_MEMBER_MIN_DECODE_TPS`, `CODING_MEMBER_MIN_PREFILL_TPS` | Speed floors from the join test (defaults 15 and 80 tokens a second). A member under them is not sent work. |

The technical contract (formats, headers, routes) is in [`coding-members.md`](coding-members.md).

Report security problems privately to the maintainer rather than in a public issue.
