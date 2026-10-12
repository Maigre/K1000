# K1000 box: deploy and run

Everything that runs on the K1000 (the Raspberry Pi 4 in the Zynthian V4 case). The design is in
`docs/03-v0-architecture.md`, the gig plan in `docs/02-plan-to-sunday.md`.

## What runs

| Unit | What | Depends on |
|---|---|---|
| `camilladsp.service` | the audio engine, CamillaDSP 5.0.0-beta.1, user `k1000`, real-time threads | nothing (audio never waits for the UI) |
| `k1000.service` | `python3 -m k1000` in `/opt/k1000`: knobs, screen, state, persistence | wants the engine, copes when it is down |
| `camillagui.service` | CamillaGUI v5 beta.2 on port 5005 (phone compact view, laptop editor) | nothing; lowest CPU priority, sandboxed |
| `k1000-console.service` | at boot, waits for the ZynScreen's framebuffer, then keeps the Linux text console off it; done ~3 s later | nothing; k1000 wants it but never waits for it |

The order at power-on:

1. The engine starts **muted at −40 dB** from `/var/lib/k1000/active.yml`.
2. k1000 connects to the engine, checks the config, unmutes and ramps Main up to the saved level over about 3 s.
3. The phone/laptop GUI comes up next to them.

- If `active.yml` is missing or broken, the engine waits for a valid one, with no websocket
  yet. k1000 writes it, and the engine starts on it within a second. The journal says
  `waiting for a valid /var/lib/k1000/active.yml` once, then every 30 s.
- The engine always runs from that file, never in CamillaDSP's wait mode (`-w`). So whenever
  processing stops, the process exits and systemd starts it again, muted, from `active.yml`.
- If the audio interface is missing (M4 unplugged or switched off), the engine is not started:
  its start script waits for the card in `/proc/asound` (the entry k1000 reads to show NO
  DEVICE), checks every second, and starts the engine within a second of the card appearing.
  The journal says `waiting for audio device(s) hw:M4` once, then every 30 s. There is no
  websocket meanwhile. After 30 s of waiting the engine is tried anyway (it exits and the wait
  starts again), so even a wrong check can never hold the audio back for more than 30 s.
- If `/var/log/k1000` cannot be written, the engine still plays and logs to the journal instead.
- All three units restart 1–2 s after any crash, with no retry limit.
- The hardware watchdog reboots the box if systemd itself hangs for 15 s.
- The journal is kept on the SD card (32 MB at most), so after a power cycle `journalctl -b -1`
  still shows what happened before it.

| Path | What |
|---|---|
| `/opt/k1000/` | a mirror of `box/` (package `k1000/`, `tests/`, `tools/`, units, this README); `REVISION` says what is deployed |
| `/opt/k1000.prev/` | the version before the last update (rollback) |
| `/etc/k1000/k1000.yaml` | Thomas's settings: profile pin, knob mapping. Copied once from `box/etc/`, **never overwritten** by install.sh |
| `/var/lib/k1000/state.json`, `active.yml` | live state and the generated engine config (written by k1000, atomically) |
| `/var/log/k1000/` | `camilladsp_rCURRENT.log` + up to 50 rotated ones (every engine start rotates, and every 256 kB), `soak.log` |
| `/opt/camillagui/` | the GUI binary; `config/` comes from `box/camillagui/`; `files/` is the GUI's empty, read-only config folder |
| `/etc/systemd/system/{camilladsp,k1000,camillagui,k1000-console}.service` | from `box/systemd/` |
| `/etc/systemd/system.conf.d/50-k1000-watchdog.conf` | `RuntimeWatchdogSec=15` |
| `/etc/systemd/journald.conf.d/50-k1000-journal.conf` | journal on the SD card, 32 MB cap (applies at the next reboot) |
| `/etc/modules-load.d/k1000.conf` | `i2c-dev` at boot: `/dev/i2c-1`, the knob board (install.sh `--boot`) |
| `/etc/rpi/swap.conf.d/50-k1000.conf` | swap in RAM only (zram), from `tools/k1000-lean.sh` |
| `/var/backups/k1000-lean/` | `tools/k1000-lean.sh`: every removal plan it ran (or refused), the boot files it backed up, and each run's log and exit status (`run-<time>.log`, `.rc`) |
| `/var/log/k1000-bootstrap/` | `bootstrap.sh`: each run's log and exit status |

