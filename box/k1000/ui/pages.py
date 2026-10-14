# SPDX-License-Identifier: GPL-3.0-or-later
"""Screen pages for the 480x320 ZynScreen (UI v1, D-009): render(page, state, status, focus).

Pages: SHOW (the front page: master, FREQ/Q/GAIN of the selected live band, the EQ curve over
the program spectrum), ALIGN (master, the signed sub/top delay, balance, Lo-Cut; E2/E4, the two
bottom knobs, locked until a B2 hold), SETUP (the list), PHONE (drawn by ui/qr.py) and HWTEST
(the knob test). The v0 names MAIN and LIVE are aliases of ALIGN and SHOW.

Design rules (why it looks like this):
- Read at arm's length, outdoors, by someone mixing: black background, white digits, amber
  labels, one colour per meaning. Amber = what is being edited, and hints (brief). Red = mute,
  clip, a critical system alert. Orange = a system warning. Cyan = the spectrum PEAK (and the
  knob tags in their `bright` style).
- The screen mirrors the knobs: each encoder's value sits next to its corner (KNOB_CORNERS in
  input_map, confirmed on the box), flush to that corner's OUTER border with its unit beside it
  (D-015: left corners left-aligned, right corners right-aligned, under or over their legend;
  FINE goes to the box's inner side). The "E1".."E4" tag next to each label follows k1000.yaml
  `ui.knob_tags` (status.knob_tags): dim (default, small dark grey), off (the label takes the
  corner; a sticker on the shell names the knobs), bright (cyan, as v1 shipped).
- System alerts (KNOBS OFFLINE, NO DEVICE, ENGINE DOWN, CANNOT SAVE, pipeline/device fixes)
  take the STATUS BAR in their own colour, never amber: KNOBS OFFLINE red across the whole bar,
  other critical alerts red, warnings orange, passing states (APPLYING SETUP…, RAMPING IN)
  blue. Hints (what the operator's own press did, 2.5 s) keep the amber strip: the chip row on
  SHOW, the middle strip on ALIGN, the footer on SETUP. "The system has a problem" and "this is
  what your press did" never share a place or a colour.
- SHOW has persistent pills for every non-default system state (LO-CUT OFF, OUT P2, POL INV,
  MONO SUB, AUDITION, LAPTOP OPEN…), on the bottom row between the E2 and E4 labels, where
  neither a hint nor the MUTED band ever covers them.
- A live band switched OUT (D-014: a B3 hold, or a chip hold on the phone) keeps its settings:
  its chip is drawn dark with a dashed outline and a small OUT, its marker hollow and dashed at
  its stored gain, the curve without it, and the pill row says OUT P2.
- The output meters (SUB L R, TOP L R) stay on SHOW and ALIGN: clipping must be visible while
  notching. D-015: the program input's capture peaks (status.in_peak_db) are drawn left of them,
  IN L R, on the same -60..0 dBFS scale and colours; IN CLIP (the ADC clipped, latched) turns
  their clip boxes red, as an output's clip does.
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
from .input_map import (A_LAPTOP, A_RESET_LIVE, A_UNDO_RESET, BAND_CYCLE, F_ALIGN_DELAY, F_BALANCE,
                        F_LOWCUT_FREQ, F_LOWCUT_ON, F_MASTER, F_SLOT, F_SPECTRUM_ON, HINT_LOCKED,
                        KNOB_CORNERS, KNOB_TAGS_DEFAULT, LIVE_SLOT_LABELS, LIVE_SLOT_SHORT,
                        LIVE_SLOTS, SETUP_GROUPS, SETUP_ITEMS, Focus, knob_labels, page_name,
                        parse_knob_tags, setup_group_pos)

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
TAG_DIM = (105, 105, 105)              # ui.knob_tags: dim (the default): secondary to the label
TAG_W = 30                             # room a knob tag takes before its label (dim, bright)
SOUND_M_PER_MS = 0.343                 # ALIGN's delay as a distance (343 m/s)

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
RISKY_PILLS: tuple[str, ...] = ("LAPTOP OPEN", "LO-CUT OFF", "OUT ", "POL INV")
PILL_MIN_PX = 13

STATUS_H = 30
METER_X0, METER_X1 = 195, 285          # ALIGN: centre column (IN, SUB, TOP meters; D-015)
MID_Y0, MID_Y1 = 147, 183              # ALIGN: middle strip (hints)
# ALIGN (D-015): the MUTED band between the top sub-line and the bottom ones (the delay's side
# stays readable), and the LOCKED / EDIT badge on the bottom row, between the two locked knobs'
# labels (DELAY E2, LO-CUT E4), joined to both by a line.
ALIGN_MUTED_BAND = (138, 192)
LOCK_Y0, LOCK_Y1 = 294, 315
OVERLAYS: tuple[str, ...] = ("NO DEVICE", "ENGINE DOWN", "MUTED")
METER_LABELS: tuple[str, ...] = ("Sub L", "Sub R", "Top L", "Top R")
# D-015: the meters as drawn, left to right: the program input (status.in_peak_db, IN CLIP),
# then the outputs (status.levels / clip in METER_LABELS order).
METER_PAIRS: tuple[str, ...] = ("IN", "SUB", "TOP")
METER_FLOOR_DB = -60.0
ENGINE_OK = ("RUNNING", "PAUSED", "STARTING")
ENGINE_DOWN_STATES = ("INACTIVE", "STALLED", "DOWN")
ENGINE_UNKNOWN = "Unknown"             # status did not say (missing field, no status at all)
FOOTER_H = 28                          # SETUP footer row (E2 label / hint / MUTED strip)

# SHOW geometry (480 x 320; docs/07 §2.1). D-015 (Thomas, 2026-10-14): legends 20 -> 17 px,
# values 44/40 -> 38/36 px, chips 24 -> 22 px tall: the graph 130 -> 152 px high.
LABEL_PX = 17                          # SHOW and ALIGN knob legends (SETUP keeps 20)
TAG_PX = 15                            # SHOW: the band's name beside the Q legend
UNIT_PX = 16                           # SHOW: a value's unit
TOP_VALUE_PX, BOT_VALUE_PX = 38, 36    # SHOW: E1/E3 values, E2/E4 values
TOP_Y0 = STATUS_H                      # E1 / E3 blocks: label row, then the value box
TOP_BOX = (48, 86)
CHIP_Y0, CHIP_Y1 = 88, 110             # band chips LS 1 2 3 4 HS; hints take this row
CHIP_PX = 18
GRAPH = (0, 112, W, 264)               # x0, y0, x1, y1 (exclusive)
BOT_Y0 = 264                           # E2 / E4 blocks: the value box, then the label row
BOT_BOX = (264, 296)
METER_BOX = (195, 31, 285, 86)         # compact 6-bar meter (IN, SUB, TOP), top centre
PILL_Y0, PILL_Y1 = 297, 315            # SHOW: persistent pills, between the bottom labels
# MUTED on SHOW: the chip row and the graph's top (+9..+0.6 dB: live boosts stop at +6), so
# the 0 dB line, the curve's cuts and every value stay visible.
SHOW_MUTED_BAND = (CHIP_Y0, int(eg.DbAxis(GRAPH[1], GRAPH[3] - 1, eg.STYLE.top_db,
                                          eg.STYLE.bottom_db).y(0.6)))
CHIP_TEXT: dict[int, str] = {4: "LS", 0: "1", 1: "2", 2: "3", 3: "4", 5: "HS"}
OUT_CHIP_BG = (18, 18, 18)             # D-014: a band switched OUT: a dark chip, dashed outline


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
    in_peak_db: the program input's capture peaks, L R (dBFS, before any gain): the IN meters
            (D-015). Missing or short: the meters read the floor.
    UI v1 (D-009), same names as control.Status:
    alerts: [(level, text)] most urgent first, for the status bar; None = derived from
            `alert`/`message` (an older status).
    pills:  persistent non-default states for SHOW; None = derived from the state.
    align_unlock_s: seconds left of the ALIGN E2/E4 unlock (the EDIT badge); the focus's
            unlock_until counts too.
    undo_reset_s: SETUP > Undo reset, seconds left (0 = nothing to undo).
    spectrum_on / spectrum_state / spectrum: SETUP > Spectrum, the task's state, and the
            eqgraph.RtaFrame to draw behind the curve (None = none).
    laptop_s / laptop_state: SETUP > Laptop access, seconds left (0 = closed) and the state
            root reports (off = no such surface, closed, opening, open, closing, unavailable).
    knob_tags: the E1..E4 tags' style, k1000.yaml `ui.knob_tags` (dim | off | bright).
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
    in_peak_db: Sequence[float] = (-100.0, -100.0)
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
    knob_tags: str = KNOB_TAGS_DEFAULT


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

    The dispatcher passes this to `map_event(..., muted=...)`, so B4 long asks for the opposite
    of what the operator sees; unknown reads as 'not muted', so B4 long then MUTES (safe side).
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
    an older status, the ones the state alone tells (LO-CUT OFF, OUT <bands>, POL INV, MONO SUB,
    AUDITION)."""
    raw = _get(status, "pills", None)
    if isinstance(raw, str):
        return [raw] if raw else []
    if raw is not None:
        return [str(p) for p in _seq(raw) if p]
    out: list[str] = []
    if _truth(_get(state, "lowcut_on", True)) is False:
        out.append("LO-CUT OFF")
    outs = [LIVE_SLOT_SHORT[i] for i, b in enumerate(eg.live_bands(state)) if not b.on]
    if outs:
        out.append(" ".join(["OUT", *outs]))
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


