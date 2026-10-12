# 11 — The phone mic as a measurement input (Phase 4, steps 1–2)

Thomas, 2026-10-12: *"could we leverage the phone mic as measurement input to help see the PA
spectrum and spot the needed EQ corrections?"*, plus a landscape layout for the EQ.

Status: **built on branch `night/phone-rta`, for after the gig. Not tried on a real phone yet.**
Tested in headless Chromium with a fake mic fed from a WAV file (§7). `main` keeps the portrait
lock for Sunday.

New and changed files:
- `box/k1000/web/static/rta.js`: the analysis maths, with no DOM and no audio code (tested with
  `node --test`).
- `box/k1000/web/static/app.js`, `index.html`, `app.css`: the MIC chip row, the drawing behind the
  curve, the landscape layout.
- `box/k1000/web/progspec.py`: the box's program spectrum for the response view, on its own
  engine connection.
- `box/k1000/web/server.py`: the `progspec` phone message and the `ps` frames.
- `manifest.webmanifest`: `orientation: any` (this branch only). `sw.js`: cache `k1000-v2-…`,
  with `rta.js` in the app shell.
- Tests: `box/tests/js/rta.test.js` (18), `box/tests/test_progspec.py` (6),
  `box/tests/test_rta_js.py` (runs the node tests from pytest when node is installed).
- Screenshots: `docs/ui/phone-rta/` (portrait and landscape, each mode).

## 1. What it is

Two views on the LIVE page, behind the live-EQ curve. The curve stays on top and works as before:
drag a dot, long-press empty space for "notch here".

| | **RTA** (step 1) | **RESPONSE** (step 2) |
|---|---|---|
| Shows | the phone mic's spectrum | phone mic minus the box's program, each averaged over 10 or 20 s |
| Good for | a ring or a harsh band standing out, live | how the PA + room (+ the live EQ) colour the music, walking the room |
| Needs the box | no (works without a link) | yes: the box sends its program spectrum |
| Scale | the EQ grid's dB per line (6 dB), shown as band power (pink noise reads flat), placed automatically | the EQ's dB scale, 0 dB = the median of 200 Hz–5 kHz |

**Audio never leaves the phone.** The page sends only its usual small control messages, plus
`progspec on/off`. The e2e test checks every frame the page sends.

## 2. Using it at a venue

1. Open the page over **HTTPS** (`https://10.42.0.1/`, the K1000 icon). Browsers give the mic only
   to secure pages: the plain fallback on `:8080` cannot have it, and the page says so.
2. **LIVE EQ › MIC.** The first time, Chrome asks: **Allow**. Denied or no mic gives a short
   message under the chip row and nothing else changes. After a "Don't allow", Chrome refuses at
   once on every later tap, so allow it again by hand:
   - **from the K1000 icon** (the page opens as an app, with no address bar): Chrome › ⋮ ›
     Settings › Site settings › Microphone › `10.42.0.1` › Allow. The message says so.
   - **in a Chrome tab**: the icon at the left of the address bar › Permissions › Microphone.
3. **Hold the phone like a measurement mic.** Arm's length, away from your body, screen up. The
   Pixel 4a's main mic is the hole on the bottom edge: point that edge at the PA and keep your
   fingers off it. No case flap over it. Keep still while reading the RTA (handling noise is
   loud on a phone).
