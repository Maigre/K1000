# 02 — Plan to Sunday: K1000 v0 (2026-10-12)

Thomas's answers (`requirements.md` §5) tighten `01-approaches-review.md` into a plan with a date:
**test Friday 2026-10-16, gig Sunday 2026-10-18**.

## 1. What the answers change

| Answer | Consequence |
|---|---|
| USW-1P confirmed | no low-pass on the subs; v0 processing = level, polarity, delay, top Lo-Cut, PEQ |
| 4 outputs is enough | a 4-out interface (MOTU M4 class); the DAC8x / UltraLite routes are parked |
| no rack, compact | the 19″ boxes drop out (DriveRack PA2, DCX2496, DSP 408) |
| knobs first | the Zynthian's 4 encoders + 4 buttons become a real asset, not a convenience |
| DIY welcome, ready-made if it fits | compact ready-mades (t.racks Mini, miniDSP Flex) have **no live-EQ controls on the box**; the only compact ready-made with knobs + live EQ + Android is a mixer: **A&H CQ-18T** (~€959, desktop, touchscreen; Feedback Assistant; output delay and 12 dB/oct HP/LP). That's the ready-made fallback. |
| robust + Meyer-grade audio, compact budget | spend on the interface (metal, low-noise DAC), not on features |
| Sunday | v0 must be **minimal, stable and bypassable**; hardening comes after |

## 2. Proposed decisions (see `DECISIONS.md`)

- **D-002 — Architecture: D1.** Zynthian V4 (owned) + USB interface + CamillaDSP. *Decided 2026-10-12.*
- **D-005 — Base OS: fresh Raspberry Pi OS Lite 64-bit** on a new card. *Decided 2026-10-12.*
- **D-003 — Interface: MOTU M4** (€289).
  - For: bus-powered (no PSU, one cable less), metal case, ESS DAC, LCD meters for every input and
    output (instant visual check on the road), the Linux quirk has been in the kernel since 5.6,
    and it takes 2 line inputs + a mic input with phantom at once.
  - Caveats: its monitor knob scales outs 1-2, so set it full and cap/tape it, then calibrate with it
    there. The monitor-mix / input-monitor must stay off, or raw input leaks into outs 1-2 past the
    DSP.
  - Alternative if the M4 can't arrive in time: Scarlett 4i4 Gen 4 (needs its PSU on a Pi 4).
- **D-004 — Engine: CamillaDSP v5 beta** (latest beta at install, frozen before Friday). *Decided
  2026-10-12 by Thomas* over the v4.1.3 recommendation: no migration later, spectrum available early;
  the soak test and the bypass kit carry the beta risk.

## 3. v0 scope

### Does

**Pipeline (fixed slots; every show-time action is a parameter write or a ramped fader):**

```
in L/R ─ trim ─ System PEQ (8 bands) ─ Live EQ (4 peaking/notch @0 dB + low shelf + high shelf) ─┬─ Tops: Lo-Cut HP 12 dB/oct ─ gain/polarity ─ delay ─ Aux "tops" ─ Main ─► out Top L/R
                                                                                                └─ Subs: (mono-sum opt.) ─ gain/polarity ─ delay ─ Aux "subs" ─ Main ─► out Sub L/R
```

- Lo-Cut "off" means moving its frequency to 10 Hz (a parameter write), not removing it (a
  pipeline rebuild clicks). Polarity is a Gain filter's `inverted` flag (also a parameter write).
- Sub↔top balance is one macro moving the two Aux faders in opposite directions.

**Knobs (Zynthian):**

| Page | E1 | E2 | E3 | E4 |
|---|---|---|---|---|
| MAIN | master | sub↔top balance | sub delay | top Lo-Cut freq |
| LIVE EQ | slot select | frequency (push = fine) | gain | Q |

Buttons:
- B1 next page
- B2 mute (long press)
- B3 Lo-Cut on/off
- B4 reset live EQ (long press)

The screen shows the current page in large type, plus mute/clip state.

**Phone:** CamillaGUI's compact view in the Android browser for levels. The full editor runs on a
laptop for the system EQ at the tune.

**Safety:**
- Boots **muted** and ramps to the last saved levels.
- State is saved on every change, to a writable location.
- systemd restarts CamillaDSP and the control service if either dies.

