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

from k1000 import control as ctl_mod
from k1000 import state as st
from k1000.ui import display, eqgraph, input_map as im, pages
from k1000.ui.input_map import (ControlMap, Focus, GotoPage, Lock, Mute, NextPage, NotchHelper,
                                Nudge, ResetLive, SelectBand, SelectItem, SelectSlot, ShowHint,
                                Toggle, UndoReset, Unlock, apply_focus, map_event)


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


def _rgb565_v0(img: Image.Image) -> bytes:
    """v0's converter (uint16 words), the reference the packer must match byte for byte."""
    a = np.asarray(img.convert("RGB"), dtype=np.uint16)
    v = ((a[:, :, 0] & 0xF8) << 8) | ((a[:, :, 1] & 0xFC) << 3) | (a[:, :, 2] >> 3)
    return v.astype("<u2").tobytes()


def test_rgb565_packer_is_byte_identical_to_the_v0_converter():
    rng = np.random.default_rng(565)
    noise = Image.fromarray(rng.integers(0, 256, (320, 480, 3), dtype=np.uint8), "RGB")
    every = np.zeros((256, 256, 3), np.uint8)          # every value of every channel
    every[..., 0] = np.arange(256)[:, None]
    every[..., 1] = np.arange(256)[None, :]
    every[..., 2] = (np.arange(256)[:, None] + 3 * np.arange(256)[None, :]) % 256
    frame = pages.render(*pages.demo_cases()[0][1:])    # a real SHOW frame
    for img in (noise, Image.fromarray(every, "RGB"), frame, Image.new("L", (7, 3), 200),
                Image.new("RGBA", (5, 5), (12, 200, 99, 10))):
        assert display.to_rgb565(img) == _rgb565_v0(img)


def test_changed_rows_gives_the_span_of_differing_rows():
    stride = 960
    a = bytearray(stride * 320)
    assert display.changed_rows(bytes(a), None, stride) == (0, 320)        # nothing written yet
    assert display.changed_rows(bytes(a), bytes(a), stride) is None
    b = bytearray(a)
    b[40 * stride + 7] = 1
    b[200 * stride + stride - 1] = 9
    assert display.changed_rows(bytes(b), bytes(a), stride) == (40, 201)
    assert display.changed_rows(bytes(b), bytes(a[:-stride]), stride) == (0, 320)   # resized
    odd = bytearray(30)                                  # a stride that is not 8-byte aligned
    odd[13] = 1
    assert display.changed_rows(bytes(odd), bytes(30), 6) == (2, 3)


def test_framebuffer_writes_only_the_changed_rows(tmp_path, monkeypatch):
    root = setup_sysfs(tmp_path)
    fbfile = tmp_path / "fb0"
    fbfile.write_bytes(b"\0" * 480 * 320 * 2)
    fb = display.Framebuffer(str(fbfile), sysfs=str(root))
    writes: list[tuple[int, int]] = []
    real = os.pwrite

    def spy(fd, data, off):
        writes.append((off, len(data)))
        return real(fd, data, off)

    monkeypatch.setattr(display.os, "pwrite", spy)
    img = Image.new("RGB", (480, 320), (0, 0, 0))
    assert fb.show(img) is True
    assert writes[-1] == (0, 480 * 320 * 2) and fb.last_span == (0, 320)    # first frame: all
    img2 = img.copy()
    img2.putpixel((10, 130), (255, 255, 255))
    img2.putpixel((470, 250), (255, 0, 0))
    assert fb.show(img2) is True
    assert writes[-1] == (130 * 960, (251 - 130) * 960) and fb.last_span == (130, 251)
    assert fbfile.read_bytes() == _rgb565_v0(img2)                          # the panel is right
    assert fb.show(img2.copy()) is False and len(writes) == 2               # identical: skipped
    fb.invalidate()                                                         # forced rewrite
    assert fb.show(img2) is True and writes[-1] == (0, 480 * 320 * 2)
    assert fb.rows_written == 320 + 121 + 320
    fb.close()


def test_framebuffer_failed_write_rewrites_the_whole_frame_next(tmp_path, monkeypatch):
    root = setup_sysfs(tmp_path)
    fbfile = tmp_path / "fb0"
    fbfile.write_bytes(b"\0" * 480 * 320 * 2)
    fb = display.Framebuffer(str(fbfile), sysfs=str(root))
    assert fb.show(Image.new("RGB", (480, 320), (0, 0, 255))) is True
    real = os.pwrite
    monkeypatch.setattr(display.os, "pwrite", lambda *a: (_ for _ in ()).throw(OSError("spi")))
    img = Image.new("RGB", (480, 320), (0, 0, 255))
    img.putpixel((0, 100), (255, 255, 255))
    assert fb.show(img) is False                          # logged, not raised; fd closed
    writes: list[tuple[int, int]] = []
    monkeypatch.setattr(display.os, "pwrite",
                        lambda fd, d, off: (writes.append((off, len(d))), real(fd, d, off))[1])
    assert fb.show(img) is True and writes == [(0, 480 * 320 * 2)]
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


@pytest.mark.parametrize("page,editing,region", [
    # SHOW: E4 (Q) bottom-right box, FINE at its top-right corner
    ("SHOW", "live_peq.1.q", [(x, y) for x in range(452, 471) for y in range(259, 270)]),
    # ALIGN (the v0 band): E4 (LO-CUT) bottom-right quadrant
    ("ALIGN", "lowcut_hz", [(x, y) for x in range(430, 471) for y in range(276, 289)]),
])
def test_fine_marker_stays_visible_under_the_muted_band(page, editing, region):
    s = st.State()
    s.muted = True
    st_ = pages.ScreenStatus(muted=True, **OK)
    base = dict(slot=1, editing=editing, edit_until=math.inf)           # E4 = bottom-right
    with_fine = pages.render(page, s, st_, Focus(held={3}, **base), now=0.0)
    without = pages.render(page, s, st_, Focus(**base), now=0.0)
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


def test_setup_shows_the_controller_alert_in_the_status_bar():
    """D-009: system alerts take the status bar (APPLYING SETUP… is a passing state: blue),
    the SETUP footer keeps the hints and the MUTED strip."""
    s = st.State()
    base = dict(device_present=True, engine="Running", ready=True)
    plain = pages.render("SETUP", s, pages.ScreenStatus(**base), Focus(), now=0.0)
    busy = pages.render("SETUP", s, pages.ScreenStatus(alert="APPLYING SETUP…",
                                                       alerts=[("info", "APPLYING SETUP…")],
                                                       **base), Focus(), now=0.0)
    old = pages.render("SETUP", s, pages.ScreenStatus(alert="APPLYING SETUP…", **base),
                       Focus(), now=0.0)                  # an older status: no `alerts`
    for img in (plain, busy, old):
        assert img.getpixel((5, pages.H - 5)) == pages.BG
    assert not _status_bar_has(plain, pages.INFO_BLUE)
    assert _status_bar_has(busy, pages.INFO_BLUE)
    assert _status_bar_has(old, pages.ORANGE)             # derived from `alert`: a warning


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


@pytest.mark.parametrize("slot", [1, 4])          # a bell (unit '1/6 oct'), a shelf ('Q')
def test_fine_label_never_covers_the_value_or_its_unit(slot):
    s = st.State()
    s.live_peq[1].q = 8.0
    fld = im.live_field(slot, "q")
    f = Focus(slot=slot, editing=fld, edit_until=math.inf, held={3})
    img = pages.render("SHOW", s, pages.ScreenStatus(**OK), f, now=0.0)
    d = pages._Draw(Image.new("RGB", (10, 10)))
    fine_left = int(pages.W - 10 - d.textlength("FINE", font=pages.font(14)))
    # FINE (BG on the amber box) has its own column at the right edge, at the box's top: the
    # value's digits and unit stay left of that column on every row
    y0, y1 = pages.BOT_BOX
    fine_rows = range(y0 + 3, y0 + 17)
    assert any(img.getpixel((x, y)) == pages.BG for x in range(fine_left, pages.W - 6)
               for y in fine_rows)                                                  # FINE
    assert not any(img.getpixel((x, y)) == pages.BG          # (the box's round corners aside)
                   for x in range(fine_left - 2, pages.W - 9) for y in range(y0 + 3, y1 - 2)
                   if y not in fine_rows)


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
    for page in ("ALIGN", "SHOW"):
        img = pages.render(page, st.State(), status, Focus(), now=0.0)
        assert _status_bar_has(img, pages.GREEN) is engine_known      # green only if reported
        assert _status_bar_has(img, pages.AMBER) is not engine_known  # "ENGINE ?"
        assert _status_bar_has(img, pages.ORANGE)                     # the "STATUS ?" alert
    assert pages.screen_alerts(status)[0][1].startswith("STATUS ?")


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
    monkeypatch.setitem(pages._PAGES, "ALIGN", boom)
    s = st.State()
    s.muted = muted
    img = pages.render("MAIN", s, pages.ScreenStatus(**OK), Focus())     # v0 name of ALIGN
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


# --- UI v1 (D-009): SHOW, ALIGN, SETUP, alerts vs hints ------------------------------------
RUN = dict(device_present=True, engine="Running", ready=True)
INF = math.inf


