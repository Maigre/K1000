# SPDX-License-Identifier: GPL-3.0-or-later
"""A time-driven MCP23017 + INT lines for timing tests and the knobtrace bench (no hardware).

Why: whether the knob worker keeps up with a fast turn depends on WHEN it gets to read the
chip (the GIL, the event loop's CPU work, the renderer), not on what it reads. Here the knob is
a schedule of contact levels at known instants, so a test can drive the REAL `ZynthianControls`
worker thread with a 45 clicks/s turn and know exactly how many clicks it must count, and how
late each transition was sampled.

What is modelled (DS20001952, IOCON = 0, INTCON = 0, every pin interrupt-on-change):
- a port's INT falls at the first level change on that port after its last clearing read;
  INTCAP holds the port's levels at that instant; INTF says which pins changed then;
- a block read starting at GPPUA (8 bytes) or INTFA (6 bytes) returns the levels at the
  moment the bytes are latched (the end of the transfer) and clears both ports;
- the transfer takes `transfer_s` (0.9 ms: 8 bytes at the box's 100 kHz) in a sleep, so the
  calling thread releases the GIL and must win it back, exactly like smbus2's ioctl;
- `SimLines.wait` sleeps until the next fall (GIL released, like gpiod's wait), and
  `read_events` reports each fall once.
Writes are accepted and kept (configure/verify pass); force_interrupt is not modelled.
"""
from __future__ import annotations

import bisect
import threading
import time
from typing import Callable, Iterable, Optional, Sequence

from .mcp23017 import (DEFAULT_ENCODERS, EXPECTED_CONFIG, GPPUA, INTFA, IOCON, pin_index)

RING_CW = (3, 1, 0, 2)          # A<<1|B from the open/open detent, clockwise (zyncoder's)
TRANSFER_S = 0.0009


