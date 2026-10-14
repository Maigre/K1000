# K1000 box: deploy and run

Everything that runs on the K1000 (the Raspberry Pi 4 in the Zynthian V4 case). The design is in
`docs/03-v0-architecture.md`, the gig plan in `docs/02-plan-to-sunday.md`, and the panel (UI v1,
D-009: pages SHOW · ALIGN · SETUP, the buttons, the screen states) in `docs/04-operator-card.md`.

## What runs

| Unit | What | Depends on |
|---|---|---|
| `camilladsp.service` | the audio engine, CamillaDSP 5.0.0-beta.1, user `k1000`, real-time threads | nothing (audio never waits for the UI) |
| `k1000.service` | `python3 -m k1000` in `/opt/k1000`: knobs, screen, state, persistence, and the phone page (https://10.42.0.1/, `k1000/web/`; ports 80/443/8080 on the AP address only) | wants the engine, copes when it is down |
| `camillagui.service` | CamillaGUI v5 beta.2 on port 5005 (phone compact view, laptop editor) | nothing; lowest CPU priority, sandboxed |
| `k1000-console.service` | at boot, waits for the ZynScreen's framebuffer, then keeps the Linux text console off it; done ~3 s later | nothing; k1000 wants it but never waits for it |
| `k1000-ap-fence.service` | the phone AP's firewall (its own nftables table): from the K1000 Wi-Fi only DHCP, DNS, 80, 443, 8080 reach the box (16 connections per phone, 64 in all); 10.42.0.1 unreachable from `eth0` | nothing; loaded before any network |
| `k1000-ap-hostapd.service` | the phone's Wi-Fi access point "K1000" on `wlan0` (hostapd, WPA2, PMF off, 5 GHz ch 36) | requires the fence (refuses to start without its table); bound to `wlan0`; refuses while another interface is in 10.42.0.0/24; NetworkManager leaves `wlan0` alone |
| `k1000-ap-dnsmasq.service` | DHCP + DNS for the phones on the AP (10.42.0.0/24; only `k1000.home.arpa` resolves) | bound to `k1000-ap-hostapd` |

The three `k1000-ap-*` units come from `net/` and are installed, updated and enabled by
`net/setup-ap.sh` alone (`install.sh --ap`); a plain install.sh run only reports a changed one. The
phone (D-007) is `docs/06-phone.md`.

The order at power-on:

1. The engine starts **muted at −40 dB** from `/var/lib/k1000/active.yml`.
2. k1000 connects to the engine, checks the config and, unless the state was saved muted, unmutes and ramps Main up to the saved level over about 3 s (saved muted: it stays muted, `READY · hold B4 to unmute`).
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
| `/var/log/k1000/` | `camilladsp_rCURRENT.log` + up to 50 rotated ones (every engine start rotates, and every 256 kB), the soak logs (`soak*.log`, one per run) |
| `/opt/camillagui/` | the GUI binary; `config/` comes from `box/camillagui/`; `files/` is the GUI's empty, read-only config folder |
| `/etc/systemd/system/{camilladsp,k1000,camillagui,k1000-console}.service` | from `box/systemd/` |
| `/etc/systemd/system/k1000-laptop.{path,service}` | from `box/systemd/`, installed, never enabled: SETUP › PHONE › Laptop access (k1000.service `Wants=` the watcher; the root unit runs `/opt/k1000/net/k1000-laptop-access.sh`) |
| `/run/k1000/laptop-open`, `/run/k1000-laptop/status` | SETUP › PHONE › Laptop access: k1000's request (its runtime directory, gone when k1000 stops) and root's answer (open until, or closed and why) |
| `/var/lib/k1000/tune-undo.json` | the `/tune` page's undo: the system EQ the last Apply replaced |
| `/etc/systemd/system/k1000-ap-{fence,hostapd,dnsmasq}.service`, `k1000-ap-hostapd.service.d/k1000-iface.conf` | from `box/net/`, by `net/setup-ap.sh` only (`install.sh --ap`); the drop-in binds hostapd to its interface |
| `/etc/NetworkManager/dispatcher.d/90-k1000-ap-guard` | `net/k1000-ap-guard.sh`, by setup-ap.sh: stops the AP while another interface is in 10.42.0.0/24 (a laptop sharing on `eth0`), starts it again after; `journalctl -t k1000-ap-guard` |
| `/etc/k1000/tls/` | the phone page's certificate: `k1000.crt`, `k1000.key`, `k1000-ca.crt` (public), root:k1000 0640 (install.sh with `K1000_TLS_DIR`; the CA's private key never comes here) |
| `/etc/k1000/ap.psk` | the K1000 Wi-Fi password (generated once by setup-ap.sh, root:k1000 0640; shown only on the box screen) |
| `/etc/k1000/ap.info`, `hostapd.conf`, `ap.env`, `ap-dnsmasq.conf`, `ap-fence.nft` | setup-ap.sh's choices (no secret; `state=down` after a no-go) and the files it generates (`hostapd.conf` is 0600: it holds the password) |
| `/etc/NetworkManager/dnsmasq-shared.d/k1000.conf`, `/etc/NetworkManager/conf.d/k1000-ap-unmanaged.conf` | the AP's DNS (portal off by default), and `wlan0` left to hostapd |
| `/var/lib/k1000/phone.json` | SETUP › PHONE › Phone on/off, kept across restarts |
| `/etc/systemd/system.conf.d/50-k1000-watchdog.conf` | `RuntimeWatchdogSec=15` |
| `/etc/systemd/journald.conf.d/50-k1000-journal.conf` | journal on the SD card, 32 MB cap (applies at the next reboot) |
| `/etc/modules-load.d/k1000.conf` | `i2c-dev` at boot: `/dev/i2c-1`, the knob board (install.sh `--boot`) |
| `/etc/rpi/swap.conf.d/50-k1000.conf` | swap in RAM only (zram), from `tools/k1000-lean.sh` |
| `/var/backups/k1000-lean/` | `tools/k1000-lean.sh`: every removal plan it ran (or refused), the boot files it backed up, and each run's log and exit status (`run-<time>.log`, `.rc`) |
| `/var/log/k1000-bootstrap/` | `bootstrap.sh`: each run's log and exit status |

