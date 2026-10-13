# SPDX-License-Identifier: GPL-3.0-or-later
"""Screen pages for the 480x320 ZynScreen (UI v1, D-009): render(page, state, status, focus).

Pages: SHOW (the front page: master, FREQ/GAIN/Q of the selected live band, the EQ curve over
the program spectrum), ALIGN (master, balance, sub delay, Lo-Cut; E3/E4 locked until a B3
hold), SETUP (the list), PHONE (drawn by ui/qr.py) and HWTEST (the knob test). The v0 names
MAIN and LIVE are aliases of ALIGN and SHOW.

Design rules (why it looks like this):
- Read at arm's length, outdoors, by someone mixing: black background, white digits, amber
  labels, one colour per meaning. Amber = what is being edited, and hints (brief). Red = mute,
  clip, a critical system alert. Orange = a system warning. Cyan = the knob tags and the
  spectrum PEAK.
- The screen mirrors the knobs: each encoder's value sits next to its corner (KNOB_CORNERS in
  input_map, an assumption until the physical positions are confirmed).
- System alerts (KNOBS OFFLINE, NO DEVICE, ENGINE DOWN, CANNOT SAVE, pipeline/device fixes)
  take the STATUS BAR in their own colour, never amber: KNOBS OFFLINE red across the whole bar,
  other critical alerts red, warnings orange, passing states (APPLYING SETUP…, RAMPING IN)
  blue. Hints (what the operator's own press did, 2.5 s) keep the amber strip: the chip row on
  SHOW, the middle strip on ALIGN, the footer on SETUP. "The system has a problem" and "this is
  what your press did" never share a place or a colour.
- SHOW has persistent pills for every non-default system state (LO-CUT OFF, POL INV, MONO SUB,
  AUDITION, LAPTOP OPEN…), on the bottom row between the E2 and E4 labels, where neither a hint
  nor the MUTED band ever covers them.
- The 4-bar output meter stays on SHOW and ALIGN: clipping must be visible while notching.
- Overlays are derived from the status, never a page the controller must remember to show:
  NO DEVICE > ENGINE DOWN replace the body. MUTED is a red pill in the status bar plus a red
  frame on every page; on SHOW also a red band over the chip row and the top of the graph (the
  0 dB line, every value, the edit box and FINE stay visible), on ALIGN the v0 band across the
  middle, on SETUP a red footer (a band would hide the selected row), on the Knob test a tag.
- Unknown is not OK (fail closed): a status that does not say the engine runs shows an amber
  "ENGINE ?", one that does not say the device is present shows "DEVICE ?", plus a STATUS ?
  warning; never green.
- Rendering only reads attributes (`state.master_db`, `status.spectrum.level`, ...), and every
  read has a fallback, so a partial status or an older State never crashes the screen; anything
  that still raises gives a "UI ERROR" frame (with the mute state) instead.
- Cheap enough for SHOW at 10 fps while the spectrum moves: glyphs and the graph grid are
  cached, the curve is numpy (eqgraph), and display.Framebuffer sends only the changed rows.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Sequence

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import eqgraph as eg
from .input_map import (A_LAPTOP, A_RESET_LIVE, A_UNDO_RESET, BAND_CYCLE, F_BALANCE, F_LOWCUT_FREQ,
                        F_LOWCUT_ON, F_MASTER, F_SLOT, F_SPECTRUM_ON, F_SUB_DELAY, HINT_LOCKED,
                        KNOB_CORNERS, LIVE_SLOT_LABELS, LIVE_SLOTS, SETUP_ITEMS, Focus,
                        knob_labels, page_name)

log = logging.getLogger(__name__)

W, H = 480, 320
SIZE = (W, H)

FONT_DIR = "/usr/share/fonts/truetype/dejavu/"
FONT_BOLD = "DejaVuSans-Bold.ttf"
FONT_REG = "DejaVuSans.ttf"

# Colours (RGB). Chosen to survive RGB565 and a washed-out TFT in daylight.
BG = (0, 0, 0)
FG = (255, 255, 255)
DIM = (150, 150, 150)
FAINT = (70, 70, 70)
AMBER = (255, 184, 0)
BAR_BG = (36, 36, 36)
GREEN = (40, 220, 70)
YELLOW = (255, 220, 0)
RED = (235, 20, 20)
BAND_RED = (200, 0, 0)
ORANGE = (255, 120, 0)
CYAN = (0, 200, 255)
CUT = (255, 90, 0)                     # a live band that cuts (the v0 slot-pill colour)
STATUS_BG = (28, 28, 28)
INFO_BLUE = (30, 90, 180)
PILL_BG = (60, 60, 60)
PILL_EDGE = (190, 190, 190)

# System alert levels (control.Status.alerts): fill and text colour in the status bar.
ALERT_CRITICAL, ALERT_WARNING, ALERT_INFO = "critical", "warning", "info"
ALERT_STYLE: dict[str, tuple[tuple[int, int, int], tuple[int, int, int]]] = {
    ALERT_CRITICAL: (RED, FG),
    ALERT_WARNING: (ORANGE, BG),
    ALERT_INFO: (INFO_BLUE, FG),
}
FULL_WIDTH_ALERTS: tuple[str, ...] = ("KNOBS OFFLINE",)   # D-009: red, the whole status bar
ALERT_MIN_PX = 12                      # status-bar alert text: below this it is ellipsized
# The page pill while a system alert is up (the alert gets the room).
PAGE_SHORT: dict[str, str] = {"SHOW": "SHW", "ALIGN": "ALN", "SETUP": "SET", "PHONE": "PHN",
                              "TEST": "TST"}
# Persistent SHOW pills that change the sound or open the box: orange, and folded into '+N'
# only after every neutral one (MONO SUB, AUDITION).
RISKY_PILLS: tuple[str, ...] = ("LAPTOP OPEN", "LO-CUT OFF", "POL INV")
PILL_MIN_PX = 13

STATUS_H = 30
METER_X0, METER_X1 = 206, 274          # ALIGN: centre column
MID_Y0, MID_Y1 = 150, 200              # ALIGN: middle strip (hints / MUTED band)
OVERLAYS: tuple[str, ...] = ("NO DEVICE", "ENGINE DOWN", "MUTED")
METER_LABELS: tuple[str, ...] = ("Sub L", "Sub R", "Top L", "Top R")
METER_FLOOR_DB = -60.0
ENGINE_OK = ("RUNNING", "PAUSED", "STARTING")
ENGINE_DOWN_STATES = ("INACTIVE", "STALLED", "DOWN")
ENGINE_UNKNOWN = "Unknown"             # status did not say (missing field, no status at all)
FOOTER_H = 28                          # SETUP footer row (E2 label / hint / MUTED strip)

# SHOW geometry (480 x 320; docs/07 §2.1, the mockups' constants)
TOP_Y0 = STATUS_H                      # E1 / E3 blocks: label row, then the value box
TOP_BOX = (51, 95)
CHIP_Y0, CHIP_Y1 = 98, 122             # band chips LS 1 2 3 4 HS; hints take this row
GRAPH = (0, 124, W, 254)               # x0, y0, x1, y1 (exclusive)
BOT_Y0 = 254                           # E2 / E4 blocks: the value box, then the label row
BOT_BOX = (255, 294)
METER_BOX = (200, 31, 280, 96)         # compact 4-bar meter, top centre
PILL_Y0, PILL_Y1 = 296, 315            # SHOW: persistent pills, between the bottom labels
# MUTED on SHOW: the chip row and the graph's top (+9..+0.6 dB: live boosts stop at +6), so
# the 0 dB line (y 162.7), the curve's cuts and every value stay visible.
SHOW_MUTED_BAND = (CHIP_Y0, 160)
CHIP_TEXT: dict[int, str] = {4: "LS", 0: "1", 1: "2", 2: "3", 3: "4", 5: "HS"}


# ------------------------------------------------------------------------------------------
# What the screen reads from the controller
# ------------------------------------------------------------------------------------------
@dataclass
class ScreenStatus:
    """The status fields the screen reads (any object with these attributes works;
    control.Status is the real one).

    engine: CamillaDSP GetState value ("Running", "Paused", "Inactive", "Starting", "Stalled"),
            or None when the websocket is down. Default "Unknown" -> amber "ENGINE ?".
    device_present: True / False (False -> NO DEVICE). Default None = not reported yet ->
            "DEVICE ?" (never assumed present: the m4 profile is pinned, a missing M4 must show).
    levels: peak dBFS of the 4 logical outputs (Sub L, Sub R, Top L, Top R).
    clip:   per-output clip flag, latched by the controller for a few seconds.
    muted:  the engine's actual Main mute; None -> fall back to state.muted.
    ready:  the controller is in sync with the engine (False: knob edits do not reach it yet);
            the MUTED band says READY only when the engine runs AND this is not False.
    ramping: a boot ramp is pending or running (K1000 brings Main in by itself).
    alert:  the controller's most urgent message (shown inside the MUTED band).
    in_clip: the INPUT (ADC) clipped recently (latched by the controller).
    UI v1 (D-009), same names as control.Status:
    alerts: [(level, text)] most urgent first, for the status bar; None = derived from
            `alert`/`message` (an older status).
    pills:  persistent non-default states for SHOW; None = derived from the state.
    align_unlock_s: seconds left of the ALIGN E3/E4 unlock (the EDIT pill); the focus's
            unlock_until counts too.
    undo_reset_s: SETUP > Undo reset, seconds left (0 = nothing to undo).
    spectrum_on / spectrum_state / spectrum: SETUP > Spectrum, the task's state, and the
            eqgraph.RtaFrame to draw behind the curve (None = none).
    laptop_s / laptop_state: SETUP > Laptop access, seconds left (0 = closed) and the state
            root reports (off = no such surface, closed, opening, open, closing, unavailable).
    The controller must set engine and device_present explicitly: the defaults are "unknown".
    """
    profile: str = ""
    device: str = ""                     # human name, e.g. "MOTU M4", "HiFiBerry"
    device_present: bool | None = None
    engine: str | None = ENGINE_UNKNOWN
    load: float | None = None            # processing load, percent
    levels: Sequence[float] = (-100.0, -100.0, -100.0, -100.0)
    clip: Sequence[bool] = (False, False, False, False)
    muted: bool | None = None
    message: str = ""                    # the controller's top message (v0 strip; now alerts)
    stop_reason: str = ""                # engine stop reason, shown on ENGINE DOWN
    ready: bool | None = None
    ramping: bool = False
    alert: str = ""
    in_clip: bool = False
    alerts: Sequence[tuple[str, str]] | None = None
    pills: Sequence[str] | None = None
    align_locked: bool | None = None
    align_unlock_s: float = 0.0
    undo_reset_s: float = 0.0
    spectrum_on: bool = True
    spectrum_state: str = "off"
    spectrum: Any = None
    laptop_s: float = 0.0
    laptop_state: str = "off"


# ------------------------------------------------------------------------------------------
# Tolerant reads
# ------------------------------------------------------------------------------------------
_MISSING = object()


def _get(obj: Any, path: str, default: Any = None) -> Any:
    """Read 'live_peq.1.gain' from attributes/indices/dict keys; `default` on any miss."""
    cur = obj
    for part in path.split("."):
        if cur is None:
            return default
        try:
            if part.isdigit():
                cur = cur[int(part)]
            elif isinstance(cur, dict):
                cur = cur.get(part, _MISSING)
            else:
                cur = getattr(cur, part, _MISSING)
        except (IndexError, KeyError, TypeError):
            return default
        if cur is _MISSING:
            return default
    return cur


def _num(obj: Any, path: str, default: float | None = None) -> float | None:
    v = _get(obj, path, default)
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _seq(v: Any) -> list:
    """Any sequence (list, tuple, numpy array) -> list; None or a non-iterable -> []."""
    if v is None or isinstance(v, (str, bytes)):
        return []
    try:
        return list(v)
    except Exception:
        return []


def _truth(v: Any) -> bool | None:
    """bool(v) for bool / int / numpy.bool_; None for None or anything without a truth value."""
    if v is None:
        return None
    try:
        return bool(v)
    except Exception:
        return None


def device_present(status: Any) -> bool | None:
    """True / False as reported; None = unknown (missing field or no status)."""
    return _truth(_get(status, "device_present", None))


def engine_state(status: Any) -> str | None:
    """The engine state string; None = websocket down; ENGINE_UNKNOWN when the status is silent."""
    v = _get(status, "engine", ENGINE_UNKNOWN)
    if v is None:
        return None
    v = str(v).strip()
    return v or ENGINE_UNKNOWN


def engine_unknown(status: Any) -> bool:
    e = engine_state(status)
    return e is not None and e.upper() == ENGINE_UNKNOWN.upper()


def displayed_muted(state: Any, status: Any) -> bool:
    """The mute the screen shows: the engine's real Main mute, else state.muted, else False.

    The dispatcher passes this to `map_event(..., muted=...)`, so B2 long asks for the opposite
    of what the operator sees; unknown reads as 'not muted', so B2 long then MUTES (safe side).
    """
    m = _truth(_get(status, "muted", None))
    if m is None:
        m = _truth(_get(state, "muted", None))
    return bool(m)


def overlays_for(state: Any, status: Any) -> list[str]:
    """Which overlays apply, most severe first (NO DEVICE, ENGINE DOWN, MUTED)."""
    out: list[str] = []
    if device_present(status) is False:
        out.append("NO DEVICE")
    engine = engine_state(status)
    if engine is None or engine.upper() in ENGINE_DOWN_STATES:
        if not out:
            out.append("ENGINE DOWN")
    if displayed_muted(state, status):
        out.append("MUTED")
    return out


def _unknown_notice(status: Any) -> str:
    """'STATUS ? engine · device' when the controller did not report them (fail closed)."""
    parts = [n for n, unknown in (("engine", engine_unknown(status)),
                                  ("device", device_present(status) is None)) if unknown]
    return ("STATUS ? " + " · ".join(parts)) if parts else ""


def screen_alerts(status: Any) -> list[tuple[str, str]]:
    """The system alerts for the status bar, most urgent first, as (level, text).

    `status.alerts` when the status has it (control.Status: the controller ranks them). An
    older status (no `alerts`): its `alert` (KNOBS OFFLINE critical, else a warning) and a
    different `message` as a warning. Either way, when nothing else is said and the engine or
    the device state is unknown, a STATUS ? warning (fail closed)."""
    out: list[tuple[str, str]] = []
    raw = _get(status, "alerts", None)
    if raw is not None and not isinstance(raw, (str, bytes)):
        for a in _seq(raw):
            if isinstance(a, str):
                level, text = ALERT_WARNING, a
            else:
                try:
                    level, text = str(a[0]), str(a[1])
                except Exception:
                    continue
            if text:
                out.append((level if level in ALERT_STYLE else ALERT_WARNING, text))
    else:
        alert = str(_get(status, "alert", "") or "")
        msg = str(_get(status, "message", "") or "")
        if alert:
            full = alert.upper().startswith(FULL_WIDTH_ALERTS)
            out.append((ALERT_CRITICAL if full else ALERT_WARNING, alert))
        if msg and msg != alert:
            out.append((ALERT_WARNING, msg))
    if not out:
        notice = _unknown_notice(status)
        if notice:
            out.append((ALERT_WARNING, notice))
    return out


def screen_pills(state: Any, status: Any) -> list[str]:
    """SHOW's persistent pills: `status.pills` (the controller's list, in its order), or, for
    an older status, the ones the state alone tells (LO-CUT OFF, POL INV, MONO SUB, AUDITION)."""
    raw = _get(status, "pills", None)
    if isinstance(raw, str):
        return [raw] if raw else []
    if raw is not None:
        return [str(p) for p in _seq(raw) if p]
    out: list[str] = []
    if _truth(_get(state, "lowcut_on", True)) is False:
        out.append("LO-CUT OFF")
    inv = [n for n, f in (("SUB", "sub_inverted"), ("TOP", "top_inverted"))
           if _truth(_get(state, f, False))]
    if inv:
        out.append("POL INV " + "+".join(inv))
    if _truth(_get(state, "mono_subs", False)):
        out.append("MONO SUB")
    aud = _get(state, "dev_audition", "sum")
    prof = str(_get(status, "profile", "") or _get(state, "profile", "") or "")
    if isinstance(aud, str) and aud and aud.lower() != "sum" and prof == "dev":
        out.append(f"AUDITION {aud.upper()}")
    return out


def spectrum_frame(status: Any) -> eg.RtaFrame | None:
    """The spectrum to draw behind the curve, validated (None = draw none): SETUP > Spectrum
    on, a frame whose freqs/level are 1-D, the same length, increasing, finite and positive.
    A NaN level reads as silence; a bad hold or peak is dropped, never the whole graph."""
    if _truth(_get(status, "spectrum_on", True)) is False:
        return None
    fr = _get(status, "spectrum", None)
    if fr is None:
        return None
    try:
        f = np.asarray(_get(fr, "freqs", None), dtype=np.float64)
        lv = np.asarray(_get(fr, "level", None), dtype=np.float64)
        if f.ndim != 1 or lv.shape != f.shape or len(f) < 2:
            return None
        if not (np.all(np.isfinite(f)) and f[0] > 0 and np.all(np.diff(f) > 0)):
            return None
        lv = np.where(np.isfinite(lv), lv, -140.0)
        hold = _get(fr, "hold", None)
        if hold is not None:
            hold = np.asarray(hold, dtype=np.float64)
            hold = np.where(np.isfinite(hold), hold, -140.0) if hold.shape == f.shape else None
        lo, hi = float(_get(fr, "lo_db", -90.0)), float(_get(fr, "hi_db", -30.0))
        if not (math.isfinite(lo) and math.isfinite(hi) and hi > lo):
            return None
        pk = _num(fr, "peak_hz", None)
        pk = pk if pk is not None and pk > 0 else None
        return eg.RtaFrame(f, lv, hold, lo, hi, pk, str(_get(fr, "source", "PGM") or "PGM"))
    except Exception:
        return None


_STATUS_FIELDS = ("engine", "device_present", "muted", "levels", "clip", "load")
_warned_shapes: set = set()


def _check_status_shape(status: Any) -> None:
    """Log once per status type that lacks the fields the screen reads (a naming mismatch with
    the controller would otherwise just show '?' and empty meters without telling anyone)."""
    if status is None:
        missing: tuple[str, ...] = ("<no status object>",)
    else:
        missing = tuple(f for f in _STATUS_FIELDS if _get(status, f, _MISSING) is _MISSING)
    if not missing:
        return
    key = (type(status).__name__, missing)
    if key not in _warned_shapes:
        _warned_shapes.add(key)
        log.warning("screen status %s lacks %s: shown as unknown", key[0], ", ".join(missing))


# ------------------------------------------------------------------------------------------
# Drawing helpers
# ------------------------------------------------------------------------------------------
@lru_cache(maxsize=128)
def font(size: int, face: str = FONT_BOLD) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(FONT_DIR + face, size)
    except OSError:  # fonts missing (dev machine): still render something
        return ImageFont.load_default(size)


def _fit(d: Any, text: str, size: int, max_w: int, face: str = FONT_BOLD):
    """Largest font <= size whose rendering of `text` fits `max_w` pixels."""
    return font(_fit_size(text, size, int(max_w), face), face)


@lru_cache(maxsize=1024)
def _fit_size(text: str, size: int, max_w: int, face: str) -> int:
    while size > 10:
        if _textlength(text, font(size, face)) <= max_w:
            return size
        size -= 2
    return 10


@lru_cache(maxsize=256)
def _fit_ellipsis(text: str, size: int, max_w: int, min_size: int = ALERT_MIN_PX,
                  face: str = FONT_BOLD) -> tuple[int, str]:
    """(font size, text) that fits `max_w`: the largest size from `size` down to `min_size`,
    and below that the text cut with an ellipsis at `min_size` (never smaller, never
    overflowing its box)."""
    s = size
    while s >= min_size:
        if _textlength(text, font(s, face)) <= max_w:
            return s, text
        s -= 1
    f = font(min_size, face)
    cut = text
    while cut and _textlength(cut.rstrip(" ·") + "…", f) > max_w:
        cut = cut[:-1]
    return min_size, (cut.rstrip(" ·") + "…") if cut else ""


@lru_cache(maxsize=2048)
def _textlength(text: str, f: ImageFont.FreeTypeFont) -> float:
    return f.getlength(text)


@lru_cache(maxsize=512)
def _text_mask(text: str, f: ImageFont.FreeTypeFont, anchor: str):
    """Rasterised text as an 'L' mask + its offset from the anchor point.

    Why: glyph rasterising is ~70 % of a frame. Most strings (labels, unchanged values) repeat
    from frame to frame — e.g. while only the meter or the spectrum moves — so caching them
    keeps a frame well under budget on the Pi 4.
    """
    left, top, right, bottom = f.getbbox(text, anchor=anchor)
    mask = Image.new("L", (max(1, right - left), max(1, bottom - top)), 0)
    ImageDraw.Draw(mask).text((-left, -top), text, font=f, fill=255, anchor=anchor)
    return mask, left, top


class _Draw:
    """ImageDraw with cached text rendering; every other method is ImageDraw's own."""

    def __init__(self, img: Image.Image):
        self._img = img
        self._d = ImageDraw.Draw(img)

    def text(self, xy, text: str, font: ImageFont.FreeTypeFont, fill, anchor: str = "la") -> None:
        if not text:
            return
        mask, left, top = _text_mask(text, font, anchor)
        self._img.paste(fill, (int(round(xy[0])) + left, int(round(xy[1])) + top), mask)

    def textlength(self, text: str, font: ImageFont.FreeTypeFont) -> float:
        return _textlength(text, font)

    def __getattr__(self, name: str):
        return getattr(self._d, name)


