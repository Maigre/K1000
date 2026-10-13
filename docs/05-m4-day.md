# 05 — M4 day: Tuesday 2026-10-13

The MOTU M4 becomes the gig interface (profile `m4`). By the end of the day every output is
identified and labelled, the front panel is proven and taped, levels are checked, and the facts
below are reported back.

**Meyers OFF, or their XLRs unplugged, for the whole day.** Every step here may cause about a
second of silence or a tone on an output.

Sources: the M4 readiness review (MOTU M Series manual, alsa-ucm-conf `M4-HiFi.conf`, kernel
`sound/usb/quirks.c`, mdsimon2's Pi 4 + M4 CamillaDSP config), `box/README.md` "MOTU M4 bring-up",
`box/tools/k1000-m4-bringup.sh`. Anything marked UNVERIFIED is research, not yet seen on this unit.

**Order:** §0–5, then the Knob test (§10), then §6–9 and §11–12. §6 needs the buttons mapped.

## 0. Before plugging anything

| Check | |
|---|---|
| Deployed tree | The 2026-10-12 fix round is on the box: `cat /opt/k1000/REVISION` is newer than `e3a3deb`, and `/opt/k1000/tools/k1000-m4-bringup.sh` exists. camilladsp was restarted once after that deploy (new start script) |
| Profile | `grep '^profile' /etc/k1000/k1000.yaml` says `dev`. Nothing opens the M4 until step 5, except the tone in step 4 |
| Cables at hand | USB-C → USB-A, short. 2× XLR-F → TRS-M (program into LINE IN 3/4). 4× TRS-M → XLR-M, balanced (outs to the Meyers): never TS jacks, never the RCA outs. One TRS–TRS cable (loop test). A phone with a tone generator app |

Set the front panel **before plugging**, then tape it at the end of the day:

| Control | Position | Why |
|---|---|---|
| MONITOR (big knob) | fully clockwise | It sets outs 1-2 = the tops. Outs 3-4 have no knob. UNVERIFIED that fully clockwise is exactly unity: loop test, step 6 |
| INPUT MONITOR MIX | fully clockwise | USB playback only. It is a balance knob, not a switch: counter-clockwise = inputs only, i.e. the tops silent or fed the raw program. UNVERIFIED that fully clockwise blocks the inputs: leak test, step 6 |
| MON 1, MON 2, 3-4 | all unlit (no blue box around the input numbers on the LCD) | Lit = that input goes straight to outs 1-2, past K1000 and past its MUTE |
| 48V | off | |
| GAIN 1 / GAIN 2 | fully counter-clockwise | Inputs 1-2 are not in the program |
| Phones | down | Phones mirror outs 1-2 |
| Rear power switch | on | |
| Program | rear LINE IN 3 (L) and 4 (R), balanced TRS | Never into the XLR combos 1-2 (mic preamps; the manual warns against +4 line there) |

## 1. Plug and detect

USB-C → USB-A into a black (USB 2) port of the Pi, strain-relieved. Then, on the box:

```sh
mkdir -p ~/m4-day
sudo dmesg | grep -Ei 'usb|motu|m4' | tail
cat /proc/asound/cards
cat /proc/asound/M4/usbid
lsusb -v -d 07fd: 2>/dev/null | grep -E 'idProduct|bcdDevice|iProduct'
```

| Expect | If not |
|---|---|
| `idVendor=07fd`, `idProduct=0008` (first hardware revision: the card appears about 4 s after plugging) or `000b` (second revision), `Product: M4` | Another black USB port, another cable; then report the `dmesg` lines |
| A cards line `N [M4 ]: USB-Audio - M4`, with `MOTU M4 at usb-...` under it | If the card id is not exactly `M4`, set `capture_device` and `playback_device: hw:<id>` under `profiles: m4:` in `/etc/k1000/k1000.yaml` |

## 2. Run the check tool (opens nothing that plays)

```sh
/opt/k1000/tools/k1000-m4-bringup.sh | tee ~/m4-day/bringup-1.txt
cat /proc/asound/M4/stream0 > ~/m4-day/stream0.txt
```

- Exit 0 = no FAIL line. Fix any FAIL before going on.
- Expected WARN: `controls: still the defaults` (until the Knob test, step 10).
- Section 2 (from `/proc`): Playback and Capture both `Format: S32_LE` (maybe also `S24_3LE`),
  `Channels: 4`, rates including 48000. UNVERIFIED until now.
- Section 3 asks with S16 mono, which a 4-channel device refuses: `CHANNELS 4` and
  "Channels count non available" are the good answer.

## 3. Mixer and power

```sh
amixer -c M4 contents | tee ~/m4-day/amixer.txt
vcgencmd get_throttled
```

- Any playback volume must read 0 dB and any switch `on`. If you changed one, `sudo alsactl store`.
- `throttled=0x0`. Again after 10 minutes with the M4 and the screen on. The M4 is bus-powered
  from the Pi; anything else means a powered USB hub or a stronger PSU before Friday.

## 4. Identify the outputs (tone, speakers off)

```sh
sudo /opt/k1000/tools/k1000-m4-bringup.sh --tone --i-have-turned-the-speakers-off
```

- Output N plays N short beeps at −40 dBFS (about −24 dBu, ~26 dB under a Meyer limiter), one
  output at a time. Enter moves to the next. The other outputs carry digital zero.
- Watch the M4 LCD output meters: only out N moves. Label the cable ends:
  **OUT1 TOP L · OUT2 TOP R · OUT3 SUB L · OUT4 SUB R** (the current `output_map`).
- On profile `dev` the engine does not hold the M4, so there is no STOP prompt. On `m4` it asks
  you to type STOP, stops k1000 and camilladsp, and starts them again at the end.

## 5. Switch the profile to `m4`

A device change: about 1 s of silence, then the 3 s ramp. Bench only.

```sh
sudo sed -E -i 's/^profile: dev$/profile: m4/' /etc/k1000/k1000.yaml && grep -n '^profile' /etc/k1000/k1000.yaml
sudo systemctl restart k1000
journalctl -u k1000 -n 40 --no-pager
grep -A3 -E '^  (capture|playback):' /var/lib/k1000/active.yml
tail -n 30 /var/log/k1000/camilladsp_rCURRENT.log
```

| Expect | Where |
|---|---|
| `profile m4 (settings)` and no `m4:` warning line | k1000 journal |
| `engine runs 'K1000 dev', k1000 runs profile m4: switching the engine (device restart, about 1 s of silence, then the boot ramp)`, then RAMPING IN | k1000 journal, screen |
| `type: Alsa`, `channels: 4`, `device: hw:M4`, no `format:` line (CamillaDSP picks) | active.yml |
| No ERROR, no underrun | engine log |
| ● RUNNING, "M4 · MOTU M4", no NO DEVICE, no `ROUTING FIXED UP` alert | screen |

Then run the check tool again: section 2 now shows what the engine opened.

Once 4/4 is confirmed, pin it under `profiles: m4:` in `/etc/k1000/k1000.yaml`
(`capture_channels: 4`, `playback_channels: 4`), then `sudo systemctl restart k1000`. A late-plugged
M4 then always gets the proven config (review finding m4-4).

**target_level** (decision below). If Thomas picks 512 or 768, add `target_level: 512` (or 768) under
`profiles: m4:`, `sudo systemctl restart k1000` (rewrites `active.yml`; the screen may show
DEVICES CHANGED (GUI)), then `sudo systemctl restart camilladsp` (about 1 s of silence, then
the ramp). It applies at the next engine start only, never at show time.

## 6. Prove the path and the front panel (speakers off)

**Before this step:**

- **The Knob test (§10) first.** The balance, Lo-Cut and Mute rows need B1, B3 and B2 on the right
  buttons, and the box still holds the default `controls:` map.
- **SETUP › Mono subs OFF** (two pushes) for this step; the box has it ON. With Mono subs ON, both
  subs get L+R at −6 dB each, so a tone in LINE IN 3 alone lights out3 **and** out4, 6 dB under
  out1: that is mono, not a fault. Note the Lo-Cut frequency (ALIGN, E4) too. Set both back after
  the step.

Phone tone generator, moderate level, into **LINE IN 3 only**. K1000 master at −20 dB (E1 on SHOW).
Peak reader (2 s window per call):

```sh
python3 -c 'import json,websockets.sync.client as c
w=c.connect("ws://127.0.0.1:1234")
for n in ("GetCaptureSignalPeakSince","GetPlaybackSignalPeakSince"):
  w.send(json.dumps({"command":n,"value":2.0})); print(n[3:10],[round(x,1) for x in json.loads(w.recv())["value"]])'
```

| Test | Do | Expect |
|---|---|---|
| Program path | tone in LINE IN 3 | Capture: only index 2 live. Playback: index 0 (out1 Top L) and index 2 (out3 Sub L) at about the input level −20 dB; 1 and 3 at the floor. M4 LCD: In 3, Out 1, Out 3 move |
| Right side | tone in LINE IN 4 | Capture index 3; outs 2 and 4 |
| Tops vs subs | ALIGN (B1 tap), E2 BALANCE up to +12 (fully to subs) | out3/4 +6 dB, out1/2 −6 dB (against balance 0) |
| Lo-Cut | 60 Hz tone; ALIGN, hold B3 (`EDIT 0:30`), E4 LO-CUT up to 300 Hz. E4 reads `OFF`: SETUP › Lo-Cut ON first (two pushes) | out1/2 far below out3/4 |
| Mute | hold B2 (Knob test not done: Main mute in CamillaGUI instead) | all four outputs at the floor. Then restore balance (0), the Lo-Cut frequency, unmute |
| Loop (MONITOR = unity) | TRS cable from out N into front INPUT 1 (GAIN 1 fully counter-clockwise, 48V off, MON 1 unlit) | Capture index 0 within about ±0.5 dB of playback index N−1, for N = 1…4; out1 equal to out3 (Mono subs OFF) |
| MONITOR knob | loop on out1, turn MONITOR down | only outs 1-2 fall. Back fully clockwise |
| Leak (MON MIX blocks inputs) | K1000 muted, tone in LINE IN 3, loop on out1, press 3-4 | capture index 0 stays at the floor. Turning MON MIX counter-clockwise makes it rise: the raw program past K1000 and its mute. Back fully clockwise, 3-4 off, unmute |
| Optional: emergency feed | `sudo systemctl stop camilladsp`, press 3-4, MON MIX fully counter-clockwise | Is the raw program on outs 1-2 with the engine stopped? If yes, that is a two-gesture emergency feed to the tops. Restore the panel, `sudo systemctl start camilladsp` |
| Optional: persistence | M4 power switch off and on | MON buttons and knobs unchanged? |

If out1/2 read lower than out3/4 with MONITOR fully clockwise, write the difference down; do not
trim yet (Friday's mic tune sets the output trims).

End of the step: SETUP › Mono subs back ON if that is the choice for Sunday (two pushes).

## 7. Levels

Research figures (UNVERIFIED until measured): LINE IN 3/4 clip at +18 dBu with no gain knob; the TRS
outs reach +16 dBu; Meyer UPA-1P/USW-1P limiting starts at 0 dBV (+2.2 dBu), i.e. about −14 dBFS at
the M4 outputs. So K1000 at master 0 dB with flat trims is the old direct-cable level −2 dB, and the
M4 cannot overdrive a Meyer input.

| Source on Sunday | Input trim (SETUP) | Check |
|---|---|---|
| Pro, +4 dBu (DJ mixer master XLR, console) | 0 dB | loud track at the source's normal level: capture index 2/3 peaks around −12…−6 dBFS, never IN CLIP, M4 LCD In 3/4 never red |
| Consumer, −10 dBV (RCA) | about +10…+12 dB | same |
| Too hot (IN CLIP, LCD red) | trim cannot help: it acts after the converter | two −10 dB XLR inline pads, or the DJ keeps the master out of the red |

Friday, with the Meyers on: noise at −20 dBFS, find the show master with the Meyer limit LEDs dark,
then set the phone ceiling (`volume_max` = show level + 6 dB, README).

## 8. Presence drills (speakers off; time each)

| Drill | Expect |
|---|---|
| a. Pull the USB during a tone | NO DEVICE within about 1 s. `journalctl -u camilladsp -n 20` says `waiting for audio device(s) hw:M4` (once, then every 30 s; no restart churn). Copy the logs: `cp /var/log/k1000/camilladsp_r*.log ~/m4-day/` |
| b. Plug it back | engine starts within ~1 s of the card (first revision: +4 s), then RAMPING IN −40 dB → saved master over ~3 s. No k1000 restart needed |
| c. Box powered with the M4 unplugged, plug 30 s later | NO DEVICE, then audio ramps in |
| d. M4 power switch off / on | as b. MON buttons and knobs unchanged |
| e. Optional: pops | headphones at low level on the phones out while pulling the USB: pop or silence? Phones back down after |

## 9. Soak (1 hour, real program into LINE IN 3/4)

Its own unit and its own log (the night soak's `soak-night.log` and the default `soak.log` stay out
of it: `--summary` judges a log from its first sample to its last):

```sh
sudo systemd-run --unit=k1000-soak-m4 --uid=k1000 -E K1000_SOAK_LOG=/var/log/k1000/soak-m4.log \
  /opt/k1000/tools/k1000-soak.sh
/opt/k1000/tools/k1000-soak.sh --summary /var/log/k1000/soak-m4.log   # after the hour: "clean", or what needs a look
sudo systemctl stop k1000-soak-m4
```

Expect `xruns=0`, `rate` about 48000, `restarts` flat, `throttled=0x0`. Thursday night: the overnight
soak at the gig `target_level`, again with its own unit and log (`k1000-soak-thu`, `soak-thu.log`).

## 10. Knob test (required; do it before §6)

SETUP → Knob test. Press every button and turn every knob, and note for each physical position the
`sw`/`enc` index shown (the k1000 restart below brings SHOW back; later, a Knob test is left with
two B1 holds: SETUP, then SHOW). Write `controls:` in `/etc/k1000/k1000.yaml`, `sudo systemctl restart k1000`, then check: hold B2
mutes and unmutes on every page, a B1 tap steps SHOW → ALIGN → SETUP, a B3 tap on SHOW moves the
selected band, a B4 tap on SHOW shows the notch hint (`P1 · … · E3 TO CUT`, nothing audible). Label
the shell: PAGE · MUTE · BAND/UNLOCK · NOTCH (the operator card's labels). Fill in the "Where" line
and the bypass patch of `docs/04-operator-card.md` and the bypass patch of `docs/13-quick-start.md`,
then print both (`tools/make-print.sh`). Knob labels drawn at the wrong screen corners =
`KNOB_CORNERS` in `box/k1000/ui/input_map.py` (a code change).

## 11. Tape and label

- Front panel: MONITOR, INPUT MONITOR MIX, both GAINs, the phones knob, the power switch; tape over
  MON 1, MON 2, 3-4 and 48V.
- Cables: OUT1 TOP L, OUT2 TOP R, OUT3 SUB L, OUT4 SUB R; IN L (LINE IN 3), IN R (LINE IN 4).
- Bypass kit packed next to them, labelled, and its patch written on the operator card.

## 12. Report back

| Fact | From |
|---|---|
| USB `idProduct` and `bcdDevice`; new or second-hand | step 1 |
| `stream0.txt`, `bringup-1.txt` (RESULT line, every FAIL/WARN), `amixer.txt` | steps 2–3 |
| `throttled` at plug-in and after 10 min | step 3 |
| Which LCD meter moved for each beep count | step 4 |
| The k1000 journal lines after the switch; the `active.yml` devices block | step 5 |
| Program path, mute, loop (4 pairs of numbers), MONITOR, leak; optional emergency feed and persistence | step 6 |
| Source type, its capture peaks on a loud track, the input trim chosen | step 7 |
| Drill timings a–d, pops (e) | step 8 |
| `--summary` output | step 9 |
| Knob test map (physical position → `sw`/`enc`), or "not done" | step 10 |
| The decisions below | Thomas |

## Decisions for Thomas

| Question | Recommendation (M4 review) |
|---|---|
| Output mapping: outs 1-2 are the only pair the front panel touches (MONITOR, MON MIX, direct monitoring). Keep out1/2 = tops, out3/4 = subs? | Keep it and tape the knobs. The phones then let you hear the processed top feed. Swap (tops on the fixed outs 3-4) only if the M4 sits within reach of DJs or guests |
| `target_level` on the M4: 256 (~5 ms, the bench value) or 512–768 (~11–16 ms, the Pi 4 + M4 reference)? | 512 or 768 for Sunday, soaked overnight Thursday at that exact value. Live mics with stage monitors would argue for the lower one |
| What feeds K1000 on Sunday, and the cables into LINE IN 3/4 | Ask the DJ to keep the master out of the red; pack two −10 dB XLR pads |
| Four balanced TRS-M → XLR-M cables for the outs | No TS, no RCA (the outs are not cross-coupled; RCA tops out at +9.5 dBu) |
| Firmware update | Not before Sunday unless Linux problems show up (it needs a Mac or Windows machine) |
| Power: which PSU feeds the Zynthian shell? | If `throttled` is not 0x0 with the M4 and the screen on: powered USB hub or a stronger PSU before Friday |

## Still UNVERIFIED after research (settle today)

MON MIX fully clockwise mutes the monitored inputs completely · MONITOR fully clockwise is exactly
unity · direct monitoring works while the host is not streaming · MON button states survive a power
cycle · the M4 exposes an ALSA mixer volume or not · pops on the DC-coupled outs at USB power loss ·
the exact `stream0` text (formats, channels) · the underrun margin at `target_level` 256 over USB.
