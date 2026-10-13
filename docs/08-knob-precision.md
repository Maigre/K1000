# 08 — Knob precision: diagnosis, trace tool, decoder fix

Thomas, 2026-10-12 evening, after using the v0 panel: *"Knobs are at the right place but they feel
not precise, maybe a better debounce is necessary?"*

Status: **applied in the repo on 2026-10-12 night** for UI v1 (D-009): patches A and B of §6,
plus one amendment to the decoder (glitches, §9) and the 100 ms fader ramp (§7); a second
amendment after the v1 review (2026-10-13, §10: glitches whose first edge is one contact, and
fast turns losing clicks to the GIL, a storm misclassification and in-loop saves). The trace that
confirms the knob type is **not run yet** (§4: about 2 minutes, needs Thomas's hands and k1000
stopped). Tool: `box/tools/k1000-knobtrace.py`, tests: `box/tools/tests/test_knobtrace.py`.

## 1. Short answer

**It is not a debounce problem.** The decoder already throws contact bounce away by construction.
Three things are more likely to make the knobs feel imprecise:

1. **The v0 decoder's pause rule miscounts slow turns.** When the knob stays still for 0.3 s
   anywhere other than the state it expects a click on, the decoder counts the half-finished
   click and moves its reference to that position. Turn slowly with a short pause at the top of
   each click, and every click counts twice. A slow click is counted 0.3 s after it lands.
   Brisk turns are exact, so the error shows up in exactly the slow, careful gesture used for
   notching. The code and synthetic replays confirm this (§3).
2. **The setting for contact transitions per click may be wrong for this hardware.** The driver
   assumes 4. If these encoders have 2, the v0 decoder gives one step per two clicks on a brisk
   turn and one late step per click on a slow one. The evidence is mixed (§2, row 2). The trace
   settles it in 30 seconds.
3. **MASTER and BALANCE lag by ear.** The engine moves every fader change over 300 ms and starts
   that ramp again on each new click, so the level is still changing after the hand has stopped.
   This is an engine setting, not a knob problem (§7).

Acceleration adds jumps on FREQ, LO-CUT and MASTER-down (§2, row 4). The fix comes in two patches:
the decoder counts a click when the knob lands in it and never on a timer, and both the number of
transitions per click and the click position become configurable per encoder (Patch A, §6).
Patch B makes the acceleration gentler. Apply Patch A after the trace, with the settings it
prints.

## 2. Hypotheses, ranked

