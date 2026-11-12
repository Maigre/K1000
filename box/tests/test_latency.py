# SPDX-License-Identifier: GPL-3.0-or-later
"""The D-018 latency readout (k1000/latency.py): the estimate, its journal lines, the dropout
count read from the engine's live log, and where it shows (the controller's status, the box's
status bar, the phone's protocol 2). No engine, no hardware: what only the box can prove (the
estimate against the loop cable, the real log's rotation) is listed in latency.py's docstring."""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any

import pytest

from k1000 import latency as lat
from k1000 import state as st
from k1000.cdsp import CdspError
from k1000.control import Controller, Paths, Status
from k1000.latency import DropoutCounter, LatencyWatch, Reading, latency_limit_ms, run_devices, scan
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
    assert load_profile("m4", {}, probe=False).io_latency_ms == 3.8
    assert load_profile("dev", {}).io_latency_ms is None
    shipped = load_settings(BOX / "etc" / "k1000.yaml")
    assert load_profile("m4", shipped, probe=False).io_latency_ms == 3.8
    assert load_profile("dev", shipped).io_latency_ms is None
    assert load_profile("m4", {"profiles": {"m4": {"io_latency_ms": 5}}}, probe=False).io_latency_ms == 5.0
    assert load_profile("m4", {"profiles": {"m4": {"io_latency_ms": None}}}, probe=False).io_latency_ms is None
    assert load_profile("dev", {"profiles": {"dev": {"io_latency_ms": 1.2}}}).io_latency_ms == 1.2
    for junk in ("3.8", True, -1, 51, float("nan")):
        p = load_profile("m4", {"profiles": {"m4": {"io_latency_ms": junk}}}, probe=False)
        assert p.io_latency_ms == 3.8 and any("io_latency_ms" in w for w in p.warnings), junk


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


def make(tmp_path: Path, *, io: float | None = 3.8, settings: dict[str, Any] | None = None,
         engine_log: Path | None = None) -> tuple[Controller, LevelClient]:
    s = st.State()
    profile = dataclasses.replace(DEV, io_latency_ms=io)
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
            == (20.3, True, True, 536, 10.0)
        assert ctl.version == v                                      # unchanged: no wake-up
        cl.level = 540
        await ctl.poll_once()
        assert ctl.snapshot()[2].latency_ms == 20.4 and ctl.version > v
        await ctl.stop()

    run(body())
    lines = [r.getMessage() for r in caplog.records if r.name == "k1000.latency"]
    assert lines == ["latency: buffer=536 frames, about 20.3 ms in to out (limit 10.0)"]


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
        assert ctl.status.latency_ms == 20.3
        await cl.drop()
        await until(lambda: ctl.status.latency_ms is None, what="readout cleared")
        assert ctl.status.buffer_level is None
        await cl.reconnect()
        await until(lambda: ctl.engine_ready, what="sync")
        await ctl.poll_once()                                        # devices from the connect sync
        assert ctl.status.latency_ms == 20.3
        await ctl.stop()

    run(body())
    lines = [r.getMessage() for r in caplog.records if r.name == "k1000.latency"]
    assert len(lines) == 2


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
