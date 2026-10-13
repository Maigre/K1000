# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for k1000.hw.mcp23017 (Zynthian V4 knobs + buttons).

Unit tests drive a register-level fake MCP23017 (INTF/INTCAP latching, clear-on-read, DEFVAL
mode, BANK=1 addressing, I2C faults) and fake INT lines. Hardware tests are opt-in: -m hw.
"""

from __future__ import annotations

import asyncio
import collections
import pathlib
import random
import re
import subprocess
import sys
import threading
import time

import pytest

from k1000.hw import mcp23017 as m
from k1000.hw.mcp23017 import (
    DEFAULT_ENCODERS, DEFAULT_SWITCHES, EXPECTED_CONFIG, LONG, PRESS, RELEASE, SHORT,
    Accelerator, EncoderEvent, Mcp23017, QuadratureDecoder, SwitchDebouncer, SwitchEvent,
    ZynthianControls, accel_multiplier, controls_options, parse_detent, pin_index,
)

# ---------------------------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------------------------
R = {name: getattr(m, name) for name in (
    "IODIRA IODIRB IPOLA IPOLB GPINTENA GPINTENB DEFVALA DEFVALB INTCONA INTCONB IOCON "
    "GPPUA GPPUB INTFA INTFB INTCAPA INTCAPB GPIOA GPIOB").split()}
OLATA, OLATB = 0x14, 0x15
# BANK=1 raw address -> canonical (BANK=0) register
_BANK1 = {}
for i, (ra, rb) in enumerate([(0x00, 0x01), (0x02, 0x03), (0x04, 0x05), (0x06, 0x07), (0x08, 0x09),
                              (0x0A, 0x0A), (0x0C, 0x0D), (0x0E, 0x0F), (0x10, 0x11), (0x12, 0x13),
                              (OLATA, OLATB)]):
    _BANK1[i] = ra
    _BANK1[0x10 + i] = rb


class FakeChip:
    """Enough of an MCP23017 (datasheet DS20001952) to exercise the driver's interrupt logic."""

    def __init__(self, ext: int = 0xFFFF, bank1: bool = False) -> None:
        self.lock = threading.RLock()
        self.lines: "FakeLines | None" = None
        self.writes: list[tuple[int, int]] = []
        self.fail = 0
        self.transactions = 0
        self.ext = ext
        self.power_on_reset()
        if bank1:
            self.r[R["IOCON"]] = 0x80

    def power_on_reset(self) -> None:
        with self.lock:
            self.r = [0] * 0x16
            self.r[R["IODIRA"]] = self.r[R["IODIRB"]] = 0xFF

    # -- helpers for tests
    @property
    def bank(self) -> int:
        return (self.r[R["IOCON"]] >> 7) & 1

    def int_level(self, port: int) -> int:
        return 0 if self.r[R["INTFA"] + port] else 1

    def level(self, port: int) -> int:
        return (self.ext >> (8 * port)) & 0xFF

    def set_ext(self, ext: int) -> None:
        with self.lock:
            old, self.ext = self.ext, ext & 0xFFFF
            for port in (0, 1):
                prev, new = (old >> (8 * port)) & 0xFF, self.level(port)
                en, con, dv = (self.r[R["GPINTENA"] + port], self.r[R["INTCONA"] + port],
                               self.r[R["DEFVALA"] + port])
                cond = en & ((con & (new ^ dv)) | (~con & (prev ^ new))) & 0xFF
                if cond:
                    self._assert(port, cond)

    def _assert(self, port: int, cond: int) -> None:
        was = self.r[R["INTFA"] + port]
        if not was:
            self.r[R["INTCAPA"] + port] = self.level(port)
        self.r[R["INTFA"] + port] |= cond
        if not was and self.lines is not None:
            self.lines.fall(port)

    def _persistent(self, port: int) -> None:
        """DEFVAL-mode pins re-assert as long as they differ from DEFVAL."""
        cond = (self.r[R["GPINTENA"] + port] & self.r[R["INTCONA"] + port]
                & (self.level(port) ^ self.r[R["DEFVALA"] + port]))
        if cond:
            self._assert(port, cond)

    def _clear(self, port: int) -> None:
        self.r[R["INTFA"] + port] = 0
        self._persistent(port)

    def _canon(self, addr: int) -> int:
        if self.bank:
            return _BANK1.get(addr, 0x15)
        return R["IOCON"] if addr == 0x0B else addr

    def _txn(self) -> None:
        self.transactions += 1
        if self.fail > 0:
            self.fail -= 1
            raise OSError(121, "Remote I/O error")

    def _read(self, c: int) -> int:
        if c in (R["GPIOA"], R["GPIOB"]):
            port = c - R["GPIOA"]
            val = self.level(port)
            self._clear(port)
            return val
        if c in (R["INTCAPA"], R["INTCAPB"]):
            port = c - R["INTCAPA"]
            val = self.r[c]
            self._clear(port)
            return val
        return self.r[c]

    # -- smbus API
    def read_byte_data(self, addr: int, reg: int) -> int:
        with self.lock:
            self._txn()
            return self._read(self._canon(reg))

    def write_byte_data(self, addr: int, reg: int, value: int) -> None:
        with self.lock:
            self._txn()
            self.writes.append((reg, value))
            c = self._canon(reg)
            if c in (R["INTFA"], R["INTFB"], R["INTCAPA"], R["INTCAPB"]):
                return
            self.r[c] = value & 0xFF
            for port in (0, 1):
                self._persistent(port)

    def read_i2c_block_data(self, addr: int, reg: int, length: int) -> list[int]:
        assert self.bank == 0, "sequential reads only modelled in BANK=0"
        with self.lock:
            self._txn()
            return [self._read(self._canon((reg + i) % 0x16)) for i in range(length)]

    def close(self) -> None:
        pass


class FakeLines:
    def __init__(self, chip: FakeChip, int_a: int = 27, int_b: int = 4) -> None:
        self.chip = chip
        chip.lines = self
        self.map = {0: int_a, 1: int_b}
        self.ev = threading.Event()
        self.q: list[int] = []
        self.drop = 0
        self.closed = False

    def fall(self, port: int) -> None:
        if self.drop:
            self.drop -= 1
            return
        self.q.append(self.map[port])
        self.ev.set()

    def wait(self, timeout: float) -> bool:
        return self.ev.wait(timeout)

    def read_events(self) -> list[int]:
        with self.chip.lock:
            self.ev.clear()
            q, self.q = self.q, []
        return q

    def levels(self) -> dict[int, int]:
        with self.chip.lock:
            return {self.map[0]: self.chip.int_level(0), self.map[1]: self.chip.int_level(1)}

    def close(self) -> None:
        self.closed = True


