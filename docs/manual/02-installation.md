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

```sh
rsync -a --delete --rsync-path="sudo rsync" box/ k1000:/opt/k1000-src/
ssh k1000 "sudo K1000_REVISION=$(git describe --always --dirty) /opt/k1000-src/bootstrap.sh"
```

Raspberry Pi OS Lite already has `rsync` on the box side (checked on the 2026-10-06 image).
Always give `K1000_REVISION` as above: without it, a run that brings new code from
`/opt/k1000-src` (no git there) records `unknown (from /opt/k1000-src)` in `/opt/k1000/REVISION`.
A run that syncs nothing keeps the `REVISION` already there.

What it does, each step skipped when already done:

| Step | What |
|---|---|
| 1. apt | `apt-get update` **only when** a package K1000 needs is missing, then the operator tools (`i2c-tools`) |
| 2. CamillaDSP | 5.0.0-beta.1 into `/usr/local/bin/camilladsp`. The tarball and the binary are both checked against pinned sha256 sums |
| 3. K1000 | `install.sh --boot --headless --ap`: the services, the screen overlay, I²C for the knobs, CamillaGUI, boot to the console, the phone's Wi-Fi access point and its firewall ([§6](#6-installsh-reference)). An access point kept down by `setup-ap.sh --down` (or `--no-ap`) stays down |
| 4. SSH | Keys only (`/etc/ssh/sshd_config.d/10-k1000-keyonly.conf`), checked with `sshd -t`, applied at the next boot. Skipped with a warning when no key login was seen |
| 5. Lean OS | `tools/k1000-lean.sh`: no desktop leftovers, no apt timers, swap in RAM only, a quiet boot ([§7](#7-converting-a-desktop-card)) |

It never reboots and never runs `apt upgrade`.

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
| `--help` | Prints its header |
| `CAMILLADSP_TARBALL=<path>` | Installs CamillaDSP from a local copy of the release tarball (offline) |
| `CAMILLAGUI_TARBALL=<path>` | The same for CamillaGUI (passed on to `install.sh`) |
| `K1000_REVISION=<text>` | What `/opt/k1000/REVISION` records (passed on to `install.sh`) |
| `K1000_BOOTSTRAP_FOREGROUND=1` | Runs in place instead of as a unit (for a console on the box), `k1000-lean.sh` included |

Put the variables after `sudo`, as in the command above.

| Exit | Meaning |
|---|---|
| 0 | Done. The last lines say `N changes. Now power-cycle the box`, `0 changes: this card is already a K1000`, or `0 changes; still to do: cloud-init off (below)` (after the first power cycle, §4) |
| 1 | An error, printed. Nothing after it ran |
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
3. **Run the bootstrap command once more**, then power-cycle again.

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
| *(none)* | Installs and updates the files, enables the four units (`camilladsp`, `k1000`, `camillagui`, `k1000-console`), and installs the two Laptop access units (`k1000-laptop.path`, `k1000-laptop.service`), never enabled: k1000 brings the watcher. **Starts nothing.** A changed access-point unit is only reported (`not applied; between shows: … --ap`), never copied |
| `--start` | Then (re)starts those four units. **The audio drops for about a second**, then ramps back in. Never during a show |
| `--boot` | Boot-level setup, applied at the next reboot: compiles the screen overlay into `/boot/firmware/overlays/`, adds `dtoverlay=k1000-zynscreen,rotate=270` to `config.txt`, adds `vt.global_cursor_default=0` to `cmdline.txt` (original kept as `cmdline.txt.k1000-orig`), turns I²C on for the knobs (`dtparam=i2c_arm=on`, `i2c-dev` loaded at boot) |
| `--headless` | Boots to the console instead of a desktop (`multi-user.target`), masks PipeWire/WirePlumber for all users, disables Bluetooth, printing and the NFS client/rpcbind, removes the tty1 auto-login. Applied at the next reboot. It leaves cloud-init alone (see §4) |
| `--ap` | Runs `net/setup-ap.sh`: the phone's Wi-Fi "K1000" on `wlan0` and its firewall, and installs or updates the access point's units (the only way they are). Takes effect at once. Later runs keep every earlier choice, a no-go included: after `setup-ap.sh --down` the access point stays down until `setup-ap.sh --up`. It refuses (exit 4, nothing changed) while another interface holds a 10.42.0.x address or `wlan0` is a client of another Wi-Fi |
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
| 1 | An error, printed (not root, not a `box/` tree, a bad certificate pair…) |
| 2 | Unknown option |
| 3 | CamillaGUI failed. Everything else is installed |
| 4 | The access point failed. Everything else is installed |
| 5 | Both failed |

What it never does: overwrite `/etc/k1000/k1000.yaml` once it exists, touch the live state in
`/var/lib/k1000` (except removing an empty `coeffs/` folder left there by an older version), or
reboot.

It also keeps the previous version in `/opt/k1000.prev` before changing anything (rollback, §9),
installs a hardware watchdog setting (`RuntimeWatchdogSec=15`) and makes the journal persistent
(32 MB cap). The CamillaDSP binary itself comes from `bootstrap.sh`; `install.sh` only warns when
it is missing or not 5.x.

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

Its guard: every removal is simulated first and its plan saved in `/var/backups/k1000-lean/`. A
plan that would remove anything K1000, the boot, SSH, the network, the Wi-Fi, systemd, libc, Python
or ALSA needs is **refused** (exit 1, nothing removed). Boot files are backed up before any edit.

**One change set per power cycle**, and the check before each one:

```sh
rsync -a --delete --rsync-path="sudo rsync" box/ k1000:/opt/k1000-src/
ssh k1000 "sudo /opt/k1000-src/tools/k1000-lean.sh --dry-run"          # what would change (~1 min)
ssh k1000 "sudo /opt/k1000-src/tools/k1000-lean.sh --only desktop"     # runs as a unit, ends with the check
ssh k1000 "/opt/k1000-src/tools/k1000-lean.sh --check"                 # before ANY power cycle
# power-cycle, check the box, then: --only services, --only timers, --only swap, --only boot
```

| Option | Effect |
|---|---|
| *(none)* | Every step, in order |
| `--only a,b` | Some steps only |
| `--dry-run` | Prints what would change; removals are still simulated and checked |
| `--check` | Read-only: is the card safe to power-cycle? Exit 1 = **no**, and it prints what to fix |
| `--list` | The steps |

Like `bootstrap.sh`, a real run detaches as a unit (`k1000-lean-<time>`). Its log is
`/var/backups/k1000-lean/run-<time>.log`, its exit status `run-<time>.rc`. After an interrupted
run, run `--check` before anything else. `K1000_LEAN_FOREGROUND=1` runs in place.

After each power cycle: SSH answers, the engine is Running, Main is back at its saved level, the
panel shows K1000. Measured on the first box: Main back at its level 12.4 s after power-on (15.5 s
with the desktop image).

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
