"""Mockups of the v1 live UI (docs/07-live-ui-v1.md), drawn with pages.py's own helpers.

    python3 -m k1000.ui.mock_v1 docs/ui/v1        # the PNGs the spec shows
    python3 -m k1000.ui.mock_v1 --bench [N]       # render cost per frame (run it on the box)

Nothing here is wired to the controller. `render_v1()` is a prototype of the pages the build will
add to pages.py: it reuses the status bar, fonts, colours, quadrants, meter and MUTED overlay of
pages.py unchanged (imported, never modified), and eqgraph.draw_graph for the graph. Geometry
constants are at the top so the build can lift them as they are.
"""
from __future__ import annotations

import math
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np

from . import eqgraph as eg
from . import pages as pg
from .input_map import F_BALANCE, F_LOWCUT_FREQ, F_MASTER, F_SUB_DELAY, KNOB_CORNERS, Focus

W, H = pg.W, pg.H

# ------------------------------------------------------------------------------------------
# SHOW geometry (480 x 320)
# ------------------------------------------------------------------------------------------
TOP_Y0, TOP_Y1 = pg.STATUS_H, 96          # E1 / E3 blocks (label row + value box)
CHIP_Y0, CHIP_Y1 = 98, 122                # band chips SUB LS 1 2 3 4 HS
GRAPH = (0, 124, W, 254)                  # x0, y0, x1, y1 (exclusive)
BOT_Y0, BOT_Y1 = 254, H                   # E2 / E4 blocks
METER_BOX = (200, 31, 280, 96)            # compact 4-bar meter, top centre
STRIP = (CHIP_Y0, CHIP_Y1)                # hints / messages take the chip row (the marker and the
                                          # GAIN label still say which band is selected)
MUTED_BAND = (CHIP_Y0, GRAPH[1] + 36)     # MUTED band: chip row + graph top, 0 dB stays visible

# The band chips, left to right = low to high, and what each selects. None = SUB (balance).
CHIPS: tuple[str, ...] = ("SUB", "LS", "1", "2", "3", "4", "HS")
CHIP_SLOT: tuple[int | None, ...] = (None, 4, 0, 1, 2, 3, 5)
SLOT_NAME: tuple[str, ...] = ("PEQ 1", "PEQ 2", "PEQ 3", "PEQ 4", "LO SHELF", "HI SHELF")

# v1 SETUP list (v0's, plus RTA source; Top delay stays here, ALIGN is v0 MAIN)
SETUP_V1: tuple[tuple[str, str, str], ...] = (
    ("Input trim", "input_trim_db", "num"), ("Top delay", "top_delay_ms", "num"),
    ("Sub polarity", "sub_inverted", "bool"), ("Top polarity", "top_inverted", "bool"),
    ("Sub low-pass", "sub_lowpass_on", "bool"), ("Sub LP freq", "sub_lowpass_hz", "num"),
    ("Trim Sub L", "out_trim_db.0", "num"), ("Trim Sub R", "out_trim_db.1", "num"),
    ("Trim Top L", "out_trim_db.2", "num"), ("Trim Top R", "out_trim_db.3", "num"),
    ("RTA source", "rta_source", "choice"), ("Mono subs", "mono_subs", "rebuild"),
    ("Audition (dev)", "dev_audition", "rebuild"), ("Profile", "profile", "rebuild"),
    ("Knob test", "HWTEST", "page"),
)


@dataclass
class V1Focus(Focus):
    """v0 Focus + the SHOW page's chip (index into CHIPS) and the RTA source shown."""
    chip: int = 3
    rta_source: str = "PGM"
    eq_in: bool = True


# ------------------------------------------------------------------------------------------
# Small drawing pieces (pages.py look)
# ------------------------------------------------------------------------------------------
def _octaves(q: float) -> float:
    return 2.0 / math.log(2.0) * math.asinh(1.0 / (2.0 * q))


def fmt_oct(q: float | None) -> str:
    if q is None or q <= 0:
        return ""
    o = _octaves(q)
    n = 1.0 / o
    return f"1/{n:.0f} oct" if n >= 1.5 else f"{o:.1f} oct"


