# 5 — Tuning a Meyer UPA-1P + USW-1P rig

How to set up and tune the rig K1000 was built for: two Meyer USW-1P subs and two to four
UPA-1P tops, self-powered, fed through a MOTU M4. The order matters: power, gain, alignment,
Lo-Cut, then EQ, then the show.

Sources. The Meyer facts come from Meyer Sound's UPA-P operating instructions and from the LD-2
line driver's feature set, as summarised in [`docs/01-approaches-review.md`](../01-approaches-review.md)
§1. The M4 level figures are research from [`docs/05-m4-day.md`](../05-m4-day.md) §7, marked
**(research)** below until they are measured on a unit. Where this chapter says which knob to
turn, it is the panel described in [chapter 4](04-usage.md): balance, sub delay and Lo-Cut on
ALIGN (sub delay and Lo-Cut unlocked with a B3 hold), the rest in SETUP.

## 1. What you need

- K1000 installed, profile `m4`, knob map confirmed ([chapter 3](03-configuration.md)).
- The M4 front panel set and taped: MONITOR and INPUT MONITOR MIX fully clockwise, MON buttons
  unlit, 48V off, GAIN 1/2 fully counter-clockwise. **A lit MON button or the MIX knob off its
  stop sends the raw program to the tops, past K1000 and past its mute.** Details:
  [`docs/05-m4-day.md`](../05-m4-day.md).
