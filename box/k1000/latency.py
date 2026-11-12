# SPDX-License-Identifier: GPL-3.0-or-later
"""The latency readout of D-018: the box shows its delay in to out, and says so above the limit.

Why it is shaped like this:

- **An estimate from what the engine already reports.** Capture and playback are one device on
  one clock, so the samples inside the engine stay constant while nothing stops (docs/16 §2.2):
  the playback buffer level plus the chunk being collected on the capture side. The interface
  adds its converters and USB, measured through a loop cable (`profiles.<name>.io_latency_ms`),
  and the level itself reads about a quarter chunk low, because the device reports its position
  once per period, half a chunk (`io_share_ms`). Bench of 12 Nov, the M4 on both engines, chunk
  32 to 256, 27 starts: in to out = (level + chunk) / rate + 2.3 ms + a quarter chunk, within
  0.3 ms. At 536 + 256 frames that is 20.1 ms for a measured 20.2 to 20.3. A profile without the
  figure (dev) shows the engine's share alone, marked as a floor (`io` False). An estimate, not
  a loop measurement.
- **One more read a second, nothing written.** The control service's status poll adds one
  GetBufferLevel; chunksize and samplerate come from the running config the poll already reads.
  The engine refreshes the level about every 10 s (its adjust period) and reads 0 until its first
  average: no reading then.
- **It informs, it never acts.** Above `engine.latency_limit_ms` (k1000.yaml, 10 ms by default:
  D-018) the readout is flagged; nothing mutes, restarts or changes the sound.
- **A gig leaves a trace in the journal**: one line at the first reading after K1000 connects to
  the engine, one each time the estimate moves by more than one chunk from the last line, and one
  every LOG_EVERY_S otherwise.
- **Dropouts since the engine started** (`DropoutCounter`): the engine writes one line per
  dropout in its live log (`camilladsp -o`, rotated at every start and every 256 kB). The lines
  are counted with k1000-soak.sh's words, read from where the last read stopped, in a worker
  thread (blocking file I/O never runs in the event loop), with C-level searches (`bytes.find`),
  not a Python loop per line. A size rotation keeps the count going (the old file is read to its
  end through the descriptor still open). Two things set it back to 0 at an engine start, so
  that neither is relied on alone: the engine's first line ("CamillaDSP version", in beta.1 and
  in the next5 source) and a rotation that left the old file shorter than the rotation size (a
  size rotation leaves it at that size or more: flexi_logger's Criterion::Size, not read in its
  source, to confirm on the box with `ls -l /var/log/k1000`). A log that cannot be read gives
  None: nothing shown.
- **After a dropout the figure is a floor.** The playback buffer holds 1024 frames at most with
  today's settings: a backlog past that sits in the engine's queues, which GetBufferLevel does
  not see (docs/16 §2.2, read in the engine source, not measured). The screen and the phone mark
  it "≥" / "at least" once the count is above 0.
"""
from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable

log = logging.getLogger(__name__)

LIMIT_MS = 10.0                  # D-018: in to out, at any moment of a show
LIMIT_RANGE = (1.0, 1000.0)
LOG_EVERY_S = 600.0              # a journal line at least this often while readings come
DROPOUT_READ_S = 10.0            # the engine log is read at most this often
# camilladsp.service: `-o /var/log/k1000/camilladsp.log --log_rotate_size ...` writes this file.
ENGINE_LOG = Path("/var/log/k1000/camilladsp_rCURRENT.log")
LIVE_SUFFIX = "_rCURRENT"
# k1000-soak.sh's pattern, as lower-case words: a line holding any of them, in any case, is one
# dropout (CamillaDSP v5's wording: "PB: Prepare playback after buffer underrun", "Capture: read
# overrun", "Capture device is stalled", PlaybackError...). A test keeps them in step.
DROPOUT_WORDS = (b"underrun", b"overrun", b"xrun", b"stalled", b"playbackerror", b"captureerror")
ENGINE_START = b"CamillaDSP version"   # the first line of every engine run (bin.rs, beta.1 and next5)
ROTATE_SIZE = 256000             # camilladsp.service's --log_rotate_size (a test keeps them in step)
READ_CHUNK = 64 * 1024
MAX_LINE = 64 * 1024             # a line longer than this is not one of the engine's
MAX_BACK_BYTES = 2 * 1024 * 1024  # the first read goes back through at most this much rotated log


def latency_limit_ms(settings: Any) -> float:
    """k1000.yaml `engine.latency_limit_ms` (D-018: 10 ms). Anything unreadable -> the default;
    bounded to LIMIT_RANGE."""
    sec = settings.get("engine") if isinstance(settings, dict) else None
    v = sec.get("latency_limit_ms", LIMIT_MS) if isinstance(sec, dict) else LIMIT_MS
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return LIMIT_MS
    return float(min(LIMIT_RANGE[1], max(LIMIT_RANGE[0], v)))


