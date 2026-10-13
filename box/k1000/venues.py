# SPDX-License-Identifier: GPL-3.0-or-later
"""Venue snapshots: 8 named slots of the values that depend on the ROOM, recalled at a venue that
comes back (docs/12-venue-snapshots.md).

What a slot holds (VENUE_KEYS): the live EQ (4 PEQ + 2 shelves), the system PEQ (8 bands), sub
and top delay, sub and top polarity, the Lo-Cut (on/off and frequency), the sub/top balance and
mono subs. What it never holds: master, mute, input trim, output trims, profile, devices, the sub
low-pass and the spectrum switch. Why: a recall must never move MASTER (it is set by ear at every
gig, against that day's source and crowd, and a wrong master recalled at full is the one mistake a
PA cannot take back), never mute or unmute, and never touch what belongs to the rig rather than
the room (trims calibrate the boxes, the profile picks the interface). A recall CAN still make a
group or a band louder (the balance, an EQ boost, a notch taken away, the Lo-Cut switched off):
`describe_changes` says so on the lines concerned, and the confirm shows them first.

Why the store is shaped like this:
- **Same clamps as state.json.** A slot's values go through `State.from_dict` on load, so a
  hand-edited or corrupt venues.json can only ever produce values the knobs could have set.
- **Never a crash, never a half file.** Reads fall back to empty slots (with a journal warning and
  a `.bad` copy), writes are atomic (`state.atomic_write`). A slot that cannot be read is empty;
  the other slots survive.
- **Names are for a 480x320 screen read at arm's length:** at most NAME_MAX characters, upper case,
  letters, digits, space and a few marks the box font draws. Accents are folded (E for É), so a
  name typed on a phone looks the same on the box.
"""
from __future__ import annotations

import json
import logging
import math
import shutil
import threading
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .state import PeqBand, ShelfBand, State, atomic_write

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
N_SLOTS = 8
NAME_MAX = 16
# Characters a name may hold (after folding accents and upper-casing). Anything else becomes a
# space. The box's letter picker steps through NAME_PICKER (space first: a blank cell).
NAME_PICKER = " ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-.'&/+#()"
NAME_CHARS = frozenset(NAME_PICKER)
RAW_NAME_MAX = 64          # longer input is refused, not cut (a paste of the wrong thing)

# The stored keys, in display order. Bands are stored whole (freq, gain, q).
SCALAR_KEYS: tuple[str, ...] = ("sub_delay_ms", "top_delay_ms", "sub_inverted", "top_inverted",
                                "lowcut_on", "lowcut_hz", "balance_db", "mono_subs")
BAND_KEYS: tuple[str, ...] = ("live_peq", "live_lowshelf", "live_highshelf", "sys_peq")
VENUE_KEYS: tuple[str, ...] = BAND_KEYS + SCALAR_KEYS
# Never stored, never recalled (documented, and asserted by the tests).
NEVER_KEYS: tuple[str, ...] = ("master_db", "muted", "input_trim_db", "out_trim_db", "profile",
                               "dev_audition", "sub_lowpass_on", "sub_lowpass_hz", "spectrum_on")
# mono_subs changes the mixers: a pipeline rebuild. A recall applies it only while muted.
REBUILD_KEYS: frozenset[str] = frozenset({"mono_subs"})
# How describe_changes words a line that can make a group or a band LOUDER. The box (pages.py)
# draws such lines orange and the phone (app.js) marks them: keep the three in step.
LOUDER_MARKS: tuple[str, ...] = (" louder)", "(subs +", "(tops +", "Lo-Cut ON → OFF")


# --------------------------------------------------------------------------- values
def values_of(state: State) -> dict[str, Any]:
    """The venue values of `state` (plain JSON-able dicts, the state's own rounding)."""
    d = state.to_dict()
    return {k: d[k] for k in VENUE_KEYS}


def clamp_values(raw: Any) -> dict[str, Any]:
    """Raw slot values -> clamped venue values, by the state.json rules (`State.from_dict`):
    out of range clamped, unreadable or missing -> the default."""
    src = raw if isinstance(raw, dict) else {}
    return values_of(State.from_dict({k: src[k] for k in VENUE_KEYS if k in src}))


