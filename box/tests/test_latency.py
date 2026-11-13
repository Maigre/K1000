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
                           read_transit, run_devices, scan, stream_dir, usb_shift_ms)
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
    assert load_profile("m4", {}, probe=False).io_latency_ms == 2.3
    assert load_profile("dev", {}).io_latency_ms is None
    shipped = load_settings(BOX / "etc" / "k1000.yaml")
    assert load_profile("m4", shipped, probe=False).io_latency_ms == 2.3
    assert load_profile("dev", shipped).io_latency_ms is None
    assert load_profile("m4", {"profiles": {"m4": {"io_latency_ms": 5}}}, probe=False).io_latency_ms == 5.0
    assert load_profile("m4", {"profiles": {"m4": {"io_latency_ms": None}}}, probe=False).io_latency_ms is None
    assert load_profile("dev", {"profiles": {"dev": {"io_latency_ms": 1.2}}}).io_latency_ms == 1.2
    for junk in ("3.8", True, -1, 51, float("nan")):
        p = load_profile("m4", {"profiles": {"m4": {"io_latency_ms": junk}}}, probe=False)
        assert p.io_latency_ms == 2.3 and any("io_latency_ms" in w for w in p.warnings), junk


def test_io_transit_is_a_profile_number() -> None:
    """The transit io_latency_ms was measured with: built in for the m4, so a settings file
    written before it (io_latency_ms alone) counts the transit unchanged; `null` turns it off."""
    assert load_profile("m4", {}, probe=False).io_transit_ms == 1.0
    assert load_profile("dev", {}).io_transit_ms is None
    shipped = load_settings(BOX / "etc" / "k1000.yaml")
    assert load_profile("m4", shipped, probe=False).io_transit_ms == 1.0
    assert load_profile("dev", shipped).io_transit_ms is None
    older = {"profiles": {"m4": {"chunksize": 64, "target_level": 128, "io_latency_ms": 2.3}}}
    p = load_profile("m4", older, probe=False)
    assert (p.io_latency_ms, p.io_transit_ms, p.warnings) == (2.3, 1.0, [])
    assert load_profile("m4", {"profiles": {"m4": {"io_transit_ms": None}}}, probe=False).io_transit_ms is None
    assert load_profile("m4", {"profiles": {"m4": {"io_transit_ms": 2}}}, probe=False).io_transit_ms == 2.0
    for junk in ("1.0", True, -1, 51, float("nan")):
        p = load_profile("m4", {"profiles": {"m4": {"io_transit_ms": junk}}}, probe=False)
        assert p.io_transit_ms == 1.0 and any("io_transit_ms" in w for w in p.warnings), junk


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
    assert read_transit("hw:M4", tmp_path) == (60, 48000.0)             # the box's sample
    assert read_transit("hw:CARD=M4,DEV=0", tmp_path) == (60, 48000.0)
    (d / "status").write_text(STATUS.replace("241", "325"))
    assert read_transit("hw:M4", tmp_path) == (144, 48000.0)            # the load test's figure
    (d / "hw_params").write_text(HW_PARAMS.replace("48000", "96000"))
    assert read_transit("hw:M4", tmp_path) == (144, 96000.0)            # the stream's own rate
    (d / "hw_params").write_text(HW_PARAMS)
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


def test_the_reading_jitter_never_moves_the_readout(caplog: Any) -> None:
    """The night run (docs/17 §9), chunk 64 / target 128: for 16 h the transit read 48 or 60
    frames (49 once) while the loop measured the same delay every time. The readout stays at what
    the engine's level gives, and the journal gets only its ten-minute lines."""
    clock = Clock()
    w = LatencyWatch(10.0, clock=clock)
    caplog.set_level(logging.INFO, logger="k1000.latency")
    w.note_config(M4_64)
    io = w.io_share(2.3)
    rng = random.Random(1213)
    reads = ([48, 60] * 20 + [60, 60, 48, 48] * 10 + [48, 48, 60, 60, 60, 48, 49, 60]
             + [rng.choice((48, 60)) for _ in range(3000)])
    for frames in (48, 60, 48):                     # the figure before the engine's first average
        w.note_usb((frames, 48000.0))
    shown: set[Any] = set()
    for frames in reads:
        w.note_usb((frames, 48000.0))
        r = w.note("Running", 182, io, 1.0)
        assert r is not None
        shown.add((r.ms, r.usb_ms))
        clock.t += 1.0
    assert shown == {(7.8, 0.0)}                    # the readout of the night, unchanged
    lines = [x.getMessage() for x in caplog.records if x.name == "k1000.latency"]
    assert len(lines) == 1 + (len(reads) - 1) // int(lat.LOG_EVERY_S)   # the first, then ten-minute
    assert all(re.fullmatch(r"latency: buffer=182 frames, usb=(48|60) frames, about 7\.8 ms in "
                            r"to out \(limit 10\.0\)", x) for x in lines), lines
    assert not any(re.search("usb=49", x) for x in lines)


