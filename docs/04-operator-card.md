# K1000 v0 — operator card (gig)

From the 2026-10-12 fix-round code, deployed on the box the same evening. Ops: `box/README.md` · M4: `docs/05-m4-day.md`.

**Positions are ASSUMED** until SETUP › Knob test: the screen draws E1 top-left, E2 bottom-left, E3 top-right, E4 bottom-right; B1–B4 = sw4–sw7, place unknown; turn directions too. Until `controls:` is confirmed, **hold-B2 (mute) and hold-B4 (reset live EQ) may be swapped**. Then fill in "Where" and label PAGE · MUTE (hold) · LO-CUT · RESET (hold).

| Knob | Where | MAIN | LIVE EQ | SETUP |
|---|---|---|---|---|
| E1 | ____ | Master, 0.5 dB (−80…0) | Slot: P1–P4, LS, HS | Item |
| E2 | ____ | Balance, 0.5 dB, + = subs up | Freq, 1/12 oct | Value |
| E3 | ____ | Sub delay, 0.1 ms; after 2.5 s idle, 1st detent only wakes it | Gain, 0.5 dB | — |
| E4 | ____ | Lo-Cut, tops, 1/12 oct (20–300 Hz) | Q / shelf slope | — |
| Push | | hold + turn = fine (1 sample, 1/48 oct) | hold + turn = fine | push = select (Mono subs, Audition, Profile: twice) |

| Button | Where | Does |
|---|---|---|
| B1 press | ____ | Next page: MAIN → LIVE EQ → SETUP |
| B2 hold 1 s | ____ | **MUTE / UNMUTE**, every page (short press: hint only). Unmute fades in 0.3 s to the master shown |
| B3 press | ____ | Lo-Cut on/off, MAIN only |
| B4 hold 1 s, twice in 5 s | ____ | **RESET ALL LIVE EQ**, LIVE EQ only, no undo |

**Emergency.** Silence: hold B2. Feedback: LIVE EQ, E1 a free slot (outlined box), E2 onto the ring, E3 cut, E4 Q up. KNOBS OFFLINE (B2 dead too): phone mute if proven Friday, else source down, bypass. Any doubt: bypass.

| You see | Means → do |
|---|---|
| ● green RUNNING · yellow STARTING / PAUSED | processing · not yet: wait, then check source and M4 |
| Red MUTED band: READY · hold B2 to unmute | your (or saved) mute → hold B2 when ready |
| … RAMPS IN BY ITSELF · hold B2 to keep muted | engine restarted, fades in over ~3 s → nothing |
| … NO AUDIO FLOWING · HELD MUTED | no audio from the M4 yet → check USB / power |
| … SYNC FAILED / CANNOT SAVE STATE / SETTINGS | K1000 fault, audio left as is → bypass only if the sound is wrong |
| Hint STATE REBUILT / NO SAVED STATE | state.json lost: muted, master −20 → check E1/E2, hold B2 |
| Orange NO DEVICE | M4 gone, no audio → reseat USB / power, it ramps back. 30 s → bypass |
| Dark red ENGINE DOWN | engine stopped → ~5 s: back muted, ramps in. Persists → bypass |
| Red CLIP · IN CLIP pill | output full scale → master down · M4 input → source down |
| Meter green / yellow / red | < −18 / to −6 / > −6 dBFS; Meyers limit ≈ −14 (research) |
| Strip "… OUTSIDE K1000" | a CamillaGUI cut/mute, kept → undo it in the GUI |
| Strip PIPELINE / DEVICES CHANGED, RESTORING | a GUI Apply, K1000 restores or leaves it → stop GUI Applies |
| Amber ENGINE ? / DEVICE ? or UI ERROR | the screen is unsure → wait; audio never depends on it |

**Power on:** Meyers OFF, source quiet → M4 on the Pi's USB, rear switch on → K1000 on; in 15–20 s ● RUNNING, "M4 · MOTU M4" → MUTED · READY (or, if saved unmuted, it ramps in by itself) → Meyers ON → hold B2, master up.

**Power off:** hold B2 → MUTED (saved at once: next boot comes up muted) → Meyers OFF → unplug K1000 and M4. Untested with the Meyers on (thump check Friday): keep this order.

**M4 panel, taped:** MONITOR and INPUT MONITOR MIX fully clockwise · MON 1, MON 2, 3-4 unlit (lit = program to the tops past K1000 and its MUTE) · 48V off · GAIN 1/2 fully counter-clockwise.

**Bypass (~1 min):** (1) source **down**; hold B2 if K1000 answers. (2) Unplug OUT1 TOP L · OUT2 TOP R · OUT3 SUB L · OUT4 SUB R at the Meyers. (3) Bypass kit as cabled before K1000: Source L → __________ · Source R → __________. (4) Source up slowly: bypass is ≈ (2 − master) dB louder, +12 dB at master −10 (research, check Friday). Lost: Lo-Cut, delay, EQ, mute.
