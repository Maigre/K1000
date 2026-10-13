# SPDX-License-Identifier: GPL-3.0-or-later
"""K1000 persistent model: the show state, its legal ranges, and crash-safe persistence.

Why this module is strict:
- Every value that reaches CamillaDSP comes from a `State`, so clamping here (and again in
  `dspconfig`) is what keeps a corrupt file, a stray GUI edit or a fast encoder from ever
  producing a loud or invalid setting.
- The box loses power without warning (gig reality), so writes are atomic (tmp + fsync + rename +
  dir fsync) and reads never fail: a missing/corrupt/old file falls back to safe defaults and the
  bad copy is kept for post-mortem.

Field paths (`"master_db"`, `"live_peq.2.gain"`, `"out_trim_db.1"`) address one value for the UI
and controller; `range_for(path)` gives its `Range`.
"""
from __future__ import annotations

import copy
import json
import logging
import math
import os
import shutil
import tempfile
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

N_SYS_PEQ = 8
N_LIVE_PEQ = 4
N_OUT = 4

PROFILES: tuple[str, ...] = ("dev", "m4")
AUDITIONS: tuple[str, ...] = ("sum", "tops", "subs")

# "Off" is a frequency move, never a pipeline change (contract §3): these are the parked values.
LOWCUT_OFF_HZ = 10.0
SUB_LOWPASS_OFF_HZ = 20000.0
LOWCUT_Q = 0.707          # 2nd-order Butterworth = 12 dB/oct, the LD-2 Lo-Cut slope
SUB_LOWPASS_Q = 0.707

# The engine runs one fixed rate (contract §3). A Delay with `subsample: false` is a whole number
# of samples, rounded to nearest (measured on 5.0.0-beta.1: 0.0104 ms -> 0, 0.0105 ms -> 1), so
# one sample (1/48 ms) is the finest delay step that changes anything.
SAMPLERATE = 48000
SAMPLE_MS = 1000.0 / SAMPLERATE

DEFAULT_SYS_PEQ_FREQS = (63.0, 125.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0)
DEFAULT_LIVE_PEQ_FREQS = (250.0, 1000.0, 2500.0, 5000.0)


@dataclass(frozen=True)
class Range:
    """Legal range of one numeric field.

    `step` is the per-detent increment: in the field's unit for linear fields, in octaves for
    `log` fields (frequency, Q), so 1/12 means one semitone per detent. `fine` is the step while
    the encoder push is held. `digits` rounds stored values so JSON and comparisons stay stable.
    `snap` (linear fields only): a move lands on the grid of the step in use (multiples of
    `step`, or of `fine` while the push is held). From an off-grid value the first detent goes
    to the next grid line in the turn's direction, so a click never moves more than one step.
    Clamping never snaps: a stored off-grid value stays as it is until it is moved.
    """

    min: float
    max: float
    step: float
    log: bool = False
    fine: float | None = None
    default: float = 0.0
    digits: int = 2
    unit: str = ""
    snap: bool = False

    def clamp(self, value: Any, fallback: float | None = None) -> float:
        """Coerce to float inside [min, max]; non-numbers/NaN/huge ints fall back (then to `default`)."""
        try:
            v = float(value)
        except (TypeError, ValueError, OverflowError):   # OverflowError: a 400-digit JSON int
            v = math.nan
        if not math.isfinite(v):
            v = self.default if fallback is None else float(fallback)
            if not math.isfinite(v):
                v = self.default
        v = min(self.max, max(self.min, v))
        return round(v, self.digits)

    def nudge(self, value: float, detents: int | float, fine: bool = False) -> float:
        """Move `value` by `detents` encoder steps (multiplicative for log fields), clamped."""
        step = self.fine if (fine and self.fine) else self.step
        v = self.clamp(value)
        if self.log:
            v = v * (2.0 ** (step * detents))
        elif self.snap and detents:
            # 112.25 Hz on a 2 Hz grid: one click up -> 114, one click down -> 112.
            q = v / step
            base = math.floor(q + 1e-9) if detents > 0 else math.ceil(q - 1e-9)
            v = (base + detents) * step
        else:
            v = v + step * detents
        return self.clamp(v)


_FREQ_STEP = 1.0 / 12.0
_FREQ_FINE = 1.0 / 48.0
_Q_STEP = 1.0 / 6.0
_Q_FINE = 1.0 / 24.0

