# DLNE Coding Assistant

[![Open VSX Version](https://img.shields.io/open-vsx/v/dlne/dlne-code-assistant?color=blue&label=Open%20VSX)](https://open-vsx.org/extension/dlne/dlne-code-assistant)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**A free AI coding agent and autocomplete for VS Code, running on GPUs that its users share.**

No account, no API key, no subscription. Install it, type `@dlne` in VS Code's Chat view (or open the DLNE sidebar), and ask it to write or fix code. It reads the files in your workspace, and asks before it changes a file, runs a command, or looks outside your workspace.

> **Upgrading from 1.2.x?** Since 1.3.0, `dlne.serverUrl`, `dlne.apiKey`, `dlne.autoApproveReads`, `dlne.autoUpdate` and the `dlne.worker.*` settings are read only from your user settings. If you set one of them in a workspace's `.vscode/settings.json`, move it to your user settings (`Preferences: Open User Settings`), or DLNE uses the default. This stops a cloned repository from sending your code to another server. [More below](#settings).

- **Agent chat**: `@dlne` in VS Code's Chat view (one conversation per thread), **DLNE Swarm** in the Chat view's model picker so VS Code's own Ask, Edit and Agent modes can run on the swarm, and DLNE's own chat in the sidebar, which also serves editors that have no Chat view. [Details below](#chatting-with-dlne).
- **Ghost-text autocomplete**: suggestions inline as you type.
- **Fix problems**: `DLNE: Fix Current File Problems` sends the file's errors to the agent (`@dlne /fix` in the Chat view, or the sidebar chat where there is no Chat view).
- **Secrets stay in your editor**: API keys, tokens, private keys and passwords in common formats are replaced with `[REDACTED]` before a request leaves VS Code, and `@dlne` and the sidebar chat never read files like `.env` and `*.pem`. VS Code's own agent on DLNE Swarm is the exception: its tools read the files, so DLNE refuses the excluded files it can see by path and redacts secrets, but VS Code's own tools may still put file text into a request ([see below](#dlne-swarm-in-the-model-picker)). Private repositories are answered only by trusted machines unless you opt in. [Details below](#privacy-what-to-know).
- **On your phone**: [DLNE Chat](https://dlne.rossgilsonlabs.dev/chat) is the same swarm in a phone browser (on an iPhone: Safari, Share, Add to Home Screen), and there is an Android app (the APK is on the [DLNE website](https://dlne.rossgilsonlabs.dev); it is not on Google Play yet). Where the server offers them, the phone chat looks at photos you add with the **+** next to the message box and makes a picture when you ask for one. In VS Code, use the **Use DLNE on your phone** link in the sidebar, or the command `DLNE: Use DLNE on Your Phone`.

---

## How it stays free

DLNE runs on a **swarm**: a coordinator plus the GPUs of the people who use it.

1. **Try it**: **100 free chats**, with nothing to set up. Autocomplete does not use them up, and the status bar shows how many are left. They're counted once per internet connection on the server, so reinstalling or clearing settings doesn't reset them.
2. **Keep using it**: lend some of your computer to the swarm with one click (**Donate compute** in the sidebar).

| You lend | Coding agent | When the swarm is busy |
|---|---|---|
| **A GPU** | Unlimited, while your GPU is in use* | First in line |
| **A CPU** | 300 chats in any 5 hours (each frees up 5 hours after you used it) | Second in line |
| Nothing | The 100 free chats | After contributors |

\* "In use" means the swarm has given your GPU a job: a share of the shared model, or a coding member. A newly lent GPU gets the GPU perks for its first 30 minutes while the swarm finds it a job; a GPU still without one after that counts as a CPU contributor until it gets one.

A CPU contributor's chats show how many are left, and at the limit the agent says exactly when the next one frees up.

You choose how much to lend (`dlne.worker.maxVramGb`, `dlne.worker.maxRamGb`, `dlne.worker.allowedGpus`), and you can stop at any time.

**The Donate compute panel.** The top of the DLNE sidebar is where you set it, with sliders that snap to amounts the swarm can use, and a line under them that says what your choice powers and what you get back:

- **GPU** (a switch, the VRAM to lend, and the RAM for the GPU's model: Auto, then 16, 24, 32 GB and up to your RAM less 4 GB). From 8 GB of VRAM a GPU can be a coding member; a smaller one joins the GPU pool.
- **CPU** (a switch, the RAM to lend, and the threads). The RAM picks the phone-app model it runs: 8 GB Qwen3 4B, 16 GB Qwen3.6, 24 GB Qwen3.6 at full quality. At most half your RAM is offered.
- **Threads** (`dlne.worker.maxCpuThreads`, Auto by default): how much of your computer stays yours. Memory speed limits the work, so more threads add little. A change applies while the worker runs.
- One **Donate compute** button starts and stops. A change to the GPU or the RAM while you donate shows **Apply (restarts the worker)**. It needs a GPU with a switch on, or a CPU with at least 4 cores and 16 GB of RAM.
- **Optional add-on** (64-bit Windows and Linux): DLNE Donate, a small tray app that keeps donating when VS Code is closed. Its button downloads the app to your Downloads folder only when you click. It keeps the file only if it matches the size and SHA-256 published on the DLNE website, and runs it only if you choose **Run**. Or follow **Or download it from the DLNE page** to get it from [the website](https://dlne.rossgilsonlabs.dev/#dlne-tray) yourself. The Windows app is not signed yet: if Windows says "Windows protected your PC", click **More info**, then **Run anyway**. While the tray app donates, the panel says **Donating from the DLNE Donate tray app** and VS Code does not start a second worker.

> **Contributing works on Windows and Linux (x64 and ARM64) and on macOS (Apple Silicon and Intel).** GPUs are used through Vulkan on Windows and Linux and Metal on Apple Silicon. Intel Macs contribute their CPU (llama.cpp has no GPU support for them). The ARM64 and Intel Mac builds are the newest and the least tested.

### 8 GB of GPU memory or more? Be a coding member

A **coding member** runs a whole coding model on your GPU and answers coding requests on its own, so one request touches one machine and ordinary home internet is enough. **Each member adds about two more agents** working at the same time, so the swarm can run more agents as members join.

There is no separate switch to find. Switch on the GPU in the Donate compute panel and click **Donate compute** (the first time you are asked; choose **Lend my GPU**): when the coordinator has coding members turned on and your GPU is not on its own fast network, it gives your GPU this job instead of a share of the big model. The sidebar card, the status bar ("Coding member") and a one-time notice tell you which job you got. If you would rather not answer other people's requests, choose **Lend my CPU only**, or turn off **`dlne.worker.allowCodingMember`** (the extension writes `CODING_MEMBER_ALLOWED=false` into the engine's `config/config.txt` for you). The coding job uses the memory that is free when it starts (it steps aside when you game); set **`dlne.worker.maxRamGb`** to limit it. (That setting is also the limit of the shared-model job, which needs little, and its default of 4 GB does not apply to the coding job; the extension passes the coding limit as `--coding-max-ram`, `-1` meaning none.)

**Lend both.** The third button, **Lend both** (`dlne.worker.device` = `"both"`), lends the GPU and the CPU at the same time: the GPU does the job the swarm gives it, and the CPU also powers the DLNE phone app with the RAM you choose (you are asked on the first start; `dlne.worker.alsoCpuRamGb` holds the amount, at least 8 GB and never more than half your RAM; `dlne.worker.maxRamGb` stays the limit of the GPU's jobs). Your processor's threads are shared between the two jobs (`MAX_CPU_THREADS` is the total), the phone-app model loads before the coding model does, and when you game both step aside. The sidebar card, the status bar (`[Coding member 8GB + Phone app]`) and `@dlne /contribute` show both jobs while both run. On a machine without a usable GPU "both" is the same as "cpu".

- **You need** a GPU with **8 GB of memory or more**, and enough free system memory for the rest of the model: about **22 GB of GPU memory plus free system memory together** for the first version (for example an 8 GB GPU with 14 GB of RAM free). DLNE runs the best version of the model your machine can hold whole, using the GPU first and system memory for the rest.
- **Your machine is timed** when it joins, on reading a prompt and writing an answer. A machine under the speed floor is not sent coding work.
- **The more memory you have, the better the version you run.** About 22 GB runs the first version of Qwen3.6-35B-A3B (IQ3_S); higher-quality versions of the same model need roughly 26 GB (IQ4_XS), 30 GB (Q4_K_M) and 37 GB (Q6_K), and the larger ones also need a stronger GPU to be fast enough. Open, MIT-licensed heavyweight models are planned from 192 GB and are not available yet.
- **Your limits count.** The amounts you set with `dlne.worker.maxVramGb` and `dlne.worker.maxRamGb` are the most the member may use.
- **The model server runs at low priority**, so your computer stays usable, and the member steps aside while your own load (a game, a render) needs the machine.
- **Off until the coordinator turns it on.** Coding members are switched on by the coordinator's operator. Until then, coding requests are answered as before.

What a member can and cannot see of the requests it answers is under [Privacy](#privacy-what-to-know), and in full in [docs/privacy.md](https://github.com/RGlabs84/DistributedLLMEngine/blob/main/docs/privacy.md).

### No GPU? Your computer powers the DLNE phone app

A contributor without a usable GPU does not join the coding pipeline. Its worker joins the **CPU pool**, which answers the DLNE phone app (the [chat page](https://dlne.rossgilsonlabs.dev/chat) and the Android app) whenever the coordinator's own engine cannot: on a coordinator that gives phones lanes of its engine, that is while the engine is restarting or down. Each member runs a whole model on its CPU, and the server picks the model from how much RAM you lend:

| RAM you lend | Model your computer runs |
|---|---|
| 8 GB | Qwen3 4B, the fast overflow model |
| 16 GB | Qwen3.6 35B-A3B, the main phone model |
| 24 GB | Qwen3.6 35B-A3B at full quality, once 2 volunteers can host it |

In return you get the coding agent **2nd in line** (GPU contributors are 1st), with **300 chats in any 5 hours**. The model server runs at low priority on half your cores (2 to 8 threads), so your computer stays usable, and while you game the phone model drops to the lowest priority and runs lighter until you're done.

When you start, DLNE offers an amount to suit your machine, and never more than half your RAM:

| Your RAM | Offered by default | Also offered |
|---|---|---|
| 48 GB or more | 24 GB | 16 GB, 8 GB |
| 32 to 47 GB | 16 GB | 8 GB |
| 24 to 31 GB | 12 GB (runs the 8 GB model, with room to spare) | 8 GB |
| 16 to 23 GB | Half your RAM, 8 to 11 GB | 8 GB (when the default is higher) |
| Under 16 GB | Nothing: half your RAM is under the 8 GB minimum | |

To lend a different amount, set `dlne.worker.maxRamGb` yourself.

**An integrated GPU does not count as a GPU contributor**, except Intel Arc 140V / 140T (Lunar Lake, Arrow Lake) and AMD Strix Halo (Radeon 8060S / 8050S / 8040S, i.e. Ryzen AI Max). A machine whose only GPU is any other integrated GPU contributes its CPU instead, and DLNE tells you so.

A CPU-only machine needs, if it's fast enough not to slow everyone else down:

- at least **8 GB of RAM** lent to DLNE (`dlne.worker.maxRamGb`; you're asked how much when you start),
- **4 physical cores**,
- on Intel/AMD, **AVX2 and FMA** (Intel from 2013, any AMD Ryzen),
- **30 GB/s of memory bandwidth**, measured in a second or two when you join. Good dual-channel DDR4 just makes it; DDR5 and LPDDR5 pass easily.

A CPU's speed here is set by how fast it can read memory, and one slow machine would hold up every answer it takes part in, so a machine below any of these is told exactly what it's missing instead of joining. To lend only your CPU even though you have a GPU, set `dlne.worker.device` to `cpu`.

---

## Getting started

1. Install **DLNE Coding Assistant** from the Extensions view.
2. Open VS Code's Chat view and type `@dlne` followed by what you want, or run **DLNE: Open Chat** from the Command Palette, which opens the Chat view with `@dlne` typed in. If your editor has no Chat view (Cursor, VSCodium and the like), or you would rather use DLNE's own chat, open the **DLNE Agent** view in the Activity Bar instead. The status bar shows the swarm's state and your remaining free chats. [More on the ways to chat](#chatting-with-dlne).
3. When you want to contribute, click **Donate compute** in the DLNE sidebar. The first time, DLNE offers to install its compute engine:
   - It needs **Python 3.10 or newer**. The engine goes into `%LOCALAPPDATA%\DLNE\runtime` (Windows) or `~/.dlne/runtime`, with its own virtual environment.
   - **The engine runs on your machine, so it is only installed if it carries the maintainer's signature and matches it byte for byte.** Anything unsigned, altered, or older than what you have is refused, and your current engine is kept. The GPU engine (llama.cpp's `ggml-rpc-server`, an official upstream build pinned per release) is checked the same way, and updated with it.
   - Each step logs to the **DLNE Compute Worker** output channel. If one fails (no Python, `pip` offline), you are told which, with a **Show Log** button.

---

## Chatting with DLNE

DLNE has three ways to chat. All three talk to the same swarm, with the same API key, worker token or free trial.

| | Where | Who runs the agent |
|---|---|---|
| **`@dlne`** | VS Code's Chat view | DLNE's own agent: DLNE's memory, its rules about private files, its tools |
| **DLNE Swarm** | The Chat view's model picker | VS Code's own agent (Ask, Edit and Agent modes), with the swarm as its model |
| **The sidebar chat** | The **DLNE Agent** view in the Activity Bar | DLNE's own agent, the same one `@dlne` runs |

**DLNE: Open Chat** opens VS Code's Chat view with `@dlne ` typed in, and the sidebar chat in an editor that has no Chat view of its own (Cursor, VSCodium and the like). **DLNE: Open Chat Panel** always opens the sidebar chat.

### `@dlne` in the Chat view

This is DLNE's own agent inside VS Code's Chat view, with everything described in [The agent's memory](#the-agents-memory) below.

- **One conversation per Chat thread.** Every thread keeps its own conversation and its own saved task, so a second thread no longer clears the first one's memory or stops its run. After a window reload a thread carries on where it was. A **new chat** is new memory.
- **Enter during a run steers.** Send a message while `@dlne` is working: it finishes its current step, says it stopped to take your newer message, and carries on from your message in the same conversation.
- **Stop stops.** It ends a shell command the agent is running, together with everything that command started, and a run that is waiting for your approval.
- **Attach files.** Attach a file, use `#file`, or select text in the editor, and `@dlne` reads it by the same rules as its own file reading: a private file is not read and you are told, secrets are hidden, and a large file is attached in part, or not at all when it is very large (the agent reads the rest itself). A long paste (a log, say) is sent in part too: DLNE keeps the whole of it, secrets hidden, in its own private storage, and the agent reads the rest from there without asking you. Pictures and other kinds of attachment are not read. (This is `@dlne`'s. With **DLNE Swarm** picked in the model picker, VS Code's own agent reads attachments and pastes by its own rules.)
- **Auto.** The first time the agent has to ask before it edits a file or runs a command, the response offers a **Turn on Auto** button. The status bar's **DLNE: Ask** / **DLNE: Auto** item flips the same switch (`dlne.autoApproveAll`), and VS Code's own permission picker works for the message you send it with.
- **Continue.** After the step limit, an error, a steer, or a stop because the agent kept repeating itself (see [When the agent repeats itself](#when-the-agent-repeats-itself)), a **Continue** follow-up carries on from where the task stopped.
- **A run log.** **View > Output > DLNE** shows each run's start and end, each step's tool names, retries and errors. It never holds your words, file contents or secrets.
- **Slash commands:** `/code`, `/fix`, `/status`, `/model`, `/contribute`, `/clear` (which forgets this thread's conversation).

### DLNE Swarm in the model picker

With VS Code 1.104 or later, the Chat view's model picker lists **DLNE Swarm**. Pick it and VS Code's own Ask, Edit and Agent modes run on the swarm, tool calls included. It is one entry: the swarm chooses its model itself, so there is nothing else to pick. It cannot read images (an image in a request is refused with a message). An older VS Code does not show it, and `@dlne` and the sidebar chat work as before.

- **Without a GitHub sign-in.** VS Code does not call a chat participant until some language model exists, and says "Language model unavailable" without one. DLNE Swarm is such a model, so `@dlne` can run for someone who is not signed in to GitHub. (DLNE itself never asks for an account, and the sidebar chat does not depend on VS Code's models at all.)
- **Requests go the way the agent's do.** API key, worker token or the free trial; the wait for a free coding slot shows in the status bar and never in the answer; secrets are replaced with `[REDACTED]`; a restart of the swarm's engine is waited out.
- **Mind the free trial.** Every step of VS Code's own agent is one request, and each request spends one of the free chats. VS Code also sends its whole list of tools with every step, so a long task there uses far more of the swarm than `@dlne` does.
- **VS Code's tools read your files here, so the never-read rule is only partly kept.** When VS Code's own agent drives DLNE Swarm, VS Code's tools, not DLNE's, read your files and hand DLNE the text. `@dlne` and the sidebar chat keep the full never-read rule (`.env`, keys, files your `.gitignore` or `.dlneignore` lists). On this path DLNE refuses what it can see: where it can see the path of the file a tool result came from and its rule excludes that file, the result is replaced with the same "excluded for privacy" notice DLNE's own agent gives, and secrets in a known shape are replaced with `[REDACTED]`. But VS Code's own tools may still put file text into a request that DLNE cannot tie to a file, so DLNE does not promise the rule here. These requests are also capped: they are answered only by the coordinator's own engine or by a machine the operator trusts, even for a public repository, unless you set `dlne.privacy` to `anyMember`. DLNE shows a one-time notice about this. [Details](https://github.com/RGlabs84/DistributedLLMEngine/blob/main/docs/privacy.md#dlne-swarm-in-vs-codes-model-picker).
- **DLNE's own extras stay with `@dlne` and the sidebar chat:** the agent's memory (`remember` and the memory files) and `generate_image`. VS Code's agent has VS Code's tools.

### The sidebar chat

The **DLNE Agent** view in the Activity Bar is DLNE's own chat: the same agent as `@dlne` (its memory, its rules about private files, `generate_image`, **Continue** and **Auto**), for any editor that runs the extension, with or without a Chat view. Like VS Code's Chat view it keeps separate chats: **New chat** (or **+** at the top) starts another and keeps the one before, and the **←** arrow lists them, newest first, to go back to one or delete it (🗑). A task keeps running when you switch to another chat, and the list marks it as working. Each chat is saved so that a restart does not end its task, and the newest 20 are kept. The chat follows the answer only while you are at the bottom: scroll up to read and it stays put, with **↓ Latest** to jump back. It has no attachments and no slash commands; those are `@dlne`'s. It also holds the **Donate compute** panel, the swarm dashboard button and the phone link.

- Hiding the view loses nothing: the task goes on, and the chat is shown again as it was when you come back.
- While a task runs, a message is not sent: Enter and Continue say it is still working (Send is out of reach) and your words stay in the box. Press **Stop**, which reads "Stopping..." until the task has ended, or wait.
- A task that fails always ends and says why.

### Pictures

When the swarm's coordinator has image generation switched on, the agent (`@dlne` or the sidebar chat) gains a `generate_image(prompt, path, size)` tool: it creates a picture from a description and saves it as a PNG in your workspace (`images/<words of the prompt>.png` unless it is given a path). A picture takes a while, and the chat shows the time against the estimate. You approve the save exactly as you approve `write_file` (and **Always allow edits** covers it too); it never writes outside the workspace, and it asks before replacing a file. The prompt goes to the coordinator's own image engine only, never to a volunteer's machine. The coordinator refuses people, sexual content, violence and other unsafe requests, and the agent shows its reason.

---

## The agent's memory

The agent has two kinds of memory: one that keeps a single long task coherent, and one that carries over between tasks.

### Long tasks

When a task gets long, the agent does not just forget the early steps. Older steps are replaced by a summary that holds three things:

- **Your words**, kept as you typed them (very long ones are shortened, and the summary says so).
- **The agent's own notes**, which it writes for itself: the goal, what it found, what it decided, what is done and what is next.
- **A list of what it read, changed and ran**, kept exactly: which files and lines, which edits, which commands and how they ended.

The most recent steps stay word for word. How much fits in a request follows the per-request context the swarm reports, not a fixed size: that of its largest machine, because each request says how big it is and the swarm sends a big one to a machine that can hold it. If the agent's reply is cut off in the middle of a tool call, nothing is run: it is told, and it tries again in smaller pieces. **Stop** ends the run at once and runs nothing from a half-received reply.

### The step limit

One request may take up to `dlne.agent.maxTurns` steps (100 by default, from 10 to 500). A long run holds one of the swarm's shared coding slots the whole time, so it is capped. At the limit the agent does not just stop. It pauses, reports what it has done and what is left, and shows a **Continue** button (**Continue** is also offered after an error). Continue picks up from that report, with everything it has read.

### When the agent repeats itself

A model sometimes loops: it reads the same file again and again, runs the same failing command, or makes the same call and gets the same answer. DLNE looks at the last 12 steps. The first time it sees a loop it tells the model, in plain words, what it keeps doing and to change approach, and a line in the chat says so. If the agent carries on, the task stops with a notice and a **Continue** button, and everything so far is kept: say **continue** with a hint about what to try, or ask for something else. The model is told why it was stopped, so even a bare **Continue** starts from that. A task that loops a second time, much later, is told first again. Editing a file between two runs of a command is work, not a loop, and so is reading different parts of a long file. DLNE Swarm in VS Code's model picker has the same check: VS Code's own agent drives it, so the model is told in its system message, and if it goes on, the request is refused with a message that says what it kept doing. Your next message then goes through, and the model is told why the last task was stopped.

### Memory that lasts

Three plain Markdown files are read at the start of every agent request:

| File | What it is for | Who writes it |
|---|---|---|
| `AGENTS.md` in your workspace's root folder | Instructions for the agent: how to build and test, conventions, what to avoid | You. The agent never writes to it. |
| `.dlne/memory.md` in the workspace | Facts about this project: how to build and test it, where things live, its conventions, corrections you gave | The agent, by itself, and you |
| `~/.dlne/memory.md` | Your own preferences, for every project | The agent, with your approval, and you |

With no folder open, only your own file applies.

**The agent keeps its memory up to date as it works, the way a colleague takes notes.** It saves lasting, non-obvious facts with its `remember` tool without being asked. That means things like how to build and test the project, where things live, its conventions and corrections you gave it, but not details of one task or what the code already shows. After a task of 5 or more steps it also looks back once for anything worth keeping. It can update or remove facts it saved earlier, so the file stays current instead of piling up. Lines you wrote yourself are never touched: the agent's own lines carry a small `<!-- dlne -->` marker that the model never sees.

- **Project facts are saved at once** and shown in the chat with an **↩ Undo** button.
- **Facts about you ask first**, because they go with every project.
- **A fact holding a link or a command that fetches, runs or escalates** (`curl`, `wget`, `sudo`, `| sh`, and the like) asks first too, so a file the agent read cannot plant one unseen. Plain build and test commands save by themselves.
- **A fact that holds a secret is refused**, and one already saved is not added twice.

Set `dlne.agent.memory` to `ask` to approve every fact (project facts can then be set to **Always allow remembering in this project**, which **DLNE: Forget Agent Approvals for This Project** takes back), or to `off` to turn memory off. `dlne.autoApproveAll` skips every question, as it does for every action.

The files are yours. Open one and edit or delete any line, or the whole file; DLNE reads it again next time. Each file is loaded up to a size limit (8,000 characters for `AGENTS.md` and the project file, 4,000 for your own) and a longer one is cut, with a note. To keep a memory file out entirely, list it in `.dlneignore`: it is then neither loaded nor written. Git-ignoring the memory files does not switch them off (tool folders like `.dlne/` are often git-ignored); an `AGENTS.md` that your `.gitignore` excludes is not loaded.

**Should you commit `.dlne/memory.md`?** That is up to you. Committed, your team shares what the agent learned about the project, and anyone who runs DLNE on the repository sends those lines with their requests. If you would rather keep it to yourself, leave it uncommitted, or add `.dlne/` to your `.gitignore`.

To turn all of this off, set `dlne.agent.memory` to `off`. Nothing is then loaded or saved.

### Tasks and chats

A task in progress is saved in VS Code's private storage for the extension (not in your project) after each run, so a restart does not end it and **Continue** still works. `@dlne` saves one task for each Chat thread, and the sidebar one for each of its chats; only the newest are kept. A saved task is dropped after 7 days, and `/clear` (for that thread) or deleting a sidebar chat from its list deletes it. A **new chat** in VS Code's Chat view starts fresh: another thread's notes and lists are not carried over, and a second thread never touches the first. `AGENTS.md` and the memory files still load.

### What is sent where

The memory files are part of the request. They travel with every agent request to whoever serves it: the coordinator, or a volunteer's coding member. They go through the same secret redaction as everything else the agent sends, and `.dlneignore` keeps them out (see [Privacy](#privacy-what-to-know)). A secret is also refused when the agent tries to save it, but redaction catches common formats only, so keep secrets out of these files.

---

## Privacy: what to know

This section is about the coding agent in VS Code. The full version, with what each party can and cannot see, is [docs/privacy.md](https://github.com/RGlabs84/DistributedLLMEngine/blob/main/docs/privacy.md).

- **Your prompts, the code the agent reads, and the agent's memory files (`AGENTS.md`, `.dlne/memory.md`, `~/.dlne/memory.md`; see [The agent's memory](#the-agents-memory)) go to the coordinator.** The public coordinator is run by the DLNE maintainer, so treat it like any hosted AI service. When coding members are on, a request may instead be answered by a volunteer's machine (below).
- **The connection to the public swarm is encrypted (HTTPS)**, so nobody between you and the coordinator can read it. For anything sensitive, run your own coordinator (below).
- When the model is split across several contributors' GPUs, those machines process the model's intermediate numbers for your request, not your text.

**Who can answer your requests: `dlne.privacy`**

**A coding member has to read your request to run the model, so it sees the code it is sent.** This setting decides which requests can go to which members.

DLNE looks at your workspace's git remote. If it is a **public repository** (GitHub, GitLab or Codeberg), any coding member can answer. **Anything else**, and anything DLNE cannot check, is answered only by the coordinator's own engine or by machines the coordinator's operator has listed as trusted. The operator lists a machine by its identity key, not by a name it claims, so a member cannot claim to be trusted.

**A public repository counts as public even for changes you have not pushed.** If you are working on something unpublished in a clone of a public repository, set `dlne.privacy` to `trustedOnly`. A repository's own settings can only narrow `dlne.privacy`, never widen it (only your own user settings can), and DLNE does not run in a workspace you have not trusted. While coding members are off (the default), the extension makes no repository lookup, and nothing about your repository leaves your machine beyond the request itself.

| `dlne.privacy` | Who may answer |
|---|---|
| `auto` (default) | Public repositories: any member. Everything else: trusted machines only. |
| `trustedOnly` | Trusted machines only, even for a public repository. |
| `anyMember` | Any member, for every workspace. Choose this only if you are happy for any volunteer's machine to see that work. |

**One exception: VS Code's own agent on [DLNE Swarm](#dlne-swarm-in-the-model-picker).** Its requests are never widened by the repository: they are answered only by the coordinator's own engine or by a trusted machine, even for a public repository under `auto`, unless you set `anyMember`.

**Secrets are removed in your editor.** Before a request leaves VS Code, API keys, tokens, private keys and passwords in common formats are replaced with `[REDACTED]`. `@dlne` and the sidebar chat never read these files, so they are never sent: files your `.gitignore` ignores, anything in a `.dlneignore` file in your workspace, `.env*`, `*.pem`, `*.key`, `id_rsa*`, `.npmrc`, `.pypirc` and cloud credential files. The redaction applies to every request, including those to the coordinator, and to the agent's memory files (`AGENTS.md`, `.dlne/memory.md` and `~/.dlne/memory.md`), which are sent with every agent request: `.dlneignore` keeps any of them out, and `.gitignore` keeps out `AGENTS.md` (but not the two memory files, since tool folders like `.dlne/` are often git-ignored). The agent refuses to save a fact that holds a secret. It catches common formats, not every secret, so keep real secrets out of prompts.

**The full never-read rule belongs to `@dlne` and the sidebar chat**, which read your files themselves. When VS Code's own agent runs on [DLNE Swarm](#dlne-swarm-in-the-model-picker), VS Code's tools read the files and DLNE only sees the text it is handed. There DLNE refuses the excluded files it can see by path (the result is replaced with the "excluded for privacy" notice) and replaces secrets in a known shape with `[REDACTED]`, but VS Code's own tools may still put file text into a request, and DLNE shows a one-time notice about this. Anything else VS Code puts into its prompts goes to the swarm too.

**Requests and answers are encrypted on the way to and from a coding member.** Your editor encrypts the request and decrypts the reply. The coordinator relays bytes it cannot open, and the tunnel and the internet between it and the member carry nothing readable. That protects the trip, not the machine that answers: the member itself has to read the request. The coordinator is the operator's own machine and is trusted: it hands out the member's public key. A request answered by the coordinator's own engine goes over HTTPS instead and is not sealed.

**Members are not told who you are.** The coordinator passes on only the request: no IP address, no API key, no account. What the request itself contains (file paths, names in code or comments) can still say who you are.

**No-trace members and canaries.** Members run their model server with settings that keep prompts out of logs and off disk. Now and then the coordinator sends a member a normal-looking request holding a made-up link, built to look like any real request (where a file would hold a key it shows `[REDACTED]`, as yours do). If the link is ever opened, that member is flagged and gets no more work.

> **The honest limit: a coding member sees the code it is sent.** What protects you from members is the privacy mode (private repositories go only to trusted machines unless you opt in), the secrets removed in your editor, the no-trace settings and anonymity, and the canaries that are there to catch a member that snoops. A volunteer who changes DLNE's software could still read or keep what it receives, and none of this can promise otherwise. For code you cannot afford to expose, keep `dlne.privacy` on `auto` or `trustedOnly` (`trustedOnly` for unpublished work in a clone of a public repository), or run your own coordinator.

**If you contribute:**

- As a GPU contributor, your machine runs DLNE's worker and `ggml-rpc-server`, and accepts connections from the coordinator on port 50052.
- As a coding member, your machine decrypts and answers coding requests, so it sees their text (after redaction), but not who sent them. It never logs request or reply text. Its model server listens on your machine only, and the coordinator reaches it through a tunnel your worker opens.
- As a CPU contributor, your machine can answer phone-app requests (a coordinator whose own engine answers phones uses CPU machines only as the backup), so it sees the text of those questions and answers (the phone app tells its users this) and any search results for them. Its model server listens on your machine only, and the coordinator reaches it through a tunnel your worker opens. The phone chat is a separate path: none of the above (redaction, encryption, trusted routing) is promised for it. A photo someone adds in the phone chat is never sent to a volunteer's computer (only the coordinator's own engine reads it) and is not stored. Like every message, it is processed by the shared model, whose layers can run on volunteers' graphics cards, and those cards see only intermediate numbers, never the picture.

---

## Which model?

The coding agent runs on GPU contributors' machines: one model at a time, chosen by the coordinator from the GPU memory available. Bigger models unlock automatically as more GPUs join. `/status` and `/model` show what is running now, the sidebar shows it under the chat box, and so does the swarm dashboard; its context size and speed are live figures too, on the dashboard and the [DLNE website](https://dlne.rossgilsonlabs.dev), so they are not written here. There is nothing to pick: VS Code's Chat model picker lists a single entry, **DLNE Swarm**, and the model behind it is whatever the swarm is running now ([more](#dlne-swarm-in-the-model-picker)). When coding members are on, each member runs its own copy of the best version of the model its machine can hold whole (see [Be a coding member](#8-gb-of-gpu-memory-or-more-be-a-coding-member)). The phone app is answered by the same model, on lanes of its own; a smaller model on CPU contributors' machines answers it only while that engine is restarting or down.

---

## Your own private coordinator

Everything is open source, so you can run the whole thing yourself. The [repository README](https://github.com/RGlabs84/DistributedLLMEngine#run-your-own-coordinator) walks through it step by step (model, key, HTTPS, systemd, ports, workers) and says how to check that nothing goes to the public swarm. The short version:

```bash
git clone https://github.com/RGlabs84/DistributedLLMEngine.git
cd DistributedLLMEngine
pip install -r requirements.txt
python dlne_server.py start --host 0.0.0.0 --port 11435 --hybrid   # coordinator (+ a local worker)
python dlne_server.py apikey create --name "my-laptop"              # optional: prints a dlne_sk_... key
python dlne_server.py worker --coordinator 192.168.1.50:11435 --network-key "your_cluster_secret"
```

Then in VS Code settings:

```json
{
  "dlne.serverUrl": "http://192.168.1.50:11435",
  "dlne.apiKey": "dlne_sk_your_key_here",
  "dlne.worker.networkKey": "your_cluster_secret",
  "dlne.worker.allowUnsignedEngine": true
}
```

`allowUnsignedEngine` is needed only because your coordinator does not publish releases signed with the maintainer's key. Never turn it on for the public swarm.

---

## Settings

The settings that decide where your code or keys go (`dlne.serverUrl`, `dlne.apiKey`, `dlne.autoApproveReads`, `dlne.autoApproveAll`, `dlne.autoUpdate`, and every `dlne.worker.*` setting) are read only from your own user settings: a workspace's or folder's `.vscode/settings.json` cannot change them, so a cloned repository cannot point DLNE at another server or turn on auto-approval. A folder can only narrow `dlne.privacy`, never widen it.

| Setting | Default | What it does |
|---|---|---|
| `dlne.serverUrl` | `https://dlne.rossgilsonlabs.dev` | The coordinator: the public swarm (over HTTPS), or your own. The older public addresses (the DuckDNS name and before) are read as this one. |
| `dlne.apiKey` | `""` | Optional API key. Leave blank to use the free trial or your contribution. |
| `dlne.privacy` | `auto` | Who may answer your coding requests: `auto` (any coding member for a public repository, trusted machines only for everything else), `trustedOnly` (use this for unpublished work in a clone of a public repository), or `anyMember` (any member, for every workspace). A repository's own settings can only narrow it, never widen it. See [Privacy](#privacy-what-to-know). |
| `dlne.worker.networkKey` | public swarm key | The swarm's shared worker key. Change it only for a private coordinator. |
| `dlne.worker.device` | `auto` | What to contribute: `auto` (a GPU if there is one that counts, else the CPU), `gpu`, or `cpu` (power the phone app and keep the GPU for yourself). |
| `dlne.worker.allowCodingMember` | `true` | Let the swarm give your GPU the coding-member job (answering other people's coding requests). Turn it off and your GPU only joins the shared pool. |
| `dlne.ownGpu` | `faster` | When your computer is a coding member, which answers your own requests. `faster`: whichever answers sooner, your GPU while it writes at least as fast as the swarm's own engine, otherwise usually the swarm's engine. `smarter`: your own GPU whenever it runs a smarter model than the swarm's engine (a better version on the ladder), even if it writes more slowly. `dlne.privacy` still decides first: a private repository goes to your GPU only if the operator trusts it. |
| `dlne.worker.allowedGpus` | `""` | GPU indices to lend (e.g. `0,1`). Blank lends all detected GPUs. |
| `dlne.worker.maxVramGb` | `-1` | Most VRAM to lend, in GB (`-1`: detect). |
| `dlne.worker.maxRamGb` | `4.0` | Most system RAM the worker may use, in GB. A CPU-only machine needs at least 8; the more it lends, the bigger the phone-app model it runs (16 GB: Qwen3.6). You're offered an amount when you start. |
| `dlne.worker.maxCpuThreads` | `-1` | Most CPU threads the worker may use, so the rest of your computer stays yours. `-1`: the engine decides (half your cores, 2 to 8). Applies to a running worker without a restart. Set by the sidebar's Donate compute panel. |
| `dlne.worker.autoStart` | `false` | Resume contributing when VS Code opens (set for you when you start contributing). |
| `dlne.worker.pythonPath` | `python3` | Python used for the engine. |
| `dlne.enableInlineCompletion` | `true` | Ghost-text autocomplete. |
| `dlne.inlineCompletionDelay` | `150` | Delay after typing before asking for a completion, in ms. |
| `dlne.chatModel` / `dlne.fimModel` | `""` | Leave blank: the swarm serves the model it has active. Name a model file only for a private coordinator you run. |
| `dlne.autoApproveReads` | `true` | Let the agent read, list and search inside your workspace without asking. Edits and shell commands ask, and the approval dialog can remember the answer for the project: **Always allow edits in this project**, or **Always allow "npm test"** for one command (with any arguments; never for a command with `;`, `&&`, `\|` or `$(...)` in it, and never for programs that delete, use `sudo`, fetch from the network or open a shell). Anything outside the workspace always asks. **DLNE: Forget Agent Approvals for This Project** lists what is remembered and clears it. |
| `dlne.autoApproveAll` | `false` | Run every agent action without asking: edits anywhere and every shell command. The agent's next step can come from a volunteer's machine (a coding member), so a command it sends would run on your computer unseen. Only your user settings can turn it on. Flip it with the status bar's **DLNE: Ask** / **DLNE: Auto** item, the Chat view's **Turn on Auto** button, the sidebar's **Auto** button or **DLNE: Toggle Auto-Approve**. |
| `dlne.agent.maxTurns` | `100` | How many steps (model turns, usually one tool call each) the agent may take for one request, from 10 to 500. At the limit it pauses, reports what it has done and what is left, and offers **Continue**. See [The step limit](#the-step-limit). |
| `dlne.agent.memory` | `auto` | `AGENTS.md`, the project's `.dlne/memory.md` and your `~/.dlne/memory.md` go with every agent request. `auto`: the agent saves project facts by itself, each shown with Undo (facts about you, and any with a link or a risky command, ask first). `ask`: every fact asks first. `off`: nothing is loaded or written. See [The agent's memory](#the-agents-memory). |
| `dlne.autoUpdate` | `true` | Keep this extension and the engine on the latest signed release, and restart the worker when the coordinator upgrades. |
| `dlne.worker.allowUnsignedEngine` | `false` | Install the engine from a coordinator that publishes no signed release. Only for a private coordinator you run. A signed release that fails verification is never installed, whatever this says. |

---

## Support DLNE

Two ways to keep it free:

- **Donate compute at home**: click **Donate compute** in the DLNE sidebar (see [How it stays free](#how-it-stays-free)).
- **Buy a coffee**: [buymeacoffee.com/wubarrk](https://buymeacoffee.com/wubarrk) helps pay for the coordinator's power and hardware.

---

## Links

- **Website**: [dlne.rossgilsonlabs.dev](https://dlne.rossgilsonlabs.dev)
- **Discord**: [discord.gg/RNxvxWn8cw](https://discord.gg/RNxvxWn8cw)
- **Source**: [RGlabs84/DistributedLLMEngine](https://github.com/RGlabs84/DistributedLLMEngine)
- **Issues**: [GitHub Issues](https://github.com/RGlabs84/DistributedLLMEngine/issues)
- **License**: MIT
