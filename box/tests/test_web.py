# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for k1000.web (the phone surface, D-007): the message bridge against the REAL Controller
over an in-memory engine, the servers in-process on 127.0.0.1 (static files, websocket, Origin,
limits, phone on/off, error isolation, TLS with a throwaway certificate), the captive portal,
and the presence helpers the box screen uses.

No pytest-asyncio on the box (apt only): each test drives its own asyncio.run().
"""
from __future__ import annotations

import asyncio
import copy
import json
import math
import shutil
import socket
import ssl
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
import yaml
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from k1000 import dspconfig as dc
from k1000.cdsp import CdspError, CdspNoConfig, CdspUnavailable
from k1000.control import Controller, Paths, Status
from k1000.profiles import load_profile
from k1000.state import RANGES, State
from k1000.web import server as web
from k1000.web.server import (NUDGE_CAPS, SET_BOOL, SET_NUMERIC, PhoneBridge, PhoneServer,
                              PhoneSettings, ap_is_up, load_static, read_ap_info, read_arp,
                              read_leases, read_psk)


def run(coro: Any, limit: float = 30.0) -> Any:
    return asyncio.run(asyncio.wait_for(coro, limit))


async def until(pred: Any, timeout: float = 3.0, what: str = "condition") -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not pred():
        if loop.time() > deadline:
            raise AssertionError(f"{what} not met within {timeout} s")
        await asyncio.sleep(0.005)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ======================================================================= fake engine
def _set_ptr(cfg: dict[str, Any], pointer: str, value: Any) -> None:
    parts = pointer.strip("/").split("/")
    node = cfg
    for p in parts[:-1]:
        node = node[p]
    node[parts[-1]] = value


def _merge(dst: dict[str, Any], patch: dict[str, Any]) -> None:
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v)
        else:
            dst[k] = copy.deepcopy(v)


class FakeEngine:
    """The CamillaClient surface the Controller uses, in memory (cf. test_control.FakeClient)."""

    def __init__(self, cfg: dict[str, Any] | None) -> None:
        self.cfg = copy.deepcopy(cfg)
        self.faders = [{"volume": -40.0, "mute": True}] + [{"volume": 0.0, "mute": False} for _ in range(4)]
        self.connected = False
        self.on_connect: Any = None
        self.on_disconnect: Any = None
        self.peaks = [-20.0, -20.0]
        self.hooks: list[asyncio.Task[Any]] = []

    async def connect(self, timeout: float | None = None) -> bool:
        if not self.connected:
            self.connected = True
            if self.on_connect is not None:
                self.hooks.append(asyncio.get_running_loop().create_task(self.on_connect(self)))
        return True

    async def close(self) -> None:
        self.connected = False

    def _need(self) -> None:
        if not self.connected:
            raise CdspUnavailable("fake engine down")

    async def get_version(self) -> str:
        return "5.0.0-beta.1"

    async def get_config_json(self) -> dict[str, Any]:
        self._need()
        if self.cfg is None:
            raise CdspNoConfig("GetConfigJson", "NoActiveConfig")
        return copy.deepcopy(self.cfg)

    async def get_faders(self) -> list[dict[str, Any]]:
        self._need()
        return copy.deepcopy(self.faders)

    async def get_state(self) -> str:
        self._need()
        return "Running"

    async def get_processing_load(self) -> float:
        return 1.5

    async def get_clipped_samples(self) -> int:
        return 0

    async def get_stop_reason(self) -> str:
        return "None"

    async def command(self, name: str, timeout: float | None = None, **fields: Any) -> Any:
        self._need()
        if name == "GetPlaybackSignalPeakSinceLast":
            return list(self.peaks)
        raise CdspError(name, "Invalid", "fake: unknown")

    async def set_value(self, pointer: str, value: Any, clamp: Any = None) -> Any:
        self._need()
        _set_ptr(self.cfg, pointer, value)
        return value

    async def patch_config(self, patch: dict[str, Any]) -> None:
        self._need()
        _merge(self.cfg, patch)

    async def set_config_yaml(self, text: str, timeout: float = 10.0) -> None:
        self._need()
        self.cfg = yaml.safe_load(text)

    async def set_fader(self, idx: int, db: float) -> float:
        self._need()
        self.faders[idx]["volume"] = db
        return db

    async def set_fader_mute(self, idx: int, mute: bool) -> None:
        self._need()
        self.faders[idx]["mute"] = bool(mute)


async def _fast_sleep(s: float) -> None:
    await asyncio.sleep(0)


DEV = load_profile("dev", {})


def make_ctl(tmp_path: Path, state: State | None = None, **kw: Any) -> tuple[Controller, FakeEngine]:
    st = state.copy() if state is not None else State()
    eng = FakeEngine(dc.generate(DEV, st))
    opts: dict[str, Any] = dict(sleep=_fast_sleep, poll_s=3600, meter_s=3600, persist_debounce_s=0.05,
                                persist_max_delay_s=0.5, device_probe=lambda d: True)
    opts.update(kw)
    ctl = Controller(eng, DEV, st, Paths(tmp_path / "state.json", tmp_path / "active.yml"), **opts)
    return ctl, eng


async def boot(ctl: Controller) -> None:
    await ctl.start()
    await until(lambda: ctl.engine_ready, what="connect sync")
    assert await ctl.settle(timeout=3.0)


def settings(tmp_path: Path, **kw: Any) -> PhoneSettings:
    base: dict[str, Any] = dict(address="127.0.0.1", hosts=("127.0.0.1", "localhost"), https_port=0,
                                http_port=0, plain_port=free_port(), plain_app="always",
                                tls_dir=tmp_path / "tls", psk_file=tmp_path / "ap.psk",
                                ap_info_file=tmp_path / "ap.info",
                                state_file=tmp_path / "phone.json", lease_files=(),
                                arp_file=str(tmp_path / "arp"), sys_net=str(tmp_path / "sys"))
    base.update(kw)
    return PhoneSettings(**base)


# ======================================================================= settings
def test_settings_defaults() -> None:
    s = PhoneSettings.from_settings({})
    assert s == PhoneSettings()
    assert s.enabled and s.https_port == 443 and s.http_port == 80 and s.plain_app == "fallback"
    assert s.bind_address == "10.42.0.1"
    assert s.host_names == ("10.42.0.1", "k1000.home.arpa")
    assert str(s.subnet) == "10.42.0.0/24"
    assert PhoneSettings.from_settings(None) == PhoneSettings()
    assert PhoneSettings.from_settings({"phone": "yes"}) == PhoneSettings()


def test_settings_parse_and_fall_back(caplog: Any) -> None:
    s = PhoneSettings.from_settings({"phone": {
        "enabled": False, "https_port": 8443, "max_clients": 2, "ssid": "K1000-B", "notch_q": 10,
        "extra_origins": ["http://localhost:18080"], "plain_app": "always", "band": "2.4 GHz · ch 6",
    }})
    assert (s.enabled, s.https_port, s.max_clients, s.ssid, s.notch_q) == (False, 8443, 2, "K1000-B", 10.0)
    assert s.extra_origins == ("http://localhost:18080",) and s.plain_app == "always"
    bad = PhoneSettings.from_settings({"phone": {
        "enabled": "yes", "https_port": "443", "max_clients": 100, "plain_app": "maybe",
        "notch_gain_db": 3.0, "address": "not-an-ip", "ssid": "x" * 40, "rate_per_s": float("nan"),
        "iface": "../etc", "http_port": True,
    }})
    assert bad == PhoneSettings()
    assert "invalid" in caplog.text


# ======================================================================= the bridge
def test_bridge_nudge_master_and_allowlist(tmp_path: Path) -> None:
    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        b = PhoneBridge(ctl)
        assert b.apply({"t": "nudge", "f": "master_db", "d": -2}) == (True, -21.0, "")
        for d in (3, -13, 0, True, 1.5, "1", None):
            ok, _, why = b.apply({"t": "nudge", "f": "master_db", "d": d})
            assert not ok and why, d
        before = ctl.state.to_dict()
        for f in ("sub_delay_ms", "top_delay_ms", "mono_subs", "profile", "dev_audition", "out_trim_db.0",
                  "sys_peq.0.gain", "input_trim_db", "sub_inverted", "muted", "__class__", "", None, 3):
            ok, _, why = b.apply({"t": "nudge", "f": f, "d": 1})
            assert not ok and "not a phone control" in why, f
            ok, _, _ = b.apply({"t": "set", "f": f, "v": 1})
            assert not ok, f
        ok, _, why = b.apply({"t": "set", "f": "master_db", "v": 0.0})   # never an absolute master
        assert not ok
        assert ctl.state.to_dict() == before
        assert b.apply({"t": "nudge", "f": "balance_db", "d": 2})[:2] == (True, 1.0)
        assert b.apply({"t": "nudge", "f": "lowcut_hz", "d": -12})[1] == pytest.approx(50.0, abs=0.1)
        assert b.apply({"t": "nudge", "f": "live_peq.2.q", "d": 6})[1] == pytest.approx(8.0, abs=0.01)
        assert b.apply({"t": "nudge", "f": "live_peq.0.freq", "d": 12, "fine": True})[1] == pytest.approx(
            250.0 * 2 ** (12 / 48), rel=1e-3)
        assert b.apply({"t": "warp"})[0] is False
        await ctl.stop()
    run(go())


def test_bridge_master_rise_is_rate_limited(tmp_path: Path) -> None:
    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        now = [100.0]
        b = PhoneBridge(ctl, master_up_db_per_s=6.0, clock=lambda: now[0])
        ups = [b.apply({"t": "nudge", "f": "master_db", "d": 2})[0] for _ in range(6)]
        assert ups[:3] == [True, True, True] and not any(ups[3:])     # 3 dB burst
        assert b.apply({"t": "nudge", "f": "master_db", "d": 2})[2] == "master: rising too fast"
        assert all(b.apply({"t": "nudge", "f": "master_db", "d": -1})[0] for _ in range(20))  # down: never
        now[0] += 1.0
        assert b.apply({"t": "nudge", "f": "master_db", "d": 2})[0]
        await ctl.stop()
    run(go())


def test_bridge_set_band_notch(tmp_path: Path) -> None:
    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        b = PhoneBridge(ctl, notch_gain_db=-6.0, notch_q=8.0)
        assert b.apply({"t": "set", "f": "live_peq.1.freq", "v": 2500})[:2] == (True, 2500.0)
        assert b.apply({"t": "set", "f": "live_peq.1.gain", "v": 99})[1] == RANGES["live_peq.gain"].max
        assert b.apply({"t": "set", "f": "live_lowshelf.freq", "v": 5})[1] == RANGES["live_lowshelf.freq"].min
        for v in ("2", None, True, float("nan"), float("inf"), [1]):
            assert not b.apply({"t": "set", "f": "live_peq.1.freq", "v": v})[0], v
        assert b.apply({"t": "set", "f": "lowcut_on", "v": False})[:2] == (True, False)
        assert ctl.state.lowcut_on is False
        assert not b.apply({"t": "set", "f": "lowcut_on", "v": "false"})[0]
        assert not b.apply({"t": "set", "f": "lowcut_on", "v": 0})[0]
        # band: f/q before g; bad slot / values refused
        ok, v, _ = b.apply({"t": "band", "s": 2, "f": 500, "g": -4, "q": 6})
        assert ok and v == {"f": 500.0, "q": 6.0, "g": -4.0}
        assert (ctl.state.live_peq[2].freq, ctl.state.live_peq[2].gain) == (500.0, -4.0)
        for bad in ({"s": 6, "g": 0}, {"s": -1, "g": 0}, {"s": True, "g": 0}, {"s": 0},
                    {"s": 0, "g": "x"}, {"s": 0, "f": float("nan")}):
            assert not b.apply({"t": "band", **bad})[0], bad
        ok, v, _ = b.apply({"t": "band", "s": 5, "g": -20})          # high shelf: own range
        assert ok and v["g"] == RANGES["live_highshelf.gain"].min
        # notch here: the first FLAT PEQ slot (slot 2 is in use now)
        ok, v, _ = b.apply({"t": "notch", "hz": 2520})
        assert ok and v == {"s": 0, "f": 2520.0, "q": 8.0, "g": -6.0}
        assert b.apply({"t": "notch", "hz": 100000})[1]["s"] == 3          # slot 1 got +6 above
        assert ctl.state.live_peq[3].freq == RANGES["live_peq.freq"].max
        ok, v, why = b.apply({"t": "notch", "hz": 800})
        assert not ok and why == "all 4 notches in use"
        assert not b.apply({"t": "notch", "hz": "800"})[0]
        await ctl.stop()
    run(go())


def test_bridge_mute_and_reset(tmp_path: Path) -> None:
    async def go() -> None:
        ctl, eng = make_ctl(tmp_path)
        await boot(ctl)
        b = PhoneBridge(ctl)
        for v in ("yes", 1, None, "toggle"):
            assert not b.apply({"t": "mute", "on": v})[0], v
        assert b.apply({"t": "mute", "on": True}) == (True, True, "")
        await until(lambda: eng.faders[0]["mute"] is True, what="engine muted")
        # an unmute needs the generation the phone saw (an old page sends none: refused)
        ok, v, why = b.apply({"t": "mute", "on": False})
        assert (ok, v) == (False, True) and "reload" in why
        muted, gen = b.mute_view()
        assert muted is True
        assert not b.apply({"t": "mute", "on": False, "seq": gen - 1})[0]
        assert not b.apply({"t": "mute", "on": False, "seq": True})[0]
        assert b.apply({"t": "mute", "on": False, "seq": gen}) == (True, False, "")
        await until(lambda: eng.faders[0]["mute"] is False, what="engine unmuted")
        _, gen = b.mute_view()
        assert b.apply({"t": "mute", "on": False, "seq": gen}) == (True, False, "")   # already: no-op
        b.apply({"t": "band", "s": 0, "g": -9})
        assert not b.apply({"t": "reset_live"})[0]
        assert not b.apply({"t": "reset_live", "confirm": "yes"})[0]
        assert b.apply({"t": "reset_live", "confirm": True})[0]
        assert ctl.state.live_peq[0].gain == 0.0
        await ctl.stop()
    run(go())


class GenController(Controller):
    """The Controller with the mute generation WIRING.md adds to control.py (§2): the bridge then
    uses the Controller's generation, which B2 also moves. (b2_muted() exists from the
    integration round on; before it, what a hold acts against is the displayed mute.)"""

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.mute_seq = 0
        self._mute_seen: tuple[bool, bool] | None = None

    def mute_view(self) -> tuple[bool, int]:
        disp = self.displayed_muted()
        bm = getattr(super(), "b2_muted", None)
        key = (disp, bool(bm()) if callable(bm) else disp)
        if key != self._mute_seen:
            self._mute_seen = key
            self.mute_seq += 1
        return key[1], self.mute_seq

    def set_mute(self, on: Any = "toggle", seen: int | None = None) -> bool:
        if on is None or on == "toggle":
            on = not self.displayed_muted()
        if not on and seen is not None and seen != self.mute_view()[1]:
            return self.displayed_muted()
        self.mute_seq += 1
        return super().set_mute(bool(on))


@pytest.mark.parametrize("wired", [False, True], ids=["bridge-generation", "controller-generation"])
def test_a_stale_unmute_never_undoes_a_mute(tmp_path: Path, wired: bool) -> None:
    """psafe-1/2: whoever started to UNMUTE on what they saw must not undo a mute made meanwhile
    (B2 on the box, another phone); a MUTE always goes through."""
    async def go() -> None:
        st = State()
        eng = FakeEngine(dc.generate(DEV, st))
        cls = GenController if wired else Controller
        ctl = cls(eng, DEV, st, Paths(tmp_path / "state.json", tmp_path / "active.yml"), sleep=_fast_sleep,
                  poll_s=3600, meter_s=3600, persist_debounce_s=0.05, persist_max_delay_s=0.5,
                  device_probe=lambda d: True)
        await boot(ctl)
        p1, p2 = PhoneBridge(ctl), PhoneBridge(ctl)     # two phones (two bridges = worst case)
        # S1: phone 1 sees UNMUTED and starts HOLD TO MUTE; the box is muted by B2 meanwhile.
        seen = p1.mute_view()
        assert seen[0] is False
        ctl.set_mute(True)                               # B2 long
        await until(lambda: eng.faders[0]["mute"] is True)
        # the fixed page sends what the finger meant (mute) -> stays muted
        assert p1.apply({"t": "mute", "on": True}) == (True, True, "")
        # an old page decides at release time from the NEW state (on: false) with the OLD seq
        ok, v, _ = p1.apply({"t": "mute", "on": False, "seq": seen[1]})
        assert (ok, v) == (False, True)
        await asyncio.sleep(0.02)
        assert ctl.displayed_muted() is True and eng.faders[0]["mute"] is True
        # S3: both phones see MUTED; phone 2 unmutes, then phone 1 mutes again; phone 2's second,
        # stale unmute (and phone 1's...) must not pass
        m1, g1 = p1.mute_view()
        m2, g2 = p2.mute_view()
        assert m1 and m2
        assert p2.apply({"t": "mute", "on": False, "seq": g2})[0]
        await until(lambda: eng.faders[0]["mute"] is False)
        assert p1.apply({"t": "mute", "on": True})[0]
        await until(lambda: eng.faders[0]["mute"] is True)
        assert not p2.apply({"t": "mute", "on": False, "seq": g2})[0]
        assert not p1.apply({"t": "mute", "on": False, "seq": g1})[0]
        await asyncio.sleep(0.02)
        assert eng.faders[0]["mute"] is True and ctl.state.muted is True
        # a fresh look, then an unmute, works
        _, g = p1.mute_view()
        assert p1.apply({"t": "mute", "on": False, "seq": g}) == (True, False, "")
        await until(lambda: eng.faders[0]["mute"] is False)
        if wired:   # the knob side of WIRING: a B2 unmute seen before a phone's mute is refused
            ctl.set_mute(True)
            await until(lambda: eng.faders[0]["mute"] is True)
            seen_b2 = ctl.mute_view()[1]                 # B2 pressed while MUTED
            assert p2.apply({"t": "mute", "on": False, "seq": p2.mute_view()[1]})[0]
            assert p1.apply({"t": "mute", "on": True})[0]  # a phone mutes during the B2 hold
            assert ctl.set_mute(False, seen=seen_b2) is True
            await asyncio.sleep(0.02)
            assert eng.faders[0]["mute"] is True
        await ctl.stop()
    run(go())


def test_state_and_meter_messages_are_strict_json(tmp_path: Path) -> None:
    st = State()
    st.live_peq[1].gain = -6.5
    status = Status(engine="Running", load=float("nan"), levels=[-12.26, float("-inf"), -200.0, 3.0],
                    clip=[False, False, False, True], muted=True, device="HiFiBerry", profile="dev",
                    device_present=True, drift=["SUBS muted outside K1000"])
    msg = PhoneBridge.state_message(("MAIN", st, status, None), phone_on=True, phones=2)
    text = json.dumps(msg, allow_nan=False)            # raises on NaN/inf
    back = json.loads(text)
    assert back["muted"] is True and back["mute_known"] is True and back["load"] is None
    assert len(back["bands"]) == 6 and back["bands"][1] == {"f": 1000.0, "g": -6.5, "q": 4.0}
    assert back["drift"] == ["SUBS muted outside K1000"] and back["phones"] == 2
    status.muted = None
    assert PhoneBridge.state_message(("MAIN", st, status, None), phone_on=True, phones=0)["muted"] is False
    m = PhoneBridge.meters_message(status)
    json.dumps(m, allow_nan=False)
    assert m == {"t": "m", "l": [-12.5, -60.0, -60.0, 0.0], "c": [False, False, False, True]}
    assert PhoneBridge.meters_message(object())["l"] == [-60.0] * 4


def test_allowlist_is_the_phone_scope() -> None:
    """The phone reaches MAIN (steps) and the live EQ only: no setup-class field, ever."""
    setup = {"sub_delay_ms", "top_delay_ms", "mono_subs", "profile", "dev_audition", "input_trim_db",
             "sub_inverted", "top_inverted", "sub_lowpass_on", "sub_lowpass_hz"}
    reach = set(NUDGE_CAPS) | set(SET_NUMERIC) | set(SET_BOOL)
    assert not reach & setup
    assert not any(f.startswith(("sys_peq", "out_trim")) for f in reach)
    assert {"master_db", "balance_db", "lowcut_hz", "lowcut_on"} <= reach
    assert "master_db" not in SET_NUMERIC and "balance_db" not in SET_NUMERIC


# ======================================================================= servers in-process
async def http_get(port: int, path: str, *, host: str | None = None, headers: dict[str, str] | None = None,
                   ssl_ctx: ssl.SSLContext | None = None, method: str = "GET",
                   raw: bytes | None = None) -> tuple[int, dict[str, str], bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=ssl_ctx,
                                                   server_hostname="localhost" if ssl_ctx else None)
    try:
        if raw is None:
            lines = [f"{method} {path} HTTP/1.1", f"Host: {host or f'127.0.0.1:{port}'}"]
            lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
            raw = ("\r\n".join(lines) + "\r\n\r\n").encode()
        writer.write(raw)
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        status_line, *hl = head.decode("latin-1").split("\r\n")
        hdrs = {k.strip().lower(): v.strip() for k, v in (h.split(":", 1) for h in hl if ":" in h)}
        n = int(hdrs.get("content-length", "0"))
        body = await asyncio.wait_for(reader.readexactly(n), 5) if n and method != "HEAD" else b""
        return int(status_line.split()[1]), hdrs, body
    finally:
        writer.close()


async def recv_until(ws: Any, pred: Any, timeout: float = 2.0) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        left = deadline - loop.time()
        if left <= 0:
            raise AssertionError("no matching message")
        try:
            m = json.loads(await asyncio.wait_for(ws.recv(), left))
        except (TimeoutError, asyncio.TimeoutError):
            raise AssertionError("no matching message") from None
        if pred(m):
            return m


def test_static_files_and_headers(tmp_path: Path) -> None:
    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        srv = PhoneServer(ctl, settings(tmp_path))
        await srv.start()
        port = srv.ports["plain"]
        st, h, body = await http_get(port, "/")
        assert st == 200 and h["content-type"].startswith("text/html") and b"/app.js" in body
        assert "content-security-policy" in h and f"ws://127.0.0.1:{port}" in h["content-security-policy"]
        assert h["x-content-type-options"] == "nosniff" and h["connection"] == "close"
        for path, ctype in (("/app.js", "text/javascript"), ("/app.css", "text/css"),
                            ("/manifest.webmanifest", "application/manifest+json"),
                            ("/icons/icon-192.png", "image/png"), ("/sw.js", "text/javascript")):
            st, h, body = await http_get(port, path + "?v=1")
            assert st == 200 and h["content-type"].startswith(ctype), path
        man = json.loads((await http_get(port, "/manifest.webmanifest"))[2])
        assert man["start_url"] == "/" and man["scope"] == "/" and man["display"] == "standalone"
        sizes = {(i["sizes"], i["purpose"]) for i in man["icons"]}
        assert {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")} <= sizes
        for i in man["icons"]:
            st, _, png = await http_get(port, i["src"])
            assert st == 200 and png[:8] == b"\x89PNG\r\n\x1a\n"
            w, hh = int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")
            assert f"{w}x{hh}" == i["sizes"]
        sw = (await http_get(port, "/sw.js"))[2]
        assert b"__K1000_BUILD__" not in sw and srv.build.encode() in sw
        for bad in ("/nope", "/../server.py", "/static/app.js", "/%2e%2e/server.py", "/portal.html",
                    "/web/server.py"):
            assert (await http_get(port, bad))[0] == 404, bad
        st, h, _ = await http_get(port, "/app.js")
        assert (await http_get(port, "/app.js", headers={"If-None-Match": h["etag"]}))[0] == 304
        st, h, _ = await http_get(port, "/ping")
        assert st == 204 and h["access-control-allow-origin"] == "*"
        await srv.stop()
        await ctl.stop()
    run(go())


def test_build_id_follows_the_files(tmp_path: Path) -> None:
    src = web.STATIC_DIR
    dst = tmp_path / "static"
    shutil.copytree(src, dst)
    a, build_a = load_static(dst)
    (dst / "app.css").write_text((dst / "app.css").read_text() + "\n/* x */\n")
    b, build_b = load_static(dst)
    assert build_a != build_b and a["/sw.js"].etag != b["/sw.js"].etag
    (dst / ".hidden.js").write_text("x")
    (dst / "notes.md").write_text("x")
    c, _ = load_static(dst)
    assert "/.hidden.js" not in c and "/notes.md" not in c


def test_websocket_state_push_and_control(tmp_path: Path) -> None:
    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        srv = PhoneServer(ctl, settings(tmp_path), tick_s=0.02)
        await srv.start()
        url = f"ws://127.0.0.1:{srv.ports['plain']}/ws"
        async with ws_connect(url, origin=f"http://127.0.0.1:{srv.ports['plain']}") as ws:
            hello = json.loads(await ws.recv())
            assert hello["t"] == "hello" and hello["v"] == 1 and hello["phone_on"] is True
            assert hello["ranges"]["live_peq.gain"]["max"] == RANGES["live_peq.gain"].max
            st = await recv_until(ws, lambda m: m["t"] == "state")
            assert st["master_db"] == -20.0 and st["phones"] == 1 and st["engine"] == "Running"
            await ws.send(json.dumps({"t": "nudge", "f": "master_db", "d": -2, "id": 7}))
            ack = await recv_until(ws, lambda m: m["t"] == "ack")
            assert ack == {"t": "ack", "id": 7, "v": -21.0}
            await recv_until(ws, lambda m: m["t"] == "state" and m["master_db"] == -21.0)
            assert ctl.state.master_db == -21.0
            # a KNOB move (the Controller API) reaches the phone quickly
            t0 = time.monotonic()
            ctl.nudge("master_db", -4)
            await recv_until(ws, lambda m: m["t"] == "state" and m["master_db"] == -23.0)
            assert time.monotonic() - t0 < 0.2
            await ws.send(json.dumps({"t": "ping", "ts": 123}))
            assert (await recv_until(ws, lambda m: m["t"] == "pong"))["ts"] == 123
            assert srv.rx["nudge"] == 1 and srv.rx["ping"] == 1
        await until(lambda: srv.phones == 0, what="phone gone")
        await srv.stop()
        await ctl.stop()
    run(go())


def test_websocket_origin_checked(tmp_path: Path) -> None:
    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        srv = PhoneServer(ctl, settings(tmp_path))
        await srv.start()
        port = srv.ports["plain"]
        url = f"ws://127.0.0.1:{port}/ws"
        for origin in ("http://evil.example", f"https://127.0.0.1:{port}", "null", None):
            with pytest.raises(InvalidStatus) as e:
                async with ws_connect(url, origin=origin):
                    pass
            assert e.value.response.status_code == 403, origin
        async with ws_connect(url, origin=f"http://localhost:{port}") as ws:
            assert json.loads(await ws.recv())["t"] == "hello"
        assert srv.stats["refused"] == 4
        assert srv.origins() == {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
        await srv.stop()
        await ctl.stop()
    run(go())


def test_max_clients_and_phone_off(tmp_path: Path) -> None:
    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        cfg = settings(tmp_path, max_clients=2)
        srv = PhoneServer(ctl, cfg)
        await srv.start()
        port = srv.ports["plain"]
        url, origin = f"ws://127.0.0.1:{port}/ws", f"http://127.0.0.1:{port}"
        a = await ws_connect(url, origin=origin)
        b = await ws_connect(url, origin=origin)
        await until(lambda: srv.phones == 2)
        async with ws_connect(url, origin=origin) as c:
            bye = json.loads(await c.recv())
            assert bye["t"] == "bye" and bye["code"] == 4002
            with pytest.raises(ConnectionClosed) as e:
                await c.recv()
            assert e.value.rcvd.code == 4002
        # the SETUP switch: phones are told and closed, new ones refused, nothing applies
        srv.set_enabled(False)
        for ws in (a, b):
            m = await recv_until(ws, lambda m: m["t"] == "bye")
            assert m["code"] == 4003 and m["phone_on"] is False
            with pytest.raises(ConnectionClosed):
                while True:
                    await ws.recv()
        async with ws_connect(url, origin=origin) as d:
            assert json.loads(await d.recv())["code"] == 4003
        await asyncio.sleep(0.05)
        assert json.loads(cfg.state_file.read_text()) == {"on": False}
        assert PhoneServer(ctl, cfg).enabled is False                 # survives a restart
        assert srv.screen_info().on is False
        srv.set_enabled(True)
        async with ws_connect(url, origin=origin) as e2:
            assert json.loads(await e2.recv())["t"] == "hello"
        await asyncio.sleep(0.05)
        assert json.loads(cfg.state_file.read_text()) == {"on": True}
        await srv.stop()
        await ctl.stop()
    run(go())


def test_bad_clients_are_isolated(tmp_path: Path) -> None:
    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        srv = PhoneServer(ctl, settings(tmp_path), tick_s=0.02)
        await srv.start()
        port = srv.ports["plain"]
        url, origin = f"ws://127.0.0.1:{port}/ws", f"http://127.0.0.1:{port}"
        async with ws_connect(url, origin=origin) as bad, ws_connect(url, origin=origin) as good:
            for ws in (bad, good):
                await recv_until(ws, lambda m: m["t"] == "state")
            for junk in ("not json", "[1,2]", "null", '{"t": 5}', '{"t": "nudge"}',
                         '{"t": "nudge", "f": "master_db", "d": 1e999}', '{"t":"set","f":"profile","v":"m4"}',
                         '{"t": "band", "s": 0, "g": NaN}', json.dumps({"t": "x" * 500})):
                await bad.send(junk)
            await bad.send(b"\x00\x01binary")
            errs = 0
            with pytest.raises(AssertionError):
                while True:
                    await recv_until(bad, lambda m: m["t"] == "err", timeout=0.3)
                    errs += 1
            assert 1 <= errs <= 10
            await good.send(json.dumps({"t": "nudge", "f": "balance_db", "d": 1, "id": "g"}))
            assert (await recv_until(good, lambda m: m["t"] == "ack"))["v"] == 0.5
            await recv_until(good, lambda m: m["t"] == "state" and m["balance_db"] == 0.5)
            assert ctl.state.profile == "dev" and ctl.state.master_db == -20.0
            # an over-size message closes THAT phone only (1009), the other keeps working
            await bad.send("x" * 5000)
            with pytest.raises(ConnectionClosed):
                while True:
                    await bad.recv()
            ctl.nudge("master_db", -1)
            await recv_until(good, lambda m: m["t"] == "state" and m["master_db"] == -20.5)
        await srv.stop()
        await ctl.stop()
    run(go())


def test_rate_limit_never_blocks_a_mute(tmp_path: Path) -> None:
    async def go() -> None:
        ctl, eng = make_ctl(tmp_path)
        await boot(ctl)
        srv = PhoneServer(ctl, settings(tmp_path, rate_per_s=2.0, burst=3))
        await srv.start()
        port = srv.ports["plain"]
        async with ws_connect(f"ws://127.0.0.1:{port}/ws", origin=f"http://127.0.0.1:{port}") as ws:
            for i in range(8):
                await ws.send(json.dumps({"t": "nudge", "f": "live_peq.0.gain", "d": -1, "id": i}))
            await ws.send(json.dumps({"t": "mute", "on": True, "id": "m"}))
            ack = await recv_until(ws, lambda m: m["t"] == "ack" and m["id"] == "m")
            assert ack["v"] is True
        assert srv.stats["rate_limited"] >= 4
        assert ctl.state.live_peq[0].gain == -1.5          # the 3-message burst got through
        await until(lambda: eng.faders[0]["mute"] is True, what="engine muted")
        await srv.stop()
        await ctl.stop()
    run(go())


class _RampCtl:
    """A Controller stand-in in the boot-ramp situation: Main is muted by K1000 itself (shown
    MUTED), the operator did not mute, so B2 means 'keep muted' (control.b2_muted() False)."""

    def __init__(self) -> None:
        self.shown, self.b2, self.calls = True, False, []

    def snapshot(self) -> tuple[Any, Any, Any, Any]:
        return "MAIN", State(), Status(muted=self.shown), None

    def displayed_muted(self) -> bool:
        return self.shown

    def b2_muted(self) -> bool:
        return self.b2

    def set_mute(self, on: Any) -> bool:
        self.calls.append(on)
        return bool(on)


def test_a_hold_follows_b2s_rule_during_a_ramp() -> None:
    ctl = _RampCtl()
    b = PhoneBridge(ctl)
    act, gen = b.mute_view()
    assert act is False                                  # the hold KEEPS it muted
    msg = PhoneBridge.state_message(ctl.snapshot(), phone_on=True, phones=1, mute=(act, gen))
    assert msg["muted"] is True and msg["hold"] == "mute" and msg["mute_seq"] == gen
    ok, v, why = b.apply({"t": "mute", "on": False, "seq": gen})
    assert (ok, v) == (False, True) and "by itself" in why and ctl.calls == []
    assert b.apply({"t": "mute", "on": True}) == (True, True, "") and ctl.calls == [True]
    ctl.b2 = True                                        # the operator's mute now: a hold unmutes
    act, gen2 = b.mute_view()
    assert act is True and gen2 != gen
    assert b.apply({"t": "mute", "on": False, "seq": gen2})[0] and ctl.calls == [True, False]


def test_mute_generation_on_the_wire(tmp_path: Path) -> None:
    """The state carries mute_seq; an unmute with the seq the page showed works, a stale one is
    refused with an err that carries the mute as it is (never throttled away)."""
    async def go() -> None:
        ctl, eng = make_ctl(tmp_path)
        await boot(ctl)
        srv = PhoneServer(ctl, settings(tmp_path), tick_s=0.02)
        await srv.start()
        port = srv.ports["plain"]
        async with ws_connect(f"ws://127.0.0.1:{port}/ws", origin=f"http://127.0.0.1:{port}") as ws:
            s0 = await recv_until(ws, lambda m: m["t"] == "state")
            assert s0["muted"] is False and isinstance(s0["mute_seq"], int)
            await ws.send(json.dumps({"t": "mute", "on": True, "id": "mute"}))
            assert (await recv_until(ws, lambda m: m["t"] == "ack"))["v"] is True
            s1 = await recv_until(ws, lambda m: m["t"] == "state" and m["muted"] is True)
            assert s1["mute_seq"] != s0["mute_seq"]
            # a second phone presses MUTE too (it saw the old state): a new generation, so the
            # first phone's unmute based on s1 is now stale
            async with ws_connect(f"ws://127.0.0.1:{port}/ws", origin=f"http://127.0.0.1:{port}") as ws2:
                await recv_until(ws2, lambda m: m["t"] == "state")
                await ws2.send(json.dumps({"t": "mute", "on": True, "id": "mute"}))
                await recv_until(ws2, lambda m: m["t"] == "ack")
            await recv_until(ws, lambda m: m["t"] == "state" and m["mute_seq"] != s1["mute_seq"])
            for _ in range(8):          # errors are throttled, refused unmutes are not
                await ws.send(json.dumps({"t": "mute", "on": False, "seq": s1["mute_seq"], "id": "mute"}))
                e = await recv_until(ws, lambda m: m["t"] == "err" and m["id"] == "mute")
                assert e["v"] is True and "MUTE CHANGED" in e["why"]
            assert ctl.displayed_muted() is True and eng.faders[0]["mute"] is True
            seq = srv.bridge.mute_view()[1]
            await ws.send(json.dumps({"t": "mute", "on": False, "seq": seq, "id": "mute"}))
            assert (await recv_until(ws, lambda m: m["t"] == "ack" and m["id"] == "mute"))["v"] is False
            await until(lambda: eng.faders[0]["mute"] is False, what="unmuted")
        await srv.stop()
        await ctl.stop()
    run(go())


class _StalledWS:
    """A phone whose downlink is stuck (every send hangs) while its uplink still delivers."""

    def __init__(self, frames: list[str]) -> None:
        self.frames, self.sent, self.aborted = list(frames), [], False
        self.remote_address = ("10.42.0.66", 40000)
        self.gate = asyncio.Event()

        class _T:
            def abort(inner) -> None:   # noqa: N805
                self.aborted = True
        self.transport = _T()

    def __aiter__(self) -> "_StalledWS":
        return self

    async def __anext__(self) -> str:
        if not self.frames:
            await self.gate.wait()
            raise StopAsyncIteration
        await asyncio.sleep(0.001)
        return self.frames.pop(0)

    async def send(self, text: str) -> None:
        await asyncio.Event().wait()     # never returns: the send buffer is full

    async def close(self, *a: Any) -> None:
        pass


def test_a_stalled_downlink_never_delays_a_mute(tmp_path: Path) -> None:
    """psafe-4: replies go through the sender; the receive loop never waits on a send, so a MUTE
    right behind a ping is applied at once even when nothing reaches the phone any more."""
    async def go() -> None:
        ctl, eng = make_ctl(tmp_path)
        await boot(ctl)
        srv = PhoneServer(ctl, settings(tmp_path), send_timeout_s=5.0)
        ws = _StalledWS([json.dumps({"t": "ping", "ts": 1}), json.dumps({"t": "nudge", "f": "master_db",
                                                                         "d": -1, "id": 1}),
                         json.dumps({"t": "mute", "on": True, "id": "mute"})])
        task = asyncio.get_running_loop().create_task(srv._ws_handler(ws))
        t0 = time.monotonic()
        await until(lambda: ctl.state.muted is True, timeout=1.0, what="mute applied")
        assert time.monotonic() - t0 < 0.5
        await until(lambda: eng.faders[0]["mute"] is True, what="engine muted")
        assert srv.rx == {"ping": 1, "nudge": 1, "mute": 1}
        # the replies wait for the stuck sender, bounded (pongs dropped first)
        c = next(iter(srv._clients))
        for i in range(web.OUT_MAX + 10):
            srv._queue(c, {"t": "pong", "ts": i})
        assert len(c.out) == web.OUT_MAX
        assert any('"ack"' in x for x in c.out)          # the acks survived the flood of pongs
        ws.gate.set()
        await asyncio.wait_for(task, 2.0)
        await ctl.stop()
    run(go())


def test_hostile_numbers_and_types_stay_quiet(tmp_path: Path, caplog: Any) -> None:
    """psafe-6: legal JSON that is not a sane value (a 400-digit integer, a list as a field
    name) is refused like any bad value: the connection stays, at most ONE traceback per phone."""
    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        srv = PhoneServer(ctl, settings(tmp_path), tick_s=0.02)
        await srv.start()
        port = srv.ports["plain"]
        big = "9" * 400
        frames = ['{"t":"ping","ts":%s}' % big, '{"t":"set","f":[1],"v":1}', '{"t":"set","f":{"a":1},"v":1}',
                  '{"t":"nudge","f":[1],"d":1}', '{"t":"band","s":0,"g":-%s}' % big,
                  '{"t":"notch","hz":%s}' % big, '{"t":"set","f":"live_peq.0.freq","v":%s}' % big,
                  '{"t":"band","s":%s,"g":-3}' % big, '{"t":"notch","hz":800,"s":%s}' % big]
        async with ws_connect(f"ws://127.0.0.1:{port}/ws", origin=f"http://127.0.0.1:{port}") as ws:
            await recv_until(ws, lambda m: m["t"] == "state")
            for _ in range(4):
                for f in frames:
                    assert len(f) <= web.MAX_MESSAGE
                    await ws.send(f)
            await ws.send(json.dumps({"t": "ping", "ts": 5}))
            assert (await recv_until(ws, lambda m: m["t"] == "pong" and m["ts"] == 5))
            await ws.send(json.dumps({"t": "nudge", "f": "balance_db", "d": 1, "id": "ok"}))
            assert (await recv_until(ws, lambda m: m["t"] == "ack" and m["id"] == "ok"))["v"] == 0.5
        assert srv.stats["errors"] == 0 and srv.stats["connects"] == 1
        assert ctl.state.live_peq[0].gain == 0.0 and ctl.state.live_peq[0].freq != float(big)
        tracebacks = [r for r in caplog.records if r.name == "k1000.web" and r.exc_info]
        assert len(tracebacks) <= 1
        # and the bucket on the log itself: a phone that does trigger a bug logs it once in full
        c = web._Client(object(), "10.42.0.7", web._Bucket(30, 40, time.monotonic),
                        log_bucket=web._Bucket(0.2, 3.0, time.monotonic))
        caplog.clear()
        for _ in range(50):
            srv._log_failure(c, "boom", ValueError("x"))
        recs = [r for r in caplog.records if r.name == "k1000.web"]
        assert len(recs) <= 4 and sum(1 for r in recs if r.exc_info) == 1
        await srv.stop()
        await ctl.stop()
    run(go())


def test_hello_carries_the_build_and_the_page_knows_its_own(tmp_path: Path) -> None:
    """ppwa-6: the page compares its stamped build with the box's and reloads once on a change."""
    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        srv = PhoneServer(ctl, settings(tmp_path))
        await srv.start()
        port = srv.ports["plain"]
        app = (await http_get(port, "/app.js"))[2]
        assert b"__K1000_BUILD__" not in app and srv.build.encode() in app
        async with ws_connect(f"ws://127.0.0.1:{port}/ws", origin=f"http://127.0.0.1:{port}") as ws:
            hello = json.loads(await ws.recv())
            assert hello["t"] == "hello" and hello["build"] == srv.build and len(srv.build) == 12
        await srv.stop()
        await ctl.stop()
    run(go())