**Audio devices: two profiles, one pipeline.** Development starts on the Zynthian's own HiFiBerry
board (2 in / 2 out) while the MOTU M4 is being bought, then moves to the M4 without touching the
processing:

| Profile | Device (by ALSA card name, never number) | Output mapping |
|---|---|---|
| `dev` | Zynthian HiFiBerry board (exact board/overlay checked on the box) | 4 logical outs → 2 physical: each side = top + sub summed (full-range audition), or a dev switch to hear tops or subs alone |
| `m4` | MOTU M4 | 4 → 4: Sub L/R, Top L/R |

- Everything up to the logical outputs is identical, so the control service doesn't know which
  profile runs. Only the device block and the last mixer differ.
- Switching profiles is a device change, so CamillaDSP does a full stop/start (a second of silence).
  That's fine on the bench, never during a show.
- **Gig rule: the `m4` profile is pinned.** If the M4 is missing at boot, the screen says so. There
  is no silent fallback to the HiFiBerry, which isn't wired to the speakers.
- The two boards are never used together (one for capture, the other for playback): two clocks would
  need resampling and would drift.

### Doesn't (after Sunday)

Spectrum and suggest-notch (v5), the box's own Wi‑Fi AP, a read-only root, an output mute relay, a
presets UI, and a final enclosure.

## 4. Schedule (rev 2 — after Thomas's answers of 2026-10-12)

| Day | Who | What |
|---|---|---|
| **Mon 12** | Thomas | (M4 pickup moved to Tue 13, D-003); flash **Raspberry Pi OS Lite 64-bit on a new SD card** (D-005) — hostname `k1000`, SSH with his public key, network; keep the Zynthian card aside |
| Tue 13 | Thomas: gets the M4 · Claude, on the box over SSH | display + encoders on Pi OS (port the Zynthian overlays); CamillaDSP v5 beta + v0 pipeline on the `dev` profile (HiFiBerry), `m4` profile ready |
| Wed 14 | Claude | control service (encoders ↔ CamillaDSP websocket), state persistence, mute-at-boot, systemd units, CamillaGUI compact view |
| Thu 15 | Thomas: HPO prep + Pauline call (pm) · Claude | M4 plugged → switch device, calibrate gains; **overnight soak** with an xrun log |
| **Fri 16** | Thomas | **rig test + mic tune** (§6) — same day as HPO episode 2, where he is support only |
| Sat 17 | — | buffer: fixes from Friday |
| **Sun 18** | Thomas | gig, **bypass kit in the case** |

## 5. Bypass: the fallback that makes Sunday safe

The rig played without K1000 before, and Meyer designed it to: the same full-range feed to tops and
subs. **Bypass = re-patch the source straight into the boxes as before.** Pack those cables and
label K1000's outputs. Any doubt during the gig means re-patching in a minute.

## 6. Test protocol (Friday or Saturday)

1. **Power order**: K1000 on → screen shows `READY · MUTED` → speakers on. At the end, speakers
   off first.
2. **Thump check**: once, with the speakers on and the source silent, cycle K1000's power and
   listen. Note the result in `ROADMAP.md`.
3. **Gain structure**: noise at −20 dBFS. Check the M4 meters, and that the Meyer limit LEDs stay
   dark at the expected show level.
4. **Sub alignment**:
   - Stacked under the tops: 0 ms, same polarity (Meyer's design).
   - Off-plane by ≥ 1.5 m: delay the nearer element by distance ÷ 343 m/s.
   - Check with the mic for the fullest sum around 80–100 Hz, and try the polarity flip.
5. **Lo-Cut**: A/B on program. Default on when the subs are present.
6. **System EQ**: measure with the mic (REW or Open Sound Meter on the laptop). Keep to a few broad
   cuts, then save.
7. **Live drill**: place a notch from the knobs in under 10 s, then reset live.
8. **Two-hour program run**, then read the xrun log.

## 7. Blocking on Thomas (today)

1. The interface: MOTU M4 (~€150 offer) — D-003.
2. The fresh Pi OS card online with SSH (key-based; nothing secret goes into this repo).
3. ~~Friday vs HPO episode 2~~ — answered: same day, Thomas is support only at HPO.
4. ~~CamillaDSP v4.1.3 vs v5 beta~~ — answered: v5 beta (D-004).
