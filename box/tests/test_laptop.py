# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for SETUP > Laptop access (docs/06-phone.md §11), the k1000 side: laptop.LaptopAccess
following root's status file, its wiring into the Controller (the SETUP double push, a single
push to close, the countdown pill, hints, snapshot fields), the SETUP and PHONE pictures, and
the /tune page's gate on the phone servers (closed = 403, open = the page and its websocket,
closing = every tune page dropped with 4004).

Root's side (net/k1000-laptop-access.sh, the units) is tested in tools/tests/test_laptop_access.py
against a fake nft. Here "root" is the test writing the status file the way the script does.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from k1000 import laptop as lp
from k1000.control import HINT_LAPTOP_CLOSED, HINT_LAPTOP_NA, HINT_LAPTOP_OPEN, HINT_LAPTOP_OPENING
from k1000.laptop import LaptopAccess, fmt_hmm
from k1000.ui import input_map as im
from k1000.ui import pages, qr
from k1000.web.server import CLOSE_BUSY, CLOSE_LAPTOP, PhoneServer

from test_web import boot, http_get, make_ctl, recv_until, run, settings, until


class Root:
    """Writes /run/k1000-laptop/status the way k1000-laptop-access.sh does."""

    def __init__(self, d: Path, up: list[float]) -> None:
        self.status = d / "status"
        self.up = up

    def open(self, seconds: float = 7200.0) -> None:
        now = int(self.up[0])
        self.status.write_text(f"state=open\nuntil={now + int(seconds)}\nat={now}\nports=22,5005\n"
                               "subnet=10.42.0.0/24\niface=wlan0\n")

    def closed(self, reason: str) -> None:
        self.status.write_text(f"state=closed\nreason={reason}\nat={int(self.up[0])}\n")


def make_lap(tmp_path: Path) -> tuple[LaptopAccess, Root, list[float], list[float]]:
    (tmp_path / "run").mkdir(exist_ok=True)
    (tmp_path / "root").mkdir(exist_ok=True)
    up, clk = [1000.4], [50.0]
    lap = LaptopAccess(flag=tmp_path / "run" / "laptop-open", status=tmp_path / "root" / "status",
                       uptime=lambda: up[0], clock=lambda: clk[0])
    return lap, Root(tmp_path / "root", up), up, clk


# ======================================================================= LaptopAccess
def test_fmt_hmm_rounds_minutes_up():
    assert [fmt_hmm(s) for s in (7200, 7199, 7140, 7139, 3600, 61, 60, 30, 0.2, 0, -5)] == \
        ["2:00", "2:00", "1:59", "1:59", "1:00", "0:02", "0:01", "0:01", "0:01", "0:00", "0:00"]
    assert pages.fmt_hmm(7139) == "1:59"


def test_open_follows_root_then_one_close(tmp_path: Path):
    lap, root, up, clk = make_lap(tmp_path)
    seen: list[str] = []
    lap.on_change = lambda: seen.append(lap.state)
    assert lap.state == lp.CLOSED and lap.pill() is None and lap.left_s() == 0.0
    assert lap.open() and lap.flag.read_text() == "open\n" and lap.state == lp.OPENING
    assert lap.toggle() is False and lap.state == lp.CLOSED and not lap.flag.exists()
    assert lap.toggle() is True and lap.state == lp.OPENING and lap.flag.exists()
    lap.poll()
    assert lap.state == lp.OPENING and not lap.poll()          # nothing from root yet
    root.open()
    assert lap.poll() and lap.state == lp.OPEN and lap.is_open
    assert lap.left_s() == pytest.approx(7200 - 0.4) and lap.pill() == "LAPTOP OPEN 2:00"
    up[0] += 61
    assert lap.poll() and lap.pill() == "LAPTOP OPEN 1:59"        # the minute shown changed
    up[0] += 1
    assert not lap.poll()                                          # same minute: no wake-up
    assert lap.toggle() is False and lap.state == lp.CLOSING and not lap.flag.exists()
    lap.poll()
    assert lap.state == lp.CLOSING                                 # root closes within a second
    root.closed("box")
    assert lap.poll() and lap.state == lp.CLOSED and lap.message == "" and lap.reason == "box"
    assert lp.OPEN in seen and seen[-1] == lp.CLOSED


