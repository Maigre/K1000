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
                                Nudge, ResetLive, SelectBand, SelectGroup, SelectItem, SelectSlot,
                                SetChoice, ShowHint, Toggle, ToggleBand, UndoReset, Unlock,
                                apply_focus, map_event)


# ------------------------------------------------------------------------------ helpers
# D-014's gate: the B3 hold switches a band only on a confirmed button map (the Knob test).
CONFIRMED = {"controls": {"buttons_confirmed": True}}


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
    y = pages.ALIGN_MUTED_BAND[1] - 2                 # inside the band, below its text
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


# SETUP's list (D-013): 8 rows of 28 px under the top line; a group header's name at x 10, an
# item's label at x 24 (indented), its value right-aligned at W - 18; all drawn at the row's middle.
# D-017: the selected row always on the 4th of them (SETUP_SEL_Y, its middle).
SETUP_LIST_Y = (pages.STATUS_H + 30, pages.STATUS_H + 30 + 8 * 28)
SETUP_HEAD_X, SETUP_LABEL_X, SETUP_VALUE_X = 10, 24, pages.W - 18
SETUP_FOOTER_Y = pages.H - pages.FOOTER_H // 2
SETUP_SEL_Y = SETUP_LIST_Y[0] + 3 * 28 + 28 // 2 - 1


def _ink(page, state, status, focus, now=0.0, tags=None):
    """Draw SHOW or ALIGN's body and return where each knob's texts landed (pages._Ctx.ink:
    knob -> {label, tag, value, unit, sub, fine} -> (x0, y0, x1, y1); -1 -> the ALIGN badge)."""
    c = pages._Ctx(focus, now, tags or pages.knob_tags(status))
    {"SHOW": pages._page_show, "ALIGN": pages._page_align}[im.page_name(page)](c, state, status)
    return c.ink


def _overlap(a, b) -> bool:
    """Two (x0, y0, x1, y1) boxes share any pixel."""
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _drawn(page, state, status, focus, now=0.0):
    """Render `page` and also return every text drawn on it, [(text, x, y, fill)] (text checks:
    what the screen SAYS, where, in which colour)."""
    drawn = []
    real = pages._Draw.text

    def spy(self, xy, text, *a, **kw):
        fill = kw.get("fill", a[1] if len(a) > 1 else None)
        drawn.append((text, xy[0], xy[1], tuple(fill) if fill is not None else None))
        return real(self, xy, text, *a, **kw)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pages._Draw, "text", spy)
        img = pages.render(page, state, status, focus, now=now)
    return img, drawn


def _in_list(y) -> bool:
    return SETUP_LIST_Y[0] <= y < SETUP_LIST_Y[1]


def _setup_heads(drawn):
    """The group headers on screen, top to bottom: [(name, y, fill)]."""
    return [(t, y, f) for t, x, y, f in drawn if x == SETUP_HEAD_X and _in_list(y)]


def _setup_value_at(drawn, y):
    vals = [t for t, x, ty, _f in drawn if x == SETUP_VALUE_X and ty == y]
    return vals[0] if vals else None


def _setup_selected(img, drawn, label):
    """(y, label fill, bar colour, value text) of the selected SETUP row: the one row whose
    label is drawn on the amber (or red) selection bar. Asserts there is exactly one."""
    hits = [(y, f) for t, x, y, f in drawn if t == label and x == SETUP_LABEL_X and _in_list(y)
            and img.getpixel((10, y)) in (pages.AMBER, pages.RED)]
    assert len(hits) == 1, (label, hits)
    y, fill = hits[0]
    return y, fill, img.getpixel((10, y)), _setup_value_at(drawn, y)


