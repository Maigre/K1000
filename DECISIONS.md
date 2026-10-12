# K1000 — decision log

One entry per decision. States: `proposed` (Claude or anyone suggests) → `decided` (Thomas said so)
→ `superseded by D-NNN`. Newest at the bottom; never rewrite a decided entry, supersede it.

---

## D-001 — The repo is the project workspace · decided 2026-10-12

- **Context**: new project, no code yet; reviews, decisions and progress need a home.
- **Choice**: `Maigre/K1000` (public) holds requirements, reviews (`docs/NN-*.md`), this log and the
  roadmap, then the code. The 37Projects hub keeps only the project card, tasks and log.
- **By**: Thomas, at the project's creation.

## D-002 — Architecture: D1, CamillaDSP on the Zynthian V4 · proposed 2026-10-12

- **Context**: `docs/01-approaches-review.md` compared eight approaches; Thomas's answers
  (`docs/requirements.md` §5: USW-1P, 4 outs, no rack/compact, knobs first, Android, DIY welcome,
  gig 2026-10-18) narrow them (`docs/02-plan-to-sunday.md` §1).
- **Proposal**: D1 — the owned Zynthian V4 (Pi 4, 3.5″ screen, 4 encoders + 4 buttons) + a 4-out
  class-compliant USB interface + CamillaDSP; a small control service maps the knobs and serves the
  phone. Final hardware shape revisited after the first outing.
- **Rejected for now**: 19″ processors (no rack); compact DSP boxes (no live-EQ controls on the box);
  mixers (bulk) — the A&H CQ-18T stays the ready-made fallback.
- **Decision**: *pending Thomas.*

## D-003 — Audio interface: MOTU M4 · proposed 2026-10-12

- **Why**: bus-powered metal box, ESS DAC, LCD meters on all I/O, Linux-proven, 2 line in + mic with
  phantom at once, €289. Alternative: Focusrite Scarlett 4i4 Gen 4.
- **Caveats**: monitor knob scales outs 1-2 (set full and cap); monitor-mix / input monitoring off.
- **Decision**: *pending Thomas* (or an interface he already owns).

## D-004 — Engine for v0: CamillaDSP v4.1.3 (stable) · proposed 2026-10-12

- **Why**: v5.0.0-beta.1 (2026-10-08) brings the spectrum analyzer and lookahead limiter, but a beta
  does not go on a gig on 2026-10-18. Move to v5 after the first outing (configs and websocket client
  change shape).
- **Decision**: *pending Thomas.*
