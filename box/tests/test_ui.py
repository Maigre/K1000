# SPDX-License-Identifier: GPL-3.0-or-later
"""UI tests: RGB565 output, sysfs handling (faked), page rendering, knob/button semantics.

No hardware needed: the framebuffer is a temp file, sysfs is a temp tree, GPIO levels are a
fake reader. The render-time test runs everywhere but only enforces the 30 ms budget on the Pi.
"""
from __future__ import annotations

import logging
import math
import os
import platform
import statistics
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from k1000 import state as st
from k1000.ui import display, input_map as im, pages
from k1000.ui.input_map import (ControlMap, Focus, GotoPage, Mute, NextPage, Nudge, ResetLive,
                                SelectItem, SelectSlot, ShowHint, Toggle, apply_focus, map_event)


# ------------------------------------------------------------------------------ helpers
@dataclass
class Enc:
    index: int
    delta: int
    ts: float = 0.0


@dataclass
class Sw:
    index: int
    kind: str
    ts: float = 0.0


def setup_sysfs(tmp_path: Path, bl_power: str = "0") -> Path:
    root = tmp_path / "sys"
    for n, name, bind in (("vtcon0", "(S) dummy device", "0"),
                          ("vtcon1", "(M) frame buffer device", "1")):
        d = root / "class/vtconsole" / n
        d.mkdir(parents=True)
        (d / "name").write_text(name + "\n")
        (d / "bind").write_text(bind + "\n")
    bl = root / "class/backlight/fb_ili9486"
    bl.mkdir(parents=True)
    (bl / "bl_power").write_text(bl_power + "\n")
    fb = root / "class/graphics/fb0"
    fb.mkdir(parents=True)
    (fb / "name").write_text("fb_ili9486\n")
    (fb / "stride").write_text("960\n")
    return root


# ------------------------------------------------------------------------------ RGB565
@pytest.mark.parametrize("rgb,word", [
    ((0, 0, 0), 0x0000),
    ((255, 255, 255), 0xFFFF),
    ((255, 0, 0), 0xF800),
    ((0, 255, 0), 0x07E0),
    ((0, 0, 255), 0x001F),
    ((8, 4, 8), 0x0821),        # smallest step of each channel
    ((7, 3, 7), 0x0000),        # truncation, not rounding
    ((255, 200, 0), 0xFE40),    # the amber of the first test card
])
def test_rgb565_known_colours(rgb, word):
    img = Image.new("RGB", (4, 2), rgb)
    data = display.to_rgb565(img)
    assert len(data) == 4 * 2 * 2
    assert data[:2] == word.to_bytes(2, "little")
    assert data == word.to_bytes(2, "little") * 8


def test_rgb565_row_major_and_mode_conversion():
    img = Image.new("RGB", (2, 2))
    img.putpixel((1, 0), (255, 0, 0))     # x=1, y=0 -> second word
    img.putpixel((0, 1), (0, 0, 255))     # x=0, y=1 -> third word
    data = display.to_rgb565(img)
    words = [int.from_bytes(data[i:i + 2], "little") for i in range(0, 8, 2)]
    assert words == [0, 0xF800, 0x001F, 0]
    assert display.to_rgb565(Image.new("L", (2, 2), 255)) == b"\xff\xff" * 4


def test_framebuffer_show_writes_once_and_skips_identical(tmp_path):
    root = setup_sysfs(tmp_path)
    fbfile = tmp_path / "fb0"
    fbfile.write_bytes(b"\0" * 480 * 320 * 2)
    fb = display.Framebuffer(str(fbfile), sysfs=str(root))
    img = Image.new("RGB", (480, 320), (255, 0, 0))
    assert fb.show(img) is True
    assert fb.show(img.copy()) is False          # identical frame skipped
    assert fbfile.read_bytes() == b"\x00\xf8" * 480 * 320
    assert fb.show(Image.new("RGB", (240, 160), (0, 0, 255))) is True   # resized
    assert fbfile.read_bytes() == b"\x1f\x00" * 480 * 320
    assert (fb.frames_written, fb.frames_skipped) == (2, 1)
    fb.invalidate()
    assert fb.show(Image.new("RGB", (480, 320), (0, 0, 255))) is True
    fb.close()


def test_framebuffer_show_failure_is_not_raised(tmp_path):
    fb = display.Framebuffer(str(tmp_path / "missing" / "fb0"), sysfs=str(tmp_path))
    assert fb.show(Image.new("RGB", (480, 320))) is False


def test_unbind_console_targets_the_framebuffer_vtcon(tmp_path):
    root = setup_sysfs(tmp_path)
    fb = display.Framebuffer(str(tmp_path / "fb0"), sysfs=str(root))
    assert fb.unbind_console() is True
    assert (root / "class/vtconsole/vtcon1/bind").read_text().strip() == "0"
    assert (root / "class/vtconsole/vtcon0/bind").read_text().strip() == "0"
    assert fb.unbind_console() is True           # idempotent


def test_unbind_console_without_fbcon(tmp_path):
    fb = display.Framebuffer(str(tmp_path / "fb0"), sysfs=str(tmp_path))
    assert fb.unbind_console() is False


def test_find_fb_device(tmp_path):
    root = setup_sysfs(tmp_path)
    assert display.find_fb_device(sysfs=str(root)) == "/dev/fb0"
    assert display.find_fb_device("vc4drmfb", sysfs=str(root)) is None


class FakePin:
    """GPIO22 as fbtft drives it: level = (bl_power == 0) XOR inverted-overlay."""

    def __init__(self, bl: Path, inverted: bool):
        self.bl, self.inverted = bl, inverted

    def __call__(self, pin: int) -> bool:
        on = self.bl.read_text().strip() == "0"
        return on != self.inverted