def apply_values(state: State, values: dict[str, Any], *, rebuild: bool = True) -> State:
    """A copy of `state` with the venue `values` (already clamped) set. Nothing else changes:
    master, mute, trims, profile... stay as they are. `rebuild=False` keeps the state's own mono
    subs (a pipeline change)."""
    s = state.copy()
    for k in SCALAR_KEYS:
        if k in REBUILD_KEYS and not rebuild:
            continue
        setattr(s, k, values[k])
    s.live_peq = [PeqBand(**b) for b in values["live_peq"]]
    s.sys_peq = [PeqBand(**b) for b in values["sys_peq"]]
    s.live_lowshelf = ShelfBand(**values["live_lowshelf"])
    s.live_highshelf = ShelfBand(**values["live_highshelf"])
    return s


def sanitize_name(raw: Any) -> str | None:
    """A name as the box shows it: accents folded, upper case, NAME_CHARS only (others become a
    space), spaces collapsed, at most NAME_MAX characters. None when nothing is left (or the input
    is not text, or longer than RAW_NAME_MAX)."""
    if not isinstance(raw, str) or len(raw) > RAW_NAME_MAX:
        return None
    text = unicodedata.normalize("NFKD", raw)
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).upper()
    text = unicodedata.normalize("NFKD", text)       # upper() can make new composed letters
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = "".join(ch if ch in NAME_CHARS else " " for ch in text)
    text = " ".join(text.split())[:NAME_MAX].strip()
    return text or None


def default_name(slot: int) -> str:
    return f"VENUE {slot + 1}"


# --------------------------------------------------------------------------- what changes
def _audible(b: dict[str, Any]) -> tuple[float, ...]:
    """A band as the sound hears it: a 0 dB peaking/shelf biquad is unity wherever it sits."""
    g = float(b["gain"])
    return (g, float(b["freq"]), float(b["q"])) if g != 0.0 else (0.0,)


def _bands(v: dict[str, Any], key: str) -> list[dict[str, Any]]:
    x = v[key]
    return list(x) if isinstance(x, list) else [x]


def audible_key(values: dict[str, Any]) -> tuple[Any, ...]:
    """The venue values as the sound hears them, for "is this still that venue?" (the VENUE pill,
    the NOW mark, the recall undo): every band by `_audible` (a 0 dB band is unity wherever its
    frequency and Q sit: the notch helper's B4, a FREQ/Q turn on a flat band change nothing), the
    Lo-Cut frequency only while the Lo-Cut is on, every other value exactly. The same rule as the
    reset undo (control._audible_live) and as `describe_changes`."""
    out: list[Any] = [tuple(_audible(b) for b in _bands(values, k)) for k in BAND_KEYS]
    for k in SCALAR_KEYS:
        if k == "lowcut_hz":
            out.append(float(values[k]) if bool(values["lowcut_on"]) else None)
        elif k in ("sub_inverted", "top_inverted", "lowcut_on", "mono_subs"):
            out.append(bool(values[k]))
        else:
            out.append(float(values[k]))
    return tuple(out)


def _fmt_db(v: float) -> str:
    return "0.0" if abs(v) < 0.05 else f"{v:+.1f}"


def _fmt_hz(v: float) -> str:
    if v >= 10000:
        return f"{v / 1000:.1f}k"
    if v >= 1000:
        return f"{v / 1000:.2f}k"
    return f"{v:.0f}"


def _fmt_up(v: float) -> str:
    """A rise in dB, short: +3, +0.25, +4.5."""
    return "+" + f"{v:.2f}".rstrip("0").rstrip(".")


def changed_bands(now: dict[str, Any], slot: dict[str, Any], keys: tuple[str, ...]) -> int:
    """How many bands of `keys` sound different between `now` and `slot`."""
    return band_changes(now, slot, keys)[0]


def band_changes(now: dict[str, Any], slot: dict[str, Any], keys: tuple[str, ...]) -> tuple[int, int]:
    """(bands that sound different, of those the ones that make it LOUDER somewhere): in place,
    a gain that rises (a boost added, a cut made shallower or taken away: a notch that held
    feedback is gone); moved elsewhere, a boost where it lands or a cut taken from where it was."""
    n = up = 0
    for k in keys:
        for a, b in zip(_bands(now, k), _bands(slot, k)):
            if _audible(a) == _audible(b):
                continue
            n += 1
            if _louder(a, b):
                up += 1
    return n, up


def _louder(a: dict[str, Any], b: dict[str, Any]) -> bool:
    ga, gb = float(a["gain"]), float(b["gain"])
    if ga == 0.0:
        return gb > 0.0
    if gb == 0.0:
        return ga < 0.0
    if (float(a["freq"]), float(a["q"])) != (float(b["freq"]), float(b["q"])):
        return gb > 0.0 or ga < 0.0
    return gb > ga


