# SPDX-License-Identifier: GPL-3.0-or-later
"""Friday tune import (docs/06-phone.md §11): a filter list pasted from REW, Open Sound Meter or
EqualizerAPO -> the 8 system PEQ bands, previewed, applied through the Controller, undone.

Why it is shaped like this:

- **The box decides, never the page.** The laptop's page sends the pasted TEXT; the parse, the
  clamps and the plan happen here, again at Apply (the page's preview is never trusted), and
  the result goes through `Controller.set_system_eq`: the same clamps (`state.RANGES`), the
  same click-safe parameter writes and the same saved state as every other edit.
- **Peaking only.** The system EQ is 8 Biquad Peaking filters (dspconfig: parameter writes,
  never a pipeline change). Every other type (shelves, pass filters, notches, all-pass,
  GraphicEQ) is listed as skipped, never approximated. A filter at 0 dB (REW writes "-0.00")
  does nothing and is skipped too.
- **More than 8 is refused, whole.** Dropping the 9th filter would apply an EQ nobody designed;
  the message says to re-run the EQ with 8 filters at most.
- **Clamped and said so.** A value beyond the system range (20 Hz..20 kHz, -20..+10 dB,
  Q 0.3..16) is clamped like a knob would be, and each clamp is listed.
- **Lenient about the text, strict about the meaning.** The common line is
  `Filter 1: ON PK Fc 125 Hz Gain -4.0 dB Q 5.00` (REW's "Generic" equaliser, EqualizerAPO,
  AutoEQ); REW's older exports (`PA`, `Fc 129.1Hz ( 125 +2 )`, `BW/60 4.0`), EqualizerAPO's
  `BW Oct 0.5`, `kHz`, decimal commas (a French REW) and the Unicode minus of a web page are read
  too. Open Sound Meter has no filter export we know of: its filters are typed in the same form,
  or as a free line `125 Hz -4 dB Q 5`. Anything that is not a filter (REW's header, notes,
  blank lines, comments) is ignored.
- **Free lines only in a list without Filter lines.** A paste that has any `Filter N:` line is a
  filter export: its other lines are header, notes or comments, never filters, even when they
  carry a frequency, a gain and a Q (REW's `Notes:` line is free text: "Notes:boost 50 Hz +4 dB
  Q 1 for the DJ" must not become a boost on band 1 that pushes the real filters down a band,
  nor count toward the 8). Such a line is listed in the notes as ignored. REW's header keys
  (`Notes:`, `Equaliser:`, `Dated:`, `Room EQ`, `Filter Settings file`...) are never read as
  filters, in either kind of paste. The free-line form is for a hand-typed list only.
- **A width in octaves becomes the Q of the SAME digital filter** EqualizerAPO and CamillaDSP's
  Peaking/Bandwidth compute (alpha = sin w0 * sinh(ln2/2 * BW * w0 / sin w0)), at the engine
  rate: 1/(2 Q) = sinh(ln2/2 * BW * w0 / sin w0). Below a few kHz it is the textbook relation.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .state import (DEFAULT_SYS_PEQ_FREQS, N_SYS_PEQ, RANGES, SAMPLERATE, PeqBand, atomic_write,
                    default_sys_peq)

MAX_TEXT = 16000            # characters of pasted text the box reads (a 20-filter REW file is ~1.5 k)
MAX_LINES = 400
PEAKING_TYPES = frozenset({"PK", "PEQ", "PA", "MODAL", "PEAK", "PEAKING", "BELL"})
TYPE_NAMES = {
    "LS": "low shelf", "HS": "high shelf", "LSC": "low shelf", "HSC": "high shelf",
    "LSQ": "low shelf", "HSQ": "high shelf", "LP": "low-pass", "HP": "high-pass",
    "LPQ": "low-pass", "HPQ": "high-pass", "BP": "band-pass", "NO": "notch", "AP": "all-pass",
    "IIR": "IIR", "BWLP": "low-pass", "BWHP": "high-pass", "LRLP": "low-pass", "LRHP": "high-pass",
    "LS6": "low shelf", "HS6": "high shelf", "LS12": "low shelf", "HS12": "high shelf",
}
_NUM = r"[-+]?\d+(?:[.,]\d+)?"
_FILTER = re.compile(r"^\s*filter\s*(?P<num>\d+)?\s*:\s*(?P<state>on|off)\b(?P<rest>.*)$", re.I)
_TYPE = re.compile(r"^\s*(?P<type>[A-Za-z][A-Za-z0-9]*)")
_FC = re.compile(r"\bFc\s*[:=]?\s*(?P<v>" + _NUM + r")\s*(?P<u>k?Hz)?", re.I)
_GAIN = re.compile(r"\bGain\s*[:=]?\s*(?P<v>" + _NUM + r")\s*(?:dB)?", re.I)
_Q = re.compile(r"(?<![A-Za-z0-9/])Q\s*[:=]?\s*(?P<v>" + _NUM + r")", re.I)
_BW_OCT = re.compile(r"\bBW\s*Oct\s*[:=]?\s*(?P<v>" + _NUM + r")", re.I)
_BW_60 = re.compile(r"\bBW\s*/\s*60\s*[:=]?\s*(?P<v>" + _NUM + r")", re.I)
# A free line (Open Sound Meter, by hand): "125 Hz -4 dB Q 5", "PEQ 2: 2.5 kHz, -3,5 dB, Q 4"
_FREE_HZ = re.compile(r"(?<![\w.,])(?P<v>\d+(?:[.,]\d+)?)\s*(?P<u>k?Hz)\b", re.I)
_FREE_DB = re.compile(r"(?<![\w.,])(?P<v>[-+]?\d+(?:[.,]\d+)?)\s*dB\b", re.I)
_PREAMP = re.compile(r"^\s*preamp\s*:\s*(?P<v>" + _NUM + r")\s*dB", re.I)
# REW's export header (and EqualizerAPO's include/device lines): never a filter, whatever free
# text follows the key ("Notes:" is the user's own note, "Equaliser:" the target device name).
_HEADER = re.compile(r"^\s*(?:notes|equali[sz]er|dated|target\s+level|measurement|device|include"
                     r"|channel)\s*:|^\s*(?:room\s+eq\b|filter\s+settings\s+file\b)", re.I)
_SHOWN = 60                 # characters of an ignored line quoted in the notes


@dataclass(frozen=True)
class Filter:
    """One peaking filter read from the text, already clamped to the system ranges."""
    line: int               # 1-based line number in the pasted text
    label: str              # "Filter 3" or "line 7"
    freq: float
    gain: float
    q: float


@dataclass
class Import:
    """The result of reading a pasted filter list. `ok` = something to apply, nothing refused."""
    filters: list[Filter] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.filters)

    def bands(self) -> list[PeqBand]:
        """The 8 system bands this import sets: its filters in order on bands 1..n, the others
        flat (0 dB) at their default place. Only meaningful when `ok`."""
        return plan_bands(self.filters)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "error": self.error, "skipped": list(self.skipped),
                "notes": list(self.notes),
                "filters": [{"line": f.line, "label": f.label, "freq": f.freq, "gain": f.gain,
                             "q": f.q} for f in self.filters]}


def plan_bands(filters: Sequence[Filter]) -> list[PeqBand]:
    out = default_sys_peq()
    for i, f in enumerate(filters[:N_SYS_PEQ]):
        out[i] = PeqBand(f.freq, f.gain, f.q)
    return out


def q_for_bandwidth(octaves: float, freq: float, fs: float = SAMPLERATE) -> float:
    """Q of the Peaking filter equal to a Peaking/Bandwidth filter `octaves` wide at `freq`
    (EqualizerAPO's and CamillaDSP's bandwidth form, digital): 1/(2Q) = sinh(ln2/2 BW w0/sin w0)."""
    w = 2.0 * math.pi * min(max(freq, 1.0), fs * 0.49) / fs
    return 1.0 / (2.0 * math.sinh(math.log(2.0) / 2.0 * octaves * w / math.sin(w)))


def _clean(text: str) -> str:
    """Unicode minus/dashes -> '-', odd spaces -> ' ' (text pasted from a web page or a PDF)."""
    return (text.replace("−", "-").replace("–", "-").replace("‒", "-")
            .replace(" ", " ").replace(" ", " ").replace(" ", " ").replace("\t", " "))


def _num(tok: str) -> float:
    """'125', '-4.0', '4,000' (a decimal comma: a French REW) -> float."""
    return float(tok.replace(",", ".")) if "." not in tok else float(tok.replace(",", ""))


def _freq(tok: str, unit: str | None, label: str, notes: list[str]) -> float:
    if re.fullmatch(r"\d{1,2},\d{3}", tok) and (unit or "").lower() != "khz":
        # "2,500 Hz": a thousands separator (a 2.5 Hz filter is not what anyone meant)
        v = float(tok.replace(",", ""))
        notes.append(f"{label}: Fc {tok} read as {v:g} Hz")
    else:
        v = _num(tok)
    return v * 1000.0 if (unit or "").lower() == "khz" else v


def _clamped(label: str, what: str, rng_key: str, v: float, notes: list[str],
             fmt: str, unit: str) -> float:
    rng = RANGES[rng_key]
    out = rng.clamp(v)
    if abs(out - round(v, rng.digits)) > 1e-9:
        notes.append(f"{label}: {what} {format(v, fmt)}{unit} -> {format(out, fmt)}{unit} "
                     f"(system range {format(rng.min, fmt)}..{format(rng.max, fmt)}{unit})")
    return out


def _width(rest: str, freq: float, label: str, notes: list[str]) -> float | None:
    """Q from 'Q x', 'BW Oct x' or 'BW/60 x' (None: no width given)."""
    m = _BW_OCT.search(rest)
    if m:
        bw = _num(m.group("v"))
        if bw <= 0:
            return None
        q = q_for_bandwidth(bw, freq)
        notes.append(f"{label}: BW {bw:g} oct = Q {q:.3f}")
        return q
    m = _BW_60.search(rest)
    if m:
        bw = _num(m.group("v")) / 60.0
        if bw <= 0:
            return None
        q = q_for_bandwidth(bw, freq)
        notes.append(f"{label}: BW/60 {m.group('v')} ({bw:.3f} oct) = Q {q:.3f}")
        return q
    m = _Q.search(rest)
    if m:
        q = _num(m.group("v"))
        return q if q > 0 else None
    return None


def _peaking(label: str, line_no: int, rest: str, fc: tuple[str, str | None] | None,
             gain_tok: str | None, res: Import) -> None:
    """Validate one peaking filter's numbers and add it (or say why it is skipped)."""
    if fc is None:
        res.skipped.append(f"{label}: no Fc (frequency): skipped")
        return
    if gain_tok is None:
        res.skipped.append(f"{label}: no Gain: skipped")
        return
    notes: list[str] = []
    try:
        freq = _freq(fc[0], fc[1], label, notes)
        gain = _num(gain_tok)
    except ValueError:
        res.skipped.append(f"{label}: unreadable numbers: skipped")
        return
    if not (math.isfinite(freq) and math.isfinite(gain)) or freq <= 0:
        res.skipped.append(f"{label}: unreadable numbers: skipped")
        return
    try:
        q = _width(rest, freq, label, notes)
    except (ValueError, OverflowError, ZeroDivisionError):
        q = None
    if q is None or not math.isfinite(q):
        res.skipped.append(f"{label}: no Q or bandwidth: skipped")
        return
    if abs(gain) < 0.005:
        res.skipped.append(f"{label}: gain 0 dB, no effect: skipped")
        return
    res.notes.extend(notes)
    freq = _clamped(label, "Fc", "sys_peq.freq", freq, res.notes, "g", " Hz")
    gain = _clamped(label, "gain", "sys_peq.gain", gain, res.notes, "+.1f", " dB")
    q = _clamped(label, "Q", "sys_peq.q", q, res.notes, ".3g", "")
    res.filters.append(Filter(line_no, label, freq, gain, q))


