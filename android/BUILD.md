# DGX Chat — Android build (Capacitor shell)

How to build the release APK of **DGX Chat** (`com.dgxchat.app`) from this
repository. Everything here is generic: no machine-specific paths, no bundled
signing key.

```
dgxchat/
  app/dist/                  ← web bundle (built by `pnpm build` in app/)
  android/                   ← THIS: Capacitor project root (package.json, capacitor.config.json, www/)
  android/android/           ← generated native Gradle project
  android/keystore/          ← YOUR release keystore (git-ignored)
  android/dist-out/          ← APK artifacts (git-ignored)
  android/tooling/           ← make_icons.py, build_release.ps1, verify_apk.ps1
```

> Note the nesting: `cap sync` runs from `dgxchat/android`, while `gradlew` runs
> from `dgxchat/android/android`.

---

## 0. Requirements

| Component | Version |
|---|---|
| JDK | 17 (Capacitor 6 / AGP 8.2.1 toolchain) |
| Android SDK | platform **android-35**, build-tools **35.0.0**, platform-tools |
| Node.js | 20+ |
| pnpm | 9+ |
| Gradle | via the wrapper (downloaded on first run) |
| Capacitor | 6.2.3 (`@capacitor/cli`, `@capacitor/core`, `@capacitor/android`) |

Every shell that builds must export:

```sh
export JAVA_HOME=/path/to/jdk-17
export ANDROID_HOME=/path/to/android-sdk
export ANDROID_SDK_ROOT="$ANDROID_HOME"
export PATH="$JAVA_HOME/bin:$ANDROID_HOME/platform-tools:$PATH"
```

```powershell
$env:JAVA_HOME        = "C:\path\to\jdk-17"
$env:ANDROID_HOME     = "C:\path\to\android-sdk"
$env:ANDROID_SDK_ROOT = $env:ANDROID_HOME
$env:Path             = "$env:JAVA_HOME\bin;$env:ANDROID_HOME\platform-tools;" + $env:Path
```

Gradle finds the SDK through `ANDROID_HOME`; if you prefer a file, create
`android/android/local.properties` (git-ignored) with `sdk.dir=<path>`.

Settings: compileSdk / targetSdk / minSdk = **35 / 35 / 24**, versionName
`1.0.0`, versionCode `1`, appId/appName `com.dgxchat.app` / `DGX Chat`.

### Why Capacitor 6 and not 7/8

Capacitor 7 and 8 templates compile with **JDK 21** and target compileSdk
35/36. Capacitor 6.2.3 builds with JDK 17 + AGP 8.2.1, so it is the version this
project pins. `android/android/variables.gradle` overrides its
`compileSdkVersion`/`targetSdkVersion` to 35 (Capacitor 6 defaults to 34), and
`android.suppressUnsupportedCompileSdk=35` in `android/android/gradle.properties`
silences AGP 8.2.1's "newer than tested" warning.

---

## 1. Build the web bundle, then sync

```sh
cd app && pnpm install && pnpm build        # -> app/dist
cd ../android && pnpm install               # @capacitor/* 6.2.3
npx cap sync android
```

`android/capacitor.config.json` is the single source of truth; `cap sync` copies
it into the APK assets verbatim:

```json
{
  "appId": "com.dgxchat.app",
  "appName": "DGX Chat",
  "webDir": "../app/dist",
  "server": { "androidScheme": "https" },
  "android": { "allowMixedContent": true, "webContentsDebuggingEnabled": true }
}
```

If `cap sync` reports "Could not find the web assets directory", `webDir` is
wrong or `app/dist` has not been built.

---

## 2. Build the release APK

```sh
cd android
npx cap sync android
cd android && ./gradlew assembleRelease --console=plain     # gradlew.bat on Windows
```

Output: `android/android/app/build/outputs/apk/release/app-release.apk`

Or use the helper (sets nothing globally, stops on the first failure):

```powershell
cd android
pwsh -NoProfile -File .\tooling\build_release.ps1
pwsh -NoProfile -File .\tooling\build_release.ps1 -OutputName DGX-Chat-preview.apk -SkipVerify
pwsh -NoProfile -File .\tooling\verify_apk.ps1 -Apk .\dist-out\DGX-Chat.apk
```

**Rebuilding with a new web bundle** is the same two steps: `pnpm build` in
`app/`, then `cap sync android` + `assembleRelease`. No native change is needed.

---

## 3. Signing

No keystore and no password is committed. To produce a signed release APK,
create your own key and point the build at it:

```sh
keytool -genkeypair -v -keystore android/keystore/release.jks \
  -alias release -keyalg RSA -keysize 2048 -validity 10000 \
  -dname "CN=DGX Chat, OU=DGX, O=DGX, L=Local, ST=NA, C=CN"
```

Then either export the environment variables

```sh
DGX_KEYSTORE_FILE=android/keystore/release.jks
DGX_KEYSTORE_PASSWORD=...
DGX_KEY_ALIAS=release
DGX_KEY_PASSWORD=...
```