RANGES: dict[str, Range] = {
    # Main fader. Max 0 dB (unity) is also written as the engine's `volume_limit`, so neither the
    # knobs nor CamillaGUI can push the Main fader above it.
    "master_db": Range(-80.0, 0.0, 0.5, default=-20.0, unit="dB"),
    "balance_db": Range(-12.0, 12.0, 0.5, default=0.0, unit="dB"),
    "input_trim_db": Range(-24.0, 12.0, 0.5, default=0.0, unit="dB"),
    # 50 ms = 17 m of sub/top offset: more than any roaming setup needs. Fine = one sample: a
    # finer step would often not change the (whole-sample) engine delay, yet still be written
    # and rebuild the delay line, i.e. a click with no change in alignment. dspconfig sends the
    # engine whole samples (`delay_samples`); 6 digits keep 1/48 ms steps from drifting.
    "sub_delay_ms": Range(0.0, 50.0, 0.1, fine=SAMPLE_MS, default=0.0, digits=6, unit="ms"),
    "top_delay_ms": Range(0.0, 50.0, 0.1, fine=SAMPLE_MS, default=0.0, digits=6, unit="ms"),
    # The Lo-Cut moves in Hz, not octaves (D-011, Thomas: "2 Hz per click"): 2 Hz per detent,
    # 1 Hz with the push held, on that grid (a value saved off it, e.g. 112.25, snaps on its first
    # move). Every other frequency stays logarithmic.
    "lowcut_hz": Range(20.0, 300.0, 2.0, fine=1.0, snap=True, default=100.0, unit="Hz"),
    "sub_lowpass_hz": Range(40.0, 250.0, _FREQ_STEP, log=True, fine=_FREQ_FINE, default=120.0, unit="Hz"),
    "sys_peq.freq": Range(20.0, 20000.0, _FREQ_STEP, log=True, fine=_FREQ_FINE, default=1000.0, unit="Hz"),
    "sys_peq.gain": Range(-20.0, 10.0, 0.5, default=0.0, unit="dB"),
    "sys_peq.q": Range(0.3, 16.0, _Q_STEP, log=True, fine=_Q_FINE, default=1.0, digits=3),
    "live_peq.freq": Range(20.0, 20000.0, _FREQ_STEP, log=True, fine=_FREQ_FINE, default=1000.0, unit="Hz"),
    # Live boosts are capped at +6 dB: the live layer is for cutting rings and taming, not pushing.
    "live_peq.gain": Range(-20.0, 6.0, 0.5, default=0.0, unit="dB"),
    "live_peq.q": Range(0.3, 16.0, _Q_STEP, log=True, fine=_Q_FINE, default=4.0, digits=3),
    "live_lowshelf.freq": Range(30.0, 1000.0, _FREQ_STEP, log=True, fine=_FREQ_FINE, default=120.0, unit="Hz"),
    "live_lowshelf.gain": Range(-12.0, 6.0, 0.5, default=0.0, unit="dB"),
    "live_lowshelf.q": Range(0.4, 1.5, _Q_STEP, log=True, fine=_Q_FINE, default=0.707, digits=3),
    "live_highshelf.freq": Range(1000.0, 16000.0, _FREQ_STEP, log=True, fine=_FREQ_FINE, default=8000.0, unit="Hz"),
    "live_highshelf.gain": Range(-12.0, 6.0, 0.5, default=0.0, unit="dB"),
    "live_highshelf.q": Range(0.4, 1.5, _Q_STEP, log=True, fine=_Q_FINE, default=0.707, digits=3),
    "out_trim_db": Range(-24.0, 6.0, 0.5, default=0.0, unit="dB"),
}

# ALIGN E3 (D-011): ONE signed alignment delay over the two stored ones, value = sub - top.
# > 0: the subs are delayed (subs in front of the tops); < 0: the tops are delayed. Not a stored
# field (state.json, dspconfig, CamillaGUI keep sub_delay_ms and top_delay_ms): `State.get`
# computes it, and `State.set`/`State.nudge` write sub = max(v, 0), top = max(-v, 0), so after
# any move at most one of them is non-zero (a saved state with both set shows the net value and
# is normalised by its first move, never at load). A move never jumps across zero: it stops at
# exactly 0 (ALIGNED), and the next detent goes to the other side.
ALIGN_DELAY = "align_delay_ms"
VIRTUAL_RANGES: dict[str, Range] = {
    ALIGN_DELAY: Range(-50.0, 50.0, 0.1, fine=SAMPLE_MS, default=0.0, digits=6, unit="ms"),
}