def fmt_db(v: float | None, signed: bool = True) -> str:
    if v is None:
        return "--"
    if abs(v) < 0.05:
        return "0.0"
    return f"{v:+.1f}" if signed else f"{v:.1f}"


def fmt_hz(v: float | None) -> str:
    if v is None:
        return "--"
    if v >= 10000:
        return f"{v / 1000:.1f}k"
    if v >= 1000:
        return f"{v / 1000:.2f}k"
    return f"{v:.0f}"


def fmt_ms(v: float | None) -> str:
    return "--" if v is None else f"{v:.2f}"


def fmt_q(v: float | None) -> str:
    if v is None:
        return "--"
    return f"{v:.1f}" if v >= 10 else f"{v:.2f}"


def fmt_oct(q: float | None) -> str:
    """A bell's width in octaves from its Q: 8.65 -> '1/6 oct', 1.41 -> '1.0 oct'."""
    if q is None or q <= 0:
        return ""
    o = 2.0 / math.log(2.0) * math.asinh(1.0 / (2.0 * q))
    n = 1.0 / o
    return f"1/{n:.0f} oct" if n >= 1.5 else f"{o:.1f} oct"


def fmt_mmss(seconds: float | None) -> str:
    """Seconds left as m:ss, rounded UP (the last second shows 0:01, never 0:00)."""
    s = max(0, int(math.ceil(seconds or 0.0)))
    return f"{s // 60}:{s % 60:02d}"


