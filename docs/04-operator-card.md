# K1000 v1 — operator card (gig)

UI v1 (D-009), accepted on the box 2026-10-13 (`f7aa58b-v1`); ops `box/README.md`. **Not v0 any more:** E1 = MASTER on SHOW too · balance on ALIGN only · B3 = next band · B4 = notch helper · reset and Lo-Cut on/off in SETUP · ALIGN's E3/E4 locked.

**Where** (SETUP › Knob test; screen: E1/E3 top, E2/E4 bottom): E1 ____ E2 ____ E3 ____ E4 ____ · B1 ____ B2 ____ B3 ____ B4 ____ · labels PAGE · MUTE · BAND/UNLOCK · NOTCH. One click = one step; only FREQ, Lo-Cut, master *down* accelerate.

| Knob | SHOW (front page) | ALIGN | SETUP |
|---|---|---|---|
| E1 | **MASTER** 0.5 dB; up ≤ +6 dB per 2 s (`SLOW DOWN`) | **MASTER**, the same | item |
| E2 | **FREQ** of the selected band, 1/12 oct | **BALANCE** 0.5 dB, + = subs up | value |
| E3 | **GAIN** of the selected band, 0.5 dB | sub delay 0.1 ms, **locked** | — |
| E4 | **Q** 1/6 oct (shelves: slope) | Lo-Cut freq (tops) 1/12 oct, **locked** | — |
| push | held + turn = fine (FREQ 1/48 oct, Q 1/24) | fine (1 sample, 1/48 oct) | select |

| Button | Does |
|---|---|
| B1 | tap: next page SHOW → ALIGN → SETUP · **hold: SHOW** · ALIGN, SETUP go home after 60 s idle |
| B2 | **hold 0.8 s: MUTE / UNMUTE**, every page, 0.1 s fade (tap: hint only) |
| B3 | SHOW tap: **next band** LS · 1 · 2 · 3 · 4 · HS · ALIGN **hold: unlock E3/E4** for 30 s idle |
| B4 | SHOW tap: **notch helper** · elsewhere a hint |

**Notch in 3 gestures (SHOW): B4, E3 down, E4 up.** (1) **B4** sets a free bell (P1–P4 at 0 dB, the selected one first) to Q 8.65 (1/6 oct) and, if the cyan PEAK pointer is up, to its frequency; gain stays 0 dB (inaudible); strip `P3 · 2.44k · E3 TO CUT`. (2) **E3 down**: 12 clicks = −6 dB. (3) **E4 up** narrows (6 clicks = Q 16, max); E2 with its push held centres it. A PEAK within 1/6 oct of a bell already cutting re-selects it, writing nothing. `NO PEAK`: sweep with E2 (250 Hz → 2.5 kHz ≈ 40 clicks). `NO FREE BAND`: four bells in use. PEAK is read before the EQ; it is not a feedback detector, and never on silence.

**ALIGN lock.** E3/E4 turned while locked: `LOCKED · HOLD B3 TO EDIT`, nothing changes. Unlocked (`EDIT 0:30`; a B3 tap relocks), the first E3 click after 2.5 s at rest only wakes sub delay; each change dips the subs briefly.

**SETUP** (×2 = push twice within 5 s): Input trim · Sub/Top polarity · Top delay · Sub low-pass + freq · 4 trims · Mono subs ×2 · Audition ×2 · Profile ×2 · **Reset live EQ ×2** · **Undo reset** (`UNDO 4:58`; one level, until the next audible live-EQ edit or 5 min) · **Lo-Cut** on/off ×2 · **Spectrum** ON/OFF · **Laptop access ×2** (GUI + SSH over the K1000 Wi-Fi, 2 h, pill `LAPTOP OPEN`; one push closes) · Phone · Knob test.

**Emergency.** Silence: **hold B2**. Feedback: **B1 hold, B4, E3 down**. KNOBS OFFLINE (red, full width; B2 dead): phone mute if proven Friday, else source down, bypass. Any doubt: bypass.

| You see | Means → do |
|---|---|
| ● green RUNNING · yellow STARTING / PAUSED | processing · not yet, or silent input → source, M4 |
| MUTED band `READY · hold B2 to unmute` | muted (by you, or saved) → hold B2 when ready |
| `RAMPS IN BY ITSELF` (blue RAMPING IN) | engine restarted, ~3 s fade → nothing |
| `NO AUDIO FLOWING · HELD MUTED` | no audio from the M4 → USB, power |
| **Status bar** red · orange · blue · **amber strip** | **alert**: act · warning · a state · **hint** (your press) |
| SHOW pills: orange · grey · `+N` | LO-CUT OFF, POL INV, LAPTOP OPEN · MONO SUB · more |
| STATE REBUILT / NO SAVED STATE | muted, master −20 → check master, balance; hold B2 |
| NO DEVICE (orange) · ENGINE DOWN (red) | reseat M4 · back muted in ~5 s; 30 s → bypass |
| CLIP · IN CLIP pill · meter red (> −6 dBFS) | master down · source down; Meyers limit ≈ −14 |
| Orange `… (GUI)`, `… CHANGED (GUI)` | CamillaGUI: cuts kept, rest restored → no Applies |
| NO SPECTRUM · ENGINE ? · DEVICE ? · UI ERROR | engine refuses it: sound fine, Spectrum OFF · unsure → wait |

**Power on:** Meyers OFF, source quiet → M4 on the Pi's USB, rear switch on → K1000 on; ~15 s: ● RUNNING, M4 · MOTU M4, MUTED · READY (saved unmuted: ramps in) → Meyers ON → hold B2, E1 up. **Off:** hold B2 (saved at once) → Meyers OFF → unplug K1000, M4. **M4 taped:** MONITOR, INPUT MONITOR MIX full right · MONs unlit · 48V off · GAIN 1/2 min.

**Bypass (~1 min):** (1) source **down**, hold B2 if K1000 answers (2) unplug OUT1 TOP L · OUT2 TOP R · OUT3 SUB L · OUT4 SUB R at the Meyers (3) bypass kit: Source L → ______ · Source R → ______ (4) source up slowly: ≈ (2 − master) dB louder, +12 dB at master −10 (check Friday). Lost: Lo-Cut, delay, EQ, mute.