def test_root_closes_after_two_hours_and_says_so(tmp_path: Path):
    lap, root, up, clk = make_lap(tmp_path)
    closes: list[str] = []
    lap.on_close = closes.append
    lap.open()
    root.open()
    lap.poll()
    up[0] += 7200
    lap.poll()                                         # deadline passed, before root's write
    assert lap.state == lp.CLOSED and lap.reason == "time" and not lap.flag.exists()
    assert lap.take_message() == "LAPTOP ACCESS CLOSED (2 H)" and lap.take_message() == ""
    assert closes == ["time"]


@pytest.mark.parametrize("reason, msg", [("no-fence", "LAPTOP ACCESS NO WI-FI FENCE (AP NOT SET UP)"),
                                         ("no-ap", "LAPTOP ACCESS NO AP SETTINGS (SETUP-AP.SH)"),
                                         ("error", "LAPTOP ACCESS FAILED (SEE LOG)")])
def test_a_refusal_from_root_is_shown(tmp_path: Path, reason: str, msg: str):
    lap, root, up, clk = make_lap(tmp_path)
    root.closed("time")                                # an old status: not an answer
    up[0] += 5
    lap.open()
    lap.poll()
    assert lap.state == lp.OPENING
    root.closed(reason)
    lap.poll()
    assert lap.state == lp.CLOSED and lap.message == msg and not lap.flag.exists()


def test_no_answer_from_root(tmp_path: Path):
    lap, root, up, clk = make_lap(tmp_path)
    lap.open()
    clk[0] += lp.ANSWER_S - 0.5
    lap.poll()
    assert lap.state == lp.OPENING
    clk[0] += 1.0
    lap.poll()
    assert lap.state == lp.CLOSED and lap.message == "LAPTOP ACCESS: NO ANSWER (SEE LOG)"
    assert not lap.flag.exists()                       # never left behind for a late root


def test_k1000_runtime_dir_gone_closes(tmp_path: Path):
    lap, root, up, clk = make_lap(tmp_path)
    lap.open()
    root.open()
    lap.poll()
    lap.flag.unlink()                                  # systemd removed /run/k1000
    lap.poll()
    assert lap.state == lp.CLOSED and lap.reason == "k1000"


def test_cannot_ask_without_its_runtime_directory(tmp_path: Path, caplog: Any):
    lap = LaptopAccess(flag=tmp_path / "nope" / "laptop-open", status=tmp_path / "status")
    assert lap.open() is False and lap.state == lp.UNAVAILABLE
    assert lap.take_message() == "LAPTOP ACCESS: NOT AVAILABLE"
    assert "RuntimeDirectory=k1000" in caplog.text


def test_the_flag_is_never_written_through_a_symlink(tmp_path: Path):
    lap, root, up, clk = make_lap(tmp_path)
    target = tmp_path / "precious"
    target.write_text("keep\n")
    os.symlink(target, lap.flag)
    assert lap.open() is False and target.read_text() == "keep\n"


def test_shutdown_removes_the_request(tmp_path: Path):
    lap, root, up, clk = make_lap(tmp_path)
    lap.open()
    lap.shutdown()
    assert not lap.flag.exists()
    lap.shutdown()                                     # twice: quiet


def test_status_file_is_read_tolerantly(tmp_path: Path):
    p = tmp_path / "status"
    assert lp.read_status(p) == {}
    p.write_bytes(b"state=open\nuntil=12\n\xff\xfe=junk\nnot a line\n" + b"x=" + b"y" * 200 + b"\n")
    assert lp.read_status(p) == {"state": "open", "until": "12"}
    (tmp_path / "up").write_text("12345.67 999.0\n")
    assert lp.read_uptime(tmp_path / "up") == 12345.67
    assert lp.read_uptime(tmp_path / "missing") is None


