# 09 — The Android app: a thin shell around the phone page (2026-10-12)

Ask: Thomas, 2026-10-12: *"Android app (APK) — thin WebView shell … for you to sideload and
try"* (`ROADMAP.md` Phase 2, after D-007). The UI stays the page the box serves; the app adds what
Chrome cannot. Code, build, signing and sideloading: [`android/README.md`](../android/README.md).

**Status: v0.1.1 (2026-10-13: the review fixes, §8), built and tested on emulators, not yet on a
phone or against the box.**
It is not part of Sunday's gate: the Friday checklist (`docs/06-phone.md` §9) proves the PWA in
Chrome. The app becomes the phone path only once it passes the same checklist on the Pixel 4a.

## 1. What it adds over the PWA in Chrome

| | PWA in Chrome (`docs/06-phone.md`) | K1000 app |
|---|---|---|
| Certificate | Install the K1000 CA once in Android settings (needs a screen lock; Android then says *Network may be monitored*) | Built in, trusted **only** for `10.42.0.1` and `k1000.home.arpa`. Nothing to install |
| Wi-Fi | Join K1000 in Settings. With mobile data on, Android may send the page over mobile data: **airplane mode + Wi-Fi** during the show | The app asks Android for K1000 itself and binds **only itself** to it. **Mobile data stays on** for the rest of the phone (calls, messages, maps) |
| After a dropout | Depends on the portal setting and *Stay connected* (`06-phone.md` §5) | The app asks again at once; Android rejoins K1000 **without a dialog** as soon as it sees it (§3) |
| Screen awake | Wake Lock, if the browser grants it | Always, while K1000 is on screen |
| Haptics | `navigator.vibrate` | The same call, backed by Android's `VIBRATE` permission. No native haptic engine yet |
| Microphone (phone-mic spectrum, `night/phone-rta`) | Chrome asks per site | Android asks once; granted to the box's page only, audio only |
| On the home screen | Usually a Chrome-badged shortcut (a WebAPK needs Google's server, unreachable from K1000) | A real app in the app drawer, the K1000 knob icon |
| Back key | Leaves the page | Hides K1000, never closes it (closing would drop the K1000 link) |

The page itself is the same in both (served by the box, updated with the box). The app tells
the page it is there: its user agent ends with `K1000-Android/<version>`. The page does not use
that yet: in the app it still shows its *Install* button and, when the box is unreachable, its
own hint *"Phone in airplane mode + Wi-Fi on"*, which the app makes unnecessary (§7).

## 2. First run

1. Install the APK (`android/README.md` › Install: allow *Install unknown apps* for Files or
   Chrome, then *Install*).
2. Open **K1000**: *Connect to K1000*. On the box: **SETUP › Phone** shows the Wi-Fi code.
   Tap **Scan the Wi-Fi QR code**, allow the camera, scan the **first** code (the Wi-Fi one). If
   the camera will not scan, type the password printed under the code (`abcd-efgh-jkmn` form; the
   name `K1000` is filled in) and tap **Join K1000**.
3. Android 17 and later only: *Allow K1000 to find, connect to, and determine the relative position
   of nearby devices?* › **Allow** (on Android 17 this permission covers the local network, which
   the box is on).
4. Android's own dialog: *Connect to device — K1000 app wants to use a temporary Wi-Fi network to
   connect to your device — K1000* › **Connect**. Once per phone and app: Android remembers it.
5. The K1000 page opens. The box screen switches to the URL code, as with Chrome.

At a gig: Wi-Fi **on** (airplane mode is no longer needed), open K1000. When you close the app for
good (swipe it away in Recents), Android leaves K1000 and goes back to its usual Wi-Fi.

**Fallback** (if a phone refuses the app's own join): *Change K1000 Wi-Fi* › tick **I joined
K1000 in Android's Wi-Fi settings** › *Join K1000*, then join K1000 in Settings as in
`06-phone.md` §3 step 3 (*Stay connected* › Yes, *Don't ask again*). The app then follows the
K1000 network only, recognised by its address (`10.42.0.x`, the box's AP), and binds to it: still
no certificate to install, and mobile data can stay on. Any other Wi-Fi is ignored: when Android
falls back to a saved home or venue Wi-Fi while the box is off, the app stays on *Not connected to
K1000* until the phone is back on K1000 (in this mode Android decides when that is: it may stay on
the Wi-Fi with internet, so rejoin K1000 in Settings).

## 3. How it works

- **Joining K1000.** A `WifiNetworkSpecifier` (SSID + WPA2 passphrase from the QR) passed to
  `ConnectivityManager.requestNetwork`. Android shows its connect dialog the first time and stores
  the approval per app and network; for a password-protected network it matches on the SSID and
  the security type (AOSP `WifiNetworkFactory`, Android 13: *"Only require SSID and SecurityType
  match for non-open networks"*), so later requests connect without a dialog. Forgetting K1000 in
  Android's Wi-Fi settings removes the approval. The network never becomes the phone's default:
  mobile data stays the default. On a phone without dual Wi-Fi (the Pixel 4a, as far as is known),
  joining K1000 leaves the other Wi-Fi network.
  - **Rejoin.** K1000 lost: the app asks again at once, and Android connects when a scan (every
    10 s, screen on) sees K1000. Wi-Fi switched off: such a request is refused on the spot, so the
    app shows *Wi-Fi is off* and asks again the moment Wi-Fi is on. A request must come from the
    app on screen: a rejoin due while K1000 is in the background waits until it is opened again.
    A refusal without Android's dialog showing is retried (3, 6, 9 … s, five times); a *Cancel*
    in the dialog is not (no dialog loop): **Retry** asks again.
  - **Verified on the emulator** (Android 13 and 17, its simulated access point made a WPA2
    "K1000", §8): the dialog, *Connect*, the page; Wi-Fi off then on: rejoined by itself without a
    dialog; app closed: Android leaves K1000; reopened: rejoined without a dialog; approval removed
    and *Cancel*: *Not connected* with Retry and no new dialog; the K1000 network not the default.
    **Not verified**: the access point vanishing while Wi-Fi stays on (out of range, box switched
    off), which relies on Android's periodic scans (AOSP code above); a real phone and the box's
    hostapd.
- **Binding.** `bindProcessToNetwork(K1000)`: every socket the app opens goes over K1000. The
  WebView's network stack runs inside the app's process (Chromium: *"WebView runs other services
  (ex. GPU service, Network Service) in-process on all OS versions"*), so the page, its websocket
  and its service worker follow the binding. **Verified** on the emulator: the page's own requests
  carry Android's per-socket mark for "network explicitly selected" while the process is bound,
  and lose it when the binding is removed (§8). When K1000 is lost the app keeps the binding on
  purpose: the page's reconnection attempts fail at once (measured: under 0.1 s) instead of
  wandering onto mobile data, and the app binds again when K1000 is back and nudges the page to
  reconnect immediately.
  - **Binding refused.** Android may refuse `bindProcessToNetwork`: a VPN that does not allow
    apps to bypass it forbids exactly this (`VpnService.Builder.allowBypass`), and so does
    *Block connections without VPN*. The app then does not show the page (unbound, it would go
    over the VPN or mobile data) but *K1000 blocked on this phone*, and tries the binding again
    every 3 s, on every change of the K1000 network, on resume and on **Retry**: turning the VPN
    off is enough. Verified on the emulator with a simulated refusal (§8), not with a real VPN.
  - A page load that fails while K1000 is up (a just-joined network can drop its first
    connections; with no default network, as in airplane mode, WebView then reports
    `ERR_INTERNET_DISCONNECTED`) is retried by itself after 2, 4, 6, 8 and 10 s, then waits for
    **Retry**. Once the page has loaded once, its service worker opens it from the phone anyway.
- **Trust.** The app's network security config trusts `res/raw/k1000_ca.pem` (the public CA
  certificate) for `10.42.0.1` and `k1000.home.arpa`, and nothing else for them (no system CA, no
  user CA). WebView honours it, IP-literal domain included: **verified** on the emulator (Android 13
  with WebView 109, Android 17 with WebView 145), the service worker included. Behind it,
  `onReceivedSslError` proceeds only when `K1000Trust` accepts the leaf: issued and signed by the
  bundled CA, both valid now, a TLS server certificate, naming the requested host (no wildcards),
  every name inside the CA's name constraints, and the platform's PKIX check passing. Anything
  else is cancelled and the app shows *Not the K1000 box* (verified: a look-alike CA with the same
  name, and a leaf for another address). A certificate error from any other host (only reachable
  outside WebView's interception, e.g. a WebSocket) is cancelled quietly and leaves the page alone.
  - The CA's **name constraints are not what protects the app**: Android's own validator ignores
    a trust anchor's name constraints (seen on the JVM, and on both emulators: a leaf that also
    names `evil.example` was accepted for 10.42.0.1). In the app the network security config already
    limits the CA to the two box names; `K1000Trust` enforces the constraints anyway on its path.
  - **Certificate transparency**: Android 17 enforces CT by default for apps that target it, but
    not on connections that use the app's own trust anchors, so the private K1000 CA is unaffected
    (verified on the Android 17 emulator).
- **Only the box.** The WebView loads `https://10.42.0.1/` (then `https://k1000.home.arpa/` if
  the address does not answer) and nothing else: another link opens in the phone's browser (on the
  phone's normal network), page loads and fetches to any other host get an empty 403 (page and
  service worker), no plain HTTP. WebView's request interception does not see WebSockets or
  WebRTC: for those, the network security config trusts **no CA at all** for any host but the
  box's two names (so a `wss://` to anywhere else fails its TLS handshake, verified §8), and the
  binding keeps every connection on the K1000 network. Safe Browsing and WebView metrics are off,
  so nothing asks Google about the page.
- **Permissions**, each asked when needed: camera (the QR scan), nearby devices / local network
  (Android 17+, before the page), microphone (when the page asks; audio only, the box's page only;
  Android's *unprocessed* mic source is not used yet, the page gets WebView's `getUserMedia`).
  Declared without a prompt: internet, network state, Wi-Fi state, vibrate, audio settings.
- **Storage**: the SSID and password in app-private storage, excluded from backups and device
  transfer. It is the password shown on the box screen: physical access stays the authorisation.
- **Screen**: kept on while K1000 is visible; portrait; rotations, keyboard, dark mode, font size,
  *Bold text*, language and wallpaper/theme colour changes do not restart the page (restarting
  the activity would drop the K1000 link mid-show).
- **Licences**: *Change K1000 Wi-Fi* › **About & licences** shows the version, the GPL-3.0-or-later
  notice, the source address (`https://github.com/Maigre/K1000`), ZXing's Apache-2.0 licence and
  NOTICE, and both licence texts (`android/app/src/main/assets/licenses/`, inside the APK).

## 4. The app's own screens

| Screen | Meaning | What to do |
|---|---|---|
| *Connect to K1000* | First run, or *Change K1000 Wi-Fi* | Scan the Wi-Fi code on the box (SETUP › Phone) or type the password |
| *Joining K1000…* | Android is looking for K1000 (its dialog may be on top the first time) | Tap **Connect** in Android's dialog; stay near the box |
| *Not connected to K1000* + bar moving | The phone left K1000 (range, box off) | Nothing: it rejoins by itself when K1000 is seen |
| *Not connected to K1000* · *Wi-Fi is off* | Wi-Fi was switched off | **Turn Wi-Fi on**: the app rejoins by itself |
| *Not connected to K1000* · *the box's page did not answer* | Joined, but the page did not load | It retries by itself a few times; then check k1000 runs with the phone page on, and **Retry** |
| *Not connected to K1000* + **Retry** | K1000 was not found, or the dialog was declined | Check the box is on with its AP on, then **Retry** |
| *K1000 blocked on this phone* | On K1000, but Android does not let the app use it (a VPN) | Turn the VPN off (or allow apps to bypass it); the app carries on by itself |
| *Not the K1000 box* | The certificate is not from the K1000 CA (another device at 10.42.0.1, or a box with a new CA) | Check you are on the right box; a re-made CA needs a new app build |
| *Allow local network access* | Android 17+, the permission was refused | **Retry**, then **Allow** (after *Don't allow* twice: Retry opens the app's settings › Permissions › Nearby devices) |
| The page | Connected | The page's own states (LINK, NO LINK, OFF…) as in `06-phone.md` |

## 5. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| The scan says *This is not a Wi-Fi QR code* | You scanned the URL or FIRST TIME code: turn E1 on the box back to the **Wi-Fi** code |
| The scan says the code is for an open network or WPA3 | The box's AP is not the K1000 WPA2 one (`setup-ap.sh --status`) |
| *Joining K1000…* for long, no dialog | K1000 is out of range or on a band the phone cannot see (`setup-ap.sh --band bg`, `06-phone.md` §6) |
| Android's dialog does not list K1000 | Same as above; or the password changed on the box (`--new-password`): *Change K1000 Wi-Fi* and scan again |
| Joined, but *Not connected to K1000* with **Retry** | The page did not answer: is k1000 running with the phone page on (SETUP › Phone)? `sudo systemctl status k1000` on the box |
| *Not the K1000 box* after re-making the CA | The app carries the CA it was built with: rebuild it with the new `k1000-ca.crt` (a new leaf from the same CA needs nothing) |
| Every rejoin shows Android's dialog again | K1000 was forgotten in Android's Wi-Fi settings (that removes the approval): tap Connect once more |
| A phone whose maker breaks the app's own join | The fallback: *I joined K1000 in Android's Wi-Fi settings* (§2) |
| *K1000 blocked on this phone* | A VPN app (or *Block connections without VPN*) forbids the app's binding to K1000: turn it off for the show |
| Fallback mode: *Not connected* although the phone shows a Wi-Fi | That Wi-Fi is not K1000 (home, venue): the app only follows a `10.42.0.x` network. Join K1000 in Settings |
| An update will not install (*App not installed*, *package conflicts*) | It was signed with another key: uninstall K1000, install the new one, scan the code again |
| No buzz on the first action | Vibration needs one tap on the page first (WebView's rule, as in Chrome) |

## 6. Build and release

`android/README.md`: Docker build with pinned JDK, SDK tools, Ubuntu packages, Gradle and plugin;
every Maven dependency checked against `android/gradle/verification-metadata.xml` (sha256); the
release key made once in the hub's `secrets/k1000-android/` (never in this repo); the APK, its
SHA-256, `apksigner verify` and the licences file in `secrets/k1000-android/out/`. A GitHub
release is the intended way to hand it out (not done: publishing is Thomas's call); it should
carry `K1000-<version>.LICENSES.txt` next to the APK (GitHub adds the source archive itself).

## 7. Open points

- **Not yet on a real phone or a real box.** The emulator does not cover the access point
  vanishing while Wi-Fi stays on, range, the box's hostapd (PMF off), or the Pixel 4a's own WebView
  build. The Friday checklist steps 1–5 and 9–11 apply to the app too.
- **Power-cut drill (the rejoin with Wi-Fi left on).** Android 13's `WifiNetworkFactory`
  (android13-release) skips the dialog for an approved SSID + security type, scans every 10 s while
  the screen is on, pauses while it is off, and never gives up. On the Pixel 4a, with the app joined
  the normal way (not the fallback): keep K1000 on screen, cut the box's power for **at least
  30 s**, power it back. Expected: *Not connected to K1000* with the bar moving, then the page back
  **without Android's dialog**, within about 10 s of the box's AP coming up (count the box's boot
  on top). Two cases would bring the dialog back: a phone whose second-Wi-Fi feature is enabled but
  unusable (`revokeNormalBypass`; whether the Pixel 4a is one is unknown), and K1000 disconnected by
  hand from quick settings. Note what you see either way (dialog or not, seconds to the page).
- The page could recognise the app (user agent) and hide its *Install* button, its certificate
  hints and its *airplane mode* advice there (a PWA change, `night/phone` side).
- The phone-mic spectrum gets WebView's `getUserMedia` (with the page's own constraints), not
  Android's *unprocessed* source. A native capture bridge is a later step if the measurement needs it.
- The release key is a single file: it belongs in the USB escrow (the hub's secrets sync skips binary
  files). Losing it means every phone must uninstall and reinstall to update.
- A new K1000 CA (not a new leaf) needs a new app build.

## 8. Test record (2026-10-12; v0.1.1 re-run 2026-10-13)

**v0.1.1 (2026-10-13)** fixes what a review of v0.1.0 found:

- *Medium*: the fallback mode followed and bound to **any** Wi-Fi (a saved home or venue Wi-Fi
  joined by Android during a box power cut hid *Not connected* and fed the page an `online`
  event). It now follows a network only once it has an address in `10.42.0.0/24`, tracks every
  candidate, and only the accepted network's loss counts.
- *Medium with a VPN*: a refused `bindProcessToNetwork` was ignored (the page then went over the
  VPN or mobile data). It is now its own state: *K1000 blocked on this phone*, retried by itself.
- *Low*: no CA is trusted any more for hosts other than the box (WebSockets and WebRTC bypass
  WebView's interception); another host's certificate error is cancelled without touching the
  page. *Bold text*, grammatical gender and theme-colour changes no longer recreate the activity
  (which dropped the K1000 link). Every Maven artifact is sha256-verified
  (`android/gradle/verification-metadata.xml`; a tampered checksum was seen to fail the build),
  the plugin portal is gone, Ubuntu packages come from a dated snapshot, and only versioned SDK
  packages go into the build. The APK now carries its notices and licences (About & licences),
  and `build.sh` puts them next to it.

- **Unit tests** (JVM, `./build.sh check`): 23 pass. `WifiQrTest` 7 (the box's exact payload,
  escapes, quoted values, refusals), `K1000HostsTest` 4 (adds the K1000-subnet filter: 10.42.0.x
  in, a home Wi-Fi, the emulator's Wi-Fi, IPv6 and malformed prefixes out), `K1000TrustTest` 12:
  the real K1000 CA and
  box leaf accepted for both names; refused for another host, after 2029-01-14 and before its start;
  a look-alike CA (same name and constraints, another key) refused; leaves outside the constraints
  (extra IP, extra DNS name, subject CN) refused; wrong host, wildcard, CA leaf, client-only leaf
  refused; an unconstrained CA or a leaf is never accepted as the anchor; and a test that pins the
  JVM's PKIX ignoring the anchor's name constraints. **Lint**: no findings (errors abort the build).
- **Emulator smoke test** (`android/tools/smoke.sh`, x86_64 Google APIs images, KVM, emulator
  37.2.12): the real PWA
  files served over HTTPS at 10.42.0.1 from a throwaway name-constrained test CA (bundled in the
  smoke build only), the emulator's simulated access point turned into a WPA2 "K1000".
  **v0.1.1: Android 13 (WebView 109): 37/37. Android 17 (WebView 145): 38/38** (it adds the
  local network permission gate), with the signed v0.1.1 APK (v0.1.0: 33/33 and 34/34). The
  emulators misbehave now and then (the Wi-Fi dropping a fresh client, a system app not
  responding). On 2026-10-13 two Android 17 boots never got a responsive system server (no app
  installed yet), and the first boot from a wiped data partition registered no service worker
  (36/38; the same APK passed 38/38 on the next boot, and on Android 13). The driver waits for a
  stable Wi-Fi and a responsive system, repeats a check that a drop interrupted, and prints the
  page's own service-worker error if that check fails. Checked, both levels:
  - the fallback mode ignoring the emulator's own Wi-Fi (not on 10.42.0.0/24): no binding, no page,
    *Waiting for K1000*; then, pointed at that subnet (smoke build only), following it;
  - TLS through the network security config (no `onReceivedSslError`), secure context, the service
    worker registered and controlling the page; `K1000Trust` accepting the same leaf on the device;
  - `navigator.vibrate` accepted, the microphone granted to the box's page, the camera refused,
    another host's page load blocked inside the WebView, the user agent;
  - a WebSocket to another host refused at TLS, without disturbing the page: to the smoke server by
    its other address (it saw no request), and to `wss://www.google.com/`, whose public
    certificate WebView refused (`onReceivedSslError` ran: the empty base trust anchors at work);
  - the network binding (the page's requests marked as explicitly bound, and not once unbound),
    *Not connected* when Wi-Fi goes, requests failing at once while K1000 is gone, rebinding and
    the page reaching the box when it is back;
  - a refused binding (simulated in the smoke build, as a VPN would refuse it): *K1000 blocked on
    this phone*, the link not called up, nothing reaching the box; allowed again: bound within
    seconds by itself, the view gone, the page reaching the box;
  - refused: a look-alike CA, a leaf for another address (*Not the K1000 box* on screen);
  - the app's own join: the URL code refused as a Wi-Fi code, Android's dialog, Connect, the page;
    K1000 not the default network; Wi-Fi off (*Wi-Fi is off*) then on: rejoined without a dialog;
    app closed: Android leaves K1000; reopened: rejoined without a dialog; Cancel: no dialog loop;
  - the signed release APK itself (minified, the real K1000 CA): it starts on *Connect to K1000*,
    its QR scanner opens, it joins K1000 with a typed password through Android's dialog, and it
    refuses the smoke server's test-CA certificate (*Not the K1000 box*). This check caught a real
    bug before release: ZXing Android Embedded's own scanner activity crashes without AndroidX, so
    the app now scans with the library's camera view inside its activity;
  - observed (not a requirement): Android accepts a leaf naming a host outside the CA's
    constraints; `K1000Trust` refuses it.
- **Not tested**: a real phone, the box, range, the access point vanishing with Wi-Fi on (the
  power-cut drill, §7), a real VPN, the QR camera scan itself (the scan result is injected in the
  smoke build; the parser is unit-tested), touch gestures in the page (long-press "notch here"
  with the app's long-press suppression).