4. **RTA: spot a ring.** A narrow peak at least 6 dB above its ±1-octave neighbourhood, steady for
   1.5 s, gets a **cyan marker with its frequency**. The trace uses the grid's own dB per line
   (6 dB), so a ring two lines above its surroundings is 12 dB, the depth of a 12 dB cut on the
   curve. **Long-press on the marker** (the cyan pointer or its label, within a fingertip, 24 px):
   the usual "notch here" lands *on the peak*, not under the finger, and the toast says "on the
   peak". The same finger can then slide it. A long-press anywhere else lands under the finger
   as always. The marker never changes anything by itself.
   - **FREEZE**: stops the trace to read it calmly. The marker stays, and a long-press on it
     still notches on the peak.
   - **HOLD**: the dotted line is the peak hold; tap to clear it.
   - **SLOW / FAST**: 1 s or 125 ms power averaging. Slow is steadier and is what the marker uses.
   - **1/12 · 1/6 · 1/3**: smoothing in octaves (the response view uses exactly the same widths).
   - The chips sit on one row, actions first (MIC, RESPONSE, FREEZE, HOLD or RESET AVG). In
     portrait the settings (SLOW, 1/12, 10 s) may sit past the right edge: swipe the row.
5. **RESPONSE: read the room.** Play music through K1000, tap **RESPONSE**, then walk the audience
   area slowly for the length of the window (**10 s / 20 s** chip). Moving averages the room's
   position-dependent dips away (a spatial average).
   - Read **broad bumps that stay** as you walk: PA + room. Those are cut candidates.
   - **Do not boost narrow dips.** They are usually cancellations, and EQ cannot fill them.
   - The response **includes the live EQ** (the mic hears the output, the program is taken before
     the EQ). So the goal is a flat trace. The average **restarts by itself when the live EQ, the
     balance, the Lo-Cut or the profile changes** (the master only shifts the whole trace, which
     the 0 dB offset absorbs). Tap **RESET AVG** when you move to a new spot.
   - **Muting pauses it** ("MUTED: response paused" on the graph): the mic would hear the room,
     not the PA. It carries on after the unmute.
   - **PLAY MUSIC** means the program is too quiet to compare. "Waiting for the box's program…"
     means no frames are arriving (old box software, or no link). These notes are drawn on the
     graph.
   - A gap in the trace means the music has nothing in that band (often the top octave), so there
     is nothing to compare there.
6. **Know what it cannot do.**
   - The mic is **uncalibrated**: its own response is in the trace. Fine for "what sticks out",
     not for absolute tonal balance.
   - It is **weak below ~100 Hz**: that region is shaded and gets no marker. **Sub alignment and
     the low end stay with the measurement mic** (M4 input 1, with REW or Open Sound Meter).

## 3. What Chrome on the Pixel 4a must honour (check on the phone)

- The page asks for `echoCancellation: false, noiseSuppression: false, autoGainControl: false,
  channelCount: 1`. It then reads back `track.getSettings()` and shows **MIC PROCESSING ON** (the
  tooltip names which) if the browser kept any of them on. In the e2e, headless Chromium reported
  all three off.
- Android can still process the signal below the browser, depending on which audio source Chrome
  opens. If the RTA "breathes" with the music (AGC) or the hiss floor is gated (noise
  suppression), that is the sign. The fallback is the planned Android app with the UNPROCESSED
  source (ROADMAP, Phase 2 › Android app).
- **To check on the Pixel 4a:** the permission prompt appears; the mic indicator (green dot) goes
  away on MIC off, on leaving LIVE and on switching apps; MIC PROCESSING ON stays hidden; with pink
  noise from the PA at a steady level, the trace does not drift over 30 s (no AGC); the marker
  finds a sine from a phone tone generator or REW within 1/12 octave; `AnalyserNode` at 16384
  keeps 15 fps (the page drops to 8192 by itself when the steady analysis averages more than
  8 ms a frame).
- **Mic permission in the app:** deny once on purpose, then follow the message's path (Chrome ›
  Settings › Site settings › Microphone › `10.42.0.1` › Allow) and check that MIC works again.
- **Landscape from the K1000 icon:** the icon is a plain home-screen shortcut (no WebAPK: the
  box's Wi-Fi cannot reach Google's minting server, `docs/06-phone.md` §3 step 5). Chrome most
  likely stores the orientation in the shortcut when it is added and never re-reads the
  manifest, so an icon added from `main` (portrait) stays locked in portrait on this branch.
  Test landscape in a Chrome tab first, then **remove the K1000 icon and add it again** from this
  branch. Going back to `main` later: remove and re-add it once more, so it is locked to
  portrait again for shows. (Not verified without the phone.)