# ======================================================================= the Controller
def _sw(idx: int, kind: str) -> Any:
    from k1000.hw.mcp23017 import SwitchEvent
    return SwitchEvent(idx, kind, 0.0)


def _push(ctl: Any, n: int = 0) -> int:
    return next(i for i in range(8) if ctl.controls.switch_role(i) == ("P", n))


def _select(ctl: Any) -> None:
    e1 = _push(ctl)
    ctl.handle_event(_sw(e1, "press"))
    ctl.handle_event(_sw(e1, "short"))


def test_setup_laptop_access_end_to_end_with_the_controller(tmp_path: Path):
    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        snap = ctl.snapshot()[2]
        assert (snap.laptop_s, snap.laptop_state) == (0.0, "off")
        assert ctl.laptop_toggle() is None and ctl.focus.hint_now() == HINT_LAPTOP_NA
        lap, root, up, clk = make_lap(tmp_path)
        ctl.attach_laptop(lap)
        assert ctl.snapshot()[2].laptop_state == "closed"
        ctl.goto_page("SETUP")
        ctl.focus.item = [i.field for i in im.SETUP_ITEMS].index(im.A_LAPTOP)
        _select(ctl)                                           # first push: the confirm
        assert ctl.focus.hint_now() == im.HINT_CONFIRM_LAPTOP and not lap.flag.exists()
        _select(ctl)                                           # second push: asked
        assert lap.flag.exists() and lap.state == lp.OPENING
        assert ctl.focus.hint_now() == HINT_LAPTOP_OPENING
        assert ctl.snapshot()[2].laptop_state == "opening"
        root.open()
        lap.poll()
        assert ctl.focus.hint_now() == HINT_LAPTOP_OPEN and im.A_LAPTOP in ctl.focus.no_confirm
        snap = ctl.snapshot()[2]
        assert snap.laptop_state == "open" and snap.laptop_s == pytest.approx(7199.6, abs=0.1)
        assert snap.pills[0] == "LAPTOP OPEN 2:00"                 # first: never folded into +N
        assert "OPEN 2:00" == pages._laptop_value(snap)
        _select(ctl)                                           # one push closes
        assert not lap.flag.exists() and ctl.focus.hint_now() == HINT_LAPTOP_CLOSED
        assert im.A_LAPTOP not in ctl.focus.no_confirm
        root.closed("box")
        lap.poll()
        snap = ctl.snapshot()[2]
        assert snap.laptop_state == "closed" and "LAPTOP OPEN" not in " ".join(snap.pills)
        # closed by time: the screen says so
        _select(ctl); _select(ctl)
        root.open(); lap.poll()
        up[0] += 7300
        lap.poll()
        assert ctl.focus.hint_now() == "LAPTOP ACCESS CLOSED (2 H)"
        assert im.A_LAPTOP not in ctl.focus.no_confirm
        await ctl.stop()
    run(go())


