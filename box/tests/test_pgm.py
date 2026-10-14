# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for RESPONSE, the box side (docs/14-phone-v2-protocol.md §3.6): the program spectrum
averaged in power over fixed windows, served to the v2 phones that measure.

- `rta.PgmAccumulator`: the power mean (-10 and -20 dB average to -12.6, not -15), k / dt /
  prog, windows that tile time, a stall after a full window (closed first, `dt` without the
  stall, a stale window dropped), a new grid or a clock going back drops the window in
  progress, fresh arrays and a fixed footprint;
- `rta.SpectrumTask`: the same polls feed it (pre-ballistics, the engine's own estimator), only
  while a phone measures, on the ONE spectrum connection (no extra engine call); a state change
  drops the window in progress; with the PEAK's program gate off (-140), RESPONSE still judges
  the program at -60;
- `Controller`: `spectrum_wanted()` with only a RESPONSE subscriber, `pgm_info`, `set_pgm`;
- `server`: `pgm_message` (int16 * 100, clamped), `hello.pgm` present / null, `sub {pgm}`
  (independent of rta, BAD types, the ack), no frame without a sub, the idle lapse
  (`unsub {what: "pgm"}`), the `st` transitions (engine down -> off, link down -> waiting), the
  backpressure drop, and a mute ack never behind a window.

No pytest-asyncio on the box (apt only): each test drives its own asyncio.run().
"""
from __future__ import annotations

import asyncio
import base64
import json
import math
import struct
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from websockets.asyncio.client import connect as ws_connect

from k1000 import rta
from k1000.rta import PgmAccumulator, PgmWindow, SpectrumTask, fft_length, pgm_info
from k1000.web import server as web
from k1000.web.server import PhoneServer, PhoneSession, pgm_message
from test_rta import FREQS, Clock, FakeSpecClient, background
from test_web import boot, make_ctl, recv_until, settings, until
from test_web_v2 import FakeRta, _RecWS, origin, recv_until_t, reply, send, v2url
from test_web_v2 import Clock as SrvClock

N = rta.N_BINS


def run(coro: Any) -> Any:
    return asyncio.run(asyncio.wait_for(coro, 30.0))


def flat(db: float) -> np.ndarray:
    return np.full(N, float(db))


def window(seq: int = 3, db: Any = None, k: int = 5, dt: float = 0.5,
           prog: bool | None = True) -> PgmWindow:
    return PgmWindow(seq, 10.0, 20000.0, k, dt, prog, flat(-30.0) if db is None else np.asarray(db))


def decode_m(m: str) -> list[int]:
    raw = base64.b64decode(m)
    return list(struct.unpack(f"<{len(raw) // 2}h", raw))


# ============================================================================ the grid
def test_fft_length_and_hello_info() -> None:
    assert fft_length(48000, 10.0) == 8192          # 4800 -> 8192 (spectrum.rs)
    assert fft_length(44100, 10.0) == 8192
    assert fft_length(96000, 10.0) == 16384
    assert fft_length(40960, 10.0) == 4096          # an exact power of two stays
    assert fft_length(40970, 10.0) == 8192
    assert pgm_info() == {"v": 1, "n": 264, "f0": 10.0, "f1": 20000.0, "rate": 48000,
                          "fft": 8192, "est": "peak", "win_s": 0.5}
    assert N == 264 and rta.PGM_WIN_S == 0.5


# ============================================================================ the accumulator
def test_a_window_averages_in_power() -> None:
    acc = PgmAccumulator()
    assert acc.add(FREQS, flat(-10.0), True, 0.0) is None
    assert acc.add(FREQS, flat(-20.0), True, 0.25) is None
    w = acc.add(FREQS, flat(-60.0), True, 0.5)      # closes the window (without itself)
    assert w is not None and w.k == 2 and w.dt == pytest.approx(0.5) and w.seq == 1
    want = 10 * math.log10((10 ** -1 + 10 ** -2) / 2)     # -12.60 dB, not the dB mean -15
    assert want == pytest.approx(-12.596, abs=1e-3)
    assert np.allclose(w.db, want) and w.n == N
    assert (w.f0, w.f1) == (pytest.approx(10.0), pytest.approx(20000.0))
    # per bin, not across bins
    acc = PgmAccumulator()
    a, b = flat(-30.0), flat(-30.0)
    a[7], b[7] = 0.0, -100.0
    acc.add(FREQS, a, True, 0.0)
    acc.add(FREQS, b, True, 0.1)
    w = acc.add(FREQS, a, True, 0.5)
    assert w is not None and w.db[7] == pytest.approx(10 * math.log10((1 + 1e-10) / 2))
    assert w.db[0] == pytest.approx(-30.0)


def test_windows_tile_time_with_k_and_dt() -> None:
    acc = PgmAccumulator()
    got: list[PgmWindow] = []
    for i in range(31):                               # 10 Hz for 3 s
        w = acc.add(FREQS, flat(-40.0), True, round(i * 0.1, 6))
        if w is not None:
            got.append(w)
    assert [w.seq for w in got] == [1, 2, 3, 4, 5, 6]
    assert all(w.k == 5 and w.dt == pytest.approx(0.5) for w in got)
    # a slow poller: fewer polls, the time they cover said honestly
    acc = PgmAccumulator()
    for t in (0.0, 0.3):
        assert acc.add(FREQS, flat(-40.0), None, t) is None
    w = acc.add(FREQS, flat(-40.0), None, 0.6)
    assert w is not None and w.k == 2 and w.dt == pytest.approx(0.6) and w.seq == 1


@pytest.mark.parametrize("progs, want", [
    ((True, True, True), True),
    ((True, False, True), False),
    ((True, None, True), None),
    ((None, None), None),
    ((None, False), False),
    ((False, False), False),
])
def test_prog_over_the_window(progs: tuple[Any, ...], want: Any) -> None:
    acc = PgmAccumulator()
    for i, p in enumerate(progs):
        assert acc.add(FREQS, flat(-40.0), p, i * 0.1) is None
    w = acc.add(FREQS, flat(-40.0), True, 0.5)
    assert w is not None and w.k == len(progs) and w.prog is want
    # the next window starts clean (the closing poll said True)
    w2 = acc.add(FREQS, flat(-40.0), True, 1.0)
    assert w2 is not None and w2.prog is True and w2.k == 1


def test_a_hole_or_a_new_grid_drops_the_window_in_progress() -> None:
    acc = PgmAccumulator()
    acc.add(FREQS, flat(-10.0), True, 0.0)
    acc.add(FREQS, flat(-10.0), True, 0.1)
    assert acc.add(FREQS, flat(-50.0), True, 0.8) is None     # data to 0.2, closed 0.6 s later: stale
    for t in (0.9, 1.0, 1.1, 1.2):
        assert acc.add(FREQS, flat(-50.0), True, t) is None
    w = acc.add(FREQS, flat(-50.0), True, 1.3)
    assert w is not None and w.k == 5 and np.allclose(w.db, -50.0) and w.seq == 1
    # a hole before the window is full (a shorter max_gap_s than the window): dropped
    acc = PgmAccumulator(win_s=0.5, max_gap_s=0.2)
    acc.add(FREQS, flat(-10.0), True, 0.0)
    acc.add(FREQS, flat(-10.0), True, 0.1)
    assert acc.add(FREQS, flat(-50.0), True, 0.4) is None and acc._k == 1 and acc._t0 == 0.4
    acc = PgmAccumulator()
    # another grid (n changed): dropped too
    acc.add(FREQS, flat(-50.0), True, 2.0)
    assert acc.add(FREQS[:100], np.full(100, -20.0), True, 2.1) is None
    w = acc.add(FREQS[:100], np.full(100, -20.0), True, 2.6)
    assert w is not None and w.n == 100 and w.k == 1
    # a clock that went back: dropped
    acc.add(FREQS, flat(-50.0), True, 5.0)
    assert acc.add(FREQS, flat(-50.0), True, 4.0) is None and acc._k == 1
    acc.reset()
    assert not acc.active


@pytest.mark.parametrize("late, sent", [
    (0.5, True),       # on time
    (0.9, True),       # a 0.5 s stall after a full window: dt 0.5, not 0.9 (review 2026-10-14)
    (0.95, True),      # a 0.55 s stall: the full window is no longer lost (review 2026-10-14)
    (1.0, True),       # its data ended 0.5 s before: still fresh
    (1.05, False),     # 0.55 s after its data: stale, dropped
    (10.0, False),     # a long stall: stale
])
def test_a_stall_after_a_full_window(late: float, sent: bool) -> None:
    """The close comes before the hole test, and `dt` counts the polls, not the stall after
    them: a window that holds 0.5 s of data says 0.5 and is sent unless it is stale."""
    acc = PgmAccumulator()
    for i in range(5):
        assert acc.add(FREQS, flat(-40.0), True, round(i * 0.1, 6)) is None
    w = acc.add(FREQS, flat(-40.0), True, late)
    if sent:
        assert w is not None and w.seq == 1 and w.k == 5 and w.dt == pytest.approx(0.5)
    else:
        assert w is None and acc.seq == 0
    # either way the late poll starts the next window, and it tiles from there
    assert acc._k == 1 and acc._t0 == late
    for i in range(1, 5):
        assert acc.add(FREQS, flat(-40.0), True, round(late + i * 0.1, 6)) is None
    w2 = acc.add(FREQS, flat(-40.0), True, round(late + 0.5, 6))
    assert w2 is not None and w2.seq == (2 if sent else 1) and w2.k == 5
    assert w2.dt == pytest.approx(0.5)


def test_dt_is_the_time_the_polls_cover() -> None:
    # a slow but steady poller (0.13 s): its own interval, not the nominal 0.1 s
    acc = PgmAccumulator()
    for t in (0.0, 0.13, 0.26, 0.39):
        assert acc.add(FREQS, flat(-40.0), True, t) is None
    w = acc.add(FREQS, flat(-40.0), True, 0.52)
    assert w is not None and w.k == 4 and w.dt == pytest.approx(0.52)
    # one poll in a window: it stands for one poll period (the poller's, given)
    acc = PgmAccumulator(poll_s=0.2)
    acc.add(FREQS, flat(-40.0), True, 0.0)
    w = acc.add(FREQS, flat(-40.0), True, 0.6)
    assert w is not None and w.k == 1 and w.dt == pytest.approx(0.2)
    # a poll that stalls mid-window (under max_gap_s) then catches up: never past the closing poll
    acc = PgmAccumulator()
    for t in (0.0, 0.1, 0.45):
        assert acc.add(FREQS, flat(-40.0), True, t) is None
    w = acc.add(FREQS, flat(-40.0), True, 0.55)
    assert w is not None and w.k == 3 and w.dt == pytest.approx(0.55)
    # the poller's period reaches the task's accumulator
    task = SpectrumTask("ws://127.0.0.1:9", wanted=lambda: True, channels=lambda: [2, 3],
                        publish=lambda f, s: None, rate_hz=4.0)
    assert task.pgm.poll_s == pytest.approx(0.25) and task.pgm.max_gap_s == rta.PGM_MAX_GAP_S


def test_windows_are_fresh_and_the_footprint_is_fixed() -> None:
    acc = PgmAccumulator()
    wins: list[PgmWindow] = []
    for i in range(20_000):                           # ~33 min at 10 Hz
        w = acc.add(FREQS, background(-40.0 - (i % 7)), i % 2 == 0, i * 0.1)
        if w is not None:
            wins.append(w)
        assert acc._pow is None or acc._pow.shape == (N,)
        assert acc._k <= 6
    assert len(wins) == 3999 and wins[-1].seq == 3999
    first = wins[0].db.copy()
    acc.add(FREQS, flat(0.0), True, 1e9)
    assert np.array_equal(wins[0].db, first)          # never written again
    assert not np.shares_memory(wins[0].db, wins[1].db)


# ============================================================================ the task
def make_task(pgm_on: list[bool]) -> tuple[SpectrumTask, Clock, list[FakeSpecClient], list[PgmWindow], list]:
    clk = Clock()
    made: list[FakeSpecClient] = []
    wins: list[PgmWindow] = []
    published: list[tuple[Any, str]] = []

    def factory(url: str) -> FakeSpecClient:
        made.append(FakeSpecClient())
        return made[-1]

    task = SpectrumTask("ws://127.0.0.1:9", wanted=lambda: True, channels=lambda: [2, 3],
                        publish=lambda f, s: published.append((f, s)), client_factory=factory,
                        clock=clk, pgm_wanted=lambda: pgm_on[0], publish_pgm=wins.append)
    return task, clk, made, wins, published


async def steps(task: SpectrumTask, clk: Clock, n: int, dt: float = 0.1) -> None:
    for _ in range(n):
        await task.step()
        clk.t = round(clk.t + dt, 6)


def test_the_task_feeds_windows_only_while_a_phone_measures() -> None:
    async def go() -> None:
        pgm_on = [False]
        task, clk, made, wins, _ = make_task(pgm_on)
        await steps(task, clk, 12)
        assert wins == [] and not task.pgm.active and task.state == rta.LIVE
        (client,) = made
        calls_per_poll = (len(client.calls) + len(client.rms_calls)) / task.polls
        pgm_on[0] = True
        await steps(task, clk, 16)                    # 1.6 s: three windows close
        assert [w.seq for w in wins] == [1, 2, 3]
        assert all(w.k == 5 and w.dt == pytest.approx(0.5) and w.prog is True for w in wins)
        # the raw combined program (L -30, R -40 dB: their power mean), not the ballistics
        i = 120
        want = 10 * math.log10((10 ** (background(-30.0)[i] / 10) + 10 ** (background(-40.0)[i] / 10)) / 2)
        assert wins[-1].db[i] == pytest.approx(want)
        # ONE engine connection, and no extra engine call for RESPONSE
        assert made == [client] and task.client is client
        assert (len(client.calls) + len(client.rms_calls)) / task.polls == calls_per_poll == 3
        # nobody measures any more: the window in progress is dropped, nothing more is sent
        await steps(task, clk, 2)
        assert task.pgm.active
        pgm_on[0] = False
        await steps(task, clk, 10)
        assert len(wins) == 3 and not task.pgm.active
    run(go())


def test_the_window_is_the_raw_spectrum_not_the_ballistics() -> None:
    async def go() -> None:
        task, clk, made, wins, _ = make_task([True])
        await task.step()
        (client,) = made
        task.pgm.reset()                              # start the window with the test's polls
        clk.t += 0.1
        for k in range(5):
            client.mags = flat(-20.0 if k == 0 else -80.0)
            await task.step()
            clk.t = round(clk.t + 0.1, 6)
        await task.step()                             # closes the window
        assert task.ballistics.level is not None and task.ballistics.level[50] > -70.0  # falling slowly
        w = wins[-1]
        assert w.k == 5 and w.db[50] == pytest.approx(10 * math.log10((10 ** -2 + 4 * 10 ** -8) / 5))
    run(go())


def test_a_state_change_drops_the_window_in_progress() -> None:
    async def go() -> None:
        task, clk, made, wins, published = make_task([True])
        await steps(task, clk, 8)                     # one window closed, one in progress
        assert len(wins) == 1 and task.pgm.active
        (client,) = made
        client.connected = client.up = False          # the link drops
        await steps(task, clk, int(rta.STALE_S / 0.1) + 2)
        assert task.state == rta.WAITING and not task.pgm.active
        assert published[-1] == (None, rta.WAITING)
        client.up = True
        await steps(task, clk, 7)                     # data again: a fresh window, no hole in it
        assert len(wins) == 2 and wins[-1].k == 5 and wins[-1].seq == 2
    run(go())


def test_response_still_sees_the_program_with_the_peak_gate_off() -> None:
    """k1000.yaml `program_floor_db: -140` turns the PEAK's program gate off (docs/02 says to
    lower it if quiet program gets no pointer). RESPONSE must not go blind with it (review
    2026-10-14: every window said prog null, and the page never added one): while a phone
    measures, the level is still asked, judged at the default -60 dBFS; the PEAK's gate stays
    off; nobody measuring, the level is not asked at all."""
    async def go() -> None:
        pgm_on = [False]
        task, clk, made, wins, _ = make_task(pgm_on)
        task.program_floor_db = rta.FLOOR_RANGE[0]
        await steps(task, clk, 8)
        (client,) = made
        assert client.rms_calls == [] and task.program_db is None and wins == []
        calls_per_poll = len(client.calls) / task.polls
        pgm_on[0] = True
        await steps(task, clk, 11)                    # music at -20 dBFS RMS: two windows
        assert [w.prog for w in wins] == [True, True]
        assert task.ballistics.program is None        # the PEAK's gate is still off
        assert len(client.rms_calls) == 11 and len(client.calls) / task.polls == calls_per_poll
        client.rms = [-70.0] * 4                      # under the default floor (and its hysteresis)
        await steps(task, clk, 10)
        assert wins[-1].prog is False and task.ballistics.program is None
        # a lowered floor that still gates (not off): RESPONSE follows it, like the PEAK
        task.program_floor_db = -75.0
        await steps(task, clk, 10)
        assert wins[-1].prog is True and task.ballistics.program is True
        # the engine cannot say the level: null, as before
        client.rms_error = rta.CdspError("GetCaptureSignalRmsSince", "InvalidRequestError", "unknown")
        await steps(task, clk, 10)
        assert wins[-1].prog is None
    run(go())


def test_a_broken_publish_or_wanted_never_stops_the_poller() -> None:
    async def go() -> None:
        task, clk, made, wins, _ = make_task([True])

        def boom(w: PgmWindow) -> None:
            raise RuntimeError("phone gone")
        task.publish_pgm = boom
        await steps(task, clk, 12)                    # windows close, the publish fails: polls go on
        assert task.state == rta.LIVE and task.polls == 12

        def bad_wanted() -> bool:
            raise RuntimeError("nope")
        task.pgm_wanted = bad_wanted
        await steps(task, clk, 3)
        assert not task.pgm.active and task.state == rta.LIVE
    run(go())


# ============================================================================ the controller
def test_spectrum_is_wanted_with_only_a_response_subscriber(tmp_path: Path) -> None:
    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        await ctl.poll_once()
        ctl.has_screen = False                        # headless: nobody looks
        assert ctl.spectrum_wanted() is False and ctl.pgm_wanted() is False
        ctl.set_pgm_phones(1)
        assert ctl.spectrum_wanted() is True and ctl.pgm_wanted() is True
        ctl.has_screen = True
        ctl.goto_page("ALIGN")                        # a screen, not on SHOW
        assert ctl.spectrum_wanted() is True
        ctl.state.spectrum_on = False                 # SETUP > Spectrum OFF still stops it
        assert ctl.spectrum_wanted() is False
        ctl.state.spectrum_on = True
        ctl.status.engine = "Inactive"                # the engine not processing
        assert ctl.spectrum_wanted() is False
        ctl.status.engine = "Running"
        assert ctl.spectrum_wanted() is True
        ctl.set_pgm(window(seq=4))
        assert ctl.pgm_window is not None
        ctl.set_pgm_phones(0)
        assert ctl.spectrum_wanted() is False and ctl.pgm_window is None
        await ctl.stop()
    run(go())


def test_hello_pgm_present_or_null(tmp_path: Path) -> None:
    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        bridge = web.PhoneBridge(ctl)
        assert ctl.rta is None and bridge.hello2(True)["pgm"] is None        # no spectrum task

        class OldRta:                                 # a task without RESPONSE
            def note_input(self) -> None:
                pass
        ctl.rta = OldRta()
        assert bridge.hello2(True)["pgm"] is None
        ctl.rta = SpectrumTask("ws://x", wanted=lambda: False, channels=lambda: [0, 1],
                               publish=lambda f, s: None)
        h = bridge.hello2(True)
        assert h["pgm"] == {"v": 1, "n": 264, "f0": 10.0, "f1": 20000.0, "rate": 48000,
                            "fft": 8192, "est": "peak", "win_s": 0.5}
        assert list(h["pgm"]) == ["v", "n", "f0", "f1", "rate", "fft", "est", "win_s"]
        assert h["pgm"]["rate"] == h["fs"]
        json.dumps(h, allow_nan=False)
        ctl.spectrum_enabled = False                  # k1000.yaml spectrum.enabled: false
        assert bridge.hello2(True)["pgm"] is None
        ctl.spectrum_enabled = True
        ctl.rta = None
        await ctl.stop()
    run(go())


def test_state_sys_eq_follows_the_system_eq(tmp_path: Path) -> None:
    """state.sys_eq (docs/14 §3.2): the system EQ set from /tune changes what the PA plays, so
    the v2 state says it changed (an opaque digest) and a page measuring RESPONSE restarts
    (§3.6 point 4); the same EQ gives the same digest, and the state is pushed (version)."""
    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)

        def sys_eq() -> Any:
            return web.PhoneBridge.state_message2(ctl.snapshot(), phone_on=True, phones=1)["sys_eq"]
        d0, v0 = sys_eq(), ctl.version
        assert isinstance(d0, str) and len(d0) == 8 and int(d0, 16) >= 0
        assert sys_eq() == d0                                     # stable
        flat_eq = ctl.system_eq()
        bands = [dict(b) for b in flat_eq]
        bands[3]["gain"] = -2.5
        ctl.set_system_eq(bands)
        d1 = sys_eq()
        assert d1 != d0 and ctl.version > v0                      # changed, and pushed
        v1 = ctl.version
        ctl.set_system_eq(bands)                                  # the same EQ: nothing new
        assert sys_eq() == d1 and ctl.version == v1
        ctl.set_system_eq(flat_eq)                                # back (the tune's undo)
        assert sys_eq() == d0
        # the live EQ is not the system EQ: a band edit leaves the digest alone
        ctl.state.live_peq[0].gain = -4.0
        assert sys_eq() == d0
        assert web._sys_eq_id(object()) is None
        await ctl.stop()
    run(go())


# ============================================================================ the wire
def test_pgm_message_encoding() -> None:
    db = np.linspace(-20.0, -140.0, N)
    db[0], db[1], db[2], db[3], db[4], db[5] = -12.34, 400.0, -400.0, float("nan"), float("-inf"), 12.345678
    m = pgm_message(PgmWindow(17, 10.0, 20000.0, 5, 0.50012, True, db), "live", 99)
    json.dumps(m, allow_nan=False)
    assert list(m) == ["t", "seq", "st", "n", "f0", "f1", "k", "dt", "prog", "m"]
    assert (m["t"], m["seq"], m["st"], m["n"], m["f0"], m["f1"], m["k"], m["dt"], m["prog"]) == (
        "pgm", 17, "live", 264, 10.0, 20000.0, 5, 0.5, True)
    v = decode_m(m["m"])
    assert len(v) == 264 and len(base64.b64decode(m["m"])) == 528
    assert v[:6] == [-1234, 32767, -32768, -32768, -32768, 1235]
    assert v[6] == round(db[6] * 100) and v[-1] == -14000
    assert all(abs(v[i] / 100 - db[i]) <= 0.005 for i in range(6, N))
    assert len(json.dumps(m)) < 900
    assert pgm_message(window(prog=None), "live")["prog"] is None
    assert pgm_message(window(prog=False), "live")["prog"] is False
    # not live: the state only, the last window's number
    for st in ("waiting", "off", "unavailable", "disabled"):
        e = pgm_message(None, st, 12)
        assert e == {"t": "pgm", "seq": 12, "st": st, "n": 0, "m": None, "k": 0, "prog": None}
    # never "live" without data
    assert pgm_message(None, "live", 1)["st"] == "waiting"
    assert pgm_message(window(k=0), "live", 1)["st"] == "waiting"
    assert pgm_message(window(db=[1.0]), "live", 1)["m"] is None


def _setup_server(tmp_path: Path, **kw: Any) -> Any:
    ctl, _ = make_ctl(tmp_path)
    return ctl, PhoneServer(ctl, settings(tmp_path), tick_s=0.02, **kw)


async def pgm_until(ws: Any, st: str) -> dict[str, Any]:
    return await recv_until(ws, lambda m: m["t"] == "pgm" and m["st"] == st)


def test_sub_pgm_frames_only_to_subscribers(tmp_path: Path) -> None:
    async def go() -> None:
        ctl, srv = _setup_server(tmp_path)
        await boot(ctl)
        await ctl.poll_once()
        ctl.rta = FakeRta()                           # a task stand-in: the state is set_spectrum's
        ctl.goto_page("ALIGN")
        await srv.start()
        assert srv._on_pgm in ctl.pgm_hooks
        async with ws_connect(v2url(srv), origin=origin(srv)) as ws, \
                ws_connect(v2url(srv), origin=origin(srv)) as rta_only, \
                ws_connect(v2url(srv), origin=origin(srv)) as quiet:
            for w in (ws, rta_only, quiet):
                await recv_until(w, lambda m: m["t"] == "state")
            assert ctl.spectrum_wanted() is False
            # types: nothing changes on a bad sub
            await send(ws, t="sub", pgm="yes", id=0)
            assert (await reply(ws, 0))["code"] == "BAD"
            await send(ws, t="sub", rta=True, pgm=1, id=1)
            assert (await reply(ws, 1))["code"] == "BAD" and ctl.spectrum_phones == 0
            await send(ws, t="sub", id=2)
            assert (await reply(ws, 2))["code"] == "BAD"
            # subscribe: its own ack, its own count, the spectrum wanted for it alone
            await send(ws, t="sub", pgm=True, id=3)
            ack = await reply(ws, 3)
            assert ack["v"] == {"pgm": True, "pgm_st": "off"}
            assert ctl.pgm_phones == 1 and ctl.spectrum_phones == 0 and ctl.spectrum_wanted() is True
            first = await recv_until_t(ws, "pgm", 1.0)          # not live: the state at once
            assert first == {"t": "pgm", "seq": 0, "st": "off", "n": 0, "m": None, "k": 0, "prog": None}
            await send(rta_only, t="sub", rta=True, id=4)
            assert (await reply(rta_only, 4))["v"] == {"rta": True, "st": "off"}   # unchanged shape
            ctl.set_spectrum(None, "live")
            ctl.set_pgm(window(seq=1))
            got = await recv_until_t(ws, "pgm", 1.0)
            assert got["st"] == "live" and got["seq"] == 1 and decode_m(got["m"])[0] == -3000
            for w in (rta_only, quiet):                           # never to a non-subscriber
                with pytest.raises((TimeoutError, asyncio.TimeoutError)):
                    await recv_until_t(w, "pgm", 0.3)
            # both keys in one sub; rta is independent
            await send(ws, t="sub", rta=True, pgm=True, id=5)
            assert (await reply(ws, 5))["v"] == {"rta": True, "st": "live", "pgm": True, "pgm_st": "live"}
            await send(ws, t="sub", rta=False, id=6)
            assert (await reply(ws, 6))["v"] == {"rta": False, "st": "live"}
            assert ctl.pgm_phones == 1
            ctl.set_pgm(window(seq=2))
            assert (await recv_until_t(ws, "pgm", 1.0))["seq"] == 2
            # unsubscribe: no more windows
            await send(ws, t="sub", pgm=False, id=7)
            assert (await reply(ws, 7))["v"] == {"pgm": False, "pgm_st": "live"}
            assert ctl.pgm_phones == 0
            ctl.set_pgm(window(seq=3))
            with pytest.raises((TimeoutError, asyncio.TimeoutError)):
                await recv_until_t(ws, "pgm", 0.3)
            # live when it subscribes again: no old window, the next one
            await send(ws, t="sub", pgm=True, id=8)
            await reply(ws, 8)
            with pytest.raises((TimeoutError, asyncio.TimeoutError)):
                await recv_until_t(ws, "pgm", 0.3)
            ctl.set_pgm(window(seq=4))
            assert (await recv_until_t(ws, "pgm", 1.0))["seq"] == 4
        await until(lambda: srv.phones == 0, what="phones gone")
        assert ctl.pgm_phones == 0 and ctl.spectrum_phones == 0       # a disconnect unsubscribes
        await srv.stop()
        assert srv._on_pgm not in ctl.pgm_hooks
        await ctl.stop()
    run(go())


def test_pgm_lapses_like_rta(tmp_path: Path) -> None:
    async def go() -> None:
        clk = SrvClock(time.monotonic())
        ctl, srv = _setup_server(tmp_path, clock=clk)
        await boot(ctl)
        await ctl.poll_once()
        await srv.start()
        idle = web.RTA_SUB_IDLE_S
        async with ws_connect(v2url(srv), origin=origin(srv)) as ws:
            assert json.loads(await ws.recv())["rta"]["idle_s"] == idle
            await send(ws, t="sub", pgm=True, rta=True, id=1)
            await reply(ws, 1)
            for _ in range(3):                                    # pings keep it
                clk.t += idle * 0.8
                await send(ws, t="ping", ts=1)
                await recv_until(ws, lambda m: m["t"] == "pong")
            await asyncio.sleep(0.1)
            assert ctl.pgm_phones == 1
            clk.t += idle + 0.5
            notes = [await recv_until(ws, lambda m: m["t"] == "unsub") for _ in range(2)]
            assert {"t": "unsub", "what": "pgm", "why": "idle"} in notes
            assert {"t": "unsub", "what": "rta", "why": "idle"} in notes
            assert ctl.pgm_phones == 0 and ctl.spectrum_phones == 0
            ctl.set_pgm(window(seq=9))
            with pytest.raises((TimeoutError, asyncio.TimeoutError)):
                await recv_until_t(ws, "pgm", 0.3)
            await send(ws, t="sub", pgm=True, id=2)
            await reply(ws, 2)
            assert ctl.pgm_phones == 1
        await srv.stop()
        await ctl.stop()
    run(go())


def test_pgm_states_follow_the_poller(tmp_path: Path) -> None:
    """engine down -> off, link down -> waiting, back -> the next window; a disabled spectrum
    says disabled. Through the real task, controller and server."""
    async def go() -> None:
        ctl, srv = _setup_server(tmp_path)
        await boot(ctl)
        await ctl.poll_once()
        ctl.has_screen = False
        clk = Clock(1000.0)
        made: list[FakeSpecClient] = []

        def factory(url: str) -> FakeSpecClient:
            made.append(FakeSpecClient())
            return made[-1]
        task = SpectrumTask("ws://x", wanted=ctl.spectrum_wanted, channels=lambda: [0, 1],
                            publish=ctl.set_spectrum, client_factory=factory, clock=clk,
                            pgm_wanted=ctl.pgm_wanted, publish_pgm=ctl.set_pgm)
        ctl.rta = task
        await srv.start()
        async with ws_connect(v2url(srv), origin=origin(srv)) as ws:
            h = json.loads(await ws.recv())
            assert h["pgm"]["fft"] == 8192
            await send(ws, t="sub", pgm=True, id=1)
            assert (await reply(ws, 1))["v"] == {"pgm": True, "pgm_st": "off"}
            assert (await recv_until_t(ws, "pgm", 1.0))["st"] == "off"
            await steps(task, clk, 6)                             # wanted now: one window
            live = await pgm_until(ws, "live")
            assert (live["seq"], live["k"], live["dt"], live["prog"], live["n"]) == (1, 5, 0.5, True, 264)
            assert live["f0"] == pytest.approx(10.0) and live["f1"] == pytest.approx(20000.0)
            assert made and len(made) == 1                        # the one spectrum connection
            # the engine stops processing: not wanted -> off after KEEP_S, with the last window's seq
            ctl.status.engine = "Inactive"
            await steps(task, clk, int(rta.KEEP_S / 0.1) + 2)
            off = await pgm_until(ws, "off")
            assert off == {"t": "pgm", "seq": 1, "st": "off", "n": 0, "m": None, "k": 0, "prog": None}
            ctl.status.engine = "Running"
            await steps(task, clk, 7)
            assert (await pgm_until(ws, "live"))["seq"] == 2
            # the spectrum link drops: waiting after STALE_S, said once
            made[-1].up = made[-1].connected = False
            await steps(task, clk, int(rta.STALE_S / 0.1) + 2)
            assert (await pgm_until(ws, "waiting"))["seq"] == 2
            with pytest.raises((TimeoutError, asyncio.TimeoutError)):
                await recv_until_t(ws, "pgm", 0.3)
            made[-1].up = True
            await steps(task, clk, 7)
            back = await pgm_until(ws, "live")
            assert back["seq"] == 3 and back["k"] == 5            # a fresh window, no hole in it
            # spectrum disabled in k1000.yaml: said to the phones measuring, and on a new sub
            ctl.spectrum_enabled = False
            assert (await pgm_until(ws, "disabled"))["n"] == 0
            await send(ws, t="sub", pgm=False, id=2)
            await reply(ws, 2)
            await send(ws, t="sub", pgm=True, id=3)
            assert (await reply(ws, 3))["v"]["pgm_st"] == "disabled"
            assert (await recv_until_t(ws, "pgm", 1.0))["st"] == "disabled"
            ctl.spectrum_enabled = True
        await srv.stop()
        await task.stop()
        ctl.rta = None
        await ctl.stop()
    run(go())


def test_pgm_is_dropped_under_backpressure_and_never_ahead_of_a_reply(tmp_path: Path) -> None:
    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        ctl.rta = FakeRta()
        srv = PhoneServer(ctl, settings(tmp_path))
        ctl.spectrum_hooks.append(srv._on_spectrum)              # what start() does
        ctl.pgm_hooks.append(srv._on_pgm)
        ws = _RecWS()
        c = web._Client(ws, "10.42.0.9", web._Bucket(30.0, 40, time.monotonic), v=2, n=1,
                        sess=PhoneSession(n=1, rta=True, pgm=True))
        srv._clients.add(c)
        srv._refresh(force=True)
        ctl.set_spectrum(None, "live")                            # an rta frame waits too
        ctl.set_pgm(window(seq=1))
        srv._queue(c, {"t": "ack", "id": "mute", "v": True})
        task = asyncio.get_running_loop().create_task(srv._sender2(c))
        await until(lambda: any(m["t"] == "rta" for m in ws.sent), what="frames sent")
        order = [m["t"] for m in ws.sent]
        assert order.index("ack") < order.index("state") < order.index("m") < order.index("pgm") \
            < order.index("rta")
        # backpressure: more than 4 kB on their way -> the window is dropped, never queued
        ws.backlog = 10_000
        n_pgm = sum(1 for m in ws.sent if m["t"] == "pgm")
        ctl.set_pgm(window(seq=2))
        await until(lambda: srv.stats["pgm_dropped"] >= 1, what="window dropped")
        assert sum(1 for m in ws.sent if m["t"] == "pgm") == n_pgm
        # ...while a mute ack still goes out at once
        srv._queue(c, {"t": "ack", "id": "mute", "v": False})
        await until(lambda: ws.sent[-1] == {"t": "ack", "id": "mute", "v": False}, what="ack sent")
        ws.backlog = 0
        ctl.set_pgm(window(seq=3))
        await until(lambda: ws.sent[-1].get("t") == "pgm", what="next window")
        assert ws.sent[-1]["seq"] == 3                           # 2 lost: a seq gap
        assert srv.stats["pgm_sent"] == 2
        # latest only: two windows before the sender runs -> only the second
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        ctl.set_pgm(window(seq=4))
        ctl.set_pgm(window(seq=5))
        task = asyncio.get_running_loop().create_task(srv._sender2(c))
        c.kick.set()
        await until(lambda: ws.sent[-1].get("seq") == 5, what="latest window")
        assert [m["seq"] for m in ws.sent if m["t"] == "pgm"] == [1, 3, 5]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await ctl.stop()
    run(go())


def test_a_stuck_spectrum_link_never_delays_the_mute_while_measuring(tmp_path: Path) -> None:
    """RESPONSE adds nothing to the controller's link: a measuring phone with a GetSpectrum
    that never answers holds only the spectrum task."""
    from test_control import boot as cboot, make, until as cuntil

    class StuckClient(FakeSpecClient):
        async def command(self, name: str, timeout: float | None = None, **fields: Any) -> Any:
            self.calls.append(fields.get("value"))
            await asyncio.Event().wait()

    async def go() -> None:
        from k1000.state import State
        ctl, eng = make(tmp_path, State(), playing=True)
        await cboot(ctl)
        stuck = StuckClient()
        task = SpectrumTask("ws://x", wanted=ctl.spectrum_wanted, channels=lambda: [0, 1],
                            publish=ctl.set_spectrum, client_factory=lambda url: stuck,
                            pgm_wanted=ctl.pgm_wanted, publish_pgm=ctl.set_pgm)
        ctl.rta = task
        ctl.set_pgm_phones(1)
        await ctl.poll_once()
        task.start()
        await cuntil(lambda: stuck.calls, what="spectrum poll in flight")
        t0 = asyncio.get_running_loop().time()
        ctl.set_mute(True)
        await cuntil(lambda: eng.faders[0]["mute"] is True, timeout=1.0, what="mute")
        assert asyncio.get_running_loop().time() - t0 < 0.2
        await task.stop()
        ctl.rta = None
        await ctl.stop()
    run(go())
