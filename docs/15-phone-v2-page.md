# 15 — The phone v2 page: `/v2/` (S2)

**Date:** 2026-10-13. **Status:** built (S2 of k1000#t-010, DESIGN §11 P1), beside v1, never
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
| `js/bands.js` | The band row: tap = select, 0.6 s hold = IN / OUT |
| `js/graph.js` | The graph: box spectrum, PEAK, curve, dots; dot drag, long-press `grab`, ▼ PEAK; the PGM ▾ popover |
| `js/rta.js` | The spectrum subscription and frame decoding; the phone-mic hook (`mic`) |
| `js/mute.js` | MUTE, v1's protocol |
| `js/hold.js` | Every hold key (one rule: 12 px of travel or leaving the key cancels; MUTE takes over from any other hold; a second finger, or the key leaving the page, cancels) |
| `js/sheets.js` | SETUP (from `hello.setup`), THIS PHONE, STATUS |
| `js/native.js`, `js/haptics.js` | The APK bridge (android/BRIDGE.md) and the haptics, `navigator.vibrate` in Chrome |
| `js/touch.js`, `js/util.js`, `js/view.js` | Fingers, helpers, the strip / alerts / pills / meters / toasts / layout |

## 2. What the page sends (and nothing else)

| Gesture | Message |
|---|---|
| Slide a tile | `nudge {f, d, fine}`: one per field every `slide.send_ms`; detents, never a value; split at `hello.nudge` caps; the release flushes, then nothing |
| Stepper key (tap, on release) | `nudge {f, d}` with `d` from `fields[k].stepper`, one message |
| FLAT (hold 0.6 s, GAIN's steps) | `band {s, g: 0, on: true}` (its own numeric id) |
| Band chip hold 0.6 s | `band {s, on}` alone, with the IN / OUT the chip showed at touch-down (its own numeric id) |
| Dot drag | `band {s, f, g}`, at most one per 50 ms, the last value at release (none after a second finger) |
| Long-press empty graph 550 ms | `grab {hz, s}`: snapped to a peak of the box picture within ±⅓ oct, else the finger |
| ▼ PEAK hold 550 ms, or long-press on the PEAK marker | `grab {s}` (no `hz`: the box's PEAK, exactly B3) |
| DELAY / LO-CUT held 0.8 s | `unlock {what: "align"}`; LOCK (the steps' label cell) = `lock` |
| MUTE hold 0.6 s / 1.2 s | `mute {on, id: "mute-<n>"}`, `seq` on an unmute (v1's message; each press its own id, so a late ack of an earlier press that timed out never confirms this one) |
| SETUP choice, then SET | `choose {f, v, seen}` |
| ALL FLAT… / UNDO (hold 1 s), laptop OPEN (hold 1 s) / CLOSE (tap) | `act {a}` |
| LIVE visible with the box spectrum | `sub {rta: true}`; hidden, a sheet up, OFF or FROZEN: `sub {rta: false}` |
| Every 1 s | `ping {ts}` (the box wants one at least every 2 s for the spectrum; one a second lets the page see a stalled link, §3) |
| Each connection | `hi {app, bridge, caps}`: `app` = the bridge's `code` (versionCode) once the app sends it, `null` until then (§8, gap 2) |

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
- **The phone never asks for a permission** (no mic for now).
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
ALIGN row and the footer (the sheet and the footer while a sheet is up), Back closes the
stepper, then the popover, then a pending choice, then the sheet (`back_handled`), THIS PHONE ›
Fullscreen, THIS PHONE › Diagnostics (`diag`, and the app's own panel), THIS PHONE › OPEN UI v1
(`set_ui`). In Chrome none of it: `navigator.vibrate`, Esc acts as Back, UI v1 is a link to `/`.

## 5. Spectrum

Subscribed only while LIVE shows the box spectrum (page visible, portrait: the landscape notice
covers the graph, no sheet over it, PGM ▾ on BOX, not FROZEN); unsubscribed when hidden or
turned; again after the box's `unsub` or a reconnect.
Frames are drawn as the box's (bars, hold dots, PEAK) only when `st` is `live`; otherwise the
layer is cleared and the PGM tag says `PGM …`, `NO RTA` or `RTA OFF`. The phone mic is a hook
only (`rta.js` `mic`): DESIGN §7's mic line, MEASURE and RESPONSE come after the gig.

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
| `node tools/v2lab/e2e.mjs [fixtures.json] [--only=fit,slide,…] [--shots=dir]` | Headless Chromium against the fake box (`fakebox.mjs`: the box's headers, ETag / 304 and CSP, a protocol-2 socket that answers like the box, its notch helper included, and records what the page sends): fit at 393×851, 740, 600, 544 and 360×640 plus landscape 851×393 in every state (0 overflow, every target ≥ 44 px, no clipped text, the page's own alerts whole), taps, slides (pitch, fine / normal / accelerated, sensitivity, master never accelerated and cut at the guard), chips, MUTE (each press its own ack), SETUP, ALIGN (the idle relock said), the graph and the notch helper's three answers, the spectrum subscription (landscape too), two fingers (MUTE over a chip / ▼ PEAK hold; a second finger stops a slide, a drag, a hold), holds whose key leaves the page, a stalled link, link loss (LINK LOST said once), odd hello / state shapes, the sheets' labels and copy, the box-update reload, the APK bridge (and who may hand over a port), the service worker, no console error. `K1000_V2_VERBOSE=1` prints each check |
| `node tools/v2lab/real.mjs http://127.0.0.1:<port>/v2/` | Against a real server (the pytest starts one in-process) |

Slides run on Playwright's clock (paused, moved by the test): headless Chromium delivers touch
moves in uneven bursts, so a real-time slide's speed is not what the test meant. Two fingers go
through `hands(page)` (CDP `Input.dispatchTouchEvent` with several touch points: a `touchEnd`
lists the fingers that lift).

## 8. Not done, and gaps

**Against DESIGN / the prototype:**
- The phone mic: no MEASURE (`≋`), no RESPONSE, no mic line; the PGM ▾ popover offers BOX / OFF
  and FREEZE only (FAST / SLOW and smoothing were for the mic line). THIS PHONE › Phone mic,
  mic source, calibration and volume keys are "after the gig" rows or absent.
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
- Landscape: the portrait notice (as the prototype); MEASURE in landscape is after the gig.
- Pairing SETUP rows is by label: two `num` rows ending `L` / `R` with the same
  `field` sit side by side (their tiles read `TOP L` / `TOP R`, `SUB L` / `SUB R`); a `num` row
  whose label starts with the previous choice row's label (`Low-pass freq` after `Low-pass`) sits
  beside it (`SUB LP`). A new box row simply gets its own line.

**The box (exact; nothing was changed in `server.py`):**
1. **`Vary: *` and ETag / 304 together keep the v2 worker from installing** (docs/14 §1 says
   `cache.addAll` cors requests carry no Vary). After a navigation to `/v2/` the browser's HTTP
   cache holds that response WITH `Vary: *`; the worker's `addAll("./")` revalidates it
   (`If-None-Match` → `304`, sent without Vary), the stored `Vary: *` survives, and `addAll`
   rejects: the registration stays without a worker. Seen against the real server
   (`test_v2_page_end_to_end_with_the_real_server`) and reproduced in the lab. The page's worker
   now asks with `cache: "reload"`. docs/14 §1 should say so (or the box should not answer a
   conditional request for `/v2/` with `304` when the stored response is a navigation).
2. **`hi.app` cannot be filled:** the box wants the APK `versionCode` (`hello.min_app`, an int);
   the bridge's `native` carries `app` as the version name (`"0.2.0"`, versionCode 3). Fix on
   the app side: add `code: BuildConfig.VERSION_CODE` to `native` (BRIDGE.md). The page already
   sends `native.code` as `hi.app` when it is an integer, `null` until then, so `min_app` /
   `app_ok` is not checked today.
3. **Code vs docs/14 §3.4:** an `rta` frame carries the bins whenever the Controller still holds
   a picture, whatever `st` says (docs: "st not live: n 0, b null"). The page draws only
   `st == "live"`.
4. **No message clears the input-clip latch** (prototype STATUS / INPUT had CLEAR): fine on
   Sunday (3 s latch), noted for the headless page.
5. Not a gap, a page rule worth knowing: the back-off resets on the box's `hello`, not on the
   socket's open, so a box that accepts and closes at once is retried at 0.5, 1, 2, 3 s, not
   every 0.5 s.
6. **Q / SLOPE stepper:** `_V2_STEPPER[".q"]` is `(-2, -1, 1, 2)` (`server.py`, HEAD line 193);
   DESIGN §5.4 and the prototype want `WIDER ×2 · − · + · NARROWER ×2`, which is `(-6, -1, 1, 6)`
   (6 detents of ⅙ oct = ×2, inside the `[-12, 12]` caps). Change that one tuple and the keys
   read ×½ / ×2 with no page change.
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
- In the APK, THIS PHONE › Diagnostics must read `app 0.2.0`: the bridge's handshake was
  accepted (the page takes origin `""`, what the WebView's `postWebMessage` sends). If it reads
  `browser` with `bridge: handshake refused from <origin>`, that origin is what this WebView
  sends: add it to the check in `native.js` (one line), or use OPEN UI v1 from the app's menu.
- Two thumbs: one resting on a band chip or ▼ PEAK, the other holding MUTE: it mutes.
- In Chrome on `/v2/`: the same page with `navigator.vibrate`; v1's worker no longer answers for
  `/v2/` (S1); the v2 worker installs (STATUS shows the builds; `K1000v2.sw()` in the console).
- The cut line stays: THIS PHONE › OPEN UI v1 (or Chrome on `/`), the knobs, the bypass.