def knob_tags(status: Any) -> str:
    """The E1..E4 tag style the status asks for (dim | off | bright); anything else = dim."""
    return parse_knob_tags(_get(status, "knob_tags", None)) or KNOB_TAGS_DEFAULT


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
def _text_box(text: str, f: ImageFont.FreeTypeFont, anchor: str) -> tuple[int, int, int, int]:
    """The box `text` covers around its anchor point (ImageDraw.textbbox at (0, 0))."""
    return tuple(f.getbbox(text, anchor=anchor))  # type: ignore[return-value]


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


def fmt_align_delay(v: float | None) -> tuple[str, str]:
    """ALIGN E2 (D-015), the signed delay (sub - top, ms): (big value, sub-line). The direction
    is in words, never a bare minus sign: 1.25 -> ('1.25', 'SUBS LATER · 0.43 m'), -1.25 ->
    ('1.25', 'TOPS LATER · 0.43 m'), 0 -> ('0.00', 'ALIGNED')."""
    if v is None:
        return "--", ""
    if abs(v) < 0.005:
        return "0.00", "ALIGNED"
    side = "SUBS" if v > 0 else "TOPS"
    return f"{abs(v):.2f}", f"{side} LATER · {abs(v) * SOUND_M_PER_MS:.2f} m"


def align_delay(state: Any) -> float | None:
    """sub_delay_ms - top_delay_ms read tolerantly (one unreadable side counts as 0; both
    unreadable = None)."""
    sub, top = _num(state, "sub_delay_ms"), _num(state, "top_delay_ms")
    if sub is None and top is None:
        return None
    return round((sub or 0.0) - (top or 0.0), 6) + 0.0


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

    def __init__(self, focus: Any, now: float, tags: str = KNOB_TAGS_DEFAULT):
        self.img = Image.new("RGB", SIZE, BG)
        self.d = _Draw(self.img)
        self.now = now
        self.focus = focus
        self.tags = tags                               # E1..E4 tags: dim | off | bright
        ed = getattr(focus, "editing_now", None)
        self.editing = ed(now) if callable(ed) else None
        held = getattr(focus, "held", ())
        self.held = set(held) if held else set()
        self.bottom_label_end: dict[str, float] = {}   # SHOW/ALIGN: where the bottom labels reach
        # Where each knob's texts landed (knob -> {"label", "tag", "value", "unit", "sub"} ->
        # (x0, y0, x1, y1)): the tests check the flush alignment and that nothing is cut.
        self.ink: dict[int, dict[str, tuple[float, float, float, float]]] = {}

    def mark(self, e: int, part: str, xy: tuple[float, float], text: str,
             f: ImageFont.FreeTypeFont, anchor: str) -> None:
        """Record the box of `text` drawn at `xy` (see `ink`; cached: SHOW draws at 10 fps)."""
        if text:
            left, top, right, bottom = _text_box(text, f, anchor)
            x, y = xy
            self.ink.setdefault(e, {})[part] = (x + left, y + top, x + right, y + bottom)


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


def _knob_label(c: _Ctx, e: int, label: str, x_edge: float, y: float, right: bool,
                fill=AMBER, size: int = 20) -> float:
    """A knob's label at its screen corner, after its "E1".."E4" tag in the `c.tags` style;
    returns the x where the label ends on the inner side.

    `x_edge` is the corner side (padding included), `y` the tag's top as v1 drew it. bright: the
    v1 cyan tag (font 16) and the label TAG_W further in. dim: a small dark-grey tag on the
    label's baseline, the label where it was. off: no tag, the label at the corner. `size`: the
    label's font (SETUP 20; SHOW and ALIGN LABEL_PX since D-015)."""
    d = c.d
    flab = font(size)
    anchor = "ra" if right else "la"
    lx = x_edge
    if c.tags != "off":
        tag = f"E{e + 1}"
        if c.tags == "bright":
            d.text((x_edge, y), tag, font=font(16), fill=CYAN, anchor=anchor)
        else:
            asc = flab.getmetrics()[0] if hasattr(flab, "getmetrics") else 19
            base = y - 2 + asc                          # the label's baseline
            d.text((x_edge, base), tag, font=font(13), fill=TAG_DIM,
                   anchor="rs" if right else "ls")
        lx = x_edge - TAG_W if right else x_edge + TAG_W
    d.text((lx, y - 2), label, font=flab, fill=fill, anchor=anchor)
    c.mark(e, "label", (lx, y - 2), label, flab, anchor)
    lw = d.textlength(label, font=flab)
    return lx - lw if right else lx + lw


def _cap_px(f: ImageFont.FreeTypeFont) -> int:
    """Height of a digit above the baseline: a value and its unit share a baseline, the value
    centred on its box by its digits (not by the font's ascender)."""
    return -f.getbbox("8", anchor="ls")[1]