def test_notch_on_the_predicted_slot(tmp_path: Path) -> None:
    """ppwa-8: the page predicts the slot and drags it under the same finger; the box uses that
    slot only while it is still flat (a drag can never move a band someone else just set)."""
    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        b = PhoneBridge(ctl)
        assert b.apply({"t": "notch", "hz": 800, "s": 2}) == (True, {"s": 2, "f": 800.0, "q": 8.0, "g": -6.0}, "")
        ok, _, why = b.apply({"t": "notch", "hz": 1000, "s": 2})
        assert not ok and "P3" in why and "in use" in why
        for bad in (4, -1, True, "1", 1.0):
            assert not b.apply({"t": "notch", "hz": 1000, "s": bad})[0], bad
        assert b.apply({"t": "notch", "hz": 500})[1]["s"] == 0         # no s: the first flat one
        await ctl.stop()
    run(go())


def test_qr_cycle_includes_the_setup_page_when_the_portal_is_served(tmp_path: Path) -> None:
    cfg = settings(tmp_path, address="10.42.0.1", hosts=())
    now = [0.0]
    srv = PhoneServer(object(), cfg, clock=lambda: now[0])
    srv.ports["https"], srv.ports["http"] = 443, 80
    assert srv.setup_url == "http://10.42.0.1/"
    assert srv.screen_info().setup_url == "http://10.42.0.1/"
    assert [srv.select_qr(1) for _ in range(4)] == ["url", "setup", "wifi", "url"]
    assert srv.select_qr(-1) == "wifi" and srv.select_qr(-1) == "setup"
    del srv.ports["http"]                                     # forced 'setup', then no portal:
    assert srv.setup_url == "" and srv.screen_info().qr == "wifi"   # not shown any more
    assert srv.select_qr(1) == "url"


