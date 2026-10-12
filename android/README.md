# K1000 for Android

A thin WebView shell around the phone page the box already serves (`https://10.42.0.1/`, D-007):
the UI stays on the box, and the app adds what a PWA in Chrome cannot do. It joins the K1000
Wi-Fi itself while mobile data stays on, trusts the K1000 CA for the box only (no CA install in
Android settings), keeps the screen awake, and lets the page buzz and use the microphone. What it
adds, the first run and troubleshooting: [`docs/09-android.md`](../docs/09-android.md).

- Package `gr.trentesept.k1000`, app name **K1000**, Android 10+ (minSdk 29), targets Android 17
  (API 37). Thomas's Pixel 4a (Android 13) is the reference phone.
- Java only, no AndroidX, no Play Services. One third-party library: the QR scanner, ZXing Android
  Embedded 4.3.0 + ZXing core 3.5.4 (both Apache-2.0, compatible with GPL-3.0-or-later). The app
  uses the library's camera view inside its own activity; the library's `CaptureActivity` needs
  AndroidX at run time, so it is removed from the manifest.
- Sideloaded APK (GitHub releases later), never on the Play Store. Licence: GPL-3.0-or-later.
  The APK carries its notices (`app/src/main/assets/licenses/`: NOTICE with the source address,
  the GPL-3.0 and Apache-2.0 texts), shown in the app under *Change K1000 Wi-Fi* › **About &
  licences**; `build.sh` also puts them next to the APK as `K1000-<version>.LICENSES.txt`.

## Layout