## 4. How it works

### 4.1 RTA (client only)

- `AudioContext` at the device rate. `MediaStreamSource → AnalyserNode` (FFT 16384, smoothing 0),
  never connected to the speaker.
- **Power-domain averaging** per FFT bin: FAST τ = 125 ms and SLOW τ = 1 s, running side by side.
  Power, never dB: a ring that comes and goes averages to its mean power, not its mean dB.
- **Fractional-octave smoothing** for display on 1/48-octave points, from prefix sums. Partial bins
  count pro rata. The window is never narrower than one bin, so the low end interpolates instead
  of drawing steps.
- **Band power, as a 1/n-octave RTA shows it.** The smoothing gives the mean power density over a
  window whose width grows with frequency; the display adds 10·log10(f / 1 kHz) to show the
  band's power instead. Pink noise, and most music, then reads flat rather than falling 3 dB per
  octave, so the trace fits the grid's 30 dB. The peak detector works on its own (density)
  smoothing and is not affected.
- **Peak hold**: the maximum per display point.
- **Level tracker**: the plot's top follows the loudest band (40 Hz–16 kHz) plus 5 dB. It rises
  with τ ≈ 0.15 s and falls at 1 dB/s at most. The span is the EQ grid's 30 dB (6 dB a line), fixed,
  so the trace never zooms or jumps, and ring heights read off the same grid as the cuts. What
  falls more than 25 dB under the loudest band clips at the bottom. The tracker, the averages,
  the hold and the marker start afresh when the view changes (RTA ↔ RESPONSE).
- **Peak marker**: works on SLOW at 1/24 octave, between 100 Hz and 16 kHz. A peak qualifies when it
  is a local maximum, at least 6 dB above the median of its ±1 octave, and at least 3 dB above the
  trace ±1/6 octave away on both sides (a broad hump is not a ring). It must then hold within
  1/12 octave for 1.5 s, and it goes away 0.6 s after it was last seen. The frequency comes from a
  parabola through the top three points.
- **Drawing**: at most ~15 fps (requestAnimationFrame). Frames are skipped while a finger drags a
  band (the e2e sees about 40 skipped during one drag). FREEZE stops both the analysis and the redraws.
  Below 100 Hz a dark veil goes over the mic's traces (under the grid and the curve), with a
  dashed edge at 100 Hz.
- **Slow phones**: the first 20 frames of each MIC session are not timed (JIT, first buffers);
  after that, if the RTA work averages more than 8 ms a frame, the analyser drops to FFT 8192
  for the rest of the session.
- The page also remembers its RTA choices locally (view, speed, smoothing, window), next to the tab
  and band it already kept. The mic itself is never remembered as on: each visit starts with it off.

### 4.2 RESPONSE (box + client)

**The box** (`progspec.py`). While at least one phone has RESPONSE open (`progspec {on: true}`),
it polls `GetSpectrum` on its **own engine connection**, so a spectrum request can never queue in
front of a mute:

```json
{"command": "GetSpectrum", "value": {"side": "capture", "channel": N,
  "min_freq": 10, "max_freq": 20000, "n_bins": 133}}
```

- One call per **program channel**: the profile's `input_map` (dev 0, 1; M4 2, 3). Never
  `channel: null`: on the M4 that averages the measurement mic and the spare input into the
  program.
- **At most 2 Hz.** L and R are power-averaged on the box. Each phone gets only the latest frame,
  the same way state frames work.
- When the last listener stops or leaves, polling stops and the connection closes. SETUP › Phone
  OFF stops it too.
