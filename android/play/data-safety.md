# Data safety form: answers for DLNE

Play Console > Policy and programs > App content > Data safety. These answers follow
Google's definitions: "collected" means sent off the device by the app (including to your
own server); "shared" means transferred to a third party. They describe app version 1.0.6
(`io.dlne.chat`) and the server's app endpoints (`/v1/app/chat/completions`, which also takes the
photos the person adds and, when the server has image generation on, starts the pictures the
chat model creates; `/v1/app/report`; and, when image generation is on, `/v1/app/images`). Re-check them whenever the app or server changes.

Items marked **OWNER: CONFIRM** depend on how the server and the lent (backup) machines are run;
they must match the privacy policy.

## What leaves the phone (the facts behind the answers)

| When | What | To |
|---|---|---|
| The person presses Send | The recent conversation text (up to ~12,000 characters) and a random conversation id (`chat_id`, a new UUID made when the chat starts; not tied to a person, account or device, kept only so the server can keep a private speed-up cache per chat) | The DLNE server over HTTPS, which passes the text to the computer that writes the answer: DLNE's own server, with a computer that someone lends to DLNE as the backup, only while the server is restarting or down |
| The person presses Send, **only if location sharing is on** (Settings > Share location, or "Use my location" on the card the chat shows above the message box when a question looks local) and the question looks local ("near me", "nearby", weather and the like) | The phone's latitude and longitude, up to 5 decimals (`location`) | The DLNE server over HTTPS. Not sent for other questions; not sent if no position under 2 minutes old could be found (the app says so under the answer) |
| The person adds photos (+ > Photos or Camera, only shown when the server can read them) and presses Send | Up to 4 photos with that message, each re-encoded on the phone as a JPEG of at most about 1.2 megapixels, which removes the camera details and GPS position. While it is the newest message with photos, they go again with the messages sent after it, so the AI can still see them (the server keeps no photo between requests) | The DLNE server over HTTPS, which passes them only to DLNE's own computer to write the answer: **they are never sent to a lent computer, and DLNE does not keep them** (like every message, a photo is processed by the shared model, whose layers can run on lent graphics cards, which see only intermediate numbers, never the picture). The photos stay on the phone (private storage) with the chat and are removed with it |
| The person asks the chat for a picture and the model creates it (only when the server has image generation on) | The picture description the model wrote (up to 1,000 characters) and the conversation id, then the app polls for the result | The DLNE server over HTTPS, which checks it and draws the picture on its own computer, holding it in memory for up to 30 minutes after it is made. The finished picture is kept on the phone (private storage) with the chat and removed with it |
| The person taps Create image (only shown when the server has image generation on) and sends | The image description (up to 1,000 characters), the chosen size, the conversation id, then polls for the result | The DLNE server over HTTPS. The finished image is kept on the phone (private storage) and removed with its chat |
| While the app is open | A status request with no personal data | The DLNE server |
| The person taps Report on an answer and sends it | That answer, the question it answered, the chosen reason and an optional note (≤ 500 characters) | The DLNE server, **kept there for review** |

Lookups (weather from Open-Meteo, Wikipedia, news from GDELT, web search through SearXNG)
are made **by the DLNE server, not the phone**. The phone never contacts any search or
lookup provider, and no third-party search provider receives anything from it: no phone IP
address, no identifier, no conversation. The server sends those services only the short
lookup text the model writes (e.g. a city name or a topic), plus, only when the person turned location on,
rounded coordinates (weather) or a nearby place name (searches). Open-Meteo receives the
server's network address and coordinates rounded to about 100 m, never the phone's address.

No account, no device or advertising identifiers (the conversation id above is a random per-chat value), no analytics, no crash reporting, no
contacts, and no files except the photos the person chooses to add (the system photo picker and the system camera app hand the app only
those photos; the app asks for no storage or camera permission). Location is sent only if the person turns it on (see the Location data type).

**OWNER: CONFIRM** before answering for photos: the server never stores or logs a photo (its log line holds only the size and the first
characters of a hash), and the only trace is an in-memory count per network that uses a hash of each distinct photo for one hour to keep
the hourly photo limit; that hash cannot be turned back into the photo.

## Section 1: Data collection and security

| Question | Answer | Why |
|---|---|---|
| Does your app collect or share any of the required user data types? | **Yes** | Message text is sent to the DLNE server to be answered, and reports are stored there. |
| Is all of the user data collected by your app encrypted in transit? | **Yes** | The app only connects over HTTPS (`usesCleartextTraffic=false`, https-only server setting). **OWNER: CONFIRM** the hop from the DLNE server to a lent computer (used only as the backup) is also encrypted (WireGuard / TLS); if it is not, answer **No**. |
| Do you provide a way for users to request that their data is deleted? | **Yes** | Chats live only on the phone and can be deleted in the app (per chat, or all from Settings). Reports have no account attached; a person can ask for one to be removed by contacting the developer and quoting it. **OWNER: CONFIRM** you will honour such requests and that no other message text is stored server-side. |

## Section 2: Data types

Declare these four data types and nothing else.

### Messages > Other in-app messages

The chat text sent for an answer, the picture descriptions the chat model writes when someone asks it for a picture, and the question/answer pair inside a report.

| Question | Answer |
|---|---|
| Collected | **Yes** |
| Shared | **Yes**: the text is processed on DLNE's own server and, only as the backup while that is restarting or down, on a computer run by a volunteer (a third party, not a contracted service provider), and short lookup texts the model derives from it go from the DLNE server to Open-Meteo, Wikipedia, GDELT and the SearXNG search service. |
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

### Photos and videos > Photos