BOOL_FIELDS: tuple[str, ...] = (
    "muted", "mono_subs", "sub_inverted", "top_inverted", "lowcut_on", "sub_lowpass_on",
    "spectrum_on",
)
CHOICES: dict[str, tuple[str, ...]] = {"profile": PROFILES, "dev_audition": AUDITIONS}

# Changing these rebuilds the CamillaDSP pipeline (mixers/devices): SETUP page only, with confirm.
REBUILD_FIELDS: frozenset[str] = frozenset({"mono_subs", "profile", "dev_audition"})


@dataclass
class PeqBand:
    """One Biquad Peaking band (gain 0 dB = transparent)."""

    freq: float = 1000.0
    gain: float = 0.0
    q: float = 1.0


@dataclass
class ShelfBand:
    """One Biquad shelf (Lowshelf/Highshelf), steepness given as Q."""

    freq: float = 1000.0
    gain: float = 0.0
    q: float = 0.707


def default_sys_peq() -> list[PeqBand]:
    return [PeqBand(f, 0.0, RANGES["sys_peq.q"].default) for f in DEFAULT_SYS_PEQ_FREQS]


def default_live_peq() -> list[PeqBand]:
    return [PeqBand(f, 0.0, RANGES["live_peq.q"].default) for f in DEFAULT_LIVE_PEQ_FREQS]


def default_lowshelf() -> ShelfBand:
    return ShelfBand(RANGES["live_lowshelf.freq"].default, 0.0, RANGES["live_lowshelf.q"].default)


def default_highshelf() -> ShelfBand:
    return ShelfBand(RANGES["live_highshelf.freq"].default, 0.0, RANGES["live_highshelf.q"].default)