def fmt_hmm(seconds: float | None) -> str:
    """Seconds left as h:mm, minutes rounded UP ('LAPTOP OPEN 1:59'; the last minute 0:01)."""
    m = max(0, int(math.ceil((seconds or 0.0) / 60.0 - 1e-9)))
    return f"{m // 60}:{m % 60:02d}"


class _Ctx:
    """Per-frame drawing context: image, draw, time, focus snapshot."""

    def __init__(self, focus: Any, now: float):
        self.img = Image.new("RGB", SIZE, BG)
        self.d = _Draw(self.img)
        self.now = now
        self.focus = focus
        ed = getattr(focus, "editing_now", None)
        self.editing = ed(now) if callable(ed) else None
        held = getattr(focus, "held", ())
        self.held = set(held) if held else set()
        self.bottom_label_end: dict[str, float] = {}   # SHOW: where the bottom labels reach


def _hint(c: _Ctx) -> str | None:
    fn = getattr(c.focus, "hint_now", None)
    try:
        h = fn(c.now) if callable(fn) else None
    except Exception:
        return None
    return str(h) if h else None


def _focus_slot(focus: Any) -> int:
    try:
        return int(getattr(focus, "slot", 0) or 0) % len(LIVE_SLOTS)
    except (TypeError, ValueError):
        return 0


# ---- v0 quadrant layout (ALIGN) ------------------------------------------------------------
# Quadrant geometry: x range and the vertical anchors for label / value / sub-line.
_QX = {"l": (0, METER_X0), "r": (METER_X1, W)}
_QY = {  # (label_y, value_center_y, sub_y) ; label sits at the outer (screen) edge
    "t": (STATUS_H + 4, 92, 130),
    "b": (H - 26, 252, 210),
}


def _quadrant(c: _Ctx, e: int, label: str, value: str, unit: str, sub: str,
              field_key: str | None, *, dim: bool = False, value_size: int = 60,
              shift: int = 0, box_half: int = 38) -> None:
    """Draw one knob's quadrant: 'E1 MASTER' at the corner, big value, unit + sub-line.

    `shift` moves the value towards the screen edge; `box_half` is the half-height of the edit
    highlight.
    """
    corner = KNOB_CORNERS[e]
    vpos, hpos = corner[0], corner[1]
    x0, x1 = _QX[hpos]
    label_y, val_cy, sub_y = _QY[vpos]
    val_cy += -shift if vpos == "t" else shift
    d = c.d
    pad = 10
    # label at the outer corner
    tag = f"E{e + 1}"
    ftag, flab = font(16), font(20)
    if hpos == "l":
        d.text((x0 + pad, label_y), tag, font=ftag, fill=CYAN, anchor="la")
        d.text((x0 + pad + 30, label_y - 2), label, font=flab, fill=AMBER, anchor="la")
    else:
        d.text((x1 - pad, label_y), tag, font=ftag, fill=CYAN, anchor="ra")
        d.text((x1 - pad - 30, label_y - 2), label, font=flab, fill=AMBER, anchor="ra")
    # value (highlighted while being edited; switching the Lo-Cut highlights its frequency)
    edited = field_key is not None and (c.editing == field_key or (
        field_key == F_LOWCUT_FREQ and c.editing == F_LOWCUT_ON))
    fine = e in c.held
    box = (x0 + 4, val_cy - box_half, x1 - 4, val_cy + box_half)
    fs = _fit(d, sub, 18, (x1 - x0) - 2 * pad, FONT_REG) if sub else None
    if sub and fs is not None:
        # the edit box stops short of the sub-line ('≈ 0.81 m' under SUB DELAY), never half
        # over it: grey on amber, cut in two
        _l, st, _r, sb = d.textbbox(((x0 + x1) / 2, sub_y), sub, font=fs, anchor="mm")
        if vpos == "t":
            box = (box[0], box[1], box[2], min(box[3], st - 3))
        else:
            box = (box[0], max(box[1], sb + 3), box[2], box[3])
    if edited:
        d.rounded_rectangle(box, radius=8, fill=AMBER)
    fg = BG if edited else (DIM if dim else FG)
    unit_w = 0
    if unit:
        funit = font(20)
        unit_w = int(d.textlength(unit, font=funit)) + 6
    # FINE sits at the box's right edge: keep the value clear of it (a unit-less value such as
    # Q would otherwise run under it).
    fine_w = int(d.textlength("FINE", font=font(14))) + 12 if fine else 0
    room = (x1 - x0) - fine_w
    fval = _fit(d, value, value_size, room - 16 - unit_w)
    vw = d.textlength(value, font=fval)
    total = vw + unit_w
    vx = x0 + (room - total) / 2
    d.text((vx, val_cy), value, font=fval, fill=fg, anchor="lm")
    if unit:
        d.text((vx + vw + 6, val_cy + 14), unit, font=font(20), fill=BG if edited else DIM,
               anchor="ls")
    if fine:   # at the box's outer edge (top or bottom): the MUTED band never covers it
        fy, fa = (box[1] + 4, "ra") if vpos == "t" else (box[3] - 4, "rd")
        d.text((box[2] - 6, fy), "FINE", font=font(14), fill=BG if edited else CYAN, anchor=fa)
    if sub and fs is not None:
        d.text(((x0 + x1) / 2, sub_y), sub, font=fs, fill=DIM, anchor="mm")


# ---- status bar ----------------------------------------------------------------------------
def _full_width(alert: tuple[str, str]) -> bool:
    return alert[0] == ALERT_CRITICAL and alert[1].upper().startswith(FULL_WIDTH_ALERTS)