or create `android/keystore/keystore.properties` (git-ignored):

```properties
storeFile=../../keystore/release.jks
storePassword=...
keyAlias=release
keyPassword=...
```

`android/android/app/build.gradle` reads the env vars first, then that file.
**If neither is present, `assembleRelease` still succeeds and produces an
unsigned APK** — it never falls back to a shared, publicly-known key.

The CI workflow (`.github/workflows/release-apk.yml`) uses repository secrets
for the same four values; see that file.

---

## 4. Verification

```powershell
pwsh -NoProfile -File .\tooling\verify_apk.ps1 -Apk .\dist-out\DGX-Chat.apk
```

prints identity, permissions, cleartext flags, signature, size and sha256 in one
go. Raw equivalents:

```sh
$ANDROID_HOME/build-tools/35.0.0/aapt2 dump badging app.apk
$ANDROID_HOME/build-tools/35.0.0/aapt2 dump xmltree app.apk --file AndroidManifest.xml
$ANDROID_HOME/build-tools/35.0.0/apksigner verify --print-certs -v app.apk
```

Expected identity:

```
package: name='com.dgxchat.app' versionCode='1' versionName='1.0.0' compileSdkVersion='35'
minSdkVersion:'24'   targetSdkVersion:'35'
uses-permission: android.permission.INTERNET
uses-permission: android.permission.ACCESS_NETWORK_STATE
uses-permission: android.permission.RECORD_AUDIO
uses-permission: android.permission.MODIFY_AUDIO_SETTINGS
application-label:'DGX Chat'
```

---

## 5. Networking: cleartext HTTP + CORS (this is the part that bites)

The backend may be plain HTTP on your LAN, so three things matter:

1. **Manifest** — `android:usesCleartextTraffic="true"` **and**
   `android:networkSecurityConfig="@xml/network_security_config"`, whose
   `base-config` sets `cleartextTrafficPermitted="true"`. Android 9+ would
   otherwise block plain HTTP. Cleartext is allowed for all hosts because the
   server address is chosen at runtime; switch it off in that XML once your
   backend has HTTPS.
2. **WebView origin** — Capacitor keeps `androidScheme: "https"`, so the bundle
   loads from `https://localhost`, a **secure context** (required by
   `getUserMedia`, `MediaRecorder`, `AudioContext`, clipboard).
3. **Mixed content** — the page is `https://localhost` while the API is
   `http://…`. `allowMixedContent: true` makes Capacitor call
   `WebSettings.setMixedContentMode(MIXED_CONTENT_ALWAYS_ALLOW)`.

**Server-side requirement (not fixable in the APK):** the backend must answer
the WebView's requests with `Access-Control-Allow-Origin: https://localhost`
(echo the exact origin) and must handle the `OPTIONS` preflight for
`Authorization` / `x-api-key` / `content-type`. A curl smoke test:

```sh
curl -i -H "Origin: https://localhost" http://YOUR-SERVER:8439/models
# expect: HTTP/1.1 200 (or 401) AND access-control-allow-origin: https://localhost
```

---

## 6. Permissions and the microphone

`AndroidManifest.xml` declares `INTERNET`, `ACCESS_NETWORK_STATE`,
`RECORD_AUDIO`, `MODIFY_AUDIO_SETTINGS`. Capacitor's
`BridgeWebChromeClient.onPermissionRequest()` maps
`android.webkit.resource.AUDIO_CAPTURE` to `[MODIFY_AUDIO_SETTINGS,
RECORD_AUDIO]` and shows the OS prompt the first time the web app calls
`getUserMedia({audio:true})`. `MODIFY_AUDIO_SETTINGS` must be declared too,
otherwise the whole permission request is denied.

---

## 7. Icons and splash

Generated with Pillow — no binary blobs from elsewhere:

```sh
python android/tooling/make_icons.py
```

Writes `res/mipmap-{mdpi..xxxhdpi}/ic_launcher.png`, `ic_launcher_round.png`,
`ic_launcher_foreground.png` (adaptive-icon foreground), `res/drawable/splash.png`,
and the showcase `dist-out/DGX-Chat-icon-{512,1024}.png`.
Design: dark rounded square, "DGX" wordmark, electric-blue accent bar.
Adaptive background colour `#0E1620` lives in `res/values/ic_launcher_background.xml`.
Override the font with `ICON_FONT=/path/to/Bold.ttf` if the default search fails.

---

## 8. Notes and limitations

- Gradle's first run downloads the distribution (~130 MB) plus AGP artifacts;
  later builds are incremental.
- The build is not minified/obfuscated on purpose: Capacitor's bridge is
  reflective (JS interface names, plugin classes), and R8 is not worth the risk
  for a 3 MB app.
- No emulator or device is required to build; runtime smoke testing (install,
  launch, mic, CORS) still has to happen on a real phone.
