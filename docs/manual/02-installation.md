# 2 — Installation

Three ways to a working box:

| Starting point | Path | Section |
|---|---|---|
| A new SD card | Raspberry Pi OS Lite + `box/bootstrap.sh` (recommended) | [§1–§5](#1-before-you-start) |
| A card flashed with the desktop image, K1000 already on it | `tools/k1000-lean.sh`, one step per power cycle | [§7](#7-converting-a-desktop-card) |
| A working K1000 to update | `rsync` + `install.sh` | [chapter 7](07-maintenance.md#updating) |

All commands below run **on the laptop**, from a clone of the repository, unless they start with
`ssh k1000`. The ops reference on the box is [`box/README.md`](../../box/README.md) (installed as
`/opt/k1000/README.md`).

## 1. Before you start

- A laptop with `git`, `ssh` and `rsync`, and a clone of the repository.
- An SSH key pair on the laptop.
- Docker, on the laptop or on any other machine, to build the engine once (§3, about two minutes),
  or a K1000 box that already runs it, to copy it from.
- The box on **Ethernet**, on a network with internet.
- **Speakers off**, or their XLRs unplugged, for the whole install.

Add the box to the laptop's `~/.ssh/config`, so every command can say `ssh k1000`:

```
Host k1000
    HostName k1000.local      # or the box's IP address (from your router)
    User k1000
```

`k1000.local` works when the laptop resolves mDNS names (avahi runs on the box).

## 2. Flash the card

Raspberry Pi Imager (the wording and the order of these settings vary between Imager versions):

| Setting | Value |
|---|---|
| Device | Raspberry Pi 4 |
| OS | **Raspberry Pi OS Lite (64-bit)** (under "Raspberry Pi OS (other)") |
| Hostname | `k1000` |
| User | `k1000`, with a password of your choice |
| SSH | On, **"Allow public-key authentication only"**, with your public key |
| Wi-Fi | **Leave it empty** (set only the Wi-Fi country if asked). The box makes its own Wi-Fi for the phone, and `setup-ap.sh` refuses to take over `wlan0` while it is a client of another network |

Boot the box on Ethernet. Log in once with your key:

```sh
ssh k1000 true
```

This login matters: `bootstrap.sh` switches SSH to keys only, and it does so only after it has seen
a public-key login of `k1000` since the boot.

## 3. Run `bootstrap.sh`

The engine first. K1000 runs a build of CamillaDSP from upstream's `next5` branch at a pinned
commit (decision D-019): it reached 5 to 8 ms in to out on the bench, under D-018's 10 ms
([`docs/17`](../17-box-record-and-bench.md)), and no release carries it yet, so there is nothing to
download. `box/tools/engine-build/build.sh` builds it in a Docker container, from the
repository's root. Built on another machine, copy its `out/camilladsp` to the laptop first.

```sh
box/tools/engine-build/build.sh             # its last line must be: this is the binary measured on the box
scp out/camilladsp k1000:/tmp/camilladsp
rsync -a --delete --filter='P apk/' --rsync-path="sudo rsync" box/ k1000:/opt/k1000-src/
ssh k1000 "sudo K1000_REVISION=$(git describe --always --dirty) CAMILLADSP_BINARY=/tmp/camilladsp /opt/k1000-src/bootstrap.sh"
```

**When `build.sh` does not end with that line.** It ends with `NOT the measured binary` when
something it does not pin gave a different binary (the Rust version and the base image are pinned,
the Debian packages are not). That binary was never measured: do not put it on a gig card. Take the
measured one from a box that already runs it, with that box on the network as `k1000`, then go on
from the `scp out/camilladsp` line above:

```sh
scp k1000:/usr/local/bin/camilladsp out/camilladsp
sha256sum out/camilladsp                    # dd4106c927a5de2b31776cc28d797a3f42fb9b7d02deb928f45a6feee057d43c
```

The same way works without Docker. `bootstrap.sh` checks the file against the same sha256 either
way, so a wrong one is refused and nothing changes. With no box to copy from, the fallback is
`--old-engine` (below), which does not meet the 10 ms.

Raspberry Pi OS Lite already has `rsync` on the box side (checked on the 2026-10-06 image).
Always give `K1000_REVISION` as above: without it, a run that brings new code from
`/opt/k1000-src` (no git there) records `unknown (from /opt/k1000-src)` in `/opt/k1000/REVISION`.
A run that syncs nothing keeps the `REVISION` already there.

What it does, each step skipped when already done:

| Step | What |
|---|---|
| 1. apt | `apt-get update` **only when** a package K1000 needs is missing, then the operator tools (`i2c-tools`) |
| 2. The engine | The pinned build of D-019 into `/usr/local/bin/camilladsp`, from `CAMILLADSP_BINARY`, checked against its pinned sha256. A box that already runs it keeps it and does not read the file. Any other engine (5.0.0-beta.1 included) or none, and no `CAMILLADSP_BINARY`: **the run stops before step 1**, changes nothing, and prints how to build and give the binary. A binary that is not the pinned build is refused the same way |
| 3. K1000 | `install.sh --boot --headless --ap`: the services, the screen overlay, I²C for the knobs, CamillaGUI, boot to the console, the phone's Wi-Fi access point and its firewall ([§6](#6-installsh-reference)). An access point kept down by `setup-ap.sh --down` (or `--no-ap`) stays down |
| 4. SSH | Keys only (`/etc/ssh/sshd_config.d/10-k1000-keyonly.conf`), checked with `sshd -t`, applied at the next boot. Skipped with a warning when no key login was seen |
| 5. Lean OS | `tools/k1000-lean.sh`: no desktop leftovers, no apt timers, swap in RAM only, a quiet boot ([§7](#7-converting-a-desktop-card)) |

It never reboots and never runs `apt upgrade`.

**The old engine, only when asked.** `--old-engine` installs CamillaDSP 5.0.0-beta.1 from its
release instead (its tarball and binary checked against their own pinned sums), over whatever
engine is there, the pinned build included. It is the fallback, the engine of the gig of 18
October, and it **does not meet the 10 ms of D-018**: it never gets under 12.5 ms in to out, and
keeps 13 to 15 ms more after one dropout ([`docs/17`](../17-box-record-and-bench.md)). The run
says so at step 2 and again at the end. Without `--old-engine`, the old engine never comes back:
not on a re-run, not from its tarball. So the second run after the power cycle (§4) needs
`--old-engine` again, or it stops and changes nothing; the first run's closing lines say so.

**A download, later.** Where a built engine is hosted is not decided. Once it is, its URL goes on
the marked line `CDSP_PIN_URL=` in `box/bootstrap.sh`; a run with internet then downloads the
binary and checks it against the same pinned sha256. Until then, `CAMILLADSP_BINARY` it is.

What the tests prove, on fakes: each case above (kept, installed, the stop, a wrong binary,
`--old-engine`, the download once a URL is set), dry runs included, that the pin in
`bootstrap.sh` is the one in `build.sh`, and that the detached run (below) gets the binary your
terminal checked and leaves its exit status even when it stops at the engine. Not yet done on the
box: a fresh card with `CAMILLADSP_BINARY`.

**It survives a lost SSH session.** A real run starts itself again as a systemd unit,
`k1000-bootstrap-<time>`, and your terminal only follows its log. If the session drops, the run
goes on:

```sh
ssh k1000 'ls -t /var/log/k1000-bootstrap/; systemctl list-units "k1000-bootstrap-*"'
ssh k1000 'tail -f /var/log/k1000-bootstrap/run-<time>.log'
```

`run-<time>.rc` holds the exit status once the run is done.

| Option / variable | Effect |
|---|---|
| `--dry-run` | Says what it would do, changes nothing. `install.sh` has no dry run, so its changes are not counted: a finished card dry-runs to `0 changes` |
| `--no-ap` | Everything but the phone's access point, which is marked down (`setup-ap.sh --down`): a spare card after a phone no-go. Later runs, with or without `--no-ap`, keep it down; `sudo /opt/k1000/net/setup-ap.sh --up` lifts it |
| `--old-engine` | Installs the old engine, CamillaDSP 5.0.0-beta.1, from its release: the fallback, which does not meet D-018's 10 ms (above) |
| `--help` | Prints its header |
| `CAMILLADSP_BINARY=<path>` | The engine: the binary `box/tools/engine-build/build.sh` made, on the box. Checked against the pinned sha256; not read when the pinned build is installed already |
| `CAMILLADSP_TARBALL=<path>` | With `--old-engine` only: the old engine's release tarball, offline. Without `--old-engine` it installs nothing |
| `CAMILLAGUI_TARBALL=<path>` | The same for CamillaGUI (passed on to `install.sh`) |
| `K1000_REVISION=<text>` | What `/opt/k1000/REVISION` records (passed on to `install.sh`) |
| `K1000_BOOTSTRAP_FOREGROUND=1` | Runs in place instead of as a unit (for a console on the box), `k1000-lean.sh` included |

Put the variables after `sudo`, as in the command above.

| Exit | Meaning |
|---|---|
| 0 | Done. The last lines say `N changes. Now power-cycle the box`, `0 changes: this card is already a K1000`, or `0 changes; still to do: cloud-init off (below)` (after the first power cycle, §4) |
| 1 | An error, printed. Nothing after it ran. `no engine to install` and a refused `CAMILLADSP_BINARY` stop before step 1: nothing changed |
| 2 | Unknown option |
| 3 | CamillaGUI is not installed (offline, or a hash mismatch). The access point may also have failed: read the `ERROR` lines at the end. Run again with internet or `CAMILLAGUI_TARBALL` |
| 4 | The phone's access point is not set up. Everything else is. `sudo /opt/k1000/net/setup-ap.sh --status`, then run again |

It warns, and goes on, when the OS is not trixie or when `k1000` has no `~/.ssh/authorized_keys`.
It refuses anything that is not arm64.

## 4. Power-cycle, check, run it again

1. Power-cycle the box: unplug, wait 5 s, plug back in. The boot-level changes apply now.
2. About 20 s later:

   ```sh
   ssh k1000 'systemctl is-active camilladsp k1000 camillagui; cat /opt/k1000/REVISION'
   ```

   Expect `active` three times. The panel shows K1000, and Main ramps in to the saved level.
3. **Run the bootstrap command once more**, then power-cycle again. The same command: it keeps
   the engine now installed and no longer reads `CAMILLADSP_BINARY` (the copy in `/tmp` may be
   gone after the power cycle).

Why twice: the first run leaves **cloud-init** on. Turning it off makes the boot about 2.6 s
faster, but then the K1000 service can start before the screen exists, and the Linux text console
would draw over the UI. A small unit, `k1000-console.service`, closes that race. `k1000-lean.sh`
turns cloud-init off only once that unit has run at a boot and found the panel. So the second
run, after the first power cycle, is the one that turns it off. The first run says so in its last
lines (`cloud-init is still on`).

A third run changes nothing: `0 changes: this card is already a K1000`.

## 5. Settings for this box

`install.sh` copies `box/etc/k1000.yaml` to `/etc/k1000/k1000.yaml` **once** and never overwrites
it. It starts on profile `dev` with the default knob map. Then:

1. **Profile.** For the PA: `profile: m4` (the MOTU M4). See [chapter 3](03-configuration.md) and
   [`docs/05-m4-day.md`](../05-m4-day.md) for the first M4 bring-up.
2. **Knob map.** SETUP › BOX › Knob test, then `controls:` ([chapter 3](03-configuration.md#5-controls)).
   On a second card, copy the main card's file instead.
3. **The phone certificate**, if you use the phone page:

   ```sh
   ssh k1000 'install -d -m 0700 /tmp/k1000-tls'
   scp k1000.crt k1000.key k1000-ca.crt k1000:/tmp/k1000-tls/     # never the CA's private key
   ssh k1000 "sudo K1000_TLS_DIR=/tmp/k1000-tls K1000_REVISION=$(git describe --always --dirty) /opt/k1000-src/install.sh --ap; rm -rf /tmp/k1000-tls"
   ```

   Double quotes, so `git describe` runs on the laptop and `/opt/k1000/REVISION` stays right
   (without it, a run from `/opt/k1000-src`, which has no git, can record `unknown`); `;`, so the key copy is removed
   whatever `install.sh` says. `install.sh` checks the three files, and the key against the
   certificate, before it changes anything. `--ap` (re)applies the access point; drop it if the
   access point is already set up. K1000 loads the certificate at its next start:
   `ssh k1000 'sudo systemctl restart k1000'` (between shows; the audio keeps playing). Without a certificate the phone page runs on plain HTTP
   (`http://10.42.0.1:8080/`) and cannot be installed. How the certificate is made: [chapter
   7](07-maintenance.md#the-phone-certificate). The phone side: [`docs/06-phone.md`](../06-phone.md).

After any edit of `k1000.yaml`: `sudo systemctl restart k1000`.

## 6. `install.sh` reference

`install.sh` deploys `box/` onto the Pi. It is idempotent: a re-run with nothing new ends with
`0 changes`. Run it from a copy of `box/` on the box (usually `/opt/k1000-src`), never from
`/opt/k1000.prev`.

| Flag | What it adds |
|---|---|
| *(none)* | Installs and updates the files, enables the five units (`camilladsp`, `k1000`, `camillagui`, `k1000-console`, `k1000-usbwatch`), and installs the two Laptop access units (`k1000-laptop.path`, `k1000-laptop.service`), never enabled: k1000 brings the watcher. Installs the processor settings unless they were switched off (below). **Starts nothing.** A changed access-point unit is only reported (`not applied; between shows: … --ap`), never copied |
| `--start` | Then (re)starts those five units. **The audio drops for about a second**, then ramps back in. Never during a show. Refused (exit 1, nothing changed) when the processor settings are off but the reboot that applies it has not happened yet (`--cpu-off` below) |
| `--boot` | Boot-level setup, applied at the next reboot: compiles the screen overlay into `/boot/firmware/overlays/`, adds `dtoverlay=k1000-zynscreen,rotate=270` to `config.txt`, adds `vt.global_cursor_default=0` to `cmdline.txt` (original kept as `cmdline.txt.k1000-orig`), turns I²C on for the knobs (`dtparam=i2c_arm=on`, `i2c-dev` loaded at boot) |
| `--headless` | Boots to the console instead of a desktop (`multi-user.target`), masks PipeWire/WirePlumber for all users, disables Bluetooth, printing and the NFS client/rpcbind, removes the tty1 auto-login. Applied at the next reboot. It leaves cloud-init alone (see §4) |
| `--ap` | Runs `net/setup-ap.sh`: the phone's Wi-Fi "K1000" on `wlan0` and its firewall, and installs or updates the access point's units (the only way they are). Takes effect at once. Later runs keep every earlier choice, a no-go included: after `setup-ap.sh --down` the access point stays down until `setup-ap.sh --up`. It refuses (exit 4, nothing changed) while another interface holds a 10.42.0.x address or `wlan0` is a client of another Wi-Fi |
| `--cpu-off` | Removes the [processor settings](#processor-settings) and keeps them off on every later run (`/etc/k1000/cpu-tuning` says `off`), and takes both bench switches off (each remembered `off`). **Reboot to apply (between shows).** Until then systemd keeps cores 0-1 for everything it starts: an engine restarted before the reboot (a USB drop, a `systemctl restart`) shares them with everything else, worse than both on and off. So `--start` is refused until that reboot: run `--cpu-off` alone, then reboot |
| `--cpu-on` | Puts them back and keeps them on (the default). Cores and nice apply to what starts from then on, the governor at the next boot; a reboot applies all of it. The bench switches stay as they were (off after a `--cpu-off`) |
| `--usb-cpu0-on` | [Bench switch](#the-bench-switches-d-023) usb-cpu0, core 0 left to the USB handling: on, at once and at every boot, and kept on (`/etc/k1000/usb-cpu0`). No restart, no reboot. Refused (exit 1, nothing changed) while the processor settings are off, unless `--cpu-on` comes with it |
| `--usb-cpu0-off` | Takes it off at once and keeps it off (the default) |
| `--usb-rt-on` | [Bench switch](#the-bench-switches-d-023) usb-rt, real-time priority for the deferred USB work: the same way (`/etc/k1000/usb-rt`) |
| `--usb-rt-off` | Takes it off at once and keeps it off (the default) |
| `--deps` | Prints the apt packages it may install, one per line, and exits |
| `-h`, `--help` | Prints its header |

| Variable | Effect |
|---|---|
| `K1000_REVISION=<text>` | What `/opt/k1000/REVISION` records. Default: `git describe` of the source, else the source's own `REVISION` file (a rollback copy is marked "reinstalled from …") |
| `K1000_BOOT_DIR=<dir>` | Another boot directory than `/boot/firmware` (only to test `--boot` on a copy) |
| `CAMILLAGUI_TARBALL=<path>` | Installs CamillaGUI from a local tarball instead of downloading it |
| `K1000_TLS_DIR=<dir>` | Installs the phone page's certificate from `<dir>` (`k1000.crt`, `k1000.key`, `k1000-ca.crt`) into `/etc/k1000/tls`, root:k1000 0640. It refuses a key that does not match the certificate, and never copies the CA's private key (`ca.key`), only warns if it finds one |

| Exit | Meaning |
|---|---|
| 0 | Done |
| 1 | An error, printed (not root, not a `box/` tree, a bad certificate pair, `--start` before the reboot that `--cpu-off` needs, a bench switch while the processor settings are off…) |
| 2 | Unknown option, or `--cpu-off` with `--usb-cpu0-on` or `--usb-rt-on` |
| 3 | CamillaGUI failed. Everything else is installed |
| 4 | The access point failed. Everything else is installed |
| 5 | Both failed |

What it never does: overwrite `/etc/k1000/k1000.yaml` once it exists, touch the live state in
`/var/lib/k1000` (except removing an empty `coeffs/` folder left there by an older version), or
reboot.

It also keeps the previous version in `/opt/k1000.prev` before changing anything (rollback, §9),
installs a hardware watchdog setting (`RuntimeWatchdogSec=15`) and makes the journal persistent
(32 MB cap). The CamillaDSP binary itself comes from `bootstrap.sh`; `install.sh` only warns when
it is missing or not 5.x. When the source carries the Android app (`apk/K1000-<version>.apk`, put
there by the deploy, never in git), it installs the newest one to `/opt/k1000/apk/` for the
first-time page, and keeps the box's when there is none ([chapter
7](07-maintenance.md#the-android-app-on-the-box)).

### The control service's watchdog

Like the engine, k1000 restarts 1 s after a crash. `k1000.service` also restarts when the service
**hangs**: it is `Type=notify` with `WatchdogSec=15`. k1000 tells systemd it is up (`READY=1`) once
its knobs, screen and phone page are started, then sends `WATCHDOG=1` every 3.75 s from its event
loop, never from a side thread, so a loop that is stuck stops sending. 15 s after the last one,
systemd sends SIGABRT (the journal gets every thread's Python traceback: where it hung), then
starts k1000 again 1 s later. A start that never reaches `READY=1` is killed after 30 s and
retried. Without systemd (tests, a run by hand) nothing of this happens.

The engine does not depend on k1000 and keeps playing throughout. What the new k1000 does to it,
read from `box/k1000/control.py` (`_sync`) and held by the tests:

- An engine playing at its level (Main unmuted, K1000's marker on the unused Aux4 fader): it reads
  the version, the config, the faders and the state, and **adopts** them. No mute, no ramp, no
  config upload. Edits that reached the engine before the hang are kept and saved; a knob edit
  still on its way is lost. It writes the marker again only if it is missing.
- Killed in the middle of a boot ramp or a click guard (the marker records it): it finishes it.
  Main's ramp continues from where Main stands, without a dip; a group muted by a guard is
  unmuted.
- A pipeline changed in CamillaGUI while k1000 was hung: put back at once, with Main muted around
  the upload (a short gap). Values K1000 cannot adopt (out of its ranges, a group fader raised
  above the balance): pulled down in small steps, while cuts and mutes made outside it are kept. A
  profile changed in `k1000.yaml`: applied (about 1 s of silence, then the ramp). None of these
  happens on a box where nobody edited anything meanwhile.

Meanwhile the knobs and B4 do nothing: the 15 s, then 1 s and k1000's start-up. The screen keeps
the last frame until the new k1000 draws (a SIGABRT does not draw the "restarting…" frame).

```sh
journalctl -u k1000 -b | grep -iE "watchdog|systemd: ready"    # restarts, and each start's READY
systemctl show -p NRestarts -p WatchdogUSec k1000
# a hang on purpose, between shows: the sound must not change. systemd follows its SIGABRT with a
# SIGCONT, so even a stopped k1000 prints its traceback (the loop idle in its selector) and exits:
# it is back about 20 s later (11 to 15 s after the stop, then 1 s and its start-up)
sudo kill -STOP "$(systemctl show -p MainPID --value k1000)"
```

### Processor settings

`install.sh` installs five small files from `box/cpu/`, listed in one table (`CPU_FILES`), and
`--cpu-off` takes them all away (each file says why it exists). Switched on, each applies as the
last column says; switched off, they all leave at the next boot only (see `--cpu-off` above).

| File on the box | What | From |
|---|---|---|
| `/etc/udev/rules.d/61-k1000-cpu-governor.rules` | CPU governor `performance`: the clock stays at its maximum. Raspberry Pi OS sets `ondemand` with its own rule (`60-ondemand-governor.rules`); this one runs after it | the next boot |
| `/etc/systemd/system.conf.d/50-k1000-cpu.conf` | `CPUAffinity=0 1`: every process on cores 0 and 1 unless its unit says otherwise | install.sh's daemon-reload, for what systemd starts from then on; everything at the next boot |
| `/etc/systemd/system/camilladsp.service.d/50-k1000-cpu.conf` | `CPUAffinity=2 3`: the engine alone on cores 2 and 3 | the engine's next start |
| `/etc/systemd/system/k1000.service.d/50-k1000-cpu.conf` | `CPUAffinity=0 1`, `Nice=5`. Intended, not verified: the control service yields to the system's own work under load and still comes before CamillaGUI. Likely no effect until the scheduling-group check below is done | its next start |
| `/etc/systemd/system/camillagui.service.d/50-k1000-cpu.conf` | `CPUAffinity=0 1` (its unit keeps `Nice=10`) | its next start |

The cost of `performance`: the processor no longer slows down when idle, so the box draws a
little more power and runs warmer. Not measured yet. The firmware still slows it down on heat or
low voltage, so watch these after a long run, with and without the settings:

```sh
vcgencmd measure_temp; vcgencmd measure_clock arm; vcgencmd get_throttled   # throttled=0x0 is clean
cat /sys/devices/system/cpu/cpufreq/policy0/scaling_governor                 # performance
grep Cpus_allowed_list /proc/1/status                     # systemd: 0-1 on, 0-3 off (after a reboot)
pid=$(systemctl show -p MainPID --value camilladsp)
taskset -acp "$pid"                                       # every engine thread may use 2,3
taskset -acp "$(systemctl show -p MainPID --value k1000)" # every k1000 thread: 0,1
ps -L -o tid,psr,rtprio,comm -p "$pid"                    # psr: the core each last ran on
# a snapshot of user-space threads last seen on cores 2-3, kernel threads left out: the engine's only
ps -e -L -o pid=,ppid=,psr=,comm= | awk '$1 != 2 && $2 != 2 && $3 >= 2'
```

The knobs share cores 0 and 1 with everything else now: compare a knob trace
(`tools/k1000-knobtrace.py capture --guided`, [chapter 8](08-development.md)) with and without
the settings, beside the dropout and boot-time checks.

Not set, on purpose: each needs a measurement on the box first.

- **Which core handles the interrupts.** USB (the M4), Wi-Fi, the SD card and Ethernet are on
  core 0 unless moved. The USB host's cannot move: it is an MSI behind the PCIe controller, which
  the kernel refuses to move (read in its source, D-023). So the bench switch usb-cpu0 moves the
  others off core 0 instead ([below](#the-bench-switches-d-023)), off by default.
- **`isolcpus=2,3`.** It would keep even the kernel's balancing off the engine's cores, but then
  the engine's threads stay on the core they start on, all on one if unlucky, unless each is
  pinned by hand. `nohz_full` is not built into Raspberry Pi OS's 64-bit kernels (checked on
  6.18.60, normal and real-time).
- **A real-time kernel.** Raspberry Pi OS trixie has one as a package: `linux-image-rpi-v8-rt`
  (`PREEMPT_RT`; it carries the screen, touch, USB audio and Wi-Fi drivers). It installs as
  `kernel8_rt.img` (and `initramfs8_rt`) next to the normal kernel and boots only with
  `kernel=kernel8_rt.img` in `config.txt`, so the way back is that one line. To measure with
  `cyclictest` (Debian's `rt-tests`) on cores 2 and 3, under load, normal kernel against
  real-time.
- **Whether `Nice=` ranks one service against another.** Only when they share a scheduling
  group, and two things can give each service its own: the kernel's autogroup (built in, on by
  default) and systemd's CPU control groups.
  `cat /sys/fs/cgroup/cgroup.subtree_control /sys/fs/cgroup/system.slice/cgroup.subtree_control`:
  `cpu` in the second, one group per service and the lever is `CPUWeight=`; `cpu` in the first
  only, the services share one group and nice counts; in neither, the autogroup decides and
  `kernel.sched_autogroup_enabled = 0` (a sysctl) makes nice count. CamillaGUI's `Nice=10` has
  the same question.

Each is judged the same way: the loop latency, the produced fault and a night run of
`docs/16-post-gig-audit.md` §3.3, before and after.

### The bench switches (D-023)

Two system settings that should prevent the stall behind the silent delay steps of the 96 kHz
night (`docs/17-box-record-and-bench.md` §10, D-023 point 3). Read in the kernel's source: the M4's
playback follows its capture stream, and when the handling of its USB completions on core 0
stalls for about 3 ms, the playback queue rises to its maximum and stays there. Measured on the
box: two such steps in 13 hours at 96 kHz (13 to 14 Nov), one on 12 Nov at 48 kHz under load, none
in 21 minutes of heavy load on core 0. Read on the box: every xHCI interrupt on core 0,
`ksoftirqd/0` at normal priority, the SD card's and Wi-Fi's interrupt thread (`irq/40-mmc0`) at
FIFO 50 on core 0, no `irqaffinity`, `isolcpus` or `threadirqs` on the kernel command line.

**Both switches are off by default and NOT yet measured against the delay steps**: they are
switches so that the bench can turn each on and off and compare. Each:

- is turned on by its `-on` flag and off by its `-off` flag, the choice remembered in
  `/etc/k1000/usb-cpu0` or `/etc/k1000/usb-rt` (`on`; absent or anything else is off);
- needs the processor settings on: refused, with nothing changed, while they are off, unless
  `--cpu-on` comes with it. `--cpu-off` takes both off and writes both choices `off`, so a later
  `--cpu-on` brings back the processor settings alone;
- applies at once through its own root unit (`k1000-usb-cpu0.service`, `k1000-usb-rt.service`, from
  `box/cpu/`, enabled: so at every boot too). A later install.sh run applies it again only when
  its files changed, or when its unit is not active (a failed start is retried); a new
  `tools/k1000-cpu.py` alone applies at the next boot, or at once with
  `sudo systemctl restart k1000-usb-cpu0` (or `k1000-usb-rt`). A unit that
  cannot be enabled is not started, and a unit that fails is undone (below): either is a warning
  (`WARNING: … NOT applied`), the rest of the install goes on, and the next run tries again;
- comes off at once: install.sh stops its unit while the unit file is still there, the unit's
  `ExecStopPost` undoes what it applied, then the files go. The same `ExecStopPost` runs after a
  failed start: what the unit had changed at run time is put back at once. Its files stay,
  though, so a failed usb-cpu0 is only half off: systemd, k1000 and CamillaGUI take core 1 again
  at the next daemon-reload or their next start, while the interrupts stay on core 0. The check
  then says `NOT AS CHOSEN`; the next install.sh run, or
  `sudo systemctl restart k1000-usb-cpu0`, tries again, and `--usb-cpu0-off` removes it all;
- needs no service restart and no reboot, on or off. Like every install.sh run: never during a
  show.

**usb-cpu0: core 0 left to the USB handling.** Everything that ran on cores 0 and 1 goes to core
1; the engine stays alone on 2 and 3.

| What | How | When |
|---|---|---|
| Every process systemd starts (the other K1000 units, sshd, journald…): core 1 | `/etc/systemd/system.conf.d/51-k1000-usb-cpu0.conf`: `CPUAffinity=` then `CPUAffinity=1`. The lines add up, so the empty one resets the `0 1` of `50-k1000-cpu.conf` | install.sh's daemon-reload |
| k1000 and CamillaGUI: core 1 | `/etc/systemd/system/{k1000,camillagui}.service.d/51-k1000-usb-cpu0.conf`, the same two lines (k1000's `Nice=5` stays) | their next start; the unit has already moved the running ones |
| Every interrupt the kernel lets move: core 1. The SD card's and the Wi-Fi's (one shared interrupt, two threads at FIFO 50) matter most; their threads follow | `/proc/irq/<n>/smp_affinity_list` | the unit's start |
| Interrupts set up later (Ethernet when its link comes up, the knob board's lines when k1000 starts…): core 1 | `/proc/irq/default_smp_affinity` = `2` | the unit's start |
| The kernel's unbound workqueues: cores 1-3. The Wi-Fi driver's receive work and the SD card's writeback run there | `/sys/devices/virtual/workqueue/cpumask` = `e` | the unit's start |
| Every user-space thread allowed on core 0, except the engine's: core 1 | `sched_setaffinity` | the unit's start |

The unit (`/opt/k1000/tools/k1000-cpu.py usb-cpu0 apply`) logs what it moved, what it kept on core
0 and what the kernel refused, and writes it to `/run/k1000-usb-cpu0/report` for the check. It
never touches the engine: an engine still allowed on core 0 (the processor settings just turned
on, the engine not restarted since) is a warning. Off: every interrupt on core 1 alone back on
every core (except the ones it found off core 0 already), new interrupts and the workqueues on
every core, the threads on core 1 alone back on cores 0 and 1, and systemd back on 0 and 1 at the
daemon-reload: what a boot without the switch gives.

What stays on core 0: the xHCI's interrupt, the M4's USB host (an MSI behind the PCIe controller,
which the kernel refuses to move; the switch never writes it, even on a kernel that would allow
it) and its completion work, which runs on the core that took the interrupt; the per-core
interrupts (the timer tick, the inter-processor ones); the per-core kernel threads
(`ksoftirqd/0`, `kworker/0:*`, `migration/0`…); any interrupt the kernel refuses to move (the unit
names them; the GPIO lines, demultiplexed by the GPIO controller, may be among them). Not moved by
the switch: the kernel threads that may run on any core (`jbd2` for the SD card's journal,
`rcu_preempt`, `kswapd0`…), and the PCIe controller's own MSI interrupt, which has no handler of
its own (moving it might move USB with it: untested).

Why a unit and not `irqaffinity=1` on the kernel command line: the same interrupts move either
way, but the unit needs no edit of the boot files and no reboot, on or off, so the bench can
switch it within one boot.

Its cost, to watch with it on: everything but the engine shares one core. k1000 alone uses about
30 % of one; the SD card's and the Wi-Fi's interrupt threads run there too. The knobs (a knob
trace), the phone under edits and the boot time to full level (everything at boot runs on one
core) are the things to compare.

**usb-rt: real-time priority for the deferred USB work.** The USB completion work runs as
soft-interrupt work on core 0, just after the xHCI's interrupt. When it runs over 2 ms, or the
scheduler has a task waiting for the core, the kernel hands the rest to `ksoftirqd/0`, a thread
at normal priority (`SCHED_OTHER`): then it waits behind whatever task holds core 0. The unit
(`k1000-cpu.py usb-rt apply`) sets `ksoftirqd/0` to `SCHED_FIFO 51`:

- above every normal task on core 0, and just above the interrupt threads (FIFO 50, the kernel's
  priority for them; the SD card's and the Wi-Fi's among them unless usb-cpu0 moves them): the
  USB stream has a deadline of a few milliseconds, an SD-card write or a Wi-Fi frame finished a
  millisecond later costs nothing audible. Far below `migration/0` (99). The engine's threads
  (FIFO 10) are on cores 2-3 and never meet it;
- the xHCI's interrupt thread too, at FIFO 51, if the kernel runs one (only with `threadirqs`,
  below). `ksoftirqd/1` to `/3` are left alone.

Off: `ksoftirqd/0` back to `SCHED_OTHER`, an xHCI thread back to FIFO 50, the kernel's own values.
Nothing is written to any file.

What could go wrong: `ksoftirqd/0` runs all the deferred soft-interrupt work of core 0, not only
USB (network receive, block completions, timers, RCU). A flood of it would hold core 0 above every
normal task and the interrupt threads there, up to the kernel's real-time throttle
(`/proc/sys/kernel/sched_rt_runtime_us`: by default, real-time work may use 950 ms of each second
of a core). With usb-cpu0 also on, the network's, the SD card's and the Wi-Fi's work has left core
0. It fails safe: refused, with nothing changed, while that throttle is off (`-1`); a failed start
is undone; a boot without the unit is the kernel's default.

**Not part of either switch, on purpose: `threadirqs`** on the kernel command line. It would run
every interrupt handler in a thread, the xHCI's included, so that its completion work gets a
priority of its own. Not done: it changes every driver's interrupts at once (the knobs, the
screen, the SD card, the network), it hands every soft interrupt raised by a hardware interrupt
to a thread (`ksoftirqd`, timers to `ktimers`), and it needs an edit of `cmdline.txt` and a
reboot, on and off. If steps still come with both switches on, and a trace shows the completion
work delayed inside the interrupt path itself, it is the next lever; usb-rt already raises the
xHCI's thread when there is one.

**The check** needs no root and changes nothing:

```sh
/opt/k1000/tools/k1000-cpu.py check     # --sample 0 skips the 2 s count of core 0's interrupts
```

For the processor settings it prints the governor, systemd's cores and each K1000 unit's cores.
For each switch, what is chosen and whether it is in force, part by part (`[x]` or `[ ]`):
usb-cpu0's report, systemd's and the K1000 units' cores, the user-space threads still allowed on
core 0, the masks for new interrupts and workqueues, whether the interrupts it moved are still
off core 0, the ones that stay there and why (an interrupt set up after the unit ran, on a chip
that cannot move it, is named but does not count against the switch), the interrupt threads'
cores; usb-rt's `ksoftirqd/0` and xHCI threads (scheduling class, priority),
every real-time thread allowed on core 0, the throttle. Last, the interrupts core 0 took in 2 s,
by kind: with usb-cpu0 on, only the xHCI's, the timer's and the inter-processor ones should be
left. Exit 0 when each switch is as chosen, 1 when one is not (`NOT AS CHOSEN`).

**Proven, measured, not verified.** Proven by tests here, against a fake `/proc` and `/sys`
shaped like the box and a fake `systemctl` (`box/tools/tests/test_cpu_switches.py`, the installer
tests in `test_sys_tools.py`): the flags, off by default, the choices remembered and read like
`cpu-tuning`, each switch's files written and removed, its unit stopped before its file goes,
`--cpu-off` taking both off, the refusals, which interrupts and threads each switch changes and
puts back, the check's verdicts. Measured on the box: only what led to them (above). **Not
verified**: that the kernel on the box accepts each write and which interrupts it refuses, that
systemd applies the empty-then-`1` `CPUAffinity=` at a daemon-reload, the units' sandbox, the
costs above, and above all the effect on the delay steps.

## 7. Converting a desktop card

If the card was flashed with the desktop image, `tools/k1000-lean.sh` converges it to Lite plus
K1000 (decision D-006). This is the path the first box took, on a card where K1000 already ran:
the step-by-step version of what `bootstrap.sh` step 5 does in one run. On a fresh Lite card it
finds next to nothing to remove. (Running `bootstrap.sh` straight on a desktop card has not been
tried.)

| Step | What |
|---|---|
| `keep` | Marks every package K1000 needs as manually installed, so no autoremove can take one |
| `desktop` | Purges the desktop metapackages and leftovers, then autoremove (about 970 packages on the first box) |
| `services` | cloud-init off (only once the panel guard has run, §4); NetworkManager-wait-online, the boot-time EEPROM check and `e2scrub_reap` disabled |
| `timers` | apt-daily, apt-daily-upgrade, man-db and e2scrub_all disabled |
| `swap` | Swap in RAM only (zram): no swap file on the SD card |
| `boot` | A quiet boot: `quiet`, `logo.nologo`, no splash |
| `firmware` | **Opt-in**, only with `--only firmware`: no initramfs, no camera or display probing (`config.txt`). Refused unless the card boots without an initramfs. Prints the bootloader's `DISABLE_HDMI=1` as a command to run by hand, as a power cycle of its own. With both, Linux starts 7.85 s after power-on instead of 10.3 s (box/README.md « Boot to sound ») |

Its guard: every removal is simulated first and its plan saved in `/var/backups/k1000-lean/`. A
plan that would remove anything K1000, the boot, SSH, the network, the Wi-Fi, systemd, libc, Python
or ALSA needs is **refused** (exit 1, nothing removed). Boot files are backed up before any edit.

**One change set per power cycle**, and the check before each one:

```sh
rsync -a --delete --filter='P apk/' --rsync-path="sudo rsync" box/ k1000:/opt/k1000-src/
ssh k1000 "sudo /opt/k1000-src/tools/k1000-lean.sh --dry-run"          # what would change (~1 min)
ssh k1000 "sudo /opt/k1000-src/tools/k1000-lean.sh --only desktop"     # runs as a unit, ends with the check
ssh k1000 "/opt/k1000-src/tools/k1000-lean.sh --check"                 # before ANY power cycle
# power-cycle, check the box, then: --only services, --only timers, --only swap, --only boot
```

| Option | Effect |
|---|---|
| *(none)* | Every step but `firmware`, in order |
| `--only a,b` | Some steps only |
| `--dry-run` | Prints what would change; removals are still simulated and checked |
| `--check` | Read-only: is the card safe to power-cycle? Exit 1 = **no**, and it prints what to fix |
| `--list` | The steps |

Like `bootstrap.sh`, a real run detaches as a unit (`k1000-lean-<time>`). Its log is
`/var/backups/k1000-lean/run-<time>.log`, its exit status `run-<time>.rc`. After an interrupted
run, run `--check` before anything else. `K1000_LEAN_FOREGROUND=1` runs in place.

After each power cycle: SSH answers, the engine is Running, Main is back at its saved level, the
panel shows K1000. Measured on the first box: Main back at its level 12.4 s on the kernel's clock
(15.5 s with the desktop image). Counted from power-on, firmware included, with every boot lever
(box/README.md « Boot to sound »): 13.5 s. `sudo /opt/k1000-src/tools/k1000-boottime.sh` times
any boot this way.

## 8. The spare card

A second card made with §2–§5 is the spare. To make it a drop-in replacement for the main card,
copy these from the main card (they are not in the repository):

| File | Why |
|---|---|
| `/etc/k1000/k1000.yaml` | The profile pin and the confirmed knob map |
| `/var/lib/k1000/state.json` | The tune (system EQ, trims, delays) and the levels. Copy it with **both** services stopped (below) |
| The phone certificate | `K1000_TLS_DIR`, as in §5 |
| `/etc/k1000/ap.psk` | The Wi-Fi password. Without it the spare generates its own and every phone must scan the QR again. Back it up with the other files ([chapter 7](07-maintenance.md#the-spare-card)); on the spare: `scp ~/k1000-backup/ap.psk k1000:/tmp/ && ssh k1000 'sudo install -o root -g k1000 -m 0640 /tmp/ap.psk /etc/k1000/ap.psk && rm /tmp/ap.psk && sudo /opt/k1000/net/setup-ap.sh'` (it keeps an existing password). A secret: never in the repository |

**Copying `state.json`.** Stopping k1000 alone is not enough. If the engine is still playing when
k1000 starts again, k1000 keeps the engine's values and saves them over the file you copied. Only
an engine that starts fresh (muted at −40 dB) gets the saved state pushed into it. With the spare
card in the box and the main card's files backed up on the laptop
([chapter 7](07-maintenance.md#the-spare-card)):

```sh
scp ~/k1000-backup/k1000.yaml ~/k1000-backup/state.json k1000:/tmp/
ssh k1000 'sudo systemctl stop k1000 camilladsp &&
  sudo install -o root -g root -m 0644 /tmp/k1000.yaml /etc/k1000/k1000.yaml &&
  sudo install -o k1000 -g k1000 -m 0644 /tmp/state.json /var/lib/k1000/state.json &&
  sudo systemctl start k1000'
```

Starting k1000 starts the engine too (or power-cycle instead). The engine starts fresh and muted,
k1000 pushes the copied tune, then ramps in (or stays muted, if the copy was saved muted). Expect
silence while the services are stopped: between shows only.

Then swap the cards once at the bench, with the speakers off, and check the box comes up as the
main card did. The laptop's SSH will warn that the host key changed: each card has its own.

## 9. Rollback

`install.sh` saves the running version to `/opt/k1000.prev` before each update. Each update
rewrites it, so copy it out first:

```sh
ssh k1000 'sudo rm -rf /tmp/k1000-rollback && sudo cp -a /opt/k1000.prev /tmp/k1000-rollback && sudo /tmp/k1000-rollback/install.sh --start'
ssh k1000 'cat /opt/k1000/REVISION'     # e.g. "e3a3deb (reinstalled from /tmp/k1000-rollback)"
```

Or deploy an older commit from the laptop: `git checkout <good-commit>`, then the update commands
in [chapter 7](07-maintenance.md#updating) with `--start`.

`--start` drops the audio for about a second: between shows only. Settings
(`/etc/k1000/k1000.yaml`) and the sound state are not touched by a rollback.

## 10. After the install

- SETUP › BOX › Knob test and `controls:` ([chapter 3](03-configuration.md#5-controls)).
- The M4 bring-up: [`docs/05-m4-day.md`](../05-m4-day.md).
- The phone, if used: [`docs/06-phone.md`](../06-phone.md).
- A soak test before the first show ([chapter 7](07-maintenance.md#soak-test)).
- The tune: [chapter 5](05-tuning.md).