def test_a_shift_in_the_usb_stream_shows_within_three_reads(caplog: Any) -> None:
    """The load test of docs/17 §3.4, chunk 64 / target 64: the transit went from 48 to 144
    frames with the engine's level unchanged, and the delay measured through the loop from 6.11
    to 7.99 ms. Level 103 gives 6.1 ms (calculated to match). The readout moves by 2.0 ms on the
    third read of the new figure, with a journal line (more than a chunk: 1.3 ms); a single
    stray read moves nothing."""
    clock = Clock()
    w = LatencyWatch(10.0, clock=clock)
    caplog.set_level(logging.INFO, logger="k1000.latency")

    def lines() -> list[str]:
        return [x.getMessage() for x in caplog.records if x.name == "k1000.latency"]

    w.note_config(M4_64)
    io = w.io_share(2.3)
    for f in (48, 60, 48):
        w.note_usb((f, 48000.0))

    def poll(frames: int) -> Reading:
        w.note_usb((frames, 48000.0))
        clock.t += 1.0
        r = w.note("Running", 103, io, 1.0)
        assert r is not None
        return r

    assert [poll(f).ms for f in (48, 60, 500, 48, 60, 48, 60)] == [6.1] * 7
    assert lines() == ["latency: buffer=103 frames, usb=48 frames, about 6.1 ms in to out "
                       "(limit 10.0)"]
    assert [poll(f).ms for f in (144, 156, 144)] == [6.1, 6.1, 8.1]
    assert w.reading is not None and w.reading.usb == 144 and w.reading.usb_ms == pytest.approx(2.0)
    assert lines()[-1] == ("latency: buffer=103 frames, usb=144 frames, about 8.1 ms in to out "
                           "(limit 10.0)")
    assert [poll(f).ms for f in (156, 144, 156, 144, 156)] == [8.1] * 5     # its own jitter
    assert len(lines()) == 2
    assert [poll(f).ms for f in (48, 60, 48)] == [8.1, 8.1, 6.1]            # and back
    assert len(lines()) == 3 and "usb=60 frames, about 6.1 ms" in lines()[-1]


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
    for f in (48, 60, 48):
        w.note_usb((f, 48000.0))
    first = w.note("Running", 173, io, 1.0)
    for f in (144, 144, 144):
        w.note_usb((f, 48000.0))
    clock.t += 3.0
    r = w.note("Running", 173, io, 1.0)
    assert first is not None and r is not None and (first.ms, r.ms) == (9.2, 11.2)
    assert logged() == [first.line()]
    clock.t += lat.LOG_EVERY_S
    w.note("Running", 173, io, 1.0)
    assert logged() == [first.line(), r.line()] and "usb=144 frames, about 11.2 ms" in r.line()


def test_without_its_reference_the_readout_is_the_engine_level_alone(caplog: Any) -> None:
    """No io_transit_ms, or no io_latency_ms (the floor), or no figure: the readout and its
    journal line are what they were before the transit was read."""
    w = LatencyWatch(10.0, clock=Clock())
    caplog.set_level(logging.INFO, logger="k1000.latency")
    w.note_config(M4_64)
    io = w.io_share(2.3)
    for f in (144, 144, 144):
        w.note_usb((f, 48000.0))
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
    assert usb_shift_ms(48, 48000.0, 1.0) == 0.0 and usb_shift_ms(60, 48000.0, 1.0) == 0.0
    assert usb_shift_ms(144, 48000.0, 1.0) == pytest.approx(2.0)
    assert usb_shift_ms(0, 48000.0, 1.0) == pytest.approx(-1.0)
    assert usb_shift_ms(96, 96000.0, 1.0) == 0.0                         # 1 ms at 96 kHz too