@pytest.mark.parametrize("inverted", [False, True])
def test_backlight_verifies_pin_and_corrects_polarity(tmp_path, inverted):
    root = setup_sysfs(tmp_path, bl_power="4")
    bl = root / "class/backlight/fb_ili9486/bl_power"
    pin = FakePin(bl, inverted)
    fb = display.Framebuffer(str(tmp_path / "fb0"), sysfs=str(root), level_reader=pin)
    assert fb.backlight(True) is True
    assert pin(22) is True                       # lit = physically HIGH
    assert bl.read_text().strip() == ("4" if inverted else "0")
    assert fb.backlight(False) is True
    assert pin(22) is False
    assert fb.backlight(True) is True and pin(22) is True


@pytest.mark.parametrize("inverted", [False, True])
def test_backlight_learns_polarity_without_writing_when_already_lit(tmp_path, inverted):
    """The boot default may already be right (new overlay: bl_power=0 lit; old overlay after a
    root ExecStartPre: bl_power=4 lit). Then nothing is written: no blink, no root needed."""
    root = setup_sysfs(tmp_path, bl_power="4" if inverted else "0")
    bl = root / "class/backlight/fb_ili9486/bl_power"
    pin = FakePin(bl, inverted)
    assert pin(22) is True
    bl.chmod(0o444)                               # any write attempt would now fail
    try:
        fb = display.Framebuffer(str(tmp_path / "fb0"), sysfs=str(root), level_reader=pin)
        assert fb.backlight(True) is True
        assert fb._bl_inverted is inverted        # learnt from (bl_power, pin) alone
    finally:
        bl.chmod(0o644)
    assert fb.backlight(False) is True and pin(22) is False    # learnt polarity used first time
    assert bl.read_text().strip() == ("0" if inverted else "4")


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_backlight_unwritable_and_wrong_reports_false_with_the_fix(tmp_path, caplog):
    root = setup_sysfs(tmp_path, bl_power="4")
    bl = root / "class/backlight/fb_ili9486/bl_power"
    pin = FakePin(bl, inverted=False)             # dark, and k1000 cannot write bl_power
    bl.chmod(0o444)
    try:
        fb = display.Framebuffer(str(tmp_path / "fb0"), sysfs=str(root), level_reader=pin)
        with caplog.at_level(logging.ERROR):
            assert fb.backlight(True) is False
    finally:
        bl.chmod(0o644)
    assert "backlight-on" in caplog.text and "root" in caplog.text


def test_display_cli_rejects_unknown_commands(capsys):
    assert display.main(["bogus"]) == 1
    assert "unknown command" in capsys.readouterr().err


def test_backlight_without_pin_reader_or_device(tmp_path):
    root = setup_sysfs(tmp_path, bl_power="4")
    fb = display.Framebuffer(str(tmp_path / "fb0"), sysfs=str(root), level_reader=lambda p: None)
    assert fb.backlight(True) is True
    assert (root / "class/backlight/fb_ili9486/bl_power").read_text().strip() == "0"
    assert display.Framebuffer("x", sysfs=str(tmp_path / "none")).backlight(True) is False


# ------------------------------------------------------------------------------ pages
def _check(img):
    assert isinstance(img, Image.Image)
    assert img.size == (480, 320) and img.mode == "RGB"


@pytest.mark.parametrize("case", pages.demo_cases(), ids=lambda c: c[0])
def test_render_demo_cases(case):
    name, page, state, status, focus = case
    _check(pages.render(page, state, status, focus))


@pytest.mark.parametrize("page", im.ALL_PAGES + ("BOGUS",))
def test_render_defaults_and_missing_data(page):
    _check(pages.render(page, st.State(), pages.ScreenStatus(), Focus()))
    _check(pages.render(page, st.State()))                 # status/focus omitted
    _check(pages.render(page, None, None, None))           # nothing at all
    _check(pages.render(page, object(), object(), object()))
    _check(pages.render(page, {"master_db": -3}, {"engine": "Running"}, Focus()))


def _extreme_state(which: str) -> st.State:
    s = st.State()
    for path, rng in st.RANGES.items():
        v = rng.min if which == "min" else rng.max
        if path == "out_trim_db":
            s.out_trim_db = [v] * 4
        elif "." in path:
            base, param = path.split(".")
            target = getattr(s, base)
            for band in (target if isinstance(target, list) else [target]):
                setattr(band, param, v)
        else:
            setattr(s, path, v)
    flag = which == "max"
    for b in st.BOOL_FIELDS:
        setattr(s, b, flag)
    return s


@pytest.mark.parametrize("which", ["min", "max"])
@pytest.mark.parametrize("page", im.ALL_PAGES)
def test_render_extreme_states(page, which):
    s = _extreme_state(which)
    status = pages.ScreenStatus(profile="m4" * 20, device="A very long device name " * 4,
                                engine="Stalled" if which == "max" else "Running",
                                load=float("nan") if which == "max" else 999.9,
                                levels=(50.0, float("-inf"), float("nan"), None),
                                clip=(True,), message="x" * 200)
    for slot in range(len(im.LIVE_SLOTS)):
        for item in range(len(im.SETUP_ITEMS)):
            if page != "SETUP" and item:
                break
            f = Focus(slot=slot, item=item, editing="master_db", edit_until=math.inf,
                      held={0, 1, 2, 3}, hint="h" * 100, hint_until=math.inf)
            _check(pages.render(page, s, status, f))


def test_render_garbage_values_do_not_raise():
    s = st.State()
    s.master_db = float("nan")
    s.sub_delay_ms = "fast"          # type: ignore[assignment]
    s.live_peq = []                  # missing bands
    s.live_lowshelf = None           # type: ignore[assignment]
    for page in im.ALL_PAGES:
        _check(pages.render(page, s, pages.ScreenStatus(levels=None, clip=None), Focus(slot=99)))


def test_warm_up_renders_everything():
    assert pages.warm_up() > 0


OK = dict(device_present=True, engine="Running")


