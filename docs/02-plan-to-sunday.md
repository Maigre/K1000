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
  - Caveats: its MONITOR knob scales outs 1-2, so set it fully clockwise and tape it, then calibrate
    with it there. INPUT MONITOR MIX is a balance knob, not a switch: fully clockwise = USB playback
    only; anywhere else, raw input leaks into outs 1-2 past the DSP and its mute. Tape it fully
    clockwise and keep the MON buttons unlit (leak test: `docs/05-m4-day.md` step 6).
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

UI v1 (D-009, accepted on the box 2026-10-13; it replaced the v0 MAIN / LIVE EQ pages, which are in
git history), with D-011's changes (same day: Lo-Cut 2 Hz a click, one signed DELAY, dim knob
tags), D-013's (same day: the buttons B1 PAGE · B2 BAND/UNLOCK · B3 NOTCH · B4 MUTE, SETUP by
speaker floor), D-015's (2026-10-14: SHOW E3 Q / E4 GAIN, ALIGN E2 DELAY / E3 BALANCE, IN
meters) and D-017's (2026-10-17, built, not on the box yet: SETUP E2 GROUP / E3 VALUE, a fixed
selected row, a popup and an E3 push for the risky rows). The gig card is
`docs/04-operator-card.md`.

| Page | E1 | E2 | E3 | E4 |
|---|---|---|---|---|
| SHOW (front) | master | frequency of the selected band (push = fine) | Q (shelves: slope) | gain of the selected band |
| ALIGN | master | DELAY, subs (clockwise) or tops (anticlockwise) later: **locked** (after the unlock, the first detent after 2.5 s at rest only wakes it) | sub↔top balance | top Lo-Cut freq, 2 Hz a click: **locked** |
| SETUP | item | group | value: turn = change; a risky row proposes in a popup, push E3 to apply (D-017) | — |

- Master up is never accelerated and rises at most +6 dB per 2 s (`MASTER: SLOW DOWN`; k1000.yaml
  `controls.master_up_guard_db`, 0 = off). Down is always instant.
