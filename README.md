# K1000

A compact head processor for a self-powered PA, built around a Raspberry Pi 4 and
[CamillaDSP](https://github.com/HEnquist/camilladsp). It sits between the source and the speakers
and does the system work they do not do themselves: sub/top balance, a Lo-Cut on the tops, delay
and polarity between subs and tops, a system EQ set at the tune, and a separate live EQ for fast
corrections during the show. It was built for a mobile rig of Meyer USW-1P subs and UPA-1P tops
that moves from place to place, where the room changes and a measured tune is the exception. So
the point is **fast, safe live correction from a small surface**: four knobs and a 3.5″ screen,
and a phone on the box's own Wi-Fi.

*Photo: to come.*

```
 stereo line in ──► MOTU M4 ──► K1000 (Pi 4 + CamillaDSP) ──► M4 outs ──► Sub L / Sub R
                                  trim · system EQ · live EQ          └──► Top L / Top R
                                  split · Lo-Cut · delay · polarity
                                  balance · master · mute
                                  ▲
                      4 knobs + 4 buttons + screen   ·   phone (own Wi-Fi)
```

## What it does today (as built)

- **A fixed-slot DSP pipeline**: input trim, 8-band system EQ, a live EQ layer (4 bells + low and
  high shelves) on top of it, the sub/top split (optional mono subs), polarity and delay per group
  (whole samples at 48 kHz), a 12 dB/oct Lo-Cut on the tops, an optional sub low-pass, sub/top
  balance, four output trims, master and mute. Every show-time move is a parameter write or a
  ramped fader, never a pipeline rebuild.
- **Knobs and screen** on a Zynthian V4 case (live UI v1, D-009): **SHOW**, the front page, with
  the master and the live EQ drawn over the program spectrum, and a 3-gesture notch helper on the
  spectrum's peak; **ALIGN** (balance, and one signed sub/top delay and the Lo-Cut behind a
  lock); **SETUP** (the rest, live-EQ reset with undo, Lo-Cut on/off, the spectrum switch, laptop
  access for the tune).
  Meters with clip warnings, alerts in the status bar, a master-up guard (+6 dB per 2 s at most).
- **Audio safety**: boots muted, ramps in only once audio flows, comes back muted if it was switched
  off muted, saves a mute at once, never raises a cut or releases a mute made elsewhere, recovers a
  torn state file muted. Services restart on any crash; a hardware watchdog covers a hung system.
- **Two device profiles**: the MOTU M4 for the PA (pinned, no silent fallback) and the Zynthian's
  HiFiBerry board for the bench.
- **A phone page** on the box's own Wi-Fi access point, installable over HTTPS through a
  name-constrained local CA, driving the show controls: master, balance, Lo-Cut, the live EQ and
  mute (setup stays on the box). Built and tested in software; not yet proven with a real phone on
  the box ([`docs/06`](docs/06-phone.md)).
- **The tune from a laptop**: a `/tune` page over the box's Wi-Fi (SETUP › Laptop access, 2 hours)
  that imports a REW filter export into the system EQ, and CamillaGUI there or on the wired
  network.
- **Ops tools**: a bootstrap from a fresh Raspberry Pi OS Lite card, a lean-OS converter, a soak
  test with a summary, an M4 bring-up check, a knob trace. About 12 s from power-on to the saved
  level on the author's box.

## Status

**v0, on a beta engine** (CamillaDSP 5.0.0-beta.1). It runs on the author's box, where the live
UI v1 was accepted; it has not yet played a show. The MOTU M4 profile and the phone are being proven
on the rig; the [roadmap](ROADMAP.md) has the details. Use it with the bypass cables in the case.

## Documentation

| | |
|---|---|
| [**User manual**](docs/manual/00-index.md) | Requirements, installation, configuration, usage, tuning, troubleshooting, maintenance, development |
| [Quick start](docs/13-quick-start.md) | One printed page for someone who has never seen the box |
| [Operator card](docs/04-operator-card.md) | One printed page for the operator |
| [`tools/make-print.sh`](tools/make-print.sh) | Renders those two pages to PDF (`tools/make-print.sh [outdir]`, default `print/`) |
| [Box ops reference](box/README.md) | Deploy, everyday commands, rollback, tests |
| [Architecture](docs/03-v0-architecture.md) | The build contract |
| [Decisions](DECISIONS.md) | Why it is built this way |
| [Roadmap](ROADMAP.md) | Phases, gates, progress |
| [Requirements](docs/requirements.md) and [approaches review](docs/01-approaches-review.md) | What it must do, and the options that were weighed |

## Install

On a Raspberry Pi OS Lite (64-bit) card made with Raspberry Pi Imager (hostname `k1000`, user
`k1000`, SSH with your key only, Wi-Fi left empty), the box on Ethernet with internet and in the laptop's SSH config
as `k1000`, from the laptop:

```sh
git clone https://github.com/Maigre/K1000 && cd K1000
rsync -a --delete --rsync-path="sudo rsync" box/ k1000:/opt/k1000-src/
ssh k1000 "sudo K1000_REVISION=$(git describe --always --dirty) /opt/k1000-src/bootstrap.sh"
# power-cycle, run the bootstrap line once more, power-cycle again
ssh k1000 'systemctl is-active camilladsp k1000 camillagui'
```

The full procedure, and what to do next (profile, knob map, the M4, the phone):
[installation](docs/manual/02-installation.md).

## Licence

K1000 is free software: you can redistribute it and/or modify it under the terms of the **GNU
General Public License, version 3 or (at your option) any later version**. See [`LICENSE`](LICENSE).

Exception: `box/overlays/k1000-zynscreen.dts` is derived from the Linux kernel's `piscreen2r`
device-tree overlay and stays under **GPL-2.0**. It compiles to a separate firmware overlay and is
never linked with the GPLv3 code.

Third-party software K1000 installs but does not ship: CamillaDSP and CamillaGUI (GPL-3.0),
websockets, numpy, PyYAML, Pillow, qrcode, smbus2 (BSD/MIT-style), libgpiod (LGPL-2.1+). Details,
and the no-warranty and PA-safety notes: [licence](docs/manual/09-licence.md).
