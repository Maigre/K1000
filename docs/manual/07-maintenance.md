# 7 — Maintenance

Keeping a K1000 current, recoverable and trustworthy between shows. Nothing in this chapter is
done during a show.

## Updating

From the repository on the laptop (`ssh k1000` set up as in [chapter 2](02-installation.md#1-before-you-start)):

```sh
git pull
rsync -a --delete --rsync-path="sudo rsync" box/ k1000:/opt/k1000-src/
ssh k1000 "sudo K1000_REVISION=$(git describe --always --dirty) /opt/k1000-src/install.sh"
```

- `install.sh` installs and enables, but **starts nothing**. The running services keep the old
  code until they restart. To switch over at once, add `--start`: the audio drops for about a
  second, then ramps back in.
- Re-running is always safe; with nothing new it ends with `0 changes`.
- `/opt/k1000/REVISION` says what is deployed (`git describe` of the laptop's checkout, `-dirty` if
  it had uncommitted changes).
- Before changing anything, `install.sh` copies the running version to `/opt/k1000.prev`.
- Your settings (`/etc/k1000/k1000.yaml`) and the sound state (`/var/lib/k1000/`) are never
  touched by an update.
- `box/camillagui/gui-config.yml` and `camillagui.yml` **are** overwritten on the box by each
  update: change them in the repository.

After an update with `--start`, check:

```sh
ssh k1000 'systemctl is-active camilladsp k1000 camillagui; cat /opt/k1000/REVISION'
```

Then a short program run, and a soak before the next show if the update was more than a fix.

### Rollback

```sh
ssh k1000 'sudo rm -rf /tmp/k1000-rollback && sudo cp -a /opt/k1000.prev /tmp/k1000-rollback && sudo /tmp/k1000-rollback/install.sh --start'
```

Copy `/opt/k1000.prev` out first, as above: running `install.sh` from it is refused, because the
update rewrites it. `REVISION` then reads e.g. `e3a3deb (reinstalled from /tmp/k1000-rollback)`.
Or check out an older commit on the laptop and update as above with `--start`.

### The engine and the OS

- **CamillaDSP and CamillaGUI are pinned** (versions and sha256 sums in `box/bootstrap.sh` and
  `box/install.sh`). Moving to another release is a code change: the v5 betas change the config
  and websocket formats. Test it on the bench, with the test suite and a soak, never shortly
  before a show.
- **OS updates are manual**: the apt timers are off on purpose (no apt work during a show, no
  internet at gigs). Update on the bench, with time to test: `sudo apt update && sudo apt
  full-upgrade`, then `/opt/k1000/tools/k1000-lean.sh --check` **before** power-cycling, then a
  soak.

## The spare card

Keep a second card that boots into the same K1000 ([chapter 2 §8](02-installation.md#8-the-spare-card)).
Keep it current:

- After each update of the main card, update the spare the same way (swap the cards at the bench).
- After a tune, copy `state.json` to it, with **both** services stopped on the spare (stop k1000
  and camilladsp, copy, then start k1000 or power-cycle): the procedure is in
  [chapter 2 §8](02-installation.md#8-the-spare-card). Stopping k1000 alone loses the copy.
- Keep the Zynthian's original card too, untouched: it is the reference for the V4 hardware.

**Back up off the box** after every tune or settings change:

```sh
mkdir -p ~/k1000-backup && scp k1000:/etc/k1000/k1000.yaml k1000:/var/lib/k1000/state.json ~/k1000-backup/
```

The Wi-Fi password (`/etc/k1000/ap.psk`) and the phone certificate (`/etc/k1000/tls/`) are secrets:
keep them out of the repository.

## The phone certificate

The phone page is installable only over HTTPS, and Android trusts the box only through a
certificate authority installed once on each phone ([`docs/06-phone.md`](../06-phone.md) §3). The
scheme (decision D-007):

- A **local CA, name-constrained** (critical): it can vouch only for IP 10.42.0.0/24 and the name
  `k1000.home.arpa`. Even if its key leaked, it could not impersonate any other site to the phone.
- **The CA's private key never goes on the box.** Keep it offline, in your own secrets store.
- The box holds only the **leaf** certificate and key for `10.42.0.1` / `k1000.home.arpa`, and the
  CA's public certificate (served to phones at `http://10.42.0.1/k1000-ca.crt`).
- The author's current leaf is valid until **2029-01-14**.

**Expiry.** At each start, k1000 logs `phone certificate expires in N days: re-sign it` when fewer
than 60 days are left. Once it has expired, SETUP › Phone shows `phone certificate EXPIRED …`.

**Renewing the leaf** (the CA stays, so the phones need nothing new):

1. Sign a new leaf with the same CA, on the machine that holds the CA key.
2. Deploy it, then restart k1000 between shows:

   ```sh
   ssh k1000 'install -d -m 0700 /tmp/k1000-tls'
   scp k1000.crt k1000.key k1000-ca.crt k1000:/tmp/k1000-tls/
   ssh k1000 'sudo K1000_TLS_DIR=/tmp/k1000-tls /opt/k1000-src/install.sh && rm -rf /tmp/k1000-tls && sudo systemctl restart k1000'
   ```

   `install.sh` refuses a key that does not match its certificate and never copies a `ca.key`.
3. Do the same on the spare card.

**A new CA** (lost key, or a new box family) means installing the new CA certificate on every
phone again.

**An example** of such a CA and leaf with OpenSSL 3 (one way to do it; the author's own procedure is
kept privately). Run it on a trusted machine, not on the box:

```sh
# the CA, name-constrained, 10 years
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes \
  -keyout ca.key -out k1000-ca.crt -days 3650 -subj "/CN=K1000 Local CA" \
  -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -addext "nameConstraints=critical,permitted;IP:10.42.0.0/255.255.255.0,permitted;DNS:k1000.home.arpa"

# the leaf for the box, 825 days
openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes \
  -keyout k1000.key -out k1000.csr -subj "/CN=k1000.home.arpa"
printf '%s\n' 'subjectAltName=IP:10.42.0.1,DNS:k1000.home.arpa' 'basicConstraints=critical,CA:FALSE' \
  'keyUsage=critical,digitalSignature' 'extendedKeyUsage=serverAuth' > leaf.ext
openssl x509 -req -in k1000.csr -CA k1000-ca.crt -CAkey ca.key -CAcreateserial \
  -out k1000.crt -days 825 -extfile leaf.ext
openssl verify -CAfile k1000-ca.crt k1000.crt        # k1000.crt: OK
```

Only `k1000.crt`, `k1000.key` and `k1000-ca.crt` go to the box. `ca.key` stays where it was made.

## SD card care

The card is the weak part of any Pi. What K1000 already does to spare it:

| Measure | Effect |
|---|---|
| State written only when it changed, atomically | Few writes; a power cut leaves the old or the new file, never half of one |
| Swap in RAM only (zram) | No swap file on the card |
| Engine logs rotated at 256 kB, 50 kept | At most about 13 MB |
| Journal capped at 32 MB, synced every 30 s | Bounded, and it survives a power cut |
| No apt timers, no man-db | No background writes during a show |

Good practice:

- Cards from a known brand, and the spare kept current.
- Now and then: `dmesg | grep -i mmc` (I/O errors) and `df -h /`.
- A card that ever shows I/O errors is retired, not repaired.

A read-only root, which would make power cuts harmless to the filesystem, is planned for later
(ROADMAP Phase 5). Until then the box relies on atomic writes and the filesystem's journal.

## Power cuts

Pulling the plug is the normal way to switch K1000 off, and the design assumes power can go at any
moment.

| What | Behaviour |
|---|---|
| A mute, or the master turned down | Saved at once (on disk within a fraction of a second), so the box never comes back louder than it was left |
| Other edits | Saved 1 s after the last change, at most 5 s after the first. A cut inside that window loses that last edit only |
| Next power-on | The engine starts muted at −40 dB. Saved muted → stays muted (`READY`). Saved unmuted → ramps back to the saved master over about 3 s (`RAMPS IN BY ITSELF`) |
| A torn `state.json` | Rebuilt from `active.yml`, **muted** (`STATE REBUILT · CHECK MASTER`) |
| A hung system | The hardware watchdog reboots the box if systemd stops responding for 15 s |
| The journal | Kept; up to the last 30 s before the cut may be missing (critical messages are written at once) |

**End of a night: hold B2 (muted), speakers off, then K1000 off.** The next power-on stays silent
until you unmute.

The author's power-cut drill (pull the plug at show level, power on again, check the state file
survived and the box came back as it should) is tracked in [`ROADMAP.md`](../../ROADMAP.md),
Phase 1. Do the same drill once with your own box and supply.

## Soak test

Before the first show and after any significant update: hours of real program through the box,
then a summary.

```sh
sudo systemd-run --unit=k1000-soak --uid=k1000 /opt/k1000/tools/k1000-soak.sh
tail -f /var/log/k1000/soak.log                   # one line every 10 s
/opt/k1000/tools/k1000-soak.sh --summary          # afterwards: "clean", or what needs a look (exit 1)
sudo systemctl stop k1000-soak                    # "not loaded" = it had already ended (a reboot?)
```

Each line:

```
2026-01-01T23:00:10+01:00 state=Running load=1.23 buffer=224 clipped=0 xruns=0 xruns_all=0 rate=48000 restarts=0 temp_c=52.1 throttled=0x0 main_db=-20.0 muted=false k1000=active k1000_restarts=0 boot=3f2a9c1e up=8123
```

`--summary` flags:

- the soak stopped (a gap, or the last sample long before now: a reboot ends it);
- a reboot (a new `boot=` id, or `up` going down);
- `xruns` above 0: dropout lines in the engine log (underruns, overruns, a stalled device, device
  errors);
- restarts of camilladsp or of k1000;
- `state` other than `Running`, k1000 not active, Main muted, clipped samples;
- `throttled` not `0x0` (under-voltage or heat: the M4 is powered by the Pi), or the SoC at 75 °C
  or more.

Run it at the `target_level`, with the power supply and the case you will use at the show. A soak
on the bench supply proves less.

**Shorter checks:** a two-hour program run uses the same tool. The M4 bring-up has its own check
tool: `/opt/k1000/tools/k1000-m4-bringup.sh` ([`docs/05-m4-day.md`](../05-m4-day.md)).
