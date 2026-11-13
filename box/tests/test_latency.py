# SPDX-License-Identifier: GPL-3.0-or-later
"""The D-018 latency readout (k1000/latency.py): the estimate, its journal lines, the frames in
USB transit read from the kernel (fixture files, no /proc), the dropout count read from the
engine's live log, and where it shows (the controller's status, the box's status bar, the phone's
protocol 2). No engine, no hardware: what only the box can prove (the estimate against the loop
cable, the real log's rotation, the transit at other chunks) is listed in latency.py's docstring."""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import random
import re
import threading
from pathlib import Path
from typing import Any

import pytest

from k1000 import latency as lat
from k1000 import state as st
from k1000.cdsp import CdspError
from k1000.control import Controller, Paths, Status
from k1000.latency import (DropoutCounter, LatencyWatch, Reading, io_share_ms, latency_limit_ms,
                           read_transit, run_devices, scan, stream_dir)
from k1000.profiles import load_profile, load_settings
from k1000.ui import pages
from k1000.ui.input_map import Focus
from k1000.web.server import PhoneBridge
from test_control import DEV, FakeClient, boot, fast_sleep, run, until
import k1000.dspconfig as dc

BOX = Path(__file__).resolve().parents[1]
CFG = {"devices": {"samplerate": 48000, "chunksize": 256, "target_level": 512}}
START = b"2026-10-13 04:59:15.339777 INFO  [camilladsp] <src/bin.rs:463> CamillaDSP version 5.0.0-beta.1\n"
UNDERRUN = (b"2026-10-13 05:00:15.123456 WARN  [camilladsp::alsa_backend::device] "
            b"<src/alsa_backend/device.rs:126> PB: Prepare playback after buffer underrun\n")
OVERRUN = b"2026-10-13 05:00:16.000001 WARN  Capture: read overrun\n"
CLIP = b"2026-10-13 05:00:17.000001 WARN  [camilladsp::processing] Clipping detected, 3 samples\n"
# The engine's first line as the next5 source writes it (src/bin.rs: `info!("CamillaDSP version {}
# ({})")` through custom_logger_format). Read in the source, not yet in a log of the next5 build.
START_NEXT5 = (b"2026-11-12 18:00:00.000001 INFO  [camilladsp] <src/bin.rs:463> "
               b"CamillaDSP version 5.0.0-beta.2 (v5.0.0-beta.1-58-g0930f9b)\n")
RS = 1000                       # a small rotation size for the tests: the box's is 256000


# ======================================================================= the estimate
def test_the_bench_figure_is_the_estimate() -> None:
    """16 Oct, loop cable: 20.28 ms = engine 16.5 (chunk 256 + buffer 536 at 48 kHz) + M4 3.8."""
    r = Reading(536, 256, 48000.0, io_ms=3.8)
    assert r.engine_ms == pytest.approx(16.5) and r.ms == 20.3 and r.over
    assert r.line() == "latency: buffer=536 frames, about 20.3 ms in to out (limit 10.0)"
    floor = Reading(536, 256, 48000.0, io_ms=None, limit_ms=25.0)
    assert floor.ms == 16.5 and not floor.over
    assert floor.line().startswith("latency: buffer=536 frames, at least 16.5 ms in to out (engine only")
    assert not Reading(128, 128, 48000.0, io_ms=3.8).over                  # 9.1 ms: within D-018
    assert Reading(240, 128, 48000.0, io_ms=3.8, limit_ms=11.4).ms == 11.5


def test_the_interface_share_fits_the_bench_of_12_nov() -> None:
    """Loop measurements on the box, both engines, K1000 in the loop (chunk, level, measured ms):
    in to out = (level + chunk) / rate + io_latency_ms + a quarter chunk, within 0.3 ms."""
    bench = ((256, 536, 20.20), (256, 296, 15.20), (128, 292, 11.70), (128, 340, 12.70),
             (128, 173, 9.24), (64, 126, 6.55), (64, 178, 7.76), (64, 62, 5.22), (32, 61, 4.34),
             (32, 92, 5.01))
    for chunk, level, measured in bench:
        io = io_share_ms(2.3, chunk, 48000.0)
        est = Reading(level, chunk, 48000.0, io_ms=io).ms
        assert abs(est - measured) <= 0.3, (chunk, level, est, measured)
    assert io_share_ms(None, 256, 48000.0) is None
    assert io_share_ms(2.3, 256, 48000.0) == pytest.approx(3.633, abs=1e-3)
    w = LatencyWatch()
    assert w.io_share(2.3) == 2.3 and w.io_share(None) is None      # no running config yet
    w.note_config({"devices": {"chunksize": 64, "samplerate": 48000}})
    assert w.io_share(2.3) == pytest.approx(2.633, abs=1e-3)


def test_limit_and_devices_are_read_tolerantly() -> None:
    assert latency_limit_ms({}) == 10.0 == lat.LIMIT_MS
    assert latency_limit_ms({"engine": {"latency_limit_ms": 12}}) == 12.0
    for junk in ("x", True, None, float("nan"), float("inf"), [1]):
        assert latency_limit_ms({"engine": {"latency_limit_ms": junk}}) == 10.0, junk
    assert latency_limit_ms({"engine": "junk"}) == 10.0 and latency_limit_ms(None) == 10.0
    assert latency_limit_ms({"engine": {"latency_limit_ms": 0}}) == 1.0           # bounded
    assert latency_limit_ms(load_settings(BOX / "etc" / "k1000.yaml")) == 10.0  # as shipped
    assert run_devices(CFG) == (256, 48000.0)
    for bad in (None, {}, {"devices": None}, {"devices": {"chunksize": 0, "samplerate": 48000}},
                {"devices": {"chunksize": 256.0, "samplerate": 48000}},
                {"devices": {"chunksize": True, "samplerate": 48000}},
                {"devices": {"chunksize": 256, "samplerate": "48000"}}):
        assert run_devices(bad) is None, bad


def test_io_latency_is_a_profile_number() -> None:
    """Measured at one rate, given per rate (D-022): 1.9 ms at 96 kHz (no settings file, or the
    shipped one), 2.3 ms at 48 kHz (a settings file without `samplerate`)."""
    assert load_profile("m4", {}, probe=False).io_latency_ms == 1.9
    assert load_profile("m4", {"profile": "m4"}, probe=False).io_latency_ms == 2.3
    assert load_profile("dev", {}).io_latency_ms is None
    shipped = load_settings(BOX / "etc" / "k1000.yaml")
    assert load_profile("m4", shipped, probe=False).io_latency_ms == 1.9
    assert load_profile("dev", shipped).io_latency_ms is None
    assert load_profile("m4", {"profiles": {"m4": {"io_latency_ms": 5}}}, probe=False).io_latency_ms == 5.0
    assert load_profile("m4", {"profiles": {"m4": {"io_latency_ms": None}}}, probe=False).io_latency_ms is None
    assert load_profile("dev", {"profiles": {"dev": {"io_latency_ms": 1.2}}}).io_latency_ms == 1.2
    for junk in ("3.8", True, -1, 51, float("nan")):
        p = load_profile("m4", {"profiles": {"m4": {"io_latency_ms": junk}}}, probe=False)
        assert p.io_latency_ms == 2.3 and any("io_latency_ms" in w for w in p.warnings), junk


def test_io_transit_is_a_profile_number_per_chunk() -> None:
    """The transit io_latency_ms was measured with, at each chunk where it was read, as the
    readout's figure gives it at rest (`transit_figure`): built in for the m4 at 48 kHz, chunk
    64, and at 96 kHz, chunk 128 (1.13 ms both), so a settings file written before it
    (io_latency_ms alone) counts the transit unchanged; `null` turns it off."""
    def m4(**kw: Any) -> Any:
        return load_profile("m4", {"profiles": {"m4": kw}}, probe=False)

    assert load_profile("m4", {}, probe=False).io_transit_ms == {128: 1.13}
    assert load_profile("m4", {"profile": "m4"}, probe=False).io_transit_ms == {64: 1.13}
    assert load_profile("dev", {}).io_transit_ms is None
    shipped = load_settings(BOX / "etc" / "k1000.yaml")
    assert load_profile("m4", shipped, probe=False).io_transit_ms == {128: 1.13}
    assert load_profile("dev", shipped).io_transit_ms is None
    p = m4(chunksize=64, target_level=128, io_latency_ms=2.3)          # a file from before
    assert (p.io_latency_ms, p.io_transit_ms, p.warnings) == (2.3, {64: 1.13}, [])
    assert m4(io_transit_ms=None).io_transit_ms is None and m4(io_transit_ms={}).io_transit_ms is None
    p = m4(io_transit_ms={64: 1, 128: 1.25})
    assert p.io_transit_ms == {64: 1.0, 128: 1.25} and p.warnings == []
    assert load_profile("m4", {}, probe=False).io_transit_ms is not p.io_transit_ms
    for junk in (1.0, "1.0", True, [64], {"64": 1.0}, {65: 1.0}, {True: 1.0}, {64: -1},
                 {64: 51}, {64: float("nan")}, {64: "1.0"}, {64: None}, {64: 1.0, 2048: 1.0}):
        p = m4(io_transit_ms=junk)
        assert p.io_transit_ms == {64: 1.13} and any("io_transit_ms" in w for w in p.warnings), junk


