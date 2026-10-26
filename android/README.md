# DLNE for Android

The native DLNE chat app: Kotlin, Jetpack Compose, Material 3, one activity. Phones only
**use** the DLNE service; they never contribute compute.

- Package: `io.dlne.chat` (debug builds install side by side as `io.dlne.chat.debug`)
- minSdk 26 (Android 8.0), targetSdk / compileSdk 36 (Android 16)
- Permission: `INTERNET` only. No analytics, ads, Google Play Services, Firebase or tracking.
- Talks to `POST /v1/app/chat/completions` (Server-Sent Events) and `GET /v1/cluster/status`
  on `https://dlne.duckdns.org` by default (`ServerUrl.DEFAULT`); the address is editable in
  Settings (https only) for self-hosters. The service's old address,
  `https://73-215-216-195.sslip.io`, still works and is still accepted as input; an app that
  has it saved (an earlier version did) silently switches to the new default at start-up
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

./gradlew :app:testDebugUnitTest     # unit tests (SSE parser, Markdown, client, history)
./gradlew assembleDebug              # app/build/outputs/apk/debug/app-debug.apk
./gradlew assembleRelease            # app/build/outputs/apk/release/app-release.apk  (signed, R8)
./gradlew bundleRelease              # app/build/outputs/bundle/release/app-release.aab (for Play)
```

Install on a phone with USB debugging on: `adb install -r app/build/outputs/apk/release/app-release.apk`.

`gradle.properties` keeps builds light on a shared machine: a 2 GB Gradle heap, at most four
workers, and the Kotlin compiler in-process (no extra daemon). On the dev laptop, run
builds through the machine's memory guard and leave no daemon behind:

```bash
~/valheim-testbed/heavy.sh 6G dlne-android env JAVA_HOME=$HOME/.local/jdk-17 ./gradlew --no-daemon assembleRelease
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
unsigned (`app-release-unsigned.apk`) instead of failing, so anyone can build.
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
   e.g. `1.0.1`).
2. `./gradlew :app:testDebugUnitTest assembleRelease bundleRelease`
3. Upload `app/build/outputs/bundle/release/app-release.aab` to Play Console. The R8
   mapping file (`app/build/outputs/mapping/release/mapping.txt`) is embedded in the bundle,
   so Play de-obfuscates crash traces automatically; keep a copy for APKs shared elsewhere.
4. Record the APK's SHA-256 (`sha256sum app/build/outputs/apk/release/app-release.apk`) if
   the APK is published anywhere else.

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
app/src/test/                unit tests (+ the opt-in LiveSmokeTest)
play/                        Google Play listing, policy and form answers, upload guide
tools/generate_dlne_icons.py regenerates every icon asset from the official logo (see "Icons")
```

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
  one), `search_note` and `search_unavailable` (muted lines under the sources). Unknown
  `dlne` events are dropped, never shown. Source titles, hosts and URLs are treated as
  hostile: flattened to one plain line without control or bidi characters, capped, only
  http(s) URLs become links, and the host shown is the one the link really opens. `[n]` in
  the answer becomes a tappable superscript only when n is in that answer's list. Saved
  chats keep their sources and notes.
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