def _looks_like_filter(line: str) -> bool:
    """A frequency (Hz/kHz), a gain (dB) and a Q or a bandwidth somewhere on the line."""
    return bool(_FREE_HZ.search(line) and _FREE_DB.search(line)
                and (_Q.search(line) or _BW_OCT.search(line) or _BW_60.search(line)))


def _quote(line: str) -> str:
    return line if len(line) <= _SHOWN else line[:_SHOWN - 1] + "…"


def parse_filters(text: Any) -> Import:
    """Read a pasted filter list. Never raises: whatever the text, an `Import` that says what
    it found, what it skipped and why, and an `error` when it must be refused."""
    res = Import()
    if not isinstance(text, str):
        res.error = "nothing pasted"
        return res
    if len(text) > MAX_TEXT:
        res.error = f"too long ({len(text)} characters, {MAX_TEXT} at most): paste the filter list only"
        return res
    lines = _clean(text).splitlines()
    if len(lines) > MAX_LINES:
        res.error = f"too many lines ({len(lines)}, {MAX_LINES} at most): paste the filter list only"
        return res
    # A filter export (any "Filter N:" line): its other lines are never filters (see the
    # module docstring). Free lines are read only in a hand-typed list without Filter lines.
    export = any(_FILTER.match(raw.strip()) for raw in lines)
    for n, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith(("#", ";", "//")):
            continue
        if _HEADER.match(line):
            if _looks_like_filter(line):
                res.notes.append(f"line {n}: \"{_quote(line)}\" ignored (a header or notes "
                                 "line, never a filter)")
            continue
        m = _PREAMP.match(line)
        if m:
            res.notes.append(f"line {n}: Preamp {m.group('v')} dB ignored (set the level with "
                             "the master; K1000 has no preamp)")
            continue
        if re.match(r"^\s*graphiceq\s*:", line, re.I):
            res.skipped.append(f"line {n}: GraphicEQ: skipped (peaking filters only)")
            continue
        m = _FILTER.match(line)
        if m:
            label = f"Filter {m.group('num')}" if m.group("num") else f"line {n}"
            rest = m.group("rest")
            t = _TYPE.match(rest)
            ftype = t.group("type").upper() if t else ""
            if ftype in ("NONE", ""):
                continue                       # REW's unused slots: "Filter 13: ON None"
            after = rest[t.end():] if t else rest
            if m.group("state").upper() == "OFF":
                res.skipped.append(f"{label}: {ftype} switched OFF in the list: skipped")
                continue
            if ftype not in PEAKING_TYPES:
                kind = TYPE_NAMES.get(ftype, "not a peaking filter")
                res.skipped.append(f"{label}: {ftype} ({kind}): skipped (peaking filters only)")
                continue
            fc = _FC.search(after)
            g = _GAIN.search(after)
            _peaking(label, n, after, (fc.group("v"), fc.group("u")) if fc else None,
                     g.group("v") if g else None, res)
            continue
        # A free line: a frequency (Hz/kHz), a gain (dB) and a Q or a bandwidth.
        if not _looks_like_filter(line):
            continue                           # a header, a note, a measurement name...
        if export:
            res.notes.append(f"line {n}: \"{_quote(line)}\" ignored (not a \"Filter N:\" line; "
                             "free lines are read only in a list without Filter lines)")
            continue
        hz = _FREE_HZ.search(line)
        db = _FREE_DB.search(line)
        assert hz is not None and db is not None
        label = f"line {n}"
        words = {w.upper() for w in re.findall(r"[A-Za-z]+", line)}
        other = sorted((words & set(TYPE_NAMES)) - {"HZ"})
        if other and not words & PEAKING_TYPES:
            res.skipped.append(f"{label}: {other[0]} ({TYPE_NAMES[other[0]]}): skipped "
                               "(peaking filters only)")
            continue
        _peaking(label, n, line, (hz.group("v"), hz.group("u")), db.group("v"), res)
    if len(res.filters) > N_SYS_PEQ:
        res.error = (f"{len(res.filters)} peaking filters: the system EQ has {N_SYS_PEQ} bands. "
                     f"Nothing applied: re-run the EQ with {N_SYS_PEQ} filters at most "
                     "(REW: EQ window, Filter tasks, set the number of filters)")
    elif not res.filters:
        res.error = ("no peaking filter found: paste the filter list (REW: File, Export, Export "
                     "filter settings as text; EqualizerAPO: the Filter lines)")
    return res