class _HangingWS:
    def __init__(self, delay: float) -> None:
        self.delay, self.sent, self.aborted = delay, [], False

        class _T:
            def abort(inner) -> None:   # noqa: N805
                self.aborted = True
        self.transport = _T()

    async def send(self, text: str) -> None:
        await asyncio.sleep(self.delay)
        self.sent.append(text)


def test_slow_phone_is_dropped_and_skips_states(tmp_path: Path) -> None:
    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        srv = PhoneServer(ctl, settings(tmp_path), send_timeout_s=0.1)
        stuck = web._Client(_HangingWS(5.0), "10.42.0.9", web._Bucket(30, 40, time.monotonic))
        with pytest.raises(ConnectionClosed):
            await srv._send(stuck, "x")
        assert stuck.ws.aborted and srv.stats["dropped"] == 1
        # a slow (not stuck) phone only ever gets the LATEST state: intermediate ones are skipped
        slow = web._Client(_HangingWS(0.05), "10.42.0.8", web._Bucket(30, 40, time.monotonic))
        srv._clients.add(slow)
        task = asyncio.get_running_loop().create_task(srv._sender(slow))
        for _ in range(10):
            ctl.nudge("master_db", -1)
            srv._refresh()
            await asyncio.sleep(0.01)
        await until(lambda: slow.sent_state == srv._state_seq, what="caught up")
        states = [json.loads(t) for t in slow.ws.sent if json.loads(t)["t"] == "state"]
        assert len(states) < 10 and states[-1]["master_db"] == -25.0
        task.cancel()
        await ctl.stop()
    run(go())