## Deploy

From the repo on the laptop (`~/Bakery/K1000`), with the `k1000` host in your SSH config:

```sh
rsync -a --delete --rsync-path="sudo rsync" box/ k1000:/opt/k1000-src/
ssh k1000 "sudo K1000_REVISION=$(git describe --always --dirty) /opt/k1000-src/install.sh"
```

`install.sh` installs and enables the units but **starts nothing**. To deploy and restart in one
go, add `--start`. The audio drops for about a second and then ramps back in, so never do this
during a show.

```sh
ssh k1000 "sudo K1000_REVISION=$(git describe --always --dirty) /opt/k1000-src/install.sh --start"
```

Re-running is always safe. With nothing new, it ends with `0 changes`.

**Boot-level setup, once per card** (both apply at the next reboot; on a card made from scratch,
`bootstrap.sh` does this, see "Fresh card (bootstrap)"):

```sh
ssh k1000 "sudo /opt/k1000-src/install.sh --boot --headless --start && sudo reboot"
```

- `--boot`:
  - compiles `overlays/k1000-zynscreen.dts` with `dtc -@` into `/boot/firmware/overlays/`
  - makes sure `config.txt` has `dtoverlay=k1000-zynscreen,rotate=270`
  - adds `vt.global_cursor_default=0` to `cmdline.txt`, which stops the console cursor blinking
    on the ZynScreen. The original file is kept as `cmdline.txt.k1000-orig`.
  - turns I2C on for the knob board: `dtparam=i2c_arm=on` in `config.txt` (when no `[all]`/`[pi4]`
    line has it) and `i2c-dev` loaded at boot (`/etc/modules-load.d/k1000.conf`) and now.
    Raspberry Pi OS Lite has neither: without them there is no `/dev/i2c-1` and the knobs are dead.
- `--headless`:
  - boots to the console instead of the desktop: `multi-user.target`, so no labwc session
  - masks PipeWire/WirePlumber for all users, so nothing but CamillaDSP ever opens the audio
    interfaces
  - disables what the desktop image runs for nothing: Bluetooth, printing (cups) and the NFS
    client with rpcbind (port 111 open on every interface)
  - removes the tty1 auto-login (an invisible, sudo-capable shell for any USB keyboard): a
    local login then needs k1000's password
  - leaves cloud-init on: `tools/k1000-lean.sh` turns it off, and only once
    `k1000-console.service` (every install.sh run installs it) has run at a boot and found the
    panel. Without that guard, the faster boot starts k1000 before the ZynScreen registers
    (~7 s), k1000's console unbind finds nothing, and the console then draws over the UI
  - undo with `sudo systemctl set-default graphical.target`,
    `sudo systemctl --global unmask pipewire.socket pipewire.service pipewire-pulse.socket pipewire-pulse.service wireplumber.service filter-chain.service`,
    `sudo systemctl enable bluetooth cups cups.socket cups.path rpcbind rpcbind.socket nfs-blkmap`
    and `sudo mv /etc/systemd/system/getty@tty1.service.d/autologin.conf.k1000-off /etc/systemd/system/getty@tty1.service.d/autologin.conf`

## Lean OS (`tools/k1000-lean.sh`)

D-006: the box runs Raspberry Pi OS Lite plus K1000, with nothing at boot or during a show that
K1000 does not need. `tools/k1000-lean.sh` gets there from the desktop image the first card was
flashed with, and from a fresh Lite card (where the desktop purge finds next to nothing to do).
Every step is idempotent: a converged box answers `0 changes`.

