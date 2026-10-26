# Publishing DLNE on Google Play: the owner's checklist

Everything Play Console asks for is prepared in this folder:

| File | Used in |
|---|---|
| `listing.md` | Store listing: title, short and full description, category, contact |
| `icon-512.png`, `feature-graphic.png` | Store listing graphics: the official DLNE logo (blue-and-pink brain, "DLNE" under it) and a 1024 x 500 banner built from it; regenerate with `android/tools/generate_dlne_icons.py`, never edit by hand |
| `privacy-policy.html` | Draft privacy policy: fill in the brackets, then publish it |
| `data-safety.md` | Data safety form, answer by answer |
| `content-rating.md` | Content rating questionnaire, target audience, other declarations |

The upload itself is `android/app/build/outputs/bundle/release/app-release.aab`
(build it with `./gradlew bundleRelease`, see `android/README.md`).

## 0. Before you submit

1. **In-app reporting of AI answers (required by Play policy): DONE in the app.** Google
   Play's *AI-Generated Content* policy requires a way to report or flag offensive AI output
   **without leaving the app**. Every answer has a flag button ("Report answer") that opens
   a sheet: Harmful or dangerous / Wrong / Offensive / Something else, plus an optional note
   (≤ 500 characters). It sends `POST /v1/app/report` with
   `{"question", "answer", "reason": "harmful"|"wrong"|"offensive"|"other", "note"}` and
   shows "Thanks, we'll review it".
   **Deploy the server's `/v1/app/report` before submitting:** on a server without it the
   app says "Reporting isn't available on this server", and a reviewer who tries it would
   see that. Have a way to read and act on the stored reports.
2. **Fill in and confirm the privacy policy.** Every `[BRACKET]` in `privacy-policy.html`,
   and the statements marked *OWNER: CONFIRM* (whether the coordinator or volunteer machines
   log message text or IP addresses, how long logs and reports are kept, whether lookup
   texts are stored, which engines SearXNG forwards to, model training). The same answers
   decide `data-safety.md`.
3. **Publish the policy at a stable https URL**, for example GitHub Pages from this repo or a
   page under the DLNE site, and note the URL for step 5.

## 1. Create the Google Play developer account

1. Use a Google account you will keep for publishing (a dedicated one is best). Go to
   <https://play.google.com/console/signup>.
2. Choose the account type:
   - **Personal** (an individual): quickest. Needs a government ID for identity
     verification.
   - **Organization** (a company or registered project): needs a D-U-N-S number from Dun &
     Bradstreet (free, can take days to weeks) and an organization website.
3. Enter the **developer name** shown publicly on Play (e.g. "DLNE" or your name), a public
   **contact email**, and a contact phone (not shown publicly).
4. Pay the one-time **US$25** registration fee.
5. Complete **identity verification** (legal name and address must match your ID) and verify
   the contact email and phone number. Google may also ask you to confirm you have access to
   a real Android device through the Play Console mobile app.
6. **Personal accounts created after 13 November 2023 must run a closed test first:** at
   least **12 testers** opted in for **14 consecutive days** before Console lets you apply
   for production access (step 8). Line up 12+ people with Android phones and Google
   accounts now (friends, the DLNE Discord).

## 2. Create the app

Play Console > **Create app**:

- App name: `DLNE: Community AI Chat`
- Default language: English (United States)
- App or game: **App**; Free or paid: **Free** (cannot change to paid later)
- Accept the Developer Program Policies and US export laws declarations.

## 3. App signing and the first upload

1. Testing > **Closed testing** (or **Internal testing** for a quick first try) > Create
   track / Create new release.
2. **Play App Signing:** choose *Let Google manage and protect your app signing key*
   (recommended). The keystore in `~/WubarrkCODING/DLNE_RELEASE_KEYS/android/` then becomes
   your **upload key**: you sign uploads with it, Google signs what phones install.
   - Consequence: APKs you build and share yourself (GitHub releases, direct downloads) carry
     a different signature from Play installs, so a person cannot switch between the two
     without uninstalling. If you want both channels interchangeable, pick *Use a different
     app signing key* instead and upload `dlne-release.jks` with Google's PEPK tool
     (Console shows the exact command), then create a new, separate upload key.
