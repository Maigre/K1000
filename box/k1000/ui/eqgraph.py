# SPDX-License-Identifier: GPL-3.0-or-later
"""The live-EQ graph of the SHOW page: exact filter curves, a log-frequency axis, RTA ballistics
and the PIL drawing of the graph area (grid, RTA behind, curve, band markers).

Why each part is the way it is:
- The curve must be the sound, not a sketch of it. The coefficients are the ones CamillaDSP
  5.0.0-beta.1 computes (`camilladsp-schema/src/filters/biquad.rs`, `from_config`): RBJ cookbook
  biquads with alpha = sin(w0) / (2 Q), the shelves with beta = sin(w0) * sqrt(A) / Q (the `q`
  form `dspconfig.py` writes; the `slope` form is the same filter with another width input), the
  GraphicEqualizer's bands as Peaking/Bandwidth, all evaluated at the engine rate (48 kHz) with the
  numerically stable phi = sin^2(w/2) form of |H(e^jw)|^2. So the curve near 20 kHz shows the
  bilinear-transform cramping the engine really has, and a 0 dB band is exactly flat.
- Pure and fast: numpy for the curve (6 bands x 456 columns is microseconds), the static grid and
  labels rasterised once per box size into masks, glyphs cached. Nothing here touches the engine.
- The RTA is drawn BEHIND the curve, in its own scale (engine dBFS of the peak FFT bin in each
  log band), and the engine does no averaging: `Rta` adds the console ballistics (fall-back
  rate, peak hold) and a conservative "persistent narrow peak" detector that GRAB can use.
  It says PEAK, never FEEDBACK: a held synth note looks the same as a ring.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Sequence

import numpy as np
from PIL import Image, ImageDraw, ImageFont

FS = 48000.0
F_LO, F_HI = 20.0, 20000.0

PEAKING = "Peaking"
PEAKING_BW = "PeakingBandwidth"     # GraphicEqualizer band: width in octaves (q field)
LOWSHELF = "Lowshelf"
HIGHSHELF = "Highshelf"
HIGHPASS = "Highpass"
LOWPASS = "Lowpass"
_GAIN_KINDS = (PEAKING, PEAKING_BW, LOWSHELF, HIGHSHELF)


# ------------------------------------------------------------------------------------------
# Filters
# ------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Band:
    """One biquad as CamillaDSP sees it. `q` is Q (or octaves for PEAKING_BW); `gain` in dB (the
    band's stored gain). `on` False = a live band switched OUT (D-014): the engine plays it at
    0 dB, so it adds nothing to the curve, while its marker still sits at its stored gain."""
    kind: str
    freq: float
    gain: float = 0.0
    q: float = 0.707
    on: bool = True

    @property
    def flat(self) -> bool:
        """It does nothing to the sound: a gain filter at 0 dB is exactly unity (b == a), and
        an OUT band plays at 0 dB. Skip it."""
        return self.kind in _GAIN_KINDS and (abs(self.gain) < 1e-9 or not self.on)


def coefficients(band: Band, fs: float = FS) -> tuple[float, float, float, float, float]:
    """(b0, b1, b2, a1, a2), normalised by a0, exactly as CamillaDSP v5 `from_config`."""
    f = min(max(float(band.freq), 1e-3), fs / 2 * 0.9999)
    w = 2.0 * math.pi * f / fs
    sn, cs = math.sin(w), math.cos(w)
    g, q = float(band.gain), max(float(band.q), 1e-6)
    k = band.kind
    if k == PEAKING or k == PEAKING_BW:
        a = 10.0 ** (g / 40.0)
        if k == PEAKING:
            alpha = sn / (2.0 * q)
        else:   # width in octaves (CamillaDSP Peaking/Bandwidth, used by GraphicEqualizer)
            alpha = sn * math.sinh(math.log(2.0) / 2.0 * q * w / sn)
        b0, b1, b2 = 1.0 + alpha * a, -2.0 * cs, 1.0 - alpha * a
        a0, a1, a2 = 1.0 + alpha / a, -2.0 * cs, 1.0 - alpha / a
    elif k in (LOWSHELF, HIGHSHELF):
        a = 10.0 ** (g / 40.0)
        beta = sn * math.sqrt(a) / q
        if k == HIGHSHELF:
            b0 = a * ((a + 1.0) + (a - 1.0) * cs + beta)
            b1 = -2.0 * a * ((a - 1.0) + (a + 1.0) * cs)
            b2 = a * ((a + 1.0) + (a - 1.0) * cs - beta)
            a0 = (a + 1.0) - (a - 1.0) * cs + beta
            a1 = 2.0 * ((a - 1.0) - (a + 1.0) * cs)
            a2 = (a + 1.0) - (a - 1.0) * cs - beta
        else:
            b0 = a * ((a + 1.0) - (a - 1.0) * cs + beta)
            b1 = 2.0 * a * ((a - 1.0) - (a + 1.0) * cs)
            b2 = a * ((a + 1.0) - (a - 1.0) * cs - beta)
            a0 = (a + 1.0) + (a - 1.0) * cs + beta
            a1 = -2.0 * ((a - 1.0) + (a + 1.0) * cs)
            a2 = (a + 1.0) + (a - 1.0) * cs - beta
    elif k in (HIGHPASS, LOWPASS):
        alpha = sn / (2.0 * q)
        if k == HIGHPASS:
            b0, b1, b2 = (1.0 + cs) / 2.0, -(1.0 + cs), (1.0 + cs) / 2.0
        else:
            b0, b1, b2 = (1.0 - cs) / 2.0, 1.0 - cs, (1.0 - cs) / 2.0
        a0, a1, a2 = 1.0 + alpha, -2.0 * cs, 1.0 - alpha
    else:
        raise ValueError(f"unknown filter kind {k!r}")
    return b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0


def response_db(band: Band, freqs: Any, fs: float = FS) -> np.ndarray:
    """|H| in dB of one band at `freqs` (Hz), digital (at fs), not the analog prototype."""
    f = np.asarray(freqs, dtype=np.float64)
    if band.flat:
        return np.zeros_like(f)
    b0, b1, b2, a1, a2 = coefficients(band, fs)
    phi = np.sin(np.pi * f / fs) ** 2
    num = (b0 + b1 + b2) ** 2 - 4.0 * (b0 * b1 + 4.0 * b0 * b2 + b1 * b2) * phi + 16.0 * b0 * b2 * phi * phi
    den = (1.0 + a1 + a2) ** 2 - 4.0 * (a1 + 4.0 * a2 + a1 * a2) * phi + 16.0 * a2 * phi * phi
    return 10.0 * np.log10(np.maximum(num, 1e-30) / np.maximum(den, 1e-30))


def curve_db(bands: Sequence[Band], freqs: Any, fs: float = FS) -> np.ndarray:
    """The combined response (dB) of filters in series."""
    f = np.asarray(freqs, dtype=np.float64)
    out = np.zeros_like(f)
    for b in bands:
        if not b.flat:
            out += response_db(b, f, fs)
    return out


def geq_bands(gains: Sequence[float], f_min: float = F_LO, f_max: float = F_HI) -> list[Band]:
    """CamillaDSP GraphicEqualizer as Bands (the engine drops |gain| <= 0.001 the same way)."""
    n = len(gains)
    if n == 0:
        return []
    lo, hi = math.log2(f_min), math.log2(f_max)
    bw = (hi - lo) / n
    return [Band(PEAKING_BW, 2.0 ** (lo + (i + 0.5) * bw), float(g), bw)
            for i, g in enumerate(gains) if abs(g) > 0.001]


# Live-EQ slots as K1000 names them (input_map.LIVE_SLOTS order: P1..P4, LS, HS) and the order
# the SHOW page draws them in (low to high: LS 1 2 3 4 HS).
LIVE_KINDS: tuple[str, ...] = (PEAKING, PEAKING, PEAKING, PEAKING, LOWSHELF, HIGHSHELF)
DISPLAY_ORDER: tuple[int, ...] = (4, 0, 1, 2, 3, 5)
MARKER_TEXT: tuple[str, ...] = ("1", "2", "3", "4", "L", "H")


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _f(v: Any, default: float) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _on(b: Any) -> bool:
    """A band's IN/OUT (D-014): only an explicit False is OUT (an older State has no `on`)."""
    return _attr(b, "on", True) is not False


def live_bands(state: Any) -> list[Band]:
    """The 6 live slots of a k1000 State (or its dict) as Bands, in LIVE_SLOTS order.

    Tolerant like pages.py: a missing band reads as flat at its default place, never raises.
    """
    peq = _attr(state, "live_peq", None) or []
    defaults = ((250.0, 4.0), (1000.0, 4.0), (2500.0, 4.0), (5000.0, 4.0))
    out: list[Band] = []
    for i in range(4):
        b = peq[i] if i < len(peq) else None
        out.append(Band(PEAKING, _f(_attr(b, "freq"), defaults[i][0]), _f(_attr(b, "gain"), 0.0),
                        _f(_attr(b, "q"), defaults[i][1]), _on(b)))
    for name, kind, fd in (("live_lowshelf", LOWSHELF, 120.0), ("live_highshelf", HIGHSHELF, 8000.0)):
        b = _attr(state, name, None)
        out.append(Band(kind, _f(_attr(b, "freq"), fd), _f(_attr(b, "gain"), 0.0),
                        _f(_attr(b, "q"), 0.707), _on(b)))
    return out


# ------------------------------------------------------------------------------------------
# Axes
# ------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class LogAxis:
    """Frequency <-> x pixel, log scale, F_LO at x0 and F_HI at x1."""
    x0: float
    x1: float
    f_lo: float = F_LO
    f_hi: float = F_HI

    @property
    def _k(self) -> float:
        return (self.x1 - self.x0) / math.log(self.f_hi / self.f_lo)

    def x(self, f: Any) -> Any:
        if isinstance(f, (int, float)):
            return self.x0 + self._k * math.log(max(float(f), 1e-9) / self.f_lo)
        return self.x0 + self._k * np.log(np.maximum(np.asarray(f, dtype=np.float64), 1e-9) / self.f_lo)

    def f(self, x: Any) -> Any:
        if isinstance(x, (int, float)):
            return self.f_lo * math.exp((float(x) - self.x0) / self._k)
        return self.f_lo * np.exp((np.asarray(x, dtype=np.float64) - self.x0) / self._k)

    def columns(self, step: int = 1) -> tuple[np.ndarray, np.ndarray]:
        """(x, f) of every `step`-th pixel column from x0 to x1 inclusive."""
        xs = np.arange(int(round(self.x0)), int(round(self.x1)) + 1, max(1, int(step)), dtype=np.float64)
        return xs, self.f(xs)


@dataclass(frozen=True)
class DbAxis:
    """dB <-> y pixel (y grows downwards); values outside are clamped to the box."""
    y0: float
    y1: float
    top_db: float
    bottom_db: float

    def y(self, db: Any) -> Any:
        k = (self.y1 - self.y0) / (self.top_db - self.bottom_db)
        if isinstance(db, (int, float)):
            return min(self.y1, max(self.y0, self.y0 + (self.top_db - float(db)) * k))
        return np.clip(self.y0 + (self.top_db - np.asarray(db, dtype=np.float64)) * k, self.y0, self.y1)


# ------------------------------------------------------------------------------------------
# RTA ballistics
# ------------------------------------------------------------------------------------------
@dataclass
class RtaFrame:
    """What the graph draws: per-bin levels (dBFS) on the engine's log bins, the display range,
    and an optional persistent peak."""
    freqs: np.ndarray
    level: np.ndarray
    hold: np.ndarray | None = None
    lo_db: float = -90.0
    hi_db: float = -30.0
    peak_hz: float | None = None
    source: str = "PGM"


def combine_power(*mags: Any) -> np.ndarray:
    """Power average of several dB spectra (e.g. program L and R): never a waveform average,
    which would cancel out-of-phase content."""
    arr = np.asarray([np.asarray(m, dtype=np.float64) for m in mags])
    return 10.0 * np.log10(np.mean(10.0 ** (arr / 10.0), axis=0))


class Rta:
    """Console ballistics on top of GetSpectrum frames.

    level: instant rise, falls at `fall_db_s` (an X32 'decay ~1 s' look); hold: per-bin peak held
    `hold_s` then released; range: auto, top = loudest hold rounded up to 6 dB + `headroom_db`,
    `span_db` deep. peak_hz: a bin that is not buried (within `loud_db` of the frame's max),
    narrow (`prominence_db` above the median of the surrounding +-1/3 octave) and has stayed so
    for `persist_s`, within one bin. That is the GRAB target; when there is none, GRAB falls
    back to the loudest bin above the bass (`grab_target` says which).
    """

    def __init__(self, fall_db_s: float = 24.0, hold_s: float = 3.0, span_db: float = 66.0,
                 persist_s: float = 0.7, prominence_db: float = 10.0, loud_db: float = 30.0,
                 search: tuple[float, float] = (40.0, 16000.0), floor_db: float = -140.0,
                 headroom_db: float = 18.0):
        self.fall_db_s, self.hold_s, self.span_db = fall_db_s, hold_s, span_db
        self.headroom_db = headroom_db
        self.persist_s, self.prominence_db, self.loud_db = persist_s, prominence_db, loud_db
        self.search, self.floor_db = search, floor_db
        self.freqs: np.ndarray | None = None
        self.level: np.ndarray | None = None
        self.hold: np.ndarray | None = None
        self._hold_t: np.ndarray | None = None
        self._t: float | None = None
        self._cand: tuple[int, float] | None = None     # (bin, since)
        self.peak_bin: int | None = None
        self.top_db = -30.0

    def reset(self) -> None:
        self.__init__(self.fall_db_s, self.hold_s, self.span_db, self.persist_s,
                      self.prominence_db, self.loud_db, self.search, self.floor_db,
                      self.headroom_db)

    def update(self, freqs: Any, mags: Any, now: float) -> None:
        f = np.asarray(freqs, dtype=np.float64)
        m = np.maximum(np.asarray(mags, dtype=np.float64), self.floor_db)
        if self.freqs is None or self.level is None or len(f) != len(self.freqs):
            self.freqs, self.level = f, m.copy()
            self.hold, self._hold_t = m.copy(), np.full(len(m), now)
            self._t, self._cand, self.peak_bin = now, None, None
        dt = max(0.0, now - (self._t if self._t is not None else now))
        self._t = now
        self.level = np.maximum(m, self.level - self.fall_db_s * dt)
        assert self.hold is not None and self._hold_t is not None
        up = m >= self.hold
        expired = (now - self._hold_t) > self.hold_s
        self.hold = np.where(up | expired, m, self.hold)
        self._hold_t = np.where(up | expired, now, self._hold_t)
        # display top: the loudest held bin rounded up to 6 dB, plus headroom, so the music
        # sits under the curve's 0 dB line and a ring that grows out of it reaches it
        top = float(np.max(self.hold))
        self.top_db = min(18.0, max(-60.0, 6.0 * math.ceil(top / 6.0) + self.headroom_db))
        self._track_peak(m, now)

    def _track_peak(self, m: np.ndarray, now: float) -> None:
        f = self.freqs
        assert f is not None
        n = len(m)
        per_oct = (n - 1) / max(1e-9, math.log2(f[-1] / f[0])) if n > 1 else 1.0
        half = max(2, int(round(per_oct / 3.0)))
        pad = np.pad(m, half, mode="edge")
        win = np.lib.stride_tricks.sliding_window_view(pad, 2 * half + 1)
        prom = m - np.median(win, axis=1)
        ok = (f >= self.search[0]) & (f <= self.search[1]) & (m >= m.max() - self.loud_db) & \
             (prom >= self.prominence_db)
        if not ok.any():
            self._cand = None
            self.peak_bin = None
            return
        i = int(np.argmax(np.where(ok, prom, -np.inf)))
        if self._cand is not None and abs(self._cand[0] - i) <= 1:
            since = self._cand[1]
        else:
            since = now
        self._cand = (i, since)
        self.peak_bin = i if now - since >= self.persist_s else None

    @property
    def peak_hz(self) -> float | None:
        if self.peak_bin is None or self.freqs is None:
            return None
        return float(self.freqs[self.peak_bin])

    def grab_target(self, fallback: tuple[float, float] = (100.0, 16000.0)) -> tuple[float, str] | None:
        """GRAB's target and why: (hz, "PEAK") for the persistent narrow peak, else (hz, "LOUDEST")
        for the loudest bin of the current level within `fallback` (above the bass that always
        dominates music), else None (no RTA data, or silence). The UI says which it was."""
        if self.peak_hz is not None:
            return self.peak_hz, "PEAK"
        if self.freqs is None or self.level is None:
            return None
        ok = (self.freqs >= fallback[0]) & (self.freqs <= fallback[1])
        if not ok.any() or float(np.max(self.level[ok])) <= self.floor_db + 1.0:
            return None
        return float(self.freqs[ok][int(np.argmax(self.level[ok]))]), "LOUDEST"

    def grab_hz(self) -> float | None:
        t = self.grab_target()
        return None if t is None else t[0]

    def frame(self, source: str = "PGM") -> RtaFrame | None:
        if self.freqs is None or self.level is None:
            return None
        return RtaFrame(self.freqs, self.level, self.hold, self.top_db - self.span_db, self.top_db,
                        self.peak_hz, source)


# ------------------------------------------------------------------------------------------
# Drawing
# ------------------------------------------------------------------------------------------
FONT_DIR = "/usr/share/fonts/truetype/dejavu/"


@lru_cache(maxsize=32)
def _font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(FONT_DIR + ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"), size)
    except OSError:
        return ImageFont.load_default(size)


@lru_cache(maxsize=256)
def _glyphs(text: str, size: int, bold: bool, anchor: str):
    f = _font(size, bold)
    left, top, right, bottom = f.getbbox(text, anchor=anchor)
    mask = Image.new("L", (max(1, right - left), max(1, bottom - top)), 0)
    ImageDraw.Draw(mask).text((-left, -top), text, font=f, fill=255, anchor=anchor)
    return mask, left, top


def _text(img: Image.Image, xy: tuple[float, float], text: str, size: int, fill: Any,
          anchor: str = "mm", bold: bool = True) -> None:
    mask, left, top = _glyphs(text, size, bold, anchor)
    img.paste(fill, (int(round(xy[0])) + left, int(round(xy[1])) + top), mask)


@dataclass(frozen=True)
class GraphStyle:
    top_db: float = 9.0               # EQ scale: +9..-21 dB keeps 0 dB high (cuts are the job)
    bottom_db: float = -21.0
    grid_db: tuple[float, ...] = (6.0, -6.0, -12.0, -18.0)
    grid_hz: tuple[float, ...] = (50.0, 100.0, 200.0, 500.0, 1000.0, 2000.0, 5000.0, 10000.0)
    label_hz: tuple[tuple[float, str], ...] = ((50.0, "50"), (100.0, "100"), (200.0, "200"),
                                               (500.0, "500"), (1000.0, "1k"), (2000.0, "2k"),
                                               (5000.0, "5k"), (10000.0, "10k"))
    # D-009: the 0 dB line is labelled, and the dB labels sit at the RIGHT edge (the top octave
    # is where the program and the markers are least), each with a black outline so it reads
    # over the spectrum. Each label sits just under its line, so a flat curve never covers it.
    label_db: tuple[tuple[float, str], ...] = ((6.0, "+6"), (0.0, "0 dB"), (-6.0, "-6"),
                                               (-12.0, "-12"))
    label_db_right: bool = True
    label_outline: int = 2            # px of black around the dB labels (1 for the Hz labels)
    bg: tuple[int, int, int] = (0, 0, 0)
    grid: tuple[int, int, int] = (44, 44, 44)
    zero: tuple[int, int, int] = (110, 110, 110)
    label: tuple[int, int, int] = (120, 120, 120)
    label_db_fill: tuple[int, int, int] = (185, 185, 185)
    rta_fill: tuple[int, int, int] = (0, 52, 78)
    rta_edge: tuple[int, int, int] = (20, 120, 165)
    rta_hold: tuple[int, int, int] = (90, 130, 150)     # dots, every `hold_every` columns
    hold_every: int = 3
    curve: tuple[int, int, int] = (255, 255, 255)
    curve_out: tuple[int, int, int] = (110, 110, 110)
    sel_fill: tuple[int, int, int] = (255, 184, 0)     # AMBER, blended
    sel_alpha: int = 150
    sel_marker: tuple[int, int, int] = (255, 184, 0)
    cut: tuple[int, int, int] = (255, 90, 0)            # the v0 slot-pill colours
    boost: tuple[int, int, int] = (40, 220, 70)
    idle: tuple[int, int, int] = (150, 150, 150)
    out_fill: tuple[int, int, int] = (0, 0, 0)           # D-014: an OUT band's marker is hollow
    peak: tuple[int, int, int] = (0, 200, 255)          # D-009: a small CYAN pointer
    peak_line: bool = False           # the mockups' dashed line down the graph (off: a pointer)
    curve_w: int = 3
    marker_r: int = 10
    idle_r: int = 9
    sel_r: int = 13
    label_size: int = 14
    marker_size: int = 14
    step: int = 2                     # columns per curve point (2 px is smooth at 3 px width)


STYLE = GraphStyle()


@lru_cache(maxsize=8)
def _grid_layer(w: int, h: int, style: GraphStyle) -> tuple[Image.Image, Image.Image]:
    """The static grid (lines, 0 dB line, axis labels) as ONE RGB layer + its L mask for a
    w x h box, rasterised once: one paste per frame instead of drawing ~20 items."""
    fa = LogAxis(0, w - 1)
    da = DbAxis(0, h - 1, style.top_db, style.bottom_db)
    rgb = Image.new("RGB", (w, h), (0, 0, 0))
    mask = Image.new("L", (w, h), 0)
    dr, dm = ImageDraw.Draw(rgb), ImageDraw.Draw(mask)
    for hz in style.grid_hz:
        x = round(fa.x(hz))
        dr.line((x, 0, x, h - 1), fill=style.grid)
        dm.line((x, 0, x, h - 1), fill=255 if hz in (100.0, 1000.0, 10000.0) else 170)
    for db in style.grid_db:
        y = round(da.y(db))
        dr.line((0, y, w - 1, y), fill=style.grid)
        dm.line((0, y, w - 1, y), fill=170)
    y0 = round(da.y(0.0))
    dr.line((0, y0, w - 1, y0), fill=style.zero)
    dm.line((0, y0, w - 1, y0), fill=255)
    f = _font(style.label_size, False)
    out1 = min(1, style.label_outline)
    for hz, t in style.label_hz:
        xy = (fa.x(hz) + 3, h - 2)
        dr.text(xy, t, font=f, fill=style.label, anchor="ld", stroke_width=out1, stroke_fill=(0, 0, 0))
        dm.text(xy, t, font=f, fill=255, anchor="ld", stroke_width=out1, stroke_fill=255)
    fb = _font(style.label_size, True)
    for db, t in style.label_db:
        y = round(da.y(db)) + 2 + style.label_outline
        xy, anchor = ((w - 3 - style.label_outline, y), "ra") if style.label_db_right else ((3, y), "la")
        dr.text(xy, t, font=fb, fill=style.label_db_fill, anchor=anchor,
                stroke_width=style.label_outline, stroke_fill=(0, 0, 0))
        dm.text(xy, t, font=fb, fill=255, anchor=anchor, stroke_width=style.label_outline,
                stroke_fill=255)
    return rgb, mask


def _marker(img: Image.Image, d: ImageDraw.ImageDraw, x: float, y: float, text: str, fill: Any,
            outline: Any, r: int, txt: Any, size: int) -> None:
    d.ellipse((x - r, y - r, x + r, y + r), fill=fill, outline=outline, width=2)
    _text(img, (x, y + 0.5), text, size, txt)


def _out_marker(img: Image.Image, d: ImageDraw.ImageDraw, x: float, y: float, text: str,
                colour: Any, r: int, size: int, fill: Any = (0, 0, 0), width: int = 2) -> None:
    """A band switched OUT (D-014): hollow, with a dashed ring (the chip's dashed outline)."""
    box = (x - r, y - r, x + r, y + r)
    d.ellipse(box, fill=fill)
    for k in range(8):
        d.arc(box, k * 45 - 14, k * 45 + 14, fill=colour, width=width)
    _text(img, (x, y + 0.5), text, size, colour)


def draw_graph(img: Image.Image, box: tuple[int, int, int, int], bands: Sequence[Band],
               selected: int | None = None, rta: RtaFrame | None = None, *,
               labels: Sequence[str] = MARKER_TEXT, order: Sequence[int] = DISPLAY_ORDER,
               eq_in: bool = True, style: GraphStyle = STYLE) -> dict[str, Any]:
    """Draw the graph into `img` inside `box` = (x0, y0, x1, y1) (x1, y1 exclusive).

    bands/labels are in slot order (LIVE_SLOTS); `order` is the drawing order (the selected
    marker is always drawn last, on top). `selected` is a slot index or None (e.g. SUB chosen).
    eq_in=False draws the curve grey (EQ OUT). A band switched OUT (`Band.on` False, D-014)
    adds nothing to the curve and gets no amber fill; its marker stays at its stored gain,
    hollow (outlined in its cut/boost colour, amber when selected), so its way back IN is in
    sight. Returns geometry for the caller (axes, marker positions) so overlays such as a PEAK
    label can line up.
    """
    x0, y0, x1, y1 = (int(v) for v in box)
    w, h = x1 - x0, y1 - y0
    fa = LogAxis(x0, x1 - 1)
    # keep markers inside: the dB axis leaves room for half a marker at both ends
    da = DbAxis(y0, y1 - 1, style.top_db, style.bottom_db)
    d = ImageDraw.Draw(img)
    d.rectangle((x0, y0, x1 - 1, y1 - 1), fill=style.bg)
    xs, fs = fa.columns(style.step)
    # 1. RTA, behind everything
    if rta is not None and len(rta.freqs) > 1:
        lf = np.log(np.maximum(rta.freqs, 1e-3))
        span = max(1e-6, rta.hi_db - rta.lo_db)

        def ys(levels: np.ndarray) -> np.ndarray:
            v = np.interp(np.log(fs), lf, levels)
            return y1 - 1 - np.clip((v - rta.lo_db) / span, 0.0, 1.0) * (h - 1)

        yl = ys(rta.level)
        poly = [(float(xs[0]), float(y1 - 1))] + list(zip(xs.tolist(), yl.tolist())) + \
               [(float(xs[-1]), float(y1 - 1))]
        d.polygon(poly, fill=style.rta_fill)
        d.line(list(zip(xs.tolist(), yl.tolist())), fill=style.rta_edge, width=1)
        if rta.hold is not None:
            yh = ys(rta.hold)
            k = max(1, style.hold_every // max(1, style.step))
            d.point([(float(x), float(y)) for x, y in zip(xs[::k], yh[::k]) if y < y1 - 2],
                    fill=style.rta_hold)
    # 2. grid + labels over the RTA (one pre-rasterised layer)
    grid_rgb, grid_mask = _grid_layer(w, h, style)
    img.paste(grid_rgb, (x0, y0), grid_mask)
    y_zero = da.y(0.0)
    # 3. the selected band's own contribution, blended amber between 0 dB and its curve
    sel_band = bands[selected] if selected is not None and 0 <= selected < len(bands) else None
    if sel_band is not None and not sel_band.flat and eq_in:
        yb = da.y(response_db(sel_band, fs))
        on = np.nonzero(np.abs(yb - y_zero) >= 0.5)[0]       # only where the band does something
        if len(on):
            a, b = int(on[0]), int(on[-1]) + 1
            bx0, bx1 = int(xs[a]), int(xs[min(b, len(xs) - 1)]) + 1
            by0 = int(min(y_zero, float(np.min(yb[a:b])))) - 1
            by1 = int(max(y_zero, float(np.max(yb[a:b])))) + 2
            mask = Image.new("L", (max(1, bx1 - bx0), max(1, by1 - by0)), 0)
            pts = [(float(xs[a] - bx0), float(y_zero - by0))] + \
                  list(zip((xs[a:b] - bx0).tolist(), (yb[a:b] - by0).tolist())) + \
                  [(float(xs[b - 1] - bx0), float(y_zero - by0))]
            ImageDraw.Draw(mask).polygon(pts, fill=style.sel_alpha)
            img.paste(style.sel_fill, (bx0, by0), mask)
    # 4. idle bands (0 dB) first, hollow and small: they must never hide the curve
    pos: dict[int, tuple[float, float]] = {}

    def place(i: int, r: int) -> tuple[float, float]:
        b = bands[i]
        mx = min(x1 - 1 - r, max(x0 + r, fa.x(b.freq)))
        my = min(y1 - 1 - r, max(y0 + r, da.y(b.gain)))
        pos[i] = (mx, my)
        return mx, my

    live = [i for i in order if i < len(bands)]
    for i in live:
        if i != selected and abs(bands[i].gain) < 0.05 and bands[i].on:
            mx, my = place(i, style.idle_r)
            r = style.idle_r
            d.ellipse((mx - r, my - r, mx + r, my + r), outline=style.idle, width=2)
            _text(img, (mx, my + 0.5), labels[i] if i < len(labels) else "?", style.marker_size - 1,
                  style.idle)
    # 5. the combined curve
    yc = da.y(curve_db(bands, fs))
    d.line(list(zip(xs.tolist(), yc.tolist())), fill=style.curve if eq_in else style.curve_out,
           width=style.curve_w, joint="curve")
    # 6. PEAK pointer (persistent narrow peak of the RTA): a small triangle on the top edge;
    #    the caller writes its frequency next to it (geo["peak_x"])
    peak_x = None
    if rta is not None and rta.peak_hz:
        peak_x = min(x1 - 8.0, max(x0 + 7.0, fa.x(rta.peak_hz)))
        d.polygon([(peak_x - 7, y0 + 1), (peak_x + 7, y0 + 1), (peak_x, y0 + 11)], fill=style.peak)
        if style.peak_line:
            for yy in range(y0 + 13, y1 - 1, 6):      # dashed: it is a pointer, not a curve
                d.line((peak_x, yy, peak_x, min(y1 - 1, yy + 2)), fill=style.peak, width=1)
    # 7. bands OUT (hollow, at their stored gain), bands in use, then the selected one on top
    def colour(b: Band) -> Any:
        return style.idle if abs(b.gain) < 0.05 else style.cut if b.gain < 0 else style.boost

    for i in [i for i in live if i != selected and not bands[i].on]:
        mx, my = place(i, style.marker_r)
        _out_marker(img, d, mx, my, labels[i] if i < len(labels) else "?", colour(bands[i]),
                    style.marker_r, style.marker_size, style.out_fill)
    for i in [i for i in live if i != selected and bands[i].on and abs(bands[i].gain) >= 0.05]:
        mx, my = place(i, style.marker_r)
        _marker(img, d, mx, my, labels[i] if i < len(labels) else "?",
                style.cut if bands[i].gain < 0 else style.boost, (0, 0, 0), style.marker_r,
                (0, 0, 0), style.marker_size)
    if sel_band is not None and selected is not None:
        mx, my = place(selected, style.sel_r)
        if sel_band.on:
            _marker(img, d, mx, my, labels[selected] if selected < len(labels) else "?",
                    style.sel_marker, (0, 0, 0), style.sel_r, (0, 0, 0), style.marker_size + 2)
        else:
            _out_marker(img, d, mx, my, labels[selected] if selected < len(labels) else "?",
                        style.sel_marker, style.sel_r, style.marker_size + 2, style.out_fill, 3)
    return {"faxis": fa, "daxis": da, "markers": pos, "peak_x": peak_x, "y_zero": y_zero}


def render_graph(size: tuple[int, int], bands: Sequence[Band], selected: int | None = None,
                 rta: RtaFrame | None = None, **kw: Any) -> Image.Image:
    """Convenience: the graph alone on a new RGB image (tests, the phone's PNG fallback)."""
    img = Image.new("RGB", size, STYLE.bg)
    draw_graph(img, (0, 0, size[0], size[1]), bands, selected, rta, **kw)
    return img


# ------------------------------------------------------------------------------------------
# Notch helpers (GRAB)
# ------------------------------------------------------------------------------------------
NOTCH_Q = 1.0 / (2.0 * math.sinh(math.log(2.0) / 2.0 / 6.0))     # 1/6 octave = Q 8.65


def shelf_q_from_slope(slope_db: float, gain_db: float) -> float:
    """The Q of the `q` shelf that equals CamillaDSP's `slope` shelf (dB/octave, 12 = S 1).

    From the two alpha forms in biquad.rs: 1/Q = sqrt((A + 1/A) (1/S - 1) + 2), S = slope/12.
    """
    a = 10.0 ** (gain_db / 40.0)
    return 1.0 / math.sqrt((a + 1.0 / a) * (12.0 / slope_db - 1.0) + 2.0)


def q_for_octaves(octaves: float) -> float:
    """Q of a peaking band `octaves` wide (RBJ bandwidth relation): 1/3 -> 4.32, 1/10 -> 14.4."""
    return 1.0 / (2.0 * math.sinh(math.log(2.0) / 2.0 * octaves))


def free_band(bands: Sequence[Band], prefer: int | None = None,
              candidates: Sequence[int] = (0, 1, 2, 3)) -> int | None:
    """A peaking slot at 0 dB (inaudible to move): `prefer` if it is free, else the first. The
    STORED gain decides (D-014): a band OUT with a cut or boost is not free, it keeps its
    settings; an OUT band at 0 dB is."""
    def free(i: int) -> bool:
        return i < len(bands) and abs(bands[i].gain) < 1e-9

    if prefer is not None and prefer in candidates and free(prefer):
        return prefer
    for i in candidates:
        if free(i):
            return i
    return None