def io_share_ms(io_latency_ms: float | None, chunksize: int, samplerate: float) -> float | None:
    """What is added to the engine's share: the interface's own latency (the profile's
    `io_latency_ms`) plus a quarter chunk, the average by which the buffer level reads low (the
    device's position moves once per period, half a chunk). None stays None: not known."""
    if io_latency_ms is None:
        return None
    return float(io_latency_ms) + 0.25 * chunksize / samplerate * 1000.0


def run_devices(cfg: Any) -> tuple[int, float] | None:
    """(chunksize, samplerate) of a config as GetConfigJson gives it; None when unusable."""
    dev = cfg.get("devices") if isinstance(cfg, dict) else None
    if not isinstance(dev, dict):
        return None
    chunk, rate = dev.get("chunksize"), dev.get("samplerate")
    if isinstance(chunk, bool) or not isinstance(chunk, int) or chunk <= 0:
        return None
    if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not math.isfinite(rate) \
            or rate <= 0:
        return None
    return chunk, float(rate)


@dataclass(frozen=True)
class Reading:
    """One estimate: the playback buffer level (frames) and what it means in ms."""

    level: int
    chunksize: int
    samplerate: float
    io_ms: float | None          # the interface's share (the profile's), None = not known
    limit_ms: float = LIMIT_MS

    @property
    def engine_ms(self) -> float:
        return (self.level + self.chunksize) / self.samplerate * 1000.0

    @property
    def ms(self) -> float:
        """In to out, 0.1 ms: the engine's share plus the interface's when known."""
        return round(self.engine_ms + (self.io_ms or 0.0), 1)

    @property
    def chunk_ms(self) -> float:
        return self.chunksize / self.samplerate * 1000.0

    @property
    def over(self) -> bool:
        return self.ms > self.limit_ms

    def line(self) -> str:
        """The journal line."""
        if self.io_ms is None:
            return (f"latency: buffer={self.level} frames, at least {self.ms:.1f} ms in to out "
                    f"(engine only, the interface not known; limit {self.limit_ms:.1f})")
        return (f"latency: buffer={self.level} frames, about {self.ms:.1f} ms in to out "
                f"(limit {self.limit_ms:.1f})")