@dataclass
class State:
    """Everything K1000 owns about the sound, persisted to state.json.

    Logical outputs (index of `out_trim_db`): 0 = Sub L, 1 = Sub R, 2 = Top L, 3 = Top R.
    `muted` is the Main fader mute; `balance_db` b drives Aux1 (subs) = +b/2, Aux2 (tops) = -b/2.
    Mutable on purpose (the controller edits in place); take `copy()` before editing when you
    need the old state for `dspconfig.diff_ops`.
    """

    master_db: float = -20.0
    muted: bool = False
    balance_db: float = 0.0
    input_trim_db: float = 0.0
    mono_subs: bool = False
    sub_inverted: bool = False
    top_inverted: bool = False
    sub_delay_ms: float = 0.0
    top_delay_ms: float = 0.0
    lowcut_on: bool = True
    lowcut_hz: float = 100.0
    sub_lowpass_on: bool = False          # USW-1P has its own low-pass
    sub_lowpass_hz: float = 120.0
    sys_peq: list[PeqBand] = field(default_factory=default_sys_peq)
    live_peq: list[PeqBand] = field(default_factory=default_live_peq)
    live_lowshelf: ShelfBand = field(default_factory=default_lowshelf)
    live_highshelf: ShelfBand = field(default_factory=default_highshelf)
    out_trim_db: list[float] = field(default_factory=lambda: [0.0] * N_OUT)
    profile: str = "dev"
    dev_audition: str = "sum"
    # SETUP > Spectrum (D-009): the SHOW page's program spectrum. UI only: the engine config
    # never depends on it (dspconfig ignores it), and a v0 build ignores the key.
    spectrum_on: bool = True

    # ----------------------------------------------------------------- copies / clamping
    def copy(self) -> "State":
        return copy.deepcopy(self)

    def clamped(self) -> "State":
        """A copy with every field coerced into RANGES/CHOICES (what the engine may receive)."""
        return State.from_dict(self.to_dict())

    def live_reset(self) -> "State":
        """A copy with the live EQ layer (4 PEQ + 2 shelves) back to defaults: SETUP › LIVE EQ › Reset live EQ."""
        s = self.copy()
        s.live_peq = default_live_peq()
        s.live_lowshelf = default_lowshelf()
        s.live_highshelf = default_highshelf()
        return s

    # ----------------------------------------------------------------- dict round trip
    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"schema": SCHEMA_VERSION}
        for f in fields(self):
            v = getattr(self, f.name)
            if isinstance(v, list):
                d[f.name] = [_band_dict(b) if isinstance(b, (PeqBand, ShelfBand)) else b for b in v]
            elif isinstance(v, (PeqBand, ShelfBand)):
                d[f.name] = _band_dict(v)
            else:
                d[f.name] = v
        return d

    @classmethod
    def from_dict(cls, data: Any) -> "State":
        """Tolerant parse: unknown keys ignored, missing/invalid values -> defaults, all clamped.

        Old or partial files (fewer bands, missing fields) are upgraded in place, so a state
        saved by an earlier build never blocks boot.
        """
        if not isinstance(data, dict):
            data = {}
        s = cls()
        for name, rng in RANGES.items():
            if "." in name or name == "out_trim_db":
                continue
            setattr(s, name, rng.clamp(data.get(name), getattr(s, name)))
        for name in BOOL_FIELDS:
            setattr(s, name, _as_bool(data.get(name), getattr(s, name)))
        for name, choices in CHOICES.items():
            v = data.get(name)
            if isinstance(v, str) and v in choices:
                setattr(s, name, v)
        s.sys_peq = _bands(data.get("sys_peq"), default_sys_peq(), "sys_peq", PeqBand)
        s.live_peq = _bands(data.get("live_peq"), default_live_peq(), "live_peq", PeqBand)
        s.live_lowshelf = _band(data.get("live_lowshelf"), default_lowshelf(), "live_lowshelf", ShelfBand)
        s.live_highshelf = _band(data.get("live_highshelf"), default_highshelf(), "live_highshelf", ShelfBand)
        trims = data.get("out_trim_db")
        trims = trims if isinstance(trims, list) else []
        rng = RANGES["out_trim_db"]
        s.out_trim_db = [rng.clamp(trims[i] if i < len(trims) else None) for i in range(N_OUT)]
        return s

    # ----------------------------------------------------------------- path access
    def get(self, path: str) -> Any:
        """Read a field by path, e.g. "master_db", "live_peq.2.gain", "out_trim_db.1"
        ("align_delay_ms" = sub_delay_ms - top_delay_ms, computed)."""
        if path == ALIGN_DELAY:
            return align_delay_ms(self)
        obj: Any = self
        try:
            for part in path.split("."):
                obj = obj[int(part)] if part.isdigit() else getattr(obj, part)
        except (AttributeError, IndexError, TypeError) as e:
            raise KeyError(path) from e
        return obj

    def set(self, path: str, value: Any) -> Any:
        """Write a field by path, clamped/validated; returns the value actually stored.

        Raises KeyError/ValueError for an unknown path or an illegal choice, so a UI bug is loud
        instead of silently writing garbage. "align_delay_ms" writes both stored delays.
        """
        if path == ALIGN_DELAY:
            v = VIRTUAL_RANGES[ALIGN_DELAY].clamp(value, align_delay_ms(self))
            self.sub_delay_ms, self.top_delay_ms = split_align_delay(v)
            return align_delay_ms(self)
        parts = path.split(".")
        parent: Any = self.get(".".join(parts[:-1])) if len(parts) > 1 else self
        last = parts[-1]
        current = self.get(path)
        rng = range_for(path)
        if rng is not None:
            new: Any = rng.clamp(value, current)
        elif path in BOOL_FIELDS:
            new = _as_bool(value, current)
        elif path in CHOICES:
            if value not in CHOICES[path]:
                raise ValueError(f"{path}: {value!r} not in {CHOICES[path]}")
            new = value
        else:
            raise KeyError(path)
        if last.isdigit():
            parent[int(last)] = new
        else:
            setattr(parent, last, new)
        return new

    def nudge(self, path: str, detents: int | float, fine: bool = False) -> float:
        """Move a numeric field by encoder detents (log fields multiply); returns the new value."""
        rng = range_for(path)
        if rng is None:
            raise KeyError(f"{path} is not numeric")
        if path == ALIGN_DELAY:
            return self.set(path, nudge_align_delay(self.get(path), detents, fine))
        return self.set(path, rng.nudge(self.get(path), detents, fine))


def align_delay_ms(s: Any) -> float:
    """ALIGN's signed delay: sub_delay_ms - top_delay_ms (> 0 = subs later, < 0 = tops later)."""
    sub = RANGES["sub_delay_ms"].clamp(getattr(s, "sub_delay_ms", 0.0))
    top = RANGES["top_delay_ms"].clamp(getattr(s, "top_delay_ms", 0.0))
    return round(sub - top, 6) + 0.0          # + 0.0: never a "-0.0" on screen or in JSON


def split_align_delay(v: Any) -> tuple[float, float]:
    """(sub_delay_ms, top_delay_ms) for a signed delay: only the delayed side is non-zero."""
    v = VIRTUAL_RANGES[ALIGN_DELAY].clamp(v)
    return (v if v > 0 else 0.0), (-v if v < 0 else 0.0)