| Step | What | Why |
|---|---|---|
| `keep` | `apt-mark manual` every package K1000 needs (`KEEP` in the script) | no autoremove can take one |
| `desktop` | purge the ten `rpd-*` metapackages and the installed leftovers (`pinentry-gnome3`, `rpi-connect(-lite)`, chromium, firefox, vlc, cups, pipewire, plymouth...), then autoremove | ~970 packages: the card becomes Lite + K1000 |
| `services` | cloud-init off (`/etc/cloud/cloud-init.disabled`), once `k1000-console.service` has run at this boot and found the panel; `NetworkManager-wait-online`, `rpi-eeprom-update`, `e2scrub_reap` disabled | cloud-init sat on k1000's boot path (~2.6 s); no bootloader update staged at a gig |
| `timers` | `apt-daily`, `apt-daily-upgrade`, `man-db`, `e2scrub_all` disabled | no apt or man-db work in the middle of a show (and no internet at gigs) |
| `swap` | `/etc/rpi/swap.conf.d/50-k1000.conf`: `Mechanism=zram` | no 2 GB swap file re-made on the SD card at every boot (1.5 s), no writeback to the SD card hours into a show |
| `boot` | `cmdline.txt`: `quiet`, `logo.nologo`, no `splash`/`plymouth` arguments; `config.txt`: `disable_splash=1` | a quiet panel and console at boot |

- **The guard.** Every removal is simulated first (`apt-get -s`) and its plan saved in
  `/var/backups/k1000-lean/`. A plan that names a package K1000, the boot, SSH, the network, the
  Wi-Fi/AP, systemd, libc, Python or ALSA needs is refused: exit 1, nothing removed.
- Before a removal or a boot edit, `cmdline.txt` and `config.txt` are copied next to themselves
  (`*.k1000-lean-<step>-<time>`) and, with the initramfs images, into `/var/backups/k1000-lean/`.
- **It survives a lost SSH session.** A real run re-runs itself as a transient systemd unit
  (`k1000-lean-<time>`) and the terminal only follows its log. Purging the desktop takes minutes
  (apt, man-db, and plymouth's removal rebuilds the initramfs, which raspi-firmware then copies
  onto `/boot/firmware` in place). Killed halfway, that leaves a card that may not boot. If the
  session drops, the run goes on: read `/var/backups/k1000-lean/run-<time>.log`, and
  `run-<time>.rc` holds its exit status once it is done.
- **The power-cycle check** (`--check`, read-only; also at the end of every real run that changed
  something and right after a removal): `dpkg --audit` is empty; the boot packages are
  installed; `initramfs8` (and `initramfs_2712`) are byte-identical to the newest
  `/boot/initrd.img-*-rpi-v8` (`-2712`) and list an `/init`; `cmdline.txt` is one line with the
  same `root=` this boot used and `rootwait`; `kernel8.img` and `config.txt` are there; `i2c-dev`
  is loaded at boot. Any failure prints what to fix and says **do NOT power-cycle**. After an
  interrupted run, run `--check` before anything else.
- **One change set per power cycle.** After each, check the box: SSH answers, the engine is
  Running, Main is back at its saved level, the panel shows K1000.

```sh
ssh k1000 "sudo /opt/k1000-src/tools/k1000-lean.sh --dry-run"         # what would change (the desktop plan takes ~1 min)
ssh k1000 "sudo /opt/k1000-src/tools/k1000-lean.sh --only desktop"    # runs as a unit; ends with the check; then power-cycle
ssh k1000 "/opt/k1000-src/tools/k1000-lean.sh --check"                # before ANY power cycle, above all after a lost session
ssh k1000 "sudo /opt/k1000-src/tools/k1000-lean.sh --only services"   # then power-cycle and check; then timers, swap, boot
```

If the session dropped: `ssh k1000 'ls -t /var/backups/k1000-lean/run-*; systemctl list-units "k1000-lean-*"'`
shows whether it is still running; `tail` its log.

Measured on the first box (2026-10-12, desktop image converged this way, one power cycle per
step; seconds after power-on, from the boot probe):

| | desktop image | Lean (all steps) |
|---|---|---|
| systemd userspace | 16.0 s | 7.0 s |
| engine Running | 10.9 s | 7.9 s |
| Main back at its saved level | 15.5 s | 12.4 s |
| packages / `/usr` | 1650 / 4.3 GB | 678 / 1.6 GB |

