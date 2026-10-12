# 5 — Tuning a Meyer UPA-1P + USW-1P rig

How to set up and tune the rig K1000 was built for: two Meyer USW-1P subs and two to four
UPA-1P tops, self-powered, fed through a MOTU M4. The order matters: power, gain, alignment,
Lo-Cut, then EQ, then the show.

Sources. The Meyer facts come from Meyer Sound's UPA-P operating instructions and from the LD-2
line driver's feature set, as summarised in [`docs/01-approaches-review.md`](../01-approaches-review.md)
§1. The M4 level figures are research from [`docs/05-m4-day.md`](../05-m4-day.md) §7, marked
**(research)** below until they are measured on a unit. Where this chapter says which knob to
turn, it describes the v0 panel ([operator card](../04-operator-card.md)); UI v1 moves sub delay
and Lo-Cut to the ALIGN page ([`docs/07-live-ui-v1.md`](../07-live-ui-v1.md)).

## 1. What you need

- K1000 installed, profile `m4`, knob map confirmed ([chapter 3](03-configuration.md)).
- The M4 front panel set and taped: MONITOR and INPUT MONITOR MIX fully clockwise, MON buttons
  unlit, 48V off, GAIN 1/2 fully counter-clockwise. **A lit MON button or the MIX knob off its
  stop sends the raw program to the tops, past K1000 and past its mute.** Details:
  [`docs/05-m4-day.md`](../05-m4-day.md).
