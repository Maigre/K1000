# 14 — Phone protocol 2: `/v2/ws` (the API contract of the v2 page)

**Date:** 2026-10-13. **Status:** built on the box side (S1 of k1000#t-010, DESIGN §10/§11 P1).
**Code:** `box/k1000/web/server.py` (`PhoneBridge.hello2` / `state_message2` / `apply2`,
`PhoneServer`), `box/k1000/control.py` (`notch_helper`, `set_choice`, `set_mute`,
`spectrum_wanted`), `box/k1000/ui/input_map.py` (`setup_schema`). **Tests:**
`box/tests/test_web_v2.py`.

This is the contract the v2 page (`/v2/`, and the APK that wraps it) codes against. Every rule
marked **box** is enforced by the box whatever the page sends; the page must not rely on its own
copy of it, and must not hard-code a value the box sends in `hello`.

**v1 is untouched.** `/` and `/ws` stay protocol 1, byte for byte (pinned by
`test_web_v2.py::test_v1_hello_state_and_allowlist_are_pinned`). Both sockets share one bridge:
the phone count (`max_clients`, 4), the mute generation, the master-up bucket and the
phone on/off switch.

---

## 1. Endpoints

| URL | What |
|---|---|
| `https://10.42.0.1/v2/` | The v2 page: `box/k1000/web/static/v2/index.html` (`/v2` redirects here, 302) |
| `https://10.42.0.1/v2/<file>` | Every file under `static/v2/` (served from memory, ETag + `Cache-Control: no-cache`) |
| `wss://10.42.0.1/v2/ws` | The v2 socket (protocol 2) |
| `http://10.42.0.1:8080/v2/…` | The same over the plain fallback, when the box serves it (no certificate) |

- **Files.** Extensions served: `.html .js .mjs .css .webmanifest .json .png .svg .txt`; anything
  else under `static/v2/` is not served. Dot-files are never served.
- **Headers.** Every HTML file gets the same CSP as v1:
  `default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self' wss://<host>; worker-src 'self'; manifest-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`.
  So: scripts and styles only as files from the box (ES modules are fine): no inline
  `<script>`, no `<style>` element or `style=` attribute, no `eval`; images may be `data:`.
- **Build stamp.** The box replaces `__K1000_BUILD__` in **every `.js`/`.mjs` file under
  `/v2/`** with the v2 build id: 12 hex digits, a hash of every file under `static/v2/` as
  written. The v1 build (stamped in `/sw.js` and `/app.js`) is a hash of the files outside
  `/v2/` only, so a v2 deploy never makes a v1 page reload itself, and the reverse.
- **Service worker.** Register `/v2/sw.js` with `{scope: "/v2/"}`. The box serves it with
  `Service-Worker-Allowed: /v2/`. Never register a worker at `/`. Cache the shell at install
  (`cache.addAll`): a **navigation** response under `/v2/` (`Sec-Fetch-Mode: navigate`) carries
  `Vary: *`, which `Cache.put` refuses, so do not rely on caching navigation responses.
  **Known issue: v1's worker answers for `/v2/` until `sw.js` skips it (must land before the
  freeze).** v1's `/sw.js` (scope `/`) answers *every* navigation inside its scope with its
  cached `/`, and v1's `index.html` loads `/app.js` by an absolute path, so a complete, working
  **v1** page appears at `/v2/` and v2 never loads. Any browser that ever ran v1 over HTTPS has
  that worker: Thomas's Pixel already does (Chrome, 13 Oct), so "Chrome on `/v2/`" shows v1.
  **The APK is not immune:** v1's `app.js` registers its worker whenever it runs in a secure
  context, so one visit to `/` in the app's WebView (THIS PHONE › UI v1, the cut line's
  fallback) registers it, and from then on the app opens v1 at `/v2/` until its data is
  cleared. The fix is one line in v1's `sw.js` `fetch` handler, its first early return:
  `|| url.pathname.startsWith("/v2/")` (pinned by `test_web_v2.py::test_v1_worker_never_answers_for_v2`,
  an `xfail` until that line lands). It changes the v1 build, so the fix reaches a phone by
  itself: the browser re-fetches `/sw.js` on a navigation, the new worker takes over
  (`skipWaiting`, `clients.claim`), and the v1 page shown at `/v2/` sees a new build and
  reloads once, into v2. The `Vary: *` above keeps the reverse from happening (v2 replacing
  v1's cached `/`).
- **Origin.** The socket is accepted only from the box's own origins (the same list as `/ws`:
  `https://10.42.0.1`, `https://k1000.home.arpa`, the plain port when served, `phone.extra_origins`).
  Anything else: HTTP 403 before the upgrade.

## 2. The connection

```
page                                     box
 │── WebSocket /v2/ws ───────────────────▶│ Origin checked; full (4 phones) → close 4002; phone off → close 4003
 │◀──────────────────────────── hello ────│ always the first frame
 │◀──────────────────────────── state ────│ then every change (latest only)
 │◀──────────────────────────────── m ────│ meters, ~10 Hz while they move (latest only)
 │── hi {app, bridge, caps} ─────────────▶│ optional, once
 │── sub {rta: true} ────────────────────▶│ optional: box spectrum frames
 │◀────────────────────────────── rta ────│ ≤ 10 Hz, subscribers only, dropped first
 │◀──────────────────────────── unsub ────│ 10 s without a message from the page
 │── nudge / band / grab / choose / act / unlock / lock / mute ──▶│
 │◀──────────────────── ack {id, v} | err {id, code, why} ────────│
 │── ping {ts} every 2 s ────────────────▶│◀── pong {ts} ──
```

- **Frames** are JSON text, one object each, with a `t` (type). A phone message is at most
  **1024 bytes** (bigger: the box closes with 1009). Binary frames are refused (`err BAD`).
- **Closes:** `4002` too many phones (`bye` first), `4003` phone control off at the box (`bye`
  first, `phone_on: false`), `1001` box stopping, `1009` message too big. Before a 4002/4003
  close the box sends `{"t":"bye","code":4003,"why":"phone off","phone_on":false}`.
