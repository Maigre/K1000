# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for k1000.rta: the SHOW page's program spectrum (D-009).

- the request and the reply parsing (CamillaDSP 5.0.0-beta.1 GetSpectrum shapes);
- `Ballistics`: bars, the slow-tracker scale (no 6 dB steps), the PEAK detector (+-1 octave
  context, persistence, hysteresis, not buried, search range, a steady pointer);
- `SpectrumTask` against a fake client, driven step by step on a fake clock: polled only when
  wanted, its own client, channels and power combining, "No audio data available", a link
  down, a refusal, the pause after knob events, the idle close;
- the controller's mute never waits for a stuck spectrum link;
- `engine`: a real camilladsp (SignalGenerator sine) on the private port
  $K1000_CONTROL_TEST_PORT gives a live frame with its PEAK at the sine.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import yaml

from k1000 import rta
from k1000.cdsp import CdspError, CdspUnavailable
from k1000.rta import Ballistics, SpectrumTask, parse_spectrum, spectrum_request

FREQS = np.geomspace(rta.MIN_FREQ, rta.MAX_FREQ, rta.N_BINS)


def bin_of(hz: float) -> int:
    return int(np.argmin(np.abs(np.log2(FREQS / hz))))


def background(level: float = -50.0) -> np.ndarray:
    """A pink-ish program: falling 3 dB/octave above 100 Hz, with a little ripple."""
    m = level - 3.0 * np.log2(np.maximum(FREQS, 100.0) / 1000.0)
    return m + 1.5 * np.sin(np.arange(len(FREQS)) * 1.3)


def with_peak(hz: float, above: float, base: np.ndarray | None = None) -> np.ndarray:
    m = background() if base is None else base.copy()
    i = bin_of(hz)
    m[i] = m[i] + above
    return m


def feed(b: Ballistics, frames: list[np.ndarray] | np.ndarray, t0: float, dt: float = 0.1,
         n: int | None = None) -> float:
    """Feed frames every dt from t0; returns the time of the last one."""
    t = t0
    seq = [frames] * (n or 1) if isinstance(frames, np.ndarray) else frames
    for k, m in enumerate(seq):
        t = t0 + k * dt
        b.update(FREQS, m, t)
    return t


# ======================================================================= request / reply
def test_request_is_the_capture_side_of_one_channel():
    assert spectrum_request(2) == {"side": "capture", "channel": 2, "min_freq": 10.0,
                                   "max_freq": 20000.0, "n_bins": rta.N_BINS}
    assert rta.N_BINS == 264                       # 1/24 octave from 10 Hz to 20 kHz
    assert "channel" in spectrum_request(0) and spectrum_request(0)["channel"] is not None


def test_parse_spectrum_shapes():
    f, m = parse_spectrum({"frequencies": [10.0, 20.0, 40.0], "magnitudes": [-30.0, None, -200.0]})
    assert list(f) == [10.0, 20.0, 40.0] and list(m) == [-30.0, rta.FLOOR_DB, rta.FLOOR_DB]
    for bad in (None, [], {"frequencies": [1.0]}, {"frequencies": [1, 2], "magnitudes": [1]},
                {"frequencies": [20.0, 10.0], "magnitudes": [0, 0]},
                {"frequencies": [0.0, 10.0], "magnitudes": [0, 0]},
                {"frequencies": ["a", 10.0], "magnitudes": [0, 0]}):
        assert parse_spectrum(bad) is None, bad


# ======================================================================= ballistics
def test_bars_rise_at_once_and_fall_slowly():
    b = Ballistics()
    hot = background()
    hot[bin_of(1000)] = -10.0
    b.update(FREQS, hot, 0.0)
    assert b.level[bin_of(1000)] == pytest.approx(-10.0)
    b.update(FREQS, background(), 0.5)
    assert b.level[bin_of(1000)] == pytest.approx(-22.0)          # 24 dB/s
    fr = b.frame()
    assert fr is not None and fr.source == "PGM" and len(fr.freqs) == rta.N_BINS
    fr.level[:] = 0.0                                             # a frame is a copy
    assert b.level[bin_of(1000)] == pytest.approx(-22.0)


