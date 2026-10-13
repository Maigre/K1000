# SPDX-License-Identifier: GPL-3.0-or-later
"""The knob worker under load, in real time (PERF-1): the REAL ZynthianControls worker thread
reading a time-driven MCP23017 (k1000.hw.simchip: INTCAP, INT lines, a 0.9 ms transfer that
releases the GIL like smbus2), while another thread holds the GIL for most of every frame like
the SHOW renderer and the event loop do. At the GIL switch interval the service sets
(k1000.__main__.configure_gil), the worker must sample a 45 clicks/s turn within one contact
transition (5.6 ms) 95 % of the time, and count it exactly.

On the box, before the fix, MASTER lost ~24 % of its clicks at 45 clicks/s with SHOW drawn: the
worker waited up to 5 ms (the default switch interval) for the GIL at each of its 3-4 GIL
reacquisitions per read, and 20-35 ms sampling gaps swallowed whole clicks. Here, the default
5 ms gives an edge -> sample p95 of ~11.4 ms (two transitions at 45 clicks/s); 1 ms gives ~3.5.

Rare scheduling stalls of the test machine itself (tens of ms, at any switch interval) can
still eat a click: a miss is retried, but only when such a stall is in the run's own trace.
"""
from __future__ import annotations

import statistics
import sys
import threading
import time

import pytest

from k1000.__main__ import GIL_SWITCH_S, configure_gil
from k1000.hw.mcp23017 import EncoderEvent, ZynthianControls
from k1000.hw.simchip import SimChip, SimLines, edge_latencies, schedule_turn

FPS = 15.0
FRAME_CPU_S = 0.030          # 45 % of a core in one thread: harsher than the box's renderer


class _Load(threading.Thread):
    """FPS frames of pure-Python work (the GIL held, as PIL/numpy glue code holds it)."""

    def __init__(self) -> None:
        super().__init__(daemon=True, name="test-render-load")
        self.stop = threading.Event()
        self.frames = 0

    def run(self) -> None:
        period = 1.0 / FPS
        nxt = time.perf_counter()
        while not self.stop.is_set():
            end = time.perf_counter() + FRAME_CPU_S
            x = 0
            while time.perf_counter() < end:
                x += 1
            self.frames += 1
            nxt += period
            self.stop.wait(max(0.0, nxt - time.perf_counter()))


def _turns(rate_cps: float) -> tuple[SimChip, list[tuple[float, float, int]], float]:
    chip = SimChip()
    t = time.monotonic() + 0.6               # after the worker and the load are up
    windows = []
    for clicks in (16, -16, 12, -12, 16, -16):
        end = schedule_turn(chip, t, 0, clicks, rate_cps)
        windows.append((t, end, clicks))
        t = end + 0.45                       # a rest between turns (no acceleration involved)
    return chip, windows, t


def _run_turns(rate_cps: float, load: bool = True) -> tuple[list[int], list[int], list[float], dict]:
    """(clicks counted per turn, clicks turned, edge -> sample delays, driver stats)."""
    chip, windows, end = _turns(rate_cps)
    samples: list[tuple[float, int]] = []
    c = ZynthianControls(bus_factory=lambda n: chip,
                         lines_factory=lambda chipname, offs: SimLines(chip, *offs),
                         on_raw=lambda lv, ch, ts: samples.append((ts, lv)), poll_ms=50)
    th = _Load()
    if load:
        th.start()
    try:
        c.start(loop=None)                   # the worker alone; its queue is read afterwards
        while time.monotonic() < end + 0.3:
            time.sleep(0.05)
    finally:
        th.stop.set()
        c.close()
    evs = [e for e in c._pending if isinstance(e, EncoderEvent)]
    got = [sum(e.delta for e in evs if t0 - 0.01 <= e.ts <= t1 + 0.3) for t0, t1, _c in windows]
    lat = edge_latencies(chip.transitions, samples)        # type: ignore[attr-defined]
    return got, [w[2] for w in windows], lat, dict(c.stats)


def _pct(xs: list[float], q: float) -> float:
    s = sorted(xs)
    return s[int(q * (len(s) - 1))]


@pytest.mark.parametrize("rate_cps", [25.0, 45.0])
def test_a_fast_turn_is_sampled_in_time_and_counted_exactly_under_load(rate_cps: float):
    period = 1.0 / (rate_cps * 4)            # one contact transition
    prev = configure_gil()
    try:
        assert sys.getswitchinterval() == pytest.approx(GIL_SWITCH_S)
        for attempt in range(3):
            got, want, lat, stats = _run_turns(rate_cps)
            p95, worst = _pct(lat, 0.95), max(lat)
            info = (f"attempt {attempt}: got {got} want {want}, edge->sample p95 "
                    f"{p95 * 1000:.1f} ms max {worst * 1000:.1f} ms, {stats}")
            assert p95 < period, info
            assert stats.get("storms", 0) == 0 and stats.get("int_stuck", 0) == 0, info
            if got == want:
                break
            # a lost click is only excused by a stall longer than two transitions in this
            # very run (the machine, not the worker): then once more
            assert worst > 2 * period, info
        else:
            pytest.fail(info)
    finally:
        sys.setswitchinterval(prev)
    assert statistics.median(lat) < period


def test_the_service_sets_a_1_ms_switch_interval_before_any_thread():
    """main() calls configure_gil() first thing: the knob worker, the renderer's to_thread and
    the phone server all start after it."""
    import inspect

    from k1000 import __main__ as svc
    src = inspect.getsource(svc.main)
    assert src.index("configure_gil()") < src.index("asyncio.run(")
    assert svc.GIL_SWITCH_S == 0.001