# ======================================================================= captive portal
def _ca(tmp_path: Path) -> Path:
    """A syntactically valid certificate to serve as 'the CA' (content is not checked here)."""
    d = tmp_path / "tls"
    d.mkdir(exist_ok=True)
    der = b"0\x82\x01\x00" + bytes(256)
    (d / "k1000-ca.crt").write_text(ssl.DER_cert_to_PEM_cert(der))
    return d


def test_portal_routes(tmp_path: Path) -> None:
    cfg = settings(tmp_path, address="10.42.0.1", hosts=(), tls_dir=_ca(tmp_path), http_port=80,
                   plain_app="off")
    srv = PhoneServer(object(), cfg)
    srv._static, _ = load_static(web.STATIC_DIR)
    srv._load_ca()
    srv.ports["http"] = 80

    def get(head: str) -> tuple[int, dict[str, str], bytes]:
        raw = srv.portal_response(head.encode("latin-1"))
        h, _, body = raw.partition(b"\r\n\r\n")
        line, *hl = h.decode("latin-1").split("\r\n")
        hdrs = {k.strip().lower(): v.strip() for k, v in (x.split(":", 1) for x in hl)}
        assert int(hdrs["content-length"]) == len(body) or (head.startswith("HEAD") and not body)
        return int(line.split()[1]), hdrs, body

    # every connectivity probe is redirected: that is what raises "Sign in to network"
    for head in ("GET /generate_204 HTTP/1.1\r\nHost: connectivitycheck.gstatic.com\r\n\r\n",
                 "GET http://connectivitycheck.gstatic.com/generate_204 HTTP/1.1\r\n\r\n",
                 "GET /hotspot-detect.html HTTP/1.0\r\nHost: captive.apple.com\r\n\r\n",
                 "HEAD /connecttest.txt HTTP/1.1\r\nHost: www.msftconnecttest.com\r\n\r\n",
                 "GET /whatever HTTP/1.1\r\nHost: 10.42.0.1\r\n\r\n"):
        st, h, _ = get(head)
        assert st == 302 and h["location"] == "http://10.42.0.1/", head
    st, h, body = get("GET / HTTP/1.1\r\nHost: 10.42.0.1\r\n\r\n")
    assert st == 200 and h["content-type"].startswith("text/html") and h["cache-control"] == "no-store"
    text = body.decode()
    assert "{{" not in text and 'data-app="https://10.42.0.1/"' in text and "/k1000-ca.crt" in text
    assert 'data-ping=""' in text           # no TLS loaded here: no trust check
    st, h, body = get("GET / HTTP/1.1\r\nHost: k1000.home.arpa\r\n\r\n")
    assert st == 200
    st, h, body = get("HEAD / HTTP/1.1\r\nHost: 10.42.0.1\r\n\r\n")
    assert st == 200 and body == b"" and int(h["content-length"]) > 100
    st, h, body = get("GET /k1000-ca.crt HTTP/1.1\r\nHost: 10.42.0.1\r\n\r\n")
    assert st == 200 and h["content-type"] == "application/x-x509-ca-cert"
    assert body.startswith(b"-----BEGIN CERTIFICATE-----") and "attachment" in h["content-disposition"]
    st, h, body = get("GET /k1000-ca.der HTTP/1.1\r\nHost: 10.42.0.1\r\n\r\n")
    assert st == 200 and body[:2] == b"0\x82"
    for path in ("/portal.js", "/portal.css", "/icons/icon-192.png"):
        assert get(f"GET {path} HTTP/1.1\r\nHost: 10.42.0.1\r\n\r\n")[0] == 200, path
    assert get("GET /app.js HTTP/1.1\r\nHost: 10.42.0.1\r\n\r\n")[0] == 302   # the app is on HTTPS
    assert get("POST / HTTP/1.1\r\nHost: 10.42.0.1\r\n\r\n")[0] == 405
    assert get("garbage\r\n\r\n")[0] == 400
    assert get("GET / SPDY/3\r\n\r\n")[0] == 400
    srv._ca_pem = srv._ca_der = None
    assert get("GET /k1000-ca.crt HTTP/1.1\r\nHost: 10.42.0.1\r\n\r\n")[0] == 404


