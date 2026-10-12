# K1000 — requirements (draft 1, 2026-10-12)

What K1000 must do. Draft 0 (first brief) → **draft 1** after Thomas's answers of 2026-10-12
(§5). Remaining assumptions are marked *(?)*.

## 1. The rig it drives

| Box | Qty | Notes |
|---|---|---|
| Meyer **USW-1P** (confirmed) | 2 | self-powered 2×15″ sub, **built-in low-pass**, balanced XLR in + loop |
| Meyer UPA-1P | 2–4 | self-powered 12″ + horn, balanced XLR in + loop; 3rd/4th box daisy-chained off the loop for now |

The boxes already carry their own amplification, crossover and protection. K1000 does **system**
processing in front of them, the job Meyer's LD-1A / LD-2 line drivers and later the Galileo did.
Meyer designed this pair to take the **same full-range signal** (see `01-approaches-review.md` §1).

## 2. Use context — what makes this not a standard PA head

- **Mobile and roaming**: the system is on wheels, changes place during a day/show, often outdoor.
- **Measurement is the exception**: a pre-show measured tune is sometimes possible (Thomas owns a
  measurement mic), often not.
- **Correction is live**: notch, shelve, rebalance subs/tops *during* the show, by ear, depending on
  where we are, how it sounds and who plays.
- Mains (230 V) is always available. **No rack** in the rig.

## 3. Functions

### Inputs
- **I1–2**: stereo line (main program) — must.
- A measurement-mic input (phantom) — should (pre-show tune).
- 2nd stereo line / mics — not needed now.

### Outputs (balanced line)
- **4 outputs: Sub L, Sub R, Top L, Top R** — enough for now (confirmed). Extra UPA-1P pairs ride on
  the loop outs. 6 outs stays a later option, not a requirement.
- Mono-sub option (sum L+R to both subs) *(?)*.

### Processing
| Where | What | Priority |
|---|---|---|
| Input | input trim, mono sum switch | must |
| Master | **master level**, mute, **sub↔top balance** as one macro control | must |
| Tops | **switchable Lo-Cut** (LD-2 style, 12 dB/oct, ~100–160 Hz) | must |
| Subs | **polarity**, **delay** (subs off-plane from tops); *no extra low-pass by default* | must |
| System EQ layer | parametric EQ set at tune time (measured or by ear), stored per preset | must |
| **Live EQ layer** | separate notch/shelf slots on top of the system EQ; one-move "reset live" | must |
| Per output | gain, mute, polarity, delay | must |
| Per output | peak limiter (keep the boxes' own limiters idle) | nice |
| Global | presets/snapshots (by place type), A/B compare | should |
| Measurement | noise out, mic in, RTA / transfer function, EQ suggestion | later |
| Live assist | spectrum view with peak markers → "notch here" in one move | later |

### Control
- **Knobs first** (confirmed): the device's own encoders + screen run the show without a phone —
  master, balance, mute, live EQ, usable in the dark and in sunlight.
- **Phone second, Android before iOS**: over Wi‑Fi, to adjust while listening from the audience area.

## 4. Non-functional

- **Compact**: smaller than a 19″ rack unit, carried loose or in a small case (confirmed: no rack).
- **Robust for use on the move**: survives transport, power cuts and a crashed UI without taking the
  audio down; fast to sound after power-on.
- **Audio quality to match the Meyer boxes**: balanced outputs, low noise, enough headroom (the boxes
  start limiting near 0 dBV, so headroom is easy; noise and transients are the real criteria).
- **No thump** on power-up/down into the self-powered boxes (mute-at-boot + documented power order).
- **Budget**: compact — spend where it buys robustness and audio quality, not features.
- Latency irrelevant live (≤ ~10 ms) *(?)*.

## 5. Answers (2026-10-12)

| # | Question | Answer |
|---|---|---|
| 1 | Sub model | **USW-1P** |
| 2 | Outputs | **4 is enough for now** |
| 3 | Inputs | stereo line; mic input for measurement (owns a mic) |
| 4 | Build vs buy | likes **DIY and compactness**; would consider a ready-made box if it truly covers the need |
| 5 | Budget | **compact**, but **robust** (reliable on the move) and **high audio quality** to match Meyer |
| 6 | Form factor / power | **230 V available**, **no rack**; target smaller than 19″ width |
| 7 | Control posture | **knobs first**; phone: **Android** before iOS |
| 8 | Measurement mic | **yes** |
| 9 | Zynthian V4 state | *open* — needed to start the build |
| 10 | Repo licence | *open* |
| 11 | Date | **testable Friday 2026-10-16, gig Sunday 2026-10-18** |
