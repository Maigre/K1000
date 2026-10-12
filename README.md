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
| `DECISIONS.md` | Decision log |
| `ROADMAP.md` | Phases and progress |