## Deploy

From the repo checkout on the laptop, with the `k1000` host in your SSH config:

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

**The phone (D-007), once per card**: the certificate and the access point. The full story, the
one-time phone setup and the Friday go/no-go are in `docs/06-phone.md`.

```sh
# CA=the local CA's folder on the laptop (gitignored secrets, never in this repo)
ssh k1000 'install -d -m 0700 /tmp/k1000-tls'          # the leaf + the CA's PUBLIC certificate, never ca.key
scp "$CA"/{k1000.crt,k1000.key,k1000-ca.crt} k1000:/tmp/k1000-tls/
ssh k1000 "sudo K1000_TLS_DIR=/tmp/k1000-tls K1000_REVISION=$(git describe --always --dirty) /opt/k1000-src/install.sh --ap; rm -rf /tmp/k1000-tls"
```

(Double quotes: `git describe` runs on the laptop, as in the Deploy lines above; `;` so the key
copy is removed whatever install.sh says. install.sh checks the three files and the key against
the certificate before it changes anything.)

- `--ap` runs `net/setup-ap.sh`: the Wi-Fi "K1000" on `wlan0` (hostapd, PMF off, 5 GHz ch 36,
  country FR, captive portal off, SSH fenced off), its DNS and its fence. `eth0` is never touched.
  It keeps its earlier choices on a re-run (`/etc/k1000/ap.info`); change one with, e.g.,
  `sudo /opt/k1000/net/setup-ap.sh --band bg` (the others: `--portal on`, `--ssh on`,
  `--backend nm`, `--iface wlan1`).