def _count(img, box, colour) -> int:
    """Pixels of exactly `colour` inside box (x0, y0, x1, y1), x1/y1 exclusive."""
    x0, y0, x1, y1 = (int(v) for v in box)
    a = np.asarray(img)[y0:y1, x0:x1]
    return int((a == np.array(colour, np.uint8)).all(axis=2).sum())


def _crop(img, box):
    return np.asarray(img)[box[1]:box[3], box[0]:box[2]]


def _zero_db_y() -> int:
    g = pages.GRAPH
    return round(eqgraph.DbAxis(g[1], g[3] - 1, eqgraph.STYLE.top_db, eqgraph.STYLE.bottom_db).y(0.0))


STATUS_BAR = (0, 0, pages.W, pages.STATUS_H)


def test_show_draws_the_curve_over_the_program_spectrum():
    s = st.State()
    fr = pages.demo_spectrum()
    on = pages.render("SHOW", s, pages.ScreenStatus(spectrum=fr, **RUN), Focus(), now=0.0)
    off = pages.render("SHOW", s, pages.ScreenStatus(spectrum=fr, spectrum_on=False, **RUN),
                       Focus(), now=0.0)
    none = pages.render("SHOW", s, pages.ScreenStatus(**RUN), Focus(), now=0.0)
    g, fill = pages.GRAPH, eqgraph.STYLE.rta_fill
    assert _count(on, g, fill) > 2000
    assert _count(off, g, fill) == 0 and _count(none, g, fill) == 0     # SETUP > Spectrum OFF
    for img in (on, none):            # a flat EQ is a white 0 dB line, over the spectrum too
        row = np.asarray(img)[_zero_db_y()]
        assert (row == pages.FG).all(axis=1).mean() > 0.8
    # the graph stays in its box: chips and values above/below are the same with and without
    assert (_crop(on, (0, pages.STATUS_H, pages.W, g[1])) ==
            _crop(none, (0, pages.STATUS_H, pages.W, g[1]))).all()
    assert (_crop(on, (0, g[3], pages.W, pages.H)) == _crop(none, (0, g[3], pages.W, pages.H))).all()


def test_show_peak_is_a_small_cyan_pointer_and_its_frequency():
    fr = pages.demo_spectrum()
    assert fr is not None and fr.peak_hz is not None
    img = pages.render("SHOW", st.State(), pages.ScreenStatus(spectrum=fr, **RUN), Focus(), now=0.0)
    g = pages.GRAPH
    x = round(eqgraph.LogAxis(0, pages.W - 1).x(fr.peak_hz))
    assert _count(img, (x - 8, g[1], x + 9, g[1] + 12), pages.CYAN) > 20          # the pointer
    assert _count(img, (x + 9, g[1], min(pages.W, x + 80), g[1] + 15), pages.CYAN) > 20  # "2.43k"
    assert _count(img, (x - 2, g[1] + 20, x + 3, g[3] - 20), pages.CYAN) == 0     # no line down
    assert _count(img, g, (255, 220, 0)) == 0                                     # no old yellow
    quiet = pages.demo_spectrum(ring_hz=None)
    assert quiet.peak_hz is None
    img = pages.render("SHOW", st.State(), pages.ScreenStatus(spectrum=quiet, **RUN), Focus(), now=0.0)
    assert _count(img, (0, g[1], pages.W, g[1] + 15), pages.CYAN) == 0


def test_show_labels_the_0db_line_and_the_db_scale_at_the_right_edge():
    y0 = _zero_db_y()
    fill = eqgraph.STYLE.label_db_fill
    for fr in (None, pages.demo_spectrum()):
        img = pages.render("SHOW", st.State(), pages.ScreenStatus(spectrum=fr, **RUN), Focus(),
                           now=0.0)
        assert _count(img, (pages.W - 50, y0 + 2, pages.W - 2, y0 + 18), fill) > 15     # "0 dB"
        da = eqgraph.DbAxis(pages.GRAPH[1], pages.GRAPH[3] - 1, 9.0, -21.0)
        for db in (6.0, -6.0, -12.0):
            y = round(da.y(db))
            assert _count(img, (pages.W - 40, y + 2, pages.W - 2, y + 18), fill) > 8, db


def test_show_muted_band_leaves_the_0db_line_and_every_value_visible():
    s = st.State()
    s.live_peq[1].gain = -6.5
    f = Focus(slot=1, editing="live_peq.1.gain", edit_until=INF, held={2})
    fr = pages.demo_spectrum()
    clean = pages.render("SHOW", s, pages.ScreenStatus(spectrum=fr, **RUN), f, now=0.0)
    muted = pages.render("SHOW", st.State.from_dict({**s.to_dict(), "muted": True}),
                         pages.ScreenStatus(spectrum=fr, muted=True, **RUN), f, now=0.0)
    b0, b1 = pages.SHOW_MUTED_BAND
    assert all(muted.getpixel((x, b0 + 4)) == pages.BAND_RED for x in range(pages.W))
    y0 = _zero_db_y()
    assert b1 < y0 - 1
    kept = [(8, pages.TOP_BOX[0], 196, pages.TOP_BOX[1] + 1),           # E1 MASTER
            (288, pages.TOP_BOX[0], 472, pages.TOP_BOX[1] + 1),         # E3 GAIN (edited, FINE)
            (8, pages.BOT_BOX[0], 236, pages.BOT_BOX[1] + 1),           # E2 FREQ
            (246, pages.BOT_BOX[0], 472, pages.BOT_BOX[1] + 1),         # E4 Q
            (8, y0 - 1, 472, y0 + 18)]                                  # 0 dB line + its label
    for box in kept:
        assert (_crop(clean, box) == _crop(muted, box)).all(), box


def test_show_band_chips_are_ls_1_2_3_4_hs_without_a_sub_chip():
    s = st.State()
    w = (pages.W - 8 - 4 * 5) / 6
    for k, slot in enumerate(im.BAND_CYCLE):
        img = pages.render("SHOW", s, pages.ScreenStatus(**RUN), Focus(slot=slot), now=0.0)
        cx = round(4 + k * (w + 4) + 6)
        assert img.getpixel((cx, pages.CHIP_Y0 + 3)) == pages.AMBER, slot
        others = [round(4 + j * (w + 4) + 6) for j in range(6) if j != k]
        assert all(img.getpixel((x, pages.CHIP_Y0 + 3)) != pages.AMBER for x in others)


def test_show_band_name_lights_up_when_b3_changes_the_band():
    s = st.State()
    cold = pages.render("SHOW", s, pages.ScreenStatus(**RUN), Focus(slot=2), now=0.0)
    hot = pages.render("SHOW", s, pages.ScreenStatus(**RUN),
                       Focus(slot=2, editing=im.F_SLOT, edit_until=INF), now=0.0)
    lab = (pages.METER_BOX[2], pages.STATUS_H, pages.W, pages.TOP_BOX[0])
    assert _count(hot, lab, pages.AMBER) > _count(cold, lab, pages.AMBER) + 300


def test_show_pills_persist_under_hints_and_the_muted_band():
    s = st.State()
    region = (0, pages.PILL_Y0, pages.W, pages.PILL_Y1 + 1)
    two = ["LO-CUT OFF", "MONO SUB"]
    img = pages.render("SHOW", s, pages.ScreenStatus(pills=two, **RUN), Focus(), now=0.0)
    none = pages.render("SHOW", s, pages.ScreenStatus(pills=[], **RUN), Focus(), now=0.0)
    hinted = pages.render("SHOW", s, pages.ScreenStatus(pills=two, **RUN),
                          Focus(hint="MASTER: SLOW DOWN", hint_until=INF), now=0.0)
    muted = pages.render("SHOW", st.State(muted=True),
                         pages.ScreenStatus(pills=two, muted=True, **RUN), Focus(), now=0.0)
    assert _count(img, region, pages.PILL_BG) > 300 and _count(none, region, pages.PILL_BG) == 0
    inner = (8, pages.PILL_Y0, pages.W - 8, pages.PILL_Y1 + 1)       # (the MUTED frame aside)
    assert (_crop(img, inner) == _crop(hinted, inner)).all()
    assert (_crop(img, inner) == _crop(muted, inner)).all()
    # many pills: they never run under the E2/E4 labels; the overflow is counted
    many = [f"PILL NUMBER {i}" for i in range(8)]
    img = pages.render("SHOW", s, pages.ScreenStatus(pills=many, **RUN), Focus(), now=0.0)
    assert _count(img, (0, pages.PILL_Y0, 105, pages.PILL_Y1), pages.PILL_BG) == 0
    assert _count(img, (pages.W - 60, pages.PILL_Y0, pages.W, pages.PILL_Y1), pages.PILL_BG) == 0
    assert _count(img, region, pages.PILL_BG) > 100
    # an older status (no `pills`): the state tells the non-default ones
    assert pages.screen_pills(st.State(lowcut_on=False, mono_subs=True, sub_inverted=True,
                                       top_inverted=True), pages.ScreenStatus()) == \
        ["LO-CUT OFF", "POL INV SUB+TOP", "MONO SUB"]
    assert pages.screen_pills(st.State(dev_audition="tops", profile="dev"), None) == ["AUDITION TOPS"]
    assert pages.screen_pills(st.State(), pages.ScreenStatus()) == []
    assert pages.screen_pills(None, pages.ScreenStatus(pills=("A", "", None, "B"))) == ["A", "B"]