| # | Hypothesis | Verdict | Evidence |
|---|---|---|---|
| 1 | **v0 decoder pause rule** (`QuadratureDecoder.settle`): after a 0.3 s pause away from the expected detent state, it rounds the half-done click up, emits it, and re-anchors on that state | **Confirmed in code and in replay**; the size of its effect on Thomas's turns needs the trace | Synthetic replay through the deployed decoder (an exact copy is kept in the tool as `V0Decoder` and checked against the deployed class on 300 random walks). 4-transition knob, 5 clicks with a 0.5 s pause at the top of each: **+10**, all emitted by the timer. 4 very slow clicks (0.5 s between contact changes): +4, each 0.3 s **after** it lands. Half a click, a pause, then back: **+1 then −1**. Starting off-detent, as enc0 (00) and enc3 (10) did this morning: steps land mid-click and the first long rest adds an extra step. Brisk clicks are always exact. This matches "precise when quick, wrong when careful". |
| 2 | **(a) 2 transitions per click** (24 detents / 12 pulses) | **Open: decided by the trace.** Less likely than #1, roughly 1 in 4 | *For:* this morning the four encoders idled at AB 00, 11, 11, 10 (`mcp23017.py` docstring, 5858 reads). A 4-transition knob always rests on 11, both contacts open. Bourns' PEC11R, a common 12 mm part (the V4's exact part is unknown), comes with 24 detents in both 12- and 24-pulse versions (datasheet "detent option 2 = 24 detents, 12 or 24 pulses"). *Against:* tonight at 19:45, after Thomas's session, a read-only snapshot showed **all four at 11**. enc0 and enc3 had moved from 00 and 10 to 11, which a 2-transition knob does only by luck (detents alternate 00 and 11). The V4's own firmware counts one step per full cycle: zyncoder `oram` emits on long_history 0xd4/0xe8, and `stable-2109` has `ZYNCODER_TICKS_PER_RETENT 4`. The Zynthian team says "we always use 24 detent per rotation for all kits", and a DIY user with half-step encoders had to patch TICKS_PER_RETENT to 2. *If it is true:* with v0, a brisk turn gives half the steps and a turn with pauses gives one late step per click, so the speed changes the result. |
| 3 | **(h) MASTER and BALANCE lag**: 300 ms fader ramp | **Confirmed in config and engine source**; not a knob fault | `/var/lib/k1000/active.yml`: `volume_ramp_time_ms: 300.0` (Main) and `ramp_time_ms: 300.0` (Aux1/Aux2 faders). CamillaDSP `basicfilters.rs` `prepare_processing`: on every target change, `ramp_start = current_volume` and a full ramp starts, linear in dB. One click finishes 0.3 s later, and a continuous turn trails the hand by about 0.3 s. |
| 4 | **(b) Acceleration starts too early and reacts to one interval** | **Likely contributor** on MASTER-down, FREQ and LO-CUT only | `input_map._accel`: multiplier above 12 det/s (an 83 ms interval), from a **single** detent interval, cap ×4 (×2 for MASTER up). A brisk turn of a 24-click knob runs 10–20 clicks/s with uneven intervals, so one short interval jumps ×2–×3 mid-turn (FREQ ×2 = 1/6 oct, MASTER ×2 = 1 dB). GAIN, Q, BALANCE, SUB DELAY and slot never accelerate. |
| 5 | **(e) Visual latency** | **Moderate**: a lag, not imprecision | Bench on the box (`k1000-knobtrace.py bench`): a MAIN frame takes **6.9 ms** on average, **15.1 ms** at most (render + RGB565). The render loop runs at most 15 fps (up to 67 ms wait; while music plays the meters already ask for frames at 10 Hz). fbtft deferred I/O runs at 30 fps (up to 33 ms) and SPI at 64 MHz (≈40 ms for a full 300 KiB frame). Estimated knob-to-pixels time is 50–150 ms, about 90 ms typical. **Not measured end to end.** |
| 6 | **(c) Missed transitions** (I2C sampling, interrupt handling) | **Unlikely at normal speed**; possible on hard spins | The driver wakes on the INTA/INTB edge (gpiod) and reads GPPU..GPIO in one 8-byte transaction (≈1.1 ms at the box's default 100 kHz i2c-1). It feeds INTCAP, then GPIO, and the state table infers ±2 for a skipped state, so ≤2 lost transitions per sample are absorbed. Bench: wake-up delay of a sleeping thread while frames render (GIL contention) averages **0.10 ms**, p99 **0.74 ms**, max **4.8 ms**. The 50 ms resync poll (5 ms only without INT lines or during a storm) only matters when an edge is missed. Today's journal (service starts 18:21 and 18:38) has **no I2C errors, interrupt storms, polling fallback or reconfigs**. The driver does not log invalid-transition counts, so the logs cannot rule this out. The trace measures it: fastest contact change, invalid changes, and irq vs 1 kHz poll. **Superseded (§10): that bench rendered v0's MAIN in one thread, with no event loop, spectrum or saves, and timed a sleeping thread, not the worker. Under v1's real thread layout the worker lost clicks from 25 clicks/s (24 % at 45) until PERF-1/2/3.** |
| 7 | **(d) Contact bounce** | **Not the cause**; debounce is not the lever | The full state table turns a bounce x→y→x into +1−1, which nets to zero (existing tests include a random walk with 30 % bounces). A time debounce like zyncoder's 0.3 ms drop would only remove real transitions on fast turns and add latency. Only a bounce that flips both contacts between two samples with no known direction can miscount, and the trace counts those ("invalid without a direction"). The Zynthian team requires 10 nF caps because their newer decoder has no bounce tolerance; ours has it by design. The switches keep their 10 ms trailing debounce (correct, unchanged). |
| 8 | **(f) Step sizes** | **Not the cause** | 0.5 dB master and gain, 1/12 oct frequency, Q 1/6 oct, push for fine (1 sample, 1/48 oct): normal digital-console steps. |

## 3. What the v0 decoder does wrong, precisely

`QuadratureDecoder` (v0) adds each valid contact transition to `acc` and emits a step when the
contacts come back to its **anchor**. With 4 transitions per click on the V4 knobs, the anchor is
the detent state, 11 (both contacts open). That part is right, and matches zyncoder.

The anchor is learned rather than fixed. At start it is whatever the contacts read; enc0 read 00
this morning. Then `settle()` runs 0.3 s after the last change whenever `acc` ≠ 0 or the state is
not the anchor. It **emits `acc` rounded half-up** and **makes the current state the anchor**. A
click on a detented knob climbs slowly to the top of the detent hill (state 00, two transitions)
and then snaps down. A hand that stops at the top for 0.3 s therefore gets one step emitted at the
hilltop, plus a re-anchor on 00. The snap into 11 then leaves `acc = +2` off the new anchor, and
0.3 s later that becomes a second step, with the anchor moved back to 11. That is two steps for
one click, both on a timer. Other pause positions give a late step, or a step followed by a
reversed one. The trace's "pauses ≥ 0.3 s between two clicks" line counts how often Thomas's
hand does this, and the "v0 decoder" rows show what it cost.

**The fix (Patch A).** A click counts the moment the knob lands in a detent state. A pause never
counts by itself:

- **The detent state is configured, not guessed:** `detent_ab`, default `"11"` like zyncoder; `"idle"`
  keeps the v0 start behaviour. `steps_per_detent` is set per physical encoder.
- **When the knob stops off a detent for 0.3 s, nothing is emitted, with one exception.** If it
  stopped 3/4 of the way, the click is completed and the remainder is kept, so it cannot be counted
  again when the knob moves on. This covers a lost last transition, or a worn contact that reads
  one quarter-step off at its detent.
- **A mis-set detent heals itself only after 3 long rests (≥ 1 s) in a row on the same other
  state,** with no stop on a known detent in between. A finger hovering between clicks never meets
  that condition. The new position is logged and survives I2C re-seeds.
- **Running a 2-transition knob at spd 4 is never "fixed" automatically.** On such a knob, rests
  alternate between 11 and 00. A careful hand that pauses at the top of every click on a
  4-transition knob produces the same pattern, so the decoder cannot tell the two apart. After 8
  such rests in a row it writes one log line ("does it need steps_per_detent 2?"); the trace,
  where the click count is known, decides.

Replayed on the same synthetic traces (rows asserted in the tests):

| Gesture | v0 | fixed |
|---|---|---|
| 10 brisk clicks (4-transition knob) | +10 | +10 |
| 5 clicks with a 0.5 s pause at the top of each | **+10**, all by the timer | +5, each on landing |
| 4 very slow clicks (0.5 s between contact changes) | +4, each 0.3 s late | +4, on landing or at 3/4 |
| half a click, pause, back | +1 then −1 | nothing |
| stop at 3/4 (worn contact), then move on | +1 late | +1 at the stop, never again |
| detent really at 00 while configured 11 | n/a (v0 anchors on the start level) | right counts; learns 00 after 3 long rests |
| 2-transition knob, spd 4 (brisk / with pauses) | +5 / +10, half of them late, for 10 clicks | +5 / +5, plus a journal hint; with spd 2: +10 |

## 4. The trace: what Thomas runs (about 2 minutes)

Not during a show. While k1000 is stopped, the engine keeps playing at its current settings and
its faders do not move (proven by the drills). **B2 MUTE does nothing until k1000 is started
again**, so keep the source down or the Meyers off if in doubt. The panel shows K1000's stop frame.

```sh
ssh k1000@<box>                       # as for the soak
sudo systemctl stop k1000
python3 /home/k1000/dev/knobs/k1000-knobtrace.py capture --guided -o ~/knobs-1.trace
sudo systemctl start k1000
```

After the next `install.sh` the tool is also at `/opt/k1000/tools/k1000-knobtrace.py`. It refuses
to start while k1000 runs and touches nothing in that case (verified on the box). For each prompt,
press Enter, do the turn, then press Enter again. Count the clicks exactly: the click count is
the ground truth. `r` redoes a step and `q` stops. After each step it prints the transitions per
click it saw.

1. E1 (top-left): 10 clicks clockwise, slowly, stopping on each click.
2. E1: 10 clicks counter-clockwise.
3. E2, E3, E4: 10 clicks clockwise each.
4. E1: 5 clicks clockwise *very* slowly, as if fine-tuning a notch (about 2 s per click).
5. E1: fast spins, half a turn back and forth, three times.
6. Steps 1 and 5 again with 1 kHz polling instead of interrupts. The tool switches by itself.

Steps 1–5 sample INT-driven exactly like the driver: same classes, register config and
INTCAP-then-GPIO order, imported from the installed tree, with the v1 service's CPU load beside
it (SHOW + spectrum rendered in a thread, an asyncio loop thread with the ballistics and save
chunks; §10) so the sampler competes for the GIL as the service's worker does.

The end of the run prints the analysis and the exact `controls:` lines for k1000.yaml. To re-read
it later: `python3 …/k1000-knobtrace.py analyze ~/knobs-1.trace`. Once Patch A is installed, the
same command also replays the trace through the fixed decoder. `--quick` covers E1 only (about 1
minute). `bench` drives the driver's real worker on a simulated chip beside the v1 load
(§10), without touching the chip.

**How to read it:**

| The report says | Then |
|---|---|
| `VERDICT: steps_per_detent 4, detent AB 11` on all four (expected) | Apply Patch A as is (its defaults) |
| `steps_per_detent 2` on an encoder | Patch A plus `controls: steps_per_detent: [...]` in `/etc/k1000/k1000.yaml` (the line the report prints) |
| `detent AB` other than 11 | Patch A plus `detent_ab` for that encoder |
| `DIRECTION REVERSED` on a step | `controls: reverse` for that E |
| Many "pauses ≥ 0.3 s between two clicks" and the v0 rows over the click count | Hypothesis #1 is what Thomas felt |
| `invalid … without a direction` > 0, or irq and poll disagree on transitions per click | Sampling (#6): try `dtparam=i2c_arm_baudrate=400000` (boot config, needs a reboot; not before Friday unless needed) and `sys.setswitchinterval(0.001)` in the service |
| Many bounces | Still harmless to the count (#7); note it for the hardware log |
| Click-to-click rate over 20/s in more than 10 % of slow-step intervals | Raise Patch B's onset further |

Trace format (one line per level change, replayable): see the tool's module docstring. A guided
run is a few hundred kilobytes at most.

## 5. Evidence log (2026-10-12)

- **Code:** `box/k1000/hw/mcp23017.py` and `ui/input_map.py` as deployed. The box's
  `/opt/k1000` is identical to the repo (md5 of mcp23017.py, input_map.py, `__main__.py` and
  control.py; REVISION ca742e7). `open_controls()` builds `ZynthianControls()` with no options:
  spd 4, idle 300 ms, poll 50 ms.
- **Journal** (`journalctl -u k1000`, today, one boot): starts at 18:21 and 18:38, knobs with
  `irq=True`, then mute on/off at 19:29, mono subs at 19:30, and six Lo-Cut toggles at 19:32. No
  knob warnings of any kind. The service does not log nudges, so the knob turns themselves leave
  no trace.
- **GPIO snapshot, 19:45:** ten **read-only** reads of GPIOA/GPIOB (2 bytes, no writes) while
  k1000 ran: `0xff 0xff` every time, all four encoders at AB 11. This was the only access to the
  live chip, and a read like this is harmless to the driver: at worst it clears a pending INT,
  which the 50 ms resync poll recovers.
- **zyncoder** (github.com/zynthian/zyncoder, branches `oram` and `stable-2109`): see row 2. In
  current zyncoder, a time debounce drops any change less than 0.3 ms after the previous one.
- **Bourns PEC11R datasheet:** contact bounce ≤ 2 ms at 15 RPM *with* the suggested 10 k / 10 nF
  filter; 60 RPM max; 24-detent parts come with 12 or 24 pulses. Which part and which filter the
  V4 board has is **not known**.
- **Zynthian forum:** "Encoders gone crazy after update" (the team asks for 10 nF caps, which its
  new decoder needs); "Accuracy of V4.4 LCD display and encoders" (a V4.4 owner: "sometimes it
  goes the other way before going the right way", answered as low-cost encoders).
- **Bench on the box** (CPU only, `nice 19`, 4 s): see rows 5 and 6.
- **CamillaDSP source** (`src/filters/basicfilters.rs`, master): see row 3.

## 6. The fix, as patches (applied 2026-10-12 night; see §9 for what changed on top)

Both patches apply cleanly to the working tree of 2026-10-12, 20:20
(`git apply --check`). With both applied, the whole box suite passes locally: **462 passed** (all
of `box/tests` plus `tools/tests/test_knobtrace.py`, Python 3.13). The tool's 21 tests also pass
on the box.

```sh
cd /home/mgr/Bakery/K1000
# save the two diff blocks below as /tmp/knob-A.patch and /tmp/knob-B.patch, then:
git apply /tmp/knob-A.patch && git apply /tmp/knob-B.patch
cd box && python3 -m pytest -q -p no:cacheprovider tests tools/tests/test_knobtrace.py
```

**Patch A: decoder, per-encoder options, wiring, tests.**

- **`box/k1000/hw/mcp23017.py`:**
  - The new `QuadratureDecoder`, with `rest=` / `learn_rests=` / `learn_s=` and the
    `relearned` / `half_rests` counters.
  - `ZynthianControls(steps_per_detent=int|list, detent_ab="11"|list, learn_rests=3)`.
  - `parse_detent`, `steps_per_detent_list`, `detent_list`, and `controls_options(cfg)`, which
    is lenient like `ControlMap.from_cfg`: a typo logs an error and keeps that key's default.
  - Two one-time journal lines (a detent learned; the half-rest hint).
  - `--detent-ab` for the `--monitor` CLI.
- **`box/tests/test_mcp23017.py`:** one v0 test replaced (it asserted the re-anchor). One updated
  (anchors are now 11, not the idle levels). 13 new tests: the gestures of §3, learning,
  never-spin deadlines, per-encoder options, the YAML parser, the journal hint, and the service
  wiring.
- **`box/k1000/__main__.py`:** one line, `ZynthianControls(**controls_options(settings))`.
- **`box/etc/k1000.yaml`:** two documented keys with the defaults. The live
  `/etc/k1000/k1000.yaml` does not need them unless the trace says otherwise; the code defaults
  are the same.

**Patch B: acceleration** (`ui/input_map.py`, `tests/test_ui.py`):

- The onset goes from 12 to 20 det/s.
- The rate is the slower of the **last two** detent intervals, so one quick interval never jumps
  and the first two detents of a turn are ×1.
- MASTER up is never accelerated; MASTER down keeps ×4 for a hard spin.

The UI rework (EQ on MAIN, SUB DELAY and LO-CUT moved off MAIN) touches the same `KNOBS` and
`Focus` lines. If it lands first, re-apply Patch B by hand: three constants, the `turn_dt` field,
two lines in `map_event`, and `_accel`.

Patch A is independent of the UI work. Its only file outside the driver is a one-line change in
`open_controls()`. After deploying, k1000 must be restarted to load the new driver.

### Patch A

```diff
--- a/box/k1000/hw/mcp23017.py
+++ b/box/k1000/hw/mcp23017.py
@@ -11,13 +11,18 @@
 Decoding reference: zynthian/zyncoder, branch `oram` (commit cbd93aa1), zyncoder.c
 `update_zyncoder()` + `valid_quadrant_states[]`: it keeps a 2-transition history and emits +/-1
 only on the last transition of a complete quadrature cycle back into the rest state
-(long_history 0xd4 = CW, 0xe8 = CCW), i.e. **4 transitions per detent**, CW = A changes first.
-That is our default (`steps_per_detent=4`); 2 and 1 are configurable.
-
-Deviation from zyncoder, on purpose: zyncoder assumes the rest state is "both contacts open"
-(A=B=1). On this box the idle levels are enc0 AB=00, enc1 11, enc2 11, enc3 10 (stable over
-5858 reads), so a fixed rest state would mis-count. We **anchor** each decoder on its observed
-idle state and re-anchor when the knob sits still on a state we did not expect (self-healing).
+(long_history 0xd4 = CW, 0xe8 = CCW), i.e. **4 transitions per detent**, CW = A changes first
+(older zyncoder: ZYNCODER_TICKS_PER_RETENT 4). That is our default (`steps_per_detent=4`); 2 and
+1 are configurable per encoder (k1000.yaml `controls: steps_per_detent`), and
+box/tools/k1000-knobtrace.py measures which one the knobs need.
+
+Detent (rest) state: like zyncoder, a detent is "both contacts open" (AB=11) by default, per
+encoder in k1000.yaml `controls: detent_ab` (docs/08-knob-precision.md). The v0 driver anchored
+each decoder on the level read at start instead (the box once idled at enc0 AB=00, enc1 11, enc2
+11, enc3 10; all four read 11 after use on 2026-10-12) and re-anchored after any 0.3 s pause off
+that state: a finger pausing at the top of a click then made the click count twice, a very slow
+turn counted late. Now a pause never counts by itself; a mis-set detent re-learns itself only
+after three long (>= 1 s) rests in a row on the same other state (QuadratureDecoder).
 
 Interrupt storms (proven on the box): while a pin is in compare-to-DEFVAL mode and mismatched, the
 real chip re-asserts INT right after every clearing read (2 fresh falling edges per INTF..GPIO block
@@ -156,32 +161,53 @@
 
     Every valid transition adds +/-1 to an accumulator; a skipped state (both bits flipped) adds
     +/-2 in the direction we were already going (or the direction of the last step if it was
-    recent), so a fast spin that outruns the I2C sampling still counts. Detents are emitted when
-    the encoder arrives at a rest state, rounded (half away from zero) to `steps_per_detent`, so
-    contact bounce (x -> y -> x) nets to zero and never emits.
+    recent), so a fast spin that outruns the I2C sampling still counts. Detents are emitted the
+    moment the encoder arrives in a rest state, rounded (half away from zero) to
+    `steps_per_detent`, so contact bounce (x -> y -> x) nets to zero and never emits.
 
     Rest states: spd=4 -> {anchor}; spd=2 -> {anchor, anchor^3}; spd=1 -> every state. The anchor
-    starts as the idle state and is re-learned when the knob sits still for `idle_s` on a
-    non-rest state (an anchor learned off-detent heals itself on the next real detent).
+    is `rest`, the AB state of a detent (a property of the encoder: 3 = both contacts open), or
+    with rest=None the state at reset().
+
+    A pause never counts by itself. A knob still for `idle_s` off a rest state is a finger between
+    two clicks: nothing is emitted, except from 3/4 of the way (spd 4: a lost last transition,
+    or a worn contact reading one quarter-step off at its detent), and then the remainder is kept
+    so that click is not counted again when the knob moves on. With `learn_rests` the anchor
+    moves to a state the knob has rested on for `learn_s` that many times in a row, with no stop
+    on a rest state between: a mis-set `rest` heals itself; a finger hovering between clicks,
+    which stops on a detent in between, never moves it.
     """
 
     def __init__(self, steps_per_detent: int = 4, state: int = 3, now: float = 0.0,
-                 idle_s: float = 0.3, infer_s: float = 0.1) -> None:
+                 idle_s: float = 0.3, infer_s: float = 0.1, rest: Optional[int] = None,
+                 learn_rests: int = 0, learn_s: float = 1.0) -> None:
         if steps_per_detent not in (1, 2, 4):
             raise ValueError("steps_per_detent must be 1, 2 or 4")
         self.spd = steps_per_detent
         self.idle_s = idle_s
         self.infer_s = infer_s
-        self.invalid = 0  # skipped states we could not attribute a direction to
+        self.rest = None if rest is None else rest & 3
+        self.learn_rests = max(0, int(learn_rests))
+        self.learn_s = max(idle_s, learn_s)
+        self.invalid = 0     # skipped states we could not attribute a direction to
+        self.relearned = 0   # anchor moves by learn_rests
+        self.half_rests = 0  # rests in a row half a detent apart (a 2-transition encoder at spd 4?)
         self.reset(state, now)
 
     def reset(self, state: int, now: float = 0.0) -> None:
-        """Adopt `state` as the current (and anchor) position without emitting anything."""
+        """Adopt `state` as the current position without emitting anything. The anchor is `rest`
+        (configured or learned), else `state`."""
         self.state = state & 3
-        self.anchor = self.state
+        self.anchor = self.state if self.rest is None else self.rest
         self.acc = 0
         self.last_change = now
         self.last_dir = 0
+        self._settled = True              # nothing to decide until the knob moves
+        self._vote = False                # a learn vote is due at last_change + learn_s
+        self._cand: Optional[int] = None  # learn: the state voted for, and how many times
+        self._cand_n = 0
+        self._moved = 0                   # net quarter-steps since the last stop (half_rests)
+        self._last_stop = self.state
 
     def is_rest(self, state: int) -> bool:
         if self.spd == 1:
@@ -198,6 +224,7 @@
         step = _QSTEP[(self.state << 2) | state]
         prev_change, self.last_change = self.last_change, now
         self.state = state
+        self._settled, self._vote = False, False
         if step == _INV:
             if self.acc:
                 step = 2 if self.acc > 0 else -2
@@ -207,6 +234,7 @@
                 self.invalid += 1
                 return 0
         self.acc += step
+        self._moved += step
         self.last_dir = 1 if step > 0 else -1
         if self.is_rest(state):
             return self._flush()
@@ -219,19 +247,59 @@
 
     def deadline(self) -> Optional[float]:
         """When settle() has work to do (None = nothing pending)."""
-        if self.spd == 1 or (self.acc == 0 and self.is_rest(self.state)):
+        if self.spd == 1:
             return None
-        return self.last_change + self.idle_s
+        if not self._settled:
+            return self.last_change + self.idle_s
+        if self._vote:
+            return self.last_change + self.learn_s
+        return None
 
     def settle(self, now: float) -> int:
-        """Knob still for idle_s off a rest state: it is in a detent we did not know -> adopt it."""
-        d = self.deadline()
-        if d is None or now < d:
-            return 0
-        n = self._flush()
-        self.anchor = self.state
+        """Call at deadline(): the knob has been still. Returns detents to emit (rarely any)."""
+        n = 0
+        if not self._settled:
+            if now < self.last_change + self.idle_s:
+                return 0
+            self._settled = True
+            n = self._stopped()
+            self._vote = bool(self.learn_rests) and not self.is_rest(self.state)
+        if self._vote and now >= self.last_change + self.learn_s:
+            self._vote = False
+            self._learn(self.state)
         return n
 
+    def _stopped(self) -> int:
+        n = 0
+        if self.is_rest(self.state):
+            self._cand, self._cand_n = None, 0      # a stop on a known detent confirms the anchor
+        else:
+            mag = (abs(self.acc) + self.spd // 4) // self.spd   # spd 4: only from 3/4
+            n = mag if self.acc > 0 else -mag
+            self.acc -= n * self.spd
+        # A 2-transition encoder decoded at spd 4 stops alternately on the anchor and anchor^3,
+        # two transitions apart. A finger pausing at the top of every click looks the same, so
+        # this only feeds a log hint (ZynthianControls), never the decoding.
+        if self.spd == 4 and abs(self._moved) == 2 and self.state == self._last_stop ^ 3:
+            self.half_rests += 1
+        elif self._moved:
+            self.half_rests = 0
+        self._last_stop, self._moved = self.state, 0
+        return n
+
+    def _learn(self, state: int) -> None:
+        if state == self._cand:
+            self._cand_n += 1
+        else:
+            self._cand, self._cand_n = state, 1
+        if self._cand_n >= self.learn_rests:
+            self.anchor = state
+            if self.rest is not None:
+                self.rest = state                  # survives reset() (I2C fault re-seeds)
+            self.acc = 0                           # this click was counted on the old anchor
+            self._cand, self._cand_n = None, 0
+            self.relearned += 1
+
 
 # ---------------------------------------------------------------------------------------------
 # Switch debouncing + short/long classification (pure, unit-tested)
@@ -341,6 +409,73 @@
 
 
 # ---------------------------------------------------------------------------------------------
+# Encoder options (k1000.yaml `controls:`), per physical encoder enc0..enc3
+# ---------------------------------------------------------------------------------------------
+DetentRef = Union[int, str, None]
+DEFAULT_DETENT = "11"    # both contacts open (zyncoder's rest; all four knobs read it 2026-10-12)
+HALF_REST_HINT = 8       # QuadratureDecoder.half_rests that earns one log line per encoder
+
+
+def parse_detent(v: DetentRef) -> Optional[int]:
+    """'11' / '01' (A then B, 1 = open) or 0..3 -> AB state; 'idle' or None -> None (the level at
+    start, the v0 behaviour). Raises ValueError otherwise."""
+    if v is None:
+        return None
+    if isinstance(v, int) and not isinstance(v, bool):
+        if 0 <= v <= 3:
+            return v
+        raise ValueError(f"detent_ab {v!r}: want '11', '10', '01', '00' or 'idle'")
+    t = str(v).strip().lower()
+    if t == "idle":
+        return None
+    if len(t) == 2 and set(t) <= {"0", "1"}:
+        return int(t[0]) << 1 | int(t[1])
+    raise ValueError(f"detent_ab {v!r}: want '11', '10', '01', '00' or 'idle'")
+
+
+def _per_encoder(v: object, n: int, name: str) -> list:
+    if isinstance(v, (list, tuple)):
+        if len(v) != n:
+            raise ValueError(f"{name}: {n} values expected, got {len(v)}")
+        return list(v)
+    return [v] * n
+
+
+def steps_per_detent_list(v: object, n: int) -> tuple[int, ...]:
+    """1, 2 or 4, or one of those per encoder -> n values. Raises ValueError."""
+    vals = _per_encoder(v, n, "steps_per_detent")
+    if any(isinstance(x, bool) or x not in (1, 2, 4) for x in vals):
+        raise ValueError("steps_per_detent must be 1, 2 or 4")
+    return tuple(int(x) for x in vals)
+
+
+def detent_list(v: object, n: int) -> tuple[Optional[int], ...]:
+    """A detent_ab value, or one per encoder -> n AB states (None = idle). Raises ValueError."""
+    return tuple(parse_detent(x) for x in _per_encoder(v, n, "detent_ab"))
+
+
+def controls_options(cfg: object) -> dict:
+    """ZynthianControls keyword arguments from the k1000.yaml dict (`controls:` section):
+    steps_per_detent (1, 2 or 4, or a list per physical encoder) and detent_ab ('11'... or
+    'idle', or a list). Lenient like input_map.ControlMap.from_cfg: an invalid value is logged
+    and its default kept, so a typo never leaves the box without knobs."""
+    sec = cfg.get("controls") if isinstance(cfg, dict) else None
+    if not isinstance(sec, dict):
+        return {}
+    n = len(DEFAULT_ENCODERS)
+    out: dict = {}
+    for key, check in (("steps_per_detent", steps_per_detent_list), ("detent_ab", detent_list)):
+        if key not in sec:
+            continue
+        try:
+            check(sec[key], n)
+            out[key] = sec[key]
+        except (TypeError, ValueError) as e:
+            log.error("k1000.yaml controls.%s ignored (%s): using the default", key, e)
+    return out
+
+
+# ---------------------------------------------------------------------------------------------
 # Hardware access layers (injectable so unit tests run without hardware)
 # ---------------------------------------------------------------------------------------------
 class SMBusLike(Protocol):
@@ -491,7 +626,10 @@
     def __init__(self, bus: int = 1, addr: int = 0x20, int_a: int = 27, int_b: int = 4,
                  encoders: Sequence[tuple[PinRef, PinRef]] = DEFAULT_ENCODERS,
                  switches: Sequence[PinRef] = DEFAULT_SWITCHES, long_ms: float = 800, *,
-                 debounce_ms: float = 10, poll_ms: float = 50, steps_per_detent: int = 4,
+                 debounce_ms: float = 10, poll_ms: float = 50,
+                 steps_per_detent: "int | Sequence[int]" = 4,
+                 detent_ab: "DetentRef | Sequence[DetentRef]" = DEFAULT_DETENT,
+                 learn_rests: int = 3,
                  idle_ms: float = 300, gpiochip: str = "/dev/gpiochip0", use_irq: bool = True,
                  bus_factory: Callable[[int], SMBusLike] = _default_bus,
                  lines_factory: Optional[Callable[[str, Sequence[int]], IntLines]] = None,
@@ -504,9 +642,8 @@
         used = [p for ab in self.encoder_pins for p in ab] + self.switch_pins
         if len(set(used)) != len(used):
             raise ValueError("an MCP23017 pin is assigned twice in encoders/switches")
-        if steps_per_detent not in (1, 2, 4):
-            raise ValueError("steps_per_detent must be 1, 2 or 4")
-        self.steps_per_detent = steps_per_detent
+        self.steps_per_detent = steps_per_detent_list(steps_per_detent, len(self.encoder_pins))
+        self.detent_ab = detent_list(detent_ab, len(self.encoder_pins))
         self.long_s = _clamp(long_ms, 200, 5000) / 1000.0
         self.debounce_s = _clamp(debounce_ms, 1, 50) / 1000.0
         self.poll_s = _clamp(poll_ms, 5, 1000) / 1000.0
@@ -519,8 +656,10 @@
         self.on_raw = on_raw
 
         now = clock()
-        self._decoders = [QuadratureDecoder(steps_per_detent, 3, now, self.idle_s)
-                          for _ in self.encoder_pins]
+        self._decoders = [QuadratureDecoder(spd, 3, now, self.idle_s, rest=rest,
+                                            learn_rests=learn_rests)
+                          for spd, rest in zip(self.steps_per_detent, self.detent_ab)]
+        self._hinted: set[int] = set()        # encoders whose half-rest hint was logged
         self._switches = [SwitchDebouncer(self.debounce_s, self.long_s, False, now)
                           for _ in self.switch_pins]
         self.levels = 0xFFFF  # last sampled B<<8|A pin levels
@@ -875,9 +1014,19 @@
 
     def _tick(self, now: float) -> None:
         for i, dec in enumerate(self._decoders):
+            learned = dec.relearned
             d = dec.settle(now)
             if d:
                 self._emit(EncoderEvent(i, d, now))
+            if dec.relearned != learned:
+                self.stats["detents_learned"] += 1
+                log.warning("enc%d: detent learned at AB=%s after %d long rests there "
+                            "(set controls.detent_ab to keep it)", i,
+                            f"{dec.anchor:02b}", dec.learn_rests)
+            if dec.half_rests >= HALF_REST_HINT and i not in self._hinted:
+                self._hinted.add(i)
+                log.warning("enc%d: %d stops in a row half a click apart: does it need "
+                            "steps_per_detent 2? (box/tools/k1000-knobtrace.py)", i, dec.half_rests)
         for i, sw in enumerate(self._switches):
             for kind in sw.tick(now):
                 self._emit(SwitchEvent(i, kind, now))
@@ -1093,7 +1242,7 @@
 
     controls = ZynthianControls(args.bus, args.addr, args.int_a, args.int_b,
                                 long_ms=args.long_ms, steps_per_detent=args.steps_per_detent,
-                                use_irq=not args.no_irq, on_raw=on_raw)
+                                detent_ab=args.detent_ab, use_irq=not args.no_irq, on_raw=on_raw)
     bursts = _Bursts(controls.encoder_pins)
     try:
         controls.open()                   # levels known, worker not yet running
@@ -1187,6 +1336,8 @@
     p.add_argument("--raw", action="store_true", help="also print every raw pin change")
     p.add_argument("--duration", type=float, default=0.0, help="stop after N seconds (0 = until Ctrl-C)")
     p.add_argument("--steps-per-detent", type=int, choices=(1, 2, 4), default=4)
+    p.add_argument("--detent-ab", default=DEFAULT_DETENT,
+                   help="AB levels at a detent: 11 (default), 10, 01, 00, or idle (level at start)")
     p.add_argument("--long-ms", type=float, default=800.0)
     p.add_argument("--bus", type=int, default=1)
     p.add_argument("--addr", type=lambda s: int(s, 0), default=0x20)
--- a/box/tests/test_mcp23017.py
+++ b/box/tests/test_mcp23017.py
@@ -22,7 +22,7 @@
 from k1000.hw.mcp23017 import (
     DEFAULT_ENCODERS, DEFAULT_SWITCHES, EXPECTED_CONFIG, LONG, PRESS, RELEASE, SHORT,
     Accelerator, EncoderEvent, Mcp23017, QuadratureDecoder, SwitchDebouncer, SwitchEvent,
-    ZynthianControls, accel_multiplier, pin_index,
+    ZynthianControls, accel_multiplier, controls_options, parse_detent, pin_index,
 )
 
 # ---------------------------------------------------------------------------------------------
@@ -294,20 +294,138 @@
         d = QuadratureDecoder(1, state=3)
         assert run_states(d, [1, 0, 0, 2, 0]) == [1, 1, 0, 1, -1]
 
-    def test_settle_reanchors_when_resting_off_anchor(self):
+    def test_a_pause_off_the_detent_never_moves_the_anchor(self):
         d = QuadratureDecoder(4, state=3, idle_s=0.3)
         assert d.update(1, 1.0) == 0          # one quarter step, then the knob rests there
         assert d.deadline() == pytest.approx(1.3)
         assert d.settle(1.2) == 0 and d.anchor == 3
-        assert d.settle(1.31) == 0            # 1/4 detent rounds to nothing
-        assert d.anchor == 1 and d.deadline() is None
-        assert run_states(d, [0, 2, 3, 1], t0=2.0) == [0, 0, 0, 1]   # detents now end at 01
+        assert d.settle(1.31) == 0            # 1/4 detent: nothing
+        assert d.anchor == 3 and d.deadline() is None and d.acc == 1
+        assert run_states(d, [0, 2, 3], t0=2.0) == [0, 0, 1]   # the click ends where it should
 
     def test_settle_flushes_three_quarters_as_one(self):
         d = QuadratureDecoder(4, state=3, idle_s=0.3)
         run_states(d, [1, 0, 2], t0=1.0)
         assert d.settle(2.0) == 1
 
+    # -- docs/08-knob-precision.md: pauses must never change the count ------------------------
+    @staticmethod
+    def drive(d: QuadratureDecoder, timeline) -> list[tuple[float, int]]:
+        """[(t, state)] with the driver's deadline-driven settle() in between -> emissions."""
+        out = []
+        for t, s in timeline:
+            while d.deadline() is not None and d.deadline() <= t:
+                dl = d.deadline()
+                n = d.settle(dl)
+                if n:
+                    out.append((dl, n))
+                if d.deadline() == dl:
+                    break
+            n = d.update(s, t)
+            if n:
+                out.append((t, n))
+        while d.deadline() is not None:
+            dl = d.deadline()
+            n = d.settle(dl)
+            if n:
+                out.append((dl, n))
+        return out
+
+    @staticmethod
+    def clicks(n, direction=1, start=3, t=1.0, gaps=(0.05, 0.05, 0.01, 0.01), hover_at=None,
+               hover_s=0.0, rest_s=0.6, spd=4):
+        """Timeline of `n` clicks: per-transition gaps, an optional pause after transition
+        `hover_at` (the top of the click), `rest_s` on each detent."""
+        ring = [3, 1, 0, 2]
+        state, out = start, []
+        for _ in range(n):
+            for k in range(spd):
+                t += gaps[k % len(gaps)]
+                state = ring[(ring.index(state) + direction) % 4]
+                out.append((round(t, 4), state))
+                if hover_at is not None and k + 1 == hover_at:
+                    t += hover_s
+            t += rest_s
+        return out
+
+    def test_pause_at_the_top_of_a_click_counts_once_on_arrival(self):
+        # v0 bug: a >= 0.3 s pause at AB=00 emitted the click there AND re-anchored, so every
+        # slow click counted twice
+        d = QuadratureDecoder(4, state=3, idle_s=0.3, rest=3, learn_rests=3)
+        tl = self.clicks(5, hover_at=2, hover_s=0.5)
+        out = self.drive(d, tl)
+        assert sum(n for _t, n in out) == 5
+        arrivals = [t for t, s in tl if s == 3]
+        assert [t for t, _n in out] == arrivals          # on the detent, not by a timer
+        assert d.anchor == 3 and d.relearned == 0
+
+    def test_very_slow_turn_counts_every_click_once_never_late(self):
+        # 0.5 s between contact changes: v0 counted each click 0.3 s after it ended; now it
+        # counts on arrival, or at 3/4 of the way when the knob is still there for idle_s
+        d = QuadratureDecoder(4, state=3, idle_s=0.3, rest=3, learn_rests=3)
+        tl = self.clicks(4, gaps=(0.5, 0.5, 0.5, 0.5))
+        out = self.drive(d, tl)
+        assert [n for _t, n in out] == [1, 1, 1, 1]
+        arrivals = [t for t, s in tl if s == 3]
+        assert all(a - 0.5 <= t <= a for (t, _n), a in zip(out, arrivals))
+        assert d.relearned == 0
+
+    def test_half_a_click_and_back_after_a_pause_is_nothing(self):
+        d = QuadratureDecoder(4, state=3, idle_s=0.3, rest=3)
+        out = self.drive(d, [(1.0, 1), (1.05, 0), (2.0, 1), (2.05, 3)])
+        assert out == []
+
+    def test_worn_contact_one_quarter_short_counts_once(self):
+        # the knob stops at AB=10 (3/4 of a click): counted after idle_s, and the last quarter
+        # (10 -> 11, when it moves on) does not count it again
+        d = QuadratureDecoder(4, state=3, idle_s=0.3, rest=3)
+        out = self.drive(d, [(1.0, 1), (1.01, 0), (1.02, 2), (2.0, 3), (2.01, 1), (2.02, 0),
+                             (2.03, 2), (2.04, 3)])
+        assert sum(n for _t, n in out) == 2 and out[0] == (pytest.approx(1.32), 1)
+
+    def test_mis_set_detent_is_learned_after_three_long_rests(self):
+        # an encoder whose detents are at AB=00 while the config says 11
+        d = QuadratureDecoder(4, state=0, idle_s=0.3, rest=3, learn_rests=3)
+        tl = self.clicks(6, start=0, rest_s=1.5)
+        out = self.drive(d, tl)
+        assert sum(n for _t, n in out) == 6               # never a wrong count meanwhile
+        assert d.anchor == 0 and d.relearned == 1 and d.rest == 0
+        d.reset(0, 50.0)
+        assert d.anchor == 0                              # an I2C re-seed keeps what was learned
+
+    def test_hovers_between_clicks_never_relearn(self):
+        d = QuadratureDecoder(4, state=3, idle_s=0.3, rest=3, learn_rests=3)
+        self.drive(d, self.clicks(8, hover_at=2, hover_s=0.8, rest_s=0.35))
+        assert d.anchor == 3 and d.relearned == 0
+
+    def test_two_transition_encoder_at_spd4_raises_the_hint_not_the_count(self):
+        d = QuadratureDecoder(4, state=3, idle_s=0.3, rest=3, learn_rests=3)
+        out = self.drive(d, self.clicks(10, spd=2, gaps=(0.04, 0.01)))
+        assert sum(n for _t, n in out) == 5               # half: the config must say 2
+        assert d.half_rests >= 8 and d.relearned == 0
+        d2 = QuadratureDecoder(2, state=3, idle_s=0.3, rest=3)
+        assert sum(n for _t, n in self.drive(d2, self.clicks(10, spd=2, gaps=(0.04, 0.01)))) == 10
+        assert d2.half_rests == 0
+
+    def test_spd2_pause_mid_click_counts_once(self):
+        d = QuadratureDecoder(2, state=3, idle_s=0.3, rest=3)
+        out = self.drive(d, self.clicks(4, spd=2, gaps=(0.05, 0.01), hover_at=1, hover_s=0.6))
+        assert [n for _t, n in out] == [1, 1, 1, 1]
+
+    def test_settle_never_spins(self):
+        d = QuadratureDecoder(4, state=3, idle_s=0.3, rest=3, learn_rests=3)
+        d.update(1, 1.0)
+        d.update(0, 1.01)                                 # half a click, then still
+        assert d.deadline() == pytest.approx(1.31)
+        d.settle(1.31)
+        assert d.deadline() == pytest.approx(2.01)        # the learn vote
+        d.settle(2.01)
+        assert d.deadline() is None
+        d0 = QuadratureDecoder(4, state=3, idle_s=0.3, rest=3)    # no learning: one wake
+        d0.update(1, 1.0)
+        d0.settle(1.3)
+        assert d0.deadline() is None
+
     def test_random_walk_with_bounces_matches_net_detents(self):
         rng = random.Random(1234)
         for spd in (4, 2, 1):
@@ -582,10 +700,11 @@
 
 
 def test_box_idle_levels_seed_decoders_without_events():
-    chip, clk = FakeChip(ext=0xEFF9), Clock()   # the levels read on the box today
+    chip, clk = FakeChip(ext=0xEFF9), Clock()   # the levels read on the box (morning 2026-10-12)
     c = make(chip, clk)
     c.open()
-    assert [d.anchor for d in c._decoders] == [0b00, 0b11, 0b11, 0b10]
+    assert [d.anchor for d in c._decoders] == [0b11] * 4       # detents: both contacts open
+    assert [d.state for d in c._decoders] == [0b00, 0b11, 0b11, 0b10]
     assert drain(c) == []
     ext = chip.ext
     for s in [2, 3, 1, 0]:                      # CW from 00: 00 -> 10 -> 11 -> 01 -> 00
@@ -596,6 +715,78 @@
     assert [(e.index, e.delta) for e in drain(c)] == [(0, 1)]
 
 
+def test_per_encoder_steps_and_detents():
+    chip, clk = FakeChip(ext=0xEFF9), Clock()
+    c = make(chip, clk, steps_per_detent=[4, 2, 4, 1], detent_ab=["11", "11", "idle", 0b00])
+    c.open()
+    assert [d.spd for d in c._decoders] == [4, 2, 4, 1]
+    assert [d.anchor for d in c._decoders] == [0b11, 0b11, 0b11, 0b00]   # enc2 idle = its level
+    ext = chip.ext
+    for s in [1, 0]:                            # enc1 (spd 2): one click 11 -> 01 -> 00
+        ext = with_enc(ext, 1, s)
+        chip.set_ext(ext)
+        clk.advance(0.004)
+        c._service()
+    assert [(e.index, e.delta) for e in drain(c)] == [(1, 1)]
+
+
+def test_encoder_options_validation_and_yaml():
+    with pytest.raises(ValueError):
+        ZynthianControls(steps_per_detent=[4, 4, 4])
+    with pytest.raises(ValueError):
+        ZynthianControls(steps_per_detent=[4, 4, 3, 4])
+    with pytest.raises(ValueError):
+        ZynthianControls(detent_ab="12")
+    assert parse_detent("10") == 0b10 and parse_detent("idle") is None and parse_detent(2) == 2
+    assert controls_options({"controls": {"steps_per_detent": [4, 2, 4, 4],
+                                          "detent_ab": "11", "encoders": [0, 1, 2, 3]}}) == \
+        {"steps_per_detent": [4, 2, 4, 4], "detent_ab": "11"}
+    # a typo keeps the default for that key only (never no knobs)
+    assert controls_options({"controls": {"steps_per_detent": 3, "detent_ab": ["00"] * 4}}) == \
+        {"detent_ab": ["00"] * 4}
+    assert controls_options({}) == {} and controls_options(None) == {}
+    assert controls_options({"controls": "nonsense"}) == {}
+
+
+def test_service_opens_the_knobs_with_the_yaml_options(monkeypatch):
+    from k1000 import __main__ as main_mod
+    seen = []
+
+    class Spy:
+        def __init__(self, **kw):
+            seen.append(kw)
+
+        def start(self):
+            pass
+
+    monkeypatch.setattr(m, "ZynthianControls", Spy)
+    assert main_mod.open_controls({"controls": {"steps_per_detent": [4, 2, 4, 4],
+                                                "detent_ab": "bogus"}}) is not None
+    assert seen == [{"steps_per_detent": [4, 2, 4, 4]}]
+    assert main_mod.open_controls({}) is not None and seen[-1] == {}
+
+
+def test_learned_detent_and_half_rest_hint_are_logged_once(caplog):
+    chip, clk = FakeChip(), Clock()
+    c = make(chip, clk)
+    c.open()
+    ext = chip.ext
+    ring = [3, 1, 0, 2]
+    state = 3
+    with caplog.at_level("WARNING", logger="k1000.hw.mcp23017"):
+        for _ in range(12):                     # a 2-transition encoder at the default spd 4
+            for _ in range(2):
+                state = ring[(ring.index(state) + 1) % 4]
+                ext = with_enc(ext, 0, state)
+                chip.set_ext(ext)
+                clk.advance(0.02)
+                c._service()
+            clk.advance(0.35)
+            c._service()
+    hints = [r for r in caplog.records if "steps_per_detent 2" in r.getMessage()]
+    assert len(hints) == 1 and "enc0" in hints[0].getMessage()
+
+
 def test_int_held_low_does_not_spin_the_service_loop():
     chip, clk = FakeChip(), Clock()
     c = make(chip, clk)
--- a/box/k1000/__main__.py
+++ b/box/k1000/__main__.py
@@ -198,8 +198,8 @@
     A half-opened driver is closed again (its INT lines and bus released for the retry)."""
     controls = None
     try:
-        from .hw.mcp23017 import ZynthianControls
-        controls = ZynthianControls()
+        from .hw.mcp23017 import ZynthianControls, controls_options
+        controls = ZynthianControls(**controls_options(settings))
         controls.start()
         log.info("knobs on i2c-1 0x20 (irq=%s)", getattr(controls, "irq", "?"))
         return controls
--- a/box/etc/k1000.yaml
+++ b/box/etc/k1000.yaml
@@ -37,6 +37,11 @@
   pushes: [0, 1, 2, 3]           # push of E1..E4 = physical switch index (sw0..sw3)
   buttons: [4, 5, 6, 7]          # B1..B4 = physical switch index (sw4..sw7)
   reverse: [false, false, false, false]   # flip an encoder's direction
+  # Contact transitions per click of each PHYSICAL encoder enc0..enc3 (1, 2 or 4; one number =
+  # all four) and the A/B levels at a click ("11" = both contacts open; "idle" = whatever they
+  # read at start). Measured by tools/k1000-knobtrace.py (docs/08-knob-precision.md).
+  steps_per_detent: [4, 4, 4, 4]
+  detent_ab: ["11", "11", "11", "11"]
 
 engine:
   url: ws://127.0.0.1:1234
```

### Patch B

```diff
--- a/box/k1000/ui/input_map.py
+++ b/box/k1000/ui/input_map.py
@@ -25,7 +25,7 @@
   (`map_event(..., muted=pages.displayed_muted(state, status))`): the operator presses for the
   opposite of what they see, never for a state the controller has not adopted yet.
 - Pipeline-rebuilding settings (mono subs, audition) need two pushes on SETUP.
-- Acceleration makes master go DOWN faster than it goes up.
+- Acceleration makes master go DOWN faster; it never accelerates master UP.
 """
 from __future__ import annotations
 
@@ -183,7 +183,7 @@
 # E1..E4 per page — the contract's knob table, as data.
 KNOBS: dict[str, tuple[Knob, Knob, Knob, Knob]] = {
     "MAIN": (
-        Knob("MASTER", F_MASTER, accel_up=2, accel_down=4),
+        Knob("MASTER", F_MASTER, accel_up=1, accel_down=4),
         Knob("BALANCE", F_BALANCE),
         Knob("SUB DELAY", F_SUB_DELAY, arm=True),
         Knob("LO-CUT", F_LOWCUT_FREQ, accel_up=4, accel_down=4),
@@ -207,12 +207,15 @@
 # E4 bottom-right) until Thomas confirms on the box — then correct this table only.
 KNOB_CORNERS: tuple[str, str, str, str] = ("tl", "bl", "tr", "br")
 
-# Acceleration: turn rate (detents/s) -> multiplier, the same curve as the knob driver's
-# (x1 up to 12 det/s, the knob's cap from 40 det/s). A direction change or a pause longer than
-# the window resets to x1, so correcting an overshoot is always fine-grained. Applied HERE only
-# (per-field caps); the controller must not accelerate again.
+# Acceleration: turn rate (detents/s) -> multiplier, the knob driver's curve (x1 up to 20 det/s,
+# the knob's cap from 40 det/s). The rate is the slower of the last TWO detent intervals, so a
+# single quick interval (a flick, sampling jitter) never jumps, and the first two detents of
+# any turn are x1. A direction change or a pause longer than the window resets to x1, so
+# correcting an overshoot is always fine-grained. Applied HERE only (per-field caps); the
+# controller must not accelerate again. docs/08-knob-precision.md: 12 det/s (v0) is a normal
+# brisk turn of a 24-click knob, not a hurry.
 ACCEL_WINDOW_S = 0.25
-ACCEL_LOW_DPS = 12.0
+ACCEL_LOW_DPS = 20.0
 ACCEL_HIGH_DPS = 40.0
 
 try:
@@ -356,6 +359,7 @@
     held: set[int] = field(default_factory=set)          # logical encoders whose push is down
     turned: set[int] = field(default_factory=set)        # held pushes whose knob was turned
     last_turn: dict[int, tuple[float, int]] = field(default_factory=dict)  # enc -> (ts, sign)
+    turn_dt: dict[int, float | None] = field(default_factory=dict)  # enc -> previous interval
     confirm: str | None = None            # SETUP field waiting for its second push
     confirm_until: float = 0.0
     hw: HwTestLog = field(default_factory=HwTestLog)
@@ -406,16 +410,17 @@
 
 
 def _accel(knob: Knob, delta: int, last: tuple[float, int] | None, now: float,
-           fine: bool) -> int:
-    """Scale `delta` by turn rate; x1 when fine, reversing, after a pause, or for flat knobs."""
+           fine: bool, prev_dt: float | None = None) -> int:
+    """Scale `delta` by turn rate; x1 when fine, reversing, after a pause, for flat knobs, and
+    until two intervals in a row (`prev_dt`, then this one) say the turn is fast."""
     cap = knob.accel_up if delta > 0 else knob.accel_down
     sign = 1 if delta > 0 else -1
-    if fine or cap <= 1 or last is None or last[1] != sign:
+    if fine or cap <= 1 or last is None or last[1] != sign or prev_dt is None:
         return delta
     dt = now - last[0]
-    if dt > ACCEL_WINDOW_S:
+    if dt > ACCEL_WINDOW_S or prev_dt > ACCEL_WINDOW_S:
         return delta
-    rate = abs(delta) / max(dt, 0.002)
+    rate = abs(delta) / max(dt, prev_dt, 0.002)
     return delta * accel_multiplier(rate, ACCEL_LOW_DPS, ACCEL_HIGH_DPS, cap)
 
 
@@ -446,7 +451,10 @@
             return []
         knob = knobs[e]
         last = focus.last_turn.get(e)
-        focus.last_turn[e] = (now, 1 if delta > 0 else -1)
+        sign = 1 if delta > 0 else -1
+        prev_dt = focus.turn_dt.get(e)
+        focus.turn_dt[e] = now - last[0] if last is not None and last[1] == sign else None
+        focus.last_turn[e] = (now, sign)
         fine = e in focus.held
         if fine:
             focus.turned.add(e)       # its release is the end of a fine turn, not a 'select'
@@ -463,7 +471,7 @@
         if knob.arm and not fine and (last is None or now - last[0] > ARM_WINDOW_S):
             focus.editing, focus.edit_until = fld, now + EDIT_HIGHLIGHT_S
             return [ShowHint(HINT_ARM.format(knob.label))]
-        return [Nudge(fld, _accel(knob, delta, last, now, fine), fine)]
+        return [Nudge(fld, _accel(knob, delta, last, now, fine, prev_dt), fine)]
 
     # switches
     role = cmap.switch_role(int(event.index))
--- a/box/tests/test_ui.py
+++ b/box/tests/test_ui.py
@@ -637,23 +637,37 @@
     assert map_event("MAIN", f, Enc(2, 1, ts=5.0)) == [ShowHint(im.HINT_ARM.format("SUB DELAY"))]
 
 
-def test_acceleration_master_down_faster_than_up_and_frequency():
+def test_acceleration_master_down_only_and_frequency():
     f = Focus()
-    map_event("MAIN", f, Enc(0, 1, ts=0.0))
-    assert map_event("MAIN", f, Enc(0, 1, ts=0.01)) == [Nudge("master_db", 2, False)]   # up: x2 max
-    assert map_event("MAIN", f, Enc(0, -1, ts=0.02)) == [Nudge("master_db", -1, False)]  # reversal
-    assert map_event("MAIN", f, Enc(0, -1, ts=0.03)) == [Nudge("master_db", -4, False)]  # down: x4
-    assert map_event("MAIN", f, Enc(0, -1, ts=0.07)) == [Nudge("master_db", -2, False)]  # 25 det/s
+    for k in range(5):                                           # master up: never accelerated
+        assert map_event("MAIN", f, Enc(0, 1, ts=k * 0.01)) == [Nudge("master_db", 1, False)]
+    assert map_event("MAIN", f, Enc(0, -1, ts=0.05)) == [Nudge("master_db", -1, False)]  # reversal
+    assert map_event("MAIN", f, Enc(0, -1, ts=0.06)) == [Nudge("master_db", -1, False)]  # 1 interval
+    assert map_event("MAIN", f, Enc(0, -1, ts=0.07)) == [Nudge("master_db", -4, False)]  # down: x4
+    assert map_event("MAIN", f, Enc(0, -1, ts=0.10)) == [Nudge("master_db", -3, False)]  # 33 det/s
+    assert map_event("MAIN", f, Enc(0, -1, ts=0.14)) == [Nudge("master_db", -1, False)]  # 25 det/s
     assert map_event("MAIN", f, Enc(0, -1, ts=1.0)) == [Nudge("master_db", -1, False)]   # pause
     map_event("MAIN", f, Enc(3, 1, ts=2.0))
-    assert map_event("MAIN", f, Enc(3, 1, ts=2.01)) == [Nudge("lowcut_hz", 4, False)]
-    assert map_event("MAIN", f, Enc(3, 3, ts=2.06)) == [Nudge("lowcut_hz", 12, False)]  # coalesced
+    assert map_event("MAIN", f, Enc(3, 1, ts=2.01)) == [Nudge("lowcut_hz", 1, False)]
+    assert map_event("MAIN", f, Enc(3, 1, ts=2.02)) == [Nudge("lowcut_hz", 4, False)]
+    assert map_event("MAIN", f, Enc(3, 3, ts=2.07)) == [Nudge("lowcut_hz", 12, False)]  # coalesced
     map_event("MAIN", f, Enc(2, 1, ts=3.0))                      # sub delay: never accelerated
     assert map_event("MAIN", f, Enc(2, 1, ts=3.01)) == [Nudge("sub_delay_ms", 1, False)]
     map_event("MAIN", f, Enc(1, 1, ts=4.0))                      # balance: never accelerated
     assert map_event("MAIN", f, Enc(1, 1, ts=4.005)) == [Nudge("balance_db", 1, False)]
 
 
+def test_acceleration_ignores_a_single_quick_interval():
+    """docs/08: one short detent interval (a flick, sampling jitter) in a normal turn is x1;
+    a brisk 15 det/s turn of a 24-click knob (v0 accelerated from 12) stays x1."""
+    f = Focus()
+    for k, t in enumerate([1.0, 1.2, 1.4, 1.41, 1.6]):
+        assert map_event("LIVE", f, Enc(1, 1, ts=t)) == [Nudge("live_peq.0.freq", 1, False)], k
+    for k in range(8):
+        assert map_event("LIVE", f, Enc(1, 1, ts=3.0 + k / 15)) == \
+            [Nudge("live_peq.0.freq", 1, False)], k
+
+
 def test_live_knobs_follow_selected_slot():
     f = Focus()
     acts = map_event("LIVE", f, Enc(0, 1, ts=0.0))
```

## 7. Recommended, not patched (other owners)

- **Fader ramp, for the MASTER/BALANCE feel (row 3). Applied: 100 ms** (D-009) for Main
  (`devices.volume_ramp_time_ms`) and the Aux faders (`ramp_time_ms`), `dspconfig.VOLUME_RAMP_MS`.
  Side effects, as built:
  - the mute and unmute fades are 0.1 s (operator card updated);
  - the delay-guard dip (`GUARD_WAIT_S` = ramp + 50 ms) is 0.15 s instead of 0.35 s;
  - the boot ramp steps every 0.1 s (30 steps over 3 s instead of 10, each smaller).

  A short ramp still declicks a 0.5 dB step; Friday's rig test is the ear check. A box already
  running the 300 ms config gets the Aux ramps as plain value fixes (no restart); Main's is a
  `devices` change, which K1000 never forces while audio runs: the panel shows DEVICES
  CHANGED (GUI) (ENGINE DEVICES CHANGED OUTSIDE K1000 before 2026-10-13) until the engine next starts, when K1000 uploads its config to the
  fresh engine (muted) and ramps in. So a deploy of this change restarts camilladsp too.
- **Visual latency (row 5).** Render the first frame after a knob event at once, instead of
  waiting out the 15 fps cap. Consider fbtft `fps=50`.
- **Sampling (row 6), only if the trace shows losses:**
  - i2c-1 at 400 kHz (the MCP23017 supports it), which brings a read down to about 0.3 ms;
  - `sys.setswitchinterval(0.001)` in the k1000 process. **Applied 2026-10-13 (§10)**: the v1
    review measured the losses.

## 8. Not verified

- **Which encoder part the V4 has, and its transitions per click.** Hence the trace.
- **The cause of this morning's idle levels** (enc0 00, enc3 10): knobs left off-detent, or a
  worn contact. The trace's rest histogram per encoder will show which.
- **Whether the V4 controller board has RC filters on the encoder lines.**
- **What Thomas actually turned this evening, and how.** The service logs no nudges, so the link
  between "not precise" and hypothesis #1 or #2 is inferred, not observed.
- **End-to-end knob-to-pixel latency.** It is estimated from its parts; the panel write was not
  timed because it would draw on the live screen.
- **The 300 ms ramp's audible lag.** It comes from the engine's source and the active config, and
  was not heard on the box.
- **Patches A and B have not run on the box itself, only locally on Python 3.13.** The trace tool
  has never sampled the live chip (by design: it needs k1000 stopped and Thomas's hands). Its
  INT-driven path follows the driver's own code and is untested against hardware.

## 9. Applied (2026-10-12 night), and the glitch amendment

Patches A and B went in by intent on the current files (both still applied cleanly). On top:

- **MASTER up is never accelerated on any page**, enforced in `input_map._accel` whatever the
  page's `Knob` row says, so the SHOW/ALIGN rows of UI v1 cannot reintroduce it.
- **Phantom detents from a resting knob (the 20:25 Main −19 → −20).** Patch A removed the timer
  emission, but its state table still turned a 2 ms glitch that takes **both** contacts at once
  (ground bounce, a spark, a touched cable; the MCP23017 pull-ups are a weak 100 kΩ) into a
  detent once the contacts came back:

  | Glitch on a knob at rest (AB 11) | v0 and patch A | now |
  |---|---|---|
  | 11 → 00 → 01 → 11 (B back first) | −1 | nothing |
  | 11 → 00 → 10 → 11 (A back first) | +1 | nothing |
  | 11 → 00 → 11 (both back together), the knob turned some time before | ±1, the direction of its last turn | nothing |

  Two such glitches are −1 dB on MASTER, which matches the unexplained change; the journal of
  that boot (20:17–20:28) has no knob warning, as expected for this path. **Not proven:** the
  service logs no nudges. The decoder now attributes a skipped state (both bits flipped
  between two samples) only when the knob really moved within `infer_s`, measured from the last
  counted transition, not from the glitch itself. **(Incomplete: when the glitch's first sample
  has one contact flipped, that first edge IS the last counted transition. See §10.)** An unattributed skip leaves the decoder
  *lost* by half a click until the knob is back on a detent; that arrival counts the smaller of
  the two possible readings and re-syncs, and no 3/4 completion happens while lost. Cost: a turn
  whose very first sample already skips two transitions loses its first click. With interrupts
  that does not happen (INTCAP holds the first transition of any real turn); in the polling
  fallback (no INT lines) it can. **(Wrong as a safety argument: INTCAP holds the first edge of
  a glitch too, usually one contact. §10.)**
- **Still possible, by design:** a glitch that releases the contacts in exactly the quadrature
  order (A low, B low, A high, B high) is a full click to any state-table decoder. The trace's
  "invalid without a direction" count and the hardware log are where to look if a value still
  moves by itself.

Tests (`box/tests/test_mcp23017.py`): every noise walk that stays within half a click, brisk,
hovering and random timing, then 30 s of nothing; the glitch shapes above at 0.5 ms, 2 ms and
0.2 s, fresh and after a real turn, spd 4 and 2, each followed by an exact real click; fifty
glitches over ten minutes; the same through the driver with every input line pulled low and
released B-first, A-first and together (no knob and no button event). The glitch cases fail on
the patch A decoder as written in §6 (17 of them) and pass now; the noise walks pass on both
(that part is patch A's own fix).

## 10. Second amendment (2026-10-13, after the v1 review)

**The 20:25 path is narrowed, not closed.** §9's fix covered glitches whose first sample has
both contacts flipped. With interrupts, INTCAP holds the first edge of *anything*, a glitch
included, and that edge is usually one contact: INTCAP 01 then GPIO 10 in the same read is
11 → 01 → 10 → 11, and the glitch's own first edge counted as "the knob really moved", so the
skip that followed was credited and a click completed. Now:

- a skip is credited only to a knob **already turning that way before it left the detent** (a
  counted transition within `infer_s` before the excursion: a spin, whose previous click just
  landed), or after **two counted steps that way** since it left (|acc| ≥ 2);
- a click that starts **from stillness** (nothing counted within `infer_s` before it left the
  detent) and lands faster than **2.5 ms per transition** (`GLITCH_STEP_S`, 100 clicks/s at
  4 transitions per click) is not counted: a glitch whose contacts dip and come back staggered
  is a valid quadrature cycle, and only its speed gives it away.

| Glitch on a knob at rest (AB 11), samples 0.5 or 2 ms apart | §9 | now |
|---|---|---|
| 11 → 01 → 10 → 11 (INTCAP 01, GPIO 10), and 11 → 10 → 01 → 11 | +1 / −1 | nothing |
| 11 → 01 → 00 → 11 (two dips, released together), spd 4 | +1 | nothing |
| 11 → 01 → 00 → 10 → 11 (staggered dips and releases) | +1 | nothing |
| the same over 0.6 s | +1 | +1: a click, whatever made it |

Cost: the **first click of a turn from stillness** is lost when one of its first two
transitions is sampled late by more than one transition (a skip with |acc| < 2), or when it
lands within 7.5 ms of leaving the detent (faster than ~67 clicks/s for that first click). The
second click of a turn leaves the detent while moving and is never affected.

**Still possible:** a glitch slower than 2.5 ms per transition that walks a whole quadrature
cycle. **Attribution, now:** every master change is logged at INFO with its source,
`master -19.0 -> -20.0 dB (knob)` (knob, phone, console, adoption), and the driver counts the
glitches it dropped (`QuadratureDecoder.glitches`). A repeat of 20:25 will name what moved it.

Tests (`box/tests/test_mcp23017.py`): the shapes above added to the glitch lists (spd 4 and 2,
fresh and after a real turn, each followed by an exact real click), the staggered cycle at
0.5 / 2 ms (nothing) and at 0.2 s (one click), and a driver test where INTCAP latches the
glitch's one-contact first edge and GPIO the second state in the same read (four shapes, with
and without an earlier turn). 25 of them fail on §9's decoder.

**Fast turns losing clicks (Thomas: "FAST turns feel inaccurate").** Patches A/B fixed how a
click is counted, not whether the worker sees the transitions. Measured on the box with the
real v1 `run()` (review harness `~/dev/v1-perf`): MASTER down lost ~24 % of its clicks at
45 clicks/s with SHOW drawn, and started losing them at 25; v1 SHOW was about twice v0 MAIN.
Three causes, three fixes:

- **The GIL** (PERF-1). The worker needs the GIL 3–4 times per chip read; at Python's default
  5 ms switch interval each wait could last 5 ms behind the event loop or the renderer, and
  20–35 ms sampling gaps swallowed whole clicks. `__main__.main()` now sets
  `sys.setswitchinterval(0.001)` (`GIL_SWITCH_S`) before any thread starts: 0 clicks lost in
  the harness, no CPU change.
- **Storm misclassification** (PERF-2). A fast knob re-asserts INT after each clearing read;
  after three reads the driver called that "INT held low" and fell back to 5 ms polling with
  one read per pass, in the middle of the spin. Now a line still low is busy, not stuck, when
  its own port brought a new level on the last read AND a fresh falling edge is queued
  (`int_busy`); a line wedged low makes no edge and stays stuck even while a knob on its port
  turns. The edge-flood detector and the 0.25 s config verify still catch a corrupted INTCON
  or a chattering contact.
- **Saves in the event loop** (PERF-3). `active.yml` was re-dumped with PyYAML's pure-Python
  dumper (34 ms median on the Pi) every 0.25 s while MASTER went down. Now libyaml
  (`CSafeDumper`, byte-identical, tested on every profile) and no YAML at all when the config
  did not change (MASTER, balance and mute are faders, not in `active.yml`).

With the first two, the harness measured edge → sample p95 3.4 / p99 5.6 ms and no loss at
45 clicks/s. Regression test: `box/tests/test_knob_timing.py` drives the REAL worker thread
on `k1000.hw.simchip` (a time-driven MCP23017: INTCAP, INT lines, a 0.9 ms transfer that
releases the GIL) beside a thread holding the GIL 45 % of the time: at 1 ms a 45 clicks/s
turn is sampled within one transition 95 % of the time (p95 ≈ 3.5 ms) and counted exactly; at
the 5 ms default the p95 is ≈ 11.4 ms and the test fails.

**The bench is not evidence of knob timing by itself** (row 6 above). `k1000-knobtrace.py
bench` now drives the real worker on the sim chip at 25/35/45 clicks/s, idle and beside the v1
load (SHOW + a moving spectrum rendered in a thread, the ballistics and a 40 ms save chunk
every 0.25 s in an asyncio loop thread), and reports lost clicks and edge → sample delays. The
capture's `render` load is the same v1 load. Its numbers are only as good as that model: the
check is `capture --guided` on the real knobs, fast spins included, after the deploy.
