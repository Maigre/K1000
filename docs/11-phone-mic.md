# 11 — The phone mic on the v2 page: the mic line, MEASURE, RESPONSE

**Date:** 2026-10-14. **Status:** built on the v2 page (`/v2/`, and the APK that opens it) for
the Sunday field test (k1000#t-010), from the "after the gig" list: DESIGN §7 step 1 (the mic
line) and RESPONSE. **Not tried on a real phone yet**: tested in Chromium with a fake mic
playing a WAV (§7). The fallbacks stay: the knobs, the v1 page at `/`, the bypass.

**Code:** `box/k1000/web/static/v2/js/micmath.js` (the maths, no DOM), `mic.js` (getUserMedia and
Web Audio), `measure.js` (MEASURE and RESPONSE), plus `graph.js` (the drawing, the PEAK,
PGM ▾), `sheets.js` (SETUP › THIS PHONE › Phone mic). **Contract:** `docs/14-phone-v2-protocol.md`
(`hello.pgm`, `sub {pgm}`, the `pgm` frames). **Tests:** `box/tools/v2lab/micunit.mjs` (with
`spectrum_ref.json`), `e2e.mjs` (`fitm`, `measure`, `mic`, `micpeak`, `micpeakhf`, `resptilt`,
`respclip`, `micdeny`).

This replaces the night branch's version of this doc (`night/phone-rta`, written for the old v1
page). Its maths (`rta.js`, 18 tests) are ported as they were; its UI (a chip row, a third
engine connection `progspec.py`) is not: the v2 page has the PGM ▾ popover, MEASURE, and the
box's own spectrum poller sends the program windows (one poller, one engine connection).

## 1. What it is

| | **The mic line** (LIVE, or MEASURE › RTA) | **RESPONSE** (MEASURE › RESPONSE) |
|---|---|---|
| Shows | This phone's mic, as a cyan line behind the EQ curve, with its own PEAK | Phone mic minus the box's program, each averaged over 10 or 20 s |
| Good for | A ring or a harsh band standing out, live; a notch on it | How the PA, the room and the live EQ colour the music, walking the room |
| Needs | The mic allowed | The mic, and a box with `hello.pgm` (older box: "RESPONSE needs the box update") |
| Scale | The EQ grid's dB (6 dB a line), band power (pink noise reads flat), placed: its loudest band (100 Hz–12 kHz) sits at −3 dB | The EQ grid's dB, 0 dB = the median of 200 Hz–5 kHz |

**Audio never leaves the phone.** The page sends nothing new except `sub {pgm: true|false}`. The
e2e records every frame the page sends and every request it makes, and fails on anything else.

**Advisory only.** Nothing applies EQ by itself. A notch is the box's silent notch helper (B3's),
then the GAIN steps: the cut is still a deliberate move.

## 2. Using it

1. **Allow the mic once: SETUP › THIS PHONE › Phone mic › ALLOW THE MIC.** This is the only place
   the page asks. Chrome (or Android, in the app) shows its dialog: Allow. The row then reads
   `ON` with an OFF / ON choice, and says what the browser kept of the processing the page asked
   off (echo cancellation, noise suppression, auto gain: all three should read OFF).
   - The page never asks from LIVE, from PGM ▾ or from MEASURE: a system dialog there would cover
     MUTE mid-show. Without the grant they say `ALLOW THE MIC: SETUP › THIS PHONE`.
   - After a "Don't allow": the row reads `BLOCKED` and says where to allow it again (Chrome:
     the icon left of the address › Permissions › Microphone; from the home-screen icon: Chrome ›
     Settings › Site settings › Microphone › `10.42.0.1`; in the app: long-press the K1000 icon ›
     App info › Permissions › Microphone). `TRY AGAIN` asks again.
   - In Chrome the grant is remembered by the browser. In the app, the page remembers it for this
     page load: after the app restarts, tap ALLOW once more (no dialog: Android already allows it).
