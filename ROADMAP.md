# K1000 — roadmap

Phases are proposals until D-002 (architecture) is decided; see `DECISIONS.md`. Progress is
recorded here as dated lines under each phase.

## Phase 0 — Frame · *in progress*

Answer the open questions (`docs/requirements.md` §5), pick the lead(s) (D-002).

- 2026-10-12 — project created; first approaches review: `docs/01-approaches-review.md`
  (proposed lead: CamillaDSP on a Pi, prototyped on the owned Zynthian V4 + a 4-out USB interface).
- 2026-10-12 — Thomas's answers → requirements draft 1; plan with a date: `docs/02-plan-to-sunday.md`
  (test Fri 16 / gig Sun 18 → v0 minimal, stable, bypassable). D-002..D-004 proposed.

## Phase 1 — v0 for the 2026-10-18 gig *(if D is chosen)* — plan: `docs/02-plan-to-sunday.md`

- Zynthian V4 (or any Pi 4/5) + class-compliant USB interface + CamillaDSP (v4.1.3, eye on v5).
- Fixed-slot pipeline: Lo-Cut, system PEQ, live notch slots, delay, polarity, Aux faders per group.
- Phone control via CamillaGUI compact view; encoders → master / balance / notch freq / notch gain.
- Measure: boot-to-audio time, power-on/off thump into a box, 8-hour soak with xrun logging.
- Exit: audio is trustworthy on the bench, and the control loop feels fast.

## Phase 2 — Live-correction UI v1

- K1000 control service (state, presets, System/Live EQ layers, macros) over the engine API.
- Encoder pages on the device screen + a dedicated phone web UI served on the box's own AP.
- Presets per place type, A/B, "reset live".

## Phase 3 — Field trial

- One real outing on the rig; log what was touched, what was missing, whether Wi‑Fi held.
- Decide the final hardware shape (Zynthian shell kept / Pi 5 + Stream Deck+ / Pi 5 + DAC8x).

## Phase 4 — Assist & measurement

- Spectrum view (CamillaDSP v5) with peak markers; tap-to-notch; supervised suggest-notch.
- Noise generator + RTA on the measurement mic; later a transfer-function view; REW import/export.

## Phase 5 — Road-proofing

- Read-only root, watchdog/supervisor, mute-at-boot (+ optional output mute relay), enclosure,
  power, labelling, a spare surface.