def test_overlays_precedence():
    s = st.State()
    ok = pages.ScreenStatus(**OK)
    assert pages.overlays_for(s, ok) == []
    s.muted = True
    assert pages.overlays_for(s, ok) == ["MUTED"]
    assert pages.overlays_for(s, pages.ScreenStatus(muted=False, **OK)) == []   # engine wins
    assert pages.overlays_for(s, pages.ScreenStatus(engine=None)) == ["ENGINE DOWN", "MUTED"]
    assert pages.overlays_for(s, pages.ScreenStatus(engine="Stalled"))[0] == "ENGINE DOWN"
    assert pages.overlays_for(s, pages.ScreenStatus(engine="Paused")) == ["MUTED"]
    nodev = pages.ScreenStatus(device_present=False, engine=None)
    assert pages.overlays_for(s, nodev) == ["NO DEVICE", "MUTED"]


def test_muted_band_is_full_width_red():
    s = st.State()
    s.muted = True
    img = pages.render("MAIN", s, pages.ScreenStatus(**OK), Focus())
    y = pages.MID_Y1 + 16                             # inside the band, below its text
    assert all(img.getpixel((x, y)) == pages.BAND_RED for x in range(480))
    assert img.getpixel((2, 100)) == pages.BAND_RED   # red frame too