2. **The line: PGM ▾ › PHONE MIC** (or BOTH: the box's bars and the mic line).
   - **FAST / SLOW** (125 ms / 1 s, power averaging) and **1/12 · 1/6 · 1/3 oct** smoothing act
     on the mic line only, and the popover says so: the box's bars keep the box's ballistics.
   - **FREEZE** stops the box picture and the mic line; the PEAK stays and can still be grabbed.
   - Below 100 Hz the line is faint and dashed, with a dashed edge at 100 Hz: a phone mic says
     nothing useful there, and nothing is marked there. **Sub work stays with the measurement mic.**
3. **The mic PEAK.** When the mic line is shown, the `▼` tag is the mic's: `▼ MIC 2.44k`. It is
   the box's PEAK rule in its own instance: a 1/24 oct bin 10 dB over the median of the ±1 octave
   around it (0.25 s power average), within 30 dB of the loudest, steady 1.5 s; kept while 6 dB
   over, gone 0.5 s after; it moves by 2 bins or more only; searched 100 Hz–12 kHz; no new PEAK
   while the program is absent or K1000 is muted. Never the loudest bin of the moment.
   - It runs on **the box's own numbers**: the engine's estimator (the loudest FFT bin of each
     1/24 oct band, as CamillaDSP gives the box), computed on the phone. So "10 dB over" means
     the same thing on the phone as on the box. (The first build fed it the analyser's mean
     power per band: a ring at 4–11 kHz had to be 5–8 dB stronger before `▼ MIC` showed it.)
   - **Hold `▼ MIC` (0.55 s), or long-press the marker** (±24 px): the same gestures and the same
     `grab` as the box's PEAK. The page sends `grab {hz: <the mic PEAK>}`: a free bell goes there
     at Q 8.65, **gain untouched (silent)**, and the GAIN steps open. The toast says `MIC PEAK · …`.
   - **The PEAK is the one under the finger when it touched down.** If the music drops during the
     hold and the mic PEAK goes, or the mic stops in BOTH and the tag turns into the box's
     `▼ PEAK`, the hold still sends the mic PEAK it began on. It never falls back to the box's
     PEAK (`grab {}`), which may be a different frequency and is not even shown in PHONE MIC.
   - A long-press elsewhere on the graph snaps to the nearest peak of the picture shown (the
     mic's when its line is shown) within ±⅓ oct, as before.
4. **MEASURE: `≋`** (the right end of the graph's top strip; `▼ PEAK` comes and goes to its left,
   so `≋` never moves). The ALIGN row becomes `MIC ● | RTA · RESPONSE | FREEZE | AVG 10/20 s |
   RESET`. The graph, the band row, the EQ tiles and the footer (MASTER, meters, MUTE) stay.
   `≋ ✕` or Back (after the steps, the popover and a sheet) leaves it.
   - `MIC ●`: green = listening, amber = on but not running yet, grey = OFF, red = not allowed
     (a tap then says `ALLOW THE MIC: SETUP › THIS PHONE`; it never asks). A tap on it turns the
     mic OFF / ON, as the SETUP row does.
   - Steps open on BAL, DELAY or LO-CUT close when `≋` hides their tile.
   - After a link drop the average is kept (paused `NO LINK`); the new connection subscribes
     again, and a state that changed meanwhile restarts it as any change does.
5. **RESPONSE: walk the room.** Play music through K1000, `≋`, RESPONSE, then walk the audience
   area slowly for the length of AVG (20 s by default). The graph says how much is in:
   `RESPONSE · 12 s of 20 · uncalibrated mic − program`.
   - Read **broad bumps that stay** as you walk: PA + room. Those are cut candidates. **Do not
     boost narrow dips**: they are usually cancellations.
   - It **includes the live EQ** (the mic hears the output; the program is taken at the input):
     the goal is a flat line. It **restarts by itself** on any change to what the PA plays: a
     band's f / g / q / IN-OUT, BAL, Lo-Cut, DELAY, polarity, mono subs, the sub low-pass, the
     profile, the trims, the audition, the system EQ set on the laptop's `/tune`
     (`RESTARTED · the live EQ changed`, `… the system EQ changed`); the first 0.4 s after
     a restart are not used (the change has to reach the mic first). RESET empties it by hand
     (a new spot).
   - **Ride the master freely.** It does not restart RESPONSE: the box takes the program before
     the master and the mic hears it after, so the page divides each mic reading by the master's
     gain. The 0.8 s after a master move or an unmute are not used (the engine's volume ramp).
   - **It pauses, and says why**, keeping what it has: `PAUSED · MUTED`, `NO PROGRAM` (the box's
     program gate: play music), `MIC OFF`, `MIC CLIPPING` (too loud for the phone: step back
     from the speakers), `BOX RAMPING IN`, `NO LINK`, `BOX: NO SPECTRUM` (SETUP › LIVE EQ ›
     Spectrum OFF, the engine down…). A pause holds the average; windows older than a minute
     are dropped (a long pause is a new measurement).
   - It is drawn from 100 Hz to 12 kHz only: below, a phone mic says nothing useful; above, it
     rolls off by itself (a false treble drop). A gap in the line = the music has nothing there
     to compare.
   - **Read high-frequency differences under about 1 dB as noise.** In a reverberant room the
     engine's peak-per-band reading makes the highs read 0.5–1 dB high against the mids (a
     simulation: direct sound 10 dB under the reverb, RT60 0.8–1.5 s; near the PA, 0.3 dB or
     less). The box sends only the peak per band, so the page cannot correct it.

## 3. How RESPONSE works

**The box** (docs/14, the box side): while at least one phone holds `sub {pgm: true}`, the
spectrum poller (`rta.py`, its one engine connection) keeps the raw combined program spectrum of
each poll (before the screen's ballistics, the engine's own estimator, dBFS), averages it in power
over 0.5 s windows, and sends each window: `{t: "pgm", seq, st, n, f0, f1, k, dt, prog, m}` with
`m` = base64 of n little-endian int16 = round(dBFS × 100); `prog` = the program gate over the
window. Latest only, dropped under backpressure like `rta` frames (a dropped window is lost).

**The page** computes its mic spectrum **the way the engine computes the program's**
(`micmath.EngineEstimator`, CamillaDSP v5 `spectrum.rs` re-implemented): a Hann window, FFT length
= the next power of 2 ≥ rate / `f0` (8192 at 48 kHz), log bins on `hello.pgm`'s grid (n, f0,
f1: 264 bins, 10 Hz–20 kHz, 1/24 oct) with edges at the geometric midpoints, **each bin = the
peak FFT bin in its band**. The AudioContext runs at `hello.pgm.rate` (the engine's), so the bins
match exactly. About 8 times a second it takes the analyser's last samples, divides the bands
by the master's gain (the program is taken before the master), sums them in power, and when the
box's window arrives it pairs the two halves: kept only if nothing paused it during the window
(muted, no program, the mic off or clipping, the box ramping in), and not in the 0.4 s after a
restart or the 0.8 s after a master move or an unmute (what the mic hears then is still the old
sound: the engine, the PA, the air, and the analyser's 171 ms of history). RESPONSE = mic − program per bin over the newest windows that cover 10 or 20 s,
shifted so the median of 200 Hz–5 kHz is 0 dB, only where the program is within 30 dB of its own
median there, nothing below 100 Hz or above 12 kHz; then smoothed in dB at PGM ▾'s width (1/6 oct
by default).

**Why the same estimator on both sides.** The engine's value per bin is the peak FFT bin, not
the band's energy. On noise-like music that reads several dB high, more in wide (high) bands. An
energy estimate on the mic against the engine's peaks would show a false treble tilt (more than
2 dB from 300 Hz to 10 kHz on the box's 1/24 oct grid, more than 3 dB on 1/12 oct: unit test).
With the same estimator the bias cancels: flat within ±1.5 dB on independent noise (unit test),
within ±1 dB in the e2e.

**No time alignment** is done, and none is needed with 10–20 s power averages. This is not a
transfer function: no phase, no coherence (CamillaDSP gives spectra, not samples).

## 4. The rules the page keeps (DESIGN §7, §9)

- The mic is asked for **only from SETUP › THIS PHONE**. LIVE, PGM ▾ and MEASURE open it only when
  the grant is already known (the Permissions API says `granted`, or this page load got it from
  SETUP), so no dialog can come up over MUTE.
- **The tracks are stopped** (`track.stop()`, the AudioContext closed) whenever the mic is not a
  shown source (PGM ▾ on BOX or OFF, outside MEASURE), when a sheet covers LIVE, when the page
  is hidden, in landscape (the notice over the graph), and when the link has been lost for 10 s.
  They come back without any prompt only while the grant holds and the source is still chosen.
  A failure (another app took the mic) is not retried in a loop: the next user action or the
  page coming back to the front tries again.
- `sub {pgm: true}` only while RESPONSE is shown on a visible page (no sheet, portrait), `false`
  otherwise; again after the box's `unsub {what: "pgm"}` (the same 10 s idle lapse as `rta`) or a
  reconnect. RESPONSE also unsubscribes the box's bars (one picture at a time).
- MUTE stays in the footer in every new state: MEASURE, the popover open (it covers the graph
  only). The fit test checks it is uncovered and ≥ 44 px at every size.
- The analysis pauses while a finger drags a dot (the curve first), and while FROZEN.

## 5. Limits

- **Uncalibrated.** The phone's own response, its directivity, the case and handling noise are
  all in it. Fine for "what sticks out", not for absolute tonal balance. Calibration against the
  measurement mic (DESIGN §7) is later.
- **What the phone gives** (DESIGN §4.3): Chrome opens Android's GENERIC source even with the
  three processings off; the app's WebView opens CAMCORDER at 48 kHz (seen on the emulators,
  `android/BRIDGE.md` › The microphone), not UNPROCESSED. Vendor tuning is invisible to the page,
  and may add a high-pass. The SETUP row shows what the browser *says* it kept. The raw mic (the
  app's UNPROCESSED source) is later.
- **In the app, Android's dialog hides the page** while it is up (`android/BRIDGE.md`): the page
  never cancels the pending request then; the answer comes when it is visible again, and the
  tracks stop at once because SETUP still covers LIVE.
- **Crowd and wind** reach the mic, not the program: read RESPONSE on steady, full music.
- **The phone may clip near the PA.** A block whose samples reach 98 % of full scale is flagged
  (harmonics would show as a false rise in the highs): the graph says `MIC CLIPPING · too loud
  for the phone: step back from the speakers` for 1.5 s after the last one, and RESPONSE keeps
  no window meanwhile.
- **Peak-per-band in a reverberant room** reads the highs 0.5–1 dB high in RESPONSE (§2.5): read
  differences under about 1 dB above 5 kHz as noise.
- **CPU:** the line is one 16384-point analyser read, a smoothing and one 8192-point FFT (the
  PEAK, on the engine's estimator) 15 times a second; in RESPONSE the line and its PEAK stop and
  one 8192-point FFT runs about 8 times a second. Nothing while the mic is not shown. Measured
  in headless Chromium on a 4× CPU throttle: 9 ms (median) of mic work per frame for the line
  and its PEAK (7 ms before the PEAK's FFT), 1.8 ms per RESPONSE tick, no long task, and MUTE
  sent 21 ms after its 600 ms hold with the mic line on, as without it. Watch it on the Pixel 4a.

## 6. On the Pixel 4a (Thursday / Friday)

A checklist, in the app and in Chrome on `https://10.42.0.1/v2/`:

1. SETUP › THIS PHONE › Phone mic: ALLOW → the dialog → Allow. The row reads `ON`, and the
   processing line reads `echo cancel OFF · noise suppr. OFF · auto gain OFF`. If it says the
   browser KEPT one ON: note which (the line will breathe or gate).
2. Close SETUP, PGM ▾ › PHONE MIC: the cyan line comes up **with no dialog**. The mic indicator
   (green dot) shows.
3. The green dot goes away: on SETUP, on STATUS, on PGM ▾ › BOX, on switching apps, on the
   screen off. It comes back with no dialog when LIVE is in front again.
4. Pink noise from one top at a steady level: the line sits flat (within a few dB) from 200 Hz to
   8 kHz on SLOW · 1/3 oct, and does not drift over 30 s (a drift = AGC).
5. A sine from a tone generator (or REW) at 1 kHz: `▼ MIC 1.00k` within 2 s; hold it: a free bell
   goes to 1 kHz at Q 8.65, silent, the GAIN steps open. Mute: the mic PEAK goes. With pink
   noise playing as well, a tone the box's screen marks as its PEAK should show as `▼ MIC` too
   (same rule, same numbers), at 1 kHz and at 8 kHz.
6. `≋` › RESPONSE with music: `12 s of 20` counts up; mute: `PAUSED · MUTED`; move a band: it
   restarts; move the master: it does not; walk the room 20 s: a readable line. Hold the phone
   right in front of a top, loud: `MIC CLIPPING` shows and the count stops.
7. Deny once on purpose (Chrome: reset the site's permission first): the row says BLOCKED and
   where to allow; nothing else asks.
8. MUTE: hold it with PGM ▾ open, and in MEASURE.

## 7. Tests

- **Unit, node** (`node box/tools/v2lab/micunit.mjs`, 47 checks; run by
  `test_web_v2_page.py`): the night branch's 18 (ballistics in power, smoothing, peak hold, the
  level tracker, the FFT against a DFT, the engine's FFT length rule, its grid and band edges, a
  full-scale sine at 0 dBFS, band power reads pink flat, the response maths and its smoothing,
  matched vs mismatched estimators), the box's PEAK rule against the box's own test cases
  (`tests/test_rta.py`: 1.5 s persistence, hysteresis and release, a steady pointer, nothing
  below / above the range, for a sweep, a plateau, a buried bin, under the level floor, the
  program gate), the phone's search range, the program gate, the `pgm` codec, the RESPONSE
  windows (coverage, a pause holds, a minute drops, a reset), the PEAK's readout between two bins;
  and, after the review: the engine's estimator against **an independent numpy port of
  CamillaDSP's `spectrum.rs`** (`spectrum_ref.py` → `spectrum_ref.json`, a fixed test signal at
  48 and 44.1 kHz: within 0.005 dB on every bin, 0.0001 dB as built; a Hann over n instead of
  n − 1 already fails), the PEAK on the engine's numbers (an 8 kHz tone 7 dB over its 1/24 oct
  band is a PEAK; on the analyser's mean power it never was), a master move divided out of
  RESPONSE (+6 dB of bass and +6 dB of master half-way: flat; not divided, the bass > 1 dB
  high), the 1e-6 grid compare, the 12 kHz top, the clip flag, the estimator on too few samples
  (null, never NaN).
- **E2E, Chromium with a fake mic** (`e2e.mjs`; full Chromium in new headless mode,
  `--use-fake-device-for-media-stream --use-file-for-fake-audio-capture=<wav>`, with
  `--use-fake-ui-for-media-stream` for an accepted dialog; WAVs made at run time by `wav.mjs`:
  pink noise, a 1 kHz tone 15 dB over its 1/24 oct band in pink noise, pink noise through a
  +2 dB/oct tilt). The fake box's program windows are the same pink noise through the engine's
  estimator. It checks:
  - no getUserMedia from LIVE, the popover or MEASURE before the grant (they say where to allow
    it); ALLOW in SETUP asks once, with the three processings off, and reads them back; the
    tracks are stopped under SETUP, restarted on LIVE with no prompt;
  - pink noise → the line flat within ±1.5 dB of its median over 200 Hz–8 kHz (SLOW, 1/3 oct);
  - the tracks stopped on a sheet, on hidden, on PGM ▾ › BOX, after 10 s without a link (not
    after 4 s); restarted without a prompt each time;
  - a 1 kHz tone → the mic PEAK within 1/24 oct of 1 kHz, none while the program is absent, gone
    when muted; `▼ MIC` held and a long-press on the marker both send `grab {hz}` (and nothing
    else); the music dropping 150 or 250 ms before a marker long-press (the PEAK goes mid-hold)
    and, in BOTH, the mic stopping mid-hold on `▼ MIC` (the tag turns into the box's PEAK) still
    send `grab {hz ≈ 1 kHz}`, never `grab {}`; back on BOX the box's PEAK and `grab {}` are
    unchanged; an 8 kHz tone 7 dB over its band → the mic PEAK at 8 kHz;
  - RESPONSE: the same pink on both sides → flat within ±1 dB over 200 Hz–5 kHz after 20 s; the
    tilted mic → a 2 ± 0.3 dB/oct line within ±1 dB; nothing drawn below 100 Hz or above
    12 kHz; muted, no program, no spectrum, the box ramping in pause it (said); a clipping mic
    keeps no window and says `MIC CLIPPING`, on LIVE too; the master does not restart it (its
    next 0.8 s are not used), a band, BAL, the Lo-Cut, polarity, a band OUT, the system EQ
    (`state.sys_eq`) do, and the first window after a restart is kept 0.4 s or more after it;
    RESET; AVG 10 s; `sub {pgm}` on RESPONSE, off on RTA, a sheet, hidden, leaving MEASURE, again
    after the box's `unsub`; `prog: null` and a waiting or absent spectrum pause it, said (the box
    says a state once: no window meanwhile is not a fault); an older box (no `hello.pgm`) says it
    needs the update, a box with `pgm: null` says it has no spectrum, and neither subscribes;
  - audio never leaves the phone: only `ping` and `sub {rta | pgm}` frames, all small; no request
    but the page's own files;
  - the mic refused: BLOCKED, said, never asked again outside SETUP;
  - Android's mic dialog hiding the page while the request is pending (as the app's WebView
    does): the grant survives, no error, and LIVE then starts the mic with no second dialog;
  - MUTE in MEASURE and with the popover open; fit (0 overflow, targets ≥ 44 px, no clipped text)
    in MEASURE (both views) and with the popover open at 393×851, 740, 600, 544 and 360×640;
    `≋` keeps its place when the PEAK comes or goes, at every size.
- **The real round trip** (`real.mjs` from `test_web_v2_page.py`): the REAL server and
  Controller in-process, the production spectrum task (`open_spectrum`) on a fake engine that
  plays a pink noise in real time (GetSpectrum through a numpy port of `spectrum.rs`, the
  program level at −20 dBFS), and full Chromium whose fake mic plays the same noise: `≋` ›
  RESPONSE subscribes, the box's own accumulator and encoding send `pgm` windows, the page keeps
  every one (live, `prog` true), the coverage grows about 1 s a second to 9 s of 10, the line is
  flat within ±1.5 dB over 200 Hz–5 kHz (±0.45 dB as built), leaving MEASURE unsubscribes.
- **Not verified:** a real phone (§6); RESPONSE on CamillaDSP itself (the fake engine is a port
  of its code, not the engine running; the `engine`-marked `test_rta.py` test runs GetSpectrum on
  a real camilladsp where one is installed, without RESPONSE); the walk-the-room procedure
  itself. Thursday's soak on the box is the first run on the real engine.
