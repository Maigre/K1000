# 5 — Tuning a Meyer UPA-1P + USW-1P rig

How to set up and tune the rig K1000 was built for: two Meyer USW-1P subs and two to four
UPA-1P tops, self-powered, fed through a MOTU M4. The order matters: power, gain, alignment,
Lo-Cut, then EQ, then the show.

Sources. The Meyer facts come from Meyer Sound's UPA-P operating instructions and from the LD-2
line driver's feature set, as summarised in [`docs/01-approaches-review.md`](../01-approaches-review.md)
§1. The M4 level figures are research from [`docs/05-m4-day.md`](../05-m4-day.md) §7, marked
**(research)** below until they are measured on a unit. Where this chapter says which knob to
turn, it is the panel described in [chapter 4](04-usage.md): balance, the sub/top delay and Lo-Cut
on ALIGN (the delay and Lo-Cut unlocked with a B2 hold), the rest in SETUP (by speaker floor:
TOPS, SUBS, INPUT…).

## 1. What you need

- K1000 installed, profile `m4`, knob map confirmed ([chapter 3](03-configuration.md)).
- The M4 front panel set and taped: MONITOR and INPUT MONITOR MIX fully clockwise, MON buttons
  unlit, 48V off, GAIN 1/2 fully counter-clockwise. **A lit MON button or the MIX knob off its
  stop sends the raw program to the tops, past K1000 and past its mute.** Details:
  [`docs/05-m4-day.md`](../05-m4-day.md).