- `No audio data available` (a fresh engine: the first call arms the engine's ring buffer) and an
  unreachable engine become frames with a `why`, retried on the next round. Any other refusal is
  logged once.

**The phone** computes its mic spectrum **the way the engine computes the program's**. It
re-implements CamillaDSP v5's `spectrum.rs`:

- a Hann window;
- FFT length = the next power of 2 ≥ rate / 10 Hz (8192 at 48 kHz);
- log bins on the same grid, with edges at the geometric midpoints;
- each bin = the **peak** FFT bin in its band.

It does this 5 times a second from the analyser's time-domain buffer (the RTA's own 16384-point
analysis does not run in this view). Both sides are then averaged in power over the sliding
10/20 s window, recomputed and redrawn only when a mic or program frame comes in. The view draws
mic − program wherever the program is no more than 30 dB under its own 200 Hz–5 kHz median,
shifted so that median is 0 dB. Smoothing (1/6, 1/3) is a moving average in dB whose width is
exactly that fraction of an octave on the 12.04-bins-per-octave grid (end bins weighted pro rata:
[½, 1, ½]/2 and [½, 1, 1, 1, ½]/4). The label says "uncalibrated mic − program": the phone mic's
own response is in this trace too.

**Why the same estimator on both sides.** The engine's value per bin is the peak FFT bin, not the
band's energy. On noise-like music that reads several dB high, and more so in wide (high) bands.
An energy estimate on the mic minus the engine's peaks would show a false treble tilt. The unit
test on pink noise shows it:

- matched estimators: flat within ±0.7 dB from 300 Hz to 10 kHz, and a +6 dB bell reads +5.6 dB;
- mismatched estimators: more than 3 dB of tilt.

Side effect: tonal content stands out more than a broadband difference. The e2e's sine, 12 dB
above its 1/12-octave band, reads +18 dB. That is good for spotting rings; keep it in mind for
broad trends.

**Wire protocol** (in addition to `docs/06-phone.md` §8):

- phone → box: `progspec {on: true|false}`, acked when it carries an `id`;
- box → phone: `ps {grid: [10, 20000, 133], ch: [0, 1], m: [dB × 133] | null, why?}`.

**Swapping in UI v1's `rta.py` later.** The phone server uses only `set_active()`, `close()` and
the `on_frame` callback. A module that owns the box's single spectrum connection can stand in
for `progspec.py` with the same three.

### 4.3 Landscape

- The status bar and the tabs share one row. The footer is one row: meters | HOLD TO MUTE.
- **LIVE** puts the EQ graph on the left (62 % of 851 px on a Pixel 4a in landscape) and the band
  controls in a column on the right (step buttons ≥ 44 px), scrolling only inside that column.
  Every mic chip fits on the row under the graph.
- **MAIN** puts MASTER · BALANCE · LO-CUT side by side, with the steps low, near the thumbs.
- No horizontal scroll at 851 × 393. Portrait is unchanged; the original e2e suite passes 75/75 on
  this branch.
- Only short screens switch (landscape and height ≤ 540 px). Tablets and desktops keep the
  portrait layout.
- The toast moves over the right-hand column, so it never covers the graph under the finger.
- From the K1000 icon, landscape may need the icon removed and added again (§3).
- Portrait: the MIC row replaces the old gesture-hint line (the hint now sits next to MIC while
  the mic is off), so the band controls sit 32 px lower than on `main`, whatever the mic does.

## 5. Limits

- **Uncalibrated.** The phone's own response, its directivity and handling noise are all in it.
- **Crowd and wind noise** reach the mic and not the program. In quiet passages the response
  floor rises: read it on steady, full music.
- **Sample rates.** Both sides are assumed to run near 48 kHz. A 44.1 kHz phone gives 5.4 Hz bins
  against the engine's 5.9 Hz: negligible.
- **What the response includes.** The program is taken at the input, before trim and EQ. The
  response therefore includes the system EQ, crossover, Lo-Cut, live EQ and master. Master moves
  are harmless (the 0 dB offset follows them); live-EQ moves restart the average.
- **No time alignment is done**, and none is needed with 10–20 s power averages. This is not a
  transfer function: there is no phase and no coherence (step 5).
