# 10 — Tuesday bench checklist (Tue 2026-10-13)

For Thomas at the bench: the box on the desk, profile `dev` (HiFiBerry) until the M4 arrives,
then the M4 ([05-m4-day.md](05-m4-day.md)). **Meyers off all day.** Each step: what to do → what
you should see. *Claude:* what to send when it differs (a photo of the screen always helps).
Commands run on the box (`ssh k1000@<box>`).

**What changed overnight.** Live UI v1 (D-009) runs on the box, deployed at 08:03 as
`c49f006`. **SHOW** is the front page: E1 MASTER, E2/E3/E4 the FREQ/GAIN/Q of the selected
band, the program spectrum behind the EQ curve with a cyan PEAK pointer. **ALIGN** holds balance,
sub delay and Lo-Cut, E3/E4 locked behind a B3 hold. **SETUP** adds Reset live EQ with a 5-minute
undo, Spectrum, Laptop access (CamillaGUI, SSH, the `/tune` REW import) and PHONE. B3 steps the
band, B4 is the notch helper. The knob decoder counts a click when the knob lands, never on a
timer, drops a resting knob's glitches and no longer loses clicks on fast turns (harness-measured;
your hands decide). The K1000 Wi-Fi and the phone page are up, unproven with a real phone. The
power-cut drill passed (boot 10.4 s; the clock came back 3.5 min behind: no battery clock). The
soak ran 1 h 34 min, clean (06:28-08:02), and is stopped. The PEAK now survives a B1 hold on SHOW.

**Your asks of this morning (D-011), on the box since `24cba65` (10:46):** the Lo-Cut moves 2 Hz
a click (1 Hz with the push); ALIGN E3 is one signed **DELAY** (clockwise `SUBS LATER`,
anticlockwise `TOPS LATER`, a stop on `ALIGNED` between them), so SETUP › Top delay is gone; the
E1–E4 tags are small and grey (`ui: {knob_tags: off}` in k1000.yaml removes them, for a sticker):
Thomas chose `off`, on the box since 13:39 (sticker the shell).

**Your asks of this afternoon (D-013), from Claude's next deploy, on your go:** new button roles,
**B1 PAGE · B2 BAND/UNLOCK · B3 NOTCH · B4 MUTE**. B4 mutes (hold), B2 steps the band and unlocks
ALIGN (hold), B3 is the notch helper; a B1 tap flips SHOW ↔ ALIGN, a B1 hold opens SETUP. SETUP
is grouped by speaker floor (TOPS, SUBS, INPUT, LIVE EQ, PHONE, BOX; E3 jumps a group), and every
ON/off or choice is **turn E2 to choose, push to set** (the row shows `now → chosen`). The steps
below name the D-013 build, on the box since 14:31 (`dd740ce`). Re-label the shell buttons.

| SHOW | after B3 | ALIGN, locked | SETUP, a choice | Knob test |
|---|---|---|---|---|
| ![K1000 screenshot](ui/show.png) | ![K1000 screenshot](ui/show_notch.png) | ![K1000 screenshot](ui/align_locked_turn.png) | ![K1000 screenshot](ui/setup_confirm.png) | ![K1000 screenshot](ui/hwtest.png) |

## A. Start (profile `dev`)

- [ ] **1.** `systemctl is-active k1000-soak` → `inactive` (Claude stopped it at 08:02; the summary,
  `/opt/k1000/tools/k1000-soak.sh --summary /var/log/k1000/soak-night.log`, says `clean`). If
  `active`: run the summary, then `sudo systemctl stop k1000-soak`. *Claude:* the summary.
- [ ] **2.** `cat /opt/k1000/REVISION; grep '^profile' /etc/k1000/k1000.yaml` → `c49f006`
  (newer if Claude deployed since), `profile: dev`. *Claude:* both lines.
- [ ] **3.** **Buttons first:** the Knob test, `controls:`, the k1000 restart and the checks of
  [05 §10](05-m4-day.md); every knob **+** clockwise. On the Knob test, press the four buttons in
  turn: each line ends with the name the code gives it (`sw4 press = B1` … `sw7 … = B4`); label
  the shell from that, **B1 PAGE · B2 BAND/UNLOCK · B3 NOTCH · B4 MUTE** (D-013), not from the
  old card.
  *Claude:* the map (position → `sw`/`enc`), and which corner holds B1–B4.

## B. Knob feel

- [ ] **4.** Knob test, each knob: 10 clicks clockwise slowly, stopping on each, then 10 back
  briskly → each counter +10, then back. Leave the Knob test with a B1 hold (→ SETUP), then a B1
  tap (→ SHOW). *Claude:* knob, speed, count.
- [ ] **5.** SHOW, E1 down to −30.0. 10 clicks up slowly, 10 down slowly → −25.0, −30.0. Up in
  one fast spin of ~20 clicks → stops at −24.0 with `MASTER: SLOW DOWN`, rises again 2 s later;
  up is never more than 0.5 dB a click. Down fast → bigger steps (up to 2 dB): by design.
  *Claude:* the values.