def _value_group(c: _Ctx, e: int, x0: float, x1: float, cy: float, value: str, unit: str, *,
                 size: int, unit_px: int, right: bool, fill, unit_fill, reserve: float = 0.0,
                 pad: int = 10, gap: int = 5) -> tuple[float, float]:
    """D-015: a knob's value and its unit, flush to the corner's OUTER border: from x0 + pad
    on a left corner, ending at x1 - pad (the unit's end) on a right one; the digits centred on
    `cy`, the unit on their baseline. `reserve` px on the inner side stay free (FINE). The value
    shrinks to fit. Returns the group's (left, right)."""
    d = c.d
    funit = font(unit_px)
    unit_w = d.textlength(unit, font=funit) + gap if unit else 0.0
    room = (x1 - x0) - 2 * pad - reserve
    fval = _fit(d, value, size, max(10, int(room - unit_w)))
    vw = d.textlength(value, font=fval)
    total = vw + unit_w
    vx = x1 - pad - total if right else x0 + pad
    base = cy + _cap_px(fval) / 2
    d.text((vx, base), value, font=fval, fill=fill, anchor="ls")
    c.mark(e, "value", (vx, base), value, fval, "ls")
    if unit:
        d.text((vx + vw + gap, base), unit, font=funit, fill=unit_fill, anchor="ls")
        c.mark(e, "unit", (vx + vw + gap, base), unit, funit, "ls")
    return vx, vx + total


# ---- quadrant layout (ALIGN) ---------------------------------------------------------------
# Quadrant geometry: x range and the vertical anchors for label / value / sub-line. D-015:
# legends LABEL_PX, values ALIGN_VALUE_PX (was 60), everything flush to the outer border.
ALIGN_VALUE_PX = 52
ALIGN_UNIT_PX = 18
ALIGN_SUB_PX = 17
_QX = {"l": (0, METER_X0), "r": (METER_X1, W)}
_QY = {  # (label_y, value_center_y, sub_y) ; label sits at the outer (screen) edge
    "t": (STATUS_H + 3, 88, 128),
    "b": (H - 23, 240, 202),
}


def _quadrant(c: _Ctx, e: int, label: str, value: str, unit: str, sub: str,
              field_key: str | None, *, dim: bool = False, value_size: int = ALIGN_VALUE_PX,
              shift: int = 0, box_half: int = 36, sub_strong: bool = False) -> None:
    """Draw one knob's quadrant: 'E1 MASTER' at the corner, big value, unit + sub-line, all
    flush to the quadrant's outer border (D-015).

    `shift` moves the value towards the screen edge; `box_half` is the half-height of the edit
    highlight. `sub_strong`: the sub-line carries meaning the value does not (the delay's side):
    white, grey only while the quadrant is dimmed (regular weight: bold would shrink it to 14 px).
    """
    corner = KNOB_CORNERS[e]
    vpos, hpos = corner[0], corner[1]
    right = hpos == "r"
    x0, x1 = _QX[hpos]
    label_y, val_cy, sub_y = _QY[vpos]
    val_cy += -shift if vpos == "t" else shift
    d = c.d
    pad = 10
    # label at the outer corner (after its E1..E4 tag, per ui.knob_tags)
    end = _knob_label(c, e, label, x1 - pad if right else x0 + pad, label_y, right,
                      size=LABEL_PX)
    if vpos == "b":
        c.bottom_label_end[hpos] = end
    # value (highlighted while being edited; switching the Lo-Cut highlights its frequency)
    edited = field_key is not None and (c.editing == field_key or (
        field_key == F_LOWCUT_FREQ and c.editing == F_LOWCUT_ON))
    fine = e in c.held
    box = (x0 + 4, val_cy - box_half, x1 - 4, val_cy + box_half)
    fs = _fit(d, sub, ALIGN_SUB_PX, (x1 - x0) - 2 * pad, FONT_REG) if sub else None
    sx, sa = (x1 - pad, "rm") if right else (x0 + pad, "lm")
    if sub and fs is not None:
        # the edit box stops short of the sub-line ('SUBS LATER · 0.81 m' over DELAY), never
        # half over it: grey on amber, cut in two
        _l, st, _r, sb = d.textbbox((sx, sub_y), sub, font=fs, anchor=sa)
        if vpos == "t":
            box = (box[0], box[1], box[2], min(box[3], st - 3))
        else:
            box = (box[0], max(box[1], sb + 3), box[2], box[3])
    if edited:
        d.rounded_rectangle(box, radius=8, fill=AMBER)
    fg = BG if edited else (DIM if dim else FG)
    # FINE at the INNER side (the value is flush to the outer one), where the MUTED band never
    # covers it and the value keeps its whole width: a top quadrant's in its label row, a
    # bottom one's under the digits, at the box's bottom edge.
    _value_group(c, e, x0, x1, val_cy, value, unit, size=value_size, unit_px=ALIGN_UNIT_PX,
                 right=right, fill=fg, unit_fill=BG if edited else DIM)
    if fine:
        ffine = font(14)
        if vpos == "t":
            fy, on_box = label_y - 2 + font(LABEL_PX).getmetrics()[0], False
        else:
            fy, on_box = box[3] - 4, edited
        fx, fa = (x0 + pad, "ls") if right else (x1 - pad, "rs")
        d.text((fx, fy), "FINE", font=ffine, fill=BG if on_box else CYAN, anchor=fa)
        c.mark(e, "fine", (fx, fy), "FINE", ffine, fa)
    if sub and fs is not None:
        d.text((sx, sub_y), sub, font=fs, fill=FG if sub_strong and not dim else DIM, anchor=sa)
        c.mark(e, "sub", (sx, sub_y), sub, fs, sa)


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


def meter_bars(status: Any) -> list[tuple[float, bool]]:
    """The six meters as drawn (D-015), left to right: IN L, IN R (the program input's capture
    peaks, status.in_peak_db; IN CLIP lights both clip boxes), SUB L, SUB R, TOP L, TOP R
    (status.levels / clip). (dBFS, clipped) each; anything unreadable reads the floor. A single
    input peak (an older status) is drawn on both IN bars."""
    ins = _seq(_get(status, "in_peak_db", None))
    if len(ins) == 1:
        ins = ins * 2
    in_clip = bool(_truth(_get(status, "in_clip", None)))
    levels = _seq(_get(status, "levels", None))     # numpy arrays welcome (no `x or ()`)
    clips = _seq(_get(status, "clip", None))
    out = [(_level(ins, i), in_clip) for i in range(2)]
    for i in range(4):
        out.append((_level(levels, i), bool(_truth(clips[i])) if i < len(clips) else False))
    return out


def _bar(d: Any, x: float, bw: int, top: float, bot: float, clip_y0: float, clip_y1: float,
         db: float, clipped: bool) -> None:
    """One meter bar, -60..0 dBFS from `bot` up to `top`, its clip box above it."""
    def y_of(v: float) -> float:
        v = max(METER_FLOOR_DB, min(0.0, v))
        return bot - (bot - top) * (v - METER_FLOOR_DB) / -METER_FLOOR_DB

    xi = int(x)
    d.rectangle((xi, top, xi + bw - 1, bot), fill=BAR_BG)
    if db > METER_FLOOR_DB:
        for lo, hi, col in _METER_ZONES:
            if db <= lo:
                break
            d.rectangle((xi, y_of(min(db, hi)), xi + bw - 1, y_of(lo)), fill=col)
    if clipped:
        d.rectangle((xi - 1, clip_y0, xi + bw, clip_y1), fill=RED)
    else:
        d.rectangle((xi, clip_y0, xi + bw - 1, clip_y1), outline=FAINT)


def _bar_xs(cx: float, bw: int, gap: int, pair_gap: int) -> tuple[list[float], list[float]]:
    """x of each of the six bars (three L R pairs centred on `cx`) and each pair's centre."""
    total = 6 * bw + 3 * gap + 2 * pair_gap
    x = cx - total / 2
    xs, centres = [], []
    for _pair in range(3):
        xs += [x, x + bw + gap]
        centres.append(x + bw + gap / 2)
        x += 2 * bw + gap + pair_gap
    return xs, centres