def test_the_transit_figure_starts_over_with_each_stream() -> None:
    w = LatencyWatch()
    for f in (144, 144, 144):
        w.note_usb((f, 48000.0))
    assert w.usb is None                                    # no running config yet: no figure
    w.note_config(M4_64)
    w.note_usb((144, 48000.0))
    w.note_usb((144, 48000.0))
    assert w.usb is None                                    # USB_NEED reads first
    w.note_usb((144, 48000.0))
    assert w.usb == 144
    w.note_usb((48, 96000.0))                               # not the running config's rate
    assert w.usb is None
    for f in (48, 60, 48):
        w.note_usb((f, 48000.0))
    assert w.usb == 48
    w.note_usb(None)                                        # not running, closed, unreadable
    w.note_usb((144, 48000.0))
    assert w.usb is None                                    # the old reads are gone
    for f in (144, 144):
        w.note_usb((f, 48000.0))
    assert w.usb == 144
    w.lost()
    assert w.usb is None
    w.note_config({"devices": {"chunksize": 64, "samplerate": 96000}})
    for f in (96, 120, 96):
        w.note_usb((f, 96000.0))
    assert w.usb == 96
    r = w.note("Running", 300, w.io_share(2.3), 1.0)
    assert r is not None and r.samplerate == 96000.0 and r.usb_ms == 0.0   # 1.0 ms at 96 kHz
    for f in (192, 192, 192):
        w.note_usb((f, 96000.0))
    r = w.note("Running", 300, w.io_share(2.3), 1.0)
    assert r is not None and r.usb_ms == pytest.approx(1.0)


def test_the_first_line_without_the_figure_gets_one_with_it(caplog: Any) -> None:
    """A K1000 restart under a playing engine: the level comes at the first poll, the transit
    three reads later. One more line then; a stream that stops and comes back adds none."""
    clock = Clock()
    w = LatencyWatch(10.0, clock=clock)
    caplog.set_level(logging.INFO, logger="k1000.latency")
    w.note_config(M4_64)
    io = w.io_share(2.3)
    w.note("Running", 182, io, 1.0)
    for f in (48, 60, 48):
        w.note_usb((f, 48000.0))
        clock.t += 1.0
        w.note("Running", 182, io, 1.0)
    w.note_usb(None)
    w.note("Running", 182, io, 1.0)
    for f in (60, 48, 60):
        w.note_usb((f, 48000.0))
        clock.t += 1.0
        w.note("Running", 182, io, 1.0)
    lines = [x.getMessage() for x in caplog.records if x.name == "k1000.latency"]
    assert lines == ["latency: buffer=182 frames, about 7.8 ms in to out (limit 10.0)",
                     "latency: buffer=182 frames, usb=48 frames, about 7.8 ms in to out "
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


def make(tmp_path: Path, *, io: float | None = 2.3, transit: float | None = None,
         settings: dict[str, Any] | None = None,
         engine_log: Path | None = None) -> tuple[Controller, LevelClient]:
    s = st.State()
    profile = dataclasses.replace(DEV, io_latency_ms=io, io_transit_ms=transit)
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

    def reader(device: str) -> tuple[int, float] | None:
        calls.append((device, threading.current_thread().name))
        return frames[0], 48000.0

    async def body() -> None:
        ctl, cl = make(tmp_path, transit=1.0)
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
    assert lines[-1] == "latency: buffer=536 frames, usb=48 frames, about 20.1 ms in to out (limit 10.0)"


def test_no_reference_nothing_read_and_a_failed_read_is_no_figure(tmp_path: Path,
                                                                    caplog: Any) -> None:
    calls: list[str] = []

    def reader(device: str) -> tuple[int, float] | None:
        calls.append(device)
        return 144, 48000.0

    def broken(device: str) -> tuple[int, float] | None:
        raise RuntimeError("boom")

    async def body() -> None:
        for io, transit in ((2.3, None), (None, 1.0)):              # either missing: no read
            ctl, _cl = make(tmp_path, io=io, transit=transit)
            ctl.read_transit = reader                                # type: ignore[assignment]
            ctl.transit_s = 0.01
            await boot(ctl)
            await ctl.poll_once()
            await asyncio.sleep(0.05)
            assert calls == [] and ctl.latency.usb is None
            await ctl.stop()
        ctl, _cl = make(tmp_path, transit=1.0)
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