def test_portal_over_tcp(tmp_path: Path) -> None:
    async def go() -> None:
        port = free_port()
        srv = PhoneServer(object(), settings(tmp_path, http_port=port, plain_app="off"),
                          presence_s=3600)
        srv._dirty = False
        await srv.start()
        assert set(srv.ports) == {"http"}
        st, h, _ = await http_get(port, "/generate_204", host="connectivitycheck.gstatic.com")
        assert st == 302 and h["location"] == f"http://127.0.0.1:{port}/"
        st, h, body = await http_get(port, "/", host="127.0.0.1")
        assert st == 200 and b"K1000" in body
        # a request that never finishes its head does not hold the server
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(b"GET / HTTP/1.1\r\nHost: x")
        st, _, _ = await http_get(port, "/", host="127.0.0.1")
        assert st == 200
        w.close()
        await srv.stop()
    run(go())


# ======================================================================= TLS
def _self_signed(d: Path) -> Path | None:
    if shutil.which("openssl") is None:
        return None
    d.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
                        "-nodes", "-keyout", str(d / "k1000.key"), "-out", str(d / "k1000.crt"),
                        "-days", "2", "-subj", "/CN=localhost",
                        "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return None
    shutil.copy(d / "k1000.crt", d / "k1000-ca.crt")
    return d


