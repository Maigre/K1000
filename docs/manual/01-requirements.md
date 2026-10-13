# 1 — Requirements

What you need to build and run a K1000, and what it was designed around.

## Hardware

| Item | What | Notes |
|---|---|---|
| Computer | **Raspberry Pi 4 Model B** | The author's box is a Rev 1.4 with 8 GB. Smaller Pi 4s are untested. A Pi 5 is untested (the screen overlay and the GPIO lines were only proven on a Pi 4) |
| Screen and knobs | **Zynthian V4 case** | ZynScreen 3.5″ (ILI9486 on SPI, 480×320, backlight on GPIO22). Four push-encoders and four buttons on an MCP23017 at I²C bus 1, address 0x20, interrupts on GPIO27 (INTA) and GPIO4 (INTB). The resistive touch is not used |
| Audio, for the PA | **MOTU M4** (profile `m4`) | USB class-compliant, bus-powered from the Pi. 2 line inputs + 2 mic/instrument inputs, 4 balanced TRS line outputs. The M4 facts in the code are research until the bring-up in [`docs/05-m4-day.md`](../05-m4-day.md) is done on a real unit |
| Audio, bench only | HiFiBerry DAC+ADC Pro inside the Zynthian (profile `dev`) | 2 in / 2 out. Never wired to the speakers. Never used together with the M4 (two clocks would drift) |
| Power | A 5.1 V / 3 A USB-C supply for the Pi | The M4 draws its power from the Pi's USB. Check `vcgencmd get_throttled` reads `0x0` with the M4 and the screen on |
| SD cards | **Two**: the main card and a spare | 16 GB or more, a known brand. After the lean step the system uses about 1.6 GB (`/usr`). Keep the Zynthian's original card untouched as a fallback |
| Network for the install | Ethernet to a network with internet | Only for the install and updates |
| Measurement (optional) | A measurement mic and a laptop with REW or Open Sound Meter | For the system EQ at the tune ([chapter 5](05-tuning.md)) |
| Phone (optional) | An Android phone with Chrome | The phone page is gated by a rig test: [`docs/06-phone.md`](../06-phone.md) |

**Equivalent panels.** The code expects exactly the pins above (the defaults of `ZynthianControls`
in `box/k1000/hw/mcp23017.py`, the overlay `box/overlays/k1000-zynscreen.dts`). Another screen
needs its own device-tree overlay. Another encoder board needs code changes. Which physical knob
and button is E1–E4 and B1–B4 is configuration (`controls:`, [chapter 3](03-configuration.md)),
found with SETUP › Knob test.

## Cables

| Run | Cable | Count |
|---|---|---|
| M4 outputs → speakers | **Balanced TRS (male) → XLR (male)** | 4 |
| Source → M4 LINE IN 3/4 (rear) | Balanced XLR (female) → TRS (male), or what the source needs | 2 |
| M4 → Pi | USB-C → USB-A, short, into a black (USB 2) port, strain-relieved | 1 |
| Loop test (M4 bring-up) | TRS → TRS | 1 |
| Bypass kit | The cables that fed the speakers before K1000 | as before |

- **Never TS jacks** on the outputs: the M4 outputs are balanced.
- **Never the M4's RCA outputs**: per the M4 research in `docs/05-m4-day.md`, they top out at about
  +9.5 dBu and are not the balanced path.
- **The program goes into LINE IN 3 and 4** (rear, balanced TRS), not into the front combo inputs 1
  and 2: those are mic preamps.
- Label every cable end: OUT1 TOP L · OUT2 TOP R · OUT3 SUB L · OUT4 SUB R; IN L (LINE IN 3), IN R
  (LINE IN 4).

## The speakers it was designed for

