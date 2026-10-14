# The K1000 app ↔ page bridge (v1, app 0.2; `native.code` and the microphone notes: 0.2.2)

The contract between the Android app (`android/`) and the phone page the box serves (`/v2/`, and
`/` for v1). The page is the same code in Chrome and in the app. In the app it gets more: the
phone's own haptics, immersive fullscreen, Back kept off its slide areas, a Back key that closes
its overlays, and diagnostics. DESIGN §4.1 picked this design: a `WebMessagePort`, scoped to the
box's origin, carrying JSON strings. `addJavascriptInterface` is not used: it is exposed to every
frame and it is synchronous.

Code: `app/src/main/java/gr/trentesept/k1000/Bridge.java` (the contract, validated),
`MainActivity.java` (the transport and the actions), `Haptics.java`, `Diagnostics.java`. Unit
tests: `BridgeTest`, `MiniJsonTest`. Live test: `tools/bridge_test.html`, which is the page side
of this contract and is driven by `tools/smoke_driver.py`.

## Transport

- **Handshake.** After every page load of a box URL (`onPageFinished`), the app makes a new
  `WebMessageChannel` and posts one window `message` event to the page:
  - `event.data` is the JSON string `{"t":"native", ...}` (below);
  - `event.ports[0]` is the page's end of the channel.

  It is posted to the page's own origin, never to `*`: `https://10.42.0.1`,
  `https://k1000.home.arpa`, or the bench host in a bench build. A page from any other origin
  never receives it.
- **The page answers `{"t":"ready"}` on that port.** Until it does, the app posts the handshake
  again after 1.5 s and after 4.5 s, each time on a new channel. So the page must:
  - take the **newest** `native` it receives;
  - close the port it held before;
  - answer `ready` to each `native`.
- **Messages are strings.** Each message is one JSON object, serialised, with its type in `t`.
  A non-string message is dropped.
- **Unknown fields are ignored**, so a newer page can send more.
- **A message is refused whole** if it has:
  - an unknown `t`, a wrong field type or a missing required field;
  - more than 8192 characters, or nesting deeper than 4.

  The page then hears `{"t":"err","m":"<its t, or empty>","why":"..."}`.
- **Every message is handled on the app's UI thread, in order.** Messages from the port of a
  previous page are ignored.
- **Feature detection.**
  - In Chrome, no `native` message ever arrives: the page uses `navigator.vibrate` and the rest.
  - In the app, the user agent also ends with `K1000-Android/<version>`. Use it to hide Install,
    the CA steps and the airplane-mode hint. Use it for nothing else: `caps` is the truth.

Page side, minimal (`tools/bridge_test.html` is a complete one):

```js
let port = null;
window.addEventListener("message", (e) => {
  if (typeof e.data !== "string" || !e.ports || e.ports.length !== 1) return;
  let m; try { m = JSON.parse(e.data); } catch { return; }
  if (!m || m.t !== "native") return;
  if (port) port.close();
  port = e.ports[0];
  native.caps = m.caps;                       // feature-detect each one
  port.onmessage = (ev) => onApp(JSON.parse(ev.data));
  port.postMessage(JSON.stringify({ t: "ready", v: 1, caps: ["back"] }));
});
const native = (msg) => { if (port) port.postMessage(JSON.stringify(msg)); };
```

Register the listener at module top level. Module scripts run before `DOMContentLoaded`, so the
listener is in place long before `onPageFinished`. The re-posts cover a page that registers it
late.

## App → page