The panel now registers at ~2.0 s (7.0 s before: the smaller initramfs), so k1000 always starts
after it; `k1000-console.service` still logs `console unbound 1 time(s)` at every boot.

Left as they are, on purpose:

- avahi-daemon: `k1000.local` on a network whose addresses nobody knows; it is not on the
  engine's boot path.
- Bluetooth is disabled (install.sh `--headless`), but there is no `dtoverlay=disable-bt`: that
  overlay moves the PL011 UART, and the `serial0` console with it, onto GPIO 14/15, which the
  Zynthian V4 board wires to its MIDI DIN ports.
- console-setup, keyboard-setup, udisks2, fstrim, logrotate: Lite defaults, off the engine's boot
  path.

Undo: every line the script prints says how. The saved plans list what to `apt-get install` again.

## Fresh card (bootstrap)

A spare or replacement gig card from nothing (D-006, step 3):

1. Raspberry Pi Imager: **Raspberry Pi OS Lite (64-bit)**. Settings: hostname `k1000`, user
   `k1000`, SSH on with **"Allow public-key authentication only"** and your SSH public key, Wi-Fi
   country FR. Boot the box on Ethernet, on a network with internet. Log in once with your key
   (`ssh k1000 true`): bootstrap switches SSH to keys only only after it has seen that.