def test_setup_and_phone_pictures_while_open():
    s = pages.ScreenStatus(profile="m4", device="MOTU M4", device_present=True, engine="Running")
    open_ = pages.ScreenStatus(**{**s.__dict__, "laptop_s": 7140.0, "laptop_state": "open"})
    for st, want in ((s, "N/A"), (open_, "OPEN 1:59"),
                     (pages.ScreenStatus(**{**s.__dict__, "laptop_state": "closed"}), "PUSH ×2"),
                     (pages.ScreenStatus(**{**s.__dict__, "laptop_state": "opening"}), "OPENING…")):
        assert pages._laptop_value(st) == want
    from k1000.state import State
    f = im.Focus(item=[i.field for i in im.SETUP_ITEMS].index(im.A_LAPTOP))
    a = pages.render("SETUP", State(), s, f)
    b = pages.render("SETUP", State(), open_, f)
    assert a.size == b.size == (480, 320) and a.tobytes() != b.tobytes()
    info = {"feature": True, "on": True, "ap_up": True, "ssid": "K1000", "psk": "m7kq-4tzh-p2xv",
            "url": "https://10.42.0.1/", "qr": "url", "stations": 1, "tls": True}
    closed_pic = qr.render_phone_page(info, State(), s)
    open_pic = qr.render_phone_page(info, State(), open_)
    col = (qr.COL_X0, 118, qr.COL_X1, 190)
    assert closed_pic.crop(col).tobytes() != open_pic.crop(col).tobytes()
    # the same from the phone surface's own field (PhoneScreenInfo.laptop_left)
    via_info = qr.render_phone_page({**info, "laptop_left": 7140.0}, State(), s)
    assert via_info.crop(col).tobytes() == open_pic.crop(col).tobytes()
    # the Wi-Fi step keeps its layout and adds the countdown under the password
    w_closed = qr.render_phone_page({**info, "qr": "wifi"}, State(), s)
    w_open = qr.render_phone_page({**info, "qr": "wifi"}, State(), open_)
    assert w_closed.crop((qr.COL_X0, 30, qr.COL_X1, 178)).tobytes() == \
        w_open.crop((qr.COL_X0, 30, qr.COL_X1, 178)).tobytes()
    assert w_closed.crop((qr.COL_X0, 178, qr.COL_X1, 200)).tobytes() != \
        w_open.crop((qr.COL_X0, 178, qr.COL_X1, 200)).tobytes()


# ======================================================================= the /tune gate
class FakeLap:
    def __init__(self) -> None:
        self.state, self.left = "closed", 0.0

    def left_s(self) -> float:
        return self.left

    def set(self, on: bool) -> None:
        self.state, self.left = ("open", 7000.0) if on else ("closed", 0.0)


REW = ("Filter  1: ON  PK       Fc   125.0 Hz  Gain  -4.00 dB  Q  5.000\n"
       "Filter  2: ON  PK       Fc   2440 Hz  Gain  -3.00 dB  Q  8.650\nFilter  3: ON  None\n")