| `t` | Fields | When |
|---|---|---|
| `native` | `v` (bridge version, 1) · `app` (the version name: `"0.2.2"`, `"0.2.2-bench"`) · `code` (the versionCode, a JSON integer: `5` for 0.2.2; **absent before 0.2.2**) · `build` (`release`, `bench`, `debug`, `smoke`) · `caps` · `ui` (`"v2"` or `"v1"`: the path this page was loaded at) | The handshake, with the port |
| `back` | `id` (an integer) | Back was pressed, and the page said `ready` with `caps` holding `"back"`. Answer with `back_handled` within **400 ms** |
| `insets` | `top`, `right`, `bottom`, `left` in CSS px | After `ready`, and whenever they change: what the page must keep clear itself. All zero unless the page took the cutout (`fullscreen` with `cutout:true`) |
| `diag` | The diagnostics (below) | The answer to `{t:"diag"}` |
| `err` | `m` (the refused message's `t`, or `""`), `why` | A refused message |

`caps` today: `haptic`, `fullscreen`, `exclusion`, `keep_awake`, `diag`, `back`, `set_ui`,
`insets`.

**`code` is what the box compares.** The page sends it as `hi.app` (`docs/14-phone-v2-protocol.md` §4.10; `null` when it
is missing or not an integer, i.e. in Chrome and with an app before 0.2.2), and the box compares
it with `hello.min_app`. `app` is for people (THIS PHONE, Diagnostics); never parse it. `code`
only goes up: every APK the box may serve has a higher one than the APK before it (0.2.0 = 3,
0.2.1 = 4, 0.2.2 = 5). A bench or smoke build has the same `code` as its release.

## Page → app

| `t` | Fields | What the app does |
|---|---|---|
| `ready` | `v` (the page's bridge version, default 1) · `caps` (list of strings; `"back"` = the page answers `back`) | Marks the page bridged; sends `insets` |
| `haptic` | `kind`: `tick` · `click` · `confirm` · `reject` · `long_press` · `confirm2` · `reject3` | Plays it (below). Fire and forget |
| `fullscreen` | `on` (bool) · `cutout` (bool, optional, default false) | `on`: immersive. The status and navigation bars are hidden, and a swipe from the edge shows them for a moment (`BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE`). `cutout:true`: the app stops padding the display cutout and sends `insets`, so the page keeps its controls out of the hole itself. Android 11+ only: on Android 10 the app keeps the page out of the cutout, and `insets` stay zero. Remembered across app starts. **Default: on, no cutout** |
| `keep_awake` | `on` (bool) | The app's `FLAG_KEEP_SCREEN_ON` set or cleared. **Default on, and reset to on at every page load:** the page sends its choice after each `ready`. `on:false` only drops the app's own hold. The WebView (Chromium) may keep the screen on by itself: on the Android 13 emulator it did, with no media playing (diag `keep_screen_on_views`) |
| `exclusion` | `rects`: up to 16 `[x, y, w, h]` in CSS px, in the viewport (`getBoundingClientRect()` values) | The system Back gesture is kept off these rectangles (`setSystemGestureExclusionRects` on the WebView). Each message **replaces** the set, and `[]` clears it. The app converts CSS px to pixels with the display density (rounded outward, clipped to the WebView) and re-applies the set when the WebView resizes. A new page load clears it |
| `diag` | `show` (bool, optional) | Answers `{t:"diag", ...}`. `show:true` also opens the app's own Diagnostics panel |
| `back_handled` | `id` (the `back` it answers) · `handled` (bool) | `true`: the page closed something (a stepper, sheet, popover, MEASURE), and Back stops there. `false`: the page is at its top level, so the app shows its menu, and a second Back hides K1000 |
| `set_ui` | `ui`: `"v1"` or `"v2"` | Remembers the choice on this phone and loads that page: `https://<box>/` for v1, `https://<box>/v2/` for v2. This is THIS PHONE › UI v1/v2, the field-test escape hatch. **A reload, so send it only when the user changes the choice.** The app guards anyway: when that page is already on screen nothing reloads (and the choice is remembered if it differed), so a `set_ui` re-sent at each `ready` is harmless |

### Haptics

The app uses Android's own haptic feedback, `View.performHapticFeedback`, so the phone's tuned
effects apply.

- If the system plays nothing while Touch feedback is on, the app plays a predefined
  `VibrationEffect` instead, with the touch usage.
- **Android's *Touch feedback* setting** silences the texture kinds (`tick`, `click`,
  `long_press`): off means silent, on purpose.
- **The answers cannot be turned off** (DESIGN §5.5): `confirm`, `confirm2`, `reject` and
  `reject3`. With Touch feedback off they play through the vibrator with the **alarm** usage,
  which that setting does not silence. Android's master *Use vibration & haptics* switch and its
  *Alarm vibration* setting still do. Diagnostics counts them (`haptics_count`).
- The page's own THIS PHONE › Haptics (off, light, full) is the page's business: it simply sends
  less. Keep sending the answers at every level.

| `kind` | Android | Use (DESIGN §5.5) |
|---|---|---|
| `tick` | `CLOCK_TICK`. **At most 25 per second** (40 ms apart): extra ticks are dropped, never queued | A detent sent |
| `click` | `VIRTUAL_KEY` (EFFECT_CLICK) | A landmark or sticky stop |
| `confirm` | `CONFIRM` (API 30+) | The box acked an unmute or a grab |
| `reject` | `REJECT` (API 30+) | Range end, guard, refusal |
| `long_press` | `LONG_PRESS` | A hold arms |
| `confirm2` | `CONFIRM`, then `CONFIRM` 140 ms later | The box acked a mute |
| `reject3` | `REJECT` three times, 180 ms apart | No answer, link lost |

### Back

1. The app's own panels close first: menu, Diagnostics, Wi-Fi setup, About.
2. Then, if the page said `ready` with `caps:["back"]`, the app sends `{t:"back", id}` to the
   page. The page closes its top overlay and answers `{t:"back_handled", id, handled:true}`, or
   answers `handled:false` if nothing was open. With no answer within 400 ms, the app treats the
   page as being at its top level.
3. At the top level, the app shows its menu at the top of the screen, never over the footer.
   The menu reads *K1000 app 0.2.0 · Back again now: hide K1000*. **A second Back within 2 s
   hides K1000** (`moveTaskToBack`). The app never calls `finish()`: the K1000 link and the page
   stay up behind. The menu closes by itself 5 s after it was last touched; a finger on the
   page under it does not keep it open.
4. **A held Back (1 s) hides K1000 at once** from the top level. This works on Android 14+
   (predictive back callback) and Android 10–12 (the BACK key). On Android 13, the Pixel 4a's
   version, the platform does not report the hold, so the double Back is the way there.

Answer `back` quickly. Do not animate before answering. Do not answer twice.

### Exclusion rects: what Android does with them

- Only the **left and right** edges have a Back gesture to exclude. The bottom edge (Home)
  cannot be excluded.
- Android honours at most **200 dp of exclusion per edge**, counted from the bottom up. The
  rest is ignored. **But not while the app is immersive with the bars hidden**: Android lifts
  the limit then (AOSP `DisplayContent`, *sticky immersive*; read in the source, not yet
  measured on the 4a). Send the rects for the rows a slide
  happens on: the band row or stepper, the tiles, the ALIGN row and the footer. Make each rect
  about 40 CSS px wide at each edge, or full width: only the part in the gesture area counts.
- **Seen on the Android 13 emulator** (gesture navigation, `tools/smoke_driver.py`):
  - **Immersive:** a swipe in from a side edge did **not** fire Back, inside the rect or outside
    it. The swipe showed the bars instead; Back then takes a second swipe.
  - **Bars shown:** the exclusion rect kept Back off, and a swipe outside it fired Back.

  So immersive is already the safer mode. The rects matter whenever the bars show, and when the
  page turns fullscreen off.
- **[verify on the 4a]** Diagnostics › **Edge test** shows two amber bands excluded from Back
  and counts the Backs a swipe still fires. It shows the system bars for its 20 s, since in
  immersive no swipe fires Back and the test would prove nothing, then restores the page's
  choice.

### `diag` fields

| Field | What |
|---|---|
| `app`, `build` | App version and build type |
| `webview_package`, `webview_version` | The WebView in use (e.g. `com.google.android.webview`, `141.0.…`) |
| `sdk`, `release`, `security_patch`, `model`, `device` | Android and the phone |
| `unprocessed_supported` | `AudioManager.PROPERTY_SUPPORT_AUDIO_SOURCE_UNPROCESSED`: the raw mic source exists (DESIGN §4.3) |
| `output_sample_rate` | The phone's native output rate |
| `mics` | `AudioManager.getMicrophones()`. Each one has `id`, `desc`, `type`, `address`, `location`, `directionality`, `sensitivity_db`, `max_spl` and `min_spl`; a value is null when unknown |
| `has_vibrator`, `has_amplitude_control`, `system_haptics` | Haptic hardware, and the Touch feedback setting |
| `haptic_primitives` | `click`, `quick_rise`, `slow_rise`, `quick_fall`, `tick`, `thud`, `spin` and `low_tick`, each true or false (API 30+; the last three API 31+) |
| `haptic_effects` | `click`, `double_click`, `heavy_click` and `tick`, each `yes`, `no` or `unknown` (API 30+) |
| `density`, `screen_css`, `viewport_css` | Scale, and sizes in CSS px |
| `gesture_insets`, `mandatory_gesture_insets` | Where a swipe is Back or Home (CSS px, `left top right bottom`) |
| `cutout_safe`, `cutout_rects` | The display cutout (CSS px) |
| `exclusion_rects` | The rects in force, read back from the WebView (CSS px) |
| `page`, `page_choice`, `bridge`, `fullscreen`, `cutout_to_page`, `keep_awake`, `link`, `haptics_count` (`bench_host` in a bench build) | The app's own state |

## The microphone (the phone mic on the v2 page, 0.2.2)

No bridge message: the page uses the WebView's own `getUserMedia`, as in Chrome. The app decides
two things, and the WebView a third.

- **Who gets it.** `onPermissionRequest` grants the microphone (`RESOURCE_AUDIO_CAPTURE`) only
  when it is the only thing asked for and the page is the box's (`https://10.42.0.1`,
  `https://k1000.home.arpa`, port 443; a bench build adds its bench host). The camera, MIDI,
  protected media, the microphone together with the camera, and every other origin are refused
  whole, at once, with no dialog (`ShellRules.grantMic`, `ShellRulesTest`). A sandboxed frame
  (opaque origin) is refused by the WebView itself before the app is asked.