2. From the repo on the laptop:

   ```sh
   rsync -a --delete --rsync-path="sudo rsync" box/ k1000:/opt/k1000-src/
   ssh k1000 "sudo K1000_REVISION=$(git describe --always --dirty) /opt/k1000-src/bootstrap.sh"
   ```

   It runs as a systemd unit (`k1000-bootstrap-<time>`, log in `/var/log/k1000-bootstrap/`), so a
   lost session does not stop it. It runs `apt-get update` only when a package is missing,
   installs CamillaDSP 5.0.0-beta.1 (tarball and binary checked against pinned sha256 sums), runs
   `install.sh --boot --headless` (CamillaGUI is pinned there; `--ap` is added once install.sh
   offers it), switches SSH to keys only (`/etc/ssh/sshd_config.d/10-k1000-keyonly.conf`, the
   first box's file, checked with `sshd -t`, applied at the next boot), then
   `tools/k1000-lean.sh`. It never reboots by itself: it ends with
   `N changes. Now power-cycle the box`.
3. Power-cycle, wait ~20 s, then check:

   ```sh
   ssh k1000 'systemctl is-active camilladsp k1000 camillagui; cat /opt/k1000/REVISION'
   ```

4. Run the bootstrap command once more, then power-cycle again. The first run leaves cloud-init
   on, because the panel guard had not run at a boot yet; this run turns it off.
5. Settings: `/etc/k1000/k1000.yaml` starts from `box/etc/k1000.yaml` (profile `dev`, default knob
   map). Copy the main card's file over (profile pin, the confirmed `controls:`), then
   `sudo systemctl restart k1000`.

- A re-run changes nothing: `0 changes: this card is already a K1000`.
- `--dry-run` says what it would do and changes nothing.
- Offline: `CAMILLADSP_TARBALL=<camilladsp-linux-aarch64.tar.gz>` and
  `CAMILLAGUI_TARBALL=<camillagui_linux_aarch64.tar.gz>` (put them after `sudo`, like
  `K1000_REVISION`).

## Everyday commands (on the box)

```sh
systemctl status camilladsp k1000 camillagui      # all three at a glance
journalctl -u k1000 -f                            # control service log (live)
journalctl -u camilladsp -n 50                    # engine start/stop, "waiting for ...", ALSA errors
tail -f /var/log/k1000/camilladsp_rCURRENT.log    # engine log (xruns, device errors, config loads)
journalctl --list-boots                           # did the box reboot? when?
journalctl -b -1 -u k1000 -u camilladsp           # the boot before this one (after a power cycle)
sudo systemctl restart k1000                      # restart the control surface (audio keeps playing)
sudo systemctl restart camilladsp                 # restart the engine: ~1 s of silence, comes back muted, k1000 ramps in
```

After a dropout, keep the evidence before anything else (50 engine logs are kept, but each
engine start rotates one). After a power cycle, also save `journalctl -b -1 --no-pager`:

```sh
mkdir -p ~/incident && cp /var/log/k1000/camilladsp_r*.log ~/incident/ && journalctl -b --no-pager > ~/incident/journal.txt
```

Check that the engine has real-time priority. The log must **not** say `could not get real time priority`:

```sh
pid=$(systemctl show -p MainPID --value camilladsp); for t in /proc/$pid/task/*; do echo "$(cat $t/comm): $(chrt -p ${t##*/} | head -1)"; done
# processing and AlsaPlayback/AlsaCapture show SCHED_FIFO
```

## Switching profile: `dev` (HiFiBerry) / `m4` (MOTU M4)

The profile is pinned in `/etc/k1000/k1000.yaml`:

```sh
sudo nano /etc/k1000/k1000.yaml     # profile: m4      (or: dev)
sudo systemctl restart k1000        # k1000 regenerates active.yml for that device and reloads the engine
```

- A device change is a full engine stop/start, about a second of silence. That is fine on the
  bench, never during a show.
- **Gig rule: `profile: m4`**. If the M4 is missing, the screen says so; nothing falls back to
  the HiFiBerry.
- When the `profile:` line is absent, k1000 uses the last profile chosen on the SETUP page.

## MOTU M4 bring-up (Tuesday)

Speakers OFF, or their XLRs unplugged, for all of it.

```sh
/opt/k1000/tools/k1000-m4-bringup.sh        # checks only: card, USB id, formats, channels, k1000.yaml, power, panel
sudo /opt/k1000/tools/k1000-m4-bringup.sh --tone --i-have-turned-the-speakers-off    # out N plays N beeps, -40 dBFS
```

- The checks open nothing that plays. Exit 1 means a FAIL line to fix first. Section 7 lists the
  M4 front-panel positions to set and tape; section 8 the next steps (pin `profile: m4`, presence
  drills, a 1-hour soak).
- The tone plays output by output (Enter for the next) at a fixed −40 dBFS, about −24 dBu at the
  line outs. Watch the M4's output meters and label each cable. It refuses without the speakers
  flag. If the engine holds the M4, it asks you to type `STOP`, stops k1000 and camilladsp, and
  starts them again at the end, also after Ctrl-C or an error.
- Two front-panel facts are UNVERIFIED until these are done (peak reader below):
  - **Loop test** (MONITOR fully clockwise = unity): a TRS cable from out N into front INPUT 1
    (GAIN 1 fully counter-clockwise, 48V off, MON 1 unlit), a tone in LINE IN 3, K1000 Main at
    −20 dB. Capture index 0 should read within about 0.5 dB of playback index N−1, and out 1 the
    same as out 3. Input 1 is not in the program, so nothing feeds back.
  - **Leak test** (MON MIX fully clockwise blocks direct monitoring): K1000 muted (hold B2), the
    tone in LINE IN 3, the loop on out 1, then press 3-4. Capture index 0 must stay at the floor.
    Turning MON MIX counter-clockwise makes it rise: that is the raw program going past K1000
    and its mute. Put MON MIX back fully clockwise and 3-4 off.

```sh
python3 -c 'import json,websockets.sync.client as c
w=c.connect("ws://127.0.0.1:1234")
for n in ("GetCaptureSignalPeakSince","GetPlaybackSignalPeakSince"):
  w.send(json.dumps({"command":n,"value":2.0})); print(n[3:10],[round(x,1) for x in json.loads(w.recv())["value"]])'
```

## Phone and laptop: CamillaGUI

- **The phone needs a network that the box is on.** Today the box has only its wired LAN (Wi-Fi
  off), and the screen does not show the box's IP. How the phone reaches it at a gig (the box's
  own access point, D-007, or a travel router on Ethernet) is a decision to prove at Friday's rig
  test. Until then, the knobs carry the show.
