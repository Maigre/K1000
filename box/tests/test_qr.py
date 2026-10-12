# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for k1000.ui.qr: the Wi-Fi / URL QR codes and the PHONE page of the box screen.

python3-qrcode is an apt dependency added with the phone (install.sh); tests that need it skip
with that reason when it is missing, and the text fallback is tested either way. When a QR
decoder is available (zxing-cpp, dev machines only) the rendered page is DECODED, so what the
camera reads is what is tested.
"""
from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from k1000.ui import pages, qr
from k1000.ui.pages import ScreenStatus

NEED_QR = "python3-qrcode not installed (apt install python3-qrcode, done by install.sh)"
PSK = "m7kq-4tzh-p2xv"
WIFI = f"WIFI:T:WPA;S:K1000;P:{PSK};;"
RUN = ScreenStatus(profile="m4", device="MOTU M4", device_present=True, engine="Running", load=3.0)


def _need_qrcode() -> None:
    if qr.qrcode is None:
        pytest.skip(NEED_QR)


def _decode(img: Image.Image) -> list[str]:
    zx = pytest.importorskip("zxingcpp", reason="no QR decoder (pip install zxing-cpp) on this machine")
    return [r.text for r in zx.read_barcodes(img)]


def _info(**kw: Any) -> qr._Info:
    base = dict(feature=True, on=True, ap_up=True, ssid="K1000", psk=PSK, url="https://10.42.0.1/",
                stations=0, phones=0, tls=True, qr="wifi", band="5 GHz · ch 36")
    base.update(kw)
    return qr._Info(**base)


# ----------------------------------------------------------------------------- payloads
def test_wifi_payload_escapes() -> None:
    assert qr.wifi_payload("K1000", PSK) == WIFI
    assert qr.wifi_payload('a;b,c"d:e\\f', 'p;w:"\\,') == 'WIFI:T:WPA;S:a\\;b\\,c\\"d\\:e\\\\f;P:p\\;w\\:\\"\\\\\\,;;'
    assert qr.wifi_payload("K1000", None) == "WIFI:T:nopass;S:K1000;;"
    assert qr.wifi_payload("K1000", PSK, hidden=True).endswith("H:true;;")


# ----------------------------------------------------------------------------- codes
def test_qr_image_is_the_matrix_with_whole_pixel_modules() -> None:
    _need_qrcode()
    m = qr.qr_matrix(WIFI)
    n = len(m)
    img = qr.qr_image(WIFI, 254)
    px = qr.module_px(WIFI, 254)
    assert img.size == ((n + 2 * qr.QUIET) * px,) * 2 and img.width <= 254
    g = img.convert("L")
    for y in range(-qr.QUIET, n + qr.QUIET):
        for x in range(-qr.QUIET, n + qr.QUIET):
            cx, cy = (x + qr.QUIET) * px + px // 2, (y + qr.QUIET) * px + px // 2
            dark = 0 <= x < n and 0 <= y < n and m[y][x]
            assert g.getpixel((cx, cy)) == (0 if dark else 255), (x, y)


def test_codes_are_big_enough_to_scan_from_50_cm() -> None:
    """The 3.5" panel is 73.7 mm for 480 px (0.154 mm/px). At 50 cm a phone camera (~65 deg wide,
    1280 px analysis frame) sees ~2 px per mm: decoders want >= 2 px per module, so >= 1.2 mm
    modules leave a margin (ppwa-9: 7 px = 1.08 mm was at the limit)."""
    _need_qrcode()
    mm_per_px = 73.7 / 480
    side = qr.PANEL[2] - qr.PANEL[0]
    assert side == qr.PANEL[3] - qr.PANEL[1] >= 284 and qr.PANEL[3] <= 320
    assert qr.module_px(WIFI, side) >= 8                  # ~1.23 mm, 36 mm across
    assert qr.module_px(WIFI, side) * mm_per_px >= 1.2
    assert qr.module_px(qr.wifi_payload("K1000-STAGE-LEFT", "abcd-efgh-jkmn-pqrs"), side) >= 7
    assert qr.module_px("https://10.42.0.1/", side) >= 9
    assert qr.module_px("http://10.42.0.1/", side) >= 9
    frame_px_per_mm = 1280 / (2 * 500 * 0.637)            # tan(32.5 deg) = 0.637
    assert qr.module_px(WIFI, side) * mm_per_px * frame_px_per_mm >= 2.4


def test_codes_decode() -> None:
    _need_qrcode()
    assert _decode(qr.qr_image(WIFI, 254)) == [WIFI]
    assert _decode(qr.qr_image("https://10.42.0.1/", 254)) == ["https://10.42.0.1/"]


# ----------------------------------------------------------------------------- the page
def test_page_steps_decode_from_the_rendered_screen() -> None:
    """Step 1 shows the Wi-Fi code, step 2 the URL: decoded from the full 480x320 frame."""
    _need_qrcode()
    img = qr.render_phone_page(_info(), None, RUN)
    assert img.size == (480, 320) and img.mode == "RGB"
    assert _decode(img) == [WIFI]
    img = qr.render_phone_page(_info(qr="url", stations=1), None, RUN)
    assert _decode(img) == ["https://10.42.0.1/"]
    plain = qr.render_phone_page(_info(qr="url", tls=False, url="http://10.42.0.1:8080/"), None, RUN)
    assert _decode(plain) == ["http://10.42.0.1:8080/"]
    setup = qr.render_phone_page(_info(qr="setup", setup_url="http://10.42.0.1/"), None, RUN)
    assert _decode(setup) == ["http://10.42.0.1/"]
    # no portal served: 'setup' cannot be shown, the URL is
    assert _decode(qr.render_phone_page(_info(qr="setup", setup_url=""), None, RUN)) == ["https://10.42.0.1/"]


def test_page_states_render(tmp_path: Path) -> None:
    from k1000.state import State
    muted = State.from_dict({**State().to_dict(), "muted": True})
    cases = [
        (_info(on=False), None, RUN),
        (_info(feature=False), None, RUN),
        (_info(ap_up=False), None, RUN),
        (_info(ap_up=None), None, RUN),
        (_info(psk=None, psk_error="no /etc/k1000/ap.psk (run setup-ap.sh)"), None, RUN),
        (_info(error="cannot listen on 10.42.0.1:443 (Permission denied)"), None, RUN),
        (_info(), muted, RUN),
        (_info(), None, ScreenStatus(device_present=False, device="MOTU M4", engine=None)),
        (_info(), None, ScreenStatus(device_present=True, engine=None)),
        (_info(), None, None),
        (None, None, None),
        ({"qr": "url", "url": "https://10.42.0.1/", "stations": "2"}, None, RUN),
        (object(), object(), object()),
    ]
    for info, st, status in cases:
        img = qr.render_phone_page(info, st, status)
        assert img.size == (480, 320)
    # a muted box shows it here too: red frame + red footer (in the text column)
    img = qr.render_phone_page(_info(), muted, RUN)
    assert img.getpixel((1, 200)) == pages.BAND_RED and img.getpixel((470, 300)) == pages.BAND_RED
    # the QR panel is not covered by the footer: its bottom quiet zone is still white
    assert img.getpixel((150, qr.PANEL[3] - 8)) == (255, 255, 255) or qr.qrcode is None
    # NO DEVICE is a footer strip here, the QR stays usable
    nodev = qr.render_phone_page(_info(), None, ScreenStatus(device_present=False, engine=None))
    assert nodev.getpixel((20, 60)) == (255, 255, 255) or qr.qrcode is None


def test_page_never_raises(monkeypatch: Any) -> None:
    def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("bug")
    monkeypatch.setattr(qr, "_body_wifi", boom)
    img = qr.render_phone_page(_info(), None, RUN)
    assert img.size == (480, 320)       # the UI ERROR frame, not an exception


def test_text_fallback_without_qrcode(monkeypatch: Any) -> None:
    monkeypatch.setattr(qr, "qrcode", None)
    qr.qr_matrix.cache_clear()
    qr.qr_image.cache_clear()
    try:
        assert qr.qr_matrix(WIFI) is None and qr.qr_image(WIFI, 254) is None and qr.module_px(WIFI, 254) == 0
        for info in (_info(), _info(qr="url")):
            img = qr.render_phone_page(info, None, RUN)
            x0, y0, x1, y1 = qr.PANEL
            panel = img.crop((x0 + 4, y0 + 4, x1 - 4, y1 - 4)).convert("L")
            assert panel.getextrema()[1] > 100          # text drawn on the dark panel
            assert panel.getpixel((5, 5)) < 60          # ...not a white QR panel
    finally:
        qr.qr_matrix.cache_clear()
        qr.qr_image.cache_clear()


def test_module_imports_without_qrcode(monkeypatch: Any) -> None:
    import builtins
    real = builtins.__import__

    def fake(name: str, *a: Any, **k: Any) -> Any:
        if name == "qrcode" or name.startswith("qrcode."):
            raise ImportError("no qrcode")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake)
    mod = importlib.reload(qr)
    try:
        assert mod.qrcode is None
        assert mod.render_phone_page(_info(), None, RUN).size == (480, 320)
    finally:
        monkeypatch.setattr(builtins, "__import__", real)
        importlib.reload(qr)


def test_snapshot(tmp_path: Path) -> None:
    paths = qr.snapshot(str(tmp_path))
    names = {Path(p).stem for p in paths}
    assert {"phone_wifi", "phone_url", "phone_setup", "phone_off", "phone_ap_down"} <= names
    for p in paths:
        with Image.open(p) as img:
            assert img.size == (480, 320)


def test_screen_info_feeds_the_page(tmp_path: Path) -> None:
    """The server's PhoneScreenInfo is what the page reads (attribute names agree)."""
    from k1000.web.server import PhoneScreenInfo
    fields = set(PhoneScreenInfo.__dataclass_fields__)
    assert fields == set(qr._Info.__dataclass_fields__)
    img = qr.render_phone_page(PhoneScreenInfo(psk=PSK, ap_up=True), None, RUN)
    assert img.size == (480, 320)
    if qr.qrcode is not None:
        try:
            assert _decode(img) == [WIFI]
        except pytest.skip.Exception:
            pass


def test_phone_page_says_why_the_ap_is_down() -> None:
    """A subnet clash (a laptop sharing on eth0) and the no-go (setup-ap.sh --down) each say so."""
    plain = qr.render_phone_page(_info(ap_up=False), None, RUN)
    clash = qr.render_phone_page(_info(ap_up=False, ap_clash="eth0 10.42.0.23"), None, RUN)
    down = qr.render_phone_page(_info(ap_up=False, ap_kept_down=True), None, RUN)
    up_clash = qr.render_phone_page(_info(ap_up=True, ap_clash="eth0 10.42.0.23"), None, RUN)
    for img in (plain, clash, down, up_clash):
        assert img.size == (480, 320)
    assert clash.tobytes() != plain.tobytes() != down.tobytes() != clash.tobytes()
    assert up_clash.tobytes() != qr.render_phone_page(_info(ap_up=True), None, RUN).tobytes()
    assert qr._Info.of({"ap_clash": None, "ap_kept_down": "yes"}).ap_kept_down is False