- **Android's dialog comes only when the page asks.** The app never asks for RECORD_AUDIO by
  itself: the first `getUserMedia` from the box's page brings Android's own dialog
  (*Allow K1000 to record audio?* · *While using the app* · *Only this time* · *Don't allow*).
  Ask from a tap (THIS PHONE, DESIGN §7), never at load. *Don't allow* rejects the page's request
  with `NotAllowedError`; a second *Don't allow* is final on Android 11+, and from then on the page
  gets `NotAllowedError` at once, with no dialog: the user must allow it in Android's settings
  (long-press the K1000 icon › App info › Permissions › Microphone). Say so in the page.
  *Only this time* lasts while K1000 is in use: Android takes it back some time after K1000
  leaves the screen, and the page's next ask brings the dialog again.
- **The page is hidden while Android's dialog is up.** The dialog pauses the activity, and the
  WebView hides the page then (`visibilitychange` → `hidden`, then `visible` once the dialog
  closes). The `getUserMedia` promise resolves after the page is visible again. **Do not cancel a
  pending request on `hidden`**; when it resolves while the page is still hidden, stop its tracks.
- **Leaving the screen hides the page.** HOME, the screen turning off, and Back › Back (*hide
  K1000*) all fire `visibilitychange` → `hidden` at once (the WebView follows the activity's
  `onPause`), and `visible` when K1000 is back on screen. **The page stops its tracks on `hidden`**
  (and its AudioContext); that releases the microphone at once, and Android's mic indicator goes
  within about 3 s (its own minimum display time). Start again on `visible` only if the user had
  the mic on. No `{t:"life"}` message is needed: seen on the Android 13 emulator (WebView 109) and
  the Android 17 one (WebView 145), `docs/09-android.md` §8.
- **A page that keeps its track in the background** gets silence, not an error: about 5 s after
  K1000 leaves the screen Android silences the recording (the indicator then goes), the track stays
  `live` and delivers zeros, and the recording comes back when K1000 is on screen again. So an
  average that kept running there would average silence: pause on `hidden` (DESIGN §7, RESPONSE).
- **What Android records.** The WebView opens the microphone at 48 kHz with the `CAMCORDER`
  source when `echoCancellation`, `noiseSuppression` and `autoGainControl` are all false (seen
  in Android's audio service on the Android 13 emulator), not `UNPROCESSED`; the page's track says
  `sampleRate: 48000, channelCount: 1`. A phone's CAMCORDER path may still have its own tuning;
  calibration comes later (DESIGN §7).

## The page choice (v1 / v2)

- The app opens `https://10.42.0.1/v2/` by default. It falls back to `https://k1000.home.arpa/v2/`
  as for v1. A bench build opens its bench host first, then `10.42.0.1`.
- **v1 on this phone**, remembered until changed. There are three ways:
  - the page sends `{t:"set_ui", ui:"v1"}`, from THIS PHONE;
  - Back opens the app's menu: **hold** *Classic page (v1)*;
  - long-press the K1000 icon on the home screen and pick *Classic page (v1)*.

  The same three ways lead back to v2. A choice of the page already on screen reloads nothing.
- **The launcher shortcuts** go through a non-exported alias (`.Shortcut` in the manifest).
  Their actions (`gr.trentesept.k1000.action.UI_V1`, `UI_V2`, `DIAG`) sent straight to
  `MainActivity`, which is exported for the launcher, come from another app, and are ignored.
- **A box without `/v2/`** answers 404, as today's box does before S1 lands. The app then shows
  v1 for the rest of the session, with a toast. The remembered choice stays v2.

## Box-side notes (not the app's code)

- **v1's service worker breaks both pages once `/v2/` exists** (`box/k1000/web/static/sw.js`).
  It has scope `/` and files **every navigation** under the key `/`
  (`const key = req.mode === "navigate" ? "/" : url.pathname`), answering from the cache and
  refreshing that key in the background with `cache.put(key, res.clone())`. On any phone or
  WebView where v1's worker is installed (Chrome on Thomas's Pixel already has it; in the app,
  the 404 fallback or THIS PHONE › v1 installs it):
  - a navigation to `/v2/` gets whatever is cached as `/`: v1's shell the first time (seen on
    the emulator: no bridge `ready` after the shortcut to v2);
  - the background fetch of `/v2/` then stores **v2's HTML as `/`**, so the next v1 load
    (THIS PHONE › UI v1, `set_ui`, the app's menu, Chrome on `/`) gets v2's HTML with v1's
    relative links: broken, until the load after it. The two pages keep swapping that way.

  The fix is box-side: treat a navigation as v1's shell only for `/` and `/index.html`, and leave
  every other navigation to the network, e.g.
  `if (req.mode === "navigate" && url.pathname !== "/" && url.pathname !== "/index.html") return;`
  at the top of its fetch handler. Rename the cache too: v1's build id leaves `sw.js` and
  `/v2/` out (`server.py` `load_static_all`), so the fixed worker installs (its bytes change) but
  keeps the old cache name, and with it a `/` entry that is already wrong.
  The emulator smoke test reports what `/v2/` shows after v1's worker is installed (an INFO line).
- `/v2/ws` and the origin check: the app sends the `Origin` of the page it loaded. A bench box
  reached at its LAN address needs that address in `phone.hosts` (or `extra_origins`), and a
  certificate for it (`docs/09-android.md` §9).