def _meter(c: _Ctx, status: Any, y0: int = STATUS_H + 8, y1: int = H - 54) -> None:
    """ALIGN: 6 vertical peak bars, IN L R (the program input), SUB L R, TOP L R, -60..0 dBFS,
    a clip box on top of each (IN CLIP lights the IN pair's), the labels under them."""
    d = c.d
    bars = meter_bars(status)
    bw = 10
    clip_h = 9
    top = y0 + clip_h + 4
    for db_tick in (-6, -20, -40):
        yt = y1 - (y1 - top) * (db_tick - METER_FLOOR_DB) / -METER_FLOOR_DB
        d.line((METER_X0 + 2, yt, METER_X1 - 2, yt), fill=FAINT)
    xs, centres = _bar_xs((METER_X0 + METER_X1) / 2, bw, 3, 10)
    for x, (db, clipped) in zip(xs, bars):
        _bar(d, x, bw, top, y1, y0, y0 + clip_h, db, clipped)
    # labels: IN, SUB, TOP under their pairs, then L R (clear of the badge row under them)
    for cx, name in zip(centres, METER_PAIRS):
        d.text((cx, y1 + 2), name, font=font(12), fill=DIM, anchor="ma")
        d.text((cx, y1 + 13), "L R", font=font(10), fill=FAINT, anchor="ma")


def _mini_meter(c: _Ctx, status: Any) -> None:
    """SHOW: the same 6 bars (IN L R, SUB L R, TOP L R) compacted into METER_BOX, top centre."""
    d = c.d
    x0, y0, x1, y1 = METER_BOX
    bars = meter_bars(status)
    bw = 10
    top, bot = y0 + 8, y1 - 12
    xs, centres = _bar_xs((x0 + x1) / 2, bw, 3, 8)
    for x, (db, clipped) in zip(xs, bars):
        _bar(d, x, bw, top, bot, y0, y0 + 5, db, clipped)
    for cx, name in zip(centres, METER_PAIRS):
        d.text((cx, bot + 2), name, font=font(11), fill=DIM, anchor="ma")


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
           size: int = TOP_VALUE_PX, dim: bool = False, tag: str = "", tag_hot: bool = False,
           tag_short: str = "", value_dim: bool = False) -> None:
    """One encoder's block on SHOW: 'E1 MASTER' on the outer edge, the value in a box next to
    its knob (amber while edited, FINE while its push is held), flush to the outer border with
    its unit beside it (D-015). `tag` (the band's name) sits next to the label, amber-filled
    right after the band changed; `tag_short` ('LS', 'P2') replaces it when FINE leaves too
    little room for the whole name. `value_dim`: the value is grey (the selected band is OUT,
    D-014: stored, not playing)."""
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
    fx = font(TAG_PX)
    lab_col = DIM if dim else AMBER
    fine = e in c.held
    ffine = font(14)
    fine_tw = d.textlength("FINE", font=ffine)
    # the tag and a top block's FINE share the label's baseline
    base = label_y - 2 + font(LABEL_PX).getmetrics()[0]
    if fine and top:
        # a top block's FINE sits in its label row, at the inner end: the value row keeps its
        # whole width (a long Q and its '1/11 oct' would shrink to nothing next to it)
        fxy, fa = ((x1 - pad, base), "rs") if left else ((x0 + pad, base), "ls")
        d.text(fxy, "FINE", font=ffine, fill=CYAN, anchor=fa)
        c.mark(e, "fine", fxy, "FINE", ffine, fa)
    if left:
        end = _knob_label(c, e, label, x0 + pad, label_y, False, lab_col, LABEL_PX)
        if tag:
            tx = end + 8
            tw = d.textlength(tag, font=fx)
            if tag_hot:
                d.rounded_rectangle((tx - 4, label_y - 1, tx + tw + 4, base + 3), radius=3,
                                    fill=AMBER)
            d.text((tx, base), tag, font=fx, fill=BG if tag_hot else FG, anchor="ls")
            c.mark(e, "tag", (tx, base), tag, fx, "ls")
            end = tx + tw
        if not top:
            c.bottom_label_end["l"] = end
    else:
        end = _knob_label(c, e, label, x1 - pad, label_y, True, lab_col, LABEL_PX)
        if tag:
            tx = end - 8
            if fine and top:              # the band's name keeps clear of FINE
                room = int(max(1.0, tx - (x0 + pad + fine_tw + 8)))
                if tag_short and d.textlength(tag, font=fx) > room:
                    tag = tag_short       # 'HI SHELF' beside SLOPE and FINE: 'HS', never 'HI…'
                tsize, tag = _fit_ellipsis(tag, TAG_PX, room, min_size=11)
                fx = font(tsize)
            tw = d.textlength(tag, font=fx)
            if tag_hot:
                d.rounded_rectangle((tx - tw - 4, label_y - 1, tx + 4, base + 3), radius=3,
                                    fill=AMBER)
            d.text((tx, base), tag, font=fx, fill=BG if tag_hot else FG, anchor="rs")
            c.mark(e, "tag", (tx, base), tag, fx, "rs")
            end = tx - tw
        if not top:
            c.bottom_label_end["r"] = end
    cy = (by0 + by1) / 2
    edited = field_key is not None and c.editing == field_key
    if edited:
        d.rounded_rectangle((x0 + 4, by0, x1 - 4, by1), radius=8, fill=AMBER)
    fg = BG if edited else (DIM if dim or value_dim else FG)
    # A bottom block's FINE has its own column at the box's INNER edge (the value and its unit
    # are flush to the outer one) and sits at the box's top, clear of the unit's baseline row.
    low_fine = fine and not top
    fine_w = fine_tw + 14 if low_fine else 0.0
    _value_group(c, e, x0, x1, cy, value, unit, size=size, unit_px=UNIT_PX, right=not left,
                 fill=fg, unit_fill=BG if edited else DIM, reserve=fine_w, pad=pad)
    if low_fine:
        fxy, fa = ((x1 - 10, by0 + 3), "ra") if left else ((x0 + 10, by0 + 3), "la")
        d.text(fxy, "FINE", font=ffine, fill=BG if edited else CYAN, anchor=fa)
        c.mark(e, "fine", fxy, "FINE", ffine, fa)


def _dashed_rect(d: Any, box: tuple[float, float, float, float], fill: Any, width: int = 2,
                 dash: int = 6, gap: int = 4) -> None:
    """A dashed rectangle outline (an OUT chip, D-014)."""
    x0, y0, x1, y1 = box
    for a, b, horizontal, at in ((x0, x1, True, y0), (x0, x1, True, y1 - width + 1),
                                 (y0, y1, False, x0), (y0, y1, False, x1 - width + 1)):
        p = a
        while p < b:
            q = min(b, p + dash)
            if horizontal:
                d.rectangle((p, at, q, at + width - 1), fill=fill)
            else:
                d.rectangle((at, p, at + width - 1, q), fill=fill)
            p = q + gap