3. Upload `app-release.aab`. The R8 mapping file is embedded in the bundle, so crash reports
   in Console are de-obfuscated automatically.
4. Release name: `1.0.2 (3)`. Release notes, e.g.: `First release of DLNE for Android.`
5. Add testers: create an email list (or a Google Group) with your testers, save, and share
   the opt-in link from the track page. Testers must open it and accept.
6. Review and roll out the release to the track.

## 4. App content forms (Policy and programs > App content)

Fill each card using `content-rating.md` and `data-safety.md`:

- Privacy policy (URL from step 0)
- Ads: **No**
- App access: **All functionality available without special access**
- Content rating: questionnaire answers in `content-rating.md`
- Target audience and content: **18 and over**
- Data safety: `data-safety.md`
- Government apps: No; Financial features: None; Health: No; News: No
- Advertising ID: **No**

## 5. Store listing (Grow users > Store presence > Main store listing)

Copy the texts from `listing.md`; upload `icon-512.png` (app icon) and
`feature-graphic.png` (feature graphic). Set the category to **Productivity** under Store
settings, add the contact email and website.

## 6. Screenshots to take

Play needs **2 to 8 phone screenshots**, PNG or JPEG, each side 320 to 3,840 px, and the
long side **no more than twice** the short side. A modern 1080 x 2400 phone is too tall
(2.2:1), so capture at **1080 x 1920**: on an emulator or phone with USB debugging run
`adb shell wm size 1080x1920` first (`adb shell wm size reset` afterwards), and capture with
`adb exec-out screencap -p > shot1.png`.

Take these, using harmless example questions (no personal data, no real names):

1. **A finished answer with formatting**, dark theme: e.g. "How do I reverse a list in
   Python?", showing bold text, a bullet list and a code block with its copy button.
2. **An answer with sources** in light theme (Settings > Display > Dark theme off): e.g.
   "What's the weather in Philadelphia this weekend?", showing the `[1]` markers and the
   source list under the answer.
3. **An answer streaming in**, with the Stop button visible and the model / machines line
   under the title (or the "Checking the weather for …" line while it looks something up).
4. **The chat list drawer** with three or four example conversations.
5. **The first-run privacy notice** ("Before you start").
6. **Settings**, showing the privacy section and the Website / Discord / Support / Donate compute rows.
7. Optional: **the Report answer sheet** (shows reviewers the AI-content reporting).
8. Optional: **sharing text into DLNE** from a browser (share sheet with DLNE selected, or the
   new chat with the shared text in the box).

Tablet screenshots are optional; the app is phone-first.

## 7. Production

1. After the closed test has had 12+ opted-in testers for 14 days (personal accounts),
   Console unlocks **Apply for production** on the Dashboard; answer its short questions
   about the test.
2. Once approved: Production > Create new release > add the same (or a newer) bundle >
   choose countries > roll out. The first review usually takes a few days.
3. Watch Console > **Pre-launch report** (Google's automatic test devices): the app needs no
   login, so the robot can explore it fully.

## 8. Every later update

1. Raise `versionCode` (and `versionName`) in `android/app/build.gradle.kts`.
2. `./gradlew :app:testDebugUnitTest bundleRelease`
3. Console > the track > Create new release > upload the new `.aab` > release notes > roll out.
4. Keep `targetSdk` within a year of the newest Android release (Play's rule each August).

## Also worth knowing

- **Distributing the APK outside Play.** Google is phasing in developer verification for
  apps installed outside Play on certified Android devices (starting in some countries in
  2026). If you publish the APK directly (GitHub releases), check
  <https://developer.android.com/developer-verification> and register `io.dlne.chat` with
  the signing certificate listed in `android/README.md`.
- **Keep the keystore folder backed up offline.** Losing the upload key is recoverable
  through Play support; losing the key used for direct APK downloads is not.
