# K1000 box: deploy and run

Everything that runs on the K1000 (the Raspberry Pi 4 in the Zynthian V4 case). The design is in
`docs/03-v0-architecture.md`, the gig plan in `docs/02-plan-to-sunday.md`.

## What runs

| Unit | What | Depends on |
|---|---|---|
| `camilladsp.service` | the audio engine, CamillaDSP 5.0.0-beta.1, user `k1000`, real-time threads | nothing (audio never waits for the UI) |
| `k1000.service` | `python3 -m k1000` in `/opt/k1000`: knobs, screen, state, persistence | wants the engine, copes when it is down |
| `camillagui.service` | CamillaGUI v5 beta.2 on port 5005 (phone compact view, laptop editor) | nothing; lowest CPU priority, sandboxed |

The order at power-on:

1. The engine starts **muted at −40 dB** from `/var/lib/k1000/active.yml`.
2. k1000 connects to the engine, checks the config, unmutes and ramps Main up to the saved level over about 3 s.
3. The phone/laptop GUI comes up next to them.

- If `active.yml` is missing or broken, the engine waits for a valid one, with no websocket
  yet. k1000 writes it, and the engine starts on it within a second. The journal says
  `waiting for a valid /var/lib/k1000/active.yml` once, then every 30 s.
- The engine always runs from that file, never in CamillaDSP's wait mode (`-w`). So whenever
  processing stops, the process exits and systemd starts it again, muted, from `active.yml`.
- If the audio interface is missing (M4 unplugged), the engine exits at start and retries every
  1–2 s, forever. It starts by itself once the interface is there. While it is missing there is
  no websocket: k1000 shows NO DEVICE / ENGINE DOWN.
- If `/var/log/k1000` cannot be written, the engine still plays and logs to the journal instead.
- All three units restart 1–2 s after any crash, with no retry limit.
- The hardware watchdog reboots the box if systemd itself hangs for 15 s.

| Path | What |
|---|---|
| `/opt/k1000/` | a mirror of `box/` (package `k1000/`, `tests/`, `tools/`, units, this README); `REVISION` says what is deployed |
| `/opt/k1000.prev/` | the version before the last update (rollback) |
| `/etc/k1000/k1000.yaml` | Thomas's settings: profile pin, knob mapping. Copied once from `box/etc/`, **never overwritten** by install.sh |
| `/var/lib/k1000/state.json`, `active.yml` | live state and the generated engine config (written by k1000, atomically) |
| `/var/log/k1000/` | `camilladsp_rCURRENT.log` (+ 3 rotated), `soak.log` |
| `/opt/camillagui/` | the GUI binary; `config/` comes from `box/camillagui/`; `files/` is the GUI's empty, read-only config folder |
| `/etc/systemd/system/{camilladsp,k1000,camillagui}.service` | from `box/systemd/` |
| `/etc/systemd/system.conf.d/50-k1000-watchdog.conf` | `RuntimeWatchdogSec=15` |

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

**Fresh SD card, once** (both apply at the next reboot):

```sh
ssh k1000 "sudo /opt/k1000-src/install.sh --boot --headless --start && sudo reboot"
```

- `--boot`:
  - compiles `overlays/k1000-zynscreen.dts` with `dtc -@` into `/boot/firmware/overlays/`
  - makes sure `config.txt` has `dtoverlay=k1000-zynscreen,rotate=270`
  - adds `vt.global_cursor_default=0` to `cmdline.txt`, which stops the console cursor blinking
    on the ZynScreen. The original file is kept as `cmdline.txt.k1000-orig`.
- `--headless`:
  - boots to the console instead of the desktop: `multi-user.target`, so no labwc session
  - masks PipeWire/WirePlumber for all users, so nothing but CamillaDSP ever opens the audio
    interfaces
  - undo with `sudo systemctl set-default graphical.target` and
    `sudo systemctl --global unmask pipewire.socket pipewire.service pipewire-pulse.socket pipewire-pulse.service wireplumber.service filter-chain.service`

## Everyday commands (on the box)