| Path | What |
|---|---|
| `app/src/main/java/gr/trentesept/k1000/MainActivity.java` | The one activity: the WebView, the native panels (setup, QR scanner, joining, not connected), permissions, back key |
| `…/K1000Link.java` | Asks Android for the K1000 Wi-Fi (`WifiNetworkSpecifier`), or follows the one joined in Settings (10.42.0.x only), and binds the process to it; a refused binding is its own state |
| `…/K1000Trust.java` | The explicit TLS check against the bundled CA (behind `onReceivedSslError`) |
| `…/WifiQr.java` | Parses the box's Wi-Fi QR code (`WIFI:T:WPA;S:K1000;P:…;;`) and typed passwords |
| `…/K1000Hosts.java` | The only hosts the WebView may load: `10.42.0.1` and `k1000.home.arpa`, HTTPS |
| `app/src/main/res/raw/k1000_ca.pem` | The K1000 CA **certificate** (public; its key never leaves mgr-ran) |
| `app/src/main/res/xml/network_security_config.xml` | That CA, for the two box names only; no CA at all for any other host |
| `app/src/main/assets/licenses/` | NOTICE (K1000's GPL notice + source address, ZXing's notices), `GPL-3.0.txt` (= the repo's `LICENSE`), `Apache-2.0.txt` |
| `gradle/verification-metadata.xml` | The sha256 of every Maven artifact the build may use (plugin, tools, libraries) |
| `app/src/test/` | Unit tests; `resources/certs/` = public test certificates (`tools/make_test_certs.sh`) |
| `docker/Dockerfile` | The build image (JDK 17 + SDK command-line tools) and the emulator image |
| `build.sh` | Build, lint, test, sign, verify, all in Docker |
| `tools/smoke.sh` | Emulator smoke test (`smoke_server.py` stands in for the box, `smoke_driver.py` checks) |
| `tools/make_icons.py` | Launcher icon from the PWA's own maskable icon |

## Build

Needs Docker only. Everything is pinned:

| | Version |
|---|---|
| JDK | Eclipse Temurin 17.0.20+8 (`eclipse-temurin:17.0.20_8-jdk-noble`, by digest) |
| Ubuntu packages (unzip, python3; the emulator's libraries) | the archive as of 2026-09-20 (`APT::Snapshot`, snapshot.ubuntu.com) |
| SDK command-line tools | 22.0 (`commandlinetools-linux-15859902`, sha256 checked) |
| Gradle | 9.6.1 (wrapper jar and distribution sha256 checked) |
| Android Gradle Plugin | 9.4.1 |
| SDK | platform `android-37.0`, build-tools 36.0.0 |
| Libraries | ZXing Android Embedded 4.3.0, ZXing core 3.5.4; tests: JUnit 4.13.2 |
| Maven artifacts (all of the above that Gradle fetches) | sha256-checked against `gradle/verification-metadata.xml`; Google's Maven and Maven Central only |

The emulator test alone uses sdkmanager's latest `platform-tools`, `emulator` and system images
(they cannot be pinned there; `tools/smoke.sh` prints their revisions). The build never uses them.

```sh
cd android
./build.sh check      # lint + unit tests
./build.sh            # lint + unit tests + the signed release APK (needs the keystore)
./build.sh gradle assembleDebug     # any Gradle task
```

The SDK packages and the Gradle caches live in Docker named volumes (`k1000-android-sdk`,
`k1000-android-gradle`), not in the image or the repo. Without Docker: JDK 17 + the Android SDK,
then `./gradlew lint test assembleRelease` with `K1000_KEYSTORE_PROPERTIES` set (below).

**Changing a dependency** (a version in a `build.gradle.kts`, Gradle, the plugin): Gradle refuses
any artifact whose sha256 is not in `gradle/verification-metadata.xml`. Regenerate it over every
variant, check the diff names only what you changed, and commit it with the change:

```sh
./build.sh gradle --write-verification-metadata sha256 --rerun-tasks \
  lint test assembleRelease assembleDebug assembleSmoke
```

## Sign

The release key is made **once** and never enters this repo:

```sh
./build.sh keystore   # refuses to overwrite an existing one
```

It writes `k1000-release.jks` (PKCS12, RSA 4096, valid 50 years) and `keystore.properties`
(random password) to `K1000_SIGNING_DIR`, by default the 37Projects hub's gitignored
`secrets/k1000-android/` (directory 700, files 600). Gradle reads it through
`K1000_KEYSTORE_PROPERTIES`; without it, a release build comes out unsigned. `./build.sh` copies
the signed APK to `$K1000_SIGNING_DIR/out/K1000-<versionName>.apk`, with its `.sha256` and
`apksigner verify` output.

**Keep the keystore safe** (it is a binary file: the hub's secrets sync does not carry it, so it
belongs in the USB escrow). A K1000 update must be signed with the same key to install over the
previous version; with another key, Android refuses it and the old app must be uninstalled first
(which also forgets the stored Wi-Fi password).

The signing certificate, to check an APK before installing it:

```
CN=K1000 Android release, O=Trente-Sept K1000
SHA-256 85:ff:f9:65:e4:6d:4b:50:23:47:40:d1:38:4b:83:a6:21:0a:0a:43:93:f8:e9:ca:85:98:ad:1d:e2:d4:0a:8f
```

## Install (sideload)

1. Put `K1000-<version>.apk` on the phone: USB (the phone in *File transfer* mode, copy it to
   `Download/`), Seafile, a mail to yourself, or (later) the GitHub release page in the phone's
   browser.
2. Tap it in **Files** (Files by Google › Downloads), or in Chrome's downloads. The first time,
   Android says the app (Files or Chrome) is not allowed to install unknown apps: **Settings** ›
   **Allow from this source** (also at Settings › Apps › Special app access › **Install unknown
   apps** › Files › Allow). Go back, **Install** (or **Update** over an earlier K1000).
3. Play Protect may ask to scan or warn about an unknown developer: **Scan** or **Install anyway**.
   The app is not on the Play Store, that is expected.
4. Optional: switch *Allow from this source* off again for Files.

From a computer instead: `adb install -r K1000-<version>.apk`.

Check the file first if it came over a channel you do not trust: `sha256sum` against the
`.sha256`, or `apksigner verify --print-certs` against the SHA-256 above.

**Updates**: raise `versionCode` and `versionName` in `app/build.gradle.kts`, build, and install
over the old app: the stored Wi-Fi password and the K1000 approval stay.

## Tests

- `./build.sh check`: Android lint (errors abort the build) and the JVM unit tests:
  `WifiQrTest` (the box's exact QR format, escapes, refusals), `K1000HostsTest` (what the WebView
  may load, which origin may use the microphone, the K1000-subnet filter of the fallback mode),
  `K1000TrustTest` (the real K1000 CA and box
  leaf, a look-alike CA with the same name, name-constraint violations, wrong host, CA leaf,
  client-only leaf, expiry).
- `tools/smoke.sh [33|37] [pwa-static-dir]`: boots an x86_64 emulator (KVM) with Android 13 or
  17, its simulated access point turned into a WPA2 "K1000", serves the real PWA files over HTTPS
  from a throwaway test CA at 10.42.0.1, and checks the app end to end: the fallback mode ignoring
  a Wi-Fi off the K1000 subnet, TLS, service worker, haptics, microphone, foreign page loads
  blocked and a foreign WebSocket refused at TLS, the network binding, a refused binding (as under
  a VPN), refused certificates, the app's own join (Android's dialog, rejoin without it, Cancel),
  and the signed release APK itself.
  Results: `docs/09-android.md` §8. The emulator is the SDK's stable one (37.2.12, a test tool
  only; it is newer than two weeks on 2026-10-12, the build itself does not use it).
