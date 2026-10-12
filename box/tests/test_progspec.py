# SPDX-License-Identifier: GPL-3.0-or-later
"""The program spectrum for the phone's RESPONSE view (k1000/web/progspec.py, Phase 4 step 2,
docs/11-phone-mic.md): the `progspec` phone message, polling only while a phone listens, on its
own engine connection, at most 2 Hz, the profile's program channels only (never channel null),
L/R power-averaged, quiet retries on "No audio data available".

The engine side is a fake CamillaClient (the same surface: connect / command / close /
connected); the phone side is a real websocket against the real PhoneServer + Controller.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect as ws_connect

from k1000.cdsp import CdspError, CdspUnavailable
from k1000.profiles import load_profile
from k1000.web import progspec as ps
from k1000.web.server import MESSAGE_TYPES, PhoneServer
from test_web import DEV, boot, http_get, make_ctl, recv_until, run, settings, until

GRID = list(ps.GRID)


class FakeSpectrumEngine:
    """A second engine connection as progspec.py opens it. Program channel c reads -20 - 20*c
    dBFS flat; the first call answers like a fresh engine ("No audio data available")."""

    def __init__(self, url: str, *, fresh: bool = True, up: bool = True, channels: int = 2) -> None:
        self.url = url
        self.connected = False
        self.up = up
        self.fresh = fresh
        self.channels = channels
        self.calls: list[dict[str, Any]] = []
        self.times: list[float] = []
        self.closed = 0

    async def connect(self, timeout: float | None = None) -> bool:
        self.connected = self.up
        return self.connected

    async def close(self) -> None:
        self.connected = False
        self.closed += 1

    async def command(self, name: str, timeout: float | None = None, **fields: Any) -> Any:
        if not self.connected:
            raise CdspUnavailable("down")
        assert name == "GetSpectrum"
        v = fields["value"]
        self.calls.append(v)
        self.times.append(time.monotonic())
        if self.fresh:
            self.fresh = False
            raise CdspError(name, "InvalidRequestError", "No audio data available")
        ch = v["channel"]
        if not isinstance(ch, int) or ch >= self.channels:
            raise CdspError(name, "InvalidRequestError", f"Channel {ch} out of range ({self.channels} channels available)")
        n = v["n_bins"]
        r = (v["max_freq"] / v["min_freq"]) ** (1 / (n - 1))
        return {"frequencies": [v["min_freq"] * r ** i for i in range(n)], "magnitudes": [-20.0 - 20.0 * ch] * n}


def test_combine_power_and_channels() -> None:
    assert ps.combine_power([[-20.0, -10.0], [-40.0, -10.0]]) == [-23.0, -10.0]   # power, not dB, mean
    assert ps.combine_power([[-300.0], [-300.0]]) == [ps.FLOOR_DB]
    assert ps.combine_power([]) == []
    assert ps.program_channels(DEV) == list(DEV.input_map) == [0, 1]
    assert ps.program_channels(load_profile("m4", {})) == [2, 3]          # the M4's program inputs
    for bad in (None, object(), type("P", (), {"input_map": (None, True, -1, "2")})()):
        assert ps.program_channels(bad) == []
    assert ps.program_channels(type("P", (), {"input_map": (1, 1)})()) == [1]
    assert "progspec" in MESSAGE_TYPES


def test_program_frames_only_while_a_phone_listens(tmp_path: Path) -> None:
    async def go() -> None:
        ctl, eng = make_ctl(tmp_path)
        eng.url = "ws://127.0.0.1:1299"                  # the controller's engine address
        ctl_calls: list[str] = []
        orig = eng.command

        async def spy(name: str, timeout: float | None = None, **fields: Any) -> Any:
            ctl_calls.append(name)
            return await orig(name, timeout, **fields)
        eng.command = spy                                # type: ignore[method-assign]
        await boot(ctl)
        made: list[FakeSpectrumEngine] = []

        def factory(url: str) -> FakeSpectrumEngine:
            made.append(FakeSpectrumEngine(url))
            return made[-1]
        srv = PhoneServer(ctl, settings(tmp_path), tick_s=0.02, spectrum_client=factory)
        await srv.start()
        port = srv.ports["plain"]
        url, origin = f"ws://127.0.0.1:{port}/ws", f"http://127.0.0.1:{port}"
        async with ws_connect(url, origin=origin) as ws, ws_connect(url, origin=origin) as other:
            await recv_until(ws, lambda m: m["t"] == "state")
            await asyncio.sleep(0.6)
            assert made == [] and not srv.progspec.active, "nothing polled before a phone asks"
            await ws.send(json.dumps({"t": "progspec", "on": True, "id": 5}))
            assert (await recv_until(ws, lambda m: m["t"] == "ack"))["v"] is True
            first = await recv_until(ws, lambda m: m["t"] == "ps")
            assert first["m"] is None and first["why"] == "no audio data"        # a fresh engine, quiet
            frame = await recv_until(ws, lambda m: m["t"] == "ps" and m["m"] is not None, timeout=3.0)
            assert frame["grid"] == GRID and frame["ch"] == [0, 1] and len(frame["m"]) == GRID[2]
            assert frame["m"][0] == -23.0                                         # L -20, R -40: power mean
            assert len(made) == 1 and made[0].url == "ws://127.0.0.1:1299"       # its own connection
            # rate: at most 2 rounds (one call per program channel each) per second
            eng_ = made[0]
            r0, c0, t0 = srv.progspec.rounds, len(eng_.calls), time.monotonic()
            await asyncio.sleep(2.0)
            r1, c1, dt = srv.progspec.rounds, len(eng_.calls), time.monotonic() - t0
            assert 3 <= r1 - r0 <= math.ceil(2 * dt) + 1, f"{r1 - r0} rounds in {dt:.2f} s"
            assert c1 - c0 == 2 * (r1 - r0)
            # the shape of every request: capture side, a program channel, never null
            for v in eng_.calls:
                assert v == {"side": "capture", "channel": v["channel"], "min_freq": GRID[0],
                             "max_freq": GRID[1], "n_bins": GRID[2]}
                assert v["channel"] in (0, 1)
            assert "GetSpectrum" not in ctl_calls, "the control connection never carries it"
            # the other phone never asked: it gets no program frames
            got_other: list[str] = []
            while True:
                try:
                    got_other.append(json.loads(await asyncio.wait_for(other.recv(), 0.2))["t"])
                except (TimeoutError, asyncio.TimeoutError):
                    break
            assert "ps" not in got_other
            # off: polling stops and the engine connection closes
            await ws.send(json.dumps({"t": "progspec", "on": False}))
            await until(lambda: not srv.progspec.active and eng_.closed == 1, what="progspec off")
            n = len(eng_.calls)
            await asyncio.sleep(0.8)
            assert len(eng_.calls) == n
            # a bad request
            await ws.send(json.dumps({"t": "progspec", "on": "yes", "id": 9}))
            err = await recv_until(ws, lambda m: m["t"] == "err")
            assert err["id"] == 9 and "on: true or false" in err["why"]
            # on again, then the phone leaves: stopped
            await ws.send(json.dumps({"t": "progspec", "on": True}))
            await until(lambda: srv.progspec.active and len(made) == 2, what="on again")
        await until(lambda: not srv.progspec.active and made[1].closed == 1, what="stopped on disconnect")
        await srv.stop()
        await ctl.stop()
    run(go())


def test_two_listeners_one_poller_and_phone_off(tmp_path: Path) -> None:
    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        made: list[FakeSpectrumEngine] = []
        srv = PhoneServer(ctl, settings(tmp_path), tick_s=0.02,
                          spectrum_client=lambda url: made.append(FakeSpectrumEngine(url, fresh=False)) or made[-1])
        await srv.start()
        port = srv.ports["plain"]
        url, origin = f"ws://127.0.0.1:{port}/ws", f"http://127.0.0.1:{port}"
        async with ws_connect(url, origin=origin) as a, ws_connect(url, origin=origin) as b:
            for w in (a, b):
                await w.send(json.dumps({"t": "progspec", "on": True}))
            for w in (a, b):
                assert (await recv_until(w, lambda m: m["t"] == "ps" and m["m"] is not None, timeout=3.0))["m"]
            assert len(made) == 1, "one engine connection for every phone"
            await a.send(json.dumps({"t": "progspec", "on": False}))
            await asyncio.sleep(0.3)
            assert srv.progspec.active, "b still listens"
            srv.set_enabled(False)                       # SETUP > Phone OFF
            await until(lambda: not srv.progspec.active and made[0].closed == 1, what="off with the phones")
        await srv.stop()
        await ctl.stop()
    run(go())


def test_engine_down_and_bad_channels_stay_quiet(tmp_path: Path, caplog: Any) -> None:
    async def go() -> None:
        frames: list[dict[str, Any]] = []
        down = FakeSpectrumEngine("x", up=False)
        p = ps.ProgramSpectrum(on_frame=frames.append, channels_of=lambda: [0, 1],
                               client_factory=lambda url: down)
        p.period_s = 0.05                                # faster than the phone's 2 Hz, for the test
        p.set_active(True)
        await until(lambda: len(frames) >= 3, what="frames while down")
        assert all(f["m"] is None and f["why"] == "engine unavailable" for f in frames)
        await p.close()
        assert down.closed == 1 and not p.active
        # a profile whose program channel the device does not have: said once, never a flood
        frames.clear()
        narrow = FakeSpectrumEngine("x", fresh=False, channels=1)
        p = ps.ProgramSpectrum(on_frame=frames.append, channels_of=lambda: [0, 3],
                               client_factory=lambda url: narrow)
        p.period_s = 0.05
        with caplog.at_level(logging.WARNING, logger="k1000.web.progspec"):
            p.set_active(True)
            await until(lambda: len(frames) >= 5, what="frames with a bad channel")
            await p.close()
        assert all(f["m"] is None and f["why"].startswith("refused") for f in frames)
        assert sum("out of range" in r.getMessage() for r in caplog.records) == 1
        # nothing at all to poll: no program channels
        frames.clear()
        p = ps.ProgramSpectrum(on_frame=frames.append, channels_of=lambda: [None, True],
                               client_factory=lambda url: FakeSpectrumEngine(url))
        p.period_s = 0.05
        p.set_active(True)
        await until(lambda: len(frames) >= 2, what="frames without channels")
        await p.close()
        assert frames[0] == {"t": "ps", "grid": GRID, "ch": [], "m": None, "why": "no program channels"}
        # a listener that raises never stops the loop; a broken reply is not passed on
        calls = [0]

        def boom(frame: dict[str, Any]) -> None:
            calls[0] += 1
            raise ValueError("listener bug")
        weird = FakeSpectrumEngine("x", fresh=False)

        async def bad_reply(name: str, timeout: float | None = None, **fields: Any) -> Any:
            return {"magnitudes": [float("nan")] * GRID[2]}
        weird.command = bad_reply                        # type: ignore[method-assign]
        p = ps.ProgramSpectrum(on_frame=boom, channels_of=lambda: [0], client_factory=lambda url: weird)
        p.period_s = 0.05
        p.set_active(True)
        await until(lambda: calls[0] >= 3, what="the loop survives its listener")
        await p.close()
    run(go())


def test_frames_are_strict_json_and_floored(tmp_path: Path) -> None:
    async def go() -> None:
        frames: list[dict[str, Any]] = []
        eng = FakeSpectrumEngine("x", fresh=False)

        async def silent(name: str, timeout: float | None = None, **fields: Any) -> Any:
            return {"magnitudes": [-math.inf] + [-300.0] * (GRID[2] - 1)}
        eng.command = silent                             # type: ignore[method-assign]
        eng.connected = True
        p = ps.ProgramSpectrum(on_frame=frames.append, channels_of=lambda: [0, 1], client_factory=lambda url: eng)
        p.period_s = 0.05
        p.set_active(True)
        await until(lambda: frames, what="a frame")
        await p.close()
        text = json.dumps(frames[0], allow_nan=False)    # what the server sends: never NaN/inf
        assert json.loads(text)["m"] == [ps.FLOOR_DB] * GRID[2]
        assert ps.ProgramSpectrum(on_frame=frames.append, period_s=0.01).period_s == 0.5   # <= 2 Hz
    run(go())


def test_page_ships_the_mic_files(tmp_path: Path) -> None:
    """rta.js is served, loaded before app.js, cached by the service worker (cache v2), and the
    manifest lets the phone turn (this branch only: main stays portrait for Sunday)."""
    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        srv = PhoneServer(ctl, settings(tmp_path))
        await srv.start()
        port = srv.ports["plain"]
        st, h, body = await http_get(port, "/rta.js")
        assert st == 200 and h["content-type"].startswith("text/javascript") and b"K1000RTA" in body
        assert body.startswith(b"// SPDX-License-Identifier: GPL-3.0-or-later")
        index = (await http_get(port, "/"))[2]
        assert index.index(b'src="/rta.js"') < index.index(b'src="/app.js"')
        sw = (await http_get(port, "/sw.js"))[2]
        assert b'"/rta.js"' in sw and b'"k1000-v2-"' in sw
        man = json.loads((await http_get(port, "/manifest.webmanifest"))[2])
        assert man["orientation"] == "any"
        await srv.stop()
        await ctl.stop()
    run(go())
