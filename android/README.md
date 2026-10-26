# DLNE for Android

The native DLNE chat app: Kotlin, Jetpack Compose, Material 3, one activity. Phones only
**use** the DLNE service; they never contribute compute.

- Package: `io.dlne.chat` (debug builds install side by side as `io.dlne.chat.debug`)
- minSdk 26 (Android 8.0), targetSdk / compileSdk 36 (Android 16)
- Two builds of the same app (same package, same signing key), see "Flavors and updates" below:
  `sideload` (the APK on the website, with the in-app updater) and `play` (Google Play, none).
- Permissions: `play` = `INTERNET` only. `sideload` = `INTERNET` + `REQUEST_INSTALL_PACKAGES`
  (to hand a verified update to Android's installer; Android still asks for a tap).
  No analytics, ads, Google Play Services, Firebase or tracking in either.
- Talks to `POST /v1/app/chat/completions` (Server-Sent Events) and `GET /v1/cluster/status`
  on `https://dlne.rossgilsonlabs.dev` by default (`ServerUrl.DEFAULT`, since 1.0.4); the
  address is editable in Settings (https only) for self-hosters. The service's older addresses,
  `https://dlne.duckdns.org` (1.0.1-1.0.3) and `https://73-215-216-195.sslip.io`, still work and
  are still accepted as input; an app that has one saved (an earlier version did) silently
  switches to the new default at start-up
  (`ServerUrl.startupAddress`, `SettingsStore`, tests in `ServerUrlTest` and `SettingsStoreTest`).
- Chats are stored in one JSON file in app-private storage and excluded from cloud backup
  and device transfer.

## Toolchain (already installed on the dev machine)

| What | Where | Version |
|---|---|---|
| JDK (Eclipse Temurin) | `~/.local/jdk-17` | 17.0.21+9 |
| Android SDK | `~/Android/Sdk` | cmdline-tools 22.0, platforms;android-36, build-tools;35.0.0, platform-tools |
| Gradle | wrapper (`./gradlew`, downloads itself, SHA-256 pinned) | 8.14.3 |
| Android Gradle Plugin / Kotlin | `gradle/libs.versions.toml` | 8.13.2 / 2.2.21 |

Every plugin and library version is pinned in `gradle/libs.versions.toml`.

`local.properties` (not committed) tells Gradle where the SDK is. Put your own Android SDK
folder in it (the one `sdkmanager` installs into):

```
sdk.dir=/path/to/Android/Sdk
```

On a fresh machine: install a JDK 17, install the Android command-line tools, then
`sdkmanager "platforms;android-36" "build-tools;35.0.0"` and `sdkmanager --licenses`.

## Build

All commands run from this `android/` directory.

```bash
export JAVA_HOME=~/.local/jdk-17

./gradlew :app:testDebugUnitTest     # unit tests of both flavors (SSE parser, Markdown, client, history, updater)
./gradlew assembleSideloadDebug      # app/build/outputs/apk/sideload/debug/app-sideload-debug.apk
./gradlew assembleRelease            # BOTH release APKs (signed, R8), see the table below
./gradlew assembleSideloadRelease    # only the website APK
./gradlew bundlePlayRelease          # app/build/outputs/bundle/playRelease/app-play-release.aab (for Play)
```

| Flavor | Release APK (signed, R8) | For |
|---|---|---|
| `sideload` | `app/build/outputs/apk/sideload/release/app-sideload-release.apk` | the DLNE website (publish it as `downloads/DLNE-Chat-<version>.apk`) |
| `play` | `app/build/outputs/apk/play/release/app-play-release.apk` | testing; Play gets the `.aab` |

Before 1.0.4 there was one APK, `app/build/outputs/apk/release/app-release.apk`; that path no
longer exists. Install on a phone with USB debugging on:
`adb install -r app/build/outputs/apk/sideload/release/app-sideload-release.apk`.

`gradle.properties` keeps builds light on a shared machine: a 2 GB Gradle heap, at most four
workers, and the Kotlin compiler in-process (no extra daemon). On the dev laptop, run
builds through the machine's memory guard and leave no daemon behind (`ANDROID_HOME` because a
checkout has no `local.properties`):

```bash
~/valheim-testbed/heavy.sh 6G dlne-android env JAVA_HOME=$HOME/.local/jdk-17 ANDROID_HOME=$HOME/Android/Sdk ./gradlew --no-daemon :app:testDebugUnitTest assembleRelease
```

### Live smoke test

One real question to the live service, printed as it streams (skipped in normal test runs):

```bash
./gradlew :app:testDebugUnitTest --tests '*LiveSmokeTest*' -Pdlne.smoke=true
# another server:
./gradlew :app:testDebugUnitTest --tests '*LiveSmokeTest*' -Pdlne.smoke=true -Pdlne.smoke.url=https://your.server
```