- k1000 loads a new certificate at its next start (between shows: `sudo systemctl restart k1000`).
- **No-go** (the Friday checklist fails): `sudo /opt/k1000/net/setup-ap.sh --down` (off the air,
  across reboots, and kept down by later `install.sh --ap` and `bootstrap.sh` runs) and
  `phone: enabled: false` in `/etc/k1000/k1000.yaml`. Back on: `sudo /opt/k1000/net/setup-ap.sh --up`.
- **A laptop on a direct `eth0` cable must not share its connection on 10.42.0.x** (NetworkManager's
  "Shared to other computers" default is 10.42.0.1/24, the AP's subnet): the AP is then kept down
  (setup-ap.sh exits 4, the hostapd unit refuses, the NetworkManager guard stops it), so that SSH
  over `eth0` keeps working. Share on another subnet (e.g. `ipv4.addresses 10.43.0.1/24` on the
  laptop's wired connection) or use the house network; the AP comes back by itself once the
  address is gone.
- install.sh exits 4 when only the access point failed (3: only CamillaGUI; 5: both); everything
  else is installed either way.

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
   country FR, **Wi-Fi SSID and password left empty** (`wlan0` becomes the phone's AP: setup-ap.sh
   refuses to take an interface that is a Wi-Fi client). Boot the box on Ethernet, on a network
   with internet. Log in once with your key (`ssh k1000 true`): bootstrap switches SSH to keys
   only only after it has seen that.
2. From the repo on the laptop:

   ```sh
   rsync -a --delete --rsync-path="sudo rsync" box/ k1000:/opt/k1000-src/
   ssh k1000 "sudo K1000_REVISION=$(git describe --always --dirty) /opt/k1000-src/bootstrap.sh"
   ```

   It runs as a systemd unit (`k1000-bootstrap-<time>`, log in `/var/log/k1000-bootstrap/`), so a
   lost session does not stop it. It runs `apt-get update` only when a package is missing,
   installs CamillaDSP 5.0.0-beta.1 (tarball and binary checked against pinned sha256 sums), runs
   `install.sh --boot --headless --ap` (CamillaGUI is pinned there; `--ap` brings up the phone's
   access point, the certificate is the separate step under "Deploy"), switches SSH to keys only (`/etc/ssh/sshd_config.d/10-k1000-keyonly.conf`, the
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
- `--no-ap`: everything but the phone's access point, which is marked down (a spare card after a
  phone no-go); later runs, with or without `--no-ap`, keep it down (`setup-ap.sh --up` lifts it).
- Offline: `CAMILLADSP_TARBALL=<camilladsp-linux-aarch64.tar.gz>` and
  `CAMILLAGUI_TARBALL=<camillagui_linux_aarch64.tar.gz>` (put them after `sudo`, like
  `K1000_REVISION`).

## Everyday commands (on the box)

```sh
systemctl status camilladsp k1000 camillagui      # all three at a glance
sudo /opt/k1000/net/setup-ap.sh --status          # the phone's Wi-Fi: AP, fence, DNS, leases, phones seen
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
  - **Leak test** (MON MIX fully clockwise blocks direct monitoring): K1000 muted (hold B4), the
    tone in LINE IN 3, the loop on out 1, then press 3-4. Capture index 0 must stay at the floor.
    Turning MON MIX counter-clockwise makes it rise: that is the raw program going past K1000
    and its mute. Put MON MIX back fully clockwise and 3-4 off.

```sh
python3 -c 'import json,websockets.sync.client as c
w=c.connect("ws://127.0.0.1:1234")
for n in ("GetCaptureSignalPeakSince","GetPlaybackSignalPeakSince"):
  w.send(json.dumps({"command":n,"value":2.0})); print(n[3:10],[round(x,1) for x in json.loads(w.recv())["value"]])'
```

## Phone: K1000's own page (D-007)

The phone joins the box's own Wi-Fi "K1000" (QR on SETUP › PHONE › Phone) and opens
`https://10.42.0.1/`: K1000's page, served by the k1000 service, driving the same controller as
the knobs. Everything about it (the AP, the fence, the certificate, the one-time phone setup, the
mute rules, troubleshooting, the Friday go/no-go) is in **`docs/06-phone.md`**. It is gated by
the Friday rig test; on a no-go the AP goes fully off and the knobs carry the show. CamillaGUI is
**not** reachable from the K1000 Wi-Fi (the fence).

## Laptop (and a phone on the wired LAN): CamillaGUI

- CamillaGUI is reached over the wired LAN (`eth0`; the screen does not show the box's IP), or
  over the K1000 Wi-Fi while **SETUP › PHONE › Laptop access** is open (next section).
- Phone (compact view): `http://<box-ip>:5005/gui/index.html?compactview`
  - **Levels only**: Main volume, mute and the meters. These are runtime commands, so they never
    send a config. k1000 follows the Main fader.
  - Live EQ, Lo-Cut, the sub/top delay and the sub/top balance are on the knobs (delay = E2,
    balance = E3 on the ALIGN page, D-015). The phone has no sliders for them on purpose: in CamillaGUI v5 beta.2 every
    slider move sends the whole config as the page loaded it, which would put back old knob
    values (a feedback notch), the pre-tune system EQ, or even the old audio device.
- **Phone rule.** The compact view has two small icons at the top, "Change to normal view" and
  "Change to dashboard view". One tap opens the full editor, with no confirmation on anything.
  - Stay in the compact view. Never tap those icons on the phone.
  - If the normal view opened anyway, reload `...?compactview` before touching anything.
  - Never press **Apply to DSP** or **Stop processing** from the phone, and never tick
    **Apply automatically**:
    - Apply to DSP sends the config the page loaded. Every knob move since then (feedback
      notches, Lo-Cut, delay) is undone, and k1000 adopts and saves the undo.
    - Stop processing stops the engine. systemd starts it again about a second later, muted,
      and k1000 ramps Main back in over 3 s: about 2 s of silence, then the ramp.
    - Apply automatically turns every later edit into a whole-config Apply.
- The GUI's Aux1/Aux2 faders (normal and dashboard views) are k1000's sub and top groups, not
  a balance control: use ALIGN E3. k1000 pulls a raise on them back down to its balance, but keeps a
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
- The GUI has no password and listens on every interface: anyone on the same wired network can
  change or stop the engine (the K1000 Wi-Fi cannot reach it: the fence).

## Laptop at the rig: SETUP › Laptop access and the tune page

At the rig the laptop has only the K1000 Wi-Fi, whose fence keeps CamillaGUI and SSH off it.
**SETUP › PHONE › Laptop access, push twice** opens both, from the K1000 Wi-Fi's subnet only, for 2 hours
at most (`docs/06-phone.md` §11):

- The row reads `OPEN 1:59`, SHOW shows the pill `LAPTOP OPEN 1:59`, and SETUP › PHONE › Phone prints the
  Wi-Fi password as text for the laptop.
- On the laptop (joined to K1000): CamillaGUI `http://10.42.0.1:5005/gui/index.html` (the rules
  above apply: *Fetch from DSP* first), `ssh k1000@10.42.0.1`, and the **tune page**
  `https://10.42.0.1/tune`: paste a REW / Open Sound Meter / EqualizerAPO filter list, preview it
  on the 8 system PEQ bands (peaking only, 8 at most, clamped and said so), **Apply** (through
  k1000 like a knob: click-safe, saved), **Undo** (the system EQ before).
- **One push closes it.** It closes by itself after 2 h, when k1000 stops or restarts, and at
  every boot: nothing about it is persistent. Open connections stay until they end; new ones are
  refused.
- Who does what: k1000 (user k1000, no capability but port binding, so no sudo and no nft) only
  creates `/run/k1000/laptop-open`; `k1000-laptop.path` starts the root `k1000-laptop.service`,
  which inserts one tagged rule in the fence (`tcp 22, 5005` from 10.42.0.0/24 to 10.42.0.1 on the
  AP interface) and removes it on every way out.

```sh
sudo /opt/k1000/net/k1000-laptop-access.sh status   # open or closed, the rule, root's status
journalctl -t k1000-laptop -n 20                     # opened / closed (why) / refused
systemctl status k1000-laptop.path                   # the watcher (k1000 brings it)
```

After the deploy that brings it, k1000 must restart once (between shows) before SETUP › Laptop
access answers: install.sh says so.

## Knobs and buttons: confirm them before the rig test (buttons OPEN)

The knobs are confirmed: on 2026-10-12 Thomas checked on the box that each knob sits at its
on-screen corner (E1 top-left, E2 bottom-left, E3 top-right, E4 bottom-right) and that 10 clicks
read +10. Nobody has confirmed yet which physical button is B1–B4, and `/etc/k1000/k1000.yaml`
still holds the defaults.
Until the map is confirmed, a B3 hold only shows a hint (`B3 HOLD: IN/OUT AFTER KNOB TEST`): a
swapped map cannot take a band OUT (D-014's gate) or reset the EQ, but the emergency mute (a B4
hold, D-013) would then land on another button and do nothing: confirm the map before the
speakers. When the Knob test shows the defaults are right, add `buttons_confirmed: true` under
`controls:` (a different `buttons` list counts as confirmed by itself); then a B3 hold on SHOW
switches the selected band IN/OUT. `k1000-m4-bringup.sh` warns while the defaults are in place.

What each button and knob does per page (SHOW · ALIGN · SETUP) is the operator card,
`docs/04-operator-card.md`. In short (D-013): B1 tap = SHOW ↔ ALIGN (SETUP → SHOW), hold = SETUP ·
B2 = next band on SHOW, a hold unlocks the delay and Lo-Cut on ALIGN · B3 = the notch helper on SHOW
· B4 hold = mute/unmute (every page). The knobs (D-015): SHOW E1 MASTER · E2 FREQ · E3 Q · E4 GAIN
of the selected band; ALIGN E1 MASTER · E2 DELAY · E3 BALANCE · E4 LO-CUT, the two bottom ones
(DELAY, LO-CUT) locked. ALIGN's E2 DELAY (D-011) is one setting for both sides:
clockwise from 0 delays the subs (`SUBS LATER`), anticlockwise the tops (`TOPS LATER`); it writes
`sub_delay_ms` or `top_delay_ms` and zeroes the other (SETUP has no Top delay any more). The Lo-Cut
moves 2 Hz a click (1 Hz with the push).

1. SETUP › BOX › Knob test (HWTEST). Press every button and turn every knob. Note, by physical
   position, the `sw`/`enc` index each one shows, and the name the code gives it (`= B1` … `= B4`:
   sw4–sw7 by default).
2. Write `controls:` in `/etc/k1000/k1000.yaml` (encoders, pushes, buttons, reverse), then
   `sudo systemctl restart k1000`. If the on-screen labels ever sit at the wrong corners (another
   shell), that is `KNOB_CORNERS` in `k1000/ui/input_map.py`: a code change, deployed like any other.
3. Label the buttons on the shell as the Knob test names them: B1 PAGE · B2 BAND / UNLOCK (hold) ·
   B3 NOTCH · B4 MUTE (hold) (D-013). The knobs can carry a sticker too (E1 top-left, E2
   bottom-left, E3 top-right, E4 bottom-right): the screen's own E1–E4 tags are then redundant. The
   key `ui.knob_tags` in `/etc/k1000/k1000.yaml` sets them: `dim` (small grey, the default), `off`
   (none; the labels take the corners) or `bright` (cyan, v1 as first deployed). A box whose
   k1000.yaml predates the key has no `ui:` section and shows `dim`. To change it, add the section
   at the end of the file, two lines, the second indented by two spaces, and restart k1000:

   ```yaml
   ui:
     knob_tags: off
   ```

   On one line it is `ui: {knob_tags: off}`; `ui: knob_tags: off` does not parse, and a k1000.yaml
   that does not parse loses every setting but `profile:` (the screen says `SETTINGS: …`).
4. At the rig test, with program playing: hold B4, and all four Meyers mute.

In a terminal, the monitor needs the MCP23017 and GPIO lines, which the service holds, so stop
the service first:

```sh
sudo systemctl stop k1000
cd /opt/k1000 && python3 -m k1000.hw.mcp23017 --monitor        # --raw for every pin change, --selftest for the interrupt lines
sudo systemctl start k1000
```

## SHOW spectrum: on, off, and out

The program spectrum behind SHOW's EQ curve (D-009) polls the engine on a second websocket, only
while SHOW is displayed and SETUP › LIVE EQ › Spectrum is ON (`docs/03-v0-architecture.md` §8).

- **SETUP › LIVE EQ › Spectrum** switches it at run time (saved in `state.json`; E2 chooses, a push
  sets). Off, SHOW draws the curve alone and B3 still sets a notch up (`NO PEAK`: E4 down first,
  about −6 dB, then sweep E2 until the ring drops; at 0 dB the sweep is silent).
- **Out entirely** (Friday's no-go, or anything odd): `spectrum: enabled: false` in
  `/etc/k1000/k1000.yaml`, then `sudo systemctl restart k1000` between shows. No task and no second
  connection; SETUP › LIVE EQ › Spectrum then reads `DISABLED` (read-only).
- After an engine start the spectrum comes back by itself within a few seconds: its link
  reconnects (back-off up to 2 s) and waits for the engine to read `Running`, then CamillaDSP
  5.0.0-beta.1's analyser fills its buffer in well under a second (`Insufficient data in buffer`,
  retried quietly every 0.4 s; fixed after the 2026-10-13 acceptance, which saw `NO SPECTRUM` for
  ~5 s). A grey `NO SPECTRUM` now means the engine really refused (or was still warming up after
  10 s, or gave no data for 30 s of warm-up answers): `journalctl -u k1000 | grep -i spectrum`.
- **No PEAK on silence** (fixed after the acceptance, where a −114 dBFS noise bin was a PEAK): the
  cyan pointer needs the peak bin at `spectrum.peak_floor_db` (−80 dBFS) or above and the program's
  capture RMS at `spectrum.program_floor_db` (−60 dBFS) or above, both in `/etc/k1000/k1000.yaml`
  (built-in defaults when absent; −140 turns a gate off; restart k1000 after a change). On the
  silent bench input there is no pointer and B3 says `NO PEAK`.

## Soak test (overnight, and the 2-hour program run)

**One unit and one log per run.** A log is only ever appended to, and `--summary` judges
everything from its first sample to its last: two runs in one log are judged as one window, the
gap between them included. The names used this week: `soak-night.log` (the night to Tue 13, done),
`k1000-soak-m4` / `soak-m4.log` (the M4 day's hour), `k1000-soak-thu` / `soak-thu.log`
(Thursday's overnight soak), `k1000-soak-2h` / `soak-2h.log` (the 2-hour program run). Without
`K1000_SOAK_LOG` the tool appends to `/var/log/k1000/soak.log`.

Thursday's overnight soak:

```sh
sudo systemd-run --unit=k1000-soak-thu --uid=k1000 -E K1000_SOAK_LOG=/var/log/k1000/soak-thu.log \
  /opt/k1000/tools/k1000-soak.sh
tail -f /var/log/k1000/soak-thu.log
/opt/k1000/tools/k1000-soak.sh --summary /var/log/k1000/soak-thu.log   # in the morning: "clean", or what needs a look (exit 1)
sudo systemctl stop k1000-soak-thu            # "not loaded" = it had already ended: a reboot?
```

For the 2-hour program run:

```sh
sudo systemd-run --unit=k1000-soak-2h --uid=k1000 -E K1000_SOAK_LOG=/var/log/k1000/soak-2h.log \
  /opt/k1000/tools/k1000-soak.sh
/opt/k1000/tools/k1000-soak.sh --summary /var/log/k1000/soak-2h.log
```

The tool writes one line every 10 s:

```
2026-10-15T23:00:10+02:00 state=Running load=1.23 buffer=224 clipped=0 xruns=0 xruns_all=0 rate=48000 restarts=0 temp_c=52.1 throttled=0x0 main_db=-20.0 muted=false k1000=active k1000_restarts=0 boot=3f2a9c1e up=8123
```

`--summary` flags:

- the soak stopped: the last sample is long before now, or there is a gap. A reboot ends it (a
  `systemd-run` unit does not come back), and `journalctl --list-boots` then says when;
- a reboot: a new `boot=` id, or `up` going down;
- dropouts during the soak (playback underruns, capture overruns, stalled device, device
  errors; a stall counts twice, entering and leaving): engine log lines in
  `/var/log/k1000/camilladsp_r*.log` dated from the first sample to the last one (+ 3 intervals,
  at least 60 s), and any rise of `xruns`/`xruns_all` between samples. A dropout from before the
  soak (an older engine run's rotated log; an `xruns_all` already above 0 at the first sample) is
  printed as a `note:` with its time, never as a failure (fixed after the 2026-10-13 acceptance,
  whose summary exited 1 on an underrun from the night before);
- restarts of camilladsp (`restarts`: a crash, or a lost device) or of k1000 (`k1000_restarts`);
- `state` other than `Running` (`Paused` = CamillaDSP's silence pause, which K1000 leaves off, so
  a silent input still reads `Running`; `DOWN` = no engine websocket), k1000
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
  - Live EQ: SETUP › LIVE EQ › Reset live EQ, push twice within 5 s. Undo reset (one push) puts it
    back exactly, once, until the next audible live-EQ edit or 5 min.
  - A torn `state.json` is rebuilt from `active.yml` (the bad copy is kept as `state.json.bad`) and
    comes back MUTED ("STATE REBUILT · CHECK MASTER").
  - If the state file itself is bad, stop k1000, move `state.json` away and start k1000. Next to a
    K1000 `active.yml` it comes up on defaults and stays MUTED ("NO SAVED STATE · CHECK MASTER").
    Check the master, then hold B4. An engine that is already playing is adopted unchanged.
- **The show itself:** the bypass kit. Re-patch the source straight into the Meyers
  (`docs/02-plan-to-sunday.md` §5).

## Tests

```sh
cd /opt/k1000 && python3 -m pytest -q -p no:cacheprovider tests tools/tests   # unit tests (no hardware)
cd /opt/k1000 && python3 -m pytest -q -p no:cacheprovider -m hw              # hardware tests (opt-in; stop k1000 first)
```

`tools/tests` covers the engine unit's start script, the panel console guard, install.sh's helpers
(headless leftovers, I2C at boot, `--deps`, the phone certificate, `--ap`), the soak tool, the M4
bring-up tool, `k1000-lean.sh` (the guard, the dry run, the boot-file edits, the power-cycle check,
the cloud-init gate, the detached run), `bootstrap.sh` (apt only when needed, SSH keys only, the
detached run, `--no-ap`), `net/setup-ap.sh` (its defaults, `--down` kept by later runs and `--up`,
every option, both backends, the subnet and Wi-Fi-client refusals, a hostapd that dies after its
start, the fence's caps), the hostapd unit's refusals and `net/k1000-ap-guard.sh`, all against
fakes (no service, card, package, network or speaker is touched). `tests/test_web.py`,
`test_qr.py` and `test_phone_wiring.py` cover the phone page, its QR codes and its wiring into
the controller (B4 and phone mute races). `tests/test_laptop.py`, `test_tune.py` and
`tools/tests/test_laptop_access.py` cover SETUP › PHONE › Laptop access (the k1000 side, the root helper
against a fake nft, the units, install.sh) and the `/tune` page (the parser on real REW exports,
apply/undo through the controller). UI v1 (D-009): `tests/test_ui.py` and `test_control.py` cover
the pages, buttons, lock, notch helper, reset/undo, master-up guard and alerts; `test_eqgraph.py`
the curve against CamillaDSP's own biquad assertions; `test_rta.py` the spectrum task (a fake
client, plus one engine test on the private control test port); `test_mcp23017.py` the decoder
(including phantom detents from a resting knob) and `test_knob_timing.py` the knob worker under a
busy GIL.
