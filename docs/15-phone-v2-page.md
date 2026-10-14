# 15 — The phone v2 page: `/v2/` (S2)

**Date:** 2026-10-13; the phone mic, MEASURE and RESPONSE 2026-10-14 (`docs/11-phone-mic.md`).
**Status:** built (S2 of k1000#t-010, DESIGN §11 P1), beside v1, never
instead of it: `/` stays v1 until Thomas flips it. **Code:** `box/k1000/web/static/v2/`.
**Contract:** `docs/14-phone-v2-protocol.md` (the box side, S1). **Tests:**
`box/tests/test_web_v2_page.py`, the lab in `box/tools/v2lab/`.

The page is the reviewed prototype (`phone-v2/prototype.html`: its layout, gestures, words and
look) with everything simulated taken out and everything driven by protocol 2. The box is the
truth: every rule it enforces (the mute generation, master and trim rises, the ALIGN lock,
choose-then-set, the notch helper, the caps) is the box's, and the page hard-codes none of the
values the box sends in `hello` (`fields`, `slide`, `setup`, `limits`, `nudge`).

## 1. Files

ES modules, no build step; no inline script or style (the box's CSP is `script-src 'self';
style-src 'self'`); the box stamps its v2 build into every `.js`.

| File | What |
|---|---|
| `index.html` | The shell: status strip, alert line, graph + overlay strip, band row ⇄ stepper, EQ tiles, ALIGN row, footer (MASTER, meters, MUTE), the sheet, the toast, the muted frame |
| `app.css` | The prototype's look, minus the simulator. Fixed rows, the graph flexes; below 560 px tall the rows shorten, never a key under 44 px |
| `sw.js` | The service worker, scope `/v2/` (§6) |
| `js/main.js` | Wiring, render, Back / Esc, the APK's exclusion rects, housekeeping, `window.K1000v2` (read-only views for the lab) |
| `js/link.js` | The socket (`/v2/ws`): reconnect, ping, stalled and dead link, replies by id, `hi`, the box-update reload |
| `js/store.js` | hello / state / meters (their shapes checked on arrival), this phone's UI state and prefs, the local values a gesture shows before the box acks, `linked` / `linkSlow` / `canEdit` |
| `js/fields.js` | Pure: the box's step arithmetic, the speed → step mapping, stepper labels, the PA-up bucket and guard mirrors, the nudge batches |
| `js/nudge.js` | `nudge` (slides and stepper keys), replies (`LOCKED`, `SLOW`, clamps), `unlock` / `lock` |
| `js/tile.js` | Value tiles and their gesture (tap, slide, the 0.8 s unlock hold), the HUD |
| `js/stepper.js` | The stepper (band-row slot, or under a SETUP row), FLAT, LOCK |
| `js/bands.js` | The band row: tap = select, 0.6 s hold = IN / OUT, three taps on the selected chip = FLAT (+ UNDO 5 s) |
| `js/graph.js` | The graph: box spectrum, the mic line, RESPONSE, the PEAK of the source shown, curve, dots; dot drag, long-press `grab`, ▼ PEAK; the PGM ▾ popover (BOX · PHONE MIC · BOTH · OFF, FAST / SLOW, smoothing, FREEZE); the ≋ key |
| `js/rta.js` | The box spectrum subscription and frame decoding; the hooks the mic and RESPONSE hang on (`mic`, `resp`, `syncHooks`); the active PEAK |
| `js/micmath.js` | Pure (node-tested): the mic line's maths, the box's PEAK rule in its own instance, the engine's estimator, the `pgm` codec, the RESPONSE windows (docs/11) |
| `js/mic.js` | The phone mic: getUserMedia (asked from SETUP › THIS PHONE only) into Web Audio; when the tracks run or stop; the line, its PEAK |
| `js/measure.js` | MEASURE (≋): the MEASURE row; RESPONSE: `sub {pgm}`, the paired windows, pauses, restarts, the line and the graph's note |
| `js/mute.js` | MUTE, v1's protocol |
| `js/hold.js` | Every hold key (one rule: 12 px of travel or leaving the key cancels; MUTE takes over from any other hold; a second finger, or the key leaving the page, cancels) |
| `js/sheets.js` | SETUP (from `hello.setup`), THIS PHONE (with Phone mic: ALLOW, then OFF / ON and what the browser kept of the processing), STATUS |
| `js/native.js`, `js/haptics.js` | The APK bridge (android/BRIDGE.md) and the haptics, `navigator.vibrate` in Chrome |
| `js/touch.js`, `js/util.js`, `js/view.js` | Fingers, helpers, the strip / alerts / pills / meters / toasts / layout |

## 2. What the page sends (and nothing else)

| Gesture | Message |
|---|---|
| Slide a tile | `nudge {f, d, fine}`: one per field every `slide.send_ms`; detents, never a value; split at `hello.nudge` caps; the release flushes, then nothing |
| Stepper key (tap, on release) | `nudge {f, d}` with `d` from `fields[k].stepper`, one message |
| FLAT (hold 0.6 s, GAIN's steps) | `band {s, g: 0, on: true}` (its own numeric id) |
| Band chip hold 0.6 s | `band {s, on}` alone, with the IN / OUT the chip showed at touch-down (its own numeric id) |
| Three taps on the already-selected chip (≤ 450 ms apart, ≤ 1 s in all; Thomas, 14/10) | `band {s, g: 0, on: true}`, FLAT's message. Nothing when the band is flat and IN already ("P3 ALREADY FLAT"); a tap on another chip only selects it |
| UNDO (tap, 5 s after a three-tap FLAT) | `band {s, g, on}` with the values FLAT replaced. Offered only when that is no rise: a cut, or a band that was OUT (a boost that was IN reads "· boost gone": the GAIN steps raise it). Gone when the band moves meanwhile, on a link loss or a sheet |
| Dot drag | `band {s, f, g}`, at most one per 50 ms, the last value at release (none after a second finger) |
| Long-press empty graph 550 ms | `grab {hz, s}`: snapped to a peak of the box picture within ±⅓ oct, else the finger |
| ▼ PEAK hold 550 ms, or long-press on the PEAK marker | `grab {s}` (no `hz`: the box's PEAK, exactly B3) |
| The same on the mic's PEAK (`▼ MIC`, the mic line shown) | `grab {hz, s}`, `hz` = the mic PEAK: the same silent helper (the box cannot see the phone's PEAK) |
| (both rows) | The PEAK is the one under the finger at **touch-down**: a mic PEAK that goes during the hold (the music drops, the mic stops in BOTH) still sends its own `hz`, never the box's `grab {}` |
| DELAY / LO-CUT held 0.8 s | `unlock {what: "align"}`; LOCK (the steps' label cell) = `lock` |
| MUTE hold 0.6 s / 1.2 s | `mute {on, id: "mute-<n>"}`, `seq` on an unmute (v1's message; each press its own id, so a late ack of an earlier press that timed out never confirms this one) |
| SETUP choice, then SET | `choose {f, v, seen}` |
| ALL FLAT… / UNDO (hold 1 s), laptop OPEN (hold 1 s) / CLOSE (tap) | `act {a}` |
| LIVE visible with the box spectrum | `sub {rta: true}`; hidden, a sheet up, OFF, PHONE MIC only, FROZEN or RESPONSE: `sub {rta: false}` |
| RESPONSE shown (MEASURE), page visible | `sub {pgm: true}`; otherwise `sub {pgm: false}`; again after `unsub {what: "pgm"}` or a reconnect. Nothing else for the mic: audio never leaves the phone |
| Every 1 s | `ping {ts}` (the box wants one at least every 2 s for the spectrum; one a second lets the page see a stalled link, §3) |
| Each connection | `hi {app, bridge, caps}`: `app` = the bridge's `code` (versionCode, app 0.2.2 on), `null` in Chrome and from an older app; sent again when the APK's handshake comes after the hello (it often does), so the box gets the code (§8, gap 2) |

## 3. The safety rules the page keeps (DESIGN §9)

- **MUTE** is in every view and sheet; hold 0.6 s to mute, 1.2 s to unmute, the intent fixed at
  touch-down, `mute_seq` on an unmute, an unmute hold cancelled when the box's mute changes;
  12 px of travel or leaving the key cancels silently (a swipe never mutes); never rate-limited
  by the page; confirmed only by the box's ack of that press, 1.5 s without one is said with
  the box's own fallback (`state.mute_fallback`, never a hard-coded B4).
- **MUTE is never blocked by another key.** A thumb resting on a band chip, ▼ PEAK, FLAT or a
  1 s SETUP hold does not stop the other thumb from muting: MUTE's press cancels that hold
  silently and starts its own (`bindHold` `preempt`). A second finger stops a slide, a dot drag
  or any other hold (sending stops; what was sent stays), never MUTE's, and never starts a hold
  or a slide itself.
- **A hold never fires after its key left.** A hold key removed or hidden mid-hold (the stepper
  closing, a sheet or tab closing, Back) cancels the hold silently; the stepper's 8 s idle close
  waits while a finger is on its keys and restarts when it lifts.
- **No single tap changes the sound** on LIVE: a tap opens steps or selects; sound changes take a
  step key, a slide past the 10 px lock-in, a drag, or a hold.
- **Nothing after the finger lifts**: no inertia, no auto-repeat, no queue. On a link loss the
  waiting detents are dropped, a hold in progress is cancelled and said (`LINK LOST · nothing
  sent`), and nothing is replayed after the reconnect (a new connection starts locked).
- **Master up** is 0.5 dB per 20 px whatever the sensitivity, never accelerated; the page mirrors
  the box's `pa` bucket (master and the trims share it) and the Controller's 6 dB / 2 s guard,
  drops the excess and says `SLOW DOWN · master up is limited`; the box's guards stay
  authoritative.
- **DELAY and LO-CUT** move only inside the window the box granted this connection; the tiles
  show the box's countdown (`unlock_s` in the acks, `locked` from the box, `err LOCKED`).
- **Choose, then set**, with `seen`; SET on its own line, refused within 400 ms of the choice;
  a rebuild row's SET is a 1 s hold marked "it may click".
- **A stalled link** (open, but nothing heard for 2.5 s: the box sends meters while there is
  sound, a pong a second always) refuses every edit (`nudge`, `band`, `grab`, `choose`, `act`,
  `unlock`) and says `LINK SLOW · nothing sent`; the alert line reads `LINK SLOW · edits paused ·
  MUTE still sends` and the strip `SLOW`. On a Wi-Fi stall TCP would otherwise hold the edits
  and deliver them late, in a burst with the second slide the operator makes when nothing seems
  to happen (gain, frequency and delay have no box-side bucket). A MUTE still goes out (late is
  the safe direction); an unmute does not. A slide or drag in progress stops. It clears with the
  next frame.
- **On link loss** the controls dim at once, the meters go grey, the key reads NO LINK and the
  alert line names the fallback, through every retry (back-off 0.5 s doubling to 3 s, 3 s after a
  4xxx close, dead after 6.5 s without a frame); `LINK LOST` is said once, not at each retry. A
  new connection is a new session: the controls wake only on its first state, never on the old
  one's.
- **What the box sends is checked on arrival**: a hello or state of an odd shape (a list that is
  not one, a missing `short`, `slide` keys) never breaks the page; MUTE renders first and on its
  own.
- **A box update reloads the page** once, and only when no finger has been down for 2 s and no
  mute, hold or slide is pending.
- **The phone never asks for a permission from LIVE.** The mic is asked for only from SETUP ›
  THIS PHONE › Phone mic; LIVE, PGM ▾ and MEASURE open it only once the grant is known and say
  `ALLOW THE MIC: SETUP › THIS PHONE` otherwise. The tracks stop when the mic is not shown, under
  a sheet, when the page is hidden or turned, after 10 s without a link (docs/11 §4).
- **MUTE in the new states:** the PGM ▾ popover covers the graph only; MEASURE replaces the ALIGN
  row only. The fit test checks MUTE uncovered and every target ≥ 44 px in both.
- **The page's own alerts fit the 28 px alert line at 360 px**, the action first (`HOLD = KEEP
  MUTED · K1000 RAMPS IN BY ITSELF`, `PHONE CONTROL OFF · use B4 on the box`, `NO LINK · to mute:
  turn the source down · bypass`); the lab fails if one is cut. The box's own alerts may end in an
  ellipsis there: STATUS lists them whole.

## 4. The APK (android/BRIDGE.md)

The handshake listener is registered at module top level; every `native` gets `ready {v: 1,
caps: ["back"]}` and the newest port wins. Only two senders may hand over a port: the WebView
(`postWebMessage` arrives with origin `""`) and a window of the page's own origin; any other
origin, an opaque `"null"` one above all (a sandboxed frame or popup holding a reference to the
page), is refused and listed in THIS PHONE › Diagnostics (`bridge: handshake refused from …`). When the app has the cap: haptics go through it,
`keep_awake` on after each `ready`, exclusion rects for the band row / stepper, the tiles, the
ALIGN row (or the MEASURE row) and the footer (the sheet and the footer while a sheet is up),
Back closes the stepper, then the popover, then a pending choice, then the sheet, then MEASURE
(`back_handled`), THIS PHONE ›
Fullscreen, THIS PHONE › Diagnostics (`diag`, and the app's own panel), THIS PHONE › OPEN UI v1
(`set_ui`). In Chrome none of it: `navigator.vibrate`, Esc acts as Back, UI v1 is a link to `/`.

## 5. Spectrum

Subscribed only while LIVE shows the box spectrum (page visible, portrait: the landscape notice
covers the graph, no sheet over it, PGM ▾ on BOX, not FROZEN); unsubscribed when hidden or
turned; again after the box's `unsub` or a reconnect.
Frames are drawn as the box's (bars, hold dots, PEAK) only when `st` is `live`; otherwise the
layer is cleared and the PGM tag says `PGM …`, `NO RTA` or `RTA OFF`.

**The phone mic, MEASURE and RESPONSE: `docs/11-phone-mic.md`.** PGM ▾ › BOX · PHONE MIC · BOTH ·
OFF (the tag reads `PGM`, `MIC`, `PGM+MIC`, `RTA OFF`; `MIC ?` while the mic is not allowed);
FAST / SLOW and the smoothing act on the mic line only, and say so. The `▼` tag is the PEAK of
the source shown: the mic's own (`▼ MIC 2.44k`) when its line is on the graph, else the box's.
`≋` opens MEASURE: the ALIGN row becomes `MIC ● | RTA · RESPONSE | FREEZE | AVG 10/20 s | RESET`,
the graph's note says the state and any pause (`RESPONSE · 12 s of 20 · …`, `PAUSED · MUTED`).
The overlay strip reads `PGM ▾ · pills · ▼ PEAK · ≋`: `≋` is fixed at the right edge and `▼ PEAK`
comes and goes to its left, so neither key moves under a finger when the PEAK appears or goes.

## 6. Service worker

`/v2/sw.js`, scope `/v2/`, cache `k1000v2-<build>`. The shell (every file of the page) is cached
at install with `cache: "reload"` requests, network first with a 2.5 s timeout: on the K1000
Wi-Fi the page is always the box's current one; off it, the cached page opens and says NO LINK
with the fallback. `test_web_v2_page.py` checks that the worker's list is exactly the page's
files: **add a new module to `SHELL` in `sw.js`**.

## 7. How to test

From `box/`, the box suite runs everything, the browser parts only when node (and Playwright)
are there:

```sh
python -m pytest -q -p no:cacheprovider -m "not engine and not hw" tests tools/tests
K1000_PLAYWRIGHT=/path/to/node_modules/playwright python -m pytest -q tests/test_web_v2_page.py
```

The lab, by hand (`K1000_PLAYWRIGHT` points at a Playwright install with Chromium; install it
outside the repo, the whole `box/` tree is deployed to the box):

| Command | What |
|---|---|
| `python tools/v2lab/record.py > tools/v2lab/fixtures.json` | Record hello / state / meters from the REAL server (S1's in-memory engine) for the fake box |
| `node tools/v2lab/unit.mjs` | The pure modules under node (31 checks) |
| `node tools/v2lab/micunit.mjs` | The phone-mic maths under node (47 checks, docs/11 §7) |
| `python tools/v2lab/spectrum_ref.py > tools/v2lab/spectrum_ref.json` | Remake micunit's reference spectra: CamillaDSP's `spectrum.rs` re-done in numpy (needs numpy; only when the grid or the engine's estimator changes) |
| `node tools/v2lab/e2e.mjs [fixtures.json] [--only=fit,slide,…] [--shots=dir]` | Headless Chromium against the fake box (`fakebox.mjs`: the box's headers, ETag / 304 and CSP, a protocol-2 socket that answers like the box, its notch helper included, and records what the page sends): fit at 393×851, 740, 600, 544 and 360×640 plus landscape 851×393 in every state (0 overflow, every target ≥ 44 px, no clipped text, the page's own alerts whole), taps, slides (pitch, fine / normal / accelerated, sensitivity, master never accelerated and cut at the guard), chips, MUTE (each press its own ack), SETUP, ALIGN (the idle relock said), the graph and the notch helper's three answers, the spectrum subscription (landscape too), two fingers (MUTE over a chip / ▼ PEAK hold; a second finger stops a slide, a drag, a hold), holds whose key leaves the page, a stalled link, link loss (LINK LOST said once), odd hello / state shapes, the sheets' labels and copy, the box-update reload, the APK bridge (and who may hand over a port), the service worker, no console error; then, in full Chromium with a fake mic playing WAVs made at run time (`wav.mjs`), the phone mic, its PEAK and the notch on it (the PEAK taken at touch-down), MEASURE and RESPONSE against the fake box's `pgm` windows, the clip flag (docs/11 §7), MEASURE and the popover fitted at every size, `≋` steady when the PEAK comes or goes. `K1000_V2_VERBOSE=1` prints each check |
| `node tools/v2lab/real.mjs http://127.0.0.1:<port>/v2/ [pink.wav]` | Against a real server (the pytest starts one in-process, with the production spectrum task on a fake engine that plays `pink.wav` as the program): hello, state, a nudge, a chip hold, the worker, the box spectrum's frames; with the WAV, RESPONSE end to end in full Chromium whose fake mic plays the same WAV (the box's real `pgm` windows kept, the coverage growing, the line flat within ±1.5 dB) |

Slides run on Playwright's clock (paused, moved by the test): headless Chromium delivers touch
moves in uneven bursts, so a real-time slide's speed is not what the test meant. Two fingers go
through `hands(page)` (CDP `Input.dispatchTouchEvent` with several touch points: a `touchEnd`
lists the fingers that lift).

## 8. Not done, and gaps

**Against DESIGN / the prototype:**
- The phone mic is built (docs/11): the line, its PEAK, MEASURE and RESPONSE. Not built: mic
  source (the APK's raw mic), calibration, a box RESPONSE (box mic), MEASURE in landscape;
  volume keys are absent. Clipped mic blocks are flagged (`MIC CLIPPING`; RESPONSE keeps none).
- SETUP › PHONE › Pair another phone and BOX › Shut down (headless, decision 6) are not shown.
- The Knob test row is not shown (`phone: hidden`); BOX shows the knobs' state instead.
- The input clip latch has no CLEAR key (the protocol has no message for it; the box's latch
  lasts 3 s).
- Q / SLOPE steps follow the box's `stepper`. A key that halves or doubles Q reads `×½` / `×2`
  (wider / narrower, softer / steeper on a shelf), one detent `−` / `+`, any other count as it
  is: with today's box (`±1, ±2` detents of ⅙ oct) the keys read `−2 · − · + · +2`, not the
  prototype's ×½ / ×2 (box gap 6 below: one line, and the page follows by itself).
- The alert line is a 28 px full-width bar (DESIGN §5.1): the one target under 44 px; it repeats
  the 44 px STATUS key of the status strip (the fit test names this exception).
- Landscape: the portrait notice (as the prototype); MEASURE in landscape is after the gig (the
  mic stops while the notice covers the graph).
- Pairing SETUP rows is by label: two `num` rows ending `L` / `R` with the same
  `field` sit side by side (their tiles read `TOP L` / `TOP R`, `SUB L` / `SUB R`); a `num` row
  whose label starts with the previous choice row's label (`Low-pass freq` after `Low-pass`) sits
  beside it (`SUB LP`). A new box row simply gets its own line.

**The box (exact; the page's own work changed nothing in `server.py`; closed items say so):**
1. **`Vary: *` and ETag / 304 together keep the v2 worker from installing** (docs/14 §1 says
   `cache.addAll` cors requests carry no Vary). After a navigation to `/v2/` the browser's HTTP
   cache holds that response WITH `Vary: *`; the worker's `addAll("./")` revalidates it
   (`If-None-Match` → `304`, sent without Vary), the stored `Vary: *` survives, and `addAll`
   rejects: the registration stays without a worker. Seen against the real server
   (`test_v2_page_end_to_end_with_the_real_server`) and reproduced in the lab. The page's worker
   now asks with `cache: "reload"`. docs/14 §1 should say so (or the box should not answer a
   conditional request for `/v2/` with `304` when the stored response is a navigation).
2. **`hi.app`: closed (14 Oct, integration).** App 0.2.2 sends `code` (versionCode 5) in
   `native` (BRIDGE.md); its emulator smoke test reads `native.code` 5. The page sends it as
   `hi.app` (`null` before 0.2.2 and in Chrome; the lab checks `hi.app === 5`); STATUS › App
   shows `code N`, and `BOX WANTS code M+` when the box's `app_ok` is false. No `life` message
   was needed: on both WebViews tested, HOME, screen off and Back hide the page
   (`visibilitychange`), the page stops its mic, and Android's mic dot goes. Android's own mic
   dialog hides the page while it is up; a pending request survives that (lab part `micdialog`).
3. **Code vs docs/14 §3.4:** an `rta` frame carries the bins whenever the Controller still holds
   a picture, whatever `st` says (docs: "st not live: n 0, b null"). The page draws only
   `st == "live"`.
4. **No message clears the input-clip latch** (prototype STATUS / INPUT had CLEAR): fine on
   Sunday (3 s latch), noted for the headless page.
5. Not a gap, a page rule worth knowing: the back-off resets on the box's `hello`, not on the
   socket's open, so a box that accepts and closes at once is retried at 0.5, 1, 2, 3 s, not
   every 0.5 s.
6. **Q / SLOPE stepper** (the box side's working tree of 14 Oct already sends `(-6, -1, 1, 6)`:
   the keys then read ×½ / ×2 with no page change): `_V2_STEPPER[".q"]` was `(-2, -1, 1, 2)` (`server.py`, HEAD line 193);
   DESIGN §5.4 and the prototype want `WIDER ×2 · − · + · NARROWER ×2`, which is `(-6, -1, 1, 6)`
   (6 detents of ⅙ oct = ×2, inside the `[-12, 12]` caps). Change that one tuple and the keys
   read ×½ / ×2 with no page change.
8. **RESPONSE and the system EQ: closed (14 Oct, integration).** The v2 state now carries
   `sys_eq` (docs/14 §3.2), an opaque digest of the system EQ set on the laptop's `/tune`: a
   change there restarts RESPONSE (`RESTARTED · the system EQ changed`).
9. **RESPONSE and `prog: null`: closed by the box.** With the PEAK's program gate set off
   (−140 in k1000.yaml), the box still judges the program for RESPONSE at −60 (docs/14 §3.6), so
   `prog` is `null` only from an engine that cannot report the level (`BOX: PROGRAM NOT KNOWN`).
7. **An unmute refused for RATE can go unanswered** (docs/14 §5, rule 5: "a refused unmute is
   answered always"). In `_handle2` only `mute {on: true}` skips the message bucket; an unmute
   that finds it empty is refused through `_err2` (HEAD line 2393), which the error bucket
   throttles, so a phone that also ran out of error tokens gets no reply and says `No answer
   from K1000` after 1.5 s. Fix: answer a refused `mute` there like the Controller's refusal
   just below (never throttled, with `v` = the mute as it is). Rare: it takes 40 messages in a
   burst, then 30 a second, plus a page looping on errors.

## 9. Friday, on the Pixel 4a

- In the APK: the slide pitch (2.2 mm a detent) and the fine / normal feel; the ticks (25 / s at
  most); Back never leaves mid-slide (exclusion rects, immersive); MUTE on a 115×76 key.
- In the APK, THIS PHONE › Diagnostics must read `app 0.2.2`: the bridge's handshake was
  accepted (the page takes origin `""`, what the WebView's `postWebMessage` sends). If it reads
  `browser` with `bridge: handshake refused from <origin>`, that origin is what this WebView
  sends: add it to the check in `native.js` (one line), or use OPEN UI v1 from the app's menu.
- Two thumbs: one resting on a band chip or ▼ PEAK, the other holding MUTE: it mutes.
- In Chrome on `/v2/`: the same page with `navigator.vibrate`; v1's worker no longer answers for
  `/v2/` (S1); the v2 worker installs (STATUS shows the builds; `K1000v2.sw()` in the console).
- The phone mic: the checklist in docs/11 §6 (ALLOW in SETUP, no dialog anywhere else, the green
  dot going away under a sheet and with the screen off, pink noise flat, a 1 kHz tone's PEAK and
  its notch, RESPONSE with music, MUTE with the popover open and in MEASURE).
- The cut line stays: THIS PHONE › OPEN UI v1 (or Chrome on `/`), the knobs, the bypass.
