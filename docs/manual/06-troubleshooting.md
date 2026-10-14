# 6 — Troubleshooting

**During a show, any doubt about the sound: bypass** ([§4](#4-the-bypass)). Diagnose afterwards.

The screen messages below are the ones the code shows (UI v1, D-009). How the panel works:
[chapter 4](04-usage.md). The phone page's own messages are in
[`docs/06-phone.md`](../06-phone.md) §7.

## 1. The screen

Three kinds of message, never in the same place or colour ([chapter 4 §7](04-usage.md#7-alerts-hints-and-pills)):
**alerts** take the status bar (red critical, orange warning, blue passing state), **hints** use
the amber strip for a few seconds (what your own press did), **pills** sit on SHOW's bottom row
(a state that lasts).

### Status bar (top line, every page)

| Shows | Means | Do |
|---|---|---|
| `SHOW`, `ALIGN`, `SETUP`, `TEST`, `PHONE` (white pill) | The page. Shortened to `SHW`, `ALN`, `SET`, `TST`, `PHN` while an alert takes the bar | — |
| `MUTED` (red pill) | Main is muted | Hold B4 to unmute when ready |
| `IN CLIP` (red pill) | An M4 input carrying the program reached −0.5 dBFS (held 3 s) | Source down, or inline pads. No K1000 knob fixes it: the trim acts after the converter |
| `CLIP` (red pill) | An output reached full scale, or the engine counted clipped samples (held 3 s) | Master down; check the live and system EQ for a big boost |
| `M4 · MOTU M4` in white | Profile and device; the device is there | — |
| ● `RUNNING` green | The engine is processing | — |
| ● `STARTING` / `PAUSED` yellow | The engine is not processing yet | Wait a few seconds; then check the M4 (USB, power). A silent source does not do this: K1000 leaves CamillaDSP's silence pause off, so the engine reads `RUNNING` with the meters still (§3, No sound at all) |
| ● `INACTIVE` / `STALLED` red | The engine stopped, or gets no input data | See ENGINE DOWN |
| `NO ENGINE` red | No connection to CamillaDSP | See ENGINE DOWN |
| `ENGINE ?` amber, and the orange alert `STATUS ? engine · device` | K1000 cannot tell yet whether the engine runs or the device is there ([picture](../ui/unknown_status.png)) | Wait. Audio never depends on the screen |
| `DSP 1.2%` | The engine's processing load | Normal: a few percent |
| A coloured bar across the top | A system alert | See "Alerts" below |

### Full-screen states

| Screen | Means | Do |
|---|---|---|
| **NO DEVICE** (orange, [picture](../ui/no_device.png)): "MOTU M4 not found · check USB cable and power · profile m4 is pinned: no fallback · NO AUDIO — bypass if needed"; the status bar says `NO DEVICE · MOTU M4` | The audio interface is gone. No audio | Reseat the USB cable, check the M4's power switch. The engine starts within about a second of the card coming back (the M4 itself takes a few seconds to appear), then ramps in. Still there after 30 s: bypass |
| **ENGINE DOWN** (dark red, [picture](../ui/engine_down.png)): "no connection to CamillaDSP" or "CamillaDSP inactive", "restarting automatically…"; the status bar says `ENGINE DOWN` | The engine stopped. systemd restarts it within 1–2 s; it comes back muted and K1000 ramps it in | Wait about 5 s. If it stays: bypass, then the logs (§5) |
| **ENGINE DOWN** with "no input data · check the interface / USB" | The engine is `STALLED`: the capture side gives no data | Check the M4's USB and power. Nothing restarts a stalled engine by itself |
| **UI ERROR**: "screen fault · journalctl -u k1000" | The screen code hit a bug. It still shows MUTED when it can tell | Audio is not affected. Read `journalctl -u k1000` later |
| "K1000 CONTROL · restarting… · audio unchanged · knobs inactive" | The k1000 service is stopped or restarting. The engine keeps playing as it was | Wait. The knobs and B4 do nothing until it is back. If it stays: `systemctl status k1000` |
| Dark screen | See [§3](#3-symptoms) | — |

NO DEVICE wins over ENGINE DOWN, which wins over the MUTED band. The PHONE page
(SETUP › PHONE › Phone) is the exception: there NO DEVICE and ENGINE DOWN only take the footer
strip (MUTED takes it first), so the QR code stays visible.

### The MUTED band

When Main is muted, every page gets the red `MUTED` pill and a red frame. SHOW and ALIGN also get
a red band ([picture](../ui/muted.png)); SETUP and PHONE get a red footer strip instead (a band
would cover the selected row or the QR); the Knob test gets a red `MUTED` tag. On SHOW, ALIGN and
SETUP, the text next to MUTED says what happens next (the PHONE footer only says
`hold B4 to unmute`). It never says something false. On SETUP, a choice waiting for its push
takes the footer over the MUTED strip.

| Band text | Means | Do |
|---|---|---|
| `READY · hold B4 to unmute` | Your mute (or a mute saved at power-off). The engine runs and K1000 is in sync | Hold B4 when ready |
| `hold B4 to unmute` | Muted, but the engine is not running yet, or K1000 is not in sync | Wait for `READY` |
| `RAMPS IN BY ITSELF · hold B4 to keep muted` | The engine (re)started. K1000 holds Main muted and will fade it in over about 3 s **by itself**; the status bar shows the blue `RAMPING IN` ([picture](../ui/muted_ramp_hold.png)) | Nothing, or hold B4 to keep it muted |
| `NO AUDIO FLOWING · HELD MUTED` | K1000 waits for the engine to process before ramping in. No timeout | Check the M4's USB and power, and the source |
| `APPLYING SETUP…` | A setup action (Mono L+R, Audition, Profile) is rebuilding the pipeline with Main muted | Wait a second |
| `KNOBS OFFLINE · …` | See the alerts below | — |
| any other alert | See the alerts below | — |

### Alerts (the status bar)

An alert replaces the profile and device text in the status bar, in its own colour: **red** =
critical (`KNOBS OFFLINE` takes the whole bar), **orange** = warning, **blue** = a passing state.
Alerts also show inside the MUTED band, and a critical one is repeated in red over SHOW's band
chips when no hint is up ([picture](../ui/knobs_offline_live.png)). When several are up, the most
urgent shows. Highest priority first:

| Alert | Means | Do |
|---|---|---|
| `KNOBS OFFLINE · USE PHONE MUTE / BYPASS` (red) | The knob board does not answer for more than 1 s. **B4 does nothing meanwhile.** K1000 retries every 2 s. This text shows only while a phone can mute right now: phone servers on, SETUP › PHONE › Phone on, the access point up, no subnet clash, the page served | Mute from the phone, else the source down; bypass if needed. Then §3 "Knobs dead" |
| `KNOBS OFFLINE · SOURCE DOWN / BYPASS` (red) | The same, with no phone mute to offer (phone off, a phone no-go, the access point down) | Source down; bypass if needed. Then §3 "Knobs dead" |
| `NO DEVICE · MOTU M4` (red) | See NO DEVICE above | — |
| `ENGINE DOWN`, `ENGINE INACTIVE`, `ENGINE STALLED` (red) | See ENGINE DOWN above | — |
| `CANNOT SAVE STATE` (red) | Writing `state.json`/`active.yml` failed; retried after 2, 5, 10 s | Audio is not affected. After the show: the SD card (`dmesg`), free space (`df -h /var/lib/k1000`) |
| `ENGINE SYNC FAILED (SEE LOG)` (red) | K1000 could not bring the engine in line on connect; it leaves the engine as it is and retries every 2 s | Bypass only if the sound is wrong. `journalctl -u k1000` |
| `SETTINGS: …` (orange) | `k1000.yaml` could not be used as written (does not parse, unknown profile…). The text says why | Fix the file ([chapter 3](03-configuration.md)), restart k1000 |
| `k1000.yaml ROUTING FIXED UP (SEE LOG)` (orange) | An `input_map` or `output_map` was wrong and replaced by a default | Fix the map; the journal says what was replaced |
| `NO AUDIO FLOWING · HELD MUTED` (orange) | As in the MUTED band | — |
| `APPLYING SETUP…` (blue) | As in the MUTED band | — |
| `RAMPING IN` (blue) | The boot ramp is running | — |
| `PIPELINE CHANGED (GUI)` (orange) | Someone changed the engine's structure (mixers, pipeline, a filter type), usually a CamillaGUI Apply. K1000 restores it with Main muted around it, at most once per 30 s | Stop applying configs from CamillaGUI |
| `CONFIG DRIFT (SEE LOG)` (orange) | The engine still differs after K1000 re-sent a fix three times | `journalctl -u k1000` |
| `RESTORING ENGINE DEVICES` (orange) | The engine runs on another audio device or channel count than the profile (e.g. a stale CamillaGUI page applied a default config). K1000 uploads the right one with Main muted, then ramps in | Stop applying configs from CamillaGUI |
| `DEVICES CHANGED (GUI)` (orange) | Another device setting differs (`target_level`, `volume_limit`, chunk size, the fader ramp…). Left alone while audio runs: fixed at the next engine start | Between shows: `sudo systemctl restart camilladsp` (about 1 s of silence, then the ramp) |
| `SUBS GROUP MUTED (GUI)`, `TOPS GROUP -3.0 DB (GUI)`, `<FILTER> MUTED (GUI)`, and the same with `<FILTER> +1.0 DB`, `<FILTER> GAIN …` or `<FILTER> LIMIT …` (`<FILTER>` = the CamillaDSP filter's name, in capitals) (orange) | A cut or a mute made outside K1000 (CamillaGUI). **K1000 keeps it**: it never raises or unmutes it by itself | Undo it in CamillaGUI if it was not meant |
| `N CUTS/MUTES (GUI)` (orange) | Several of those | As above |
| `STATUS ? engine · device` (orange) | K1000 has not reported the engine or the device state yet | Wait |

### Hints (amber strip, 2.5 s unless said)

On SHOW the strip covers the band chips, on ALIGN the middle, on SETUP the footer.

| Hint | Means |
|---|---|
| `HOLD B4 TO MUTE / UNMUTE` | B4 was only tapped |
| `MASTER: SLOW DOWN` | The master-up guard refused some up-clicks: master rises at most 6 dB within any 2 s (`controls.master_up_guard_db`). Down is never limited |
| `P3 · 2.44k · E4 TO CUT` (4 s) | B3 on SHOW set P3 up as a notch at the PEAK (gain still 0 dB), or selected the bell already cutting there. E4 (GAIN, bottom right) cuts, E3 (Q) narrows |
| `P2 · 1.00k · NO PEAK · E4 TO CUT` (4 s) | The same, but no PEAK was shown: the band kept its frequency. E4 down first (about −6 dB: at 0 dB the bell is inaudible), then sweep E2 until the ring drops, then E3 up |
| `NO FREE BAND` (4 s) | B3: all four bells are in use |
| `P2 OUT` / `P2 IN` | B3 held on SHOW (map confirmed): the selected band switched OUT (kept, silent) or back IN (D-014) |
| `B3 HOLD: IN/OUT AFTER KNOB TEST` | B3 held on SHOW while the button map is unconfirmed: nothing changed. Knob test, then `buttons_confirmed: true` |
| `B2: NEXT BAND ON SHOW`, `B3: NOTCH HELPER ON SHOW` | B2 or B3 on a page where it does nothing |
| `LOCKED · HOLD B2 TO EDIT` | ALIGN: E2 (DELAY) or E4 (LO-CUT) turned (or B2 tapped) while locked. Nothing changed |
| `DELAY + LO-CUT UNLOCKED` / `DELAY + LO-CUT LOCKED` | ALIGN: a B2 hold unlocked them for 30 s of idle; a B2 tap locked them |
| `DELAY · TURN AGAIN TO CHANGE` | ALIGN's DELAY was at rest for 2.5 s: the first click only wakes it |
| `PUSH TO SET ON`, `PUSH TO SET INVERTED`… (footer, while it waits) | SETUP: E2 chose a value (the row, red, shows `now → chosen`); a push of E1 or E2 sets it. Nothing changes until then; it lapses after 8 s or when E1/E3 moves |
| `PUSH TO SET … · MAY CLICK` | The same on a pipeline rebuild (Mono L+R, Profile, Audition) |
| `NO CHANGE: ALREADY SET` | SETUP: the chosen value was already set (a phone changed it meanwhile) |
| `TURN THE VALUE KNOB` | SETUP: a push on a number row; E2 changes it |
| `PUSH TO OPEN`, `PUSH TO RUN`, `PUSH TWICE TO RUN` | SETUP: E2 turned on a page or an action row |
| `PUSH AGAIN TO RESET ALL LIVE EQ`, `PUSH AGAIN: LAPTOP ACCESS FOR 2 H` | SETUP: the confirms, a second push within 5 s |
| `LIVE EQ RESET · UNDO IN SETUP` / `LIVE EQ ALREADY FLAT` | After SETUP › LIVE EQ › Reset live EQ (or the phone's reset) |
| `LIVE EQ RESTORED` / `NOTHING TO UNDO` | SETUP › LIVE EQ › Undo reset |
| `RESET UNDO LOST` | An audible live-EQ edit (a gain moved off 0 dB) ended the reset's undo |
| `LO-CUT ON · 112 Hz` / `LO-CUT OFF` | SETUP › TOPS › Lo-Cut, or the phone's switch |
| `SPECTRUM ON` / `SPECTRUM OFF` / `SPECTRUM OFF IN k1000.yaml` | SETUP › LIVE EQ › Spectrum; the last one when `spectrum: enabled: false` (the row is read-only then) |
| `LAPTOP ACCESS: OPENING…`, `LAPTOP OPEN 2 H · GUI :5005 · SSH`, `LAPTOP ACCESS CLOSED`, `LAPTOP ACCESS CLOSED (2 H)` (4 s) | SETUP › PHONE › Laptop access ([chapter 4 §5.1](04-usage.md#51-laptop-access-and-the-tune-page)) |
| `LAPTOP ACCESS: NOT AVAILABLE`, `LAPTOP ACCESS: NO ANSWER (SEE LOG)`, `LAPTOP ACCESS NO WI-FI FENCE (AP NOT SET UP)` (4 s) | It could not open: no laptop-access service, the root helper did not answer (`journalctl -t k1000-laptop`), or there is no K1000 Wi-Fi ([`docs/06-phone.md`](../06-phone.md) §7) |
| `MUTE OR CONFIRM ON SETUP (MAY CLICK)` | A setup action asked for (console) without the confirm and while unmuted |
| `PROFILE PINNED IN k1000.yaml` | SETUP › BOX › Profile is read-only: `k1000.yaml` pins the profile |
| `AUDITION: DEV PROFILE ONLY` | SETUP › BOX › Audition (dev) is read-only off the bench profile |
| `MUTE CHANGED · LOOK, HOLD B4 AGAIN` | An unmute was refused: someone (a phone) muted or unmuted while you held B4. Look at the screen, then decide |
| `STILL MUTED · HOLD B4 TO UNMUTE` | An unmute pressed while the engine link was down never reached it |
| `ENGINE REFUSED THE CHANGE` | CamillaDSP rejected an edit; K1000 re-reads the engine |
| `SETUP INTERRUPTED` / `SETUP FAILED` / `PROFILE M4 FAILED` | A setup action did not complete; the previous setting is kept |
| `PHONE CONTROL ON` / `PHONE OFF · KNOBS ONLY` / `PHONE DISABLED (k1000.yaml / --no-phone)` | SETUP › PHONE › Phone |
| `STATE REBUILT · CHECK MASTER` (15 s) | `state.json` was unreadable: the tune was rebuilt from `active.yml`, master and balance are defaults, the box is **muted**. Check master (E1) and balance (ALIGN E3), then hold B4 |
| `NO SAVED STATE · CHECK MASTER` (15 s) | `state.json` was missing on a box that was set up before: defaults, **muted**. Same check |

### Pills (SHOW's bottom row)

`LAPTOP OPEN 1:59`, `LO-CUT OFF`, `POL INV SUB` / `TOP` / `SUB+TOP` (orange) and `MONO SUB`,
`AUDITION TOPS` / `SUBS` (grey): a non-default state, for as long as it lasts. Too many fold into
`+N`. Not a fault: check each one is meant ([chapter 4 §3.3](04-usage.md#33-pills)).

### The spectrum (SHOW)

| Shows | Means | Do |
|---|---|---|
| No spectrum, no message | SETUP › LIVE EQ › Spectrum is off, or the engine has just started (it comes back within a few seconds) | — |
| Grey `NO SPECTRUM` | The engine refuses to give one (or was still warming up after 10 s) | The sound is not affected: SETUP › LIVE EQ › Spectrum off. Later: `journalctl -u k1000 \| grep -i spectrum` |
| No cyan PEAK pointer | No narrow peak, or the program is silent (the PEAK needs the peak band at −80 dBFS and the program at −60 dBFS RMS, [chapter 3 §7](03-configuration.md#7-spectrum)) | B3 then says `NO PEAK`: E4 down first, then find the ring with E2, then E3 up |
| SETUP › LIVE EQ › Spectrum reads `DISABLED` | `spectrum: enabled: false` in `k1000.yaml` | Expected after a spectrum no-go |

### The meters

Six bars in three pairs (D-015): **IN L R** (the program input, before any K1000 gain), **SUB L R**,
**TOP L R** (the outputs), −60 … 0 dBFS. Green below −18 dBFS, yellow to −6, red above. A red box
on top of a bar = that output clipped; on the IN pair = `IN CLIP` (the source is too hot for the
M4's converter). IN moving and SUB/TOP still: K1000 is muted or the master is down. The Meyers
start limiting around −14 dBFS at the M4 outputs (research,
[chapter 5](05-tuning.md#3-gain-structure)).

### The PHONE page

| Shows | Do |
|---|---|
| `Wi-Fi AP ON` | — |
| `Wi-Fi AP is OFF` · `kept down (no-go)` | Expected after a phone no-go (`setup-ap.sh --down`). It stays down across reboots and `install.sh --ap`; back on: `sudo /opt/k1000/net/setup-ap.sh --up` |
| `Wi-Fi AP is OFF` · `eth0 10.42.0.x` · `is in the AP subnet` | A laptop on the Ethernet cable shares its connection on 10.42.0.0/24, the access point's own subnet (NetworkManager's *Shared to other computers* default), so the box keeps its Wi-Fi down. On the laptop, share on another subnet (e.g. 10.43.0.1/24), or unplug it: the access point comes back by itself. SSH over `eth0` works meanwhile |
| `Wi-Fi AP OFF` / `Wi-Fi AP is OFF` (no reason) | `sudo /opt/k1000/net/setup-ap.sh --status`, then [`docs/06-phone.md`](../06-phone.md) §7. Down for good after a Wi-Fi chip reset or repeated failures (systemd gave up on it, or `wlan0` came back without starting it): over `eth0`, `sudo systemctl reset-failed k1000-ap-hostapd k1000-ap-dnsmasq; sudo systemctl start k1000-ap-hostapd` (it brings its DHCP server) |
| `PHONE OFF` · `knobs only` | Push E1 to turn phone control on |
| `LAPTOP OPEN 1:59` and the Wi-Fi password in print | SETUP › PHONE › Laptop access is open: the password is printed for the laptop. Close it when the tune is done |
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
2. **The box meters move?** The IN pair (left) is what comes in, SUB and TOP what goes out.
   - **No**: no program reaches K1000 (IN still). The source, its cables into LINE IN 3/4, the
     M4's input meters. `IN CLIP` or nothing at all? On the bench, `input_map` in `k1000.yaml`
     must name the inputs the program uses (`m4`: `[2, 3]` = LINE IN 3/4).
   - **Yes**: the problem is after K1000. The M4's output meters, the output cables (TRS → XLR,
     never TS), the speakers' power and their own input settings. Outs 1-2 also follow the M4's
     MONITOR knob: it must be fully clockwise.
3. The master: at −80 dB (the bottom of its range) the PA is practically silent.

### One side, or the subs, or the tops missing

| Missing | Check |
|---|---|
| One output | The M4's output meter for it; the cable; `output_map` |
| Both tops (outs 1-2) | The M4's MONITOR knob (it scales outs 1-2 only); a `TOPS GROUP MUTED (GUI)` alert |
| Both subs | A `SUBS GROUP MUTED (GUI)` alert; SETUP › SUBS › Low-pass on at a very low frequency |
| One side of everything | The source side: the capture meter, the input cable; Mono L+R hides a missing side on the subs only |
| Only on the bench (`dev`) | SETUP › BOX › Audition (dev) is on `TOPS` or `SUBS` |

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
| A click on SETUP › SUBS › Mono L+R, BOX › Profile or Audition | Expected: those rebuild the pipeline (hence `· MAY CLICK` and the choose-then-push) | Never during a show |
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
| "Phone control is OFF on the box" | SETUP › PHONE › Phone, push E1 |
| "K1000 already has the maximum number of phones" | 4 at most (`phone.max_clients`) |
| SETUP › PHONE › Phone shows `Wi-Fi AP OFF` | §1 above |
| A laptop on the Ethernet cable, and the phone's Wi-Fi gone | The laptop shares on 10.42.0.0/24: share on another subnet (10.43.0.1/24) or unplug it (§1, the PHONE page) |

### Screen dark

| Check | How |
|---|---|
| The backlight and the overlay | `grep k1000-zynscreen /boot/firmware/config.txt` must show `dtoverlay=k1000-zynscreen,rotate=270`. If not: `sudo /opt/k1000-src/install.sh --boot`, then power-cycle |
| The panel registered | `cat /sys/class/graphics/fb*/name` lists `fb_ili9486` |
| k1000 runs | `systemctl status k1000`. It retries the screen every 2 s for its whole life |
| Text drawn over the UI | The Linux console won the panel: `systemctl status k1000-console` should say `console unbound N time(s)`; `sudo systemctl restart k1000` unbinds it again |

The audio does not depend on the screen.

### Knobs dead

`KNOBS OFFLINE` means the knob board does not answer (B4 too). Between shows:

```sh
ls /dev/i2c-1                                   # missing: install.sh --boot, then power-cycle
journalctl -u k1000 | grep -i knob              # "knobs on i2c-1 0x20 (irq=True)" when they open
sudo systemctl stop k1000                       # the audio keeps playing; B4 does nothing now
sudo i2cdetect -y 1                             # the MCP23017 answers at 20
cd /opt/k1000 && python3 -m k1000.hw.mcp23017 --monitor     # turn and press: events print
sudo systemctl start k1000
```

A loose ribbon cable inside the case is the usual hardware cause.

**Knobs work but do the wrong thing**: the `controls:` map ([chapter 3](03-configuration.md#5-controls)).

**A knob skips clicks, or a value moves by itself.** The decoder fixes of
[`docs/08-knob-precision.md`](../08-knob-precision.md) are in (§6, §9, §10): one click is one
step, a short contact glitch on a knob at rest is dropped, fast turns keep their clicks, and master
up is never accelerated. Known limits: the very first click of a turn from stillness can be lost
when it is very fast, and a glitch slower than 2.5 ms per transition still counts as a click.
Between shows:

```sh
journalctl -u k1000 | grep -E 'master .* dB \((knob|phone|console|adoption)\)'   # every master change, with its source
journalctl -u k1000 | grep -i glitch                                              # clicks dropped as glitches
```

If the knob itself is in doubt, record a trace: `k1000-knobtrace.py capture --guided`
([chapter 8](08-development.md#the-knob-tools)). Its result also says which
`steps_per_detent` and `detent_ab` to set ([chapter 3 §5](03-configuration.md#5-controls)).


## 4. The bypass

The rig played without K1000 before, and Meyer designed it to: the same full-range feed to tops
and subs. **Bypass = the source straight into the speakers, as it was cabled before K1000.** About a
minute:

1. **Source down.** Hold B4 if K1000 still answers.
2. Unplug OUT1 TOP L, OUT2 TOP R and OUT3 SUBS (OUT4 too if it is patched) at the speakers.
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
| A soak run | `/opt/k1000/tools/k1000-soak.sh --summary /var/log/k1000/<its log>` ([chapter 7](07-maintenance.md#soak-test)) |

The journal is kept on the SD card (32 MB at most) and written to it every 30 s, so after a power
cut `journalctl -b -1` shows what happened before it, minus up to its last 30 s. After a power cut
a journal file may be set aside as corrupt (its name ends in `~`) and a new one started: normal,
and `journalctl` still reads both.

**Times after a power cut.** The Pi has no battery clock. After a cut it boots with the time last
saved to the SD card, minutes behind, and only an internet time server (over `eth0`) puts it
right. At a gig without internet the clock stays behind for the whole night, so journal and soak
times after a cut lag the wall clock: compare with `journalctl --list-boots` rather than your
watch ([chapter 7](07-maintenance.md#power-cuts)).

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