- For the system EQ: a measurement mic, a laptop with REW or Open Sound Meter, and a way to get the
  mic into the laptop (a USB measurement mic, or the laptop's own interface). The laptop reaches
  the box over the **K1000 Wi-Fi** with SETUP › Laptop access open (2 h,
  [chapter 4 §5.1](04-usage.md#51-laptop-access-and-the-tune-page)), or on a wired network
  (CamillaGUI only: the `/tune` page answers on the K1000 Wi-Fi alone).
- The bypass kit, and ear protection for the noise.

## 2. Power order and the thump check

The boxes are self-powered: any transient at their input goes straight to the drivers.

**Power on:** speakers **off**, source quiet → M4 on the Pi's USB, its rear switch on → K1000 on.
Wait for ● RUNNING and the MUTED band (or the ramp, if it was saved unmuted) → speakers **on** →
hold B2 to unmute, master up.

**Power off:** hold B2 until MUTED (saved at once, so the next boot stays muted) → speakers **off**
→ unplug K1000 and the M4.

**Thump check, once per rig:** speakers on, source silent, cycle K1000's power and listen. Note
the result. Until it has been done with your speakers, keep the order above. The M4's behaviour
when its USB power drops is not yet measured (docs/05 lists it as unverified).

## 3. Gain structure

Aim: the source drives the M4 line inputs well below clipping, K1000's master sets the show level,
and the Meyer limiters stay idle at that level.

| Stage | Set | Check |
|---|---|---|
| Source → M4 LINE IN 3/4 | A pro +4 dBu source (DJ mixer XLR master, console): input trim 0 dB. A consumer −10 dBV source: about +10 to +12 dB (SETUP › Input trim) | A loud track peaks around −12 to −6 dBFS on the capture side, never `IN CLIP`, the M4 input meters never red |
| Too hot | `IN CLIP` lights, or the M4 input meters go red | The trim cannot help (it acts after the converter): inline −10 dB XLR pads, or the source turns down |
| K1000 → speakers | Master (E1) | The M4 output meters, the box meter (green below −18 dBFS, yellow to −6, red above) |
| The speakers | — | The Meyer limit LEDs stay dark at show level |

Level figures **(research)**: LINE IN 3/4 clip at about +18 dBu; the M4 outputs reach about
+16 dBu; Meyer UPA-1P/USW-1P limiting starts at about 0 dBV (+2.2 dBu), so roughly −14 dBFS at the
M4 outputs. That makes K1000 at master 0 dB, flat trims, about 2 dB below the old direct cable, and
the M4 cannot overdrive a Meyer input. Check it with your rig.

**Finding the show level:**

1. Pink noise from the source at −20 dBFS.
2. Bring the master up to the loudest level the show will need, watching the Meyer limit LEDs.
   Stop below the point where they light. Note that master value: it is the show level.
3. Output trims (SETUP › Trim Sub L … Top R) only to match boxes or sides, not to set the level.

**The ceiling for remote controls.**

- K1000's master never goes above 0 dB (`volume_limit`).
- CamillaGUI (wired, or over the K1000 Wi-Fi while Laptop access is open): after this step, set `volume_max` in
  `box/camillagui/gui-config.yml` to the show level + 6 dB and `volume_range` to 80 + `volume_max`,
  then deploy. One swipe on its slider otherwise reaches 0 dB in about half a second.
- **The master-up guard**, on every surface (knobs, phone, console): master rises at most 6 dB
  within any 2 s; further up-clicks are refused with `MASTER: SLOW DOWN`. Down is never limited
  (`controls.master_up_guard_db`, [chapter 3 §5](03-configuration.md#5-controls)). On the knobs,
  master up is also never accelerated: one click, 0.5 dB.
- The K1000 phone page has no absolute ceiling below 0 dB. It moves the master in steps (at most
  +1 dB per message) and rate-limits rises to +6 dB/s for all phones together, with a 3 dB burst
  (`phone.master_up_db_per_s`), on top of the guard.


## 4. Sub alignment

Meyer designed the USW-1P and UPA-1P to take the same full-range signal, and to be **in phase
across their overlap when they are close and coplanar**. Beyond about 1.5 m of separation, Meyer
says polarity reversal and/or delay may be needed.

| Layout | Delay | Polarity |
|---|---|---|
| Subs stacked under the tops, fronts in one plane | **0 ms** | same (both normal) |
| Off-plane by 1.5 m or more | Delay the element **nearer** to the audience by distance ÷ 343 m/s: **2.9 ms per metre** | check (below) |

Where the delays are: **sub delay** = ALIGN E3, locked until a **B3 hold** unlocks it (30 s of
idle locks it again); 0.1 ms per click, push held = one sample (1/48 ms); after 2.5 s at rest the
first click only wakes it. The screen shows the distance too (`≈ 0.81 m`). **Top delay** =
SETUP › Top delay. Both 0–50 ms. Delays are whole samples at 48 kHz.

**Check with the mic:**

1. The mic at a listening position, about as far from the subs as from the tops.
2. Pink noise (or program with plenty of low end).
3. Look at the response from about 60 to 150 Hz, around the overlap (60–120 Hz per Meyer,
   [`docs/01`](../01-approaches-review.md) §0). You want the **fullest sum** around
   80–100 Hz.
4. Flip SETUP › Sub polarity and compare. Keep the setting with the fuller sum. A deep, narrow dip
   in the overlap with one setting usually means the other one is right.
5. Fine-tune the delay with the push held, in single samples, for the fullest sum.

A delay or polarity change dips that group briefly: K1000 mutes the group, writes the value, and
unmutes it, so the write does not click. That dip is expected. Do the alignment before the show,
not during it.

Re-check the alignment after changing the Lo-Cut frequency: a high-pass shifts the tops' phase in
the overlap.

## 5. Lo-Cut A/B

The Lo-Cut is a 12 dB/oct high-pass on the **tops only**. It keeps low-frequency excursion out of the
UPA-1P's 12″ so the tops limit later, and leaves the low end to the subs.

| Setting | Where it comes from |
|---|---|
| **160 Hz, 12 dB/oct** | Meyer's LD-2 line driver had a Mid-Hi Lo-Cut at exactly this: the historical practice for this pair |
| **100 Hz** | K1000's default (on); the Sunday choice (D-010) |

A/B it on program with the subs running: SETUP › Lo-Cut switches it on and off (two pushes; the
phone's Lo-Cut switch, with a confirm, is handier while walking the room). ALIGN E4, unlocked with
a B3 hold, moves the frequency (20–300 Hz, 1/12 octave per click, push held = 1/48). "Off" parks
it at 10 Hz, and SHOW shows the pill `LO-CUT OFF` until it is back on.

- Higher (toward 160 Hz): cleaner, louder tops before limiting; more of the 100–160 Hz region on the
  subs. Check the sum in the overlap again (§4).
- Lower (toward 100 Hz): fuller tops; useful when the subs are far from the tops or not used.
- **Keep it on whenever the subs are present.**

## 6. Before the EQ: what not to fix with it

- A level difference between sides or boxes: output trims.
- Sub/top balance: ALIGN E2 (balance), not EQ.
- A cancellation (a deep, narrow dip that moves when the mic moves): position and alignment, not a
  boost.

## 7. System EQ

The system EQ is eight parametric bands (`sys_peq_1` … `sys_peq_8`) applied before the split, so to
subs and tops alike. Gain −20 … +10 dB, Q 0.3–16. It is set once at the tune and is **not on the
knobs**: it comes from a laptop, either imported on the `/tune` page or typed into CamillaGUI.
The live EQ sits on top of it and never changes it.

### Measure

1. The laptop and the box: SETUP › Laptop access, two pushes, then the laptop joins the **K1000**
   Wi-Fi (the PHONE page prints the password while access is open). The box is `10.42.0.1`. On a
   wired network instead (CamillaGUI only, no `/tune`): `k1000.local`, your router's list, or
   `ssh k1000 'hostname -I'`.
2. The laptop's audio output into K1000's program input (M4 LINE IN 3/4), at a level that peaks
   around −12 dBFS on the capture side.
3. The measurement mic into the laptop. Load its calibration file in REW or Open Sound Meter.
4. Pink noise (Open Sound Meter, REW's RTA) or log sweeps (REW). Measure a few positions across the
   listening area and average them.
5. Live EQ flat while you measure: SETUP › Reset live EQ, two pushes. (SETUP › Undo reset brings
   it back within 5 minutes, unless a live-EQ gain has moved since.)

### Decide

- **A few broad cuts.** Wide bells (low Q) on the broad bumps.
- **No boosts into dips.** Dips are usually cancellations or reflections: a boost spends headroom
  and does not fill them.
- Leave the extreme low end to the alignment and the balance.
- Write the bands down on paper as you go.

Never load a CamillaDSP config file exported by REW or anything else into the engine: K1000's
pipeline has fixed slots. Use the `/tune` page, or type the values into the eight `sys_peq_N`
bands.

### Import it on the tune page

`https://10.42.0.1/tune` on the laptop, while Laptop access is open (the laptop needs the K1000 CA,
like a phone, or accept the browser's warning once). It answers `403` while access is closed.

1. **Paste the filter list.** REW: *File › Export › Export filter settings as text*, then the
   file's whole text (the header is ignored). EqualizerAPO / AutoEQ: the `Filter` lines. Open
   Sound Meter has no filter export we know of: type the filters as
   `Filter 1: ON PK Fc 125 Hz Gain -4.0 dB Q 5.00` or `125 Hz -4 dB Q 5`, one per line.
2. **Preview.** The eight system bands now (grey) and after the import (amber), computed like the
   engine, and a band table. The filters go on bands 1…n in the list's order; the other bands
   become flat.
   - **Peaking filters only.** Shelves, pass filters, notches and the rest are listed as skipped,
     never approximated; a 0 dB filter (REW's `-0.00`) is skipped too.
   - **More than 8 peaking filters: refused whole.** Re-run the EQ with 8 at most.
   - Values are clamped to the system ranges (20 Hz–20 kHz, −20 … +10 dB, Q 0.3–16), and each clamp
     is listed. `Preamp` is ignored: set the level with the master.
3. **Apply to the system EQ** (with a confirm). The box re-reads the text itself and sets the
   eight bands like a knob would: clamped, click-safe steps (about a second), saved at once.
4. **Undo** puts back the system EQ the last Apply replaced (one level; it survives a k1000
   restart).
5. Close Laptop access: one push on SETUP › Laptop access.

More: [`docs/06-phone.md`](../06-phone.md) §11.

![The tune page](../ui/tune_preview.png)

### Or enter it in CamillaGUI (the rules)

CamillaGUI: `http://10.42.0.1:5005/gui/index.html` over the K1000 Wi-Fi with Laptop access open,
or `http://<box>:5005/` on a wired network.

1. **Press "Fetch from DSP" (or reload the page) right before editing.** "Apply" sends the whole
   config the page holds. A page opened before knob moves, or before a profile change, would put
   the old values (or the old audio device) back.
2. In Filters, edit only the `sys_peq_1` … `sys_peq_8` bands: frequency, gain, Q. They are Peaking
   biquads. **Do not change a filter's type, add or remove filters, or touch the mixers or the
   pipeline**: K1000 restores its own pipeline (`PIPELINE CHANGED (GUI)`).
3. **"Apply to DSP".** K1000 adopts the new values and saves them in `state.json` within a few
   seconds.
4. "Save to file" fails on purpose (`Read-only file system`): K1000 owns the files.
5. Never tick "Apply automatically", and never "Stop processing" (about 2 s of silence, then the
   ramp).
6. Check: `ssh k1000 'python3 -c "import json; print(json.load(open(\"/var/lib/k1000/state.json\"))[\"sys_peq\"])"'`

If you lower or mute a group or a filter in CamillaGUI (to measure the tops alone, for example),
K1000 **keeps** that cut and shows it on the screen (`TOPS GROUP MUTED (GUI)`, `… (GUI)`). Undo it
in CamillaGUI when you are done. K1000 never raises or unmutes it by itself. A raise it cannot
adopt (a group fader, a fixed gain, a value out of range) is pulled back down in small steps.

## 8. Live notching at the show

The live EQ: four bells (cut down to −20 dB, boost at most +6 dB) and two shelves, on top of the
system EQ. A band at 0 dB is free and inaudible.

**On SHOW, in 3 gestures: B4, E3 down, E4 up** (on another page: B1 hold first, then wait for the
cyan PEAK, about 2 s, since the spectrum runs only while SHOW is on screen):

1. **B4**: the notch helper sets a free bell to Q 8.65 (1/6 octave) and, if the cyan PEAK pointer
   is up, to its frequency. The gain stays at 0 dB, so nothing is audible yet; the strip says
   `P3 · 2.44k · E3 TO CUT`. A PEAK within 1/6 octave of a bell already in use selects that bell
   instead.
2. **E3 down**: the cut (12 clicks = −6 dB).
3. **E4 up**: narrower, until the ring goes without dulling the program (6 clicks = Q 16, the
   maximum). Centre it with E2, push held (1/48 octave per click).

With no PEAK (`NO PEAK` in the hint: the program is silent, the ring is not in the program, or
Spectrum is OFF) the bell stays at its own frequency. **Cut first, then sweep**: E3 down (about
−6 dB; at 0 dB the bell is inaudible wherever it sits), then E2 until the ring drops (250 Hz →
2.5 kHz is about 40 clicks), then E4 up to narrow it. The spectrum is the program input, before the EQ: it shows a ring
that feeds back through a microphone in the program, not a room resonance, and it does not drop
when you cut. Details and pictures: [chapter 4 §3.2](04-usage.md#32-notching-a-ring-in-3-gestures).

**Phone:** long-press "notch here" on the graph, and slide onto the ring without lifting. Unlike
B4, the phone's notch cuts at once (−6 dB, Q 8: `phone.notch_gain_db`, `phone.notch_q`)
([`docs/06-phone.md`](../06-phone.md) §2).

**Reset all live EQ:** SETUP › Reset live EQ, two pushes. **SETUP › Undo reset** puts it back
exactly, for 5 minutes or until the next audible live-EQ edit. The phone's reset (with a confirm)
is the same reset: the box can undo it.

Good practice:

- Cut narrow and only as deep as needed. Several small notches beat one huge one.
- Reset the live EQ when the system moves to a new place, then start again from the tune.
- A boost on the live layer is limited to +6 dB on purpose: it is for taming, not pushing.

## 9. After the tune

- Everything is saved automatically. Note the key values on paper anyway (master show level,
  delays, Lo-Cut, the eight system bands).
- Copy `/var/lib/k1000/state.json` off the box: it is the tune ([chapter 7](07-maintenance.md)).
- Do a two-hour program run and read the soak summary before the first show.