- [ ] **6.** B2 to band 2 (`1.00k`). E2 12 clicks up slowly, then back → `2.00k`, `1.00k` (a
  fast turn jumps further). E3 10 down, 10 up, briskly → −5.0, 0.0 at any speed. E4 6 up, 6 down
  → Q back where it was. E2 with its push held → `FINE`, 1/48 oct; releasing the push selects
  nothing. *Claude:* the values.
- [ ] **7.** ALIGN (B1 tap). E2 10 clicks up, then back → `+5.0 dB`, `subs +2.5 · tops -2.5`,
  0.0. Turn E3 or E4 → `LOCKED · HOLD B2 TO EDIT`, nothing moves.
- [ ] **8.** Hold B2 → `EDIT 0:30`. After a 3 s rest, one E3 click only wakes it
  (`DELAY · TURN AGAIN TO CHANGE`); 10 more clockwise → `1.00 ms`, `SUBS LATER · 0.34 m` (the
  subs dip at each change); back anticlockwise → it stops one click on `0.00 · ALIGNED`; one more
  → `0.10`, `TOPS LATER` (now the tops dip); back to `ALIGNED` (a quick turn through it dips subs
  and tops together). E3 with its push held → `FINE`, one sample (0.02 ms). E4 down to 100 Hz,
  the Sunday default (D-010): six clicks from 112, seven from a saved 112.25 (its first click only
  puts it on the 2 Hz grid: the screen stays at `112`). Then 10 clicks up slowly → `120` (2 Hz a
  click; push held: 1 Hz; a fast spin: up to 8 Hz), 10 back → `100`. *Claude:* the values.
- [ ] **9.** ALIGN untouched 30 s, turn E4 → locked again. Hold B2, then tap B2 →
  `DELAY + LO-CUT LOCKED` at once.

## C. Pages and buttons

- [ ] **10.** B1 taps → SHOW → ALIGN → SHOW (the page moves on release). Hold B1 on SHOW, on
  ALIGN → SETUP; on SETUP a tap or a hold → SHOW. SETUP › PHONE › Phone, B1 tap → back to
  SETUP. ALIGN untouched for 60 s → SHOW by itself.
- [ ] **11.** Hold B4 (0.8 s) on SHOW, ALIGN (locked, unlocked), SETUP, PHONE, Knob test → each
  mutes / unmutes (red frame, `MUTED` pill). A tap → only `HOLD B4 TO MUTE / UNMUTE`.
  *Claude:* the page where it failed.
- [ ] **12.** SHOW, B2 taps → LS → 1 → 2 → 3 → 4 → HS → LS; the E3 label names the band
  (`PEQ 2 GAIN`). B2 on SETUP → `B2: NEXT BAND ON SHOW`; B3 → `B3: NOTCH HELPER ON SHOW`.
- [ ] **12b.** SETUP (D-013): E3 turns → the groups TOPS, SUBS, INPUT, LIVE EQ, PHONE, BOX (the
  top line says `SUBS 1/6`), E1 → the items, headers skipped. SUBS › Polarity: turn E2 one
  click → the row turns red, `normal → INVERTED`, footer `PUSH TO SET INVERTED`; turn back →
  nothing chosen; turn again and wait 8 s → it lapses. Choose `INVERTED`, push E2 → set (the subs
  dip); choose `normal`, push → back. BOX › Profile → dimmed `DEV · PINNED` if k1000.yaml pins
  it; a turn says why. *Claude:* a photo of anything unclear on the row or the footer.

## D. Spectrum, notch, reset (program into the HiFiBerry line in; on the M4, LINE IN 3/4)

- [ ] **13.** Music in, SHOW → the spectrum moves behind the curve. The phone's 2 kHz sine → the
  cyan `▼` at about 2k within 2 s. Silence → no PEAK. SETUP › LIVE EQ › Spectrum, choose off,
  push → the curve alone; choose ON, push → back. *Claude:* `NO SPECTRUM`, a frozen spectrum, a
  PEAK on silence.
- [ ] **14.** Tone on, SHOW: B3 → `Pn · ≈2k · E3 TO CUT`, gain 0.0, nothing audible. E3 12
  clicks down → −6.0 dB. E4 6 up → Q 16. B3 again → selects that bell, changes nothing.
  *Claude:* the strip text, a photo.
- [ ] **15.** From ALIGN: tap B1, wait for the PEAK (~2 s), then B3 → it lands on the PEAK.
  *Claude:* `NO PEAK` although the PEAK showed.
- [ ] **16.** B3 in silence → `… NO PEAK · E3 TO CUT`. All four bells cut (B2, E3 on each), B3
  → `NO FREE BAND`. A B3 hold → `RESET LIVE EQ: IN SETUP`.
- [ ] **17.** SETUP › LIVE EQ › Reset live EQ, push twice → `LIVE EQ RESET · UNDO IN SETUP`,
  curve flat. Undo reset (its row counts down, `UNDO 4:47`), push → `LIVE EQ RESTORED`, the cuts
  back. Reset again, one E3 click on SHOW, Undo → `RESET UNDO LOST`, then `NOTHING TO UNDO`.

## E. Phone ([06 §3](06-phone.md), once per phone)