```sh
systemctl status camilladsp k1000 camillagui      # all three at a glance
journalctl -u k1000 -f                            # control service log (live)
journalctl -u camilladsp -n 50                    # engine start/stop, "waiting for a valid active.yml", ALSA errors
tail -f /var/log/k1000/camilladsp_rCURRENT.log    # engine log (xruns, device errors, config loads)
sudo systemctl restart k1000                      # restart the control surface (audio keeps playing)
sudo systemctl restart camilladsp                 # restart the engine: ~1 s of silence, comes back muted, k1000 ramps in
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

## Phone and laptop: CamillaGUI

- Phone (compact view): `http://<box-ip>:5005/gui/index.html?compactview`
  - **Levels only**: Main volume, mute and the meters. They are runtime commands, so they never
    send a config and cannot undo anything done elsewhere. k1000 follows the Main fader.
  - Live EQ, Lo-Cut and sub delay are on the knobs. The phone has no sliders for them on
    purpose: in CamillaGUI v5 beta.2 every slider move sends the whole config as the page loaded
    it, which would put back old knob values (a feedback notch), the pre-tune system EQ, or even
    the old audio device.
- Laptop (full editor, for the system EQ at the tune): `http://<box-ip>:5005/`
  - **Press "Fetch from DSP" (or reload the page) right before editing.** Apply sends the whole
    config the page holds, so a page opened before knob moves (or before a profile change) puts
    the old values, or the old device, back.
  - Edit, then **"Apply to DSP"**. k1000 adopts the change and saves it in `state.json`.
  - "Save to file" (and the save half of "Apply and save") fails on purpose
    (`Read-only file system`): the GUI cannot write any file. k1000 owns `active.yml` and the state.
- The sub/top balance (Aux1/Aux2 faders) is not in the compact view.
  - On the box: MAIN page, E2.
  - In the normal view: the side panel's Aux faders.
- After an engine restart, the GUI's first action can fail with 503. The next one works.
- The GUI has no password and listens on every interface: anyone on the same network can change
  or stop the engine.

## Knob test

- **On the screen:** SETUP page → "Knob test" (HWTEST). Every knob or button event shows with its
  physical pin.
- **In a terminal:** the monitor needs the MCP23017 and GPIO lines, which the service holds, so
  stop the service first:

```sh
sudo systemctl stop k1000
cd /opt/k1000 && python3 -m k1000.hw.mcp23017 --monitor        # --raw for every pin change, --selftest for the interrupt lines
sudo systemctl start k1000
```

## Soak test (overnight, and the 2-hour program run)

```sh
sudo systemd-run --unit=k1000-soak --uid=k1000 /opt/k1000/tools/k1000-soak.sh
tail -f /var/log/k1000/soak.log
sudo systemctl stop k1000-soak      # in the morning
```

The tool writes one line every 10 s:

```
2026-10-15T23:00:10+02:00 state=Running load=1.23 buffer=0 clipped=0 xruns=0 xruns_all=0 rate=48000 restarts=0 temp_c=52.1 throttled=0x0
```

Read it for:

- `xruns`/`xruns_all` above 0: dropout lines in the engine log (playback underruns, capture
  overruns, stalled device, device errors). A stall counts twice (entering and leaving).
- `restarts` going up: the engine stopped (a crash, or a lost or missing device) and systemd
  restarted it.
- `state` other than `Running` (`Paused` = silent input, `DOWN` = no engine websocket).
- `throttled` not `0x0`: under-voltage or heat. The M4 is bus-powered from the Pi.

Summary in the morning:

```sh
grep -v 'xruns=0 xruns_all=0' /var/log/k1000/soak.log | head        # any xrun at all?
awk '{print $2}' /var/log/k1000/soak.log | sort | uniq -c           # time spent in each state
```

## Roll back

- **Code or units:** reinstall the previous version. `/opt/k1000.prev` is rewritten by each
  update, so copy it out first.

  ```sh
  sudo rm -rf /tmp/k1000-rollback && sudo cp -a /opt/k1000.prev /tmp/k1000-rollback && sudo /tmp/k1000-rollback/install.sh --start
  cat /opt/k1000/REVISION
  ```

  Or from the laptop: `git checkout <good-commit>`, then deploy as above with `--start`.
- **Settings:** `/etc/k1000/k1000.yaml` is never touched by install.sh. Fix it by hand, then
  `sudo systemctl restart k1000`.
- **Sound state:** `/var/lib/k1000/state.json` holds the levels and EQ.
  - Live EQ: reset with B4 (long press).
  - If the state file itself is bad, stop k1000, move `state.json` away and start k1000. It comes
    up on defaults, muted, then ramps in.
- **The show itself:** the bypass kit. Re-patch the source straight into the Meyers
  (`docs/02-plan-to-sunday.md` §5).

## Tests

```sh
cd /opt/k1000 && python3 -m pytest -q -p no:cacheprovider          # unit tests (no hardware)
cd /opt/k1000 && python3 -m pytest -q -p no:cacheprovider -m hw    # hardware tests (opt-in; stop k1000 first)
```
