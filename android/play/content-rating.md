# Content rating (IARC questionnaire) and related App content answers

Play Console > Policy and programs > App content. Answer truthfully for what the app can
show. The questionnaire wording changes from time to time; the intent of each answer is
given so it can be mapped onto the current wording.

## Content rating questionnaire

- **Email for rating notices:** the owner's contact address.
- **Category:** *All Other App Types* (DLNE is not a game, not a social/communication app
  between users, and not a news app; it is a productivity tool / AI assistant).

| Topic | Answer | Reasoning |
|---|---|---|
| Violence, blood, gore | No | The app ships no such content. |
| Sexual content or nudity | No | No images at all; the app ships no sexual content. |
| Profanity / crude humour | No | Not part of the app's own content. |
| Controlled substances (drugs, alcohol, tobacco) | No | |
| Gambling, simulated gambling | No | |
| Fear / horror | No | |
| Does the app let users interact or exchange content with **other users**? | **No** | People never see or message each other; each person chats only with the model. |
| Does the app share the user's location with other users? | No | |
| Does the app allow purchases of digital goods? | No | Donation links open external websites in the browser. |
| Does the app provide unrestricted access to the internet (browser / search engine)? | No | Links open the phone's own browser. |
| Does the app contain or generate AI-generated content? (if asked) | **Yes** | Answers are written by an open-source language model. |
| Is the app a web browser or search engine? | No | |

Expected outcome: a general-audience rating (e.g. ESRB Everyone, PEGI 3). Because the model
can write anything a user asks for, keep the target audience at 18+ (below) rather than
relying on the rating alone.

## Target audience and content

- **Target age groups:** **18 and over** only. This keeps the app out of Google's Families
  policy, which a general-purpose AI chat cannot meet (no content filtering for children,
  conversations processed on third-party computers).
- **Could the app unintentionally appeal to children?** No (plain text assistant, no
  characters, games or child-oriented imagery).
- **Store listing ads to children:** none.

## Other App content declarations

| Declaration | Answer |
|---|---|
| Ads | **No**, the app contains no ads. |
| App access | **All functionality is available without special access** (no login). |
| Government app | No |
| Financial features | None |
| Health app | No |
| News app | No |
| COVID-19 contact tracing / status | No |
| Data safety | see `data-safety.md` |
| Privacy policy | URL of the published `privacy-policy.html` |
| Advertising ID (Android 13+) | **No**, the app does not use the advertising ID. |

## AI-generated content policy

Google Play's *AI-Generated Content* policy requires apps that generate content with AI to
(1) prevent prohibited content as far as possible and (2) **let users report or flag
offensive AI output from inside the app, without leaving it**, so the developer can act on
it.

**Done:** every answer has a "Report answer" button (flag icon) opening an in-app sheet
with a reason (Harmful or dangerous / Wrong / Offensive / Something else) and an optional
note; the report goes to `POST /v1/app/report` on the DLNE server and is kept for review.
The server endpoint must be live before review (see README.md, step 0), and someone must
actually review the reports.
