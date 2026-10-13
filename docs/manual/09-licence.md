# 9 — Licence

## K1000

K1000 is free software: you can redistribute it and/or modify it under the terms of the **GNU
General Public License as published by the Free Software Foundation, version 3 or (at your option)
any later version** (SPDX: `GPL-3.0-or-later`). The full text is in [`LICENSE`](../../LICENSE).
Every source file carries an SPDX line saying so (the PWA manifest, plain JSON, cannot carry
one). Decision: D-008 in [`DECISIONS.md`](../../DECISIONS.md).

**One exception.** `box/overlays/k1000-zynscreen.dts` is derived from the Linux kernel's
`piscreen2r` device-tree overlay and stays under **GPL-2.0**. It compiles to a separate firmware
overlay, loaded by the Pi's boot firmware, and is never linked with the GPLv3 code.

## Third-party software

K1000 installs these but does not ship them: nothing third-party is vendored in the repository.
They come from Debian (apt) or from the projects' own releases. The licences below were checked
on the box for D-008.

| Component | Licence | How K1000 uses it |
|---|---|---|
| CamillaDSP | GPL-3.0 | The audio engine, a separate program driven over its websocket |
| CamillaGUI | GPL-3.0 | The laptop editor, a separate program |
| websockets (Python) | BSD-3-Clause | The engine client and the phone page's server |
| numpy | BSD-3-Clause | The EQ curve maths, the screen's pixel conversion |
| PyYAML | MIT | Settings and engine configs |
| smbus2 | MIT | The knob board (I²C) |
| Pillow | HPND | Drawing the screen |
| qrcode (Python) | BSD | The QR codes on SETUP › PHONE › Phone |
| python3-libgpiod | LGPL-2.1-or-later | The knob board's interrupt lines |
| DejaVu fonts | Bitstream Vera licence (a free font licence; Debian's `fonts-dejavu-core`) | The screen's fonts, used from the system |

**Not used, on purpose:** `python3-smbus`. Its C module is GPL-2.0-only, which is not compatible
with GPL-3.0-or-later, so K1000 uses `smbus2` (same API, MIT) instead.

The operating system (Raspberry Pi OS / Debian) and the system tools K1000 installs or configures
(hostapd, dnsmasq, nftables, alsa-utils, i2c-tools, device-tree-compiler…) are separate programs
under their own licences; K1000 runs them, it does not link them.

## Trademarks

Meyer Sound, UPA-1P, USW-1P and LD-2 are names of Meyer Sound Laboratories. MOTU and M4
are MOTU's. Zynthian, Raspberry Pi and HiFiBerry belong to their owners. K1000 is an independent
project, not affiliated with or endorsed by any of them. The names are used only to say what K1000
was built and tested with.

## No warranty

As the licence says (sections 15 and 16), K1000 is provided **"as is", without warranty of any
kind**, and in no event is any author or copyright holder liable for damages arising from its use.

## Driving a PA: your responsibility

K1000 drives loudspeakers that can damage hearing and themselves. Read this before using it:

- **It runs on a beta engine** (CamillaDSP 5.0.0-beta.1) and is a v0. Software fails. The design
  aims at failing silent, not loud, but that is a design aim, not a guarantee.
- **Keep the bypass ready**: the cables that fed the speakers before K1000, in the case, labelled
  ([chapter 6](06-troubleshooting.md#4-the-bypass)).
- **Speakers on last, off first.** Do the thump check with your own rig before trusting the power
  order ([chapter 5](05-tuning.md#2-power-order-and-the-thump-check)).
- **K1000 has no output limiter in v0.** It relies on the speakers' own protection. Speakers or
  amplifiers without it need their own.
- **Measurement noise is loud.** Hearing protection for everyone in the room, and start low.
- **Sound levels are regulated** in many places (venues, outdoor events). Meeting those limits is the
  operator's job, not the software's.
- **The phone page and CamillaGUI are remote controls.** Physical access to the box (the Wi-Fi
  password on its screen) is the authorisation for the phone; CamillaGUI has no password at all and
  must only ever be on a wired network you control.