The photos the person adds to a message (+ > Photos, or Camera, which opens the system
camera app). Each is re-encoded on the phone as a JPEG, which removes the camera details and
the GPS position. They go with that message and, while it is the newest message with photos, with
the messages sent after it, so the AI can still see them (the server keeps no photo between requests;
a newer message with photos replaces them, and photos the server refused are not sent again). The
app says it where the person adds them (the sheet behind the + button) and in Settings > Privacy:
"Photos you add go to DLNE's own server to write the answer and are never sent to a lent computer.
Like every message, they are processed by the shared model, whose layers can run on lent graphics
cards; those cards see only intermediate numbers, never the picture. DLNE doesn't keep them. They
stay on this phone with the chat."

| Question | Answer |
|---|---|
| Collected | **Yes** (up to 4 photos with a message the person sends, and again with the messages after it while it is the newest with photos) |
| Shared | **No**: the photos go only to DLNE's own server and its own computer that reads them, never to a lent (volunteer) computer and never to a third party. **OWNER: CONFIRM** this answer. DLNE's shared model has layers that can run on lent graphics cards, which see only intermediate numbers (never the picture), as they do for every message; decide whether Play's meaning of "shared" covers that, and if it does, answer **Yes** for photos. |
| Is this data processed ephemerally? | **Yes**: read in memory to write the answer and not stored or logged on the server (**OWNER: CONFIRM**, see above). They stay on the phone, in the app's private storage with the chat. |
| Is this data required, or can users choose? | **Optional** (nothing is sent unless the person adds a photo) |
| Why is this user data collected? | **App functionality** |
| Encrypted in transit | **Yes** (HTTPS) |

### Location > Precise location

Off by default. Sent only while sharing is on: the person turned on Settings > Share location,
or tapped "Use my location" on the card the chat shows above the message box when a question
looks local ("Use your location for this?"), and in both cases allowed Android's location
permission (they may allow only approximate; that still works, less exactly). Never read in the
background (no `ACCESS_BACKGROUND_LOCATION`).

| Question | Answer |
|---|---|
| Collected | **Yes** (the phone's latitude and longitude, up to 5 decimals, sent with the question) |
| Shared | **Yes**, with service providers only as described: weather coordinates, rounded to about 100 m, go from the DLNE server to Open-Meteo; for local searches only a nearby place name goes to the search engines behind DLNE's search. The AI model and the computer that writes the answer (DLNE's own server; a lent computer only as the backup while it is down) never receive the coordinates, but when a local lookup runs, the results they read name the nearby town, so that machine does see a town name. |
| Is this data processed ephemerally? | **Yes**: used in memory for the lookup and then dropped; not logged or stored. |
| Is this data required, or can users choose? | **Optional** (off until the person turns it on) |
| Why is this user data collected? | **App functionality** |
| Why is this user data shared? | **App functionality** |
| Encrypted in transit | **Yes** (HTTPS) |

If Play asks for Approximate location as well, answer it identically: it is the same
coordinates when the person allows only approximate location.

### Everything else: not collected

| Data type | Collected? | Notes |
|---|---|---|
| Location (coarse beyond the above) | No | Nothing further is derived from the IP address, which the server uses only for rate limiting. |
| Personal info (name, email, user IDs, address, phone, ...) | No | No accounts. |
| Financial info | No | Donation links open in the browser; nothing is handled in the app. |
| Health and fitness | No | |
| Videos, Audio, Files and docs | No | The share target accepts text only. Photos are declared above: the system photo picker and camera app hand the app only the photos the person chooses (no storage or camera permission). |
| Calendar, Contacts | No | |
| App activity: interactions, search history, installed apps, other actions | No | No analytics. (Lookups are made by the server and are not linked to a person.) |
| Web browsing | No | Sources open in the person's own browser. |
| App info and performance (crash logs, diagnostics) | No | No crash reporting SDK. |
| Device or other IDs | No | No advertising ID, no Android ID, no install ID. The random per-conversation `chat_id` is generated by the app for each chat; it identifies no person or device and is not shared with reports. **OWNER: CONFIRM** you are comfortable answering No here; if Play's reviewer reads it as an identifier, say it is a random, non-persistent, per-conversation value. |

## Notes for the reviewer field (if asked)

> DLNE has no accounts, ads, analytics or SDKs that collect data. Chat text the user
> chooses to send goes over HTTPS to the DLNE server and is answered by an open-source
> model on DLNE's own server (a lent computer answers only as the backup while that is restarting or down); when needed, the server (not the phone) looks things up
> on Open-Meteo, Wikipedia, GDELT or a SearXNG search service. Photos the user chooses to
> add (through the system photo picker or camera app, so no storage or camera permission)
> go over HTTPS to DLNE's own server to write the answer, with that message and, while it is
> the newest with photos, with the messages after it so the AI can still see them. They are never
> sent to a lent computer (like every message, they are processed by the shared model, whose layers
> can run on lent graphics cards, which see only intermediate numbers, never the picture); DLNE does
> not keep them. They stay on the device with the chat. When the user asks the chat for a picture, the
> description the model writes goes to DLNE's own server, which draws the picture and holds it
> in memory for up to 30 minutes after it is made, so the app can collect it. Location is optional and off
> by default: if the user turns it on (in Settings, or by tapping "Use my location" when the
> chat asks), the phone's location goes with the question to the
> DLNE server and is used only for lookups (weather coordinates rounded to about 100 m to
> Open-Meteo; a nearby place name added to web searches). The coordinates are never given
> to the model or to the computer that writes the answer (which sees only the nearby town's
> name in the lookup results), and are never logged or stored. Users can report any answer
> from inside the app; a report (the answer, its question, a reason and an optional note)
> is stored on the DLNE server for review. Chats are stored only on the device and can be
> deleted in the app.