def test_scale_is_a_slow_tracker_without_6_db_steps():
    b = Ballistics()
    loud = background(-30.0)
    top0 = float(np.max(loud[(FREQS >= 20) & (FREQS <= 20000)])) + 18.0
    b.update(FREQS, loud, 0.0)
    assert b.top_db == pytest.approx(top0)                       # not rounded to 6 dB
    quiet = background(-60.0)
    tops = []
    for k in range(1, 36):
        b.update(FREQS, quiet, k * 0.1)
        tops.append(b.top_db)
    assert tops[-1] == pytest.approx(top0 - 3.5, abs=0.01)        # falls 1 dB/s
    assert all(a > c for a, c in zip(tops, tops[1:]))            # smoothly, every frame
    fr = b.frame()
    assert fr.hi_db == pytest.approx(b.top_db) and fr.hi_db - fr.lo_db == pytest.approx(66.0)
    b.update(FREQS, background(-10.0), 4.0)                      # rises at once
    assert b.top_db == pytest.approx(min(18.0, float(np.max(background(-10.0))) + 18.0))
    b.update(FREQS, np.full(len(FREQS), rta.FLOOR_DB), 100.0)    # silence: bounded
    assert b.top_db >= -60.0


def test_peak_needs_1_5_s_and_points_at_its_bin():
    b = Ballistics()
    m = with_peak(2440.0, 20.0)
    t = feed(b, m, 0.0, n=15)                                      # 0.0 .. 1.4 s
    assert t == pytest.approx(1.4) and b.peak_hz is None
    b.update(FREQS, m, 1.5)
    assert b.peak_hz == pytest.approx(FREQS[bin_of(2440.0)])
    assert abs(math.log2(b.peak_hz / 2440.0)) < 1 / 48
    assert b.frame().peak_hz == b.peak_hz


def test_peak_hysteresis_and_release():
    b = Ballistics()
    feed(b, with_peak(2440.0, 20.0), 0.0, n=16)
    assert b.peak_hz is not None
    t = feed(b, with_peak(2440.0, 8.0), 1.6, n=50)               # between EXIT (6) and ENTER (10)
    assert b.peak_hz is not None, "kept while above the exit threshold"
    t0 = t + 0.1
    feed(b, with_peak(2440.0, 2.0), t0, n=3)                      # dropped: not at once
    assert b.peak_hz is not None
    feed(b, with_peak(2440.0, 2.0), t0 + 0.3, n=25)
    assert b.peak_hz is None
    # a new +8 dB bump is not enough to enter
    feed(b, with_peak(5000.0, 8.0), t0 + 3.0, n=30)
    assert b.peak_hz is None


def test_peak_pointer_is_steady_on_one_bin_jitter():
    b = Ballistics()
    feed(b, with_peak(2440.0, 20.0), 0.0, n=16)
    shown = b.peak_hz
    i = bin_of(2440.0)
    alt = [with_peak(FREQS[i + (k % 2)], 20.0) for k in range(20)]
    feed(b, alt, 1.6)
    assert b.peak_hz == shown


