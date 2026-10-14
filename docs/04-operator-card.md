# K1000 v1 — operator card (gig)

UI v1 (D-009, D-013, D-015), ops `box/README.md`. **New:** SHOW E3 Q · E4 GAIN · ALIGN E2 DELAY · E3 BALANCE · IN meters.

**Where**: knobs E1 top-left · E2 bottom-left · E3 top-right · E4 bottom-right · buttons (Knob test) B1 ____ B2 ____ B3 ____ B4 ____, labels PAGE · BAND/UNLOCK · NOTCH · MUTE. One click = one step; only FREQ, Lo-Cut, master *down* accelerate.

| Knob | SHOW (front page) | ALIGN | SETUP |
|---|---|---|---|
| E1 | **MASTER** 0.5 dB; up ≤ +6 dB per 2 s (`SLOW DOWN`) | **MASTER**, the same | item |
| E2 | **FREQ** of the selected band, 1/12 oct | **DELAY** subs or tops, 0.1 ms, **locked** | value |
| E3 | **Q** 1/6 oct (shelves: slope) | **BALANCE** 0.5 dB, + = subs up | group |
| E4 | **GAIN** of the selected band, 0.5 dB | Lo-Cut freq (tops) **2 Hz**, **locked** | — |
| push | held + turn = fine (FREQ 1/48 oct, Q 1/24) | fine (1 sample, 1 Hz) | set · run · open |

| Button | Does |
|---|---|
| B1 | tap: **SHOW ↔ ALIGN** (SETUP → SHOW) · **hold: SETUP** · Knob test: hold, tap · SHOW by itself after 60 s |
| B2 | SHOW tap: **next band** LS · 1 · 2 · 3 · 4 · HS · ALIGN **hold: unlock E2/E4** for 30 s idle, tap: relock |
| B3 | SHOW tap: **notch helper** · **hold: band OUT/IN** (Knob test first; pill `OUT P2`) · elsewhere a hint |
| B4 | **hold 0.8 s: MUTE / UNMUTE**, every page, 0.1 s fade (tap: hint only) |

**Notch in 3 gestures (SHOW): B3, E4 down, E3 up.** (1) **B3** sets a free bell (P1–P4 at 0 dB, the selected one first) to Q 8.65 (1/6 oct) and, if the cyan PEAK pointer is up, to its frequency; gain stays 0 dB (inaudible); strip `P3 · 2.44k · E4 TO CUT`. (2) **E4 down**: 12 clicks = −6 dB. (3) **E3 up** narrows (6 clicks = Q 16, max); E2 with its push held centres it. `NO PEAK`: **E4 down first** (≈ −6 dB; at 0 dB the sweep is silent), then sweep E2 until the ring drops (250 Hz → 2.5 kHz ≈ 40 clicks), then E3 up. `NO FREE BAND`: four bells in use. No PEAK on silence.

**ALIGN lock.** E2/E4 turned while locked: `LOCKED · HOLD B2 TO EDIT`. Unlocked (`EDIT 0:30`; a B2 tap relocks), the first E2 click after 2.5 s at rest only wakes DELAY; each change dips the group whose delay changes (both when crossing `ALIGNED` fast). **DELAY**: clockwise `SUBS LATER` (subs in front), anticlockwise `TOPS LATER` (tops in front); one click on `ALIGNED` before crossing.

**SETUP** (E3 group, E1 item): TOPS Trim L/R · Polarity · **Lo-Cut** | SUBS Trim L/R · Polarity · Mono L+R · Low-pass + freq | INPUT Trim | LIVE EQ **Reset ×2** · **Undo** (5 min) · **Spectrum** | PHONE Phone · **Laptop access ×2** (GUI, SSH, `/tune`, 2 h) | BOX Profile · Audition · Knob test. **On/off, choices: E2 chooses (red `now → chosen`), push sets.** ×2 = twice in 5 s.

**Emergency.** Silence: **hold B4**. Feedback, on SHOW: **B3, E4 down, E3 up** (no B1); from ALIGN or SETUP: tap B1, wait for the cyan PEAK (~2 s), then B3. KNOBS OFFLINE (B4 dead): phone mute if proven, else source down, bypass. Any doubt: bypass.

| You see | Means → do |
|---|---|
| ● green RUNNING · yellow STARTING | processing · not yet → M4 (USB, power) |
| RUNNING, IN meters still, no sound | nothing comes in → source playing? into rear LINE IN 3/4? |
| MUTED band `READY · hold B4 to unmute` | muted (by you, or saved) → hold B4 when ready |
| `RAMPS IN BY ITSELF` (blue RAMPING IN) | engine (re)started, ~3 s fade → nothing |
| `NO AUDIO FLOWING · HELD MUTED` | no audio from the M4 → USB, power |
| **Status bar** red · orange · blue · **amber strip** | **alert**: act · warning · a state · **hint** (your press) |
| SHOW pills: orange · grey · `+N` | LO-CUT OFF, POL INV, LAPTOP OPEN · MONO SUB · more |
| STATE REBUILT / NO SAVED STATE | muted, master −20 → check master, balance; hold B4 |
| NO DEVICE (orange) | M4 gone → reseat USB, rear switch · 30 s → bypass |
| ENGINE DOWN (red) | back in ~5 s · `no input data`: M4 USB, M4 off-on · 30 s → bypass |
| CLIP · IN CLIP pill · meter red (> −6 dBFS) | master down · source down; Meyers limit ≈ −14 |
| Orange `… (GUI)`, `… CHANGED (GUI)` | CamillaGUI: cuts kept, rest restored → no Applies |
| NO SPECTRUM · ENGINE ? · DEVICE ? · UI ERROR | engine refuses it: sound fine, Spectrum OFF · unsure → wait |

**Power on:** Meyers OFF, source quiet → M4 on the Pi's USB, rear switch on → K1000 on; ~15 s: ● RUNNING, M4 · MOTU M4, **MUTED · READY** → Meyers ON → hold B4 → E1 up. No MUTED band (`RAMPS IN BY ITSELF`): live at its saved level → Meyers ON with the source down, **no B4** (it mutes). **Off:** hold B4 (saved at once) → Meyers OFF → unplug K1000, M4. **M4 taped:** MONITOR, INPUT MONITOR MIX full right · MONs unlit · 48V off · GAIN 1/2 min.

**Bypass (~1 min):** (1) source **down**, hold B4 if K1000 answers (2) unplug OUT1 TOP L · OUT2 TOP R · OUT3 SUBS at the Meyers (3) bypass kit: Source L → ______ · Source R → ______ (4) source up slowly: ≈ (2 − master) dB louder, +12 dB at master −10 (check Friday). Lost: Lo-Cut, delay, EQ, mute.
