# SPDX-License-Identifier: GPL-3.0-or-later
"""The latency readout of D-018: the box shows its delay in to out, and says so above the limit.

Why it is shaped like this:

- **An estimate from what the engine already reports.** Capture and playback are one device on
  one clock, so the samples inside the engine stay constant while nothing stops (docs/16 §2.2):
  the playback buffer level plus the chunk being collected on the capture side. The interface
  adds its converters and USB, a fixed share measured through a loop cable
  (`profiles.<name>.io_latency_ms`: 3.8 ms on the M4, 16 Oct). On the bench, 536 + 256 frames at
  48 kHz + 3.8 ms = 20.3 ms, the loop measurement of 16 Oct. A profile without that figure (dev)
  shows the engine's share alone, marked as a floor (`io` False). A backlog held in the engine's
  queues after a dropout shows here only in part (docs/16 §2.2): this is a floor, not a loop
  measurement.
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
  are counted with k1000-soak.sh's pattern, read from where the last read stopped, in a worker
  thread (blocking file I/O never runs in the event loop). A size rotation keeps the count going
  (the old file is read to its end through the descriptor still open); the engine's first line
  ("CamillaDSP version") sets it back to 0. A log that cannot be read gives None: nothing shown.
"""
from __future__ import annotations

import logging
import math
import os
import re
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
# k1000-soak.sh's pattern (CamillaDSP v5's wording: "PB: Prepare playback after buffer underrun",
# "Capture: read overrun", "Capture device is stalled", PlaybackError...).
DROPOUT = re.compile(rb"underrun|overrun|xrun|stalled|PlaybackError|CaptureError", re.IGNORECASE)
ENGINE_START = re.compile(rb"CamillaDSP version")      # the first line of every engine run
READ_CHUNK = 64 * 1024
MAX_LINE = 64 * 1024             # a line longer than this is not one of the engine's
MAX_BACK = 50                    # rotated logs read back at most (the unit keeps 50)


def latency_limit_ms(settings: Any) -> float:
    """k1000.yaml `engine.latency_limit_ms` (D-018: 10 ms). Anything unreadable -> the default;
    bounded to LIMIT_RANGE."""
    sec = settings.get("engine") if isinstance(settings, dict) else None
    v = sec.get("latency_limit_ms", LIMIT_MS) if isinstance(sec, dict) else LIMIT_MS
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return LIMIT_MS
    return float(min(LIMIT_RANGE[1], max(LIMIT_RANGE[0], v)))


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


class DropoutCounter:
    """Dropout lines of the engine's current run, from its live log, read a piece at a time.

    `read()` does blocking file I/O: run it in a worker thread, one call at a time. It returns
    the count, or None while the log cannot be read. The first read also counts back through the
    rotated logs (`<stem>_r<time>.log`, newest first, at most MAX_BACK) when the live one does
    not hold the engine's first line: a k1000 restart in the middle of a long run still counts
    from the engine's start."""

    def __init__(self, path: str | Path = ENGINE_LOG, max_back: int = MAX_BACK) -> None:
        self.path = Path(path)
        self.max_back = max_back
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
            self._close()
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

    def _line(self, line: bytes) -> None:
        if ENGINE_START.search(line):
            self.count = 0
            self._started = True
        elif DROPOUT.search(line):
            self.count += 1

    def _drain(self) -> None:
        f = self._f
        if f is None:
            return
        try:
            while True:
                chunk = f.read(READ_CHUNK)
                if not chunk:
                    break
                lines = (self._tail + chunk).split(b"\n")
                self._tail = lines.pop()             # a line still being written
                if len(self._tail) > MAX_LINE:
                    self._tail = b""
                for line in lines:
                    self._line(line)
        except OSError:
            pass

    def _close(self) -> None:
        if self._tail:
            self._line(self._tail)                   # a closed file's last line is complete
            self._tail = b""
        if self._f is not None:
            try:
                self._f.close()
            except OSError:
                pass
        self._f = None

    def _count_back(self) -> int:
        """Dropout lines after the engine's first line in the rotated logs before the live one."""
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
        n = 0
        for p in older[: self.max_back]:
            try:
                data = p.read_bytes()
            except OSError:
                continue
            k, started = 0, False
            for line in data.split(b"\n"):
                if ENGINE_START.search(line):
                    k, started = 0, True
                elif DROPOUT.search(line):
                    k += 1
            n += k
            if started:
                break
        return n