- Phone (compact view): `http://<box-ip>:5005/gui/index.html?compactview`
  - **Levels only**: Main volume, mute and the meters. These are runtime commands, so they never
    send a config. k1000 follows the Main fader.
  - Live EQ, Lo-Cut, sub delay and the sub/top balance are on the knobs (balance = E2 on the
    MAIN page). The phone has no sliders for them on purpose: in CamillaGUI v5 beta.2 every
    slider move sends the whole config as the page loaded it, which would put back old knob
    values (a feedback notch), the pre-tune system EQ, or even the old audio device.
- **Phone rule.** The compact view has two small icons at the top, "Change to normal view" and
  "Change to dashboard view". One tap opens the full editor, with no confirmation on anything.
  - Stay in the compact view. Never tap those icons on the phone.
  - If the normal view opened anyway, reload `...?compactview` before touching anything.
  - Never press **Apply to DSP** or **Stop processing** from the phone, and never tick
    **Apply automatically**:
    - Apply to DSP sends the config the page loaded. Every knob move since then (feedback
      notches, Lo-Cut, sub delay) is undone, and k1000 adopts and saves the undo.
    - Stop processing stops the engine. systemd starts it again about a second later, muted,
      and k1000 ramps Main back in over 3 s: about 2 s of silence, then the ramp.
    - Apply automatically turns every later edit into a whole-config Apply.
- The GUI's Aux1/Aux2 faders (normal and dashboard views) are k1000's sub and top groups, not
  a balance control: use E2. k1000 pulls a raise on them back down to its balance, but keeps a
  cut or a mute (the box screen then shows e.g. SUBS GROUP MUTED) until it is undone in the GUI.
- The Main slider goes up to 0 dB (`volume_max` in `box/camillagui/gui-config.yml`), and one
  swipe can get there in about half a second. **After Friday's gain-structure step**, set
  `volume_max` to the show level + 6 dB and `volume_range` to 80 + `volume_max` (the bottom stays
  at −80 dB). Edit it in the repo and deploy: install.sh overwrites the box's copy. It applies at
  the next page load, with no restart. If the knobs then put Main above that ceiling, the phone's
  +1 dB button pulls Main down toward it.
