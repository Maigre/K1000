# 17 — What the box recorded, and the bench of 12 Nov

2026-11-12, the box on the desk bench with a loop cable (its own analog output into M4 LINE IN 3,
M4 OUT 1 into LINE IN 4), nothing else plugged. This follows `docs/16-post-gig-audit.md`, which
was written before the box was powered, and corrects it. Each figure says whether it was
**measured** on the box, **read** in a log or in source code, or **calculated**. Sections 9 to 13
were added on 13 Nov, the 96 kHz night of section 10 on 14 Nov.

## 1. Summary

- **The theory of docs/16 §2 does not explain the gig.** The box's own logs of that night hold
  no dropout at all (read). The engine ran once, from one start, at the buffer settings that
  give about 20 ms in to out (measured today, 20.2 to 20.4 ms after eight reboots). The saved sub
  and top delays were both 0. So the box added about 20 ms that night, not 50 to 200.
- **The mechanism of docs/16 §2 is real, and it bites at start-up.** On the current engine
  (CamillaDSP 5.0.0-beta.1) one playback underrun leaves the delay 13 to 15 ms higher for the rest
  of the run (measured). The first spectrum request after an engine start stalls the audio for
  about 20 ms and can cause that underrun: 2 engine starts out of 14 came up at 34 ms instead of
  20 (measured).
- **On the current engine no smaller buffer helps.** With less margin every start underruns, and
  the delay never gets under 12.5 ms (measured, whole system).
- **On the new engine (upstream `next5` at `0930f9b`, D-019) the delay comes back after an
  underrun, and the box reaches 5 to 8 ms** (measured, whole system): 6.6 to 7.8 ms at chunk 64 /
  target 128, 9.2 to 10.3 ms at 128 / 128. The box's own service drives it unchanged.
- **A new finding, not about latency: the outputs clipped for hours at the gig.** 5,718 clip
  warnings, peaks up to +5.7 dBFS, mostly on the subs (read). Only the box's screen could show
  it, for 3 s at a time; the phone never did. Section 5 proposes what to do.
- **A second, smaller kind of shift sits below the engine, in the USB stream.** At the smallest
  setting the delay moved by 1.9 ms during a load test with nothing in the engine's log; the
  kernel's count of frames in transit to the M4 moved by the same amount. The first power-on of
  the day (24.35 ms where a reboot gives 20.2) is probably the same thing. The readout does not
  include that figure yet.
- **The night run passed (section 9, added 13 Nov).** 16 hours at chunk 64 / target 128 on the new
  engine: 185 loop measurements, every one at 7.59 ms, no dropout, no restart.
- **13 Nov: the box runs at 24-bit / 96 kHz, chunk 128 / target 256 (D-022, section 10).** 6.1 to
  7.6 ms in to out over 22 starts, 6.42 ms steady and no dropout under load (measured). The sample
  rate is now a setting.
- **13 Nov: the dropout just after a start now falls while the box is still muted (section 11).**
  Before, 14 engine starts out of 14 logged it (measured); with the fix, in 42 starts none of the
  underruns came after the ramp had begun.
- **13 Nov: the readout counts the frames in USB transit (section 12).** It agrees with the loop
  within 0.2 ms at rest. That it follows a real shift is not proven: none happened on the day.
- **14 Nov: the night run at 96 kHz did not pass (section 10).** No dropout in 13 hours, but the
  delay grew from 7.4 to 10.1 ms in two steps with nothing in any log (measured). The readout
  showed both steps. The 48 kHz night of section 9 had none. The cause is not known yet.
- **Still open:** why the delay grew at 96 kHz, a few real power cycles, and Thomas's ear on the
  result.

## 2. The gig night, from the box's logs

The box has no clock battery: without a network its clock resumes from the last saved time. Its
logs of the rig and the gig are therefore dated well before the real time (at least 19 hours
behind from the rig of 17 Oct on, read from the build that was running). The gig is the last boot
before today, about 17.5 hours long, the only one with loud program (inference, high confidence).