- For the system EQ: a measurement mic, a laptop with REW or Open Sound Meter, and a way to get the
  mic into the laptop (a USB measurement mic, or the laptop's own interface). The laptop on the
  **wired** network with the box, for CamillaGUI.
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
- CamillaGUI on the wired network: after this step, set `volume_max` in
  `box/camillagui/gui-config.yml` to the show level + 6 dB and `volume_range` to 80 + `volume_max`,
  then deploy. One swipe on its slider otherwise reaches 0 dB in about half a second.
- The K1000 phone page has no absolute ceiling in the current code. It moves the master in steps
  (at most +1 dB per message) and rate-limits rises to +6 dB/s for all phones together, with a
  3 dB burst (`phone.master_up_db_per_s`).
- On the knobs, D-009 plans a master-up guard: UI v1 turns off acceleration on master up
  ([`docs/07`](../07-live-ui-v1.md) §2.2).
  <!-- TODO(after UI v1): re-check both points against box/k1000/web/server.py and the master
  knob's acceleration in box/k1000/ui/input_map.py. -->


## 4. Sub alignment

Meyer designed the USW-1P and UPA-1P to take the same full-range signal, and to be **in phase
across their overlap when they are close and coplanar**. Beyond about 1.5 m of separation, Meyer
says polarity reversal and/or delay may be needed.

| Layout | Delay | Polarity |
|---|---|---|
| Subs stacked under the tops, fronts in one plane | **0 ms** | same (both normal) |
| Off-plane by 1.5 m or more | Delay the element **nearer** to the audience by distance ÷ 343 m/s: **2.9 ms per metre** | check (below) |

Where the delays are: **sub delay** = MAIN E3 (v0), 0.1 ms per detent, push held = one sample
(1/48 ms); after 2.5 s at rest the first detent only wakes it. **Top delay** = SETUP › Top delay.
Both 0–50 ms. Delays are whole samples at 48 kHz.

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
| **100 Hz** | K1000's default (on) |

A/B it on program with the subs running: MAIN B3 switches it on and off (v0), MAIN E4 moves the
frequency (20–300 Hz, 1/12 octave per detent). "Off" parks it at 10 Hz.

- Higher (toward 160 Hz): cleaner, louder tops before limiting; more of the 100–160 Hz region on the
  subs. Check the sum in the overlap again (§4).
- Lower (toward 100 Hz): fuller tops; useful when the subs are far from the tops or not used.
- **Keep it on whenever the subs are present.**

## 6. Before the EQ: what not to fix with it

- A level difference between sides or boxes: output trims.
- Sub/top balance: E2 (balance), not EQ.
- A cancellation (a deep, narrow dip that moves when the mic moves): position and alignment, not a
  boost.

## 7. System EQ

The system EQ is eight parametric bands (`sys_peq_1` … `sys_peq_8`) applied before the split, so to
subs and tops alike. Gain −20 … +10 dB, Q 0.3–16. It is set once at the tune and is **not on the
knobs**: you enter it from CamillaGUI on a laptop. The live EQ sits on top of it and never changes
it.

### Measure

1. The laptop on the wired network with the box. Find the box's address (`k1000.local`, or your
   router's list, or `ssh k1000 'hostname -I'`).
2. The laptop's audio output into K1000's program input (M4 LINE IN 3/4), at a level that peaks
   around −12 dBFS on the capture side.
3. The measurement mic into the laptop. Load its calibration file in REW or Open Sound Meter.
4. Pink noise (Open Sound Meter, REW's RTA) or log sweeps (REW). Measure a few positions across the
   listening area and average them.
5. Live EQ flat while you measure (LIVE page: hold B4, then hold it again within 5 s).

### Decide

- **A few broad cuts.** Wide bells (low Q) on the broad bumps.
- **No boosts into dips.** Dips are usually cancellations or reflections: a boost spends headroom
  and does not fill them.
- Leave the extreme low end to the alignment and the balance.
- Write the bands down on paper as you go.

If your REW version exports filters in CamillaDSP's format, still **do not load an exported
file**: K1000's pipeline has fixed slots. Copy the values by hand into the eight `sys_peq_N` bands.

### Enter it in CamillaGUI (the rules)

CamillaGUI: `http://<box>:5005/` on the laptop.

1. **Press "Fetch from DSP" (or reload the page) right before editing.** "Apply" sends the whole
   config the page holds. A page opened before knob moves, or before a profile change, would put
   the old values (or the old audio device) back.
2. In Filters, edit only the `sys_peq_1` … `sys_peq_8` bands: frequency, gain, Q. They are Peaking
   biquads. **Do not change a filter's type, add or remove filters, or touch the mixers or the
   pipeline**: K1000 restores its own pipeline (`PIPELINE CHANGED OUTSIDE K1000`).
3. **"Apply to DSP".** K1000 adopts the new values and saves them in `state.json` within a few
   seconds.
4. "Save to file" fails on purpose (`Read-only file system`): K1000 owns the files.
5. Never tick "Apply automatically", and never "Stop processing" (about 2 s of silence, then the
   ramp).
6. Check: `ssh k1000 'python3 -c "import json; print(json.load(open(\"/var/lib/k1000/state.json\"))[\"sys_peq\"])"'`

If you lower or mute a group or a filter in CamillaGUI (to measure the tops alone, for example),
K1000 **keeps** that cut and shows it on the screen (`TOPS GROUP MUTED`, `… OUTSIDE K1000`). Undo it
in CamillaGUI when you are done. K1000 never raises or unmutes it by itself. A raise it cannot
adopt (a group fader, a fixed gain, a value out of range) is pulled back down in small steps.

## 8. Live notching at the show

The live EQ: four bells (cut down to −20 dB, boost at most +6 dB) and two shelves, on top of the
system EQ. A band at 0 dB is free and inaudible.

**v0 panel, LIVE page** (the operator card's "Feedback" line):

1. E1: pick a free slot (gain 0 dB).
2. E2: frequency onto the ring (push held = 1/48 octave steps).
3. E3: cut.
4. E4: Q up (narrower) until the ring goes without dulling the program.

**Phone:** long-press "notch here" on the graph, and slide onto the ring without lifting
([`docs/06-phone.md`](../06-phone.md) §2).

**UI v1:** a B4 tap grabs a free band at the spectrum peak (gain untouched, so inaudible until E3
cuts). See [`docs/07-live-ui-v1.md`](../07-live-ui-v1.md) §2.4.

**Reset all live EQ:** LIVE page, hold B4, then hold it again within 5 s. No undo in v0. The phone
has its own reset with a confirm.

Good practice:

- Cut narrow and only as deep as needed. Several small notches beat one huge one.
- Reset the live EQ when the system moves to a new place, then start again from the tune.
- A boost on the live layer is limited to +6 dB on purpose: it is for taming, not pushing.

## 9. After the tune

- Everything is saved automatically. Note the key values on paper anyway (master show level,
  delays, Lo-Cut, the eight system bands).
- Copy `/var/lib/k1000/state.json` off the box: it is the tune ([chapter 7](07-maintenance.md)).
- Do a two-hour program run and read the soak summary before the first show.
