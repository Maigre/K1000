"""Screen pages for the 480x320 ZynScreen: render(page, state, status, focus) -> PIL image.

Design rules (why it looks like this):
- Read at arm's length, outdoors, by someone mixing: black background, white 60 px digits,
  amber labels, one colour per meaning (amber = edited/attention, red = mute/clip/failure).
- The screen mirrors the knobs: each encoder's value sits in the quadrant next to its corner
  (KNOB_CORNERS in input_map, an assumption until the physical positions are confirmed).
- The 4-bar output meter stays in the centre on the show pages: clipping must be visible while
  notching.
- Overlays are derived from the status, never a page the controller must remember to show:
  NO DEVICE > ENGINE DOWN replace the body. MUTED is a red pill in the status bar plus a red
  frame on every page; on MAIN/LIVE also a full-width red band, on SETUP a red footer (a band
  would hide the selected row), on the Knob test a red tag.
- Unknown is not OK (fail closed): a status that does not say the engine runs shows an amber
  "ENGINE ?", one that does not say the device is present shows "DEVICE ?", never green.
- Rendering only reads attributes (`state.master_db`, `state.live_peq[1].gain`, `status.load`),
  and every read has a fallback, so a partial status or an older State never crashes the screen;
  anything that still raises gives a "UI ERROR" frame (with the mute state) instead.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Sequence

from PIL import Image, ImageDraw, ImageFont

from .input_map import (F_BALANCE, F_LOWCUT_FREQ, F_LOWCUT_ON, F_MASTER, F_SLOT, F_SUB_DELAY,
                        KNOB_CORNERS, LIVE_SLOT_LABELS, LIVE_SLOT_SHORT, LIVE_SLOTS, PAGES,
                        SETUP_ITEMS, Focus, knob_labels)

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

STATUS_H = 30
METER_X0, METER_X1 = 206, 274          # centre column
MID_Y0, MID_Y1 = 150, 200              # middle strip (page name / hints / MUTED band)
OVERLAYS: tuple[str, ...] = ("NO DEVICE", "ENGINE DOWN", "MUTED")
METER_LABELS: tuple[str, ...] = ("Sub L", "Sub R", "Top L", "Top R")
METER_FLOOR_DB = -60.0
ENGINE_OK = ("RUNNING", "PAUSED", "STARTING")
ENGINE_DOWN_STATES = ("INACTIVE", "STALLED", "DOWN")
ENGINE_UNKNOWN = "Unknown"             # status did not say (missing field, no status at all)
FOOTER_H = 28                          # SETUP footer row (E2 label / hint / MUTED strip)


# ------------------------------------------------------------------------------------------
# What the screen reads from the controller
# ------------------------------------------------------------------------------------------
@dataclass
class ScreenStatus:
    """The status fields the screen reads (any object with these attributes works).

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
    alert:  the controller's most urgent message (APPLYING SETUP…, HELD MUTED, SYNC FAILED,
            KNOBS OFFLINE…): also shown inside the MUTED band and on SETUP, where `message`
            has no room.
    in_clip: the INPUT (ADC) clipped recently (latched by the controller).
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
    message: str = ""                    # one line for the middle strip (e.g. "ramping in")
    stop_reason: str = ""                # engine stop reason, shown on ENGINE DOWN
    ready: bool | None = None
    ramping: bool = False
    alert: str = ""
    in_clip: bool = False


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


@lru_cache(maxsize=2048)
def _textlength(text: str, f: ImageFont.FreeTypeFont) -> float:
    return f.getlength(text)


