# Data safety form: answers for DLNE

Play Console > Policy and programs > App content > Data safety. These answers follow
Google's definitions: "collected" means sent off the device by the app (including to your
own server); "shared" means transferred to a third party. They describe app version 1.0.1
(`io.dlne.chat`) and the server's app endpoints (`/v1/app/chat/completions`,
`/v1/app/report`). Re-check them whenever the app or server changes.

Items marked **OWNER: CONFIRM** depend on how the server and volunteer machines are run;
they must match the privacy policy.

## What leaves the phone (the facts behind the answers)

| When | What | To |
|---|---|---|
| The person presses Send | The recent conversation text (up to ~12,000 characters) | The DLNE server over HTTPS, which passes it to one volunteer computer to write the answer |
| While the app is open | A status request with no personal data | The DLNE server |
| The person taps Report on an answer and sends it | That answer, the question it answered, the chosen reason and an optional note (≤ 500 characters) | The DLNE server, **kept there for review** |

Lookups (weather from Open-Meteo, Wikipedia, news from GDELT, web search through SearXNG)
are made **by the DLNE server, not the phone**. The phone never contacts any search or
lookup provider, and no third-party search provider receives anything from it: no IP
address, no identifiers, no conversation. The server sends those services only the short
lookup text the model writes (e.g. a city name or a topic).

No account, no device or advertising identifiers, no analytics, no crash reporting, no
location, no contacts or files.

## Section 1: Data collection and security

| Question | Answer | Why |
|---|---|---|
| Does your app collect or share any of the required user data types? | **Yes** | Message text is sent to the DLNE server to be answered, and reports are stored there. |
| Is all of the user data collected by your app encrypted in transit? | **Yes** | The app only connects over HTTPS (`usesCleartextTraffic=false`, https-only server setting). **OWNER: CONFIRM** the coordinator-to-volunteer hop is also encrypted (WireGuard / TLS); if it is not, answer **No**. |
| Do you provide a way for users to request that their data is deleted? | **Yes** | Chats live only on the phone and can be deleted in the app (per chat, or all from Settings). Reports have no account attached; a person can ask for one to be removed by contacting the developer and quoting it. **OWNER: CONFIRM** you will honour such requests and that no other message text is stored server-side. |

## Section 2: Data types

Declare these two data types and nothing else.

### Messages > Other in-app messages

The chat text sent for an answer, and the question/answer pair inside a report.

| Question | Answer |
|---|---|
| Collected | **Yes** |
| Shared | **Yes**: the text is processed on computers run by volunteers (third parties, not contracted service providers), and short lookup texts the model derives from it go from the DLNE server to Open-Meteo, Wikipedia, GDELT and the SearXNG search service. |
| Is this data processed ephemerally? | **No**: chat text is handled only in memory while answering (**OWNER: CONFIRM**), but reported answers and questions are **stored** for review, so the honest answer for this data type is No. |
| Is this data required, or can users choose? | **Required** (the app's only function is answering messages the user chooses to send). |
| Why is this user data collected? | **App functionality**; **Fraud prevention, security, and compliance** (reviewing reported answers) |
| Why is this user data shared? | **App functionality** |

### App activity > Other user-generated content

The optional note a person types when reporting an answer.

| Question | Answer |
|---|---|
| Collected | **Yes** |
| Shared | **No** (kept on the DLNE server) |
| Is this data processed ephemerally? | **No** (kept for review) |
| Is this data required, or can users choose? | **Optional** (sent only if the person reports an answer and writes a note) |
| Why is this user data collected? | **Fraud prevention, security, and compliance** |

### Everything else: not collected

| Data type | Collected? | Notes |
|---|---|---|
| Location (approximate / precise) | No | Weather lookups use a place the person names in the chat, resolved on the server; the app never reads the phone's location. The server sees the IP address but uses it only for rate limiting. |
| Personal info (name, email, user IDs, address, phone, ...) | No | No accounts. |
| Financial info | No | Donation links open in the browser; nothing is handled in the app. |
| Health and fitness | No | |
| Photos and videos, Audio, Files and docs | No | The share target accepts text only. |
| Calendar, Contacts | No | |
| App activity: interactions, search history, installed apps, other actions | No | No analytics. (Lookups are made by the server and are not linked to a person.) |
| Web browsing | No | Sources open in the person's own browser. |
| App info and performance (crash logs, diagnostics) | No | No crash reporting SDK. |
| Device or other IDs | No | No advertising ID, no Android ID, no install ID. |

## Notes for the reviewer field (if asked)

> DLNE has no accounts, ads, analytics or SDKs that collect data. Chat text the user
> chooses to send goes over HTTPS to the DLNE server and is answered by an open-source
> model on a volunteer's computer; when needed, the server (not the phone) looks things up
> on Open-Meteo, Wikipedia, GDELT or a SearXNG search service. Users can report any answer
> from inside the app; a report (the answer, its question, a reason and an optional note)
> is stored on the DLNE server for review. Chats are stored only on the device and can be
> deleted in the app.