- For the system EQ: a measurement mic, a laptop with REW or Open Sound Meter, and a way to get the
  mic into the laptop (a USB measurement mic, or the laptop's own interface). The laptop reaches
  the box over the **K1000 Wi-Fi** with SETUP › PHONE › Laptop access open (2 h,
  [chapter 4 §5.1](04-usage.md#51-laptop-access-and-the-tune-page)), or on a wired network
  (CamillaGUI only: the `/tune` page answers on the K1000 Wi-Fi alone).
- The bypass kit, and ear protection for the noise.

## 2. Power order and the thump check

The boxes are self-powered: any transient at their input goes straight to the drivers.

**Power on:** speakers **off**, source quiet → M4 on the Pi's USB, its rear switch on → K1000 on.
Wait for ● RUNNING and the MUTED band (or the ramp, if it was saved unmuted) → speakers **on** →
hold B4 to unmute, master up.

**Power off:** hold B4 until MUTED (saved at once, so the next boot stays muted) → speakers **off**
→ unplug K1000 and the M4.

**Thump check, once per rig:** speakers on, source silent, cycle K1000's power and listen. Note
the result. Until it has been done with your speakers, keep the order above. The M4's behaviour
when its USB power drops is not yet measured (docs/05 lists it as unverified).

## 3. Gain structure

Aim: the source drives the M4 line inputs well below clipping, K1000's master sets the show level,
and the Meyer limiters stay idle at that level.

| Stage | Set | Check |
|---|---|---|
| Source → M4 LINE IN 3/4 | A pro +4 dBu source (DJ mixer XLR master, console): input trim 0 dB to start; about +10 dB gives the old direct-cable level. A consumer −10 dBV source: +12 dB (SETUP › INPUT › Trim) | A loud track peaks around −12 to −6 dBFS on the capture side, never `IN CLIP`, the M4 input meters never red |
| Too hot | `IN CLIP` lights, or the M4 input meters go red | The trim cannot help (it acts after the converter): inline −10 dB XLR pads, or the source turns down |
| K1000 → speakers | Master (E1) | The M4 output meters, the box meter (green below −18 dBFS, yellow to −6, red above) |
| The speakers | — | The Meyer limit LEDs stay dark at show level |

Level figures **(measured 2026-10-16, loop test)**: the M4's outputs sit 9.9 dB below its line
inputs (an output at 0 dBFS reads −9.9 dBFS on LINE IN 3/4, all four outputs alike). So K1000 at
master 0 dB with flat trims plays about **10 dB under the old direct cable**; master stops at 0 dB,
and the input trim (up to +12 dB) makes up the rest. Estimated in dBu (from the HiFiBerry's full
scale, not a meter): LINE IN 3/4 clip at about +20.7 dBu, the outputs reach about +10.8 dBu, and
Meyer UPA-1P/USW-1P limiting (about 0 dBV, +2.2 dBu) sits near −8.6 dBFS at the M4 outputs: the
limiters act well before the M4 clips, and the M4 cannot overdrive a Meyer input.

**Finding the show level:**

1. Pink noise from the source at −20 dBFS.
2. Bring the master up to the loudest level the show will need, watching the Meyer limit LEDs.
   Stop below the point where they light. Note that master value: it is the show level.
3. Output trims (SETUP › SUBS › Trim L/R, TOPS › Trim L/R) only to match boxes or sides, not to set
   the level.

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

Where the delay is: **ALIGN E2, DELAY** (bottom left; E3 until D-015), one setting for either
side (D-011), locked until a **B2 hold** unlocks it (30 s of idle locks it again). Clockwise from 0 delays the **subs** (subs
nearer the audience: `SUBS LATER`), anticlockwise from 0 delays the **tops** (tops nearer:
`TOPS LATER`); up to 50 ms either way, and only the late side is delayed. 0.1 ms per click, push
held = one sample (1/48 ms); after 2.5 s at rest the first click only wakes it. The screen shows
the distance too (`SUBS LATER · 0.81 m`). Turning through zero stops one click on `ALIGNED`.
Delays are whole samples at 48 kHz.

**Check with the mic:**

1. The mic at a listening position, about as far from the subs as from the tops.
2. Pink noise (or program with plenty of low end).
3. Look at the response from about 60 to 150 Hz, around the overlap (60–120 Hz per Meyer,
   [`docs/01`](../01-approaches-review.md) §0). You want the **fullest sum** around
   80–100 Hz.
4. Flip SETUP › SUBS › Polarity (turn E3, push E3 in the popup) and compare. Keep the setting with
   the fuller sum. A deep, narrow dip in the overlap with one setting usually means the other one is
   right.
5. Fine-tune the delay with the push held, in single samples, for the fullest sum.

A delay or polarity change dips that group briefly: K1000 mutes the group, writes the value, and
unmutes it, so the write does not click. A quick turn through `ALIGNED` changes both delays in
one write, so subs and tops dip together (about 0.3 s). That dip is expected. Do the alignment
before the show, not during it.

Re-check the alignment after changing the Lo-Cut frequency: a high-pass shifts the tops' phase in
the overlap.

## 5. Lo-Cut A/B

The Lo-Cut is a 12 dB/oct high-pass on the **tops only**. It keeps low-frequency excursion out of the
UPA-1P's 12″ so the tops limit later, and leaves the low end to the subs.

| Setting | Where it comes from |
|---|---|
| **160 Hz, 12 dB/oct** | Meyer's LD-2 line driver had a Mid-Hi Lo-Cut at exactly this: the historical practice for this pair |
| **100 Hz** | K1000's default (on); the Sunday choice (D-010) |

A/B it on program with the subs running: SETUP › TOPS › Lo-Cut switches it on and off (turn E3,
then push E3 in the popup; the phone's Lo-Cut switch, with a confirm, is handier while walking the room).
ALIGN E4, unlocked with a B2 hold, moves the frequency (20–300 Hz, 2 Hz per click, push held = 1 Hz;
a fast spin goes 8 Hz a click: 100 → 160 Hz is 30 clicks, or a few fast turns). "Off" parks it at
10 Hz, and SHOW shows the pill `LO-CUT OFF` until it is back on.

- Higher (toward 160 Hz): cleaner, louder tops before limiting; more of the 100–160 Hz region on the
  subs. Check the sum in the overlap again (§4).
- Lower (toward 100 Hz): fuller tops; useful when the subs are far from the tops or not used.
- **Keep it on whenever the subs are present.**

## 6. Before the EQ: what not to fix with it

- A level difference between sides or boxes: output trims.
- Sub/top balance: ALIGN E3 (balance), not EQ.
- A cancellation (a deep, narrow dip that moves when the mic moves): position and alignment, not a
  boost.

## 7. System EQ

The system EQ is eight parametric bands (`sys_peq_1` … `sys_peq_8`) applied before the split, so to
subs and tops alike. Gain −20 … +10 dB, Q 0.3–16. It is set once at the tune and is **not on the
knobs**: it comes from a laptop, either imported on the `/tune` page or typed into CamillaGUI.
The live EQ sits on top of it and never changes it.

### Measure

1. The laptop and the box: SETUP › PHONE › Laptop access, two E3 pushes, then the laptop joins the
   **K1000** Wi-Fi (the PHONE page prints the password while access is open). The box is `10.42.0.1`. On a
   wired network instead (CamillaGUI only, no `/tune`): `k1000.local`, your router's list, or
   `ssh k1000 'hostname -I'`.
2. The laptop's audio output into K1000's program input (M4 LINE IN 3/4), at a level that peaks
   around −12 dBFS on the capture side.
3. The measurement mic into the laptop. Load its calibration file in REW or Open Sound Meter.
4. Pink noise (Open Sound Meter, REW's RTA) or log sweeps (REW). Measure a few positions across the
   listening area and average them.
5. Live EQ flat while you measure: SETUP › LIVE EQ › Reset live EQ, two E3 pushes. (Undo reset, just
   below it, brings it back within 5 minutes, unless a live-EQ gain has moved since.)

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
5. Close Laptop access: one E3 push on SETUP › PHONE › Laptop access.

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

**On SHOW, in 3 gestures: B3, E4 down, E3 up** (on ALIGN or SETUP: a B1 tap first, then wait for
the cyan PEAK, about 2 s, since the spectrum runs only while SHOW is on screen):

1. **B3**: the notch helper sets a free bell to Q 8.65 (1/6 octave) and, if the cyan PEAK pointer
   is up, to its frequency. The gain stays at 0 dB, so nothing is audible yet; the strip says
   `P3 · 2.44k · E4 TO CUT`. A PEAK within 1/6 octave of a bell already in use selects that bell
   instead.
2. **E4 down** (GAIN, bottom right): the cut (12 clicks = −6 dB).
3. **E3 up** (Q, top right): narrower, until the ring goes without dulling the program (6 clicks
   = Q 16, the maximum). Centre it with E2, push held (1/48 octave per click).

With no PEAK (`NO PEAK` in the hint: the program is silent, the ring is not in the program, or
Spectrum is OFF) the bell stays at its own frequency. **Cut first, then sweep**: E4 down (about
−6 dB; at 0 dB the bell is inaudible wherever it sits), then E2 until the ring drops (250 Hz →
2.5 kHz is about 40 clicks), then E3 up to narrow it. The spectrum is the program input, before the EQ: it shows a ring
that feeds back through a microphone in the program, not a room resonance, and it does not drop
when you cut. Details and pictures: [chapter 4 §3.2](04-usage.md#32-notching-a-ring-in-3-gestures).

**Phone:** long-press "notch here" on the graph, and slide onto the ring without lifting. Unlike
B3, the phone's notch cuts at once (−6 dB, Q 8: `phone.notch_gain_db`, `phone.notch_q`)
([`docs/06-phone.md`](../06-phone.md) §2).

**Reset all live EQ:** SETUP › LIVE EQ › Reset live EQ, two E3 pushes. **Undo reset**, the next row,
puts it back exactly, for 5 minutes or until the next audible live-EQ edit. The phone's reset (with a confirm)
is the same reset: the box can undo it.

Good practice:

- Cut narrow and only as deep as needed. Several small notches beat one huge one.
- Reset the live EQ when the system moves to a new place, then start again from the tune.
- A boost on the live layer is limited to +6 dB on purpose: it is for taming, not pushing.

## 9. De-harsh and sub loudness by ear

Two SETUP switches from the first rig run (D-016), both **off by default**. Neither replaces the
tune: do the system EQ first (§7), then try them at the rig with the music of the show, at show
level, listening from where the audience will be. Each switch rebuilds the pipeline (a possible
click): switch it **muted, or before the doors**. Their numbers move without a click, any time.
Both are saved like everything else; off, the sound is exactly what it was without them.

### De-harsh: hard attacks around 2.7 kHz

**What it does.** A notch in the live EQ cuts 2.7 kHz all the time, also in the quiet passages
where nothing is harsh, and the program loses some presence. De-harsh watches one band, about an
octave wide around **De-harsh freq**, and cuts it **only while that band is louder than De-harsh
thresh**: a hard attack (a snare crack, a hard consonant, a synth stab) is pulled down within a
millisecond, then let go within 80 ms; everything quieter passes exactly as it is. Above the
threshold it cuts 3:1: 9 dB over the threshold becomes 3 dB over, a 6 dB cut. It acts on the tops
and the subs alike (in practice only the tops play 2.7 kHz), and on the AUX too.

**The threshold follows the master.** The engine applies the master before anything else, so
de-harsh reads the program at the level you play it: turn the master down and it acts less (the
attacks are less harsh then too); turn it up and it acts more. **Set it at show level.** The input
trim and the source level move it the same way. It reads the left and right bands added together,
so a centred source reads 6 dB over one side's level: a number, not a dBFS meter reading.

**Step by step:**

1. Before the doors, muted: SETUP › TOPS › **De-harsh**, turn E3 to `ON`, push E3 (`MAY CLICK`).
2. SETUP › TOPS › **De-harsh thresh** to **0 dB** (turn E3 up): at 0 dB it never acts. Unmute and
   play the track whose attacks hurt, at show level.
3. If a live bell already cuts there (the notch from the first run), switch it OUT (B3 hold on
   SHOW, or its chip held on the phone) so you hear de-harsh alone.
4. **De-harsh freq**: leave `2.70k` if that is where the harshness was (the notch's frequency
   tells you). If it is elsewhere, move it with E3 (1/12 octave a click) while the threshold is
   low (step 5), to where the attacks soften most.
5. **Lower De-harsh thresh** a few clicks at a time (0.5 dB a click), listening to the attacks
   only: they get softer, the rest stays. Stop when they no longer bite.
6. **Check what you gave up**: listen to a quiet passage, a voice, cymbals. Dull, lisping, or the
   sound "breathes" between hits? Two to four clicks back up. Normal: somewhere between −30 and
   −15 dB with a loud program; the default is −24.
7. Compare: De-harsh `off` / `ON` (an E3 turn and an E3 push each, between tracks), or the threshold back to 0 dB
   and down again (no click). Keep it only if the difference is clearly better.

If it never seems to act: the threshold is too high for this program level (lower it), or the
harshness is elsewhere (move the frequency). If everything sounds duller even between attacks:
the threshold is far too low; come back up. The live EQ notch is still there if de-harsh does not
convince you: it is the static fix, de-harsh the dynamic one.

### Sub loudness: the subs at low level

**What it does.** Ears hear less low end at low volume (equal-loudness curves), and subs need
some level to fill a room: at background level the cabinets sound flat. Loudness raises the subs'
low end (a shelf below 70 Hz) **as the master goes down under Loudness ref**, by **Loudness boost**
when the master is 20 dB under it, half of that 10 dB under. At or above the reference it adds
nothing, so the show level is untouched. It never makes the subs louder than they are at the
reference: the master always takes away more than loudness gives back (0.6 dB per dB at most).
The tops are not touched.

**Step by step:**

1. Before the doors, muted: SETUP › SUBS › **Loudness**, turn E3 to `ON`, push E3 (`MAY CLICK`).
2. Play the show music and set the master where the subs **sound right**: full, filling the room
   (usually the show level, or a little under). Read the master on SHOW or ALIGN.
3. SETUP › SUBS › **Loudness ref** to that master value (E3, 0.5 dB a click; default −10).
4. Turn the master down to the quietest level you will use (background, between sets) and listen
   to the subs. Too thin: **Loudness boost** up (+0.5 dB a click; default +6); boomy or the subs
   stand out: down. Typical: +4 to +8 dB.
5. Sweep the master slowly between the two levels: the balance between subs and tops should stay
   about the same. The boost changes smoothly with the master, no step.
6. Then leave the reference alone: moving it later changes how much low end every quieter level
   gets.

Above the reference nothing changes, so a show at full level sounds exactly as without loudness.
On the phone, raising the boost or the reference shares master's 6 dB/s limit (they raise the
subs).

### AUX IN: a mic or a second source on the M4's front inputs

SETUP › INPUT › **AUX IN 1/2** puts the M4's IN1 (`MIC`, both sides at −3 dB, a 100 Hz high-pass
against handling noise and boom) or IN1 + IN2 (`LINE`, IN1 left, IN2 right) into the program,
after the input trim and before the EQ, so it gets the room EQ, the live EQ and de-harsh. Off by
default. **A live mic can feed back**: the orange `AUX MIC` pill says it is open.

1. Master down. On the M4 front: **GAIN 1/2 at minimum**, **MON 1 and MON 2 stay unlit** (a lit
   MON sends the input straight to the outputs, past K1000 and its mute), 48V only for a
   condenser mic.
2. Muted: AUX IN 1/2 → `MIC` or `LINE`, push (`MAY CLICK`); unmute.
3. Raise the **M4 GAIN** while someone talks or the source plays, until the M4's own IN1 / IN2
   meter is healthy without red; then **AUX level** (SETUP › INPUT, 0.5 dB a click, default 0 dB)
   to sit it in the mix, then the master. K1000's IN meters, `IN CLIP` and the spectrum read the
   program inputs (LINE IN 3/4) only, never the AUX: watch the M4's front meters for it.
4. Mic in front of the speakers: listen for a ring as you go. The notch (§8) cuts it like any
   other ring, but the spectrum does not see the mic, so there is no cyan PEAK for it: B3, then
   **cut first and sweep** (the `NO PEAK` way).
5. Done: AUX IN back to `off` (muted), M4 GAIN 1/2 back to minimum.

## 10. After the tune

- Everything is saved automatically. Note the key values on paper anyway (master show level,
  delays, Lo-Cut, the eight system bands).
- Copy `/var/lib/k1000/state.json` off the box: it is the tune ([chapter 7](07-maintenance.md)).
- Do a two-hour program run and read the soak summary before the first show.
