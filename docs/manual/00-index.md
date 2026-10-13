# K1000 user manual

K1000 is a small head processor for a self-powered PA. It sits between the source (a DJ mixer, a
console, a player) and the speakers, and does the system work the speakers do not do themselves:

- the sub/top balance;
- a Lo-Cut on the tops;
- delay and polarity between subs and tops;
- a system EQ, set once at the tune;
- a separate live EQ for fast corrections during the show, which never touches the tune.

The hardware is a Raspberry Pi 4 running [CamillaDSP](https://github.com/HEnquist/camilladsp), in a
Zynthian V4 case (a 3.5″ screen, four push-encoders, four buttons), with a MOTU M4 USB interface
for the audio. A phone can join the box's own Wi-Fi and drive the show controls: master, balance,
Lo-Cut, the live EQ and mute.

It was built for one rig: two Meyer USW-1P subs and two to four Meyer UPA-1P tops, all
self-powered, on a mobile system that moves during a day. [Chapter 1](01-requirements.md) says
how far it generalises.

## Who this manual is for

- **The operator.** You know a PA, gain structure and an SSH terminal. You do not need Python.
- **Anyone who finds the repository** and wants to build one. The hardware list, the install, the
  configuration and the code layout are all here.

## Status

**v0, on a beta engine** (CamillaDSP 5.0.0-beta.1, decision D-004 in
[`DECISIONS.md`](../../DECISIONS.md)), with the live UI v1 (D-009), the knob fixes and the phone
page built and accepted on the author's box. The buttons and SETUP follow D-013 (B1 PAGE · B2
BAND/UNLOCK · B3 NOTCH · B4 MUTE; SETUP by speaker floor), built on 2026-10-13: a box not yet
updated still runs D-009's buttons (B2 mute, B3 band and unlock, B4 notch). It has not yet played a
show; the MOTU M4 and the phone are being proven on the real rig. Where this manual points to a
numbered doc (`docs/0X`), that doc holds the design detail and the history; this manual says how the
box works today.

## Chapters

| # | Chapter | What is in it |
|---|---|---|
| 1 | [Requirements](01-requirements.md) | Hardware, cables, speakers, software versions, network |
| 2 | [Installation](02-installation.md) | A fresh card with `bootstrap.sh`, `install.sh` flags, converting a desktop card, the spare card, rollback |
| 3 | [Configuration](03-configuration.md) | Every key of `/etc/k1000/k1000.yaml` (profiles, knobs, spectrum, phone), what needs a restart, the state files |
| 4 | [Usage](04-usage.md) | Power on to sound, SHOW (master, live EQ, notching, spectrum), ALIGN, SETUP (by speaker floor, choose-then-set, incl. Laptop access and `/tune`), mute, alerts, the phone, end of night, bypass |
| 5 | [Tuning a Meyer rig](05-tuning.md) | Power order, gain structure, sub alignment, Lo-Cut, system EQ with a mic, live notching |
| 6 | [Troubleshooting](06-troubleshooting.md) | Every screen message, common faults, the bypass, logs |
| 7 | [Maintenance](07-maintenance.md) | Updates, rollback, the spare card, the certificate, SD cards, power cuts, soak test |
| 8 | [Development](08-development.md) | Repo layout, architecture, tests, the bench console, the knob trace, printing the cards, coding rules |
| 9 | [Licence](09-licence.md) | GPL-3.0-or-later, third-party licences, no warranty, PA safety |

Other references in the repository:

- [`docs/13-quick-start.md`](../13-quick-start.md): one page for someone who has never seen the box.
- [`docs/04-operator-card.md`](../04-operator-card.md): the one-page card for the operator.
- Both print to PDF with `tools/make-print.sh` ([chapter 8](08-development.md#printing-the-cards)).
- [`box/README.md`](../../box/README.md): the ops reference on the box (`/opt/k1000/README.md`).
- [`docs/03-v0-architecture.md`](../03-v0-architecture.md): the build contract.
- [`DECISIONS.md`](../../DECISIONS.md) and [`ROADMAP.md`](../../ROADMAP.md).

## Safety principles

1. The engine always starts **muted at −40 dB**. K1000 then ramps it to the saved level over about 3 s, and only once audio really flows.
2. The box comes back **the way it was switched off**: muted if it was muted, otherwise it ramps in by itself.
3. **Hold B4 = mute / unmute**, on every page (the long press fires at 0.8 s). A short press only shows a hint.
4. K1000 never releases a mute it did not make, and never raises by itself a cut made from outside (CamillaGUI).
5. **Bypass is the old cabling**: the source straight into the speakers, as before K1000. Keep those cables in the case.
6. Speakers **on last, off first**.