- SHOW draws the live-EQ curve (computed from the engine's own filters) over the program spectrum,
  with a cyan PEAK pointer when one stands out.

Buttons (D-013):
- B1 tap: SHOW ↔ ALIGN (from SETUP: SHOW); hold: SETUP. ALIGN and SETUP return to SHOW after 60 s
  idle.
- B2 on SHOW: next band, LS · 1 · 2 · 3 · 4 · HS (wraps). On ALIGN, a hold unlocks E2/E4 for 30 s of
  idle.
- B3 on SHOW: the notch helper. A free bell (at 0 dB) gets a notch Q (1/6 oct) and the PEAK's
  frequency; its gain is never touched, so it is inaudible until E4 cuts.
- B4 mute / unmute (long press, 0.8 s), every page.
- SETUP, grouped by speaker floor (TOPS, SUBS, INPUT, LIVE EQ, PHONE, BOX), holds what used to be
  on buttons: Reset live EQ (two E3 pushes, then a one-level Undo reset), TOPS › Lo-Cut on/off, and
  Spectrum and Laptop access. E3 changes a value as it turns; the risky rows (every rebuild, both
  polarities, Lo-Cut) open a popup on the turn and apply on an E3 push (D-017).

The screen shows the current page, plus mute/clip state. System alerts take the status bar; hints
are a brief amber strip.

**Phone:** K1000's own phone page over the box's Wi-Fi (D-007, `docs/06-phone.md`; go/no-go on
Friday). CamillaGUI's full editor runs on a laptop for the system EQ at the tune: at the rig, over
the K1000 Wi-Fi while SETUP › PHONE › Laptop access is open (2 h), where the `https://10.42.0.1/tune` page
also takes a pasted REW filter list.

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

A read-only root, an output mute relay, a presets UI, and a final enclosure. (Moved forward: the
box's own Wi‑Fi AP + phone PWA on 2026-10-12, D-007, gated by a go/no-go at the Friday rig test; the
SHOW spectrum and the notch helper on 2026-10-12, D-009, the spectrum behind a SETUP switch
decided at the Friday rig test.)

## 4. Schedule (rev 2 — after Thomas's answers of 2026-10-12)

| Day | Who | What |
|---|---|---|
| **Mon 12** | Thomas | (M4 pickup moved to Tue 13, D-003); flash **Raspberry Pi OS Lite 64-bit on a new SD card** (D-005) — hostname `k1000`, SSH with his public key, network; keep the Zynthian card aside |
| Tue 13 | Thomas: gets the M4 · Claude, on the box over SSH | display + encoders on Pi OS (port the Zynthian overlays); CamillaDSP v5 beta + v0 pipeline on the `dev` profile (HiFiBerry), `m4` profile ready |
| Wed 14 | Claude | control service (encoders ↔ CamillaDSP websocket), state persistence, mute-at-boot, systemd units, CamillaGUI compact view |
| Thu 15 | Thomas: another project (pm) · Claude | M4 plugged → switch device, calibrate gains; **overnight soak** with an xrun log |
| **Fri 16** | Thomas | **rig test + mic tune** (§6) — same day as another project, where he is support only |
| Sat 17 | — | buffer: fixes from Friday |
| **Sun 18** | Thomas | gig, **bypass kit in the case** |

## 5. Bypass: the fallback that makes Sunday safe

The rig played without K1000 before, and Meyer designed it to: the same full-range feed to tops and
subs. **Bypass = re-patch the source straight into the boxes as before.** Pack those cables and
label K1000's outputs. Any doubt during the gig means re-patching in a minute.

## 6. Test protocol (Friday or Saturday)

0. **Before the speakers**: the Knob test is done and `controls:` confirmed (SETUP › BOX › Knob
   test, `box/README.md`), the buttons labelled B1 PAGE · B2 BAND/UNLOCK · B3 NOTCH · B4 MUTE as the
   Knob test names them (D-013); hold B4 mutes all four Meyers (a B3 press in its place is
   harmless, but it does not mute). The M4 front
   panel is taped (MONITOR and INPUT MONITOR MIX fully clockwise, MON buttons unlit). Phone: decide
   the go/no-go on the own-AP PWA (D-007) now, but if it is a no-go, switch phone control off only
   (SETUP › PHONE › Phone, `PHONE OFF · KNOBS ONLY`) and take the AP fully off **after step 6**: the system
   EQ uses the K1000 Wi-Fi. Fully off = `setup-ap.sh --down` + `phone: enabled: false`
   (`docs/06-phone.md` §9; SETUP › PHONE › Phone alone leaves the Wi-Fi on the air); the knobs then carry
   the show.
1. **Power order**: K1000 on → screen shows MUTED → speakers on. The box powers up the way it was
   powered down: `READY · MUTED` only if it was muted (hold B4) before power-off; otherwise it ramps
   back to the saved level by itself ("RAMPS IN BY ITSELF"). So the end-of-night routine is: hold B4
   until MUTED, speakers off, then K1000 off.
2. **Thump check**: once, with the speakers on and the source silent, cycle K1000's power and
   listen. Note the result in `ROADMAP.md`.
3. **Gain structure**: noise at −20 dBFS. Check the M4 meters, and that the Meyer limit LEDs stay
   dark at the expected show level.
4. **Sub alignment** (ALIGN, hold B2 to unlock E2 = DELAY, one setting for both sides; the
   polarity flips are SETUP › SUBS › Polarity and TOPS › Polarity):
   - Stacked under the tops: 0 ms (`ALIGNED`), same polarity (Meyer's design).
   - Off-plane by ≥ 1.5 m: delay the nearer element by distance ÷ 343 m/s: subs nearer → E2
     clockwise (`SUBS LATER`), tops nearer → E2 anticlockwise past 0 (`TOPS LATER`). The screen
     shows the distance, so read it against the tape measure.
   - Check with the mic for the fullest sum around 80–100 Hz, and try the polarity flip.
5. **Lo-Cut**: A/B on program with SETUP › TOPS › Lo-Cut (turn E3, push E3 in the popup, each
   way; SHOW shows a `LO-CUT OFF` pill while it is off); the frequency is E4 on ALIGN after a B2
   hold. Default on when the subs are present, at 100 Hz (D-010); 160 Hz only if the tops limit
   early.
6. **System EQ**: the laptop joins the K1000 Wi-Fi and SETUP › PHONE › Laptop access opens
   CamillaGUI and SSH for 2 h (two E3 pushes; the PHONE page prints the Wi-Fi password meanwhile).
   Measure with the mic (REW or Open Sound Meter), then paste the filter list at
   `https://10.42.0.1/tune` (preview on the 8 system bands, Apply = saved, Undo), or use CamillaGUI
   at `http://10.42.0.1:5005` (Fetch from DSP first). Keep to a few broad cuts. One E3 push closes
   Laptop access when done. Without the K1000 Wi-Fi (already off after a no-go, or not working):
   CamillaGUI over a direct `eth0` cable, the laptop sharing its connection on 10.43.0.1/24 (never
   10.42.0.x, the AP's subnet), at `http://k1000.local:5005`; no `/tune` page that way (it answers
   on the K1000 Wi-Fi only).
7. **Notch drill with B3** (SHOW): make a steady peak in the program at low level (a sine from the
   source, e.g. 1–3 kHz, or a held note) and wait for the cyan PEAK pointer. Then B3 (a free bell
   lands on it at Q 8.65, nothing audible), E4 down 12 clicks (−6 dB: the tone drops), E3 up to
   narrow: under 10 s. B3 again must re-select that bell and write nothing. Also try B3 with no peak
   (`NO PEAK`: E4 down first, about −6 dB, then sweep E2 until the tone drops; at 0 dB the sweep is
   silent). Then SETUP › LIVE EQ › Reset live EQ (two E3 pushes) and Undo reset (it must come
   back exactly), and reset again for the show.
8. **Spectrum** (ON for Sunday, D-010; SETUP › LIVE EQ › Spectrum, saved). Check, with real program at the
   rig: the graph reads well in that light and at that angle; the PEAK
   pointer is steady on music, B3 lands where the ear says, and the pointer goes when the program
   stops (fixed after the 2026-10-13 acceptance: no PEAK under −80 dBFS, nor with the program under
   −60 dBFS RMS; if quiet real program never gets a pointer, lower `spectrum.peak_floor_db` /
   `program_floor_db` in k1000.yaml, then restart k1000); after an engine start the picture comes
   back by itself within a few seconds, with no grey `NO SPECTRUM` and nothing new in
   `journalctl -u k1000 | grep -i spectrum` (also fixed); the knobs feel the same with it ON and
   OFF (a fast E2 sweep, E1 master); Thursday's soak with it ON was clean. If one of these fails, OFF. If
   anything at all is odd, take it out entirely: `spectrum: enabled: false` in `/etc/k1000/k1000.yaml`, then
   `sudo systemctl restart k1000` between shows (no second engine connection; SETUP › LIVE EQ ›
   Spectrum then reads DISABLED). B3 still works without it (`NO PEAK`: E4 down first, then sweep E2). Note the decision in
   `ROADMAP.md`.
9. **Two-hour program run**, then read the xrun log (`k1000-soak.sh --summary`: only dropouts
   inside the run count; older ones are printed as a `note:`). Give the run its own unit and log
   (`--unit=k1000-soak-2h -E K1000_SOAK_LOG=/var/log/k1000/soak-2h.log`, then `--summary` on that
   file): appended to Thursday's `soak-thu.log`, its window would span both runs (`box/README.md`
   § Soak test).

## 7. Blocking on Thomas (today)

1. The interface: MOTU M4 (~€150 offer) — D-003.
2. The fresh Pi OS card online with SSH (key-based; nothing secret goes into this repo).
3. ~~Friday vs another project the same day~~ — answered: Thomas is support only there.
4. ~~CamillaDSP v4.1.3 vs v5 beta~~ — answered: v5 beta (D-004).