class SimChip:
    """The chip side: smbus-like (read_i2c_block_data, read_byte_data, write_byte_data)."""

    def __init__(self, idle: int = 0xFFFF, transfer_s: float = TRANSFER_S,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.clock, self.sleep, self.transfer_s = clock, sleep, transfer_s
        self.lock = threading.Lock()
        self._t: list[float] = [-1e18]          # change instants (sorted) and the levels from then
        self._lv: list[int] = [idle & 0xFFFF]
        self._clear = [clock(), clock()]        # last clearing read, per port
        self.regs = dict(EXPECTED_CONFIG)
        self.regs.setdefault(IOCON, 0)
        self.reads = 0

    # -- the schedule ---------------------------------------------------------------------------
    def at(self, t: float, levels: int) -> None:
        """From instant `t` on, the pins read `levels` (B << 8 | A). Call in time order."""
        with self.lock:
            if t <= self._t[-1]:
                raise ValueError("schedule changes in time order")
            if (levels & 0xFFFF) != self._lv[-1]:
                self._t.append(t)
                self._lv.append(levels & 0xFFFF)

    def levels_at(self, t: float) -> int:
        return self._lv[bisect.bisect_right(self._t, t) - 1]

    def _first_change(self, port: int, after: float) -> Optional[float]:
        """When the first level change on `port` after `after` happens (None: never)."""
        mask = 0xFF << (8 * port)
        i = bisect.bisect_right(self._t, after)
        prev = self._lv[i - 1]
        for k in range(i, len(self._t)):
            if (self._lv[k] ^ prev) & mask:
                return self._t[k]
            prev = self._lv[k]
        return None

    def fall(self, port: int) -> Optional[float]:
        """The instant `port`'s INT fell since its last clear (None: not asserted yet)."""
        return self._first_change(port, self._clear[port])

    def asserted(self, port: int, now: float) -> bool:
        f = self.fall(port)
        return f is not None and f <= now

    # -- smbus API --------------------------------------------------------------------------------
    def read_i2c_block_data(self, addr: int, reg: int, length: int) -> list[int]:
        if self.transfer_s > 0:
            self.sleep(self.transfer_s)          # the GIL is released, as in the real ioctl
        with self.lock:
            self.reads += 1
            now = self.clock()
            if reg == 0x00:                      # verify(): the config registers
                return [self.regs.get(r, 0) for r in range(length)]
            if reg not in (GPPUA, INTFA):
                raise ValueError(f"SimChip: block read at 0x{reg:02x} not modelled")
            intf, cap = [0, 0], [0, 0]
            for port in (0, 1):
                f = self.fall(port)
                if f is not None and f <= now:
                    k = bisect.bisect_right(self._t, f) - 1      # the change at instant f
                    before, at_f = self._lv[k - 1], self._lv[k]
                    intf[port] = ((before ^ at_f) >> (8 * port)) & 0xFF
                    cap[port] = (at_f >> (8 * port)) & 0xFF
            lv = self.levels_at(now)
            self._clear = [now, now]
            out = [intf[0], intf[1], cap[0], cap[1], lv & 0xFF, lv >> 8]
            return ([0xFF, 0xFF] + out) if reg == GPPUA else out

    def read_byte_data(self, addr: int, reg: int) -> int:
        with self.lock:
            if reg in (0x12, 0x13):              # GPIOA/B
                return (self.levels_at(self.clock()) >> (8 * (reg - 0x12))) & 0xFF
            return self.regs.get(reg, 0)

    def write_byte_data(self, addr: int, reg: int, value: int) -> None:
        with self.lock:
            self.regs[reg] = value & 0xFF

    def close(self) -> None:
        pass


class SimLines:
    """INTA/INTB of a SimChip, gpiod-like (wait / read_events / levels / close)."""

    def __init__(self, chip: SimChip, int_a: int = 27, int_b: int = 4) -> None:
        self.chip, self.offs = chip, (int_a, int_b)
        self._seen: list[Optional[float]] = [None, None]   # the last fall reported, per port

    def _pending(self, now: float) -> list[int]:
        out = []
        for port in (0, 1):
            f = self.chip.fall(port)
            if f is not None and f <= now and f != self._seen[port]:
                out.append(port)
        return out

    def wait(self, timeout: float) -> bool:
        deadline = self.chip.clock() + max(0.0, timeout)
        while True:
            with self.chip.lock:
                now = self.chip.clock()
                if self._pending(now):
                    return True
                nxt = [f for f in (self.chip.fall(0), self.chip.fall(1)) if f is not None and f > now]
            if now >= deadline:
                return False
            self.chip.sleep(max(0.0, min([deadline] + nxt) - now))

    def read_events(self) -> list[int]:
        with self.chip.lock:
            now = self.chip.clock()
            out = []
            for port in self._pending(now):
                self._seen[port] = self.chip.fall(port)
                out.append(self.offs[port])
            return out

    def levels(self) -> dict[int, int]:
        with self.chip.lock:
            now = self.chip.clock()
            return {self.offs[p]: 0 if self.chip.asserted(p, now) else 1 for p in (0, 1)}

    def close(self) -> None:
        pass


def with_state(levels: int, pins: tuple[int, int], state: int) -> int:
    a, b = pins
    levels &= ~((1 << a) | (1 << b))
    return levels | (((state >> 1) & 1) << a) | ((state & 1) << b)


def schedule_turn(chip: SimChip, t0: float, enc: int, clicks: int, rate_cps: float,
                  encoders: Sequence[tuple] = DEFAULT_ENCODERS, spd: int = 4) -> float:
    """A uniform turn of `clicks` detents (negative = counter-clockwise) at `rate_cps` clicks/s
    on encoder `enc`, starting at `t0`, from wherever its contacts are (on a detent). Returns
    the instant of its last transition. The transitions are listed in `chip.transitions`."""
    pins = tuple(pin_index(p) for p in encoders[enc])
    lv = chip._lv[-1]
    cur = ((lv >> pins[0]) & 1) << 1 | ((lv >> pins[1]) & 1)
    pos = RING_CW.index(cur)
    step = 1 if clicks > 0 else -1
    dt = 1.0 / (rate_cps * spd)
    t = t0
    log = getattr(chip, "transitions", None)
    if log is None:
        log = chip.transitions = []          # type: ignore[attr-defined]
    for _ in range(abs(clicks) * spd):
        pos = (pos + step) % 4
        lv = with_state(lv, pins, RING_CW[pos])
        chip.at(t, lv)
        log.append((t, enc))
        t += dt
    return t - dt


def edge_latencies(transitions: Iterable[tuple[float, int]],
                   samples: Sequence[tuple[float, int]]) -> list[float]:
    """For each scheduled transition: how long until the first sample taken at or after it
    (seconds; the edge -> sample delay the decoder lives with)."""
    ts = [s[0] for s in samples]
    out = []
    for t, _enc in transitions:
        i = bisect.bisect_left(ts, t)
        if i < len(ts):
            out.append(ts[i] - t)
    return out


__all__ = ["RING_CW", "SimChip", "SimLines", "TRANSFER_S", "edge_latencies", "schedule_turn",
           "with_state"]