@lru_cache(maxsize=512)
def _text_mask(text: str, f: ImageFont.FreeTypeFont, anchor: str):
    """Rasterised text as an 'L' mask + its offset from the anchor point.

    Why: glyph rasterising is ~70 % of a frame. Most strings (labels, unchanged values) repeat
    from frame to frame — e.g. while only the meter moves — so caching them keeps a frame well
    under the 30 ms budget on the Pi 4.
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

    `shift` moves the value towards the screen edge (frees room for the LIVE slot pills);
    `box_half` is the half-height of the edit highlight.
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
    # value (highlighted while being edited; B3 toggling the Lo-Cut highlights its frequency)
    edited = field_key is not None and (c.editing == field_key or (
        field_key == F_LOWCUT_FREQ and c.editing == F_LOWCUT_ON))
    fine = e in c.held
    box = (x0 + 4, val_cy - box_half, x1 - 4, val_cy + box_half)
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
    if sub:
        fs = _fit(d, sub, 18, (x1 - x0) - 2 * pad, FONT_REG)
        d.text(((x0 + x1) / 2, sub_y), sub, font=fs, fill=DIM, anchor="mm")


def _status_bar(c: _Ctx, page: str, state: Any, status: Any, muted: bool) -> None:
    """[PAGE] [MUTED] profile · device ........ ● ENGINE  DSP load%

    Unknown engine -> amber "ENGINE ?"; unknown device -> amber "... ?": never a healthy look
    for something nobody reported.
    """
    d = c.d
    cy = STATUS_H // 2
    d.rectangle((0, 0, W, STATUS_H - 1), fill=(28, 28, 28))
    name = {"LIVE": "LIVE EQ", "HWTEST": "TEST"}.get(page, page)
    fp = font(16)
    pw = int(d.textlength(name, font=fp)) + 12
    d.rounded_rectangle((4, 4, 4 + pw, STATUS_H - 5), radius=4, fill=FG)
    d.text((4 + pw / 2, cy), name, font=fp, fill=BG, anchor="mm")
    lx = 4 + pw + 6
    if muted:   # on every page, whatever the body shows
        mw = int(d.textlength("MUTED", font=fp)) + 12
        d.rounded_rectangle((lx, 4, lx + mw, STATUS_H - 5), radius=4, fill=BAND_RED)
        d.text((lx + mw / 2, cy), "MUTED", font=fp, fill=FG, anchor="mm")
        lx += mw + 6
    # A latched clip, readable at arm's length (the meter's clip boxes are 12 px): CLIP = an
    # output reached full scale, IN CLIP = the input ADC did (no K1000 knob fixes that one).
    pills = [t for t, on in (("IN CLIP", _truth(_get(status, "in_clip", None))),
                             ("CLIP", any(bool(_truth(v)) for v in _seq(_get(status, "clip", None)))))
             if on]
    for t in pills:
        cw = int(d.textlength(t, font=fp)) + 12
        d.rounded_rectangle((lx, 4, lx + cw, STATUS_H - 5), radius=4, fill=RED)
        d.text((lx + cw / 2, cy), t, font=fp, fill=FG, anchor="mm")
        lx += cw + 6
    # right side first, so the left text gets whatever room is left
    engine = engine_state(status)
    if engine is None:
        eng, col = "NO ENGINE", RED
    elif engine.upper() == ENGINE_UNKNOWN.upper():
        eng, col = "ENGINE ?", AMBER
    else:
        eng = engine.upper()
        col = GREEN if eng == "RUNNING" else (YELLOW if eng in ("PAUSED", "STARTING") else RED)
    load = _num(status, "load", None)
    x = W - 8
    if load is not None:
        right = f"{load:.1f}%"
        d.text((x, cy), right, font=font(17), fill=FG, anchor="rm")
        x -= int(d.textlength(right, font=font(17))) + 4
        d.text((x, cy + 1), "DSP", font=font(12), fill=DIM, anchor="rm")
        x -= 38
    d.text((x, cy), eng, font=_fit(d, eng, 17, 150), fill=col, anchor="rm")
    x -= int(d.textlength(eng, font=_fit(d, eng, 17, 150))) + 8
    d.ellipse((x - 12, cy - 6, x, cy + 6), fill=col)
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
    room = x - 20 - lx
    # Pills (MUTED, CLIP...) come first; the profile text shrinks, then loses the device name,
    # then goes: it never overprints the engine state.
    for text in (left, left.split(" · ")[0]):
        f = _fit(d, text, 17, max(1, room))
        if room > 0 and d.textlength(text, font=f) <= room:
            d.text((lx, cy), text, font=f, fill=lcol, anchor="lm")
            break


def _meter(c: _Ctx, status: Any, y0: int = STATUS_H + 8, y1: int = H - 30) -> None:
    """4 vertical peak bars (Sub L, Sub R, Top L, Top R), -60..0 dBFS, clip box on top."""
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
        lv = levels[i] if i < len(levels) else None
        try:
            lv = float(lv) if lv is not None else METER_FLOOR_DB
        except (TypeError, ValueError):
            lv = METER_FLOOR_DB
        if not math.isfinite(lv):
            lv = METER_FLOOR_DB
        d.rectangle((xi, top, xi + bw - 1, y1), fill=BAR_BG)
        if lv > METER_FLOOR_DB:
            for lo, hi, col in ((METER_FLOOR_DB, -18.0, GREEN), (-18.0, -6.0, YELLOW),
                                (-6.0, 0.0, RED)):
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


def _unknown_notice(status: Any) -> str:
    """'STATUS ? engine · device' when the controller did not report them (fail closed)."""
    parts = [n for n, unknown in (("engine", engine_unknown(status)),
                                  ("device", device_present(status) is None)) if unknown]
    return ("STATUS ? " + " · ".join(parts)) if parts else ""


def _middle(c: _Ctx, status: Any = None) -> None:
    """Middle strip: a hint toast, else the controller's one-line message (e.g. 'ramping in'),
    else a notice when the engine/device state is unknown."""
    d = c.d
    cy = (MID_Y0 + MID_Y1) // 2
    msg = str(_get(status, "message", "") or "")
    hint_fn = getattr(c.focus, "hint_now", None)
    hint = hint_fn(c.now) if callable(hint_fn) else None
    text = hint or msg or _unknown_notice(status)
    if text:
        d.rectangle((0, cy - 18, W, cy + 18), fill=AMBER if hint else (60, 50, 0))
        d.text((W // 2, cy), text, font=_fit(d, text, 24, W - 20), fill=BG if hint else YELLOW,
               anchor="mm")


# ------------------------------------------------------------------------------------------
# Pages
# ------------------------------------------------------------------------------------------
def _page_main(c: _Ctx, state: Any, status: Any) -> None:
    master = _num(state, F_MASTER)
    bal = _num(state, F_BALANCE)
    sd = _num(state, F_SUB_DELAY)
    lc_on = bool(_get(state, "lowcut_on", True))
    lc = _num(state, F_LOWCUT_FREQ)
    labels = knob_labels("MAIN")
    _quadrant(c, 0, labels[0], fmt_db(master), "dB", "", F_MASTER)
    if bal is None or abs(bal) < 0.05:
        bsub = "subs = tops"
    else:
        bsub = f"subs {bal / 2:+.1f} · tops {-bal / 2:+.1f}"
    _quadrant(c, 1, labels[1], fmt_db(bal), "dB", bsub, F_BALANCE)
    _quadrant(c, 2, labels[2], fmt_ms(sd), "ms",
              f"≈ {sd * 0.343:.2f} m" if sd is not None else "", F_SUB_DELAY)
    if lc_on:
        _quadrant(c, 3, labels[3], fmt_hz(lc), "Hz", "12 dB/oct · B3 off", F_LOWCUT_FREQ)
    else:
        _quadrant(c, 3, labels[3], "OFF", "", f"{fmt_hz(lc)} Hz · B3 on", F_LOWCUT_FREQ,
                  dim=True)
    _meter(c, status)
    _middle(c, status)


def _slot_band(state: Any, slot: int) -> tuple[float | None, float | None, float | None]:
    base = LIVE_SLOTS[slot]
    return (_num(state, base + ".freq"), _num(state, base + ".gain"), _num(state, base + ".q"))


def _page_live(c: _Ctx, state: Any, status: Any) -> None:
    slot = int(getattr(c.focus, "slot", 0) or 0) % len(LIVE_SLOTS)
    freq, gain, q = _slot_band(state, slot)
    base = LIVE_SLOTS[slot]
    labels = knob_labels("LIVE")
    _quadrant(c, 0, labels[0], LIVE_SLOT_LABELS[slot], "", "", F_SLOT, value_size=40,
              shift=12, box_half=26)
    _slot_pills(c, state, slot)
    _quadrant(c, 1, labels[1], fmt_hz(freq), "Hz", "", base + ".freq")
    _quadrant(c, 2, labels[2], fmt_db(gain), "dB", "", base + ".gain")
    _quadrant(c, 3, labels[3], fmt_q(q), "", "shelf slope" if slot >= 4 else "", base + ".q")
    _meter(c, status)
    _middle(c, status)


def _slot_pills(c: _Ctx, state: Any, sel: int) -> None:
    """Six small boxes under the slot name: filled = band is active (gain != 0)."""
    d = c.d
    x0, x1 = _QX[KNOB_CORNERS[0][1]]
    vpos = KNOB_CORNERS[0][0]
    y = 114 if vpos == "t" else 182          # between the slot name and the middle strip
    n = len(LIVE_SLOTS)
    pw, gap = 28, 4
    sx = x0 + ((x1 - x0) - (n * pw + (n - 1) * gap)) / 2
    for i in range(n):
        _, g, _ = _slot_band(state, i)
        active = g is not None and abs(g) >= 0.05
        bx = sx + i * (pw + gap)
        box = (bx, y, bx + pw, y + 24)
        if active:
            d.rectangle(box, fill=(255, 90, 0) if (g or 0) < 0 else GREEN)
        if i == sel:
            d.rectangle((box[0] - 2, box[1] - 2, box[2] + 2, box[3] + 2), outline=FG, width=2)
        else:
            d.rectangle(box, outline=FAINT)
        d.text(((box[0] + box[2]) / 2, y + 12), LIVE_SLOT_SHORT[i], font=font(13),
               fill=BG if active else DIM, anchor="mm")


def _setup_value(state: Any, item) -> str:
    if item.kind == "page":
        return "PUSH ▸"
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
    sel = max(0, min(len(SETUP_ITEMS) - 1, int(getattr(c.focus, "item", 0) or 0)))
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
        fg = FG if pending else (BG if is_sel else FG)
        d.text((14, y + row_h // 2 - 1), item.label, font=font(20, FONT_REG if not is_sel
                                                               else FONT_BOLD),
               fill=fg, anchor="lm")
        val = "PUSH AGAIN" if pending else _setup_value(state, item)
        vcol = fg if is_sel else (AMBER if item.kind == "rebuild" else FG)
        d.text((W - 14, y + row_h // 2 - 1), val, font=font(20), fill=vcol, anchor="rm")
    # scroll marks
    if first > 0:
        d.polygon([(W // 2 - 8, y0 - 4), (W // 2 + 8, y0 - 4), (W // 2, y0 - 12)], fill=DIM)
    if first + rows < len(SETUP_ITEMS):
        yb = y0 + rows * row_h
        d.polygon([(W // 2 - 8, yb), (W // 2 + 8, yb), (W // 2, yb + 8)], fill=DIM)
    hint_fn = getattr(c.focus, "hint_now", None)
    hint = hint_fn(c.now) if callable(hint_fn) else None
    alert = str(_get(status, "alert", "") or "")
    if hint:
        d.rectangle((0, H - FOOTER_H, W, H), fill=AMBER)
        d.text((W // 2, H - FOOTER_H // 2), hint, font=_fit(d, hint, 20, W - 20), fill=BG,
               anchor="mm")
    elif alert:   # e.g. APPLYING SETUP… on the page that triggers rebuilds
        d.rectangle((0, H - FOOTER_H, W, H), fill=(60, 50, 0))
        d.text((W // 2, H - FOOTER_H // 2), alert, font=_fit(d, alert, 20, W - 20),
               fill=YELLOW, anchor="mm")


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
    hint_fn = getattr(c.focus, "hint_now", None)
    hint = hint_fn(c.now) if callable(hint_fn) else None
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


def _overlay_muted(c: _Ctx, state: Any, status: Any, page: str = "MAIN") -> None:
    """MUTED, impossible to miss but never hiding what is being edited.

    MAIN / LIVE: full-width red band across the middle + red frame (the band only covers the
    middle strip and the bottom sub-lines; values, edit boxes and FINE stay visible).
    SETUP: red frame + red footer strip (a band would cover the selected row and its
    'PUSH AGAIN'); an active hint takes the footer for its 2.5 s. HWTEST: frame + red tag.
    """
    d = c.d
    d.rectangle((0, STATUS_H, W - 1, H - 1), outline=BAND_RED, width=4)
    hint, unmute = _mute_hint(c, state, status)
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
    y0, y1 = MID_Y0 - 10, MID_Y1 + 22   # covers the sub-lines whole, never half a line
    d.rectangle((0, y0, W, y1), fill=BAND_RED)
    cy = (y0 + y1) // 2
    d.text((20, cy), "MUTED", font=font(56), fill=FG, anchor="lm")
    small = hint or unmute
    fx = 20 + int(d.textlength("MUTED", font=font(56))) + 14
    lines = small.split(" · ", 1)
    if len(lines) == 2:
        d.text((W - 16, cy - 13), lines[0], font=_fit(d, lines[0], 22, W - fx - 16), fill=FG,
               anchor="rm")
        d.text((W - 16, cy + 14), lines[1], font=_fit(d, lines[1], 18, W - fx - 16, FONT_REG),
               fill=FG, anchor="rm")
    else:
        d.text((W - 16, cy), small, font=_fit(d, small, 20, W - fx - 16), fill=FG, anchor="rm")


_PAGES = {"MAIN": _page_main, "LIVE": _page_live, "SETUP": _page_setup, "HWTEST": _page_hwtest}


def render(page: str, state: Any, status: Any = None, focus: Any = None,
           now: float | None = None) -> Image.Image:
    """Render `page` (MAIN, LIVE, SETUP, HWTEST) with status-driven overlays -> 480x320 RGB.

    Never raises: unknown page -> MAIN, missing fields -> '--' / '?', no status -> everything
    unknown (amber), and any unexpected error -> a 'UI ERROR' frame that still shows MUTED.
    """
    try:
        return _render(page, state, status, focus, now)
    except Exception as e:   # the screen must keep updating; never take the loop down
        _log_render_error(e)
        return _error_frame(state, status, e)


def _render(page: Any, state: Any, status: Any, focus: Any, now: float | None) -> Image.Image:
    page = page if isinstance(page, str) and page in _PAGES else "MAIN"
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
    """Load fonts and fill the text cache once (call at service start); returns ms spent.

    Why: the first frame costs ~40 ms on the Pi 4 (font files + glyph rasterising), over the
    30 ms budget; every later frame is ~3-5 ms render + ~6 ms RGB565 conversion.
    """
    t0 = time.perf_counter()
    ok = ScreenStatus(device_present=True, engine="Running")
    for page in _PAGES:
        render(page, None, ok, Focus())
        render(page, None, ScreenStatus(device_present=True, engine="Running", muted=True),
               Focus())
    for ov in (ScreenStatus(device_present=False), ScreenStatus(engine=None), ScreenStatus()):
        render("MAIN", None, ov, Focus())
    return (time.perf_counter() - t0) * 1000


# ------------------------------------------------------------------------------------------
# Demo states + snapshots:  python3 -m k1000.ui.pages <outdir>
# ------------------------------------------------------------------------------------------
def demo_cases() -> list[tuple[str, str, Any, Any, Any]]:
    """(name, page, state, status, focus) for every page and overlay, with representative data.

    Used by the snapshot command (docs/ui/*.png) and by the tests, so what is reviewed by eye
    is exactly what is tested.
    """
    from ..state import State  # local import: pages must render without state.py too

    inf = float("inf")
    s = State()
    s.master_db, s.balance_db, s.sub_delay_ms, s.lowcut_hz = -12.5, 3.0, 2.35, 120.0
    s.live_peq[1].freq, s.live_peq[1].gain, s.live_peq[1].q = 2520.0, -6.5, 8.0
    s.live_peq[3].gain = -3.0
    s.live_lowshelf.gain = 2.0
    run = ScreenStatus(profile="m4", device="MOTU M4", device_present=True, engine="Running",
                       load=7.4, levels=(-14.0, -15.5, -9.0, -2.5), clip=(False, False, False, True),
                       ready=True)
    edit = Focus(editing=F_MASTER, edit_until=inf)
    live = Focus(slot=1, editing="live_peq.1.gain", edit_until=inf, held={1})
    setup = Focus(item=3)
    from .input_map import HwTestLog
    hw = HwTestLog(enc_count=[3, -2, 0, 7], sw_down=[False, False, False, False, False, True,
                                                     False, False],
                   sw_last=["short", "", "", "", "", "long", "", ""], last="sw5 press  = B2")
    hwf = Focus(hw=hw)
    lc_off = State.from_dict({**s.to_dict(), "lowcut_on": False, "master_db": -80.0,
                              "balance_db": -12.0, "sub_delay_ms": 50.0})
    muted = State.from_dict({**s.to_dict(), "muted": True})
    dev = ScreenStatus(profile="dev", device="HiFiBerry", device_present=True, engine="Paused",
                       load=0.1, ready=True)
    dev_muted = ScreenStatus(profile="dev", device="HiFiBerry", device_present=True,
                             engine="Running", load=0.1, muted=True, ready=True)
    run_muted = ScreenStatus(**{**run.__dict__, "muted": True})
    return [
        ("main", "MAIN", s, run, Focus()),
        ("main_edit", "MAIN", s, run, edit),
        ("main_extreme", "MAIN", lc_off, dev, Focus()),
        ("live", "LIVE", s, run, live),
        ("live_slot", "LIVE", s, run, Focus(slot=4, editing=F_SLOT, edit_until=inf)),
        ("setup", "SETUP", s, dev, setup),
        ("setup_confirm", "SETUP", s, dev, Focus(item=10, confirm="mono_subs", confirm_until=inf,
                                                 hint="PUSH AGAIN TO APPLY (MAY CLICK)",
                                                 hint_until=inf)),
        ("setup_muted", "SETUP", muted, dev_muted, Focus(item=10, confirm="mono_subs",
                                                         confirm_until=inf)),
        ("hwtest", "HWTEST", s, run, hwf),
        ("hwtest_muted", "HWTEST", muted, run_muted, hwf),
        ("muted", "MAIN", muted, run, Focus()),
        ("muted_fine", "LIVE", muted, run_muted, Focus(slot=1, editing="live_peq.1.q",
                                                       edit_until=inf, held={3})),
        ("unknown_status", "MAIN", s, ScreenStatus(profile="m4", device="MOTU M4"), Focus()),
        ("no_device", "MAIN", s, ScreenStatus(profile="m4", device="MOTU M4",
                                              device_present=False, engine=None), Focus()),
        ("engine_down", "MAIN", s, ScreenStatus(profile="m4", device="MOTU M4", engine=None),
         Focus()),
        ("hint", "MAIN", s, run, Focus(hint="HOLD B2 TO MUTE / UNMUTE", hint_until=inf)),
        # K1000 holds Main before its own boot ramp: not 'READY · hold B2 to unmute'
        ("muted_ramp_hold", "MAIN", s, ScreenStatus(**{**run.__dict__, "engine": "Starting",
                                                       "muted": True, "ramping": True,
                                                       "ready": False, "levels": (-100.0,) * 4,
                                                       "clip": (False,) * 4}), Focus()),
        ("setup_applying", "SETUP", s, ScreenStatus(**{**dev.__dict__, "engine": "Running",
                                                       "alert": "APPLYING SETUP…",
                                                       "message": "APPLYING SETUP…"}),
         Focus(item=10)),
        ("knobs_offline", "LIVE", muted, ScreenStatus(**{**run_muted.__dict__,
                                                         "alert": "KNOBS OFFLINE · USE PHONE MUTE / BYPASS",
                                                         "message": "KNOBS OFFLINE · USE PHONE MUTE / BYPASS"}),
         Focus(slot=1)),
        ("input_clip", "LIVE", s, ScreenStatus(**{**run.__dict__, "in_clip": True}), live),
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


if __name__ == "__main__":
    import sys
    for n, ms in snapshot(sys.argv[1] if len(sys.argv) > 1 else "."):
        print(f"{n:14s} {ms:6.1f} ms")
