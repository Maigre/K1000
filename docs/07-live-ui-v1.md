# 07 — Live UI v1: SHOW · ALIGN · SETUP (design spec)

> **As built (2026-10-13).** UI v1 is built, deployed and accepted on the box (`f7aa58b-v1`).
> Thomas's choices (**D-009**, 2026-10-12) overrode this spec where they differ, and the build
> followed D-009. The spec below is kept as written. The operator view is `04-operator-card.md`,
> the contract changes are `03-v0-architecture.md` §8. What the build did differently:
>
> | Spec (below) | As built |
> |---|---|
> | Chips `SUB · LS · 1 · 2 · 3 · 4 · HS`, SUB puts the balance on E3 (§0 #3, §2.6) | **No SUB chip.** Chips `LS · 1 · 2 · 3 · 4 · HS`; the balance is on ALIGN only (E2) |
> | B3 hold on SHOW = previous band | No previous band: a B3 tap steps LS → 1 → 2 → 3 → 4 → HS and wraps; a hold does nothing on SHOW |
> | ALIGN: Lo-Cut on/off on a B3 hold, alignment knobs live (§3) | **ALIGN E3/E4 (sub delay, Lo-Cut freq) are locked**: a turn shows `LOCKED · HOLD B3 TO EDIT` and changes nothing. A B3 hold unlocks them (`EDIT 0:30` badge, renewed by any input on ALIGN, so 30 s idle relocks; a B3 tap or leaving ALIGN relocks). Sub delay keeps its wake-up detent. Lo-Cut on/off is a SETUP item with a double-push confirm |
> | GRAB (§2.4): freq = the PEAK, else the LOUDEST bin, else `GRAB: NO RTA`; FREQ highlighted; hint `GRAB: P3 → 2.47k · E3 TO CUT` | **Notch helper**: a free bell (gain exactly 0, the selected one first, never a shelf) always gets Q 8.65; its freq moves only to a detected PEAK (no LOUDEST fallback: `P2 · 1.00k · NO PEAK · E3 TO CUT`). A PEAK within 1/6 oct of a bell already in use selects that bell and writes nothing. The edit highlight goes on **GAIN**, hint `P3 · 2.44k · E3 TO CUT` (4 s); `NO FREE BAND` when all four bells are in use. Gain is never written |
> | Reset all live EQ = B4 hold, twice within 5 s (§0 #8) | **SETUP › Reset live EQ** (two pushes), then **SETUP › Undo reset** (one level; until the next *audible* live-EQ edit from any surface, or 5 min; an edit that ends it says `RESET UNDO LOST`). A B4 hold on SHOW only shows `RESET LIVE EQ: IN SETUP` |
> | SETUP gains `RTA source` PGM / MIC / OUT / OFF (§4, §5.1); kill switch `ui: {rta: false}` | **SETUP › Spectrum ON/OFF** only (default ON, decided at Friday's rig test); the source is always the program (capture side, the profile's `input_map` channels, power average). Kill switch: `spectrum: enabled: false` in k1000.yaml (no task, no second connection). MIC and OUT are not built |
> | SETUP list: v0 + RTA source | v0's list + Reset live EQ ×2 · Undo reset · Lo-Cut ×2 · Spectrum · **Laptop access ×2** (new, not in this spec: opens CamillaGUI and SSH on the K1000 Wi-Fi for 2 h, plus the `/tune` page; `06-phone.md` §11) · Phone · Knob test |
> | 265 bins; polled on SHOW with source ≠ OFF and engine Running | 264 bins (1/24 oct, 10 Hz–20 kHz); polled at 10 Hz on its own websocket while SHOW is displayed, Spectrum is ON, the engine is Running **or Paused**, and the device is present; the connection closes after 30 s unwanted. New frames are held for 300 ms after any knob or button event |
> | Range: loudest hold rounded up to 6 dB + 18 dB headroom (§5.2) | **Slow tracker**: rises at once, falls 1 dB/s, no 6 dB steps (still +18 dB headroom, 66 dB deep) |
> | PEAK: 10 dB above the median of ±1/3 oct, steady 0.7 s (§2.4) | ±**1 octave** context, enters at 10 dB, released 0.5 s after it drops below 6 dB (hysteresis), within 30 dB of the loudest bin, 40 Hz–16 kHz, persistent **1.5 s**, on a 0.25 s power average; the pointer moves only by ≥ 2 bins. **Never on silence** (added after the acceptance): the peak bin ≥ −80 dBFS (`spectrum.peak_floor_db`) and the program's capture RMS ≥ −60 dBFS (`spectrum.program_floor_db`) |
> | PEAK ▼ + `PEAK 2.44k · B4 GRAB`; `RTA PGM` tag | A small cyan pointer + its frequency, no call-to-action text; no source tag. A grey `NO SPECTRUM` only when the engine refuses (its warm-up answers after a start are not refusals) |
> | Controller messages in a dark-amber strip on the chip row; MUTED band carries alerts (§0 #14, §2.7) | **System alerts take the status bar** (critical red, KNOBS OFFLINE full width; warning orange; info blue, e.g. RAMPING IN); a critical one is repeated over SHOW's chips when no hint is up. Hints keep the amber strip, briefly. **Persistent pills** on SHOW's bottom row: LAPTOP OPEN, LO-CUT OFF, POL INV (orange), MONO SUB, AUDITION (grey). The 0 dB line is labelled, dB labels at the right edge |
> | No master guard beyond Patch B (§2.2 rejected a cross-knob guard) | **Master-up guard** (D-009): master up is never accelerated and rises at most +6 dB within any 2 s (`controls.master_up_guard_db`, 0 = off), the rest refused with `MASTER: SLOW DOWN`. Down is never limited |
> | 300 ms fader ramp (doc 08) | **100 ms** for Main and the Aux faders: master and balance answer the hand; mute and unmute fade over 0.1 s |
> | Auto-home not while a SETUP or B4 confirm is pending | Not while a SETUP confirm is pending (there is no B4 confirm); PHONE and the Knob test never go home |
> | Phone parity (§8.1): front page = SHOW's controls; GRAB rule | The PWA keeps its own layout (MAIN: master, mute, balance, Lo-Cut; LIVE EQ) and has no spectrum. Its Lo-Cut switch and live-EQ reset go through the same Controller paths (confirm on the phone; the undo is on the box). Its "notch here" still cuts −6 dB at Q 8 at once |
> | Cost ≈ 10 % of one core in k1000 (§6) | Measured at the acceptance (silent input, SHOW + spectrum, 60 s): k1000 **19.6 %** of one core (max 32.8 %, unexplained ~30 % spikes every ~11 s), camilladsp 4.7 %, engine load 0.98 %, 0 xruns |
> | Knob sampler under continuous rendering: "[?] re-run the bench" (§8) | Measured: MASTER lost ~24 % of its clicks at 45 clicks/s with SHOW drawn; fixed by a 1 ms GIL switch interval and lighter saves (`08-knob-precision.md` §10, PERF-1..3). Patches A and B are applied |
>
> **Found at the acceptance, fixed after it:** `NO SPECTRUM` for ~5 s after every engine start
> (CamillaDSP 5.0.0-beta.1 answers `Insufficient data in buffer` while its analyser fills: now a
> quiet retry every 0.4 s, a refusal only after 10 s), and a PEAK on a silent input (a −114 dBFS
> noise bin near 7.3 kHz was shown and B4 aimed at it: now the level floor and the program gate
> above, so B4 says `NO PEAK` on silence and between tracks). `03-v0-architecture.md` §8 has the
> details and the measured scales.
>
> **Changed after the acceptance by D-011** (2026-10-13, Thomas's asks at the bench): ALIGN E3 is
> **DELAY**, one signed setting, −50 … +50 ms (+ = the subs later, − = the tops later), shown as the
> size plus the side in words (`SUBS LATER · 0.43 m`, `TOPS LATER · 0.43 m`, `ALIGNED`), with a stop
> on 0 before crossing; it writes `sub_delay_ms` or `top_delay_ms` and zeroes the other. So §3's
> "Top delay stays in SETUP" no longer holds: SETUP has no Top delay (19 items). The Lo-Cut moves
> **2 Hz a click** (1 Hz fine, on that grid) instead of 1/12 octave (§3's table). The E1–E4 tags
> are **dim grey** by default (k1000.yaml `ui.knob_tags`: dim, off, bright); this spec's cyan tags
> are the `bright` style. Hints: `DELAY + LO-CUT UNLOCKED` / `LOCKED`, `DELAY · TURN AGAIN TO CHANGE`.
>
> **Superseded by D-013** (2026-10-13, Thomas at the bench) for the buttons and SETUP, in the note
> above and in the spec below: **B1 PAGE · B2 BAND/UNLOCK · B3 NOTCH · B4 MUTE**. A B1 tap flips
> SHOW ↔ ALIGN (from SETUP: SHOW), a B1 hold opens SETUP. Band select and the ALIGN unlock move from
> B3 to B2 (`LOCKED · HOLD B2 TO EDIT`), the notch helper from B4 to B3, the mute (long press only)
> from B2 to B4. SETUP is grouped by speaker floor (TOPS, SUBS, INPUT, LIVE EQ, PHONE, BOX; E3 jumps
> a group); every ON/off or choice is chosen with E2 (the row shows `now → chosen`) and set by a
> push, so Lo-Cut on/off has no double push. Details: `DECISIONS.md` D-013, `docs/manual/04-usage.md`.
>
> **Changed by D-015** (2026-10-14, Thomas at the bench): the knob roles and the layout of §2.1,
> §2.2 and §3. **SHOW: E3 = Q** (a shelf's `SLOPE`), with the band's name beside its label
> (`PEQ 2 Q`); **E4 = GAIN**, so the notch strip says `P3 · 2.44k · E4 TO CUT` and the notch is
> B3, E4 down, E3 up. **ALIGN: E2 = DELAY, E3 = BALANCE**: the two locked knobs (DELAY, LO-CUT) are
> the bottom pair, and the `LOCKED · HOLD B2` / `EDIT 0:30` badge sits on the bottom row between
> their labels, joined to both by a line; the MUTED band leaves the sub-lines visible. **Every
> corner value is flush to its outer border** with its unit beside it (left corners
> left-aligned, right corners right-aligned); FINE moves to the inner side (a top knob's into its
> label row). **Smaller legends and values**: labels 20 → 17 px, SHOW values 44/40 → 38/36 px,
> ALIGN values 60 → 52 px, chips 24 → 22 px tall: the graph is **152 px high** (y 112–264; it was
> 130, y 124–254). **Input meters**: IN L R (the program input's capture peaks, before any gain)
> left of SUB L R and TOP L R on both pages, the same scale and colours, `IN CLIP` lighting their
> clip boxes. §2.1's diagram and §2.2's table below are the D-009 layout. Details: `DECISIONS.md`
> D-015, `docs/manual/04-usage.md`.

2026-10-12 evening, answering Thomas's notes on the v0 panel. Status: **design and prototype,
nothing wired** (superseded by the note above). The engine and panel figures were measured on the
box tonight, read-only.

New files:
- `box/k1000/ui/eqgraph.py`: the curve math and the drawing.
- `box/tests/test_eqgraph.py`: 41 tests.
- `box/k1000/ui/mock_v1.py`: the prototype pages that drew the mockups.
- `docs/ui/v1/*.png`: the mockups, drawn with pages.py's own helpers.

Knob feel is covered separately in `docs/08-knob-precision.md`. It is a decoder fix, not a
debounce.

Tags: **[M]** measured tonight · **[S]** from a source (the research brief, CamillaDSP v5 source)
· **[G]** design judgement · **[?]** uncertain, to check.

## 0. Recommendations, one line each

1. **SHOW is the front page.**
   - Encoders: E1 MASTER · E2 FREQ · E3 GAIN · E4 Q of the selected band (v0 LIVE muscle memory, plus
     master).
   - Band chips: `SUB · LS · 1 · 2 · 3 · 4 · HS`.
   - Middle: the live-EQ curve over a live RTA.
   - Top centre: the meter.
2. **B3 on SHOW selects the band.** A tap moves to the next band (→), a hold to the previous (←).
   The encoder push stays "fine", never "select".
3. **The SUB chip lives on SHOW** (variant A). Selected, it puts the sub level (the v0 balance) on
   E3. The balance is also on ALIGN.
4. **ALIGN is the v0 MAIN page, renamed and moved to second place.**
   - Encoders: E1 MASTER · E2 BALANCE · E3 SUB DELAY (wakes on the first detent) · E4 LO-CUT.
   - Lo-Cut on/off becomes a **B3 hold**. A tap only shows a hint.
5. **SETUP** keeps the v0 list, plus `RTA source`. Top delay, polarity and the sub low-pass stay
   there.
6. **Navigation:**
   - B1 tap: next page (SHOW → ALIGN → SETUP).
   - B1 hold: SHOW.
   - ALIGN and SETUP go back to SHOW after 60 s with no input. The Knob test and a pending
     confirm stay where they are.
7. **GRAB (B4 tap on SHOW)** puts a *free* bell (one at 0 dB) on the RTA peak at Q 8.65 (1/6
   oct). Gain is not touched, so it is **inaudible by construction**. It never moves a band in
   use. It earns its place if the RTA does.
8. **B4 hold, twice within 5 s, resets the live EQ.** This is v0's gesture, now on SHOW.
9. **No EQ IN/OUT for Sunday.** Bypassing the live EQ removes the feedback notches, and the ring
   comes back. A mis-press would be the worst failure the page can produce.
10. **RTA source: PGM by default.** That is the program input, pre-EQ, L+R power-averaged.
    - A ring from a live mic in the mix goes *through* K1000, so it shows up there.
    - It also works while muted.
    - MIC and OUT are in SETUP, to try on Friday.
11. **RTA engine call:**
    - `GetSpectrum` on the capture side, one call per program channel: 10 Hz to 20 kHz, 265 bins
      (1/24 oct), polled at 10 Hz.
    - On a **second websocket connection**, and only while SHOW is displayed.
    - Never `channel: null` on the M4: it averages the mic and the spare input into the program.
    - Never `SubscribeSpectrum`: it fails silently (§5).
12. **Cost:** SHOW at 10 fps is about **10–16 % of one core** in k1000 [M]. The engine's processing
    load does not move [M]. Ship the faster RGB565 packer and write only the changed rows.
13. **GEQ:** none for Sunday, and never as a toggle of the same EQ. If one ever comes, it gets its
    own fixed, flat slot and its own page, after a click test on the bench (§7).
14. **Keep the v0 overlays.** NO DEVICE and ENGINE DOWN replace the page body. MUTED shows as a
    red band, frame and pill. Hints and controller messages take the chip row.

## 1. What Thomas asked → what v1 does

| Thomas | v1 |
|---|---|
| "a page with SUB DELAY and LO-CUT … not front page … risky to touch" | **ALIGN** is second in the cycle. Sub delay keeps its wake-up detent. Lo-Cut on/off needs a **hold**. Pages go home to SHOW after 60 s idle |
| "a MAIN page with EQ on it, so every live control is on the same page" | **SHOW**: master, the live EQ (6 bands) and the sub level, on one page |
| "spectrum analyser … coupled with a master filter/EQ view … see both the background signal and the applied EQ … LS – EQ1 – EQ2 – EQ3 – EQ4 – HS point selection" | The curve is drawn over an RTA, and each band has a marker. The selected band is a big amber marker with an amber fill of its own contribution. Chips `SUB · LS · 1 · 2 · 3 · 4 · HS` sit above the graph |
| "consider a toggle to switch to master GEQ? not sure" | Not for Sunday, and not as a toggle (§7). Concept mock: `geq_concept.png` |
| "easy live usage … common pro console behaviours" | Band buttons plus shared knobs (X32 strip). The analyser sits behind the curve, semi-opaque (X32, Yamaha, dLive). Master stays on one fixed encoder. Reset is a gated chord. A "jump to the RTA peak" key follows the AFS/FBX persistence idea |
| "Knobs … not precise, a better debounce?" | Not debounce: see `docs/08-knob-precision.md` (decoder pause rule, transitions per click, acceleration, 300 ms fader ramp). Also measured here: **knob-to-glass latency** (§6) |

## 2. SHOW (front page)

Mockups:
- `show.png`: normal, P2 selected, RTA with a PEAK.
- `show_edit_fine.png`: FREQ in fine mode, CLIP pill.
- `show_grab.png`: right after GRAB.
- `show_shelf.png`: LS selected.
- `show_sub.png`: SUB selected.
- `show_no_sub_chip.png`: variant B.
- `show_muted.png`, `show_muted_alert.png`: muted.
- `show_alert.png`, `show_knobs_offline.png`: controller messages.
- `show_flat_rta_off.png`: flat EQ, RTA off.
- `show_eq_out.png`: concept only.
- `no_device.png`.

### 2.1 Layout (480 × 320, `mock_v1` constants)

```
y   0 ┌ status bar (v0): [SHOW] [MUTED] [CLIP] m4 · MOTU M4 ……… ● RUNNING  DSP 2.0%
   30 ├ E1 MASTER            ┌meter┐            PEQ 2 GAIN E3     label row (20 px)
      │  -12.5 dB            │SUB TOP│            -6.5 dB         values 44 px (cap ≈ 32 px)
   96 ├ [SUB +3.0] [LS] [1] [2] [3] [4] [HS]     chip row, 20 px; hints and messages land here
  124 ├ graph 480 × 130: RTA (dark teal) · grid · selected-band fill (amber) · curve (white, 3 px)
      │ · band markers · PEAK ▼ + "PEAK 2.44k · B4 GRAB" · "RTA PGM" tag (top right)
  254 ├  2.52k Hz                  8.00 1/6 oct                  values 40 px (cap ≈ 29 px)
      │ E2 FREQ                                    Q E4          label row
  320 └
```

- **Big values sit next to their encoder** (v0 rule): MASTER top-left, GAIN top-right, FREQ
  bottom-left, Q bottom-right.
  - Glyph heights: values 29–32 px; labels and chips about 14.6 px (font 20); PEAK about 14 px
    (font 19).
  - Axis labels are 14 px regular (cap about 10 px). They are for orientation, not read at arm's
    length.
- **The selected band is shown three ways:** the amber chip, the big amber marker, and its name
  next to GAIN ("PEQ 2", "LO SHELF").
  - The chip row is the only one a hint can hide.
- **Q shows its width in octaves** ("8.00 · 1/6 oct"), so notch thinking needs no table.
  - Shelves show "SLOPE · 0.71 Q".
- **Graph scale +9 … −21 dB.** 0 dB sits high because live boosts stop at +6 dB, and cuts down to
  −20 are the job.
  - Grid lines at +6/−6/−12/−18 dB, and at 50 … 10k Hz.
- **Graph drawing order:**
  1. RTA, behind everything.
  2. Grid and labels, one cached layer.
  3. The selected band's own contribution, amber, blended at 60 %.
  4. Idle markers: hollow, small, *under* the curve, so they never hide it.
  5. The combined curve.
  6. The PEAK pointer.
  7. Markers of bands in use (orange cut, green boost, as the v0 pills), then the selected
     marker last.
- **The curve is the engine's sound**, not a sketch (see §2.5).
  - It is the live EQ only (LS, P1–P4, HS), which acts on subs and tops alike.
  - The system PEQ ×8, Lo-Cut and balance are not in it. A faint system curve could come later
    [G].
- **The meter is the v0 4-bar meter, compacted to 80 × 65 px.** The CLIP and IN CLIP pills in the
  status bar stay the readable clip warning.

### 2.2 Encoders

| Enc | Chip = LS/1–4/HS | Chip = SUB |
|---|---|---|
| E1 (top-left) | **MASTER**, as v0 MAIN (0.5 dB; acceleration per `docs/08` Patch B: up never, down ×4 on a hard spin) | MASTER |
| E2 (bottom-left) | **FREQ** of the band: 1/12 oct, push = 1/48; acceleration only above 20 det/s (Patch B) | idle: hint `SUB: E3 = LEVEL · B3 = NEXT` |
| E3 (top-right) | **GAIN** of the band: 0.5 dB, never accelerated | **SUB LEVEL** (`balance_db`, 0.5 dB, + = subs up) |
| E4 (bottom-right) | **Q**: 1/6 oct log steps, push = 1/24; shelves: slope | idle, as E2 |

Considered and rejected:
- **[G] FREQ and GAIN together on the right column**, for a one-hand notch. Rejected: v0 LIVE
  already has E2/E3/E4 = FREQ/GAIN/Q, the operator card says so, and the gig is in six days.
- **A "cross-knob guard" on master up.** It would also delay a deliberate move, and Patch B
  already removes master-up acceleration. The 0 dB `volume_limit` caps it anyway.

### 2.3 Buttons on SHOW

| Button | Tap | Hold (0.8 s) |
|---|---|---|
| B1 | next page → ALIGN | SHOW (no-op here) |
| B2 | hint `HOLD B2 TO MUTE / UNMUTE` | **MUTE / UNMUTE**, as on every page (unchanged) |
| B3 | **next band** → (SUB → LS → 1 → 2 → 3 → 4 → HS → SUB) | **previous band** ← |
| B4 | **GRAB** (§2.4) | arms **RESET ALL LIVE EQ**; a second hold within 5 s resets (v0 gesture) |

Encoder pushes stay "fine while held" (v0). A push released without turning does **nothing** on
SHOW. Making it "next band" would let a fine-turn that never turned move the selection, and the
next turn would then edit a band nobody is looking at.

### 2.4 GRAB: the ring-out shortcut

The research recipe: sweep a narrow cut onto the ring, then deepen it (AFS/FBX persistence, A&H
"narrow −12 dB sweep") [S]. On four encoders the slow part is finding the frequency. GRAB does
only that part, and only where it cannot be heard:

1. **Target.** The RTA's **PEAK**, if there is one: a bin at least 10 dB above the median of its
   ±1/3 octave, not buried (within 30 dB of the frame's loudest bin), steady within one bin for
   0.7 s, between 40 Hz and 16 kHz.
   - Otherwise the **loudest bin from 100 Hz to 16 kHz** ("LOUDEST").
   - Otherwise nothing: hint `GRAB: NO RTA`.
2. **Band.** The selected chip if it is a bell at 0 dB, else the first bell at 0 dB (P1…P4).
   - Never a shelf, never a band in use.
   - None free: hint `GRAB: NO FREE BAND (SET ONE TO 0 dB)`. Nothing changes.
3. **Write.**
   - `freq` = target, clamped to `live_peq.freq`.
   - `q` = 8.65 (1/6 oct).
   - **`gain` untouched (0 dB).** A 0 dB peaking biquad is exactly unity (b = a, tested), so the
     sound does not change.
4. **UI.**
   - Select that chip.
   - FREQ edit highlight.
   - Hint `GRAB: P3 → 2.47k · E3 TO CUT` (adds `(LOUDEST)` when there was no PEAK).

The operator then turns E3 down (cut), E2 with the push held to centre it, and E4 to narrow it.

A wrong grab costs nothing: a band at 0 dB sits at a new frequency. It never moves an existing
notch away from its ring, which is the failure "move the selected band to the peak" would have.

The detector says **PEAK, never FEEDBACK**: a held synth note looks the same as a ring.
- It was tuned on synthetic spectra.
- **[?] Retune it on Friday on real program**, with a ring made deliberately at low level.

### 2.5 Curve accuracy (eqgraph)

- **Coefficients copied from CamillaDSP v5.0.0-beta.1** (`camilladsp-schema/src/filters/biquad.rs`,
  `from_config`):
  - RBJ, alpha = sin w0 / 2Q.
  - Shelves: beta = sin w0 · √A / Q. This is the `q` form `dspconfig.py` writes, and it equals
    the cookbook's 2√A·alpha.
  - Highpass and lowpass as RBJ. The GraphicEqualizer band is Peaking/Bandwidth.
- **Evaluated digitally at 48 kHz** with the stable φ = sin²(w/2) form. So the curve shows the
  real cramping near 20 kHz.
- **Tests:**
  - A peak reads its gain at f0 (±1e-6 dB).
  - Shelves read half their gain at f0, and the full gain at the far end.
  - The Lo-Cut is −3.01 dB at fc and −12.3 dB an octave below.
  - **CamillaDSP's own biquad test assertions** (same filters, 44.1 kHz) pass.
  - The φ form matches direct complex evaluation (±1e-6 dB).
- **Cost:** the graph draws in **4.9 ms** on the box (median, with the RTA) [M] and 1.0 ms on the
  dev machine.

### 2.6 SUB chip vs balance on ALIGN only

| | A: SUB chip on SHOW (recommended) | B: balance on ALIGN only |
|---|---|---|
| Sub level live ("more bass" for a DJ set) | B3 to SUB (SUB is first, so it is usually 1–3 taps), E3 | B1 to ALIGN, E2, then back (and now E3 under the hand is SUB DELAY) |
| Risk | E3 means SUB LEVEL instead of GAIN while SUB is selected. Shown big ("SUB LEVEL"), and a 0.5 dB balance step is benign | Leaving the box on ALIGN; more page trips during the show |
| Mock | `show_sub.png` | `show_no_sub_chip.png` + `align.png` |

### 2.7 Overlays on SHOW

They are the same as in v0, placed so the edited value, the edit box, FINE and the 0 dB line stay
visible:

- **NO DEVICE > ENGINE DOWN:** replace the body (v0 code, unchanged).
- **MUTED** (`show_muted*.png`): red pill, red 4 px frame, and a red band over the chip row and the
  graph top (y 98–160). The band says what v0's says: `READY · hold B2 to unmute`, `RAMPS IN BY
  ITSELF …`, or the controller alert (`NO AUDIO FLOWING · HELD MUTED`, `KNOBS OFFLINE …`).
- **Controller message** (`PIPELINE CHANGED OUTSIDE K1000`, `CONFIG DRIFT …`): dark-amber strip
  on the chip row (`show_alert.png`).
- **Hint** (2.5 s): amber strip on the chip row (`show_grab.png`).
- **Order of precedence:** NO DEVICE > ENGINE DOWN > MUTED band (it carries the hint or alert) >
  hint > message.
- **The graph never draws outside its box** (tested), so a big curve cannot hide an alert.

## 3. ALIGN (`align.png`, `align_lowcut_off.png`, `align_muted.png`)

**ALIGN is the v0 MAIN page, renamed and moved to second.** It reuses tested code (the
`_quadrant` layout, meter, middle strip and MUTED band). The operator card already describes it.

| Enc | ALIGN | Guard (unchanged from v0) |
|---|---|---|
| E1 | MASTER | as on SHOW: master never changes meaning on a live page |
| E2 | BALANCE (+ = subs up) | — |
| E3 | SUB DELAY, 0.1 ms, push = 1 sample | the first detent after 2.5 s at rest only wakes it. Whole samples only, a write only when the sample count changes, and a group-fader dip around the write |
| E4 | LO-CUT (tops HP 12 dB/oct), 1/12 oct, push = 1/48 | Off = parked at 10 Hz (a frequency move, never a pipeline change) |

Buttons:
- **B3 hold** = Lo-Cut on/off. A B3 tap shows `HOLD B3: LO-CUT ON/OFF`, so the band-select tap
  habit from SHOW cannot switch it.
- B4 = hint.
- B2 = mute.
- B1 tap/hold as everywhere.

What else belongs to alignment, and where it goes:
- **Top delay:** stays in SETUP (rare; the four encoders are taken).
- **Polarity sub/top:** SETUP (rare; flips are guarded dips). [G] A read-only line on ALIGN was
  tried and dropped: no room without hiding a value.
- **Sub low-pass:** SETUP (the USW-1P has its own).
- **Mono subs:** SETUP (a rebuild, double push).

## 4. SETUP and navigation (`setup.png`)

**SETUP list:** v0's, with one new item, `RTA source` (PGM / MIC / OUT / OFF).
- A push moves to the next value. No confirm: the item never touches audio.
- It is persisted in `state.json`; the engine ignores it.
- The full list: Input trim · Top delay · Sub polarity · Top polarity · Sub low-pass · Sub LP freq
  · Trims ×4 · **RTA source** · Mono subs · Audition (dev) · Profile · Knob test.

**Navigation:**
- **B1 tap** = next page, on press (instant, v0).
- **B1 hold** = SHOW, from anywhere. Holding B1 always lands on SHOW: the press moved one page, the
  hold undoes it.
- **Home after 60 s.** ALIGN and SETUP fall back to SHOW after 60 s with no knob or button event.
  Not from the Knob test, and not while a SETUP or B4 confirm is pending. [G] Processors do this,
  consoles do not.
  - Why it matters: on ALIGN, E3 is SUB DELAY. The B3 hold and E3's wake-up detent already guard
    it; the timeout removes the long tail.

## 5. RTA: what the engine gives (probed tonight, live engine, read-only)

**Box:** dev profile, HiFiBerry, 2 ch, silence at the input. CamillaDSP 5.0.0-beta.1. The box was
rebooted several times by other agents during the evening, which gave clean fresh-engine runs.

| Fact | Value [M] |
|---|---|
| Command | `{"command":"GetSpectrum","value":{"side":"capture"\|"playback","channel":int\|null,"min_freq":f,"max_freq":f,"n_bins":n}}` |
| Channels | `capture` = capture device channels (M4: 0–1 front inputs, 2–3 LINE IN 3–4); `playback` = **physical** output channels after the output mixer (M4: out1 Top L, out2 Top R, out3 Sub L, out4 Sub R). Out of range → `InvalidRequestError: Channel 2 out of range (2 channels available)` |
| `channel: null` | averages the **waveforms** of all channels. On the M4 capture that mixes mic + input 2 + program L + R: never use it |
| FFT length | next power of 2 ≥ fs / min_freq: min 20 Hz → 4096 (85 ms, 11.7 Hz) · **10 Hz → 8192 (171 ms, 5.9 Hz)** · 5 Hz → 16384 |
| Bins | log-spaced, each = the **peak** FFT bin in its band (dBFS, 0 = full-scale sine). Below about 1/12 oct per FFT bin, neighbours repeat (seen: 4–10 identical bins at the bottom). No averaging in the engine |
| Reply | 121 bins ≈ 2.5 kB, 0.3–0.5 ms back-to-back; 241 bins ≈ 5.0 kB. Isolated calls at 5–10 Hz: **1.8–1.9 ms** round trip median (CPU clocked down between calls) |
| First call | a fresh engine answers `No audio data available` (seen at 20:22 on a fresh boot); the call sets a **sticky** flag, and audio threads fill a 262 144-frame ring buffer from then on. Next call (3 s later): Ok |
| Ring-buffer cost | fresh engine, 60 s before vs 60 s after the first call: audio threads (processing + AlsaCapture + AlsaPlayback) **4.65 → 5.16 % of one core**; processing load median **1.12 → 1.16 %** |
| Polling cost (two runs: 60 s phases at 20:05, 25 s phases on a fresh boot at 20:37) | Processing load median does not move: run 1 2.0 / 2.0 / 1.8 % (idle / 5 Hz / 10 Hz), run 2 1.1 / 0.9 / 1.0 %. The work lands in camilladsp's websocket thread (not RT): +0.2–0.4 % at 5 Hz, +0.3–0.8 % at 10 Hz. Client side (Python, JSON): **0.4–0.7 %** at 5 Hz, **0.8–1.3 %** at 10 Hz of one core. Round trip median 1.0–1.9 ms, p99 ≤ 2.4 ms. Whole-box figures from both runs are unusable (another agent's pytest, then boot activity) |
| `SubscribeSpectrum` | max_rate none → 21.3 events/s, 10 → 9.3, 5 → 5.3; camilladsp +1.0–1.1 %. **Answers `Ok` to an out-of-range channel and then sends nothing**, and is cancelled when processing stops. Polling is simpler and heals itself |

### 5.1 Which signal (and what each honestly shows)

| Source | Call | Shows | Good for | Blind to |
|---|---|---|---|---|
| **PGM** (default) | capture, profile `input_map` channels (m4 2, 3; dev 0, 1), two calls, **power**-averaged (`eqgraph.combine_power`) | the music entering K1000, before trim and EQ | **feedback through the program**: K1000 sits after the DJ mixer or console, so any ring from a live mic in that mix runs through K1000 and is in this signal. Also a dominant note. Works muted, independent of master | the room (boom, a sick box). What the cut does to the output: the curve shows that |
| **MIC** | capture ch 0 (M4 input 1, measurement mic, needs **48V**) | room + music + crowd at the mic | room resonances the music excites; rings from systems K1000 does not feed (e.g. stage wedges); "how it sounds here" | it cannot tell room from music: that needs a **transfer function** (mic ÷ program, delay-found, averaged, with coherence). Later, not Sunday |
| **OUT** | playback, the Top L/R channels of `output_map` (m4 0, 1) | what the tops are fed: after EQ, Lo-Cut and master | seeing the notch bite | subs; silent while muted |
| OFF | — | nothing, no polling | kill switch | — |

### 5.2 Parameters and ballistics (build values)

- **Request:** min 10 Hz, max 20 kHz, **265 bins (1/24 oct)**, **10 Hz** polling, on its **own
  websocket**.
  - The control client serialises every command behind one lock, and `set_config_yaml` holds that
    lock until a read-back. The RTA must never queue in front of a mute, nor behind an upload.
  - Polling runs only on SHOW with source ≠ OFF and engine Running.
  - The display maps 20 Hz–20 kHz; the bins below 20 Hz only feed the FFT length.
- **`eqgraph.Rta`:**
  - Instant rise, fall at 24 dB/s (the X32 "decay ~1 s" look).
  - Per-bin peak hold for 3 s, drawn as dots.
  - **Auto range:** loudest hold rounded up to 6 dB, +18 dB headroom, 66 dB deep. The music sits
    under the 0 dB line, and a ring growing out of it reaches the curve.
  - PEAK detector as in §2.4.
  - Pink noise will not read flat (peak-of-band, not energy): calibrate by eye on Friday [?].
- **Failure handling:**
  - `No audio data available` → blank RTA, silently, retried.
  - Channel out of range (a wrong profile) → RTA OFF with a log line once.
  - Websocket down → RTA blank. The control link is independent.

## 6. Panel speed (measured without changing the picture)

Method: read the current frame from `/dev/fb0` and write the same bytes back (`write()` and
`mmap`, 8 runs each). Watch the SPI byte counter (`/sys/bus/spi/devices/spi0.0/statistics`)
to time the transfer that follows.

| | Value [M] |
|---|---|
| SPI clock | 64 MHz requested; **≈62.5 MHz effective** (307 200 B in 39 ms); DMA; the `spi0` pump thread is **SCHED_OTHER**, so it cannot preempt the FIFO audio threads |
| `write()` full frame | syscall 0.31 ms · SPI starts **34 ms** later (fbtft `fps=30` deferred I/O) · done at **73 ms** |
| `write()` rows 124–256 (graph) | syscall 0.14 ms · **131 kB** sent (kernel 6.18 fbtft damages only the written pages, +1 page) · done at **48 ms** |
| `mmap` rows | same as `write()` (128 kB, 51 ms): no need for mmap |
| Ceiling | asked for 30 full frames/s → **18.3** delivered (5.5 MB/s): about 18 fps full-screen, more for row bands |
| Background | k1000 alone, in silence: 184–270 kB/s (≈0.6–0.9 frames/s) |
| Knob to glass | render (7–10 ms) + 0.3 ms + 34 ms + 13–39 ms ≈ **55–85 ms** after the frame starts, plus the render loop's wait (≤ 67 ms at its 15 fps cap). This fills the "not measured" line of doc 08 |

**k1000 side, SHOW at 10 fps, on the box** (300 frames after warm-up; another agent's `dpkg`
purge was running, so read these as upper bounds):

| Step | Median (p95) [M] |
|---|---|
| `Rta.update` | 1.05 ms (1.8) |
| SHOW render, all of it | 7.1 ms (9.1); the graph alone 4.9 ms (6.6) |
| RGB565, `display.to_rgb565`, full frame | 5.8–6.3 ms (8.7) |
| RGB565, **uint8-plane packer**, full frame (bit-identical, checked) | **3.9 ms** (4.8) |
| uint8 packer, graph rows only | **1.8 ms** (2.1) |
| 10 fps loop, render + convert + same-bytes write, 3 × 10 s | process **16.2 %** of one core (full frame) / **14.4 %** (rows 31–254); kernel threads +1.5 % / +0.5 % |

**Conclusion:** 10 fps for the RTA is affordable. With the packer and row writes, expect about
10 % of one core in k1000 and about 1 % in the engine. That leaves three idle cores, and the audio
threads are FIFO. 15 fps is possible, but decay matters more than frame rate [S], so 10 it is.

### 6.1 The packer (for `display.py`, build step 6)

```python
def to_rgb565(img):                       # same bytes as v0's, 40 % faster on the Pi 4
    a = np.asarray(img.convert("RGB") if img.mode != "RGB" else img)
    out = np.empty(a.shape[:2] + (2,), np.uint8)
    np.bitwise_or(a[..., 0] & 0xF8, a[..., 1] >> 5, out=out[..., 1])
    np.bitwise_or((a[..., 1] & 0x1C) << 3, a[..., 2] >> 3, out=out[..., 0])
    return out.tobytes()
```

## 7. GEQ: recommendation

**No master GEQ for Sunday, and not as a PEQ/GEQ toggle.**

Why:
1. **For feedback, 1/3 octave is too wide** [S].
   - A GEQ cut removes a third of an octave of program around the ring. A 1/6–1/10-oct bell
     removes the ring.
   - A low ring needs a notch within a few Hz.
   - Meyer GALAXY, the closest thing to Thomas's system world, has no GEQ at all [S].
2. **Consoles do not toggle one EQ between two views.** PEQ and GEQ are two blocks in series,
   each with its own page, IN/OUT and RTA [S]. A "toggle" would be navigation, and it would hide
   the PEQ notches while the GEQ page is showing.
3. **31 bands on four encoders is slow:** a cursor plus a gain (up to 30 detents to reach a band),
   unless it also gets a jump-to-peak.
4. **Engine [S, untested]:** CamillaDSP's GraphicEqualizer drops 0 dB bands, so its biquad count
   changes as soon as a band leaves 0. Whether that restarts the filter states (a click) is
   **not tested**.
   - Adding the filter to the pipeline is a rebuild, so it would have to exist from boot (fixed
     slot) to keep show-time edits to parameter writes.

**If it ever comes** (`geq_concept.png`):
- A fixed `master_geq` slot (BiquadCombo GraphicEqualizer, 31 bands, 20 Hz–20 kHz, **flat by
  default**) after the live EQ, generated by `dspconfig`.
- Its own page in the B1 cycle, or one enabled from SETUP.
- E2 = band cursor, E3 = band gain (±12, 0.5 dB), B4 tap = jump the cursor to the PEAK band.
- `eqgraph.geq_bands()` already draws it exactly (a test checks the band centres against the
  README).
- **First:** a bench click test of a band leaving 0 dB under program.

## 8. Safety and robustness: what can go wrong live

| Risk | Mitigation |
|---|---|
| Editing the wrong band | The selection is shown three ways; B3 and GRAB change only the selection (and GRAB only an inaudible band); E2–E4 act on the selected band only. The push no longer selects anything on SHOW |
| E1 master grabbed instead of E2 FREQ | One detent = 0.5 dB; master up is never accelerated (doc 08 Patch B); capped at 0 dB; amber highlight. A cross-knob guard was rejected (§2.2) |
| Box left on ALIGN | E1 is still master there; SUB DELAY wakes on its first detent; Lo-Cut needs a hold; back to SHOW after 60 s |
| Lo-Cut off by habit | B3 *tap* on ALIGN = hint only |
| RESET ALL by mistake (B4 is also GRAB) | GRAB = tap; reset = hold, then hold again within 5 s (v0). The first hold only arms it, with a hint |
| EQ OUT bringing a ring back | Not built (§0 #9) |
| A big curve hiding alerts | The graph draws inside its box only (tested); overlays draw after it; MUTED keeps the 0 dB line and every value visible |
| RTA misread | The source is always labelled ("RTA PGM"); PEAK ≠ feedback; peak-of-band dBFS; the bottom octave is coarse (5.9 Hz bins) |
| RTA stalls control | Own websocket; polling stops off SHOW; a GetSpectrum takes 0.3–2 ms anyway |
| CPU vs audio | Audio threads are SCHED_FIFO 10; processing load did not move while polling [M]; the panel's SPI pump is SCHED_OTHER [M]; SHOW 10 fps ≈ 10–16 % of one core [M]. **Thursday's soak runs with SHOW + RTA on** (watch SoC temperature and xruns) |
| Knob sampler vs continuous render (GIL) | Doc 08 measured a p99 wake-up of 0.74 ms while v0 renders. **[?] Re-run `k1000-knobtrace.py bench` with SHOW + RTA at 10 fps.** INTCAP and the ±2 inference absorb short delays |
| Kill switches | `RTA source: OFF` (SETUP), plus a `ui: {rta: false}` key in k1000.yaml for the gig if Friday shows anything odd |
| Phone and panel disagree | One state, adopted both ways (v0). Band *selection* is per surface; values are shared |

### 8.1 Phone (PWA, D-007) parity, for its builder

- **Band names and order:** `SUB · LS · 1 · 2 · 3 · 4 · HS`. Same colours: cut orange, boost
  green, selected amber.
- **The curve math must equal `eqgraph`:** same coefficient formulas, digital, 48 kHz. Reuse
  these test vectors, then the CamillaDSP parity points in `test_eqgraph.py`:

  | Band | Expected |
  |---|---|
  | Peaking 2520 Hz / −6.5 dB / Q 8 | −0.456 · −3.953 · **−6.500** · −5.103 · −0.751 dB at 2000 · 2400 · 2520 · 2600 · 3000 Hz |
  | Highpass 100 Hz / Q 0.707 | −12.305 · −3.012 · −0.264 dB at 50 · 100 · 200 Hz |
  | Lowshelf 120 Hz / +2 dB / Q 0.707 | 1.998 · 1.000 · 0.000 dB at 20 · 120 · 1000 Hz |
- **Front page = SHOW's controls**, with delays and Lo-Cut on a second tab (the same reasoning as
  ALIGN). Mute on every view.
- **GRAB** only with the same rule (free bell, gain untouched).
- **One RTA poller on the box.** If the phone shows an RTA, serve it the box's `Rta` frame rather
  than letting each phone poll the engine.

## 9. Build plan

**Order and files.** Each step comes with its tests; nothing changes the pipeline.

0. **Prerequisite:** doc 08's Patch A (decoder) and Patch B (acceleration). Patch B edits the same
   `KNOBS`/`_accel` lines as step 2, so land it first. The phone's edits to `pages.py`,
   `input_map.py` and `__main__.py` must be merged or sequenced with steps 2, 5 and 6 [?].
1. **`ui/eqgraph.py` + `tests/test_eqgraph.py`:** done (41 tests pass locally, Python 3.14; the
   graph is timed on the box).
2. **`ui/input_map.py`:**
   - `PAGES = ("SHOW", "ALIGN", "SETUP")`.
   - `KNOBS["SHOW"]`: MASTER, `{band}.freq`, `{band}.gain` (balance on SUB), `{band}.q`.
   - `KNOBS["ALIGN"]` = v0 `MAIN`.
   - `Focus.chip`, keeping `slot` as a derived property for old callers.
   - New actions `SelectChip(delta)` and `Grab()`.
   - B1 long → `GotoPage("SHOW")`.
   - B3: SHOW short/long → `SelectChip(±1)`; ALIGN long → `Toggle(lowcut_on)`, short → hint.
   - B4: SHOW short → `Grab()`, long ×2 → `ResetLive` (v0 logic).
   - SETUP item `rta_source` (kind `choice`, no confirm).
   - The 60 s home timer lives in the dispatcher, not in `map_event`.
3. **`state.py`:** `rta_source: str = "PGM"`, plus `CHOICES["rta_source"] = ("PGM", "MIC", "OUT",
   "OFF")`. `from_dict` is tolerant, so old files load; `dspconfig` ignores the field.
4. **New `k1000/rta.py`:**
   - `RtaTask(url, profile, want)`: its own `CamillaClient`; polls at 10 Hz while `want()`
     (SHOW, source ≠ OFF, engine Running).
   - Maps the source to channels from the `Profile`. MIC on `dev` → OFF plus a hint.
   - Power-combines the channels and feeds `eqgraph.Rta`. Exposes `frame()`, `grab_target()` and
     a `changed` event for the render loop.
   - Tests with a fake client: polling stops off SHOW, errors are tolerated, the channel map is
     right, reconnects work.
5. **`control.py`:** `grab(chip, target)`.
   - Picks with `eqgraph.free_band`. Writes `freq` (clamped) and `q = NOTCH_Q` through the normal
     apply path. Never `gain`. Returns the slot.
   - Tests: never a band in use, never a shelf, gain unchanged, refusal when none is free.
6. **`ui/pages.py`:** port `mock_v1.page_show` / `page_align` / `page_setup` (geometry constants as
   they are). `render(..., rta=None)`. New `demo_cases`. Regenerate `docs/ui/`.
   **`ui/display.py`:** the packer (§6.1) and `show(img, rows=None)` for row writes.
7. **`__main__.py`:**
   - Start `RtaTask`.
   - The render loop also wakes on `rta.changed`, capped at 10 fps for the RTA, and keeps 15 fps
     for knob edits.
   - Draw the first frame after a knob event at once (doc 08 §7).
8. **Docs:** `04-operator-card.md` (v1 knob and button table), `03` §4 knob semantics.

**Estimate [G]:**
- Steps 2–7: **6–8 h** of build plus 2 h of review and fixes. Deploy and drill on the box: **1–2
  h**.
- Tuesday is M4 day, so: code on Tuesday (no box needed until the deploy), deploy and drill on
  **Wednesday**, freeze **Wednesday evening**.

**What lands by Wednesday 14 evening:**

| Tier | Content | Why |
|---|---|---|
| **1 (must)** | SHOW with master, the band knobs, chips and the curve (no RTA); ALIGN = MAIN renamed; B1/B3/B4 remap; home after 60 s | Thomas's first two bullets. Mostly moves tested code; the curve is a pure function |
| **2 (should)** | RTA PGM + GRAB + RTA source in SETUP + the packer | Third bullet. Isolated module with a kill switch |
| 3 (Friday) | MIC and OUT sources tried at the rig test; PEAK detector tuned on real program | needs the M4, the mic and the room |
| later | EQ OUT (if ever), GEQ slot, transfer function | §7, §0 #9 |

- **Thursday's overnight soak runs the final UI with SHOW + RTA on.**
- **Friday's rig test** includes: GRAB on a deliberately provoked low-level ring, MIC source with
  the measurement mic, and a pink-noise look.

**Rollback** (`box/README.md` §Roll back):

```sh
sudo rm -rf /tmp/k1000-rollback && sudo cp -a /opt/k1000.prev /tmp/k1000-rollback && sudo /tmp/k1000-rollback/install.sh --start
```

`state.json` stays compatible both ways: v0 ignores `rta_source`.

## 10. Open questions for Thomas (with my recommendation)

1. **Sub level on SHOW (SUB chip), or only on ALIGN?** → **SUB chip on SHOW** (§2.6).
2. **Will a live mic be in the program on Sunday?** If it is DJ only, there is no acoustic loop
   through K1000, and GRAB and the PGM RTA matter less. → **Ship PGM anyway**: it shows the music
   and costs little. On Friday, try MIC with the measurement mic on M4 input 1. This needs 48V on
   input 1, against the taped "48V off" rule: switch it on with the Meyers off or muted, and
   decide whether the tape rule changes.
3. **GRAB on a B4 tap (reset stays hold-hold)?** → **Yes.**
4. **Back to SHOW after 60 s on ALIGN/SETUP?** → **Yes.**
5. **No EQ IN/OUT for Sunday?** → **Agree** (a bypass brings the rings back).

## 11. Not verified / uncertain

- **The PEAK detector on real program.** Thresholds come from synthetic spectra.
- **The RTA on the M4.** Channels are from the profile; no M4 is attached yet. Its 4-channel
  capture side is expected to answer channels 0–3.
- **Whole-box CPU while polling.** Polluted by other agents' jobs (pytest, a dpkg purge, several
  reboots) during the probes. The per-process and per-thread figures are clean.
- **Knob sampler latency with continuous 10 fps rendering:** not measured.
- **GraphicEqualizer click behaviour:** not tested.
- **Visual readability outdoors.** The mockups are judged on a monitor. Glyph heights follow the
  14 px rule for values, but colours (the dark-teal RTA, the amber fill) need a look on the
  ZynScreen in daylight on Wednesday.

## Appendix: probe scripts (on the box in `/home/k1000/dev/ui-v1/`, read-only)

| Script | What it does |
|---|---|
| `probe_rta.py` | GetSpectrum shapes, errors, resolution and subscription rates |
| `probe_load.py` | engine load and CPU at idle / 5 Hz / 10 Hz / subscribed |
| `probe_flag.py` | the sticky ring-buffer cost, on a fresh engine |
| `probe_fb.py` | panel timing with same-bytes writes |
| `probe_show10.py` | SHOW 10 fps CPU with same-bytes writes |
| `probe_565.py` | packer timing |
| `python3 -m k1000.ui.mock_v1 --bench` | the render cost |

Mockups: `cd box && python3 -m k1000.ui.mock_v1 ../docs/ui/v1`.