def test_tls_page_and_wss(tmp_path: Path) -> None:
    tls = _self_signed(tmp_path / "tls")
    if tls is None:
        pytest.skip("openssl CLI not available to make a throwaway certificate")

    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        cfg = settings(tmp_path, https_port=free_port(), http_port=free_port(), plain_app="fallback",
                       tls_dir=tls)
        srv = PhoneServer(ctl, cfg)
        await srv.start()
        assert set(srv.ports) == {"https", "http"}        # TLS loaded: no plain fallback
        port = srv.ports["https"]
        assert srv.url == f"https://127.0.0.1:{port}/" and srv.screen_info().tls
        client = ssl.create_default_context(cafile=str(tls / "k1000.crt"))
        st, h, body = await http_get(port, "/", ssl_ctx=client)
        assert st == 200 and f"wss://127.0.0.1:{port}" in h["content-security-policy"]
        st, h, _ = await http_get(port, "/ping", ssl_ctx=client)
        assert st == 204 and h["access-control-allow-origin"] == "*"
        async with ws_connect(f"wss://127.0.0.1:{port}/ws", ssl=client,
                              origin=f"https://localhost:{port}") as ws:
            assert json.loads(await ws.recv())["t"] == "hello"
        # a client that does not trust the box (Android's HTTPS probe) fails quietly
        with pytest.raises(ssl.SSLError):
            await http_get(port, "/", ssl_ctx=ssl.create_default_context())
        st, _, body = await http_get(port, "/", ssl_ctx=client)
        assert st == 200
        # the portal knows where to check trust and where the app is
        st, _, body = await http_get(srv.ports["http"], "/", host="127.0.0.1")
        assert f'data-ping="https://127.0.0.1:{port}/ping"'.encode() in body
        await srv.stop()
        await ctl.stop()
    run(go())


