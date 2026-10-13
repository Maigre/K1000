# K1000 v1 — operator card (gig)

UI v1 (D-009), ops `box/README.md`. **Not v0:** E1 = MASTER on SHOW · balance on ALIGN · B3 band · B4 notch · reset in SETUP.

**Where**: knobs E1 top-left · E2 bottom-left · E3 top-right · E4 bottom-right · buttons (Knob test) B1 ____ B2 ____ B3 ____ B4 ____, labels PAGE · MUTE · BAND/UNLOCK · NOTCH. One click = one step; only FREQ, Lo-Cut, master *down* accelerate.

| Knob | SHOW (front page) | ALIGN | SETUP |
|---|---|---|---|
| E1 | **MASTER** 0.5 dB; up ≤ +6 dB per 2 s (`SLOW DOWN`) | **MASTER**, the same | item |
| E2 | **FREQ** of the selected band, 1/12 oct | **BALANCE** 0.5 dB, + = subs up | value |
| E3 | **GAIN** of the selected band, 0.5 dB | **DELAY** subs or tops, 0.1 ms, **locked** | — |
| E4 | **Q** 1/6 oct (shelves: slope) | Lo-Cut freq (tops) **2 Hz**, **locked** | — |
| push | held + turn = fine (FREQ 1/48 oct, Q 1/24) | fine (1 sample, 1 Hz) | select |

| Button | Does |
|---|---|
| B1 | tap: next page · **hold: SHOW** (Knob test: hold twice; PHONE: tap) · ALIGN, SETUP go home after 60 s |
| B2 | **hold 0.8 s: MUTE / UNMUTE**, every page, 0.1 s fade (tap: hint only) |
| B3 | SHOW tap: **next band** LS · 1 · 2 · 3 · 4 · HS · ALIGN **hold: unlock E3/E4** for 30 s idle |
| B4 | SHOW tap: **notch helper** · elsewhere a hint |

**Notch in 3 gestures (SHOW): B4, E3 down, E4 up.** (1) **B4** sets a free bell (P1–P4 at 0 dB, the selected one first) to Q 8.65 (1/6 oct) and, if the cyan PEAK pointer is up, to its frequency; gain stays 0 dB (inaudible); strip `P3 · 2.44k · E3 TO CUT`. (2) **E3 down**: 12 clicks = −6 dB. (3) **E4 up** narrows (6 clicks = Q 16, max); E2 with its push held centres it. `NO PEAK`: **E3 down first** (≈ −6 dB; at 0 dB the sweep is silent), then sweep E2 until the ring drops (250 Hz → 2.5 kHz ≈ 40 clicks), then E4 up. `NO FREE BAND`: four bells in use. No PEAK on silence.

**ALIGN lock.** E3/E4 turned while locked: `LOCKED · HOLD B3 TO EDIT`. Unlocked (`EDIT 0:30`; a B3 tap relocks), the first E3 click after 2.5 s at rest only wakes DELAY; each change dips the group whose delay changes (both when crossing `ALIGNED` fast). **DELAY**: clockwise `SUBS LATER` (subs in front), anticlockwise `TOPS LATER` (tops in front); one click on `ALIGNED` before crossing.

**SETUP** (×2 = push twice within 5 s): Input trim · Sub/Top polarity · Sub low-pass + freq · 4 trims · Mono subs ×2 · Audition ×2 · Profile ×2 · **Reset live EQ ×2** · **Undo reset** (5 min) · **Lo-Cut** on/off ×2 · **Spectrum** ON/OFF · **Laptop access ×2** (GUI, SSH, `/tune` on the Wi-Fi, 2 h) · Phone · Knob test.

**Emergency.** Silence: **hold B2**. Feedback, on SHOW: **B4, E3 down, E4 up** (no B1); from another page: hold B1, wait for the cyan PEAK (~2 s), then B4. KNOBS OFFLINE (B2 dead): phone mute if proven, else source down, bypass. Any doubt: bypass.

| You see | Means → do |
|---|---|
| ● green RUNNING · yellow STARTING | processing · not yet → M4 (USB, power) |
| RUNNING, meters still, no sound | nothing comes in → source playing? into rear LINE IN 3/4? |
| MUTED band `READY · hold B2 to unmute` | muted (by you, or saved) → hold B2 when ready |
| `RAMPS IN BY ITSELF` (blue RAMPING IN) | engine (re)started, ~3 s fade → nothing |
| `NO AUDIO FLOWING · HELD MUTED` | no audio from the M4 → USB, power |
| **Status bar** red · orange · blue · **amber strip** | **alert**: act · warning · a state · **hint** (your press) |
| SHOW pills: orange · grey · `+N` | LO-CUT OFF, POL INV, LAPTOP OPEN · MONO SUB · more |
| STATE REBUILT / NO SAVED STATE | muted, master −20 → check master, balance; hold B2 |
| NO DEVICE (orange) | M4 gone → reseat USB, rear switch · 30 s → bypass |
| ENGINE DOWN (red) | back in ~5 s · `no input data`: M4 USB, M4 off-on · 30 s → bypass |
| CLIP · IN CLIP pill · meter red (> −6 dBFS) | master down · source down; Meyers limit ≈ −14 |
| Orange `… (GUI)`, `… CHANGED (GUI)` | CamillaGUI: cuts kept, rest restored → no Applies |
| NO SPECTRUM · ENGINE ? · DEVICE ? · UI ERROR | engine refuses it: sound fine, Spectrum OFF · unsure → wait |

**Power on:** Meyers OFF, source quiet → M4 on the Pi's USB, rear switch on → K1000 on; ~15 s: ● RUNNING, M4 · MOTU M4, **MUTED · READY** → Meyers ON → hold B2 → E1 up. No MUTED band (`RAMPS IN BY ITSELF`): live at its saved level → Meyers ON with the source down, **no B2** (it mutes). **Off:** hold B2 (saved at once) → Meyers OFF → unplug K1000, M4. **M4 taped:** MONITOR, INPUT MONITOR MIX full right · MONs unlit · 48V off · GAIN 1/2 min.

**Bypass (~1 min):** (1) source **down**, hold B2 if K1000 answers (2) unplug OUT1 TOP L · OUT2 TOP R · OUT3 SUBS at the Meyers (3) bypass kit: Source L → ______ · Source R → ______ (4) source up slowly: ≈ (2 − master) dB louder, +12 dB at master −10 (check Friday). Lost: Lo-Cut, delay, EQ, mute.
