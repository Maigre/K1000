#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""k1000-knobtrace: what the Zynthian V4 knobs really send, and what the K1000 decoder makes of it.

Why: on 2026-10-12 Thomas found the knobs "not precise". The cause can be the encoder type (2 or
4 contact transitions per click), the decoder's timing rules, transitions missed while sampling,
contact bounce or the acceleration, and the code alone cannot tell which. A raw contact trace with
known turns can. docs/08-knob-precision.md has the analysis and the fix this tool decides.

Commands:

  capture   Opens the MCP23017 exactly like the k1000 driver (its own classes and register
            config, imported from /opt/k1000) and logs every A/B contact and switch change with a
            timestamp: INT-driven like the driver (`--mode irq`, the default, with the v1
            service's CPU load beside it: SHOW + spectrum, an asyncio loop thread, save chunks),
            or polled at 1 kHz (`--mode poll`).
            `--guided` walks you through ~2 minutes of known turns ("E1: exactly 10 clicks
            clockwise") so the analysis has a ground truth, then repeats two of them polled.
            Needs k1000.service STOPPED (it owns the chip and the INT lines). The engine keeps
            playing and its faders do not move, but B2 MUTE does nothing until k1000 runs again.
  analyze   The offline analysis of a saved trace (also printed at the end of every capture).
            Per encoder: transitions per click, the contact states it rests on, invalid
            (double-bit) changes, bounces, turn rates, pauses between clicks, a verdict for
            steps_per_detent, and a replay of the trace through the driver's QuadratureDecoder.
  bench     No hardware: known fast turns through the driver's REAL worker thread on a
            time-driven MCP23017 (k1000.hw.simchip), idle and beside the v1 service's load
            (SHOW + moving spectrum rendered in a thread, ballistics and a CPU-bound save chunk
            in an asyncio loop thread): edge -> sample delays and clicks lost.

On the box (about 2 minutes, not during a show):

    sudo systemctl stop k1000
    python3 /opt/k1000/tools/k1000-knobtrace.py capture --guided -o ~/knobs-$(date +%H%M).trace
    sudo systemctl start k1000

Trace format (text, one line per change, replayable): `#K1000-KNOBTRACE 1`, a `#meta {json}` line,
then `<t seconds> <levels hex GPIOB<<8|GPIOA> <source> [<INT edge -> sample latency s>]` lines and
`#mode`, `#mark`, `#stats` json directives. Only level changes are written.
"""
from __future__ import annotations

import argparse
import collections
import inspect
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

MAGIC = "#K1000-KNOBTRACE 1"

# Contract defaults (docs/03-v0-architecture.md section 1), as pin indices of the 16-bit
# GPIOB<<8|GPIOA level word. Duplicated on purpose: the offline analysis must run anywhere.
ENC_PINS: tuple[tuple[int, int], ...] = ((2, 1), (5, 4), (10, 9), (13, 12))
SW_PINS: tuple[int, ...] = (0, 3, 8, 11, 6, 7, 14, 15)
PIN_NAMES = tuple([f"GPA{i}" for i in range(8)] + [f"GPB{i}" for i in range(8)])
# E1..E4 at the screen corners (Thomas confirmed 2026-10-12), default controls.encoders map.
CORNERS = ("top-left", "bottom-left", "top-right", "bottom-right")

# Quadrature: state = A << 1 | B (1 = contact open, pull-up high). Clockwise per the driver's
# convention (zyncoder's, A changes first): 11 -> 01 -> 00 -> 10 -> 11. Same table as the
# driver's _QSTEP; INV = both contacts changed between two samples (direction unknown).
INV = 2
QSTEP = (0, -1, +1, INV,
         +1, 0, INV, -1,
         -1, INV, 0, +1,
         INV, +1, -1, 0)
RING = (3, 1, 0, 2)

REST_S = 0.08        # a pause this long is a rest (the knob sits in a detent)
IDLE_S = 0.3         # the driver's idle_ms: its decoder re-decides after this long off a detent
INFER_S = 0.1        # the driver's infer_s (skipped-state direction inference)
BOUNCE_S = 0.002     # x -> y -> x within this = contact bounce
I2C_READ_S = 0.0011  # one guarded 8-byte read at the box's 100 kHz i2c-1 (measured 0.87 ms / 6 B)
ACCEL_LOW_DPS = 12.0  # input_map.ACCEL_LOW_DPS today (x1 below)


def ab_str(state: int) -> str:
    return f"{(state >> 1) & 1}{state & 1}"


def parse_ab(v: Any) -> int:
    """'11' / '01' (A then B) or 0..3 -> state int."""
    if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 3:
        return v
    s = str(v).strip()
    if len(s) == 2 and set(s) <= {"0", "1"}:
        return int(s[0]) << 1 | int(s[1])
    raise ValueError(f"not an AB state: {v!r}")


def ab_of(levels: int, pins: Sequence[int]) -> int:
    return (((levels >> pins[0]) & 1) << 1) | ((levels >> pins[1]) & 1)


# =============================================================================================
# Trace container, reader, writer
# =============================================================================================
@dataclass
class Trace:
    meta: dict = field(default_factory=dict)
    samples: list = field(default_factory=list)    # (t, levels, src, lat | None)
    marks: list = field(default_factory=list)      # dicts with t, step, phase, ...
    modes: list = field(default_factory=list)      # dicts with t, mode, load
    stats: list = field(default_factory=list)      # dicts (one per capture pass)

    @property
    def encoders(self) -> list[tuple[int, int]]:
        return [tuple(p) for p in self.meta.get("encoders", ENC_PINS)]

    @property
    def switches(self) -> list[int]:
        return list(self.meta.get("switches", SW_PINS))

    def label(self, i: int) -> str:
        labels = self.meta.get("labels") or {}
        return labels.get(str(i)) or f"enc{i}"

    @property
    def t_end(self) -> float:
        ts = [s[0] for s in self.samples] + [m.get("t", 0.0) for m in self.marks]
        return max(ts) if ts else 0.0


def read_trace(src: "str | Path | Iterable[str]") -> Trace:
    lines = Path(src).read_text().splitlines() if isinstance(src, (str, Path)) else list(src)
    if not lines or not lines[0].startswith("#K1000-KNOBTRACE"):
        raise ValueError("not a k1000-knobtrace file (first line must be '#K1000-KNOBTRACE 1')")
    tr = Trace()
    for n, line in enumerate(lines[1:], 2):
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            kind, _, rest = line[1:].partition(" ")
            try:
                obj = json.loads(rest) if rest else {}
            except ValueError:
                continue                          # a comment
            if kind == "meta":
                tr.meta.update(obj)
            elif kind == "mark":
                tr.marks.append(obj)
            elif kind == "mode":
                tr.modes.append(obj)
            elif kind == "stats":
                tr.stats.append(obj)
            continue
        parts = line.split()
        try:
            t, lv = float(parts[0]), int(parts[1], 16)
        except (IndexError, ValueError):
            raise ValueError(f"line {n}: bad sample {line!r}") from None
        src_ = parts[2] if len(parts) > 2 else "?"
        lat = float(parts[3]) if len(parts) > 3 and parts[3] != "-" else None
        tr.samples.append((t, lv, src_, lat))
    tr.samples.sort(key=lambda s: s[0])
    return tr


class TraceWriter:
    """Appends to a trace file from the sampler thread and the prompt thread (one lock), and
    keeps everything in memory for the live feedback and the final analysis."""

    def __init__(self, path: Optional[str], meta: dict, t0: float) -> None:
        self.t0 = t0
        self.trace = Trace(meta=dict(meta))
        self._lock = threading.Lock()
        self._f = open(path, "w", buffering=1 << 16) if path else None
        self._last: Optional[int] = None
        self._last_t = 0.0
        self._write(MAGIC)
        self._write("#meta " + json.dumps(meta, sort_keys=True))

    def _write(self, line: str) -> None:
        if self._f is not None:
            self._f.write(line + "\n")

    def rel(self, t_mono: float) -> float:
        return t_mono - self.t0

    def sample(self, t_mono: float, levels: int, src: str, lat: Optional[float] = None,
               force: bool = False) -> bool:
        """Record `levels` if they changed (or `force`). Times never go backwards."""
        with self._lock:
            if not force and levels == self._last:
                return False
            t = max(self.rel(t_mono), self._last_t)
            self._last, self._last_t = levels, t
            self.trace.samples.append((t, levels, src, lat))
            lat_s = "-" if lat is None else f"{lat:.6f}"
            self._write(f"{t:.6f} {levels:04x} {src} {lat_s}")
            return True

    def directive(self, kind: str, obj: dict) -> None:
        with self._lock:
            getattr(self.trace, {"mark": "marks", "mode": "modes", "stats": "stats"}[kind]).append(obj)
            self._write(f"#{kind} " + json.dumps(obj, sort_keys=True))
            if self._f is not None:
                self._f.flush()

    def now(self) -> float:
        return round(self.rel(time.monotonic()), 6)

    def close(self) -> None:
        with self._lock:
            if self._f is not None:
                self._f.close()
                self._f = None


# =============================================================================================
# Synthetic traces (unit tests, `demo`)
# =============================================================================================
class Synth:
    """Physical contact changes of the four encoders, as a trace with perfect sampling.

    A click is `spd` transitions: the first half slow (the hand pushes up the detent hill), the
    second half a fast snap into the next detent, like a real detented encoder."""

    def __init__(self, spd: int = 4, rest: "int | Sequence[int]" = 3) -> None:
        self.spd = spd
        rests = list(rest) if isinstance(rest, (list, tuple)) else [rest] * 4
        self.state = [r & 3 for r in rests]
        self.levels = 0xFFFF
        for i, pins in enumerate(ENC_PINS):
            self.levels = self._with(self.levels, i, self.state[i])
        self.t = 0.0
        self.tr = Trace(meta={"encoders": [list(p) for p in ENC_PINS], "switches": list(SW_PINS),
                              "labels": {str(i): f"E{i + 1} {CORNERS[i]}" for i in range(4)},
                              "synthetic": True})
        self.tr.samples.append((0.0, self.levels, "seed", None))
        self.tr.modes.append({"t": 0.0, "mode": "synthetic"})

    @staticmethod
    def _with(levels: int, enc: int, state: int) -> int:
        a, b = ENC_PINS[enc]
        levels &= ~((1 << a) | (1 << b))
        return levels | (((state >> 1) & 1) << a) | ((state & 1) << b)

    def _set(self, enc: int, state: int) -> None:
        self.state[enc] = state & 3
        self.levels = self._with(self.levels, enc, state)
        self.tr.samples.append((round(self.t, 6), self.levels, "syn", None))

    def wait(self, s: float) -> "Synth":
        self.t += s
        return self

    def step(self, enc: int, direction: int) -> None:
        pos = RING.index(self.state[enc])
        self._set(enc, RING[(pos + (1 if direction > 0 else -1)) % 4])

    def click(self, enc: int, direction: int = 1, dur: float = 0.06, *, spd: Optional[int] = None,
              bounce: int = 0, hover_at: Optional[int] = None, hover_s: float = 0.0,
              pause: float = 0.25) -> "Synth":
        n = spd or self.spd
        slow = [dur * 0.8 / max(1, n // 2)] * (n // 2)
        fast = [dur * 0.2 / max(1, n - n // 2)] * (n - n // 2)
        for k, gap in enumerate(slow + fast):
            self.t += gap
            prev = self.state[enc]
            self.step(enc, direction)
            for _ in range(bounce):                      # x -> y -> x -> y, 0.3 ms apart
                cur = self.state[enc]
                self.t += 0.0003
                self._set(enc, prev)
                self.t += 0.0003
                self._set(enc, cur)
            if hover_at is not None and k + 1 == hover_at:
                self.t += hover_s
        self.t += pause
        return self

    def clicks(self, enc: int, direction: int, n: int, **kw: Any) -> "Synth":
        for _ in range(n):
            self.click(enc, direction, **kw)
        return self

    def spin(self, enc: int, direction: int, n: int, rate_dps: float) -> "Synth":
        """`n` clicks at a steady `rate_dps` (transitions evenly spaced), then a rest."""
        gap = 1.0 / (rate_dps * self.spd)
        for _ in range(n * self.spd):
            self.t += gap
            self.step(enc, direction)
        self.t += 0.3
        return self

    def press(self, sw: int, hold: float = 0.15, chatter: int = 0) -> "Synth":
        p = SW_PINS[sw]
        for _ in range(chatter):
            self.levels &= ~(1 << p)
            self.tr.samples.append((round(self.t, 6), self.levels, "syn", None))
            self.t += 0.0005
            self.levels |= 1 << p
            self.tr.samples.append((round(self.t, 6), self.levels, "syn", None))
            self.t += 0.0005
        self.levels &= ~(1 << p)
        self.tr.samples.append((round(self.t, 6), self.levels, "syn", None))
        self.t += hold
        self.levels |= 1 << p
        self.tr.samples.append((round(self.t, 6), self.levels, "syn", None))
        self.t += 0.2
        return self

    def mark(self, phase: str, step: str, **kw: Any) -> "Synth":
        self.tr.marks.append({"t": round(self.t, 6), "phase": phase, "step": step, **kw})
        return self

    def guided(self, step: str, enc: int, direction: int, clicks: int, kind: str = "slow",
               body: Optional[Callable[["Synth"], Any]] = None) -> "Synth":
        self.wait(0.5).mark("start", step, enc=enc, dir=direction, clicks=clicks, kind=kind,
                            mode="synthetic", label=self.tr.label(enc))
        if body is None:
            self.clicks(enc, direction, clicks)
        else:
            body(self)
        return self.wait(0.5).mark("end", step)

    def trace(self) -> Trace:
        self.tr.samples.sort(key=lambda s: s[0])
        return self.tr


def resample(tr: Trace, period: float, src: str = "poll") -> Trace:
    """`tr` as a poller reading every `period` s would see it (fast changes merge or vanish)."""
    out = Trace(meta=dict(tr.meta), marks=list(tr.marks), modes=list(tr.modes))
    if not tr.samples:
        return out
    k, cur, last = 0, tr.samples[0][1], None
    t, t_end = 0.0, tr.t_end + period
    while t <= t_end:
        while k < len(tr.samples) and tr.samples[k][0] <= t:
            cur = tr.samples[k][1]
            k += 1
        if cur != last:
            out.samples.append((round(t, 6), cur, src, None))
            last = cur
        t += period
    return out


# =============================================================================================
# Analysis (pure)
# =============================================================================================
@dataclass
class Tr:
    t: float
    prev: int
    cur: int
    step: int        # +1 / -1 valid quarter-step, INV = both contacts changed between samples
    credit: int      # quarter-steps credited: the step, or +/-2 inferred for INV (0 = unknown)


def enc_states(tr: Trace, enc: int) -> tuple[int, list[tuple[float, int]]]:
    """(initial AB state, [(t, state)] at every change) of one encoder."""
    pins = tr.encoders[enc]
    if not tr.samples:
        return 3, []
    init = ab_of(tr.samples[0][1], pins)
    out, last = [], init
    for t, lv, _src, _lat in tr.samples[1:]:
        s = ab_of(lv, pins)
        if s != last:
            out.append((t, s))
            last = s
    return init, out


def transitions(init: int, states: Sequence[tuple[float, int]], infer_s: float = INFER_S) -> list[Tr]:
    out: list[Tr] = []
    prev, last_dir, last_t = init, 0, -1e9
    for t, s in states:
        step = QSTEP[(prev << 2) | s]
        if step == INV:
            credit = 2 * last_dir if (last_dir and t - last_t <= infer_s) else 0
        else:
            credit = step
        if credit:
            last_dir, last_t = (1 if credit > 0 else -1), t
        out.append(Tr(t, prev, s, step, credit))
        prev = s
    return out


@dataclass
class Seg:
    """Transitions between two rests (pauses >= REST_S)."""
    t0: float
    t1: float
    start: int
    end: int
    net: int
    n: int
    invalid: int
    unknown: int
    rest_after: float


def segments(init: int, trs: Sequence[Tr], t_end: float, rest_s: float = REST_S) -> list[Seg]:
    segs: list[Seg] = []
    i = 0
    start = init
    while i < len(trs):
        j = i
        while j + 1 < len(trs) and trs[j + 1].t - trs[j].t < rest_s:
            j += 1
        grp = trs[i:j + 1]
        nxt = trs[j + 1].t if j + 1 < len(trs) else t_end
        segs.append(Seg(grp[0].t, grp[-1].t, start, grp[-1].cur, sum(x.credit for x in grp),
                        len(grp), sum(1 for x in grp if x.step == INV),
                        sum(1 for x in grp if x.step == INV and not x.credit),
                        max(0.0, nxt - grp[-1].t)))
        start = grp[-1].cur
        i = j + 1
    return segs


def count_bounces(trs: Sequence[Tr], bounce_s: float = BOUNCE_S) -> int:
    """x -> y -> x within bounce_s (non-overlapping pairs)."""
    n, i = 0, 1
    while i < len(trs):
        a, b = trs[i - 1], trs[i]
        if b.cur == a.prev and b.t - a.t <= bounce_s and a.step != INV and b.step != INV:
            n += 1
            i += 2
        else:
            i += 1
    return n


def detent_states(spd: int, detent: int) -> set[int]:
    if spd == 1:
        return {0, 1, 2, 3}
    if spd == 2:
        return {detent, detent ^ 3}
    return {detent}


def rest_hist(init: int, trs: Sequence[Tr], segs: Sequence[Seg], t_start: float,
              rest_s: float = REST_S) -> collections.Counter:
    h: collections.Counter = collections.Counter()
    if not trs or trs[0].t - t_start >= rest_s:
        h[init] += 1
    for s in segs:
        if s.rest_after >= rest_s:
            h[s.end] += 1
    return h


def verdict(rests: collections.Counter, seg_hist: collections.Counter,
            guided: Sequence[float] = ()) -> dict:
    """steps_per_detent and the detent AB state, with how sure we are and why."""
    why: list[str] = []
    total = sum(rests.values())
    by_rest: Optional[int] = None
    detent: Optional[int] = None
    if total:
        (s1, n1), *more = rests.most_common()
        share1 = n1 / total
        pair = rests[s1] + rests[s1 ^ 3]
        if share1 >= 0.75:
            by_rest, detent = 4, s1
        elif pair / total >= 0.75 and min(rests[s1], rests[s1 ^ 3]) / total >= 0.2:
            by_rest, detent = 2, (3 if 3 in (s1, s1 ^ 3) else s1)
        elif sum(1 for s in range(4) if rests[s] / total >= 0.1) >= 3:
            by_rest, detent = 1, s1
        why.append("rests at AB " + ", ".join(f"{ab_str(s)} x{n}" for s, n in rests.most_common()))
    small = {k: v for k, v in seg_hist.items() if 1 <= k <= 4}
    by_hist = max(small, key=lambda k: (small[k], k)) if small else None
    if by_hist not in (None, 1, 2, 4):
        by_hist = None
    if seg_hist:
        why.append("transitions between rests " +
                   ", ".join(f"{k} x{v}" for k, v in sorted(seg_hist.items(), key=lambda kv: -kv[1])[:5]))
    by_guided = None
    if guided:
        r = statistics.median(guided)
        near = min((1, 2, 4), key=lambda s: abs(r - s))
        if abs(r - near) <= 0.2 * near:
            by_guided = near
        why.append(f"guided {r:.2f} transitions per click")
    votes = [v for v in (by_guided, by_rest, by_hist) if v]
    if by_guided:
        spd = by_guided
        conf = "high" if all(v == spd for v in votes) else "medium"
    elif by_rest and by_rest == by_hist:
        spd, conf = by_rest, "medium"
    elif votes:
        spd, conf = votes[0], "low"
    else:
        spd, conf = 4, "none"
        why.append("no clicks in the trace: the driver default (4)")
    if detent is None or (spd == 4 and by_rest != 4):
        detent = rests.most_common(1)[0][0] if total else 3
    if spd == 2 and detent ^ 3 == 3:
        detent = 3
    return {"spd": spd, "detent": ab_str(detent), "confidence": conf, "why": why}


def detent_arrivals(init: int, trs: Sequence[Tr], spd: int, detent: int) -> list[tuple[float, int]]:
    """(t, direction) each time the knob reaches another detent: the physical clicks. A bounce
    back into the detent it just left is not a click (same position)."""
    dset = detent_states(spd, detent)
    out: list[tuple[float, int]] = []
    pos = last_pos = 0
    for x in trs:
        pos += x.credit
        if x.cur in dset:
            n = int(math.floor((pos - last_pos) / spd + 0.5)) if spd > 1 else pos - last_pos
            out.extend((x.t, 1 if n > 0 else -1) for _ in range(abs(n)))
            last_pos = pos
    return out


def accel_share(arrivals: Sequence[tuple[float, int]], window_s: float = 0.25) -> dict:
    """Detent-to-detent rates as the acceleration sees them (same direction, within the window)."""
    rates = []
    for (t0, d0), (t1, d1) in zip(arrivals, arrivals[1:]):
        if d0 == d1 and 0 < t1 - t0 <= window_s:
            rates.append(1.0 / max(t1 - t0, 0.002))
    if not rates:
        return {"intervals": 0}
    return {"intervals": len(rates),
            "over_12": round(sum(r > 12 for r in rates) / len(rates), 3),
            "over_20": round(sum(r > 20 for r in rates) / len(rates), 3),
            "over_40": round(sum(r > 40 for r in rates) / len(rates), 3),
            "max_dps": round(max(rates), 1), "median_dps": round(statistics.median(rates), 1)}


# --------------------------------------------------------------------------------- replay
class V0Decoder:
    """The k1000 v0 QuadratureDecoder as deployed on 2026-10-12 (box/k1000/hw/mcp23017.py at
    ca742e7), frozen here so every trace shows what the v0 service made of it, whatever driver
    is installed. Its settle() flushes and RE-ANCHORS after any idle_s pause off the anchor."""

    def __init__(self, steps_per_detent: int = 4, state: int = 3, now: float = 0.0,
                 idle_s: float = IDLE_S, infer_s: float = INFER_S) -> None:
        self.spd, self.idle_s, self.infer_s, self.invalid = steps_per_detent, idle_s, infer_s, 0
        self.state = self.anchor = state & 3
        self.acc, self.last_change, self.last_dir = 0, now, 0

    def is_rest(self, state: int) -> bool:
        if self.spd == 1:
            return True
        if self.spd == 2:
            return state in (self.anchor, self.anchor ^ 3)
        return state == self.anchor

    def update(self, state: int, now: float = 0.0) -> int:
        state &= 3
        if state == self.state:
            return 0
        step = QSTEP[(self.state << 2) | state]
        prev_change, self.last_change = self.last_change, now
        self.state = state
        if step == INV:
            if self.acc:
                step = 2 if self.acc > 0 else -2
            elif self.last_dir and now - prev_change <= self.infer_s:
                step = 2 * self.last_dir
            else:
                self.invalid += 1
                return 0
        self.acc += step
        self.last_dir = 1 if step > 0 else -1
        return self._flush() if self.is_rest(state) else 0

    def _flush(self) -> int:
        acc, self.acc = self.acc, 0
        n = (abs(acc) + self.spd // 2) // self.spd
        return n if acc > 0 else -n

    def deadline(self) -> Optional[float]:
        if self.spd == 1 or (self.acc == 0 and self.is_rest(self.state)):
            return None
        return self.last_change + self.idle_s

    def settle(self, now: float) -> int:
        d = self.deadline()
        if d is None or now < d:
            return 0
        n = self._flush()
        self.anchor = self.state
        return n


def decoder_supports(cls: Any, name: str) -> bool:
    try:
        return name in inspect.signature(cls).parameters
    except (TypeError, ValueError):
        return False


def replay(make: Callable[[int, float], Any], init: int, states: Sequence[tuple[float, int]],
           t_start: float, t_end: float) -> list[tuple[float, int, str]]:
    """Feed the state stream to a decoder like the driver does: update() on every sample and
    settle() at its deadline. -> [(t, detents, 'arrive' | 'settle')]."""
    dec = make(init, t_start)
    out: list[tuple[float, int, str]] = []

    def run_deadlines(until: float) -> None:
        for _ in range(64):
            d = dec.deadline()
            if d is None or d > until:
                return
            n = dec.settle(d)
            if n:
                out.append((d, n, "settle"))
            if dec.deadline() == d:
                return

    for t, s in states:
        run_deadlines(t)
        n = dec.update(s, t)
        if n:
            out.append((t, n, "arrive"))
    run_deadlines(t_end + 10.0)
    return out


def decoder_variants(qd: Any, spd_v: int, detent_v: int) -> list[tuple[str, Callable[[int, float], Any]]]:
    """The decoders worth comparing on this trace: v0 (what the service ran on 2026-10-12) at
    spd 4 and 2, and, when the installed driver has the fixed decoder (its `rest` option,
    docs/08-knob-precision.md), the fix configured with this trace's verdict and with the other
    steps_per_detent."""
    vs: list[tuple[str, Callable[[int, float], Any]]] = [
        ("v0 decoder, spd 4 (2026-10-12)", lambda s, t: V0Decoder(4, s, t)),
        ("v0 decoder, spd 2", lambda s, t: V0Decoder(2, s, t))]
    if qd is not None and decoder_supports(qd, "rest"):
        kw: dict = {"learn_rests": 3} if decoder_supports(qd, "learn_rests") else {}
        other = 2 if spd_v == 4 else 4
        vs.append((f"fixed decoder, spd {spd_v} detent {ab_str(detent_v)}",
                   lambda s, t: qd(spd_v, s, t, idle_s=IDLE_S, rest=detent_v, **kw)))
        vs.append((f"fixed decoder, spd {other} detent 11",
                   lambda s, t: qd(other, s, t, idle_s=IDLE_S, rest=3, **kw)))
    return vs


# ---------------------------------------------------------------------------- guided steps
def guided_windows(tr: Trace) -> list[dict]:
    """Pairs of start/end marks -> [{step, enc, dir, clicks, kind, t0, t1, mode}], redone steps
    (end mark with discard) dropped."""
    out: list[dict] = []
    open_: dict = {}
    for m in sorted(tr.marks, key=lambda m: m.get("t", 0.0)):
        if m.get("phase") == "start":
            open_[m.get("step")] = m
        elif m.get("phase") == "end" and m.get("step") in open_:
            s = open_.pop(m["step"])
            if m.get("discard"):
                continue
            out.append({**{k: v for k, v in s.items() if k not in ("t", "phase")},
                        "t0": s["t"], "t1": m["t"]})
    return out


def window_net(trs: Sequence[Tr], t0: float, t1: float) -> dict:
    w = [x for x in trs if t0 <= x.t <= t1]
    return {"transitions": len(w), "net": sum(x.credit for x in w),
            "invalid": sum(1 for x in w if x.step == INV),
            "unknown": sum(1 for x in w if x.step == INV and not x.credit),
            "bounces": count_bounces(w)}


# ------------------------------------------------------------------------------- switches
def switch_report(tr: Trace, chatter_s: float = 0.005) -> list[dict]:
    out = []
    for i, p in enumerate(tr.switches):
        last, last_t, flips, presses, chatter = None, None, 0, 0, 0
        for t, lv, _s, _l in tr.samples:
            v = (lv >> p) & 1
            if last is None:
                last, last_t = v, t
                continue
            if v != last:
                flips += 1
                if last_t is not None and t - last_t <= chatter_s:
                    chatter += 1
                if v == 0:
                    presses += 1
                last, last_t = v, t
        if flips:
            out.append({"switch": i, "pin": PIN_NAMES[p], "presses": presses, "flips": flips,
                        "chatter": chatter})
    return out


# ------------------------------------------------------------------------------- the lot
def analyze(tr: Trace, qd: Any = None, rest_s: float = REST_S) -> dict:
    """Everything the report prints, as a dict (JSON-able). `qd`: the installed driver's
    QuadratureDecoder class (replayed when it is the fixed one), or None."""
    t_start = tr.samples[0][0] if tr.samples else 0.0
    t_end = tr.t_end
    wins = guided_windows(tr)
    res: dict = {"duration_s": round(t_end - t_start, 3), "samples": len(tr.samples),
                 "modes": tr.modes, "capture": tr.stats, "encoders": [], "steps": [],
                 "switches": switch_report(tr)}
    per_enc_trs = {}
    for i in range(len(tr.encoders)):
        init, states = enc_states(tr, i)
        trs = transitions(init, states)
        per_enc_trs[i] = (init, states, trs)
        segs = segments(init, trs, t_end, rest_s)
        rests = rest_hist(init, trs, segs, t_start, rest_s)
        seg_hist = collections.Counter(abs(s.net) for s in segs if s.net)
        ratios = []
        v_rests: collections.Counter = collections.Counter()
        v_hist: collections.Counter = collections.Counter()
        for w in wins:
            if w.get("enc") != i or not w.get("clicks"):
                continue
            wn = window_net(trs, w["t0"], w["t1"])
            if wn["transitions"]:
                ratios.append(abs(wn["net"]) / w["clicks"])
            if w.get("kind") == "slow":
                # normal clicks only: a very slow turn pauses between every contact change
                before = [x for x in trs if x.t < w["t0"]]
                inw = [x for x in trs if w["t0"] <= x.t <= w["t1"]]
                ws = segments(before[-1].cur if before else init, inw, w["t1"], rest_s)
                v_rests += rest_hist(before[-1].cur if before else init, inw, ws, w["t0"], rest_s)
                v_hist += collections.Counter(abs(x.net) for x in ws if x.net)
        basis = "slow guided steps" if v_rests else "whole trace"
        v = verdict(v_rests or rests, v_hist or seg_hist, ratios)
        v["basis"] = basis
        spd_v, det_v = v["spd"], parse_ab(v["detent"])
        dset = detent_states(spd_v, det_v)
        hovers = sum(1 for s in segs if s.rest_after >= IDLE_S and s.end not in dset)
        gaps = [b.t - a.t for a, b in zip(trs, trs[1:]) if b.t - a.t < rest_s]
        rates = [abs(s.net) / spd_v / max(s.t1 - s.t0, 1e-3) for s in segs
                 if abs(s.net) >= 2 * spd_v]
        arr = detent_arrivals(init, trs, spd_v, det_v)
        e: dict = {
            "index": i, "label": tr.label(i),
            "pins": [PIN_NAMES[p] for p in tr.encoders[i]],
            "initial": ab_str(init), "transitions": len(trs),
            "invalid": sum(1 for x in trs if x.step == INV),
            "invalid_unattributed": sum(1 for x in trs if x.step == INV and not x.credit),
            "bounces": count_bounces(trs),
            "rests": {ab_str(s): n for s, n in rests.most_common()},
            "between_rests": {str(k): n for k, n in sorted(seg_hist.items())},
            "hovers": hovers,
            "turn_dps_max": round(max(rates), 1) if rates else None,
            "turn_dps_median": round(statistics.median(rates), 1) if rates else None,
            "gap_min_ms": round(min(gaps) * 1000, 2) if gaps else None,
            "gap_p5_ms": round(sorted(gaps)[len(gaps) // 20] * 1000, 2) if gaps else None,
            "gaps_under_i2c_read": sum(1 for g in gaps if g < I2C_READ_S),
            "clicks": len(arr), "accel": accel_share(arr), "verdict": v, "replay": {},
        }
        if trs:
            for name, make in decoder_variants(qd, spd_v, det_v):
                ev = replay(make, init, states, t_start, t_end)
                e["replay"][name] = {"emitted": sum(n for _t, n, _h in ev),
                                     "events": len(ev),
                                     "timer": sum(1 for _t, _n, h in ev if h == "settle")}
                e.setdefault("_events", {})[name] = ev
        res["encoders"].append(e)
    for w in wins:
        i = w.get("enc")
        if i not in per_enc_trs:
            continue
        init, states, trs = per_enc_trs[i]
        wn = window_net(trs, w["t0"], w["t1"])
        step = {**w, **wn, "per_click": (round(abs(wn["net"]) / w["clicks"], 2)
                                         if w.get("clicks") else None),
                "direction_ok": (None if not w.get("dir") or not wn["net"]
                                 else (wn["net"] > 0) == (w["dir"] > 0)),
                "decoders": {}}
        inw = [x for x in trs if w["t0"] <= x.t <= w["t1"]]
        before = [x for x in trs if x.t < w["t0"]]
        segs = segments(before[-1].cur if before else init, inw, w["t1"])
        spd_v = res["encoders"][i]["verdict"]["spd"]
        det_v = parse_ab(res["encoders"][i]["verdict"]["detent"])
        step["hovers"] = sum(1 for s in segs if s.rest_after >= IDLE_S
                             and s.end not in detent_states(spd_v, det_v))
        for name, ev in res["encoders"][i].get("_events", {}).items():
            got = [(t, n, h) for t, n, h in ev if w["t0"] <= t <= w["t1"] + IDLE_S + 0.05]
            wrong = 0
            if w.get("dir"):
                wrong = sum(abs(n) for _t, n, _h in got if (n > 0) != (w["dir"] > 0))
            step["decoders"][name] = {"emitted": sum(n for _t, n, _h in got),
                                      "timer": sum(1 for _t, _n, h in got if h == "settle"),
                                      "wrong_way": wrong}
        res["steps"].append(step)
    for e in res["encoders"]:
        e.pop("_events", None)
    used = [e for e in res["encoders"] if e["transitions"]]
    res["config"] = {
        "steps_per_detent": [e["verdict"]["spd"] for e in res["encoders"]],
        "detent_ab": [e["verdict"]["detent"] for e in res["encoders"]],
        "from_encoders_moved": [e["index"] for e in used]}
    return res


# --------------------------------------------------------------------------------- report
def _fmt_capture(st: dict) -> str:
    mode = st.get("mode", "?")
    if mode == "irq":
        s = (f"irq{'+' + st['load'] if st.get('load') else ''}: {st.get('reads', 0)} reads, "
             f"read {st.get('read_ms_avg', 0):.2f} ms avg / {st.get('read_ms_max', 0):.2f} max")
        if st.get("lat_ms_avg") is not None:
            s += (f"; INT edge -> sample {st['lat_ms_avg']:.2f} ms avg, p95 {st['lat_ms_p95']:.2f}, "
                  f"max {st['lat_ms_max']:.2f}")
        s += f"; edges {st.get('edges', 0)}, resync polls {st.get('polls', 0)}"
    elif mode == "poll":
        s = (f"poll: {st.get('reads', 0)} reads at {st.get('rate_hz', 0):.0f} Hz "
             f"(asked {st.get('asked_hz', 0):.0f}), max gap {st.get('gap_ms_max', 0):.2f} ms, "
             f"gaps > 5 ms: {st.get('gaps_over_5ms', 0)}")
    else:
        s = mode
    extra = [f"{k} {st[k]}" for k in ("pullup_alarms", "i2c_errors", "reconfigs") if st.get(k)]
    return s + (" | " + ", ".join(extra) if extra else "")


def report(res: dict, title: str = "") -> str:
    L: list[str] = []
    L.append(f"K1000 knob trace{(': ' + title) if title else ''} "
             f"({res['duration_s']:.1f} s, {res['samples']} samples)")
    for st in res.get("capture") or []:
        L.append("  capture " + _fmt_capture(st))
    if res["steps"]:
        names = sorted({n for s in res["steps"] for n in s["decoders"]},
                       key=lambda n: (not n.startswith("v0"), n))
        L.append("")
        L.append("GUIDED STEPS (contacts = raw contact transitions; then the detents each decoder "
                 "emitted; 'by the pause timer' = emitted 0.3 s after the knob stopped instead "
                 "of on arrival in a detent; 'wrong' = against the turn)")
        for k, s in enumerate(res["steps"], 1):
            want = f"{s.get('clicks') or '?'} clicks" + (" CW" if s.get("dir", 0) > 0 else
                                                          " CCW" if s.get("dir", 0) < 0 else "")
            head = (f" {k:2d} {s.get('label') or ('enc%d' % s.get('enc', -1))}: {want} "
                    f"{s.get('kind', '')} [{s.get('mode', '')}]")
            L.append(head)
            pc = f" = {s['per_click']:.2f}/click" if s.get("per_click") is not None else ""
            dirn = "" if s.get("direction_ok") in (None, True) else "  DIRECTION REVERSED"
            L.append(f"      contacts {s['net']:+d}{pc} ({s['transitions']} changes, "
                     f"{s['invalid']} invalid, {s['bounces']} bounces, "
                     f"{s['hovers']} pauses >= 0.3 s between clicks){dirn}")
            for n in names:
                d = s["decoders"].get(n)
                if d is None:
                    continue
                bad = []
                if d["timer"]:
                    bad.append(f"{d['timer']} by the pause timer")
                if d["wrong_way"]:
                    bad.append(f"{d['wrong_way']} wrong")
                L.append(f"      {n:<34} {d['emitted']:+d}" + (f"  ({', '.join(bad)})" if bad else ""))
    for e in res["encoders"]:
        L.append("")
        L.append(f"enc{e['index']} = {e['label']} [A={e['pins'][0]} B={e['pins'][1]}], "
                 f"at start AB {e['initial']}")
        if not e["transitions"]:
            L.append("  not turned")
            continue
        L.append(f"  {e['transitions']} contact changes: {e['invalid']} invalid (both contacts at once"
                 f", {e['invalid_unattributed']} without a direction), {e['bounces']} bounces "
                 f"(x->y->x < {BOUNCE_S * 1000:.0f} ms)")
        L.append("  rests at AB: " + ", ".join(f"{k} x{v}" for k, v in e["rests"].items()))
        L.append("  transitions between rests: " +
                 ", ".join(f"{k} x{v}" for k, v in sorted(e["between_rests"].items(),
                                                           key=lambda kv: -kv[1])[:6]))
        if e["turn_dps_max"] is not None:
            L.append(f"  continuous turns: up to {e['turn_dps_max']} clicks/s "
                     f"(median {e['turn_dps_median']})")
        if e["gap_min_ms"] is not None:
            L.append(f"  fastest contact change after another: {e['gap_min_ms']} ms "
                     f"(p5 {e['gap_p5_ms']} ms; {e['gaps_under_i2c_read']} faster than one "
                     f"{I2C_READ_S * 1000:.1f} ms I2C read)")
        L.append(f"  pauses >= 0.3 s between two clicks: {e['hovers']}")
        a = e["accel"]
        if a.get("intervals"):
            L.append(f"  click-to-click rate (as the acceleration sees it): median {a['median_dps']}/s, "
                     f"max {a['max_dps']}/s; over 12/s {a['over_12'] * 100:.0f} %, "
                     f"over 20/s {a['over_20'] * 100:.0f} %, over 40/s {a['over_40'] * 100:.0f} %")
        for name, r in e["replay"].items():
            L.append(f"  replay {name:<34} {r['emitted']:+d} in {r['events']} events"
                     + (f", {r['timer']} by the pause timer" if r["timer"] else ""))
        v = e["verdict"]
        L.append(f"  VERDICT: steps_per_detent {v['spd']}, detent AB {v['detent']} "
                 f"(confidence {v['confidence']}, from the {v.get('basis', '?')}: "
                 f"{'; '.join(v['why'])})")
    if res["switches"]:
        L.append("")
        L.append("switches: " + "; ".join(
            f"sw{s['switch']} [{s['pin']}] {s['presses']} presses"
            + (f", {s['chatter']} chatter flips (< 5 ms)" if s["chatter"] else "")
            for s in res["switches"]))
    c = res["config"]
    L.append("")
    L.append("k1000.yaml, section controls: (per physical encoder enc0..enc3; only encoders "
             f"moved in this trace count: {c['from_encoders_moved']})")
    L.append(f"  steps_per_detent: {c['steps_per_detent']}")
    L.append("  detent_ab: [" + ", ".join(f'"{d}"' for d in c["detent_ab"]) + "]")
    return "\n".join(L)


# =============================================================================================
# Hardware: driver import, sampling
# =============================================================================================
def find_driver_root(explicit: Optional[str] = None) -> Optional[Path]:
    here = Path(__file__).resolve().parent.parent
    for c in (explicit, os.environ.get("K1000_PATH"), str(here), "/opt/k1000"):
        if c and (Path(c) / "k1000" / "hw" / "mcp23017.py").is_file():
            return Path(c)
    return None


def load_driver(explicit: Optional[str] = None) -> tuple[Any, Optional[Path]]:
    """The driver module (k1000.hw.mcp23017), or (None, None) when it cannot be imported."""
    root = find_driver_root(explicit)
    if root is None:
        return None, None
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from k1000.hw import mcp23017 as drv   # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        print(f"warning: cannot import the driver from {root}: {e}", file=sys.stderr)
        return None, None
    return drv, root


def encoder_map(path: str = "/etc/k1000/k1000.yaml") -> list[int]:
    """controls.encoders: physical encoder index of E1..E4 (the service's mapping), else 0..3."""
    try:
        import yaml  # noqa: PLC0415
        data = yaml.safe_load(Path(path).read_text()) or {}
        enc = (data.get("controls") or {}).get("encoders")
        if isinstance(enc, list) and sorted(enc) == [0, 1, 2, 3]:
            return [int(x) for x in enc]
    except Exception:  # noqa: BLE001
        pass
    return [0, 1, 2, 3]


def encoder_labels(enc_map: Sequence[int]) -> dict[str, str]:
    """{'<physical index>': 'E1 top-left', ...}"""
    return {str(phys): f"E{logical + 1} {CORNERS[logical]}" for logical, phys in enumerate(enc_map)}


def k1000_active() -> Optional[bool]:
    try:
        r = subprocess.run(["systemctl", "is-active", "--quiet", "k1000.service"], timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.returncode == 0


class RenderLoad(threading.Thread):
    """The k1000 service's CPU work beside the knob worker, as v1 runs it (PERF-4):

    - an asyncio loop thread (like k1000's) that runs the SHOW spectrum's ballistics at 10 Hz
      (rta.Ballistics over a moving program spectrum) and, every `save_every_s`, a CPU-bound
      `save_ms` chunk standing in for an urgent save (MASTER going down saves every 0.25 s;
      the pure-Python YAML dump took 34-61 ms on the Pi before PERF-3, ~13 ms after);
    - the SHOW page with that spectrum rendered at `fps` in a to_thread worker (+ RGB565),
      exactly as k1000's render loop hands frames to a thread.
    `kind="busy"` (no k1000 tree): a 10 ms busy loop per frame instead. v0's MAIN at 15 fps in
    one thread, with no loop and no spectrum, said "all is well" while the real layout lost
    clicks: never use it as evidence of knob timing."""

    def __init__(self, kind: str = "render", fps: float = 15.0, save_ms: float = 40.0,
                 save_every_s: float = 0.25) -> None:
        super().__init__(name="knobtrace-loop", daemon=True)
        self.kind, self.period = kind, 1.0 / fps
        self.save_s, self.save_every_s = save_ms / 1000.0, save_every_s
        self.stop_ev = threading.Event()
        self.frames = 0
        self.saves = 0
        self.ms: list[float] = []
        self._draw: Optional[Callable[[Any], Any]] = None
        self._spectrum: Optional[Callable[[float], Any]] = None
        if kind == "render":
            try:
                self._setup_render()
            except Exception as e:  # noqa: BLE001
                print(f"warning: renderer unavailable ({e}): busy-loop load instead", file=sys.stderr)
                self.kind = "busy"
        if self._draw is None:
            def busy(_frame: Any) -> None:
                t = time.perf_counter() + 0.010
                x = 0
                while time.perf_counter() < t:
                    x += 1
            self._draw = busy

    def _setup_render(self) -> None:
        import numpy as np  # noqa: PLC0415
        from k1000.ui import display, pages  # noqa: PLC0415
        from k1000.ui.input_map import Focus  # noqa: PLC0415
        from k1000.state import State  # noqa: PLC0415
        try:
            from k1000.rta import Ballistics  # noqa: PLC0415
            bal: Any = Ballistics()
        except Exception:  # noqa: BLE001 - an older tree: the graph's own tracker
            from k1000.ui import eqgraph  # noqa: PLC0415
            bal = eqgraph.Rta()
        st = State()
        st.master_db = -14.0
        st.live_peq[1].freq, st.live_peq[1].gain, st.live_peq[1].q = 2520.0, -6.5, 8.0
        first = pages.demo_spectrum(seconds=1.0)
        freqs, base = first.freqs, np.array(first.level)
        rng = np.random.default_rng(3)
        foc = Focus(slot=1)
        pages.warm_up()

        def spectrum(t: float) -> Any:
            bal.update(freqs, base + rng.normal(0, 2.0, len(base)), t)
            return bal.frame()

        def draw(frame: Any) -> Any:
            status = pages.ScreenStatus(profile="m4", device="MOTU M4", device_present=True,
                                        engine="Running", load=2.0, ready=True, spectrum=frame,
                                        spectrum_state="live", clip=(False,) * 4,
                                        levels=tuple(-14.0 + 3.0 * rng.random(4)))
            return display.to_rgb565(pages.render("SHOW", st, status, foc))
        self._spectrum, self._draw = spectrum, draw

    def run(self) -> None:
        import asyncio  # noqa: PLC0415
        asyncio.run(self._main())

    async def _main(self) -> None:
        import asyncio  # noqa: PLC0415
        frame: list[Any] = [None]

        async def spectrum() -> None:
            t = 0.0
            while self._spectrum is not None:
                frame[0] = self._spectrum(t)
                t += 0.1
                await asyncio.sleep(0.1)

        async def render() -> None:
            assert self._draw is not None
            while True:
                t0 = time.perf_counter()
                await asyncio.to_thread(self._draw, frame[0])
                dt = time.perf_counter() - t0
                self.frames += 1
                self.ms.append(dt * 1000)
                await asyncio.sleep(max(0.0, self.period - dt))

        async def saves() -> None:
            while self.save_s > 0:
                await asyncio.sleep(self.save_every_s)
                end = time.perf_counter() + self.save_s     # the GIL held, in the loop thread
                x = 0
                while time.perf_counter() < end:
                    x += 1
                self.saves += 1

        tasks = [asyncio.create_task(c()) for c in (spectrum, render, saves)]
        while not self.stop_ev.is_set():
            await asyncio.sleep(0.05)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def stop(self) -> None:
        self.stop_ev.set()
        self.join(timeout=3.0)


def _pct(xs: Sequence[float], q: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    return s[min(len(s) - 1, int(q * len(s)))]


class Sampler(threading.Thread):
    """Samples the chip in one mode until stop(); writes level changes to the TraceWriter."""

    POLL_RESYNC_S = 0.05   # the driver's poll_ms (resync while idle) in irq mode
    MAX_REREADS = 3
    VERIFY_S = 0.25

    def __init__(self, drv: Any, chip: Any, bus: Any, w: TraceWriter, mode: str,
                 lines: Any = None, poll_hz: float = 1000.0, load: str = "") -> None:
        super().__init__(name=f"knobtrace-{mode}", daemon=True)
        self.drv, self.chip, self.bus, self.w = drv, chip, bus, w
        self.mode, self.lines, self.poll_hz, self.load = mode, lines, poll_hz, load
        self.stop_ev = threading.Event()
        self.st: dict = collections.defaultdict(int)
        self.read_s: list[float] = []
        self.lat_s: list[float] = []
        self.gaps: list[float] = []
        self.error: Optional[str] = None

    def stop(self) -> dict:
        self.stop_ev.set()
        self.join(timeout=3.0)
        return self.summary()

    def summary(self) -> dict:
        s: dict = {"mode": self.mode, "load": self.load, **dict(self.st)}
        if self.read_s:
            s["read_ms_avg"] = round(statistics.mean(self.read_s) * 1000, 3)
            s["read_ms_max"] = round(max(self.read_s) * 1000, 3)
        if self.lat_s:
            s["lat_ms_avg"] = round(statistics.mean(self.lat_s) * 1000, 3)
            s["lat_ms_p95"] = round(_pct(self.lat_s, 0.95) * 1000, 3)
            s["lat_ms_max"] = round(max(self.lat_s) * 1000, 3)
        if self.mode == "poll" and self.gaps:
            s["asked_hz"] = self.poll_hz
            s["rate_hz"] = round(len(self.gaps) / max(sum(self.gaps), 1e-9), 1)
            s["gap_ms_max"] = round(max(self.gaps) * 1000, 3)
            s["gaps_over_5ms"] = sum(1 for g in self.gaps if g > 0.005)
        if self.error:
            s["error"] = self.error
        return s

    def _reconfigure(self, why: str) -> None:
        self.st["reconfigs"] += 1
        self.chip.configure()
        st = self.chip.read_state()
        self.w.directive("mark", {"t": self.w.now(), "phase": "note", "step": "reconfigured",
                                  "why": why})
        self.w.sample(time.monotonic(), st[4] | (st[5] << 8), "reseed", force=True)

    def run(self) -> None:
        try:
            if self.mode == "irq":
                self._run_irq()
            else:
                self._run_poll()
        except Exception as e:  # noqa: BLE001 - report, never hang the prompt thread
            self.error = f"{type(e).__name__}: {e}"

    def _guarded_read(self) -> Optional[tuple]:
        t_a = time.monotonic()
        try:
            gppu, st = self.chip.read_guarded()
        except OSError as e:
            self.st["i2c_errors"] += 1
            self.stop_ev.wait(0.02)
            try:
                self._reconfigure(f"I2C error {e}")
            except OSError:
                pass
            return None
        t_b = time.monotonic()
        self.st["reads"] += 1
        self.read_s.append(t_b - t_a)
        if gppu != 0xFFFF:
            self.st["pullup_alarms"] += 1
            self._reconfigure(f"GPPU=0x{gppu:04x}")
            return None
        return t_a, t_b, st

    def _run_irq(self) -> None:
        lines = self.lines
        last = self.w._last if self.w._last is not None else 0xFFFF
        read_ts = getattr(getattr(lines, "_req", None), "read_edge_events", None)
        while not self.stop_ev.is_set():
            edge_t: Optional[float] = None
            if lines.wait(self.POLL_RESYNC_S):
                if read_ts is not None:
                    evs = read_ts()
                    self.st["edges"] += len(evs)
                    if evs:
                        edge_t = min(ev.timestamp_ns for ev in evs) / 1e9
                else:
                    self.st["edges"] += len(lines.read_events())
            else:
                self.st["polls"] += 1
            for k in range(self.MAX_REREADS):
                r = self._guarded_read()
                if r is None:
                    break
                t_a, t_b, (intf_a, intf_b, cap_a, cap_b, gp_a, gp_b) = r
                lat = (t_b - edge_t) if (edge_t is not None and k == 0 and t_b >= edge_t) else None
                if lat is not None and lat < 1.0:
                    self.lat_s.append(lat)
                if intf_a or intf_b:
                    a = cap_a if intf_a else last & 0xFF
                    b = cap_b if intf_b else last >> 8
                    t_cap = edge_t if (edge_t is not None and k == 0 and edge_t <= t_a) else t_a
                    if self.w.sample(t_cap, a | (b << 8), "cap", lat):
                        last = a | (b << 8)
                if self.w.sample(t_b, gp_a | (gp_b << 8), "gpio", lat):
                    last = gp_a | (gp_b << 8)
                if all(lines.levels().values()):
                    break
            else:
                self.st["int_stuck"] += 1

    def _run_poll(self) -> None:
        period = 1.0 / max(1.0, self.poll_hz)
        gpioa = getattr(self.drv, "GPIOA", 0x12)
        nxt = time.monotonic()
        last_t: Optional[float] = None
        next_verify = nxt + self.VERIFY_S
        while not self.stop_ev.is_set():
            t_a = time.monotonic()
            try:
                a, b = self.bus.read_i2c_block_data(self.chip.addr, gpioa, 2)
            except OSError as e:
                self.st["i2c_errors"] += 1
                self.stop_ev.wait(0.02)
                try:
                    self._reconfigure(f"I2C error {e}")
                except OSError:
                    pass
                nxt = time.monotonic()
                continue
            t = time.monotonic()
            self.st["reads"] += 1
            self.read_s.append(t - t_a)
            if last_t is not None:
                self.gaps.append(t - last_t)
            last_t = t
            self.w.sample(t, a | (b << 8), "poll")
            if t >= next_verify:
                next_verify = t + self.VERIFY_S
                try:
                    bad = self.chip.verify()
                except OSError:
                    bad = ["read failed"]
                if bad:
                    self.st["pullup_alarms"] += 1
                    self._reconfigure("verify: " + ", ".join(bad))
            nxt += period
            delay = nxt - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            elif delay < -10 * period:
                nxt = time.monotonic()


class Rig:
    """The chip, the bus and (irq mode) the INT lines, opened like the driver opens them."""

    def __init__(self, drv: Any, bus_num: int = 1, addr: int = 0x20, int_a: int = 27,
                 int_b: int = 4, gpiochip: str = "/dev/gpiochip0") -> None:
        self.drv = drv
        self.bus = drv._default_bus(bus_num)
        self.chip = drv.Mcp23017(self.bus, addr)
        self.chip.configure()
        bad = self.chip.verify()
        if bad:
            self.bus.close()
            raise SystemExit("MCP23017 did not take its configuration: " + ", ".join(bad))
        self.int_offsets = (int_a, int_b)
        self.gpiochip = gpiochip
        self.lines: Any = None

    def open_lines(self) -> Any:
        if self.lines is None:
            self.lines = self.drv.GpiodIntLines(self.gpiochip, self.int_offsets,
                                                consumer="k1000-knobtrace")
            self.chip.read_state()             # clear INTA/INTB now that the lines are watched
        return self.lines

    def close_lines(self) -> None:
        if self.lines is not None:
            try:
                self.lines.close()
            except Exception:  # noqa: BLE001
                pass
            self.lines = None

    def levels(self) -> int:
        st = self.chip.read_state()
        return st[4] | (st[5] << 8)

    def close(self) -> None:
        self.close_lines()
        try:
            self.bus.close()
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------------------------- guided plan
@dataclass
class Step:
    id: str
    enc: int            # LOGICAL E1..E4 = 0..3
    kind: str           # slow | fine | fast
    clicks: int         # 0 = not counted
    dir: int
    mode: str           # irq | poll
    text: str


def guided_plan(quick: bool = False) -> list[Step]:
    def corner(e: int) -> str:
        return f"E{e + 1} ({CORNERS[e]})"
    p = [Step("e1-cw", 0, "slow", 10, 1, "irq",
              f"{corner(0)}: turn exactly 10 clicks CLOCKWISE, slowly, stopping on each click."),
         Step("e1-ccw", 0, "slow", 10, -1, "irq",
              f"{corner(0)}: exactly 10 clicks COUNTER-clockwise, slowly, stopping on each click.")]
    if not quick:
        p += [Step(f"e{e + 1}-cw", e, "slow", 10, 1, "irq",
                   f"{corner(e)}: exactly 10 clicks CLOCKWISE, slowly, stopping on each click.")
              for e in (1, 2, 3)]
    p += [Step("e1-fine", 0, "fine", 5, 1, "irq",
               f"{corner(0)}: 5 clicks CLOCKWISE, VERY slowly, as if fine-tuning a notch "
               "(about 2 seconds per click)."),
          Step("e1-fast", 0, "fast", 0, 0, "irq",
               f"{corner(0)}: fast spins, as in a hurry: half a turn clockwise, half a turn back, "
               "three times."),
          Step("e1-cw-poll", 0, "slow", 10, 1, "poll",
               f"(1 kHz polling now) {corner(0)}: exactly 10 clicks CLOCKWISE, slowly."),
          Step("e1-fast-poll", 0, "fast", 0, 0, "poll",
               f"(1 kHz polling) {corner(0)}: the fast spins again, three times.")]
    return p


def quick_window(w: TraceWriter, enc_phys: int, t0: float, t1: float) -> dict:
    tr = w.trace
    init, states = enc_states(tr, enc_phys)
    trs = transitions(init, states)
    return window_net(trs, t0, t1)


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip().lower()
    except EOFError:
        return "q"


def run_capture(args: argparse.Namespace) -> int:
    drv, root = load_driver(args.k1000_path)
    if drv is None:
        print("ERROR: the k1000 driver (k1000/hw/mcp23017.py) was not found: pass --k1000-path",
              file=sys.stderr)
        return 2
    active = k1000_active()
    if active and not args.force:
        print("k1000.service is RUNNING: it owns the knobs. Stop it first (the audio keeps "
              "playing, its faders do not move; B2 MUTE is off until you start it again):\n"
              "    sudo systemctl stop k1000\n"
              "then run this again, and afterwards:\n"
              "    sudo systemctl start k1000", file=sys.stderr)
        return 3
    enc_map = encoder_map(args.settings)
    labels = encoder_labels(enc_map)
    rev = "?"
    try:
        rev = (Path(root) / "REVISION").read_text().strip() if root else "?"
    except OSError:
        pass
    out = args.output or f"knobs-{time.strftime('%Y%m%d-%H%M%S')}.trace"
    meta = {"encoders": [list(drv.pin_index(p) for p in ab) for ab in drv.DEFAULT_ENCODERS],
            "switches": [drv.pin_index(p) for p in drv.DEFAULT_SWITCHES],
            "labels": labels, "host": platform.node(), "driver": str(root), "revision": rev,
            "started": time.strftime("%Y-%m-%dT%H:%M:%S"), "argv": sys.argv[1:],
            "python": platform.python_version()}
    try:
        rig = Rig(drv, args.bus, args.addr, args.int_a, args.int_b)
    except (OSError, ImportError) as e:
        print(f"ERROR: MCP23017 not reachable on i2c-{args.bus} 0x{args.addr:02x}: {e}", file=sys.stderr)
        return 1
    w = TraceWriter(out, meta, time.monotonic())
    sampler: Optional[Sampler] = None
    load: Optional[RenderLoad] = None
    cur_mode = ""

    def start(mode: str) -> None:
        nonlocal sampler, load, cur_mode
        stop_all()
        want_load = args.load if args.load != "auto" else ("render" if mode == "irq" else "none")
        lines = None
        if mode == "irq":
            try:
                lines = rig.open_lines()
            except Exception as e:  # noqa: BLE001
                raise SystemExit(f"ERROR: cannot request INT lines GPIO{args.int_a}/GPIO{args.int_b} "
                                 f"({e}): is k1000 still running?")
        else:
            rig.close_lines()
        w.sample(time.monotonic(), rig.levels(), "seed", force=True)
        if want_load in ("render", "busy"):
            load = RenderLoad(want_load)
            load.start()
        sampler = Sampler(drv, rig.chip, rig.bus, w, mode, lines, args.poll_hz,
                          load.kind if load else "")
        w.directive("mode", {"t": w.now(), "mode": mode, "load": load.kind if load else "",
                             "poll_hz": args.poll_hz if mode == "poll" else None})
        sampler.start()
        cur_mode = mode

    def stop_all() -> None:
        nonlocal sampler, load
        if sampler is not None:
            st = sampler.stop()
            if load is not None:
                st["render_frames"] = load.frames
                if load.ms:
                    st["render_ms_avg"] = round(statistics.mean(load.ms), 2)
            w.directive("stats", st)
            sampler = None
        if load is not None:
            load.stop()
            load = None

    print(f"k1000-knobtrace: writing {out} (driver {root}, rev {rev})")
    rc = 0
    try:
        if args.guided:
            plan = guided_plan(args.quick)
            print("Guided capture: ~2 minutes. For each step press Enter, do it, press Enter again.\n"
                  "Count clicks exactly: the click count is the ground truth. 'r' redoes a step, "
                  "'q' stops.")
            k = 0
            while k < len(plan):
                st = plan[k]
                if st.mode != cur_mode:
                    start(st.mode)
                phys = enc_map[st.enc]
                print(f"\n[{k + 1}/{len(plan)}] {st.text}")
                if _ask("    Enter = start ") == "q":
                    break
                t0 = w.now()
                w.directive("mark", {"t": t0, "phase": "start", "step": st.id, "enc": phys,
                                     "dir": st.dir, "clicks": st.clicks, "kind": st.kind,
                                     "mode": st.mode, "label": labels.get(str(phys), "")})
                ans = _ask("    ...turn now, then Enter = done ")
                t1 = w.now()
                q = quick_window(w, phys, t0, t1)
                msg = f"    {q['transitions']} contact changes, net {q['net']:+d}"
                if st.clicks:
                    msg += f" = {abs(q['net']) / st.clicks:.2f} per click"
                if q["invalid"]:
                    msg += f", {q['invalid']} invalid"
                print(msg)
                if sampler is not None and sampler.error:
                    print(f"    SAMPLER STOPPED: {sampler.error}")
                again = ans if ans in ("r", "q") else _ask("    Enter = next step, r = redo it ")
                w.directive("mark", {"t": t1, "phase": "end", "step": st.id,
                                     "discard": again == "r"})
                if again == "q":
                    break
                if again != "r":
                    k += 1
        else:
            start(args.mode)
            print(f"Recording ({args.mode}) for {args.seconds:.0f} s: turn the knobs. Ctrl-C stops.")
            t_stop = time.monotonic() + args.seconds
            while time.monotonic() < t_stop:
                time.sleep(0.2)
                if sampler is not None and sampler.error:
                    print(f"SAMPLER STOPPED: {sampler.error}")
                    rc = 1
                    break
    except KeyboardInterrupt:
        print("\n(stopped)")
    finally:
        stop_all()
        w.close()
        rig.close()
    res = analyze(w.trace, drv.QuadratureDecoder)
    print()
    print(report(res, out))
    if args.json:
        Path(args.json).write_text(json.dumps(res, indent=1, default=str))
    print(f"\ntrace saved: {out}\nThe knobs are still OFF for the service: sudo systemctl start k1000")
    return rc


# =============================================================================================
# bench (no hardware)
# =============================================================================================
def wake_overshoot(seconds: float, sleep_s: float = 0.002) -> list[float]:
    """How late a thread that sleeps `sleep_s` (GIL released, like gpiod's wait) gets to run
    Python again: OS scheduling + waiting for the GIL. (Only for a tree without
    k1000.hw.simchip: the knob worker itself is measured otherwise.)"""
    out = []
    t_end = time.perf_counter() + seconds
    while time.perf_counter() < t_end:
        t0 = time.perf_counter()
        time.sleep(sleep_s)
        out.append(time.perf_counter() - t0 - sleep_s)
    return out


def knob_run(drv: Any, simchip: Any, rate_cps: float, turns: int = 4, clicks: int = 12,
             load: Optional["RenderLoad"] = None) -> dict:
    """Known turns at `rate_cps` through the REAL ZynthianControls worker reading a time-driven
    MCP23017 (k1000.hw.simchip), optionally beside `load` (started and stopped here). Returns
    clicks turned/lost and the edge -> sample delays of every contact transition."""
    chip = simchip.SimChip()
    t = time.monotonic() + 0.6
    windows = []
    for k in range(turns):
        n = clicks if k % 2 == 0 else -clicks
        end = simchip.schedule_turn(chip, t, 0, n, rate_cps)
        windows.append((t, end, n))
        t = end + 0.45
    samples: list = []
    c = drv.ZynthianControls(bus_factory=lambda n: chip,
                             lines_factory=lambda name, offs: simchip.SimLines(chip, *offs),
                             on_raw=lambda lv, ch, ts: samples.append((ts, lv)), poll_ms=50)
    if load is not None:
        load.start()
    try:
        c.start(loop=None)
        while time.monotonic() < t + 0.3:
            time.sleep(0.05)
    finally:
        if load is not None:
            load.stop()
        c.close()
    evs = [e for e in c._pending if isinstance(e, drv.EncoderEvent)]
    got = [sum(e.delta for e in evs if t0 - 0.01 <= e.ts <= t1 + 0.3) for t0, t1, _n in windows]
    lat = simchip.edge_latencies(chip.transitions, samples)
    return {"rate_cps": rate_cps, "turned": sum(abs(w[2]) for w in windows),
            "lost": sum(abs(w[2] - g) for w, g in zip(windows, got)), "per_turn": got,
            "lat_ms": [x * 1000 for x in lat], "storms": c.stats.get("storms", 0),
            "load": load.kind if load else "none", "render_frames": load.frames if load else 0,
            "saves": load.saves if load else 0}


def run_bench(args: argparse.Namespace) -> int:
    drv, root = load_driver(args.k1000_path)
    switch = None
    try:
        from k1000.__main__ import configure_gil  # noqa: PLC0415 - as the service starts
        switch = args.switch_ms / 1000.0 if args.switch_ms else None
        configure_gil(*([switch] if switch else []))
    except Exception:  # noqa: BLE001 - an older tree: Python's default
        if args.switch_ms:
            sys.setswitchinterval(args.switch_ms / 1000.0)
    print(f"bench (driver tree {root}, python {platform.python_version()}, "
          f"GIL switch interval {sys.getswitchinterval() * 1000:.1f} ms)")
    res: dict = {"switch_ms": sys.getswitchinterval() * 1000, "runs": []}
    try:
        from k1000.hw import simchip  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        simchip = None
    if drv is None or simchip is None:
        idle = wake_overshoot(args.seconds / 2)
        print(f"  (no k1000.hw.simchip in this tree: thread wake-up delay only, NOT the knob "
              f"worker) idle p99 {_pct(idle, 0.99) * 1000:.2f} ms")
        res["wake_idle"] = idle
        if args.json:
            Path(args.json).write_text(json.dumps(res))
        return 0
    rates = (45.0,) if args.quick else (25.0, 35.0, 45.0)
    turns = 2 if args.quick else 4
    print("  knob worker (REAL ZynthianControls) on a time-driven MCP23017 (INTCAP, INT, 0.9 ms "
          "transfer); known turns:")
    print(f"  {'load':40s} {'c/s':>4s} {'lost':>9s}  edge->sample p50 / p95 / p99 / max (ms)")
    for name, mk in (("idle", lambda: None),
                     (f"v1 SHOW + spectrum + {args.save_ms:.0f} ms save/0.25 s",
                      lambda: RenderLoad("render" if root else "busy", save_ms=args.save_ms))):
        for rate in rates:
            r = knob_run(drv, simchip, rate, turns=turns, load=mk())
            lat = r["lat_ms"]
            print(f"  {name:40s} {rate:4.0f} {r['lost']:4d}/{r['turned']:<4d}  "
                  f"{_pct(lat, 0.5):5.2f} / {_pct(lat, 0.95):5.2f} / {_pct(lat, 0.99):5.2f} / "
                  f"{max(lat) if lat else 0.0:5.2f}"
                  + (f"   ({r['render_frames']} frames, {r['saves']} saves)" if r["load"] != "none" else ""))
            res["runs"].append({"condition": name, **r})
    print("  (a contact transition every 5.6 ms at 45 clicks/s: samples later than two of them "
          "merge into INTCAP + GPIO and can lose clicks)")
    if args.json:
        Path(args.json).write_text(json.dumps(res))
    return 0


# =============================================================================================
# CLI
# =============================================================================================
def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="k1000-knobtrace.py", description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=__doc__.split("\n\n", 1)[1])
    sub = p.add_subparsers(dest="cmd")
    c = sub.add_parser("capture", help="record the knobs (k1000.service must be stopped)")
    c.add_argument("--guided", action="store_true", help="~2 minutes of prompted turns (recommended)")
    c.add_argument("--quick", action="store_true", help="guided, E1 only (~1 minute)")
    c.add_argument("--mode", choices=("irq", "poll"), default="irq",
                   help="unguided capture: INT-driven like the driver (default) or polled")
    c.add_argument("--poll-hz", type=float, default=1000.0)
    c.add_argument("--load", choices=("auto", "render", "busy", "none"), default="auto",
                   help="screen work in a thread while sampling (auto: render in irq mode, none when polling)")
    c.add_argument("--seconds", type=float, default=30.0, help="unguided capture length")
    c.add_argument("-o", "--output", help="trace file (default knobs-<date>.trace here)")
    c.add_argument("--json", help="also write the analysis as JSON")
    c.add_argument("--force", action="store_true", help="skip the 'k1000 running' check")
    c.add_argument("--settings", default="/etc/k1000/k1000.yaml", help="for the E1..E4 labels")
    c.add_argument("--k1000-path", help="tree holding k1000/hw/mcp23017.py (default: this tool's "
                   "tree, else /opt/k1000)")
    c.add_argument("--bus", type=int, default=1)
    c.add_argument("--addr", type=lambda s: int(s, 0), default=0x20)
    c.add_argument("--int-a", type=int, default=27)
    c.add_argument("--int-b", type=int, default=4)
    a = sub.add_parser("analyze", help="analyse a saved trace (no hardware)")
    a.add_argument("trace")
    a.add_argument("--json", help="also write the analysis as JSON")
    a.add_argument("--rest-ms", type=float, default=REST_S * 1000)
    a.add_argument("--no-replay", action="store_true", help="skip the decoder replay")
    a.add_argument("--k1000-path", help="driver tree whose QuadratureDecoder is replayed")
    d = sub.add_parser("demo", help="analyse synthetic traces (what a 4- and a 2-transition encoder look like)")
    d.add_argument("--k1000-path")
    b = sub.add_parser("bench", help="the knob worker's sampling delay and lost clicks under "
                       "the v1 service load (no hardware)")
    b.add_argument("--seconds", type=float, default=4.0, help="(trees without simchip only)")
    b.add_argument("--save-ms", type=float, default=40.0,
                   help="CPU-bound chunk in the loop thread every 0.25 s (an urgent save)")
    b.add_argument("--switch-ms", type=float, default=0.0,
                   help="GIL switch interval (default: what the k1000 service sets)")
    b.add_argument("--quick", action="store_true", help="45 clicks/s only, two turns")
    b.add_argument("--json")
    b.add_argument("--k1000-path")
    args = p.parse_args(list(argv) if argv is not None else None)
    if args.cmd == "capture":
        args.guided = args.guided or args.quick
        return run_capture(args)
    if args.cmd == "analyze":
        tr = read_trace(args.trace)
        drv = None if args.no_replay else load_driver(args.k1000_path)[0]
        res = analyze(tr, drv.QuadratureDecoder if drv else None, args.rest_ms / 1000.0)
        print(report(res, args.trace))
        if args.json:
            Path(args.json).write_text(json.dumps(res, indent=1, default=str))
        return 0
    if args.cmd == "demo":
        drv = load_driver(args.k1000_path)[0]
        qd = drv.QuadratureDecoder if drv else None
        for spd in (4, 2):
            s = Synth(spd=spd)
            s.guided("e1-cw", 0, 1, 10)
            # fine-tuning: a 0.5 s pause at the top of every click, 0.6 s on each detent
            s.guided("e1-fine", 0, 1, 5, "fine",
                     body=lambda x: x.clicks(0, 1, 5, dur=0.3, hover_at=spd // 2, hover_s=0.5,
                                             pause=0.6))
            s.guided("e1-fast", 0, 0, 0, "fast",
                     body=lambda x: (x.spin(0, 1, 12, 30.0), x.spin(0, -1, 12, 30.0)))
            print(report(analyze(s.trace(), qd), f"synthetic {spd}-transition encoder"))
            print()
        return 0
    if args.cmd == "bench":
        return run_bench(args)
    p.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
