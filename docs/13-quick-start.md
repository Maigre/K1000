# K1000 quick start

K1000 sits between the music and the speakers: it splits subs from tops, keeps the deep bass out of
the tops, aligns them and holds the EQ. **Any doubt about the sound: bypass** (bottom of the page).

## Cables

| From | To | Cable |
|---|---|---|
| Source L, R | M4 rear **LINE IN 3 (L)**, **LINE IN 4 (R)** | XLR-F → TRS. Never the front inputs 1–2 |
| M4 **OUT 1 · 2 · 3** | **Top L · Top R · both subs** (Loop or XLR Y) | TRS → XLR-M, balanced. No TS, no RCA |
| M4 USB-C | K1000 (the Pi), a black USB port | Short USB-C → USB-A |

**Leave the M4 front panel as taped**: MONITOR and INPUT MONITOR MIX fully right, MON buttons
unlit, 48V off, GAIN 1/2 at minimum. A lit MON button sends the raw music to the tops, past K1000
and its mute.

## Power: speakers on last, off first

**On:** speakers OFF, source quiet → M4 rear switch on → K1000 power. About 15 s later: **SHOW**,
green ● `RUNNING`, and a red **MUTED** band saying `READY` → speakers ON → **hold B4** to unmute →
E1 up. **No MUTED band** (`RAMPS IN BY ITSELF`): already live at its saved level, so speakers ON
with the source down, **no B4** (it would mute).

**Off:** hold B4 (MUTED) → speakers OFF → unplug K1000, then the M4.

## The 5 gestures

Knobs at the screen's corners (E1–E4: the sticker on the shell; no tag on the screen,
`knob_tags: off`), each value by its knob: **E1** top-left, **E2** bottom-left, **E3** top-right, **E4**
bottom-right. Buttons: **B1 PAGE · B2 BAND/UNLOCK · B3 NOTCH · B4 MUTE**.

| Gesture | Does |
|---|---|
| 1. **Hold B4** (1 s) | **Mute / unmute**, on any page. A tap does nothing |
| 2. **Turn E1** | Master volume, 0.5 dB a click; at most +6 dB per 2 s (`MASTER: SLOW DOWN`) |
| 3. **Tap B1** | SHOW ↔ ALIGN; from SETUP, back to SHOW. A hold opens SETUP |
| 4. **B3, E4 down, E3 up** | **Kill a ring** (SHOW). B3: a narrow, silent filter on the cyan PEAK; E4 down cuts (12 clicks = −6 dB); E3 up narrows. `NO PEAK`: E4 down first, then E2 until the ring drops |
| 5. **B2, then E2 / E3 / E4** | Next band (LS 1 2 3 4 HS), then its frequency / width / gain; **hold B3**: OUT/IN |

During the show leave ALIGN and SETUP alone; they go back to SHOW by themselves after a minute.

## When it goes wrong

| You see or hear | Do |
|---|---|
| Too loud, feedback, panic | **Hold B4** |
| `NO DEVICE` (orange screen) | The M4: its USB cable, its rear switch |
| `ENGINE DOWN` (red screen) | Wait 5 s: back by itself. `no input data`: reseat the M4's USB, M4 off/on. 30 s: bypass |
| `NO AUDIO FLOWING · HELD MUTED` | K1000 gets no audio from the M4: its USB and power |
| IN meters still, no sound | Source playing? Into the rear LINE IN 3/4 (not the front 1–2)? |
| SUB/TOP meters move, no sound | After K1000: speaker power, cables, the M4 MONITOR knob fully right |
| `CLIP` · `IN CLIP` (red, top line) | E1 down · the source down |
| `KNOBS OFFLINE` (red bar, B4 dead) | `USE PHONE MUTE`: mute from the phone. Else source down, bypass |
| Another orange or red bar, sound fine | Carry on; report it after the show |

## Bypass (about a minute)

1. Source **down**. Hold B4 if K1000 still answers.
2. At the speakers, unplug the cables coming from the M4.
3. Plug in the bypass cables, as before K1000: source L → ____________ · source R → ____________
4. Source up **slowly**: the bypass is louder (about 12 dB at master −10).

Lost in bypass: the Lo-Cut, the delays, the EQ, the mute. More: the operator card, and the manual
(`docs/manual` 4, 6).