def _block(c: Any, e: int, label: str, value: str, unit: str, field_key: str | None, *,
           size: int = 44, dim: bool = False, tag_extra: str = "") -> None:
    """One encoder's block on SHOW: 'E1 MASTER' on the outer edge, the value in a box next to
    the knob (amber when it is being edited, FINE while its push is held)."""
    corner = KNOB_CORNERS[e]
    top = corner[0] == "t"
    left = corner[1] == "l"
    if top:
        x0, x1 = (0, METER_BOX[0] - 4) if left else (METER_BOX[2] + 4, W)
        label_y, box = TOP_Y0 + 3, (0, 51, 0, 95)
    else:
        x0, x1 = (0, W // 2 - 2) if left else (W // 2 + 2, W)
        label_y, box = H - 23, (0, BOT_Y0 + 1, 0, H - 26)
    d = c.d
    pad = 10
    ftag, flab = pg.font(16), pg.font(20)
    lab_col = pg.DIM if dim else pg.AMBER
    if left:
        d.text((x0 + pad, label_y), f"E{e + 1}", font=ftag, fill=pg.CYAN, anchor="la")
        d.text((x0 + pad + 30, label_y - 2), label, font=flab, fill=lab_col, anchor="la")
        if tag_extra:
            lx = x0 + pad + 30 + d.textlength(label, font=flab) + 8
            d.text((lx, label_y), tag_extra, font=pg.font(16), fill=pg.FG, anchor="la")
    else:
        d.text((x1 - pad, label_y), f"E{e + 1}", font=ftag, fill=pg.CYAN, anchor="ra")
        d.text((x1 - pad - 30, label_y - 2), label, font=flab, fill=lab_col, anchor="ra")
        if tag_extra:
            rx = x1 - pad - 30 - d.textlength(label, font=flab) - 8
            d.text((rx, label_y), tag_extra, font=pg.font(16), fill=pg.FG, anchor="ra")
    by0, by1 = box[1], box[3]
    cy = (by0 + by1) // 2
    edited = field_key is not None and c.editing == field_key
    fine = e in c.held
    if edited:
        d.rounded_rectangle((x0 + 4, by0, x1 - 4, by1), radius=8, fill=pg.AMBER)
    fg = pg.BG if edited else (pg.DIM if dim else pg.FG)
    funit = pg.font(18)
    unit_w = int(d.textlength(unit, font=funit)) + 6 if unit else 0
    fine_w = int(d.textlength("FINE", font=pg.font(14))) + 10 if fine else 0
    room = (x1 - x0) - fine_w
    fval = pg._fit(d, value, size, room - 16 - unit_w)
    vw = d.textlength(value, font=fval)
    vx = x0 + (room - (vw + unit_w)) / 2
    d.text((vx, cy), value, font=fval, fill=fg, anchor="lm")
    if unit:
        d.text((vx + vw + 6, cy + 13), unit, font=funit, fill=pg.BG if edited else pg.DIM, anchor="ls")
    if fine:
        fy, fa = ((by0 + 4, "ra") if top else (by1 - 4, "rd"))
        d.text((x1 - 10, fy), "FINE", font=pg.font(14), fill=pg.BG if edited else pg.CYAN, anchor=fa)


def _mini_meter(c: Any, status: Any) -> None:
    """4 bars (Sub L R, Top L R) -60..0 dBFS with clip boxes, in METER_BOX (top centre)."""
    d = c.d
    x0, y0, x1, y1 = METER_BOX
    levels = pg._seq(pg._get(status, "levels", None))
    clips = pg._seq(pg._get(status, "clip", None))
    bw, gap, pair = 12, 3, 9
    total = 4 * bw + 2 * gap + pair
    bx = (x0 + x1) / 2 - total / 2
    top, bot = y0 + 9, y1 - 13

    def y_of(db: float) -> float:
        db = max(pg.METER_FLOOR_DB, min(0.0, db))
        return bot - (bot - top) * (db - pg.METER_FLOOR_DB) / -pg.METER_FLOOR_DB

    for i in range(4):
        xi = bx + i * (bw + gap) + (pair - gap if i >= 2 else 0)
        try:
            lv = float(levels[i]) if i < len(levels) else pg.METER_FLOOR_DB
        except (TypeError, ValueError):
            lv = pg.METER_FLOOR_DB
        if not math.isfinite(lv):
            lv = pg.METER_FLOOR_DB
        d.rectangle((xi, top, xi + bw - 1, bot), fill=pg.BAR_BG)
        for lo, hi, col in ((pg.METER_FLOOR_DB, -18.0, pg.GREEN), (-18.0, -6.0, pg.YELLOW), (-6.0, 0.0, pg.RED)):
            if lv <= lo:
                break
            d.rectangle((xi, y_of(min(lv, hi)), xi + bw - 1, y_of(lo)), fill=col)
        clipped = bool(pg._truth(clips[i])) if i < len(clips) else False
        if clipped:
            d.rectangle((xi - 1, y0, xi + bw, y0 + 6), fill=pg.RED)
        else:
            d.rectangle((xi, y0, xi + bw - 1, y0 + 6), outline=pg.FAINT)
    fl = pg.font(11)
    d.text((bx + bw + gap / 2, bot + 2), "SUB", font=fl, fill=pg.DIM, anchor="ma")
    d.text((bx + 3 * bw + 2 * gap + pair - gap / 2 + 0.5 * gap, bot + 2), "TOP", font=fl, fill=pg.DIM, anchor="ma")


def _chips(c: Any, state: Any, sel: int, bands: Sequence[eg.Band], *, with_sub: bool = True) -> None:
    """SUB · LS · 1 · 2 · 3 · 4 · HS: selected = amber, a band in use = its cut/boost colour."""
    d = c.d
    names = CHIPS if with_sub else CHIPS[1:]
    slots = CHIP_SLOT if with_sub else CHIP_SLOT[1:]
    x, gap = 4, 4
    widths = [92] + [58] * 6 if with_sub else [74] * 6
    widths[-1] = (W - 4) - x - sum(widths[:-1]) - gap * (len(widths) - 1)
    for i, name in enumerate(names):
        w = widths[i]
        box = (x, CHIP_Y0, x + w, CHIP_Y1)
        slot = slots[i]
        is_sel = (i == sel)
        col, txt = pg.FAINT, pg.DIM
        g = None
        if slot is not None and slot < len(bands):
            g = bands[slot].gain
        if slot is None:
            bal = pg._num(state, F_BALANCE, 0.0) or 0.0
            label = f"SUB {pg.fmt_db(bal)}"
            active = abs(bal) >= 0.05
            acol = pg.CYAN
        else:
            label = name
            active = g is not None and abs(g) >= 0.05
            acol = (255, 90, 0) if (g or 0) < 0 else pg.GREEN
        if is_sel:
            d.rounded_rectangle(box, radius=4, fill=pg.AMBER)
            d.text(((box[0] + box[2]) / 2, (box[1] + box[3]) / 2), label,
                   font=pg._fit(d, label, 20, w - 8), fill=pg.BG, anchor="mm")
        else:
            d.rounded_rectangle(box, radius=4, outline=acol if active else col, width=2 if active else 1)
            d.text(((box[0] + box[2]) / 2, (box[1] + box[3]) / 2), label,
                   font=pg._fit(d, label, 20, w - 8), fill=acol if active else txt, anchor="mm")
        x += w + gap


def _strip(c: Any, text: str, hint: bool) -> None:
    """Hint (amber) or controller message (dark amber, yellow text), over the chip row."""
    d = c.d
    y0, y1 = STRIP
    d.rectangle((0, y0, W, y1), fill=pg.AMBER if hint else (60, 50, 0))
    d.text((W // 2, (y0 + y1) // 2 + 1), text, font=pg._fit(d, text, 20, W - 16),
           fill=pg.BG if hint else pg.YELLOW, anchor="mm")


def _muted_band(c: Any, state: Any, status: Any) -> None:
    """The v0 MUTED band, moved to the chip row + graph top on SHOW (values, the edit box,
    FINE and the 0 dB line stay visible)."""
    d = c.d
    d.rectangle((0, pg.STATUS_H, W - 1, H - 1), outline=pg.BAND_RED, width=4)
    hint, unmute = pg._mute_hint(c, state, status)
    y0, y1 = MUTED_BAND
    d.rectangle((0, y0, W, y1), fill=pg.BAND_RED)
    cy = (y0 + y1) // 2
    d.text((16, cy), "MUTED", font=pg.font(44), fill=pg.FG, anchor="lm")
    small = hint or unmute
    fx = 16 + int(d.textlength("MUTED", font=pg.font(44))) + 14
    parts = small.split(" · ", 1)
    if len(parts) == 2:
        d.text((W - 14, cy - 12), parts[0], font=pg._fit(d, parts[0], 22, W - fx - 14), fill=pg.FG, anchor="rm")
        d.text((W - 14, cy + 13), parts[1], font=pg._fit(d, parts[1], 18, W - fx - 14, pg.FONT_REG),
               fill=pg.FG, anchor="rm")
    else:
        d.text((W - 14, cy), small, font=pg._fit(d, small, 20, W - fx - 14), fill=pg.FG, anchor="rm")


def _peak_label(c: Any, geo: dict[str, Any], rta: eg.RtaFrame | None) -> None:
    if rta is None or not rta.peak_hz or geo.get("peak_x") is None:
        return
    d = c.d
    x = geo["peak_x"]
    t = f"PEAK {pg.fmt_hz(rta.peak_hz)} · B4 GRAB"
    f = pg.font(19)
    tw = d.textlength(t, font=f)
    left = x + 10 + tw <= W - 84          # keep clear of the RTA tag (top-right)
    tx = x + 10 if left else x - 10
    y = GRAPH[1] + 14
    d.rectangle((tx - 3 if left else tx - tw - 3, y - 12, tx + tw + 3 if left else tx + 3, y + 11), fill=pg.BG)
    d.text((tx, y), t, font=f, fill=pg.YELLOW, anchor="lm" if left else "rm")


def _rta_tag(c: Any, rta: eg.RtaFrame | None, source: str) -> None:
    """Which signal the RTA shows, at the graph's top-right corner (the +9 dB zone, boosts
    stop at +6)."""
    d = c.d
    t = "RTA OFF" if rta is None else f"RTA {source}"
    d.text((W - 6, GRAPH[1] + 4), t, font=pg.font(14), fill=pg.CYAN if rta is not None else pg.DIM,
           anchor="ra")


# ------------------------------------------------------------------------------------------
# Pages
# ------------------------------------------------------------------------------------------
def page_show(c: Any, state: Any, status: Any, rta: eg.RtaFrame | None, *, with_sub: bool = True) -> None:
    f = c.focus
    chip = int(getattr(f, "chip", 3) or 0)
    chip = max(0, min(len(CHIPS) - 1, chip)) if with_sub else max(1, min(len(CHIPS) - 1, chip))
    slot = CHIP_SLOT[chip]
    bands = eg.live_bands(state)
    eq_in = bool(getattr(f, "eq_in", True))
    # graph first, so the blocks and strips draw over its edges
    geo = eg.draw_graph(c.img, GRAPH, bands, slot, rta, eq_in=eq_in)
    _peak_label(c, geo, rta)
    _rta_tag(c, rta, str(getattr(f, "rta_source", "PGM")))
    _chips(c, state, chip if with_sub else chip - 1, bands, with_sub=with_sub)
    master = pg._num(state, F_MASTER)
    _block(c, 0, "MASTER", pg.fmt_db(master), "dB", F_MASTER, size=44)
    _mini_meter(c, status)
    if slot is None:      # SUB selected: E3 = sub level (the balance), E2/E4 idle
        bal = pg._num(state, F_BALANCE)
        _block(c, 2, "SUB LEVEL", pg.fmt_db(bal), "dB", F_BALANCE, size=44)
        _block(c, 1, "FREQ", "—", "", None, size=40, dim=True)
        _block(c, 3, "Q", "—", "", None, size=40, dim=True)
    else:
        b = bands[slot]
        base = ("live_peq.%d" % slot) if slot < 4 else ("live_lowshelf" if slot == 4 else "live_highshelf")
        name = SLOT_NAME[slot]
        _block(c, 2, "GAIN", pg.fmt_db(b.gain), "dB", base + ".gain", size=44, tag_extra=name)
        _block(c, 1, "FREQ", pg.fmt_hz(b.freq), "Hz", base + ".freq", size=40)
        if slot < 4:
            _block(c, 3, "Q", pg.fmt_q(b.q), fmt_oct(b.q), base + ".q", size=40)
        else:
            _block(c, 3, "SLOPE", pg.fmt_q(b.q), "Q", base + ".q", size=40)
    hint_fn = getattr(f, "hint_now", None)
    hint = hint_fn(c.now) if callable(hint_fn) else None
    msg = str(pg._get(status, "message", "") or "")
    if not eq_in and not hint:
        _strip(c, "LIVE EQ OUT (concept) · notches are NOT active", hint=True)
    elif hint or msg or pg._unknown_notice(status):
        _strip(c, hint or msg or pg._unknown_notice(status), hint=bool(hint))


def page_align(c: Any, state: Any, status: Any) -> None:
    """ALIGN = v0 MAIN, second in the cycle: master stays on E1 (as on SHOW); B3 is a HOLD now."""
    master = pg._num(state, F_MASTER)
    bal = pg._num(state, F_BALANCE)
    sd = pg._num(state, F_SUB_DELAY)
    lc_on = bool(pg._get(state, "lowcut_on", True))
    lc = pg._num(state, F_LOWCUT_FREQ)
    pg._quadrant(c, 0, "MASTER", pg.fmt_db(master), "dB", "", F_MASTER)
    bsub = "subs = tops" if bal is None or abs(bal) < 0.05 else f"subs {bal / 2:+.1f} · tops {-bal / 2:+.1f}"
    pg._quadrant(c, 1, "BALANCE", pg.fmt_db(bal), "dB", bsub, F_BALANCE)
    sub3 = f"≈ {sd * 0.343:.2f} m" if sd is not None else ""
    pg._quadrant(c, 2, "SUB DELAY", pg.fmt_ms(sd), "ms", sub3, F_SUB_DELAY)
    if lc_on:
        pg._quadrant(c, 3, "LO-CUT", pg.fmt_hz(lc), "Hz", "12 dB/oct · hold B3 off", F_LOWCUT_FREQ)
    else:
        pg._quadrant(c, 3, "LO-CUT", "OFF", "", f"{pg.fmt_hz(lc)} Hz · hold B3 on", F_LOWCUT_FREQ, dim=True)
    pg._meter(c, status)
    pg._middle(c, status)


def _setup_value(state: Any, fld: str, kind: str, focus: Any) -> str:
    if kind == "page":
        return "PUSH ▸"
    if fld == "rta_source":
        return str(getattr(focus, "rta_source", "PGM"))
    item = type("I", (), {"field": fld, "kind": kind})()
    return pg._setup_value(state, item)


def page_setup(c: Any, state: Any, status: Any) -> None:
    """v0 SETUP look, v1 list."""
    d = c.d
    for e, lab in ((0, "ITEM"), (1, "VALUE")):
        corner = KNOB_CORNERS[e]
        y = pg.STATUS_H + 4 if corner[0] == "t" else H - 24
        d.text((10, y), f"E{e + 1}", font=pg.font(16), fill=pg.CYAN, anchor="la")
        d.text((40, y - 2), lab, font=pg.font(20), fill=pg.AMBER, anchor="la")
    d.text((W - 10, H - 22), "push = select", font=pg.font(16, pg.FONT_REG), fill=pg.DIM, anchor="ra")
    sel = max(0, min(len(SETUP_V1) - 1, int(getattr(c.focus, "item", 0) or 0)))
    d.text((W - 10, pg.STATUS_H + 4), f"{sel + 1}/{len(SETUP_V1)}", font=pg.font(18), fill=pg.DIM, anchor="ra")
    rows, row_h, y0 = 8, 28, pg.STATUS_H + 30
    first = max(0, min(sel - rows // 2, len(SETUP_V1) - rows))
    for r in range(rows):
        i = first + r
        if i >= len(SETUP_V1):
            break
        label, fld, kind = SETUP_V1[i]
        y = y0 + r * row_h
        is_sel = i == sel
        if is_sel:
            d.rectangle((4, y, W - 4, y + row_h - 3), fill=pg.AMBER)
        fg = pg.BG if is_sel else pg.FG
        d.text((14, y + row_h // 2 - 1), label, font=pg.font(20, pg.FONT_BOLD if is_sel else pg.FONT_REG),
               fill=fg, anchor="lm")
        val = _setup_value(state, fld, kind, c.focus)
        vcol = fg if is_sel else (pg.AMBER if kind == "rebuild" else (pg.CYAN if fld == "rta_source" else pg.FG))
        d.text((W - 14, y + row_h // 2 - 1), val, font=pg.font(20), fill=vcol, anchor="rm")
    if first > 0:
        d.polygon([(W // 2 - 8, y0 - 4), (W // 2 + 8, y0 - 4), (W // 2, y0 - 12)], fill=pg.DIM)
    if first + rows < len(SETUP_V1):
        yb = y0 + rows * row_h
        d.polygon([(W // 2 - 8, yb), (W // 2 + 8, yb), (W // 2, yb + 8)], fill=pg.DIM)
    hint_fn = getattr(c.focus, "hint_now", None)
    hint = hint_fn(c.now) if callable(hint_fn) else None
    if hint:
        d.rectangle((0, H - pg.FOOTER_H, W, H), fill=pg.AMBER)
        d.text((W // 2, H - pg.FOOTER_H // 2), hint, font=pg._fit(d, hint, 20, W - 20), fill=pg.BG, anchor="mm")


def page_geq_concept(c: Any, state: Any, status: Any, rta: eg.RtaFrame | None, gains: Sequence[float],
                     cursor: int) -> None:
    """NOT PROPOSED for Sunday: what a 31-band master GEQ would look like on this panel."""
    d = c.d
    x0, y0, x1, y1 = GRAPH[0], CHIP_Y0, GRAPH[2], GRAPH[3]
    geq = eg.geq_bands(gains)
    eg.draw_graph(c.img, (x0, y0, x1, y1), geq, None, rta, labels=(), order=())
    fa = eg.LogAxis(x0, x1 - 1)
    da = eg.DbAxis(y0, y1 - 1, eg.STYLE.top_db, eg.STYLE.bottom_db)
    lo, hi = math.log2(eg.F_LO), math.log2(eg.F_HI)
    bw = (hi - lo) / len(gains)
    for i, g in enumerate(gains):
        fc = 2.0 ** (lo + (i + 0.5) * bw)
        xc = fa.x(fc)
        half = (fa.x(2.0 ** (lo + (i + 1) * bw)) - fa.x(2.0 ** (lo + i * bw))) / 2 - 2
        yz, yg = da.y(0.0), da.y(g)
        col = pg.AMBER if i == cursor else ((255, 90, 0) if g < 0 else (pg.GREEN if g > 0 else pg.FAINT))
        if abs(g) >= 0.05:
            d.rectangle((xc - half, min(yz, yg), xc + half, max(yz, yg)), fill=col)
        d.rectangle((xc - half, yg - 2, xc + half, yg + 2), fill=col if i == cursor else pg.DIM)
    fc = 2.0 ** (lo + (cursor + 0.5) * bw)
    _block(c, 0, "MASTER", pg.fmt_db(pg._num(state, F_MASTER)), "dB", None, size=44)
    _mini_meter(c, status)
    _block(c, 2, "GAIN", pg.fmt_db(gains[cursor]), "dB", "geq.gain", size=44, tag_extra=f"{cursor + 1}/31")
    _block(c, 1, "BAND", pg.fmt_hz(fc), "Hz", None, size=40)
    _block(c, 3, "Q", "4.32", "1/3 oct", None, size=40, dim=True)
    _strip(c, "CONCEPT · master GEQ · not proposed for Sunday", hint=False)


def render_v1(page: str, state: Any, status: Any = None, focus: Any = None, *,
              rta: eg.RtaFrame | None = None, with_sub: bool = True, now: float | None = None,
              geq: tuple[Sequence[float], int] | None = None) -> Any:
    """Prototype of the v1 pages: SHOW, ALIGN, SETUP (+ GEQ concept), v0 overlays and status bar."""
    focus = V1Focus() if focus is None else focus
    now = time.monotonic() if now is None else now
    c = pg._Ctx(focus, now)
    ov = pg.overlays_for(state, status)
    shown = {"GEQ": "SHOW"}.get(page, page)
    pg._status_bar(c, shown, state, status, "MUTED" in ov)
    if "NO DEVICE" in ov:
        pg._overlay_no_device(c, state, status)
    elif "ENGINE DOWN" in ov:
        pg._overlay_engine_down(c, state, status)
    else:
        if page == "SHOW":
            page_show(c, state, status, rta, with_sub=with_sub)
            if "MUTED" in ov:
                _muted_band(c, state, status)
        elif page == "GEQ" and geq is not None:
            page_geq_concept(c, state, status, rta, geq[0], geq[1])
        elif page == "ALIGN":
            page_align(c, state, status)
            if "MUTED" in ov:
                pg._overlay_muted(c, state, status, "MAIN")
        else:
            page_setup(c, state, status)
            if "MUTED" in ov:
                pg._overlay_muted(c, state, status, "SETUP")
    return c.img


# ------------------------------------------------------------------------------------------
# Demo data
# ------------------------------------------------------------------------------------------
def synth_rta(ring_hz: float | None = 2470.0, seconds: float = 3.0, fps: float = 10.0,
              seed: int = 7, level_db: float = -36.0) -> eg.Rta:
    """An Rta fed with GetSpectrum-shaped frames (1/24 octave, 10 Hz-20 kHz, peak-of-band
    dBFS) of a music-like spectrum, plus an optional narrow ring that persists."""
    rng = np.random.default_rng(seed)
    n = 265
    f = 10.0 * (2000.0 ** (np.arange(n) / (n - 1)))
    lf = np.log2(f / 1000.0)
    base = level_db - 3.0 * lf - 4.0 * np.maximum(0, -lf - 3.2) ** 2 + 5.0 * np.exp(-((np.log2(f / 70.0)) ** 2) / 0.5)
    base -= 10.0 * np.maximum(0, np.log2(f / 12000.0)) ** 2
    r = eg.Rta()
    t = 0.0
    for k in range(int(seconds * fps)):
        m = base + rng.normal(0, 2.0, n) + 3.0 * np.sin(k / 3.0 + lf * 2)
        if ring_hz:
            ring_db = float(np.interp(math.log2(ring_hz / 1000.0), lf, base)) + 16.0
            m = np.maximum(m, ring_db - 240.0 * np.abs(np.log2(f / ring_hz)))
        r.update(f, m, t)
        t += 1.0 / fps
    return r


def demo_state():
    from ..state import State
    s = State()
    s.master_db, s.balance_db, s.sub_delay_ms, s.lowcut_hz = -12.5, 3.0, 2.35, 120.0
    s.live_peq[1].freq, s.live_peq[1].gain, s.live_peq[1].q = 2520.0, -6.5, 8.0
    s.live_peq[3].freq, s.live_peq[3].gain, s.live_peq[3].q = 6300.0, -3.0, 4.0
    s.live_lowshelf.gain = 2.0
    return s


def demo_cases() -> list[tuple[str, str, Any, Any, Any, dict[str, Any]]]:
    from ..state import State
    inf = float("inf")
    s = demo_state()
    run = pg.ScreenStatus(profile="m4", device="MOTU M4", device_present=True, engine="Running",
                          load=2.0, levels=(-14.0, -15.5, -9.0, -12.5), clip=(False,) * 4, ready=True)
    run_clip = pg.ScreenStatus(**{**run.__dict__, "levels": (-14.0, -15.5, -9.0, -2.5),
                                  "clip": (False, False, False, True)})
    rta = synth_rta().frame("PGM")
    rta_noring = synth_rta(ring_hz=None).frame("PGM")
    muted = State.from_dict({**s.to_dict(), "muted": True})
    run_muted = pg.ScreenStatus(**{**run.__dict__, "muted": True})
    grab = State.from_dict(s.to_dict())
    grab.live_peq[2].freq, grab.live_peq[2].q = round(rta.peak_hz or 2470.0, 1), round(eg.NOTCH_Q, 2)
    geq = [0.0] * 31
    for i, g in ((3, 2.0), (4, 1.5), (19, -4.0), (20, -6.0), (25, -2.0)):
        geq[i] = g
    return [
        ("show", "SHOW", s, run, V1Focus(chip=3), {"rta": rta}),
        ("show_edit_fine", "SHOW", s, run_clip, V1Focus(chip=3, editing="live_peq.1.freq",
                                                        edit_until=inf, held={1}), {"rta": rta}),
        ("show_grab", "SHOW", grab, run, V1Focus(chip=4, editing="live_peq.2.freq", edit_until=inf,
                                                 hint="GRAB: P3 → 2.44k · E3 TO CUT", hint_until=inf),
         {"rta": rta}),
        ("show_shelf", "SHOW", s, run, V1Focus(chip=1, editing="live_lowshelf.gain", edit_until=inf),
         {"rta": rta_noring}),
        ("show_sub", "SHOW", s, run, V1Focus(chip=0, editing=F_BALANCE, edit_until=inf), {"rta": rta_noring}),
        ("show_no_sub_chip", "SHOW", s, run, V1Focus(chip=3), {"rta": rta, "with_sub": False}),
        ("show_muted_alert", "SHOW", muted, pg.ScreenStatus(**{**run_muted.__dict__,
                                                             "alert": "NO AUDIO FLOWING · HELD MUTED",
                                                             "levels": (-100.0,) * 4}),
         V1Focus(chip=3), {"rta": None}),
        ("show_muted", "SHOW", muted, run_muted, V1Focus(chip=3), {"rta": rta}),
        ("show_alert", "SHOW", s, pg.ScreenStatus(**{**run.__dict__,
                                                     "message": "PIPELINE CHANGED OUTSIDE K1000"}),
         V1Focus(chip=3), {"rta": rta}),
        ("show_knobs_offline", "SHOW", s, pg.ScreenStatus(**{**run.__dict__,
                                                             "alert": "KNOBS OFFLINE · USE PHONE MUTE / BYPASS",
                                                             "message": "KNOBS OFFLINE · USE PHONE MUTE / BYPASS"}),
         V1Focus(chip=3), {"rta": rta}),
        ("show_eq_out", "SHOW", s, run, V1Focus(chip=3, eq_in=False), {"rta": rta}),
        ("show_flat_rta_off", "SHOW", State(), run, V1Focus(chip=2), {"rta": None}),
        ("align", "ALIGN", s, run, V1Focus(), {}),
        ("align_lowcut_off", "ALIGN", State.from_dict({**s.to_dict(), "lowcut_on": False}), run,
         V1Focus(hint="LO-CUT OFF · TOPS FULL RANGE", hint_until=inf), {}),
        ("align_muted", "ALIGN", muted, run_muted, V1Focus(editing=F_SUB_DELAY, edit_until=inf), {}),
        ("setup", "SETUP", s, run, V1Focus(item=10), {}),
        ("geq_concept", "GEQ", s, run, V1Focus(), {"rta": rta_noring, "geq": (geq, 20)}),
        ("no_device", "SHOW", s, pg.ScreenStatus(profile="m4", device="MOTU M4", device_present=False,
                                                 engine=None), V1Focus(), {}),
    ]


def snapshot(outdir: str) -> list[tuple[str, float]]:
    os.makedirs(outdir, exist_ok=True)
    out = []
    for name, page, st, stat, foc, kw in demo_cases():
        t0 = time.perf_counter()
        img = render_v1(page, st, stat, foc, **kw)
        out.append((name, (time.perf_counter() - t0) * 1000))
        img.save(os.path.join(outdir, f"{name}.png"))
    return out


def bench(n: int = 200) -> None:
    """Per-frame cost of the SHOW page as the build would run it at 10 fps: a fresh RTA frame
    every time (Rta.update + render + RGB565), after a warm-up. Prints ms percentiles."""
    from .display import to_rgb565
    s = demo_state()
    run = pg.ScreenStatus(profile="m4", device="MOTU M4", device_present=True, engine="Running",
                          load=2.0, levels=(-14.0, -15.5, -9.0, -12.5), clip=(False,) * 4, ready=True)
    r = synth_rta(seconds=1.0)
    rng = np.random.default_rng(1)
    f = r.freqs
    assert f is not None
    base = np.array(r.level)
    foc = V1Focus(chip=3)
    t = 10.0
    for _ in range(10):   # warm-up: fonts, glyph cache, grid masks
        render_v1("SHOW", s, run, foc, rta=r.frame())
    upd, ren, conv, gr = [], [], [], []
    img = None
    for _ in range(n):
        m = base + rng.normal(0, 2.0, len(base))
        t0 = time.perf_counter()
        r.update(f, m, t)
        t1 = time.perf_counter()
        img = render_v1("SHOW", s, run, foc, rta=r.frame())
        t2 = time.perf_counter()
        to_rgb565(img)
        t3 = time.perf_counter()
        from PIL import Image
        g = Image.new("RGB", (W, H))
        eg.draw_graph(g, GRAPH, eg.live_bands(s), 1, r.frame())
        t4 = time.perf_counter()
        upd.append((t1 - t0) * 1000)
        ren.append((t2 - t1) * 1000)
        conv.append((t3 - t2) * 1000)
        gr.append((t4 - t3) * 1000)
        t += 0.1

    def p(v: list[float]) -> str:
        v = sorted(v)
        return f"med {v[len(v) // 2]:6.2f}  p95 {v[int(len(v) * 0.95)]:6.2f}  max {v[-1]:6.2f} ms"

    print(f"Rta.update          {p(upd)}")
    print(f"SHOW render (all)   {p(ren)}")
    print(f"  of which graph    {p(gr)}")
    print(f"RGB565 convert      {p(conv)}")
    tot = [a + b + c for a, b, c in zip(upd, ren, conv)]
    print(f"total per frame     {p(tot)}  -> at 10 fps {sorted(tot)[len(tot) // 2] * 10 / 10:.1f} % of one core")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--bench":
        bench(int(args[1]) if len(args) > 1 else 200)
    else:
        for name, ms in snapshot(args[0] if args else "."):
            print(f"{name:20s} {ms:6.1f} ms")