def test_no_certificate_falls_back_to_plain(tmp_path: Path, caplog: Any) -> None:
    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        cfg = settings(tmp_path, https_port=free_port(), plain_app="fallback")
        srv = PhoneServer(ctl, cfg)
        await srv.start()
        assert set(srv.ports) == {"plain"} and "no certificate" in srv.tls_error
        info = srv.screen_info()
        assert not info.tls and info.url == f"http://127.0.0.1:{cfg.plain_port}/"
        await srv.stop()
        # unreadable key: said plainly (the deploy mode is root:k1000 0640)
        bad = tmp_path / "badtls"
        bad.mkdir()
        (bad / "k1000.crt").write_text("not a cert")
        (bad / "k1000.key").write_text("not a key")
        srv2 = PhoneServer(ctl, settings(tmp_path, https_port=free_port(), tls_dir=bad))
        await srv2.start()
        assert "bad certificate" in srv2.tls_error and srv2.screen_info().error
        await srv2.stop()
        await ctl.stop()
    run(go())


def test_start_never_raises(tmp_path: Path) -> None:
    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen(1)
            taken = busy.getsockname()[1]
            srv = PhoneServer(ctl, settings(tmp_path, plain_port=taken, http_port=free_port()))
            await srv.start()
            assert "plain" not in srv.ports and "http" in srv.ports
            assert "cannot listen" in srv.screen_info().error
            await srv.stop()
            # a certificate, but 443 refused (e.g. no CAP_NET_BIND_SERVICE): the plain page still runs
            tls = _self_signed(tmp_path / "tls443")
            if tls is not None:
                srv = PhoneServer(ctl, settings(tmp_path, https_port=taken, plain_app="fallback",
                                                tls_dir=tls))
                await srv.start()
                assert set(srv.ports) == {"plain"}
                assert srv.url == f"http://127.0.0.1:{srv.ports['plain']}/"
                assert srv.screen_info().tls is False
                await srv.stop()
        srv = PhoneServer(ctl, settings(tmp_path), static_dir=tmp_path / "missing")
        await srv.start()          # no files: 404s, but the websocket and the box still work
        await srv.stop()
        off = PhoneServer(ctl, settings(tmp_path, enabled=False))
        await off.start()
        assert off.ports == {} and off.screen_info().feature is False
        await off.stop()
        await ctl.stop()
    run(go())


# ======================================================================= presence helpers
ARP = """IP address       HW type     Flags       HW address            Mask     Device
192.0.2.17       0x1         0x2         aa:bb:cc:dd:ee:00     *        eth0
10.42.0.23       0x1         0x2         aa:bb:cc:dd:ee:01     *        wlan0
10.42.0.24       0x1         0x0         00:00:00:00:00:00     *        wlan0
10.42.0.25       0x1         0x2         aa:bb:cc:dd:ee:02     *        wlan0
10.42.0.1        0x1         0x2         aa:bb:cc:dd:ee:03     *        wlan0
10.43.0.9        0x1         0x2         aa:bb:cc:dd:ee:04     *        wlan0
garbage line
"""


def test_read_arp_and_leases() -> None:
    import ipaddress
    net = ipaddress.ip_network("10.42.0.0/24")
    assert read_arp(ARP, "wlan0", net, exclude=("10.42.0.1",)) == {"10.42.0.23", "10.42.0.25"}
    assert read_arp(ARP, "eth0", net) == set()
    assert read_arp("", "wlan0", net) == set()
    now = 1_000_000.0
    leases = ("1000100 aa:bb:cc:dd:ee:01 10.42.0.23 pixel 01:aa\n"
              "999000 aa:bb:cc:dd:ee:02 10.42.0.24 old *\n"
              "0 aa:bb:cc:dd:ee:03 10.42.0.30 forever *\n"
              "x y z\n1000100 aa 192.0.2.2 other *\n")
    assert read_leases(leases, net, now) == {"10.42.0.23", "10.42.0.30"}


