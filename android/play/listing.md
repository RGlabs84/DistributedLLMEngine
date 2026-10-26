# Google Play store listing: DLNE

Paste each block into Play Console > Grow users > Store presence > Main store listing.

## App name (max 30 characters)

```
DLNE: Community AI Chat
```
(23 characters)

## Short description (max 80 characters)

```
Free AI chat answered by computers volunteers lend. No account, no tracking.
```
(76 characters)

## Full description (max 4,000 characters)

```
DLNE is a free AI chat assistant run by a community, not by a data company.

Every answer is written by an open-source language model running on computers that volunteers lend to the DLNE network. Your phone only asks the question: it never does any of the work and never runs anything in the background.

WHAT YOU GET
• Answers that appear word by word as they are written, with a Stop button
• Up-to-date answers: DLNE can check the weather, Wikipedia, the news and the web, and lists its sources so you can read them yourself
• Clean formatting: lists, bold text, links, and code blocks with a copy button
• Report any answer that is harmful, wrong or offensive, right from the app
• Long-press any message to copy it
• Your chat history, kept only on your phone. Delete one chat or all of them at any time
• Share text from any app into DLNE to ask about it
• Light and dark themes that follow your phone
• Comfortable on small phones and large ones

PRIVATE BY DESIGN
• No account, no sign-in, no email address
• No ads, no analytics, no tracking, no Google Play Services
• One permission only: internet access
• Chats are left out of cloud backups
• Lookups (weather, Wikipedia, news, web search) are made by the DLNE server, not your phone; reported answers are kept on the server for review

Because answers are written on computers that belong to other people, please don't send passwords, private details or anything secret. The app reminds you before your first message.

Answers come from AI and can be wrong. Check anything important.

FAIR USE
DLNE is shared by everyone, so each network can have one answer in progress at a time and a few questions per minute. If you ask too quickly, the app tells you how long to wait.

RUN YOUR OWN
DLNE uses the public DLNE server at https://dlne.duckdns.org unless you say otherwise. DLNE is open source under the MIT license. If you run your own DLNE server, enter its address in Settings.

HELP DLNE GROW
The more computers people lend, the bigger and faster the models DLNE can offer. Anyone with a PC can donate spare computing power with the free DLNE extension for VS Code and VSCodium. You can also support the project with a coffee from the app's Settings, which also links to the DLNE website and the DLNE Discord for news and help.

Source code: https://github.com/RGlabs84/DistributedLLMEngine
```

## Other listing fields

| Field | Value |
|---|---|
| App or game | App |
| Free or paid | Free |
| Category | Productivity |
| Tags (pick up to 5 offered by Console) | AI assistant, Chat, Productivity |
| Contact email | *(required, shown publicly: the owner's support address)* |
| Website | https://github.com/RGlabs84/DistributedLLMEngine |
| Privacy policy URL | where `privacy-policy.html` is published (see README.md) |

## Graphics in this folder

| File | Play requirement | What it is |
|---|---|---|
| `icon-512.png` | App icon, 512 x 512 PNG, 32-bit | The one official DLNE logo (`vscode-extension/media/dlne_logo.png`): the cyan-and-pink glowing brain on a dark rounded square with "DLNE" under it. Pixel-identical to the official file, saved with an alpha channel because Play asks for 32-bit. |
| `feature-graphic.png` | Feature graphic, 1024 x 500 PNG (no alpha) | The official logo on the left; "DLNE Chat" and the line "Free AI chat. No account." on a dark navy background in the logo's palette (cyan to pink). |

Both are generated from the official logo by `android/tools/generate_dlne_icons.py`; do not
edit or replace them by hand (the unit tests check them). The launcher icon inside the app
is the same logo.

Phone screenshots: see README.md, step 6.