# ------------------------------------------------------------------------------------------
# The preview curve and the Controller side
# ------------------------------------------------------------------------------------------
def band_dicts(bands: Sequence[Any]) -> list[dict[str, float]]:
    """PeqBands (or dicts) -> plain JSON-able dicts, clamped like the state stores them."""
    out = []
    for i in range(N_SYS_PEQ):
        b = bands[i] if i < len(bands) else None
        get = (lambda k, d, b=b: b.get(k, d)) if isinstance(b, dict) else (
            lambda k, d, b=b: getattr(b, k, d))
        out.append({"freq": RANGES["sys_peq.freq"].clamp(get("freq", DEFAULT_SYS_PEQ_FREQS[i])),
                    "gain": RANGES["sys_peq.gain"].clamp(get("gain", 0.0)),
                    "q": RANGES["sys_peq.q"].clamp(get("q", RANGES["sys_peq.q"].default))})
    return out


def curve(bands: Sequence[Any], points: int = 160) -> dict[str, list[float]]:
    """The summed response (dB) of the 8 peaking bands at `points` log-spaced frequencies,
    exactly as the engine computes it (eqgraph: CamillaDSP's biquads at 48 kHz)."""
    from .ui import eqgraph as eg      # numpy + PIL: only when a preview is drawn
    import numpy as np
    f = np.geomspace(eg.F_LO, eg.F_HI, max(8, int(points)))
    bs = [eg.Band(eg.PEAKING, d["freq"], d["gain"], d["q"]) for d in band_dicts(bands)]
    db = eg.curve_db(bs, f)
    return {"f": [round(float(x), 1) for x in f], "db": [round(float(x), 2) for x in db]}