def nudge_align_delay(value: Any, detents: int | float, fine: bool = False) -> float:
    """The signed delay moved by `detents` (0.1 ms, fine = one sample), stopping at exactly 0
    when the move would cross it: the operator feels the zero (ALIGNED) before the other side."""
    rng = VIRTUAL_RANGES[ALIGN_DELAY]
    v = rng.clamp(value)
    new = rng.nudge(v, detents, fine)
    if (v > 0 > new) or (v < 0 < new):
        return 0.0
    return new + 0.0


def delay_samples(ms: Any) -> int:
    """Whole samples the engine uses for a delay of `ms` (subsample off): round half up, >= 0."""
    try:
        v = float(ms)
    except (TypeError, ValueError, OverflowError):
        return 0
    if not math.isfinite(v) or v <= 0.0:
        return 0
    return int(math.floor(v * SAMPLERATE / 1000.0 + 0.5))


def range_for(path: str) -> Range | None:
    """RANGES entry for a field path ("live_peq.2.gain" -> RANGES["live_peq.gain"]); the
    computed "align_delay_ms" has its own (VIRTUAL_RANGES)."""
    if path in VIRTUAL_RANGES:
        return VIRTUAL_RANGES[path]
    key = ".".join(p for p in path.split(".") if not p.isdigit())
    return RANGES.get(key)


# --------------------------------------------------------------------------- helpers
def _band_dict(b: PeqBand | ShelfBand) -> dict[str, float]:
    return {"freq": b.freq, "gain": b.gain, "q": b.q}


def _as_bool(v: Any, fallback: bool) -> bool:
    return v if isinstance(v, bool) else bool(fallback)


def _band(raw: Any, default: Any, key: str, cls: type) -> Any:
    raw = raw if isinstance(raw, dict) else {}
    return cls(
        freq=RANGES[f"{key}.freq"].clamp(raw.get("freq"), default.freq),
        gain=RANGES[f"{key}.gain"].clamp(raw.get("gain"), default.gain),
        q=RANGES[f"{key}.q"].clamp(raw.get("q"), default.q),
    )


def _bands(raw: Any, defaults: list[Any], key: str, cls: type) -> list[Any]:
    raw = raw if isinstance(raw, list) else []
    return [_band(raw[i] if i < len(raw) else None, d, key, cls) for i, d in enumerate(defaults)]


# --------------------------------------------------------------------------- persistence
def atomic_write(path: str | os.PathLike[str], data: str | bytes, mode: int = 0o644) -> None:
    """Write a file so that a power cut leaves either the old or the new content, never half.

    tmp file in the same directory -> fsync -> rename over the target -> fsync the directory
    (the rename itself is only durable once the directory entry is on disk).
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = data.encode("utf-8") if isinstance(data, str) else data
    fd, tmp = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=p.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dfd = os.open(p.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def save_state_atomic(path: str | os.PathLike[str], state: State) -> None:
    """Persist `state` (clamped) as JSON, atomically."""
    text = json.dumps(state.clamped().to_dict(), indent=2) + "\n"
    atomic_write(path, text)


def load_state(path: str | os.PathLike[str]) -> State:
    """Load state.json; never raises.

    Missing -> defaults. Unreadable/corrupt/not-an-object -> defaults, and the file is copied to
    `<path>.bad` so the evidence survives the next save. Old/partial -> upgraded field by field.
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        log.info("no state at %s, using defaults", p)
        return State()
    except Exception as e:  # noqa: BLE001 - OSError, UnicodeDecodeError, MemoryError...
        log.warning("cannot read state %s (%s), using defaults", p, e)
        _keep_bad(p)
        return State()
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("state is not a JSON object")
        schema = data.get("schema")
        if schema != SCHEMA_VERSION:
            log.info("state schema %r != %d, upgrading tolerantly", schema, SCHEMA_VERSION)
        return State.from_dict(data)
    except Exception as e:  # noqa: BLE001 - boot must never crash-loop on a bad file
        # ValueError (bad JSON), RecursionError (deeply nested), OverflowError, anything else a
        # hostile/corrupt file can provoke: defaults + keep the evidence.
        log.warning("corrupt state %s (%s: %s), using defaults", p, type(e).__name__, e)
        _keep_bad(p)
        return State()


def _keep_bad(p: Path) -> None:
    try:
        shutil.copyfile(p, p.with_name(p.name + ".bad"))
    except OSError as e:
        log.warning("could not keep a .bad copy of %s: %s", p, e)