@pytest.mark.parametrize("page", ["SHOW", "ALIGN", "SETUP", "HWTEST", "PHONE"])
def test_system_alerts_take_the_status_bar_in_their_own_colour(page):
    """D-009: KNOBS OFFLINE red across the whole status bar; a warning orange; a state blue;
    never amber (the hints' colour)."""
    s = st.State()
    knobs = "KNOBS OFFLINE · USE PHONE MUTE / BYPASS"
    crit = pages.render(page, s, pages.ScreenStatus(alerts=[("critical", knobs)], **RUN), Focus())
    bar = np.asarray(crit)[2:pages.STATUS_H - 2]
    assert (bar == np.array(pages.RED, np.uint8)).all(axis=2).mean() > 0.5
    corner = (pages.W - 24, 2, pages.W, pages.STATUS_H - 2)            # where the engine dot is
    assert _count(crit, corner, pages.RED) > 300                       # full width
    muted = pages.render(page, st.State(muted=True),
                         pages.ScreenStatus(alerts=[("critical", knobs)], muted=True, **RUN), Focus())
    assert _count(muted, STATUS_BAR, pages.BAND_RED) > 100                     # MUTED pill kept
    for level, colour in (("warning", pages.ORANGE), ("info", pages.INFO_BLUE),
                          ("critical", pages.RED)):
        img = pages.render(page, s, pages.ScreenStatus(alerts=[(level, "CANNOT SAVE STATE")], **RUN),
                           Focus())
        assert _count(img, STATUS_BAR, colour) > 500, level
        assert _count(img, STATUS_BAR, pages.AMBER) == 0, level
        assert _count(img, corner, colour) == 0, level                 # not full width
    plain = pages.render(page, s, pages.ScreenStatus(alerts=[], **RUN), Focus())
    assert _status_bar_has(plain, pages.GREEN) and not _status_bar_has(plain, pages.ORANGE)


def test_alert_text_is_never_tiny_and_never_cut_at_both_ends():
    """UX-5: the alert shrank to 10 px and overflowed its pill ('NGINE DEVICES CHANGED OUTSIDE
    K100'). Now >= 12 px, ellipsized at the end, the page pill in 3 letters, short texts."""
    for text in ("KNOBS OFFLINE · USE PHONE MUTE / BYPASS", "ENGINE DEVICES CHANGED OUTSIDE K1000",
                 "DEVICES CHANGED (GUI)", "X" * 80):
        for room in (60, 140, 250, 400):
            size, shown = pages._fit_ellipsis(text, 18, room)
            assert size >= pages.ALERT_MIN_PX
            assert pages._textlength(shown, pages.font(size)) <= room, (text, room)
            assert shown == text or shown.endswith("…")
            if shown != text:
                assert text.startswith(shown[:-1].rstrip(" ·"))       # cut at the end only
    assert ctl_mod.MSG_DEVICES_CHANGED == "DEVICES CHANGED (GUI)"
    assert ctl_mod.MSG_PIPELINE_CHANGED == "PIPELINE CHANGED (GUI)"
    # the most crowded bar renders; with an alert the page pill is three letters wide
    s = st.State(muted=True)
    crowded = pages.ScreenStatus(alerts=[("warning", "ENGINE DEVICES CHANGED OUTSIDE K1000")],
                                 muted=True, clip=(False, False, False, True), in_clip=True, **RUN)
    pages.render("SHOW", s, crowded, Focus())
    calm = pages.render("SHOW", st.State(), pages.ScreenStatus(**RUN), Focus())
    alert = pages.render("SHOW", st.State(), pages.ScreenStatus(alerts=[("warning", "X")], **RUN),
                         Focus())
    pill = (0, 4, 90, pages.STATUS_H - 5)
    assert _count(alert, pill, pages.FG) < _count(calm, pill, pages.FG)       # "SHW" < "SHOW"
    assert pages.PAGE_SHORT["SHOW"] == "SHW" and pages.PAGE_SHORT["ALIGN"] == "ALN"


def test_a_critical_alert_is_repeated_over_the_chip_row_when_no_hint_is_up():
    """UX-5: KNOBS OFFLINE's instruction readable at arm's length on SHOW (in red, never
    amber); a hint still wins the strip; a warning stays in the status bar (it may last a
    whole gig and the chips say the band)."""
    s = st.State()
    knobs = "KNOBS OFFLINE · USE PHONE MUTE / BYPASS"
    row = (0, pages.CHIP_Y0, pages.W, pages.CHIP_Y1)
    crit = pages.render("SHOW", s, pages.ScreenStatus(alerts=[("critical", knobs)], **RUN), Focus(),
                        now=0.0)
    assert _count(crit, row, pages.RED) > 0.6 * pages.W * (pages.CHIP_Y1 - pages.CHIP_Y0)
    hinted = pages.render("SHOW", s, pages.ScreenStatus(alerts=[("critical", knobs)], **RUN),
                          Focus(hint="MASTER: SLOW DOWN", hint_until=INF), now=0.0)
    assert _count(hinted, row, pages.AMBER) > 5000 and _count(hinted, row, pages.RED) < 50
    warn = pages.render("SHOW", s, pages.ScreenStatus(alerts=[("warning", "DEVICES CHANGED (GUI)")],
                                                      **RUN), Focus(), now=0.0)
    assert _count(warn, row, pages.ORANGE) < 500


def test_risky_pills_are_orange_and_fold_into_plus_n_last():
    """UX-6 / LAS-7: LO-CUT OFF, POL INV and LAPTOP OPEN orange, MONO SUB (Thomas's normal state)
    grey; >= 13 px; MONO SUB folds first, LAPTOP OPEN (first in the controller's order) last."""
    four = ["LAPTOP OPEN 1:59", "LO-CUT OFF", "POL INV SUB", "MONO SUB"]
    shown, folded, size = pages.pill_layout(four, 250)
    assert size >= pages.PILL_MIN_PX and folded[0] == "MONO SUB" and shown[0] == "LAPTOP OPEN 1:59"
    shown, folded, size = pages.pill_layout(four, 200)
    assert shown == ["LAPTOP OPEN 1:59"] and folded == ["MONO SUB", "POL INV SUB", "LO-CUT OFF"]
    assert pages.pill_layout(four, 2000) == (four, [], 14)
    region = (0, pages.PILL_Y0, pages.W, pages.PILL_Y1 + 1)
    s = st.State()
    risky = pages.render("SHOW", s, pages.ScreenStatus(pills=["LO-CUT OFF"], **RUN), Focus())
    calm = pages.render("SHOW", s, pages.ScreenStatus(pills=["MONO SUB"], **RUN), Focus())
    assert _count(risky, region, pages.ORANGE) > 100 and _count(calm, region, pages.ORANGE) == 0
    assert _count(calm, region, pages.PILL_BG) > 100


@pytest.mark.parametrize("field, xs, ys, colour", [
    (im.F_ALIGN_DELAY, (pages.METER_X1 + 20, pages.W - 20), (124, 137), pages.FG),  # E3, under
    (im.F_BALANCE, (20, pages.METER_X0 - 20), (203, 217), pages.DIM),              # E2, above
])
def test_align_edit_box_stops_short_of_the_sub_line(field, xs, ys, colour):
    """UX-9: '≈ 0.81 m' was drawn half over the amber edit box (grey on amber, cut in two).
    The delay's sub-line ('SUBS LATER · 0.81 m') is white while unlocked: it says the side."""
    s = st.State(sub_delay_ms=2.35, balance_db=3.0)
    img = pages.render("ALIGN", s, pages.ScreenStatus(align_locked=False, align_unlock_s=20.0, **RUN),
                       Focus(editing=field, edit_until=INF), now=0.0)
    assert _count(img, (xs[0], ys[0], xs[1], ys[1]), pages.AMBER) == 0
    assert _count(img, (xs[0], ys[0], xs[1], ys[1]), colour) > 20        # the sub-line is there


def test_the_notch_pictures_are_what_the_real_notch_helper_does():
    """UX-3: show_notch was hand-built (P3 set up next to P2 cutting 0.05 oct away, which the
    controller never does). Both pictures now come from Controller.notch_helper."""
    cases = {c[0]: c for c in pages.demo_cases()}
    _n, _p, fresh, _s, f1 = cases["show_notch"]
    assert f1.slot == 2 and f1.editing == "live_peq.2.gain" and f1.hint.startswith("P3 · ")
    assert fresh.live_peq[2].gain == 0.0 and fresh.live_peq[2].q == pytest.approx(8.651, abs=1e-3)
    _n, _p, again, _s, f2 = cases["show_notch_reselect"]
    assert f2.slot == 1 and f2.hint == "P2 · 2.52k · E3 TO CUT"
    assert again.to_dict() == cases["show"][2].to_dict()                 # nothing written