def test_ap_state_psk_and_screen_info(tmp_path: Path) -> None:
    cfg = settings(tmp_path, address="10.42.0.1", hosts=())
    sysnet = Path(cfg.sys_net) / "wlan0"
    assert ap_is_up(cfg, lambda i: "10.42.0.1") is None              # no interface
    sysnet.mkdir(parents=True)
    (sysnet / "operstate").write_text("up\n")
    assert ap_is_up(cfg, lambda i: "10.42.0.1") is True
    assert ap_is_up(cfg, lambda i: None) is False                    # up, but not the AP address
    (sysnet / "operstate").write_text("down\n")
    assert ap_is_up(cfg, lambda i: "10.42.0.1") is False
    # the Wi-Fi password file
    assert read_psk(cfg.psk_file)[0] is None and "setup-ap" in read_psk(cfg.psk_file)[1]
    cfg.psk_file.write_text("m7kq-4tzh-p2xv\n")
    assert read_psk(cfg.psk_file) == ("m7kq-4tzh-p2xv", "")
    cfg.psk_file.write_text("short\n")
    assert read_psk(cfg.psk_file)[0] is None
    cfg.psk_file.write_text("pässwörd-mit-umlaut\n")
    assert read_psk(cfg.psk_file)[0] is None
    cfg.psk_file.write_text("m7kq-4tzh-p2xv\n")
    # the screen: Wi-Fi QR until a phone is on the AP, then the URL; E1 overrides
    now = [0.0]
    srv = PhoneServer(object(), cfg, clock=lambda: now[0])
    srv.refresh_presence()
    info = srv.screen_info()
    assert (info.qr, info.stations, info.psk, info.ssid) == ("wifi", 0, "m7kq-4tzh-p2xv", "K1000")
    assert srv.select_qr(1) == "url" and srv.screen_info().qr == "url"     # E1: the other code
    assert srv.select_qr(-1) == "wifi" and srv.screen_info().qr == "wifi"
    Path(cfg.arp_file).write_text(ARP)
    srv.refresh_presence()
    assert srv.screen_info().stations == 2 and srv.screen_info().qr == "wifi"   # still forced
    now[0] += web.QR_OVERRIDE_S + 1
    assert srv.screen_info().qr == "url"                  # back to auto: a phone is on the AP
    assert srv.select_qr(1) == "wifi" and srv.screen_info().qr == "wifi"
    assert srv.toggle() is False and srv.screen_info().on is False
    assert srv.toggle() is True
    # what setup-ap.sh really set wins over k1000.yaml (an outdoor 2.4 GHz re-setup shows)
    assert (srv.screen_info().ssid, srv.screen_info().band) == ("K1000", "")
    cfg.ap_info_file.write_text("ssid=K1000-B\nband=bg\nchannel=6\nlabel=2.4 GHz · ch 6\nbogus=1\n")
    srv.refresh_presence()
    assert (srv.screen_info().ssid, srv.screen_info().band) == ("K1000-B", "2.4 GHz · ch 6")
    assert read_ap_info(cfg.ap_info_file) == {"ssid": "K1000-B", "band": "bg", "channel": "6",
                                              "label": "2.4 GHz · ch 6"}
    cfg.ap_info_file.write_text("ssid=" + "x" * 33 + "\n")
    assert read_ap_info(cfg.ap_info_file) == {}
    # a USB dongle (setup-ap.sh --iface wlan1): ap.info names the interface, the screen follows it
    (Path(cfg.sys_net) / "wlan1").mkdir(parents=True)
    (Path(cfg.sys_net) / "wlan1" / "operstate").write_text("up\n")
    assert ap_is_up(cfg, lambda i: "10.42.0.1" if i == "wlan1" else None, iface="wlan1") is True
    cfg.ap_info_file.write_text("iface=wlan1\nbackend=hostapd\nportal=off\nssh=off\n")
    assert read_ap_info(cfg.ap_info_file)["iface"] == "wlan1"
    cfg.ap_info_file.write_text("iface=wl an/1\n")
    assert "iface" not in read_ap_info(cfg.ap_info_file)


def test_subnet_clash_finds_another_interface_in_the_ap_subnet(tmp_path: Path) -> None:
    """A laptop on eth0 with NetworkManager's 'Shared to other computers' (10.42.0.1/24 too)."""
    cfg = settings(tmp_path, address="10.42.0.1", hosts=())
    for n in ("lo", "eth0", "wlan0"):
        (Path(cfg.sys_net) / n).mkdir(parents=True)
    addrs = {"lo": "127.0.0.1", "eth0": "192.0.2.10", "wlan0": "10.42.0.1"}
    assert web.subnet_clash(cfg, "wlan0", addrs.get) == ""
    addrs["eth0"] = "10.42.0.23"
    assert web.subnet_clash(cfg, "wlan0", addrs.get) == "eth0 10.42.0.23"
    addrs["eth0"] = None                                         # no address at all
    assert web.subnet_clash(cfg, "wlan0", addrs.get) == ""
    assert web.subnet_clash(settings(tmp_path, sys_net=str(tmp_path / "none")), "wlan0", addrs.get) == ""
    cfg.ap_info_file.write_text("ssid=K1000\nstate=down\n")
    assert read_ap_info(cfg.ap_info_file)["state"] == "down"


def test_knobs_offline_alert_follows_the_phone_surface(tmp_path: Path) -> None:
    """SAFE-1, wired: the alert names the phone mute only while one is there to use (servers up,
    SETUP › Phone on, the AP up, no subnet clash). The PHONE page says why the AP is down."""
    from k1000 import control as ctl_mod

    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        cfg = settings(tmp_path)
        sysnet = Path(cfg.sys_net)
        for n in ("eth0", "wlan0"):
            (sysnet / n).mkdir(parents=True)
            (sysnet / n / "operstate").write_text("up\n")
        addrs = {"wlan0": "127.0.0.1", "eth0": "192.0.2.10"}
        srv = PhoneServer(ctl, cfg)
        srv._addr_of = addrs.get
        ctl.set_knobs_ok(False)
        assert ctl.status.alert == ctl_mod.MSG_KNOBS_OFFLINE              # no phone yet
        await srv.start()
        ctl.phone = srv
        ctl.phone_changed()                                               # what __main__ does
        assert srv.remote_mute_ready and ctl.status.alert == ctl_mod.MSG_KNOBS_OFFLINE_PHONE
        srv.set_enabled(False)                                            # SETUP › Phone off
        assert not srv.remote_mute_ready and ctl.status.alert == ctl_mod.MSG_KNOBS_OFFLINE
        srv.set_enabled(True)
        assert ctl.status.alert == ctl_mod.MSG_KNOBS_OFFLINE_PHONE
        addrs["eth0"] = "127.0.0.23"                                      # a laptop sharing on eth0
        srv.refresh_presence()
        assert srv.screen_info().ap_clash == "eth0 127.0.0.23"
        assert ctl.status.alert == ctl_mod.MSG_KNOBS_OFFLINE
        addrs["eth0"] = "192.0.2.10"
        srv.refresh_presence()
        assert srv.screen_info().ap_clash == "" and ctl.status.alert == ctl_mod.MSG_KNOBS_OFFLINE_PHONE
        (sysnet / "wlan0" / "operstate").write_text("down\n")           # the no-go: AP down
        cfg.ap_info_file.write_text("ssid=K1000\nstate=down\n")
        srv.refresh_presence()
        info = srv.screen_info()
        assert info.ap_up is False and info.ap_kept_down is True
        assert ctl.status.alert == ctl_mod.MSG_KNOBS_OFFLINE
        await srv.stop()
        assert not srv.remote_mute_ready
        await ctl.stop()

    run(go())
    off = PhoneServer(object(), settings(tmp_path, enabled=False))
    run(off.start())
    assert off.remote_mute_ready is False                                 # phone.enabled: false


def test_values_are_finite_on_the_wire(tmp_path: Path) -> None:
    """Everything the bridge returns as an ack value is JSON-strict (no NaN reaches the page)."""
    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        b = PhoneBridge(ctl)
        for msg in ({"t": "nudge", "f": "live_peq.1.q", "d": 12}, {"t": "band", "s": 4, "f": 1e9, "g": -1e9},
                    {"t": "set", "f": "live_highshelf.q", "v": 1e-12}, {"t": "notch", "hz": -5}):
            ok, v, _ = b.apply(msg)
            assert ok, msg
            json.dumps(v, allow_nan=False)
            for x in (v.values() if isinstance(v, dict) else [v]):
                assert isinstance(x, (int, float)) and math.isfinite(x)
        await ctl.stop()
    run(go())
