# 09 — The Android app: a thin shell around the phone page (2026-10-12; 0.2 and 0.2.1: 2026-10-13)

Ask: Thomas, 2026-10-12: *"Android app (APK) — thin WebView shell … for you to sideload and
try"* (`ROADMAP.md` Phase 2, after D-007). The UI stays the page the box serves; the app adds what
Chrome cannot. Code, build, signing and sideloading: [`android/README.md`](../android/README.md).
The app ↔ page contract: [`android/BRIDGE.md`](../android/BRIDGE.md).

**Status: v0.2.1 (2026-10-13): v0.2.0 plus the K1000 Wi-Fi taken from the box. A phone that
joined K1000 with the camera's Wi-Fi QR code needs no scan in the app: on its Connect screen the app
reads the Wi-Fi from the box over the pinned TLS and joins it itself from then on (§2, §3). The box
serves the APK from its first-time page (`06-phone.md`). Built and tested on emulators (§8),
reviewed and rebuilt the same day (use only that build), not yet on a phone or against the box.**
v0.2.0 (phone v2 design §4 / S3: the v2 page by default, the page bridge, immersive, Back,
haptics, diagnostics, the bench build) was never installed on a phone either; neither was v0.1.1.
Sunday 18 Oct is a field test (Thomas, 2026-10-13). The APK is used on Sunday only once the
release key is escrowed to USB and the real-phone checklist (§10) passes at Friday's rig test;
Chrome on `/v2/` or `/` stays the fallback, then the knobs, then the bypass.

## 1. What it adds over the PWA in Chrome