- Laptop (full editor, for the system EQ at the tune): `http://<box-ip>:5005/`
  - **Press "Fetch from DSP" (or reload the page) right before editing.** Apply sends the whole
    config the page holds, so a page opened before knob moves (or before a profile change) puts
    the old values, or the old device, back.
  - Edit, then **"Apply to DSP"**. k1000 adopts the change and saves it in `state.json`.
  - "Save to file" (and the save half of "Apply and save") fails on purpose
    (`Read-only file system`): the GUI cannot write any file. k1000 owns `active.yml` and the state.
  - The GUI shows no active config file name: expected (the engine runs from `active.yml`,
    outside the GUI's folder).
- After an engine restart, the GUI's first action can fail with 503. The next one works.
- The GUI has no password and listens on every interface: anyone on the same network can change
  or stop the engine.

## Knobs and buttons: confirm them before the rig test (OPEN)

Nobody has confirmed yet which physical button is B1–B4 or which knob is E1–E4.
`/etc/k1000/k1000.yaml` still holds the defaults, and the on-screen knob corners are assumed.
Hold-B2 (mute) and hold-B4 (reset live EQ) are the same gesture, so a wrong map would turn the
emergency mute into a live-EQ reset. `k1000-m4-bringup.sh` warns while the defaults are in place.

1. SETUP → "Knob test" (HWTEST). Press every button and turn every knob. Note, by physical
   position, the `sw`/`enc` index each one shows.
2. Write `controls:` in `/etc/k1000/k1000.yaml` (encoders, pushes, buttons, reverse), then
   `sudo systemctl restart k1000`. If the on-screen labels sit at the wrong corners, that is
   `KNOB_CORNERS` in `k1000/ui/input_map.py`: a code change, deployed like any other.
3. Label the buttons on the shell: PAGE · MUTE (hold) · LO-CUT · RESET (hold).
4. At the rig test, with program playing: hold B2, and all four Meyers mute. Check by feel that
   B4 is not right next to it.

In a terminal, the monitor needs the MCP23017 and GPIO lines, which the service holds, so stop
the service first:

```sh
sudo systemctl stop k1000
cd /opt/k1000 && python3 -m k1000.hw.mcp23017 --monitor        # --raw for every pin change, --selftest for the interrupt lines
sudo systemctl start k1000
```

## Soak test (overnight, and the 2-hour program run)

```sh
sudo systemd-run --unit=k1000-soak --uid=k1000 /opt/k1000/tools/k1000-soak.sh
tail -f /var/log/k1000/soak.log
/opt/k1000/tools/k1000-soak.sh --summary      # in the morning: "clean", or what needs a look (exit 1)
sudo systemctl stop k1000-soak                # "not loaded" = it had already ended: a reboot?
```

The tool writes one line every 10 s:

```
2026-10-15T23:00:10+02:00 state=Running load=1.23 buffer=224 clipped=0 xruns=0 xruns_all=0 rate=48000 restarts=0 temp_c=52.1 throttled=0x0 main_db=-20.0 muted=false k1000=active k1000_restarts=0 boot=3f2a9c1e up=8123
```

`--summary` flags:

- the soak stopped: the last sample is long before now, or there is a gap. A reboot ends it (a
  `systemd-run` unit does not come back), and `journalctl --list-boots` then says when;
- a reboot: a new `boot=` id, or `up` going down;
- `xruns`/`xruns_all` above 0: dropout lines in the engine log (playback underruns, capture
  overruns, stalled device, device errors). A stall counts twice (entering and leaving);
- restarts of camilladsp (`restarts`: a crash, or a lost device) or of k1000 (`k1000_restarts`);
- `state` other than `Running` (`Paused` = silent input, `DOWN` = no engine websocket), k1000
  not active, Main muted, clipped samples;
- `throttled` not `0x0` (under-voltage or heat: the M4 is bus-powered from the Pi), or the SoC
  at 75 °C or more.

## Roll back

- **Code or units:** reinstall the previous version. `/opt/k1000.prev` is rewritten by each
  update, so copy it out first.

  ```sh
  sudo rm -rf /tmp/k1000-rollback && sudo cp -a /opt/k1000.prev /tmp/k1000-rollback && sudo /tmp/k1000-rollback/install.sh --start
  cat /opt/k1000/REVISION        # e.g. "e3a3deb (reinstalled from /tmp/k1000-rollback)"
  ```

  Or from the laptop: `git checkout <good-commit>`, then deploy as above with `--start`.
- **Settings:** `/etc/k1000/k1000.yaml` is never touched by install.sh. Fix it by hand, then
  `sudo systemctl restart k1000`.
- **Sound state:** `/var/lib/k1000/state.json` holds the levels and EQ.
  - Live EQ: on the LIVE page, hold B4 twice within 5 s.
  - A torn `state.json` is rebuilt from `active.yml` (the bad copy is kept as `state.json.bad`) and
    comes back MUTED ("STATE REBUILT · CHECK MASTER").
  - If the state file itself is bad, stop k1000, move `state.json` away and start k1000. Next to a
    K1000 `active.yml` it comes up on defaults and stays MUTED ("NO SAVED STATE · CHECK MASTER").
    Check the master, then hold B2. An engine that is already playing is adopted unchanged.
- **The show itself:** the bypass kit. Re-patch the source straight into the Meyers
  (`docs/02-plan-to-sunday.md` §5).

## Tests

```sh
cd /opt/k1000 && python3 -m pytest -q -p no:cacheprovider tests tools/tests   # unit tests (no hardware)
cd /opt/k1000 && python3 -m pytest -q -p no:cacheprovider -m hw              # hardware tests (opt-in; stop k1000 first)
```

`tools/tests` covers the engine unit's start script, the panel console guard, install.sh's helpers
(headless leftovers, I2C at boot, `--deps`), the soak tool, the M4 bring-up tool, `k1000-lean.sh`
(the guard, the dry run, the boot-file edits, the power-cycle check, the cloud-init gate, the
detached run) and `bootstrap.sh` (apt only when needed, SSH keys only, the detached run), all
against fakes (no service, card, package or speaker is touched).