def _pill(c: _Ctx, x: float, text: str, fill, fg, outline=None) -> float:
    """A status-bar pill at x; returns the x after it (with a 6 px gap)."""
    d = c.d
    fp = font(16)
    w = int(d.textlength(text, font=fp)) + 12
    d.rounded_rectangle((x, 4, x + w, STATUS_H - 5), radius=4, fill=fill,
                        outline=outline, width=2 if outline else 1)
    d.text((x + w / 2, STATUS_H // 2), text, font=fp, fill=fg, anchor="mm")
    return x + w + 6


def _status_bar(c: _Ctx, page: str, state: Any, status: Any, muted: bool) -> None:
    """[PAGE] [MUTED] [IN CLIP] [CLIP] profile · device | alert ...... ● ENGINE  DSP load%

    A system alert (status.alerts[0]) takes the profile/device place in its level's colour
    (critical red, warning orange, info blue; never amber, which is the hints'), and the right
    side shrinks to the engine dot (plus the state when it is not RUNNING). KNOBS OFFLINE takes
    the whole bar, red. Unknown engine -> amber "ENGINE ?"; unknown device -> amber "... ?":
    never a healthy look for something nobody reported.
    """
    d = c.d
    cy = STATUS_H // 2
    alerts = screen_alerts(status)
    top = alerts[0] if alerts else None
    full = top is not None and _full_width(top)
    d.rectangle((0, 0, W, STATUS_H - 1), fill=RED if full else STATUS_BG)
    name = {"LIVE": "SHOW", "MAIN": "ALIGN", "HWTEST": "TEST"}.get(page, page)
    if top is not None:          # the alert gets the room: the page in 3 letters
        name = PAGE_SHORT.get(name, name[:3])
    lx = _pill(c, 4, name, FG, BG)
    if muted:   # on every page, whatever the body shows (outlined on a red bar)
        lx = _pill(c, lx, "MUTED", BAND_RED, FG, FG if full else None)
    if full:
        assert top is not None
        room = W - 8 - lx
        size, text = _fit_ellipsis(top[1], 18, int(room))
        d.text((lx + room / 2, cy), text, font=font(size), fill=FG, anchor="mm")
        return
    # A latched clip, readable at arm's length (the meter's clip boxes are small): CLIP = an
    # output reached full scale, IN CLIP = the input ADC did (no K1000 knob fixes that one).
    for t, on in (("IN CLIP", _truth(_get(status, "in_clip", None))),
                  ("CLIP", any(bool(_truth(v)) for v in _seq(_get(status, "clip", None))))):
        if on:
            lx = _pill(c, lx, t, RED, FG)
    # right side first, so the left text gets whatever room is left
    engine = engine_state(status)
    if engine is None:
        eng, col = "NO ENGINE", RED
    elif engine.upper() == ENGINE_UNKNOWN.upper():
        eng, col = "ENGINE ?", AMBER
    else:
        eng = engine.upper()
        col = GREEN if eng == "RUNNING" else (YELLOW if eng in ("PAUSED", "STARTING") else RED)
    x = W - 8
    if top is None:
        load = _num(status, "load", None)
        if load is not None:
            right = f"{load:.1f}%"
            d.text((x, cy), right, font=font(17), fill=FG, anchor="rm")
            x -= int(d.textlength(right, font=font(17))) + 4
            d.text((x, cy + 1), "DSP", font=font(12), fill=DIM, anchor="rm")
            x -= 38
    if top is None or eng != "RUNNING":
        fe = _fit(d, eng, 17, 150)
        d.text((x, cy), eng, font=fe, fill=col, anchor="rm")
        x -= int(d.textlength(eng, font=fe)) + 8
    d.ellipse((x - 12, cy - 6, x, cy + 6), fill=col)
    x -= 12
    if top is not None:
        fill, fg = ALERT_STYLE.get(top[0], ALERT_STYLE[ALERT_WARNING])
        x1 = x - 8
        if x1 - lx > 24:
            d.rounded_rectangle((lx, 3, x1, STATUS_H - 4), radius=4, fill=fill)
            size, text = _fit_ellipsis(top[1], 17, int(x1 - lx - 10))
            d.text(((lx + x1) / 2, cy), text, font=font(size), fill=fg, anchor="mm")
        return
    profile = str(_get(status, "profile", "") or _get(state, "profile", "") or "?")
    device = str(_get(status, "device", "") or "")
    present = device_present(status)
    if present is None:
        left = profile.upper() + (f" · {device} ?" if device else " · DEVICE ?")
        lcol = AMBER
    else:
        left = profile.upper() + (f" · {device}" if device else "")
        lcol = FG if present else ORANGE
    lx += 2
    room = x - 8 - lx
    # Pills (MUTED, CLIP...) come first; the profile text shrinks, then loses the device name,
    # then goes: it never overprints the engine state.
    for text in (left, left.split(" · ")[0]):
        f = _fit(d, text, 17, max(1, room))
        if room > 0 and d.textlength(text, font=f) <= room:
            d.text((lx, cy), text, font=f, fill=lcol, anchor="lm")
            break


# ---- meters --------------------------------------------------------------------------------
def _level(levels: list, i: int) -> float:
    lv = levels[i] if i < len(levels) else None
    try:
        lv = float(lv) if lv is not None else METER_FLOOR_DB
    except (TypeError, ValueError):
        return METER_FLOOR_DB
    return lv if math.isfinite(lv) else METER_FLOOR_DB


_METER_ZONES = ((METER_FLOOR_DB, -18.0, GREEN), (-18.0, -6.0, YELLOW), (-6.0, 0.0, RED))


def _meter(c: _Ctx, status: Any, y0: int = STATUS_H + 8, y1: int = H - 30) -> None:
    """ALIGN: 4 vertical peak bars (Sub L, Sub R, Top L, Top R), -60..0 dBFS, clip box on top."""
    d = c.d
    levels = _seq(_get(status, "levels", None))     # numpy arrays welcome (no `x or ()`)
    clips = _seq(_get(status, "clip", None))
    bw, gap = 11, 5
    x = (METER_X0 + METER_X1) / 2 - (4 * bw + 3 * gap) / 2 + 1
    clip_h = 10
    top = y0 + clip_h + 4
    span = y1 - top

    def y_of(db: float) -> float:
        db = max(METER_FLOOR_DB, min(0.0, db))
        return y1 - span * (db - METER_FLOOR_DB) / -METER_FLOOR_DB

    for db_tick in (-6, -20, -40):
        yt = y_of(db_tick)
        d.line((METER_X0 + 4, yt, METER_X1 - 4, yt), fill=FAINT)
    for i in range(4):
        xi = int(x + i * (bw + gap)) + (3 if i >= 2 else 0) - 1
        lv = _level(levels, i)
        d.rectangle((xi, top, xi + bw - 1, y1), fill=BAR_BG)
        if lv > METER_FLOOR_DB:
            for lo, hi, col in _METER_ZONES:
                if lv <= lo:
                    break
                d.rectangle((xi, y_of(min(lv, hi)), xi + bw - 1, y_of(lo)), fill=col)
        clipped = bool(_truth(clips[i])) if i < len(clips) else False
        if clipped:
            d.rectangle((xi - 1, y0, xi + bw, y0 + clip_h), fill=RED)
        else:
            d.rectangle((xi, y0, xi + bw - 1, y0 + clip_h), outline=FAINT)
    # labels: SUB under bars 0-1, TOP under 2-3
    fl = font(13)
    cx_sub = x + bw + gap / 2 - 1
    cx_top = x + 2 * (bw + gap) + bw + gap / 2 + 2
    # (kept clear of the 4 px MUTED frame at the bottom edge)
    d.text((cx_sub, y1 + 2), "SUB", font=fl, fill=DIM, anchor="ma")
    d.text((cx_top, y1 + 2), "TOP", font=fl, fill=DIM, anchor="ma")
    d.text((cx_sub, y1 + 13), "L R", font=font(11), fill=FAINT, anchor="ma")
    d.text((cx_top, y1 + 13), "L R", font=font(11), fill=FAINT, anchor="ma")


def _mini_meter(c: _Ctx, status: Any) -> None:
    """SHOW: the same 4 bars (Sub L R, Top L R) compacted into METER_BOX, top centre."""
    d = c.d
    x0, y0, x1, y1 = METER_BOX
    levels = _seq(_get(status, "levels", None))
    clips = _seq(_get(status, "clip", None))
    bw, gap, pair = 12, 3, 9
    total = 4 * bw + 2 * gap + pair
    bx = (x0 + x1) / 2 - total / 2
    top, bot = y0 + 9, y1 - 13

    def y_of(db: float) -> float:
        db = max(METER_FLOOR_DB, min(0.0, db))
        return bot - (bot - top) * (db - METER_FLOOR_DB) / -METER_FLOOR_DB

    for i in range(4):
        xi = int(bx + i * (bw + gap) + (pair - gap if i >= 2 else 0))
        lv = _level(levels, i)
        d.rectangle((xi, top, xi + bw - 1, bot), fill=BAR_BG)
        if lv > METER_FLOOR_DB:
            for lo, hi, col in _METER_ZONES:
                if lv <= lo:
                    break
                d.rectangle((xi, y_of(min(lv, hi)), xi + bw - 1, y_of(lo)), fill=col)
        clipped = bool(_truth(clips[i])) if i < len(clips) else False
        if clipped:
            d.rectangle((xi - 1, y0, xi + bw, y0 + 6), fill=RED)
        else:
            d.rectangle((xi, y0, xi + bw - 1, y0 + 6), outline=FAINT)
    fl = font(11)
    d.text((bx + bw + gap / 2, bot + 2), "SUB", font=fl, fill=DIM, anchor="ma")
    d.text((bx + 3 * bw + 2 * gap + pair - gap / 2 + 0.5 * gap, bot + 2), "TOP", font=fl,
           fill=DIM, anchor="ma")


def _middle(c: _Ctx, status: Any = None) -> None:
    """ALIGN's middle strip: a hint (amber, brief). System alerts are in the status bar."""
    d = c.d
    cy = (MID_Y0 + MID_Y1) // 2
    hint = _hint(c)
    if hint:
        d.rectangle((0, cy - 18, W, cy + 18), fill=AMBER)
        d.text((W // 2, cy), hint, font=_fit(d, hint, 24, W - 20), fill=BG, anchor="mm")


# ------------------------------------------------------------------------------------------
# SHOW
# ------------------------------------------------------------------------------------------
def _block(c: _Ctx, e: int, label: str, value: str, unit: str, field_key: str | None, *,
           size: int = 44, dim: bool = False, tag: str = "", tag_hot: bool = False) -> None:
    """One encoder's block on SHOW: 'E1 MASTER' on the outer edge, the value in a box next to
    its knob (amber while edited, FINE while its push is held). `tag` (the band's name) sits
    next to the label, amber-filled right after the band changed."""
    corner = KNOB_CORNERS[e]
    top = corner[0] == "t"
    left = corner[1] == "l"
    if top:
        x0, x1 = (0, METER_BOX[0] - 4) if left else (METER_BOX[2] + 4, W)
        label_y, (by0, by1) = TOP_Y0 + 3, TOP_BOX
    else:
        x0, x1 = (0, W // 2 - 2) if left else (W // 2 + 2, W)
        label_y, (by0, by1) = H - 23, BOT_BOX
    d = c.d
    pad = 10
    ftag, flab, fx = font(16), font(20), font(16)
    lab_col = DIM if dim else AMBER
    lw = d.textlength(label, font=flab)
    if left:
        d.text((x0 + pad, label_y), f"E{e + 1}", font=ftag, fill=CYAN, anchor="la")
        d.text((x0 + pad + 30, label_y - 2), label, font=flab, fill=lab_col, anchor="la")
        end = x0 + pad + 30 + lw
        if tag:
            tx = end + 8
            tw = d.textlength(tag, font=fx)
            if tag_hot:
                d.rounded_rectangle((tx - 4, label_y - 2, tx + tw + 4, label_y + 16), radius=3,
                                    fill=AMBER)
            d.text((tx, label_y), tag, font=fx, fill=BG if tag_hot else FG, anchor="la")
            end = tx + tw
        if not top:
            c.bottom_label_end["l"] = end
    else:
        d.text((x1 - pad, label_y), f"E{e + 1}", font=ftag, fill=CYAN, anchor="ra")
        d.text((x1 - pad - 30, label_y - 2), label, font=flab, fill=lab_col, anchor="ra")
        end = x1 - pad - 30 - lw
        if tag:
            tw = d.textlength(tag, font=fx)
            tx = end - 8
            if tag_hot:
                d.rounded_rectangle((tx - tw - 4, label_y - 2, tx + 4, label_y + 16), radius=3,
                                    fill=AMBER)
            d.text((tx, label_y), tag, font=fx, fill=BG if tag_hot else FG, anchor="ra")
            end = tx - tw
        if not top:
            c.bottom_label_end["r"] = end
    cy = (by0 + by1) // 2
    edited = field_key is not None and c.editing == field_key
    fine = e in c.held
    if edited:
        d.rounded_rectangle((x0 + 4, by0, x1 - 4, by1), radius=8, fill=AMBER)
    fg = BG if edited else (DIM if dim else FG)
    funit = font(18)
    unit_w = int(d.textlength(unit, font=funit)) + 6 if unit else 0
    # FINE has its own column at the box's right edge (the value and its unit stay left of it)
    # and sits at the box's top, clear of the unit's baseline row.
    fine_w = int(d.textlength("FINE", font=font(14))) + 16 if fine else 0
    room = (x1 - x0) - fine_w
    fval = _fit(d, value, size, room - 16 - unit_w)
    vw = d.textlength(value, font=fval)
    vx = x0 + (room - (vw + unit_w)) / 2
    d.text((vx, cy), value, font=fval, fill=fg, anchor="lm")
    if unit:
        d.text((vx + vw + 6, cy + 13), unit, font=funit, fill=BG if edited else DIM, anchor="ls")
    if fine:
        d.text((x1 - 10, by0 + 4), "FINE", font=font(14), fill=BG if edited else CYAN, anchor="ra")


def _chips(c: _Ctx, sel: int, bands: Sequence[eg.Band]) -> None:
    """LS · 1 · 2 · 3 · 4 · HS (B3's order): the selected band amber, a band in use outlined in
    its cut (orange) / boost (green) colour, an idle band faint."""
    d = c.d
    n, gap = len(BAND_CYCLE), 4
    w = (W - 8 - gap * (n - 1)) / n
    for k, slot in enumerate(BAND_CYCLE):
        bx0 = round(4 + k * (w + gap))
        bx1 = round(4 + k * (w + gap) + w)
        box = (bx0, CHIP_Y0, bx1, CHIP_Y1)
        g = bands[slot].gain if slot < len(bands) else 0.0
        active = abs(g) >= 0.05
        acol = CUT if g < 0 else GREEN
        label = CHIP_TEXT.get(slot, "?")
        cxy = ((bx0 + bx1) / 2, (CHIP_Y0 + CHIP_Y1) / 2)
        if slot == sel:
            d.rounded_rectangle(box, radius=4, fill=AMBER)
            d.text(cxy, label, font=font(20), fill=BG, anchor="mm")
        else:
            d.rounded_rectangle(box, radius=4, outline=acol if active else FAINT,
                                width=2 if active else 1)
            d.text(cxy, label, font=font(20), fill=acol if active else DIM, anchor="mm")


def _peak_label(c: _Ctx, geo: dict[str, Any], rta: eg.RtaFrame | None) -> None:
    """The PEAK's frequency next to its cyan pointer (D-009: a pointer and a frequency, no
    call to action), on a black patch so it reads over the spectrum."""
    x = geo.get("peak_x")
    if rta is None or not rta.peak_hz or x is None:
        return
    d = c.d
    t = fmt_hz(rta.peak_hz)
    f = font(16)
    tw = d.textlength(t, font=f)
    tx = x + 10 if x + 10 + tw <= W - 6 else x - 10 - tw
    y0 = GRAPH[1] + 1
    d.rectangle((tx - 3, y0, tx + tw + 3, y0 + 14), fill=BG)
    d.text((tx, y0 + 7), t, font=f, fill=CYAN, anchor="lm")


def _spectrum_tag(c: _Ctx, status: Any, rta: eg.RtaFrame | None) -> None:
    """Says so when the spectrum is switched on but the engine does not give one."""
    if rta is not None or _truth(_get(status, "spectrum_on", True)) is False:
        return
    if str(_get(status, "spectrum_state", "") or "") != "unavailable":
        return
    c.d.text((W - 8, GRAPH[1] + 4), "NO SPECTRUM", font=font(13), fill=DIM, anchor="ra")


def _risky_pill(text: str) -> bool:
    return str(text).upper().startswith(RISKY_PILLS)


def pill_layout(pills: Sequence[str], room: float, gap: int = 6) -> tuple[list[str], list[str], int]:
    """(pills shown, pills folded into '+N', font size) for SHOW's pill row `room` px wide:
    shrink from 14 to PILL_MIN_PX, then fold the last neutral pill, and a risky one only when
    no neutral one is left."""
    shown, folded, size = [str(p) for p in pills], [], 14
    while True:
        f = font(size)
        labels = shown + ([f"+{len(folded)}"] if folded else [])
        total = sum(_textlength(p, f) + 12 for p in labels) + gap * (len(labels) - 1)
        if total <= room or not shown:
            return shown, folded, size
        if size > PILL_MIN_PX:
            size -= 1
            continue
        k = next((i for i in range(len(shown) - 1, -1, -1) if not _risky_pill(shown[i])),
                 len(shown) - 1)
        folded.append(shown.pop(k))


def _pills_row(c: _Ctx, pills: Sequence[str], x0: float, x1: float) -> None:
    """Persistent pills centred between x0 and x1 on SHOW's bottom row, in the controller's
    order. A pill that changes the sound or opens the box (RISKY_PILLS: LAPTOP OPEN, LO-CUT
    OFF, POL INV) is orange (outline and text); MONO SUB (Thomas's normal state) and AUDITION
    stay grey. They shrink to PILL_MIN_PX, then fold into a '+N' pill: the last neutral ones
    first, a risky one only when no neutral one is left (LAPTOP OPEN, first, folds last). The
    '+N' is orange when it hides a risky pill."""
    if not pills or x1 - x0 < 40:
        return
    d = c.d
    gap = 6
    room = x1 - x0
    shown, folded, size = pill_layout(pills, room)
    f = font(size)
    labels = shown + ([f"+{len(folded)}"] if folded else [])
    widths = [d.textlength(p, font=f) + 12 for p in labels]
    total = sum(widths) + gap * (len(widths) - 1)
    if not labels or total > room:
        return
    x = x0 + (room - total) / 2
    for p, w in zip(labels, widths):
        risky = _risky_pill(p) if not p.startswith("+") else any(map(_risky_pill, folded))
        if risky:
            d.rounded_rectangle((x, PILL_Y0, x + w, PILL_Y1), radius=4, fill=BG, outline=ORANGE,
                                width=2)
        else:
            d.rounded_rectangle((x, PILL_Y0, x + w, PILL_Y1), radius=4, fill=PILL_BG,
                                outline=PILL_EDGE)
        d.text((x + w / 2, (PILL_Y0 + PILL_Y1) / 2 + 1), p, font=f, fill=ORANGE if risky else FG,
               anchor="mm")
        x += w + gap


def _strip(c: _Ctx, text: str, fill=AMBER, fg=BG) -> None:
    """A hint (amber) over SHOW's chip row: the marker and the GAIN label still say the band.
    A critical system alert takes it in its own colour when no hint is up (_page_show)."""
    d = c.d
    d.rectangle((0, CHIP_Y0, W, CHIP_Y1), fill=fill)
    size, text = _fit_ellipsis(text, 20, W - 16)
    d.text((W // 2, (CHIP_Y0 + CHIP_Y1) // 2 + 1), text, font=font(size), fill=fg, anchor="mm")


def _page_show(c: _Ctx, state: Any, status: Any) -> None:
    """SHOW: E1 MASTER, E2/E3/E4 = FREQ/GAIN/Q of the selected band, chips, meter, and the
    live-EQ curve (the engine's own biquads, eqgraph) over the program spectrum."""
    slot = _focus_slot(c.focus)
    bands = eg.live_bands(state)
    rta = spectrum_frame(status)
    # the graph first, so the blocks, chips and strips draw over its edges
    geo = eg.draw_graph(c.img, GRAPH, bands, slot, rta)
    _peak_label(c, geo, rta)
    _spectrum_tag(c, status, rta)
    _chips(c, slot, bands)
    labels = knob_labels("SHOW")
    base = LIVE_SLOTS[slot]
    _block(c, 0, labels[0], fmt_db(_num(state, F_MASTER)), "dB", F_MASTER, size=44)
    _mini_meter(c, status)
    _block(c, 2, labels[2], fmt_db(_num(state, base + ".gain")), "dB", base + ".gain", size=44,
           tag=LIVE_SLOT_LABELS[slot], tag_hot=c.editing == F_SLOT)
    _block(c, 1, labels[1], fmt_hz(_num(state, base + ".freq")), "Hz", base + ".freq", size=40)
    q = _num(state, base + ".q")
    if slot < 4:
        _block(c, 3, labels[3], fmt_q(q), fmt_oct(q), base + ".q", size=40)
    else:     # shelves: the same knob is the slope (a Q of 0.4..1.5)
        _block(c, 3, "SLOPE", fmt_q(q), "Q", base + ".q", size=40)
    lx = c.bottom_label_end.get("l", 110.0) + 12
    rx = c.bottom_label_end.get("r", W - 70.0) - 12
    _pills_row(c, screen_pills(state, status), lx, rx)
    hint = _hint(c)
    if hint:
        _strip(c, hint)
    else:
        # A critical system alert (KNOBS OFFLINE and its instruction, CANNOT SAVE...) is also
        # said here, big enough for arm's length, in its own colour (never amber: the hints').
        # Warnings stay in the status bar only: some last a whole gig (a cut kept from
        # CamillaGUI) and would hide the band chips for it.
        crit = [a for a in screen_alerts(status) if a[0] == ALERT_CRITICAL]
        if crit:
            fill, fg = ALERT_STYLE[ALERT_CRITICAL]
            _strip(c, crit[0][1], fill, fg)


# ------------------------------------------------------------------------------------------
# ALIGN
# ------------------------------------------------------------------------------------------
def align_unlock_left(c: _Ctx, status: Any) -> float:
    """Seconds left of ALIGN's E3/E4 unlock: the status's figure or the focus's own deadline,
    whichever says more (0 = locked)."""
    s = _num(status, "align_unlock_s", 0.0) or 0.0
    fn = getattr(c.focus, "unlock_left", None)
    try:
        f = float(fn(c.now)) if callable(fn) else 0.0
    except Exception:
        f = 0.0
    if _truth(_get(status, "align_locked", None)) is True and f <= 0.0:
        return 0.0
    return max(0.0, s, f)


def _lock_badge(c: _Ctx, left_s: float) -> None:
    """Between E3 (SUB DELAY) and E4 (LO-CUT), in their column: LOCKED (grey, how to unlock)
    or EDIT m:ss (amber, the unlock's countdown)."""
    d = c.d
    corner3, corner4 = KNOB_CORNERS[2][1], KNOB_CORNERS[3][1]
    x0, x1 = _QX[corner3] if corner3 == corner4 else (0, W)
    cx, cy = (x0 + x1) / 2, (MID_Y0 + MID_Y1) // 2 - 5
    if left_s > 0:
        t, f = f"EDIT {fmt_mmss(left_s)}", font(20)
        tw = d.textlength(t, font=f)
        d.rounded_rectangle((cx - tw / 2 - 10, cy - 15, cx + tw / 2 + 10, cy + 15), radius=6,
                            fill=AMBER)
        d.text((cx, cy), t, font=f, fill=BG, anchor="mm")
    else:
        t = "LOCKED · HOLD B3"
        f = _fit(d, t, 16, (x1 - x0) - 30)
        tw = d.textlength(t, font=f)
        d.rounded_rectangle((cx - tw / 2 - 9, cy - 13, cx + tw / 2 + 9, cy + 13), radius=6,
                            outline=DIM, width=2)
        d.text((cx, cy), t, font=f, fill=DIM, anchor="mm")


def _page_align(c: _Ctx, state: Any, status: Any) -> None:
    """ALIGN: E1 MASTER and E2 BALANCE live; E3 SUB DELAY and E4 LO-CUT dimmed while locked
    (a B3 hold unlocks them for 30 s of idle: the EDIT badge counts it down). Lo-Cut on/off
    is a SETUP item now: said here, not offered."""
    master = _num(state, F_MASTER)
    bal = _num(state, F_BALANCE)
    sd = _num(state, F_SUB_DELAY)
    lc_on = _truth(_get(state, "lowcut_on", True)) is not False
    lc = _num(state, F_LOWCUT_FREQ)
    left = align_unlock_left(c, status)
    locked = left <= 0.0
    labels = knob_labels("ALIGN")
    _quadrant(c, 0, labels[0], fmt_db(master), "dB", "", F_MASTER)
    bsub = "subs = tops" if bal is None or abs(bal) < 0.05 else \
        f"subs {bal / 2:+.1f} · tops {-bal / 2:+.1f}"
    _quadrant(c, 1, labels[1], fmt_db(bal), "dB", bsub, F_BALANCE)
    _quadrant(c, 2, labels[2], fmt_ms(sd), "ms", f"≈ {sd * 0.343:.2f} m" if sd is not None else "",
              F_SUB_DELAY, dim=locked)
    if lc_on:
        _quadrant(c, 3, labels[3], fmt_hz(lc), "Hz", "tops · 12 dB/oct", F_LOWCUT_FREQ, dim=locked)
    else:
        _quadrant(c, 3, labels[3], "OFF", "", f"{fmt_hz(lc)} Hz · on/off in SETUP", F_LOWCUT_FREQ,
                  dim=True)
    _meter(c, status)
    _lock_badge(c, left)
    _middle(c, status)


# ------------------------------------------------------------------------------------------
# SETUP
# ------------------------------------------------------------------------------------------
def _setup_value(state: Any, item, status: Any = None) -> str:
    if item.kind == "page":
        return "PUSH ▸"
    if item.kind == "action":
        if item.field == A_UNDO_RESET:
            left = _num(status, "undo_reset_s", 0.0) or 0.0
            return f"UNDO {fmt_mmss(left)}" if left > 0 else "—"
        if item.field == A_LAPTOP:
            return _laptop_value(status)
        return "PUSH ×2" if getattr(item, "confirm", False) else "PUSH ▸"
    if item.field == F_SPECTRUM_ON and _get(status, "spectrum_state", "") == "disabled":
        return "DISABLED"
    v = _get(state, item.field, None)
    if v is None:
        return "--"
    if item.field.endswith("_inverted"):
        return "INVERTED" if v else "normal"
    if isinstance(v, bool):
        return "ON" if v else "off"
    if isinstance(v, str):
        return v.upper()
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return str(v)
    if item.field.endswith("_ms"):
        return f"{fv:.2f} ms"
    if item.field.endswith("_hz"):
        return f"{fmt_hz(fv)} Hz"
    return f"{fmt_db(fv)} dB"


def _laptop_value(status: Any) -> str:
    """SETUP > Laptop access: 'OPEN 1:59' (a push closes), 'OPENING…', 'PUSH ×2', 'N/A'."""
    left = _num(status, "laptop_s", 0.0) or 0.0
    state = _get(status, "laptop_state", "off")
    if state == "open" and left > 0:
        return f"OPEN {fmt_hmm(left)}"
    if state == "opening":
        return "OPENING…"
    if state == "closing":
        return "CLOSING…"
    if state in ("off", "unavailable"):
        return "N/A"
    return "PUSH ×2"


def _setup_unavailable(item, status: Any) -> bool:
    """A row with nothing to do right now (Undo reset with no undo, Laptop access without its
    surface): its label is dimmed."""
    if item.kind == "action" and item.field == A_UNDO_RESET:
        return not (_num(status, "undo_reset_s", 0.0) or 0.0) > 0
    if item.kind == "action" and item.field == A_LAPTOP:
        return _get(status, "laptop_state", "off") in ("off", "unavailable")
    return item.field == F_SPECTRUM_ON and _get(status, "spectrum_state", "") == "disabled"


def _page_setup(c: _Ctx, state: Any, status: Any) -> None:
    d = c.d
    labels = knob_labels("SETUP")
    # corner labels (E1 top-left, E2 bottom-left as per KNOB_CORNERS) on a header/footer row
    for e, lab in enumerate(labels):
        if not lab:
            continue
        corner = KNOB_CORNERS[e]
        y = STATUS_H + 4 if corner[0] == "t" else H - 24
        if corner[1] == "l":
            d.text((10, y), f"E{e + 1}", font=font(16), fill=CYAN, anchor="la")
            d.text((40, y - 2), lab, font=font(20), fill=AMBER, anchor="la")
        else:
            d.text((W - 10, y), f"E{e + 1}", font=font(16), fill=CYAN, anchor="ra")
            d.text((W - 40, y - 2), lab, font=font(20), fill=AMBER, anchor="ra")
    d.text((W - 10, H - 22), "push = select", font=font(16, FONT_REG), fill=DIM, anchor="ra")
    try:
        sel = max(0, min(len(SETUP_ITEMS) - 1, int(getattr(c.focus, "item", 0) or 0)))
    except (TypeError, ValueError):
        sel = 0
    conf_fn = getattr(c.focus, "confirm_now", None)
    confirm = conf_fn(c.now) if callable(conf_fn) else None
    d.text((W - 10, STATUS_H + 4), f"{sel + 1}/{len(SETUP_ITEMS)}", font=font(18), fill=DIM,
           anchor="ra")
    rows, row_h, y0 = 8, 28, STATUS_H + 30
    first = max(0, min(sel - rows // 2, len(SETUP_ITEMS) - rows))
    for r in range(rows):
        i = first + r
        if i >= len(SETUP_ITEMS):
            break
        item = SETUP_ITEMS[i]
        y = y0 + r * row_h
        is_sel = i == sel
        pending = is_sel and confirm == item.field
        if is_sel:
            d.rectangle((4, y, W - 4, y + row_h - 3), fill=RED if pending else AMBER)
        fg = FG if pending else (BG if is_sel else (DIM if _setup_unavailable(item, status) else FG))
        d.text((14, y + row_h // 2 - 1), item.label,
               font=font(20, FONT_BOLD if is_sel else FONT_REG), fill=fg, anchor="lm")
        val = "PUSH AGAIN" if pending else _setup_value(state, item, status)
        vcol = fg if is_sel else (AMBER if item.kind == "rebuild" else fg)
        if not is_sel and item.field == A_LAPTOP and val.startswith("OPEN "):
            vcol = AMBER                       # a gate open in the fence: never quiet
        d.text((W - 14, y + row_h // 2 - 1), val, font=font(20), fill=vcol, anchor="rm")
    # scroll marks
    if first > 0:
        d.polygon([(W // 2 - 8, y0 - 4), (W // 2 + 8, y0 - 4), (W // 2, y0 - 12)], fill=DIM)
    if first + rows < len(SETUP_ITEMS):
        yb = y0 + rows * row_h
        d.polygon([(W // 2 - 8, yb), (W // 2 + 8, yb), (W // 2, yb + 8)], fill=DIM)
    hint = _hint(c)
    if hint:
        d.rectangle((0, H - FOOTER_H, W, H), fill=AMBER)
        d.text((W // 2, H - FOOTER_H // 2), hint, font=_fit(d, hint, 20, W - 20), fill=BG,
               anchor="mm")


# ------------------------------------------------------------------------------------------
# HWTEST (unchanged from v0)
# ------------------------------------------------------------------------------------------
def _page_hwtest(c: _Ctx, state: Any, status: Any) -> None:
    d = c.d
    hw = getattr(c.focus, "hw", None)
    d.text((W // 2, STATUS_H + 6), "KNOB TEST", font=font(24), fill=AMBER, anchor="ma")
    sub = "turn / press each control · hold B2 = mute · hold any other = exit"
    d.text((W // 2, STATUS_H + 36), sub, font=_fit(d, sub, 15, W - 24, FONT_REG), fill=DIM,
           anchor="ma")
    counts = list(getattr(hw, "enc_count", [0] * 4)) if hw else [0] * 4
    down = list(getattr(hw, "sw_down", [False] * 8)) if hw else [False] * 8
    last_kind = list(getattr(hw, "sw_last", [""] * 8)) if hw else [""] * 8
    enc_role = list(getattr(hw, "enc_role", ["E1", "E2", "E3", "E4"])) if hw else ["?"] * 4
    sw_role = list(getattr(hw, "sw_role", [""] * 8)) if hw else [""] * 8
    # encoders row
    bw = 108
    for i in range(4):
        x = 12 + i * (bw + 8)
        y = STATUS_H + 62
        d.rounded_rectangle((x, y, x + bw, y + 64), radius=6, outline=FAINT, width=2)
        d.text((x + 8, y + 6), f"enc{i}", font=font(16), fill=CYAN, anchor="la")
        role = enc_role[i] if i < len(enc_role) else ""
        d.text((x + bw - 8, y + 6), role, font=font(16), fill=AMBER, anchor="ra")
        d.text((x + bw / 2, y + 42), f"{counts[i]:+d}", font=font(28), fill=FG, anchor="mm")
    # switches: 2 rows of 4
    sw = 108
    for i in range(8):
        x = 12 + (i % 4) * (sw + 8)
        y = STATUS_H + 136 + (i // 4) * 46
        pressed = down[i] if i < len(down) else False
        d.rounded_rectangle((x, y, x + sw, y + 40), radius=6,
                            fill=AMBER if pressed else None, outline=FAINT, width=2)
        col = BG if pressed else FG
        d.text((x + 8, y + 12), f"sw{i}", font=font(17), fill=col, anchor="lm")
        role = sw_role[i] if i < len(sw_role) else ""
        d.text((x + 8, y + 30), role, font=font(13), fill=BG if pressed else AMBER, anchor="lm")
        lk = last_kind[i] if i < len(last_kind) else ""
        if lk:
            d.text((x + sw - 8, y + 12), lk, font=font(14, FONT_REG), fill=col, anchor="rm")
    last = getattr(hw, "last", "") if hw else ""
    d.text((W // 2, H - 22), last or "waiting for input…", font=font(22),
           fill=FG if last else DIM, anchor="mm")


# ------------------------------------------------------------------------------------------
# Overlays
# ------------------------------------------------------------------------------------------
def _overlay_no_device(c: _Ctx, state: Any, status: Any) -> None:
    d = c.d
    d.rectangle((0, STATUS_H, W, H), fill=(40, 18, 0))
    d.rectangle((0, STATUS_H, W, STATUS_H + 6), fill=ORANGE)
    device = str(_get(status, "device", "") or "audio interface")
    d.text((W // 2, 90), "NO DEVICE", font=font(54), fill=ORANGE, anchor="mm")
    line = f"{device} not found"
    d.text((W // 2, 160), line, font=_fit(d, line, 32, W - 30), fill=FG, anchor="mm")
    d.text((W // 2, 202), "check USB cable and power", font=font(24), fill=FG, anchor="mm")
    prof = str(_get(status, "profile", "") or _get(state, "profile", "") or "")
    note = f"profile {prof} is pinned: no fallback" if prof else "no fallback device"
    d.text((W // 2, 252), note, font=font(18, FONT_REG), fill=DIM, anchor="mm")
    d.text((W // 2, 286), "NO AUDIO — bypass if needed", font=font(20), fill=ORANGE,
           anchor="mm")


def _overlay_engine_down(c: _Ctx, state: Any, status: Any) -> None:
    d = c.d
    d.rectangle((0, STATUS_H, W, H), fill=(48, 0, 0))
    d.rectangle((0, STATUS_H, W, STATUS_H + 6), fill=RED)
    d.text((W // 2, 92), "ENGINE DOWN", font=font(52), fill=RED, anchor="mm")
    engine = _get(status, "engine", None)
    reason = str(_get(status, "stop_reason", "") or "")
    line = ("no connection to CamillaDSP" if engine is None else f"CamillaDSP {str(engine).lower()}")
    d.text((W // 2, 160), line, font=_fit(d, line, 28, W - 30), fill=FG, anchor="mm")
    if reason:
        d.text((W // 2, 200), reason, font=_fit(d, reason, 22, W - 30, FONT_REG), fill=FG,
               anchor="mm")
    # Stalled = the capture gives no data: the engine waits for it, nothing restarts it.
    stalled = engine is not None and str(engine).strip().upper() == "STALLED"
    note = "no input data · check the interface / USB" if stalled else "restarting automatically…"
    d.text((W // 2, 248), note, font=_fit(d, note, 20, W - 30, FONT_REG), fill=DIM,
           anchor="mm")
    tail = ("audio may be off — bypass if it persists" if engine is None
            else "NO AUDIO — bypass if it persists")
    d.text((W // 2, 286), tail, font=_fit(d, tail, 20, W - 20), fill=RED, anchor="mm")


def _mute_hint(c: _Ctx, state: Any, status: Any) -> tuple[str | None, str]:
    """(active hint or None, what the MUTED band says next to MUTED).

    The band must never tell the operator something false about what happens next:
    - K1000 itself holds Main muted on its way to a boot ramp (and the operator did not mute):
      it comes in BY ITSELF, and B2 keeps it muted (the controller resolves B2 that way);
    - a controller alert (APPLYING SETUP…, NO AUDIO FLOWING · HELD MUTED, ENGINE SYNC FAILED,
      KNOBS OFFLINE…) says why, instead of READY;
    - READY only when the engine RUNS (not Paused/Starting) and the controller is in sync.
    """
    hint = _hint(c)
    engine = engine_state(status)
    running = engine is not None and engine.upper() == "RUNNING"
    if _truth(_get(status, "ramping", None)) and _truth(_get(state, "muted", None)) is False:
        # (the status bar says whether the engine is processing yet: STARTING / RUNNING)
        return hint, "RAMPS IN BY ITSELF · hold B2 to keep muted"
    alert = str(_get(status, "alert", "") or "")
    if alert:
        return hint, alert
    ready = running and _truth(_get(status, "ready", None)) is not False
    return hint, ("READY · hold B2 to unmute" if ready else "hold B2 to unmute")


def _band_text(c: _Ctx, y0: int, y1: int, big: int, line1: int, line2: int, small: str) -> None:
    """MUTED (big) at the left of a red band, the hint / unmute line(s) at its right."""
    d = c.d
    cy = (y0 + y1) // 2
    left = 16 if big < 50 else 20
    d.text((left, cy), "MUTED", font=font(big), fill=FG, anchor="lm")
    fx = left + int(d.textlength("MUTED", font=font(big))) + 14
    parts = small.split(" · ", 1)
    if len(parts) == 2:
        d.text((W - 14, cy - 12), parts[0], font=_fit(d, parts[0], line1, W - fx - 14), fill=FG,
               anchor="rm")
        d.text((W - 14, cy + 13), parts[1], font=_fit(d, parts[1], line2, W - fx - 14, FONT_REG),
               fill=FG, anchor="rm")
    else:
        d.text((W - 14, cy), small, font=_fit(d, small, 20, W - fx - 14), fill=FG, anchor="rm")


def _overlay_muted(c: _Ctx, state: Any, status: Any, page: str = "ALIGN") -> None:
    """MUTED, impossible to miss but never hiding what is being edited.

    SHOW: a red band over the chip row and the graph's top (+9..+0.6 dB, where live boosts
    stop), so the 0 dB line, the cuts, every value, the edit box and FINE stay visible.
    ALIGN (and the v0 names): the v0 full-width band across the middle strip and the bottom
    sub-lines. SETUP: red frame + red footer strip (a band would cover the selected row and its
    'PUSH AGAIN'); an active hint takes the footer for its 2.5 s. HWTEST: frame + red tag.
    """
    d = c.d
    d.rectangle((0, STATUS_H, W - 1, H - 1), outline=BAND_RED, width=4)
    hint, unmute = _mute_hint(c, state, status)
    page = page_name(page)
    if page == "SETUP":
        if not hint:
            d.rectangle((0, H - FOOTER_H, W, H), fill=BAND_RED)
            cy = H - FOOTER_H // 2
            d.text((14, cy), "MUTED", font=font(22), fill=FG, anchor="lm")
            fx = 14 + int(d.textlength("MUTED", font=font(22))) + 12
            d.text((W - 14, cy), unmute, font=_fit(d, unmute, 18, W - fx - 14, FONT_REG),
                   fill=FG, anchor="rm")
        # else: the hint has the footer (drawn by the page) for its 2.5 s
        return
    if page == "HWTEST":
        box = (W - 120, STATUS_H + 6, W - 10, STATUS_H + 32)
        d.rectangle(box, fill=BAND_RED)
        d.text(((box[0] + box[2]) / 2, (box[1] + box[3]) / 2), "MUTED", font=font(20),
               fill=FG, anchor="mm")
        return
    if page == "SHOW":
        y0, y1 = SHOW_MUTED_BAND
        d.rectangle((0, y0, W, y1), fill=BAND_RED)
        _band_text(c, y0, y1, 44, 22, 18, hint or unmute)
        return
    y0, y1 = MID_Y0 - 10, MID_Y1 + 22   # covers the sub-lines whole, never half a line
    d.rectangle((0, y0, W, y1), fill=BAND_RED)
    _band_text(c, y0, y1, 56, 22, 18, hint or unmute)


_PAGES = {"SHOW": _page_show, "ALIGN": _page_align, "SETUP": _page_setup, "HWTEST": _page_hwtest}


def render(page: str, state: Any, status: Any = None, focus: Any = None,
           now: float | None = None, phone: Any = None) -> Image.Image:
    """Render `page` (SHOW, ALIGN, SETUP, HWTEST, PHONE; MAIN/LIVE = ALIGN/SHOW) with the
    status-driven overlays -> 480x320 RGB. The spectrum comes in `status.spectrum`.

    PHONE is drawn by ui/qr.py from `phone` (web.PhoneScreenInfo; None = nothing known yet).

    Never raises: unknown page -> SHOW, missing fields -> '--' / '?', no status -> everything
    unknown (amber), and any unexpected error -> a 'UI ERROR' frame that still shows MUTED.
    """
    try:
        if page == "PHONE":
            from .qr import render_phone_page   # lazy: qr imports this module
            return render_phone_page(phone, state, status, focus, now)
        return _render(page, state, status, focus, now)
    except Exception as e:   # the screen must keep updating; never take the loop down
        _log_render_error(e)
        return _error_frame(state, status, e)


def _render(page: Any, state: Any, status: Any, focus: Any, now: float | None) -> Image.Image:
    page = page_name(page)
    page = page if isinstance(page, str) and page in _PAGES else "SHOW"
    _check_status_shape(status)
    focus = Focus() if focus is None else focus
    now = time.monotonic() if now is None else now
    c = _Ctx(focus, now)
    ov = overlays_for(state, status)
    _status_bar(c, page, state, status, "MUTED" in ov)
    if "NO DEVICE" in ov:
        _overlay_no_device(c, state, status)
    elif "ENGINE DOWN" in ov:
        _overlay_engine_down(c, state, status)
    else:
        _PAGES[page](c, state, status)
        if "MUTED" in ov:
            _overlay_muted(c, state, status, page)
    return c.img


_last_render_error: str | None = None


def _log_render_error(e: Exception) -> None:
    """Log each distinct render error once (a 15 Hz loop must not flood the journal)."""
    global _last_render_error
    key = f"{type(e).__name__}: {e}"
    if key != _last_render_error:
        _last_render_error = key
        log.exception("render failed (showing UI ERROR frame): %s", key)


def _error_frame(state: Any, status: Any, exc: Exception) -> Image.Image:
    """Minimal frame for a render bug: says so, and still shows MUTED when it can tell."""
    img = Image.new("RGB", SIZE, BG)
    try:
        d = ImageDraw.Draw(img)
        try:
            muted = displayed_muted(state, status)
        except Exception:
            muted = False
        d.rectangle((0, 0, W - 1, H - 1), outline=BAND_RED if muted else RED, width=4)
        d.text((W // 2, 50), "UI ERROR", font=font(44), fill=RED, anchor="mm")
        if muted:
            d.rectangle((0, 120, W, 200), fill=BAND_RED)
            d.text((W // 2, 160), "MUTED", font=font(56), fill=FG, anchor="mm")
        else:
            d.text((W // 2, 160), "mute state unknown", font=font(28), fill=AMBER, anchor="mm")
        name = type(exc).__name__
        d.text((W // 2, 240), name, font=_fit(d, name, 20, W - 30, FONT_REG), fill=DIM,
               anchor="mm")
        d.text((W // 2, 282), "screen fault · journalctl -u k1000", font=font(18, FONT_REG),
               fill=DIM, anchor="mm")
    except Exception:
        pass
    return img


def warm_up() -> float:
    """Load fonts, fill the text cache and rasterise the graph grid once (call at service
    start); returns ms spent.

    Why: the first frame costs ~40 ms on the Pi 4 (font files + glyph rasterising), over the
    budget; later SHOW frames are a few ms of render plus the RGB565 packing.
    """
    t0 = time.perf_counter()
    ok = ScreenStatus(device_present=True, engine="Running", spectrum=demo_spectrum(seconds=0.5),
                      spectrum_state="live")
    for page in _PAGES:
        render(page, None, ok, Focus())
        render(page, None, ScreenStatus(device_present=True, engine="Running", muted=True),
               Focus())
    for ov in (ScreenStatus(device_present=False), ScreenStatus(engine=None), ScreenStatus()):
        render("SHOW", None, ov, Focus())
    return (time.perf_counter() - t0) * 1000


# ------------------------------------------------------------------------------------------
# Demo states + snapshots:  python3 -m k1000.ui.pages <outdir>   |   --bench [N]
# ------------------------------------------------------------------------------------------
def demo_spectrum(ring_hz: float | None = 2440.0, seconds: float = 3.0, seed: int = 7,
                  level_db: float = -30.0) -> eg.RtaFrame | None:
    """A program-like spectrum shaped as GetSpectrum gives it (1/24 octave, 10 Hz-20 kHz,
    peak-of-band dBFS), passed through the spectrum task's own ballistics (rta.Ballistics) at
    10 frames/s, with an optional narrow ring that persists long enough to be a PEAK."""
    try:
        from ..rta import MAX_FREQ, MIN_FREQ, N_BINS, Ballistics
        bal: Any = Ballistics()
    except Exception:          # pages must render without the spectrum module too
        MIN_FREQ, MAX_FREQ, N_BINS = 10.0, 20000.0, 264
        bal = eg.Rta()
    rng = np.random.default_rng(seed)
    n = int(N_BINS)
    f = MIN_FREQ * (MAX_FREQ / MIN_FREQ) ** (np.arange(n) / (n - 1))
    lf = np.log2(f / 1000.0)
    base = (level_db - 3.0 * lf - 4.0 * np.maximum(0, -lf - 3.2) ** 2
            + 5.0 * np.exp(-((np.log2(f / 70.0)) ** 2) / 0.5)
            - 10.0 * np.maximum(0, np.log2(f / 12000.0)) ** 2)
    for k in range(max(1, int(seconds * 10))):
        m = base + rng.normal(0, 2.0, n) + 3.0 * np.sin(k / 3.0 + lf * 2)
        if ring_hz:
            ring_db = float(np.interp(math.log2(ring_hz / 1000.0), lf, base)) + 16.0
            m = np.maximum(m, ring_db - 240.0 * np.abs(np.log2(f / ring_hz)))
        bal.update(f, m, k * 0.1)
    return bal.frame()


def _setup_index(field: str) -> int:
    return next((i for i, it in enumerate(SETUP_ITEMS) if it.field == field), 0)


def demo_notch(state: Any, peak_hz: float, slot: int) -> tuple[Any, Any]:
    """(state, focus) after a B4 tap on SHOW with `slot` selected and the spectrum's PEAK at
    `peak_hz`, from the REAL Controller.notch_helper (no engine: nothing is sent), so the
    picture shows what the box does. The hint and the edit highlight are kept on for the
    snapshot."""
    from pathlib import Path

    from ..control import Controller, Paths
    from ..profiles import load_profile

    class _Peak:
        peak_hz = 0.0

    ctl = Controller(None, load_profile("dev", {}, probe=False), state.copy(),
                     Paths(Path("/nonexistent/k1000-demo/state.json"),
                           Path("/nonexistent/k1000-demo/active.yml")))
    fr = _Peak()
    fr.peak_hz = float(peak_hz)
    ctl.set_spectrum(fr, "live")
    ctl.focus.slot = slot
    ctl.notch_helper(now=0.0)
    focus = ctl.focus
    focus.hint_until = focus.edit_until = float("inf")
    return ctl.state, focus


def demo_cases() -> list[tuple[str, str, Any, Any, Any]]:
    """(name, page, state, status, focus) for every page and overlay, with representative data.

    Used by the snapshot command (docs/ui/*.png) and by the tests, so what is reviewed by eye
    is exactly what is tested.
    """
    from ..state import State  # local import: pages must render without state.py too
    from .input_map import HwTestLog

    inf = float("inf")
    s = State()
    s.master_db, s.balance_db, s.sub_delay_ms, s.lowcut_hz = -14.0, 3.0, 2.35, 112.0
    s.live_peq[1].freq, s.live_peq[1].gain, s.live_peq[1].q = 2520.0, -6.5, 8.0
    s.live_peq[3].freq, s.live_peq[3].gain, s.live_peq[3].q = 6300.0, -3.0, 4.0
    s.live_lowshelf.gain = 2.0
    spec = demo_spectrum()
    plain = demo_spectrum(ring_hz=None)
    peak = spec.peak_hz if spec is not None and spec.peak_hz else 2440.0
    run = ScreenStatus(profile="m4", device="MOTU M4", device_present=True, engine="Running",
                       load=2.0, levels=(-14.0, -15.5, -9.0, -12.5), clip=(False,) * 4,
                       ready=True, spectrum=spec, spectrum_state="live")

    def with_(base: ScreenStatus, **kw: Any) -> ScreenStatus:
        return ScreenStatus(**{**base.__dict__, **kw})

    run_plain = with_(run, spectrum=plain)
    run_clip = with_(run, levels=(-14.0, -15.5, -9.0, -2.5), clip=(False, False, False, True))
    muted = State.from_dict({**s.to_dict(), "muted": True})
    run_muted = with_(run, muted=True)
    # B4 (the real notch helper): with no bell in use within 1/6 oct of the PEAK (P2 is at
    # 1 kHz here), P3 (selected, free) goes to the PEAK at Q 8.65, gain 0; with P2 cutting at
    # 2.52k (0.05 oct from the PEAK) it selects P2 and writes nothing.
    away = State.from_dict(s.to_dict())
    away.live_peq[1].freq = 1000.0
    notch, notch_focus = demo_notch(away, peak, 2)
    renotch, renotch_focus = demo_notch(s, peak, 2)
    pills = State.from_dict({**s.to_dict(), "lowcut_on": False, "sub_inverted": True,
                             "mono_subs": True})
    lc_off = State.from_dict({**s.to_dict(), "lowcut_on": False})
    dev = ScreenStatus(profile="dev", device="HiFiBerry", device_present=True, engine="Running",
                       load=0.1, ready=True, laptop_state="closed")
    dev_muted = with_(dev, muted=True)
    knobs = "KNOBS OFFLINE · USE PHONE MUTE / BYPASS"
    hw = HwTestLog(enc_count=[3, -2, 0, 7], sw_down=[False, False, False, False, False, True,
                                                     False, False],
                   sw_last=["short", "", "", "", "", "long", "", ""], last="sw5 press  = B2")
    hwf = Focus(hw=hw)
    reset_i, undo_i = _setup_index(A_RESET_LIVE), _setup_index(A_UNDO_RESET)
    laptop_i = _setup_index(A_LAPTOP)
    dev_open = with_(dev, laptop_state="open", laptop_s=7140.0,
                     pills=["MONO SUB", "LAPTOP OPEN 1:59"])
    mono_i = _setup_index("mono_subs")
    return [
        # SHOW: the front page
        ("show", "SHOW", s, run, Focus(slot=1)),
        ("show_edit_fine", "SHOW", s, run_clip, Focus(slot=1, editing="live_peq.1.freq",
                                                      edit_until=inf, held={1})),
        ("show_notch", "SHOW", notch, run, notch_focus),
        ("show_notch_reselect", "SHOW", renotch, run, renotch_focus),
        ("show_band", "SHOW", s, run_plain, Focus(slot=2, editing=F_SLOT, edit_until=inf)),
        ("show_shelf", "SHOW", s, run_plain, Focus(slot=4, editing="live_lowshelf.gain",
                                                   edit_until=inf)),
        ("show_flat_spectrum_off", "SHOW", State(), with_(run, spectrum=None, spectrum_on=False,
                                                          spectrum_state="off"), Focus(slot=4)),
        ("show_no_spectrum", "SHOW", s, with_(run, spectrum=None, spectrum_state="unavailable"),
         Focus(slot=1)),
        # the controller's order (LAPTOP OPEN first); too many to fit: MONO SUB folds first
        ("show_pills", "SHOW", pills, with_(run, pills=["LAPTOP OPEN 1:59", "LO-CUT OFF",
                                                         "POL INV SUB", "MONO SUB"]), Focus(slot=1)),
        ("show_pills_two", "SHOW", lc_off, with_(run, pills=["LO-CUT OFF", "MONO SUB"]),
         Focus(slot=1)),
        ("hint", "SHOW", s, run, Focus(slot=1, editing=F_MASTER, edit_until=inf,
                                       hint="MASTER: SLOW DOWN", hint_until=inf)),
        ("input_clip", "SHOW", s, with_(run, in_clip=True), Focus(slot=1)),
        ("alert_warning", "SHOW", s, with_(run, alerts=[("warning", "PIPELINE CHANGED (GUI)")],
                                           message="PIPELINE CHANGED (GUI)"), Focus(slot=1)),
        ("muted", "SHOW", muted, run_muted, Focus(slot=1)),
        ("muted_fine", "SHOW", muted, run_muted, Focus(slot=1, editing="live_peq.1.q",
                                                       edit_until=inf, held={3})),
        # K1000 holds Main before its own boot ramp: not 'READY · hold B2 to unmute'
        ("muted_ramp_hold", "SHOW", s, with_(run, engine="Starting", muted=True, ramping=True,
                                             ready=False, levels=(-100.0,) * 4, clip=(False,) * 4,
                                             alerts=[("info", "RAMPING IN")], spectrum=None),
         Focus(slot=1)),
        ("knobs_offline", "SHOW", muted, with_(run_muted, alert=knobs, message=knobs,
                                               alerts=[("critical", knobs)]), Focus(slot=1)),
        # unmuted: the critical alert is said again over the chip row, big, in red
        ("knobs_offline_live", "SHOW", s, with_(run, alert=knobs, message=knobs,
                                                alerts=[("critical", knobs)]), Focus(slot=1)),
        # the most crowded status bar: muted, CLIP latched, a warning (ellipsized, never cut)
        ("alert_crowded", "SHOW", muted, with_(run_muted, levels=run_clip.levels, clip=run_clip.clip,
                                               in_clip=True,
                                               alerts=[("warning", "DEVICES CHANGED (GUI)")],
                                               message="DEVICES CHANGED (GUI)"), Focus(slot=1)),
        ("unknown_status", "SHOW", s, ScreenStatus(profile="m4", device="MOTU M4"), Focus(slot=1)),
        ("no_device", "SHOW", s, ScreenStatus(profile="m4", device="MOTU M4",
                                              device_present=False, engine=None,
                                              alerts=[("critical", "NO DEVICE · MOTU M4")]),
         Focus()),
        ("engine_down", "SHOW", s, ScreenStatus(profile="m4", device="MOTU M4", engine=None,
                                                alerts=[("critical", "ENGINE DOWN")]), Focus()),
        # ALIGN
        ("align", "ALIGN", s, run, Focus()),
        ("align_locked_turn", "ALIGN", s, run, Focus(hint=HINT_LOCKED, hint_until=inf)),
        ("align_unlocked", "ALIGN", s, with_(run, align_locked=False, align_unlock_s=27.4),
         Focus(editing=F_SUB_DELAY, edit_until=inf)),
        ("align_lowcut_off", "ALIGN", lc_off, run, Focus()),
        ("align_muted", "ALIGN", muted, run_muted, Focus()),
        # SETUP
        ("setup", "SETUP", s, dev, Focus(item=reset_i)),
        ("setup_reset_confirm", "SETUP", s, dev, Focus(item=reset_i, confirm=A_RESET_LIVE,
                                                       confirm_until=inf,
                                                       hint="PUSH AGAIN TO RESET ALL LIVE EQ",
                                                       hint_until=inf)),
        ("setup_undo", "SETUP", s, with_(dev, undo_reset_s=287.0), Focus(item=undo_i)),
        ("setup_confirm", "SETUP", s, dev, Focus(item=mono_i, confirm="mono_subs",
                                                 confirm_until=inf,
                                                 hint="PUSH AGAIN TO APPLY (MAY CLICK)",
                                                 hint_until=inf)),
        ("setup_muted", "SETUP", muted, dev_muted, Focus(item=mono_i, confirm="mono_subs",
                                                         confirm_until=inf)),
        ("setup_applying", "SETUP", s, with_(dev, alert="APPLYING SETUP…", message="APPLYING SETUP…",
                                             alerts=[("info", "APPLYING SETUP…")]),
         Focus(item=mono_i)),
        ("setup_laptop_confirm", "SETUP", s, dev,
         Focus(item=laptop_i, confirm=A_LAPTOP, confirm_until=inf,
               hint="PUSH AGAIN: LAPTOP ACCESS FOR 2 H", hint_until=inf)),
        ("setup_laptop_open", "SETUP", s, dev_open, Focus(item=laptop_i - 1)),
        # Knob test
        ("hwtest", "HWTEST", s, run, hwf),
        ("hwtest_muted", "HWTEST", muted, run_muted, hwf),
    ]


def snapshot(outdir: str) -> list[tuple[str, float]]:
    """Render every demo case to <outdir>/<name>.png; returns (name, render ms)."""
    import os
    os.makedirs(outdir, exist_ok=True)
    out = []
    for name, page, st, stat, foc in demo_cases():
        t0 = time.perf_counter()
        img = render(page, st, stat, foc)
        out.append((name, (time.perf_counter() - t0) * 1000))
        img.save(os.path.join(outdir, f"{name}.png"))
    return out


def bench(n: int = 300, fps: float = 10.0, fb_path: str | None = None) -> dict[str, float]:
    """SHOW as the box runs it with the spectrum on: every frame a fresh spectrum (the task's
    ballistics), moving meters, render, then Framebuffer.show (changed rows packed to RGB565
    and written to `fb_path`; default a temporary file: the live panel is never touched).
    Prints ms percentiles and the full-frame packer against v0's converter, then paces `n`
    frames at `fps` twice (with and without the ballistics) and reports this process's CPU as
    % of one core."""
    import os
    import tempfile

    from .display import Framebuffer, to_rgb565
    try:
        from ..rta import Ballistics
        bal: Any = Ballistics()
    except Exception:
        bal = eg.Rta()
    from ..state import State
    s = State()
    s.master_db = -14.0
    s.live_peq[1].freq, s.live_peq[1].gain, s.live_peq[1].q = 2520.0, -6.5, 8.0
    s.live_peq[3].freq, s.live_peq[3].gain = 6300.0, -3.0
    s.live_lowshelf.gain = 2.0
    rng = np.random.default_rng(1)
    first = demo_spectrum(seconds=2.0)
    assert first is not None
    f = first.freqs
    base = np.array(first.level)
    tmp = None
    if fb_path is None:
        tmp = tempfile.NamedTemporaryFile(prefix="k1000-fb-", delete=False)
        tmp.write(b"\0" * (W * H * 2))
        tmp.close()
        fb_path = tmp.name
    fb = Framebuffer(fb_path, sysfs="/nonexistent")
    foc = Focus(slot=1)
    t = 10.0

    fixed = demo_spectrum(seconds=2.0)

    def frame(i: int, spectrum: bool = True) -> tuple[float, float, float]:
        nonlocal t
        t0 = time.perf_counter()
        if spectrum:
            bal.update(f, base + rng.normal(0, 2.0, len(base)), t)
            fr = bal.frame()
        else:
            fr = fixed
        t1 = time.perf_counter()
        st = ScreenStatus(profile="m4", device="MOTU M4", device_present=True, engine="Running",
                          load=2.0, levels=tuple(-14.0 + 3.0 * rng.random(4)), ready=True,
                          clip=(False,) * 4, spectrum=fr, spectrum_state="live", pills=[], alerts=[])
        img = render("SHOW", s, st, foc, now=t)
        t2 = time.perf_counter()
        fb.show(img)
        t3 = time.perf_counter()
        t += 1.0 / fps
        return (t1 - t0) * 1000, (t2 - t1) * 1000, (t3 - t2) * 1000

    for i in range(15):            # warm-up: fonts, glyphs, grid layer
        frame(i)
    rows = [frame(i) for i in range(n)]

    def p(v: Sequence[float]) -> str:
        v = sorted(v)
        return f"med {v[len(v) // 2]:6.2f}  p95 {v[int(len(v) * 0.95)]:6.2f}  max {v[-1]:6.2f} ms"

    cols = list(zip(*rows))
    tot = [a + b + c for a, b, c in rows]
    print(f"spectrum ballistics       {p(cols[0])}")
    print(f"SHOW render               {p(cols[1])}")
    print(f"Framebuffer.show          {p(cols[2])}  (changed rows packed + written; "
          f"last span {fb.last_span})")
    print(f"per frame, all            {p(tot)}")
    img = render("SHOW", s, ScreenStatus(device_present=True, engine="Running", spectrum=fixed),
                 foc, now=t)

    def v0(im: Image.Image) -> bytes:
        a = np.asarray(im, dtype=np.uint16)
        w = ((a[:, :, 0] & 0xF8) << 8) | ((a[:, :, 1] & 0xFC) << 3) | (a[:, :, 2] >> 3)
        return w.astype("<u2").tobytes()

    for name, fn in (("RGB565 v0 (uint16), full", v0), ("RGB565 packer, full", to_rgb565)):
        ts = []
        for _ in range(60):
            t0 = time.perf_counter()
            fn(img)
            ts.append((time.perf_counter() - t0) * 1000)
        print(f"{name:26s}{p(ts[10:])}")
    out = {"render_ms": sorted(cols[1])[len(cols[1]) // 2],
           "frame_ms": sorted(tot)[len(tot) // 2]}
    # paced runs: what one core spends at `fps`
    period = 1.0 / fps
    for label, spectrum in (("moving spectrum: ballistics + render + show", True),
                            ("still spectrum, meters moving: render + show", False)):
        w0 = fb.rows_written, fb.frames_written
        c0, t0 = time.process_time(), time.monotonic()
        nxt = t0
        for i in range(n):
            frame(i, spectrum)
            nxt += period
            delay = nxt - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        cpu = (time.process_time() - c0) / max(1e-9, time.monotonic() - t0) * 100.0
        rows_pf = (fb.rows_written - w0[0]) / max(1, fb.frames_written - w0[1])
        print(f"paced {fps:.0f} fps x {n}, {label}: {cpu:.1f} % of one core "
              f"({rows_pf:.0f} rows written per frame)")
        out[f"cpu_pct_{'all' if spectrum else 'render'}"] = cpu
    fb.close()
    if tmp is not None:
        os.unlink(fb_path)
    return out


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    if args and args[0] == "--bench":
        bench(int(args[1]) if len(args) > 1 else 300)
    else:
        for n, ms in snapshot(args[0] if args else "."):
            print(f"{n:24s} {ms:6.1f} ms")
