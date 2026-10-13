# SPDX-License-Identifier: GPL-3.0-or-later
"""Zynthian V4 knobs and buttons: MCP23017 on I2C + two interrupt lines (gpiod v2).

Hardware (proven on the box, see docs/03-v0-architecture.md section 1):
  MCP23017 on I2C bus 1 at 0x20, internal pull-ups on all 16 pins, INTA -> BCM GPIO27,
  INTB -> BCM GPIO4, active-low. Encoders (A, B): GPA2/GPA1, GPA5/GPA4, GPB2/GPB1, GPB5/GPB4.
  Switches (active-low): GPA0, GPA3, GPB0, GPB3 (encoder pushes), GPA6, GPA7, GPB6, GPB7.
  Same map as Zynthian's own V4 config: zynthian-sys config/zynthian_envars_V4.sh
  (ENCODER_A=102,105,110,113 ENCODER_B=101,104,109,112 SWITCHES=100,103,108,111,106,107,114,115,
  base pin 100; INTA/INTB wiringPi 2/7 = BCM 27/4).

Decoding reference: zynthian/zyncoder, branch `oram` (commit cbd93aa1), zyncoder.c
`update_zyncoder()` + `valid_quadrant_states[]`: it keeps a 2-transition history and emits +/-1
only on the last transition of a complete quadrature cycle back into the rest state
(long_history 0xd4 = CW, 0xe8 = CCW), i.e. **4 transitions per detent**, CW = A changes first
(older zyncoder: ZYNCODER_TICKS_PER_RETENT 4). That is our default (`steps_per_detent=4`); 2 and
1 are configurable per encoder (k1000.yaml `controls: steps_per_detent`), and
box/tools/k1000-knobtrace.py measures which one the knobs need.

Detent (rest) state: like zyncoder, a detent is "both contacts open" (AB=11) by default, per
encoder in k1000.yaml `controls: detent_ab` (docs/08-knob-precision.md). The v0 driver anchored
each decoder on the level read at start instead (the box once idled at enc0 AB=00, enc1 11, enc2
11, enc3 10; all four read 11 after use on 2026-10-12) and re-anchored after any 0.3 s pause off
that state: a finger pausing at the top of a click then made the click count twice, a very slow
turn counted late. Now a pause never counts by itself; a mis-set detent re-learns itself only
after three long (>= 1 s) rests in a row on the same other state (QuadratureDecoder).

Interrupt storms (proven on the box): while a pin is in compare-to-DEFVAL mode and mismatched, the
real chip re-asserts INT right after every clearing read (2 fresh falling edges per INTF..GPIO block
read). Any persistent condition (corrupted INTCON, a chattering contact) would therefore turn the
edge -> read loop into a CPU spin; the worker detects it and degrades to 5 ms polling instead.

Config guard (audio safety): B4 long = mute toggle on every page (UI v1, D-009/D-013; the other
LONG that changes audio is a B3 hold on SHOW, band IN/OUT, D-014, and only once the button map is
confirmed; the live-EQ reset is a confirmed SETUP item), so a chip
that lost its configuration must never be able to fake a long press. If the expander alone resets
(a VDD glitch shorter than the gap between reads gives no I2C error), its pull-ups turn off and
the switch inputs float, possibly low. So every service read starts at GPPUA (8 bytes instead of 6,
about +0.2 ms): a sample taken without pull-ups is never ingested, the chip is re-verified and
re-configured, and every switch is re-seeded (held ones suppressed until released). Any other
corruption is caught by the full config verify every VERIFY_S (0.25 s), kept below
long_ms - debounce_ms, so a re-seed always lands before a fake LONG could fire.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import AsyncIterator, Callable, Iterable, Optional, Protocol, Sequence, Union

log = logging.getLogger("k1000.hw.mcp23017")

# ---------------------------------------------------------------------------------------------
# Register map (IOCON.BANK = 0, the power-on default and what configure() enforces)
# ---------------------------------------------------------------------------------------------
IODIRA, IODIRB = 0x00, 0x01
IPOLA, IPOLB = 0x02, 0x03
GPINTENA, GPINTENB = 0x04, 0x05
DEFVALA, DEFVALB = 0x06, 0x07
INTCONA, INTCONB = 0x08, 0x09
IOCON = 0x0A
GPPUA, GPPUB = 0x0C, 0x0D
INTFA, INTFB = 0x0E, 0x0F
INTCAPA, INTCAPB = 0x10, 0x11
GPIOA, GPIOB = 0x12, 0x13

# IOCON = 0: BANK=0 (A/B interleaved, so one sequential read covers INTF..GPIO of both ports),
# MIRROR=0 (separate INTA/INTB: the edge tells us which port moved, and the contract wiring has
# both lines), SEQOP=0 (address auto-increment, needed for the block read), ODR=0 (push-pull),
# INTPOL=0 (active-low, as proven on the box). Zynthian uses INTPOL=1 + rising edges; either works,
# active-low matches the idle-high pull-ups we request on the Pi side.
IOCON_VALUE = 0x00
# In BANK=1 mode IOCON lives at 0x05 (in BANK=0 that address is GPINTENB, which we set anyway).
IOCON_BANK1_ADDR = 0x05

EXPECTED_CONFIG = {
    IODIRA: 0xFF, IODIRB: 0xFF,      # all inputs
    IPOLA: 0x00, IPOLB: 0x00,        # no polarity inversion
    GPINTENA: 0xFF, GPINTENB: 0xFF,  # interrupt-on-change on all 16 pins
    INTCONA: 0x00, INTCONB: 0x00,    # compare to previous value (not DEFVAL)
    IOCON: IOCON_VALUE,
    GPPUA: 0xFF, GPPUB: 0xFF,        # 100k pull-ups: switches and encoder contacts close to GND
}
_REG_NAMES = {
    IODIRA: "IODIRA", IODIRB: "IODIRB", IPOLA: "IPOLA", IPOLB: "IPOLB",
    GPINTENA: "GPINTENA", GPINTENB: "GPINTENB", DEFVALA: "DEFVALA", DEFVALB: "DEFVALB",
    INTCONA: "INTCONA", INTCONB: "INTCONB", IOCON: "IOCON", 0x0B: "IOCON(B)",
    GPPUA: "GPPUA", GPPUB: "GPPUB",
}

PIN_NAMES: tuple[str, ...] = tuple([f"GPA{i}" for i in range(8)] + [f"GPB{i}" for i in range(8)])

# Contract defaults (03-v0-architecture.md section 1).
DEFAULT_ENCODERS: tuple[tuple[str, str], ...] = (
    ("GPA2", "GPA1"), ("GPA5", "GPA4"), ("GPB2", "GPB1"), ("GPB5", "GPB4"))
DEFAULT_SWITCHES: tuple[str, ...] = (
    "GPA0", "GPA3", "GPB0", "GPB3", "GPA6", "GPA7", "GPB6", "GPB7")

PinRef = Union[int, str]


def pin_index(pin: PinRef) -> int:
    """'GPA0'..'GPB7' or 0..15 -> 0..15 (bit index in the 16-bit B<<8|A level word)."""
    if isinstance(pin, str):
        name = pin.strip().upper()
        if name not in PIN_NAMES:
            raise ValueError(f"unknown MCP23017 pin {pin!r} (want GPA0..GPA7, GPB0..GPB7)")
        return PIN_NAMES.index(name)
    if not 0 <= int(pin) <= 15:
        raise ValueError(f"MCP23017 pin {pin} out of range 0..15")
    return int(pin)


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


# ---------------------------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------------------------
PRESS, RELEASE, SHORT, LONG = "press", "release", "short", "long"


@dataclass(frozen=True)
class EncoderEvent:
    """`delta` detents (positive = clockwise per zyncoder's convention; swap A/B to reverse)."""
    index: int
    delta: int
    ts: float


@dataclass(frozen=True)
class SwitchEvent:
    """kind: press | release | short (released before long_ms) | long (still held at long_ms)."""
    index: int
    kind: str
    ts: float = 0.0


Event = Union[EncoderEvent, SwitchEvent]


# ---------------------------------------------------------------------------------------------
# Quadrature decoding (pure, unit-tested)
# ---------------------------------------------------------------------------------------------
_INV = 2  # marker: both bits changed between samples (a skipped state, direction unknown)
# A click that starts from stillness and whose transitions come faster than this (on average,
# leave -> arrival) is a contact glitch, never a hand: 2.5 ms per transition = 100 clicks/s at
# 4 transitions per click; the fastest spins measured are ~45 clicks/s, and a first click from
# stillness is slower still. QuadratureDecoder._arrive.
GLITCH_STEP_S = 0.0025
# index = prev_state << 2 | cur_state, state = A << 1 | B (1 = high).
# CW (+1) cycle from the open/open rest: 11 -> 01 -> 00 -> 10 -> 11 (A leads, as in zyncoder).
_QSTEP = (0, -1, +1, _INV,
          +1, 0, _INV, -1,
          -1, _INV, 0, +1,
          _INV, +1, -1, 0)


class QuadratureDecoder:
    """Full-quadrature state-table decoder for one encoder.

    Every valid transition adds +/-1 to an accumulator; a skipped state (both bits flipped) adds
    +/-2 in the direction we were already going, if the knob really moved within `infer_s`, so
    a fast spin that outruns the I2C sampling still counts. Detents are emitted the moment the
    encoder arrives in a rest state, rounded (half away from zero) to `steps_per_detent`, so
    contact bounce (x -> y -> x) nets to zero and never emits.

    "Really moved" means the knob was already turning in that direction BEFORE this excursion
    off the detent: a counted transition within `infer_s` before it left the rest state (a
    spin: the previous click had just landed), or two counted steps that way since it left
    (|acc| >= 2). The excursion's own first transition is not evidence: with interrupts, INTCAP
    holds the first edge of anything, a glitch included, so a glitch whose first sample has one
    contact flipped (11 -> 01 -> 10 -> 11, INTCAP 01 then GPIO 10 in one read) must not lend
    its own edge to the skip that follows. Cost: the first click of a turn from stillness whose
    first or second transition is skipped is lost (never more than that click; the next one
    leaves the detent while moving).

    A skip without such a move is not attributed: on a knob at rest it is a glitch that takes
    both contacts at once. Its true size is +2 or -2, so the decoder is *lost* by half a click
    until the knob is back on a rest state; that arrival counts the smaller of the two readings
    (it never invents a detent) and re-syncs. Without this, a 2 ms glitch on a resting knob
    emitted a detent: 11 -> 00 -> 01 -> 11 gave -1, 11 -> 00 -> 10 -> 11 gave +1, 11 -> 00 -> 11
    gave one step in the direction of the knob's last turn (the glitch itself counted as
    "recent"), and 11 -> 01 -> 10 -> 11 gave +1 (its first edge counted as a recent move).

    Rest states: spd=4 -> {anchor}; spd=2 -> {anchor, anchor^3}; spd=1 -> every state. The anchor
    is `rest`, the AB state of a detent (a property of the encoder: 3 = both contacts open), or
    with rest=None the state at reset().

    A pause never counts by itself. A knob still for `idle_s` off a rest state is a finger between
    two clicks: nothing is emitted, except from 3/4 of the way (spd 4: a lost last transition,
    or a worn contact reading one quarter-step off at its detent), and then the remainder is kept
    so that click is not counted again when the knob moves on. With `learn_rests` the anchor
    moves to a state the knob has rested on for `learn_s` that many times in a row, with no stop
    on a rest state between: a mis-set `rest` heals itself; a finger hovering between clicks,
    which stops on a detent in between, never moves it.
    """

    def __init__(self, steps_per_detent: int = 4, state: int = 3, now: float = 0.0,
                 idle_s: float = 0.3, infer_s: float = 0.1, rest: Optional[int] = None,
                 learn_rests: int = 0, learn_s: float = 1.0,
                 glitch_s: float = GLITCH_STEP_S) -> None:
        if steps_per_detent not in (1, 2, 4):
            raise ValueError("steps_per_detent must be 1, 2 or 4")
        self.spd = steps_per_detent
        self.idle_s = idle_s
        self.infer_s = infer_s
        self.rest = None if rest is None else rest & 3
        self.learn_rests = max(0, int(learn_rests))
        self.learn_s = max(idle_s, learn_s)
        self.glitch_s = max(0.0, float(glitch_s))
        self.invalid = 0     # skipped states we could not attribute a direction to
        self.glitches = 0    # clicks from stillness too fast for a hand: not counted
        self.relearned = 0   # anchor moves by learn_rests
        self.half_rests = 0  # rests in a row half a detent apart (a 2-transition encoder at spd 4?)
        self.reset(state, now)

    def reset(self, state: int, now: float = 0.0) -> None:
        """Adopt `state` as the current position without emitting anything. The anchor is `rest`
        (configured or learned), else `state`."""
        self.state = state & 3
        self.anchor = self.state if self.rest is None else self.rest
        self.acc = 0
        self.last_change = now
        self.last_dir = 0
        self._settled = True              # nothing to decide until the knob moves
        self._vote = False                # a learn vote is due at last_change + learn_s
        self._cand: Optional[int] = None  # learn: the state voted for, and how many times
        self._cand_n = 0
        self._moved = 0                   # net quarter-steps since the last stop (half_rests)
        self._last_stop = self.state
        self._lost = False                # an unattributable skip since the last rest (+-2 unknown)
        self._moved_at = now              # time of the last real (counted) transition
        self._entry_dir = 0               # direction the knob was ALREADY turning when it left
        #                                   the last rest state (0: it left from stillness)
        self._still = False               # it left the last rest state from stillness
        self._left_at = now               # when it left the last rest state

    def is_rest(self, state: int) -> bool:
        if self.spd == 1:
            return True
        if self.spd == 2:
            return state in (self.anchor, self.anchor ^ 3)
        return state == self.anchor

    def update(self, state: int, now: float = 0.0) -> int:
        """Feed one sample (A << 1 | B). Returns the detents completed by it (usually 0)."""
        state &= 3
        if state == self.state:
            return 0
        prev = self.state
        step = _QSTEP[(prev << 2) | state]
        recent = bool(self.last_dir) and now - self._moved_at <= self.infer_s
        self.last_change = now
        self.state = state
        self._settled, self._vote = False, False
        if step == _INV:
            d = self._skip_dir(prev, recent)
            if d:
                step = 2 * d
            else:
                self.invalid += 1
                self._entry_dir = 0   # nothing about this excursion is a known turn any more
                if self.is_rest(prev):            # it left the detent from stillness
                    self._still, self._left_at = True, now
                if self.spd == 1:     # every state is emitted at once: nothing to put right
                    return 0
                if self.is_rest(state):
                    if self._lost:    # a second jump of unknown sign: nothing to trust, re-sync
                        self.acc, self._lost = 0, False
                        return 0
                    if self.spd == 4:     # from half a click off back onto a detent: +-2
                        self._lost = True
                        return self._arrive(now)
                self._lost = True
                return 0
        sign = 1 if step > 0 else -1
        if self.is_rest(prev):
            # Leaving a detent: was the knob already turning this way (a spin), or is this the
            # first edge of something from stillness (a turn starting, or a glitch)?
            self._entry_dir = sign if (recent and self.last_dir == sign) else 0
            self._still, self._left_at = not recent, now
        self.acc += step
        self._moved += step
        self.last_dir = sign
        self._moved_at = now
        if self.is_rest(state):
            return self._arrive(now)
        return 0

    def _arrive(self, now: float) -> int:
        """On a rest state: the detents completed (see _flush), unless the click started from
        stillness and came faster than a hand turns (`glitch_s` per transition: a contact glitch
        whose dips and releases are staggered, e.g. 11 -> 01 -> 00 -> 10 -> 11 within 2 ms, is a
        valid quadrature cycle; only its speed tells it from a click). Then nothing counts, and
        the next excursion is from stillness too."""
        n = self._flush()
        if (n and self._still and self.spd > 1
                and now - self._left_at < (self.spd - 1) * self.glitch_s):
            self.glitches += 1
            self.last_dir = 0
            return 0
        return n

    def _skip_dir(self, prev: int, recent: bool) -> int:
        """The direction to credit a skipped state (both contacts changed) to, 0 = none.
        Only a knob already turning that way before this excursion off the detent: see the
        class docstring. The excursion's own first edge never counts as that evidence."""
        if not recent:
            return 0
        if self.is_rest(prev):              # the skip leaves the detent: a spin, or nothing
            return self.last_dir
        if abs(self.acc) >= 2:              # two counted steps that way since it left
            return 1 if self.acc > 0 else -1
        d = self._entry_dir
        if d and (self.acc == 0 or (self.acc > 0) == (d > 0)):
            return d
        return 0

    def _flush(self) -> int:
        acc, self.acc = self.acc, 0
        if self._lost:
            # +-2 transitions unaccounted for: the reading closer to zero, rounded down
            if abs(acc) < 2:          # spd 2: back on the detent the jump landed on, still lost
                return 0
            self._lost = False
            n = (abs(acc) - 2) // self.spd
        else:
            n = (abs(acc) + self.spd // 2) // self.spd
        return n if acc > 0 else -n

    def deadline(self) -> Optional[float]:
        """When settle() has work to do (None = nothing pending)."""
        if self.spd == 1:
            return None
        if not self._settled:
            return self.last_change + self.idle_s
        if self._vote:
            return self.last_change + self.learn_s
        return None

    def settle(self, now: float) -> int:
        """Call at deadline(): the knob has been still. Returns detents to emit (rarely any)."""
        n = 0
        if not self._settled:
            if now < self.last_change + self.idle_s:
                return 0
            self._settled = True
            n = self._stopped()
            self._vote = bool(self.learn_rests) and not self.is_rest(self.state)
        if self._vote and now >= self.last_change + self.learn_s:
            self._vote = False
            self._learn(self.state)
        return n

    def _stopped(self) -> int:
        n = 0
        if self.is_rest(self.state):
            self._cand, self._cand_n = None, 0      # a stop on a known detent confirms the anchor
        elif not self._lost:                        # lost: 3/4 might be 1/4; wait for a rest
            mag = (abs(self.acc) + self.spd // 4) // self.spd   # spd 4: only from 3/4
            n = mag if self.acc > 0 else -mag
            self.acc -= n * self.spd
        # A 2-transition encoder decoded at spd 4 stops alternately on the anchor and anchor^3,
        # two transitions apart. A finger pausing at the top of every click looks the same, so
        # this only feeds a log hint (ZynthianControls), never the decoding.
        if self.spd == 4 and abs(self._moved) == 2 and self.state == self._last_stop ^ 3:
            self.half_rests += 1
        elif self._moved:
            self.half_rests = 0
        self._last_stop, self._moved = self.state, 0
        return n

    def _learn(self, state: int) -> None:
        if state == self._cand:
            self._cand_n += 1
        else:
            self._cand, self._cand_n = state, 1
        if self._cand_n >= self.learn_rests:
            self.anchor = state
            if self.rest is not None:
                self.rest = state                  # survives reset() (I2C fault re-seeds)
            self.acc = 0                           # this click was counted on the old anchor
            self._lost = False
            self._cand, self._cand_n = None, 0
            self.relearned += 1


# ---------------------------------------------------------------------------------------------
# Switch debouncing + short/long classification (pure, unit-tested)
# ---------------------------------------------------------------------------------------------
class SwitchDebouncer:
    """Stable-for-`debounce_s` debouncer with press/release/short/long classification.

    Trailing-edge (the level must hold for debounce_s) rather than leading-edge: the inputs use the
    MCP23017's weak 100k pull-ups, so a single EMI glitch must never become a press. `long` fires
    while the switch is still held (at press + long_s) so a long-press action is felt without
    letting go; the release that follows emits `release` but no `short`.
    A switch already down when the driver starts (or re-seeds after an I2C fault) is suppressed
    until it has been released: a stuck or held button can never fire an action at boot.
    """

    def __init__(self, debounce_s: float = 0.010, long_s: float = 0.8,
                 pressed: bool = False, now: float = 0.0) -> None:
        self.debounce_s = debounce_s
        self.long_s = long_s
        self.reset(pressed, now)

    def reset(self, pressed: bool, now: float = 0.0) -> None:
        """Adopt `pressed` silently. A switch adopted as pressed is suppressed until released."""
        self.raw = self.stable = bool(pressed)
        self.raw_since = now
        self.press_ts = now
        self.long_sent = bool(pressed)
        self.suppressed = bool(pressed)

    def abort(self, pressed: bool, now: float = 0.0) -> list[str]:
        """Re-seed after a fault (I2C error, chip reset): returns [RELEASE] if a press had been
        reported, then reset(). WHY: consumers pair press/release (encoder-push fine mode), so a
        reported press must always get its release; never SHORT or LONG, since the hold was cut by
        the fault, not by the hand. A switch still held afterwards stays suppressed until released."""
        out = [RELEASE] if self.stable and not self.suppressed else []
        self.reset(pressed, now)
        return out

    def feed(self, pressed: bool, now: float) -> list[str]:
        """A raw sample (True = contact closed). Returns the events it completes."""
        pressed = bool(pressed)
        if pressed != self.raw:
            self.raw = pressed
            self.raw_since = now
        return self.tick(now)

    def tick(self, now: float) -> list[str]:
        out: list[str] = []
        if self.raw != self.stable and now - self.raw_since >= self.debounce_s:
            self.stable = self.raw
            if self.stable:
                self.press_ts = self.raw_since
                self.long_sent = False
                self.suppressed = False
                out.append(PRESS)
            else:
                if not self.suppressed:
                    out.append(RELEASE)
                    if not self.long_sent:
                        out.append(SHORT)
                self.suppressed = False
        if self.stable and not self.long_sent and now - self.press_ts >= self.long_s:
            self.long_sent = True
            out.append(LONG)
        return out

    def deadline(self) -> Optional[float]:
        if self.raw != self.stable:
            return self.raw_since + self.debounce_s
        if self.stable and not self.long_sent:
            return self.press_ts + self.long_s
        return None


# ---------------------------------------------------------------------------------------------
# Acceleration helper (separate on purpose: the controller decides which fields accelerate)
# ---------------------------------------------------------------------------------------------
def accel_multiplier(rate_dps: float, low: float = 12.0, high: float = 40.0, max_mult: int = 4) -> int:
    """Detents/second -> integer step multiplier: 1 up to `low`, `max_mult` from `high`, linear
    in between. zyncoder accelerates only below 40 ms/detent (25 det/s) up to x4-5; we start a
    little earlier because a live engineer spins a master knob slower than a synth player."""
    max_mult = max(1, int(max_mult))
    if rate_dps <= low or high <= low:
        return 1 if rate_dps <= low else max_mult
    if rate_dps >= high:
        return max_mult
    return 1 + int((max_mult - 1) * (rate_dps - low) / (high - low))


class Accelerator:
    """Scales EncoderEvent deltas by turn speed, per encoder. A direction change or a pause longer
    than `window_s` resets to x1, so reversing to correct an overshoot is always fine-grained."""

    def __init__(self, window_s: float = 0.25, low: float = 12.0, high: float = 40.0,
                 max_mult: int = 4) -> None:
        self.window_s, self.low, self.high, self.max_mult = window_s, low, high, max_mult
        self._last: dict[int, tuple[float, int]] = {}

    def scale(self, ev: EncoderEvent) -> int:
        sign = 1 if ev.delta > 0 else -1
        last = self._last.get(ev.index)
        self._last[ev.index] = (ev.ts, sign)
        if ev.delta == 0 or last is None or last[1] != sign or ev.ts - last[0] > self.window_s:
            return ev.delta
        rate = abs(ev.delta) / max(ev.ts - last[0], 0.002)
        return ev.delta * accel_multiplier(rate, self.low, self.high, self.max_mult)


# ---------------------------------------------------------------------------------------------
# Encoder options (k1000.yaml `controls:`), per physical encoder enc0..enc3
# ---------------------------------------------------------------------------------------------
DetentRef = Union[int, str, None]
DEFAULT_DETENT = "11"    # both contacts open (zyncoder's rest; all four knobs read it 2026-10-12)
HALF_REST_HINT = 8       # QuadratureDecoder.half_rests that earns one log line per encoder


def parse_detent(v: DetentRef) -> Optional[int]:
    """'11' / '01' (A then B, 1 = open) or 0..3 -> AB state; 'idle' or None -> None (the level at
    start, the v0 behaviour). Raises ValueError otherwise."""
    if v is None:
        return None
    if isinstance(v, int) and not isinstance(v, bool):
        if 0 <= v <= 3:
            return v
        raise ValueError(f"detent_ab {v!r}: want '11', '10', '01', '00' or 'idle'")
    t = str(v).strip().lower()
    if t == "idle":
        return None
    if len(t) == 2 and set(t) <= {"0", "1"}:
        return int(t[0]) << 1 | int(t[1])
    raise ValueError(f"detent_ab {v!r}: want '11', '10', '01', '00' or 'idle'")


def _per_encoder(v: object, n: int, name: str) -> list:
    if isinstance(v, (list, tuple)):
        if len(v) != n:
            raise ValueError(f"{name}: {n} values expected, got {len(v)}")
        return list(v)
    return [v] * n


def steps_per_detent_list(v: object, n: int) -> tuple[int, ...]:
    """1, 2 or 4, or one of those per encoder -> n values. Raises ValueError."""
    vals = _per_encoder(v, n, "steps_per_detent")
    if any(isinstance(x, bool) or x not in (1, 2, 4) for x in vals):
        raise ValueError("steps_per_detent must be 1, 2 or 4")
    return tuple(int(x) for x in vals)


def detent_list(v: object, n: int) -> tuple[Optional[int], ...]:
    """A detent_ab value, or one per encoder -> n AB states (None = idle). Raises ValueError."""
    return tuple(parse_detent(x) for x in _per_encoder(v, n, "detent_ab"))


def controls_options(cfg: object) -> dict:
    """ZynthianControls keyword arguments from the k1000.yaml dict (`controls:` section):
    steps_per_detent (1, 2 or 4, or a list per physical encoder) and detent_ab ('11'... or
    'idle', or a list). Lenient like input_map.ControlMap.from_cfg: an invalid value is logged
    and its default kept, so a typo never leaves the box without knobs."""
    sec = cfg.get("controls") if isinstance(cfg, dict) else None
    if not isinstance(sec, dict):
        return {}
    n = len(DEFAULT_ENCODERS)
    out: dict = {}
    for key, check in (("steps_per_detent", steps_per_detent_list), ("detent_ab", detent_list)):
        if key not in sec:
            continue
        try:
            check(sec[key], n)
            out[key] = sec[key]
        except (TypeError, ValueError) as e:
            log.error("k1000.yaml controls.%s ignored (%s): using the default", key, e)
    return out


# ---------------------------------------------------------------------------------------------
# Hardware access layers (injectable so unit tests run without hardware)
# ---------------------------------------------------------------------------------------------
class SMBusLike(Protocol):
    def read_byte_data(self, addr: int, reg: int) -> int: ...
    def write_byte_data(self, addr: int, reg: int, value: int) -> None: ...
    def read_i2c_block_data(self, addr: int, reg: int, length: int) -> list[int]: ...
    def close(self) -> None: ...


class IntLines(Protocol):
    """The two MCP23017 interrupt lines as seen from the Pi."""
    def wait(self, timeout: float) -> bool: ...
    def read_events(self) -> list[int]: ...          # line offsets that saw a falling edge
    def levels(self) -> dict[int, int]: ...           # offset -> 0 (asserted) / 1 (idle)
    def close(self) -> None: ...


class ControlsError(RuntimeError):
    """Knobs unavailable (chip absent / not configurable). Audio must not depend on this."""


class ControlsClosed(ControlsError):
    """next_event() after close() once the queue is drained (a polling loop must not spin)."""


class Mcp23017:
    """Thin register layer. One sequential read of INTFA..GPIOB (6 bytes, 0.87 ms measured on the box
    at its 100 kHz i2c-1 clock) gives which port interrupted, the captured levels, the current levels, and
    clears both INT lines."""

    def __init__(self, bus: SMBusLike, addr: int = 0x20) -> None:
        self.bus = bus
        self.addr = addr

    def read(self, reg: int) -> int:
        return self.bus.read_byte_data(self.addr, reg)

    def write(self, reg: int, value: int) -> None:
        self.bus.write_byte_data(self.addr, reg, value & 0xFF)

    def configure(self) -> None:
        """Bring the chip from ANY state (incl. a previous BANK=1 user or a brown-out reset) to ours."""
        self.write(IOCON_BANK1_ADDR, 0x00)   # BANK=1 -> back to BANK=0; in BANK=0 it's GPINTENB
        self.write(IOCON, IOCON_VALUE)
        self.write(GPINTENA, 0x00)           # quiet while we change the comparison mode
        self.write(GPINTENB, 0x00)
        for reg in (IODIRA, IODIRB, GPPUA, GPPUB):
            self.write(reg, 0xFF)
        for reg in (IPOLA, IPOLB, DEFVALA, DEFVALB, INTCONA, INTCONB):
            self.write(reg, 0x00)
        self.write(GPINTENA, 0xFF)
        self.write(GPINTENB, 0xFF)
        self.read_state()                    # drop anything latched during the reconfiguration

    def verify(self) -> list[str]:
        """Mismatches between the chip and EXPECTED_CONFIG (empty = healthy). One 14-byte read."""
        regs = self.bus.read_i2c_block_data(self.addr, 0x00, 14)
        bad = []
        for reg, want in EXPECTED_CONFIG.items():
            got = regs[reg]
            if got != want:
                bad.append(f"{_REG_NAMES.get(reg, hex(reg))}=0x{got:02x} (want 0x{want:02x})")
        return bad

    def read_state(self) -> tuple[int, int, int, int, int, int]:
        """(INTFA, INTFB, INTCAPA, INTCAPB, GPIOA, GPIOB) in one transaction."""
        b = self.bus.read_i2c_block_data(self.addr, INTFA, 6)
        if len(b) != 6:
            raise OSError(f"short MCP23017 read ({len(b)} bytes)")
        return b[0], b[1], b[2], b[3], b[4], b[5]

    def read_guarded(self) -> tuple[int, tuple[int, int, int, int, int, int]]:
        """(GPPUB << 8 | GPPUA, read_state()) in ONE transaction (GPPUA..GPIOB, 8 bytes).

        WHY: the pull-up registers come back with every sample, so a sample taken after the
        expander reset (GPPU = 0x00, inputs floating) can be recognised and thrown away before it
        is decoded. Same INTF -> INTCAP -> GPIO order as read_state(), so it clears INT the same."""
        b = self.bus.read_i2c_block_data(self.addr, GPPUA, 8)
        if len(b) != 8:
            raise OSError(f"short MCP23017 read ({len(b)} bytes)")
        return b[0] | (b[1] << 8), (b[2], b[3], b[4], b[5], b[6], b[7])


class GpiodIntLines:
    """INTA/INTB via libgpiod v2: inputs, pull-up, falling-edge events (the lines are active-low)."""

    def __init__(self, chip: str, offsets: Sequence[int], consumer: str = "k1000-knobs") -> None:
        import gpiod  # apt: python3-libgpiod (2.2 API)
        from gpiod.line import Bias, Direction, Edge
        self._offsets = tuple(offsets)
        settings = gpiod.LineSettings(direction=Direction.INPUT, edge_detection=Edge.FALLING,
                                      bias=Bias.PULL_UP)
        self._req = gpiod.request_lines(chip, consumer=consumer, config={self._offsets: settings})

    def wait(self, timeout: float) -> bool:
        return self._req.wait_edge_events(max(0.0, timeout))

    def read_events(self) -> list[int]:
        return [ev.line_offset for ev in self._req.read_edge_events()]

    def levels(self) -> dict[int, int]:
        vals = self._req.get_values(list(self._offsets))
        return {off: int(v.value) for off, v in zip(self._offsets, vals)}

    def close(self) -> None:
        self._req.release()


def _default_bus(bus: int) -> SMBusLike:
    # smbus2 (apt: python3-smbus2, MIT), not python3-smbus (GPL-2.0-only; D-008). The same SMBus
    # API for every call made here: read_byte_data, write_byte_data, read_i2c_block_data (<= 32
    # bytes; 14 at most here) and close().
    import smbus2
    return smbus2.SMBus(bus)


# ---------------------------------------------------------------------------------------------
# The driver
# ---------------------------------------------------------------------------------------------
class ZynthianControls:
    """The four Zynthian V4 encoders and eight switches as an asyncio event stream.

        controls = ZynthianControls()
        async for ev in controls.events():      # EncoderEvent / SwitchEvent
            ...

    For a loop that must also wake on a timer (screen refresh), use the cancellation-safe
    `ev = await controls.next_event(timeout)` (None on timeout), never
    `asyncio.wait_for(agen.__anext__(), t)`: see events().

    A daemon worker thread owns the chip: it sleeps in gpiod's wait for a falling edge on INTA or
    INTB (or the next deadline), then reads GPPU/INTF/INTCAP/GPIO of both ports in one transaction,
    which clears the interrupt. It re-reads while an INT line is still low, and polls every
    `poll_ms` regardless, so a missed edge can never leave an INT line wedged low (with falling
    edges, a wedged line would silence the knobs for good). A sample whose pull-up registers are
    not 0xFF (expander reset: brown-out, hot-plug) is never decoded: the chip is re-verified,
    re-configured and the switches re-seeded right there. The full config is also re-verified every
    VERIFY_S (0.25 s, always < long_ms - debounce_ms) so no corruption can outlive a long press.
    Re-seeds release every switch that had reported a press. I2C errors are survived with backoff;
    they never kill the thread or reach the asyncio loop.
    """

    VERIFY_S = 0.25        # config re-check period; capped below long_s - debounce_s (_verify_period)
    MAX_REREADS = 3        # reads per wake while an INT line stays low
    NO_IRQ_POLL_S = 0.005  # poll period when GPIO27/GPIO4 cannot be requested
    STORM_EDGES = 100      # edge wakes per STORM_WINDOW_S that count as an interrupt storm
    STORM_WINDOW_S = 0.1   # (1 kHz; one knob spun hard is ~160 edges/s)
    STORM_POLL_S = 0.005
    STORM_HOLD_S = 0.1

    def __init__(self, bus: int = 1, addr: int = 0x20, int_a: int = 27, int_b: int = 4,
                 encoders: Sequence[tuple[PinRef, PinRef]] = DEFAULT_ENCODERS,
                 switches: Sequence[PinRef] = DEFAULT_SWITCHES, long_ms: float = 800, *,
                 debounce_ms: float = 10, poll_ms: float = 50,
                 steps_per_detent: "int | Sequence[int]" = 4,
                 detent_ab: "DetentRef | Sequence[DetentRef]" = DEFAULT_DETENT,
                 learn_rests: int = 3,
                 idle_ms: float = 300, gpiochip: str = "/dev/gpiochip0", use_irq: bool = True,
                 bus_factory: Callable[[int], SMBusLike] = _default_bus,
                 lines_factory: Optional[Callable[[str, Sequence[int]], IntLines]] = None,
                 clock: Callable[[], float] = time.monotonic,
                 on_raw: Optional[Callable[[int, int, float], None]] = None,
                 max_pending: int = 1024) -> None:
        self.bus_num, self.addr, self.int_a, self.int_b = bus, addr, int_a, int_b
        self.encoder_pins = [(pin_index(a), pin_index(b)) for a, b in encoders]
        self.switch_pins = [pin_index(p) for p in switches]
        used = [p for ab in self.encoder_pins for p in ab] + self.switch_pins
        if len(set(used)) != len(used):
            raise ValueError("an MCP23017 pin is assigned twice in encoders/switches")
        self.steps_per_detent = steps_per_detent_list(steps_per_detent, len(self.encoder_pins))
        self.detent_ab = detent_list(detent_ab, len(self.encoder_pins))
        self.long_s = _clamp(long_ms, 200, 5000) / 1000.0
        self.debounce_s = _clamp(debounce_ms, 1, 50) / 1000.0
        self.poll_s = _clamp(poll_ms, 5, 1000) / 1000.0
        self.idle_s = _clamp(idle_ms, 100, 2000) / 1000.0
        self.gpiochip = gpiochip
        self.use_irq = use_irq
        self._bus_factory = bus_factory
        self._lines_factory = lines_factory or (lambda chip, offs: GpiodIntLines(chip, offs))
        self._clock = clock
        self.on_raw = on_raw

        now = clock()
        self._decoders = [QuadratureDecoder(spd, 3, now, self.idle_s, rest=rest,
                                            learn_rests=learn_rests)
                          for spd, rest in zip(self.steps_per_detent, self.detent_ab)]
        self._hinted: set[int] = set()        # encoders whose half-rest hint was logged
        self._switches = [SwitchDebouncer(self.debounce_s, self.long_s, False, now)
                          for _ in self.switch_pins]
        self.levels = 0xFFFF  # last sampled B<<8|A pin levels
        self._moved_mask = 0  # pins that changed in the current read (_service: busy vs stuck)
        self.healthy = False
        self.irq = False
        self.stats: dict[str, int] = collections.defaultdict(int)

        self._bus: Optional[SMBusLike] = None
        self._chip: Optional[Mcp23017] = None
        self._lines: Optional[IntLines] = None
        self._i2c_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._verify_paused_until = 0.0   # self-test forcing; expires on its own (never sticks)
        self._storm_until = 0.0
        self._started = False
        self._closed = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._wake: Optional[asyncio.Event] = None
        self._pending: collections.deque[Event] = collections.deque()
        # The worker appends (and drops the oldest when full) while the consumer pops + merges:
        # both are compound check-then-act on the deque, so they share this lock (held for us).
        self._q_lock = threading.Lock()
        self._max_pending = max(1, int(max_pending))
        self._err_logged = 0

    # -- lifecycle ------------------------------------------------------------------------------
    def open(self) -> None:
        """Configure the chip and request the INT lines, without starting the worker (tests use
        this + _service()). Raises ControlsError if the chip is absent or will not configure."""
        if self._chip is not None:
            return
        try:
            self._bus = self._bus_factory(self.bus_num)
            self._chip = Mcp23017(self._bus, self.addr)
            self._chip.configure()
            bad = self._chip.verify()
            if bad:
                raise ControlsError("MCP23017 did not take its configuration: " + ", ".join(bad))
        except (OSError, ImportError) as e:
            self._close_bus()
            raise ControlsError(f"MCP23017 not reachable on i2c-{self.bus_num} "
                                f"0x{self.addr:02x}: {e}") from e
        except ControlsError:
            self._close_bus()
            raise
        if self.use_irq:
            try:
                self._lines = self._lines_factory(self.gpiochip, (self.int_a, self.int_b))
                self.irq = True
            except Exception as e:  # busy line, missing chip: degrade to fast polling, keep knobs
                log.warning("INT lines GPIO%d/GPIO%d unavailable (%s): polling every %.0f ms",
                            self.int_a, self.int_b, e, self.NO_IRQ_POLL_S * 1000)
                self._lines = None
        if self._lines is None:
            self.poll_s = min(self.poll_s, self.NO_IRQ_POLL_S)
        with self._i2c_lock:
            st = self._chip.read_state()     # clears INTA/INTB after the lines are watched
        self._reseed(st[4] | (st[5] << 8), self._clock())
        self.healthy = True

    def start(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """open() + start the worker thread. Call from the event loop (or pass `loop`)."""
        if self._started:
            return
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
        if loop is not None:
            self._bind_loop(loop)
        self.open()
        self._started = True
        self._thread = threading.Thread(target=self._run, name="k1000-knobs", daemon=True)
        self._thread.start()

    def close(self) -> None:
        """Stop the worker, release the lines and the bus. The chip stays configured (harmless)."""
        self._closed = True
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._lines is not None:
            try:
                self._lines.close()
            except Exception:
                pass
            self._lines = None
        self._close_bus()
        self._notify()

    async def __aenter__(self) -> "ZynthianControls":
        self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.close()

    def _close_bus(self) -> None:
        if self._bus is not None:
            try:
                self._bus.close()
            except Exception:
                pass
        self._bus = None
        self._chip = None

    def _bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        if self._wake is None:
            self._wake = asyncio.Event()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def storm(self) -> bool:
        """True while the worker is in interrupt-storm polling (see _enter_storm)."""
        return self._clock() < self._storm_until

    # -- async consumer -------------------------------------------------------------------------
    def _consumer_wake(self) -> asyncio.Event:
        """Bind to the running loop and start the worker on first use (raises ControlsError)."""
        if self._loop is None:
            self._bind_loop(asyncio.get_running_loop())
        if not self._started and not self._closed:
            self.start(self._loop)
        assert self._wake is not None
        return self._wake

    def _pop_merged(self) -> Optional[Event]:
        """Oldest event; encoder events of the same knob queued behind it are merged (deltas
        summed, a net-zero merge dropped) so a stalled UI never replays a backlog step by step."""
        with self._q_lock:
            while self._pending:
                ev = self._pending.popleft()
                if not isinstance(ev, EncoderEvent):
                    return ev
                delta, ts = ev.delta, ev.ts
                while (self._pending and isinstance(self._pending[0], EncoderEvent)
                       and self._pending[0].index == ev.index):
                    nxt = self._pending.popleft()
                    delta, ts = delta + nxt.delta, nxt.ts
                if delta:
                    return EncoderEvent(ev.index, delta, ts)
        return None

    async def next_event(self, timeout: Optional[float] = None) -> Optional[Event]:
        """The next EncoderEvent / SwitchEvent, or None after `timeout` seconds (None = wait).

        Cancellation-safe (no generator state; the queue lives on the object), so it is the API for
        a loop that also refreshes on a timer, or for asyncio.wait_for. Raises ControlsError if the
        chip is unreachable at first use, ControlsClosed after close() once the queue is drained.
        Single consumer: do not mix two concurrent readers of one ZynthianControls."""
        wake = self._consumer_wake()
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + max(0.0, timeout)
        while True:
            ev = self._pop_merged()
            if ev is not None:
                return ev
            if self._closed:
                raise ControlsClosed("knobs closed")
            wake.clear()
            if self._pending:            # the worker appended between the pop and the clear
                continue
            if deadline is None:
                await wake.wait()
                continue
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                await asyncio.wait_for(wake.wait(), remaining)
            except TimeoutError:
                pass                     # loop once more: pop (race with the worker), then None

    async def events(self) -> AsyncIterator[Event]:
        """Yield EncoderEvent / SwitchEvent in order (merged like next_event) until close().

        WARNING: never cancel a pending __anext__() of this generator, e.g. with
        asyncio.wait_for(agen.__anext__(), t): the cancellation is thrown into the generator,
        finalises it, and the next __anext__() raises StopAsyncIteration although the worker keeps
        queueing (silently dead knobs, mute included). Use next_event(timeout) for that. No event
        is lost by such a cancellation and calling events() again resumes the same queue."""
        while True:
            try:
                ev = await self.next_event()
            except ControlsClosed:
                return
            if ev is not None:
                yield ev

    def _emit(self, ev: Event) -> None:
        with self._q_lock:
            if len(self._pending) >= self._max_pending:   # consumer gone: keep the newest
                self._pending.popleft()
                self.stats["dropped_events"] += 1
            self._pending.append(ev)
        self._notify()

    def _notify(self) -> None:
        loop, wake = self._loop, self._wake
        if loop is None or wake is None:
            return
        try:
            loop.call_soon_threadsafe(wake.set)
        except RuntimeError:  # loop closed during shutdown
            pass

    # -- worker ---------------------------------------------------------------------------------
    def _verify_period(self) -> float:
        """VERIFY_S, but always shorter than (long_s - debounce_s) / 2: a corruption the per-read
        pull-up check cannot see (IPOL, IODIR...) is repaired, and every switch re-seeded, before
        a press it faked could become a LONG (B4: a mute toggle)."""
        return max(0.02, min(self.VERIFY_S, (self.long_s - self.debounce_s) / 2))

    def _run(self) -> None:
        backoff = 0.0
        next_poll = self._clock() + self.poll_s
        next_verify = self._clock() + self._verify_period()
        win_start, win_edges = self._clock(), 0
        while not self._stop.is_set():
            try:
                if not self.healthy:
                    self._recover()
                    next_verify = self._clock() + self._verify_period()
                now = self._clock()
                # +0.5 ms: never wake a hair before a debounce/long deadline and read twice
                timeout = max(0.0, min(next_poll, self._next_deadline() + 0.0005) - now)
                storm = now < self._storm_until
                woke = False
                if self._lines is not None and not storm:
                    if self._lines.wait(timeout):
                        woke = True
                        self._count_edges(self._lines.read_events())
                        now = self._clock()
                        if now - win_start > self.STORM_WINDOW_S:
                            win_start, win_edges = now, 0
                        win_edges += 1
                        if win_edges > self.STORM_EDGES:
                            self._enter_storm(now, "edge flood")
                else:
                    wait_s = min(timeout, self.STORM_POLL_S) if storm else timeout
                    if wait_s > 0:
                        self._stop.wait(wait_s)
                    if storm and self._lines is not None and self._lines.wait(0):
                        self._count_edges(self._lines.read_events())   # drain, don't react
                if self._stop.is_set():
                    break
                if not woke and self._clock() >= next_poll:
                    self.stats["polls"] += 1
                if self._service(reread=not storm):
                    self._enter_storm(self._clock(), "INT held low")
                now = self._clock()
                next_poll = now + self.poll_s
                if now >= next_verify:
                    next_verify = now + self._verify_period()
                    if now >= self._verify_paused_until:
                        self._verify_or_reconfigure("periodic verify")
                backoff = 0.0
            except OSError as e:
                self.healthy = False
                self.stats["i2c_errors"] += 1
                if self._err_logged < 3 or self.stats["i2c_errors"] % 100 == 0:
                    log.warning("knobs I2C error #%d: %s", self.stats["i2c_errors"], e)
                    self._err_logged += 1
                backoff = min(0.5, max(0.02, backoff * 2))   # 20 ms .. 0.5 s
                self._stop.wait(backoff)
            except Exception:  # a bug must not kill the knobs silently or spin
                log.exception("knobs worker error")
                self.stats["worker_errors"] += 1
                self._stop.wait(0.5)

    def _count_edges(self, offsets: Iterable[int]) -> None:
        for off in offsets:
            self.stats["edges_a" if off == self.int_a else "edges_b"] += 1

    def _enter_storm(self, now: float, why: str) -> None:
        """Interrupt storm (INT stuck asserted, or re-asserting on every read, or a chattering
        contact): stop reacting to edges and poll every STORM_POLL_S with one read per pass for
        STORM_HOLD_S. Knobs keep working (degraded), the CPU never spins, and the periodic config
        verify repairs a corrupted INTCON/DEFVAL meanwhile."""
        if now >= self._storm_until:
            self.stats["storms"] += 1
            if self.stats["storms"] <= 3 or self.stats["storms"] % 100 == 0:
                log.warning("knobs interrupt storm #%d (%s): polling every %.0f ms",
                            self.stats["storms"], why, self.STORM_POLL_S * 1000)
        self._storm_until = now + self.STORM_HOLD_S

    def _service(self, reread: bool = True) -> bool:
        """Read (clears INTA/INTB) and feed the decoders; with `reread`, repeat while an INT line
        stays low. Returns True if an INT line is still asserted afterwards AND the last read
        brought no new level on that line's port (stuck). A sample read with the pull-ups off
        is never decoded (see _config_alarm).

        A knob spun fast re-asserts INT right after each clearing read, with a new level every
        time: that is a busy line, not a stuck one, and the next edge wakes the worker at once.
        Calling it stuck sent ordinary fast turns into storm polling (5 ms, one read per pass)
        in the middle of the spin, where the sampling gaps lost whole clicks (PERF-2). A wedged
        line, or a corrupted INTCON re-asserting on a level that does not move, still brings
        no new level: stuck. A chattering contact is the edge-flood detector's (STORM_EDGES)."""
        assert self._chip is not None
        low: list[int] = []
        moved = 0
        for _ in range(self.MAX_REREADS if reread else 1):
            with self._i2c_lock:
                gppu, st = self._chip.read_guarded()
            self.stats["reads"] += 1
            if gppu != 0xFFFF:
                # sample dropped. Repaired chip: _reseed adopted fresh levels. Corrupted read:
                # the next edge/poll re-reads (a lost quadrature state is inferred by the decoder)
                self._config_alarm(gppu)
                low = []
                break
            self._moved_mask = 0
            self._ingest(st, self._clock())
            moved = self._moved_mask
            low = ([off for off, v in self._lines.levels().items() if not v]
                   if self._lines is not None else [])
            if not low:
                break
        # Each line still low is stuck unless ITS port brought a new level on the last read
        # (a knob on port B turning must not hide a wedged INTA) AND the line fell again since
        # (a fresh edge is queued: the next wait returns at once). A line wedged low makes no
        # edge, even with a knob turning on its port: stuck, storm polling, as before.
        stuck = any(not moved & (0x00FF if off == self.int_a else 0xFF00) for off in low)
        if low and not stuck and not self._lines.wait(0):
            stuck = True
        if low and not stuck:
            self.stats["int_busy"] += 1
        if stuck:
            self.stats["int_stuck"] += 1
        self._tick(self._clock())
        return stuck

    def _ingest(self, st: Sequence[int], now: float) -> None:
        intf_a, intf_b, cap_a, cap_b, gp_a, gp_b = st
        if intf_a or intf_b:
            # INTCAP holds the levels at the moment the port interrupted; feeding it before the
            # current levels recovers a transition that happened while we were reading. A port
            # without INTF has a stale INTCAP: use our last known levels for it instead.
            a = cap_a if intf_a else self.levels & 0xFF
            b = cap_b if intf_b else self.levels >> 8
            self._feed(a | (b << 8), now)
        self._feed(gp_a | (gp_b << 8), now)

    def _feed(self, sample: int, now: float) -> None:
        changed = sample ^ self.levels
        if not changed:
            return
        self._moved_mask |= changed
        self.levels = sample
        if self.on_raw is not None:
            try:
                self.on_raw(sample, changed, now)
            except Exception:
                log.exception("on_raw callback failed")
        for i, (pa, pb) in enumerate(self.encoder_pins):
            if changed & ((1 << pa) | (1 << pb)):
                dec = self._decoders[i]
                inv, gl = dec.invalid, dec.glitches
                d = dec.update((((sample >> pa) & 1) << 1) | ((sample >> pb) & 1), now)
                self.stats["invalid_transitions"] += dec.invalid - inv
                if dec.glitches != gl:
                    self.stats["glitches"] += 1
                    n = self.stats["glitches"]
                    if n <= 5 or n % 100 == 0:   # the journal can tell a phantom from a hand
                        log.info("enc%d: a click from stillness faster than a hand was ignored "
                                 "(contact glitch #%d)", i, n)
                if d:
                    self._emit(EncoderEvent(i, d, now))
        for i, p in enumerate(self.switch_pins):
            if changed & (1 << p):
                for kind in self._switches[i].feed(((sample >> p) & 1) == 0, now):
                    self._emit(SwitchEvent(i, kind, now))

    def _tick(self, now: float) -> None:
        for i, dec in enumerate(self._decoders):
            learned = dec.relearned
            d = dec.settle(now)
            if d:
                self._emit(EncoderEvent(i, d, now))
            if dec.relearned != learned:
                self.stats["detents_learned"] += 1
                log.warning("enc%d: detent learned at AB=%s after %d long rests there "
                            "(set controls.detent_ab to keep it)", i,
                            f"{dec.anchor:02b}", dec.learn_rests)
            if dec.half_rests >= HALF_REST_HINT and i not in self._hinted:
                self._hinted.add(i)
                log.warning("enc%d: %d stops in a row half a click apart: does it need "
                            "steps_per_detent 2? (box/tools/k1000-knobtrace.py)", i, dec.half_rests)
        for i, sw in enumerate(self._switches):
            for kind in sw.tick(now):
                self._emit(SwitchEvent(i, kind, now))

    def _next_deadline(self) -> float:
        ds = [d for d in (x.deadline() for x in (*self._decoders, *self._switches)) if d is not None]
        return min(ds) if ds else float("inf")

    def _reseed(self, levels: int, now: float) -> None:
        """Adopt the current pin levels (start-up, after a chip reset or I2C fault). Decoders
        restart silently. A switch that had reported a press gets its RELEASE now (no SHORT, no
        LONG) so press/release always pair; a switch still held stays suppressed until released."""
        self.levels = levels
        for (pa, pb), dec in zip(self.encoder_pins, self._decoders):
            dec.reset((((levels >> pa) & 1) << 1) | ((levels >> pb) & 1), now)
        for i, (p, sw) in enumerate(zip(self.switch_pins, self._switches)):
            for kind in sw.abort(((levels >> p) & 1) == 0, now):
                self._emit(SwitchEvent(i, kind, now))

    def _recover(self) -> None:
        assert self._chip is not None
        with self._i2c_lock:
            self._chip.configure()
            st = self._chip.read_state()
        self._reseed(st[4] | (st[5] << 8), self._clock())
        self.healthy = True
        self._err_logged = 0
        self.stats["recoveries"] += 1
        log.warning("knobs recovered after I2C errors (chip reconfigured)")

    def _config_alarm(self, gppu: int) -> None:
        """A service read saw GPPU != 0xFFFF: the expander probably reset (pull-ups off, inputs
        floating). The sample was dropped by the caller; check the whole config and repair it.
        A clean verify means the read itself was corrupted (counted, nothing else to do)."""
        self.stats["pullup_alarms"] += 1
        if not self._verify_or_reconfigure(f"GPPU=0x{gppu:04x} on a service read"):
            self.stats["bad_reads"] += 1
            n = self.stats["bad_reads"]
            if n <= 3 or n % 100 == 0:
                log.warning("knobs: corrupted MCP23017 read #%d (GPPU=0x%04x, config intact): "
                            "sample dropped", n, gppu)

    def _verify_or_reconfigure(self, why: str = "") -> bool:
        """Verify the chip config; if it drifted, re-configure and re-seed. True if repaired."""
        assert self._chip is not None
        with self._i2c_lock:
            bad = self._chip.verify()
            if not bad:
                return False
            self._chip.configure()
            st = self._chip.read_state()
        self.stats["reconfigs"] += 1
        if self.stats["reconfigs"] <= 3 or self.stats["reconfigs"] % 100 == 0:
            log.warning("MCP23017 configuration lost (%s; %s): reconfigured (#%d)",
                        why or "verify", ", ".join(bad), self.stats["reconfigs"])
        self._reseed(st[4] | (st[5] << 8), self._clock())
        return True

    # -- introspection + hardware self-test hooks -----------------------------------------------
    def int_levels(self) -> dict[int, int]:
        """{gpio: 0 asserted / 1 idle} for INTA/INTB ({} when running without the lines)."""
        return self._lines.levels() if self._lines is not None else {}

    def pin_levels(self) -> dict[str, int]:
        return {name: (self.levels >> i) & 1 for i, name in enumerate(PIN_NAMES)}

    def force_interrupt(self, port: str, max_hold_s: float = 2.0) -> int:
        """SELF-TEST ONLY. Hold `port`'s INT asserted without touching a knob: switch bit 0 of the
        port to compare-against-DEFVAL with DEFVAL opposite to its level. Returns that pin index.
        The periodic config verify (which would undo this) is paused for at most `max_hold_s`:
        if release_interrupt() never comes (cancelled caller), the verify repairs the chip."""
        assert self._chip is not None
        base = 0 if port.upper() == "A" else 8
        self._verify_paused_until = self._clock() + _clamp(max_hold_s, 0.05, 10.0)
        with self._i2c_lock:
            level = (self._chip.read(GPIOA if base == 0 else GPIOB)) & 1
            self._chip.write(DEFVALA if base == 0 else DEFVALB, 0x00 if level else 0x01)
            self._chip.write(INTCONA if base == 0 else INTCONB, 0x01)
        return base

    def release_interrupt(self, port: str) -> None:
        """SELF-TEST ONLY. Back to compare-to-previous. Deliberately does NOT read the port, so the
        interrupt latched while forced is cleared by the worker's own read. Note what clears it in
        self_test(): forcing holds INT low, which the worker treats as an interrupt storm, so the
        clear is the 5 ms storm poll (STORM_POLL_S), NOT the poll_ms resync poll that recovers a
        missed edge in normal IRQ mode (that one is proven by the missed-edge tests)."""
        assert self._chip is not None
        base = 0 if port.upper() == "A" else 8
        try:
            with self._i2c_lock:
                self._chip.write(INTCONA if base == 0 else INTCONB, 0x00)
                self._chip.write(DEFVALA if base == 0 else DEFVALB, 0x00)
        finally:
            self._verify_paused_until = 0.0


def _release_quietly(controls: ZynthianControls, port: str) -> None:
    """release_interrupt() from a finally: an I2C error must not mask a CancelledError. The
    verify pause has been lifted either way, so the periodic verify repairs INTCON/DEFVAL."""
    try:
        controls.release_interrupt(port)
    except OSError as e:
        log.warning("self-test: could not release INT%s (%s); the config verify will repair it",
                    port, e)


async def self_test(controls: ZynthianControls, timeout_s: float = 0.5) -> dict[str, dict]:
    """Force an interrupt on each port and check that the worker woke on that INT line's falling
    edge, did not spin while the line was held low, and cleared it once released. No knob needed.
    Requires a started ZynthianControls with IRQ lines.

    What it proves and what it does not: holding INT forced puts the worker into storm polling,
    so the clear after release (clear_ms ~ STORM_POLL_S) is the storm poll's work
    (`storm_at_release` says so). The poll_ms resync poll that recovers a MISSED edge in normal
    IRQ mode is proven separately (test_hw_missed_edge_is_recovered_by_the_resync_poll).
    Cancellation-safe: a cancelled self-test always releases the forced port."""
    out: dict[str, dict] = {}
    for port, line, key in (("A", controls.int_a, "edges_a"), ("B", controls.int_b, "edges_b")):
        await asyncio.sleep(2 * controls.poll_s)            # start from a quiet, cleared chip
        res: dict = {"gpio": line, "idle_before": controls.int_levels().get(line)}
        edges0, reads0 = controls.stats[key], controls.stats["reads"]
        t0 = time.monotonic()
        try:
            controls.force_interrupt(port, max_hold_s=timeout_s + 1.0)
            while controls.stats[key] == edges0 and time.monotonic() - t0 < timeout_s:
                await asyncio.sleep(0.001)
            res["woke"] = controls.stats[key] > edges0
            res["wake_ms"] = round((time.monotonic() - t0) * 1000, 1)
            await asyncio.sleep(0.2)                        # held low for 200 ms
            res["held_low"] = controls.int_levels().get(line) == 0
            res["reads_while_held_200ms"] = controls.stats["reads"] - reads0
            res["storm_at_release"] = controls.storm
        finally:
            t1 = time.monotonic()
            _release_quietly(controls, port)
        while controls.int_levels().get(line) == 0 and time.monotonic() - t1 < timeout_s:
            await asyncio.sleep(0.001)
        res["cleared"] = controls.int_levels().get(line) == 1
        res["clear_ms"] = round((time.monotonic() - t1) * 1000, 1)
        # not spinning: storm polling is one read per STORM_POLL_S (+ the re-reads of the first
        # wake and some slack); a spinning edge->read loop does thousands in 200 ms
        res["max_reads_allowed"] = int(2 * 0.2 / controls.STORM_POLL_S + 4 * controls.MAX_REREADS)
        res["ok"] = bool(res["woke"] and res["held_low"] and res["cleared"]
                         and res["reads_while_held_200ms"] <= res["max_reads_allowed"])
        out[port] = res
    return out


# ---------------------------------------------------------------------------------------------
# CLI: python3 -m k1000.hw.mcp23017 --monitor
# ---------------------------------------------------------------------------------------------
def encoder_label(i: int, pins: tuple[int, int]) -> str:
    return f"enc{i} [A={PIN_NAMES[pins[0]]} B={PIN_NAMES[pins[1]]}]"


def switch_label(i: int, pin: int, n_encoders: int = 4) -> str:
    role = f"push of enc{i}?" if i < n_encoders else f"button {i - n_encoders + 1}?"
    return f"sw{i} [{PIN_NAMES[pin]}] ({role})"


class _Bursts:
    """Counts raw quarter-steps per encoder until the knob rests, so one slow click tells us the
    encoder type (4, 2 or 1 transitions per detent). Fed from the worker thread."""

    def __init__(self, encoder_pins: list[tuple[int, int]], quiet_s: float = 0.4) -> None:
        self.pins = encoder_pins
        self.quiet_s = quiet_s
        self._lock = threading.Lock()
        self._state: list[Optional[int]] = [None] * len(encoder_pins)
        self._b: dict[int, list] = {}  # i -> [quarter_steps, skipped, start_ab, last_ts]

    def seed(self, levels: int) -> None:
        self._state = [(((levels >> a) & 1) << 1) | ((levels >> b) & 1) for a, b in self.pins]

    def on_raw(self, levels: int, changed: int, ts: float) -> None:
        with self._lock:
            for i, (a, b) in enumerate(self.pins):
                if not changed & ((1 << a) | (1 << b)):
                    continue
                cur = (((levels >> a) & 1) << 1) | ((levels >> b) & 1)
                prev = self._state[i] if self._state[i] is not None else cur
                self._state[i] = cur
                burst = self._b.setdefault(i, [0, 0, prev, ts])
                step = _QSTEP[(prev << 2) | cur]
                if step == _INV:
                    burst[1] += 1
                else:
                    burst[0] += step
                burst[3] = ts

    def finished(self, now: float) -> list[str]:
        out = []
        with self._lock:
            for i in [i for i, b in self._b.items() if now - b[3] >= self.quiet_s]:
                q, skipped, start, _ = self._b.pop(i)
                end = self._state[i]
                out.append(f"{encoder_label(i, self.pins[i])} burst: {q:+d} quarter-steps"
                           f"{f' ({skipped} skipped)' if skipped else ''}, AB {start:02b} -> {end:02b}")
        return out


async def _monitor(args: argparse.Namespace) -> int:
    bursts: Optional[_Bursts] = None
    t_start = time.monotonic()

    def on_raw(levels: int, changed: int, ts: float) -> None:
        if bursts is not None:
            bursts.on_raw(levels, changed, ts)
        if args.raw:
            pins = " ".join(f"{PIN_NAMES[i]}={(levels >> i) & 1}" for i in range(16) if changed >> i & 1)
            print(f"{ts - t_start:9.3f}  raw  A=0x{levels & 0xFF:02x} B=0x{levels >> 8:02x}  {pins}",
                  flush=True)

    controls = ZynthianControls(args.bus, args.addr, args.int_a, args.int_b,
                                long_ms=args.long_ms, steps_per_detent=args.steps_per_detent,
                                detent_ab=args.detent_ab, use_irq=not args.no_irq, on_raw=on_raw)
    bursts = _Bursts(controls.encoder_pins)
    try:
        controls.open()                   # levels known, worker not yet running
        bursts.seed(controls.levels)
        controls.start()
    except ControlsError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    cpu0 = os.times()
    t0 = time.monotonic()
    lv = controls.levels
    print(f"K1000 knobs - MCP23017 i2c-{args.bus} 0x{args.addr:02x}, "
          f"INTA=GPIO{args.int_a} INTB=GPIO{args.int_b} ({'interrupts' if controls.irq else 'POLLING, no IRQ'}), "
          f"{args.steps_per_detent} transitions/detent, long press {args.long_ms:.0f} ms", flush=True)
    print(f"idle levels: GPIOA=0x{lv & 0xFF:02x} GPIOB=0x{lv >> 8:02x}", flush=True)
    for i, (a, b) in enumerate(controls.encoder_pins):
        print(f"  {encoder_label(i, (a, b))}  rest AB={(lv >> a) & 1}{(lv >> b) & 1}")
    for i, p in enumerate(controls.switch_pins):
        print(f"  {switch_label(i, p, len(controls.encoder_pins))}  "
              f"{'released' if (lv >> p) & 1 else 'PRESSED (ignored until released)'}")
    if args.selftest:
        res = await self_test(controls)
        for port, r in res.items():
            print(f"selftest INT{port} (GPIO{r['gpio']}): {'OK' if r['ok'] else 'FAIL'} {r}", flush=True)
        if not args.monitor:
            controls.close()
            return 0 if all(r["ok"] for r in res.values()) else 1
    print("Turn each knob ONE click slowly, then press each button (short, then long). "
          "Ctrl-C to stop.\n'burst' lines count raw quarter-steps per click: 4 = full-step encoder "
          "(the default), 2 = half-step, 1 = quarter-step (then rerun with --steps-per-detent N).",
          flush=True)

    n_enc = n_sw = 0
    totals = [0] * len(controls.encoder_pins)
    press_ts: dict[int, float] = {}
    stop_at = t0 + args.duration if args.duration > 0 else None

    async def reporter() -> None:
        while True:
            await asyncio.sleep(0.1)
            for line in bursts.finished(time.monotonic()):
                print(f"{time.monotonic() - t_start:9.3f}  {line}", flush=True)

    rep = asyncio.create_task(reporter())
    try:
        while True:
            remaining = None if stop_at is None else stop_at - time.monotonic()
            if remaining is not None and remaining <= 0:
                break
            ev = await controls.next_event(remaining)   # cancellation-safe, None on timeout
            if ev is None:
                break
            t = ev.ts - t_start
            if isinstance(ev, EncoderEvent):
                n_enc += 1
                totals[ev.index] += ev.delta
                print(f"{t:9.3f}  {encoder_label(ev.index, controls.encoder_pins[ev.index])}"
                      f"  {ev.delta:+d}  (total {totals[ev.index]:+d})", flush=True)
            else:
                n_sw += 1
                extra = ""
                if ev.kind == PRESS:
                    press_ts[ev.index] = ev.ts
                elif ev.kind in (SHORT, LONG) and ev.index in press_ts:
                    extra = f"  ({(ev.ts - press_ts[ev.index]) * 1000:.0f} ms)"
                print(f"{t:9.3f}  {switch_label(ev.index, controls.switch_pins[ev.index], len(controls.encoder_pins))}"
                      f"  {ev.kind}{extra}", flush=True)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        rep.cancel()
        elapsed = time.monotonic() - t0
        cpu1 = os.times()
        cpu = (cpu1.user - cpu0.user) + (cpu1.system - cpu0.system)
        s = controls.stats
        controls.close()
        print(f"--- {elapsed:.1f} s: encoder_events={n_enc} switch_events={n_sw} "
              f"edges_a={s['edges_a']} edges_b={s['edges_b']} polls={s['polls']} reads={s['reads']} "
              f"i2c_errors={s['i2c_errors']} int_stuck={s['int_stuck']} reconfigs={s['reconfigs']} "
              f"invalid={s['invalid_transitions']} cpu={100 * cpu / max(elapsed, 1e-6):.2f}%", flush=True)
    return 0


def main(argv: Optional[Iterable[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m k1000.hw.mcp23017",
                                description="Zynthian V4 knobs/buttons (MCP23017) monitor and self-test.")
    p.add_argument("--monitor", action="store_true",
                   help="print every knob/button event with its physical pin names")
    p.add_argument("--selftest", action="store_true",
                   help="force an interrupt on each port; check the driver wakes on INTA/INTB and clears it")
    p.add_argument("--raw", action="store_true", help="also print every raw pin change")
    p.add_argument("--duration", type=float, default=0.0, help="stop after N seconds (0 = until Ctrl-C)")
    p.add_argument("--steps-per-detent", type=int, choices=(1, 2, 4), default=4)
    p.add_argument("--detent-ab", default=DEFAULT_DETENT,
                   help="AB levels at a detent: 11 (default), 10, 01, 00, or idle (level at start)")
    p.add_argument("--long-ms", type=float, default=800.0)
    p.add_argument("--bus", type=int, default=1)
    p.add_argument("--addr", type=lambda s: int(s, 0), default=0x20)
    p.add_argument("--int-a", type=int, default=27)
    p.add_argument("--int-b", type=int, default=4)
    p.add_argument("--no-irq", action="store_true", help="poll only (if GPIO27/GPIO4 are busy)")
    args = p.parse_args(list(argv) if argv is not None else None)
    if not (args.monitor or args.selftest):
        p.print_help()
        return 2
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        return asyncio.run(_monitor(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
