# K1000 — requirements (draft 0, 2026-10-12)

What K1000 must do, as understood from Thomas's first brief. **Draft**: every line marked *(?)* is an
assumption waiting for an answer (see "Open questions" at the bottom).

## 1. The rig it drives

| Box | Qty | Notes |
|---|---|---|
| Meyer USW (USW-1P *(?)*) | 2 | self-powered sub, balanced XLR line in + loop out |
| Meyer UPA-1P | 2–4 | self-powered 2-way top, balanced XLR line in + loop out |

The boxes already carry their own amplification, crossover and protection. K1000 does **system**
processing in front of them, the job Meyer's LD-1A / LD-3 line drivers and later the Galileo did.

## 2. Use context — what makes this not a standard PA head

- **Mobile and roaming**: the system is on wheels, changes place during a day/show, often outdoor.
- **Measurement is the exception**: a pre-show measured tune is sometimes possible, often not.
- **Correction is live**: notch, shelve, rebalance subs/tops *during* the show, by ear, depending on
  where we are, how it sounds and who plays.
- Operator: Thomas *(?)*, possibly walking around the audience area while adjusting.

## 3. Functions

### Inputs
- **I1–2**: stereo line (main program) — must have.
- Option: a 2nd stereo line, or 1–2 mic inputs (announcements, and/or the measurement mic) *(?)*.

### Outputs (balanced line, XLR)
- Must: **Sub L, Sub R, Top L, Top R** (4).
- Probably wanted: **Top 2 L/R** (6) so a 2nd pair of UPA-1P can be a fill/delay pair with its own
  delay, gain and EQ instead of daisy-chained off the loop out *(?)*.
- Mono-sub option (sum L+R to both subs) — common outdoors *(?)*.

### Processing
| Where | What | Priority |
|---|---|---|
| Input | input gain/trim, input select/mix, mono sum switch | must |
| Master | **master level**, mute, **sub↔top balance** as one macro control | must |
| System EQ layer | parametric EQ set at tune time (measured or by ear), stored per preset | must |
| **Live EQ layer** | separate, quick notches/shelves on top of the system EQ; one-tap "reset live" | must |
| Per output | gain, mute, polarity, delay (ms / m), HPF/LPF (LR24/LR48…), a few PEQ bands | must |
| Per output | peak limiter (keep the boxes' own limiters from working) | nice |
| Global | presets/snapshots (by place type), A/B compare | should |
| Measurement | pink noise out, mic in, RTA / transfer function view, EQ suggestion | later |
| Live assist | spectrum view with peak markers → "notch here" in one move | should |

### Control
- **Physical**: at least master level, sub/top balance, mute, live-EQ access — usable in the dark
  and in sunlight, without a phone.
- **Remote**: phone or small tablet over Wi‑Fi, so the operator can listen from the audience area
  while adjusting.
- Either surface alone must be enough to run a show *(?)*.

## 4. Non-functional

- **Compact, road-proof**, fits the rig's transport (rack? case?) *(?)*.
- **Fast to sound** after power-on, and **survives power cuts** (no corrupted state).
- **No thump** on power-up/down into the self-powered boxes (or a documented power order).
- **Fails safe**: a crashed UI must never take the audio with it.
- Latency low enough to be irrelevant live (≤ ~5 ms) *(?)*.
- Sound quality on par with the source chain: balanced outs, +4 dBu nominal, enough headroom.

## 5. Open questions (for Thomas)

1. **Sub model**: USW-1P, or another USW variant? (Changes the useful crossover region.)
2. **Outputs**: 4 or 6? Stereo or mono subs? Ever a sub array (end-fire / cardioid) or delay towers?
3. **Inputs**: what feeds it — a mixer, DJ, player? Are mic inputs really wanted, and for what?
4. **Build vs buy**: is building K1000 part of the goal (a tool you want to own and hack), or is the
   *function* the goal and a good off-the-shelf box is fine?
5. **Budget** envelope (rough: < €300 / < €800 / < €2 000 / more)?
6. **Form factor**: is there a 19" rack in the rig (1U / half-rack welcome), or a separate case? Power:
   always mains, or sometimes battery/generator?
7. **Control posture**: phone-first with a few hard controls, or hardware-first with the phone as a bonus?
   Which phone/tablet (iOS / Android)?
8. **Measurement**: do you own a measurement mic? Preferred tool (Smaart, REW, Open Sound Meter, phone app)?
9. **Zynthian V4**: does it boot today, which Pi (4 GB?), and is it truly free to repurpose?
10. **Repo licence** (public repo has none yet): MIT, GPL-3, AGPL…?
11. **Date**: is there an outing you want K1000 on (sets whether a bought hedge is needed meanwhile)?
