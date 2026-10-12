# K1000 — roadmap

D-002 (architecture) is decided: D1, CamillaDSP on the Zynthian V4 (`DECISIONS.md`). Progress is
recorded here as dated lines under each phase.

## Phase 0 — Frame · *done 2026-10-12*

Answer the open questions (`docs/requirements.md` §5), pick the lead(s) (D-002).

- 2026-10-12 — project created; first approaches review: `docs/01-approaches-review.md`
  (proposed lead: CamillaDSP on a Pi, prototyped on the owned Zynthian V4 + a 4-out USB interface).
- 2026-10-12 — Thomas's answers → requirements draft 1; plan with a date: `docs/02-plan-to-sunday.md`
  (test Fri 16 / gig Sun 18 → v0 minimal, stable, bypassable). D-002..D-004 proposed.
- 2026-10-12 — D-002, D-004, D-005, D-006, D-007 and D-008 decided. D-003 (MOTU M4) is still open in
  the log; the M4 is picked up Tue 13.

## Phase 1 — v0 for the 2026-10-18 gig · *in progress* — plan: `docs/02-plan-to-sunday.md`

- Zynthian V4 + class-compliant USB interface (MOTU M4) + CamillaDSP **5.0.0-beta.1** (D-004).
- Fixed-slot pipeline: Lo-Cut, system PEQ, live notch slots, delay, polarity, Aux faders per group.
- Phone control via CamillaGUI compact view; encoders → master / balance / notch freq / notch gain.
- Measure: boot-to-audio time, power-on/off thump into a box, 8-hour soak with xrun logging.
- Exit: audio is trustworthy on the bench, and the control loop feels fast.

Progress:

- 2026-10-12 — Box brought up on Raspberry Pi OS trixie, booting headless (`multi-user.target`,
  PipeWire/WirePlumber masked): ZynScreen overlay, MCP23017 knob driver on gpiod v2 interrupts,
  CamillaDSP 5.0.0-beta.1, CamillaGUI v5 beta.2. Build contract: `docs/03-v0-architecture.md`.
- 2026-10-12 — v0 control service deployed (`e3a3deb`): engine client, DSP config, knobs, screen,
  controller, systemd units, install.sh, soak tool. Measured after a reboot, `dev` profile: systemd
  userspace 15.0 s, k1000 on the engine at 11.7 s, ramp −40 → −20 dB in ~3 s (boot-to-audio ≈ 15 s),
  engine load ~1.1 %, k1000 ~12 % of a core, SoC 49.6 °C, throttled 0x0.
- 2026-10-12 — Review round on the deployed box (audio safety, operator UX, ops, phone/GUI, M4
  readiness) with drills, then two fix rounds, deployed the same evening: knobs and screen retried for
  life + KNOBS OFFLINE; MUTED band tells the truth (alerts, RAMPS IN BY ITSELF, READY only when
  Running); B4 reset LIVE-only with a second press, B3 MAIN-only, E3 sub delay wakes first; torn state
  comes back muted; mute and master-down saved at once; GUI Apply onto another card repaired; IN CLIP
  on the M4 input; per-profile `target_level`; render CPU 10.1 % → 1.6 % in-process; the engine unit
  waits for the M4 instead of restarting every second; log rotation 256 kB × 50; persistent journal;
  soak `--summary`; M4 bring-up tool. Combined tree on the box: 502 passed, 1 skipped (no shellcheck),
  0 failed. Re-accepted live: kill -9 of either process, the m4 pin without the M4 (3 engine starts in
  100 s instead of 56 in 67 s), a clean reboot to full level 15.5 s after kernel start, k1000 idle CPU
  ~5 % (was ~14–19 %).
- 2026-10-12 — Docs: operator card `docs/04-operator-card.md` (one printed A4 page), M4 procedure
  `docs/05-m4-day.md`, contract corrected to the build (`docs/03-v0-architecture.md` §7).

### What v0 has now (in the repo; on the box once the fix round is deployed)

| Area | State |
|---|---|
| Pipeline | input trim, 8 system PEQ, 4 live PEQ + 2 shelves, split (mono-subs option), sub/top polarity, delay (whole samples), top Lo-Cut, sub low-pass, Aux group faders (balance), 4 output trims, Main master + mute |
| Knobs and screen | MAIN / LIVE EQ / SETUP / Knob test pages; B2 mute on every page; guarded B4 reset; meters with CLIP / IN CLIP; MUTED band with alerts; NO DEVICE / ENGINE DOWN |
| Audio safety | boots muted; ramp gated on Running; Aux4 marker; outside cuts kept, raises pulled down; pipeline restore and device repair; mute saved at once; torn state recovered muted |
| Ops | restart-forever units, device wait, watchdog, persistent journal (from the next reboot), soak tool with `--summary`, M4 bring-up tool, rollback |
| Phone | CamillaGUI compact view, levels only (README phone rule). No network for the phone at a gig yet |

### Left before Friday 16 (rig test)

| # | What | When |
|---|---|---|
| 1 | ~~Deploy the fix round; full suite; new start script; persistent journal cost~~ — done 2026-10-12 evening (502 passed; journal flush off the critical chain) | ~~before Tue 13~~ |
| 2 | M4 day, `docs/05-m4-day.md`: detect, check tool, tone identification, profile `m4`, loop and leak tests, levels, presence drills, 1 h soak, report back | Tue 13 |
| 3 | Knob test → `controls:` in k1000.yaml (and `KNOB_CORNERS` if the labels sit at the wrong corners); label the shell; fill in "Where" on the operator card | Tue–Thu |
| 4 | Thomas's calls: output mapping (keep tops on outs 1-2), M4 `target_level` (256 vs 512/768), power-on mute policy (today: the saved mute), B1 page cycle, sub delay on MAIN, Sunday source and cables, PSU | by Thu 15 |
| 5 | Pin the M4 channel counts (4/4) in k1000.yaml after the probe; set the chosen `target_level` (next engine start) | Tue–Thu |
| 6 | Overnight soak at the gig `target_level`; `--summary` clean | Thu 15 night |
| 7 | Boot race: fbcon vs `k1000.service` ExecStartPre (cloud-init stays on until settled) | before Fri |
| 8 | ~~Owner edits: `docs/02` §3/§6, D-003 wording, README "Sound state"~~ — done 2026-10-12 | ~~before Fri~~ |
| 9 | D-007 phone AP + PWA: not built; go/no-go at the rig test, else the knobs carry the show | Fri 16 |
| 10 | Print the operator card; pack the bypass kit, labelled, its patch written on the card | Sat 17 |

Friday itself is `docs/02` §6: thump check, gain structure (Meyer limit LEDs, then the phone's
`volume_max` = show level + 6 dB), sub alignment, Lo-Cut A/B, system EQ, live drill, 2-hour run.

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
- python3-smbus → python3-smbus2 (D-008); a box gesture to release CamillaGUI Aux cuts.
