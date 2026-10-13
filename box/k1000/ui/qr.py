# SPDX-License-Identifier: GPL-3.0-or-later
"""The PHONE page of the box screen (480x320): the Wi-Fi QR to join the K1000 AP, then the URL QR
once a phone is on it (D-007, docs/06-phone.md).

Why it looks like this:
- **Scannable from 50 cm.** The QR is black on a white panel (inverted codes defeat many
  scanners), with a quiet zone and whole-pixel modules as large as the panel allows. The panel
  takes the whole height under the status bar (286 px): 8 px modules for the Wi-Fi code (29
  modules: ~1.23 mm each, ~36 mm across on the 3.5" panel; about 2.5 camera pixels per module
  at 50 cm in a 1280 px frame, above the ~2 px decoders need), 9 px for the URL code.
  The URL stays lowercase (byte mode): an uppercase 'HTTPS://' would be a smaller code, but
  not every Android scanner offers an uppercase scheme as a link.
- **Two steps, one code at a time.** Step 1 joins the Wi-Fi (Android 10+ joins from a
  `WIFI:` code in one tap); once a phone holds a lease (or is connected) the panel switches to
  the URL. E1 steps through the codes by hand: Wi-Fi, URL, and the first-time setup page
  (http://10.42.0.1/: the certificate download), for a second phone or a first install.
- **The password is also printed** (easy-to-type alphabet, setup-ap.sh): the fallback when a
  camera will not scan. It appears on the box screen only. While SETUP > Laptop access is open
  it is printed on every step (a laptop cannot scan the QR), with the time left.
- **Same language as the other pages** (pages.py): status bar, amber labels, red frame + footer
  while MUTED. NO DEVICE / ENGINE DOWN become a footer strip here instead of hiding the QR: this
  page is used during setup, when the interface may not be plugged yet.
- **Never raises**, like pages.render: no python3-qrcode -> the codes are replaced by the same
  information as text; anything unexpected -> pages' UI ERROR frame.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from PIL import Image

from . import pages
from .pages import AMBER, BAND_RED, DIM, FAINT, FG, GREEN, ORANGE, RED, H, W, font

try:  # python3-qrcode (apt). Missing -> text fallback, never a crash.
    import qrcode
    from qrcode.constants import ERROR_CORRECT_L, ERROR_CORRECT_M, ERROR_CORRECT_Q
    _ECC = {"L": ERROR_CORRECT_L, "M": ERROR_CORRECT_M, "Q": ERROR_CORRECT_Q}
except Exception:  # noqa: BLE001 - ImportError, or a broken install
    qrcode = None
    _ECC = {}

log = logging.getLogger(__name__)

FONT_MONO = "DejaVuSansMono-Bold.ttf"
PAGE_NAME = "PHONE"
QUIET = 3                       # quiet-zone modules around the code (spec says 4; 3 + the
                                # white panel edge scans reliably and buys a bigger module)
PANEL = (4, pages.STATUS_H + 2, 4 + 286, pages.STATUS_H + 2 + 286)   # white QR panel, 286 px
COL_X0, COL_X1 = 300, W - 6     # the text column (174 px)
FOOTER_Y = H - 46               # the column's footer: hints, or MUTED


# ----------------------------------------------------------------------------- payloads
def wifi_escape(text: str) -> str:
    """Escape `\\ ; , " :` as the WIFI: QR format requires (ZXing's MECARD-like syntax)."""
    return "".join("\\" + ch if ch in '\\;,":' else ch for ch in str(text))


def wifi_payload(ssid: str, psk: str | None, *, hidden: bool = False) -> str:
    """'WIFI:T:WPA;S:<ssid>;P:<psk>;;' (no password -> an open network code)."""
    if psk:
        out = f"WIFI:T:WPA;S:{wifi_escape(ssid)};P:{wifi_escape(psk)};"
    else:
        out = f"WIFI:T:nopass;S:{wifi_escape(ssid)};"
    if hidden:
        out += "H:true;"
    return out + ";"


# ----------------------------------------------------------------------------- codes
@lru_cache(maxsize=16)
def qr_matrix(data: str, ecc: str = "M") -> tuple[tuple[bool, ...], ...] | None:
    """The module matrix (True = dark), without quiet zone; None without python3-qrcode."""
    if qrcode is None:
        return None
    q = qrcode.QRCode(version=None, error_correction=_ECC.get(ecc, _ECC["M"]), box_size=1, border=0)
    q.add_data(data)
    q.make(fit=True)
    return tuple(tuple(bool(v) for v in row) for row in q.get_matrix())


@lru_cache(maxsize=16)
def qr_image(data: str, max_px: int, quiet: int = QUIET, ecc: str = "M") -> Image.Image | None:
    """Black-on-white code, whole-pixel modules, as large as fits in `max_px` (RGB), or None."""
    m = qr_matrix(data, ecc)
    if m is None:
        return None
    n = len(m)
    total = n + 2 * quiet
    px = max(1, max_px // total)
    small = Image.new("L", (total, total), 255)
    dark = [(x + quiet, y + quiet) for y, row in enumerate(m) for x, v in enumerate(row) if v]
    for xy in dark:
        small.putpixel(xy, 0)
    return small.resize((total * px, total * px), Image.NEAREST).convert("RGB")


def module_px(data: str, max_px: int, quiet: int = QUIET, ecc: str = "M") -> int:
    """Pixels per module for `data` in `max_px` (0 without python3-qrcode)."""
    m = qr_matrix(data, ecc)
    return 0 if m is None else max(1, max_px // (len(m) + 2 * quiet))


# ----------------------------------------------------------------------------- the page
@dataclass
class _Info:
    """Tolerant view of web.server.PhoneScreenInfo (any object with these attributes)."""

    feature: bool = True
    on: bool = True
    ap_up: bool | None = None
    ssid: str = "K1000"
    psk: str | None = None
    psk_error: str = ""
    url: str = "https://10.42.0.1/"
    setup_url: str = ""
    stations: int = 0
    phones: int = 0
    tls: bool = True
    qr: str = "wifi"
    band: str = ""
    error: str = ""
    ap_clash: str = ""
    ap_kept_down: bool = False
    laptop_left: float = 0.0        # SETUP > Laptop access open: seconds left

    @classmethod
    def of(cls, info: Any) -> "_Info":
        d = cls()
        if info is None:
            return d
        out = cls()
        for k in d.__dataclass_fields__:
            v = getattr(info, k, None) if not isinstance(info, dict) else info.get(k)
            if v is None and k not in ("ap_up", "psk"):
                continue
            setattr(out, k, v)
        out.ssid = str(out.ssid or "K1000")
        out.url = str(out.url or d.url)
        out.qr = out.qr if out.qr in ("wifi", "url", "setup") else "wifi"
        out.setup_url = str(out.setup_url or "")
        out.ap_clash = str(out.ap_clash or "")
        out.ap_kept_down = out.ap_kept_down is True
        if out.qr == "setup" and not out.setup_url:
            out.qr = "url"
        for k in ("stations", "phones"):
            try:
                setattr(out, k, max(0, int(getattr(out, k))))
            except (TypeError, ValueError):
                setattr(out, k, 0)
        return out


def _text_fit(c: Any, xy: tuple[float, float], text: str, size: int, max_w: int, fill: Any,
              anchor: str = "la", face: str = pages.FONT_BOLD) -> int:
    """Draw `text` at the largest size <= `size` that fits; returns the size used."""
    f = pages._fit(c.d, text, size, max_w, face)
    c.d.text(xy, text, font=f, fill=fill, anchor=anchor)
    return f.size if hasattr(f, "size") else size


def _panel(c: Any, img: Image.Image | None, lines: list[tuple[str, Any]] | None = None,
           dim: bool = False) -> None:
    x0, y0, x1, y1 = PANEL
    if img is not None:
        c.d.rectangle(PANEL, fill=(255, 255, 255) if not dim else (90, 90, 90))
        ox = x0 + ((x1 - x0) - img.width) // 2
        oy = y0 + ((y1 - y0) - img.height) // 2
        c.img.paste(img, (ox, oy))
        return
    c.d.rounded_rectangle(PANEL, radius=10, fill=(22, 22, 22), outline=FAINT, width=2)
    lines = lines or []
    cy = (y0 + y1) / 2 - (len(lines) - 1) * 17
    for text, col in lines:
        _text_fit(c, ((x0 + x1) / 2, cy), text, 22, (x1 - x0) - 20, col, anchor="mm")
        cy += 34


def _host_of(url: str) -> tuple[str, str]:
    """('https://', '10.42.0.1') for display; port kept with the host."""
    for scheme in ("https://", "http://"):
        if url.startswith(scheme):
            return scheme, url[len(scheme):].rstrip("/")
    return "", url


def _lines(c: Any, y: int, lines: list[str], size: int = 12, fill: Any = DIM) -> None:
    for text in lines:
        _text_fit(c, (COL_X0, y), text, size, COL_X1 - COL_X0, fill, anchor="lm", face=pages.FONT_REG)
        y += size + 2


def _footer(c: Any, state: Any, status: Any, muted: bool, hint: list[str]) -> None:
    """Bottom of the text column: MUTED, else NO DEVICE / ENGINE DOWN, else the knob hints."""
    x0, y0, x1, y1 = COL_X0 - 6, FOOTER_Y, W, H
    if muted:
        c.d.rectangle((x0, y0, x1, y1), fill=BAND_RED)
        c.d.text((COL_X0, y0 + 15), "MUTED", font=font(22), fill=FG, anchor="lm")
        _text_fit(c, (COL_X0, y0 + 35), "hold B4 to unmute", 13, COL_X1 - COL_X0, FG, anchor="lm",
                  face=pages.FONT_REG)
        return
    if pages.device_present(status) is False:
        c.d.rectangle((x0, y0, x1, y1), fill=(60, 28, 0))
        dev = str(pages._get(status, "device", "") or "audio interface")
        _text_fit(c, (COL_X0, y0 + 14), "NO DEVICE", 18, COL_X1 - COL_X0, ORANGE, anchor="lm")
        _text_fit(c, (COL_X0, y0 + 34), f"{dev} not found", 13, COL_X1 - COL_X0, ORANGE, anchor="lm",
                  face=pages.FONT_REG)
        return
    eng = pages.engine_state(status)
    if eng is None or eng.upper() in pages.ENGINE_DOWN_STATES:
        c.d.rectangle((x0, y0, x1, y1), fill=(60, 0, 0))
        _text_fit(c, (COL_X0, y0 + 14), "ENGINE DOWN", 18, COL_X1 - COL_X0, RED, anchor="lm")
        _text_fit(c, (COL_X0, y0 + 34), "no audio", 13, COL_X1 - COL_X0, RED, anchor="lm",
                  face=pages.FONT_REG)
        return
    _lines(c, y0 + 6, hint)


def _status_lines(c: Any, inf: _Info, y: int) -> None:
    """AP state + phones, above the column's footer."""
    x = COL_X0
    if inf.ap_up is True:
        col, txt = GREEN, "Wi-Fi AP ON"
    elif inf.ap_up is False:
        col, txt = RED, "Wi-Fi AP OFF"
    else:
        col, txt = AMBER, "Wi-Fi AP ?"
    c.d.ellipse((x, y - 6, x + 12, y + 6), fill=col)
    _text_fit(c, (x + 18, y), txt, 16, COL_X1 - x - 18, col, anchor="lm")
    y += 20
    if inf.band:
        _text_fit(c, (x + 18, y), inf.band, 13, COL_X1 - x - 18, DIM, anchor="lm", face=pages.FONT_REG)
        y += 17
    who = f"{inf.stations} on Wi-Fi · {inf.phones} on K1000"
    _text_fit(c, (x + 18, y), who, 13, COL_X1 - x - 18, FG if inf.phones else DIM, anchor="lm",
              face=pages.FONT_REG)


STATUS_Y = 214


def _laptop_left(status: Any) -> float:
    """Seconds of laptop access left (0 = closed), from the controller's status."""
    if pages._get(status, "laptop_state", "") != "open":
        return 0.0
    return max(0.0, pages._num(status, "laptop_s", 0.0) or 0.0)


def _laptop_tag(c: Any, inf: _Info, y: int) -> None:
    _text_fit(c, (COL_X0, y), f"LAPTOP OPEN {pages.fmt_hmm(inf.laptop_left)}", 15,
              COL_X1 - COL_X0, AMBER)


def _body_wifi(c: Any, inf: _Info) -> None:
    x, w = COL_X0, COL_X1 - COL_X0
    c.d.text((x, 38), "1 · JOIN WI-FI", font=font(16), fill=AMBER, anchor="la")
    _text_fit(c, (x, 58), inf.ssid, 40, w, FG)
    c.d.text((x, 106), "scan to join", font=font(15, pages.FONT_REG), fill=DIM, anchor="la")
    c.d.text((x, 134), "password", font=font(13, pages.FONT_REG), fill=DIM, anchor="la")
    if inf.psk:
        _text_fit(c, (x, 150), inf.psk, 22, w, FG, face=FONT_MONO)
    else:
        _text_fit(c, (x, 152), inf.psk_error or "unknown", 13, w, ORANGE, face=pages.FONT_REG)
    if inf.laptop_left > 0:
        _laptop_tag(c, inf, 182)
    _status_lines(c, inf, STATUS_Y)


def _body_link(c: Any, inf: _Info, step: str, url: str, lines: list[str]) -> None:
    x, w = COL_X0, COL_X1 - COL_X0
    c.d.text((x, 38), step, font=font(16), fill=AMBER, anchor="la")
    scheme, host = _host_of(url)
    c.d.text((x, 64), scheme or " ", font=font(14, pages.FONT_REG), fill=DIM, anchor="la")
    _text_fit(c, (x, 82), host, 30, w, FG)
    y = 124
    if inf.laptop_left > 0:
        # SETUP > Laptop access is open: the password as text, for a laptop that cannot scan
        _laptop_tag(c, inf, 122)
        c.d.text((x, 144), "Wi-Fi password", font=font(13, pages.FONT_REG), fill=DIM, anchor="la")
        if inf.psk:
            _text_fit(c, (x, 160), inf.psk, 22, w, FG, face=FONT_MONO)
        else:
            _text_fit(c, (x, 162), inf.psk_error or "unknown", 13, w, ORANGE, face=pages.FONT_REG)
        _status_lines(c, inf, STATUS_Y)
        return
    for text, col in lines:
        _text_fit(c, (x, y), text, 14, w, col, face=pages.FONT_REG)
        y += 19
    _status_lines(c, inf, STATUS_Y)


def _body_url(c: Any, inf: _Info) -> None:
    lines = [("scan with the camera,", DIM), ("or tap the K1000 app", DIM)]
    if not inf.tls:
        lines.append(("no HTTPS: page only", ORANGE))
    _body_link(c, inf, "2 · OPEN K1000", inf.url, lines)


def _body_setup(c: Any, inf: _Info) -> None:
    _body_link(c, inf, "FIRST TIME", inf.setup_url,
               [("the K1000 certificate", FG), ("+ setup help,", DIM), ("once per phone", DIM)])


def _render(info: Any, state: Any, status: Any, focus: Any, now: float | None) -> Image.Image:
    from .input_map import Focus
    inf = _Info.of(info)
    try:
        inf.laptop_left = max(_laptop_left(status), float(inf.laptop_left or 0.0))
    except (TypeError, ValueError):
        inf.laptop_left = _laptop_left(status)
    c = pages._Ctx(Focus() if focus is None else focus, time.monotonic() if now is None else now)
    muted = pages.displayed_muted(state, status)
    pages._status_bar(c, PAGE_NAME, state, status, muted)
    hint = ["E1 turn: next code", "E1 push: phone on/off", "B1: back"]
    if not inf.feature:
        _panel(c, None, [("PHONE DISABLED", ORANGE), ("k1000.yaml", DIM), ("phone.enabled: false", DIM)])
        c.d.text((COL_X0, 40), "KNOBS ONLY", font=font(20), fill=AMBER, anchor="la")
        _text_fit(c, (COL_X0, 72), "no phone servers", 15, COL_X1 - COL_X0, DIM, face=pages.FONT_REG)
        hint = ["B1: back"]
    elif not inf.on:
        _panel(c, None, [("PHONE OFF", AMBER), ("knobs only", DIM)])
        c.d.text((COL_X0, 38), "PHONE CONTROL", font=font(16), fill=AMBER, anchor="la")
        c.d.text((COL_X0, 60), "OFF", font=font(44), fill=FG, anchor="la")
        _text_fit(c, (COL_X0, 116), "push E1 to turn it on", 15, COL_X1 - COL_X0, DIM, face=pages.FONT_REG)
        _status_lines(c, inf, STATUS_Y)
        hint = ["E1 push: phone on", "B1: back"]
    elif inf.ap_up is False:
        if inf.ap_clash:          # another interface in the AP's subnet: the AP is kept down
            _panel(c, None, [("Wi-Fi AP is OFF", RED), (inf.ap_clash, ORANGE),
                             ("is in the AP subnet", DIM), ("laptop sharing on eth0?", DIM)])
        elif inf.ap_kept_down:    # setup-ap.sh --down: the no-go
            _panel(c, None, [("Wi-Fi AP is OFF", RED), ("kept down (no-go)", DIM),
                             ("back: setup-ap.sh --up", DIM)])
        else:
            _panel(c, None, [("Wi-Fi AP is OFF", RED), ("sudo box/net/setup-ap.sh", DIM),
                             ("(or check rfkill)", DIM)])
        c.d.text((COL_X0, 40), "NO WI-FI", font=font(20), fill=RED, anchor="la")
        _text_fit(c, (COL_X0, 72), f"SSID {inf.ssid}", 15, COL_X1 - COL_X0, DIM, face=pages.FONT_REG)
        _status_lines(c, inf, STATUS_Y)
    else:
        if inf.qr in ("url", "setup"):
            data = inf.url if inf.qr == "url" else inf.setup_url
            img = qr_image(data, PANEL[2] - PANEL[0], QUIET, "M")
            _panel(c, img, None if img else [("QR unavailable", AMBER), ("python3-qrcode", DIM),
                                              (_host_of(data)[1], FG)])
            (_body_url if inf.qr == "url" else _body_setup)(c, inf)
        else:
            img = None
            if inf.psk:
                img = qr_image(wifi_payload(inf.ssid, inf.psk), PANEL[2] - PANEL[0], QUIET, "M")
                lines = [("QR unavailable", AMBER), ("python3-qrcode", DIM), ("type the password", DIM)]
            else:
                lines = [("no Wi-Fi password", ORANGE), ("run setup-ap.sh", DIM)]
            _panel(c, img, None if img else lines)
            _body_wifi(c, inf)
        err = inf.error or (f"SUBNET CLASH: {inf.ap_clash}" if inf.ap_clash else "")
        if err:
            c.d.rectangle((COL_X0 - 4, 180, COL_X1, 200), fill=(60, 0, 0))
            _text_fit(c, (COL_X0, 190), err, 12, COL_X1 - COL_X0 - 4, FG, anchor="lm",
                      face=pages.FONT_REG)
    _footer(c, state, status, muted, hint)
    if muted:
        c.d.rectangle((0, pages.STATUS_H, W - 1, H - 1), outline=BAND_RED, width=4)
    return c.img


def render_phone_page(info: Any, state: Any = None, status: Any = None, focus: Any = None,
                      now: float | None = None) -> Image.Image:
    """The PHONE page, 480x320 RGB. Never raises (pages' UI ERROR frame on a bug)."""
    try:
        return _render(info, state, status, focus, now)
    except Exception as e:  # noqa: BLE001 - the screen must keep updating
        pages._log_render_error(e)
        return pages._error_frame(state, status, e)


# ----------------------------------------------------------------------------- snapshots
def demo_cases() -> list[tuple[str, Any, Any, Any]]:
    """(name, info, state, status) for docs/ui/phone_*.png and the tests."""
    from ..state import State
    run = pages.ScreenStatus(profile="m4", device="MOTU M4", device_present=True, engine="Running",
                             load=7.4, levels=(-20.0, -20.0, -14.0, -14.0))
    s = State()
    base = dict(feature=True, on=True, ap_up=True, ssid="K1000", psk="m7kq-4tzh-p2xv",
                url="https://10.42.0.1/", setup_url="http://10.42.0.1/", stations=0, phones=0,
                tls=True, qr="wifi",
                band="5 GHz · ch 36")
    muted = State.from_dict({**s.to_dict(), "muted": True})
    run_m = pages.ScreenStatus(**{**run.__dict__, "muted": True})
    nodev = pages.ScreenStatus(profile="m4", device="MOTU M4", device_present=False, engine=None)
    return [
        ("phone_wifi", _Info(**base), s, run),
        ("phone_url", _Info(**{**base, "qr": "url", "stations": 1}), s, run),
        ("phone_connected", _Info(**{**base, "qr": "url", "stations": 1, "phones": 1}), muted, run_m),
        ("phone_off", _Info(**{**base, "on": False}), s, run),
        ("phone_ap_down", _Info(**{**base, "ap_up": False}), s, nodev),
        ("phone_no_tls", _Info(**{**base, "qr": "url", "stations": 1, "tls": False,
                                   "url": "http://10.42.0.1:8080/"}), s, run),
        ("phone_setup", _Info(**{**base, "qr": "setup", "stations": 1}), s, run),
        ("phone_laptop", _Info(**{**base, "qr": "url", "stations": 2, "phones": 1}), s,
         pages.ScreenStatus(**{**run.__dict__, "laptop_s": 7140.0, "laptop_state": "open"})),
    ]


def snapshot(outdir: str) -> list[str]:
    import os
    os.makedirs(outdir, exist_ok=True)
    out = []
    for name, info, st, stat in demo_cases():
        path = os.path.join(outdir, f"{name}.png")
        render_phone_page(info, st, stat).save(path)
        out.append(path)
    return out


if __name__ == "__main__":
    import sys
    for p in snapshot(sys.argv[1] if len(sys.argv) > 1 else "."):
        print(p)