- [ ] **18.** CA: copy `k1000-ca.crt` to the phone (or SETUP › PHONE › Phone, E1 to `FIRST TIME`,
  Chrome, *Keep*). Do not tap it: Settings › … › Encryption & credentials › Install a
  certificate › **CA certificate** → *Network may be monitored*. *Claude:* the Android message.
- [ ] **19.** SETUP › PHONE › Phone, QR 1, scanned from Settings › Wi-Fi (QR icon); *Stay
  connected?* → **Yes**, *Don't ask again* → an IP 10.42.0.x kept over 30 s; the box shows QR 2
  and `1 on Wi-Fi`. *Claude:* dropped after ~6 s? `sudo /opt/k1000/net/setup-ap.sh --status`.
- [ ] **20.** QR 2 (or `https://10.42.0.1/`), *Install* in the top bar; airplane mode + Wi-Fi,
  tap the icon → no certificate warning; opens in < 2 s with `LINK` + `RUNNING`. Note: an app
  or a Chrome-badged shortcut. *Claude:* the warning text.
- [ ] **21.** Master −1 dB on the phone → the box follows; E1 on the box → the phone follows.
  Phone *MUTE* (hold) → `MUTED`; B4 hold → unmuted; phone hold and B4 hold together → ends
  **muted**.
- [ ] **22.** Out of range (or the phone's Wi-Fi off) for a minute, then back → `NO LINK`, the
  box unchanged; `LINK` again by itself within 30 s. *Claude:* the distance, the rejoin time.
- [ ] **23.** SETUP › PHONE › Phone, E1 push → `PHONE OFF · KNOBS ONLY`, the page says *Phone
  control is OFF on the box*; push again → it reconnects. Range on both bands is Friday's
  ([06 §9](06-phone.md)).

## F. Optional: laptop access and `/tune` ([06 §11](06-phone.md))

- [ ] **24.** SETUP › PHONE › Laptop access, push twice → `OPEN 2:00`, SHOW pill
  `LAPTOP OPEN 1:59`. The laptop joins K1000 with the password SETUP › PHONE › Phone now prints →
  `https://10.42.0.1/tune`, `http://10.42.0.1:5005/gui/index.html`, `ssh k1000@10.42.0.1` all
  open. On `/tune` paste
  `125 Hz -4 dB Q 5` (or a REW export): preview, Apply, Undo. One push on Laptop access →
  `LAPTOP ACCESS CLOSED`, CamillaGUI no longer loads. *Claude:* `journalctl -t k1000-laptop -n 20`.

## G. Optional: the knob trace (about 2 minutes)

Worth it if a count in B was off or the knobs still feel imprecise ([08 §4](08-knob-precision.md)).
While k1000 is stopped the sound keeps playing but **B4 does nothing**. Use the deployed copy:
`~/dev/knobs/k1000-knobtrace.py` is the older one (12 October, v0's load model).

```sh
sudo systemctl stop k1000
python3 /opt/k1000/tools/k1000-knobtrace.py capture --guided -o ~/knobs-1.trace
sudo systemctl start k1000
```

- [ ] **25.** Follow the prompts, count the clicks exactly (`r` redoes a step, `q` stops) →
  `VERDICT: steps_per_detent 4, detent AB 11` on all four. *Claude:* the VERDICT lines; the
  trace stays in `~/knobs-1.trace`.

## H. When the M4 arrives: [05-m4-day.md](05-m4-day.md)

- [ ] **26.** §0–4: the front panel set before plugging, plug, detect, the check tool, mixer,
  `throttled`, then one output at a time and the cables labelled.
- [ ] **27.** §5: profile `m4`; SHOW with program in LINE IN 3/4 → the spectrum back by itself
  within seconds of the engine switch. *Claude:* the k1000 journal lines; `NO SPECTRUM`.
- [ ] **28.** §6 (its Knob test is step 3), §7, §8 → SUBS › Mono L+R back ON and the Lo-Cut back
  to 100 Hz after §6.
- [ ] **29.** §9: the 1 h soak (`k1000-soak-m4`); meanwhile §11, tape and label → `--summary`
  says `clean`.
- [ ] **30.** §12: the facts (the decisions are taken: D-010, `target_level` 512 is set).
  *Claude:* all of §12, and `grep target_level /var/lib/k1000/active.yml` on `m4`.

## I. End of the day

- [ ] **31.** Live EQ flat (SETUP › LIVE EQ › Reset live EQ). ALIGN as found: balance 0.0, DELAY
  0.00 `ALIGNED`, Lo-Cut ON at 100 Hz. SUBS › Mono L+R ON, Spectrum ON, Laptop access closed, Phone
  control as decided → SHOW: a flat curve, no orange pill.
- [ ] **32.** **Hold B4** → `READY · hold B4 to unmute`. Leave it on, on `eth0`, the M4 plugged
  and taped: Claude needs it Wednesday (freeze) and Thursday (soak). To switch off instead: hold
  B4 → Meyers off → unplug K1000 → unplug the M4; it comes back muted (power on: M4 first, then
  K1000). *Claude:* the profile you left (`m4` once §5 passed).