# ------------------------------------------------------------------------------------------
# The desk: preview / apply / undo against the Controller (the /tune page's back end)
# ------------------------------------------------------------------------------------------
class TuneDesk:
    """What the laptop's /tune page does, against the Controller (web.server calls it).

    preview(text): the import read and planned, with both curves; changes nothing.
    apply(text): the same text read AGAIN here (the page's preview is never trusted), applied
        through `Controller.set_system_eq` (clamped, click-safe parameter writes, saved at once).
        The system EQ it replaced becomes the undo.
    undo(): that previous system EQ back, the same way; one level (gone once used, replaced by
        the next apply). It is kept in `undo_file` too, so a k1000 restart in the middle of the
        tune does not lose it.
    Never raises on input: every result is a JSON-able dict with `ok` and, when not ok, `why`.
    """

    def __init__(self, ctl: Any, undo_file: Any = None, *, settle_s: float = 5.0,
                 save_s: float = 3.0) -> None:
        self.ctl = ctl
        self.undo_file = Path(undo_file) if undo_file else None
        self.settle_s = settle_s
        self.save_s = save_s
        self._lock = asyncio.Lock()
        self.undo_bands: list[dict[str, float]] | None = self._load_undo()

    # ------------------------------------------------------------------ views
    def current(self) -> list[dict[str, float]]:
        return band_dicts(self.ctl.system_eq())

    def state(self) -> dict[str, Any]:
        cur = self.current()
        return {"current": cur, "curve_now": curve(cur), "undo": self.undo_bands,
                "ranges": {k: {"min": RANGES[k].min, "max": RANGES[k].max}
                           for k in ("sys_peq.freq", "sys_peq.gain", "sys_peq.q")}}

    def preview(self, text: Any) -> dict[str, Any]:
        imp = parse_filters(text)
        out = imp.to_dict()
        cur = self.current()
        out["current"], out["curve_now"] = cur, curve(cur)
        if imp.ok:
            bands = band_dicts(imp.bands())
            out["bands"], out["curve"] = bands, curve(bands)
        else:
            out["bands"], out["curve"] = None, None
        if not imp.ok:
            out["why"] = imp.error
        return out

    # ------------------------------------------------------------------ changes
    async def apply(self, text: Any) -> dict[str, Any]:
        imp = parse_filters(text)
        if not imp.ok:
            return {**imp.to_dict(), "why": imp.error}
        async with self._lock:
            bands = band_dicts(imp.bands())
            prev = band_dicts(self.ctl.set_system_eq(bands, source="tune import"))
            if prev != self.current():
                self.undo_bands = prev
                await self._save_undo()
            saved = await self._settled()
        return {**imp.to_dict(), "bands": self.current(), "curve": curve(self.current()),
                "undo": self.undo_bands, "saved": saved}

    async def undo(self) -> dict[str, Any]:
        async with self._lock:
            if not self.undo_bands:
                return {"ok": False, "why": "nothing to undo"}
            back, self.undo_bands = self.undo_bands, None
            self.ctl.set_system_eq(back, source="tune undo")
            await self._save_undo()
            saved = await self._settled()
        cur = self.current()
        return {"ok": True, "bands": cur, "curve": curve(cur), "undo": None, "saved": saved}

    async def _settled(self) -> bool:
        """The engine has the new EQ (or is down: nothing to wait for), and it is on the disk."""
        try:
            await self.ctl.settle(timeout=self.settle_s)
        except Exception:   # noqa: BLE001 - a settle problem is not an apply failure
            pass
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.save_s
        while getattr(self.ctl, "unsaved", False) and loop.time() < deadline:
            await asyncio.sleep(0.05)
        return not getattr(self.ctl, "unsaved", False)

    # ------------------------------------------------------------------ the undo on disk
    def _load_undo(self) -> list[dict[str, float]] | None:
        if self.undo_file is None:
            return None
        try:
            data = json.loads(self.undo_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        bands = data.get("undo") if isinstance(data, dict) else None
        if not isinstance(bands, list) or len(bands) != N_SYS_PEQ \
                or not all(isinstance(b, dict) for b in bands):
            return None
        return band_dicts(bands)

    async def _save_undo(self) -> None:
        if self.undo_file is None:
            return
        path, bands = self.undo_file, self.undo_bands

        def write() -> None:
            try:
                if bands is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_write(path, json.dumps({"undo": bands}, indent=1) + "\n")
            except OSError:
                pass   # the undo stays in memory: only a restart would lose it
        await asyncio.to_thread(write)


__all__ = ["Filter", "Import", "MAX_TEXT", "TuneDesk", "band_dicts", "curve", "parse_filters",
           "plan_bands", "q_for_bandwidth"]