def test_hints_take_the_amber_strip_never_the_status_bar():
    s = st.State()
    for page, probe in (("SHOW", (3, pages.CHIP_Y0 + 3)),
                        ("ALIGN", (3, (pages.MID_Y0 + pages.MID_Y1) // 2)),
                        ("SETUP", (3, pages.H - 5))):
        plain = pages.render(page, s, pages.ScreenStatus(**RUN), Focus(), now=0.0)
        hinted = pages.render(page, s, pages.ScreenStatus(**RUN),
                              Focus(hint="MASTER: SLOW DOWN", hint_until=INF), now=0.0)
        assert (_crop(plain, STATUS_BAR) == _crop(hinted, STATUS_BAR)).all(), page
        assert hinted.getpixel(probe) == pages.AMBER and plain.getpixel(probe) != pages.AMBER, page


def test_screen_alerts_from_a_controller_status_or_an_older_one():
    assert pages.screen_alerts(pages.ScreenStatus(alerts=[("critical", "A"), ("bogus", "B"),
                                                          ("info", ""), "C", (1,)], **RUN)) == \
        [("critical", "A"), ("warning", "B"), ("warning", "C")]
    old = pages.ScreenStatus(alert="KNOBS OFFLINE · X", message="KNOBS OFFLINE · X", **RUN)
    assert pages.screen_alerts(old) == [("critical", "KNOBS OFFLINE · X")]
    old = pages.ScreenStatus(alert="CANNOT SAVE", message="DRIFT", **RUN)
    assert pages.screen_alerts(old) == [("warning", "CANNOT SAVE"), ("warning", "DRIFT")]
    assert pages.screen_alerts(pages.ScreenStatus(alerts=[], **RUN)) == []
    assert pages.screen_alerts(pages.ScreenStatus(alerts=[]))[0][1].startswith("STATUS ?")


def test_align_locks_e3_e4_and_counts_the_unlock_down():
    s = st.State(sub_delay_ms=2.35, lowcut_hz=112.0)
    locked = pages.render("ALIGN", s, pages.ScreenStatus(**RUN), Focus(), now=0.0)
    by_status = pages.render("ALIGN", s, pages.ScreenStatus(align_locked=False, align_unlock_s=27.4,
                                                            **RUN), Focus(), now=0.0)
    by_focus = pages.render("ALIGN", s, pages.ScreenStatus(**RUN), Focus(unlock_until=100.0),
                            now=72.6)
    e3 = (pages.METER_X1 + 4, 60, pages.W - 4, 122)          # SUB DELAY value
    e4 = (pages.METER_X1 + 4, 222, pages.W - 4, 284)         # LO-CUT value
    badge = (pages.METER_X1 + 4, 148, pages.W - 4, 186)
    for box in (e3, e4):
        assert _count(locked, box, pages.FG) == 0 and _count(locked, box, pages.DIM) > 200
        assert _count(by_status, box, pages.FG) > 200
    assert _count(locked, badge, pages.AMBER) == 0 and _count(locked, badge, pages.DIM) > 100
    assert _count(by_status, badge, pages.AMBER) > 1000                        # EDIT 0:28
    assert (_crop(by_status, badge) == _crop(by_focus, badge)).all()
    # master and balance are live either way: never dimmed
    for img in (locked, by_status):
        assert _count(img, (8, 60, pages.METER_X0 - 4, 122), pages.FG) > 200
        assert _count(img, (8, 222, pages.METER_X0 - 4, 284), pages.FG) > 200
    # the status saying locked wins over a stale focus deadline in the past
    assert pages.align_unlock_left(pages._Ctx(Focus(unlock_until=5.0), 10.0),
                                   pages.ScreenStatus(align_locked=True)) == 0.0


def test_setup_shows_the_v1_items_and_their_state():
    items = {it.field: it for it in im.SETUP_ITEMS}
    s = st.State()
    v = pages._setup_value
    assert v(s, items[im.A_RESET_LIVE], pages.ScreenStatus()) == "PUSH ×2"
    assert v(s, items[im.A_UNDO_RESET], pages.ScreenStatus()) == "—"
    assert v(s, items[im.A_UNDO_RESET], pages.ScreenStatus(undo_reset_s=299.2)) == "UNDO 5:00"
    assert v(s, items[im.A_UNDO_RESET], pages.ScreenStatus(undo_reset_s=0.4)) == "UNDO 0:01"
    assert v(s, items[im.F_LOWCUT_ON], None) == "ON"
    assert v(st.State(lowcut_on=False), items[im.F_LOWCUT_ON], None) == "off"
    assert v(st.State(spectrum_on=False), items[im.F_SPECTRUM_ON], None) == "off"
    assert v(s, items[im.F_SPECTRUM_ON], pages.ScreenStatus(spectrum_state="disabled")) == "DISABLED"
    # the undo row is dimmed while there is nothing to undo
    i = _item(im.A_UNDO_RESET)
    f = Focus(item=i - 1)
    rows, row_h, y0 = 8, 28, pages.STATUS_H + 30
    first = max(0, min(f.item - rows // 2, len(im.SETUP_ITEMS) - rows))
    y = y0 + (i - first) * row_h + row_h // 2 - 1
    none = pages.render("SETUP", s, pages.ScreenStatus(**RUN), f, now=0.0)
    some = pages.render("SETUP", s, pages.ScreenStatus(undo_reset_s=200.0, **RUN), f, now=0.0)
    lab = (10, y - 10, 160, y + 10)
    assert _count(none, lab, pages.FG) == 0 and _count(some, lab, pages.FG) > 30


@pytest.mark.parametrize("frame", [
    None, object(), "spectrum", {"freqs": [100.0, 200.0], "level": [-20.0, -30.0]},
    eqgraph.RtaFrame(np.array([100.0, 200.0, 300.0]), np.array([-20.0, -30.0])),          # lengths
    eqgraph.RtaFrame(np.array([300.0, 200.0, 100.0]), np.array([-20.0, -30.0, -40.0])),   # order
    eqgraph.RtaFrame(np.array([100.0]), np.array([-20.0])),                              # one bin
    eqgraph.RtaFrame(np.array([0.0, 200.0]), np.array([-20.0, -30.0])),                  # 0 Hz
    eqgraph.RtaFrame(np.array([100.0, 200.0]), np.array([np.nan, np.inf]), hold=np.zeros(5),
                     lo_db=-90.0, hi_db=-30.0, peak_hz=float("nan")),
    eqgraph.RtaFrame(np.array([100.0, 200.0]), np.array([-20.0, -30.0]), lo_db=0.0, hi_db=0.0),
    eqgraph.RtaFrame(np.geomspace(10, 20000, 264), np.full(264, -140.0), peak_hz=-5.0),
])
def test_show_survives_any_spectrum_frame(frame):
    img = pages.render("SHOW", st.State(), pages.ScreenStatus(spectrum=frame, **RUN), Focus(), now=0.0)
    _check(img)
    assert img.getpixel((8, pages.STATUS_H // 2)) == pages.FG          # the page pill, not UI ERROR


def test_spectrum_frame_validation():
    good = pages.demo_spectrum()
    out = pages.spectrum_frame(pages.ScreenStatus(spectrum=good))
    assert out is not None and np.array_equal(out.level, good.level) and out.peak_hz == good.peak_hz
    assert pages.spectrum_frame(pages.ScreenStatus(spectrum=good, spectrum_on=False)) is None
    nan = eqgraph.RtaFrame(np.array([100.0, 200.0]), np.array([np.nan, -30.0]), hold=np.zeros(3))
    out = pages.spectrum_frame(pages.ScreenStatus(spectrum=nan))
    assert out is not None and out.level[0] == -140.0 and out.hold is None


def test_show_says_when_the_engine_gives_no_spectrum():
    corner = (pages.W - 110, pages.GRAPH[1] + 2, pages.W - 4, pages.GRAPH[1] + 18)
    for state, shown in (("unavailable", True), ("waiting", False), ("off", False)):
        img = pages.render("SHOW", st.State(), pages.ScreenStatus(spectrum_state=state, **RUN),
                           Focus(), now=0.0)
        assert (_count(img, corner, pages.DIM) > 20) is shown, state


def test_fmt_helpers():
    assert pages.fmt_oct(8.65) == "1/6 oct" and pages.fmt_oct(4.32) == "1/3 oct"
    assert pages.fmt_oct(1.41) == "1.0 oct" and pages.fmt_oct(None) == ""
    assert pages.fmt_mmss(119.2) == "2:00" and pages.fmt_mmss(0.0) == "0:00"
    assert pages.fmt_mmss(None) == "0:00" and pages.fmt_mmss(61) == "1:01"


@pytest.mark.skipif(platform.machine() not in ("aarch64", "armv7l"), reason="budget is for the Pi")
def test_show_with_a_moving_spectrum_fits_10_fps_on_pi():
    """SHOW at 10 fps with the spectrum on: a fresh frame every time (ballistics, render,
    RGB565) must stay well under the 100 ms period (docs/07 §6 measured ~12 ms)."""
    from k1000.rta import Ballistics
    bal = Ballistics()
    first = pages.demo_spectrum(seconds=2.0)
    f, base = first.freqs, np.array(first.level)
    rng = np.random.default_rng(3)
    s, foc = st.State(), Focus(slot=1)
    times = []
    for i in range(40):
        t0 = time.perf_counter()
        bal.update(f, base + rng.normal(0, 2.0, len(base)), i * 0.1)
        img = pages.render("SHOW", s, pages.ScreenStatus(spectrum=bal.frame(), spectrum_state="live",
                                                         **RUN), foc, now=0.0)
        display.to_rgb565(img)
        times.append((time.perf_counter() - t0) * 1000)
    med = statistics.median(times[5:])
    print(f"SHOW + spectrum frame median {med:.1f} ms")
    assert med < 30.0


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
def _item(field: str) -> int:
    return next(i for i, it in enumerate(im.SETUP_ITEMS) if it.field == field)


UNLOCKED = math.inf          # Focus(unlock_until=UNLOCKED): ALIGN's E3/E4 editable


def test_field_names_exist_in_state():
    """Every field the knobs can emit is a real k1000.state path (numeric, bool or choice)."""
    s = st.State()
    fields = [k.field for p in ("SHOW", "ALIGN") for k in im.KNOBS[p]
              if k.field and "{slot}" not in k.field]
    fields += [im.live_field(i, p) for i in range(len(im.LIVE_SLOTS)) for p in ("freq", "gain", "q")]
    for f in fields:
        s.get(f)
        assert st.range_for(f) is not None, f
    assert im.F_LOWCUT_ON in st.BOOL_FIELDS and im.F_SPECTRUM_ON in st.BOOL_FIELDS
    for item in im.SETUP_ITEMS:
        if item.kind == "page":
            assert item.field in im.ALL_PAGES
            continue
        if item.kind == "action":
            assert item.field in (im.A_RESET_LIVE, im.A_UNDO_RESET, im.A_LAPTOP)
            continue
        s.get(item.field)
        if item.kind == "num":
            assert st.range_for(item.field) is not None, item.field
        elif item.kind == "bool":
            assert item.field in st.BOOL_FIELDS
        else:
            assert item.field in st.REBUILD_FIELDS


def test_setup_list_is_d009s():
    """v0's list + Reset live EQ, Undo reset, Lo-Cut, Spectrum, then Phone and the Knob test
    last; what touches the sound in one push needs a confirm. No Top delay since D-011: ALIGN's
    signed E3 is the one place to set the alignment."""
    labels = [i.label for i in im.SETUP_ITEMS]
    for want in ("Input trim", "Mono subs", "Reset live EQ", "Undo reset", "Lo-Cut",
                 "Spectrum", "Laptop access", "Phone", "Knob test"):
        assert want in labels, want
    assert "Top delay" not in labels and len(im.SETUP_ITEMS) == 19
    assert not any(i.field.endswith("_delay_ms") for i in im.SETUP_ITEMS)
    assert im.SETUP_ITEMS[-1].field == "HWTEST" and im.SETUP_ITEMS[0].field == "input_trim_db"
    assert [i.field for i in im.SETUP_ITEMS[-3:]] == [im.A_LAPTOP, "PHONE", "HWTEST"]
    by = {i.field: i for i in im.SETUP_ITEMS}
    for f in ("reset_live", "lowcut_on", "mono_subs", "dev_audition", "profile", "laptop_access"):
        assert by[f].needs_confirm, f
    for f in ("undo_reset", "spectrum_on", "sub_inverted", "PHONE"):
        assert not by[f].needs_confirm, f


def test_pages_and_v0_aliases():
    assert im.PAGES == ("SHOW", "ALIGN", "SETUP") and im.HOME_PAGE == "SHOW"
    assert set(im.ALL_PAGES) == {"SHOW", "ALIGN", "SETUP", "HWTEST", "PHONE"}
    assert im.page_name("MAIN") == "ALIGN" and im.page_name("LIVE") == "SHOW"
    assert im.page_name("SETUP") == "SETUP" and im.page_name(None) is None
    assert map_event("MAIN", Focus(), Enc(1, 1)) == [Nudge("balance_db", 1, False)]
    assert map_event("LIVE", Focus(), Enc(2, 1)) == [Nudge("live_peq.0.gain", 1, False)]
    assert im.knob_labels("LIVE") == im.knob_labels("SHOW") == ["MASTER", "FREQ", "GAIN", "Q"]
    assert im.knob_labels("ALIGN") == ["MASTER", "BALANCE", "DELAY", "LO-CUT"]


def test_show_knobs_follow_the_selected_band():
    f = Focus()
    assert map_event("SHOW", f, Enc(0, 1, ts=0.0)) == [Nudge("master_db", 1, False)]
    assert map_event("SHOW", f, Enc(1, 1, ts=1.0)) == [Nudge("live_peq.0.freq", 1, False)]
    assert map_event("SHOW", f, Enc(2, -1, ts=2.0)) == [Nudge("live_peq.0.gain", -1, False)]
    assert map_event("SHOW", f, Enc(3, 1, ts=3.0)) == [Nudge("live_peq.0.q", 1, False)]
    f.slot = 4
    assert map_event("SHOW", f, Enc(1, 1, ts=4.0)) == [Nudge("live_lowshelf.freq", 1, False)]
    assert map_event("SHOW", f, Sw(1, "press")) == []             # E2 push = fine frequency
    assert map_event("SHOW", f, Enc(1, -3, ts=5.0)) == [Nudge("live_lowshelf.freq", -3, True)]
    assert map_event("SHOW", f, Sw(1, "short")) == []             # a push never selects on SHOW
    for n in range(4):                                            # not even without a turn
        assert map_event("SHOW", f, Sw(n, "press")) == [] and map_event("SHOW", f, Sw(n, "short")) == []
    assert map_event("SHOW", f, Enc(0, 0, ts=6.0)) == []
    f.slot = 101                                                  # tolerant: wraps (101 % 6 = HS)
    assert map_event("SHOW", f, Enc(2, 1, ts=7.0)) == [Nudge("live_highshelf.gain", 1, False)]


def test_b3_on_show_cycles_the_bands_low_to_high_and_wraps():
    f = Focus(slot=4)                                             # LS
    seen = []
    for k in range(7):
        acts = map_event("SHOW", f, Sw(6, "short", ts=float(k)))
        assert acts == [SelectBand(1)]
        assert apply_focus(f, acts[0], now=float(k)) is True
        seen.append(im.LIVE_SLOT_SHORT[f.slot])
    assert seen == ["P1", "P2", "P3", "P4", "HS", "LS", "P1"]
    assert f.editing_now(6.1) == im.F_SLOT
    assert map_event("SHOW", f, Sw(6, "long")) == []              # no 'previous band'
    assert map_event("SHOW", f, Sw(6, "press")) == [] and map_event("SHOW", f, Sw(6, "release")) == []
    assert im.next_band(5) == 4 and im.next_band(4, -1) == 5 and im.next_band(99) in im.BAND_CYCLE
    apply_focus(f, SelectSlot(-1), now=0.0)                       # v0 action still wraps
    assert f.slot == 5


def test_b4_on_show_is_the_notch_helper_never_a_reset():
    f = Focus()
    assert map_event("SHOW", f, Sw(7, "short")) == [NotchHelper()]
    assert apply_focus(f, NotchHelper()) is False                 # the controller acts on it
    for _ in range(3):                                            # holds never arm anything
        assert map_event("SHOW", f, Sw(7, "long")) == [ShowHint(im.HINT_RESET_IN_SETUP)]
    assert f.confirm is None
    for page in ("ALIGN", "SETUP", "PHONE"):
        acts = [a for k in ("press", "short", "long", "release") for a in map_event(page, Focus(), Sw(7, k))]
        assert acts == [ShowHint(im.HINT_B4_OFF_SHOW)], page


def test_align_master_and_balance_are_live_e3_e4_locked():
    f = Focus()
    assert map_event("ALIGN", f, Enc(0, 1, ts=10.0)) == [Nudge("master_db", 1, False)]
    assert map_event("ALIGN", f, Enc(1, -1, ts=11.0)) == [Nudge("balance_db", -1, False)]
    for e in (2, 3):
        for d in (1, -3):
            assert map_event("ALIGN", f, Enc(e, d, ts=12.0)) == [ShowHint(im.HINT_LOCKED)]
    map_event("ALIGN", f, Sw(2, "press"))                         # FINE held: still locked
    assert map_event("ALIGN", f, Enc(2, 1, ts=12.5)) == [ShowHint(im.HINT_LOCKED)]
    map_event("ALIGN", f, Sw(2, "release"))
    assert 2 not in f.last_turn and 3 not in f.last_turn          # a locked turn records nothing
    assert map_event("ALIGN", f, Sw(6, "short", ts=13.0)) == [ShowHint(im.HINT_LOCKED)]
    assert not f.unlocked(13.0) and f.unlock_left(13.0) == 0.0


def test_b3_hold_unlocks_align_until_30_s_of_idle():
    f = Focus()
    acts = map_event("ALIGN", f, Sw(6, "long", ts=100.0))
    assert acts == [Unlock(im.UNLOCK_S)] and im.UNLOCK_S == 30.0
    assert apply_focus(f, acts[0], now=100.0) is True
    assert f.unlocked(100.0) and f.hint_now(100.1) == im.HINT_UNLOCKED
    assert f.unlock_left(110.0) == pytest.approx(20.0)
    # the delay keeps v0's wake-up detent, then edits; Lo-Cut edits at once
    assert map_event("ALIGN", f, Enc(2, 1, ts=101.0)) == [ShowHint(im.HINT_ARM.format("DELAY"))]
    assert map_event("ALIGN", f, Enc(2, 1, ts=101.5)) == [Nudge("align_delay_ms", 1, False)]
    assert map_event("ALIGN", f, Enc(3, -1, ts=102.0)) == [Nudge("lowcut_hz", -1, False)]
    # every input on ALIGN renews the 30 s; any 30 s of idle relocks
    map_event("ALIGN", f, Enc(0, -1, ts=125.0))                   # master: renewed to 155
    assert map_event("ALIGN", f, Enc(3, 1, ts=150.0)) == [Nudge("lowcut_hz", 1, False)]
    assert map_event("ALIGN", f, Enc(3, 1, ts=180.5)) == [ShowHint(im.HINT_LOCKED)]
    assert not f.unlocked(180.5)
    # a B3 tap while unlocked locks at once; a hold while unlocked renews
    apply_focus(f, Unlock(30.0), now=200.0)
    assert map_event("ALIGN", f, Sw(6, "long", ts=205.0)) == [Unlock(im.UNLOCK_S)]
    acts = map_event("ALIGN", f, Sw(6, "short", ts=206.0))
    assert acts == [Lock()]
    assert apply_focus(f, acts[0], now=206.0) is True
    assert not f.unlocked(206.0) and f.hint_now(206.1) == im.HINT_RELOCKED
    assert map_event("ALIGN", f, Enc(3, 1, ts=207.0)) == [ShowHint(im.HINT_LOCKED)]
    # only ALIGN unlocks anything
    for page in ("SHOW", "SETUP", "PHONE"):
        assert not any(isinstance(a, Unlock) for a in map_event(page, Focus(), Sw(6, "long")))


def test_align_knobs_when_unlocked():
    f = Focus(unlock_until=UNLOCKED)
    assert map_event("ALIGN", f, Enc(0, 1, ts=10.0)) == [Nudge("master_db", 1, False)]
    assert map_event("ALIGN", f, Enc(1, -1, ts=11.0)) == [Nudge("balance_db", -1, False)]
    arm = map_event("ALIGN", f, Enc(2, 2, ts=12.0))               # the delay: wakes up first
    assert arm == [ShowHint(im.HINT_ARM.format("DELAY"))] and f.editing_now(12.1) == "align_delay_ms"
    assert map_event("ALIGN", f, Enc(2, 2, ts=12.5)) == [Nudge("align_delay_ms", 2, False)]
    assert map_event("ALIGN", f, Enc(3, -1, ts=13.0)) == [Nudge("lowcut_hz", -1, False)]
    assert map_event("ALIGN", f, Enc(0, 0, ts=14.0)) == []


def test_sub_delay_brushed_once_does_not_change_it():
    """Every settled delay change dips the subs or the tops (delay guard): even unlocked, one
    brushed detent must not. A detent after ARM_WINDOW_S of rest only wakes the knob again."""
    f = Focus(unlock_until=UNLOCKED)
    assert not any(isinstance(a, Nudge) for a in map_event("ALIGN", f, Enc(2, 1, ts=100.0)))
    assert map_event("ALIGN", f, Enc(2, 1, ts=101.0)) == [Nudge("align_delay_ms", 1, False)]
    assert map_event("ALIGN", f, Enc(2, -1, ts=103.0)) == [Nudge("align_delay_ms", -1, False)]
    later = 103.0 + im.ARM_WINDOW_S + 0.1                         # at rest again: wake first
    assert not any(isinstance(a, Nudge) for a in map_event("ALIGN", f, Enc(2, 1, ts=later)))
    # the other knobs act on their first detent
    assert map_event("ALIGN", Focus(), Enc(1, 1, ts=0.0)) == [Nudge("balance_db", 1, False)]
    assert map_event("SHOW", Focus(), Enc(2, 1, ts=0.0)) == [Nudge("live_peq.0.gain", 1, False)]


def test_fine_while_push_held():
    f = Focus(unlock_until=UNLOCKED)
    assert map_event("ALIGN", f, Sw(2, "press")) == []            # sw2 = E3 push
    assert map_event("ALIGN", f, Enc(2, 1, ts=1.0)) == [Nudge("align_delay_ms", 1, True)]  # deliberate
    assert map_event("ALIGN", f, Sw(2, "long")) == []             # still held
    assert map_event("ALIGN", f, Enc(2, 1, ts=1.01)) == [Nudge("align_delay_ms", 1, True)]  # no accel
    map_event("ALIGN", f, Sw(2, "release"))
    assert map_event("ALIGN", f, Enc(2, 1, ts=2.0)) == [Nudge("align_delay_ms", 1, False)]
    assert map_event("ALIGN", f, Enc(2, 1, ts=5.0)) == [ShowHint(im.HINT_ARM.format("DELAY"))]


def test_acceleration_master_down_only_and_frequency():
    f = Focus(unlock_until=UNLOCKED)
    for k in range(5):                                            # master up: never accelerated
        assert map_event("ALIGN", f, Enc(0, 1, ts=k * 0.01)) == [Nudge("master_db", 1, False)]
    assert map_event("ALIGN", f, Enc(0, -1, ts=0.05)) == [Nudge("master_db", -1, False)]  # reversal
    assert map_event("ALIGN", f, Enc(0, -1, ts=0.06)) == [Nudge("master_db", -1, False)]  # 1 interval
    assert map_event("ALIGN", f, Enc(0, -1, ts=0.07)) == [Nudge("master_db", -4, False)]  # down: x4
    assert map_event("ALIGN", f, Enc(0, -1, ts=0.10)) == [Nudge("master_db", -3, False)]  # 33 det/s
    assert map_event("ALIGN", f, Enc(0, -1, ts=0.14)) == [Nudge("master_db", -1, False)]  # 25 det/s
    assert map_event("ALIGN", f, Enc(0, -1, ts=1.0)) == [Nudge("master_db", -1, False)]   # pause
    map_event("ALIGN", f, Enc(3, 1, ts=2.0))
    assert map_event("ALIGN", f, Enc(3, 1, ts=2.01)) == [Nudge("lowcut_hz", 1, False)]
    assert map_event("ALIGN", f, Enc(3, 1, ts=2.02)) == [Nudge("lowcut_hz", 4, False)]
    assert map_event("ALIGN", f, Enc(3, 3, ts=2.07)) == [Nudge("lowcut_hz", 12, False)]  # coalesced
    map_event("ALIGN", f, Enc(2, 1, ts=3.0))                      # the delay: never accelerated
    assert map_event("ALIGN", f, Enc(2, 1, ts=3.01)) == [Nudge("align_delay_ms", 1, False)]
    map_event("ALIGN", f, Enc(1, 1, ts=4.0))                      # balance: never accelerated
    assert map_event("ALIGN", f, Enc(1, 1, ts=4.005)) == [Nudge("balance_db", 1, False)]
    g = Focus()                                                   # SHOW: FREQ accelerates, GAIN/Q never
    map_event("SHOW", g, Enc(1, 1, ts=0.0))
    map_event("SHOW", g, Enc(1, 1, ts=0.01))
    assert map_event("SHOW", g, Enc(1, 1, ts=0.02)) == [Nudge("live_peq.0.freq", 4, False)]
    for e, p in ((2, "gain"), (3, "q")):
        acts = [map_event("SHOW", g, Enc(e, -1, ts=1.0 + e + k * 0.01)) for k in range(6)]
        assert acts == [[Nudge(f"live_peq.0.{p}", -1, False)]] * 6, p


def test_master_up_is_never_accelerated_on_any_page(monkeypatch):
    """D-009 master-up guard, acceleration side: every page whose knob drives master_db (SHOW
    and ALIGN) gives one detent per click going up, however fast the turn, even if its Knob
    row asks for more; going down still accelerates where the row allows."""
    spin = [k * 0.01 for k in range(12)]                       # 100 det/s: far above the onset
    pages_with_master = [(p, e) for p, row in im.KNOBS.items() for e, k in enumerate(row)
                         if k.field == im.F_MASTER]
    assert sorted(p for p, _e in pages_with_master) == ["ALIGN", "SHOW"]
    for page, e in pages_with_master:
        f = Focus()
        got = [map_event(page, f, Enc(e, 1, ts=t)) for t in spin]
        assert got == [[Nudge(im.F_MASTER, 1, False)]] * len(spin), page
        if im.KNOBS[page][e].accel_down > 1:
            down = [map_event(page, f, Enc(e, -1, ts=1.0 + t)) for t in spin]
            assert max(-a[0].detents for a in down) > 1, page        # down: still quick
    # a future row that forgets the rule (accel_up=4) is still held to x1 going up
    monkeypatch.setitem(im.KNOBS, "SHOW", (im.Knob("MASTER", im.F_MASTER, accel_up=4, accel_down=4),)
                        + im.KNOBS["SHOW"][1:])
    f = Focus()
    assert [map_event("SHOW", f, Enc(0, 1, ts=t)) for t in spin] == \
        [[Nudge(im.F_MASTER, 1, False)]] * len(spin)
    assert map_event("SHOW", f, Enc(0, 3, ts=0.2)) == [Nudge(im.F_MASTER, 3, False)]  # merged: as is


def test_acceleration_ignores_a_single_quick_interval():
    """docs/08: one short detent interval (a flick, sampling jitter) in a normal turn is x1;
    a brisk 15 det/s turn of a 24-click knob (v0 accelerated from 12) stays x1."""
    f = Focus()
    for k, t in enumerate([1.0, 1.2, 1.4, 1.41, 1.6]):
        assert map_event("SHOW", f, Enc(1, 1, ts=t)) == [Nudge("live_peq.0.freq", 1, False)], k
    for k in range(8):
        assert map_event("SHOW", f, Enc(1, 1, ts=3.0 + k / 15)) == \
            [Nudge("live_peq.0.freq", 1, False)], k


def test_b1_press_next_page_hold_home():
    for page in ("SHOW", "ALIGN", "SETUP", "PHONE"):
        f = Focus()
        assert map_event(page, f, Sw(4, "press")) == [NextPage()], page
        assert map_event(page, f, Sw(4, "long")) == [GotoPage("SHOW")], page
        assert map_event(page, f, Sw(4, "short")) == [] and map_event(page, f, Sw(4, "release")) == []
    assert map_event("HWTEST", Focus(), Sw(4, "long")) == [GotoPage("SETUP")]   # the test's own exit


def test_buttons():
    f = Focus()
    assert map_event("SHOW", f, Sw(5, "press")) == []
    assert map_event("SHOW", f, Sw(5, "short")) == [ShowHint(im.HINT_MUTE)]
    assert map_event("SHOW", f, Sw(5, "long")) == [Mute("toggle")]
    for page in ("ALIGN", "SETUP"):                               # mute works everywhere
        assert map_event(page, Focus(), Sw(5, "long")) == [Mute("toggle")]
    for page in ("SETUP", "PHONE"):
        assert map_event(page, Focus(), Sw(6, "short")) == [ShowHint(im.HINT_B3_OFF_SHOW)]
        assert map_event(page, Focus(), Sw(6, "long")) == []


@pytest.mark.parametrize("page", im.ALL_PAGES)
def test_no_button_switches_the_lo_cut_or_resets_any_more(page):
    """D-009: Lo-Cut on/off and the live EQ reset live in SETUP (confirmed items) only. Tapping
    buttons in the dark to find the mute changes neither, on any page."""
    for sw in range(4, 8):
        for kind in ("press", "short", "long", "release"):
            for a in map_event(page, Focus(unlock_until=UNLOCKED), Sw(sw, kind)):
                assert not isinstance(a, (Toggle, ResetLive, UndoReset)), (page, sw, kind, a)


def test_setup_reset_live_needs_two_pushes_and_undo_one():
    idx = _item("reset_live")
    f = Focus(item=idx)
    assert map_event("SETUP", f, Sw(0, "short", ts=10.0)) == [ShowHint(im.HINT_CONFIRM_RESET)]
    assert f.confirm_now(10.5) == "reset_live"
    assert map_event("SETUP", f, Sw(1, "short", ts=11.0)) == [ResetLive()]   # E1 or E2 push
    assert f.confirm is None
    map_event("SETUP", f, Sw(0, "short", ts=20.0))                # an expired confirm arms again
    assert map_event("SETUP", f, Sw(0, "short", ts=30.0)) == [ShowHint(im.HINT_CONFIRM_RESET)]
    apply_focus(f, SelectItem(1))                                 # moving cancels
    assert f.confirm is None and im.SETUP_ITEMS[f.item].field == "undo_reset"
    assert map_event("SETUP", f, Sw(0, "short", ts=40.0)) == [UndoReset()]   # one push
    assert map_event("SETUP", f, Enc(1, 1)) == [ShowHint(im.HINT_PUSH_TOGGLE)]
    # a confirm of one item never confirms another
    g = Focus(item=idx)
    map_event("SETUP", g, Sw(0, "short", ts=50.0))
    g.item = _item("mono_subs")
    assert map_event("SETUP", g, Sw(0, "short", ts=51.0)) == [ShowHint(im.HINT_CONFIRM)]


def test_setup_laptop_access_opens_on_a_double_push_and_closes_on_one():
    """SETUP > Laptop access emits SetupAction("laptop_access") after its double push (the
    opening); while the Controller marks it open (Focus.no_confirm) one push does (the closing,
    the safe direction). A generic action item still gets the generic confirm."""
    f = Focus(item=_item(im.A_LAPTOP))
    assert map_event("SETUP", f, Sw(0, "short", ts=1.0)) == [ShowHint(im.HINT_CONFIRM_LAPTOP)]
    assert f.confirm == im.A_LAPTOP
    assert map_event("SETUP", f, Sw(0, "short", ts=2.0)) == [im.SetupAction(im.A_LAPTOP)]
    f.no_confirm.add(im.A_LAPTOP)                                  # open: one push closes
    assert map_event("SETUP", f, Sw(0, "short", ts=3.0)) == [im.SetupAction(im.A_LAPTOP)]
    assert f.confirm is None
    f.no_confirm.discard(im.A_LAPTOP)
    assert map_event("SETUP", f, Sw(1, "short", ts=4.0)) == [ShowHint(im.HINT_CONFIRM_LAPTOP)]
    assert map_event("SETUP", f, Sw(0, "short", ts=10.0)) == [ShowHint(im.HINT_CONFIRM_LAPTOP)]


def test_setup_action_items_beyond_reset_and_undo(monkeypatch):
    """A later SETUP action only needs its SetupItem: input_map emits SetupAction(name), after
    its confirm when it has one; a waived confirm (no_confirm) is per item."""
    extra = im.SetupItem("Other", "other_action", "action", confirm=True)
    monkeypatch.setattr(im, "SETUP_ITEMS", im.SETUP_ITEMS[:-2] + (extra,) + im.SETUP_ITEMS[-2:])
    f = Focus(item=_item("other_action"), no_confirm={im.A_LAPTOP})
    assert map_event("SETUP", f, Sw(0, "short", ts=1.0)) == [ShowHint(im.HINT_CONFIRM)]
    assert map_event("SETUP", f, Sw(0, "short", ts=2.0)) == [im.SetupAction("other_action")]


def test_setup_lo_cut_needs_a_confirm_spectrum_does_not():
    f = Focus(item=_item("lowcut_on"))
    assert map_event("SETUP", f, Sw(0, "short", ts=1.0)) == [ShowHint(im.HINT_CONFIRM_LOWCUT)]
    assert map_event("SETUP", f, Sw(0, "short", ts=2.0)) == [Toggle("lowcut_on")]
    assert map_event("SETUP", f, Enc(1, 1)) == [ShowHint(im.HINT_PUSH_TOGGLE)]   # a turn never toggles
    g = Focus(item=_item("spectrum_on"))
    assert map_event("SETUP", g, Sw(0, "short", ts=1.0)) == [Toggle("spectrum_on")]


def test_setup_push_after_a_fine_turn_is_not_a_select():
    """The SHOW fine-mode habit (hold a push, turn, release fast = 'short') must not flip the
    selected SETUP item (a polarity) or open a page."""
    f = Focus(item=_item("sub_inverted"))
    assert map_event("SETUP", f, Sw(1, "press", ts=1.0)) == []
    map_event("SETUP", f, Enc(1, 1, ts=1.1))
    assert map_event("SETUP", f, Sw(1, "short", ts=1.3)) == []          # no Toggle
    assert map_event("SETUP", f, Sw(1, "press", ts=2.0)) == []          # a plain push still selects
    assert map_event("SETUP", f, Sw(1, "short", ts=2.1)) == [Toggle("sub_inverted")]


def test_slot_and_item_follow_every_detent():
    """A fast turn reaches the controller as one merged event: every detent counts."""
    assert map_event("SETUP", Focus(), Enc(0, -2)) == [SelectItem(-2)]
    assert map_event("SETUP", Focus(), Enc(0, 3)) == [SelectItem(3)]
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
    f = Focus(unlock_until=UNLOCKED)
    assert map_event("ALIGN", f, Enc(3, 1, ts=0.0), cfg) == [Nudge("master_db", -1, False)]
    assert map_event("ALIGN", f, Enc(0, 1, ts=5.0), cfg) == [Nudge("lowcut_hz", 1, False)]
    assert map_event("ALIGN", f, Sw(0, "press"), cfg) == [NextPage()]
    assert map_event("ALIGN", f, Sw(4, "press"), cfg) == []          # now E1 push
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
    for a in (NextPage(), Mute(), ResetLive(), UndoReset(), NotchHelper(), GotoPage("HWTEST")):
        assert apply_focus(f, a) is False


def test_knob_corner_table_is_a_permutation():
    assert sorted(im.KNOB_CORNERS) == ["bl", "br", "tl", "tr"]
    for page in im.ALL_PAGES:
        assert len(im.KNOBS[page]) == 4


# --- D-011: the signed ALIGN delay, the knob tags --------------------------------------------
@pytest.mark.parametrize("v,want", [
    (1.25, ("1.25", "SUBS LATER · 0.43 m")),
    (-1.25, ("1.25", "TOPS LATER · 0.43 m")),
    (2.35, ("2.35", "SUBS LATER · 0.81 m")),
    (-50.0, ("50.00", "TOPS LATER · 17.15 m")),
    (st.SAMPLE_MS, ("0.02", "SUBS LATER · 0.01 m")),
    (0.0, ("0.00", "ALIGNED")),
    (-0.0, ("0.00", "ALIGNED")),
    (None, ("--", "")),
])
def test_align_delay_display_says_the_side_in_words(v, want):
    assert pages.fmt_align_delay(v) == want
    if v is not None:                                          # never a bare minus sign
        assert "-" not in want[0] and "−" not in want[0]


def test_align_delay_reads_the_two_stored_delays_tolerantly():
    assert pages.align_delay(st.State(sub_delay_ms=1.25)) == 1.25
    assert pages.align_delay(st.State(top_delay_ms=1.25)) == -1.25
    assert pages.align_delay(st.State(sub_delay_ms=1.0, top_delay_ms=0.25)) == 0.75   # net value
    assert pages.align_delay(None) is None and pages.align_delay(object()) is None
    assert pages.align_delay({"sub_delay_ms": 2.0}) == 2.0
    assert pages.align_delay({"sub_delay_ms": "x", "top_delay_ms": 0.5}) == -0.5


def test_align_e3_shows_size_big_and_side_in_words():
    """Subs 1.25 ms later and tops 1.25 ms later draw the same big value; only the sub-line
    (white while unlocked, grey while locked) tells them apart."""
    on = pages.ScreenStatus(align_locked=False, align_unlock_s=20.0, **RUN)
    value = (pages.METER_X1 + 4, 60, pages.W - 4, 122)
    subline = (pages.METER_X1 + 4, 123, pages.W - 4, 138)
    subs = pages.render("ALIGN", st.State(sub_delay_ms=1.25), on, Focus(), now=0.0)
    tops = pages.render("ALIGN", st.State(top_delay_ms=1.25), on, Focus(), now=0.0)
    zero = pages.render("ALIGN", st.State(), on, Focus(), now=0.0)
    assert (_crop(subs, value) == _crop(tops, value)).all()
    assert not (_crop(subs, subline) == _crop(tops, subline)).all()
    assert not (_crop(zero, subline) == _crop(subs, subline)).all()
    assert _count(subs, subline, pages.FG) > 20 and _count(tops, subline, pages.FG) > 20
    locked = pages.render("ALIGN", st.State(top_delay_ms=1.25), pages.ScreenStatus(**RUN), Focus(),
                          now=0.0)
    assert _count(locked, subline, pages.FG) == 0 and _count(locked, subline, pages.DIM) > 20


def test_align_e3_is_the_signed_delay_knob():
    k = im.KNOBS["ALIGN"][2]
    assert (k.label, k.field, k.arm, k.locked, k.accel_up, k.accel_down) == \
        ("DELAY", im.F_ALIGN_DELAY, True, True, 1, 1)
    assert im.F_ALIGN_DELAY == st.ALIGN_DELAY
    lc = im.KNOBS["ALIGN"][3]
    assert (lc.field, lc.locked, lc.accel_up, lc.accel_down) == (im.F_LOWCUT_FREQ, True, 4, 4)
    assert im.HINT_UNLOCKED == "DELAY + LO-CUT UNLOCKED" and im.HINT_RELOCKED == "DELAY + LO-CUT LOCKED"
    f = Focus()                                                   # locked: nothing, as before
    assert map_event("ALIGN", f, Enc(2, -1, ts=1.0)) == [ShowHint(im.HINT_LOCKED)]
    f = Focus(unlock_until=UNLOCKED)
    assert map_event("ALIGN", f, Enc(2, -1, ts=1.0)) == [ShowHint(im.HINT_ARM.format("DELAY"))]
    assert map_event("ALIGN", f, Enc(2, -1, ts=1.2)) == [Nudge("align_delay_ms", -1, False)]
    assert map_event("ALIGN", f, Enc(2, -1, ts=1.21)) == [Nudge("align_delay_ms", -1, False)]
    assert map_event("ALIGN", f, Enc(2, -1, ts=1.22)) == [Nudge("align_delay_ms", -1, False)]  # x1
    s = st.State(sub_delay_ms=0.1)                                # what the controller then does
    s.nudge("align_delay_ms", -1)
    s.nudge("align_delay_ms", -1)
    assert (s.sub_delay_ms, s.top_delay_ms) == (0.0, 0.1)


def test_lowcut_shows_whole_hz():
    assert pages.fmt_hz(112.25) == "112" and pages.fmt_hz(113.0) == "113"
    assert pages.fmt_hz(300.0) == "300" and pages.fmt_hz(20.0) == "20"


@pytest.mark.parametrize("raw,want", [
    ("dim", "dim"), ("off", "off"), ("bright", "bright"), (" Off ", "off"), ("BRIGHT", "bright"),
    (False, "off"), (True, "dim"),              # YAML: a bare off / on
    ("cyan", None), ("", None), (0, None), (1, None), (None, None), (["off"], None),
])
def test_parse_knob_tags(raw, want):
    assert im.parse_knob_tags(raw) == want


@pytest.mark.parametrize("cfg,want", [
    (None, "dim"), ({}, "dim"), ({"ui": None}, "dim"), ({"ui": "off"}, "dim"),
    ({"ui": {}}, "dim"), ({"ui": {"knob_tags": "off"}}, "off"),
    ({"ui": {"knob_tags": False}}, "off"), ({"ui": {"knob_tags": "bright"}}, "bright"),
    ({"ui": {"knob_tags": "neon"}}, "dim"),
])
def test_knob_tag_style_from_k1000_yaml(cfg, want):
    assert im.knob_tag_style(cfg) == want


def test_shipped_k1000_yaml_says_off():
    """Thomas's box (2026-10-13): no tags, a sticker on the shell; the built-in default stays dim."""
    from k1000.profiles import load_settings
    settings = load_settings(Path(__file__).resolve().parents[1] / "etc" / "k1000.yaml")
    assert settings["ui"]["knob_tags"] is False and im.knob_tag_style(settings) == "off"
    assert im.knob_tag_style({}) == "dim"


def test_knob_corners_are_the_sticker_layout():
    """The operator card's sticker line: E1 top-left, E2 bottom-left, E3 top-right, E4
    bottom-right (confirmed on the box 2026-10-12)."""
    assert im.KNOB_CORNERS == ("tl", "bl", "tr", "br")


_TAG_X = {"l": (6, 38), "r": (pages.W - 38, pages.W - 6)}
_TAG_Y = {"t": (30, 56), "b": (290, 318)}


def _tag_box(e: int) -> tuple[int, int, int, int]:
    v, h = im.KNOB_CORNERS[e]
    return (_TAG_X[h][0], _TAG_Y[v][0], _TAG_X[h][1], _TAG_Y[v][1])


@pytest.mark.parametrize("page", ["SHOW", "ALIGN", "SETUP"])
def test_knob_tags_dim_off_bright(page):
    """dim (default): a small dark-grey tag, no cyan; off: no tag, the label in the tag's
    corner; bright: v1's cyan tag. Every page that names its knobs follows the style."""
    s = st.State(sub_delay_ms=2.35)
    imgs = {t: pages.render(page, s, pages.ScreenStatus(knob_tags=t, **RUN), Focus(slot=1), now=0.0)
            for t in ("dim", "off", "bright", "neon")}
    default = pages.render(page, s, pages.ScreenStatus(**RUN), Focus(slot=1), now=0.0)
    assert (np.asarray(default) == np.asarray(imgs["dim"])).all()          # dim is the default
    assert (np.asarray(imgs["neon"]) == np.asarray(imgs["dim"])).all()     # unknown = dim
    knobs = [e for e, lab in enumerate(im.knob_labels(page)) if lab]
    assert knobs
    for e in knobs:
        box = _tag_box(e)
        assert _count(imgs["bright"], box, pages.CYAN) > 20, (page, e)
        assert _count(imgs["bright"], box, pages.AMBER) == 0, (page, e)
        assert _count(imgs["dim"], box, pages.TAG_DIM) > 10, (page, e)
        assert _count(imgs["dim"], box, pages.CYAN) == 0, (page, e)
        assert _count(imgs["dim"], box, pages.AMBER) == 0, (page, e)
        assert _count(imgs["off"], box, pages.TAG_DIM) == 0, (page, e)
        assert _count(imgs["off"], box, pages.CYAN) == 0, (page, e)
        assert _count(imgs["off"], box, pages.AMBER) > 20, (page, e)      # the label moved in
        # dim: smaller than bright, and it leaves the label where v1 had it
        assert _count(imgs["dim"], box, pages.TAG_DIM) < _count(imgs["bright"], box, pages.CYAN)
    for y0, y1 in (_TAG_Y["t"], _TAG_Y["b"]):                              # the label rows
        row = (0, y0, pages.W, y1)
        assert _count(imgs["dim"], row, pages.CYAN) == 0
        assert _count(imgs["dim"], row, pages.AMBER) == _count(imgs["bright"], row, pages.AMBER)


def test_knob_tags_off_gives_the_show_pills_more_room():
    s = st.State()
    c_dim = pages._Ctx(Focus(slot=1), 0.0, "dim")
    c_off = pages._Ctx(Focus(slot=1), 0.0, "off")
    for c in (c_dim, c_off):
        pages._page_show(c, s, pages.ScreenStatus(**RUN))
    assert c_off.bottom_label_end["l"] == c_dim.bottom_label_end["l"] - pages.TAG_W
    assert c_off.bottom_label_end["r"] == c_dim.bottom_label_end["r"] + pages.TAG_W


def test_demo_cases_include_the_d011_pictures():
    names = {c[0] for c in pages.demo_cases()}
    assert {"show_tags_off", "show_tags_bright", "align_tags_off", "align_tops_later",
            "align_aligned"} <= names