@pytest.mark.parametrize("case", ["buried", "low", "high", "wanders", "plateau"])
def test_no_peak_for(case: str):
    b = Ballistics()
    if case == "buried":                     # 35 dB under the loudest bin: not a PEAK
        m = background(-60.0)
        m[bin_of(60.0)] = -10.0
        m[bin_of(3000.0)] += 15.0
        m[bin_of(3000.0)] = min(m[bin_of(3000.0)], -46.0)
        frames: Any = [m] * 30
    elif case == "low":
        frames = [with_peak(30.0, 25.0)] * 30
    elif case == "high":
        frames = [with_peak(18000.0, 25.0)] * 30
    elif case == "wanders":                  # a moving tone (a sweep) never persists
        frames = [with_peak(500.0 * 2 ** (k * 4 / 24), 20.0) for k in range(30)]
    else:                                    # +12 dB over three octaves is context, not a peak
        m = background()
        m[(FREQS >= 300) & (FREQS <= 2400)] += 12.0
        frames = [m] * 30
    feed(b, frames, 0.0)
    if case == "buried":
        assert b.peak_hz is None or abs(math.log2(b.peak_hz / 3000.0)) > 0.1
    else:
        assert b.peak_hz is None, case


# ======================================================================= the task
class Clock:
    def __init__(self, t: float = 100.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeSpecClient:
    """The CamillaClient surface SpectrumTask uses."""

    def __init__(self) -> None:
        self.up = True
        self.connected = False
        self.calls: list[dict[str, Any]] = []
        self.connects = 0
        self.closed = 0
        self.error: Exception | None = None       # raised by the next GetSpectrum calls
        self.levels = {0: -30.0, 1: -40.0, 2: -30.0, 3: -40.0}
        self.reply: Any = None                    # a raw reply to return instead

    async def connect(self, timeout: float | None = None) -> bool:
        self.connects += 1
        self.connected = self.up
        return self.connected

    async def command(self, name: str, timeout: float | None = None, **fields: Any) -> Any:
        assert name == "GetSpectrum"
        self.calls.append(fields["value"])
        if not self.connected:
            raise CdspUnavailable("down")
        if self.error is not None:
            raise self.error
        if self.reply is not None:
            return self.reply
        lv = self.levels[fields["value"]["channel"]]
        mags = background(lv)
        return {"frequencies": FREQS.tolist(), "magnitudes": mags.tolist()}

    async def close(self) -> None:
        self.closed += 1
        self.connected = False


def make_task(want: list[bool] | None = None, chans: Any = (2, 3)) -> tuple[SpectrumTask, Any, Clock, list, list]:
    want = [True] if want is None else want
    clk = Clock()
    made: list[FakeSpecClient] = []
    published: list[tuple[Any, str]] = []

    def factory(url: str) -> FakeSpecClient:
        assert url == "ws://127.0.0.1:9"
        made.append(FakeSpecClient())
        return made[-1]

    task = SpectrumTask("ws://127.0.0.1:9", wanted=lambda: want[0], channels=lambda: list(chans),
                        publish=lambda f, s: published.append((f, s)), client_factory=factory,
                        clock=clk)
    return task, want, clk, made, published


def run(coro: Any) -> Any:
    return asyncio.run(asyncio.wait_for(coro, 20.0))


def test_not_wanted_means_no_connection_and_no_poll():
    async def go() -> None:
        task, want, clk, made, published = make_task([False])
        for _ in range(5):
            await task.step()
            clk.t += 0.1
        assert made == [] and published == [] and task.state == rta.OFF and task.frame is None
    run(go())


def test_polls_each_program_channel_and_power_combines():
    async def go() -> None:
        task, want, clk, made, published = make_task(chans=(3, 2, 2))
        await task.step()
        (client,) = made
        assert client.calls == [spectrum_request(2), spectrum_request(3)]
        assert task.state == rta.LIVE and task.polls == 1
        frame, state = published[-1]
        assert state == rta.LIVE and frame is task.frame and frame is not None
        i = bin_of(1000.0)
        want_db = 10 * math.log10((10 ** (background(-30.0)[i] / 10) + 10 ** (background(-40.0)[i] / 10)) / 2)
        assert frame.level[i] == pytest.approx(want_db)
        assert task.client is client and client.connects == 1
    run(go())


def test_no_audio_data_is_retried_quietly(caplog: pytest.LogCaptureFixture):
    async def go() -> None:
        task, want, clk, made, published = make_task()
        await task.step()
        made[0].error = CdspError("GetSpectrum", "InvalidRequestError", "No audio data available")
        task.ballistics.reset()
        task._last_data = -math.inf
        task.frame = None
        with caplog.at_level(logging.INFO, logger="k1000.rta"):
            for _ in range(3):
                clk.t += 0.1
                await task.step()
        assert task.state == rta.WAITING and task.frame is None
        assert not [r for r in caplog.records if r.name == "k1000.rta"]
        made[0].error = None
        clk.t += 0.1
        await task.step()
        assert task.state == rta.LIVE and task.frame is not None
    run(go())


def test_link_down_clears_the_frame_after_stale_s_and_recovers():
    async def go() -> None:
        task, want, clk, made, published = make_task()
        await task.step()
        assert task.frame is not None
        made[0].up = False
        made[0].connected = False
        clk.t += 0.5
        await task.step()
        assert task.frame is not None                    # a blip: the picture stays
        clk.t += 0.6
        await task.step()
        assert task.frame is None and task.state == rta.WAITING
        assert published[-1] == (None, rta.WAITING)      # never a frozen spectrum
        made[0].up = True
        clk.t += 0.1
        await task.step()
        assert task.state == rta.LIVE and task.frame is not None and len(made) == 1
    run(go())


def test_a_refusal_is_logged_once_and_retried_later(caplog: pytest.LogCaptureFixture):
    async def go() -> None:
        task, want, clk, made, published = make_task()
        await task.step()
        made[0].error = CdspError("GetSpectrum", "InvalidRequestError",
                                  "Channel 2 out of range (2 channels available)")
        with caplog.at_level(logging.WARNING, logger="k1000.rta"):
            clk.t += 0.1
            await task.step()
            assert task.state == rta.UNAVAILABLE and task.frame is None
            n = len(made[0].calls)
            for _ in range(10):                          # within RETRY_BAD_S: not asked again
                clk.t += 0.1
                await task.step()
            assert len(made[0].calls) == n
            clk.t += rta.RETRY_BAD_S
            await task.step()
            assert len(made[0].calls) > n
        assert len([r for r in caplog.records if "unavailable" in r.getMessage()]) == 1
        made[0].error = None
        made[0].reply = {"frequencies": [1, 2], "magnitudes": []}     # garbage: also refused
        clk.t += rta.RETRY_BAD_S
        await task.step()
        assert task.state == rta.UNAVAILABLE
        task2, *_rest = make_task(chans=())
        await task2.step()
        assert task2.state == rta.UNAVAILABLE
    run(go())


def test_knob_events_hold_the_next_frame_for_300_ms():
    async def go() -> None:
        task, want, clk, made, published = make_task()
        await task.step()
        n = len(published)
        task.note_input()
        for _ in range(2):                               # 0.1, 0.2 s: polled, not drawn
            clk.t += 0.1
            await task.step()
        assert len(published) == n and task.polls == 3
        clk.t += 0.15                                    # 0.35 s: drawn again
        await task.step()
        assert len(published) == n + 1 and published[-1][0] is task.frame
    run(go())


def test_leaving_show_clears_then_closes_the_connection():
    async def go() -> None:
        task, want, clk, made, published = make_task()
        await task.step()
        want[0] = False
        clk.t += 0.1
        await task.step()
        assert published[-1] == (None, rta.OFF) and task.frame is None
        assert task.client is not None                  # kept a while (SHOW comes back often)
        clk.t += rta.IDLE_CLOSE_S
        await task.step()
        assert task.client is None and made[0].closed == 1
        want[0] = True                                   # back on SHOW: a fresh link, fresh scale
        clk.t += 1.0
        await task.step()
        assert len(made) == 2 and task.state == rta.LIVE
    run(go())


def test_the_loop_survives_a_broken_publish_and_stops_cleanly():
    async def go() -> None:
        calls: list[Any] = []

        def bad(frame: Any, state: str) -> None:
            calls.append(state)
            raise RuntimeError("renderer bug")

        client = FakeSpecClient()
        task = SpectrumTask("ws://x", wanted=lambda: True, channels=lambda: [0], publish=bad,
                            client_factory=lambda url: client, rate_hz=50.0)
        task.start()
        await asyncio.sleep(0.2)
        assert task.polls >= 3 and calls
        await task.stop()
        assert client.closed == 1 and task.client is None
        n = task.polls
        await asyncio.sleep(0.05)
        assert task.polls == n
    run(go())


def test_a_stuck_spectrum_link_never_delays_the_mute(tmp_path: Path):
    """Its own client: a GetSpectrum that never answers holds only the spectrum task."""
    from test_control import boot, make, until

    class StuckClient(FakeSpecClient):
        async def command(self, name: str, timeout: float | None = None, **fields: Any) -> Any:
            self.calls.append(fields["value"])
            await asyncio.Event().wait()

    async def go() -> None:
        from k1000.state import State
        ctl, eng = make(tmp_path, State(), playing=True)
        await boot(ctl)
        stuck = StuckClient()
        task = SpectrumTask("ws://x", wanted=ctl.spectrum_wanted, channels=lambda: [0, 1],
                            publish=ctl.set_spectrum, client_factory=lambda url: stuck)
        ctl.rta = task
        await ctl.poll_once()
        task.start()
        await until(lambda: stuck.calls, what="spectrum poll in flight")
        t0 = asyncio.get_running_loop().time()
        ctl.set_mute(True)
        await until(lambda: eng.faders[0]["mute"] is True, timeout=1.0, what="mute")
        assert asyncio.get_running_loop().time() - t0 < 0.2
        ctl.nudge("master_db", -3)
        assert await ctl.settle(2.0)
        await task.stop()
        await ctl.stop()
    run(go())


# ======================================================================= engine
@pytest.mark.engine
def test_engine_spectrum_of_a_sine_has_its_peak(tmp_path: Path):
    from k1000 import dspconfig as dc
    from k1000.cdsp import CamillaClient
    from k1000.profiles import load_profile
    from k1000.state import State
    from test_control import Engine

    if not os.access("/usr/local/bin/camilladsp", os.X_OK):
        pytest.skip("no camilladsp binary")
    cfg = dc.signal_generator_variant(dc.generate(load_profile("dev", {}), State()), level_db=-20.0,
                                      freq=1000.0)
    path = tmp_path / "rta.yml"
    path.write_text(yaml.safe_dump(cfg))
    eng = Engine(path, tmp_path / "cdsp.log")
    eng.start()
    states: list[str] = []
    frames: list[Any] = []

    def publish(frame: Any, state: str) -> None:
        states.append(state)
        if frame is not None:
            frames.append(frame)

    async def go() -> None:
        task = SpectrumTask(eng.url, wanted=lambda: True, channels=lambda: [0, 1], publish=publish,
                            client_factory=lambda u: CamillaClient(u, timeout=0.5, backoff_min=0.05,
                                                                   backoff_max=0.2))
        task.start()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 8.0
        while loop.time() < deadline and not (frames and frames[-1].peak_hz):
            await asyncio.sleep(0.1)
        await task.stop()

    try:
        run(go())
    finally:
        eng.kill()
    assert rta.LIVE in states and frames, (states[-5:], (tmp_path / "cdsp.log").read_text()[-500:])
    assert rta.UNAVAILABLE not in states          # the fresh engine's first answer was understood
    fr = frames[-1]
    assert len(fr.freqs) == rta.N_BINS and fr.freqs[0] == pytest.approx(10.0, rel=0.01)
    i = int(np.argmax(fr.level))
    assert abs(math.log2(fr.freqs[i] / 1000.0)) < 1 / 12
    assert fr.peak_hz is not None and abs(math.log2(fr.peak_hz / 1000.0)) < 1 / 12