| Box | Qty | Relevant facts (from Meyer's documentation, as summarised in [`docs/01`](../01-approaches-review.md) §1) |
|---|---|---|
| Meyer **UPA-1P** | 2–4 | Self-powered 12″ + horn. Operating range about 60 Hz–18 kHz. Balanced XLR input + loop, pin 2 hot. Own crossover, correction and limiters. Limiting starts near 0 dBV |
| Meyer **USW-1P** | 2 | Self-powered 2×15″ sub. About 32–200 Hz. **Built-in low-pass**. Same input as the UPA |

Meyer designed this pair to take **the same full-range signal**. Coplanar tops and subs are in phase
across the overlap. So K1000's audio job is mostly level, polarity, delay, a gentle Lo-Cut on the
tops, and EQ. A 3rd and 4th UPA-1P ride on the loop outputs of the first two.

### How it generalises

- **Any active speakers with balanced line inputs** work. K1000 has four outputs: Sub L, Sub R,
  Top L, Top R.
- **Subs without their own low-pass**: switch on SETUP › Sub low-pass (12 dB/oct, 40–250 Hz). It
  is off by default because the USW-1P has its own, and a second one would shift the phase at the
  overlap.
- **The tops' Lo-Cut** is a 12 dB/oct high-pass, 20–300 Hz, on by default at 100 Hz.
- **No limiter in v0.** The Meyer boxes limit themselves. Speakers or amplifiers without their own
  protection need it elsewhere.
- **Other 4-out USB interfaces** are untested. The `m4` profile's device name, channel counts and
  routing can be changed in `k1000.yaml` ([chapter 3](03-configuration.md)), but the screen still
  calls it "MOTU M4".

## Software

| Part | Version | Where it comes from |
|---|---|---|
| OS | **Raspberry Pi OS Lite (64-bit)**, Debian 13 "trixie"; kernel 6.18 on the author's box | Raspberry Pi Imager |
| Python | 3.13 (the OS's) | apt |
| Engine | **CamillaDSP 5.0.0-beta.1**, linux aarch64 | `bootstrap.sh` downloads it from GitHub and checks the tarball and the binary against pinned sha256 sums |
| Laptop editor | **CamillaGUI v5.0.0-beta.2** (its binary reports "5.0.0-beta.1") | `install.sh`, pinned by sha256 |
| Python packages | python3-websockets, python3-smbus2, python3-libgpiod (gpiod 2.x API), python3-yaml, python3-pil, python3-numpy, python3-qrcode, python3-pytest | apt (`install.sh --deps` lists them) |
| System tools | rsync, alsa-utils, fonts-dejavu-core, usbutils, hostapd, device-tree-compiler, i2c-tools | apt |

Nothing is vendored: everything is installed from apt or from the projects' own releases.

## Network

| When | Needs |
|---|---|
| Install and updates | Ethernet with internet: apt, and GitHub for CamillaDSP and CamillaGUI. Offline, give the tarballs with `CAMILLADSP_TARBALL` / `CAMILLAGUI_TARBALL` ([chapter 2](02-installation.md)); apt packages still need a mirror |
| At the gig | **None.** The box makes its own Wi-Fi for the phone ("K1000", WPA2, 10.42.0.1/24). Ethernet is never touched by it |
| The tune | A laptop for the system EQ. **The `/tune` page**: only over the **box's own Wi-Fi**, while SETUP › Laptop access is open (2 h at most, [chapter 4 §5.1](04-usage.md#51-laptop-access-and-the-tune-page)) and with the phone servers on (`phone: enabled: true`). **CamillaGUI** (port 5005): over that Wi-Fi the same way, or on a **wired** network with the box. A laptop sharing its connection on the Ethernet cable must not use 10.42.0.0/24, the Wi-Fi's subnet (e.g. 10.43.0.1/24 instead) |

Ports on the box:

| Port | What | Reachable from |
|---|---|---|
| 1234 | CamillaDSP websocket | the box only (127.0.0.1) |
| 5005 | CamillaGUI | the wired network; from the box's Wi-Fi only while SETUP › Laptop access is open. **No password**: anyone who reaches it can change or stop the engine |
| 80, 443, 8080 | The phone page (443 also serves `/tune`, closed unless Laptop access is open) | the box's own Wi-Fi only (10.42.0.1) |
| 22 | SSH, keys only after `bootstrap.sh` | the wired network; from the box's Wi-Fi only while SETUP › Laptop access is open, or with `setup-ap.sh --ssh on` |
