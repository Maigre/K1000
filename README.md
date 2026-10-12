# K1000

A portable, compact **head DSP** for a mobile sound rig: 2× Meyer USW subs + 2–4× Meyer UPA-1P
tops (self-powered, self-processed boxes). K1000 sits between the source and the boxes:

```
 stereo line in ─┐                           ┌─► Sub L / Sub R
 (opt. 2nd stereo├─► K1000 ─ balance · X-over ├─► Top L / Top R
  or mics)      ─┘   delay · EQ · live notch  └─► (opt.) Top 2 L / R
                     ▲
          screen + knobs  and/or  phone / tablet
```

Goals: sub/top balance · crossover pre-filtering to spare the boxes · delay/phase alignment ·
system EQ (measured when possible) · **fast live correction** on a system that moves from place to
place, from a portable surface.

## Status

**Idea / review phase** (started 2026-10-12). Nothing is decided yet.

## Layout

| Path | What |
|---|---|
| `docs/requirements.md` | What K1000 must do, as currently understood, and the open questions |
| `docs/01-approaches-review.md` | First review of buy / build approaches |
| `docs/02-plan-to-sunday.md` | v0 plan for the first gig (2026-10-18) |
| `DECISIONS.md` | Decision log |
| `ROADMAP.md` | Phases and progress |

## Licence

K1000 is free software: you can redistribute it and/or modify it under the terms of the **GNU
General Public License, version 3 or (at your option) any later version** — see [`LICENSE`](LICENSE).

Exception: `box/overlays/k1000-zynscreen.dts` is derived from the Linux kernel's `piscreen2r`
device-tree overlay and stays under **GPL-2.0**. It compiles to a separate firmware overlay and is
never linked with the GPLv3 code.

Third-party software K1000 installs but does not ship: CamillaDSP and CamillaGUI (GPL-3.0),
websockets, numpy, PyYAML, Pillow, qrcode, smbus2 (BSD/MIT-style), libgpiod (LGPL-2.1+).