def test_an_io_latency_of_its_own_does_not_take_the_builtin_transit() -> None:
    """The built-in 1.13 ms is what sat in transit when the built-in 2.3 ms was measured: a new
    loop measurement written alone has no transit to count from, so none is read. The same at
    96 kHz with 1.9 ms."""
    def m4(**kw: Any) -> Any:
        return load_profile("m4", {"profiles": {"m4": kw}}, probe=False)

    p = m4(io_latency_ms=3.1)
    assert (p.io_latency_ms, p.io_transit_ms, p.warnings) == (3.1, None, [])
    assert m4(io_latency_ms=3.1, io_transit_ms={64: 1.2}).io_transit_ms == {64: 1.2}
    assert m4(io_latency_ms=2.3).io_transit_ms == {64: 1.13}          # the built-in figure
    p = m4(io_latency_ms="3.1")                                        # refused: the built-in
    assert p.io_latency_ms == 2.3 and p.io_transit_ms == {64: 1.13}
    assert m4(samplerate=96000, io_latency_ms=1.9).io_transit_ms == {128: 1.13}
    assert m4(samplerate=96000, io_latency_ms=2.3).io_transit_ms is None   # 48 kHz's, not 96's
    assert m4(io_latency_ms=None).io_transit_ms is None
    p = m4(io_latency_ms=3.1, io_transit_ms=1.2)
    assert p.io_transit_ms is None and any("io_transit_ms" in w and "using None" in w
                                           for w in p.warnings)
    assert load_profile("dev", {"profiles": {"dev": {"io_latency_ms": 1.2}}}).io_transit_ms is None


def test_the_transit_counts_only_at_a_chunk_its_reference_was_read() -> None:
    """The 48 frames were read at chunk 64 only. If the transit at rest differs at another chunk,
    the quarter chunk of the bench's fit may already hold the difference: there the transit is
    neither read nor counted until a reading at rest is added for that chunk."""
    w = LatencyWatch()
    refs = {64: 1.0, 128: 1.25}
    assert w.usb_ref(refs) is None                          # no running config yet
    w.note_config(M4_64)
    assert w.usb_ref(refs) == 1.0
    w.note_config({"devices": {"chunksize": 128, "samplerate": 48000}})
    assert w.usb_ref(refs) == 1.25
    w.note_config(CFG)                                      # chunk 256, as shipped
    assert w.usb_ref(refs) is None and w.usb_ref({64: 1.0}) is None
    for none in (None, {}, 1.0, "x"):
        assert w.usb_ref(none) is None, none


# ======================================================================= the journal
class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_journal_lines_at_connect_on_a_move_and_every_ten_minutes(caplog: Any) -> None:
    clock = Clock()
    w = LatencyWatch(10.0, clock=clock)
    caplog.set_level(logging.INFO, logger="k1000.latency")

    def lines() -> list[str]:
        return [r.getMessage() for r in caplog.records if r.name == "k1000.latency"]

    assert w.note("Running", 536, 3.8) is None                  # the config not read yet
    w.note_config(CFG)
    assert w.note("Running", 0, 3.8) is None                    # no average published yet
    assert w.note("Paused", 536, 3.8) is None and w.note("Running", None, 3.8) is None
    assert w.note("Running", True, 3.8) is None and w.note("Running", "536", 3.8) is None
    assert lines() == []
    r = w.note("Running", 536, 3.8)
    assert r is not None and r.ms == 20.3 and w.reading == r
    assert lines() == ["latency: buffer=536 frames, about 20.3 ms in to out (limit 10.0)"]
    for level in (540, 600, 780, 300):                          # within one chunk of 536: quiet
        clock.t += 10.0
        w.note("Running", level, 3.8)
    assert len(lines()) == 1
    clock.t += 10.0
    w.note("Running", 793, 3.8)                                 # 257 frames: more than a chunk
    assert len(lines()) == 2 and "buffer=793" in lines()[-1]
    clock.t += lat.LOG_EVERY_S - 1.0
    w.note("Running", 793, 3.8)
    assert len(lines()) == 2
    clock.t += 1.0
    w.note("Running", 793, 3.8)                                 # ten minutes: a line anyway
    assert len(lines()) == 3
    w.note("Running", 793, None)                                # the interface share changed
    assert len(lines()) == 4 and "at least" in lines()[-1]
    w.lost()                                                    # link gone: devices forgotten
    assert w.reading is None and w.note("Running", 793, 3.8) is None
    w.note_config(CFG)
    w.note("Running", 793, 3.8)                                 # reconnected: a line again
    assert len(lines()) == 5