def _chips(c: _Ctx, sel: int, bands: Sequence[eg.Band]) -> None:
    """LS · 1 · 2 · 3 · 4 · HS (B2's order): the selected band amber, a band in use outlined in
    its cut (orange) / boost (green) colour, an idle band faint. A band switched OUT (D-014) is
    dark with a dashed outline (its cut/boost colour, amber when selected) and a small OUT."""
    d = c.d
    n, gap = len(BAND_CYCLE), 4
    w = (W - 8 - gap * (n - 1)) / n
    for k, slot in enumerate(BAND_CYCLE):
        bx0 = round(4 + k * (w + gap))
        bx1 = round(4 + k * (w + gap) + w)
        box = (bx0, CHIP_Y0, bx1, CHIP_Y1)
        b = bands[slot] if slot < len(bands) else None
        g = b.gain if b is not None else 0.0
        on = b.on if b is not None else True
        active = abs(g) >= 0.05
        acol = CUT if g < 0 else GREEN
        label = CHIP_TEXT.get(slot, "?")
        cxy = ((bx0 + bx1) / 2, (CHIP_Y0 + CHIP_Y1) / 2)
        if not on:
            col = AMBER if slot == sel else acol if active else DIM
            d.rounded_rectangle(box, radius=4, fill=OUT_CHIP_BG)
            _dashed_rect(d, box, col, width=2)
            d.text((bx0 + (bx1 - bx0) * 0.36, cxy[1]), label, font=font(CHIP_PX), fill=col,
                   anchor="mm")
            d.text((bx1 - 5, cxy[1] + 1), "OUT", font=font(11), fill=col, anchor="rm")
            continue
        if slot == sel:
            d.rounded_rectangle(box, radius=4, fill=AMBER)
            d.text(cxy, label, font=font(CHIP_PX), fill=BG, anchor="mm")
        else:
            d.rounded_rectangle(box, radius=4, outline=acol if active else FAINT,
                                width=2 if active else 1)
            d.text(cxy, label, font=font(CHIP_PX), fill=acol if active else DIM, anchor="mm")


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
    """A hint (amber) over SHOW's chip row: the marker and the band's name by the Q legend
    (E3) still say the band.
    A critical system alert takes it in its own colour when no hint is up (_page_show)."""
    d = c.d
    d.rectangle((0, CHIP_Y0, W, CHIP_Y1), fill=fill)
    size, text = _fit_ellipsis(text, 19, W - 16)
    d.text((W // 2, (CHIP_Y0 + CHIP_Y1) // 2 + 1), text, font=font(size), fill=fg, anchor="mm")


def _page_show(c: _Ctx, state: Any, status: Any) -> None:
    """SHOW: E1 MASTER, E2/E3/E4 = FREQ/Q/GAIN of the selected band (D-015: Q, or a shelf's
    SLOPE, top right with the band's name; GAIN bottom right), chips, the IN/SUB/TOP meters,
    and the live-EQ curve (the engine's own biquads, eqgraph) over the program spectrum."""
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
    out = slot < len(bands) and not bands[slot].on       # D-014: its values are stored, not played
    _block(c, 0, labels[0], fmt_db(_num(state, F_MASTER)), "dB", F_MASTER, size=TOP_VALUE_PX)
    _mini_meter(c, status)
    q = _num(state, base + ".q")
    # E3 (top right): Q, or the slope of a shelf (a Q of 0.4..1.5), with the band's name
    qlab, qunit = (labels[2], fmt_oct(q)) if slot < 4 else ("SLOPE", "Q")
    _block(c, 2, qlab, fmt_q(q), qunit, base + ".q", size=TOP_VALUE_PX,
           tag=LIVE_SLOT_LABELS[slot], tag_short=LIVE_SLOT_SHORT[slot],
           tag_hot=c.editing == F_SLOT, value_dim=out)
    _block(c, 1, labels[1], fmt_hz(_num(state, base + ".freq")), "Hz", base + ".freq",
           size=BOT_VALUE_PX, value_dim=out)
    _block(c, 3, labels[3], fmt_db(_num(state, base + ".gain")), "dB", base + ".gain",
           size=BOT_VALUE_PX, value_dim=out)
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
    """Seconds left of ALIGN's E2/E4 unlock: the status's figure or the focus's own deadline,
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
    """D-015: on the bottom row, between the two locked knobs' labels (E2 DELAY bottom left,
    E4 LO-CUT bottom right) and joined to both by a line: LOCKED · HOLD B2 (grey, how to
    unlock) or EDIT m:ss (amber, the unlock's countdown)."""
    d = c.d
    lx = c.bottom_label_end.get("l", 90.0) + 8
    rx = c.bottom_label_end.get("r", W - 90.0) - 8
    cy = (LOCK_Y0 + LOCK_Y1) / 2
    cx = (lx + rx) / 2
    unlocked = left_s > 0
    t = f"EDIT {fmt_mmss(left_s)}" if unlocked else "LOCKED · HOLD B2"
    col = AMBER if unlocked else DIM
    size, t = _fit_ellipsis(t, 16, int(max(40.0, rx - lx - 50)), min_size=12)
    f = font(size)
    tw = d.textlength(t, font=f)
    bx0, bx1 = cx - tw / 2 - 9, cx + tw / 2 + 9
    for a, b in ((lx, bx0 - 4), (bx1 + 4, rx)):          # the line to each locked knob
        if b - a >= 6:
            d.line((a, cy, b, cy), fill=col, width=2)
    if unlocked:
        d.rounded_rectangle((bx0, LOCK_Y0, bx1, LOCK_Y1), radius=6, fill=AMBER)
        d.text((cx, cy), t, font=f, fill=BG, anchor="mm")
    else:
        d.rounded_rectangle((bx0, LOCK_Y0, bx1, LOCK_Y1), radius=6, fill=BG, outline=DIM,
                            width=2)
        d.text((cx, cy), t, font=f, fill=DIM, anchor="mm")
    c.ink.setdefault(-1, {})["badge"] = (bx0, LOCK_Y0, bx1, LOCK_Y1)


def _page_align(c: _Ctx, state: Any, status: Any) -> None:
    """ALIGN: E1 MASTER and E3 BALANCE live; E2 DELAY and E4 LO-CUT, the two bottom knobs, dimmed
    while locked (a B2 hold unlocks them for 30 s of idle: the badge between them counts it
    down). Lo-Cut on/off is a SETUP item: said here, not offered. E2 is the signed sub/top delay
    (D-011): its size big, which side is late in words over it (SUBS LATER / TOPS LATER /
    ALIGNED). The meters in the centre: IN L R (the program input), SUB L R, TOP L R (D-015)."""
    master = _num(state, F_MASTER)
    bal = _num(state, F_BALANCE)
    ad_val, ad_sub = fmt_align_delay(align_delay(state))
    lc_on = _truth(_get(state, "lowcut_on", True)) is not False
    lc = _num(state, F_LOWCUT_FREQ)
    left = align_unlock_left(c, status)
    locked = left <= 0.0
    labels = knob_labels("ALIGN")
    _meter(c, status)
    _quadrant(c, 0, labels[0], fmt_db(master), "dB", "", F_MASTER)
    _quadrant(c, 1, labels[1], ad_val, "ms", ad_sub, F_ALIGN_DELAY, dim=locked, sub_strong=True)
    bsub = "subs = tops" if bal is None or abs(bal) < 0.05 else \
        f"subs {bal / 2:+.1f} · tops {-bal / 2:+.1f}"
    _quadrant(c, 2, labels[2], fmt_db(bal), "dB", bsub, F_BALANCE)
    if lc_on:
        _quadrant(c, 3, labels[3], fmt_hz(lc), "Hz", "tops · 12 dB/oct", F_LOWCUT_FREQ, dim=locked)
    else:
        _quadrant(c, 3, labels[3], "OFF", "", f"{fmt_hz(lc)} Hz · on/off in SETUP", F_LOWCUT_FREQ,
                  dim=True)
    _lock_badge(c, left)
    _middle(c, status)


# ------------------------------------------------------------------------------------------
# SETUP
# ------------------------------------------------------------------------------------------
def _fmt_setup(item, v: Any) -> str:
    """One SETUP value as the row shows it (also the chosen value of a pending choice)."""
    if v is None:
        return "--"
    if item.field.endswith("_inverted"):
        return "INVERTED" if v else "normal"
    if isinstance(v, bool) or item.kind == "bool":
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


def _setup_value(state: Any, item, status: Any = None, readonly: Any = None) -> str:
    if item.kind == "page":
        return "PUSH ▸"
    if item.kind == "action":
        if item.field == A_UNDO_RESET:
            left = _num(status, "undo_reset_s", 0.0) or 0.0
            return f"UNDO {fmt_mmss(left)}" if left > 0 else "—"
        if item.field == A_LAPTOP:
            return _laptop_value(status)
        return "PUSH ×2" if getattr(item, "confirm", False) else "PUSH ▸"
    if item.field == F_SPECTRUM_ON and (_get(status, "spectrum_state", "") == "disabled"
                                        or item.field in (readonly or {})):
        return "DISABLED"
    if item.field == "dev_audition" and item.field in (readonly or {}):
        return "—"                                     # not the dev profile: nothing to audition
    v = _get(state, item.field, None)
    out = _fmt_setup(item, v)
    if item.field == F_LOWCUT_ON and v is not None:
        hz = _num(state, "lowcut_hz", None)
        if hz is not None:
            out += f" · {fmt_hz(hz)} Hz"               # the frequency itself: ALIGN E4
    if item.field == "profile" and item.field in (readonly or {}):
        out += " · PINNED"
    return out


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


def _setup_unavailable(item, status: Any, readonly: Any = None) -> bool:
    """A row with nothing to do right now (Undo reset with no undo, Laptop access without its
    surface, a field the Controller holds read-only: Focus.readonly): its label is dimmed."""
    if item.field in (readonly or {}):
        return True
    if item.kind == "action" and item.field == A_UNDO_RESET:
        return not (_num(status, "undo_reset_s", 0.0) or 0.0) > 0
    if item.kind == "action" and item.field == A_LAPTOP:
        return _get(status, "laptop_state", "off") in ("off", "unavailable")
    return item.field == F_SPECTRUM_ON and _get(status, "spectrum_state", "") == "disabled"


def _setup_sel(c: _Ctx) -> int:
    try:
        return max(0, min(len(SETUP_ITEMS) - 1, int(getattr(c.focus, "item", 0) or 0)))
    except (TypeError, ValueError):
        return 0


def _setup_pick(c: _Ctx, item) -> Any:
    """The value chosen for `item` and waiting for its push, or None (D-013). Never for a
    field that has become read-only meanwhile: its push only shows the hint."""
    if item.field in (getattr(c.focus, "readonly", None) or {}):
        return None
    fn = getattr(c.focus, "pick_now", None)
    try:
        pk = fn(c.now) if callable(fn) else None
    except Exception:
        return None
    return pk if pk is not None and pk[0] == item.field else None


def _setup_prompt(c: _Ctx) -> str | None:
    """SETUP's footer while a choice waits for its push: what the push will set."""
    item = SETUP_ITEMS[_setup_sel(c)]
    pk = _setup_pick(c, item)
    if pk is None:
        return None
    text = f"PUSH TO SET {_fmt_setup(item, pk[1]).upper()}"
    return text + " · MAY CLICK" if item.rebuild else text


def _setup_help(c: _Ctx, item, readonly: Any) -> str:
    """The bottom line's right side: what the knobs do on the selected row."""
    if item.field in (readonly or {}):
        return "fixed here"
    if item.kind == "num":
        return "turn = change"
    if item.picks:
        return "push = set" if _setup_pick(c, item) is not None else "turn = choose · push = set"
    if item.kind == "page":
        return "push = open"
    twice = item.needs_confirm and item.field not in (getattr(c.focus, "no_confirm", None) or ())
    return "push ×2 = run" if twice else "push = run"


# SETUP rows (D-013): a header per group, then its items. 8 rows of 28 px from y 60 to 284.
SETUP_ROWS: tuple[tuple[str, Any], ...] = tuple(
    r for name, _items in SETUP_GROUPS
    for r in [("head", name)] + [("item", k) for k, it in enumerate(SETUP_ITEMS) if it.group == name])


def _page_setup(c: _Ctx, state: Any, status: Any) -> None:
    """SETUP (D-013): the items grouped by speaker floor. Top line: E1 ITEM, the group and the
    position in it, E3 GROUP. The list: a header per group (the selected item's in white), the
    selected row amber, red while a push would change something (a chosen value: `now ->
    chosen`; an action waiting for its second push: PUSH AGAIN). A scroll bar on the right.
    Bottom line: E2 VALUE and what the knobs do on that row; the footer: a hint, else the
    pending choice's prompt."""
    d = c.d
    labels = knob_labels("SETUP")
    for e, lab in enumerate(labels):
        if not lab:
            continue
        corner = KNOB_CORNERS[e]
        y = STATUS_H + 4 if corner[0] == "t" else H - 24
        right = corner[1] == "r"
        _knob_label(c, e, lab, W - 10 if right else 10, y, right)
    sel = _setup_sel(c)
    item_sel = SETUP_ITEMS[sel]
    readonly = getattr(c.focus, "readonly", None) or {}
    grp, pos, size = setup_group_pos(sel)
    head = f"{grp} {pos}/{size}"
    d.text((W // 2, STATUS_H + 2), head, font=_fit(d, head, 20, 150), fill=FG, anchor="ma")
    d.text((W - 10, H - 22), _setup_help(c, item_sel, readonly), font=font(16, FONT_REG),
           fill=DIM, anchor="ra")
    conf_fn = getattr(c.focus, "confirm_now", None)
    confirm = conf_fn(c.now) if callable(conf_fn) else None
    rows, row_h, y0 = 8, 28, STATUS_H + 30
    sel_row = next(r for r, (k, v) in enumerate(SETUP_ROWS) if k == "item" and v == sel)
    head_row = next(r for r in range(sel_row, -1, -1) if SETUP_ROWS[r][0] == "head")
    first = sel_row - 3
    if head_row < first and sel_row - head_row <= rows - 2:
        first = head_row                      # the group's header stays on screen when it fits
    first = max(0, min(first, len(SETUP_ROWS) - rows))
    x_val = W - 18
    for r in range(rows):
        if first + r >= len(SETUP_ROWS):
            break
        kind, v = SETUP_ROWS[first + r]
        y = y0 + r * row_h
        cy = y + row_h // 2 - 1
        if kind == "head":
            hcol = FG if v == grp else DIM
            d.text((10, cy), v, font=font(15), fill=hcol, anchor="lm")
            hx = 10 + int(d.textlength(v, font=font(15))) + 8
            d.line((hx, cy, x_val, cy), fill=FAINT, width=1)
            continue
        item = SETUP_ITEMS[v]
        is_sel = v == sel
        pk = _setup_pick(c, item) if is_sel else None
        pending = is_sel and (pk is not None or confirm == item.field)
        if is_sel:
            d.rectangle((4, y, W - 10, y + row_h - 3), fill=RED if pending else AMBER)
        dimmed = _setup_unavailable(item, status, readonly)
        fg = FG if pending else (BG if is_sel else (DIM if dimmed else FG))
        d.text((24, cy), item.label, font=font(20, FONT_BOLD if is_sel else FONT_REG), fill=fg,
               anchor="lm")
        if pk is not None:
            val = f"{_fmt_setup(item, _get(state, item.field, None))} → {_fmt_setup(item, pk[1])}"
        elif pending:
            val = "PUSH AGAIN"
        else:
            val = _setup_value(state, item, status, readonly)
        vcol = fg if is_sel else (DIM if dimmed else (AMBER if item.rebuild else fg))
        if not is_sel and item.field == A_LAPTOP and val.startswith("OPEN "):
            vcol = AMBER                       # a gate open in the fence: never quiet
        lw = 24 + int(d.textlength(item.label, font=font(20, FONT_BOLD if is_sel else FONT_REG)))
        d.text((x_val, cy), val, font=_fit(d, val, 20, x_val - lw - 16, FONT_REG if not is_sel else FONT_BOLD),
               fill=vcol, anchor="rm")
    # scroll bar: where the 8 rows sit in the whole list
    ty0, ty1 = y0, y0 + rows * row_h - 3
    d.rectangle((W - 6, ty0, W - 3, ty1), fill=FAINT)
    n = len(SETUP_ROWS)
    t0 = ty0 + (ty1 - ty0) * first // n
    t1 = ty0 + (ty1 - ty0) * min(n, first + rows) // n
    d.rectangle((W - 6, t0, W - 3, t1), fill=DIM)
    footer = _hint(c) or _setup_prompt(c)
    if footer:
        d.rectangle((0, H - FOOTER_H, W, H), fill=AMBER)
        d.text((W // 2, H - FOOTER_H // 2), footer, font=_fit(d, footer, 20, W - 20), fill=BG,
               anchor="mm")


# ------------------------------------------------------------------------------------------
# HWTEST (unchanged from v0)
# ------------------------------------------------------------------------------------------
def _page_hwtest(c: _Ctx, state: Any, status: Any) -> None:
    d = c.d
    hw = getattr(c.focus, "hw", None)
    d.text((W // 2, STATUS_H + 6), "KNOB TEST", font=font(24), fill=AMBER, anchor="ma")
    sub = "turn / press each control · hold B4 = mute · hold any other = exit"
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
      it comes in BY ITSELF, and B4 keeps it muted (the controller resolves B4 that way);
    - a controller alert (APPLYING SETUP…, NO AUDIO FLOWING · HELD MUTED, ENGINE SYNC FAILED,
      KNOBS OFFLINE…) says why, instead of READY;
    - READY only when the engine RUNS (not Paused/Starting) and the controller is in sync.
    """
    hint = _hint(c)
    engine = engine_state(status)
    running = engine is not None and engine.upper() == "RUNNING"
    if _truth(_get(status, "ramping", None)) and _truth(_get(state, "muted", None)) is False:
        # (the status bar says whether the engine is processing yet: STARTING / RUNNING)
        return hint, "RAMPS IN BY ITSELF · hold B4 to keep muted"
    alert = str(_get(status, "alert", "") or "")
    if alert:
        return hint, alert
    ready = running and _truth(_get(status, "ready", None)) is not False
    return hint, ("READY · hold B4 to unmute" if ready else "hold B4 to unmute")


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
    ALIGN (and the v0 names): the v0 full-width band across the middle strip, between the top
    sub-line and the bottom ones (D-015: the delay's side stays readable). SETUP: red frame +
    red footer strip (a band would cover the selected row and its 'PUSH AGAIN'); an active hint
    takes the footer for its 2.5 s. HWTEST: frame + red tag.
    """
    d = c.d
    d.rectangle((0, STATUS_H, W - 1, H - 1), outline=BAND_RED, width=4)
    hint, unmute = _mute_hint(c, state, status)
    page = page_name(page)
    if page == "SETUP":
        if not hint and not _setup_prompt(c):
            d.rectangle((0, H - FOOTER_H, W, H), fill=BAND_RED)
            cy = H - FOOTER_H // 2
            d.text((14, cy), "MUTED", font=font(22), fill=FG, anchor="lm")
            fx = 14 + int(d.textlength("MUTED", font=font(22))) + 12
            d.text((W - 14, cy), unmute, font=_fit(d, unmute, 18, W - fx - 14, FONT_REG),
                   fill=FG, anchor="rm")
        # else: the hint (its 2.5 s) or a pending choice's prompt has the footer (the page)
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
    y0, y1 = ALIGN_MUTED_BAND   # between the sub-lines, never half over one (D-015)
    d.rectangle((0, y0, W, y1), fill=BAND_RED)
    _band_text(c, y0, y1, 52, 22, 18, hint or unmute)


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
    c = _Ctx(focus, now, knob_tags(status))
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
    """(state, focus) after a B3 tap on SHOW with `slot` selected and the spectrum's PEAK at
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
                       ready=True, spectrum=spec, spectrum_state="live",
                       in_peak_db=(-11.0, -12.5))

    def with_(base: ScreenStatus, **kw: Any) -> ScreenStatus:
        return ScreenStatus(**{**base.__dict__, **kw})

    run_plain = with_(run, spectrum=plain)
    run_clip = with_(run, levels=(-14.0, -15.5, -9.0, -2.5), clip=(False, False, False, True))
    muted = State.from_dict({**s.to_dict(), "muted": True})
    run_muted = with_(run, muted=True)
    # B3 (the real notch helper): with no bell in use within 1/6 oct of the PEAK (P2 is at
    # 1 kHz here), P3 (selected, free) goes to the PEAK at Q 8.65, gain 0; with P2 cutting at
    # 2.52k (0.05 oct from the PEAK) it selects P2 and writes nothing.
    away = State.from_dict(s.to_dict())
    away.live_peq[1].freq = 1000.0
    notch, notch_focus = demo_notch(away, peak, 2)
    renotch, renotch_focus = demo_notch(s, peak, 2)
    pills = State.from_dict({**s.to_dict(), "lowcut_on": False, "sub_inverted": True,
                             "mono_subs": True})
    # D-014: P2 (cutting 6.5 dB at 2.52k) switched OUT by a B3 hold, and selected: the curve
    # without it, its chip dashed with OUT, its marker hollow at -6.5, its values grey, the
    # OUT P2 pill; the hint is the hold's answer
    band_out = State.from_dict(s.to_dict())
    band_out.live_peq[1].on = False
    lc_off = State.from_dict({**s.to_dict(), "lowcut_on": False})
    dev = ScreenStatus(profile="dev", device="HiFiBerry", device_present=True, engine="Running",
                       load=0.1, ready=True, laptop_state="closed")
    dev_muted = with_(dev, muted=True)
    knobs = "KNOBS OFFLINE · USE PHONE MUTE / BYPASS"
    hw = HwTestLog(enc_count=[3, -2, 0, 7], sw_down=[False, False, False, False, False, False,
                                                     False, True],
                   sw_last=["short", "", "", "", "", "", "", "long"], last="sw7 press  = B4")
    hwf = Focus(hw=hw)
    reset_i, undo_i = _setup_index(A_RESET_LIVE), _setup_index(A_UNDO_RESET)
    laptop_i = _setup_index(A_LAPTOP)
    dev_open = with_(dev, laptop_state="open", laptop_s=7140.0,
                     pills=["MONO SUB", "LAPTOP OPEN 1:59"])
    mono_i = _setup_index("mono_subs")
    aud_i, prof_i, tpol_i = _setup_index("dev_audition"), _setup_index("profile"), _setup_index("top_inverted")
    ro = {"profile": "PROFILE PINNED IN k1000.yaml"}     # k1000.yaml names the profile (the box)
    mono_pick = dict(pick="mono_subs", pick_value=not bool(s.mono_subs), pick_until=inf, readonly=ro)
    tops_later = State.from_dict({**s.to_dict(), "sub_delay_ms": 0.0, "top_delay_ms": 1.25})
    aligned = State.from_dict({**s.to_dict(), "sub_delay_ms": 0.0, "top_delay_ms": 0.0})
    # D-015: the input clipping in the ADC (IN CLIP latched, the IN bars at full scale), and the
    # longest values each corner can show (none is ever cut or runs into its neighbour)
    in_clip = with_(run, in_clip=True, in_peak_db=(-0.2, -1.5))
    longest = State.from_dict(s.to_dict())
    longest.master_db, longest.balance_db = -120.0, -12.0
    longest.sub_delay_ms, longest.top_delay_ms, longest.lowcut_hz = 0.0, 50.0, 300.0
    longest.live_peq[1].freq, longest.live_peq[1].gain, longest.live_peq[1].q = 20000.0, -20.0, 0.1
    p3 = longest.live_peq[2]
    p3.freq, p3.gain, p3.q = 10500.0, -12.5, 16.0
    longest.live_highshelf.freq, longest.live_highshelf.gain = 16000.0, -12.0
    longest.live_highshelf.q = 1.5
    return [
        # SHOW: the front page
        ("show", "SHOW", s, run, Focus(slot=1)),
        # ui.knob_tags (D-011): `dim` is the default every other picture shows; `off` (a
        # sticker on the shell names the knobs) and v1's `bright`, to compare
        ("show_tags_off", "SHOW", s, with_(run, knob_tags="off"), Focus(slot=1)),
        ("show_tags_bright", "SHOW", s, with_(run, knob_tags="bright"), Focus(slot=1)),
        ("align_tags_off", "ALIGN", s, with_(run, knob_tags="off"), Focus()),
        ("show_edit_fine", "SHOW", s, run_clip, Focus(slot=1, editing="live_peq.1.freq",
                                                      edit_until=inf, held={1})),
        ("show_notch", "SHOW", notch, run, notch_focus),
        ("show_notch_reselect", "SHOW", renotch, run, renotch_focus),
        ("show_band", "SHOW", s, run_plain, Focus(slot=2, editing=F_SLOT, edit_until=inf)),
        ("show_shelf", "SHOW", s, run_plain, Focus(slot=4, editing="live_lowshelf.gain",
                                                   edit_until=inf)),
        ("show_band_out", "SHOW", band_out, with_(run_plain, pills=["OUT P2"]), Focus(slot=1)),
        ("show_band_out_hint", "SHOW", band_out, with_(run_plain, pills=["OUT P2"]),
         Focus(slot=1, hint="P2 OUT", hint_until=inf)),
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
        ("input_clip", "SHOW", s, in_clip, Focus(slot=1)),
        # D-015: the longest values; show_long_values as they rest (no edit, no FINE),
        # show_long_q with FINE held on all four knobs (the worst case for room: FINE takes a
        # bottom box's inner corner and a top knob's label row), show_long_slope a shelf's SLOPE
        # with FINE held on E3 and E4 (the band's name gives way to its short name, 'HS')
        ("show_long_values", "SHOW", longest, run_clip, Focus(slot=1)),
        ("show_long_q", "SHOW", longest, run, Focus(slot=2, editing="live_peq.2.q",
                                                    edit_until=inf, held={0, 1, 2, 3})),
        ("show_long_slope", "SHOW", longest, run, Focus(slot=5, editing="live_highshelf.gain",
                                                        edit_until=inf, held={2, 3})),
        ("alert_warning", "SHOW", s, with_(run, alerts=[("warning", "PIPELINE CHANGED (GUI)")],
                                           message="PIPELINE CHANGED (GUI)"), Focus(slot=1)),
        ("muted", "SHOW", muted, run_muted, Focus(slot=1)),
        ("muted_fine", "SHOW", muted, run_muted, Focus(slot=1, editing="live_peq.1.q",
                                                       edit_until=inf, held={2})),
        # K1000 holds Main before its own boot ramp: not 'READY · hold B4 to unmute'
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
         Focus(editing=F_ALIGN_DELAY, edit_until=inf)),
        # the signed delay (D-011): the tops delayed (the tops stand in front of the subs), and
        # exactly aligned (where a turn stops for one click before crossing to the other side)
        ("align_tops_later", "ALIGN", tops_later, with_(run, align_locked=False,
                                                         align_unlock_s=27.4),
         Focus(editing=F_ALIGN_DELAY, edit_until=inf)),
        ("align_aligned", "ALIGN", aligned, with_(run, align_locked=False, align_unlock_s=12.0),
         Focus(editing=F_ALIGN_DELAY, edit_until=inf)),
        ("align_lowcut_off", "ALIGN", lc_off, run, Focus()),
        ("align_muted", "ALIGN", muted, run_muted, Focus()),
        ("align_input_clip", "ALIGN", s, in_clip, Focus()),
        ("align_long_values", "ALIGN", longest, with_(run_clip, align_locked=False,
                                                      align_unlock_s=27.4),
         Focus(editing=F_ALIGN_DELAY, edit_until=inf, held={0, 1, 2, 3})),
        # SETUP
        # SETUP (D-013): grouped by speaker floor; E2 chooses, a push sets
        ("setup", "SETUP", s, dev, Focus(item=tpol_i, readonly=ro)),
        ("setup_reset_confirm", "SETUP", s, dev, Focus(item=reset_i, confirm=A_RESET_LIVE,
                                                       confirm_until=inf,
                                                       hint="PUSH AGAIN TO RESET ALL LIVE EQ",
                                                       hint_until=inf, readonly=ro)),
        ("setup_undo", "SETUP", s, with_(dev, undo_reset_s=287.0), Focus(item=undo_i, readonly=ro)),
        ("setup_confirm", "SETUP", s, dev, Focus(item=mono_i, **mono_pick)),
        ("setup_pick_audition", "SETUP", s, dev,
         Focus(item=aud_i, pick="dev_audition", pick_value="tops", pick_until=inf, readonly=ro)),
        ("setup_profile_pinned", "SETUP", s, dev,
         Focus(item=prof_i, readonly=ro, hint="PROFILE PINNED IN k1000.yaml", hint_until=inf)),
        ("setup_muted", "SETUP", muted, dev_muted, Focus(item=mono_i, **mono_pick)),
        ("setup_applying", "SETUP", s, with_(dev, alert="APPLYING SETUP…", message="APPLYING SETUP…",
                                             alerts=[("info", "APPLYING SETUP…")]),
         Focus(item=mono_i, readonly=ro)),
        ("setup_laptop_confirm", "SETUP", s, dev,
         Focus(item=laptop_i, confirm=A_LAPTOP, confirm_until=inf,
               hint="PUSH AGAIN: LAPTOP ACCESS FOR 2 H", hint_until=inf, readonly=ro)),
        ("setup_laptop_open", "SETUP", s, dev_open, Focus(item=laptop_i - 1, readonly=ro)),
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