def _muted_pill(img) -> bool:
    """A red MUTED pill in the status bar (pixels of BAND_RED within the bar)."""
    return any(img.getpixel((x, pages.STATUS_H // 2)) == pages.BAND_RED for x in range(40, 200))


@pytest.mark.parametrize("page", im.ALL_PAGES)
def test_muted_is_visible_on_every_page(page):
    s = st.State()
    s.muted = True
    img = pages.render(page, s, pages.ScreenStatus(**OK), Focus())
    assert _muted_pill(img)
    assert img.getpixel((2, 100)) == pages.BAND_RED                   # frame
    clean = pages.render(page, st.State(), pages.ScreenStatus(**OK), Focus())
    assert not _muted_pill(clean) and clean.getpixel((2, 100)) != pages.BAND_RED


def test_hwtest_muted_shows_a_red_tag():
    s = st.State()
    img = pages.render("HWTEST", s, pages.ScreenStatus(muted=True, **OK), Focus())
    assert img.getpixel((pages.W - 20, pages.STATUS_H + 10)) == pages.BAND_RED


@pytest.mark.parametrize("confirm", [False, True])
@pytest.mark.parametrize("item", range(len(im.SETUP_ITEMS)))
def test_setup_muted_keeps_selected_row_visible(item, confirm):
    """The selected row (and its PUSH AGAIN) must stay visible while muted: SETUP is used at
    setup time, when the box is muted. MUTED moves to a red footer instead of the band."""
    s = st.State()
    s.muted = True
    field = im.SETUP_ITEMS[item].field
    f = Focus(item=item, confirm=field if confirm else None, confirm_until=math.inf)
    img = pages.render("SETUP", s, pages.ScreenStatus(muted=True, **OK), f, now=0.0)
    rows, row_h, y0 = 8, 28, pages.STATUS_H + 30
    first = max(0, min(item - rows // 2, len(im.SETUP_ITEMS) - rows))
    y = y0 + (item - first) * row_h + 2
    assert img.getpixel((240, y)) == (pages.RED if confirm else pages.AMBER)
    assert img.getpixel((150, pages.H - 8)) == pages.BAND_RED          # MUTED footer
    hinted = Focus(item=item, hint="x", hint_until=math.inf)            # a hint takes the footer
    img = pages.render("SETUP", s, pages.ScreenStatus(muted=True, **OK), hinted, now=0.0)
    assert img.getpixel((20, pages.H - 8)) == pages.AMBER


def test_fine_marker_stays_visible_under_the_muted_band():
    s = st.State()
    s.muted = True
    st_ = pages.ScreenStatus(muted=True, **OK)
    base = dict(slot=1, editing="live_peq.1.q", edit_until=math.inf)    # E4 = bottom-right
    region = [(x, y) for x in range(430, 471) for y in range(276, 289)]
    with_fine = pages.render("LIVE", s, st_, Focus(held={3}, **base), now=0.0)
    without = pages.render("LIVE", s, st_, Focus(**base), now=0.0)
    assert sum(with_fine.getpixel(p) == pages.BG for p in region) > 10   # FINE (BG on AMBER)
    assert all(without.getpixel(p) == pages.AMBER for p in region)


def _band_line(state, status, focus=None):
    return pages._mute_hint(pages._Ctx(focus or Focus(), 0.0), state, status)[1]


def test_muted_band_says_ready_only_when_the_engine_runs_in_sync():
    s = st.State(muted=True)
    run = dict(device_present=True, muted=True)
    assert _band_line(s, pages.ScreenStatus(engine="Running", ready=True, **run)) == \
        "READY · hold B2 to unmute"
    for engine in ("Starting", "Paused"):         # not processing: no READY
        assert _band_line(s, pages.ScreenStatus(engine=engine, ready=True, **run)) == \
            "hold B2 to unmute"
    assert _band_line(s, pages.ScreenStatus(engine="Running", ready=False, **run)) == \
        "hold B2 to unmute"                       # sync failed: knob edits do not reach it


def test_muted_band_tells_a_k1000_hold_from_the_operators_mute():
    """K1000 holding Main before its own ramp is not 'READY · hold B2 to unmute': it comes in
    by itself, and B2 keeps it muted."""
    held = st.State(muted=False)
    status = dict(device_present=True, muted=True, ramping=True, ready=False,
                  message="NO AUDIO FLOWING · HELD MUTED", alert="NO AUDIO FLOWING · HELD MUTED")
    for engine in ("Starting", "Running"):
        assert _band_line(held, pages.ScreenStatus(engine=engine, **status)) == \
            "RAMPS IN BY ITSELF · hold B2 to keep muted"
    # the operator's own mute during the ramp: the reason, never 'ramps in'
    assert _band_line(st.State(muted=True), pages.ScreenStatus(engine="Starting", **status)) == \
        "NO AUDIO FLOWING · HELD MUTED"
    # a hint toast still wins for its 2.5 s
    f = Focus(hint="HOLD B2 TO MUTE / UNMUTE", hint_until=math.inf)
    assert pages._mute_hint(pages._Ctx(f, 0.0), held, pages.ScreenStatus(**status))[0]


@pytest.mark.parametrize("page", ["MAIN", "LIVE", "SETUP"])
def test_controller_alerts_are_visible_while_muted(page):
    s = st.State(muted=True)
    base = dict(device_present=True, engine="Running", muted=True, ready=True)
    plain = pages.render(page, s, pages.ScreenStatus(**base), Focus(), now=0.0)
    alert = pages.render(page, s, pages.ScreenStatus(alert="CANNOT SAVE STATE",
                                                     message="CANNOT SAVE STATE", **base),
                         Focus(), now=0.0)
    diff = sum(a != b for a, b in zip(plain.getdata(), alert.getdata()))
    assert diff > 200, diff


def test_setup_shows_the_controller_alert_in_its_footer():
    s = st.State()
    base = dict(device_present=True, engine="Running", ready=True)
    plain = pages.render("SETUP", s, pages.ScreenStatus(**base), Focus(), now=0.0)
    busy = pages.render("SETUP", s, pages.ScreenStatus(alert="APPLYING SETUP…", **base),
                        Focus(), now=0.0)
    assert plain.getpixel((5, pages.H - 5)) == pages.BG
    assert busy.getpixel((5, pages.H - 5)) == (60, 50, 0)


def test_engine_down_does_not_promise_a_restart_for_a_stall():
    stalled = pages.render("MAIN", st.State(), pages.ScreenStatus(engine="Stalled",
                                                                  device_present=True), Focus())
    inactive = pages.render("MAIN", st.State(), pages.ScreenStatus(engine="Inactive",
                                                                   device_present=True), Focus())
    row = [(x, y) for x in range(480) for y in range(238, 258)]
    assert any(stalled.getpixel(p) != inactive.getpixel(p) for p in row)


def _red_in_status_bar(img) -> int:
    return sum(img.getpixel((x, pages.STATUS_H // 2)) == pages.RED for x in range(40, 330))


def test_clip_and_input_clip_pills_in_the_status_bar():
    s = st.State()
    ok = dict(device_present=True, engine="Running", ready=True)
    assert _red_in_status_bar(pages.render("LIVE", s, pages.ScreenStatus(**ok), Focus())) == 0
    out = pages.render("LIVE", s, pages.ScreenStatus(clip=(False, False, True, False), **ok), Focus())
    inp = pages.render("LIVE", s, pages.ScreenStatus(in_clip=True, **ok), Focus())
    assert _red_in_status_bar(out) > 10 and _red_in_status_bar(inp) > 10
    both = pages.render("MAIN", st.State(muted=True),
                        pages.ScreenStatus(in_clip=True, clip=(True,) * 4, muted=True, **ok), Focus())
    assert _red_in_status_bar(both) > _red_in_status_bar(inp)


def test_fine_label_never_covers_a_unitless_value():
    s = st.State()
    s.live_peq[1].q = 8.0
    f = Focus(slot=1, editing="live_peq.1.q", edit_until=math.inf, held={3})
    img = pages.render("LIVE", s, pages.ScreenStatus(**OK), f, now=0.0)
    d = pages._Draw(Image.new("RGB", (10, 10)))
    fine_left = int(pages.W - 4 - 6 - d.textlength("FINE", font=pages.font(14)))
    # the value's digits (BG on the amber box) stay left of FINE on every row they occupy
    assert not any(img.getpixel((x, y)) == pages.BG
                   for x in range(fine_left - 2, pages.W - 6) for y in range(230, 272))


def test_lowcut_toggle_highlights_the_lowcut_quadrant():
    s = st.State()
    f = Focus(editing="lowcut_on", edit_until=math.inf)
    img = pages.render("MAIN", s, pages.ScreenStatus(**OK), f, now=0.0)
    assert img.getpixel((pages.METER_X1 + 8, 252)) == pages.AMBER     # E4 box (bottom right)


# --- fail closed: unknown is never shown as healthy ---------------------------------------
def _status_bar_has(img, colour) -> bool:
    return any(img.getpixel((x, y)) == colour for x in range(0, 480, 1)
               for y in range(6, pages.STATUS_H - 6, 3))


class WrongNames:
    """A controller status with names the screen does not know."""
    engine_state = None
    device_ok = False
    mute = True


@pytest.mark.parametrize("status,engine_known", [
    (None, False), (WrongNames(), False), (pages.ScreenStatus(), False),
    (pages.ScreenStatus(engine="Running", device_present=None), True),   # device ? only
])
def test_unknown_status_is_not_shown_as_healthy(status, engine_known):
    img = pages.render("MAIN", st.State(), status, Focus(), now=0.0)
    assert _status_bar_has(img, pages.GREEN) is engine_known          # green only if reported
    assert _status_bar_has(img, pages.AMBER)                          # "ENGINE ?" / "DEVICE ?"
    cy = (pages.MID_Y0 + pages.MID_Y1) // 2
    assert img.getpixel((5, cy)) == (60, 50, 0)                       # "STATUS ?" strip


def test_healthy_status_is_green():
    img = pages.render("MAIN", st.State(), pages.ScreenStatus(**OK), Focus(), now=0.0)
    assert _status_bar_has(img, pages.GREEN)


@pytest.mark.parametrize("absent", [False, 0, np.False_])
def test_device_absent_in_any_falsy_form_shows_no_device(absent):
    status = pages.ScreenStatus(device_present=absent, engine="Running")
    assert pages.overlays_for(st.State(), status)[0] == "NO DEVICE"
    assert pages.device_present(status) is False


def test_device_unknown_is_not_absent_but_flagged():
    assert pages.device_present(pages.ScreenStatus()) is None
    assert pages.device_present(None) is None
    assert pages.device_present(pages.ScreenStatus(device_present=np.True_)) is True
    assert pages.engine_unknown(None) and pages.engine_unknown(WrongNames())
    assert not pages.engine_unknown(pages.ScreenStatus(engine=None))   # None = websocket down


def test_status_shape_mismatch_is_logged_once(caplog):
    class OddStatus:
        engine_state = "Running"
    with caplog.at_level(logging.WARNING, logger="k1000.ui.pages"):
        pages.render("MAIN", st.State(), OddStatus(), Focus())
        pages.render("MAIN", st.State(), OddStatus(), Focus())
    msgs = [r.getMessage() for r in caplog.records if "OddStatus" in r.getMessage()]
    assert len(msgs) == 1 and "engine" in msgs[0] and "device_present" in msgs[0]


def test_numpy_levels_and_clip_render():
    status = pages.ScreenStatus(levels=np.array([-10.0, -20.0, -3.0, 0.0]),
                                clip=np.array([False, False, False, True]), **OK)
    img = pages.render("MAIN", st.State(), status, Focus())
    _check(img)
    assert any(img.getpixel((x, pages.STATUS_H + 12)) == pages.RED
               for x in range(pages.METER_X0, pages.METER_X1))           # clip box lit
    _check(pages.render("LIVE", st.State(), pages.ScreenStatus(levels=np.zeros((2, 2)),
                                                                clip=np.ones((3, 3))), Focus()))


@pytest.mark.parametrize("muted", [False, True])
def test_render_error_gives_an_error_frame_not_an_exception(monkeypatch, muted):
    def boom(*a):
        raise RuntimeError("render bug")
    monkeypatch.setitem(pages._PAGES, "MAIN", boom)
    s = st.State()
    s.muted = muted
    img = pages.render("MAIN", s, pages.ScreenStatus(**OK), Focus())
    _check(img)
    assert img.getpixel((240, 130)) == (pages.BAND_RED if muted else pages.BG)
    assert img.getpixel((1, 1)) == (pages.BAND_RED if muted else pages.RED)


def test_displayed_muted_matches_overlays():
    s = st.State()
    for status, want in ((pages.ScreenStatus(**OK), False),
                         (pages.ScreenStatus(muted=True, **OK), True),
                         (pages.ScreenStatus(muted=np.True_, **OK), True),
                         (None, False)):
        assert pages.displayed_muted(s, status) is want
        assert ("MUTED" in pages.overlays_for(s, status)) is want
    s.muted = True
    assert pages.displayed_muted(s, pages.ScreenStatus(**OK)) is True       # state fallback
    assert pages.displayed_muted(s, pages.ScreenStatus(muted=False, **OK)) is False


def test_edit_highlight_and_expiry():
    s = st.State()
    f = Focus(editing="master_db", edit_until=100.0)
    hot = pages.render("MAIN", s, pages.ScreenStatus(), f, now=99.0)
    cold = pages.render("MAIN", s, pages.ScreenStatus(), f, now=101.0)
    assert hot.getpixel((8, 60)) == pages.AMBER
    assert cold.getpixel((8, 60)) == pages.BG


def test_no_device_names_the_device():
    st_ = pages.ScreenStatus(profile="m4", device="MOTU M4", device_present=False)
    img = pages.render("MAIN", st.State(), st_, Focus())
    assert img.getpixel((240, pages.STATUS_H + 3)) == pages.ORANGE
    assert img.getpixel((5, 300)) == (40, 18, 0)


@pytest.mark.skipif(platform.machine() not in ("aarch64", "armv7l"), reason="budget is for the Pi")
def test_render_time_budget_on_pi():
    """Render + RGB565 conversion < 30 ms per frame, with values changing every frame (so the
    text cache misses, as when a knob turns), for every page and overlay."""
    worst = 0.0
    for name, page, s, status, focus in pages.demo_cases():
        times = []
        for i in range(15):
            s.master_db = -79.5 + i * 0.5 + (0.25 if name.endswith("edit") else 0)
            s.live_peq[1].freq = 101.0 + 37 * i
            s.live_peq[1].gain = -19.5 + 0.5 * i
            t0 = time.perf_counter()
            display.to_rgb565(pages.render(page, s, status, focus))
            times.append((time.perf_counter() - t0) * 1000)
        worst = max(worst, statistics.median(times))
    print(f"worst median render+rgb565 with changing values: {worst:.1f} ms")
    assert worst < 30.0


# ------------------------------------------------------------------------------ input map
def test_field_names_exist_in_state():
    """Every field the knobs can emit is a real k1000.state path (numeric, bool or choice)."""
    s = st.State()
    fields = [k.field for p in ("MAIN",) for k in im.KNOBS[p] if k.field]
    fields += [im.live_field(i, p) for i in range(len(im.LIVE_SLOTS)) for p in ("freq", "gain", "q")]
    for f in fields:
        s.get(f)
        assert st.range_for(f) is not None, f
    assert im.F_LOWCUT_ON in st.BOOL_FIELDS
    for item in im.SETUP_ITEMS:
        if item.kind == "page":
            assert item.field in im.ALL_PAGES
            continue
        s.get(item.field)
        if item.kind == "num":
            assert st.range_for(item.field) is not None, item.field
        elif item.kind == "bool":
            assert item.field in st.BOOL_FIELDS
        else:
            assert item.field in st.REBUILD_FIELDS


def test_main_knobs():
    f = Focus()
    assert map_event("MAIN", f, Enc(0, 1, ts=10.0)) == [Nudge("master_db", 1, False)]
    assert map_event("MAIN", f, Enc(1, -1, ts=11.0)) == [Nudge("balance_db", -1, False)]
    arm = map_event("MAIN", f, Enc(2, 2, ts=12.0))              # sub delay: wakes up first
    assert arm == [ShowHint(im.HINT_ARM.format("SUB DELAY"))] and f.editing_now(12.1) == "sub_delay_ms"
    assert map_event("MAIN", f, Enc(2, 2, ts=12.5)) == [Nudge("sub_delay_ms", 2, False)]
    assert map_event("MAIN", f, Enc(3, -1, ts=13.0)) == [Nudge("lowcut_hz", -1, False)]
    assert map_event("MAIN", f, Enc(0, 0, ts=14.0)) == []


def test_sub_delay_brushed_once_does_not_change_it():
    """Every settled sub-delay change dips the subs (delay guard): one brushed detent on the
    show page must not. A detent after ARM_WINDOW_S of rest only wakes the knob again."""
    f = Focus()
    assert not any(isinstance(a, Nudge) for a in map_event("MAIN", f, Enc(2, 1, ts=100.0)))
    assert map_event("MAIN", f, Enc(2, 1, ts=101.0)) == [Nudge("sub_delay_ms", 1, False)]
    assert map_event("MAIN", f, Enc(2, -1, ts=103.0)) == [Nudge("sub_delay_ms", -1, False)]
    later = 103.0 + im.ARM_WINDOW_S + 0.1                       # at rest again: wake first
    assert not any(isinstance(a, Nudge) for a in map_event("MAIN", f, Enc(2, 1, ts=later)))
    # other MAIN knobs and LIVE knobs act on their first detent
    assert map_event("MAIN", Focus(), Enc(1, 1, ts=0.0)) == [Nudge("balance_db", 1, False)]
    assert map_event("LIVE", Focus(), Enc(2, 1, ts=0.0)) == [Nudge("live_peq.0.gain", 1, False)]


def test_fine_while_push_held():
    f = Focus()
    assert map_event("MAIN", f, Sw(2, "press")) == []          # sw2 = E3 push
    assert map_event("MAIN", f, Enc(2, 1, ts=1.0)) == [Nudge("sub_delay_ms", 1, True)]   # deliberate
    assert map_event("MAIN", f, Sw(2, "long")) == []           # still held
    assert map_event("MAIN", f, Enc(2, 1, ts=1.01)) == [Nudge("sub_delay_ms", 1, True)]  # no accel
    map_event("MAIN", f, Sw(2, "release"))
    assert map_event("MAIN", f, Enc(2, 1, ts=2.0)) == [Nudge("sub_delay_ms", 1, False)]
    assert map_event("MAIN", f, Enc(2, 1, ts=5.0)) == [ShowHint(im.HINT_ARM.format("SUB DELAY"))]


def test_acceleration_master_down_faster_than_up_and_frequency():
    f = Focus()
    map_event("MAIN", f, Enc(0, 1, ts=0.0))
    assert map_event("MAIN", f, Enc(0, 1, ts=0.01)) == [Nudge("master_db", 2, False)]   # up: x2 max
    assert map_event("MAIN", f, Enc(0, -1, ts=0.02)) == [Nudge("master_db", -1, False)]  # reversal
    assert map_event("MAIN", f, Enc(0, -1, ts=0.03)) == [Nudge("master_db", -4, False)]  # down: x4
    assert map_event("MAIN", f, Enc(0, -1, ts=0.07)) == [Nudge("master_db", -2, False)]  # 25 det/s
    assert map_event("MAIN", f, Enc(0, -1, ts=1.0)) == [Nudge("master_db", -1, False)]   # pause
    map_event("MAIN", f, Enc(3, 1, ts=2.0))
    assert map_event("MAIN", f, Enc(3, 1, ts=2.01)) == [Nudge("lowcut_hz", 4, False)]
    assert map_event("MAIN", f, Enc(3, 3, ts=2.06)) == [Nudge("lowcut_hz", 12, False)]  # coalesced
    map_event("MAIN", f, Enc(2, 1, ts=3.0))                      # sub delay: never accelerated
    assert map_event("MAIN", f, Enc(2, 1, ts=3.01)) == [Nudge("sub_delay_ms", 1, False)]
    map_event("MAIN", f, Enc(1, 1, ts=4.0))                      # balance: never accelerated
    assert map_event("MAIN", f, Enc(1, 1, ts=4.005)) == [Nudge("balance_db", 1, False)]


def test_live_knobs_follow_selected_slot():
    f = Focus()
    acts = map_event("LIVE", f, Enc(0, 1, ts=0.0))
    assert acts == [SelectSlot(1)]
    assert apply_focus(f, acts[0], now=0.0) is True and f.slot == 1
    assert map_event("LIVE", f, Enc(1, 1, ts=1.0)) == [Nudge("live_peq.1.freq", 1, False)]
    assert map_event("LIVE", f, Enc(2, -1, ts=2.0)) == [Nudge("live_peq.1.gain", -1, False)]
    assert map_event("LIVE", f, Enc(3, 1, ts=3.0)) == [Nudge("live_peq.1.q", 1, False)]
    f.slot = 4
    assert map_event("LIVE", f, Enc(1, 1, ts=4.0)) == [Nudge("live_lowshelf.freq", 1, False)]
    map_event("LIVE", f, Sw(1, "press"))                          # E2 push = fine frequency
    assert map_event("LIVE", f, Enc(1, -3, ts=5.0)) == [Nudge("live_lowshelf.freq", -3, True)]
    apply_focus(f, SelectSlot(2), now=0.0)                        # wraps 4 -> 0
    assert f.slot == 0
    apply_focus(f, SelectSlot(-1), now=0.0)
    assert f.slot == 5


def test_buttons():
    f = Focus()
    assert map_event("MAIN", f, Sw(4, "press")) == [NextPage()]
    assert map_event("MAIN", f, Sw(4, "short")) == []
    assert map_event("MAIN", f, Sw(5, "press")) == []
    assert map_event("MAIN", f, Sw(5, "short")) == [ShowHint(im.HINT_MUTE)]
    assert map_event("MAIN", f, Sw(5, "long")) == [Mute("toggle")]
    assert map_event("MAIN", f, Sw(6, "short")) == [Toggle("lowcut_on")]
    assert map_event("MAIN", f, Sw(6, "long")) == []
    assert map_event("LIVE", f, Sw(7, "short", ts=1.0)) == [ShowHint(im.HINT_RESET)]
    assert map_event("SETUP", f, Sw(5, "long")) == [Mute("toggle")]   # mute works everywhere


@pytest.mark.parametrize("page", ["LIVE", "SETUP"])
def test_b3_lowcut_acts_on_main_only(page):
    """Tapping buttons in the dark to find the mute must not change the tops' low end on a
    page that does not show it."""
    assert map_event(page, Focus(), Sw(6, "short")) == [ShowHint(im.HINT_LOWCUT_ON_MAIN)]


def test_b4_reset_live_needs_live_page_and_a_second_long_press():
    f = Focus()
    for page in ("MAIN", "SETUP"):                 # never off LIVE: hint only
        assert map_event(page, f, Sw(7, "long", ts=1.0)) == [ShowHint(im.HINT_RESET_ON_LIVE)]
        assert map_event(page, f, Sw(7, "short", ts=1.0)) == [ShowHint(im.HINT_RESET_ON_LIVE)]
    first = map_event("LIVE", f, Sw(7, "long", ts=10.0))
    assert first == [ShowHint(im.HINT_RESET_AGAIN, im.CONFIRM_WINDOW_S)]
    apply_focus(f, first[0], now=10.0)
    assert f.hint_now(14.0) == im.HINT_RESET_AGAIN          # stays up for the whole window
    assert map_event("LIVE", f, Sw(7, "long", ts=12.0)) == [ResetLive()]
    assert f.confirm is None
    # an expired confirm arms again instead of resetting
    map_event("LIVE", f, Sw(7, "long", ts=20.0))
    assert map_event("LIVE", f, Sw(7, "long", ts=30.0)) == [ShowHint(im.HINT_RESET_AGAIN,
                                                                     im.CONFIRM_WINDOW_S)]
    # a B4 confirm never confirms a SETUP rebuild item (and the other way round)
    g = Focus(item=next(i for i, it in enumerate(im.SETUP_ITEMS) if it.field == "mono_subs"))
    map_event("LIVE", g, Sw(7, "long", ts=40.0))
    assert map_event("SETUP", g, Sw(0, "short", ts=41.0)) == [ShowHint(im.HINT_CONFIRM)]


def test_setup_push_after_a_fine_turn_is_not_a_select():
    """The LIVE fine-mode habit (hold a push, turn, release fast = 'short') must not flip the
    selected SETUP item (a polarity) or open a page."""
    idx = next(i for i, it in enumerate(im.SETUP_ITEMS) if it.field == "sub_inverted")
    f = Focus(item=idx)
    assert map_event("SETUP", f, Sw(1, "press", ts=1.0)) == []
    map_event("SETUP", f, Enc(1, 1, ts=1.1))
    assert map_event("SETUP", f, Sw(1, "short", ts=1.3)) == []          # no Toggle
    assert map_event("SETUP", f, Sw(1, "press", ts=2.0)) == []          # a plain push still selects
    assert map_event("SETUP", f, Sw(1, "short", ts=2.1)) == [Toggle("sub_inverted")]


def test_slot_and_item_follow_every_detent():
    """A fast turn reaches the controller as one merged event: every detent counts."""
    assert map_event("LIVE", Focus(), Enc(0, 3)) == [SelectSlot(3)]
    assert map_event("SETUP", Focus(), Enc(0, -2)) == [SelectItem(-2)]
    f = Focus()
    apply_focus(f, SelectSlot(3))
    assert f.slot == 3


@pytest.mark.parametrize("page", im.ALL_PAGES)
def test_b2_long_mutes_on_every_page_with_an_explicit_target(page):
    """The dispatcher passes the displayed mute; B2 long asks for its opposite."""
    assert map_event(page, Focus(), Sw(5, "long"), muted=False) == [Mute(True)]
    assert map_event(page, Focus(), Sw(5, "long"), muted=True) == [Mute(False)]
    assert map_event(page, Focus(), Sw(5, "long"), muted=np.True_) == [Mute(False)]
    assert map_event(page, Focus(), Sw(5, "long")) == [Mute("toggle")]     # unknown
    assert im.mute_action(np.array([True, False])) == Mute("toggle")


def test_setup_navigation_and_values():
    f = Focus()
    acts = map_event("SETUP", f, Enc(0, 1))
    assert acts == [SelectItem(1)]
    apply_focus(f, acts[0])
    assert f.item == 1 and im.SETUP_ITEMS[1].kind == "bool"
    assert map_event("SETUP", f, Enc(1, 1)) == [ShowHint(im.HINT_PUSH_TOGGLE)]
    assert map_event("SETUP", f, Sw(1, "short")) == [Toggle(im.SETUP_ITEMS[1].field)]
    f.item = 0
    assert map_event("SETUP", f, Enc(1, -2)) == [Nudge("input_trim_db", -2, False)]
    assert map_event("SETUP", f, Enc(2, 1)) == []                   # E3 unused on SETUP
    apply_focus(f, SelectItem(-5))
    assert f.item == 0
    apply_focus(f, SelectItem(99))
    assert f.item == len(im.SETUP_ITEMS) - 1
    assert map_event("SETUP", f, Sw(0, "short")) == [GotoPage("HWTEST")]


def test_setup_rebuild_needs_two_pushes_within_window():
    idx = next(i for i, it in enumerate(im.SETUP_ITEMS) if it.field == "mono_subs")
    f = Focus(item=idx)
    assert map_event("SETUP", f, Sw(0, "short", ts=10.0)) == [ShowHint(im.HINT_CONFIRM)]
    assert f.confirm_now(10.5) == "mono_subs"
    assert map_event("SETUP", f, Sw(1, "short", ts=11.0)) == [Toggle("mono_subs")]
    assert f.confirm is None
    assert map_event("SETUP", f, Sw(0, "short", ts=20.0)) == [ShowHint(im.HINT_CONFIRM)]
    assert map_event("SETUP", f, Sw(0, "short", ts=30.0)) == [ShowHint(im.HINT_CONFIRM)]  # expired
    apply_focus(f, SelectItem(-1))                                   # moving cancels
    assert f.confirm is None


def test_hwtest_records_and_exits_on_long_press():
    f = Focus()
    assert map_event("HWTEST", f, Enc(2, 3)) == []
    assert map_event("HWTEST", f, Sw(5, "press")) == []
    assert f.hw.enc_count[2] == 3 and f.hw.sw_down[5] is True
    assert f.hw.last.endswith("= B2")
    assert map_event("HWTEST", f, Sw(5, "short")) == []           # short presses only logged
    assert map_event("HWTEST", f, Sw(5, "long"), muted=False) == [Mute(True)]  # B2 still mutes
    assert f.hw.sw_last[5] == "long"                               # ...and is still logged
    assert map_event("HWTEST", f, Sw(4, "press")) == []
    assert map_event("HWTEST", f, Sw(4, "long")) == [GotoPage("SETUP")]
    assert map_event("HWTEST", f, Sw(0, "long")) == [GotoPage("SETUP")]   # any other switch
    assert len(f.hw.lines) <= 4


def test_hwtest_mute_follows_the_configured_b2():
    cfg = {"controls": {"buttons": [7, 6, 5, 4]}}                  # B2 = sw6
    assert map_event("HWTEST", Focus(), Sw(6, "long"), cfg, muted=True) == [Mute(False)]
    assert map_event("HWTEST", Focus(), Sw(5, "long"), cfg) == [GotoPage("SETUP")]


def test_control_map_from_cfg():
    cfg = {"controls": {"encoders": [3, 2, 1, 0], "buttons": [0, 1, 2, 3],
                        "pushes": [4, 5, 6, 7], "reverse": [True, False, False, False]}}
    f = Focus()
    assert map_event("MAIN", f, Enc(3, 1, ts=0.0), cfg) == [Nudge("master_db", -1, False)]
    assert map_event("MAIN", f, Enc(0, 1, ts=5.0), cfg) == [Nudge("lowcut_hz", 1, False)]
    assert map_event("MAIN", f, Sw(0, "press"), cfg) == [NextPage()]
    assert map_event("MAIN", f, Sw(4, "press"), cfg) == []          # now E1 push
    assert f.held == {0}
    hw = Focus()
    map_event("HWTEST", hw, Enc(3, 1), cfg)
    assert hw.hw.enc_role == ["E4", "E3", "E2", "E1"] and hw.hw.sw_role[0] == "B1"


@pytest.mark.parametrize("cfg", [
    None, {}, {"controls": None}, {"controls": "junk"},
    {"controls": {"encoders": [0, 0, 1, 2]}},                     # duplicate
    {"controls": {"encoders": [0, 1, 2]}},                        # too short
    {"controls": {"buttons": [0, 1, 2, 3]}},                      # collides with default pushes
    {"controls": {"encoders": ["a", 1, 2, 3]}},
    {"controls": {"encoders": ["0", "1", "2", "3"]}},             # quoted: not an index
    {"controls": {"encoders": [True, 0, 2, 3]}},                  # bool is not an index
    {"controls": {"encoders": [0.0, 1.7, 2, 3]}},
])
def test_control_map_invalid_falls_back_to_identity(cfg):
    m = ControlMap.from_cfg(cfg)
    assert m.encoders == (0, 1, 2, 3)
    assert m.buttons == (4, 5, 6, 7) and m.pushes == (0, 1, 2, 3)


@pytest.mark.parametrize("rev,want", [
    (["false", "false", "false", "false"], (False,) * 4),   # quoted YAML: default, NOT reversed
    (["true", False, False, False], (False,) * 4),
    ([1, 0, 0, 0], (True, False, False, False)),
    ([True, False, True, False], (True, False, True, False)),
    ([2, 0, 0, 0], (False,) * 4),
    ("TFFF", (False,) * 4),
])
def test_control_map_reverse_accepts_only_real_bools(rev, want):
    assert ControlMap.from_cfg({"controls": {"reverse": rev}}).reverse == want


def test_unknown_events_are_ignored():
    f = Focus()
    assert map_event("MAIN", f, Enc(9, 1)) == []
    assert map_event("MAIN", f, Sw(9, "press")) == []
    assert map_event("BOGUS", f, Enc(0, 1)) == []


def test_apply_focus_marks_edits_and_hints():
    f = Focus()
    assert apply_focus(f, Nudge("master_db", 1), now=5.0) is False   # controller must act
    assert f.editing_now(6.0) == "master_db" and f.editing_now(9.0) is None
    assert apply_focus(f, Toggle("lowcut_on"), now=5.0) is False
    assert apply_focus(f, ShowHint("x"), now=5.0) is True
    assert f.hint_now(6.0) == "x" and f.hint_now(8.0) is None
    for a in (NextPage(), Mute(), ResetLive(), GotoPage("HWTEST")):
        assert apply_focus(f, a) is False


def test_knob_corner_table_is_a_permutation():
    assert sorted(im.KNOB_CORNERS) == ["bl", "br", "tl", "tr"]
    for page in im.ALL_PAGES:
        assert len(im.KNOBS[page]) == 4