## Signing

The release keystore lives **outside the repository**:

```
~/WubarrkCODING/DLNE_RELEASE_KEYS/android/dlne-release.jks        (PKCS12, RSA 4096, alias "dlne", valid 10,000 days)
~/WubarrkCODING/DLNE_RELEASE_KEYS/android/keystore.properties     (storeFile, storePassword, keyAlias, keyPassword)
```

Both files are `chmod 600`. `app/build.gradle.kts` reads `keystore.properties` from that
path when it exists; point it elsewhere with `-Pdlne.keystoreProperties=/path/to/file` or
the `DLNE_KEYSTORE_PROPERTIES` environment variable. Without it, release builds are produced
unsigned (`app-sideload-release-unsigned.apk`, `app-play-release-unsigned.apk`) instead of failing, so anyone can build.
`.gitignore` blocks `*.jks`, `*.keystore` and `keystore.properties` as a second lock.

Signing certificate SHA-256:
`EC:51:E6:66:0B:3C:FD:57:86:C5:D9:94:8D:DE:B2:74:38:52:E6:38:B7:5B:A4:C2:B7:4D:49:3C:E4:D8:DE:71`

**Back up that folder offline.** If the key is lost, an app distributed outside Play can
never be updated under the same identity again. For Google Play this key is the *upload key*
(see `play/README.md`): Play re-signs installs with its own app-signing key, and a lost
upload key can be reset through Play Console support.

## Releasing a new version

1. In `app/build.gradle.kts`, raise `versionCode` by 1 (Play rejects any upload whose
   versionCode is not higher than every earlier one) and set `versionName` (what people see,
   e.g. `1.0.2`).
2. `./gradlew :app:testDebugUnitTest assembleRelease bundlePlayRelease`
3. **Google Play:** upload `app/build/outputs/bundle/playRelease/app-play-release.aab` to Play
   Console. The R8 mapping file (`app/build/outputs/mapping/playRelease/mapping.txt`) is
   embedded in the bundle, so Play de-obfuscates crash traces automatically.
4. **The website** (people who sideload; this is also how running apps learn about the update):
   1. Copy `app/build/outputs/apk/sideload/release/app-sideload-release.apk` to the site's
      `downloads/` folder as `DLNE-Chat-<versionName>.apk`. Keep the older APKs there.
   2. Write `downloads/android.json` **after** the APK is in place (see "Flavors and updates"):
      `sha256sum` and `stat -c %s` of exactly the file you copied. Write it last: the moment it
      says a higher `versionCode`, installed apps start offering the update.
   3. Check it: `curl -s https://dlne.rossgilsonlabs.dev/downloads/android.json` and
      `curl -s <url in it> | sha256sum` must give the hash in the manifest.
   Keep a copy of `mapping/sideload*/mapping.txt` for reading crash traces from that APK.

## Project layout

```
app/src/main/java/io/dlne/chat/
  MainActivity.kt            single activity, edge-to-edge, share target (ACTION_SEND text/plain)
  AppGraph.kt                process-wide singletons (API client, settings, history store)
  net/Sse.kt                 SSE byte->line buffer and event parser (split reads, UTF-8, [DONE], notices)
  net/SearchEvents.kt        the service's {"dlne": ...} lookup events, with hostile-content cleaning
  net/DlneApi.kt             OkHttp client: streaming chat, status, answer reports, HTTP/429 error mapping
  net/ServerUrl.kt           default server address and https-only validation
  data/                      conversation model (with sources), JSON-file store, settings, history, reports
  markdown/Markdown.kt       the safe Markdown subset parser ([n] citations included)
  ui/                        Compose screens: chat, drawer, settings, report sheet, sources, Markdown, theme
app/src/sideload/            the updater, sideload flavor only (manifest with the permission + receiver, strings)
  java/io/dlne/chat/update/    UpdateManifest (strict parser), UpdateStore (12 h interval, "Later"), UpdateNetwork
                               (no-redirect OkHttp, hashing downloader), Verification (SHA-256, signer, APK checks),
                               UpdateInstaller (PackageInstaller session + receiver), UpdateController, UpdateHooks (UI)
app/src/play/                three empty functions (UpdateHost, UpdateBanner, UpdateSettingsSection): no updater at all
app/src/test/                unit tests (+ the opt-in LiveSmokeTest); FlavorManifestTest checks both merged manifests
app/src/testSideload/        updater tests (manifest, interval, "Later", hashing, certificates, downloader)
play/                        Google Play listing, policy and form answers, upload guide
tools/generate_dlne_icons.py regenerates every icon asset from the official logo (see "Icons")
```

## Flavors and updates