| What | Finding | Source |
|---|---|---|
| Dropouts | None: no underrun, overrun, stall, device error or restart in the engine's log, the kernel's or the units'. One engine start for the whole night | read |
| Engine start | Engine start to first playback 0.08 s, as on the bench starts. Nothing differs from a clean start | read |
| Buffer settings | Chunk 256, target level 512, rate adjust off | read (saved config) |
| Sub and top delay | Both 0 when the settings were last saved. ALIGN was opened at least six times; its edits are not logged | read |
| Operator | Master moved 117 times between −10 and 0 dB (107 on the knob, 10 from the phone). No mute, no setup change, no restart. One phone on the Wi-Fi for about four hours | read |
| Power, USB, heat | The kernel is silent after boot; the USB guard did nothing | read |
| Clipping | Section 5 | read |

What the logs cannot say: the buffer level during the music (it was not logged; the readout of
section 6 now logs it), the ALIGN delay while the music played, and how far the DJ stood from the
speakers.

**So for that night:** about 20 ms through the box (24 ms if a power-on start is always like
today's first, section 3.1), plus 3 ms per metre of air. That is enough to make beatmatching
against the mains hard. It is not 50 to 200 ms, and nothing in the box's record supports a figure
that high.

## 3. The bench

All delays are analog in to out, measured through the loop with `box/tools/bench/m4loop.py`
(pink noise, the comb in the engine's spectrum). Three measurements in a row without touching
anything agree to the sample (19.31 ms three times). The tool was extended on the same day to
search up to 150 ms; its simulator checks 8 to 140 ms.

### 3.1 The delay depends on how the audio stream started

Current engine, chunk 256, target 512, the whole system running:

| Kind of start | In to out | Buffer level | Runs |
|---|---|---|---|
| First power-on of the day | 24.35 ms | 536 | 1 |
| Reboot | 20.16 to 20.41 ms | 536 | 8 |
| Engine restart, clean | 20.21 to 20.35 ms | 536 | 12 of 14 |
| Engine restart with one underrun a second after the start | 33.6 and 34.9 ms | about 1000 | 2 of 14 |
| After a forced 50 ms freeze (capture overrun, half a second of silence, recovery) | 19.31 ms | about 490 | 1 |

- The two slow starts each show one line `PB: Prepare playback after buffer underrun`, 1.08 s
  after the engine start, and the buffer then sits full. This is the backlog of docs/16 §2.2,
  measured: one underrun, +13 to +15 ms, for the rest of the run.
- The power-on figure is one run. A reboot does not power-cycle the M4, so it does not test it.
  A few real power cycles will say whether the extra 4 ms is systematic. The buffer level does
  not show it, so it sits outside the engine: in the M4 or the USB stream at its first start.

### 3.2 What causes the underrun at start

The first `GetSpectrum` request after an engine start. In the engine's debug log the underrun
comes 22.5 ms after that request (measured). The engine fills its spectrum buffers only once
something asks; the first request makes the audio threads allocate them, and the audio stalls
for about 20 ms. K1000 asks for the spectrum as soon as it sees the engine running, about a
second after the start. With 536 frames in the buffer (11 ms) the stall is survived most of the
time. With a smaller buffer it never is (section 3.3).

A forced 50 ms freeze does not add delay: it overflows the capture buffer, the engine stalls for
about half a second, and comes back near its base. Only a gap shorter than the capture buffer
(21 ms at chunk 256) and longer than the playback margin adds delay that stays.

### 3.3 Chunk and target, both engines, the whole system

`k1000.service` and `camilladsp.service` as at a gig, four engine starts per setting. On the
current engine the chunk was changed by patching the installed code; the per-profile setting of
section 6 replaces that.

| Chunk : target | 5.0.0-beta.1 | next5 `0930f9b` |
|---|---|---|
| 256 : 512 (the gig) | 20.3 ms, or 31 to 33 ms when the start underruns (2 of 4) | 20.2 to 21.3 ms |
| 256 : 256 | 20.6 to 28.7 ms (4 of 4 underrun) | 15.2 to 16.8 ms |
| 128 : 256 | 18.0 to 18.9 ms (4 of 4) | 11.7 to 12.7 ms |
| 128 : 128 | 15.8 to 19.1 ms (4 of 4) | 9.2 to 10.3 ms |
| 64 : 128 | 13.4 to 14.7 ms (4 of 4) | **6.6 to 7.8 ms** |
| 64 : 64 | 12.5 to 13.5 ms (4 of 4) | 5.2 to 6.4 ms |
| 32 : 64 | the engine did not start | 4.3 to 5.0 ms |

- On next5 the start underrun still happens at the small settings (the spectrum request), but
  the engine then drops the late chunks (`Dropped N frames that arrived late after a playback
  restart`) and the delay is back within a chunk.
- After forced freezes of 15 and 50 ms on next5 the delay came back at every setting.
- Processing load 2 to 6 % of the real-time budget. No dropout in any 60-second quiet hold.
- K1000 at the gig build (`025b8f8`) drove next5 with no change. Off the box, its 262 generated
  configs pass the new engine's check and its engine tests pass on both engines.

### 3.4 Under load

next5, pink noise through the loop, two phone-like clients connected, then four phases: quiet
(3 min), a phone nudging a value every 2 s (2 min), four busy processes (3 min), synchronous
writes to the SD card (2 min). No processor tuning yet (governor `ondemand`).

| Chunk : target | Dropouts in quiet / edits / CPU / disk | In to out at start and at end |
|---|---|---|
| 64 : 64 | 0 / 0 / 0 / 0 | 6.4 ms, 8.3 ms |
| 64 : 128 | 0 / 0 / 0 / 0 | 6.9 ms, 6.9 ms |
| 128 : 128 | 0 / 0 / 0 / 0 | 9.8 ms, 9.8 ms |
| 128 : 256 | 0 / 0 / 0 / 0 | 11.7 ms, 11.7 ms |

No dropout at any setting, and the delay did not move at three of the four. At 64 : 64 it went
from 6.4 to 8.3 ms with no line in the engine's log and with the engine's buffer level unchanged.

A second pass at 64 : 64, measuring after each phase, located it (measured):

| After | In to out | Playback frames in USB transit, as the kernel counts them |
|---|---|---|
| start | 6.11 ms | 48 to 60 |
| quiet | 6.11 ms | 48 |
| edits | 7.99 ms | 144 |
| CPU | 7.99 ms | 144 |
| disk | 7.99 ms | 144 |

The shift came while a phone was nudging a value, and the kernel's own count of frames in
transit to the M4 (`delay` minus the buffer fill in `/proc/asound/M4/pcm0p/sub0/status`) rose by
96 frames, 2.0 ms, at the same moment. So it sits in the USB stream below the engine: the engine
fed the device late by more than the 1.3 ms of margin this setting has, the device missed about
2 ms of sound, and the stream stayed that much deeper. The engine reads the buffer's free room,
not this figure, so it cannot see it; K1000 can read it. With 2.7 ms of margin (64 : 128) it did
not happen in the same test. The 4 ms of the first power-on (section 3.1) is probably the same
kind of thing at the stream's first start.

**The candidate for the night is chunk 64 / target 128 on next5: about 7 ms**, with the largest
margin of the settings under 10 ms.

### 3.5 A formula for the delay

From 27 starts on both engines, chunk 32 to 256 (measured, fits within 0.3 ms):

> in to out = (buffer level + chunk) / rate + 2.3 ms + a quarter chunk

The 2.3 ms is the M4's own share (converters and USB). The quarter chunk is the average by which
the engine's buffer level reads low: the device reports its position once per period, half a
chunk. The "M4 about 3.8 ms" of 16 Oct was 2.3 ms plus a quarter of chunk 256. After a dropout on
the current engine the level stops showing the whole backlog, and the formula becomes a floor.

## 4. What this changes in docs/16

- §2 "the most likely cause": not supported for the gig night. The cause of the report is the
  base delay, which was the "second cause" there.
- §2.2 "about 105 ms": the mechanism is confirmed on the box, one step at a time (+13 to +15 ms
  per underrun). The ceiling was not reached on the bench and was not looked for.
- §3.2 option A (the new engine): confirmed by measurement. It is no longer the fix for a
  growing delay at the gig; it is what makes a small buffer possible at all.
- §3.3: the table's calculated figures were close for the current engine at 256 and wrong below,
  because every start underruns there. Section 3.3 above replaces it.
- §5.1 "the M4's 3.8 ms is a floor": the floor is 2.3 ms.

## 5. The clipping

Read in the gig night's engine log:

- 5,718 `Clipping detected` warnings, 1,065,013 clipped samples. 82 % of them fall in one burst
  of two minutes. The last one is about six hours into the night.
- Peaks: median +1.2 dBFS, 1 % above +3.6, the worst +5.67 dBFS.
- Almost every warning counts an even number of samples, up to two full chunks: the two sub
  outputs clipping together (they carry the same mono signal). Seven warnings include a third
  output.
- At the worst moment the master was at −5 dB and the input was not clipping, so the chain after
  the master had at least about 11 dB of gain at the clipping frequencies. The pieces are known:
  the +6 dB output trims set at the rig after the master, the input trim, the EQ and the sub
  balance. Their exact values at that moment are not, because EQ and balance edits are not
  logged.
- What the operator could see: the CLIP mark on the box's screen, held 3 s, and the clip boxes
  on the meters. The phone page shows nothing about output clipping. Small bursts were answered
  by a cut of 0.5 to 2 dB within seconds; the big burst was not.

The Meyers have their own limiters, so a digital clip here is first of all distortion: a kick's
attack flattened before it reaches them.

**Proposal** (a decision for Thomas, nothing is built):

1. **A limiter as the last stage of every output**, so that the box cannot send a clipped
   sample. The engine offers a clipper and a compressor with a clip limit, both with no delay,
   and a look-ahead limiter whose delay is its attack time (1 ms). Against a 10 ms budget the
   choice is between none and 1 ms.
2. **No fixed gain after the master.** The +6 dB output trims move in front of it, or to the
   input trim, so that master 0 dB cannot be above full scale on a normal source.
3. **Clipping shown where the operator looks**: on the phone too, and latched until
   acknowledged, with the number of dB to take off.
4. **EQ, balance and delay edits written to the journal**, so that the next record of a gig can
   be read whole.

## 6. What came out of the day, in the repository

Built in parallel streams, each reviewed by a second reader, tests green; on the box only where
section 7 says so.

| Change | What it does |
|---|---|
| Delay readout (D-018) | The status bar and the phone show the estimated delay in to out, orange above the limit (10 ms), as a floor after a dropout, with the dropout count. One line in the journal at each engine connect, at each move of more than a chunk, and every ten minutes: a gig now leaves its delay on record |
| Chunk size per profile | `profiles.<name>.chunksize` (64 to 1024) next to `target_level`; the default gives byte-identical configs; the waits follow the chunk |
| No delay hidden behind ALIGN | A sub delay and a top delay can no longer both be set. A pair found in a playing engine is shown (`PA LATE … · MUTE TO CLEAR`) and cleared at the next mute or engine start |
| Watchdog | `k1000.service` is restarted when its event loop hangs (15 s). A restart leaves a playing engine alone |
| Processor settings | Governor `performance`, the engine alone on two cores, switchable with `install.sh --cpu-off`. Not measured yet |
| Engine build | `box/tools/engine-build/`: upstream `0930f9b` for the box, reproducible (the same checksum twice), with the measured build's dependency versions |
| Engine compatibility | Tests against both engines; the fake engine in the tests now refuses what next5 refuses |
| Bench tools | `m4loop.py` to 150 ms, `gaptest.py`, `startseries.py`, `latmatrix.py`; the soak summary reports a buffer that ends above where it started |
| Repair limit (docs/16 §5.1 #5) | The automatic repair of a drifted engine config stops after 3 tries for one drift and raises an alert instead of muting every 30 s |
| A MUTE that never waits (docs/16 §5.1 #6) | Main's mute has its own connection to the engine; it no longer waits behind a config write or an unmute |
| A new phone (Thomas's ask of the day) | After the Wi-Fi code the screen now shows the first-time page, not the https link: a fresh phone no longer lands on a certificate warning and then the old page. That page offers the Android app or the web app as one clear choice. A phone that reads slowly no longer gets a queue of stale frames |
| Truthful documents, CI | README, roadmap and manual say the gig happened; D-016 is recorded as deployed; private details left the current files; the tests run on every push; every deploy command protects the box's APK |

## 7. The faster boot, measured the same day

Reboots in place of the power switch, two boots per step, seconds after power-on:

| Step | Linux start | Ramp start | Full level | First screen |
|---|---|---|---|---|
| Baseline (gig build + the timer) | 10.3 | 15.7 | **18.8** | 16.0 |
| Engine first at boot | 10.4 | 14.3 | 18.3 | 18.5 |
| + no initramfs, no camera or display probing | 8.9 | 13.2 to 14.1 | 17.3 to 18.2 | 17.4 |
| + bootloader `DISABLE_HDMI=1` | 7.85 | 11.7 to 12.5 | 15.7 to 16.5 | 15.9 to 16.5 |
| + ramp 1.5 s | 7.9 | 12.0 | **13.5** | 15.0 |

Full level 5.3 s earlier. Two notes for the branch: the bootloader command it prints appends its
line after the `[none]` section of this box's bootloader config, where it does not apply (it was
written under `[all]` by hand); and "engine first" alone shows the screen 2.5 s later than the
baseline. Not proven: that a real power-on times like a reboot, and the 1.5 s ramp by ear.

## 8. Still to do

1. The night run at the candidate setting on the new engine, with the readout logging: passed
   at 48 kHz, section 9. At 96 kHz the delay grew during the night (section 10): find which of
   the rate, the buffer or the new transit reader does it, one night per change.
2. The kernel's transit figure in the readout: done, section 12. It followed two real shifts
   during the 96 kHz night. Left: the first power-on with it. The note for upstream is drafted
   for Thomas to send.
3. A few real power cycles: the power-on delay (24.35 ms once, at the old setting), and the boot
   times.
4. Thomas listens: beatmatching at the new setting, the ramp.
5. The decisions of section 5.
6. The dropout just after a start: done, section 11.
7. Where the pinned engine binary is hosted, so that a fresh card needs neither Docker nor a
   running box (section 13): Thomas decides.
8. The engine service starts before the M4 exists at some boots and is started again a second
   later (section 10): to look at, it may cost up to a second of boot.

## 9. The night run, 12 to 13 Nov

The box as at a gig, on the code of section 6 with the faster boot of section 7: the new engine,
chunk 64 / target 128, pink noise through the loop as the program, two phone-like clients
connected and reading. From 17:03 to 09:21, 16 h 17 min. Every 5 minutes the noise stopped for a
loop measurement of the delay in to out; a second logger sampled the engine every 10 s.

| | Result | |
|---|---|---|
| Delay in to out | 7.593 ms on each of the 185 measurements, and again at the end | measured |
| Readout on the box | "about 7.8 ms" on each of its 97 ten-minute journal lines | read |
| Engine buffer | 181 to 182 frames in 5,866 samples | read |
| Frames in USB transit to the M4 | 48 or 60 (49 once) | read |
| Dropouts | None in the engine's log; the engine Running in every sample | read |
| Restarts | None, engine or control service; the two clients never had to reconnect | read |
| Clipped samples | 0 | read |
| Engine load | 3.1 to 6.8 % | read |
| Temperature | 57.4 to 62.8 °C, never throttled | read |

The delay did not move by one thousandth of a millisecond. That is expected, not luck: the M4 is
both the input and the output, on one clock, so once a stream has started only a dropout or a
shift in the USB stream can move the delay, and the night had neither. Each measurement is a new
one (its noise figure differs every time).

What the night does not cover: starts (the delay differs from one start to the next, 6.6 to
7.8 ms, section 3.3), real power cycles, edits from a phone during the run (section 3.4 covered
them for minutes, not hours), and heat in a closed case or in the sun.

**After the run** the box was moved to `main` (`b35e568`, the same code with the faster boot
landed). First measurement on the restarted engine: 6.781 ms three times, readout 6.8 ms, buffer
137 frames.

**One thing seen at that restart.** The engine logged one playback underrun 3.5 s after its
start: 0.8 s after the ramp had reached full level, and 0.1 s after the box's spectrum reader
connected to the engine. It is the stall of section 3.2. The new engine gets its delay back from
it (the 6.78 ms above was measured after it), but with a program playing it is a short gap just
after the sound has come in. In the engine logs kept since this code went on the box, 2 starts
out of 5 show it (3.5 and 4.2 s after the start) and one more has an underrun at 6.2 s; the start
of the night run shows none. Not heard, and not measured as audio yet.

## 10. 96 kHz, 13 Nov

D-018 wished for 24-bit / 96 kHz. The M4 offers it, and its converters are 24-bit at every rate.
The rate was a constant in the code (48 kHz), so the first measurements were made with a
bench-only patch of that constant, on the code of section 9.

Four engine starts per setting, then forced 15 and 50 ms freezes, then 45 s at rest (measured,
whole system, new engine):

| Chunk : target at 96 kHz | In to out | Starts with a dropout in the first seconds | After the freezes | At rest |
|---|---|---|---|---|
| 256 : 512 | 11.3 to 11.4 ms | 0 of 4 | 10.9 to 11.0 ms | no dropout |
| 128 : 256 | 6.2 to 7.5 ms | 3 of 4 | 6.5 to 6.6 ms | no dropout |
| 128 : 128 | 4.9 to 6.1 ms | 4 of 4 | 5.1 ms | no dropout |
| 64 : 128 | 3.1 to 4.5 ms | 4 of 4 | 4.2 to 4.4 ms | no dropout |
| 64 : 64 | 3.7 to 3.8 ms | 4 of 4 | 3.5 ms | no dropout |

The engine's load was 4 to 8 %, the box at 61 to 62 °C. Under phone edits, four CPU hogs and
SD-card writes, 128 : 256 stayed at 6.76 ms and 128 : 128 at 6.14 ms, with no dropout (measured).
The formula of section 3.5 holds at 96 kHz with 1.9 ms for the M4's own share (median of 30
measurements, 1.88 to 2.18; it is 2.3 ms at 48 kHz).

**Decision D-022 (Thomas, 13 Nov): 96 kHz at chunk 128 / target 256 is the box's main setting.**
It keeps the same margin in time as 48 kHz at 64 / 128, which passed the night run.

**The rate as a setting.** `profiles.<name>.samplerate` in `k1000.yaml`, 48000 or 96000 for the
M4, read once when the control service starts. A settings file without the key runs 48 kHz with
its own figures, so a box keeps its setting through an update until its file is edited. The
manual (chapter 3) has the five lines to write, and the five lines to go back.

**On the box with that code**, its own file edited to the five lines of D-022 (measured):

| | Result |
|---|---|
| What runs | The engine reports 96000 Hz, chunk 128, target 256; the kernel shows 96000 Hz, S32_LE. Saved delays untouched. `k1000-m4-bringup.sh`: no failure |
| 14 engine starts | 6.19 to 7.46 ms |
| 6 starts of the engine and the control service together | 6.14 to 7.63 ms |
| Load test (quiet, phone edits, four CPU hogs, SD-card writes) | 6.42 ms at each phase, no dropout, 62 °C |
| 2 boots (a reboot in place of the switch) | Full level 14.3 and 14.5 s after power-on; 6.38 and 7.39 ms |
| Readout against the loop | 6.3 for 6.26 ms, 6.4 for 6.38 ms, 7.2 for 7.39 ms |

The delay differs from one start to the next because the engine settles at a different buffer
level each time (251 to 372 frames here). All of them are under the 10 ms of D-018.

At the last of those boots the engine service started before the M4 existed for the kernel, gave
up, and was started again by systemd a second later (read in the journal). The start that
follows is normal. Whether this happens at every boot, and what it costs, is not measured.

Not done at 96 kHz: real power cycles, a phone on the box, the ear.

### The night run at 96 kHz, 13 to 14 Nov

Added 14 Nov. The test of section 9 at the 96 kHz setting, from the second boot above: 19:56 to
09:08, 13 h 12 min, a loop measurement every 5 minutes.

| | Result | |
|---|---|---|
| Dropouts and restarts | None in 13 h 12 min: no line in the engine's log, the engine Running in every sample, 0 clipped samples, 62.3 °C at most | read |
| Delay in to out | 7.391 ms on the first 6 measurements; 9.058 ms on the next 53, from 20:32; 10.069 ms on the last 91, from 01:07 | measured |
| First step, between 20:27 and 20:32 | The frames in USB transit went from about 132 to 288 (+1.6 ms). The engine's buffer did not move | read |
| Second step, at 01:07 | The engine's buffer went from 347 to 444 frames (+1.0 ms). The transit did not move | read |
| Readout on the box | "about 9.0 ms" logged at 20:32:18, then "about 10.0 ms" | read |

**The delay grew by 2.7 ms in two steps and ended just over the 10 ms of D-018.** Nothing is in
the engine's log, the kernel's or the control service's at either moment. D-018 asks for a delay
that never grows: at this setting, on this night, it grew.

The readout showed both steps within seconds or minutes. That is the first proof that it follows
a real shift (section 12).

The 48 kHz night of section 9 had no step in 16 hours. Between the two nights these differ: the
sample rate; the chunk and target; the code, in which the transit reader now reads the kernel's
status 20 times a second; and the buffer level the start happened to give (347 frames here, a
high one). Which of them matters is not known. Seven hours at rest at 48 kHz on the new code,
with the reader running, showed no step (section 11), but that was without a program and without
phone clients.

**At 09:08:41 the M4 left the USB bus**, and came back and left again 23 times in 69 seconds (read
in the kernel's log). The engine crashes when its device disappears, and systemd started it 21
times until the M4 stayed. The box then came back by itself: a muted start, the warm-up, the ramp,
7.31 ms. Nothing in the logs says why the M4 left: no under-voltage flag, no message before the
first disconnect.

## 11. The dropout just after a start

**Before the fix** (code of section 9, 48 kHz, chunk 64 / target 128): 14 engine starts out of 14
logged a playback underrun in the first seconds (measured). At 96 kHz it was 3 or 4 starts out
of 4 at every setting under 256 : 512 (section 10).

**The cause** (read in the engine's source, upstream `next5` at `0930f9b`): the engine keeps a
buffer of 262,144 values per channel for its spectrum. It allocates that buffer on the audio
thread, at the first chunk after the first spectrum request of a run. That is the stall of
section 3.2. The control service asked for the spectrum when its displays started, which the
faster boot of section 7 had moved to after the ramp.

**The fix**: on a freshly started engine, the control service now asks once for the spectrum
while Main is still muted, on a connection of its own, and waits at most 0.3 s for the answer.
Then it ramps. An engine that is already playing is left alone.

**With the fix** (measured, the time of each dropout line of the engine's log against the
control service's journal):

| | Starts | Underrun lines after the ramp had begun |
|---|---|---|
| 48 kHz, 64 / 128, engine starts | 14 | 0 |
| 48 kHz, 64 / 128, engine and control service together | 6 | 0 |
| 96 kHz, 128 / 256, engine starts | 14 | 0 |
| 96 kHz, 128 / 256, engine and control service together | 6 | 0 |
| 96 kHz, 128 / 256, boots | 2 | 0 |

On an engine start the underrun comes 0.04 to 0.08 s after the engine's first log line, more
than a second before the ramp. When both start together, as at a boot, it comes at least 0.17 s
before the ramp begins. One start of the 42 shows one more line 0.27 s into the ramp: the engine
dropping 128 frames (1.3 ms) that arrived late, with Main at about −33 dB. Seven hours at rest on
this code at 48 kHz: no dropout (read).

The underrun itself still happens, muted. It is what makes the buffer level, and so the delay,
differ from one start to the next (6.6 to 7.9 ms at 48 kHz, 6.1 to 7.6 ms at 96 kHz). Only a
change in the engine would remove it.

## 12. The USB transit in the readout

The readout now reads what the kernel holds in USB transit to the M4 (section 3.4): a worker
thread reads the stream's status 20 times a second. One read costs 88 µs of CPU on the box,
about 0.2 % of one core (measured). Seven hours with it running: no dropout (read).

Single reads do not sit on one value (measured, the engine at rest):

| Setting | Reads | Where they fall |
|---|---|---|
| 48 kHz, chunk 64 | 3,620 | 60 frames 46 %, 72 frames 52 %, 12 frames 1 % |
| 96 kHz, chunk 128, three starts | 820 each | 96 frames about 46 %, 120 frames about 50 %, 24 frames about 3 % |
| 96 kHz, chunk 128, one start | 820 | 120 frames 74 %, 144 frames 11 %, 24 frames 14 % |

A pair of values 0.25 ms apart, and a small group one millisecond lower. A median flips between
the two values of the pair: the first version of the reader used one, and read 7.1 ms for seven
hours where the loop measured 6.68 ms. The figure is now the mean of the reads near their
median. On the data above it gives 1.13 ms at rest at both rates, and 1.28 ms on the one start
where the loop also measured 0.16 ms more. The readout adds what the figure holds beyond
1.13 ms, and counts nothing within 0.4 ms of it.

The load test at chunk 64 / target 64 was run again on 13 Nov and no shift happened: 5.22 ms at
every phase, readout 5.3 ms. The first real shifts came during the 96 kHz night (section 10): the
readout went to 9.0 ms where the loop measured 9.06, then to 10.0 ms where it measured 10.07.

## 13. The engine a fresh card gets

`box/bootstrap.sh` installed the old engine and would have put it back on a box that runs the
new one. It now installs the pinned build of D-019 from a local file, after checking its
checksum; it leaves a box that already runs it alone; and the old engine comes back only with
`--old-engine`. On the desk box (read): the engine's checksum is the pinned one, and a dry run
says "nothing to do".

The build recipe now pins its Rust toolchain and base image. Built again from an empty cache, it
gave the same checksum a third time. There is still no download address for the binary: a fresh
card needs the recipe (Docker) or a copy from a box that runs it. Where to host it is Thomas's
decision.