- **Box cost** while a phone has RESPONSE open: 2 GetSpectrum calls (~2 ms each, measured in
  `docs/07-live-ui-v1.md` §5) every 0.5 s. Nothing when nobody listens.
- **Battery**: RTA, one 16384 FFT per frame at 15 fps; RESPONSE, one 8192 FFT 5 times a second;
  FREEZE, nothing. The screen dominates; the wake lock was already on.

## 6. Next steps (not built)

3. **Calibration**, once at the bench: the measurement mic on M4 input 1 and the phone side by
   side on pink noise. The box stores the phone's correction curve, and both views apply it.
4. **Suggest-notch** on the response: cuts only, proposed as a free live PEQ, applied only on a
   tap (the GRAB rule of UI v1).
5. **A time-aligned transfer function** with coherence, the way Open Sound Meter does it (GPL-3,
   compatible).

Also worth trying: a marker on response bumps; the Android app's UNPROCESSED mic source (§3).

## 7. Tests

- **Unit, JS** (`node --test box/tests/js/*.test.js`, 18 tests): ballistics time constants and
  power averaging, smoothing, peak hold, the level tracker's rise and fall, the peak detector
  (narrow vs broad, minimum prominence, frequency range, two rings), the tracker's persistence and
  release, the FFT against a direct DFT, the engine's FFT-length rule, grid and band edges, a
  full-scale sine at 0 dBFS, the sliding window, the response maths, the response smoothing's
  exact 1/6 and 1/3-octave widths, the band-power display (pink reads flat), and the
  matched-estimator test above.
- **Unit, Python** (`box/tests/test_progspec.py`, 6 tests, fake engine): polling only while a phone
  listens; the first-call "No audio data" frame; ≤ 2 Hz; capture side, program channels only,
  never null; never on the control connection; other phones get no frames; off, disconnect and
  phone OFF stop it and close the connection; an engine that is down, a bad channel (logged once),
  no channels and broken replies all stay quiet; frames are strict JSON; the static files are
  served and cached.
- **E2E** (scratchpad harness, full Chromium in new headless mode with
  `--use-fake-ui-for-media-stream --use-fake-device-for-media-stream
  --use-file-for-fake-audio-capture=<48 kHz WAV: pink noise + a 2.5 kHz sine 12 dB above its
  1/12-octave band>`, the real Controller and PhoneServer, and a fake engine computing
  GetSpectrum the engine's way on pink noise): 61 checks. Among them:
  - MIC starts and stops the stream (`readyState` live → ended), including on leaving LIVE and on
    hiding the page;
  - the marker lands at 2505 Hz (1/12 octave allowed) after 1.5 s of persistence;
  - a long-press on the pointer (14 px off, frozen) notches at 2510 Hz on the box; one half an
    octave away, low on the plot, notches under the finger;
  - FREEZE keeps the marker and stops the redraws (15 → 0 draws/s); RESPONSE redraws ~7 times a
    second;
  - the slow-phone fallback: slow first frames in two sessions in a row keep 16384, a phone slow
    on every frame drops to 8192;
  - the chip row stays one line (52 px, 44 px chips, 8 px gaps) and the band controls do not move
    when MIC turns on, in both views;
  - a live-EQ or balance change restarts the response; muting pauses it;
  - the EQ still drags in portrait and landscape;
  - the response is flat within 2 dB from 300 Hz to 8 kHz (1.0–1.9 dB across runs) with the sine
    standing out at +18 dB, and PLAY MUSIC shows;
  - no horizontal scroll at 851 × 393;
  - denied (in a tab, and in the installed app with no address bar) and no-mic paths;
  - no JS errors.
- **Not verified**: a real Pixel 4a (§3), real CamillaDSP `GetSpectrum` through `progspec.py`
  (the reply shape is taken from `spectrum.rs` and the probes in `docs/07-live-ui-v1.md` §5), and
  the walk-the-room procedure itself.