`app/build.gradle.kts` has one flavor dimension, `distribution`, with two flavors. Both use the
package `io.dlne.chat` and are signed by the same release key here (with Play App Signing, what
Play installs carries Google's key instead, so the two channels are not interchangeable; see
`play/README.md`). They differ like this:

| | `sideload` | `play` |
|---|---|---|
| Where | the APK on https://dlne.rossgilsonlabs.dev | Google Play |
| Permissions | `INTERNET`, `REQUEST_INSTALL_PACKAGES` | `INTERNET` |
| Updater | yes (`BuildConfig.SELF_UPDATE = true`) | none: no permission, no manifest address, no download or installer code |

Google Play does not let an app update itself outside Play, and `REQUEST_INSTALL_PACKAGES` is a
restricted permission there, so the Play build must never contain any of it. `FlavorManifestTest`
reads both merged release manifests on every test run and fails if `play` has the permission or
the receiver, or if `main`/`play` sources mention the installer or the manifest address. To check
by hand: `aapt2 dump permissions <apk>`.

### How an update reaches a phone (sideload)

1. **Check.** When the app comes to the screen, at most once every 12 hours (the time of the last
   *successful* check is stored), and when someone taps *Settings > Updates > Check for updates*.
   *Check for updates automatically* (default on) turns the automatic check off. No service,
   no WorkManager: it only runs while the app is open.