def _eq_line(label: str, n: int, up: int) -> str:
    return f"{label}: {n} band{'s' if n != 1 else ''}" + (f" ({up} louder)" if up else "")


def describe_changes(now: dict[str, Any], slot: dict[str, Any], *, rebuild_ok: bool = True,
                     compact: bool = False) -> list[str]:
    """What a recall of `slot` changes, against the current venue values `now`, as short lines for
    the phone's confirm and the box (SETUP > Venues): "Sub delay 0.00 → 2.90 ms", "Lo-Cut 112 →
    90 Hz", "Balance 0.0 → +6.0 dB (subs +3)", "Live EQ: 3 bands (1 louder)". EQ bands count only
    when they SOUND different (a band at 0 dB moved elsewhere changes nothing). What can make a
    group or a band louder says so: the balance names the group that rises, an EQ line counts its
    bands that boost or lose a cut ("louder"), "Lo-Cut ON → OFF". `rebuild_ok=False` (unmuted):
    mono subs is said, but as not applied ("· MUTE FIRST"). Empty list = nothing audible changes.

    `compact=True` (the box's half screen, at most 7 lines): the MUTE FIRST line first, the two
    polarities in one line, the live and system EQ in one line."""
    out: list[str] = []
    first: list[str] = []
    for key, label in (("sub_delay_ms", "Sub delay"), ("top_delay_ms", "Top delay")):
        a, b = float(now[key]), float(slot[key])
        if a != b:
            out.append(f"{label} {a:.2f} → {b:.2f} ms")
    pol = [(name, bool(slot[key])) for key, name in (("sub_inverted", "sub"), ("top_inverted", "top"))
           if bool(now[key]) != bool(slot[key])]
    if compact and len(pol) == 2:
        out.append("Polarity → " + " · ".join(f"{n} {'INV' if inv else 'normal'}" for n, inv in pol))
    else:
        for name, inv in pol:
            out.append(f"{name.capitalize()} polarity {'normal' if inv else 'INVERTED'} → "
                       f"{'INVERTED' if inv else 'normal'}")
    lo_a, lo_b = bool(now["lowcut_on"]), bool(slot["lowcut_on"])
    fa, fb = float(now["lowcut_hz"]), float(slot["lowcut_hz"])
    if lo_a and lo_b and fa != fb:
        out.append(f"Lo-Cut {_fmt_hz(fa)} → {_fmt_hz(fb)} Hz")
    elif lo_a and not lo_b:
        out.append("Lo-Cut ON → OFF")
    elif lo_b and not lo_a:
        out.append(f"Lo-Cut OFF → ON {_fmt_hz(fb)} Hz")
    a, b = float(now["balance_db"]), float(slot["balance_db"])
    if a != b:
        # the Aux faders carry +b/2 (subs) and -b/2 (tops): one group always rises by |b-a|/2
        group = "subs" if b > a else "tops"
        out.append(f"Balance {_fmt_db(a)} → {_fmt_db(b)} dB ({group} {_fmt_up(abs(b - a) / 2)})")
    if bool(now["mono_subs"]) != bool(slot["mono_subs"]):
        line = f"Mono subs {'ON' if now['mono_subs'] else 'off'} → {'ON' if slot['mono_subs'] else 'off'}"
        (first if compact and not rebuild_ok else out).append(line if rebuild_ok else line + " · MUTE FIRST")
    n_live, up_live = band_changes(now, slot, ("live_peq", "live_lowshelf", "live_highshelf"))
    n_sys, up_sys = band_changes(now, slot, ("sys_peq",))
    if compact and n_live and n_sys:
        up = up_live + up_sys
        out.append(f"EQ: live {n_live} · system {n_sys}" + (f" ({up} louder)" if up else ""))
    else:
        if n_live:
            out.append(_eq_line("Live EQ", n_live, up_live))
        if n_sys:
            out.append(_eq_line("System EQ", n_sys, up_sys))
    return first + out


# --------------------------------------------------------------------------- the store
@dataclass(frozen=True)
class Venue:
    """One slot: its name (sanitised), when it was saved (Unix time, the box's clock; 0 =
    unknown) and its clamped venue values."""

    name: str
    saved_at: float
    values: dict[str, Any] = field(compare=True)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "saved_at": self.saved_at, "values": self.values}


def _saved_at(v: Any) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return 0.0
    try:
        f = float(v)
    except OverflowError:
        return 0.0
    return round(f, 3) if math.isfinite(f) and 0.0 <= f < 1e11 else 0.0