class Clock:
    def __init__(self, t: float = 100.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


ENC = [(pin_index(a), pin_index(b)) for a, b in DEFAULT_ENCODERS]
SW = [pin_index(p) for p in DEFAULT_SWITCHES]
CW = [3, 1, 0, 2, 3]   # A<<1|B, one detent clockwise from the open/open rest (zyncoder: A leads)


def with_enc(ext: int, i: int, state: int) -> int:
    a, b = ENC[i]
    ext &= ~((1 << a) | (1 << b))
    return ext | (((state >> 1) & 1) << a) | ((state & 1) << b)


def with_sw(ext: int, i: int, pressed: bool) -> int:
    return ext & ~(1 << SW[i]) if pressed else ext | (1 << SW[i])


def make(chip: FakeChip, clock=None, **kw) -> ZynthianControls:
    kw.setdefault("lines_factory", lambda c, o: FakeLines(chip))
    return ZynthianControls(bus_factory=lambda n: chip, clock=clock or time.monotonic, **kw)


def drain(c: ZynthianControls) -> list:
    out = list(c._pending)
    c._pending.clear()
    return out


def run_states(dec: QuadratureDecoder, states, t0: float = 0.0, dt: float = 0.005) -> list[int]:
    return [dec.update(s, t0 + k * dt) for k, s in enumerate(states)]


# ---------------------------------------------------------------------------------------------
# Quadrature decoder
# ---------------------------------------------------------------------------------------------
class TestDecoder:
    def test_cw_one_detent_emits_on_return_to_rest(self):
        d = QuadratureDecoder(4, state=3)
        assert run_states(d, CW[1:]) == [0, 0, 0, 1]

    def test_ccw_one_detent(self):
        d = QuadratureDecoder(4, state=3)
        assert run_states(d, list(reversed(CW))[1:]) == [0, 0, 0, -1]

    def test_several_detents_both_ways(self):
        d = QuadratureDecoder(4, state=3)
        seq = CW[1:] * 3 + list(reversed(CW))[1:] * 5
        assert sum(run_states(d, seq)) == 3 - 5

    def test_bounce_at_rest_emits_nothing(self):
        d = QuadratureDecoder(4, state=3)
        assert run_states(d, [1, 3, 1, 3, 2, 3, 2, 3]) == [0] * 8
        assert d.acc == 0

    def test_bounce_mid_cycle_still_one_detent(self):
        d = QuadratureDecoder(4, state=3)
        assert sum(run_states(d, [1, 0, 1, 0, 2, 0, 2, 3])) == 1

    def test_half_turn_and_back_is_zero(self):
        d = QuadratureDecoder(4, state=3)
        assert sum(run_states(d, [1, 0, 1, 3])) == 0

    def test_skipped_state_mid_cycle_inferred_from_direction(self):
        d = QuadratureDecoder(4, state=3)
        assert run_states(d, CW[1:]) == [0, 0, 0, 1]          # the knob is turning CW
        # 3 -> 1 (+1), 1 -> 2 skips 0 (both bits flip) -> +2, since the knob left the detent
        # already turning CW, 2 -> 3 (+1) = one detent
        assert run_states(d, [1, 2, 3], t0=0.025) == [0, 0, 1]
        assert d.invalid == 0

    def test_a_skip_in_a_first_click_from_stillness_needs_two_steps_first(self):
        # LAS-3: the excursion's own first edge is not "a recent move". 11 -> 01 -> 10 -> 11 on
        # a resting knob (INTCAP 01 then GPIO 10 in one read) is a glitch, never a click.
        d = QuadratureDecoder(4, state=3)
        assert run_states(d, [1, 2, 3]) == [0, 0, 0]
        assert d.invalid == 1 and d.acc == 0
        assert run_states(d, [2, 1, 3], t0=1.0) == [0, 0, 0]
        # two counted steps that way since it left (|acc| >= 2): the skip is the turn's
        assert run_states(d, [1, 0, 3], t0=2.0) == [0, 0, 1]
        assert d.invalid == 2
        # and the next click, left while turning, has its skip credited at once
        assert run_states(d, [1, 2, 3], t0=2.015) == [0, 0, 1]

    def test_a_click_from_stillness_faster_than_a_hand_is_a_glitch(self):
        # staggered dips and releases make a valid cycle: only its speed gives it away
        for spd, n in ((4, 1), (2, 2)):
            d = QuadratureDecoder(spd, state=3)
            assert sum(run_states(d, [1, 0, 2, 3], t0=5.0, dt=0.002)) == 0
            assert d.glitches >= 1 and d.acc == 0
            # the same cycle at a hand's pace counts, from stillness or in a spin
            assert sum(run_states(d, [1, 0, 2, 3], t0=6.0, dt=0.004)) == n
            assert sum(run_states(d, [1, 0, 2, 3], t0=6.016, dt=0.001)) == n

    def test_unattributable_first_skip_never_invents_a_detent(self):
        # 3 -> 0 with no movement before: +2 or -2, unknown (v0 and the first cut of patch A
        # rounded the following 0 -> 2 -> 3 up to +1: a glitch then read as a click). Counted as
        # the smaller reading (0), and the decoder is in sync again for the next real click.
        d = QuadratureDecoder(4, state=3)
        assert run_states(d, [0, 2, 3]) == [0, 0, 0]
        assert d.invalid == 1 and d.acc == 0
        assert run_states(d, CW[1:], t0=1.0) == [0, 0, 0, 1]

    def test_skip_right_after_a_detent_uses_recent_direction(self):
        d = QuadratureDecoder(4, state=3)
        run_states(d, CW[1:])                                  # +1, acc back to 0
        assert run_states(d, [0, 2, 3], t0=0.03) == [0, 0, 1]  # 3 -> 0 skip inferred CW
        assert d.invalid == 0

    def test_fast_spin_skipping_the_rest_state_counts_both_detents(self):
        d = QuadratureDecoder(4, state=3)
        # CW 3 1 0 2 (3 missed) 1 0 2 3
        assert sum(run_states(d, [1, 0, 2, 1, 0, 2, 3])) == 2

    def test_anchor_is_the_idle_state_not_always_open_open(self):
        d = QuadratureDecoder(4, state=0)                     # enc0 idles at AB=00 on the box
        assert run_states(d, [2, 3, 1, 0]) == [0, 0, 0, 1]    # CW from 00
        assert run_states(d, [1, 3, 2, 0]) == [0, 0, 0, -1]

    def test_half_step_encoder(self):
        d = QuadratureDecoder(2, state=3)
        assert run_states(d, [1, 0, 2, 3]) == [0, 1, 0, 1]
        assert run_states(d, [2, 0]) == [0, -1]

    def test_quarter_step_encoder(self):
        d = QuadratureDecoder(1, state=3)
        assert run_states(d, [1, 0, 0, 2, 0]) == [1, 1, 0, 1, -1]

    def test_a_pause_off_the_detent_never_moves_the_anchor(self):
        d = QuadratureDecoder(4, state=3, idle_s=0.3)
        assert d.update(1, 1.0) == 0          # one quarter step, then the knob rests there
        assert d.deadline() == pytest.approx(1.3)
        assert d.settle(1.2) == 0 and d.anchor == 3
        assert d.settle(1.31) == 0            # 1/4 detent: nothing
        assert d.anchor == 3 and d.deadline() is None and d.acc == 1
        assert run_states(d, [0, 2, 3], t0=2.0) == [0, 0, 1]   # the click ends where it should

    def test_settle_flushes_three_quarters_as_one(self):
        d = QuadratureDecoder(4, state=3, idle_s=0.3)
        run_states(d, [1, 0, 2], t0=1.0)
        assert d.settle(2.0) == 1

    # -- docs/08-knob-precision.md: pauses must never change the count ------------------------
    @staticmethod
    def drive(d: QuadratureDecoder, timeline) -> list[tuple[float, int]]:
        """[(t, state)] with the driver's deadline-driven settle() in between -> emissions."""
        out = []
        for t, s in timeline:
            while d.deadline() is not None and d.deadline() <= t:
                dl = d.deadline()
                n = d.settle(dl)
                if n:
                    out.append((dl, n))
                if d.deadline() == dl:
                    break
            n = d.update(s, t)
            if n:
                out.append((t, n))
        while d.deadline() is not None:
            dl = d.deadline()
            n = d.settle(dl)
            if n:
                out.append((dl, n))
        return out

    @staticmethod
    def clicks(n, direction=1, start=3, t=1.0, gaps=(0.05, 0.05, 0.01, 0.01), hover_at=None,
               hover_s=0.0, rest_s=0.6, spd=4):
        """Timeline of `n` clicks: per-transition gaps, an optional pause after transition
        `hover_at` (the top of the click), `rest_s` on each detent."""
        ring = [3, 1, 0, 2]
        state, out = start, []
        for _ in range(n):
            for k in range(spd):
                t += gaps[k % len(gaps)]
                state = ring[(ring.index(state) + direction) % 4]
                out.append((round(t, 4), state))
                if hover_at is not None and k + 1 == hover_at:
                    t += hover_s
            t += rest_s
        return out

    def test_pause_at_the_top_of_a_click_counts_once_on_arrival(self):
        # v0 bug: a >= 0.3 s pause at AB=00 emitted the click there AND re-anchored, so every
        # slow click counted twice
        d = QuadratureDecoder(4, state=3, idle_s=0.3, rest=3, learn_rests=3)
        tl = self.clicks(5, hover_at=2, hover_s=0.5)
        out = self.drive(d, tl)
        assert sum(n for _t, n in out) == 5
        arrivals = [t for t, s in tl if s == 3]
        assert [t for t, _n in out] == arrivals          # on the detent, not by a timer
        assert d.anchor == 3 and d.relearned == 0

    def test_very_slow_turn_counts_every_click_once_never_late(self):
        # 0.5 s between contact changes: v0 counted each click 0.3 s after it ended; now it
        # counts on arrival, or at 3/4 of the way when the knob is still there for idle_s
        d = QuadratureDecoder(4, state=3, idle_s=0.3, rest=3, learn_rests=3)
        tl = self.clicks(4, gaps=(0.5, 0.5, 0.5, 0.5))
        out = self.drive(d, tl)
        assert [n for _t, n in out] == [1, 1, 1, 1]
        arrivals = [t for t, s in tl if s == 3]
        assert all(a - 0.5 <= t <= a for (t, _n), a in zip(out, arrivals))
        assert d.relearned == 0

    def test_half_a_click_and_back_after_a_pause_is_nothing(self):
        d = QuadratureDecoder(4, state=3, idle_s=0.3, rest=3)
        out = self.drive(d, [(1.0, 1), (1.05, 0), (2.0, 1), (2.05, 3)])
        assert out == []

    def test_worn_contact_one_quarter_short_counts_once(self):
        # the knob stops at AB=10 (3/4 of a click): counted after idle_s, and the last quarter
        # (10 -> 11, when it moves on) does not count it again
        d = QuadratureDecoder(4, state=3, idle_s=0.3, rest=3)
        out = self.drive(d, [(1.0, 1), (1.01, 0), (1.02, 2), (2.0, 3), (2.01, 1), (2.02, 0),
                             (2.03, 2), (2.04, 3)])
        assert sum(n for _t, n in out) == 2 and out[0] == (pytest.approx(1.32), 1)

    def test_mis_set_detent_is_learned_after_three_long_rests(self):
        # an encoder whose detents are at AB=00 while the config says 11
        d = QuadratureDecoder(4, state=0, idle_s=0.3, rest=3, learn_rests=3)
        tl = self.clicks(6, start=0, rest_s=1.5)
        out = self.drive(d, tl)
        assert sum(n for _t, n in out) == 6               # never a wrong count meanwhile
        assert d.anchor == 0 and d.relearned == 1 and d.rest == 0
        d.reset(0, 50.0)
        assert d.anchor == 0                              # an I2C re-seed keeps what was learned

    def test_hovers_between_clicks_never_relearn(self):
        d = QuadratureDecoder(4, state=3, idle_s=0.3, rest=3, learn_rests=3)
        self.drive(d, self.clicks(8, hover_at=2, hover_s=0.8, rest_s=0.35))
        assert d.anchor == 3 and d.relearned == 0

    def test_two_transition_encoder_at_spd4_raises_the_hint_not_the_count(self):
        d = QuadratureDecoder(4, state=3, idle_s=0.3, rest=3, learn_rests=3)
        out = self.drive(d, self.clicks(10, spd=2, gaps=(0.04, 0.01)))
        assert sum(n for _t, n in out) == 5               # half: the config must say 2
        assert d.half_rests >= 8 and d.relearned == 0
        d2 = QuadratureDecoder(2, state=3, idle_s=0.3, rest=3)
        assert sum(n for _t, n in self.drive(d2, self.clicks(10, spd=2, gaps=(0.04, 0.01)))) == 10
        assert d2.half_rests == 0

    def test_spd2_pause_mid_click_counts_once(self):
        d = QuadratureDecoder(2, state=3, idle_s=0.3, rest=3)
        out = self.drive(d, self.clicks(4, spd=2, gaps=(0.05, 0.01), hover_at=1, hover_s=0.6))
        assert [n for _t, n in out] == [1, 1, 1, 1]

    def test_settle_never_spins(self):
        d = QuadratureDecoder(4, state=3, idle_s=0.3, rest=3, learn_rests=3)
        d.update(1, 1.0)
        d.update(0, 1.01)                                 # half a click, then still
        assert d.deadline() == pytest.approx(1.31)
        d.settle(1.31)
        assert d.deadline() == pytest.approx(2.01)        # the learn vote
        d.settle(2.01)
        assert d.deadline() is None
        d0 = QuadratureDecoder(4, state=3, idle_s=0.3, rest=3)    # no learning: one wake
        d0.update(1, 1.0)
        d0.settle(1.3)
        assert d0.deadline() is None

    # -- phantom detents: a knob nobody touches must never move a value -----------------------
    # 2026-10-12 20:25: Main went -19 -> -20 during a package purge, nobody known at the box, no
    # knob warning in the journal. A resting knob that sees contact noise, half a click, or a
    # glitch taking both contacts at once, and then nothing, must emit nothing: neither on a
    # timer (v0) nor when its contacts come back to the detent one after the other.
    @staticmethod
    def near_detent_walks(spd: int, max_len: int = 6) -> list[list[int]]:
        """Every sequence of single-contact flips from the detent (AB 11) that never strays
        more than half a click from it: contact noise, a brushed knob, half a click."""
        ring, bound, out = [3, 1, 0, 2], spd // 2, []

        def walk(pos: int, seq: list[int]) -> None:
            if seq:
                out.append(seq)
            if len(seq) < max_len:
                for step in (1, -1):
                    if abs(pos + step) <= bound:
                        walk(pos + step, seq + [ring[(pos + step) % 4]])
        walk(0, [])
        return out

    # Both contacts at once (00 first), and LAS-3's shapes whose first sample has ONE contact
    # flipped (what INTCAP holds with interrupts): 01 then 10 in one read, and two dips whose
    # release is simultaneous.
    GLITCHES = ([0], [0, 3], [0, 1, 3], [0, 2, 3], [0, 1, 0, 2, 3], [0, 2, 0, 3],
                [1, 2, 3], [2, 1, 3], [1, 2], [2, 1, 3, 1, 2, 3])
    # Two dips released together: at 4 transitions per click (the box's knobs). At 2, 11 -> 01
    # -> 00 is a whole click, so that shape is a click then a jump of unknown sign there.
    GLITCHES_SPD4 = ([1, 0, 3], [2, 0, 3])
    # Staggered dips AND releases: a valid quadrature cycle, so a glitch only by its speed (a
    # click from stillness in 1.5 / 6 ms). The same cycle over 0.6 s is a click (tested too).
    FAST_GLITCHES = ([1, 0, 2, 3], [2, 0, 1, 3], [1, 0, 2, 3, 1, 0, 2, 3])

    @staticmethod
    def resting(spd: int, moved: bool) -> QuadratureDecoder:
        """The driver's decoder (detent 11, learning on) at rest; with `moved`, after a real
        click 4 s earlier (its direction must not leak into a glitch)."""
        d = QuadratureDecoder(spd, state=3, idle_s=0.3, rest=3, learn_rests=3)
        if moved:
            ring = [3, 1, 0, 2]
            assert sum(d.update(ring[(k + 1) % 4], 1.0 + 0.01 * k) for k in range(4)) == \
                (2 if spd == 2 else 1)
            while d.deadline() is not None:
                d.settle(d.deadline())
        return d

    @pytest.mark.parametrize("spd", [4, 2])
    @pytest.mark.parametrize("moved", [False, True])
    def test_noise_or_half_a_click_then_nothing_emits_nothing(self, spd, moved):
        rng = random.Random(20251012 + spd)
        walks = self.near_detent_walks(spd)
        assert len(walks) > 20
        for seq in walks:
            for gaps in ([0.002] * len(seq), [0.35] * len(seq),           # brisk / hovering
                         [rng.uniform(0.001, 0.9) for _ in seq]):
                d = self.resting(spd, moved)
                t, tl = 5.0, []
                for s, g in zip(seq, gaps):
                    t += g
                    tl.append((round(t, 4), s))
                tl.append((t + 30.0, seq[-1]))             # 30 s of nothing (same level)
                assert self.drive(d, tl) == [], (seq, gaps)
                assert d.anchor == 3 and d.relearned == 0

    @pytest.mark.parametrize("spd", [4, 2])
    @pytest.mark.parametrize("moved", [False, True])
    @pytest.mark.parametrize("gap", [0.0005, 0.002, 0.2])
    def test_both_contact_glitch_on_a_resting_knob_emits_nothing(self, spd, moved, gap):
        # the first cut of patch A: 11 -> 00 -> 01 -> 11 gave -1, 11 -> 00 -> 10 -> 11 gave +1,
        # and 11 -> 00 -> 11 one step in the direction of the last turn
        for seq in self.GLITCHES + (self.GLITCHES_SPD4 if spd == 4 else ()):
            d = self.resting(spd, moved)
            tl = [(5.0 + k * gap, s) for k, s in enumerate(seq)]
            assert self.drive(d, tl + [(40.0, seq[-1])]) == [], seq
            assert d.anchor == 3 and d.relearned == 0
            if seq[-1] == 3:                               # back on the detent: in sync again
                t = 60.0
                ring = [3, 1, 0, 2]
                assert sum(d.update(ring[(k + 1) % 4], t + 0.01 * k) for k in range(4)) == \
                    (2 if spd == 2 else 1), seq

    @pytest.mark.parametrize("spd", [4, 2])
    @pytest.mark.parametrize("moved", [False, True])
    @pytest.mark.parametrize("gap", [0.0005, 0.002])
    def test_staggered_glitch_on_a_resting_knob_emits_nothing(self, spd, moved, gap):
        for seq in self.FAST_GLITCHES:
            d = self.resting(spd, moved)
            tl = [(5.0 + k * gap, s) for k, s in enumerate(seq)]
            assert self.drive(d, tl + [(40.0, seq[-1])]) == [], seq
            assert d.anchor == 3 and d.relearned == 0
            ring = [3, 1, 0, 2]                            # in sync: the next click counts
            assert sum(d.update(ring[(k + 1) % 4], 60.0 + 0.01 * k) for k in range(4)) == \
                (2 if spd == 2 else 1), seq

    def test_a_slow_valid_cycle_is_a_click_whatever_caused_it(self):
        # the limit of LAS-3's fix, said: the contacts alone cannot tell a slow glitch from a
        # click (the master log names every change's source instead)
        d = self.resting(4, moved=False)
        assert [n for _t, n in self.drive(d, [(5.0 + 0.2 * k, s)
                                              for k, s in enumerate([1, 0, 2, 3])])] == [1]

    def test_many_glitches_never_drift_a_resting_knob(self):
        # the symptom: a value creeping one way while nobody touches the box. Fifty glitches
        # whose contacts always come back B first (one line's RC slower), over ten minutes.
        d = self.resting(4, moved=True)
        tl = []
        for k in range(50):
            t = 10.0 + 12.0 * k
            tl += [(t, 0), (t + 0.001, 1), (t + 0.002, 3)]
        assert self.drive(d, tl) == []
        assert d.invalid == 50 and d.acc == 0 and d.anchor == 3

    def test_random_walk_with_bounces_matches_net_detents(self):
        rng = random.Random(1234)
        for spd in (4, 2, 1):
            d = QuadratureDecoder(spd, state=3)
            state, net, emitted, t = 3, 0, 0, 0.0
            ring = CW[:-1]                                    # 3 1 0 2, CW = forward
            for _ in range(400):
                direction = rng.choice((1, -1))
                for _ in range(spd):
                    pos = ring.index(state)
                    nxt = ring[(pos + direction) % 4]
                    if rng.random() < 0.3:                    # contact bounce: x -> y -> x -> y
                        for s in (nxt, state):
                            t += 0.001
                            emitted += d.update(s, t)
                    state = nxt
                    t += 0.004
                    emitted += d.update(state, t)
                net += direction
            assert emitted == net, spd

    def test_bad_steps_per_detent(self):
        with pytest.raises(ValueError):
            QuadratureDecoder(3)


# ---------------------------------------------------------------------------------------------
# Switch debounce / short / long (fake time)
# ---------------------------------------------------------------------------------------------
class TestSwitch:
    def test_clean_short_press(self):
        s = SwitchDebouncer(0.010, 0.8)
        assert s.feed(True, 1.000) == []
        assert s.deadline() == pytest.approx(1.010)
        assert s.tick(1.009) == []
        assert s.tick(1.010) == [PRESS]
        assert s.feed(False, 1.200) == []
        assert s.tick(1.211) == [RELEASE, SHORT]
        assert s.deadline() is None

    def test_bouncy_press_is_one_press(self):
        s = SwitchDebouncer(0.010, 0.8)
        out = []
        for k, lv in enumerate([True, False, True, False, True]):
            out += s.feed(lv, 1.0 + k * 0.002)
        assert out == []
        assert s.tick(1.008 + 0.0099) == []
        assert s.tick(1.018) == [PRESS]
        assert s.press_ts == pytest.approx(1.008)

    def test_glitch_shorter_than_debounce_is_ignored(self):
        s = SwitchDebouncer(0.010, 0.8)
        assert s.feed(True, 1.000) == []
        assert s.feed(False, 1.004) == []
        assert s.tick(1.100) == []
        assert s.deadline() is None

    def test_long_fires_while_held_then_release_without_short(self):
        s = SwitchDebouncer(0.010, 0.8)
        s.feed(True, 1.0)
        assert s.tick(1.010) == [PRESS]
        assert s.deadline() == pytest.approx(1.8)
        assert s.tick(1.799) == []
        assert s.tick(1.800) == [LONG]
        assert s.tick(3.0) == []
        s.feed(False, 3.0)
        assert s.tick(3.011) == [RELEASE]

    def test_just_under_long_is_short(self):
        s = SwitchDebouncer(0.010, 0.8)
        s.feed(True, 1.0)
        s.tick(1.010)
        assert s.tick(1.799) == []
        s.feed(False, 1.799)
        assert s.tick(1.809) == [RELEASE, SHORT]

    def test_held_at_start_is_suppressed_until_released(self):
        s = SwitchDebouncer(0.010, 0.8, pressed=True, now=0.0)
        assert s.tick(5.0) == []                 # no long for a stuck/held button
        s.feed(False, 5.0)
        assert s.tick(5.02) == []                # its release is swallowed too
        s.feed(True, 6.0)
        assert s.tick(6.02) == [PRESS]           # then it works normally

    def test_abort_releases_only_a_reported_press(self):
        s = SwitchDebouncer(0.010, 0.8)
        assert s.abort(False, 1.0) == []         # nothing reported, nothing to release
        s.feed(True, 1.0)
        assert s.tick(1.010) == [PRESS]
        assert s.abort(True, 1.1) == [RELEASE]   # paired; never SHORT
        assert s.tick(5.0) == []                 # still held: suppressed, no LONG
        assert s.abort(True, 5.1) == []          # suppressed press was never reported
        s.feed(False, 6.0)
        assert s.tick(6.02) == []
        mid = SwitchDebouncer(0.010, 0.8)
        mid.feed(True, 1.0)                      # still debouncing: no press reported yet
        assert mid.abort(False, 1.005) == []


# ---------------------------------------------------------------------------------------------
# Acceleration
# ---------------------------------------------------------------------------------------------
def test_accel_multiplier_curve():
    assert accel_multiplier(0) == 1
    assert accel_multiplier(12) == 1
    assert accel_multiplier(26) == 2
    assert accel_multiplier(39.9) == 3
    assert accel_multiplier(40) == 4
    assert accel_multiplier(500) == 4
    assert accel_multiplier(100, max_mult=1) == 1


def test_accelerator_scales_fast_turns_and_resets_on_reverse_or_pause():
    a = Accelerator(window_s=0.25)
    assert a.scale(EncoderEvent(0, 1, 1.000)) == 1          # first event: no history
    assert a.scale(EncoderEvent(0, 1, 1.020)) == 4          # 50 det/s
    assert a.scale(EncoderEvent(0, 1, 1.100)) == 1          # 12.5 det/s
    assert a.scale(EncoderEvent(0, -1, 1.110)) == -1        # reversal -> fine
    assert a.scale(EncoderEvent(0, -1, 2.000)) == -1        # pause -> fine
    assert a.scale(EncoderEvent(1, 1, 2.001)) == 1          # per-encoder history
    assert a.scale(EncoderEvent(0, -3, 2.060)) == -12       # coalesced 3 detents in 60 ms = 50/s


# ---------------------------------------------------------------------------------------------
# The default bus: smbus2, never the GPL-2.0-only smbus (D-008)
# ---------------------------------------------------------------------------------------------
def test_default_bus_is_smbus2_and_the_driver_uses_only_its_api(monkeypatch):
    """smbus2's SMBus (MIT) opened on the given bus number. smbus2 signatures, keyword `force`
    included: the driver passes positional (addr, reg[, value|length]) only, as both modules take."""
    import types

    opened: list[int] = []

    class FakeSMBus2:
        def __init__(self, bus=None, force=False):
            opened.append(bus)
            self.chip = FakeChip()

        def read_byte_data(self, i2c_addr, register, force=None):
            return self.chip.read_byte_data(i2c_addr, register)

        def write_byte_data(self, i2c_addr, register, value, force=None):
            self.chip.write_byte_data(i2c_addr, register, value)

        def read_i2c_block_data(self, i2c_addr, register, length, force=None):
            assert length <= 32       # smbus2 refuses more (I2C_SMBUS_BLOCK_MAX)
            return self.chip.read_i2c_block_data(i2c_addr, register, length)

        def close(self):
            opened.append(-1)

    monkeypatch.setitem(sys.modules, "smbus2", types.SimpleNamespace(SMBus=FakeSMBus2))
    monkeypatch.setitem(sys.modules, "smbus", None)          # importing it would raise
    c = ZynthianControls(bus=1, use_irq=False, lines_factory=lambda chip, offs: None)
    c.open()
    try:
        assert opened == [1] and c.healthy
        assert c._chip.verify() == []
        c._chip.read_guarded()
    finally:
        c.close()
    assert opened == [1, -1]
    assert "import smbus\n" not in pathlib.Path(m.__file__).read_text()


# ---------------------------------------------------------------------------------------------
# Register configuration
# ---------------------------------------------------------------------------------------------
def test_open_configures_every_register_from_defaults():
    chip = FakeChip()
    c = make(chip)
    c.open()
    for reg, want in EXPECTED_CONFIG.items():
        assert chip.r[reg] == want, hex(reg)
    assert chip.r[R["DEFVALA"]] == chip.r[R["DEFVALB"]] == 0
    assert c.healthy and c.irq
    assert Mcp23017(chip).verify() == []
    # interrupts are armed only after the comparison mode is set
    regs = [r for r, _ in chip.writes]
    assert regs.index(R["INTCONA"]) < len(regs) - 1 - regs[::-1].index(R["GPINTENA"])


def test_open_recovers_a_chip_left_in_bank1_mode():
    chip = FakeChip(bank1=True)
    chip.r[R["GPINTENA"]] = 0x0F
    chip.r[R["IOCON"]] = 0x80 | 0x40 | 0x02                    # BANK, MIRROR, INTPOL
    c = make(chip)
    c.open()
    assert chip.writes[0] == (0x05, 0x00)                      # BANK=1 IOCON address first
    assert chip.bank == 0
    assert Mcp23017(chip).verify() == []


def test_verify_names_the_registers_that_drifted():
    chip = FakeChip()
    mc = Mcp23017(chip)
    mc.configure()
    chip.power_on_reset()
    bad = mc.verify()
    assert any(b.startswith("GPINTENA=0x00") for b in bad)
    assert any(b.startswith("GPPUB=0x00") for b in bad)


def test_open_raises_controls_error_when_chip_absent():
    chip = FakeChip()
    chip.fail = 10**6
    with pytest.raises(m.ControlsError):
        make(chip).open()


def test_open_degrades_to_polling_when_int_lines_busy():
    chip = FakeChip()

    def busy(chip_path, offsets):
        raise OSError(16, "Device or resource busy")
    c = make(chip, lines_factory=busy)
    c.open()
    assert not c.irq and c.poll_s == pytest.approx(ZynthianControls.NO_IRQ_POLL_S)
    assert c.int_levels() == {}


def test_config_validation():
    with pytest.raises(ValueError):
        ZynthianControls(encoders=[("GPA0", "GPA1")], switches=["GPA0"])
    with pytest.raises(ValueError):
        ZynthianControls(steps_per_detent=3)
    with pytest.raises(ValueError):
        pin_index("GPC1")
    assert pin_index("gpb7") == 15 and pin_index(3) == 3
    c = ZynthianControls(long_ms=10, debounce_ms=0, poll_ms=0)
    assert c.long_s == 0.2 and c.debounce_s == 0.001 and c.poll_s == 0.005


# ---------------------------------------------------------------------------------------------
# Driver processing with the fake chip (no thread, fake clock)
# ---------------------------------------------------------------------------------------------
def test_service_decodes_a_turn_and_clears_int():
    chip, clk = FakeChip(), Clock()
    c = make(chip, clk)
    c.open()
    ext = chip.ext
    events = []
    for s in CW[1:]:
        ext = with_enc(ext, 1, s)
        chip.set_ext(ext)
        assert chip.int_level(0) == 0          # enc1 is on port A
        clk.advance(0.004)
        c._service()
        assert chip.int_level(0) == 1
        events += drain(c)
    assert [(e.index, e.delta) for e in events] == [(1, 1)]


def test_intcap_recovers_a_transition_that_happened_before_the_read():
    chip, clk = FakeChip(), Clock()
    c = make(chip, clk)
    c.open()
    ext = with_enc(chip.ext, 0, 1)
    chip.set_ext(ext)                       # 3 -> 1 latched in INTCAPA
    chip.set_ext(with_enc(ext, 0, 0))       # 1 -> 0 before we read: GPIO only shows 0
    c._service()
    assert c.stats["invalid_transitions"] == 0
    assert c._decoders[0].acc == 2


def test_stale_intcap_of_the_quiet_port_is_ignored():
    chip, clk = FakeChip(), Clock()
    c = make(chip, clk)
    c.open()
    chip.r[R["INTCAPB"]] = 0x00             # stale capture, all port-B pins "low"
    for s in CW[1:]:
        chip.set_ext(with_enc(chip.ext, 0, s))
        clk.advance(0.004)
        c._service()
    evs = drain(c)
    assert [(type(e).__name__, e.index) for e in evs] == [("EncoderEvent", 0)]


def test_switch_press_short_and_long_through_the_driver():
    chip, clk = FakeChip(), Clock()
    c = make(chip, clk)
    c.open()
    chip.set_ext(with_sw(chip.ext, 5, True))
    c._service()
    assert drain(c) == []                                   # not yet stable
    clk.advance(0.011)
    c._service()
    assert [(e.index, e.kind) for e in drain(c)] == [(5, PRESS)]
    clk.advance(0.8)
    c._service()
    assert [(e.index, e.kind) for e in drain(c)] == [(5, LONG)]
    chip.set_ext(with_sw(chip.ext, 5, False))
    c._service()
    clk.advance(0.011)
    c._service()
    assert [(e.index, e.kind) for e in drain(c)] == [(5, RELEASE)]
    # a short press on a port-B button
    chip.set_ext(with_sw(chip.ext, 7, True))
    c._service(); clk.advance(0.02); c._service()
    chip.set_ext(with_sw(chip.ext, 7, False))
    c._service(); clk.advance(0.02); c._service()
    assert [(e.index, e.kind) for e in drain(c)] == [(7, PRESS), (7, RELEASE), (7, SHORT)]


def test_switch_held_at_boot_fires_nothing():
    chip, clk = FakeChip(), Clock()
    chip.ext = with_sw(chip.ext, 4, True)
    c = make(chip, clk)
    c.open()
    clk.advance(2.0); c._service()
    chip.set_ext(with_sw(chip.ext, 4, False))
    c._service(); clk.advance(0.02); c._service()
    assert drain(c) == []


def test_box_idle_levels_seed_decoders_without_events():
    chip, clk = FakeChip(ext=0xEFF9), Clock()   # the levels read on the box (morning 2026-10-12)
    c = make(chip, clk)
    c.open()
    assert [d.anchor for d in c._decoders] == [0b11] * 4       # detents: both contacts open
    assert [d.state for d in c._decoders] == [0b00, 0b11, 0b11, 0b10]
    assert drain(c) == []
    ext = chip.ext
    for s in [2, 3, 1, 0]:                      # CW from 00: 00 -> 10 -> 11 -> 01 -> 00
        ext = with_enc(ext, 0, s)
        chip.set_ext(ext)
        clk.advance(0.004)
        c._service()
    assert [(e.index, e.delta) for e in drain(c)] == [(0, 1)]


@pytest.mark.parametrize("back_first", ["B", "A", "both"])
def test_driver_glitch_on_every_line_of_a_resting_box_emits_nothing(back_first):
    """A 2 ms disturbance that pulls every input low (ground bounce, a spark, a cable touched),
    released one contact after the other, on a box at rest whose E1 last turned 4 s ago: no
    knob event and no button event, then 30 s of resync polls with nothing either; the next
    real click counts exactly."""
    chip, clk = FakeChip(), Clock()
    c = make(chip, clk)
    c.open()
    ext = chip.ext
    for s in CW[1:]:                              # a real click on enc0 (E1 = MASTER)
        ext = with_enc(ext, 0, s)
        chip.set_ext(ext)
        clk.advance(0.004)
        c._service()
    assert [(e.index, e.delta) for e in drain(c)] == [(0, 1)]
    clk.advance(4.0)
    c._service()
    rest = chip.ext
    half = 0x0000
    for i in range(4):                            # one contact of every encoder back first
        half = with_enc(half, i, {"B": 0b01, "A": 0b10, "both": 0b00}[back_first])
    for levels in (0x0000, half, rest):
        chip.set_ext(levels)
        clk.advance(0.001)
        c._service()
    for _ in range(600):
        clk.advance(0.05)
        c._service()
    assert drain(c) == []
    for s in CW[1:]:
        chip.set_ext(with_enc(chip.ext, 0, s))
        clk.advance(0.004)
        c._service()
    assert [(e.index, e.delta) for e in drain(c)] == [(0, 1)]


@pytest.mark.parametrize("first, then", [(0b01, 0b10), (0b10, 0b01), (0b01, 0b00), (0b10, 0b00)])
@pytest.mark.parametrize("turned_before", [False, True])
def test_driver_glitch_whose_first_edge_is_one_contact_emits_nothing(first, then, turned_before):
    """LAS-3: with interrupts INTCAP holds a glitch's FIRST edge, which is usually one contact.
    INTCAP 01 then GPIO 10 in the same read (and the same timestamp) once completed a click as
    a "recent move" + an inferred skip. Every encoder, a resting box, then 30 s of polls."""
    chip, clk = FakeChip(), Clock()
    c = make(chip, clk)
    c.open()
    if turned_before:
        for s in CW[1:]:
            chip.set_ext(with_enc(chip.ext, 0, s))
            clk.advance(0.004)
            c._service()
        assert [(e.index, e.delta) for e in drain(c)] == [(0, 1)]
    clk.advance(50.0)
    c._service()
    rest = chip.ext
    one, two = rest, rest
    for i in range(4):
        one, two = with_enc(one, i, first), with_enc(two, i, then)
    chip.set_ext(one)                             # INT asserted, INTCAP = the first edge
    chip.set_ext(two)                             # before the read: GPIO = the second state
    clk.advance(0.0005)
    c._service()
    chip.set_ext(rest)
    clk.advance(0.0005)
    c._service()
    for _ in range(600):
        clk.advance(0.05)
        c._service()
    assert drain(c) == []
    if then == 0b00:              # two dips released together: a whole click in 1 ms, dropped
        assert c.stats["glitches"] == 4                                # (and counted, logged)
    for s in CW[1:]:                              # in sync: the next real click counts
        chip.set_ext(with_enc(chip.ext, 0, s))
        clk.advance(0.01)
        c._service()
    assert [(e.index, e.delta) for e in drain(c)] == [(0, 1)]


def test_per_encoder_steps_and_detents():
    chip, clk = FakeChip(ext=0xEFF9), Clock()
    c = make(chip, clk, steps_per_detent=[4, 2, 4, 1], detent_ab=["11", "11", "idle", 0b00])
    c.open()
    assert [d.spd for d in c._decoders] == [4, 2, 4, 1]
    assert [d.anchor for d in c._decoders] == [0b11, 0b11, 0b11, 0b00]   # enc2 idle = its level
    ext = chip.ext
    for s in [1, 0]:                            # enc1 (spd 2): one click 11 -> 01 -> 00
        ext = with_enc(ext, 1, s)
        chip.set_ext(ext)
        clk.advance(0.004)
        c._service()
    assert [(e.index, e.delta) for e in drain(c)] == [(1, 1)]


def test_encoder_options_validation_and_yaml():
    with pytest.raises(ValueError):
        ZynthianControls(steps_per_detent=[4, 4, 4])
    with pytest.raises(ValueError):
        ZynthianControls(steps_per_detent=[4, 4, 3, 4])
    with pytest.raises(ValueError):
        ZynthianControls(detent_ab="12")
    assert parse_detent("10") == 0b10 and parse_detent("idle") is None and parse_detent(2) == 2
    assert controls_options({"controls": {"steps_per_detent": [4, 2, 4, 4],
                                          "detent_ab": "11", "encoders": [0, 1, 2, 3]}}) == \
        {"steps_per_detent": [4, 2, 4, 4], "detent_ab": "11"}
    # a typo keeps the default for that key only (never no knobs)
    assert controls_options({"controls": {"steps_per_detent": 3, "detent_ab": ["00"] * 4}}) == \
        {"detent_ab": ["00"] * 4}
    assert controls_options({}) == {} and controls_options(None) == {}
    assert controls_options({"controls": "nonsense"}) == {}


def test_service_opens_the_knobs_with_the_yaml_options(monkeypatch):
    from k1000 import __main__ as main_mod
    seen = []

    class Spy:
        def __init__(self, **kw):
            seen.append(kw)

        def start(self):
            pass

    monkeypatch.setattr(m, "ZynthianControls", Spy)
    assert main_mod.open_controls({"controls": {"steps_per_detent": [4, 2, 4, 4],
                                                "detent_ab": "bogus"}}) is not None
    assert seen == [{"steps_per_detent": [4, 2, 4, 4]}]
    assert main_mod.open_controls({}) is not None and seen[-1] == {}


def test_learned_detent_and_half_rest_hint_are_logged_once(caplog):
    chip, clk = FakeChip(), Clock()
    c = make(chip, clk)
    c.open()
    ext = chip.ext
    ring = [3, 1, 0, 2]
    state = 3
    with caplog.at_level("WARNING", logger="k1000.hw.mcp23017"):
        for _ in range(12):                     # a 2-transition encoder at the default spd 4
            for _ in range(2):
                state = ring[(ring.index(state) + 1) % 4]
                ext = with_enc(ext, 0, state)
                chip.set_ext(ext)
                clk.advance(0.02)
                c._service()
            clk.advance(0.35)
            c._service()
    hints = [r for r in caplog.records if "steps_per_detent 2" in r.getMessage()]
    assert len(hints) == 1 and "enc0" in hints[0].getMessage()


def test_int_held_low_does_not_spin_the_service_loop():
    chip, clk = FakeChip(), Clock()
    c = make(chip, clk)
    c.open()
    c.force_interrupt("A")
    assert chip.int_level(0) == 0
    before = chip.transactions
    assert c._service() is True                 # reports the line as stuck
    assert chip.transactions - before == ZynthianControls.MAX_REREADS
    assert c.stats["int_stuck"] == 1 and chip.int_level(0) == 0
    c.release_interrupt("A")
    assert c._service() is False
    assert chip.int_level(0) == 1
    assert drain(c) == []                       # forcing never produces knob events


# ---------------------------------------------------------------------------------------------
# Config guard: an expander reset or a corrupted config must never fake a press or a LONG
# (B2 long = mute toggle; v0 also had B4 long = reset live EQ). Review finding 1 (major) + finding 2.
# ---------------------------------------------------------------------------------------------
ALL_SW_LOW = 0xFFFF & ~sum(1 << p for p in SW)


def test_por_with_floating_low_inputs_is_repaired_before_any_event():
    chip, clk = FakeChip(), Clock()
    c = make(chip, clk)
    c.open()
    chip.power_on_reset()                       # the expander alone resets: pull-ups + GPINTEN off
    chip.set_ext(ALL_SW_LOW)                    # ...and the floating switch inputs read low
    clk.advance(0.05)
    c._service()                                # the next resync poll
    assert c.stats["pullup_alarms"] == 1 and c.stats["reconfigs"] == 1
    assert Mcp23017(chip).verify() == []
    for _ in range(40):                         # 2 s still reading low: nothing, no LONG
        clk.advance(0.05)
        c._service()
    assert drain(c) == []
    chip.set_ext(0xFFFF)                        # pull-ups back: that "release" is swallowed too
    c._service(); clk.advance(0.02); c._service()
    assert drain(c) == []
    chip.set_ext(with_sw(0xFFFF, 6, True))      # and a real press works again
    c._service(); clk.advance(0.02); c._service()
    assert [(e.index, e.kind) for e in drain(c)] == [(6, PRESS)]


def test_thread_por_with_floating_low_inputs_never_fires_press_or_long():
    """The reviewer's probe with the real worker and default timings (poll 50 ms, long 800 ms)."""
    chip = FakeChip()

    async def scenario():
        c = make(chip)
        c.start()
        await asyncio.sleep(0.3)
        chip.power_on_reset()
        chip.set_ext(ALL_SW_LOW)
        got = []
        t_end = time.monotonic() + 1.5
        while (ev := await c.next_event(max(0.0, t_end - time.monotonic()))) is not None:
            got.append(ev)
        stats = dict(c.stats)
        c.close()
        return got, stats
    got, stats = asyncio.run(scenario())
    assert got == []
    assert stats["pullup_alarms"] >= 1 and stats["reconfigs"] >= 1


def test_thread_config_corruption_is_reseeded_before_a_long_can_fire():
    """A corruption the per-read pull-up check cannot see (IPOLA) while switch pins read low:
    the periodic verify re-seeds before long_ms, so a press it faked ends in RELEASE, never LONG."""
    chip = FakeChip()

    async def scenario():
        c = make(chip)                          # default timings
        c.start()
        await asyncio.sleep(0.1)
        with chip.lock:
            chip.r[R["IPOLA"]] = 0x01
        chip.set_ext(with_sw(with_sw(0xFFFF, 4, True), 5, True))
        got = []
        t_end = time.monotonic() + 1.5
        while (ev := await c.next_event(max(0.0, t_end - time.monotonic()))) is not None:
            got.append((ev.index, ev.kind))
        stats = dict(c.stats)
        c.close()
        return got, stats
    got, stats = asyncio.run(scenario())
    assert stats["reconfigs"] >= 1
    for i in (4, 5):                            # verify may land before or after the debounce
        assert [k for j, k in got if j == i] in ([], [PRESS, RELEASE]), got
    assert not any(k in (LONG, SHORT) for _, k in got), got


@pytest.mark.parametrize("long_ms", [200, 800, 5000])
def test_verify_period_always_beats_a_long_press(long_ms):
    c = ZynthianControls(long_ms=long_ms, debounce_ms=50)
    assert c._verify_period() < c.long_s - c.debounce_s
    assert c._verify_period() <= ZynthianControls.VERIFY_S


def test_a_single_corrupted_read_is_dropped_without_reconfiguring():
    chip, clk = FakeChip(), Clock()
    c = make(chip, clk)
    c.open()
    real = chip.read_i2c_block_data

    def corrupt_once(addr, reg, length):
        chip.read_i2c_block_data = real
        out = real(addr, reg, length)
        out[0] = 0x7F                           # a bit error in GPPUA ...
        out[6] = 0x00                           # ... and GPIOA all low (4 switches "pressed")
        return out
    chip.read_i2c_block_data = corrupt_once
    c._service()
    assert c.stats["pullup_alarms"] == 1 and c.stats["bad_reads"] == 1
    assert c.stats["reconfigs"] == 0 and c.levels == 0xFFFF   # sample never decoded
    clk.advance(1.0)
    c._service()
    assert drain(c) == []


def test_reseed_after_an_i2c_fault_releases_a_reported_press():
    chip, clk = FakeChip(), Clock()
    c = make(chip, clk)
    c.open()
    chip.set_ext(with_sw(chip.ext, 1, True))    # hold enc1's push (fine mode in input_map)
    c._service(); clk.advance(0.011); c._service()
    assert [(e.index, e.kind) for e in drain(c)] == [(1, PRESS)]
    c._recover()                                # what the worker does after I2C errors
    assert [(e.index, e.kind) for e in drain(c)] == [(1, RELEASE)]   # paired, no SHORT
    clk.advance(2.0); c._service()
    assert drain(c) == []                       # still held: suppressed, no LONG
    chip.set_ext(with_sw(chip.ext, 1, False))
    c._service(); clk.advance(0.011); c._service()
    assert drain(c) == []                       # its release was already reported
    chip.set_ext(with_sw(chip.ext, 1, True))
    c._service(); clk.advance(0.011); c._service()
    assert [(e.index, e.kind) for e in drain(c)] == [(1, PRESS)]


def test_reseed_after_config_loss_releases_a_reported_press_even_after_long():
    chip, clk = FakeChip(), Clock()
    c = make(chip, clk)
    c.open()
    chip.set_ext(with_sw(chip.ext, 0, True))
    c._service(); clk.advance(0.011); c._service(); clk.advance(0.8); c._service()
    assert [(e.index, e.kind) for e in drain(c)] == [(0, PRESS), (0, LONG)]
    chip.power_on_reset()
    clk.advance(0.05); c._service()
    assert c.stats["reconfigs"] == 1
    assert [(e.index, e.kind) for e in drain(c)] == [(0, RELEASE)]
    chip.set_ext(with_sw(chip.ext, 0, False))
    c._service(); clk.advance(0.011); c._service()
    assert drain(c) == []


# ---------------------------------------------------------------------------------------------
# Consumer API safety (review findings 3 and 5)
# ---------------------------------------------------------------------------------------------
def test_next_event_timeout_keeps_the_stream_alive():
    chip = FakeChip()

    async def scenario():
        c = make(chip, poll_ms=20)
        c.start()
        assert await c.next_event(0.05) is None          # a screen-refresh tick
        with pytest.raises(TimeoutError):                # cancelled from outside: still safe
            await asyncio.wait_for(c.next_event(), 0.05)
        await _turn(chip, 0, CW[1:])
        ev = await c.next_event(1.0)
        assert (ev.index, ev.delta) == (0, 1)
        # the documented generator caveat: a cancelled __anext__ ends THAT generator, but no
        # event is lost and a fresh events() resumes the same queue
        agen = c.events()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(agen.__anext__(), 0.05)
        await _turn(chip, 1, CW[1:])
        agen2 = c.events()
        ev = await asyncio.wait_for(agen2.__anext__(), 1.0)
        assert (ev.index, ev.delta) == (1, 1)
        await agen2.aclose()
        c.close()
        with pytest.raises(m.ControlsClosed):            # closed: no silent None busy-loop
            await c.next_event(0.1)
        assert [ev async for ev in c.events()] == []     # and events() just ends
    asyncio.run(scenario())


def test_queue_overflow_cannot_interleave_with_a_merge():
    """The race, made deterministic: the worker overflows the full queue (drops the oldest)
    exactly between the consumer's head check and its popleft. With _q_lock the worker has to
    wait; without it the consumer would add knob 1's delta to knob 0 (or crash on a switch)."""
    c = make(FakeChip(), max_pending=4)

    class Interleaving(collections.deque):
        reads = 0
        worker: threading.Thread

        def __getitem__(self, i):
            v = super().__getitem__(i)
            self.reads += 1
            if self.reads == 2:                  # both head checks done, popleft not yet
                self.worker = threading.Thread(
                    target=lambda: [c._emit(SwitchEvent(7, PRESS, 9.0)) for _ in range(2)])
                self.worker.start()
                self.worker.join(0.2)            # blocked by _q_lock (fixed) or done (racy)
            return v
    q = Interleaving([EncoderEvent(0, 1000, 1.0), EncoderEvent(0, 1000, 2.0),
                      EncoderEvent(1, 1, 3.0), EncoderEvent(1, 1, 4.0)])
    c._pending = q
    ev = c._pop_merged()
    q.worker.join(1.0)
    assert ev == EncoderEvent(0, 2000, 2.0)
    assert list(c._pending) == [EncoderEvent(1, 1, 3.0), EncoderEvent(1, 1, 4.0),
                                SwitchEvent(7, PRESS, 9.0), SwitchEvent(7, PRESS, 9.0)]


def test_events_merges_queued_steps_of_the_same_knob():
    chip = FakeChip()
    c = make(chip)
    c.open()
    c._started = True                           # no worker thread: we fill the queue by hand
    for ev in [EncoderEvent(0, 1, 1.0), EncoderEvent(0, 1, 1.1), EncoderEvent(1, 1, 1.2),
               EncoderEvent(0, -1, 1.3), EncoderEvent(0, 1, 1.4), SwitchEvent(2, PRESS, 1.5),
               EncoderEvent(3, 2, 1.6)]:
        c._pending.append(ev)
    c._closed = True

    async def collect():
        return [ev async for ev in c.events()]
    out = asyncio.run(collect())
    assert out == [EncoderEvent(0, 2, 1.1), EncoderEvent(1, 1, 1.2), SwitchEvent(2, PRESS, 1.5),
                   EncoderEvent(3, 2, 1.6)]     # the -1/+1 pair of enc0 cancels out


# ---------------------------------------------------------------------------------------------
# Worker thread + asyncio, end to end with the fakes (real time, short)
# ---------------------------------------------------------------------------------------------
async def _next(agen, timeout=1.0):
    return await asyncio.wait_for(agen.__anext__(), timeout)


async def _turn(chip: FakeChip, i: int, states, dt=0.004):
    ext = chip.ext
    for s in states:
        ext = with_enc(ext, i, s)
        chip.set_ext(ext)
        await asyncio.sleep(dt)


def test_thread_end_to_end_turn_press_long():
    chip = FakeChip()

    async def scenario():
        c = make(chip, poll_ms=20, long_ms=200)
        c.start()
        agen = c.events()
        await _turn(chip, 2, CW[1:])
        ev = await _next(agen)
        assert (ev.index, ev.delta) == (2, 1)
        await _turn(chip, 3, list(reversed(CW))[1:])
        ev = await _next(agen)
        assert (ev.index, ev.delta) == (3, -1)
        chip.set_ext(with_sw(chip.ext, 1, True))
        assert (await _next(agen)).kind == PRESS
        t0 = time.monotonic()
        assert (await _next(agen)).kind == LONG
        assert 0.15 < time.monotonic() - t0 < 0.4
        chip.set_ext(with_sw(chip.ext, 1, False))
        assert (await _next(agen)).kind == RELEASE
        assert c.stats["edges_a"] > 0 and c.stats["edges_b"] > 0
        c.close()
        assert c._lines is None
    asyncio.run(scenario())


def test_thread_missed_edge_is_recovered_by_the_poll():
    chip = FakeChip()

    async def scenario():
        c = make(chip, poll_ms=20)
        c.start()
        agen = c.events()
        chip.lines.drop = 1                    # the falling edge is lost: INTA stays low
        chip.set_ext(with_enc(chip.ext, 0, 1))
        assert chip.int_level(0) == 0
        await asyncio.sleep(0.08)
        assert chip.int_level(0) == 1          # the resync poll read the port and cleared it
        await _turn(chip, 0, CW[2:])           # and edges work again afterwards
        ev = await _next(agen)
        assert (ev.index, ev.delta) == (0, 1)
        assert c.stats["polls"] >= 1
        c.close()
    asyncio.run(scenario())


def test_thread_survives_i2c_errors_and_chip_reset():
    chip = FakeChip()

    async def scenario():
        c = make(chip, poll_ms=20)
        c.VERIFY_S = 0.1
        c.start()
        agen = c.events()
        chip.fail = 3                          # a burst of NACKs
        await asyncio.sleep(0.6)
        assert c.stats["i2c_errors"] >= 1 and c.stats["recoveries"] >= 1 and c.healthy
        chip.power_on_reset()                  # brown-out: interrupts and pull-ups gone
        await asyncio.sleep(0.3)
        assert c.stats["reconfigs"] >= 1
        assert Mcp23017(chip).verify() == []
        await _turn(chip, 1, CW[1:])
        ev = await _next(agen)
        assert (ev.index, ev.delta) == (1, 1)
        c.close()
    asyncio.run(scenario())


def test_thread_interrupt_storm_degrades_to_polling_without_spinning():
    chip = FakeChip()

    async def scenario():
        c = make(chip, poll_ms=50)
        c.start()
        agen = c.events()
        await asyncio.sleep(0.05)
        reads0 = c.stats["reads"]
        c.force_interrupt("A")                  # the fake re-asserts (new edge) on every read
        await asyncio.sleep(0.3)
        reads = c.stats["reads"] - reads0
        assert c.stats["storms"] >= 1
        assert reads < 2 * 0.3 / c.STORM_POLL_S, reads      # ~60 expected; spinning is >5000
        await _turn(chip, 2, CW[1:], dt=0.015)  # knobs still decode during the storm
        ev = await _next(agen)
        assert (ev.index, ev.delta) == (2, 1)
        c.release_interrupt("A")
        await asyncio.sleep(0.2)
        assert chip.int_level(0) == 1
        r0 = c.stats["reads"]
        await asyncio.sleep(0.2)
        assert c.stats["reads"] - r0 <= 6       # back to edge mode + 50 ms polls
        await _turn(chip, 0, CW[1:])
        ev = await _next(agen)
        assert (ev.index, ev.delta) == (0, 1)
        c.close()
    asyncio.run(scenario())


class _BusyKnobChip(FakeChip):
    """A knob turned so fast that it moves right after EVERY service read (the clearing GPIO
    read): INT is low again at each re-check, with a new level each time. Each read takes
    `step_s` (the knob's transition period: 3 ms = 83 clicks/s), on the test's clock or for
    real (a sleep, GIL released like smbus2's ioctl)."""

    def __init__(self, enc: int, states, clock=None, step_s: float = 0.003, **kw) -> None:
        super().__init__(**kw)
        self.enc, self.script, self.armed = enc, list(states), False
        self.clock, self.step_s = clock, step_s

    def read_i2c_block_data(self, addr: int, reg: int, length: int) -> list[int]:
        out = super().read_i2c_block_data(addr, reg, length)
        if self.armed and reg == m.GPPUA and self.script:     # a service read (not a verify)
            if self.clock is not None:
                self.clock.advance(self.step_s)
            else:
                time.sleep(self.step_s)
            with self.lock:
                self.set_ext(with_enc(self.ext, self.enc, self.script.pop(0)))
        return out


def test_a_fast_turn_re_asserting_int_with_new_levels_is_busy_not_stuck():
    """PERF-2: INT low again after MAX_REREADS reads that EACH brought a new level is a fast
    knob, not a stuck line: no storm polling (its 5 ms passes left 20-35 ms gaps mid-spin on
    the box, losing whole clicks), and the clicks count exactly."""
    ring = [1, 0, 2, 3]
    clk = Clock()
    chip = _BusyKnobChip(0, ring * 6, clock=clk)
    c = make(chip, clk)
    c.open()
    chip.armed = True
    chip.set_ext(with_enc(chip.ext, 0, chip.script.pop(0)))   # the first transition: INT falls
    results = []
    for _ in range(20):
        clk.advance(0.001)
        results.append(c._service())
    assert not any(results) and c.stats["int_stuck"] == 0 and c.stats["int_busy"] >= 5
    assert sum(e.delta for e in drain(c) if isinstance(e, EncoderEvent)) == 6
    # a line that stays low with NO new level is still stuck (a wedged INT, a corrupted INTCON)
    c.force_interrupt("A")
    clk.advance(0.001)
    assert c._service() is True and c.stats["int_stuck"] == 1
    # ... even while a knob on the OTHER port moves on every read
    chip.enc, chip.script = 2, ring * 2
    chip.set_ext(with_enc(chip.ext, 2, chip.script.pop(0)))
    clk.advance(0.001)
    assert c._service() is True
    c.release_interrupt("A")


class _WedgedLines(FakeLines):
    """INTA wedged low (a hardware fault): no edge ever again, whatever the chip does."""

    def fall(self, port: int) -> None:
        if port == 1:
            super().fall(port)

    def levels(self) -> dict[int, int]:
        return {**super().levels(), self.map[0]: 0}


def test_a_wedged_int_line_is_stuck_even_while_a_knob_on_its_port_turns():
    """PERF-2's busy rule needs a FRESH edge: a line wedged low makes none, so a knob turning
    on its port must not keep the driver out of storm polling (5 ms) on a 50 ms poll."""
    ring = [1, 0, 2, 3]
    clk = Clock()
    chip = _BusyKnobChip(0, ring * 4, clock=clk)
    c = make(chip, clk, lines_factory=lambda chipname, offs: _WedgedLines(chip))
    c.open()
    c._lines.read_events()
    chip.armed = True
    chip.set_ext(with_enc(chip.ext, 0, chip.script.pop(0)))
    clk.advance(0.001)
    assert c._service() is True and c.stats["int_stuck"] == 1 and c.stats["int_busy"] == 0


def test_thread_fast_turn_never_enters_storm_polling():
    ring = [1, 0, 2, 3]
    chip = _BusyKnobChip(1, ring * 10)

    async def scenario():
        c = make(chip, poll_ms=50)
        c.start()
        agen = c.events()
        await asyncio.sleep(0.05)
        chip.armed = True
        with chip.lock:
            chip.set_ext(with_enc(chip.ext, 1, chip.script.pop(0)))
        total = 0
        while total < 10:
            ev = await _next(agen)
            if isinstance(ev, EncoderEvent):
                total += ev.delta
        await asyncio.sleep(0.1)
        assert total == 10 and c.stats["storms"] == 0 and c.stats["int_stuck"] == 0, c.stats
        c.close()
    asyncio.run(scenario())


def test_self_test_logic_on_the_fake():
    chip = FakeChip()

    async def scenario():
        c = make(chip, poll_ms=20)
        c.start()
        res = await m.self_test(c)
        c.close()
        return res
    res = asyncio.run(scenario())
    assert res["A"]["ok"] and res["B"]["ok"], res
    assert res["A"]["storm_at_release"] and res["B"]["storm_at_release"]   # storm poll clears it
    assert Mcp23017(chip).verify() == []


def test_cancelled_self_test_releases_the_chip():
    """Leaving the HWTEST page mid-test must not leave the chip forced and the verify paused."""
    chip = FakeChip()

    async def scenario():
        c = make(chip, poll_ms=50)
        c.VERIFY_S = 0.1
        c.start()
        task = asyncio.create_task(m.self_test(c))
        await asyncio.sleep(2 * c.poll_s + 0.1)          # inside port A's 200 ms forced hold
        assert chip.r[R["INTCONA"]] == 0x01
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert chip.r[R["INTCONA"]] == 0x00 and c._verify_paused_until == 0.0
        await asyncio.sleep(0.4)
        r0 = c.stats["reads"]
        await asyncio.sleep(0.5)
        reads = c.stats["reads"] - r0
        c.close()
        return reads
    reads = asyncio.run(scenario())
    assert reads <= 0.5 / 0.05 + 5, reads                # idle polling (~10), not storm (~100)
    assert Mcp23017(chip).verify() == [] and chip.int_level(0) == 1


def test_forced_interrupt_without_release_is_repaired_once_the_pause_expires():
    chip = FakeChip()

    async def scenario():
        c = make(chip, poll_ms=50)
        c.VERIFY_S = 0.05
        c.start()
        c.force_interrupt("A", max_hold_s=0.2)            # caller vanished: no release_interrupt()
        await asyncio.sleep(0.1)
        assert chip.r[R["INTCONA"]] == 0x01 and c.stats["reconfigs"] == 0   # pause honoured
        await asyncio.sleep(0.4)
        stats = dict(c.stats)
        c.close()
        return stats
    stats = asyncio.run(scenario())
    assert stats["reconfigs"] >= 1
    assert chip.r[R["INTCONA"]] == 0x00 and chip.int_level(0) == 1


def test_monitor_cli_end_to_end_on_the_fake(monkeypatch, capsys):
    """What Thomas will run: physical pin names, burst sizes, short/long, a clean summary."""
    chip = FakeChip(ext=0xEFF9)                 # today's idle levels
    real = m.ZynthianControls

    def fake_controls(*a, **kw):
        kw.update(bus_factory=lambda n: chip, lines_factory=lambda c, o: FakeLines(chip))
        return real(*a, **kw)
    monkeypatch.setattr(m, "ZynthianControls", fake_controls)

    def actor():
        time.sleep(0.3)
        ext = chip.ext
        for s in CW[1:]:                        # enc1 one click CW from its 11 rest
            ext = with_enc(ext, 1, s)
            chip.set_ext(ext)
            time.sleep(0.005)
        time.sleep(0.5)                         # let the burst line close
        chip.set_ext(with_sw(chip.ext, 4, True)); time.sleep(0.1)
        chip.set_ext(with_sw(chip.ext, 4, False)); time.sleep(0.1)
        chip.set_ext(with_sw(chip.ext, 5, True)); time.sleep(0.9)
        chip.set_ext(with_sw(chip.ext, 5, False))
    th = threading.Thread(target=actor, daemon=True)
    th.start()
    assert m.main(["--monitor", "--duration", "2.6"]) == 0
    th.join()
    out = capsys.readouterr().out
    assert "enc0 [A=GPA2 B=GPA1]  rest AB=00" in out
    assert "enc1 [A=GPA5 B=GPA4]  +1  (total +1)" in out
    assert "enc1 [A=GPA5 B=GPA4] burst: +4 quarter-steps, AB 11 -> 11" in out
    assert re.search(r"sw4 \[GPA6\] \(button 1\?\)  short  \(\d+ ms\)", out)
    # durations count from the press *event* (after the 10 ms debounce): a long shows ~790 ms
    assert re.search(r"sw5 \[GPA7\] \(button 2\?\)  long  \([78]\d\d ms\)", out)
    summary = out.strip().splitlines()[-1]
    assert "encoder_events=1 switch_events=6" in summary and "i2c_errors=0" in summary


def test_cli_without_mode_prints_help():
    assert m.main([]) == 2


def test_labels_name_physical_pins():
    assert m.encoder_label(0, ENC[0]) == "enc0 [A=GPA2 B=GPA1]"
    assert m.switch_label(4, SW[4]) == "sw4 [GPA6] (button 1?)"
    assert m.switch_label(2, SW[2]) == "sw2 [GPB0] (push of enc2?)"


# ---------------------------------------------------------------------------------------------
# Hardware (opt-in: python3 -m pytest -m hw). Owns the MCP23017 and GPIO4/GPIO27 while it runs.
# ---------------------------------------------------------------------------------------------
BOX = pathlib.Path(__file__).resolve().parents[1]


@pytest.mark.hw
def test_hw_init_config_and_idle_state():
    c = ZynthianControls()
    c.open()
    try:
        assert c._chip.verify() == []
        lv = c.levels
        print(f"\nidle GPIOA=0x{lv & 0xFF:02x} GPIOB=0x{lv >> 8:02x}; encoder AB: "
              + ", ".join(f"enc{i}={((lv >> a) & 1)}{((lv >> b) & 1)}" for i, (a, b) in enumerate(ENC)))
        assert all((lv >> p) & 1 for p in SW), "a switch reads pressed at idle"
        assert c.int_levels() == {27: 1, 4: 1}
        # cost of the config guard: the service read is 8 bytes (GPPUA..GPIOB) instead of 6
        timings = {}
        for name, fn in (("read_state_6B", c._chip.read_state), ("read_guarded_8B", c._chip.read_guarded)):
            t0 = time.perf_counter()
            for _ in range(200):
                r = fn()
            timings[name] = round((time.perf_counter() - t0) / 200 * 1000, 3)
        t0 = time.perf_counter()
        c._chip.configure()                      # what a repair costs (16 writes + 1 read)
        timings["configure"] = round((time.perf_counter() - t0) * 1000, 3)
        gppu, _ = c._chip.read_guarded()
        print("ms:", timings, f"GPPU=0x{gppu:04x}")
        assert gppu == 0xFFFF
        assert timings["read_guarded_8B"] < 2.0
    finally:
        c.close()


@pytest.mark.hw
def test_hw_forced_interrupts_wake_the_worker_and_get_cleared():
    async def scenario():
        c = ZynthianControls()
        c.start()
        try:
            res = await m.self_test(c)
            stray = []
            while (ev := await c.next_event(0.2)) is not None:
                stray.append(ev)
            return res, stray, c._chip.verify(), dict(c.stats)
        finally:
            c.close()
    res, stray, bad, stats = asyncio.run(scenario())
    print("\nself_test:", res, "\nstats:", stats)
    assert res["A"]["ok"] and res["A"]["gpio"] == 27, res
    assert res["B"]["ok"] and res["B"]["gpio"] == 4, res
    # honest about what clears it here: forcing holds INT low -> storm polling (5 ms)
    assert res["A"]["storm_at_release"] and res["B"]["storm_at_release"], res
    assert stray == []
    assert bad == []                            # chip left in the driver's configuration


class DroppingLines:
    """Real INT lines that swallow the next `drop` edge events as if the kernel had missed them.
    wait() keeps waiting out its timeout after a swallowed edge, so nothing but the worker's
    poll_ms resync poll can clear the line (the self-test cannot prove that path: storm mode)."""

    def __init__(self, inner) -> None:
        self.inner, self.drop, self.dropped = inner, 0, 0

    def wait(self, timeout: float) -> bool:
        end = time.monotonic() + timeout
        while True:
            if not self.inner.wait(max(0.0, end - time.monotonic())):
                return False
            if self.drop <= 0:
                return True
            self.inner.read_events()
            self.drop -= 1
            self.dropped += 1

    def read_events(self):
        return self.inner.read_events()

    def levels(self):
        return self.inner.levels()

    def close(self) -> None:
        self.inner.close()


@pytest.mark.hw
@pytest.mark.parametrize("poll_ms", [50, 200])
def test_hw_missed_edge_is_recovered_by_the_resync_poll(poll_ms):
    """Force INT low and release it at once (INTF stays latched, INT stays low, no further edge)
    with that edge swallowed. Only the resync poll can clear it: clear_ms must track poll_ms,
    not the 5 ms storm poll or an edge wake."""
    holder = {}

    def lines_factory(chip, offsets):
        holder["l"] = DroppingLines(m.GpiodIntLines(chip, offsets))
        return holder["l"]

    async def scenario():
        c = ZynthianControls(poll_ms=poll_ms, lines_factory=lines_factory)
        c.start()
        lines, out = holder["l"], []
        try:
            for port, gpio in (("A", 27), ("B", 4)) * 2:
                await asyncio.sleep(0.15)
                r0 = c.stats["reads"]
                while c.stats["reads"] == r0:            # phase-lock: a poll read just happened
                    await asyncio.sleep(0.0005)
                # let the worker finish that pass (it checks INT right after the read and would
                # re-read a line it sees low) and get back into wait()
                await asyncio.sleep(0.005)
                edges0 = c.stats["edges_a"] + c.stats["edges_b"]
                storms0, dropped0 = c.stats["storms"], lines.dropped
                lines.drop = 1
                t0 = time.monotonic()
                c.force_interrupt(port)                  # INT falls; that edge is swallowed
                c.release_interrupt(port)                # latched: INT stays low until a read
                low = c.int_levels()[gpio] == 0
                while c.int_levels()[gpio] == 0 and time.monotonic() - t0 < 1.0:
                    await asyncio.sleep(0.0005)
                out.append(dict(port=port, low_after_release=low,
                                clear_ms=round((time.monotonic() - t0) * 1000, 1),
                                dropped=lines.dropped - dropped0,
                                edges=c.stats["edges_a"] + c.stats["edges_b"] - edges0,
                                storms=c.stats["storms"] - storms0))
                lines.drop = 0
            return out, c._chip.verify(), dict(c.stats)
        finally:
            c.close()
    out, bad, stats = asyncio.run(scenario())
    print(f"\npoll_ms={poll_ms}:", out, "\nstats:", stats)
    for r in out:
        assert r["low_after_release"] and r["dropped"] == 1, r
        assert r["edges"] == 0 and r["storms"] == 0, r
        assert 0.4 * poll_ms <= r["clear_ms"] <= poll_ms + 25, r
    assert bad == []


@pytest.mark.hw
def test_hw_pullup_loss_is_repaired_on_the_next_read_without_events():
    """The expander-only reset the config guard exists for, emulated from a second I2C handle:
    GPINTEN and GPPU back to their power-on 0x00 under the running driver (default timings).
    The next service read must see it and repair it (well before long_ms) with no event."""
    import smbus2

    async def scenario():
        c = ZynthianControls()
        c.start()
        bus = smbus2.SMBus(1)
        detect_ms, repair_ms = [], []
        try:
            await asyncio.sleep(0.3)
            for _ in range(5):
                al0, rc0, r0 = c.stats["pullup_alarms"], c.stats["reconfigs"], c.stats["reads"]
                while c.stats["reads"] == r0:            # just after a read: worst case
                    await asyncio.sleep(0.0005)
                await asyncio.sleep(0.005)               # that pass (and any verify) is over
                t0 = time.monotonic()
                bus.write_i2c_block_data(0x20, m.GPINTENA, [0x00, 0x00])   # interrupts off
                bus.write_i2c_block_data(0x20, m.GPPUA, [0x00, 0x00])      # pull-ups off
                while c.stats["pullup_alarms"] == al0 and time.monotonic() - t0 < 1.0:
                    await asyncio.sleep(0.0005)
                detect_ms.append(round((time.monotonic() - t0) * 1000, 1))
                while c.stats["reconfigs"] == rc0 and time.monotonic() - t0 < 1.0:
                    await asyncio.sleep(0.0005)
                repair_ms.append(round((time.monotonic() - t0) * 1000, 1))
                await asyncio.sleep(0.2)
            evs = []
            while (ev := await c.next_event(1.0)) is not None:   # everything queued since start
                evs.append(ev)
            return detect_ms, repair_ms, evs, dict(c.stats), c._chip.verify()
        finally:
            bus.close()
            c.close()
    detect_ms, repair_ms, evs, stats, bad = asyncio.run(scenario())
    print("\ndetect_ms:", detect_ms, "\nrepair_ms:", repair_ms, "\nstats:", stats)
    # detected by a service read (only _config_alarm counts pullup_alarms; the periodic verify
    # does not), at the next poll (~55 ms measured; <= 2 polls allowed for a loaded box); the
    # repair adds verify + configure() (~6 ms); the bad sample is never decoded
    assert all(ms <= 2 * 50 for ms in detect_ms), detect_ms
    assert all(r - d <= 50 for d, r in zip(detect_ms, repair_ms)), (detect_ms, repair_ms)
    # reconfigs >= : a verify landing between the two writes repairs GPINTEN first (harmless)
    assert stats["reconfigs"] >= 5 and stats["pullup_alarms"] == 5, stats
    assert evs == []
    assert bad == []


@pytest.mark.hw
def test_hw_monitor_idle_5s_no_spurious_events_low_cpu():
    p = subprocess.run([sys.executable, "-m", "k1000.hw.mcp23017", "--monitor", "--duration", "5"],
                       cwd=BOX, capture_output=True, text=True, timeout=30)
    print("\n" + p.stdout + p.stderr)
    assert p.returncode == 0
    summary = p.stdout.strip().splitlines()[-1]
    assert "encoder_events=0 switch_events=0" in summary
    assert "quarter-steps," not in p.stdout          # no raw encoder movement at all
    cpu = float(re.search(r"cpu=([\d.]+)%", summary).group(1))
    assert cpu < 5.0