| | PWA in Chrome (`docs/06-phone.md`) | K1000 app |
|---|---|---|
| Certificate | Install the K1000 CA once in Android settings (needs a screen lock; Android then says *Network may be monitored*) | Built in, trusted **only** for `10.42.0.1` and `k1000.home.arpa`. Nothing to install |
| Wi-Fi | Join K1000 in Settings. With mobile data on, Android may send the page over mobile data: **airplane mode + Wi-Fi** during the show | The app asks Android for K1000 itself and binds **only itself** to it. **Mobile data stays on** for the rest of the phone (calls, messages, maps). The password: taken from the box when the phone is on K1000 already (0.2.1), or scanned once |
| After a dropout | Depends on the portal setting and *Stay connected* (`06-phone.md` §5) | The app asks again at once; Android rejoins K1000 **without a dialog** as soon as it sees it (§3) |
| Screen awake | Wake Lock, if the browser grants it | Always, while K1000 is on screen |
| Haptics | `navigator.vibrate` only (no per-detent quality) | v1: the same `navigator.vibrate`. v2 (0.2): Android's own haptic effects (tick, click, confirm, reject, long press). The texture (tick, click, long press) follows the phone's *Touch feedback* setting; the answers (the mute ack, a refusal, link lost) always play (§3b) |
| Microphone (phone-mic spectrum, `night/phone-rta`) | Chrome asks per site | Android asks once; granted to the box's page only, audio only |
| On the home screen | Usually a Chrome-badged shortcut (a WebAPK needs Google's server, unreachable from K1000) | A real app in the app drawer, the K1000 knob icon |
| Back key | Leaves the page | Closes the page's overlays first; at the top level the app's menu, and Back again (or a 1 s hold) hides K1000. Never closes it (closing would drop the K1000 link) |
| Which page | What the address bar says | **v2** (`https://10.42.0.1/v2/`) by default; **v1** (`/`) on THIS PHONE's choice, remembered (§2b) |
| Full screen | The browser's bars stay | **Immersive**: status and navigation bars hidden, a swipe from the edge shows them for a moment (the page can turn it off) |
| Edge swipes | A slide near a side edge can fire Back | The page names its slide areas; Android keeps the Back gesture off them (exclusion rects) |
| Diagnostics | — | WebView, Android, model, the unprocessed mic source, the microphones, haptic hardware, gesture areas, an edge test (§4) |

The page itself is the same in both (served by the box, updated with the box). The app tells
the page it is there: its user agent ends with `K1000-Android/<version>`, and from 0.2 the app
opens a message port to the page (`android/BRIDGE.md`): the page feature-detects each capability
and asks for haptics, immersive, exclusion rects, keep-awake and diagnostics there. v1 does not
use it: in the app it still shows its *Install* button and, when the box is unreachable, its
own hint *"Phone in airplane mode + Wi-Fi on"*, which the app makes unnecessary (§7).

## 2. First run

**From the box (0.2.1, the usual way).** Nothing to scan in the app, nothing to carry over USB.
On the Pixel 4a (Android 13), with the box up, SETUP › PHONE on its screen:

1. **Join K1000 with the camera.** The **first** QR code (Wi-Fi): Camera app, or Settings ›
   Network & internet › Internet › the **⊞ QR icon** › scan › *Join*. *K1000 has no internet
   access — Stay connected?* › **Yes** (`06-phone.md` §3 step 3).
2. **The first-time page.** E1 turn to the **third** code, FIRST TIME (`http://10.42.0.1/`), scan
   it, open it in **Chrome**. Tap **Get the K1000 app** (it shows the version and the first 8 hex
   of the APK's SHA-256; no button = this box has no APK: §6).
3. **Install.** Chrome may warn about the download (an APK, over plain http on the box's own Wi-Fi):
   **Download anyway** / **Keep**. Tap the download, then, once per phone: *Your phone is not allowed
   to install unknown apps from this source* › **Settings** › **Allow from this source** › Back ›
   **Install** (*Update* over an earlier K1000). Play Protect: **Install anyway** (not on the Play
   Store, expected). Then **Open**.
4. **The app takes the Wi-Fi from the box.** The *Connect to K1000* screen shows *The phone is on
   the K1000 Wi-Fi: asking the box for it…* for a moment, then *Joining K1000…* with the line
   **K1000 Wi-Fi taken from the box**.
5. Android's own dialog: *Connect to device — K1000 app wants to use a temporary Wi-Fi network to
   connect to your device — K1000* › **Connect**. Once per phone and app: Android remembers it.
6. The K1000 page opens (the box screen switches to the URL code, as with Chrome); Android says
   *Viewing full screen · To exit, swipe down from the top* once › **Got it**. From then on the app
   joins K1000 by itself at every launch, as after a scan.

Android 17 and later ask *Allow K1000 to find, connect to, and determine the relative position of
nearby devices?* before step 4 › **Allow** (that permission covers the local network, which the box
is on; without it the app cannot reach the box at all).

Leave the K1000 that step 1 saved in Android's Wi-Fi settings as it is: forgetting it there also
removes the app's approval (§3), and the next join shows the dialog of step 5 once more.

If Android's dialog of step 5 (or of a later launch) says *No devices found* while the phone is on
K1000 from step 1 (**[verify on the 4a]**, §10 item 1): *Cancel* › *Change K1000 Wi-Fi* (its
line says the box's Wi-Fi is the app's, but the app is not on it) › tick **I joined K1000 in
Android's Wi-Fi settings** › **Join K1000**: the app follows the K1000 the camera joined (the
fallback, §2b), and the page opens.

**Scan or type (any box, and the APK from elsewhere).** If the phone is not on K1000 when the app
opens, or the box is older than this (it then says *This box does not hand its Wi-Fi to the app (an
older K1000)*), it is as in 0.2.0:

1. Install the APK (`android/README.md` › Install).
2. Open **K1000**: *Connect to K1000*. On the box: **SETUP › Phone** shows the Wi-Fi code.
   Tap **Scan the Wi-Fi QR code**, allow the camera, scan the **first** code (the Wi-Fi one). If
   the camera will not scan, type the password printed under the code (`abcd-efgh-jkmn` form; the
   name `K1000` is filled in) and tap **Join K1000**.
3. Android 17+: the nearby devices question above › **Allow**.
4. Android's *Connect to device* dialog › **Connect**, then the page, as above.

At a gig: Wi-Fi **on** (airplane mode is no longer needed), open K1000. When you close the app for
good (swipe it away in Recents), Android leaves K1000 and goes back to its usual Wi-Fi.

## 2b. Which page: v2, and the way back to v1

From 0.2 the app opens **v2** at `https://10.42.0.1/v2/` (the phone v2 design, the field test).
**v1** at `https://10.42.0.1/` is one hold away, remembered on this phone until changed:

- **Back** at the page's top level opens the app's menu (top of the screen; MUTE stays free):
  **hold** *Classic page (v1)* (a tap only says to hold). The same place says *New page (v2)* to
  return.
- **Long-press the K1000 icon** on the home screen: *Classic page (v1)*, *New page (v2)*,
  *Diagnostics*.
- From v2 itself: THIS PHONE › UI v1 (the page sends `set_ui`).

Choosing the page already on screen reloads nothing. The shortcuts reach the app through a
non-exported alias: another app sending the same actions to K1000 changes nothing.

A box without the v2 page (today's box, before the S1 deploy) answers 404 at `/v2/`: the app then
shows v1 for that session with a toast, and tries v2 again at its next start.

**Fallback** (if a phone refuses the app's own join): *Change K1000 Wi-Fi* › tick **I joined
K1000 in Android's Wi-Fi settings** › *Join K1000*, then join K1000 in Settings as in
`06-phone.md` §3 step 3 (*Stay connected* › Yes, *Don't ask again*). The app then follows the
K1000 network only, recognised by its address (`10.42.0.x`, the box's AP), and binds to it: still
no certificate to install, and mobile data can stay on. Any other Wi-Fi is ignored: when Android
falls back to a saved home or venue Wi-Fi while the box is off, the app stays on *Not connected to
K1000* until the phone is back on K1000 (in this mode Android decides when that is: it may stay on
the Wi-Fi with internet, so rejoin K1000 in Settings).

## 3. How it works

- **The Wi-Fi taken from the box (0.2.1).** While the *Connect to K1000* screen is up (first run,
  or *Change K1000 Wi-Fi*), the app watches the phone's Wi-Fi networks as the fallback mode does.
  When one has an address in `10.42.0.0/24`, it asks the box, through that network
  (`Network.openConnection`, no proxy): `GET https://10.42.0.1/api/wifi` (then
  `https://k1000.home.arpa/api/wifi` if the address does not answer). The answer
  `{"v":1,"ssid":"K1000","psk":"…","band":"…","box":"<build id>"}` must come over TLS that passes
  **both** checks the page's does: the network security config during the handshake (the K1000 CA,
  for the box's two names only), then `K1000Trust` on the leaf (the CA's name constraints
  included). Then it must be a 200 `application/json` of at most 4 KB, strict UTF-8, one JSON
  object, `v` = 1, an SSID of 1 to 32 bytes without control characters, a password of 8 to 63
  printable ASCII characters (the scan's own rules). It is stored exactly where a scan stores it
  (app-private, no backup), and the app joins it itself (JOIN mode below): later launches need
  neither the box's answer nor the Settings network. No redirect is followed, nothing is cached,
  plain HTTP is never used (no cleartext anywhere in the app), only the box's two names are asked
  (never a bench host), and no intent or link can hand the app a password. The password is never
  logged. Code: `BoxWifi.java` (the contract, pure), `BoxWifiProbe.java` (the request).
  - **Why this is safe:** the box answers only on its AP's HTTPS listener, to clients on the AP's
    own subnet (box side, `06-phone.md`): whoever can ask already knows the password, being on
    K1000. The pin proves to the app that it is the real box: a rogue access point named K1000,
    even on `10.42.0.0/24` at `10.42.0.1`, cannot show a K1000-CA certificate, so the app sends it
    nothing but the TLS handshake and takes nothing (*does not answer with the K1000 certificate*
    on the Connect screen). A Wi-Fi on any other subnet is never asked at all.
  - **When it does not ask:** the fallback box *I joined K1000 in Android's Wi-Fi settings* is
    ticked (that choice stands: no switching between the two modes behind the user's back); the
    Connect screen is not up (a phone already set up never asks again); the app is in the
    background; Android 17+ without the local network permission (asked once by itself per
    Connect screen; refused, the screen's line says so, and a tap on that line asks again).
  - **What each answer does:** a valid one is taken (or, when it is the Wi-Fi the app already
    joins, nothing changes; when the app has that Wi-Fi but is not on it, after Android's dialog
    found no K1000 or was cancelled, nothing changes either, and the line points to the fallback
    box instead of saying the app is on it); 404 (an older box, or no password file on the box)
    leaves today's Connect screen with one line saying so; a refused certificate or an unusable
    answer is never asked again on that Connect screen; no answer at all (timeout, a connection
    refused, closed or reset, also inside TLS during or after the handshake, 429, 503) is asked
    again after 2 and 4 s, three tries in all. Only a certificate the pin refuses counts as *not
    the K1000 certificate*. Leaving and reopening the Connect screen starts over: a request still
    running from before does not count, and the reopened screen asks as soon as it ends.
  - Diagnostics' `wifi_from` says where the stored Wi-Fi came from: `box <build id>, band <band>`,
    `scan`, `typed`, or the fallback.
- **Joining K1000.** A `WifiNetworkSpecifier` (SSID + WPA2 passphrase from the QR or the box) passed to
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

## 3b. The page bridge, immersive, Back, haptics (0.2)

The contract is [`android/BRIDGE.md`](../android/BRIDGE.md); in short:

- **Bridge.** A `WebMessagePort` per page load, posted to the box's origin only (never `*`), JSON
  strings, validated in `Bridge.java` before anything happens (unknown types, wrong fields,
  oversized messages refused whole, answered `{t:"err"}`). No `addJavascriptInterface`.
- **Immersive** by default (`WindowInsetsController`, bars shown for a moment by a swipe from the
  edge, cutout mode *short edges* on Android 11+); the page can turn it off, and on Android 11+
  can take the cutout area itself (it then gets `{t:"insets"}`). Android 10 keeps the page out of
  the cutout.
- **Edge gestures.** The page sends the rectangles it slides in (CSS px); the app converts them
  with the density and sets them as system-gesture exclusion on the WebView. Android limits that
  to 200 dp per edge except in sticky immersive [AOSP source; check with Diagnostics › Edge test].
- **Back**: the app's panels, then the page's overlays (`{t:"back"}` → `back_handled`, 400 ms),
  then the app's menu; a second Back within 2 s hides K1000 (`moveTaskToBack`, never `finish`). A
  1 s hold hides it at once on Android 14+ and 10–12; Android 13 (the Pixel 4a) does not report a
  held Back, so there it is the double Back.
- **Haptics**: `performHapticFeedback` (CLOCK_TICK at most 25/s, VIRTUAL_KEY, CONFIRM, REJECT,
  LONG_PRESS, and the double/triple acks); a predefined `VibrationEffect` only when the system
  played nothing while Touch feedback is on. Android's *Touch feedback* setting silences the
  texture (tick, click, long press). The answers (confirm, the double mute ack, reject, the
  triple no-answer) cannot be turned off (DESIGN §5.5): with Touch feedback off they play
  through the vibrator with the alarm usage.
- **Screen on** by default, at every page load; the page may turn it off.
- **Microphone**: unchanged, WebView's `getUserMedia` for the box's page only (the phone RTA).
  The native raw mic (UNPROCESSED) is not in 0.2; Diagnostics says whether the phone has it.

## 4. The app's own screens

| Screen | Meaning | What to do |
|---|---|---|
| *Connect to K1000* | First run, or *Change K1000 Wi-Fi* | Phone on K1000 already: nothing, the app asks the box (next rows). Otherwise scan the Wi-Fi code on the box (SETUP › Phone) or type the password |
| *…asking the box for it…* (amber, Connect screen) | The phone is on a `10.42.0.x` Wi-Fi: the app reads `/api/wifi` | Wait a second: *K1000 Wi-Fi taken from the box*, then *Joining K1000…* |
| *This box does not hand its Wi-Fi to the app (an older K1000, or no Wi-Fi password file on it)* | The box answered 404 at `/api/wifi` | Scan the code or type the password (or tick the fallback) |
| *…does not answer with the K1000 certificate: nothing was taken from it* | The Wi-Fi at `10.42.0.x` is not the K1000 box (or the box has a new CA) | Check it is your box's Wi-Fi; a re-made CA needs a new app build |
| *The box's answer could not be used (…)* · *The box did not answer on this Wi-Fi* | A malformed answer; or no answer three times | Scan the code or type the password |
| *The box's Wi-Fi is the one this app already has, but the app is not on it…* | The app has this Wi-Fi, but its own join did not happen (Android's dialog found no K1000, or was cancelled) | Dialog said *No devices found*: tick *I joined K1000 in Android's Wi-Fi settings*, **Join K1000** (the fallback, §2b). Otherwise *Cancel* › **Retry** |
| *Without the nearby devices permission (local network), the app cannot ask the box…* | Android 17+, the permission refused on the Connect screen | Tap that line › **Allow** (after *Don't allow* twice the tap opens the app's settings › Permissions › Nearby devices) |
| *Joining K1000…* | Android is looking for K1000 (its dialog may be on top the first time) | Tap **Connect** in Android's dialog; stay near the box |
| *Not connected to K1000* + bar moving | The phone left K1000 (range, box off) | Nothing: it rejoins by itself when K1000 is seen |
| *Not connected to K1000* · *Wi-Fi is off* | Wi-Fi was switched off | **Turn Wi-Fi on**: the app rejoins by itself |
| *Not connected to K1000* · *the box's page did not answer* | Joined, but the page did not load | It retries by itself a few times; then check k1000 runs with the phone page on, and **Retry** |
| *Not connected to K1000* + **Retry** | K1000 was not found, or the dialog was declined | Check the box is on with its AP on, then **Retry** |
| *K1000 blocked on this phone* | On K1000, but Android does not let the app use it (a VPN) | Turn the VPN off (or allow apps to bypass it); the app carries on by itself |
| *Not the K1000 box* | The certificate is not from the K1000 CA (another device at 10.42.0.1, or a box with a new CA) | Check you are on the right box; a re-made CA needs a new app build |
| *Allow local network access* | Android 17+, the permission was refused | **Retry**, then **Allow** (after *Don't allow* twice: Retry opens the app's settings › Permissions › Nearby devices) |
| The page | Connected | The page's own states (LINK, NO LINK, OFF…) as in `06-phone.md` |
| The app's menu (top bar) | Back at the page's top level | **Back again** within 2 s hides K1000; *Hide K1000*, *Diagnostics*, *Reload page*; **hold** *Classic page (v1)* / *New page (v2)*, **hold** *K1000 Wi-Fi*. Closes by itself 5 s after it was last touched (working the page under it does not keep it open); never covers the footer (MUTE) |
| *Diagnostics* | The menu, the launcher shortcut, or the page | The phone's answers (below); *Test haptics* plays every kind by name; *Edge test*; *Copy* (to paste to Claude) |
| *Edge test* (20 s) | Diagnostics › Edge test | Two amber bands at the side edges are excluded from Back: swipe in inside a band (Back must not fire) and outside (it fires, counted on screen). The bars show during the test (in full screen no swipe fires Back, so the test would prove nothing); the page's full screen comes back after |

## 5. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| On K1000 (joined with the camera), but the Connect screen does not ask the box | The fallback box *I joined K1000 in Android's Wi-Fi settings* is ticked (untick it), or Android 17's nearby devices permission was refused: the Connect screen then says so; tap that line › **Allow** (or close the app and open it again: a new Connect screen asks once more) |
| *This box does not hand its Wi-Fi to the app* | The box predates `/api/wifi`, or has no password file (`setup-ap.sh --status`): scan the code |
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
| No buzz on the first action | Vibration needs one tap on the page first (WebView's rule, as in Chrome; v1's `navigator.vibrate`) |
| No ticks or clicks in v2, but the mute ack still buzzes | Android's *Touch feedback* is off (Settings › Sound & vibration › Vibration & haptics): the texture follows it, the answers do not. Diagnostics says `haptics: OFF in Android settings` |
| No haptics at all, not even the mute ack | Android's *Use vibration & haptics* or *Alarm vibration* is off, or the phone has no vibrator (Diagnostics › `has_vibrator`) |
| The app shows v1 although v2 was chosen | The box has no `/v2/` yet (a toast said so), or v1's service worker answered for `/v2/` (box-side fix, `android/BRIDGE.md` › Box-side notes) |
| v1 opens broken (or as v2) right after v2 was used | The same service-worker bug: it stored `/v2/` as `/`. Reload once (Back › *Reload page*); the box-side fix is in `android/BRIDGE.md` › Box-side notes |
| v2 misbehaves mid-show | Back › hold *Classic page (v1)*, or Chrome on `/` |
| A slide near a side edge fires Back | The page's exclusion rects did not cover it, or Android's 200 dp limit: Diagnostics › *Edge test* and `exclusion_rects` |

## 6. Build and release

`android/README.md`: Docker build with pinned JDK, SDK tools, Ubuntu packages, Gradle and plugin;
every Maven dependency checked against `android/gradle/verification-metadata.xml` (sha256); the
release key made once in the hub's `secrets/k1000-android/` (never in this repo); the APK, its
SHA-256, `apksigner verify` and the licences file in `secrets/k1000-android/out/`. A GitHub
release is the intended way to hand it out (not done: publishing is Thomas's call); it should
carry `K1000-<version>.LICENSES.txt` next to the APK (GitHub adds the source archive itself).

**The box hands it out too (0.2.1):** its first-time page (`http://10.42.0.1/`, the PHONE page's
third QR) offers *Get the K1000 app* when the box was deployed with an APK: `install.sh` installs
the newest `box/apk/K1000-<version>.apk` (never in git) to `/opt/k1000/apk/`, and the deploy drops
the signed release APK there before copying the tree (`06-phone.md`, first-time page). The page
shows the version and the first 8 hex of the APK's SHA-256: compare them with
`K1000-<version>.apk.sha256`.

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
  hints and its *airplane mode* advice there (v2 does it; v1 does not).
- The phone-mic spectrum gets WebView's `getUserMedia` (with the page's own constraints), not
  Android's *unprocessed* source. A native capture bridge is a later step (DESIGN §4.3, after the
  gig); 0.2's Diagnostics reads whether the phone supports UNPROCESSED and lists its microphones.
- Not in 0.2.x (after the gig, APK 0.3): volume-key mute, an update prompt for the APK the box
  serves (0.2.1's box serves it on its first-time page, the app does not check it yet),
  `hello.min_app`, show lock, brightness, orientation.
- **[verify on the 4a]** the first run from the box (§2): with K1000 saved in Settings by the
  camera's join *and* the app's own request, what does Android do on the app's first request
  (dialog, then the app's network), on every later launch with that saved K1000 connected (the
  emulator joined by itself in about 5 s there, §8, but its radio is not a phone's), and with the
  app closed near the box (the saved K1000 rejoins like any saved Wi-Fi; with *Stay connected*
  accepted, check that mobile data stays the phone's internet)?
- The release key is a single file: it belongs in the USB escrow (the hub's secrets sync skips binary
  files). Losing it means every phone must uninstall and reinstall to update.
- A new K1000 CA (not a new leaf) needs a new app build.

## 8. Test record (2026-10-12; v0.1.1 re-run 2026-10-13; v0.2.0 and v0.2.1 2026-10-13)

**v0.2.1 after its review (2026-10-13, rebuilt).** Still 0.2.1 / versionCode 4: the first 0.2.1
build never reached a phone. Use only this build (sha256 in `K1000-0.2.1.apk.sha256`:
`c5ea7d1e…1f25ec7`). Fixed (app side; the review found nothing that leaks the password, takes it
from an unpinned host, a redirect or an intent, or loops between the two modes):
- *Medium–low*: every `SSLException` read as a fake box, so a connection that dropped during or
  after the TLS handshake (the edge of range, the box's service restarting) showed *does not
  answer with the K1000 certificate*, final for that Connect screen, and the user had to scan. Only
  a refused certificate counts now (`SSLPeerUnverifiedException`, or a `CertificateException` /
  `CertPathValidatorException` among the causes or suppressed exceptions, or a message naming
  one); any other is no answer: the other name, then asked again. Each TLS failure's cause chain
  is logged (`box wifi: TLS failure at …`; the password is never in it). Seen on both emulators: a
  close in the middle of the handshake is `SSLHandshakeException: connection closed`, a reset is
  `SocketException: Connection reset`; a look-alike CA is `SSLHandshakeException` ←
  `CertificateException` ← `CertPathValidatorException`.
- *Low*: a Connect screen reopened while an earlier request was still running (Change K1000 Wi-Fi ›
  Cancel › Change K1000 Wi-Fi, a slow box) did not ask the box until the Wi-Fi changed; and a
  screen shown again over a running request counted that request's no answer as its own, final
  after one try. The tries now know which Connect screen a request was asked on (`BoxWifi.Tries`):
  an earlier screen's answer is dropped and the screen up asks at once, its own try 1.
- *Low*: after the app took the box's Wi-Fi and Android's dialog found no K1000 (or was
  cancelled), *Change K1000 Wi-Fi* said *the one this app already joins*, although the app was not
  on it. That is its own answer now (`SAME_UNJOINED`): nothing taken again (the same dialog would
  come back), and the line points to the fallback box.
- *Low, Android 17*: the local network permission refused on the Connect screen was never asked
  again in that run, and the *Retry* the docs gave does not exist there. The line now says so, and a
  tap on it asks again (App info after *Don't allow* twice); each new Connect screen asks once by
  itself.
- *Test gap*: the relaunch was checked only after the Settings network had been forgotten. A
  relaunch with the camera's K1000 still saved and connected (the state of every later launch) is
  checked now, and the Pixel checklist (§10 item 1) asks for it.

Results:
- **`./build.sh check`: lint no issues, 61 unit tests pass** (+3 in `BoxWifiTest`: only a
  certificate failure is a fake box, dropped connections inside TLS included, with the log line's
  cause chain; the same Wi-Fi while the app is not on it; an answer to an earlier Connect screen).
  Release signed with the K1000 key (signer `85:ff:f9:65:…:0a:8f`, v3, `apksigner`: Verifies).
- **New emulator checks** (`smoke_server.py --tls-cut`, `--wifi-reset`, `--wifi-slow`), on
  Android 13 and 17:
  - the box closes the first connection mid-handshake and resets the next request: two no answers
    (`SSLHandshakeException: connection closed`, `SocketException: Connection reset`), the third
    try takes the Wi-Fi; never *not the K1000 certificate*;
  - the app takes the Wi-Fi, Android's dialog (often *No devices found* on both emulators while
    the Settings network holds K1000), *Cancel*, *Change K1000 Wi-Fi*: the line says
    the app is not on it (not *already joins*), no re-join; then tick the fallback box, *Join
    K1000*: fallback mode, link up, the page loads (the way out §2 gives);
  - a slow box (its answer over 10 s): Connect screen, *Cancel*, Connect screen again: the old
    answer is dropped, the reopened screen asks by itself (try 1) and says *already joins* 15 s
    after reopening;
  - relaunched with the camera's K1000 saved again in Settings and connected: joined by itself in
    JOIN mode in 5 to 8 s, no dialog, `/api/wifi` not asked. **The emulator joins there; a phone
    is still to be seen** (§10 item 1);
  - Android 17: the permission refused on the Connect screen: no prompt loop, the line, a tap on
    it, *Allow*: the box is asked and the Wi-Fi taken (the box never asked before *Allow*).
- **Android 13 emulator (WebView 109): 75/75**, the new signed release APK included: the 70 of
  the first build and 5 new ones. Alone (`K1000_SMOKE_ONLY=boxwifi`): 12/12. A second release
  build from the same tree gave the same bytes.
- **Android 17 emulator (WebView 145): 77/77**, the new signed release APK included: the 71 of
  the first build and 6 new ones (the permission check is Android 17's). Alone
  (`K1000_SMOKE_ONLY=boxwifi`): 14/14. One earlier full run gave 76/77: the 0.2.0 check of the
  binding allowed again after a simulated VPN refusal (`fetch -> 503`), the one that also failed in
  the first build's second run; it passed in the next run, and the code under it did not change.

**v0.2.1 (2026-10-13), first build: the K1000 Wi-Fi taken from the box** (superseded by the
rebuild above). versionCode 4.
- **`./build.sh check`: lint no issues, 58 unit tests pass** (+11: `BoxWifiTest`: the contract's
  answer; the password never in a log line; `v` must be 1; the SSID's and the password's rules
  (32 bytes, multi-byte characters, control characters, 8 to 63 printable ASCII); one JSON object,
  no duplicate field, strict UTF-8, at most 4 KB; only a 200 `application/json` (404 = an older
  box, 429/503 = later, a redirect or anything else = not taken); HTTPS to the box's two names only,
  bench build included; the same Wi-Fi changing nothing; when to ask (the fallback ticked never
  asks, Android 17's permission asked once); three tries per network and Connect screen).
  Release signed with the K1000 key (signer `85:ff:f9:65:…:0a:8f`, v3); that first
  `K1000-0.2.1.apk` was SHA-256 `bf0ba125…379e83` (a second build from the same tree gave the same
  bytes).
- **Android 13 emulator (WebView 109): 70/70**, the signed 0.2.1 release APK included: the 63
  checks of 0.2.0 and 7 new ones (also alone, `K1000_SMOKE_ONLY=boxwifi`: 7/7). The new ones, with
  the phone on the emulated K1000 joined in Settings and the app fresh on its Connect screen:
  - an older box (404 at `/api/wifi`): asked once, the Connect screen stays with its line, nothing
    stored, no join;
  - a look-alike box (a CA with the same name, another key): the TLS handshake fails, `/api/wifi`
    never reaches it, nothing taken;
  - the box: one `/api/wifi` over the pinned TLS; stored where a scan stores it (`mode` JOIN, the
    SSID, the password, `wifi_from` = `box smoke-1, band bg`); the joining screen says *K1000 Wi-Fi
    taken from the box*; Android's dialog, **Connect**, link up in JOIN mode, the page;
  - relaunched: joined by itself in JOIN mode in 5 to 8 s, no dialog, no scan, `/api/wifi` not
    asked again;
  - the Connect screen opened while the app is on K1000 itself (Back › menu › hold *K1000
    Wi-Fi*): the box gives the same Wi-Fi, nothing re-joins;
  - the signed release APK, first run on a Wi-Fi off `10.42.0.0/24`: the box is not asked.
- **Android 17 emulator (WebView 145): the 7 new checks pass in both runs**, the release APK's
  included. The whole suite: 70/71, then 69/71. Each run failed different 0.2.0 checks, and each
  of those passed in the other run: first *a box without `/v2/`* (the fake box saw only the `/v2/`
  request), then the simulated VPN binding refusal and its recovery (the emulated Wi-Fi did not come
  back within the check's 60 s). These are the Android 17 emulator's known Wi-Fi hiccups (0.2.0
  below), on code 0.2.1 does not change. The Pixel 4a is Android 13, where the suite is clean.
- **Seen on the emulator, not the app's:** its radio reports **no scan results while it is
  associated** (`WifiNetworkFactory: Received 0 scan results` with K1000 connected), so Android's
  dialog for the app's own join cannot list K1000 while the network joined in Settings holds it
  (*Searching for device…*, then *No devices found*). AOSP's `WifiNetworkFactory` (Android 13)
  does not exclude the connected network from its matches, and a real phone scans while connected,
  so the test drops the Settings network once the app has asked, and the dialog then finds K1000.
  **[verify on the 4a]** (§10 item 1): the dialog lists K1000 while the phone is still on it.

**v0.2.0 after its review (2026-10-13, rebuilt).** Still 0.2.0 / versionCode 3: the first 0.2.0
build never reached a phone. Use only this build (sha256 in `K1000-0.2.0.apk.sha256`). Fixed:
- *Low–medium*: a page choice that changed nothing reloaded the page (a `set_ui` re-sent at each
  `ready` would loop), and any installed app could send the shortcuts' actions to the exported
  activity (a reload mid-show). Now the page already on screen is never reloaded, and the
  shortcuts go through a non-exported alias: the same actions sent to `MainActivity` are ignored.
- *Medium–low*: with Android's *Touch feedback* off, the answers (mute ack, refusal, link lost)
  were silent too, against DESIGN §5.5. They now play through the alarm vibration then; the
  texture (tick, click, long press) still follows the setting.
- *Low–medium*: any touch on the page kept the app's menu open over the status strip. Only a
  touch on the menu does now.
- *Low*: Diagnostics › Edge test ran in full screen, where no swipe fires Back, so it could not
  fail. It shows the bars for its 20 s now.
- *Low, Android 10 only*: the page was drawn under the camera cutout without padding. Android 10
  keeps the default cutout mode now.
- *Low*: the tick cap allowed 26/s (38 ms). 40 ms: 25/s exactly.
- *Test gaps*: the smoke build is now minified with the release rules, so every bridge check runs
  on R8's output; holds on the page are checked; the Friday checklist (§10) gained the band-chip
  hold and the graph long-press.

Results:
- **`./build.sh check`: lint no issues, 47 unit tests pass** (+10: `ShellRulesTest` 7,
  `HapticsTest` 3). Release signed with the K1000 key (signer `85:ff:f9:65:…:0a:8f`, v3).
- **Android 13 emulator (WebView 109): 63/63**, the smoke build minified, the new signed release
  APK included. New checks, all passing:
  - `set_ui v2` three times on the v2 page: no reload;
  - a shortcut action sent straight to `MainActivity`: ignored, no page load; through the alias:
    v2; the same shortcut again with v2 on screen: no reload. The dynamic shortcuts point at
    `gr.trentesept.k1000.Shortcut`;
  - the app's menu closed 5 s after it opened while the page under it was tapped every 0.8 s;
  - Edge test: `innerHeight` 851 → 803 (bars shown) → 851 after; a swipe inside a band: no
    Back; outside: counted;
  - a still 0.9 s hold on the page (`touch-action` none and pan-y): `pointerup` at 904 ms, no
    `pointercancel`, no context menu. **The app's long-press suppression eats no hold.**
- **Android 17 emulator (WebView 145): 64/64**, a clean run (the local network permission check
  added), the same new checks and the two Wi-Fi rejoin checks that failed on the first 0.2 build
  included. Android 17 counts now.
- **Measured for the page side** (INFO, a scrollable page, a 0.9 s hold drifting straight down;
  the same on Android 13 and 17):
  on `touch-action: pan-y` (v1's band chips) WebView took the gesture and sent `pointercancel`
  once the finger had moved about **8 CSS px**: 4 and 8 px of drift ended in `pointerup`; 11 px
  in `pointercancel` at 674 ms; 16 px in `pointercancel` at 476 ms, before v1's 600 ms. On
  `touch-action: none` the same 16 px drift ended in `pointerup`. So on v1 a band-chip hold dies
  silently on a few px of vertical drift, before the page's own 12 px rule. Chrome shares the
  engine.
- **Seen on the emulator, box side:** once v1's service worker is installed, the shortcut to v2
  loaded `/v2/` with v1's cached shell (no bridge `ready`): the `sw.js` bug in
  `android/BRIDGE.md` › Box-side notes.

**v0.2.0 (2026-10-13), first build** (superseded by the rebuild above)

- **Build.** `./build.sh gradle lint test assembleRelease assembleBench`. Lint: no issues.
  Release signed with the K1000 release key (signer SHA-256 `85:ff:f9:65:…:0a:8f`, v3 scheme).
  The bench build is signed with the debug key.
- **Unit tests: 37 pass.** That is the 23 of 0.1.1, plus:
  - `BridgeTest` 8: every page message accepted and refused, nesting and size limits, CSS px to
    pixel rects (density 2.75, rounded outward, clipped), the app's own messages;
  - `MiniJsonTest` 4;
  - `K1000HostsTest` +2: the v1/v2 URLs, the bridge's target origin, the bench host.
- **Emulator smoke test, Android 13 (WebView 109), the AVD now with a navigation bar: 58/58,**
  the signed 0.2.0 release APK included. That is the final run; three earlier runs found and
  fixed test-side issues: the full-screen hint over the screen, the v2 path in the join check,
  and the keep-awake check reading the WebView's own hold.
- **New in 0.2**, against `tools/bridge_test.html` served at `/v2/`:
  - **Handshake:** the `native` message with its port, and `ready` heard by the app.
  - **Haptics:** all 7 kinds played through `performHapticFeedback`. A burst of 12 ticks: 11
    dropped by the 25/s cap.
  - **`diag`:** WebView, SDK, model, UNPROCESSED (`false` on the emulator), the microphones, the
    haptic primitives and effects, the gesture insets.
  - **Refusals:** an unknown type, a bad haptic, non-JSON and a negative rect each answered with
    `err`.
  - **Immersive:** `innerHeight` 779 → 851 CSS px.
  - **Exclusion rects:** read back exactly, CSS px → px → CSS px.
  - **Keep-awake:** the app's flag clears and sets. The WebView itself still held
    `keepScreenOn` with no media playing (Chromium's, reported, not fought).
  - **Back:**
    - a page overlay closed first, with no menu;
    - at the top level, the app's menu, which closed by itself;
    - with a page that does not answer, the menu after 0.4 s, and Back again hid K1000: same
      process, no reload on reopening.
  - **Gesture navigation, an exclusion rect at the left edge:**
    - immersive: no Back, inside the rect or outside it (the swipe shows the bars);
    - bars shown: no Back inside the rect, Back outside it.
  - **Page choice:** `set_ui v1` opened v1. The choice survived a restart. The launcher shortcut
    *New page (v2)* went back to v2, and the three dynamic shortcuts exist.
  - **A box answering 404 at `/v2/`:** v1 for the session.
  - **The release APK:** Back over *Not the K1000 box* shows the menu, and Back again hides it.
- The 0.1 checks re-ran on v1 (after `set_ui`) and all pass. Android 13 shows *Viewing full
  screen* once (the driver marks it seen first).
- **Android 17 (WebView 145):**
  - **First run: 57/59.** Every 0.2 check passed, the edge swipes included. Two 0.1 rejoin
    checks failed: *page reaches the box again* (one fetch, 503, just after the Wi-Fi came back)
    and *Wi-Fi on: rejoins by itself* (not within 60 s). The rejoin code is unchanged in 0.2.
  - **Second run: 47/59, on a sick boot.** Android's own Wi-Fi request dialogs failed (*Something
    came up*, *No devices found*), injected BACK keys never reached the app, and the release
    APK's scanner did not open. Earlier Android 17 boots misbehaved the same way (below).
  - Android 17 needed a clean re-run before it counted (done on the rebuild: 64/64). The Pixel
    4a is Android 13.
- **Not tested:**
  - a real phone or the box;
  - the real v2 page (S2), which was not in the tree yet;
  - the bench build against a real bench host;
  - the hold-Back paths: Android 14+ predictive back, and the BACK key on 10–12;
  - the cutout (the AVD has none);
  - whether haptics *feel* right: the emulator only says they were accepted.

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
  smoke build; the parser is unit-tested), touch gestures in the real page (0.2 checks that a
  hold reaches the test page whole, with the app's long-press suppression).

## 9. The bench build (debug only, 0.2)

To drive Thomas's bench box over the LAN (DESIGN §4.2: "Release builds trust only
10.42.0.0/24"), there is a separate build type, `bench`, built as `android/README.md` › Build
shows. It is never distributed and never the gig app:

- Package `gr.trentesept.k1000.bench`, app name K1000, version `0.2.0-bench`. It installs
  **beside** the release app; it is signed with the SDK's debug key, never the release key.
- The WebView is debuggable: `chrome://inspect` on the laptop, with the phone on USB.
- It trusts **one** development host besides the box: `-Pk1000.benchHost` (default
  `192.168.8.54`). The host's CA comes from `-Pk1000.benchCa` (default: the K1000 CA). That CA is
  trusted for that host only, through a network security config generated for the bench variant.
  The bench host is loaded first, then `10.42.0.1`. The fallback mode (*I joined K1000 in
  Android's Wi-Fi settings*) also follows a Wi-Fi on the bench host's /24.
- **The box side is not done, and it is Thomas's call.** Today the phone server binds the AP
  address only (`server.py`, *physical access is the authorisation*). To serve the bench box on
  its LAN address, `k1000.yaml` would need:
  - `phone.bind: 192.168.8.54` and `phone.hosts: [192.168.8.54]`. With those, the page is no
    longer served on the AP while they are set.
  - A certificate naming 192.168.8.54 in a separate `phone.tls_dir`, issued by a **bench CA**
    (pass that CA as `k1000.benchCa`). Do not use the K1000 CA for it: its name constraints
    exclude the LAN. Android would still accept such a leaf (the gap in §3), but Chrome and
    `K1000Trust` refuse it.

  Until then, the bench build works exactly like the release one on the bench box's own AP, and
  it keeps WebView debugging.

## 10. 0.2.1 on the Pixel 4a: sideload and the real-phone checklist (Friday rig test)

**Before the APK reaches the phone: escrow the release key to USB** (k1000#t-008, DESIGN §4.4):
`secrets/k1000-android/k1000-release.jks` and `keystore.properties`. That is the only copy.

**What is on the phone now?** Settings › Apps › See all apps: is **K1000** listed? Its *App info*
page shows the version at the bottom. Or, from a computer:
`adb shell dumpsys package gr.trentesept.k1000 | grep versionName` (nothing printed = not
installed).
- Not installed (0.1.x and 0.2.0 never were): 0.2.1 installs fresh.
- An earlier K1000 installed: 0.2.1 installs over it. Same key, higher versionCode (4); the Wi-Fi
  password and the approval stay.
- *App not installed* / *package conflicts*: another key. Uninstall K1000 first (§5).

**Sideload** (`android/README.md` › Install):
- **From the box** (0.2.1, a box deployed with the APK): the first-time page's *Get the K1000
  app* (§2, steps 1–3). The page's 8 hex must match the start of `K1000-0.2.1.apk.sha256`.
- **USB + adb** (Developer options › USB debugging):
  `adb install -r K1000-0.2.1.apk`.
- **USB file transfer, no adb:** copy the file to `Download/`. Open it in **Files** ›
  Downloads › *Install*. The first time: *Settings* › **Allow from this source** for Files, then
  back, then *Install*. Play Protect: *Install anyway*.
- **Check the file first:** `sha256sum K1000-0.2.1.apk` against `K1000-0.2.1.apk.sha256`. The
  signer's SHA-256 must be `85:ff:f9:65:…:0a:8f` (README › Sign).

**Checklist** (on the box; v2 needs the S1 deploy, and before it the app shows v1 with a toast):
- [ ] 1. First run **from the box** (§2): the camera's Wi-Fi join, the first-time page, *Get the
  K1000 app*, install, open: *K1000 Wi-Fi taken from the box*, *Connect*, and the page opens.
  Note v1 or v2, the seconds from *Open* to the page, and whether Android's dialog listed K1000
  at once while the phone was still on it from the camera's join (the emulator could not: §8). If
  it says *No devices found*: note it for Claude, *Cancel*, *Change K1000 Wi-Fi*, tick *I joined
  K1000 in Android's Wi-Fi settings*, *Join K1000*: the app then follows the network the camera
  joined (the fallback, §2b), and the show goes on. Otherwise force-stop K1000
  (App info › Force stop) and open it again, the K1000 the camera saved still in Android's Wi-Fi
  settings and connected (the state of **every later launch**, §2): the page, no dialog, no scan.
  Note whether Android's dialog showed up there, and the seconds to the page; do it twice (once
  right away, once after Wi-Fi off and on). A *No devices found* at a later launch is the same
  question as above, with the same way out. Diagnostics › `wifi_from` reads `box <build id>,
  band …`.
- [ ] 2. **Diagnostics:** Back › menu › *Diagnostics* › *Copy*, and paste it to Claude. Record:
  - the WebView version;
  - `unprocessed_supported`;
  - the microphones;
  - `haptic_primitives` and `haptic_effects`;
  - `gesture_insets`.
- [ ] 3. *Test haptics*: is each named kind felt and told apart (tick, click, confirm, reject,
  long press, double, triple)? Note any that is weak or missing. If nothing plays, check
  Android's *Touch feedback* setting. Then Touch feedback **off**, *Test haptics* again: only
  confirm, reject, double and triple play. Touch feedback back on.
- [ ] 4. *Edge test* (the bars show for it): 5 swipes in from each side **inside** the amber
  bands (count stays 0), 2 outside (it counts). After *End test* the page is full screen again.
- [ ] 5. v2, immersive: are the bars hidden? Does a swipe from the top or bottom show them for a
  moment? Does a slide on a tile or the footer near a side edge **never** fire Back?
- [ ] 6. Back: does it close the stepper or a sheet first? At the top level, the menu appears,
  and a second Back hides K1000. Reopen K1000: is it the same page, LINK within 2 s, no reload?
- [ ] 7. MUTE hold in the app (v2, then v1): does it act as in `10-tuesday-checklist.md` item
  21?
- [ ] 8. **Hold a band chip** 0.6 s, finger still, in the app (v2, then v1): the fill bar runs,
  then the band goes OUT (v2: toast `P2 OUT`, pill `OUT P2`; v1: the chip's `OUT` tag); hold it
  again → `IN`. A tap only selects. If the fill bar starts and stops short, note whether the
  finger drifted: on v1 the chip keeps vertical panning (`touch-action: pan-y`), and about 8 CSS
  px (≈ 1.3 mm) of vertical drift lets the browser take the gesture and cancel the hold (§8,
  measured on the emulator). If no fill bar ever shows, check that the box runs D-014: it logs
  `phone …: band {"s":…,"on":…}` for every completed hold. Try the same in Chrome on `/`.
- [ ] 9. **Long-press empty graph** 0.55 s, in the app: v1 puts its −6 dB notch at the finger,
  v2 the silent notch (a free bell at Q 8.65, the GAIN steps open). No text selection, no
  context menu, no system buzz.
- [ ] 10. The escape: Back › **hold** *Classic page (v1)* → v1. Long-press the K1000 icon ›
  *New page (v2)* → v2. Do both survive closing the app? Once v1 has been used, does `/v2/`
  still show v2 (and `/` still v1)? If not, it is the box-side service-worker bug
  (`android/BRIDGE.md` › Box-side notes).
- [ ] 11. Leave the phone untouched for 10 min on the page: does the screen stay on?
- [ ] 12. Out of range, then back (item 22), and the power-cut drill (§7).

Anything red here: Sunday runs Chrome on `/v2/` or `/`, then the knobs, then the bypass.
