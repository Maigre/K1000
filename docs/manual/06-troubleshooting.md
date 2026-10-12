# 6 — Troubleshooting

**During a show, any doubt about the sound: bypass** ([§4](#4-the-bypass)). Diagnose afterwards.

The screen messages below are the v0 panel's, as the code shows them today. UI v1 keeps the same
overlays and alerts and adds its own hints ([`docs/07-live-ui-v1.md`](../07-live-ui-v1.md) §2.7).
The phone page's own messages are in [`docs/06-phone.md`](../06-phone.md) §7.

## 1. The screen

### Status bar (top line, every page)

| Shows | Means | Do |
|---|---|---|
| `MAIN`, `LIVE EQ`, `SETUP`, `TEST`, `PHONE` (white pill) | The page | — |
| `MUTED` (red pill) | Main is muted | Hold B2 to unmute when ready |
| `IN CLIP` (red pill) | An M4 input carrying the program reached −0.5 dBFS (held 3 s) | Source down, or inline pads. No K1000 knob fixes it: the trim acts after the converter |
| `CLIP` (red pill) | An output reached full scale, or the engine counted clipped samples (held 3 s) | Master down; check the live and system EQ for a big boost |
| `M4 · MOTU M4` in white | Profile and device; the device is there | — |
| … in orange | The device is missing (NO DEVICE below) | — |
| `M4 · MOTU M4 ?` / `DEVICE ?` in amber | K1000 cannot tell whether the device is there | Wait. Audio never depends on the screen |
| ● `RUNNING` green | The engine is processing | — |
| ● `STARTING` / `PAUSED` yellow | The engine is not processing yet | Wait a few seconds; then check the source and the M4 |
| ● `INACTIVE` / `STALLED` red | The engine stopped, or gets no input data | See ENGINE DOWN |
| `NO ENGINE` red | No connection to CamillaDSP | See ENGINE DOWN |
| `ENGINE ?` amber | Not known yet (just started) | Wait |
| `DSP 1.2%` | The engine's processing load | Normal: a few percent |

### Full-screen states

| Screen | Means | Do |
|---|---|---|
| **NO DEVICE** (orange, [picture](../ui/no_device.png)): "MOTU M4 not found · check USB cable and power · profile m4 is pinned: no fallback · NO AUDIO — bypass if needed" | The audio interface is gone. No audio | Reseat the USB cable, check the M4's power switch. The engine starts within about a second of the card coming back (the M4 itself takes a few seconds to appear), then ramps in. Still there after 30 s: bypass |
| **ENGINE DOWN** (dark red, [picture](../ui/engine_down.png)): "no connection to CamillaDSP" or "CamillaDSP inactive", "restarting automatically…" | The engine stopped. systemd restarts it within 1–2 s; it comes back muted and K1000 ramps it in | Wait about 5 s. If it stays: bypass, then the logs (§5) |
| **ENGINE DOWN** with "no input data · check the interface / USB" | The engine is `STALLED`: the capture side gives no data | Check the M4's USB and power. Nothing restarts a stalled engine by itself |
| **UI ERROR**: "screen fault · journalctl -u k1000" | The screen code hit a bug. It still shows MUTED when it can tell | Audio is not affected. Read `journalctl -u k1000` later |
| "K1000 CONTROL · restarting… · audio unchanged · knobs inactive" | The k1000 service is stopped or restarting. The engine keeps playing as it was | Wait. The knobs and B2 do nothing until it is back. If it stays: `systemctl status k1000` |
| Dark screen | See [§3](#3-symptoms) | — |

NO DEVICE wins over ENGINE DOWN, which wins over the MUTED band. The PHONE page (SETUP › Phone) is
the exception: there NO DEVICE and ENGINE DOWN only take the footer strip (MUTED takes it first),
so the QR code stays visible.

### The MUTED band

When Main is muted, MAIN and LIVE EQ get a red frame and a red band ([picture](../ui/muted.png)).
SETUP and PHONE get the red frame and a red footer strip instead (a band would cover the selected
row or the QR); the Knob test page gets the frame and a red `MUTED` tag. On MAIN, LIVE EQ and
SETUP, the text next to MUTED says what happens next (the PHONE footer only says `hold B2 to
unmute`). It never says something false.

<!-- TODO(after UI v1): re-check every string and picture in §1 against box/k1000/control.py,
box/k1000/ui/pages.py, box/k1000/ui/input_map.py and the regenerated docs/ui/*.png (the four
links no_device.png, engine_down.png, muted.png, muted_ramp_hold.png may be renamed). -->


| Band text | Means | Do |
|---|---|---|
| `READY · hold B2 to unmute` | Your mute (or a mute saved at power-off). The engine runs and K1000 is in sync | Hold B2 when ready |
| `hold B2 to unmute` | Muted, but the engine is not running yet, or K1000 is not in sync | Wait for `READY` |
| `RAMPS IN BY ITSELF · hold B2 to keep muted` | The engine (re)started. K1000 holds Main muted and will fade it in over about 3 s **by itself** ([picture](../ui/muted_ramp_hold.png)) | Nothing, or hold B2 to keep it muted |
| `NO AUDIO FLOWING · HELD MUTED` | K1000 waits for the engine to process before ramping in. No timeout | Check the M4's USB and power, and the source |
| `APPLYING SETUP…` | A setup action (mono subs, audition, profile) is rebuilding the pipeline with Main muted | Wait a second |
| `KNOBS OFFLINE · USE PHONE MUTE / BYPASS` | See the alerts below | — |
| any other alert | See the alerts below | — |

### Alerts and messages (the middle strip)

Messages show in a dark-amber strip in the middle of the page. The serious ones (alerts) also show
inside the MUTED band and on the SETUP page's footer. Highest priority first:

| Message | Means | Do |
|---|---|---|
| `KNOBS OFFLINE · USE PHONE MUTE / BYPASS` (alert) | The knob board does not answer for more than 1 s. **B2 does nothing meanwhile.** K1000 retries every 2 s | Mute from the phone if it is in use, else the source down; bypass if needed. Then §3 "Knobs dead" |
| `CANNOT SAVE STATE` (alert) | Writing `state.json`/`active.yml` failed; retried after 2, 5, 10 s | Audio is not affected. After the show: the SD card (`dmesg`), free space (`df -h /var/lib/k1000`) |
| `ENGINE SYNC FAILED (SEE LOG)` (alert) | K1000 could not bring the engine in line on connect; it leaves the engine as it is and retries every 2 s | Bypass only if the sound is wrong. `journalctl -u k1000` |
| `SETTINGS: …` (alert) | `k1000.yaml` could not be used as written (does not parse, unknown profile…). The text says why | Fix the file ([chapter 3](03-configuration.md)), restart k1000 |
| `k1000.yaml ROUTING FIXED UP (SEE LOG)` (alert) | An `input_map` or `output_map` was wrong and replaced by a default | Fix the map; the journal says what was replaced |
| `NO AUDIO FLOWING · HELD MUTED` (alert) | As in the MUTED band | — |
| `APPLYING SETUP…` (alert) | As in the MUTED band | — |
| `RAMPING IN` | The boot ramp is running | — |
| `PIPELINE CHANGED OUTSIDE K1000` | Someone changed the engine's structure (mixers, pipeline, a filter type), usually a CamillaGUI Apply. K1000 restores it with Main muted around it, at most once per 30 s | Stop applying configs from CamillaGUI |
| `CONFIG DRIFT (SEE LOG)` | The engine still differs after K1000 re-sent a fix three times | `journalctl -u k1000` |
| `RESTORING ENGINE DEVICES` | The engine runs on another audio device or channel count than the profile (e.g. a stale CamillaGUI page applied a default config). K1000 uploads the right one with Main muted, then ramps in | Stop applying configs from CamillaGUI |
| `ENGINE DEVICES CHANGED OUTSIDE K1000` | Another device setting differs (`target_level`, `volume_limit`, chunk size…). Left alone while audio runs: fixed at the next engine start | Between shows: `sudo systemctl restart camilladsp` (about 1 s of silence, then the ramp) |
| `SUBS GROUP MUTED OUTSIDE K1000`, `TOPS GROUP -3.0 DB OUTSIDE K1000`, `<FILTER> MUTED OUTSIDE K1000`, and the same with `<FILTER> +1.0 DB`, `<FILTER> GAIN …` or `<FILTER> LIMIT …` (`<FILTER>` = the CamillaDSP filter's name, in capitals) | A cut or a mute made outside K1000 (CamillaGUI). **K1000 keeps it**: it never raises or unmutes it by itself | Undo it in CamillaGUI if it was not meant |
| `N CUTS/MUTES MADE OUTSIDE K1000` | Several of those | As above |
| `STATUS ? engine · device` | K1000 has not reported the engine or the device state yet | Wait |

### Hints (amber strip, about 2.5 s)

| Hint | Means |
|---|---|
| `HOLD B2 TO MUTE / UNMUTE` | B2 was only tapped |
| `HOLD B4 TO RESET LIVE EQ` / `HOLD B4 AGAIN TO RESET ALL LIVE EQ` | B4 on LIVE: the first hold only arms the reset for 5 s (the second hint stays those 5 s) |
| `RESET LIVE EQ: B4 ON THE LIVE PAGE` | B4 elsewhere |
| `LO-CUT: B3 ON THE MAIN PAGE` | B3 elsewhere |
| `LO-CUT ON · 100 Hz` / `LO-CUT OFF` | B3 on MAIN switched it |
| `SUB DELAY · TURN AGAIN TO CHANGE` | Sub delay was at rest for 2.5 s: the first detent only wakes it |
| `LIVE EQ RESET (ALL BANDS FLAT)` / `LIVE EQ ALREADY FLAT` | After the reset |
| `PUSH TO CHANGE` / `PUSH AGAIN TO APPLY (MAY CLICK)` | SETUP: a switch item needs a push; a pipeline item (mono subs, audition, profile) needs a second push within 5 s |
| `MUTE OR CONFIRM ON SETUP (MAY CLICK)` | A setup action asked for without the confirm and while unmuted |
| `PROFILE PINNED IN k1000.yaml` | SETUP › Profile is locked by the pin |
| `MUTE CHANGED · LOOK, HOLD B2 AGAIN` | An unmute was refused: someone (a phone) muted or unmuted while you held B2. Look at the screen, then decide |
| `STILL MUTED · HOLD B2 TO UNMUTE` | An unmute pressed while the engine link was down never reached it |
| `ENGINE REFUSED THE CHANGE` | CamillaDSP rejected an edit; K1000 re-reads the engine |
| `SETUP INTERRUPTED` / `SETUP FAILED` / `PROFILE M4 FAILED` | A setup action did not complete; the previous setting is kept |
| `PHONE CONTROL ON` / `PHONE OFF · KNOBS ONLY` / `PHONE DISABLED (k1000.yaml / --no-phone)` | SETUP › Phone |
| `STATE REBUILT · CHECK MASTER` (15 s) | `state.json` was unreadable: the tune was rebuilt from `active.yml`, master and balance are defaults, the box is **muted**. Check E1 and E2, then hold B2 |
| `NO SAVED STATE · CHECK MASTER` (15 s) | `state.json` was missing on a box that was set up before: defaults, **muted**. Same check |

### The meter

Four bars: Sub L, Sub R, Top L, Top R, −60 … 0 dBFS. Green below −18 dBFS, yellow to −6, red above.
A red box on top of a bar = that output clipped. The Meyers start limiting around −14 dBFS at the
M4 outputs (research, [chapter 5](05-tuning.md#3-gain-structure)).

### SETUP › Phone page

| Shows | Do |
|---|---|
| `Wi-Fi AP ON` | — |
| `Wi-Fi AP OFF` / `Wi-Fi AP is OFF` | Expected after a phone no-go (`setup-ap.sh --down`). Otherwise `sudo /opt/k1000/net/setup-ap.sh --status` |
| `PHONE OFF · knobs only` | Push E1 to turn phone control on |
| `PHONE DISABLED · phone.enabled: false` | Set in `k1000.yaml`; no phone servers |
| `no HTTPS: page only` | No certificate in `/etc/k1000/tls`: the page works on `http://10.42.0.1:8080/` but cannot be installed |
| `QR unavailable · python3-qrcode` | The package is missing. With internet: `sudo apt-get update && sudo apt-get install python3-qrcode`, then `sudo systemctl restart k1000` (between shows: k1000 looks for it only when it starts). Meanwhile the page shows the same information as text |
| `no Wi-Fi password · run setup-ap.sh` | `/etc/k1000/ap.psk` is missing or unreadable |
| An error line in small print | e.g. `phone certificate EXPIRED …`, `bad certificate/key …`, `cannot listen on …`: the phone servers' own errors. Certificates: [chapter 7](07-maintenance.md#the-phone-certificate) |

## 2. The quick checks

```sh
ssh k1000 'systemctl is-active camilladsp k1000 camillagui'
ssh k1000 'cat /proc/asound/cards'                 # is the M4 there? (card id "M4")
ssh k1000 'vcgencmd get_throttled'                 # 0x0 = no under-voltage or heat
ssh k1000 'journalctl -u k1000 -n 40 --no-pager'
ssh k1000 'journalctl -u camilladsp -n 40 --no-pager'
```

## 3. Symptoms

### No sound at all

1. The screen: NO DEVICE, ENGINE DOWN, or MUTED? Act on that first (§1).
2. **The box meters move?**
   - **No**: no program reaches K1000. The source, its cables into LINE IN 3/4, the M4's input
     meters. `IN CLIP` or nothing at all? On the bench, `input_map` in `k1000.yaml` must name the
     inputs the program uses (`m4`: `[2, 3]` = LINE IN 3/4).
   - **Yes**: the problem is after K1000. The M4's output meters, the output cables (TRS → XLR,
     never TS), the speakers' power and their own input settings. Outs 1-2 also follow the M4's
     MONITOR knob: it must be fully clockwise.
3. The master: at −80 dB (the bottom of its range) the PA is practically silent.

### One side, or the subs, or the tops missing

| Missing | Check |
|---|---|
| One output | The M4's output meter for it; the cable; `output_map` |
| Both tops (outs 1-2) | The M4's MONITOR knob (it scales outs 1-2 only); a `TOPS GROUP MUTED OUTSIDE K1000` strip |
| Both subs | A `SUBS GROUP MUTED OUTSIDE K1000` strip; SETUP › Sub low-pass on at a very low frequency |
| One side of everything | The source side: the capture meter, the input cable; mono subs hides a missing side on the subs only |
| Only on the bench (`dev`) | SETUP › Audition (dev) is on `tops` or `subs` |

### Hum or buzz

Not something K1000 creates. Usual causes:

- An unbalanced link somewhere: TS jacks, RCA outputs. Use balanced TRS → XLR.
- A ground loop through a laptop on its charger (USB or Ethernet to the box): unplug the laptop
  and listen.
- The source's own output. A DI box or isolation transformer on the source fixes most loops.
- **Never lift a safety earth.**

### Clicks, dropouts, crackles

| What | Cause | Do |
|---|---|---|
| A short dip when changing a delay or a polarity | Expected: the group is muted around the write | Do alignment before the show |
| A click on SETUP › Mono subs / Profile / Audition | Expected: those rebuild the pipeline (hence the confirm) | Never during a show |
| Regular crackles or dropouts | Audio buffer underruns (xruns) | The engine log (below), `vcgencmd get_throttled`, a soak test. A larger `target_level` (512 or 768) gives the USB more margin ([chapter 3](03-configuration.md#4-profiles)) |
| Knob moves | Should never click: gains move in 0.5 dB steps (0.1 dB on fixed gains), frequency and Q in small steps | If they do, note what and when |

```sh
grep -iE 'underrun|overrun|xrun|stalled' /var/log/k1000/camilladsp_rCURRENT.log
```

### Phone cannot connect

The full table is [`docs/06-phone.md`](../06-phone.md) §7. The usual ones:

| Symptom | Do |
|---|---|
| The phone does not list "K1000" | 5 GHz channel 36 is indoor-only in the EU and not every phone has 5 GHz: `sudo /opt/k1000/net/setup-ap.sh --band bg` (2.4 GHz). The choice sticks |
| Joins, then drops after a few seconds | `sudo /opt/k1000/net/setup-ap.sh --status` (backend hostapd, PMF unset), then `--band bg`, then a USB Wi-Fi dongle |
| "Not connected to K1000" | The phone left the K1000 Wi-Fi: Wi-Fi off/on. With mobile data on, Android may route around it: **airplane mode, then Wi-Fi on** |
| "Your connection is not private" | The K1000 CA is not installed on the phone, or not as a **CA certificate** |
| "Phone control is OFF on the box" | SETUP › Phone, push E1 |
| "K1000 already has the maximum number of phones" | 4 at most (`phone.max_clients`) |
| SETUP › Phone shows `Wi-Fi AP OFF` | §1 above |

### Screen dark

| Check | How |
|---|---|
| The backlight and the overlay | `grep k1000-zynscreen /boot/firmware/config.txt` must show `dtoverlay=k1000-zynscreen,rotate=270`. If not: `sudo /opt/k1000-src/install.sh --boot`, then power-cycle |
| The panel registered | `cat /sys/class/graphics/fb*/name` lists `fb_ili9486` |
| k1000 runs | `systemctl status k1000`. It retries the screen every 2 s for its whole life |
| Text drawn over the UI | The Linux console won the panel: `systemctl status k1000-console` should say `console unbound N time(s)`; `sudo systemctl restart k1000` unbinds it again |

The audio does not depend on the screen.

### Knobs dead

`KNOBS OFFLINE` means the knob board does not answer (B2 too). Between shows:

```sh
ls /dev/i2c-1                                   # missing: install.sh --boot, then power-cycle
journalctl -u k1000 | grep -i knob              # "knobs on i2c-1 0x20 (irq=True)" when they open
sudo systemctl stop k1000                       # the audio keeps playing; B2 does nothing now
sudo i2cdetect -y 1                             # the MCP23017 answers at 20
cd /opt/k1000 && python3 -m k1000.hw.mcp23017 --monitor     # turn and press: events print
sudo systemctl start k1000
```

A loose ribbon cable inside the case is the usual hardware cause.

**Knobs work but do the wrong thing**: the `controls:` map ([chapter 3](03-configuration.md#5-controls)).
**Knobs feel imprecise when turned slowly or fast**: a known decoder issue, analysed in
[`docs/08-knob-precision.md`](../08-knob-precision.md). Until its fix lands, turn briskly and
use the push for fine steps.
<!-- TODO(after UI v1): update once Patch A/B (docs/08) has landed. -->


## 4. The bypass

The rig played without K1000 before, and Meyer designed it to: the same full-range feed to tops
and subs. **Bypass = the source straight into the speakers, as it was cabled before K1000.** About a
minute:

1. **Source down.** Hold B2 if K1000 still answers.
2. Unplug OUT1 TOP L, OUT2 TOP R, OUT3 SUB L, OUT4 SUB R at the speakers.
3. Patch the bypass kit, as cabled before K1000 (write your own patch on the operator card).
4. **Source up slowly.** The bypass is louder than K1000 at a low master: about (2 − master) dB, so
   +12 dB at master −10, with input trim and output trims at 0 (research, from the level figures
   in [chapter 5](05-tuning.md#3-gain-structure)).

Lost in bypass: the Lo-Cut, the delays, the EQ, the mute.

## 5. Logs

| What | Command (on the box) |
|---|---|
| The control service, live | `journalctl -u k1000 -f` |
| The engine's starts and stops, "waiting for …" lines | `journalctl -u camilladsp -n 50` |
| The engine's own log (xruns, device errors, config loads) | `tail -f /var/log/k1000/camilladsp_rCURRENT.log` |
| Older engine logs | `/var/log/k1000/camilladsp_r<date>.log`: up to 50, rotated at every engine start and every 256 kB |
| Did the box reboot? When? | `journalctl --list-boots` |
| The boot before this one (after a power cut) | `journalctl -b -1 -u k1000 -u camilladsp` |
| A soak run | `/opt/k1000/tools/k1000-soak.sh --summary` ([chapter 7](07-maintenance.md#soak-test)) |

The journal is kept on the SD card (32 MB at most) and written to it every 30 s, so after a power
cut `journalctl -b -1` shows what happened before it, minus up to its last 30 s.

**After a dropout, keep the evidence first** (each engine start rotates one log):

```sh
mkdir -p ~/incident && cp /var/log/k1000/camilladsp_r*.log ~/incident/ && journalctl -b --no-pager > ~/incident/journal.txt
```

After a power cycle, also `journalctl -b -1 --no-pager > ~/incident/journal-before.txt`.

**Real-time priority.** The engine's log must not say `could not get real time priority`:

```sh
pid=$(systemctl show -p MainPID --value camilladsp); for t in /proc/$pid/task/*; do echo "$(cat $t/comm): $(chrt -p ${t##*/} | head -1)"; done
# the processing and AlsaPlayback/AlsaCapture threads show SCHED_FIFO
```