# ======================================================================= the USB transit
# The kernel's files, read on the box with the engine playing (docs/17 §9): fill = 256 - 75 = 181
# frames (the engine's level that night), transit = 241 - 181 = 60.
STATUS = """state: RUNNING
owner_pid   : 544211
trigger_time: 58770.748315426
tstamp      : 0.000000000
delay       : 241
avail       : 75
avail_max   : 259
-----
hw_ptr      : 41463499
appl_ptr    : 41463680
"""
HW_PARAMS = """access: RW_INTERLEAVED
format: S32_LE
subformat: STD
channels: 4
rate: 48000 (48000/1)
period_size: 32
buffer_size: 256
"""
M4_64 = {"devices": {"chunksize": 64, "samplerate": 48000}}
STREAM = "58770.748315426"          # the sample's trigger_time: one start of the stream
JIT = (48, 60) * (lat.USB_BURST // 2)        # a burst at rest: the night's two figures (54.0)
SHIFT = (144, 156) * (lat.USB_BURST // 2)    # the same, 96 frames deeper (docs/17 §3.4): 150.0
# The reference the m4 profile gives at 48 kHz, chunk 64: the figure at rest (1.13 ms, D-022).
REF_64 = load_profile("m4", {"profile": "m4"}, probe=False).io_transit_ms[64]
# Single reads at rest, read on the box on 13 Nov with this reader (bursts of 20 reads, a window
# of 100): frames -> % of the reads.
BOX_48K_64 = {60: 46.4, 72: 51.9, 12: 1.0}       # 3620 reads; the loop read 0.25 ms more than usual
BOX_96K_128 = {96: 46.0, 120: 50.0, 24: 2.7}     # three engine starts of 820 reads each, alike
BOX_96K_128_ODD = {120: 73.9, 144: 11.2, 24: 14.0}   # the fourth start: the loop read 0.16 ms more
# The usual state at 48 kHz, chunk 64 (the pair the night run read, 48 / 60 frames), with the
# shares of the 3620 reads: a model built from them, not a read distribution.
USUAL_48K_64 = {48: 46.4, 60: 51.9, 0: 1.0}


def burst(frames: int | tuple[int, ...], rate: float = 48000.0,
          stream: str = STREAM) -> lat.Burst:
    """One burst of reads of one stream: `frames` USB_BURST times, or the reads given."""
    reads = (frames,) * lat.USB_BURST if isinstance(frames, int) else tuple(frames)
    return reads, rate, stream


def _stream(root: Path, status: str = STATUS, hw: str = HW_PARAMS, card: str = "M4") -> Path:
    d = root / card / "pcm0p" / "sub0"
    d.mkdir(parents=True, exist_ok=True)
    (d / "status").write_text(status)
    (d / "hw_params").write_text(hw)
    return d


def test_the_stream_follows_the_playback_device(tmp_path: Path) -> None:
    m4 = tmp_path / "M4" / "pcm0p" / "sub0"
    for name in ("hw:M4", " hw:M4 ", "hw:M4,0", "hw:M4,0,0", "hw:CARD=M4", "hw:CARD=M4,DEV=0",
                 "hw:CARD=M4,DEV=0,SUBDEV=-1", "plughw:M4", "plughw:CARD=M4,DEV=0"):
        assert stream_dir(name, tmp_path) == m4, name
    assert stream_dir("hw:1,0", tmp_path) == tmp_path / "card1" / "pcm0p" / "sub0"
    assert stream_dir("hw:CARD=M4,DEV=1,SUBDEV=2", tmp_path) == tmp_path / "M4" / "pcm1p" / "sub2"
    assert stream_dir("hw:M4") == Path("/proc/asound/M4/pcm0p/sub0")
    for other in ("default", "dmix:M4", "sysdefault:CARD=M4", "front:M4", "pulse", "hw:", "hw:M4,x",
                  "hw:../M4", "hw:M4,0,0,0", "hw:CARD=M4,FOO=1", "hw:M4,DEV=0", "hw:CARD=,DEV=0",
                  "", None, 4):
        assert stream_dir(other, tmp_path) is None, other               # type: ignore[arg-type]


def test_the_transit_is_the_kernel_delay_beyond_the_buffer_fill(tmp_path: Path) -> None:
    d = _stream(tmp_path)
    assert read_transit("hw:M4", tmp_path) == (60, 48000.0, STREAM)     # the box's sample
    assert read_transit("hw:CARD=M4,DEV=0", tmp_path) == (60, 48000.0, STREAM)
    (d / "status").write_text(STATUS.replace("241", "325"))
    assert read_transit("hw:M4", tmp_path) == (144, 48000.0, STREAM)    # the load test's figure
    (d / "hw_params").write_text(HW_PARAMS.replace("48000", "96000"))
    assert read_transit("hw:M4", tmp_path) == (144, 96000.0, STREAM)    # the stream's own rate
    (d / "hw_params").write_text(HW_PARAMS)
    (d / "status").write_text(STATUS.replace(f"trigger_time: {STREAM}\n", ""))
    assert read_transit("hw:M4", tmp_path) == (60, 48000.0, "")         # no start: still a figure
    for state in ("XRUN", "PREPARED", "SETUP", "DRAINING", "PAUSED", "SUSPENDED"):
        (d / "status").write_text(STATUS.replace("RUNNING", state))
        assert read_transit("hw:M4", tmp_path) is None, state           # only a running stream
    for status, hw in (("closed\n", "closed\n"),                        # no stream open
                       (STATUS.replace("241", "x"), HW_PARAMS),
                       (STATUS.replace("avail       : 75\n", ""), HW_PARAMS),
                       (STATUS, HW_PARAMS.replace("buffer_size", "buffer")),
                       (STATUS, HW_PARAMS.replace("rate: 48000 (48000/1)", "rate:")),
                       (STATUS, HW_PARAMS.replace("48000 (48000/1)", "0")),
                       (STATUS.replace("241", "100"), HW_PARAMS),       # below the fill: not sane
                       ("", "")):
        _stream(tmp_path, status, hw)
        assert read_transit("hw:M4", tmp_path) is None, (status, hw)
    _stream(tmp_path)
    (d / "hw_params").unlink()
    assert read_transit("hw:M4", tmp_path) is None
    assert read_transit("hw:card9", tmp_path) is None                   # no such card
    assert read_transit("default", tmp_path) is None


def test_a_burst_reads_one_stream_between_short_sleeps(tmp_path: Path) -> None:
    """USB_BURST reads about USB_GAP_S apart (slept in the worker thread that runs the burst, each
    gap drawn from 0.5 to 1.5 x), all of one stream: a restart or a stop during the burst gives
    no burst at all. hw_params is read once a burst: the rate and buffer size of a stream change
    only with a new start."""
    d = _stream(tmp_path)
    slept: list[float] = []
    delays = [(241, 253, 289)[i % 3] for i in range(lat.USB_BURST - 1)]

    def sleep(s: float) -> None:                    # the stream moves on between two reads
        (d / "status").write_text(STATUS.replace("241", str(delays[len(slept)])))
        (d / "hw_params").unlink(missing_ok=True)
        slept.append(s)

    b = lat.read_transits("hw:M4", tmp_path, sleep=sleep)
    assert b == ((60, *(x - 181 for x in delays)), 48000.0, STREAM)
    assert len(slept) == lat.USB_BURST - 1
    assert all(0.5 * lat.USB_GAP_S <= s < 1.5 * lat.USB_GAP_S for s in slept), slept
    assert len(set(slept)) > 1                                  # not one fixed gap
    assert (lat.USB_BURST, lat.USB_GAP_S) == (20, 0.01)    # about 0.19 s of a worker thread a second

    def change_at(k: int, text: str) -> Any:
        n = [0]

        def sleep(s: float) -> None:
            n[0] += 1
            if n[0] == k:
                (d / "status").write_text(text)
        return sleep

    for text in (STATUS.replace(STREAM, "58801.000000001"),    # restarted between two reads
                 "closed\n", STATUS.replace("RUNNING", "XRUN"),
                 STATUS.replace("241", "100")):                 # below the fill: not sane
        _stream(tmp_path)
        assert lat.read_transits("hw:M4", tmp_path, sleep=change_at(4, text)) is None, text
    _stream(tmp_path)
    assert lat.read_transits("hw:M4", tmp_path, reads=0) is None
    slept.clear()
    assert lat.read_transits("default", tmp_path, sleep=slept.append) is None and slept == []
    assert lat.read_transits("hw:M4", tmp_path, reads=1, sleep=slept.append) == ((60,), 48000.0,
                                                                                  STREAM)
    assert slept == []


def _hour(w: LatencyWatch, clock: Clock, reads: Any, level: int, io: float | None,
          n: int = 3600) -> set[tuple[float, float]]:
    """`n` bursts of `reads()` single reads, one a second, each followed by a poll: the
    (readout, USB share) pairs shown."""
    shown: set[tuple[float, float]] = set()
    for _ in range(n):
        w.note_usb(burst(tuple(reads() for _ in range(lat.USB_BURST))))
        r = w.note("Running", level, io, REF_64)
        assert r is not None
        shown.add((r.ms, r.usb_ms))
        clock.t += 1.0
    return shown


def test_the_spread_of_the_reads_never_moves_the_readout(caplog: Any) -> None:
    """The night run (docs/17 §9), chunk 64 / target 128: for 16 h the transit read 48 or 60
    frames (49 once), as medians of 3 s of reads, while the loop measured the same delay every
    time. Single reads at rest as read on 13 Nov (the pair with the shares of the 3620 reads, and
    a low group one USB frame down, 1 % of them and 14 %, the most seen), and models (reads spread
    over a whole USB millisecond, 36 to 72 frames, or 36/48/60/72; a quarter of them one USB frame
    higher, never seen). In every case an hour of bursts keeps the readout at what the engine's
    level gives, and the journal gets only its ten-minute lines."""
    caplog.set_level(logging.INFO, logger="k1000.latency")
    rng = random.Random(1213)
    for reads in (lambda: rng.choices(list(USUAL_48K_64), list(USUAL_48K_64.values()))[0],
                  lambda: rng.choices((48, 60, 0), (43, 43, 14))[0],
                  lambda: rng.choice((48, 60)), lambda: rng.randint(36, 72),
                  lambda: rng.choice((36, 48, 48, 60, 60, 72)),
                  lambda: rng.choice((48, 60, 48, 60, 48, 60, 0, 12)),
                  lambda: rng.choice((48, 60, 48, 60, 48, 60, 96, 108))):
        caplog.clear()
        clock = Clock()
        w = LatencyWatch(10.0, clock=clock)
        w.note_config(M4_64)
        io = w.io_share(2.3)
        for _ in range(3):                          # the figure before the engine's first average
            w.note_usb(burst(JIT))
        assert _hour(w, clock, reads, 182, io) == {(7.8, 0.0)}    # the readout of the night
        lines = [x.getMessage() for x in caplog.records if x.name == "k1000.latency"]
        assert len(lines) == 1 + (3600 - 1) // int(lat.LOG_EVERY_S)   # the first, then ten-minute
        assert all(re.fullmatch(r"latency: buffer=182 frames, usb=\d+\.\d frames, about 7\.8 ms "
                                r"in to out \(limit 10\.0\)", x) for x in lines), lines


def _box_run(dist: dict[int, float], rate: float, chunk: int, ref: float, *, seed: int = 1311,
             bursts: int = 600) -> tuple[list[float], list[float], set[tuple[float, float]]]:
    """`bursts` bursts of single reads drawn from `dist` (frames -> %), one a second, each
    followed by a poll at a fixed level: (the figure in ms after each, the median of the same
    window in ms, the (readout, counted shift) pairs shown)."""
    rng = random.Random(seed)
    w = LatencyWatch(10.0, clock=Clock())
    w.note_config({"devices": {"chunksize": chunk, "samplerate": rate}})
    io = w.io_share(2.0)
    figs: list[float] = []
    meds: list[float] = []
    shown: set[tuple[float, float]] = set()
    window: list[int] = []
    for _ in range(bursts):
        reads = rng.choices(list(dist), list(dist.values()), k=lat.USB_BURST)
        window = (window + reads)[-lat.USB_POOL:]
        w.note_usb(burst(tuple(reads), rate=rate))
        r = w.note("Running", 2 * chunk, io, ref)
        assert r is not None
        if w.usb is not None:
            figs.append(w.usb / rate * 1000.0)
            meds.append(sorted(window)[(len(window) - 1) // 2] / rate * 1000.0)
            shown.add((r.ms, r.usb_ms))
    return figs, meds, shown


@pytest.mark.parametrize("dist,rate,chunk,want_ms", [
    (USUAL_48K_64, 48000.0, 64, 1.13), (BOX_48K_64, 48000.0, 64, 1.38),
    (BOX_96K_128, 96000.0, 128, 1.13), (BOX_96K_128_ODD, 96000.0, 128, 1.28)])
def test_the_reads_of_13_nov_give_a_figure_that_does_not_flip(dist: dict[int, float], rate: float,
                                                               chunk: int, want_ms: float) -> None:
    """The single reads at rest read on the box on 13 Nov (a pair 0.25 ms apart, about half each,
    a low group one USB frame below): ten minutes of them. The figure stays within 0.05 ms of
    what the loop said (1.13 ms in the usual state at both rates; 1.38 and 1.28 ms in the two
    states the loop read 0.25 and 0.16 ms higher), where the median of the same windows flips
    between the two values of the pair; against the profile's reference the readout never
    moves (what it counts stays 0: none of these states is a shift)."""
    profile = load_profile("m4", {} if rate == 96000.0 else {"profile": "m4"}, probe=False)
    assert profile.samplerate == rate and profile.chunksize in (chunk, 256)
    ref = profile.io_transit_ms[chunk]
    assert ref == 1.13
    figs, meds, shown = _box_run(dist, rate, chunk, ref)
    assert len(figs) > 550
    assert all(abs(f - want_ms) <= 0.05 for f in figs), (min(figs), max(figs))
    assert max(figs) - min(figs) < 0.1
    if max(dist.values()) < 60:                      # a pair about half each: the median flips
        assert max(meds) - min(meds) == pytest.approx(0.25)
    assert len(shown) == 1 and next(iter(shown))[1] == 0.0


def test_the_pair_that_moved_the_readout_by_half_a_ms_no_longer_does() -> None:
    """Seen once on the box (13 Nov): the readout went from 6.6 to 7.1 ms where the loop read
    6.8. With the median and the 1.0 ms reference shipped then, a stream whose pair sits 0.25 and
    0.5 ms above the reference counts 0 or 0.5 ms depending on which value the median takes: the
    3620 reads at 48 kHz / chunk 64 are such a stream (60 / 72 frames: 1.25 / 1.5 ms). With the
    figure and the profile's reference (the figure at rest, 1.13 ms), the same reads count 0
    throughout, and the figure no longer flips. (On the code before D-022 this test fails: the
    figure flips by 0.25 ms, and the readout counts 0.5 ms from the first figure on.)"""
    ref = load_profile("m4", {"profile": "m4"}, probe=False).io_transit_ms[64]
    figs, _meds, shown = _box_run(BOX_48K_64, 48000.0, 64, ref)
    assert max(figs) - min(figs) < 0.1
    assert len(shown) == 1 and next(iter(shown))[1] == 0.0


def test_the_figure_is_the_mean_of_the_reads_near_the_median() -> None:
    """`transit_figure`: the reads more than USB_KEEP_MS (0.6 ms) from the window's median are
    left out, whatever the rate (28.8 frames at 48 kHz, 57.6 at 96 kHz)."""
    assert lat.USB_KEEP_MS == 0.6
    assert lat.transit_figure([48, 60] * 50, 48000.0) == 54.0
    assert lat.transit_figure([48] * 49 + [60] * 49 + [0, 12], 48000.0) == 54.0      # low group out
    assert lat.transit_figure([60] * 60 + [500] * 20 + [48] * 20, 48000.0) == 57.0  # stray out
    assert lat.transit_figure([96] * 46 + [120] * 50 + [24] * 4, 96000.0) == pytest.approx(
        (96 * 46 + 120 * 50) / 96)                 # 1.13 ms
    assert lat.transit_figure([100] * 60 + [60] * 40, 96000.0) == 84.0      # 40 frames: 0.42 ms
    assert lat.transit_figure([100] * 60 + [60] * 40, 48000.0) == 100.0     # 40 frames: 0.83 ms
    assert lat.transit_figure([60, 89], 48000.0) == 60.0
    assert lat.transit_figure([60, 88], 48000.0) == 74.0
    assert lat.transit_figure([77], 96000.0) == 77.0


def test_a_shift_in_the_usb_stream_shows_within_three_bursts(caplog: Any) -> None:
    """The load test of docs/17 §3.4, chunk 64 / target 64: the transit went from 48 to 144
    frames with the engine's level unchanged, and the delay measured through the loop from 6.11
    to 7.99 ms. Level 103 gives 6.1 ms (calculated to match). The readout moves by 2.0 ms on the
    third burst of the new figure, with a journal line (more than a chunk: 1.3 ms); a stray
    burst moves nothing."""
    clock = Clock()
    w = LatencyWatch(10.0, clock=clock)
    caplog.set_level(logging.INFO, logger="k1000.latency")

    def lines() -> list[str]:
        return [x.getMessage() for x in caplog.records if x.name == "k1000.latency"]

    w.note_config(M4_64)
    io = w.io_share(2.3)
    for _ in range(4):
        w.note_usb(burst(JIT))

    def poll(frames: int | tuple[int, ...]) -> Reading:
        w.note_usb(burst(frames))
        clock.t += 1.0
        r = w.note("Running", 103, io, REF_64)
        assert r is not None
        return r

    assert [poll(f).ms for f in (JIT, 500, JIT, JIT, JIT, JIT)] == [6.1] * 6
    assert lines() == ["latency: buffer=103 frames, usb=54.0 frames, about 6.1 ms in to out "
                       "(limit 10.0)"]
    assert [poll(SHIFT).ms for _ in range(3)] == [6.1, 6.1, 8.1]
    assert w.reading is not None and w.reading.usb == 150.0
    assert w.reading.usb_ms == pytest.approx(150 / 48 - REF_64)              # 2.0 ms
    assert lines()[-1] == ("latency: buffer=103 frames, usb=150.0 frames, about 8.1 ms in to out "
                           "(limit 10.0)")
    assert [poll(SHIFT).ms for _ in range(5)] == [8.1] * 5                  # its own spread
    assert len(lines()) == 2
    assert [poll(JIT).ms for _ in range(3)] == [8.1, 8.1, 6.1]              # and back
    assert len(lines()) == 3 and "usb=54.0 frames, about 6.1 ms" in lines()[-1]


def test_a_shift_follows_the_journal_rule_of_the_level(caplog: Any) -> None:
    """The journal gets a line when the estimate moves by more than a chunk, whatever moved it:
    at chunk 128 (2.7 ms) the same 2 ms shift shows at once and waits for the ten-minute line."""
    clock = Clock()
    w = LatencyWatch(10.0, clock=clock)
    caplog.set_level(logging.INFO, logger="k1000.latency")

    def logged() -> list[str]:
        return [x.getMessage() for x in caplog.records if x.name == "k1000.latency"]

    w.note_config({"devices": {"chunksize": 128, "samplerate": 48000}})
    io = w.io_share(2.3)
    for _ in range(3):
        w.note_usb(burst(JIT))
    first = w.note("Running", 173, io, 1.0)
    for _ in range(3):
        w.note_usb(burst(144))
    clock.t += 3.0
    r = w.note("Running", 173, io, 1.0)
    assert first is not None and r is not None and (first.ms, r.ms) == (9.2, 11.2)
    assert logged() == [first.line()]
    clock.t += lat.LOG_EVERY_S
    w.note("Running", 173, io, 1.0)
    assert logged() == [first.line(), r.line()] and "usb=144.0 frames, about 11.2 ms" in r.line()


def test_without_its_reference_the_readout_is_the_engine_level_alone(caplog: Any) -> None:
    """No io_transit_ms, or no io_latency_ms (the floor), or no figure: the readout and its
    journal line are what they were before the transit was read."""
    w = LatencyWatch(10.0, clock=Clock())
    caplog.set_level(logging.INFO, logger="k1000.latency")
    w.note_config(M4_64)
    io = w.io_share(2.3)
    for _ in range(3):
        w.note_usb(burst(144))
    assert w.usb == 144
    before = Reading(103, 64, 48000.0, io_ms=io)
    assert w.note("Running", 103, io) == before                          # no reference
    assert w.note("Running", 103, io, None) == before
    floor = w.note("Running", 103, None, 1.0)                            # the engine's share alone
    assert floor == Reading(103, 64, 48000.0, io_ms=None) and floor.line().count("usb") == 0
    w.note_usb(None)                                                     # the stream stopped
    assert w.usb is None and w.note("Running", 103, io, 1.0) == before
    lines = [x.getMessage() for x in caplog.records if x.name == "k1000.latency"]
    assert lines and not any("usb" in x for x in lines)


def test_the_counted_shift_is_never_more_than_one_band_off() -> None:
    """One band, not two: what the readout counts moves when the figure's own shift from the
    reference is more than USB_STEP_MS away from it, to that shift, or to 0 when that shift is
    within USB_STEP_MS of the reference. Settled on 60 frames (0.25 ms, counted as 0), a transit
    that then sits at 79 (0.65 ms) is counted: two stacked dead bands, the figure's and the
    reference's, hid it (second reading of the branch). Whatever the figure does, what is
    counted stays within USB_STEP_MS of it."""
    def first(frames: int, rate: float = 48000.0) -> float:
        w = LatencyWatch(10.0, clock=Clock())
        w.note_config({"devices": {"chunksize": 64, "samplerate": rate}})
        for _ in range(3):
            w.note_usb(burst(frames, rate=rate))
        r = w.note("Running", 103, w.io_share(2.3), 1.0)
        assert r is not None and r.usb == frames
        return r.usb_ms

    assert lat.USB_STEP_MS == 0.4                                        # 19.2 frames at 48 kHz
    assert first(48) == first(60) == first(67) == first(29) == 0.0
    assert first(68) == pytest.approx(20 / 48) and first(28) == pytest.approx(-20 / 48)
    assert first(144) == pytest.approx(2.0) and first(0) == pytest.approx(-1.0)
    assert first(96, 96000.0) == 0.0 and first(192, 96000.0) == pytest.approx(1.0)

    w = LatencyWatch(10.0, clock=Clock())
    w.note_config(M4_64)
    io = w.io_share(2.3)
    for _ in range(5):
        w.note_usb(burst(60))
    base = w.note("Running", 103, io, 1.0)
    assert base is not None and (base.usb, base.usb_ms, base.ms) == (60, 0.0, 6.1)
    for _ in range(3):
        w.note_usb(burst(79))
    r = w.note("Running", 103, io, 1.0)               # 40 reads of 60 and 60 of 79: their mean
    assert r is not None and r.usb == pytest.approx(71.4)
    assert r.usb_ms == pytest.approx(71.4 / 48 - 1.0) and r.ms == 6.6

    rng = random.Random(1214)
    v, moved = 48, set()
    for _ in range(5000):                           # a figure that wanders, a burst at a time
        v = min(300, max(0, v + rng.choice((-24, -12, -6, 0, 0, 6, 12, 24))))
        w.note_usb(burst(v))
        r = w.note("Running", 103, io, 1.0)
        assert r is not None and r.usb is not None
        assert abs(r.usb_ms - (r.usb / 48.0 - 1.0)) <= lat.USB_STEP_MS + 1e-9, (r.usb, r.usb_ms)
        assert r.usb_ms == 0.0 or abs(r.usb_ms) > lat.USB_STEP_MS
        moved.add(r.usb_ms)
    assert len(moved) > 20                          # it did move, many times


def test_the_transit_figure_starts_over_with_each_stream() -> None:
    w = LatencyWatch()
    for _ in range(3):
        w.note_usb(burst(144))
    assert w.usb is None                                    # no running config yet: no figure
    w.note_config(M4_64)
    w.note_usb(burst(144))
    w.note_usb(burst(144))
    assert w.usb is None and lat.USB_NEED == 3 * lat.USB_BURST     # three bursts first
    w.note_usb(burst(144))
    assert w.usb == 144
    w.note_usb(burst(48, rate=96000.0))                     # not the running config's rate
    assert w.usb is None
    for _ in range(3):
        w.note_usb(burst(JIT))
    assert w.usb == 54.0
    w.note_usb(None)                                        # not running, closed, unreadable
    w.note_usb(burst(144))
    assert w.usb is None                                    # the old reads are gone
    for _ in range(2):
        w.note_usb(burst(144))
    assert w.usb == 144
    w.note_usb(burst(()))                                   # an empty burst counts as none
    assert w.usb is None
    w.lost()
    assert w.usb is None
    w.note_config({"devices": {"chunksize": 64, "samplerate": 96000}})
    for _ in range(3):
        w.note_usb(burst((96, 120) * (lat.USB_BURST // 2), rate=96000.0))
    assert w.usb == 108.0
    r = w.note("Running", 300, w.io_share(2.3), 1.0)
    assert r is not None and r.samplerate == 96000.0 and r.usb_ms == 0.0   # 1.0 ms at 96 kHz
    for _ in range(3):
        w.note_usb(burst(192, rate=96000.0))
    r = w.note("Running", 300, w.io_share(2.3), 1.0)
    assert r is not None and r.usb_ms == pytest.approx(1.0)


def test_a_restart_between_two_bursts_starts_the_figure_over() -> None:
    """The stream restarts between two bursts, with no failed read between them (the engine
    restarted within a second): its start changed, so the old stream's reads are dropped at
    once, and the new stream's figure needs its own three bursts."""
    w = LatencyWatch(10.0, clock=Clock())
    w.note_config(M4_64)
    for _ in range(5):
        w.note_usb(burst(144))
    assert w.usb == 144
    shown = []
    for _ in range(3):
        w.note_usb(burst(JIT, stream="58801.000000001"))
        shown.append(w.usb)
    assert shown == [None, None, 54.0]


def test_the_first_line_without_the_figure_gets_one_with_it(caplog: Any) -> None:
    """A K1000 restart under a playing engine: the level comes at the first poll, the transit
    three bursts later. One more line then; a stream that stops and comes back adds none."""
    clock = Clock()
    w = LatencyWatch(10.0, clock=clock)
    caplog.set_level(logging.INFO, logger="k1000.latency")
    w.note_config(M4_64)
    io = w.io_share(2.3)
    w.note("Running", 182, io, 1.0)
    for _ in range(3):
        w.note_usb(burst(JIT))
        clock.t += 1.0
        w.note("Running", 182, io, 1.0)
    w.note_usb(None)
    w.note("Running", 182, io, 1.0)
    for _ in range(3):
        w.note_usb(burst(JIT[::-1], stream="58801.000000001"))
        clock.t += 1.0
        w.note("Running", 182, io, 1.0)
    lines = [x.getMessage() for x in caplog.records if x.name == "k1000.latency"]
    assert lines == ["latency: buffer=182 frames, about 7.8 ms in to out (limit 10.0)",
                     "latency: buffer=182 frames, usb=54.0 frames, about 7.8 ms in to out "
                     "(limit 10.0)"]


# ======================================================================= the dropout count
def _write(p: Path, data: bytes, mode: str = "ab") -> None:
    with open(p, mode) as f:
        f.write(data)


def _fill(p: Path, size: int = RS) -> None:
    """Clipping lines (no dropout) until the file holds `size` bytes: what a size rotation
    leaves behind (an engine start leaves a shorter file)."""
    while not p.exists() or p.stat().st_size < size:
        _write(p, CLIP)


def test_scan_counts_lines_after_the_last_start() -> None:
    assert scan(b"") == (0, False)
    assert scan(UNDERRUN + CLIP + OVERRUN) == (2, False)                # clipping is not a dropout
    assert scan(b"x: UNDERRUN then an overrun, one line\n") == (1, False)
    assert scan(UNDERRUN * 3 + START + OVERRUN) == (1, True)            # the run before is not counted
    assert scan(UNDERRUN + START_NEXT5 + OVERRUN * 2) == (2, True)      # next5's first line
    assert scan(OVERRUN + START) == (0, True)
    for line in (b"Capture device is stalled", b"Entering STALLED state", b"PlaybackError: x",
                 b"CaptureError: y", b"alsa xrun"):
        assert scan(line + b"\n") == (1, False), line


def test_the_words_and_the_rotation_size_follow_the_tools() -> None:
    """The same words as k1000-soak.sh's PATTERN, the same size as the engine unit's rotation."""
    soak = (BOX / "tools" / "k1000-soak.sh").read_text()
    m = re.search(r'PATTERN = re\.compile\(rb"([^"]+)", re\.IGNORECASE\)', soak)
    assert m is not None
    assert tuple(w.lower().encode() for w in m.group(1).split("|")) == lat.DROPOUT_WORDS
    unit = (BOX / "systemd" / "camilladsp.service").read_text()
    assert f"--log_rotate_size {lat.ROTATE_SIZE} " in unit


def test_dropouts_are_counted_incrementally(tmp_path: Path) -> None:
    live = tmp_path / "camilladsp_rCURRENT.log"
    c = DropoutCounter(live)
    assert c.read() is None                                     # no log: nothing to show
    _write(live, START + UNDERRUN + CLIP + OVERRUN)
    assert c.read() == 2                                        # clipping is not a dropout
    assert c.read() == 2
    _write(live, b"2026-10-13 05:01:00.000001 WARN  Capture device is STALLED\nPlaybackError: x")
    assert c.read() == 3                                        # the half-written line waits
    _write(live, b"\n")
    assert c.read() == 4
    c.close()


def test_a_size_rotation_keeps_counting_and_an_engine_start_resets(tmp_path: Path) -> None:
    live = tmp_path / "camilladsp_rCURRENT.log"
    _write(live, START + UNDERRUN)
    c = DropoutCounter(live, rotate_size=RS)
    assert c.read() == 1
    _write(live, UNDERRUN)                                      # written before the rotation...
    _fill(live)
    os.rename(live, tmp_path / "camilladsp_r2026-10-13_05-30-00.log")
    _write(live, CLIP + OVERRUN)                                # ...a size rotation: no start line
    assert c.read() == 3
    _fill(live)                                                 # the engine killed past the size,
    os.rename(live, tmp_path / "camilladsp_r2026-10-13_06-00-00.log")
    _write(live, START + OVERRUN)                               # restarted: its first line resets
    assert c.read() == 1
    live.write_bytes(b"")                                       # truncated in place
    assert c.read() == 1
    _write(live, START)
    assert c.read() == 0
    c.close()


def test_an_engine_start_resets_the_count_whatever_its_first_line(tmp_path: Path) -> None:
    """A rotation that leaves the old file short of the rotation size is an engine start: the
    count goes back to 0 even when the new run's first line is worded differently."""
    live = tmp_path / "camilladsp_rCURRENT.log"
    _write(live, START + UNDERRUN * 2)
    c = DropoutCounter(live, rotate_size=RS)
    assert c.read() == 2
    _write(live, OVERRUN)                                       # the run's last line, then it stops
    os.rename(live, tmp_path / "camilladsp_r2026-10-13_06-00-00.log")
    _write(live, b"2026-10-13 06:00:01.000001 INFO  [camilladsp] engine 6.0 starting\n" + UNDERRUN)
    assert c.read() == 1                                        # not 2 + 1 + 1
    os.rename(live, tmp_path / "camilladsp_r2026-10-13_06-10-00.log")
    assert c.read() is None                                     # between the rename and the new file
    _write(live, b"")
    assert c.read() == 0
    c.close()


def test_a_k1000_restart_counts_back_to_the_engine_start(tmp_path: Path) -> None:
    """k1000 restarted after two size rotations of the same engine run: the first read goes back
    through the rotated logs to the engine's first line, never further."""
    _write(tmp_path / "camilladsp_r2026-10-13_04-00-00.log", START + UNDERRUN * 5)      # older run
    _write(tmp_path / "camilladsp_r2026-10-13_05-00-00.log", UNDERRUN + START + UNDERRUN)
    _fill(tmp_path / "camilladsp_r2026-10-13_05-00-00.log")
    _write(tmp_path / "camilladsp_r2026-10-13_05-30-00.log", OVERRUN * 2 + CLIP)
    _fill(tmp_path / "camilladsp_r2026-10-13_05-30-00.log")
    _write(tmp_path / "camilladsp_rCURRENT.log", UNDERRUN)
    _write(tmp_path / "soak.log", UNDERRUN * 9)                                       # not the engine's
    live = tmp_path / "camilladsp_rCURRENT.log"
    c = DropoutCounter(live, rotate_size=RS)
    assert c.read() == 1 + 2 + 1
    one = (tmp_path / "camilladsp_r2026-10-13_05-30-00.log").stat().st_size
    assert DropoutCounter(live, rotate_size=RS, max_back_bytes=one).read() == 1 + 2   # the budget
    assert DropoutCounter(live, rotate_size=RS, max_back_bytes=one - 1).read() == 1
    assert DropoutCounter(tmp_path / "other.log").read() is None
    _write(tmp_path / "plain.log", UNDERRUN)
    assert DropoutCounter(tmp_path / "plain.log").read() == 1    # no rotation names: no going back
    c.close()


def test_counting_back_stops_at_the_run_before_without_its_first_line(tmp_path: Path) -> None:
    """No start line anywhere (another wording): the count back stops at the file an engine start
    rotated (short of the rotation size), which is the run before's last."""
    _write(tmp_path / "camilladsp_r2026-10-13_04-00-00.log", UNDERRUN * 5)              # run before
    _write(tmp_path / "camilladsp_r2026-10-13_05-00-00.log", OVERRUN * 2)
    _fill(tmp_path / "camilladsp_r2026-10-13_05-00-00.log")                             # this run
    _write(tmp_path / "camilladsp_rCURRENT.log", UNDERRUN)
    assert DropoutCounter(tmp_path / "camilladsp_rCURRENT.log", rotate_size=RS).read() == 2 + 1


def test_a_first_read_on_a_fresh_log_does_not_count_the_run_before(tmp_path: Path) -> None:
    """k1000 reads right after an engine start, before the new run wrote anything: the count is
    0, not the previous run's (its last file was rotated at this start, short of the size)."""
    _write(tmp_path / "camilladsp_r2026-10-13_06-00-00.log", START + UNDERRUN * 4)
    live = tmp_path / "camilladsp_rCURRENT.log"
    _write(live, b"")
    c = DropoutCounter(live)                                    # the box's rotation size
    assert c.read() == 0
    _write(live, START_NEXT5 + UNDERRUN)
    assert c.read() == 1
    c.close()


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads anything")
def test_an_unreadable_log_shows_nothing(tmp_path: Path) -> None:
    live = tmp_path / "camilladsp_rCURRENT.log"
    _write(live, START + UNDERRUN)
    live.chmod(0)
    try:
        assert DropoutCounter(live).read() is None
    finally:
        live.chmod(0o644)


# ======================================================================= the controller
class LevelClient(FakeClient):
    """FakeClient that answers GetBufferLevel (and counts every command)."""

    def __init__(self, cfg: dict[str, Any] | None) -> None:
        super().__init__(cfg)
        self.level: Any = 536
        self.commands: list[str] = []

    async def command(self, name: str, timeout: float | None = None, **fields: Any) -> Any:
        self._need()
        self.commands.append(name)
        if name == "GetBufferLevel":
            if isinstance(self.level, Exception):
                raise self.level
            return self.level
        return await super().command(name, timeout, **fields)


def make(tmp_path: Path, *, io: float | None = 2.3, transit: dict[int, float] | None = None,
         settings: dict[str, Any] | None = None, engine_log: Path | None = None,
         samplerate: int = 48000) -> tuple[Controller, LevelClient]:
    s = st.State()
    profile = dataclasses.replace(DEV, io_latency_ms=io, io_transit_ms=transit,
                                  samplerate=samplerate)
    client = LevelClient(dc.generate(profile, s))
    ctl = Controller(client, profile, s, Paths(tmp_path / "state.json", tmp_path / "active.yml",
                                               engine_log), settings=settings,
                     sleep=fast_sleep, poll_s=3600, meter_s=3600, persist_debounce_s=0.05,
                     persist_max_delay_s=0.5, device_probe=lambda d: True)
    return ctl, client


def test_the_poll_reads_the_level_once_and_shows_the_delay(tmp_path: Path, caplog: Any) -> None:
    caplog.set_level(logging.INFO, logger="k1000.latency")

    async def body() -> None:
        ctl, cl = make(tmp_path)
        await boot(ctl)
        await ctl.poll_once()
        n, writes = cl.commands.count("GetBufferLevel"), len(cl.log)
        v = ctl.version
        await ctl.poll_once()
        assert cl.commands.count("GetBufferLevel") == n + 1         # one read a poll, no more
        assert cl.log[writes:] == []                                 # nothing written
        s = ctl.snapshot()[2]
        assert (s.latency_ms, s.latency_io, s.latency_over, s.buffer_level, s.latency_limit_ms) \
            == (20.1, True, True, 536, 10.0)
        assert ctl.version == v                                      # unchanged: no wake-up
        cl.level = 540
        await ctl.poll_once()
        assert ctl.snapshot()[2].latency_ms == 20.2 and ctl.version > v
        await ctl.stop()

    run(body())
    lines = [r.getMessage() for r in caplog.records if r.name == "k1000.latency"]
    assert lines == ["latency: buffer=536 frames, about 20.1 ms in to out (limit 10.0)"]


def test_no_reading_without_a_level_and_never_a_failed_poll(tmp_path: Path) -> None:
    async def body() -> None:
        ctl, cl = make(tmp_path, io=None, settings={"engine": {"latency_limit_ms": 20}})
        await boot(ctl)
        await ctl.poll_once()
        s = ctl.snapshot()[2]
        assert (s.latency_ms, s.latency_io, s.latency_over, s.latency_limit_ms) == (16.5, False, False, 20.0)
        cl.level = CdspError("GetBufferLevel", "Invalid", "an engine without it")
        cl.engine_state = "Paused"
        await ctl.poll_once()                                        # the rest of the poll goes on
        s = ctl.snapshot()[2]
        assert s.latency_ms is None and s.buffer_level is None and s.engine == "Paused"
        cl.level, cl.engine_state = 0, "Running"                     # no average published yet
        await ctl.poll_once()
        assert ctl.snapshot()[2].latency_ms is None
        await ctl.stop()

    run(body())


def test_a_lost_link_clears_the_readout_and_the_next_one_is_logged(tmp_path: Path, caplog: Any) -> None:
    caplog.set_level(logging.INFO, logger="k1000.latency")

    async def body() -> None:
        ctl, cl = make(tmp_path)
        await boot(ctl)
        await ctl.poll_once()
        assert ctl.status.latency_ms == 20.1
        await cl.drop()
        await until(lambda: ctl.status.latency_ms is None, what="readout cleared")
        assert ctl.status.buffer_level is None
        await cl.reconnect()
        await until(lambda: ctl.engine_ready, what="sync")
        await ctl.poll_once()                                        # devices from the connect sync
        assert ctl.status.latency_ms == 20.1
        await ctl.stop()

    run(body())
    lines = [r.getMessage() for r in caplog.records if r.name == "k1000.latency"]
    assert len(lines) == 2


def test_the_transit_is_read_in_a_worker_thread_while_the_engine_runs(tmp_path: Path,
                                                                      caplog: Any) -> None:
    caplog.set_level(logging.INFO, logger="k1000.latency")
    calls: list[tuple[str, str]] = []
    frames = [48]

    def reader(device: str) -> lat.Burst | None:
        calls.append((device, threading.current_thread().name))
        return burst(frames[0])

    async def body() -> None:
        ctl, cl = make(tmp_path, transit={256: 1.0})                 # DEV runs chunk 256
        ctl.read_transit = reader                                    # type: ignore[assignment]
        ctl.transit_s = 0.01
        await boot(ctl)
        await until(lambda: ctl.latency.usb == 48, what="the transit figure")
        await ctl.poll_once()
        assert (ctl.status.latency_ms, ctl.status.buffer_level) == (20.1, 536)   # 1.0 ms: as before
        v = ctl.version
        frames[0] = 144
        await until(lambda: ctl.latency.usb == 144, what="the shift")
        await ctl.poll_once()
        assert ctl.status.latency_ms == 22.1 and ctl.version > v
        cl.engine_state = "Paused"
        await ctl.poll_once()
        await until(lambda: ctl.latency.usb is None, what="no stream, no figure")
        n = len(calls)
        await asyncio.sleep(0.05)
        assert len(calls) == n                                       # nothing read meanwhile
        await ctl.stop()

    run(body())
    assert {d for d, _ in calls} == {DEV.playback_device}
    assert threading.main_thread().name not in {t for _, t in calls}
    lines = [r.getMessage() for r in caplog.records if r.name == "k1000.latency"]
    # The last line names the transit; the 2 ms shift, less than chunk 256, wrote none.
    assert lines[-1] == ("latency: buffer=536 frames, usb=48.0 frames, about 20.1 ms in to out "
                         "(limit 10.0)")


def test_no_reference_nothing_read_and_a_failed_read_is_no_figure(tmp_path: Path,
                                                                    caplog: Any) -> None:
    calls: list[str] = []

    def reader(device: str) -> lat.Burst | None:
        calls.append(device)
        return burst(144)

    def broken(device: str) -> lat.Burst | None:
        raise RuntimeError("boom")

    async def body() -> None:
        # Either missing, or the reference read at another chunk than the running one: no read.
        for io, transit in ((2.3, None), (None, {256: 1.0}), (2.3, {64: 1.0, 128: 1.0})):
            ctl, _cl = make(tmp_path, io=io, transit=transit)
            ctl.read_transit = reader                                # type: ignore[assignment]
            ctl.transit_s = 0.01
            await boot(ctl)
            await ctl.poll_once()
            await asyncio.sleep(0.05)
            assert calls == [] and ctl.latency.usb is None
            assert ctl.status.latency_ms == (20.1 if io else 16.5)   # the readout as before
            await ctl.stop()
        ctl, _cl = make(tmp_path, transit={256: 1.0})
        ctl.read_transit = broken                                    # type: ignore[assignment]
        ctl.transit_s = 0.01
        await boot(ctl)
        await asyncio.sleep(0.05)
        await ctl.poll_once()
        assert ctl.latency.usb is None and ctl.status.latency_ms == 20.1
        await ctl.stop()

    caplog.set_level(logging.WARNING, logger="k1000.control")
    run(body())
    warned = [r.getMessage() for r in caplog.records if "USB transit" in r.getMessage()]
    assert len(warned) == 1 and "boom" in warned[0]                  # once, not every second


def test_figures_measured_at_another_rate_are_not_applied(tmp_path: Path) -> None:
    """D-022: io_latency_ms and io_transit_ms are the profile's, measured at its rate. While the
    engine runs another rate (a k1000.yaml rate change before camilladsp restarts, a config from
    CamillaGUI), the readout is the engine's share alone, as a floor, and the transit is not
    read; back at the profile's rate, both count again."""
    calls: list[str] = []

    def reader(device: str) -> lat.Burst | None:
        calls.append(device)
        return burst(96, rate=96000.0)

    async def body() -> None:
        ctl, cl = make(tmp_path, transit={256: 1.0}, samplerate=96000)
        ctl.read_transit = reader                                    # type: ignore[assignment]
        ctl.transit_s = 0.01
        await boot(ctl)
        assert cl.cfg["devices"]["samplerate"] == 96000 and st.engine_rate() == 96000
        await until(lambda: ctl.latency.usb == 96, what="the transit figure")
        await ctl.poll_once()
        assert ctl.status.latency_ms == 11.2          # (536 + 256) / 96 kHz + 1.9 + a quarter chunk
        cl.cfg["devices"]["samplerate"] = 48000       # the engine runs 48 kHz
        await ctl.poll_once()                         # the config read: the next poll shows it
        await ctl.poll_once()
        await until(lambda: ctl.latency.usb is None, what="no figure at another rate")
        n = len(calls)
        await asyncio.sleep(0.05)
        assert len(calls) == n                                       # nothing read meanwhile
        assert ctl.status.latency_ms == 16.5                         # (536 + 256) / 48 kHz: a floor
        assert ctl.status.drift == ["devices.samplerate 48000 != 96000"]
        cl.cfg["devices"]["samplerate"] = 96000
        await ctl.poll_once()
        await until(lambda: ctl.latency.usb == 96, what="the figure back")
        assert ctl.status.drift == []
        await ctl.poll_once()
        assert ctl.status.latency_ms == 11.2
        await ctl.stop()

    run(body())


def test_the_dropout_count_comes_from_a_worker_thread(tmp_path: Path) -> None:
    live = tmp_path / "camilladsp_rCURRENT.log"
    _write(live, START + UNDERRUN + UNDERRUN)

    async def body() -> None:
        ctl, _cl = make(tmp_path, engine_log=live)
        assert ctl.dropouts is not None
        threads: list[str] = []
        read = ctl.dropouts.read

        def spy() -> int | None:
            threads.append(threading.current_thread().name)
            return read()

        ctl.dropouts.read = spy                                      # type: ignore[method-assign]
        ctl.dropout_s = 0.01
        await boot(ctl)
        await until(lambda: ctl.status.dropouts == 2, what="first count")
        v = ctl.version
        _write(live, OVERRUN)
        await until(lambda: ctl.status.dropouts == 3, what="the new line")
        assert ctl.version > v
        assert threads and threading.main_thread().name not in threads
        live.unlink()
        await until(lambda: ctl.status.dropouts is None, what="log gone")
        await ctl.stop()
        ctl.dropouts.close()

    run(body())


def test_no_engine_log_no_count(tmp_path: Path) -> None:
    async def body() -> None:
        ctl, _cl = make(tmp_path)
        assert ctl.dropouts is None
        await boot(ctl)
        await asyncio.sleep(0.05)
        assert ctl.status.dropouts is None
        await ctl.stop()

    run(body())
    assert Paths.from_settings({}).engine_log == lat.ENGINE_LOG
    assert Paths.from_settings({"paths": {"engine_log": "/tmp/x.log"}}).engine_log == Path("/tmp/x.log")
    assert Status().latency_limit_ms == 10.0


# ======================================================================= the screen
OK = dict(device_present=True, engine="Running", load=2.0, profile="m4", device="MOTU M4")


def _bar_has(img: Any, colour: Any) -> bool:
    return any(img.getpixel((x, y)) == colour for x in range(0, 480)
               for y in range(6, pages.STATUS_H - 6, 3))


def test_latency_readout_text() -> None:
    S = pages.ScreenStatus
    assert pages.latency_readout(S()) is None and pages.latency_readout(None) is None
    assert pages.latency_readout(S(latency_ms=20.3, latency_over=True)) == ("20.3ms", True, "")
    assert pages.latency_readout(S(latency_ms=16.5, latency_io=False)) == ("≥16.5ms", False, "")
    assert pages.latency_readout(S(latency_ms=8.0, dropouts=0)) == ("8.0ms", False, "")
    assert pages.latency_readout(S(latency_ms=8.0, dropouts=3)) == ("≥8.0ms", False, "3 DROP")  # a floor
    for junk in (float("nan"), "x", -1.0):
        assert pages.latency_readout(S(latency_ms=junk)) is None, junk     # type: ignore[arg-type]
    assert pages.latency_readout(S(latency_ms=1e12))[0] == "9999.9ms"


def test_the_status_bar_shows_the_delay_orange_above_the_limit() -> None:
    s = st.State()
    plain = pages.render("SHOW", s, pages.ScreenStatus(**OK), Focus(), now=0.0)
    within = pages.render("SHOW", s, pages.ScreenStatus(**OK, latency_ms=8.4), Focus(), now=0.0)
    above = pages.render("SHOW", s, pages.ScreenStatus(**OK, latency_ms=20.3, latency_over=True),
                         Focus(), now=0.0)
    assert within.tobytes() != plain.tobytes()                       # shown
    assert not _bar_has(within, pages.ORANGE) and _bar_has(above, pages.ORANGE)
    assert _bar_has(above, pages.GREEN)                              # the engine state stays


def _bar(page: str, s: Any, **kw: Any) -> bytes:
    return pages.render(page, s, pages.ScreenStatus(**kw), Focus(), now=0.0).crop(
        (0, 0, pages.W, pages.STATUS_H)).tobytes()


@pytest.mark.parametrize("page", ["SHOW", "ALIGN", "SETUP"])
def test_with_two_pills_the_dsp_load_gives_way_to_the_delay(page: str) -> None:
    """MUTED + CLIP, or IN CLIP + CLIP: the DSP load goes, then the dropout count; the delay
    stays (a clip latch must not blink it away)."""
    lat_kw = dict(latency_ms=20.3, latency_over=True, dropouts=3)
    for pills in (dict(muted=True, clip=(True, False, False, False)),
                  dict(in_clip=True, clip=(True, False, False, False))):
        s = st.State()
        s.muted = bool(pills.get("muted"))
        crowded = dict(OK, **pills)
        with_lat = _bar(page, s, **crowded, **lat_kw)
        assert with_lat != _bar(page, s, **crowded)                          # the delay is drawn
        assert with_lat == _bar(page, s, **dict(crowded, load=None), **lat_kw)   # the load is not
        assert with_lat == _bar(page, s, **crowded, latency_ms=20.3, latency_over=True,
                                latency_io=False)                           # "≥20.3ms", no "3 DROP"


def test_with_one_pill_the_dsp_load_goes_first() -> None:
    s = st.State()
    lat_kw = dict(latency_ms=20.3, latency_over=True, dropouts=3)
    one = dict(OK, clip=(True, False, False, False))
    drops = _bar("SHOW", s, **one, **lat_kw)
    assert drops == _bar("SHOW", s, **dict(one, load=None), **lat_kw)     # the load is not drawn
    assert drops != _bar("SHOW", s, **one, latency_ms=20.3, latency_over=True,
                         latency_io=False)                                  # "3 DROP" is
    roomy = _bar("SHOW", s, **OK, **lat_kw)
    assert roomy != _bar("SHOW", s, **dict(OK, load=None), **lat_kw)      # room: the load stays


@pytest.mark.parametrize("page", ["SHOW", "ALIGN", "SETUP"])
def test_the_readout_gives_way_to_three_pills_and_to_alerts(page: str) -> None:
    s = st.State()
    s.muted = True
    lat_kw = dict(latency_ms=41.2, latency_over=True, dropouts=3)
    crowded = dict(OK, muted=True, in_clip=True, clip=(True, False, False, False))
    assert _bar(page, s, **crowded, **lat_kw) == _bar(page, s, **crowded)  # no room: not drawn
    alert = dict(OK, alerts=[("critical", "ENGINE SYNC FAILED (SEE LOG)")])
    assert _bar(page, st.State(), **alert, **lat_kw) == _bar(page, st.State(), **alert)


# ======================================================================= the phone
def test_protocol_2_carries_the_readout_and_protocol_1_does_not() -> None:
    status = Status(engine="Running", latency_ms=20.3, latency_io=True, latency_over=True,
                    latency_limit_ms=10.0, buffer_level=536, dropouts=0)
    snap = ("SHOW", st.State(), status, Focus())
    v2 = PhoneBridge.state_message2(snap, phone_on=True, phones=1)
    assert v2["latency"] == {"ms": 20.3, "io": True, "over": True, "limit": 10.0, "buf": 536, "drops": 0}
    assert "latency" not in PhoneBridge.state_message(snap, phone_on=True, phones=1)
    odd = Status(latency_ms=float("nan"), latency_io=False, buffer_level=True, dropouts=None)  # type: ignore[arg-type]
    assert PhoneBridge.state_message2(("SHOW", st.State(), odd, Focus()), phone_on=True, phones=1)[
        "latency"] == {"ms": None, "io": False, "over": False, "limit": 10.0, "buf": None, "drops": None}