2. **The manifest.** `https://dlne.rossgilsonlabs.dev/downloads/android.json`, a static file next to
   the APKs. The address is fixed in the app (`UpdateSource`); it does **not** follow the server
   address someone typed into Settings. No cookies or identifiers are sent; no redirect is followed.
   ```json
   {"versionCode": 6, "versionName": "1.0.5", "minSdk": 26,
    "url": "https://dlne.rossgilsonlabs.dev/downloads/DLNE-Chat-1.0.5.apk",
    "sha256": "<64 hex>", "size": 2017073, "notes": "What changed, one or two lines.", "published": "2026-10-27"}
   ```
   It is accepted only if: `versionCode`, `minSdk` and `size` are JSON integers (`versionCode` up
   to 2147483647, `size` from 100,000 bytes to just under 100 MB); `versionName` is 1-32 characters
   of letters, digits and `._+-`; `sha256` is 64 hex digits; `url` is `https://dlne.rossgilsonlabs.dev/downloads/<name>.apk`
   exactly (default port, no user info, query, fragment, `%`, `\`, `..` or subfolder); `notes`
   (optional) is text, shown flattened to one line of at most 300 characters; `published`
   (optional) is `yyyy-MM-dd`. Anything else is ignored whole. Extra fields are ignored.
   Only a **strictly higher `versionCode`** than the installed app is an update, and a `minSdk`
   above the phone's Android version means "not for this phone" (nothing is offered).
3. **The offer.** A card at the top of the chat and a row in Settings: "DLNE Chat 1.0.5 is
   available. <notes> One tap to install: Android will ask you to confirm. [Update] [Later]".
   *Later* hides the chat card for that version until the next check (Settings still offers it).
   The offer is remembered (re-validated) across restarts until the next check replaces it.
4. **Update.** If Android has "install unknown apps" off for DLNE Chat, one sentence explains it
   and Android's settings page for the app opens (`ACTION_MANAGE_UNKNOWN_APP_SOURCES`); the
   update continues when the person comes back. Then the APK downloads with OkHttp into
   `cache/updates/` with a progress bar and a Cancel button (no redirects, so it can only come from
   the official host). While downloading, the byte count and SHA-256 are checked against the manifest:
   a different size or hash discards the file. Next, PackageManager reads the file: it must be
   `io.dlne.chat`, have exactly the manifest's `versionCode`, be newer than the installed app, and be
   signed by the same certificate as the installed app (`GET_SIGNING_CERTIFICATES` on Android 9+,
   `GET_SIGNATURES` below; a key rotation is accepted only with a signing lineage that contains the
   installed certificate). Only then is the file streamed into a `PackageInstaller` session (hashed
   once more on the way) and committed. Android shows its own confirmation, which the app starts
   from the visible activity (it waits for the app to be on screen if needed). The file is deleted
   right after the commit, and on any failure; leftovers and stale sessions are cleaned at start.
5. **Failure.** Anything that fails (validation, size, hash, package, version, signer, installer)
   shows a plain message and installs nothing; the current app stays.

Nothing is silent or privileged: Android always asks the person to confirm an install of a
sideloaded app, and the UI says so ("One tap to install").

Limits to know: an app installed from Play has the Play flavor (no updater); a sideload install
only updates to APKs signed with the same key; debug builds (`io.dlne.chat.debug`) will refuse the
update as "not this app". A person on 1.0.3 or older has no updater and installs 1.0.4+ by hand once.

## Icons

There is exactly one DLNE icon: `vscode-extension/media/dlne_logo.png`, the cyan-and-pink
glowing brain with "DLNE" under it. Nothing else may be a DLNE mark.

- **Launcher icon** (`res/mipmap-anydpi/ic_launcher.xml`): an adaptive icon. Foreground = the
  logo inset 17% on every side, so it fills 66% of the 108 dp layer (inside the safe zone
  that no launcher mask crops); background = `#060B14`, the logo's own backdrop colour;
  monochrome (Android 13+ themed icons) = `drawable-nodpi/ic_launcher_monochrome.png`, the
  brain alone cut out of the logo (no lettering). minSdk is 26, so no legacy `mipmap-*dpi`
  PNGs exist.
- **In-app marks** (empty chat screen, drawer header): `R.drawable.dlne_logo`
  (`res/drawable-nodpi/dlne_logo.png`), a byte-for-byte copy of the official file. There is no
  drawn or vector mark anywhere in the app. The remaining `res/drawable/ic_*.xml` files are
  plain toolbar glyphs (send, back, menu...), not DLNE marks.
- **Play assets** (`play/icon-512.png`, `play/feature-graphic.png`): see `play/listing.md`.

All of these are produced by one script; run it after the official logo ever changes:

```bash
~/.venvs/dlne/bin/python android/tools/generate_dlne_icons.py   # needs Pillow and numpy
```

It also rewrites `tools/official-icon-hashes.txt`. `OfficialIconPolicyTest` (part of
`:app:testDebugUnitTest`) fails if any other launcher icon, drawable or image exists, if code
references a mark that is not the official logo, if a generated file was edited by hand, or if
the Play icon is not exactly the official logo as a 512 x 512 32-bit PNG.

## Behaviour notes

- **Streaming.** Bytes are split into lines before UTF-8 decoding, so a `data:` line or a
  multi-byte character cut across network reads is reassembled correctly. Malformed lines
  are ignored. Stop cancels the HTTP call, which also tells the server to stop.
- **Service notices.** The service sends its own messages in-band as a final content chunk
  `"\n\n[...]"` (engine/app_pool.py `_sse_notice`). The app shows those in a quieter style,
  with Retry, and never sends them back to the model.
- **Lookups and sources.** The server can look things up for the model (weather, Wikipedia,
  news, web search) and says so in-band with `data: {"dlne": {...}}` lines:
  `searching` (tool + query; the latest is shown as "Checking the weather for “…”" etc.
  until the answer text starts), `sources` (the whole list so far, replacing the previous
  one), `search_note` and `search_unavailable` (muted lines under the sources), and
  `retract` (server 2.3.5+: the model began answering from memory while unsure, so the
  server takes that text back and looks the question up; the app empties the reply, keeps
  its bubble and drops any sources held for it, and the usual `searching`, `sources` and a
  fresh answer follow). Unknown `dlne` events are dropped, never shown. Source titles, hosts and URLs are treated as
  hostile: flattened to one plain line without control or bidi characters, capped, only
  http(s) URLs become links, and the host shown is the one the link really opens. `[n]` in
  the answer becomes a tappable superscript only when n is in that answer's list. Saved
  chats keep their sources and notes. Under an answer the sources are folded into one
  tappable line, "› 10 sources · patriots.com, nytimes.com, bostonglobe.com +3" (1.0.3+), and
  only a tap shows the full list; an unfolded list stays open while the answer streams, once
  it is saved, and across rotation (not across app restarts).
- **Links.** Settings > "Website and community" opens the DLNE website
  (`https://dlne.rossgilsonlabs.dev`) and the DLNE Discord for news and help
  (`https://discord.gg/RNxvxWn8cw`), and the chat drawer has the same two links under
  Settings. They open in the browser through `openInBrowser`, like the coffee link; the URLs
  are the `WEBSITE_URL` and `DISCORD_URL` constants in `ui/SettingsScreen.kt`.
- **Report answer.** The flag under each answer opens a sheet (reason + optional note,
  ≤ 500 characters) and sends `POST /v1/app/report` with the answer (≤ 8,000 characters)
  and the question it answered (≤ 4,000). 404 shows "Reporting isn't available on this
  server"; 429 shows when to try again.
- **Throttling.** On HTTP 429 the server's `detail` is shown and Send is disabled until the
  `Retry-After` time passes (5 s if the header is missing).
- **History sent.** The newest ~12,000 characters of the conversation (about 3k tokens) go
  with each request; answers are capped at 1,024 tokens by the server.
- **Upgrading to compileSdk 37 (Android 17).** Needs Android Gradle Plugin 9.x, which moves
  Kotlin support into AGP (the `org.jetbrains.kotlin.android` plugin is dropped). Target
  API 36 satisfies Google Play's requirement for new apps and updates until August 2027.