class LatencyWatch:
    """The last reading, and when the journal gets a line. `note_config` with every running
    config read, `note` with every poll, `lost` when the engine link goes."""

    def __init__(self, limit_ms: float = LIMIT_MS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.limit_ms = limit_ms
        self._clock = clock
        self._devices: tuple[int, float] | None = None
        self.reading: Reading | None = None
        self._logged: Reading | None = None      # the last line's reading (None: none on this link)
        self._logged_at = -math.inf

    def note_config(self, cfg: Any) -> None:
        d = run_devices(cfg)
        if d is not None:
            self._devices = d

    def lost(self) -> None:
        """The engine link is gone: no reading until the next one, which gets a journal line."""
        self._devices = None
        self.reading = None
        self._logged = None

    def io_share(self, io_latency_ms: float | None) -> float | None:
        """`io_share_ms` for the running chunk and rate (the profile's figure alone until the
        running config is known)."""
        if io_latency_ms is None or self._devices is None:
            return io_latency_ms
        return io_share_ms(io_latency_ms, *self._devices)

    def note(self, engine_state: Any, level: Any, io_ms: float | None) -> Reading | None:
        """One poll: the engine's state and its GetBufferLevel value (None = not read) -> the
        reading to show (None = none: not running, no average yet, config not read yet)."""
        r = None
        if (engine_state == "Running" and self._devices is not None
                and isinstance(level, (int, float)) and not isinstance(level, bool)
                and math.isfinite(level) and level >= 1):
            r = Reading(int(round(level)), *self._devices, io_ms=io_ms, limit_ms=self.limit_ms)
        self.reading = r
        if r is not None and self._due(r):
            log.info("%s", r.line())
            self._logged, self._logged_at = r, self._clock()
        return r

    def _due(self, r: Reading) -> bool:
        last = self._logged
        if last is None:
            return True                                    # the first reading on this link
        if (r.chunksize, r.samplerate, r.io_ms) != (last.chunksize, last.samplerate, last.io_ms):
            return True
        if abs(r.engine_ms - last.engine_ms) > r.chunk_ms:
            return True                                    # moved by more than one chunk
        return self._clock() - self._logged_at >= LOG_EVERY_S


def scan(data: bytes) -> tuple[int, bool]:
    """Whole log lines -> (dropout lines after the last engine start line, whether one was there).
    Only C-level searches: a 256 kB log costs a few ms, where a Python loop per line costs tens."""
    i = data.rfind(ENGINE_START)
    started = i >= 0
    if started:
        j = data.find(b"\n", i)
        data = b"" if j < 0 else data[j + 1:]
    low = data.lower()
    lines: set[int] = set()              # one dropout per line, whatever words it holds
    for w in DROPOUT_WORDS:
        k = low.find(w)
        while k >= 0:
            lines.add(low.rfind(b"\n", 0, k))
            k = low.find(w, k + len(w))
    return len(lines), started


class DropoutCounter:
    """Dropout lines of the engine's current run, from its live log, read a piece at a time.

    `read()` does blocking file I/O: run it in a worker thread, one call at a time. It returns
    the count, or None while the log cannot be read. The first read also counts back through the
    rotated logs (`<stem>_r<time>.log`, newest first) when the live one does not hold the
    engine's first line, so that a k1000 restart in the middle of a long run still counts from
    the engine's start. It stops at that start (the first line, or a file shorter than
    `rotate_size`: the last one of the run before) and after `max_back_bytes`, past which the
    count falls short."""

    def __init__(self, path: str | Path = ENGINE_LOG, *, rotate_size: int = ROTATE_SIZE,
                 max_back_bytes: int = MAX_BACK_BYTES) -> None:
        self.path = Path(path)
        self.rotate_size = rotate_size
        self.max_back_bytes = max_back_bytes
        self.count = 0
        self._f: BinaryIO | None = None
        self._ident: tuple[int, int] | None = None
        self._tail = b""
        self._opened = False                 # a file was opened once: no more reading back
        self._started = False                # the engine's first line was seen

    def read(self) -> int | None:
        try:
            st: os.stat_result | None = os.stat(self.path)
        except OSError:
            st = None
        if self._f is not None and (st is None or (st.st_dev, st.st_ino) != self._ident):
            self._drain()                    # rotated: the old file to its end, then let go
            size = self._size()
            self._close()
            if size is not None and size < self.rotate_size:
                self.count = 0               # rotated at an engine start, whatever its first line
        if st is None:
            return None
        if self._f is None:
            try:
                f = open(self.path, "rb")
                fst = os.fstat(f.fileno())
            except OSError:
                return None
            self._f, self._ident = f, (fst.st_dev, fst.st_ino)
            first, self._opened = not self._opened, True
            if first:
                self._started = False
                self._drain()
                if not self._started:
                    self.count += self._count_back()
                return self.count
        elif st.st_size < self._f.tell():    # truncated in place: read it again from the start
            self._f.seek(0)
            self._tail = b""
        self._drain()
        return self.count

    def close(self) -> None:
        self._close()

    def _lines(self, data: bytes) -> None:
        k, started = scan(data)
        if started:
            self.count, self._started = k, True
        else:
            self.count += k

    def _drain(self) -> None:
        f = self._f
        if f is None:
            return
        try:
            while True:
                chunk = f.read(READ_CHUNK)
                if not chunk:
                    break
                buf = self._tail + chunk
                cut = buf.rfind(b"\n") + 1
                self._tail = buf[cut:]               # a line still being written
                if len(self._tail) > MAX_LINE:
                    self._tail = b""
                if cut:
                    self._lines(buf[:cut])
        except OSError:
            pass

    def _size(self) -> int | None:
        try:
            return os.fstat(self._f.fileno()).st_size if self._f is not None else None
        except OSError:
            return None

    def _close(self) -> None:
        if self._tail:
            self._lines(self._tail)                  # a closed file's last line is complete
            self._tail = b""
        if self._f is not None:
            try:
                self._f.close()
            except OSError:
                pass
        self._f = None

    def _count_back(self) -> int:
        """Dropout lines of this engine run in the rotated logs before the live one."""
        name = self.path.name
        stem, dot, ext = name.rpartition(".")
        if not dot or not stem.endswith(LIVE_SUFFIX):
            return 0
        base = stem[: -len(LIVE_SUFFIX)]
        try:
            older = sorted((p for p in self.path.parent.glob(f"{base}_r*.{ext}") if p.name != name),
                           key=lambda p: p.name, reverse=True)
        except OSError:
            return 0
        n, budget = 0, self.max_back_bytes
        for p in older:
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if size < self.rotate_size:
                break                        # rotated at an engine start: the run before's last
            budget -= size
            if budget < 0:
                break                        # a very long run: the count stays short
            try:
                data = p.read_bytes()
            except OSError:
                continue
            k, started = scan(data)
            n += k
            if started:
                break
            time.sleep(0)                    # the GIL back to the event loop between files
        return n