def venue_from_dict(raw: Any, slot: int) -> Venue | None:
    """One slot of venues.json -> a Venue (clamped, name sanitised), or None (empty/unreadable)."""
    if not isinstance(raw, dict) or not isinstance(raw.get("values"), dict):
        return None
    name = sanitize_name(raw.get("name")) or default_name(slot)
    return Venue(name, _saved_at(raw.get("saved_at")), clamp_values(raw["values"]))


class VenueStore:
    """The 8 slots in memory, persisted to `path` (k1000.yaml `paths.venues`). Reads never raise;
    `save()` never raises either (it returns False and logs)."""

    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path) if path else None
        self.slots: list[Venue | None] = [None] * N_SLOTS
        self.load_error = ""
        self._lock = threading.Lock()        # one writer at a time: the newest content wins
        self.load()

    # -------------------------------------------------------------- reading
    def load(self) -> None:
        """Read venues.json. Missing = empty slots (first use). Unreadable / not JSON / not an
        object = empty slots + a warning, and the file kept as `<path>.bad`."""
        self.slots = [None] * N_SLOTS
        self.load_error = ""
        if self.path is None:
            return
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            log.info("no venues at %s: 8 empty slots", self.path)
            return
        except Exception as e:  # noqa: BLE001 - OSError, UnicodeDecodeError...
            self._bad(f"cannot read ({type(e).__name__}: {e})")
            return
        try:
            data = json.loads(text)
            if not isinstance(data, dict) or not isinstance(data.get("slots"), list):
                raise ValueError("not a venues object")
        except Exception as e:  # noqa: BLE001 - ValueError, RecursionError, anything a torn file does
            self._bad(f"corrupt ({type(e).__name__}: {e})")
            return
        if data.get("schema") != SCHEMA_VERSION:
            log.info("venues schema %r != %d, reading tolerantly", data.get("schema"), SCHEMA_VERSION)
        raw = data["slots"]
        if len(raw) > N_SLOTS:
            log.warning("venues: %d slots in %s, only the first %d are kept", len(raw), self.path,
                        N_SLOTS)
        for i in range(N_SLOTS):
            item = raw[i] if i < len(raw) else None
            try:
                v = venue_from_dict(item, i)
            except Exception as e:  # noqa: BLE001 - one bad slot must not take the others
                log.warning("venues: slot %d unreadable (%s: %s): left empty", i + 1,
                            type(e).__name__, e)
                v = None
            if v is None and item is not None:
                log.warning("venues: slot %d in %s is not a venue: left empty", i + 1, self.path)
            self.slots[i] = v

    def _bad(self, why: str) -> None:
        assert self.path is not None
        self.load_error = why
        log.warning("venues file %s %s: 8 empty slots (the file is kept as %s.bad)", self.path,
                    why, self.path.name)
        try:
            shutil.copyfile(self.path, self.path.with_name(self.path.name + ".bad"))
        except OSError as e:
            log.warning("could not keep a .bad copy of %s: %s", self.path, e)

    def get(self, slot: int) -> Venue | None:
        return self.slots[slot] if isinstance(slot, int) and 0 <= slot < N_SLOTS else None

    def find(self, values: dict[str, Any]) -> int | None:
        """The first slot that sounds exactly like `values` (`audible_key`), or None."""
        key = audible_key(values)
        return next((i for i, v in enumerate(self.slots)
                     if v is not None and audible_key(v.values) == key), None)

    # -------------------------------------------------------------- writing
    def put(self, slot: int, venue: Venue) -> None:
        if not (isinstance(slot, int) and 0 <= slot < N_SLOTS):
            raise IndexError(slot)
        slots = list(self.slots)
        slots[slot] = venue
        self.slots = slots                   # one assignment: a writer thread sees old or new

    def to_json(self) -> str:
        slots = list(self.slots)
        return json.dumps({"schema": SCHEMA_VERSION,
                           "slots": [v.to_dict() if v is not None else None for v in slots]},
                          indent=1) + "\n"

    def save(self) -> bool:
        """Write venues.json atomically (the content of the moment the lock is taken, so of two
        writers the later always writes the newer slots). Never raises."""
        if self.path is None:
            return True
        with self._lock:
            try:
                atomic_write(self.path, self.to_json())
            except Exception as e:  # noqa: BLE001 - OSError (SD card), anything: never a crash
                log.error("cannot write %s: %s", self.path, e)
                return False
        return True