- **Liveness:** the box pings at the websocket level every 5 s (a browser cannot see those). The
  page sends `{"t":"ping","ts":<ms>}` every 2 s and treats 6.5 s without any frame as a dead link
  (v1's `PING_MS` / `DEAD_MS`). `pong` echoes `ts` (a number) or `null`.
- **Reconnection:** back off 0.5 s doubling to 3 s (3 s after a 4xxx close), reset to 0.5 s on
  `online` and when the page becomes visible. **A new connection is a new session:** the ALIGN
  unlock is gone (locked), the spectrum subscription is gone (send `sub` again), the phone
  number (`hello.you`) may differ. Nothing the page sent before the drop is replayed by the box,
  and the page must not replay it either (DESIGN §9.3).

## 3. Box → phone

### 3.1 `hello` (first frame)

```json
{"t": "hello", "v": 2, "server": "k1000 0.4.0", "build": "3f9a0c11e2d4", "phone_on": true,
 "you": 1, "min_app": 0,
 "surface": {"screen": true, "knobs": true},
 "bands": ["live_peq.0", "live_peq.1", "live_peq.2", "live_peq.3", "live_lowshelf", "live_highshelf"],
 "labels": ["PEQ 1", "PEQ 2", "PEQ 3", "PEQ 4", "LO SHELF", "HI SHELF"],
 "short": ["P1", "P2", "P3", "P4", "LS", "HS"],
 "kind": ["bell", "bell", "bell", "bell", "low_shelf", "high_shelf"],
 "order": [4, 0, 1, 2, 3, 5], "bells": [0, 1, 2, 3],
 "fields": { "...": "§3.1.1" },
 "slide": { "...": "§3.1.2" },
 "setup": ["… §3.1.3 …"],
 "acts": ["reset_live", "undo_reset", "laptop_open", "laptop_close"],
 "notch": {"q": 8.651, "near_oct": 0.1667},
 "unlock_s": 30.0, "pick_window_s": 8.0, "undo_reset_s": 300.0, "fs": 48000,
 "rta": {"enabled": true, "src": "PGM", "hz": 10.0, "step_db": 0.5, "idle_s": 10.0},
 "limits": {"max_message": 1024, "rate_per_s": 30.0, "burst": 40, "max_phones": 4,
            "master_up_db_per_s": 6.0, "master_up_burst_db": 3.0,
            "guard_db": 6.0, "guard_window_s": 2.0},
 "nudge": {"master_db": [-12, 2], "balance_db": [-4, 4], "lowcut_hz": [-12, 12], "...": "every path"}}
```

| Key | Meaning |
|---|---|
| `v` | `2`. A page that gets another `v` says *OLD PAGE* and reloads once (as v1 does) |
| `build` | The v2 build id (§1). The page compares it with its own stamp and reloads once, when no finger is down, if they differ (v1's `checkBuild` rule) |
| `phone_on` | SETUP › Phone at the box. In practice always `true`: with phone control off the box answers `bye` + close 4003 instead of a hello, and switching it off closes every phone with 4003. A message that races the switch gets `err OFF` |
| `you` | This connection's phone number, 1..4 (lowest free at connect). `state.mute_src` says `"phone 2"` for a mute made by phone 2 |
| `min_app` | Lowest APK `versionCode` the box wants (k1000.yaml `phone.min_app`, default 0 = any). Compare with the app's own; advisory (§4.10) |
| `surface` | `screen`: the box has its screen (not `--headless`); `knobs`: the box has its knobs (not `--no-hw`). Live fallback text is `state.mute_fallback` |
| `bands`, `labels`, `short` | The 6 live bands, `LIVE_SLOTS` order: a band index `s` (0..5) is this order everywhere in the protocol |
| `kind` | `bell` (Biquad peaking) or `low_shelf` / `high_shelf` (Q = shelf slope) |
| `order` | Draw and chip order, low to high: LS 1 2 3 4 HS (B2's order on the box) |
| `bells` | The bands the notch helper may take (never a shelf) |
| `notch` | The helper's Q (1/6 octave, 8.65) and how near (octaves) a used bell must be to be reselected |
| `unlock_s` | ALIGN unlock idle window (§4.4) |
| `pick_window_s` | SETUP: a chosen value waits this long for its SET (D-013 `PICK_WINDOW_S`; also the stepper's idle close) |
| `undo_reset_s` | How long *Undo all flat* lasts at most |
| `fs` | Engine sample rate: one sample = 1000/fs ms (the DELAY fine step and its "samples" readout) |
| `rta` | `enabled` false = k1000.yaml `spectrum.enabled: false` (no box spectrum at all); `src` the only source on Sunday; `hz` frame rate ceiling; `step_db` the byte step of §3.4; `idle_s` how long a subscription lasts without a message from the page (§4.8) |
| `limits` | The box's limits, for display and for the page's own mirror of the PA-up bucket (master and the trims): §6 |
| `nudge` | The `nudge` allowlist: path → `[most negative, most positive]` detents in one message. Anything else is `err BAD` |

#### 3.1.1 `fields{}`: one entry per editable number

Keyed by the **generic** name: the path with its index removed (`live_peq.2.gain` → `live_peq.gain`,
`out_trim_db.1` → `out_trim_db`). Keys: `master_db`, `balance_db`, `align_delay_ms`, `lowcut_hz`,
`live_peq.freq|gain|q`, `live_lowshelf.freq|gain|q`, `live_highshelf.freq|gain|q`,
`out_trim_db`, `input_trim_db`, `sub_lowpass_hz`.

```json
"live_peq.gain": {"label": "GAIN", "unit": "dB", "min": -20.0, "max": 6.0, "default": 0.0,
                  "log": false, "step": 0.5, "fine": 0.5, "digits": 2, "snap": false,
                  "caps": [-12, 12], "stepper": [-6, -1, 1, 6], "accel": {"up": 1, "down": 1},
                  "lock": null, "sticky": [0.0], "landmarks": [],
                  "paths": ["live_peq.0.gain", "live_peq.1.gain", "live_peq.2.gain", "live_peq.3.gain"]}
```

| Key | Meaning |
|---|---|
| `min`, `max`, `default`, `unit`, `digits` | The box's `Range` (`state.RANGES`; `align_delay_ms` is the signed D-011 delay, −50..+50 ms) |
| `log` | `true`: `step`/`fine` are **octaves** per detent and a detent **multiplies** (`v·2^(step·d)`); `false`: added |
| `step` | One normal detent (`fine: false`) |
| `fine` | One fine detent (`fine: true`); equal to `step` where the box has no finer step |
| `snap` | `true` (Lo-Cut): a move lands on the grid of the step in use; from an off-grid value the first detent goes to the next grid line |
| `caps` | Most negative / most positive detents **in one nudge** (box: `err BAD` beyond) |
| `stepper` | The stepper keys, in **normal detents**, left to right (DESIGN §5.4). GAIN's FLAT key is not a detent: it is `band {s, g: 0, on: true}` (§4.3) |
| `accel` | The largest multiplier a fast slide may use, per direction (`up` = positive detents). 1 = never accelerated |
| `lock` | `"align"`: the field moves only inside this connection's ALIGN unlock (§4.4); `null`: always live |
| `sticky` | Values a slide stops on for one extra detent of travel (0 dB, ALIGNED) |
| `landmarks` | Values a slide clicks on passing (Hz) |
| `paths` | The `nudge` paths this entry describes |

`master_db` adds `"up_px_per_detent": 20` (a master-up slide uses 20 px per detent whatever the
sensitivity, never accelerated) and `"up_limit": {"db_per_s": 6.0, "burst_db": 3.0, "bucket": "pa"}`;
`input_trim_db` and `out_trim_db` carry the same `up_limit`. **`bucket` names one bucket shared by
every field that carries it:** raising a trim raises the PA like master, so a master rise and a
trim rise spend the same tokens (§6). The page mirrors one bucket per `bucket` name, never one
per field.

The table as built (normal step / fine / caps / stepper / accel up,down / lock / sticky):

| Key | Step | Fine | Caps | Stepper | Accel | Lock | Sticky / landmarks |
|---|---|---|---|---|---|---|---|
| `master_db` | 0.5 dB | 0.5 | −12, +2 | −12 −2 −1 +1 +2 | 1, 4 | — | — |
| `balance_db` | 0.5 dB | 0.5 | ±4 | −2 −1 +1 +2 | 1, 1 | — | 0 |
| `align_delay_ms` | 0.1 ms | 1 sample (0.0208) | ±10 | −10 −1 +1 +10 | 1, 1 | align | 0 |
| `lowcut_hz` | 2 Hz (snap) | 1 Hz | ±12 | −5 −1 +1 +5 | 4, 4 | align | — |
| `*.freq` | 1/12 oct | 1/48 | ±24 | −4 −1 +1 +4 | 4, 4 | — | 100, 1000, 10000 Hz |
| `*.gain` | 0.5 dB | 0.5 | ±12 | −6 −1 +1 +6 | 1, 1 | — | 0 |
| `*.q` | 1/6 oct | 1/24 | ±12 | −2 −1 +1 +2 | 1, 1 | — | — |
| `out_trim_db` | 0.5 dB | 0.5 | ±4 | −2 −1 +1 +2 | 1, 1 | — | 0 |
| `input_trim_db` | 0.5 dB | 0.5 | ±4 | −2 −1 +1 +2 | 1, 1 | — | 0 |
| `sub_lowpass_hz` | 1/12 oct | 1/48 | ±24 | −4 −1 +1 +4 | 4, 4 | — | 100 Hz |

Q: a positive detent is a **higher Q = narrower** (a shelf: steeper). The stepper's `WIDER ×2`
is −2.

#### 3.1.2 `slide{}`: the tile gesture's parameters (DESIGN §5.3)

```json
"slide": {"px_per_detent": 14, "hysteresis_px": 4, "lockin_px": 10, "axis_ratio": 2,
          "vertical_px": 14, "tap_px": 10, "tap_ms": 450, "edge_px": 24,
          "speed_window_ms": 100, "fine_below_px_s": 100, "accel_from_px_s": 168,
          "accel_full_px_s": 560, "reverse_fine_ms": 150, "send_ms": 50, "max_per_s": 20,
          "echo_ms": 450, "master_up_px_per_detent": 20, "unlock_hold_ms": 800,
          "stepper_idle_s": 8.0}
```

Speed `v` (px/s over the last `speed_window_ms`) → step per detent: `v < fine_below_px_s` →
the field's `fine`; `v ≤ accel_from_px_s` → one normal detent; above, the multiplier
`m = 1 + floor((cap − 1) · (v − accel_from) / (accel_full − accel_from))`, `cap` from
`v ≥ accel_full_px_s`, where `cap` = `fields[k].accel.up` or `.down` by direction. This is the
knob driver's `accel_multiplier(v / px_per_detent, 12, 40, cap)`. A reversal is fine for
`reverse_fine_ms`. These values are the box's defaults for the page; the box does not enforce
them (it enforces `caps`, the locks and the buckets).

#### 3.1.3 `setup[]`: the SETUP sheet, built from `input_map.SETUP_GROUPS`

```json
"setup": [
 {"group": "TOPS", "items": [
   {"label": "Trim L", "f": "out_trim_db.2", "kind": "num", "field": "out_trim_db", "phone": "edit"},
   {"label": "Trim R", "f": "out_trim_db.3", "kind": "num", "field": "out_trim_db", "phone": "edit"},
   {"label": "Polarity", "f": "top_inverted", "kind": "bool", "choices": [false, true],
    "names": ["normal", "INVERTED"], "rebuild": false, "phone": "edit"},
   {"label": "Lo-Cut", "f": "lowcut_on", "kind": "bool", "choices": [false, true],
    "names": ["off", "ON"], "rebuild": false, "phone": "edit"}]},
 {"group": "SUBS", "items": ["… Trim L/R, Polarity, Mono L+R (rebuild: true), Low-pass, Low-pass freq …"]},
 {"group": "INPUT", "items": [{"label": "Trim", "f": "input_trim_db", "kind": "num", "field": "input_trim_db", "phone": "edit"}]},
 {"group": "LIVE EQ", "items": [
   {"label": "Reset live EQ", "f": "reset_live", "kind": "action", "confirm": true, "act": ["reset_live"], "phone": "edit"},
   {"label": "Undo reset", "f": "undo_reset", "kind": "action", "confirm": false, "act": ["undo_reset"], "phone": "edit"},
   {"label": "Spectrum", "f": "spectrum_on", "kind": "bool", "choices": [false, true], "names": ["off", "ON"], "rebuild": false, "phone": "edit"}]},
 {"group": "PHONE", "items": [
   {"label": "Phone", "f": "PHONE", "kind": "page", "phone": "readonly", "why": "ON · SWITCHED AT THE BOX"},
   {"label": "Laptop access", "f": "laptop_access", "kind": "action", "confirm": true,
    "act": ["laptop_open", "laptop_close"], "phone": "edit"}]},
 {"group": "BOX", "items": [
   {"label": "Profile", "f": "profile", "kind": "choice", "choices": ["dev", "m4"], "names": ["DEV", "M4"], "rebuild": true, "phone": "edit"},
   {"label": "Audition (dev)", "f": "dev_audition", "kind": "choice", "choices": ["sum", "tops", "subs"], "names": ["SUM", "TOPS", "SUBS"], "rebuild": true, "phone": "edit"},
   {"label": "Knob test", "f": "HWTEST", "kind": "page", "phone": "hidden", "why": "ON THE BOX ONLY"}]}]
```

| Row key | Meaning |
|---|---|
| `f` | The state path (`num`, `bool`, `choice`), the action name (`action`), or the box page (`page`) |
| `kind` | `num`: a value tile, `nudge {f}`. `bool` / `choice`: choose-then-set, `choose {f, v, seen}` (§4.6). `action`: `act` (§4.7). `page`: a box page; not reachable from the phone |
| `field` | (`num`) the `fields{}` key |
| `choices`, `names` | (`bool`/`choice`) the values in order and how the box screen names them |
| `rebuild` | (`bool`/`choice`) setting it rebuilds the pipeline (it may click): the SET is a 1 s hold, marked MAY CLICK |
| `confirm` | (`action`) the box asks for a second push: on the phone, a 1 s hold |
| `act` | (`action`) the `act` names that drive it |
| `phone` | `edit`, `readonly` (shown dimmed with `why`: the Phone row `ON · SWITCHED AT THE BOX`; a future number row outside the phone allowlist `SET ON THE BOX`), or `hidden` (`why` `ON THE BOX ONLY`: the Knob test) |

Read-only **now** (a pinned profile, Audition off the dev profile, Spectrum disabled in
k1000.yaml) is live: `state.readonly{}` (§3.2). New SETUP rows on the box appear here with no page
change; the page must ignore a `kind` it does not know.

### 3.2 `state` (every change; latest only)

A slow phone skips states, it never queues them. Every v1 key keeps its v1 meaning; v2 adds the
rest. Numbers are rounded, never NaN or infinite (`null` instead).

```json
{"t": "state",
 "master_db": -14.0, "muted": false, "hold": "mute", "mute_seq": 7, "mute_known": true,
 "balance_db": 3.0, "lowcut_on": true, "lowcut_hz": 112.0,
 "bands": [{"f": 250.0, "g": 0.0, "q": 4.0, "on": true}, {"f": 2440.0, "g": -6.0, "q": 8.651, "on": false},
           "… 6 in all, LIVE_SLOTS order …"],
 "engine": "Running", "linked": true, "device_present": true, "device": "MOTU M4", "profile": "m4",
 "message": "", "drift": [], "ramping": false, "load": 4.2, "phone_on": true, "phones": 2,
 "alerts": [["critical", "KNOBS OFFLINE · USE PHONE MUTE / BYPASS"]],
 "pills": ["LAPTOP OPEN 1:59", "OUT P2", "POL INV SUB"],
 "hint": "P2 OUT", "hint_s": 3, "ready": true, "in_clip": false,
 "mute_src": "", "mute_band": {"kind": "", "text": ""}, "mute_fallback": "use B4 on the box",
 "undo_reset_s": 0, "align_delay_ms": 1.25,
 "setup": {"out_trim_db.2": 0.0, "out_trim_db.3": 0.0, "top_inverted": false, "lowcut_on": true,
           "out_trim_db.0": 0.0, "out_trim_db.1": 0.0, "sub_inverted": true, "mono_subs": true,
           "sub_lowpass_on": false, "sub_lowpass_hz": 120.0, "input_trim_db": 0.0,
           "spectrum_on": true, "profile": "m4", "dev_audition": "sum"},
 "spectrum": {"on": true, "src": "PGM", "state": "live"},
 "laptop": {"state": "open", "s": 7140},
 "readonly": {"profile": "PROFILE PINNED IN k1000.yaml", "dev_audition": "AUDITION: DEV PROFILE ONLY"},
 "box_band": 1, "box_page": "SHOW", "knobs_ok": true}
```

| Key | Meaning |
|---|---|
| `master_db`, `balance_db`, `lowcut_on`, `lowcut_hz` | As v1 |
| `muted`, `hold`, `mute_seq`, `mute_known` | The mute protocol, unchanged: §5 |
| `bands[s]` | `f` Hz, `g` dB **stored**, `q`, `on` (D-014: OUT keeps f/g/q and plays 0 dB) |
| `engine`, `linked`, `device_present`, `device`, `profile`, `message`, `drift`, `ramping`, `load` | As v1 (`engine` `null` = link down) |
| `phone_on`, `phones` | As v1; `phones` counts v1 and v2 connections |
| `alerts` | **Every** alert, most urgent first: `[level, text]`, level `critical` (red) · `warning` (orange) · `info` (`Controller.alerts()`) |
| `pills` | Every pill, the box's order (`Controller.pills()`): `LAPTOP OPEN m:ss`, `LO-CUT OFF`, `OUT P2 LS`, `POL INV SUB+TOP`, `MONO SUB`, `AUDITION TOPS` |
| `hint`, `hint_s` | The box's current hint and its seconds left (whole, rounded up); `""` / 0 = none. Count down locally: the box does not resend when it expires |
| `ready` | The box finished its engine sync: edits reach the engine |
| `in_clip` | An input (ADC) clip, latched 3 s |
| `mute_src` | Who made the current operator mute: `B4`, `phone N`, `console`, `boot` (restored muted at start), `GUI` (adopted from CamillaGUI), `local`; `""` when not muted by the operator |
| `mute_band` | What the box's MUTED band says (`pages._mute_hint`'s rule), as `{kind, text}`: `ramp` / `RAMPS IN BY ITSELF` (K1000 holds Main muted on its way in, the operator did not mute: a hold KEEPS it muted), `alert` / the alert's text, `ready` / `READY` (engine running, in sync), `wait` / `""` (not processing yet), `""` / `""` (not muted) |
| `mute_fallback` | The words for "what else mutes": `use B4 on the box` with healthy knobs, else `turn the source down · bypass`. Never hard-code B4 |
| `undo_reset_s` | Seconds the *Undo all flat* is still available (0 = none). Count down locally |
| `align_delay_ms` | The signed D-011 delay: > 0 subs later, < 0 tops later, 0 ALIGNED |
| `setup` | The current value of every SETUP `num`/`bool`/`choice` row, keyed by its `f`. A `choose` sends the one it showed as `seen` |
| `spectrum` | `on`: SETUP › Spectrum (and not disabled); `src` `PGM`; `state` `off` · `waiting` · `live` · `unavailable` · `disabled`. Indicative: a subscriber gets the exact state in each `rta` frame |
| `laptop` | `state` `off` (no such surface) · `closed` · `opening` · `open` · `closing` · `unavailable`; `s` seconds left while open (count down locally) |
| `readonly` | SETUP fields that cannot change right now → the box's reason, to show dimmed |
| `box_band` | The band the knobs are on (the `E` mark). Not the phone's selection: each phone keeps its own |
| `box_page` | The box screen's page (`SHOW`, `ALIGN`, `SETUP`, `PHONE`, `HWTEST`) |
| `knobs_ok` | `true` knobs answer, `false` KNOBS OFFLINE, `null` no knobs expected |

Countdowns are the values when the state was built; the box resends when something else changes,
so the page counts down from the last one it got.

### 3.3 `m`: meters (latest only, ~10 Hz while they move)

```json
{"t": "m", "l": [-18.5, -18.0, -12.5, -12.0], "c": [false, false, false, false], "i": [-20.5, -21.0]}
```

`l`: output peaks of Sub L, Sub R, Top L, Top R (dBFS, 0.5 dB steps, floor −60, top 0); `c`:
their clip latches; **`i`** (v2 only): the program inputs' capture peaks before any K1000 gain
(profile `input_map` order: L, R), same scale: the SETUP › INPUT meter. An input clip is
`state.in_clip`.

### 3.4 `rta`: the box spectrum (subscribers only)

```json
{"t": "rta", "seq": 412, "src": "PGM", "st": "live", "n": 264, "f0": 10.0, "f1": 20000.0,
 "hi": -24.0, "lo": -90.0, "peak_hz": 2440.3,
 "b": "AAECAwQF…", "h": "AAEBAgIC…"}
```

| Key | Meaning |
|---|---|
| `seq` | Frame counter (gaps = frames this phone did not get) |
| `src` | `PGM` (the program, capture side): the only source on Sunday |
| `st` | `waiting` (analyser warming up / engine not processing), `live`, `unavailable` (the engine refuses: NO SPECTRUM), `off` (not wanted: Spectrum OFF, engine down), `disabled` (k1000.yaml) |
| `n`, `f0`, `f1` | Bin count and the first / last bin's frequency. Bin `i` sits at `f0·(f1/f0)^(i/(n−1))` (log-spaced, about 1/24 octave) |
| `hi`, `lo` | The display range the box screen uses (dBFS): top and bottom of the bars |
| `b` | Base64 of `n` bytes: the level of bin `i` is **`hi − b[i]·0.5` dB** (0 = at `hi`; 255 = 127.5 dB or more below) |
| `h` | The same for the hold dots (the per-bin peak held 3 s), or `null` |
| `peak_hz` | The box's PEAK (`rta.Ballistics`: persistent, never on silence), or `null` |

Without a picture (`st` not `live`) the frame is `{"t":"rta","seq":…,"src":"PGM","st":"waiting","n":0,"b":null,"h":null,"peak_hz":null}`
(`f0 f1 hi lo` null): clear the layer.

- **Rate:** at most one frame per 100 ms (≤ 10 Hz), only to connections that sent
  `sub {rta: true}`. About 0.9 kB per frame with `h`: ≈ 9 kB/s per phone.
- **Latest only, dropped first:** a frame is never queued behind another; when more than 4 kB
  are still on their way to this phone, the frame is dropped (the next one replaces it). "On
  their way" counts every layer a later reply would queue behind: asyncio's buffer, the TLS
  layer's, and the **kernel's send queue** (written, not yet acknowledged by the phone;
  `TIOCOUTQ`). The asyncio buffers alone stay empty until the kernel's queue is full, which on
  a stalled link holds megabytes. A frame is sent only after every pending reply, the state and
  the meters, and only while at most 4 kB are in flight: a mute ack never waits behind more
  than that plus one frame, never behind a pile of spectrum.
- **Ballistics are the box's** (the same picture as the box screen). Smoothing, FAST/SLOW and
  FREEZE in the page act on the phone-mic line only.

### 3.5 Replies

```json
{"t": "ack", "id": 17, "v": -13.5}
{"t": "err", "id": 18, "code": "LOCKED", "why": "LOCKED · HOLD 0.8 s TO EDIT", "v": 112.0}
{"t": "locked", "what": "align", "why": "idle"}
{"t": "unsub", "what": "rta", "why": "idle"}
{"t": "pong", "ts": 1729000000000}
```

- **`ack`** comes for every accepted message **that carried an `id`** (an integer, or a string of
  at most 32 characters; anything else counts as no id). No `id`, no ack. `v` is the result (per
  message, §4).
- **`err`** comes for every refusal, with the message's `id` (or `null`), a machine `code` and a
  human `why` (box words, upper case where the box screen says the same). Some carry `v`: the
  value as it is now (snap the tile back), or the mute as it is (§5). **Errors are throttled per
  phone** (5/s, burst 5): a page looping on errors gets silence. A refused **unmute** is never
  throttled.
- **`locked`** (no `id`): this connection's ALIGN unlock ended by itself (30 s idle).
- **`unsub`** (no `id`): this connection's spectrum subscription lapsed (`rta.idle_s` without
  a message from the page, §4.8). A visible page sends `sub {rta: true}` again.

| `code` | When |
|---|---|
| `BAD` | Not JSON, not an object, a binary frame, a missing or wrong-typed field, a field outside the allowlist, detents out of `caps` or 0 |
| `UNKNOWN` | Unknown `t` (includes v1-only `set` and `notch`) |
| `OFF` | Phone control is off at the box |
| `RATE` | Over this phone's message bucket (§6). A MUTE is never refused for this |
| `SLOW` | Master or a trim rising faster than the box allows (`MASTER: SLOW DOWN`); `v` = the value now |
| `LOCKED` | `align_delay_ms` / `lowcut_hz` without this connection's unlock; `v` = the value now |
| `STALE` | `choose` with a `seen` that is no longer the value (`CHANGED MEANWHILE`, `v` = the value now); an unmute whose `seq` is old (§5) |
| `READONLY` | `choose` on a row that is read-only now (`why` = the box's reason) |
| `NOCHANGE` | `choose` of the value already set; `undo_reset` with nothing to undo |
| `NOFREE` | `grab`: all four bells in use (`NO FREE BAND`) |
| `UNAVAILABLE` | Laptop access on a box without it |
| `HELD` | An unmute while K1000 itself holds the mute on its way in (§5) |
| `REFUSED` | The controller refused it (e.g. an engine-side rule) |
| `INTERNAL` | A box bug (logged); nothing was half-applied by the bridge |

## 4. Phone → box

Every message is an object with `t` and, to get an `ack`, an `id`. Unknown keys are ignored.
Types are strict: a number is a JSON number (not a string, not a boolean), detents are integers.

### 4.1 `nudge`: steps on one number

```json
{"t": "nudge", "f": "live_peq.1.gain", "d": -3, "fine": false, "id": 21}
```

- `f`: a path of `hello.nudge`. `d`: integer detents, non-zero, inside the path's caps.
  `fine: true` uses the field's fine step (anything but `true` = normal).
- **Never an absolute value.** Split a big move into several messages; one per field every
  50 ms at most (`slide.send_ms`).
- `ack.v` = the new value (clamped at the range: an ack whose `v` did not move means the end of
  the range). For `align_delay_ms` / `lowcut_hz` the ack also carries `"unlock_s": 30.0`
  (the window was renewed).
- Box rules: `master_db` up through the shared PA-up bucket, then the controller's guard
  (6 dB per 2 s), never accelerated by the box (`err SLOW` with `v` when nothing could be
  raised; a partial rise acks the value reached). `input_trim_db` and `out_trim_db.N` up through
  **the same bucket** (`TRIM: SLOW DOWN`): a trim rise and a master rise spend the same 6 dB/s. `align_delay_ms` and `lowcut_hz` need the unlock (§4.4). A DELAY move stops
  at 0 (ALIGNED) and goes to the other side on the next detent (D-011).
- An OUT band moves too (it stays OUT, kept for its way back IN).

### 4.2 `band`: absolute values for one live band (a dot drag), and IN/OUT (D-014)

```json
{"t": "band", "s": 1, "f": 2440.0, "g": -6.0, "id": 22}
{"t": "band", "s": 1, "on": false, "id": "bandon1"}
{"t": "band", "s": 1, "g": 0, "on": true, "id": "flat1"}
```

v1's message and rules, unchanged: `s` 0..5; any of `f` (Hz), `q`, `g` (dB), clamped by the box;
**`on` goes alone or with `g` only, never with `f` or `q`** (else `err BAD`, nothing set). The
whole message is one smoothed engine move. `ack.v` = what was set, e.g. `{"f": 2440.0, "g": -6.0}`
or `{"on": false}`.

- **IN / OUT** is sent only when the 0.6 s chip hold completes with the link up, never queued or
  replayed; it is `Controller.set_band_on` (logged with the phone, said on the box screen).
- **FLAT** (GAIN's held key) = `{s, g: 0, on: true}`: 0 dB and back IN, in one move.

### 4.3 `grab`: the notch helper (B3's), silent

```json
{"t": "grab", "hz": 2440.0, "s": 2, "id": 23}
{"t": "grab", "id": 24}
```

→ `Controller.notch_helper(target=hz, select=False, prefer=s)`. **The box's own band selection
does not move.**

- `hz` (optional): the target frequency (the page snaps it to a detected peak first). No `hz`:
  **the box's PEAK**, exactly B3. `s` (optional): the phone's selected band, any of the six
  (0..5); it is preferred only if it is a free bell. A shelf, or a bell in use, is ignored: the
  helper picks as if no `s` came (with LS or HS selected, the long-press still works).
- What the box does (D-009, D-014): a **bell in use** (its effective gain ≠ 0; an OUT band is not
  in use) within 1/6 octave of the target is **selected, nothing written**. Else a **free bell**
  (stored gain exactly 0, IN or OUT; never a shelf): `s` if free, else the free bell nearest the
  target, else the first free one. It gets **Q 8.65 and the target frequency, and comes back IN;
  its gain is never touched** (0 dB = silent). No PEAK and no `hz`: Q only, the frequency stays.
  An OUT bell holding a cut or boost is neither free nor in use: it keeps its settings.
- `ack.v`: `{"s": 2, "f": 2440.0, "q": 8.651, "g": 0.0, "on": true, "new": true, "peak": true}`.
  `new` false = an existing bell was reselected; `peak` false = no `hz` and no PEAK (only Q set).
  Then open the GAIN steps on band `s`: the next move is the cut.
- `err NOFREE` when the four bells are in use; `err BAD` for a bad `hz`, or an `s` that is not
  an integer 0..5.

### 4.4 `unlock` / `lock`: ALIGN (DELAY and LO-CUT), per connection

```json
{"t": "unlock", "what": "align", "id": 25}      → {"t":"ack","id":25,"v":{"align_s":30.0}}
{"t": "lock", "id": 26}                          → {"t":"ack","id":26,"v":{"align_s":0.0}}
```

- Send `unlock` when the 0.8 s hold on DELAY or LO-CUT completes. It unlocks **both** for
  `unlock_s` (30 s); every accepted nudge on either renews it (its ack says `unlock_s`). `lock`
  ends it at once (the LOCK key). 30 s without such a nudge: the box relocks and sends
  `{"t":"locked","what":"align","why":"idle"}`.
- **Box:** a nudge on `align_delay_ms` or `lowcut_hz` outside the window is `err LOCKED`,
  `v` = the value now, nothing moves.
- **Per connection:** another phone's unlock does not unlock this one. **The phone's unlock never
  unlocks the knobs** (ALIGN E3/E4 keep B2's hold), **and the knobs' unlock never unlocks a phone.**
  A reconnect starts locked.

### 4.5 `mute`: unchanged from v1 (§5)

### 4.6 `choose`: SETUP choose-then-set (D-013)

```json
{"t": "choose", "f": "sub_inverted", "v": true, "seen": false, "id": 27}
```

→ `Controller.set_choice(f, v)`, so the box's read-only, pinned, already-set and setup rules
apply unchanged.

- `f`: a `bool`/`choice` row of `hello.setup` (`err BAD` otherwise: numbers are `nudge`, `muted`
  is `mute`). `v`: one of the row's `choices` (a JSON boolean for `bool` rows, the string for
  `choice` rows). **`seen` (required):** the value the row showed when the user chose, from
  `state.setup[f]`.
- Refusals, in this order: `BAD` (types); `STALE` when `seen` is not the value now (`why`
  `CHANGED MEANWHILE`, `v` = the value now: nothing is written); `NOCHANGE` when `v` is the value
  now; `READONLY` with the box's reason (`PROFILE PINNED IN k1000.yaml`, `AUDITION: DEV PROFILE
  ONLY`, `SPECTRUM OFF IN k1000.yaml`); `REFUSED` when `set_choice` refuses otherwise.
- `ack.v` = the value now. A `rebuild` row rebuilds the pipeline with Main muted around it (the
  box's own guard): the page's 1 s hold *is* the confirm.
- The page's own rules (DESIGN §6.3): SET sits apart from the chosen key, a SET within 400 ms of
  the choice is ignored, a choice expires after `pick_window_s`.

### 4.7 `act`: the SETUP actions

```json
{"t": "act", "a": "reset_live", "id": 28}
```

| `a` | Controller | `ack.v` | Errors |
|---|---|---|---|
| `reset_live` | `reset_live()`: every live band flat, default frequencies, IN (smoothed); undo kept | `{"changed": true, "undo_s": 300}` (`changed` false: already flat) | — |
| `undo_reset` | `undo_reset()` | `{"undo_s": 0}` | `NOCHANGE` `NOTHING TO UNDO` |
| `laptop_open` | `setup_actions["laptop_access"]` (= `laptop_toggle`) when closed | `{"state": "opening", "s": 0}` (already open: its state, nothing done) | `UNAVAILABLE`; `REFUSED` when root refuses |
| `laptop_close` | the same toggle when open or opening | `{"state": "closed", "s": 0}` (already closed: nothing done) | `UNAVAILABLE` |

`reset_live` and `laptop_open` are 1 s holds on the page (the box asks two pushes). v1's
`{"t":"reset_live","confirm":true}` is accepted too.

### 4.8 `sub`: the box spectrum

```json
{"t": "sub", "rta": true, "id": 29}   → {"t":"ack","id":29,"v":{"rta":true,"st":"off"}}
```

(`st`: the spectrum state at that instant, often still `off` until the task wakes, ≤ 0.1 s; the
frames say the rest.)

`rta: true` subscribes this connection to `rta` frames (§3.4) and sends the latest frame at once
when there is one; `false` stops them. While at least one phone is subscribed, the box polls the
spectrum even when its screen is not on SHOW, or when it has no screen (`spectrum_wanted()`);
SETUP › Spectrum OFF, `spectrum.enabled: false`, an engine down or a missing device still stop
it (`st` says which). A disconnect unsubscribes.

- **A subscription lapses** after `hello.rta.idle_s` (10 s) without **any** message from the
  page; the 2 s `ping` keeps it. A frozen page (screen off, app in the background) keeps its
  socket, because the browser itself answers the box's websocket pings, but its own pings stop:
  the box then stops sending frames, stops polling the engine for it, and says
  `{"t":"unsub","what":"rta","why":"idle"}`. A page that gets `unsub` while visible subscribes
  again.
- **The page unsubscribes when hidden:** `sub {rta: false}` on `visibilitychange` → hidden,
  `sub {rta: true}` when visible again (and after every reconnect). A hidden page that is still
  running keeps pinging, so only the page can say it is not looking.

### 4.9 `ping`

`{"t":"ping","ts":<number>}` → `{"t":"pong","ts":<the same, or null>}`. Never rate-limited.

### 4.10 `hi`: who is connecting (optional, once)

```json
{"t": "hi", "app": 3, "bridge": 1, "caps": ["haptics", "immersive", "exclusion"], "id": 1}
```

`app`: the APK `versionCode` (integer) or `null` in a browser; `bridge`: the native bridge
version or `null`; `caps`: up to 16 strings of at most 24 characters. Logged once per connection.
`ack.v = {"min_app": 0, "app_ok": true}`: `app_ok` false when `app < min_app` (the page then asks
for the app update; the box still serves it). `err BAD` for wrong types.

### 4.11 What v2 does not have

`set` (absolute values: the phone has no absolute input outside a band's dot) and `notch` (the
audible −6 dB "notch here": replaced by `grab`) are v1 only: `err UNKNOWN` on `/v2/ws`.
Phone control on/off is never reachable from a phone.

## 5. The mute protocol (unchanged from v1)

The page can take v1's mute logic verbatim (`app.js`: `holdUnmutes`, `holdButton($("#mute"), …)`,
`sendMute`, `muteDone`). The wire is the same as `/ws`; the generation is shared by every phone
and B4.

**Box (`server.py` `PhoneBridge._mute`, `Controller.set_mute` / `mute_view`):**

1. Every `state` carries `muted` (the mute the box shows), `hold` (`"unmute"` when a hold would
   unmute, `"mute"` when it would mute **or keep muted**), `mute_seq` (the generation: it moves
   whenever the shown mute or the hold's meaning changes, and on **every** mute request from any
   surface) and `mute_known` (`false`: the engine's mute is unknown, `muted` is the saved one).
2. `{"t":"mute","on":true,"id":"mute"}` (MUTE) is **always applied**, never rate-limited, never
   refused (`ack.v = true`). It records `mute_src` (`phone N`).
3. `{"t":"mute","on":false,"seq":<n>,"id":"mute"}` (UNMUTE) goes through the message bucket and is
   applied only if:
   - `seq` is an integer, else `err` `why` `unmute refused: this page is out of date (reload it)`
     (`code BAD`, `v` = the mute shown);
   - `seq` equals the generation now, else `err` `why` `MUTE CHANGED meanwhile: unmute refused,
     look and hold again` (`code STALE`, `v` = the mute shown): an emergency mute made meanwhile
     (B4, another phone) is never undone by someone who looked at the old state;
   - a hold means unmute now; else, if the box shows MUTED, K1000 itself holds Main muted (a boot
     ramp, a setup guard): `err` `why` `K1000 brings the sound in by itself: nothing to unmute`
     (`code HELD`, `v: true`); if it shows not muted, `ack v: false` and nothing happens.
   - Applied: `ack.v = false`.
4. `on` that is not a boolean: `err` `why` `mute needs on: true/false` (`code BAD`).
5. A refused unmute is answered **always** (never throttled), with `v` = the mute as it is.
6. The box **never** mutes or unmutes because a phone left (audio never depends on the UI).

**Page (v1 `app.js`, to keep as is):**

- `holdUnmutes() = S.hold ? S.hold === "unmute" : S.muted === true`.
- Touch-down fixes the intent: `want = !holdUnmutes()`, `seq = S.mute_seq`. No hold starts
  without a link or while a mute is pending.
- **Hold 600 ms to mute (`MUTE_MS`), 1200 ms to unmute (`UNMUTE_MS`).** An unmute hold is
  cancelled the moment `holdUnmutes()` turns false or `S.mute_seq` changes
  (`The mute changed meanwhile: look, then hold again`). A mute hold always sends a mute.
- Send `{t:"mute", on: want, id:"mute"}` plus `seq` when unmuting. The send failing (no link):
  `Not connected: <fallback>` and the REJECT pattern.
- Pending: the key reads `MUTING…` / `UNMUTING…`; **no `ack`/`err` with `id:"mute"` within
  1500 ms (`ACK_MS`)**: `No answer from K1000: <fallback>`. The link dropping while pending:
  `Link lost: <fallback>`. Confirmed **only by the box's ack**, never by the send.
- Labels: pending → `MUTING…`/`UNMUTING…`; else `holdUnmutes()` → `MUTED`; else `muted` →
  `KEEP MUTED` (amber, K1000 holds it); else `MUTE`.
- v2 replaces v1's hard-coded `use B4 on the box` with `state.mute_fallback`, and adds DESIGN
  §5.2's movement rule (12 px of travel or leaving the key cancels silently). Neither changes the
  wire.

## 6. Rate limits and the guards

| What | Limit | Shared? | On excess |
|---|---|---|---|
| Messages | `phone.rate_per_s` 30/s, burst 40 (`limits.rate_per_s`, `.burst`) | per connection | `err RATE`, dropped (never queued) |
| MUTE (`on: true`) | none | — | — |
| `ping` | none | — | — |
| Errors sent | 5/s, burst 5 | per connection | silence (a refused unmute is always answered) |
| Master up | `phone.master_up_db_per_s` 6 dB/s, burst 3 dB (`limits.master_up_*`) | **all phones, v1 and v2** | `err SLOW` |
| Master up, controller | +6 dB within any 2 s (`controls.master_up_guard_db`; `limits.guard_*`) | phones and knobs | the excess detents refused (`MASTER: SLOW DOWN` on the box) |
| Trims up (`input_trim_db`, `out_trim_db.N`) | **master's bucket**: one 6 dB/s, burst 3 dB for master and the trims together (`up_limit.bucket` `pa`) | all phones, v1 and v2, with master | `err SLOW` (`TRIM: SLOW DOWN`) |
| Detents per nudge | `hello.nudge` caps | — | `err BAD` |
| RTA frames | 10 Hz; dropped beyond 4 kB in flight; the subscription lapses after 10 s without a message | per subscriber | dropped (§3.4), `unsub` (§4.8) |

The page mirrors the PA-up bucket (master and the trims: one bucket) to say `SLOW DOWN` at once
and **drops** the excess detents (it never queues them); the box's bucket and guard stay
authoritative.

## 7. Several phones

- Up to `limits.max_phones` (4) connections, v1 and v2 together; the fifth gets `bye` + 4002.
- **Values are shared, the last writer wins**; every phone gets every state.
- **Per phone:** the band selection (the page's own), the ALIGN unlock, the spectrum
  subscription, the open stepper and sheet.
- The mute generation is shared: phone 1's unmute based on a state older than phone 2's mute is
  refused. A mute made elsewhere shows in `mute_src` (`B4`, `phone 2`…): announce it.
- SETUP › Phone off at the box closes every phone (4003); only the box switches it back on.

## 8. Changes against DESIGN §10, and why

- **The SETUP values are one `state.setup{}` object** keyed by the row's `f` (trims,
  polarities, Mono, the sub low-pass, input trim, Spectrum, profile, audition), not separate
  top-level keys: the sheet is built from `hello.setup`, so a new box row needs no page change.
- **`in_peak_db` is in `m.i`, not in `state`:** it moves at the meter rate; in the state it
  would resend the whole state ~10 times a second.
- **`mute_band` is an object** (`kind`, `text`), and `mute_fallback` is added: the box screen's
  MUTED band says `hold B4…`, which is wrong on a phone.
- **`rta` frames carry `h`** (the hold dots, DESIGN §7 "bars and hold dots"), `hi`/`lo` (the box's
  display range) and `f1`/`n` instead of `bpo` (the engine's 264 bins span 10 Hz–20 kHz at
  slightly more than 1/24 octave each). About 9 kB/s per phone instead of 4.5.
- **Error `code`s** are added to v2 errors (v1 has `why` only).
- **The trims rise through master's bucket** (one 6 dB/s, burst 3 dB, for master and every
  trim, every phone): raising an input or output trim raises the PA like master does, and a
  separate trim bucket let master and the trims rise 9 dB/s together (S1 review).
- **`grab {s}`** (the phone's selected band, 0..5; only a free bell is preferred) is added, and with `select=False` the helper takes
  the free bell **nearest the target** rather than the box's selected one (DESIGN §5.6: "the
  nearest free bell … moves there silently"). B3 (`select=True`) is unchanged.
- **`locked`** (box → phone) is added so the page relocks with the box.
- **`unsub`** (box → phone) and the subscription's 10 s idle lapse are added: a frozen page
  would otherwise keep the box polling and streaming to a pocketed phone (S1 review).
- **`act` results** are objects (`changed`, `undo_s`, `state`, `s`).
- Not built (P2, after the gig): `surface` in k1000.yaml (it is derived from `--headless` /
  `--no-hw`), the operator token, spectrum sources other than PGM, the phone-mic calibration
  store.