def test_tune_page_is_reachable_only_while_laptop_access_is_open(tmp_path: Path):
    async def go() -> None:
        ctl, eng = make_ctl(tmp_path)
        await boot(ctl)
        ctl.laptop = fake = FakeLap()
        srv = PhoneServer(ctl, settings(tmp_path), tick_s=0.02, tune_undo_file=tmp_path / "undo.json")
        await srv.start()
        port = srv.ports["plain"]
        origin, url = f"http://127.0.0.1:{port}", f"ws://127.0.0.1:{port}/tune/ws"
        st, h, body = await http_get(port, "/tune")
        assert st == 403 and b"laptop access is closed" in body and h["cache-control"] == "no-store"
        assert (await http_get(port, "/tune/tune.js"))[0] == 403
        with pytest.raises(InvalidStatus):
            async with ws_connect(url, origin=origin):
                pass
        fake.set(True)
        st, h, body = await http_get(port, "/tune")
        assert st == 200 and b"/tune/tune.js" in body and h["cache-control"] == "no-store"
        assert f"ws://127.0.0.1:{port}" in h["content-security-policy"]
        for path, ctype in (("/tune/tune.js", "text/javascript"), ("/tune/tune.css", "text/css"),
                            ("/tune/", "text/html")):
            st, h, _ = await http_get(port, path)
            assert st == 200 and h["content-type"].startswith(ctype), path
        for bad in ("/tune/nope", "/tune.js", "/tune/../app.js", "/tune/index.htm"):
            assert (await http_get(port, bad))[0] == 404, bad
        with pytest.raises(InvalidStatus):                       # Origin checked like /ws
            async with ws_connect(url, origin="http://evil.example"):
                pass
        async with ws_connect(url, origin=origin) as ws:
            hello = json.loads(await ws.recv())
            assert hello["t"] == "hello" and hello["left_s"] == 7000.0 and hello["undo"] is None
            assert len(hello["current"]) == 8 and len(hello["curve_now"]["f"]) == 160
            await ws.send(json.dumps({"t": "preview", "text": REW, "id": 1}))
            p = await recv_until(ws, lambda m: m["t"] == "preview")
            assert p["ok"] and p["id"] == 1 and p["bands"][1] == {"freq": 2440.0, "gain": -3.0, "q": 8.65}
            assert ctl.system_eq()[0]["gain"] == 0.0               # a preview changes nothing
            await ws.send(json.dumps({"t": "apply", "text": REW, "id": 2}))
            a = await recv_until(ws, lambda m: m["t"] == "applied", timeout=10)
            assert a["ok"] and a["saved"] and a["undo"][0]["gain"] == 0.0
            assert ctl.system_eq()[0] == {"freq": 125.0, "gain": -4.0, "q": 5.0}
            assert eng.cfg["filters"]["sys_peq_2"]["parameters"]["freq"] == 2440.0
            await ws.send(json.dumps({"t": "apply", "text": "Filter 1: ON LS Fc 100 Hz Gain 3 dB"}))
            bad = await recv_until(ws, lambda m: m["t"] == "applied")
            assert not bad["ok"] and bad["why"].startswith("no peaking filter")
            await ws.send(json.dumps({"t": "undo", "id": 3}))
            u = await recv_until(ws, lambda m: m["t"] == "undone", timeout=10)
            assert u["ok"] and ctl.system_eq()[0]["gain"] == 0.0
            for junk in ("not json", "[1]", json.dumps({"t": "x"}), json.dumps({"t": "set", "f": "master_db"})):
                await ws.send(junk)
                assert (await recv_until(ws, lambda m: m["t"] == "err"))["why"]
            await ws.send(json.dumps({"t": "preview", "text": "x" * 20000}))   # > MAX_TEXT, < frame cap
            assert (await recv_until(ws, lambda m: m["t"] == "preview"))["why"].startswith("too long")
            # two tune pages at most
            async with ws_connect(url, origin=origin) as ws2:
                await recv_until(ws2, lambda m: m["t"] == "hello")
                async with ws_connect(url, origin=origin) as ws3:
                    bye = await recv_until(ws3, lambda m: m["t"] == "bye")
                    assert bye["code"] == CLOSE_BUSY
            # the box closes laptop access: the page is dropped with 4004
            fake.set(False)
            bye = await recv_until(ws, lambda m: m["t"] == "bye", timeout=3)
            assert bye["code"] == CLOSE_LAPTOP
            with pytest.raises(ConnectionClosed):
                while True:
                    await asyncio.wait_for(ws.recv(), 3)
        assert ws.close_code == CLOSE_LAPTOP
        await until(lambda: not srv._tune_clients, what="tune pages gone")
        await srv.stop()
        await ctl.stop()
    run(go())


def test_phone_frames_stay_capped_at_1kb_on_the_bigger_server(tmp_path: Path):
    """The server reads up to TUNE_MAX_MESSAGE for the tune page; a phone's frame above 1 KB
    still closes that phone (1009), and a 1 KB one is still answered."""
    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        srv = PhoneServer(ctl, settings(tmp_path), tick_s=0.02)
        await srv.start()
        port = srv.ports["plain"]
        async with ws_connect(f"ws://127.0.0.1:{port}/ws", origin=f"http://127.0.0.1:{port}") as ws:
            await recv_until(ws, lambda m: m["t"] == "state")
            await ws.send(json.dumps({"t": "ping", "ts": 1, "pad": "x" * 900}))
            assert (await recv_until(ws, lambda m: m["t"] == "pong"))["ts"] == 1
            await ws.send(json.dumps({"t": "ping", "pad": "é" * 600}))        # 1.2 KB in UTF-8
            with pytest.raises(ConnectionClosed):
                while True:
                    await asyncio.wait_for(ws.recv(), 3)
            assert ws.close_code == 1009
        await srv.stop()
        await ctl.stop()
    run(go())