def _setup_footer(drawn):
    return [t for t, x, y, _f in drawn if y == SETUP_FOOTER_Y and x == pages.W // 2]


def _other_choice(item, state):
    """A value of a bool/choice SETUP item other than the one `state` holds."""
    cur = im.setup_value_of(state, item)
    return next(c for c in item.choices if c != cur)


# Every SETUP row as it can be selected (D-017): plain; with its popup open, an action waiting
# for its second E3 push (Reset live EQ, Laptop access) or a risky row with a value proposed.
SETUP_ROW_MODES = [(i, mode) for i, it in enumerate(im.SETUP_ITEMS)
                   for mode in ("plain",) + (("confirm",) if it.needs_confirm else ())
                   + (("pick",) if it.risky else ())]


def _setup_mode_focus(item: int, mode: str, state, **kw) -> Focus:
    it = im.SETUP_ITEMS[item]
    if mode == "confirm":
        kw.update(confirm=it.field, confirm_until=math.inf)
    elif mode == "pick":
        kw.update(pick=it.field, pick_value=_other_choice(it, state), pick_until=math.inf)
    return Focus(item=item, **kw)


def _setup_ctx(state, status, focus, now=0.0):
    """Draw SETUP's body and return the frame context: `popup` is what the D-017 popup said
    ({title, big, what, instr, px}; None when closed), `ink[pages.POPUP_INK]` where its texts
    landed, `ink[0..2]` the knob labels."""
    c = pages._Ctx(focus, now, pages.knob_tags(status))
    pages._page_setup(c, state, status)
    return c


def _popup_edge(img) -> bool:
    """The D-017 popup's red outline is on screen (both side edges, at mid height)."""
    x0, y0, x1, y1 = pages.SETUP_POPUP
    ym = (y0 + y1) // 2
    return img.getpixel((x0 + 1, ym)) == pages.RED and img.getpixel((x1 - 1, ym)) == pages.RED


@pytest.mark.parametrize("item,mode", SETUP_ROW_MODES,
                         ids=[f"{im.SETUP_ITEMS[i].group}-{im.SETUP_ITEMS[i].label}-{m}"
                              for i, m in SETUP_ROW_MODES])
def test_setup_muted_keeps_selected_row_visible(item, mode):
    """The selected row, or its popup (D-017: a risky row's proposal, an action waiting for its
    second E3 push), must stay visible while muted: SETUP is used at setup time, when the box is
    muted. MUTED moves to a red footer instead of the band, below the popup; a hint takes that
    footer, the row or the popup stays."""
    s = st.State(muted=True)
    it = im.SETUP_ITEMS[item]
    status = pages.ScreenStatus(muted=True, **OK)
    focus = _setup_mode_focus(item, mode, s)
    img, drawn = _drawn("SETUP", s, status, focus)
    if mode == "plain":
        y, fill, bar, _val = _setup_selected(img, drawn, it.label)
        assert (bar, fill) == (pages.AMBER, pages.BG)
        assert img.getpixel((240, y - 11)) == bar                      # the bar spans the row
        assert not _popup_edge(img)
    else:
        assert _popup_edge(img)
        c = _setup_ctx(s, status, focus)
        assert c.popup is not None
        assert ("→" in c.popup["big"]) if mode == "pick" else c.popup["big"].endswith("?")
    assert img.getpixel((150, pages.H - 8)) == pages.BAND_RED          # MUTED footer
    assert _muted_pill(img) and img.getpixel((2, 100)) == pages.BAND_RED
    hinted = _setup_mode_focus(item, mode, s, hint="x", hint_until=math.inf)   # a hint: footer
    img, drawn = _drawn("SETUP", s, status, hinted)
    assert img.getpixel((20, pages.H - 8)) == pages.AMBER
    assert _setup_footer(drawn) == ["x"]
    if mode == "plain":
        assert _setup_selected(img, drawn, it.label)[2] == pages.AMBER  # the row stays
    else:
        assert _popup_edge(img)                                        # the popup stays


@pytest.mark.parametrize("page,editing,e,ink", [
    # SHOW (D-015): E3 Q top right, FINE in its label row (cyan); E4 GAIN bottom right, FINE at
    # its box's inner top corner (black on the amber box)
    ("SHOW", "live_peq.1.q", 2, pages.CYAN),
    ("SHOW", "live_peq.1.gain", 3, pages.BG),
    # ALIGN (the v0 band): E3 BALANCE top right (label row), E4 LO-CUT bottom right (on its box)
    ("ALIGN", "balance_db", 2, pages.CYAN),
    ("ALIGN", "lowcut_hz", 3, pages.BG),
])
def test_fine_marker_stays_visible_under_the_muted_band(page, editing, e, ink):
    s = st.State()
    s.muted = True
    st_ = pages.ScreenStatus(muted=True, **OK)
    base = dict(slot=1, editing=editing, edit_until=math.inf)
    f = Focus(held={e}, **base)
    box = _ink(page, s, st_, f)[e]["fine"]
    region = [(x, y) for x in range(int(box[0]), int(box[2])) for y in range(int(box[1]), int(box[3]))]
    with_fine = pages.render(page, s, st_, f, now=0.0)
    without = pages.render(page, s, st_, Focus(**base), now=0.0)
    assert sum(with_fine.getpixel(p) == ink for p in region) > 10        # FINE
    assert sum(without.getpixel(p) == ink for p in region) == 0
    assert not any(with_fine.getpixel(p) == pages.BAND_RED for p in region)


def _band_line(state, status, focus=None):
    return pages._mute_hint(pages._Ctx(focus or Focus(), 0.0), state, status)[1]


def test_muted_band_says_ready_only_when_the_engine_runs_in_sync():
    s = st.State(muted=True)
    run = dict(device_present=True, muted=True)
    assert _band_line(s, pages.ScreenStatus(engine="Running", ready=True, **run)) == \
        "READY · hold B4 to unmute"
    for engine in ("Starting", "Paused"):         # not processing: no READY
        assert _band_line(s, pages.ScreenStatus(engine=engine, ready=True, **run)) == \
            "hold B4 to unmute"
    assert _band_line(s, pages.ScreenStatus(engine="Running", ready=False, **run)) == \
        "hold B4 to unmute"                       # sync failed: knob edits do not reach it


def test_muted_band_tells_a_k1000_hold_from_the_operators_mute():
    """K1000 holding Main before its own ramp is not 'READY · hold B4 to unmute': it comes in
    by itself, and B4 (the mute button since D-013) keeps it muted."""
    held = st.State(muted=False)
    status = dict(device_present=True, muted=True, ramping=True, ready=False,
                  message="NO AUDIO FLOWING · HELD MUTED", alert="NO AUDIO FLOWING · HELD MUTED")
    for engine in ("Starting", "Running"):
        assert _band_line(held, pages.ScreenStatus(engine=engine, **status)) == \
            "RAMPS IN BY ITSELF · hold B4 to keep muted"
    # the operator's own mute during the ramp: the reason, never 'ramps in'
    assert _band_line(st.State(muted=True), pages.ScreenStatus(engine="Starting", **status)) == \
        "NO AUDIO FLOWING · HELD MUTED"
    # a hint toast still wins for its 2.5 s
    f = Focus(hint=im.HINT_MUTE, hint_until=math.inf)
    assert im.HINT_MUTE == "HOLD B4 TO MUTE / UNMUTE"
    assert pages._mute_hint(pages._Ctx(f, 0.0), held, pages.ScreenStatus(**status))[0]
    # no screen text names the old mute button any more
    for line in (_band_line(held, pages.ScreenStatus(engine="Running", **status)),
                 _band_line(st.State(muted=True), pages.ScreenStatus(engine="Running", ready=True,
                                                                     device_present=True, muted=True))):
        assert "B2" not in line and "B4" in line


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
@pytest.mark.parametrize("e", [0, 1, 2, 3])
def test_fine_label_never_covers_the_value_or_its_unit(slot, e):
    """FINE sits on the INNER side (D-015: the value is flush to the outer one): a top block's
    in its label row, a bottom block's at its box's inner top corner, in a column the value and
    its unit keep clear of. A top block keeps its whole value size with FINE held."""
    s = st.State()
    s.live_peq[1].q = 8.0
    fld = [im.F_MASTER, im.live_field(slot, "freq"), im.live_field(slot, "q"),
           im.live_field(slot, "gain")][e]
    f = Focus(slot=slot, editing=fld, edit_until=math.inf, held={e})
    ink = _ink("SHOW", s, pages.ScreenStatus(**OK), f)
    plain = _ink("SHOW", s, pages.ScreenStatus(**OK), Focus(slot=slot, editing=fld,
                                                            edit_until=math.inf))
    fine = ink[e]["fine"]
    for k, parts in ink.items():
        for part, box in parts.items():
            if (k, part) != (e, "fine"):
                assert not _overlap(fine, box), (k, part)
    left = im.KNOB_CORNERS[e][1] == "l"
    if im.KNOB_CORNERS[e][0] == "t":                                      # in the label row,
        label = ink[e].get("tag", ink[e]["label"]) if not left else ink[e]["label"]
        assert (fine[0] > label[2]) if left else (fine[2] < label[0])    # inner end
        assert fine[3] <= pages.TOP_BOX[0] + 1
        assert ink[e]["value"] == plain[e]["value"]                       # nothing shrank
    else:                                                                 # at the box's inner side
        value = ink[e]["value"]
        assert (fine[0] > value[2]) if left else (fine[2] < value[0])
        assert pages.BOT_BOX[0] <= fine[1] and fine[3] <= pages.BOT_BOX[1]


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
    f = Focus(slot=1, editing="live_peq.1.gain", edit_until=INF, held={2, 3})
    fr = pages.demo_spectrum()
    clean = pages.render("SHOW", s, pages.ScreenStatus(spectrum=fr, **RUN), f, now=0.0)
    muted = pages.render("SHOW", st.State.from_dict({**s.to_dict(), "muted": True}),
                         pages.ScreenStatus(spectrum=fr, muted=True, **RUN), f, now=0.0)
    b0, b1 = pages.SHOW_MUTED_BAND
    assert all(muted.getpixel((x, b0 + 4)) == pages.BAND_RED for x in range(pages.W))
    y0 = _zero_db_y()
    assert b1 < y0 - 1
    mx0, mx1 = pages.METER_BOX[0], pages.METER_BOX[2]
    top = pages.STATUS_H + 4                                            # (under the red frame)
    kept = [(8, top, mx0 - 4, pages.TOP_BOX[1] + 1),                    # E1 MASTER
            (mx1 + 4, top, 472, pages.TOP_BOX[1] + 1),                  # E3 Q (FINE in its label row)
            (mx0, top, mx1, pages.METER_BOX[3]),                         # IN SUB TOP meters
            (8, pages.BOT_BOX[0], 236, pages.BOT_BOX[1] + 1),           # E2 FREQ
            (246, pages.BOT_BOX[0], 472, pages.BOT_BOX[1] + 1),         # E4 GAIN (edited, FINE)
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


def test_show_band_name_lights_up_when_b2_changes_the_band():
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


@pytest.mark.parametrize("field, e, colour", [
    (im.F_ALIGN_DELAY, 1, pages.FG),        # E2 (bottom left, D-015): the sub-line over it
    (im.F_BALANCE, 2, pages.DIM),           # E3 (top right, D-015): the sub-line under it
])
def test_align_edit_box_stops_short_of_the_sub_line(field, e, colour):
    """UX-9: '≈ 0.81 m' was drawn half over the amber edit box (grey on amber, cut in two).
    The delay's sub-line ('SUBS LATER · 0.81 m') is white while unlocked: it says the side."""
    s = st.State(sub_delay_ms=2.35, balance_db=3.0)
    status = pages.ScreenStatus(align_locked=False, align_unlock_s=20.0, **RUN)
    f = Focus(editing=field, edit_until=INF)
    img = pages.render("ALIGN", s, status, f, now=0.0)
    sub = _ink("ALIGN", s, status, f)[e]["sub"]
    box = (int(sub[0]), int(sub[1]), int(sub[2]) + 1, int(sub[3]) + 1)
    assert _count(img, box, pages.AMBER) == 0
    assert _count(img, box, colour) > 20                                  # the sub-line is there
    assert _count(img, (box[0], box[3] + 4, box[2], box[3] + 12) if e == 1 else
                  (box[0], box[1] - 12, box[2], box[1] - 4), pages.AMBER) > 50   # the box is there


def test_the_notch_pictures_are_what_the_real_notch_helper_does():
    """UX-3: show_notch was hand-built (P3 set up next to P2 cutting 0.05 oct away, which the
    controller never does). Both pictures now come from Controller.notch_helper."""
    cases = {c[0]: c for c in pages.demo_cases()}
    _n, _p, fresh, _s, f1 = cases["show_notch"]
    assert f1.slot == 2 and f1.editing == "live_peq.2.gain" and f1.hint.startswith("P3 · ")
    assert fresh.live_peq[2].gain == 0.0 and fresh.live_peq[2].q == pytest.approx(8.651, abs=1e-3)
    _n, _p, again, _s, f2 = cases["show_notch_reselect"]
    assert f2.slot == 1 and f2.hint == "P2 · 2.52k · E4 TO CUT"
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


def test_align_locks_e2_e4_and_counts_the_unlock_down():
    """D-015: the locked knobs are the two bottom ones (E2 DELAY, E4 LO-CUT); the LOCKED ·
    HOLD B2 badge sits on the bottom row between their labels, joined to both by a line, and
    turns into the amber EDIT m:ss countdown once a B2 hold unlocked them."""
    s = st.State(sub_delay_ms=2.35, lowcut_hz=112.0)
    locked = pages.render("ALIGN", s, pages.ScreenStatus(**RUN), Focus(), now=0.0)
    by_status = pages.render("ALIGN", s, pages.ScreenStatus(align_locked=False, align_unlock_s=27.4,
                                                            **RUN), Focus(), now=0.0)
    by_focus = pages.render("ALIGN", s, pages.ScreenStatus(**RUN), Focus(unlock_until=100.0),
                            now=72.6)
    e2 = (4, 214, pages.METER_X0 - 4, 276)                    # DELAY value, bottom left
    e4 = (pages.METER_X1 + 4, 214, pages.W - 4, 276)          # LO-CUT value, bottom right
    badge = (pages.METER_X0, pages.LOCK_Y0, pages.METER_X1, pages.LOCK_Y1 + 1)
    for box in (e2, e4):
        assert _count(locked, box, pages.FG) == 0 and _count(locked, box, pages.DIM) > 200
        assert _count(by_status, box, pages.FG) > 200
    assert _count(locked, badge, pages.AMBER) == 0 and _count(locked, badge, pages.DIM) > 100
    assert _count(by_status, badge, pages.AMBER) > 1000                        # EDIT 0:28
    assert (_crop(by_status, badge) == _crop(by_focus, badge)).all()
    # the badge covers both locked knobs: it lies between their labels, and a line in its
    # colour runs from each label to it
    ink = _ink("ALIGN", s, pages.ScreenStatus(**RUN), Focus())
    bx0, by0, bx1, by1 = ink[-1]["badge"]
    lab2, lab4 = ink[1]["label"], ink[3]["label"]
    assert lab2[2] < bx0 and bx1 < lab4[0] and by0 <= (lab2[1] + lab2[3]) / 2 <= by1
    assert abs((bx0 + bx1) / 2 - (lab2[2] + lab4[0]) / 2) < 2
    cy = round((pages.LOCK_Y0 + pages.LOCK_Y1) / 2)
    row = np.asarray(locked)[cy - 1:cy + 2]
    for xa, xb in ((lab2[2] + 12, bx0 - 6), (bx1 + 6, lab4[0] - 12)):
        span = (row[:, int(xa):int(xb)] == np.array(pages.DIM, np.uint8)).all(axis=2).any(axis=0)
        assert span.mean() > 0.9, (xa, xb)
    # master and balance (the top knobs) are live either way: never dimmed
    for img in (locked, by_status):
        assert _count(img, (8, 55, pages.METER_X0 - 4, 118), pages.FG) > 200
        assert _count(img, (pages.METER_X1 + 4, 55, pages.W - 4, 118), pages.FG) > 200
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
    # Lo-Cut (D-013, TOPS): on/off here, with its frequency (set on ALIGN E4) said
    assert v(s, items[im.F_LOWCUT_ON], None) == "ON · 100 Hz"
    assert v(st.State(lowcut_hz=112.0), items[im.F_LOWCUT_ON], None) == "ON · 112 Hz"
    assert v(st.State(lowcut_on=False), items[im.F_LOWCUT_ON], None).startswith("off")
    assert v(st.State(spectrum_on=False), items[im.F_SPECTRUM_ON], None) == "off"
    assert v(s, items[im.F_SPECTRUM_ON], pages.ScreenStatus(spectrum_state="disabled")) == "DISABLED"
    assert v(s, items["top_inverted"], None) == "normal"
    assert v(st.State(sub_inverted=True), items["sub_inverted"], None) == "INVERTED"
    assert v(st.State(mono_subs=True), items["mono_subs"], None) == "ON"
    assert v(s, items["dev_audition"], None) == "SUM" and v(s, items["profile"], None) == "DEV"
    # read-only rows (Focus.readonly, the Controller's): what they are, and that they are fixed
    ro = {"profile": "PROFILE PINNED IN k1000.yaml", "dev_audition": "AUDITION: DEV PROFILE ONLY",
          im.F_SPECTRUM_ON: "SPECTRUM OFF IN k1000.yaml"}
    assert v(s, items["profile"], None, ro) == "DEV · PINNED"
    assert v(st.State(profile="m4"), items["profile"], None, ro) == "M4 · PINNED"
    assert v(s, items["dev_audition"], None, ro) == "—"
    assert v(s, items[im.F_SPECTRUM_ON], None, ro) == "DISABLED"
    # the undo row is dimmed while there is nothing to undo
    f = Focus(item=_item(im.A_UNDO_RESET) - 1)

    def undo_label(status):
        _img, drawn = _drawn("SETUP", s, status, f)
        return [fill for t, x, y, fill in drawn if t == "Undo reset" and x == SETUP_LABEL_X]

    assert undo_label(pages.ScreenStatus(**RUN)) == [pages.DIM]
    assert undo_label(pages.ScreenStatus(undo_reset_s=200.0, **RUN)) == [pages.FG]


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


UNLOCKED = math.inf          # Focus(unlock_until=UNLOCKED): ALIGN's E2/E4 editable


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
            assert item.kind == "choice" and item.field in st.CHOICES, item.field
            assert item.field in st.REBUILD_FIELDS


# D-013: SETUP grouped by speaker floor, then the rest by what it serves (Thomas, 2026-10-13).
# D-016 (2026-10-16): De-harsh in TOPS, Loudness in SUBS, AUX IN in INPUT, each switch first.
D013_SETUP = (
    ("TOPS", (("Trim L", "out_trim_db.2", "num"), ("Trim R", "out_trim_db.3", "num"),
              ("Polarity", "top_inverted", "bool"), ("Lo-Cut", "lowcut_on", "bool"),
              ("De-harsh", "deharsh_on", "bool"), ("De-harsh freq", "deharsh_hz", "num"),
              ("De-harsh thresh", "deharsh_thresh_db", "num"))),
    ("SUBS", (("Trim L", "out_trim_db.0", "num"), ("Trim R", "out_trim_db.1", "num"),
              ("Polarity", "sub_inverted", "bool"), ("Mono L+R", "mono_subs", "bool"),
              ("Low-pass", "sub_lowpass_on", "bool"), ("Low-pass freq", "sub_lowpass_hz", "num"),
              ("Loudness", "loud_on", "bool"), ("Loudness boost", "loud_boost_db", "num"),
              ("Loudness ref", "loud_ref_db", "num"))),
    ("INPUT", (("Trim", "input_trim_db", "num"), ("AUX IN 1/2", "aux_mode", "choice"),
               ("AUX level", "aux_gain_db", "num"))),
    ("LIVE EQ", (("Reset live EQ", "reset_live", "action"), ("Undo reset", "undo_reset", "action"),
                 ("Spectrum", "spectrum_on", "bool"))),
    ("PHONE", (("Phone", "PHONE", "page"), ("Laptop access", "laptop_access", "action"))),
    ("BOX", (("Profile", "profile", "choice"), ("Audition (dev)", "dev_audition", "choice"),
             ("Knob test", "HWTEST", "page"))),
)


def test_setup_groups_are_d013s():
    """The 27 items under TOPS / SUBS / INPUT / LIVE EQ / PHONE / BOX, in that order; every
    item knows its group. No Top delay since D-011: ALIGN's signed E3 is the one place to set
    the alignment (and the Lo-Cut frequency stays on ALIGN E4). D-016 added 8 rows."""
    assert tuple((g, tuple((it.label, it.field, it.kind) for it in items))
                 for g, items in im.SETUP_GROUPS) == D013_SETUP
    assert im.SETUP_GROUP_NAMES == ("TOPS", "SUBS", "INPUT", "LIVE EQ", "PHONE", "BOX")
    assert len(im.SETUP_ITEMS) == 27
    assert [(it.group, it.label, it.field, it.kind) for it in im.SETUP_ITEMS] == \
        [(g, *row) for g, rows in D013_SETUP for row in rows]
    assert len({it.field for it in im.SETUP_ITEMS}) == 27                  # one row per field
    assert not any(i.field.endswith("_delay_ms") or i.field == im.F_LOWCUT_FREQ
                   for i in im.SETUP_ITEMS)
    assert "Top delay" not in [i.label for i in im.SETUP_ITEMS]


def test_setup_item_kinds_say_how_each_row_is_set():
    """D-013's kinds, D-017's gestures: an action that touches the sound (Reset live EQ) or opens
    the fence (Laptop access) needs a second E3 push; ON/off and choice rows are stepped by E3;
    the pipeline rebuilds (a possible click) are among them, and say so."""
    by = {i.field: i for i in im.SETUP_ITEMS}
    assert {f for f, i in by.items() if i.needs_confirm} == {"reset_live", "laptop_access"}
    assert {f for f, i in by.items() if i.picks} == \
        {"top_inverted", "lowcut_on", "sub_inverted", "mono_subs", "sub_lowpass_on", "spectrum_on",
         "profile", "dev_audition", "deharsh_on", "loud_on", "aux_mode"}
    assert {f for f, i in by.items() if i.rebuild} == set(st.REBUILD_FIELDS)
    for f in st.REBUILD_FIELDS:                                       # never one gesture
        assert by[f].picks and by[f].risky and not by[f].needs_confirm, f
    assert not by["lowcut_on"].confirm                                 # turn + push = confirm
    for f, i in by.items():
        if i.kind == "bool":
            assert i.choices == (False, True), f
        elif i.kind == "choice":
            assert i.choices == tuple(st.CHOICES[f]) and len(i.choices) > 1, f
        else:
            assert i.choices == () and not i.picks and not i.risky, f
    assert by["dev_audition"].choices == ("sum", "tops", "subs")
    assert by["profile"].choices == ("dev", "m4")
    assert by["aux_mode"].choices == ("off", "mic", "line")                # D-016
    assert [im.choice_name(by["aux_mode"], c) for c in by["aux_mode"].choices] == ["off", "MIC", "LINE"]
    assert {"deharsh_on", "loud_on", "aux_mode"} <= {f for f, i in by.items() if i.rebuild}
    # the old kinds and hints are gone: no blind double push on anything but the two actions
    assert {i.kind for i in im.SETUP_ITEMS} == {"num", "bool", "choice", "page", "action"}
    for gone in ("HINT_PUSH_TOGGLE", "HINT_CONFIRM_LOWCUT", "HINT_CONFIRM", "HINT_CONFIRM_RESET",
                 "HINT_CONFIRM_LAPTOP"):
        assert not hasattr(im, gone), gone


# D-017: the risky rows (Claude's proposal, Thomas's to change: SETUP_RISKY, one line)
D017_RISKY = set(st.REBUILD_FIELDS) | {"top_inverted", "sub_inverted", "lowcut_on"}
EVERYDAY_ON_OFF = ("sub_lowpass_on", "spectrum_on")


def test_setup_risky_rows_are_d017s():
    """Every pipeline rebuild (Mono L+R, Profile, Audition, De-harsh, Loudness, AUX IN 1/2), both
    polarities and Lo-Cut are risky (an E3 turn proposes, an E3 push applies); one definition,
    input_map.SETUP_RISKY. Every other ON/off applies as E3 turns. Only ON/off and choice rows
    can be risky."""
    by = {i.field: i for i in im.SETUP_ITEMS}
    assert im.SETUP_RISKY == D017_RISKY
    assert {f for f, i in by.items() if i.risky} == D017_RISKY
    assert {f for f, i in by.items() if i.picks and not i.risky} == set(EVERYDAY_ON_OFF)
    assert not any(i.risky for i in im.SETUP_ITEMS if not i.picks)


def test_setup_group_helpers():
    first = {g: next(i for i, it in enumerate(im.SETUP_ITEMS) if it.group == g)
             for g in im.SETUP_GROUP_NAMES}
    assert list(first.values()) == [0, 7, 16, 19, 22, 24]
    mono = _item("mono_subs")
    assert im.setup_group_start(mono) == first["SUBS"]
    assert im.setup_group_pos(mono) == ("SUBS", 4, 9)
    assert im.setup_group_pos(0) == ("TOPS", 1, 7)
    assert im.setup_group_pos(_item("input_trim_db")) == ("INPUT", 1, 3)
    assert im.setup_group_pos(_item("aux_gain_db")) == ("INPUT", 3, 3)
    assert im.setup_group_pos(26) == ("BOX", 3, 3)
    assert im.setup_group_pos(99) == ("BOX", 3, 3) and im.setup_group_pos(-4) == ("TOPS", 1, 7)
    assert im.setup_group_step(mono, 1) == first["INPUT"]
    assert im.setup_group_step(mono, -1) == first["SUBS"]             # its own first item first
    assert im.setup_group_step(first["SUBS"], -1) == first["TOPS"]
    assert im.setup_group_step(mono, -2) == first["TOPS"]
    assert im.setup_group_step(0, 99) == first["BOX"] and im.setup_group_step(26, -99) == 0
    assert im.setup_group_step(0, -1) == 0 and im.setup_group_step(first["BOX"], 1) == first["BOX"]


def test_pages_and_v0_aliases():
    assert im.PAGES == ("SHOW", "ALIGN", "SETUP") and im.HOME_PAGE == "SHOW"
    assert set(im.ALL_PAGES) == {"SHOW", "ALIGN", "SETUP", "HWTEST", "PHONE"}
    assert im.page_name("MAIN") == "ALIGN" and im.page_name("LIVE") == "SHOW"
    assert im.page_name("SETUP") == "SETUP" and im.page_name(None) is None
    assert map_event("MAIN", Focus(), Enc(2, 1)) == [Nudge("balance_db", 1, False)]
    assert map_event("LIVE", Focus(), Enc(3, 1)) == [Nudge("live_peq.0.gain", 1, False)]
    # D-015: SHOW's Q on E3, GAIN on E4; ALIGN's DELAY on E2, BALANCE on E3
    assert im.knob_labels("LIVE") == im.knob_labels("SHOW") == ["MASTER", "FREQ", "Q", "GAIN"]
    assert im.knob_labels("ALIGN") == ["MASTER", "DELAY", "BALANCE", "LO-CUT"]


def test_show_knobs_follow_the_selected_band():
    f = Focus()
    assert map_event("SHOW", f, Enc(0, 1, ts=0.0)) == [Nudge("master_db", 1, False)]
    assert map_event("SHOW", f, Enc(1, 1, ts=1.0)) == [Nudge("live_peq.0.freq", 1, False)]
    assert map_event("SHOW", f, Enc(2, 1, ts=2.0)) == [Nudge("live_peq.0.q", 1, False)]      # E3 Q
    assert map_event("SHOW", f, Enc(3, -1, ts=3.0)) == [Nudge("live_peq.0.gain", -1, False)]  # E4
    f.slot = 4
    assert map_event("SHOW", f, Enc(1, 1, ts=4.0)) == [Nudge("live_lowshelf.freq", 1, False)]
    assert map_event("SHOW", f, Sw(1, "press")) == []             # E2 push = fine frequency
    assert map_event("SHOW", f, Enc(1, -3, ts=5.0)) == [Nudge("live_lowshelf.freq", -3, True)]
    assert map_event("SHOW", f, Sw(1, "short")) == []             # a push never selects on SHOW
    for n in range(4):                                            # not even without a turn
        assert map_event("SHOW", f, Sw(n, "press")) == [] and map_event("SHOW", f, Sw(n, "short")) == []
    assert map_event("SHOW", f, Enc(0, 0, ts=6.0)) == []
    f.slot = 101                                                  # tolerant: wraps (101 % 6 = HS)
    assert map_event("SHOW", f, Enc(3, 1, ts=7.0)) == [Nudge("live_highshelf.gain", 1, False)]
    assert map_event("SHOW", f, Enc(2, 1, ts=8.0)) == [Nudge("live_highshelf.q", 1, False)]  # SLOPE


def test_b2_on_show_cycles_the_bands_low_to_high_and_wraps():
    """D-013: band select moved from B3 to B2 (sw5)."""
    f = Focus(slot=4)                                             # LS
    seen = []
    for k in range(7):
        acts = map_event("SHOW", f, Sw(5, "short", ts=float(k)))
        assert acts == [SelectBand(1)]
        assert apply_focus(f, acts[0], now=float(k)) is True
        seen.append(im.LIVE_SLOT_SHORT[f.slot])
    assert seen == ["P1", "P2", "P3", "P4", "HS", "LS", "P1"]
    assert f.editing_now(6.1) == im.F_SLOT
    assert map_event("SHOW", f, Sw(5, "long")) == [ShowHint(im.HINT_MUTE)]   # B2 was the mute
    assert map_event("SHOW", f, Sw(5, "press")) == [] and map_event("SHOW", f, Sw(5, "release")) == []
    assert im.next_band(5) == 4 and im.next_band(4, -1) == 5 and im.next_band(99) in im.BAND_CYCLE
    apply_focus(f, SelectSlot(-1), now=0.0)                       # v0 action still wraps
    assert f.slot == 5
    # the other buttons never change the band
    for sw in (4, 6, 7):
        for k in ("press", "short", "long", "release"):
            assert not any(isinstance(a, (SelectBand, SelectSlot))
                           for a in map_event("SHOW", Focus(), Sw(sw, k))), (sw, k)
    # off SHOW (and off ALIGN, where B2 is the unlock), a tap only says where it works
    assert im.HINT_B2_OFF_SHOW == "B2: NEXT BAND ON SHOW"
    for page in ("SETUP", "PHONE"):
        acts = [a for k in ("press", "short", "long", "release") for a in map_event(page, Focus(), Sw(5, k))]
        assert acts == [ShowHint(im.HINT_B2_OFF_SHOW), ShowHint(im.HINT_MUTE)], page   # tap, hold


def test_b3_on_show_is_the_notch_helper_never_a_reset():
    """D-013: the notch helper moved from B4 to B3 (sw6). D-014: a B3 hold on SHOW switches the
    SELECTED band IN/OUT (it said RESET LIVE EQ: IN SETUP before); it never resets anything."""
    f = Focus()
    assert map_event("SHOW", f, Sw(6, "short")) == [NotchHelper()]
    assert apply_focus(f, NotchHelper()) is False                 # the controller acts on it
    for slot in (0, 1, 4, 5):                                     # holds never arm anything
        f.slot = slot
        assert map_event("SHOW", f, Sw(6, "long"), CONFIRMED) == [ToggleBand(slot)]
    assert apply_focus(f, ToggleBand(1)) is False                 # the controller acts on it
    assert f.confirm is None
    assert map_event("SHOW", f, Sw(6, "press")) == [] and map_event("SHOW", f, Sw(6, "release")) == []
    for sw in (4, 5, 7):                                          # no other button is the helper
        for k in ("press", "short", "long", "release"):
            assert NotchHelper() not in map_event("SHOW", Focus(), Sw(sw, k)), (sw, k)
    assert im.HINT_B3_OFF_SHOW == "B3: NOTCH HELPER ON SHOW"
    for page in ("ALIGN", "SETUP", "PHONE"):
        acts = [a for k in ("press", "short", "long", "release") for a in map_event(page, Focus(), Sw(6, k))]
        assert acts == [ShowHint(im.HINT_B3_OFF_SHOW)], page


def test_align_master_and_balance_are_live_e2_e4_locked():
    """D-015: the two locked knobs are the bottom ones, E2 DELAY and E4 LO-CUT; E1 MASTER and
    E3 BALANCE (top) are live."""
    f = Focus()
    assert map_event("ALIGN", f, Enc(0, 1, ts=10.0)) == [Nudge("master_db", 1, False)]
    assert map_event("ALIGN", f, Enc(2, -1, ts=11.0)) == [Nudge("balance_db", -1, False)]
    assert [k.locked for k in im.KNOBS["ALIGN"]] == [False, True, False, True]
    assert [im.KNOB_CORNERS[e] for e, k in enumerate(im.KNOBS["ALIGN"]) if k.locked] == ["bl", "br"]
    for e in (1, 3):
        for d in (1, -3):
            assert map_event("ALIGN", f, Enc(e, d, ts=12.0)) == [ShowHint(im.HINT_LOCKED)]
    map_event("ALIGN", f, Sw(1, "press"))                         # FINE held: still locked
    assert map_event("ALIGN", f, Enc(1, 1, ts=12.5)) == [ShowHint(im.HINT_LOCKED)]
    map_event("ALIGN", f, Sw(1, "release"))
    assert 1 not in f.last_turn and 3 not in f.last_turn          # a locked turn records nothing
    assert map_event("ALIGN", f, Sw(5, "short", ts=13.0)) == [ShowHint(im.HINT_LOCKED)]   # B2 tap
    assert not f.unlocked(13.0) and f.unlock_left(13.0) == 0.0
    assert im.HINT_LOCKED == "LOCKED · HOLD B2 TO EDIT"


def test_b2_hold_unlocks_align_until_30_s_of_idle():
    """D-013: the ALIGN unlock moved from B3 to B2 (sw5): hold = unlock, tap = relock."""
    f = Focus()
    acts = map_event("ALIGN", f, Sw(5, "long", ts=100.0))
    assert acts == [Unlock(im.UNLOCK_S)] and im.UNLOCK_S == 30.0
    assert apply_focus(f, acts[0], now=100.0) is True
    assert f.unlocked(100.0) and f.hint_now(100.1) == im.HINT_UNLOCKED
    assert f.unlock_left(110.0) == pytest.approx(20.0)
    # one hold unlocks BOTH bottom knobs (D-015: E2 DELAY, E4 LO-CUT). The delay keeps v0's
    # wake-up detent, then edits; Lo-Cut edits at once
    assert map_event("ALIGN", f, Enc(1, 1, ts=101.0)) == [ShowHint(im.HINT_ARM.format("DELAY"))]
    assert map_event("ALIGN", f, Enc(1, 1, ts=101.5)) == [Nudge("align_delay_ms", 1, False)]
    assert map_event("ALIGN", f, Enc(3, -1, ts=102.0)) == [Nudge("lowcut_hz", -1, False)]
    # every input on ALIGN renews the 30 s; any 30 s of idle relocks
    map_event("ALIGN", f, Enc(0, -1, ts=125.0))                   # master: renewed to 155
    assert map_event("ALIGN", f, Enc(3, 1, ts=150.0)) == [Nudge("lowcut_hz", 1, False)]
    assert map_event("ALIGN", f, Enc(3, 1, ts=180.5)) == [ShowHint(im.HINT_LOCKED)]
    assert not f.unlocked(180.5)
    # a B2 tap while unlocked locks at once; a hold while unlocked renews
    apply_focus(f, Unlock(30.0), now=200.0)
    assert map_event("ALIGN", f, Sw(5, "long", ts=205.0)) == [Unlock(im.UNLOCK_S)]
    assert map_event("ALIGN", f, Sw(5, "release", ts=205.5)) == []    # the end of the hold
    acts = map_event("ALIGN", f, Sw(5, "short", ts=206.0))
    assert acts == [Lock()]
    assert apply_focus(f, acts[0], now=206.0) is True
    assert not f.unlocked(206.0) and f.hint_now(206.1) == im.HINT_RELOCKED
    assert map_event("ALIGN", f, Enc(3, 1, ts=207.0)) == [ShowHint(im.HINT_LOCKED)]
    assert map_event("ALIGN", f, Enc(1, 1, ts=207.5)) == [ShowHint(im.HINT_LOCKED)]
    # only B2 on ALIGN unlocks anything (B3, the old unlock, no longer does)
    for page in im.ALL_PAGES:
        for sw in range(4, 8):
            if (page, sw) == ("ALIGN", 5):
                continue
            for k in ("press", "short", "long", "release"):
                assert not any(isinstance(a, (Unlock, Lock)) for a in
                               map_event(page, Focus(), Sw(sw, k))), (page, sw, k)


def test_align_knobs_when_unlocked():
    f = Focus(unlock_until=UNLOCKED)
    assert map_event("ALIGN", f, Enc(0, 1, ts=10.0)) == [Nudge("master_db", 1, False)]
    assert map_event("ALIGN", f, Enc(2, -1, ts=11.0)) == [Nudge("balance_db", -1, False)]
    arm = map_event("ALIGN", f, Enc(1, 2, ts=12.0))               # the delay: wakes up first
    assert arm == [ShowHint(im.HINT_ARM.format("DELAY"))] and f.editing_now(12.1) == "align_delay_ms"
    assert map_event("ALIGN", f, Enc(1, 2, ts=12.5)) == [Nudge("align_delay_ms", 2, False)]
    assert map_event("ALIGN", f, Enc(3, -1, ts=13.0)) == [Nudge("lowcut_hz", -1, False)]
    assert map_event("ALIGN", f, Enc(0, 0, ts=14.0)) == []


def test_sub_delay_brushed_once_does_not_change_it():
    """Every settled delay change dips the subs or the tops (delay guard): even unlocked, one
    brushed detent must not. A detent after ARM_WINDOW_S of rest only wakes the knob again."""
    f = Focus(unlock_until=UNLOCKED)
    assert not any(isinstance(a, Nudge) for a in map_event("ALIGN", f, Enc(1, 1, ts=100.0)))
    assert map_event("ALIGN", f, Enc(1, 1, ts=101.0)) == [Nudge("align_delay_ms", 1, False)]
    assert map_event("ALIGN", f, Enc(1, -1, ts=103.0)) == [Nudge("align_delay_ms", -1, False)]
    later = 103.0 + im.ARM_WINDOW_S + 0.1                         # at rest again: wake first
    assert not any(isinstance(a, Nudge) for a in map_event("ALIGN", f, Enc(1, 1, ts=later)))
    # the other knobs act on their first detent
    assert map_event("ALIGN", Focus(), Enc(2, 1, ts=0.0)) == [Nudge("balance_db", 1, False)]
    assert map_event("SHOW", Focus(), Enc(3, 1, ts=0.0)) == [Nudge("live_peq.0.gain", 1, False)]


def test_fine_while_push_held():
    f = Focus(unlock_until=UNLOCKED)
    assert map_event("ALIGN", f, Sw(1, "press")) == []            # sw1 = E2 push (DELAY, D-015)
    assert map_event("ALIGN", f, Enc(1, 1, ts=1.0)) == [Nudge("align_delay_ms", 1, True)]  # deliberate
    assert map_event("ALIGN", f, Sw(1, "long")) == []             # still held
    assert map_event("ALIGN", f, Enc(1, 1, ts=1.01)) == [Nudge("align_delay_ms", 1, True)]  # no accel
    map_event("ALIGN", f, Sw(1, "release"))
    assert map_event("ALIGN", f, Enc(1, 1, ts=2.0)) == [Nudge("align_delay_ms", 1, False)]
    assert map_event("ALIGN", f, Enc(1, 1, ts=5.0)) == [ShowHint(im.HINT_ARM.format("DELAY"))]


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
    map_event("ALIGN", f, Enc(1, 1, ts=3.0))                      # the delay: never accelerated
    assert map_event("ALIGN", f, Enc(1, 1, ts=3.01)) == [Nudge("align_delay_ms", 1, False)]
    map_event("ALIGN", f, Enc(2, 1, ts=4.0))                      # balance: never accelerated
    assert map_event("ALIGN", f, Enc(2, 1, ts=4.005)) == [Nudge("balance_db", 1, False)]
    g = Focus()                                                   # SHOW: FREQ accelerates, GAIN/Q never
    map_event("SHOW", g, Enc(1, 1, ts=0.0))
    map_event("SHOW", g, Enc(1, 1, ts=0.01))
    assert map_event("SHOW", g, Enc(1, 1, ts=0.02)) == [Nudge("live_peq.0.freq", 4, False)]
    for e, p in ((3, "gain"), (2, "q")):                          # D-015: E4 GAIN, E3 Q
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


@pytest.mark.parametrize("page,tap,hold", [
    ("SHOW", "ALIGN", "SETUP"),
    ("ALIGN", "SHOW", "SETUP"),
    ("SETUP", "SHOW", "SHOW"),          # a tap steps back home; a hold too
    ("PHONE", "SETUP", "SHOW"),         # a tap goes back where PHONE was opened; a hold: home
])
def test_b1_tap_flips_show_and_align_hold_opens_setup(page, tap, hold):
    """D-013 (Thomas: "B1 switch between SHOW and ALIGN only and B1-hold to get to SETUP"): a
    tap acts on its release (the driver's 'short'), a hold at 0.8 s ('long'); the press alone
    does nothing any more (it used to step pages), nor does the release after a hold."""
    f = Focus()
    assert map_event(page, f, Sw(4, "press", ts=1.0)) == []
    assert map_event(page, f, Sw(4, "release", ts=1.2)) == []
    assert map_event(page, f, Sw(4, "short", ts=1.2)) == [GotoPage(tap)]
    assert map_event(page, f, Sw(4, "press", ts=2.0)) == []
    assert map_event(page, f, Sw(4, "long", ts=2.8)) == [GotoPage(hold)]
    assert map_event(page, f, Sw(4, "release", ts=3.5)) == []
    assert im.B1_TAP[page] == tap and im.B1_HOLD.get(page, im.HOME_PAGE) == hold
    assert not any(isinstance(a, NextPage) for k in ("press", "short", "long", "release")
                   for a in map_event(page, Focus(), Sw(4, k)))


def test_b1_never_reaches_setup_on_a_tap_and_the_v0_names_follow():
    """SETUP is a hold away from SHOW and ALIGN, never a tap: tapping B1 in the dark to flip
    SHOW/ALIGN cannot land on the list. MAIN/LIVE (v0 names) act as ALIGN/SHOW."""
    page = "SHOW"
    for _ in range(5):
        (act,) = map_event(page, Focus(), Sw(4, "short"))
        page = act.page
        assert page in ("SHOW", "ALIGN")
    assert map_event("MAIN", Focus(), Sw(4, "short")) == [GotoPage("SHOW")]
    assert map_event("LIVE", Focus(), Sw(4, "short")) == [GotoPage("ALIGN")]
    assert map_event("LIVE", Focus(), Sw(4, "long")) == [GotoPage("SETUP")]


def test_b1_in_the_knob_test_is_recorded_and_its_hold_leaves_to_setup():
    f = Focus()
    for k in ("press", "release", "short"):
        assert map_event("HWTEST", f, Sw(4, k)) == [], k                # recorded like any switch
    assert f.hw.sw_last[4] == "short" and f.hw.last.endswith("= B1")
    assert map_event("HWTEST", f, Sw(4, "long")) == [GotoPage("SETUP")]   # the test's own exit


def test_buttons():
    """D-013: B1 PAGE · B2 BAND/UNLOCK · B3 NOTCH · B4 MUTE (sw4..sw7)."""
    f = Focus()
    assert map_event("SHOW", f, Sw(7, "press")) == []
    assert map_event("SHOW", f, Sw(7, "short")) == [ShowHint(im.HINT_MUTE)]
    assert map_event("SHOW", f, Sw(7, "long")) == [Mute("toggle")]
    for page in ("ALIGN", "SETUP", "PHONE"):                      # mute works everywhere
        assert map_event(page, Focus(), Sw(7, "long")) == [Mute("toggle")]
    for page in ("SETUP", "PHONE"):
        assert map_event(page, Focus(), Sw(6, "short")) == [ShowHint(im.HINT_B3_OFF_SHOW)]
        assert map_event(page, Focus(), Sw(6, "long")) == []
        assert map_event(page, Focus(), Sw(5, "short")) == [ShowHint(im.HINT_B2_OFF_SHOW)]
        assert map_event(page, Focus(), Sw(5, "long")) == [ShowHint(im.HINT_MUTE)]   # B2 was mute


@pytest.mark.parametrize("page", im.ALL_PAGES)
def test_no_button_switches_the_lo_cut_or_resets_any_more(page):
    """D-009/D-013: Lo-Cut on/off (chosen, then set by a push) and the live EQ reset (a double
    push) live in SETUP only. Tapping buttons in the dark to find the mute changes neither, nor
    any other SETUP value, on any page."""
    for sw in range(4, 8):
        for kind in ("press", "short", "long", "release"):
            for a in map_event(page, Focus(unlock_until=UNLOCKED), Sw(sw, kind),
                               state=st.State()):
                assert not isinstance(a, (Toggle, SetChoice, ResetLive, UndoReset)), \
                    (page, sw, kind, a)


def test_setup_reset_live_needs_two_e3_pushes_and_undo_one():
    """D-017: Reset live EQ's first E3 push opens its popup (nothing runs), the second within
    CONFIRM_WINDOW_S resets; Undo reset runs on one E3 push; an E3 turn runs nothing; E1/E2
    pushes never run anything; an E1 turn cancels the popup without moving."""
    idx = _item("reset_live")
    f = Focus(item=idx)
    assert map_event("SETUP", f, Sw(2, "short", ts=10.0)) == []                 # the popup
    assert f.confirm_now(10.5) == "reset_live" and im.setup_popup(f, 10.5) == ("confirm", None)
    assert map_event("SETUP", f, Sw(2, "short", ts=11.0)) == [ResetLive()]
    assert f.confirm is None and im.setup_popup(f, 11.0) is None
    map_event("SETUP", f, Sw(2, "short", ts=20.0))                # an expired confirm arms again
    assert map_event("SETUP", f, Sw(2, "short", ts=20.0 + im.CONFIRM_WINDOW_S)) == []
    assert f.confirm_now(26.0) == "reset_live"
    for n in (0, 1):                                              # E1/E2 pushes: a hint only
        assert map_event("SETUP", f, Sw(n, "short", ts=26.0)) == [ShowHint(im.HINT_PUSH_RUN)]
    assert f.confirm_now(26.5) == "reset_live"
    assert map_event("SETUP", f, Enc(0, 1, ts=27.0)) == [ShowHint(im.HINT_CANCELLED)]
    assert f.confirm is None and f.item == idx                    # cancelled, not moved
    apply_focus(f, SelectItem(1))
    assert im.SETUP_ITEMS[f.item].field == "undo_reset"
    assert map_event("SETUP", f, Sw(2, "short", ts=40.0)) == [UndoReset()]     # one push
    assert map_event("SETUP", f, Enc(2, 1)) == [ShowHint(im.HINT_PUSH_RUN)]    # a turn runs nothing
    f.item = idx
    assert map_event("SETUP", f, Enc(2, 1)) == [ShowHint(im.HINT_PUSH_TWICE)]
    assert f.confirm is None
    # a confirm of one item never confirms another: not the other confirmed action...
    g = Focus(item=idx)
    map_event("SETUP", g, Sw(2, "short", ts=50.0))
    g.item = _item(im.A_LAPTOP)
    assert map_event("SETUP", g, Sw(2, "short", ts=51.0)) == []
    assert g.confirm_now(51.0) == im.A_LAPTOP
    # ...nor a risky row: with nothing proposed its push only says to turn E3
    g = Focus(item=idx)
    map_event("SETUP", g, Sw(2, "short", ts=50.0))
    g.item = _item("mono_subs")
    assert map_event("SETUP", g, Sw(2, "short", ts=51.0), state=st.State()) == \
        [ShowHint(im.HINT_TURN_VALUE)]
    assert g.pick_now(51.0) is None


def test_setup_laptop_access_opens_on_a_double_e3_push_and_closes_on_one():
    """SETUP > Laptop access emits SetupAction("laptop_access") after its double E3 push (the
    opening: the first opens the popup); while the Controller marks it open (Focus.no_confirm)
    one E3 push does (the closing, the safe direction)."""
    f = Focus(item=_item(im.A_LAPTOP))
    assert map_event("SETUP", f, Sw(2, "short", ts=1.0)) == []                  # the popup
    assert f.confirm == im.A_LAPTOP and im.setup_popup(f, 1.0) == ("confirm", None)
    assert map_event("SETUP", f, Sw(2, "short", ts=2.0)) == [im.SetupAction(im.A_LAPTOP)]
    f.no_confirm.add(im.A_LAPTOP)                                  # open: one push closes
    assert map_event("SETUP", f, Sw(2, "short", ts=3.0)) == [im.SetupAction(im.A_LAPTOP)]
    assert f.confirm is None
    f.no_confirm.discard(im.A_LAPTOP)
    assert map_event("SETUP", f, Sw(1, "short", ts=4.0)) == [ShowHint(im.HINT_PUSH_TWICE)]
    assert map_event("SETUP", f, Sw(2, "short", ts=4.0)) == []
    assert map_event("SETUP", f, Sw(2, "short", ts=10.0)) == []   # expired: the popup again
    assert f.confirm_now(10.0) == im.A_LAPTOP


def test_setup_action_items_beyond_reset_and_undo(monkeypatch):
    """A later SETUP action only needs its SetupItem: input_map emits SetupAction(name) on an E3
    push, after its popup when it has a confirm; a waived confirm (no_confirm) is per item."""
    extra = im.SetupItem("Other", "other_action", "action", confirm=True)
    monkeypatch.setattr(im, "SETUP_ITEMS", im.SETUP_ITEMS[:-2] + (extra,) + im.SETUP_ITEMS[-2:])
    f = Focus(item=_item("other_action"), no_confirm={im.A_LAPTOP})
    assert map_event("SETUP", f, Sw(2, "short", ts=1.0)) == []
    assert im.setup_popup(f, 1.0) == ("confirm", None)
    assert map_event("SETUP", f, Sw(2, "short", ts=2.0)) == [im.SetupAction("other_action")]


@pytest.mark.parametrize("field", EVERYDAY_ON_OFF)
def test_setup_everyday_on_off_applies_as_e3_turns_by_direction(field):
    """D-017: an ON/off that is not risky (Low-pass, Spectrum) is set as E3 turns: right = ON,
    left = off, through SetChoice (the controller's path); a turn towards the value already held
    only says so, so a fast turn never toggles back and forth; never a popup; E3's push sets
    nothing."""
    for cur in (False, True):
        s = st.State.from_dict({**st.State().to_dict(), field: cur})
        f = Focus(item=_item(field))
        right = map_event("SETUP", f, Enc(2, 1, ts=1.0), state=s)
        left = map_event("SETUP", f, Enc(2, -1, ts=2.0), state=s)
        assert right == ([ShowHint("ALREADY ON")] if cur else [SetChoice(field, True)]), cur
        assert left == ([SetChoice(field, False)] if cur else [ShowHint("ALREADY OFF")]), cur
        assert map_event("SETUP", f, Enc(2, 5, ts=3.0), state=s) == right     # fast: one step
        assert f.pick is None and im.setup_popup(f, 3.0) is None               # never a popup
        assert map_event("SETUP", f, Sw(2, "short", ts=4.0), state=s) == \
            [ShowHint(im.HINT_TURN_VALUE)]
        f.held.add(2)                                    # E3's push held: the same, no fine here
        assert map_event("SETUP", f, Enc(2, 1, ts=5.0), state=s) == right


@pytest.mark.parametrize("field", sorted(D017_RISKY))
def test_setup_every_risky_row_proposes_on_a_turn_and_applies_only_on_an_e3_push(field):
    """D-017: an E3 turn on a risky row opens the popup (`now -> proposed`) and sets nothing,
    however many turns; E1/E2 pushes leave it open and only say to push E3; the E3 push applies
    exactly the proposal. An E1 or an E2 turn cancels it (and moves nothing); so does the end of
    PICK_WINDOW_S without a turn."""
    s = st.State()
    i = _item(field)
    it = im.SETUP_ITEMS[i]
    cur = im.setup_value_of(s, it)
    d = 1 if cur != it.choices[-1] else -1
    want = it.choices[it.choices.index(cur) + d]
    f = Focus(item=i)
    assert map_event("SETUP", f, Enc(2, d, ts=1.0), state=s) == []
    assert im.setup_popup(f, 1.0) == ("pick", want)
    for n in (0, 1):
        assert map_event("SETUP", f, Sw(n, "short", ts=1.5), state=s) == \
            [ShowHint(im.HINT_PUSH_APPLY)]
    assert im.setup_popup(f, 1.5) == ("pick", want)
    assert map_event("SETUP", f, Sw(2, "short", ts=2.0), state=s) == [SetChoice(field, want)]
    assert im.setup_popup(f, 2.0) is None and f.pick is None
    for enc in (0, 1):                                            # E1 / E2 turn: cancelled
        g = Focus(item=i)
        map_event("SETUP", g, Enc(2, d, ts=1.0), state=s)
        assert map_event("SETUP", g, Enc(enc, 1, ts=1.2), state=s) == [ShowHint(im.HINT_CANCELLED)]
        assert g.item == i and im.setup_popup(g, 1.2) is None
        assert map_event("SETUP", g, Sw(2, "short", ts=1.4), state=s) == \
            [ShowHint(im.HINT_TURN_VALUE)]
    g = Focus(item=i)                                             # timeout: nothing applied
    map_event("SETUP", g, Enc(2, d, ts=1.0), state=s)
    assert map_event("SETUP", g, Sw(2, "short", ts=1.0 + im.PICK_WINDOW_S), state=s) == \
        [ShowHint(im.HINT_TURN_VALUE)]


def test_setup_push_after_a_fine_turn_is_not_a_select():
    """The SHOW fine-mode habit (hold E3's push, turn, release fast = 'short') must never apply a
    risky row's proposal (a polarity, Mono L+R: a rebuild) or open a page. The turn proposed
    (the popup shows it); only a later plain E3 push applies."""
    s = st.State()
    f = Focus(item=_item("sub_inverted"))
    assert map_event("SETUP", f, Sw(2, "press", ts=1.0), state=s) == []
    assert map_event("SETUP", f, Enc(2, 1, ts=1.1), state=s) == []
    assert f.pick_now(1.1) == ("sub_inverted", True)                     # proposed, not set
    assert map_event("SETUP", f, Sw(2, "short", ts=1.3), state=s) == []  # no SetChoice
    assert f.pick_now(1.3) == ("sub_inverted", True)                     # still waiting
    assert map_event("SETUP", f, Sw(2, "press", ts=2.0), state=s) == []  # a plain push sets it
    assert map_event("SETUP", f, Sw(2, "short", ts=2.1), state=s) == [SetChoice("sub_inverted", True)]
    # Mono L+R (a pipeline rebuild): the same, never one gesture
    g = Focus(item=_item("mono_subs"))
    assert map_event("SETUP", g, Sw(2, "press", ts=1.0), state=s) == []
    assert map_event("SETUP", g, Enc(2, 1, ts=1.1), state=s) == []
    assert map_event("SETUP", g, Sw(2, "short", ts=1.3), state=s) == []
    # a page row: the fine release does not open it, a plain push does
    h = Focus(item=_item("HWTEST"))
    map_event("SETUP", h, Sw(2, "press", ts=1.0), state=s)
    assert map_event("SETUP", h, Enc(2, 1, ts=1.1), state=s) == [ShowHint(im.HINT_PUSH_OPEN)]
    assert map_event("SETUP", h, Sw(2, "short", ts=1.3), state=s) == []
    map_event("SETUP", h, Sw(2, "press", ts=2.0), state=s)
    assert map_event("SETUP", h, Sw(2, "short", ts=2.1), state=s) == [GotoPage("HWTEST")]


@pytest.mark.parametrize("field", ["mono_subs", "sub_inverted", "lowcut_on", "aux_mode"])
def test_setup_a_detent_clicked_by_the_pressing_finger_never_applies(field):
    """D-017 review: E3 clicks one detent as the finger presses it (turn at t, press 5 ms
    later): that turn opens the popup, and the same push must not apply it, the popup never
    seen. The push is refused (PUSH E3 TO APPLY), the popup stays, the next push applies. A
    proposal moved again just before a press is refused the same way; a press PICK_SETTLE_S
    after the last move applies."""
    s = st.State()
    i = _item(field)
    it = im.SETUP_ITEMS[i]
    cur = im.setup_value_of(s, it)
    d = 1 if cur != it.choices[-1] else -1
    want = it.choices[it.choices.index(cur) + d]
    f = Focus(item=i)
    assert map_event("SETUP", f, Enc(2, d, ts=1.0), state=s) == []
    assert map_event("SETUP", f, Sw(2, "press", ts=1.005), state=s) == []
    assert map_event("SETUP", f, Sw(2, "release", ts=1.15), state=s) == []
    assert map_event("SETUP", f, Sw(2, "short", ts=1.15), state=s) == [ShowHint(im.HINT_PUSH_APPLY)]
    assert im.setup_popup(f, 1.2) == ("pick", want)                      # still open, nothing set
    assert map_event("SETUP", f, Sw(2, "press", ts=1.6), state=s) == []
    assert map_event("SETUP", f, Sw(2, "short", ts=1.7), state=s) == [SetChoice(field, want)]
    g = Focus(item=i)                                    # opened earlier, moved just before the press
    map_event("SETUP", g, Enc(2, d, ts=1.0), state=s)
    map_event("SETUP", g, Enc(2, -d, ts=2.0), state=s)                  # back: closed
    map_event("SETUP", g, Enc(2, d, ts=2.1), state=s)                   # open again
    map_event("SETUP", g, Sw(2, "press", ts=2.1 + im.PICK_SETTLE_S - 0.01), state=s)
    assert map_event("SETUP", g, Sw(2, "short", ts=2.5), state=s) == [ShowHint(im.HINT_PUSH_APPLY)]
    map_event("SETUP", g, Sw(2, "press", ts=2.1 + im.PICK_SETTLE_S), state=s)
    assert map_event("SETUP", g, Sw(2, "short", ts=2.6), state=s) == [SetChoice(field, want)]


@pytest.mark.parametrize("field,want_not", [("mono_subs", SetChoice), ("lowcut_on", SetChoice),
                                            ("HWTEST", GotoPage), (im.A_UNDO_RESET, UndoReset)])
@pytest.mark.parametrize("push", [0, 1, 2])
def test_setup_fine_turn_release_with_the_drivers_event_order_is_not_a_select(field, want_not, push):
    """The same habit with the events exactly as the driver sends them: press, turn, RELEASE,
    SHORT (Switch.tick appends RELEASE then SHORT on a release before long_ms). Found by the
    D-013 review: the release used to forget the turn, so the short set the value the turn had
    just chosen (one gesture). A plain E3 push after it acts; E1/E2 pushes never do (D-017)."""
    s = st.State()
    f = Focus(item=_item(field))
    it = im.SETUP_ITEMS[f.item]
    d = -1 if it.picks and im.setup_value_of(s, it) == it.choices[-1] else 1   # towards a change
    acts = []
    for ev in (Sw(push, "press", ts=1.0), Enc(push, d, ts=1.1), Sw(push, "release", ts=1.3),
               Sw(push, "short", ts=1.3)):
        acts += map_event("SETUP", f, ev, state=s)
    assert not any(isinstance(a, want_not) for a in acts), acts
    acts = []
    for ev in (Sw(push, "press", ts=2.0), Sw(push, "release", ts=2.1), Sw(push, "short", ts=2.1)):
        acts += map_event("SETUP", f, ev, state=s)
    if push != 2:                             # E1 / E2: a hint, never an action
        assert all(isinstance(a, ShowHint) for a in acts), acts
    elif want_not is SetChoice:               # E3's turn proposed; a plain E3 push applies it
        assert any(isinstance(a, SetChoice) for a in acts), acts
    else:
        assert any(isinstance(a, want_not) for a in acts), acts


def test_phone_fine_turn_release_is_not_a_toggle():
    """PHONE: hold E1, turn (next QR), let go with the driver's order: no phone on/off."""
    f = Focus()
    acts = []
    for ev in (Sw(0, "press", ts=1.0), Enc(0, 1, ts=1.1), Sw(0, "release", ts=1.3),
               Sw(0, "short", ts=1.3)):
        acts += map_event("PHONE", f, ev)
    assert not any(isinstance(a, im.PhoneToggle) for a in acts), acts
    assert map_event("PHONE", f, Sw(0, "press", ts=2.0)) == []
    map_event("PHONE", f, Sw(0, "release", ts=2.1))
    assert map_event("PHONE", f, Sw(0, "short", ts=2.1)) == [im.PhoneToggle()]


@pytest.mark.parametrize("page", ["SHOW", "SETUP", "PHONE"])
def test_b2_hold_off_align_says_where_mute_went(page):
    """B2 was the mute button until D-013: holding it off ALIGN shows HINT_MUTE and mutes
    nothing; on ALIGN the hold still unlocks."""
    assert map_event(page, Focus(), Sw(5, "long"), muted=False) == [ShowHint(im.HINT_MUTE)]
    assert im.HINT_MUTE == "HOLD B4 TO MUTE / UNMUTE"
    acts = map_event("ALIGN", Focus(), Sw(5, "long"), muted=False)
    assert len(acts) == 1 and isinstance(acts[0], im.Unlock)


def test_laptop_access_open_says_one_push():
    """While Laptop access is open one E3 push closes it: the turn hint and the bottom line say so."""
    f = Focus(item=_item(im.A_LAPTOP))
    assert map_event("SETUP", f, Enc(2, 1)) == [ShowHint(im.HINT_PUSH_TWICE)]
    f.no_confirm.add(im.A_LAPTOP)
    assert map_event("SETUP", f, Enc(2, 1)) == [ShowHint(im.HINT_PUSH_RUN)]
    _img, drawn = _drawn("SETUP", st.State(), None, f)
    texts = [t for t, *_ in drawn]
    assert "push E3 = run" in texts and "push E3 ×2 = run" not in texts


def test_a_choice_on_a_field_turned_read_only_is_not_drawn_pending():
    """A proposal waits on Audition, then the profile leaves dev (read-only): the popup closes;
    the E3 push only shows the hint."""
    inf = float("inf")
    f = Focus(item=_item("dev_audition"), pick="dev_audition", pick_value="tops", pick_until=inf)
    c = _setup_ctx(st.State(), None, f)
    assert c.popup is not None and c.popup["big"] == "SUM → TOPS"
    f.readonly = {"dev_audition": "AUDITION: DEV PROFILE ONLY"}
    assert _setup_ctx(st.State(), None, f).popup is None
    img, drawn = _drawn("SETUP", st.State(), None, f)
    assert not any("→" in t for t, *_ in drawn) and not _popup_edge(img)
    assert map_event("SETUP", f, Sw(2, "short"), state=st.State()) == [
        ShowHint("AUDITION: DEV PROFILE ONLY")]


def test_slot_and_item_follow_every_detent():
    """A fast turn reaches the controller as one merged event: every detent counts."""
    assert map_event("SETUP", Focus(), Enc(0, -2)) == [SelectItem(-2)]
    assert map_event("SETUP", Focus(), Enc(0, 3)) == [SelectItem(3)]
    f = Focus()
    apply_focus(f, SelectSlot(3))
    assert f.slot == 3


@pytest.mark.parametrize("page", im.ALL_PAGES)
def test_b4_long_mutes_on_every_page_with_an_explicit_target(page):
    """The dispatcher passes the displayed mute; B4 long (the mute button since D-013, sw7)
    asks for its opposite, on every page, the Knob test and PHONE included. A short press only
    says how (in the Knob test it is only logged); no other button mutes."""
    assert map_event(page, Focus(), Sw(7, "long"), muted=False) == [Mute(True)]
    assert map_event(page, Focus(), Sw(7, "long"), muted=True) == [Mute(False)]
    assert map_event(page, Focus(), Sw(7, "long"), muted=np.True_) == [Mute(False)]
    assert map_event(page, Focus(), Sw(7, "long")) == [Mute("toggle")]     # unknown
    assert im.mute_action(np.array([True, False])) == Mute("toggle")
    f = Focus()
    assert map_event(page, f, Sw(7, "press"), muted=False) == []
    assert map_event(page, f, Sw(7, "release"), muted=False) == []
    assert map_event(page, f, Sw(7, "short"), muted=False) == \
        ([] if page == "HWTEST" else [ShowHint(im.HINT_MUTE)])
    for sw in (4, 5, 6):
        for k in ("press", "short", "long", "release"):
            assert not any(isinstance(a, Mute) for a in
                           map_event(page, Focus(), Sw(sw, k), muted=False)), (sw, k)


@pytest.mark.parametrize("page", im.ALL_PAGES)
def test_b4_acts_on_what_its_press_saw(page):
    """B4's long fires 0.8 s after its press, still held: it acts on the mute the operator saw
    when the finger went down. Started unmuted it always MUTES (a phone muting meanwhile must
    not turn it into an unmute); started muted it unmutes with the generation it saw."""
    f = Focus()
    assert map_event(page, f, Sw(7, "press"), muted=False, mute_seq=3) == []
    assert map_event(page, f, Sw(7, "long"), muted=True, mute_seq=4) == [Mute(True)]
    assert f.b2_seen is None
    assert map_event(page, f, Sw(7, "press"), muted=True, mute_seq=5) == []
    assert map_event(page, f, Sw(7, "long"), muted=False, mute_seq=6) == [Mute(False, 5)]
    # a short press forgets what it saw: a later long without a press acts on the screen now
    map_event(page, f, Sw(7, "press"), muted=True, mute_seq=7)
    map_event(page, f, Sw(7, "release"), muted=True, mute_seq=7)
    map_event(page, f, Sw(7, "short"), muted=True, mute_seq=7)
    assert f.b2_seen is None
    assert map_event(page, f, Sw(7, "long"), muted=False) == [Mute(True)]


def test_setup_navigation_and_values():
    """SETUP knobs (D-017): E1 ITEM (one item per detent, clamps), E2 GROUP, E3 VALUE (turn =
    change, push = set / open / run), E4 unused."""
    assert im.knob_labels("SETUP") == ["ITEM", "GROUP", "VALUE", ""]
    s = st.State()
    f = Focus()
    acts = map_event("SETUP", f, Enc(0, 1), state=s)
    assert acts == [SelectItem(1)]
    apply_focus(f, acts[0])
    assert f.item == 1 and im.SETUP_ITEMS[1].field == "out_trim_db.3"      # TOPS › Trim R
    assert map_event("SETUP", f, Enc(2, -2), state=s) == [Nudge("out_trim_db.3", -2, False)]
    assert map_event("SETUP", f, Sw(2, "short"), state=s) == [ShowHint(im.HINT_TURN_VALUE)]
    assert map_event("SETUP", f, Enc(1, -2), state=s) == [SelectGroup(-1)]   # E2: not a value
    apply_focus(f, SelectItem(1))
    assert im.SETUP_ITEMS[f.item].field == "top_inverted" and f.pick is None
    assert map_event("SETUP", f, Enc(2, 1, ts=5.0), state=s) == []        # proposes (risky)
    assert f.pick_now(5.0) == ("top_inverted", True)
    assert map_event("SETUP", f, Sw(2, "short", ts=6.0), state=s) == [SetChoice("top_inverted", True)]
    assert map_event("SETUP", f, Enc(1, 1), state=s) == [SelectGroup(1)]  # E2: the next group
    assert map_event("SETUP", f, Enc(3, 1), state=s) == []                # E4 unused on SETUP
    assert map_event("SETUP", f, Sw(3, "short"), state=s) == []
    f.item = _item("input_trim_db")
    assert map_event("SETUP", f, Enc(2, 3), state=s) == [Nudge("input_trim_db", 3, False)]
    apply_focus(f, SelectItem(-99))
    assert f.item == 0
    apply_focus(f, SelectItem(99))
    assert f.item == len(im.SETUP_ITEMS) - 1
    assert map_event("SETUP", f, Sw(2, "short"), state=s) == [GotoPage("HWTEST")]
    f.item = _item("PHONE")
    assert map_event("SETUP", f, Sw(2, "short"), state=s) == [GotoPage("PHONE")]


def test_setup_e3_moves_a_number_per_detent_fine_while_its_push_is_held():
    """D-017: E3 turns a number one Nudge per detent (a merged fast turn: every detent, never
    accelerated); E3's push held while turning = fine steps (the per-encoder rule: another
    knob's push held does not make E3 fine); the release after it shows nothing."""
    s = st.State()
    fld = "out_trim_db.0"
    f = Focus(item=_item(fld))
    assert map_event("SETUP", f, Enc(2, 1, ts=1.0), state=s) == [Nudge(fld, 1, False)]
    assert map_event("SETUP", f, Enc(2, -3, ts=1.01), state=s) == [Nudge(fld, -3, False)]
    for k in range(6):
        assert map_event("SETUP", f, Enc(2, -1, ts=1.02 + k * 0.01), state=s) == \
            [Nudge(fld, -1, False)]
    assert map_event("SETUP", f, Sw(2, "press", ts=2.0), state=s) == []
    assert map_event("SETUP", f, Enc(2, 2, ts=2.1), state=s) == [Nudge(fld, 2, True)]
    assert map_event("SETUP", f, Sw(2, "release", ts=2.3), state=s) == []
    assert map_event("SETUP", f, Sw(2, "short", ts=2.3), state=s) == []   # end of a fine turn
    assert map_event("SETUP", f, Enc(2, 1, ts=3.0), state=s) == [Nudge(fld, 1, False)]
    map_event("SETUP", f, Sw(0, "press", ts=4.0), state=s)                # E1's push held
    assert map_event("SETUP", f, Enc(2, 1, ts=4.1), state=s) == [Nudge(fld, 1, False)]
    for d016 in ("deharsh_hz", "loud_boost_db", "aux_gain_db"):           # D-016's numbers too
        g = Focus(item=_item(d016))
        assert map_event("SETUP", g, Enc(2, 2, ts=1.0), state=s) == [Nudge(d016, 2, False)]


@pytest.mark.parametrize("item", range(len(im.SETUP_ITEMS)),
                         ids=[f"{it.group}-{it.label}" for it in im.SETUP_ITEMS])
def test_setup_e1_and_e2_pushes_act_on_nothing_and_point_to_e3(item):
    """D-017: on SETUP only E3's push sets, opens or runs. An E1 or E2 push emits no action but
    a hint naming the E3 gesture the row takes (or, on a read-only row, why it is fixed), and
    leaves a popup as it was."""
    s = st.State()
    it = im.SETUP_ITEMS[item]
    want = (im.HINT_TURN_VALUE if it.kind in ("num", "bool", "choice") else
            im.HINT_PUSH_OPEN if it.kind == "page" else
            im.HINT_PUSH_TWICE if it.needs_confirm else im.HINT_PUSH_RUN)
    for n in (0, 1):
        f = Focus(item=item)
        acts = []
        for kind in ("press", "release", "short"):
            acts += map_event("SETUP", f, Sw(n, kind, ts=1.0), state=s)
        assert acts == [ShowHint(want)], (n, acts)
        assert f.pick is None and f.confirm is None and f.item == item
    if it.risky or it.needs_confirm:                  # with its popup open: still only a hint
        f = _setup_mode_focus(item, "pick" if it.risky else "confirm", s)
        before = (f.pick, f.pick_value, f.confirm)
        for n in (0, 1):
            assert map_event("SETUP", f, Sw(n, "short", ts=1.0), state=s) == \
                [ShowHint(im.HINT_PUSH_APPLY if it.risky else im.HINT_PUSH_RUN)]
        assert (f.pick, f.pick_value, f.confirm) == before


def test_e2_jumps_one_group_per_event_and_clamps():
    """E2 on SETUP (D-017; E3 under D-013): one group per event however fast the turn; forward =
    the first item of the next group; backward from inside a group = its own first item first;
    clamps at both ends. A jump drops a pending proposal and a pending confirm; with the popup
    open the turn only cancels it."""
    names = im.SETUP_GROUP_NAMES
    first = [next(i for i, it in enumerate(im.SETUP_ITEMS) if it.group == g) for g in names]
    f = Focus()
    seen = []
    for k, d in enumerate((1, 3, 1, 7, 1, 1, 2)):
        acts = map_event("SETUP", f, Enc(1, d, ts=k * 0.01))
        assert acts == [SelectGroup(1)], d
        assert apply_focus(f, acts[0]) is True
        seen.append(im.SETUP_ITEMS[f.item].group)
    assert seen == ["SUBS", "INPUT", "LIVE EQ", "PHONE", "BOX", "BOX", "BOX"]
    assert f.item == first[-1]
    f.item = _item("dev_audition")                                         # BOX 2/3
    back = []
    for k in range(7):
        acts = map_event("SETUP", f, Enc(1, -3, ts=1.0 + k * 0.01))
        assert acts == [SelectGroup(-1)]
        apply_focus(f, acts[0])
        back.append(f.item)
    assert back == [first[5], first[4], first[3], first[2], first[1], first[0], first[0]]
    # inside a group, back once = that group's top; from its top, the previous group's top
    f.item = _item("mono_subs")
    apply_focus(f, SelectGroup(-1))
    assert f.item == first[1]
    apply_focus(f, SelectGroup(-1))
    assert f.item == first[0]
    # a jump clears a pending proposal and a pending confirm
    g = Focus(item=_item("mono_subs"), pick="mono_subs", pick_value=True, pick_until=INF,
              confirm="reset_live", confirm_until=INF)
    apply_focus(g, SelectGroup(1))
    assert im.SETUP_ITEMS[g.item].group == "INPUT"
    assert g.pick is None and g.pick_now(0.0) is None and g.confirm is None
    # with the popup open, E2 cancels it and jumps nowhere
    g = Focus(item=_item("mono_subs"), pick="mono_subs", pick_value=True, pick_until=INF)
    assert map_event("SETUP", g, Enc(1, -1, ts=0.0)) == [ShowHint(im.HINT_CANCELLED)]
    assert g.pick is None and g.item == _item("mono_subs")


def test_setup_e3_proposal_steps_by_direction_stops_at_the_ends_and_closes_on_the_current():
    """D-017 on a risky row: E3 right = the next choice, left = the previous one, from the
    proposal shown, else from the current value; it stops at both ends (never wraps: a fast turn
    lands where it points); back on the current value the popup closes; at an end with no popup
    a hint says the row is already there. A turn alone never sets anything."""
    s = st.State()                                                         # audition "sum"
    f = Focus(item=_item("dev_audition"))
    steps = [(1, "tops", []), (1, "subs", []), (1, "subs", []), (-1, "tops", []), (-1, None, []),
             (-1, None, [ShowHint("ALREADY SUM")])]
    for k, (d, v, acts) in enumerate(steps):
        t = 1.0 + k
        assert map_event("SETUP", f, Enc(2, d, ts=t), state=s) == acts, k
        assert f.pick_now(t) == (None if v is None else ("dev_audition", v)), k
        if v is None:
            assert f.pick is None
    # an ON/off: right = ON (INVERTED), left = off (normal), whatever was proposed before
    g = Focus(item=_item("top_inverted"))
    assert map_event("SETUP", g, Enc(2, -1, ts=1.0), state=s) == [ShowHint("ALREADY NORMAL")]
    assert map_event("SETUP", g, Enc(2, 1, ts=1.1), state=s) == []
    assert g.pick_now(1.1) == ("top_inverted", True)
    assert map_event("SETUP", g, Enc(2, 1, ts=1.2), state=s) == []        # stays INVERTED
    assert g.pick_now(1.2) == ("top_inverted", True)
    assert map_event("SETUP", g, Enc(2, -1, ts=1.3), state=s) == []       # back: closed
    assert g.pick_now(1.3) is None
    # it reads the state it is given: the current value is where the stepping starts
    h = Focus(item=_item("dev_audition"))
    assert map_event("SETUP", h, Enc(2, 1, ts=1.0), state=st.State(dev_audition="subs")) == \
        [ShowHint("ALREADY SUBS")]
    map_event("SETUP", h, Enc(2, -1, ts=1.1), state=st.State(dev_audition="subs"))
    assert h.pick_now(1.1) == ("dev_audition", "tops")
    # a value left proposed for ANOTHER field is not the start (nor applied by this row's push)
    k = Focus(item=_item("sub_inverted"), pick="mono_subs", pick_value=True, pick_until=INF)
    map_event("SETUP", k, Enc(2, 1, ts=1.0), state=s)
    assert k.pick_now(1.0) == ("sub_inverted", True)
    k = Focus(item=_item("sub_inverted"), pick="mono_subs", pick_value=True, pick_until=INF)
    assert map_event("SETUP", k, Sw(2, "short", ts=1.0), state=s) == [ShowHint(im.HINT_TURN_VALUE)]


def test_setup_one_choice_per_event_however_fast():
    """A fast turn reaches map_event as ONE merged event (delta 3, 5...): it moves the proposal
    one step, in its direction, never 3 steps past what the operator could read."""
    s = st.State()
    f = Focus(item=_item("dev_audition"))
    assert map_event("SETUP", f, Enc(2, 3, ts=1.0), state=s) == []
    assert f.pick_now(1.0) == ("dev_audition", "tops")
    map_event("SETUP", f, Enc(2, 5, ts=1.01), state=s)
    assert f.pick_now(1.01) == ("dev_audition", "subs")
    map_event("SETUP", f, Enc(2, -4, ts=1.02), state=s)
    assert f.pick_now(1.02) == ("dev_audition", "tops")
    g = Focus(item=_item("mono_subs"))
    map_event("SETUP", g, Enc(2, 2, ts=1.0), state=s)                      # 2 detents: still ON
    assert g.pick_now(1.0) == ("mono_subs", True)
    h = Focus(item=_item("aux_mode"))
    map_event("SETUP", h, Enc(2, 9, ts=1.0), state=s)                      # off -> MIC, not LINE
    assert h.pick_now(1.0) == ("aux_mode", "mic")


def test_setup_e3_push_applies_the_proposal_and_clears_it():
    s = st.State()
    f = Focus(item=_item("dev_audition"))
    map_event("SETUP", f, Enc(2, 1, ts=1.0), state=s)
    map_event("SETUP", f, Enc(2, 1, ts=1.2), state=s)
    for n in (0, 1):                                    # E1/E2 pushes never apply it
        assert map_event("SETUP", f, Sw(n, "short", ts=1.5), state=s) == \
            [ShowHint(im.HINT_PUSH_APPLY)]
    acts = map_event("SETUP", f, Sw(2, "short", ts=2.0), state=s)
    assert acts == [SetChoice("dev_audition", "subs")]
    assert f.pick is None and f.pick_now(2.0) is None
    assert apply_focus(f, acts[0], now=2.0) is False                       # the controller sets it
    assert f.editing_now(2.1) == "dev_audition"
    # the next push applies nothing again: it says to turn E3
    assert map_event("SETUP", f, Sw(2, "short", ts=3.0), state=s) == [ShowHint(im.HINT_TURN_VALUE)]
    assert f.pick_now(3.0) is None


def test_setup_choice_expires_after_pick_window_and_each_turn_renews_it():
    s = st.State()
    assert im.PICK_WINDOW_S == 8.0
    f = Focus(item=_item("dev_audition"))
    map_event("SETUP", f, Enc(2, 1, ts=10.0), state=s)
    assert f.pick_until == pytest.approx(10.0 + im.PICK_WINDOW_S)
    map_event("SETUP", f, Enc(2, 1, ts=15.0), state=s)                     # renewed by the turn
    assert f.pick_now(15.0 + im.PICK_WINDOW_S - 0.1) == ("dev_audition", "subs")
    assert f.pick_now(15.0 + im.PICK_WINDOW_S) is None                     # expired
    # an E3 push after the window applies nothing
    assert map_event("SETUP", f, Sw(2, "short", ts=23.5), state=s) == [ShowHint(im.HINT_TURN_VALUE)]
    assert f.pick_now(23.5) is None
    # a turn after the window starts from the current value, not from the stale proposal
    g = Focus(item=_item("dev_audition"))
    map_event("SETUP", g, Enc(2, 1, ts=0.0), state=s)
    map_event("SETUP", g, Enc(2, 1, ts=1.0), state=s)                      # subs, until 9
    map_event("SETUP", g, Enc(2, 1, ts=30.0), state=s)
    assert g.pick_now(30.0) == ("dev_audition", "tops")


def test_setup_e1_or_e2_turn_cancels_the_popup_and_moves_nothing():
    """D-017: the popup says `turn E1 = cancel`. An E1 (or E2) turn closes it, applies nothing
    and leaves the row selected; the next turn moves as usual. Moving otherwise (apply_focus,
    a page change in the controller) drops a proposal too."""
    s = st.State()
    mono = _item("mono_subs")
    for enc, d in ((0, 1), (0, -2), (1, 1), (1, -1)):
        f = Focus(item=mono)
        map_event("SETUP", f, Enc(2, 1, ts=1.0), state=s)
        assert im.setup_popup(f, 1.0) == ("pick", True)
        assert map_event("SETUP", f, Enc(enc, d, ts=1.5), state=s) == [ShowHint(im.HINT_CANCELLED)]
        assert f.pick is None and f.item == mono and im.setup_popup(f, 1.5) is None
        assert map_event("SETUP", f, Sw(2, "short", ts=2.0), state=s) == \
            [ShowHint(im.HINT_TURN_VALUE)]                                 # nothing to apply
        acts = map_event("SETUP", f, Enc(enc, d, ts=2.5), state=s)
        assert acts == ([SelectItem(d)] if enc == 0 else [SelectGroup(1 if d > 0 else -1)])
    f = Focus(item=mono, pick="mono_subs", pick_value=True, pick_until=INF)
    apply_focus(f, SelectItem(1), now=1.0)
    assert f.pick is None and f.pick_now(1.0) is None
    # E4 and the buttons other than B1 leave it open (B4 may mute meanwhile)
    g = Focus(item=mono, pick="mono_subs", pick_value=True, pick_until=INF)
    map_event("SETUP", g, Enc(3, 1, ts=1.0), state=s)
    for sw in (5, 6, 7):
        for kind in ("press", "short", "long", "release"):
            map_event("SETUP", g, Sw(sw, kind, ts=1.0), state=s, muted=False)
    assert im.setup_popup(g, 1.0) == ("pick", True)


def test_setup_readonly_rows_only_show_their_hint():
    """Focus.readonly (refreshed by the Controller at every knob event): Profile pinned in
    k1000.yaml, Audition off the dev profile, Spectrum disabled in k1000.yaml. An E3 turn or any
    push there only says why; nothing is proposed, nothing set."""
    s = st.State()
    ro = {"profile": "PROFILE PINNED IN k1000.yaml", "dev_audition": "AUDITION: DEV PROFILE ONLY",
          im.F_SPECTRUM_ON: "SPECTRUM OFF IN k1000.yaml"}
    for field, hint in ro.items():
        f = Focus(item=_item(field), readonly=dict(ro))
        for ev in (Enc(2, 1, ts=1.0), Enc(2, -3, ts=1.5), Sw(2, "short", ts=2.0),
                   Sw(0, "short", ts=3.0), Sw(1, "short", ts=3.5)):
            assert map_event("SETUP", f, ev, state=s) == [ShowHint(hint)], (field, ev)
        assert f.pick is None and f.confirm is None
    # a proposal made before the field became read-only (a phone changed the profile) is not set
    f = Focus(item=_item("profile"))
    map_event("SETUP", f, Enc(2, 1, ts=1.0), state=s)
    assert f.pick_now(1.0) == ("profile", "m4")
    f.readonly = {"profile": ro["profile"]}
    assert map_event("SETUP", f, Sw(2, "short", ts=2.0), state=s) == [ShowHint(ro["profile"])]
    # the other rows are untouched by it
    g = Focus(item=_item("mono_subs"), readonly=dict(ro))
    assert map_event("SETUP", g, Enc(2, 1, ts=1.0), state=s) == []
    assert g.pick_now(1.0) == ("mono_subs", True)


def test_setup_knobs_say_what_a_row_wants():
    """A gesture with no meaning on a row says what that row takes (naming E3), and changes
    nothing."""
    s = st.State()
    assert (im.HINT_TURN_VALUE, im.HINT_PUSH_OPEN, im.HINT_PUSH_RUN, im.HINT_PUSH_TWICE,
            im.HINT_PUSH_APPLY, im.HINT_CANCELLED) == \
        ("TURN E3 TO CHANGE", "PUSH E3 TO OPEN", "PUSH E3 TO RUN", "PUSH E3 TWICE TO RUN",
         "PUSH E3 TO APPLY", "CANCELLED")
    for field in ("out_trim_db.2", "out_trim_db.0", "sub_lowpass_hz", "input_trim_db"):
        f = Focus(item=_item(field))
        for n in (0, 1, 2):
            assert map_event("SETUP", f, Sw(n, "short"), state=s) == [ShowHint(im.HINT_TURN_VALUE)]
        assert f.pick is None and f.confirm is None
    for field, hint in (("PHONE", im.HINT_PUSH_OPEN), ("HWTEST", im.HINT_PUSH_OPEN),
                        (im.A_RESET_LIVE, im.HINT_PUSH_TWICE), (im.A_UNDO_RESET, im.HINT_PUSH_RUN),
                        (im.A_LAPTOP, im.HINT_PUSH_TWICE)):
        f = Focus(item=_item(field))
        for d in (1, -2):
            assert map_event("SETUP", f, Enc(2, d), state=s) == [ShowHint(hint)], field
        assert f.pick is None and f.confirm is None
    # the action's popup open: a turn says the next push runs it
    f = Focus(item=_item(im.A_RESET_LIVE), confirm=im.A_RESET_LIVE, confirm_until=INF)
    assert map_event("SETUP", f, Enc(2, 1), state=s) == [ShowHint(im.HINT_PUSH_RUN)]
    assert f.confirm == im.A_RESET_LIVE


@pytest.mark.parametrize("risky", ["d017", "list emptied"])
def test_setup_rebuild_is_never_applied_by_turns_alone(risky, monkeypatch):
    """A pipeline rebuild (a possible click) is turn + E3 push, never turns alone: however many
    turns, both ways, fast, or with E3's push held and released; even with SETUP_RISKY emptied
    (SetupItem.risky keeps every rebuild risky). The push applies the proposal within
    PICK_WINDOW_S; moving drops it."""
    if risky != "d017":
        monkeypatch.setattr(im, "SETUP_RISKY", frozenset())
    s = st.State()
    for field in sorted(st.REBUILD_FIELDS):
        i = _item(field)
        assert im.SETUP_ITEMS[i].risky, field
        f = Focus(item=i)
        acts, t = [], 1.0
        for d in (1, 1, -1, 3, -2, 1, 5, -9, 1):
            acts += map_event("SETUP", f, Enc(2, d, ts=t), state=s)
            t += 0.03
        acts += map_event("SETUP", f, Sw(2, "press", ts=t), state=s)
        for d in (1, -1, 1):
            acts += map_event("SETUP", f, Enc(2, d, ts=t), state=s)
            t += 0.03
        acts += map_event("SETUP", f, Sw(2, "release", ts=t), state=s)
        acts += map_event("SETUP", f, Sw(2, "short", ts=t), state=s)
        assert not any(isinstance(a, (SetChoice, Toggle)) for a in acts), (field, acts)
        target = _other_choice(im.SETUP_ITEMS[i], s)
        g = Focus(item=i)
        d = 1 if im.SETUP_ITEMS[i].choices.index(target) > 0 else -1
        assert map_event("SETUP", g, Enc(2, d, ts=10.0), state=s) == []
        assert g.pick_now(10.5) == (field, target) and g.confirm is None
        assert map_event("SETUP", g, Sw(2, "short", ts=11.0), state=s) == [SetChoice(field, target)]
        assert g.pick is None
        map_event("SETUP", g, Enc(2, d, ts=20.0), state=s)
        assert map_event("SETUP", g, Sw(2, "short", ts=20.0 + im.PICK_WINDOW_S), state=s) == \
            [ShowHint(im.HINT_TURN_VALUE)]                                # expired: nothing
        map_event("SETUP", g, Enc(2, d, ts=30.0), state=s)
        apply_focus(g, SelectItem(-1))                                    # moving cancels
        assert g.pick is None


def test_hwtest_records_and_exits_on_long_press():
    f = Focus()
    assert map_event("HWTEST", f, Enc(2, 3)) == []
    assert map_event("HWTEST", f, Sw(5, "press")) == []
    assert f.hw.enc_count[2] == 3 and f.hw.sw_down[5] is True
    assert f.hw.last.endswith("= B2")                             # sw5 is still named B2
    assert map_event("HWTEST", f, Sw(5, "short")) == []           # short presses only logged
    assert map_event("HWTEST", f, Sw(7, "press"), muted=False) == []
    assert f.hw.last.endswith("= B4")
    assert map_event("HWTEST", f, Sw(7, "short")) == []           # B4 short: logged, no hint
    assert map_event("HWTEST", f, Sw(7, "press"), muted=False) == []
    assert map_event("HWTEST", f, Sw(7, "long"), muted=False) == [Mute(True)]  # B4 still mutes
    assert f.hw.sw_last[7] == "long"                               # ...and is still logged
    assert map_event("HWTEST", f, Sw(5, "long")) == [GotoPage("SETUP")]   # B2: not the mute now
    assert map_event("HWTEST", f, Sw(4, "press")) == []
    assert map_event("HWTEST", f, Sw(4, "long")) == [GotoPage("SETUP")]
    assert map_event("HWTEST", f, Sw(0, "long")) == [GotoPage("SETUP")]   # any other switch
    assert len(f.hw.lines) <= 4


def test_hwtest_mute_follows_the_configured_b4():
    """The Knob test's 'mute on every page' rule follows the configured B4
    (cmap.switch_role == ('B', 3)), not a physical switch number."""
    cfg = {"controls": {"buttons": [7, 6, 5, 4]}}                  # B4 = sw4, B1 = sw7
    assert map_event("HWTEST", Focus(), Sw(4, "long"), cfg, muted=True) == [Mute(False)]
    assert map_event("HWTEST", Focus(), Sw(4, "long"), cfg, muted=False) == [Mute(True)]
    assert map_event("HWTEST", Focus(), Sw(7, "long"), cfg) == [GotoPage("SETUP")]
    assert map_event("HWTEST", Focus(), Sw(5, "long"), cfg) == [GotoPage("SETUP")]   # B3 there
    hw = Focus()
    map_event("HWTEST", hw, Sw(4, "press"), cfg)
    assert hw.hw.last.endswith("= B4") and hw.hw.sw_role[7] == "B1"
    # and on the other pages the same configured B4 is the mute
    assert map_event("SHOW", Focus(), Sw(4, "long"), cfg, muted=False) == [Mute(True)]
    assert map_event("SHOW", Focus(), Sw(7, "long"), cfg) == [GotoPage("SETUP")]     # B1 hold


def test_control_map_from_cfg():
    cfg = {"controls": {"encoders": [3, 2, 1, 0], "buttons": [0, 1, 2, 3],
                        "pushes": [4, 5, 6, 7], "reverse": [True, False, False, False]}}
    f = Focus(unlock_until=UNLOCKED)
    assert map_event("ALIGN", f, Enc(3, 1, ts=0.0), cfg) == [Nudge("master_db", -1, False)]
    assert map_event("ALIGN", f, Enc(0, 1, ts=5.0), cfg) == [Nudge("lowcut_hz", 1, False)]
    assert map_event("ALIGN", f, Sw(0, "press"), cfg) == []          # B1: the press does nothing
    assert map_event("ALIGN", f, Sw(0, "short"), cfg) == [GotoPage("SHOW")]   # a B1 tap
    assert map_event("ALIGN", f, Sw(3, "long"), cfg, muted=False) == [Mute(True)]   # B4
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


def test_align_e2_shows_size_big_and_side_in_words():
    """Subs 1.25 ms later and tops 1.25 ms later draw the same big value; only the sub-line
    (white while unlocked, grey while locked) tells them apart. D-015: the delay is E2, bottom
    left, its sub-line over it."""
    on = pages.ScreenStatus(align_locked=False, align_unlock_s=20.0, **RUN)
    value = (4, 214, pages.METER_X0 - 4, 276)
    subline = (4, 194, pages.METER_X0 - 4, 211)
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


def test_align_e2_is_the_signed_delay_knob():
    k = im.KNOBS["ALIGN"][1]                                      # D-015: E2 (E3 until then)
    assert (k.label, k.field, k.arm, k.locked, k.accel_up, k.accel_down) == \
        ("DELAY", im.F_ALIGN_DELAY, True, True, 1, 1)
    b = im.KNOBS["ALIGN"][2]
    assert (b.label, b.field, b.locked) == ("BALANCE", im.F_BALANCE, False)
    assert im.F_ALIGN_DELAY == st.ALIGN_DELAY
    lc = im.KNOBS["ALIGN"][3]
    assert (lc.field, lc.locked, lc.accel_up, lc.accel_down) == (im.F_LOWCUT_FREQ, True, 4, 4)
    assert im.HINT_UNLOCKED == "DELAY + LO-CUT UNLOCKED" and im.HINT_RELOCKED == "DELAY + LO-CUT LOCKED"
    f = Focus()                                                   # locked: nothing, as before
    assert map_event("ALIGN", f, Enc(1, -1, ts=1.0)) == [ShowHint(im.HINT_LOCKED)]
    f = Focus(unlock_until=UNLOCKED)
    assert map_event("ALIGN", f, Enc(1, -1, ts=1.0)) == [ShowHint(im.HINT_ARM.format("DELAY"))]
    assert map_event("ALIGN", f, Enc(1, -1, ts=1.2)) == [Nudge("align_delay_ms", -1, False)]
    assert map_event("ALIGN", f, Enc(1, -1, ts=1.21)) == [Nudge("align_delay_ms", -1, False)]
    assert map_event("ALIGN", f, Enc(1, -1, ts=1.22)) == [Nudge("align_delay_ms", -1, False)]  # x1
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


# --- D-013: SETUP by speaker floor; D-017: the fixed row, the popup, on screen ---------------
def _setup_help_text(drawn):
    """The bottom line's right side: what the knobs do on the selected row."""
    return [t for t, x, y, _f in drawn if x == pages.W - 10 and y == pages.H - 22]


@pytest.mark.parametrize("field,state_kw,proposed,big,what", [
    ("mono_subs", {}, True, "off → ON", "MAY CLICK"),
    ("dev_audition", {}, "tops", "SUM → TOPS", "MAY CLICK"),
    ("profile", {}, "m4", "DEV → M4", "MAY CLICK"),
    ("aux_mode", {}, "mic", "off → MIC", "MAY CLICK"),
    ("top_inverted", {}, True, "normal → INVERTED", "THE TOPS DIP A MOMENT"),
    ("sub_inverted", {}, True, "normal → INVERTED", "THE SUBS DIP A MOMENT"),
    ("lowcut_on", {}, False, "ON · 100 Hz → off", "FULL RANGE TO THE TOPS"),
    ("lowcut_on", {"lowcut_on": False, "lowcut_hz": 112.0}, True, "off → ON", "TOPS CUT BELOW 112 Hz"),
])
def test_setup_popup_shows_the_row_now_and_proposed_and_what_it_does(field, state_kw, proposed,
                                                                     big, what):
    """Thomas (2026-10-17): "a confirm popup for really sensitive settings (confirmed by e3
    push)". The popup names the row, shows `now → proposed` big, what it does (MAY CLICK on a
    rebuild), and the two ways out; it covers the selected row (red-edged: a push changes
    something), the footer stays free for hints. Expired, the row is plain again."""
    s = st.State(**state_kw)
    i = _item(field)
    it = im.SETUP_ITEMS[i]
    f = Focus(item=i, pick=field, pick_value=proposed, pick_until=10.0)
    c = _setup_ctx(s, pages.ScreenStatus(**RUN), f, now=5.0)
    big = big.replace("ON · 100 Hz", "ON")                 # the popup names the value alone
    assert {k: v for k, v in c.popup.items() if k != "px"} == {
        "title": f"{it.group} › {it.label}", "big": big, "what": what,
        "instr": "push E3 = apply · turn E1 = cancel"}
    img, drawn = _drawn("SETUP", s, pages.ScreenStatus(**RUN), f, now=5.0)
    assert _popup_edge(img)
    assert not any(t == it.label and x == SETUP_LABEL_X for t, x, _y, _f in drawn)   # covered
    assert _setup_footer(drawn) == [] and img.getpixel((5, pages.H - 5)) == pages.BG
    assert _setup_help_text(drawn) == ["turn E3 · push E3 = apply"]
    img, drawn = _drawn("SETUP", s, pages.ScreenStatus(**RUN), f, now=10.0)   # expired
    _y, fill, bar, val = _setup_selected(img, drawn, it.label)
    assert (bar, fill) == (pages.AMBER, pages.BG) and "→" not in val and not _popup_edge(img)
    assert _setup_ctx(s, pages.ScreenStatus(**RUN), f, now=10.0).popup is None


@pytest.mark.parametrize("item,mode", [(i, m) for i, m in SETUP_ROW_MODES if m != "plain"],
                         ids=[f"{im.SETUP_ITEMS[i].group}-{im.SETUP_ITEMS[i].label}-{m}"
                              for i, m in SETUP_ROW_MODES if m != "plain"])
def test_setup_popup_is_readable_and_covers_only_the_middle_of_the_list(item, mode):
    """Every popup (each risky row, each confirmed action): its texts inside the box at a size
    readable at arm's length (the value 28 px or more, the rest 14 or more); the box inside the
    list, clear of the status bar, the top line (E1 ITEM, the group, VALUE E3), the bottom line
    (E2 GROUP, the help) and the scroll bar: outside it the frame is pixel for pixel the one
    without the popup."""
    s = st.State()
    status = pages.ScreenStatus(**RUN)
    f = _setup_mode_focus(item, mode, s)
    c = _setup_ctx(s, status, f)
    px = c.popup["px"]
    assert px["big"] >= 28 and min(px["title"], px["instr"], px.get("what", 99)) >= 14, px
    x0, y0, x1, y1 = pages.SETUP_POPUP
    assert SETUP_LIST_Y[0] <= y0 and y1 <= SETUP_LIST_Y[1] and x1 < pages.W - 6
    for part, box in c.ink[pages.POPUP_INK].items():
        assert x0 + 3 <= box[0] and box[2] <= x1 - 3 and y0 + 3 <= box[1] and box[3] <= y1 - 3, \
            (part, box)
    with_ = pages.render("SETUP", s, status, f, now=0.0)
    without = pages.render("SETUP", s, status, Focus(item=item), now=0.0)
    outside = [(x, y) for y in list(range(0, y0)) + list(range(y1 + 1, pages.H))
               for x in range(0, pages.W, 3)]
    assert all(with_.getpixel(p) == without.getpixel(p) for p in outside)


def test_setup_popup_leaves_muted_its_footer_and_a_hint_takes_it():
    """Muted with a popup open: the MUTED strip keeps the footer (the popup says what a push
    does), inside the red frame the popup's own outline stays apart from it; a hint takes the
    footer for its 2.5 s, the popup stays. The action popup the same."""
    s = st.State(muted=True)
    status = pages.ScreenStatus(muted=True, **RUN)
    mono = _item("mono_subs")
    pick = dict(pick="mono_subs", pick_value=True, pick_until=INF)
    img, drawn = _drawn("SETUP", s, status, Focus(item=mono, **pick))
    assert _popup_edge(img) and img.getpixel((150, pages.H - 8)) == pages.BAND_RED
    footer = [t for t, _x, y, _f in drawn if y == SETUP_FOOTER_Y]
    assert "MUTED" in footer and "READY · hold B4 to unmute" in footer
    assert _muted_pill(img) and img.getpixel((2, 100)) == pages.BAND_RED       # still says MUTED
    x0, y0, x1, y1 = pages.SETUP_POPUP
    assert img.getpixel((x0 - 2, (y0 + y1) // 2)) == pages.BG                  # apart from the frame
    img, drawn = _drawn("SETUP", s, status, Focus(item=mono, hint="NO CHANGE: ALREADY SET",
                                                  hint_until=INF, **pick))
    assert _setup_footer(drawn) == ["NO CHANGE: ALREADY SET"] and _popup_edge(img)
    img, _d = _drawn("SETUP", s, status, Focus(item=_item(im.A_RESET_LIVE), confirm=im.A_RESET_LIVE,
                                               confirm_until=INF))
    assert _popup_edge(img) and img.getpixel((150, pages.H - 8)) == pages.BAND_RED


def test_setup_readonly_rows_are_drawn_fixed():
    """Profile pinned by k1000.yaml: 'DEV · PINNED'; Audition off the dev profile: '—';
    Spectrum disabled in k1000.yaml: 'DISABLED'. Label and value dimmed when not selected; the
    bottom line says 'fixed here' when selected. Free, a risky row's value is amber (D-017:
    amber = asks for an E3 push), an everyday one's white."""
    s = st.State()
    ro = {"profile": "PROFILE PINNED IN k1000.yaml", "dev_audition": "AUDITION: DEV PROFILE ONLY",
          im.F_SPECTRUM_ON: "SPECTRUM OFF IN k1000.yaml"}

    def row(drawn, label):
        (y, fill), = [(y, f) for t, x, y, f in drawn if t == label and x == SETUP_LABEL_X]
        (val, vfill), = [(t, f) for t, x, ty, f in drawn if x == SETUP_VALUE_X and ty == y]
        return fill, val, vfill

    _img, drawn = _drawn("SETUP", s, pages.ScreenStatus(**RUN), Focus(item=_item("HWTEST"), readonly=ro))
    assert row(drawn, "Profile") == (pages.DIM, "DEV · PINNED", pages.DIM)
    assert row(drawn, "Audition (dev)") == (pages.DIM, "—", pages.DIM)
    _img, drawn = _drawn("SETUP", s, pages.ScreenStatus(**RUN), Focus(item=_item("HWTEST")))
    assert row(drawn, "Profile") == (pages.FG, "DEV", pages.AMBER)            # free: risky
    assert row(drawn, "Audition (dev)") == (pages.FG, "SUM", pages.AMBER)
    _img, drawn = _drawn("SETUP", s, pages.ScreenStatus(**RUN), Focus(item=_item("sub_lowpass_hz")))
    assert row(drawn, "Mono L+R") == (pages.FG, "off", pages.AMBER)           # a rebuild
    assert row(drawn, "Polarity") == (pages.FG, "normal", pages.AMBER)        # SUBS › Polarity
    assert row(drawn, "Low-pass") == (pages.FG, "off", pages.FG)              # everyday: white
    _img, drawn = _drawn("SETUP", s, pages.ScreenStatus(**RUN), Focus(item=_item("top_inverted")))
    assert row(drawn, "Lo-Cut") == (pages.FG, "ON · 100 Hz", pages.AMBER)     # risky, no rebuild
    for field, val in (("profile", "DEV · PINNED"), ("dev_audition", "—"),
                       (im.F_SPECTRUM_ON, "DISABLED")):
        img, drawn = _drawn("SETUP", s, pages.ScreenStatus(**RUN), Focus(item=_item(field), readonly=ro))
        _y, fill, bar, shown = _setup_selected(img, drawn, im.SETUP_ITEMS[_item(field)].label)
        assert (bar, shown) == (pages.AMBER, val), field
        assert _setup_help_text(drawn) == ["fixed here"], field
    img, drawn = _drawn("SETUP", s, pages.ScreenStatus(**RUN), Focus(item=_item(im.F_SPECTRUM_ON)))
    assert _setup_selected(img, drawn, "Spectrum")[3] == "ON"
    assert _setup_help_text(drawn) == ["turn E3 = change"]


@pytest.mark.parametrize("item", range(len(im.SETUP_ITEMS)),
                         ids=[f"{it.group}-{it.label}" for it in im.SETUP_ITEMS])
def test_setup_selected_row_always_sits_on_the_same_screen_row(item):
    """D-017 (Thomas: "A centered active row might be more simple and readable"): the selected
    row is always drawn on the 4th of the 8 rows, the list scrolling under it, at both ends too
    (blank rows above the first item, below the last; no clamping, no pinned header). Group
    headers stay inline as dividers, the selected item's in white, the others grey; the top line
    names the group and the position in it (no '10/27')."""
    it = im.SETUP_ITEMS[item]
    img, drawn = _drawn("SETUP", st.State(), pages.ScreenStatus(**RUN), Focus(item=item))
    assert _setup_selected(img, drawn, it.label)[0] == SETUP_SEL_Y
    rows = pages.SETUP_ROWS
    k = next(r for r, (kind, v) in enumerate(rows) if kind == "item" and v == item)
    for r in range(8):
        y = SETUP_LIST_Y[0] + r * 28 + 13
        at = [(t, x) for t, x, ty, _f in drawn if ty == y and x in (SETUP_HEAD_X, SETUP_LABEL_X)]
        j = k - pages.SETUP_SEL_ROW + r
        if not 0 <= j < len(rows):
            assert at == [], (r, at)                                       # past an end: blank
        elif rows[j][0] == "head":
            assert at == [(rows[j][1], SETUP_HEAD_X)], (r, at)
        else:
            assert at == [(im.SETUP_ITEMS[rows[j][1]].label, SETUP_LABEL_X)], (r, at)
    heads = _setup_heads(drawn)
    assert all(f == (pages.FG if n == it.group else pages.DIM) for n, _y, f in heads)
    assert [n for n, _y, _f in heads] == [g for g in im.SETUP_GROUP_NAMES
                                          if g in {n for n, _y, _f in heads}]   # in order
    grp, pos, size = im.setup_group_pos(item)
    top = [t for t, x, y, _f in drawn if y == pages.STATUS_H + 2 and x == pages.W // 2]
    assert top == [f"{grp} {pos}/{size}"]
    assert not any(t.endswith(f"/{len(im.SETUP_ITEMS)}") for t, *_r in drawn)


def test_setup_first_and_last_items_sit_where_every_other_does():
    """The ends, named: TOPS › Trim L (two blank rows, then the TOPS header, then it) and BOX ›
    Knob test (four blank rows under it) on the same screen row as SUBS › Mono L+R."""
    ys = []
    for item in (0, _item("mono_subs"), len(im.SETUP_ITEMS) - 1):
        img, drawn = _drawn("SETUP", st.State(), pages.ScreenStatus(**RUN), Focus(item=item))
        ys.append(_setup_selected(img, drawn, im.SETUP_ITEMS[item].label)[0])
        if item == 0:
            assert _setup_heads(drawn)[0][:2] == ("TOPS", SETUP_SEL_Y - 28)
            assert not [t for t, x, y, _f in drawn if _in_list(y) and y < SETUP_SEL_Y - 28
                        and x in (SETUP_HEAD_X, SETUP_LABEL_X)]
        if item == len(im.SETUP_ITEMS) - 1:
            assert not [t for t, x, y, _f in drawn if _in_list(y) and y > SETUP_SEL_Y
                        and x in (SETUP_HEAD_X, SETUP_LABEL_X, SETUP_VALUE_X)]
    assert ys == [SETUP_SEL_Y] * 3


def test_setup_scroll_bar_follows_the_selection():
    """No ▲▼ marks: a scroll bar at the right edge says which part of the list is in sight
    (D-017: the blank rows past its ends are not the list, so at the ends the thumb is shorter
    and touches its end of the bar)."""
    def thumb(item):
        img = pages.render("SETUP", st.State(), pages.ScreenStatus(**RUN), Focus(item=item), now=0.0)
        ys = [y for y in range(*SETUP_LIST_Y) if img.getpixel((pages.W - 5, y)) == pages.DIM]
        assert ys, item
        return ys[0], ys[-1]
    n = len(pages.SETUP_ROWS)
    ty0, ty1 = SETUP_LIST_Y[0], SETUP_LIST_Y[1] - 3
    for item in (0, 1, _item("input_trim_db"), _item("mono_subs"), len(im.SETUP_ITEMS) - 1):
        k = next(r for r, (kind, v) in enumerate(pages.SETUP_ROWS) if kind == "item" and v == item)
        v0, v1 = max(0, k - 3), min(n, k + 5)
        assert thumb(item) == (ty0 + (ty1 - ty0) * v0 // n, ty0 + (ty1 - ty0) * v1 // n), item
    top, bottom = thumb(0), thumb(len(im.SETUP_ITEMS) - 1)
    assert top[0] == SETUP_LIST_Y[0] and bottom[1] == ty1
    assert top[0] < thumb(_item("input_trim_db"))[0] < bottom[0]
    assert top[1] - top[0] < SETUP_LIST_Y[1] - SETUP_LIST_Y[0] - 28   # a part of the list only


@pytest.mark.parametrize("field,help", [
    ("out_trim_db.2", "turn E3 = change"), ("sub_lowpass_hz", "turn E3 = change"),
    ("sub_lowpass_on", "turn E3 = change"), ("spectrum_on", "turn E3 = change"),
    ("top_inverted", "turn E3 · push E3 = apply"), ("lowcut_on", "turn E3 · push E3 = apply"),
    ("mono_subs", "turn E3 · push E3 = apply"), ("profile", "turn E3 · push E3 = apply"),
    ("PHONE", "push E3 = open"), ("HWTEST", "push E3 = open"),
    (im.A_RESET_LIVE, "push E3 ×2 = run"), (im.A_LAPTOP, "push E3 ×2 = run"),
    (im.A_UNDO_RESET, "push E3 = run"),
])
def test_setup_bottom_line_says_what_the_knobs_do_on_the_row(field, help):
    _img, drawn = _drawn("SETUP", st.State(), pages.ScreenStatus(**RUN), Focus(item=_item(field)))
    assert _setup_help_text(drawn) == [help]
    assert "push = select" not in [t for t, *_r in drawn]


@pytest.mark.parametrize("tags", ["dim", "off", "bright"])
def test_setup_knob_labels_sit_at_their_corners(tags):
    """D-017: E1 ITEM top-left, VALUE E3 top-right, E2 GROUP bottom-left, the help text bottom
    right (E4 has nothing on SETUP); the group's name between the top two, the help clear of
    GROUP. At each knob-tag style (Thomas's box: off)."""
    status = pages.ScreenStatus(knob_tags=tags, **RUN)
    for item in (0, _item("mono_subs"), _item(im.A_LAPTOP)):
        c = _setup_ctx(st.State(), status, Focus(item=item))
        lab = {e: c.ink[e]["label"] for e in c.ink if e >= 0}
        assert set(lab) == {0, 1, 2}
        assert lab[0][0] <= 45 and lab[0][3] <= SETUP_LIST_Y[0]              # ITEM: top-left
        assert lab[2][2] >= pages.W - 45 and lab[2][3] <= SETUP_LIST_Y[0]    # VALUE: top-right
        assert lab[1][0] <= 45 and lab[1][1] >= SETUP_LIST_Y[1]              # GROUP: bottom-left
        _img, drawn = _drawn("SETUP", st.State(), status, Focus(item=item))
        (help_text,) = _setup_help_text(drawn)
        help_x0 = pages.W - 10 - pages._textlength(help_text, pages.font(16, pages.FONT_REG))
        assert lab[1][2] + 12 < help_x0, (tags, item)
        grp, pos, size = im.setup_group_pos(item)
        head_w = pages._textlength(f"{grp} {pos}/{size}", pages.font(20))
        assert lab[0][2] + 8 < pages.W / 2 - head_w / 2 and pages.W / 2 + head_w / 2 + 8 < lab[2][0]


def test_setup_lo_cut_row_shows_its_frequency():
    """TOPS › Lo-Cut is on/off here; its frequency (ALIGN E4) is said on the row."""
    s = st.State(lowcut_hz=112.0)
    _img, drawn = _drawn("SETUP", s, pages.ScreenStatus(**RUN), Focus(item=_item("top_inverted")))
    (y,) = [y for t, x, y, _f in drawn if t == "Lo-Cut" and x == SETUP_LABEL_X]
    assert _setup_value_at(drawn, y) == "ON · 112 Hz"
    img, drawn = _drawn("SETUP", s, pages.ScreenStatus(**RUN), Focus(item=_item("lowcut_on")))
    assert _setup_selected(img, drawn, "Lo-Cut")[3] == "ON · 112 Hz"


def test_demo_cases_include_the_d013_pictures():
    cases = {c[0]: c for c in pages.demo_cases()}
    assert {"setup", "setup_confirm", "setup_pick_audition", "setup_profile_pinned", "setup_muted",
            "setup_applying", "setup_reset_confirm", "setup_undo", "setup_laptop_confirm",
            "setup_laptop_open", "hwtest", "hwtest_muted"} <= set(cases)
    _n, _p, s, status, f = cases["setup"]
    assert im.SETUP_ITEMS[f.item].field == "top_inverted"                 # TOPS › Polarity
    # D-017: the same states drawn the new way, the proposal in the popup
    for name, big in (("setup_confirm", "off → ON"), ("setup_muted", "off → ON"),
                      ("setup_pick_audition", "SUM → TOPS")):
        _n, page, s, status, f = cases[name]
        c = _setup_ctx(s, status, f)
        assert (c.popup["big"], c.popup["what"]) == (big, "MAY CLICK"), name
    for name, ask in (("setup_reset_confirm", "Reset live EQ?"),
                      ("setup_laptop_confirm", "Open laptop access?")):
        _n, page, s, status, f = cases[name]
        assert _setup_ctx(s, status, f).popup["big"] == ask, name
    _n, page, s, status, f = cases["setup_profile_pinned"]
    img, drawn = _drawn(page, s, status, f)
    assert _setup_selected(img, drawn, "Profile")[3] == "DEV · PINNED"
    for name in ("hwtest", "hwtest_muted"):                                # B4 (sw7) held long
        hw = cases[name][4].hw
        assert hw.sw_down[7] and hw.sw_last[7] == "long" and hw.last.endswith("= B4"), name
        assert not hw.sw_down[5] and hw.sw_last[5] == ""


def test_demo_cases_include_the_d017_pictures():
    """The two popups as on Thomas's box (knob_tags off, the M4): Mono L+R off → ON (a rebuild)
    and Reset live EQ after its first E3 push; Lo-Cut ON → off (risky, no rebuild); the first
    and last items on the fixed row."""
    cases = {c[0]: c for c in pages.demo_cases()}
    assert {"setup_popup", "setup_popup_reset", "setup_popup_lowcut", "setup_first",
            "setup_last"} <= set(cases)
    _n, _p, s, status, f = cases["setup_popup"]
    c = _setup_ctx(s, status, f)
    assert status.knob_tags == "off" and status.profile == "m4"
    assert (c.popup["title"], c.popup["big"], c.popup["what"]) == ("SUBS › Mono L+R", "off → ON",
                                                                   "MAY CLICK")
    _n, _p, s, status, f = cases["setup_popup_reset"]
    c = _setup_ctx(s, status, f)
    assert status.knob_tags == "off"
    assert (c.popup["big"], c.popup["instr"]) == ("Reset live EQ?",
                                                  "push E3 again = reset · turn E1 = cancel")
    _n, _p, s, status, f = cases["setup_popup_lowcut"]
    assert _setup_ctx(s, status, f).popup["what"] == "FULL RANGE TO THE TOPS"
    assert cases["setup_first"][4].item == 0
    assert cases["setup_last"][4].item == len(im.SETUP_ITEMS) - 1


# ------------------------------------------------------------------------------ D-014 band IN/OUT
def _chip_box(slot: int) -> tuple[int, int, int, int]:
    w = (pages.W - 8 - 4 * 5) / 6
    k = im.BAND_CYCLE.index(slot)
    return (round(4 + k * (w + 4)), pages.CHIP_Y0, round(4 + k * (w + 4) + w) + 1, pages.CHIP_Y1 + 1)


def _p2_cut(on: bool = True, q: float = 8.0) -> st.State:
    s = st.State()
    s.live_peq[1].freq, s.live_peq[1].gain, s.live_peq[1].q, s.live_peq[1].on = 2520.0, -6.5, q, on
    return s


def test_b3_hold_on_show_switches_the_selected_band_off_show_unchanged():
    """D-014: B3 hold on SHOW = the selected band IN/OUT (ToggleBand of that band); a tap stays
    the notch helper. Off SHOW a B3 hold keeps its D-013 behaviour (nothing)."""
    for slot in range(6):
        assert map_event("SHOW", Focus(slot=slot), Sw(6, "long"), CONFIRMED) == [ToggleBand(slot)]
    assert map_event("SHOW", Focus(slot=3), Sw(6, "short"), CONFIRMED) == [NotchHelper()]
    for page in ("ALIGN", "SETUP", "PHONE"):
        assert map_event(page, Focus(slot=2), Sw(6, "long"), CONFIRMED) == [], page
    for sw in (4, 5, 7):                                          # no other button switches a band
        for k in ("press", "short", "long", "release"):
            assert not any(isinstance(a, ToggleBand)
                           for a in map_event("SHOW", Focus(), Sw(sw, k), CONFIRMED))


@pytest.mark.parametrize("cfg", [
    None, {}, {"controls": None},
    {"controls": {"buttons": [4, 5, 6, 7]}},                         # the shipped k1000.yaml
    {"controls": {"buttons": [4, 5, 6, 7], "buttons_confirmed": "true"}},   # quoted: a typo
    {"controls": {"buttons_confirmed": 1}},                          # not a real bool
    {"controls": {"buttons": [4, 5, 6, 8], "buttons_confirmed": True}},     # invalid: fell back
    {"controls": {"buttons": [0, 1, 2, 3], "buttons_confirmed": True}},     # collides: fell back
    {"controls": {"buttons": "4567", "buttons_confirmed": True}},
    {"controls": {"buttons": [True, 5, 6, 7], "buttons_confirmed": True}},
    {"controls": {"encoders": [3, 2, 1, 0]}},                        # knobs set, buttons not
])
def test_b3_hold_never_switches_a_band_on_an_unconfirmed_button_map(cfg):
    """Review of D-014 (HIGH): until the Knob test proved which button is B3, a B3 hold may be
    an emergency MUTE aimed at B4 on a swapped map; taking the selected band (the notch holding
    the ring) OUT would make it worse. So it only says which button it was, and changes
    nothing; B4's mute and the B3 tap are untouched."""
    m = ControlMap.from_cfg(cfg)
    assert m.buttons_confirmed is False
    f = Focus(slot=1)
    for k in ("press", "long", "release"):
        acts = map_event("SHOW", f, Sw(6, k), cfg)
        assert not any(isinstance(a, ToggleBand) for a in acts), k
    assert map_event("SHOW", f, Sw(6, "long"), cfg) == [ShowHint(im.HINT_B3_HOLD_UNCONFIRMED)]
    assert map_event("SHOW", f, Sw(6, "short"), cfg) == [NotchHelper()]
    assert map_event("SHOW", f, Sw(m.buttons[3], "long"), cfg, muted=False) == [Mute(True)]
    assert "B3" in im.HINT_B3_HOLD_UNCONFIRMED


@pytest.mark.parametrize("cfg,b3", [
    ({"controls": {"buttons_confirmed": True}}, 6),                  # the default map, confirmed
    ({"controls": {"buttons": [4, 5, 6, 7], "buttons_confirmed": True}}, 6),
    ({"controls": {"buttons": [7, 6, 5, 4]}}, 5),                    # written by hand: B3 = sw5
    ({"hw": {"buttons": [4, 5, 7, 6]}}, 7),                          # the older section name
])
def test_b3_hold_switches_the_band_once_the_map_is_confirmed(cfg, b3):
    m = ControlMap.from_cfg(cfg)
    assert m.buttons_confirmed is True and m.buttons[2] == b3
    assert map_event("SHOW", Focus(slot=4), Sw(b3, "long"), cfg) == [ToggleBand(4)]
    assert map_event("SHOW", Focus(slot=4), Sw(b3, "long"), m) == [ToggleBand(4)]   # a ControlMap
    assert ControlMap.from_cfg(m) is m


def test_live_bands_carry_in_out_and_an_out_band_adds_nothing_to_the_curve():
    on, out = eqgraph.live_bands(_p2_cut()), eqgraph.live_bands(_p2_cut(on=False))
    assert on[1].on and not out[1].on and out[1].gain == -6.5        # the marker keeps its place
    assert not on[1].flat and out[1].flat
    f = np.geomspace(20, 20000, 200)
    assert np.allclose(eqgraph.curve_db(out, f), 0.0)
    assert eqgraph.curve_db(on, f).min() < -6.0
    assert not np.allclose(eqgraph.response_db(on[1], f), 0.0)
    # a dict state (the phone's) and an older State without `on` read as IN
    assert eqgraph.live_bands({"live_peq": [{"freq": 100, "gain": -3, "q": 4}]})[0].on
    assert not eqgraph.live_bands({"live_peq": [{"freq": 100, "gain": -3, "q": 4, "on": False}]})[0].on
    # GRAB's free band: the STORED gain decides (an OUT cut keeps its settings; OUT at 0 dB is free)
    bands = eqgraph.live_bands(_p2_cut(on=False))
    assert eqgraph.free_band(bands, prefer=1) == 0
    flat_out = st.State()
    flat_out.live_peq[1].on = False
    assert eqgraph.free_band(eqgraph.live_bands(flat_out), prefer=1) == 1


def test_an_out_band_marker_is_hollow_and_the_curve_goes_without_it():
    box = (0, 0, 456, 130)
    on = eqgraph.render_graph((456, 130), eqgraph.live_bands(_p2_cut(q=2.0)), selected=None)
    out = eqgraph.render_graph((456, 130), eqgraph.live_bands(_p2_cut(on=False, q=2.0)), selected=None)
    flat = eqgraph.render_graph((456, 130), eqgraph.live_bands(st.State()), selected=None)
    cut = eqgraph.STYLE.cut
    fa = eqgraph.LogAxis(0, 455)
    da = eqgraph.DbAxis(0, 129, eqgraph.STYLE.top_db, eqgraph.STYLE.bottom_db)
    mx, my = fa.x(2520.0), da.y(-6.5)
    r = eqgraph.STYLE.marker_r
    mbox = (mx - r - 2, my - r - 2, mx + r + 3, my + r + 3)
    assert _count(on, mbox, cut) > 150                 # a solid orange dot
    assert 15 < _count(out, mbox, cut) < 120           # a dashed orange ring
    # away from the marker, the OUT picture is the flat one: the curve does not dip at 2.5k
    dip = (fa.x(1400.0), da.y(-0.7), fa.x(1800.0), da.y(-6.0))
    assert _count(on, dip, eqgraph.STYLE.curve) > 0
    assert (_crop(out, tuple(int(v) for v in dip)) == _crop(flat, tuple(int(v) for v in dip))).all()
    # selected and OUT: an amber ring (not a disc), no amber fill under a curve it no longer makes
    sel_on = eqgraph.render_graph((456, 130), eqgraph.live_bands(_p2_cut(q=2.0)), selected=1)
    sel_out = eqgraph.render_graph((456, 130), eqgraph.live_bands(_p2_cut(on=False, q=2.0)), selected=1)
    amber = eqgraph.STYLE.sel_marker

    def blended(img: Image.Image, b: tuple[float, float, float, float]) -> int:
        a = _crop(img, tuple(int(v) for v in b)).astype(int)
        return int(((a[..., 0] > 80) & (a[..., 1] > 50) & (a[..., 2] < 40)).sum())

    assert blended(sel_on, dip) > 20 and blended(sel_out, dip) == 0
    rs = eqgraph.STYLE.sel_r
    sbox = (mx - rs - 2, my - rs - 2, mx + rs + 3, my + rs + 3)
    assert 15 < _count(sel_out, sbox, amber) < _count(sel_on, sbox, amber) * 0.7


def test_show_draws_an_out_band_dark_dashed_with_out_its_values_grey():
    s_on, s_out = _p2_cut(), _p2_cut(on=False)
    for sel in (1, 2):                                   # selected, and not
        on = pages.render("SHOW", s_on, pages.ScreenStatus(**RUN), Focus(slot=sel), now=0.0)
        out = pages.render("SHOW", s_out, pages.ScreenStatus(**RUN), Focus(slot=sel), now=0.0)
        chip = _chip_box(1)
        assert _count(out, chip, pages.OUT_CHIP_BG) > 400, sel       # dark
        assert _count(on, chip, pages.OUT_CHIP_BG) == 0, sel
        col = pages.AMBER if sel == 1 else pages.CUT
        # dashed: the outline colour on the top edge is broken by gaps
        top = np.asarray(out)[pages.CHIP_Y0, chip[0] + 6:chip[2] - 6]
        hits = (top == np.array(col, np.uint8)).all(axis=1)
        assert 0.3 < hits.mean() < 0.9, (sel, hits.mean())
    # the OUT mark: small text right of the band number, in the chip's colour
    out = pages.render("SHOW", s_out, pages.ScreenStatus(**RUN), Focus(slot=2), now=0.0)
    on = pages.render("SHOW", s_on, pages.ScreenStatus(**RUN), Focus(slot=2), now=0.0)
    x0, y0, x1, y1 = _chip_box(1)
    right = (x0 + (x1 - x0) * 0.6, y0 + 4, x1 - 3, y1 - 4)
    assert _count(out, right, pages.CUT) > 20 and _count(on, right, pages.CUT) == 0
    # the selected band OUT: its FREQ / GAIN / Q are grey (stored, not playing), never white
    sel_on = pages.render("SHOW", s_on, pages.ScreenStatus(**RUN), Focus(slot=1), now=0.0)
    sel_out = pages.render("SHOW", s_out, pages.ScreenStatus(**RUN), Focus(slot=1), now=0.0)
    q = (pages.METER_BOX[2] + 4, pages.TOP_BOX[0] + 3, pages.W - 4, pages.TOP_BOX[1])  # E3
    freq = (4, pages.BOT_BOX[0], pages.W // 2 - 4, pages.BOT_BOX[1])                  # E2
    gain = (pages.W // 2 + 4, pages.BOT_BOX[0], pages.W - 4, pages.BOT_BOX[1])        # E4
    for box in (q, freq, gain):
        assert _count(sel_on, box, pages.FG) > 200 and _count(sel_out, box, pages.FG) == 0
        assert _count(sel_out, box, pages.DIM) > 200


def test_out_pill_from_the_state_and_it_is_orange():
    s = _p2_cut(on=False)
    s.live_lowshelf.on = False
    assert pages.screen_pills(s, pages.ScreenStatus()) == ["OUT P2 LS"]
    s.lowcut_on, s.mono_subs = False, True
    assert pages.screen_pills(s, None) == ["LO-CUT OFF", "OUT P2 LS", "MONO SUB"]
    assert pages.screen_pills(st.State(), None) == []
    region = (0, pages.PILL_Y0, pages.W, pages.PILL_Y1 + 1)
    img = pages.render("SHOW", _p2_cut(on=False), pages.ScreenStatus(pills=["OUT P2"], **RUN), Focus())
    assert _count(img, region, pages.ORANGE) > 100
    shown, folded, _size = pages.pill_layout(["OUT P2", "MONO SUB"], 120)
    assert (shown, folded) == (["OUT P2"], ["MONO SUB"])    # a neutral pill folds before OUT


def test_demo_cases_include_the_d014_pictures():
    cases = {c[0]: c for c in pages.demo_cases()}
    _n, page, s, status, f = cases["show_band_out"]
    assert page == "SHOW" and f.slot == 1 and s.live_peq[1].on is False and s.live_peq[1].gain < 0
    assert [b.on for b in eqgraph.live_bands(s)].count(False) == 1
    assert list(status.pills) == ["OUT P2"]
    _n, page, s2, _status, f2 = cases["show_band_out_hint"]
    assert f2.hint == "P2 OUT" and s2.to_dict() == s.to_dict()


# --- D-015: Q on E3 / GAIN on E4, DELAY on E2 / BALANCE on E3, flush values, IN meters --------
def test_d015_knob_maps():
    """Thomas on the box, 2026-10-14: SHOW E3 = Q (a shelf's SLOPE), E4 = GAIN; ALIGN E2 = the
    signed DELAY (locked, wake-up detent), E3 = BALANCE; LO-CUT stays E4 (locked)."""
    show = im.KNOBS["SHOW"]
    assert [(k.label, k.field) for k in show] == [
        ("MASTER", im.F_MASTER), ("FREQ", "{slot}.freq"), ("Q", "{slot}.q"), ("GAIN", "{slot}.gain")]
    align = im.KNOBS["ALIGN"]
    assert [(k.label, k.field, k.locked, k.arm) for k in align] == [
        ("MASTER", im.F_MASTER, False, False), ("DELAY", im.F_ALIGN_DELAY, True, True),
        ("BALANCE", im.F_BALANCE, False, False), ("LO-CUT", im.F_LOWCUT_FREQ, True, False)]
    # the corners they are drawn at: GAIN bottom right, Q top right; both locked knobs at the
    # bottom (E2 bottom left, E4 bottom right)
    assert im.KNOB_CORNERS[3] == "br" and im.KNOB_CORNERS[2] == "tr" and im.KNOB_CORNERS[1] == "bl"
    # what the phone's slide reads (docs/14): the per-field acceleration is unchanged
    assert im.knob_accel("live_peq.2.gain") == (1, 1) and im.knob_accel("live_peq.2.q") == (1, 1)
    assert im.knob_accel("live_peq.2.freq") == (4, 4) and im.knob_accel(im.F_ALIGN_DELAY) == (1, 1)
    # E3 on a shelf is its slope
    f = Focus(slot=4)
    assert map_event("SHOW", f, Enc(2, -1, ts=0.0)) == [Nudge("live_lowshelf.q", -1, False)]
    assert map_event("SHOW", f, Enc(3, -1, ts=1.0)) == [Nudge("live_lowshelf.gain", -1, False)]


def _module_strings(mod) -> list[tuple[str, str]]:
    return [(n, v) for n, v in vars(mod).items() if isinstance(v, str) and n.isupper()]


def test_every_hint_naming_the_gain_knob_says_e4():
    """D-015: GAIN is E4. The notch helper's strip says which knob cuts; no box string still
    sends the operator to E3 for a cut, nor names E3/E4 as ALIGN's locked pair."""
    assert ctl_mod.HINT_NOTCH.format(band="P3", hz="2.44k") == "P3 · 2.44k · E4 TO CUT"
    assert ctl_mod.HINT_NOTCH_NO_PEAK.format(band="P2", hz="1.00k") == \
        "P2 · 1.00k · NO PEAK · E4 TO CUT"
    for mod in (ctl_mod, im, pages):
        for name, text in _module_strings(mod):
            assert "E3 TO CUT" not in text and "E3/E4" not in text, (mod.__name__, name)
            if "TO CUT" in text:
                assert "E4 TO CUT" in text, (mod.__name__, name)
    # the strip it shows fits the chip row at a readable size
    for t in (ctl_mod.HINT_NOTCH.format(band="P4", hz="20.0k"),
              ctl_mod.HINT_NOTCH_NO_PEAK.format(band="P4", hz="20.0k")):
        size, shown = pages._fit_ellipsis(t, 19, pages.W - 16)
        assert shown == t and size >= 15, (t, size)


def test_align_locked_turns_show_the_lock_hint_and_one_b2_hold_unlocks_both():
    """D-015: E2 (DELAY) and E4 (LO-CUT) are the locked pair: a turn of either shows the lock
    hint and changes nothing; one B2 hold unlocks both; a B2 tap relocks both."""
    f = Focus()
    for e in (1, 3):
        assert map_event("ALIGN", f, Enc(e, 1, ts=1.0)) == [ShowHint(im.HINT_LOCKED)]
    for e, fld in ((0, "master_db"), (2, "balance_db")):              # the top pair: live
        assert map_event("ALIGN", f, Enc(e, -1, ts=1.5)) == [Nudge(fld, -1, False)]
    acts = map_event("ALIGN", f, Sw(5, "long", ts=2.0))
    assert acts == [Unlock(im.UNLOCK_S)] and apply_focus(f, acts[0], now=2.0)
    assert map_event("ALIGN", f, Enc(1, 1, ts=3.0)) == [ShowHint(im.HINT_ARM.format("DELAY"))]
    assert map_event("ALIGN", f, Enc(1, 1, ts=3.2)) == [Nudge("align_delay_ms", 1, False)]
    assert map_event("ALIGN", f, Enc(3, 1, ts=3.4)) == [Nudge("lowcut_hz", 1, False)]
    acts = map_event("ALIGN", f, Sw(5, "short", ts=4.0))
    assert acts == [Lock()] and apply_focus(f, acts[0], now=4.0)
    for e in (1, 3):
        assert map_event("ALIGN", f, Enc(e, 1, ts=5.0 + e)) == [ShowHint(im.HINT_LOCKED)]
    # the screen draws the locked pair dimmed with the badge between them
    s = st.State(sub_delay_ms=2.35, lowcut_hz=112.0, balance_db=3.0)
    ink = _ink("ALIGN", s, pages.ScreenStatus(**RUN), Focus(), tags="off")
    assert {e: ink[e]["label"] for e in range(4)}[1][1] > pages.H / 2      # DELAY: bottom
    assert ink[3]["label"][1] > pages.H / 2 and ink[2]["label"][3] < pages.H / 2


def _bar_box(page: str, i: int) -> tuple[int, int, int, int]:
    """Pixel box of meter bar `i` (0, 1 = IN L R; 2..5 = SUB L R, TOP L R), clip box excluded."""
    if page == "SHOW":
        x0, y0, x1, y1 = pages.METER_BOX
        xs, _c = pages._bar_xs((x0 + x1) / 2, 10, 3, 8)
        top, bot = y0 + 8, y1 - 12
    else:
        xs, _c = pages._bar_xs((pages.METER_X0 + pages.METER_X1) / 2, 10, 3, 10)
        top, bot = pages.STATUS_H + 8 + 13, pages.H - 54
    return int(xs[i]), int(top), int(xs[i]) + 10, int(bot) + 1


def _clip_box(page: str, i: int) -> tuple[int, int, int, int]:
    x0, y0, x1, _y1 = _bar_box(page, i)
    cy0 = pages.METER_BOX[1] if page == "SHOW" else pages.STATUS_H + 8
    return x0, cy0, x1, cy0 + 5


@pytest.mark.parametrize("page", ["SHOW", "ALIGN"])
def test_in_meters_are_drawn_left_of_the_outputs_with_in_clip(page):
    """D-015: the program input's capture peaks (status.in_peak_db) as IN L R, left of SUB L R
    and TOP L R, same -60..0 dBFS scale and colours; IN CLIP lights the IN pair's clip boxes."""
    s = st.State()
    quiet = pages.ScreenStatus(levels=(-100.0,) * 4, in_peak_db=(-100.0, -100.0), **RUN)
    loud = pages.ScreenStatus(levels=(-100.0,) * 4, in_peak_db=(-3.0, -12.0), **RUN)
    clip = pages.ScreenStatus(levels=(-100.0,) * 4, in_peak_db=(-0.1, -0.2), in_clip=True, **RUN)
    old = pages.ScreenStatus(levels=(-100.0,) * 4, **RUN)          # no in_peak_db: the floor
    imgs = {k: pages.render(page, s, v, Focus(), now=0.0)
            for k, v in (("quiet", quiet), ("loud", loud), ("clip", clip), ("old", old))}
    for i in (0, 1):
        box = _bar_box(page, i)
        assert _count(imgs["quiet"], box, pages.GREEN) == 0
        assert _count(imgs["loud"], box, pages.GREEN) > 50
        assert _count(imgs["clip"], _clip_box(page, i), pages.RED) > 10
        assert _count(imgs["loud"], _clip_box(page, i), pages.RED) == 0
        assert (_crop(imgs["old"], box) == _crop(imgs["quiet"], box)).all()
    l_box, r_box = _bar_box(page, 0), _bar_box(page, 1)               # L at -3: into the red
    assert _count(imgs["loud"], l_box, pages.RED) > 0 and _count(imgs["loud"], r_box, pages.RED) == 0
    assert _count(imgs["loud"], r_box, pages.YELLOW) > 0               # R at -12: yellow
    for i in range(2, 6):                                              # outputs untouched
        assert (_crop(imgs["loud"], _bar_box(page, i)) == _crop(imgs["quiet"], _bar_box(page, i))).all()
        assert _count(imgs["clip"], _clip_box(page, i), pages.RED) == 0
    _img, drawn = _drawn(page, s, loud, Focus())
    labels = sorted((x, t) for t, x, y, _f in drawn if t in pages.METER_PAIRS)
    assert [t for _x, t in labels] == ["IN", "SUB", "TOP"]             # IN left of SUB, TOP
    # meter_bars: the order and the latch, tolerant of an older or odd status
    bars = pages.meter_bars(pages.ScreenStatus(levels=(-1.0, -2.0, -3.0, -4.0), in_peak_db=(-5.0,),
                                               clip=(False, True), in_clip=True))
    assert bars == [(-5.0, True), (-5.0, True), (-1.0, False), (-2.0, True), (-3.0, False),
                    (-4.0, False)]
    assert pages.meter_bars(None) == [(pages.METER_FLOOR_DB, False)] * 6


def test_the_controller_status_carries_the_input_peaks_to_the_screen():
    """control.Status names the field the screen reads (in_peak_db), as ScreenStatus does."""
    status = ctl_mod.Status()
    assert len(status.in_peak_db) == 2 and "in_peak_db" in pages.ScreenStatus.__dataclass_fields__
    status.in_peak_db = [-6.0, -9.0]
    assert pages.meter_bars(status)[:2] == [(-6.0, False), (-9.0, False)]


def _edges_check(ink, e: int, tags: str) -> None:
    """Knob `e`'s value group is flush to its corner's outer border: from x 10 on a left corner
    (with its label when tags are off), ending at W - 10 on a right one; above the label on a
    bottom corner, under it on a top one."""
    v, h = im.KNOB_CORNERS[e]
    parts = ink[e]
    first = parts["value"]
    last = parts.get("unit", parts["value"])
    label = parts["label"]
    if h == "l":
        assert abs(first[0] - 10) <= 4, (e, first)
        if tags == "off":
            assert abs(first[0] - label[0]) <= 3, (e, first, label)
    else:
        assert abs(last[2] - (pages.W - 10)) <= 3, (e, last)
        if tags == "off":
            assert abs(last[2] - label[2]) <= 3, (e, last, label)
    if v == "t":
        assert first[1] > label[3], e
    else:
        assert first[3] < label[1], e


@pytest.mark.parametrize("tags", ["off", "dim", "bright"])
def test_every_corner_value_is_flush_to_its_outer_border(tags):
    """D-015 (Thomas: '120Hz to the left above FREQ and 0.17 Q to the right above SLOPE'):
    SHOW and ALIGN draw each value flush to its corner's outer border, its unit beside it."""
    s = st.State(master_db=-14.0, balance_db=3.0, sub_delay_ms=2.35, lowcut_hz=112.0)
    s.live_peq[1].freq, s.live_peq[1].gain, s.live_peq[1].q = 120.0, -6.5, 8.0
    s.live_lowshelf.freq, s.live_lowshelf.q = 120.0, 0.71
    for page, slot in (("SHOW", 1), ("SHOW", 4), ("ALIGN", 0)):
        ink = _ink(page, s, pages.ScreenStatus(knob_tags=tags, **RUN), Focus(slot=slot))
        for e in range(4):
            _edges_check(ink, e, tags)
    # the SHOW pair Thomas named: the frequency left above FREQ, the slope right under SLOPE
    ink = _ink("SHOW", s, pages.ScreenStatus(knob_tags="off", **RUN), Focus(slot=4))
    assert ink[1]["label"][0] < 14 and ink[1]["value"][0] < 14
    assert ink[2]["label"][2] > pages.W - 14 and ink[2]["unit"][2] > pages.W - 14


@pytest.mark.parametrize("tags", ["off", "dim", "bright"])
def test_the_band_name_beside_fine_gives_way_to_its_short_name_never_cut(tags):
    """D-015 review: E3's push held (fine Q, or a shelf's fine SLOPE) puts FINE in E3's label
    row. A shelf's name beside SLOPE then reads 'LS' / 'HS' (LIVE_SLOT_SHORT), at the tag's
    full size, never cut to 'LO SHE…' or 'HI…'; a bell's 'PEQ 2' beside Q still fits whole,
    and without FINE every band shows its whole name."""
    marked: list[tuple[int, str, str, int]] = []
    real = pages._Ctx.mark

    def spy(self, e, part, xy, text, f, anchor):
        if part == "tag":
            marked.append((e, part, text, f.size))
        return real(self, e, part, xy, text, f, anchor)

    s = st.State()
    status = pages.ScreenStatus(knob_tags=tags, **RUN)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pages._Ctx, "mark", spy)
        for slot in range(len(im.LIVE_SLOTS)):
            for held in (frozenset(), frozenset({2}), frozenset({2, 3}), frozenset({0, 1, 2, 3})):
                marked.clear()
                ink = _ink("SHOW", s, status, Focus(slot=slot, held=set(held)))
                shelf_fine = slot >= 4 and 2 in held
                want = im.LIVE_SLOT_SHORT[slot] if shelf_fine else im.LIVE_SLOT_LABELS[slot]
                assert marked == [(2, "tag", want, pages.TAG_PX)], (tags, slot, held, marked)
                if 2 in held:                          # and it keeps clear of FINE
                    assert not _overlap(ink[2]["tag"], ink[2]["fine"]), (tags, slot, held)


def _layout_ok(name: str, page: str, ink) -> None:
    """Nothing cut, nothing run into: every text box on screen under the status bar, no two
    knob texts (labels, tags, values, units, sub-lines, FINE, the badge) sharing a pixel, none
    in the meters, a SHOW value in its box (never into the chips or the graph)."""
    items = [(e, part, box) for e, parts in ink.items() for part, box in parts.items()]
    for e, part, box in items:
        assert 0 <= box[0] and box[2] <= pages.W, (name, e, part, box)
        assert pages.STATUS_H <= box[1] and box[3] <= pages.H - 1, (name, e, part, box)
    for i, (e1, p1, b1) in enumerate(items):
        for e2, p2, b2 in items[i + 1:]:
            assert not _overlap(b1, b2), (name, (e1, p1, b1), (e2, p2, b2))
    meters = (pages.METER_BOX if page == "SHOW" else
              (pages.METER_X0, pages.STATUS_H + 8, pages.METER_X1, pages.LOCK_Y0 - 2))
    for e, part, box in items:
        if e >= 0:
            assert not _overlap(box, meters), (name, e, part, box)
    if page == "SHOW":
        for e in range(4):
            vbox = ink[e]["value"]
            lo, hi = pages.TOP_BOX if im.KNOB_CORNERS[e][0] == "t" else pages.BOT_BOX
            assert lo <= vbox[1] and vbox[3] <= hi, (name, e, vbox)


def test_nothing_is_cut_or_run_into_in_any_screenshot():
    """Every SHOW and ALIGN picture of docs/ui (the longest values among them: -120.0 dB, 20.0k
    Hz, Q 0.10 and 16.0, a shelf's SLOPE, a band OUT, muted, FINE held on every knob), with
    each knob-tag style: every text fits the 480 x 320 screen and its own place."""
    cases = [c for c in pages.demo_cases() if im.page_name(c[1]) in ("SHOW", "ALIGN")]
    assert {"show_long_values", "show_long_q", "show_long_slope", "align_long_values",
            "show_shelf", "show_band_out", "muted", "align_muted"} <= {c[0] for c in cases}
    for name, page, state, status, focus in cases:
        for tags in ("off", "dim", "bright"):
            _layout_ok(f"{name}/{tags}", im.page_name(page),
                       _ink(page, state, status, focus, tags=tags))
    # and the long values keep a size readable at arm's length
    cases = {c[0]: c for c in cases}
    for name, min_h in (("show_long_values", 20), ("show_long_q", 20), ("align_long_values", 26)):
        _n, page, state, status, focus = cases[name]
        ink = _ink(page, state, status, focus, tags="off")
        for e in range(4):
            v = ink[e]["value"]
            assert v[3] - v[1] >= min_h, (name, e, v)


def test_show_legends_and_values_are_smaller_and_the_graph_taller():
    """D-015: legends 20 -> 17 px, values 44/40 -> 38/36 px, the graph 130 -> 152 px high;
    still readable at arm's length (legends >= 16 px, values >= 34 px)."""
    assert 16 <= pages.LABEL_PX < 20
    assert 36 <= pages.TOP_VALUE_PX < 44 and 34 <= pages.BOT_VALUE_PX < 40
    assert pages.GRAPH[3] - pages.GRAPH[1] == 152
    assert pages.CHIP_Y1 < pages.GRAPH[1] and pages.METER_BOX[3] < pages.CHIP_Y0
    assert pages.TOP_BOX[1] < pages.CHIP_Y0 and pages.GRAPH[3] <= pages.BOT_BOX[0]
    # MUTED still leaves the 0 dB line and its label in sight
    assert pages.SHOW_MUTED_BAND[1] < _zero_db_y() - 1


def test_demo_cases_include_the_d015_pictures():
    cases = {c[0]: c for c in pages.demo_cases()}
    assert {"input_clip", "align_input_clip", "show_long_values", "show_long_q",
            "show_long_slope", "align_long_values"} <= set(cases)
    _n, _p, _s, status, _f = cases["align_input_clip"]
    assert status.in_clip is True and max(status.in_peak_db) > -1.0
    _n, _p, _s, status, _f = cases["show"]
    assert max(status.in_peak_db) > -60.0                     # the IN meters show something



# --- D-016: de-harsh, sub loudness, AUX IN (2026-10-16, all off by default) ----------------
D016_FIELDS = ("deharsh_on", "deharsh_hz", "deharsh_thresh_db", "loud_on", "loud_boost_db",
               "loud_ref_db", "aux_mode", "aux_gain_db")


def test_d016_setup_rows_say_their_values():
    """Each D-016 row in its unit: Hz for the de-harsh centre, dB for the rest; the switches
    off / ON, AUX IN off / MIC / LINE (its off reads like every other off). On a profile with
    no spare input the AUX row says so instead of offering one (Controller.setup_readonly)."""
    by = {it.field: it for it in im.SETUP_ITEMS}
    v = pages._setup_value
    s = st.State()
    assert [v(s, by[f], None) for f in D016_FIELDS] == [
        "off", "2.70k Hz", "-24.0 dB", "off", "+6.0 dB", "-10.0 dB", "off", "0.0 dB"]
    on = st.State(deharsh_on=True, deharsh_hz=1500.0, deharsh_thresh_db=-48.0, loud_on=True,
                  loud_boost_db=12.0, loud_ref_db=-40.0, aux_mode="line", aux_gain_db=-40.0)
    assert [v(on, by[f], None) for f in D016_FIELDS] == [
        "ON", "1.50k Hz", "-48.0 dB", "ON", "+12.0 dB", "-40.0 dB", "LINE", "-40.0 dB"]
    assert v(st.State(aux_mode="mic"), by["aux_mode"], None) == "MIC"
    ro = {"aux_mode": "AUX: NO SPARE INPUT ON THIS DEVICE", "aux_gain_db": "AUX: NO SPARE INPUT ON THIS DEVICE"}
    assert v(st.State(aux_mode="mic"), by["aux_mode"], None, ro) == "NO SPARE INPUT"
    assert v(s, by["aux_gain_db"], None, ro) == "0.0 dB"            # the level, drawn dimmed
    assert pages._setup_unavailable(by["aux_gain_db"], None, ro)
    assert pages._setup_unavailable(by["aux_mode"], None, ro)


def _row_fits(item, value: str, selected: bool) -> bool:
    """The SETUP row's value fits beside its label at the full 20 px (pages._page_setup's
    geometry: label at x 24, value right-aligned at W - 18, 16 px between them)."""
    face = pages.FONT_BOLD if selected else pages.FONT_REG
    lw = 24 + int(pages._textlength(item.label, pages.font(20, face)))
    return pages._textlength(value, pages.font(20, face)) <= (pages.W - 18) - lw - 16


@pytest.mark.parametrize("field", D016_FIELDS)
def test_d016_setup_rows_fit_the_480_px_row_whole(field):
    """Every value a D-016 row can show, its extremes and a pending choice `now → chosen`
    included, fits beside its label at the full 20 px, plain and selected (bold): nothing is
    shrunk, nothing cut. And on screen: the label and the value are both drawn, never over the
    scroll bar."""
    item = next(it for it in im.SETUP_ITEMS if it.field == field)
    if item.kind == "num":
        rng = st.range_for(field)
        values = [pages._fmt_setup(item, x) for x in (rng.min, rng.max, rng.default,
                                                       (rng.min + rng.max) / 2)]
    else:
        names = [pages._fmt_setup(item, c) for c in item.choices]
        values = names + [f"{a} → {b}" for a in names for b in names if a != b]
        if field == "aux_mode":
            values.append("NO SPARE INPUT")
    for val in values:
        for sel in (False, True):
            assert _row_fits(item, val, sel), (field, val, sel)
    for x in ((st.range_for(field).max,) if item.kind == "num" else item.choices):
        s = st.State.from_dict({**st.State().to_dict(), field: x})
        img, drawn = _drawn("SETUP", s, pages.ScreenStatus(**RUN), Focus(item=_item(field)))
        y, _fill, _bar, val = _setup_selected(img, drawn, item.label)
        assert val == pages._setup_value(s, item, pages.ScreenStatus(**RUN))


def test_d016_rows_sit_with_what_they_act_on_switch_first():
    """De-harsh in TOPS (the 2.7 kHz attacks are the tops'), Loudness in SUBS, AUX IN in INPUT;
    each switch right before its numbers, the first of which the phone pairs with it (its label
    starts with the switch's). The switches rebuild the pipeline (MAY CLICK), the numbers do
    not, and none needs a double push."""
    by = {it.field: it for it in im.SETUP_ITEMS}
    assert [by[f].group for f in D016_FIELDS] == ["TOPS"] * 3 + ["SUBS"] * 3 + ["INPUT"] * 2
    for sw, first in (("deharsh_on", "deharsh_hz"), ("loud_on", "loud_boost_db"),
                      ("aux_mode", "aux_gain_db")):
        assert _item(first) == _item(sw) + 1
        assert by[sw].rebuild and by[sw].picks and not by[sw].needs_confirm
        assert not by[first].rebuild and by[first].kind == "num"
    assert by["deharsh_hz"].label.startswith(by["deharsh_on"].label)
    assert by["loud_boost_db"].label.startswith(by["loud_on"].label)


def test_d016_a_read_only_number_row_only_shows_its_reason():
    """The AUX level on a profile with no spare input (Focus.readonly, set by the Controller):
    E3 shows the reason and moves nothing; the bottom line says `fixed here`. A number that is
    not read-only turns as before."""
    why = "AUX: NO SPARE INPUT ON THIS DEVICE"
    s = st.State()
    f = Focus(item=_item("aux_gain_db"), readonly={"aux_gain_db": why, "aux_mode": why})
    assert map_event("SETUP", f, Enc(2, 2), state=s) == [ShowHint(why)]
    assert map_event("SETUP", f, Sw(2, "short"), state=s) == [ShowHint(why)]
    f.item = _item("aux_mode")
    assert map_event("SETUP", f, Enc(2, 1, ts=1.0), state=s) == [ShowHint(why)] and f.pick is None
    f.item = _item("input_trim_db")
    assert map_event("SETUP", f, Enc(2, 2), state=s) == [Nudge("input_trim_db", 2, False)]
    _img, drawn = _drawn("SETUP", s, pages.ScreenStatus(**RUN),
                         Focus(item=_item("aux_gain_db"), readonly={"aux_gain_db": why}))
    assert _setup_help_text(drawn) == ["fixed here"]


@pytest.mark.parametrize("field,proposed,big", [
    ("deharsh_on", True, "off → ON"), ("loud_on", True, "off → ON"),
    ("aux_mode", "mic", "off → MIC"), ("aux_mode", "line", "off → LINE"),
])
def test_d016_switches_are_chosen_then_set_and_say_may_click(field, proposed, big):
    """D-016's switches are risky rows (rebuilds): an E3 turn proposes in the popup, which says
    MAY CLICK; an E3 push applies (never one gesture)."""
    i = _item(field)
    f = Focus(item=i, pick=field, pick_value=proposed, pick_until=10.0)
    c = _setup_ctx(st.State(), pages.ScreenStatus(**RUN), f, now=5.0)
    assert (c.popup["big"], c.popup["what"]) == (big, "MAY CLICK")
    g = Focus(item=i)
    want = True if field != "aux_mode" else "mic"
    assert map_event("SETUP", g, Enc(2, 1, ts=1.0), state=st.State()) == []
    assert g.pick_now(1.0) == (field, want)
    assert map_event("SETUP", g, Sw(2, "short", ts=2.0), state=st.State()) == [SetChoice(field, want)]


D016_PILLS = ["LAPTOP OPEN 1:59", "LO-CUT OFF", "OUT P2 LS", "POL INV SUB+TOP", "AUX LINE",
              "MONO SUB", "LOUDNESS", "DE-HARSH"]


@pytest.mark.parametrize("tags", ["dim", "off", "bright"])
def test_d016_pills_fold_and_are_never_cut_at_any_tag_style(tags):
    """With every pill up (the controller's order), at each knob-tag style: the row fits between
    the bottom labels at >= 13 px; the neutral D-016 pills (LOUDNESS, DE-HARSH) fold into '+N'
    before any orange one, AUX (an input summed into the PA) is orange and folds only after them."""
    s = st.State()
    c = pages._Ctx(Focus(slot=1), 0.0, tags)
    pages._page_show(c, s, pages.ScreenStatus(**RUN))
    room = (c.bottom_label_end["r"] - 12) - (c.bottom_label_end["l"] + 12)
    shown, folded, size = pages.pill_layout(D016_PILLS, room)
    assert size >= pages.PILL_MIN_PX
    f = pages.font(size)
    labels = shown + ([f"+{len(folded)}"] if folded else [])
    assert sum(pages._textlength(p, f) + 12 for p in labels) + 6 * (len(labels) - 1) <= room
    neutral = [p for p in D016_PILLS if not pages._risky_pill(p)]
    assert pages._risky_pill("AUX LINE") and pages._risky_pill("AUX MIC")
    assert not pages._risky_pill("LOUDNESS") and not pages._risky_pill("DE-HARSH")
    risky_folded = [p for p in folded if pages._risky_pill(p)]
    if risky_folded:
        assert set(neutral) <= set(folded)
    # drawn: the orange '+N' when it hides a risky pill, nothing over the labels
    img = pages.render("SHOW", s, pages.ScreenStatus(pills=D016_PILLS, knob_tags=tags, **RUN),
                       Focus(slot=1), now=0.0)
    _check(img)


def test_d016_pills_from_the_state_alone():
    """An older status (no pills): the state tells AUX (not on dev: no spare input there),
    LOUDNESS and DE-HARSH, in the controller's order."""
    s = st.State(aux_mode="mic", mono_subs=True, loud_on=True, deharsh_on=True)
    assert pages.screen_pills(s, pages.ScreenStatus(profile="m4")) == \
        ["AUX MIC", "MONO SUB", "LOUDNESS", "DE-HARSH"]
    assert pages.screen_pills(s, pages.ScreenStatus(profile="dev")) == \
        ["MONO SUB", "LOUDNESS", "DE-HARSH"]
    assert pages.screen_pills(st.State(), pages.ScreenStatus(profile="m4")) == []


def test_demo_cases_include_the_d016_pictures():
    names = {c[0] for c in pages.demo_cases()}
    assert {"show_pills_d016", "setup_deharsh_pick", "setup_loudness", "setup_aux_pick",
            "setup_aux_dev"} <= names
